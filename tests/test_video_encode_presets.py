# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The render path's x264 presets come from two settings; their defaults keep every encode as it was."""
import pytest

from backend.config import settings
from backend.services import video_utils as vu


def test_defaults_keep_each_call_sites_own_preset_and_crf(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_ENCODE_PRESET", "fast")
    monkeypatch.setattr(settings, "VIDEO_INTERMEDIATE_PRESET", "")
    assert vu.encode_preset() == "fast"
    assert not vu.intermediates_are_fast()
    assert vu.intermediate_x264("20") == ["-preset", "fast", "-crf", "20"]
    assert vu.intermediate_x264("18", "veryfast") == ["-preset", "veryfast", "-crf", "18"]


def test_fast_intermediates_are_near_lossless(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_INTERMEDIATE_PRESET", " UltraFast ")
    assert vu.intermediates_are_fast()
    assert vu.intermediate_x264("20", "veryfast") == ["-preset", "ultrafast", "-crf", vu.INTERMEDIATE_CRF]


@pytest.mark.parametrize("value", ["", "bogus", "placebo; rm -rf /"])
def test_an_unknown_preset_falls_back_instead_of_failing_every_encode(monkeypatch, value):
    monkeypatch.setattr(settings, "VIDEO_ENCODE_PRESET", value)
    monkeypatch.setattr(settings, "VIDEO_INTERMEDIATE_PRESET", value)
    assert vu.encode_preset() == "fast"
    assert not vu.intermediates_are_fast()
