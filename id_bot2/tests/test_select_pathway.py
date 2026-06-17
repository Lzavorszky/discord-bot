"""Unit tests for select_pathway (Plan D, Phase 2.5 cont.) — the pathway tool.

Covers the ordered select-ladder selection for all six migrated pathways
(CAP, UTI, SBP, cdiff, endocarditis, intra-abdominal infections): single-guard
matches, list-order priority (a higher rung wins over a lower one), the terminal
default rung, verbatim fidelity, undeclared-slot tolerance, guard safety, and the
load/wrong-kind errors. Offline — loads the migrated pathways from the package
protocols dir.
"""
import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent          # id_bot2/
sys.path.insert(0, str(_PKG / "tools"))
sys.path.insert(0, str(_PKG / "protocols"))

import select_pathway as sp  # noqa: E402
from get_dose import GuardError  # noqa: E402

PD = str(_PKG / "protocols")


def _run(pathway, **kw):
    return sp.select_pathway(pathway, protocols_dir=PD, **kw)


# --------------------------------------------------------------------------- #
# Loading / errors                                                            #
# --------------------------------------------------------------------------- #
def test_load_pathway_ok():
    rec = sp.load_pathway("cap", protocols_dir=PD)
    assert rec["kind"] == "pathway" and rec["id"] == "cap"


def test_missing_pathway_raises():
    with pytest.raises(sp.PathwayError):
        _run("does_not_exist")


def test_wrong_kind_raises():
    # meropenem is a drug_dose protocol, not a pathway.
    with pytest.raises(sp.PathwayError):
        _run("meropenem")


def test_result_route_and_tool():
    r = _run("cap", intubated=True)
    assert r.route == "pathway" and r.tool == "select_pathway"
    assert r.pathway_id == "cap"


# --------------------------------------------------------------------------- #
# CAP — every branch + list-order priority                                    #
# --------------------------------------------------------------------------- #
def test_cap_intubated_bool():
    r = _run("cap", intubated=True)
    assert r.output == "INTUBATED_CAP"
    assert "BioFire Pneumonia Panel" in r.items and "ceftriaxone" in r.items


def test_cap_intubated_via_patient_status_enum():
    r = _run("cap", patient_status="intubated")
    assert r.output == "INTUBATED_CAP"


def test_cap_intubated_beats_hospitalized_priority():
    # Both intubated (rung 0) and hospitalized supplied -> the higher rung wins.
    r = _run("cap", intubated=True, patient_status="hospitalized",
             nosocomial_risk=True)
    assert r.output == "INTUBATED_CAP"


def test_cap_influenza():
    assert _run("cap", influenza=True).output == "INFLUENZA"


def test_cap_aspiration():
    assert _run("cap", aspiration_event=True).output == "ASPIRATION_PNEUMONIA"


def test_cap_copd():
    assert _run("cap", copd_exacerbation=True).output == "COPD_ACUTE_EXACERBATION"


def test_cap_hospitalized_nosocomial():
    r = _run("cap", patient_status="hospitalized", nosocomial_risk=True)
    assert r.output == "HOSPITALIZED_NOSOCOMIAL_RISK"
    assert r.items == ["levofloxacin"]


def test_cap_hospitalized_standard():
    r = _run("cap", patient_status="hospitalized")
    assert r.output == "HOSPITALIZED_STANDARD"
    assert "ceftriaxone" in r.items and "clarithromycin" in r.items


def test_cap_dischargeable_viral_positive():
    r = _run("cap", patient_status="dischargeable", viral_test_result="positive")
    assert r.output == "OUTPATIENT_VIRAL_POSITIVE"


def test_cap_dischargeable_atypical():
    r = _run("cap", patient_status="dischargeable", atypical_suspicion=True)
    assert r.output == "OUTPATIENT_ATYPICAL"
    assert r.items == ["azithromycin"]


def test_cap_dischargeable_standard():
    # bare dischargeable -> amoxicillin (viral-positive rung doesn't fire).
    r = _run("cap", patient_status="dischargeable")
    assert r.output == "OUTPATIENT_STANDARD"
    assert r.items == ["amoxicillin"]


def test_cap_viral_positive_beats_atypical_priority():
    # dischargeable + viral positive + atypical -> viral-positive wins (rung order).
    r = _run("cap", patient_status="dischargeable",
             viral_test_result="positive", atypical_suspicion=True)
    assert r.output == "OUTPATIENT_VIRAL_POSITIVE"


def test_cap_default_no_input():
    r = _run("cap")
    assert r.output == "DEFAULT_ANSWER" and r.is_default
    assert "quick map" in r.text_en


