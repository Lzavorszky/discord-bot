#!/usr/bin/env python3
"""get_dose.py — the first ID Bot rebuild tool (Plan D, Phase 3.1).

The vertical slice: given a migrated `drug_dose` protocol and the caller's
clinical slots, pick the right tier off the ordered `select:` ladder and return
it **verbatim**. This tool NEVER computes a novel dose — it only ever returns
text that already exists in an approved protocol file, plus the protocol's own
`always_show` tiers (e.g. the LOADING dose) and footer/prep notes.

Design rules (locked in PROGRESS.md / the roadmap)
--------------------------------------------------
* **List order IS priority.** The `select:` ladder is walked top to bottom; the
  first guard that fires wins. This mirrors the source SELECTION_RULES priority,
  already encoded as list order during migration.
* **Verbatim only.** The returned dose/when/admin strings are copied straight
  from the protocol's `tiers:` table. No arithmetic, no interpolation.
* **Out-of-range GFR → ask, don't guess.** If a numeric slot is provided outside
  its declared `min/max` and the slot says `on_out_of_range: ask_confirmation`,
  the tool returns `needs_confirmation=True` and runs no ladder.
* **Guards are evaluated by a restricted AST walker, not `eval`.** Only boolean
  ops, comparisons, the declared slot names, and literals are allowed; anything
  else (or an unknown name — i.e. a protocol typo) raises loudly.

Public API
----------
    get_dose(drug_id, *, gfr=None, crrt=False, ihd=False,
             cns_infection=False, tdm_low_level=False,
             record=None, protocols_dir=None) -> DoseResult
    render_dose(result) -> str        # faithful plain-text dump (harness/debug)
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Load protocol records through the existing validating loader. Insert the
# protocols dir on sys.path so this works whether imported as a package or run
# directly (mirrors loader.py / run_harness.py).
import sys as _sys
_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_sys.path.insert(0, str(_PKG / "protocols"))
from loader import load_protocol  # noqa: E402  type: ignore

DEFAULT_PROTOCOLS_DIR = _PKG / "protocols"

# Sentinel a `default:` rung may name to mean "show the whole table".
DEFAULT_ANSWER = "DEFAULT_ANSWER"


class DoseError(ValueError):
    """Raised when get_dose cannot honour a request (bad drug, bad guard)."""


class GuardError(DoseError):
    """Raised when a `select:` guard expression is malformed or references an
    unknown slot — a protocol-authoring bug we want to fail loudly on."""


# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Tier:
    """One dose tier, copied verbatim from the protocol's `tiers:` table."""
    name: str
    dose: str
    when: Optional[str] = None
    admin: Optional[str] = None
    always_show: bool = False


@dataclass
class DoseResult:
    """The structured decision trace get_dose returns.

    Exactly one of these holds:
      * `needs_confirmation` is True (GFR out of declared range), or
      * `is_default` is True  → `tiers` is the full table (no single tier matched), or
      * `matched_tier` is set → `tiers` is [always_show tiers..., matched_tier].
    """
    drug_id: str
    source_label: str
    route: str = "drug_dose"
    tool: str = "get_dose"
    needs_confirmation: bool = False
    confirmation_reason: Optional[str] = None
    matched_rule_index: Optional[int] = None     # which select[] rung fired
    matched_tier: Optional[Tier] = None          # the single selected tier (None for default)
    is_default: bool = False                     # default rung → full table
    tiers: list[Tier] = field(default_factory=list)   # tiers to present, in declared order
    always_show: list[Tier] = field(default_factory=list)
    footer: Optional[str] = None
    prep: Optional[str] = None
    never: list[str] = field(default_factory=list)
    inputs: dict = field(default_factory=dict)   # echo of the slots used

    def tier_names(self) -> list[str]:
        return [t.name for t in self.tiers]

    def to_dict(self) -> dict:
        def _tier(t: Optional[Tier]):
            if t is None:
                return None
            return {"name": t.name, "dose": t.dose, "when": t.when,
                    "admin": t.admin, "always_show": t.always_show}
        return {
            "drug_id": self.drug_id, "source_label": self.source_label,
            "route": self.route, "tool": self.tool,
            "needs_confirmation": self.needs_confirmation,
            "confirmation_reason": self.confirmation_reason,
            "matched_rule_index": self.matched_rule_index,
            "matched_tier": _tier(self.matched_tier),
            "is_default": self.is_default,
            "tiers": [_tier(t) for t in self.tiers],
            "always_show": [_tier(t) for t in self.always_show],
            "footer": self.footer, "prep": self.prep, "never": list(self.never),
            "inputs": dict(self.inputs),
        }


