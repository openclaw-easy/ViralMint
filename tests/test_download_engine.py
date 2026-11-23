# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The engine: the one attempt loop, driven by a scripted yt-dlp.

Each test names what a download meets and asserts what the person gets: the
exception type and sentence, how long the ladder waited, what it changed
between attempts, what is left on disk, and what fed the yt-dlp update streak.
"""
import asyncio
import json
import threading
from pathlib import Path

import pytest

from backend.core.exceptions import (
    BotDetectionError, DownloadError, DownloadStalledError, ExtractorMissingError,
    JobCancelledError, RateLimitError, VideoUnavailableError,
)
from backend.download import engine, health, opts, outputs, progress
from backend.download.classify import ErrorClass as E
from backend.download.cookies import CookieState
from backend.download.policy import Budget
from tests.download_fakes import VIDEO_BYTES, ytdlp  # noqa: F401  (fixture)

URL = "https://www.youtube.com/watch?v=abc"
CORPUS = json.loads((Path(__file__).parent / "fixtures" / "download_error_corpus.json").read_text())["rows"]
COOKIE_DB = 'ERROR: could not find chrome cookies database in "/Users/x/Library/Application Support/Google/Chrome"'
E403 = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
NET = "ERROR: [youtube] abc: Unable to download webpage: [Errno 104] Connection reset by peer"
BOT = "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies for the authentication."

pytestmark = pytest.mark.asyncio


def video(tmp_path, **kw):
    base = dict(url=URL, output_dir=tmp_path, stem="vid1", extract_audio=False)
    base.update(kw)
    return engine.Request(**base)



def leftovers(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if p.is_file())


# ── Success ──────────────────────────────────────────────────────────────────

async def test_a_clean_video_download_returns_the_file_and_its_metadata(ytdlp, tmp_path):
    result = await engine.run(video(tmp_path))
    assert Path(result["video_path"]) == tmp_path / "vid1.mp4"
    assert Path(result["video_path"]).stat().st_size == VIDEO_BYTES
    assert (result["title"], result["duration"], result["height"]) == ("A title", 42, 1080)
    assert result["category"] == "Music" and result["option_postprocessors_dropped"] is False
    assert len(ytdlp.calls) == 1 and ytdlp.waits == []
    first = ytdlp.calls[0]
    assert first["cookiefile"].endswith("browser_cookies.txt") and "cookiesfrombrowser" not in first
    assert first["format"].startswith("bestvideo[height<=1080]") and first["playlist_items"] == "1"



async def test_a_template_stem_is_refused_before_anything_runs(ytdlp, tmp_path):
    with pytest.raises(ValueError):
        await engine.run(video(tmp_path, stem="%(id)s"))
    assert ytdlp.calls == []


# ── The incidents this rebuild is for ────────────────────────────────────────

async def test_2026_11_12_unreadable_cookies_cost_no_waiting_and_the_video_downloads(ytdlp, tmp_path):
    # The packaged app can't read Chrome's profile: yt-dlp fails before any
    # request while a cookie source is set. Used to be 7+ minutes at 0%.
    ytdlp.on_ok = None
    def needs_no_cookies(opts):
        Path(opts["outtmpl"].replace(".%(ext)s", ".mp4")).write_bytes(b"x" * VIDEO_BYTES)
    ytdlp.script = [COOKIE_DB, "ok"]
    ytdlp.on_ok = needs_no_cookies
    result = await engine.run(video(tmp_path))
    assert result["video_path"].endswith("vid1.mp4")
    assert ytdlp.waits == [] and len(ytdlp.calls) == 2
    assert "cookiefile" not in ytdlp.calls[1]
    assert ytdlp.store.status().state is CookieState.UNREADABLE     # later downloads don't ask again
    assert ytdlp.streak == []


async def test_cookie_error_that_survives_dropping_cookies_says_so_in_words(ytdlp, tmp_path):
    ytdlp.script = [COOKIE_DB]
    with pytest.raises(DownloadError) as err:
        await engine.run(video(tmp_path))
    assert "couldn't read your browser's cookies" in str(err.value)
    assert "Full Disk Access" in str(err.value)
    assert len(ytdlp.calls) == 2 and ytdlp.waits == [] and ytdlp.streak == []


async def test_2026_10_25_a_403_that_survives_everything_escalates_after_three_recoveries(ytdlp, tmp_path):
    # Used to be 30 attempts and ~13 minutes of backoff across the format chain.
    ytdlp.script = [E403]
    with pytest.raises(ExtractorMissingError) as err:
        await engine.run(video(tmp_path))
    assert "refused the download (HTTP 403 Forbidden)" in str(err.value)
    assert err.value.error_class is E.HTTP_403
    assert len(ytdlp.calls) == 6 and ytdlp.waits == [8.0]   # still bounded: the four below, then one try on each remaining format, no more waiting
    assert ytdlp.browser_reads == 2                         # the first jar, then one refresh
    assert ["cookiefile" in c for c in ytdlp.calls] == [True, True, False, False, False, False]
    assert [c["format"] for c in ytdlp.calls] == [*[opts.FORMAT_CHAIN[0]] * 4, opts.FORMAT_CHAIN[1], opts.FORMAT_CHAIN[2]]


async def test_2026_11_20_a_403_on_the_separate_streams_downloads_on_the_single_stream_format(ytdlp, tmp_path):
    # youtube.com/shorts/CLco-qq94Lo: YouTube refused that video's separate https streams (403) and served its single HLS stream ("best").
    # The ladder used to stop before the last format, so the phone was told "needs signing in" for a video that downloads.
    ytdlp.script = [E403, E403, E403, E403, E403, "ok"]
    result = await engine.run(video(tmp_path))
    assert result["video_path"] and ytdlp.calls[-1]["format"] == opts.FORMAT_CHAIN[-1] and ytdlp.waits == [8.0]


async def test_2026_07_28_a_403_caused_by_the_cookies_is_fixed_by_dropping_them(ytdlp, tmp_path):
    ytdlp.script = [E403, E403, "ok"]
    result = await engine.run(video(tmp_path))
    assert result["video_path"].endswith("vid1.mp4")
    assert "cookiefile" not in ytdlp.calls[2] and ytdlp.waits == []


async def test_2026_10_25_an_invalid_output_never_becomes_a_success_naming_a_missing_file(ytdlp, tmp_path):
    def tiny(opts):
        Path(opts["outtmpl"].replace(".%(ext)s", ".mp4")).write_bytes(b"x" * 3848)   # an init segment
    ytdlp.on_ok = tiny
    with pytest.raises(DownloadError) as err:
        await engine.run(video(tmp_path))
    assert "isn't usable" in str(err.value)
    assert len(ytdlp.calls) == 3                            # each format once
    assert leftovers(tmp_path) == []


async def test_twitter_could_not_authenticate_is_a_sign_in_wall_not_an_unknown_error(ytdlp, tmp_path):
    ytdlp.script = ["ERROR: [twitter] 1: Error(s) while querying API: Could not authenticate you; please report"]
    with pytest.raises(BotDetectionError) as err:
        await engine.run(video(tmp_path, url="https://x.com/a/status/1"))
    assert "x.com wants you signed in" in str(err.value)
    assert len(ytdlp.calls) == 2 and ytdlp.waits == [3.0]   # one refresh, not five waits per format
    assert ytdlp.streak == []


# ── Every corpus wording, through the real loop ──────────────────────────────

@pytest.mark.parametrize("row", CORPUS, ids=[r["label"] for r in CORPUS])
async def test_every_corpus_error_ends_inside_the_budget_with_one_of_our_exceptions(ytdlp, tmp_path, row):
    ytdlp.script = [row["error"]]
    with pytest.raises(DownloadError) as err:               # every engine failure is a DownloadError of ours
        await engine.run(video(tmp_path, url=row["url"]))
    b = Budget.video()
    assert len(ytdlp.calls) <= b.max_attempts
    assert sum(ytdlp.waits) <= b.max_wait_s
    assert err.value.error_class.value == row["expect"]
    assert "Traceback" not in str(err.value) and "/Users/" not in str(err.value)
    assert leftovers(tmp_path) == []



# ── Rows of the table, as the person meets them ──────────────────────────────

async def test_bot_check_gets_one_cookie_refresh_then_the_sign_in_message(ytdlp, tmp_path):
    ytdlp.script = [BOT]
    with pytest.raises(BotDetectionError) as err:
        await engine.run(video(tmp_path))
    assert "YouTube requires browser sign-in" in str(err.value)
    assert len(ytdlp.calls) == 2 and ytdlp.browser_reads == 2


async def test_bot_check_when_the_browser_cannot_be_read_explains_that_instead(ytdlp, tmp_path):
    ytdlp.store.mark_unreadable(Exception(COOKIE_DB))
    ytdlp.script = [BOT]
    with pytest.raises(BotDetectionError) as err:
        await engine.run(video(tmp_path))
    assert "Full Disk Access" in str(err.value) and "open Chrome" not in str(err.value)
    assert len(ytdlp.calls) == 1 and ytdlp.browser_reads == 0


async def test_private_video_is_unavailable_at_once_in_plain_words(ytdlp, tmp_path):
    ytdlp.script = ["ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video."]
    with pytest.raises(VideoUnavailableError) as err:
        await engine.run(video(tmp_path))
    assert str(err.value) == "This video is private."
    assert len(ytdlp.calls) == 1 and ytdlp.streak == []


async def test_429_stops_and_the_next_download_to_that_host_waits(ytdlp, tmp_path):
    ytdlp.script = ["ERROR: [youtube] abc: Unable to download webpage: HTTP Error 429: Too Many Requests"]
    with pytest.raises(RateLimitError):
        await engine.run(video(tmp_path))
    assert len(ytdlp.calls) == 1
    assert health.cooldown_for(URL) > 0 and health.cooldown_for("https://vimeo.com/1") == 0


async def test_subtitle_429_continues_without_subtitles(ytdlp, tmp_path):
    ytdlp.script = ["ERROR: Unable to download video subtitles for 'ja': HTTP Error 429: Too Many Requests", "ok"]
    await engine.run(video(tmp_path))
    assert ytdlp.calls[0]["writesubtitles"] is True and ytdlp.calls[1]["writesubtitles"] is False
    assert health.cooldown_for(URL) == 0


async def test_network_error_waits_8_20_45_then_says_network(ytdlp, tmp_path):
    ytdlp.script = [NET]
    with pytest.raises(DownloadError) as err:
        await engine.run(video(tmp_path))
    assert "network problem" in str(err.value)
    assert ytdlp.waits == [8.0, 20.0, 45.0] and len(ytdlp.calls) == 4 and ytdlp.streak == []


async def test_missing_format_walks_the_chain_and_a_later_format_downloads(ytdlp, tmp_path):
    ytdlp.script = ["ERROR: [youtube] abc: Requested format is not available.", "ok"]
    await engine.run(video(tmp_path))
    assert ytdlp.calls[1]["format"] == "bestvideo+bestaudio/best" and ytdlp.waits == []


async def test_unknown_error_on_a_supported_site_feeds_the_update_streak_once(ytdlp, tmp_path):
    ytdlp.script = ["ERROR: [youtube] abc: Unable to extract initial player response"]
    with pytest.raises(DownloadError) as err:
        await engine.run(video(tmp_path))
    assert str(err.value).startswith("Download failed: [youtube] abc: Unable to extract")
    assert len(ytdlp.calls) == 6 and ytdlp.waits == [8.0, 8.0, 8.0]
    assert ytdlp.streak == [1]


async def test_embed_extras_that_fail_are_dropped_and_the_video_is_kept(ytdlp, tmp_path):
    ytdlp.script = ["ERROR: Postprocessing: Supported filetypes for thumbnail embedding are: mkv/mka, mp4", "ok"]
    result = await engine.run(video(tmp_path, options={"thumbnail": True}))
    assert result["option_postprocessors_dropped"] is True
    keys = [[pp["key"] for pp in c.get("postprocessors") or []] for c in ytdlp.calls]
    assert keys[0] and "EmbedThumbnail" in keys[0] and "EmbedThumbnail" not in keys[1]
    assert ytdlp.waits == []


async def test_merge_failure_with_streams_on_disk_is_merged_by_hand(ytdlp, tmp_path, monkeypatch):
    merged = tmp_path / "vid1.mp4"
    def fake_merge(output_dir, stem):
        merged.write_bytes(b"x" * VIDEO_BYTES)
        return merged
    monkeypatch.setattr(outputs, "try_manual_merge", fake_merge)
    ytdlp.script = ["ERROR: unable to rename file: [Errno 2] No such file or directory: 'vid1.f398.mp4.part'"]
    result = await engine.run(video(tmp_path))
    assert result["video_path"] == str(merged) and result["title"] == "A title"
    assert len(ytdlp.calls) == 1 and ytdlp.waits == []


async def test_a_bug_inside_yt_dlp_is_one_honest_failure_and_feeds_nothing(ytdlp, tmp_path):
    ytdlp.script = [KeyError("player_response")]
    (tmp_path / "vid1.f398.mp4.part").write_bytes(b"partial")
    with pytest.raises(DownloadError) as err:
        await engine.run(video(tmp_path))
    assert str(err.value).startswith("Download failed:")
    assert len(ytdlp.calls) == 1 and ytdlp.streak == [] and leftovers(tmp_path) == []


async def test_a_playlist_with_nothing_playable_is_unavailable(ytdlp, tmp_path):
    ytdlp.info = {"_type": "playlist", "entries": []}
    with pytest.raises(VideoUnavailableError):
        await engine.run(video(tmp_path))
    assert len(ytdlp.calls) == 1


async def test_partial_files_are_swept_after_every_failure(ytdlp, tmp_path):
    for name in ("vid1.mp4.part", "vid1.f251.webm", "vid1.temp.mp4"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "other.mp4").write_bytes(b"keep")
    ytdlp.script = [NET]
    with pytest.raises(DownloadError):
        await engine.run(video(tmp_path))
    assert leftovers(tmp_path) == ["other.mp4"]


async def test_the_persons_subtitle_file_is_kept_when_asked_for(ytdlp, tmp_path):
    def with_subs(opts):
        stem = opts["outtmpl"].replace(".%(ext)s", "")
        Path(stem + ".mp4").write_bytes(b"x" * VIDEO_BYTES)
        Path(stem + ".en.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nhello\n")
    ytdlp.on_ok = with_subs
    kept = await engine.run(video(tmp_path, options={"subtitles": "file"}))
    assert kept["kept_subtitle_paths"] == [str(tmp_path / "vid1.en.srt")] and (tmp_path / "vid1.en.srt").exists()
    swept = await engine.run(video(tmp_path, stem="vid2"))
    assert swept["kept_subtitle_paths"] == [] and not (tmp_path / "vid2.en.srt").exists()
    assert swept["subtitles"]                               # parsed for the transcript before the sweep


# ── Cancel ───────────────────────────────────────────────────────────────────

async def test_a_cancel_stops_the_ladder_at_the_next_step_and_keeps_the_reason(ytdlp, tmp_path):
    ytdlp.script = [NET]
    ytdlp.cancel_after_attempt = 1
    with pytest.raises(DownloadError) as err:
        await engine.run(video(tmp_path, job_id="job-1"))
    assert str(err.value).startswith("Download cancelled: Couldn't reach YouTube")
    assert len(ytdlp.calls) == 1 and ytdlp.waits == [] and ytdlp.streak == []


async def test_a_cancel_while_queued_never_starts_the_transfer(ytdlp, tmp_path, monkeypatch):
    async def cancelled(job_id):
        return True
    monkeypatch.setattr("backend.agents.job_helper.job_cancelled", cancelled)
    with pytest.raises(JobCancelledError):
        await engine.run(video(tmp_path, job_id="job-1"))
    assert ytdlp.calls == []


async def test_a_cancel_before_the_sign_in_refresh_never_reads_the_browser_again(ytdlp, tmp_path):
    ytdlp.script = [BOT]
    ytdlp.cancel_after_attempt = 1
    with pytest.raises(DownloadError):
        await engine.run(video(tmp_path, job_id="job-1"))
    assert ytdlp.browser_reads == 1                          # only the first jar; no Keychain prompt after a cancel


# ── Stall and abandonment ────────────────────────────────────────────────────

async def test_a_download_that_moves_nothing_is_abandoned_and_nothing_retries_its_stem(ytdlp, tmp_path, monkeypatch):
    release = threading.Event()
    def hang(opts):
        release.wait(10)
        Path(opts["outtmpl"].replace(".%(ext)s", ".mp4")).write_bytes(b"x" * VIDEO_BYTES)
    ytdlp.on_ok = hang
    monkeypatch.setattr(engine.settings, "DOWNLOAD_STALL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(progress, "_STALL_POLL_S", 0.05)
    try:
        with pytest.raises(DownloadStalledError):
            await engine.run(video(tmp_path))
        assert len(ytdlp.calls) == 1                         # terminal: no second attempt into the stem
    finally:
        release.set()
        await asyncio.sleep(0.3)                             # let the abandoned thread finish


# ── The probe ────────────────────────────────────────────────────────────────

async def test_probe_passes_a_normal_video(ytdlp):
    result = await engine.probe(URL, max_duration_s=7200)
    assert result.force_generic is False and result.info["duration"] == 42


@pytest.mark.parametrize("info,words", [
    ({"is_live": True}, "Live streams"),
    ({"live_status": "is_upcoming"}, "Live streams"),
    ({"duration": 3 * 3600}, "3.0 hours long"),
])
async def test_probe_refuses_live_and_overlong_sources(ytdlp, info, words):
    ytdlp.info = info
    with pytest.raises(VideoUnavailableError) as err:
        await engine.probe(URL, max_duration_s=7200)
    assert words in str(err.value)


async def test_probe_retries_without_cookies_when_the_browser_cannot_be_read(ytdlp):
    ytdlp.probe_script = [COOKIE_DB, "ok"]
    await engine.probe(URL, max_duration_s=7200)
    assert "cookiefile" in ytdlp.probe_calls[0] and "cookiefile" not in ytdlp.probe_calls[1]
    assert ytdlp.store.status().state is CookieState.UNREADABLE


async def test_probe_tries_the_generic_extractor_for_an_unsupported_site(ytdlp):
    ytdlp.probe_script = ["ERROR: Unsupported URL: https://some-site.example/watch/9", "ok"]
    result = await engine.probe("https://some-site.example/watch/9", max_duration_s=7200)
    assert result.force_generic is True and ytdlp.probe_calls[1]["force_generic_extractor"] is True


async def test_probe_escalates_when_even_the_generic_extractor_sees_nothing(ytdlp):
    ytdlp.probe_script = ["ERROR: Unsupported URL: https://some-site.example/watch/9"]
    with pytest.raises(ExtractorMissingError) as err:
        await engine.probe("https://some-site.example/watch/9", max_duration_s=7200)
    assert str(err.value) == "No standard extractor for some-site.example."
    assert len(ytdlp.probe_calls) == 2


async def test_probe_ends_on_certain_answers_only(ytdlp):
    ytdlp.probe_script = ["ERROR: [youtube] abc: This video has been removed by the uploader."]
    with pytest.raises(VideoUnavailableError):
        await engine.probe(URL, max_duration_s=7200)
    ytdlp.probe_calls.clear()
    ytdlp.probe_script = ["ERROR: [generic] 1: Checking your browser. Cloudflare challenge."]
    with pytest.raises(ExtractorMissingError):
        await engine.probe("https://example.com/v/1", max_duration_s=7200)


@pytest.mark.parametrize("text", [BOT, NET, "ERROR: [youtube] abc: something nobody has seen before",
                                  "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users. Use --cookies-from-browser"])
async def test_probe_lets_the_download_try_when_the_answer_is_not_certain(ytdlp, text):
    ytdlp.probe_script = [text]
    result = await engine.probe(URL, max_duration_s=7200)
    assert result.info is None and result.force_generic is False
    assert len(ytdlp.probe_calls) == 1                       # the probe never retries a guess


async def test_generic_forced_by_the_probe_is_used_by_the_download(ytdlp, tmp_path):
    await engine.run(video(tmp_path), force_generic=True)
    assert ytdlp.calls[0]["force_generic_extractor"] is True


# ── Found while migrating the old tests (2026-11-13) ─────────────────────────


async def test_an_abandoned_download_that_finishes_later_keeps_no_orphan_file(ytdlp, tmp_path, monkeypatch):
    release = threading.Event()
    def slow(opts):
        release.wait(10)
        Path(opts["outtmpl"].replace(".%(ext)s", ".mp4")).write_bytes(b"x" * VIDEO_BYTES)
    ytdlp.on_ok = slow
    monkeypatch.setattr(engine.settings, "DOWNLOAD_STALL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(progress, "_STALL_POLL_S", 0.05)
    with pytest.raises(DownloadStalledError):
        await engine.run(video(tmp_path))
    release.set()                                          # the abandoned thread now "finishes"
    for _ in range(40):
        await asyncio.sleep(0.05)
        if not leftovers(tmp_path):
            break
    assert leftovers(tmp_path) == []                       # no video that no Library row owns
    assert len(ytdlp.calls) == 1


async def test_an_abandoned_download_starts_no_further_attempt(ytdlp, tmp_path, monkeypatch):
    release = threading.Event()
    def hang_then_fail(opts):
        release.wait(10)
        raise __import__("yt_dlp").utils.DownloadError(NET)
    ytdlp.on_ok = hang_then_fail
    monkeypatch.setattr(engine.settings, "DOWNLOAD_STALL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(progress, "_STALL_POLL_S", 0.05)
    with pytest.raises(DownloadStalledError):
        await engine.run(video(tmp_path))
    release.set()
    await asyncio.sleep(0.5)
    assert len(ytdlp.calls) == 1 and ytdlp.waits == []     # the ladder did not carry on behind our back


async def test_cleanup_and_progress_never_count_another_download_with_a_longer_name(tmp_path):
    for name in ("vid1.mp4.part", "vid1.mp4.part-Frag3", "vid1.mp4.ytdl", "vid10.mp4.part", "vid1x.f251.webm"):
        (tmp_path / name).write_bytes(b"x" * 10)
    assert progress.stem_footprint((tmp_path, "vid1")) == 30
    outputs.cleanup_partial_files(tmp_path, "vid1")
    assert leftovers(tmp_path) == ["vid10.mp4.part", "vid1x.f251.webm"]


async def test_a_listing_or_subtitle_failure_never_asks_for_a_browser():
    exc = engine.terminal_exception(Exception("ERROR: [x] 1: HTTP Error 403: Forbidden"), URL)
    assert type(exc) is DownloadError and exc.error_class is E.HTTP_403
    assert not isinstance(exc, ExtractorMissingError)


async def test_a_cancel_during_a_wait_starts_no_further_transfer(ytdlp, tmp_path, monkeypatch):
    # The wait returns early on a cancel; the ladder must not go straight on
    # to the next attempt (found 2026-11-13 migrating the cancel tests).
    state = {"cancelled": False}
    def wait_and_cancel(seconds, job_id, loop):
        ytdlp.waits.append(seconds)
        state["cancelled"] = True
    monkeypatch.setattr(progress, "cancellable_sleep", wait_and_cancel)
    monkeypatch.setattr(progress, "job_cancelled_from_thread", lambda job_id, loop: state["cancelled"])
    ytdlp.script = [NET]
    with pytest.raises(DownloadError) as err:
        await engine.run(video(tmp_path, job_id="job-1"))
    assert str(err.value).startswith("Download cancelled:")
    assert len(ytdlp.calls) == 1 and ytdlp.waits == [8.0]


# ── A cancel stops a MOVING transfer (2026-11-13) ────────────────────────────

async def test_a_cancel_while_bytes_flow_stops_the_transfer_and_keeps_nothing(ytdlp, tmp_path, monkeypatch):
    # The old design let a cancelled transfer run to the end ("yt-dlp can't be
    # interrupted"): measured 97 s more and a kept file on a 10-minute stream.
    from backend.core.exceptions import JobCancelledError
    monkeypatch.setattr(progress, "STOP_POLL_S", 0.0)
    chunks = []
    def transfer(opts):
        stem = opts["outtmpl"].replace(".%(ext)s", "")
        Path(stem + ".mp4.part").write_bytes(b"x" * 1000)
        for n in range(1, 50):                                  # yt-dlp calls the hook per chunk
            chunks.append(n)
            opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": n * 1000})
        Path(stem + ".mp4").write_bytes(b"x" * VIDEO_BYTES)     # never reached
    ytdlp.on_ok = transfer
    ytdlp.cancel_after_attempt = 1                              # "cancelled" as soon as the attempt has started
    with pytest.raises(JobCancelledError):
        await engine.run(video(tmp_path, job_id="job-1"))
    assert chunks == [1]                                        # stopped on the first chunk after the cancel
    assert leftovers(tmp_path) == [] and len(ytdlp.calls) == 1 and ytdlp.streak == []


async def test_without_a_cancel_the_hook_never_stops_a_transfer(ytdlp, tmp_path, monkeypatch):
    monkeypatch.setattr(progress, "STOP_POLL_S", 0.0)
    def transfer(opts):
        for n in range(1, 20):
            opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": n * 1000})
        Path(opts["outtmpl"].replace(".%(ext)s", ".mp4")).write_bytes(b"x" * VIDEO_BYTES)
    ytdlp.on_ok = transfer
    result = await engine.run(video(tmp_path, job_id="job-1"))
    assert Path(result["video_path"]).exists()


async def test_a_failing_cancel_check_never_stops_a_healthy_download(ytdlp, tmp_path, monkeypatch):
    monkeypatch.setattr(progress, "STOP_POLL_S", 0.0)
    def boom(job_id, loop):
        raise RuntimeError("database is locked")
    def transfer(opts):
        monkeypatch.setattr(progress, "job_cancelled_from_thread", boom)
        opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 1000})
        Path(opts["outtmpl"].replace(".%(ext)s", ".mp4")).write_bytes(b"x" * VIDEO_BYTES)
    ytdlp.on_ok = transfer
    result = await engine.run(video(tmp_path, job_id="job-1"))
    assert Path(result["video_path"]).exists()


async def test_an_abandoned_transfer_that_moves_again_is_stopped_by_its_own_hook(ytdlp, tmp_path, monkeypatch):
    release = threading.Event()
    seen = []
    def transfer(opts):
        release.wait(10)                                        # no bytes: the watchdog abandons it
        for n in range(1, 50):
            seen.append(n)
            opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": n * 1000})
        Path(opts["outtmpl"].replace(".%(ext)s", ".mp4")).write_bytes(b"x" * VIDEO_BYTES)
    ytdlp.on_ok = transfer
    monkeypatch.setattr(engine.settings, "DOWNLOAD_STALL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(progress, "_STALL_POLL_S", 0.05)
    with pytest.raises(DownloadStalledError):
        await engine.run(video(tmp_path))
    release.set()                                               # the connection comes back to life
    await asyncio.sleep(0.4)
    assert seen == [1] and leftovers(tmp_path) == []            # one chunk, then stopped; nothing kept
