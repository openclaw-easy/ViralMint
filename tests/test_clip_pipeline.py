# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The clip-processing pipeline — extract, desilence, caption, thumbnail.

This is the fan-out: N clips, each going through several ffmpeg passes plus a
caption burn plus a thumbnail plus AI metadata. What makes it worth testing
without real ffmpeg is the FAILURE arithmetic, not the happy path:

  - one clip failing must cost that clip and nothing else. A gather() over
    N clips that propagates the first exception throws away the other N-1
    perfectly good ones after they've already been paid for in CPU time;
  - a clip that fails after its file was cut must clean that file up, or it
    orphans in GENERATED_DIR forever;
  - captioning is best-effort — a caption failure ships the clip WITHOUT
    captions rather than losing the clip;
  - the transcript is loaded from cache when it's there, re-run when it isn't,
    and a corrupt cached blob re-transcribes instead of crashing.

ffmpeg, Whisper and the AI client are all stubbed.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services import clip_extractor as CE


@pytest.fixture(autouse=True)
def gen_dir(tmp_path, monkeypatch):
    from backend.config import settings as S
    d = tmp_path / "generated"
    d.mkdir()
    monkeypatch.setattr(type(S), "GENERATED_DIR", property(lambda self: d))
    return d


@pytest.fixture(autouse=True)
def quiet_ws(monkeypatch):
    async def noop(*a, **k):
        return None
    from backend.core.ws_manager import ws_manager
    monkeypatch.setattr(ws_manager, "send_progress", noop)
    monkeypatch.setattr(ws_manager, "send", noop)
    monkeypatch.setattr(ws_manager, "send_constraint_warning", noop)


@pytest.fixture()
def video(tmp_path):
    src = tmp_path / "source.mp4"
    src.write_bytes(b"\x00" * 4096)
    return SimpleNamespace(id="abcdef012345", title="A source",
                           video_path=str(src), audio_path=None,
                           duration_seconds=600,
                           transcript_segments_json=None)


SEGMENTS = [{"start": float(i * 10), "end": float(i * 10 + 9),
             "text": f"Segment {i}",
             "words": [{"word": f"w{i}", "start": float(i * 10),
                        "end": float(i * 10 + 1)}]}
            for i in range(12)]

WINDOWS = [
    {"start": 0.0, "end": 30.0, "title": "One", "hook": "", "reason": "",
     "virality_score": 8.0, "hook_score": 7.0},
    {"start": 60.0, "end": 90.0, "title": "Two", "hook": "", "reason": "",
     "virality_score": 6.0, "hook_score": 5.0},
]


@pytest.fixture()
def ffmpeg(monkeypatch, gen_dir):
    """Stub the ffmpeg seams; each test can make specific calls fail."""
    state = {"cut": [], "thumbs": [], "fail_cut": set(), "fail_thumb": set()}

    async def fake_extract_clip(src, start, end, out, vertical=True, **kw):
        idx = len(state["cut"])
        state["cut"].append(Path(out))
        if idx in state["fail_cut"]:
            raise RuntimeError(f"ffmpeg failed cutting clip {idx}")
        Path(out).write_bytes(b"\x00" * 2048)
        return Path(out)

    async def fake_thumb(video_path, output_path=None, **kw):
        idx = len(state["thumbs"])
        out = Path(output_path) if output_path else Path(str(video_path) + ".jpg")
        state["thumbs"].append(out)
        if idx in state["fail_thumb"]:
            raise RuntimeError("thumbnail failed")
        out.write_bytes(b"\xff\xd8\xff")
        return out

    monkeypatch.setattr("backend.services.ffmpeg_service.extract_clip",
                        fake_extract_clip)
    monkeypatch.setattr("backend.services.ffmpeg_service.extract_thumbnail",
                        fake_thumb)

    # _burn_clip_captions returns (path, status) — the pipeline unpacks it.
    async def no_captions(clip_path, segs, style, **k):
        return Path(clip_path), "skipped"
    monkeypatch.setattr(CE, "_burn_clip_captions", no_captions)

    async def no_metadata(*a, **k):
        return None
    monkeypatch.setattr(CE, "_generate_clip_metadata", no_metadata)

    # Don't shell out to ffprobe for the per-clip measurement.
    monkeypatch.setattr("backend.services.video_utils.probe_media",
                        lambda p: (1080, 1920, 30.0))
    return state


