#!/usr/bin/env python3
"""test_verifier.py — offline tests for the grounding verifier (roadmap 4.1/4.1b).

All free/offline: pure string-in, verdict-out. No API key, no network. This is
what check.sh runs every session. The two roadmap-mandated cases are explicit:
`test_catches_hallucinated_dose` and `test_faithful_paraphrase_survives`.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent          # id_bot2/
sys.path.insert(0, str(_PKG))

from verifier import verify_grounding, mode_for_kind, VerifierResult, VERIFIER_MODES  # noqa: E402

# A representative verbatim get_dose render (the ground truth the verifier trusts).
GROUNDED = (
    "[Meropenem (Merrem)]\n"
    "LOADING: 2 g stat\n"
    "NORMAL: 4 g/day (gfr >= 20) — 8.3 mL/h\n"
    "Think TDM!"
)
DRUGS = ["meropenem", "merrem", "vancomycin", "piperacillin-tazobactam"]


class TestMode(unittest.TestCase):
    def test_mode_table(self):
        self.assertEqual(mode_for_kind("drug_dose"), "hard")
        self.assertEqual(mode_for_kind("pcr_panel"), "hard")
        self.assertEqual(mode_for_kind("pathway"), "soft")
        self.assertEqual(mode_for_kind("prose"), "soft")

    def test_unknown_kind_defaults_soft(self):
        self.assertEqual(mode_for_kind(None), "soft")
        self.assertEqual(mode_for_kind("something_new"), "soft")


class TestHardBlock(unittest.TestCase):
    # --- the two roadmap-mandated cases -----------------------------------
    def test_faithful_paraphrase_survives(self):
        """A reworded but numerically faithful answer must NOT be touched."""
        fp = ("Give a 2 g loading dose, then 4 g per day "
              "(when GFR is 20 or above), infused at 8.3 mL/h. Think TDM!")
        r = verify_grounding(fp, GROUNDED, "drug_dose", known_drugs=DRUGS)
        self.assertTrue(r.ok)
        self.assertFalse(r.blocked)
        self.assertEqual(r.text, fp)
        self.assertEqual(r.violations, [])

    def test_catches_hallucinated_dose(self):
        """An invented number is blocked; the verbatim source is returned."""
        bad = "Give 2 g loading, then 6 g per day at 8.3 mL/h."
        r = verify_grounding(bad, GROUNDED, "drug_dose", known_drugs=DRUGS)
        self.assertFalse(r.ok)
        self.assertTrue(r.blocked)
        self.assertEqual(r.text, GROUNDED)        # falls back to ground truth
        self.assertTrue(any("6" in v for v in r.violations))

    # --- unit grounding ----------------------------------------------------
    def test_right_number_wrong_unit_blocked(self):
        r = verify_grounding("Then 4 mg per day.", GROUNDED, "drug_dose")
        self.assertTrue(r.blocked)
        self.assertTrue(any("unit" in v for v in r.violations))

    def test_unit_synonym_grounds(self):
        """'8.3 mL per hour' is grounded by source '8.3 mL/h'."""
        r = verify_grounding("Run at 8.3 millilitres per hour.", GROUNDED, "drug_dose")
        self.assertTrue(r.ok, r.violations)

    def test_base_unit_match_tolerates_compound(self):
        """Candidate '4 g' is grounded by source '4 g/day' (base unit g)."""
        r = verify_grounding("The total is 4 g.", GROUNDED, "drug_dose")
        self.assertTrue(r.ok, r.violations)

    def test_decimal_formatting_not_a_violation(self):
        r = verify_grounding("Infuse at 8.30 mL/h.", GROUNDED, "drug_dose")
        self.assertTrue(r.ok, r.violations)

    def test_integer_decimal_equivalence(self):
        g = "[X] dose: 4.0 g/day"
        r = verify_grounding("Give 4 g/day.", g, "drug_dose")
        self.assertTrue(r.ok, r.violations)

    # --- drug-name grounding ----------------------------------------------
    def test_wrong_drug_name_blocked(self):
        r = verify_grounding("Use vancomycin 4 g/day.", GROUNDED, "drug_dose",
                             known_drugs=DRUGS)
        self.assertTrue(r.blocked)
        self.assertTrue(any("vancomycin" in v for v in r.violations))

    def test_correct_drug_name_survives(self):
        r = verify_grounding("Meropenem: 4 g/day.", GROUNDED, "drug_dose",
                             known_drugs=DRUGS)
        self.assertTrue(r.ok, r.violations)

    def test_drug_name_only_checked_against_known_vocab(self):
        """With no known_drugs vocabulary, drug-name checks are inert."""
        r = verify_grounding("Use vancomycin 4 g/day.", GROUNDED, "drug_dose")
        self.assertTrue(r.ok, r.violations)   # number/unit fine; no vocab to flag


class TestSoftFlag(unittest.TestCase):
    def test_soft_flags_but_keeps_text(self):
        bad = "Give 6 g per day."
        r = verify_grounding(bad, GROUNDED, "prose")
        self.assertFalse(r.ok)
        self.assertFalse(r.blocked)
        self.assertTrue(r.flagged)
        self.assertEqual(r.text, bad)          # paraphrase preserved
        self.assertTrue(r.violations)

    def test_soft_clean_is_ok(self):
        r = verify_grounding("Loading dose is 2 g.", GROUNDED, "prose")
        self.assertTrue(r.ok)
        self.assertFalse(r.flagged)


class TestEdges(unittest.TestCase):
    def test_no_numbers_is_grounded(self):
        r = verify_grounding("Discuss with micro and check TDM.", GROUNDED, "drug_dose")
        self.assertTrue(r.ok, r.violations)

    def test_empty_candidate_ok(self):
        r = verify_grounding("", GROUNDED, "drug_dose")
        self.assertTrue(r.ok)

    def test_accents_and_mojibake_folded(self):
        """Hungarian phrasing with accents still grounds against ASCII source."""
        g = "[Meropenem] dozis: 4 g/day"
        r = verify_grounding("A dózisa 4 g/day.", g, "drug_dose")
        self.assertTrue(r.ok, r.violations)

    def test_result_to_dict(self):
        r = verify_grounding("6 g/day", GROUNDED, "drug_dose")
        d = r.to_dict()
        self.assertEqual(d["mode"], "hard")
        self.assertTrue(d["blocked"])
        self.assertIn("violations", d)


if __name__ == "__main__":
    unittest.main()
