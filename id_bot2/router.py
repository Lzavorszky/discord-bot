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
import interpret_pcr as _ip                       # noqa: E402
import select_pathway as _sp                     # noqa: E402
import get_table_dose as _td                     # noqa: E402
import calculate as _ca                           # noqa: E402
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
    pcr: object = None               # the PcrResult, when route == "pcr_panel"
    pathway: object = None           # the PathwayResult, when route == "pathway"
    table: object = None             # the TableDoseResult, when route == "table_lookup"
    calc: object = None              # the CalcResult, when route == "calculator"
    organisms: list = field(default_factory=list)  # detected organisms (pcr)
    markers: list = field(default_factory=list)    # detected markers (pcr)
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
# PCR panel registry + resolver (roadmap 3.2). The deterministic stage detects a
# named panel by its aliases, then extracts the detected organisms and resistance
# markers from the message using THAT panel's own vocabulary, and dispatches to
# interpret_pcr / list_panel. A bare genus the panel can't disambiguate is passed
# through so interpret_pcr asks which species (F5); panel listing is F6.        #
# --------------------------------------------------------------------------- #
_LIST_INTENT = ("panel list", "list panel", "panel contents", "what is on",
                "what's on", "what does it cover", "panel membership", "list")


@dataclass(frozen=True)
class _PanelEntry:
    panel_id: str
    aliases: tuple             # folded panel aliases, longest first
    organisms: tuple           # (folded_alias, canonical_name), longest first
    genera: tuple              # (folded_genus, genus_name)
    markers: tuple             # folded marker aliases, longest first


@dataclass(frozen=True)
class PanelCall:
    """A deterministic (or LLM) PCR routing decision."""
    tool: str                  # interpret_pcr | list_panel
    panel_id: str
    organisms: tuple = ()
    markers: tuple = ()
    via: str = "deterministic"


def _build_panel_registry(protocols_dir) -> dict:
    records = load_protocol_dir(protocols_dir)
    reg: dict = {}
    for _path, rec in records:
        if rec.get("kind") != "pcr_panel":
            continue
        pid = rec["id"]
        aliases = set(rec.get("aliases") or [])
        aliases.add(rec.get("canonical_name") or "")
        aliases.add(rec.get("source_label") or "")
        folded_aliases = tuple(sorted(
            {_norm(a) for a in aliases if a and a.strip()}, key=len, reverse=True))
        orgs = []
        for o in rec.get("organisms") or []:
            for n in [o.get("name", "")] + list(o.get("aliases") or []):
                f = _norm(n)
                if f:
                    orgs.append((f, o.get("name", "")))
        orgs.sort(key=lambda x: len(x[0]), reverse=True)
        genera = tuple((_norm(g.get("genus", "")), g.get("genus", ""))
                       for g in rec.get("disambiguate_genus") or [] if g.get("genus"))
        marks = set()
        for m in rec.get("markers") or []:
            for n in [m.get("name", "")] + list(m.get("aliases") or []):
                f = _norm(n)
                if f:
                    marks.add(f)
        reg[pid] = _PanelEntry(
            panel_id=pid, aliases=folded_aliases, organisms=tuple(orgs),
            genera=genera, markers=tuple(sorted(marks, key=len, reverse=True)))
    return reg


def _span_dedup(spans):
    """Drop any span strictly contained inside a longer one (compound beats part)."""
    kept = []
    for a in spans:
        contained = any(
            b is not a and b[0] <= a[0] and b[1] >= a[1] and (b[1] - b[0]) > (a[1] - a[0])
            for b in spans)
        if not contained:
            kept.append(a)
    kept.sort()
    return kept


def _match_panels(folded_msg: str, panels: dict) -> list:
    spans = []
    for pid, entry in panels.items():
        for alias in entry.aliases:        # longest alias first
            m = re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded_msg)
            if m:
                spans.append((m.start(), m.end(), pid))
                break
    kept = _span_dedup(spans)
    out, seen = [], set()
    for _s, _e, pid in kept:
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def _extract_organisms(folded_msg: str, entry: _PanelEntry) -> tuple:
    """Detected organisms = species-alias hits (containment-deduped), plus any
    bare genus present that no species span already covers (so interpret_pcr can
    disambiguate it)."""
    spans = []   # (start, end, canonical)
    for alias, canon in entry.organisms:      # longest alias first
        for m in re.finditer(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded_msg):
            spans.append((m.start(), m.end(), canon))
    kept = _span_dedup(spans)
    out, seen = [], set()
    for _s, _e, canon in kept:
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    # bare genera not covered by any kept species span
    covered = [(s, e) for s, e, _c in kept]
    for gfold, gname in entry.genera:
        for m in re.finditer(r"(?<!\w)" + re.escape(gfold) + r"(?!\w)", folded_msg):
            gs, ge = m.start(), m.end()
            if not any(s <= gs and e >= ge for s, e in covered):
                if gname not in out:
                    out.append(gname)
    return tuple(out)


def _extract_markers(folded_msg: str, entry: _PanelEntry) -> tuple:
    found, seen = [], set()
    for alias in entry.markers:               # longest alias first
        if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded_msg):
            if alias not in seen:
                seen.add(alias)
                found.append(alias)
    return tuple(found)


def resolve_pcr(message: str, panels: dict):
    """Deterministic PCR stage. Returns:
        * a PanelCall          — exactly one panel named
        * a list[str]          — >1 panel named (ambiguous; panel ids)
        * None                 — no panel alias present (let drug logic run)
    """
    folded = _norm(message)
    matched = _match_panels(folded, panels)
    if not matched:
        return None
    if len(matched) > 1:
        return matched
    pid = matched[0]
    entry = panels[pid]
    if any(kw in folded for kw in _LIST_INTENT):
        return PanelCall(tool="list_panel", panel_id=pid, via="deterministic")
    organisms = _extract_organisms(folded, entry)
    markers = _extract_markers(folded, entry)
    return PanelCall(tool="interpret_pcr", panel_id=pid,
                     organisms=organisms, markers=markers, via="deterministic")


