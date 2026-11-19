"""Step 1 (pick a hot topic) + Step 6 (rotate the topic every day).

Candidates = configured pool minus anything used inside `rotation_days`. Claude (with web
search when enabled) picks today's hottest candidate -- or a clearly trending new subtopic
inside the niche -- and returns hashtags to research plus a few current, sourced facts the
script is allowed to use. Rotation is enforced in code, not left to the model.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..models import Topic
from ..store import Store

log = logging.getLogger("hookloop.topic")

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
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"fact": {"type": "string"}, "source_url": {"type": "string"}},
                "required": ["fact", "source_url"],
            },
            "description": "3-6 current, verifiable facts a 45-second how-to video could use.",
        },
    },
    "required": ["topic", "hashtags", "why_now", "facts"],
}


def default_hashtags(topic: str, niche: str) -> list[str]:
    tag = re.sub(r"[^a-z0-9]", "", topic.lower())
    base = re.sub(r"[^a-z0-9]", "", niche.lower())
    tags = [tag, base, f"{base}ai", "aitools"]
    out: list[str] = []
    for t in tags:
        if t and t not in out:
            out.append(t)
    return out


def candidates(cfg: dict[str, Any], store: Store) -> list[str]:
    pool = list(dict.fromkeys(cfg["topics"]["pool"]))
    recent = set(store.recent_topics(int(cfg["topics"]["rotation_days"])))
    fresh = [t for t in pool if t not in recent]
    if fresh:
        return fresh
    # everything was used recently: fall back to least-recently-used first
    last = store.topic_last_used()
    return sorted(pool, key=lambda t: last.get(t, ""))


def pick_topic(cfg: dict[str, Any], store: Store, llm, *, record: bool = True) -> Topic:
    niche = cfg["niche"]
    cands = candidates(cfg, store)
    recent = store.recent_topics(int(cfg["topics"]["rotation_days"]))
    topic: Topic | None = None

    if llm is not None:
        prompt = (
            f"Niche: {niche}\n"
            f"Candidate topics (not used in the last {cfg['topics']['rotation_days']} days): {cands}\n"
            f"Used recently (do NOT pick): {recent}\n\n"
            "Pick ONE topic for today's 45-second short-form video. Prefer whatever in this niche is getting the most "
            "attention on TikTok/Instagram/YouTube Shorts right now (new launches, features people are discovering, "
            "viral use cases). You may propose a new subtopic inside the niche if it is clearly trending and not in the "
            "recent list. Then give hashtags to search and 3-6 current facts with source URLs that make the video "
            "accurate and specific. Only include facts you verified."
        )
        try:
            out = llm.structured(
                system="You are the research lead for a daily short-form video channel. You are precise about facts.",
                content=prompt,
                tool_name="submit_topic",
                schema=SCHEMA,
                web_search=bool(cfg["topics"]["discover_trending"]),
            )
            name = out["topic"].strip()
            if name in recent:
                log.warning("model picked a recently used topic (%s); enforcing rotation", name)
            else:
                tags = [h.lstrip("#").lower().replace(" ", "") for h in out.get("hashtags", []) if h.strip()]
                topic = Topic(name=name, hashtags=tags or default_hashtags(name, niche),
                              why_now=out.get("why_now", ""), facts=out.get("facts", []))
        except Exception as e:  # never let topic discovery kill the daily run
            log.warning("topic discovery failed (%s); falling back to rotation", e)

    if topic is None:
        name = cands[0]
        topic = Topic(name=name, hashtags=default_hashtags(name, niche), why_now="next in rotation")

    if record:
        store.record_topic(topic.name, topic.hashtags, topic.why_now)
    log.info("topic: %s  hashtags=%s", topic.name, topic.hashtags)
    return topic
