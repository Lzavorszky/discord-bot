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
import json
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

_NUMERIC_SELECTION_SLOT_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:>=|<=|>|<)\s*[-+]?\d+(?:\.\d+)?\b"
)

_TABLE_AXIS_SLOT_RE = re.compile(
    r"^\s*(?:WEIGHT_SLOT|NUMERIC_AXIS|TABLE_AXIS|AXIS_SLOT):\s*([A-Za-z_][A-Za-z0-9_]*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_EXTRAPOLATION_ALLOWED_RE = re.compile(
    r"\b(?:extrapolation_allowed|allow_extrapolation)\s*:\s*(?:true|yes)\b",
    re.IGNORECASE,
)

_EXTRAPOLATION_METHOD_RE = re.compile(
    r"\b(?:extrapolation_method|extrapolation_policy)\s*:\s*\S+",
    re.IGNORECASE,
)

_SAFETY_NOTE_RE = re.compile(
    r"\b(?:safety_note|extrapolation_safety_note)\s*:\s*\S+",
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
    issue_path = os.path.normpath(os.path.abspath(path))
    try:
        p = parse_protocol_file(path)
    except Exception as exc:
        result.add("ERROR", "parse_crash", issue_path, f"Parser crashed: {exc}")
        return

    meta = p["metadata"]
    is_new_schema = bool(
        p.get("intents") or p.get("selection_rules") or p.get("selected_outputs")
    )

    # ── Structure ────────────────────────────────────────────────────────────

    if not meta:
        result.add("WARNING", "missing_required_panels", issue_path,
                   "## METADATA panel is absent or empty")

    if not p.get("aliases") and not p.get("free_form"):
        result.add("WARNING", "missing_required_panels", issue_path,
                   "## ALIASES panel is absent")

    if p.get("warnings"):
        for w in p["warnings"]:
            result.add("WARNING", "out_of_order_panels", issue_path, w)

    for ff_name in p.get("free_form", {}):
        result.add("WARNING", "unknown_panel", issue_path,
                   f"Unknown panel '## {ff_name}' landed in free_form")

    # ── Metadata ─────────────────────────────────────────────────────────────

    if not meta.get("protocol_id"):
        result.add("WARNING", "missing_protocol_id", issue_path,
                   "metadata.protocol_id is missing")

    if not meta.get("source_label"):
        result.add("WARNING", "missing_source_label", issue_path,
                   "metadata.source_label is missing")

    ptype = meta.get("protocol_type", "")
    if ptype and ptype not in VALID_PROTOCOL_TYPES:
        result.add("WARNING", "invalid_protocol_type", issue_path,
                   f"Unknown protocol_type '{ptype}'. "
                   f"Valid: {sorted(VALID_PROTOCOL_TYPES)}")

    amode = meta.get("answer_mode", "")
    if amode and amode not in VALID_ANSWER_MODES:
        result.add("WARNING", "invalid_answer_mode", issue_path,
                   f"Unknown answer_mode '{amode}'. "
                   f"Valid: {sorted(VALID_ANSWER_MODES)}")

    smode = meta.get("selection_mode", "")
    if smode and smode not in VALID_SELECTION_MODES:
        result.add("WARNING", "invalid_selection_mode", issue_path,
                   f"Unknown selection_mode '{smode}'. "
                   f"Valid: {sorted(VALID_SELECTION_MODES)}")

    for gkey in GOVERNANCE_KEYS:
        if not meta.get(gkey):
            result.add("WARNING", "missing_governance", issue_path,
                       f"Governance metadata '{gkey}' is missing")

    status = meta.get("status", "")
    if status and status not in VALID_STATUSES:
        result.add("WARNING", "invalid_status", issue_path,
                   f"Invalid status '{status}'. Valid: {sorted(VALID_STATUSES)}")

    # ── Aliases ──────────────────────────────────────────────────────────────

    alias_items = _extract_alias_list(p.get("aliases", ""))
    pid = meta.get("protocol_id", name)

    seen_in_file = set()
    for alias in alias_items:
        a_lower = alias.lower()

        if a_lower in BROAD_ALIAS_SET:
            result.add("WARNING", "broad_alias", issue_path,
                       f"Broad/generic alias '{alias}' may cause false matches")

        if a_lower in seen_in_file:
            result.add("WARNING", "duplicate_alias", issue_path,
                       f"Duplicate alias '{alias}' within this protocol")
        seen_in_file.add(a_lower)

        # Register for cross-protocol collision check
        all_aliases.setdefault(a_lower, []).append((pid, issue_path))

    # ── Links ────────────────────────────────────────────────────────────────

    base_dir = os.path.dirname(os.path.dirname(path))  # one up from protocols/
    for lname, lentry in p.get("links", {}).items():
        target_file = lentry.get("target_file", "")
        target_missing = lentry.get("target_missing_behavior", "")
        if target_file:
            abs_target = os.path.join(base_dir, target_file)
            if not os.path.exists(abs_target) and not target_missing:
                result.add("WARNING", "link_target_missing", issue_path,
                           f"LINK '{lname}': target '{target_file}' not found "
                           f"and no target_missing_behavior defined")

    # ── Safety / dosing intent ────────────────────────────────────────────────

    allows_dosing = meta.get("allows_dosing", "yes").lower()
    default_dose_allowed = meta.get("default_dose_allowed", "yes").lower()

    if allows_dosing == "no":
        # Check selected_outputs and intents for dose-like content
        dosing_text = p.get("selected_outputs", "") + "\n" + p.get("intents", "")
        if _DOSE_RE.search(dosing_text):
            result.add("WARNING", "dosing_without_flag", issue_path,
                       "allows_dosing:no but dose-like content found in "
                       "SELECTED_OUTPUTS or INTENTS")

    if default_dose_allowed == "no":
        default_answer = p.get("default_answer", "")
        if _DOSE_RE.search(default_answer):
            result.add("WARNING", "default_dose_without_flag", issue_path,
                       "default_dose_allowed:no but DEFAULT_ANSWER contains "
                       "dose-like text")

    _lint_slot_schema_safety(p, issue_path, result)


def _lint_slot_schema_safety(p: dict, issue_path: str, result: LintResult):
    """Validate numeric SLOT_SCHEMA metadata used by deterministic selection."""
    meta = p.get("metadata") or {}
    schema = p.get("slot_schema") or {}
    selection_rules = p.get("selection_rules") or ""
    severity = _numeric_schema_severity(meta)

    numeric_selection_slots = _numeric_slots_used_in_selection(selection_rules)
    table_axis_slots = _numeric_table_axis_slots(selection_rules) | _numeric_table_axis_slots_from_outputs(
        p.get("selected_outputs") or "",
        schema,
    )
    required_numeric_slots = numeric_selection_slots | table_axis_slots

    for slot_name in sorted(required_numeric_slots):
        spec = schema.get(slot_name)
        if not isinstance(spec, dict):
            code = "missing_slot_schema" if not schema else "undeclared_numeric_selection_slot"
            result.add(
                severity,
                code,
                issue_path,
                f"Numeric selection slot '{slot_name}' is used in SELECTION_RULES but is not declared in SLOT_SCHEMA",
            )
            continue
        if str(spec.get("type", "")).lower() != "number":
            result.add(
                severity,
                "missing_numeric_slot_bounds",
                issue_path,
                f"Slot '{slot_name}' is used numerically in SELECTION_RULES but SLOT_SCHEMA.type is not 'number'",
            )

    for slot_name, spec in sorted(schema.items()):
        if not isinstance(spec, dict):
            continue
        if str(spec.get("type", "")).lower() != "number":
            continue
        _lint_clinical_bounds(slot_name, spec, severity, issue_path, result)
        _lint_supported_bounds_if_present(slot_name, spec, severity, issue_path, result)

    _lint_table_axis_bounds(
        table_axis_slots=table_axis_slots,
        schema=schema,
        selection_rules=selection_rules,
        selected_outputs=p.get("selected_outputs") or "",
        safety_rules=p.get("safety_rules") or "",
        selection_mode=(meta.get("selection_mode") or "").lower(),
        severity=severity,
        issue_path=issue_path,
        result=result,
    )


def _numeric_schema_severity(meta: dict) -> str:
    """Blocking for deterministic dosing protocols; warning for info-only/other files."""
    answer_mode = (meta.get("answer_mode") or "").lower()
    selection_mode = (meta.get("selection_mode") or "").lower()
    protocol_type = (meta.get("protocol_type") or "").lower()
    allows_dosing = (meta.get("allows_dosing") or "yes").lower()

    is_info_only = (
        answer_mode == "info_only"
        or selection_mode == "none"
        or allows_dosing == "no"
    )
    is_deterministic_dosing = (
        protocol_type == "drug_dosing_protocol"
        and selection_mode in {"priority_rules", "table_lookup"}
        and not is_info_only
    )
    return "ERROR" if is_deterministic_dosing else "WARNING"


def _numeric_slots_used_in_selection(selection_rules: str) -> set[str]:
    return {
        match.group(1).lower()
        for match in _NUMERIC_SELECTION_SLOT_RE.finditer(selection_rules or "")
    }


def _numeric_table_axis_slots(selection_rules: str) -> set[str]:
    return {
        match.group(1).lower()
        for match in _TABLE_AXIS_SLOT_RE.finditer(selection_rules or "")
    }


def _normalize_axis_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _axis_tokens(value: str) -> list[str]:
    return [t for t in _normalize_axis_text(value).split("_") if t]


def _slot_axis_labels(slot_name: str, spec: dict) -> set[str]:
    labels = {slot_name}
    for key in ("table_header", "axis_header", "header", "label", "display_name"):
        if isinstance(spec, dict) and spec.get(key):
            labels.add(str(spec.get(key)))
    tokens = _axis_tokens(slot_name)
    if tokens:
        labels.add(" ".join(tokens))
    if tokens and tokens[-1] in {"kg", "mg", "ml", "min", "day", "hr", "h"}:
        labels.add(" ".join(tokens[:-1]))
    if "weight" in tokens and "adjusted" not in tokens:
        labels.update({"weight", "body weight"})
    return {_normalize_axis_text(label) for label in labels if _normalize_axis_text(label)}


def _table_header_matches_slot(header: str, slot_name: str, spec: dict) -> bool:
    header_norm = _normalize_axis_text(header)
    if not header_norm:
        return False
    slot_tokens = set(_axis_tokens(slot_name))
    if header_norm == "weight" and "adjusted" in slot_tokens:
        return False
    labels = _slot_axis_labels(slot_name, spec)
    if header_norm in labels:
        return True
    header_tokens = set(_axis_tokens(header_norm))
    for label in labels:
        label_tokens = set(_axis_tokens(label))
        if label_tokens and label_tokens.issubset(header_tokens):
            return True
        if header_tokens and header_tokens.issubset(label_tokens):
            return True
    return False


def _markdown_table_headers(selected_outputs: str) -> list[list[str]]:
    headers = []
    table_lines = []
    for raw_line in (selected_outputs or "").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("|"):
            table_lines.append(stripped)
            continue
        if table_lines:
            header = _header_from_table_lines(table_lines)
            if header:
                headers.append(header)
            table_lines = []
    if table_lines:
        header = _header_from_table_lines(table_lines)
        if header:
            headers.append(header)
    return headers


def _header_from_table_lines(lines: list[str]) -> list[str]:
    for line in lines:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.match(r"^[-:]+$", c.strip()) for c in cells if c.strip()):
            continue
        return [c.lower().replace(" ", "_") for c in cells]
    return []


