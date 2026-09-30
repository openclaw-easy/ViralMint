# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Auto-cut's clip budget is computed twice — it must be the same number.

The dialog tells the user how many clips the button will make; the endpoint
decides how many it actually makes. Those are two implementations of one rule
in two languages. If they drift, the dialog under-states and someone who
agreed to "up to 8" gets 43 renders and a Library to weed.
"""
import re
from pathlib import Path

import pytest

from backend.api.downloaded import (
    AUTO_CLIP_MAX,
    AUTO_CLIP_MIN,
    AUTO_CLIP_SECONDS_PER_CLIP,
    AUTO_CLIP_UNKNOWN_DURATION,
    auto_clip_count,
)

JS = Path("frontend/src/components/clip/autoClipCount.js")


def _js_const(name: str) -> int:
    m = re.search(rf"export const {name} = (\d+)", JS.read_text())
    assert m, f"{name} is missing from {JS} — the two copies have diverged"
    return int(m.group(1))


@pytest.mark.parametrize("name,py", [
    ("AUTO_CLIP_SECONDS_PER_CLIP", AUTO_CLIP_SECONDS_PER_CLIP),
    ("AUTO_CLIP_MIN", AUTO_CLIP_MIN),
    ("AUTO_CLIP_MAX", AUTO_CLIP_MAX),
    ("AUTO_CLIP_UNKNOWN_DURATION", AUTO_CLIP_UNKNOWN_DURATION),
])
def test_the_constants_match(name, py):
    assert _js_const(name) == py, (
        f"{name}: backend says {py}, {JS.name} says {_js_const(name)} — "
        "Auto-cut would promise one number and produce another")


def test_the_js_mirrors_the_python_formula():
    """Same shape, not just the same constants: floor(duration / N), clamped."""
    src = JS.read_text()
    assert "Math.floor(durationSeconds / AUTO_CLIP_SECONDS_PER_CLIP)" in src
    assert "Math.max(\n    AUTO_CLIP_MIN," in src or "Math.max(AUTO_CLIP_MIN," in src
    assert "Math.min(AUTO_CLIP_MAX" in src


@pytest.mark.parametrize("duration,expected", [
    (None, 5), (0, 5), (-1, 5),          # unknown duration
    (30, 3), (60, 3), (89, 3),           # floor
    (853, 28),                            # a 14-minute podcast
    (3600, 99), (99_999, 99),            # ceiling
])
def test_known_points(duration, expected):
    assert auto_clip_count(duration) == expected


def test_the_endpoint_uses_the_helper_rather_than_its_own_arithmetic():
    src = Path("backend/api/downloaded.py").read_text()
    assert "auto_clip_count(duration)" in src
    assert "duration // 30" not in src, (
        "the inline formula is back — it is the copy this helper replaced")


def test_the_min_length_floor_matches_the_extractor():
    """A named "Min (s)" caps the quoted count at duration // floor.

    The dialog mirrors clip_extractor's `min_clip_floor = max(10,
    int(min_duration))`. If the backend's floor moves and the dialog's
    doesn't, the quote stops being an upper bound on what the run can cut.
    """
    backend = Path("backend/services/clip_extractor.py").read_text()
    m = re.search(r"min_clip_floor = max\((\d+), int\(min_duration\)\)", backend)
    assert m, "clip_extractor's min_clip_floor rule changed — update autoClipCount.js too"
    js = JS.read_text()
    assert f"Math.max({m.group(1)}, minDurationSeconds)" in js, (
        f"backend floors a named minimum at {m.group(1)}s; autoClipCount.js does not"
    )
    assert "duration_based_max = max(1, duration // min_clip_floor)" in backend
