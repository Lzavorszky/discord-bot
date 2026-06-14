"""
selection_engine.py  — Deterministic protocol selection engine (Session 9)

Supported selection_mode values (from protocol METADATA):
  priority_rules                            — CAP, meropenem, ampsul
  table_lookup                              — TMP/SMX
  organism_mapping_with_spectrum_escalation — BioFire PN
  decision_tree                             — handled in telegram_bot.py
  none                                      — fall through to RAG
"""

import re
import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SelectionResult:
    output_key:    Optional[str] = None
    output_data:   dict          = field(default_factory=dict)
    missing_slots: list          = field(default_factory=list)
    default_used:  bool          = False
    mode_used:     str           = "none"
    rendered:      Optional[str] = None
    ask_missing:   Optional[str] = None
    no_match:      bool          = False
    render_vars:   dict          = field(default_factory=dict)


_OUTPUT_SECTION_RE = re.compile(r"^###\s+(\S+)\s*$", re.MULTILINE)
_TABLE_AXIS_SLOT_RE = re.compile(
    r"^\s*(?:WEIGHT_SLOT|NUMERIC_AXIS|TABLE_AXIS|AXIS_SLOT):\s*([A-Za-z_][A-Za-z0-9_]*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_TABLE_KEY_RE = re.compile(r"^\s*TABLE_KEY:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
_IF_MISSING_RE = re.compile(r"^\s*IF_MISSING:\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", re.IGNORECASE | re.MULTILINE)
_EXTRAPOLATION_ALLOWED_RE = re.compile(
    r"\b(?:extrapolation_allowed|allow_extrapolation)\s*:\s*(?:true|yes)\b",
    re.IGNORECASE,
)
_EXTRAPOLATION_METHOD_RE = re.compile(
    r"\b(?:extrapolation_method|extrapolation_policy)\s*:\s*\S+",
    re.IGNORECASE,
)
_SAFETY_NOTE_RE = re.compile(r"\b(?:safety_note|extrapolation_safety_note)\s*:\s*\S+", re.IGNORECASE)


def _parse_selected_outputs_panel(text):
    if not text:
        return {}
    result = {}
    matches = list(_OUTPUT_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        key = m.group(1)
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        result[key] = _parse_output_section_body(body, key)
    return result


def _parse_output_section_body(body, section_key=""):
    entry = {"_key": section_key, "_raw": body}
    current_key = None
    current_list = []
    table_lines = []
    in_table = False
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("|"):
            if not in_table:
                if current_key is not None and current_list:
                    entry[current_key] = current_list
                    current_key = None
                    current_list = []
                in_table = True
            table_lines.append(stripped)
            continue
        elif in_table:
            if table_lines:
                entry["_table_rows"] = _parse_markdown_table(table_lines)
            table_lines = []
            in_table = False
            if not stripped:
                continue
        if not stripped:
            if current_key is not None and current_list:
                entry[current_key] = current_list
            current_key = None
            current_list = []
            continue
        if stripped.startswith("- ") and current_key is not None:
            current_list.append(stripped[2:].strip())
            continue
        if ":" in stripped:
            if current_key is not None and current_list:
                entry[current_key] = current_list
                current_list = []
            k, _, v = stripped.partition(":")
            k = k.strip().lower()
            v = v.strip()
            current_key = k
            if v:
                entry[k] = v
                current_key = None
                current_list = []
            else:
                current_list = []
    if in_table and table_lines:
        entry["_table_rows"] = _parse_markdown_table(table_lines)
    if current_key is not None and current_list:
        entry[current_key] = current_list
    return entry


def _parse_markdown_table(lines):
    rows = []
    header = None
    for line in lines:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.match(r"^[-:]+$", c.strip()) for c in cells if c.strip()):
            continue
        if header is None:
            header = [c.lower().replace(" ", "_") for c in cells]
        else:
            if header and len(cells) >= len(header):
                rows.append(dict(zip(header, cells)))
    return rows


_TEMPLATE_SECTION_RE = re.compile(r"^###\s+(\S+)\s*$", re.MULTILINE)


def _parse_output_templates_panel(text):
    if not text:
        return {}
    result = {}
    matches = list(_TEMPLATE_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        name = m.group(1)
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        result[name] = text[body_start:body_end].strip()
    return result


def _eval_condition(cond, slots):
    cond = cond.strip()
    if " OR " in cond:
        return any(_eval_condition(p.strip(), slots) for p in cond.split(" OR "))
    if " AND " in cond:
        return all(_eval_condition(p.strip(), slots) for p in cond.split(" AND "))
    m = re.match(r"(\w+)\s*(==|!=|>=|<=|>|<)\s*(\S+)", cond)
    if not m:
        return False
    slot_name = m.group(1).lower()
    op = m.group(2)
    expected = m.group(3).lower()
    if slot_name == "no_selection_input_supplied":
        return bool(slots.get("_no_selection_input"))
    actual = slots.get(slot_name)
    if op in (">", ">=", "<", "<="):
        try:
            a, b = float(str(actual)), float(expected)
            return (a > b if op == ">" else a >= b if op == ">=" else a < b if op == "<" else a <= b)
        except (TypeError, ValueError):
            return False
    actual_str = str(actual).lower() if actual is not None else None
    if op == "==":
        return actual_str == expected if actual is not None else False
    if op == "!=":
        return actual_str != expected if actual is not None else True
    return False


def _parse_priority_rules(text):
    rules = []
    current = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("RULE:"):
            if current:
                rules.append(current)
            current = {"name": s[5:].strip(), "if": "", "priority": 0, "select": ""}
        elif current:
            if s.startswith("IF:"):
                current["if"] = s[3:].strip()
            elif s.startswith("PRIORITY:"):
                try: current["priority"] = int(s[9:].strip())
                except ValueError: pass
            elif s.startswith("SELECT:"):
                current["select"] = s[7:].strip()
    if current:
        rules.append(current)
    return rules


def _run_priority_rules(selection_rules_text, outputs, slots):
    rules = _parse_priority_rules(selection_rules_text)
    if not rules:
        return SelectionResult(no_match=True, mode_used="priority_rules")
    non_default = [r for r in rules if r["name"] != "DEFAULT"]
    selection_present = any(_eval_condition(r["if"], slots) for r in non_default if r["if"])
    slots2 = dict(slots)
    slots2["_no_selection_input"] = not selection_present
    best = None
    for rule in rules:
        if rule["if"] and _eval_condition(rule["if"], slots2):
            if best is None or rule["priority"] > best["priority"]:
                best = rule
    if best is None:
        return SelectionResult(default_used=True, mode_used="priority_rules", render_vars=dict(slots))
    select_key = best["select"]
    if select_key == "DEFAULT_ANSWER":
        return SelectionResult(default_used=True, mode_used="priority_rules", render_vars=dict(slots))
    output_data = outputs.get(select_key, {})
    return SelectionResult(
        output_key=select_key, output_data=output_data, mode_used="priority_rules",
        render_vars={**slots, **output_data, "selected_output": select_key},
    )


_INDICATION_RULES = [
    ("PROPHYLAXIS", r"\b(prophylaxis|prophylactic|immunosuppressed|immunosuppression|hematology|haematology|transplant)\b"),
    ("HIGH_DOSE", r"\b(pcp|pjp|pneumocystis|steno|stenotrophomonas|bloodstream.infection|bsi|bacteraemia|bacteremia|nocardia)\b"),
    ("MODERATE_DOSE", r"\b(severe|cns|meningitis|brain.abscess|bone|joint|osteomyelitis|septic.arthritis|refractory|icu|critically.ill|deep.seated)\b"),
    ("STANDARD_DOSE", r"\b(standard|susceptible|non.septic|nonseptic|oral.step.down|stepdown|uncomplicated)\b"),
]


def _classify_indication_tier(indication):
    if not indication:
        return None
    text = indication.lower()
    for tier, pattern in _INDICATION_RULES:
        if re.search(pattern, text):
            return tier
    return None


def _classify_renal_category(slots):
    if slots.get("ihd") is True or str(slots.get("ihd", "")).lower() == "true":
        return "IHD"
    if slots.get("crrt") is True or str(slots.get("crrt", "")).lower() == "true":
        return "GFR_GT_30_OR_CRRT"
    gfr = slots.get("gfr")
    if gfr is not None:
        try:
            g = float(gfr)
            if g > 30: return "GFR_GT_30_OR_CRRT"
            if 15 <= g <= 30: return "GFR_15_TO_30"
            return "GFR_LT_15_WITHOUT_CRRT"
        except (ValueError, TypeError):
            pass
    return None


def _find_weight_row(table_rows, weight_kg):
    if not table_rows:
        return None
    best_row, best_delta = None, float("inf")
    for row in table_rows:
        row_weight = _numeric_table_row_value(row, "weight")
        if row_weight is None:
            continue
        delta = abs(row_weight - weight_kg)
        if delta < best_delta:
            best_delta = delta
            best_row = row
    return best_row


def _numeric_table_row_value(row, preferred_keyword=None):
    if not row:
        return None
    cell = None
    if preferred_keyword:
        for k, v in row.items():
            if preferred_keyword.lower() in k.lower():
                cell = v
                break
    if cell is None:
        cell = next(iter(row.values()), None)
    m = re.search(r"(\d+(?:\.\d+)?)", str(cell or ""))
    return float(m.group(1)) if m else None


def _normalize_axis_text(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _axis_tokens(value):
    return [t for t in _normalize_axis_text(value).split("_") if t]


def _slot_axis_labels(slot_name, spec=None):
    spec = spec or {}
    labels = {slot_name}
    for key in ("table_header", "axis_header", "header", "label", "display_name"):
        if spec.get(key):
            labels.add(str(spec.get(key)))
    tokens = _axis_tokens(slot_name)
    if tokens:
        labels.add(" ".join(tokens))
    if tokens and tokens[-1] in {"kg", "mg", "ml", "min", "day", "hr", "h"}:
        labels.add(" ".join(tokens[:-1]))
    if "weight" in tokens and "adjusted" not in tokens:
        labels.update({"weight", "body weight"})
    return {_normalize_axis_text(label) for label in labels if _normalize_axis_text(label)}


def _table_header_matches_slot(header, slot_name, spec=None):
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


def _numeric_table_row_value_for_slot(row, slot_name, spec=None):
    if not row:
        return None
    for key, value in row.items():
        if _table_header_matches_slot(key, slot_name, spec):
            m = re.search(r"[-+]?\d+(?:\.\d+)?", str(value or ""))
            return float(m.group(0)) if m else None
    return None


def _table_numeric_bounds(table_rows, preferred_keyword=None):
    values = [
        v for v in (
            _numeric_table_row_value(row, preferred_keyword)
            for row in table_rows or []
        )
        if v is not None
    ]
    if not values:
        return None, None
    return min(values), max(values)


def _table_numeric_bounds_for_slot(table_rows, slot_name, spec=None):
    values = [
        v for v in (
            _numeric_table_row_value_for_slot(row, slot_name, spec)
            for row in table_rows or []
        )
        if v is not None
    ]
    if not values:
        return None, None
    return min(values), max(values)


def _table_axis_slots_from_rules(selection_rules):
    return {
        match.group(1).lower()
        for match in _TABLE_AXIS_SLOT_RE.finditer(selection_rules or "")
    }


def _detect_table_axis_slots_from_outputs(parsed, outputs=None, selected_output=None):
    schema = parsed.get("slot_schema") or {}
    numeric_slots = {
        name: spec for name, spec in schema.items()
        if isinstance(spec, dict) and str(spec.get("type", "")).lower() == "number"
    }
    if not numeric_slots:
        return set()
    entries = [selected_output] if selected_output else list((outputs or {}).values())
    axes = set()
    for entry in entries:
        if not entry:
            continue
        rows = entry.get("_table_rows") or []
        if not rows:
            continue
        headers = list(rows[0].keys())
        for slot_name, spec in numeric_slots.items():
            if any(_table_header_matches_slot(header, slot_name, spec) for header in headers):
                axes.add(slot_name)
    return axes


def _table_lookup_axis_slots(parsed, outputs=None, selected_output=None):
    return (
        _table_axis_slots_from_rules(parsed.get("selection_rules", ""))
        | _detect_table_axis_slots_from_outputs(parsed, outputs=outputs, selected_output=selected_output)
    )


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1"}


def _explicit_extrapolation_policy(parsed, output_data, slot_name, spec):
    raw_parts = [
        parsed.get("selection_rules", ""),
        parsed.get("safety_rules", ""),
        output_data.get("_raw", "") if output_data else "",
    ]
    raw = "\n".join(raw_parts)
    allowed = (
        _truthy((spec or {}).get("extrapolation_allowed"))
        or _truthy((spec or {}).get("allow_extrapolation"))
        or bool(_EXTRAPOLATION_ALLOWED_RE.search(raw))
    )
    method = (
        bool((spec or {}).get("extrapolation_method"))
        or bool((spec or {}).get("extrapolation_policy"))
        or bool(_EXTRAPOLATION_METHOD_RE.search(raw))
    )
    safety_note = (
        bool((spec or {}).get("safety_note"))
        or bool((spec or {}).get("extrapolation_safety_note"))
        or bool(_SAFETY_NOTE_RE.search(raw))
    )
    return allowed and method and safety_note


def _explicit_extrapolation_allowed(parsed, output_data):
    raw_parts = [
        parsed.get("selection_rules", ""),
        output_data.get("_raw", "") if output_data else "",
        str(output_data.get("extrapolation_allowed", "")) if output_data else "",
        str(output_data.get("allow_extrapolation", "")) if output_data else "",
    ]
    raw = "\n".join(raw_parts).lower()
    if re.search(r"\b(?:extrapolation_allowed|allow_extrapolation)\s*:\s*(?:true|yes)\b", raw):
        return True
    if re.search(r"\b(?:allow|allows|permitted)\s+extrapolat", raw):
        return True
    return False


def _slot_supported_bounds(spec):
    if not isinstance(spec, dict):
        return None, None
    try:
        if spec.get("supported_min") is None or spec.get("supported_max") is None:
            return None, None
        return float(spec.get("supported_min")), float(spec.get("supported_max"))
    except (TypeError, ValueError):
        return None, None


def _find_nearest_axis_row(table_rows, slot_name, value, spec=None):
    best_row, best_delta, best_value = None, float("inf"), None
    for row in table_rows or []:
        row_value = _numeric_table_row_value_for_slot(row, slot_name, spec)
        if row_value is None:
            continue
        delta = abs(row_value - value)
        if delta < best_delta:
            best_row, best_delta, best_value = row, delta, row_value
    return best_row, best_value


def _out_of_supported_message(spec, output_data, direction):
    keys = []
    if direction == "low":
        keys.extend(("out_of_supported_low_message", "out_of_supported_lower_message", "below_supported_message"))
    elif direction == "high":
        keys.extend(("out_of_supported_high_message", "out_of_supported_upper_message", "above_supported_message"))
    keys.append("out_of_supported_message")
    for source in (output_data or {}, spec or {}):
        for key in keys:
            value = source.get(key)
            if value:
                return str(value)
    return None


def _apply_table_axis_range_guard(parsed, slots, output_data, render_vars, axis_slots=None):
    schema = parsed.get("slot_schema") or {}
    table_rows = output_data.get("_table_rows", []) if output_data else []
    axis_slots = axis_slots or _table_lookup_axis_slots(parsed, selected_output=output_data)
    for slot_name in sorted(axis_slots):
        spec = schema.get(slot_name) if isinstance(schema.get(slot_name), dict) else {}
        if slot_name not in slots or slots.get(slot_name) is None:
            continue
        try:
            entered = float(str(slots.get(slot_name)).replace(str(spec.get("unit", "")), "").strip())
        except (TypeError, ValueError):
            continue

        supported_min, supported_max = _slot_supported_bounds(spec)
        explicit_min, explicit_max = _table_numeric_bounds_for_slot(table_rows, slot_name, spec)
        min_bound = supported_min if supported_min is not None else explicit_min
        max_bound = supported_max if supported_max is not None else explicit_max
        if min_bound is None or max_bound is None:
            continue
        if min_bound <= entered <= max_bound:
            continue
        if _explicit_extrapolation_policy(parsed, output_data, slot_name, spec):
            continue

        row, row_value = _find_nearest_axis_row(table_rows, slot_name, entered, spec)
        nearest_row_data = {k.lower().replace(" ", "_"): v for k, v in (row or {}).items()}
        direction = "low" if entered < min_bound else "high"
        render_vars.update({
            "table_bound_slot": spec.get("display_name") or spec.get("label") or _slot_display_name(slot_name),
            "table_bound_unit": spec.get("unit", ""),
            "entered_bound_value": entered,
            "table_min_value": min_bound,
            "table_max_value": max_bound,
            "nearest_row_value": row_value,
            "nearest_row_data": nearest_row_data,
            "out_of_table_range": True,
            "review_required": True,
            "out_of_supported_direction": direction,
            "out_of_supported_message": _out_of_supported_message(spec, output_data, direction),
        })
        return render_vars
    return render_vars


def _pick_col(row, keyword):
    for k, v in row.items():
        if keyword.lower() in k.lower():
            return v
    return None


def _slot_display_name(slot_name):
    return str(slot_name).replace("_", " ")


def _slot_schema_number_bounds(parsed, slots):
    schema = parsed.get("slot_schema") or {}
    for slot_name, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        if str(spec.get("type", "")).lower() != "number":
            continue
        if slot_name not in slots or slots.get(slot_name) is None:
            continue
        try:
            value = float(slots.get(slot_name))
        except (TypeError, ValueError):
            continue
        unit = spec.get("unit", "")
        unit_suffix = f" {unit}" if unit else ""

        clinical_min = spec.get("clinical_min")
        clinical_max = spec.get("clinical_max")
        if ((clinical_min is not None and value < float(clinical_min))
                or (clinical_max is not None and value > float(clinical_max))):
            low = _fmt_number(clinical_min) if clinical_min is not None else "-infinity"
            high = _fmt_number(clinical_max) if clinical_max is not None else "infinity"
            rendered = (
                f"The supplied {_slot_display_name(slot_name)} ({_fmt_number(value)}{unit_suffix}) "
                f"is outside the expected clinical bounds for this protocol ({low}-{high}{unit_suffix}).\n"
                "Please confirm or correct this value before dosing."
            )
            return SelectionResult(
                output_key="SLOT_OUT_OF_CLINICAL_BOUNDS",
                output_data={"type": "slot_bounds", "slot": slot_name},
                mode_used="slot_bounds",
                rendered=rendered,
                render_vars={**slots, "out_of_bounds_slot": slot_name},
            )
    return None


def _parse_table_key_template(selection_rules):
    m = _TABLE_KEY_RE.search(selection_rules or "")
    return m.group(1).strip() if m else ""


def _render_table_key(template, render_vars):
    key = template
    for name, value in render_vars.items():
        key = key.replace("{" + name + "}", str(value))
    return key if "{" not in key and "}" not in key else ""


def _single_table_output_key(outputs):
    table_keys = [
        key for key, data in outputs.items()
        if data.get("_table_rows")
    ]
    return table_keys[0] if len(table_keys) == 1 else ""


def _generic_table_lookup_missing_slots(parsed, slots, outputs):
    required = {
        m.group(1).lower()
        for m in _IF_MISSING_RE.finditer(parsed.get("selection_rules", "") or "")
    }
    required |= _table_lookup_axis_slots(parsed, outputs=outputs)
    return [slot for slot in sorted(required) if slots.get(slot) is None]


def _row_render_vars_for_axes(parsed, output_data, slots, axis_slots):
    schema = parsed.get("slot_schema") or {}
    table_rows = output_data.get("_table_rows", []) if output_data else []
    best_row = None
    for slot_name in sorted(axis_slots):
        if slots.get(slot_name) is None:
            continue
        spec = schema.get(slot_name) if isinstance(schema.get(slot_name), dict) else {}
        try:
            entered = float(str(slots.get(slot_name)).replace(str(spec.get("unit", "")), "").strip())
        except (TypeError, ValueError):
            continue
        best_row, _ = _find_nearest_axis_row(table_rows, slot_name, entered, spec)
        if best_row:
            break
    if not best_row:
        return {}
    return {k.lower().replace(" ", "_"): v for k, v in best_row.items()}


def _run_generic_table_lookup(parsed, slots, outputs):
    missing = _generic_table_lookup_missing_slots(parsed, slots, outputs)
    if missing:
        return SelectionResult(missing_slots=missing, default_used=True, mode_used="table_lookup")

    table_key_template = _parse_table_key_template(parsed.get("selection_rules", ""))
    table_key = _render_table_key(table_key_template, slots) if table_key_template else ""
    if not table_key:
        table_key = _single_table_output_key(outputs)
    output_data = outputs.get(table_key, {}) if table_key else {}
    if not output_data:
        return SelectionResult(default_used=True, mode_used="table_lookup", render_vars=dict(slots))

    axis_slots = _table_lookup_axis_slots(parsed, outputs=outputs, selected_output=output_data)
    row_vars = _row_render_vars_for_axes(parsed, output_data, slots, axis_slots)
    rvars = {**slots, **output_data, **row_vars, "selected_output": table_key}
    _apply_table_axis_range_guard(parsed, slots, output_data, rvars, axis_slots=axis_slots)
    return SelectionResult(output_key=table_key, output_data=output_data, mode_used="table_lookup", render_vars=rvars)


def _uses_builtin_tmpsmx_table_lookup(parsed):
    rules = parsed.get("selection_rules", "")
    return (
        "USE_RULE_SET: INDICATION_RULES" in rules
        or "USE_RULE_SET: RENAL_RULES" in rules
        or "{indication_tier}_{renal_category}" in rules
    )


_TMPSMX_RENAL_DISPLAY = {
    "GFR_GT_30_OR_CRRT": "GFR >30 or CRRT",
    "GFR_15_TO_30": "GFR 15-30",
    "GFR_LT_15_WITHOUT_CRRT": "GFR <15 (no CRRT)",
    "IHD": "IHD",
}


def _tmpsmx_renal_display(renal_category):
    return _TMPSMX_RENAL_DISPLAY.get(renal_category, renal_category or "not provided")


def _tmpsmx_prophylaxis_result(outputs, slots, indication_tier, renal_category):
    if renal_category == "IHD":
        table_key = "IHD"
    elif renal_category == "GFR_LT_15_WITHOUT_CRRT":
        table_key = "GFR_LT_15_WITHOUT_CRRT"
    elif renal_category == "GFR_15_TO_30":
        table_key = "PROPHYLAXIS_GFR_15_TO_30"
    elif renal_category == "GFR_GT_30_OR_CRRT":
        table_key = "PROPHYLAXIS_GFR_GT_30_OR_CRRT"
    else:
        table_key = "PROPHYLAXIS_GENERAL"

    output_data = outputs.get(table_key, {})
    practical_options = output_data.get("practical_options") or []
    practical_dose = "; ".join(practical_options) if practical_options else output_data.get("practical_dose", "")
    rvars = {
        **slots,
        **output_data,
        "indication_tier": indication_tier,
        "renal_category": _tmpsmx_renal_display(renal_category),
        "practical_dose": practical_dose,
    }
    return SelectionResult(output_key=table_key, output_data=output_data, mode_used="table_lookup", render_vars=rvars)


def _tmpsmx_framework_sections(outputs, indication_tier, renal_category):
    if renal_category in {"GFR_GT_30_OR_CRRT", "GFR_15_TO_30"}:
        keys = [f"{indication_tier}_{renal_category}"]
    else:
        keys = [
            f"{indication_tier}_GFR_GT_30_OR_CRRT",
            f"{indication_tier}_GFR_15_TO_30",
        ]
    sections = []
    for key in keys:
        data = outputs.get(key, {})
        if data:
            sections.append({
                "key": key,
                "renal_category": _tmpsmx_renal_display(key.replace(f"{indication_tier}_", "")),
                "data": data,
            })
    return sections


def _tmpsmx_treatment_framework_result(outputs, slots, indication_tier, renal_category, missing):
    sections = _tmpsmx_framework_sections(outputs, indication_tier, renal_category)
    target = ""
    for section in sections:
        target = section["data"].get("target") or target
        if target:
            break
    output_data = {
        "type": "tmpsmx_treatment_framework",
        "target": target,
    }
    return SelectionResult(
        output_key=f"{indication_tier}_GENERAL",
        output_data=output_data,
        mode_used="table_lookup",
        render_vars={
            **slots,
            **output_data,
            "indication_tier": indication_tier,
            "renal_category": _tmpsmx_renal_display(renal_category),
            "missing_for_exact_dose": ", ".join(missing),
            "framework_sections": sections,
        },
    )


def _run_table_lookup(parsed, slots):
    outputs = _parse_selected_outputs_panel(parsed.get("selected_outputs", ""))
    if not _uses_builtin_tmpsmx_table_lookup(parsed):
        return _run_generic_table_lookup(parsed, slots, outputs)

    indication_raw = slots.get("indication") or slots.get("indication_text") or ""
    body_weight_kg = slots.get("body_weight_kg")
    has_renal = (slots.get("gfr") is not None or slots.get("crrt") is not None or slots.get("ihd") is not None)
    if not indication_raw:
        return SelectionResult(missing_slots=["indication"], default_used=True, mode_used="table_lookup")
    indication_tier = _classify_indication_tier(indication_raw)
    if not indication_tier:
        return SelectionResult(missing_slots=["indication (could not classify)"], default_used=True, mode_used="table_lookup")
    renal_category = _classify_renal_category(slots) if has_renal else None
    if indication_tier == "PROPHYLAXIS":
        return _tmpsmx_prophylaxis_result(outputs, slots, indication_tier, renal_category)
    missing_for_exact = []
    if body_weight_kg is None:
        missing_for_exact.append("body_weight_kg")
    if not has_renal or not renal_category:
        missing_for_exact.append("renal_function (GFR/CRRT/IHD)")
    if renal_category == "IHD":
        od = outputs.get("IHD", {})
        return SelectionResult(output_key="IHD", output_data=od, mode_used="table_lookup",
            render_vars={**slots, **od, "indication_tier": indication_tier, "renal_category": "IHD", "body_weight_kg": str(body_weight_kg)})
    if renal_category == "GFR_LT_15_WITHOUT_CRRT":
        od = outputs.get("GFR_LT_15_WITHOUT_CRRT", {})
        return SelectionResult(output_key="GFR_LT_15_WITHOUT_CRRT", output_data=od, mode_used="table_lookup",
            render_vars={**slots, **od, "indication_tier": indication_tier, "renal_category": "GFR <15 (no CRRT)", "body_weight_kg": str(body_weight_kg)})
    if missing_for_exact:
        return _tmpsmx_treatment_framework_result(outputs, slots, indication_tier, renal_category, missing_for_exact)
    table_key = f"{indication_tier}_{renal_category}"
    output_data = outputs.get(table_key, {})
    if not output_data:
        return SelectionResult(default_used=True, mode_used="table_lookup",
            render_vars={**slots, "indication_tier": indication_tier, "renal_category": renal_category})
    weight_row = {}
    try:
        wf = float(str(body_weight_kg).replace("kg","").strip())
        table_rows = output_data.get("_table_rows", [])
        row = _find_weight_row(table_rows, wf)
        if row:
            weight_row = {k.lower().replace(" ","_"): v for k, v in row.items()}
    except (ValueError, TypeError):
        pass
    practical_dose = (
        _pick_col(weight_row, "practical")
        or weight_row.get("practical_dose")
        or output_data.get("practical_dose")
        or "see table"
    )
    total_daily = (
        _pick_col(weight_row, "total")
        or weight_row.get("total_daily_tmp/smx")
        or output_data.get("total_daily_tmp_smx")
        or "see table"
    )
    renal_display = _tmpsmx_renal_display(renal_category)
    target = output_data.get("target") or ("fixed practical dose" if output_data.get("type") == "fixed_dose" else "")
    rvars = {**slots, **output_data, "indication_tier": indication_tier, "renal_category": renal_display,
             "target": target,
             "body_weight_kg": str(body_weight_kg), "practical_dose": practical_dose, "total_daily_tmp_smx": total_daily}
    _apply_table_axis_range_guard(parsed, slots, output_data, rvars, axis_slots={"body_weight_kg"})
    return SelectionResult(output_key=table_key, output_data=output_data, mode_used="table_lookup", render_vars=rvars)


_TIER_OUTPUT_KEY = {1: "TIER_1_CEFTRIAXONE", 2: "TIER_2_CEFEPIME", 3: "TIER_3_ERTAPENEM", 4: "TIER_4_MEROPENEM_COLISTIN"}


def _canonical_token(name):
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")


def _therapy_agents(therapy):
    agents = []
    for part in re.split(r"\s*\+\s*|,\s*", str(therapy or "")):
        agent = part.strip()
        if agent and agent not in agents:
            agents.append(agent)
    return agents


def _pcr_alias_map(alias_panel):
    aliases = {}
    for canonical, items in (alias_panel or {}).items():
        canonical_l = str(canonical or "").lower().strip()
        if not canonical_l:
            continue
        aliases[canonical_l] = canonical_l
        for alias in items or []:
            alias_l = str(alias or "").lower().strip()
            if alias_l:
                aliases[alias_l] = canonical_l
    return aliases


def _literal_alias_match(text, alias):
    return bool(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text, re.IGNORECASE))


def _normalize_pcr_organism(parsed, name):
    key = str(name or "").lower().strip()
    if key in (parsed.get("pcr_organism_mapping") or {}):
        return key
    return _pcr_alias_map(parsed.get("pcr_organism_aliases") or {}).get(key)


def _normalize_pcr_marker(parsed, name):
    key = str(name or "").lower().strip()
    legacy = {
        "meca_c": "meca_c_mrej",
        "meca/c": "meca_c_mrej",
        "ctx_m": "ctx_m",
    }
    if key in legacy:
        return legacy[key]
    aliases = _pcr_alias_map(parsed.get("pcr_resistance_marker_aliases") or {})
    return _canonical_token(aliases.get(key, key))


def _extract_pcr_entities(text, parsed):
    organisms = []
    genes = []
    for alias, canonical in sorted(
        _pcr_alias_map(parsed.get("pcr_organism_aliases") or {}).items(),
        key=lambda x: len(x[0]),
        reverse=True,
    ):
        if _literal_alias_match(text, alias) and canonical not in organisms:
            organisms.append(canonical)
    for alias, canonical in sorted(
        _pcr_alias_map(parsed.get("pcr_resistance_marker_aliases") or {}).items(),
        key=lambda x: len(x[0]),
        reverse=True,
    ):
        token = _canonical_token(canonical)
        if _literal_alias_match(text, alias) and token not in genes:
            genes.append(token)
    return organisms, genes


def _ji_pcr_ambiguity(text, parsed_protocol):
    meta = (parsed_protocol or {}).get("metadata", {}) or {}
    if meta.get("protocol_id") != "biofire_joint_infection":
        return None
    text = text or ""
    lower = text.lower()

    if re.search(r"\bklebsiella\b", lower) and not re.search(
        r"\bklebsiella\s+(?:aerogenes|oxytoca|pneumoniae|pn)\b|\bkpn\b",
        lower,
    ):
        return {
            "type": "klebsiella",
            "question": (
                "Which Klebsiella was detected: Klebsiella aerogenes "
                "(cefepime) or Klebsiella oxytoca / Klebsiella pneumoniae group "
                "(ceftriaxone)?"
            ),
        }

    if re.search(r"\benterococcus\b", lower) and not re.search(
        r"\b(?:enterococcus\s+)?faec(?:alis|ium)\b|\be\.?\s*faec(?:alis|ium)\b",
        lower,
    ):
        return {
            "type": "enterococcus",
            "question": (
                "Which Enterococcus was detected: Enterococcus faecalis "
                "(ampicillin + vancomycin) or Enterococcus faecium (linezolid)?"
            ),
        }

    if re.search(r"\b(?:streptococcus|strep)\b", lower) and not re.search(
        r"\b(?:agalactiae|pneumoniae|pneumo|pyogenes|gbs|gas|group\s+[ab]\s+strep)\b",
        lower,
    ):
        return {
            "type": "streptococcus",
            "question": (
                "Which Streptococcus was detected: Streptococcus agalactiae / "
                "Streptococcus pneumoniae (ceftriaxone) or Streptococcus pyogenes "
                "(penicillin + clindamycin)?"
            ),
        }

    return None


def _merge_unique(existing, new_items):
    merged = list(existing or [])
    for item in new_items or []:
        if item not in merged:
            merged.append(item)
    return merged


def _organism_row(parsed, organism):
    return (parsed.get("pcr_organism_mapping") or {}).get(str(organism or "").lower().strip()) or {}


def _organism_tier(row):
    try:
        return int(str(row.get("base_tier") or "").strip())
    except (TypeError, ValueError):
        entity_type = str(row.get("entity_type") or "").lower()
        if entity_type == "atypical":
            return 0
        if entity_type == "virus":
            return -1
        return 1


def _is_gram_negative_row(row):
    text = f"{row.get('entity_type', '')} {row.get('notes', '')}".lower()
    return "gram_negative" in text or "enterobacterales" in text


def _pcr_output_key(outputs, tier, agents):
    agent_set = {str(agent).lower() for agent in agents}
    for key, data in (outputs or {}).items():
        selected = {
            str(agent).lower()
            for agent in data.get("selected_items", [])
            if isinstance(data.get("selected_items", []), list)
        }
        if selected and selected == agent_set:
            return key
    if {"meropenem", "colistin"}.issubset(agent_set):
        return "TIER_4_MEROPENEM_COLISTIN"
    if "meropenem" in agent_set and "TIER_3_MEROPENEM" in outputs:
        return "TIER_3_MEROPENEM"
    if "ertapenem" in agent_set and "TIER_3_ERTAPENEM" in outputs:
        return "TIER_3_ERTAPENEM"
    if "cefepime" in agent_set:
        return "TIER_2_CEFEPIME"
    if "ceftriaxone" in agent_set:
        return "TIER_1_CEFTRIAXONE"
    return _TIER_OUTPUT_KEY.get(tier, "TIER_1_CEFTRIAXONE")


def _run_organism_mapping(parsed, slots):
    return _run_pcr_mapping(parsed, slots)


def _run_pcr_mapping(parsed, slots):
    outputs = _parse_selected_outputs_panel(parsed.get("selected_outputs", ""))
    pathogen_list = slots.get("pathogen_list", [])
    resistance_list = slots.get("resistance_gene_list", [])
    ambiguity = slots.get("pcr_ambiguity")
    if ambiguity and not pathogen_list:
        return SelectionResult(
            output_key="ambiguous_pathogen",
            missing_slots=["detected_pathogen"],
            mode_used="organism_mapping",
            ask_missing=ambiguity.get("question") or "Which pathogen was detected?",
            render_vars=dict(slots),
        )
    if not pathogen_list:
        if resistance_list:
            return SelectionResult(missing_slots=["detected_pathogen"], mode_used="organism_mapping",
                ask_missing="Resistance markers can only be interpreted with a detected pathogen. Which pathogen was positive?")
        return SelectionResult(default_used=True, mode_used="organism_mapping")
    canonical_organisms = []
    for name in pathogen_list:
        c = _normalize_pcr_organism(parsed, name) or str(name or "").lower().strip()
        if c and c not in canonical_organisms and _organism_row(parsed, c):
            canonical_organisms.append(c)
    canonical_genes = []
    for g in resistance_list:
        c = _normalize_pcr_marker(parsed, g)
        if c and c not in canonical_genes:
            canonical_genes.append(c)
    staph_present = "staphylococcus aureus" in canonical_organisms
    mrsa = staph_present and "meca_c_mrej" in canonical_genes
    non_staph = [o for o in canonical_organisms if o != "staphylococcus aureus"]
    gram_negative_present = any(_is_gram_negative_row(_organism_row(parsed, o)) for o in canonical_organisms)
    ctx_m = "ctx_m" in canonical_genes
    carbapenemase = "carbapenemase" in canonical_genes
    items = []
    for o in non_staph:
        row = _organism_row(parsed, o)
        if row:
            items.append((o, _organism_tier(row), _therapy_agents(row.get("baseline_therapy")), row))
    added_agents = []
    appended_notes = []
    forced_backbone = None
    for rule in (parsed.get("pcr_resistance_rules") or {}).values():
        marker = _canonical_token(rule.get("if_marker"))
        if marker and marker not in canonical_genes:
            continue
        action = str(rule.get("action") or "").lower()
        therapy = _therapy_agents(rule.get("therapy"))
        if action in {"set_backbone", "replace_backbone", "replace_gram_negative_backbone"}:
            if action != "replace_gram_negative_backbone" or gram_negative_present:
                forced_backbone = therapy
        elif action == "add_agent":
            for agent in therapy:
                if agent not in added_agents:
                    added_agents.append(agent)
        elif action in {"append_note", "consult_note"} and rule.get("note"):
            appended_notes.append(rule.get("note"))
        if rule.get("note") and action not in {"append_note", "consult_note"}:
            appended_notes.append(rule.get("note"))
    if carbapenemase and not forced_backbone:
        forced_backbone = ["meropenem", "colistin"]
    bacterial_items = [(o, t, d) for o, t, d, _ in items if t >= 1]
    atypical_items  = [(o, t, d) for o, t, d, _ in items if t == 0]
    viral_items     = [(o, t, d) for o, t, d, _ in items if t < 0]
    if ambiguity:
        ask_for_ambiguity = True
        if ambiguity.get("type") == "klebsiella":
            ask_for_ambiguity = not (
                ctx_m
                or carbapenemase
                or any(t >= 2 for _, t, _ in bacterial_items)
            )
        if ask_for_ambiguity:
            return SelectionResult(
                output_key="ambiguous_pathogen",
                missing_slots=["detected_pathogen"],
                mode_used="organism_mapping",
                ask_missing=ambiguity.get("question") or "Which pathogen was detected?",
                render_vars=dict(slots),
            )
    detected_str = ", ".join(pathogen_list) + ((" + " + ", ".join(resistance_list)) if resistance_list else "")
    def _mk(key, hu, en):
        od = outputs.get(key, {})
        rvars = {**slots, **od, "detected_entities": detected_str,
                 "selected_output_answer_en": od.get("answer_en", en),
                 "selected_output_answer_hu": od.get("answer_hu", hu),
                 "spectrum_logic_if_polymicrobial": ""}
        return SelectionResult(output_key=key, output_data=od, mode_used="organism_mapping", render_vars=rvars)
    if not bacterial_items and not atypical_items and viral_items:
        if any(o == "influenza a/b" for o, _, _ in viral_items):
            return _mk("INFLUENZA", "Influenza - oseltamivir.", "Influenza A/B - oseltamivir.")
        return _mk("VIRAL_ONLY", "Csak vírus.", "Viral pathogen only - supportive therapy.")
    if staph_present and not bacterial_items:
        return _mk("STAPH_AUREUS_MRSA" if mrsa else "STAPH_AUREUS_MSSA",
                   ("MRSA - vancomycin." if mrsa else "MSSA - cefazolin."),
                   ("MRSA likely - vancomycin." if mrsa else "MSSA likely - cefazolin."))
    max_tier = 0
    max_drugs = ["ceftriaxone"]
    for o, t, drugs in bacterial_items:
        if t > max_tier:
            max_tier = t
            max_drugs = drugs
    if forced_backbone:
        max_drugs = forced_backbone
        max_tier = 4 if "colistin" in {a.lower() for a in forced_backbone} else max(max_tier, 3)
    if staph_present and mrsa and max_tier < 1:
        max_tier = 1
    strep_pyogenes = "streptococcus pyogenes" in canonical_organisms
    if strep_pyogenes and max_tier <= 2 and len(bacterial_items) == 1:
        return _mk("STREP_PYOGENES", "Strep pyogenes - penicillin + clindamycin.", "Strep pyogenes - penicillin + clindamycin.")
    has_pseudo = "pseudomonas aeruginosa" in canonical_organisms
    if ctx_m and gram_negative_present and has_pseudo and not forced_backbone:
        return _mk("CONFLICTING_REQUIREMENTS", "Konfliktus - ID konzultáció.", "Conflicting requirements - ID consultation.")
    final_agents = max_drugs + [agent for agent in added_agents if agent not in max_drugs]
    output_key = _pcr_output_key(outputs, max_tier, final_agents)
    od = outputs.get(output_key, {})
    answer_en = od.get("answer_en", f"Tier {max_tier} - {' + '.join(final_agents)}.")
    answer_hu = od.get("answer_hu", answer_en)
    if atypical_items:
        note = " + clarithromycin (atypical coverage)"
        answer_en += note; answer_hu += note
    for agent in added_agents:
        if agent.lower() not in answer_en.lower():
            answer_en += f" + {agent}"
            answer_hu += f" + {agent}"
    if appended_notes:
        note_text = " ".join(n for n in appended_notes if n)
        if note_text:
            answer_en += f" {note_text}"
            answer_hu += f" {note_text}"
    spectrum_logic = ""
    if len(bacterial_items) > 1 or ctx_m or carbapenemase:
        parts = [f"{o}: Tier {t}" for o, t, _ in bacterial_items]
        spectrum_logic = "; ".join(parts) + f" -> Tier {max_tier}"
    for rule in (parsed.get("pcr_context_notes") or {}).values():
        context = slots.get("pcr_context") or slots.get("context")
        if context and str(rule.get("if_context") or "").lower() == str(context).lower():
            note = rule.get("note")
            if note:
                spectrum_logic = (spectrum_logic + "; " if spectrum_logic else "") + note
    rvars = {**slots, **od, "detected_entities": detected_str,
             "selected_output_answer_en": answer_en, "selected_output_answer_hu": answer_hu,
             "spectrum_logic_if_polymicrobial": spectrum_logic}
    if staph_present and mrsa:
        rvars["selected_output_answer_en"] += " + vancomycin (MRSA)"
        rvars["selected_output_answer_hu"] += " + vancomycin (MRSA)"
    return SelectionResult(output_key=output_key, output_data=od, mode_used="organism_mapping", render_vars=rvars)


_GFR_RE    = re.compile(
    r"\b(?:GFR|eGFR|CrCl)\s*[=:~]?\s*(\d+(?:\.\d+)?)"
    r"|(\d+(?:\.\d+)?)\s*(?:ml/min|mL/min)\b",
    re.IGNORECASE,
)
_WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg\b", re.IGNORECASE)
_VANCOMYCIN_LEVEL_RE = re.compile(
    r"\b(?:vancomycin|vanco|vankomicin)?\s*(?:level|szint|concentration|conc|tdm)\s*(?:is|at|=|:|~)?\s*(\d+(?:\.\d+)?)"
    r"|(\d+(?:\.\d+)?)\s*(?:ug/l|ug/ml|mcg/l|mcg/ml)\b",
    re.IGNORECASE,
)
_CM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*cm\b", re.IGNORECASE)
_UNIT_VALUE_RE = r"([-+]?\d+(?:\.\d+)?)\s*(mm|cm)\b"
_VELOCITY_VALUE_RE = r"([-+]?\d+(?:\.\d+)?)\s*(m/s|cm/s|mps|cmps)\b"
_LVOT_VTI_RE = re.compile(r"\blvot\s+vti\s*[:=]?\s*" + _UNIT_VALUE_RE, re.IGNORECASE)
_LVOT_DIAMETER_RE = re.compile(
    r"\blvot\s+(?:diam(?:eter)?|d)\s*[:=]?\s*" + _UNIT_VALUE_RE,
    re.IGNORECASE,
)
_AV_VTI_RE = re.compile(r"\b(?:av|aortic(?:\s+valve)?)\s+vti\s*[:=]?\s*" + _UNIT_VALUE_RE, re.IGNORECASE)
_LVOT_CSA_RE = re.compile(r"\blvot\s+(?:csa|area)\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)\s*(?:cm2|cm\^2|cm²)\b", re.IGNORECASE)
_BSA_RE = re.compile(r"\bbsa\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)\s*(?:m2|m\^2|m²)?\b", re.IGNORECASE)
_LVOT_VMAX_RE = re.compile(r"\blvot\s+v(?:max|el(?:ocity)?)\s*[:=]?\s*" + _VELOCITY_VALUE_RE, re.IGNORECASE)
_AV_VMAX_RE = re.compile(r"\b(?:av|aortic(?:\s+valve)?)\s+v(?:max|el(?:ocity)?)\s*[:=]?\s*" + _VELOCITY_VALUE_RE, re.IGNORECASE)
_HR_RE = re.compile(r"\b(?:hr|heart\s*rate)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(?:bpm)?\b", re.IGNORECASE)
_PISA_RADIUS_RE = re.compile(r"\b(?:pisa\s+)?radius\s*[:=]?\s*" + _UNIT_VALUE_RE, re.IGNORECASE)
_ALIASING_VELOCITY_RE = re.compile(
    r"\b(?:aliasing(?:\s+velocity)?|nyquist|va)\s*[:=]?\s*" + _VELOCITY_VALUE_RE,
    re.IGNORECASE,
)
_PEAK_REGURGITANT_VELOCITY_RE = re.compile(
    r"\b(?:peak\s+)?(?:regurg(?:itant)?\s+)?(?:velocity|vmax|v)\s*[:=]?\s*" + _VELOCITY_VALUE_RE,
    re.IGNORECASE,
)
_REGURGITANT_VTI_RE = re.compile(r"\b(?:regurg(?:itant)?|mr|ar|tr|pr)\s+vti\s*[:=]?\s*" + _UNIT_VALUE_RE, re.IGNORECASE)
_EROA_RE = re.compile(r"\b(?:eroa?|effective\s+regurgitant\s+orifice(?:\s+area)?)\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)\s*(?:cm2|cm\^2|cm²)?\b", re.IGNORECASE)
_RVOL_RE = re.compile(r"\b(?:rvol|regurgitant\s+volume|rv)\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)\s*(?:ml|mL)?\b", re.IGNORECASE)
_ANGLE_RE = re.compile(r"\b(?:angle|flow\s+convergence\s+angle)\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)\s*(?:deg|degrees?)?\b", re.IGNORECASE)
_LV_VOLUME_RE = re.compile(r"\blv\s*(edv|esv)\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)\s*(?:ml|mL)?\b", re.IGNORECASE)
_FORWARD_SV_RE = re.compile(r"\bforward\s+(?:sv|stroke\s+volume)\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)\s*(?:ml|mL)?\b", re.IGNORECASE)
_STROKE_VOLUME_PAIR_RE = re.compile(
    r"\b(?:regurgitant\s+valve\s+sv|svreg)\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)\s*(?:ml|mL)?\b.*?"
    r"\b(?:competent\s+valve\s+sv|svcomp)\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)\s*(?:ml|mL)?\b",
    re.IGNORECASE | re.DOTALL,
)
_BOOL_SLOTS = {
    "intubated":       re.compile(r"\b(intubat(?:ed|alt)|mechanically.vent(?:ilated)?)\b", re.IGNORECASE),
    "crrt":            re.compile(r"\bCRRT\b", re.IGNORECASE),
    "ihd":             re.compile(r"\b(IHD|haemodialysis|hemodialysis)\b", re.IGNORECASE),
    "nosocomial_risk": re.compile(r"\b(nosocomial|nozokomiális|HAP|VAP)\b", re.IGNORECASE),
    "influenza":       re.compile(r"\b(influenza|flu)\b", re.IGNORECASE),
    "aspiration_event":re.compile(r"\b(aspiration|aspiratio)\b", re.IGNORECASE),
    "copd_exacerbation":re.compile(r"\bCOPD\b", re.IGNORECASE),
    "atypical_suspicion":re.compile(r"\b(atypical|atipusos)\b", re.IGNORECASE),
    "cns_infection":   re.compile(r"\b(CNS|meningitis|brain\s+abscess|central\s+nervous\s+system)\b", re.IGNORECASE),
    "tdm_low_level":   re.compile(r"\b(low\s+(?:level|levels|exposure)|subtherapeutic|TDM\s+(?:low|below))\b", re.IGNORECASE),
    "septic_shock":   re.compile(r"\b(septic\s+shock|szeptikus\s+sokk)\b", re.IGNORECASE),
    "hypoalbuminemia": re.compile(r"\b(albumin\s*(?:<|below|under)\s*30|albumin\s+30-|hypoalbumin(?:emia|aemia)?)\b", re.IGNORECASE),
}
_PATIENT_STATUS_MAP = [
    (re.compile(r"\b(intubat(?:ed|alt)|mechanically.vent(?:ilated)?)\b", re.IGNORECASE), "intubated"),
    (re.compile(r"\b(hospitali[sz](?:ed|ált|alt)|admitted)\b", re.IGNORECASE), "hospitalized"),
    (re.compile(r"\b(discharg(?:e|eable)|outpatient|ambulant|hazaengedhet)\b", re.IGNORECASE), "dischargeable"),
]
_VIRAL_POS_RE = re.compile(r"\b(viral.positive|viral.test.positive)\b", re.IGNORECASE)
_VIRAL_NEG_RE = re.compile(r"\b(viral.negative|viral.test.negative)\b", re.IGNORECASE)


def extract_slots_from_query(question, parsed_protocol=None, existing_slots=None):
    slots = dict(existing_slots or {})
    text = question
    protocol_id = ""
    if parsed_protocol:
        protocol_id = (parsed_protocol.get("metadata", {}) or {}).get("protocol_id", "")
    gfr_matches = list(_GFR_RE.finditer(text))
    if gfr_matches:
        slots["gfr"] = float(gfr_matches[-1].group(1) or gfr_matches[-1].group(2))
    weight_matches = list(_WEIGHT_RE.finditer(text))
    if weight_matches:
        slots["body_weight_kg"] = float(weight_matches[-1].group(1))
    if protocol_id == "body_size_calculators":
        if weight_matches:
            slots["actual_weight_kg"] = float(weight_matches[-1].group(1))
        cm_matches = list(_CM_RE.finditer(text))
        if cm_matches:
            slots["height_cm"] = float(cm_matches[-1].group(1))
    if protocol_id in {"echo_cardiac_output", "echo_ava", "echo_ero_rvol"}:
        _extract_echo_calculator_slots(text, slots, protocol_id)
    for sn, pat in _BOOL_SLOTS.items():
        if pat.search(text):
            slots[sn] = True
    for pat, status in _PATIENT_STATUS_MAP:
        if pat.search(text):
            slots["patient_status"] = status
            if status == "intubated":
                slots["intubated"] = True
            break
    if _VIRAL_POS_RE.search(text):
        slots["viral_test_result"] = "positive"
    elif _VIRAL_NEG_RE.search(text):
        slots["viral_test_result"] = "negative"
    if parsed_protocol:
        meta = parsed_protocol.get("metadata", {})
        _apply_protocol_slot_aliases(text, parsed_protocol, slots)
        if meta.get("protocol_id") == "tmpsmx":
            indication = _extract_indication_text(text, slots)
            if indication:
                slots["indication"] = indication
                slots["indication_text"] = indication
        if parsed_protocol.get("pcr_organism_aliases") or parsed_protocol.get("pcr_resistance_marker_aliases"):
            ambiguity = _ji_pcr_ambiguity(text, parsed_protocol)
            if ambiguity:
                slots["pcr_ambiguity"] = ambiguity
            organisms, genes = _extract_pcr_entities(text, parsed_protocol)
            if organisms:
                existing_orgs = slots.get("pathogen_list", [])
                slots["pathogen_list"] = _merge_unique(existing_orgs, organisms)
            if genes:
                existing_genes = slots.get("resistance_gene_list", [])
                slots["resistance_gene_list"] = _merge_unique(existing_genes, genes)
        if meta.get("protocol_id") == "endocarditis_antibiotics":
            _extract_endocarditis_slots(text, slots)
        if meta.get("protocol_id") == "vancomycin":
            vm = _VANCOMYCIN_LEVEL_RE.search(text)
            if vm:
                slots["vancomycin_level"] = float(vm.group(1) or vm.group(2))
    return slots


def _set_unit_slot(slots, name, match):
    if not match:
        return
    slots[name] = float(match.group(1))
    slots[f"{name}_unit"] = match.group(2).lower()


def _set_velocity_slot(slots, name, match):
    if not match:
        return
    slots[name] = float(match.group(1))
    slots[f"{name}_unit"] = match.group(2).lower()


def _extract_echo_calculator_slots(text, slots, protocol_id):
    _set_unit_slot(slots, "lvot_vti", _LVOT_VTI_RE.search(text))
    _set_unit_slot(slots, "lvot_diameter", _LVOT_DIAMETER_RE.search(text))
    hm = _HR_RE.search(text)
    if hm:
        slots["heart_rate_bpm"] = float(hm.group(1))

    if protocol_id == "echo_ava":
        _set_unit_slot(slots, "av_vti", _AV_VTI_RE.search(text))
        _set_velocity_slot(slots, "lvot_vmax", _LVOT_VMAX_RE.search(text))
        _set_velocity_slot(slots, "av_vmax", _AV_VMAX_RE.search(text))
        for name, pat in (("lvot_csa", _LVOT_CSA_RE), ("bsa_m2", _BSA_RE)):
            m = pat.search(text)
            if m:
                slots[name] = float(m.group(1))

    if protocol_id == "echo_ero_rvol":
        _set_unit_slot(slots, "pisa_radius", _PISA_RADIUS_RE.search(text))
        _set_velocity_slot(slots, "aliasing_velocity", _ALIASING_VELOCITY_RE.search(text))
        _set_velocity_slot(slots, "peak_regurgitant_velocity", _PEAK_REGURGITANT_VELOCITY_RE.search(text))
        _set_unit_slot(slots, "regurgitant_vti", _REGURGITANT_VTI_RE.search(text))
        _set_unit_slot(slots, "annulus_diameter", re.search(r"\bannulus\s+diam(?:eter)?\s*[:=]?\s*" + _UNIT_VALUE_RE, text, re.IGNORECASE))
        _set_unit_slot(slots, "annulus_vti", re.search(r"\bannulus\s+vti\s*[:=]?\s*" + _UNIT_VALUE_RE, text, re.IGNORECASE))
        for name, pat in (
            ("eroa_cm2", _EROA_RE),
            ("regurgitant_volume_ml", _RVOL_RE),
            ("flow_convergence_angle_degrees", _ANGLE_RE),
            ("forward_stroke_volume", _FORWARD_SV_RE),
        ):
            m = pat.search(text)
            if m:
                slots[name] = float(m.group(1))
        for m in _LV_VOLUME_RE.finditer(text):
            slots[f"lv_{m.group(1).lower()}"] = float(m.group(2))
        pair = _STROKE_VOLUME_PAIR_RE.search(text)
        if pair:
            slots["stroke_volume_regurgitant_valve_ml"] = float(pair.group(1))
            slots["stroke_volume_competent_valve_ml"] = float(pair.group(2))


def _apply_protocol_slot_aliases(text, parsed_protocol, slots):
    for slot_name, values in (parsed_protocol.get("slot_aliases") or {}).items():
        matched_values = set()
        for value, aliases in (values or {}).items():
            for alias in aliases or []:
                if _literal_alias_match(text, str(alias).lower()):
                    matched_values.add(value)
                    break
        if len(matched_values) == 1:
            slots[slot_name] = next(iter(matched_values))
        elif len(matched_values) > 1:
            slots.pop(slot_name, None)


def _extract_indication_text(text, slots):
    lower = text.lower()
    for tier, pattern in _INDICATION_RULES:
        if re.search(pattern, lower):
            m = re.search(pattern, lower)
            return m.group(0) if m else lower
    return None


def _extract_endocarditis_slots(text, slots):
    lower = (text or "").lower()

    if re.search(r"\b(culture[-\s]?negative|blood\s+culture[-\s]?negative|bcnie)\b", lower):
        slots["unsupported_topic"] = "culture_negative"
    elif re.search(r"\b(fungal|candida|aspergillus)\b", lower):
        slots["unsupported_topic"] = "fungal"
    elif re.search(r"\b(opat|oral\s+step[-\s]?down|outpatient)\b", lower):
        slots["unsupported_topic"] = "opat"

    if re.search(r"\b(empiric|empirical)\b", lower):
        slots["treatment_mode"] = "empiric"
    elif re.search(r"\b(targeted|target|culture\s+directed|culture-directed)\b", lower):
        slots["treatment_mode"] = "targeted"

    if re.search(r"\b(penicillin|betalactam|beta[-\s]?lactam)\s+allerg", lower):
        slots["penicillin_allergy"] = True

    early_pve = re.search(
        r"\b(early\s+pve|pve.{0,30}(?:<|less\s+than|under)\s*12\s*months?|"
        r"(?:<|less\s+than|under)\s*12\s*months?.{0,30}pve)\b",
        lower,
    )
    late_pve = re.search(
        r"\b(late\s+pve|pve.{0,30}(?:>=|more\s+than|over|after)\s*12\s*months?|"
        r"(?:>=|more\s+than|over|after)\s*12\s*months?.{0,30}pve)\b",
        lower,
    )
    if early_pve:
        slots["valve_context"] = "early_pve"
        slots["pve_timing"] = "early"
    elif late_pve:
        slots["valve_context"] = "pve"
        slots["pve_timing"] = "late"
    elif re.search(r"\b(pve|prosthetic\s+valve)\b", lower):
        slots["valve_context"] = "pve"
    elif re.search(r"\b(nve|native\s+valve)\b", lower):
        slots["valve_context"] = "nve"

    if re.search(r"\b(vre|vancomycin[-\s]?resistant\s+enterococcus)\b", lower):
        slots["pathogen_group"] = "vre"
        slots["resistance_profile"] = "vre"
        return
    if re.search(r"\bmrsa\b", lower):
        slots["pathogen_group"] = "mrsa"
        slots["resistance_profile"] = "mrsa"
        return
    if re.search(r"\bmssa\b", lower):
        slots["pathogen_group"] = "mssa"
        slots["resistance_profile"] = "mssa"
        return
    if re.search(r"\b(staphylococcus\s+aureus|staph\s+aureus|s\.\s*aureus)\b", lower):
        slots["pathogen_group"] = "staphylococcus_aureus"

    if re.search(r"\b(e(?:nterococcus)?\.\s*faecalis|e(?:nterococcus)?\.\s*faecium|enterococcus|enterococcal)\b", lower):
        slots["pathogen_group"] = "enterococcus"
        if re.search(r"\b(beta[-\s]?lactam\s+sensitive|ampicillin\s+susceptible|ampicillin\s+sensitive)\b", lower):
            slots["resistance_profile"] = "beta_lactam_sensitive"
        elif re.search(r"\b(beta[-\s]?lactam\s+resistant|ampicillin\s+resistant)\b", lower):
            slots["resistance_profile"] = "beta_lactam_resistant_not_vre"

    if re.search(
        r"\b(oral\s+streptococci|viridans\s+strep(?:tococci)?|streptococcus\s+gallolyticus|"
        r"s\.\s*gallolyticus|streptococcus\s+pneumoniae|strep\s+pneumo|"
        r"s\.\s*pneumoniae|pneumococcus|pneumococcal)\b",
        lower,
    ):
        slots["pathogen_group"] = "streptococcus"

    if (
        re.search(r"\bendocarditis\b|\bie\b", lower)
        and re.search(r"\b(pathogen|organism|bacteria|culture)\b", lower)
        and "pathogen_group" not in slots
    ):
        slots["pathogen_group"] = "unsupported"


def _render_template(template_text, render_vars):
    result = template_text
    for k, v in render_vars.items():
        placeholder = "{" + k + "}"
        if placeholder not in result:
            continue
        if isinstance(v, list):
            rendered = "\n".join(f"- {item}" for item in v) if v else ""
        elif v is None:
            rendered = "?"
        else:
            rendered = str(v)
        result = result.replace(placeholder, rendered)
    result = re.sub(r"\{[a-z_]+\}", "?", result)
    return result.strip()


def _fmt_number(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f.is_integer() else str(f)


def _render_out_of_bounds_reference(parsed, result, lang="en"):
    rv = result.render_vars or {}
    if not rv.get("out_of_table_range"):
        return None

    slot = rv.get("table_bound_slot", "value")
    unit = rv.get("table_bound_unit", "")
    entered = _fmt_number(rv.get("entered_bound_value"))
    min_value = _fmt_number(rv.get("table_min_value"))
    max_value = _fmt_number(rv.get("table_max_value"))
    nearest = _fmt_number(rv.get("nearest_row_value"))
    unit_suffix = f" {unit}" if unit else ""
    custom_message = rv.get("out_of_supported_message")

    if lang == "hu":
        lines = [
            f"A megadott {slot} ({entered}{unit_suffix}) az explicit protokolltablazat tartomanyan kivul van "
            f"({min_value}-{max_value}{unit_suffix}).",
            custom_message or "Automatikus dozisemeles nem tamogatott, es ennel az erteknel veszelyes lehet.",
        ]
    else:
        lines = [
            f"Patient {slot} {entered}{unit_suffix} is outside the explicit protocol table range "
            f"({min_value}-{max_value}{unit_suffix}).",
            custom_message or "Automatic dose escalation is not supported and may be unsafe at this value.",
        ]

    nearest_row = rv.get("nearest_row_data") or {}
    if nearest_row and rv.get("nearest_row_value") is not None:
        if lang == "hu":
            lines.extend([
                f"Legkozelebbi explicit protokollsor, csak tajekoztatasra: {nearest}{unit_suffix} sor.",
                f"Ez nem {entered}{unit_suffix}-ra adott dozisjavaslat.",
                "Ehhez a beteghez individualizalt ID/gyogyszereszeti felulvizsgalat szukseges.",
                "",
                "Referencia protokolladat:",
            ])
        else:
            lines.extend([
                f"Closest explicit protocol row for reference only: {nearest}{unit_suffix} row.",
                f"This is not a {entered}{unit_suffix} dosing recommendation.",
                "Use individualized ID/pharmacy review for this patient.",
                "",
                "Reference protocol data:",
            ])
        for key, value in nearest_row.items():
            if value:
                lines.append(f"- {key.replace('_', ' ')}: {value}")
    else:
        lines.append(
            "Use individualized ID/pharmacy review for this patient."
            if lang != "hu"
            else "Ehhez a beteghez individualizalt ID/gyogyszereszeti felulvizsgalat szukseges."
        )

    context_rows = []
    for key in (
        "target", "target_range", "target_mg_kg", "target_mg_kg_day",
        "target_tmp_mg_kg_day", "renal_adjustment", "renal_category", "indication_tier"
    ):
        value = rv.get(key)
        if value:
            context_rows.append((key, value))
    if context_rows:
        lines.append("")
        lines.append("Protocol context:" if lang != "hu" else "Protokoll kontextus:")
        for key, value in context_rows:
            lines.append(f"- {key.replace('_', ' ')}: {value}")

    return "\n".join(lines).strip()


def _markdown_table_from_rows(rows):
    if not rows:
        return ""
    headers = list(rows[0].keys())
    lines = [
        "| " + " | ".join(header.replace("_", " ").title() for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")
    return "\n".join(lines)


def _render_tmpsmx_prophylaxis(result, lang="en"):
    rv = result.render_vars or {}
    od = result.output_data or {}
    if od.get("type") == "renal_warning":
        lines = ["TMP/SMX - PROPHYLAXIS"]
        if rv.get("renal_category"):
            lines.append(f"- renal function: {rv.get('renal_category')}")
        if od.get("recommendation"):
            lines.append(f"- recommendation: {od.get('recommendation')}")
        return "\n".join(lines).strip()

    lines = ["TMP/SMX - PROPHYLAXIS"]
    renal_category = rv.get("renal_category")
    if renal_category and renal_category != "not provided":
        lines.append(f"- renal function: {renal_category}")
    options = od.get("practical_options") or []
    if options:
        lines.append("- practical prophylaxis options:")
        lines.extend(f"  - {option}" for option in options)
    elif rv.get("practical_dose"):
        lines.append(f"- practical prophylaxis dose: {rv.get('practical_dose')}")
    if od.get("recommendation"):
        lines.append(f"- recommendation: {od.get('recommendation')}")
    if renal_category == "not provided" and not od.get("recommendation"):
        lines.append("- provide GFR/CRRT/IHD if renal impairment is suspected or known")
    return "\n".join(lines).strip()


def _render_tmpsmx_treatment_framework(result, lang="en"):
    rv = result.render_vars or {}
    lines = [
        f"TMP/SMX - {rv.get('indication_tier', 'TREATMENT')}",
    ]
    target = rv.get("target")
    if target:
        lines.append(f"- target: {target}")
    missing = rv.get("missing_for_exact_dose")
    if missing:
        lines.append(f"- exact practical dose requires: {missing}")
    lines.append("- send body weight and GFR/CRRT/IHD for the patient-specific practical dose")

    sections = rv.get("framework_sections") or []
    for section in sections:
        data = section.get("data") or {}
        renal_category = section.get("renal_category")
        lines.append("")
        lines.append(f"{renal_category}:")
        section_target = data.get("target")
        if section_target and section_target != target:
            lines.append(f"- target: {section_target}")
        rows = data.get("_table_rows") or []
        if rows:
            lines.append(_markdown_table_from_rows(rows))
        else:
            practical = data.get("practical_dose")
            total = data.get("total_daily_tmp_smx")
            if practical:
                dose_line = f"- practical dose: {practical}"
                if total:
                    dose_line += f" ({total})"
                lines.append(dose_line)
            elif data.get("recommendation"):
                lines.append(f"- recommendation: {data.get('recommendation')}")
    return "\n".join(line for line in lines if line is not None).strip()


def render_selected_output(parsed, result, lang="en"):
    if result.rendered:
        return result.rendered
    if result.default_used:
        da = parsed.get("default_answer", "")
        if da:
            return _pick_lang_section(da, lang)
        return ""
    if not result.output_key:
        return ""
    templates = _parse_output_templates_panel(parsed.get("output_templates", ""))
    lang_suffix = "_HU" if lang == "hu" else "_EN"
    output_type = str(result.output_data.get("type", "")).lower()
    if output_type.startswith("tdm_"):
        tkey = f"TDM_SELECTED{lang_suffix}"
    else:
        tkey = f"FINAL_SELECTED{lang_suffix}"
    template_text = templates.get(tkey) or templates.get("FINAL_SELECTED_EN") or ""
    out_of_bounds = _render_out_of_bounds_reference(parsed, result, lang=lang)
    if out_of_bounds:
        return out_of_bounds
    if output_type == "prophylaxis_fixed_dose" or result.output_key in {
        "PROPHYLAXIS_GENERAL",
        "PROPHYLAXIS_GFR_GT_30_OR_CRRT",
        "PROPHYLAXIS_GFR_15_TO_30",
    }:
        return _render_tmpsmx_prophylaxis(result, lang=lang)
    if output_type == "tmpsmx_treatment_framework":
        return _render_tmpsmx_treatment_framework(result, lang=lang)
    if template_text:
        return _render_template(template_text, result.render_vars)
    return _plain_render(result.output_key, result.output_data, lang)


def _pick_lang_section(text, lang):
    section_re = re.compile(r"^###\s+(HU|EN)\s*$", re.MULTILINE)
    matches = list(section_re.finditer(text))
    preferred = "HU" if lang == "hu" else "EN"
    fallback  = "EN" if lang == "hu" else "HU"
    for label in (preferred, fallback):
        for i, m in enumerate(matches):
            if m.group(1) == label:
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                return text[start:end].strip()
    return text.strip()


def _plain_render(key, data, lang):
    lines = [key]
    for k, v in data.items():
        if k.startswith("_"): continue
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines.extend(f"  - {item}" for item in v)
        else:
            lines.append(f"- {k}: {v}")
    return "\n".join(lines)


_CALCULATOR_PROTOCOL_IDS = {
    "body_size_calculators",
    "echo_cardiac_output",
    "echo_ava",
    "echo_ero_rvol",
}


def _round(value, digits=2):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return value


def _display(value, digits=2):
    return _fmt_number(_round(value, digits))


def _to_cm(value, unit):
    if value is None:
        return None
    unit = (unit or "").lower()
    if unit == "mm":
        return float(value) / 10
    if unit == "cm":
        return float(value)
    return None


def _to_cm_s(value, unit):
    if value is None:
        return None
    unit = (unit or "").lower()
    if unit in {"m/s", "mps"}:
        return float(value) * 100
    if unit in {"cm/s", "cmps"}:
        return float(value)
    return None


def _calculator_result(output_key, rendered, slots, **render_vars):
    return SelectionResult(
        output_key=output_key,
        output_data={"type": "calculator"},
        mode_used="calculator",
        rendered=rendered.strip(),
        render_vars={**slots, **render_vars, "selected_output": output_key},
    )


def _run_body_size_calculator(slots):
    missing = []
    if slots.get("height_cm") is None:
        missing.append("height_cm")
    if slots.get("actual_weight_kg") is None:
        missing.append("actual_weight_kg")
    if missing:
        return SelectionResult(
            output_key="missing_input",
            missing_slots=missing,
            mode_used="calculator",
            ask_missing="Please provide height in cm and actual body weight in kg.",
            render_vars=dict(slots),
        )

    height_cm = float(slots["height_cm"])
    actual_weight_kg = float(slots["actual_weight_kg"])
    height_m = height_cm / 100
    bmi = actual_weight_kg / (height_m * height_m)
    bsa = math.sqrt((height_cm * actual_weight_kg) / 3600)
    ibw_male = 50 + 0.91 * (height_cm - 152.4)
    ibw_female = 45.5 + 0.91 * (height_cm - 152.4)
    adj_male = ibw_male + 0.4 * (actual_weight_kg - ibw_male)
    adj_female = ibw_female + 0.4 * (actual_weight_kg - ibw_female)

    lines = [
        f"Body size calculations for { _display(actual_weight_kg) } kg, { _display(height_cm) } cm:",
        f"- BMI: {_display(bmi)} kg/m2",
        f"- BSA (Mosteller): {_display(bsa)} m2",
        f"- IBW male-formula: {_display(ibw_male)} kg",
        f"- IBW female-formula: {_display(ibw_female)} kg",
        f"- AdjBW40 using male-formula IBW: {_display(adj_male)} kg",
        f"- AdjBW40 using female-formula IBW: {_display(adj_female)} kg",
    ]
    if actual_weight_kg <= ibw_male or actual_weight_kg <= ibw_female:
        lines.append("")
        lines.append("Note: actual weight is at or below at least one IBW result; adjusted body weight may not be the appropriate scalar unless a drug-specific protocol says so.")
    return _calculator_result(
        "calculated_body_size",
        "\n".join(lines),
        slots,
        bmi=bmi,
        bsa_m2=bsa,
        ibw_male_kg=ibw_male,
        ibw_female_kg=ibw_female,
        adjusted_body_weight_male_kg=adj_male,
        adjusted_body_weight_female_kg=adj_female,
    )


def _run_echo_cardiac_output_calculator(slots):
    lvot_diameter_cm = _to_cm(slots.get("lvot_diameter"), slots.get("lvot_diameter_unit"))
    lvot_vti_cm = _to_cm(slots.get("lvot_vti"), slots.get("lvot_vti_unit"))
    missing = []
    if lvot_diameter_cm is None:
        missing.append("lvot_diameter")
    if lvot_vti_cm is None:
        missing.append("lvot_vti")
    if missing:
        return SelectionResult(
            output_key="missing_input",
            missing_slots=missing,
            mode_used="calculator",
            ask_missing="Please provide LVOT diameter and LVOT VTI, with units mm or cm. Add HR if you want cardiac output.",
            render_vars=dict(slots),
        )
    lvot_csa = math.pi * (lvot_diameter_cm / 2) ** 2
    sv = lvot_csa * lvot_vti_cm
    lines = [
        "Echo LVOT stroke volume:",
        f"- LVOT diameter: {_display(lvot_diameter_cm)} cm",
        f"- LVOT CSA: {_display(lvot_csa)} cm2",
        f"- LVOT VTI: {_display(lvot_vti_cm)} cm",
        f"- Stroke volume: {_display(sv)} mL",
    ]
    output_key = "calculated_sv"
    co = None
    if slots.get("heart_rate_bpm") is not None:
        hr = float(slots["heart_rate_bpm"])
        co = sv * hr / 1000
        lines.extend([
            f"- HR: {_display(hr)} bpm",
            f"- Cardiac output: {_display(co)} L/min",
        ])
        output_key = "calculated_co"
    return _calculator_result(
        output_key,
        "\n".join(lines),
        slots,
        lvot_diameter_cm=lvot_diameter_cm,
        lvot_csa_cm2=lvot_csa,
        lvot_vti_cm=lvot_vti_cm,
        stroke_volume_ml=sv,
        cardiac_output_l_min=co,
    )


def _run_echo_ava_calculator(slots):
    lvot_diameter_cm = _to_cm(slots.get("lvot_diameter"), slots.get("lvot_diameter_unit"))
    lvot_vti_cm = _to_cm(slots.get("lvot_vti"), slots.get("lvot_vti_unit"))
    av_vti_cm = _to_cm(slots.get("av_vti"), slots.get("av_vti_unit"))
    lvot_csa = slots.get("lvot_csa")
    if lvot_csa is None and lvot_diameter_cm is not None:
        lvot_csa = math.pi * (lvot_diameter_cm / 2) ** 2

    if lvot_csa is not None and lvot_vti_cm is not None and av_vti_cm is not None:
        ava = float(lvot_csa) * lvot_vti_cm / av_vti_cm
        di = lvot_vti_cm / av_vti_cm
        lines = [
            "Echo AVA by continuity equation:",
            f"- LVOT CSA: {_display(lvot_csa)} cm2",
            f"- LVOT VTI: {_display(lvot_vti_cm)} cm",
            f"- AV VTI: {_display(av_vti_cm)} cm",
            f"- AVA: {_display(ava)} cm2",
            f"- Dimensionless index: {_display(di)}",
        ]
        output_key = "calculated_ava"
        indexed = None
        if slots.get("bsa_m2") is not None:
            indexed = ava / float(slots["bsa_m2"])
            lines.append(f"- Indexed AVA: {_display(indexed)} cm2/m2")
            output_key = "calculated_ava_indexed"
        return _calculator_result(output_key, "\n".join(lines), slots, ava_cm2=ava, dimensionless_index=di, indexed_ava_cm2_m2=indexed)

    lvot_vmax = _to_cm_s(slots.get("lvot_vmax"), slots.get("lvot_vmax_unit"))
    av_vmax = _to_cm_s(slots.get("av_vmax"), slots.get("av_vmax_unit"))
    if lvot_vmax is not None and av_vmax is not None:
        ratio = lvot_vmax / av_vmax
        lines = ["Echo velocity ratio:", f"- Velocity ratio: {_display(ratio)}"]
        if lvot_csa is not None:
            lines.append(f"- Simplified AVA: {_display(float(lvot_csa) * ratio)} cm2")
        lines.append("VTI continuity-equation AVA is preferred when VTI measurements are available.")
        return _calculator_result("calculated_velocity_ratio", "\n".join(lines), slots, velocity_ratio=ratio)

    return SelectionResult(
        output_key="missing_input",
        missing_slots=["lvot_vti", "av_vti", "lvot_diameter_or_lvot_csa"],
        mode_used="calculator",
        ask_missing="Please provide LVOT VTI, AV VTI, and either LVOT diameter or LVOT CSA, with units.",
        render_vars=dict(slots),
    )


def _run_echo_ero_rvol_calculator(slots):
    eroa = slots.get("eroa_cm2")
    rvol = slots.get("regurgitant_volume_ml")
    reg_vti_cm = _to_cm(slots.get("regurgitant_vti"), slots.get("regurgitant_vti_unit"))

    radius_cm = _to_cm(slots.get("pisa_radius"), slots.get("pisa_radius_unit"))
    aliasing_cm_s = _to_cm_s(slots.get("aliasing_velocity"), slots.get("aliasing_velocity_unit"))
    peak_cm_s = _to_cm_s(slots.get("peak_regurgitant_velocity"), slots.get("peak_regurgitant_velocity_unit"))
    if radius_cm is not None and aliasing_cm_s is not None and peak_cm_s is not None:
        angle = slots.get("flow_convergence_angle_degrees")
        pisa_area = 2 * math.pi * radius_cm ** 2
        if angle is not None:
            pisa_area *= float(angle) / 180
        flow = pisa_area * aliasing_cm_s
        eroa_calc = flow / peak_cm_s
        lines = [
            "Echo EROA by PISA:",
            f"- PISA area: {_display(pisa_area)} cm2",
            f"- Regurgitant flow: {_display(flow)} mL/s",
            f"- EROA: {_display(eroa_calc)} cm2",
        ]
        rvol_calc = None
        if reg_vti_cm is not None:
            rvol_calc = eroa_calc * reg_vti_cm
            lines.append(f"- Regurgitant volume: {_display(rvol_calc)} mL")
        else:
            lines.append("Regurgitant VTI is needed to calculate regurgitant volume.")
        return _calculator_result("calculated_pisa_eroa_rvol", "\n".join(lines), slots, eroa_cm2=eroa_calc, regurgitant_volume_ml=rvol_calc)

    if eroa is not None and reg_vti_cm is not None:
        rvol_calc = float(eroa) * reg_vti_cm
        return _calculator_result(
            "calculated_direct_rvol",
            f"Regurgitant volume from EROA and regurgitant VTI:\n- RVol: {_display(rvol_calc)} mL",
            slots,
            regurgitant_volume_ml=rvol_calc,
        )
    if rvol is not None and reg_vti_cm is not None:
        eroa_calc = float(rvol) / reg_vti_cm
        return _calculator_result(
            "calculated_direct_eroa",
            f"EROA from regurgitant volume and regurgitant VTI:\n- EROA: {_display(eroa_calc)} cm2",
            slots,
            eroa_cm2=eroa_calc,
        )

    sv_reg = slots.get("stroke_volume_regurgitant_valve_ml")
    sv_comp = slots.get("stroke_volume_competent_valve_ml")
    if sv_reg is not None and sv_comp is not None:
        rvol_calc = float(sv_reg) - float(sv_comp)
        rf = 100 * rvol_calc / float(sv_reg) if float(sv_reg) else None
        lines = [
            "Echo regurgitant volume by volumetric method:",
            f"- RVol: {_display(rvol_calc)} mL",
            f"- Regurgitant fraction: {_display(rf)}%",
        ]
        if reg_vti_cm is not None:
            lines.append(f"- EROA: {_display(rvol_calc / reg_vti_cm)} cm2")
        return _calculator_result("calculated_volumetric_rvol", "\n".join(lines), slots, regurgitant_volume_ml=rvol_calc, regurgitant_fraction_percent=rf)

    if slots.get("lv_edv") is not None and slots.get("lv_esv") is not None and slots.get("forward_stroke_volume") is not None:
        lv_sv = float(slots["lv_edv"]) - float(slots["lv_esv"])
        rvol_calc = lv_sv - float(slots["forward_stroke_volume"])
        lines = [
            "Echo regurgitant volume by LV-volume method:",
            f"- LV stroke volume: {_display(lv_sv)} mL",
            f"- RVol: {_display(rvol_calc)} mL",
        ]
        if reg_vti_cm is not None:
            lines.append(f"- EROA: {_display(rvol_calc / reg_vti_cm)} cm2")
        return _calculator_result("calculated_volumetric_rvol", "\n".join(lines), slots, regurgitant_volume_ml=rvol_calc)

    return SelectionResult(
        output_key="missing_input",
        missing_slots=["complete_calculation_method"],
        mode_used="calculator",
        ask_missing="Please provide a complete set for one method: PISA inputs, direct EROA/RVol conversion inputs, or volumetric stroke-volume inputs.",
        render_vars=dict(slots),
    )


def _run_calculator(parsed, slots, lang="en"):
    protocol_id = (parsed.get("metadata", {}) or {}).get("protocol_id")
    if protocol_id == "body_size_calculators":
        return _run_body_size_calculator(slots)
    if protocol_id == "echo_cardiac_output":
        return _run_echo_cardiac_output_calculator(slots)
    if protocol_id == "echo_ava":
        return _run_echo_ava_calculator(slots)
    if protocol_id == "echo_ero_rvol":
        return _run_echo_ero_rvol_calculator(slots)
    return SelectionResult(no_match=True, mode_used="calculator")


def run_selection(parsed, slots, lang="en"):
    meta = parsed.get("metadata", {})
    protocol_id = meta.get("protocol_id", "")
    mode = meta.get("selection_mode", "none").lower()
    if protocol_id in _CALCULATOR_PROTOCOL_IDS:
        return _run_calculator(parsed, slots, lang=lang)
    bounds_result = _slot_schema_number_bounds(parsed, slots)
    if bounds_result:
        return bounds_result
    if mode == "priority_rules":
        outputs = _parse_selected_outputs_panel(parsed.get("selected_outputs", ""))
        return _run_priority_rules(parsed.get("selection_rules", ""), outputs, slots)
    if mode == "table_lookup":
        return _run_table_lookup(parsed, slots)
    if mode in {"organism_mapping_with_spectrum_escalation", "pcr_mapping"}:
        return _run_organism_mapping(parsed, slots)
    return SelectionResult(no_match=True, mode_used=mode)
