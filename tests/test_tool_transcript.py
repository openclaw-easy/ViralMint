# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Transcript Downloader — fetch the caption track a video already publishes.

Three layers, each tested at its own seam:

  * `extract_subtitles_only` (ytdlp_service): track choice, the two yt-dlp
    passes on `download_pool`, the one bounded retry, and honest failures. yt-dlp
    itself is replaced by a fake `YoutubeDL`, and the pool by a pass-through, so
    nothing reaches the network.
  * `run_tool_transcript` (tool_runners): format conversion through the shared
    parser + writer, the "no captions" failure that names the alternative
    instead of transcribing, the language-substitution warning, scratch cleanup.
  * `POST /api/tools/transcript` and the page wiring: validation, the job type
    the frontend re-attaches on, the route + hub card.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

_TMP = Path(tempfile.mkdtemp(prefix="vm-transcript-"))
os.environ.setdefault("VIRALMINT_DATA_DIR", str(_TMP))
os.environ.setdefault("DEBUG", "false")

import pytest

from backend.core import tool_runners as tr
from backend.core.exceptions import (
    DownloadError,
    DownloadTimeoutError,
    NoCaptionsError,
    VideoUnavailableError,
)
from backend.services import ytdlp_service as ys

REPO = Path(__file__).resolve().parents[1]

SRT = """1
00:00:01,000 --> 00:00:03,000
Hello there

2
00:00:03,500 --> 00:00:06,000
This is the second cue
"""


# ── Track choice (pure) ─────────────────────────────────────────────────────

class TestLanguageChain:
    def test_caller_language_leads_then_the_default_chain(self):
        chain = ys._subtitle_lang_chain("sw", "en")
        assert chain[0] == "sw"
        assert chain[1:] == list(ys._DEFAULT_SUBTITLE_LANGS)

    def test_without_a_caller_language_the_videos_own_language_leads(self):
        """YouTube publishes ~150 auto-TRANSLATED tracks next to the original.
        "Auto" on a Spanish talk must not resolve to the English translation
        just because "en" heads the default chain."""
        chain = ys._subtitle_lang_chain(None, "es-419")
        assert chain[0] == "es"
        assert chain.count("es") == 1

    def test_no_hint_at_all_is_the_default_chain(self):
        assert ys._subtitle_lang_chain("", None) == list(ys._DEFAULT_SUBTITLE_LANGS)


class TestPickTrack:
    def test_creator_track_beats_auto(self):
        assert ys._pick_subtitle_track({"en": []}, {"en": []}, ["en"]) == ("en", "en", False)

    def test_regional_creator_track_counts(self):
        """`en-US` / `es-419` are creator tracks for that language — an exact-key
        match alone skipped them and fell through to auto captions."""
        assert ys._pick_subtitle_track({"es-419": []}, {"es": []}, ["es"]) == ("es-419", "es", False)

    def test_orig_auto_track_is_used_when_that_is_all_there_is(self):
        assert ys._pick_subtitle_track({}, {"fr-orig": []}, ["fr"]) == ("fr-orig", "fr", True)

    def test_caller_order_wins_over_track_kind(self):
        """An auto track in the requested language beats a creator track in a
        later fallback language."""
        assert ys._pick_subtitle_track({"de": []}, {"en": []}, ["en", "de"]) == ("en", "en", True)

    def test_prefer_manual_false_takes_auto_first(self):
        assert ys._pick_subtitle_track({"en": []}, {"en": []}, ["en"], prefer_manual=False) \
            == ("en", "en", True)

    def test_a_script_keyed_auto_track_counts(self):
        # YouTube keys Chinese auto tracks zh-Hans / zh-Hant, never plain zh.
        assert ys._pick_subtitle_track({}, {"zh-Hans": [], "zh-Hant": []}, ["zh"]) \
            == ("zh-Hans", "zh", True)

    def test_a_regional_auto_track_counts(self):
        assert ys._pick_subtitle_track({}, {"pt-BR": []}, ["pt"]) == ("pt-BR", "pt", True)

    def test_the_orig_track_beats_a_variant(self):
        assert ys._pick_subtitle_track({}, {"es-419": [], "es-orig": []}, ["es"]) \
            == ("es-orig", "es", True)

    def test_nothing_in_the_chain(self):
        assert ys._pick_subtitle_track({"it": []}, {}, ["en", "de"]) is None


