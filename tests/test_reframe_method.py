# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Re-framing must not shrink the content to a third of the frame.

Exporting a 9:16 short to 16:9 used blur_fill: the whole portrait frame is
FITTED inside the landscape one, leaving the real content as a narrow
full-height strip with blur either side. Every ViralMint short is itself a
blur_fill composite (that's how shorts are built from landscape sources), so
the export nested a second box and the picture landed at roughly a third of
the frame in each direction. The Library's Export button hardcoded the same
method, so the UI produced the identical file.

`pick_reframe_method` is the fix: crop when WIDENING, blur_fill when narrowing.

The last test is the app-wide lock: "scale until it fits inside the frame" may
only appear where we decided it should, and every such place is narrowing-only.
"""
import pathlib
from pathlib import Path

import pytest

from backend.services.ffmpeg_service import ASPECT_DIMS, pick_reframe_method


# ── Widening: crop, so the picture fills the frame ─────────────────────────

@pytest.mark.parametrize("src,target", [
    ((1080, 1920), "16:9"),   # the reported case: a 9:16 short → YouTube
    ((1080, 1920), "1:1"),    # 9:16 → square
    ((1080, 1350), "16:9"),   # 4:5 → landscape
    ((1080, 1080), "16:9"),   # square → landscape
])
def test_widening_crops_to_fill(src, target):
    assert pick_reframe_method(src[0], src[1], target) == "crop"


# ── Narrowing: blur_fill, the short-form look the product is built on ──────

@pytest.mark.parametrize("src,target", [
    ((1920, 1080), "9:16"),   # the core product path — must NOT change
    ((1920, 1080), "1:1"),
    ((1920, 1080), "4:5"),
    ((1080, 1080), "9:16"),
])
def test_narrowing_keeps_the_blur_fill_look(src, target):
    assert pick_reframe_method(src[0], src[1], target) == "blur_fill"


def test_same_aspect_is_not_treated_as_widening():
    """1.777… vs 1.778 must not flip to crop and shave the edges off."""
    assert pick_reframe_method(1920, 1080, "16:9") == "blur_fill"
    assert pick_reframe_method(1280, 720, "16:9") == "blur_fill"
    assert pick_reframe_method(1080, 1920, "9:16") == "blur_fill"


def test_unknown_dimensions_fall_back_to_the_lossless_look():
    """An unreadable probe must never crop — that would silently discard
    picture on a video we know nothing about."""
    assert pick_reframe_method(0, 0, "16:9") == "blur_fill"
    assert pick_reframe_method(1080, 0, "16:9") == "blur_fill"


def test_unknown_target_does_not_raise():
    assert pick_reframe_method(1080, 1920, "banana") in ("crop", "blur_fill")


def test_every_supported_aspect_has_dimensions():
    for aspect in ("9:16", "16:9", "1:1", "4:5"):
        w, h = ASPECT_DIMS[aspect]
        assert w > 0 and h > 0


@pytest.mark.asyncio
async def test_auto_resolves_the_method_before_ffmpeg_sees_it(tmp_path, monkeypatch):
    """`method="auto"` must be resolved by the probe — never reach the filter
    builder as a literal, which would silently take the letterbox `else`."""
    from backend.services import ffmpeg_service

    async def fake_probe(path):
        return 1080, 1920                      # a 9:16 source, widening to 16:9

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        class _R:
            returncode = 0
            stderr = ""
        # Write where ffmpeg was TOLD to write: the conversion encodes to a
        # temp file and renames, so a stub that writes the final name directly
        # leaves the temp empty and the size check rejects the pass. ≥1KB
        # because an exit-0 pass that produced nothing is treated as a failure.
        pathlib.Path(cmd[-1]).write_bytes(b"x" * 2048)
        return _R()

    monkeypatch.setattr(ffmpeg_service, "_probe_dimensions", fake_probe)
    monkeypatch.setattr(ffmpeg_service.subprocess, "run", fake_run)

    out = await ffmpeg_service.convert_aspect_ratio(
        tmp_path / "src.mp4", target_aspect="16:9", method="auto",
        output_path=tmp_path / "out.mp4")

    assert out == tmp_path / "out.mp4"
    vf = " ".join(seen["cmd"])
    # crop-to-fill was chosen: cover-scale + crop, and NOT the fit chain.
    assert "force_original_aspect_ratio=increase" in vf
    assert "force_original_aspect_ratio=decrease" not in vf
    assert "boxblur" not in vf


@pytest.mark.asyncio
async def test_auto_still_blur_fills_the_core_short_form_path(tmp_path, monkeypatch):
    """16:9 → 9:16 is how every short is built. It must keep the blur look."""
    from backend.services import ffmpeg_service

    async def fake_probe(path):
        return 1920, 1080

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        class _R:
            returncode = 0
            stderr = ""
        # Same reason as above: write where ffmpeg was told to, and enough
        # bytes that the pass is not treated as an empty encode.
        pathlib.Path(cmd[-1]).write_bytes(b"x" * 2048)
        return _R()

    monkeypatch.setattr(ffmpeg_service, "_probe_dimensions", fake_probe)
    monkeypatch.setattr(ffmpeg_service.subprocess, "run", fake_run)

    await ffmpeg_service.convert_aspect_ratio(
        tmp_path / "src.mp4", target_aspect="9:16", method="auto",
        output_path=tmp_path / "out916.mp4")

    assert "boxblur" in " ".join(seen["cmd"])


# ── App-wide lock ──────────────────────────────────────────────────────────

BACKEND = Path(__file__).resolve().parents[1] / "backend"

# The signature of "shrink the source until it fits inside the frame". Paired
# with a pad or an overlay it produces letterbox / blur-fill.
FIT_SIGNATURE = "force_original_aspect_ratio=decrease"

# Files allowed to contain it, and why. Adding an entry here is a deliberate
# decision that this path only ever NARROWS (16:9 → 9:16), where fitting is
# the correct short-form look — never widens, where it shrinks the picture.
ALLOWED = {
    "services/ffmpeg_service.py": (
        "convert_aspect_ratio's explicit letterbox/blur_fill branches (reached "
        "only when a caller names the method, or when pick_reframe_method "
        "chooses blur_fill for a NARROWING re-frame), plus extract_clip's "
        "landscape→9:16 branch."
    ),
}


def test_fit_into_frame_only_exists_where_we_decided_it_should():
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        if FIT_SIGNATURE not in path.read_text():
            continue
        rel = str(path.relative_to(BACKEND))
        if rel not in ALLOWED:
            offenders.append(rel)
    assert not offenders, (
        f"{sorted(offenders)} scales media to FIT inside the target frame. "
        "When the target is WIDER than the source that shrinks the picture to "
        "a fraction of the frame. Use a cover-scale + center-crop chain, or "
        "ffmpeg_service.pick_reframe_method if the direction isn't known up "
        "front. If this path genuinely only ever narrows, add it to ALLOWED "
        "with the reason."
    )
