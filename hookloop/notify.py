from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("hookloop.notify")


def notify(cfg: dict[str, Any], text: str) -> None:
    url = (cfg.get("notify") or {}).get("slack_webhook") or ""
    if not url:
        return
    try:
        httpx.post(url, json={"text": text}, timeout=20).raise_for_status()
    except Exception as e:
        log.warning("notify failed: %s", e)
