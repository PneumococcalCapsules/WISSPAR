"""
LLM-based dual extraction pipeline for pneumococcal vaccine trial data.

Uses Claude to interpret trial context for classification fields (assay,
dose_number, schedule, vaccine, manufacturer, timeframe), while extracting
measurement values deterministically from the ClinicalTrials.gov API JSON.

Two agents with different analytical prompts provide independent verification,
reconciled using the same pipeline as the regex-based approach.

Usage:
    python scripts/llm_extract.py NCT06151288
    python scripts/llm_extract.py --all
    python scripts/llm_extract.py --all --compare data/wisspar_export_2026_02_05.csv
    python scripts/llm_extract.py --model claude-haiku-4-5-20251001 NCT06151288

Requires ANTHROPIC_API_KEY environment variable.
Approximate cost: ~$0.03-0.06 per trial (Sonnet), ~$2-4 for all ~70 trials.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time

# Fix Windows console encoding for Unicode characters
if sys.stdout and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr and hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Load .env file if present (for ANTHROPIC_API_KEY)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
except ImportError:
    pass

# Add scripts dir to path for imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    import anthropic
except ImportError:
    print("ERROR: 'anthropic' package not installed. Run: pip install anthropic")
    sys.exit(1)

from extractor_base import (
    CSV_FIELDNAMES, DEFAULT_CSV, EXTRACTIONS_DIR,
    load_vaccine_lookup, load_country_lookup,
    fetch_and_cache, save_extraction, now_iso,
    extract_metadata, clean_serotype, detect_class_timepoint,
    IMMUNO_KEYWORDS,
)
from reconcile import (
    reconcile, save_reconciliation, generate_review_csv,
    get_final_rows, print_summary,
)

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"

# ── Agent System Prompts ────────────────────────────────────────────

AGENT_A_SYSTEM = """\
You are a clinical immunogenicity researcher analyzing pneumococcal vaccine \
trial data from ClinicalTrials.gov. You will classify immunogenicity \
measurements by reasoning about the overall trial design.

Your analytical approach:
1. First, understand the overall trial design: What vaccines are compared? \
What dosing schedules? What age groups?
2. Look at ALL outcome measures together to understand the trial's \
measurement plan (e.g., "After Dose 3" and "After Dose 4" outcomes reveal \
the dosing schedule).
3. Use timing gaps between measurements (e.g., Dose 3 at ~7 months, Dose 4 \
at ~13 months) to identify booster doses vs primary series doses.
4. For vaccine identification, understand which arm/group corresponds to \
which vaccine product based on the full trial description.
5. Think holistically about the trial before classifying individual \
measurements."""

AGENT_B_SYSTEM = """\
You are a systematic data extraction specialist processing structured \
clinical trial API data. You classify immunogenicity measurements using \
a strict evidence hierarchy.

Your analytical approach:
1. For assay classification, prioritize: (a) explicit keywords in the \
outcome title (OPA, IgG, GMT, GMC, opsonophagocytic, immunoglobulin), \
(b) unitOfMeasure metadata (titer → OPA, mcg/mL → GMC), (c) paramType.
2. For vaccine identification, trace: group title → arm label → \
intervention name, using the armsInterventionsModule mappings.
3. For timeframe, parse the timeFrame text: extract the number and unit, \
convert to weeks (4.33 weeks/month, 52 weeks/year, 7 days/week).
4. For dose/schedule, analyze arm descriptions for explicit dose counts \
and booster indicators, then use outcome titles to identify each \
measurement's position in the schedule.
5. Process each field independently using available metadata. Prefer \
structured fields over narrative text interpretation."""

SHARED_TASK = """\
Analyze each outcome measure and its groups. Return ONLY a JSON object \
(no markdown fences, no commentary) with this structure:
{
  "outcomes": [
    {
      "outcome_index": 0,
      "assay": "OPA" or "GMC",
      "time_frame_weeks": <integer>,
      "groups": {
        "<group_id>": {
          "vaccine": "<standardized vaccine name>",
          "manufacturer": "<company short name>",
          "dose_number": <integer>,
          "schedule": "<schedule string>",
          "dose_description": "<description string>"
        }
      }
    }
  ]
}

