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

from router import (   # noqa: E402
    Router, resolve_call, RoutedCall, RouterResult,
    PanelCall, resolve_pcr,
    PathwayCall, resolve_pathway,
    SAFETY_RULES, ROUTER_SYSTEM, PHRASING_SYSTEM,
)
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
        # 29 migrated drug_dose protocols (28 antibiotics + vancomycin;
        # imipenem/cilastatin/relebactam removed 2026-06-17 — not on formulary).
        self.assertEqual(len(self.R.registry), 29)
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
        for form in ("ceftazidime-avibactam", "ceftazidime/avibactam", "ceftazidime avibactam"):
            self.assertEqual(self._drug(f"{form} gfr 40"),
                             "ceftazidime_avibactam", form)

    def test_compound_beats_component_via_containment(self):
        # 'ceftazidime' (component) must NOT win over 'ceftazidime avibactam'.
        self.assertEqual(self._drug("Ceftazidime-avibactam IHD dose"),
                         "ceftazidime_avibactam")
        # but a bare component name still resolves to the component.
        self.assertEqual(self._drug("ceftazidime gfr 60"), "ceftazidime")
        # plain imipenem/cilastatin resolves to itself (relebactam variant removed).
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

class TestPhrasingVerifierLoop(unittest.TestCase):
    """The router -> get_dose -> phrase -> verify loop (roadmap 4.1).

    A phrasing provider's `chat` rewrites the verbatim tool text; the grounding
    verifier (hard-block for drug_dose) then either passes it through or, on any
    ungrounded number/unit/drug, falls back to the verbatim text. All offline:
    the phraser is scripted, never a real model."""

    def setUp(self):
        self.R = Router(protocols_dir=PROTOCOLS)

    def test_no_phraser_keeps_verbatim(self):
        """Default (check.sh) path: no phrasing provider -> verbatim render_dose."""
        res = self.R.route("meropenem gfr 40")
        self.assertFalse(res.phrased)
        self.assertFalse(res.phrasing_blocked)
        self.assertEqual(res.answer, res.grounded_answer)
        self.assertTrue(res.answer.startswith("[meropenem]"))

    def test_faithful_phrasing_survives(self):
        faithful = _Phraser(lambda g: "Meropenem dosing:\n" + g)  # echoes all numbers
        res = self.R.route("meropenem gfr 40", phrasing_provider=faithful)
        self.assertTrue(res.phrased)
        self.assertFalse(res.phrasing_blocked)
        self.assertTrue(res.answer.startswith("Meropenem dosing:"))
        self.assertIn("4 g/day", res.answer)

    def test_hallucinated_number_is_blocked(self):
        bad = _Phraser(lambda g: "Meropenem: give 6 g/day at 8.3 mL/h.")
        res = self.R.route("meropenem gfr 40", phrasing_provider=bad)
        self.assertFalse(res.phrased)
        self.assertTrue(res.phrasing_blocked)
        self.assertEqual(res.answer, res.grounded_answer)   # verbatim fallback
        self.assertNotIn("6 g/day", res.answer)

    def test_wrong_drug_phrasing_is_blocked(self):
        bad = _Phraser(lambda g: "Use vancomycin 4 g/day.")
        res = self.R.route("meropenem gfr 40", phrasing_provider=bad)
        self.assertTrue(res.phrasing_blocked)
        self.assertEqual(res.answer, res.grounded_answer)

    def test_phrasing_failure_falls_back_to_verbatim(self):
        res = self.R.route("meropenem gfr 40", phrasing_provider=_ExplodingPhraser())
        self.assertFalse(res.phrased)
        self.assertFalse(res.phrasing_blocked)        # not a block — a safe fallback
        self.assertEqual(res.answer, res.grounded_answer)

    def test_empty_phrasing_keeps_verbatim(self):
        res = self.R.route("meropenem gfr 40", phrasing_provider=_Phraser(lambda g: "  "))
        self.assertEqual(res.answer, res.grounded_answer)

    def test_out_of_range_is_never_phrased(self):
        # A needs-confirmation result must not be run through the phrasing model.
        faithful = _Phraser(lambda g: "ANYTHING")
        res = self.R.route("meropenem gfr 999", phrasing_provider=faithful)
        self.assertTrue(res.needs_clarification)
        self.assertFalse(res.phrased)
        self.assertIn("confirm", res.answer.lower())

    def test_phrasing_via_llm_route(self):
        # The phrasing step also runs when the drug was resolved by the LLM router.
        prov = ScriptedProvider(ToolCall(name="get_dose",
                                         arguments={"drug_id": "meropenem", "gfr": 40}))
        faithful = _Phraser(lambda g: "Dosing:\n" + g)
        res = self.R.route("cryptic", provider=prov, phrasing_provider=faithful)
        self.assertEqual(res.via, "llm")
        self.assertTrue(res.phrased)

    def test_init_level_phraser_used_by_default(self):
        R = Router(protocols_dir=PROTOCOLS,
                   phrasing_provider=_Phraser(lambda g: "X:\n" + g))
        res = R.route("meropenem gfr 40")
        self.assertTrue(res.phrased)


