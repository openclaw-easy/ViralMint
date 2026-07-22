# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for `_select_clip_windows_with_retries` — user-bound discipline.

The retry cascade exists so the AI gets a second/third chance on a video
where attempt 1 found nothing. Pre-2026-05-26 the retries hardcoded
`min=10/max=90` and then `min=5/max=120`, completely overriding whatever
duration range the user typed — so a request for 10-20s clips on a
sparse-speech video silently shipped a 53s clip. These tests lock in the
fix: user-pinned bounds are non-negotiable in retries.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.services import clip_extractor
from backend.services.clip_options import ExtractOptions


@pytest.fixture
def fake_segments():
    """Minimal segment list — content doesn't matter; we mock the AI call."""
    return [{"start": 0.0, "end": 1.6, "text": "hello"}]


async def _run_retries(
    fake_segments, min_duration, max_duration,
    mock_calls: list[list[dict] | None],
):
    """Helper: stub `_select_clip_windows` to return successive responses
    from `mock_calls`. Each entry is the value returned by one call (None
    or [] means "AI found nothing this attempt")."""
    mock = AsyncMock(side_effect=[r or [] for r in mock_calls])
    with patch.object(clip_extractor, "_select_clip_windows", mock):
        result = await clip_extractor._select_clip_windows_with_retries(
            segments=fake_segments,
            title="Test",
            duration=63,
            max_clips=3,
            user_settings=None,
            min_duration=min_duration,
            max_duration=max_duration,
        )
    return result, mock


class TestUserPinnedBoth:
    """When the user typed BOTH min and max, the cascade must NOT widen the
    duration window. _select_clip_windows internally retries the AI 2x
    already, so a third call with the same bounds is unlikely to help."""

    @pytest.mark.asyncio
    async def test_no_relax_when_both_bounds_pinned(self, fake_segments):
        # AI finds nothing on attempt 1 — we should NOT retry with relaxed bounds.
        result, mock = await _run_retries(
            fake_segments, min_duration=10, max_duration=20,
            mock_calls=[[]],
        )
        assert result == []
        # Exactly ONE call — no relaxation retries.
        assert mock.call_count == 1
        # And that one call used the user's exact bounds.
        kwargs = mock.call_args.kwargs
        assert kwargs["min_duration"] == 10
        assert kwargs["max_duration"] == 20

    @pytest.mark.asyncio
    async def test_returns_attempt1_clips_when_present(self, fake_segments):
        # AI found valid clips on attempt 1 — no retry needed.
        clip = {"start": 5.0, "end": 17.0, "virality_score": 8}
        result, mock = await _run_retries(
            fake_segments, min_duration=10, max_duration=20,
            mock_calls=[[clip]],
        )
        assert result == [clip]
        assert mock.call_count == 1


class TestAutoMode:
    """When the user did NOT pin bounds, the cascade should preserve the
    historical behavior: relax to 10-90s, then to 5-120s. Anyone relying
    on the auto-mode wide net (a user who clicked 'Extract' without
    touching the range fields) sees identical behavior."""

    @pytest.mark.asyncio
    async def test_cascades_through_all_three_attempts(self, fake_segments):
        clip = {"start": 0.0, "end": 60.0, "virality_score": 5}
        result, mock = await _run_retries(
            fake_segments, min_duration=None, max_duration=None,
            mock_calls=[[], [], [clip]],
        )
        assert result == [clip]
        assert mock.call_count == 3
        # Attempt 1: user bounds (both None — defaults computed internally).
        a1 = mock.call_args_list[0].kwargs
        assert a1["min_duration"] is None
        assert a1["max_duration"] is None
        # Attempt 2: hardcoded 10-90 relax.
        a2 = mock.call_args_list[1].kwargs
        assert a2["min_duration"] == 10
        assert a2["max_duration"] == 90
        # Attempt 3: hardcoded 5-120 permissive.
        a3 = mock.call_args_list[2].kwargs
        assert a3["min_duration"] == 5
        assert a3["max_duration"] == 120


class TestOneSidedPin:
    """When the user pinned only ONE bound, the cascade should widen only
    the side they left blank — never override what they typed."""

    @pytest.mark.asyncio
    async def test_pinned_max_only_preserves_max_on_retry(self, fake_segments):
        # User said "no longer than 30s" but left min blank.
        result, mock = await _run_retries(
            fake_segments, min_duration=None, max_duration=30,
            mock_calls=[[], [], []],
        )
        assert result == []
        assert mock.call_count == 3
        # Attempt 2: relax min to 10, but keep user's max=30.
        a2 = mock.call_args_list[1].kwargs
        assert a2["min_duration"] == 10
        assert a2["max_duration"] == 30, "user's max must not be widened"
        # Attempt 3: relax min to 5, but STILL keep user's max=30.
        a3 = mock.call_args_list[2].kwargs
        assert a3["min_duration"] == 5
        assert a3["max_duration"] == 30, "user's max must not be widened"

    @pytest.mark.asyncio
    async def test_pinned_min_only_preserves_min_on_retry(self, fake_segments):
        # User said "at least 25s" but left max blank.
        result, mock = await _run_retries(
            fake_segments, min_duration=25, max_duration=None,
            mock_calls=[[], [], []],
        )
        assert result == []
        assert mock.call_count == 3
        # Attempt 2: keep user's min=25, widen max to 90.
        a2 = mock.call_args_list[1].kwargs
        assert a2["min_duration"] == 25, "user's min must not be lowered"
        assert a2["max_duration"] == 90
        # Attempt 3: keep user's min=25, widen max to 120.
        a3 = mock.call_args_list[2].kwargs
        assert a3["min_duration"] == 25, "user's min must not be lowered"
        assert a3["max_duration"] == 120


