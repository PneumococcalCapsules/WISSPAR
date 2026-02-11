"""
Reconciliation engine for dual-extraction workflow.

Compares rows from Agent A and Agent B, classifies disagreements,
generates review CSVs for human adjudication, and produces audit trails.
"""

import csv
import json
import os
from extractor_base import (
    CSV_FIELDNAMES, EXTRACTIONS_DIR, VERSION, now_iso,
)

# ---------------------------------------------------------------------------
# Field classification for comparison
# ---------------------------------------------------------------------------

# Fields that come directly from the API -- must match exactly between agents
IDENTITY_FIELDS = {
    "clinical_trial_study_id",
    "outcome_overview_serotype",
    "outcome_overview_id",
}

NUMERIC_API_FIELDS = {
    "outcome_overview_value",
    "outcome_overview_upper_limit",
    "outcome_overview_lower_limit",
    "outcome_overview_participants",
}

PASSTHROUGH_FIELDS = {
    "outcome_overview_time_frame",
    "outcome_overview_description",
}

METADATA_FIELDS = {
    "clinical_trial_study_name",
    "clinical_trial_sponsor",
    "clinical_trial_responsible_party",
    "clinical_trial_phase",
    "location_country_code",
    "location_continent",
    "study_eligibility_standard_age_list",
    "study_eligibility_ethnicity",
}

# Fields where agents may legitimately differ due to different interpretation logic
INTERPRETED_FIELDS = {
    "outcome_overview_assay",
    "outcome_overview_vaccine",
    "outcome_overview_manufacturer",
    "outcome_overview_schedule",
    "outcome_overview_dose_number",
    "outcome_overview_dose_description",
    "outcome_overview_time_frame_weeks",
    "outcome_overview_title",  # Agent maps group title differently
}

# Fields that are typically empty or defaulted
DEFAULT_FIELDS = {
    "outcome_overview_ratio",
    "outcome_overview_immunocompromised_population",
    "outcome_overview_confidence_interval",
    "outcome_overview_percent_responders",
}


# ---------------------------------------------------------------------------
# Row matching
# ---------------------------------------------------------------------------

def _source_key(row):
    """Primary matching key: structural position in the JSON."""
    addr = row.get("_source_address", {})
    return (
        addr.get("outcome_index"),
        addr.get("class_index"),
        addr.get("category_index"),
        addr.get("measurement_index"),
    )


def _content_key(row):
    """Fallback matching key: (serotype, group_id, value)."""
    return (
        row.get("outcome_overview_serotype", ""),
        row.get("outcome_overview_id", ""),
        row.get("outcome_overview_value", ""),
    )


def match_rows(rows_a, rows_b):
    """Match rows from Agent A and Agent B.

    Returns:
        matched: list of (row_a, row_b) tuples
        unmatched_a: list of rows only in Agent A
        unmatched_b: list of rows only in Agent B
    """
    matched = []
    unmatched_a = []
    used_b_indices = set()

    # Build index for Agent B by source key
    b_by_source = {}
    for i, row in enumerate(rows_b):
        key = _source_key(row)
        if key != (None, None, None, None):
            b_by_source[key] = i

    # Build index for Agent B by content key
    b_by_content = {}
    for i, row in enumerate(rows_b):
        key = _content_key(row)
        if i not in used_b_indices:
            b_by_content.setdefault(key, []).append(i)

    # Phase 1: match by structural position
    for row_a in rows_a:
        key = _source_key(row_a)
        if key in b_by_source:
            b_idx = b_by_source[key]
            if b_idx not in used_b_indices:
                matched.append((row_a, rows_b[b_idx]))
                used_b_indices.add(b_idx)
                continue
        unmatched_a.append(row_a)

    # Phase 2: content-based fallback for unmatched A rows
    still_unmatched_a = []
    for row_a in unmatched_a:
        key = _content_key(row_a)
        candidates = b_by_content.get(key, [])
        found = False
        for b_idx in candidates:
            if b_idx not in used_b_indices:
                matched.append((row_a, rows_b[b_idx]))
                used_b_indices.add(b_idx)
                found = True
                break
        if not found:
            still_unmatched_a.append(row_a)

    # Collect unmatched B rows
    unmatched_b = [rows_b[i] for i in range(len(rows_b)) if i not in used_b_indices]

    return matched, still_unmatched_a, unmatched_b


# ---------------------------------------------------------------------------
# Field comparison
# ---------------------------------------------------------------------------

