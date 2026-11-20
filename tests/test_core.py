import json
from pathlib import Path

import pytest

from hookloop.config import load_config
from hookloop.llm import FakeLLM
from hookloop.models import Hook, Script, Segment, SourceVideo
from hookloop.providers.sources import norm_instagram, norm_tiktok, _iso_duration
from hookloop.steps import growth
from hookloop.steps.research import select_winners
from hookloop.steps.script import overlap, trim_to_budget, validate, word_budget, write_script
from hookloop.steps.topic import pick_topic
from hookloop.store import Store

DEMO = Path(__file__).resolve().parent.parent / "hookloop" / "demo" / "demo_llm.json"


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c["state_dir"] = str(tmp_path)
    return c


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def demo_script() -> Script:
    d = json.loads(DEMO.read_text())["submit_script"]
    d["hook_id"] = d.pop("reference_hook_id")
    return Script.from_dict(d)


# ---------------- Step 1 + 6: topic rotation
def test_rotation_blocks_recent_topic_even_if_model_picks_it(cfg, store):
    store.record_topic("Claude Code Code Mode", ["claudecode"])
    llm = FakeLLM({"submit_topic": {"topic": "Claude Code Code Mode", "hashtags": ["x"], "why_now": "", "facts": []}})
    t = pick_topic(cfg, store, llm)
    assert t.name != "Claude Code Code Mode"
    assert t.name == "Claude Fable 5.2"  # next fresh item in the pool


def test_topic_uses_model_choice_and_cleans_hashtags(cfg, store):
    llm = FakeLLM({"submit_topic": {"topic": "Claude Skills", "hashtags": ["#ClaudeSkills", "claude ai"],
                                    "why_now": "hot", "facts": [{"fact": "f", "source_url": "u"}]}})
    t = pick_topic(cfg, store, llm)
    assert t.hashtags == ["claudeskills", "claudeai"]
    assert store.recent_topics(7) == ["Claude Skills"]


def test_rotation_falls_back_to_least_recent_when_pool_exhausted(cfg, store):
    cfg["topics"]["pool"] = ["A", "B"]
    store.record_topic("B", [], day="2026-01-01")
    store.record_topic("A", [], day="2026-01-02")
    cfg["topics"]["rotation_days"] = 100000
    t = pick_topic(cfg, store, None)
    assert t.name == "B"


# ---------------- Steps 2-3: research
def test_tiktok_and_instagram_normalization():
    tt = norm_tiktok({"id": "1", "webVideoUrl": "https://tiktok.com/@a/video/1", "playCount": 1500000,
                      "diggCount": 9, "text": "hi", "authorMeta": {"name": "a"}, "videoMeta": {"duration": 30}})
    assert tt.views == 1_500_000 and tt.duration_s == 30 and tt.author == "a"
    ig = norm_instagram({"id": "2", "shortCode": "abc", "type": "Video", "videoPlayCount": 2000000, "ownerUsername": "b"})
    assert ig.url.endswith("/reel/abc/") and ig.views == 2_000_000
    assert norm_instagram({"id": "3", "type": "Image", "url": "x"}) is None
    assert _iso_duration("PT1M5S") == 65


def test_select_winners_applies_1m_bar_dedupes_and_falls_back(cfg):
    mk = lambda i, v, d=30: SourceVideo("tiktok", str(i), f"u{i}", v, duration_s=d)  # noqa: E731
    vids = [mk(1, 3_000_000), mk(1, 3_100_000), mk(2, 900_000), mk(3, 5_000_000, d=300)]
    w = select_winners(vids, cfg)
    assert [v.video_id for v in w] == ["1"] and w[0].views == 3_100_000  # dedupe kept max, long video dropped
    cfg["research"]["fallback_top_n"] = 1
    w = select_winners([mk(2, 900_000), mk(4, 100)], cfg)
    assert [v.video_id for v in w] == ["2"]


# ---------------- Step 5: script rules
def test_demo_script_passes_validation(cfg):
    s = demo_script()
    assert validate(s, cfg, []) == []
    assert s.word_count <= word_budget(cfg)


