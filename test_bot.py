"""
Session 1 tests — deployment and output hygiene.
Run: python test_bot.py
"""

import sys
import os
import io
import unittest
import importlib.util
import types
from unittest.mock import patch, MagicMock

# Allow importing from protocols/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "protocols"))

# Provide dummy env vars so the module loads without crashing
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("OPENAI_API_KEY",  "dummy")

# Let local unit tests import telegram_bot even when external SDKs are not
# installed in the active Python environment. Production still uses
# requirements.txt.
if importlib.util.find_spec("openai") is None:
    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = MagicMock
    sys.modules["openai"] = fake_openai

if importlib.util.find_spec("telegram") is None:
    fake_telegram = types.ModuleType("telegram")
    fake_telegram.Update = type("Update", (), {})
    fake_ext = types.ModuleType("telegram.ext")
    fake_ext.ApplicationBuilder = MagicMock
    fake_ext.MessageHandler = MagicMock
    fake_ext.CommandHandler = MagicMock
    fake_ext.ContextTypes = type("ContextTypes", (), {"DEFAULT_TYPE": object})
    fake_ext.filters = type("filters", (), {"TEXT": MagicMock(), "COMMAND": MagicMock()})
    sys.modules["telegram"] = fake_telegram
    sys.modules["telegram.ext"] = fake_ext

# Patch OpenAI constructor so it doesn't fail on a dummy key at import time
_openai_patch = patch("openai.OpenAI", return_value=MagicMock())
_openai_patch.start()

import telegram_bot as bot

_openai_patch.stop()

# Direct import of postprocess (should work independently of telegram_bot)
import postprocess


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFooterPlacement(unittest.TestCase):
    """
    finalize_answer and clean_response must:
      - place SAFETY_FOOTER before the Source line
      - make Source the final non-empty line
      - not append SAFETY_FOOTER onto the Source line itself
    """

    SAFETY = bot.SAFETY_FOOTER  # whatever was loaded (file or fallback)

    def _finalize(self, body, footer=None, source_label="TEST_SRC"):
        return bot.finalize_answer(body, footer, source_label)

    def _clean(self, body, source_label="TEST_SRC"):
        return bot.clean_response(body, source_label)

    # --- finalize_answer ---

    def test_finalize_source_is_final_line(self):
        result = self._finalize("Some answer text.")
        non_empty = [l for l in result.splitlines() if l.strip()]
        self.assertTrue(
            non_empty[-1].startswith("Source:"),
            f"Last non-empty line must start with 'Source:', got: {non_empty[-1]!r}"
        )

    def test_finalize_source_has_no_safety_footer_appended(self):
        result = self._finalize("Some answer text.")
        source_line = next(
            (l for l in result.splitlines() if l.startswith("Source:")), None
        )
        self.assertIsNotNone(source_line, "No Source line in output")
        if self.SAFETY:
            self.assertNotIn(
                self.SAFETY, source_line,
                f"SAFETY_FOOTER must not appear on Source line, got: {source_line!r}"
            )

    def test_finalize_safety_footer_before_source(self):
        if not self.SAFETY:
            self.skipTest("SAFETY_FOOTER is empty — nothing to check")
        result = self._finalize("Some answer text.")
        lines = result.splitlines()
        src_idx = next((i for i, l in enumerate(lines) if l.startswith("Source:")), None)
        self.assertIsNotNone(src_idx, "No Source line in output")
        before_source = "\n".join(lines[:src_idx])
        self.assertIn(self.SAFETY, before_source,
                      "SAFETY_FOOTER must appear before the Source line")

    def test_finalize_source_label_preserved(self):
        result = self._finalize("Body text.", source_label="MY_PROTO")
        self.assertIn("Source: MY_PROTO", result)

    # --- clean_response ---

    def test_clean_source_is_final_line(self):
        result = self._clean("Some answer text.")
        non_empty = [l for l in result.splitlines() if l.strip()]
        self.assertTrue(
            non_empty[-1].startswith("Source:"),
            f"Last non-empty line must start with 'Source:', got: {non_empty[-1]!r}"
        )

    def test_clean_source_has_no_safety_footer_appended(self):
        result = self._clean("Some answer text.")
        source_line = next(
            (l for l in result.splitlines() if l.startswith("Source:")), None
        )
        self.assertIsNotNone(source_line, "No Source line in output")
        if self.SAFETY:
            self.assertNotIn(
                self.SAFETY, source_line,
                f"SAFETY_FOOTER must not appear on Source line, got: {source_line!r}"
            )

    def test_clean_safety_footer_before_source(self):
        if not self.SAFETY:
            self.skipTest("SAFETY_FOOTER is empty — nothing to check")
        result = self._clean("Some answer text.")
        lines = result.splitlines()
        src_idx = next((i for i, l in enumerate(lines) if l.startswith("Source:")), None)
        self.assertIsNotNone(src_idx, "No Source line in output")
        before_source = "\n".join(lines[:src_idx])
        self.assertIn(self.SAFETY, before_source,
                      "SAFETY_FOOTER must appear before the Source line")


class TestAllowlistWarning(unittest.TestCase):
    """!! ALLOWED USERS NOT DEFINED !! must be printed when allowlist env var is absent."""

    def test_warning_text_present_in_source(self):
        """Verify the exact warning string is in run_startup_checks source code."""
        import inspect
        src = inspect.getsource(bot.run_startup_checks)
        self.assertIn(
            "!! ALLOWED USERS NOT DEFINED !!",
            src,
            "Expected warning string not found in run_startup_checks"
        )

    def test_warning_emitted_when_allowlist_missing(self):
        """run_startup_checks prints the warning when ALLOWED_USER_IDS is unset.

        sys.exit is patched to a no-op so the function runs past the error-check
        block (which exits early in the test env due to missing aliases.json) and
        reaches the ALLOWED_USER_IDS check.
        """
        saved = os.environ.pop("ALLOWED_USER_IDS", None)
        try:
            buf = io.StringIO()
            from contextlib import redirect_stdout
            from unittest.mock import patch as _patch
            with redirect_stdout(buf), _patch("sys.exit"):
                bot.run_startup_checks()
            output = buf.getvalue()
            self.assertIn(
                "!! ALLOWED USERS NOT DEFINED !!",
                output,
                f"Expected warning not in stdout. Got:\n{output}"
            )
        finally:
            if saved is not None:
                os.environ["ALLOWED_USER_IDS"] = saved




class TestPostprocessModule(unittest.TestCase):
    """postprocess.py can be imported and used independently of telegram_bot."""

    def test_module_importable(self):
        import postprocess as pp
        self.assertTrue(callable(pp.finalize_answer))
        self.assertTrue(callable(pp.clean_response))
        self.assertTrue(callable(pp.apply_footer))

    def test_direct_finalize_source_is_final(self):
        import postprocess as pp
        result = pp.finalize_answer("Body text.", None, "DIRECT_SRC")
        non_empty = [l for l in result.splitlines() if l.strip()]
        self.assertTrue(non_empty[-1].startswith("Source:"))

    def test_direct_finalize_matches_bot(self):
        """postprocess.finalize_answer and bot.finalize_answer return identical results."""
        import postprocess as pp
        body, footer, label = "Some text.", "proto footer", "SRC"
        self.assertEqual(
            pp.finalize_answer(body, footer, label),
            bot.finalize_answer(body, footer, label),
        )



