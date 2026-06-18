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
    TableCall, resolve_table,
    CalcCall, resolve_calculator,
    ProseCall, resolve_prose,
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

    def test_llm_cannot_invent_an_undescribed_drug(self):
        # Decision 1 (2026-06-18): the LLM may not resolve a drug whose name is
        # absent from the message ("the big gun carbapenem" -> meropenem is the
        # model inferring a drug from a description). Refuse, never guess.
        prov = ScriptedProvider(ToolCall(name="get_dose",
                                         arguments={"drug_id": "meropenem", "gfr": 55}))
        res = self.R.route("the big gun carbapenem, kidneys at 55", provider=prov)
        self.assertEqual(prov.calls, 1)
        self.assertEqual(res.route, "unsupported")

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

    def test_llm_invented_drug_with_slots_is_refused(self):
        # Even with plausible slots, an un-named drug pick is refused outright
        # (Decision 1) — so no ungrounded dose and no slot leak.
        prov = ScriptedProvider(ToolCall(name="get_dose",
                                         arguments={"drug_id": "meropenem",
                                                    "septic_shock": True, "gfr": 60}))
        res = self.R.route("cryptic", provider=prov)
        self.assertEqual(res.route, "unsupported")

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

    def test_llm_invented_drug_refused_before_phrasing(self):
        # Decision 1 gates LLM drug invention, so the "phrase an LLM-resolved drug"
        # path no longer occurs: an un-named drug pick is refused before phrasing.
        prov = ScriptedProvider(ToolCall(name="get_dose",
                                         arguments={"drug_id": "meropenem", "gfr": 40}))
        faithful = _Phraser(lambda g: "Dosing:\n" + g)
        res = self.R.route("cryptic", provider=prov, phrasing_provider=faithful)
        self.assertEqual(res.route, "unsupported")
        self.assertFalse(res.phrased)

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


