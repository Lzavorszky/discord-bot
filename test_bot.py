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
# Stub heavy production dependencies that are not needed for pure-Python
# tests (schema parser, tree parser, footer helpers, dispatcher smoke).
# This lets sections 6–10 import telegram_bot and call its pure functions
# without numpy / openai / rapidfuzz being installed.
#
# The stubs are injected into sys.modules BEFORE any `from telegram_bot`
# import so the module-level code in telegram_bot.py never trips on them.
# Functions that actually call openai or numpy at runtime will still fail
# if invoked — but none of the fast tests do that.
# ---------------------------------------------------------------------------

def _stub_missing_deps():
    from unittest.mock import MagicMock
    _HEAVY = [
        "numpy",
        "openai",
        "rapidfuzz",
        "rapidfuzz.fuzz",
        "rapidfuzz.process",
        "telegram",
        "telegram.ext",
        "telebot",
    ]
    for mod in _HEAVY:
        if mod not in sys.modules:
            try:
                __import__(mod)
            except ImportError:
                stub = MagicMock()
                # numpy.array and numpy.dot are called at module level in some
                # versions; make them return something falsy but not error.
                stub.array = MagicMock(return_value=[])
                stub.dot   = MagicMock(return_value=0.0)
                sys.modules[mod] = stub

_stub_missing_deps()

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
# 6. Schema parser tests (no API)
# ---------------------------------------------------------------------------

_SAMPLE_FULL = """\
# SAMPLE

## METADATA

protocol_name: Sample Protocol
source_label: SampleDrug
protocol_file: protocols/sample.txt

## ALIASES

sampledrug

## ANSWER_POLICY

Ask for indication.

## REQUIRED_INFORMATION

indication

## PREFERRED_INFORMATION

body weight

## MODIFIER_INFORMATION

CRRT modifies dose

## DEFAULT_QUESTION

What is the indication?

## PATHWAY_PRIORITY

1. Severe -> HIGH

## DECISION_TREE

(none)

## TREATMENT_PATHWAYS

### HIGH

Give 4 g/day.

## SAFETY_NOTES

Monitor.

## DEFAULT_FOOTER

Check levels.

## PROTOCOL_LINKS

ceftriaxone -> protocols/ceftriaxone.txt via: patient_status, renal_gfr
clarithromycin -> protocols/clarithromycin.txt
"""


