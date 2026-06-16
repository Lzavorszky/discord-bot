"""test_provider_contract.py — the LLMProvider seam contract (Phase 1).

The contract every provider must satisfy: given a prompt and a set of tools,
`call_with_tools` returns a ToolCall naming the right tool with arguments that
validate against that tool's schema (or a plain string when no tool fits).

Runs fully OFFLINE and FREE by default:
  * `ScriptedProvider` — a minimal stand-in proving the contract shape.
  * `OpenAIProvider` driven by a FAKE OpenAI client — proves the real parsing
    logic (tool_calls -> ToolCall, content -> str, schema sent on the wire)
    without any network call.
The LIVE OpenAI test (real network, a few cents) is skipped unless both
OPENAI_API_KEY is set and ID_BOT2_LIVE=1.
"""
import json
import os
import sys
from types import SimpleNamespace as NS

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ID_BOT2 = os.path.dirname(HERE)
REPO = os.path.dirname(ID_BOT2)
for p in (REPO, ID_BOT2):
    if p not in sys.path:
        sys.path.insert(0, p)

from llm.tools import Tool, ToolCall            # noqa: E402
from llm.provider import OpenAIProvider, get_provider  # noqa: E402


# --- two fake tools the router must choose between --------------------------
GET_DOSE = Tool(
    name="get_dose",
    description="Look up the renal-adjusted dose of an antibiotic for a patient.",
    parameters={
        "type": "object",
        "properties": {"drug": {"type": "string"}, "crcl": {"type": "number"}},
        "required": ["drug"],
    },
)
INTERPRET_PCR = Tool(
    name="interpret_pcr",
    description="Interpret a respiratory/joint PCR panel result for an organism.",
    parameters={
        "type": "object",
        "properties": {"organism": {"type": "string"}},
        "required": ["organism"],
    },
)
TOOLS = [GET_DOSE, INTERPRET_PCR]


def assert_picks(provider, query, expected_tool):
    """Shared contract assertion used by every provider variant."""
    messages = [
        {"role": "system", "content": "Pick exactly one tool for the user's question."},
        {"role": "user", "content": query},
    ]
    result = provider.call_with_tools(messages, TOOLS)
    assert isinstance(result, ToolCall), f"expected a ToolCall, got {result!r}"
    assert result.name == expected_tool, f"picked {result.name!r}, expected {expected_tool!r}"
    tool = next(t for t in TOOLS if t.name == expected_tool)
    problems = tool.validate_arguments(result.arguments)
    assert not problems, f"invalid args {result.arguments}: {problems}"


# --- Tool abstraction: wire shapes + validation ----------------------------
def test_tool_to_openai_shape():
    s = GET_DOSE.to_openai()
    assert s["type"] == "function"
    assert s["function"]["name"] == "get_dose"
    assert s["function"]["parameters"]["required"] == ["drug"]


def test_tool_to_anthropic_shape():
    s = INTERPRET_PCR.to_anthropic()
    assert s["name"] == "interpret_pcr"
    assert "input_schema" in s and "parameters" not in s
    assert s["input_schema"]["properties"]["organism"]["type"] == "string"


def test_validate_arguments_required_and_types():
    assert GET_DOSE.validate_arguments({"drug": "meropenem", "crcl": 30}) == []
    assert any("missing" in p for p in GET_DOSE.validate_arguments({"crcl": 30}))
    assert any("crcl" in p for p in GET_DOSE.validate_arguments({"drug": "x", "crcl": "lots"}))
    # bool must not satisfy number
    assert GET_DOSE.validate_arguments({"drug": "x", "crcl": True})


# --- offline #1: a minimal scripted provider proves the contract shape ------
class ScriptedProvider:
    """Minimal LLMProvider stand-in: keyword-routes, returns a ToolCall."""

    def chat(self, messages, **kw):
        return "ok"

    def call_with_tools(self, messages, tools, **kw):
        text = messages[-1]["content"].lower()
        if "pcr" in text or "panel" in text:
            return ToolCall(name="interpret_pcr", arguments={"organism": "proteus"})
        return ToolCall(name="get_dose", arguments={"drug": "meropenem", "crcl": 30})


