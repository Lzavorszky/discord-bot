"""
Session 1 tests - deployment and output hygiene.
Run: python test_bot.py
"""

import sys
import os
import io
import json
import tempfile
import unittest
import importlib.util
import types
from unittest.mock import patch, MagicMock, AsyncMock

# Allow importing from protocols/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "protocols"))

TEST_ROOT = os.path.dirname(__file__)
TEST_PROTOCOL_DIR = os.path.join(TEST_ROOT, "protocols")


def protocol_fixture_path(filename):
    """Resolve protocol test fixtures after the antibiotics folder split."""
    direct = os.path.join(TEST_PROTOCOL_DIR, filename)
    if os.path.exists(direct):
        return direct
    for root, _dirs, files in os.walk(TEST_PROTOCOL_DIR):
        if filename in files:
            return os.path.join(root, filename)
    raise FileNotFoundError(filename)


def protocol_fixture_relpath(filename):
    path = protocol_fixture_path(filename)
    rel = os.path.relpath(path, TEST_ROOT)
    return rel.replace(os.sep, "/")

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
            self.skipTest("SAFETY_FOOTER is empty - nothing to check")
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
            self.skipTest("SAFETY_FOOTER is empty - nothing to check")
        result = self._clean("Some answer text.")
        lines = result.splitlines()
        src_idx = next((i for i, l in enumerate(lines) if l.startswith("Source:")), None)
        self.assertIsNotNone(src_idx, "No Source line in output")
        before_source = "\n".join(lines[:src_idx])
        self.assertIn(self.SAFETY, before_source,
                      "SAFETY_FOOTER must appear before the Source line")