# ── extract_subtitles_only (fake yt-dlp, pass-through pool) ─────────────────

class _FakeYDL:
    """Stands in for yt_dlp.YoutubeDL. `info` is what the probe returns; on a
    download pass it writes the subtitle file yt-dlp would have written."""
    info: dict = {}
    writes: str | None = SRT
    calls: list[dict] = []
    raise_on: list[Exception] = []

    def __init__(self, opts):
        self.opts = opts
        _FakeYDL.calls.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if _FakeYDL.raise_on:
            raise _FakeYDL.raise_on.pop(0)
        if download and _FakeYDL.writes is not None:
            stem = self.opts["outtmpl"].rsplit(".%(ext)s", 1)[0]
            track = self.opts["subtitleslangs"][0]
            Path(f"{stem}.{track}.srt").write_text(_FakeYDL.writes, encoding="utf-8")
        return dict(_FakeYDL.info)


@pytest.fixture
def fake_ytdlp(monkeypatch):
    import yt_dlp

    _FakeYDL.info = {
        "id": "abc123", "title": "A video", "duration": 6, "language": "en",
        "subtitles": {"en": [{}]}, "automatic_captions": {"en": [{}]},
    }
    _FakeYDL.writes = SRT
    _FakeYDL.calls = []
    _FakeYDL.raise_on = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    # No cookie extraction (it would touch the browser / Keychain).
    monkeypatch.setattr(ys, "_apply_cookies", lambda opts: opts.update({"cookiefile": "/x"}))
    monkeypatch.setattr(ys.asyncio, "sleep", AsyncMock())

    pool_calls: list = []

    async def _passthrough(fn, *args, **kwargs):
        pool_calls.append(fn)
        return fn(*args, **kwargs)

    monkeypatch.setattr(ys.download_pool, "run", _passthrough)
    return pool_calls


