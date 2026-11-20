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


# ---------------- Claude Code CLI backend (subscription)
import base64 as _b64
import json as _json
import os as _os
import stat as _stat

import pytest as _pytest

from hookloop.llm import ClaudeCLI, make_llm

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log = os.environ["FAKE_CLAUDE_LOG"]
images = []
if "--add-dir" in args:
    d = args[args.index("--add-dir") + 1]
    images = sorted(os.listdir(d))
with open(log, "w") as f:
    json.dump({"args": args, "cwd": os.getcwd(), "images": images, "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")}, f)
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "error":
    print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "ran out of turns"}))
elif mode == "legacy":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps({"topic": "Claude Skills"})}))
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                      "structured_output": {"topic": "Claude Code Code Mode", "hashtags": ["claudecode"]}}))
'''


@_pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    b = tmp_path / "bin" / "claude"
    b.parent.mkdir()
    b.write_text(FAKE_CLAUDE)
    b.chmod(b.stat().st_mode | _stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{b.parent}{_os.pathsep}{_os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "call.json"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path / "call.json"


def _arg(args, flag):
    return args[args.index(flag) + 1]


def test_auto_backend_picks_cli_without_api_key(fake_claude):
    assert isinstance(make_llm(load_config()), ClaudeCLI)


def test_auto_backend_prefers_api_key_when_present(fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test")
    assert isinstance(make_llm(load_config()), LLM)


def test_cli_web_search_call(fake_claude):
    cfg = load_config()
    out = ClaudeCLI(cfg).structured(system="research lead", content="pick a topic", tool_name="submit_topic",
                                    schema={"type": "object", "properties": {"topic": {"type": "string"}}},
                                    web_search=True)
    assert out == {"topic": "Claude Code Code Mode", "hashtags": ["claudecode"]}
    call = _json.loads(fake_claude.read_text())
    a = call["args"]
    assert a[0] == "-p" and "pick a topic" in a[1]
    assert _arg(a, "--output-format") == "json" and _json.loads(_arg(a, "--json-schema"))["type"] == "object"
    assert _arg(a, "--model") == cfg["llm"]["model"] and _arg(a, "--system-prompt") == "research lead"
    assert _arg(a, "--allowedTools") == "WebSearch,WebFetch" and "Bash" in _arg(a, "--disallowedTools")
    assert call["token"] == "sk-ant-oat01-test"  # subscription token reaches the CLI
    assert "hookloop_cli_" in call["cwd"]  # runs outside the repo (no project CLAUDE.md / settings)


def test_cli_images_become_files_and_fast_model(fake_claude):
    cfg = load_config()
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64.b64encode(b"\xff\xd8jpg").decode()}}
    ClaudeCLI(cfg).structured(system="s", content=[{"type": "text", "text": "analyze"}, img, img],
                              tool_name="submit_hook_analysis", schema={"type": "object"}, fast=True)
    call = _json.loads(fake_claude.read_text())
    a = call["args"]
    assert call["images"] == ["image_01.jpeg", "image_02.jpeg"]
    assert "Read" in _arg(a, "--allowedTools") and "image_01.jpeg" in a[1]
    assert _arg(a, "--model") == cfg["llm"]["fast_model"]


def test_cli_error_and_legacy_result(fake_claude, monkeypatch):
    cli = ClaudeCLI(load_config())
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    with _pytest.raises(RuntimeError, match="error_max_turns"):
        cli.structured(system="s", content="c", tool_name="t", schema={"type": "object"})
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "legacy")
    assert cli.structured(system="s", content="c", tool_name="t", schema={"type": "object"}) == {"topic": "Claude Skills"}


def test_cli_strips_whitespace_from_pasted_token(fake_claude, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-abc\ndef  \n")
    ClaudeCLI(load_config()).structured(system="s", content="c", tool_name="t", schema={"type": "object"})
    assert _json.loads(fake_claude.read_text())["token"] == "sk-ant-oat01-abcdef"


def test_cli_auth_error_explains_the_fix(fake_claude, tmp_path, monkeypatch):
    bad = tmp_path / "bin" / "claude"
    bad.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps({'type':'result','subtype':'success','is_error':True,"
                   "'result':'Failed to authenticate. API Error: 401 OAuth access token is invalid.'}))\n")
    with _pytest.raises(RuntimeError, match="claude setup-token"):
        ClaudeCLI(load_config()).structured(system="s", content="c", tool_name="t", schema={"type": "object"})
