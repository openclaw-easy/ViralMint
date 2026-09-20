# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Auto-zoom had never parsed, and said it had.

Two independent defects in one filtergraph:

1. The zoom expression contains `between(t,a,b)`, whose commas are filtergraph
   separators unless the value is quoted — ffmpeg refused the graph with
   "No option name near '.../(1+0.15*sin(PI*(t-...)*between(t'" on every input.
2. It animated `crop`'s w/h, which ffmpeg evaluates ONCE at filter init, where
   `t` is NaN. A time-varying crop SIZE cannot work at all.

Because `apply_auto_zoom` returned its INPUT on any ffmpeg failure, the runner
copied the source to the output path and reported success — so every auto-zoom
in the app's history produced a byte-identical copy and called it a zoom.

The integration half renders with the real binary and measures that the output
actually differs from its source, which is the only assertion that would have
caught this.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.exceptions import VideoGenerationError
from backend.services.ffmpeg_service import auto_zoom_vf

EXPR = "1+0.15*sin(PI*(t-0.000)/1.000)*between(t,0.000,1.000)"


class TestTheFiltergraphString:
    def test_the_expression_is_quoted(self):
        """Unquoted, the commas inside between() split the graph."""
        vf = auto_zoom_vf(1080, 1920, EXPR)
        assert "'" in vf
        assert vf.count("'") % 2 == 0
        for part in vf.split("eval=frame")[0].split(":"):
            if "between(t" in part:
                assert "'" in part, f"unquoted expression in {part!r}"

    def test_crop_size_is_fixed_and_scale_is_animated(self):
        """crop's w/h are evaluated once at init (t is NaN there); scale with
        eval=frame is re-evaluated per frame."""
        vf = auto_zoom_vf(1080, 1920, EXPR)
        assert "eval=frame" in vf
        assert vf.endswith("crop=1080:1920")
        assert "crop=w=" not in vf

    def test_dimensions_stay_even(self):
        """yuv420p needs both sides even at EVERY zoom level, not just at 1.0."""
        vf = auto_zoom_vf(1080, 1920, EXPR)
        assert "trunc(1080*" in vf and "/2)*2" in vf
        assert "trunc(1920*" in vf


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@needs_ffmpeg
class TestRealFfmpeg:
    @pytest.fixture()
    def src(self, tmp_path: Path) -> Path:
        out = tmp_path / "in.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "testsrc=s=128x128:d=2", str(out)],
            check=True, capture_output=True, timeout=60,
        )
        return out

    def _render(self, src: Path, vf: str, out: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-vf", vf, str(out)],
            capture_output=True, text=True, timeout=120,
        )

    def test_the_shipped_form_reproduces_the_parse_failure(self, src: Path, tmp_path: Path):
        z = f"(1+0.15*sin(PI*(t-0.000)/2.000)*between(t,0.000,2.000))"
        old = (f"crop=w=128/{z}:h=128/{z}:x=(128-128/{z})/2:y=(128-128/{z})/2,"
               f"scale=128:128:flags=lanczos")
        r = self._render(src, old, tmp_path / "old.mp4")
        assert r.returncode != 0 or not (tmp_path / "old.mp4").exists()
        assert "No option name near" in r.stderr, r.stderr

    def test_the_new_form_renders_and_actually_zooms(self, src: Path, tmp_path: Path):
        expr = "1+0.15*sin(PI*(t-0.000)/2.000)*between(t,0.000,2.000)"
        out = tmp_path / "new.mp4"
        r = self._render(src, auto_zoom_vf(128, 128, expr), out)
        assert r.returncode == 0, r.stderr
        assert out.exists() and out.stat().st_size > 0

        # A byte-identical copy was the OLD outcome. Prove the pixels moved:
        # PSNR against the source must be far from "identical" (>50 dB).
        psnr = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(src), "-i", str(out),
             "-filter_complex", "[0:v][1:v]psnr", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        ).stderr
        line = [l for l in psnr.splitlines() if "average:" in l]
        assert line, psnr
        average = float(line[-1].split("average:")[1].split()[0])
        assert average < 40.0, f"output is ~identical to the source (PSNR {average} dB)"


class TestFailureIsNotSuccess:
    async def test_a_failed_ffmpeg_raises_instead_of_returning_the_input(self, tmp_path: Path):
        from backend.services import ffmpeg_service

        src = tmp_path / "in.mp4"
        src.write_bytes(b"\x00" * 1024)
        words = [{"text": "hi", "start": 0.0, "end": 1.0},
                 {"text": "there", "start": 1.0, "end": 2.0}]

        def _fake_run(cmd, **kw):
            if cmd[0] == "ffprobe":
                return MagicMock(returncode=0, stdout="128,128,30/1\n", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="ffmpeg version 8.1\nboom: bad filter")

        with patch.object(ffmpeg_service.subprocess, "run", side_effect=_fake_run):
            with pytest.raises(VideoGenerationError):
                await ffmpeg_service.apply_auto_zoom(src, words, output_path=tmp_path / "o.mp4")

    async def test_no_words_still_returns_the_source_quietly(self, tmp_path: Path):
        """That path is not a failure — there is nothing to pulse on."""
        from backend.services import ffmpeg_service
        src = tmp_path / "in.mp4"
        src.write_bytes(b"\x00" * 1024)
        got = await ffmpeg_service.apply_auto_zoom(src, [], output_path=tmp_path / "o.mp4")
        assert Path(got) == src
