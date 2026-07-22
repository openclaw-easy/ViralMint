# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for the manual-mode extraction path (2026-05-26).

Three layers locked in:

1. **`_parse_timestamp`** — the flexible timestamp parser shared by the
   API validator + (transitively) the documentation. Tests cover every
   accepted form (SS / MM:SS / HH:MM:SS, with and without fractional
   seconds, and numeric inputs) plus the reject cases (empty, negative,
   too many colons, non-numeric, seconds/minutes ≥ 60 in multi-part).

2. **`_build_manual_clip_windows`** — pure builder that converts the
   validated range list into the dict shape `_process_clips_parallel`
   expects. Tests cover the title format, the `reason`/`hook` empty-
   string contract (downstream `_clip_title` falls through to "clip N"),
   and defensive clamping when the cached duration drifts.

3. **`_validate_manual_time_ranges`** (API layer) — parses + bounds-
   checks user input, surfaces per-row 400 errors. Tests cover happy
   path, sorting, every error branch (empty, too many, non-dict entry,
   parse failure, inverted, sub-1s, beyond-duration).

The end-to-end branch in `extract_viral_clips` itself is integration-
heavy (Whisper + ffmpeg + AI metadata) so it's not unit-tested
directly. The three layers above plus the existing test suite for
`_process_clips_parallel`-adjacent code give us coverage of every line
manual-mode actually adds.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.services.clip_extractor import (
    _build_manual_clip_windows,
    _parse_timestamp,
)
from backend.api.downloaded import (
    _MANUAL_MAX_RANGES,
    _MANUAL_MIN_CLIP_SEC,
    _validate_manual_time_ranges,
)


# ── _parse_timestamp ────────────────────────────────────────────────────────


class TestParseTimestampAccepts:
    """Every accepted input form. Locks in the cross-product so a future
    refactor can't quietly drop one of the four accepted shapes."""

    @pytest.mark.parametrize("inp,expected", [
        # numeric pass-through
        (0, 0.0),
        (45, 45.0),
        (45.5, 45.5),
        # plain SS form
        ("0", 0.0),
        ("18", 18.0),
        ("70", 70.0),  # > 60 is fine in single-part (70 seconds)
        ("18.5", 18.5),
        # MM:SS form — minutes unbounded (90-min videos read as "90:00")
        ("0:18", 18.0),
        ("10:38", 638.0),
        ("90:00", 5400.0),  # 90 minutes — long-form video, allowed
        # HH:MM:SS form
        ("1:02:15", 3735.0),
        ("0:00:05", 5.0),
        # fractional seconds in multi-part forms
        ("1:30.5", 90.5),
        ("0:00:05.25", 5.25),
    ])
    def test_accepted_forms(self, inp, expected):
        assert _parse_timestamp(inp) == pytest.approx(expected)


class TestParseTimestampRejects:
    """Every rejection branch + the canonical error string fragment for
    each. The API layer wraps these ValueErrors in HTTPException(400) so
    the user knows which row in the dialog needs fixing."""

    @pytest.mark.parametrize("inp,fragment", [
        ("", "empty"),
        (None, "empty"),
        ("abc", "non-numeric"),
        ("1:2:3:4", "too many"),
        ("-5", "negative"),
        (-5, "negative"),
        ("1:62:00", "minutes"),     # minutes ≥ 60 in HH:MM:SS
        ("10:60", "seconds"),       # seconds ≥ 60 in MM:SS
        ("0:00:90", "seconds"),     # seconds ≥ 60 in HH:MM:SS
    ])
    def test_rejected_forms(self, inp, fragment):
        with pytest.raises(ValueError) as exc:
            _parse_timestamp(inp)
        assert fragment in str(exc.value), (
            f"Error for {inp!r} should mention {fragment!r}, got: {exc.value}"
        )


# ── _build_manual_clip_windows ──────────────────────────────────────────────


