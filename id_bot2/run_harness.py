#!/usr/bin/env python3
"""run_harness.py — the regression harness for the ID Bot rebuild (Plan D).

The single source of truth for "is the bot better or worse." Reads
`regression_cases.yaml`, runs each case against a target pipeline, and reports a
pass-rate. It guards every step of the rebuild (design principle 6).

Targets
-------
  none  (default)  Offline. Loads + schema-validates every case and reports the
                   case inventory. Makes NO model calls — this is what `check.sh`
                   runs every session, free. Exits non-zero only if the case file
                   is malformed (a real, catchable failure).
  old   (--live)   Runs each input through the OLD bot (`bot_core.ask_ai`) and
                   checks the text-level expectations (output_has / output_not).
                   This records the Phase 0 baseline. Needs OPENAI_API_KEY.
  new              The Plan D pipeline. Not built until Phase 3 — reported as
                   skipped for now.

Usage
-----
  python id_bot2/run_harness.py regression_cases.yaml            # offline validate
  python id_bot2/run_harness.py regression_cases.yaml --live     # baseline vs old bot
  python id_bot2/run_harness.py regression_cases.yaml --target new
  python id_bot2/run_harness.py regression_cases.yaml --json     # machine-readable

`route`/`tool`/`protocol`/`clarifies` are Plan-D structured-output assertions;
they are SKIPPED for the `old` target (the old bot returns prose, not a tool
trace). They become checkable once the new pipeline emits a decision trace.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("PyYAML is required: pip install pyyaml\n")
    raise

# --- accent-folded, case-insensitive substring matching for clinical tokens ---
sys.path.insert(0, str(Path(__file__).resolve().parent))
from textnorm import fold_accents  # noqa: E402

VALID_ROUTES = {
    "drug_dose", "pcr_panel", "pathway", "prose",
    "clarify", "unsupported", "out_of_scope",
}
VALID_TOOLS = {
    "get_dose", "interpret_pcr", "select_pathway", "answer_from_section",
    "list_panel", "ask_clarification", "none",
}
VALID_STATUS = {"known_fail", "baseline", "new"}
VALID_EXPECT_KEYS = {
    "route", "tool", "protocol", "output_has", "output_not", "clarifies",
}
# Expectations that need the Plan-D structured decision trace (not old-bot prose).
STRUCTURED_KEYS = {"route", "tool", "protocol", "clarifies"}

PASS, FAIL, SKIP, ERROR = "PASS", "FAIL", "SKIP", "ERROR"


# --------------------------------------------------------------------------- #
# Loading & validation                                                        #
# --------------------------------------------------------------------------- #
def load_cases(path: str) -> list[dict]:
    """Parse the YAML case file into a list of case dicts."""
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict) or "cases" not in doc:
        raise ValueError(f"{path}: expected a top-level 'cases:' list")
    cases = doc["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path}: 'cases' must be a non-empty list")
    return cases


def validate_cases(cases: list[dict]) -> list[str]:
    """Return a list of human-readable problems; empty list means valid."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    for i, c in enumerate(cases):
        where = f"case[{i}]"
        cid = c.get("id")
        if not cid:
            problems.append(f"{where}: missing 'id'")
        else:
            where = f"case '{cid}'"
            if cid in seen_ids:
                problems.append(f"{where}: duplicate id")
            seen_ids.add(cid)
        if "input" not in c or not isinstance(c["input"], str) or not c["input"].strip():
            problems.append(f"{where}: missing/empty 'input'")
        status = c.get("status")
        if status not in VALID_STATUS:
            problems.append(f"{where}: status {status!r} not in {sorted(VALID_STATUS)}")
        expect = c.get("expect", {})
        if not isinstance(expect, dict):
            problems.append(f"{where}: 'expect' must be a mapping")
            continue
        for k in expect:
            if k not in VALID_EXPECT_KEYS:
                problems.append(f"{where}: unknown expect key {k!r}")
        if "route" in expect and expect["route"] not in VALID_ROUTES:
            problems.append(f"{where}: route {expect['route']!r} not in {sorted(VALID_ROUTES)}")
        if "tool" in expect and expect["tool"] not in VALID_TOOLS:
            problems.append(f"{where}: tool {expect['tool']!r} not in {sorted(VALID_TOOLS)}")
        for key in ("output_has", "output_not"):
            if key in expect and not isinstance(expect[key], list):
                problems.append(f"{where}: '{key}' must be a list of substrings")
    return problems


# --------------------------------------------------------------------------- #
# Matching helpers                                                            #
# --------------------------------------------------------------------------- #
def _contains(haystack: str, needle: str) -> bool:
    """Accent-folded, case-insensitive substring test (tolerant of HU accents)."""
    return fold_accents(needle) in fold_accents(haystack)


def check_text_expectations(answer: str, expect: dict) -> tuple[str, list[str]]:
    """Check output_has / output_not against an answer string.
    Returns (PASS|FAIL, reasons)."""
    reasons: list[str] = []
    for needle in expect.get("output_has", []):
        if not _contains(answer, needle):
            reasons.append(f"missing expected substring: {needle!r}")
    for needle in expect.get("output_not", []):
        if _contains(answer, needle):
            reasons.append(f"contains forbidden substring: {needle!r}")
    return (FAIL if reasons else PASS), reasons


