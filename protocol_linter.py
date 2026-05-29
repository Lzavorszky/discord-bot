"""
protocol_linter.py — Protocol file linter for the hospital bot.

Usage (CLI):
    python -m protocol_linter                     # lint protocols/*.txt
    python -m protocol_linter path/to/file.txt    # lint specific files

All checks are WARNING-level only; the bot starts regardless.

Checks performed
----------------
Structure:
  missing_required_panels   — METADATA or ALIASES absent
  out_of_order_panels       — canonical panels appear out of guide order
  unknown_panel             — unrecognised ## SECTION landed in free_form

Metadata:
  missing_protocol_id       — protocol_id key absent
  missing_source_label      — source_label key absent
  invalid_protocol_type     — value not in known set
  invalid_answer_mode       — value not in guide's valid set
  invalid_selection_mode    — value not in guide's valid set
  missing_governance        — version / last_reviewed / owner / status absent
  invalid_status            — status not in draft|internal|approved

Aliases:
  broad_alias               — single generic word like "carbapenem", "pneumonia"
  duplicate_alias           — same alias listed twice in one protocol
  alias_collision           — same alias maps to two different protocols

Links:
  link_target_missing       — target_file does not exist and no target_missing_behavior

Safety / dosing intent:
  dosing_without_flag       — allows_dosing:no but dose-like text in outputs/intents
  default_dose_without_flag — default_dose_allowed:no but DEFAULT_ANSWER has dose-like text
"""

from __future__ import annotations

import glob
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List

