from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Topic:
    name: str
    hashtags: list[str]
    why_now: str = ""
    facts: list[dict[str, str]] = field(default_factory=list)  # [{fact, source_url}]
    examples: list["SourceVideo"] = field(default_factory=list)  # 1M+ videos behind a trend-picked topic
    method: str = "rotation"  # trend | web_search | rotation


@dataclass
class SourceVideo:
    platform: str
    video_id: str
    url: str
    views: int
    caption: str = ""
    author: str = ""
    likes: int = 0
    duration_s: float = 0.0
    posted_at: str = ""
    media_url: str = ""  # direct mp4 if the scraper returned one

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.video_id}"


@dataclass
class Hook:
    source_key: str
    platform: str
    views: int
    opening_line: str
    hook_type: str
    template: str
    structure: list[dict[str, Any]] = field(default_factory=list)
    visual_pattern: str = ""
    on_screen_text: str = ""
    why_it_works: str = ""
    score: float = 0.0
    topic: str = ""
    id: int | None = None


@dataclass
class Segment:
    voiceover: str
    on_screen: str = ""
    style: str = "point"  # hook | point | code | cta
    code: str = ""


@dataclass
class Script:
    title: str
    hook_overlay: str
    segments: list[Segment]
    caption: str
    hashtags: list[str]
    hook_type: str = ""
    hook_id: int | None = None
    topic: str = ""

    @property
    def spoken_text(self) -> str:
        return " ".join(s.voiceover.strip() for s in self.segments)

    @property
    def word_count(self) -> int:
        return len(self.spoken_text.split())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Script":
        segs = [Segment(**{k: v for k, v in s.items() if k in Segment.__dataclass_fields__}) for s in d["segments"]]
        return cls(
            title=d.get("title", ""),
            hook_overlay=d.get("hook_overlay", ""),
            segments=segs,
            caption=d.get("caption", ""),
            hashtags=[h.lstrip("#") for h in d.get("hashtags", [])],
            hook_type=d.get("hook_type", ""),
            hook_id=d.get("hook_id"),
            topic=d.get("topic", ""),
        )


@dataclass
class PublishResult:
    platform: str
    ok: bool
    remote_id: str = ""
    url: str = ""
    status: str = ""
    warning: str = ""
    error: str = ""