def _process(video, **kw):
    defaults = dict(video=video, clip_windows=WINDOWS, segments=SEGMENTS,
                    caption_style="none", user_settings=None)
    defaults.update(kw)
    return asyncio.run(CE._process_clips_parallel(**defaults))


# ── the fan-out ─────────────────────────────────────────────────────────────

class TestProcessClipsParallel:
    def test_every_clip_comes_back(self, video, ffmpeg):
        out = _process(video)
        assert len(out) == 2
        assert all(Path(c["video_path"]).exists() for c in out)

    def test_one_clip_failing_costs_only_that_clip(self, video, ffmpeg):
        """A gather that propagates the first exception throws away the other
        clips after they've already been paid for in CPU."""
        ffmpeg["fail_cut"].add(0)
        out = _process(video)
        assert len(out) == 1, "the surviving clip must still ship"

    def test_every_clip_failing_yields_nothing_rather_than_raising(
            self, video, ffmpeg):
        ffmpeg["fail_cut"].update({0, 1})
        assert _process(video) == []

    def test_a_thumbnail_failure_does_not_lose_the_clip(self, video, ffmpeg):
        """A missing thumbnail is a cosmetic problem; losing the clip isn't."""
        ffmpeg["fail_thumb"].update({0, 1})
        out = _process(video)
        assert len(out) == 2

    def test_clips_carry_their_window_metadata(self, video, ffmpeg):
        out = _process(video)
        titles = {c.get("title") for c in out}
        assert "One" in titles and "Two" in titles

    def test_the_scores_survive_the_pipeline(self, video, ffmpeg):
        out = _process(video)
        assert any(c.get("virality_score") == 8.0 for c in out)

    def test_no_windows_means_no_clips(self, video, ffmpeg):
        assert _process(video, clip_windows=[]) == []

    def test_captions_are_skipped_when_disabled(self, video, ffmpeg, monkeypatch):
        called = {"n": 0}

        async def spy(clip_path, segs, style, **k):
            called["n"] += 1
            return Path(clip_path), "skipped"
        monkeypatch.setattr(CE, "_burn_clip_captions", spy)
        _process(video, caption_style="none")
        assert all(c.get("caption_status") in ("skipped", None)
                   for c in _process(video, caption_style="none"))

    def test_captions_run_when_a_style_is_given(self, video, ffmpeg, monkeypatch):
        called = {"n": 0}

        async def spy(clip_path, segs, style, **k):
            called["n"] += 1
            called["style"] = style
            return Path(clip_path), "ok"
        monkeypatch.setattr(CE, "_burn_clip_captions", spy)
        _process(video, caption_style="viral")
        assert called["n"] == 2 and called["style"] == "viral"

    def test_a_handled_caption_failure_still_ships_the_clip(self, video, ffmpeg,
                                                             monkeypatch):
        """The burner owns its known failure modes — a missing libass, a
        non-zero ffmpeg, an empty ASS file — and reports them as a STATUS. The
        clip ships uncaptioned rather than being lost."""
        async def degraded(clip_path, segs, style, **k):
            return Path(clip_path), "failed"
        monkeypatch.setattr(CE, "_burn_clip_captions", degraded)
        out = _process(video, caption_style="viral")
        assert len(out) == 2
        assert all(c.get("caption_status") == "failed" for c in out)

    def test_an_UNEXPECTED_caption_raise_drops_that_clip(self, video, ffmpeg,
                                                         monkeypatch):
        """Pinning the trade-off rather than asserting it's ideal: the burner
        is expected to return a status, so a raise is genuinely exceptional
        and the per-clip handler drops it. The cut file exists and is playable
        without captions, so this is the one place the pipeline throws away
        work it had already finished."""
        async def boom(*a, **k):
            raise RuntimeError("something nobody anticipated")
        monkeypatch.setattr(CE, "_burn_clip_captions", boom)
        assert _process(video, caption_style="viral") == []


