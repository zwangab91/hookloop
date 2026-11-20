"""Claude backends. Every backend exposes `structured(...) -> dict` (schema-shaped JSON).

LLM        Claude API via the anthropic SDK (ANTHROPIC_API_KEY, pay-per-token).
ClaudeCLI  The official Claude Code CLI in headless mode (`claude -p --json-schema`), so the
           agent runs on your Claude subscription: CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`
           (or a local `claude` login). Anthropic only allows subscription logins through its own
           apps, which is why this backend shells out to `claude` instead of reusing the token.
make_llm() picks one based on llm.backend (auto | api | claude-cli).
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

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


class ClaudeCLI:
    """Runs each structured call as one `claude -p` invocation with --json-schema."""

    def __init__(self, cfg: dict[str, Any], *, binary: str | None = None,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.bin = binary or shutil.which("claude")
        if not self.bin:
            raise RuntimeError("Claude Code CLI (`claude`) not found; install it with "
                               "`npm install -g @anthropic-ai/claude-code`")
        lc = cfg["llm"]
        self.model, self.fast_model = lc["model"], lc["fast_model"]
        self.web_enabled = bool(lc["web_search"])
        self.max_searches = int(lc["max_searches"])
        self.timeout = int(lc.get("cli_timeout_s", 900))
        self.runner = runner

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
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        with tempfile.TemporaryDirectory(prefix="hookloop_cli_") as td:
            texts, images = [], []
            for i, b in enumerate(blocks):
                if b.get("type") == "text":
                    texts.append(b["text"])
                elif b.get("type") == "image":
                    ext = b["source"].get("media_type", "image/jpeg").split("/")[-1]
                    img = Path(td) / f"image_{i:02d}.{ext}"
                    img.write_bytes(base64.standard_b64decode(b["source"]["data"]))
                    images.append(str(img))
            prompt = "\n\n".join(texts)
            if images:
                prompt += ("\n\nImages, in order (open every one with the Read tool before answering):\n"
                           + "\n".join(images))
            prompt += f"\n\nReply only with the structured output: {description or tool_name}."

            use_web = web_search and self.web_enabled
            allowed = (["Read"] if images else []) + (["WebSearch", "WebFetch"] if use_web else [])
            turns = 4 + len(images) + (self.max_searches + 2 if use_web else 0)
            cmd = [self.bin, "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(schema),
                   "--model", self.fast_model if fast else self.model, "--system-prompt", system,
                   "--max-turns", str(turns), "--disallowedTools", "Bash,Edit,Write,NotebookEdit"]
            if allowed:
                cmd += ["--allowedTools", ",".join(allowed)]
            if images:
                cmd += ["--add-dir", td]
            env = os.environ.copy()
            if env.get("CLAUDE_CODE_OAUTH_TOKEN"):  # tokens never contain whitespace; copy-paste from a wrapped
                env["CLAUDE_CODE_OAUTH_TOKEN"] = "".join(env["CLAUDE_CODE_OAUTH_TOKEN"].split())  # terminal adds it
            p = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout, cwd=td, env=env)
        if p.returncode != 0 and not p.stdout.strip():
            raise RuntimeError(f"claude -p failed ({p.returncode}): {p.stderr.strip()[-500:]}")
        try:
            data = json.loads(p.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as e:
            raise RuntimeError(f"claude -p returned non-JSON output: {p.stdout[-300:]!r}") from e
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            msg = str(data.get("result", ""))[:500]
            if "401" in msg or "authenticate" in msg.lower():
                raise RuntimeError(f"Claude rejected the login ({msg}). The CLAUDE_CODE_OAUTH_TOKEN secret is invalid, "
                                   "expired or revoked: run `claude setup-token` and save the new token with "
                                   "`gh secret set CLAUDE_CODE_OAUTH_TOKEN -R <owner>/hookloop`.")
            raise RuntimeError(f"claude -p error ({data.get('subtype')}): {msg}")
        out = data.get("structured_output")
        if out is None:  # older CLIs: schema-valid JSON arrives in `result`
            try:
                out = json.loads(data.get("result", ""))
            except (TypeError, json.JSONDecodeError) as e:
                raise RuntimeError("claude -p returned no structured_output") from e
        if not isinstance(out, dict):
            raise RuntimeError(f"claude -p structured_output is not an object: {type(out).__name__}")
        return out


def make_llm(cfg: dict[str, Any]):
    """auto: ANTHROPIC_API_KEY -> API; else the Claude Code CLI (subscription token or local login)."""
    backend = cfg["llm"].get("backend", "auto")
    if backend == "api" or (backend == "auto" and os.environ.get("ANTHROPIC_API_KEY")):
        log.info("brain: Claude API")
        return LLM(cfg)
    if backend in ("auto", "claude-cli"):
        log.info("brain: Claude Code CLI (%s)", "subscription token" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "local login")
        return ClaudeCLI(cfg)
    raise ValueError(f"unknown llm.backend {backend!r} (use auto, api or claude-cli)")


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