def _organism_panel_hint(message: str, panels: dict) -> list:
    """For a message with NO panel alias and NO drug match: if it names an organism
    that belongs to a panel, return the panel ids so the router can ask which
    panel/source (F8 — never emit an unexplained recommendation). Only fires on a
    reasonably specific organism token (>= 5 chars) to avoid false positives."""
    folded = _norm(message)
    hits = set()
    for pid, entry in panels.items():
        for alias, _canon in entry.organisms:
            if len(alias) >= 5 and re.search(
                    r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded):
                hits.add(pid)
                break
    return sorted(hits)


# --------------------------------------------------------------------------- #
# Pathway registry + resolver (Plan D, Phase 2.5 cont.). A NAMED empiric/
# diagnostic pathway (CAP, UTI, SBP, cdiff, endocarditis, intra-abdominal) is a
# strong signal — like a named PCR panel — so it precedes drug resolution. The
# deterministic stage matches a pathway by its aliases, keyword-extracts the
# clinical selector slots from the message (only slots the matched pathway
# declares), and dispatches select_pathway. Slot extraction quality only affects
# WHICH output is surfaced; an unrecognised context simply falls through to the
# pathway's own DEFAULT_ANSWER (the source "quick map") — never a guessed pathway.
# --------------------------------------------------------------------------- #

# Pathway slot vocabulary, mirroring the drug _BOOL_SLOTS approach (kept in the
# router, not the YAML, exactly as the drug slot keywords are). Each entry maps a
# folded phrase -> the value to assign; ordered most-specific-first, first hit
# per slot wins. Extraction is gated to the slots the matched pathway declares,
# so e.g. patient_status is only read for CAP/UTI, pathogen_group only for
# endocarditis. Phrases are in _norm() form (accents/separators folded).
_PATHWAY_SLOT_VOCAB: dict[str, tuple[tuple[str, object], ...]] = {
    # shared (CAP, UTI, intra-abdominal where declared)
    "patient_status": (
        ("intubated", "intubated"), ("mechanically ventilated", "intubated"),
        ("ventilated", "intubated"),
        ("hospitalized", "hospitalized"), ("hospitalised", "hospitalized"),
        ("admitted", "hospitalized"), ("inpatient", "hospitalized"),
        ("hospitalizalt", "hospitalized"),
        ("dischargeable", "dischargeable"), ("outpatient", "dischargeable"),
        ("ambulant", "dischargeable"), ("hazaengedheto", "dischargeable"),
    ),
    "nosocomial_risk": (
        ("nosocomial", True), ("nozokomialis", True), ("icu", True), ("ito", True),
    ),
    # CAP
    "intubated": (("intubated", True), ("mechanically ventilated", True),
                  ("ventilated", True)),
    "influenza": (("influenza", True), ("flu", True)),
    "aspiration_event": (("aspiration", True), ("aspiratio", True)),
    "copd_exacerbation": (("copd", True),),
    "atypical_suspicion": (("atypical", True), ("atypusos", True)),
    "viral_test_result": (
        ("viral test positive", "positive"), ("viral positive", "positive"),
        ("viral test negative", "negative"), ("viral negative", "negative"),
    ),
    # UTI
    "syndrome_class": (
        ("asymptomatic bacteriuria", "asymptomatic_bacteriuria"),
        ("asymptomatic", "asymptomatic_bacteriuria"),
        ("uncomplicated", "uncomplicated_uti"), ("cystitis", "uncomplicated_uti"),
        ("complicated", "complicated_uti"), ("pyelonephritis", "complicated_uti"),
        ("prostatitis", "complicated_uti"),
    ),
    "asymptomatic_bacteriuria": (("asymptomatic bacteriuria", True),
                                 ("asymptomatic", True), ("bacteriuria", True)),
    "uncomplicated": (("uncomplicated", True), ("cystitis", True)),
    "complicated": (("complicated", True), ("pyelonephritis", True),
                    ("prostatitis", True)),
    "catheter_associated": (("catheter associated", True),
                            ("catheter-associated", True), ("ca uti", True),
                            ("catheter", True), ("kateter", True)),
    # cdiff
    "cdiff_request_type": (
        ("diagnosis", "diagnosis"), ("diagnostic", "diagnosis"),
        ("diagnosztika", "diagnosis"), ("diagnozis", "diagnosis"),
        ("toxin", "diagnosis"), ("antigen", "diagnosis"),
        ("treatment", "treatment"), ("therapy", "treatment"),
        ("kezeles", "treatment"),
    ),
    # intra-abdominal
    "iai_context": (
        ("clostridium difficile", "cdiff"), ("c difficile", "cdiff"),
        ("c diff", "cdiff"), ("cdiff", "cdiff"),
        ("spontaneous bacterial peritonitis", "sbp"),
        ("ascites infection", "sbp"), ("sbp", "sbp"), ("peritonitis", "sbp"),
        ("splenectomy", "splenectomy_prophylaxis"),
        ("asplenia", "splenectomy_prophylaxis"),
        ("variceal", "varix_bleeding_prophylaxis"),
        ("varix", "varix_bleeding_prophylaxis"),
        ("pancreatitis", "pancreatitis"),
        ("anastomotic leak", "complex_nosocomial"),
        ("complex nosocomial", "complex_nosocomial"),
        ("reoperation", "complex_nosocomial"),
        ("source control", "hospitalized_source_control"),
        ("perforation", "hospitalized_source_control"),
        ("ileus", "hospitalized_source_control"),
        ("diverticulosis", "dischargeable"),
        ("dischargeable", "dischargeable"), ("outpatient", "dischargeable"),
    ),
    # endocarditis
    "treatment_mode": (("empiric", "empiric"), ("empirical", "empiric"),
                       ("targeted", "targeted")),
    "pathogen_group": (
        ("staphylococcus aureus", "staphylococcus_aureus"),
        ("staph aureus", "staphylococcus_aureus"),
        ("s aureus", "staphylococcus_aureus"),
        ("mrsa", "mrsa"), ("mssa", "mssa"),
        ("enterococcus", "enterococcus"),
        ("vre", "vre"),
        ("streptococcus", "streptococcus"), ("strep", "streptococcus"),
        ("pneumococcus", "streptococcus"), ("pneumoniae", "streptococcus"),
    ),
    "resistance_profile": (
        ("beta lactam sensitive", "beta_lactam_sensitive"),
        ("beta lactam resistant", "beta_lactam_resistant_not_vre"),
        ("mrsa", "mrsa"), ("mssa", "mssa"), ("vre", "vre"),
    ),
    "pve_timing": (("early pve", "early"), ("late pve", "late")),
    "valve_context": (
        ("early pve", "early_pve"),
        ("native valve", "nve"), ("nve", "nve"),
        ("prosthetic valve", "pve"), ("pve", "pve"),
    ),
    "penicillin_allergy": (("penicillin allergy", True),
                           ("beta lactam allergy", True),
                           ("penicillin allergic", True),
                           ("penicillin allergia", True)),
    "unsupported_topic": (
        ("culture negative", "culture_negative"),
        ("blood culture negative", "culture_negative"),
        ("fungal", "fungal"), ("candida", "fungal"),
        ("opat", "opat"), ("oral step down", "opat"),
    ),
}


