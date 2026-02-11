# Extraction Comparison Summary

**Date:** 2026-02-10
**Original CSV:** `data/wisspar_export_2026_02_05.csv`
**Extraction method:** Dual-agent LLM pipeline (`scripts/llm_extract.py`)

## Row Counts

| Metric | Count |
|---|---|
| Original CSV rows | 6,203 |
| Original CSV trials | 70 |
| New extraction final rows | 7,760 |
| New extraction trials | 63 |
| Excluded rows | 663 |
| Reconciliation status | All 66 trials FULLY_AGREED (0 pending) |

## Trials in Original Only (7)

| NCT ID | Reason |
|---|---|
| NCT01641133 | Excluded: complex combination of Synflorix/Prevnar primary doses |
| NCT02225587 | Excluded: sequential PCV13-PPSV23 timing study, not head-to-head |
| NCT02097472 | Too large for single LLM prompt (known limitation) |
| NCT02736240 | Missing from extractions (no raw.json cached) |
| NCT05540028 | Missing from extractions (no raw.json cached) |
| NCT06077656 | Missing from extractions (no raw.json cached) |
| (blank) | Empty row in original CSV |

## Excluded Groups/Arms

| NCT ID | Group | Reason |
|---|---|---|
| NCT01641133 | All groups (Prevnar 1, Prevnar 2, Synflorix) | Complex combination of different primary doses |
| NCT02225587 | All groups | Sequential PCV13-PPSV23 timing, not head-to-head |
| NCT01545375 | Control Group, dPly-PhtD Group (selection only) | Agent A included rows Agent B excluded; resolved as excluded |
| NCT00546572 | 13vPnC, 23vPS | Not head-to-head trial of different PCVs |
| NCT00427895 | 23vPS, Cohort 1 | Not head-to-head trial of different PCVs |
| NCT00366678 | 7vPnC/13vPnC Before Toddler Dose | Uses mix of PCVs for primary and booster doses |

**Known issue:** NCT00546572 group "13vPnC / 13vPnC" (revaccination arm, 25 rows) was not caught by group-level exclusion because the group name doesn't match "13vPnC" exactly. Needs trial-level exclusion support or manual addition.

## Field-Level Differences (888 matched rows, 899 diffs)

### By Field

| Field | Diffs | Main Pattern |
|---|---|---|
| time_frame_weeks | 487 | `0 -> 4`: new extraction assigns 4 weeks for "1 month post dose"; original had 0 |
| vaccine | 240 | Stripping redundant manufacturer suffix, splitting sequential regimens |
| schedule | 156 | Various notation differences |
| assay | 14 | `IgG -> GMC` (3x), `GMC -> OPA` (11x in NCT00344318) |
| dose_number | 2 | Minor |

### time_frame_weeks Details (487 diffs)

The dominant pattern is `0 -> 4` (original had 0, new extraction has 4). This reflects a systematic difference: the new extraction correctly assigns 4 weeks for "1 month after dose" measurements, while the original CSV stored these as 0.

Notable outliers in the original CSV:
- NCT00427895: `2600 -> 4`, `3120 -> 4`, `936 -> 4` (original values were clearly wrong; 936 weeks = 18 years)
- NCT00366340: `28 -> 4`
- NCT00366899: `48 -> 4`
- NCT00373958: `64 -> 4`
- NCT03896477: `64 -> 0` (new extraction says baseline)

### vaccine Details (240 diffs)

| Pattern | Count | Notes |
|---|---|---|
| `PCV13 (Pfizer) -> PCV13` | ~100+ | New strips redundant manufacturer suffix (correct per naming conventions) |
| `PCV13+PPV23 -> PCV13` or `PPV23` | ~70 | New correctly splits sequential regimens into individual vaccines |
| `PCV13+PCV15 -> PCV15` | ~4 | Same sequential split pattern |
| `PCV13+PCV20+PPV23 -> PCV20` | 2 | Same pattern |
| `PCV15 (medium dose) -> PCV15` | 1 | Stripped dose qualifier |
| `PCV20 -> PCV20 (manufacturer: Pfizer)` | 57 | NCT06151288: new adds manufacturer tag (may need fixing) |

### schedule Details (156 diffs)

| Pattern | Trials | Notes |
|---|---|---|
| `1 dose adult -> 2 dose adult` | NCT00546572, NCT00574548 | Sequential regimen counted differently |
| `2+1 child -> 3+1 child` | NCT04546425 (83x) | Schedule classification disagreement |
| `3+1 child -> 3 dose child` | NCT00344318 (12x) | Notation difference |
| `0+1 child -> 1 dose child` | NCT00345358 (2x) | Notation normalization |
| `3 dose adult -> 1 dose adult` | NCT03835975 (2x) | Sequential regimen split |

### assay Details (14 diffs)

- NCT00344318: `GMC -> OPA` (11x) -- needs verification against trial outcomes
- NCT02531373, NCT03692871, NCT03893448: `IgG -> GMC` (3x) -- "IgG" was non-standard; "GMC" is the canonical name

## Row Count Differences by Trial

The new extraction includes more timepoints/outcomes per trial. Largest increases:

| NCT ID | Original | New | Diff | Likely Reason |
|---|---|---|---|---|
| NCT02037984 | 70 | 495 | +425 | More timepoints/cohorts included |
| NCT03197376 | 120 | 354 | +234 | More timepoints/cohorts included |
| NCT01204658 | 90 | 312 | +222 | More timepoints/cohorts included |
| NCT01616459 | 52 | 252 | +200 | More timepoints/cohorts included |
| NCT04546425 | 160 | 320 | +160 | More timepoints included |
| NCT02308540 | 182 | 318 | +136 | More timepoints/cohorts included |
| NCT01545375 | 5 | 134 | +129 | Original heavily curated; new includes all agreed rows |
| NCT02573181 | 8 | 120 | +112 | Original heavily curated |
| NCT02531373 | 70 | 165 | +95 | More timepoints included |
| NCT03760146 | 26 | 120 | +94 | More timepoints/cohorts included |

Trials with fewer rows:
- NCT00366678: 103 -> 90 (-13) -- excluded mixed PCV arm
- NCT03512288: 160 -> 80 (-80) -- outcome filtering removed non-qualifying measures

18 trials had identical row counts between original and new extraction.

## Actions Taken

1. **Outcome filtering** added to `is_immuno_outcome()`: excludes non-pneumococcal antibodies, fold-rise/GMFR, percentage/threshold outcomes
2. **Auto-resolution rules** in `reconcile.py`: manufacturer normalization, assay Unknown resolution, vaccine suffix stripping, dose_description 4w->1m, time_frame_weeks empty preference
3. **Prompt improvements**: mandatory time_frame_weeks (no null), explicit manufacturer mapping table
4. **Full re-extraction** (`--all --force`) across all 69 trials
5. **Batch review** via `build_review.py` / `apply_review.py` with exclusion support

## Remaining Work

- [ ] Add trial-level exclusion for NCT00546572 (25 rows from "13vPnC / 13vPnC" group still present)
- [ ] Extract missing trials: NCT02736240, NCT05540028, NCT06077656
- [ ] Handle NCT02097472 (too large for single prompt -- needs split-prompt strategy)
- [ ] Review trials with large row count increases to determine if extra timepoints should be curated
- [ ] Verify NCT00344318 assay classification (GMC vs OPA)
- [ ] Fix NCT06151288 vaccine naming: `PCV20 (manufacturer: Pfizer)` should be just `PCV20`