def compare_fields(row_a, row_b):
    """Compare all CSV fields between two matched rows.

    Returns list of disagreement dicts.
    """
    disagreements = []

    for field in CSV_FIELDNAMES:
        val_a = str(row_a.get(field, ""))
        val_b = str(row_b.get(field, ""))

        if val_a == val_b:
            continue

        if field in IDENTITY_FIELDS:
            dtype = "identity_mismatch"
        elif field in NUMERIC_API_FIELDS:
            dtype = "numeric"
        elif field in METADATA_FIELDS or field in PASSTHROUGH_FIELDS:
            dtype = "metadata"
        elif field in INTERPRETED_FIELDS:
            dtype = "categorical"
        elif field in DEFAULT_FIELDS:
            dtype = "default"
        else:
            dtype = "categorical"

        disagreements.append({
            "field": field,
            "agent_a_value": val_a,
            "agent_b_value": val_b,
            "type": dtype,
        })

    return disagreements


# ---------------------------------------------------------------------------
# Auto-resolution rules for trivial disagreements
# ---------------------------------------------------------------------------

# Canonical manufacturer short names and their known long-form variants
_MANUFACTURER_CANONICAL = {
    "GLAXOSMITHKLINE": "GSK",
    "GLAXOSMITHKLINE BIOLOGICALS": "GSK",
    "GLAXOSMITHKLINE LLC": "GSK",
    "MERCK": "MSD",
    "MERCK & CO": "MSD",
    "MERCK SHARP & DOHME": "MSD",
    "MERCK SHARP & DOHME LLC": "MSD",
    "MERCK SHARP & DOHME CORP": "MSD",
    "PFIZER INC": "Pfizer",
    "WYETH": "Pfizer",
    "WYETH-LEDERLE": "Pfizer",
    "WYETH PHARMACEUTICALS": "Pfizer",
    "SERUM INSTITUTE OF INDIA PVT. LTD.": "Serum Institute of India",
}

_CANONICAL_NAMES = {"Pfizer", "GSK", "MSD", "Vaxcyte", "Inventprise",
                    "Serum Institute of India", "Walvax"}


def _normalize_manufacturer(val):
    """Return canonical short name if val is a known variant, else val."""
    upper = val.strip().upper()
    return _MANUFACTURER_CANONICAL.get(upper, val.strip())


def _strip_redundant_mfr_suffix(vaccine_name):
    """Strip redundant manufacturer suffix from vaccine name.

    e.g. 'PCV21(Merck V116) (MSD)' -> 'PCV21(Merck V116)'
         'PPV23 (MSD)' -> 'PPV23'
    """
    import re
    # Remove trailing ' (Pfizer)', ' (MSD)', ' (GSK)' etc. when already named
    stripped = re.sub(r'\s+\((?:Pfizer|GSK|MSD|Vaxcyte|Inventprise|Walvax'
                      r'|Serum Institute of India)\)$', '', vaccine_name)
    return stripped


