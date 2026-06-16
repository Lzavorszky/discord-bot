"""Unit tests for the F12 input-normalisation helper (Phase 0.4)."""
import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import textnorm  # noqa: E402


def test_repair_cp437_box_drawing():
    # The exact F12 symptom: 'dózisa' shown as 'd├│zisa'.
    corrupted = "dózisa".encode("utf-8").decode("cp437")
    assert corrupted == "d├│zisa"
    assert textnorm.repair_mojibake(corrupted) == "dózisa"


def test_repair_cp1252_double_decode():
    corrupted = "dózisa".encode("utf-8").decode("cp1252")  # 'dÃ³zisa'
    assert textnorm.repair_mojibake(corrupted) == "dózisa"


def test_repair_full_phrase():
    src = "meropenem dózisa"
    corrupted = src.encode("utf-8").decode("cp437")
    assert textnorm.repair_mojibake(corrupted) == src


def test_repair_is_noop_on_clean_text():
    for clean in ["meropenem dose", "dózis", "Árvíztűrő tükörfúrógép", "JiPCR Klebsiella"]:
        assert textnorm.repair_mojibake(clean) == clean


def test_repair_idempotent():
    corrupted = "vízhajtó dózisa".encode("utf-8").decode("cp437")
    once = textnorm.repair_mojibake(corrupted)
    assert textnorm.repair_mojibake(once) == once


def test_fold_accents_hungarian():
    assert textnorm.fold_accents("dózis") == "dozis"
    assert textnorm.fold_accents("Árvíztűrő") == "arvizturo"
    assert textnorm.fold_accents("MEROPENEM") == "meropenem"
    # folding is stable
    assert textnorm.fold_accents(textnorm.fold_accents("Őrült")) == "orult"


def test_normalize_input_collapses_ws_and_strips():
    assert textnorm.normalize_input("  Meropenem   dózisa? ") == "Meropenem dózisa?"


def test_normalize_input_handles_none_and_controls():
    assert textnorm.normalize_input(None) == ""
    assert textnorm.normalize_input("a\x00b\x07c") == "abc"


def test_normalize_input_output_is_nfc():
    out = textnorm.normalize_input("dózis")
    assert out == unicodedata.normalize("NFC", out)