@dataclass(frozen=True)
class _PathwayEntry:
    pathway_id: str
    aliases: tuple            # folded pathway aliases, longest first
    slots: tuple              # declared slot names


@dataclass(frozen=True)
class PathwayCall:
    """A deterministic (or LLM) pathway routing decision."""
    tool: str                 # select_pathway
    pathway_id: str
    slots: dict = field(default_factory=dict)
    via: str = "deterministic"


def _build_pathway_registry(protocols_dir) -> dict:
    records = load_protocol_dir(protocols_dir)
    reg: dict = {}
    for _path, rec in records:
        if rec.get("kind") != "pathway":
            continue
        pid = rec["id"]
        aliases = set(rec.get("aliases") or [])
        aliases.add(rec.get("canonical_name") or "")
        aliases.add(rec.get("source_label") or "")
        folded = tuple(sorted(
            {_norm(a) for a in aliases if a and a.strip()}, key=len, reverse=True))
        reg[pid] = _PathwayEntry(
            pathway_id=pid, aliases=folded,
            slots=tuple((rec.get("slots") or {}).keys()))
    return reg


def _match_pathways(folded_msg: str, pathways: dict) -> list:
    spans = []
    for pid, entry in pathways.items():
        for alias in entry.aliases:        # longest alias first
            m = re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded_msg)
            if m:
                spans.append((m.start(), m.end(), pid))
                break
    kept = _span_dedup(spans)
    out, seen = [], set()
    for _s, _e, pid in kept:
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def _extract_pathway_slots(folded_msg: str, declared) -> dict:
    """Keyword-extract only the slots the matched pathway declares. First
    matching phrase per slot wins (vocab is ordered specific-first)."""
    out: dict = {}
    for slot in declared:
        vocab = _PATHWAY_SLOT_VOCAB.get(slot)
        if not vocab:
            continue
        for phrase, value in vocab:
            if _alias_hit(folded_msg, phrase):
                out[slot] = value
                break
    # Endocarditis timing/valve disambiguation: when the message pins PVE timing
    # (early/late PVE), don't ALSO set valve_context=pve — the timing rungs
    # (EMPIRIC_EARLY_PVE / EMPIRIC_NVE_LATE_PVE) must win over the
    # timing-unknown EMPIRIC_PVE_TIMING_OPTIONS rung.
    if out.get("pve_timing") in ("early", "late") and out.get("valve_context") == "pve":
        del out["valve_context"]
    return out


def resolve_pathway(message: str, pathways: dict):
    """Deterministic pathway stage. Returns:
        * a PathwayCall         — exactly one pathway named
        * a list[str]           — >1 pathway named (ambiguous; pathway ids)
        * None                  — no pathway alias present (let drug logic run)
    """
    folded = _norm(message)
    matched = _match_pathways(folded, pathways)
    if not matched:
        return None
    if len(matched) > 1:
        return matched
    pid = matched[0]
    slots = _extract_pathway_slots(folded, pathways[pid].slots)
    return PathwayCall(tool="select_pathway", pathway_id=pid, slots=slots,
                       via="deterministic")


