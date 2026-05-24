"""
Regression test suite for the hospital protocol bot.

HOW TO RUN:
    python test_bot.py           # fast tests only (no API calls)
    python test_bot.py --all     # include integration tests (uses OpenAI API)

Fast tests (no API needed):
  - Alias/synonym recognition
  - clean_response post-processing
  - Source label derivation
  - Protocol file path matching

Integration tests (requires OPENAI_API_KEY and protocol files in place):
  - Full ask_ai() calls with expected output fragments

Add new cases to ALIAS_CASES and INTEGRATION_CASES as you add protocols.
"""

import sys
import os
import re
import json

# ---------------------------------------------------------------------------
# Minimal inline copies of the pure-Python functions (no imports needed)
# This lets you run fast tests without installing any bot dependencies.
# ---------------------------------------------------------------------------

_BOLD_RE        = re.compile(r'\*\*(.+?)\*\*', re.DOTALL)
_SOURCE_LINE_RE = re.compile(
    r'[\n\r]?[ \t]*[-•]?[ \t]*'
    r'(?:Source|Forrás|Source file[s]?|Forrás fájl[ok]?)'
    r'[ \t]*[:\*]*[ \t]*[`"]?[^\n\r]*',
    re.IGNORECASE
)
_FILE_PATH_RE   = re.compile(r'`?protocols/[^\s`\n\r,;]+`?', re.IGNORECASE)
_NOT_SPEC_RE    = re.compile(
    r'[-•]?[ \t]*This is not specified in the uploaded protocol\.?[ \t]*[\n\r]?',
    re.IGNORECASE
)
_BLANK_RE       = re.compile(r'\n{3,}')
_HAS_DOSING_RE  = re.compile(r'\d+\s*(mg|g|amp|ml|mmol|mcg)', re.IGNORECASE)


def clean_response(text, source_label):
    text = _BOLD_RE.sub(r'\1', text)
    text = _SOURCE_LINE_RE.sub('', text)
    text = _FILE_PATH_RE.sub('', text)
    if _HAS_DOSING_RE.search(text):
        text = _NOT_SPEC_RE.sub('', text)
    text = _BLANK_RE.sub('\n\n', text).strip()
    if source_label:
        text = text + f'\n\nSource: {source_label}'
    return text


