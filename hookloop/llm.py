"""Thin Claude wrapper that always returns structured JSON via a forced tool call.

`structured()` optionally enables Claude's server-side web search first (for "what is hot
right now" and fact-checking), then requires the answer through the submit tool.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("hookloop.llm")


def image_block(path: str | Path, media_type: str = "image/jpeg") -> dict[str, Any]:
    data = base64.standard_b64encode(Path(path).read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


class LLM:
    def __init__(self, cfg: dict[str, Any]):
        import anthropic  # imported lazily so offline/demo mode needs no key

        self.client = anthropic.Anthropic()
        self.model = cfg["llm"]["model"]
        self.fast_model = cfg["llm"]["fast_model"]
        self.web_tool = cfg["llm"]["web_search_tool"]
        self.max_searches = int(cfg["llm"]["max_searches"])
        self.web_enabled = bool(cfg["llm"]["web_search"])

    def structured(
        self,
        *,
        system: str,
        content: str | list[dict[str, Any]],
        tool_name: str,
        schema: dict[str, Any],
        description: str = "",
        fast: bool = False,
        web_search: bool = False,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        tools: list[dict[str, Any]] = [
            {"name": tool_name, "description": description or f"Submit the final {tool_name}.", "input_schema": schema}
        ]
        use_web = web_search and self.web_enabled
        if use_web:
            tools.insert(0, {"type": self.web_tool, "name": "web_search", "max_uses": self.max_searches})
            tool_choice: dict[str, Any] = {"type": "auto"}
            system = system + f"\n\nWhen you are done researching, call `{tool_name}` exactly once with your answer."
        else:
            tool_choice = {"type": "tool", "name": tool_name}

        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        for _ in range(8):
            resp = self.client.messages.create(
                model=self.fast_model if fast else self.model,
                max_tokens=max_tokens,
                system=system,
                tools=tools,
                tool_choice=tool_choice,
                messages=messages,
            )
            for block in resp.content:
                if getattr(block, "type", "") == "tool_use" and block.name == tool_name:
                    return dict(block.input)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":  # long-running server tool turn; let it continue
                continue
            messages.append({"role": "user", "content": f"Call `{tool_name}` now with your final answer."})
            tool_choice = {"type": "tool", "name": tool_name}
        raise RuntimeError(f"model never called {tool_name}")


class FakeLLM:
    """Deterministic stand-in used by `hookloop demo` and the tests.

    `responses` maps tool_name -> a dict, or a list of dicts consumed in order.
    """

    def __init__(self, responses: dict[str, Any]):
        self.responses = copy.deepcopy(responses)
        self.calls: list[dict[str, Any]] = []

    def structured(self, *, tool_name: str, **kw) -> dict[str, Any]:
        self.calls.append({"tool": tool_name, **{k: v for k, v in kw.items() if k != "content"}})
        r = self.responses.get(tool_name)
        if r is None:
            raise KeyError(f"FakeLLM has no response for {tool_name}")
        if isinstance(r, list):
            if not r:
                raise KeyError(f"FakeLLM ran out of responses for {tool_name}")
            return copy.deepcopy(r.pop(0) if len(r) > 1 else r[0])
        return copy.deepcopy(r)


def load_fake(path: str | Path) -> FakeLLM:
    return FakeLLM(json.loads(Path(path).read_text()))