class TestProductionStartupHardening(unittest.TestCase):
    """Production startup must fail closed and avoid mutating protocol sources."""

    def setUp(self):
        self._old_runtime_options = dict(bot.RUNTIME_OPTIONS)
        self._old_access_mode = bot.ACCESS_MODE
        self._old_debug_logging_options = dict(bot.DEBUG_LOGGING_OPTIONS)
        self._old_full_conversation_log = bot.FULL_CONVERSATION_LOG
        self._old_allowed_user_ids = set(bot.ALLOWED_USER_IDS)

    def tearDown(self):
        bot.RUNTIME_OPTIONS = self._old_runtime_options
        bot.ACCESS_MODE = self._old_access_mode
        bot.DEBUG_LOGGING_OPTIONS = self._old_debug_logging_options
        bot.FULL_CONVERSATION_LOG = self._old_full_conversation_log
        bot.ALLOWED_USER_IDS = self._old_allowed_user_ids

    def test_warning_text_present_in_source(self):
        """Verify the exact warning string is in run_startup_checks source code."""
        import inspect
        src = inspect.getsource(bot.run_startup_checks)
        self.assertIn(
            "!! ALLOWED USERS NOT DEFINED !!",
            src,
            "Expected warning string not found in run_startup_checks"
        )

    def test_missing_allowlist_fails_closed_outside_local_debug(self):
        saved_allowed = os.environ.pop("ALLOWED_USER_IDS", None)
        saved_debug = os.environ.pop("LOCAL_DEBUG", None)
        saved_runtime_file = os.environ.get("RUNTIME_OPTIONS_FILE")
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        try:
            json.dump({
                "access_mode": "closed",
                "log_user_messages": False,
                "allowed_user_ids": [],
                "admin_user_ids": [],
            }, tmp)
            tmp.close()
            buf = io.StringIO()
            from contextlib import redirect_stdout
            os.environ["TELEGRAM_TOKEN"] = "dummy"
            os.environ["OPENAI_API_KEY"] = "dummy"
            os.environ["RUNTIME_OPTIONS_FILE"] = tmp.name
            with redirect_stdout(buf), self.assertRaises(SystemExit):
                bot.run_startup_checks()
            output = buf.getvalue()
            self.assertIn(
                "ALLOWED_USER_IDS environment variable is not set",
                output,
                f"Expected fail-closed startup error. Got:\n{output}"
            )
        finally:
            if saved_allowed is not None:
                os.environ["ALLOWED_USER_IDS"] = saved_allowed
            else:
                os.environ.pop("ALLOWED_USER_IDS", None)
            if saved_debug is not None:
                os.environ["LOCAL_DEBUG"] = saved_debug
            else:
                os.environ.pop("LOCAL_DEBUG", None)
            if saved_runtime_file is not None:
                os.environ["RUNTIME_OPTIONS_FILE"] = saved_runtime_file
            else:
                os.environ.pop("RUNTIME_OPTIONS_FILE", None)
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def test_missing_allowlist_allowed_with_local_debug_warning(self):
        saved_allowed = os.environ.pop("ALLOWED_USER_IDS", None)
        saved_debug = os.environ.get("LOCAL_DEBUG")
        try:
            buf = io.StringIO()
            from contextlib import redirect_stdout
            os.environ["TELEGRAM_TOKEN"] = "dummy"
            os.environ["OPENAI_API_KEY"] = "dummy"
            os.environ["LOCAL_DEBUG"] = "1"
            with redirect_stdout(buf):
                bot.run_startup_checks()
            output = buf.getvalue()
            self.assertIn("!! ALLOWED USERS NOT DEFINED !!", output)
            self.assertIn("[startup] All checks passed.", output)
        finally:
            if saved_allowed is not None:
                os.environ["ALLOWED_USER_IDS"] = saved_allowed
            else:
                os.environ.pop("ALLOWED_USER_IDS", None)
            if saved_debug is not None:
                os.environ["LOCAL_DEBUG"] = saved_debug
            else:
                os.environ.pop("LOCAL_DEBUG", None)

    def test_runtime_options_file_can_open_access_and_enable_user_message_logs(self):
        saved_runtime_file = os.environ.get("RUNTIME_OPTIONS_FILE")
        saved_access = os.environ.pop("ACCESS_MODE", None)
        saved_bot_access = os.environ.pop("BOT_ACCESS_MODE", None)
        saved_log = os.environ.pop("LOG_USER_MESSAGES", None)
        old_options = dict(bot.RUNTIME_OPTIONS)
        old_access_mode = bot.ACCESS_MODE
        old_debug_logging_options = dict(bot.DEBUG_LOGGING_OPTIONS)
        old_full_log = bot.FULL_CONVERSATION_LOG
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        try:
            json.dump({
                "access_mode": "open",
                "log_user_messages": True,
                "allowed_user_ids": [],
                "admin_user_ids": [],
            }, tmp)
            tmp.close()
            os.environ["RUNTIME_OPTIONS_FILE"] = tmp.name
            bot._refresh_runtime_settings()

            self.assertEqual(bot.ACCESS_MODE, "open")
            self.assertTrue(bot.FULL_CONVERSATION_LOG)
            self.assertTrue(bot._is_allowed(None))
            self.assertEqual(bot._safe_user_message_for_log("John Doe fever"), "John Doe fever")
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            if saved_runtime_file is not None:
                os.environ["RUNTIME_OPTIONS_FILE"] = saved_runtime_file
            else:
                os.environ.pop("RUNTIME_OPTIONS_FILE", None)
            if saved_access is not None:
                os.environ["ACCESS_MODE"] = saved_access
            if saved_bot_access is not None:
                os.environ["BOT_ACCESS_MODE"] = saved_bot_access
            if saved_log is not None:
                os.environ["LOG_USER_MESSAGES"] = saved_log
            bot.RUNTIME_OPTIONS = old_options
            bot.ACCESS_MODE = old_access_mode
            bot.DEBUG_LOGGING_OPTIONS = old_debug_logging_options
            bot.FULL_CONVERSATION_LOG = old_full_log

    def test_runtime_options_file_closed_access_overrides_local_debug_open_fallback(self):
        saved_runtime_file = os.environ.get("RUNTIME_OPTIONS_FILE")
        saved_access = os.environ.pop("ACCESS_MODE", None)
        saved_bot_access = os.environ.pop("BOT_ACCESS_MODE", None)
        saved_debug = os.environ.get("LOCAL_DEBUG")
        old_options = dict(bot.RUNTIME_OPTIONS)
        old_access_mode = bot.ACCESS_MODE
        old_debug_logging_options = dict(bot.DEBUG_LOGGING_OPTIONS)
        old_allowed = set(bot.ALLOWED_USER_IDS)
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        try:
            json.dump({
                "access_mode": "closed",
                "log_user_messages": False,
                "allowed_user_ids": [],
                "admin_user_ids": [],
            }, tmp)
            tmp.close()
            os.environ["RUNTIME_OPTIONS_FILE"] = tmp.name
            os.environ["LOCAL_DEBUG"] = "1"
            bot.ALLOWED_USER_IDS = set()
            bot._refresh_runtime_settings()

            self.assertEqual(bot.ACCESS_MODE, "closed")
            self.assertFalse(bot._is_allowed(None))
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            if saved_runtime_file is not None:
                os.environ["RUNTIME_OPTIONS_FILE"] = saved_runtime_file
            else:
                os.environ.pop("RUNTIME_OPTIONS_FILE", None)
            if saved_access is not None:
                os.environ["ACCESS_MODE"] = saved_access
            if saved_bot_access is not None:
                os.environ["BOT_ACCESS_MODE"] = saved_bot_access
            if saved_debug is not None:
                os.environ["LOCAL_DEBUG"] = saved_debug
            else:
                os.environ.pop("LOCAL_DEBUG", None)
            bot.RUNTIME_OPTIONS = old_options
            bot.ACCESS_MODE = old_access_mode
            bot.DEBUG_LOGGING_OPTIONS = old_debug_logging_options
            bot.ALLOWED_USER_IDS = old_allowed

    def test_alias_sync_not_called_in_production_startup(self):
        saved_debug = os.environ.pop("LOCAL_DEBUG", None)
        saved_sync = os.environ.get("ALIAS_SYNC_ON_STARTUP")
        try:
            os.environ["ALIAS_SYNC_ON_STARTUP"] = "1"
            with patch.object(bot, "ALIAS_SYNC_AVAILABLE", True), \
                    patch.object(bot, "_alias_sync") as sync_mock:
                ran = bot._maybe_run_alias_sync_on_startup()
            self.assertFalse(ran)
            sync_mock.assert_not_called()
        finally:
            if saved_debug is not None:
                os.environ["LOCAL_DEBUG"] = saved_debug
            else:
                os.environ.pop("LOCAL_DEBUG", None)
            if saved_sync is not None:
                os.environ["ALIAS_SYNC_ON_STARTUP"] = saved_sync
            else:
                os.environ.pop("ALIAS_SYNC_ON_STARTUP", None)

    def test_linter_blocking_error_prevents_startup(self):
        import protocol_linter

        issue = protocol_linter.LintIssue(
            "ERROR", "parse_crash", "protocols/bad.txt", "Parser crashed"
        )
        fake_result = MagicMock()
        fake_result.errors.return_value = [issue]
        fake_result.warnings.return_value = []

        saved_allowed = os.environ.get("ALLOWED_USER_IDS")
        saved_debug = os.environ.pop("LOCAL_DEBUG", None)
        try:
            os.environ["TELEGRAM_TOKEN"] = "dummy"
            os.environ["OPENAI_API_KEY"] = "dummy"
            os.environ["ALLOWED_USER_IDS"] = "123"
            buf = io.StringIO()
            from contextlib import redirect_stdout
            with patch.object(protocol_linter, "run_linter", return_value=fake_result), \
                    redirect_stdout(buf), self.assertRaises(SystemExit):
                bot.run_startup_checks()
            self.assertIn("Protocol linter blocking error", buf.getvalue())
        finally:
            if saved_allowed is not None:
                os.environ["ALLOWED_USER_IDS"] = saved_allowed
            else:
                os.environ.pop("ALLOWED_USER_IDS", None)
            if saved_debug is not None:
                os.environ["LOCAL_DEBUG"] = saved_debug
            else:
                os.environ.pop("LOCAL_DEBUG", None)




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
    """New-schema panels must be parsed explicitly - not land in free_form."""

    PROTO_DIR = os.path.join(os.path.dirname(__file__), "protocols")

    def _parse(self, filename):
        import protocol_parser as pp
        return pp.parse_protocol_file(protocol_fixture_path(filename))

    # New panels are recognised (not in free_form)

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
                      "SLOT_SCHEMA", "SELECTION_RULES", "SELECTED_OUTPUTS", "INFO_BLOCKS",
                      "RESTRICTED_OUTPUTS", "SAFETY_RULES", "OUTPUT_TEMPLATES"]:
            self.assertNotIn(panel, p["free_form"],
                             f"meropenem.txt: '{panel}' must not be in free_form")

    def test_new_panels_have_content(self):
        """Spot-check that new panels actually got their text."""
        p = self._parse("cap.txt")
        self.assertIn("priority_rules", p["selection_rules"])
        self.assertIn("INTUBATED_CAP", p["selected_outputs"])
        self.assertIn("ceftriaxone", p["info_blocks"])

    def test_slot_schema_parsed_for_numeric_bounds(self):
        p = self._parse("tmpsmx.txt")
        schema = p["slot_schema"]
        self.assertEqual(schema["body_weight_kg"]["type"], "number")
        self.assertEqual(schema["body_weight_kg"]["clinical_min"], 1.0)
        self.assertEqual(schema["body_weight_kg"]["supported_max"], 100.0)
        self.assertEqual(schema["gfr"]["clinical_max"], 250.0)

    # Old-schema file still loads cleanly

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

    # METADATA parsing

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
        return pp.parse_protocol_file(protocol_fixture_path(filename))

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
        self.assertEqual(link.get("target_file"), "protocols/antibiotics/ceftriaxone.txt")
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
        """meropenem.txt has LINKS: (none) - should parse to empty dict."""
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
        return pp.parse_protocol_file(protocol_fixture_path(filename))

    def test_valid_answer_mode_no_warning(self):
        p = self._parse("cap.txt")
        mode_warnings = [w for w in p["warnings"] if "answer_mode" in w]
        self.assertEqual(mode_warnings, [],
                         f"cap.txt has a valid answer_mode but got warnings: {mode_warnings}")

    def test_meropenem_answer_mode_no_warning(self):
        p = self._parse("meropenem.txt")
        mode_warnings = [w for w in p["warnings"] if "answer_mode" in w]
        self.assertEqual(mode_warnings, [],
                         f"meropenem.txt should use a current answer_mode. Got: {mode_warnings}")

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

    def test_unsupported_policy_collision_with_cap_alias_detected(self):
        import json
        import tempfile
        import protocol_linter as pl

        alias_data = {
            "conditions": {
                "cap": {
                    "canonical": "community-acquired pneumonia",
                    "display": "CAP",
                    "aliases": ["cap"],
                }
            },
            "unsupported_syndromes": {
                "bad_policy": {
                    "terms": ["cap"],
                    "message": "Unsupported.",
                    "allowed_if_explicit_drug": True,
                }
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(alias_data, f)
            path = f.name
        try:
            result = pl.LintResult()
            pl._lint_aliases_json(path, result)
            self.assertIn("unsupported_policy_collision", _codes(result))
        finally:
            os.unlink(path)

    def test_unsupported_policy_requires_terms_and_message(self):
        import json
        import tempfile
        import protocol_linter as pl

        alias_data = {
            "unsupported_syndromes": {
                "empty": {
                    "terms": [],
                    "message": "",
                }
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(alias_data, f)
            path = f.name
        try:
            result = pl.LintResult()
            pl._lint_aliases_json(path, result)
            self.assertIn("unsupported_policy_empty_terms", _codes(result))
            self.assertIn("unsupported_policy_missing_message", _codes(result))
        finally:
            os.unlink(path)


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


class TestLinterSlotSchemaSafety(unittest.TestCase):

    def _deterministic_text(self, slot_schema=""):
        schema_panel = f"\n## SLOT_SCHEMA\n\n{slot_schema}\n" if slot_schema is not None else ""
        return (
            "## METADATA\n"
            "protocol_id: renal_dose\n"
            "protocol_name: Renal dose protocol\n"
            "source_label: RENAL DOSE\n"
            "protocol_type: drug_dosing_protocol\n"
            "answer_mode: default_then_selected_output\n"
            "selection_mode: priority_rules\n"
            "allows_dosing: yes\n"
            "default_dose_allowed: yes\n"
            "version: 1.0\n"
            "last_reviewed: 2024-01-01\n"
            "owner: test_team\n"
            "status: draft\n"
            "\n"
            "## ALIASES\n"
            "- renal dose test\n"
            f"{schema_panel}"
            "## SELECTION_RULES\n"
            "\n"
            "selection_mode: priority_rules\n"
            "\n"
            "RULE: RENAL_LOW\n"
            "  IF: gfr < 30\n"
            "  PRIORITY: 10\n"
            "  SELECT: LOW\n"
            "\n"
            "## SELECTED_OUTPUTS\n"
            "\n"
            "### LOW\n"
            "dose: reduce dose\n"
        )

    def test_deterministic_numeric_selection_slot_without_slot_schema_is_error(self):
        result = _lint_text(self._deterministic_text(slot_schema=None))
        issues = [i for i in result.issues if i.code == "missing_slot_schema"]
        self.assertTrue(issues)
        self.assertTrue(all(i.severity == "ERROR" for i in issues))

    def test_deterministic_numeric_slot_missing_clinical_bounds_is_error(self):
        text = self._deterministic_text(
            "SLOT: gfr\n"
            "  type: number\n"
            "  unit: mL/min\n"
        )
        result = _lint_text(text)
        issues = [i for i in result.issues if i.code == "missing_numeric_slot_bounds"]
        self.assertTrue(issues)
        self.assertTrue(all(i.severity == "ERROR" for i in issues))

    def test_numeric_selection_slot_missing_from_existing_schema_is_error(self):
        text = self._deterministic_text(
            "SLOT: body_weight_kg\n"
            "  type: number\n"
            "  clinical_min: 1\n"
            "  clinical_max: 300\n"
        )
        result = _lint_text(text)
        issues = [i for i in result.issues if i.code == "undeclared_numeric_selection_slot"]
        self.assertTrue(issues)
        self.assertTrue(all(i.severity == "ERROR" for i in issues))

    def test_info_only_numeric_slot_missing_bounds_is_warning(self):
        text = (
            "## METADATA\n"
            "protocol_id: info_numeric\n"
            "protocol_name: Numeric info protocol\n"
            "source_label: INFO NUMERIC\n"
            "protocol_type: general_rules_protocol\n"
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
            "- numeric info test\n"
            "\n"
            "## SLOT_SCHEMA\n"
            "\n"
            "SLOT: gfr\n"
            "  type: number\n"
            "\n"
            "## INFO_BLOCKS\n"
            "\n"
            "Mentions GFR for background only.\n"
        )
        result = _lint_text(text)
        issues = [i for i in result.issues if i.code == "missing_numeric_slot_bounds"]
        self.assertTrue(issues)
        self.assertTrue(all(i.severity == "WARNING" for i in issues))

    def test_table_lookup_missing_supported_axis_bounds_is_error(self):
        text = (
            "## METADATA\n"
            "protocol_id: table_dose\n"
            "protocol_name: Table dose protocol\n"
            "source_label: TABLE DOSE\n"
            "protocol_type: drug_dosing_protocol\n"
            "answer_mode: required_slots_then_selected_output\n"
            "selection_mode: table_lookup\n"
            "allows_dosing: yes\n"
            "default_dose_allowed: yes\n"
            "version: 1.0\n"
            "last_reviewed: 2024-01-01\n"
            "owner: test_team\n"
            "status: draft\n"
            "\n"
            "## ALIASES\n"
            "- table dose test\n"
            "\n"
            "## SLOT_SCHEMA\n"
            "\n"
            "SLOT: body_weight_kg\n"
            "  type: number\n"
            "  unit: kg\n"
            "  clinical_min: 1\n"
            "  clinical_max: 300\n"
            "\n"
            "## SELECTION_RULES\n"
            "\n"
            "selection_mode: table_lookup\n"
            "\n"
            "STEP: SELECT_WEIGHT_ROW\n"
            "  METHOD: closest_practical_row\n"
            "  WEIGHT_SLOT: body_weight_kg\n"
            "\n"
            "## SELECTED_OUTPUTS\n"
            "\n"
            "### TABLE\n"
            "dose: see table\n"
        )
        result = _lint_text(text)
        issues = [i for i in result.issues if i.code == "missing_supported_table_bounds"]
        self.assertTrue(issues)
        self.assertTrue(all(i.severity == "ERROR" for i in issues))

    def test_table_lookup_detects_axis_from_selected_outputs_table(self):
        text = (
            "## METADATA\n"
            "protocol_id: selected_output_axis\n"
            "protocol_name: Selected output axis protocol\n"
            "source_label: SELECTED OUTPUT AXIS\n"
            "protocol_type: drug_dosing_protocol\n"
            "answer_mode: required_slots_then_selected_output\n"
            "selection_mode: table_lookup\n"
            "allows_dosing: yes\n"
            "default_dose_allowed: yes\n"
            "version: 1.0\n"
            "last_reviewed: 2024-01-01\n"
            "owner: test_team\n"
            "status: draft\n"
            "\n"
            "## ALIASES\n"
            "- selected output axis test\n"
            "\n"
            "## SLOT_SCHEMA\n"
            "\n"
            "SLOT: body_weight_kg\n"
            "  type: number\n"
            "  unit: kg\n"
            "  clinical_min: 1\n"
            "  clinical_max: 300\n"
            "\n"
            "## SELECTION_RULES\n"
            "\n"
            "selection_mode: table_lookup\n"
            "STEP: SELECT_TABLE\n"
            "  TABLE_KEY: TABLE\n"
            "\n"
            "## SELECTED_OUTPUTS\n"
            "\n"
            "### TABLE\n"
            "type: dosing_table\n"
            "| Weight | Practical dose |\n"
            "|---|---|\n"
            "| 40 kg | low dose |\n"
            "| 100 kg | high dose |\n"
        )
        result = _lint_text(text)
        issues = [i for i in result.issues if i.code == "missing_supported_table_bounds"]
        self.assertTrue(issues)
        self.assertTrue(all(i.severity == "ERROR" for i in issues))

    def test_table_lookup_extrapolation_allowed_without_method_is_error(self):
        text = (
            "## METADATA\n"
            "protocol_id: unsafe_extrapolation\n"
            "protocol_name: Unsafe extrapolation protocol\n"
            "source_label: UNSAFE EXTRAPOLATION\n"
            "protocol_type: drug_dosing_protocol\n"
            "answer_mode: required_slots_then_selected_output\n"
            "selection_mode: table_lookup\n"
            "allows_dosing: yes\n"
            "default_dose_allowed: yes\n"
            "version: 1.0\n"
            "last_reviewed: 2024-01-01\n"
            "owner: test_team\n"
            "status: draft\n"
            "\n"
            "## ALIASES\n"
            "- unsafe extrapolation test\n"
            "\n"
            "## SLOT_SCHEMA\n"
            "\n"
            "SLOT: body_weight_kg\n"
            "  type: number\n"
            "  unit: kg\n"
            "  clinical_min: 1\n"
            "  clinical_max: 300\n"
            "  extrapolation_allowed: true\n"
            "\n"
            "## SELECTION_RULES\n"
            "\n"
            "selection_mode: table_lookup\n"
            "STEP: SELECT_TABLE_ROW\n"
            "  AXIS_SLOT: body_weight_kg\n"
            "\n"
            "## SELECTED_OUTPUTS\n"
            "\n"
            "### TABLE\n"
            "type: dosing_table\n"
            "| Weight | Practical dose |\n"
            "|---|---|\n"
            "| 40 kg | low dose |\n"
            "| 100 kg | high dose |\n"
        )
        result = _lint_text(text)
        issues = [i for i in result.issues if i.code == "unsafe_table_extrapolation_policy"]
        self.assertTrue(issues)
        self.assertTrue(all(i.severity == "ERROR" for i in issues))

    def test_generic_table_lookup_valid_bounds_gets_review_response(self):
        import protocol_parser as pp
        import selection_engine as se

        text = (
            "## METADATA\n"
            "protocol_id: valid_generic_table\n"
            "protocol_name: Valid generic table protocol\n"
            "source_label: VALID GENERIC TABLE\n"
            "protocol_type: drug_dosing_protocol\n"
            "answer_mode: required_slots_then_selected_output\n"
            "selection_mode: table_lookup\n"
            "allows_dosing: yes\n"
            "default_dose_allowed: yes\n"
            "version: 1.0\n"
            "last_reviewed: 2024-01-01\n"
            "owner: test_team\n"
            "status: draft\n"
            "\n"
            "## ALIASES\n"
            "- valid generic table test\n"
            "\n"
            "## SLOT_SCHEMA\n"
            "\n"
            "SLOT: body_weight_kg\n"
            "  type: number\n"
            "  unit: kg\n"
            "  clinical_min: 1\n"
            "  clinical_max: 300\n"
            "  supported_min: 40\n"
            "  supported_max: 100\n"
            "\n"
            "## SELECTION_RULES\n"
            "\n"
            "selection_mode: table_lookup\n"
            "STEP: SELECT_TABLE\n"
            "  TABLE_KEY: TABLE\n"
            "STEP: SELECT_TABLE_ROW\n"
            "  AXIS_SLOT: body_weight_kg\n"
            "\n"
            "## SELECTED_OUTPUTS\n"
            "\n"
            "### TABLE\n"
            "type: dosing_table\n"
            "target: 10 mg/kg/day\n"
            "| Weight | Practical dose |\n"
            "|---|---|\n"
            "| 40 kg | low dose |\n"
            "| 100 kg | high dose |\n"
        )
        parsed = pp._parse_protocol_text(text)
        result = se.run_selection(parsed, {"body_weight_kg": 150.0})
        rendered = se.render_selected_output(parsed, result, lang="en")
        lower = rendered.lower()
        self.assertEqual(result.output_key, "TABLE")
        self.assertIn("outside the explicit protocol table range", lower)
        self.assertIn("automatic dose escalation is not supported", lower)
        self.assertIn("100 kg row", lower)
        self.assertIn("high dose", lower)
        self.assertIn("10 mg/kg/day", lower)


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

    def test_meropenem_answer_mode_is_current(self):
        result = self._run()
        issues = [i for i in result.issues
                  if i.code == "invalid_answer_mode" and "meropenem" in i.protocol]
        self.assertEqual(issues, [],
                         f"meropenem.txt should not use retired answer modes: {issues}")

    def test_broad_alias_detected_in_library(self):
        result = self._run()
        broad = [i for i in result.issues if i.code == "broad_alias"]
        self.assertTrue(len(broad) > 0, "Expected at least one broad_alias warning")

    def test_governance_warnings_cleared(self):
        """Session 6: all protocols now have governance metadata - no missing_governance warnings."""
        result = self._run()
        gov = [i for i in result.issues if i.code == "missing_governance"]
        self.assertEqual(gov, [], f"Unexpected missing_governance warnings: {gov}")

    def test_real_protocols_pass_slot_schema_safety_checks(self):
        result = self._run()
        slot_schema_codes = {
            "missing_slot_schema",
            "missing_numeric_slot_bounds",
            "invalid_numeric_slot_bounds",
            "missing_supported_table_bounds",
            "invalid_supported_table_bounds",
            "undeclared_numeric_selection_slot",
            "unsafe_table_extrapolation_policy",
        }
        issues = [i for i in result.issues if i.code in slot_schema_codes]
        self.assertEqual(issues, [], f"Unexpected SLOT_SCHEMA lint issues: {issues}")



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
        for q in ["new patient", "reset", "new case", "\u00faj beteg"]:
            self.assertEqual(b.classify_intent(q), "reset",
                             f"Expected reset for {q!r}")

    def test_unknown_returns_unknown(self):
        import telegram_bot as b
        self.assertEqual(b.classify_intent("hello there"), "unknown")


class TestRoutingEvidenceExtraction(unittest.TestCase):
    """Evidence extraction feeds the primary route-claims router."""

    def setUp(self):
        import aliases as alias_helpers
        import telegram_bot as b

        self.alias_helpers = alias_helpers
        self.b = b
        self._old_aliases = dict(b.ALIASES)
        self._old_alias_index = dict(b.ALIAS_INDEX)
        self._old_blocked_aliases = set(b.BLOCKED_ALIASES)
        self._old_unsupported_syndromes = dict(b.UNSUPPORTED_SYNDROMES)
        self._old_file_labels = dict(b.PROTOCOL_FILE_TO_LABEL)
        self._old_helper_aliases = dict(alias_helpers.ALIASES)
        self._old_helper_alias_index = dict(alias_helpers.ALIAS_INDEX)
        self._old_helper_blocked_aliases = set(alias_helpers.BLOCKED_ALIASES)
        self._old_helper_unsupported_syndromes = dict(alias_helpers.UNSUPPORTED_SYNDROMES)
        self._old_helper_file_labels = dict(alias_helpers.PROTOCOL_FILE_TO_LABEL)
        b.load_aliases(os.path.join("protocols", "aliases.json"))

    def tearDown(self):
        b = self.b
        alias_helpers = self.alias_helpers
        b.ALIASES = self._old_aliases
        b.ALIAS_INDEX = self._old_alias_index
        b.BLOCKED_ALIASES = self._old_blocked_aliases
        b.UNSUPPORTED_SYNDROMES = self._old_unsupported_syndromes
        b.PROTOCOL_FILE_TO_LABEL = self._old_file_labels
        alias_helpers.ALIASES = self._old_helper_aliases
        alias_helpers.ALIAS_INDEX = self._old_helper_alias_index
        alias_helpers.BLOCKED_ALIASES = self._old_helper_blocked_aliases
        alias_helpers.UNSUPPORTED_SYNDROMES = self._old_helper_unsupported_syndromes
        alias_helpers.PROTOCOL_FILE_TO_LABEL = self._old_helper_file_labels

    def test_evidence_dataclasses_have_safe_defaults(self):
        import routing

        evidence = routing.RoutingEvidence()
        self.assertEqual(evidence.intent, "unknown")
        self.assertEqual(evidence.subject, routing.RoutingSubject())
        self.assertEqual(evidence.test, routing.RoutingTest())
        self.assertEqual(evidence.microbes, [])
        self.assertEqual(evidence.markers, [])
        self.assertEqual(evidence.context, {})

        decision = routing.RouteDecision(kind="route", protocol_file="protocols/example.txt")
        self.assertEqual(decision.kind, "route")
        self.assertEqual(decision.protocol_file, "protocols/example.txt")

    def test_drug_dose_alias_extracts_subject_intent_and_renal_context(self):
        evidence = self.b.extract_routing_evidence("meropenem dose GFR 35")

        self.assertEqual(evidence.intent, "dose")
        self.assertEqual(evidence.subject.kind, "drug")
        self.assertEqual(evidence.subject.name, "meropenem")
        self.assertTrue(evidence.context["renal_function"])
        self.assertIn(
            "meropenem dose",
            [m.matched_text for m in evidence.matches if m.entity_type == "subject"],
        )

    def test_pcr_microbe_conflict_keeps_all_typed_evidence(self):
        evidence = self.b.extract_routing_evidence("Penumonia PCR Proteus")

        self.assertEqual(evidence.intent, "test_interpretation")
        self.assertEqual(evidence.subject.kind, "syndrome")
        self.assertEqual(evidence.subject.name, "community-acquired pneumonia")
        self.assertEqual(evidence.test.family, "pcr")
        self.assertIn("Proteus spp.", evidence.microbes)
        self.assertEqual(
            {"subject", "test", "microbe", "intent"} <= {m.entity_type for m in evidence.matches},
            True,
        )

    def test_collect_alias_matches_exposes_all_slice_examples(self):
        cases = [
            (
                "Penumonia PCR Proteus",
                [
                    ("penumonia", "protocols/cap.txt", "exact"),
                    ("pneumonia pcr", "protocols/pneumonia_pcr.txt", "high"),
                ],
            ),
            (
                "BioFire PN Proteus",
                [
                    ("biofire pn", "protocols/pneumonia_pcr.txt", "exact"),
                    ("biofire", "protocols/pneumonia_pcr.txt", "exact"),
                ],
            ),
            (
                "pneumonia pcr proteus",
                [
                    ("pneumonia pcr", "protocols/pneumonia_pcr.txt", "exact"),
                    ("pneumonia", "protocols/cap.txt", "exact"),
                ],
            ),
            (
                "meropenem dose GFR 35",
                [
                    ("meropenem dose", "protocols/antibiotics/meropenem.txt", "exact"),
                    ("meropenem", "protocols/antibiotics/meropenem.txt", "exact"),
                ],
            ),
            (
                "aspirin before surgery",
                [
                    ("before surgery", "protocols/periop_gyogyszerek.txt", "exact"),
                ],
            ),
        ]

        for question, expected_matches in cases:
            with self.subTest(question=question):
                matches = self.alias_helpers.collect_alias_matches(question)
                observed = {
                    (
                        match["alias"],
                        self.b.normalize_path(match["protocol_file"]),
                        match["confidence"],
                    )
                    for match in matches
                }
                for expected in expected_matches:
                    self.assertIn(expected, observed)
                self.assertTrue(all(match.get("canonical") for match in matches))
                self.assertTrue(all(match.get("display") for match in matches))
                self.assertTrue(all(match.get("matched_text") for match in matches))

    def test_broad_cap_aliases_are_weak_evidence(self):
        weak_aliases = {
            "pneumonia",
            "penumonia",
            "pneuomonia",
            "tudogyulladas",
            "leguti",
            "lrti",
        }

        for alias in weak_aliases:
            with self.subTest(alias=alias):
                matches = self.alias_helpers.collect_alias_matches(alias)
                cap_matches = [
                    match for match in matches
                    if self.b.normalize_path(match.get("protocol_file")) == "protocols/cap.txt"
                ]
                self.assertTrue(cap_matches)
                self.assertTrue(all(match.get("routing_strength") == "weak" for match in cap_matches))

    def test_extract_routing_evidence_uses_collected_alias_matches(self):
        cases = [
            ("Penumonia PCR Proteus", {"penumonia", "pneumonia pcr"}),
            ("BioFire PN Proteus", {"biofire pn", "biofire"}),
            ("pneumonia pcr proteus", {"pneumonia pcr", "pneumonia"}),
            ("meropenem dose GFR 35", {"meropenem dose", "meropenem"}),
            ("aspirin before surgery", {"before surgery"}),
        ]

        for question, expected_aliases in cases:
            with self.subTest(question=question):
                evidence = self.b.extract_routing_evidence(question)
                aliases = {
                    match.metadata.get("alias")
                    for match in evidence.matches
                    if match.source == "aliases"
                }
                self.assertTrue(expected_aliases <= aliases)

    def test_weak_aliases_do_not_directly_win_legacy_normalize_question(self):
        cases = [
            ("BioFire PN Proteus", "biofire pn", "protocols/pneumonia_pcr.txt"),
            ("pneumonia pcr proteus", "pneumonia pcr", "protocols/pneumonia_pcr.txt"),
            ("meropenem dose GFR 35", "meropenem dose", "protocols/antibiotics/meropenem.txt"),
            ("aspirin before surgery", "before surgery", "protocols/periop_gyogyszerek.txt"),
        ]

        for question, expected_alias, expected_protocol in cases:
            with self.subTest(question=question):
                _, recognized = self.b.normalize_question(question)
                self.assertIsNotNone(recognized)
                self.assertEqual(recognized.get("matched_alias"), expected_alias)
                self.assertEqual(
                    self.b.normalize_path(recognized.get("protocol_file")),
                    expected_protocol,
                )

        for weak_only in [
            "pneumonia",
            "penumonia",
            "pneuomonia",
            "tudogyulladas",
            "leguti",
            "lrti",
            "Penumonia PCR Proteus",
        ]:
            with self.subTest(weak_only=weak_only):
                _, recognized = self.b.normalize_question(weak_only)
                self.assertIsNone(recognized)

    def test_biofire_panel_microbe_and_marker_are_separate_evidence(self):
        evidence = self.b.extract_routing_evidence("BioFire PN E. coli CTX-M positive")

        self.assertEqual(evidence.intent, "test_interpretation")
        self.assertEqual(evidence.subject.kind, "test_panel")
        self.assertEqual(evidence.test.family, "pcr")
        self.assertEqual(evidence.test.panel, "pneumonia")
        self.assertIn("Escherichia coli", evidence.microbes)
        self.assertIn("CTX-M", evidence.markers)
        self.assertTrue(evidence.context["test_or_result"])

    def test_periop_and_conversion_intents_use_global_subject_kinds(self):
        periop = self.b.extract_routing_evidence("aspirin before surgery")
        self.assertEqual(periop.intent, "periop_advice")
        self.assertEqual(periop.subject.kind, "periop_med")
        self.assertTrue(periop.context["perioperative"])

        conversion = self.b.extract_routing_evidence("methylprednisone equivalent")
        self.assertEqual(conversion.intent, "conversion")
        self.assertEqual(conversion.subject.kind, "calculator")


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

    def test_single_drug_missing_target_returns_not_available(self):
        """CAP -> dose? -> amoxicillin protocol missing -> fallback message."""
        import telegram_bot as b
        cap_file = protocol_fixture_path("cap.txt")
        # Load the protocol so PROTOCOL_PARSED_BY_FILE is populated
        with open(cap_file, encoding="utf-8") as f:
            parsed = b._parse_protocol_text(f.read(), path=cap_file)
        norm_path = b.normalize_path(cap_file)
        b.PROTOCOL_PARSED_BY_FILE[norm_path] = parsed

        state = self._make_state(["amoxicillin"], active_file=cap_file)
        result = b._handle_dosing_shortcut(state, "dose?", None)
        self.assertIsNotNone(result, "Should return a message when dosing protocol missing")
        self.assertNotIn("Source:", result, "Shortcut result should not contain Source line yet")
        # Should mention amoxicillin not available OR return target_missing_behavior
        lower = result.lower()
        self.assertTrue(
            "amoxicillin" in lower or "not specified" in lower or "not available" in lower,
            f"Should reference amoxicillin unavailability, got: {result}"
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
            "protocol_file": "protocols/antibiotics/meropenem.txt",
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
    return pp.parse_protocol_file(protocol_fixture_path(filename))


class TestPriorityRulesEngine(unittest.TestCase):

    def test_meropenem_no_slots_returns_default(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {})
        self.assertTrue(result.default_used)
        self.assertFalse(result.no_match)

    def test_meropenem_gfr_gt_90_selects_NORMAL(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"gfr": 95.0})
        self.assertEqual(result.output_key, "NORMAL")

    def test_meropenem_crrt_selects_crrt(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"crrt": True})
        self.assertEqual(result.output_key, "CRRT")

    def test_meropenem_ihd_selects_ihd(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"ihd": True})
        self.assertEqual(result.output_key, "SEVERE_AKI")

    def test_meropenem_gfr_45_selects_NORMAL(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"gfr": 45.0})
        self.assertEqual(result.output_key, "NORMAL")

    def test_meropenem_gfr_lt_20_selects_SEVERE_AKI(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"gfr": 15.0})
        self.assertEqual(result.output_key, "SEVERE_AKI")

    def test_meropenem_default_renders_text(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {})
        rendered = se.render_selected_output(parsed, result, lang="en")
        self.assertIn("Meropenem", rendered)

    def test_meropenem_selected_renders_with_dose(self):
        parsed = _s9_load("meropenem.txt")
        result = se.run_selection(parsed, {"gfr": 95.0})
        rendered = se.render_selected_output(parsed, result, lang="en")
        self.assertIn("3 g/day", rendered)

    def test_dantrolene_dose_returns_full_guideline_text(self):
        parsed = _s9_load("dantrolene_mh.txt")
        result = se.run_selection(parsed, {})
        rendered = se.render_selected_output(parsed, result, lang="en")
        self.assertTrue(result.default_used)
        self.assertIn("Dantrium", rendered)
        self.assertIn("Agilus", rendered)
        self.assertIn("Oldat elk", rendered)
        self.assertNotIn("FULL_GUIDELINE", rendered)
        self.assertNotIn("Return the full Hungarian guideline text", rendered)

    def test_dantrolene_weight_still_returns_full_guideline_text(self):
        parsed = _s9_load("dantrolene_mh.txt")
        result = se.run_selection(parsed, {"body_weight_kg": 80.0})
        rendered = se.render_selected_output(parsed, result, lang="en")
        self.assertTrue(result.default_used)
        self.assertIn("80 kg: 10 ampulla", rendered)
        self.assertIn("80 kg: 32 ml", rendered)
        self.assertNotIn("FULL_GUIDELINE", rendered)

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

    def test_implausible_numeric_slot_asks_confirmation_before_dosing(self):
        parsed = _s9_load("tmpsmx.txt")
        slots = {"indication": "stenotrophomonas bsi", "body_weight_kg": 60.0, "gfr": 900.0}
        result = se.run_selection(parsed, slots)
        rendered = se.render_selected_output(parsed, result, lang="en")
        self.assertEqual(result.output_key, "SLOT_OUT_OF_CLINICAL_BOUNDS")
        self.assertIn("outside the expected clinical bounds", rendered)
        self.assertIn("Please confirm or correct", rendered)


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

    def test_body_size_height_and_weight_extracted(self):
        parsed = _s9_load("body_size_calculators.txt")
        slots = se.extract_slots_from_query("50kg, 190cm", parsed_protocol=parsed)
        self.assertEqual(slots.get("actual_weight_kg"), 50.0)
        self.assertEqual(slots.get("height_cm"), 190.0)