def test_validation_catches_copying_and_length(cfg):
    s = demo_script()
    src = Hook("tiktok:1", "tiktok", 2_000_000, opening_line="Stop pasting the same instructions into Claude every day.",
               hook_type="contrarian", template="")
    issues = validate(s, cfg, [src])
    assert any("too close to source" in i for i in issues)
    long = Script("t", "x", [Segment("word " * 60, style="hook"), Segment("word " * 80), Segment("bye", style="cta")], "", [])
    issues = validate(long, cfg, [])
    assert any("too long" in i for i in issues) and any("hook voiceover" in i for i in issues)


def test_trim_keeps_hook_and_cta(cfg):
    segs = [Segment("Hook line here.", style="hook")] + [Segment("filler " * 40) for _ in range(4)] + [Segment("Follow.", style="cta")]
    s = trim_to_budget(Script("t", "x", segs, "", []), cfg)
    assert s.segments[0].style == "hook" and s.segments[-1].style == "cta"
    assert s.word_count <= word_budget(cfg)


def test_overlap_metric():
    assert overlap("stop pasting the same prompt", "stop pasting the same prompt") == 1.0
    assert overlap("a b c d", "w x y z") == 0.0


def test_write_script_uses_critic_revision(cfg, store):
    from hookloop.models import Topic
    d = json.loads(DEMO.read_text())
    bad = dict(d["submit_script"]); bad["segments"] = [dict(x) for x in bad["segments"]]
    bad["segments"][-1]["style"] = "point"  # missing CTA -> should be revised
    llm = FakeLLM({"submit_script": bad, "submit_review": {"pass": False, "issues": ["no cta"], "revised": d["submit_script"]}})
    s = write_script(cfg, store, llm, Topic("Claude Skills", ["claudeskills"]))
    assert s.segments[-1].style == "cta"


# ---------------- Step 7: growth loop
def test_bandit_learns_from_beat_median(cfg, store):
    import random
    from hookloop.models import PublishResult
    for i, (ht, views) in enumerate([("result_first", 9000), ("result_first", 8000), ("contrarian", 100),
                                     ("contrarian", 200), ("result_first", 7000), ("contrarian", 150)]):
        store.create_post(f"p{i}", "t", None, ht, {}, "v.mp4", "published")
        store.record_publication(f"p{i}", PublishResult("tiktok", True, remote_id=str(i)))
        store.add_metrics("tiktok", str(i), {"views": views})
    store.db.execute("UPDATE publications SET published_at='2020-01-01T00:00:00+00:00'")
    store.db.commit()
    rng = random.Random(0)
    wins = sum(growth.hook_type_weights(store, rng)["result_first"] > growth.hook_type_weights(store, rng)["contrarian"]
               for _ in range(200))
    assert wins > 150


def test_milestone_fires_once(cfg, store, tmp_path):
    store.add_followers("tiktok", 10_250)
    kit = growth.check_milestone(cfg, store, tmp_path)
    assert kit and "10,250" in kit.read_text()
    assert growth.check_milestone(cfg, store, tmp_path) is None