def _get_pathway_tools(pathways: dict) -> list:
    """The select_pathway tool exposed to the LLM: pathway_id constrained to the
    loaded pathway protocols (closed enum), plus the union of pathway slot names
    as optional args. select_pathway ignores undeclared/invalid slots, so an
    off-pathway slot is never used."""
    if not pathways:
        return []
    bool_like = {"intubated", "influenza", "aspiration_event", "copd_exacerbation",
                 "nosocomial_risk", "atypical_suspicion", "asymptomatic_bacteriuria",
                 "uncomplicated", "complicated", "catheter_associated",
                 "penicillin_allergy"}
    props: dict = {
        "pathway_id": {"type": "string", "enum": sorted(pathways.keys()),
                       "description": "Which empiric/diagnostic pathway the "
                                      "question is about."},
    }
    all_slots: set = set()
    for entry in pathways.values():
        all_slots.update(entry.slots)
    for s in sorted(all_slots):
        props[s] = {"type": "boolean"} if s in bool_like else {"type": "string"}
    tool = Tool(
        name="select_pathway",
        description=("Select the empiric/diagnostic treatment pathway output for "
                     "a clinical syndrome (CAP, UTI, SBP, C. difficile, "
                     "endocarditis, intra-abdominal infection). Set only the "
                     "context slots the user actually stated; never invent values. "
                     "This selects a pathway, it does NOT dose drugs."),
        parameters={"type": "object", "properties": props,
                    "required": ["pathway_id"]},
    )
    return [tool]


# --------------------------------------------------------------------------- #
# table_lookup registry + resolver (Plan D, final migration phase). A named
# table_lookup protocol (today only tmpsmx / TMP-SMX) is a strong, drug-like
# signal — its aliases never collide with the other kinds — so it is resolved in
# the drug stage, AHEAD of the generic drug_dose resolver. The whole user message
# is handed to the tool as the free-text `indication`; the tool's own
# indication_rules classify it (so "PCP treatment" -> HIGH_DOSE, "PCP
# prophylaxis" -> PROPHYLAXIS — the F3/F4 fix). gfr / body_weight / crrt / ihd are
# keyword-extracted with the SAME drug-slot vocabulary.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _TableEntry:
    table_id: str
    aliases: tuple            # folded aliases, longest first
    slots: tuple              # declared slot names


@dataclass(frozen=True)
class TableCall:
    """A deterministic (or LLM) table_lookup routing decision."""
    tool: str                 # get_table_dose
    table_id: str
    slots: dict = field(default_factory=dict)
    via: str = "deterministic"


def _build_table_registry(protocols_dir) -> dict:
    records = load_protocol_dir(protocols_dir)
    reg: dict = {}
    for _path, rec in records:
        if rec.get("kind") != "table_lookup":
            continue
        tid = rec["id"]
        aliases = set(rec.get("aliases") or [])
        aliases.add(tid)
        aliases.add(rec.get("canonical_name") or "")
        aliases.add(rec.get("source_label") or "")
        folded = tuple(sorted(
            {_norm(a) for a in aliases if a and a.strip()}, key=len, reverse=True))
        reg[tid] = _TableEntry(
            table_id=tid, aliases=folded,
            slots=tuple((rec.get("slots") or {}).keys()))
    return reg


def _match_tables(folded_msg: str, tables: dict) -> list:
    spans = []
    for tid, entry in tables.items():
        for alias in entry.aliases:        # longest alias first
            m = re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded_msg)
            if m:
                spans.append((m.start(), m.end(), tid))
                break
    kept = _span_dedup(spans)
    out, seen = [], set()
    for _s, _e, tid in kept:
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def resolve_table(message: str, tables: dict):
    """Deterministic table_lookup stage. Returns:
        * a TableCall          — exactly one table_lookup protocol named
        * a list[str]          — >1 named (ambiguous; ids)
        * None                 — none named (let pathway/drug logic run)
    """
    folded = _norm(message)
    matched = _match_tables(folded, tables)
    if not matched:
        return None
    if len(matched) > 1:
        return matched
    tid = matched[0]
    declared = tables[tid].slots
    slots = _extract_slots(folded, declared)         # gfr/weight/crrt/ihd
    # The whole (mojibake-repaired) message is the free-text indication the tool
    # classifies. Passing the raw message is safe: alias tokens never match an
    # indication keyword.
    slots["indication"] = repair_mojibake(message or "")
    return TableCall(tool="get_table_dose", table_id=tid, slots=slots,
                     via="deterministic")


def _get_table_tools(tables: dict) -> list:
    """The get_table_dose tool exposed to the LLM: table_id constrained to the
    loaded table_lookup protocols (closed enum), plus the dosing slots. The model
    fills `indication` with the clinical indication text; the tool classifies it."""
    if not tables:
        return []
    tool = Tool(
        name="get_table_dose",
        description=("Return the approved dose for a 2-D table-lookup protocol "
                     "(TMP/SMX). Pass `indication` as the clinical indication text "
                     "the user stated (e.g. 'PCP treatment', 'PCP prophylaxis', "
                     "'severe CNS infection'); set body_weight_kg / gfr / crrt / "
                     "ihd only if the user stated them. Never invent values."),
        parameters={
            "type": "object",
            "properties": {
                "table_id": {"type": "string", "enum": sorted(tables.keys()),
                             "description": "Which table-lookup protocol."},
                "indication": {"type": "string",
                               "description": "The clinical indication, verbatim."},
                "body_weight_kg": {"type": "number"},
                "gfr": {"type": "number"},
                "crrt": {"type": "boolean"},
                "ihd": {"type": "boolean"},
            },
            "required": ["table_id"],
        },
    )
    return [tool]