def _numeric_table_axis_slots_from_outputs(selected_outputs: str, schema: dict) -> set[str]:
    numeric_slots = {
        name: spec for name, spec in (schema or {}).items()
        if isinstance(spec, dict) and str(spec.get("type", "")).lower() == "number"
    }
    if not numeric_slots:
        return set()
    axes = set()
    for headers in _markdown_table_headers(selected_outputs or ""):
        for slot_name, spec in numeric_slots.items():
            if any(_table_header_matches_slot(header, slot_name, spec) for header in headers):
                axes.add(slot_name)
    return axes


def _as_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _lint_clinical_bounds(
    slot_name: str,
    spec: dict,
    severity: str,
    issue_path: str,
    result: LintResult,
):
    has_min = "clinical_min" in spec
    has_max = "clinical_max" in spec
    if not has_min or not has_max:
        result.add(
            severity,
            "missing_numeric_slot_bounds",
            issue_path,
            f"Numeric SLOT_SCHEMA slot '{slot_name}' must declare both clinical_min and clinical_max",
        )
        return

    clinical_min = _as_number(spec.get("clinical_min"))
    clinical_max = _as_number(spec.get("clinical_max"))
    if clinical_min is None or clinical_max is None:
        result.add(
            severity,
            "invalid_numeric_slot_bounds",
            issue_path,
            f"Numeric SLOT_SCHEMA slot '{slot_name}' has non-numeric clinical_min/clinical_max",
        )
        return
    if clinical_min >= clinical_max:
        result.add(
            severity,
            "invalid_numeric_slot_bounds",
            issue_path,
            f"Numeric SLOT_SCHEMA slot '{slot_name}' requires clinical_min lower than clinical_max",
        )