class TestBuildManualClipWindows:
    def test_basic_two_ranges(self):
        windows = _build_manual_clip_windows(
            [{"start": 18, "end": 38}, {"start": 638, "end": 658}],
            duration=3600,
            title="Tesla Q4 earnings",
        )
        assert len(windows) == 2
        assert windows[0] == {
            "start": 18.0, "end": 38.0,
            "title": "Tesla Q4 earnings — clip 1",
            "hook": "", "reason": "",
        }
        assert windows[1]["title"] == "Tesla Q4 earnings — clip 2"

    def test_empty_reason_and_hook_locked_in(self):
        """Downstream task_runner._clip_title relies on `reason` being
        empty so it falls through to the `{source} — clip N` branch.
        If we ever populate reason here, every clip would inherit the
        source's title verbatim — useless when browsing the Library."""
        windows = _build_manual_clip_windows(
            [{"start": 0, "end": 10}], 60, "T"
        )
        assert windows[0]["reason"] == ""
        assert windows[0]["hook"] == ""

    def test_no_virality_or_hook_score_fields(self):
        """Manual mode has no AI judgment to inject. The persisted
        clip_virality_score / clip_hook_score columns stay NULL, which
        the UI already handles for legacy rows. Locking in: we
        explicitly DO NOT set these fields here."""
        windows = _build_manual_clip_windows(
            [{"start": 0, "end": 10}], 60, "T"
        )
        assert "virality_score" not in windows[0]
        assert "hook_score" not in windows[0]
        assert "hook_type" not in windows[0]

    def test_clamps_end_to_duration_defensively(self):
        """API validates against video_duration but a drifted DB row
        (yt-dlp reported 60s, actual file is 58s) could let an end-time
        through that exceeds the real source. _build_manual_clip_windows
        defensively clamps so ffmpeg's seek doesn't run past EOF."""
        windows = _build_manual_clip_windows(
            [{"start": 10, "end": 70}], duration=60, title="T",
        )
        assert windows[0]["end"] == 60.0

    def test_rounds_to_one_decimal(self):
        windows = _build_manual_clip_windows(
            [{"start": 1.23456, "end": 5.99}], 60, "T"
        )
        assert windows[0]["start"] == 1.2
        assert windows[0]["end"] == 6.0

    def test_zero_duration_does_not_clamp(self):
        """Edge case: video.duration_seconds = 0 (not yet probed). We
        shouldn't clamp end to 0 — that would collapse every window."""
        windows = _build_manual_clip_windows(
            [{"start": 10, "end": 20}], duration=0, title="T",
        )
        assert windows[0]["end"] == 20.0

    def test_empty_input_returns_empty(self):
        assert _build_manual_clip_windows([], 60, "T") == []

    def test_default_title_when_source_unset(self):
        windows = _build_manual_clip_windows(
            [{"start": 0, "end": 5}], 60, "Untitled",
        )
        assert windows[0]["title"] == "Untitled — clip 1"


# ── _validate_manual_time_ranges (API layer) ───────────────────────────────


class TestValidateManualTimeRangesHappyPath:
    def test_basic_parses_and_passes(self):
        parsed = _validate_manual_time_ranges(
            [{"start": "18", "end": "38"}, {"start": "10:38", "end": "10:58"}],
            video_duration=3600,
        )
        assert parsed == [
            {"start": 18.0, "end": 38.0},
            {"start": 638.0, "end": 658.0},
        ]

    def test_sorts_by_start_for_stable_clip_numbering(self):
        """Out-of-order user input gets sorted so persisted clip 1, 2, 3
        match the timeline order. This stops the surprise of
        `clip 1 = 10:38` when the user typed `18-38` first.
        """
        parsed = _validate_manual_time_ranges(
            [{"start": "10:38", "end": "10:58"}, {"start": 18, "end": 38}],
            video_duration=3600,
        )
        assert [r["start"] for r in parsed] == [18.0, 638.0]

    def test_numeric_start_end_works(self):
        """Frontend sends already-parsed floats in the row-mode payload;
        backend must accept those without forcing the user to stringify."""
        parsed = _validate_manual_time_ranges(
            [{"start": 18, "end": 38.5}],
            video_duration=60,
        )
        assert parsed == [{"start": 18.0, "end": 38.5}]

    def test_end_within_half_second_epsilon_accepted(self):
        """yt-dlp's cached duration can be 0.3-0.5s off from the actual
        frame count. We accept end-times within +0.5s of duration so a
        '38-58' on a 57.8s-duration row doesn't bounce."""
        parsed = _validate_manual_time_ranges(
            [{"start": 0, "end": 58.3}],
            video_duration=58.0,
        )
        assert parsed[0]["end"] == 58.3


