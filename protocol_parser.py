"""
protocol_parser.py — Protocol file parser for both old and new schema.

Old schema panels (legacy, kept for backward compat):
  ANSWER_POLICY, REQUIRED_INFORMATION, PREFERRED_INFORMATION,
  MODIFIER_INFORMATION, DEFAULT_QUESTION, PATHWAY_PRIORITY,
  TREATMENT_PATHWAYS, SAFETY_NOTES, PROTOCOL_LINKS, DECISION_TREE

New schema panels (canonical as of guide v1):
  INTENTS, INPUT_SLOTS, DEFAULT_ANSWER, SELECTION_RULES,
  SELECTED_OUTPUTS, LINKS, INFO_BLOCKS, RESTRICTED_OUTPUTS,
  SAFETY_RULES, OUTPUT_TEMPLATES

Shared panels (both schemas):
  METADATA, ALIASES, ROUTE_CLAIMS, DEFAULT_FOOTER

Public API
----------
parse_protocol_file(path)       -> panel dict
_parse_protocol_text(text, path) -> panel dict  (testable, no IO)
extract_policy_header(text)     -> str          (for LLM gating)
CANONICAL_PANELS                list[str]
POLICY_SECTIONS                 set[str]
"""

import json
import os
import re

# ---------------------------------------------------------------------------
# Valid metadata values
# ---------------------------------------------------------------------------

VALID_ANSWER_MODES = {
    "default_then_selected_output",
    "required_slots_then_selected_output",
    "tree_then_selected_output",
    "info_only",
}

VALID_SELECTION_MODES = {
    "none",
    "priority_rules",
    "table_lookup",
    "decision_tree",
    "pcr_mapping",
    "organism_mapping_with_spectrum_escalation",
}

# ---------------------------------------------------------------------------
# Section headers used for gating/policy (old schema names, still relevant)
# ---------------------------------------------------------------------------

POLICY_SECTIONS = {
    "ANSWER_POLICY",
    "DEFAULT_QUESTION",
    "REQUIRED_INFORMATION",
    "PATHWAY_PRIORITY",
}

# ---------------------------------------------------------------------------
# Canonical panel list — old panels first (legacy compat), then new panels
# ---------------------------------------------------------------------------

CANONICAL_PANELS = [
    # ── Shared ──────────────────────────────────────────────────────────────
    "METADATA",
    "ALIASES",
    "ROUTE_CLAIMS",
    "DEFAULT_FOOTER",

    # ── New schema ──────────────────────────────────────────────────────────
    "INTENTS",
    "INPUT_SLOTS",
    "SLOT_SCHEMA",
    "SLOT_ALIASES",
    "DEFAULT_ANSWER",
    "SELECTION_RULES",
    "PCR_ORGANISM_MAPPING",
    "PCR_ORGANISM_ALIASES",
    "PCR_RESISTANCE_MARKER_ALIASES",
    "PCR_RESISTANCE_RULES",
    "PCR_CONTEXT_NOTES",
    "SELECTED_OUTPUTS",
    "LINKS",
    "INFO_BLOCKS",
    "RESTRICTED_OUTPUTS",
    "SAFETY_RULES",
    "OUTPUT_TEMPLATES",

    # ── Old schema (legacy, kept for compat) ────────────────────────────────
    "ANSWER_POLICY",
    "REQUIRED_INFORMATION",
    "PREFERRED_INFORMATION",
    "MODIFIER_INFORMATION",
    "DEFAULT_QUESTION",
    "PATHWAY_PRIORITY",
    "DECISION_TREE",
    "TREATMENT_PATHWAYS",
    "SAFETY_NOTES",
    "PROTOCOL_LINKS",
]

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_PANEL_HEADER_RE = re.compile(
    r"^##[ \t]+([A-Z_][A-Z0-9_ ]*?)[ \t]*$",
    re.MULTILINE,
)

_NONE_BODY_RE = re.compile(r"^\s*\(none\)\s*$", re.IGNORECASE)

