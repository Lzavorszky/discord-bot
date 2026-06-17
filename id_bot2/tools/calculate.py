#!/usr/bin/env python3
"""calculate.py — the calculator tool (Plan D, final migration phase).

The ONLY tool in the rebuild that COMPUTES a value rather than SELECTS a verbatim
cell. Every other tool (get_dose, interpret_pcr, select_pathway, get_table_dose)
returns text copied verbatim from a protocol; a calculator must do arithmetic
(BMI, BSA, steroid-dose equivalence, ...). To keep that safe we hold the line in
three ways:

  1. **The formulas are declared in the protocol, not in code.** Each
     ``calculator`` YAML lists ``methods``; each method is an ordered list of
     ``compute`` steps — an arithmetic ``expr`` or a ``lookup`` into a declared
     reference table — transcribed verbatim from the source SELECTION_RULES.
     The tool is a generic evaluator; it invents no formula.
  2. **Arithmetic is evaluated by a restricted AST, never ``eval``.** Only
     ``+ - * / // % **``, unary minus, parentheses, numeric literals, the
     declared slot/intermediate names, the constant ``pi``, and a closed
     whitelist of functions (``sqrt``, ``abs``, ``min``, ``max``, ``round``) are
     allowed. An attribute, subscript, comprehension, or unknown name raises
     ``CalcError`` — the same defence ``get_dose`` uses for its boolean guards.
  3. **Every formula is unit-tested with a hand-computed expected value** (see
     ``test_calculate.py``) and the phrased answer is checked by the grounding
     verifier in ``calculator`` (hard) mode — a phrasing pass may never introduce
     a number the computed answer didn't contain.

The state machine (mirrors the source SELECTION_RULES / required-slot gating)
---------------------------------------------------------------------------
1. **No input at all** → the verbatim ``default_answer`` (mode ``default``).
2. **Out-of-range numeric** (a provided slot outside its declared clinical range
   with ``on_out_of_range: ask_confirmation``) → ``needs_confirmation`` (mode
   ``needs_confirmation``), run nothing — never silently calculate on an
   implausible input.
3. **No method's ``requires`` are all satisfied** → the verbatim
   ``missing_inputs`` ask (mode ``needs_input``) — never guess a missing input.
4. **A ``lookup`` whose key (e.g. an unsupported steroid) is not in its table** →
   the verbatim ``unsupported_value`` message (mode ``unsupported_value``).
5. Otherwise: pick the first method whose requires are satisfied, run its compute
   steps, and render the method's verbatim ``template_en`` with the computed
   values slotted in (mode ``compute``).

Public API
----------
    calculate(calculator_id, *, record=None, protocols_dir=None, **slots)
        -> CalcResult
    render_calc(result) -> str
    load_calculator(calculator_id, *, protocols_dir=None) -> dict
"""
from __future__ import annotations

import ast
import math
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import sys as _sys
_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_sys.path.insert(0, str(_PKG))                 # textnorm
_sys.path.insert(0, str(_PKG / "protocols"))   # loader
from loader import load_protocol                        # noqa: E402

DEFAULT_PROTOCOLS_DIR = _PKG / "protocols"


class CalcError(ValueError):
    """Raised when calculate cannot honour a request (bad id/kind/formula)."""


# --------------------------------------------------------------------------- #
# Restricted arithmetic evaluator — the heart of the safety story.            #
# Only arithmetic over declared names + literals + a closed function whitelist.#
# No attributes, subscripts, calls outside the whitelist, comprehensions, or   #
# names outside the supplied context. Anything else raises CalcError.          #
# --------------------------------------------------------------------------- #
_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}

# Closed function whitelist. All pure, total, side-effect-free.
_FUNCS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
}

# Names that are constants, not slots.
_CONSTS = {"pi": math.pi}


