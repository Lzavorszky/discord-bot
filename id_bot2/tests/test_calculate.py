#!/usr/bin/env python3
"""test_calculate.py — the calculator tool (the only COMPUTING tool).

Two jobs:
  1. Prove the restricted arithmetic evaluator is SAFE (rejects everything that
     isn't arithmetic over declared names + literals + the function whitelist).
  2. Prove every migrated formula computes the HAND-COMPUTED value, and that the
     required-slot / out-of-range / unsupported-value gates behave.

Hand-computed reference values are written out in the comments so a reviewer can
re-derive them without trusting the code.
"""
import math
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
TOOLS = os.path.abspath(os.path.join(HERE, "..", "tools"))
PROTO = os.path.abspath(os.path.join(HERE, "..", "protocols"))
sys.path.insert(0, TOOLS)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "protocols")))
import calculate as C  # noqa: E402

PDIR = PROTO


# --------------------------------------------------------------------------- #
# 1. The restricted arithmetic evaluator — correctness                        #
# --------------------------------------------------------------------------- #
def test_eval_basic_arithmetic_and_precedence():
    assert C.eval_expr("2 + 3 * 4", {}) == 14
    assert C.eval_expr("(2 + 3) * 4", {}) == 20
    assert C.eval_expr("2 ** 3 ** 2", {}) == 512        # right-assoc
    assert C.eval_expr("7 / 2", {}) == 3.5
    assert C.eval_expr("7 // 2", {}) == 3
    assert C.eval_expr("7 % 2", {}) == 1
    assert C.eval_expr("-5 + 2", {}) == -3


def test_eval_names_and_constants():
    assert C.eval_expr("a * b", {"a": 3, "b": 4}) == 12
    assert C.eval_expr("pi", {}) == math.pi
    assert C.eval_expr("2 * pi", {}) == 2 * math.pi


def test_eval_whitelisted_functions():
    assert C.eval_expr("sqrt(16)", {}) == 4
    assert C.eval_expr("abs(-3)", {}) == 3
    assert C.eval_expr("max(2, 9, 4)", {}) == 9
    assert C.eval_expr("min(2, 9, 4)", {}) == 2
    assert C.eval_expr("round(2.345, 2)", {}) == 2.34 or C.eval_expr("round(2.345, 2)", {}) == 2.35


# --------------------------------------------------------------------------- #
# 1b. The evaluator — SAFETY (must reject everything non-arithmetic)           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("expr", [
    "__import__('os').system('x')",
    "a.b",                     # attribute access
    "x[0]",                    # subscript
    "open('f')",               # non-whitelisted call
    "exec('1')",
    "[i for i in range(3)]",   # comprehension
    "lambda: 1",
    "a if b else c",           # ternary
    "'string'",                # non-numeric literal
    "True and False",          # boolean ops not part of arithmetic grammar
])
def test_eval_rejects_non_arithmetic(expr):
    with pytest.raises(C.CalcError):
        C.eval_expr(expr, {"a": 1, "b": 1, "c": 1, "x": [1]})


def test_eval_unknown_name_raises():
    with pytest.raises(C.CalcError) as ei:
        C.eval_expr("height_cm / 100", {})
    assert "unknown name" in str(ei.value)


def test_eval_keyword_args_rejected():
    with pytest.raises(C.CalcError):
        C.eval_expr("round(2.5, ndigits=1)", {})


# --------------------------------------------------------------------------- #
# 2. Template rendering                                                        #
# --------------------------------------------------------------------------- #
def test_render_template_formats_fields():
    out = C.render_template("BMI {bmi:.1f} kg/m2", {"bmi": 24.2153})
    assert out == "BMI 24.2 kg/m2"


def test_render_template_unknown_field_raises():
    with pytest.raises(C.CalcError):
        C.render_template("x {nope}", {"bmi": 1})


def test_render_template_rejects_attribute_field():
    with pytest.raises(C.CalcError):
        C.render_template("x {a.b}", {"a": 1})


