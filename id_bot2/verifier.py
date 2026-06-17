#!/usr/bin/env python3
"""verifier.py — the grounding verifier (Plan D, roadmap 4.1 / PROGRESS "Phase 5").

The last safety gate before a phrased answer reaches the user. The pipeline is

    router -> get_dose (verbatim) -> phrasing model -> **verifier** -> user

`get_dose` only ever returns approved protocol text, so its output is *ground
truth*. The phrasing model rewrites that text into natural HU/EN prose. The
verifier's single job is to prove the phrasing did not introduce any clinical
fact — a dose number, a unit, or a different drug name — that is **not present in
the tool output**. (Fixes F10 "answered when nothing was covered" and F11
"gross hallucinations".)

One function, a per-kind mode (roadmap 4.1)
-------------------------------------------
`verify_grounding(candidate, grounded, kind, ...)` picks its behaviour from the
protocol `kind`, so the mix adds no branching logic at the call site:

* **hard-block** (`drug_dose`, `pcr_panel`): a dose/number is safety-critical.
  If the phrasing asserts any ungrounded numeric/unit span or names a drug that
  is not in the tool output, the phrasing is **rejected wholesale** and the
  verbatim tool output is returned instead. (Stripping a single number out of a
  sentence would leave a mangled, still-unsafe dose; falling back to the
  approved text is the safe reading of "strip + log".) Every violation is logged.
* **soft-flag** (`pathway`, `prose`, default): faithful paraphrase must survive,
  so nothing is stripped. Violations are logged/flagged for escalation only and
  the candidate text is returned unchanged.

What counts as a grounded "numeric/unit span" (roadmap 4.1b)
------------------------------------------------------------
The verifier compares *numbers* and, where the source constrains it, the *unit*
attached to a number — the two ways a dose goes wrong:

  1. **Hallucinated number** — a number in the phrasing that is absent from the
     tool output (e.g. the model invents "2 g" when the source says "4 g/day").
  2. **Right number, wrong unit** — the number exists in the source but with a
     different base unit (e.g. "4 mg" when the source says "4 g"). Only flagged
     when the source actually pins a unit to that number, so a legitimately
     unit-light paraphrase is not punished.

Units are compared on their **base** component (the part before any "/", with
common synonyms folded: microgram/µg → mcg, millilitre → ml, gram → g, …), so
"8.3 mL/h" in the source grounds "8.3 mL per hour" in the phrasing, while
"4 g/day" does NOT ground "4 mg". Pure formatting (case, accents, separators,
trailing ".0") is normalised away on both sides to avoid false strips.

Public API
----------
    verify_grounding(candidate, grounded, kind, *, known_drugs=(), logger=None)
        -> VerifierResult
    mode_for_kind(kind) -> "hard" | "soft"
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Reuse the project's accent-folding / mojibake-repair so the verifier sees the
# same normalised text the rest of the pipeline does.
import sys as _sys
_HERE = Path(__file__).resolve().parent
_sys.path.insert(0, str(_HERE))
from textnorm import repair_mojibake, fold_accents  # noqa: E402

_LOG = logging.getLogger("id_bot2.verifier")

# Per-kind mode table. drug_dose/pcr_panel carry safety-critical facts → hard.
# Everything else (pathway prose, prose sections) tolerates paraphrase → soft.
VERIFIER_MODES: dict[str, str] = {
    "drug_dose": "hard",
    "pcr_panel": "hard",
    "pathway": "soft",
    "prose": "soft",
}
DEFAULT_MODE = "soft"


def mode_for_kind(kind: Optional[str]) -> str:
    return VERIFIER_MODES.get(kind or "", DEFAULT_MODE)


# --------------------------------------------------------------------------- #
# Result type                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class VerifierResult:
    """The verifier's verdict.

    ok           True iff no grounding violations were found.
    mode         "hard" | "soft" (derived from kind).
    text         the SAFE text to emit: the verbatim `grounded` text when a
                 hard-mode check blocked the candidate; otherwise the candidate.
    blocked      True iff hard mode rejected the candidate (text == grounded).
    violations   human-readable list of what was ungrounded (always logged).
    flagged      True iff soft mode found violations (logged, not stripped).
    """
    ok: bool
    mode: str
    text: str
    blocked: bool = False
    violations: list[str] = field(default_factory=list)
    flagged: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "mode": self.mode, "blocked": self.blocked,
            "flagged": self.flagged, "violations": list(self.violations),
        }


# --------------------------------------------------------------------------- #
# Normalisation + extraction                                                  #
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    """Fold accents/case, repair mojibake, collapse runs of whitespace. Keeps
    digits, '.', '/', '%' so numbers and compound units survive."""
    t = fold_accents(repair_mojibake(text or "")).lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t


# A number: integer or decimal. (Thousands separators are not used in these
# protocols; a bare comma is treated as punctuation, not a decimal point.)
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

# Unit-synonym folding → a canonical base token. Order doesn't matter; we map the
# leading component of a unit (before any '/') through this table.
_UNIT_SYNONYMS = {
    "microgram": "mcg", "micrograms": "mcg", "ug": "mcg", "mcg": "mcg", "µg": "mcg",
    "milligram": "mg", "milligrams": "mg", "mg": "mg",
    "gram": "g", "grams": "g", "gm": "g", "g": "g",
    "kilogram": "kg", "kilograms": "kg", "kg": "kg",
    "millilitre": "ml", "millilitres": "ml", "milliliter": "ml", "milliliters": "ml", "ml": "ml",
    "litre": "l", "litres": "l", "liter": "l", "liters": "l", "l": "l",
    "unit": "unit", "units": "unit", "iu": "unit", "u": "unit",
    "hour": "h", "hours": "h", "hr": "h", "hrs": "h", "h": "h", "hourly": "h",
    "minute": "min", "minutes": "min", "min": "min", "mins": "min",
    "day": "day", "days": "day", "daily": "day", "od": "day",
    "%": "%",
}
# Tokens that may legitimately follow a number AS a unit. Anything else after a
# number is treated as ordinary prose (the number is then "bare").
_UNIT_TOKEN_RE = re.compile(
    r"[a-zµ%]+(?:\s*/\s*[a-z%]+)*",  # e.g. g, mg, ml/h, g/day, mg/kg
)


def _canon_unit(raw: str) -> Optional[str]:
    """Canonicalise a unit token to its base component, or None if it is not a
    recognised unit (so the number is treated as bare)."""
    if not raw:
        return None
    base = raw.split("/")[0].strip().rstrip(".")
    return _UNIT_SYNONYMS.get(base)


def _canon_num(raw: str) -> str:
    """4.0 -> '4', 8.30 -> '8.3' so formatting never causes a false mismatch."""
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw or "0"


def _claims(text: str) -> tuple[set[str], dict[str, set[str]]]:
    """Extract grounded-fact claims from normalised text.

    Returns:
      numbers       set of canonical number strings present.
      units_by_num  number -> set of canonical base units attached to it.
    """
    norm = _norm(text)
    numbers: set[str] = set()
    units_by_num: dict[str, set[str]] = {}
    for m in _NUM_RE.finditer(norm):
        num = _canon_num(m.group(0))
        numbers.add(num)
        # Look just past the number for an immediate unit token.
        tail = norm[m.end():]
        tail = tail[: 16]  # only the immediate neighbourhood
        um = _UNIT_TOKEN_RE.match(tail.lstrip())
        # require the unit to be adjacent (allow a single space)
        if um and (tail[:1] == " " or tail[:1] == "" or tail[:1] in "%"):
            unit = _canon_unit(um.group(0).replace(" ", ""))
            if unit:
                units_by_num.setdefault(num, set()).add(unit)
    return numbers, units_by_num


# --------------------------------------------------------------------------- #
# The verifier                                                                #
# --------------------------------------------------------------------------- #
def verify_grounding(
    candidate: str,
    grounded: str,
    kind: Optional[str],
    *,
    known_drugs: Iterable[str] = (),
    logger: Optional[logging.Logger] = None,
) -> VerifierResult:
    """Check that `candidate` (phrased answer) asserts no clinical fact absent
    from `grounded` (the verbatim tool output). See module docstring."""
    log = logger or _LOG
    mode = mode_for_kind(kind)

    g_numbers, g_units = _claims(grounded)
    c_numbers, c_units = _claims(candidate)
    g_norm = _norm(grounded)
    c_norm = _norm(candidate)

    violations: list[str] = []

    # 1) Number grounding: every number the candidate states must exist in source.
    for num in sorted(c_numbers):
        if num not in g_numbers:
            violations.append(f"ungrounded number {num!r} (not in tool output)")
            continue
        # 2) Unit grounding: if the source pins unit(s) to this number and the
        #    candidate gives a unit, the base units must overlap.
        c_u = c_units.get(num)
        g_u = g_units.get(num)
        if c_u and g_u and not (c_u & g_u):
            violations.append(
                f"number {num!r} has unit {sorted(c_u)} "
                f"but tool output uses {sorted(g_u)}")

    # 3) Drug-name grounding: a phrasing must not name a drug absent from source.
    #    `known_drugs` is the router's full alias vocabulary; only flag an alias
    #    that actually appears in the candidate and is missing from the source.
    for alias in sorted({_norm(a) for a in known_drugs if a and a.strip()},
                        key=len, reverse=True):
        if _whole(alias, c_norm) and not _whole(alias, g_norm):
            violations.append(f"names drug {alias!r} not in tool output")

    if not violations:
        return VerifierResult(ok=True, mode=mode, text=candidate)

    if mode == "hard":
        for v in violations:
            log.warning("grounding BLOCK (%s): %s", kind, v)
        return VerifierResult(ok=False, mode=mode, text=grounded,
                              blocked=True, violations=violations)
    # soft: keep candidate, flag only.
    for v in violations:
        log.info("grounding flag (%s): %s", kind, v)
    return VerifierResult(ok=False, mode=mode, text=candidate,
                          flagged=True, violations=violations)


def _whole(needle: str, haystack: str) -> bool:
    """Whole-token containment on already-normalised strings."""
    if not needle:
        return False
    return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack) is not None


__all__ = ["verify_grounding", "mode_for_kind", "VerifierResult", "VERIFIER_MODES"]
