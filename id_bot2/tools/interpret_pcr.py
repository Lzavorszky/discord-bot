#!/usr/bin/env python3
"""interpret_pcr.py — the pcr_panel interpretation tool (Plan D, roadmap 3.2).

Given a migrated ``pcr_panel`` protocol (a BioFire-style organism panel) and the
organisms / resistance markers the user reports as detected, return the panel's
own **verbatim** selected-output text. Like ``get_dose``, this tool NEVER
composes a novel clinical recommendation — it only ever SELECTS among the answer
strings already approved in the protocol file.

What it fixes (the logged failures)
------------------------------------
* **F5** — a bare genus (e.g. "Klebsiella") is not silently mapped to one
  species; the tool returns a *disambiguation* asking which species, listing the
  panel's candidate rows. An organism is never dropped.
* **F6** — ``list_panel`` returns the panel contents (organisms), not a
  recommendation.
* **F7** — every panel organism (including influenza) is modelled as structured
  data, so membership is explicit; an on-panel organism is never reported as
  "not on panel".
* **F8** — an organism that is genuinely not on the named panel is reported as
  such (an explicit, grounded "not on this panel"), never an unexplained pick.

Safety invariants
-----------------
* **At least one pathogen is required.** No organism → the panel's verbatim
  ``default_answer`` (never a therapy).
* **Markers need a pathogen.** A resistance marker with no organism → the
  verbatim ``marker_without_pathogen`` ask.
* **Verbatim only.** Every emitted clinical string is copied from the protocol.
* **No dosing from the panel.** The panel selects an agent; dosing is forwarded
  to a ``drug_dose`` protocol elsewhere (``dose_via``). This tool emits no dose.

Public API
----------
    interpret_pcr(panel_id, *, organisms=(), markers=(),
                  record=None, protocols_dir=None) -> PcrResult
    list_panel(panel_id, *, record=None, protocols_dir=None) -> PcrResult
    render_pcr(result) -> str
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import sys as _sys
_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_sys.path.insert(0, str(_PKG))                 # textnorm
_sys.path.insert(0, str(_PKG / "protocols"))   # loader
from textnorm import repair_mojibake, fold_accents  # noqa: E402
from loader import load_protocol                     # noqa: E402

DEFAULT_PROTOCOLS_DIR = _PKG / "protocols"


class PcrError(ValueError):
    """Raised when interpret_pcr cannot honour a request (bad panel/kind)."""


# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PcrItem:
    """One detected organism mapped to its verbatim selected-output answer."""
    organism: str
    answer: str
    tier: Optional[int] = None
    therapy: Optional[str] = None
    via_marker: Optional[str] = None   # the marker rule that changed the answer


@dataclass
class PcrResult:
    panel_id: str
    source_label: str
    canonical_name: str = ""
    route: str = "pcr_panel"
    tool: str = "interpret_pcr"
    mode: str = "interpret"            # interpret | list | needs_input | clarify
    needs_input: bool = False
    needs_clarification: bool = False
    clarify_reason: Optional[str] = None
    message: str = ""                  # the verbatim fixed text for non-interpret modes
    items: list = field(default_factory=list)          # list[PcrItem]
    not_on_panel: list = field(default_factory=list)    # organism strings not recognised
    escalation: Optional[PcrItem] = None                # polymicrobial highest-tier pick
    conflict: bool = False
    panel_organisms: list = field(default_factory=list)  # for list_panel
    selected_therapies: list = field(default_factory=list)
    footer: Optional[str] = None
    inputs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        def _item(it):
            return None if it is None else {
                "organism": it.organism, "answer": it.answer, "tier": it.tier,
                "therapy": it.therapy, "via_marker": it.via_marker}
        return {
            "panel_id": self.panel_id, "source_label": self.source_label,
            "canonical_name": self.canonical_name, "route": self.route,
            "tool": self.tool, "mode": self.mode, "needs_input": self.needs_input,
            "needs_clarification": self.needs_clarification,
            "clarify_reason": self.clarify_reason, "message": self.message,
            "items": [_item(i) for i in self.items],
            "not_on_panel": list(self.not_on_panel),
            "escalation": _item(self.escalation), "conflict": self.conflict,
            "panel_organisms": list(self.panel_organisms),
            "selected_therapies": list(self.selected_therapies),
            "footer": self.footer, "inputs": dict(self.inputs),
        }


# --------------------------------------------------------------------------- #
# Normalisation + matching (mirrors router._norm: fold accents AND separators) #
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    t = fold_accents(repair_mojibake(text or "")).lower()
    t = re.sub(r"[/_\-.]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _token_hit(folded_msg: str, alias: str) -> bool:
    """Whole-token (phrase) match of `alias` within the folded message."""
    if not alias:
        return False
    return re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded_msg) is not None


def load_pcr_panel(panel_id: str, protocols_dir=None) -> dict:
    base = Path(protocols_dir) if protocols_dir else DEFAULT_PROTOCOLS_DIR
    path = base / f"{panel_id}.yaml"
    if not path.exists():
        alt = base / f"{panel_id}.yml"
        path = alt if alt.exists() else path
    if not path.exists():
        raise PcrError(f"no pcr_panel protocol file for {panel_id!r} in {base}")
    record = load_protocol(path)
    if record.get("kind") != "pcr_panel":
        raise PcrError(f"{panel_id!r} is kind {record.get('kind')!r}, not 'pcr_panel'")
    return record


# --------------------------------------------------------------------------- #
# Index building                                                              #
# --------------------------------------------------------------------------- #
def _organism_index(record: dict):
    """folded-alias -> organism dict, longest alias first (for greedy match)."""
    pairs = []  # (folded_alias, organism)
    for org in record.get("organisms", []) or []:
        names = [org.get("name", "")] + list(org.get("aliases", []) or [])
        for n in names:
            f = _norm(n)
            if f:
                pairs.append((f, org))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def _marker_index(record: dict):
    pairs = []  # (folded_alias, marker)
    for mk in record.get("markers", []) or []:
        names = [mk.get("name", "")] + list(mk.get("aliases", []) or [])
        for n in names:
            f = _norm(n)
            if f:
                pairs.append((f, mk))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def _match_organism(s: str, org_pairs):
    """Return the organism whose longest folded alias is a whole-token match in s."""
    folded = _norm(s)
    if not folded:
        return None
    for alias, org in org_pairs:   # longest alias first
        if _token_hit(folded, alias):
            return org
    return None


def _match_markers(strings: Sequence[str], mk_pairs):
    found = []
    seen = set()
    for s in strings:
        folded = _norm(s)
        for alias, mk in mk_pairs:
            if _token_hit(folded, alias) and mk.get("name") not in seen:
                seen.add(mk.get("name"))
                found.append(mk)
                break
    return found


def _genus_disambiguation(s: str, record: dict) -> Optional[dict]:
    """If `s` is a bare genus declared in disambiguate_genus, return that entry."""
    folded = _norm(s)
    for ent in record.get("disambiguate_genus", []) or []:
        genus = _norm(ent.get("genus", ""))
        if genus and folded == genus:
            return ent
    return None


# --------------------------------------------------------------------------- #
# Marker application (declarative; verbatim outputs only)                      #
# --------------------------------------------------------------------------- #
def _rule_markers(markers):
    by_rule = {}
    for mk in markers:
        r = mk.get("rule")
        if r:
            by_rule.setdefault(r, mk)
    return by_rule


def _apply_markers_to_organism(org: dict, by_rule: dict):
    """Return (answer, therapy, tier, via_marker) after applying any firing marker
    rule to this organism. Verbatim strings only; never composed."""
    name = org.get("name", "")
    base_answer = org.get("answer", "")
    therapy = org.get("therapy")
    tier = org.get("tier")
    entero = bool(org.get("enterobacterales", False))
    etype = org.get("entity_type", "")

    # carbapenemase: panel-wide escalation to tier 4 for gram-negatives.
    if "carbapenemase" in by_rule and etype == "gram_negative":
        mk = by_rule["carbapenemase"]
        return mk.get("answer", base_answer), mk.get("therapy", therapy), 4, "carbapenemase"
    # mrsa: only changes Staph aureus (the organism that declares a marker_answer).
    if "mrsa" in by_rule and org.get("marker_answer") and "aureus" in name.lower():
        return org["marker_answer"], "vancomycin", tier, "mrsa"
    # vre: Enterococcus with a marker_answer (e.g. faecalis) -> its marker answer.
    if "vre" in by_rule and org.get("marker_answer") and "enterococcus" in name.lower():
        return org["marker_answer"], "linezolid", tier, "vre"
    # ctx_m: only Enterobacterales escalate (source: do NOT apply to non-entero).
    if "ctx_m" in by_rule and entero:
        mk = by_rule["ctx_m"]
        return mk.get("answer", base_answer), mk.get("therapy", therapy), 3, "ctx_m"
    return base_answer, therapy, tier, None


# --------------------------------------------------------------------------- #
# The tools                                                                   #
# --------------------------------------------------------------------------- #
def list_panel(panel_id: str, *, record=None, protocols_dir=None) -> PcrResult:
    if record is None:
        record = load_pcr_panel(panel_id, protocols_dir=protocols_dir)
    if record.get("kind") != "pcr_panel":
        raise PcrError(f"{panel_id!r} is not a pcr_panel protocol")
    names = [o.get("name", "") for o in record.get("organisms", []) or []]
    return PcrResult(
        panel_id=record.get("id", panel_id),
        source_label=record.get("source_label") or record.get("id") or panel_id,
        canonical_name=record.get("canonical_name", ""),
        tool="list_panel", mode="list",
        panel_organisms=names,
        footer=record.get("footer"),
    )


def interpret_pcr(panel_id: str, *, organisms: Sequence[str] = (),
                  markers: Sequence[str] = (), record=None,
                  protocols_dir=None) -> PcrResult:
    if record is None:
        record = load_pcr_panel(panel_id, protocols_dir=protocols_dir)
    if record.get("kind") != "pcr_panel":
        raise PcrError(f"{panel_id!r} is not a pcr_panel protocol")

    pid = record.get("id", panel_id)
    source_label = record.get("source_label") or pid
    base = dict(panel_id=pid, source_label=source_label,
                canonical_name=record.get("canonical_name", ""),
                footer=record.get("footer"),
                inputs={"organisms": list(organisms), "markers": list(markers)})

    organisms = [s for s in (organisms or []) if str(s).strip()]
    markers = [s for s in (markers or []) if str(s).strip()]

    # 1) required-input gate: at least one pathogen.
    if not organisms:
        if markers:
            msg = record.get("marker_without_pathogen") or record.get("default_answer", "")
            return PcrResult(mode="clarify", needs_clarification=True,
                             clarify_reason="marker_without_pathogen",
                             message=msg, **base)
        return PcrResult(mode="needs_input", needs_input=True,
                         message=record.get("default_answer", ""), **base)

    org_pairs = _organism_index(record)
    mk_pairs = _marker_index(record)

    # 2) resolve organisms; a bare genus -> disambiguation (F5).
    resolved: list[dict] = []
    not_on_panel: list[str] = []
    for s in organisms:
        org = _match_organism(s, org_pairs)
        if org is not None:
            resolved.append(org)
            continue
        dis = _genus_disambiguation(s, record)
        if dis is not None:
            species = ", ".join(dis.get("species", []))
            msg = (f"{dis.get('genus')} is on the {record.get('canonical_name', pid)} "
                   f"but several species are possible: {species}. "
                   f"Which species was detected?")
            return PcrResult(mode="clarify", needs_clarification=True,
                             clarify_reason="ambiguous_genus", message=msg, **base)
        not_on_panel.append(s)

    found_markers = _match_markers(markers, mk_pairs)
    by_rule = _rule_markers(found_markers)

    # 3) if nothing resolved to a panel organism, say so explicitly (F8) — never
    #    fabricate a recommendation.
    if not resolved:
        listed = ", ".join(not_on_panel)
        msg = (f"{listed} is not on the {record.get('canonical_name', pid)}. "
               f"Send an organism detected by this panel.")
        return PcrResult(mode="clarify", needs_clarification=True,
                         clarify_reason="not_on_panel", message=msg,
                         not_on_panel=not_on_panel, **base)

    # 4) per-organism verbatim selection (with marker adjustment).
    items: list[PcrItem] = []
    therapies: list[str] = []
    for org in resolved:
        answer, therapy, tier, via = _apply_markers_to_organism(org, by_rule)
        items.append(PcrItem(organism=org.get("name", ""), answer=answer,
                             tier=tier, therapy=therapy, via_marker=via))
        if therapy:
            therapies.append(therapy)

    res = PcrResult(mode="interpret", items=items, not_on_panel=not_on_panel,
                    selected_therapies=therapies, **base)

    # 5) polymicrobial escalation: pick the highest numeric spectrum tier among the
    #    (marker-adjusted) organisms and surface the panel's verbatim tier output.
    if len(items) > 1:
        spectrum = record.get("spectrum_tiers", {}) or {}
        tiered = [it for it in items if isinstance(it.tier, int)]
        if tiered:
            top = max(it.tier for it in tiered)
            tspec = spectrum.get(str(top))
            if tspec:
                res.escalation = PcrItem(
                    organism="polymicrobial", answer=tspec.get("answer", ""),
                    tier=top, therapy=tspec.get("therapy"))
            # Documented conflict: an ertapenem (CTX-M tier-3) selection cannot
            # cover an antipseudomonal/MDR non-Enterobacterales organism present
            # at tier >= 2 (Pseudomonas, Acinetobacter). Recommend ID consult.
            if any(it.via_marker == "ctx_m" for it in items):
                conflict_orgs = [it for it in items
                                 if it.via_marker is None and (it.tier or 0) >= 2]
                if conflict_orgs and (tspec or {}).get("therapy") == "ertapenem":
                    res.conflict = True
                    res.message = record.get("conflict_answer", "")
    return res


# --------------------------------------------------------------------------- #
# Faithful plain-text rendering (harness/debug — NOT final UX phrasing)        #
# --------------------------------------------------------------------------- #
def render_pcr(result: PcrResult) -> str:
    lbl = result.source_label
    if result.mode in ("needs_input", "clarify"):
        return f"[{lbl}] {result.message}".rstrip()
    if result.mode == "list":
        lines = [f"[{lbl}] {result.canonical_name} panel organisms:"]
        for n in result.panel_organisms:
            lines.append(f"- {n}")
        if result.footer:
            lines.append(result.footer.strip())
        return "\n".join(lines)
    # interpret
    lines = [f"[{lbl}]"]
    for it in result.items:
        lines.append(f"{it.organism}: {it.answer}")
    if result.not_on_panel:
        lines.append("Not on this panel: " + ", ".join(result.not_on_panel))
    if result.conflict and result.message:
        lines.append(result.message)
    elif result.escalation is not None:
        lines.append("Polymicrobial - select by highest required spectrum tier: "
                     + result.escalation.answer)
    if result.footer:
        lines.append(result.footer.strip())
    return "\n".join(lines)


__all__ = [
    "interpret_pcr", "list_panel", "render_pcr", "load_pcr_panel",
    "PcrResult", "PcrItem", "PcrError",
]
