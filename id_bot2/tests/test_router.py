#!/usr/bin/env python3
"""test_router.py — offline tests for the Plan D router (roadmap 3.5).

All offline/free (no API key, no network): the deterministic resolver is
exercised directly, and the LLM path is driven by a scripted fake provider that
returns canned ToolCalls (the same pattern test_provider_contract.py uses). This
is what check.sh runs every session.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent          # id_bot2/
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_PKG / "llm"))

from router import Router, resolve_call, RoutedCall, RouterResult   # noqa: E402
from llm.tools import Tool, ToolCall                                 # noqa: E402

PROTOCOLS = str(_PKG / "protocols")


# --------------------------------------------------------------------------- #
# Scripted providers for the LLM path                                         #
# --------------------------------------------------------------------------- #
class ScriptedProvider:
    """Returns a preset ToolCall (or string) and records that it was consulted."""
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def chat(self, messages, **kw):  # pragma: no cover - unused here
        return ""

    def call_with_tools(self, messages, tools, **kw):
        self.calls += 1
        return self.response


class ExplodingProvider:
    """A provider that must never be called (asserts the deterministic fast path)."""
    def chat(self, messages, **kw):  # pragma: no cover
        raise AssertionError("chat() should not be called")

    def call_with_tools(self, messages, tools, **kw):
        raise AssertionError("LLM provider was called for a deterministically-resolvable message")


class TestRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    def test_registry_loads_all_drug_dose_protocols(self):
        # 30 migrated drug_dose protocols (29 antibiotics + vancomycin).
        self.assertEqual(len(self.R.registry), 30)
        self.assertIn("meropenem", self.R.registry)
        self.assertIn("vancomycin", self.R.registry)

    def test_tool_schema_has_closed_drug_enum(self):
        tool = self.R.tools()[0]
        self.assertEqual(tool.name, "get_dose")
        enum = tool.parameters["properties"]["drug_id"]["enum"]
        self.assertEqual(set(enum), set(self.R.registry))
        self.assertEqual(tool.parameters["required"], ["drug_id"])


class TestAliasResolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    def _drug(self, msg):
        r = resolve_call(msg, self.R.registry)
        return r.drug_id if isinstance(r, RoutedCall) else r

    def test_canonical_and_short_aliases(self):
        self.assertEqual(self._drug("meropenem dose"), "meropenem")
        self.assertEqual(self._drug("mero gfr 30"), "meropenem")
        self.assertEqual(self._drug("MEM CRRT"), "meropenem")

    def test_accent_and_case_folding(self):
        # ceftazidim (no e) is a declared alias; folding + case must still hit.
        self.assertEqual(self._drug("CEFTAZIDIM dose"), "ceftazidime")

    def test_separator_normalisation_hyphen_slash_space(self):
        for form in ("imipenem-relebactam", "imipenem/relebactam", "imipenem relebactam"):
            self.assertEqual(self._drug(f"{form} gfr 40"),
                             "imipenem_cilastatin_relebactam", form)

    def test_compound_beats_component_via_containment(self):
        # 'imipenem' (component) must NOT win over 'imipenem relebactam'.
        self.assertEqual(self._drug("Imipenem-relebactam dose"),
                         "imipenem_cilastatin_relebactam")
        # 'ceftazidime' must NOT win over 'ceftazidime avibactam'.
        self.assertEqual(self._drug("Ceftazidime-avibactam IHD dose"),
                         "ceftazidime_avibactam")
        # but a bare component name still resolves to the component.
        self.assertEqual(self._drug("ceftazidime gfr 60"), "ceftazidime")
        self.assertEqual(self._drug("imipenem dose"), "imipenem_cilastatin")

    def test_brand_and_short_aliases(self):
        self.assertEqual(self._drug("tazocin dose"), "piperacillin_tazobactam")
        self.assertEqual(self._drug("pip-tazo gfr 60"), "piperacillin_tazobactam")

    def test_unknown_drug_returns_none(self):
        self.assertIsNone(self._drug("aspirin dose"))
        self.assertIsNone(self._drug("what is the capital of France"))

    def test_two_distinct_drugs_is_ambiguous(self):
        r = resolve_call("meropenem and vancomycin", self.R.registry)
        self.assertIsInstance(r, list)
        self.assertEqual(set(r), {"meropenem", "vancomycin"})

    def test_substring_safety_no_partial_word_hits(self):
        # 'hd' (an ihd trigger) must not fire inside an unrelated word, and a
        # random word containing 'mero' must not resolve to meropenem.
        self.assertIsNone(self._drug("numerology"))


class TestSlotExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    def _slots(self, msg):
        r = resolve_call(msg, self.R.registry)
        return r.slots

    def test_gfr_variants(self):
        for msg, exp in [("meropenem gfr 60", 60), ("meropenem gfr=45", 45),
                         ("meropenem egfr 30", 30), ("meropenem crcl 22", 22),
                         ("meropenem creatinine clearance 15", 15)]:
            self.assertEqual(self._slots(msg).get("gfr"), exp, msg)

    def test_gfr_decimal(self):
        self.assertEqual(self._slots("meropenem gfr 12.5").get("gfr"), 12.5)

    def test_boolean_slots(self):
        self.assertTrue(self._slots("meropenem CRRT dose").get("crrt"))
        self.assertTrue(self._slots("meropenem IHD").get("ihd"))
        self.assertTrue(self._slots("meropenem CNS infection").get("cns_infection"))
        self.assertTrue(self._slots("meropenem low levels").get("tdm_low_level"))

    def test_only_declared_slots_are_populated(self):
        # 'septic shock' is declared by ceftriaxone but NOT meropenem, so the
        # word 'shock' must never set a slot on meropenem.
        self.assertNotIn("septic_shock", self._slots("meropenem septic shock"))
        self.assertTrue(self._slots("ceftriaxone septic shock").get("septic_shock"))

    def test_hypoalbuminaemia_phrases(self):
        self.assertTrue(self._slots("ceftriaxone low albumin").get("hypoalbuminemia"))
        self.assertTrue(self._slots("ceftriaxone albumin <30").get("hypoalbuminemia"))

    def test_vancomycin_numeric_slots(self):
        s = self._slots("vancomycin level 12 weight 70 gfr 40")
        self.assertEqual(s.get("vancomycin_level"), 12)
        self.assertEqual(s.get("body_weight_kg"), 70)
        self.assertEqual(s.get("gfr"), 40)

    def test_weight_kg_suffix_form(self):
        self.assertEqual(self._slots("vancomycin 80 kg gfr 50").get("body_weight_kg"), 80)

    def test_no_invented_slots(self):
        self.assertEqual(self._slots("meropenem dose"), {})


class TestRouteEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    def test_normal_gfr_tier(self):
        res = self.R.route("meropenem gfr 60")
        self.assertEqual(res.route, "drug_dose")
        self.assertEqual(res.tool, "get_dose")
        self.assertEqual(res.protocol, "meropenem")
        self.assertIn("4 g/day", res.answer)
        self.assertNotIn("STEP_UP", res.answer)
        self.assertFalse(res.needs_clarification)
        self.assertEqual(res.via, "deterministic")

    def test_crrt_tier(self):
        res = self.R.route("meropenem CRRT dose")
        self.assertIn("CRRT", res.answer)
        self.assertIn("3 g/day", res.answer)

    def test_step_up_overrides_gfr(self):
        res = self.R.route("meropenem CNS infection gfr 80")
        self.assertIn("STEP_UP", res.answer)
        self.assertIn("6 g/day", res.answer)

    def test_default_full_table_when_no_slots(self):
        res = self.R.route("meropenem dose")
        for tok in ("LOADING", "NORMAL", "SEVERE_AKI", "CRRT", "STEP_UP"):
            self.assertIn(tok, res.answer)

    def test_out_of_range_gfr_asks(self):
        res = self.R.route("meropenem gfr 400")
        self.assertTrue(res.needs_clarification)
        self.assertIn("confirmation", res.answer.lower())

    def test_unsupported_message_is_explicit_not_silent(self):
        res = self.R.route("what's the weather today")
        self.assertEqual(res.route, "unsupported")
        self.assertEqual(res.tool, "none")
        self.assertTrue(res.answer.strip())

    def test_ambiguous_message_clarifies(self):
        res = self.R.route("dose for meropenem or vancomycin")
        self.assertEqual(res.route, "clarify")
        self.assertTrue(res.needs_clarification)
        self.assertEqual(set(res.candidates), {"meropenem", "vancomycin"})


class TestLLMPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    def test_deterministic_fast_path_skips_provider(self):
        # A resolvable message must NOT consult the LLM.
        res = self.R.route("meropenem gfr 60", provider=ExplodingProvider())
        self.assertEqual(res.protocol, "meropenem")
        self.assertEqual(res.via, "deterministic")

    def test_llm_resolves_when_deterministic_fails(self):
        # A message the keyword resolver can't crack, but the model can.
        prov = ScriptedProvider(ToolCall(name="get_dose",
                                         arguments={"drug_id": "meropenem", "gfr": 55}))
        res = self.R.route("the big gun carbapenem, kidneys at 55", provider=prov)
        self.assertEqual(prov.calls, 1)
        self.assertEqual(res.route, "drug_dose")
        self.assertEqual(res.protocol, "meropenem")
        self.assertEqual(res.via, "llm")
        self.assertIn("4 g/day", res.answer)   # gfr 55 -> NORMAL

    def test_llm_invalid_args_falls_through_to_unsupported(self):
        # drug_id missing required -> validate_arguments fails -> no silent dose.
        prov = ScriptedProvider(ToolCall(name="get_dose", arguments={"gfr": 55}))
        res = self.R.route("some cryptic carbapenem ask", provider=prov)
        self.assertEqual(res.route, "unsupported")

    def test_llm_unknown_drug_id_falls_through(self):
        prov = ScriptedProvider(ToolCall(name="get_dose",
                                         arguments={"drug_id": "not_a_real_drug"}))
        res = self.R.route("cryptic ask", provider=prov)
        self.assertEqual(res.route, "unsupported")

    def test_llm_plain_string_is_not_treated_as_an_answer(self):
        # The model answering with prose (no tool) must NOT become a dose answer.
        prov = ScriptedProvider("Meropenem is usually 1g TDS")
        res = self.R.route("cryptic ask", provider=prov)
        self.assertEqual(res.route, "unsupported")

    def test_llm_drops_slots_not_declared_by_protocol(self):
        # Model hallucinates septic_shock on meropenem (which lacks that slot):
        # the router must drop it (get_dose would ignore it, but we keep state clean).
        prov = ScriptedProvider(ToolCall(name="get_dose",
                                         arguments={"drug_id": "meropenem",
                                                    "septic_shock": True, "gfr": 60}))
        res = self.R.route("cryptic", provider=prov)
        self.assertEqual(res.slots, {"gfr": 60})


if __name__ == "__main__":
    unittest.main(verbosity=2)
