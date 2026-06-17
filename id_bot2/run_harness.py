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
        # Optional `call:` — an explicit tool invocation for the `new` target
        # (the structured decision the router will later derive from `input`).
        call = c.get("call")
        if call is not None:
            if not isinstance(call, dict):
                problems.append(f"{where}: 'call' must be a mapping")
            else:
                ctool = call.get("tool")
                if ctool not in VALID_TOOLS:
                    problems.append(f"{where}: call.tool {ctool!r} "
                                    f"not in {sorted(VALID_TOOLS)}")
                if ctool in ("interpret_pcr", "list_panel"):
                    # PCR calls key off a panel_id (not a drug_id).
                    if "panel_id" not in call:
                        problems.append(f"{where}: pcr call needs a 'panel_id'")
                    for k in ("organisms", "markers"):
                        if k in call and not isinstance(call[k], list):
                            problems.append(f"{where}: 'call.{k}' must be a list")
                else:
                    if "drug_id" not in call:
                        problems.append(f"{where}: call needs a 'drug_id'")
                    if "slots" in call and not isinstance(call["slots"], dict):
                        problems.append(f"{where}: 'call.slots' must be a mapping")
        if "route_input" in c and not isinstance(c["route_input"], bool):
            problems.append(f"{where}: 'route_input' must be a boolean")
        # Optional `phrase:` — exercise the router's phrasing+grounding-verifier
        # loop offline with a *scripted* phraser (no real model). Keys:
        #   candidate: the text the scripted phrasing model returns
        #   verdict:   "survives" (faithful → kept) | "blocked" (ungrounded → verbatim)
        phrase = c.get("phrase")
        if phrase is not None:
            if not isinstance(phrase, dict):
                problems.append(f"{where}: 'phrase' must be a mapping")
            else:
                if not isinstance(phrase.get("candidate"), str) or not phrase["candidate"].strip():
                    problems.append(f"{where}: phrase needs a non-empty 'candidate' string")
                if phrase.get("verdict") not in {"survives", "blocked"}:
                    problems.append(f"{where}: phrase.verdict must be 'survives' or 'blocked'")
                if c.get("call") is None:
                    problems.append(f"{where}: 'phrase' requires a 'call:' (the dose to phrase)")
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
        # Cases carry either an explicit `call:` (a structured tool invocation we
        # run directly) or `route_input: true` (route the raw input through the
        # router and assert the routing outcome). Everything else SKIPs.
        call = case.get("call")
        if call:
            return _evaluate_new_call(case, call, result)
        if case.get("route_input"):
            return _evaluate_route_input(case, result)
        result["result"] = SKIP
        result["reasons"] = ["no 'call:'/'route_input:' — not part of the slice"]
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

# Tools the `new` target can execute today (grows as Phase 3 lands more tools).
_PROTOCOLS_DIR = str(Path(__file__).resolve().parent / "protocols")


def _get_dose_module():
    """Import the get_dose tool lazily (keeps the offline target dependency-free)."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here / "tools"))
    sys.path.insert(0, _PROTOCOLS_DIR)
    import get_dose as gd  # noqa: E402
    return gd


def _get_pcr_module():
    """Import the interpret_pcr tool lazily."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here / "tools"))
    sys.path.insert(0, _PROTOCOLS_DIR)
    import interpret_pcr as ip  # noqa: E402
    return ip


_ROUTER = None


def _get_router():
    """Build the deterministic Router once (loads the 30 protocols). Used to
    cross-check that each case's `input` routes to the same drug/tool the
    explicit `call:` names — i.e. the router (roadmap 3.5) actually works."""
    global _ROUTER
    if _ROUTER is None:
        here = Path(__file__).resolve().parent
        sys.path.insert(0, str(here))
        from router import Router  # noqa: E402
        _ROUTER = Router(protocols_dir=_PROTOCOLS_DIR)
    return _ROUTER