# --------------------------------------------------------------------------- #
# table_lookup routing (tmpsmx) — final migration phase.                       #
# --------------------------------------------------------------------------- #
class TestTableLookupRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    def test_registry_has_tmpsmx(self):
        self.assertIn("tmpsmx", self.R.tables)

    def test_tool_schema_exposed(self):
        names = {t.name for t in self.R.tools()}
        self.assertIn("get_table_dose", names)
        tool = next(t for t in self.R.tools() if t.name == "get_table_dose")
        self.assertEqual(tool.parameters["properties"]["table_id"]["enum"], ["tmpsmx"])

    def test_resolve_by_alias(self):
        call = resolve_table("co-trimoxazole dose", self.R.tables)
        self.assertIsInstance(call, TableCall)
        self.assertEqual(call.table_id, "tmpsmx")

    def test_resolve_none_when_absent(self):
        self.assertIsNone(resolve_table("meropenem gfr 40", self.R.tables))

    def test_indication_is_whole_message(self):
        call = resolve_table("bactrim for PCP pneumonia 70 kg gfr 40", self.R.tables)
        self.assertIn("pcp", call.slots["indication"].lower())
        self.assertEqual(call.slots.get("gfr"), 40)
        self.assertEqual(call.slots.get("body_weight_kg"), 70)

    def test_end_to_end_high_dose(self):
        r = self.R.route("tmpsmx dose for PCP pneumonia, 70 kg, gfr 40")
        self.assertEqual(r.route, "table_lookup")
        self.assertEqual(r.tool, "get_table_dose")
        self.assertEqual(r.protocol, "tmpsmx")
        self.assertEqual(r.table.indication_tier, "HIGH_DOSE")
        self.assertIn("3 x 4 amp", r.answer)

    def test_prophylaxis_not_escalated(self):
        # F3/F4: a prophylaxis request must NOT return a treatment (mg/kg) dose.
        r = self.R.route("co-trimoxazole PCP prophylaxis")
        self.assertEqual(r.protocol, "tmpsmx")
        self.assertEqual(r.table.indication_tier, "PROPHYLAXIS")
        self.assertIn("1 tablet", r.answer)
        self.assertNotIn("mg/kg", r.answer)

    def test_table_beats_incidental_pathway_keyword(self):
        # "pneumonia" is a CAP alias, but an explicitly named tmpsmx wins.
        r = self.R.route("septrin dose, severe pneumonia, 80 kg, gfr 50")
        self.assertEqual(r.route, "table_lookup")
        self.assertEqual(r.protocol, "tmpsmx")

    def test_pathway_unaffected_when_no_table_named(self):
        r = self.R.route("CAP empiric treatment, intubated")
        self.assertEqual(r.route, "pathway")
        self.assertEqual(r.protocol, "cap")

    def test_drug_routing_unaffected(self):
        r = self.R.route("meropenem gfr 40")
        self.assertEqual(r.route, "drug_dose")
        self.assertEqual(r.protocol, "meropenem")

    def test_renal_unknown_asks(self):
        r = self.R.route("bactrim nocardia 70 kg")
        self.assertEqual(r.protocol, "tmpsmx")
        self.assertTrue(r.needs_clarification)

    def test_out_of_range_weight_confirms(self):
        r = self.R.route("tmpsmx PCP 400 kg gfr 50")
        self.assertEqual(r.protocol, "tmpsmx")
        self.assertTrue(r.needs_clarification)
        self.assertIn("confirmation", r.answer.lower())

    def test_llm_path_dispatches_table(self):
        prov = ScriptedProvider(ToolCall(
            name="get_table_dose",
            arguments={"table_id": "tmpsmx", "indication": "PCP pneumonia",
                       "body_weight_kg": 70, "gfr": 40}))
        # a message with no deterministic table alias → falls to the LLM stage
        r = self.R.route("what co_trim should I give", provider=prov)
        self.assertEqual(r.route, "table_lookup")
        self.assertEqual(r.protocol, "tmpsmx")
        self.assertEqual(r.table.indication_tier, "HIGH_DOSE")

    def test_llm_unknown_table_id_is_unsupported(self):
        prov = ScriptedProvider(ToolCall(
            name="get_table_dose",
            arguments={"table_id": "not_a_table", "indication": "PCP"}))
        r = self.R.route("zzzqqq nonsense", provider=prov)
        self.assertEqual(r.route, "unsupported")


