"""One-time OAuth helpers to mint the refresh tokens the daily job needs.

  hookloop auth youtube    local loopback flow (Google "Desktop app" OAuth client)
  hookloop auth tiktok     paste-back flow (TikTok requires an https redirect URI you own)
  hookloop auth instagram  prints the token steps (Meta dashboard issues the long-lived token)
"""
from __future__ import annotations

import http.server
import os
import secrets
import threading
import urllib.parse
import webbrowser

import httpx

from .store import Store

TIKTOK_SCOPES = "user.info.basic,user.info.profile,user.info.stats,video.list,video.publish,video.upload"
YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def _code_from(url_or_code: str) -> str:
    s = url_or_code.strip()
    if s.startswith("http"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(s).query)
        return q.get("code", [""])[0]
    return s


def tiktok(store: Store, expected_handle: str = "") -> None:
    key, secret = os.environ["TIKTOK_CLIENT_KEY"], os.environ["TIKTOK_CLIENT_SECRET"]
    redirect = os.environ.get("TIKTOK_REDIRECT_URI") or input("Redirect URI registered in your TikTok app (https://...): ").strip()
    state = secrets.token_urlsafe(16)
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": key, "scope": TIKTOK_SCOPES, "response_type": "code", "redirect_uri": redirect, "state": state})
    print(f"\n1) Open and approve:\n{url}\n\n2) You'll land on your redirect URI. Paste that full URL here.")
    code = _code_from(input("> "))
    r = httpx.post("https://open.tiktokapis.com/v2/oauth/token/", data={
        "client_key": key, "client_secret": secret, "code": code, "grant_type": "authorization_code", "redirect_uri": redirect,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    j = r.json()
    if "refresh_token" not in j:
        raise SystemExit(f"TikTok token exchange failed: {j}")
    store.secret_set("tiktok_refresh_token", j["refresh_token"])
    who = check_tiktok_account(j["access_token"], expected_handle)
    if who:
        store.kv_set("tiktok_username", who)
    print(f"\nTIKTOK_REFRESH_TOKEN={j['refresh_token']}\n(Saved to state DB; add it as a GitHub Actions secret too.)")


def check_tiktok_account(access_token: str, expected_handle: str = "", client: httpx.Client | None = None) -> str:
    """Print which TikTok account the token belongs to; warn if it isn't the configured handle."""
    c = client or httpx.Client(timeout=30)
    r = c.get("https://open.tiktokapis.com/v2/user/info/", params={"fields": "username,display_name"},
              headers={"Authorization": f"Bearer {access_token}"})
    u = (r.json().get("data") or {}).get("user") or {}
    name = u.get("username", "")
    if not name:
        print(f"\n!! Could not read the TikTok username ({r.json().get('error', {})}). Check the account in the TikTok app.")
        return ""
    want = expected_handle.lstrip("@").lower()
    if want and name.lower() != want:
        print(f"\n!! You authorized @{name}, but config.yaml expects @{want}. Re-run this with the right TikTok account.")
    else:
        print(f"\nConnected TikTok account: @{name} ({u.get('display_name', '')})")
    return name


def youtube(store: Store, port: int = 8765) -> None:
    cid, csec = os.environ["YT_CLIENT_ID"], os.environ["YT_CLIENT_SECRET"]
    redirect = f"http://127.0.0.1:{port}"
    got: dict[str, str] = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got["code"] = q.get("code", [""])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hookloop: authorized. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": YT_SCOPES,
        "access_type": "offline", "prompt": "consent"})
    print(f"Opening browser. If it doesn't open, visit:\n{url}")
    webbrowser.open(url)
    input("Press Enter after approving in the browser... ")
    code = got.get("code") or _code_from(input("Paste the redirected URL (or code): "))
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": cid, "client_secret": csec, "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30)
    j = r.json()
    if "refresh_token" not in j:
        raise SystemExit(f"Google token exchange failed: {j}")
    print(f"\nYT_REFRESH_TOKEN={j['refresh_token']}\n(Add it as a GitHub Actions secret.)")


def instagram() -> None:
    print("""Instagram (Instagram API with Instagram Login):
 1. Switch the account to Professional (Creator or Business).
 2. developers.facebook.com -> create an app -> add the "Instagram" product ->
    "API setup with Instagram login". Add your account as an Instagram tester and accept the invite.
 3. Permissions: instagram_business_basic, instagram_business_content_publish,
    instagram_business_manage_insights.
 4. Click "Generate token" for your account. That long-lived token (60 days) is IG_ACCESS_TOKEN;
    hookloop refreshes it weekly. IG_USER_ID is shown next to it.""")