def _router_crosscheck(case: dict, call: dict) -> list[str]:
    """Route the case `input` through the deterministic router and confirm it
    lands on the same tool/protocol decision as the explicit `call:`. Slots /
    organisms / markers are NOT compared (an input may legitimately under-specify
    them, e.g. 'Amikacin dose' with a representative gfr in the call)."""
    tool = call.get("tool")
    try:
        res = _get_router().route(case["input"])
    except Exception as exc:  # noqa: BLE001
        return [f"router raised {type(exc).__name__}: {exc}"]
    problems: list[str] = []
    if tool == "get_dose":
        if res.route != "drug_dose":
            problems.append(f"router: input routed to {res.route!r}, expected "
                            f"'drug_dose' (answer: {res.answer[:60]!r})")
        if res.tool != "get_dose":
            problems.append(f"router: tool {res.tool!r}, expected 'get_dose'")
        if res.protocol != call["drug_id"]:
            problems.append(f"router: input routed to drug {res.protocol!r}, "
                            f"expected {call['drug_id']!r}")
    elif tool in ("interpret_pcr", "list_panel"):
        if res.route != "pcr_panel":
            problems.append(f"router: input routed to {res.route!r}, expected "
                            f"'pcr_panel' (answer: {res.answer[:60]!r})")
        if res.tool != tool:
            problems.append(f"router: tool {res.tool!r}, expected {tool!r}")
        if res.protocol != call["panel_id"]:
            problems.append(f"router: input routed to panel {res.protocol!r}, "
                            f"expected {call['panel_id']!r}")
    return problems


def _evaluate_new_call(case: dict, call: dict, result: dict) -> dict:
    """Run a case's explicit `call:` against the new-pipeline slice and check
    both the structured expectations (route/tool/protocol/clarifies) and the
    text expectations (output_has/output_not) against the rendered answer."""
    expect = case.get("expect", {}) or {}
    tool = call.get("tool")
    if tool in ("interpret_pcr", "list_panel"):
        return _evaluate_pcr_call(case, call, result)
    if tool != "get_dose":
        result["result"] = SKIP
        result["reasons"] = [f"call.tool {tool!r} not implemented in the slice yet"]
        return result
    try:
        gd = _get_dose_module()
        slots = call.get("slots") or {}
        res = gd.get_dose(call["drug_id"], protocols_dir=_PROTOCOLS_DIR, **slots)
        answer = gd.render_dose(res)
    except Exception as exc:  # noqa: BLE001
        result["result"] = ERROR
        result["reasons"] = [f"{type(exc).__name__}: {exc}"]
        return result

    result["answer"] = answer
    reasons: list[str] = []
    actual = {"route": res.route, "tool": res.tool, "protocol": res.drug_id}
    for key in ("route", "tool", "protocol"):
        if key in expect and expect[key] != actual[key]:
            reasons.append(f"{key}: expected {expect[key]!r}, got {actual[key]!r}")
    if expect.get("clarifies") and not res.needs_confirmation:
        reasons.append("expected a clarifying/confirmation response, got a dose")
    if not expect.get("clarifies") and res.needs_confirmation:
        reasons.append(f"unexpected confirmation request: {res.confirmation_reason}")
    _, text_reasons = check_text_expectations(answer, expect)
    reasons += text_reasons
    # Router cross-check: the deterministic router must derive this same
    # drug/tool from the raw `input` (not just the pre-baked `call:`).
    reasons += _router_crosscheck(case, call)
    # Phrasing + grounding-verifier loop (roadmap 4.1b): with a scripted phraser,
    # confirm a faithful paraphrase survives and an ungrounded one is blocked
    # (falls back to the verbatim tool text). Offline — no real model.
    reasons += _phrase_crosscheck(case, call)
    result["result"] = FAIL if reasons else PASS
    result["reasons"] = reasons
    return result