def derive_source_label(file_path):
    from pathlib import Path
    stem = Path(file_path).stem.lower()
    fallback_labels = {
        "tmpsmx":                          "TMP/SMX",
        "tmp_smx":                         "TMP/SMX",
        "ampsul":                          "ampicillin/sulbactam",
        "ampicillin_sulbactam":            "ampicillin/sulbactam",
        "meropenem":                       "meropenem",
        "cap":                             "CAP",
        "biofire":                         "BioFire",
        "pneumonia_pcr":                   "BioFire",
        "general_rules_antibiotic_dosing": "General antibiotic dosing rules",
    }
    return fallback_labels.get(stem, stem.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = {"pass": 0, "fail": 0}


def ok(name):
    results["pass"] += 1
    print(f"  {PASS}  {name}")


def fail(name, detail=""):
    results["fail"] += 1
    msg = f"  {FAIL}  {name}"
    if detail:
        msg += f"\n         → {detail}"
    print(msg)


def assert_equal(name, got, expected):
    if got == expected:
        ok(name)
    else:
        fail(name, f"expected {expected!r}, got {got!r}")


def assert_contains(name, text, fragment):
    if fragment.lower() in text.lower():
        ok(name)
    else:
        fail(name, f"expected to find {fragment!r} in:\n         {text!r}")


def assert_not_contains(name, text, fragment):
    if fragment.lower() not in text.lower():
        ok(name)
    else:
        fail(name, f"did NOT expect to find {fragment!r} in:\n         {text!r}")


# ---------------------------------------------------------------------------
# 1. Alias recognition tests (no API)
# ---------------------------------------------------------------------------

def load_alias_index(aliases_path="protocols/aliases.json"):
    """Load and build the alias index exactly as the bot does."""
    if not os.path.exists(aliases_path):
        print(f"  WARNING: {aliases_path} not found — skipping alias tests")
        return {}, {}
    with open(aliases_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    alias_index = {}
    protocol_file_to_label = {}
    for category in ["drugs", "conditions"]:
        for key, item in data.get(category, {}).items():
            display       = item.get("display", key)
            canonical     = item.get("canonical", display)
            source_label  = item.get("source_label", display)
            protocol_file = item.get("protocol_file", "")
            entry = {
                "key": key, "category": category,
                "display": display, "canonical": canonical,
                "source_label": source_label, "protocol_file": protocol_file,
            }
            if protocol_file:
                from pathlib import Path
                norm = str(Path(protocol_file)).replace("\\", "/").lower()
                protocol_file_to_label[norm] = source_label
            for term in [display, canonical] + item.get("aliases", []):
                if term:
                    alias_index[term.lower()] = entry
    return alias_index, protocol_file_to_label


def match_alias(text, alias_index):
    """Exact alias matching — mirrors normalize_question() in the bot."""
    text = text.lower().strip()
    for alias in sorted(alias_index.keys(), key=len, reverse=True):
        data = alias_index[alias]
        if len(alias) <= 4:
            matched = re.search(r"\b" + re.escape(alias) + r"\b", text) is not None
        else:
            matched = alias in text
        if matched:
            return data
    return None


# Test cases: (input_text, expected_display_or_None)
ALIAS_CASES = [
    # TMP/SMX variants
    ("What's the dose of sumetrolim?",          "TMP/SMX"),
    ("Sumetrolim forte for UTI",                "TMP/SMX"),
    ("TMP/SMX dose",                            "TMP/SMX"),
    ("co-trimoxazole dosing",                   "TMP/SMX"),
    ("Bactrim iv",                              "TMP/SMX"),
    ("cotrimoxazole",                           "TMP/SMX"),
    ("trimethoprim/sulfamethoxazole",           "TMP/SMX"),

    # Meropenem variants
    ("meropenem septic shock",                  "meropenem"),
    ("mero dose",                               "meropenem"),
    ("meronem iv",                              "meropenem"),

    # Amp/Sul variants
    ("unasyn high dose",                        "ampicillin/sulbactam"),
    ("amp/sul MACI",                            "ampicillin/sulbactam"),
    ("ampsul CRAB",                             "ampicillin/sulbactam"),
    ("high-dose sulbactam",                     "ampicillin/sulbactam"),

    # CAP variants
    ("CAP treatment",                           "CAP"),
    ("community acquired pneumonia",            "CAP"),
    ("tüdőgyulladás kezelés",                   "CAP"),
    ("COPD exacerbation",                       "CAP"),

    # BioFire variants
    ("biofire pneumonia panel",                 "BioFire"),
    ("filmarray results",                       "BioFire"),

    # Should NOT match anything
    ("what time is it",                         None),
    ("vancomycin dose",                         None),
]


# Fuzzy / misspell cases — these exercise the per-word fuzzy + partial_ratio
# cascade in the real normalize_question(). Match via inline substring matcher
# would always say None for these, so we test against the real function.
FUZZY_CASES = [
    # one-letter swap (ne ↔ en) in the keyword + sentence noise
    ("Penumoniara mit adjak?",     "CAP"),
    ("penumonia mit adjak?",       "CAP"),
    # misspelled drug + sentence noise
    ("meronemet adjak?",           "meropenem"),
    # compound alias with typo — exercises partial_ratio fallback
    ("ampicilin sulbaktam dose",   "ampicillin/sulbactam"),
    # genuine no-match — must NOT spuriously match to anything
    ("uti-re mit adjak?",          None),
    ("vancomycin dose",            None),
]


def test_alias_recognition():
    print("\n=== 1. Alias recognition ===")
    alias_index, _ = load_alias_index()
    if not alias_index:
        return

    for text, expected_display in ALIAS_CASES:
        result = match_alias(text, alias_index)
        got_display = result["display"] if result else None
        assert_equal(f'recognize: "{text[:50]}"', got_display, expected_display)


def test_fuzzy_recognition():
    """Test the real normalize_question() — covers per-word fuzzy + partial_ratio.

    Requires rapidfuzz and a loaded ALIAS_INDEX. We import the bot module
    only here so the simple alias tests above stay zero-dependency.
    """
    print("\n=== 1b. Fuzzy / misspell recognition ===")
    try:
        from telegram_bot import normalize_question, load_aliases, ALIAS_INDEX
    except ImportError as e:
        print(f"  WARNING: Could not import telegram_bot: {e}")
        return

    if not ALIAS_INDEX:
        load_aliases("protocols/aliases.json")
        from telegram_bot import ALIAS_INDEX as AI2
        if not AI2:
            print("  WARNING: ALIAS_INDEX empty after load — skipping")
            return

    for text, expected_display in FUZZY_CASES:
        _, recognized = normalize_question(text)
        got_display = recognized["display"] if recognized else None
        assert_equal(f'fuzzy: "{text[:50]}"', got_display, expected_display)


# ---------------------------------------------------------------------------
# 2. Protocol file path tests (no API)
# ---------------------------------------------------------------------------

# Ensure every protocol_file in aliases.json actually exists on disk
def test_protocol_file_paths():
    print("\n=== 2. Protocol file paths exist ===")
    if not os.path.exists("protocols/aliases.json"):
        print("  WARNING: protocols/aliases.json not found — skipping")
        return
    with open("protocols/aliases.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    for category in ["drugs", "conditions"]:
        for key, item in data.get(category, {}).items():
            pf = item.get("protocol_file", "")
            if pf:
                exists = os.path.exists(pf)
                name = f"{key} → {pf}"
                if exists:
                    ok(name)
                else:
                    fail(name, f"File not found: {pf}")


# ---------------------------------------------------------------------------
# 3. Source label derivation tests (no API)
# ---------------------------------------------------------------------------

SOURCE_LABEL_CASES = [
    ("protocols/tmpsmx.txt",                  "TMP/SMX"),
    ("protocols/ampsul.txt",                  "ampicillin/sulbactam"),
    ("protocols/meropenem.txt",               "meropenem"),
    ("protocols/cap.txt",                     "CAP"),
    ("protocols/pneumonia_pcr.txt",           "BioFire"),
    ("protocols/general_rules_antibiotic_dosing.txt", "General antibiotic dosing rules"),
]


def test_source_label_derivation():
    print("\n=== 3. Source label derivation ===")
    for path, expected in SOURCE_LABEL_CASES:
        got = derive_source_label(path)
        assert_equal(f"label for {path}", got, expected)


# ---------------------------------------------------------------------------
# 4. clean_response post-processing tests (no API)
# ---------------------------------------------------------------------------

CLEAN_CASES = [
    {
        "name": "strips markdown bold",
        "input": "Give **3 x 4 amp** IV",
        "source": "TMP/SMX",
        "must_contain": "3 x 4 amp",
        "must_not_contain": "**",
    },
    {
        "name": "removes model-generated Source line",
        "input": "Give 3 x 4 amp\n\nSource: protocols/medical/antibiotics/tmpsmx.txt",
        "source": "TMP/SMX",
        "must_contain": "Source: TMP/SMX",
        "must_not_contain": "protocols/",
    },
    {
        "name": "removes stray file path",
        "input": "See protocols/medical/tmpsmx.txt for details",
        "source": "TMP/SMX",
        "must_not_contain": "protocols/",
    },
    {
        "name": "removes 'not specified' when dosing is present",
        "input": "Give 500 mg IV\nThis is not specified in the uploaded protocol.",
        "source": "meropenem",
        "must_contain": "500 mg",
        "must_not_contain": "not specified",
    },
    {
        "name": "keeps 'not specified' when no dosing in text",
        "input": "This is not specified in the uploaded protocol.",
        "source": None,
        "must_contain": "not specified",
    },
    {
        "name": "appends source at end",
        "input": "Give 4 g/day meropenem.",
        "source": "meropenem",
        "must_contain": "Source: meropenem",
    },
    {
        "name": "removes Hungarian Forrás line",
        "input": "3 x 4 amp IV\nForrás: protocols/tmpsmx.txt",
        "source": "TMP/SMX",
        "must_not_contain": "protocols/",
        "must_contain": "Source: TMP/SMX",
    },
    {
        "name": "collapses excessive blank lines",
        "input": "Line 1\n\n\n\n\nLine 2",
        "source": None,
        "must_contain": "Line 1\n\nLine 2",
    },
]


def test_clean_response():
    print("\n=== 4. clean_response post-processing ===")
    for case in CLEAN_CASES:
        result = clean_response(case["input"], case.get("source"))
        if "must_contain" in case:
            assert_contains(f'{case["name"]} — contains', result, case["must_contain"])
        if "must_not_contain" in case:
            assert_not_contains(f'{case["name"]} — not contains', result, case["must_not_contain"])


# ---------------------------------------------------------------------------
# 4b. Policy-header extraction (no API)
# Verifies extract_policy_header() parses ## ANSWER_POLICY / DEFAULT_QUESTION
# / REQUIRED_INFORMATION / PATHWAY_PRIORITY out of a protocol file and
# discards other sections. This is what gets injected into the LLM context
# when an alias-matched protocol is recognized.
# ---------------------------------------------------------------------------

def test_policy_header_extraction():
    print("\n=== 4b. Policy-header extraction ===")
    try:
        from telegram_bot import extract_policy_header
    except ImportError as e:
        print(f"  WARNING: Could not import telegram_bot: {e}")
        return

    cap_path = "protocols/cap.txt"
    if not os.path.exists(cap_path):
        print(f"  WARNING: {cap_path} not found — skipping")
        return

    with open(cap_path, "r", encoding="utf-8") as f:
        text = f.read()

    header = extract_policy_header(text)

    assert_contains("cap.txt header has ANSWER_POLICY",       header, "## ANSWER_POLICY")
    assert_contains("cap.txt header has DEFAULT_QUESTION",    header, "## DEFAULT_QUESTION")
    assert_contains("cap.txt header has REQUIRED_INFORMATION", header, "## REQUIRED_INFORMATION")
    assert_contains("cap.txt header has PATHWAY_PRIORITY",    header, "## PATHWAY_PRIORITY")
    # And explicitly does NOT include the treatment-pathway sections —
    # those should come in via semantic search only, after the gate clears.
    assert_not_contains("cap.txt header excludes OUTPATIENT_DISCHARGEABLE_CAP",
                        header, "## OUTPATIENT_DISCHARGEABLE_CAP")
    assert_not_contains("cap.txt header excludes HOSPITALIZED_CAP_NON_INTUBATED",
                        header, "## HOSPITALIZED_CAP_NON_INTUBATED")
    assert_not_contains("cap.txt header excludes INTUBATED_CAP",
                        header, "## INTUBATED_CAP")


# ---------------------------------------------------------------------------
# 5. Integration tests (require OPENAI_API_KEY + protocol files)
# ---------------------------------------------------------------------------

# Each case: (description, user_message, list_of_expected_fragments_in_output)
# Fragments are case-insensitive substrings that MUST appear in the answer.
INTEGRATION_CASES = [
    # Missing info → bot should ask for it
    (
        "TMP/SMX: missing all params → ask for them",
        "What's the dose of sumetrolim?",
        ["indication", "weight", "renal"],  # must ask for these
    ),
    (
        "TMP/SMX: Steno BSI 60kg GFR>30 → give high dose",
        "Stenotrophomonas BSI, 60 kg, GFR 60",
        ["3", "4", "amp"],  # 3 x 4 amp per high-dose table
    ),
    (
        "TMP/SMX: Hungarian synonym Steno HK pozitív",
        "Steno HK pozitív, 60 kg, GFR 60",
        ["3", "4", "amp"],
    ),
    (
        "TMP/SMX: source is TMP/SMX not a file path",
        "sumetrolim forte, PCP treatment, 70 kg, GFR 50",
        ["Source: TMP/SMX"],
    ),
    (
        "Meropenem: no info → provide MAGAS default",
        "meropenem dose",
        ["4 g", "magas"],
    ),
    (
        "Meropenem: septic shock → MAGAS",
        "meropenem, septic shock, GFR 70",
        ["4 g"],
    ),
    (
        "CAP: intubated not specified → ask for it",
        "CAP treatment",
        ["intubated"],
    ),
    # Patient status MUST be asked before pathway info. Verifies the
    # policy-header injection works: even though semantic search returns
    # treatment-pathway chunks, the ANSWER_POLICY block is always in context.
    (
        "CAP: 'mit adjak' without patient status → asks DEFAULT_QUESTION",
        "Pneumoniara mit adjak?",
        ["hazaengedhető"],   # part of the DEFAULT_QUESTION wording
    ),
    # Same, with misspell — exercises the fuzzy fix AND policy injection together
    (
        "CAP: misspelled 'penumoniara' → recognized + asks status",
        "Penumoniara mit adjak?",
        ["hazaengedhető"],
    ),
    (
        "Source never contains file path",
        "sumetrolim dose, PCP, 70kg, GFR 50",
        [],  # checked separately below
    ),
]


def test_integration():
    print("\n=== 5. Integration tests (OpenAI API) ===")
    try:
        from telegram_bot import ask_ai, load_rule_files, load_aliases, load_protocols, PROTOCOL_CHUNKS
    except ImportError as e:
        print(f"  WARNING: Could not import telegram_bot: {e}")
        print("  Make sure you run this from the bot's root folder with all dependencies installed.")
        return

    # Load everything once
    if not PROTOCOL_CHUNKS:
        print("  Loading rule files, aliases, and protocols...")
        load_rule_files()
        load_aliases("protocols/aliases.json")
        load_protocols()

    FAKE_CHAT_ID = 99999  # dummy chat id for tests

    for description, user_message, expected_fragments in INTEGRATION_CASES:
        try:
            answer = ask_ai(user_message, FAKE_CHAT_ID)

            # Check expected fragments
            all_ok = True
            for fragment in expected_fragments:
                if fragment.lower() not in answer.lower():
                    fail(f"{description} — missing '{fragment}'", f"Answer was: {answer[:200]}")
                    all_ok = False

            # Always check: no file paths in output
            if "protocols/" in answer.lower():
                fail(f"{description} — file path leaked into output", answer[:200])
                all_ok = False

            # Always check: source not at the top
            lines = answer.strip().splitlines()
            if lines and lines[0].lower().startswith("source:"):
                fail(f"{description} — source appeared at top", lines[0])
                all_ok = False

            if all_ok and expected_fragments:
                ok(description)
            elif all_ok and not expected_fragments:
                ok(f"{description} (no file paths, source at bottom)")

        except Exception as e:
            fail(description, str(e))

        # Reset state between test cases to avoid cross-contamination
        from telegram_bot import CONVERSATION_STATE
        CONVERSATION_STATE.pop(FAKE_CHAT_ID, None)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_integration = "--all" in sys.argv

    test_alias_recognition()
    test_fuzzy_recognition()
    test_protocol_file_paths()
    test_source_label_derivation()
    test_clean_response()
    test_policy_header_extraction()

    if run_integration:
        test_integration()
    else:
        print("\n=== 5. Integration tests ===")
        print("  (skipped — run with --all to include API tests)")

    total = results["pass"] + results["fail"]
    print(f"\n{'='*40}")
    print(f"Results: {results['pass']}/{total} passed", end="")
    if results["fail"]:
        print(f"  ({results['fail']} FAILED)")
        sys.exit(1)
    else:
        print("  ✓ all passed")
