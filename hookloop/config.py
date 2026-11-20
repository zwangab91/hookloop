"""Configuration: defaults deep-merged with config.yaml; ${ENV_VAR} / ${ENV_VAR:-default} expanded.

Secrets (API keys, tokens) are read from environment variables by the providers,
never stored in the YAML file.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "niche": "AI tools and AI engineering",
    "handle": "@yourhandle",
    "state_dir": "state",
    "output_dir": "output",
    # Step 1 + 6: topic selection and daily rotation
    "topics": {
        # auto: trend-first when research data is available, else Claude web-searches the area
        # trend: always try the 1M+ trend scan first | niche: skip the scan
        "mode": "auto",
        "scan_hashtags": ["ai", "aitools", "chatgpt", "claudeai", "aiagents", "machinelearning", "cursorai",
                          "artificialintelligence"],
        "trend_window_days": 14,  # only videos posted this recently count as "trending"
        "trend_min_videos": 3,  # fewer 1M+ videos than this in the window -> fall back to web search
        "niche_hashtags": ["ai", "aitools", "artificialintelligence"],
        "pool": [  # used by the fallback; Claude may also propose a trending subtopic inside the area
            "Claude Code",
            "ChatGPT",
            "Gemini",
            "Cursor",
            "AI agents",
            "MCP servers",
            "Context engineering",
            "RAG",
            "LLM evals",
            "Fine-tuning vs prompting",
            "Open-weight models",
            "How LLMs work",
            "Vibe coding",
            "AI for data analysis",
        ],
        "discover_trending": True,  # fallback: let Claude web-search what is hot this week
        "rotation_days": 7,  # never repeat a topic inside this window
    },
    # Steps 2-4: research + hook mining
    "research": {
        "platforms": ["tiktok", "instagram", "youtube"],
        "min_views": 1_000_000,
        "max_age_days": 120,
        "max_duration_s": 90,
        "fallback_top_n": 5,  # if nothing clears min_views, still learn from the top N
        "analyze_top": 6,  # how many winners to "watch" per run
        "hook_seconds": 10,
        "apify": {
            "tiktok": {
                "actor": "clockworks~tiktok-scraper",
                "input": {
                    "resultsPerPage": 30,
                    "shouldDownloadVideos": False,
                    "shouldDownloadCovers": False,
                    "shouldDownloadSubtitles": False,
                },
            },
            "instagram": {
                "actor": "apify~instagram-hashtag-scraper",
                "input": {"resultsType": "reels", "resultsLimit": 30},
            },
        },
        "youtube": {"max_results": 50},
        "transcriber": "openai",  # openai | local | none
        "transcribe_model": "gpt-4o-transcribe",
    },
    "llm": {
        "backend": "auto",  # auto | api (ANTHROPIC_API_KEY) | claude-cli (your subscription via `claude -p`)
        "cli_timeout_s": 900,
        "model": "claude-sonnet-5-5",
        "fast_model": "claude-haiku-5-5",
        "web_search": True,
        "web_search_tool": "web_search_20250305",
        "max_searches": 5,
    },
    # Step 5: script
    "script": {
        "max_seconds": 45,
        "words_per_second": 2.6,
        "persona": "An ML engineer who has shipped AI systems at scale, explaining AI tools and how they actually "
        "work in plain words. One concrete, copyable idea per video. No hype, no filler, talks like a friend at a desk.",
        "cta": "Follow for one practical AI tip a day.",
        "max_revisions": 2,
    },
    "tts": {
        "provider": "kokoro",  # kokoro (free, local) | openai | elevenlabs | espeak
        "gap_s": 0.12,
        "kokoro": {"voice": "am_michael", "speed": 1.05, "lang": "en-us", "model_dir": "~/.cache/hookloop/kokoro"},
        "openai": {
            "model": "gpt-4o-mini-tts",
            "voice": "ash",
            "instructions": "Energetic, confident creator voice. Fast but clear. Punchy on the first line.",
        },
        "elevenlabs": {"voice_id": "", "model_id": "eleven_multilingual_v2"},
    },
    "render": {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "crf": 20,
        "ken_burns": True,
        "music": "",  # optional path to a royalty-free track
        "music_volume": 0.07,
        "theme": {
            "bg_top": "#0B0B14",
            "bg_bottom": "#23123A",
            "glow_a": "#FF7A45",
            "glow_b": "#7C5CFF",
            "accent": "#FF7A45",
            "highlight": "#FFD54A",
            "text": "#FFFFFF",
            "code_bg": "#0D1117",
        },
        "fonts": {"heavy": "", "bold": "", "mono": ""},  # empty = auto-detect
    },
    # Step 6: publishing
    "publish": {
        "mode": "auto",  # auto = publish immediately | review = render, notify, wait for `hookloop approve`
        "platforms": {
            "tiktok": {
                "enabled": True,
                "method": "direct",  # direct (video.publish) | inbox (video.upload -> drafts, 1 tap to post)
                "privacy": "PUBLIC_TO_EVERYONE",
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
                "post_info_extra": {},
            },
            "instagram": {
                "enabled": True,
                "api_host": "graph.instagram.com",  # or graph.facebook.com (Facebook Login)
                "api_version": "v25.0",
                "share_to_feed": True,
            },
            "youtube": {
                "enabled": True,
                "privacy": "public",
                "category_id": "28",  # Science & Technology
                "contains_synthetic_media": True,
            },
            "webhook": {"enabled": False, "url": "${HOOKLOOP_WEBHOOK_URL}"},  # fan out via n8n / Make
        },
    },
    # Step 7: growth
    "growth": {
        "monetize_at_followers": 10_000,
        "metrics_window_days": 14,
        "reward_after_hours": 48,
    },
    "notify": {"slack_webhook": "${SLACK_WEBHOOK_URL}"},
}

_ENV = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        p = Path(path)
        if p.exists():
            cfg = _merge(cfg, yaml.safe_load(p.read_text()) or {})
        else:
            raise FileNotFoundError(f"config not found: {p}")
    return _expand(cfg)