Classification rules:

ASSAY:
- "OPA" for opsonophagocytic activity / geometric mean titer (GMT) measures.
- "GMC" for IgG geometric mean concentration measures.
- Key indicators: unit "1/dil" or "titer" → OPA. Unit "mcg/mL" or "ug/mL" → GMC.
- "Unknown" only if truly unclear.

TIME_FRAME_WEEKS:
- Integer weeks from the most recent dose to the measurement. Use \
4.33 weeks/month, 52 weeks/year. Round to nearest integer.
- "1 month after vaccination" = 4. "6 months after dose 3" = 26.
- This is the TIME FROM THE MOST RECENT DOSE to the measurement, NOT from \
study start. E.g., "1 month after dose 4" = 4 weeks (time since dose 4).
- IMPORTANT: Every outcome has a timeframe. You MUST return an integer \
for EVERY outcome — never return null. The timeFrame field in every \
outcome measure contains enough information to derive this value. \
Common patterns: "1 month after" = 4, "one month after" = 4, \
"approximately 1 month" = 4, "28 days" = 4, "4 weeks" = 4, \
"6 months" = 26, "1 year" = 52. For pre-vaccination/baseline \
measurements, use 0.
- If the timeframe text spans multiple timepoints (e.g., \
"before and after the toddler dose"), determine which specific \
timepoint THIS outcome measures based on the serotype classes \
(PRE/POST labels) or outcome context, and return the weeks for \
that specific timepoint.

DOSE_NUMBER:
- The total number of vaccine doses the participant has received AT THE TIME \
of this measurement.
- "after dose 3" = 3. "after booster" in a 3+1 trial = 4. \
"pre-booster" in a 3+1 trial = 3. "after dose 1" = 1.

SCHEDULE:
- Use "{primary}+1 {age}" when the trial includes a booster/toddler dose \
(e.g., "3+1 child", "2+1 child"). A trial has a booster if ANY outcome \
mentions "booster", "toddler dose", or if there is a large timing gap \
between the last dose and prior doses (e.g., doses at 2,4,6 months then \
12-15 months).
- Use "{n} dose {age}" ONLY when there is NO booster in the trial \
(e.g., "1 dose adult", "2 dose adult", "3 dose child").
- IMPORTANT: The schedule describes the ENTIRE regimen for that arm, not \
just this measurement. A "3+1 child" arm stays "3+1 child" for ALL its \
outcomes (post-dose-3, pre-booster, post-booster).
- Use "child" for CHILD populations, "adult" for ADULT/OLDER_ADULT.

DOSE_DESCRIPTION:
- Format: "{time}m post dose {n} {age}" when time_frame_weeks ≈ 4 \
(use "1m"), otherwise "{weeks}w post dose {n} {age}".
- For post-booster: "1m post boost {age}" (if ~4 weeks) or \
"{weeks}w post boost {age}".
- For pre-booster: "pre boost {age}".
- ALWAYS use "1m" (not "4w") when the timeframe is approximately 1 month \
(4-5 weeks). Use "{weeks}w" for all other durations.
- Examples: "1m post dose 3 child", "26w post dose 1 adult", \
"1m post boost child", "pre boost child".

VACCINE:
- Use EXACTLY the standardized name from the reference list when it matches.
- Do NOT append the manufacturer in parentheses if the reference name \
already exists without it (e.g., use "PCV13" not "PCV13 (Pfizer)" when \
"PCV13" is in the reference list).
- For sequential regimens (e.g., PCV13 then PPV23): each group receives \
ONE vaccine at a time. Assign the vaccine that group received, not a combo.
- For new/unknown vaccines: use "PCV{valency} ({brand/company})".

