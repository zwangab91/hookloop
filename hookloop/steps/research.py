"""Step 2 (search the topic's hashtags) + Step 3 (keep the 1M+ view winners)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..models import SourceVideo, Topic
from ..providers import sources
from ..store import Store

log = logging.getLogger("hookloop.research")


def _parse_time(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if s.isdigit():
            return datetime.fromtimestamp(int(s), tz=timezone.utc)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def select_winners(videos: list[SourceVideo], cfg: dict[str, Any]) -> list[SourceVideo]:
    rc = cfg["research"]
    min_views = int(rc["min_views"])
    max_dur = float(rc["max_duration_s"])
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(rc["max_age_days"]))

    seen: dict[str, SourceVideo] = {}
    for v in videos:
        if v.key in seen and seen[v.key].views >= v.views:
            continue
        seen[v.key] = v
    pool = list(seen.values())

    def ok(v: SourceVideo) -> bool:
        if v.duration_s and v.duration_s > max_dur:
            return False
        t = _parse_time(v.posted_at)
        return t is None or t >= cutoff

    pool = sorted([v for v in pool if ok(v)], key=lambda v: v.views, reverse=True)
    winners = [v for v in pool if v.views >= min_views]
    if not winners:
        n = int(rc["fallback_top_n"])
        log.warning("no video cleared %s views; learning from top %d instead", f"{min_views:,}", n)
        winners = pool[:n]
    return winners


def research(cfg: dict[str, Any], store: Store, topic: Topic, *, fixture: str | Path | None = None) -> list[SourceVideo]:
    videos: list[SourceVideo] = []
    if fixture:
        raw = json.loads(Path(fixture).read_text())
        videos = [SourceVideo(**v) for v in raw]
    else:
        plats = cfg["research"]["platforms"]
        for p in ("tiktok", "instagram"):
            if p in plats:
                try:
                    videos += sources.apify_search(p, topic.hashtags, cfg)
                except Exception as e:
                    log.warning("%s research failed: %s", p, e)
        if "youtube" in plats:
            try:
                videos += sources.youtube_search(topic.hashtags, cfg)
            except Exception as e:
                log.warning("youtube research failed: %s", e)
    winners = select_winners(videos, cfg)
    store.upsert_sources(winners, topic.name)
    log.info("research: %d candidates -> %d winners (top views %s)", len(videos), len(winners),
             f"{winners[0].views:,}" if winners else "n/a")
    return winners
