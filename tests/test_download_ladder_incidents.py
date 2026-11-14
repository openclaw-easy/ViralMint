# ruff: noqa: F811  (pytest fixtures are imported, then named as parameters)
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The retry ladder's incidents, re-pinned against the rebuilt engine.

These pin incidents the old single-function ladder
(`ytdlp_service.download_video`, now deleted) ran into. Only what
tests/test_download_engine.py does not already pin lives here.

Every expected step count is read off the table in backend/download/policy.py;
where the old ladder's number mattered, the comment says what it was.
"""
import os
from pathlib import Path

import pytest
import yt_dlp

from backend.core.exceptions import (
    BotDetectionError, DownloadError, ExtractorMissingError,
)
from backend.download import cookies as cookies_mod
from backend.download import engine, health, opts, outputs
from backend.download.classify import ErrorClass as E
from backend.download.classify import classify
from backend.download.cookies import CookieState, CookieStore
from tests.download_fakes import VIDEO_BYTES, jar_text, ytdlp  # noqa: F401  (fixture)

URL = "https://www.youtube.com/watch?v=abc"
COOKIE_DB = 'ERROR: could not find chrome cookies database in "/Users/x/Library/Application Support/Google/Chrome"'
E403 = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
NET = "ERROR: [youtube] abc: Unable to download webpage: [Errno 104] Connection reset by peer"
BOT = "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies for the authentication."
PRIVATE = "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video."
E429 = "ERROR: [youtube] abc: Unable to download webpage: HTTP Error 429: Too Many Requests"
CLOUDFLARE = "ERROR: [generic] 9: Checking your browser. Cloudflare challenge in progress."
UNSUPPORTED = "ERROR: Unsupported URL: https://www.youtube.com/watch?v=abc"
UNKNOWN = "ERROR: [youtube] abc: some brand-new yt-dlp wording we've never seen"
NO_FORMATS = "ERROR: [youtube] abc: No video formats found"
MERGE = "ERROR: unable to rename file: [Errno 2] No such file or directory: 'vid1.f398.mp4.part'"
PP = ("ERROR: Postprocessing: Supported filetypes for thumbnail embedding are: "
      "mp3, mkv/mka, ogg/opus/flac, m4a/mp4/m4v/mov")


def video(tmp_path, **kw):
    base = dict(url=URL, output_dir=tmp_path, stem="vid1", extract_audio=False)
    base.update(kw)
    return engine.Request(**base)



def leftovers(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if p.is_file())


def use_store(monkeypatch, tmp_path, ytdlp, *, detect="chrome", disabled=False):  # noqa: F811
    """Swap the scripted store for one with no browser, or with cookies off."""
    def fake_browser(browser, dest):
        ytdlp.browser_reads += 1
        dest.write_text(jar_text())
    store = CookieStore(tmp_path / "cookies2" / "browser_cookies.txt", extract=fake_browser,
                        detect=lambda: detect, diagnose=lambda b: "no_access",
                        disabled=lambda: disabled)
    monkeypatch.setattr(cookies_mod, "_store", store)
    ytdlp.store = store
    return store


def embed_keys(call):
    return [pp["key"] for pp in call.get("postprocessors") or []]


# ── Merge / rename ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_merge_failure_nothing_can_merge_retries_twice_then_walks_the_formats(ytdlp, tmp_path, monkeypatch):
    # Was 5 attempts on each of 3 formats (15). The table: 8 s, 20 s on the
    # same format, then one try on each remaining format, the hand merge
    # offered every time.
    merges = []
    monkeypatch.setattr(outputs, "try_manual_merge", lambda d, stem: merges.append(stem))
    ytdlp.script = [MERGE]
    with pytest.raises(DownloadError) as err:
        await engine.run(video(tmp_path))
    assert not isinstance(err.value, yt_dlp.utils.DownloadError)
    assert err.value.error_class is E.MERGE_RENAME
    assert ytdlp.waits == [8.0, 20.0] and len(ytdlp.calls) == 5
    formats = [c["format"] for c in ytdlp.calls]
    assert formats[0] == formats[1] == formats[2] and len(set(formats)) == 3
    assert merges == ["vid1"] * 5
    assert ytdlp.streak == []                               # a local failure says nothing about yt-dlp


@pytest.mark.asyncio
async def test_2026_10_25_a_hand_merge_that_is_not_a_video_is_never_a_success_or_a_raw_error(ytdlp, tmp_path, monkeypatch):
    # Audit 2026-10-25 (3): a merged file that failed validation escaped as a raw yt-dlp error.
    def tiny_merge(output_dir, stem):
        out = Path(output_dir) / f"{stem}.mp4"
        out.write_bytes(b"\x00" * 100)
        return out
    monkeypatch.setattr(outputs, "try_manual_merge", tiny_merge)
    ytdlp.script = [MERGE]
    with pytest.raises(DownloadError) as err:
        await engine.run(video(tmp_path))
    assert not isinstance(err.value, yt_dlp.utils.DownloadError)
    assert "Traceback" not in str(err.value)
    assert leftovers(tmp_path) == []                        # the unusable merge is not left behind


# ── Sign-in walls ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_bot_check_caused_by_stale_cookies_is_fixed_by_one_refresh(ytdlp, tmp_path):
    ytdlp.script = [BOT, "ok"]
    result = await engine.run(video(tmp_path))
    assert Path(result["video_path"]).exists()
    assert len(ytdlp.calls) == 2 and ytdlp.waits == [3.0]
    assert ytdlp.browser_reads == 2                         # the first jar, then the refresh
    assert "cookiefile" in ytdlp.calls[1]


@pytest.mark.asyncio
async def test_a_bot_check_with_no_browser_to_refresh_from_ends_at_once(ytdlp, tmp_path, monkeypatch):
    use_store(monkeypatch, tmp_path, ytdlp, detect=None)
    ytdlp.script = [BOT]
    with pytest.raises(BotDetectionError):
        await engine.run(video(tmp_path))
    assert len(ytdlp.calls) == 1 and ytdlp.waits == [] and ytdlp.browser_reads == 0



@pytest.mark.asyncio
async def test_a_site_that_keeps_asking_for_sign_in_reads_the_browser_once_not_once_per_download(ytdlp, tmp_path):
    # Each forced read can raise a macOS Keychain prompt outside the app.
    ytdlp.script = [BOT]
    with pytest.raises(BotDetectionError):
        await engine.run(video(tmp_path))
    assert ytdlp.browser_reads == 2 and len(ytdlp.calls) == 2
    for stem in ("vid2", "vid3"):
        with pytest.raises(BotDetectionError):
            await engine.run(video(tmp_path, stem=stem))
    assert ytdlp.browser_reads == 2                         # inside the refresh cooldown: no new prompt
    assert len(ytdlp.calls) == 4                            # and no pointless second try either


@pytest.mark.asyncio
async def test_a_cancel_before_the_403_refresh_never_reads_the_browser_again(ytdlp, tmp_path):
    ytdlp.script = [E403]
    ytdlp.cancel_after_attempt = 1
    with pytest.raises(DownloadError):
        await engine.run(video(tmp_path, job_id="job-1"))
    assert ytdlp.browser_reads == 1 and len(ytdlp.calls) == 1 and ytdlp.waits == []


# ── HTTP 403 without cookies ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_403_without_cookies_gets_one_wait_and_a_passing_one_downloads(ytdlp, tmp_path, monkeypatch):
    use_store(monkeypatch, tmp_path, ytdlp, disabled=True)
    ytdlp.script = [E403, "ok"]
    result = await engine.run(video(tmp_path))
    assert Path(result["video_path"]).exists()
    assert ytdlp.waits == [8.0] and all("cookiefile" not in c for c in ytdlp.calls)


@pytest.mark.asyncio
async def test_a_403_without_cookies_that_stays_escalates_after_two_tries(ytdlp, tmp_path, monkeypatch):
    # Was 5 backoff attempts on the rung (and before 2026-10-25, on every rung).
    use_store(monkeypatch, tmp_path, ytdlp, disabled=True)
    ytdlp.script = [E403]
    with pytest.raises(ExtractorMissingError, match="403"):
        await engine.run(video(tmp_path))
    assert len(ytdlp.calls) == 2 and ytdlp.waits == [8.0] and ytdlp.browser_reads == 0


# ── Optional postprocessors ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_embed_extras_are_dropped_once_then_the_error_is_treated_like_any_other(ytdlp, tmp_path):
    # If the source keeps failing after the extras are gone the error is not
    # ours: one 8 s retry per format, down the chain, then a failure in words.
    ytdlp.script = [PP]
    with pytest.raises(DownloadError) as err:
        await engine.run(video(tmp_path, options={"thumbnail": True}))
    assert not isinstance(err.value, yt_dlp.utils.DownloadError)
    with_extras = ["EmbedThumbnail" in embed_keys(c) for c in ytdlp.calls]
    assert with_extras == [True] + [False] * 6
    assert ytdlp.waits == [8.0, 8.0, 8.0]
    assert len({c["format"] for c in ytdlp.calls}) == 3


@pytest.mark.asyncio
async def test_a_postprocessing_error_with_no_extras_to_drop_is_retried_and_reports_nothing_dropped(ytdlp, tmp_path):
    ytdlp.script = [PP, "ok"]
    result = await engine.run(video(tmp_path))
    assert result["option_postprocessors_dropped"] is False
    assert ytdlp.waits == [8.0] and ytdlp.calls[0]["format"] == ytdlp.calls[1]["format"]


# ── Network, challenge, formats, unknown ─────────────────────────────────────

@pytest.mark.asyncio
async def test_a_network_error_never_changes_the_format(ytdlp, tmp_path):
    # Was 5 attempts on the first format. A different format doesn't fix a network.
    ytdlp.script = [NET]
    with pytest.raises(DownloadError):
        await engine.run(video(tmp_path))
    assert len(ytdlp.calls) == 4 and len({c["format"] for c in ytdlp.calls}) == 1


@pytest.mark.asyncio
async def test_a_challenge_page_met_during_the_download_gets_one_retry_then_asks_for_a_browser(ytdlp, tmp_path):
    # Was up to 15 attempts before the end of the ladder escalated.
    ytdlp.script = [CLOUDFLARE]
    with pytest.raises(ExtractorMissingError) as err:
        await engine.run(video(tmp_path, url="https://example.com/v/9"))
    assert err.value.error_class is E.ANTI_BOT_CHALLENGE
    assert len(ytdlp.calls) == 2 and ytdlp.waits == [8.0]


@pytest.mark.asyncio
async def test_no_formats_on_every_rung_asks_for_a_browser_without_waiting(ytdlp, tmp_path):
    ytdlp.script = [NO_FORMATS]
    with pytest.raises(ExtractorMissingError) as err:
        await engine.run(video(tmp_path))
    assert err.value.error_class is E.NO_FORMATS
    assert len(ytdlp.calls) == 3 and ytdlp.waits == []
    assert len({c["format"] for c in ytdlp.calls}) == 3


@pytest.mark.asyncio
async def test_an_unknown_error_that_clears_on_the_last_format_still_downloads(ytdlp, tmp_path):
    ytdlp.script = [UNKNOWN] * 4 + ["ok"]
    result = await engine.run(video(tmp_path))
    assert Path(result["video_path"]).exists()
    assert ytdlp.waits == [8.0, 8.0] and len({c["format"] for c in ytdlp.calls}) == 3
    assert ytdlp.streak == []                               # it worked: nothing to count


@pytest.mark.parametrize("wording", [
    "ERROR: Unable to download subtitle 'en': HTTP Error 429",
    "ERROR: HTTP Error 429: Too Many Requests on subtitle download",
])
@pytest.mark.asyncio
async def test_either_subtitle_429_wording_drops_subtitles_and_keeps_the_video(ytdlp, tmp_path, wording):
    ytdlp.script = [wording, "ok"]
    result = await engine.run(video(tmp_path))
    assert Path(result["video_path"]).exists()
    assert ytdlp.calls[1]["writesubtitles"] is False and ytdlp.waits == []
    assert health.cooldown_for(URL) == 0


# ── Output checks (audit 2026-10-25) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_2026_10_25_the_next_format_rescues_an_undersized_first_one(ytdlp, tmp_path):
    def tiny_then_real(o):
        size = 1024 if len(ytdlp.calls) == 1 else VIDEO_BYTES
        Path(o["outtmpl"].replace(".%(ext)s", ".mp4")).write_bytes(b"x" * size)
    ytdlp.on_ok = tiny_then_real
    result = await engine.run(video(tmp_path))
    assert Path(result["video_path"]).stat().st_size == VIDEO_BYTES
    assert len(ytdlp.calls) == 2 and ytdlp.calls[0]["format"] != ytdlp.calls[1]["format"]
    assert ytdlp.waits == []


@pytest.mark.asyncio
async def test_2026_10_25_a_success_that_left_no_file_is_a_failure(ytdlp, tmp_path):
    make = video
    # yt-dlp returned normally and nothing is on disk: callers save a Library
    # row on the returned path, so this must never be a result.
    ytdlp.on_ok = lambda o: None
    with pytest.raises(DownloadError) as err:
        await engine.run(make(tmp_path))
    assert not isinstance(err.value, yt_dlp.utils.DownloadError)
    assert "Traceback" not in str(err.value) and str(tmp_path) not in str(err.value)
    assert len(ytdlp.calls) <= 6                            # inside the budget, whichever row it took
    assert leftovers(tmp_path) == []
    assert ytdlp.streak == []                               # ours, not yt-dlp's


def test_the_video_length_limit_is_two_hours():
    assert engine.VIDEO_MAX_DURATION_S == 7200


# ── Unreadable browser cookies (2026-11-12) ──────────────────────────────────
# A desktop sat at 0% for 7+ minutes: the packaged app could not
# list Chrome's profile folder, and every attempt failed before any request.

@pytest.mark.parametrize("msg", [
    COOKIE_DB,
    "could not find firefox cookies database in /Users/x/Library/Application Support/Firefox",
    "could not find safari cookies database",
    "Could not copy Chrome cookie database. See  https://github.com/yt-dlp/yt-dlp/issues/7271",
    'unsupported browser specified for cookies: "netscape"',
    "unsupported keyring: kwallet9",
    "[Errno 1] Operation not permitted: '/Users/x/Library/Cookies/Cookies.binarycookies'",
])
def test_local_cookie_failures_are_recognised(msg):
    assert classify(msg) is E.COOKIE_SOURCE


@pytest.mark.parametrize("msg", [
    "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser",
    "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    "Unable to download webpage: The read operation timed out",
    "[Errno 13] Permission denied: '/Users/x/ViralMint/storage/videos/a.mp4'",
    "ERROR: [youtube] abc: Video unavailable",
])
def test_site_answers_are_not_cookie_failures(msg):
    assert classify(msg) is not E.COOKIE_SOURCE


@pytest.mark.skipif(os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs a non-root POSIX user for an unreadable folder")
def test_a_folder_macos_wont_list_reads_as_no_access(tmp_path, monkeypatch):
    chrome = tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
    chrome.mkdir(parents=True)
    monkeypatch.setattr(cookies_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cookies_mod.Path, "home", classmethod(lambda cls: tmp_path))
    os.chmod(chrome, 0)
    try:
        assert cookies_mod.diagnose_access("chrome") == "no_access"
    finally:
        os.chmod(chrome, 0o755)
    assert cookies_mod.diagnose_access("firefox") == "not_found"


def test_a_cookie_failure_outside_the_ladder_is_worded_and_is_not_a_sign_in_wall(ytdlp):
    # A listing or a subtitle fetch that ends on our own cookie read.
    ytdlp.store.mark_unreadable(Exception(COOKIE_DB))
    err = engine.terminal_exception(yt_dlp.utils.DownloadError(COOKIE_DB), "https://youtu.be/tjfaAsbaayc")
    assert isinstance(err, DownloadError) and not isinstance(err, BotDetectionError)
    assert not isinstance(err, yt_dlp.utils.DownloadError)
    assert "couldn't read your browser's cookies" in str(err)
    assert "Full Disk Access" in str(err)
    assert ytdlp.streak == []                               # our failure never feeds the update / rollback streak


def test_an_attempt_is_never_handed_a_live_browser_read_when_the_jar_cannot_be_made(ytdlp):
    # The shipped build fell back to `cookiesfrombrowser`, which failed the
    # same way on every call.
    ytdlp.browser_error = Exception(COOKIE_DB)
    jar = ytdlp.store.jar_for_attempt()
    assert jar is None and ytdlp.store.status().state is CookieState.UNREADABLE
    built = opts.build(URL, jar=jar)
    assert "cookiesfrombrowser" not in built and "cookiefile" not in built
    assert ytdlp.store.jar_for_attempt() is None and ytdlp.browser_reads == 1      # not asked again



@pytest.mark.asyncio
async def test_a_refresh_while_the_browser_is_unreadable_keeps_the_jar_and_reads_nothing(ytdlp, tmp_path):
    ytdlp.store.jar_for_attempt()                           # a good jar from before
    before = ytdlp.store.jar_path.read_text()
    ytdlp.store.mark_unreadable(Exception(COOKIE_DB))
    ytdlp.script = [E403, "ok"]
    await engine.run(video(tmp_path))
    assert ytdlp.browser_reads == 1                         # the 403's cookie step did not ask the browser
    assert ytdlp.store.jar_path.read_text() == before
    assert "cookiefile" not in ytdlp.calls[1]               # it went straight to "without cookies"