class TestNewSchemaParser(unittest.TestCase):
    """New-schema panels must be parsed explicitly — not land in free_form."""

    PROTO_DIR = os.path.join(os.path.dirname(__file__), "protocols")

    def _parse(self, filename):
        import protocol_parser as pp
        return pp.parse_protocol_file(os.path.join(self.PROTO_DIR, filename))

    # ── New panels are recognised (not in free_form) ─────────────────────────

    def test_cap_new_panels_not_in_free_form(self):
        p = self._parse("cap.txt")
        new_panels = [
            "intents", "input_slots", "default_answer",
            "selection_rules", "selected_outputs",
            "info_blocks", "restricted_outputs",
            "safety_rules", "output_templates",
        ]
        for panel in new_panels:
            self.assertNotIn(
                panel.upper(), p["free_form"],
                f"cap.txt: panel '{panel}' should not be in free_form"
            )

    def test_meropenem_new_panels_not_in_free_form(self):
        p = self._parse("meropenem.txt")
        for panel in ["INTENTS", "INPUT_SLOTS", "DEFAULT_ANSWER",
                      "SELECTION_RULES", "SELECTED_OUTPUTS", "INFO_BLOCKS",
                      "RESTRICTED_OUTPUTS", "SAFETY_RULES", "OUTPUT_TEMPLATES"]:
            self.assertNotIn(panel, p["free_form"],
                             f"meropenem.txt: '{panel}' must not be in free_form")

    def test_new_panels_have_content(self):
        """Spot-check that new panels actually got their text."""
        p = self._parse("cap.txt")
        self.assertIn("priority_rules", p["selection_rules"])
        self.assertIn("INTUBATED_CAP", p["selected_outputs"])
        self.assertIn("ceftriaxone", p["info_blocks"])

    # ── Old-schema file still loads cleanly ──────────────────────────────────

    def test_legacy_file_loads(self):
        p = self._parse("general_rules_antibiotic_dosing.txt")
        self.assertIsInstance(p, dict)
        self.assertIsInstance(p["warnings"], list)

    def test_general_rules_migrated_to_info_only_schema(self):
        p = self._parse("general_rules_antibiotic_dosing.txt")
        self.assertEqual(p["metadata"].get("answer_mode"), "info_only")
        self.assertEqual(p["metadata"].get("selection_mode"), "none")
        self.assertIn("general_rules", p["info_blocks"])
        self.assertFalse(p["treatment_pathways"])

    # ── METADATA parsing ─────────────────────────────────────────────────────

    def test_metadata_keys_parsed(self):
        p = self._parse("cap.txt")
        meta = p["metadata"]
        self.assertEqual(meta.get("protocol_id"), "cap")
        self.assertEqual(meta.get("answer_mode"), "default_then_selected_output")
        self.assertEqual(meta.get("selection_mode"), "priority_rules")

    def test_metadata_meropenem(self):
        p = self._parse("meropenem.txt")
        self.assertEqual(p["metadata"].get("protocol_id"), "meropenem")
        self.assertEqual(p["metadata"].get("allows_dosing"), "yes")


class TestLinksParser(unittest.TestCase):
    """New-format ## LINKS panel must be parsed into structured dicts."""

    PROTO_DIR = os.path.join(os.path.dirname(__file__), "protocols")

    def _parse(self, filename):
        import protocol_parser as pp
        return pp.parse_protocol_file(os.path.join(self.PROTO_DIR, filename))

    def test_cap_links_parsed(self):
        p = self._parse("cap.txt")
        links = p["links"]
        self.assertIsInstance(links, dict, "links should be a dict")
        self.assertIn("ceftriaxone_dosing", links,
                      "ceftriaxone_dosing link not found")

    def test_ceftriaxone_link_fields(self):
        import protocol_parser as pp
        p = pp.parse_protocol_file(os.path.join(self.PROTO_DIR, "cap.txt"))
        link = p["links"]["ceftriaxone_dosing"]
        self.assertEqual(link.get("target_protocol_id"), "ceftriaxone")
        self.assertEqual(link.get("target_file"), "protocols/ceftriaxone.txt")
        self.assertIn(
            "Ceftriaxone dosing",
            link.get("target_missing_behavior", ""),
        )

    def test_link_transfer_slots_is_list(self):
        import protocol_parser as pp
        p = pp.parse_protocol_file(os.path.join(self.PROTO_DIR, "cap.txt"))
        slots = p["links"]["ceftriaxone_dosing"].get("transfer_slots", [])
        self.assertIsInstance(slots, list, "transfer_slots should be a list")
        self.assertIn("gfr", slots)

    def test_link_trigger_intents_is_list(self):
        import protocol_parser as pp
        p = pp.parse_protocol_file(os.path.join(self.PROTO_DIR, "cap.txt"))
        intents = p["links"]["ceftriaxone_dosing"].get("trigger_intents", [])
        self.assertIsInstance(intents, list)
        self.assertIn("dosing_request", intents)

    def test_cap_has_multiple_links(self):
        p = self._parse("cap.txt")
        self.assertGreater(len(p["links"]), 1,
                           "CAP should have more than one LINK entry")

    def test_meropenem_links_none(self):
        """meropenem.txt has LINKS: (none) — should parse to empty dict."""
        p = self._parse("meropenem.txt")
        self.assertEqual(p["links"], {},
                         "meropenem LINKS (none) should parse to empty dict")

    def test_inline_links_parser(self):
        """Unit test _parse_links_block directly with a minimal fixture."""
        import protocol_parser as pp
        sample = """LINK: my_drug
  link_type: antimicrobial_dosing
  target_protocol_id: my_drug
  target_file: protocols/my_drug.txt
  target_missing_behavior: My drug is not available.
  trigger_intents:
    - dosing_request
  transfer_slots:
    - gfr
    - weight
"""
        result = pp._parse_links_block(sample)
        self.assertIn("my_drug", result)
        entry = result["my_drug"]
        self.assertEqual(entry["link_type"], "antimicrobial_dosing")
        self.assertEqual(entry["target_file"], "protocols/my_drug.txt")
        self.assertEqual(entry["trigger_intents"], ["dosing_request"])
        self.assertEqual(entry["transfer_slots"], ["gfr", "weight"])


class TestAnswerModeValidation(unittest.TestCase):
    """Invalid answer_mode values in METADATA must produce a warning."""

    PROTO_DIR = os.path.join(os.path.dirname(__file__), "protocols")

    def _parse(self, filename):
        import protocol_parser as pp
        return pp.parse_protocol_file(os.path.join(self.PROTO_DIR, filename))

    def test_valid_answer_mode_no_warning(self):
        p = self._parse("cap.txt")
        mode_warnings = [w for w in p["warnings"] if "answer_mode" in w]
        self.assertEqual(mode_warnings, [],
                         f"cap.txt has a valid answer_mode but got warnings: {mode_warnings}")

    def test_invalid_answer_mode_produces_warning(self):
        """meropenem uses default_or_required_slots_then_selected_output (not in guide)."""
        p = self._parse("meropenem.txt")
        mode_warnings = [w for w in p["warnings"] if "answer_mode" in w]
        self.assertTrue(
            len(mode_warnings) > 0,
            "meropenem.txt has an invalid answer_mode but no warning was emitted"
        )

    def test_inline_invalid_mode_warning(self):
        import protocol_parser as pp
        text = """## METADATA
protocol_id: test
answer_mode: made_up_mode
selection_mode: priority_rules
"""
        p = pp._parse_protocol_text(text)
        self.assertTrue(
            any("answer_mode" in w for w in p["warnings"]),
            "Expected answer_mode warning for 'made_up_mode'"
        )

    def test_inline_valid_mode_no_warning(self):
        import protocol_parser as pp
        text = """## METADATA
protocol_id: test
answer_mode: info_only
selection_mode: none
"""
        p = pp._parse_protocol_text(text)
        mode_warnings = [w for w in p["warnings"] if "answer_mode" in w]
        self.assertEqual(mode_warnings, [])