class _Phraser:
    """Scripted phrasing provider: chat(messages) -> fn(grounded_text)."""
    def __init__(self, fn):
        self.fn = fn

    def chat(self, messages, **kw):
        return self.fn(messages[-1]["content"])

    def call_with_tools(self, *a, **k):  # pragma: no cover - unused
        return ""


class _ExplodingPhraser:
    def chat(self, messages, **kw):
        raise RuntimeError("phrasing model down")

    def call_with_tools(self, *a, **k):  # pragma: no cover
        return ""


class _CapturingProvider:
    """Records the messages it is handed, then returns a preset response.
    Lets a test assert WHAT prompt the router/phraser actually sent."""
    def __init__(self, response):
        self.response = response
        self.seen_messages = None

    def chat(self, messages, **kw):
        self.seen_messages = messages
        # echo the grounded text unchanged so the verifier passes it through
        return messages[-1]["content"]

    def call_with_tools(self, messages, tools, **kw):
        self.seen_messages = messages
        return self.response


def _system_text(messages):
    """The concatenated content of any system-role messages."""
    return "\n".join(m["content"] for m in messages if m.get("role") == "system")


class TestSafetyRuleParity(unittest.TestCase):
    """Roadmap 4.2 — the legacy system_rules.txt safety guarantees are present in
    the rebuilt router AND answerer (phrasing) prompts, as one shared constant so
    they cannot drift, and they actually reach the model at call time."""

    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    # --- the three named rules the roadmap calls out -----------------------
    def test_no_identifiers_rule_present(self):
        self.assertIn("identifier", SAFETY_RULES.lower())
        self.assertIn("ignore", SAFETY_RULES.lower())  # ignore + remind, not store

    def test_no_outside_knowledge_rule_present(self):
        low = SAFETY_RULES.lower()
        self.assertIn("outside medical knowledge", low)
        self.assertIn("only", low)            # "answer ONLY from ... protocols"
        self.assertIn("invent", low)          # never invent doses/drugs/etc.

    def test_escalate_on_conflict_rule_present(self):
        low = SAFETY_RULES.lower()
        self.assertIn("conflict", low)
        self.assertIn("senior clinician review", low)

    # --- parity: one shared constant in BOTH seams -------------------------
    def test_safety_rules_embedded_in_both_prompts(self):
        self.assertIn(SAFETY_RULES, ROUTER_SYSTEM)
        self.assertIn(SAFETY_RULES, PHRASING_SYSTEM)

    def test_router_system_message_carries_safety_rules(self):
        # A message the deterministic resolver can't crack -> LLM stage, so we can
        # inspect the exact system prompt the router sends.
        prov = _CapturingProvider(
            ToolCall(name="get_dose", arguments={"drug_id": "meropenem", "gfr": 55}))
        self.R.route("the big gun carbapenem, kidneys at 55", provider=prov)
        self.assertIsNotNone(prov.seen_messages)
        self.assertIn(SAFETY_RULES, _system_text(prov.seen_messages))

    def test_phrasing_system_message_carries_safety_rules(self):
        phraser = _CapturingProvider(None)   # chat() echoes grounded text -> survives
        res = self.R.route("meropenem gfr 40", phrasing_provider=phraser)
        self.assertIsNotNone(phraser.seen_messages)
        self.assertIn(SAFETY_RULES, _system_text(phraser.seen_messages))
        self.assertTrue(res.phrased)

    # --- behavioural: patient identifiers can never ride into the engine ---
    def test_identifiers_never_become_clinical_slots(self):
        # Even with a name, MRN and DOB in the message, only the declared clinical
        # slot (gfr) is extracted; the identifier digits do not leak into slots.
        res = self.R.route(
            "meropenem gfr 40 for John Smith MRN 1234567 dob 01/01/1980")
        self.assertEqual(res.route, "drug_dose")
        self.assertEqual(res.protocol, "meropenem")
        self.assertEqual(res.slots, {"gfr": 40})
        self.assertNotIn("1234567", str(res.slots))


