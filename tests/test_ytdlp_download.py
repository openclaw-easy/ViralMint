# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The download ladder — the single most load-bearing feature in the app.

Downloading is where the platform actively works against us: formats rotate,
403s appear for cookie'd requests that would have succeeded without them, bot
detection triggers on some clients and not others, and 429s arrive mid-run. So
`download_video` isn't one call, it's a cascade — a format-fallback chain, two
attempts per format, and several error-specific recoveries layered on top.

Each rung exists because of a specific real failure, and each is pinned here:

  * a PERMANENT error (private / deleted / region-blocked) must raise
    immediately, not grind through every remaining format;
  * a 429 caused by SUBTITLES retries without them — Whisper transcribes
    anyway, so subtitles are never worth failing a download over;
  * a 403 on a cookie'd request retries WITHOUT cookies, and the drop has to
    persist across the later formats too;
  * a rate limit sets a cooldown the next download waits out.

`yt_dlp` is stubbed; nothing here touches the network.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from backend.core.exceptions import (DownloadError, RateLimitError,
                                     VideoUnavailableError)
from backend.services import ytdlp_service as YT


# ── error classification ────────────────────────────────────────────────────

class TestErrorClassification:
    @pytest.mark.parametrize("msg", [
        "ERROR: Private video. Sign in if you've been granted access",
        "Video unavailable. This video has been removed by the uploader",
        "This video is not available in your country",
        "This video has been removed for violating",
    ])
    def test_permanent_failures_are_recognised(self, msg):
        """Retrying these burns minutes for a result that can't change."""
        assert YT._is_permanent_error(msg.lower()) is True

    @pytest.mark.parametrize("msg", [
        "ERROR: [youtube] abc: Unable to download webpage: HTTP Error 500: Internal Server Error",
        "Unable to download webpage: <urlopen error [Errno 54] Connection reset by peer>",
        "ERROR: Requested format is not available. Use --list-formats",
        "HTTP Error 404: Not Found",
        "Unable to extract uploader id; please report this issue",
    ])
    def test_ordinary_failures_are_not_permanent(self, msg):
        """A bare token used to decide this: "age" is inside "webpage", so a
        plain HTTP 500 on the page fetch failed at once as "Video unavailable"
        and was never retried."""
        assert YT._is_permanent_error(msg.lower()) is False

    @pytest.mark.parametrize("msg", [
        "Sign in to confirm your age. This video may be inappropriate for some users.",
        "This video is age-restricted",
        "The uploader has not made this video available in your country",
        "This video is not available in your country",
        "Join this channel to get access to members-only content",
        "This video is no longer available because the YouTube account has been terminated",
    ])
    def test_permanent_phrases_still_match(self, msg):
        assert YT._is_permanent_error(msg.lower()) is True

    def test_a_word_in_the_url_does_not_classify_the_error(self):
        """yt-dlp echoes the request URL; a phrase that happens to sit in the
        URL's path must not make a transient error permanent."""
        url = "https://example.com/age-gate/clip/123"
        err = f"ERROR: Unable to download webpage: {url} (HTTP Error 500)"
        text = YT._classifiable_text(err, url)
        assert "age-gate" not in text
        assert YT._is_permanent_error(text) is False
        assert YT._is_permanent_error(err.lower()) is True  # what the strip prevents

    def test_terminal_mapping_retries_a_webpage_500_as_a_download_error(self):
        err = Exception("Unable to download webpage: HTTP Error 500")
        mapped = YT._terminal_download_error(err, "https://example.com/v")
        assert not isinstance(mapped, VideoUnavailableError)

    @pytest.mark.parametrize("msg", [
        "unable to download video data: HTTP Error 500",
        "Connection reset by peer",
        "read timed out",
    ])
    def test_transient_failures_are_recognised(self, msg):
        assert YT._is_transient_error(msg.lower()) is True

    @pytest.mark.parametrize("msg", [
        "Sign in to confirm you're not a bot",
        "confirm you're not a bot. use --cookies",
    ])
    def test_bot_detection_is_recognised(self, msg):
        """This one has its own recovery — re-attempting with cookies."""
        assert YT._is_bot_detection_error(msg) is True

    def test_an_ordinary_error_is_none_of_the_three(self):
        msg = "something else entirely went wrong"
        assert not YT._is_permanent_error(msg)
        assert not YT._is_bot_detection_error(msg)


