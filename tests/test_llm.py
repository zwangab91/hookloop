"""The Claude wrapper: forced tool output, web-search pause_turn continuation, and nudging."""
from types import SimpleNamespace as NS

from hookloop.config import load_config
from hookloop.llm import LLM


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def make(responses, web=True):
    llm = LLM.__new__(LLM)
    cfg = load_config()
    llm.model, llm.fast_model = cfg["llm"]["model"], cfg["llm"]["fast_model"]
    llm.web_tool, llm.max_searches, llm.web_enabled = cfg["llm"]["web_search_tool"], 3, web
    llm.client = StubClient(responses)
    return llm


def tool(name, data):
    return NS(type="tool_use", name=name, input=data)


def test_forced_tool_without_web():
    llm = make([NS(content=[tool("submit_x", {"a": 1})], stop_reason="tool_use")], web=False)
    assert llm.structured(system="s", content="c", tool_name="submit_x", schema={"type": "object"}) == {"a": 1}
    call = llm.client.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "submit_x"} and len(call["tools"]) == 1


def test_web_search_pause_then_nudge_then_answer():
    llm = make([
        NS(content=[NS(type="server_tool_use", name="web_search")], stop_reason="pause_turn"),
        NS(content=[NS(type="text", text="here is what I found")], stop_reason="end_turn"),
        NS(content=[tool("submit_topic", {"topic": "Claude Code"})], stop_reason="tool_use"),
    ])
    out = llm.structured(system="s", content="c", tool_name="submit_topic", schema={"type": "object"}, web_search=True)
    assert out == {"topic": "Claude Code"}
    first, second, third = llm.client.calls
    assert first["tools"][0]["type"] == "web_search_20250305" and first["tool_choice"] == {"type": "auto"}
    assert second["tool_choice"] == {"type": "auto"}  # pause_turn just continues
    assert third["tool_choice"] == {"type": "tool", "name": "submit_topic"}  # nudged after a text-only answer
    assert third["messages"][-1]["role"] == "user"
