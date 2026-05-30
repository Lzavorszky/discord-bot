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


def _pick_col(row, keyword):
    for k, v in row.items():
        if keyword.lower() in k.lower():
            return v
    return None


def _run_table_lookup(parsed, slots):
    outputs = _parse_selected_outputs_panel(parsed.get("selected_outputs", ""))
    indication_raw = slots.get("indication") or slots.get("indication_text") or ""
    body_weight_kg = slots.get("body_weight_kg")
    has_renal = (slots.get("gfr") is not None or slots.get("crrt") is not None or slots.get("ihd") is not None)
    missing = []
    if not indication_raw:
        missing.append("indication")
    if body_weight_kg is None:
        missing.append("body_weight_kg")
    if not has_renal:
        missing.append("renal_function (GFR/CRRT/IHD)")
    if missing:
        return SelectionResult(missing_slots=missing, default_used=True, mode_used="table_lookup")
    indication_tier = _classify_indication_tier(indication_raw)
    if not indication_tier:
        return SelectionResult(missing_slots=["indication (could not classify)"], default_used=True, mode_used="table_lookup")
    renal_category = _classify_renal_category(slots)
    if not renal_category:
        return SelectionResult(missing_slots=["renal_function"], default_used=True, mode_used="table_lookup")
    if renal_category == "IHD":
        od = outputs.get("IHD", {})
        return SelectionResult(output_key="IHD", output_data=od, mode_used="table_lookup",
            render_vars={**slots, **od, "indication_tier": indication_tier, "renal_category": "IHD", "body_weight_kg": str(body_weight_kg)})
    if renal_category == "GFR_LT_15_WITHOUT_CRRT":
        od = outputs.get("GFR_LT_15_WITHOUT_CRRT", {})
        return SelectionResult(output_key="GFR_LT_15_WITHOUT_CRRT", output_data=od, mode_used="table_lookup",
            render_vars={**slots, **od, "indication_tier": indication_tier, "renal_category": "GFR <15 (no CRRT)", "body_weight_kg": str(body_weight_kg)})
    table_key = f"{indication_tier}_{renal_category}"
    output_data = outputs.get(table_key, {})
    if not output_data:
        return SelectionResult(default_used=True, mode_used="table_lookup",
            render_vars={**slots, "indication_tier": indication_tier, "renal_category": renal_category})
    weight_row = {}
    bounds = (None, None)
    out_of_bounds = False
    reference_row_value = None
    try:
        wf = float(str(body_weight_kg).replace("kg","").strip())
        table_rows = output_data.get("_table_rows", [])
        row = _find_weight_row(table_rows, wf)
        if row:
            weight_row = {k.lower().replace(" ","_"): v for k, v in row.items()}
            reference_row_value = _numeric_table_row_value(row, "weight")
        bounds = _table_numeric_bounds(table_rows, "weight")
        min_bound, max_bound = bounds
        if (min_bound is not None and max_bound is not None
                and (wf < min_bound or wf > max_bound)
                and not _explicit_extrapolation_allowed(parsed, output_data)):
            out_of_bounds = True
    except (ValueError, TypeError):
        pass
    practical_dose = (_pick_col(weight_row, "practical") or weight_row.get("practical_dose") or "see table")
    total_daily = (_pick_col(weight_row, "total") or weight_row.get("total_daily_tmp/smx") or "see table")
    renal_display = {"GFR_GT_30_OR_CRRT": "GFR >30 or CRRT", "GFR_15_TO_30": "GFR 15-30"}.get(renal_category, renal_category)
    rvars = {**slots, **output_data, "indication_tier": indication_tier, "renal_category": renal_display,
             "body_weight_kg": str(body_weight_kg), "practical_dose": practical_dose, "total_daily_tmp_smx": total_daily}
    if out_of_bounds:
        min_bound, max_bound = bounds
        entered_weight = float(str(body_weight_kg).replace("kg","").strip())
        rvars.update({
            "table_bound_slot": "body weight",
            "table_bound_unit": "kg",
            "entered_bound_value": entered_weight,
            "table_min_value": min_bound,
            "table_max_value": max_bound,
            "nearest_row_value": reference_row_value,
            "nearest_row_data": weight_row,
            "out_of_table_range": True,
            "review_required": True,
        })
    return SelectionResult(output_key=table_key, output_data=output_data, mode_used="table_lookup", render_vars=rvars)


