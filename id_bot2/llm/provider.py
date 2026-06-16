"""LLMProvider — the swappability seam (Phase 1, not yet implemented).

Every model-specific detail lives behind this interface. OpenAI is the first
implementation; DeepSeek/Claude are later drop-ins that must pass the same
contract test (id_bot2/tests/test_provider_contract.py, Phase 1).

    chat(messages) -> str
    call_with_tools(messages, tools) -> ToolCall | str
"""
from __future__ import annotations
from typing import Protocol, Sequence, Any


class LLMProvider(Protocol):
    def chat(self, messages: Sequence[dict]) -> str: ...
    def call_with_tools(self, messages: Sequence[dict], tools: Sequence[Any]): ...


# Implementations land in Phase 1.
