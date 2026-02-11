"""
Shared base class for dual-extraction agents.

Handles API fetching, lookup table loading, metadata extraction, and structural
traversal of ClinicalTrials.gov JSON responses. Subclasses (Agent A and Agent B)
override the interpretation methods to provide independent extraction logic.
"""

import csv
import json
import os
import re
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CSV = os.path.join(PROJECT_ROOT, "data", "wisspar_export_2026_02_05.csv")
VACCINE_LOOKUP = os.path.join(PROJECT_ROOT, "data", "vaccine_lookup.csv")
COUNTRY_LOOKUP = os.path.join(PROJECT_ROOT, "data", "country_lookup.csv")
EXTRACTIONS_DIR = os.path.join(PROJECT_ROOT, "data", "extractions")

API_BASE = "https://clinicaltrials.gov/api/v2"

IMMUNO_KEYWORDS = ["OPA", "IGG", "GMT", "GMC", "GEOMETRIC"]

WEEKS_PER_MONTH = 4.33  # 52 weeks / 12 months — standard conversion


def clean_serotype(raw):
    """Clean serotype name from API class title.

    Strips timing info, sample sizes, grouping prefixes, and assay prefixes
    to extract the core serotype identifier.

    Examples:
      "Anti-6B, Month 3 (N= 149, 149)"              → "6B"
      "Common Serotypes - Serotype 4 (n=94,107)"     → "4"
      "ANTI-1 At Month 3"                            → "1"
      "OPA-19F PIII(Month 5)"                        → "19F"
      "Opsono-1"                                     → "1"
      "Anti-PhtD-Post-booster (Month 11)"            → "PhtD"
      "Pertussis - pertactin"                        → "Pertussis - pertactin"
      "Polio Type 1 (n=61,64)"                       → "Polio Type 1"
    """
    if not raw:
        return raw

    s = raw.strip()

    # 1. Remove sample size annotations: (N= 149, 149), (n=94,107), etc.
    s = re.sub(r'\s*\([Nn]\s*=\s*[\d,\s]+\)', '', s)

    # 2. Remove parenthesized month/time annotations: (Month 11), (at 7 months of age)
    s = re.sub(r'\s*\((?:at\s+)?\d+\s*months?\s*(?:of age)?\)', '', s, flags=re.I)
    s = re.sub(r'\s*\(Month\s+\d+\)', '', s, flags=re.I)

    # 3. Remove grouping prefixes: "Common Serotypes - Serotype X" etc.
    m = re.match(r'(?:Common|Additional)\s+Serotypes?\s*-?\s*(?:Serotype\s*)?(.+)', s, re.I)
    if m:
        s = m.group(1).strip()

    # 4. Remove assay prefixes: Anti-, ANTI-, Opsono-, OPA-, OPA Anti-
    s = re.sub(r'^(?:OPA\s+)?(?:Anti|ANTI|Opsono|OPA)\s*-\s*', '', s)

    # 5. Remove timing suffixes: ", Month 3", " At Month 3", " - Month 10"
    s = re.sub(r'[,\s]*(?:-\s*)?(?:At\s+)?Month\s+\d+.*$', '', s, flags=re.I)

    # 6. Remove phase markers: PIII, PII, PI (with optional parenthesized content)
    s = re.sub(r'\s*P(?:III|II|I)\s*(?:\(.*?\))?', '', s, flags=re.I)

    # 7. Remove boost/pre/post suffixes
    s = re.sub(
        r'[,\s]*(?:Post-?Booster|Pre-?Booster|POST-?BST|PRE-?BST|'
        r'POST-?PRY|PRE-?PRY|POST|PRE).*$',
        '', s, flags=re.I,
    )

    # 8. Remove standalone "Serotype" prefix (with or without space before identifier)
    s = re.sub(r'^Serotype[\s-]*(?=\w)', '', s, flags=re.I)

    # 9. Clean up trailing punctuation and whitespace
    s = s.strip().rstrip(',-').strip()

    return s if s else raw

CSV_FIELDNAMES = [
    "clinical_trial_study_name",
    "clinical_trial_study_id",
    "clinical_trial_sponsor",
    "clinical_trial_responsible_party",
    "clinical_trial_phase",
    "location_country_code",
    "location_continent",
    "study_eligibility_standard_age_list",
    "study_eligibility_ethnicity",
    "outcome_overview_title",
    "outcome_overview_id",
    "outcome_overview_description",
    "outcome_overview_time_frame",
    "outcome_overview_assay",
    "outcome_overview_dose_number",
    "outcome_overview_participants",
    "outcome_overview_serotype",
    "outcome_overview_value",
    "outcome_overview_upper_limit",
    "outcome_overview_lower_limit",
    "outcome_overview_ratio",
    "outcome_overview_vaccine",
    "outcome_overview_immunocompromised_population",
    "outcome_overview_manufacturer",
    "outcome_overview_dose_description",
    "outcome_overview_schedule",
    "outcome_overview_time_frame_weeks",
    "outcome_overview_confidence_interval",
    "outcome_overview_percent_responders",
]

VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

def load_vaccine_lookup():
    """Load vaccine keyword -> (vaccine_name, manufacturer) mapping."""
    lookup = []
    if not os.path.exists(VACCINE_LOOKUP):
        return lookup
    with open(VACCINE_LOOKUP, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lookup.append(row)
    return lookup


def load_country_lookup():
    """Load country_name -> (country_code, continent) mapping."""
    lookup = {}
    if not os.path.exists(COUNTRY_LOOKUP):
        return lookup
    with open(COUNTRY_LOOKUP, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lookup[row["country_name"]] = {
                "code": row["country_code"],
                "continent": row["continent"],
            }
    return lookup


def map_countries(locations, country_lookup):
    """Map a list of location dicts to comma-separated country codes and continents."""
    codes = []
    continents = []
    seen = set()
    for loc in locations:
        country_name = loc.get("country", "")
        if country_name in seen:
            continue
        seen.add(country_name)
        if country_name in country_lookup:
            codes.append(country_lookup[country_name]["code"])
            continents.append(country_lookup[country_name]["continent"])
        elif country_name:
            codes.append("")
            continents.append("")
    return ",".join(codes), ",".join(continents)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_get(url):
    """Fetch JSON from the ClinicalTrials.gov API."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  ERROR: HTTP {e.code} for {url}")
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR: {e.reason} for {url}")
        return None


def fetch_study(nct_id):
    """Fetch a single study by NCT ID."""
    url = f"{API_BASE}/studies/{nct_id}?fields=resultsSection,protocolSection"
    return api_get(url)


def fetch_and_cache(nct_id):
    """Fetch study JSON and cache to disk. Returns parsed JSON."""
    trial_dir = os.path.join(EXTRACTIONS_DIR, nct_id)
    raw_path = os.path.join(trial_dir, "raw.json")

    if os.path.exists(raw_path):
        with open(raw_path, "r", encoding="utf-8") as f:
            return json.load(f)

    data = fetch_study(nct_id)
    if data:
        os.makedirs(trial_dir, exist_ok=True)
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    return data


def search_studies(query, page_size=50):
    """Search for studies with results. Returns list of NCT IDs."""
    params = urllib.parse.urlencode({
        "query.term": query,
        "fields": "protocolSection.identificationModule",
        "filter.overallStatus": "COMPLETED",
        "pageSize": page_size,
        "countTotal": "true",
    })
    url = f"{API_BASE}/studies?{params}"
    data = api_get(url)
    if not data:
        return []
    total = data.get("totalCount", 0)
    studies = data.get("studies", [])
    nct_ids = []
    for s in studies:
        nct_id = (
            s.get("protocolSection", {})
            .get("identificationModule", {})
            .get("nctId", "")
        )
        if nct_id:
            nct_ids.append(nct_id)
    print(f"  Search returned {total} total results, fetched {len(nct_ids)} IDs")
    return nct_ids


# ---------------------------------------------------------------------------
# Shared metadata extraction (deterministic, no interpretation)
# ---------------------------------------------------------------------------

def parse_phase(phases):
    """Convert API phase list to display string."""
    if not phases:
        return ""
    mapping = {
        "EARLY_PHASE1": "Early Phase 1",
        "PHASE1": "Phase 1",
        "PHASE2": "Phase 2",
        "PHASE3": "Phase 3",
        "PHASE4": "Phase 4",
        "NA": "",
    }
    names = [mapping.get(p, p) for p in phases]
    names = [n for n in names if n]
    return "/".join(names)


def extract_metadata(proto, country_lookup):
    """Extract study metadata from protocolSection. No interpretation -- deterministic."""
    ident = proto.get("identificationModule", {})
    study_name = ident.get("officialTitle", ident.get("briefTitle", ""))
    nct_id = ident.get("nctId", "")

    sponsor_mod = proto.get("sponsorCollaboratorsModule", {})
    sponsor = sponsor_mod.get("leadSponsor", {}).get("name", "")
    resp_party = sponsor_mod.get("responsibleParty", {}).get("organization", sponsor)

    design = proto.get("designModule", {})
    phase = parse_phase(design.get("phases", []))

    elig = proto.get("eligibilityModule", {})
    std_ages = elig.get("stdAges", [])
    age_list_str = json.dumps(std_ages) if std_ages else ""

    locs = proto.get("contactsLocationsModule", {}).get("locations", [])
    country_codes, continents = map_countries(locs, country_lookup)

    return {
        "study_name": study_name,
        "nct_id": nct_id,
        "sponsor": sponsor,
        "resp_party": resp_party,
        "phase": phase,
        "std_ages": std_ages,
        "age_list_str": age_list_str,
        "country_codes": country_codes,
        "continents": continents,
    }


# ---------------------------------------------------------------------------
# Base extractor class
# ---------------------------------------------------------------------------

def detect_class_timepoint(cls_title):
    """Detect PRE/POST booster timepoint from raw class title.

    Returns "PRE", "POST", or None.
    POST-PRY (post-primary) is NOT treated as a booster timepoint.
    """
    if not cls_title:
        return None
    t = cls_title.upper().strip()

    # POST-PRY / PRE-PRY are primary timepoints, not booster
    if "POST-PRY" in t or "POST PRY" in t:
        return None
    if "PRE-PRY" in t or "PRE PRY" in t:
        return None

    # PRE-BST / POST-BST (explicit booster markers)
    if "PRE-BST" in t or "PRE BST" in t:
        return "PRE"
    if "POST-BST" in t or "POST BST" in t:
        return "POST"

    # Generic PRE/POST at end of title or after comma
    if t.endswith(" PRE") or ", PRE" in t:
        return "PRE"
    if t.endswith(" POST") or ", POST" in t:
        return "POST"

    return None


def _find_primary_count(all_outcomes):
    """Search all outcome titles for 'X-dose infant series' to get primary dose count."""
    if not all_outcomes:
        return None
    for om in all_outcomes:
        t = om.get("title", "").lower()
        tf = om.get("timeFrame", "").lower()
        combined = f"{t} {tf}"
        m = re.search(r'(\d+)-?\s*dose\s+infant\s+series', combined)
        if m:
            return int(m.group(1))
        # Also check "postvaccination X" to find the max
        m = re.search(r'post[- ]?vaccination\s+(\d+)', combined)
        if m:
            return int(m.group(1)) - 1  # Assume last is booster
    return None


class ExtractorBase:
    """Base class for extraction agents. Subclasses override interpret_* methods."""

    agent_name = "base"

    def __init__(self, vaccine_lookup, country_lookup):
        self.vaccine_lookup = vaccine_lookup
        self.country_lookup = country_lookup

    def _match_group_to_arm(self, group, proto):
        """Match an outcome measure group to its arm description.

        Returns arm dict or None.
        Uses multiple strategies: exact substring, normalized prefix, and
        vaccine-abbreviation expansion.
        """
        if not group:
            return None
        g_title = group.get("title", "").upper()
        if not g_title:
            return None
        arms_module = proto.get("armsInterventionsModule", {})
        arm_groups = arms_module.get("armGroups", [])

        # Strategy 1: Exact substring match (both directions)
        # Require min 3 chars to avoid false matches on labels like "A", "1"
        for arm in arm_groups:
            arm_label = arm.get("label", "")
            if not arm_label:
                continue
            arm_upper = arm_label.upper()
            if len(arm_upper) >= 3 and arm_upper in g_title:
                return arm
            if len(g_title) >= 3 and g_title in arm_upper:
                return arm
            # Exact match (any length)
            if g_title == arm_upper:
                return arm

        # Strategy 2: Normalize both sides and retry
        # Strip common suffixes: "Pooled", "Group", cohort/population markers
        strip_re = re.compile(
            r'\b(POOLED|COMBINED|GROUP|PRIMARY STUDY POPULATION'
            r'|BEFORE|AFTER)\b',
            re.IGNORECASE,
        )
        g_norm = strip_re.sub('', g_title)
        g_norm = re.sub(r'(TODDLER|INFANT|BOOSTER)\s+(DOSE|SERIES)', '', g_norm)
        g_norm = re.sub(r'COHORT\s*\d*', '', g_norm)
        g_norm = re.sub(r'[:\-/,\d]', ' ', g_norm)
        g_norm = re.sub(r'\s+', ' ', g_norm).strip()

        if g_norm and len(g_norm) >= 3:
            for arm in arm_groups:
                arm_label_u = arm.get("label", "").upper()
                arm_norm = strip_re.sub('', arm_label_u)
                arm_norm = re.sub(r'COHORT\s*\d*', '', arm_norm)
                arm_norm = re.sub(r'[:\-/,\d]', ' ', arm_norm)
                arm_norm = re.sub(r'\s+', ' ', arm_norm).strip()
                if arm_norm and (g_norm in arm_norm or arm_norm in g_norm):
                    return arm

        # Strategy 3: Vaccine abbreviation expansion
        # "13vPnC" -> match arms containing "13-valent" or "13v"
        m = re.match(r'(\d+)V', g_title.strip())
        if m:
            valency = m.group(1)
            for arm in arm_groups:
                arm_u = arm.get("label", "").upper()
                arm_d = arm.get("description", "").upper()
                if (f"{valency}-VALENT" in arm_u or f"{valency}V" in arm_u
                        or f"{valency}-VALENT" in arm_d):
                    return arm

        return None

    @staticmethod
    def _extract_dose_from_text(combined):
        """Extract (primary_doses, has_booster, found) from text.

        Returns (primary_doses, has_booster, True) if a dose pattern was
        found, or (1, False, False) as default.
        """
        if not combined.strip():
            return (1, False, False)

        # Detect booster (mentioned explicitly but not negated)
        has_booster = (
            bool(re.search(r'\bbooster\b', combined))
            and not bool(re.search(r'\bno\s+booster\b', combined))
        )

        # "X-dose primary" or "X dose primary"
        m = re.search(r'(\d+)-?\s*dose\s+primary', combined)
        if m:
            return (int(m.group(1)), has_booster, True)

        # "X primary doses"
        m = re.search(r'(\d+)\s+primary\s+doses?', combined)
        if m:
            return (int(m.group(1)), has_booster, True)

        # Timepoint gap detection (check BEFORE generic dose count so that
        # "4 vaccinations at 2, 4, 6, and 12 months" detects 3+1, not just 4)
        # Note: API sometimes returns \~ (escaped tilde), so match \\?~
        tp_match = re.search(
            r'at\s+(?:\\?~\s*)?'
            r'([\d]+(?:\s*,\s*\d+)*(?:\s*(?:,\s*)?(?:and\s+)?\d+)?)'
            r'\s*(?:to\s+\d+\s+)?(?:weeks?|months?|days?)\s+of\s+age',
            combined,
        )
        if tp_match:
            timepoints = re.findall(r'\d+', tp_match.group(1))
            if len(timepoints) >= 2:
                times = sorted(int(t) for t in timepoints)
                # Check for booster gap: last dose much later than prior gaps
                if len(times) >= 3:
                    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
                    avg_primary = sum(gaps[:-1]) / len(gaps[:-1])
                    if avg_primary > 0 and gaps[-1] > avg_primary * 2:
                        return (len(times) - 1, True, True)
                return (len(times), has_booster, True)

        # General "X doses" / "X dose" / "X injections" / "X vaccinations"
        # Also handles "X total ... vaccinations" (words between number and unit)
        # Exclude false positives like "23-valent" or "23vPS"
        m = re.search(r'(\d+)\s*(?:doses?|injections?|vaccinations?)\b', combined)
        if not m:
            m = re.search(
                r'(\d+)\s+total\b.*?(?:doses?|injections?|vaccinations?)\b',
                combined,
            )
        if m:
            count = int(m.group(1))
            if count <= 10:  # Sanity: no PCV trial has >10 doses
                return (count, has_booster, True)

        # "single dose" / "single injection"
        if re.search(r'single\s+(dose|injection)', combined):
            return (1, has_booster, True)

        return (1, False, False)

    def parse_arm_dose_info(self, group, proto):
        """Parse primary dose count and booster status from the group's arm.

        Returns (primary_doses, has_booster, found) where found=True means
        an explicit dose pattern was found in the text.
        """
        arm = self._match_group_to_arm(group, proto)

        # Combine group description and arm description
        texts = []
        if group:
            texts.append(group.get("description", ""))
        if arm:
            texts.append(arm.get("description", ""))
        combined = " ".join(texts).lower()

        result = self._extract_dose_from_text(combined)
        if result[2]:  # found
            return result

        # Fallback: if no arm matched and no dose info in group desc,
        # check ALL arms for consensus dose count
        if not arm:
            arms_module = proto.get("armsInterventionsModule", {})
            all_arms = arms_module.get("armGroups", [])
            if all_arms:
                arm_results = []
                for a in all_arms:
                    a_desc = a.get("description", "").lower()
                    a_label = a.get("label", "").lower()
                    a_text = f"{a_label} {a_desc}"
                    ar = self._extract_dose_from_text(a_text)
                    if ar[2]:
                        arm_results.append(ar)
                if arm_results:
                    # Use consensus: if all found arms agree on dose count
                    doses = set(ar[0] for ar in arm_results)
                    boosters = any(ar[1] for ar in arm_results)
                    if len(doses) == 1:
                        return (doses.pop(), boosters, True)

        return (1, False, False)

    @staticmethod
    def infer_dose_from_outcome_title(outcome_measure, all_outcomes=None,
                                      trial_has_booster=False,
                                      max_outcome_dose=0):
        """Infer dose number and booster status from outcome title/timeframe.

        Returns (dose_number_int, is_booster) or None if not determinable.
        Used as fallback when arm/group descriptions have no dose info.

        When trial_has_booster=True and max_outcome_dose is known, uses
        dose numbering to detect booster (max dose = booster).
        """
        title = outcome_measure.get("title", "").lower()
        timeframe = outcome_measure.get("timeFrame", "").lower()
        combined = f"{title} {timeframe}"

        # "X-dose infant series" -> primary_doses = X
        m = re.search(r'(\d+)-?\s*dose\s+infant\s+series', combined)
        if m:
            primary = int(m.group(1))
            is_booster = "toddler" in combined or "booster" in combined
            if is_booster:
                return (primary + 1, True)
            return (primary, False)

        # "infant series dose X" / "dose X of the infant series"
        m = re.search(r'(?:infant\s+series\s+dose|dose\s+\d+\s+of\s+(?:the\s+)?infant\s+series)\s*(\d+)?', combined)
        if m:
            # Try to find the number
            nums = re.findall(r'dose\s+(\d+)', combined)
            if nums:
                return (int(nums[0]), False)

        # "postvaccination X" / "post-vaccination X" / "post dose X"
        m = re.search(r'post[- ]?(?:vaccination|dose)\s+(\d+)', combined)
        if m:
            dose = int(m.group(1))
            # If this is the max dose and trial has booster, it's the booster
            if (trial_has_booster and max_outcome_dose >= 3
                    and dose == max_outcome_dose):
                return (dose, True)
            return (dose, False)

        # "after vaccination X" / "vaccination X"
        m = re.search(r'(?:after\s+)?vaccination\s+(\d+)', combined)
        if m:
            dose = int(m.group(1))
            if (trial_has_booster and max_outcome_dose >= 3
                    and dose == max_outcome_dose):
                return (dose, True)
            return (dose, False)

        # "after dose X" / "1 month after dose X"
        m = re.search(r'after\s+dose\s+(\d+)', combined)
        if m:
            dose = int(m.group(1))
            if (trial_has_booster and max_outcome_dose >= 3
                    and dose == max_outcome_dose):
                return (dose, True)
            return (dose, False)

        # "toddler dose" or "booster dose" without explicit primary count
        has_toddler_or_booster = (
            "toddler dose" in combined or "booster dose" in combined
        )
        if has_toddler_or_booster:
            # Distinguish "before toddler dose" (pre-booster) from
            # "after toddler dose" (post-booster)
            is_pre_booster = bool(re.search(
                r'before\s+(?:the\s+)?(?:toddler|booster)\s+dose', combined
            ))
            # Try to find primary_doses from OTHER outcomes in the same trial
            primary_count = _find_primary_count(all_outcomes)
            if primary_count:
                if is_pre_booster:
                    return (primary_count, False)
                return (primary_count + 1, True)
            # Can't determine without primary count
            return (None, True)  # Caller handles None

        # "after the infant series" (no explicit count)
        if "infant series" in combined and "toddler" not in combined:
            primary_count = _find_primary_count(all_outcomes)
            if primary_count:
                return (primary_count, False)

        return None

    def extract(self, data, nct_id):
        """Main extraction entry point. Returns list of row dicts with _source_address."""
        proto = data.get("protocolSection", {})
        results = data.get("resultsSection")
        if not results:
            return []

        meta = extract_metadata(proto, self.country_lookup)
        all_outcomes = (
            results.get("outcomeMeasuresModule", {})
            .get("outcomeMeasures", [])
        )

        # Age label for PRE/POST override
        std_ages = meta.get("std_ages", [])
        is_child = any(a.upper() == "CHILD" for a in std_ages)
        age_label = "child" if is_child else "adult"

        # Pre-scan: does ANY outcome in this trial mention booster/toddler?
        trial_has_booster = any(
            "toddler" in om.get("title", "").lower()
            or "booster" in om.get("title", "").lower()
            or "toddler" in om.get("timeFrame", "").lower()
            or "booster" in om.get("timeFrame", "").lower()
            for om in all_outcomes
        )

        # Also check arm descriptions for timepoint gap (3+1 pattern)
        if not trial_has_booster:
            arms_module = proto.get("armsInterventionsModule", {})
            for arm in arms_module.get("armGroups", []):
                desc = arm.get("description", "").lower()
                label = arm.get("label", "").lower()
                _, arm_booster, arm_found = self._extract_dose_from_text(
                    f"{label} {desc}"
                )
                if arm_found and arm_booster:
                    trial_has_booster = True
                    break

        # Check for "before dose X" in outcomes (pre-booster measurement)
        if not trial_has_booster:
            for om in all_outcomes:
                t = om.get("title", "").lower()
                tf = om.get("timeFrame", "").lower()
                if "before dose" in t or "before dose" in tf:
                    trial_has_booster = True
                    break

        # Pre-scan: find max dose number mentioned in outcome titles
        max_outcome_dose = 0
        for om in all_outcomes:
            t = om.get("title", "").lower()
            tf = om.get("timeFrame", "").lower()
            for dm in re.finditer(
                r'(?:after|before|post[- ]?)\s*(?:dose|vaccination)\s+(\d+)',
                f"{t} {tf}",
            ):
                d = int(dm.group(1))
                if d > max_outcome_dose:
                    max_outcome_dose = d

        trial_primary_count = _find_primary_count(all_outcomes)

        # Store for use by infer_dose_number()
        self._trial_has_booster = trial_has_booster
        self._max_outcome_dose = max_outcome_dose

        rows = []
        for om_idx, om in enumerate(all_outcomes):
            if not self.is_immunogenicity_outcome(om, proto):
                continue

            title = om.get("title", "")
            assay = self.classify_assay(om, proto)
            description = om.get("description", "")
            timeframe = om.get("timeFrame", "")
            time_weeks = self.parse_timeframe_weeks(timeframe, om)

            # Groups
            groups = {g["id"]: g for g in om.get("groups", [])}

            # Participant counts from denoms
            denom_counts = {}
            for d in om.get("denoms", []):
                for c in d.get("counts", []):
                    denom_counts[c["groupId"]] = c["value"]

            # Build vaccine/manufacturer/dose mapping per group
            group_info = {}
            for gid, g in groups.items():
                vac_name, mfr = self.resolve_vaccine(g, om, meta, proto)
                dn = self.infer_dose_number(om, meta, proto, group=g)
                sched = self.infer_schedule(om, meta, proto, dn, group=g)
                ddesc = self.infer_dose_description(
                    om, meta, proto, dn, time_weeks, group=g
                )
                # Arm dose info for PRE/POST class-level override
                primary_doses, _, arm_found = self.parse_arm_dose_info(g, proto)

                group_info[gid] = {
                    "title": g.get("title", ""),
                    "vaccine": vac_name,
                    "manufacturer": mfr,
                    "participants": denom_counts.get(gid, ""),
                    "dose_number": dn,
                    "schedule": sched,
                    "dose_desc": ddesc,
                    "primary_doses": primary_doses,
                    "_arm_found": arm_found,
                }

            # Post-process: apply trial-level booster to schedule.
            # When the arm dose count == max_outcome_dose, the count
            # includes the booster (primary = N-1). Otherwise the arm
            # count is primary only and booster is separate (primary = N).
            if trial_has_booster:
                for gid, gi in group_info.items():
                    sched = gi["schedule"]
                    sched_m = re.match(r'^(\d+) dose (\w+)$', sched)
                    if sched_m:
                        n = int(sched_m.group(1))
                        if (max_outcome_dose > 0 and n == max_outcome_dose
                                and n > 1):
                            # Dose count includes booster
                            pri = n - 1
                        else:
                            # Dose count is primary only
                            pri = n
                        gi["schedule"] = (
                            f"{pri}+1 {sched_m.group(2)}"
                        )
                        gi["primary_doses"] = pri

            # Outcome-title fallback: when arm-level dose info was not found,
            # try inferring from the outcome title/timeframe
            any_missing = any(
                not gi["_arm_found"] for gi in group_info.values()
            )
            if any_missing:
                om_inferred = self.infer_dose_from_outcome_title(
                    om, all_outcomes,
                    trial_has_booster=trial_has_booster,
                    max_outcome_dose=max_outcome_dose,
                )
                if om_inferred and om_inferred[0] is not None:
                    om_dose, om_is_booster = om_inferred
                    # Find primary count for schedule label
                    pri = om_dose - 1 if om_is_booster else om_dose
                    for gid, gi in group_info.items():
                        if gi["_arm_found"]:
                            continue

                        # Check if group title indicates pre-booster
                        g_lower = gi["title"].lower()
                        is_pre_group = bool(re.search(
                            r'before\s+(?:the\s+)?(?:toddler|booster)',
                            g_lower,
                        )) or bool(re.search(
                            r'after\s+(?:the\s+)?(?:infant\s+series)',
                            g_lower,
                        ))

                        if is_pre_group and om_is_booster:
                            # Pre-booster group: dose = primary count
                            gi["dose_number"] = str(pri)
                            gi["primary_doses"] = pri
                            gi["schedule"] = f"{pri}+1 {age_label}"
                            gi["dose_desc"] = (
                                f"pre boost {age_label}"
                            )
                        elif om_is_booster:
                            gi["dose_number"] = str(om_dose)
                            gi["primary_doses"] = pri
                            gi["schedule"] = f"{pri}+1 {age_label}"
                            if time_weeks:
                                gi["dose_desc"] = (
                                    f"{time_weeks}w post boost "
                                    f"{age_label}"
                                )
                            else:
                                gi["dose_desc"] = (
                                    f"post boost {age_label}"
                                )
                        else:
                            gi["dose_number"] = str(om_dose)
                            gi["primary_doses"] = om_dose
                            # Use trial-level booster info for schedule
                            if trial_has_booster:
                                gi["schedule"] = (
                                    f"{om_dose}+1 {age_label}"
                                )
                            else:
                                gi["schedule"] = (
                                    f"{om_dose} dose {age_label}"
                                )
                            if time_weeks == "4":
                                gi["dose_desc"] = (
                                    f"1m post dose {om_dose} "
                                    f"{age_label}"
                                )
                            elif time_weeks:
                                gi["dose_desc"] = (
                                    f"{time_weeks}w post dose "
                                    f"{om_dose} {age_label}"
                                )
                            else:
                                gi["dose_desc"] = (
                                    f"post dose {om_dose}"
                                )

            # Extract measurements
            for cls_idx, cls in enumerate(om.get("classes", [])):
                raw_cls_title = cls.get("title", "")
                serotype = clean_serotype(raw_cls_title)
                cls_timepoint = detect_class_timepoint(raw_cls_title)

                for cat_idx, cat in enumerate(cls.get("categories", [])):
                    for m_idx, m in enumerate(cat.get("measurements", [])):
                        gid = m.get("groupId", "")
                        if gid not in group_info:
                            continue
                        gi = group_info[gid]

                        # Default to group-level values
                        dose_number = gi["dose_number"]
                        dose_desc = gi["dose_desc"]

                        # Override for PRE-booster classes
                        if cls_timepoint == "PRE":
                            dose_number = str(gi["primary_doses"])
                            dose_desc = f"pre boost {age_label}"

                        source_address = {
                            "outcome_index": om_idx,
                            "class_index": cls_idx,
                            "category_index": cat_idx,
                            "measurement_index": m_idx,
                            "group_id": gid,
                            "outcome_title": title,
                        }

                        row = {
                            "clinical_trial_study_name": meta["study_name"],
                            "clinical_trial_study_id": nct_id,
                            "clinical_trial_sponsor": meta["sponsor"],
                            "clinical_trial_responsible_party": meta["resp_party"],
                            "clinical_trial_phase": meta["phase"],
                            "location_country_code": meta["country_codes"],
                            "location_continent": meta["continents"],
                            "study_eligibility_standard_age_list": meta["age_list_str"],
                            "study_eligibility_ethnicity": "",
                            "outcome_overview_title": gi["title"],
                            "outcome_overview_id": gid,
                            "outcome_overview_description": description,
                            "outcome_overview_time_frame": timeframe,
                            "outcome_overview_assay": assay,
                            "outcome_overview_dose_number": dose_number,
                            "outcome_overview_participants": gi["participants"],
                            "outcome_overview_serotype": serotype,
                            "outcome_overview_value": m.get("value", ""),
                            "outcome_overview_upper_limit": m.get("upperLimit", ""),
                            "outcome_overview_lower_limit": m.get("lowerLimit", ""),
                            "outcome_overview_ratio": "",
                            "outcome_overview_vaccine": gi["vaccine"],
                            "outcome_overview_immunocompromised_population": "",
                            "outcome_overview_manufacturer": gi["manufacturer"],
                            "outcome_overview_dose_description": dose_desc,
                            "outcome_overview_schedule": gi["schedule"],
                            "outcome_overview_time_frame_weeks": time_weeks,
                            "outcome_overview_confidence_interval": "",
                            "outcome_overview_percent_responders": "0",
                            "_source_address": source_address,
                            "_agent": self.agent_name,
                        }
                        rows.append(row)

        return rows

    # ------------------------------------------------------------------
    # Methods subclasses MUST override
    # ------------------------------------------------------------------

    def classify_assay(self, outcome_measure, proto):
        """Determine assay type (OPA, GMC, Unknown) from an outcome measure."""
        raise NotImplementedError

    def is_immunogenicity_outcome(self, outcome_measure, proto):
        """Return True if this outcome measure should be extracted."""
        raise NotImplementedError

    def resolve_vaccine(self, group, outcome_measure, metadata, proto):
        """Return (vaccine_name, manufacturer) for a group."""
        raise NotImplementedError

    def parse_timeframe_weeks(self, timeframe_text, outcome_measure):
        """Convert timeframe text to numeric weeks string."""
        raise NotImplementedError

    def infer_schedule(self, outcome_measure, metadata, proto, dose_number, group=None):
        """Infer schedule string (e.g. '1 dose adult', '3+1 child')."""
        raise NotImplementedError

    def infer_dose_number(self, outcome_measure, metadata, proto, group=None):
        """Infer dose number string. group is the outcome measure group dict."""
        raise NotImplementedError

    def infer_dose_description(self, outcome_measure, metadata, proto, dose_number, time_weeks, group=None):
        """Infer dose description string."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Utility: match vaccine from keyword lookup (shared helper, agents may or may not use)
# ---------------------------------------------------------------------------

def match_vaccine_keyword(text, vaccine_lookup):
    """Try to match text against vaccine lookup keywords. Returns (vaccine_name, manufacturer) or (None, None)."""
    text_upper = text.upper()
    for entry in vaccine_lookup:
        if entry["keyword"].upper() in text_upper:
            return entry["vaccine_name"], entry["manufacturer"]
    return None, None


# ---------------------------------------------------------------------------
# Utility: save/load extraction results as JSON
# ---------------------------------------------------------------------------

def save_extraction(rows, nct_id, agent_name):
    """Save agent extraction results to JSON."""
    trial_dir = os.path.join(EXTRACTIONS_DIR, nct_id)
    os.makedirs(trial_dir, exist_ok=True)
    path = os.path.join(trial_dir, f"agent_{agent_name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "nct_id": nct_id,
            "agent": agent_name,
            "version": VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "row_count": len(rows),
            "rows": rows,
        }, f, indent=2)
    return path


def load_extraction(nct_id, agent_name):
    """Load agent extraction results from JSON."""
    path = os.path.join(EXTRACTIONS_DIR, nct_id, f"agent_{agent_name}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def now_iso():
    """Return current UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Study design consistency validation
# ---------------------------------------------------------------------------

def validate_study_design(rows, data, nct_id):
    """Check if extracted categories are consistent with the study design.

    Compares extracted schedules, vaccines, and doses across groups
    against the trial's arm group design to flag potential misclassifications.

    Returns a list of warning strings (empty if no issues found).
    """
    warnings = []
    if not rows:
        return warnings

    proto = data.get("protocolSection", {})
    arms_module = proto.get("armsInterventionsModule", {})
    arm_groups = arms_module.get("armGroups", [])
    interventions = arms_module.get("interventions", [])

    # --- Collect per-group extracted values ---
    group_schedules = {}  # group_title -> set of schedules
    group_vaccines = {}   # group_title -> set of vaccines
    group_doses = {}      # group_title -> set of dose_numbers
    for row in rows:
        title = row.get("outcome_overview_title", "")
        group_schedules.setdefault(title, set()).add(
            row.get("outcome_overview_schedule", "")
        )
        group_vaccines.setdefault(title, set()).add(
            row.get("outcome_overview_vaccine", "")
        )
        group_doses.setdefault(title, set()).add(
            row.get("outcome_overview_dose_number", "")
        )

    # --- Check 1: Internal consistency (each group should be uniform) ---
    for title in group_schedules:
        if len(group_schedules[title]) > 1:
            warnings.append(
                f"Group '{title}' has mixed schedules: "
                f"{sorted(group_schedules[title])}. "
                f"Each group should have a single schedule."
            )
        if len(group_vaccines[title]) > 1:
            warnings.append(
                f"Group '{title}' has mixed vaccines: "
                f"{sorted(group_vaccines[title])}. "
                f"Each group should have a single vaccine."
            )

    # --- Check 2: Schedule differentiation ---
    # If protocol arms have different dose counts, extracted schedules should differ
    dose_counts_per_arm = []
    for arm in arm_groups:
        desc = arm.get("description", "").lower()
        label = arm.get("label", "").lower()
        combined = f"{label} {desc}"
        m = re.search(r"(\d+)-?\s*dose\s+primary", combined)
        if m:
            dose_counts_per_arm.append(m.group(1))
        else:
            m = re.search(r"(\d+)-?\s*dose", combined)
            if m:
                dose_counts_per_arm.append(m.group(1))

    if len(set(dose_counts_per_arm)) > 1:
        # Protocol describes arms with different dose counts
        all_extracted_schedules = set()
        for s_set in group_schedules.values():
            all_extracted_schedules.update(s_set)
        if len(all_extracted_schedules) == 1:
            warnings.append(
                f"Protocol arms have different dose counts "
                f"({', '.join(sorted(set(dose_counts_per_arm)))}), "
                f"but all extracted groups have the same schedule: "
                f"'{all_extracted_schedules.pop()}'. "
                f"Check if dose/schedule inference is differentiating arms correctly."
            )

    # --- Check 3: Vaccine differentiation ---
    # If trial compares different vaccines, extracted vaccine names should differ
    vaccine_interventions = []
    for inv in interventions:
        inv_type = inv.get("type", "").upper()
        if inv_type in ("DRUG", "BIOLOGICAL", ""):
            name = inv.get("name", "")
            if name:
                vaccine_interventions.append(name)

    if len(set(vaccine_interventions)) > 1:
        all_extracted_vaccines = set()
        for v_set in group_vaccines.values():
            all_extracted_vaccines.update(v_set)
        if len(all_extracted_vaccines) == 1 and len(group_vaccines) > 1:
            warnings.append(
                f"Protocol has {len(set(vaccine_interventions))} distinct interventions "
                f"({', '.join(sorted(set(vaccine_interventions))[:4])}), "
                f"but all extracted groups have the same vaccine: "
                f"'{all_extracted_vaccines.pop()}'. "
                f"Check vaccine_lookup.csv or group-to-arm mapping."
            )

    # --- Check 4: Group count consistency ---
    n_extracted_groups = len(group_schedules)
    n_protocol_arms = len(arm_groups)
    if n_protocol_arms > 0 and n_extracted_groups > 0:
        if n_extracted_groups > n_protocol_arms * 2:
            warnings.append(
                f"Extracted {n_extracted_groups} distinct groups but protocol "
                f"only has {n_protocol_arms} arm groups. "
                f"Some groups may be duplicated or incorrectly split."
            )

    # --- Check 5: All groups identical across all dimensions ---
    if len(group_schedules) >= 2:
        flat_schedules = set()
        flat_vaccines = set()
        flat_doses = set()
        for s_set in group_schedules.values():
            flat_schedules.update(s_set)
        for v_set in group_vaccines.values():
            flat_vaccines.update(v_set)
        for d_set in group_doses.values():
            flat_doses.update(d_set)
        if (len(flat_schedules) == 1 and len(flat_vaccines) == 1
                and len(flat_doses) == 1):
            warnings.append(
                f"All {len(group_schedules)} groups have identical schedule "
                f"('{flat_schedules.pop()}'), vaccine ('{flat_vaccines.pop()}'), "
                f"and dose ('{flat_doses.pop()}'). "
                f"This trial likely compares different treatments — "
                f"verify that arm differentiation is working."
            )
