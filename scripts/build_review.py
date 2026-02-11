"""
Build a consolidated review spreadsheet for all pending disagreements.

Groups identical disagreement patterns together so one decision resolves
many rows at once. Outputs data/review_all.csv.

Usage:
    python scripts/build_review.py
"""

import csv
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from extractor_base import EXTRACTIONS_DIR

PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "data", "review_all.csv")


def load_trial_context(nct_id):
    """Load protocol info for context."""
    raw_path = os.path.join(EXTRACTIONS_DIR, nct_id, "raw.json")
    if not os.path.exists(raw_path):
        return {}
    with open(raw_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    proto = data.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    return {
        "title": ident.get("briefTitle", ""),
        "sponsor": proto.get("sponsorCollaboratorsModule", {})
            .get("leadSponsor", {}).get("name", ""),
    }


def main():
    # Collect all disagreements
    all_patterns = {}  # (nct_id, field, agent_a_value, agent_b_value) -> count + context
    trial_contexts = {}

    nct_ids = sorted(os.listdir(EXTRACTIONS_DIR))
    for nct_id in nct_ids:
        recon_path = os.path.join(EXTRACTIONS_DIR, nct_id, "reconciliation.json")
        if not os.path.exists(recon_path):
            continue
        with open(recon_path, "r", encoding="utf-8") as f:
            recon = json.load(f)
        if recon["summary"]["status"] != "PENDING_REVIEW":
            continue

        if nct_id not in trial_contexts:
            trial_contexts[nct_id] = load_trial_context(nct_id)

        for rr in recon["rows"]:
            if rr["resolution"] != "pending_review":
                continue
            # Get context from the row
            agent_a = rr.get("agent_a_row") or {}
            agent_b = rr.get("agent_b_row") or {}
            context_row = agent_a or agent_b
            group_title = context_row.get("outcome_overview_title", "")

            for d in rr["disagreements"]:
                field = d["field"]
                val_a = d["agent_a_value"]
                val_b = d["agent_b_value"]
                key = (nct_id, field, val_a, val_b, group_title)

                if key not in all_patterns:
                    all_patterns[key] = {
                        "count": 0,
                        "sample_serotype": "",
                        "sample_value": "",
                        "outcome_title": rr.get("source_address", {}).get("outcome_title", ""),
                    }
                all_patterns[key]["count"] += 1
                if not all_patterns[key]["sample_serotype"]:
                    all_patterns[key]["sample_serotype"] = context_row.get(
                        "outcome_overview_serotype", ""
                    )
                if not all_patterns[key]["sample_value"]:
                    all_patterns[key]["sample_value"] = context_row.get(
                        "outcome_overview_value", ""
                    )

    # Sort: group by field, then by trial
    sorted_keys = sorted(all_patterns.keys(), key=lambda k: (k[1], k[0], k[4]))

    # Write consolidated review CSV
    fieldnames = [
        "nct_id",
        "trial_title",
        "field",
        "group",
        "agent_a_value",
        "agent_b_value",
        "chosen_value",
        "rows_affected",
        "sample_serotype",
        "outcome_title",
        "notes",
    ]

    rows_written = 0
    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for key in sorted_keys:
            nct_id, field, val_a, val_b, group_title = key
            info = all_patterns[key]
            ctx = trial_contexts.get(nct_id, {})

            writer.writerow({
                "nct_id": nct_id,
                "trial_title": ctx.get("title", ""),
                "field": field,
                "group": group_title,
                "agent_a_value": val_a,
                "agent_b_value": val_b,
                "chosen_value": "",
                "rows_affected": info["count"],
                "sample_serotype": info["sample_serotype"],
                "outcome_title": info["outcome_title"],
                "notes": "",
            })
            rows_written += 1

    total_rows_affected = sum(p["count"] for p in all_patterns.values())
    unique_fields = len(set(k[1] for k in all_patterns))
    unique_trials = len(set(k[0] for k in all_patterns))

    print(f"Consolidated review written to: {OUTPUT_PATH}")
    print(f"  {rows_written} unique disagreement patterns")
    print(f"  covering {total_rows_affected} individual rows")
    print(f"  across {unique_trials} trials and {unique_fields} fields")
    print()
    print("Instructions:")
    print("  1. Open data/review_all.csv in Excel/Sheets")
    print("  2. Fill in the 'chosen_value' column:")
    print("     - Type 'A' to accept Agent A's value")
    print("     - Type 'B' to accept Agent B's value")
    print("     - Or type a custom value")
    print("  3. Save and run: python scripts/apply_review.py")


if __name__ == "__main__":
    main()
