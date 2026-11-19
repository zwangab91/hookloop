# hookloop

A daily agent that runs the "AI content creator" playbook end to end: it finds what's hot,
studies the openings of 1M+ view videos, writes an original ≤45-second script on a proven hook,
renders a vertical video, and publishes it to TikTok, Instagram Reels and YouTube Shorts. Then it
learns from its own results.

```
 ┌─ 1 topic ──► 2 hashtag search ──► 3 keep 1M+ ──► 4 study first 10s ─┐
 │  (rotates     TikTok · IG · YT      views          → hook library     │
 │   daily)                                                              ▼
 7 growth ◄── 6 publish daily ◄────── 5 write ≤45s script ─► render MP4
 (bandit over   TikTok · Reels ·        (critic loop,         (TTS, karaoke
  hook types,   Shorts · webhook         originality guard)    captions)
  10k alert)
```

## The playbook → code

| # | Playbook step | What the agent does | Code |
|---|---|---|---|
| 1 | Pick a hot topic | Claude web-searches what's trending in your niche, picks one subtopic, returns hashtags + sourced facts | `steps/topic.py` |
| 2 | Search the topic's hashtag | TikTok + Instagram via Apify scrapers, YouTube Shorts via Data API | `steps/research.py`, `providers/sources.py` |
| 3 | Watch videos with 1M+ views | Filters by views (default 1,000,000), recency, ≤90s; falls back to top-N if nothing clears the bar | `steps/research.py` |
| 4 | Copy the first 10 seconds | Downloads only the first 10s, transcribes it, grabs 4 frames, and Claude (vision) abstracts the hook into a reusable template + beat structure | `steps/hooks.py` |
| 5 | Keep it ≤45 seconds | Word budget, then real duration check after TTS (speed up ≤1.2× or drop a beat) | `steps/script.py`, `steps/render.py` |
| 6 | Post daily, rotate topic | Daily GitHub Actions cron; a topic can't repeat inside `rotation_days`; publishes to every configured surface | `steps/publish.py`, `.github/workflows/daily.yml` |
| 7 | Monetize at 10k | Pulls followers + views, Thompson-samples which hook types beat your median, writes `media_kit.md` and pings you when you cross 10k | `steps/growth.py` |

"Copy the first 10 seconds" is implemented as copying the **structure** (hook type, beats,
pacing, on-screen pattern), never the creator's words: templates are abstracted, and a trigram
overlap guard blocks any script that's too close to a source opening.

## Try it now (no API keys)

```bash
pip install -e ".[kokoro]"       # needs ffmpeg on PATH
hookloop demo --handle @you       # runs the whole loop on bundled fixtures, renders a real video
open demo_output/*/video.mp4
```

The demo uses synthetic research rows and a canned script, a free local voice (Kokoro), and
publishing in dry-run mode. Everything else is the production code path.

## Go live

1. `cp config.example.yaml config.yaml` and set `niche`, `handle`, and the topic pool.
2. Get the keys in `.env.example`. Run `hookloop doctor` to see what's missing.
3. Mint platform tokens once: `hookloop auth tiktok`, `hookloop auth youtube`, `hookloop auth instagram`.
4. `hookloop keygen` → `HOOKLOOP_STATE_KEY` (encrypts rotated OAuth tokens in the committed state DB).
5. `hookloop run --dry-run` locally, watch `output/<date>-<topic>/video.mp4`.
6. Push to a **private** GitHub repo, add the env vars as Actions secrets, and the workflow runs daily.

Want to see each video before it goes out? Set `publish.mode: review`. The run renders, pings
Slack, and waits. Approve with `hookloop approve <post_id>`, or from GitHub: Actions → hookloop
daily → Run workflow → `approve: <post_id>`.

## Platform rules to know (checked Nov 2026)

| Surface | Fully automatic on day 1? | Notes |
|---|---|---|
| Instagram Reels | **Yes** | Professional account + your own Meta app in dev mode with you as tester. Resumable upload, so there's no public video URL to host. |
| TikTok | Private only until audit | Unaudited Direct Post apps can only post `SELF_ONLY`. hookloop detects this, posts private, and warns. `method: inbox` sends each video to your TikTok drafts instead, so posting takes one tap. Apply for the Direct Post audit to go fully automatic. |
| YouTube Shorts | Private only until audit | Uploads from unaudited Google Cloud projects are locked private. Submit the YouTube API Services audit form. `containsSyntheticMedia` is set to disclose the AI voice. |
| Anything else | via webhook | `publish.platforms.webhook` POSTs the MP4 + caption to n8n/Make, which can fan out to X, LinkedIn, Threads, etc. |

Label AI-generated content where a platform requires it. On TikTok, add the AIGC flag through
`publish.platforms.tiktok.post_info_extra`, following TikTok's current field name.

## Make it yours

- **Your voice**: `tts.provider: elevenlabs` with a clone of your voice (`voice_id`). `openai` (gpt-4o-mini-tts) is another option.
- **Look**: `render.theme` colors, `render.fonts`, optional `render.music` loop (use royalty-free audio).
- **Brain**: `llm.model` / `llm.fast_model`; the critic runs on the fast model.
- **Scrapers**: Apify actor IDs and inputs live in config, so if an actor changes you can swap it without touching code.

## Commands

```
hookloop run [--dry-run] [--offline]   full daily loop
hookloop demo                          offline end-to-end demo
hookloop approve <post_id>             publish a reviewed post / retry failed platforms
hookloop pending                       posts waiting for review
hookloop hooks                         the hook library (what 1M+ videos taught it)
hookloop metrics                       followers + post stats, 10k milestone check
hookloop mediakit                      sponsorship one-pager from current stats
hookloop render script.json -o dir     render any script
hookloop auth tiktok|youtube|instagram mint OAuth tokens
hookloop doctor | keygen
```

## Tests

`pip install -e ".[dev]" && pytest`: 21 tests. They cover topic rotation, the 1M filter,
script rules and the originality guard, the critic revision, the hook bandit, the milestone,
token encryption, the Claude tool-loop (web search pause/resume), and each publisher's request
flow against mocked APIs, including TikTok's unaudited fallback, Instagram's resumable upload,
and YouTube's resumable upload with the private-lock warning.