def test_schema_parser():
    print("\n=== 6. Schema parser ===")
    try:
        from telegram_bot import _parse_protocol_text
    except ImportError as e:
        print(f"  WARNING: Could not import: {e}")
        return

    # 6a: fully-populated 13-panel file — every field correct
    p = _parse_protocol_text(_SAMPLE_FULL)
    assert_equal("6a: no warnings",                     p["warnings"],                              [])
    assert_equal("6a: metadata.protocol_name",          p["metadata"].get("protocol_name"),         "Sample Protocol")
    assert_equal("6a: metadata.source_label",           p["metadata"].get("source_label"),          "SampleDrug")
    assert_contains("6a: aliases body",                 p["aliases"],                               "sampledrug")
    assert_contains("6a: answer_policy",                p["answer_policy"],                         "indication")
    assert_contains("6a: required_information",         p["required_information"],                  "indication")
    assert_contains("6a: preferred_information",        p["preferred_information"],                 "weight")
    assert_contains("6a: modifier_information",         p["modifier_information"],                  "CRRT")
    assert_contains("6a: default_question",             p["default_question"],                      "indication")
    assert_contains("6a: pathway_priority",             p["pathway_priority"],                      "HIGH")
    assert_equal("6a: decision_tree is None",           p["decision_tree"],                         None)
    assert_contains("6a: treatment_pathways",           p["treatment_pathways"],                    "HIGH")
    assert_contains("6a: safety_notes",                 p["safety_notes"],                          "Monitor")
    assert_equal("6a: default_footer",                  p["default_footer"],                        "Check levels.")
    assert_equal("6a: no free_form panels",             p["free_form"],                             {})
    pl = p.get("protocol_links", {})
    assert_equal("6a: links ceftriaxone file",          pl.get("ceftriaxone",    {}).get("file"),     "protocols/ceftriaxone.txt")
    assert_equal("6a: links ceftriaxone ctx_keys",      pl.get("ceftriaxone",    {}).get("ctx_keys"), ["patient_status", "renal_gfr"])
    assert_equal("6a: links clarithromycin ctx_keys",   pl.get("clarithromycin", {}).get("ctx_keys"), [])

    # 6b: missing panels → defaults
    p2 = _parse_protocol_text("## METADATA\n\nprotocol_name: Bare\n")
    assert_equal("6b: missing aliases → ''",            p2["aliases"],          "")
    assert_equal("6b: missing default_question → None", p2["default_question"], None)
    assert_equal("6b: missing default_footer → None",   p2["default_footer"],   None)
    assert_equal("6b: missing decision_tree → None",    p2["decision_tree"],    None)
    assert_equal("6b: missing protocol_links → {}",     p2["protocol_links"],   {})

    # 6c: (none) body → empty string / None
    p3 = _parse_protocol_text(
        "## REQUIRED_INFORMATION\n\n(none)\n\n"
        "## DEFAULT_QUESTION\n\n(none)\n\n"
        "## DEFAULT_FOOTER\n\n(none)\n\n"
        "## PROTOCOL_LINKS\n\n(none)\n"
    )
    assert_equal("6c: (none) required_info → ''",      p3["required_information"], "")
    assert_equal("6c: (none) default_question → None", p3["default_question"],     None)
    assert_equal("6c: (none) default_footer → None",   p3["default_footer"],       None)
    assert_equal("6c: (none) protocol_links → {}",     p3["protocol_links"],       {})

    # 6d: unknown ## header → free_form, not in canonical slots
    p4 = _parse_protocol_text("## UNKNOWN_CUSTOM_PANEL\n\nSome content.\n")
    assert_equal("6d: unknown → in free_form",           "UNKNOWN_CUSTOM_PANEL" in p4["free_form"], True)
    assert_contains("6d: free_form body intact",         p4["free_form"]["UNKNOWN_CUSTOM_PANEL"],   "Some content")

    # 6e: cap.txt on disk — regression: no free_form, footer + metadata present
    cap_path = "protocols/cap.txt"
    if os.path.exists(cap_path):
        with open(cap_path, "r", encoding="utf-8") as f:
            cap_text = f.read()
        cap = _parse_protocol_text(cap_text, path=cap_path)
        assert_equal("6e: cap.txt free_form is empty",        cap["free_form"],                         {})
        assert_equal("6e: cap.txt has default_footer",        bool(cap["default_footer"]),              True)
        assert_equal("6e: cap.txt metadata.protocol_name",    bool(cap["metadata"].get("protocol_name")), True)
    else:
        print("  SKIP 6e: protocols/cap.txt not found on disk")


# ---------------------------------------------------------------------------
# 7. Decision-tree parser tests (no API)
# ---------------------------------------------------------------------------

_SIMPLE_TREE = """\
ROOT: ask_status

NODE: ask_status
  TYPE: question
  ASK_HU: Intubált?
  ASK_EN: Intubated?
  BRANCHES:
    yes -> give_high
    no  -> give_std

NODE: give_high
  TYPE: answer
  ANSWER_HU: Adjál 4 g/nap.
  LINK: meropenem, ceftriaxone
  THEN: end

NODE: give_std
  TYPE: answer
  ANSWER_HU: Adjál 2 g/nap.
  THEN: end
"""

_COLLECT_TREE = """\
ROOT: ask_weight

NODE: ask_weight
  TYPE: collect
  ASK_HU: Testsúly?
  COLLECT:
    - name: weight_kg
      type: number
      unit: kg
  NEXT: ask_gfr

NODE: ask_gfr
  TYPE: question
  ASK_HU: GFR?
  BRANCHES:
    normal  -> dose_normal
    reduced -> dose_reduced

NODE: dose_normal
  TYPE: answer
  ANSWER_HU: Normál dózis.
  THEN: end

NODE: dose_reduced
  TYPE: answer
  ANSWER_HU: Csökkentett dózis.
  THEN: end
"""

_BLOCK_SCALAR_TREE = """\
ROOT: only_node

NODE: only_node
  TYPE: answer
  ANSWER_HU: |
    Első sor.
    Második sor.
  THEN: end
"""


