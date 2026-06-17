#!/usr/bin/env python3
"""select_pathway.py — the pathway-selection tool (Plan D, Phase 2.5 cont.).

Given a migrated ``pathway`` protocol (an empiric-treatment / diagnostic ladder
such as CAP, UTI, SBP, cdiff, endocarditis, intra-abdominal infections) and the
clinical context slots the user supplied, walk the protocol's ordered ``select:``
ladder and return the matching output's **verbatim** text.

Like ``get_dose`` and ``interpret_pcr``, this tool NEVER composes a novel
clinical recommendation. It only ever SELECTS one of the answer strings already
approved in the protocol file:

  * the ``select:`` ladder is walked top-to-bottom (list order IS priority,
    mirroring the source SELECTION_RULES priority numbers) — the first guard that
    fires wins;
  * the chosen output's ``text_en`` / ``text_hu`` is returned exactly as written;
  * a terminal ``default:`` rung returns the protocol's DEFAULT_ANSWER (the
    source's "quick map" / "choose a section" text) — never a guessed pathway.

Guards are evaluated by the SAME restricted AST walker ``get_dose`` uses (no
``eval``): only boolean ops, comparisons, the protocol's declared slot names, and
literals are allowed; a guard that references an undeclared slot (a protocol
typo) raises ``GuardError`` loudly. A ``None`` operand (an unsupplied slot) makes
its comparison False, so an unspecified slot simply doesn't fire its rung and the
ladder falls through to the next — and ultimately to the default.

Dosing is NOT this tool's job. A pathway names antimicrobials (in each output's
``items``); a follow-up dose request is routed to the relevant ``drug_dose``
protocol via ``get_dose``. ``select_pathway`` emits no dose of its own beyond the
short verbatim starting-dose strings the source itself prints inside an output.

Public API
----------
    select_pathway(pathway_id, *, record=None, protocols_dir=None, **slots)
        -> PathwayResult
    render_pathway(result) -> str
    load_pathway(pathway_id, *, protocols_dir=None) -> dict
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import sys as _sys
_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_sys.path.insert(0, str(_HERE))                # get_dose (guard evaluator)
_sys.path.insert(0, str(_PKG / "protocols"))   # loader
from loader import load_protocol               # noqa: E402  type: ignore
# Reuse get_dose's restricted guard evaluator + its error type so pathway guards
# and dose guards can never diverge in what they allow.
from get_dose import _eval_guard, GuardError   # noqa: E402  type: ignore

DEFAULT_PROTOCOLS_DIR = _PKG / "protocols"


class PathwayError(ValueError):
    """Raised when select_pathway cannot honour a request (bad id/kind/target)."""


# --------------------------------------------------------------------------- #
# Result type                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class PathwayResult:
    pathway_id: str
    source_label: str
    canonical_name: str = ""
    route: str = "pathway"
    tool: str = "select_pathway"
    output: str = ""                       # the selected output name
    items: list = field(default_factory=list)
    text_hu: str = ""
    text_en: str = ""
    is_default: bool = False               # True when the default rung fired
    matched_rule_index: Optional[int] = None
    footer: Optional[str] = None
    inputs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pathway_id": self.pathway_id, "source_label": self.source_label,
            "canonical_name": self.canonical_name, "route": self.route,
            "tool": self.tool, "output": self.output, "items": list(self.items),
            "text_hu": self.text_hu, "text_en": self.text_en,
            "is_default": self.is_default,
            "matched_rule_index": self.matched_rule_index,
            "footer": self.footer, "inputs": dict(self.inputs),
        }


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #
def load_pathway(pathway_id: str, *, protocols_dir=None) -> dict:
    base = Path(protocols_dir) if protocols_dir else DEFAULT_PROTOCOLS_DIR
    path = base / f"{pathway_id}.yaml"
    if not path.exists():
        alt = base / f"{pathway_id}.yml"
        path = alt if alt.exists() else path
    if not path.exists():
        raise PathwayError(f"no pathway protocol file for {pathway_id!r} in {base}")
    record = load_protocol(path)
    if record.get("kind") != "pathway":
        raise PathwayError(
            f"{pathway_id!r} is kind {record.get('kind')!r}, not 'pathway'")
    return record


# --------------------------------------------------------------------------- #
# Context building                                                            #
# --------------------------------------------------------------------------- #
def _build_ctx(record: dict, provided: dict) -> dict:
    """Populate every DECLARED slot into the guard context.

    * boolean slots default to False, and a provided value is coerced to bool;
    * every other slot type defaults to None (so its comparisons are False until
      a value is supplied), and a provided value is passed through unchanged.

    Slots that are NOT declared by this protocol are ignored — a caller can pass
    a generic bag of slots and only the relevant ones take effect (mirrors
    get_dose's ``**extra_slots`` discipline)."""
    slots = record.get("slots") or {}
    ctx: dict = {}
    for name, spec in slots.items():
        stype = spec.get("type") if isinstance(spec, dict) else None
        if name in provided and provided[name] is not None:
            ctx[name] = bool(provided[name]) if stype == "bool" else provided[name]
        else:
            ctx[name] = False if stype == "bool" else None
    return ctx


def _output(record: dict, name: str) -> dict:
    outputs = record.get("outputs") or {}
    spec = outputs.get(name)
    if spec is None:
        raise PathwayError(
            f"select ladder targets undefined output {name!r} "
            f"(defined: {sorted(outputs)})")
    return spec if isinstance(spec, dict) else {}


# --------------------------------------------------------------------------- #
# The tool                                                                    #
# --------------------------------------------------------------------------- #
def select_pathway(pathway_id: str, *, record: Optional[dict] = None,
                   protocols_dir=None, **slots) -> PathwayResult:
    """Select and return the verbatim output for ``pathway_id`` given the slots.

    Pass ``record`` to use an already-loaded protocol dict (offline unit tests);
    otherwise it is loaded from ``protocols_dir`` (default: the package
    ``protocols/`` dir). Extra/undeclared slots are ignored.
    """
    if record is None:
        record = load_pathway(pathway_id, protocols_dir=protocols_dir)
    if record.get("kind") != "pathway":
        raise PathwayError(f"{pathway_id!r} is not a pathway protocol")

    pid = record.get("id", pathway_id)
    source_label = record.get("source_label") or pid
    ctx = _build_ctx(record, slots)

    base = dict(
        pathway_id=pid,
        source_label=source_label,
        canonical_name=record.get("canonical_name", ""),
        footer=record.get("footer"),
        inputs=dict(ctx),
    )

    def _result(output_name: str, idx: int, is_default: bool) -> PathwayResult:
        ospec = _output(record, output_name)
        return PathwayResult(
            output=output_name,
            items=list(ospec.get("items") or []),
            text_hu=ospec.get("text_hu", "") or "",
            text_en=ospec.get("text_en", "") or "",
            is_default=is_default,
            matched_rule_index=idx,
            **base,
        )

    select = record.get("select") or []
    for i, entry in enumerate(select):
        if "default" in entry:
            return _result(entry["default"], i, True)
        guard = entry.get("if")
        if guard is None:
            raise GuardError(
                f"select[{i}] for {pid!r} has no 'if' or 'default'")
        if _eval_guard(guard, ctx):
            target = entry.get("output")
            if not target:
                raise PathwayError(
                    f"select[{i}] for {pid!r} fired but names no 'output'")
            return _result(target, i, False)

    # The validator guarantees a terminal default; reaching here is a bug.
    raise PathwayError(
        f"select ladder for {pid!r} fell through with no default rung")


# --------------------------------------------------------------------------- #
# Faithful plain-text rendering (harness/debug — NOT final UX phrasing)        #
# --------------------------------------------------------------------------- #
def render_pathway(result: PathwayResult) -> str:
    """Render the selected output verbatim. Prefers the English text; falls back
    to the Hungarian text when an output has no English string (e.g. some source
    chunks are Hungarian-only)."""
    body = result.text_en.strip() or result.text_hu.strip()
    lines = [f"[{result.source_label}] {result.output}"]
    if body:
        lines.append(body)
    if result.footer:
        lines.append(result.footer.strip())
    return "\n".join(lines)


__all__ = [
    "select_pathway", "render_pathway", "load_pathway",
    "PathwayResult", "PathwayError",
]
