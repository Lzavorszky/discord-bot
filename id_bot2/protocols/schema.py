#!/usr/bin/env python3
"""schema.py — the protocol schema for the ID Bot rebuild (Plan D, Phase 2).

One schema, four `kind`s. Every migrated protocol is a single YAML file that
declares a `kind`, and the shared engine answers it accordingly:

  drug_dose  — a renal/selection dose table (meropenem, cefepime, ...).
  pcr_panel  — a BioFire-style organism panel (joint infection, pneumonia).
  pathway    — an empiric-treatment ladder (CAP, UTI, ...).
  prose      — bounded-text, section-addressable protocols (periop meds).

This module is the single declarative source of truth for what a valid protocol
looks like. It exposes:

  * the enums (`KINDS`, `INTENTS`, `SLOT_TYPES`) the validator and tools share;
  * `PROTOCOL_JSON_SCHEMA` — a real JSON-Schema (draft 2020-12) document, for
    documentation/export and any external tooling that wants it;
  * `FIELD_RULES` — a compact, kind-aware rule table the dependency-light
    validator in `loader.py` walks (no hard `jsonschema` dependency, matching
    the style already used by `run_harness.py`).

Keeping both in one place means the human-readable JSON Schema and the rules the
validator actually enforces can't silently drift apart.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# Enums shared across the loader, the linter, and (later) the tools.          #
# --------------------------------------------------------------------------- #
KINDS = ("drug_dose", "pcr_panel", "pathway", "prose", "table_lookup")

# Intents a protocol may declare it answers / refuses (routing metadata that
# replaces the old .route_claims.json sidecars). Kept permissive but closed so a
# typo in a migrated file is caught at author time.
INTENTS = (
    "dose",
    "coverage_question",
    "targeted_treatment",
    "empiric_treatment",
    "test_interpretation",
    "panel_list",
    "info",
)

# Slot value types (was the old SLOT_SCHEMA). `enum` slots carry a `values` list.
SLOT_TYPES = ("number", "bool", "string", "enum")

# What may be done when a numeric slot is out of its declared range.
OUT_OF_RANGE_ACTIONS = ("ask_confirmation", "clamp", "reject", "ignore")

STATUSES = ("draft", "review", "approved", "retired")


# --------------------------------------------------------------------------- #
# JSON Schema (draft 2020-12) — documentation / export form.                   #
# The validator in loader.py does NOT depend on a jsonschema library; this      #
# document is the portable, human-auditable statement of the same contract.     #
# --------------------------------------------------------------------------- #
_SLOT_SCHEMA = {
    "type": "object",
    "required": ["type"],
    "additionalProperties": True,
    "properties": {
        "type": {"enum": list(SLOT_TYPES)},
        "unit": {"type": "string"},
        "min": {"type": "number"},
        "max": {"type": "number"},
        "values": {"type": "array", "items": {"type": "string"}},
        "enum": {"type": "array", "items": {"type": "string"}},
        "on_out_of_range": {"enum": list(OUT_OF_RANGE_ACTIONS)},
    },
}

_SELECT_ENTRY = {
    "type": "object",
    "description": "One rung of the ordered selection ladder. List order IS "
                   "priority. Each entry is either a guard (`if` + a target) or "
                   "the terminal `default`.",
    "additionalProperties": True,
    "properties": {
        "if": {"type": "string"},
        "tier": {"type": "string"},      # drug_dose target
        "output": {"type": "string"},    # pathway target
        "default": {"type": "string"},
    },
}

_COMMON_PROPERTIES = {
    "id": {"type": "string", "pattern": "^[a-z0-9_]+$"},
    "kind": {"enum": list(KINDS)},
    "source_label": {"type": "string"},
    "canonical_name": {"type": "string"},
    "status": {"enum": list(STATUSES)},
    "version": {"type": ["number", "string"]},
    "aliases": {"type": "array", "items": {"type": "string"}},
    "answers_intents": {"type": "array", "items": {"enum": list(INTENTS)}},
    "refuses_intents": {"type": "array", "items": {"enum": list(INTENTS)}},
    "footer": {"type": "string"},
}

PROTOCOL_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://id-bot/protocol.schema.json",
    "title": "ID Bot protocol",
    "type": "object",
    "required": ["id", "kind"],
    "properties": {
        **_COMMON_PROPERTIES,
        # drug_dose
        "slots": {"type": "object", "additionalProperties": _SLOT_SCHEMA},
        "tiers": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["dose"],
                "properties": {
                    "dose": {"type": "string"},
                    "when": {"type": "string"},
                    "admin": {"type": "string"},
                    "always_show": {"type": "boolean"},
                },
            },
        },
        "select": {"type": "array", "items": _SELECT_ENTRY},
        "never": {"type": "array", "items": {"type": "string"}},
        # free-text preparation/dilution instructions and general clinical notes
        # (shared by every drug_dose protocol).
        "prep": {"type": "string"},
        "notes": {"type": "string"},
        # pcr_panel
        "organisms": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "tier": {"type": "integer"},
                    "therapy": {"type": "string"},
                    "entity_type": {"type": "string"},
                    "enterobacterales": {"type": "boolean"},
                    "answer": {"type": "string"},
                    "answer_hu": {"type": "string"},
                    "marker_answer": {"type": "string"},
                    "marker_answer_hu": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "marker_rules": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "markers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "rule": {"type": "string"},
                    "therapy": {"type": "string"},
                    "answer": {"type": "string"},
                    "note": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "spectrum_tiers": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "therapy": {"type": "string"},
                    "answer": {"type": "string"},
                    "answer_hu": {"type": "string"},
                },
            },
        },
        "disambiguate_genus": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["genus", "species"],
                "properties": {
                    "genus": {"type": "string"},
                    "species": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "requires": {"type": "array", "items": {"type": "string"}},
        "dose_via": {"type": "string"},
        "default_answer": {"type": "string"},
        "default_answer_hu": {"type": "string"},
        "marker_without_pathogen": {"type": "string"},
        "marker_without_pathogen_hu": {"type": "string"},
        "conflict_answer": {"type": "string"},
        # pathway
        "outputs": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "items": {"type": "array"},
                    "text_hu": {"type": "string"},
                    "text_en": {"type": "string"},
                },
            },
        },
        "doses": {"type": "boolean"},
        # table_lookup (2-D {indication_tier}_{renal_category} lookup; tmpsmx)
        "indication_rules": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["tier", "contains"],
                "properties": {
                    "tier": {"type": "string"},
                    "contains": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
            },
        },
        "renal_rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "if": {"type": "string"},
                    "category": {"type": "string"},
                    "default": {"type": "string"},
                    "comment": {"type": "string"},
                },
            },
        },
        "tables": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"enum": ["dosing_table", "fixed_dose",
                                       "prophylaxis", "renal_warning"]},
                    "target": {"type": "string"},
                    "renal_adjustment": {"type": "string"},
                    "text": {"type": "string"},
                    "text_en": {"type": "string"},
                    "text_hu": {"type": "string"},
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["weight_kg", "practical_dose"],
                            "properties": {
                                "weight_kg": {"type": "number"},
                                "target": {"type": "string"},
                                "practical_dose": {"type": "string"},
                                "total": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "prophylaxis_tables": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "info_blocks": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "weight_slot": {"type": "string"},
        "supported_weight_min": {"type": "number"},
        "supported_weight_max": {"type": "number"},
        "output_template_en": {"type": "string"},
        "output_template_hu": {"type": "string"},
        "missing_inputs": {"type": "string"},
        "missing_inputs_hu": {"type": "string"},
        # prose
        "sections": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "text_hu": {"type": "string"},
                    "text_en": {"type": "string"},
                    "text": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    "allOf": [
        {"if": {"properties": {"kind": {"const": "drug_dose"}}},
         "then": {"required": ["tiers", "select"]}},
        {"if": {"properties": {"kind": {"const": "pcr_panel"}}},
         "then": {"required": ["organisms"]}},
        {"if": {"properties": {"kind": {"const": "pathway"}}},
         "then": {"required": ["outputs", "select"]}},
        {"if": {"properties": {"kind": {"const": "prose"}}},
         "then": {"required": ["sections"]}},
        {"if": {"properties": {"kind": {"const": "table_lookup"}}},
         "then": {"required": ["tables", "indication_rules", "renal_rules"]}},
    ],
}


# --------------------------------------------------------------------------- #
# FIELD_RULES — what the dependency-light validator walks.                     #
# Each kind lists the fields REQUIRED beyond the common ones. The validator     #
# also enforces shapes/enums structurally (see loader.validate_record).         #
# --------------------------------------------------------------------------- #
COMMON_REQUIRED = ("id", "kind")

KIND_REQUIRED = {
    "drug_dose": ("tiers", "select"),
    "pcr_panel": ("organisms",),
    "pathway": ("outputs", "select"),
    "prose": ("sections",),
    "table_lookup": ("tables", "indication_rules", "renal_rules"),
}

# Fields that only make sense for a given kind. Presence on the wrong kind is a
# warning-level smell, surfaced by the validator (helps catch copy-paste errors
# during the bulk migration). Common fields are allowed on every kind.
KIND_FIELDS = {
    "drug_dose": {"slots", "tiers", "select", "never", "prep", "notes"},
    "pcr_panel": {"organisms", "markers", "disambiguate_genus", "requires",
                  "dose_via", "spectrum_tiers", "default_answer", "default_answer_hu",
                  "marker_without_pathogen", "marker_without_pathogen_hu",
                  "conflict_answer"},
    "pathway": {"slots", "outputs", "select", "doses"},
    "prose": {"sections"},
    "table_lookup": {"slots", "requires", "indication_rules", "renal_rules",
                     "never",
                     "tables", "prophylaxis_tables", "info_blocks",
                     "weight_slot", "supported_weight_min", "supported_weight_max",
                     "output_template_en", "output_template_hu",
                     "default_answer", "default_answer_hu",
                     "missing_inputs", "missing_inputs_hu"},
}

COMMON_FIELDS = set(_COMMON_PROPERTIES.keys())

__all__ = [
    "KINDS", "INTENTS", "SLOT_TYPES", "OUT_OF_RANGE_ACTIONS", "STATUSES",
    "PROTOCOL_JSON_SCHEMA", "COMMON_REQUIRED", "KIND_REQUIRED",
    "KIND_FIELDS", "COMMON_FIELDS",
]
