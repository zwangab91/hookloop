"""Step 5a: write an original <=45s script on a proven hook pattern.

write -> deterministic checks -> Claude critique/revise (up to N rounds) -> trim as last resort.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any

from ..models import Hook, Script, Topic
from ..store import Store
from .growth import hook_type_weights
from .hooks import HOOK_TYPES

log = logging.getLogger("hookloop.script")

SEGMENT = {
    "type": "object",
    "properties": {
        "voiceover": {"type": "string", "description": "Exactly what is spoken. Short sentences."},
        "on_screen": {"type": "string", "description": "Big on-screen headline for this beat, <= 7 words."},
        "style": {"type": "string", "enum": ["hook", "point", "code", "cta"]},
        "code": {"type": "string", "description": "Only for style=code: <= 8 lines, <= 34 chars per line. Else empty."},
    },
    "required": ["voiceover", "on_screen", "style"],
}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "reference_hook_id": {"type": "integer", "description": "id of the reference pattern you adapted (0 if none)."},
        "hook_type": {"type": "string", "enum": HOOK_TYPES},
        "title": {"type": "string", "description": "YouTube Shorts title, <= 70 chars, no hashtags."},
        "hook_overlay": {"type": "string", "description": "On-screen hook text shown from frame 1, <= 6 words, punchy."},
        "segments": {"type": "array", "items": SEGMENT, "minItems": 3, "maxItems": 8},
        "caption": {"type": "string", "description": "Post caption, <= 150 chars, no hashtags."},
        "hashtags": {"type": "array", "items": {"type": "string"}, "description": "3-5 hashtags, no '#'."},
    },
    "required": ["reference_hook_id", "hook_type", "title", "hook_overlay", "segments", "caption", "hashtags"],
}

CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "revised": SCRIPT_SCHEMA,
    },
    "required": ["pass", "issues"],
}


def word_budget(cfg: dict[str, Any]) -> int:
    sc = cfg["script"]
    # leave ~2.5s of headroom for pauses between beats
    return int((float(sc["max_seconds"]) - 2.5) * float(sc["words_per_second"]))


def _trigrams(s: str) -> set[tuple[str, ...]]:
    w = re.findall(r"[a-z0-9']+", s.lower())
    return {tuple(w[i:i + 3]) for i in range(len(w) - 2)}


def overlap(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def validate(s: Script, cfg: dict[str, Any], refs: list[Hook]) -> list[str]:
    issues: list[str] = []
    budget = word_budget(cfg)
    if s.word_count > budget:
        issues.append(f"too long: {s.word_count} words > budget {budget} (must fit {cfg['script']['max_seconds']}s)")
    if not 3 <= len(s.segments) <= 8:
        issues.append(f"need 3-8 segments, got {len(s.segments)}")
    if s.segments:
        first, last = s.segments[0], s.segments[-1]
        if first.style != "hook":
            issues.append("first segment must be style=hook")
        if len(first.voiceover.split()) > 14:
            issues.append("hook voiceover must be <= 14 words so it lands in ~3 seconds")
        if last.style != "cta":
            issues.append("last segment must be style=cta")
    if len(s.hook_overlay.split()) > 6:
        issues.append("hook_overlay must be <= 6 words")
    for i, seg in enumerate(s.segments):
        if len(seg.on_screen.split()) > 8:
            issues.append(f"segment {i}: on_screen > 8 words")
        if seg.style == "code":
            lines = seg.code.splitlines()
            if not lines:
                issues.append(f"segment {i}: style=code but no code")
            if len(lines) > 9 or any(len(line) > 38 for line in lines):
                issues.append(f"segment {i}: code must be <= 9 lines of <= 38 chars")
    for h in refs:
        for text in (h.opening_line, h.on_screen_text):
            if text and overlap(s.spoken_text + " " + s.hook_overlay, text) > 0.5:
                issues.append(f"too close to source wording ({h.source_key}); adapt the structure, not the words")
    return issues


def trim_to_budget(s: Script, cfg: dict[str, Any]) -> Script:
    budget = word_budget(cfg)
    while s.word_count > budget and len(s.segments) > 3:
        middle = [i for i, seg in enumerate(s.segments) if seg.style not in ("hook", "cta")]
        if len(middle) <= 1:
            break
        drop = max(middle, key=lambda i: len(s.segments[i].voiceover.split()))
        s.segments.pop(drop)
    return s


def rank_references(store: Store, topic: Topic, k: int = 4) -> list[Hook]:
    hooks = store.hooks(limit=300)
    if not hooks:
        return []
    weights = hook_type_weights(store)
    max_lv = max(math.log10(max(h.views, 10)) for h in hooks)

    def score(h: Hook) -> float:
        return (0.45 * math.log10(max(h.views, 10)) / max_lv + 0.30 * (h.score / 10)
                + 0.25 * weights.get(h.hook_type, 0.5) + (0.1 if h.topic == topic.name else 0))

    ranked = sorted(hooks, key=score, reverse=True)
    out: list[Hook] = []
    for h in ranked:  # prefer distinct hook types so the writer has real options
        if all(o.hook_type != h.hook_type for o in out):
            out.append(h)
        if len(out) == k:
            return out
    for h in ranked:
        if h not in out:
            out.append(h)
        if len(out) == k:
            break
    return out


def _brief(cfg: dict[str, Any], topic: Topic, refs: list[Hook]) -> str:
    facts = "\n".join(f"- {f['fact']} (source: {f.get('source_url', '')})" for f in topic.facts) or "- (no fresh facts; stick to well-established basics)"
    ref_txt = "\n".join(
        f"[id={h.id}] type={h.hook_type} views={h.views:,} platform={h.platform}\n"
        f"  template: {h.template}\n  beats: {h.structure}\n  visual: {h.visual_pattern}\n  why: {h.why_it_works}"
        for h in refs
    ) or "(library empty - use a proven mistake_callout or result_first hook)"
    sc = cfg["script"]
    return f"""Write today's short-form video.