@pytest.mark.asyncio
class TestExtractSubtitlesOnly:
    async def test_fetches_the_creator_track_without_video_bytes(self, fake_ytdlp, tmp_path):
        info = await ys.extract_subtitles_only("https://x.test/v", output_dir=tmp_path)

        assert info["language"] == "en" and info["is_auto_generated"] is False
        assert Path(info["path"]).read_text(encoding="utf-8") == SRT
        assert Path(info["path"]).parent == tmp_path.resolve()
        # Both passes skip the media, refuse to expand a playlist, and the
        # fetch asks for the creator track only.
        assert all(c["skip_download"] and c["noplaylist"] for c in _FakeYDL.calls)
        fetch = _FakeYDL.calls[-1]
        assert fetch["writesubtitles"] is True and fetch["writeautomaticsub"] is False
        assert fetch["subtitleslangs"] == ["en"]

    async def test_runs_on_the_download_pool(self, fake_ytdlp, tmp_path):
        """yt-dlp can fail to return; it must never hold a default-executor worker."""
        await ys.extract_subtitles_only("https://x.test/v", output_dir=tmp_path)
        assert len(fake_ytdlp) == 1

    async def test_auto_only_source_fetches_the_auto_track(self, fake_ytdlp, tmp_path):
        _FakeYDL.info["subtitles"] = {}
        info = await ys.extract_subtitles_only("https://x.test/v", output_dir=tmp_path)
        assert info["is_auto_generated"] is True
        assert _FakeYDL.calls[-1]["writeautomaticsub"] is True

    async def test_no_captions_raises_and_lists_what_exists(self, fake_ytdlp, tmp_path):
        _FakeYDL.info["subtitles"] = {"it": [{}]}
        _FakeYDL.info["automatic_captions"] = {}
        _FakeYDL.info["language"] = None
        with pytest.raises(NoCaptionsError, match="Source offers: it"):
            await ys.extract_subtitles_only("https://x.test/v", output_dir=tmp_path, language="en")

    async def test_a_track_that_writes_no_file_is_a_no_captions_failure(self, fake_ytdlp, tmp_path):
        _FakeYDL.writes = None
        with pytest.raises(NoCaptionsError, match="could not be fetched"):
            await ys.extract_subtitles_only("https://x.test/v", output_dir=tmp_path)

    async def test_a_channel_link_is_refused(self, fake_ytdlp, tmp_path):
        _FakeYDL.info = {"_type": "playlist", "entries": [], "id": "chan"}
        with pytest.raises(VideoUnavailableError, match="single video"):
            await ys.extract_subtitles_only("https://x.test/@chan", output_dir=tmp_path)

    async def test_a_403_retries_once_without_cookies(self, fake_ytdlp, tmp_path):
        import yt_dlp
        _FakeYDL.raise_on = [yt_dlp.utils.DownloadError("ERROR: HTTP Error 403: Forbidden")]
        info = await ys.extract_subtitles_only("https://x.test/v", output_dir=tmp_path)
        assert info["language"] == "en"
        retry_calls = _FakeYDL.calls[1:]
        assert retry_calls and all("cookiefile" not in c for c in retry_calls)

    async def test_a_transient_error_retries_once(self, fake_ytdlp, tmp_path):
        import yt_dlp
        _FakeYDL.raise_on = [yt_dlp.utils.DownloadError("ERROR: Connection reset by peer")]
        info = await ys.extract_subtitles_only("https://x.test/v", output_dir=tmp_path)
        assert info["path"]

    async def test_a_second_failure_is_a_download_error_with_the_reason(self, fake_ytdlp, tmp_path):
        import yt_dlp
        _FakeYDL.raise_on = [
            yt_dlp.utils.DownloadError("ERROR: Connection reset by peer"),
            yt_dlp.utils.DownloadError("ERROR: Connection reset by peer"),
        ]
        with pytest.raises(DownloadError, match="Connection reset"):
            await ys.extract_subtitles_only("https://x.test/v", output_dir=tmp_path)

    async def test_a_permanent_error_is_not_retried(self, fake_ytdlp, tmp_path):
        import yt_dlp
        _FakeYDL.raise_on = [yt_dlp.utils.DownloadError("ERROR: Private video")]
        with pytest.raises(DownloadError, match="Private video"):
            await ys.extract_subtitles_only("https://x.test/v", output_dir=tmp_path)
        assert len(_FakeYDL.calls) == 1

    async def test_a_wedged_fetch_times_out_and_is_not_retried(self, monkeypatch, tmp_path):
        """A timed-out call still owns its worker; retrying would race it."""
        calls = []

        async def _hang(fn, *a, **k):
            calls.append(fn)
            await asyncio.sleep(3600)

        monkeypatch.setattr(ys.download_pool, "run", _hang)
        monkeypatch.setattr(ys, "SUBTITLE_FETCH_TIMEOUT_S", 0.05)
        with pytest.raises(DownloadTimeoutError):
            await ys.extract_subtitles_only("https://x.test/v", output_dir=tmp_path)
        assert len(calls) == 1


# ── run_tool_transcript ─────────────────────────────────────────────────────

@pytest.fixture
def terminal_spies():
    with patch.object(tr, "_tool_progress", new=AsyncMock()) as prog, \
         patch.object(tr, "_tool_success", new=AsyncMock(return_value=True)) as ok, \
         patch.object(tr, "_tool_fail", new=AsyncMock()) as fail:
        yield {"progress": prog, "success": ok, "fail": fail}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    out = tmp_path / "out"
    scratch_root = tmp_path / "in"
    out.mkdir()
    scratch_root.mkdir()
    monkeypatch.setattr("backend.api.tools.tool_out_path",
                        lambda job_id, ext=".mp4": out / f"{job_id}{ext}")
    monkeypatch.setattr("backend.api.tools.tools_in_dir", lambda: scratch_root)
    return {"out": out, "scratch_root": scratch_root}


