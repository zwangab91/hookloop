"""TikTok Content Posting API.

method=direct  -> /v2/post/publish/video/init/  (scope video.publish). Until your app passes
                  TikTok's audit, posts are forced to SELF_ONLY (private); we detect that and say so.
method=inbox   -> /v2/post/publish/inbox/video/init/ (scope video.upload). Lands in your TikTok
                  inbox/drafts; you tap Post. Not subject to the Direct Post audit.

Env: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN (rotated value is persisted
in the state DB). Scopes: video.publish and/or video.upload, user.info.basic, user.info.stats, video.list
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from ...models import PublishResult
from .base import Post, PublishError, Publisher

log = logging.getLogger("hookloop.tiktok")
API = "https://open.tiktokapis.com"
MB = 1024 * 1024


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok rules: chunks 5-64MB, last chunk absorbs the rest."""
    if size <= 64 * MB:
        return size, 1
    chunk = 10 * MB
    return chunk, size // chunk


class TikTokPublisher(Publisher):
    name = "tiktok"

    def configured(self) -> bool:
        return bool(os.environ.get("TIKTOK_CLIENT_KEY") and os.environ.get("TIKTOK_CLIENT_SECRET")
                    and (self.store.secret_get("tiktok_refresh_token") or os.environ.get("TIKTOK_REFRESH_TOKEN")))

    # ---- auth ----
    def token(self) -> str:
        cached = self.store.secret_get("tiktok_access_token")
        exp = self.store.kv_get("tiktok_access_expires")
        if cached and exp and datetime.fromisoformat(exp) > datetime.now(timezone.utc) + timedelta(minutes=10):
            return cached
        refresh = self.store.secret_get("tiktok_refresh_token") or os.environ["TIKTOK_REFRESH_TOKEN"]
        r = self.http.post(f"{API}/v2/oauth/token/", data={
            "client_key": os.environ["TIKTOK_CLIENT_KEY"], "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
            "grant_type": "refresh_token", "refresh_token": refresh,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        j = r.json()
        if r.status_code != 200 or "access_token" not in j:
            raise PublishError(f"tiktok token refresh failed: {j}")
        self.store.secret_set("tiktok_access_token", j["access_token"])
        self.store.kv_set("tiktok_access_expires",
                          (datetime.now(timezone.utc) + timedelta(seconds=int(j.get("expires_in", 86400)))).isoformat())
        if j.get("refresh_token"):
            self.store.secret_set("tiktok_refresh_token", j["refresh_token"])
        return j["access_token"]

    def _post(self, path: str, body: dict, params: dict | None = None) -> dict:
        r = self.http.post(f"{API}{path}", params=params, json=body, headers={
            "Authorization": f"Bearer {self.token()}", "Content-Type": "application/json; charset=UTF-8"})
        j = r.json()
        code = j.get("error", {}).get("code", "ok")
        if code != "ok":
            raise PublishError(f"{code}: {j.get('error', {}).get('message', '')}")
        return j.get("data", {})

    # ---- publish ----
    def publish(self, post: Post) -> PublishResult:
        c = self.cfg["publish"]["platforms"]["tiktok"]
        size = post.video.stat().st_size
        chunk, count = chunk_plan(size)
        source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
        warning = ""
        if c.get("method", "direct") == "inbox":
            data = self._post("/v2/post/publish/inbox/video/init/", {"source_info": source})
        else:
            info = self._post("/v2/post/publish/creator_info/query/", {})
            options = info.get("privacy_level_options") or []
            privacy = c.get("privacy", "PUBLIC_TO_EVERYONE")
            if options and privacy not in options:
                warning = f"privacy {privacy} not offered ({options}); using {options[0]}"
                privacy = options[0]
            post_info = {
                "title": post.caption_with_tags(2200), "privacy_level": privacy,
                "disable_comment": bool(c.get("disable_comment")), "disable_duet": bool(c.get("disable_duet")),
                "disable_stitch": bool(c.get("disable_stitch")), "video_cover_timestamp_ms": 500,
                **(c.get("post_info_extra") or {}),
            }
            try:
                data = self._post("/v2/post/publish/video/init/", {"post_info": post_info, "source_info": source})
            except PublishError as e:
                if "unaudited_client_can_only_post_to_private_accounts" not in str(e) or privacy == "SELF_ONLY":
                    raise
                warning = ("TikTok app not audited yet: posted as SELF_ONLY (private). Pass the Direct Post audit, "
                           "or set publish.platforms.tiktok.method=inbox for one-tap public posting.")
                post_info["privacy_level"] = "SELF_ONLY"
                data = self._post("/v2/post/publish/video/init/", {"post_info": post_info, "source_info": source})

        self._upload(data["upload_url"], post.video, size, chunk, count)
        status = self._wait(data["publish_id"])
        ids = status.get("publicaly_available_post_id") or status.get("publicly_available_post_id") or []
        remote = str(ids[0]) if ids else data["publish_id"]
        user = self.store.kv_get("tiktok_username") or ""
        url = f"https://www.tiktok.com/@{user}/video/{remote}" if ids and user else ""
        return PublishResult(platform=self.name, ok=True, remote_id=remote, url=url,
                             status=status.get("status", ""), warning=warning)

    def _upload(self, url: str, path, size: int, chunk: int, count: int) -> None:
        with open(path, "rb") as f:
            for i in range(count):
                start = i * chunk
                end = size - 1 if i == count - 1 else start + chunk - 1
                f.seek(start)
                body = f.read(end - start + 1)
                r = self.http.put(url, content=body, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {start}-{end}/{size}"}, timeout=600)
                if r.status_code not in (200, 201, 206):
                    raise PublishError(f"tiktok upload chunk {i} failed: {r.status_code} {r.text[:200]}")

    def _wait(self, publish_id: str, timeout_s: int = 600) -> dict:
        waited = 0
        while waited <= timeout_s:
            d = self._post("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
            st = d.get("status", "")
            if st in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                return d
            if st == "FAILED":
                raise PublishError(f"tiktok publish failed: {d.get('fail_reason', d)}")
            self.sleep(5)
            waited += 5
        raise PublishError("tiktok publish timed out")

    # ---- growth ----
    def followers(self) -> int | None:
        r = self.http.get(f"{API}/v2/user/info/", params={"fields": "follower_count,username"},
                          headers={"Authorization": f"Bearer {self.token()}"})
        u = r.json().get("data", {}).get("user", {})
        if u.get("username"):
            self.store.kv_set("tiktok_username", u["username"])
        return u.get("follower_count")

    def post_metrics(self, remote_ids: list[str]) -> dict[str, dict[str, int]]:
        ids = [i for i in remote_ids if i.isdigit()][:20]
        if not ids:
            return {}
        d = self._post("/v2/video/query/", {"filters": {"video_ids": ids}},
                       params={"fields": "id,view_count,like_count,comment_count,share_count"})
        return {str(v["id"]): {"views": v.get("view_count", 0), "likes": v.get("like_count", 0),
                               "comments": v.get("comment_count", 0), "shares": v.get("share_count", 0)}
                for v in d.get("videos", [])}
