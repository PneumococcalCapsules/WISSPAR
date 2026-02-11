# Wisspar Project Context

## Purpose & Context

This project works with vaccine immunogenicity data, particularly focusing on pneumococcal vaccine clinical trials. The work involves extracting and analyzing outcome measures like geometric mean concentrations (GMC) and opsonophagocytic activity (OPA) measurements across multiple serotypes and treatment arms from clinical trial datasets. It maintains structured datasets that track immunogenicity outcomes across different vaccine formulations, dosage levels, and geographic regions.

## Current State

The project is actively building and expanding a comprehensive immunogenicity dataset (wisspar_export.csv) by extracting clinical trial data from ClinicalTrials.gov. The current dataset structure includes fields for treatment arms, serotypes, outcome measures, dosage information, geographic location, and timeframe data.

The current data extract is located at `data/wisspar_export_2026_02_05.csv`. The goal is to ingest and format additional trials from ClinicalTrials.gov into this dataset.

## Data Extraction Rules

- **ONLY** use data from the results tables and metadata tables from ClinicalTrials.gov. Do not use data from other sources without explicit user permission.
- **ALWAYS** extract data via the ClinicalTrials.gov API v2 — never scrape directly from the website.
- If additional data sources beyond ClinicalTrials.gov are needed, ask the user for permission first.
- If it is not clear which results tables or outcome measures should be extracted from a trial, ask the user for clarification before proceeding.
- When data is extracted from a manuscript or publication (rather than ClinicalTrials.gov results tables), **always log the source** in `data/manuscript_sources.csv` with the NCT ID, publication reference, tables extracted, and any relevant notes. Keep this file up to date whenever manuscript-sourced data is added.

## Manuscript-Sourced Data

Some trials do not have results posted on ClinicalTrials.gov. When the user provides a manuscript, data may be extracted directly from publication tables with explicit permission.

- **Registry**: `data/manuscript_sources.csv` tracks all trials with manuscript-extracted data.
- **Publication files**: Stored in `data/publications/`.
- Always record: NCT ID, publication citation, which tables were extracted, serotype count, treatment arms, and assay types.
- Manuscript-sourced data follows the same CSV formatting conventions as API-extracted data.

## Tools & Resources

This project uses the ClinicalTrials.gov API v2 for data extraction. API-based extraction is preferred over web scraping for reliability and consistency in data collection.

### ClinicalTrials.gov API v2

- **Base URL**: `https://clinicaltrials.gov/api/v2/`
- **API Reference**: https://clinicaltrials.gov/data-api/api
- **Authentication**: Public API, no authentication required.
- **Rate limits**: ~50 requests/minute per IP. Use the `fields` parameter to request only needed data.

**Key endpoints:**
- **Single study**: `GET https://clinicaltrials.gov/api/v2/studies/{NCT_ID}`
  - Use `?fields=resultsSection,protocolSection` to fetch outcome measures and trial metadata.
- **Search studies**: `GET https://clinicaltrials.gov/api/v2/studies`
  - Parameters: `query.cond` (condition), `query.intr` (intervention), `query.term` (general search), `pageSize`, `format` (json/csv), `fields`, `countTotal`.

**Response structure:** The API returns JSON with `protocolSection` (study info, eligibility, design) and `resultsSection` (outcomes and results when available).

## Key Learnings & Principles

- The ClinicalTrials.gov API requires specific field parameters to efficiently access immunogenicity results data.
- Outcome measures are structured as nested JSON with classes representing serotypes and categories containing measurements for each treatment group.
- Group IDs follow predictable patterns, and immunogenicity data can be identified by searching for "OPA" or "IgG" indicators in outcome titles, or by checking `paramType` for "GEOMETRIC".
- Maintaining consistent CSV formatting with predefined fieldnames ensures compatibility across dataset expansions.

## LLM-Based Dual-Extraction Workflow (Primary)

The primary extraction method uses `scripts/llm_extract.py`, which combines deterministic Python extraction (metadata, raw measurements, serotype cleaning) with Claude LLM reasoning for interpretive fields (assay, dose_number, schedule, vaccine, manufacturer, timeframe). Two independent LLM agents with different analytical prompts provide verification.

### Architecture

```
llm_extract.py (orchestrator)
  ├── Fetches JSON once → data/extractions/{nct_id}/raw.json
  ├── Deterministic extraction: metadata, serotypes, raw values (Python)
  ├── Agent A (Claude, holistic clinical reasoning) → interpretive fields
  ├── Agent B (Claude, systematic metadata-first) → interpretive fields
  └── Reconciler (reconcile.py) → compares, auto-accepts agreements, flags disagreements
        ├── Agreements → auto-accepted
        ├── Categorical disagreements → review.csv for human adjudication
        └── Selection disagreements → review.csv for human adjudication
```

