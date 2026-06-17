#!/usr/bin/env python3
"""Unit tests for answer_from_section.py — the prose-selection tool.

The tool SELECTS one verbatim section (or the verbatim default/ask text); it
never composes. These tests pin: section selection, the whole-guide
default_section path, the topic-less ask, the unknown-section no-match path,
verbatim fidelity, language fallback, and load/kind errors — plus the real
migrated protocols (dantrolene_mh, periop_steroids, periop_gyogyszerek).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PKG / "tools"))
sys.path.insert(0, str(_PKG / "protocols"))

import answer_from_section as afs  # noqa: E402
from answer_from_section import (  # noqa: E402
    answer_from_section, render_prose, load_prose, ProseError,
)

_PROTOCOLS_DIR = str(_PKG / "protocols")


# --------------------------------------------------------------------------- #
# In-memory records (offline, no file IO)                                      #
# --------------------------------------------------------------------------- #
def _multi_record() -> dict:
    return {
        "id": "periop_meds", "kind": "prose",
        "source_label": "Periop meds",
        "canonical_name": "perioperative medication management",
        "default_answer": "Which medication are you asking about perioperatively?",
        "sections": {
            "aspirin": {"text_en": "Usually no need to omit.\nOmit only if high risk.",
                        "aliases": ["aspirin", "asa"]},
            "warfarin": {"text_en": "Omit/adjust until INR <1.5.",
                         "aliases": ["warfarin", "marfarin"]},
        },
    }


def _single_record() -> dict:
    return {
        "id": "guide", "kind": "prose",
        "source_label": "Guide",
        "default_section": "whole",
        "footer": "Ask a follow-up if needed.",
        "sections": {
            "whole": {"text_hu": "Teljes magyar útmutató.",
                      "text_en": "Full English guide."},
        },
    }


# --------------------------------------------------------------------------- #
# Section selection                                                            #
# --------------------------------------------------------------------------- #
def test_named_section_returned_verbatim():
    res = answer_from_section("periop_meds", section="aspirin",
                              record=_multi_record())
    assert res.section == "aspirin"
    assert res.text_en == "Usually no need to omit.\nOmit only if high risk."
    assert res.is_default is False
    assert res.needs_input is False
    assert res.route == "prose"
    assert res.tool == "answer_from_section"


def test_named_section_other_entry():
    res = answer_from_section("periop_meds", section="warfarin",
                              record=_multi_record())
    assert res.section == "warfarin"
    assert "INR <1.5" in res.text_en


def test_no_section_multi_returns_default_ask():
    res = answer_from_section("periop_meds", record=_multi_record())
    assert res.section == ""
    assert res.is_default is True
    assert res.needs_input is True
    assert res.text_en == "Which medication are you asking about perioperatively?"


def test_unknown_section_returns_ask_not_invention():
    res = answer_from_section("periop_meds", section="heparin",
                              record=_multi_record())
    # heparin is not a section here → the verbatim ask, NOT a guessed entry.
    assert res.section == ""
    assert res.needs_input is True
    assert res.is_default is True
    assert "Which medication" in res.text_en


def test_unknown_section_prefers_no_match_answer():
    rec = _multi_record()
    rec["no_match_answer"] = "Not specified in the uploaded perioperative protocol."
    res = answer_from_section("periop_meds", section="ozempic", record=rec)
    assert res.text_en == "Not specified in the uploaded perioperative protocol."
    assert res.needs_input is True


# --------------------------------------------------------------------------- #
# default_section (whole-guide) path                                           #
# --------------------------------------------------------------------------- #
def test_default_section_returns_whole_guide():
    res = answer_from_section("guide", record=_single_record())
    assert res.section == "whole"
    assert res.is_default is False        # a real answer, not an ask
    assert res.needs_input is False
    assert res.text_en == "Full English guide."


def test_default_section_ignores_no_section_arg_even_for_single():
    # Explicitly naming the single section still works.
    res = answer_from_section("guide", section="whole", record=_single_record())
    assert res.section == "whole"
    assert res.needs_input is False


def test_bad_default_section_raises():
    rec = _single_record()
    rec["default_section"] = "ghost"
    with pytest.raises(ProseError):
        answer_from_section("guide", record=rec)


# --------------------------------------------------------------------------- #
# Rendering + language fallback                                                #
# --------------------------------------------------------------------------- #
def test_render_prefers_english():
    res = answer_from_section("guide", record=_single_record())
    out = render_prose(res)
    assert "Full English guide." in out
    assert "Guide" in out                 # source label header
    assert "Ask a follow-up if needed." in out  # footer


def test_render_falls_back_to_hungarian():
    rec = {
        "id": "hu_only", "kind": "prose", "source_label": "HU",
        "default_section": "x",
        "sections": {"x": {"text_hu": "Csak magyar."}},
    }
    res = answer_from_section("hu_only", record=rec)
    assert res.text_en == ""
    out = render_prose(res)
    assert "Csak magyar." in out


def test_bare_text_surfaced_through_english():
    rec = {
        "id": "bare", "kind": "prose", "source_label": "B",
        "default_section": "x",
        "sections": {"x": {"text": "Bare block."}},
    }
    res = answer_from_section("bare", record=rec)
    assert res.text_en == "Bare block."


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #
def test_wrong_kind_raises():
    with pytest.raises(ProseError):
        answer_from_section("x", record={"id": "x", "kind": "drug_dose"})


def test_missing_file_raises():
    with pytest.raises(ProseError):
        load_prose("does_not_exist_xyz", protocols_dir=_PROTOCOLS_DIR)


def test_empty_sections_raises():
    with pytest.raises(ProseError):
        answer_from_section("x", record={"id": "x", "kind": "prose",
                                         "default_answer": "ask",
                                         "sections": {}})


# --------------------------------------------------------------------------- #
# Real migrated protocols (load from disk)                                     #
# --------------------------------------------------------------------------- #
def test_dantrolene_returns_full_guideline_verbatim():
    res = answer_from_section("dantrolene_mh", protocols_dir=_PROTOCOLS_DIR)
    assert res.needs_input is False       # the guide is always shown whole
    body = res.text_hu or res.text_en
    # source-verbatim anchors (Dantrium + Agilus content, stability rules)
    assert "Dantrium" in body
    assert "Agilus" in body
    assert "2,5 mg/ttkg" in body
    assert "NEM HŰTHETŐ" in body


def test_dantrolene_with_weight_still_returns_whole_guide():
    # The source FORBIDS computing new ampoule counts; a body weight does not
    # change the answer — the full guide is still returned.
    res = answer_from_section("dantrolene_mh", protocols_dir=_PROTOCOLS_DIR)
    assert res.section  # a concrete section (the whole guideline), not an ask
    assert res.is_default is False


def test_periop_steroids_returns_table_verbatim():
    res = answer_from_section("periop_steroids", protocols_dir=_PROTOCOLS_DIR)
    body = res.text_en or res.text_hu
    assert "methylprednisone < 8 mg" in body
    assert "hydrocortisone" in body.lower()
    assert res.needs_input is False


def test_periop_meds_no_drug_asks():
    res = answer_from_section("periop_gyogyszerek", protocols_dir=_PROTOCOLS_DIR)
    assert res.needs_input is True
    assert "medication" in res.text_en.lower()


def test_periop_meds_aspirin_entry_complete():
    res = answer_from_section("periop_gyogyszerek", section="aspirin",
                              protocols_dir=_PROTOCOLS_DIR)
    assert res.needs_input is False
    # antithrombotic entries must come back complete (both timing options)
    assert "no need to omit" in res.text_en.lower()
    assert "high bleeding risk" in res.text_en.lower()


def test_periop_meds_dabigatran_entry_has_renal_split():
    res = answer_from_section("periop_gyogyszerek", section="dabigatran",
                              protocols_dir=_PROTOCOLS_DIR)
    body = res.text_en
    assert "GFR" in body
    assert "Epidural catheter: forbidden" in body


def test_periop_meds_unknown_drug_does_not_invent():
    res = answer_from_section("periop_gyogyszerek", section="amoxicillin",
                              protocols_dir=_PROTOCOLS_DIR)
    assert res.needs_input is True
    assert res.section == ""
