import pytest


@pytest.fixture(autouse=True)
def _no_live_research(monkeypatch):
    """Keep the suite hermetic: research providers skip themselves when these keys are absent."""
    for k in ("APIFY_TOKEN", "YOUTUBE_API_KEY", "OPENAI_API_KEY", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