def _lint_supported_bounds_if_present(
    slot_name: str,
    spec: dict,
    severity: str,
    issue_path: str,
    result: LintResult,
):
    has_min = "supported_min" in spec
    has_max = "supported_max" in spec
    if not has_min and not has_max:
        return
    if not has_min or not has_max:
        result.add(
            severity,
            "invalid_supported_table_bounds",
            issue_path,
            f"Numeric SLOT_SCHEMA slot '{slot_name}' must declare supported_min and supported_max together",
        )
        return

    supported_min = _as_number(spec.get("supported_min"))
    supported_max = _as_number(spec.get("supported_max"))
    if supported_min is None or supported_max is None:
        result.add(
            severity,
            "invalid_supported_table_bounds",
            issue_path,
            f"Numeric SLOT_SCHEMA slot '{slot_name}' has non-numeric supported_min/supported_max",
        )
        return
    if supported_min >= supported_max:
        result.add(
            severity,
            "invalid_supported_table_bounds",
            issue_path,
            f"Numeric SLOT_SCHEMA slot '{slot_name}' requires supported_min lower than supported_max",
        )


def _lint_table_axis_bounds(
    table_axis_slots: set[str],
    schema: dict,
    selection_rules: str,
    selected_outputs: str,
    safety_rules: str,
    selection_mode: str,
    severity: str,
    issue_path: str,
    result: LintResult,
):
    if selection_mode != "table_lookup":
        return

    rules_extrapolation_allowed = bool(_EXTRAPOLATION_ALLOWED_RE.search(selection_rules or ""))
    rules_extrapolation_method = bool(_EXTRAPOLATION_METHOD_RE.search(selection_rules or ""))
    policy_safety_note = bool(
        _SAFETY_NOTE_RE.search(selection_rules or "")
        or _SAFETY_NOTE_RE.search(selected_outputs or "")
        or _SAFETY_NOTE_RE.search(safety_rules or "")
    )

    for slot_name in sorted(table_axis_slots):
        spec = schema.get(slot_name)
        if not isinstance(spec, dict) or str(spec.get("type", "")).lower() != "number":
            continue

        slot_extrapolation_allowed = (
            rules_extrapolation_allowed
            or _truthy(spec.get("extrapolation_allowed"))
            or _truthy(spec.get("allow_extrapolation"))
        )
        slot_extrapolation_method = (
            rules_extrapolation_method
            or bool(spec.get("extrapolation_method"))
            or bool(spec.get("extrapolation_policy"))
        )
        slot_safety_note = (
            policy_safety_note
            or bool(spec.get("safety_note"))
            or bool(spec.get("extrapolation_safety_note"))
        )

        has_supported_bounds = (
            spec.get("supported_min") is not None
            and spec.get("supported_max") is not None
        )
        if has_supported_bounds:
            continue
        if slot_extrapolation_allowed and slot_extrapolation_method and slot_safety_note:
            continue
        if slot_extrapolation_allowed and not slot_extrapolation_method:
            result.add(
                severity,
                "unsafe_table_extrapolation_policy",
                issue_path,
                f"Table lookup axis '{slot_name}' allows extrapolation but does not declare an extrapolation method",
            )
            continue
        if slot_extrapolation_allowed and slot_extrapolation_method and not slot_safety_note:
            result.add(
                severity,
                "unsafe_table_extrapolation_policy",
                issue_path,
                f"Table lookup axis '{slot_name}' allows extrapolation but does not declare a safety note",
            )
            continue
        result.add(
            severity,
            "missing_supported_table_bounds",
            issue_path,
            f"Table lookup numeric axis '{slot_name}' must declare supported_min/supported_max or an explicit extrapolation policy",
        )


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1"}


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