def _extract_writing(content: str, name: str = "subs_abc.en.srt", **over):
    """An extract_subtitles_only stub that writes `content` into the scratch dir
    the runner hands it, exactly as the real one does."""
    seen = {}

    async def _fake(url, output_dir, language=None, prefer_manual=True):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        p = output_dir / name
        p.write_text(content, encoding="utf-8")
        seen["output_dir"] = output_dir
        payload = {"path": str(p), "language": "en", "track": "en", "format": "srt",
                   "is_auto_generated": False, "title": "A video", "duration": 6}
        payload.update(over)
        return payload

    return AsyncMock(side_effect=_fake), seen


@pytest.mark.asyncio
class TestRunner:
    async def test_txt_is_the_words_only(self, terminal_spies, dirs):
        stub, _ = _extract_writing(SRT)
        with patch("backend.services.ytdlp_service.extract_subtitles_only", new=stub):
            await tr.run_tool_transcript("job1", "https://x.test/v", fmt="txt")

        terminal_spies["fail"].assert_not_awaited()
        body = (dirs["out"] / "job1.txt").read_text(encoding="utf-8")
        assert "Hello there" in body and "This is the second cue" in body
        assert "-->" not in body

    async def test_srt_keeps_timings(self, terminal_spies, dirs):
        stub, _ = _extract_writing(SRT)
        with patch("backend.services.ytdlp_service.extract_subtitles_only", new=stub):
            await tr.run_tool_transcript("job2", "https://x.test/v", fmt="srt")
        body = (dirs["out"] / "job2.srt").read_text(encoding="utf-8")
        assert "00:00:01,000 --> 00:00:03,000" in body

    async def test_vtt_from_an_srt_source(self, terminal_spies, dirs):
        stub, _ = _extract_writing(SRT)
        with patch("backend.services.ytdlp_service.extract_subtitles_only", new=stub):
            await tr.run_tool_transcript("job3", "https://x.test/v", fmt="vtt")
        body = (dirs["out"] / "job3.vtt").read_text(encoding="utf-8")
        assert body.startswith("WEBVTT")
        assert "00:00:01.000 --> 00:00:03.000" in body

    async def test_a_vtt_source_with_header_and_tags_parses(self, terminal_spies, dirs):
        vtt = ("WEBVTT\nKind: captions\nLanguage: en\n\n"
               "00:00:00.500 --> 00:00:02.000 align:start position:0%\n<c>first</c> line\n\n"
               "00:00:02.000 --> 00:00:04.000\nsecond line\n")
        stub, _ = _extract_writing(vtt, name="subs_abc.en.vtt", format="vtt")
        with patch("backend.services.ytdlp_service.extract_subtitles_only", new=stub):
            await tr.run_tool_transcript("job3b", "https://x.test/v", fmt="txt")
        body = (dirs["out"] / "job3b.txt").read_text(encoding="utf-8")
        assert body.strip() == "first line second line"

    async def test_a_held_caption_is_one_line_not_two(self, terminal_spies, dirs):
        held = ("1\n00:00:01,000 --> 00:00:02,000\nsame words\n\n"
                "2\n00:00:02,000 --> 00:00:03,000\nsame words\n\n"
                "3\n00:00:03,000 --> 00:00:04,000\nnext\n")
        stub, _ = _extract_writing(held)
        with patch("backend.services.ytdlp_service.extract_subtitles_only", new=stub):
            await tr.run_tool_transcript("job3c", "https://x.test/v", fmt="srt")
        body = (dirs["out"] / "job3c.srt").read_text(encoding="utf-8")
        assert body.count("same words") == 1
        assert "00:00:01,000 --> 00:00:03,000" in body

    async def test_reports_language_and_auto_flag(self, terminal_spies, dirs):
        stub, _ = _extract_writing(SRT, language="es", is_auto_generated=True)
        with patch("backend.services.ytdlp_service.extract_subtitles_only", new=stub):
            await tr.run_tool_transcript("job4", "https://x.test/v", fmt="txt")
        extra = terminal_spies["success"].call_args.kwargs["extra_output"]
        assert extra["language"] == "es"
        assert extra["is_auto_generated"] is True
        assert extra["segment_count"] == 2
        assert extra["source_url"] == "https://x.test/v"

    async def test_the_scratch_dir_is_removed_and_the_output_kept(self, terminal_spies, dirs):
        stub, seen = _extract_writing(SRT)
        with patch("backend.services.ytdlp_service.extract_subtitles_only", new=stub):
            await tr.run_tool_transcript("job5", "https://x.test/v", fmt="txt")
        assert seen["output_dir"].parent == dirs["scratch_root"]
        assert not seen["output_dir"].exists()
        assert (dirs["out"] / "job5.txt").exists()

    async def test_the_scratch_dir_is_removed_on_failure_too(self, terminal_spies, dirs):
        stub, seen = _extract_writing("not a subtitle file")
        with patch("backend.services.ytdlp_service.extract_subtitles_only", new=stub):
            await tr.run_tool_transcript("job5b", "https://x.test/v", fmt="txt")
        terminal_spies["fail"].assert_awaited()
        assert not seen["output_dir"].exists()

    async def test_no_captions_names_the_alternative_and_never_transcribes(self, terminal_spies, dirs):
        boom = AsyncMock(side_effect=NoCaptionsError(
            "No captions available in ['en']. Source offers: none."))
        with patch("backend.services.ytdlp_service.extract_subtitles_only", new=boom), \
             patch("backend.services.whisper_service.whisper_service.transcribe",
                   new=AsyncMock()) as whisper:
            await tr.run_tool_transcript("job6", "https://x.test/v", fmt="txt")

        whisper.assert_not_awaited()
        terminal_spies["success"].assert_not_awaited()
        message = str(terminal_spies["fail"].call_args.args[1])
        assert "Source offers: none." in message
        assert "Export Subtitles" in message

    async def test_an_unreadable_track_fails_instead_of_writing_an_empty_file(self, terminal_spies, dirs):
        stub, _ = _extract_writing("not a subtitle file at all")
        with patch("backend.services.ytdlp_service.extract_subtitles_only", new=stub):
            await tr.run_tool_transcript("job7", "https://x.test/v", fmt="txt")
        terminal_spies["success"].assert_not_awaited()
        assert "no readable cues" in str(terminal_spies["fail"].call_args.args[1])
        assert not (dirs["out"] / "job7.txt").exists()

    async def test_other_errors_route_through_tool_fail_unchanged(self, terminal_spies, dirs):
        with patch("backend.services.ytdlp_service.extract_subtitles_only",
                   new=AsyncMock(side_effect=DownloadError("network gone"))):
            await tr.run_tool_transcript("job8", "https://x.test/v", fmt="txt")
        message = str(terminal_spies["fail"].call_args.args[1])
        assert message == "network gone"