def auto_resolve_disagreements(disagreements, row_a, row_b):
    """Try to auto-resolve trivial disagreements.

    Returns (resolved_fields, remaining_disagreements) where resolved_fields
    is a dict of {field: chosen_value} for fields that were auto-resolved.
    """
    resolved = {}
    remaining = []

    for d in disagreements:
        field = d["field"]
        val_a = d["agent_a_value"]
        val_b = d["agent_b_value"]
        auto = None

        # Rule 1: Manufacturer normalization
        if field == "outcome_overview_manufacturer":
            norm_a = _normalize_manufacturer(val_a)
            norm_b = _normalize_manufacturer(val_b)
            if norm_a == norm_b:
                auto = norm_a
            elif norm_a in _CANONICAL_NAMES and norm_b not in _CANONICAL_NAMES:
                auto = norm_a
            elif norm_b in _CANONICAL_NAMES and norm_a not in _CANONICAL_NAMES:
                auto = norm_b

        # Rule 2: Assay — accept the non-Unknown value
        elif field == "outcome_overview_assay":
            if val_a == "Unknown" and val_b in ("OPA", "GMC"):
                auto = val_b
            elif val_b == "Unknown" and val_a in ("OPA", "GMC"):
                auto = val_a

        # Rule 3: Vaccine — strip redundant manufacturer suffix
        elif field == "outcome_overview_vaccine":
            stripped_a = _strip_redundant_mfr_suffix(val_a)
            stripped_b = _strip_redundant_mfr_suffix(val_b)
            if stripped_a == stripped_b:
                auto = stripped_a
            # "None" vs a real vaccine name: accept the real name
            elif val_a == "None" and val_b not in ("None", ""):
                auto = val_b
            elif val_b == "None" and val_a not in ("None", ""):
                auto = val_a

        # Rule 4: dose_description — normalize "4w" to "1m"
        elif field == "outcome_overview_dose_description":
            norm_a = val_a.replace("4w post", "1m post")
            norm_b = val_b.replace("4w post", "1m post")
            if norm_a == norm_b:
                auto = norm_a

        # Rule 5: time_frame_weeks — prefer non-empty when one is empty
        elif field == "outcome_overview_time_frame_weeks":
            if val_a == "" and val_b != "":
                auto = val_b
            elif val_b == "" and val_a != "":
                auto = val_a

        if auto is not None:
            resolved[field] = auto
        else:
            remaining.append(d)

    return resolved, remaining


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile(rows_a, rows_b, nct_id):
    """Compare two agent extractions and produce a reconciliation result.

    Returns a dict with:
        summary: counts and status
        rows: per-row comparison results
    """
    matched, unmatched_a, unmatched_b = match_rows(rows_a, rows_b)

    result_rows = []
    counts = {
        "total_rows_a": len(rows_a),
        "total_rows_b": len(rows_b),
        "matched_pairs": len(matched),
        "unmatched_a": len(unmatched_a),
        "unmatched_b": len(unmatched_b),
        "agreements": 0,
        "numeric_disagreements": 0,
        "categorical_disagreements": 0,
        "metadata_disagreements": 0,
        "selection_disagreements": len(unmatched_a) + len(unmatched_b),
    }

    # Process matched pairs
    for idx, (row_a, row_b) in enumerate(matched):
        disagreements = compare_fields(row_a, row_b)

        if not disagreements:
            status = "AGREE"
            counts["agreements"] += 1
            # Auto-accept: use Agent A values (identical to B)
            final_row = {k: row_a[k] for k in CSV_FIELDNAMES}
            resolution = "auto_accepted"
        else:
            # Try auto-resolution of trivial disagreements
            resolved_fields, remaining = auto_resolve_disagreements(
                disagreements, row_a, row_b
            )

            if not remaining:
                # All disagreements were auto-resolved
                status = "AGREE"
                counts["agreements"] += 1
                final_row = {k: row_a[k] for k in CSV_FIELDNAMES}
                for field, value in resolved_fields.items():
                    final_row[field] = value
                resolution = "auto_resolved"
                disagreements = []  # Clear for audit trail
            else:
                # Some disagreements remain — classify by worst type
                has_numeric = any(d["type"] == "numeric" for d in remaining)
                has_categorical = any(d["type"] == "categorical" for d in remaining)
                has_metadata = any(d["type"] == "metadata" for d in remaining)

                if has_numeric:
                    status = "NUMERIC_DISAGREE"
                    counts["numeric_disagreements"] += 1
                elif has_categorical:
                    status = "CATEGORICAL_DISAGREE"
                    counts["categorical_disagreements"] += 1
                else:
                    status = "METADATA_DISAGREE"
                    counts["metadata_disagreements"] += 1

                # Build partial final_row with auto-resolved fields applied
                if resolved_fields:
                    final_row = None  # Still needs review for remaining
                else:
                    final_row = None
                resolution = "pending_review"
                disagreements = remaining  # Only keep unresolved for review

        result_rows.append({
            "row_index": idx,
            "source_address": row_a.get("_source_address", {}),
            "status": status,
            "final_row": final_row,
            "agent_a_row": {k: row_a[k] for k in CSV_FIELDNAMES},
            "agent_b_row": {k: row_b[k] for k in CSV_FIELDNAMES},
            "disagreements": disagreements,
            "resolution": resolution,
            "resolved_by": "reconciler" if resolution in ("auto_accepted", "auto_resolved") else None,
            "resolved_at": now_iso() if resolution in ("auto_accepted", "auto_resolved") else None,
        })

    # Process unmatched A rows (selection disagreements)
    for row_a in unmatched_a:
        result_rows.append({
            "row_index": len(result_rows),
            "source_address": row_a.get("_source_address", {}),
            "status": "SELECTION_DISAGREE",
            "final_row": None,
            "agent_a_row": {k: row_a[k] for k in CSV_FIELDNAMES},
            "agent_b_row": None,
            "disagreements": [{
                "field": "_selection",
                "agent_a_value": "included",
                "agent_b_value": "excluded",
                "type": "selection",
            }],
            "resolution": "pending_review",
            "resolved_by": None,
            "resolved_at": None,
        })

    # Process unmatched B rows
    for row_b in unmatched_b:
        result_rows.append({
            "row_index": len(result_rows),
            "source_address": row_b.get("_source_address", {}),
            "status": "SELECTION_DISAGREE",
            "final_row": None,
            "agent_a_row": None,
            "agent_b_row": {k: row_b[k] for k in CSV_FIELDNAMES},
            "disagreements": [{
                "field": "_selection",
                "agent_a_value": "excluded",
                "agent_b_value": "included",
                "type": "selection",
            }],
            "resolution": "pending_review",
            "resolved_by": None,
            "resolved_at": None,
        })

    # Determine overall status
    total_disagree = (
        counts["numeric_disagreements"]
        + counts["categorical_disagreements"]
        + counts["metadata_disagreements"]
        + counts["selection_disagreements"]
    )
    if total_disagree == 0:
        overall_status = "FULLY_AGREED"
    else:
        overall_status = "PENDING_REVIEW"

    return {
        "nct_id": nct_id,
        "extraction_timestamp": now_iso(),
        "agent_a_version": VERSION,
        "agent_b_version": VERSION,
        "summary": {**counts, "status": overall_status},
        "rows": result_rows,
    }