class TestNotCoveredOutcome(unittest.TestCase):
    """Roadmap 4.3 — "not covered by uploaded protocols" is an explicit, tested
    outcome: route == 'unsupported', no tool, no dose, a non-empty message — and
    NEVER a silently-emitted answer (closes F10/F11)."""

    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    def _assert_not_covered(self, res):
        self.assertEqual(res.route, "unsupported")
        self.assertEqual(res.tool, "none")
        self.assertIsNone(res.dose)                 # nothing emitted
        self.assertFalse(res.needs_clarification)
        self.assertTrue(res.answer.strip())         # but it does say something
        self.assertIn("protocol", res.answer.lower())

    def test_off_topic_message_is_explicit_not_covered(self):
        self._assert_not_covered(self.R.route("what's the weather today"))

    def test_drug_not_in_protocol_set_is_not_covered(self):
        # aspirin is not in the uploaded antibiotic protocol set.
        self._assert_not_covered(self.R.route("aspirin 500 mg dose"))

    def test_llm_prose_answer_does_not_become_a_silent_dose(self):
        # The model free-generates a dose (no tool call). It must be discarded,
        # not surfaced — the no-outside-knowledge guarantee at the router seam.
        prov = ScriptedProvider("Meropenem is usually 1 g TDS.")
        res = self.R.route("how do I dose the big carbapenem", provider=prov)
        self._assert_not_covered(res)
        self.assertNotIn("1 g TDS", res.answer)

    def test_llm_off_set_drug_is_not_covered(self):
        prov = ScriptedProvider(
            ToolCall(name="get_dose", arguments={"drug_id": "not_a_real_drug"}))
        self._assert_not_covered(self.R.route("cryptic ask", provider=prov))

    def test_not_covered_without_provider_is_still_explicit(self):
        # No LLM provider at all (the offline check.sh path): an unresolvable
        # message still gets the explicit not-covered outcome, never silence.
        self._assert_not_covered(self.R.route("tell me a joke"))