# --------------------------------------------------------------------------- #
# Restricted guard evaluator — boolean ops + comparisons over declared slots.  #
# No function calls, attributes, subscripts, names outside the slot context.   #
# A None operand in a comparison (e.g. unprovided gfr) makes the guard False,   #
# so an unspecified slot simply doesn't fire its rung (falls through to default).#
# --------------------------------------------------------------------------- #
_CMP_OPS = {
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}


def _eval_guard(expr: str, ctx: dict) -> bool:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise GuardError(f"cannot parse guard {expr!r}: {exc}") from exc
    return bool(_ev(tree.body, expr, ctx))


def _ev(node, expr: str, ctx: dict):
    if isinstance(node, ast.BoolOp):
        vals = [_ev(v, expr, ctx) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(vals)
        if isinstance(node.op, ast.Or):
            return any(vals)
        raise GuardError(f"unsupported boolean op in guard {expr!r}")
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _ev(node.operand, expr, ctx)
        if isinstance(node.op, ast.USub):
            return -_ev(node.operand, expr, ctx)
        raise GuardError(f"unsupported unary op in guard {expr!r}")
    if isinstance(node, ast.Compare):
        left = _ev(node.left, expr, ctx)
        for op, comp in zip(node.ops, node.comparators):
            right = _ev(comp, expr, ctx)
            fn = _CMP_OPS.get(type(op))
            if fn is None:
                raise GuardError(f"unsupported comparison in guard {expr!r}")
            # An unprovided operand (None) → the whole guard is False.
            if left is None or right is None:
                return False
            if not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        if node.id not in ctx:
            raise GuardError(
                f"guard {expr!r} references unknown slot {node.id!r} "
                f"(declared slots: {sorted(ctx)})")
        return ctx[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    raise GuardError(
        f"disallowed expression in guard {expr!r}: {type(node).__name__}")


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #
def load_drug_dose(drug_id: str, protocols_dir=None) -> dict:
    """Load + validate the drug_dose record named `drug_id` from a directory."""
    base = Path(protocols_dir) if protocols_dir else DEFAULT_PROTOCOLS_DIR
    path = base / f"{drug_id}.yaml"
    if not path.exists():
        alt = base / f"{drug_id}.yml"
        path = alt if alt.exists() else path
    if not path.exists():
        raise DoseError(f"no drug_dose protocol file for {drug_id!r} in {base}")
    record = load_protocol(path)
    if record.get("kind") != "drug_dose":
        raise DoseError(
            f"{drug_id!r} is kind {record.get('kind')!r}, not 'drug_dose'")
    return record


# --------------------------------------------------------------------------- #
# The tool                                                                    #
# --------------------------------------------------------------------------- #
def _tier_from_spec(name: str, spec: dict) -> Tier:
    return Tier(
        name=name,
        dose=spec.get("dose", ""),
        when=spec.get("when"),
        admin=spec.get("admin"),
        always_show=bool(spec.get("always_show", False)),
    )


def _range_problem(slots: dict, name: str, value) -> Optional[str]:
    """Return a confirmation reason if `value` is outside the slot's declared
    range and the slot asks for confirmation; else None."""
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


def get_dose(
    drug_id: str,
    *,
    gfr: Optional[float] = None,
    crrt: bool = False,
    ihd: bool = False,
    cns_infection: bool = False,
    tdm_low_level: bool = False,
    record: Optional[dict] = None,
    protocols_dir=None,
    **extra_slots: object,
) -> DoseResult:
    """Select and return the verbatim dose tier for `drug_id`.

    Pass `record` to use an already-loaded protocol dict (offline unit tests);
    otherwise the record is loaded from `protocols_dir` (default: the package
    `protocols/` dir).

    ``extra_slots`` accepts any additional protocol-declared boolean slots
    (e.g. ``septic_shock=True``, ``hypoalbuminemia=True`` for ceftriaxone)
    that are not in the fixed signature.  Unknown names are silently ignored
    unless they appear in the protocol's ``slots:`` section.
    """
    if record is None:
        record = load_drug_dose(drug_id, protocols_dir=protocols_dir)
    if record.get("kind") != "drug_dose":
        raise DoseError(f"{drug_id!r} is not a drug_dose protocol")

    source_label = record.get("source_label") or record.get("id") or drug_id
    tiers_raw = record.get("tiers") or {}
    declared_order = list(tiers_raw.keys())
    tier_objs = {name: _tier_from_spec(name, spec)
                 for name, spec in tiers_raw.items()}
    always_show = [tier_objs[n] for n in declared_order
                   if tier_objs[n].always_show]
    slots = record.get("slots") or {}

    ctx: dict = {
        "gfr": gfr,
        "crrt": bool(crrt),
        "ihd": bool(ihd),
        "cns_infection": bool(cns_infection),
        "tdm_low_level": bool(tdm_low_level),
    }
    # Populate any protocol-specific slots declared in the YAML but not in the
    # fixed signature (e.g. septic_shock, hypoalbuminemia for ceftriaxone).
    for _sname, _sspec in (slots or {}).items():
        if _sname in ctx:
            continue  # already set from fixed parameter
        if _sname in extra_slots:
            _stype = _sspec.get("type") if isinstance(_sspec, dict) else None
            ctx[_sname] = bool(extra_slots[_sname]) if _stype == "bool" else extra_slots[_sname]
        else:
            # Default: False for bool slots, None for numeric slots.
            _stype = _sspec.get("type") if isinstance(_sspec, dict) else None
            ctx[_sname] = False if _stype == "bool" else None
    inputs = {k: v for k, v in ctx.items()}

    base = dict(
        drug_id=record.get("id", drug_id),
        source_label=source_label,
        footer=record.get("footer"),
        prep=record.get("prep"),
        never=list(record.get("never") or []),
        always_show=always_show,
        inputs=inputs,
    )

    # 1) Out-of-range numeric slot → ask, never guess. Checks EVERY declared
    #    numeric slot that was actually provided (unprovided slots are None in
    #    ctx and skipped) — e.g. an implausible vancomycin_level asks rather than
    #    silently selecting a TDM band.
    for sname in slots:
        reason = _range_problem(slots, sname, ctx.get(sname))
        if reason:
            return DoseResult(needs_confirmation=True,
                              confirmation_reason=reason, **base)

    # 2) Walk the ordered ladder; first firing rung wins.
    select = record.get("select") or []
    for i, entry in enumerate(select):
        if "default" in entry:
            # Terminal rung → present the whole table, in declared order.
            return DoseResult(
                is_default=True,
                matched_rule_index=i,
                tiers=[tier_objs[n] for n in declared_order],
                **base,
            )
        guard = entry.get("if")
        if guard is None:
            raise GuardError(f"select[{i}] for {drug_id!r} has no 'if' or 'default'")
        if _eval_guard(guard, ctx):
            tier_name = entry.get("tier")
            if tier_name not in tier_objs:
                raise DoseError(
                    f"select[{i}] for {drug_id!r} targets undefined tier "
                    f"{tier_name!r}")
            matched = tier_objs[tier_name]
            # Present always_show tiers + the matched tier, in declared order,
            # de-duplicated (the matched tier might itself be always_show).
            present_names = [n for n in declared_order
                             if tier_objs[n].always_show or n == tier_name]
            return DoseResult(
                matched_tier=matched,
                matched_rule_index=i,
                tiers=[tier_objs[n] for n in present_names],
                **base,
            )

    # The validator guarantees a terminal default; reaching here is a bug.
    raise DoseError(
        f"select ladder for {drug_id!r} fell through with no default rung")


# --------------------------------------------------------------------------- #
# Faithful plain-text rendering (for the harness & debugging — NOT final UX    #
# phrasing, which the phrasing model will own in a later phase).               #
# --------------------------------------------------------------------------- #
def render_dose(result: DoseResult) -> str:
    if result.needs_confirmation:
        return (f"[{result.source_label}] Needs confirmation: "
                f"{result.confirmation_reason}")
    lines = [f"[{result.source_label}]"]
    for t in result.tiers:
        parts = [f"{t.name}: {t.dose}"]
        if t.when:
            parts.append(f"({t.when})")
        if t.admin:
            parts.append(f"— {t.admin}")
        lines.append(" ".join(parts))
    if result.prep:
        lines.append(f"Prep: {result.prep.strip()}")
    if result.footer:
        lines.append(result.footer)
    return "\n".join(lines)


__all__ = [
    "get_dose", "render_dose", "load_drug_dose",
    "DoseResult", "Tier", "DoseError", "GuardError", "DEFAULT_ANSWER",
]
