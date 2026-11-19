"""Step 7: track growth, learn which hooks work for *this* account, flag the 10k line.

- Pulls follower counts and per-post views from each publisher.
- Reward per post = did it beat the platform's median views (after `reward_after_hours`)?
  Thompson sampling over hook types feeds back into which reference hooks the writer sees.
- When any platform crosses `monetize_at_followers`, writes a media kit and notifies once.
"""
from __future__ import annotations

import logging
import random
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..store import Store, now_iso

log = logging.getLogger("hookloop.growth")


def _rewards(store: Store, min_age_hours: int = 48) -> list[tuple[str, int]]:
    """(hook_type, 1/0 beat-median) for posts old enough to judge."""
    latest = store.latest_metrics()
    pubs = store.publications()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=min_age_hours)
    by_platform: dict[str, list[tuple[str, int]]] = {}
    for p in pubs:
        try:
            if datetime.fromisoformat(p["published_at"]) > cutoff:
                continue
        except (TypeError, ValueError):
            continue
        m = latest.get((p["platform"], p["remote_id"]))
        if m:
            by_platform.setdefault(p["platform"], []).append((p["hook_type"] or "other", int(m["views"] or 0)))
    out: list[tuple[str, int]] = []
    for rows in by_platform.values():
        if len(rows) < 3:
            continue
        med = statistics.median(v for _, v in rows)
        out += [(ht, int(v > med)) for ht, v in rows]
    return out


def hook_type_weights(store: Store, rng: random.Random | None = None) -> dict[str, float]:
    """Thompson sample P(beat median) per hook type; Beta(1,1) prior."""
    rng = rng or random.Random()
    counts: dict[str, list[int]] = {}
    for ht, r in _rewards(store):
        a = counts.setdefault(ht, [1, 1])
        a[0 if r else 1] += 1
    return {ht: rng.betavariate(a, b) for ht, (a, b) in counts.items()}


def collect(cfg: dict[str, Any], store: Store, publishers: dict[str, Any]) -> dict[str, Any]:
    window = int(cfg["growth"]["metrics_window_days"])
    pubs = store.publications(since_days=window)
    n = 0
    for name, pub in publishers.items():
        mine = [p for p in pubs if p["platform"] == name]
        try:
            if mine:
                for rid, m in pub.post_metrics([p["remote_id"] for p in mine]).items():
                    store.add_metrics(name, rid, m)
                    n += 1
            f = pub.followers()
            if f is not None:
                store.add_followers(name, f)
        except Exception as e:
            log.warning("metrics for %s failed: %s", name, e)
    followers = store.latest_followers()
    log.info("metrics: %d post snapshots; followers=%s", n, followers)
    return {"snapshots": n, "followers": followers}


def check_milestone(cfg: dict[str, Any], store: Store, out_dir: Path) -> Path | None:
    goal = int(cfg["growth"]["monetize_at_followers"])
    followers = store.latest_followers()
    crossed = {p: c for p, c in followers.items() if c >= goal}
    if not crossed or store.kv_get("milestone_10k_notified"):
        return None
    kit = write_media_kit(cfg, store, out_dir)
    store.kv_set("milestone_10k_notified", now_iso())
    return kit


def write_media_kit(cfg: dict[str, Any], store: Store, out_dir: Path) -> Path:
    followers = store.latest_followers()
    latest = store.latest_metrics()
    pubs = store.publications(since_days=30)
    views = [int(latest.get((p["platform"], p["remote_id"]), {}).get("views", 0) or 0) for p in pubs]
    top = sorted(
        ((int(latest.get((p["platform"], p["remote_id"]), {}).get("views", 0) or 0), p) for p in pubs),
        key=lambda x: x[0], reverse=True,
    )[:5]
    lines = [
        f"# {cfg['handle']} — media kit",
        "",
        f"Daily short-form videos about **{cfg['niche']}**: one concrete, copyable trick per video.",
        "",
        "## Audience",
        *[f"- {p.title()}: {c:,} followers" for p, c in sorted(followers.items())],
        f"- Total: {sum(followers.values()):,}",
        "",
        "## Last 30 days",
        f"- Videos published: {len(pubs)}",
        f"- Median views per video: {int(statistics.median(views)) if views else 0:,}",
        "",
        "## Top videos",
        *[f"- {v:,} views — {p['topic']} ({p['platform']}) {p['url']}" for v, p in top],
        "",
        "## Formats",
        "- Integrated 10–15s product segment inside a daily tutorial",
        "- Dedicated 30–45s walkthrough of your product",
        "- Cross-posted to TikTok, Instagram Reels and YouTube Shorts",
        "",
        "Contact: <add your email>",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "media_kit.md"
    path.write_text("\n".join(lines))
    log.info("media kit written: %s", path)
    return path