_ORGANISM_TIER_MAP = {
    "acinetobacter calcoaceticus-baumannii complex": (4, ["meropenem", "colistin"]),
    "enterobacter cloacae": (2, ["cefepime"]), "escherichia coli": (1, ["ceftriaxone"]),
    "haemophilus influenzae": (1, ["ceftriaxone"]), "klebsiella aerogenes": (2, ["cefepime"]),
    "klebsiella oxytoca": (1, ["ceftriaxone"]), "klebsiella pneumoniae group": (1, ["ceftriaxone"]),
    "moraxella catarrhalis": (1, ["ceftriaxone"]), "proteus spp.": (2, ["cefepime"]),
    "pseudomonas aeruginosa": (2, ["cefepime"]), "serratia marcescens": (2, ["cefepime"]),
    "staphylococcus aureus": (1, ["cefazolin"]), "streptococcus agalactiae": (1, ["ceftriaxone"]),
    "streptococcus pneumoniae": (1, ["ceftriaxone"]), "streptococcus pyogenes": (2, ["penicillin", "clindamycin"]),
    "legionella pneumophila": (0, ["clarithromycin"]), "mycoplasma pneumoniae": (0, ["clarithromycin"]),
    "chlamydia pneumoniae": (0, ["clarithromycin"]), "influenza a/b": (-1, ["oseltamivir"]),
}
_ENTEROBACTERALES = {"escherichia coli","klebsiella pneumoniae group","klebsiella aerogenes",
    "klebsiella oxytoca","enterobacter cloacae","proteus spp.","serratia marcescens"}
