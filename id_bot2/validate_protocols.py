#!/usr/bin/env python3
"""validate_protocols.py — CI gate for the protocol corpus (Plan D, Phase 2).

Loads every protocol YAML, schema-validates each one, then runs the cross-file
**linter stub**:

  * no duplicate aliases across files (pre-empts the F1-class "wrong protocol
    matched" bug at author time — this is the check the Phase-2 done-when cares
    about);
  * referenced drug ids resolve (a pcr `therapy` / pathway `items` entry that
    names a drug should map to a `kind:drug_dose` protocol). Unresolved
    references are WARNINGS here, not errors, because the corpus is migrated in
    batches — many drugs simply aren't converted yet. The full linter (step 2.6)
    promotes these to hard errors once migration is complete.

Exit code: 0 = green (schema-valid + no alias collisions), 1 = red.
`check.sh` runs this with no arguments; it stays green while `protocols/` is
still empty (Phase 2 hasn't migrated anything yet).

Usage
-----
    python id_bot2/validate_protocols.py                # scan id_bot2/protocols/
    python id_bot2/validate_protocols.py path/to/dir    # scan a directory
    python id_bot2/validate_protocols.py a.yaml b.yaml  # specific files
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "protocols"))
sys.path.insert(0, str(_HERE))

import yaml  # noqa: E402
from textnorm import fold_accents  # noqa: E402
from loader import validate_record  # noqa: E402


# --------------------------------------------------------------------------- #
# Gathering input files                                                       #
# --------------------------------------------------------------------------- #
def gather_files(args: list[str]) -> list[Path]:
    if not args:
        default_dir = _HERE / "protocols"
        return sorted(default_dir.glob("*.y*ml"))
    files: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            files.extend(sorted(p.glob("*.y*ml")))
        else:
            files.append(p)
    return files


def _norm_alias(s: str) -> str:
    """Fold accents + casefold + collapse whitespace, so 'BioFire JI' and
    'biofire  ji' collide, matching how the router will compare names."""
    return " ".join(fold_accents(s).casefold().split())


def _drug_token(s: str) -> str:
    """A drug name normalised to id form: 'Staph aureus' style spacing/case
    folded; spaces and slashes -> underscore (so 'piperacillin/tazobactam'
    can match the id 'piperacillin_tazobactam')."""
    base = _norm_alias(s)
    return base.replace("/", "_").replace("-", "_").replace(" ", "_")


# --------------------------------------------------------------------------- #
# Linter stub                                                                 #
# --------------------------------------------------------------------------- #
def lint_corpus(records: list[tuple[str, dict]]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a list of (filename, record).

    errors  — duplicate aliases across files (hard fail).
    warnings — unresolved drug references (soft, expected mid-migration).
    """
    errors: list[str] = []
    warnings: list[str] = []

    # --- 1. cross-file duplicate aliases (and ids treated as aliases) ---
    alias_owner: dict[str, str] = {}
    for fname, rec in records:
        rid = rec.get("id", fname)
        names = list(rec.get("aliases", []) or [])
        if isinstance(rec.get("id"), str):
            names.append(rec["id"])
        if isinstance(rec.get("canonical_name"), str):
            names.append(rec["canonical_name"])
        seen_here: set[str] = set()
        for name in names:
            key = _norm_alias(name)
            if not key:
                continue
            if key in seen_here:
                continue  # within-file repeat is harmless
            seen_here.add(key)
            prev = alias_owner.get(key)
            if prev is not None and prev != rid:
                errors.append(
                    f"alias collision: {name!r} claimed by both "
                    f"'{prev}' and '{rid}'")
            else:
                alias_owner[key] = rid

    # --- 2. referenced drug ids resolve (warnings only in the stub) ---
    drug_ids = {rec["id"] for _, rec in records
                if rec.get("kind") == "drug_dose" and isinstance(rec.get("id"), str)}
    # any drug_dose id/alias, in token form, is a resolvable target
    resolvable = set()
    for _, rec in records:
        if rec.get("kind") != "drug_dose":
            continue
        for name in [rec.get("id"), rec.get("canonical_name"),
                     *(rec.get("aliases", []) or [])]:
            if isinstance(name, str):
                resolvable.add(_drug_token(name))

    def _maybe_ref(label: str, value: str, rid: str) -> None:
        tok = _drug_token(value)
        if tok and tok not in resolvable:
            warnings.append(f"'{rid}': {label} {value!r} does not resolve to a "
                            f"kind:drug_dose protocol")

    if drug_ids:  # only meaningful once at least one drug is migrated
        for _, rec in records:
            rid = rec.get("id", "?")
            if rec.get("kind") == "pcr_panel":
                for org in rec.get("organisms", []) or []:
                    if isinstance(org, dict) and isinstance(org.get("therapy"), str):
                        _maybe_ref("therapy", org["therapy"], rid)
            elif rec.get("kind") == "pathway":
                for oname, ospec in (rec.get("outputs", {}) or {}).items():
                    if isinstance(ospec, dict):
                        for item in ospec.get("items", []) or []:
                            if isinstance(item, str):
                                _maybe_ref(f"output {oname} item", item, rid)

    return errors, warnings


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #
def run(args: list[str]) -> int:
    files = gather_files(args)
    if not files:
        print("validate_protocols: no protocol files found "
              "(protocols/ is empty — nothing migrated yet). ✓")
        return 0

    schema_errors: list[str] = []
    records: list[tuple[str, dict]] = []
    for f in files:
        if not f.exists():
            schema_errors.append(f"{f}: file not found")
            continue
        try:
            rec = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            schema_errors.append(f"{f.name}: invalid YAML: {exc}")
            continue
        if rec is None:
            schema_errors.append(f"{f.name}: file is empty")
            continue
        problems = validate_record(rec)
        if problems:
            for p in problems:
                schema_errors.append(f"{f.name}: {p}")
        else:
            records.append((f.name, rec))

    lint_errors, warnings = lint_corpus(records)

    print(f"validate_protocols: {len(files)} file(s), "
          f"{len(records)} valid record(s).")
    for w in warnings:
        print(f"   ⚠ {w}")
    all_errors = schema_errors + lint_errors
    if all_errors:
        print(f"\nFAILED — {len(all_errors)} error(s):")
        for e in all_errors:
            print(f"   ✗ {e}")
        return 1
    print("All protocols schema-valid; no cross-file alias collisions. ✓")
    return 0


def main(argv=None) -> int:
    return run(list(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