# --------------------------------------------------------------------------- #
# Targets                                                                     #
# --------------------------------------------------------------------------- #
def evaluate_case(case: dict, target: str, answer_fn=None) -> dict:
    """Evaluate one case against a target. `answer_fn(input, chat_id)->str` is the
    pipeline under test (only used for targets that produce an answer)."""
    expect = case.get("expect", {}) or {}
    result = {"id": case.get("id"), "status": case.get("status"), "result": SKIP,
              "reasons": [], "answer": None}

    if target == "none":
        # Offline: structural validation already done globally; nothing to run.
        result["result"] = SKIP
        result["reasons"] = ["offline validate-only (no pipeline invoked)"]
        return result

    if target == "new":
        result["result"] = SKIP
        result["reasons"] = ["new pipeline not built yet (Phase 3)"]
        return result

    # target == "old": run the old bot and check only text-level expectations.
    text_keys_present = bool(set(expect) & {"output_has", "output_not"})
    structured_only = bool(set(expect) & STRUCTURED_KEYS) and not text_keys_present
    try:
        answer = answer_fn(case["input"], chat_id=_CHAT_ID) if answer_fn else ""
    except Exception as exc:  # noqa: BLE001
        result["result"] = ERROR
        result["reasons"] = [f"{type(exc).__name__}: {exc}"]
        return result
    result["answer"] = answer
    if structured_only:
        result["result"] = SKIP
        result["reasons"] = ["only structured (route/tool) expectations — n/a for old target"]
        return result
    if not text_keys_present:
        result["result"] = SKIP
        result["reasons"] = ["no text-level expectations to check"]
        return result
    verdict, reasons = check_text_expectations(answer, expect)
    result["result"] = verdict
    result["reasons"] = reasons
    return result


_CHAT_ID = -98765  # synthetic chat id for harness runs


def _old_bot_answer_fn():
    """Import the old bot and return its answer callable. Needs a key."""
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY not set — cannot run the old bot baseline")
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    import bot_core  # noqa: E402

    def _answer(text, chat_id=_CHAT_ID):
        return bot_core.ask_ai(text, chat_id)

    return _answer


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #
def run(path: str, target: str = "none", as_json: bool = False) -> int:
    cases = load_cases(path)
    problems = validate_cases(cases)
    if problems:
        if as_json:
            print(json.dumps({"ok": False, "problems": problems}, indent=2))
        else:
            print("CASE FILE INVALID:")
            for p in problems:
                print(f"   - {p}")
        return 1  # malformed case file is a real failure

    answer_fn = None
    if target == "old":
        try:
            answer_fn = _old_bot_answer_fn()
        except Exception as exc:  # noqa: BLE001
            print(f"Cannot run target 'old': {exc}")
            print("Run this where OPENAI_API_KEY is set to record the baseline.")
            return 0  # not a regression — environment limitation, stays green offline

    results = [evaluate_case(c, target, answer_fn) for c in cases]

    counts = {PASS: 0, FAIL: 0, SKIP: 0, ERROR: 0}
    for r in results:
        counts[r["result"]] += 1
    checked = counts[PASS] + counts[FAIL]
    rate = (counts[PASS] / checked * 100) if checked else None

    by_status: dict[str, int] = {}
    for c in cases:
        by_status[c.get("status", "?")] = by_status.get(c.get("status", "?"), 0) + 1

    if as_json:
        print(json.dumps({
            "ok": True, "target": target, "n_cases": len(cases),
            "by_status": by_status, "counts": counts,
            "pass_rate": rate, "results": results,
        }, indent=2, ensure_ascii=False))
        return 0 if counts[ERROR] == 0 else 1

    print(f"Harness target: {target}   cases: {len(cases)}")
    print("Inventory by status: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    if target == "none":
        print("Offline validate-only: all cases loaded and schema-valid. ✓")
        print("(No pipeline invoked. Run --live for the old-bot baseline, or "
              "--target new once Phase 3 lands.)")
        return 0
    for r in results:
        if r["result"] in (FAIL, ERROR):
            print(f"   [{r['result']}] {r['id']}: {'; '.join(r['reasons'])}")
    print(f"\nPASS={counts[PASS]} FAIL={counts[FAIL]} "
          f"SKIP={counts[SKIP]} ERROR={counts[ERROR]}")
    print(f"Pass-rate (of checked): {rate:.0f}%" if rate is not None
          else "Pass-rate: n/a (nothing text-checkable for this target)")
    return 0 if counts[ERROR] == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ID Bot regression harness")
    ap.add_argument("cases", help="path to regression_cases.yaml")
    ap.add_argument("--target", choices=["none", "old", "new"], default="none")
    ap.add_argument("--live", action="store_true",
                    help="shortcut for --target old (run against the live old bot)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    target = "old" if args.live else args.target
    return run(args.cases, target=target, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