@pytest.mark.asyncio
class TestLanguageWarning:
    async def _run(self, dirs, requested, got):
        sent: list[str] = []

        async def _capture(**kwargs):
            sent.append(kwargs.get("message", ""))

        stub, _ = _extract_writing(SRT, language=got)
        with patch("backend.services.ytdlp_service.extract_subtitles_only", new=stub), \
             patch("backend.core.ws_manager.ws_manager.send_constraint_warning",
                   new=AsyncMock(side_effect=_capture)):
            await tr.run_tool_transcript("jobw", "https://x.test/v", language=requested, fmt="txt")
        return sent

    async def test_a_substituted_language_is_announced(self, terminal_spies, dirs):
        sent = await self._run(dirs, "sw", "en")
        terminal_spies["success"].assert_awaited()
        assert any("no 'sw' captions" in m and "'en' track" in m for m in sent)

    async def test_the_requested_language_says_nothing(self, terminal_spies, dirs):
        assert await self._run(dirs, "en", "en") == []

    async def test_a_regional_request_answered_by_its_base_says_nothing(self, terminal_spies, dirs):
        # The service reports the BASE code ("es" for an es-419 track).
        assert await self._run(dirs, "es-419", "es") == []

    async def test_auto_never_warns(self, terminal_spies, dirs):
        assert await self._run(dirs, None, "de") == []


