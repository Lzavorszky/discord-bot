"""provider.py — the LLM swappability seam (Phase 1).

Every model-specific detail lives behind `LLMProvider`. OpenAI is the first
implementation; DeepSeek (OpenAI-compatible) and Anthropic are later drop-ins that
must pass the same contract test (id_bot2/tests/test_provider_contract.py).

Interface
---------
    chat(messages) -> str
        A plain completion. Used for phrasing tool results (PHRASING_MODEL).

    call_with_tools(messages, tools) -> ToolCall | str
        The routing decision. Returns a ToolCall when the model picks a tool,
        or a plain string when it answers directly. Uses ROUTER_MODEL.

`tools` is a sequence of id_bot2.llm.tools.Tool. Each provider renders them to its
own wire shape internally, so callers never touch provider-specific schemas.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional, Protocol, Sequence, Union

from .tools import Tool, ToolCall


class LLMProvider(Protocol):
    """The seam every model backend must satisfy."""

    def chat(self, messages: Sequence[dict]) -> str: ...

    def call_with_tools(
        self, messages: Sequence[dict], tools: Sequence[Tool]
    ) -> Union[ToolCall, str]: ...


# ---------------------------------------------------------------------------
# Defaults — read from config when available, else env, else hard fallback.
# Kept lazy so importing this module never requires config/openai to be present.
# ---------------------------------------------------------------------------
def _cfg(name: str, env: str, fallback: str) -> str:
    try:
        import config  # repo-root module; available when CWD/path includes repo
        val = getattr(config, name, None)
        if val:
            return val
    except Exception:
        pass
    return os.getenv(env, fallback)


class OpenAIProvider:
    """OpenAIProvider — first concrete LLMProvider.

    Models default to config.ROUTER_MODEL (tool calls) and config.PHRASING_MODEL
    (plain chat); override per call with `model=`. The OpenAI client is injectable
    (`client=`) so the parsing logic can be unit-tested offline with a fake client;
    a real client is constructed lazily only when one is not supplied.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        tool_model: Optional[str] = None,
        chat_model: Optional[str] = None,
        client: Any = None,
    ):
        self.tool_model = tool_model or _cfg("ROUTER_MODEL", "ID_BOT2_ROUTER_MODEL", "gpt-5.5")
        self.chat_model = chat_model or _cfg("PHRASING_MODEL", "ID_BOT2_PHRASING_MODEL", "gpt-5.5-mini")
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI  # lazy: only needed for live calls
            self._client = OpenAI(api_key=api_key or _cfg("OPENAI_API_KEY", "OPENAI_API_KEY", ""))

    # -- plain completion ----------------------------------------------------
    def chat(
        self,
        messages: Sequence[dict],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": model or self.chat_model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        resp = self._client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()

    # -- routing decision ----------------------------------------------------
    def call_with_tools(
        self,
        messages: Sequence[dict],
        tools: Sequence[Tool],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Union[ToolCall, str]:
        resp = self._client.chat.completions.create(
            model=model or self.tool_model,
            messages=list(messages),
            tools=[t.to_openai() for t in tools],
            tool_choice="auto",
            temperature=temperature,
        )
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None)
        if calls:
            tc = calls[0]
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (TypeError, json.JSONDecodeError):
                args = {}
            return ToolCall(name=tc.function.name, arguments=args, id=getattr(tc, "id", None), raw=tc)
        return (msg.content or "").strip()


def get_provider(name: Optional[str] = None, **kwargs) -> LLMProvider:
    """Factory: map a provider key (config.ROUTER_PROVIDER) to an implementation.

    DeepSeek/Anthropic register here in later phases; for now only 'openai'.
    """
    key = (name or _cfg("ROUTER_PROVIDER", "ID_BOT2_ROUTER_PROVIDER", "openai")).lower()
    if key == "openai":
        return OpenAIProvider(**kwargs)
    raise ValueError(f"unknown LLM provider: {key!r} (only 'openai' implemented in Phase 1)")