# --------------------------------------------------------------------------- #
# calculator registry + resolver (Plan D, final migration phase). A named
# calculator (body size / steroid equivalence) is a strong, specific signal —
# its aliases never collide with the drug/panel/pathway sets — so it is resolved
# AHEAD of the generic drug stage. Numeric/enum slots are keyword-extracted from
# the message (unit-anchored: "170 cm", "70 kg", "6 mg", plus the steroid name);
# an incomplete extraction simply yields the tool's verbatim missing-input ask,
# never a guessed value. The tool itself does the (declared, AST-evaluated)
# arithmetic — the router only selects which calculator to run.
# --------------------------------------------------------------------------- #
# Unit-anchored numeric extractors, keyed by the slot's declared `unit`.
_CALC_UNIT_RE = {
    "cm": re.compile(r"(?P<v>\d+(?:[.,]\d+)?)\s*cm\b"),
    "kg": re.compile(r"(?P<v>\d+(?:[.,]\d+)?)\s*kg\b"),
    "mg": re.compile(r"(?P<v>\d+(?:[.,]\d+)?)\s*mg\b"),
}
# Keyword fallbacks (used only when the unit-anchored pattern misses).
_CALC_KEYWORD_RE = {
    "height_cm": re.compile(r"\bheight\b[\s:=]*(?P<v>\d+(?:[.,]\d+)?)"),
    "actual_weight_kg": re.compile(r"\b(?:body ?weight|weight|wt)\b[\s:=]*(?P<v>\d+(?:[.,]\d+)?)"),
    "steroid_dose_mg": re.compile(r"\b(?:dose|adag|dozis)\b[\s:=]*(?P<v>\d+(?:[.,]\d+)?)"),
}
# Steroid-name synonyms → the canonical enum value (kept small + safe).
_STEROID_SYNONYMS = {
    "dexa": "dexamethasone",
    "methylprednisolone": "methylprednisone",
    "medrol": "methylprednisone",
}


@dataclass(frozen=True)
class _CalcEntry:
    calc_id: str
    aliases: tuple            # folded aliases, longest first
    slots: tuple              # declared slot names
    specs: dict               # slot name -> spec (type/unit/values)


@dataclass(frozen=True)
class CalcCall:
    """A deterministic (or LLM) calculator routing decision."""
    tool: str                 # calculate
    calc_id: str
    slots: dict = field(default_factory=dict)
    via: str = "deterministic"


def _build_calc_registry(protocols_dir) -> dict:
    records = load_protocol_dir(protocols_dir)
    reg: dict = {}
    for _path, rec in records:
        if rec.get("kind") != "calculator":
            continue
        cid = rec["id"]
        aliases = set(rec.get("aliases") or [])
        aliases.add(cid)
        aliases.add(rec.get("canonical_name") or "")
        aliases.add(rec.get("source_label") or "")
        folded = tuple(sorted(
            {_norm(a) for a in aliases if a and a.strip()}, key=len, reverse=True))
        specs = dict(rec.get("slots") or {})
        reg[cid] = _CalcEntry(
            calc_id=cid, aliases=folded, slots=tuple(specs.keys()), specs=specs)
    return reg


def _match_calcs(folded_msg: str, calcs: dict) -> list:
    spans = []
    for cid, entry in calcs.items():
        for alias in entry.aliases:        # longest alias first
            m = re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded_msg)
            if m:
                spans.append((m.start(), m.end(), cid))
                break
    kept = _span_dedup(spans)
    out, seen = [], set()
    for _s, _e, cid in kept:
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def _num(val: str):
    n = float(val.replace(",", "."))
    return int(n) if n.is_integer() else n


def _extract_calc_slots(folded_msg: str, specs: dict) -> dict:
    """Pull only this calculator's declared slots from the message. Numeric slots
    are unit-anchored (then keyword fallback); enum slots match their declared
    values (+ a small synonym map). Anything not found is simply absent → the tool
    asks for it. Never guesses."""
    out: dict = {}
    for name, spec in specs.items():
        stype = spec.get("type")
        if stype == "number":
            unit = spec.get("unit")
            m = _CALC_UNIT_RE[unit].search(folded_msg) if unit in _CALC_UNIT_RE else None
            if not m and name in _CALC_KEYWORD_RE:
                m = _CALC_KEYWORD_RE[name].search(folded_msg)
            if m:
                out[name] = _num(m.group("v"))
        elif stype == "enum":
            values = spec.get("values") or spec.get("enum") or []
            # exact value token first (longest first to avoid partial hits)
            for val in sorted(values, key=len, reverse=True):
                if _alias_hit(folded_msg, _norm(val)):
                    out[name] = val
                    break
            else:
                for syn, canon in _STEROID_SYNONYMS.items():
                    if canon in values and _alias_hit(folded_msg, syn):
                        out[name] = canon
                        break
    return out


def resolve_calculator(message: str, calcs: dict):
    """Deterministic calculator stage. Returns:
        * a CalcCall          — exactly one calculator named
        * a list[str]         — >1 named (ambiguous; ids)
        * None                — none named (let drug logic run)
    """
    folded = _norm(message)
    matched = _match_calcs(folded, calcs)
    if not matched:
        return None
    if len(matched) > 1:
        return matched
    cid = matched[0]
    slots = _extract_calc_slots(folded, calcs[cid].specs)
    return CalcCall(tool="calculate", calc_id=cid, slots=slots, via="deterministic")


def _get_calc_tools(calcs: dict) -> list:
    """The calculate tool exposed to the LLM: calculator_id constrained to the
    loaded calculator protocols (closed enum), plus their union of slots. The
    model fills only slots the user actually stated; the tool computes the rest
    from its own declared formulas (and asks for anything missing)."""
    if not calcs:
        return []
    props = {
        "calculator_id": {"type": "string", "enum": sorted(calcs.keys()),
                           "description": "Which calculator protocol."},
    }
    for entry in calcs.values():
        for name, spec in entry.specs.items():
            if name in props:
                continue
            if spec.get("type") == "number":
                props[name] = {"type": "number"}
            elif spec.get("type") == "enum":
                props[name] = {"type": "string",
                               "enum": list(spec.get("values") or spec.get("enum") or [])}
            else:
                props[name] = {"type": "string"}
    tool = Tool(
        name="calculate",
        description=("Run an explicit-formula clinical calculator (body size: "
                     "BMI/BSA/IBW/adjusted weight; steroid dose equivalence). Set "
                     "only the inputs the user stated (e.g. height_cm, "
                     "actual_weight_kg, or steroid_agent + steroid_dose_mg); never "
                     "invent values. The tool computes from its declared formulas."),
        parameters={"type": "object", "properties": props,
                    "required": ["calculator_id"]},
    )
    return [tool]


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