# --------------------------------------------------------------------------- #
# Calculator routing (Plan D, final migration phase). A named calculator (body  #
# size / steroid equivalence) is the only COMPUTING route; precedence is        #
# panel > table_lookup > pathway > calculator > drug.                           #
# --------------------------------------------------------------------------- #
class TestCalculatorRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = Router(protocols_dir=PROTOCOLS)

    # registry / tool schema ------------------------------------------------
    def test_registry_has_both_calculators(self):
        self.assertIn("body_size_calculators", self.router.calcs)
        self.assertIn("steroid_equivalence", self.router.calcs)

    def test_calc_tool_has_closed_enum(self):
        tool = next(t for t in self.router.tools() if t.name == "calculate")
        enum = tool.parameters["properties"]["calculator_id"]["enum"]
        self.assertTrue({"body_size_calculators", "steroid_equivalence"} <= set(enum))
        # steroid_agent surfaced as a closed enum too
        self.assertIn("steroid_agent", tool.parameters["properties"])
        self.assertIn("dexamethasone",
                      tool.parameters["properties"]["steroid_agent"]["enum"])

    # deterministic alias resolution ---------------------------------------
    def test_resolve_body_size_by_alias(self):
        call = resolve_calculator("body size calculator height 170 cm weight 70 kg",
                                  self.router.calcs)
        self.assertIsInstance(call, CalcCall)
        self.assertEqual(call.calc_id, "body_size_calculators")
        self.assertEqual(call.slots, {"height_cm": 170, "actual_weight_kg": 70})

    def test_resolve_bmi_alias_and_unit_extraction(self):
        call = resolve_calculator("bmi calculator 180 cm 90 kg", self.router.calcs)
        self.assertEqual(call.calc_id, "body_size_calculators")
        self.assertEqual(call.slots["height_cm"], 180)
        self.assertEqual(call.slots["actual_weight_kg"], 90)

    def test_resolve_steroid_equivalence(self):
        call = resolve_calculator("steroid equivalence dexamethasone 6 mg",
                                  self.router.calcs)
        self.assertEqual(call.calc_id, "steroid_equivalence")
        self.assertEqual(call.slots,
                         {"steroid_agent": "dexamethasone", "steroid_dose_mg": 6})

    def test_resolve_steroid_via_conversion_alias(self):
        call = resolve_calculator("hydrocortisone conversion 100 mg", self.router.calcs)
        self.assertEqual(call.calc_id, "steroid_equivalence")
        self.assertEqual(call.slots["steroid_agent"], "hydrocortisone")
        self.assertEqual(call.slots["steroid_dose_mg"], 100)

    def test_steroid_synonym_dexa(self):
        call = resolve_calculator("steroid equivalence dexa 6 mg", self.router.calcs)
        self.assertEqual(call.slots.get("steroid_agent"), "dexamethasone")

    def test_no_calculator_named_returns_none(self):
        self.assertIsNone(resolve_calculator("meropenem gfr 40", self.router.calcs))

    # end-to-end route ------------------------------------------------------
    def test_route_body_size_computes(self):
        res = self.router.route("body size calculator height 170 cm weight 70 kg")
        self.assertEqual(res.route, "calculator")
        self.assertEqual(res.tool, "calculate")
        self.assertEqual(res.protocol, "body_size_calculators")
        self.assertIn("BMI: 24.2", res.answer)
        self.assertFalse(res.needs_clarification)

    def test_route_steroid_computes(self):
        res = self.router.route("steroid equivalence dexamethasone 6 mg")
        self.assertEqual(res.protocol, "steroid_equivalence")
        self.assertIn("hydrocortisone: 160.00 mg", res.answer)

    def test_route_calculator_no_input_is_default(self):
        res = self.router.route("body size calculator")
        self.assertEqual(res.route, "calculator")
        self.assertIn("cm", res.answer)

    def test_route_calculator_partial_asks(self):
        res = self.router.route("ideal body weight calculator height 165 cm")
        self.assertEqual(res.route, "calculator")
        self.assertTrue(res.needs_clarification)

    def test_route_calculator_out_of_range_confirms(self):
        res = self.router.route("body size calculator height 400 cm weight 70 kg")
        self.assertEqual(res.route, "calculator")
        self.assertTrue(res.needs_clarification)

    def test_drug_query_not_captured_by_calculator(self):
        res = self.router.route("meropenem gfr 40")
        self.assertEqual(res.route, "drug_dose")
        self.assertEqual(res.protocol, "meropenem")

    # LLM path --------------------------------------------------------------
    def test_llm_dispatch_calculate(self):
        prov = ScriptedProvider(ToolCall(
            name="calculate",
            arguments={"calculator_id": "body_size_calculators",
                       "height_cm": 170, "actual_weight_kg": 70}))
        # use an input with no deterministic calculator alias so the LLM stage runs
        res = self.router.route("compute body metrics 170 70", provider=prov)
        self.assertEqual(prov.calls, 1)
        self.assertEqual(res.route, "calculator")
        self.assertEqual(res.protocol, "body_size_calculators")
        self.assertIn("BMI", res.answer)

    def test_llm_unknown_calculator_id_unsupported(self):
        prov = ScriptedProvider(ToolCall(
            name="calculate", arguments={"calculator_id": "made_up_calc"}))
        res = self.router.route("compute something weird", provider=prov)
        self.assertEqual(res.route, "unsupported")
        self.assertIsNone(res.dose)

    def test_llm_prose_not_treated_as_answer(self):
        prov = ScriptedProvider("BMI is weight over height squared")
        res = self.router.route("how do i compute body metrics", provider=prov)
        self.assertEqual(res.route, "unsupported")