NICHE: {cfg['niche']}
TOPIC: {topic.name}
WHY NOW: {topic.why_now}
ALLOWED FACTS (do not state other specifics, numbers or claims):
{facts}

PERSONA: {sc['persona']}

PROVEN HOOK PATTERNS from 1M+ view videos (adapt ONE pattern's structure; never reuse their wording):
{ref_txt}

RULES
- Hard limit: {word_budget(cfg)} spoken words total (the video must run <= {sc['max_seconds']} seconds).
- Segment 1 (style=hook): <= 12 words, delivers the hook immediately. No greeting, no "in this video".
- Teach ONE concrete, copyable thing. Each middle beat earns the next second (payoff, step, proof).
- Use style=code when showing a prompt/file/command makes it copyable (<= 8 lines, <= 34 chars per line).
- Last segment (style=cta) voiceover: "{sc['cta']}"
- on_screen text <= 7 words and must add something, not just repeat the voiceover.
- Write for the ear: short sentences, contractions, numbers as words where natural.
"""


def write_script(cfg: dict[str, Any], store: Store, llm, topic: Topic) -> Script:
    refs = rank_references(store, topic)
    brief = _brief(cfg, topic, refs)
    out = llm.structured(
        system="You write viral, accurate, original short-form scripts. You obey word limits exactly.",
        content=brief, tool_name="submit_script", schema=SCRIPT_SCHEMA,
    )
    script = _to_script(out, topic)

    for round_ in range(int(cfg["script"]["max_revisions"])):
        issues = validate(script, cfg, refs)
        crit = llm.structured(
            system="You are a ruthless short-form editor. You check hook strength, accuracy against the allowed facts, "
                   "originality vs. the reference patterns, pacing, and the word limit.",
            content=f"{brief}\n\nDRAFT SCRIPT:\n{script.to_dict()}\n\nAutomated checks found: {issues or 'none'}\n\n"
                    "If the draft is publish-ready and the automated checks are clean, return pass=true. Otherwise return "
                    "pass=false, list the issues, and include a fully revised script that fixes them.",
            tool_name="submit_review", schema=CRITIC_SCHEMA, fast=True,
        )
        if crit.get("pass") and not issues:
            log.info("script approved by critic after %d round(s)", round_ + 1)
            break
        if crit.get("revised"):
            script = _to_script(crit["revised"], topic)
            log.info("script revised (round %d): %s", round_ + 1, "; ".join(crit.get("issues", []) + issues)[:300])

    script = trim_to_budget(script, cfg)
    remaining = validate(script, cfg, refs)
    if any(i.startswith("too close to source") for i in remaining):
        raise RuntimeError("script still too close to a source video after revisions; refusing to publish it")
    if remaining:
        log.warning("script ships with minor issues: %s", remaining)
    log.info("script: %d words, hook_type=%s, '%s'", script.word_count, script.hook_type, script.segments[0].voiceover)
    return script


def _to_script(d: dict[str, Any], topic: Topic) -> Script:
    d = dict(d)
    d["hook_id"] = d.pop("reference_hook_id", None) or None
    d["topic"] = topic.name
    return Script.from_dict(d)