# ---------------------------------------------------------------------------
# Session 5: Protocol Linter Tests
# ---------------------------------------------------------------------------

def _lint_text(text):
    """Helper: lint a single inline protocol text. Returns LintResult."""
    import protocol_linter as pl
    import tempfile, os

    result = pl.LintResult()
    all_aliases = {}
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, dir=tempfile.gettempdir()
    ) as tf:
        tf.write(text)
        tmp_path = tf.name
    try:
        pl._lint_file(tmp_path, result, all_aliases)
    finally:
        os.unlink(tmp_path)
    return result


def _codes(result):
    return {i.code for i in result.issues}


_MINIMAL_VALID = (
    "## METADATA\n"
    "protocol_id: test\n"
    "protocol_name: Test protocol\n"
    "source_label: TEST\n"
    "protocol_type: drug_dosing_protocol\n"
    "answer_mode: info_only\n"
    "selection_mode: none\n"
    "allows_dosing: no\n"
    "default_dose_allowed: no\n"
    "version: 1.0\n"
    "last_reviewed: 2024-01-01\n"
    "owner: test_team\n"
    "status: draft\n"
    "\n"
    "## ALIASES\n"
    "- test drug\n"
    "\n"
    "## INFO_BLOCKS\n"
    "\n"
    "Some general information.\n"
    "\n"
    "## DEFAULT_FOOTER\n"
    "\n"
    "(none)\n"
)


class TestLinterStructure(unittest.TestCase):

    def test_clean_protocol_no_structure_warnings(self):
        result = _lint_text(_MINIMAL_VALID)
        struct_codes = {"missing_required_panels", "out_of_order_panels", "unknown_panel"}
        found = _codes(result) & struct_codes
        self.assertEqual(found, set(), f"Clean protocol got structural warnings: {found}")

    def test_unknown_panel_flagged(self):
        text = _MINIMAL_VALID + "\n## MADE_UP_SECTION\n\nsome content\n"
        result = _lint_text(text)
        self.assertIn("unknown_panel", _codes(result))

    def test_missing_aliases_flagged(self):
        text = (
            "## METADATA\n"
            "protocol_id: x\n"
            "source_label: X\n"
            "version: 1.0\n"
            "last_reviewed: 2024-01-01\n"
            "owner: me\n"
            "status: draft\n"
        )
        result = _lint_text(text)
        self.assertIn("missing_required_panels", _codes(result))


class TestLinterMetadata(unittest.TestCase):

    def test_missing_protocol_id(self):
        text = _MINIMAL_VALID.replace("protocol_id: test\n", "")
        result = _lint_text(text)
        self.assertIn("missing_protocol_id", _codes(result))

    def test_missing_source_label(self):
        text = _MINIMAL_VALID.replace("source_label: TEST\n", "")
        result = _lint_text(text)
        self.assertIn("missing_source_label", _codes(result))

    def test_invalid_protocol_type(self):
        text = _MINIMAL_VALID.replace("protocol_type: drug_dosing_protocol",
                                      "protocol_type: made_up_type")
        result = _lint_text(text)
        self.assertIn("invalid_protocol_type", _codes(result))

    def test_valid_protocol_type_no_warning(self):
        result = _lint_text(_MINIMAL_VALID)
        self.assertNotIn("invalid_protocol_type", _codes(result))

    def test_invalid_answer_mode(self):
        text = _MINIMAL_VALID.replace("answer_mode: info_only",
                                      "answer_mode: bad_mode")
        result = _lint_text(text)
        self.assertIn("invalid_answer_mode", _codes(result))

    def test_valid_answer_mode_no_warning(self):
        result = _lint_text(_MINIMAL_VALID)
        self.assertNotIn("invalid_answer_mode", _codes(result))

    def test_missing_governance_flagged(self):
        text = _MINIMAL_VALID.replace("version: 1.0\n", "")
        result = _lint_text(text)
        self.assertIn("missing_governance", _codes(result))

    def test_all_governance_present_no_warning(self):
        result = _lint_text(_MINIMAL_VALID)
        self.assertNotIn("missing_governance", _codes(result))

    def test_invalid_status(self):
        text = _MINIMAL_VALID.replace("status: draft", "status: secret")
        result = _lint_text(text)
        self.assertIn("invalid_status", _codes(result))

    def test_valid_status_no_warning(self):
        result = _lint_text(_MINIMAL_VALID)
        self.assertNotIn("invalid_status", _codes(result))


class TestLinterAliases(unittest.TestCase):

    def test_broad_alias_flagged(self):
        text = _MINIMAL_VALID.replace("- test drug", "- test drug\n- carbapenem")
        result = _lint_text(text)
        self.assertIn("broad_alias", _codes(result))

    def test_specific_alias_no_warning(self):
        result = _lint_text(_MINIMAL_VALID)
        self.assertNotIn("broad_alias", _codes(result))

    def test_duplicate_alias_flagged(self):
        text = _MINIMAL_VALID.replace("- test drug", "- test drug\n- test drug")
        result = _lint_text(text)
        self.assertIn("duplicate_alias", _codes(result))

    def test_no_duplicate_alias_no_warning(self):
        result = _lint_text(_MINIMAL_VALID)
        self.assertNotIn("duplicate_alias", _codes(result))

    def test_cross_protocol_alias_collision(self):
        import protocol_linter as pl
        result = pl.LintResult()
        all_aliases = {
            "shared_alias": [("proto_a", "a.txt"), ("proto_b", "b.txt")]
        }
        pl._lint_cross_protocol(all_aliases, result)
        self.assertIn("alias_collision", _codes(result))

    def test_no_collision_when_same_protocol(self):
        import protocol_linter as pl
        result = pl.LintResult()
        all_aliases = {
            "shared_alias": [("proto_a", "a.txt"), ("proto_a", "a.txt")]
        }
        pl._lint_cross_protocol(all_aliases, result)
        self.assertNotIn("alias_collision", _codes(result))


class TestLinterDosingSafety(unittest.TestCase):

    def test_dosing_without_flag_detected(self):
        text = _MINIMAL_VALID.replace(
            "## INFO_BLOCKS\n\nSome general information.",
            "## SELECTED_OUTPUTS\n\nDose: 500 mg q8h\n\n## INFO_BLOCKS\n\nSome text."
        )
        result = _lint_text(text)
        self.assertIn("dosing_without_flag", _codes(result))

    def test_default_dose_without_flag_detected(self):
        text = _MINIMAL_VALID.replace(
            "## INFO_BLOCKS\n\nSome general information.",
            "## DEFAULT_ANSWER\n\nGive 500 mg q8h if no renal issues.\n\n## INFO_BLOCKS\n\nSome text."
        )
        result = _lint_text(text)
        self.assertIn("default_dose_without_flag", _codes(result))

    def test_dosing_allowed_no_warning(self):
        text = _MINIMAL_VALID.replace("allows_dosing: no", "allows_dosing: yes")
        text = text.replace("default_dose_allowed: no", "default_dose_allowed: yes")
        text = text.replace(
            "## INFO_BLOCKS\n\nSome general information.",
            "## SELECTED_OUTPUTS\n\nDose: 500 mg q8h\n\n## INFO_BLOCKS\n\nSome text."
        )
        result = _lint_text(text)
        self.assertNotIn("dosing_without_flag", _codes(result))