# ---------------------------------------------------------------------------
# Import parser (sibling module)
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from protocol_parser import (
    parse_protocol_file,
    _parse_protocol_text,
    VALID_ANSWER_MODES,
    VALID_SELECTION_MODES,
    CANONICAL_PANELS,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_PROTOCOL_TYPES = {
    "drug_dosing_protocol",
    "pathway_selection_protocol",
    "microbiology_interpretation_protocol",
    "diagnostic_protocol",
    "monitoring_protocol",
    "general_rules_protocol",
}

VALID_STATUSES = {"draft", "internal", "approved"}

GOVERNANCE_KEYS = {"version", "last_reviewed", "owner", "status"}

# Aliases that are too broad (single very generic terms)
BROAD_ALIAS_SET = {
    "antibiotic", "antibiotics", "antimicrobial", "antimicrobials",
    "beta-lactam", "beta lactam", "carbapenem", "carbapenems",
    "penicillin", "penicillins", "pneumonia", "infection", "infections",
    "drug", "medicine", "therapy",
}

# Dose-like pattern in text
_DOSE_RE = re.compile(
    r"\b(\d+\s*(mg|g|mcg|µg|amp|ml|mmol|mEq|units?)\b"
    r"|\d+\s*/\s*(kg|day|24h|dose|hr|h)\b"
    r"|q\d+h\b|every\s+\d+\s*h(our)?s?\b"
    r"|\d+\s*g/day\b|\d+\s*mg/kg\b)",
    re.IGNORECASE,
)

# New-schema panels that should exist for well-formed new-schema protocols
NEW_SCHEMA_REQUIRED = {"METADATA", "ALIASES"}

# Panels that indicate this is a new-schema file
NEW_SCHEMA_INDICATOR = {"INTENTS", "INPUT_SLOTS", "SELECTION_RULES", "SELECTED_OUTPUTS"}

# Files to skip (non-protocol helper files)
SKIP_FILES = {
    "protocol structure guide.txt",
    "aliases.json",
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class LintIssue:
    severity:    str   # "WARNING" | "ERROR"
    code:        str
    protocol:    str
    message:     str


@dataclass
class LintResult:
    issues:  List[LintIssue] = field(default_factory=list)

    def add(self, severity, code, protocol, message):
        self.issues.append(LintIssue(severity, code, protocol, message))

    def warnings(self):
        return [i for i in self.issues if i.severity == "WARNING"]

    def errors(self):
        return [i for i in self.issues if i.severity == "ERROR"]

    def has_issues(self):
        return bool(self.issues)


# ---------------------------------------------------------------------------
# Per-file checks
# ---------------------------------------------------------------------------

def _lint_file(path: str, result: LintResult, all_aliases: dict):
    """Lint a single protocol file and append issues to result."""
    name = os.path.basename(path)
    try:
        p = parse_protocol_file(path)
    except Exception as exc:
        result.add("ERROR", "parse_crash", name, f"Parser crashed: {exc}")
        return

    meta = p["metadata"]
    is_new_schema = bool(
        p.get("intents") or p.get("selection_rules") or p.get("selected_outputs")
    )

    # ── Structure ────────────────────────────────────────────────────────────

    if not meta:
        result.add("WARNING", "missing_required_panels", name,
                   "## METADATA panel is absent or empty")

    if not p.get("aliases") and not p.get("free_form"):
        result.add("WARNING", "missing_required_panels", name,
                   "## ALIASES panel is absent")

    if p.get("warnings"):
        for w in p["warnings"]:
            result.add("WARNING", "out_of_order_panels", name, w)

    for ff_name in p.get("free_form", {}):
        result.add("WARNING", "unknown_panel", name,
                   f"Unknown panel '## {ff_name}' landed in free_form")

    # ── Metadata ─────────────────────────────────────────────────────────────

    if not meta.get("protocol_id"):
        result.add("WARNING", "missing_protocol_id", name,
                   "metadata.protocol_id is missing")

    if not meta.get("source_label"):
        result.add("WARNING", "missing_source_label", name,
                   "metadata.source_label is missing")

    ptype = meta.get("protocol_type", "")
    if ptype and ptype not in VALID_PROTOCOL_TYPES:
        result.add("WARNING", "invalid_protocol_type", name,
                   f"Unknown protocol_type '{ptype}'. "
                   f"Valid: {sorted(VALID_PROTOCOL_TYPES)}")

    amode = meta.get("answer_mode", "")
    if amode and amode not in VALID_ANSWER_MODES:
        result.add("WARNING", "invalid_answer_mode", name,
                   f"Unknown answer_mode '{amode}'. "
                   f"Valid: {sorted(VALID_ANSWER_MODES)}")

    smode = meta.get("selection_mode", "")
    if smode and smode not in VALID_SELECTION_MODES:
        result.add("WARNING", "invalid_selection_mode", name,
                   f"Unknown selection_mode '{smode}'. "
                   f"Valid: {sorted(VALID_SELECTION_MODES)}")

    for gkey in GOVERNANCE_KEYS:
        if not meta.get(gkey):
            result.add("WARNING", "missing_governance", name,
                       f"Governance metadata '{gkey}' is missing")

    status = meta.get("status", "")
    if status and status not in VALID_STATUSES:
        result.add("WARNING", "invalid_status", name,
                   f"Invalid status '{status}'. Valid: {sorted(VALID_STATUSES)}")

    # ── Aliases ──────────────────────────────────────────────────────────────

    alias_items = _extract_alias_list(p.get("aliases", ""))
    pid = meta.get("protocol_id", name)

    seen_in_file = set()
    for alias in alias_items:
        a_lower = alias.lower()

        if a_lower in BROAD_ALIAS_SET:
            result.add("WARNING", "broad_alias", name,
                       f"Broad/generic alias '{alias}' may cause false matches")

        if a_lower in seen_in_file:
            result.add("WARNING", "duplicate_alias", name,
                       f"Duplicate alias '{alias}' within this protocol")
        seen_in_file.add(a_lower)

        # Register for cross-protocol collision check
        all_aliases.setdefault(a_lower, []).append((pid, name))

    # ── Links ────────────────────────────────────────────────────────────────

    base_dir = os.path.dirname(os.path.dirname(path))  # one up from protocols/
    for lname, lentry in p.get("links", {}).items():
        target_file = lentry.get("target_file", "")
        target_missing = lentry.get("target_missing_behavior", "")
        if target_file:
            abs_target = os.path.join(base_dir, target_file)
            if not os.path.exists(abs_target) and not target_missing:
                result.add("WARNING", "link_target_missing", name,
                           f"LINK '{lname}': target '{target_file}' not found "
                           f"and no target_missing_behavior defined")

    # ── Safety / dosing intent ────────────────────────────────────────────────

    allows_dosing = meta.get("allows_dosing", "yes").lower()
    default_dose_allowed = meta.get("default_dose_allowed", "yes").lower()

    if allows_dosing == "no":
        # Check selected_outputs and intents for dose-like content
        dosing_text = p.get("selected_outputs", "") + "\n" + p.get("intents", "")
        if _DOSE_RE.search(dosing_text):
            result.add("WARNING", "dosing_without_flag", name,
                       "allows_dosing:no but dose-like content found in "
                       "SELECTED_OUTPUTS or INTENTS")

    if default_dose_allowed == "no":
        default_answer = p.get("default_answer", "")
        if _DOSE_RE.search(default_answer):
            result.add("WARNING", "default_dose_without_flag", name,
                       "default_dose_allowed:no but DEFAULT_ANSWER contains "
                       "dose-like text")


def _extract_alias_list(aliases_text: str) -> list[str]:
    """Extract individual alias strings from an ## ALIASES panel body."""
    items = []
    for line in aliases_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
        elif stripped and not stripped.endswith(":") and ":" not in stripped:
            items.append(stripped)
    return items


# ---------------------------------------------------------------------------
# Cross-protocol checks (run after all files are linted)
# ---------------------------------------------------------------------------

def _lint_cross_protocol(all_aliases: dict, result: LintResult):
    """Check for alias collisions across protocols."""
    for alias, entries in all_aliases.items():
        if len(entries) < 2:
            continue
        pids = [e[0] for e in entries]
        # Deduplicate (same pid appearing twice from duplicate alias in one file)
        unique_pids = list(dict.fromkeys(pids))
        if len(unique_pids) > 1:
            names = ", ".join(f"{e[1]}({e[0]})" for e in entries)
            result.add("WARNING", "alias_collision", "<cross-protocol>",
                       f"Alias '{alias}' maps to multiple protocols: {names}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_linter(
    proto_dir: str = "protocols",
    extra_files: list[str] | None = None,
) -> LintResult:
    """Lint all protocol files in proto_dir and return a LintResult.

    Args:
        proto_dir:    directory containing *.txt protocol files
        extra_files:  additional file paths to lint (optional)
    """
    result = LintResult()
    all_aliases: dict[str, list[tuple[str, str]]] = {}

    files = sorted(
        f for f in glob.glob(os.path.join(proto_dir, "*.txt"))
        if os.path.basename(f) not in SKIP_FILES
    )
    if extra_files:
        files += [f for f in extra_files if f not in files]

    if not files:
        result.add("WARNING", "no_files", "<linter>",
                   f"No protocol files found in '{proto_dir}'")
        return result

    for path in files:
        _lint_file(path, result, all_aliases)

    _lint_cross_protocol(all_aliases, result)
    return result


def print_report(result: LintResult, verbose: bool = False) -> int:
    """Print a human-readable linter report. Returns exit code (0=clean)."""
    issues = result.issues
    if not issues:
        print("[linter] All protocols clean.")
        return 0

    # Group by code for summary
    from collections import Counter
    counts = Counter(i.code for i in issues)

    print(f"\n[linter] {len(issues)} issue(s) found:\n")
    for issue in issues:
        print(f"  [{issue.severity}] {issue.protocol}: [{issue.code}] {issue.message}")

    print(f"\n[linter] Summary: {dict(counts)}")
    return 1 if result.errors() else 0


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = sys.argv[1:]

    # Determine protocol directory (default: protocols/ relative to CWD or script)
    if args and os.path.isdir(args[0]):
        proto_dir = args.pop(0)
    else:
        # Try CWD first, then script directory
        for candidate in ["protocols", os.path.join(_HERE, "..", "protocols"), _HERE]:
            if os.path.isdir(candidate):
                proto_dir = candidate
                break
        else:
            proto_dir = "protocols"

    extra = args  # any remaining args treated as additional file paths

    print(f"[linter] Linting protocols in: {os.path.abspath(proto_dir)}")
    result = run_linter(proto_dir=proto_dir, extra_files=extra or None)
    sys.exit(print_report(result))