def _get_pcr_tools(panels: dict) -> list:
    """The two PCR tools exposed to the LLM, with panel_id constrained to the
    loaded pcr_panel protocols (closed enum). organisms/markers are free-text
    arrays the model fills from the detected result; interpret_pcr re-validates
    them against the panel's own vocabulary, so an off-panel string is reported,
    never silently mapped."""
    if not panels:
        return []
    panel_ids = sorted(panels.keys())
    interpret = Tool(
        name="interpret_pcr",
        description=("Interpret a BioFire/PCR panel result. Pass the detected "
                     "organism names and any resistance markers EXACTLY as the "
                     "user stated them; never invent organisms or markers."),
        parameters={
            "type": "object",
            "properties": {
                "panel_id": {"type": "string", "enum": panel_ids,
                             "description": "Which PCR panel the result is from."},
                "organisms": {"type": "array", "items": {"type": "string"},
                              "description": "Detected organism names, verbatim."},
                "markers": {"type": "array", "items": {"type": "string"},
                            "description": "Detected resistance markers, verbatim."},
            },
            "required": ["panel_id"],
        },
    )
    listing = Tool(
        name="list_panel",
        description="List the organisms a PCR panel covers (no recommendation).",
        parameters={
            "type": "object",
            "properties": {
                "panel_id": {"type": "string", "enum": panel_ids},
            },
            "required": ["panel_id"],
        },
    )
    return [interpret, listing]


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
        # PCR panels (roadmap 3.2): a parallel registry + tool schemas.
        self.panels = _build_panel_registry(self.protocols_dir)
        self._pcr_tools = _get_pcr_tools(self.panels)
        # Pathway protocols (Phase 2.5 cont.): a parallel registry + tool schema.
        self.pathways = _build_pathway_registry(self.protocols_dir)
        self._pathway_tools = _get_pathway_tools(self.pathways)
        # table_lookup protocols (final migration phase): parallel registry + tool.
        self.tables = _build_table_registry(self.protocols_dir)
        self._table_tools = _get_table_tools(self.tables)
        # calculator protocols (final migration phase): parallel registry + tool.
        self.calcs = _build_calc_registry(self.protocols_dir)
        self._calc_tools = _get_calc_tools(self.calcs)

    # public ---------------------------------------------------------------
    def tools(self) -> list[Tool]:
        return [self._tool, *self._pcr_tools, *self._pathway_tools,
                *self._table_tools, *self._calc_tools]

    def route(self, message: str, *, provider=None, phrasing_provider=None) -> RouterResult:
        phraser = phrasing_provider or self.phrasing_provider
        # 1) deterministic PCR stage — a NAMED panel is a strong signal, so it
        #    takes precedence over drug resolution.
        pcr = resolve_pcr(message, self.panels)
        if isinstance(pcr, PanelCall):
            return self._run_pcr_call(pcr)
        if isinstance(pcr, list):                # >1 panel named → clarify
            return RouterResult(
                route="clarify", tool="ask_clarification", needs_clarification=True,
                candidates=pcr, via="deterministic",
                answer=("Which panel do you mean: " + ", ".join(pcr) + "?"))

        # 1b) deterministic TABLE_LOOKUP stage — a named table_lookup protocol
        #     (tmpsmx / TMP-SMX) is an explicit, specific drug-dose request. Its
        #     free-text indication often contains a clinical-syndrome word (e.g.
        #     "PCP pneumonia") that would otherwise trip a pathway alias, so a
        #     table_lookup explicitly named by the user wins over an incidental
        #     pathway keyword. (A pathway query never names a tmpsmx alias.)
        #     Precedence: named panel > named table_lookup > named pathway > drug.
        tbl = resolve_table(message, self.tables)
        if isinstance(tbl, TableCall):
            return self._run_table_call(tbl)
        if isinstance(tbl, list):                # >1 table named → clarify
            return RouterResult(
                route="clarify", tool="ask_clarification", needs_clarification=True,
                candidates=tbl, via="deterministic",
                answer=("Which protocol do you mean: " + ", ".join(tbl) + "?"))

        # 1c) deterministic PATHWAY stage — a NAMED empiric/diagnostic pathway
        #     is a strong signal too (like a named panel), so it precedes drug
        #     resolution. (A drug query never matches a pathway alias, so real
        #     dosing requests still fall through to the drug stage below.)
        pw = resolve_pathway(message, self.pathways)
        if isinstance(pw, PathwayCall):
            return self._run_pathway_call(pw)
        if isinstance(pw, list):                 # >1 pathway named → clarify
            return RouterResult(
                route="clarify", tool="ask_clarification", needs_clarification=True,
                candidates=pw, via="deterministic",
                answer=("Which pathway do you mean: " + ", ".join(pw) + "?"))

        # 1d) deterministic CALCULATOR stage — a NAMED calculator (body size /
        #     steroid equivalence) is a strong, specific signal whose aliases
        #     never collide with the drug set, so it precedes drug resolution.
        #     Precedence: panel > table_lookup > pathway > calculator > drug.
        calc = resolve_calculator(message, self.calcs)
        if isinstance(calc, CalcCall):
            return self._run_calc_call(calc)
        if isinstance(calc, list):               # >1 calculator named → clarify
            return RouterResult(
                route="clarify", tool="ask_clarification", needs_clarification=True,
                candidates=calc, via="deterministic",
                answer=("Which calculator do you mean: " + ", ".join(calc) + "?"))


        # 2) deterministic drug stage
        resolved = resolve_call(message, self.registry)
        if isinstance(resolved, RoutedCall):
            return self._run_call(resolved, phrasing_provider=phraser)
        if isinstance(resolved, list):           # ambiguous → clarify, never pick
            return RouterResult(
                route="clarify", tool="ask_clarification", needs_clarification=True,
                candidates=resolved, via="deterministic",
                answer=("Which antibiotic do you mean: "
                        + ", ".join(resolved) + "?"))

        # 3) LLM stage (only if a provider is available)
        prov = provider or self.provider
        if prov is not None:
            llm = self._route_via_llm(message, prov, phrasing_provider=phraser)
            if llm is not None:
                return llm

        # 4) bare organism with no panel/drug → ask which panel/source (F8),
        #    never an unexplained recommendation.
        hint = _organism_panel_hint(message, self.panels)
        if hint:
            opts = ", ".join(hint)
            return RouterResult(
                route="clarify", tool="ask_clarification", needs_clarification=True,
                candidates=hint, via="deterministic",
                answer=("That organism is on a PCR panel. Which panel/source is "
                        f"this result from: {opts}? (Send it as a PCR result.)"))

        # 5) no silent answers
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

    def _run_pcr_call(self, call: "PanelCall") -> RouterResult:
        if call.panel_id not in self.panels:
            return RouterResult(route="unsupported", tool="none",
                                answer=f"No uploaded panel for {call.panel_id!r}.")
        pdir = str(self.protocols_dir)
        if call.tool == "list_panel":
            res = _ip.list_panel(call.panel_id, protocols_dir=pdir)
        else:
            res = _ip.interpret_pcr(call.panel_id, organisms=list(call.organisms),
                                    markers=list(call.markers), protocols_dir=pdir)
        grounded = _ip.render_pcr(res)
        return RouterResult(
            route="pcr_panel", tool=call.tool, protocol=res.panel_id,
            answer=grounded, grounded_answer=grounded,
            needs_clarification=bool(res.needs_clarification or res.needs_input),
            via=call.via, organisms=list(call.organisms),
            markers=list(call.markers), pcr=res)

    def _run_pathway_call(self, call: "PathwayCall") -> RouterResult:
        if call.pathway_id not in self.pathways:
            return RouterResult(route="unsupported", tool="none",
                                answer=f"No uploaded pathway for {call.pathway_id!r}.")
        res = _sp.select_pathway(call.pathway_id,
                                 protocols_dir=str(self.protocols_dir),
                                 **call.slots)
        grounded = _sp.render_pathway(res)
        # A pathway always returns SOME verbatim guidance (a matched output or
        # the source DEFAULT_ANSWER quick map), so this is never a silent answer.
        return RouterResult(
            route="pathway", tool="select_pathway", protocol=res.pathway_id,
            answer=grounded, grounded_answer=grounded,
            needs_clarification=False, via=call.via,
            slots=dict(call.slots), pathway=res)

    def _run_table_call(self, call: "TableCall") -> RouterResult:
        if call.table_id not in self.tables:
            return RouterResult(route="unsupported", tool="none",
                                answer=f"No uploaded protocol for {call.table_id!r}.")
        res = _td.get_table_dose(call.table_id,
                                 protocols_dir=str(self.protocols_dir),
                                 **call.slots)
        grounded = _td.render_table_dose(res)
        # A table_lookup always returns SOME verbatim guidance (a selected dose,
        # a verbatim renal warning, or the source default_answer / missing_inputs
        # ask), so this is never a silent answer.
        return RouterResult(
            route="table_lookup", tool="get_table_dose", protocol=res.table_id,
            answer=grounded, grounded_answer=grounded,
            needs_clarification=bool(res.needs_input or res.needs_confirmation),
            via=call.via, slots=dict(call.slots), table=res)

    def _run_calc_call(self, call: "CalcCall") -> RouterResult:
        if call.calc_id not in self.calcs:
            return RouterResult(route="unsupported", tool="none",
                                answer=f"No uploaded calculator for {call.calc_id!r}.")
        res = _ca.calculate(call.calc_id, protocols_dir=str(self.protocols_dir),
                            **call.slots)
        grounded = _ca.render_calc(res)
        # A calculator always returns SOME text (a computed answer, a verbatim
        # default_answer / missing_inputs ask, an out-of-range confirmation, or
        # the unsupported_value message), so this is never a silent answer.
        return RouterResult(
            route="calculator", tool="calculate", protocol=res.calculator_id,
            answer=grounded, grounded_answer=grounded,
            needs_clarification=bool(res.needs_input or res.needs_confirmation),
            via=call.via, slots=dict(call.slots), calc=res)

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
            # PCR tools (interpret_pcr / list_panel).
            if out.name in ("interpret_pcr", "list_panel"):
                tool = next((t for t in self._pcr_tools if t.name == out.name), None)
                if tool is None or tool.validate_arguments(out.arguments):
                    return None
                args = dict(out.arguments)
                panel_id = args.get("panel_id")
                if panel_id not in self.panels:
                    return None
                if out.name == "list_panel":
                    return self._run_pcr_call(PanelCall(tool="list_panel",
                                                        panel_id=panel_id, via="llm"))
                orgs = tuple(args.get("organisms") or ())
                marks = tuple(args.get("markers") or ())
                return self._run_pcr_call(PanelCall(
                    tool="interpret_pcr", panel_id=panel_id,
                    organisms=orgs, markers=marks, via="llm"))
            # select_pathway (Phase 2.5 cont.).
            if out.name == "select_pathway":
                tool = next((t for t in self._pathway_tools if t.name == out.name), None)
                if tool is None or tool.validate_arguments(out.arguments):
                    return None
                args = dict(out.arguments)
                pathway_id = args.pop("pathway_id", None)
                if pathway_id not in self.pathways:
                    return None
                declared = set(self.pathways[pathway_id].slots)
                slots = {k: v for k, v in args.items() if k in declared}
                return self._run_pathway_call(PathwayCall(
                    tool="select_pathway", pathway_id=pathway_id,
                    slots=slots, via="llm"))
            # calculate (calculator; body size / steroid equivalence).
            if out.name == "calculate":
                tool = next((t for t in self._calc_tools if t.name == out.name), None)
                if tool is None or tool.validate_arguments(out.arguments):
                    return None
                args = dict(out.arguments)
                calc_id = args.pop("calculator_id", None)
                if calc_id not in self.calcs:
                    return None
                declared = set(self.calcs[calc_id].slots)
                slots = {k: v for k, v in args.items() if k in declared}
                return self._run_calc_call(CalcCall(
                    tool="calculate", calc_id=calc_id, slots=slots, via="llm"))
            # get_table_dose (table_lookup; tmpsmx).
            if out.name == "get_table_dose":
                tool = next((t for t in self._table_tools if t.name == out.name), None)
                if tool is None or tool.validate_arguments(out.arguments):
                    return None
                args = dict(out.arguments)
                table_id = args.pop("table_id", None)
                if table_id not in self.tables:
                    return None
                allowed = {"indication", "body_weight_kg", "gfr", "crrt", "ihd"}
                slots = {k: v for k, v in args.items() if k in allowed}
                return self._run_table_call(TableCall(
                    tool="get_table_dose", table_id=table_id, slots=slots, via="llm"))
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


