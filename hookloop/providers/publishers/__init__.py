from __future__ import annotations

from typing import Any

from .base import Post, PublishError, Publisher
from .instagram import InstagramPublisher
from .tiktok import TikTokPublisher
from .webhook import WebhookPublisher
from .youtube import YouTubePublisher

REGISTRY = {
    "tiktok": TikTokPublisher,
    "instagram": InstagramPublisher,
    "youtube": YouTubePublisher,
    "webhook": WebhookPublisher,
}


def make_publishers(cfg: dict[str, Any], store, *, only_configured: bool = True, client=None) -> dict[str, Publisher]:
    out: dict[str, Publisher] = {}
    for name, c in cfg["publish"]["platforms"].items():
        if not c.get("enabled") or name not in REGISTRY:
            continue
        p = REGISTRY[name](cfg, store, client=client)
        if only_configured and not p.configured():
            continue
        out[name] = p
    return out


__all__ = ["Post", "PublishError", "Publisher", "make_publishers", "REGISTRY"]
