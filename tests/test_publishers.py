"""Publisher flows against mocked platform APIs (request order, headers, payloads, fallbacks)."""
import json

import httpx
import pytest
import respx

from hookloop.config import load_config
from hookloop.providers.publishers import Post
from hookloop.providers.publishers.instagram import InstagramPublisher
from hookloop.providers.publishers.tiktok import TikTokPublisher, chunk_plan
from hookloop.providers.publishers.youtube import YouTubePublisher
from hookloop.store import Store

MB = 1024 * 1024


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in {"TIKTOK_CLIENT_KEY": "ck", "TIKTOK_CLIENT_SECRET": "cs", "TIKTOK_REFRESH_TOKEN": "rt",
                 "IG_USER_ID": "17841", "IG_ACCESS_TOKEN": "igtok", "YT_CLIENT_ID": "yc", "YT_CLIENT_SECRET": "ys",
                 "YT_REFRESH_TOKEN": "yr"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("HOOKLOOP_STATE_KEY", raising=False)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 2048)
    return {"cfg": load_config(), "store": Store(tmp_path / "s.db"),
            "post": Post(video=video, title="Stop re-pasting prompts", caption="Write once.", hashtags=["claude", "aitools"])}


def test_chunk_plan():
    assert chunk_plan(3 * MB) == (3 * MB, 1)
    assert chunk_plan(64 * MB) == (64 * MB, 1)
    assert chunk_plan(95 * MB) == (10 * MB, 9)


@respx.mock
def test_tiktok_direct_post_with_unaudited_fallback(env):
    api = "https://open.tiktokapis.com"
    respx.post(f"{api}/v2/oauth/token/").respond(json={"access_token": "at", "expires_in": 86400, "refresh_token": "rt2"})
    respx.post(f"{api}/v2/post/publish/creator_info/query/").respond(
        json={"data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]}, "error": {"code": "ok"}})
    init = respx.post(f"{api}/v2/post/publish/video/init/").mock(side_effect=[
        httpx.Response(403, json={"error": {"code": "unaudited_client_can_only_post_to_private_accounts", "message": "x"}}),
        httpx.Response(200, json={"data": {"publish_id": "pub1", "upload_url": "https://open-upload.tiktokapis.com/upload/?id=1"},
                                  "error": {"code": "ok"}}),
    ])
    put = respx.put("https://open-upload.tiktokapis.com/upload/").respond(201)
    respx.post(f"{api}/v2/post/publish/status/fetch/").mock(side_effect=[
        httpx.Response(200, json={"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}}),
        httpx.Response(200, json={"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7311]},
                                  "error": {"code": "ok"}}),
    ])
    p = TikTokPublisher(env["cfg"], env["store"], sleep=lambda s: None)
    r = p.publish(env["post"])
    assert r.ok and r.remote_id == "7311" and "SELF_ONLY" in r.warning
    second = json.loads(init.calls[1].request.content)
    assert second["post_info"]["privacy_level"] == "SELF_ONLY"
    assert second["source_info"] == {"source": "FILE_UPLOAD", "video_size": 2048, "chunk_size": 2048, "total_chunk_count": 1}
    assert "#claude #aitools" in second["post_info"]["title"]
    assert put.calls[0].request.headers["Content-Range"] == "bytes 0-2047/2048"
    assert env["store"].secret_get("tiktok_refresh_token") == "rt2"  # rotated token kept


@respx.mock
def test_tiktok_inbox_mode(env):
    api = "https://open.tiktokapis.com"
    env["cfg"]["publish"]["platforms"]["tiktok"]["method"] = "inbox"
    respx.post(f"{api}/v2/oauth/token/").respond(json={"access_token": "at", "expires_in": 86400})
    inbox = respx.post(f"{api}/v2/post/publish/inbox/video/init/").respond(
        json={"data": {"publish_id": "p2", "upload_url": "https://up.example/u"}, "error": {"code": "ok"}})
    respx.put("https://up.example/u").respond(201)
    respx.post(f"{api}/v2/post/publish/status/fetch/").respond(json={"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}})
    r = TikTokPublisher(env["cfg"], env["store"], sleep=lambda s: None).publish(env["post"])
    assert r.ok and r.status == "SEND_TO_USER_INBOX" and inbox.called


@respx.mock
def test_instagram_resumable_reel(env):
    base = "https://graph.instagram.com/v25.0"
    respx.get("https://graph.instagram.com/refresh_access_token").respond(json={"access_token": "igtok2"})
    create = respx.post(f"{base}/17841/media").respond(json={"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/c1"})
    up = respx.post("https://rupload.facebook.com/ig-api-upload/v25.0/c1").respond(json={"success": True})
    respx.get(f"{base}/c1").mock(side_effect=[httpx.Response(200, json={"status_code": "IN_PROGRESS"}),
                                              httpx.Response(200, json={"status_code": "FINISHED"})])
    pub = respx.post(f"{base}/17841/media_publish").respond(json={"id": "m9"})
    respx.get(f"{base}/m9").respond(json={"permalink": "https://www.instagram.com/reel/xyz/"})
    r = InstagramPublisher(env["cfg"], env["store"], sleep=lambda s: None).publish(env["post"])
    assert r.ok and r.remote_id == "m9" and r.url.endswith("/reel/xyz/")
    form = dict(x.split("=", 1) for x in create.calls[0].request.content.decode().split("&"))
    assert form["media_type"] == "REELS" and form["upload_type"] == "resumable"
    h = up.calls[0].request.headers
    assert h["Authorization"] == "OAuth igtok2" and h["offset"] == "0" and h["file_size"] == "2048"
    assert b"creation_id=c1" in pub.calls[0].request.content


@respx.mock
def test_youtube_resumable_short_and_private_warning(env):
    respx.post("https://oauth2.googleapis.com/token").respond(json={"access_token": "ya29"})
    init = respx.post("https://www.googleapis.com/upload/youtube/v3/videos").respond(
        200, headers={"Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=U1"})
    respx.put("https://www.googleapis.com/upload/youtube/v3/videos?upload_id=U1").respond(
        200, json={"id": "abc123", "status": {"uploadStatus": "uploaded", "privacyStatus": "private"}})
    r = YouTubePublisher(env["cfg"], env["store"]).publish(env["post"])
    assert r.ok and r.url == "https://youtube.com/shorts/abc123" and "audit" in r.warning
    meta = json.loads(init.calls[0].request.content)
    assert meta["snippet"]["title"].endswith("#shorts")
    assert meta["status"]["containsSyntheticMedia"] is True
    assert init.calls[0].request.headers["X-Upload-Content-Length"] == "2048"
