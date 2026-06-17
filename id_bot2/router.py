#!/usr/bin/env python3
"""router.py — the ID Bot rebuild router (Plan D, roadmap 3.5 / PROGRESS "Phase 4").

The router is the single decision point that turns a *user message* into a
*structured tool call* and runs it. Today the only dose-emitting tool is
``get_dose`` over the 30 migrated ``drug_dose`` protocols (29 antibiotics +
vancomycin); ``interpret_pcr`` / ``select_pathway`` / ``answer_from_section``
register here in later phases without changing this contract.

Two resolution strategies, behind one interface
-----------------------------------------------
1. **Deterministic resolver (default, offline, free).** Alias-matches the drug
   against the protocols' own ``aliases:`` (accent-folded, longest-alias-first)
   and keyword-extracts the clinical slots (gfr, crrt, ihd, cns, tdm, septic
   shock, hypoalbuminaemia, weight, vanco level, mic). This is what ``check.sh``
   runs every session — no API key, fully reproducible — and it doubles as a
   fast-path / fallback in production.
2. **LLM resolver (production primary).** When a deterministic match is not
   found (free-text, paraphrase, code-switching) and an ``LLMProvider`` is
   supplied, the router asks ``provider.call_with_tools`` to pick the tool and
   arguments, then dispatches the returned ``ToolCall`` to the same handler.
   Any provider that passes ``test_provider_contract`` works here unchanged.

Safety invariants (locked in PROGRESS.md / the roadmap)
-------------------------------------------------------
* **No silent answers (F10/F11).** A message that resolves to no known drug
  returns ``route="unsupported"`` with an explicit "not covered" message — never
  a guessed dose.
* **Ambiguity asks (F2).** A message that matches two or more distinct drugs
  returns ``route="clarify"`` listing the candidates, rather than picking one.
* **The tool stays verbatim.** The router never computes a dose; it only selects
  a ``get_dose`` call, and ``get_dose`` only ever returns approved protocol text.
* **Out-of-range slots ask, don't guess.** Propagated straight from ``get_dose``
  (``needs_confirmation`` → ``needs_clarification`` on the result).

Public API
----------
    Router(protocols_dir=None, provider=None)
    Router.route(message, *, provider=None) -> RouterResult
    Router.tools() -> list[Tool]          # the tool schemas exposed to the LLM
    resolve_call(message, registry) -> RoutedCall | None   # deterministic stage
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Union

# --- imports wired the same way get_dose.py / run_harness.py do --------------
import sys as _sys
_HERE = Path(__file__).resolve().parent
_sys.path.insert(0, str(_HERE))                 # id_bot2/  (textnorm)
_sys.path.insert(0, str(_HERE / "protocols"))   # loader
_sys.path.insert(0, str(_HERE / "tools"))       # get_dose

from textnorm import repair_mojibake, fold_accents  # noqa: E402
from loader import load_protocol_dir              # noqa: E402
import get_dose as _gd                            # noqa: E402
from llm.tools import Tool, ToolCall              # noqa: E402
from verifier import verify_grounding             # noqa: E402

DEFAULT_PROTOCOLS_DIR = _HERE / "protocols"

# Slot catalogue. Numeric slots carry a compiled extraction pattern (the value is
# captured in group "v"); boolean slots carry a set of trigger phrases (already
# accent-folded/lowercased — matched on the folded message). Only slots the
# *matched protocol* declares are ever populated, so a generic word like "shock"
# can never set a slot on a drug that doesn't define it.
_NUMERIC_SLOTS: dict[str, re.Pattern] = {
    # gfr / egfr / creatinine clearance / crcl  (allow =, :, or space, decimals)
    "gfr": re.compile(r"\b(?:e?gfr|cr?cl|creatinine clearance)\b[\s:=]*?(?P<v>\d+(?:\.\d+)?)"),
    "body_weight_kg": re.compile(r"\b(?:body ?weight|weight|wt)\b[\s:=]*?(?P<v>\d+(?:\.\d+)?)\s*(?:kg)?|\b(?P<v2>\d+(?:\.\d+)?)\s*kg\b"),
    "vancomycin_level": re.compile(r"\b(?:vanco(?:mycin)? )?(?:level|trough)\b[\s:=]*?(?P<v>\d+(?:\.\d+)?)"),
    "mic": re.compile(r"\bmic\b[\s:=]*?(?P<v>\d+(?:\.\d+)?)"),
}
_BOOL_SLOTS: dict[str, tuple[str, ...]] = {
    "crrt": ("crrt", "cvvhdf", "cvvhd", "cvvh", "haemofiltration", "hemofiltration",
             "continuous renal replacement"),
    "ihd": ("ihd", "intermittent haemodialysis", "intermittent hemodialysis",
            "haemodialysis", "hemodialysis", "dialysis"),
    "cns_infection": ("cns infection", "cns", "meningitis", "encephalitis",
                      "ventriculitis", "cerebral", "central nervous"),
    "tdm_low_level": ("low level", "low levels", "subtherapeutic", "sub-therapeutic",
                      "tdm low", "step up", "step-up", "below target"),
    "septic_shock": ("septic shock", "shock"),
    "hypoalbuminemia": ("hypoalbumin", "low albumin", "albumin <30", "albumin < 30",
                        "albumin<30", "low alb"),
}
# `dialysis` alone is weaker than `crrt`; if both CRRT and a bare dialysis word
# appear we still set both and let the protocol's select-ladder priority decide.


class RouterError(Exception):
    """Unexpected router-internal failure (not a user-facing 'unsupported')."""


@dataclass(frozen=True)
class RoutedCall:
    """A deterministic (or LLM) routing decision: which tool, which drug, slots."""
    tool: str
    drug_id: str
    slots: dict = field(default_factory=dict)
    via: str = "deterministic"   # "deterministic" | "llm"


@dataclass
class RouterResult:
    """Everything a caller (or the harness) needs about one routed message."""
    route: str                       # drug_dose | clarify | unsupported
    tool: Optional[str] = None       # get_dose | none
    protocol: Optional[str] = None   # drug_id when a protocol was selected
    answer: str = ""                 # rendered text (render_dose, or a message)
    needs_clarification: bool = False
    via: Optional[str] = None        # how the drug was resolved
    slots: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)  # for route == "clarify"
    dose: object = None              # the DoseResult, when route == "drug_dose"
    phrased: bool = False            # True if the phrasing model rewrote the answer
    phrasing_blocked: bool = False   # True if the verifier rejected the phrasing
    grounded_answer: str = ""        # the verbatim tool text the answer was verified against

    def to_dict(self) -> dict:
        return {
            "route": self.route, "tool": self.tool, "protocol": self.protocol,
            "answer": self.answer, "needs_clarification": self.needs_clarification,
            "via": self.via, "slots": dict(self.slots),
            "candidates": list(self.candidates),
            "dose": self.dose.to_dict() if self.dose is not None else None,
            "phrased": self.phrased,
            "phrasing_blocked": self.phrasing_blocked,
        }


# --------------------------------------------------------------------------- #
# Protocol registry (id + aliases + declared slots), built once per Router.    #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Entry:
    drug_id: str
    slots: tuple
    aliases: tuple   # folded alias -> longest first (built in _build_registry)


def _build_registry(protocols_dir) -> dict[str, _Entry]:
    records = load_protocol_dir(protocols_dir)
    registry: dict[str, _Entry] = {}
    for _path, rec in records:
        if rec.get("kind") != "drug_dose":
            continue
        drug_id = rec["id"]
        aliases = set(rec.get("aliases") or [])
        aliases.add(drug_id)
        aliases.add(rec.get("canonical_name") or "")
        aliases.add(rec.get("source_label") or "")
        folded = sorted({_norm(a) for a in aliases if a and a.strip()},
                        key=len, reverse=True)
        registry[drug_id] = _Entry(
            drug_id=drug_id,
            slots=tuple((rec.get("slots") or {}).keys()),
            aliases=tuple(folded),
        )
    return registry


def _norm(text: str) -> str:
    """Fold accents/case AND collapse separators (/, -, _) and runs of
    whitespace to single spaces, so 'Ceftazidime-avibactam', 'ceftazidime/avibactam'
    and 'ceftazidime avibactam' all compare equal."""
    t = fold_accents(repair_mojibake(text or "")).lower()
    t = re.sub(r"[/_\-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _alias_hit(folded_msg: str, alias: str) -> bool:
    """Whole-token (phrase) match on the already-normalised message."""
    return re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded_msg) is not None


def _match_drugs(folded_msg: str, registry: dict[str, _Entry]) -> list[str]:
    """Return drug_ids whose alias set hits the message, **specific-first**.

    For each drug, take its longest matching alias and the (start,end) span it
    occupies. Then drop any drug whose span is fully contained inside another
    drug's span — that is the less-specific component of a compound name
    (e.g. 'ceftazidime' inside
    'ceftazidime avibactam'), so the compound wins. Genuinely distinct drugs
    mentioned at different places keep their own spans and remain candidates
    (→ clarify)."""
    spans: list[tuple[int, int, str]] = []   # (start, end, drug_id)
    for drug_id, entry in registry.items():
        for alias in entry.aliases:          # longest alias first
            m = re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded_msg)
            if m:
                spans.append((m.start(), m.end(), drug_id))
                break
    # containment dedup: keep a span unless another span strictly contains it.
    kept: list[tuple[int, int, str]] = []
    for a in spans:
        contained = any(
            b is not a and b[0] <= a[0] and b[1] >= a[1] and (b[1] - b[0]) > (a[1] - a[0])
            for b in spans
        )
        if not contained:
            kept.append(a)
    kept.sort()
    # de-dupe drug_ids while preserving first-position order
    seen: set[str] = set()
    out: list[str] = []
    for _s, _e, d in kept:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _extract_slots(folded_msg: str, declared: Sequence[str]) -> dict:
    """Pull only the slots the matched protocol declares out of the message."""
    out: dict = {}
    for name in declared:
        if name in _NUMERIC_SLOTS:
            m = _NUMERIC_SLOTS[name].search(folded_msg)
            if m:
                val = m.groupdict().get("v") or m.groupdict().get("v2")
                if val is not None:
                    num = float(val)
                    out[name] = int(num) if num.is_integer() else num
        elif name in _BOOL_SLOTS:
            for phrase in _BOOL_SLOTS[name]:
                if _alias_hit(folded_msg, phrase) if " " not in phrase else (phrase in folded_msg):
                    out[name] = True
                    break
    return out


def resolve_call(message: str, registry: dict[str, _Entry]) -> Union[RoutedCall, list, None]:
    """Deterministic stage. Returns:
        * a RoutedCall          — exactly one drug matched
        * a list[str]           — >1 drug matched (ambiguous; the candidate ids)
        * None                  — no drug matched
    """
    folded = _norm(message)
    drugs = _match_drugs(folded, registry)
    if not drugs:
        return None
    if len(drugs) > 1:
        return drugs
    drug_id = drugs[0]
    slots = _extract_slots(folded, registry[drug_id].slots)
    return RoutedCall(tool="get_dose", drug_id=drug_id, slots=slots, via="deterministic")


# --------------------------------------------------------------------------- #
# The router                                                                  #
# --------------------------------------------------------------------------- #
_BOOL_SLOT_NAMES = set(_BOOL_SLOTS)
_NUM_SLOT_NAMES = set(_NUMERIC_SLOTS)


def _get_dose_tool(registry: dict[str, _Entry]) -> Tool:
    """The get_dose tool schema exposed to the LLM: drug_id constrained to the
    loaded protocols (closed enum), plus every known slot as an optional arg."""
    props: dict = {
        "drug_id": {
            "type": "string",
            "enum": sorted(registry.keys()),
            "description": "The protocol id of the antibiotic the user is asking about.",
        }
    }
    for s in sorted(_NUM_SLOT_NAMES):
        props[s] = {"type": "number"}
    for s in sorted(_BOOL_SLOT_NAMES):
        props[s] = {"type": "boolean"}
    return Tool(
        name="get_dose",
        description=("Return the approved renal/clinical dose for one antibiotic. "
                     "Set only the slots the user actually stated; never invent values."),
        parameters={"type": "object", "properties": props, "required": ["drug_id"]},
    )


class Router:
    def __init__(self, protocols_dir=None, provider=None, phrasing_provider=None):
        self.protocols_dir = Path(protocols_dir) if protocols_dir else DEFAULT_PROTOCOLS_DIR
        self.registry = _build_registry(self.protocols_dir)
        self.provider = provider
        # The phrasing model (PHRASING_MODEL) that rewrites verbatim tool text
        # into natural prose. Defaults to the routing provider, but is a separate
        # seam so a cheaper model can phrase. When None, answers stay verbatim
        # (render_dose) — this is the offline/free default check.sh relies on.
        self.phrasing_provider = phrasing_provider
        # Full drug-alias vocabulary, handed to the grounding verifier so a
        # phrasing that names a *different* antibiotic than the tool output is
        # caught (not just hallucinated numbers).
        self.known_drugs = tuple(sorted(
            {a for e in self.registry.values() for a in e.aliases}))
        self._tool = _get_dose_tool(self.registry)

    # public ---------------------------------------------------------------
    def tools(self) -> list[Tool]:
        return [self._tool]

    def route(self, message: str, *, provider=None, phrasing_provider=None) -> RouterResult:
        phraser = phrasing_provider or self.phrasing_provider
        # 1) deterministic stage
        resolved = resolve_call(message, self.registry)
        if isinstance(resolved, RoutedCall):
            return self._run_call(resolved, phrasing_provider=phraser)
        if isinstance(resolved, list):           # ambiguous → clarify, never pick
            return RouterResult(
                route="clarify", tool="ask_clarification", needs_clarification=True,
                candidates=resolved, via="deterministic",
                answer=("Which antibiotic do you mean: "
                        + ", ".join(resolved) + "?"))

        # 2) LLM stage (only if a provider is available)
        prov = provider or self.provider
        if prov is not None:
            llm = self._route_via_llm(message, prov, phrasing_provider=phraser)
            if llm is not None:
                return llm

        # 3) no silent answers
        return RouterResult(
            route="unsupported", tool="none",
            answer=("I don't have an uploaded protocol that covers that. "
                    "I can give renal/clinical dosing for the antibiotics in the "
                    "protocol set — name one (e.g. 'meropenem gfr 40')."))

    # internal -------------------------------------------------------------
    def _run_call(self, call: RoutedCall, *, phrasing_provider=None) -> RouterResult:
        if call.tool != "get_dose":
            return RouterResult(route="unsupported", tool="none",
                                answer=f"Tool {call.tool!r} is not available yet.")
        if call.drug_id not in self.registry:
            return RouterResult(
                route="unsupported", tool="none",
                answer=f"No uploaded protocol for {call.drug_id!r}.")
        res = _gd.get_dose(call.drug_id, protocols_dir=str(self.protocols_dir),
                           **call.slots)
        grounded = _gd.render_dose(res)
        result = RouterResult(
            route="drug_dose", tool="get_dose", protocol=res.drug_id,
            answer=grounded, grounded_answer=grounded,
            needs_clarification=res.needs_confirmation,
            via=call.via, slots=dict(call.slots), dose=res,
        )
        # Phrasing step, behind the grounding verifier (roadmap 4.1). Only runs
        # when a phrasing model is supplied AND we have a real dose to phrase
        # (never phrase an out-of-range "needs confirmation" prompt). The verifier
        # is hard-block for drug_dose: an ungrounded number/unit/drug → keep the
        # verbatim tool text. Any phrasing failure also falls back to verbatim.
        if phrasing_provider is not None and not res.needs_confirmation:
            self._phrase(result, grounded, "drug_dose", phrasing_provider)
        return result

    def _phrase(self, result: "RouterResult", grounded: str, kind: str,
                provider) -> None:
        """Rewrite `grounded` into natural prose, then verify it is grounded.
        Mutates `result.answer` in place; on block or failure leaves the
        verbatim text. Never raises."""
        try:
            candidate = provider.chat([
                {"role": "system", "content": _PHRASING_SYSTEM},
                {"role": "user", "content": grounded},
            ])
        except Exception:  # provider/network failure → keep verbatim, stay safe
            return
        if not candidate or not candidate.strip():
            return
        # Drug-name vocabulary for the verifier = every OTHER drug's aliases. We
        # exclude the answered drug's own aliases so a faithful paraphrase that
        # says e.g. "meropenem dosing" (a same-drug alias) is not falsely
        # blocked, while a switch to a *different* antibiotic still is.
        self_aliases = set(self.registry[result.protocol].aliases) if result.protocol in self.registry else set()
        other_drugs = tuple(a for a in self.known_drugs if a not in self_aliases)
        verdict = verify_grounding(candidate, grounded, kind,
                                   known_drugs=other_drugs)
        result.answer = verdict.text
        result.phrased = not verdict.blocked
        result.phrasing_blocked = verdict.blocked

    def _route_via_llm(self, message: str, provider, *, phrasing_provider=None) -> Optional[RouterResult]:
        messages = [
            {"role": "system", "content": _ROUTER_SYSTEM},
            {"role": "user", "content": message},
        ]
        try:
            out = provider.call_with_tools(messages, self.tools())
        except Exception as exc:  # provider/network failure → fall through to unsupported
            raise RouterError(f"LLM routing failed: {exc}") from exc
        if isinstance(out, ToolCall):
            problems = self._tool.validate_arguments(out.arguments)
            if out.name != "get_dose" or problems:
                return None
            args = dict(out.arguments)
            drug_id = args.pop("drug_id", None)
            if drug_id not in self.registry:
                return None
            # keep only slots the chosen protocol declares
            declared = set(self.registry[drug_id].slots)
            slots = {k: v for k, v in args.items() if k in declared}
            return self._run_call(RoutedCall(tool="get_dose", drug_id=drug_id,
                                             slots=slots, via="llm"),
                                  phrasing_provider=phrasing_provider)
        # the model answered with prose instead of a tool — not allowed (no
        # ungrounded free-text dosing); let the caller emit "unsupported".
        return None


_ROUTER_SYSTEM = (
    "You route messages for a hospital antibiotic dosing assistant. "
    "If the user asks for the dose of an antibiotic in the protocol set, call "
    "get_dose with its drug_id and ONLY the clinical slots the user explicitly "
    "stated (renal function, CRRT, IHD, CNS infection, low TDM level, septic "
    "shock, low albumin, body weight, vancomycin level, MIC). Never invent slot "
    "values. If the message is not about an antibiotic dose in the set, do not "
    "call any tool."
)


_PHRASING_SYSTEM = (
    "You rephrase an antibiotic dosing answer for a clinician into clear, "
    "concise prose (match the user's language: English or Hungarian). "
    "STRICT RULES: use ONLY the doses, numbers, units, drug names and conditions "
    "given in the message. Do NOT add, change, round, convert, or infer any dose, "
    "number, unit, frequency, or drug. Do not introduce clinical facts that are "
    "not in the message. Keep every numeric value and unit exactly as written. "
    "If the message is already clear, you may return it almost unchanged."
)


__all__ = ["Router", "RouterResult", "RoutedCall", "resolve_call", "RouterError"]