class TestEchoCalculatorRouting(unittest.TestCase):
    """The echo trio (final calculator migration): named-calculator routing +
    LABELLED measurement/unit extraction. The deterministic stage must (a) route
    the right echo calculator, (b) pull each labelled measurement WITH its unit so
    two same-unit values never collide, and (c) leave a unit-less measurement's
    *_unit slot absent so the tool asks rather than guessing a conversion."""

    @classmethod
    def setUpClass(cls):
        cls.router = Router(protocols_dir=PROTOCOLS)

    # registry / tool schema ------------------------------------------------
    def test_registry_has_three_echo_calcs(self):
        for cid in ("echo_cardiac_output", "echo_ava", "echo_ero_rvol"):
            self.assertIn(cid, self.router.calcs)

    def test_calc_tool_enum_includes_echo(self):
        tool = next(t for t in self.router.tools() if t.name == "calculate")
        enum = set(tool.parameters["properties"]["calculator_id"]["enum"])
        self.assertTrue({"echo_cardiac_output", "echo_ava", "echo_ero_rvol"} <= enum)
        # a unit enum slot is surfaced as a closed enum to the model
        props = tool.parameters["properties"]
        self.assertEqual(set(props["lvot_diameter_unit"]["enum"]), {"mm", "cm"})
        self.assertEqual(set(props["aliasing_velocity_unit"]["enum"]),
                         {"m_per_s", "cm_per_s"})

    # deterministic extraction ---------------------------------------------
    def test_cardiac_output_extracts_two_cm_values_distinctly(self):
        call = resolve_calculator(
            "echo cardiac output calculator lvot diameter 2.0 cm lvot vti 20 cm hr 70",
            self.router.calcs)
        self.assertIsInstance(call, CalcCall)
        self.assertEqual(call.calc_id, "echo_cardiac_output")
        self.assertEqual(call.slots["lvot_diameter"], 2)
        self.assertEqual(call.slots["lvot_diameter_unit"], "cm")
        self.assertEqual(call.slots["lvot_vti"], 20)
        self.assertEqual(call.slots["lvot_vti_unit"], "cm")
        self.assertEqual(call.slots["heart_rate_bpm"], 70)

    def test_ava_extracts_csa_and_bsa(self):
        call = resolve_calculator(
            "ava calculator lvot csa 3.0 lvot vti 18 cm av vti 90 cm bsa 2.0",
            self.router.calcs)
        self.assertEqual(call.calc_id, "echo_ava")
        self.assertEqual(call.slots["lvot_csa"], 3)
        self.assertEqual(call.slots["lvot_vti"], 18)
        self.assertEqual(call.slots["av_vti"], 90)
        self.assertEqual(call.slots["bsa_m2"], 2)

    def test_ero_extracts_velocity_units(self):
        call = resolve_calculator(
            "pisa calculator pisa radius 1.0 cm aliasing velocity 40 cm/s "
            "peak regurgitant velocity 500 cm/s regurgitant vti 100 cm",
            self.router.calcs)
        self.assertEqual(call.calc_id, "echo_ero_rvol")
        self.assertEqual(call.slots["aliasing_velocity"], 40)
        self.assertEqual(call.slots["aliasing_velocity_unit"], "cm_per_s")
        self.assertEqual(call.slots["peak_regurgitant_velocity_unit"], "cm_per_s")
        self.assertEqual(call.slots["pisa_radius_unit"], "cm")

    def test_unitless_measurement_leaves_unit_slot_absent(self):
        call = resolve_calculator(
            "echo cardiac output calculator lvot diameter 2.0 lvot vti 20 hr 70",
            self.router.calcs)
        self.assertEqual(call.slots.get("lvot_diameter"), 2)
        self.assertNotIn("lvot_diameter_unit", call.slots)

    # end-to-end route ------------------------------------------------------
    def test_route_cardiac_output_computes(self):
        res = self.router.route(
            "echo cardiac output calculator lvot diameter 2.0 cm lvot vti 20 cm hr 70")
        self.assertEqual(res.route, "calculator")
        self.assertEqual(res.tool, "calculate")
        self.assertEqual(res.protocol, "echo_cardiac_output")
        self.assertIn("Cardiac output: 4.40 L/min", res.answer)
        self.assertFalse(res.needs_clarification)

    def test_route_ava_continuity_computes(self):
        res = self.router.route(
            "aortic valve area calculator lvot diameter 2.0 cm lvot vti 20 cm av vti 100 cm")
        self.assertEqual(res.protocol, "echo_ava")
        self.assertIn("AVA: 0.63 cm2", res.answer)

    def test_route_ero_direct_computes(self):
        res = self.router.route("eroa calculator eroa 0.5 jet vti 100 cm")
        self.assertEqual(res.protocol, "echo_ero_rvol")
        self.assertIn("Regurgitant volume: 50.0 mL", res.answer)

    def test_route_missing_unit_asks_to_resend(self):
        res = self.router.route(
            "echo cardiac output calculator lvot diameter 2.0 lvot vti 20 hr 70")
        self.assertEqual(res.route, "calculator")
        self.assertTrue(res.needs_clarification)
        self.assertIn("mm or cm", res.answer)

    def test_route_echo_no_input_is_default(self):
        res = self.router.route("aortic valve area calculator")
        self.assertEqual(res.route, "calculator")
        self.assertEqual(res.protocol, "echo_ava")
        self.assertFalse(res.answer.startswith("Echo AVA by continuity"))

    def test_drug_query_not_captured_by_echo(self):
        res = self.router.route("meropenem gfr 40")
        self.assertEqual(res.route, "drug_dose")
        self.assertEqual(res.protocol, "meropenem")

    # LLM path --------------------------------------------------------------
    def test_llm_dispatch_echo(self):
        prov = ScriptedProvider(ToolCall(
            name="calculate",
            arguments={"calculator_id": "echo_cardiac_output",
                       "lvot_diameter": 2.0, "lvot_diameter_unit": "cm",
                       "lvot_vti": 20, "lvot_vti_unit": "cm",
                       "heart_rate_bpm": 70}))
        # input with no deterministic alias so the LLM stage runs
        res = self.router.route("work out the forward output 2 by 20 at 70",
                                provider=prov)
        self.assertEqual(res.route, "calculator")
        self.assertEqual(res.protocol, "echo_cardiac_output")
        self.assertIn("Cardiac output", res.answer)