class TestPcrRouting(unittest.TestCase):
    """Deterministic PCR routing (roadmap 3.2): panel detection, organism/marker
    extraction, disambiguation (F5), panel listing (F6), membership (F7/F8)."""

    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    def test_panel_named_routes_to_pcr_panel(self):
        r = self.R.route("Biofire pneumonia panel pneumococcus")
        self.assertEqual(r.route, "pcr_panel")
        self.assertEqual(r.tool, "interpret_pcr")
        self.assertEqual(r.protocol, "biofire_pneumonia")
        self.assertIn("ceftriaxone", r.answer)

    def test_joint_panel_alias_jipcr(self):
        r = self.R.route("JiPCR Klebsiella oxytoca")
        self.assertEqual(r.protocol, "biofire_joint_infection")
        self.assertIn("ceftriaxone", r.answer)

    def test_bare_genus_disambiguates_f5(self):
        r = self.R.route("JiPCR Klebsiella")
        self.assertEqual(r.route, "pcr_panel")
        self.assertTrue(r.needs_clarification)
        self.assertIn("oxytoca", r.answer)
        self.assertIn("pneumoniae", r.answer)

    def test_panel_list_uses_list_panel_f6(self):
        r = self.R.route("JiPCR panel list")
        self.assertEqual(r.tool, "list_panel")
        self.assertIn("Staphylococcus aureus", r.answer)
        self.assertTrue(any("Klebsiella" in x for x in r.pcr.panel_organisms))

    def test_influenza_recognised_on_panel_f7(self):
        r = self.R.route("Pneumonia PCR influenza")
        self.assertEqual(r.route, "pcr_panel")
        self.assertIn("oseltamivir", r.answer)
        self.assertNotIn("not on panel", r.answer.lower())

    def test_bare_organism_no_panel_asks_which_panel_f8(self):
        r = self.R.route("Mycoplasma")
        self.assertEqual(r.route, "clarify")
        self.assertTrue(r.needs_clarification)
        self.assertIn("biofire_pneumonia", r.candidates)

    def test_marker_with_organism_escalates(self):
        r = self.R.route("Pneumonia PCR E. coli CTX-M")
        self.assertEqual(r.protocol, "biofire_pneumonia")
        self.assertIn("ertapenem", r.answer)        # PN: CTX-M -> ertapenem

    def test_marker_with_organism_joint_meropenem(self):
        r = self.R.route("BioFire JI E. coli CTX-M")
        self.assertIn("meropenem", r.answer)         # JI: CTX-M -> meropenem

    def test_staph_mecA_routes_to_mrsa(self):
        r = self.R.route("Pneumonia panel Staphylococcus aureus mecA")
        self.assertIn("vancomycin", r.answer)

    def test_drug_request_unaffected_by_pcr_stage(self):
        r = self.R.route("meropenem gfr 40")
        self.assertEqual(r.route, "drug_dose")
        self.assertEqual(r.protocol, "meropenem")

    def test_pcr_tools_exposed_to_llm(self):
        names = {t.name for t in self.R.tools()}
        self.assertIn("interpret_pcr", names)
        self.assertIn("list_panel", names)
        self.assertIn("get_dose", names)

    def test_pcr_panel_id_enum_closed(self):
        tool = next(t for t in self.R.tools() if t.name == "interpret_pcr")
        enum = tool.parameters["properties"]["panel_id"]["enum"]
        self.assertEqual(set(enum), {"biofire_pneumonia", "biofire_joint_infection"})

    def test_resolve_pcr_none_when_no_panel(self):
        self.assertIsNone(resolve_pcr("meropenem dose", self.R.panels))

    def test_resolve_pcr_returns_panel_call(self):
        out = resolve_pcr("JiPCR Klebsiella oxytoca", self.R.panels)
        self.assertIsInstance(out, PanelCall)
        self.assertEqual(out.panel_id, "biofire_joint_infection")
        self.assertEqual(out.tool, "interpret_pcr")

    def test_llm_can_call_interpret_pcr(self):
        # When nothing resolves deterministically, the LLM may pick interpret_pcr.
        call = ToolCall(name="interpret_pcr",
                        arguments={"panel_id": "biofire_pneumonia",
                                   "organisms": ["Streptococcus pneumoniae"]})
        R = Router(protocols_dir=PROTOCOLS, provider=ScriptedProvider(call))
        r = R.route("interpret my respiratory result, pneumococcus detected by the analyser")
        self.assertEqual(r.route, "pcr_panel")
        self.assertEqual(r.via, "llm")
        self.assertIn("ceftriaxone", r.answer)

    def test_llm_interpret_pcr_unknown_panel_falls_through(self):
        call = ToolCall(name="interpret_pcr",
                        arguments={"panel_id": "made_up_panel",
                                   "organisms": ["x"]})
        R = Router(protocols_dir=PROTOCOLS, provider=ScriptedProvider(call))
        r = R.route("some opaque message with no panel or drug token zzz")
        self.assertEqual(r.route, "unsupported")

    def test_pcr_answer_carries_no_dose_numbers(self):
        r = self.R.route("Pneumonia PCR E. coli CTX-M")
        self.assertNotIn("mg", r.answer)
        self.assertNotIn("g/day", r.answer)


