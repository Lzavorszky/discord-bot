#!/usr/bin/env python3
"""test_get_table_dose.py — unit tests for the table_lookup dosing tool (tmpsmx).

Covers every branch of the state machine: required-input gating, indication
classification (incl. the F3/F4 prophylaxis-vs-treatment fix), renal
classification, 2-D table selection, weight-band rounding, prophylaxis without
weight/renal, renal warnings, out-of-range confirmation, and verbatim fidelity.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PKG, "tools"))
sys.path.insert(0, os.path.join(PKG, "protocols"))
sys.path.insert(0, PKG)

from get_table_dose import (  # noqa: E402
    get_table_dose, render_table_dose, load_table_lookup, TableDoseError,
)

PROTOCOLS = os.path.join(PKG, "protocols")


def gd(**kw):
    return get_table_dose("tmpsmx", protocols_dir=PROTOCOLS, **kw)


class TestLoading(unittest.TestCase):
    def test_loads_table_lookup(self):
        rec = load_table_lookup("tmpsmx", protocols_dir=PROTOCOLS)
        self.assertEqual(rec["kind"], "table_lookup")

    def test_wrong_kind_raises(self):
        with self.assertRaises(TableDoseError):
            load_table_lookup("meropenem", protocols_dir=PROTOCOLS)

    def test_missing_file_raises(self):
        with self.assertRaises(TableDoseError):
            load_table_lookup("does_not_exist", protocols_dir=PROTOCOLS)


class TestRequiredInputGate(unittest.TestCase):
    def test_no_indication_returns_default(self):
        r = gd()
        self.assertEqual(r.mode, "default")
        self.assertTrue(r.needs_input)
        self.assertIn("Indication groups", r.text)

    def test_blank_indication_returns_default(self):
        self.assertEqual(gd(indication="   ").mode, "default")

    def test_unclassifiable_indication_returns_default(self):
        r = gd(indication="something the rules don't cover", body_weight_kg=70, gfr=50)
        self.assertEqual(r.mode, "default")
        self.assertIsNone(r.indication_tier)


class TestIndicationClassification(unittest.TestCase):
    def test_pcp_treatment_is_high_dose(self):
        self.assertEqual(gd(indication="PCP pneumonia", body_weight_kg=70, gfr=50).indication_tier, "HIGH_DOSE")

    def test_nocardia_is_high_dose(self):
        self.assertEqual(gd(indication="Nocardia brain", body_weight_kg=70, gfr=50).indication_tier, "HIGH_DOSE")

    def test_stenotrophomonas_bsi_is_high_dose(self):
        self.assertEqual(gd(indication="Stenotrophomonas BSI", body_weight_kg=70, gfr=50).indication_tier, "HIGH_DOSE")

    def test_severe_cns_is_moderate(self):
        self.assertEqual(gd(indication="severe CNS infection", body_weight_kg=70, gfr=50).indication_tier, "MODERATE_DOSE")

    def test_standard_susceptible_is_standard(self):
        self.assertEqual(gd(indication="standard susceptible infection", gfr=50).indication_tier, "STANDARD_DOSE")

    # --- F3/F4: prophylaxis must NOT escalate to a treatment dose, and a
    #     treatment indication must NOT collapse to prophylaxis. ---
    def test_pcp_prophylaxis_is_prophylaxis_not_high(self):
        r = gd(indication="PCP prophylaxis")
        self.assertEqual(r.indication_tier, "PROPHYLAXIS")
        self.assertEqual(r.output_kind, "prophylaxis")
        self.assertIn("1 tablet", r.text)
        self.assertNotIn("mg/kg/day", r.text)

    def test_prophylaxis_immunosuppressed_is_prophylaxis(self):
        self.assertEqual(gd(indication="prophylaxis in immunosuppressed patient").indication_tier, "PROPHYLAXIS")

    def test_immunosuppressed_with_pcp_pneumonia_is_treatment(self):
        # immunosuppressed patient WITH a treatment indication → HIGH_DOSE,
        # not prophylaxis (the explicit "prophylaxis" word is absent).
        r = gd(indication="immunosuppressed patient with PCP pneumonia", body_weight_kg=70, gfr=50)
        self.assertEqual(r.indication_tier, "HIGH_DOSE")


class TestRenalClassification(unittest.TestCase):
    def test_gfr_gt_30(self):
        self.assertEqual(gd(indication="PCP", body_weight_kg=70, gfr=50).renal_category, "GFR_GT_30_OR_CRRT")

    def test_crrt_is_full_dose_category(self):
        self.assertEqual(gd(indication="PCP", body_weight_kg=70, crrt=True).renal_category, "GFR_GT_30_OR_CRRT")

    def test_gfr_15_to_30(self):
        self.assertEqual(gd(indication="PCP", body_weight_kg=70, gfr=20).renal_category, "GFR_15_TO_30")

    def test_gfr_boundary_15_is_reduced(self):
        self.assertEqual(gd(indication="PCP", body_weight_kg=70, gfr=15).renal_category, "GFR_15_TO_30")

    def test_gfr_boundary_30_is_reduced(self):
        self.assertEqual(gd(indication="PCP", body_weight_kg=70, gfr=30).renal_category, "GFR_15_TO_30")

    def test_gfr_31_is_full(self):
        self.assertEqual(gd(indication="PCP", body_weight_kg=70, gfr=31).renal_category, "GFR_GT_30_OR_CRRT")

    def test_gfr_lt_15(self):
        self.assertEqual(gd(indication="PCP", body_weight_kg=70, gfr=10).renal_category, "GFR_LT_15_WITHOUT_CRRT")

    def test_ihd(self):
        self.assertEqual(gd(indication="PCP", body_weight_kg=70, ihd=True).renal_category, "IHD")

    def test_crrt_precedes_ihd(self):
        # crrt rung is listed first → full-dose category even if ihd also set.
        self.assertEqual(gd(indication="PCP", body_weight_kg=70, crrt=True, ihd=True).renal_category, "GFR_GT_30_OR_CRRT")

    def test_no_renal_is_unknown(self):
        self.assertEqual(gd(indication="PCP", body_weight_kg=70).renal_category, "UNKNOWN")


class TestTableSelectionAndVerbatim(unittest.TestCase):
    def test_high_dose_full_70kg_verbatim(self):
        r = gd(indication="PCP pneumonia", body_weight_kg=70, gfr=50)
        self.assertEqual(r.table_name, "HIGH_DOSE_GFR_GT_30_OR_CRRT")
        self.assertEqual(r.output_kind, "dosing_row")
        self.assertEqual(r.selected_weight, 70)
        self.assertEqual(r.practical_dose, "3 x 4 amp")
        self.assertEqual(r.total, "960/4800 mg daily")
        self.assertIn("15-20 mg/kg/day TMP component", r.text)

    def test_high_dose_reduced_40kg_verbatim(self):
        r = gd(indication="PJP", body_weight_kg=40, gfr=20)
        self.assertEqual(r.table_name, "HIGH_DOSE_GFR_15_TO_30")
        self.assertEqual(r.practical_dose, "2 x 2 amp")
        self.assertEqual(r.total, "320/1600 mg daily")

    def test_moderate_full_80kg_verbatim(self):
        r = gd(indication="osteomyelitis", body_weight_kg=80, gfr=50)
        self.assertEqual(r.table_name, "MODERATE_DOSE_GFR_GT_30_OR_CRRT")
        self.assertEqual(r.practical_dose, "3 x 3 amp")
        self.assertEqual(r.total, "720/3600 mg daily")

    def test_standard_fixed_full(self):
        r = gd(indication="standard susceptible", gfr=50)
        self.assertEqual(r.output_kind, "fixed_dose")
        self.assertIn("2 x 2 amp", r.text)
        self.assertIn("320/1600 mg daily", r.text)

    def test_standard_fixed_reduced(self):
        r = gd(indication="oral step-down", gfr=20)
        self.assertEqual(r.table_name, "STANDARD_DOSE_GFR_15_TO_30")
        self.assertIn("160/800 mg daily", r.text)

    def test_template_echoes_weight(self):
        r = gd(indication="PCP", body_weight_kg=90, gfr=50)
        self.assertIn("body weight: 90 kg", r.text)


class TestWeightBanding(unittest.TestCase):
    def test_tie_rounds_up(self):
        # 55 is equidistant to 50 and 60 → choose the higher row (avoid underdose)
        r = gd(indication="PCP", body_weight_kg=55, gfr=50)
        self.assertEqual(r.selected_weight, 60)

    def test_closest_lower(self):
        r = gd(indication="PCP", body_weight_kg=63, gfr=50)
        self.assertEqual(r.selected_weight, 60)

    def test_closest_upper(self):
        r = gd(indication="PCP", body_weight_kg=68, gfr=50)
        self.assertEqual(r.selected_weight, 70)

    def test_exact_row(self):
        r = gd(indication="PCP", body_weight_kg=100, gfr=50)
        self.assertEqual(r.selected_weight, 100)

    def test_below_range_clamps_low_with_note(self):
        r = gd(indication="PCP", body_weight_kg=35, gfr=50)
        self.assertEqual(r.selected_weight, 40)
        self.assertIsNotNone(r.weight_note)
        self.assertIn("below the table range", r.text)

    def test_above_supported_clamps_high_with_note(self):
        # 120 kg is above the 100 kg supported row but within the clinical max
        r = gd(indication="PCP", body_weight_kg=120, gfr=50)
        self.assertEqual(r.selected_weight, 100)
        self.assertIn("individualized", r.text.lower())


class TestProphylaxisRenalMapping(unittest.TestCase):
    def test_prophylaxis_unknown_renal(self):
        r = gd(indication="PCP prophylaxis")
        self.assertEqual(r.table_name, "PROPHYLAXIS_GENERAL")

    def test_prophylaxis_full_renal(self):
        r = gd(indication="PJP prophylaxis", gfr=50)
        self.assertEqual(r.table_name, "PROPHYLAXIS_GFR_GT_30_OR_CRRT")
        self.assertIn("prolonged prophylaxis", r.text)

    def test_prophylaxis_reduced_renal(self):
        r = gd(indication="prophylaxis", gfr=20)
        self.assertEqual(r.table_name, "PROPHYLAXIS_GFR_15_TO_30")
        self.assertIn("three times weekly", r.text)

    def test_prophylaxis_gfr_lt_15_returns_warning(self):
        r = gd(indication="PCP prophylaxis", gfr=10)
        self.assertEqual(r.output_kind, "renal_warning")
        self.assertIn("avoid", r.text.lower())

    def test_prophylaxis_ihd_returns_warning(self):
        r = gd(indication="prophylaxis", ihd=True)
        self.assertEqual(r.table_name, "IHD")
        self.assertEqual(r.output_kind, "renal_warning")


class TestRenalWarningsAndGating(unittest.TestCase):
    def test_treatment_gfr_lt_15_warning(self):
        r = gd(indication="PCP", body_weight_kg=70, gfr=10)
        self.assertEqual(r.output_kind, "renal_warning")
        self.assertEqual(r.table_name, "GFR_LT_15_WITHOUT_CRRT")
        self.assertIn("avoid", r.text.lower())

    def test_treatment_ihd_warning(self):
        r = gd(indication="severe infection", body_weight_kg=70, ihd=True)
        self.assertEqual(r.output_kind, "renal_warning")
        self.assertEqual(r.table_name, "IHD")

    def test_treatment_renal_unknown_asks(self):
        r = gd(indication="Nocardia", body_weight_kg=70)
        self.assertEqual(r.mode, "needs_input")
        self.assertTrue(r.needs_input)
        self.assertEqual(r.indication_tier, "HIGH_DOSE")

    def test_treatment_weight_missing_asks_no_table_dump(self):
        r = gd(indication="PCP pneumonia", gfr=50)
        self.assertEqual(r.mode, "needs_input")
        # must NOT dump a full table of doses
        self.assertNotIn("4 x 2 amp", r.text)
        self.assertNotIn("3 x 4 amp", r.text)


class TestOutOfRange(unittest.TestCase):
    def test_weight_above_clinical_max_confirms(self):
        r = gd(indication="PCP", body_weight_kg=400, gfr=50)
        self.assertTrue(r.needs_confirmation)
        self.assertIn("body_weight_kg=400", r.confirmation_reason)

    def test_weight_below_clinical_min_confirms(self):
        r = gd(indication="PCP", body_weight_kg=0, gfr=50)
        # 0 < min 1 → confirmation
        self.assertTrue(r.needs_confirmation)

    def test_gfr_above_clinical_max_confirms(self):
        r = gd(indication="PCP", body_weight_kg=70, gfr=400)
        self.assertTrue(r.needs_confirmation)
        self.assertIn("gfr=400", r.confirmation_reason)

    def test_in_range_does_not_confirm(self):
        self.assertFalse(gd(indication="PCP", body_weight_kg=70, gfr=50).needs_confirmation)


class TestRendering(unittest.TestCase):
    def test_render_includes_source_label_and_footer(self):
        out = render_table_dose(gd(indication="PCP", body_weight_kg=70, gfr=50))
        self.assertIn("[TMP/SMX]", out)
        self.assertIn("Guidelines generally refer to the trimethoprim component", out)

    def test_render_confirmation(self):
        out = render_table_dose(gd(indication="PCP", body_weight_kg=400, gfr=50))
        self.assertIn("Needs confirmation", out)


if __name__ == "__main__":
    unittest.main()
