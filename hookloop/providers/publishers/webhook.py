"""Generic fan-out: multipart POST (video file + JSON metadata) to an n8n / Make webhook, so you
can add X, LinkedIn, Threads, Facebook, Pinterest, etc. without code changes.

Config: publish.platforms.webhook.url (or HOOKLOOP_WEBHOOK_URL). Optional HOOKLOOP_WEBHOOK_SECRET
is sent as X-Hookloop-Secret.
"""
from __future__ import annotations

import json
import os

from ...models import PublishResult
from .base import Post, PublishError, Publisher


class WebhookPublisher(Publisher):
    name = "webhook"

    def configured(self) -> bool:
        return bool(self.cfg["publish"]["platforms"]["webhook"].get("url"))

    def publish(self, post: Post) -> PublishResult:
        url = self.cfg["publish"]["platforms"]["webhook"]["url"]
        meta = {"title": post.title, "caption": post.caption, "hashtags": post.hashtags,
                "caption_with_tags": post.caption_with_tags()}
        headers = {}
        if os.environ.get("HOOKLOOP_WEBHOOK_SECRET"):
            headers["X-Hookloop-Secret"] = os.environ["HOOKLOOP_WEBHOOK_SECRET"]
        with post.video.open("rb") as f:
            r = self.http.post(url, data={"meta": json.dumps(meta)}, files={"video": (post.video.name, f, "video/mp4")},
                               headers=headers, timeout=600)
        if r.status_code >= 300:
            raise PublishError(f"webhook {r.status_code}: {r.text[:200]}")
        return PublishResult(platform=self.name, ok=True, remote_id="", status=str(r.status_code))
