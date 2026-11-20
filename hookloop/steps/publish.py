"""Step 6: publish one rendered post to every configured surface (idempotent per platform)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..models import PublishResult, Script
from ..providers.publishers import Post, Publisher
from ..store import Store

log = logging.getLogger("hookloop.publish")


def build_post(row: dict[str, Any], platform: str | None = None) -> Post:
    """Use output/<id>/video_<platform>.mp4 when that platform got its own copy (different handle/button)."""
    s = Script.from_dict(json.loads(row["script"]))
    video = Path(row["video_path"])
    cover = video.parent / "cover.jpg"
    if platform and (video.parent / f"video_{platform}.mp4").exists():
        video = video.parent / f"video_{platform}.mp4"
        cover = video.parent / f"cover_{platform}.jpg"
    return Post(video=video, title=s.title, caption=s.caption, hashtags=s.hashtags, cover=cover if cover.exists() else None)


def publish_post(store: Store, post_id: str, publishers: dict[str, Publisher], *, dry_run: bool = False) -> list[PublishResult]:
    row = store.get_post(post_id)
    if not row:
        raise KeyError(f"no post {post_id}")
    post = build_post(row)
    if not post.video.exists():
        raise FileNotFoundError(post.video)
    done = {p["platform"] for p in store.publications() if p["post_id"] == post_id}
    results: list[PublishResult] = []
    for name, pub in publishers.items():
        if name in done:
            log.info("%s: already published, skipping", name)
            continue
        post = build_post(row, name)
        if dry_run:
            log.info("[dry-run] would publish to %s: %r", name, post.caption_with_tags()[:120])
            results.append(PublishResult(platform=name, ok=True, status="DRY_RUN"))
            continue
        try:
            res = pub.publish(post)
            log.info("%s: published %s %s %s", name, res.remote_id, res.url, res.warning)
        except Exception as e:
            log.error("%s: publish failed: %s", name, e)
            res = PublishResult(platform=name, ok=False, error=str(e)[:500])
        store.record_publication(post_id, res)
        results.append(res)
    if not dry_run:
        ok = any(r.ok for r in results) or bool(done)
        store.set_post_status(post_id, "published" if ok else "failed")
    return results