### How It Works

1. **Python handles deterministic fields**: study name, NCT ID, sponsor, phase, country, serotype (via `clean_serotype()`), raw measurement values, CI bounds, participant counts — all extracted directly from the API JSON with no ambiguity.
2. **Claude handles interpretive fields**: assay classification (OPA vs GMC), vaccine name, manufacturer, dose_number, schedule, dose_description, time_frame_weeks — fields that require understanding trial context.
3. **Two agents provide verification**: Agent A uses holistic clinical reasoning; Agent B uses systematic metadata-first evidence hierarchy. Both receive the same trial data but reason differently.
4. **Reconciliation**: Same `reconcile.py` pipeline compares outputs row-by-row using `_source_address` matching. Agreements are auto-accepted; disagreements go to `review.csv`.

### Requirements

- Python packages: `anthropic`, `python-dotenv`
- API key: Set `ANTHROPIC_API_KEY` in `.env` file at project root
- Approximate cost: ~$0.03-0.06 per trial with Sonnet, ~$2-4 for all ~70 trials

### Usage

```bash
# Extract a single trial:
python scripts/llm_extract.py NCT06151288

# Extract multiple trials:
python scripts/llm_extract.py NCT06151288 NCT03197376

# Extract all trials with cached raw.json:
python scripts/llm_extract.py --all

# Force re-extract (overwrite existing agent files):
python scripts/llm_extract.py --force NCT06151288

# Preview without writing:
python scripts/llm_extract.py --dry-run NCT06151288

# Compare LLM results against original CSV:
python scripts/llm_extract.py --compare data/wisspar_export_2026_02_05.csv

# Use a different model:
python scripts/llm_extract.py --model claude-haiku-4-5-20251001 NCT06151288
```

### Resolving Disagreements

When agents disagree, a review CSV is generated at `data/extractions/{nct_id}/review.csv`. Fill in the `chosen_value` column and run adjudication:

```bash
# Apply decisions from edited review.csv:
python scripts/adjudicate.py NCT06151288

# Interactive terminal prompts instead:
python scripts/adjudicate.py --interactive NCT06151288

# Accept all Agent A or B values:
python scripts/adjudicate.py --accept-a NCT06151288
python scripts/adjudicate.py --accept-b NCT06151288

# Check current status:
python scripts/adjudicate.py --status NCT06151288

# Append resolved rows to CSV after adjudication:
python scripts/adjudicate.py --append NCT06151288
```

### Batch Review (All Trials)

For reviewing disagreements across all trials at once:

```bash
# Build consolidated review spreadsheet (groups identical patterns):
python scripts/build_review.py

# Edit data/review_all.csv:
#   - Fill 'chosen_value' column with 'A', 'B', or a custom value
#   - Each row is a unique pattern — rows_affected shows how many data rows it covers

# Apply decisions from review_all.csv:
python scripts/apply_review.py

# Dry run (show what would be resolved):
python scripts/apply_review.py --dry-run

# Apply and export resolved rows to the main dataset:
python scripts/apply_review.py --export data/wisspar_export_2026_02_05.csv
```

### Audit Trail

Each trial produces artifacts in `data/extractions/{nct_id}/`:
- `raw.json` — cached API response
- `agent_a.json` — Agent A extraction with `_source_address` metadata
- `agent_b.json` — Agent B extraction with `_source_address` metadata
- `reconciliation.json` — full row-by-row comparison, disagreement classification, resolution history
- `review.csv` — human review file (when disagreements exist)
- `llm_prompt_a.txt` / `llm_prompt_b.txt` — prompts sent to Claude (for debugging)
- `llm_response_a.json` / `llm_response_b.json` — raw Claude responses (for debugging)

Reconciliation statuses: `FULLY_AGREED`, `PENDING_REVIEW`, `HUMAN_ADJUDICATED`

### How the Agents Differ

Both agents share the same raw JSON and produce the same 29-field schema. They differ in how they reason about interpretive fields:

| Logic | Agent A (holistic clinical) | Agent B (systematic metadata-first) |
|---|---|---|
| Approach | Understands overall trial design first, then classifies | Processes each field independently using structured evidence |
| Assay | Reasons from trial context | Prioritizes title keywords, then unitOfMeasure/paramType |
| Vaccine | Reasons about which arm/group corresponds to which product | Traces group → arm → intervention using armsInterventionsModule |
| Schedule | Uses timing gaps between measurements to identify boosters | Analyzes arm descriptions for explicit dose counts and booster indicators |
| Timeframe | Contextual interpretation | Structured regex extraction with unit conversion |

### Disagreement Types

- **Categorical**: Interpreted fields differ (assay, vaccine, schedule, etc.) → human review
- **Selection**: One agent included a row the other excluded → human review
- **Numeric**: Direct-from-API fields differ → indicates a parsing bug (critical, should not happen)