# --------------------------------------------------------------------------- #
# 3. body_size_calculators — hand-computed values                             #
# --------------------------------------------------------------------------- #
# height 170 cm, weight 70 kg:
#   height_m = 1.70
#   BMI      = 70 / 1.70^2 = 70 / 2.89          = 24.2215  -> 24.2
#   BSA      = sqrt(170*70/3600) = sqrt(3.30556)= 1.81812  -> 1.82
#   IBW_m    = 50  + 0.91*(170-152.4) = 50+16.016 = 66.016 -> 66.0
#   IBW_f    = 45.5+ 0.91*(170-152.4) = 45.5+16.016=61.516 -> 61.5
#   AdjBW_m  = 66.016 + 0.4*(70-66.016) = 67.6096 -> 67.6
#   AdjBW_f  = 61.516 + 0.4*(70-61.516) = 64.9096 -> 64.9
def test_body_size_hand_values():
    r = C.calculate("body_size_calculators", protocols_dir=PDIR,
                    height_cm=170, actual_weight_kg=70)
    assert r.mode == "compute"
    v = r.values
    assert v["height_m"] == pytest.approx(1.70)
    assert v["bmi"] == pytest.approx(70 / 1.70 ** 2)
    assert round(v["bmi"], 1) == 24.2
    assert v["bsa_m2"] == pytest.approx(math.sqrt(170 * 70 / 3600))
    assert round(v["bsa_m2"], 2) == 1.82
    assert v["ibw_male_kg"] == pytest.approx(50 + 0.91 * (170 - 152.4))
    assert round(v["ibw_male_kg"], 1) == 66.0
    assert v["ibw_female_kg"] == pytest.approx(45.5 + 0.91 * (170 - 152.4))
    assert round(v["ibw_female_kg"], 1) == 61.5
    assert round(v["adjbw_male_kg"], 1) == 67.6
    assert round(v["adjbw_female_kg"], 1) == 64.9


def test_body_size_reports_both_sex_formulas():
    # Source SAFETY_RULE: report BOTH male-formula and female-formula results.
    r = C.calculate("body_size_calculators", protocols_dir=PDIR,
                    height_cm=180, actual_weight_kg=90)
    txt = C.render_calc(r)
    assert "IBW (male formula)" in txt
    assert "IBW (female formula)" in txt
    assert "male-formula IBW" in txt
    assert "female-formula IBW" in txt


def test_body_size_footer_present():
    r = C.calculate("body_size_calculators", protocols_dir=PDIR,
                    height_cm=170, actual_weight_kg=70)
    assert "use the relevant clinical protocol" in C.render_calc(r)


def test_body_size_no_input_returns_default():
    r = C.calculate("body_size_calculators", protocols_dir=PDIR)
    assert r.mode == "default"
    assert "cm" in r.text and "kg" in r.text


def test_body_size_partial_input_asks():
    r = C.calculate("body_size_calculators", protocols_dir=PDIR, height_cm=170)
    assert r.mode == "needs_input"
    assert r.needs_input
    assert "height in cm and actual body weight in kg" in r.text


@pytest.mark.parametrize("h,w", [(400, 70), (50, 70), (170, 5000), (170, 0.2)])
def test_body_size_out_of_range_asks_confirmation(h, w):
    r = C.calculate("body_size_calculators", protocols_dir=PDIR,
                    height_cm=h, actual_weight_kg=w)
    assert r.mode == "needs_confirmation"
    assert r.needs_confirmation
    assert r.confirmation_reason


def test_body_size_boundary_values_compute():
    # min/max edges are in-range (inclusive), must compute, not ask.
    r = C.calculate("body_size_calculators", protocols_dir=PDIR,
                    height_cm=80, actual_weight_kg=350)
    assert r.mode == "compute"


def test_body_size_string_numeric_coercion():
    r = C.calculate("body_size_calculators", protocols_dir=PDIR,
                    height_cm="170", actual_weight_kg="70")
    assert r.mode == "compute"
    assert round(r.values["bmi"], 1) == 24.2


def test_body_size_comma_decimal_coercion():
    r = C.calculate("body_size_calculators", protocols_dir=PDIR,
                    height_cm="170,5", actual_weight_kg="70")
    assert r.mode == "compute"
    assert r.values["height_m"] == pytest.approx(1.705)


# --------------------------------------------------------------------------- #
# 4. steroid_equivalence — hand-computed values                               #
# --------------------------------------------------------------------------- #
# dexamethasone 6 mg: factor = 6 / 1.5 = 4
#   methylprednisone = 4*8  = 32
#   dexamethasone    = 4*1.5= 6
#   hydrocortisone   = 4*40 = 160
#   prednisolone     = 4*10 = 40
#   fludrocortisone  = 4*4  = 16
def test_steroid_dexamethasone_6mg():
    r = C.calculate("steroid_equivalence", protocols_dir=PDIR,
                    steroid_agent="dexamethasone", steroid_dose_mg=6)
    assert r.mode == "compute"
    v = r.values
    assert v["factor"] == 4
    assert v["eq_methylprednisone"] == 32
    assert v["eq_dexamethasone"] == 6
    assert v["eq_hydrocortisone"] == 160
    assert v["eq_prednisolone"] == 40
    assert v["eq_fludrocortisone"] == 16