class TestHttpHeaders:
    def test_a_browser_like_user_agent_is_sent(self):
        """A default python-urllib UA is an instant block on some hosts."""
        h = YT._yt_dlp_http_headers()
        ua = next(v for k, v in h.items() if k.lower() == "user-agent")
        assert "Mozilla" in ua

    def test_the_headers_are_a_plain_string_dict(self):
        h = YT._yt_dlp_http_headers()
        assert h and all(isinstance(k, str) and isinstance(v, str)
                         for k, v in h.items())


# ── cookie discovery ────────────────────────────────────────────────────────

class TestCookieBrowserDetection:
    @pytest.fixture(autouse=True)
    def clear_cache(self, monkeypatch):
        monkeypatch.setattr(YT, "_BROWSER_COOKIE_SOURCE", None)

    def test_a_cached_answer_is_reused(self, monkeypatch):
        """Probing the filesystem on every download is pure waste."""
        monkeypatch.setattr(YT, "_BROWSER_COOKIE_SOURCE", "brave")
        monkeypatch.setattr(YT.Path, "exists", lambda self: (_ for _ in ()).throw(
            AssertionError("must not probe when cached")))
        assert YT._detect_cookie_browser() == "brave"

    def test_a_cached_negative_stays_negative(self, monkeypatch):
        monkeypatch.setattr(YT, "_BROWSER_COOKIE_SOURCE", "")
        assert YT._detect_cookie_browser() is None

    def test_the_first_installed_browser_wins_on_macos(self, monkeypatch):
        monkeypatch.setattr(YT.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(YT.Path, "exists",
                            lambda self: "Brave" in str(self))
        assert YT._detect_cookie_browser() == "brave"

    def test_no_browser_at_all_is_none(self, monkeypatch):
        monkeypatch.setattr(YT.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(YT.Path, "exists", lambda self: False)
        assert YT._detect_cookie_browser() is None


class TestCookieFile:
    @pytest.fixture(autouse=True)
    def tmp_cookies(self, tmp_path, monkeypatch):
        from backend.config import settings as S
        monkeypatch.setattr(type(S), "TMP_DIR", property(lambda self: tmp_path))
        monkeypatch.setattr(YT, "_COOKIE_FILE", None)
        monkeypatch.setattr(YT, "_COOKIE_FILE_AGE", 0.0)
        return tmp_path

    def test_a_fresh_file_on_disk_is_reused(self, tmp_cookies, monkeypatch):
        """It survives a server restart — re-extracting would trigger another
        Keychain prompt for nothing."""
        ck = tmp_cookies / "yt_cookies.txt"
        ck.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setattr(YT, "_detect_cookie_browser", lambda: (_ for _ in ()).throw(
            AssertionError("must not re-extract when a fresh file exists")))
        assert YT._get_cookie_file() == ck

    def test_an_empty_file_is_not_reused(self, tmp_cookies, monkeypatch):
        (tmp_cookies / "yt_cookies.txt").write_text("")
        monkeypatch.setattr(YT, "_detect_cookie_browser", lambda: None)
        assert YT._get_cookie_file() is None

    def test_a_stale_file_beats_nothing_when_no_browser_is_present(
            self, tmp_cookies, monkeypatch):
        import os
        import time
        ck = tmp_cookies / "yt_cookies.txt"
        ck.write_text("# cookies\n")
        old = time.time() - (YT._COOKIE_MAX_AGE + 100)
        os.utime(ck, (old, old))
        monkeypatch.setattr(YT, "_detect_cookie_browser", lambda: None)
        assert YT._get_cookie_file() == ck

    def test_no_file_and_no_browser_is_none(self, tmp_cookies, monkeypatch):
        monkeypatch.setattr(YT, "_detect_cookie_browser", lambda: None)
        assert YT._get_cookie_file() is None

    def test_a_failed_extraction_degrades_to_none(self, tmp_cookies, monkeypatch):
        """A Keychain denial must not fail the download outright."""
        monkeypatch.setattr(YT, "_detect_cookie_browser", lambda: "chrome")
        mod = types.ModuleType("yt_dlp")

        class YDL:
            def __init__(self, opts):
                raise RuntimeError("user denied Keychain access")
        mod.YoutubeDL = YDL
        monkeypatch.setitem(sys.modules, "yt_dlp", mod)
        assert YT._get_cookie_file() is None


# ── the download cascade ────────────────────────────────────────────────────

class _DownloadError(Exception):
    pass


def _install_yt_dlp(monkeypatch, script, out_dir: Path):
    """`script` is a list of outcomes, one per YoutubeDL().extract_info call:
    an Exception to raise, or None to succeed."""
    calls: list[dict] = []

    utils = types.SimpleNamespace(DownloadError=_DownloadError)

    class YDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            idx = len(calls)
            calls.append({"format": self.opts.get("format"),
                          "opts": dict(self.opts)})
            outcome = script[idx] if idx < len(script) else None
            if isinstance(outcome, Exception):
                raise outcome
            vid = out_dir / "testid.mp4"
            vid.write_bytes(b"\x00" * 4096)
            return {"id": "testid", "ext": "mp4", "duration": 120,
                    "title": "A video", "requested_downloads": [{"filepath": str(vid)}]}

        def prepare_filename(self, info):
            return str(out_dir / "testid.mp4")

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = YDL
    mod.utils = utils
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", utils)
    return calls


@pytest.fixture()
def dl_env(tmp_path, monkeypatch):
    monkeypatch.setattr(YT, "_check_cooldown", lambda: 0)
    monkeypatch.setattr(YT, "_get_cookie_file", lambda: None)
    monkeypatch.setattr(YT, "_detect_cookie_browser", lambda: None)
    # The retry rungs sleep 3-5s between attempts; without this the failure
    # tests take ~20s of real waiting for nothing.
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda *_: None)
    return tmp_path


class TestDownloadCascade:
    def _download(self, out_dir, **kw):
        return asyncio.run(YT.download_video(
            "https://youtu.be/testid", output_dir=out_dir,
            extract_audio=False, **kw))

    def test_the_happy_path_returns_the_file_and_its_metadata(
            self, dl_env, monkeypatch):
        _install_yt_dlp(monkeypatch, [None], dl_env)
        out = self._download(dl_env)
        assert Path(out["video_path"]).exists()
        assert out["duration"] == 120

    def test_only_the_first_format_is_tried_when_it_works(
            self, dl_env, monkeypatch):
        calls = _install_yt_dlp(monkeypatch, [None], dl_env)
        self._download(dl_env)
        assert len(calls) == 1

    def test_a_permanent_failure_raises_immediately(self, dl_env, monkeypatch):
        """Grinding through every remaining format for a deleted video is
        minutes spent on a result that cannot change."""
        calls = _install_yt_dlp(
            monkeypatch, [_DownloadError("ERROR: Private video. Sign in")], dl_env)
        with pytest.raises(VideoUnavailableError):
            self._download(dl_env)
        assert len(calls) == 1, "it must not walk the fallback chain"

    def test_a_transient_failure_retries_the_same_format(
            self, dl_env, monkeypatch):
        calls = _install_yt_dlp(
            monkeypatch, [_DownloadError("HTTP Error 500"), None], dl_env)
        self._download(dl_env)
        assert len(calls) == 2
        assert calls[0]["format"] == calls[1]["format"], "same format, second try"

    def test_it_walks_the_format_chain(self, dl_env, monkeypatch):
        """Formats rotate; the chain is why a download survives that. A
        non-transient format failure moves straight to the next format rather
        than burning its second attempt on the same one."""
        script = [_DownloadError("requested format not available")] * 2 + [None]
        calls = _install_yt_dlp(monkeypatch, script, dl_env)
        self._download(dl_env)
        tried = [c["format"] for c in calls]
        assert tried == YT.FORMAT_FALLBACK_CHAIN[:3], tried

    def test_a_subtitle_429_retries_WITHOUT_subtitles(self, dl_env, monkeypatch):
        """Whisper transcribes anyway — subtitles are never worth failing a
        download over."""
        calls = _install_yt_dlp(
            monkeypatch,
            [_DownloadError("HTTP Error 429: Too Many Requests (subtitles)"), None],
            dl_env)
        self._download(dl_env)
        assert len(calls) >= 2
        assert calls[1]["opts"].get("writesubtitles") in (False, None)

    def test_a_403_on_a_cookied_request_retries_without_cookies(
            self, dl_env, monkeypatch, tmp_path):
        """Supplying any cookie makes yt-dlp skip every player client that
        can't carry one — which are exactly the token-free ones."""
        ck = tmp_path / "ck.txt"
        ck.write_text("# cookies\n")
        monkeypatch.setattr(YT, "_get_cookie_file", lambda: ck)
        calls = _install_yt_dlp(
            monkeypatch, [_DownloadError("HTTP Error 403: Forbidden"), None], dl_env)
        self._download(dl_env)
        assert len(calls) >= 2
        assert "cookiefile" not in calls[-1]["opts"], (
            "the cookie must be dropped on the retry")

    def test_a_hard_rate_limit_raises_and_sets_a_cooldown(
            self, dl_env, monkeypatch):
        _install_yt_dlp(
            monkeypatch,
            [_DownloadError("HTTP Error 429: Too Many Requests")] * 30, dl_env)
        with pytest.raises(RateLimitError):
            self._download(dl_env)

    def test_exhausting_every_format_raises_a_download_error(
            self, dl_env, monkeypatch):
        _install_yt_dlp(
            monkeypatch, [_DownloadError("some other failure")] * 40, dl_env)
        with pytest.raises(DownloadError):
            self._download(dl_env)

    def test_an_active_cooldown_is_waited_out(self, dl_env, monkeypatch):
        slept = {}

        async def fake_sleep(secs):
            slept["secs"] = secs
        monkeypatch.setattr(YT.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(YT, "_check_cooldown", lambda: 4.0)
        _install_yt_dlp(monkeypatch, [None], dl_env)
        self._download(dl_env)
        assert slept["secs"] == 4.0


# ── per-download options ────────────────────────────────────────────────────

class TestPerDownloadOptions:
    """Options are strictly opt-in and can only ever degrade.

    The whole design rests on two invariants that are easy to break by
    accident: passing nothing must reach yt-dlp exactly as it did before the
    parameter existed, and asking for something the source can't provide must
    yield a lesser file rather than an error.
    """

    def _download(self, out_dir, **kw):
        return asyncio.run(YT.download_video(
            "https://youtu.be/testid", output_dir=out_dir,
            extract_audio=False, **kw))

    def test_no_options_walks_the_module_chain_untouched(self, dl_env, monkeypatch):
        calls = _install_yt_dlp(
            monkeypatch, [_DownloadError("requested format not available")] * 2 + [None],
            dl_env)
        out = self._download(dl_env)
        assert [c["format"] for c in calls] == YT.FORMAT_FALLBACK_CHAIN[:3]
        assert out["requested_quality"] is None
        assert out["option_postprocessors_dropped"] is False

    def test_a_quality_request_swaps_in_its_own_ladder(self, dl_env, monkeypatch):
        from backend.services import download_options as dlo
        calls = _install_yt_dlp(monkeypatch, [None], dl_env)
        out = self._download(dl_env, options={"quality": "480p"})
        assert calls[0]["format"] == dlo.QUALITY_LADDERS["480p"][0]
        assert out["requested_quality"] == "480p"

    def test_a_cap_the_source_cannot_meet_falls_through_to_uncapped(
            self, dl_env, monkeypatch):
        """The safety property, end to end: every capped rung failing must
        still produce a video off the unconstrained tail, not an error."""
        calls = _install_yt_dlp(
            monkeypatch, [_DownloadError("requested format not available")] * 2 + [None],
            dl_env)
        out = self._download(dl_env, options={"quality": "2160p"})
        assert Path(out["video_path"]).exists()
        assert "height<=" not in calls[-1]["format"], (
            "the last rung tried must be unconstrained")

    def test_option_overrides_reach_yt_dlp(self, dl_env, monkeypatch):
        calls = _install_yt_dlp(monkeypatch, [None], dl_env)
        self._download(dl_env, options={"subtitles": "file", "sub_langs": ["es"],
                                        "container": "mkv"})
        opts = calls[0]["opts"]
        assert opts["subtitleslangs"] == ["es"]
        assert opts["merge_output_format"] == "mkv"

    def test_garbage_options_degrade_to_the_default_path(self, dl_env, monkeypatch):
        """An LLM inventing an option must not fail the download."""
        calls = _install_yt_dlp(monkeypatch, [None], dl_env)
        out = self._download(dl_env, options={"quality": "8k", "sponsorblock": "all"})
        assert calls[0]["format"] == YT.FORMAT_FALLBACK_CHAIN[0]
        assert out["requested_quality"] is None

    def test_subtitles_file_mode_keeps_the_sidecar(self, dl_env, monkeypatch):
        _install_yt_dlp(monkeypatch, [None], dl_env)
        sidecar = dl_env / "testid.en.srt"
        sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        out = self._download(dl_env, options={"subtitles": "file"})
        assert sidecar.exists(), "the sidecar IS the deliverable in file mode"
        assert str(sidecar) in out["kept_subtitle_paths"]

    def test_subtitles_are_still_swept_when_not_requested(self, dl_env, monkeypatch):
        _install_yt_dlp(monkeypatch, [None], dl_env)
        sidecar = dl_env / "testid.en.srt"
        sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        out = self._download(dl_env)
        assert not sidecar.exists(), "loose subtitle files are litter by default"
        assert out["kept_subtitle_paths"] == []


class TestOptionalPostprocessorDrop:
    """A postprocessing failure from the embed extras (thumbnail / subtitles /
    metadata options) fires AFTER the video is fully downloaded — it must drop
    the extras and keep the video, never fail the download. Real-world
    trigger: EmbedThumbnail hard-raises on a single-stream webm, which the
    merge_output_format steer can't prevent (no merge happens)."""

    _PP_ERR = ("ERROR: Postprocessing: Supported filetypes for thumbnail "
               "embedding are: mp3, mkv/mka, ogg/opus/flac, m4a/mp4/m4v/mov")

    def _download(self, out_dir, **kw):
        return asyncio.run(YT.download_video(
            "https://youtu.be/testid", output_dir=out_dir,
            extract_audio=False, **kw))

    def test_pp_failure_drops_extras_and_keeps_the_video(
            self, dl_env, monkeypatch, caplog):
        calls = _install_yt_dlp(monkeypatch, [_DownloadError(self._PP_ERR)], dl_env)
        with caplog.at_level("WARNING"):
            out = self._download(dl_env, options={"thumbnail": True})

        assert Path(out["video_path"]).exists()
        # The degradation is REPORTED, not silent — callers pass this on so
        # "embed the cover art" is never claimed over a plain file.
        assert out["option_postprocessors_dropped"] is True
        assert calls[0]["format"] == calls[1]["format"], "same format, no re-download"
        assert not calls[1]["opts"].get("postprocessors"), (
            "our postprocessors must be gone on the retry")

    def test_pp_drop_fires_once_then_normal_classification(
            self, dl_env, monkeypatch, caplog):
        # If the source keeps failing after the extras are gone, the error is
        # NOT ours — it must flow to the normal retry/format cascade instead
        # of looping on the drop branch forever.
        _install_yt_dlp(monkeypatch, [_DownloadError(self._PP_ERR)] * 40, dl_env)
        with caplog.at_level("WARNING"):
            with pytest.raises(DownloadError):
                self._download(dl_env, options={"thumbnail": True})

        drops = [r for r in caplog.records
                 if "retrying without embed extras" in r.getMessage()]
        assert len(drops) == 1, "the drop rung must burn at most once per download"

    def test_pp_error_without_options_takes_the_normal_path(
            self, dl_env, monkeypatch, caplog):
        # No per-download options → no optional postprocessors → nothing to
        # drop. The branch must not fire (and must not swallow the error).
        _install_yt_dlp(monkeypatch, [_DownloadError(self._PP_ERR), None], dl_env)
        with caplog.at_level("WARNING"):
            out = self._download(dl_env)

        assert not any("retrying without embed extras" in r.getMessage()
                       for r in caplog.records)
        assert Path(out["video_path"]).exists()
        assert out["option_postprocessors_dropped"] is False