MANUFACTURER:
- You MUST use ONLY these exact short names — no other forms are accepted:
  Pfizer, GSK, MSD, Vaxcyte, Inventprise, Serum Institute of India, Walvax.
- Mapping (use the SHORT NAME on the left, never the long form on the right):
  GSK = GlaxoSmithKline, GlaxoSmithKline Biologicals, GlaxoSmithKline LLC
  MSD = Merck, Merck & Co, Merck Sharp & Dohme, Merck Sharp & Dohme LLC, \
Merck Sharp & Dohme Corp
  Pfizer = Pfizer Inc, Wyeth, Wyeth-Lederle, Wyeth Pharmaceuticals
  Serum Institute of India = Serum Institute of India Pvt. Ltd.
- If the sponsor or intervention text contains ANY of the long forms above, \
you MUST convert to the short name. This is a strict requirement."""


# ── Helpers ─────────────────────────────────────────────────────────

def get_vaccine_reference(vaccine_lookup):
    """Build deduplicated vaccine reference list from lookup table."""
    seen = {}
    for row in vaccine_lookup:
        name = row.get("vaccine_name", "")
        mfr = row.get("manufacturer", "")
        if name and name not in seen:
            seen[name] = mfr
    return [f"{n} (manufacturer: {m})" for n, m in sorted(seen.items())]


def is_immuno_outcome(outcome):
    """Check if an outcome measure is pneumococcal immunogenicity-related.

    Excludes non-pneumococcal antibodies (polio, diphtheria, tetanus, etc.),
    fold-rise/GMFR outcomes, and percentage/threshold outcomes to match
    the curated dataset scope.
    """
    title = outcome.get("title", "").upper()
    param_type = outcome.get("paramType", "").upper()

    # Step 1: Must be immunogenicity-related
    is_immuno = (
        any(kw in title for kw in IMMUNO_KEYWORDS)
        or "GEOMETRIC" in param_type
    )
    if not is_immuno:
        return False

    # Step 2: Exclude non-pneumococcal antibody outcomes
    NON_PNEUMO_KEYWORDS = [
        "DIPHTHERIA", "TETANUS", "PERTUSSIS", "PERTACTIN",
        "HEPATITIS", "POLIO", "POLIOVIRUS",
        "ANTI-PRP", "POLYRIBOSYL", "ANTI-D ", "ANTI-T ",
        "ANTI-PT", "ANTI-FHA", "ANTI-PRN", "ANTI-FIM",
        "ANTI-HBS", "PROTEIN D", "ANTI-PD",
        "HAEMOPHILUS",
    ]
    for kw in NON_PNEUMO_KEYWORDS:
        if kw in title:
            return False

    # Step 3: Exclude fold-rise / GMFR outcomes
    FOLD_RISE_KEYWORDS = [
        "FOLD RISE", "FOLD-RISE", "GMFR",
        "GEOMETRIC MEAN FOLD",
    ]
    for kw in FOLD_RISE_KEYWORDS:
        if kw in title:
            return False

    # Step 4: Exclude percentage/proportion/threshold outcomes
    PERCENTAGE_KEYWORDS = [
        "PERCENTAGE OF PARTICIPANTS",
        "PROPORTION OF PARTICIPANTS",
        "PERCENTAGE OF SUBJECTS",
        "PROPORTION OF SUBJECTS",
        "PERCENTAGE OF INFANTS",
        "SEROCONVERSION",
        "SEROPROTECTION",
        "RESPONDER RATE",
    ]
    for kw in PERCENTAGE_KEYWORDS:
        if kw in title:
            return False

    return True


def get_all_trial_ids():
    """Get sorted list of NCT IDs that have cached raw.json."""
    trials = []
    if not os.path.isdir(EXTRACTIONS_DIR):
        return trials
    for d in os.listdir(EXTRACTIONS_DIR):
        if d.startswith("NCT"):
            if os.path.exists(os.path.join(EXTRACTIONS_DIR, d, "raw.json")):
                trials.append(d)
    return sorted(trials)


# ── Prompt Building ─────────────────────────────────────────────────

def build_prompt(data, nct_id, immuno_outcomes, vaccine_reference, meta):
    """Build the user message with trial context for Claude."""
    proto = data.get("protocolSection", {})
    arms_mod = proto.get("armsInterventionsModule", {})
    arm_groups = arms_mod.get("armGroups", [])
    interventions = arms_mod.get("interventions", [])

    # Arm → intervention name mapping
    arm_inv = {}
    for inv in interventions:
        for label in inv.get("armGroupLabels", []):
            arm_inv.setdefault(label, []).append(inv.get("name", ""))

    lines = []
    lines.append(f"# Trial: {nct_id} - {meta['study_name']}")
    lines.append(f"Sponsor: {meta['sponsor']}")
    lines.append(f"Phase: {meta['phase']}")
    pop = ", ".join(meta.get("std_ages", []))
    lines.append(f"Population: {pop}")

    # Arms
    lines.append("\n## Arms and Interventions")
    for arm in arm_groups:
        label = arm.get("label", "")
        desc = arm.get("description", "")
        atype = arm.get("type", "")
        inv_names = arm_inv.get(label, [])
        lines.append(f"\nARM: {label} ({atype})")
        if desc:
            lines.append(f"  Description: {desc[:500]}")
        if inv_names:
            lines.append(f"  Interventions: {', '.join(inv_names)}")

    # Outcome measures
    lines.append("\n## Immunogenicity Outcome Measures")
    for idx, om in enumerate(immuno_outcomes):
        title = om.get("title", "")
        desc = om.get("description", "")
        tf = om.get("timeFrame", "")
        pt = om.get("paramType", "")
        unit = om.get("unitOfMeasure", "")
        disp = om.get("dispersionType", "")

        lines.append(f"\n### Outcome {idx}: \"{title}\"")
        if desc:
            lines.append(f"Description: {desc[:300]}")
        if tf:
            lines.append(f"Timeframe: {tf}")
        lines.append(f"ParamType: {pt} | Unit: {unit} | Dispersion: {disp}")

        # Groups
        groups = om.get("groups", [])
        lines.append("Groups:")
        for g in groups:
            gid = g.get("id", "")
            gtitle = g.get("title", "")
            gdesc = g.get("description", "")
            preview = gdesc[:250] + "..." if len(gdesc) > 250 else gdesc
            lines.append(f"  {gid}: \"{gtitle}\" - {preview}")

        # Class sample (serotypes)
        classes = om.get("classes", [])
        sample = [c.get("title", "") for c in classes[:5]]
        extra = len(classes) - 5 if len(classes) > 5 else 0
        ct = ", ".join(f'"{s}"' for s in sample)
        if extra > 0:
            ct += f", ... +{extra} more"
        lines.append(f"  Serotypes ({len(classes)}): {ct}")

    # Vaccine reference
    lines.append("\n## Vaccine Reference (use these standardized names)")
    for v in vaccine_reference:
        lines.append(f"  - {v}")

    # Task
    lines.append(f"\n## Task\n{SHARED_TASK}")

    return "\n".join(lines)


# ── Claude API ──────────────────────────────────────────────────────

def call_claude(client, system_prompt, user_message, model):
    """Call Claude API and parse JSON response. Returns dict or None."""
    try:
        response = client.messages.create(
            model=model,
            max_tokens=16384,
            temperature=0,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": "{"},
            ],
        )
    except anthropic.APIError as e:
        print(f"    API error: {e}")
        return None

    text = "{" + response.content[0].text

    # Check for truncation
    if response.stop_reason == "max_tokens":
        print("    WARNING: response truncated (max_tokens)")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find a valid JSON object
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        print(f"    WARNING: failed to parse JSON response")
        return None


# ── Row Building ────────────────────────────────────────────────────

def build_rows(meta, immuno_outcomes, llm_output, data, agent_name):
    """Merge deterministic extraction with LLM interpretation into CSV rows."""
    if not llm_output or "outcomes" not in llm_output:
        return []

    rows = []
    for om_result in llm_output["outcomes"]:
        idx = om_result.get("outcome_index")
        if idx is None or idx < 0 or idx >= len(immuno_outcomes):
            continue

        om = immuno_outcomes[idx]
        assay = om_result.get("assay", "Unknown")
        tw_raw = om_result.get("time_frame_weeks")
        time_weeks = str(tw_raw) if tw_raw is not None else ""
        description = om.get("description", "")
        timeframe = om.get("timeFrame", "")

        # Denominator counts
        denom_counts = {}
        for denom in om.get("denoms", []):
            for dc in denom.get("counts", []):
                denom_counts[dc.get("groupId", "")] = dc.get("value", "")

        groups = {g["id"]: g for g in om.get("groups", [])}
        llm_groups = om_result.get("groups", {})
        classes = om.get("classes", [])

        for cls_idx, cls in enumerate(classes):
            raw_title = cls.get("title", "")
            serotype = clean_serotype(raw_title)
            if not serotype:
                continue

            cls_tp = detect_class_timepoint(raw_title)
            categories = cls.get("categories", [])
            if not categories:
                continue

            for cat_idx, cat in enumerate(categories):
                measurements = cat.get("measurements", [])
                for m_idx, meas in enumerate(measurements):
                    gid = meas.get("groupId", "")
                    if gid not in groups:
                        continue

                    group = groups[gid]
                    lg = llm_groups.get(gid, {})

                    vaccine = lg.get("vaccine", group.get("title", ""))
                    manufacturer = lg.get("manufacturer", meta["sponsor"])
                    dose_number = str(lg.get("dose_number", 1))
                    schedule = lg.get("schedule", "")
                    dose_desc = lg.get("dose_description", "")

                    # Adjust for PRE/POST class timepoints
                    if cls_tp and schedule:
                        sched_m = re.match(r'(\d+)\+1', schedule)
                        if sched_m:
                            primary = int(sched_m.group(1))
                            age = "child" if "child" in schedule else "adult"
                            if cls_tp == "PRE":
                                dose_number = str(primary)
                                dose_desc = f"pre boost {age}"
                            elif cls_tp == "POST":
                                dose_number = str(primary + 1)

                    source_address = {
                        "outcome_index": idx,
                        "class_index": cls_idx,
                        "category_index": cat_idx,
                        "measurement_index": m_idx,
                        "group_id": gid,
                        "outcome_title": om.get("title", ""),
                    }

                    row = {
                        "clinical_trial_study_name": meta["study_name"],
                        "clinical_trial_study_id": meta["nct_id"],
                        "clinical_trial_sponsor": meta["sponsor"],
                        "clinical_trial_responsible_party": meta["resp_party"],
                        "clinical_trial_phase": meta["phase"],
                        "location_country_code": meta["country_codes"],
                        "location_continent": meta["continents"],
                        "study_eligibility_standard_age_list": meta["age_list_str"],
                        "study_eligibility_ethnicity": "",
                        "outcome_overview_title": group.get("title", ""),
                        "outcome_overview_id": gid,
                        "outcome_overview_description": description,
                        "outcome_overview_time_frame": timeframe,
                        "outcome_overview_assay": assay,
                        "outcome_overview_dose_number": dose_number,
                        "outcome_overview_participants": denom_counts.get(gid, ""),
                        "outcome_overview_serotype": serotype,
                        "outcome_overview_value": meas.get("value", ""),
                        "outcome_overview_upper_limit": meas.get("upperLimit", ""),
                        "outcome_overview_lower_limit": meas.get("lowerLimit", ""),
                        "outcome_overview_ratio": "",
                        "outcome_overview_vaccine": vaccine,
                        "outcome_overview_immunocompromised_population": "",
                        "outcome_overview_manufacturer": manufacturer,
                        "outcome_overview_dose_description": dose_desc,
                        "outcome_overview_schedule": schedule,
                        "outcome_overview_time_frame_weeks": time_weeks,
                        "outcome_overview_confidence_interval": "",
                        "outcome_overview_percent_responders": "0",
                        "_source_address": source_address,
                        "_agent": agent_name,
                    }
                    rows.append(row)

    return rows


# ── Per-trial Extraction ────────────────────────────────────────────

def extract_trial_llm(data, nct_id, client, system_prompt, agent_name,
                       vaccine_reference, country_lookup, model):
    """Run LLM extraction for one agent on one trial."""
    proto = data.get("protocolSection", {})
    results_sec = data.get("resultsSection")
    if not results_sec:
        return []

    meta = extract_metadata(proto, country_lookup)
    all_outcomes = (
        results_sec.get("outcomeMeasuresModule", {})
        .get("outcomeMeasures", [])
    )
    immuno_outcomes = [om for om in all_outcomes if is_immuno_outcome(om)]
    if not immuno_outcomes:
        return []

    prompt = build_prompt(data, nct_id, immuno_outcomes, vaccine_reference, meta)

    # Save prompt for debugging
    trial_dir = os.path.join(EXTRACTIONS_DIR, nct_id)
    os.makedirs(trial_dir, exist_ok=True)
    prompt_path = os.path.join(trial_dir, f"llm_prompt_{agent_name}.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(f"SYSTEM:\n{system_prompt}\n\nUSER:\n{prompt}")

    llm_output = call_claude(client, system_prompt, prompt, model)

    # Save raw LLM response for debugging
    if llm_output:
        resp_path = os.path.join(trial_dir, f"llm_response_{agent_name}.json")
        with open(resp_path, "w", encoding="utf-8") as f:
            json.dump(llm_output, f, indent=2)

    if not llm_output:
        # Retry once
        print(f"    Retrying agent {agent_name}...")
        time.sleep(1)
        llm_output = call_claude(client, system_prompt, prompt, model)
        if llm_output:
            resp_path = os.path.join(trial_dir, f"llm_response_{agent_name}.json")
            with open(resp_path, "w", encoding="utf-8") as f:
                json.dump(llm_output, f, indent=2)

    if not llm_output:
        return []

    return build_rows(meta, immuno_outcomes, llm_output, data, agent_name)


def process_trial(nct_id, client, vaccine_lookup, country_lookup,
                   vaccine_reference, model, dry_run=False):
    """Full pipeline for one trial: both agents + reconciliation."""
    trial_dir = os.path.join(EXTRACTIONS_DIR, nct_id)
    raw_path = os.path.join(trial_dir, "raw.json")

    skip = {"nct_id": nct_id, "rows_a": 0, "rows_b": 0,
            "agreed": 0, "pending": 0, "final_rows": []}

    if not os.path.exists(raw_path):
        # Try to fetch
        print(f"  Fetching {nct_id} from API...")
        data = fetch_and_cache(nct_id)
        if not data:
            return {**skip, "status": "SKIP_FETCH_FAIL"}
    else:
        with open(raw_path, "r", encoding="utf-8") as f:
            data = json.load(f)

    if not data.get("resultsSection"):
        return {**skip, "status": "SKIP_NO_RESULTS"}

    # Agent A
    print(f"  Agent A ({model.split('-')[1]})...", end="", flush=True)
    rows_a = extract_trial_llm(
        data, nct_id, client, AGENT_A_SYSTEM, "a",
        vaccine_reference, country_lookup, model)
    print(f" {len(rows_a)} rows")

    time.sleep(0.5)

    # Agent B
    print(f"  Agent B ({model.split('-')[1]})...", end="", flush=True)
    rows_b = extract_trial_llm(
        data, nct_id, client, AGENT_B_SYSTEM, "b",
        vaccine_reference, country_lookup, model)
    print(f" {len(rows_b)} rows")

    if not dry_run:
        save_extraction(rows_a, nct_id, "a")
        save_extraction(rows_b, nct_id, "b")

    # Reconcile
    result = reconcile(rows_a, rows_b, nct_id)
    if not dry_run:
        save_reconciliation(result, nct_id)

    final_rows, pending = get_final_rows(result)

    if pending > 0 and not dry_run:
        review_path = os.path.join(EXTRACTIONS_DIR, nct_id, "review.csv")
        n_review = generate_review_csv(result, review_path)
        print(f"  {n_review} disagreements -> {review_path}")

    status = "FULLY_AGREED" if pending == 0 else "PENDING_REVIEW"
    return {
        "nct_id": nct_id,
        "status": status,
        "rows_a": len(rows_a),
        "rows_b": len(rows_b),
        "agreed": len(final_rows),
        "pending": pending,
        "final_rows": final_rows,
    }


# ── Comparison with Original CSV ────────────────────────────────────

COMPARE_FIELDS = [
    "outcome_overview_dose_number",
    "outcome_overview_schedule",
    "outcome_overview_time_frame_weeks",
    "outcome_overview_assay",
    "outcome_overview_vaccine",
]

def _row_key(row):
    return (
        row.get("clinical_trial_study_id", ""),
        row.get("outcome_overview_id", ""),
        row.get("outcome_overview_serotype", ""),
        row.get("outcome_overview_assay", ""),
        row.get("outcome_overview_value", ""),
    )

def _row_key_loose(row):
    return (
        row.get("clinical_trial_study_id", ""),
        row.get("outcome_overview_id", ""),
        row.get("outcome_overview_serotype", ""),
        row.get("outcome_overview_value", ""),
    )

def compare_with_csv(csv_path):
    """Compare agent_a.json extractions against original CSV."""
    original = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            nct = row["clinical_trial_study_id"]
            original.setdefault(nct, []).append(row)

    total_compared = 0
    total_diffs = 0
    trial_diffs = {}
    field_counts = {f: 0 for f in COMPARE_FIELDS}

    for nct_id in sorted(original.keys()):
        agent_path = os.path.join(EXTRACTIONS_DIR, nct_id, "agent_a.json")
        if not os.path.exists(agent_path):
            continue
        with open(agent_path, encoding="utf-8") as f:
            agent_data = json.load(f)
        new_rows = agent_data if isinstance(agent_data, list) else agent_data.get("rows", [])
        old_rows = original.get(nct_id, [])
        if not old_rows or not new_rows:
            continue

        old_by_key = {}
        old_by_loose = {}
        for r in old_rows:
            old_by_key[_row_key(r)] = r
            lk = _row_key_loose(r)
            if lk not in old_by_loose:
                old_by_loose[lk] = r

        diffs = []
        matched = 0
        for nr in new_rows:
            old_r = old_by_key.get(_row_key(nr))
            if not old_r:
                old_r = old_by_loose.get(_row_key_loose(nr))
            if not old_r:
                continue
            matched += 1
            for field in COMPARE_FIELDS:
                ov = old_r.get(field, "").strip()
                nv = nr.get(field, "").strip()
                if ov != nv:
                    diffs.append({
                        "field": field, "old": ov, "new": nv,
                        "group": nr.get("outcome_overview_title", ""),
                        "serotype": nr.get("outcome_overview_serotype", ""),
                    })
                    field_counts[field] += 1

        total_compared += matched
        total_diffs += len(diffs)
        if diffs:
            trial_diffs[nct_id] = diffs

    # Print results
    print(f"\n{'='*60}")
    print(f"COMPARISON: {total_compared} matched rows, {total_diffs} diffs")
    print(f"{'='*60}")
    print("\nBy field:")
    for f, c in sorted(field_counts.items(), key=lambda x: -x[1]):
        if c > 0:
            print(f"  {f.replace('outcome_overview_', '')}: {c}")

    print(f"\nTrials with diffs: {len(trial_diffs)}")
    for nct_id in sorted(trial_diffs.keys()):
        diffs = trial_diffs[nct_id]
        patterns = set()
        for d in diffs:
            fn = d["field"].replace("outcome_overview_", "")
            patterns.add(f"{fn}: {d['old']} -> {d['new']}")
        print(f"\n  {nct_id} ({len(diffs)} diffs, {len(original.get(nct_id, []))} orig rows):")
        for p in sorted(patterns):
            count = sum(1 for d in diffs
                        if f"{d['field'].replace('outcome_overview_', '')}: {d['old']} -> {d['new']}" == p)
            print(f"    {p} (x{count})")

    return total_compared, total_diffs


# ── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LLM-based dual extraction with Claude"
    )
    parser.add_argument("nct_ids", nargs="*", help="NCT IDs to extract")
    parser.add_argument("--all", action="store_true",
                        help="Process all trials with cached raw.json")
    parser.add_argument("--compare", metavar="CSV",
                        help="Compare results against original CSV")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Claude model (default: {DEFAULT_MODEL})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract but don't save files")
    parser.add_argument("--force", action="store_true",
                        help="Re-extract even if agent files exist")
    args = parser.parse_args()

    if not args.nct_ids and not args.all and not args.compare:
        parser.print_help()
        sys.exit(1)

    # Init API client
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and (args.nct_ids or args.all):
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        print("Get your key at: https://console.anthropic.com/settings/keys")
        sys.exit(1)

    client = anthropic.Anthropic() if api_key else None

    # Load lookups
    vaccine_lookup = load_vaccine_lookup()
    country_lookup = load_country_lookup()
    vaccine_reference = get_vaccine_reference(vaccine_lookup)

    # Build trial list
    nct_ids = []
    if args.all:
        nct_ids = get_all_trial_ids()
        print(f"Found {len(nct_ids)} trials with cached data")
    elif args.nct_ids:
        nct_ids = args.nct_ids

    # Process trials
    if nct_ids and client:
        results = []
        for i, nct_id in enumerate(nct_ids):
            print(f"\n[{i+1}/{len(nct_ids)}] {nct_id}")

            # Skip if already extracted (unless --force)
            if not args.force:
                a_path = os.path.join(EXTRACTIONS_DIR, nct_id, "agent_a.json")
                b_path = os.path.join(EXTRACTIONS_DIR, nct_id, "agent_b.json")
                if os.path.exists(a_path) and os.path.exists(b_path):
                    # Check if these are LLM extractions (have llm_response file)
                    llm_resp = os.path.join(EXTRACTIONS_DIR, nct_id, "llm_response_a.json")
                    if os.path.exists(llm_resp):
                        print("  Already extracted (use --force to redo)")
                        continue

            try:
                r = process_trial(nct_id, client, vaccine_lookup,
                                  country_lookup, vaccine_reference,
                                  args.model, args.dry_run)
            except Exception as e:
                print(f"  ERROR: {e}")
                r = {"nct_id": nct_id, "status": f"ERROR: {e}",
                     "rows_a": 0, "rows_b": 0, "agreed": 0,
                     "pending": 0, "final_rows": []}
            results.append(r)

            icon = {"FULLY_AGREED": "+", "PENDING_REVIEW": "?"}.get(
                r.get("status", ""), "!")
            print(f"  [{icon}] A={r['rows_a']} B={r['rows_b']} "
                  f"agreed={r['agreed']} pending={r['pending']}")

            # Brief pause between trials
            if i < len(nct_ids) - 1:
                time.sleep(0.5)

        # Summary
        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        statuses = {}
        for r in results:
            s = r.get("status", "UNKNOWN")
            statuses[s] = statuses.get(s, 0) + 1
        for s, c in sorted(statuses.items()):
            print(f"  {s}: {c}")
        total_agreed = sum(r.get("agreed", 0) for r in results)
        total_pending = sum(r.get("pending", 0) for r in results)
        print(f"  Total agreed rows: {total_agreed}")
        print(f"  Total pending: {total_pending}")

    # Comparison
    if args.compare:
        compare_with_csv(args.compare)


if __name__ == "__main__":
    main()
