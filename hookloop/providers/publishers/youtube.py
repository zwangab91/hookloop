"""YouTube Shorts via the Data API v3 resumable upload (vertical + <= 3 min = Short).

Env: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN
Scopes: youtube.upload, youtube.readonly. Uploads from an unaudited Google Cloud project are
locked to private until the project passes the YouTube API compliance audit.
"""
from __future__ import annotations

import logging
import os

from ...models import PublishResult
from .base import Post, PublishError, Publisher

log = logging.getLogger("hookloop.youtube")
TOKEN = "https://oauth2.googleapis.com/token"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"


class YouTubePublisher(Publisher):
    name = "youtube"
    _tok: str | None = None

    def configured(self) -> bool:
        return all(os.environ.get(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))

    def token(self) -> str:
        if self._tok:
            return self._tok
        r = self.http.post(TOKEN, data={
            "client_id": os.environ["YT_CLIENT_ID"], "client_secret": os.environ["YT_CLIENT_SECRET"],
            "refresh_token": os.environ["YT_REFRESH_TOKEN"], "grant_type": "refresh_token"})
        if r.status_code != 200:
            raise PublishError(f"youtube token refresh failed: {r.text[:300]}")
        self._tok = r.json()["access_token"]
        return self._tok

    def publish(self, post: Post) -> PublishResult:
        c = self.cfg["publish"]["platforms"]["youtube"]
        title = post.title.strip()
        if "#shorts" not in title.lower() and len(title) <= 90:
            title = f"{title} #shorts"
        meta = {
            "snippet": {"title": title[:100], "description": post.caption_with_tags(4900),
                        "tags": post.hashtags[:15], "categoryId": str(c.get("category_id", "28"))},
            "status": {"privacyStatus": c.get("privacy", "public"), "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": bool(c.get("contains_synthetic_media", True))},
        }
        size = post.video.stat().st_size
        r = self.http.post(UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"}, json=meta, headers={
            "Authorization": f"Bearer {self.token()}", "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(size)})
        if r.status_code != 200 or "location" not in r.headers:
            raise PublishError(f"youtube upload init failed: {r.status_code} {r.text[:300]}")
        up = self.http.put(r.headers["location"], content=post.video.read_bytes(),
                           headers={"Content-Type": "video/mp4"}, timeout=900)
        if up.status_code not in (200, 201):
            raise PublishError(f"youtube upload failed: {up.status_code} {up.text[:300]}")
        v = up.json()
        privacy = v.get("status", {}).get("privacyStatus", "")
        warning = ""
        if c.get("privacy", "public") == "public" and privacy == "private":
            warning = "YouTube kept the upload private (unaudited API project). Submit the YouTube API audit form."
        return PublishResult(platform=self.name, ok=True, remote_id=v["id"], url=f"https://youtube.com/shorts/{v['id']}",
                             status=v.get("status", {}).get("uploadStatus", ""), warning=warning)

    def followers(self) -> int | None:
        r = self.http.get(f"{API}/channels", params={"part": "statistics", "mine": "true"},
                          headers={"Authorization": f"Bearer {self.token()}"})
        items = r.json().get("items", [])
        return int(items[0]["statistics"].get("subscriberCount", 0)) if items else None

    def post_metrics(self, remote_ids: list[str]) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for i in range(0, len(remote_ids), 50):
            r = self.http.get(f"{API}/videos", params={"part": "statistics", "id": ",".join(remote_ids[i:i + 50])},
                              headers={"Authorization": f"Bearer {self.token()}"})
            for v in r.json().get("items", []):
                s = v.get("statistics", {})
                out[v["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                "comments": int(s.get("commentCount", 0)), "shares": 0}
        return out
