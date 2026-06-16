"""Unit tests for get_dose — the Phase 3.1 vertical slice (fully offline).

Covers every rung of the meropenem `select:` ladder, selection priority,
out-of-range GFR confirmation, verbatim fidelity, the restricted guard
evaluator's safety, loading, and the plain-text renderer.

The clinical *values* (e.g. NORMAL = 4 g/day) are the human's non-delegable
hand-check (meropenem_handcheck.md). These tests assert that get_dose returns
whatever the protocol file declares — i.e. the engine is faithful to the YAML.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ID_BOT2 = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ID_BOT2, "tools"))
sys.path.insert(0, os.path.join(ID_BOT2, "protocols"))

import get_dose as GD  # noqa: E402
from get_dose import get_dose, render_dose, DoseError, GuardError  # noqa: E402

PROTOCOLS = os.path.join(ID_BOT2, "protocols")


def mero(**kw):
    """get_dose against the real meropenem.yaml in the package protocols dir."""
    return get_dose("meropenem", protocols_dir=PROTOCOLS, **kw)


# --------------------------------------------------------------------------- #
# Every ladder branch (the Phase 3.1 done-when list)                          #
# --------------------------------------------------------------------------- #
def test_cns_infection_selects_step_up():
    r = mero(cns_infection=True, gfr=80)   # STEP_UP must beat NORMAL
    assert r.matched_tier.name == "STEP_UP"
    assert r.matched_rule_index == 0
    assert r.tier_names() == ["LOADING", "STEP_UP"]


def test_tdm_low_level_selects_step_up():
    r = mero(tdm_low_level=True)
    assert r.matched_tier.name == "STEP_UP"
    assert r.matched_rule_index == 0


def test_ihd_selects_severe_aki():
    r = mero(ihd=True, gfr=80)             # IHD beats NORMAL (priority 100 > 70)
    assert r.matched_tier.name == "SEVERE_AKI"
    assert r.matched_rule_index == 1


def test_crrt_selects_crrt():
    r = mero(crrt=True)
    assert r.matched_tier.name == "CRRT"
    assert r.matched_rule_index == 2


def test_gfr_ge_20_selects_normal():
    r = mero(gfr=20)
    assert r.matched_tier.name == "NORMAL"
    assert r.matched_rule_index == 3


def test_gfr_lt_20_selects_severe_aki():
    r = mero(gfr=10)
    assert r.matched_tier.name == "SEVERE_AKI"
    assert r.matched_rule_index == 4


def test_no_input_returns_default_full_table():
    r = mero()
    assert r.is_default is True
    assert r.matched_tier is None
    assert r.matched_rule_index == 5
    # Full table, in declared order.
    assert r.tier_names() == ["LOADING", "NORMAL", "SEVERE_AKI", "CRRT", "STEP_UP"]


def test_gfr_out_of_range_needs_confirmation():
    r = mero(gfr=300)
    assert r.needs_confirmation is True
    assert "above the declared maximum" in r.confirmation_reason
    assert r.matched_tier is None and r.tiers == []


# --------------------------------------------------------------------------- #
# Selection priority — list order is priority                                 #
# --------------------------------------------------------------------------- #
def test_cns_beats_ihd_and_crrt():
    r = mero(cns_infection=True, ihd=True, crrt=True, gfr=10)
    assert r.matched_tier.name == "STEP_UP"   # rung 0 wins over everything


def test_ihd_beats_crrt_and_gfr():
    r = mero(ihd=True, crrt=True, gfr=50)
    assert r.matched_tier.name == "SEVERE_AKI"  # rung 1 (ihd) before rung 2/3


def test_crrt_beats_gfr():
    r = mero(crrt=True, gfr=50)
    assert r.matched_tier.name == "CRRT"        # rung 2 before rung 3


# --------------------------------------------------------------------------- #
# GFR boundaries (declared range 0..250, ask_confirmation)                    #
# --------------------------------------------------------------------------- #
def test_gfr_at_max_boundary_is_in_range():
    r = mero(gfr=250)
    assert r.needs_confirmation is False
    assert r.matched_tier.name == "NORMAL"


def test_gfr_at_min_boundary_is_in_range():
    r = mero(gfr=0)
    assert r.needs_confirmation is False
    assert r.matched_tier.name == "SEVERE_AKI"   # 0 < 20


def test_gfr_below_min_needs_confirmation():
    r = mero(gfr=-5)
    assert r.needs_confirmation is True
    assert "below the declared minimum" in r.confirmation_reason


# --------------------------------------------------------------------------- #
# Verbatim fidelity + always_show + metadata propagation                      #
# --------------------------------------------------------------------------- #
def test_normal_tier_is_verbatim():
    r = mero(gfr=60)
    normal = r.matched_tier
    assert normal.dose == "4 g/day"            # owner-revised value, verbatim
    assert normal.when == "GFR 20+"
    assert normal.admin == "1 g/50 mL, 8.3 mL/h"


def test_loading_always_shown_on_every_match():
    for kw in ({"gfr": 60}, {"crrt": True}, {"ihd": True}, {"cns_infection": True},
               {"gfr": 5}):
        r = mero(**kw)
        assert "LOADING" in r.tier_names(), kw
        assert any(t.always_show for t in r.always_show)


def test_footer_prep_never_propagated():
    r = mero(gfr=60)
    assert r.footer == "Think TDM! replace later"
    assert "Reduced-dose preparation" in r.prep
    assert any("STEP_UP" in n for n in r.never)


def test_inputs_echoed():
    r = mero(gfr=60, crrt=True)
    assert r.inputs["gfr"] == 60 and r.inputs["crrt"] is True


# --------------------------------------------------------------------------- #
# Restricted guard evaluator — safety                                         #
# --------------------------------------------------------------------------- #
def _rec(select):
    return {
        "id": "x", "kind": "drug_dose", "source_label": "x",
        "tiers": {"A": {"dose": "1"}, "LOADING": {"dose": "0", "always_show": True}},
        "slots": {"gfr": {"type": "number", "min": 0, "max": 100,
                          "on_out_of_range": "ask_confirmation"},
                  "crrt": {"type": "bool"}},
        "select": select,
    }


def test_guard_unknown_slot_raises():
    rec = _rec([{"if": "nonsense_flag", "tier": "A"}, {"default": "DEFAULT_ANSWER"}])
    with pytest.raises(GuardError):
        get_dose("x", record=rec, gfr=5)


def test_guard_malformed_expression_raises():
    rec = _rec([{"if": "gfr >=", "tier": "A"}, {"default": "DEFAULT_ANSWER"}])
    with pytest.raises(GuardError):
        get_dose("x", record=rec, gfr=5)


def test_guard_disallows_function_calls():
    rec = _rec([{"if": "__import__('os').system('echo hi')", "tier": "A"},
                {"default": "DEFAULT_ANSWER"}])
    with pytest.raises(GuardError):
        get_dose("x", record=rec, gfr=5)


def test_guard_none_operand_is_false_then_default():
    # gfr unprovided → 'gfr > 0' is False → falls to default full table.
    rec = _rec([{"if": "gfr > 0", "tier": "A"}, {"default": "DEFAULT_ANSWER"}])
    r = get_dose("x", record=rec)
    assert r.is_default is True


def test_select_target_undefined_tier_raises():
    rec = _rec([{"if": "crrt", "tier": "GHOST"}, {"default": "DEFAULT_ANSWER"}])
    with pytest.raises(DoseError):
        get_dose("x", record=rec, crrt=True)


# --------------------------------------------------------------------------- #
# Loading + error paths                                                       #
# --------------------------------------------------------------------------- #
def test_missing_drug_raises():
    with pytest.raises(DoseError):
        get_dose("does_not_exist", protocols_dir=PROTOCOLS)


def test_record_kwarg_bypasses_disk():
    rec = _rec([{"if": "crrt", "tier": "A"}, {"default": "DEFAULT_ANSWER"}])
    r = get_dose("x", record=rec, crrt=True)
    assert r.matched_tier.name == "A"


def test_wrong_kind_record_raises():
    rec = {"id": "p", "kind": "prose", "sections": {"s": {"text": "hi"}}}
    with pytest.raises(DoseError):
        get_dose("p", record=rec)


# --------------------------------------------------------------------------- #
# Renderer                                                                     #
# --------------------------------------------------------------------------- #
def test_render_contains_tier_and_dose():
    out = render_dose(mero(gfr=60))
    assert "NORMAL" in out and "4 g/day" in out
    assert "LOADING" in out


def test_render_needs_confirmation():
    out = render_dose(mero(gfr=300))
    assert "Needs confirmation" in out