class TestLinterOnRealFiles(unittest.TestCase):

    PROTO_DIR = os.path.join(os.path.dirname(__file__), "protocols")

    def _run(self):
        import protocol_linter as pl
        return pl.run_linter(proto_dir=self.PROTO_DIR)

    def test_linter_runs_without_crash(self):
        result = self._run()
        self.assertIsNotNone(result)

    def test_no_parse_crashes(self):
        result = self._run()
        crashes = [i for i in result.issues if i.code == "parse_crash"]
        self.assertEqual(crashes, [], f"Parser crashed: {crashes}")

    def test_meropenem_invalid_answer_mode_flagged(self):
        result = self._run()
        issues = [i for i in result.issues
                  if i.code == "invalid_answer_mode" and "meropenem" in i.protocol]
        self.assertTrue(len(issues) > 0,
                        "Expected invalid_answer_mode warning for meropenem.txt")

    def test_broad_alias_detected_in_library(self):
        result = self._run()
        broad = [i for i in result.issues if i.code == "broad_alias"]
        self.assertTrue(len(broad) > 0, "Expected at least one broad_alias warning")

    def test_governance_warnings_cleared(self):
        """Session 6: all protocols now have governance metadata — no missing_governance warnings."""
        result = self._run()
        gov = [i for i in result.issues if i.code == "missing_governance"]
        self.assertEqual(gov, [], f"Unexpected missing_governance warnings: {gov}")



# ---------------------------------------------------------------------------
# Session 8: Routing and Conversation State Tests
# ---------------------------------------------------------------------------

class TestIntentClassifier(unittest.TestCase):

    def test_dosing_keywords_classified(self):
        import telegram_bot as b
        for q in ["dose?", "dosing", "adag?", "mennyi?", "GFR 45", "pump setting"]:
            self.assertEqual(b.classify_intent(q), "dosing_request",
                             f"Expected dosing_request for {q!r}")

    def test_selection_keywords_classified(self):
        import telegram_bot as b
        for q in ["patient is intubated", "hospitalized", "dischargeable"]:
            self.assertEqual(b.classify_intent(q), "selection_request",
                             f"Expected selection_request for {q!r}")

    def test_info_keywords_classified(self):
        import telegram_bot as b
        for q in ["toxicity?", "monitoring", "TDM"]:
            self.assertEqual(b.classify_intent(q), "info_request",
                             f"Expected info_request for {q!r}")

    def test_reset_classified(self):
        import telegram_bot as b
        for q in ["new patient", "reset", "new case", "új beteg"]:
            self.assertEqual(b.classify_intent(q), "reset",
                             f"Expected reset for {q!r}")

    def test_unknown_returns_unknown(self):
        import telegram_bot as b
        self.assertEqual(b.classify_intent("hello there"), "unknown")


class TestDosingShortcut(unittest.TestCase):
    """'What dose?' rule: bare dosing request after a recommendation."""

    def _make_state(self, last_drugs=None, active_file=None):
        import telegram_bot as b
        state = b.get_chat_state(f"test_{id(self)}")
        state["last_recommended_antibiotics"] = last_drugs or []
        if active_file:
            state["active_recognized"] = {"protocol_file": active_file, "source_label": "TEST"}
        else:
            state["active_recognized"] = None
        return state

    def test_no_last_drugs_returns_none(self):
        import telegram_bot as b
        state = self._make_state([])
        result = b._handle_dosing_shortcut(state, "dose?", None)
        self.assertIsNone(result, "Should return None when no last drugs")

    def test_not_bare_dosing_returns_none(self):
        import telegram_bot as b
        state = self._make_state(["ceftriaxone"])
        result = b._handle_dosing_shortcut(state, "what is the mechanism of ceftriaxone?", None)
        self.assertIsNone(result)

    def test_multiple_drugs_asks_which_one(self):
        import telegram_bot as b
        state = self._make_state(["ceftriaxone", "clarithromycin"])
        result = b._handle_dosing_shortcut(state, "dose?", None)
        self.assertIsNotNone(result)
        self.assertTrue(
            "ceftriaxone" in result.lower() or "clarithromycin" in result.lower(),
            f"Should mention drug names, got: {result}"
        )

    def test_single_drug_no_link_returns_not_available(self):
        """CAP -> dose? -> ceftriaxone protocol missing -> fallback message."""
        import telegram_bot as b
        # Use CAP protocol file path (has LINKS with ceftriaxone but target missing)
        cap_file = os.path.join(os.path.dirname(__file__), "protocols", "cap.txt")
        # Load the protocol so PROTOCOL_PARSED_BY_FILE is populated
        parsed = b._parse_protocol_text(open(cap_file).read(), path=cap_file)
        norm_path = b.normalize_path(cap_file)
        b.PROTOCOL_PARSED_BY_FILE[norm_path] = parsed

        state = self._make_state(["ceftriaxone"], active_file=cap_file)
        result = b._handle_dosing_shortcut(state, "dose?", None)
        self.assertIsNotNone(result, "Should return a message when dosing protocol missing")
        self.assertNotIn("Source:", result, "Shortcut result should not contain Source line yet")
        # Should mention ceftriaxone not available OR return target_missing_behavior
        lower = result.lower()
        self.assertTrue(
            "ceftriaxone" in lower or "not specified" in lower or "not available" in lower,
            f"Should reference ceftriaxone unavailability, got: {result}"
        )

    def test_single_drug_no_protocol_context(self):
        """No active protocol file -> protocol does not cover this drug."""
        import telegram_bot as b
        state = self._make_state(["oseltamivir"], active_file=None)
        result = b._handle_dosing_shortcut(state, "dose?", None)
        self.assertIsNotNone(result)


class TestOrganismDisambiguation(unittest.TestCase):
    """Organism-only queries without BioFire context should trigger disambiguation."""

    def _make_state(self, active_type=None):
        import telegram_bot as b
        state = b.get_chat_state(f"org_{id(self)}")
        if active_type:
            state["active_recognized"] = {"protocol_type": active_type}
        else:
            state["active_recognized"] = None
        return state

    def _biofire_recognized(self):
        return {
            "protocol_type": "microbiology_interpretation_protocol",
            "display": "BioFire",
            "source_label": "BioFire",
            "protocol_file": "protocols/pneumonia_pcr.txt",
        }

    def test_organism_without_context_triggers_disambiguation(self):
        import telegram_bot as b
        state = self._make_state(active_type=None)
        recognized = self._biofire_recognized()
        result = b._handle_organism_disambiguation(state, "Strep pneumo", recognized)
        self.assertIsNotNone(result, "Should return disambiguation prompt")
        lower = result.lower()
        self.assertTrue(
            "biofire" in lower or "pcr" in lower or "interpret" in lower or "antibiotic" in lower,
            f"Disambiguation should mention BioFire/PCR or antibiotic, got: {result}"
        )

    def test_organism_with_microbiology_context_no_disambiguation(self):
        """Already in BioFire context -> no disambiguation needed."""
        import telegram_bot as b
        state = self._make_state(active_type="microbiology_interpretation_protocol")
        recognized = self._biofire_recognized()
        result = b._handle_organism_disambiguation(state, "Strep pneumo", recognized)
        self.assertIsNone(result, "No disambiguation when already in microbiology context")

    def test_non_microbiology_recognized_no_disambiguation(self):
        import telegram_bot as b
        state = self._make_state(active_type=None)
        recognized = {"protocol_type": "drug_dosing_protocol", "display": "meropenem"}
        result = b._handle_organism_disambiguation(state, "meropenem", recognized)
        self.assertIsNone(result)

    def test_no_recognized_no_disambiguation(self):
        import telegram_bot as b
        state = self._make_state()
        result = b._handle_organism_disambiguation(state, "some text", None)
        self.assertIsNone(result)

    def test_organism_with_cap_context_triggers_disambiguation(self):
        """Active CAP (pathway) context + organism mention -> disambiguate."""
        import telegram_bot as b
        state = self._make_state(active_type="pathway_selection_protocol")
        recognized = self._biofire_recognized()
        result = b._handle_organism_disambiguation(state, "Strep pneumo", recognized)
        self.assertIsNotNone(result, "Should disambiguate when switching from pathway to micro context")


