"""Step 5b: turn a Script into a vertical 1080x1920 MP4.

Layers: animated background (slow Ken Burns push on a gradient+glow plate) + one transparent
text layer per spoken word (hook card / headline / code card, karaoke captions with the active
word highlighted, progress bar, handle). Text is kept inside the TikTok/Reels safe zone.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..models import Script
from ..providers.tts import voice_track
from ..util import run

log = logging.getLogger("hookloop.render")

W, H = 1080, 1920
LEFT, RIGHT = 80, 150  # right margin clears the like/comment/share rail
CONTENT_W = W - LEFT - RIGHT
CX = LEFT + CONTENT_W // 2
CAPTION_Y = 1250
ZONE_MID = 680  # vertical centre of the headline zone (above captions)

FONT_CANDIDATES = {
    "heavy": ["/usr/share/fonts/opentype/inter/Inter-Black.otf", "/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Black.ttf"],
    "bold": ["/usr/share/fonts/opentype/inter/Inter-Bold.otf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/Library/Fonts/Arial Bold.ttf"],
    "mono": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
             "/System/Library/Fonts/Menlo.ttc"],
}
FC_QUERY = {"heavy": "Inter:style=Black", "bold": "Inter:style=Bold", "mono": "DejaVu Sans Mono:style=Bold"}


def resolve_font(kind: str, override: str = "") -> str:
    if override and Path(override).exists():
        return override
    for p in FONT_CANDIDATES[kind]:
        if Path(p).exists():
            return p
    if shutil.which("fc-match"):
        out = subprocess.run(["fc-match", "-f", "%{file}", FC_QUERY[kind]], capture_output=True, text=True).stdout.strip()
        if out:
            return out
    raise RuntimeError(f"no {kind} font found; set render.fonts.{kind} in config.yaml")


def rgb(h: str, a: int = 255) -> tuple[int, int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a


@dataclass
class Fonts:
    heavy: str
    bold: str
    mono: str

    def get(self, kind: str, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(getattr(self, kind), size)


def wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for w in text.split():
        trial = f"{cur} {w}".strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit(text: str, fonts: Fonts, kind: str, max_w: int, max_lines: int, start: int, smallest: int):
    size = start
    while True:
        f = fonts.get(kind, size)
        lines = wrap(text, f, max_w)
        if (len(lines) <= max_lines and all(f.getlength(line) <= max_w for line in lines)) or size <= smallest:
            return f, lines
        size -= 4


# ---------------------------------------------------------------- background
def background(theme: dict[str, str]) -> Image.Image:
    top, bot = rgb(theme["bg_top"]), rgb(theme["bg_bottom"])
    grad = Image.new("RGBA", (1, H))
    for y in range(H):
        t = y / (H - 1)
        grad.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
    img = grad.resize((W, H))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((-260, 120, 620, 1000), fill=rgb(theme["glow_a"], 120))
    g.ellipse((520, 980, 1400, 1860), fill=rgb(theme["glow_b"], 130))
    glow = glow.filter(ImageFilter.GaussianBlur(170))
    img = Image.alpha_composite(img, glow)
    dots = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(dots)
    for y in range(40, H, 54):
        for x in range(40, W, 54):
            d.ellipse((x - 2, y - 2, x + 2, y + 2), fill=(255, 255, 255, 18))
    return Image.alpha_composite(img, dots)


# ---------------------------------------------------------------- text layer pieces
def draw_hook_card(layer: Image.Image, text: str, fonts: Fonts, theme: dict[str, str]) -> None:
    f, lines = fit(text.upper(), fonts, "heavy", CONTENT_W - 90, 3, 112, 60)
    lh = int(f.size * 1.08)
    card_w = int(max(f.getlength(line) for line in lines)) + 90
    card_h = lh * len(lines) + 70
    card = Image.new("RGBA", (card_w + 40, card_h + 40), (0, 0, 0, 0))
    cd = ImageDraw.Draw(card)
    cd.rounded_rectangle((20, 26, card_w + 20, card_h + 26), radius=34, fill=(0, 0, 0, 110))  # shadow
    cd.rounded_rectangle((20, 14, card_w + 20, card_h + 14), radius=34, fill=rgb(theme["accent"]))
    y = 14 + 35
    for line in lines:
        cd.text((20 + (card_w - f.getlength(line)) / 2, y), line, font=f, fill=(17, 17, 17, 255))
        y += lh
    card = card.rotate(2.2, resample=Image.BICUBIC, expand=True)
    layer.alpha_composite(card, (int(CX - card.width / 2), int(560 - card.height / 2)))


def headline_height(text: str, fonts: Fonts, start: int = 100) -> int:
    if not text:
        return 0
    f, lines = fit(text, fonts, "heavy", CONTENT_W, 3, start, 56)
    return int(f.size * 1.12) * len(lines) + 30


def draw_headline(layer: Image.Image, text: str, fonts: Fonts, theme: dict[str, str], top: int, start: int = 100) -> int:
    if not text:
        return top
    f, lines = fit(text, fonts, "heavy", CONTENT_W, 3, start, 56)
    d = ImageDraw.Draw(layer)
    lh = int(f.size * 1.12)
    y = top
    for line in lines:
        d.text((CX - f.getlength(line) / 2, y), line, font=f, fill=rgb(theme["text"]),
               stroke_width=max(2, f.size // 22), stroke_fill=(0, 0, 0, 200))
        y += lh
    d.rounded_rectangle((CX - 70, y + 18, CX + 70, y + 30), radius=6, fill=rgb(theme["accent"]))
    return y + 30


def _code_color(line: str, theme: dict[str, str]):
    s = line.strip()
    if s.startswith("---") or s.startswith("#") or s.startswith("//"):
        return [(line, (139, 148, 158, 255))]
    m = re.match(r"^(\s*)([A-Za-z_][\w-]*)(:)(.*)$", line)
    if m:
        return [(m.group(1), (230, 237, 243, 255)), (m.group(2) + m.group(3), rgb(theme["accent"])),
                (m.group(4), (230, 237, 243, 255))]
    m = re.match(r"^(\s*)(\d+\.|[-*>$])(\s.*)$", line)
    if m:
        return [(m.group(1), (230, 237, 243, 255)), (m.group(2), rgb(theme["highlight"])), (m.group(3), (230, 237, 243, 255))]
    return [(line, (230, 237, 243, 255))]


def draw_code_card(layer: Image.Image, code: str, fonts: Fonts, theme: dict[str, str], top: int) -> int:
    lines = code.splitlines()[:9] or [""]
    size = 40
    while size > 26:
        f = fonts.get("mono", size)
        if max(f.getlength(line) for line in lines) <= CONTENT_W - 80:
            break
        size -= 2
    f = fonts.get("mono", size)
    lh = int(size * 1.42)
    box_h = 74 + lh * len(lines) + 36
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((LEFT + 6, top + 12, LEFT + CONTENT_W + 6, top + box_h + 12), radius=28, fill=(0, 0, 0, 120))
    d.rounded_rectangle((LEFT, top, LEFT + CONTENT_W, top + box_h), radius=28, fill=rgb(theme["code_bg"], 245),
                        outline=(255, 255, 255, 40), width=2)
    for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        d.ellipse((LEFT + 34 + i * 34, top + 28, LEFT + 54 + i * 34, top + 48), fill=c + (255,))
    y = top + 74
    for line in lines:
        x = LEFT + 40
        for txt, col in _code_color(line, theme):
            d.text((x, y), txt, font=f, fill=col)
            x += f.getlength(txt)
        y += lh
    return top + box_h


def draw_caption(layer: Image.Image, words: list[str], active: int, fonts: Fonts, theme: dict[str, str]) -> None:
    size = 92
    while size > 56:
        f = fonts.get("heavy", size)
        if f.getlength(" ".join(words).upper()) <= CONTENT_W:
            break
        size -= 4
    f = fonts.get("heavy", size)
    up = [w.upper() for w in words]
    space = f.getlength(" ")
    total = sum(f.getlength(w) for w in up) + space * (len(up) - 1)
    lines = [up] if total <= CONTENT_W else [up[: (len(up) + 1) // 2], up[(len(up) + 1) // 2:]]
    d = ImageDraw.Draw(layer)
    y = CAPTION_Y - (len(lines) - 1) * int(size * 0.55)
    idx = 0
    for line in lines:
        lw = sum(f.getlength(w) for w in line) + space * (len(line) - 1)
        x = CX - lw / 2
        for w in line:
            col = rgb(theme["highlight"]) if idx == active else rgb(theme["text"])
            d.text((x, y), w, font=f, fill=col, stroke_width=9, stroke_fill=(0, 0, 0, 255))
            x += f.getlength(w) + space
            idx += 1
        y += int(size * 1.1)


def draw_chrome(layer: Image.Image, progress: float, handle: str, fonts: Fonts, theme: dict[str, str]) -> None:
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((LEFT, 70, W - LEFT, 80), radius=5, fill=(255, 255, 255, 50))
    d.rounded_rectangle((LEFT, 70, LEFT + max(10, int((W - 2 * LEFT) * progress)), 80), radius=5, fill=rgb(theme["accent"]))
    if handle:
        f = fonts.get("bold", 38)
        d.text((CX - f.getlength(handle) / 2, 1440), handle, font=f, fill=(255, 255, 255, 150))


def draw_cta(layer: Image.Image, text: str, handle: str, fonts: Fonts, theme: dict[str, str]) -> None:
    y = draw_headline(layer, text, fonts, theme, top=460, start=96)
    f = fonts.get("heavy", 76)
    label = "+ FOLLOW"
    w = f.getlength(label) + 120
    d = ImageDraw.Draw(layer)
    top = y + 80
    d.rounded_rectangle((CX - w / 2, top, CX + w / 2, top + 130), radius=65, fill=rgb(theme["accent"]))
    d.text((CX - f.getlength(label) / 2, top + 22), label, font=f, fill=(17, 17, 17, 255))
    if handle:
        hf = fonts.get("heavy", 64)
        d.text((CX - hf.getlength(handle) / 2, top + 175), handle, font=hf, fill=rgb(theme["text"]),
               stroke_width=3, stroke_fill=(0, 0, 0, 200))


# ---------------------------------------------------------------- timing
def chunk_words(text: str, max_words: int = 3, max_chars: int = 18) -> list[list[str]]:
    chunks: list[list[str]] = []
    cur: list[str] = []
    for w in text.split():
        cur.append(w)
        if len(cur) >= max_words or len(" ".join(cur)) >= max_chars or re.search(r"[.,!?;:]$", w):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def word_events(script: Script, spans: list[tuple[float, float]], total: float) -> list[dict[str, Any]]:
    """One event per spoken word: (start, end, segment index, chunk words, active index)."""
    ev: list[dict[str, Any]] = []
    for si, (seg, (s0, s1)) in enumerate(zip(script.segments, spans)):
        chunks = chunk_words(seg.voiceover)
        flat = [(ci, wi, w) for ci, ch in enumerate(chunks) for wi, w in enumerate(ch)]
        weights = [len(re.sub(r"\W", "", w)) + 2 for _, _, w in flat]
        tot_w = sum(weights) or 1
        t = s0
        for (ci, wi, _), wt in zip(flat, weights):
            dur = (s1 - s0) * wt / tot_w
            ev.append({"start": t, "seg": si, "chunk": chunks[ci], "active": wi})
            t += dur
    if ev:
        ev[0]["start"] = 0.0
    for a, b in zip(ev, ev[1:]):
        a["end"] = b["start"]
    if ev:
        ev[-1]["end"] = total
    return ev


# ---------------------------------------------------------------- main
def render_layer(script: Script, e: dict[str, Any], total: float, fonts: Fonts, cfg: dict[str, Any]) -> Image.Image:
    theme = cfg["render"]["theme"]
    handle = cfg.get("handle", "")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    seg = script.segments[e["seg"]]
    if seg.style == "hook":
        draw_hook_card(layer, script.hook_overlay or seg.on_screen, fonts, theme)
    elif seg.style == "code":
        n = max(1, min(9, len(seg.code.splitlines())))
        block = headline_height(seg.on_screen, fonts, 72) + 40 + 110 + int(40 * 1.42) * n
        top = max(170, int(ZONE_MID - block / 2))
        y = draw_headline(layer, seg.on_screen, fonts, theme, top=top, start=72)
        draw_code_card(layer, seg.code, fonts, theme, top=y + 40)
    elif seg.style == "cta":
        draw_cta(layer, seg.on_screen, handle, fonts, theme)
    else:
        draw_headline(layer, seg.on_screen, fonts, theme, top=int(ZONE_MID - headline_height(seg.on_screen, fonts) / 2))
    if seg.style != "cta":
        draw_caption(layer, e["chunk"], e["active"], fonts, theme)
    draw_chrome(layer, e["start"] / total if total else 0, handle if seg.style != "cta" else "", fonts, theme)
    return layer


def render(script: Script, cfg: dict[str, Any], tts, out_dir: Path) -> dict[str, Any]:
    rc = cfg["render"]
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    fc = rc["fonts"]
    fonts = Fonts(resolve_font("heavy", fc.get("heavy", "")), resolve_font("bold", fc.get("bold", "")),
                  resolve_font("mono", fc.get("mono", "")))

    voice, spans, total = voice_track(
        tts, [s.voiceover for s in script.segments], work,
        gap_s=float(cfg["tts"]["gap_s"]), max_seconds=float(cfg["script"]["max_seconds"]),
    )
    events = word_events(script, spans, total)

    bg = background(rc["theme"])
    bg_path = work / "bg.png"
    bg.convert("RGB").save(bg_path)

    listing = []
    cover = None
    for i, e in enumerate(events):
        layer = render_layer(script, e, total, fonts, cfg)
        p = work / f"layer_{i:04d}.png"
        layer.save(p, compress_level=1)
        if i == 0:
            cover = Image.alpha_composite(bg, layer).convert("RGB")
        listing.append(f"file '{p.resolve()}'\nduration {max(e['end'] - e['start'], 0.001):.4f}")
    listing.append(f"file '{(work / f'layer_{len(events) - 1:04d}.png').resolve()}'")
    lst = work / "layers.txt"
    lst.write_text("\n".join(listing) + "\n")
    cover_path = out_dir / "cover.jpg"
    if cover:
        cover.save(cover_path, quality=90)

    fps = int(rc["fps"])
    n = int(total * fps) + fps
    video = out_dir / "video.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if rc.get("ken_burns", True):
        cmd += ["-i", str(bg_path)]
        bgf = (f"[0:v]scale={W * 2}:{H * 2},zoompan=z='1+0.07*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
               f":d={n}:s={W}x{H}:fps={fps},setsar=1[bg]")
    else:
        cmd += ["-loop", "1", "-framerate", str(fps), "-i", str(bg_path)]
        bgf = "[0:v]setsar=1[bg]"
    cmd += ["-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(voice)]
    music = rc.get("music") or ""
    afilter = "[2:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100[a]"
    if music and Path(music).exists():
        cmd += ["-stream_loop", "-1", "-i", music]
        afilter = (f"[2:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100[voice];"
                   f"[3:a]volume={float(rc['music_volume'])},aresample=44100[m];"
                   f"[voice][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    graph = f"{bgf};[1:v]fps={fps},format=rgba,setsar=1[fg];[bg][fg]overlay=0:0,format=yuv420p[v];{afilter}"
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}", "-r", str(fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(rc["crf"]), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart", str(video)]
    run(cmd, timeout=900)
    log.info("rendered %s (%.1fs, %d caption events)", video, total, len(events))
    return {"video": str(video), "cover": str(cover_path), "duration": total,
            "spans": spans, "events": len(events)}