def _evaluate_pcr_call(case: dict, call: dict, result: dict) -> dict:
    """Run a pcr_panel `call:` (interpret_pcr / list_panel) and check the
    structured + text expectations, plus the router input->call cross-check."""
    expect = case.get("expect", {}) or {}
    try:
        ip = _get_pcr_module()
        pid = call["panel_id"]
        if call["tool"] == "list_panel":
            res = ip.list_panel(pid, protocols_dir=_PROTOCOLS_DIR)
        else:
            res = ip.interpret_pcr(pid, organisms=call.get("organisms") or [],
                                   markers=call.get("markers") or [],
                                   protocols_dir=_PROTOCOLS_DIR)
        answer = ip.render_pcr(res)
    except Exception as exc:  # noqa: BLE001
        result["result"] = ERROR
        result["reasons"] = [f"{type(exc).__name__}: {exc}"]
        return result

    result["answer"] = answer
    reasons: list[str] = []
    actual = {"route": res.route, "tool": res.tool, "protocol": res.panel_id}
    for key in ("route", "tool", "protocol"):
        if key in expect and expect[key] != actual[key]:
            reasons.append(f"{key}: expected {expect[key]!r}, got {actual[key]!r}")
    clar = bool(res.needs_clarification or res.needs_input)
    if expect.get("clarifies") and not clar:
        reasons.append("expected a clarifying response, got an interpretation")
    if "clarifies" in expect and not expect.get("clarifies") and clar:
        reasons.append(f"unexpected clarifying response ({res.clarify_reason})")
    _, text_reasons = check_text_expectations(answer, expect)
    reasons += text_reasons
    reasons += _router_crosscheck(case, call)
    result["result"] = FAIL if reasons else PASS
    result["reasons"] = reasons
    return result


def _evaluate_route_input(case: dict, result: dict) -> dict:
    """Route the raw `input` through the deterministic router and assert the
    routing-level expectations (route/tool/protocol/clarifies/output). Used for
    behavioural cases that resolve to a clarify/unsupported outcome with no single
    tool invocation (e.g. a bare organism with no panel named)."""
    expect = case.get("expect", {}) or {}
    try:
        res = _get_router().route(case["input"])
    except Exception as exc:  # noqa: BLE001
        result["result"] = ERROR
        result["reasons"] = [f"{type(exc).__name__}: {exc}"]
        return result
    answer = res.answer
    result["answer"] = answer
    reasons: list[str] = []
    actual = {"route": res.route, "tool": res.tool, "protocol": res.protocol}
    for key in ("route", "tool", "protocol"):
        if key in expect and expect[key] != actual[key]:
            reasons.append(f"{key}: expected {expect[key]!r}, got {actual[key]!r}")
    if expect.get("clarifies") and not res.needs_clarification:
        reasons.append("expected a clarifying response, got a definitive answer")
    if "clarifies" in expect and not expect.get("clarifies") and res.needs_clarification:
        reasons.append("unexpected clarifying response")
    _, text_reasons = check_text_expectations(answer, expect)
    reasons += text_reasons
    result["result"] = FAIL if reasons else PASS
    result["reasons"] = reasons
    return result


class _ScriptedPhraser:
    """A fixed phrasing-model stand-in for the harness: chat() returns `text`."""
    def __init__(self, text):
        self._text = text

    def chat(self, messages, **kw):
        return self._text

    def call_with_tools(self, *a, **k):  # pragma: no cover - unused
        return ""


def _phrase_crosscheck(case: dict, call: dict) -> list[str]:
    """If the case declares a `phrase:` block, route its `input` through the
    router with a scripted phraser and assert the verifier verdict."""
    phrase = case.get("phrase")
    if not phrase or call.get("tool") != "get_dose":
        return []
    candidate = phrase["candidate"]
    want = phrase["verdict"]
    try:
        res = _get_router().route(case["input"],
                                  phrasing_provider=_ScriptedPhraser(candidate))
    except Exception as exc:  # noqa: BLE001
        return [f"phrase: router raised {type(exc).__name__}: {exc}"]
    if want == "blocked":
        problems = []
        if not res.phrasing_blocked:
            problems.append("phrase: expected the verifier to BLOCK the "
                            "ungrounded phrasing, but it passed")
        if res.answer != res.grounded_answer:
            problems.append("phrase: blocked answer should fall back to the "
                            "verbatim tool text")
        return problems
    # want == "survives"
    problems = []
    if res.phrasing_blocked:
        problems.append("phrase: faithful paraphrase was wrongly blocked by the verifier")
    if not res.phrased:
        problems.append("phrase: expected the paraphrase to be used, but it was not")
    if candidate.strip() not in res.answer:
        problems.append("phrase: surviving answer should contain the candidate text")
    return problems


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