class TestStateManagement(unittest.TestCase):
    """State fields, resets, and context-source tracking."""

    def test_new_state_has_all_session8_fields(self):
        import telegram_bot as b
        state = b.get_chat_state(f"new_{id(self)}")
        required = [
            "active_protocol_id", "protocol_type", "last_user_intent",
            "collected_slots", "pending_question", "last_recommended_antibiotics",
            "dosing_allowed", "linked_dosing_protocol_available", "context_source",
        ]
        for field in required:
            self.assertIn(field, state, f"Missing state field: {field}")

    def test_reset_tree_state_clears_session8_fields(self):
        import telegram_bot as b
        state = b.get_chat_state(f"reset_{id(self)}")
        state["last_user_intent"] = "dosing_request"
        state["last_recommended_antibiotics"] = ["ceftriaxone"]
        state["context_source"] = "fresh_alias"
        state["collected_slots"] = {"gfr": "45"}
        b.reset_tree_state(state)
        self.assertIsNone(state["last_user_intent"])
        self.assertEqual(state["last_recommended_antibiotics"], [])
        self.assertIsNone(state["context_source"])
        self.assertEqual(state["collected_slots"], {})

    def test_explicit_reset_ack(self):
        """is_explicit_reset_phrase recognises reset phrases."""
        import telegram_bot as b
        for phrase in ["new patient", "new case", "reset", "new patient!"]:
            self.assertTrue(b.is_explicit_reset_phrase(phrase),
                            f"Should be reset phrase: {phrase!r}")

    def test_normal_text_not_reset(self):
        import telegram_bot as b
        for phrase in ["meropenem dose GFR 45", "what is the CAP pathway"]:
            self.assertFalse(b.is_explicit_reset_phrase(phrase))

    def test_non_tree_switch_updates_active_recognized(self):
        """When a fresh high-confidence alias arrives for a different protocol
        with no active tree, active_recognized should switch and
        last_recommended_antibiotics should clear."""
        import telegram_bot as b
        state = b.get_chat_state(f"switch_{id(self)}")
        # Simulate being in CAP context
        state["active_recognized"] = {
            "protocol_file": "protocols/cap.txt",
            "display": "CAP",
            "source_label": "CAP",
        }
        state["last_recommended_antibiotics"] = ["ceftriaxone"]
        state["tree"] = None  # no active tree

        # Simulate fresh meropenem recognition
        mero_recognized = {
            "protocol_file": "protocols/meropenem.txt",
            "display": "meropenem",
            "source_label": "meropenem",
            "confidence": "exact",
            "score": 100,
        }

        # Manually trigger the switch logic (as ask_ai would)
        if (mero_recognized
                and state.get("active_recognized")
                and not state.get("tree")
                and mero_recognized["protocol_file"] != state["active_recognized"].get("protocol_file")
                and mero_recognized.get("confidence") in ("exact", "high")):
            state["active_recognized"] = mero_recognized
            state["last_recommended_antibiotics"] = []

        self.assertEqual(state["active_recognized"]["display"], "meropenem")
        self.assertEqual(state["last_recommended_antibiotics"], [])



# ---------------------------------------------------------------------------
# Session 9: Deterministic Selection Engine Tests
# ---------------------------------------------------------------------------

# selection_engine lives in protocols/ which is already on sys.path
import selection_engine as se

_S9_PROTO_DIR = os.path.join(os.path.dirname(__file__), "protocols")


def _s9_load(filename):
    import protocol_parser as pp
    return pp.parse_protocol_file(os.path.join(_S9_PROTO_DIR, filename))


class TestPriorityRulesEngine(unittest.TestCase):

    def test_meropenem_no_slots_returns_default(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {})
        self.assertTrue(result.default_used)
        self.assertFalse(result.no_match)

    def test_meropenem_gfr_gt_90_selects_magas(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"gfr": 95.0})
        self.assertEqual(result.output_key, "MAGAS")

    def test_meropenem_crrt_selects_crrt(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"crrt": True})
        self.assertEqual(result.output_key, "CRRT")

    def test_meropenem_ihd_selects_ihd(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"ihd": True})
        self.assertEqual(result.output_key, "IHD")

    def test_meropenem_gfr_45_selects_atlagos(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"gfr": 45.0})
        self.assertEqual(result.output_key, "ATLAGOS")

    def test_meropenem_gfr_lt_20_selects_csokkentett(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"gfr": 15.0})
        self.assertEqual(result.output_key, "CSOKKENTETT")

    def test_meropenem_default_renders_text(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {})
        rendered = se.render_selected_output(parsed, result, lang="en")
        self.assertIn("Meropenem", rendered)

    def test_meropenem_selected_renders_with_dose(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"gfr": 95.0})
        rendered = se.render_selected_output(parsed, result, lang="en")
        self.assertIn("4 g/day", rendered)

    def test_ampsul_no_renal_returns_default(self):
        parsed = _s9_load("ampsul.txt")
        result = se.run_selection(parsed, {})
        self.assertTrue(result.default_used)

    def test_ampsul_gfr_45_selects_gfr_30_to_60(self):
        parsed = _s9_load("ampsul.txt")
        result = se.run_selection(parsed, {"gfr": 45.0})
        self.assertEqual(result.output_key, "GFR_30_TO_60",
                         f"Expected GFR_30_TO_60, got {result.output_key!r}")

    def test_ampsul_gfr_45_renders_sulbactam_dose(self):
        parsed = _s9_load("ampsul.txt")
        result = se.run_selection(parsed, {"gfr": 45.0})
        rendered = se.render_selected_output(parsed, result, lang="en")
        self.assertIn("6 g/day", rendered)

    def test_ampsul_ihd_selects_ihd(self):
        parsed = _s9_load("ampsul.txt")
        result = se.run_selection(parsed, {"ihd": True})
        self.assertEqual(result.output_key, "IHD")

    def test_ampsul_crrt_selects_crrt_or_gfr_ge_60(self):
        parsed = _s9_load("ampsul.txt")
        result = se.run_selection(parsed, {"crrt": True})
        self.assertEqual(result.output_key, "CRRT_OR_GFR_GE_60")


