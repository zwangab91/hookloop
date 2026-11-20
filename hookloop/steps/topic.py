"""Step 1 (pick a hot topic) + Step 6 (rotate the topic every day).

Trend-first (topics.mode = auto | trend), when view-count data is available:
  scan broad hashtags for videos posted in the last `trend_window_days` with 1M+ views
  -> Claude groups them into themes inside the area and ranks them
  -> pick the strongest theme not used inside `rotation_days`
  -> Claude web-searches a few current, sourced facts for it.
  The 1M+ videos behind the theme are kept as `topic.examples` and feed the hook library.

Fallback (or topics.mode = niche): Claude web-searches what is hot inside the area right now and
picks from the pool or proposes a trending subtopic; with no Claude at all, plain rotation.
Rotation is enforced in code, never left to the model.
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any, Callable

from ..models import SourceVideo, Topic
from ..providers import sources
from ..store import Store
from .research import select_winners

log = logging.getLogger("hookloop.topic")

FACT_ITEMS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"fact": {"type": "string"}, "source_url": {"type": "string"}},
        "required": ["fact", "source_url"],
    },
    "description": "3-6 current, verifiable facts a 45-second video could use.",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "description": "Today's topic, short (2-4 words)."},
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-6 hashtags to search on TikTok/Instagram/YouTube, lowercase, no '#', most specific first.",
        },
        "why_now": {"type": "string", "description": "One sentence: why this is hot today."},
        "facts": FACT_ITEMS,
    },
    "required": ["topic", "hashtags", "why_now", "facts"],
}

THEMES_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "description": "Themes inside the area, strongest first.",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Specific, 2-5 words (e.g. 'Claude Code hooks', not 'AI')."},
                    "hashtags": {"type": "array", "items": {"type": "string"},
                                 "description": "3-6 hashtags to research it, lowercase, no '#', most specific first."},
                    "why_now": {"type": "string", "description": "One sentence citing the evidence (how many 1M+ videos, how recent)."},
                    "video_keys": {"type": "array", "items": {"type": "string"},
                                   "description": "Keys of the listed videos that belong to this theme."},
                },
                "required": ["topic", "hashtags", "why_now", "video_keys"],
            },
        }
    },
    "required": ["themes"],
}

FACTS_SCHEMA = {"type": "object", "properties": {"facts": FACT_ITEMS}, "required": ["facts"]}


def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for h in tags:
        t = re.sub(r"[^a-z0-9_]", "", h.lstrip("#").lower())
        if t and t not in out:
            out.append(t)
    return out


def default_hashtags(topic: str, cfg: dict[str, Any]) -> list[str]:
    return _clean_tags([topic.replace(" ", "")] + list(cfg["topics"].get("niche_hashtags", [])))


def candidates(cfg: dict[str, Any], store: Store) -> list[str]:
    pool = list(dict.fromkeys(cfg["topics"]["pool"]))
    recent = set(store.recent_topics(int(cfg["topics"]["rotation_days"])))
    fresh = [t for t in pool if t not in recent]
    if fresh:
        return fresh
    # everything was used recently: fall back to least-recently-used first
    last = store.topic_last_used()
    return sorted(pool, key=lambda t: last.get(t, ""))


# ------------------------------------------------------------------ trend-first
def scan_trends(cfg: dict[str, Any]) -> list[SourceVideo]:
    """Recent 1M+ videos across the area's broad hashtags (only sources with keys are queried)."""
    tc = cfg["topics"]
    c = copy.deepcopy(cfg)
    c["research"]["max_age_days"] = int(tc["trend_window_days"])
    tags = _clean_tags(tc["scan_hashtags"])
    vids: list[SourceVideo] = []
    plats = cfg["research"]["platforms"]
    for p in ("tiktok", "instagram"):
        if p in plats:
            try:
                vids += sources.apify_search(p, tags, c)
            except Exception as e:
                log.warning("trend scan on %s failed: %s", p, e)
    if "youtube" in plats:
        try:
            vids += sources.youtube_search(tags, c, max_tags=len(tags))
        except Exception as e:
            log.warning("trend scan on youtube failed: %s", e)
    min_views = int(cfg["research"]["min_views"])
    return [v for v in select_winners(vids, c) if v.views >= min_views]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def pick_trend_topic(cfg: dict[str, Any], llm, winners: list[SourceVideo], recent: list[str]) -> Topic | None:
    listing = "\n".join(
        f"[{v.key}] {v.views:,} views | {v.platform} | {(v.posted_at or '')[:10]} | {v.caption[:160].replace(chr(10), ' ')}"
        for v in sorted(winners, key=lambda v: v.views, reverse=True)[:60]
    )
    out = llm.structured(
        system="You are the research lead for a daily short-form video channel. You find the themes behind viral videos.",
        content=(
            f"Area: {cfg['niche']}\n"
            f"These videos were posted in the last {cfg['topics']['trend_window_days']} days and each has 1M+ views:\n"
            f"{listing}\n\n"
            f"Used recently (do NOT return these): {recent}\n\n"
            "Group the videos into specific themes INSIDE the area (ignore off-area videos). Rank the themes by "
            "strength: more 1M+ videos, more views and more recent is stronger. Each theme must be teachable in a "
            "45-second video by an ML engineer: one concrete, copyable idea. Return up to 5."
        ),
        tool_name="submit_themes",
        schema=THEMES_SCHEMA,
    )
    by_key = {v.key: v for v in winners}
    blocked = {_norm(t) for t in recent}
    for th in out.get("themes", []):
        name = th.get("topic", "").strip()
        examples = [by_key[k] for k in th.get("video_keys", []) if k in by_key]
        if not name or _norm(name) in blocked or not examples:
            continue
        return Topic(name=name, hashtags=_clean_tags(th.get("hashtags", [])) or default_hashtags(name, cfg),
                     why_now=th.get("why_now", ""), examples=examples, method="trend")
    return None