def test_decision_tree_parser():
    print("\n=== 7. Decision-tree parser ===")
    try:
        from telegram_bot import parse_decision_tree
    except ImportError as e:
        print(f"  WARNING: Could not import: {e}")
        return

    # 7a: minimal yes/no tree — structure
    t = parse_decision_tree(_SIMPLE_TREE)
    assert_equal("7a: root is ask_status",       t["root"],                                     "ask_status")
    assert_equal("7a: 3 nodes",                  len(t["nodes"]),                               3)
    assert_equal("7a: ask_status type=question", t["nodes"]["ask_status"]["type"],              "question")
    assert_equal("7a: branch yes → give_high",   t["nodes"]["ask_status"]["branches"].get("yes"), "give_high")
    assert_equal("7a: branch no  → give_std",    t["nodes"]["ask_status"]["branches"].get("no"),  "give_std")

    # 7b: LINK: key on answer node — parsed as list
    assert_equal("7b: give_high LINK list",      t["nodes"]["give_high"]["link"], ["meropenem", "ceftriaxone"])
    assert_equal("7b: give_std empty LINK",      t["nodes"]["give_std"]["link"],  [])

    # 7c: terminal THEN: end
    assert_equal("7c: give_high THEN=end",       t["nodes"]["give_high"]["then"], "end")
    assert_equal("7c: give_std  THEN=end",       t["nodes"]["give_std"]["then"],  "end")

    # 7d: COLLECT block — items parsed correctly
    tc = parse_decision_tree(_COLLECT_TREE)
    items = tc["nodes"]["ask_weight"]["collect"]
    assert_equal("7d: 1 collect item",           len(items),                  1)
    assert_equal("7d: collect name=weight_kg",   items[0].get("name"),        "weight_kg")
    assert_equal("7d: collect type=number",      items[0].get("type"),        "number")
    assert_equal("7d: collect unit=kg",          items[0].get("unit"),        "kg")
    assert_equal("7d: NEXT=ask_gfr",             tc["nodes"]["ask_weight"]["next"], "ask_gfr")

    # 7e: block scalar (| notation) — multiline body preserved
    tb = parse_decision_tree(_BLOCK_SCALAR_TREE)
    body = tb["nodes"]["only_node"].get("answer_hu") or ""
    assert_contains("7e: block scalar line 1",   body, "Első sor")
    assert_contains("7e: block scalar line 2",   body, "Második sor")

    # 7f: empty / (none) body → None
    assert_equal("7f: empty body → None",        parse_decision_tree(""),       None)
    assert_equal("7f: (none) body → None",       parse_decision_tree("(none)"), None)


# ---------------------------------------------------------------------------
# 8. Footer helpers — apply_footer / finalize_answer (no API)
# ---------------------------------------------------------------------------

def test_footer_helpers():
    print("\n=== 8. Footer helpers ===")
    try:
        from telegram_bot import apply_footer, finalize_answer
    except ImportError as e:
        print(f"  WARNING: Could not import: {e}")
        return

    # 8a: footer appended when not present in body
    r = apply_footer("Give 4 g/day.", "Check K+ after 48h.")
    assert_contains("8a: footer appended",      r, "Check K+ after 48h.")
    assert_contains("8a: original body intact", r, "Give 4 g/day.")

    # 8b: footer NOT duplicated when already in body
    body_with = "Give 4 g/day.\n\nCheck K+ after 48h."
    r2 = apply_footer(body_with, "Check K+ after 48h.")
    assert_equal("8b: no duplication", r2.count("Check K+ after 48h."), 1)

    # 8c: None footer → body returned unchanged
    r3 = apply_footer("Give 4 g/day.", None)
    assert_equal("8c: None footer → no change", r3, "Give 4 g/day.")

    # 8d: finalize_answer — footer appears before Source line
    full = finalize_answer("Give 4 g/day.", "Check K+ after 48h.", "meropenem")
    lines = full.splitlines()
    footer_idx = next((i for i, l in enumerate(lines) if "Check K+" in l), -1)
    source_idx  = next((i for i, l in enumerate(lines) if "Source:"  in l), -1)
    assert_equal("8d: footer before Source",         footer_idx < source_idx, True)

    # 8e: finalize_answer — None footer still produces Source line
    full2 = finalize_answer("Give 4 g/day.", None, "meropenem")
    assert_contains("8e: Source line present", full2, "Source: meropenem")
    assert_not_contains("8e: no literal None", full2, "None")


# ---------------------------------------------------------------------------
# 9. Cross-protocol links tests (no API)
# ---------------------------------------------------------------------------

