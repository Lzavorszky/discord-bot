#!/usr/bin/env python3
"""loader.py — load + validate protocol YAML files (Plan D, Phase 2).

YAML in, a validated protocol record (plain dict) out — or a loud failure. The
validator is dependency-light (no `jsonschema`), matching the style in
`run_harness.py`: it returns a list of human-readable problems, and the loader
turns a non-empty list into a `ProtocolError` naming the file and every issue.

Public API
----------
    validate_record(record)        -> list[str]      # [] means valid
    load_protocol(path)            -> dict           # raises ProtocolError
    load_protocol_dir(dirpath)     -> list[(Path, dict)]

Design notes
------------
* "Fail loudly" means: a bad file raises, with the filename and ALL problems in
  the message (not just the first), so a migrator fixes everything in one pass.
* Validation is structural and enum-level, not clinical. Whether a migrated dose
  matches the source `.txt` is the human's non-delegable Phase-2.4 hand-check.
"""
from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml")

# Import the schema enums/rule-table. Insert this dir on sys.path first so the
# module loads whether imported as a package or run directly (mirrors the
# sys.path approach in run_harness.py).
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema import (  # noqa: E402  type: ignore
    KINDS,
    INTENTS,
    SLOT_TYPES,
    OUT_OF_RANGE_ACTIONS,
    STATUSES,
    KIND_REQUIRED,
    KIND_FIELDS,
    COMMON_FIELDS,
)


class ProtocolError(ValueError):
    """Raised when a protocol file is structurally invalid."""


_ID_OK = set("abcdefghijklmnopqrstuvwxyz0123456789_")


def _is_str(v) -> bool:
    return isinstance(v, str)


