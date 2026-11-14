# ruff: noqa: F811  (pytest fixtures are imported, then named as parameters)
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The probe and the whole fetch path, for the incidents probes run into.

The stories run through `engine.probe` and `fetch.fetch_video` with a
scripted yt-dlp; nothing touches the network. tests/test_download_engine.py
and test_download_fetch.py hold the cases that needed no new test.
"""
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.exceptions import (
    BotDetectionError, DownloadError, ExtractorMissingError, VideoUnavailableError,
)
from backend.download import cookies as cookies_mod
from backend.download import engine, fetch
from backend.download.classify import ErrorClass as E
from backend.download.cookies import CookieStore
from tests.download_fakes import jar_text, ytdlp  # noqa: F401  (fixture)

YT = "https://www.youtube.com/watch?v=abc"
ODD = "https://odd.example/v/1"
HINT = "Use --cookies-from-browser or --cookies for the authentication."
AGE = f"ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users. {HINT}"
BOT = f"ERROR: [youtube] abc: Sign in to confirm you're not a bot. {HINT}"
UNSUPPORTED = f"ERROR: Unsupported URL: {ODD}"


class World:
    """What sits around yt-dlp in a fetch: the link fixer, toasts."""
    def __init__(self):
        self.fix_calls: list[str] = []
        self.fix_to: str | None = None
        self.toasts: list[str] = []


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

    async def not_cancelled(job_id):
        return False

    class _NoDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **kw):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    monkeypatch.setattr("backend.core.ai_retry.ai_fix_url", fake_fix)
    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send_constraint_warning", fake_warning)
    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send", fake_send)
    monkeypatch.setattr("backend.download.progress.job_is_cancelled", not_cancelled)
    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _NoDB())
    monkeypatch.setattr(fetch, "settings", SimpleNamespace(
        TMP_DIR=tmp_path / "tmp", VIDEOS_DIR=tmp_path, AUDIO_DIR=tmp_path))
    return w


def no_browser(monkeypatch, tmp_path, ytdlp):  # noqa: F811
    store = CookieStore(tmp_path / "cookies2" / "browser_cookies.txt",
                        extract=lambda browser, dest: dest.write_text(jar_text()),
                        detect=lambda: None, diagnose=lambda b: "error", disabled=lambda: False)
    monkeypatch.setattr(cookies_mod, "_store", store)
    ytdlp.store = store


async def get_video(tmp_path, url=YT):
    return await fetch.fetch_video(url, output_dir=tmp_path, filename="vid1", extract_audio=False)


# ── What the probe is certain about ──────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "ERROR: [BiliBili] BV1: HTTP Error 412: Precondition Failed",          # Bilibili's signature rejection
    "ERROR: Cloudflare challenge — checking your browser before access",
])
@pytest.mark.asyncio
async def test_a_challenge_at_the_probe_asks_for_a_browser_and_is_never_called_unavailable(ytdlp, text):
    ytdlp.probe_script = [text]
    with pytest.raises(ExtractorMissingError) as err:
        await engine.probe(YT, max_duration_s=7200)
    assert not isinstance(err.value, VideoUnavailableError)
    assert err.value.error_class is E.ANTI_BOT_CHALLENGE
    assert len(ytdlp.probe_calls) == 1


@pytest.mark.parametrize("text,sentence", [
    ("ERROR: This video is private", "This video is private."),
    ("ERROR: This video has been removed by the uploader", "This video has been removed."),
    ("ERROR: Video unavailable. This contains copyright claim content.", "This video was taken down for copyright."),
])
@pytest.mark.asyncio
async def test_a_video_that_is_gone_ends_at_the_probe_in_plain_words(ytdlp, text, sentence):
    ytdlp.probe_script = [text]
    with pytest.raises(VideoUnavailableError) as err:
        await engine.probe(YT, max_duration_s=7200)
    assert str(err.value) == sentence
    assert len(ytdlp.probe_calls) == 1


@pytest.mark.asyncio
async def test_an_unsupported_site_is_never_reported_as_video_unavailable(ytdlp):
    # 2026-05-19 to 2026-07-28: "unsupported url" was on the permanent list and
    # checked first, so these sites said "Video is unavailable" and the
    # generic extractor was never tried for exactly the sites it reads. The old test
    # accepted DownloadError, which VideoUnavailableError subclasses, and passed
    # for two months. Keep both assertions exact.
    ytdlp.probe_script = [UNSUPPORTED, UNSUPPORTED]
    with pytest.raises(ExtractorMissingError) as err:
        await engine.probe(ODD, max_duration_s=7200)
    assert type(err.value) is ExtractorMissingError
    assert not issubclass(VideoUnavailableError, ExtractorMissingError)
    assert err.value.error_class is E.UNSUPPORTED_URL
    assert ytdlp.probe_calls[1]["force_generic_extractor"] is True


# ── A probe that never answers ───────────────────────────────────────────────

@pytest.fixture
def hung_probe(ytdlp, monkeypatch):
    """Makes metadata calls hang until released. `only_generic` hangs just the
    generic-extractor re-probe."""
    import yt_dlp
    release = threading.Event()
    state = SimpleNamespace(only_generic=False, release=release)
    scripted = yt_dlp.YoutubeDL

    class Hanging(scripted):
        def extract_info(self, url, download=False):
            if not download and (not state.only_generic or self.opts.get("force_generic_extractor")):
                ytdlp.probe_calls.append(dict(self.opts))
                release.wait(10)
                return {"id": "late"}
            return super().extract_info(url, download=download)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Hanging)
    yield state
    release.set()


@pytest.mark.asyncio
async def test_a_probe_that_never_answers_fails_in_words_and_is_not_a_verdict_on_the_video(world, hung_probe, tmp_path, monkeypatch):
    # YouTube's player-client cascade can outlast the probe's cap. That is a
    # timeout, not "unavailable", and nothing downloads behind a hung probe.
    monkeypatch.setattr(engine, "PROBE_TIMEOUT_S", 0.2)
    world.fix_to = "https://other.example/x"
    with pytest.raises(DownloadError) as err:
        await get_video(tmp_path)
    assert not isinstance(err.value, (VideoUnavailableError, ExtractorMissingError))
    assert "didn't answer in 0.2 seconds" in str(err.value) and "YouTube" in str(err.value)
    assert err.value.error_class is E.TRANSIENT_NETWORK
    assert world.ytdlp.calls == [] and world.fix_calls == []


@pytest.mark.asyncio
async def test_a_generic_probe_that_never_answers_ends_in_words(world, hung_probe, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "GENERIC_PROBE_TIMEOUT_S", 0.2)
    hung_probe.only_generic = True
    world.ytdlp.probe_script = [UNSUPPORTED]
    world.fix_to = None
    with pytest.raises(ExtractorMissingError) as err:
        await get_video(tmp_path, url=ODD)
    assert err.value.error_class is E.UNSUPPORTED_URL and world.ytdlp.calls == []


# ── The generic extractor carries into the download ──────────────────────────

@pytest.mark.asyncio
async def test_a_site_only_the_generic_extractor_reads_is_downloaded_with_it(world, tmp_path):
    world.ytdlp.probe_script = [UNSUPPORTED, "ok"]
    result = await get_video(tmp_path, url=ODD)
    assert Path(result["video_path"]).exists()
    assert world.ytdlp.calls and all(c.get("force_generic_extractor") for c in world.ytdlp.calls)
    assert world.fix_calls == []


# ── The age gate (2026-11-05) ────────────────────────────────────────────────
# "Sign in to confirm your age" was permanent by phrase and the probe checked
# permanent first, so a stale jar never got its refresh.

@pytest.mark.asyncio
async def test_an_age_gate_met_with_a_stale_jar_gets_its_refresh_and_the_download_works(world, tmp_path):
    world.ytdlp.probe_script = [AGE]
    world.ytdlp.script = [AGE, "ok"]
    result = await get_video(tmp_path)
    assert Path(result["video_path"]).exists()
    assert world.ytdlp.browser_reads == 2                   # the first jar, then exactly one refresh
    assert len(world.ytdlp.probe_calls) == 1 and len(world.ytdlp.calls) == 2


@pytest.mark.asyncio
async def test_an_age_gate_the_refresh_does_not_open_ends_once_as_unavailable(world, tmp_path):
    world.ytdlp.probe_script = [AGE]
    world.ytdlp.script = [AGE]
    with pytest.raises(VideoUnavailableError) as err:
        await get_video(tmp_path)
    assert str(err.value) == "This video is age-restricted and needs a signed-in account."
    assert world.ytdlp.browser_reads == 2
    assert len(world.ytdlp.calls) == 2                      # refreshed once, one more try, no loop
    assert world.fix_calls == []


@pytest.mark.asyncio
async def test_an_age_gate_with_no_browser_to_refresh_from_says_an_account_is_needed(world, tmp_path, monkeypatch):
    # Was BotDetectionError ("sign in in Chrome"). The table ends the age gate
    # as unavailable; the sentence still says a signed-in account is what's missing.
    no_browser(monkeypatch, tmp_path, world.ytdlp)
    world.ytdlp.probe_script = [AGE]
    world.ytdlp.script = [AGE]
    with pytest.raises(VideoUnavailableError) as err:
        await get_video(tmp_path)
    assert "needs a signed-in account" in str(err.value)
    assert len(world.ytdlp.calls) == 1


@pytest.mark.parametrize("gone,sentence", [
    (f"ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video. {HINT}",
     "This video is private."),
    (f"ERROR: [youtube] abc: Join this channel to get access to members-only content. {HINT}",
     "This video is members-only."),
    ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader",
     "This video has been removed."),
    ("ERROR: [youtube] abc: This video is age-restricted",
     "This video is age-restricted and needs a signed-in account."),
])
@pytest.mark.asyncio
async def test_a_video_that_is_really_gone_keeps_its_message_and_costs_no_refresh(world, tmp_path, gone, sentence):
    # yt-dlp appends the cookie hint to private and members-only videos too:
    # they must not be read as a sign-in wall worth a refresh.
    world.ytdlp.probe_script = [gone]
    with pytest.raises(VideoUnavailableError) as err:
        await get_video(tmp_path)
    assert str(err.value) == sentence
    assert world.ytdlp.calls == []                          # the probe answered: no transfer attempt
    assert world.ytdlp.browser_reads == 1                   # the first jar only
    assert world.fix_calls == []


@pytest.mark.asyncio
async def test_a_bot_wall_a_fresh_jar_cannot_pass_ends_after_one_refresh(world, tmp_path):
    # Not minutes of backoff across the format rungs.
    world.ytdlp.probe_script = [BOT]
    world.ytdlp.script = [BOT]
    with pytest.raises(BotDetectionError):
        await get_video(tmp_path)
    assert world.ytdlp.browser_reads == 2 and len(world.ytdlp.calls) == 2
    assert world.ytdlp.waits == [3.0]


# ── The probe's certain answers ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_live_stream_never_starts_a_transfer(world, tmp_path):
    world.ytdlp.info = {"id": "x", "is_live": True}
    with pytest.raises(VideoUnavailableError, match="Live"):
        await get_video(tmp_path)
    assert world.ytdlp.calls == []


@pytest.mark.asyncio
async def test_a_video_longer_than_two_hours_is_refused_before_a_transfer(world, tmp_path):
    world.ytdlp.info = {**world.ytdlp.info, "duration": 3 * 3600}
    with pytest.raises(VideoUnavailableError, match="hours long"):
        await get_video(tmp_path)
    assert world.ytdlp.calls == []