_ORGANISM_ALIASES = {
    "acinetobacter": "acinetobacter calcoaceticus-baumannii complex",
    "baumannii": "acinetobacter calcoaceticus-baumannii complex",
    "acb": "acinetobacter calcoaceticus-baumannii complex",
    "acinetobacter calcoaceticus-baumannii complex": "acinetobacter calcoaceticus-baumannii complex",
    "enterobacter cloacae": "enterobacter cloacae", "enterobacter": "enterobacter cloacae",
    "escherichia coli": "escherichia coli", "e. coli": "escherichia coli",
    "ecoli": "escherichia coli", "e.coli": "escherichia coli",
    "haemophilus influenzae": "haemophilus influenzae", "haemophilus": "haemophilus influenzae",
    "h. influenzae": "haemophilus influenzae",
    "klebsiella aerogenes": "klebsiella aerogenes", "klebsiella oxytoca": "klebsiella oxytoca",
    "klebsiella pneumoniae": "klebsiella pneumoniae group",
    "klebsiella pneumoniae group": "klebsiella pneumoniae group",
    "klebsiella pn": "klebsiella pneumoniae group", "kpn": "klebsiella pneumoniae group",
    "moraxella catarrhalis": "moraxella catarrhalis", "moraxella": "moraxella catarrhalis",
    "proteus spp.": "proteus spp.", "proteus": "proteus spp.",
    "pseudomonas aeruginosa": "pseudomonas aeruginosa", "pseudomonas": "pseudomonas aeruginosa",
    "pa": "pseudomonas aeruginosa", "psa": "pseudomonas aeruginosa",
    "serratia marcescens": "serratia marcescens", "serratia": "serratia marcescens",
    "staphylococcus aureus": "staphylococcus aureus", "staph aureus": "staphylococcus aureus",
    "s. aureus": "staphylococcus aureus", "mssa": "staphylococcus aureus", "mrsa": "staphylococcus aureus",
    "streptococcus agalactiae": "streptococcus agalactiae", "strep agalactiae": "streptococcus agalactiae",
    "gbs": "streptococcus agalactiae", "group b strep": "streptococcus agalactiae",
    "streptococcus pneumoniae": "streptococcus pneumoniae", "strep pneumoniae": "streptococcus pneumoniae",
    "strep pneumo": "streptococcus pneumoniae", "strep pn": "streptococcus pneumoniae",
    "s. pneumoniae": "streptococcus pneumoniae", "s.pneumoniae": "streptococcus pneumoniae",
    "pneumococcus": "streptococcus pneumoniae", "pneumococcal": "streptococcus pneumoniae",
    "streptococcus pyogenes": "streptococcus pyogenes", "strep pyogenes": "streptococcus pyogenes",
    "gas": "streptococcus pyogenes", "group a strep": "streptococcus pyogenes",
    "legionella pneumophila": "legionella pneumophila", "legionella": "legionella pneumophila",
    "mycoplasma pneumoniae": "mycoplasma pneumoniae", "mycoplasma": "mycoplasma pneumoniae",
    "chlamydia pneumoniae": "chlamydia pneumoniae", "chlamydia": "chlamydia pneumoniae",
    "influenza a": "influenza a/b", "influenza b": "influenza a/b", "influenza a/b": "influenza a/b",
}
_RESISTANCE_GENE_ALIASES = {
    "ctx-m": "ctx_m", "ctxm": "ctx_m", "esbl": "ctx_m",
    "kpc": "carbapenemase", "ndm": "carbapenemase", "vim": "carbapenemase",
    "imp": "carbapenemase", "oxa-48": "carbapenemase", "oxa48": "carbapenemase",
    "carbapenemase": "carbapenemase",
    "meca/c": "meca_c", "meca": "meca_c", "mecc": "meca_c", "mrej": "meca_c",
    "meca_c": "meca_c",
    "ctx_m": "ctx_m",
}
_TIER_OUTPUT_KEY = {1: "TIER_1_CEFTRIAXONE", 2: "TIER_2_CEFEPIME", 3: "TIER_3_ERTAPENEM", 4: "TIER_4_MEROPENEM_COLISTIN"}


def _normalize_organism(name):
    return _ORGANISM_ALIASES.get(name.lower().strip())


def _normalize_resistance_gene(name):
    return _RESISTANCE_GENE_ALIASES.get(name.lower().strip())