def _central_supported_alias_terms(alias_data: dict) -> set[str]:
    terms = set()
    for category in ("drugs", "conditions"):
        entries = alias_data.get(category, {})
        if not isinstance(entries, dict):
            continue
        for key, item in entries.items():
            if not isinstance(item, dict):
                continue
            aliases = item.get("aliases", [])
            if not isinstance(aliases, list):
                aliases = []
            for term in [key, item.get("display"), item.get("canonical"), *aliases]:
                if isinstance(term, str) and term.strip():
                    terms.add(term.strip().lower())
    return terms


def _lint_aliases_json(path: str, result: LintResult, all_aliases: dict | None = None):
    issue_path = os.path.normpath(os.path.abspath(path))
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            alias_data = json.load(f)
    except Exception as exc:
        result.add("ERROR", "aliases_json_parse_error", issue_path, f"Could not parse aliases.json: {exc}")
        return

    policies = alias_data.get("unsupported_syndromes", {})
    if policies is None:
        return
    if not isinstance(policies, dict):
        result.add("ERROR", "unsupported_policy_invalid", issue_path,
                   "unsupported_syndromes must be an object keyed by canonical unsupported syndrome")
        return

    supported_terms = _central_supported_alias_terms(alias_data)
    if all_aliases:
        supported_terms.update(all_aliases.keys())

    for key, entry in policies.items():
        policy_path = f"{issue_path}::{key}"
        if not isinstance(entry, dict):
            result.add("ERROR", "unsupported_policy_invalid", policy_path,
                       "Unsupported policy entry must be an object")
            continue

        raw_terms = entry.get("terms", [])
        terms = [
            term.strip().lower()
            for term in raw_terms
            if isinstance(term, str) and term.strip()
        ] if isinstance(raw_terms, list) else []
        if not terms:
            result.add("ERROR", "unsupported_policy_empty_terms", policy_path,
                       "Unsupported policy entry must declare nonempty terms")

        message = entry.get("message")
        if not isinstance(message, str) or not message.strip():
            result.add("ERROR", "unsupported_policy_missing_message", policy_path,
                       "Unsupported policy entry must declare a nonempty message")

        if entry.get("allow_supported_alias_collision"):
            continue
        collisions = sorted(set(terms) & supported_terms)
        if collisions:
            result.add("ERROR", "unsupported_policy_collision", policy_path,
                       "Unsupported policy terms collide with supported aliases: "
                       + ", ".join(collisions))


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
    _lint_aliases_json(os.path.join(proto_dir, "aliases.json"), result, all_aliases)
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