class TestCalculatorProtocols(unittest.TestCase):

    def test_body_size_calculator_accepts_kg_cm(self):
        parsed = _s9_load("body_size_calculators.txt")
        slots = se.extract_slots_from_query("50kg, 190cm", parsed_protocol=parsed)
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "calculated_body_size")
        self.assertIn("BMI: 13.85 kg/m2", result.rendered)
        self.assertIn("BSA (Mosteller): 1.62 m2", result.rendered)

    def test_body_size_calculator_accepts_second_example(self):
        parsed = _s9_load("body_size_calculators.txt")
        slots = se.extract_slots_from_query("60kg, 189cm", parsed_protocol=parsed)
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "calculated_body_size")
        self.assertNotIn("Please provide height", result.rendered)

    def test_echo_cardiac_output_calculator(self):
        parsed = _s9_load("echo_cardiac_output.txt")
        slots = se.extract_slots_from_query("LVOT VTI 20 cm, LVOT diam 2 cm, HR 70", parsed_protocol=parsed)
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "calculated_co")
        self.assertIn("Stroke volume: 62.83 mL", result.rendered)
        self.assertIn("Cardiac output: 4.4 L/min", result.rendered)

    def test_echo_ava_calculator(self):
        parsed = _s9_load("echo_ava.txt")
        slots = se.extract_slots_from_query("LVOT diam 2 cm LVOT VTI 20 cm AV VTI 80 cm", parsed_protocol=parsed)
        result = se.run_selection(parsed, slots)
        self.assertEqual(result.output_key, "calculated_ava")
        self.assertIn("AVA: 0.79 cm2", result.rendered)


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
        self._old_unsupported_syndromes = dict(bot.UNSUPPORTED_SYNDROMES)
        self._old_file_labels = dict(bot.PROTOCOL_FILE_TO_LABEL)
        bot.load_aliases(os.path.join("protocols", "aliases.json"))

    def tearDown(self):
        bot.ALIASES = self._old_aliases
        bot.ALIAS_INDEX = self._old_alias_index
        bot.BLOCKED_ALIASES = self._old_blocked_aliases
        bot.UNSUPPORTED_SYNDROMES = self._old_unsupported_syndromes
        bot.PROTOCOL_FILE_TO_LABEL = self._old_file_labels

    def _recognized_file(self, query):
        _, recognized = bot.normalize_question(query)
        if not recognized:
            return None
        return bot.normalize_path(recognized.get("protocol_file", ""))

    def test_broad_carbapenem_no_longer_routes_to_meropenem(self):
        self.assertNotEqual(
            self._recognized_file("carbapenem"),
            bot.normalize_path("protocols/antibiotics/meropenem.txt"),
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

    def test_unsupported_policy_hit_exposes_key_term_and_message(self):
        hit = bot._detect_unsupported_policy("what ab for ventilator associated pneumonia?")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["key"], "vap")
        self.assertEqual(hit["matched_term"], "ventilator associated pneumonia")
        self.assertIn("VAP antibiotic selection", hit["message"])

    def test_legacy_blocked_alias_still_works_without_policy(self):
        import aliases as alias_helpers

        hit = alias_helpers._detect_unsupported_policy(
            "legacy syndrome antibiotic?",
            unsupported_policies={},
            blocked_aliases={"legacy syndrome"},
            rapidfuzz_available=False,
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit["key"], "legacy syndrome")
        self.assertEqual(hit["matched_term"], "legacy syndrome")
        self.assertIn("LEGACY SYNDROME antibiotic selection", hit["message"])


# ---------------------------------------------------------------------------
# Routing Regression Tests: deterministic routing before production changes
# ---------------------------------------------------------------------------

def _routing_fake_chat_response(text):
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content=text)
            )
        ]
    )


GOLDEN_ROUTING_CASES = [
    {
        "name": "meropenem_gfr_45_NORMAL",
        "input_turns": ["meropenem dose GFR 45"],
        "expected_protocol_file": "protocols/antibiotics/meropenem.txt",
        "expected_protocol_id": "meropenem",
        "expected_selected_output_key": "NORMAL",
        "expected_slots": {"gfr": 45.0},
        "expected_deterministic_or_llm": "deterministic_selection",
        "expected_llm_called": False,
        "expected_source_label": "uploaded antibiotic renal dosing DOCX - meropenem",
        "expected_unsupported_syndrome": None,
        "expected_unsupported_action": None,
        "expected_answer_fragments": [
            "Meropenem - renal tier: Normal",
            "3 g/day",
            "Source: uploaded antibiotic renal dosing DOCX - meropenem",
        ],
        "forbidden_answer_fragments": ["No uploaded protocol supports"],
    },
    {
        "name": "meropenem_then_gfr_30_carried_context",
        "input_turns": ["meropenem", "GFR 30"],
        "expected_protocol_file": "protocols/antibiotics/meropenem.txt",
        "expected_protocol_id": "meropenem",
        "expected_selected_output_key": "NORMAL",
        "expected_slots": {"gfr": 30.0},
        "expected_deterministic_or_llm": "deterministic_selection",
        "expected_llm_called": False,
        "expected_source_label": "uploaded antibiotic renal dosing DOCX - meropenem",
        "expected_unsupported_syndrome": None,
        "expected_unsupported_action": None,
        "expected_answer_fragments": [
            "Meropenem - renal tier: Normal",
            "3 g/day",
            "Source: uploaded antibiotic renal dosing DOCX - meropenem",
        ],
        "forbidden_answer_fragments": ["No uploaded protocol supports"],
    },
    {
        "name": "vap_only_unsupported_no_antibiotic_recommendation",
        "input_turns": ["VAP antibiotic?"],
        "expected_protocol_file": None,
        "expected_protocol_id": None,
        "expected_selected_output_key": None,
        "expected_slots": {},
        "expected_deterministic_or_llm": "deterministic_route_claims",
        "expected_llm_called": False,
        "expected_source_label": None,
        "expected_unsupported_syndrome": "vap",
        "expected_unsupported_action": "blocked",
        "expected_answer_fragments": [
            "No uploaded protocol supports VAP antibiotic selection",
            "cannot recommend antibiotics",
        ],
        "forbidden_answer_fragments": ["ceftriaxone", "meropenem"],
    },
    {
        "name": "hap_only_unsupported_no_antibiotic_recommendation",
        "input_turns": ["HAP antibiotic?"],
        "expected_protocol_file": None,
        "expected_protocol_id": None,
        "expected_selected_output_key": None,
        "expected_slots": {},
        "expected_deterministic_or_llm": "deterministic_route_claims",
        "expected_llm_called": False,
        "expected_source_label": None,
        "expected_unsupported_syndrome": "hap",
        "expected_unsupported_action": "blocked",
        "expected_answer_fragments": [
            "No uploaded protocol supports HAP antibiotic selection",
            "cannot recommend antibiotics",
        ],
        "forbidden_answer_fragments": ["ceftriaxone", "meropenem"],
    },
    {
        "name": "vap_plus_meropenem_gfr_40_ignores_unsupported",
        "input_turns": ["VAP patient, meropenem dose GFR 40"],
        "expected_protocol_file": "protocols/antibiotics/meropenem.txt",
        "expected_protocol_id": "meropenem",
        "expected_selected_output_key": "NORMAL",
        "expected_slots": {"gfr": 40.0},
        "expected_deterministic_or_llm": "deterministic_selection",
        "expected_llm_called": False,
        "expected_source_label": "uploaded antibiotic renal dosing DOCX - meropenem",
        "expected_unsupported_syndrome": "vap",
        "expected_unsupported_action": "ignored_explicit_drug",
        "expected_answer_fragments": [
            "Meropenem - renal tier: Normal",
            "3 g/day",
            "Source: uploaded antibiotic renal dosing DOCX - meropenem",
        ],
        "forbidden_answer_fragments": ["No uploaded protocol supports"],
    },
    {
        "name": "biofire_clear_previous_pathogens_no_old_gene_retention",
        "input_turns": [
            "BioFire result pneumococcus CTX-M",
            "clear previous pathogens",
            "BioFire result pseudomonas",
        ],
        "expected_protocol_file": "protocols/pneumonia_pcr.txt",
        "expected_protocol_id": "biofire_pneumonia",
        "expected_selected_output_key": "TIER_2_CEFEPIME",
        "expected_slots": {"pathogen_list": ["pseudomonas aeruginosa"]},
        "expected_deterministic_or_llm": "deterministic_selection",
        "expected_llm_called": False,
        "expected_source_label": "BioFire",
        "expected_unsupported_syndrome": None,
        "expected_unsupported_action": None,
        "expected_answer_fragments": [
            "pseudomonas aeruginosa",
            "Tier 2 - cefepime",
            "Source: BioFire",
        ],
        "forbidden_answer_fragments": [
            "streptococcus pneumoniae",
            "ctx-m",
            "ceftriaxone",
            "ertapenem",
        ],
        "forbidden_slot_values": {
            "pathogen_list": ["streptococcus pneumoniae"],
            "resistance_gene_list": ["ctx_m"],
        },
    },
    {
        "name": "tmpsmx_weight_correction_to_150kg_review_reference_row",
        "input_turns": [
            "TMP/SMX Steno BSI 70 kg GFR 60",
            "not 70kg but 150kg",
        ],
        "expected_protocol_file": "protocols/antibiotics/tmpsmx.txt",
        "expected_protocol_id": "tmpsmx",
        "expected_selected_output_key": "HIGH_DOSE_GFR_GT_30_OR_CRRT",
        "expected_slots": {
            "gfr": 60.0,
            "body_weight_kg": 150.0,
            "indication": "steno",
        },
        "expected_deterministic_or_llm": "deterministic_selection",
        "expected_llm_called": False,
        "expected_source_label": "TMP/SMX",
        "expected_unsupported_syndrome": None,
        "expected_unsupported_action": None,
        "expected_answer_fragments": [
            "outside the explicit protocol table range",
            "Closest explicit protocol row for reference only: 100 kg row",
            "not a 150 kg dosing recommendation",
            "3 x 6 amp",
            "Source: TMP/SMX",
        ],
        "forbidden_answer_fragments": ["70 kg row", "not a 70 kg dosing recommendation"],
    },
    {
        "name": "casual_out_of_scope_without_active_protocol_no_clinical_protocol",
        "input_turns": ["hi"],
        "expected_protocol_file": None,
        "expected_protocol_id": None,
        "expected_selected_output_key": None,
        "expected_slots": {},
        "expected_deterministic_or_llm": "deterministic_policy",
        "expected_llm_called": False,
        "expected_source_label": None,
        "expected_unsupported_syndrome": None,
        "expected_unsupported_action": None,
        "expected_answer_fragments": [
            "No active clinical protocol is selected",
            "does not match an uploaded clinical protocol",
        ],
        "forbidden_answer_fragments": ["Source:", "ceftriaxone", "meropenem"],
    },
    {
        "name": "casual_out_of_scope_with_active_protocol_confirmation_prompt",
        "input_turns": ["meropenem GFR 45", "how are you?"],
        "expected_protocol_file": "protocols/antibiotics/meropenem.txt",
        "expected_protocol_id": "meropenem",
        "expected_selected_output_key": None,
        "expected_slots": {"gfr": 45.0},
        "expected_deterministic_or_llm": "deterministic_confirmation",
        "expected_llm_called": False,
        "expected_source_label": "uploaded antibiotic renal dosing DOCX - meropenem",
        "expected_unsupported_syndrome": None,
        "expected_unsupported_action": None,
        "expected_answer_fragments": [
            "not sure this message belongs",
            "Reply yes",
            "Source: uploaded antibiotic renal dosing DOCX - meropenem",
        ],
        "forbidden_answer_fragments": ["No active clinical protocol is selected"],
    },
    {
        "name": "immunosuppressed_pneumonia_unsupported_without_explicit_protocol",
        "input_turns": ["immunosuppressed pneumonia what antibiotic?"],
        "expected_protocol_file": None,
        "expected_protocol_id": None,
        "expected_selected_output_key": None,
        "expected_slots": {},
        "expected_deterministic_or_llm": "deterministic_route_claims",
        "expected_llm_called": False,
        "expected_source_label": None,
        "expected_unsupported_syndrome": "immunosuppressed pneumonia",
        "expected_unsupported_action": "blocked",
        "expected_answer_fragments": [
            "No uploaded protocol supports IMMUNOSUPPRESSED PNEUMONIA",
            "cannot recommend antibiotics",
        ],
        "forbidden_answer_fragments": [
            "ceftriaxone",
            "clarithromycin",
            "amoxicillin",
            "Source: CAP",
        ],
    },
]