# New LINKS block: each link starts with `LINK: name` (unindented)
_LINK_START_RE = re.compile(r"^LINK:[ \t]+(\S+)[ \t]*$", re.MULTILINE)

# Old PROTOCOL_LINKS: `label -> file [via: key1, key2]`
_OLD_LINK_RE = re.compile(r"^(\S+)\s*->\s*(\S+)(?:\s+via:\s*(.+))?$")

# Decision tree patterns (unchanged from original)
_TREE_NODE_RE  = re.compile(r"^NODE:[ \t]*(\w+)[ \t]*$", re.MULTILINE)
_TREE_ROOT_RE  = re.compile(r"^ROOT:[ \t]*(\w+)[ \t]*$", re.MULTILINE)
_TREE_KEY_RE   = re.compile(r"^([ \t]*)([A-Z_][A-Z_0-9]*):[ \t]*(.*?)[ \t]*$")
_TREE_BRANCH_RE = re.compile(r"^[ \t]*(\S+)[ \t]*->[ \t]*(\S+)[ \t]*$")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def parse_protocol_file(path):
    """Read a protocol .txt and return its canonical-panel dict."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    parsed = _parse_protocol_text(text, path=path)
    _attach_route_claims_sidecar(parsed, path)
    return parsed


def extract_policy_header(text):
    """Return the concatenated text of all POLICY_SECTIONS found in `text`.

    Used to prepend gating rules to the LLM context regardless of what
    semantic search returned.
    """
    pattern = re.compile(r"^##\s+([A-Z_]+)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return ""

    kept = []
    for i, m in enumerate(matches):
        name = m.group(1)
        if name not in POLICY_SECTIONS:
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        kept.append(text[start:end].strip())
    return "\n\n".join(kept)


# ---------------------------------------------------------------------------
# Internal parser
# ---------------------------------------------------------------------------

def _parse_protocol_text(text, path="<inline>"):
    """Parse protocol text into a canonical-panel dict.

    Returns a dict with these keys
    ────────────────────────────────────────────────────────────────────────
    path                  str
    warnings              list[str]        non-fatal parse issues

    Shared panels
    metadata              dict[str, str]   parsed key:value from ## METADATA
    aliases               str
    route_claims          dict             parsed from sidecar or ## ROUTE_CLAIMS

    New-schema panels (str unless noted)
    intents               str
    input_slots           str
    slot_schema           dict
    default_answer        str
    selection_rules       str
    selected_outputs      str
    links                 dict             {name: {key: val, ...}}  (new format)
    info_blocks           str
    restricted_outputs    str
    safety_rules          str
    output_templates      str
    default_footer        str | None

    Old-schema panels (str unless noted, kept for legacy compat)
    answer_policy         str
    required_information  str
    preferred_information str
    modifier_information  str
    default_question      str | None
    pathway_priority      str
    decision_tree         dict | None      see parse_decision_tree()
    treatment_pathways    str
    safety_notes          str
    protocol_links        dict             {label: {file, ctx_keys}} (old format)

    Unrecognised panels
    free_form             dict[str, str]   keyed by normalised panel name
    """
    result = {
        "path":     path,
        "warnings": [],

        # shared
        "metadata":       {},
        "aliases":        "",
        "route_claims":   {},
        "default_footer": None,

        # new schema
        "intents":           "",
        "input_slots":       "",
        "slot_schema":       {},
        "slot_aliases":      {},
        "default_answer":    "",
        "selection_rules":   "",
        "pcr_organism_mapping": {},
        "pcr_organism_aliases": {},
        "pcr_resistance_marker_aliases": {},
        "pcr_resistance_rules": {},
        "pcr_context_notes": {},
        "selected_outputs":  "",
        "links":             {},
        "info_blocks":       "",
        "restricted_outputs": "",
        "safety_rules":      "",
        "output_templates":  "",

        # old schema (legacy)
        "answer_policy":         "",
        "required_information":  "",
        "preferred_information": "",
        "modifier_information":  "",
        "default_question":      None,
        "pathway_priority":      "",
        "decision_tree":         None,
        "treatment_pathways":    "",
        "safety_notes":          "",
        "protocol_links":        {},

        "free_form": {},
    }

    matches = list(_PANEL_HEADER_RE.finditer(text))
    if not matches:
        return result

    seen_canonical = []
    for i, m in enumerate(matches):
        raw_name = m.group(1).strip()
        name = re.sub(r"[ \t]+", "_", raw_name).upper()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()

        if _NONE_BODY_RE.match(body):
            body = ""

        if name not in CANONICAL_PANELS:
            result["free_form"][name] = body
            continue

        seen_canonical.append(name)

        # ── Panels requiring structured parsing ──────────────────────────────
        if name == "METADATA":
            result["metadata"] = _parse_metadata_block(body, path, result["warnings"])
        elif name == "ROUTE_CLAIMS":
            result["route_claims"] = _parse_route_claims_block(body, path, result["warnings"])
        elif name == "DEFAULT_FOOTER":
            result["default_footer"] = body or None
        elif name == "DEFAULT_QUESTION":
            result["default_question"] = body or None
        elif name == "DECISION_TREE":
            result["decision_tree"] = parse_decision_tree(body) if body else None
        elif name == "LINKS":
            result["links"] = _parse_links_block(body, path, result["warnings"])
        elif name == "SLOT_SCHEMA":
            result["slot_schema"] = _parse_slot_schema_block(body, path, result["warnings"])
        elif name == "SLOT_ALIASES":
            result["slot_aliases"] = _parse_slot_aliases_block(body, path, result["warnings"])
        elif name == "PCR_ORGANISM_MAPPING":
            result["pcr_organism_mapping"] = _parse_pcr_organism_mapping(body, path, result["warnings"])
        elif name == "PCR_ORGANISM_ALIASES":
            result["pcr_organism_aliases"] = _parse_named_alias_block(
                body, "Organism", path, result["warnings"]
            )
        elif name == "PCR_RESISTANCE_MARKER_ALIASES":
            result["pcr_resistance_marker_aliases"] = _parse_named_alias_block(
                body, "Marker", path, result["warnings"]
            )
        elif name == "PCR_RESISTANCE_RULES":
            result["pcr_resistance_rules"] = _parse_rule_block(body, path, result["warnings"])
        elif name == "PCR_CONTEXT_NOTES":
            result["pcr_context_notes"] = _parse_rule_block(body, path, result["warnings"])
        elif name == "PROTOCOL_LINKS":
            result["protocol_links"] = _parse_protocol_links(body)
        # ── Plain-text panels ────────────────────────────────────────────────
        else:
            key = name.lower()
            result[key] = body

    # ── Warn if any new-schema panel landed in free_form ────────────────────
    NEW_SCHEMA_PANELS = {
        "INTENTS", "INPUT_SLOTS", "SLOT_SCHEMA", "DEFAULT_ANSWER", "SELECTION_RULES",
        "SLOT_ALIASES", "PCR_ORGANISM_MAPPING", "PCR_ORGANISM_ALIASES",
        "PCR_RESISTANCE_MARKER_ALIASES", "PCR_RESISTANCE_RULES", "PCR_CONTEXT_NOTES",
        "ROUTE_CLAIMS", "SELECTED_OUTPUTS", "LINKS", "INFO_BLOCKS", "RESTRICTED_OUTPUTS",
        "SAFETY_RULES", "OUTPUT_TEMPLATES",
    }
    for ff_name in result["free_form"]:
        if ff_name in NEW_SCHEMA_PANELS:
            result["warnings"].append(
                f"{path}: new-schema panel '{ff_name}' unexpectedly landed in free_form"
            )

    return result


# ---------------------------------------------------------------------------
# METADATA block parser
# ---------------------------------------------------------------------------

def _parse_metadata_block(text, path="<inline>", warnings=None):
    """Parse `key: value` lines from a ## METADATA panel.

    Also validates answer_mode and selection_mode and appends warnings.
    """
    if warnings is None:
        warnings = []
    meta = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        meta[key.strip().lower()] = val.strip()

    # ── Validate answer_mode ─────────────────────────────────────────────────
    answer_mode = meta.get("answer_mode", "")
    if answer_mode and answer_mode not in VALID_ANSWER_MODES:
        warnings.append(
            f"{path}: unknown answer_mode '{answer_mode}'. "
            f"Valid modes: {sorted(VALID_ANSWER_MODES)}"
        )

    # ── Validate selection_mode ──────────────────────────────────────────────
    selection_mode = meta.get("selection_mode", "")
    if selection_mode and selection_mode not in VALID_SELECTION_MODES:
        warnings.append(
            f"{path}: unknown selection_mode '{selection_mode}'. "
            f"Valid modes: {sorted(VALID_SELECTION_MODES)}"
        )

    return meta


# ---------------------------------------------------------------------------
# ROUTE_CLAIMS parser
# ---------------------------------------------------------------------------

_ROUTE_CLAIMS_SIDECAR_SUFFIX = ".route_claims.json"
_ROUTE_CLAIMS_LIST_FIELDS = {
    "intents",
    "subjects",
    "requires",
    "excludes",
    "clarify_if_missing",
    "opt_out",
}


def _route_claims_sidecar_path(path):
    root, _ext = os.path.splitext(path)
    return root + _ROUTE_CLAIMS_SIDECAR_SUFFIX


def _attach_route_claims_sidecar(parsed, path):
    sidecar = _route_claims_sidecar_path(path)
    if not os.path.exists(sidecar):
        return

    with open(sidecar, "r", encoding="utf-8") as f:
        claims = json.load(f)

    if parsed.get("route_claims"):
        parsed.setdefault("warnings", []).append(
            f"{path}: ROUTE_CLAIMS sidecar overrides inline ROUTE_CLAIMS section"
        )
    parsed["route_claims"] = claims


def _parse_route_claims_block(text, path="<inline>", warnings=None):
    """Parse the minimal ROUTE_CLAIMS block shape.

    This intentionally supports only the small YAML-like subset used by route
    claims: top-level lists, plus an ``owns`` mapping whose entries are either
    lists or ``source: parser_field`` objects.
    """
    if warnings is None:
        warnings = []
    if not text:
        return {}

    claims = {}
    current_top = None
    current_owns_key = None

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()

        if indent == 0 and stripped.endswith(":"):
            current_top = stripped[:-1].strip().lower()
            current_owns_key = None
            if current_top in _ROUTE_CLAIMS_LIST_FIELDS:
                claims.setdefault(current_top, [])
            elif current_top == "owns":
                claims.setdefault("owns", {})
            else:
                claims.setdefault(current_top, None)
            continue

        if current_top in _ROUTE_CLAIMS_LIST_FIELDS and indent >= 2:
            if stripped.startswith("- "):
                claims.setdefault(current_top, []).append(stripped[2:].strip())
            continue

        if current_top != "owns":
            continue

        if indent == 2 and stripped.endswith(":"):
            current_owns_key = stripped[:-1].strip().lower()
            claims.setdefault("owns", {}).setdefault(current_owns_key, [])
            continue

        if indent >= 4 and current_owns_key:
            owns = claims.setdefault("owns", {})
            if stripped.startswith("- "):
                if not isinstance(owns.get(current_owns_key), list):
                    owns[current_owns_key] = []
                owns[current_owns_key].append(stripped[2:].strip())
            elif stripped.startswith("source:"):
                owns[current_owns_key] = {"source": stripped.split(":", 1)[1].strip()}

    return claims


# ---------------------------------------------------------------------------
# New LINKS block parser
# ---------------------------------------------------------------------------

def _parse_links_block(text, path="<inline>", warnings=None):
    """Parse a ## LINKS panel body (new schema).

    Each link block starts with `LINK: name` and contains indented
    key-value pairs. List values follow the key with `  - item` lines.

    Returns::

        {
          "ceftriaxone_dosing": {
            "link_type": "antimicrobial_dosing",
            "target_protocol_id": "ceftriaxone",
            "target_file": "protocols/ceftriaxone.txt",
            "target_missing_behavior": "...",
            "trigger_intents": ["dosing_request", "link_request"],
            "transfer_slots": ["gfr", "egfr", ...],
            ...
          },
          ...
        }
    """
    if warnings is None:
        warnings = []
    if not text:
        return {}

    link_matches = list(_LINK_START_RE.finditer(text))
    if not link_matches:
        return {}

    result = {}
    for i, m in enumerate(link_matches):
        name = m.group(1)
        body_start = m.end()
        body_end = link_matches[i + 1].start() if i + 1 < len(link_matches) else len(text)
        body = text[body_start:body_end]
        result[name] = _parse_link_entry(body)

    return result


def _parse_link_entry(text):
    """Parse the body of one LINK: block into a flat dict.

    Keys are lowercased; list values accumulate from `  - item` lines.
    """
    entry = {}
    current_key = None
    in_list = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("- ") and current_key is not None and in_list:
            entry[current_key].append(stripped[2:].strip())
        elif ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip().lower().replace(" ", "_").replace("-", "_")
            val = val.strip()
            current_key = key
            if val:
                entry[key] = val
                in_list = False
            else:
                entry[key] = []
                in_list = True
        elif stripped.startswith("- ") and current_key is not None:
            # List item after a key that had no inline value
            if not isinstance(entry.get(current_key), list):
                entry[current_key] = []
            entry[current_key].append(stripped[2:].strip())
            in_list = True

    return entry


# ---------------------------------------------------------------------------
# SLOT_SCHEMA parser
# ---------------------------------------------------------------------------

_SLOT_START_RE = re.compile(r"^SLOT:[ \t]+(\S+)[ \t]*$", re.MULTILINE)


def _parse_slot_schema_block(text, path="<inline>", warnings=None):
    """Parse a ## SLOT_SCHEMA panel body.

    Format is intentionally close to LINKS:

        SLOT: body_weight_kg
          type: number
          unit: kg
          clinical_min: 1
          clinical_max: 300
          supported_min: 40
          supported_max: 100
          out_of_supported_policy: review_plus_nearest_explicit_row

    Returns {slot_name: {key: value}} with numeric-looking bounds converted
    to floats and list-valued keys preserved as lists.
    """
    if warnings is None:
        warnings = []
    if not text:
        return {}

    slot_matches = list(_SLOT_START_RE.finditer(text))
    if not slot_matches:
        return {}

    result = {}
    for i, m in enumerate(slot_matches):
        name = m.group(1).strip().lower()
        body_start = m.end()
        body_end = slot_matches[i + 1].start() if i + 1 < len(slot_matches) else len(text)
        result[name] = _parse_slot_schema_entry(text[body_start:body_end])
    return result


def _parse_slot_schema_entry(text):
    entry = {}
    current_key = None
    in_list = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("- ") and current_key is not None:
            if not isinstance(entry.get(current_key), list):
                entry[current_key] = []
            entry[current_key].append(stripped[2:].strip())
            in_list = True
            continue

        if ":" not in stripped:
            continue

        key, _, val = stripped.partition(":")
        key = key.strip().lower().replace(" ", "_").replace("-", "_")
        val = val.strip()
        current_key = key
        if val:
            entry[key] = _parse_slot_schema_value(val)
            in_list = False
        else:
            entry[key] = []
            in_list = True

    return entry


def _parse_slot_schema_value(value):
    lower = value.lower()
    if lower in {"true", "yes"}:
        return True
    if lower in {"false", "no"}:
        return False
    try:
        return float(value)
    except ValueError:
        return value


# ---------------------------------------------------------------------------
# SLOT_ALIASES parser
# ---------------------------------------------------------------------------

_NAMED_ALIAS_START_TEMPLATE = r"^{label}:[ \t]+(.+?)[ \t]*$"


def _parse_slot_aliases_block(text, path="<inline>", warnings=None):
    """Parse categorical slot aliases.

    Expected format::

        SLOT: cdiff_request_type
          diagnosis:
            - diagnosis
            - toxin
          treatment:
            - treatment

    Returns {slot_name: {slot_value: [alias, ...]}}.
    """
    if warnings is None:
        warnings = []
    if not text:
        return {}

    slot_matches = list(_SLOT_START_RE.finditer(text))
    if not slot_matches:
        return {}

    result = {}
    for i, m in enumerate(slot_matches):
        slot_name = m.group(1).strip().lower()
        body_start = m.end()
        body_end = slot_matches[i + 1].start() if i + 1 < len(slot_matches) else len(text)
        values = _parse_slot_alias_entry(text[body_start:body_end])
        if values:
            result[slot_name] = values
    return result


def _parse_slot_alias_entry(text):
    values = {}
    current_value = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") and current_value:
            values.setdefault(current_value, []).append(stripped[2:].strip())
            continue
        if stripped.endswith(":"):
            current_value = stripped[:-1].strip().lower()
            values.setdefault(current_value, [])
    return values


# ---------------------------------------------------------------------------
# PCR panel parsers
# ---------------------------------------------------------------------------

def _parse_markdown_table_block(text):
    rows = []
    header = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.match(r"^[-:]+$", c) for c in cells if c):
            continue
        if header is None:
            header = [c.lower().replace(" ", "_").replace("-", "_") for c in cells]
            continue
        if len(cells) >= len(header):
            rows.append(dict(zip(header, cells)))
    return rows


def _parse_pcr_organism_mapping(text, path="<inline>", warnings=None):
    if warnings is None:
        warnings = []
    result = {}
    for row in _parse_markdown_table_block(text):
        organism = (row.get("organism") or row.get("canonical_entity") or "").strip()
        if not organism:
            continue
        key = organism.lower()
        result[key] = {
            "organism": organism,
            "entity_type": row.get("entity_type", ""),
            "baseline_therapy": row.get("baseline_therapy") or row.get("recommended_therapy", ""),
            "base_tier": row.get("base_tier", ""),
            "notes": row.get("notes", ""),
        }
    return result


def _parse_named_alias_block(text, label, path="<inline>", warnings=None):
    if warnings is None:
        warnings = []
    if not text:
        return {}
    start_re = re.compile(_NAMED_ALIAS_START_TEMPLATE.format(label=re.escape(label)), re.MULTILINE)
    matches = list(start_re.finditer(text))
    result = {}
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        aliases = []
        for line in text[body_start:body_end].splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                aliases.append(stripped[2:].strip())
        if aliases:
            result[name.lower()] = aliases
    return result


def _parse_rule_block(text, path="<inline>", warnings=None):
    if warnings is None:
        warnings = []
    if not text:
        return {}
    rule_matches = list(re.finditer(r"^RULE:[ \t]+(\S+)[ \t]*$", text, re.MULTILINE))
    result = {}
    for i, m in enumerate(rule_matches):
        name = m.group(1).strip()
        body_start = m.end()
        body_end = rule_matches[i + 1].start() if i + 1 < len(rule_matches) else len(text)
        result[name] = _parse_rule_entry(text[body_start:body_end])
    return result


def _parse_rule_entry(text):
    entry = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        entry[key.strip().lower()] = value.strip()
    return entry


# ---------------------------------------------------------------------------
# Old PROTOCOL_LINKS parser (legacy, kept for compat)
# ---------------------------------------------------------------------------

def _parse_protocol_links(text):
    """Parse an old-schema ## PROTOCOL_LINKS panel.

    Each line has the form::
        label -> protocols/foo.txt [via: key1, key2]
    """
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _OLD_LINK_RE.match(line)
        if not m:
            continue
        label    = m.group(1)
        file_    = m.group(2)
        ctx_keys = [k.strip() for k in m.group(3).split(",")] if m.group(3) else []
        result[label] = {"file": file_, "ctx_keys": ctx_keys}
    return result


# ---------------------------------------------------------------------------
# Decision-tree parser (unchanged from original)
# ---------------------------------------------------------------------------

_TREE_NODE_KEYS = {
    "type", "ask_hu", "ask_en",
    "answer_hu", "answer_en", "answer_ref",
    "next", "then", "hint", "link",
}


def parse_decision_tree(text):
    """Parse the body of a ## DECISION_TREE panel.

    Returns {"root": <node_id>, "nodes": {id: <node dict>}} or None.
    """
    if not text:
        return None

    node_matches = list(_TREE_NODE_RE.finditer(text))
    if not node_matches:
        return None

    root_match = _TREE_ROOT_RE.search(text)
    root = root_match.group(1) if root_match else node_matches[0].group(1)

    nodes = {}
    for i, m in enumerate(node_matches):
        node_id = m.group(1)
        body_start = m.end()
        body_end = node_matches[i + 1].start() if i + 1 < len(node_matches) else len(text)
        nodes[node_id] = _parse_tree_node(node_id, text[body_start:body_end])

    return {"root": root, "nodes": nodes}


def _parse_tree_node(node_id, body):
    """Parse one NODE: block body into a node dict."""
    node = {
        "id":         node_id,
        "type":       None,
        "ask_hu":     None,
        "ask_en":     None,
        "answer_hu":  None,
        "answer_en":  None,
        "answer_ref": None,
        "next":       None,
        "then":       None,
        "hint":       None,
        "link":       [],
        "branches":   {},
        "collect":    [],
    }

    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        m = _TREE_KEY_RE.match(line)
        if not m:
            i += 1
            continue

        indent = len(m.group(1))
        key    = m.group(2).lower()
        value  = m.group(3)

        if key == "branches":
            i += 1
            while i < len(lines):
                sub = lines[i]
                if not sub.strip():
                    i += 1
                    continue
                sub_indent = len(sub) - len(sub.lstrip())
                if sub_indent <= indent:
                    break
                bm = _TREE_BRANCH_RE.match(sub)
                if bm:
                    node["branches"][bm.group(1)] = bm.group(2)
                i += 1
            continue

        if key == "collect":
            i += 1
            item = None
            while i < len(lines):
                sub = lines[i]
                if not sub.strip():
                    i += 1
                    continue
                sub_indent = len(sub) - len(sub.lstrip())
                if sub_indent <= indent:
                    break
                stripped = sub.strip()
                if stripped.startswith("- "):
                    if item is not None:
                        node["collect"].append(item)
                    item = {}
                    rest = stripped[2:]
                    if ":" in rest:
                        ck, cv = rest.split(":", 1)
                        item[ck.strip().lower()] = cv.strip()
                else:
                    if ":" in stripped:
                        ck, cv = stripped.split(":", 1)
                        if item is None:
                            item = {}
                        item[ck.strip().lower()] = cv.strip()
                i += 1
            if item is not None:
                node["collect"].append(item)
            continue

        if value == "|":
            i += 1
            block_lines = []
            block_indent = None
            while i < len(lines):
                sub = lines[i]
                if not sub.strip():
                    block_lines.append("")
                    i += 1
                    continue
                sub_indent = len(sub) - len(sub.lstrip())
                if block_indent is None:
                    block_indent = sub_indent
                if sub_indent < block_indent:
                    break
                block_lines.append(sub[block_indent:])
                i += 1
            value = "\n".join(block_lines).strip()

        if key in _TREE_NODE_KEYS:
            if key == "link":
                node["link"].append(value)
            else:
                node[key] = value or None

        i += 1

    return node
