"""textnorm — input-boundary text normalisation (Plan D, Phase 0.4).

Fixes the F12 mojibake class (`d├│zisa`, `dÃ³zisa`) at the input boundary and
provides accent-folding used for tolerant matching downstream. Pure, dependency
-free, and idempotent on already-clean text.

Public API:
    normalize_input(text)  -> str   # the boundary call: repair + NFC + tidy
    repair_mojibake(text)  -> str   # undo UTF-8-misdecoded-as-X corruption
    fold_accents(text)     -> str   # accent/diacritic-insensitive matching key
    nfc(text)              -> str   # Unicode NFC normalisation
"""
from __future__ import annotations
import re
import unicodedata

# Codecs that commonly mangle UTF-8 bytes when a UTF-8 stream is wrongly decoded
# as one of these single-byte encodings. We try to *reverse* each: re-encode the
# corrupted text back to the wrong codec's bytes, then decode those as UTF-8.
_MOJIBAKE_CODECS = ("cp1252", "latin-1", "cp437", "cp850", "cp852")

# Box-drawing glyphs (U+2500-257F) are the CP437/85x tell (`├│`).
_BOX_DRAWING = (0x2500, 0x257F)
# Characters that signal probable mojibake and gate whether we even try a repair.
_MOJIBAKE_SIGNAL = re.compile(r"[─-╿�]|[ÃÂÅ][\x80-\xff-ÿ]")


def nfc(text: str) -> str:
    """Unicode NFC normalisation (compose accents into single code points)."""
    return unicodedata.normalize("NFC", text)


def _suspicion_score(text: str) -> int:
    """Count characters that look like mojibake. Lower is cleaner."""
    score = 0
    for ch in text:
        o = ord(ch)
        if _BOX_DRAWING[0] <= o <= _BOX_DRAWING[1]:
            score += 3
        elif ch == "�":  # replacement character
            score += 3
        elif ch in "ÃÂÅ":
            score += 1
    return score


def repair_mojibake(text: str) -> str:
    """Best-effort undo of UTF-8 wrongly decoded as a single-byte codec.

    Guarded: only attempts repair when the text shows mojibake signals, and only
    accepts a candidate decode if it is strictly *less* suspicious than the
    input. Returns the input unchanged when nothing improves it. Idempotent.
    """
    if not text or not _MOJIBAKE_SIGNAL.search(text):
        return text

    best = text
    best_score = _suspicion_score(text)
    for codec in _MOJIBAKE_CODECS:
        try:
            candidate = text.encode(codec, errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        cand_score = _suspicion_score(candidate)
        if cand_score < best_score:
            best, best_score = candidate, cand_score
    return best


def fold_accents(text: str) -> str:
    """Accent/diacritic-insensitive, case-insensitive matching key.

    Decomposes (NFKD), drops combining marks, lowercases. Hungarian long vowels
    fold to their base letter (ő->o, ű->u, á->a, …) which is what tolerant alias
    matching wants. Not for display — only for comparison.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return no_marks.casefold()


_WS = re.compile(r"\s+")
# Strip C0/C1 control chars except tab/newline/carriage-return.
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def normalize_input(text) -> str:
    """The input-boundary call. Repair mojibake, NFC-normalise, strip control
    characters, and collapse runs of whitespace. Idempotent on clean text."""
    if text is None:
        return ""
    text = repair_mojibake(str(text))
    text = nfc(text)
    text = _CTRL.sub("", text)
    text = _WS.sub(" ", text).strip()
    return text