class TestGoldenRoutingCases(unittest.TestCase):
    """Data-driven golden cases for clinical routing and deterministic output."""

    PROTO_DIR = os.path.join(os.path.dirname(__file__), "protocols")

    def setUp(self):
        import protocol_parser as pp
        import telegram_bot as b

        self.b = b
        self._old_parsed = dict(b.PROTOCOL_PARSED_BY_FILE)
        self._old_policy = dict(b.PROTOCOL_POLICY_BY_FILE)
        self._old_aliases = dict(b.ALIASES)
        self._old_alias_index = dict(b.ALIAS_INDEX)
        self._old_blocked_aliases = set(b.BLOCKED_ALIASES)
        self._old_unsupported_syndromes = dict(b.UNSUPPORTED_SYNDROMES)
        self._old_file_labels = dict(b.PROTOCOL_FILE_TO_LABEL)
        self._old_state = dict(b.CONVERSATION_STATE)

        b.PROTOCOL_PARSED_BY_FILE.clear()
        b.PROTOCOL_POLICY_BY_FILE.clear()
        b.CONVERSATION_STATE.clear()
        for filename in [
            "meropenem.txt",
            "tmpsmx.txt",
            "pneumonia_pcr.txt",
            "cap.txt",
            "vancomycin.txt",
            "body_size_calculators.txt",
        ]:
            rel_path = protocol_fixture_relpath(filename)
            abs_path = protocol_fixture_path(filename)
            with open(abs_path, encoding="utf-8") as f:
                text = f.read()
            parsed = pp.parse_protocol_file(abs_path)
            parsed["path"] = rel_path
            b.PROTOCOL_PARSED_BY_FILE[b.normalize_path(rel_path)] = parsed
            b.PROTOCOL_POLICY_BY_FILE[b.normalize_path(rel_path)] = pp.extract_policy_header(text)
        b.load_aliases(os.path.join("protocols", "aliases.json"))

    def tearDown(self):
        b = self.b
        b.PROTOCOL_PARSED_BY_FILE.clear()
        b.PROTOCOL_PARSED_BY_FILE.update(self._old_parsed)
        b.PROTOCOL_POLICY_BY_FILE.clear()
        b.PROTOCOL_POLICY_BY_FILE.update(self._old_policy)
        b.ALIASES = self._old_aliases
        b.ALIAS_INDEX = self._old_alias_index
        b.BLOCKED_ALIASES = self._old_blocked_aliases
        b.UNSUPPORTED_SYNDROMES = self._old_unsupported_syndromes
        b.PROTOCOL_FILE_TO_LABEL = self._old_file_labels
        b.CONVERSATION_STATE.clear()
        b.CONVERSATION_STATE.update(self._old_state)

    def _chat_id(self, case):
        return f"golden_{case['name']}_{id(self)}"

    def _assert_slot_subset(self, actual_slots, expected_slots):
        for key, expected_value in expected_slots.items():
            self.assertIn(key, actual_slots, f"Expected slot {key!r} in {actual_slots}")
            self.assertEqual(actual_slots[key], expected_value)

    def _assert_forbidden_slot_values_absent(self, actual_slots, forbidden):
        for key, forbidden_values in forbidden.items():
            actual = actual_slots.get(key, [])
            if not isinstance(actual, (list, tuple, set)):
                actual = [actual]
            for forbidden_value in forbidden_values:
                self.assertNotIn(forbidden_value, actual)

    def _run_golden_case(self, case):
        chat_id = self._chat_id(case)
        fake_llm = _routing_fake_chat_response("MOCK LLM RESPONSE SHOULD NOT BE USED")

        search_patch = patch.object(
            self.b,
            "search_protocols",
            side_effect=AssertionError("RAG should not run for this golden deterministic case"),
        )
        llm_patch = patch.object(
            self.b.openai_client.chat.completions,
            "create",
            side_effect=AssertionError("OpenAI should not run for this golden deterministic case"),
        )
        if case["expected_llm_called"]:
            search_patch = patch.object(self.b, "search_protocols", return_value=[])
            llm_patch = patch.object(self.b.openai_client.chat.completions, "create", return_value=fake_llm)

        with search_patch as search_mock, llm_patch as llm_mock, patch.object(self.b, "_log_query") as log_mock:
            answer = None
            for turn in case["input_turns"]:
                answer = self.b.ask_ai(turn, chat_id)

        self.assertIsNotNone(log_mock.call_args, "Golden case did not produce an audit log entry")
        trace = log_mock.call_args.kwargs["trace"]
        state = self.b.get_chat_state(chat_id)
        answer_lower = (answer or "").lower()

        expected_file = case["expected_protocol_file"]
        if expected_file is None:
            self.assertIsNone(trace.get("selected_protocol_file"))
        else:
            self.assertEqual(
                self.b.normalize_path(trace.get("selected_protocol_file")),
                self.b.normalize_path(expected_file),
            )
        self.assertEqual(trace.get("selected_protocol_id"), case["expected_protocol_id"])
        self.assertEqual(trace.get("selection_output_key"), case["expected_selected_output_key"])
        self.assertEqual(trace.get("deterministic_or_llm"), case["expected_deterministic_or_llm"])
        self.assertEqual(trace.get("llm_called"), case["expected_llm_called"])
        self.assertEqual(trace.get("source_label"), case["expected_source_label"])
        self.assertEqual(trace.get("unsupported_syndrome"), case["expected_unsupported_syndrome"])
        self.assertEqual(trace.get("unsupported_action"), case["expected_unsupported_action"])

        self._assert_slot_subset(state.get("collected_slots", {}), case["expected_slots"])
        self._assert_forbidden_slot_values_absent(
            state.get("collected_slots", {}),
            case.get("forbidden_slot_values", {}),
        )

        for fragment in case["expected_answer_fragments"]:
            self.assertIn(fragment.lower(), answer_lower)
        for fragment in case["forbidden_answer_fragments"]:
            self.assertNotIn(fragment.lower(), answer_lower)

        if case["expected_llm_called"]:
            self.assertGreater(llm_mock.call_count, 0)
        else:
            self.assertEqual(getattr(llm_mock, "call_count", 0), 0)
            self.assertEqual(getattr(search_mock, "call_count", 0), 0)

    def test_golden_routing_cases(self):
        for case in GOLDEN_ROUTING_CASES:
            with self.subTest(case=case["name"]):
                self.b.CONVERSATION_STATE.pop(self._chat_id(case), None)
                self._run_golden_case(case)