def test_scripted_provider_picks_get_dose():
    assert_picks(ScriptedProvider(), "What is the meropenem dose at CrCl 30?", "get_dose")


def test_scripted_provider_picks_interpret_pcr():
    assert_picks(ScriptedProvider(), "Interpret this PCR panel: Proteus", "interpret_pcr")


# --- offline #2: OpenAIProvider parsing via a fake OpenAI client ------------
def _mk_tool_response(name, args):
    fn = NS(name=name, arguments=json.dumps(args))
    tc = NS(id="call_abc", type="function", function=fn)
    return NS(choices=[NS(message=NS(content=None, tool_calls=[tc]))])


def _mk_text_response(text):
    return NS(choices=[NS(message=NS(content=text, tool_calls=None))])


class _FakeOpenAIClient:
    """Mimics openai.OpenAI just enough for OpenAIProvider; records the last call."""

    def __init__(self, responder):
        self.last_kwargs = None
        self.chat = NS(completions=NS(create=self._create))
        self._responder = responder

    def _create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._responder(kwargs)


def test_openai_provider_parses_tool_call():
    client = _FakeOpenAIClient(lambda kw: _mk_tool_response("get_dose", {"drug": "meropenem", "crcl": 30}))
    provider = OpenAIProvider(client=client, tool_model="test-router")
    assert_picks(provider, "meropenem dose at CrCl 30", "get_dose")


def test_openai_provider_returns_text_when_no_tool():
    client = _FakeOpenAIClient(lambda kw: _mk_text_response("  Hello there  "))
    provider = OpenAIProvider(client=client)
    out = provider.call_with_tools([{"role": "user", "content": "hi"}], TOOLS)
    assert out == "Hello there"


def test_openai_provider_sends_openai_tool_schema_and_model():
    client = _FakeOpenAIClient(lambda kw: _mk_tool_response("get_dose", {"drug": "x"}))
    provider = OpenAIProvider(client=client, tool_model="router-x")
    provider.call_with_tools([{"role": "user", "content": "q"}], TOOLS)
    sent = client.last_kwargs
    assert sent["model"] == "router-x"
    assert sent["tools"][0]["type"] == "function"
    assert {t["function"]["name"] for t in sent["tools"]} == {"get_dose", "interpret_pcr"}
    assert sent["tool_choice"] == "auto"


def test_openai_provider_handles_malformed_arguments():
    """Bad JSON in arguments must degrade to {} rather than crash."""
    fn = NS(name="get_dose", arguments="{not json")
    tc = NS(id="c1", type="function", function=fn)
    resp = NS(choices=[NS(message=NS(content=None, tool_calls=[tc]))])
    provider = OpenAIProvider(client=_FakeOpenAIClient(lambda kw: resp))
    result = provider.call_with_tools([{"role": "user", "content": "q"}], TOOLS)
    assert isinstance(result, ToolCall) and result.arguments == {}


def test_openai_chat_returns_stripped_content():
    client = _FakeOpenAIClient(lambda kw: _mk_text_response("  answer  "))
    provider = OpenAIProvider(client=client, chat_model="phrase-x")
    out = provider.chat([{"role": "user", "content": "hello"}])
    assert out == "answer"
    assert client.last_kwargs["model"] == "phrase-x"


def test_get_provider_factory_returns_openai():
    p = get_provider("openai", client=_FakeOpenAIClient(lambda kw: _mk_text_response("x")))
    assert isinstance(p, OpenAIProvider)
    with pytest.raises(ValueError):
        get_provider("deepseek")


# --- live (network) — opt-in only ------------------------------------------
@pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY") or os.getenv("ID_BOT2_LIVE") != "1",
    reason="live OpenAI test; set OPENAI_API_KEY and ID_BOT2_LIVE=1 to run",
)
def test_openai_provider_live_routes_to_get_dose():
    provider = OpenAIProvider()
    assert_picks(provider, "What meropenem dose should I give at CrCl 30?", "get_dose")