def test_protocol_links():
    print("\n=== 9. Cross-protocol links ===")
    try:
        from telegram_bot import (
            _parse_protocol_links,
            _render_link_offer,
            _maybe_attach_links,
            _is_link_batchable,
            PROTOCOL_PARSED_BY_FILE,
            normalize_path,
        )
    except ImportError as e:
        print(f"  WARNING: Could not import: {e}")
        return

    # 9a: _parse_protocol_links — via: parsing, comment/bad-line skipping
    pl = _parse_protocol_links(
        "ceftriaxone -> protocols/ceftriaxone.txt via: patient_status, renal_gfr\n"
        "clarithromycin -> protocols/clarithromycin.txt\n"
        "# comment\n"
        "bad line without arrow\n"
    )
    assert_equal("9a: 2 entries parsed",              len(pl),                                   2)
    assert_equal("9a: ceftriaxone file",              pl["ceftriaxone"]["file"],                 "protocols/ceftriaxone.txt")
    assert_equal("9a: ceftriaxone ctx_keys",          pl["ceftriaxone"]["ctx_keys"],             ["patient_status", "renal_gfr"])
    assert_equal("9a: clarithromycin ctx_keys empty", pl["clarithromycin"]["ctx_keys"],          [])

    # 9b: empty / (none) body → empty dict
    assert_equal("9b: empty body → {}",   _parse_protocol_links(""),       {})
    assert_equal("9b: (none) body → {}",  _parse_protocol_links("(none)"), {})

    # 9c: _render_link_offer
    assert_equal("9c: single HU",
                 _render_link_offer(["ceftriaxone"], "hu"),
                 "Kell dózis? → ceftriaxone")
    assert_equal("9c: two EN",
                 _render_link_offer(["ceftriaxone", "clarithromycin"], "en"),
                 "Need dosing? → ceftriaxone / clarithromycin / both")
    assert_contains("9c: two HU has mindkettő",
                    _render_link_offer(["a", "b"], "hu"), "mindkettő")

    # 9d: _maybe_attach_links — node with no LINK: → body and state unchanged
    st_d = {"tree": {"collected": {}}, "pending_links": None}
    node_d = {"type": "answer", "link": [], "then": "end"}
    r_d = _maybe_attach_links(st_d, node_d, {}, "Original body.", "hu")
    assert_equal("9d: no link → body unchanged",           r_d,                      "Original body.")
    assert_equal("9d: no link → pending_links still None", st_d["pending_links"],    None)

    # 9e: _maybe_attach_links — with LINK: → offer appended, forwarded context captured
    fake_parsed = {
        "protocol_links": {
            "ceftriaxone":    {"file": "protocols/ceftriaxone.txt",    "ctx_keys": ["renal_gfr"]},
            "clarithromycin": {"file": "protocols/clarithromycin.txt", "ctx_keys": []},
        }
    }
    st_e = {"tree": {"collected": {"renal_gfr": "35"}}, "pending_links": None}
    node_e = {"type": "answer", "link": ["ceftriaxone", "clarithromycin"], "then": "end"}
    r_e = _maybe_attach_links(st_e, node_e, fake_parsed, "Give CRO + CLR.", "hu")
    assert_contains("9e: body preserved",        r_e,                          "Give CRO + CLR.")
    assert_contains("9e: offer appended",        r_e,                          "Kell dózis?")
    assert_contains("9e: mindkettő present",     r_e,                          "mindkettő")
    assert_equal("9e: pending_links has 2",      len(st_e["pending_links"]),   2)
    cro = next(e for e in st_e["pending_links"] if e["label"] == "ceftriaxone")
    clr = next(e for e in st_e["pending_links"] if e["label"] == "clarithromycin")
    assert_equal("9e: renal_gfr forwarded",      cro["forwarded"].get("renal_gfr"), "35")
    assert_equal("9e: clr forwarded empty",      clr["forwarded"],              {})

    # 9f: label in LINK: but missing from protocol_links → silently skipped
    st_f = {"tree": {"collected": {}}, "pending_links": None}
    node_f = {"type": "answer", "link": ["nonexistent_drug"], "then": "end"}
    r_f = _maybe_attach_links(st_f, node_f, {"protocol_links": {}}, "Body.", "hu")
    assert_equal("9f: unknown label → body unchanged",        r_f,                   "Body.")
    assert_equal("9f: unknown label → pending_links is None", st_f["pending_links"], None)

    # 9g–9i: _is_link_batchable — inject fake parsed entries into PROTOCOL_PARSED_BY_FILE
    _f1 = "protocols/__test_batch1__.txt"
    _f2 = "protocols/__test_batch2__.txt"
    _f3 = "protocols/__test_batch3__.txt"
    PROTOCOL_PARSED_BY_FILE[normalize_path(_f1)] = {"required_information": "",            "decision_tree": None}
    PROTOCOL_PARSED_BY_FILE[normalize_path(_f2)] = {"required_information": "GFR, weight", "decision_tree": None}
    PROTOCOL_PARSED_BY_FILE[normalize_path(_f3)] = {"required_information": "",            "decision_tree": {"root": "x", "nodes": {}}}
    assert_equal("9g: batchable (no req, no tree)",    _is_link_batchable({"file": _f1}), True)
    assert_equal("9h: not batchable (has req info)",   _is_link_batchable({"file": _f2}), False)
    assert_equal("9i: not batchable (has tree)",       _is_link_batchable({"file": _f3}), False)
    for f in [_f1, _f2, _f3]:
        PROTOCOL_PARSED_BY_FILE.pop(normalize_path(f), None)