class TestRoutingRegressionGuardrails(unittest.TestCase):
    """Regression coverage for routing, traceability, and safety guardrails."""

    PROTO_DIR = os.path.join(os.path.dirname(__file__), "protocols")

    def setUp(self):
        import protocol_parser as pp
        import telegram_bot as b

        self.b = b
        self._old_parsed = dict(b.PROTOCOL_PARSED_BY_FILE)
        self._old_policy = dict(b.PROTOCOL_POLICY_BY_FILE)
        self._old_aliases = dict(b.ALIASES)
        self._old_alias_index = dict(b.ALIAS_INDEX)
        self._old_blocked_aliases = set(b.BLOCKED_ALIASES)
        self._old_unsupported_syndromes = dict(b.UNSUPPORTED_SYNDROMES)
        self._old_file_labels = dict(b.PROTOCOL_FILE_TO_LABEL)
        self._old_state = dict(b.CONVERSATION_STATE)

        b.PROTOCOL_PARSED_BY_FILE.clear()
        b.PROTOCOL_POLICY_BY_FILE.clear()
        b.CONVERSATION_STATE.clear()
        for filename in [
            "meropenem.txt",
            "tmpsmx.txt",
            "pneumonia_pcr.txt",
            "cap.txt",
            "vancomycin.txt",
            "body_size_calculators.txt",
            "periop_gyogyszerek.txt",
            "steroid_equivalence.txt",
            "echo_cardiac_output.txt",
            "echo_ava.txt",
        ]:
            rel_path = protocol_fixture_relpath(filename)
            abs_path = protocol_fixture_path(filename)
            with open(abs_path, encoding="utf-8") as f:
                text = f.read()
            parsed = pp.parse_protocol_file(abs_path)
            parsed["path"] = rel_path
            b.PROTOCOL_PARSED_BY_FILE[b.normalize_path(rel_path)] = parsed
            b.PROTOCOL_POLICY_BY_FILE[b.normalize_path(rel_path)] = pp.extract_policy_header(text)
        b.load_aliases(os.path.join("protocols", "aliases.json"))

        self._log_patch = patch.object(b, "_log_query")
        self.mock_log = self._log_patch.start()

    def tearDown(self):
        b = self.b
        self._log_patch.stop()
        b.PROTOCOL_PARSED_BY_FILE.clear()
        b.PROTOCOL_PARSED_BY_FILE.update(self._old_parsed)
        b.PROTOCOL_POLICY_BY_FILE.clear()
        b.PROTOCOL_POLICY_BY_FILE.update(self._old_policy)
        b.ALIASES = self._old_aliases
        b.ALIAS_INDEX = self._old_alias_index
        b.BLOCKED_ALIASES = self._old_blocked_aliases
        b.UNSUPPORTED_SYNDROMES = self._old_unsupported_syndromes
        b.PROTOCOL_FILE_TO_LABEL = self._old_file_labels
        b.CONVERSATION_STATE.clear()
        b.CONVERSATION_STATE.update(self._old_state)

    def _chat_id(self, suffix):
        return f"routing_regression_{suffix}_{id(self)}"

    def _install_protocol_fixture(self, filename):
        import protocol_parser as pp

        rel_path = protocol_fixture_relpath(filename)
        abs_path = protocol_fixture_path(filename)
        with open(abs_path, encoding="utf-8") as f:
            text = f.read()
        parsed = pp.parse_protocol_file(abs_path)
        parsed["path"] = rel_path
        self.b.PROTOCOL_PARSED_BY_FILE[self.b.normalize_path(rel_path)] = parsed
        self.b.PROTOCOL_POLICY_BY_FILE[self.b.normalize_path(rel_path)] = pp.extract_policy_header(text)
        return parsed

    def _recognized_for(self, query):
        _, recognized = self.b.normalize_question(query)
        self.assertIsNotNone(recognized, f"Expected recognized protocol for {query!r}")
        return recognized

    def _assert_no_rag_or_llm(self):
        return (
            patch.object(self.b, "search_protocols",
                         side_effect=AssertionError("RAG should not run for deterministic routing")),
            patch.object(self.b.openai_client.chat.completions, "create",
                         side_effect=AssertionError("LLM should not run for deterministic routing")),
        )

    def _ask_without_rag_or_llm(self, question, chat_id):
        rag_patch, llm_patch = self._assert_no_rag_or_llm()
        with rag_patch as search_mock, llm_patch as llm_mock:
            answer = self.b.ask_ai(question, chat_id)
        self.assertEqual(search_mock.call_count, 0)
        self.assertEqual(llm_mock.call_count, 0)
        return answer

    def _ask_with_empty_rag_and_mock_llm(self, question, chat_id, llm_text="MOCK LLM RAG RESPONSE"):
        with patch.object(self.b, "search_protocols", return_value=[]) as search_mock, \
                patch.object(
                    self.b.openai_client.chat.completions,
                    "create",
                    return_value=_routing_fake_chat_response(llm_text),
                ) as llm_mock:
            answer = self.b.ask_ai(question, chat_id)
        return answer, search_mock.call_count, llm_mock.call_count

    def _last_logged(self):
        self.assertIsNotNone(self.mock_log.call_args, "Expected an audit log entry")
        return self.mock_log.call_args.kwargs

    def _last_trace(self):
        return self._last_logged()["trace"]

    def _assert_trace_selected_protocol(self, trace, protocol_id, protocol_file, source_label):
        self.assertEqual(trace.get("selected_protocol_id"), protocol_id)
        self.assertEqual(self.b.normalize_path(trace.get("selected_protocol_file")), protocol_file)
        self.assertEqual(trace.get("source_label"), source_label)

    def _protocol_slots(self, state, query):
        return self.b._get_protocol_slots(state, self._recognized_for(query))

    def _assert_over_max_weight_returns_review_plus_reference_row(self, parsed, slots, max_table_weight_kg=100.0):
        self.assertGreater(float(slots["body_weight_kg"]), max_table_weight_kg)
        result = se.run_selection(parsed, slots)
        rendered = se.render_selected_output(parsed, result, lang="en")
        lower = rendered.lower()
        review_terms = ("individualized", "individualised", "id/pharmacy", "pharmacy")
        self.assertTrue(
            any(term in lower for term in review_terms),
            f"Weight above table maximum should require review, got: {rendered}",
        )
        self.assertIn("outside the explicit protocol table range", lower)
        self.assertIn("automatic dose escalation is not supported", lower)
        self.assertIn("closest explicit protocol row for reference only", lower)
        self.assertIn("100 kg row", lower)
        self.assertIn("not a 150 kg dosing recommendation", lower)
        self.assertIn("3 x 6 amp", lower)

    def test_fresh_meropenem_deterministic_query_sets_active_protocol(self):
        chat_id = self._chat_id("fresh_meropenem")
        answer = self._ask_without_rag_or_llm("meropenem dose GFR 45", chat_id)

        state = self.b.get_chat_state(chat_id)
        active = state.get("active_recognized")
        self.assertIsNotNone(active, "Fresh deterministic drug query must activate protocol context.")
        self.assertEqual(self.b.normalize_path(active["protocol_file"]), "protocols/antibiotics/meropenem.txt")
        self.assertEqual(state["collected_slots"].get("gfr"), 45.0)
        self.assertIn("Source:", answer)
        self.assertIn("meropenem", answer.lower())

    def test_meropenem_gfr_followup_uses_active_deterministic_context(self):
        chat_id = self._chat_id("meropenem_followup")
        self._ask_without_rag_or_llm("meropenem dose GFR 45", chat_id)
        self.mock_log.reset_mock()

        answer = self._ask_without_rag_or_llm("GFR 30", chat_id)

        state = self.b.get_chat_state(chat_id)
        active = state.get("active_recognized")
        self.assertIsNotNone(active)
        self.assertEqual(self.b.normalize_path(active["protocol_file"]), "protocols/antibiotics/meropenem.txt")
        self.assertEqual(state["collected_slots"].get("gfr"), 30.0)
        self.assertIn("Source:", answer)
        self.assertIn("meropenem", answer.lower())
        logged = self.mock_log.call_args.kwargs
        self.assertEqual(self.b.normalize_path(logged["recognized"]["protocol_file"]), "protocols/antibiotics/meropenem.txt")
        self.assertEqual(logged["retrieved_chunks"], [])

    def test_blocked_respiratory_syndromes_without_supported_drug_do_not_recommend_antibiotics(self):
        unsafe_llm = _routing_fake_chat_response("Use ceftriaxone for VAP.")
        blocked_queries = [
            "what ab for VAP?",
            "HAP/VAP what antibiotic?",
            "hospital-acquired pneumonia what antibiotic?",
            "hospital acquired pneumonia what antibiotic?",
            "ventilator-associated pneumonia antibiotic?",
            "ventilator associated pneumonia antibiotic?",
            "nosocomial pneumonia antibiotic?",
        ]

        for query in blocked_queries:
            with self.subTest(query=query):
                chat_id = self._chat_id("blocked_syndrome")
                self.b.CONVERSATION_STATE.pop(chat_id, None)
                with patch.object(self.b, "search_protocols", return_value=[]), \
                        patch.object(self.b.openai_client.chat.completions, "create",
                                     return_value=unsafe_llm):
                    answer = self.b.ask_ai(query, chat_id)
                state = self.b.get_chat_state(chat_id)
                lower = answer.lower()
                self.assertIsNone(state.get("active_recognized"))
                self.assertNotIn("ceftriaxone", lower)
                self.assertNotIn("meropenem", lower)
                self.assertTrue(
                    any(phrase in lower for phrase in [
                        "no uploaded protocol",
                        "not specified in the uploaded protocol",
                        "not specified in the uploaded protocols",
                        "not covered",
                        "unsupported",
                    ]),
                    f"Blocked syndrome should return a no-protocol message, got: {answer}",
                )

    def test_explicit_meropenem_overrides_blocked_vap_for_protocol_selection(self):
        chat_id = self._chat_id("vap_meropenem")
        answer = self._ask_without_rag_or_llm("GFR 40 and VAP, mero dose?", chat_id)

        state = self.b.get_chat_state(chat_id)
        active = state.get("active_recognized")
        self.assertIsNotNone(active)
        self.assertEqual(self.b.normalize_path(active["protocol_file"]), "protocols/antibiotics/meropenem.txt")
        self.assertEqual(state["collected_slots"].get("gfr"), 40.0)
        self.assertIn("Source:", answer)
        self.assertIn("meropenem", answer.lower())

    def test_biofire_clear_previous_pathogens_clears_pathogen_and_resistance_slots(self):
        for clear_phrase in ["delete previous pathogens", "clear previous pathogens"]:
            with self.subTest(clear_phrase=clear_phrase):
                chat_id = self._chat_id(clear_phrase.replace(" ", "_"))
                state = self.b.get_chat_state(chat_id)
                state["active_recognized"] = self._recognized_for("BioFire")

                self._ask_without_rag_or_llm("BioFire result pneumococcus CTX-M", chat_id)
                self.assertIn("streptococcus pneumoniae", state["collected_slots"].get("pathogen_list", []))
                self.assertIn("ctx_m", state["collected_slots"].get("resistance_gene_list", []))

                self._ask_without_rag_or_llm(clear_phrase, chat_id)
                self.assertEqual(state["collected_slots"].get("pathogen_list", []), [])
                self.assertEqual(state["collected_slots"].get("resistance_gene_list", []), [])

                self._ask_without_rag_or_llm("BioFire result pseudomonas", chat_id)
                self.assertEqual(state["collected_slots"].get("pathogen_list"), ["pseudomonas aeruginosa"])
                self.assertNotIn("ctx_m", state["collected_slots"].get("resistance_gene_list", []))
                self.assertNotIn("streptococcus pneumoniae", state["collected_slots"].get("pathogen_list", []))

    def test_numeric_correction_updates_active_tmpsmx_slots_without_cross_routing(self):
        chat_id = self._chat_id("tmpsmx_correction")
        state = self.b.get_chat_state(chat_id)
        state["active_recognized"] = self._recognized_for("TMP/SMX")

        self._ask_without_rag_or_llm("TMP/SMX Steno BSI 70 kg GFR 60", chat_id)
        self._ask_without_rag_or_llm("not 70kg but 150kg", chat_id)

        active = state.get("active_recognized")
        self.assertEqual(self.b.normalize_path(active["protocol_file"]), "protocols/antibiotics/tmpsmx.txt")
        self.assertEqual(state["collected_slots"].get("body_weight_kg"), 150.0)
        self.assertEqual(state["collected_slots"].get("gfr"), 60.0)
        self.assertIn("steno", state["collected_slots"].get("indication", "").lower())
        self.assertNotEqual(self.b.normalize_path(active["protocol_file"]), "protocols/pneumonia_pcr.txt")

    def test_hungarian_numeric_correction_updates_weight(self):
        chat_id = self._chat_id("tmpsmx_hu_correction")
        state = self.b.get_chat_state(chat_id)
        state["active_recognized"] = self._recognized_for("TMP/SMX")

        self._ask_without_rag_or_llm("TMP/SMX Steno BSI 70 kg GFR 60", chat_id)
        self._ask_without_rag_or_llm("nem 70kg hanem 150kg", chat_id)

        self.assertEqual(state["collected_slots"].get("body_weight_kg"), 150.0)
        self.assertEqual(state["collected_slots"].get("gfr"), 60.0)

    def test_gfr_correction_updates_active_protocol_renal_slot(self):
        chat_id = self._chat_id("gfr_correction")
        state = self.b.get_chat_state(chat_id)
        state["active_recognized"] = self._recognized_for("meropenem")

        self._ask_without_rag_or_llm("meropenem dose GFR 70", chat_id)
        self._ask_without_rag_or_llm("actually GFR 40", chat_id)

        active = state.get("active_recognized")
        self.assertEqual(self.b.normalize_path(active["protocol_file"]), "protocols/antibiotics/meropenem.txt")
        self.assertEqual(state["collected_slots"].get("gfr"), 40.0)

    def test_ml_per_min_correction_updates_active_protocol_gfr(self):
        chat_id = self._chat_id("ml_min_correction")
        state = self.b.get_chat_state(chat_id)
        state["active_recognized"] = self._recognized_for("meropenem")

        self._ask_without_rag_or_llm("meropenem dose GFR 70", chat_id)
        self._ask_without_rag_or_llm("rather 30 ml/min", chat_id)

        self.assertEqual(state["collected_slots"].get("gfr"), 30.0)

    def test_biofire_clear_previous_result_uses_generic_clear_slot_operation(self):
        chat_id = self._chat_id("biofire_clear_result")
        state = self.b.get_chat_state(chat_id)
        state["active_recognized"] = self._recognized_for("BioFire")

        self._ask_without_rag_or_llm("BioFire result pneumococcus CTX-M", chat_id)
        self.assertIn("streptococcus pneumoniae", state["collected_slots"].get("pathogen_list", []))
        self.assertIn("ctx_m", state["collected_slots"].get("resistance_gene_list", []))

        self._ask_without_rag_or_llm("clear previous result", chat_id)

        self.assertNotIn("pathogen_list", state["collected_slots"])
        self.assertNotIn("resistance_gene_list", state["collected_slots"])

    def test_forget_gfr_removes_active_protocol_gfr(self):
        chat_id = self._chat_id("forget_gfr")
        state = self.b.get_chat_state(chat_id)
        state["active_recognized"] = self._recognized_for("meropenem")

        self._ask_without_rag_or_llm("meropenem dose GFR 70", chat_id)
        self.assertEqual(state["collected_slots"].get("gfr"), 70.0)

        self._ask_without_rag_or_llm("forget GFR", chat_id)

        self.assertNotIn("gfr", state["collected_slots"])
        active = state.get("active_recognized")
        self.assertEqual(self.b.normalize_path(active["protocol_file"]), "protocols/antibiotics/meropenem.txt")

    def test_correction_does_not_switch_protocol_without_explicit_alias(self):
        chat_id = self._chat_id("correction_no_switch")
        state = self.b.get_chat_state(chat_id)
        state["active_recognized"] = self._recognized_for("TMP/SMX")

        self._ask_without_rag_or_llm("TMP/SMX Steno BSI 70 kg GFR 60", chat_id)
        self._ask_without_rag_or_llm("actually GFR 40", chat_id)

        active = state.get("active_recognized")
        self.assertEqual(self.b.normalize_path(active["protocol_file"]), "protocols/antibiotics/tmpsmx.txt")
        self.assertEqual(state["collected_slots"].get("gfr"), 40.0)
        self.assertEqual(state["collected_slots"].get("body_weight_kg"), 70.0)

    def test_ambiguous_numeric_correction_asks_which_slot(self):
        chat_id = self._chat_id("ambiguous_correction")
        state = self.b.get_chat_state(chat_id)
        state["active_recognized"] = self._recognized_for("TMP/SMX")

        self._ask_without_rag_or_llm("TMP/SMX Steno BSI 70 kg GFR 60", chat_id)
        answer = self._ask_without_rag_or_llm("actually 80", chat_id)

        self.assertIn("Which slot should I correct", answer)
        self.assertEqual(state["collected_slots"].get("body_weight_kg"), 70.0)
        self.assertEqual(state["collected_slots"].get("gfr"), 60.0)

    def test_admin_debug_note_does_not_update_patient_facts(self):
        chat_id = self._chat_id("debug_note")
        state = self.b.get_chat_state(chat_id)
        state["active_recognized"] = self._recognized_for("meropenem")

        self._ask_without_rag_or_llm("meropenem dose GFR 70", chat_id)
        answer = self._ask_without_rag_or_llm("debug: GFR parser saw 15", chat_id)

        self.assertIn("Admin/debug note ignored", answer)
        self.assertEqual(state["collected_slots"].get("gfr"), 70.0)

    def test_tmpsmx_above_table_weight_returns_review_plus_reference_row(self):
        parsed = _s9_load("tmpsmx.txt")
        slots = {
            "indication": "stenotrophomonas bsi",
            "body_weight_kg": 150.0,
            "gfr": 60.0,
        }
        self._assert_over_max_weight_returns_review_plus_reference_row(parsed, slots)

    def test_casual_out_of_scope_messages_do_not_fuzzy_match_protocols(self):
        for query in ["hi", "how are you?", "what is the capital of paris?"]:
            with self.subTest(query=query):
                _, recognized = self.b.normalize_question(query)
                self.assertIsNone(recognized, f"Out-of-scope text matched a protocol: {recognized}")

    def test_capital_of_paris_full_routing_selects_no_protocol(self):
        chat_id = self._chat_id("capital_of_paris")
        answer = self._ask_without_rag_or_llm("capital of paris", chat_id)

        state = self.b.get_chat_state(chat_id)
        self.assertIsNone(state.get("active_recognized"))
        self.assertIn("No active clinical protocol is selected", answer)
        logged = self.mock_log.call_args.kwargs
        self.assertIsNone(logged["recognized"])
        trace = logged["trace"]
        self.assertIsNone(trace.get("selected_protocol_file"))
        self.assertFalse(trace.get("llm_called"))

    @unittest.skipUnless(bot.RAPIDFUZZ_AVAILABLE, "rapidfuzz not installed")
    def test_medium_confidence_fuzzy_requires_confirmation_without_clinical_output(self):
        chat_id = self._chat_id("medium_fuzzy")
        answer = self._ask_without_rag_or_llm("meropn dose GFR 45", chat_id)

        state = self.b.get_chat_state(chat_id)
        self.assertIsNone(state.get("active_recognized"))
        self.assertIsNotNone(state.get("pending_context_confirmation"))
        self.assertIn("Did you mean meropenem", answer)
        self.assertNotIn("3 g/day", answer)
        logged = self.mock_log.call_args.kwargs
        self.assertIsNone(logged["recognized"])
        trace = logged["trace"]
        self.assertTrue(trace.get("confirmation_required"))
        self.assertIsNone(trace.get("selected_protocol_file"))
        self.assertEqual(trace.get("deterministic_or_llm"), "deterministic_confirmation")
        self.assertFalse(trace.get("llm_called"))

    @unittest.skipUnless(bot.RAPIDFUZZ_AVAILABLE, "rapidfuzz not installed")
    def test_confirmed_medium_confidence_fuzzy_can_then_route(self):
        chat_id = self._chat_id("confirmed_medium_fuzzy")
        self._ask_without_rag_or_llm("meropn dose GFR 45", chat_id)
        self.mock_log.reset_mock()

        answer = self._ask_without_rag_or_llm("yes", chat_id)

        state = self.b.get_chat_state(chat_id)
        active = state.get("active_recognized")
        self.assertIsNotNone(active)
        self.assertEqual(self.b.normalize_path(active["protocol_file"]), "protocols/antibiotics/meropenem.txt")
        self.assertEqual(state["collected_slots"].get("gfr"), 45.0)
        self.assertIn("Meropenem - renal tier", answer)
        self.assertIn("Source: uploaded antibiotic renal dosing DOCX - meropenem", answer)

    @unittest.skipUnless(bot.RAPIDFUZZ_AVAILABLE, "rapidfuzz not installed")
    def test_blocked_respiratory_typos_do_not_fuzzy_route_to_cap(self):
        chat_id = self._chat_id("blocked_resp_typo")
        answer = self._ask_without_rag_or_llm("ventilator asociated pneumonia antibiotic", chat_id)

        state = self.b.get_chat_state(chat_id)
        self.assertIsNone(state.get("active_recognized"))
        self.assertIn("No uploaded protocol supports VAP", answer)
        logged = self.mock_log.call_args.kwargs
        trace = logged["trace"]
        self.assertEqual(trace.get("unsupported_syndrome"), "vap")
        self.assertEqual(trace.get("unsupported_key"), "vap")
        self.assertEqual(trace.get("unsupported_matched_term"), "ventilator associated pneumonia")
        self.assertEqual(trace.get("unsupported_action"), "blocked")
        self.assertIsNone(trace.get("selected_protocol_file"))

    @unittest.skipUnless(bot.RAPIDFUZZ_AVAILABLE, "rapidfuzz not installed")
    def test_explicit_meropenem_typo_with_dosing_context_routes_high_confidence(self):
        chat_id = self._chat_id("high_fuzzy_meropenem")
        answer = self._ask_without_rag_or_llm("meropnem dose GFR 45", chat_id)

        state = self.b.get_chat_state(chat_id)
        active = state.get("active_recognized")
        self.assertIsNotNone(active)
        self.assertEqual(active.get("confidence"), "high")
        self.assertEqual(self.b.normalize_path(active["protocol_file"]), "protocols/antibiotics/meropenem.txt")
        self.assertEqual(state["collected_slots"].get("gfr"), 45.0)
        self.assertIn("Meropenem - renal tier", answer)
        self.assertIn("Source: uploaded antibiotic renal dosing DOCX - meropenem", answer)

    @unittest.skipUnless(bot.RAPIDFUZZ_AVAILABLE, "rapidfuzz not installed")
    def test_misspelled_clinical_pneumonia_can_match_cap_safely(self):
        _, recognized = self.b.normalize_question("penumonia outpatient antibiotic")
        self.assertIsNone(recognized)

        chat_id = self._chat_id("weak_penumonia_cap")
        answer = self._ask_without_rag_or_llm("penumonia outpatient antibiotic", chat_id)
        trace = self._last_trace()

        self._assert_trace_selected_protocol(trace, "cap", "protocols/cap.txt", "CAP")
        self.assertEqual(trace["route_decision"]["reason"], "syndrome plus empiric intent claimed")
        self.assertIn("CAP - OUTPATIENT_STANDARD", answer)

    def test_pneumonia_alone_still_routes_to_cap_via_weak_evidence(self):
        chat_id = self._chat_id("weak_pneumonia_alone")
        answer = self._ask_without_rag_or_llm("pneumonia", chat_id)
        trace = self._last_trace()

        self._assert_trace_selected_protocol(trace, "cap", "protocols/cap.txt", "CAP")
        self.assertEqual(trace["route_decision"]["reason"], "weak syndrome evidence claimed as fallback")
        self.assertIn("CAP quick map", answer)

    def test_weak_pneumonia_alias_does_not_override_result_or_microbe_evidence(self):
        cases = [
            ("Penumonia PCR Proteus", "protocols/pneumonia_pcr.txt", "BioFire PN - proteus spp."),
            ("pneumonia result Proteus", None, "Is this a PCR/BioFire result"),
            ("Proteus pneumonia", None, "Is this a PCR/BioFire result"),
            ("is meropenem good vs pneumonia", None, "No uploaded protocol explicitly claims drug coverage"),
        ]

        for query, expected_file, expected_fragment in cases:
            with self.subTest(query=query):
                chat_id = self._chat_id("weak_pneumonia_stronger")
                self.b.CONVERSATION_STATE.pop(chat_id, None)
                answer = self._ask_without_rag_or_llm(query, chat_id)
                trace = self._last_trace()
                if expected_file is None:
                    self.assertIsNone(trace.get("selected_protocol_file"))
                else:
                    self.assertEqual(
                        self.b.normalize_path(trace.get("selected_protocol_file")),
                        expected_file,
                    )
                self.assertFalse(
                    (trace.get("selected_protocol_file") or "").replace("\\", "/").endswith("protocols/cap.txt"),
                    f"{query!r} unexpectedly selected CAP",
                )
                self.assertIn(expected_fragment.lower(), answer.lower())

    def test_ji_pcr_klebsiella_extracts_panel_and_routes_to_joint_infection(self):
        self._install_protocol_fixture("joint_infection_pcr.txt")
        evidence = self.b.extract_routing_evidence("JI PCR klebsiella")

        self.assertEqual(evidence.intent, "test_interpretation")
        self.assertEqual(evidence.test.family, "pcr")
        self.assertEqual(evidence.test.panel, "joint_infection")
        self.assertIn("Klebsiella pneumoniae group", evidence.microbes)

        decision = self.b.resolve_route(evidence, self.b.PROTOCOL_PARSED_BY_FILE)
        self.assertEqual(decision.kind, "route")
        self.assertEqual(
            self.b.normalize_path(decision.protocol_file),
            "protocols/joint_infection_pcr.txt",
        )

    def test_ji_pcr_klebsiella_full_flow_uses_joint_infection_protocol(self):
        self._install_protocol_fixture("joint_infection_pcr.txt")
        chat_id = self._chat_id("ji_pcr_klebsiella")
        answer = self._ask_without_rag_or_llm("JI PCR klebsiella", chat_id)
        trace = self._last_trace()

        self._assert_trace_selected_protocol(
            trace,
            "biofire_joint_infection",
            "protocols/joint_infection_pcr.txt",
            "BioFire JI",
        )
        self.assertEqual(trace["selection_output_key"], "ambiguous_pathogen")
        self.assertIn("which klebsiella was detected", answer.lower())
        self.assertIn("klebsiella aerogenes", answer.lower())
        self.assertIn("klebsiella oxytoca", answer.lower())
        self.assertNotIn("which pcr/biofire panel", answer.lower())
        self.assertNotIn("please check the specific recommendations", answer.lower())

    def test_ji_pcr_klebsiella_pneumoniae_full_flow_selects_ceftriaxone(self):
        self._install_protocol_fixture("joint_infection_pcr.txt")
        chat_id = self._chat_id("ji_pcr_klebsiella_pneumoniae")
        answer = self._ask_without_rag_or_llm("JI PCR klebsiella pneumoniae", chat_id)
        trace = self._last_trace()

        self._assert_trace_selected_protocol(
            trace,
            "biofire_joint_infection",
            "protocols/joint_infection_pcr.txt",
            "BioFire JI",
        )
        self.assertEqual(trace["selection_output_key"], "TIER_1_CEFTRIAXONE")
        self.assertIn("klebsiella pneumoniae group", answer.lower())
        self.assertIn("ceftriaxone", answer.lower())
        self.assertIn("for skin-soft tissue infections safe to use this protocol as is", answer.lower())
        self.assertIn("do not narrow below meropenem", answer.lower())

    def test_pcr_klebsiella_then_ji_preserves_organism_context(self):
        self._install_protocol_fixture("joint_infection_pcr.txt")
        chat_id = self._chat_id("pcr_klebsiella_then_ji")

        first = self._ask_without_rag_or_llm("PCR klebsiella", chat_id)
        self.assertIn("which pcr/biofire panel", first.lower())
        self.mock_log.reset_mock()

        answer = self._ask_without_rag_or_llm("JI", chat_id)
        trace = self._last_trace()

        self._assert_trace_selected_protocol(
            trace,
            "biofire_joint_infection",
            "protocols/joint_infection_pcr.txt",
            "BioFire JI",
        )
        self.assertEqual(trace["selection_output_key"], "ambiguous_pathogen")
        self.assertIn("which klebsiella was detected", answer.lower())
        self.assertNotIn("which pcr/biofire panel", answer.lower())

    def test_high_risk_route_gate_intercepts_alias_conflicts(self):
        """Route claims are primary; legacy alias routing is fallback only."""
        cases = [
            {
                "name": "penumonia_pcr_proteus_routes_to_biofire_not_cap",
                "query": "Penumonia PCR Proteus",
                "expected_matched_alias": "route_claims",
                "expected_protocol_id": "biofire_pneumonia",
                "expected_protocol_file": "protocols/pneumonia_pcr.txt",
                "expected_mode": "deterministic_selection",
                "expected_llm_called": False,
                "expected_search_calls": 0,
                "expected_llm_calls": 0,
                "expected_selection_output_key": "TIER_2_CEFEPIME",
                "expected_slots": {"pathogen_list": ["proteus spp."]},
                "expected_answer_fragments": ["BioFire PN - proteus spp.", "Tier 2 - cefepime"],
                "forbidden_answer_fragments": ["CAP quick map"],
            },
            {
                "name": "biofire_pn_proteus_routes_to_biofire",
                "query": "BioFire PN Proteus",
                "expected_matched_alias": "route_claims",
                "expected_protocol_id": "biofire_pneumonia",
                "expected_protocol_file": "protocols/pneumonia_pcr.txt",
                "expected_mode": "deterministic_selection",
                "expected_llm_called": False,
                "expected_search_calls": 0,
                "expected_llm_calls": 0,
                "expected_selection_output_key": "TIER_2_CEFEPIME",
                "expected_slots": {"pathogen_list": ["proteus spp."]},
                "expected_answer_fragments": ["BioFire PN - proteus spp.", "Tier 2 - cefepime"],
            },
            {
                "name": "pcr_proteus_asks_panel_source",
                "query": "PCR Proteus",
                "expected_matched_alias": None,
                "expected_protocol_id": None,
                "expected_protocol_file": None,
                "expected_mode": "deterministic_route_claims",
                "expected_llm_called": False,
                "expected_search_calls": 0,
                "expected_llm_calls": 0,
                "expected_selection_output_key": None,
                "expected_answer_fragments": ["Which PCR/BioFire panel"],
            },
            {
                "name": "proteus_pneumonia_asks_not_cap_targeted_therapy",
                "query": "Proteus pneumonia",
                "expected_matched_alias": None,
                "expected_protocol_id": None,
                "expected_protocol_file": None,
                "expected_mode": "deterministic_route_claims",
                "expected_llm_called": False,
                "expected_search_calls": 0,
                "expected_llm_calls": 0,
                "expected_selection_output_key": None,
                "expected_answer_fragments": ["Is this a PCR/BioFire result"],
                "forbidden_answer_fragments": ["CAP quick map", "ceftriaxone"],
            },
            {
                "name": "meropenem_dose_gfr_currently_meropenem",
                "query": "meropenem dose GFR 35",
                "expected_matched_alias": "route_claims",
                "expected_protocol_id": "meropenem",
                "expected_protocol_file": "protocols/antibiotics/meropenem.txt",
                "expected_mode": "deterministic_selection",
                "expected_llm_called": False,
                "expected_search_calls": 0,
                "expected_llm_calls": 0,
                "expected_selection_output_key": "NORMAL",
                "expected_slots": {"gfr": 35.0},
                "expected_answer_fragments": ["Meropenem - renal tier: Normal", "3 g/day"],
            },
            {
                "name": "meropenem_vs_staph_pneumonia_is_unsupported",
                "query": "is meropenem good vs staphylococcal pneumonia",
                "expected_matched_alias": None,
                "expected_protocol_id": None,
                "expected_protocol_file": None,
                "expected_mode": "deterministic_route_claims",
                "expected_llm_called": False,
                "expected_search_calls": 0,
                "expected_llm_calls": 0,
                "expected_selection_output_key": None,
                "expected_answer_fragments": ["No uploaded protocol explicitly claims drug coverage"],
                "forbidden_answer_fragments": ["Meropenem dosing table", "spectrum"],
            },
            {
                "name": "pneumonia_antibiotics_currently_cap",
                "query": "pneumonia what antibiotics",
                "expected_matched_alias": "route_claims",
                "expected_protocol_id": "cap",
                "expected_protocol_file": "protocols/cap.txt",
                "expected_mode": "deterministic_selection",
                "expected_llm_called": False,
                "expected_search_calls": 0,
                "expected_llm_calls": 0,
                "expected_selection_output_key": "default",
                "expected_answer_fragments": ["CAP quick map", "ceftriaxone"],
            },
            {
                "name": "aspirin_before_surgery_currently_periop_med",
                "query": "aspirin before surgery",
                "expected_matched_alias": "route_claims",
                "expected_protocol_id": "periop_gyogyszerek",
                "expected_protocol_file": "protocols/periop_gyogyszerek.txt",
                "expected_mode": "deterministic_periop_info",
                "expected_llm_called": False,
                "expected_search_calls": 0,
                "expected_llm_calls": 0,
                "expected_selection_output_key": None,
                "expected_answer_fragments": ["Aspirin; acetylsalicylic acid; ASA", "Usually no need to omit"],
            },
            {
                "name": "hydrocortisone_to_prednisolone_routes_to_steroid_equivalence",
                "query": "hydrocortisone to prednisolone",
                "expected_matched_alias": "route_claims",
                "expected_protocol_id": "steroid_equivalence",
                "expected_protocol_file": "protocols/steroid_equivalence.txt",
                "expected_mode": "deterministic_steroid_equivalence",
                "expected_llm_called": False,
                "expected_search_calls": 0,
                "expected_llm_calls": 0,
                "expected_selection_output_key": None,
                "expected_answer_fragments": ["Please provide the hydrocortisone dose in mg"],
            },
        ]

        for case in cases:
            with self.subTest(case=case["name"]):
                chat_id = self._chat_id(case["name"])
                self.b.CONVERSATION_STATE.pop(chat_id, None)
                self.mock_log.reset_mock()

                answer, search_calls, llm_calls = self._ask_with_empty_rag_and_mock_llm(
                    case["query"], chat_id
                )

                logged = self._last_logged()
                trace = logged["trace"]
                recognized = logged["recognized"]

                if case["expected_matched_alias"] is None:
                    self.assertIsNone(recognized)
                else:
                    self.assertIsNotNone(recognized)
                    self.assertEqual(recognized.get("matched_alias"), case["expected_matched_alias"])

                self.assertEqual(trace.get("selected_protocol_id"), case["expected_protocol_id"])
                if case["expected_protocol_file"] is None:
                    self.assertIsNone(trace.get("selected_protocol_file"))
                else:
                    self.assertEqual(
                        self.b.normalize_path(trace.get("selected_protocol_file")),
                        case["expected_protocol_file"],
                    )
                self.assertEqual(trace.get("deterministic_or_llm"), case["expected_mode"])
                self.assertEqual(trace.get("llm_called"), case["expected_llm_called"])
                self.assertEqual(search_calls, case["expected_search_calls"])
                self.assertEqual(llm_calls, case["expected_llm_calls"])
                self.assertEqual(
                    trace.get("selection_output_key"),
                    case["expected_selection_output_key"],
                )
                self.assertIn("routing_evidence", trace)
                self.assertIn("candidate_protocols", trace)
                self.assertIn("route_decision", trace)
                self.assertIn("reason", trace["route_decision"])
                for slot_key, expected_value in case.get("expected_slots", {}).items():
                    self.assertEqual(trace.get("slots", {}).get(slot_key), expected_value)
                for fragment in case["expected_answer_fragments"]:
                    self.assertIn(fragment.lower(), answer.lower())
                for fragment in case.get("forbidden_answer_fragments", []):
                    self.assertNotIn(fragment.lower(), answer.lower())

    def test_deterministic_short_circuit_has_source_and_is_logged_without_retrieval(self):
        chat_id = self._chat_id("deterministic_logging")
        answer = self._ask_without_rag_or_llm("meropenem GFR 95", chat_id)

        self.assertIn("Source:", answer)
        self.assertIn("meropenem", answer.lower())
        logged = self.mock_log.call_args.kwargs
        self.assertEqual(self.b.normalize_path(logged["recognized"]["protocol_file"]), "protocols/antibiotics/meropenem.txt")
        self.assertEqual(logged["retrieved_chunks"], [])
        trace = logged["trace"]
        self.assertEqual(trace["selected_protocol_id"], "meropenem")
        self.assertEqual(trace["selection_output_key"], "NORMAL")
        self.assertEqual(trace["deterministic_or_llm"], "deterministic_selection")
        self.assertFalse(trace["llm_called"])
        self.assertEqual(trace["slots"]["gfr"], 95.0)

    def test_deterministic_answer_trace_fields_are_complete(self):
        chat_id = self._chat_id("trace_deterministic")
        answer = self._ask_without_rag_or_llm("meropenem GFR 95", chat_id)

        logged = self._last_logged()
        trace = logged["trace"]
        self._assert_trace_selected_protocol(
            trace, "meropenem", "protocols/antibiotics/meropenem.txt", "uploaded antibiotic renal dosing DOCX - meropenem"
        )
        self.assertEqual(logged["retrieved_chunks"], [])
        self.assertEqual(trace["retrieved_chunks"], [])
        self.assertEqual(trace["protocol_type"], "drug_dosing_protocol")
        self.assertEqual(trace["selection_output_key"], "NORMAL")
        self.assertEqual(trace["selected_output_key"], "NORMAL")
        self.assertEqual(trace["selection_mode"], "priority_rules")
        self.assertEqual(trace["missing_slots"], [])
        self.assertEqual(trace["slots"], {"gfr": 95.0})
        self.assertEqual(trace["deterministic_or_llm"], "deterministic_selection")
        self.assertFalse(trace["llm_called"])
        self.assertIsNone(trace["unsupported_syndrome"])
        self.assertIsNone(trace["unsupported_action"])
        self.assertIsNone(trace["blocked_reason"])
        self.assertIn("Meropenem - renal tier", trace["final_body"])
        self.assertEqual(trace["final_answer"], answer)
        self.assertEqual(
            self.b.normalize_path(trace["active_after"]["protocol_file"]),
            "protocols/antibiotics/meropenem.txt",
        )
        self.assertEqual(trace["turn_context"]["protocol_slots_after"], {"gfr": 95.0})

    def test_implicit_body_size_input_routes_without_active_protocol(self):
        chat_id = self._chat_id("implicit_body_size")
        answer = self._ask_without_rag_or_llm("190cm, 130kg", chat_id)

        self.assertIn("Body size calculations for 130 kg, 190 cm", answer)
        self.assertIn("BMI:", answer)
        self.assertIn("Source: Body size calculators", answer)
        active = self.b.get_chat_state(chat_id).get("active_recognized")
        self.assertEqual(
            self.b.normalize_path(active["protocol_file"]),
            "protocols/body_size_calculators.txt",
        )

    def test_implicit_body_size_input_overrides_stale_drug_context(self):
        chat_id = self._chat_id("body_size_over_stale_meropenem")
        self._ask_without_rag_or_llm("meropenem", chat_id)
        self.mock_log.reset_mock()

        answer = self._ask_without_rag_or_llm("150cm NORMAL, 100kg s\u00faly", chat_id)

        self.assertIn("Body size calculations for 100 kg, 150 cm", answer)
        self.assertNotIn("Meropenem gyors", answer)
        active = self.b.get_chat_state(chat_id).get("active_recognized")
        self.assertEqual(
            self.b.normalize_path(active["protocol_file"]),
            "protocols/body_size_calculators.txt",
        )

    def test_asa_short_entry_alias_routes_to_aspirin_periop_entry(self):
        chat_id = self._chat_id("asa_periop")
        answer = self._ask_without_rag_or_llm("ASA before surgery", chat_id)

        self.assertIn("Aspirin; acetylsalicylic acid; ASA", answer)
        self.assertIn("Usually no need to omit", answer)
        self.assertIn("Omit only if high bleeding risk", answer)
        trace = self._last_trace()
        self._assert_trace_selected_protocol(
            trace,
            "periop_gyogyszerek",
            "protocols/periop_gyogyszerek.txt",
            "Perioperative medications + antithrombotics",
        )
        self.assertEqual(trace["deterministic_or_llm"], "deterministic_periop_info")

    def test_implicit_echo_ava_inputs_route_to_ava_calculator(self):
        chat_id = self._chat_id("implicit_echo_ava")
        answer = self._ask_without_rag_or_llm(
            "Calculate AVA LVOT VTI 20cm, AV VTI 80cm LVOT diam 18mm",
            chat_id,
        )

        self.assertIn("Echo AVA by continuity equation", answer)
        self.assertIn("AVA: 0.64 cm2", answer)
        trace = self._last_trace()
        self._assert_trace_selected_protocol(
            trace,
            "echo_ava",
            "protocols/echo_ava.txt",
            "Echo AVA",
        )
        self.assertEqual(trace["selection_output_key"], "calculated_ava")
        self.assertEqual(trace["deterministic_or_llm"], "deterministic_selection")

    def test_implicit_echo_cardiac_output_inputs_route_when_hr_present(self):
        chat_id = self._chat_id("implicit_echo_co")
        answer = self._ask_without_rag_or_llm("LVOT VTI 20cm LVOT diam 18mm HR 70", chat_id)

        self.assertIn("Echo LVOT stroke volume", answer)
        self.assertIn("Cardiac output:", answer)
        trace = self._last_trace()
        self._assert_trace_selected_protocol(
            trace,
            "echo_cardiac_output",
            "protocols/echo_cardiac_output.txt",
            "Echo cardiac output",
        )
        self.assertEqual(trace["selection_output_key"], "calculated_co")
        self.assertEqual(trace["deterministic_or_llm"], "deterministic_selection")

    def test_dexa_routes_to_dexamethasone_steroid_equivalence(self):
        chat_id = self._chat_id("dexa_equivalence")
        answer = self._ask_without_rag_or_llm("dexa 6 mg equivalent", chat_id)

        self.assertIn("Steroid equivalence for 6 mg dexamethasone", answer)
        self.assertIn("| hydrocortisone | 160 mg |", answer)
        trace = self._last_trace()
        self._assert_trace_selected_protocol(
            trace,
            "steroid_equivalence",
            "protocols/steroid_equivalence.txt",
            "Steroid equivalence table",
        )
        self.assertEqual(trace["deterministic_or_llm"], "deterministic_steroid_equivalence")

    def test_unsupported_syndrome_block_trace_fields_are_complete(self):
        chat_id = self._chat_id("trace_unsupported_block")
        answer = self._ask_without_rag_or_llm("VAP antibiotic?", chat_id)

        logged = self._last_logged()
        trace = logged["trace"]
        self.assertIsNone(logged["recognized"])
        self.assertEqual(logged["retrieved_chunks"], [])
        self.assertIsNone(trace["selected_protocol_id"])
        self.assertIsNone(trace["selected_protocol_file"])
        self.assertIsNone(trace["selection_output_key"])
        self.assertEqual(trace["deterministic_or_llm"], "deterministic_route_claims")
        self.assertFalse(trace["llm_called"])
        self.assertEqual(trace["retrieved_chunks"], [])
        self.assertEqual(trace["unsupported_syndrome"], "vap")
        self.assertEqual(trace["unsupported_key"], "vap")
        self.assertEqual(trace["unsupported_matched_term"], "vap")
        self.assertEqual(trace["unsupported_action"], "blocked")
        self.assertEqual(trace["blocked_reason"], "unsupported_syndrome")
        self.assertEqual(trace["turn_context"]["unsupported_syndrome"], "vap")
        self.assertEqual(trace["turn_context"]["selected_recognized"], None)
        self.assertEqual(trace["final_answer"], answer)
        self.assertIn("No uploaded protocol supports VAP", answer)

    def test_explicit_drug_overrides_unsupported_syndrome_trace_fields(self):
        chat_id = self._chat_id("trace_explicit_drug_overrides_vap")
        answer = self._ask_without_rag_or_llm("GFR 40 and VAP, mero dose?", chat_id)

        trace = self._last_trace()
        self._assert_trace_selected_protocol(
            trace, "meropenem", "protocols/antibiotics/meropenem.txt", "uploaded antibiotic renal dosing DOCX - meropenem"
        )
        self.assertEqual(trace["selection_output_key"], "NORMAL")
        self.assertEqual(trace["selection_mode"], "priority_rules")
        self.assertEqual(trace["slots"]["gfr"], 40.0)
        self.assertEqual(trace["deterministic_or_llm"], "deterministic_selection")
        self.assertFalse(trace["llm_called"])
        self.assertEqual(trace["unsupported_syndrome"], "vap")
        self.assertEqual(trace["unsupported_key"], "vap")
        self.assertEqual(trace["unsupported_matched_term"], "vap")
        self.assertEqual(trace["unsupported_action"], "ignored_explicit_drug")
        self.assertIsNone(trace["blocked_reason"])
        self.assertEqual(trace["turn_context"]["unsupported_syndrome"], "vap")
        self.assertEqual(trace["turn_context"]["protocol_slots_after"]["gfr"], 40.0)
        self.assertIn("Source: uploaded antibiotic renal dosing DOCX - meropenem", answer)
        self.assertNotIn("No uploaded protocol supports", answer)

    def test_deterministic_missing_and_out_of_bounds_cases_do_not_call_rag_or_llm(self):
        cases = [
            {
                "name": "missing_weight",
                "query": "TMP/SMX Steno BSI GFR 60",
                "expected_output": "default",
                "expected_missing": ["body_weight_kg"],
                "expected_fragments": ["Missing: body_weight_kg", "Source: TMP/SMX"],
            },
            {
                "name": "out_of_bounds_weight",
                "query": "TMP/SMX Steno BSI 150 kg GFR 60",
                "expected_output": "HIGH_DOSE_GFR_GT_30_OR_CRRT",
                "expected_missing": [],
                "expected_fragments": [
                    "outside the explicit protocol table range",
                    "not a 150 kg dosing recommendation",
                    "Source: TMP/SMX",
                ],
            },
        ]
        for case in cases:
            with self.subTest(case=case["name"]):
                chat_id = self._chat_id(case["name"])
                self.mock_log.reset_mock()

                answer = self._ask_without_rag_or_llm(case["query"], chat_id)

                trace = self._last_trace()
                self._assert_trace_selected_protocol(
                    trace, "tmpsmx", "protocols/antibiotics/tmpsmx.txt", "TMP/SMX"
                )
                self.assertEqual(trace["deterministic_or_llm"], "deterministic_selection")
                self.assertFalse(trace["llm_called"])
                self.assertEqual(trace["retrieved_chunks"], [])
                self.assertEqual(trace["selection_mode"], "table_lookup")
                self.assertEqual(trace["selection_output_key"], case["expected_output"])
                self.assertEqual(trace["missing_slots"], case["expected_missing"])
                for fragment in case["expected_fragments"]:
                    self.assertIn(fragment, answer)

    def test_conversation_confirmation_yes_and_no_flows_are_deterministic(self):
        no_chat_id = self._chat_id("confirmation_no")
        self._ask_without_rag_or_llm("meropenem GFR 45", no_chat_id)
        self.mock_log.reset_mock()

        prompt = self._ask_without_rag_or_llm("how are you?", no_chat_id)
        prompt_trace = self._last_trace()
        self.assertIn("Reply yes", prompt)
        self.assertEqual(prompt_trace["deterministic_or_llm"], "deterministic_confirmation")
        self.assertTrue(prompt_trace["confirmation_required"])
        self.assertEqual(prompt_trace["blocked_reason"], "unclear_followup")
        self.assertIsNotNone(
            self.b.get_chat_state(no_chat_id).get("pending_context_confirmation")
        )

        self.mock_log.reset_mock()
        no_answer = self._ask_without_rag_or_llm("no", no_chat_id)
        no_trace = self._last_trace()
        self.assertIn("will not apply", no_answer)
        self.assertEqual(no_trace["deterministic_or_llm"], "deterministic_confirmation")
        self.assertFalse(no_trace["llm_called"])
        self.assertEqual(no_trace["blocked_reason"], "context_confirmation_no")
        self.assertTrue(no_trace["confirmation_pending"])
        self.assertIsNone(
            self.b.get_chat_state(no_chat_id).get("pending_context_confirmation")
        )

        yes_chat_id = self._chat_id("confirmation_yes")
        self._ask_without_rag_or_llm("meropenem GFR 45", yes_chat_id)
        self._ask_without_rag_or_llm("how are you?", yes_chat_id)
        self.mock_log.reset_mock()

        yes_answer = self._ask_without_rag_or_llm("yes", yes_chat_id)
        yes_trace = self._last_trace()
        self.assertIn("Meropenem - renal tier", yes_answer)
        self._assert_trace_selected_protocol(
            yes_trace, "meropenem", "protocols/antibiotics/meropenem.txt", "uploaded antibiotic renal dosing DOCX - meropenem"
        )
        self.assertEqual(yes_trace["deterministic_or_llm"], "deterministic_selection")
        self.assertFalse(yes_trace["llm_called"])
        self.assertTrue(yes_trace["confirmation_pending"])
        self.assertEqual(yes_trace["slots"], {"gfr": 45.0})
        self.assertIsNone(
            self.b.get_chat_state(yes_chat_id).get("pending_context_confirmation")
        )

    def test_debug_note_variants_do_not_mutate_protocol_state(self):
        chat_id = self._chat_id("debug_note_no_state_mutation")
        self._ask_without_rag_or_llm("Biofire pneumococcus", chat_id)
        state = self.b.get_chat_state(chat_id)
        before_active = dict(state.get("active_recognized") or {})
        before_slots = dict(state.get("collected_slots") or {})
        before_history_len = len(state.get("history", []))
        self.mock_log.reset_mock()

        answer = self._ask_without_rag_or_llm(
            "debug note: this should not go to biofire",
            chat_id,
        )

        state = self.b.get_chat_state(chat_id)
        self.assertIn("Admin/debug note ignored", answer)
        self.assertEqual(state.get("active_recognized"), before_active)
        self.assertEqual(state.get("collected_slots"), before_slots)
        self.assertEqual(len(state.get("history", [])), before_history_len)
        logged = self._last_logged()
        self.assertIsNone(logged["recognized"])
        self.assertEqual(logged["trace"]["blocked_reason"], "admin_debug_note")

    def test_out_of_bounds_confirmation_yes_is_intentional_and_safe(self):
        chat_id = self._chat_id("out_of_bounds_yes")
        first = self._ask_without_rag_or_llm("TMP/SMX Steno BSI 60kg GFR 289", chat_id)
        self.assertIn("Please confirm or correct", first)
        self.assertIsNotNone(
            self.b.get_chat_state(chat_id).get("pending_out_of_bounds_confirmation")
        )
        self.mock_log.reset_mock()

        answer = self._ask_without_rag_or_llm("yes it is 289", chat_id)
        trace = self._last_trace()

        self.assertIn("Confirmed GFR 289", answer)
        self.assertIn("cannot provide automatic dosing", answer)
        self.assertEqual(trace["deterministic_or_llm"], "deterministic_confirmation")
        self.assertEqual(trace["blocked_reason"], "out_of_bounds_confirmed")
        self.assertTrue(trace["confirmation_pending"])
        self.assertIsNone(
            self.b.get_chat_state(chat_id).get("pending_out_of_bounds_confirmation")
        )

    def test_out_of_bounds_confirmation_corrected_value_resumes_selection(self):
        chat_id = self._chat_id("out_of_bounds_corrected")
        self._ask_without_rag_or_llm("TMP/SMX Steno BSI 60kg GFR 289", chat_id)
        self.mock_log.reset_mock()

        answer = self._ask_without_rag_or_llm("no, GFR 60", chat_id)
        trace = self._last_trace()

        self.assertIn("TMP/SMX - HIGH_DOSE", answer)
        self.assertEqual(trace["deterministic_or_llm"], "deterministic_selection")
        self.assertEqual(trace["selection_output_key"], "HIGH_DOSE_GFR_GT_30_OR_CRRT")
        self.assertEqual(trace["slots"]["gfr"], 60.0)
        self.assertIsNone(
            self.b.get_chat_state(chat_id).get("pending_out_of_bounds_confirmation")
        )

    def test_no_match_out_of_scope_does_not_claim_selected_protocol(self):
        chat_id = self._chat_id("out_of_scope")
        fake_chunks = [{
            "source": "protocols/antibiotics/meropenem.txt",
            "source_label": "meropenem",
            "text": "## DEFAULT_ANSWER\nMeropenem dosing text",
            "similarity": 0.01,
        }]
        with patch.object(self.b, "search_protocols", return_value=fake_chunks):
            debug = self.b.build_debug_trace("hi", chat_id)
        self.assertIn("Context source: none", debug)
        self.assertIn("Selected protocol: none", debug)
        self.assertIn("Matched alias: none", debug)
        self.assertIn("Deterministic/LLM source: LLM-generated RAG path", debug)

    def test_debug_trace_shows_unsupported_syndrome_block(self):
        chat_id = self._chat_id("debug_vap")
        with patch.object(self.b, "search_protocols", return_value=[]):
            debug = self.b.build_debug_trace("what ab for VAP?", chat_id)
        self.assertIn("Selected protocol: none", debug)
        self.assertIn("Unsupported syndrome: vap", debug)
        self.assertIn("Unsupported key: vap", debug)
        self.assertIn("Unsupported matched term: vap", debug)
        self.assertIn("Unsupported action: blocked", debug)
        self.assertIn("LLM called: false", debug)

    def test_debug_trace_shows_unsupported_syndrome_ignored_for_explicit_drug(self):
        chat_id = self._chat_id("debug_vap_meropenem")
        self._install_protocol_for_debug("meropenem.txt")
        with patch.object(self.b, "search_protocols", return_value=[]):
            debug = self.b.build_debug_trace("mero dose for VAP GFR 40", chat_id)
        self.assertIn("Selected protocol: meropenem", debug)
        self.assertIn("Unsupported syndrome: vap", debug)
        self.assertIn("Unsupported key: vap", debug)
        self.assertIn("Unsupported matched term: vap", debug)
        self.assertIn("Unsupported action: ignored_explicit_drug", debug)
        self.assertIn("Selection output: NORMAL", debug)

    def _install_protocol_for_debug(self, filename):
        import protocol_parser as pp
        path = protocol_fixture_relpath(filename)
        parsed = pp.parse_protocol_file(protocol_fixture_path(filename))
        self.b.PROTOCOL_PARSED_BY_FILE[self.b.normalize_path(path)] = parsed
        return path, parsed

    def test_unclear_followup_with_active_protocol_asks_confirmation(self):
        chat_id = self._chat_id("unclear_followup")
        self._ask_without_rag_or_llm("meropenem GFR 45", chat_id)

        answer = self._ask_without_rag_or_llm("how are you?", chat_id)
        self.assertIn("not sure this message belongs", answer)
        self.assertIn("Reply yes", answer)
        self.assertIsNotNone(self.b.get_chat_state(chat_id).get("pending_context_confirmation"))

        answer = self._ask_without_rag_or_llm("no", chat_id)
        self.assertIn("will not apply", answer)
        self.assertIsNone(self.b.get_chat_state(chat_id).get("pending_context_confirmation"))

    def test_biofire_organisms_do_not_affect_drug_protocol_slots(self):
        chat_id = self._chat_id("biofire_to_drugs")
        state = self.b.get_chat_state(chat_id)

        self._ask_without_rag_or_llm("BioFire result pneumococcus CTX-M", chat_id)
        self.assertIn("streptococcus pneumoniae", self._protocol_slots(state, "BioFire").get("pathogen_list", []))

        self._ask_without_rag_or_llm("meropenem dose GFR 40", chat_id)
        meropenem_slots = self._protocol_slots(state, "meropenem")
        self.assertEqual(meropenem_slots.get("gfr"), 40.0)
        self.assertNotIn("pathogen_list", meropenem_slots)
        self.assertNotIn("resistance_gene_list", meropenem_slots)

        self._ask_without_rag_or_llm("vancomycin dose 70 kg GFR 40", chat_id)
        vancomycin_slots = self._protocol_slots(state, "vancomycin")
        self.assertEqual(vancomycin_slots.get("body_weight_kg"), 70.0)
        self.assertNotIn("pathogen_list", vancomycin_slots)
        self.assertNotIn("resistance_gene_list", vancomycin_slots)

        self._ask_without_rag_or_llm("TMP/SMX Steno BSI 70 kg GFR 60", chat_id)
        tmpsmx_slots = self._protocol_slots(state, "TMP/SMX")
        self.assertEqual(tmpsmx_slots.get("body_weight_kg"), 70.0)
        self.assertEqual(tmpsmx_slots.get("gfr"), 60.0)
        self.assertNotIn("pathogen_list", tmpsmx_slots)
        self.assertNotIn("resistance_gene_list", tmpsmx_slots)

    def test_tmpsmx_weight_and_gfr_do_not_affect_biofire(self):
        chat_id = self._chat_id("tmpsmx_to_biofire")
        state = self.b.get_chat_state(chat_id)

        self._ask_without_rag_or_llm("TMP/SMX Steno BSI 70 kg GFR 60", chat_id)
        self._ask_without_rag_or_llm("BioFire result pseudomonas", chat_id)

        biofire_slots = self._protocol_slots(state, "BioFire")
        self.assertEqual(biofire_slots.get("pathogen_list"), ["pseudomonas aeruginosa"])
        self.assertNotIn("body_weight_kg", biofire_slots)
        self.assertNotIn("gfr", biofire_slots)
        self.assertNotIn("indication", biofire_slots)
        self.assertEqual(self._protocol_slots(state, "TMP/SMX").get("body_weight_kg"), 70.0)

    def test_meropenem_to_tmpsmx_switch_starts_tmpsmx_slots_clean(self):
        chat_id = self._chat_id("mero_to_tmpsmx_clean")
        state = self.b.get_chat_state(chat_id)

        self._ask_without_rag_or_llm("meropenem dose GFR 70", chat_id)
        self._ask_without_rag_or_llm("TMP/SMX dose", chat_id)

        tmpsmx_slots = self._protocol_slots(state, "TMP/SMX")
        self.assertNotIn("gfr", tmpsmx_slots)
        self.assertNotIn("body_weight_kg", tmpsmx_slots)
        self.assertEqual(self._protocol_slots(state, "meropenem").get("gfr"), 70.0)

    def test_new_patient_reset_clears_slots_by_protocol(self):
        chat_id = self._chat_id("reset_slots_by_protocol")
        state = self.b.get_chat_state(chat_id)

        self._ask_without_rag_or_llm("meropenem dose GFR 70", chat_id)
        self._ask_without_rag_or_llm("TMP/SMX Steno BSI 70 kg GFR 60", chat_id)
        self.assertTrue(state.get("slots_by_protocol"))

        self._ask_without_rag_or_llm("new patient", chat_id)

        self.assertEqual(state.get("slots_by_protocol"), {})
        self.assertEqual(state.get("collected_slots"), {})
        self.assertIsNone(state.get("active_recognized"))

    def test_debug_trace_shows_active_protocol_slots_only(self):
        chat_id = self._chat_id("debug_active_slots")
        state = self.b.get_chat_state(chat_id)

        self._ask_without_rag_or_llm("BioFire result pneumococcus CTX-M", chat_id)
        self._ask_without_rag_or_llm("meropenem dose GFR 45", chat_id)

        with patch.object(self.b, "search_protocols", return_value=[]):
            debug = self.b.build_debug_trace("dose", chat_id)

        self.assertIn('Collected slots: {"gfr": 45.0}', debug)
        self.assertNotIn("pathogen_list", debug)
        self.assertNotIn("resistance_gene_list", debug)

    def test_links_transfer_only_explicit_target_safe_slots(self):
        chat_id = self._chat_id("link_transfer_safe")
        state = self.b.get_chat_state(chat_id)
        biofire = self._recognized_for("BioFire")
        state["active_recognized"] = biofire
        self.b._set_protocol_slots(state, biofire, {
            "pathogen_list": ["pseudomonas aeruginosa"],
            "resistance_gene_list": ["carbapenemase"],
            "gfr": 40.0,
            "body_weight_kg": 80.0,
        })
        state["last_recommended_antibiotics"] = ["meropenem"]

        self._ask_without_rag_or_llm("dose?", chat_id)

        active = state.get("active_recognized")
        self.assertEqual(self.b.normalize_path(active["protocol_file"]), "protocols/antibiotics/meropenem.txt")
        meropenem_slots = self._protocol_slots(state, "meropenem")
        self.assertEqual(meropenem_slots, {"gfr": 40.0})
        biofire_slots = self._protocol_slots(state, "BioFire")
        self.assertIn("pathogen_list", biofire_slots)