# --------------------------------------------------------------------------- #
# Safety rules (roadmap 4.2). Ported from the legacy ``system_rules.txt`` so the
# rebuilt prompts carry the SAME guarantees the old bot stated:
#   * Scope        — answer only from the uploaded protocols / tool output; no
#                    outside medical knowledge or memory; never invent facts.
#   * Patient data — never request identifiers; ignore + remind if any appear.
#   * Conflicts    — state the conflict, don't resolve it from outside knowledge,
#                    recommend senior clinician review.
#   * Role         — protocol-based decision support, not autonomous prescribing.
# ONE shared constant, injected into BOTH the router and the phrasing/answerer
# prompts, so the safety language cannot drift between the two LLM seams. These
# prompt rules *reinforce* the deterministic invariants (no silent answer,
# verbatim tool text, closed drug enum, ambiguity → clarify) — they never
# replace them; the engine, not the prompt, is the real backstop.
# --------------------------------------------------------------------------- #
_SAFETY_RULES = (
    "SAFETY RULES (always apply; never override):\n"
    "- Scope: answer ONLY from the uploaded hospital protocols and the tool "
    "output you are given. Do NOT use outside medical knowledge or memory, and "
    "do NOT invent or guess doses, drugs, indications, contraindications, "
    "monitoring, alternatives, or durations.\n"
    "- Patient data: never request patient identifiers; if any identifiable "
    "patient data (e.g. name, MRN, date of birth, address) is present, ignore it "
    "and remind the user not to include identifiable patient data.\n"
    "- Conflicts: if the protocol text conflicts or its logic is unclear, state "
    "the conflict briefly, do NOT resolve it from outside knowledge, and "
    "recommend senior clinician review.\n"
    "- Role: this is protocol-based decision support, not autonomous prescribing; "
    "the treating clinician remains responsible for the final decision."
)