def facts_for(cfg: dict[str, Any], llm, topic: Topic) -> list[dict[str, str]]:
    try:
        out = llm.structured(
            system="You are a fact-checker for a short-form video channel. Only report facts you verified.",
            content=(f"Topic: {topic.name} (area: {cfg['niche']})\nWhy it is trending: {topic.why_now}\n\n"
                     "Find 3-6 current, verifiable facts with source URLs that make a 45-second video on this "
                     "topic accurate and specific."),
            tool_name="submit_facts",
            schema=FACTS_SCHEMA,
            web_search=True,
        )
        return out.get("facts", [])
    except Exception as e:
        log.warning("fact research for %s failed: %s", topic.name, e)
        return []


# ------------------------------------------------------------------ fallback
def pick_niche_topic(cfg: dict[str, Any], llm, cands: list[str], recent: list[str]) -> Topic | None:
    prompt = (
        f"Area: {cfg['niche']}\n"
        f"Candidate topics (not used in the last {cfg['topics']['rotation_days']} days): {cands}\n"
        f"Used recently (do NOT pick): {recent}\n\n"
        "Pick ONE topic for today's 45-second short-form video. Prefer whatever in this area is getting the most "
        "attention on TikTok/Instagram/YouTube Shorts right now (new launches, features people are discovering, "
        "viral use cases). You may propose a new subtopic inside the area if it is clearly trending and not in the "
        "recent list. Then give hashtags to search and 3-6 current facts with source URLs that make the video "
        "accurate and specific. Only include facts you verified."
    )
    out = llm.structured(
        system="You are the research lead for a daily short-form video channel. You are precise about facts.",
        content=prompt,
        tool_name="submit_topic",
        schema=SCHEMA,
        web_search=bool(cfg["topics"]["discover_trending"]),
    )
    name = out["topic"].strip()
    if _norm(name) in {_norm(t) for t in recent}:
        log.warning("model picked a recently used topic (%s); enforcing rotation", name)
        return None
    return Topic(name=name, hashtags=_clean_tags(out.get("hashtags", [])) or default_hashtags(name, cfg),
                 why_now=out.get("why_now", ""), facts=out.get("facts", []), method="web_search")


def pick_topic(cfg: dict[str, Any], store: Store, llm, *, record: bool = True, offline: bool = False,
               scan: Callable[[dict[str, Any]], list[SourceVideo]] | None = None) -> Topic:
    tc = cfg["topics"]
    recent = store.recent_topics(int(tc["rotation_days"]))
    topic: Topic | None = None

    if llm is not None and not offline and tc.get("mode", "auto") in ("auto", "trend"):
        try:
            winners = (scan or scan_trends)(cfg)
            if len(winners) >= int(tc["trend_min_videos"]):
                topic = pick_trend_topic(cfg, llm, winners, recent)
                if topic:
                    topic.facts = facts_for(cfg, llm, topic)
                    log.info("trend scan: %d recent 1M+ videos -> '%s' (%d examples)",
                             len(winners), topic.name, len(topic.examples))
            else:
                log.info("trend scan found %d recent 1M+ videos (< %s); using web-search pick",
                         len(winners), tc["trend_min_videos"])
        except Exception as e:  # never let topic discovery kill the daily run
            log.warning("trend scan failed (%s); using web-search pick", e)

    if topic is None and llm is not None:
        try:
            topic = pick_niche_topic(cfg, llm, candidates(cfg, store), recent)
        except Exception as e:
            log.warning("topic discovery failed (%s); falling back to rotation", e)

    if topic is None:
        name = candidates(cfg, store)[0]
        topic = Topic(name=name, hashtags=default_hashtags(name, cfg), why_now="next in rotation", method="rotation")

    if record:
        store.record_topic(topic.name, topic.hashtags, topic.why_now)
    log.info("topic (%s): %s  hashtags=%s", topic.method, topic.name, topic.hashtags)
    return topic
