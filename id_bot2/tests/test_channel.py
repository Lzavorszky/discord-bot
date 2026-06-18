#!/usr/bin/env python3
"""Tests for the Channel adapter (channel.py) and replay tool (replay_diff.py).

These are the cutover seam. Offline (no API key) the adapter must run the
deterministic stage and return verbatim answers — never crash, never a silent
dose. The bot_core gate is exercised too: default OFF keeps the old pipeline;
flipping config.USE_ID_BOT2 routes through the adapter.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1]
for _p in (_PKG, _PKG / "llm", _PKG / "tools", _PKG / "protocols"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import channel as ch          # noqa: E402
import replay_diff as rd      # noqa: E402

PROTOCOLS = str(_PKG / "protocols")


@pytest.fixture(scope="module")
def router():
    # No provider attached (offline) — deterministic stage only.
    from router import Router
    return Router(protocols_dir=PROTOCOLS)


# --------------------------------------------------------------------------- #
# Channel adapter                                                              #
# --------------------------------------------------------------------------- #
def test_answer_for_drug_is_verbatim(router):
    ans = ch.answer_for("meropenem gfr 40", router=router)
    assert "meropenem" in ans.lower()
    assert "g/day" in ans.lower()           # a real dose came back


def test_answer_for_prose(router):
    ans = ch.answer_for("aspirin before surgery", router=router)
    assert "no need to omit" in ans.lower()


def test_answer_for_off_scope_is_explicit_not_silent(router):
    ans = ch.answer_for("what is my wife's name?", router=router)
    assert "don't have an uploaded protocol" in ans.lower()
    assert ans.strip()                       # never empty / silent


def test_answer_for_empty_returns_empty(router):
    assert ch.answer_for("", router=router) == ""
    assert ch.answer_for("   ", router=router) == ""


def test_route_returns_full_result(router):
    res = ch.route("meropenem gfr 40", router=router)
    assert res.route == "drug_dose"
    assert res.protocol == "meropenem"


def test_has_llm_false_without_key(monkeypatch):
    # Force the no-key path regardless of the ambient environment.
    monkeypatch.setattr(ch, "_api_key", lambda: "")
    assert ch.has_llm() is False


def test_get_router_offline_has_no_provider(monkeypatch):
    monkeypatch.setattr(ch, "_api_key", lambda: "")
    r = ch.get_router(protocols_dir=PROTOCOLS)   # protocols_dir => uncached fresh build
    assert r.provider is None
    assert r.phrasing_provider is None


def test_make_provider_imports_cleanly_with_key(monkeypatch):
    # Regression: _make_provider used `from provider import ...` which failed with
    # a relative-import error and was silently swallowed -> LLM stage never ran.
    # With a key present and the OpenAI client stubbed, a provider MUST be built.
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(ch, "_api_key", lambda: "sk-test-key")
    prov = ch._make_provider()
    assert prov is not None, "provider should build when a key is present"


def test_get_router_with_key_attaches_provider(monkeypatch):
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(ch, "_api_key", lambda: "sk-test-key")
    r = ch.get_router(protocols_dir=PROTOCOLS)   # fresh build
    assert r.provider is not None
    assert r.phrasing_provider is not None


# --------------------------------------------------------------------------- #
# Replay tool                                                                  #
# --------------------------------------------------------------------------- #
def test_load_md_table_extracts_backticked_messages():
    md = (
        "| # | Message | Notes |\n"
        "|---|---------|-------|\n"
        "| 1 | `meropenem gfr 40` | dose |\n"
        "| 2 | `aspirin before surgery` | prose |\n"
    )
    turns = rd._load_md_table(md)
    assert turns == ["meropenem gfr 40", "aspirin before surgery"]


def test_load_turns_plaintext(tmp_path):
    f = tmp_path / "turns.txt"
    f.write_text("meropenem gfr 40\n# a comment\n\naspirin before surgery\n", encoding="utf-8")
    assert rd.load_turns(str(f)) == ["meropenem gfr 40", "aspirin before surgery"]


def test_run_new_and_summary(router):
    turns = ["meropenem gfr 40", "aspirin before surgery", "what is my wife's name?"]
    rows = rd.run_new(turns, router=router)
    assert [r.route for r in rows] == ["drug_dose", "prose", "unsupported"]
    s = rd.summarize(rows)
    assert s["total"] == 3
    assert s["answered"] == 2
    assert s["unsupported"] == 1


def test_run_new_never_raises_on_bad_input(router):
    rows = rd.run_new(["", "???", "meropenem gfr 40"], router=router)
    assert len(rows) == 3
    assert all(r.error is None for r in rows)   # adapter degrades gracefully


# --------------------------------------------------------------------------- #
# bot_core cutover gate (default OFF; flag flips to the adapter)               #
# --------------------------------------------------------------------------- #
def _import_bot_core(monkeypatch):
    """Import the old bot_core in a no-key sandbox: it builds an OpenAI client at
    module import (bot_core.py), so stub the constructor first (mirrors test_bot.py).
    Skips cleanly if the old module can't be imported for another reason."""
    import os
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
    try:
        import openai
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **k: object(), raising=False)
    except Exception:
        pass
    try:
        import bot_core  # noqa: E402
        return bot_core
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"bot_core not importable in this environment: {exc}")


def test_bot_core_gate_default_off_uses_old(monkeypatch):
    bc = _import_bot_core(monkeypatch)
    import config
    monkeypatch.setattr(config, "USE_ID_BOT2", False, raising=False)
    monkeypatch.setattr(bc, "_ask_ai_impl", lambda q, c: "OLD-PIPELINE")
    monkeypatch.setattr(bc, "_answer_via_id_bot2", lambda q, c: "NEW-PIPELINE")
    assert bc.ask_ai("meropenem gfr 40", chat_id=-1) == "OLD-PIPELINE"


def test_bot_core_gate_flag_on_uses_new(monkeypatch):
    bc = _import_bot_core(monkeypatch)
    import config
    monkeypatch.setattr(config, "USE_ID_BOT2", True, raising=False)
    monkeypatch.setattr(bc, "_ask_ai_impl", lambda q, c: "OLD-PIPELINE")
    monkeypatch.setattr(bc, "_answer_via_id_bot2", lambda q, c: "NEW-PIPELINE")
    assert bc.ask_ai("meropenem gfr 40", chat_id=-1) == "NEW-PIPELINE"



# --------------------------------------------------------------------------- #
# Minimal conversation memory (Decision 2): active drug + numeric follow-ups   #
# --------------------------------------------------------------------------- #
def test_memory_numeric_followup_applies_to_active_drug(router):
    ch.reset_memory()
    a1 = ch.answer_for("meropenem", chat_id=101, router=router)
    assert "meropenem" in a1.lower()
    a2 = ch.answer_for("gfr 40", chat_id=101, router=router)   # bare follow-up
    assert "g/day" in a2.lower()              # applied to meropenem, real dose
    assert "don't have an uploaded protocol" not in a2.lower()


def test_memory_cleared_on_new_subject(router):
    ch.reset_memory()
    ch.answer_for("meropenem", chat_id=102, router=router)
    ch.answer_for("aspirin before surgery", chat_id=102, router=router)  # new subject
    a3 = ch.answer_for("gfr 40", chat_id=102, router=router)
    assert "don't have an uploaded protocol" in a3.lower()    # memory dropped


def test_memory_no_numeric_stays_unsupported(router):
    ch.reset_memory()
    ch.answer_for("meropenem", chat_id=103, router=router)
    a = ch.answer_for("tell me something", chat_id=103, router=router)
    assert "don't have an uploaded protocol" in a.lower()


def test_memory_is_per_chat(router):
    ch.reset_memory()
    ch.answer_for("meropenem", chat_id=104, router=router)
    other = ch.answer_for("gfr 40", chat_id=999, router=router)   # different chat
    assert "don't have an uploaded protocol" in other.lower()


def test_stateless_when_no_chat_id(router):
    ch.reset_memory()
    ch.answer_for("meropenem", chat_id=None, router=router)
    a = ch.answer_for("gfr 40", chat_id=None, router=router)
    assert "don't have an uploaded protocol" in a.lower()
