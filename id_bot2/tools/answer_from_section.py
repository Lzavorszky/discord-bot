#!/usr/bin/env python3
"""answer_from_section.py — the prose-selection tool (Plan D, final migration).

Given a migrated ``prose`` protocol (a bounded-text, section-addressable
info-only reference such as the perioperative medication guide, the
perioperative steroid guide, or the dantrolene / malignant-hyperthermia sheet)
and an optional named ``section``, return the matching section's **verbatim**
text.

Like ``get_dose``, ``interpret_pcr``, ``select_pathway`` and ``calculate``, this
tool NEVER composes a novel clinical statement. It only ever SELECTS one of the
text blocks already written into the protocol file:

  * a named ``section`` that exists  → that section's ``text_en`` / ``text_hu``
    is returned **exactly as written** (the whole entry — for the antithrombotic
    drugs the source insists the *complete* entry, all timing rows, is returned,
    and that is simply what the section text contains);
  * no ``section`` given, when the protocol declares a ``default_section``
    (the single-block guides: dantrolene, perioperative steroids) → that block
    is returned verbatim — these guides are always shown whole;
  * no ``section`` given and no ``default_section`` (the multi-entry guide:
    perioperative medications) → the verbatim ``default_answer`` ask
    ("Which medication …?") is returned and ``needs_input`` is set — never a
    guessed entry;
  * a named ``section`` that does NOT exist in this protocol → the verbatim
    ``no_match_answer`` (or, if absent, the ``default_answer``) is returned and
    ``needs_input`` is set — never an invented entry, never a different drug.

There is no computation and no slot logic here: the addressable unit is the
section, the router decides which section name (if any) the message points to,
and this tool returns it verbatim. Dantrolene's source explicitly FORBIDS
computing new ampoule counts or volumes beyond its 60/80/100 kg examples, so it
is modelled as a single verbatim block — NOT a calculator.

Public API
----------
    answer_from_section(prose_id, *, section=None, record=None,
                        protocols_dir=None) -> ProseResult
    render_prose(result) -> str
    load_prose(prose_id, *, protocols_dir=None) -> dict
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import sys as _sys
_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_sys.path.insert(0, str(_PKG / "protocols"))   # loader
from loader import load_protocol               # noqa: E402  type: ignore

DEFAULT_PROTOCOLS_DIR = _PKG / "protocols"


class ProseError(ValueError):
    """Raised when answer_from_section cannot honour a request (bad id/kind)."""


# --------------------------------------------------------------------------- #
# Result type                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class ProseResult:
    prose_id: str
    source_label: str
    canonical_name: str = ""
    route: str = "prose"
    tool: str = "answer_from_section"
    section: str = ""                      # the selected section name ("" = default/ask)
    text_hu: str = ""
    text_en: str = ""
    is_default: bool = False               # True when no specific section was returned
    needs_input: bool = False              # True when the protocol asked for the topic
    requested_section: Optional[str] = None
    footer: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "prose_id": self.prose_id, "source_label": self.source_label,
            "canonical_name": self.canonical_name, "route": self.route,
            "tool": self.tool, "section": self.section,
            "text_hu": self.text_hu, "text_en": self.text_en,
            "is_default": self.is_default, "needs_input": self.needs_input,
            "requested_section": self.requested_section, "footer": self.footer,
        }


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #
def load_prose(prose_id: str, *, protocols_dir=None) -> dict:
    base = Path(protocols_dir) if protocols_dir else DEFAULT_PROTOCOLS_DIR
    path = base / f"{prose_id}.yaml"
    if not path.exists():
        alt = base / f"{prose_id}.yml"
        path = alt if alt.exists() else path
    if not path.exists():
        raise ProseError(f"no prose protocol file for {prose_id!r} in {base}")
    record = load_protocol(path)
    if record.get("kind") != "prose":
        raise ProseError(
            f"{prose_id!r} is kind {record.get('kind')!r}, not 'prose'")
    return record


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _section_text(sspec: dict) -> tuple[str, str]:
    """Return (text_hu, text_en) for a section spec. A bare ``text`` (language
    not split in the source) is surfaced through ``text_en`` so the renderer,
    which prefers EN, prints it."""
    if not isinstance(sspec, dict):
        return "", ""
    hu = sspec.get("text_hu", "") or ""
    en = sspec.get("text_en", "") or ""
    bare = sspec.get("text", "") or ""
    if bare and not en and not hu:
        en = bare
    return hu, en


# --------------------------------------------------------------------------- #
# The tool                                                                    #
# --------------------------------------------------------------------------- #
def answer_from_section(prose_id: str, *, section: Optional[str] = None,
                        record: Optional[dict] = None,
                        protocols_dir=None) -> ProseResult:
    """Select and return the verbatim text for ``prose_id`` (optionally a named
    ``section``).

    Pass ``record`` to use an already-loaded protocol dict (offline unit tests);
    otherwise it is loaded from ``protocols_dir`` (default: the package
    ``protocols/`` dir).
    """
    if record is None:
        record = load_prose(prose_id, protocols_dir=protocols_dir)
    if record.get("kind") != "prose":
        raise ProseError(f"{prose_id!r} is not a prose protocol")

    pid = record.get("id", prose_id)
    source_label = record.get("source_label") or pid
    sections = record.get("sections") or {}
    if not isinstance(sections, dict) or not sections:
        raise ProseError(f"{pid!r} has no sections")

    base = dict(
        prose_id=pid,
        source_label=source_label,
        canonical_name=record.get("canonical_name", ""),
        footer=record.get("footer"),
        requested_section=section,
    )

    def _ask(is_no_match: bool) -> ProseResult:
        """Return the verbatim default/ask text (no specific section selected)."""
        if is_no_match and record.get("no_match_answer"):
            en = record.get("no_match_answer", "") or ""
            hu = record.get("no_match_answer_hu", "") or ""
        else:
            en = record.get("default_answer", "") or ""
            hu = record.get("default_answer_hu", "") or ""
        return ProseResult(section="", text_hu=hu, text_en=en,
                           is_default=True, needs_input=True, **base)

    # 1) explicit section requested
    if section is not None:
        if section in sections:
            hu, en = _section_text(sections[section])
            return ProseResult(section=section, text_hu=hu, text_en=en,
                               is_default=False, needs_input=False, **base)
        # named a section this protocol does not have → verbatim no-match/ask,
        # never an invented entry or a different drug's entry.
        return _ask(is_no_match=True)

    # 2) no section named → the whole-guide default section, if declared
    ds = record.get("default_section")
    if ds:
        if ds not in sections:
            raise ProseError(
                f"{pid!r} default_section {ds!r} is not a defined section "
                f"(defined: {sorted(sections)})")
        hu, en = _section_text(sections[ds])
        return ProseResult(section=ds, text_hu=hu, text_en=en,
                           is_default=False, needs_input=False, **base)

    # 3) no section and no default_section → ask which topic (verbatim)
    return _ask(is_no_match=False)


# --------------------------------------------------------------------------- #
# Faithful plain-text rendering (harness/debug — NOT final UX phrasing)        #
# --------------------------------------------------------------------------- #
def render_prose(result: ProseResult) -> str:
    """Render the selected section verbatim. Prefers the English text; falls
    back to the Hungarian text when a section has no English string (e.g. the
    dantrolene guideline is Hungarian-only)."""
    body = result.text_en.strip() or result.text_hu.strip()
    head = result.section or "(default)"
    lines = [f"[{result.source_label}] {head}"]
    if body:
        lines.append(body)
    if result.footer:
        lines.append(result.footer.strip())
    return "\n".join(lines)


__all__ = [
    "answer_from_section", "render_prose", "load_prose",
    "ProseResult", "ProseError",
]
