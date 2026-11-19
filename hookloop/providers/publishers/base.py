from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from ...models import PublishResult


@dataclass
class Post:
    video: Path
    title: str  # short title (YouTube)
    caption: str  # caption without hashtags
    hashtags: list[str]
    cover: Path | None = None

    def caption_with_tags(self, limit: int = 2200) -> str:
        tags = " ".join(f"#{h}" for h in self.hashtags)
        return (f"{self.caption}\n\n{tags}".strip())[:limit]


class PublishError(RuntimeError):
    pass


class Publisher:
    name = "base"

    def __init__(self, cfg: dict[str, Any], store, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.store = store
        self.http = client or httpx.Client(timeout=httpx.Timeout(120, connect=20))
        self.sleep = sleep

    def configured(self) -> bool:  # has the credentials it needs
        raise NotImplementedError

    def publish(self, post: Post) -> PublishResult:
        raise NotImplementedError

    def followers(self) -> int | None:
        return None

    def post_metrics(self, remote_ids: list[str]) -> dict[str, dict[str, int]]:
        return {}