# ---------------------------------------------------------------------------
# 10. Dispatcher smoke tests — pure state-machine paths, no API
#
# Uses manually injected fake parsed protocols and advances the state
# machine directly to answer nodes (bypassing _classify_branch which
# needs the OpenAI API).
# ---------------------------------------------------------------------------

_SMOKE_TREE_TEXT = """\
ROOT: ask_status

NODE: ask_status
  TYPE: question
  ASK_HU: Intubált?
  ASK_EN: Intubated?
  BRANCHES:
    yes -> give_ceftriaxone
    no  -> give_std

NODE: give_ceftriaxone
  TYPE: answer
  ANSWER_HU: Ceftriaxone 2 g.
  ANSWER_EN: Ceftriaxone 2 g.
  LINK: ceftriaxone
  THEN: end

NODE: give_std
  TYPE: answer
  ANSWER_HU: Standard terápia.
  ANSWER_EN: Standard therapy.
  THEN: end
"""

_SMOKE_CRO_TREE = """\
ROOT: ask_gfr

NODE: ask_gfr
  TYPE: question
  ASK_HU: CRO GFR?
  BRANCHES:
    normal -> done

NODE: done
  TYPE: answer
  ANSWER_HU: 2 g q24h.
  THEN: end
"""


def _smoke_setup(chat_id):
    """Inject fake parsed protocols, return (state, parsed, proto_file)."""
    try:
        from telegram_bot import (
            parse_decision_tree, get_chat_state,
            PROTOCOL_PARSED_BY_FILE, CONVERSATION_STATE, normalize_path,
        )
    except ImportError:
        return None, None, None

    proto_file = "protocols/__smoke_cap__.txt"
    cro_file   = "protocols/__smoke_cro__.txt"

    parsed = {
        "metadata":              {"protocol_name": "Smoke CAP", "source_label": "SmokeCAP",
                                  "protocol_file": proto_file},
        "decision_tree":         parse_decision_tree(_SMOKE_TREE_TEXT),
        "default_footer":        "Smoke footer.",
        "protocol_links":        {"ceftriaxone": {"file": cro_file, "ctx_keys": []}},
        "required_information":  "",
        "preferred_information": "",
        "treatment_pathways":    "",
        "safety_notes":          "",
        "warnings":              [],
        "free_form":             {},
    }
    cro_parsed = {
        "metadata":              {"source_label": "ceftriaxone", "protocol_file": cro_file},
        "decision_tree":         parse_decision_tree(_SMOKE_CRO_TREE),
        "default_footer":        None,
        "protocol_links":        {},
        "required_information":  "",
        "preferred_information": "",
        "treatment_pathways":    "",
        "safety_notes":          "",
        "warnings":              [],
        "free_form":             {},
    }
    PROTOCOL_PARSED_BY_FILE[normalize_path(proto_file)] = parsed
    PROTOCOL_PARSED_BY_FILE[normalize_path(cro_file)]   = cro_parsed

    CONVERSATION_STATE.pop(chat_id, None)
    return get_chat_state(chat_id), parsed, proto_file


def _smoke_recognized(proto_file):
    return {"protocol_file": proto_file, "source_label": "SmokeCAP",
            "display": "SmokeCAP", "canonical": "SmokeCAP"}