# ---------------- secrets never land in the committed DB in plaintext
def test_secrets_encrypted_at_rest(store, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("HOOKLOOP_STATE_KEY", Fernet.generate_key().decode())
    store.secret_set("tiktok_refresh_token", "rt-SECRET")
    raw = store.kv_get("tiktok_refresh_token")
    assert raw.startswith("enc:") and "rt-SECRET" not in raw
    assert store.secret_get("tiktok_refresh_token") == "rt-SECRET"


def test_secrets_memory_only_without_key(store, monkeypatch):
    monkeypatch.delenv("HOOKLOOP_STATE_KEY", raising=False)
    store.secret_set("ig_access_token", "tok")
    assert store.kv_get("ig_access_token") is None and store.secret_get("ig_access_token") == "tok"


def test_placeholder_handle_blocks_publishing(cfg, tmp_path, monkeypatch):
    """With real publishers configured but handle still '@yourhandle', the run renders but posts nothing."""
    from hookloop import pipeline
    from hookloop.models import PublishResult

    calls = []

    class Pub:
        def publish(self, post):
            calls.append(post)
            return PublishResult("tiktok", True, remote_id="1")

    cfg["state_dir"], cfg["output_dir"] = str(tmp_path / "s"), str(tmp_path / "o")
    monkeypatch.setattr(pipeline, "make_publishers", lambda c, s: {"tiktok": Pub()})
    monkeypatch.setattr(pipeline, "make_llm", lambda c: pipeline.load_fake(pipeline.FIXTURES / "demo_llm.json"))
    monkeypatch.setattr(pipeline, "research", lambda c, s, t, fixture=None: [])
    monkeypatch.setattr(pipeline, "render_with_fit", lambda s, c, t, d: (d.mkdir(parents=True, exist_ok=True), {"video": str(d / "v.mp4"), "duration": 30.0})[1])
    monkeypatch.setattr(pipeline, "make_tts", lambda c: None)
    monkeypatch.setattr(pipeline.growth, "collect", lambda *a, **k: {})
    out = pipeline.run_daily(cfg)
    assert calls == [] and out["results"] == []


def test_review_mode_then_approve_on_a_fresh_runner(cfg, tmp_path, monkeypatch):
    """Day run renders + parks the post; a later 'approve' run (no output/ dir) publishes it."""
    import argparse
    from hookloop import cli, pipeline
    from hookloop.models import PublishResult

    posted = []

    class Pub:
        def configured(self):
            return True

        def publish(self, post):
            assert post.video.exists()
            posted.append(post)
            return PublishResult("instagram", True, remote_id="m1", url="https://instagram.com/reel/x/")

    cfg.update(state_dir=str(tmp_path / "state"), output_dir=str(tmp_path / "output"), handle="@realhandle")
    cfg["publish"]["mode"] = "review"

    def fake_render(s, c, t, d):
        d.mkdir(parents=True, exist_ok=True)
        (d / "video.mp4").write_bytes(b"mp4")
        return {"video": str(d / "video.mp4"), "duration": 31.0}

    monkeypatch.setattr(pipeline, "make_publishers", lambda c, s: {"instagram": Pub()})
    monkeypatch.setattr(pipeline, "make_llm", lambda c: pipeline.load_fake(pipeline.FIXTURES / "demo_llm.json"))
    monkeypatch.setattr(pipeline, "research", lambda c, s, t, fixture=None: [])
    monkeypatch.setattr(pipeline, "render_with_fit", fake_render)
    monkeypatch.setattr(pipeline, "make_tts", lambda c: None)
    monkeypatch.setattr(pipeline.growth, "collect", lambda *a, **k: {})
    out, summ = tmp_path / "gh_out", tmp_path / "gh_summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))

    s = pipeline.run_daily(cfg)
    cli.ci_report(s)
    assert s["status"] == "pending_review" and posted == []
    pid = s["post_id"]
    assert out.read_text() == f"post_id={pid}\n"
    card = summ.read_text()
    assert f"approve: `{pid}`" in card and f"hookloop-{pid}" in card and "Full script" in card

    # fresh runner: rendered files are gone, only the committed state DB remains
    import shutil
    shutil.rmtree(tmp_path / "output")
    monkeypatch.setattr(cli, "_cfg", lambda a: cfg)
    monkeypatch.setattr(cli, "make_publishers", lambda c, s: {"instagram": Pub()})
    with pytest.raises(FileNotFoundError):  # without the workflow's fetch step there is nothing to post
        cli.cmd_approve(argparse.Namespace(post_id=pid, dry_run=False))
    # the workflow's fetch step restores output/<post_id>/video.mp4 from the artifact
    (tmp_path / "output" / pid).mkdir(parents=True)
    (tmp_path / "output" / pid / "video.mp4").write_bytes(b"mp4")
    monkeypatch.chdir(tmp_path)
    cli.cmd_approve(argparse.Namespace(post_id=pid, dry_run=False))
    assert len(posted) == 1 and "Published" in summ.read_text()
    assert pipeline.open_store(cfg).get_post(pid)["status"] == "published"

    cfg["handle"] = "@yourhandle"
    with pytest.raises(SystemExit, match="placeholder"):
        cli.cmd_approve(argparse.Namespace(post_id=pid, dry_run=False))