# methylprednisone 8 mg: factor = 8/8 = 1 -> identity row (the reference doses).
def test_steroid_methylprednisone_8mg_identity():
    r = C.calculate("steroid_equivalence", protocols_dir=PDIR,
                    steroid_agent="methylprednisone", steroid_dose_mg=8)
    v = r.values
    assert v["factor"] == 1
    assert v["eq_methylprednisone"] == 8
    assert v["eq_dexamethasone"] == 1.5
    assert v["eq_hydrocortisone"] == 40
    assert v["eq_prednisolone"] == 10
    assert v["eq_fludrocortisone"] == 4


# hydrocortisone 100 mg: factor = 100/40 = 2.5 -> dexamethasone = 2.5*1.5 = 3.75
def test_steroid_hydrocortisone_100mg():
    r = C.calculate("steroid_equivalence", protocols_dir=PDIR,
                    steroid_agent="hydrocortisone", steroid_dose_mg=100)
    assert r.values["factor"] == 2.5
    assert r.values["eq_dexamethasone"] == pytest.approx(3.75)
    assert r.values["eq_prednisolone"] == 25


def test_steroid_unsupported_agent():
    r = C.calculate("steroid_equivalence", protocols_dir=PDIR,
                    steroid_agent="budesonide", steroid_dose_mg=6)
    assert r.mode == "unsupported_value"
    assert "only for methylprednisone" in r.text


def test_steroid_missing_dose_asks():
    r = C.calculate("steroid_equivalence", protocols_dir=PDIR,
                    steroid_agent="dexamethasone")
    assert r.mode == "needs_input"
    assert "steroid and dose in mg" in r.text


def test_steroid_no_input_default():
    r = C.calculate("steroid_equivalence", protocols_dir=PDIR)
    assert r.mode == "default"
    assert "supported steroid and dose in mg" in r.text


def test_steroid_out_of_range_dose():
    r = C.calculate("steroid_equivalence", protocols_dir=PDIR,
                    steroid_agent="dexamethasone", steroid_dose_mg=99999)
    assert r.mode == "needs_confirmation"


def test_steroid_footer_has_generic_table():
    r = C.calculate("steroid_equivalence", protocols_dir=PDIR,
                    steroid_agent="prednisolone", steroid_dose_mg=10)
    txt = C.render_calc(r)
    assert "Generic steroid equivalence table" in txt
    assert "30:0" in txt           # dexamethasone activity row, verbatim


# --------------------------------------------------------------------------- #
# 5. Loading / error handling                                                 #
# --------------------------------------------------------------------------- #
def test_load_wrong_kind_raises():
    with pytest.raises(C.CalcError):
        C.load_calculator("meropenem", protocols_dir=PDIR)


def test_load_missing_file_raises():
    with pytest.raises(C.CalcError):
        C.load_calculator("no_such_calculator", protocols_dir=PDIR)


def test_outputs_dict_contains_declared_outputs_only():
    r = C.calculate("body_size_calculators", protocols_dir=PDIR,
                    height_cm=170, actual_weight_kg=70)
    assert set(r.outputs) == {"bmi", "bsa_m2", "ibw_male_kg", "ibw_female_kg",
                              "adjbw_male_kg", "adjbw_female_kg"}
    assert "height_m" in r.values and "height_m" not in r.outputs


def test_to_dict_roundtrip_keys():
    r = C.calculate("body_size_calculators", protocols_dir=PDIR,
                    height_cm=170, actual_weight_kg=70)
    d = r.to_dict()
    assert d["route"] == "calculator" and d["tool"] == "calculate"
    assert d["mode"] == "compute" and d["method_id"] == "body_size"


def test_non_numeric_for_numeric_slot_is_treated_absent():
    # A non-numeric weight is ignored (treated absent) -> partial -> asks.
    r = C.calculate("body_size_calculators", protocols_dir=PDIR,
                    height_cm=170, actual_weight_kg="heavy")
    assert r.mode == "needs_input"