def test_dispatcher_smoke():
    print("\n=== 10. Dispatcher smoke tests ===")
    try:
        from telegram_bot import (
            dispatch_tree, init_tree_state, reset_tree_state, advance_tree_state,
            PROTOCOL_PARSED_BY_FILE, CONVERSATION_STATE, normalize_path,
        )
    except ImportError as e:
        print(f"  WARNING: Could not import: {e}")
        return

    # 10a: no recognized protocol → None (fall through to RAG)
    state_a, _, _ = _smoke_setup(88880)
    if state_a is None:
        print("  SKIP 10a-10h: telegram_bot import failed")
        return
    assert_equal("10a: no recognized → None",
                 dispatch_tree(state_a, None, "anything", "anything"), None)

    # 10b: tree init → emits root ASK text
    state_b, parsed_b, pf_b = _smoke_setup(88881)
    rec_b = _smoke_recognized(pf_b)
    result_b = dispatch_tree(state_b, rec_b, "pneumonia?", "pneumonia?")
    assert_equal("10b: current_node=root",   state_b.get("tree", {}).get("current_node"), "ask_status")
    assert_contains("10b: emits root ASK",   result_b or "", "Intubált")

    # 10c: jump to terminal answer node → body returned, tree reset
    state_c, parsed_c, pf_c = _smoke_setup(88882)
    rec_c = _smoke_recognized(pf_c)
    init_tree_state(state_c, parsed_c, rec_c)
    advance_tree_state(state_c, "give_std")
    result_c = dispatch_tree(state_c, rec_c, "nem", "nem")
    assert_contains("10c: answer body returned",  result_c or "", "Standard")
    assert_equal("10c: tree reset after end",     state_c.get("tree"),          None)

    # 10d: answer node with LINK: → offer appended, pending_links set
    state_d, parsed_d, pf_d = _smoke_setup(88883)
    rec_d = _smoke_recognized(pf_d)
    init_tree_state(state_d, parsed_d, rec_d)
    advance_tree_state(state_d, "give_ceftriaxone")
    result_d = dispatch_tree(state_d, rec_d, "igen", "igen")
    assert_contains("10d: answer body present",    result_d or "", "Ceftriaxone")
    assert_contains("10d: link offer appended",    result_d or "", "Kell dózis?")
    assert_equal("10d: pending_links set",         bool(state_d.get("pending_links")), True)

    # 10e: pending_links — user picks label → target tree init, emits its root ASK
    # state_d.pending_links still set from 10d
    result_e = dispatch_tree(state_d, None, "ceftriaxone", "ceftriaxone")
    assert_equal("10e: pending_links cleared",     state_d.get("pending_links"), None)
    assert_contains("10e: target root ASK",        result_e or "",               "CRO GFR")

    # 10f: pending_links — user declines → None returned, offer cleared
    state_f, _, _ = _smoke_setup(88884)
    state_f["pending_links"] = [{"label": "ceftriaxone", "file": "protocols/__smoke_cro__.txt",
                                  "ctx_keys": [], "forwarded": {}}]
    result_f = dispatch_tree(state_f, None, "nem", "nem")
    assert_equal("10f: decline → None",            result_f,                      None)
    assert_equal("10f: pending_links cleared",     state_f.get("pending_links"),  None)

    # 10g: pending_links — unrecognised reply → re-ask with offer
    state_g, _, _ = _smoke_setup(88885)
    state_g["pending_links"] = [{"label": "ceftriaxone", "file": "protocols/__smoke_cro__.txt",
                                  "ctx_keys": [], "forwarded": {}}]
    result_g = dispatch_tree(state_g, None, "something random", "something random")
    assert_contains("10g: re-ask contains offer",  result_g or "", "Kell dózis?")
    assert_equal("10g: pending_links still set",   bool(state_g.get("pending_links")), True)

    # 10h: mid-tree topic switch → switch proposed
    state_h, parsed_h, pf_h = _smoke_setup(88886)
    rec_h = _smoke_recognized(pf_h)
    init_tree_state(state_h, parsed_h, rec_h)
    other_rec = {"protocol_file": "protocols/meropenem.txt", "source_label": "meropenem",
                 "display": "meropenem", "canonical": "meropenem"}
    result_h = dispatch_tree(state_h, other_rec, "meropenem dose", "meropenem dose")
    assert_contains("10h: switch proposed",        result_h or "", "meropenem")
    assert_equal("10h: pending_switch set",        bool(state_h.get("pending_topic_switch")), True)

    # Cleanup
    for cid in range(88880, 88887):
        CONVERSATION_STATE.pop(cid, None)
    for f in ["protocols/__smoke_cap__.txt", "protocols/__smoke_cro__.txt"]:
        PROTOCOL_PARSED_BY_FILE.pop(normalize_path(f), None)


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
    test_schema_parser()
    test_decision_tree_parser()
    test_footer_helpers()
    test_protocol_links()
    test_dispatcher_smoke()

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
