"""The daily loop: steps 1-7 in order, with state, retries and a human-review option."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .llm import load_fake, make_llm
from .models import Script
from .notify import notify
from .providers.publishers import make_publishers
from .providers.tts import TooLong, make_tts
from .steps import growth
from .steps.hooks import mine_hooks
from .steps.publish import publish_post
from .steps.render import render
from .steps.research import research
from .steps.script import write_script
from .steps.topic import pick_topic
from .store import Store, today
from .util import slug

log = logging.getLogger("hookloop")
FIXTURES = Path(__file__).resolve().parent / "demo"


def open_store(cfg: dict[str, Any]) -> Store:
    return Store(Path(cfg["state_dir"]) / "hookloop.db")


def render_with_fit(script: Script, cfg: dict[str, Any], tts, out_dir: Path) -> dict[str, Any]:
    """Render; if the voiceover can't fit max_seconds, drop the longest middle beat and retry."""
    for _ in range(3):
        try:
            return render(script, cfg, tts, out_dir)
        except TooLong as e:
            middle = [i for i, s in enumerate(script.segments) if s.style not in ("hook", "cta")]
            if len(middle) <= 1:
                raise
            drop = max(middle, key=lambda i: len(script.segments[i].voiceover.split()))
            log.warning("%s -> dropping beat %d", e, drop)
            script.segments.pop(drop)
    raise TooLong("could not fit the script into max_seconds")


def run_daily(cfg: dict[str, Any], *, offline: bool = False, dry_run: bool = False) -> dict[str, Any]:
    store = open_store(cfg)
    publishers = {} if offline else make_publishers(cfg, store)

    # Step 7 (feedback half): refresh metrics first so today's hook choice learns from yesterday
    if publishers and not dry_run:
        growth.collect(cfg, store, publishers)

    llm = load_fake(FIXTURES / "demo_llm.json") if offline else make_llm(cfg)

    topic = pick_topic(cfg, store, llm)  # Step 1 (+6 rotation)
    winners = research(cfg, store, topic, fixture=FIXTURES / "demo_sources.json" if offline else None)  # Steps 2-3
    mine_hooks(cfg, store, llm, winners, topic, offline=offline)  # Step 4
    script = write_script(cfg, store, llm, topic)  # Step 5

    post_id = f"{today()}-{slug(topic.name, 30)}"
    out_dir = Path(cfg["output_dir"]) / post_id
    tts = make_tts(cfg)
    r = render_with_fit(script, cfg, tts, out_dir)  # Step 5 (<=45s enforced)
    (out_dir / "script.json").write_text(json.dumps(script.to_dict(), indent=2))
    (out_dir / "caption.txt").write_text(script.caption + "\n\n" + " ".join(f"#{h}" for h in script.hashtags))
    store.create_post(post_id, topic.name, script.hook_id, script.hook_type, script.to_dict(), r["video"], "rendered")

    summary: dict[str, Any] = {"post_id": post_id, "topic": topic.name, "video": r["video"], "duration": round(r["duration"], 1),
                               "hook": script.segments[0].voiceover, "hook_type": script.hook_type, "results": []}

    mode = cfg["publish"]["mode"]
    if mode == "review" and not dry_run:
        store.set_post_status(post_id, "pending_review")
        notify(cfg, f"hookloop: new video ready for review — *{topic.name}* ({r['duration']:.0f}s)\n"
                    f"Hook: “{summary['hook']}”\nApprove with: `hookloop approve {post_id}`")
        summary["status"] = "pending_review"
    else:
        targets = publishers or ({} if not offline else {n: None for n, c in cfg["publish"]["platforms"].items() if c.get("enabled")})
        if targets and not (dry_run or offline) and cfg.get("handle", "").strip() in ("", "@yourhandle"):
            log.error("handle is still the placeholder '@yourhandle'; set `handle` in config.yaml. Rendered, not posted.")
            notify(cfg, f"hookloop: rendered {post_id} but did not post: set `handle` in config.yaml first.")
            targets = {}
        if not targets:
            log.warning("no publishers configured; video rendered but not posted (run `hookloop doctor`)")
            results = []
        else:
            results = publish_post(store, post_id, targets, dry_run=dry_run or offline)  # Step 6
        summary["results"] = [res.__dict__ for res in results]
        summary["status"] = "dry_run" if (dry_run or offline) else store.get_post(post_id)["status"]
        lines = [f"hookloop posted *{topic.name}* ({r['duration']:.0f}s) — “{summary['hook']}”"]
        lines += [f"• {x.platform}: {'ok' if x.ok else 'FAILED'} {x.url or x.status} {x.warning or x.error}".rstrip() for x in results]
        if not (dry_run or offline):
            notify(cfg, "\n".join(lines))

    # Step 7 (milestone half)
    kit = growth.check_milestone(cfg, store, Path(cfg["output_dir"]))
    if kit:
        notify(cfg, f"hookloop: you crossed {cfg['growth']['monetize_at_followers']:,} followers — time to monetize. "
                    f"Media kit: {kit}")
        summary["media_kit"] = str(kit)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary
