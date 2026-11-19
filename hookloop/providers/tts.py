"""Text-to-speech, synthesized one segment at a time so caption timing is exact without
forced alignment.

kokoro      free, local, good quality (model auto-downloads from GitHub, ~120 MB)
openai      OPENAI_API_KEY, gpt-4o-mini-tts (steerable delivery via `instructions`)
elevenlabs  ELEVENLABS_API_KEY + voice_id (use a clone of your own voice)
espeak      last-resort fallback
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import soundfile as sf

from ..util import run

log = logging.getLogger("hookloop.tts")
SR = 24000

KOKORO_FILES = {
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}


def _to_mono24k(src: Path, dst: Path) -> None:
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", str(SR), "-sample_fmt", "s16", str(dst)])


class KokoroTTS:
    def __init__(self, c: dict[str, Any]):
        from kokoro_onnx import Kokoro

        d = Path(os.path.expanduser(c["model_dir"]))
        d.mkdir(parents=True, exist_ok=True)
        for name, url in KOKORO_FILES.items():
            p = d / name
            if not p.exists():
                log.info("downloading %s", name)
                with httpx.stream("GET", url, follow_redirects=True, timeout=600) as r:
                    r.raise_for_status()
                    tmp = p.with_suffix(".part")
                    with tmp.open("wb") as f:
                        for chunk in r.iter_bytes():
                            f.write(chunk)
                    tmp.rename(p)
        self.k = Kokoro(str(d / "kokoro-v1.0.int8.onnx"), str(d / "voices-v1.0.bin"))
        self.voice, self.speed, self.lang = c["voice"], float(c["speed"]), c.get("lang", "en-us")

    def synth(self, text: str, out: Path) -> None:
        samples, sr = self.k.create(text, voice=self.voice, speed=self.speed, lang=self.lang)
        tmp = out.with_suffix(".raw.wav")
        sf.write(tmp, samples, sr)
        _to_mono24k(tmp, out)
        tmp.unlink(missing_ok=True)


class OpenAITTS:
    def __init__(self, c: dict[str, Any]):
        self.key = os.environ["OPENAI_API_KEY"]
        self.c = c

    def synth(self, text: str, out: Path) -> None:
        r = httpx.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {self.key}"},
            json={"model": self.c["model"], "voice": self.c["voice"], "input": text,
                  "instructions": self.c.get("instructions", ""), "response_format": "wav"},
            timeout=120,
        )
        r.raise_for_status()
        tmp = out.with_suffix(".raw.wav")
        tmp.write_bytes(r.content)
        _to_mono24k(tmp, out)
        tmp.unlink(missing_ok=True)


class ElevenLabsTTS:
    def __init__(self, c: dict[str, Any]):
        self.key = os.environ["ELEVENLABS_API_KEY"]
        if not c.get("voice_id"):
            raise ValueError("tts.elevenlabs.voice_id is required")
        self.c = c

    def synth(self, text: str, out: Path) -> None:
        r = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.c['voice_id']}",
            params={"output_format": "pcm_24000"},
            headers={"xi-api-key": self.key},
            json={"text": text, "model_id": self.c["model_id"]},
            timeout=120,
        )
        r.raise_for_status()
        pcm = np.frombuffer(r.content, dtype="<i2")
        sf.write(out, pcm, SR, subtype="PCM_16")


class EspeakTTS:
    def __init__(self, c: dict[str, Any] | None = None):
        self.bin = shutil.which("espeak-ng") or shutil.which("espeak")
        if not self.bin:
            raise RuntimeError("espeak-ng not installed")

    def synth(self, text: str, out: Path) -> None:
        tmp = out.with_suffix(".raw.wav")
        run([self.bin, "-s", "175", "-w", str(tmp), text])
        _to_mono24k(tmp, out)
        tmp.unlink(missing_ok=True)


def make_tts(cfg: dict[str, Any]):
    t = cfg["tts"]
    p = t["provider"]
    if p == "kokoro":
        return KokoroTTS(t["kokoro"])
    if p == "openai":
        return OpenAITTS(t["openai"])
    if p == "elevenlabs":
        return ElevenLabsTTS(t["elevenlabs"])
    if p == "espeak":
        return EspeakTTS()
    raise ValueError(f"unknown tts provider {p}")


class TooLong(Exception):
    pass


def voice_track(tts, texts: list[str], workdir: Path, *, gap_s: float, max_seconds: float,
                tail_s: float = 0.6, max_speedup: float = 1.2) -> tuple[Path, list[tuple[float, float]], float]:
    """Synthesize each text, join with gaps. Returns (wav, [(start,end)] per text, total seconds).

    If the result is longer than max_seconds, speeds it up (<= max_speedup) or raises TooLong.
    """
    parts: list[np.ndarray] = []
    spans: list[tuple[float, float]] = []
    t = 0.0
    gap = np.zeros(int(gap_s * SR), dtype=np.float32)
    for i, text in enumerate(texts):
        p = workdir / f"seg_{i:02d}.wav"
        tts.synth(text, p)
        a, sr = sf.read(p, dtype="float32")
        assert sr == SR
        a = _trim_silence(a)
        dur = len(a) / SR
        spans.append((t, t + dur))
        parts.append(a)
        t += dur
        if i < len(texts) - 1:
            parts.append(gap)
            t += gap_s
    parts.append(np.zeros(int(tail_s * SR), dtype=np.float32))
    total = t + tail_s
    wav = workdir / "voice.wav"
    sf.write(wav, np.concatenate(parts), SR, subtype="PCM_16")

    if total > max_seconds:
        factor = total / (max_seconds - 0.2)
        if factor > max_speedup:
            raise TooLong(f"voiceover is {total:.1f}s; needs {factor:.2f}x speed-up to fit {max_seconds}s")
        fast = workdir / "voice_fit.wav"
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-filter:a", f"atempo={factor:.4f}", str(fast)])
        spans = [(a / factor, b / factor) for a, b in spans]
        total = total / factor
        wav = fast
        log.info("voiceover sped up %.2fx to fit %ss", factor, max_seconds)
    return wav, spans, total


def _trim_silence(a: np.ndarray, thresh: float = 0.01, pad: int = int(0.04 * SR)) -> np.ndarray:
    idx = np.where(np.abs(a) > thresh)[0]
    if len(idx) == 0:
        return a
    return a[max(idx[0] - pad, 0): min(idx[-1] + pad, len(a))]
