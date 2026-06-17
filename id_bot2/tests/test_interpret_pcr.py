"""Unit tests for interpret_pcr (Plan D, roadmap 3.2) — the pcr_panel tool.

Covers the required-input gate, single-organism verbatim selection, the four
resistance-marker rules, bare-genus disambiguation (F5), panel listing (F6),
on/off-panel membership (F7/F8), polymicrobial escalation, and verbatim fidelity.
Offline — loads the two migrated panels from the package protocols dir.
"""
import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent          # id_bot2/
sys.path.insert(0, str(_PKG / "tools"))
sys.path.insert(0, str(_PKG / "protocols"))

import interpret_pcr as ip  # noqa: E402

PD = str(_PKG / "protocols")
JI = "biofire_joint_infection"
PN = "biofire_pneumonia"


def _run(panel, **kw):
    return ip.interpret_pcr(panel, protocols_dir=PD, **kw)


# --------------------------------------------------------------------------- #
# Required-input gate                                                          #
# --------------------------------------------------------------------------- #
def test_no_organism_returns_default_answer():
    r = _run(JI)
    assert r.mode == "needs_input" and r.needs_input
    assert "at least one detected pathogen" in r.message
    assert not r.items                       # never a therapy


def test_marker_without_pathogen_asks():
    r = _run(JI, markers=["mecA"])
    assert r.mode == "clarify" and r.needs_clarification
    assert r.clarify_reason == "marker_without_pathogen"
    assert "Which pathogen was positive" in r.message
    assert not r.items


def test_no_input_emits_no_dose_and_no_therapy():
    r = _run(PN)
    assert r.selected_therapies == []


# --------------------------------------------------------------------------- #
# Single-organism verbatim selection                                          #
# --------------------------------------------------------------------------- #
def test_single_gram_negative_tier1_ceftriaxone():
    r = _run(JI, organisms=["Klebsiella oxytoca"])
    assert r.mode == "interpret"
    assert len(r.items) == 1
    assert r.items[0].organism == "Klebsiella oxytoca"
    assert r.items[0].answer == "Tier 1 - ceftriaxone."


def test_single_organism_alias_resolves():
    # GBS is an alias of Streptococcus agalactiae
    r = _run(JI, organisms=["GBS"])
    assert r.items[0].organism == "Streptococcus agalactiae"
    assert "ceftriaxone" in r.items[0].answer


def test_pneumococcus_alias_pneumonia_panel():
    r = _run(PN, organisms=["pneumococcus"])
    assert r.items[0].organism == "Streptococcus pneumoniae"
    assert r.items[0].answer == "Tier 1 - ceftriaxone."


def test_influenza_is_on_pneumonia_panel_not_dropped():
    # F7: influenza must be recognised; never "not on panel".
    r = _run(PN, organisms=["influenza"])
    assert r.mode == "interpret"
    assert r.items[0].organism == "Influenza A/B"
    assert "oseltamivir" in r.items[0].answer
    assert not r.not_on_panel
    assert "not on" not in ip.render_pcr(r).lower()


def test_staph_aureus_mssa_baseline():
    r = _run(PN, organisms=["Staphylococcus aureus"])
    assert "MSSA likely - cefazolin" in r.items[0].answer
    assert r.items[0].via_marker is None


def test_strep_pyogenes_verbatim_pneumonia():
    r = _run(PN, organisms=["Strep pyogenes"])
    assert r.items[0].answer == \
        "Streptococcus pyogenes - penicillin + clindamycin for toxin suppression."


def test_atypical_pathogen_orthogonal_output():
    r = _run(PN, organisms=["Mycoplasma"])
    assert "clarithromycin" in r.items[0].answer
    assert r.items[0].tier is None           # atypicals carry no numeric tier


# --------------------------------------------------------------------------- #
# Resistance-marker rules                                                      #
# --------------------------------------------------------------------------- #
def test_marker_mrsa_replaces_staph_therapy():
    r = _run(PN, organisms=["Staphylococcus aureus"], markers=["mecA/C"])
    assert r.items[0].via_marker == "mrsa"
    assert "MRSA likely - vancomycin" in r.items[0].answer


def test_marker_mrsa_does_not_touch_non_staph():
    r = _run(PN, organisms=["E. coli"], markers=["MREJ"])
    assert r.items[0].via_marker is None     # mecA/MREJ only changes Staph aureus
    assert r.items[0].answer == "Tier 1 - ceftriaxone."


def test_ctx_m_escalates_enterobacterales_pneumonia_to_ertapenem():
    r = _run(PN, organisms=["E. coli"], markers=["CTX-M"])
    assert r.items[0].via_marker == "ctx_m"
    assert r.items[0].answer == "Tier 3 - ertapenem."


def test_ctx_m_escalates_enterobacterales_joint_to_meropenem():
    # Panel-specific: JI uses meropenem where PN uses ertapenem.
    r = _run(JI, organisms=["E. coli"], markers=["CTX-M"])
    assert r.items[0].via_marker == "ctx_m"
    assert r.items[0].answer == "Tier 3 - meropenem."


