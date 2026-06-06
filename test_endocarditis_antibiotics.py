from protocol_parser import parse_protocol_file
from selection_engine import extract_slots_from_query, run_selection, render_selected_output


def _select(question):
    parsed = parse_protocol_file("protocols/endocarditis_antibiotics.txt")
    slots = extract_slots_from_query(question, parsed)
    result = run_selection(parsed, slots)
    return parsed, slots, result, render_selected_output(parsed, result, lang="en")


def test_endocarditis_default_shows_empiric_options():
    _, slots, result, rendered = _select("endocarditis antibiotics")

    assert slots == {}
    assert result.default_used is True
    assert "NVE / late PVE" in rendered
    assert "Early PVE" in rendered


def test_endocarditis_pve_without_timing_shows_both_options():
    _, slots, result, rendered = _select("PVE endocarditis empiric")

    assert slots["valve_context"] == "pve"
    assert result.output_key == "EMPIRIC_PVE_TIMING_OPTIONS"
    assert "Late PVE" in rendered
    assert "Early PVE" in rendered


def test_endocarditis_mssa_mrsa_route_to_exact_valve_rows():
    assert _select("MSSA endocarditis NVE")[2].output_key == "MSSA_NVE"
    assert _select("MSSA endocarditis PVE")[2].output_key == "MSSA_PVE"
    assert _select("MRSA endocarditis NVE")[2].output_key == "MRSA_NVE"
    assert _select("MRSA endocarditis PVE")[2].output_key == "MRSA_PVE"


def test_endocarditis_unspecified_pathogens_show_option_sets():
    assert _select("Staphylococcus aureus endocarditis")[2].output_key == "STAPH_AUREUS_MSSA_MRSA"
    assert _select("Enterococcus endocarditis")[2].output_key == "ENTEROCOCCUS_ALL_THREE"


def test_endocarditis_excluded_topics_do_not_return_regimens():
    assert _select("culture negative endocarditis")[2].output_key == "NOT_COVERED_CULTURE_NEGATIVE"
    assert _select("fungal endocarditis")[2].output_key == "NOT_COVERED_FUNGAL"
    assert _select("OPAT endocarditis")[2].output_key == "NOT_COVERED_OPAT"
