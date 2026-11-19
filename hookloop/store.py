"""SQLite state: topic rotation history, source videos, hook library, posts, metrics, tokens."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Hook, SourceVideo

log = logging.getLogger("hookloop.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics_used (
  day TEXT NOT NULL, topic TEXT NOT NULL, hashtags TEXT, why TEXT
);
CREATE TABLE IF NOT EXISTS source_videos (
  key TEXT PRIMARY KEY, platform TEXT, url TEXT, media_url TEXT, author TEXT, caption TEXT,
  views INTEGER, likes INTEGER, duration_s REAL, posted_at TEXT, topic TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS hooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT UNIQUE, platform TEXT, views INTEGER,
  opening_line TEXT, hook_type TEXT, template TEXT, structure TEXT, visual_pattern TEXT,
  on_screen_text TEXT, why_it_works TEXT, score REAL, topic TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, day TEXT, topic TEXT, hook_id INTEGER, hook_type TEXT, script TEXT,
  video_path TEXT, status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS publications (
  post_id TEXT, platform TEXT, remote_id TEXT, url TEXT, status TEXT, warning TEXT, error TEXT,
  published_at TEXT, PRIMARY KEY (post_id, platform)
);
CREATE TABLE IF NOT EXISTS metrics (
  platform TEXT, remote_id TEXT, captured_at TEXT, views INTEGER, likes INTEGER,
  comments INTEGER, shares INTEGER
);
CREATE TABLE IF NOT EXISTS followers (platform TEXT, captured_at TEXT, count INTEGER);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, updated_at TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- kv (rotating OAuth tokens etc.) ----
    def kv_get(self, k: str, default: str | None = None) -> str | None:
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def kv_set(self, k: str, v: str) -> None:
        self.db.execute(
            "INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (k, v, now_iso()),
        )
        self.db.commit()

    # ---- secrets (OAuth tokens): encrypted at rest, because the workflow commits this DB ----
    _mem: dict[str, str]

    def _fernet(self):
        key = os.environ.get("HOOKLOOP_STATE_KEY")
        if not key:
            return None
        from cryptography.fernet import Fernet

        return Fernet(key.encode())

    def secret_get(self, k: str) -> str | None:
        f = self._fernet()
        raw = self.kv_get(k)
        if f and raw and raw.startswith("enc:"):
            try:
                return f.decrypt(raw[4:].encode()).decode()
            except Exception:
                log.warning("could not decrypt %s (HOOKLOOP_STATE_KEY changed?)", k)
                return None
        return getattr(self, "_mem", {}).get(k)

    def secret_set(self, k: str, v: str) -> None:
        f = self._fernet()
        if f:
            self.kv_set(k, "enc:" + f.encrypt(v.encode()).decode())
        else:
            if not hasattr(self, "_mem"):
                self._mem = {}
                log.warning("HOOKLOOP_STATE_KEY not set: rotated tokens are kept in memory only, not persisted")
            self._mem[k] = v

    def kv_updated(self, k: str) -> str | None:
        r = self.db.execute("SELECT updated_at FROM kv WHERE k=?", (k,)).fetchone()
        return r["updated_at"] if r else None

    # ---- topics ----
    def record_topic(self, topic: str, hashtags: list[str], why: str = "", day: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO topics_used(day,topic,hashtags,why) VALUES(?,?,?,?)",
            (day or today(), topic, json.dumps(hashtags), why),
        )
        self.db.commit()

    def recent_topics(self, days: int) -> list[str]:
        since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
        rows = self.db.execute("SELECT topic FROM topics_used WHERE day > ? ORDER BY day DESC", (since,)).fetchall()
        return [r["topic"] for r in rows]

    def topic_last_used(self) -> dict[str, str]:
        rows = self.db.execute("SELECT topic, MAX(day) AS d FROM topics_used GROUP BY topic").fetchall()
        return {r["topic"]: r["d"] for r in rows}

    # ---- sources ----
    def upsert_sources(self, vids: Iterable[SourceVideo], topic: str) -> None:
        for v in vids:
            self.db.execute(
                """INSERT INTO source_videos VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET views=excluded.views, likes=excluded.likes,
                media_url=excluded.media_url, fetched_at=excluded.fetched_at""",
                (v.key, v.platform, v.url, v.media_url, v.author, v.caption, v.views, v.likes,
                 v.duration_s, v.posted_at, topic, now_iso()),
            )
        self.db.commit()

    # ---- hooks ----
    def has_hook(self, source_key: str) -> bool:
        return self.db.execute("SELECT 1 FROM hooks WHERE source_key=?", (source_key,)).fetchone() is not None

    def add_hook(self, h: Hook) -> int:
        cur = self.db.execute(
            """INSERT OR IGNORE INTO hooks(source_key,platform,views,opening_line,hook_type,template,structure,
            visual_pattern,on_screen_text,why_it_works,score,topic,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (h.source_key, h.platform, h.views, h.opening_line, h.hook_type, h.template, json.dumps(h.structure),
             h.visual_pattern, h.on_screen_text, h.why_it_works, h.score, h.topic, now_iso()),
        )
        self.db.commit()
        return cur.lastrowid

    def hooks(self, topic: str | None = None, limit: int = 200) -> list[Hook]:
        q = "SELECT * FROM hooks"
        args: tuple = ()
        if topic:
            q += " WHERE topic=?"
            args = (topic,)
        q += " ORDER BY views DESC LIMIT ?"
        rows = self.db.execute(q, args + (limit,)).fetchall()
        return [
            Hook(
                id=r["id"], source_key=r["source_key"], platform=r["platform"], views=r["views"],
                opening_line=r["opening_line"], hook_type=r["hook_type"], template=r["template"],
                structure=json.loads(r["structure"] or "[]"), visual_pattern=r["visual_pattern"],
                on_screen_text=r["on_screen_text"], why_it_works=r["why_it_works"], score=r["score"], topic=r["topic"],
            )
            for r in rows
        ]

    # ---- posts ----
    def create_post(self, post_id: str, topic: str, hook_id: int | None, hook_type: str, script: dict,
                    video_path: str, status: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO posts VALUES(?,?,?,?,?,?,?,?,?)",
            (post_id, today(), topic, hook_id, hook_type, json.dumps(script), video_path, status, now_iso()),
        )
        self.db.commit()

    def set_post_status(self, post_id: str, status: str) -> None:
        self.db.execute("UPDATE posts SET status=? WHERE id=?", (status, post_id))
        self.db.commit()

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        return dict(r) if r else None

    def posts(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            rows = self.db.execute("SELECT * FROM posts WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.execute("SELECT * FROM posts ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows.fetchall()]

    def record_publication(self, post_id: str, res) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO publications VALUES(?,?,?,?,?,?,?,?)",
            (post_id, res.platform, res.remote_id, res.url, res.status, res.warning, res.error, now_iso()),
        )
        self.db.commit()

    def publications(self, since_days: int | None = None) -> list[dict[str, Any]]:
        q = """SELECT p.*, posts.hook_type, posts.topic FROM publications p JOIN posts ON posts.id = p.post_id
               WHERE p.remote_id != ''"""
        args: tuple = ()
        if since_days is not None:
            q += " AND p.published_at > ?"
            args = ((datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat(),)
        return [dict(r) for r in self.db.execute(q, args).fetchall()]

    # ---- metrics ----
    def add_metrics(self, platform: str, remote_id: str, m: dict[str, int]) -> None:
        self.db.execute(
            "INSERT INTO metrics VALUES(?,?,?,?,?,?,?)",
            (platform, remote_id, now_iso(), m.get("views", 0), m.get("likes", 0), m.get("comments", 0), m.get("shares", 0)),
        )
        self.db.commit()

    def latest_metrics(self) -> dict[tuple[str, str], dict[str, Any]]:
        rows = self.db.execute(
            """SELECT m.* FROM metrics m JOIN (SELECT platform, remote_id, MAX(captured_at) c FROM metrics
               GROUP BY platform, remote_id) x ON m.platform=x.platform AND m.remote_id=x.remote_id AND m.captured_at=x.c"""
        ).fetchall()
        return {(r["platform"], r["remote_id"]): dict(r) for r in rows}

    def add_followers(self, platform: str, count: int) -> None:
        self.db.execute("INSERT INTO followers VALUES(?,?,?)", (platform, now_iso(), count))
        self.db.commit()

    def latest_followers(self) -> dict[str, int]:
        rows = self.db.execute(
            """SELECT f.platform, f.count FROM followers f JOIN (SELECT platform, MAX(captured_at) c FROM followers
               GROUP BY platform) x ON f.platform=x.platform AND f.captured_at=x.c"""
        ).fetchall()
        return {r["platform"]: r["count"] for r in rows}