def test_ctx_m_not_applied_to_non_enterobacterales():
    # Source rule CTX_M_WITH_NON_ENTEROBACTERALES_ONLY: do NOT upgrade S. pneumoniae.
    r = _run(PN, organisms=["Streptococcus pneumoniae"], markers=["CTX-M"])
    assert r.items[0].via_marker is None
    assert r.items[0].answer == "Tier 1 - ceftriaxone."


def test_carbapenemase_escalates_gram_negative_to_tier4():
    r = _run(JI, organisms=["Klebsiella pneumoniae group"], markers=["KPC"])
    assert r.items[0].via_marker == "carbapenemase"
    assert "meropenem + colistin" in r.items[0].answer
    assert r.items[0].tier == 4


def test_vre_marker_on_faecalis_uses_marker_answer():
    r = _run(JI, organisms=["Enterococcus faecalis"], markers=["VanA"])
    assert r.items[0].via_marker == "vre"
    assert "linezolid" in r.items[0].answer


def test_esbl_alias_maps_to_ctx_m():
    r = _run(PN, organisms=["Klebsiella oxytoca"], markers=["ESBL"])
    assert r.items[0].via_marker == "ctx_m"


# --------------------------------------------------------------------------- #
# Disambiguation / membership                                                 #
# --------------------------------------------------------------------------- #
def test_bare_genus_klebsiella_disambiguates():
    # F5: never silently pick a species.
    r = _run(JI, organisms=["Klebsiella"])
    assert r.mode == "clarify" and r.needs_clarification
    assert r.clarify_reason == "ambiguous_genus"
    assert "oxytoca" in r.message and "pneumoniae" in r.message
    assert not r.items                        # organism not dropped into a pick


def test_off_panel_organism_reported_explicitly():
    # F8: an organism genuinely not on the panel -> say so, never fabricate.
    r = _run(JI, organisms=["Stenotrophomonas maltophilia"])
    assert r.mode == "clarify"
    assert r.clarify_reason == "not_on_panel"
    assert "not on" in r.message.lower()
    assert not r.items


def test_mixed_known_and_unknown_keeps_known_flags_unknown():
    r = _run(JI, organisms=["Klebsiella oxytoca", "Stenotrophomonas"])
    assert r.mode == "interpret"
    assert [it.organism for it in r.items] == ["Klebsiella oxytoca"]
    assert r.not_on_panel == ["Stenotrophomonas"]


# --------------------------------------------------------------------------- #
# Panel listing (F6)                                                          #
# --------------------------------------------------------------------------- #
def test_list_panel_returns_contents_not_recommendation():
    r = ip.list_panel(JI, protocols_dir=PD)
    assert r.tool == "list_panel" and r.mode == "list"
    names = r.panel_organisms
    assert "Staphylococcus aureus" in names
    assert any("Klebsiella" in n for n in names)
    assert len(names) == 29


def test_list_panel_pneumonia_count():
    r = ip.list_panel(PN, protocols_dir=PD)
    assert len(r.panel_organisms) == 20
    assert "Influenza A/B" in r.panel_organisms


# --------------------------------------------------------------------------- #
# Polymicrobial escalation                                                    #
# --------------------------------------------------------------------------- #
def test_polymicrobial_selects_highest_spectrum_tier():
    # S. pneumoniae (tier 1) + Pseudomonas (tier 2) -> escalate to cefepime.
    r = _run(PN, organisms=["Streptococcus pneumoniae", "Pseudomonas aeruginosa"])
    assert len(r.items) == 2
    assert r.escalation is not None
    assert r.escalation.tier == 2
    assert "cefepime" in r.escalation.answer


def test_polymicrobial_conflict_pseudomonas_plus_ctxm():
    # Pseudomonas (tier 2, antipseudomonal) + CTX-M Enterobacterales (->ertapenem)
    # = conflicting requirements -> ID consultation (documented source example).
    r = _run(PN, organisms=["Pseudomonas aeruginosa", "E. coli"], markers=["CTX-M"])
    assert r.conflict
    assert "ID consultation" in r.message


def test_single_organism_no_escalation():
    r = _run(PN, organisms=["E. coli"])
    assert r.escalation is None


# --------------------------------------------------------------------------- #
# Verbatim fidelity / rendering / errors                                      #
# --------------------------------------------------------------------------- #
def test_render_interpret_contains_verbatim_answer_and_footer():
    r = _run(JI, organisms=["Klebsiella oxytoca"])
    text = ip.render_pcr(r)
    assert "Tier 1 - ceftriaxone." in text
    assert "nosocomial intra-abdominal" in text     # footer present


def test_render_clarify_is_just_the_message():
    r = _run(JI, organisms=["Klebsiella"])
    text = ip.render_pcr(r)
    assert "Which species was detected?" in text


def test_load_wrong_kind_raises():
    with pytest.raises(ip.PcrError):
        ip.interpret_pcr("meropenem", organisms=["x"], protocols_dir=PD)


def test_load_missing_panel_raises():
    with pytest.raises(ip.PcrError):
        ip.interpret_pcr("no_such_panel", organisms=["x"], protocols_dir=PD)


def test_panel_emits_no_dose_numbers():
    # The panel selects agents; it must never carry a mg/g dose.
    r = _run(PN, organisms=["E. coli"], markers=["CTX-M"])
    text = ip.render_pcr(r)
    assert "mg" not in text and "g/day" not in text