def _run_organism_mapping(parsed, slots):
    outputs = _parse_selected_outputs_panel(parsed.get("selected_outputs", ""))
    pathogen_list = slots.get("pathogen_list", [])
    resistance_list = slots.get("resistance_gene_list", [])
    if not pathogen_list:
        if resistance_list:
            return SelectionResult(missing_slots=["detected_pathogen"], mode_used="organism_mapping",
                ask_missing="Resistance markers can only be interpreted with a detected pathogen. Which pathogen was positive?")
        return SelectionResult(default_used=True, mode_used="organism_mapping")
    canonical_organisms = []
    for name in pathogen_list:
        c = _normalize_organism(name)
        if c and c not in canonical_organisms:
            canonical_organisms.append(c)
    canonical_genes = []
    for g in resistance_list:
        c = _normalize_resistance_gene(g)
        if c and c not in canonical_genes:
            canonical_genes.append(c)
    staph_present = "staphylococcus aureus" in canonical_organisms
    mrsa = staph_present and "meca_c" in canonical_genes
    non_staph = [o for o in canonical_organisms if o != "staphylococcus aureus"]
    enterobacterales_present = any(o in _ENTEROBACTERALES for o in canonical_organisms)
    ctx_m = "ctx_m" in canonical_genes
    carbapenemase = "carbapenemase" in canonical_genes
    items = []
    for o in non_staph:
        ti = _ORGANISM_TIER_MAP.get(o)
        if ti:
            t, drugs = ti
            if ctx_m and o in _ENTEROBACTERALES:
                items.append((o, 3, ["ertapenem"]))
            else:
                items.append((o, t, drugs))
    if carbapenemase:
        items.append(("__carbapenemase__", 4, ["meropenem", "colistin"]))
    bacterial_items = [(o, t, d) for o, t, d in items if t >= 1]
    atypical_items  = [(o, t, d) for o, t, d in items if t == 0]
    viral_items     = [(o, t, d) for o, t, d in items if t < 0]
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
    if staph_present and mrsa and max_tier < 1:
        max_tier = 1
    strep_pyogenes = "streptococcus pyogenes" in canonical_organisms
    if strep_pyogenes and max_tier <= 2 and len(bacterial_items) == 1:
        return _mk("STREP_PYOGENES", "Strep pyogenes - penicillin + clindamycin.", "Strep pyogenes - penicillin + clindamycin.")
    has_pseudo = "pseudomonas aeruginosa" in canonical_organisms
    if ctx_m and enterobacterales_present and has_pseudo:
        return _mk("CONFLICTING_REQUIREMENTS", "Konfliktus - ID konzultáció.", "Conflicting requirements - ID consultation.")
    output_key = _TIER_OUTPUT_KEY.get(max_tier, "TIER_1_CEFTRIAXONE")
    od = outputs.get(output_key, {})
    answer_en = od.get("answer_en", f"Tier {max_tier} - {chr(43).join(max_drugs)}.")
    answer_hu = od.get("answer_hu", answer_en)
    if atypical_items:
        note = " + clarithromycin (atypical coverage)"
        answer_en += note; answer_hu += note
    spectrum_logic = ""
    if len(bacterial_items) > 1 or ctx_m or carbapenemase:
        parts = [f"{o}: Tier {t}" for o, t, _ in bacterial_items]
        spectrum_logic = "; ".join(parts) + f" -> Tier {max_tier}"
    rvars = {**slots, **od, "detected_entities": detected_str,
             "selected_output_answer_en": answer_en, "selected_output_answer_hu": answer_hu,
             "spectrum_logic_if_polymicrobial": spectrum_logic}
    if staph_present and mrsa:
        rvars["selected_output_answer_en"] += " + vancomycin (MRSA)"
        rvars["selected_output_answer_hu"] += " + vancomycin (MRSA)"
    return SelectionResult(output_key=output_key, output_data=od, mode_used="organism_mapping", render_vars=rvars)