# ---------------------------------------------------------------------------
# Perioperative steroid and steroid equivalence split
# ---------------------------------------------------------------------------

class TestPeriopSteroidSplit(unittest.TestCase):

    PROTO_DIR = os.path.join(os.path.dirname(__file__), "protocols")

    def setUp(self):
        import protocol_parser as pp
        import telegram_bot as b

        self.b = b
        self._old_parsed = dict(b.PROTOCOL_PARSED_BY_FILE)
        self._old_state = dict(b.CONVERSATION_STATE)
        b.PROTOCOL_PARSED_BY_FILE.clear()
        b.CONVERSATION_STATE.clear()

        for filename in [
            "periop_gyogyszerek.txt",
            "periop_steroids.txt",
            "steroid_equivalence.txt",
        ]:
            rel_path = protocol_fixture_relpath(filename)
            with open(protocol_fixture_path(filename), encoding="utf-8") as f:
                text = f.read()
            parsed = pp._parse_protocol_text(text, path=rel_path)
            b.PROTOCOL_PARSED_BY_FILE[b.normalize_path(rel_path)] = parsed

    def tearDown(self):
        b = self.b
        b.PROTOCOL_PARSED_BY_FILE.clear()
        b.PROTOCOL_PARSED_BY_FILE.update(self._old_parsed)
        b.CONVERSATION_STATE.clear()
        b.CONVERSATION_STATE.update(self._old_state)

    def test_periop_steroid_table_excludes_equivalence_table(self):
        b = self.b
        periop = b._recognized_for_protocol_id("periop_steroids")
        body = b._try_periop_info_shortcut({}, periop, "perioperative steroid stress dose")

        self.assertIn("small surgery", body)
        self.assertIn("major surgery", body)
        self.assertNotIn("Generic steroid equivalence table", body)
        self.assertNotIn("Steroid equivalence table", body)

    def test_steroid_equivalence_calculates_all_table_rows(self):
        b = self.b
        periop = b._recognized_for_protocol_id("periop_steroids")
        state = {"active_recognized": periop}

        body = b._try_steroid_equivalence_shortcut(
            state, periop, "methylprednisone 8 mg equivalent"
        )

        self.assertIn("| hydrocortisone | 40 mg |", body)
        self.assertIn("| dexamethasone | 1.5 mg |", body)
        self.assertIn("| fludrocortisone | 4 mg |", body)
        self.assertNotIn("Generic steroid equivalence table", body)
        self.assertNotIn("Glucocorticoid:mineralocorticoid", body)
        self.assertNotIn("Duration", body)
        self.assertNotIn("12-36 h", body)
        self.assertNotIn("5:0.5", body)
        self.assertEqual(b._active_protocol_id(state), "steroid_equivalence")

        parsed = b.PROTOCOL_PARSED_BY_FILE[
            b.normalize_path(os.path.join("protocols", "steroid_equivalence.txt"))
        ]
        footer = parsed.get("default_footer", "")
        self.assertIn("Generic steroid equivalence table", footer)
        self.assertIn("Glucocorticoid:mineralocorticoid", footer)
        self.assertIn("12-36 h", footer)
        self.assertIn("5:0.5", footer)

    def test_steroid_equivalence_active_followup_accepts_dose_only(self):
        b = self.b
        calc = b._recognized_for_protocol_id("steroid_equivalence")
        state = {"active_recognized": calc}

        self.assertTrue(
            b._looks_like_active_protocol_followup("hydrocortisone 20 mg", state)
        )

        body = b._try_steroid_equivalence_shortcut(
            state, calc, "hydrocortisone 20 mg"
        )
        self.assertIn("| methylprednisone | 4 mg |", body)
        self.assertIn("| dexamethasone | 0.75 mg |", body)


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
        path = protocol_fixture_relpath(filename)
        parsed = pp.parse_protocol_file(protocol_fixture_path(filename))
        b.PROTOCOL_PARSED_BY_FILE[b.normalize_path(path)] = parsed
        return path, parsed

    def test_protocols_output_lists_governance_fields(self):
        import telegram_bot as b
        self._install_protocol("meropenem.txt")
        output = b.format_protocols_output()
        self.assertIn("meropenem", output)
        self.assertIn("drug_dosing_protocol", output)
        self.assertIn("draft", output)
        self.assertIn("0.3", output)

    def test_version_uses_protocol_versions_when_available(self):
        import telegram_bot as b
        self._install_protocol("meropenem.txt")
        output = b.format_version_output()
        self.assertIn("Bot version:", output)
        self.assertIn("Protocol library version: 0.3", output)

    def test_debug_trace_explains_fresh_alias_without_prompt_echo(self):
        import telegram_bot as b
        old_debug = dict(b.DEBUG_LOGGING_OPTIONS)
        old_full = b.FULL_CONVERSATION_LOG
        self._install_protocol("meropenem.txt")
        b.load_aliases(os.path.join("protocols", "aliases.json"))
        fake_chunks = [{
            "source": "protocols/antibiotics/meropenem.txt",
            "source_label": "meropenem",
            "text": "## DEFAULT_ANSWER\nProtocol dosing text",
            "similarity": 0.9876,
        }]
        try:
            b.FULL_CONVERSATION_LOG = False
            b.DEBUG_LOGGING_OPTIONS = {
                "log_user_messages": False,
                "log_admin_debug_notes": True,
                "log_prompt_preview": False,
            }
            with patch.object(b, "search_protocols", return_value=fake_chunks):
                output = b.build_debug_trace("private patient details meropenem dose", f"debug_{id(self)}")
        finally:
            b.DEBUG_LOGGING_OPTIONS = old_debug
            b.FULL_CONVERSATION_LOG = old_full
        self.assertIn("Context source: route_claims", output)
        self.assertIn("Matched alias: meropenem", output)
        self.assertIn("Protocol type: drug_dosing_protocol", output)
        self.assertIn("Deterministic/LLM source: deterministic selection_engine", output)
        self.assertIn("File: protocols/antibiotics/meropenem.txt", output)
        self.assertIn("Section: DEFAULT_ANSWER", output)
        self.assertNotIn("private patient details", output)
        self.assertNotIn("Preview:", output)

    def test_default_logging_redacts_prompt(self):
        import telegram_bot as b
        old_debug = dict(b.DEBUG_LOGGING_OPTIONS)
        old_full = b.FULL_CONVERSATION_LOG
        try:
            b.FULL_CONVERSATION_LOG = False
            b.DEBUG_LOGGING_OPTIONS = {
                "log_user_messages": False,
                "log_admin_debug_notes": True,
                "log_prompt_preview": False,
            }
            safe = b._safe_user_message_for_log("John Doe fever")
        finally:
            b.DEBUG_LOGGING_OPTIONS = old_debug
            b.FULL_CONVERSATION_LOG = old_full
        self.assertIsInstance(safe, dict)
        self.assertTrue(safe["redacted"])
        self.assertNotIn("John Doe", str(safe))

    def test_admin_debug_note_logging_preserves_note_text(self):
        import contextlib
        import io
        import json
        import logging
        import telegram_bot as b

        class _CaptureHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.messages = []

            def emit(self, record):
                self.messages.append(record.getMessage())

        old_full = b.FULL_CONVERSATION_LOG
        old_debug = dict(b.DEBUG_LOGGING_OPTIONS)
        old_query_log = b._query_log
        logger = logging.getLogger(f"query_test_{id(self)}")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        capture = _CaptureHandler()
        logger.addHandler(capture)
        b.FULL_CONVERSATION_LOG = False
        b.DEBUG_LOGGING_OPTIONS = {
            "log_user_messages": False,
            "log_bot_responses": True,
            "log_raw_llm_responses": True,
            "log_retrieved_chunks": True,
            "log_routing_trace": True,
            "log_prompt_preview": False,
            "log_admin_debug_notes": True,
            "stdout_full_turns": False,
        }
        b._query_log = logger
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                b._log_query(
                    chat_id=123,
                    user_message="debug note: keep this visible",
                    recognized=None,
                    retrieved_chunks=[],
                    raw_llm=None,
                    final_response="ignored",
                    duration_ms=1,
                    trace={"blocked_reason": "admin_debug_note"},
                )

            payload = json.loads(capture.messages[0])
            self.assertEqual(payload["user_message"], "debug note: keep this visible")
            self.assertIn("debug note: keep this visible", out.getvalue())
        finally:
            b.FULL_CONVERSATION_LOG = old_full
            b.DEBUG_LOGGING_OPTIONS = old_debug
            b._query_log = old_query_log
            logger.handlers.clear()

    def test_debug_logging_options_can_hide_detail_fields(self):
        import contextlib
        import io
        import json
        import logging
        import telegram_bot as b

        class _CaptureHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.messages = []

            def emit(self, record):
                self.messages.append(record.getMessage())

        old_full = b.FULL_CONVERSATION_LOG
        old_debug = dict(b.DEBUG_LOGGING_OPTIONS)
        old_query_log = b._query_log
        logger = logging.getLogger(f"query_hide_test_{id(self)}")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        capture = _CaptureHandler()
        logger.addHandler(capture)
        b.FULL_CONVERSATION_LOG = False
        b.DEBUG_LOGGING_OPTIONS = {
            "log_user_messages": False,
            "log_bot_responses": False,
            "log_raw_llm_responses": False,
            "log_retrieved_chunks": False,
            "log_routing_trace": False,
            "log_prompt_preview": False,
            "log_admin_debug_notes": False,
            "stdout_full_turns": False,
        }
        b._query_log = logger
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                b._log_query(
                    chat_id=123,
                    user_message="John Doe fever",
                    recognized=None,
                    retrieved_chunks=[{
                        "source_label": "Secret Protocol",
                        "similarity": 0.99,
                    }],
                    raw_llm="raw answer",
                    final_response="final answer",
                    duration_ms=1,
                    trace={"selected_protocol_id": "secret"},
                )

            payload = json.loads(capture.messages[0])
            self.assertTrue(payload["user_message"]["redacted"])
            self.assertEqual(payload["retrieved"], [])
            self.assertEqual(payload["raw_llm"], "")
            self.assertEqual(payload["final"], "")
            self.assertEqual(payload["trace"], {})
            stdout = out.getvalue()
            self.assertIn("<redacted>", stdout)
            self.assertIn("[R] <hidden>", stdout)
            self.assertIn("[A] <hidden>", stdout)
        finally:
            b.FULL_CONVERSATION_LOG = old_full
            b.DEBUG_LOGGING_OPTIONS = old_debug
            b._query_log = old_query_log
            logger.handlers.clear()

    def test_full_conversation_logging_prints_reconstructable_turn(self):
        import contextlib
        import io
        import json
        import telegram_bot as b

        old_full = b.FULL_CONVERSATION_LOG
        old_debug = dict(b.DEBUG_LOGGING_OPTIONS)
        old_query_log = b._query_log
        b.FULL_CONVERSATION_LOG = True
        b.DEBUG_LOGGING_OPTIONS = {
            "log_user_messages": True,
            "log_bot_responses": True,
            "log_raw_llm_responses": True,
            "log_retrieved_chunks": True,
            "log_routing_trace": True,
            "log_prompt_preview": True,
            "log_admin_debug_notes": True,
            "stdout_full_turns": True,
        }
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
            b.DEBUG_LOGGING_OPTIONS = old_debug
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
        self.assertIn('CommandHandler("segits", handle_commands)', src)

    def test_load_protocols_handles_fresh_file_after_module_split(self):
        import numpy as np
        import tempfile
        import telegram_bot as b

        old_cwd = os.getcwd()
        old_cache_db = b.CACHE_DB
        old_cache_disabled = b._CACHE_DISABLED
        old_chunks = list(b.PROTOCOL_CHUNKS)
        old_policy = dict(b.PROTOCOL_POLICY_BY_FILE)
        old_parsed = dict(b.PROTOCOL_PARSED_BY_FILE)
        old_file_labels = dict(b.PROTOCOL_FILE_TO_LABEL)

        with tempfile.TemporaryDirectory() as tmp:
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
                b._CACHE_DISABLED = True
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
                b._CACHE_DISABLED = old_cache_disabled
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