class TestPathwayRouting(unittest.TestCase):
    """Phase 2.5 cont. — the deterministic pathway stage + LLM dispatch."""

    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    # --- registry / tool schema ------------------------------------------
    def test_pathway_tool_registered(self):
        names = [t.name for t in self.R.tools()]
        self.assertIn("select_pathway", names)

    def test_pathway_tool_pathway_id_is_closed_enum(self):
        tool = next(t for t in self.R.tools() if t.name == "select_pathway")
        enum = tool.parameters["properties"]["pathway_id"]["enum"]
        for pid in ("cap", "uti", "sbp", "cdiff", "endocarditis_antibiotics",
                    "intraabdominal_infections"):
            self.assertIn(pid, enum)

    def test_six_pathways_registered(self):
        self.assertEqual(
            set(self.R.pathways),
            {"cap", "uti", "sbp", "cdiff", "endocarditis_antibiotics",
             "intraabdominal_infections"})

    # --- deterministic detection -----------------------------------------
    def test_resolve_pathway_cap(self):
        call = resolve_pathway("community acquired pneumonia, intubated",
                               self.R.pathways)
        self.assertIsInstance(call, PathwayCall)
        self.assertEqual(call.pathway_id, "cap")
        self.assertEqual(call.tool, "select_pathway")

    def test_resolve_pathway_none_for_drug(self):
        # a pure drug query names no pathway -> None (drug stage handles it)
        self.assertIsNone(resolve_pathway("meropenem gfr 40", self.R.pathways))

    def test_resolve_pathway_ambiguous_returns_list(self):
        out = resolve_pathway("CAP and UTI", self.R.pathways)
        self.assertIsInstance(out, list)
        self.assertEqual(set(out), {"cap", "uti"})

    # --- end-to-end routing ----------------------------------------------
    def test_route_cap_intubated(self):
        r = self.R.route("CAP intubated patient")
        self.assertEqual(r.route, "pathway")
        self.assertEqual(r.tool, "select_pathway")
        self.assertEqual(r.protocol, "cap")
        self.assertEqual(r.pathway.output, "INTUBATED_CAP")

    def test_route_cap_hospitalized_nosocomial(self):
        r = self.R.route("pneumonia, hospitalized, nosocomial risk")
        self.assertEqual(r.protocol, "cap")
        self.assertEqual(r.pathway.output, "HOSPITALIZED_NOSOCOMIAL_RISK")
        self.assertIn("levofloxacin", r.answer)

    def test_route_uti_complicated_hospitalized(self):
        r = self.R.route("UTI, complicated, hospitalized")
        self.assertEqual(r.route, "pathway")
        self.assertEqual(r.protocol, "uti")
        self.assertEqual(r.pathway.output, "COMPLICATED_HOSPITALIZED")

    def test_route_uti_asymptomatic(self):
        r = self.R.route("asymptomatic bacteriuria")
        self.assertEqual(r.protocol, "uti")
        self.assertEqual(r.pathway.output, "ASYMPTOMATIC_BACTERIURIA")

    def test_route_cdiff_default_no_section(self):
        r = self.R.route("cdiff")
        self.assertEqual(r.protocol, "cdiff")
        self.assertTrue(r.pathway.is_default)
        self.assertIn("diagnosis", r.answer)
        self.assertIn("treatment", r.answer)

    def test_route_cdiff_treatment(self):
        r = self.R.route("C diff treatment")
        self.assertEqual(r.pathway.output, "TREATMENT_CHUNK")

    def test_route_sbp(self):
        r = self.R.route("spontaneous bacterial peritonitis")
        self.assertEqual(r.protocol, "sbp")
        self.assertIn("paracentesis", r.answer)

    def test_route_endocarditis_targeted(self):
        r = self.R.route("endocarditis treatment, MRSA, prosthetic valve")
        self.assertEqual(r.protocol, "endocarditis_antibiotics")
        self.assertEqual(r.pathway.output, "MRSA_PVE")

    def test_route_endocarditis_pathway_beats_penicillin_drug(self):
        # 'penicillin allergy' contains the migrated drug 'penicillin'; the named
        # pathway must still win (pathway stage precedes drug stage).
        r = self.R.route("infective endocarditis treatment, penicillin allergy")
        self.assertEqual(r.route, "pathway")
        self.assertEqual(r.protocol, "endocarditis_antibiotics")
        self.assertEqual(r.pathway.output, "EMPIRIC_NVE_LATE_PVE_PENICILLIN_ALLERGY")

    def test_route_iai_context(self):
        r = self.R.route("intraabdominal infection, pancreatitis")
        self.assertEqual(r.protocol, "intraabdominal_infections")
        self.assertEqual(r.pathway.output, "PANCREATITIS")

    # --- precedence: a real drug query is unaffected ---------------------
    def test_drug_query_still_routes_to_get_dose(self):
        r = self.R.route("meropenem gfr 40")
        self.assertEqual(r.route, "drug_dose")
        self.assertEqual(r.protocol, "meropenem")

    def test_named_panel_still_takes_precedence(self):
        r = self.R.route("biofire joint infection: Klebsiella oxytoca")
        self.assertEqual(r.route, "pcr_panel")

    # --- pathway answer never emits a guessed dose (verbatim only) --------
    def test_pathway_answer_is_verbatim_output(self):
        r = self.R.route("UTI uncomplicated")
        # the answer is exactly render_pathway over the selected output
        self.assertIn("fosfomycin", r.answer)
        self.assertIn("nitrofurantoin", r.answer)

    # --- LLM dispatch -----------------------------------------------------
    def test_llm_select_pathway(self):
        call = ToolCall(name="select_pathway",
                        arguments={"pathway_id": "uti",
                                   "syndrome_class": "uncomplicated_uti"})
        R = Router(protocols_dir=PROTOCOLS, provider=ScriptedProvider(call))
        r = R.route("opaque free text with no pathway or drug token zzz")
        self.assertEqual(r.route, "pathway")
        self.assertEqual(r.protocol, "uti")
        self.assertEqual(r.via, "llm")
        self.assertEqual(r.pathway.output, "UNCOMPLICATED_UTI")

    def test_llm_select_pathway_unknown_id_falls_through(self):
        call = ToolCall(name="select_pathway",
                        arguments={"pathway_id": "made_up_pathway"})
        R = Router(protocols_dir=PROTOCOLS, provider=ScriptedProvider(call))
        r = R.route("opaque free text with no pathway or drug token zzz")
        self.assertEqual(r.route, "unsupported")

    def test_llm_select_pathway_drops_undeclared_slots(self):
        # gfr is not a uti slot; it must be dropped, not passed to select_pathway.
        call = ToolCall(name="select_pathway",
                        arguments={"pathway_id": "uti",
                                   "asymptomatic_bacteriuria": True, "gfr": 40})
        R = Router(protocols_dir=PROTOCOLS, provider=ScriptedProvider(call))
        r = R.route("opaque free text with no pathway or drug token zzz")
        self.assertEqual(r.pathway.output, "ASYMPTOMATIC_BACTERIURIA")
        self.assertNotIn("gfr", r.slots)


if __name__ == "__main__":
    unittest.main(verbosity=2)
