#!/usr/bin/env python3
"""replay_diff.py — Phase 5 replay & parity tool (Plan D step 0.3 / 5.1).

Feed recorded user turns through the **new** id_bot2 pipeline and report what it
decides/answers. With a live model attached (an OPENAI_API_KEY present, and the
old bot importable), it ALSO runs each turn through the **old** bot and diffs the
two answers so a human can triage every difference as *better / equal /
regression* (roadmap 5.2) before cutover.

Offline (no key) this still does real, useful work: it shows the new bot's answer
for every real logged turn and the **deterministic coverage** — how many turns the
free, offline deterministic stage resolves end-to-end vs how many would fall to
the LLM router stage (which needs a key) vs how many are an explicit "not covered"
or "clarify". That coverage number is the pre-cutover signal: the higher it is,
the less the live bot depends on the model.

Sources of turns:
  * a markdown table like `test_questions.md` (messages in `backticks`)
  * `regression_cases.yaml` (each case's `input`)
  * a plain text file (one message per line; blank lines / `#` comments skipped)

Usage:
  python id_bot2/replay_diff.py --source test_questions.md
  python id_bot2/replay_diff.py --source regression_cases.yaml --limit 50
  python id_bot2/replay_diff.py --source test_questions.md --with-old   # needs key
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE / "llm", _HERE / "tools", _HERE / "protocols"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import channel as _ch  # noqa: E402


# --------------------------------------------------------------------------- #
# Loading recorded turns                                                       #
# --------------------------------------------------------------------------- #
_BACKTICK = re.compile(r"`([^`]+)`")


def load_turns(source: str) -> list[str]:
    """Return the list of user-message strings from a source file."""
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"replay source not found: {source}")
    name = path.name.lower()
    text = path.read_text(encoding="utf-8", errors="replace")

    if name.endswith((".yaml", ".yml")):
        return _load_yaml_inputs(path)
    if name.endswith(".md"):
        turns = _load_md_table(text)
        if turns:
            return turns
    # plain text: one message per line
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _load_yaml_inputs(path: Path) -> list[str]:
    import yaml  # local import; only needed for this source
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = doc.get("cases", doc) if isinstance(doc, dict) else doc
    out = []
    for c in cases or []:
        if isinstance(c, dict) and c.get("input"):
            out.append(str(c["input"]))
    return out


def _load_md_table(text: str) -> list[str]:
    """Extract the first backtick-quoted span from each markdown table row."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        m = _BACKTICK.search(s)
        if m:
            msg = m.group(1).strip()
            if msg and msg != "#":
                out.append(msg)
    return out


# --------------------------------------------------------------------------- #
# Running the new pipeline                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class ReplayRow:
    input: str
    route: str = ""
    tool: str = ""
    protocol: Optional[str] = None
    via: Optional[str] = None
    needs_clarification: bool = False
    answer: str = ""
    old_answer: Optional[str] = None
    differs: Optional[bool] = None
    error: Optional[str] = None


def run_new(turns, *, router=None) -> list[ReplayRow]:
    r = router or _ch.get_router()
    rows = []
    for q in turns:
        try:
            res = r.route(q)
            rows.append(ReplayRow(
                input=q, route=res.route, tool=res.tool or "",
                protocol=res.protocol, via=res.via,
                needs_clarification=bool(res.needs_clarification),
                answer=res.answer or ""))
        except Exception as exc:  # noqa: BLE001
            rows.append(ReplayRow(input=q, route="ERROR", error=f"{type(exc).__name__}: {exc}"))
    return rows


def _ensure_old_bot_ready(bot_core) -> None:
    """The old bot's ask_ai reads module globals (rules, aliases, protocols,
    drug-name set) that are only populated by main()'s startup sequence. Replaying
    without this gives degraded/empty old answers — an invalid parity diff. Run the
    same loaders once (best-effort; load_protocols builds embeddings, hence the key)."""
    for fn, args in (("load_rule_files", ()),
                     ("load_aliases", ("protocols/aliases.json",)),
                     ("load_protocols", ()),
                     ("_build_drug_name_set", ())):
        f = getattr(bot_core, fn, None)
        if callable(f):
            try:
                f(*args)
            except Exception as exc:  # noqa: BLE001
                print(f"  [warn] old-bot init {fn}() failed: {type(exc).__name__}: {exc}")