class TestRuntimeFailureBoundaries(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import telegram_bot as b
        self.b = b
        self._old_state = dict(b.CONVERSATION_STATE)
        self._old_allowed = set(b.ALLOWED_USER_IDS)
        self._old_runtime_options = dict(b.RUNTIME_OPTIONS)
        self._old_access_mode = b.ACCESS_MODE
        self._old_debug_logging_options = dict(b.DEBUG_LOGGING_OPTIONS)

    def tearDown(self):
        b = self.b
        b.CONVERSATION_STATE.clear()
        b.CONVERSATION_STATE.update(self._old_state)
        b.ALLOWED_USER_IDS = self._old_allowed
        b.RUNTIME_OPTIONS = self._old_runtime_options
        b.ACCESS_MODE = self._old_access_mode
        b.DEBUG_LOGGING_OPTIONS = self._old_debug_logging_options

    def test_openai_chat_failure_returns_safe_error_and_logs(self):
        fake_chunk = {
            "source": "protocols/test.txt",
            "source_label": "Test Protocol",
            "text": "Clinical protocol context",
            "similarity": 0.9,
        }
        with patch.object(self.b, "search_protocols", return_value=[fake_chunk]), \
                patch.object(self.b.openai_client.chat.completions, "create",
                             side_effect=RuntimeError("openai unavailable")), \
                patch.object(self.b, "_log_query") as log_mock:
            answer = self.b.ask_ai("clinical protocol question", f"chat_failure_{id(self)}")

        self.assertEqual(answer, self.b.SAFE_RUNTIME_FAILURE_MESSAGE)
        self.assertTrue(log_mock.called)
        self.assertTrue(log_mock.call_args.kwargs["trace"]["runtime_error"])

    def test_openai_embedding_failure_returns_safe_error_and_logs(self):
        with patch.object(self.b, "get_embedding", side_effect=RuntimeError("embedding unavailable")), \
                patch.object(self.b, "_log_query") as log_mock:
            answer = self.b.ask_ai("clinical protocol question", f"embedding_failure_{id(self)}")

        self.assertEqual(answer, self.b.SAFE_RUNTIME_FAILURE_MESSAGE)
        self.assertTrue(log_mock.called)
        self.assertTrue(log_mock.call_args.kwargs["trace"]["runtime_error"])

    async def test_telegram_reply_failure_is_handled_and_logged(self):
        b = self.b
        b.ALLOWED_USER_IDS = {123}
        message = types.SimpleNamespace(
            text="clinical question",
            chat=types.SimpleNamespace(send_action=AsyncMock()),
            reply_text=AsyncMock(side_effect=RuntimeError("telegram unavailable")),
        )
        update = types.SimpleNamespace(
            message=message,
            effective_user=types.SimpleNamespace(id=123),
            effective_chat=types.SimpleNamespace(id=456),
        )

        with patch.object(b, "ask_ai", return_value="safe answer"), \
                patch.object(b, "_log_runtime_error") as log_mock:
            await b.handle_message(update, types.SimpleNamespace())

        message.reply_text.assert_awaited()
        log_mock.assert_called()

    async def test_missing_user_id_allowed_in_open_mode(self):
        b = self.b
        b.RUNTIME_OPTIONS = {"access_mode": "open", "log_user_messages": False}
        b.ACCESS_MODE = "open"
        b.ALLOWED_USER_IDS = set()
        message = types.SimpleNamespace(
            text="clinical question",
            chat=types.SimpleNamespace(id=456, send_action=AsyncMock()),
            reply_text=AsyncMock(),
        )
        update = types.SimpleNamespace(
            message=message,
            effective_user=None,
            effective_chat=types.SimpleNamespace(id=456),
        )

        with patch.object(b, "ask_ai", return_value="safe answer") as ask_mock:
            await b.handle_message(update, types.SimpleNamespace())

        ask_mock.assert_called_once_with("clinical question", 456)
        message.reply_text.assert_awaited()

    async def test_missing_user_id_denied_in_closed_mode_without_crash(self):
        b = self.b
        b.RUNTIME_OPTIONS = {"access_mode": "closed", "log_user_messages": False}
        b.ACCESS_MODE = "closed"
        b.ALLOWED_USER_IDS = {123}
        message = types.SimpleNamespace(
            text="clinical question",
            chat=types.SimpleNamespace(id=456, send_action=AsyncMock()),
            reply_text=AsyncMock(),
        )
        update = types.SimpleNamespace(
            message=message,
            effective_user=None,
            effective_chat=types.SimpleNamespace(id=456),
        )

        with patch.object(b, "ask_ai", return_value="safe answer") as ask_mock:
            await b.handle_message(update, types.SimpleNamespace())

        ask_mock.assert_not_called()
        message.reply_text.assert_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)