class TestProseRouting(unittest.TestCase):
    """The prose stage (answer_from_section): periop meds / steroids / dantrolene.
    Resolved AFTER the drug stage; selects ONE verbatim section, never composes."""
    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    # registry + tool schema -----------------------------------------------
    def test_registry_loads_three_prose_protocols(self):
        self.assertEqual(
            set(self.R.prose),
            {"periop_gyogyszerek", "periop_steroids", "dantrolene_mh"})

    def test_prose_tool_is_closed_enum(self):
        tool = next(t for t in self.R.tools() if t.name == "answer_from_section")
        enum = tool.parameters["properties"]["prose_id"]["enum"]
        self.assertEqual(set(enum), set(self.R.prose))
        self.assertEqual(tool.parameters["required"], ["prose_id"])

    # deterministic resolution ---------------------------------------------
    def test_resolve_periop_med_aspirin(self):
        call = resolve_prose("aspirin before surgery", self.R.prose)
        self.assertIsInstance(call, ProseCall)
        self.assertEqual(call.prose_id, "periop_gyogyszerek")
        self.assertEqual(call.section, "aspirin")

    def test_resolve_periop_med_warfarin(self):
        call = resolve_prose("warfarin perioperative", self.R.prose)
        self.assertEqual(call.section, "warfarin")

    def test_resolve_no_drug_named_section_none(self):
        call = resolve_prose("perioperative medications", self.R.prose)
        self.assertIsInstance(call, ProseCall)
        self.assertEqual(call.prose_id, "periop_gyogyszerek")
        self.assertIsNone(call.section)

    def test_resolve_steroid_guide(self):
        call = resolve_prose("perioperative steroid stress dose", self.R.prose)
        self.assertEqual(call.prose_id, "periop_steroids")

    def test_resolve_dantrolene_by_mh(self):
        call = resolve_prose("malignant hyperthermia", self.R.prose)
        self.assertEqual(call.prose_id, "dantrolene_mh")

    def test_cross_listed_drug_is_ambiguous_section(self):
        # selexipag appears in both the respiratory and antiplatelet entries.
        call = resolve_prose("selexipag perioperative", self.R.prose)
        self.assertIsInstance(call, ProseCall)
        self.assertEqual(len(call.candidates), 2)
        self.assertIn("selexipag_respiratory", call.candidates)

    def test_prazosin_cross_listed_ambiguous(self):
        call = resolve_prose("prazosin before surgery", self.R.prose)
        self.assertEqual(len(call.candidates), 2)

    def test_no_periop_context_no_prose(self):
        # A bare drug with no perioperative context must NOT match a prose guide.
        self.assertIsNone(resolve_prose("aspirin dose", self.R.prose))

    # end-to-end route() ----------------------------------------------------
    def test_route_aspirin_returns_complete_entry(self):
        res = self.R.route("aspirin before surgery")
        self.assertEqual(res.route, "prose")
        self.assertEqual(res.tool, "answer_from_section")
        self.assertEqual(res.protocol, "periop_gyogyszerek")
        self.assertFalse(res.needs_clarification)
        # the antithrombotic entry comes back complete (both timing rows)
        self.assertIn("no need to omit", res.answer.lower())
        self.assertIn("high bleeding risk", res.answer.lower())

    def test_route_dabigatran_has_renal_split(self):
        res = self.R.route("dabigatran perioperative gfr 40")
        self.assertEqual(res.route, "prose")
        self.assertIn("GFR", res.answer)
        self.assertIn("Epidural catheter: forbidden", res.answer)

    def test_route_topicless_asks(self):
        res = self.R.route("perioperative medications")
        self.assertEqual(res.route, "prose")
        self.assertTrue(res.needs_clarification)
        self.assertIn("medication", res.answer.lower())

    def test_route_steroid_table_verbatim(self):
        res = self.R.route("perioperative steroids")
        self.assertEqual(res.route, "prose")
        self.assertEqual(res.protocol, "periop_steroids")
        self.assertIn("hydrocortisone", res.answer.lower())

    def test_route_dantrolene_verbatim_and_not_a_calculator(self):
        res = self.R.route("dantrolene")
        self.assertEqual(res.route, "prose")
        self.assertIn("Dantrium", res.answer)
        self.assertIn("Agilus", res.answer)
        self.assertFalse(res.needs_clarification)

    def test_cross_listed_route_clarifies(self):
        res = self.R.route("selexipag perioperative")
        self.assertEqual(res.route, "clarify")
        self.assertEqual(res.tool, "ask_clarification")
        self.assertTrue(res.needs_clarification)

    def test_drug_precedence_preserved(self):
        # a real antibiotic dose request still wins (drug stage before prose)
        res = self.R.route("meropenem gfr 40")
        self.assertEqual(res.route, "drug_dose")
        self.assertEqual(res.protocol, "meropenem")

    def test_bare_drug_no_periop_unsupported(self):
        res = self.R.route("aspirin 500 mg dose")
        self.assertEqual(res.route, "unsupported")
        self.assertEqual(res.tool, "none")

    # LLM path --------------------------------------------------------------
    def test_llm_dispatch_prose(self):
        prov = ScriptedProvider(ToolCall(
            name="answer_from_section",
            arguments={"prose_id": "periop_gyogyszerek", "section": "warfarin"}))
        res = self.R.route("what about that blood thinner round the operation",
                           provider=prov)
        self.assertEqual(res.route, "prose")
        self.assertEqual(res.protocol, "periop_gyogyszerek")
        self.assertIn("INR <1.5", res.answer)

    def test_llm_unknown_prose_id_falls_through(self):
        prov = ScriptedProvider(ToolCall(
            name="answer_from_section",
            arguments={"prose_id": "not_a_guide", "section": "x"}))
        res = self.R.route("zzz nonsense topic", provider=prov)
        self.assertEqual(res.route, "unsupported")



