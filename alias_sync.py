"""
alias_sync.py
─────────────
Bidirectional alias sync between individual protocol .txt files
and the central aliases.json.

─── What it does ───────────────────────────────────────────────
Protocol → JSON  : any alias added to a protocol's ## ALIASES section
                   that isn't already in aliases.json gets appended there.

JSON → Protocol  : any alias in aliases.json that is missing from the
                   protocol's ## ALIASES section gets added back.

─── Call at startup (in telegram_bot.py) ───────────────────────
    from alias_sync import run_sync
    run_sync()          # uses defaults: aliases.json in protocols/, bot root as base

─── Standalone / CLI ───────────────────────────────────────────
    python alias_sync.py                              # defaults
    python alias_sync.py protocols/aliases.json .     # explicit paths
"""

import json
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# ─── Helpers ────────────────────────────────────────────────────────────────

def _norm(alias: str) -> str:
    """Lowercase + strip for comparison only. Never stored."""
    return alias.strip().lower()


def _parse_protocol_aliases(protocol_path: Path) -> list[str]:
    """
    Return the aliases listed under ## ALIASES in a protocol file.
    Returns [] when the section is absent, empty, or explicitly '(none)'.
    """
    try:
        text = protocol_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("Protocol file not found: %s", protocol_path)
        return []

    # Match ## ALIASES … up to the next ## heading or EOF
    match = re.search(
        r"^## ALIASES\s*$\n(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []

    section = match.group(1).strip()
    if not section or section.lower() == "(none)":
        return []

    aliases = []
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("- "):
            alias = line[2:].strip()
            if alias:
                aliases.append(alias)
    return aliases


def _append_to_protocol_aliases(protocol_path: Path, to_add: list[str]) -> None:
    """
    Append *to_add* entries to the ## ALIASES section of a protocol file.
    Inserts them just before the next ## heading (or EOF).
    No-op if to_add is empty.
    """
    if not to_add:
        return

    text = protocol_path.read_text(encoding="utf-8")

    # Find the start-of-section line
    header_match = re.search(r"^## ALIASES\s*$", text, re.MULTILINE)
    if not header_match:
        log.warning("Cannot locate ## ALIASES section in %s — skipping", protocol_path.name)
        return

    section_body_start = header_match.end()  # right after the header line

    # Find the next ## heading or EOF
    next_header = re.search(r"^## ", text[section_body_start:], re.MULTILINE)
    if next_header:
        section_body_end = section_body_start + next_header.start()
    else:
        section_body_end = len(text)

    # Build the block to inject (blank line before if section already has content)
    existing_body = text[section_body_start:section_body_end]
    separator = "" if not existing_body.strip() else ""   # always append directly
    new_lines = "\n".join(f"- {a}" for a in to_add) + "\n"

    new_body = existing_body.rstrip("\n") + "\n" + new_lines + "\n"
    new_text = text[:section_body_start] + new_body + text[section_body_end:]
    protocol_path.write_text(new_text, encoding="utf-8")
    log.info("  → protocol %s: +%d alias(es): %s", protocol_path.name, len(to_add), to_add)


# ─── Main sync ──────────────────────────────────────────────────────────────

def run_sync(
    aliases_json_path: "str | Path" = "protocols/aliases.json",
    base_dir: "str | Path" = ".",
) -> dict:
    """
    Run the bidirectional sync.

    Parameters
    ----------
    aliases_json_path : path to aliases.json (relative paths resolved from cwd)
    base_dir          : root directory; values of `protocol_file` in aliases.json
                        are resolved relative to this directory.

    Returns
    -------
    dict with keys:
        json_updated       – list of {entry, added} dicts (changes written to JSON)
        protocols_updated  – list of {file, added} dicts (changes written to .txt)
        errors             – list of error strings
    """
    aliases_path = Path(aliases_json_path)
    base = Path(base_dir)

    if not aliases_path.exists():
        msg = f"aliases.json not found at {aliases_path.resolve()}"
        log.error(msg)
        return {"json_updated": [], "protocols_updated": [], "errors": [msg]}

    with aliases_path.open(encoding="utf-8") as f:
        data = json.load(f)

    json_dirty = False
    report: dict = {"json_updated": [], "protocols_updated": [], "errors": []}

    for category, entries in data.items():
        if not isinstance(entries, dict):
            continue

        for key, entry in entries.items():
            proto_rel = entry.get("protocol_file")
            if not proto_rel:
                continue

            protocol_path = base / proto_rel

            # ── parse protocol aliases ──────────────────────────────────
            proto_aliases = _parse_protocol_aliases(protocol_path)

            # Skip if file missing (already warned inside helper)
            if not protocol_path.exists():
                report["errors"].append(f"Missing protocol file: {protocol_path}")
                continue

            json_aliases: list = entry.get("aliases", [])
            json_norm  = {_norm(a) for a in json_aliases}
            proto_norm = {_norm(a) for a in proto_aliases}

            # ── Protocol → JSON ─────────────────────────────────────────
            new_for_json = [a for a in proto_aliases if _norm(a) not in json_norm]
            if new_for_json:
                entry["aliases"].extend(new_for_json)
                json_dirty = True
                json_norm.update(_norm(a) for a in new_for_json)  # keep in sync
                log.info("[%s/%s] JSON ← protocol: +%d: %s", category, key, len(new_for_json), new_for_json)
                report["json_updated"].append({"entry": f"{category}/{key}", "added": new_for_json})

            # ── JSON → Protocol ─────────────────────────────────────────
            new_for_proto = [a for a in json_aliases if _norm(a) not in proto_norm]
            if new_for_proto:
                _append_to_protocol_aliases(protocol_path, new_for_proto)
                report["protocols_updated"].append({"file": proto_rel, "added": new_for_proto})

    # ── persist JSON if changed ─────────────────────────────────────────────
    if json_dirty:
        with aliases_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log.info("aliases.json saved.")

    # ── summary log ────────────────────────────────────────────────────────
    total_json  = sum(len(r["added"]) for r in report["json_updated"])
    total_proto = sum(len(r["added"]) for r in report["protocols_updated"])
    if total_json or total_proto:
        log.info(
            "alias_sync done: +%d to aliases.json, +%d to protocol file(s).",
            total_json, total_proto,
        )
    else:
        log.debug("alias_sync: nothing to update.")

    return report


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    aliases_arg  = sys.argv[1] if len(sys.argv) > 1 else "protocols/aliases.json"
    base_arg     = sys.argv[2] if len(sys.argv) > 2 else "."

    result = run_sync(aliases_arg, base_arg)

    print("\n═══ Alias sync report ═══")

    if result["json_updated"]:
        print(f"\n▸ aliases.json  ({sum(len(r['added']) for r in result['json_updated'])} new alias(es) across "
              f"{len(result['json_updated'])} entr(ies)):")
        for item in result["json_updated"]:
            print(f"  {item['entry']}")
            for a in item["added"]:
                print(f"    + {a}")
    else:
        print("\n▸ aliases.json:  no changes")

    if result["protocols_updated"]:
        print(f"\n▸ Protocol files  ({sum(len(r['added']) for r in result['protocols_updated'])} new alias(es) across "
              f"{len(result['protocols_updated'])} file(s)):")
        for item in result["protocols_updated"]:
            print(f"  {item['file']}")
            for a in item["added"]:
                print(f"    + {a}")
    else:
        print("\n▸ Protocol files: no changes")

    if result["errors"]:
        print(f"\n▸ Errors ({len(result['errors'])}):")
        for e in result["errors"]:
            print(f"  ✗ {e}")