### Scripts Overview

| Script | Purpose |
|---|---|
| `scripts/llm_extract.py` | Primary extraction pipeline (LLM + deterministic) |
| `scripts/extractor_base.py` | Shared utilities: `clean_serotype()`, `extract_metadata()`, `detect_class_timepoint()`, CSV constants, API fetch/cache |
| `scripts/reconcile.py` | Compares agent outputs, generates review.csv for disagreements |
| `scripts/adjudicate.py` | Resolves disagreements from review.csv (interactive or batch) |
| `scripts/build_review.py` | Builds consolidated review spreadsheet across all trials |
| `scripts/apply_review.py` | Applies review decisions and optionally exports to main CSV |

## CSV Field Mapping Conventions

| CSV Field | Source / Rule |
|---|---|
| `outcome_overview_title` | Group title (e.g., "VAX-31 Low Dose", "PCV20") |
| `outcome_overview_id` | Group ID from API (e.g., OG000) |
| `outcome_overview_description` | Outcome measure description from API |
| `outcome_overview_assay` | `OPA` for OPA GMT outcomes, `GMC` for IgG GMC outcomes |
| `outcome_overview_serotype` | Cleaned class title (via `clean_serotype()`): strips Anti-/Opsono- prefixes, timing info, sample sizes, grouping prefixes |
| `outcome_overview_value` | Measurement value |
| `outcome_overview_upper_limit` | Upper 95% CI bound |
| `outcome_overview_lower_limit` | Lower 95% CI bound |
| `outcome_overview_participants` | From denoms counts for each group |
| `outcome_overview_dose_number` | Number of doses received at time of measurement |
| `outcome_overview_schedule` | Pattern: "1 dose adult", "3+1 child", "2 dose adult", etc. |
| `outcome_overview_dose_description` | Pattern: "1m post dose 1 adult", "1m post boost child", etc. |
| `outcome_overview_time_frame_weeks` | Numeric weeks from most recent dose to measurement (e.g., "4" for 1 month) |
| `outcome_overview_vaccine` | Standardized name from `data/vaccine_lookup.csv` |
| `outcome_overview_manufacturer` | Short company name: Pfizer, GSK, MSD, Vaxcyte, etc. |
| `clinical_trial_phase` | "Phase 1", "Phase 2", "Phase 3", "Phase 1/Phase 2", etc. |
| `location_country_code` | ISO 2-letter code (e.g., "US", "GM") |
| `location_continent` | "North America", "Africa", "Europe", etc. |

## Vaccine Naming Conventions

Use standardized names from `data/vaccine_lookup.csv`. Existing names in the dataset:
- `PCV7`, `PCV10 (Synflorix)`, `PCV10 (Pneumosil)`, `PCV13`, `PCV13 (Walvax)`
- `PCV15`, `PCV20`, `PCV21(Merck V116)`, `PCV24 (Vaxcyte VAX-24)`, `PCV25 (Inventprise)`, `PCV31 (Vaxcyte VAX-31)`
- `PPV23`
- Pattern: `PCV{valency} ({Brand/Product})` when disambiguation is needed.

Canonical manufacturer short names: `Pfizer`, `GSK`, `MSD`, `Vaxcyte`, `Inventprise`, `Serum Institute of India`, `Walvax`.

For sequential regimens (e.g., PCV13 then PPV23), assign each group the individual vaccine it received — not a combo name like "PCV13+PPV23".

## Lookup Tables

- **`data/vaccine_lookup.csv`** — Maps keyword patterns to standardized vaccine names and manufacturers. Add new entries here when new vaccine products appear.
- **`data/country_lookup.csv`** — Maps country names to ISO 2-letter codes and continents. Add entries for countries not yet covered.

## Known Edge Cases & Limitations

- **API response size**: The JSON response can be very large. Always parse with Python rather than reading directly.
- **Trials without results**: Many trials on ClinicalTrials.gov have no `resultsSection`. The script skips these automatically.
- **Trials without immunogenicity outcomes**: Some completed vaccine trials only post safety data, not OPA/IgG. The script lists available outcomes and skips.
- **Very large trials**: Trials with many outcome measures (e.g., NCT02097472) may exceed the LLM's max_tokens limit. These need to be split into smaller prompts or processed manually.
- **Multi-site country fields**: The `location_country_code` and `location_continent` fields store comma-separated values for unique countries across all sites.
- **Sequential adult regimens**: Trials where participants receive one vaccine then another (e.g., PCV13 followed by PPV23) are correctly split into individual vaccine assignments by the LLM approach.
- **Booster detection**: The LLM uses timing gaps, outcome titles, and arm descriptions to distinguish booster doses from primary series. Human review should verify schedule classification for ambiguous cases.