# --------------------------------------------------------------------------- #
# UTI — merged BY_STATUS rules + priority                                      #
# --------------------------------------------------------------------------- #
def test_uti_asymptomatic_bool():
    assert _run("uti", asymptomatic_bacteriuria=True).output == "ASYMPTOMATIC_BACTERIURIA"


def test_uti_asymptomatic_via_syndrome_class():
    assert _run("uti", syndrome_class="asymptomatic_bacteriuria").output == \
        "ASYMPTOMATIC_BACTERIURIA"


def test_uti_complicated_hosp_nosocomial():
    r = _run("uti", complicated=True, patient_status="hospitalized",
             nosocomial_risk=True)
    assert r.output == "COMPLICATED_HOSPITALIZED_NOSOCOMIAL_RISK"
    assert r.items == ["ertapenem"]


def test_uti_complicated_hosp_via_syndrome_class():
    r = _run("uti", syndrome_class="complicated_uti", patient_status="hospitalized")
    assert r.output == "COMPLICATED_HOSPITALIZED"
    assert r.items == ["ceftriaxone"]


def test_uti_complicated_dischargeable():
    r = _run("uti", complicated=True, patient_status="dischargeable")
    assert r.output == "COMPLICATED_DISCHARGEABLE"
    assert r.items == ["cefuroxime"]


def test_uti_nosocomial_beats_plain_hospitalized_priority():
    # complicated+hospitalized+nosocomial: the nosocomial rung (higher) wins.
    r = _run("uti", complicated=True, patient_status="hospitalized",
             nosocomial_risk=True)
    assert r.output != "COMPLICATED_HOSPITALIZED"


def test_uti_catheter():
    assert _run("uti", catheter_associated=True).output == \
        "CATHETER_ASSOCIATED_UTI_DIAGNOSTICS"


def test_uti_uncomplicated():
    r = _run("uti", uncomplicated=True)
    assert r.output == "UNCOMPLICATED_UTI"
    assert "fosfomycin" in r.items and "nitrofurantoin" in r.items


def test_uti_asymptomatic_beats_complicated_priority():
    # asymptomatic (rung 0) wins even if complicated/hospitalized also set.
    r = _run("uti", asymptomatic_bacteriuria=True, complicated=True,
             patient_status="hospitalized")
    assert r.output == "ASYMPTOMATIC_BACTERIURIA"


def test_uti_default():
    assert _run("uti").output == "DEFAULT_ANSWER"


# --------------------------------------------------------------------------- #
# SBP + cdiff                                                                  #
# --------------------------------------------------------------------------- #
def test_sbp_always_whole():
    r = _run("sbp")
    assert r.output == "WHOLE_SBP" and r.is_default
    assert "paracentesis" in r.text_en and "ceftriaxone" in r.text_en


def test_cdiff_default_asks_for_section():
    r = _run("cdiff")
    assert r.output == "DEFAULT_ANSWER" and r.is_default
    assert "diagnosis" in r.text_en and "treatment" in r.text_en


def test_cdiff_diagnosis():
    r = _run("cdiff", cdiff_request_type="diagnosis")
    assert r.output == "DIAGNOSIS_CHUNK"
    assert "Bristol" in r.text_en


def test_cdiff_treatment_verbatim_dose():
    r = _run("cdiff", cdiff_request_type="treatment")
    assert r.output == "TREATMENT_CHUNK"
    # the source's own inline dose string is preserved verbatim.
    assert "NG vancomycin 4x125 mg" in r.text_en


# --------------------------------------------------------------------------- #
# Endocarditis — pathogen priority, not-covered, empiric, default             #
# --------------------------------------------------------------------------- #
ENDO = "endocarditis_antibiotics"


def test_endo_culture_negative_not_covered():
    r = _run(ENDO, unsupported_topic="culture_negative")
    assert r.output == "NOT_COVERED_CULTURE_NEGATIVE"
    assert "not covered" in r.text_en


def test_endo_vre_targeted():
    r = _run(ENDO, pathogen_group="vre")
    assert r.output == "ENTERO_VRE"
    assert "daptomycin" in r.text_en


def test_endo_mrsa_pve():
    assert _run(ENDO, resistance_profile="mrsa", valve_context="pve").output == "MRSA_PVE"


def test_endo_mrsa_nve():
    assert _run(ENDO, resistance_profile="mrsa", valve_context="nve").output == "MRSA_NVE"


def test_endo_staph_aureus_unspecified_shows_both():
    r = _run(ENDO, pathogen_group="staphylococcus_aureus")
    assert r.output == "STAPH_AUREUS_MSSA_MRSA"


def test_endo_enterococcus_unspecified_shows_all_three():
    assert _run(ENDO, pathogen_group="enterococcus").output == "ENTEROCOCCUS_ALL_THREE"