class TestValidateManualTimeRangesRejects:
    def _expect_400(self, payload, video_duration, expected_fragment):
        with pytest.raises(HTTPException) as exc:
            _validate_manual_time_ranges(payload, video_duration)
        assert exc.value.status_code == 400
        assert expected_fragment in exc.value.detail, (
            f"Expected {expected_fragment!r} in {exc.value.detail!r}"
        )
        return exc.value

    def test_none_payload_rejected(self):
        self._expect_400(None, 60, "required")

    def test_empty_list_rejected(self):
        self._expect_400([], 60, "required")

    def test_non_list_rejected(self):
        self._expect_400("18-38", 60, "required")

    def test_too_many_ranges_rejected(self):
        too_many = [{"start": i, "end": i + 2} for i in range(_MANUAL_MAX_RANGES + 1)]
        err = self._expect_400(too_many, 3600, "Too many")
        # Make sure the error names the cap, not just "too many".
        assert str(_MANUAL_MAX_RANGES) in err.detail

    def test_non_dict_entry_rejected(self):
        self._expect_400(
            [{"start": 0, "end": 10}, "not a dict"],
            60,
            "Range 2: expected an object",
        )

    def test_parse_failure_per_range_labeled(self):
        """Validation error names the row index (1-based) so the user
        knows which textarea line / row component to fix. Pre-fix the
        error was a flat 'parse failed', no row pointer."""
        err = self._expect_400(
            [{"start": 18, "end": 38}, {"start": "abc", "end": 10}],
            60,
            "Range 2:",
        )
        # The wrapped ValueError message also bubbles up.
        assert "non-numeric" in err.detail or "invalid" in err.detail.lower()

    def test_inverted_range_rejected(self):
        self._expect_400(
            [{"start": 20, "end": 10}],
            60,
            "must be after",
        )

    def test_zero_length_range_rejected(self):
        self._expect_400(
            [{"start": 30, "end": 30}],
            60,
            "must be after",
        )

    def test_sub_one_second_range_rejected(self):
        self._expect_400(
            [{"start": 0, "end": 0.5}],
            60,
            "too short",
        )

    def test_beyond_duration_rejected(self):
        err = self._expect_400(
            [{"start": 0, "end": 7200}],
            3600,
            "exceeds video duration",
        )
        # Surface the actual duration so the user knows the limit.
        assert "3600" in err.detail

    def test_missing_start_or_end_keys_rejected(self):
        """Dict shape requires both `start` and `end`. A row with only
        one field would otherwise pass parse but end up as 0 → confusion."""
        # _parse_timestamp(None) raises with "empty" — that's the error
        # the user sees, not a 500.
        self._expect_400(
            [{"start": 10}],
            60,
            "empty",
        )


class TestManualConstants:
    """Sanity check the constants exposed to tests + docs so a tweak
    doesn't silently shift behaviour for callers that pinned them."""

    def test_max_ranges_is_10(self):
        # Lowered 20 → 10 on 2026-05-27 (UX feedback: 20 was too many
        # for a hand-curated workflow). Mirrored on the frontend as
        # MANUAL_ROWS_MAX in ClipStudio.jsx — keep both in sync.
        assert _MANUAL_MAX_RANGES == 10

    def test_min_clip_is_one_second(self):
        assert _MANUAL_MIN_CLIP_SEC == 1.0