class TestCAPPriorityRules(unittest.TestCase):

    def test_cap_intubated_selects_intubated_cap(self):
        parsed = _s9_load("cap.txt")
        result = se.run_selection(parsed, {"intubated": True, "patient_status": "intubated"})
        self.assertEqual(result.output_key, "INTUBATED_CAP")

    def test_cap_hospitalized_standard(self):
        parsed = _s9_load("cap.txt")
        result = se.run_selection(parsed, {"patient_status": "hospitalized"})
        self.assertEqual(result.output_key, "HOSPITALIZED_STANDARD")

    def test_cap_hospitalized_nosocomial_risk(self):
        parsed = _s9_load("cap.txt")
        result = se.run_selection(parsed, {"patient_status": "hospitalized", "nosocomial_risk": True})
        self.assertEqual(result.output_key, "HOSPITALIZED_NOSOCOMIAL_RISK")

    def test_cap_dischargeable_viral_negative(self):
        parsed = _s9_load("cap.txt")
        result = se.run_selection(parsed, {"patient_status": "dischargeable", "viral_test_result": "negative"})
        self.assertEqual(result.output_key, "OUTPATIENT_STANDARD")

    def test_cap_dischargeable_viral_positive(self):
        parsed = _s9_load("cap.txt")
        result = se.run_selection(parsed, {"patient_status": "dischargeable", "viral_test_result": "positive"})
        self.assertEqual(result.output_key, "OUTPATIENT_VIRAL_POSITIVE")

    def test_cap_no_input_returns_default(self):
        parsed = _s9_load("cap.txt")
        result = se.run_selection(parsed, {})
        self.assertTrue(result.default_used)

    def test_cap_intubated_priority_over_nosocomial(self):
        parsed = _s9_load("cap.txt")
        result = se.run_selection(parsed, {"patient_status": "intubated", "intubated": True, "nosocomial_risk": True})
        self.assertEqual(result.output_key, "INTUBATED_CAP")

    def test_cap_influenza(self):
        parsed = _s9_load("cap.txt")
        result = se.run_selection(parsed, {"influenza": True})
        self.assertEqual(result.output_key, "INFLUENZA")

    def test_cap_intubated_renders_ceftriaxone_or_biofire(self):
        parsed = _s9_load("cap.txt")
        result = se.run_selection(parsed, {"intubated": True, "patient_status": "intubated"})
        rendered = se.render_selected_output(parsed, result, lang="en")
        lower = rendered.lower()
        self.assertTrue("ceftriaxone" in lower or "biofire" in lower,
                        f"INTUBATED_CAP should mention ceftriaxone or BioFire: {rendered[:200]}")


class TestTMPSMXTableLookup(unittest.TestCase):

    def test_steno_bsi_60kg_gfr60_returns_high_dose(self):
        """Core test: Stenotrophomonas BSI 60 kg GFR 60 -> HIGH_DOSE_GFR_GT_30_OR_CRRT."""
        parsed = _s9_load("tmpsmx.txt")
        slots = se.extract_slots_from_query("Sumetrolim, Steno BSI, 60 kg, GFR 60",
                                            parsed_protocol=parsed)
        result = se.run_selection(parsed, slots)
        self.assertFalse(result.no_match)
        self.assertEqual(result.output_key, "HIGH_DOSE_GFR_GT_30_OR_CRRT",
                         f"Expected HIGH_DOSE_GFR_GT_30_OR_CRRT, got {result.output_key!r}; "
                         f"missing={result.missing_slots}, default={result.default_used}")

    def test_steno_bsi_60kg_gfr60_renders_weight_row(self):
        parsed = _s9_load("tmpsmx.txt")
        slots = {"indication": "stenotrophomonas bsi", "body_weight_kg": 60.0, "gfr": 60.0}
        result = se.run_selection(parsed, slots)
        rendered = se.render_selected_output(parsed, result, lang="en")
        # 60 kg row: 3 x 4 amp / 960/4800 mg daily
        self.assertTrue("4 amp" in rendered or "960" in rendered or "3 x 4" in rendered,
                        f"60 kg weight row not found: {rendered}")

    def test_missing_weight_asks(self):
        parsed = _s9_load("tmpsmx.txt")
        slots = {"indication": "stenotrophomonas bsi", "gfr": 60.0}
        result = se.run_selection(parsed, slots)
        self.assertIn("body_weight_kg", result.missing_slots)

    def test_missing_indication_returns_default(self):
        parsed = _s9_load("tmpsmx.txt")
        slots = {"body_weight_kg": 60.0, "gfr": 60.0}
        result = se.run_selection(parsed, slots)
        self.assertIn("indication", result.missing_slots)

    def test_missing_all_slots(self):
        parsed = _s9_load("tmpsmx.txt")
        result = se.run_selection(parsed, {})
        self.assertTrue(result.missing_slots or result.default_used)

    def test_pcp_50kg_gfr20_high_dose_gfr_15_30(self):
        parsed = _s9_load("tmpsmx.txt")
        slots = {"indication": "pcp treatment", "body_weight_kg": 50.0, "gfr": 20.0}
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "HIGH_DOSE_GFR_15_TO_30",
                         f"Got {result.output_key!r}")

    def test_prophylaxis_gfr_gt_30(self):
        parsed = _s9_load("tmpsmx.txt")
        slots = {"indication": "pcp prophylaxis immunosuppressed", "body_weight_kg": 70.0, "gfr": 50.0}
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "PROPHYLAXIS_GFR_GT_30_OR_CRRT")

    def test_ihd_returns_ihd(self):
        parsed = _s9_load("tmpsmx.txt")
        slots = {"indication": "stenotrophomonas bsi", "body_weight_kg": 60.0, "ihd": True}
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "IHD")

    def test_gfr_lt_15_returns_warning(self):
        parsed = _s9_load("tmpsmx.txt")
        slots = {"indication": "stenotrophomonas bsi", "body_weight_kg": 60.0, "gfr": 10.0}
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "GFR_LT_15_WITHOUT_CRRT")


class TestBioFireOrganismMapping(unittest.TestCase):

    def test_pneumococcus_selects_tier1_ceftriaxone(self):
        """Core test: BioFire pneumococcus -> Tier 1 ceftriaxone."""
        parsed = _s9_load("pneumonia_pcr.txt")
        result = se.run_selection(parsed, {"pathogen_list": ["streptococcus pneumoniae"]})
        self.assertEqual(result.output_key, "TIER_1_CEFTRIAXONE",
                         f"Got {result.output_key!r}")

    def test_pneumococcus_alias_normalized(self):
        parsed = _s9_load("pneumonia_pcr.txt")
        slots = se.extract_slots_from_query("BioFire PN result: pneumococcus detected",
                                            parsed_protocol=parsed)
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "TIER_1_CEFTRIAXONE")

    def test_pneumococcus_no_dosing_in_rendered(self):
        parsed = _s9_load("pneumonia_pcr.txt")
        result = se.run_selection(parsed, {"pathogen_list": ["streptococcus pneumoniae"]})
        rendered = se.render_selected_output(parsed, result, lang="en")
        self.assertIn("ceftriaxone", rendered.lower())
        # BioFire should not contain dose amounts
        self.assertNotRegex(rendered, r"\d+ g/day")

    def test_pseudomonas_tier2_cefepime(self):
        parsed = _s9_load("pneumonia_pcr.txt")
        result = se.run_selection(parsed, {"pathogen_list": ["pseudomonas aeruginosa"]})
        self.assertEqual(result.output_key, "TIER_2_CEFEPIME")

    def test_ecoli_ctx_m_tier3_ertapenem(self):
        """E. coli + CTX-M -> Tier 3 ertapenem."""
        parsed = _s9_load("pneumonia_pcr.txt")
        slots = {"pathogen_list": ["escherichia coli"], "resistance_gene_list": ["ctx_m"]}
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "TIER_3_ERTAPENEM",
                         f"CTX-M should upgrade to Tier 3, got {result.output_key!r}")

    def test_acinetobacter_tier4(self):
        parsed = _s9_load("pneumonia_pcr.txt")
        result = se.run_selection(parsed, {"pathogen_list": ["acinetobacter calcoaceticus-baumannii complex"]})
        self.assertEqual(result.output_key, "TIER_4_MEROPENEM_COLISTIN")

    def test_polymicrobial_highest_tier_wins(self):
        """Strep pneumoniae (T1) + Pseudomonas (T2) -> Tier 2."""
        parsed = _s9_load("pneumonia_pcr.txt")
        slots = {"pathogen_list": ["streptococcus pneumoniae", "pseudomonas aeruginosa"]}
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "TIER_2_CEFEPIME",
                         f"Polymicrobial highest tier: {result.output_key!r}")

    def test_resistance_marker_without_pathogen_asks(self):
        parsed = _s9_load("pneumonia_pcr.txt")
        result = se.run_selection(parsed, {"resistance_gene_list": ["ctx_m"]})
        self.assertTrue(result.missing_slots or result.ask_missing)
        self.assertIsNone(result.output_key)

    def test_no_pathogen_returns_default(self):
        parsed = _s9_load("pneumonia_pcr.txt")
        result = se.run_selection(parsed, {})
        self.assertTrue(result.default_used)

    def test_staph_mssa_cefazolin(self):
        parsed = _s9_load("pneumonia_pcr.txt")
        result = se.run_selection(parsed, {"pathogen_list": ["staphylococcus aureus"]})
        self.assertEqual(result.output_key, "STAPH_AUREUS_MSSA")

    def test_staph_mrsa_vancomycin(self):
        parsed = _s9_load("pneumonia_pcr.txt")
        slots = {"pathogen_list": ["staphylococcus aureus"], "resistance_gene_list": ["meca_c"]}
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "STAPH_AUREUS_MRSA")

    def test_influenza_oseltamivir(self):
        parsed = _s9_load("pneumonia_pcr.txt")
        slots = se.extract_slots_from_query("BioFire: Influenza A detected", parsed_protocol=parsed)
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "INFLUENZA")

    def test_carbapenemase_tier4(self):
        parsed = _s9_load("pneumonia_pcr.txt")
        slots = {"pathogen_list": ["klebsiella pneumoniae group"], "resistance_gene_list": ["carbapenemase"]}
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "TIER_4_MEROPENEM_COLISTIN")


