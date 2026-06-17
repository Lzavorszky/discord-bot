"""Unit tests for the protocol schema, loader/validator, and linter (Phase 2).

Fully offline — no model calls. Covers:
  * a valid record of each kind passes;
  * every bad-file class is rejected with a clear, specific message;
  * load_protocol() raises ProtocolError naming the file + all problems;
  * the cross-file alias-collision linter (the F1 pre-emption) fires;
  * validate_protocols.main() is green over good fixtures, red over bad ones.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ID_BOT2 = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ID_BOT2, "protocols"))
sys.path.insert(0, ID_BOT2)

import loader  # noqa: E402
import schema  # noqa: E402
import validate_protocols as VP  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
GOOD = os.path.join(FIX, "good")
BAD = os.path.join(FIX, "bad")


# --------------------------------------------------------------------------- #
# Schema enums / JSON Schema sanity                                           #
# --------------------------------------------------------------------------- #
def test_kinds_and_json_schema_consistent():
    assert set(schema.KINDS) == {"drug_dose", "pcr_panel", "pathway", "prose", "table_lookup"}
    js = schema.PROTOCOL_JSON_SCHEMA
    assert js["required"] == ["id", "kind"]
    # every kind has a required-fields entry
    assert set(schema.KIND_REQUIRED) == set(schema.KINDS)


# --------------------------------------------------------------------------- #
# Valid records — one per kind                                                #
# --------------------------------------------------------------------------- #
def test_valid_drug_dose():
    rec = {
        "id": "meropenem", "kind": "drug_dose",
        "tiers": {"NORMAL": {"dose": "3 g/day"}},
        "select": [{"if": "gfr >= 20", "tier": "NORMAL"},
                   {"default": "DEFAULT_ANSWER"}],
    }
    assert loader.validate_record(rec) == []


def test_valid_drug_dose_with_prep_and_notes():
    # prep/notes are optional free-text strings allowed on every drug_dose protocol.
    rec = {
        "id": "meropenem", "kind": "drug_dose",
        "tiers": {"NORMAL": {"dose": "4 g/day"}},
        "select": [{"if": "gfr >= 20", "tier": "NORMAL"},
                   {"default": "DEFAULT_ANSWER"}],
        "prep": "dissolve 1 g in 20 mL NaCl 0.9%, withdraw 10 mL, dilute to 50 mL",
        "notes": "Think TDM!",
    }
    assert loader.validate_record(rec) == []


def test_reject_non_string_prep():
    p = loader.validate_record({
        "id": "x", "kind": "drug_dose",
        "tiers": {"NORMAL": {"dose": "1 g"}},
        "select": [{"default": "NORMAL"}],
        "prep": ["not", "a", "string"]})
    assert any("'prep' must be a string" in m for m in p)


def test_valid_pcr_panel():
    rec = {
        "id": "biofire_ji", "kind": "pcr_panel",
        "requires": ["at_least_one_detected_pathogen"],
        "spectrum_tiers": {"1": {"therapy": "ceftriaxone",
                                 "answer": "Tier 1 - ceftriaxone."}},
        "disambiguate_genus": [{"genus": "Klebsiella",
                                "species": ["Klebsiella oxytoca",
                                            "Klebsiella pneumoniae group"]}],
        "default_answer": "Send at least one pathogen.",
        "marker_without_pathogen": "Which pathogen was positive?",
        "organisms": [{"name": "Staphylococcus aureus", "tier": 1,
                       "entity_type": "bacteria", "therapy": "cefazolin",
                       "answer": "MSSA likely - cefazolin.",
                       "marker_answer": "MRSA likely - vancomycin.",
                       "aliases": ["S. aureus"]}],
        "markers": [{"name": "mecA/C & MREJ", "rule": "mrsa",
                     "therapy": "vancomycin", "aliases": ["mecA", "MREJ"]}],
    }
    assert loader.validate_record(rec) == []


def test_reject_unknown_marker_rule():
    p = loader.validate_record({
        "id": "x", "kind": "pcr_panel",
        "organisms": [{"name": "E. coli"}],
        "markers": [{"name": "weird", "rule": "made_up_rule"}]})
    assert any("rule 'made_up_rule' not in" in m for m in p)


def test_reject_markers_list_of_strings():
    # the old (pre-2.5) shape is no longer valid: markers must be mappings now.
    p = loader.validate_record({
        "id": "x", "kind": "pcr_panel",
        "organisms": [{"name": "E. coli"}],
        "markers": ["mecA"]})
    assert any("markers[0]: must be a mapping" in m for m in p)


def test_reject_disambiguate_genus_missing_species():
    p = loader.validate_record({
        "id": "x", "kind": "pcr_panel",
        "organisms": [{"name": "Klebsiella oxytoca"}],
        "disambiguate_genus": [{"genus": "Klebsiella"}]})
    assert any("'species' must be a list of strings" in m for m in p)


def test_valid_pathway():
    rec = {
        "id": "cap", "kind": "pathway",
        "outputs": {"DEFAULT_ANSWER": {"items": ["ceftriaxone"]}},
        "select": [{"if": "x", "output": "DEFAULT_ANSWER"},
                   {"default": "DEFAULT_ANSWER"}],
    }
    assert loader.validate_record(rec) == []


def test_valid_prose():
    rec = {
        "id": "periop", "kind": "prose",
        "sections": {"antithrombotic": {"text_en": "Continue ASA"}},
    }
    assert loader.validate_record(rec) == []


# --------------------------------------------------------------------------- #
# Bad-file classes — each rejected with a recognisable message                #
# --------------------------------------------------------------------------- #
def test_reject_non_mapping():
    assert loader.validate_record(["not", "a", "map"]) == [
        "top-level document must be a mapping"]


def test_reject_bad_id_charset():
    p = loader.validate_record({"id": "Bad Id!", "kind": "prose",
                                "sections": {"s": {"text": "x"}}})
    assert any("lowercase" in m for m in p)


def test_reject_unknown_kind():
    p = loader.validate_record({"id": "x", "kind": "wizardry"})
    assert any("'kind'" in m for m in p)


def test_reject_bad_status():
    p = loader.validate_record({"id": "x", "kind": "prose",
                                "status": "nope",
                                "sections": {"s": {"text": "x"}}})
    assert any("status" in m for m in p)


def test_reject_unknown_intent():
    p = loader.validate_record({"id": "x", "kind": "prose",
                                "answers_intents": ["dose", "bogus"],
                                "sections": {"s": {"text": "x"}}})
    assert any("bogus" in m for m in p)


def test_reject_missing_kind_required_field():
    # drug_dose without tiers/select
    p = loader.validate_record({"id": "x", "kind": "drug_dose"})
    assert any("requires 'tiers'" in m for m in p)
    assert any("requires 'select'" in m for m in p)


def test_reject_tier_without_dose():
    p = loader.validate_record({
        "id": "x", "kind": "drug_dose",
        "tiers": {"NORMAL": {"when": "always"}},
        "select": [{"default": "NORMAL"}]})
    assert any("missing 'dose'" in m for m in p)


def test_reject_select_ghost_tier():
    p = loader.validate_record({
        "id": "x", "kind": "drug_dose",
        "tiers": {"NORMAL": {"dose": "1 g"}},
        "select": [{"if": "a", "tier": "GHOST"}, {"default": "NORMAL"}]})
    assert any("GHOST" in m and "not a defined tier" in m for m in p)


def test_reject_select_without_default():
    p = loader.validate_record({
        "id": "x", "kind": "drug_dose",
        "tiers": {"NORMAL": {"dose": "1 g"}},
        "select": [{"if": "a", "tier": "NORMAL"}]})
    assert any("no terminal" in m for m in p)


def test_reject_bad_slot_type():
    p = loader.validate_record({
        "id": "x", "kind": "drug_dose",
        "slots": {"gfr": {"type": "floaty"}},
        "tiers": {"NORMAL": {"dose": "1 g"}},
        "select": [{"default": "NORMAL"}]})
    assert any("type 'floaty'" in m for m in p)


def test_reject_enum_slot_without_values():
    p = loader.validate_record({
        "id": "x", "kind": "pathway",
        "slots": {"status": {"type": "enum"}},
        "outputs": {"A": {}},
        "select": [{"default": "A"}]})
    assert any("enum slot needs 'values'" in m for m in p)


def test_reject_wrong_kind_field():
    p = loader.validate_record({
        "id": "x", "kind": "drug_dose",
        "tiers": {"NORMAL": {"dose": "1 g"}},
        "select": [{"default": "NORMAL"}],
        "organisms": [{"name": "bug"}]})
    assert any("unexpected field 'organisms'" in m for m in p)


def test_reject_pcr_without_organisms():
    p = loader.validate_record({"id": "x", "kind": "pcr_panel"})
    assert any("requires 'organisms'" in m for m in p)


def test_reject_prose_section_without_text():
    p = loader.validate_record({
        "id": "x", "kind": "prose",
        "sections": {"s": {"aliases": ["a"]}}})
    assert any("needs 'text'" in m for m in p)


# --------------------------------------------------------------------------- #
# load_protocol() — fails loudly                                              #
# --------------------------------------------------------------------------- #
def test_load_good_fixture_returns_record():
    rec = loader.load_protocol(os.path.join(GOOD, "meropenem_min.yaml"))
    assert rec["id"] == "meropenem_min"
    assert rec["kind"] == "drug_dose"


def test_load_bad_fixture_raises_with_filename_and_problems():
    with pytest.raises(loader.ProtocolError) as ei:
        loader.load_protocol(os.path.join(BAD, "broken_schema.yaml"))
    msg = str(ei.value)
    assert "broken_schema.yaml" in msg
    assert "GHOST_TIER" in msg           # a specific problem is surfaced
    assert "problem(s)" in msg           # count is reported


def test_load_empty_file_raises(tmp_path):
    f = tmp_path / "empty.yaml"
    f.write_text("", encoding="utf-8")
    with pytest.raises(loader.ProtocolError):
        loader.load_protocol(f)


def test_load_protocol_dir_loads_all_good():
    out = loader.load_protocol_dir(GOOD)
    ids = {rec["id"] for _, rec in out}
    assert {"meropenem_min", "periop_min"} <= ids


# --------------------------------------------------------------------------- #
# Linter stub                                                                 #
# --------------------------------------------------------------------------- #
def test_linter_passes_clean_corpus():
    recs = [
        ("a.yaml", {"id": "a", "kind": "drug_dose", "aliases": ["alpha"],
                    "tiers": {"N": {"dose": "1g"}}, "select": [{"default": "N"}]}),
        ("b.yaml", {"id": "b", "kind": "drug_dose", "aliases": ["beta"],
                    "tiers": {"N": {"dose": "1g"}}, "select": [{"default": "N"}]}),
    ]
    errors, warnings = VP.lint_corpus(recs)
    assert errors == []


def test_linter_detects_alias_collision_with_folding():
    # 'Shared Alias' vs 'shared   alias' must collide (accent/space/case folded).
    recs = [
        ("a.yaml", {"id": "a", "kind": "drug_dose", "aliases": ["Shared Alias"],
                    "tiers": {"N": {"dose": "1g"}}, "select": [{"default": "N"}]}),
        ("b.yaml", {"id": "b", "kind": "drug_dose", "aliases": ["shared   alias"],
                    "tiers": {"N": {"dose": "1g"}}, "select": [{"default": "N"}]}),
    ]
    errors, _ = VP.lint_corpus(recs)
    assert any("collision" in e for e in errors)


def test_linter_warns_on_unresolved_drug_reference():
    recs = [
        ("d.yaml", {"id": "cefazolin", "kind": "drug_dose",
                    "tiers": {"N": {"dose": "2g"}}, "select": [{"default": "N"}]}),
        ("p.yaml", {"id": "ji", "kind": "pcr_panel",
                    "organisms": [{"name": "S. aureus", "therapy": "cefazolin"},
                                  {"name": "P. aeruginosa", "therapy": "cefepime"}]}),
    ]
    errors, warnings = VP.lint_corpus(recs)
    assert errors == []
    assert any("cefepime" in w for w in warnings)        # not migrated -> warned
    assert not any("cefazolin" in w for w in warnings)   # migrated -> resolves


# --------------------------------------------------------------------------- #
# validate_protocols.main() exit codes                                        #
# --------------------------------------------------------------------------- #
def test_main_green_over_good_fixtures():
    assert VP.main([GOOD]) == 0


def test_main_red_over_bad_schema():
    assert VP.main([os.path.join(BAD, "broken_schema.yaml")]) == 1


def test_main_red_over_alias_collision():
    assert VP.main([os.path.join(BAD, "collide_a.yaml"),
                    os.path.join(BAD, "collide_b.yaml")]) == 1


def test_main_green_when_dir_empty(tmp_path):
    # An empty protocols dir (nothing migrated yet) must stay green.
    assert VP.main([str(tmp_path)]) == 0


# --------------------------------------------------------------------------- #
# table_lookup kind (tmpsmx) — Phase 2.5 final / "final migration phase".      #
# --------------------------------------------------------------------------- #
def _good_table_lookup():
    return {
        "id": "tmpsmx", "kind": "table_lookup",
        "slots": {
            "indication": {"type": "string"},
            "body_weight_kg": {"type": "number", "min": 1, "max": 300,
                               "on_out_of_range": "ask_confirmation"},
            "gfr": {"type": "number", "min": 0, "max": 250,
                    "on_out_of_range": "ask_confirmation"},
            "crrt": {"type": "bool"}, "ihd": {"type": "bool"},
        },
        "requires": ["indication"],
        "indication_rules": [
            {"tier": "HIGH_DOSE", "contains": ["pcp", "nocardia"]},
            {"tier": "PROPHYLAXIS", "contains": ["prophylaxis"]},
        ],
        "renal_rules": [
            {"if": "crrt", "category": "GFR_GT_30_OR_CRRT"},
            {"if": "gfr > 30", "category": "GFR_GT_30_OR_CRRT"},
            {"default": "UNKNOWN"},
        ],
        "tables": {
            "HIGH_DOSE_GFR_GT_30_OR_CRRT": {
                "type": "dosing_table", "target": "15-20 mg/kg/day",
                "rows": [
                    {"weight_kg": 40, "practical_dose": "4 x 2 amp",
                     "total": "640/3200 mg daily"},
                    {"weight_kg": 70, "practical_dose": "3 x 4 amp",
                     "total": "960/4800 mg daily"},
                ],
            },
            "PROPHYLAXIS_GENERAL": {
                "type": "prophylaxis", "text_en": "1 tablet daily"},
        },
        "prophylaxis_tables": {"UNKNOWN": "PROPHYLAXIS_GENERAL"},
    }


def test_valid_table_lookup():
    assert loader.validate_record(_good_table_lookup()) == []


def test_table_lookup_requires_tables_and_rules():
    rec = {"id": "tmpsmx", "kind": "table_lookup"}
    probs = loader.validate_record(rec)
    assert any("requires 'tables'" in p for p in probs)
    assert any("requires 'indication_rules'" in p for p in probs)
    assert any("requires 'renal_rules'" in p for p in probs)


def test_table_lookup_dosing_table_needs_rows():
    rec = _good_table_lookup()
    rec["tables"]["HIGH_DOSE_GFR_GT_30_OR_CRRT"]["rows"] = []
    probs = loader.validate_record(rec)
    assert any("non-empty 'rows'" in p for p in probs)


def test_table_lookup_row_needs_practical_dose():
    rec = _good_table_lookup()
    rec["tables"]["HIGH_DOSE_GFR_GT_30_OR_CRRT"]["rows"][0] = {"weight_kg": 40}
    probs = loader.validate_record(rec)
    assert any("practical_dose" in p for p in probs)


def test_table_lookup_renal_rules_need_default():
    rec = _good_table_lookup()
    rec["renal_rules"] = [{"if": "crrt", "category": "GFR_GT_30_OR_CRRT"}]
    probs = loader.validate_record(rec)
    assert any("no terminal {default" in p for p in probs)


def test_table_lookup_prophylaxis_table_must_resolve():
    rec = _good_table_lookup()
    rec["prophylaxis_tables"] = {"UNKNOWN": "DOES_NOT_EXIST"}
    probs = loader.validate_record(rec)
    assert any("is not a defined table" in p for p in probs)


def test_table_lookup_fixed_dose_needs_text():
    rec = _good_table_lookup()
    rec["tables"]["STANDARD"] = {"type": "fixed_dose"}
    probs = loader.validate_record(rec)
    assert any("verbatim 'text'" in p for p in probs)


def test_table_lookup_bad_table_type():
    rec = _good_table_lookup()
    rec["tables"]["WEIRD"] = {"type": "not_a_type", "text_en": "x"}
    probs = loader.validate_record(rec)
    assert any("not in ['dosing_table'" in p for p in probs)
