"""tools.py — provider-agnostic tool definitions for the LLM seam (Phase 1).

A `Tool` is a deterministic engine the router may invoke (get_dose, interpret_pcr,
…; bodies land in Phase 3). It carries a name, a description, a JSON-Schema for its
arguments, and an optional Python handler. The same `Tool` renders to whichever
wire shape the provider needs:

    OpenAI / DeepSeek (OpenAI-compatible)  →  Tool.to_openai()
    Anthropic                              →  Tool.to_anthropic()

A `ToolCall` is the model's decision to invoke one tool: a name + parsed arguments,
provider-agnostic. Every LLMProvider.call_with_tools returns either a ToolCall or a
plain string (the model answered without calling a tool).

Argument validation is intentionally minimal and dependency-free (the project does
not depend on `jsonschema`): it checks required keys are present and that provided
values loosely match their declared JSON-Schema primitive type. That is enough for
the routing contract; deep validation belongs to each tool's handler in Phase 3.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class ToolCall:
    """A model's decision to call one tool. Provider-agnostic.

    name       the tool's name (matches a Tool.name)
    arguments  parsed argument dict (already JSON-decoded)
    id         provider-side call id, needed to submit the result back
    raw        the original provider object, kept for debugging/tracing
    """
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: Optional[str] = None
    raw: Any = None


# JSON-Schema primitive type name -> python isinstance check.
# bool is excluded from "number"/"integer" deliberately (it is an int subclass).
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_integer(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "number": _is_number,
    "integer": _is_integer,
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, (list, tuple)),
    "null": lambda v: v is None,
}


@dataclass
class Tool:
    """A provider-agnostic tool definition.

    parameters is a JSON-Schema object describing the arguments, e.g.
        {"type": "object",
         "properties": {"drug": {"type": "string"}, "crcl": {"type": "number"}},
         "required": ["drug"]}
    """
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Optional[Callable[..., Any]] = None

    # -- wire shapes ---------------------------------------------------------
    def to_openai(self) -> dict[str, Any]:
        """OpenAI (and DeepSeek, which is OpenAI-compatible) function shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic(self) -> dict[str, Any]:
        """Anthropic tool shape (note: input_schema, not parameters)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    # -- minimal validation --------------------------------------------------
    def validate_arguments(self, args: Mapping[str, Any]) -> list[str]:
        """Return a list of human-readable problems (empty list = valid).

        Checks: args is a mapping; every `required` key is present; each provided
        value loosely matches its declared primitive type. Unknown keys and
        unconstrained schemas are tolerated (the handler does the rest).
        """
        problems: list[str] = []
        if not isinstance(args, Mapping):
            return [f"arguments must be an object, got {type(args).__name__}"]

        props: dict = self.parameters.get("properties", {}) or {}
        required: Sequence[str] = self.parameters.get("required", []) or []

        for key in required:
            if key not in args:
                problems.append(f"missing required argument: {key!r}")

        for key, value in args.items():
            spec = props.get(key)
            if not spec:
                continue  # unknown / unconstrained key — tolerate
            declared = spec.get("type")
            if declared is None:
                continue
            # `type` may be a single string or a list of allowed types.
            allowed = [declared] if isinstance(declared, str) else list(declared)
            checks = [_TYPE_CHECKS[t] for t in allowed if t in _TYPE_CHECKS]
            if checks and not any(chk(value) for chk in checks):
                problems.append(
                    f"argument {key!r} should be {'/'.join(allowed)}, "
                    f"got {type(value).__name__}"
                )
        return problems

    def is_valid_call(self, call: "ToolCall") -> bool:
        return call.name == self.name and not self.validate_arguments(call.arguments)
