"""
Apply decisions from data/review_all.csv to all pending reconciliations.

For each row in review_all.csv where chosen_value is filled in:
- 'A' → use Agent A's value
- 'B' → use Agent B's value
- anything else → use as a custom value

After applying, updates reconciliation.json files and reports
remaining unresolved items.

Usage:
    python scripts/apply_review.py
    python scripts/apply_review.py --dry-run
    python scripts/apply_review.py --export data/wisspar_export_2026_02_05.csv
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from extractor_base import CSV_FIELDNAMES, EXTRACTIONS_DIR, DEFAULT_CSV

PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
REVIEW_PATH = os.path.join(PROJECT_ROOT, "data", "review_all.csv")


def load_decisions():
    """Load decisions from review_all.csv. Returns dict keyed by (nct_id, field, val_a, val_b, group)."""
    decisions = {}
    if not os.path.exists(REVIEW_PATH):
        print(f"ERROR: {REVIEW_PATH} not found. Run build_review.py first.")
        return decisions

    with open(REVIEW_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            chosen = row.get("chosen_value", "").strip()
            if not chosen:
                continue
            key = (
                row["nct_id"],
                row["field"],
                row["agent_a_value"],
                row["agent_b_value"],
                row.get("group", ""),
            )
            decisions[key] = chosen
    return decisions


def resolve_value(chosen, agent_a_val, agent_b_val):
    """Resolve a chosen_value to an actual value."""
    if chosen.upper() == "A":
        return agent_a_val
    elif chosen.upper() == "B":
        return agent_b_val
    else:
        return chosen


def apply_to_trial(nct_id, decisions, dry_run=False):
    """Apply decisions to a single trial's reconciliation.

    Returns (resolved_count, remaining_count, final_rows).
    """
    recon_path = os.path.join(EXTRACTIONS_DIR, nct_id, "reconciliation.json")
    if not os.path.exists(recon_path):
        return 0, 0, []

    with open(recon_path, "r", encoding="utf-8") as f:
        recon = json.load(f)

    resolved_count = 0
    remaining_count = 0
    now = datetime.now(timezone.utc).isoformat()

    for rr in recon["rows"]:
        if rr["resolution"] != "pending_review":
            continue

        agent_a = rr.get("agent_a_row") or {}
        agent_b = rr.get("agent_b_row") or {}
        context_row = agent_a or agent_b
        group_title = context_row.get("outcome_overview_title", "")

        # Try to resolve each disagreement
        all_resolved = True
        resolved_fields = {}

        for d in rr["disagreements"]:
            field = d["field"]
            val_a = d["agent_a_value"]
            val_b = d["agent_b_value"]

            key = (nct_id, field, val_a, val_b, group_title)
            if key in decisions:
                resolved_fields[field] = resolve_value(
                    decisions[key], val_a, val_b
                )
            else:
                all_resolved = False

        if all_resolved and resolved_fields:
            # Build the final row from agent_a with overrides
            base_row = dict(agent_a) if agent_a else dict(agent_b)
            final_row = {k: base_row.get(k, "") for k in CSV_FIELDNAMES}
            for field, value in resolved_fields.items():
                if field in final_row:
                    final_row[field] = value

            rr["final_row"] = final_row
            rr["resolution"] = "human_adjudicated"
            rr["resolved_by"] = "review_all.csv"
            rr["resolved_at"] = now
            resolved_count += 1
        elif resolved_fields:
            # Partially resolved — still mark remaining
            remaining_count += 1
        else:
            remaining_count += 1

    # Update summary
    total_resolved = sum(
        1 for rr in recon["rows"]
        if rr["resolution"] in ("auto_accepted", "human_adjudicated")
    )
    total_pending = sum(
        1 for rr in recon["rows"] if rr["resolution"] == "pending_review"
    )
    if total_pending == 0:
        recon["summary"]["status"] = "FULLY_AGREED"
    else:
        recon["summary"]["status"] = "PENDING_REVIEW"

    # Save updated reconciliation
    if not dry_run and resolved_count > 0:
        with open(recon_path, "w", encoding="utf-8") as f:
            json.dump(recon, f, indent=2)

    # Collect all final rows
    final_rows = []
    for rr in recon["rows"]:
        if rr["final_row"] is not None:
            final_rows.append(rr["final_row"])

    return resolved_count, remaining_count, final_rows


def export_to_csv(all_rows, csv_path):
    """Write all resolved rows to CSV (replacing any existing data for these trials)."""
    # Load existing data
    existing_rows = []
    existing_ncts = set()
    if os.path.exists(csv_path):
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_rows = list(reader)

    # Get NCT IDs we're replacing
    new_ncts = set(r.get("clinical_trial_study_id", "") for r in all_rows)

    # Keep existing rows for trials NOT being replaced
    kept = [r for r in existing_rows
            if r.get("clinical_trial_study_id", "") not in new_ncts]

    # Merge
    final = kept + all_rows

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(final)

    print(f"\nExported to {csv_path}:")
    print(f"  Kept {len(kept)} existing rows")
    print(f"  Added {len(all_rows)} new/updated rows")
    print(f"  Total: {len(final)} rows")


def main():
    parser = argparse.ArgumentParser(
        description="Apply review decisions from data/review_all.csv"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be resolved without writing",
    )
    parser.add_argument(
        "--export",
        help="Export all resolved rows to this CSV path",
    )
    args = parser.parse_args()

    # Load decisions
    decisions = load_decisions()
    if not decisions:
        print("No decisions found in review_all.csv. "
              "Fill in the 'chosen_value' column first.")
        return

    print(f"Loaded {len(decisions)} decisions from review_all.csv")

    # Apply to each trial
    nct_ids = sorted(set(k[0] for k in decisions))
    # Also process any trial that has pending reviews
    for d in os.listdir(EXTRACTIONS_DIR):
        recon_path = os.path.join(EXTRACTIONS_DIR, d, "reconciliation.json")
        if os.path.exists(recon_path):
            with open(recon_path, "r", encoding="utf-8") as f:
                recon = json.load(f)
            if recon["summary"]["status"] == "PENDING_REVIEW" and d not in nct_ids:
                nct_ids.append(d)
    nct_ids = sorted(set(nct_ids))

    total_resolved = 0
    total_remaining = 0
    all_export_rows = []

    for nct_id in nct_ids:
        resolved, remaining, final_rows = apply_to_trial(
            nct_id, decisions, dry_run=args.dry_run
        )
        if resolved > 0 or remaining > 0:
            status = "DONE" if remaining == 0 else f"{remaining} remaining"
            print(f"  {nct_id}: resolved {resolved} rows ({status})")
        total_resolved += resolved
        total_remaining += remaining
        all_export_rows.extend(final_rows)

    print(f"\nTotal: {total_resolved} resolved, {total_remaining} still pending")
    if args.dry_run:
        print("(dry run — no files were modified)")

    # Export if requested
    if args.export and all_export_rows and not args.dry_run:
        export_to_csv(all_export_rows, args.export)


if __name__ == "__main__":
    main()