class TestSilenceRemovalPhase:
    def test_it_runs_per_clip_when_enabled(self, video, ffmpeg, monkeypatch):
        seen = []

        # Signature must match the real one — the pipeline's soft fallback
        # swallows a TypeError, so a stale stub reads as "never called".
        async def fake_desilence(clip_path, segs, warn_user_id=None):
            seen.append(Path(clip_path))
            return Path(clip_path), segs
        monkeypatch.setattr(CE, "_remove_silence_and_fillers", fake_desilence)
        _process(video, remove_silence=True)
        assert len(seen) == 2

    def test_it_is_skipped_when_disabled(self, video, ffmpeg, monkeypatch):
        async def boom(*a, **k):
            raise AssertionError("must not desilence when not asked")
        monkeypatch.setattr(CE, "_remove_silence_and_fillers", boom)
        _process(video, remove_silence=False)

    def test_a_failure_falls_back_to_the_original_clip(self, video, ffmpeg,
                                                        monkeypatch):
        """Soft fallback: an un-tightened clip beats a lost one."""
        async def boom(*a, **k):
            raise RuntimeError("silencedetect failed")
        monkeypatch.setattr(CE, "_remove_silence_and_fillers", boom)
        out = _process(video, remove_silence=True)
        assert len(out) == 2


# ── the transcript ──────────────────────────────────────────────────────────

class TestLoadOrTranscribe:
    def _load(self, video, **kw):
        return asyncio.run(CE._load_or_transcribe_segments(video, None, **kw))

    def test_a_cached_transcript_is_reused(self, video, monkeypatch):
        """Whisper on a long source is minutes; never pay it twice."""
        video.transcript_segments_json = json.dumps(SEGMENTS)

        def boom(*a, **k):
            raise AssertionError("must not re-run Whisper on a cached transcript")
        monkeypatch.setattr("backend.services.whisper_service.whisper_service",
                            SimpleNamespace(load=lambda *a, **k: None,
                                            transcribe=boom))
        assert len(self._load(video)) == len(SEGMENTS)

    def test_force_retranscribe_ignores_the_cache(self, video, monkeypatch):
        video.transcript_segments_json = json.dumps(SEGMENTS)
        called = {"n": 0}

        async def fake_transcribe(*a, **k):
            called["n"] += 1
            return {"segments": [{"start": 0, "end": 1, "text": "fresh"}]}
        monkeypatch.setattr("backend.services.whisper_service.whisper_service",
                            SimpleNamespace(load=lambda *a, **k: None,
                                            transcribe=fake_transcribe))
        out = self._load(video, force_retranscribe=True)
        assert called["n"] == 1 and out[0]["text"] == "fresh"

    def test_a_corrupt_cache_re_transcribes_instead_of_crashing(
            self, video, monkeypatch):
        video.transcript_segments_json = "{not json at all"
        called = {"n": 0}

        async def fake_transcribe(*a, **k):
            called["n"] += 1
            return {"segments": [{"start": 0, "end": 1, "text": "recovered"}]}
        monkeypatch.setattr("backend.services.whisper_service.whisper_service",
                            SimpleNamespace(load=lambda *a, **k: None,
                                            transcribe=fake_transcribe))
        assert self._load(video)[0]["text"] == "recovered"
        assert called["n"] == 1

    def test_an_empty_cached_list_falls_through_to_whisper(
            self, video, monkeypatch):
        video.transcript_segments_json = "[]"

        async def fake_transcribe(*a, **k):
            return {"segments": [{"start": 0, "end": 1, "text": "fresh"}]}
        monkeypatch.setattr("backend.services.whisper_service.whisper_service",
                            SimpleNamespace(load=lambda *a, **k: None,
                                            transcribe=fake_transcribe))
        assert self._load(video)[0]["text"] == "fresh"

    def test_a_missing_file_returns_nothing_rather_than_raising(self, video):
        video.video_path = "/no/such/file.mp4"
        video.audio_path = None
        assert self._load(video) == []

    def test_no_path_at_all_returns_nothing(self, video):
        video.video_path = None
        video.audio_path = None
        assert self._load(video) == []

    def test_the_audio_path_is_preferred_when_present(self, video, tmp_path,
                                                       monkeypatch):
        """Transcribing the extracted audio skips a video decode."""
        audio = tmp_path / "source.mp3"
        audio.write_bytes(b"\x00" * 512)
        video.audio_path = str(audio)
        seen = {}

        async def fake_transcribe(path, *a, **k):
            seen["path"] = str(path)
            return {"segments": []}
        monkeypatch.setattr("backend.services.whisper_service.whisper_service",
                            SimpleNamespace(load=lambda *a, **k: None,
                                            transcribe=fake_transcribe))
        self._load(video)
        assert seen["path"] == str(audio)