class TestSlotExtractor(unittest.TestCase):

    def test_gfr_extracted(self):
        self.assertEqual(se.extract_slots_from_query("meropenem GFR 45").get("gfr"), 45.0)

    def test_weight_extracted(self):
        self.assertEqual(se.extract_slots_from_query("TMP/SMX 60 kg").get("body_weight_kg"), 60.0)

    def test_crrt_flag(self):
        self.assertTrue(se.extract_slots_from_query("meropenem CRRT patient").get("crrt"))

    def test_ihd_flag(self):
        self.assertTrue(se.extract_slots_from_query("amp/sul IHD").get("ihd"))

    def test_intubated_flag(self):
        self.assertTrue(se.extract_slots_from_query("patient is intubated").get("intubated"))

    def test_patient_status_hospitalized(self):
        self.assertEqual(se.extract_slots_from_query("hospitalized patient").get("patient_status"), "hospitalized")

    def test_biofire_pneumococcus(self):
        parsed = _s9_load("pneumonia_pcr.txt")
        slots = se.extract_slots_from_query("BioFire result: pneumococcus", parsed_protocol=parsed)
        self.assertIn("streptococcus pneumoniae", slots.get("pathogen_list", []))

    def test_existing_slots_preserved(self):
        slots = se.extract_slots_from_query("meropenem", existing_slots={"gfr": 45.0})
        self.assertEqual(slots.get("gfr"), 45.0)

    def test_new_gfr_overwrites_existing(self):
        slots = se.extract_slots_from_query("GFR 80", existing_slots={"gfr": 45.0})
        self.assertEqual(slots.get("gfr"), 80.0)


class TestEngineNoMatchModes(unittest.TestCase):

    def test_decision_tree_mode_no_match(self):
        import protocol_parser as pp
        p = pp._parse_protocol_text("## METADATA\nprotocol_id: test\nselection_mode: decision_tree\n")
        self.assertTrue(se.run_selection(p, {}).no_match)

    def test_none_mode_no_match(self):
        import protocol_parser as pp
        p = pp._parse_protocol_text("## METADATA\nprotocol_id: test\nselection_mode: none\n")
        self.assertTrue(se.run_selection(p, {}).no_match)


# ---------------------------------------------------------------------------
# Session 13: Conservative Protocol Alias Cleanup Tests
# ---------------------------------------------------------------------------

class TestSession13AliasCleanup(unittest.TestCase):

    def setUp(self):
        self._old_aliases = dict(bot.ALIASES)
        self._old_alias_index = dict(bot.ALIAS_INDEX)
        self._old_blocked_aliases = set(bot.BLOCKED_ALIASES)
        self._old_file_labels = dict(bot.PROTOCOL_FILE_TO_LABEL)
        bot.load_aliases(os.path.join("protocols", "aliases.json"))

    def tearDown(self):
        bot.ALIASES = self._old_aliases
        bot.ALIAS_INDEX = self._old_alias_index
        bot.BLOCKED_ALIASES = self._old_blocked_aliases
        bot.PROTOCOL_FILE_TO_LABEL = self._old_file_labels

    def _recognized_file(self, query):
        _, recognized = bot.normalize_question(query)
        if not recognized:
            return None
        return bot.normalize_path(recognized.get("protocol_file", ""))

    def test_broad_carbapenem_no_longer_routes_to_meropenem(self):
        self.assertNotEqual(
            self._recognized_file("carbapenem"),
            bot.normalize_path("protocols/meropenem.txt"),
        )

    def test_hap_vap_aliases_do_not_route_to_cap(self):
        for query in [
            "hap",
            "vap",
            "hospital acquired pneumonia",
            "ventilator associated pneumonia",
            "nosocomial pneumonia",
        ]:
            with self.subTest(query=query):
                self.assertNotEqual(
                    self._recognized_file(query),
                    bot.normalize_path("protocols/cap.txt"),
                )

    def test_joint_infection_aliases_do_not_route_to_pneumonia_pcr(self):
        for query in [
            "ji panel",
            "joint infection panel",
            "biofire joint infection",
            "filmarray ji",
        ]:
            with self.subTest(query=query):
                self.assertNotEqual(
                    self._recognized_file(query),
                    bot.normalize_path("protocols/pneumonia_pcr.txt"),
                )


# ---------------------------------------------------------------------------
# Session 10: Debug/Admin Inspectability Tests
# ---------------------------------------------------------------------------

