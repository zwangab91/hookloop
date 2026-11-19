"""Speech-to-text for the first seconds of source videos.

openai: OPENAI_API_KEY + /v1/audio/transcriptions (default model gpt-4o-transcribe)
local:  faster-whisper (pip install hookloop[local-asr])
none:   skip (hook analysis then relies on frames + caption only)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger("hookloop.asr")


def transcribe(audio_path: str | Path, provider: str = "openai", model: str = "gpt-4o-transcribe") -> str:
    if provider == "none":
        return ""
    if provider == "local":
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            log.warning("faster-whisper not installed; skipping transcription")
            return ""
        m = WhisperModel("small", device="cpu", compute_type="int8")
        segs, _ = m.transcribe(str(audio_path), vad_filter=True)
        return " ".join(s.text.strip() for s in segs)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.warning("OPENAI_API_KEY not set; skipping transcription")
        return ""
    p = Path(audio_path)
    with p.open("rb") as f:
        r = httpx.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (p.name, f, "audio/wav")},
            data={"model": model},
            timeout=120,
        )
    r.raise_for_status()
    return r.json().get("text", "").strip()