def test_endo_strep():
    r = _run(ENDO, pathogen_group="streptococcus")
    assert r.output == "STREPTOCOCCUS_CEFTRIAXONE"
    assert "ceftriaxone 2 g/day IV" in r.text_en


def test_endo_pathogen_beats_empiric_priority():
    # A targeted pathogen output (250) beats empiric NVE (80) even with valve_context.
    r = _run(ENDO, pathogen_group="vre", valve_context="nve")
    assert r.output == "ENTERO_VRE"


def test_endo_empiric_penicillin_allergy():
    r = _run(ENDO, penicillin_allergy=True)
    assert r.output == "EMPIRIC_NVE_LATE_PVE_PENICILLIN_ALLERGY"


def test_endo_empiric_nve():
    assert _run(ENDO, valve_context="nve").output == "EMPIRIC_NVE_LATE_PVE"


def test_endo_pve_timing_unknown_shows_options():
    assert _run(ENDO, valve_context="pve").output == "EMPIRIC_PVE_TIMING_OPTIONS"


def test_endo_early_pve():
    assert _run(ENDO, pve_timing="early").output == "EMPIRIC_EARLY_PVE"


def test_endo_default():
    r = _run(ENDO)
    assert r.output == "DEFAULT_ANSWER"
    assert "starting options" in r.text_en


# --------------------------------------------------------------------------- #
# Intra-abdominal infections                                                   #
# --------------------------------------------------------------------------- #
IAI = "intraabdominal_infections"


@pytest.mark.parametrize("ctx,expected", [
    ("cdiff", "CDIFF"),
    ("sbp", "SBP"),
    ("splenectomy_prophylaxis", "SPLENECTOMY_PROPHYLAXIS"),
    ("varix_bleeding_prophylaxis", "VARIX_BLEEDING_PROPHYLAXIS"),
    ("pancreatitis", "PANCREATITIS"),
    ("complex_nosocomial", "COMPLEX_NOSOCOMIAL"),
    ("hospitalized_source_control", "HOSPITALIZED_SOURCE_CONTROL"),
    ("dischargeable", "DISCHARGEABLE"),
])
def test_iai_contexts(ctx, expected):
    assert _run(IAI, iai_context=ctx).output == expected


def test_iai_complex_nosocomial_meropenem():
    r = _run(IAI, iai_context="complex_nosocomial")
    assert "meropenem" in r.items


def test_iai_default():
    assert _run(IAI).output == "DEFAULT_ANSWER"


# --------------------------------------------------------------------------- #
# Cross-cutting: undeclared slots ignored; verbatim fidelity; guard safety     #
# --------------------------------------------------------------------------- #
def test_undeclared_slots_ignored():
    # passing slots a pathway doesn't declare must not change the result/raise.
    r = _run("cap", intubated=True, gfr=40, vancomycin_level=12, nonsense=True)
    assert r.output == "INTUBATED_CAP"


def test_inputs_echo_only_declared_slots():
    r = _run("cdiff", cdiff_request_type="treatment", gfr=99)
    assert set(r.inputs) == {"cdiff_request_type"}   # gfr is not a cdiff slot


def test_verbatim_text_not_composed():
    # the returned text is exactly the protocol string (newline-joined block).
    rec = sp.load_pathway("uti", protocols_dir=PD)
    r = _run("uti", uncomplicated=True)
    assert r.text_en == rec["outputs"]["UNCOMPLICATED_UTI"]["text_en"]


def test_render_includes_output_and_footer():
    r = _run("sbp")
    out = sp.render_pathway(r)
    assert "WHOLE_SBP" in out and "ceftriaxone" in out
    assert "ceftriaxone dozis kulon protokollbol" in out  # footer


def test_guard_with_unknown_slot_raises():
    # a hand-built record whose guard references a slot it never declares.
    bad = {
        "id": "bad_pw", "kind": "pathway",
        "slots": {"a": {"type": "bool"}},
        "outputs": {"X": {"text_en": "x"}, "DEFAULT_ANSWER": {"text_en": "d"}},
        "select": [{"if": "undeclared_slot", "output": "X"},
                   {"default": "DEFAULT_ANSWER"}],
    }
    with pytest.raises(GuardError):
        sp.select_pathway("bad_pw", record=bad)


def test_select_targets_unknown_output_raises():
    bad = {
        "id": "bad_pw2", "kind": "pathway",
        "slots": {"a": {"type": "bool"}},
        "outputs": {"DEFAULT_ANSWER": {"text_en": "d"}},
        "select": [{"if": "a", "output": "NOPE"},
                   {"default": "DEFAULT_ANSWER"}],
    }
    with pytest.raises(sp.PathwayError):
        sp.select_pathway("bad_pw2", record=bad, a=True)