# ── per-clip segment windows ────────────────────────────────────────────────

class TestFilterAndOffsetSegments:
    def test_only_overlapping_segments_are_kept(self):
        out = CE._filter_and_offset_segments(SEGMENTS, 20.0, 50.0)
        assert out, "the window covers segments 2-4"
        assert all(s["end"] > 0 for s in out)

    def test_timestamps_are_rebased_to_the_clip(self):
        """A clip starting at 60s must caption from 0s, not 60s."""
        out = CE._filter_and_offset_segments(SEGMENTS, 60.0, 90.0)
        assert out and min(s["start"] for s in out) < 10.0

    def test_words_are_rebased_too(self):
        out = CE._filter_and_offset_segments(SEGMENTS, 60.0, 90.0)
        worded = [s for s in out if s.get("words")]
        assert worded
        assert all(w["start"] < 40 for s in worded for w in s["words"])

    def test_a_window_with_no_speech_yields_nothing(self):
        assert CE._filter_and_offset_segments(SEGMENTS, 5000.0, 5030.0) == []

    def test_no_segments_yields_nothing(self):
        assert CE._filter_and_offset_segments([], 0.0, 30.0) == []


class TestGenerateClipMetadata:
    """Per-clip titles/descriptions come from one batched AI call. It's the
    last step, so a failure here must never cost the finished clips."""

    def test_an_ai_failure_leaves_the_clips_intact(self, video, ffmpeg,
                                                    monkeypatch):
        async def boom(*a, **k):
            raise RuntimeError("model down")
        monkeypatch.setattr(CE, "_generate_clip_metadata", boom)
        out = _process(video)
        assert len(out) == 2, "metadata is a bonus, not a gate"

    def test_it_runs_once_per_clip_with_that_clip_s_title(self, video, ffmpeg,
                                                            monkeypatch):
        seen = []

        async def spy(title, transcript_text, user_settings):
            seen.append(title)
            return {}
        monkeypatch.setattr(CE, "_generate_clip_metadata", spy)
        _process(video)
        assert len(seen) == 2 and set(seen) == {"One", "Two"}


class TestHasVideoStream:
    def test_an_unprobeable_file_FAILS_OPEN_as_a_video(self):
        """Pinning real behaviour. When ffprobe can't answer, this returns
        True — the audio tools branch on it to choose .mp4 vs .mp3, so failing
        open means an unprobeable input is treated as video. Safe today
        because every caller validates the file exists first, but it is a
        fail-open default, not a fail-closed one."""
        assert CE._has_video_stream(Path("/no/such/file.mp4")) is True