_ROUTER_SYSTEM = (
    _SAFETY_RULES + "\n\n"
    "You route messages for a hospital antibiotic dosing assistant. "
    "If the user asks for the dose of an antibiotic in the protocol set, call "
    "get_dose with its drug_id and ONLY the clinical slots the user explicitly "
    "stated (renal function, CRRT, IHD, CNS infection, low TDM level, septic "
    "shock, low albumin, body weight, vancomycin level, MIC). Never invent slot "
    "values. If the message is not about an antibiotic dose in the set, do not "
    "call any tool."
)


_PHRASING_SYSTEM = (
    _SAFETY_RULES + "\n\n"
    "You rephrase an antibiotic dosing answer for a clinician into clear, "
    "concise prose (match the user's language: English or Hungarian). "
    "STRICT RULES: use ONLY the doses, numbers, units, drug names and conditions "
    "given in the message. Do NOT add, change, round, convert, or infer any dose, "
    "number, unit, frequency, or drug. Do not introduce clinical facts that are "
    "not in the message. Keep every numeric value and unit exactly as written. "
    "If the message is already clear, you may return it almost unchanged."
)

# Public aliases — used by the safety-parity tests and any future answerer
# wiring that needs to reuse the exact same prompt text.
SAFETY_RULES = _SAFETY_RULES
ROUTER_SYSTEM = _ROUTER_SYSTEM
PHRASING_SYSTEM = _PHRASING_SYSTEM


__all__ = [
    "Router", "RouterResult", "RoutedCall", "resolve_call", "RouterError",
    "PanelCall", "resolve_pcr",
    "PathwayCall", "resolve_pathway",
    "TableCall", "resolve_table",
    "SAFETY_RULES", "ROUTER_SYSTEM", "PHRASING_SYSTEM",
]