_GFR_RE    = re.compile(r"\b(?:GFR|eGFR|CrCl)\s*[=:~]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg\b", re.IGNORECASE)
_VANCOMYCIN_LEVEL_RE = re.compile(
    r"\b(?:vancomycin|vanco|vankomicin)?\s*(?:level|szint|concentration|conc|tdm)\s*(?:is|at|=|:|~)?\s*(\d+(?:\.\d+)?)"
    r"|(\d+(?:\.\d+)?)\s*(?:ug/l|ug/ml|mcg/l|mcg/ml)\b",
    re.IGNORECASE,
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
    gfr_matches = list(_GFR_RE.finditer(text))
    if gfr_matches:
        slots["gfr"] = float(gfr_matches[-1].group(1))
    weight_matches = list(_WEIGHT_RE.finditer(text))
    if weight_matches:
        slots["body_weight_kg"] = float(weight_matches[-1].group(1))
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
        if meta.get("protocol_id") == "tmpsmx":
            indication = _extract_indication_text(text, slots)
            if indication:
                slots["indication"] = indication
                slots["indication_text"] = indication
        if meta.get("protocol_id") == "biofire_pneumonia":
            organisms, genes = _extract_biofire_entities(text)
            if organisms:
                existing_orgs = slots.get("pathogen_list", [])
                slots["pathogen_list"] = list({*existing_orgs, *organisms})
            if genes:
                existing_genes = slots.get("resistance_gene_list", [])
                slots["resistance_gene_list"] = list({*existing_genes, *genes})
        if meta.get("protocol_id") == "vancomycin":
            vm = _VANCOMYCIN_LEVEL_RE.search(text)
            if vm:
                slots["vancomycin_level"] = float(vm.group(1) or vm.group(2))
    return slots


def _extract_indication_text(text, slots):
    lower = text.lower()
    for tier, pattern in _INDICATION_RULES:
        if re.search(pattern, lower):
            m = re.search(pattern, lower)
            return m.group(0) if m else lower
    return None


def _extract_biofire_entities(text):
    organisms = []
    genes = []
    sorted_aliases = sorted(_ORGANISM_ALIASES.keys(), key=len, reverse=True)
    for alias in sorted_aliases:
        if re.search(r"\b" + re.escape(alias) + r"\b", text, re.IGNORECASE):
            c = _ORGANISM_ALIASES[alias]
            if c not in organisms:
                organisms.append(c)
    for alias, canonical in _RESISTANCE_GENE_ALIASES.items():
        if re.search(r"\b" + re.escape(alias) + r"\b", text, re.IGNORECASE):
            if canonical not in genes:
                genes.append(canonical)
    return organisms, genes


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

    if lang == "hu":
        lines = [
            f"A megadott {slot} ({entered}{unit_suffix}) az explicit protokolltablazat tartomanyan kivul van "
            f"({min_value}-{max_value}{unit_suffix}).",
            "Automatikus dozisemeles nem tamogatott, es ennel az erteknel veszelyes lehet.",
            f"Legkozelebbi explicit protokollsor, csak tajekoztatasra: {nearest}{unit_suffix} sor.",
            f"Ez nem {entered}{unit_suffix}-ra adott dozisjavaslat.",
            "Ehhez a beteghez individualizalt ID/gyogyszereszeti felulvizsgalat szukseges.",
            "",
            "Referencia protokolladat:",
        ]
    else:
        lines = [
            f"Patient {slot} {entered}{unit_suffix} is outside the explicit protocol table range "
            f"({min_value}-{max_value}{unit_suffix}).",
            "Automatic dose escalation is not supported and may be unsafe at this value.",
            f"Closest explicit protocol row for reference only: {nearest}{unit_suffix} row.",
            f"This is not a {entered}{unit_suffix} dosing recommendation.",
            "Use individualized ID/pharmacy review for this patient.",
            "",
            "Reference protocol data:",
        ]

    nearest_row = rv.get("nearest_row_data") or {}
    for key, value in nearest_row.items():
        if value:
            lines.append(f"- {key.replace('_', ' ')}: {value}")

    context_rows = []
    for key in ("target", "renal_adjustment", "renal_category", "indication_tier"):
        value = rv.get(key)
        if value:
            context_rows.append((key, value))
    if context_rows:
        lines.append("")
        lines.append("Protocol context:" if lang != "hu" else "Protokoll kontextus:")
        for key, value in context_rows:
            lines.append(f"- {key.replace('_', ' ')}: {value}")

    return "\n".join(lines).strip()


def render_selected_output(parsed, result, lang="en"):
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


def run_selection(parsed, slots, lang="en"):
    meta = parsed.get("metadata", {})
    mode = meta.get("selection_mode", "none").lower()
    if mode == "priority_rules":
        outputs = _parse_selected_outputs_panel(parsed.get("selected_outputs", ""))
        return _run_priority_rules(parsed.get("selection_rules", ""), outputs, slots)
    if mode == "table_lookup":
        return _run_table_lookup(parsed, slots)
    if mode == "organism_mapping_with_spectrum_escalation":
        return _run_organism_mapping(parsed, slots)
    return SelectionResult(no_match=True, mode_used=mode)
