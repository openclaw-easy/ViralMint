# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""fetch_video: the whole path a caller gets. yt-dlp and the AI link fix are
scripted; nothing touches the network."""
from pathlib import Path

import pytest

from backend.core.exceptions import (
    BotDetectionError, DownloadError, DownloadStalledError,
    ExtractorMissingError, RateLimitError, VideoUnavailableError,
)
from backend.download import engine, fetch, health
from tests.download_fakes import ytdlp  # noqa: F401  (fixture)

pytestmark = pytest.mark.asyncio

URL = "https://some-site.example/watch/9"
YT = "https://www.youtube.com/watch?v=abc"
UNSUPPORTED = "ERROR: Unsupported URL: https://some-site.example/watch/9"
CLOUDFLARE = "ERROR: [generic] 9: Checking your browser. Cloudflare challenge in progress."
NET = "ERROR: [generic] 9: Unable to download webpage: [Errno 104] Connection reset by peer"


class World:
    """The link fixer and the toasts, scripted."""
    def __init__(self):
        self.fix_calls: list[str] = []
        self.fix_to: str | None = None
        self.toasts: list[str] = []
        self.cancelled = False


@pytest.fixture
def world(monkeypatch, ytdlp, tmp_path):  # noqa: F811
    w = World()
    w.ytdlp = ytdlp

    async def fake_fix(url, error, user_settings=None):
        w.fix_calls.append(url)
        return w.fix_to

    async def fake_warning(constraint=None, message=None, severity="warning", user_id="local", **kw):
        w.toasts.append(constraint)

    async def fake_send(*a, **kw):
        return None

    async def cancelled(job_id):
        return w.cancelled

    monkeypatch.setattr("backend.core.ai_retry.ai_fix_url", fake_fix)
    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send_constraint_warning", fake_warning)
    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send", fake_send)
    monkeypatch.setattr("backend.download.progress.job_is_cancelled", cancelled)

    class _NoDB:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def execute(self, *a, **kw):
            class R:
                def scalar_one_or_none(self):
                    return None
            return R()
    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _NoDB())
    from types import SimpleNamespace
    monkeypatch.setattr(fetch, "settings", SimpleNamespace(
        TMP_DIR=tmp_path / "tmp", VIDEOS_DIR=tmp_path, AUDIO_DIR=tmp_path))
    return w


async def get_video(tmp_path, url=URL, **kw):
    return await fetch.fetch_video(url, output_dir=tmp_path, filename="vid1", extract_audio=False, **kw)


# ── The ordinary path ────────────────────────────────────────────────────────

async def test_a_normal_link_is_probed_then_downloaded_with_yt_dlp(world, tmp_path):
    result = await get_video(tmp_path, url=YT)
    assert result["url"] == YT
    assert Path(result["video_path"]).exists()
    assert len(world.ytdlp.probe_calls) == 1 and len(world.ytdlp.calls) == 1
    assert world.fix_calls == []


async def test_without_a_filename_each_download_gets_its_own_concrete_stem(world, tmp_path):
    a = await fetch.fetch_video(YT, output_dir=tmp_path, extract_audio=False)
    b = await fetch.fetch_video(YT, output_dir=tmp_path, extract_audio=False)
    assert a["video_path"] != b["video_path"] and "%" not in a["video_path"]


async def test_bad_arguments_are_refused_before_any_work(world, tmp_path):
    with pytest.raises(ValueError):
        await fetch.fetch_video("")
    assert world.ytdlp.probe_calls == []


# ── The host cooldown ────────────────────────────────────────────────────────

async def test_a_host_that_just_rate_limited_us_is_not_asked_again_and_the_person_is_told_when(world, tmp_path):
    health.record_rate_limit(YT)
    with pytest.raises(RateLimitError) as err:
        await get_video(tmp_path, url=YT)
    assert "Try again in about 1 minute" in str(err.value)
    assert world.ytdlp.probe_calls == [] and world.ytdlp.calls == [] and world.fix_calls == []
    await get_video(tmp_path, url="https://vimeo.com/1")            # another host is unaffected


# ── The AI link fix ──────────────────────────────────────────────────────────

async def test_a_wrapped_link_is_rewritten_once_and_the_result_names_the_link_used(world, tmp_path):
    world.fix_to = YT
    world.ytdlp.probe_script = [UNSUPPORTED, UNSUPPORTED, "ok"]      # site probe, generic probe, then the fixed link
    result = await get_video(tmp_path, url="https://some-site.example/watch/9")
    assert result["url"] == YT
    assert world.fix_calls == ["https://some-site.example/watch/9"]


async def test_the_link_fix_runs_at_most_once(world, tmp_path):
    world.fix_to = "https://other.example/x"
    world.ytdlp.probe_script = [UNSUPPORTED]
    with pytest.raises(ExtractorMissingError):
        await get_video(tmp_path)
    assert len(world.fix_calls) == 1


@pytest.mark.parametrize("probe_error,exc", [
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video.", VideoUnavailableError),
])
async def test_a_verdict_on_a_working_link_is_never_sent_to_the_link_fixer(world, tmp_path, probe_error, exc):
    world.fix_to = YT
    world.ytdlp.probe_script = [probe_error]
    with pytest.raises(exc):
        await get_video(tmp_path, url=YT)
    assert world.fix_calls == []


@pytest.mark.parametrize("script,exc", [
    (["ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser"], BotDetectionError),
    (["ERROR: [youtube] abc: Unable to download webpage: HTTP Error 429: Too Many Requests"], RateLimitError),
    ([NET], DownloadError),
    (['ERROR: could not find chrome cookies database in "/x"'], DownloadError),
])
async def test_sign_in_rate_limit_network_and_cookie_failures_never_reach_the_link_fixer(world, tmp_path, script, exc):
    world.fix_to = "https://other.example/x"
    world.ytdlp.script = script
    with pytest.raises(exc):
        await get_video(tmp_path, url=YT)
    assert world.fix_calls == []


async def test_an_abandoned_download_is_never_retried_by_the_fixer(world, tmp_path, monkeypatch):
    async def stalled(req, *, force_generic=False):
        raise DownloadStalledError("Download stalled: no data for 10 minutes (0.0 MB received).")
    monkeypatch.setattr(engine, "run", stalled)
    world.fix_to = "https://other.example/x"
    with pytest.raises(DownloadStalledError):
        await get_video(tmp_path, url=YT)
    assert world.fix_calls == []


async def test_a_fixer_that_returns_the_same_link_or_nothing_changes_nothing(world, tmp_path):
    world.ytdlp.probe_script = [UNSUPPORTED]
    for answer in (None, URL, "  " + URL + " "):
        world.fix_to = answer
        with pytest.raises(ExtractorMissingError):
            await get_video(tmp_path)
    assert len(world.ytdlp.probe_calls) == 6                         # two probes per try, never a third fetch


# ── The cookie notice ────────────────────────────────────────────────────────

async def test_the_person_is_told_once_when_downloads_ran_without_their_cookies(world, tmp_path):
    world.ytdlp.probe_script = ['ERROR: could not find chrome cookies database in "/x"', "ok"]
    await get_video(tmp_path, url=YT)
    await get_video(tmp_path, url=YT)
    assert world.toasts == ["browser_cookies_unreadable"]


async def test_escalation_ends_in_words_without_a_second_engine(world, tmp_path):
    # A challenge page: the ladder's last word is "escalate"; this build has
    # no browser fallback, so the person gets the class's sentence.
    world.ytdlp.probe_script = ["ok"]
    world.ytdlp.script = [CLOUDFLARE]
    with pytest.raises(ExtractorMissingError) as err:
        await get_video(tmp_path)
    assert "bot check" in str(err.value)
    assert len(world.ytdlp.calls) == 2 and world.fix_calls == []


async def test_fix_link_false_skips_the_link_fixer(world, tmp_path):
    world.fix_to = YT
    world.ytdlp.probe_script = [UNSUPPORTED]
    with pytest.raises(ExtractorMissingError):
        await get_video(tmp_path, fix_link=False)
    assert world.fix_calls == []
