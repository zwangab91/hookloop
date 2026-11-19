"""Hashtag search providers (Step 2).

TikTok and Instagram have no official public hashtag-search API with view counts, so we use
Apify actors (actor id + input template are configurable, so you can swap scrapers if one
changes). YouTube Shorts uses the official Data API.

Env: APIFY_TOKEN, YOUTUBE_API_KEY
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..models import SourceVideo

log = logging.getLogger("hookloop.sources")

APIFY_RUN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YT_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        cur: Any = d
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, ""):
            return cur
    return default


def norm_tiktok(item: dict[str, Any]) -> SourceVideo | None:
    vid = str(_first(item, "id", "videoId", default=""))
    url = _first(item, "webVideoUrl", "url", "videoUrl", default="")
    if not vid or not url:
        return None
    media = _first(item, "mediaUrls", default=[]) or []
    return SourceVideo(
        platform="tiktok",
        video_id=vid,
        url=url,
        views=int(_first(item, "playCount", "stats.playCount", "viewCount", default=0) or 0),
        likes=int(_first(item, "diggCount", "stats.diggCount", "likes", default=0) or 0),
        caption=_first(item, "text", "desc", "caption", default="") or "",
        author=_first(item, "authorMeta.name", "author.uniqueId", "authorMeta.nickName", default="") or "",
        duration_s=float(_first(item, "videoMeta.duration", "duration", default=0) or 0),
        posted_at=_first(item, "createTimeISO", "createTime", default="") or "",
        media_url=media[0] if isinstance(media, list) and media else "",
    )


def norm_instagram(item: dict[str, Any]) -> SourceVideo | None:
    if str(_first(item, "type", default="Video")).lower() not in ("video", "clips", "reel", "reels"):
        return None
    vid = str(_first(item, "id", "shortCode", default=""))
    url = _first(item, "url", default="") or (f"https://www.instagram.com/reel/{item['shortCode']}/" if item.get("shortCode") else "")
    if not vid or not url:
        return None
    return SourceVideo(
        platform="instagram",
        video_id=vid,
        url=url,
        views=int(_first(item, "videoPlayCount", "videoViewCount", "playCount", "viewCount", default=0) or 0),
        likes=int(_first(item, "likesCount", "likeCount", default=0) or 0),
        caption=_first(item, "caption", "text", default="") or "",
        author=_first(item, "ownerUsername", "owner.username", default="") or "",
        duration_s=float(_first(item, "videoDuration", "duration", default=0) or 0),
        posted_at=_first(item, "timestamp", "takenAt", default="") or "",
        media_url=_first(item, "videoUrl", default="") or "",
    )


def apify_search(platform: str, hashtags: list[str], cfg: dict[str, Any], client: httpx.Client | None = None) -> list[SourceVideo]:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        log.warning("APIFY_TOKEN not set; skipping %s research", platform)
        return []
    spec = cfg["research"]["apify"][platform]
    body = dict(spec.get("input", {}))
    body["hashtags"] = hashtags
    c = client or httpx.Client(timeout=360)
    r = c.post(APIFY_RUN.format(actor=spec["actor"]), params={"token": token, "timeout": 300}, json=body)
    r.raise_for_status()
    items = r.json()
    norm = norm_tiktok if platform == "tiktok" else norm_instagram
    out = [v for v in (norm(i) for i in items if isinstance(i, dict)) if v]
    log.info("%s: %d videos for %s", platform, len(out), hashtags)
    return out


def _iso_duration(s: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0.0
    d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + se)


def youtube_search(hashtags: list[str], cfg: dict[str, Any], client: httpx.Client | None = None) -> list[SourceVideo]:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        log.warning("YOUTUBE_API_KEY not set; skipping YouTube Shorts research")
        return []
    c = client or httpx.Client(timeout=60)
    after = (datetime.now(timezone.utc) - timedelta(days=int(cfg["research"]["max_age_days"]))).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    for tag in hashtags[:3]:  # search.list costs quota; the most specific tags are enough
        r = c.get(YT_SEARCH, params={
            "part": "snippet", "q": f"#{tag} #shorts", "type": "video", "videoDuration": "short",
            "order": "viewCount", "publishedAfter": after, "maxResults": cfg["research"]["youtube"]["max_results"], "key": key,
        })
        r.raise_for_status()
        ids += [i["id"]["videoId"] for i in r.json().get("items", []) if i.get("id", {}).get("videoId")]
    ids = list(dict.fromkeys(ids))
    out: list[SourceVideo] = []
    for i in range(0, len(ids), 50):
        r = c.get(YT_VIDEOS, params={"part": "snippet,statistics,contentDetails", "id": ",".join(ids[i:i + 50]), "key": key})
        r.raise_for_status()
        for v in r.json().get("items", []):
            out.append(SourceVideo(
                platform="youtube",
                video_id=v["id"],
                url=f"https://www.youtube.com/shorts/{v['id']}",
                views=int(v.get("statistics", {}).get("viewCount", 0)),
                likes=int(v.get("statistics", {}).get("likeCount", 0) or 0),
                caption=v["snippet"].get("title", ""),
                author=v["snippet"].get("channelTitle", ""),
                duration_s=_iso_duration(v.get("contentDetails", {}).get("duration", "")),
                posted_at=v["snippet"].get("publishedAt", ""),
            ))
    log.info("youtube: %d shorts for %s", len(out), hashtags[:3])
    return out