class TestLLMDrugInventionGuard(unittest.TestCase):
    """Decision 1 (2026-06-18): the LLM stage must not introduce a drug whose
    name is absent from the message (e.g. organism -> guessed drug). Semantic
    pathway routing stays allowed."""
    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    def test_llm_invented_drug_is_refused(self):
        # "Stenotrophomonas severe infection" names no drug; model picks ceftazidime.
        prov = ScriptedProvider(ToolCall(name="get_dose",
                                         arguments={"drug_id": "ceftazidime"}))
        res = self.R.route("stenotrophomonas severe infection 70kg gfr 70", provider=prov)
        self.assertEqual(res.route, "unsupported")
        self.assertEqual(res.tool, "none")

    def test_llm_drug_allowed_when_named_in_message(self):
        # Control: the drug IS in the message -> the guard permits the pick.
        prov = ScriptedProvider(ToolCall(name="get_dose",
                                         arguments={"drug_id": "ceftazidime"}))
        res = self.R.route("ceftazidime please", provider=prov)
        self.assertEqual(res.route, "drug_dose")
        self.assertEqual(res.protocol, "ceftazidime")

    def test_llm_pathway_routing_still_semantic(self):
        # Pathways are NOT gated — a semantic CAP pick with no literal 'cap' stands.
        prov = ScriptedProvider(ToolCall(name="select_pathway",
                                         arguments={"pathway_id": "cap"}))
        res = self.R.route("tudogyulladasra mit adjak", provider=prov)
        self.assertEqual(res.route, "pathway")
        self.assertEqual(res.protocol, "cap")



