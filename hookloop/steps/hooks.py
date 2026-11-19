"""Step 4: "watch" the winners and study their first 10 seconds.

For each 1M+ video: grab only the first N seconds (yt-dlp section download, or ffmpeg on a
direct media URL), transcribe the speech, pull 4 frames, and have Claude break the opening
down into a reusable *pattern*: hook type, beat structure, visual pattern and a fill-in-the-
blank template. Templates deliberately strip the creator's specific words so later scripts
copy the structure that works, not someone else's content.
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..llm import image_block
from ..models import Hook, SourceVideo, Topic
from ..providers.transcribe import transcribe
from ..store import Store
from ..util import run

log = logging.getLogger("hookloop.hooks")

HOOK_TYPES = [
    "mistake_callout",  # "You're using X wrong"
    "contrarian",  # "Stop doing X"
    "secret_reveal",  # "Nobody talks about this feature"
    "result_first",  # show the end result, then how
    "list_tease",  # "3 X you need"
    "pov_relatable",  # "POV: ..."
    "question",  # direct question to viewer
    "news_flash",  # "X just dropped"
    "comparison",  # X vs Y
    "challenge",  # "I tried X for 7 days"
    "warning",  # "Don't do X until you see this"
    "other",
]

SCHEMA = {
    "type": "object",
    "properties": {
        "opening_line": {"type": "string", "description": "The literal first spoken sentence (or on-screen text if no speech)."},
        "hook_type": {"type": "string", "enum": HOOK_TYPES},
        "structure": {
            "type": "array",
            "description": "Beat-by-beat breakdown of the first 10 seconds.",
            "items": {
                "type": "object",
                "properties": {"t": {"type": "string", "description": "e.g. 0-2s"}, "beat": {"type": "string"}},
                "required": ["t", "beat"],
            },
        },
        "visual_pattern": {"type": "string", "description": "Framing, text overlay, cuts, b-roll, screen recording, etc."},
        "on_screen_text": {"type": "string"},
        "template": {
            "type": "string",
            "description": "Generalized fill-in-the-blank version of the opening using {placeholders}. Must not reuse "
            "the creator's distinctive phrasing, names or claims, e.g. 'Stop doing {common habit} in {tool}. Do {better move} instead.'",
        },
        "why_it_works": {"type": "string"},
        "score": {"type": "number", "description": "1-10: how transferable/strong this hook pattern is for our niche."},
    },
    "required": ["opening_line", "hook_type", "structure", "visual_pattern", "template", "why_it_works", "score"],
}


def fetch_opening(v: SourceVideo, seconds: int, workdir: Path) -> Path | None:
    """Download only the first `seconds` of the video. Returns a small mp4, or None."""
    out = workdir / f"{v.platform}_{v.video_id}.mp4"
    try:
        if v.media_url:
            run(["ffmpeg", "-y", "-loglevel", "error", "-t", str(seconds), "-i", v.media_url,
                 "-vf", "scale=540:-2", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(out)], timeout=180)
        else:
            stem = out.with_suffix("")
            run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-f", "best[height<=720]/best",
                 "--download-sections", f"*0-{seconds}", "--force-keyframes-at-cuts",
                 "-o", f"{stem}.%(ext)s", "--remux-video", "mp4", v.url], timeout=300)
            got = sorted(workdir.glob(stem.name + ".*"), key=lambda p: p.suffix != ".mp4")
            if got:
                out = got[0]
    except Exception as e:
        log.warning("could not fetch %s: %s", v.url, str(e).splitlines()[0])
        return None
    return out if out.exists() and out.stat().st_size > 0 else None


def extract(clip: Path, workdir: Path) -> tuple[Path | None, list[Path]]:
    wav = workdir / (clip.stem + ".wav")
    try:
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    except Exception:
        wav = None  # silent video
    frames = []
    for t in (0.2, 1.5, 3.5, 7.0):
        f = workdir / f"{clip.stem}_{t:.1f}.jpg"
        try:
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(clip), "-frames:v", "1",
                 "-vf", "scale=360:-2", "-q:v", "4", str(f)])
            if f.exists():
                frames.append(f)
        except Exception:
            break
    return wav, frames


def analyze(llm, v: SourceVideo, transcript: str, frames: list[Path], niche: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": (
            f"Platform: {v.platform}\nViews: {v.views:,}\nCaption: {v.caption[:500]}\n"
            f"Transcript of the first 10 seconds: {transcript or '(none - use the frames)'}\n"
            f"Frames below are at 0.2s, 1.5s, 3.5s and 7s.\n\n"
            f"Break down why this opening holds attention, and abstract it into a reusable pattern for a {niche} channel."
        )},
    ]
    content += [image_block(f) for f in frames]
    return llm.structured(
        system="You are a short-form video strategist who reverse-engineers viral hooks into reusable patterns.",
        content=content,
        tool_name="submit_hook_analysis",
        schema=SCHEMA,
    )


def mine_hooks(cfg: dict[str, Any], store: Store, llm, winners: list[SourceVideo], topic: Topic,
               *, offline: bool = False) -> list[Hook]:
    rc = cfg["research"]
    new: list[Hook] = []
    todo = [v for v in winners if not store.has_hook(v.key)][: int(rc["analyze_top"])]
    with tempfile.TemporaryDirectory(prefix="hookloop_") as td:
        wd = Path(td)
        for v in todo:
            transcript, frames = "", []
            if not offline:
                clip = fetch_opening(v, int(rc["hook_seconds"]), wd)
                if clip:
                    wav, frames = extract(clip, wd)
                    if wav:
                        try:
                            transcript = transcribe(wav, rc["transcriber"], rc["transcribe_model"])
                        except Exception as e:
                            log.warning("transcription failed: %s", e)
                if not clip and not v.caption:
                    continue
            try:
                a = analyze(llm, v, transcript, frames, cfg["niche"])
            except Exception as e:
                log.warning("hook analysis failed for %s: %s", v.url, e)
                continue
            h = Hook(
                source_key=v.key, platform=v.platform, views=v.views,
                opening_line=a.get("opening_line", transcript[:200]), hook_type=a.get("hook_type", "other"),
                template=a.get("template", ""), structure=a.get("structure", []), visual_pattern=a.get("visual_pattern", ""),
                on_screen_text=a.get("on_screen_text", ""), why_it_works=a.get("why_it_works", ""),
                score=float(a.get("score", 5)), topic=topic.name,
            )
            h.id = store.add_hook(h)
            new.append(h)
    log.info("hooks: analyzed %d new openings (library size %d)", len(new), len(store.hooks()))
    return new