def _is_list_of_str(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


# --------------------------------------------------------------------------- #
# Per-field validators                                                        #
# --------------------------------------------------------------------------- #
def _check_common(rec: dict, problems: list[str]) -> None:
    cid = rec.get("id")
    if not cid or not _is_str(cid):
        problems.append("missing/empty 'id' (string)")
    elif set(cid) - _ID_OK:
        problems.append(f"'id' {cid!r} must be lowercase a-z, 0-9, underscore")

    kind = rec.get("kind")
    if kind not in KINDS:
        problems.append(f"'kind' {kind!r} not in {list(KINDS)}")

    status = rec.get("status")
    if status is not None and status not in STATUSES:
        problems.append(f"'status' {status!r} not in {list(STATUSES)}")

    for key in ("source_label", "canonical_name", "footer"):
        if key in rec and not _is_str(rec[key]):
            problems.append(f"'{key}' must be a string")

    if "aliases" in rec and not _is_list_of_str(rec["aliases"]):
        problems.append("'aliases' must be a list of strings")

    for key in ("answers_intents", "refuses_intents"):
        if key in rec:
            val = rec[key]
            if not _is_list_of_str(val):
                problems.append(f"'{key}' must be a list of strings")
            else:
                bad = [x for x in val if x not in INTENTS]
                if bad:
                    problems.append(f"'{key}' has unknown intent(s) {bad} "
                                    f"(allowed: {list(INTENTS)})")


def _check_slots(slots, problems: list[str]) -> None:
    if not isinstance(slots, dict):
        problems.append("'slots' must be a mapping of name -> spec")
        return
    for name, spec in slots.items():
        where = f"slot '{name}'"
        if not isinstance(spec, dict):
            problems.append(f"{where}: spec must be a mapping")
            continue
        stype = spec.get("type")
        if stype not in SLOT_TYPES:
            problems.append(f"{where}: type {stype!r} not in {list(SLOT_TYPES)}")
        if stype == "enum":
            vals = spec.get("values") or spec.get("enum")
            if not _is_list_of_str(vals):
                problems.append(f"{where}: enum slot needs 'values' (list of strings)")
        for bound in ("min", "max"):
            if bound in spec and not isinstance(spec[bound], (int, float)):
                problems.append(f"{where}: '{bound}' must be a number")
        oor = spec.get("on_out_of_range")
        if oor is not None and oor not in OUT_OF_RANGE_ACTIONS:
            problems.append(f"{where}: on_out_of_range {oor!r} not in "
                            f"{list(OUT_OF_RANGE_ACTIONS)}")


def _check_select(select, problems: list[str], *, target_key: str,
                  valid_targets: set[str]) -> None:
    """A `select:` ladder for drug_dose (target_key='tier') or pathway
    ('output'). Each entry is a guard (`if` + target) or `default`."""
    if not isinstance(select, list) or not select:
        problems.append("'select' must be a non-empty list (the ordered ladder)")
        return
    saw_default = False
    for i, entry in enumerate(select):
        where = f"select[{i}]"
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        has_default = "default" in entry
        has_if = "if" in entry
        target = entry.get(target_key)
        if has_default:
            saw_default = True
            dflt = entry["default"]
            # 'default' may name a target OR a sentinel like DEFAULT_ANSWER.
            if not _is_str(dflt):
                problems.append(f"{where}: 'default' must be a string")
            continue
        if not has_if:
            problems.append(f"{where}: needs an 'if' guard or a 'default'")
        if target is None:
            problems.append(f"{where}: guard needs a '{target_key}' target")
        elif not _is_str(target):
            problems.append(f"{where}: '{target_key}' must be a string")
        elif valid_targets and target not in valid_targets:
            problems.append(f"{where}: '{target_key}' {target!r} is not a "
                            f"defined {target_key} {sorted(valid_targets)}")
    if not saw_default:
        problems.append("'select' has no terminal {default: ...} rung")


def _check_drug_dose(rec: dict, problems: list[str]) -> None:
    tiers = rec.get("tiers")
    tier_names: set[str] = set()
    if not isinstance(tiers, dict) or not tiers:
        problems.append("'tiers' must be a non-empty mapping of TIER -> {dose, ...}")
    else:
        for tname, tspec in tiers.items():
            tier_names.add(tname)
            if not isinstance(tspec, dict):
                problems.append(f"tier '{tname}': must be a mapping")
            elif "dose" not in tspec or not _is_str(tspec["dose"]):
                problems.append(f"tier '{tname}': missing 'dose' (string)")
    if "slots" in rec:
        _check_slots(rec["slots"], problems)
    if "select" in rec:
        _check_select(rec["select"], problems, target_key="tier",
                      valid_targets=tier_names)
    if "never" in rec and not _is_list_of_str(rec["never"]):
        problems.append("'never' must be a list of strings")
    for key in ("prep", "notes"):
        if key in rec and not _is_str(rec[key]):
            problems.append(f"'{key}' must be a string")


_MARKER_RULES = ("mrsa", "vre", "ctx_m", "carbapenemase")


def _check_pcr_panel(rec: dict, problems: list[str]) -> None:
    organisms = rec.get("organisms")
    if not isinstance(organisms, list) or not organisms:
        problems.append("'organisms' must be a non-empty list")
    else:
        for i, org in enumerate(organisms):
            where = f"organisms[{i}]"
            if not isinstance(org, dict):
                problems.append(f"{where}: must be a mapping")
                continue
            if not org.get("name") or not _is_str(org["name"]):
                problems.append(f"{where}: missing 'name' (string)")
            if "tier" in org and not isinstance(org["tier"], int):
                problems.append(f"{where}: 'tier' must be an integer")
            if "enterobacterales" in org and not isinstance(org["enterobacterales"], bool):
                problems.append(f"{where}: 'enterobacterales' must be a boolean")
            for skey in ("therapy", "entity_type", "answer", "answer_hu",
                         "marker_answer", "marker_answer_hu"):
                if skey in org and not _is_str(org[skey]):
                    problems.append(f"{where}: '{skey}' must be a string")
            if "aliases" in org and not _is_list_of_str(org["aliases"]):
                problems.append(f"{where}: 'aliases' must be a list of strings")
            if "marker_rules" in org and not _is_list_of_str(org["marker_rules"]):
                problems.append(f"{where}: 'marker_rules' must be a list of strings")

    markers = rec.get("markers")
    if markers is not None:
        if not isinstance(markers, list):
            problems.append("'markers' must be a list of marker mappings")
        else:
            for i, mk in enumerate(markers):
                where = f"markers[{i}]"
                if not isinstance(mk, dict):
                    problems.append(f"{where}: must be a mapping")
                    continue
                if not mk.get("name") or not _is_str(mk["name"]):
                    problems.append(f"{where}: missing 'name' (string)")
                rule = mk.get("rule")
                if rule is not None and rule not in _MARKER_RULES:
                    problems.append(f"{where}: rule {rule!r} not in {list(_MARKER_RULES)}")
                for skey in ("therapy", "answer", "note"):
                    if skey in mk and not _is_str(mk[skey]):
                        problems.append(f"{where}: '{skey}' must be a string")
                if "aliases" in mk and not _is_list_of_str(mk["aliases"]):
                    problems.append(f"{where}: 'aliases' must be a list of strings")

    spec = rec.get("spectrum_tiers")
    if spec is not None:
        if not isinstance(spec, dict):
            problems.append("'spectrum_tiers' must be a mapping of tier -> {answer, ...}")
        else:
            for tname, tspec in spec.items():
                if not isinstance(tspec, dict):
                    problems.append(f"spectrum_tiers[{tname}]: must be a mapping")
                    continue
                for skey in ("therapy", "answer", "answer_hu"):
                    if skey in tspec and not _is_str(tspec[skey]):
                        problems.append(f"spectrum_tiers[{tname}]: '{skey}' must be a string")

    dg = rec.get("disambiguate_genus")
    if dg is not None:
        if not isinstance(dg, list):
            problems.append("'disambiguate_genus' must be a list")
        else:
            for i, ent in enumerate(dg):
                where = f"disambiguate_genus[{i}]"
                if not isinstance(ent, dict):
                    problems.append(f"{where}: must be a mapping")
                    continue
                if not ent.get("genus") or not _is_str(ent["genus"]):
                    problems.append(f"{where}: missing 'genus' (string)")
                if not _is_list_of_str(ent.get("species")):
                    problems.append(f"{where}: 'species' must be a list of strings")

    if "requires" in rec and not _is_list_of_str(rec["requires"]):
        problems.append("'requires' must be a list of strings")
    for key in ("dose_via", "default_answer", "default_answer_hu",
                "marker_without_pathogen", "marker_without_pathogen_hu",
                "conflict_answer"):
        if key in rec and not _is_str(rec[key]):
            problems.append(f"'{key}' must be a string")


def _check_pathway(rec: dict, problems: list[str]) -> None:
    outputs = rec.get("outputs")
    output_names: set[str] = set()
    if not isinstance(outputs, dict) or not outputs:
        problems.append("'outputs' must be a non-empty mapping of NAME -> {...}")
    else:
        output_names = set(outputs.keys())
        for oname, ospec in outputs.items():
            if not isinstance(ospec, dict):
                problems.append(f"output '{oname}': must be a mapping")
    if "slots" in rec:
        _check_slots(rec["slots"], problems)
    if "select" in rec:
        _check_select(rec["select"], problems, target_key="output",
                      valid_targets=output_names)
    if "doses" in rec and not isinstance(rec["doses"], bool):
        problems.append("'doses' must be a boolean")


def _check_prose(rec: dict, problems: list[str]) -> None:
    sections = rec.get("sections")
    if not isinstance(sections, dict) or not sections:
        problems.append("'sections' must be a non-empty mapping of name -> {...}")
        return
    for sname, sspec in sections.items():
        where = f"section '{sname}'"
        if not isinstance(sspec, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        if not any(k in sspec for k in ("text", "text_hu", "text_en")):
            problems.append(f"{where}: needs 'text' or 'text_hu'/'text_en'")
        if "aliases" in sspec and not _is_list_of_str(sspec["aliases"]):
            problems.append(f"{where}: 'aliases' must be a list of strings")


_TABLE_TYPES = ("dosing_table", "fixed_dose", "prophylaxis", "renal_warning")


def _check_table_lookup(rec: dict, problems: list[str]) -> None:
    if "slots" in rec:
        _check_slots(rec["slots"], problems)

    # indication_rules: ordered keyword->tier classifier.
    irules = rec.get("indication_rules")
    if not isinstance(irules, list) or not irules:
        problems.append("'indication_rules' must be a non-empty list")
    else:
        for i, r in enumerate(irules):
            where = f"indication_rules[{i}]"
            if not isinstance(r, dict):
                problems.append(f"{where}: must be a mapping")
                continue
            if not r.get("tier") or not _is_str(r["tier"]):
                problems.append(f"{where}: missing 'tier' (string)")
            if not _is_list_of_str(r.get("contains")):
                problems.append(f"{where}: 'contains' must be a list of strings")

    # renal_rules: ordered guard->category ladder + terminal default.
    rrules = rec.get("renal_rules")
    if not isinstance(rrules, list) or not rrules:
        problems.append("'renal_rules' must be a non-empty list (the renal ladder)")
    else:
        saw_default = False
        for i, r in enumerate(rrules):
            where = f"renal_rules[{i}]"
            if not isinstance(r, dict):
                problems.append(f"{where}: must be a mapping")
                continue
            if "default" in r:
                saw_default = True
                if not _is_str(r["default"]):
                    problems.append(f"{where}: 'default' must be a string")
                continue
            if "if" not in r or not _is_str(r["if"]):
                problems.append(f"{where}: needs an 'if' guard (string) or a 'default'")
            if not r.get("category") or not _is_str(r.get("category")):
                problems.append(f"{where}: guard needs a 'category' (string) target")
        if not saw_default:
            problems.append("'renal_rules' has no terminal {default: ...} rung")

    # tables: keyed verbatim outputs.
    tables = rec.get("tables")
    table_names: set = set()
    if not isinstance(tables, dict) or not tables:
        problems.append("'tables' must be a non-empty mapping of KEY -> {type, ...}")
    else:
        table_names = set(tables.keys())
        for tname, tspec in tables.items():
            where = f"table '{tname}'"
            if not isinstance(tspec, dict):
                problems.append(f"{where}: must be a mapping")
                continue
            ttype = tspec.get("type")
            if ttype not in _TABLE_TYPES:
                problems.append(f"{where}: type {ttype!r} not in {list(_TABLE_TYPES)}")
            if ttype == "dosing_table":
                rows = tspec.get("rows")
                if not isinstance(rows, list) or not rows:
                    problems.append(f"{where}: dosing_table needs a non-empty 'rows' list")
                else:
                    for j, row in enumerate(rows):
                        rw = f"{where} rows[{j}]"
                        if not isinstance(row, dict):
                            problems.append(f"{rw}: must be a mapping")
                            continue
                        if not isinstance(row.get("weight_kg"), (int, float)):
                            problems.append(f"{rw}: 'weight_kg' must be a number")
                        if not _is_str(row.get("practical_dose")):
                            problems.append(f"{rw}: 'practical_dose' must be a string")
            else:
                # fixed_dose / prophylaxis / renal_warning carry verbatim text.
                if not any(_is_str(tspec.get(k)) for k in ("text", "text_en", "text_hu")):
                    problems.append(f"{where}: needs verbatim 'text'/'text_en'/'text_hu'")
            for skey in ("target", "renal_adjustment", "text", "text_en", "text_hu"):
                if skey in tspec and not _is_str(tspec[skey]):
                    problems.append(f"{where}: '{skey}' must be a string")

    # prophylaxis_tables: renal_category -> table name (all must resolve).
    ptab = rec.get("prophylaxis_tables")
    if ptab is not None:
        if not isinstance(ptab, dict):
            problems.append("'prophylaxis_tables' must be a mapping of category -> table name")
        else:
            for cat, target in ptab.items():
                if not _is_str(target):
                    problems.append(f"prophylaxis_tables[{cat}]: value must be a string")
                elif table_names and target not in table_names:
                    problems.append(f"prophylaxis_tables[{cat}]: {target!r} is not a "
                                    f"defined table {sorted(table_names)}")

    info = rec.get("info_blocks")
    if info is not None:
        if not isinstance(info, dict):
            problems.append("'info_blocks' must be a mapping of name -> {text}")
        else:
            for iname, ispec in info.items():
                if not isinstance(ispec, dict):
                    problems.append(f"info_blocks['{iname}']: must be a mapping")
                    continue
                if not _is_str(ispec.get("text")):
                    problems.append(f"info_blocks['{iname}']: needs 'text' (string)")
                if "aliases" in ispec and not _is_list_of_str(ispec["aliases"]):
                    problems.append(f"info_blocks['{iname}']: 'aliases' must be a list of strings")

    if "requires" in rec and not _is_list_of_str(rec["requires"]):
        problems.append("'requires' must be a list of strings")
    if "never" in rec and not _is_list_of_str(rec["never"]):
        problems.append("'never' must be a list of strings")
    if "weight_slot" in rec and not _is_str(rec["weight_slot"]):
        problems.append("'weight_slot' must be a string")
    for nkey in ("supported_weight_min", "supported_weight_max"):
        if nkey in rec and not isinstance(rec[nkey], (int, float)):
            problems.append(f"'{nkey}' must be a number")
    for skey in ("output_template_en", "output_template_hu",
                 "default_answer", "default_answer_hu",
                 "missing_inputs", "missing_inputs_hu"):
        if skey in rec and not _is_str(rec[skey]):
            problems.append(f"'{skey}' must be a string")


def _check_calculator(rec: dict, problems: list[str]) -> None:
    """A `calculator` protocol: declared slots + one or more `methods`, each an
    ordered list of compute steps (arithmetic `expr` or a `lookup` table) and a
    verbatim render template. This is the only kind that COMPUTES; every formula
    is declared here (verbatim from source) and evaluated by a restricted AST in
    the tool, never `eval`."""
    slots = rec.get("slots")
    declared_slots: set = set()
    if slots is not None:
        _check_slots(slots, problems)
        if isinstance(slots, dict):
            declared_slots = set(slots.keys())

    methods = rec.get("methods")
    if not isinstance(methods, list) or not methods:
        problems.append("'methods' must be a non-empty list")
        methods = []

    seen_ids: set = set()
    for i, m in enumerate(methods):
        where = f"methods[{i}]"
        if not isinstance(m, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        mid = m.get("id")
        if not _is_str(mid):
            problems.append(f"{where}: needs an 'id' (string)")
        elif mid in seen_ids:
            problems.append(f"{where}: duplicate method id {mid!r}")
        else:
            seen_ids.add(mid)

        req = m.get("requires", [])
        if req and not _is_list_of_str(req):
            problems.append(f"{where}: 'requires' must be a list of strings")
        elif declared_slots:
            for slot in req:
                if slot not in declared_slots:
                    problems.append(f"{where}: requires undeclared slot {slot!r}")

        compute = m.get("compute")
        produced: set = set()
        if not isinstance(compute, list) or not compute:
            problems.append(f"{where}: 'compute' must be a non-empty list of steps")
        else:
            for j, step in enumerate(compute):
                sw = f"{where} compute[{j}]"
                if not isinstance(step, dict):
                    problems.append(f"{sw}: must be a mapping")
                    continue
                name = step.get("name")
                if not _is_str(name):
                    problems.append(f"{sw}: needs a 'name' (string)")
                has_expr = "expr" in step
                has_lookup = "lookup" in step
                if has_expr == has_lookup:
                    problems.append(f"{sw}: needs exactly one of 'expr' or 'lookup'")
                if has_expr and not _is_str(step["expr"]):
                    problems.append(f"{sw}: 'expr' must be a string")
                if has_lookup:
                    if not _is_str(step["lookup"]):
                        problems.append(f"{sw}: 'lookup' must be a slot name (string)")
                    elif declared_slots and step["lookup"] not in declared_slots:
                        problems.append(f"{sw}: lookup references undeclared slot "
                                        f"{step['lookup']!r}")
                    table = step.get("table")
                    if not isinstance(table, dict) or not table:
                        problems.append(f"{sw}: lookup needs a non-empty 'table' mapping")
                    else:
                        for k, v in table.items():
                            if not isinstance(v, (int, float)):
                                problems.append(f"{sw}: table[{k!r}] must be a number")
                if _is_str(name):
                    produced.add(name)

        outputs = m.get("outputs")
        if outputs is not None and not _is_list_of_str(outputs):
            problems.append(f"{where}: 'outputs' must be a list of strings")

        if not any(_is_str(m.get(k)) for k in ("template_en", "template_hu")):
            problems.append(f"{where}: needs a 'template_en' or 'template_hu'")

    for skey in ("default_answer", "default_answer_hu", "missing_inputs",
                 "missing_inputs_hu", "unsupported_value", "unsupported_value_hu"):
        if skey in rec and not _is_str(rec[skey]):
            problems.append(f"'{skey}' must be a string")
    if "never" in rec and not _is_list_of_str(rec["never"]):
        problems.append("'never' must be a list of strings")


_KIND_CHECKERS = {
    "drug_dose": _check_drug_dose,
    "pcr_panel": _check_pcr_panel,
    "pathway": _check_pathway,
    "prose": _check_prose,
    "table_lookup": _check_table_lookup,
    "calculator": _check_calculator,
}


# --------------------------------------------------------------------------- #
# Public validation + loading                                                 #
# --------------------------------------------------------------------------- #
def validate_record(record) -> list[str]:
    """Return a list of human-readable problems; an empty list means valid."""
    problems: list[str] = []
    if not isinstance(record, dict):
        return ["top-level document must be a mapping"]

    _check_common(record, problems)

    kind = record.get("kind")
    if kind in KINDS:
        # Required-by-kind fields.
        for req in KIND_REQUIRED.get(kind, ()):
            if req not in record:
                problems.append(f"kind '{kind}' requires '{req}'")
        # Kind-specific structural checks.
        _KIND_CHECKERS[kind](record, problems)
        # Smell check: fields that belong to a different kind.
        allowed = COMMON_FIELDS | KIND_FIELDS.get(kind, set())
        for key in record:
            if key not in allowed:
                problems.append(f"unexpected field '{key}' for kind '{kind}'")

    return problems


def load_protocol(path) -> dict:
    """Load + validate one protocol file. Raises ProtocolError on any problem."""
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ProtocolError(f"{path.name}: invalid YAML: {exc}") from exc
    if record is None:
        raise ProtocolError(f"{path.name}: file is empty")
    problems = validate_record(record)
    if problems:
        joined = "\n  - ".join(problems)
        raise ProtocolError(f"{path.name}: {len(problems)} problem(s):\n  - {joined}")
    return record


def load_protocol_dir(dirpath) -> list[tuple[Path, dict]]:
    """Load every *.yaml/*.yml in a directory (sorted). Raises on the first bad
    file (loud) — `validate_protocols.py` aggregates across files instead."""
    dirpath = Path(dirpath)
    out: list[tuple[Path, dict]] = []
    for p in sorted(dirpath.glob("*.y*ml")):
        out.append((p, load_protocol(p)))
    return out
