#!/usr/bin/env python3
"""get_table_dose.py — the table_lookup dosing tool (Plan D, final migration phase).

The fourth and last dose-emitting tool. It answers a ``table_lookup`` protocol —
today only ``tmpsmx`` (TMP/SMX) — whose dosing is a 2-D lookup keyed by
``{indication_tier}_{renal_category}`` plus a weight band, rather than a single
``select:`` ladder.

Like ``get_dose`` / ``interpret_pcr`` / ``select_pathway``, this tool NEVER
composes a novel clinical recommendation. Every dose figure it surfaces is a
verbatim cell from the protocol's own tables; the only assembly it does is
slotting those verbatim cells into the protocol's own ``output_template_en``
(the source ### FINAL_SELECTED template) — exactly the faithful field-assembly
``render_dose`` already does for ``drug_dose``.

The state machine (mirrors the source SELECTION_RULES)
------------------------------------------------------
1. **Out-of-range numerics → ask, never guess.** A provided ``gfr`` or
   ``body_weight_kg`` outside its declared clinical range with
   ``on_out_of_range: ask_confirmation`` → ``needs_confirmation`` (run nothing).
2. **Required-input gate.** No (or blank) ``indication`` → the verbatim
   ``default_answer`` (show the indication groups, ask). Same when the indication
   text matches no indication rule (can't classify → ask, never guess a tier).
3. **Classify the indication** via the ordered ``indication_rules`` (keyword
   *contains*, source ### INDICATION_RULES) → an ``indication_tier``.
4. **Classify renal function** via the ordered ``renal_rules`` ladder (the SAME
   restricted-AST guard evaluator ``get_dose`` uses) → a ``renal_category``
   (or ``UNKNOWN`` when no renal input was supplied).
5. **Select + render:**
   * **PROPHYLAXIS** — allowed without weight/renal: map the renal category via
     ``prophylaxis_tables`` and return that verbatim block (a renal warning for
     GFR<15 / IHD).
   * **treatment tiers** — a GFR<15 / IHD renal category returns the verbatim
     renal warning; an UNKNOWN renal category or a missing ``body_weight_kg``
     returns the verbatim ``missing_inputs`` ask (never a guessed table or a full
     table dump — ``RESTRICTED_OUTPUTS``); otherwise the ``{tier}_{category}``
     table is selected and, for a ``dosing_table``, the **closest practical
     weight row** is rendered through the template.

Weight banding (source ## weight INFO_BLOCK)
--------------------------------------------
Rows exist at 40..100 kg. Within that supported range the closest row by absolute
weight difference is chosen; an exact tie rounds **up** to the higher-weight row
(higher dose) because the protocol says "avoid underdosing severe infection".
A weight below/above the supported range clamps to the nearest explicit row and
attaches a verbatim review note (">100 kg: individualized dosing decision, ID/
pharmacy consultation recommended").

Public API
----------
    get_table_dose(table_id, *, indication=None, body_weight_kg=None, gfr=None,
                   crrt=False, ihd=False, record=None, protocols_dir=None)
        -> TableDoseResult
    render_table_dose(result) -> str
    load_table_lookup(table_id, *, protocols_dir=None) -> dict
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import sys as _sys
_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_sys.path.insert(0, str(_HERE))                # get_dose (guard evaluator)
_sys.path.insert(0, str(_PKG))                 # textnorm
_sys.path.insert(0, str(_PKG / "protocols"))   # loader
from textnorm import repair_mojibake, fold_accents     # noqa: E402
from loader import load_protocol                        # noqa: E402
# Reuse get_dose's restricted guard evaluator + error type so table_lookup renal
# guards and drug_dose guards can never diverge in what they allow.
from get_dose import _eval_guard, GuardError            # noqa: E402

DEFAULT_PROTOCOLS_DIR = _PKG / "protocols"


class TableDoseError(ValueError):
    """Raised when get_table_dose cannot honour a request (bad id/kind/target)."""


# --------------------------------------------------------------------------- #
# Result type                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class TableDoseResult:
    table_id: str
    source_label: str
    canonical_name: str = ""
    route: str = "table_lookup"
    tool: str = "get_table_dose"
    # mode: dose | needs_input | needs_confirmation | default
    mode: str = "dose"
    output_kind: str = ""              # dosing_row | fixed_dose | prophylaxis | renal_warning | ""
    needs_input: bool = False
    needs_confirmation: bool = False
    confirmation_reason: Optional[str] = None
    indication_tier: Optional[str] = None
    renal_category: Optional[str] = None
    table_name: Optional[str] = None
    selected_weight: Optional[float] = None
    target: Optional[str] = None
    practical_dose: Optional[str] = None
    total: Optional[str] = None
    weight_note: Optional[str] = None
    text: str = ""                     # the verbatim/rendered answer text
    footer: Optional[str] = None
    never: list = field(default_factory=list)
    inputs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "table_id": self.table_id, "source_label": self.source_label,
            "canonical_name": self.canonical_name, "route": self.route,
            "tool": self.tool, "mode": self.mode, "output_kind": self.output_kind,
            "needs_input": self.needs_input,
            "needs_confirmation": self.needs_confirmation,
            "confirmation_reason": self.confirmation_reason,
            "indication_tier": self.indication_tier,
            "renal_category": self.renal_category, "table_name": self.table_name,
            "selected_weight": self.selected_weight, "target": self.target,
            "practical_dose": self.practical_dose, "total": self.total,
            "weight_note": self.weight_note, "text": self.text,
            "footer": self.footer, "never": list(self.never),
            "inputs": dict(self.inputs),
        }


# --------------------------------------------------------------------------- #
# Normalisation (mirrors router._norm / interpret_pcr._norm)                   #
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    t = fold_accents(repair_mojibake(text or "")).lower()
    t = re.sub(r"[/_\-.]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #
def load_table_lookup(table_id: str, *, protocols_dir=None) -> dict:
    base = Path(protocols_dir) if protocols_dir else DEFAULT_PROTOCOLS_DIR
    path = base / f"{table_id}.yaml"
    if not path.exists():
        alt = base / f"{table_id}.yml"
        path = alt if alt.exists() else path
    if not path.exists():
        raise TableDoseError(
            f"no table_lookup protocol file for {table_id!r} in {base}")
    record = load_protocol(path)
    if record.get("kind") != "table_lookup":
        raise TableDoseError(
            f"{table_id!r} is kind {record.get('kind')!r}, not 'table_lookup'")
    return record


# --------------------------------------------------------------------------- #
# Classification helpers                                                       #
# --------------------------------------------------------------------------- #
def _classify_indication(record: dict, indication: str) -> Optional[str]:
    """Walk ``indication_rules`` in list order (priority); first rule any of whose
    folded keywords is a substring of the folded indication text wins. Returns the
    tier name, or None when nothing matches (caller -> default_answer)."""
    folded = _norm(indication)
    if not folded:
        return None
    for rule in record.get("indication_rules") or []:
        for kw in rule.get("contains") or []:
            if _norm(kw) in folded:
                return rule.get("tier")
    return None


def _classify_renal(record: dict, ctx: dict) -> str:
    """Walk the ``renal_rules`` ladder (guards over gfr/crrt/ihd) in order; first
    firing guard wins; the terminal default yields the renal category for an
    unspecified renal status (conventionally 'UNKNOWN')."""
    for i, rule in enumerate(record.get("renal_rules") or []):
        if "default" in rule:
            return rule["default"]
        guard = rule.get("if")
        if guard is None:
            raise GuardError(f"renal_rules[{i}] has no 'if' or 'default'")
        if _eval_guard(guard, ctx):
            return rule.get("category")
    # Validator guarantees a terminal default; reaching here is a bug.
    raise TableDoseError("renal_rules fell through with no default rung")


def _range_problem(slots: dict, name: str, value) -> Optional[str]:
    if value is None:
        return None
    spec = (slots or {}).get(name) or {}
    if spec.get("type") != "number":
        return None
    if spec.get("on_out_of_range") != "ask_confirmation":
        return None
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and value < lo:
        return f"{name}={value} is below the declared minimum {lo}"
    if hi is not None and value > hi:
        return f"{name}={value} is above the declared maximum {hi}"
    return None


def _select_weight_row(rows: list, weight: float,
                       supported_min: float, supported_max: float):
    """Closest practical row by |row.weight - weight|; an exact tie rounds UP to
    the higher-weight row (avoid underdosing). Below/above the supported range
    clamps to the nearest explicit row and returns a review note. Returns
    (row, note)."""
    srows = sorted(rows, key=lambda r: r["weight_kg"])
    lo_row, hi_row = srows[0], srows[-1]
    note = None
    if weight < supported_min:
        return lo_row, (f"body weight {weight} kg is below the table range "
                        f"({int(lo_row['weight_kg'])} kg shown); individualized "
                        f"dosing decision recommended.")
    if weight > supported_max:
        return hi_row, (f"body weight {weight} kg is above the table range; "
                        f">100 kg requires an individualized dosing decision and "
                        f"ID/pharmacy consultation.")
    best = None
    best_d = None
    for r in srows:
        d = abs(r["weight_kg"] - weight)
        # strict < keeps the FIRST (lower) on a non-tie; on an exact tie we want
        # the higher row, so prefer the later (higher) row when d == best_d.
        if best_d is None or d < best_d or (d == best_d and r["weight_kg"] > best["weight_kg"]):
            best, best_d = r, d
    return best, note


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #
def _render_row(record: dict, tier: str, renal_category: str, weight,
                table: dict, row: dict) -> str:
    tmpl = record.get("output_template_en") or ""
    return tmpl.format(
        indication_tier=tier,
        body_weight_kg=("?" if weight is None else _fmt_num(weight)),
        renal_category=renal_category,
        target=table.get("target", row.get("target", "")),
        practical_dose=row.get("practical_dose", ""),
        total_daily_tmp_smx=row.get("total", ""),
    ).rstrip("\n")


def _fmt_num(v) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else str(f)


def _block_text(table: dict) -> str:
    return (table.get("text_en") or table.get("text") or table.get("text_hu")
            or "").rstrip("\n")


# --------------------------------------------------------------------------- #
# The tool                                                                    #
# --------------------------------------------------------------------------- #
def get_table_dose(
    table_id: str,
    *,
    indication: Optional[str] = None,
    body_weight_kg: Optional[float] = None,
    gfr: Optional[float] = None,
    crrt: bool = False,
    ihd: bool = False,
    record: Optional[dict] = None,
    protocols_dir=None,
    **extra,
) -> TableDoseResult:
    if record is None:
        record = load_table_lookup(table_id, protocols_dir=protocols_dir)
    if record.get("kind") != "table_lookup":
        raise TableDoseError(f"{table_id!r} is not a table_lookup protocol")

    tid = record.get("id", table_id)
    slots = record.get("slots") or {}
    ctx = {"gfr": gfr, "crrt": bool(crrt), "ihd": bool(ihd)}
    inputs = {"indication": indication, "body_weight_kg": body_weight_kg,
              "gfr": gfr, "crrt": bool(crrt), "ihd": bool(ihd)}

    base = dict(
        table_id=tid,
        source_label=record.get("source_label") or tid,
        canonical_name=record.get("canonical_name", ""),
        footer=record.get("footer"),
        never=list(record.get("never") or []),
        inputs=inputs,
    )

    def _default(reason_text_key: str = "default_answer") -> TableDoseResult:
        return TableDoseResult(
            mode="default", needs_input=True,
            text=(record.get(reason_text_key) or "").rstrip("\n"), **base)

    def _ask(msg_key: str = "missing_inputs", *, tier=None, renal=None) -> TableDoseResult:
        return TableDoseResult(
            mode="needs_input", needs_input=True,
            indication_tier=tier, renal_category=renal,
            text=(record.get(msg_key) or record.get("default_answer") or "").rstrip("\n"),
            **base)

    # 1) out-of-range numerics → ask, never guess
    for sname, val in (("body_weight_kg", body_weight_kg), ("gfr", gfr)):
        reason = _range_problem(slots, sname, val)
        if reason:
            return TableDoseResult(mode="needs_confirmation",
                                   needs_confirmation=True,
                                   confirmation_reason=reason, **base)

    # 2) required-input gate: indication
    if not indication or not str(indication).strip():
        return _default()

    # 3) classify indication
    tier = _classify_indication(record, indication)
    if tier is None:
        return _default()   # can't classify → ask, never guess a tier

    # 4) classify renal
    renal_category = _classify_renal(record, ctx)

    tables = record.get("tables") or {}

    # 5) PROPHYLAXIS branch — allowed without weight/renal
    if tier == "PROPHYLAXIS":
        ptab = record.get("prophylaxis_tables") or {}
        tname = ptab.get(renal_category) or ptab.get("UNKNOWN")
        table = tables.get(tname) or {}
        okind = table.get("type") or "prophylaxis"
        return TableDoseResult(
            mode="dose", output_kind=okind, indication_tier=tier,
            renal_category=renal_category, table_name=tname,
            text=_block_text(table), **base)

    # 6) treatment tiers (HIGH/MODERATE/STANDARD)
    #    renal warnings short-circuit
    if renal_category in ("GFR_LT_15_WITHOUT_CRRT", "IHD"):
        table = tables.get(renal_category) or {}
        return TableDoseResult(
            mode="dose", output_kind="renal_warning", indication_tier=tier,
            renal_category=renal_category, table_name=renal_category,
            text=_block_text(table), **base)

    # renal unknown → cannot pick a table; ask (never guess / dump a table)
    if renal_category == "UNKNOWN":
        return _ask("missing_inputs", tier=tier, renal=renal_category)

    tname = f"{tier}_{renal_category}"
    table = tables.get(tname)
    if table is None:
        # No table for this (tier, renal) combination → ask rather than guess.
        return _ask("missing_inputs", tier=tier, renal=renal_category)

    ttype = table.get("type")
    if ttype == "fixed_dose":
        return TableDoseResult(
            mode="dose", output_kind="fixed_dose", indication_tier=tier,
            renal_category=renal_category, table_name=tname,
            target=table.get("target"), text=_block_text(table), **base)

    if ttype == "dosing_table":
        if body_weight_kg is None:
            # weight required for a per-row dose; ask (no full-table dump)
            return _ask("missing_inputs", tier=tier, renal=renal_category)
        supp_min = record.get("supported_weight_min", 40)
        supp_max = record.get("supported_weight_max", 100)
        row, note = _select_weight_row(table.get("rows") or [],
                                       float(body_weight_kg), supp_min, supp_max)
        text = _render_row(record, tier, renal_category, body_weight_kg, table, row)
        if note:
            text = text + "\n- note: " + note
        return TableDoseResult(
            mode="dose", output_kind="dosing_row", indication_tier=tier,
            renal_category=renal_category, table_name=tname,
            selected_weight=row["weight_kg"], target=table.get("target"),
            practical_dose=row.get("practical_dose"), total=row.get("total"),
            weight_note=note, text=text, **base)

    raise TableDoseError(f"table {tname!r} has unsupported type {ttype!r}")


# --------------------------------------------------------------------------- #
# Faithful plain-text rendering (harness/debug — NOT final UX phrasing)        #
# --------------------------------------------------------------------------- #
def render_table_dose(result: TableDoseResult) -> str:
    if result.needs_confirmation:
        return (f"[{result.source_label}] Needs confirmation: "
                f"{result.confirmation_reason}")
    lines = [f"[{result.source_label}]"]
    if result.text:
        lines.append(result.text)
    if result.footer:
        lines.append(result.footer.rstrip("\n"))
    return "\n".join(lines)


__all__ = [
    "get_table_dose", "render_table_dose", "load_table_lookup",
    "TableDoseResult", "TableDoseError",
]