class TestUserPinnedBothErrorMessage:
    """When the retry cascade returns [] and the user pinned both
    bounds, `extract_viral_clips` raises a TAILORED error that names
    the user's range. The generic "AI could not identify suitable
    clip segments" message would be misleading — the user already
    set a custom range, telling them to "try setting a custom duration
    range" is unhelpful.
    """

    class _FakeVideo:
        """Stand-in for the DownloadedVideo SQLAlchemy row that
        extract_viral_clips reads. Only the attrs the function touches."""

        def __init__(self):
            self.id = "test_video_abcdef"
            self.title = "Sparse podcast"
            self.duration_seconds = 90
            self.video_path = "/tmp/fake.mp4"
            self.audio_path = "/tmp/fake.mp3"
            self.transcript_segments_json = None
            self.transcript = None
            self.transcript_language = None

    @pytest.mark.asyncio
    async def test_pinned_range_appears_verbatim_in_error(self):
        """User asked 10-20s and even the duration-split fallback can't
        fit a clip → error must call out the specific 10-20s range so
        the user knows to widen it.

        Test path was retuned 2026-05-28: the original assertion fired
        directly off the AI-empty raise, but `extract_viral_clips` now
        falls back to a duration-split when the AI returns nothing
        (commit f3ddfc2 — "fall back to duration-split when AI returns
        no windows"). The user-range error message survives in the
        nested raise that fires when even the duration splitter can't
        emit a window — we exercise that path here by mocking the
        splitter to return []. Contract preserved: user-pinned bounds
        appearing in the failure message when there is NO way to ship
        a clip.
        """
        video = self._FakeVideo()
        fake_segments = [
            {"start": 0.0, "end": 5.0, "text": "lots of words " * 20},
        ]

        with (
            patch.object(
                clip_extractor, "_load_or_transcribe_segments",
                new=AsyncMock(return_value=fake_segments),
            ),
            patch.object(
                clip_extractor, "_select_clip_windows",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                clip_extractor, "_generate_duration_based_clips",
                new=lambda *a, **kw: [],
            ),
        ):
            with pytest.raises(ValueError) as exc:
                await clip_extractor.extract_viral_clips(
                    video=video,
                    user_settings=None,
                    opts=ExtractOptions(max_clips=3, min_duration=10, max_duration=20),
                )

        msg = str(exc.value)
        assert "10-20s range" in msg, (
            "Error must name the user's exact range so they can widen "
            "the duration parameter rather than guess at what failed"
        )
        # Generic copy from the auto-mode branch must NOT leak through.
        assert "set custom clip duration range" not in msg, (
            "The generic 'set custom duration range' suggestion is "
            "actively misleading when the user already did so"
        )

    @pytest.mark.asyncio
    async def test_auto_mode_keeps_generic_error_message(self):
        """When the user did NOT pin both bounds and even the duration-
        split fallback can't ship a clip, the error copy must NOT name
        a non-existent user range AND must steer the user toward the
        levers they haven't tried (min duration, longer source). The
        original "set custom clip duration range" assertion was retuned
        on 2026-05-28 (commit f3ddfc2) when the duration-split fallback
        was inserted — that copy now lives only when even the splitter
        returns nothing, and the message itself was reworded then.
        """
        video = self._FakeVideo()
        fake_segments = [
            {"start": 0.0, "end": 5.0, "text": "lots of words " * 20},
        ]

        with (
            patch.object(
                clip_extractor, "_load_or_transcribe_segments",
                new=AsyncMock(return_value=fake_segments),
            ),
            patch.object(
                clip_extractor, "_select_clip_windows",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                clip_extractor, "_generate_duration_based_clips",
                new=lambda *a, **kw: [],
            ),
        ):
            with pytest.raises(ValueError) as exc:
                await clip_extractor.extract_viral_clips(
                    video=video,
                    user_settings=None,
                    # min/max both omitted — auto mode.
                    opts=ExtractOptions(max_clips=3),
                )

        msg = str(exc.value)
        # New auto-mode failure copy steers toward the levers the user
        # hasn't pulled: min duration + source length.
        assert "lowering the min duration" in msg, (
            "Auto-mode failure should suggest lowering the min duration "
            "as one of the levers the user hasn't tried yet"
        )
        assert "longer video" in msg, (
            "Auto-mode failure should suggest using a longer video as "
            "one of the levers the user hasn't tried yet"
        )
        # Pinned-bounds error copy must NOT leak into the auto-mode branch.
        assert "range" not in msg.lower() or "the requested range" in msg, (
            "Auto-mode error must not name a 'range' the user didn't set"
        )
