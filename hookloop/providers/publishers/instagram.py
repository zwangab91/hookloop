"""Instagram Reels via the Instagram Platform content-publishing API (resumable upload, so the
video never needs a public URL).

Flow: POST /{ig_user}/media (media_type=REELS, upload_type=resumable) -> POST bytes to the
returned rupload.facebook.com uri -> poll container status_code -> POST /{ig_user}/media_publish.

Env: IG_USER_ID, IG_ACCESS_TOKEN (long-lived; auto-refreshed weekly on graph.instagram.com).
Needs a Professional (Business/Creator) account; permissions instagram_business_basic +
instagram_business_content_publish (+ instagram_business_manage_insights for metrics).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from ...models import PublishResult
from .base import Post, PublishError, Publisher

log = logging.getLogger("hookloop.instagram")


class InstagramPublisher(Publisher):
    name = "instagram"

    @property
    def c(self) -> dict:
        return self.cfg["publish"]["platforms"]["instagram"]

    @property
    def base(self) -> str:
        return f"https://{self.c['api_host']}/{self.c['api_version']}"

    def configured(self) -> bool:
        return bool(os.environ.get("IG_USER_ID") and (self.store.secret_get("ig_access_token") or os.environ.get("IG_ACCESS_TOKEN")))

    def token(self) -> str:
        tok = self.store.secret_get("ig_access_token") or os.environ["IG_ACCESS_TOKEN"]
        if self.c["api_host"] == "graph.instagram.com":
            last = self.store.kv_updated("ig_access_token")
            if not last or datetime.fromisoformat(last) < datetime.now(timezone.utc) - timedelta(days=7):
                try:
                    r = self.http.get("https://graph.instagram.com/refresh_access_token",
                                      params={"grant_type": "ig_refresh_token", "access_token": tok})
                    if r.status_code == 200 and r.json().get("access_token"):
                        tok = r.json()["access_token"]
                        self.store.secret_set("ig_access_token", tok)
                except Exception as e:  # refresh is best-effort; token may still be valid
                    log.warning("instagram token refresh failed: %s", e)
        return tok

    def _check(self, r) -> dict:
        j = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in j:
            raise PublishError(f"instagram {r.status_code}: {j.get('error', j)}")
        return j

    def publish(self, post: Post) -> PublishResult:
        tok = self.token()
        uid = os.environ["IG_USER_ID"]
        j = self._check(self.http.post(f"{self.base}/{uid}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": post.caption_with_tags(2200),
            "share_to_feed": "true" if self.c.get("share_to_feed", True) else "false", "access_token": tok,
        }))
        container = j["id"]
        upload_uri = j.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.c['api_version']}/{container}"
        data = post.video.read_bytes()
        self._check(self.http.post(upload_uri, content=data, headers={
            "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(len(data))}, timeout=600))

        for _ in range(120):  # up to ~10 minutes of processing
            s = self._check(self.http.get(f"{self.base}/{container}", params={"fields": "status_code,status", "access_token": tok}))
            code = s.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"instagram processing {code}: {s.get('status')}")
            self.sleep(5)
        else:
            raise PublishError("instagram processing timed out")

        media = self._check(self.http.post(f"{self.base}/{uid}/media_publish", data={"creation_id": container, "access_token": tok}))
        mid = media["id"]
        link = ""
        try:
            link = self._check(self.http.get(f"{self.base}/{mid}", params={"fields": "permalink", "access_token": tok})).get("permalink", "")
        except PublishError:
            pass
        return PublishResult(platform=self.name, ok=True, remote_id=mid, url=link, status="PUBLISHED")

    def followers(self) -> int | None:
        j = self._check(self.http.get(f"{self.base}/{os.environ['IG_USER_ID']}",
                                      params={"fields": "followers_count", "access_token": self.token()}))
        return j.get("followers_count")

    def post_metrics(self, remote_ids: list[str]) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        tok = self.token()
        for mid in remote_ids:
            try:
                j = self._check(self.http.get(f"{self.base}/{mid}/insights",
                                              params={"metric": "views,likes,comments,shares", "access_token": tok}))
            except PublishError as e:
                log.warning("instagram insights for %s: %s", mid, e)
                continue
            vals = {d["name"]: (d.get("values") or [{}])[0].get("value", d.get("total_value", {}).get("value", 0))
                    for d in j.get("data", [])}
            out[mid] = {"views": vals.get("views", 0), "likes": vals.get("likes", 0),
                        "comments": vals.get("comments", 0), "shares": vals.get("shares", 0)}
        return out