class TestOrganismPathwayGuard(unittest.TestCase):
    """An organism query must not be guessed into a syndrome pathway by the LLM
    (Decision 2026-06-18): 'Stenotrophomonas bsi' -> UTI is refused. Pure-syndrome
    queries (no organism token) still route."""
    @classmethod
    def setUpClass(cls):
        cls.R = Router(protocols_dir=PROTOCOLS)

    def test_organism_bsi_not_routed_to_pathway(self):
        prov = ScriptedProvider(ToolCall(name="select_pathway",
                                         arguments={"pathway_id": "uti"}))
        res = self.R.route("Stenotrophomonas bsi, 60kg, gfr 60", provider=prov)
        self.assertNotEqual(res.route, "pathway")     # never UTI for a BSI
        self.assertIn(res.route, ("unsupported", "clarify"))

    def test_pure_syndrome_still_routes(self):
        prov = ScriptedProvider(ToolCall(name="select_pathway",
                                         arguments={"pathway_id": "cap"}))
        res = self.R.route("tudogyulladasra mit adjak", provider=prov)   # no organism token
        self.assertEqual(res.route, "pathway")
        self.assertEqual(res.protocol, "cap")

    def test_names_organism_helper(self):
        self.assertTrue(self.R._names_organism("stenotrophomonas bsi"))
        self.assertFalse(self.R._names_organism("what to give for pneumonia"))
