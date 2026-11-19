from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("hookloop")


def run(cmd: list[str], *, check: bool = True, timeout: int | None = 600) -> subprocess.CompletedProcess:
    log.debug("$ %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd[:4])} ...\n{p.stderr[-2000:]}")
    return p


def need(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise RuntimeError(f"`{binary}` is required but not on PATH")
    return path


def media_duration(path: str | Path) -> float:
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    return float(json.loads(p.stdout)["format"]["duration"])


def slug(s: str, n: int = 40) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in s)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:n] or "post"