def eval_expr(expr: str, ctx: dict):
    """Evaluate an arithmetic expression against ``ctx`` (slot/intermediate
    values + constants). Raises CalcError on anything outside the allowed set."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise CalcError(f"cannot parse formula {expr!r}: {exc}") from exc
    return _ev(tree.body, expr, ctx)


def _ev(node, expr: str, ctx: dict):
    if isinstance(node, ast.BinOp):
        fn = _BIN_OPS.get(type(node.op))
        if fn is None:
            raise CalcError(f"unsupported operator in formula {expr!r}: "
                            f"{type(node.op).__name__}")
        return fn(_ev(node.left, expr, ctx), _ev(node.right, expr, ctx))
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_ev(node.operand, expr, ctx)
        if isinstance(node.op, ast.UAdd):
            return +_ev(node.operand, expr, ctx)
        raise CalcError(f"unsupported unary op in formula {expr!r}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            name = getattr(node.func, "id", type(node.func).__name__)
            raise CalcError(f"formula {expr!r} calls non-whitelisted function {name!r}")
        if node.keywords:
            raise CalcError(f"formula {expr!r}: keyword args not allowed")
        args = [_ev(a, expr, ctx) for a in node.args]
        return _FUNCS[node.func.id](*args)
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        if node.id in ctx:
            return ctx[node.id]
        raise CalcError(
            f"formula {expr!r} references unknown name {node.id!r} "
            f"(known: {sorted(set(ctx) | set(_CONSTS))})")
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise CalcError(f"formula {expr!r}: only numeric literals allowed")
    raise CalcError(
        f"disallowed expression in formula {expr!r}: {type(node).__name__}")


# --------------------------------------------------------------------------- #
# Safe template rendering — only field names present in ctx may be referenced. #
# --------------------------------------------------------------------------- #
_FORMATTER = string.Formatter()


def render_template(template: str, ctx: dict) -> str:
    """Format ``template`` ({name} / {name:.1f}) against ctx. An unknown field or
    a bad format spec raises CalcError (catches protocol template typos early)."""
    out = []
    for literal, field_name, format_spec, conversion in _FORMATTER.parse(template):
        out.append(literal)
        if field_name is None:
            continue
        # Only bare names (no attribute/index access) referencing ctx keys.
        if not field_name or any(c in field_name for c in ".[]"):
            raise CalcError(f"template references disallowed field {field_name!r}")
        if field_name not in ctx:
            raise CalcError(f"template references unknown field {field_name!r} "
                            f"(known: {sorted(ctx)})")
        value = ctx[field_name]
        try:
            out.append(format(value, format_spec or ""))
        except (ValueError, TypeError) as exc:
            raise CalcError(f"template field {field_name!r} bad format "
                            f"{format_spec!r}: {exc}") from exc
    return "".join(out)


# --------------------------------------------------------------------------- #
# Result type                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class CalcResult:
    calculator_id: str
    source_label: str
    canonical_name: str = ""
    route: str = "calculator"
    tool: str = "calculate"
    # mode: compute | needs_input | needs_confirmation | unsupported_value | default
    mode: str = "compute"
    method_id: Optional[str] = None
    needs_input: bool = False
    needs_confirmation: bool = False
    confirmation_reason: Optional[str] = None
    values: dict = field(default_factory=dict)   # all computed intermediates+outputs
    outputs: dict = field(default_factory=dict)   # just the declared `outputs`
    text: str = ""
    footer: Optional[str] = None
    never: list = field(default_factory=list)
    inputs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "calculator_id": self.calculator_id, "source_label": self.source_label,
            "canonical_name": self.canonical_name, "route": self.route,
            "tool": self.tool, "mode": self.mode, "method_id": self.method_id,
            "needs_input": self.needs_input,
            "needs_confirmation": self.needs_confirmation,
            "confirmation_reason": self.confirmation_reason,
            "values": dict(self.values), "outputs": dict(self.outputs),
            "text": self.text, "footer": self.footer,
            "never": list(self.never), "inputs": dict(self.inputs),
        }


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #
def load_calculator(calculator_id: str, *, protocols_dir=None) -> dict:
    base = Path(protocols_dir) if protocols_dir else DEFAULT_PROTOCOLS_DIR
    path = base / f"{calculator_id}.yaml"
    if not path.exists():
        alt = base / f"{calculator_id}.yml"
        path = alt if alt.exists() else path
    if not path.exists():
        raise CalcError(f"no calculator protocol file for {calculator_id!r} in {base}")
    record = load_protocol(path)
    if record.get("kind") != "calculator":
        raise CalcError(f"{calculator_id!r} is kind {record.get('kind')!r}, "
                        f"not 'calculator'")
    return record


# --------------------------------------------------------------------------- #
# Slot coercion + range gate                                                  #
# --------------------------------------------------------------------------- #
def _coerce_numeric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def _present(value) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


# --------------------------------------------------------------------------- #
# The tool                                                                    #
# --------------------------------------------------------------------------- #
def calculate(calculator_id: str, *, record=None, protocols_dir=None,
              **slots) -> CalcResult:
    rec = record if record is not None else load_calculator(
        calculator_id, protocols_dir=protocols_dir)

    source_label = rec.get("source_label", calculator_id)
    canonical = rec.get("canonical_name", "")
    footer = rec.get("footer")
    never = list(rec.get("never") or [])
    declared = rec.get("slots") or {}
    methods = rec.get("methods") or []

    def _result(**kw) -> CalcResult:
        base = dict(calculator_id=calculator_id, source_label=source_label,
                    canonical_name=canonical, footer=footer, never=never,
                    inputs={k: v for k, v in slots.items() if _present(v)})
        base.update(kw)
        return CalcResult(**base)

    # Build the typed input context (only declared slots; coerce numerics).
    ctx: dict = {}
    provided_any = False
    for name, spec in declared.items():
        raw = slots.get(name)
        if not _present(raw):
            ctx[name] = None
            continue
        provided_any = True
        stype = spec.get("type")
        if stype == "number":
            num = _coerce_numeric(raw)
            if num is None:
                # A non-numeric value for a numeric slot is treated as absent.
                ctx[name] = None
                continue
            # Out-of-range gate (ask, never silently compute on implausible input).
            lo, hi = spec.get("min"), spec.get("max")
            policy = spec.get("on_out_of_range")
            if policy == "ask_confirmation" and (
                    (lo is not None and num < lo) or (hi is not None and num > hi)):
                return _result(
                    mode="needs_confirmation", needs_confirmation=True,
                    confirmation_reason=(
                        f"{name}={num:g} is outside the expected range "
                        f"{lo}-{hi} {spec.get('unit', '')}".strip()),
                    text=(rec.get("missing_inputs")
                          or rec.get("default_answer")
                          or f"Please confirm {name}: {num:g} looks out of range."))
            ctx[name] = num
        else:
            ctx[name] = str(raw).strip()

    # 1) No input at all → verbatim default_answer.
    if not provided_any:
        return _result(mode="default",
                       text=rec.get("default_answer")
                       or rec.get("missing_inputs") or "Please provide inputs.")

    # 2) Select the first method whose `requires` are all present.
    chosen = None
    for m in methods:
        req = m.get("requires") or []
        if all(_present(ctx.get(r)) for r in req):
            chosen = m
            break
    if chosen is None:
        return _result(mode="needs_input", needs_input=True,
                       text=rec.get("missing_inputs")
                       or rec.get("default_answer") or "Please provide more inputs.")

    # 3) Run the compute steps in order.
    work = {k: v for k, v in ctx.items() if v is not None}
    for step in chosen.get("compute") or []:
        name = step["name"]
        if "lookup" in step:
            key = str(ctx.get(step["lookup"]))
            table = step.get("table") or {}
            if key not in table:
                return _result(
                    mode="unsupported_value", method_id=chosen.get("id"),
                    text=rec.get("unsupported_value")
                    or rec.get("missing_inputs")
                    or f"{key!r} is not supported.")
            work[name] = table[key]
        else:
            work[name] = eval_expr(step["expr"], work)

    # 4) Render the method's verbatim template with the computed values.
    template = chosen.get("template_en") or chosen.get("template_hu")
    text = render_template(template, work)

    output_names = chosen.get("outputs") or []
    outputs = {n: work[n] for n in output_names if n in work}
    return _result(mode="compute", method_id=chosen.get("id"),
                   values={k: v for k, v in work.items()},
                   outputs=outputs, text=text)


def render_calc(result: CalcResult) -> str:
    """Faithful plain-text rendering for the harness/router (the verbatim answer
    text plus the footer). Not final UX phrasing — that's the phrasing model."""
    parts = [result.text.rstrip()]
    if result.footer:
        parts.append("")
        parts.append(result.footer.strip())
    return "\n".join(parts).strip()