# ---------------------------------------------------------------------------
# Generate review CSV for human adjudication
# ---------------------------------------------------------------------------

def generate_review_csv(reconciliation, output_path):
    """Write a review CSV with all disagreements for human adjudication.

    Columns: row_index, field, agent_a_value, agent_b_value, chosen_value, notes,
             serotype, group_id, context_value
    """
    review_rows = []
    for rr in reconciliation["rows"]:
        if rr["resolution"] != "pending_review":
            continue
        for d in rr["disagreements"]:
            # Add context fields so reviewer can identify the row
            addr = rr.get("source_address", {})
            agent_a = rr.get("agent_a_row") or {}
            agent_b = rr.get("agent_b_row") or {}
            context_row = agent_a or agent_b

            review_rows.append({
                "row_index": rr["row_index"],
                "field": d["field"],
                "agent_a_value": d["agent_a_value"],
                "agent_b_value": d["agent_b_value"],
                "chosen_value": "",
                "notes": "",
                "serotype": context_row.get("outcome_overview_serotype", ""),
                "group_id": addr.get("group_id", ""),
                "context_value": context_row.get("outcome_overview_value", ""),
                "outcome_title": addr.get("outcome_title", ""),
            })

    if not review_rows:
        return 0

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    fieldnames = [
        "row_index", "field", "agent_a_value", "agent_b_value",
        "chosen_value", "notes", "serotype", "group_id",
        "context_value", "outcome_title",
    ]
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(review_rows)

    return len(review_rows)


# ---------------------------------------------------------------------------
# Save/load reconciliation results
# ---------------------------------------------------------------------------

def save_reconciliation(reconciliation, nct_id):
    """Save reconciliation results to JSON."""
    trial_dir = os.path.join(EXTRACTIONS_DIR, nct_id)
    os.makedirs(trial_dir, exist_ok=True)
    path = os.path.join(trial_dir, "reconciliation.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(reconciliation, f, indent=2)
    return path


def load_reconciliation(nct_id):
    """Load reconciliation results from JSON."""
    path = os.path.join(EXTRACTIONS_DIR, nct_id, "reconciliation.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Extract final rows (agreed + resolved)
# ---------------------------------------------------------------------------

def get_final_rows(reconciliation):
    """Extract all finalized rows from a reconciliation result.

    Returns (final_rows, pending_count) where final_rows is a list of
    CSV-ready dicts and pending_count is the number of unresolved rows.
    """
    final = []
    pending = 0
    for rr in reconciliation["rows"]:
        if rr["final_row"] is not None:
            final.append(rr["final_row"])
        else:
            pending += 1
    return final, pending


def print_summary(reconciliation):
    """Print a human-readable reconciliation summary."""
    s = reconciliation["summary"]
    nct = reconciliation["nct_id"]

    print(f"\n  RECONCILIATION REPORT for {nct}:")
    print(f"    Agent A rows: {s['total_rows_a']}")
    print(f"    Agent B rows: {s['total_rows_b']}")
    print(f"    Matched pairs: {s['matched_pairs']}")
    print(f"    Agreements (auto-accepted): {s['agreements']}")

    if s["numeric_disagreements"]:
        print(f"    NUMERIC DISAGREEMENTS: {s['numeric_disagreements']} (BUG - investigate)")
    if s["categorical_disagreements"]:
        print(f"    Categorical disagreements: {s['categorical_disagreements']} (needs human review)")
    if s["metadata_disagreements"]:
        print(f"    Metadata disagreements: {s['metadata_disagreements']}")
    if s["selection_disagreements"]:
        print(f"    Selection disagreements: {s['selection_disagreements']} (one agent included, other excluded)")

    print(f"    Overall status: {s['status']}")
