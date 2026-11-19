from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from . import auth
from .config import load_config
from .models import Script
from .pipeline import open_store, render_with_fit, run_daily
from .providers.publishers import REGISTRY, make_publishers
from .providers.tts import make_tts
from .steps import growth
from .steps.publish import publish_post


def _cfg(args) -> dict:
    path = args.config if args.config and Path(args.config).exists() else None
    if args.config and not path and args.config != "config.yaml":
        raise SystemExit(f"config not found: {args.config}")
    return load_config(path)


def cmd_run(args) -> None:
    s = run_daily(_cfg(args), offline=args.offline, dry_run=args.dry_run)
    print(json.dumps(s, indent=2, default=str))


def cmd_demo(args) -> None:
    cfg = _cfg(args)
    cfg["state_dir"] = args.state or "demo_state"
    cfg["output_dir"] = args.out or "demo_output"
    if args.handle:
        cfg["handle"] = args.handle
    s = run_daily(cfg, offline=True)
    print(json.dumps(s, indent=2, default=str))


def cmd_render(args) -> None:
    cfg = _cfg(args)
    s = Script.from_dict(json.loads(Path(args.script).read_text()))
    r = render_with_fit(s, cfg, make_tts(cfg), Path(args.out))
    print(json.dumps(r, indent=2, default=str))


def cmd_approve(args) -> None:
    cfg = _cfg(args)
    store = open_store(cfg)
    res = publish_post(store, args.post_id, make_publishers(cfg, store), dry_run=args.dry_run)
    for r in res:
        print(f"{r.platform:10s} {'ok' if r.ok else 'FAILED':6s} {r.url or r.status} {r.warning or r.error}")


def cmd_pending(args) -> None:
    store = open_store(_cfg(args))
    for p in store.posts(status="pending_review"):
        print(f"{p['id']}  {p['topic']}  {p['video_path']}")


def cmd_hooks(args) -> None:
    store = open_store(_cfg(args))
    for h in store.hooks(limit=args.n):
        print(f"[{h.id}] {h.views:>12,}  {h.platform:9s} {h.hook_type:16s} {h.template}")


def cmd_metrics(args) -> None:
    cfg = _cfg(args)
    store = open_store(cfg)
    out = growth.collect(cfg, store, make_publishers(cfg, store))
    kit = growth.check_milestone(cfg, store, Path(cfg["output_dir"]))
    print(json.dumps({**out, "media_kit": str(kit) if kit else None}, indent=2))


def cmd_mediakit(args) -> None:
    cfg = _cfg(args)
    print(growth.write_media_kit(cfg, open_store(cfg), Path(cfg["output_dir"])))


def cmd_auth(args) -> None:
    cfg = _cfg(args)
    if args.platform == "tiktok":
        auth.tiktok(open_store(cfg))
    elif args.platform == "youtube":
        auth.youtube(open_store(cfg))
    else:
        auth.instagram()


def cmd_doctor(args) -> None:
    cfg = _cfg(args)
    store = open_store(cfg)
    ok = lambda b: "ok " if b else "-- "  # noqa: E731
    print("tools")
    for b in ("ffmpeg", "ffprobe"):
        print(f"  {ok(shutil.which(b))}{b}")
    print("brain / research")
    for k, why in [("ANTHROPIC_API_KEY", "Claude (required)"), ("HOOKLOOP_STATE_KEY", "encrypts rotated tokens in state DB"), ("APIFY_TOKEN", "TikTok + Instagram hashtag research"),
                   ("YOUTUBE_API_KEY", "YouTube Shorts research"), ("OPENAI_API_KEY", "transcribing source hooks / openai TTS")]:
        print(f"  {ok(os.environ.get(k))}{k:20s} {why}")
    print(f"voice: {cfg['tts']['provider']}")
    print("publishers")
    for name, cls in REGISTRY.items():
        c = cfg["publish"]["platforms"].get(name, {})
        p = cls(cfg, store)
        state = "disabled" if not c.get("enabled") else ("ready" if p.configured() else "missing credentials")
        print(f"  {ok(state == 'ready')}{name:10s} {state}")
    print(f"publish mode: {cfg['publish']['mode']}")


def cmd_keygen(args) -> None:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="hookloop", description="Daily short-form content agent")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="full daily loop: topic -> research -> hooks -> script -> render -> publish")
    p.add_argument("--dry-run", action="store_true", help="do everything except publish")
    p.add_argument("--offline", action="store_true", help="use bundled demo fixtures instead of live APIs")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("demo", help="offline end-to-end demo (no API keys), renders a real video")
    p.add_argument("--state"); p.add_argument("--out"); p.add_argument("--handle")
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("render", help="render a script.json to video")
    p.add_argument("script"); p.add_argument("-o", "--out", default="output/manual")
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("approve", help="publish a post that is pending review (or retry failed platforms)")
    p.add_argument("post_id"); p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_approve)

    sub.add_parser("pending", help="list posts waiting for review").set_defaults(fn=cmd_pending)
    p = sub.add_parser("hooks", help="show the hook library"); p.add_argument("-n", type=int, default=25)
    p.set_defaults(fn=cmd_hooks)
    sub.add_parser("metrics", help="pull followers + post stats, check the 10k milestone").set_defaults(fn=cmd_metrics)
    sub.add_parser("mediakit", help="write a sponsorship media kit from current stats").set_defaults(fn=cmd_mediakit)
    p = sub.add_parser("auth", help="mint OAuth refresh tokens"); p.add_argument("platform", choices=["tiktok", "youtube", "instagram"])
    p.set_defaults(fn=cmd_auth)
    sub.add_parser("doctor", help="check tools and credentials").set_defaults(fn=cmd_doctor)
    sub.add_parser("keygen", help="generate HOOKLOOP_STATE_KEY").set_defaults(fn=cmd_keygen)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("phonemizer").setLevel(logging.ERROR)
    args.fn(args)


if __name__ == "__main__":
    main()
