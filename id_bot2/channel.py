#!/usr/bin/env python3
"""channel.py — the thin Channel adapter (Plan D design principle 8; cutover seam).

Turns a raw user message into a final answer string using the new id_bot2
pipeline (router → tool → phrase → verify). This is the **single integration
point** a messaging channel calls — Telegram today, WhatsApp later. Telegram I/O,
auth, conversation state, and message-splitting stay in the channel layer
(`bot_core`); this adapter owns only *"message text in → grounded answer out"*.

The cutover (Phase 6) is therefore a one-line switch: `bot_core.ask_ai` either
calls the old pipeline (`_ask_ai_impl`) or this adapter, gated by the
`config.USE_ID_BOT2` flag. The old module stays importable as rollback.

**Offline-safe.** If no `OPENAI_API_KEY` is configured, the LLM router/phrasing
stages are simply not attached: the deterministic stage answers what it can, and
anything it can't resolve returns the explicit "not covered" message. Never a
crash, never a silent or ungrounded dose.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional
import sys as _sys

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE / "llm", _HERE / "tools", _HERE / "protocols"):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))

from router import Router, RouterResult  # noqa: E402


def _config():
    """Best-effort import of the repo config module (root). Returns None offline."""
    try:
        import config  # noqa: E402  type: ignore
        return config
    except Exception:
        return None


def _api_key() -> str:
    cfg = _config()
    key = getattr(cfg, "OPENAI_API_KEY", "") if cfg else ""
    return key or ""


def has_llm() -> bool:
    """True when a live model can be attached (an API key is configured)."""
    return bool(_api_key().strip())


def _make_provider():
    """Build the OpenAI-backed provider, or None when no key is configured.
    The SAME provider instance serves both the routing tool-call (tool_model) and
    the phrasing rewrite (chat_model) — it picks the right model per method."""
    if not has_llm():
        return None
    try:
        from provider import get_provider  # id_bot2/llm/provider.py
        return get_provider()
    except Exception:
        return None


# Cached Router (loading 46 protocols + building registries is ~2s; do it once).
_ROUTER: Optional[Router] = None
_ROUTER_HAS_LLM: Optional[bool] = None


def get_router(*, protocols_dir: Optional[str] = None, force: bool = False) -> Router:
    """Build (and cache) the pipeline Router. A provider + phrasing provider are
    attached only when an API key is configured; otherwise the Router runs its
    deterministic stage and returns verbatim answers."""
    global _ROUTER, _ROUTER_HAS_LLM
    if _ROUTER is not None and not force and protocols_dir is None:
        return _ROUTER
    provider = _make_provider()
    router = Router(protocols_dir=protocols_dir, provider=provider,
                    phrasing_provider=provider)
    if protocols_dir is None:
        _ROUTER = router
        _ROUTER_HAS_LLM = provider is not None
    return router


def route(question: str, chat_id=None, *, router: Optional[Router] = None) -> RouterResult:
    """Route one message and return the full RouterResult (for debug / replay)."""
    r = router or get_router()
    return r.route(question)


def answer_for(question: str, chat_id=None, *, router: Optional[Router] = None) -> str:
    """Message text in → final grounded answer text out. The single seam the
    Telegram channel calls. Returns the verbatim tool text (offline / no phraser)
    or the phrased-and-verified text (when a phrasing model is attached)."""
    if not question or not question.strip():
        return ""
    result = route(question, chat_id, router=router)
    return result.answer or ""


__all__ = ["answer_for", "route", "get_router", "has_llm"]