def add_old_answers(rows: list[ReplayRow]) -> None:
    """Run the OLD bot on each turn and record the answer + whether it differs.
    Requires the old bot importable and a live key (it calls the model)."""
    import bot_core  # noqa: E402  (root module)
    _ensure_old_bot_ready(bot_core)
    for row in rows:
        try:
            old = bot_core.ask_ai(row.input, chat_id=-424242)
        except Exception as exc:  # noqa: BLE001
            old = f"<old-bot error: {type(exc).__name__}: {exc}>"
        row.old_answer = old
        row.differs = _norm(old) != _norm(row.answer)


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #
def summarize(rows: list[ReplayRow]) -> dict:
    by_route: dict = {}
    deterministic = llm = 0
    for r in rows:
        by_route[r.route] = by_route.get(r.route, 0) + 1
        if r.via == "deterministic":
            deterministic += 1
        elif r.via == "llm":
            llm += 1
    answered = sum(1 for r in rows if r.route not in ("unsupported", "clarify", "ERROR"))
    return {
        "total": len(rows), "by_route": by_route,
        "answered": answered,
        "deterministic": deterministic, "llm": llm,
        "unsupported": by_route.get("unsupported", 0),
        "clarify": by_route.get("clarify", 0),
        "errors": by_route.get("ERROR", 0),
    }


def print_report(rows: list[ReplayRow], *, with_old: bool, show_answers: bool) -> None:
    for i, r in enumerate(rows, 1):
        head = f"[{i:>3}] {r.route:<11} {(r.tool or '-'):<18} {r.protocol or '-'}"
        print(head)
        print(f"      Q: {r.input}")
        if r.error:
            print(f"      ! {r.error}")
        if show_answers and r.answer:
            first = r.answer.strip().splitlines()[0] if r.answer.strip() else ""
            print(f"      A: {first}")
        if with_old and r.old_answer is not None:
            flag = "DIFF" if r.differs else "same"
            print(f"      [{flag}] old: {r.old_answer.strip().splitlines()[0] if r.old_answer.strip() else ''}")
    s = summarize(rows)
    print("\n" + "=" * 60)
    print(f"  REPLAY SUMMARY — {s['total']} turns")
    print("=" * 60)
    print(f"  answered (a protocol fired)   : {s['answered']}")
    print(f"    via deterministic stage     : {s['deterministic']}  (offline / free)")
    print(f"    via LLM router stage        : {s['llm']}  (needs a key)")
    print(f"  clarify (asked the user)      : {s['clarify']}")
    print(f"  unsupported (not covered)     : {s['unsupported']}")
    if s["errors"]:
        print(f"  ERRORS                        : {s['errors']}")
    print(f"  routes: {s['by_route']}")
    if with_old:
        diffs = sum(1 for r in rows if r.differs)
        print(f"  OLD-vs-NEW differences        : {diffs} / {s['total']}  (triage each: better/equal/regression)")
    if not _ch.has_llm():
        print("\n  NOTE: no OPENAI_API_KEY — the LLM router stage was NOT exercised.")
        print("  Turns that need the model show as 'unsupported' here; rerun with a")
        print("  key (and --with-old) for the true parity diff.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Replay recorded turns through the new pipeline.")
    ap.add_argument("--source", required=True, help="test_questions.md | regression_cases.yaml | a .txt of messages")
    ap.add_argument("--limit", type=int, default=0, help="only the first N turns (0 = all)")
    ap.add_argument("--with-old", action="store_true", help="also run the old bot and diff (needs a key)")
    ap.add_argument("--answers", action="store_true", help="show the first line of each new answer")
    args = ap.parse_args(argv)

    turns = load_turns(args.source)
    if args.limit:
        turns = turns[: args.limit]
    if not turns:
        print(f"No turns found in {args.source}")
        return 1

    rows = run_new(turns)
    if args.with_old:
        if not _ch.has_llm():
            print("WARNING: --with-old needs a live key; old bot answers may error without one.")
        add_old_answers(rows)
    print_report(rows, with_old=args.with_old, show_answers=args.answers or args.with_old)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