# ── Endpoint ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    from starlette.testclient import TestClient

    from backend.main import create_app
    from backend.messaging import manager as messaging_manager

    async def _noop(*a, **k):
        return None
    messaging_manager.messaging.start_all = _noop      # type: ignore[assignment]
    messaging_manager.messaging.stop_all = _noop       # type: ignore[assignment]
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def dispatched(monkeypatch):
    spawned: list = []

    def fake_dispatch(coro, *a, **k):
        spawned.append(coro.cr_frame.f_locals.copy() if coro.cr_frame else {})
        coro.close()

    monkeypatch.setattr("backend.core.task_runner.dispatch", fake_dispatch)
    return spawned


class TestEndpoint:
    def test_creates_a_tool_transcript_job(self, client, dispatched):
        r = client.post("/api/tools/transcript",
                        data={"url": " https://www.youtube.com/watch?v=x ", "language": "EN",
                              "format": "srt"})
        assert r.status_code == 200, r.text
        job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
        assert job["job_type"] == "tool:transcript"
        assert dispatched[0]["url"] == "https://www.youtube.com/watch?v=x"
        assert dispatched[0]["language"] == "en"
        assert dispatched[0]["fmt"] == "srt"

    def test_empty_language_means_auto(self, client, dispatched):
        r = client.post("/api/tools/transcript", data={"url": "https://x.test/v"})
        assert r.status_code == 200, r.text
        assert dispatched[0]["language"] is None
        assert dispatched[0]["fmt"] == "txt"

    @pytest.mark.parametrize("url", ["", "ftp://x.test/v", "javascript:alert(1)", "x.test/v",
                                     "https://x.test/" + "a" * 2001])
    def test_refuses_a_non_http_url(self, client, dispatched, url):
        r = client.post("/api/tools/transcript", data={"url": url})
        assert r.status_code in (400, 422)
        assert dispatched == []

    @pytest.mark.parametrize("lang", ["eng", "e1", "en-US", "../"])
    def test_refuses_a_malformed_language(self, client, dispatched, lang):
        r = client.post("/api/tools/transcript", data={"url": "https://x.test/v", "language": lang})
        assert r.status_code == 400
        assert dispatched == []

    def test_refuses_an_unknown_format(self, client, dispatched):
        r = client.post("/api/tools/transcript", data={"url": "https://x.test/v", "format": "ass"})
        assert r.status_code == 422


# ── Page wiring ─────────────────────────────────────────────────────────────

class TestWiring:
    def test_route_and_hub_card_exist(self):
        app = (REPO / "frontend/src/App.jsx").read_text()
        assert 'import("./pages/tools/Transcript")' in app
        assert re.search(r'path="tools/transcript"\s+element=\{<ToolTranscript />\}', app)
        hub = (REPO / "frontend/src/pages/Tools.jsx").read_text()
        assert re.search(r'id:\s*"transcript"', hub)

    def test_page_posts_to_the_endpoint(self):
        page = (REPO / "frontend/src/pages/tools/Transcript.jsx").read_text()
        assert 'endpoint="/api/tools/transcript"' in page
        assert page.startswith("// SPDX-License-Identifier: AGPL-3.0-only")

    def test_the_job_type_is_a_library_producer(self):
        """A tool job that writes a file and is missing from the taxonomy is
        invisible in the Library."""
        from backend.services.library_taxonomy import MEDIA_DOC, PRODUCERS
        assert PRODUCERS["tool:transcript"].media == MEDIA_DOC