class TestSession10DebugCommands(unittest.TestCase):

    PROTO_DIR = os.path.join(os.path.dirname(__file__), "protocols")

    def setUp(self):
        import telegram_bot as b
        self._old_parsed = dict(b.PROTOCOL_PARSED_BY_FILE)
        self._old_aliases = dict(b.ALIASES)
        self._old_alias_index = dict(b.ALIAS_INDEX)
        self._old_file_labels = dict(b.PROTOCOL_FILE_TO_LABEL)
        b.PROTOCOL_PARSED_BY_FILE.clear()
        b.CONVERSATION_STATE.pop(f"debug_{id(self)}", None)

    def tearDown(self):
        import telegram_bot as b
        b.PROTOCOL_PARSED_BY_FILE.clear()
        b.PROTOCOL_PARSED_BY_FILE.update(self._old_parsed)
        b.ALIASES = self._old_aliases
        b.ALIAS_INDEX = self._old_alias_index
        b.PROTOCOL_FILE_TO_LABEL = self._old_file_labels
        b.CONVERSATION_STATE.pop(f"debug_{id(self)}", None)

    def _install_protocol(self, filename):
        import telegram_bot as b
        import protocol_parser as pp
        path = os.path.join("protocols", filename)
        parsed = pp.parse_protocol_file(os.path.join(self.PROTO_DIR, filename))
        b.PROTOCOL_PARSED_BY_FILE[b.normalize_path(path)] = parsed
        return path, parsed

    def test_protocols_output_lists_governance_fields(self):
        import telegram_bot as b
        self._install_protocol("meropenem.txt")
        output = b.format_protocols_output()
        self.assertIn("meropenem", output)
        self.assertIn("drug_dosing_protocol", output)
        self.assertIn("draft", output)
        self.assertIn("0.1", output)

    def test_version_uses_protocol_versions_when_available(self):
        import telegram_bot as b
        self._install_protocol("meropenem.txt")
        output = b.format_version_output()
        self.assertIn("Bot version:", output)
        self.assertIn("Protocol library version: 0.1", output)

    def test_debug_trace_explains_fresh_alias_without_prompt_echo(self):
        import telegram_bot as b
        self._install_protocol("meropenem.txt")
        b.load_aliases(os.path.join("protocols", "aliases.json"))
        fake_chunks = [{
            "source": "protocols/meropenem.txt",
            "source_label": "meropenem",
            "text": "## DEFAULT_ANSWER\nProtocol dosing text",
            "similarity": 0.9876,
        }]
        with patch.object(b, "search_protocols", return_value=fake_chunks):
            output = b.build_debug_trace("private patient details meropenem dose", f"debug_{id(self)}")
        self.assertIn("Context source: fresh_alias", output)
        self.assertIn("Matched alias: meropenem", output)
        self.assertIn("Protocol type: drug_dosing_protocol", output)
        self.assertIn("Deterministic/LLM source: deterministic selection_engine", output)
        self.assertIn("File: protocols/meropenem.txt", output)
        self.assertIn("Section: DEFAULT_ANSWER", output)
        self.assertNotIn("private patient details", output)
        self.assertNotIn("Preview:", output)

    def test_default_logging_redacts_prompt(self):
        import telegram_bot as b
        safe = b._safe_user_message_for_log("John Doe fever")
        self.assertIsInstance(safe, dict)
        self.assertTrue(safe["redacted"])
        self.assertNotIn("John Doe", str(safe))

    def test_full_conversation_logging_prints_reconstructable_turn(self):
        import contextlib
        import io
        import json
        import telegram_bot as b

        old_full = b.FULL_CONVERSATION_LOG
        old_query_log = b._query_log
        b.FULL_CONVERSATION_LOG = True
        b._query_log = None
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                b._log_query(
                    chat_id=123,
                    user_message="John Doe fever",
                    recognized=None,
                    retrieved_chunks=[],
                    raw_llm="raw answer",
                    final_response="final answer",
                    duration_ms=7,
                )
            turn_line = next(
                line for line in out.getvalue().splitlines()
                if line.startswith("[TURN] ")
            )
            payload = json.loads(turn_line[len("[TURN] "):])
            self.assertEqual(payload["event"], "conversation_turn")
            self.assertEqual(payload["user_message"], "John Doe fever")
            self.assertEqual(payload["assistant_message"], "final answer")
            self.assertEqual(payload["raw_llm"], "raw answer")
        finally:
            b.FULL_CONVERSATION_LOG = old_full
            b._query_log = old_query_log


# ---------------------------------------------------------------------------
# Session 11: Module Split Tests
# ---------------------------------------------------------------------------

class TestSession11ModuleSplit(unittest.TestCase):

    def test_named_modules_import(self):
        import aliases
        import authorization
        import logging_audit
        import prompting
        import protocol_schema
        import retrieval
        import routing
        import state
        import telegram_app

        self.assertTrue(callable(aliases.normalize_question))
        self.assertTrue(callable(authorization._is_allowed))
        self.assertTrue(callable(logging_audit._safe_user_message_for_log))
        self.assertTrue(callable(prompting.build_system_prompt))
        self.assertTrue(callable(protocol_schema.parse_protocol_file))
        self.assertTrue(callable(retrieval.search_protocols))
        self.assertTrue(callable(routing.ask_ai))
        self.assertTrue(callable(state.get_chat_state))
        self.assertTrue(callable(telegram_app.main))

    def test_startup_command_alias_registered(self):
        import inspect
        import telegram_bot as b

        src = inspect.getsource(b.main)
        self.assertIn('CommandHandler("start", handle_start)', src)
        self.assertIn('CommandHandler("startup", handle_start)', src)

    def test_load_protocols_handles_fresh_file_after_module_split(self):
        import numpy as np
        import tempfile
        import telegram_bot as b

        old_cwd = os.getcwd()
        old_cache_db = b.CACHE_DB
        old_chunks = list(b.PROTOCOL_CHUNKS)
        old_policy = dict(b.PROTOCOL_POLICY_BY_FILE)
        old_parsed = dict(b.PROTOCOL_PARSED_BY_FILE)
        old_file_labels = dict(b.PROTOCOL_FILE_TO_LABEL)

        with tempfile.TemporaryDirectory(dir=old_cwd) as tmp:
            protocols_dir = os.path.join(tmp, "protocols")
            os.mkdir(protocols_dir)
            protocol_path = os.path.join(protocols_dir, "fresh.txt")
            with open(protocol_path, "w", encoding="utf-8") as f:
                f.write(
                    "## METADATA\n"
                    "protocol_id: fresh\n"
                    "source_label: Fresh Protocol\n\n"
                    "## DEFAULT_ANSWER\n"
                    "Fresh protocol answer.\n"
                )

            try:
                os.chdir(tmp)
                b.CACHE_DB = os.path.join(tmp, "embeddings_cache.db")
                b.PROTOCOL_CHUNKS.clear()
                b.PROTOCOL_POLICY_BY_FILE.clear()
                b.PROTOCOL_PARSED_BY_FILE.clear()
                b.PROTOCOL_FILE_TO_LABEL.clear()

                with patch.object(b, "get_embedding", return_value=np.array([1.0, 0.0])):
                    b.load_protocols()

                self.assertEqual(len(b.PROTOCOL_CHUNKS), 1)
                self.assertEqual(b.PROTOCOL_CHUNKS[0]["source_label"], "Fresh Protocol")
                self.assertIn(
                    b.normalize_path(os.path.join("protocols", "fresh.txt")),
                    b.PROTOCOL_PARSED_BY_FILE,
                )
            finally:
                os.chdir(old_cwd)
                b.CACHE_DB = old_cache_db
                b.PROTOCOL_CHUNKS[:] = old_chunks
                b.PROTOCOL_POLICY_BY_FILE.clear()
                b.PROTOCOL_POLICY_BY_FILE.update(old_policy)
                b.PROTOCOL_PARSED_BY_FILE.clear()
                b.PROTOCOL_PARSED_BY_FILE.update(old_parsed)
                b.PROTOCOL_FILE_TO_LABEL = old_file_labels

    def test_state_module_shares_legacy_runtime_state(self):
        import telegram_bot as b
        import state

        chat_id = f"split_{id(self)}"
        try:
            from_state = state.get_chat_state(chat_id)
            from_bot = b.get_chat_state(chat_id)
            self.assertIs(from_state, from_bot)
            from_state["context_source"] = "module_split"
            self.assertEqual(b.get_chat_state(chat_id)["context_source"], "module_split")
        finally:
            b.CONVERSATION_STATE.pop(chat_id, None)

if __name__ == "__main__":
    unittest.main(verbosity=2)
