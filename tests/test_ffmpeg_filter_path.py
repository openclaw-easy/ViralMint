# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""`ff_filter_path` — the one escaper for paths inside an FFmpeg filtergraph.

Customer report 2026-09-12 (Windows 11, v2026.9.10.1): every extract-clips
job finished with `caption_status='failed'` on every clip. Root cause: the
`ass=` filter value was built as `C\\:/Users/.../s.ass`, and the filtergraph
parser consumes that single backslash BEFORE the filter's option parser
splits on `:`, so ffmpeg saw filename `C` and tried to parse the rest as
`original_size`. The raw path fails the same way. Only the quoted form
survives both parses, for `ass`, `subtitles` AND `drawtext`.

The unit half pins the string; the integration half, when an ffmpeg binary
is installed, replays the real parse against a directory literally named
`C:` (a legal POSIX name), which makes `C:/x.ass` a valid path HERE and so
isolates the filtergraph escaping question from Windows itself.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backend.services.video_utils import ff_filter_path


class TestString:
    def test_windows_drive_path_is_quoted_and_colon_escaped(self):
        assert ff_filter_path(r"C:\Users\me\with space\s.ass") == r"'C\:/Users/me/with space/s.ass'"

    def test_posix_path_unchanged_apart_from_quotes(self):
        assert ff_filter_path("/tmp/a b/s.ass") == "'/tmp/a b/s.ass'"

    def test_accepts_path_objects(self):
        assert ff_filter_path(Path("/x/s.ass")) == "'/x/s.ass'"

    def test_option_separators_are_escaped(self):
        # `=` is the option parser's key/value separator; `:` its pair separator.
        assert ff_filter_path("C:/a=b/s.ass") == r"'C\:/a\=b/s.ass'"

    def test_apostrophe_closes_escapes_and_reopens(self):
        # graph level strips the quotes and turns `\\\'` into `\'`;
        # option level turns `\'` into `'`.
        assert ff_filter_path("C:/it's/s.ass") == "'C\\:/it'\\\\\\''s/s.ass'"

    def test_old_single_backslash_form_is_not_what_we_emit(self):
        # The regression: this exact string was the shipped escape.
        assert ff_filter_path("C:/x/s.ass") != "C\\:/x/s.ass"

    def test_every_filter_site_uses_the_helper(self):
        """Rule #32: one escaper. A raw or hand-escaped path in any of these
        filter values reintroduces the Windows failure."""
        import re
        root = Path(__file__).resolve().parents[1] / "backend"
        offenders = []
        for py in root.rglob("*.py"):
            src = py.read_text(encoding="utf-8")
            for m in re.finditer(r'f"[^"\n]*\b(ass|subtitles)=\{([^}]*)\}', src):
                if "ff_filter_path" not in m.group(2) and "ff_filter_path" not in m.group(2):
                    offenders.append(f"{py.relative_to(root.parent)}: {m.group(0)}")
            if '.replace(":", "\\\\:")' in src and py.name != "video_utils.py":
                offenders.append(f"{py.relative_to(root.parent)}: hand-rolled colon escape")
        assert not offenders, offenders


ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 64
PlayResY: 64

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,10,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,hi
"""

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _ffmpeg_has_filter(name: str) -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return f" {name} " in out


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", "in.mp4", *args, "-t", "0.2", "out.mp4"],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )


@needs_ffmpeg
class TestRealFfmpeg:
    @pytest.fixture()
    def work(self, tmp_path: Path) -> Path:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
             "color=c=black:s=64x64:d=1", "in.mp4"],
            cwd=tmp_path, check=True, capture_output=True, timeout=60,
        )
        return tmp_path

    # A directory literally named "C:" so the escaped value resolves here.
    @pytest.mark.parametrize("folder", ["C:/with space", "C:/it's odd", "C:/a=b", "C:/[x],y;z", "D:/plain"])
    def test_ass_filter_accepts_the_quoted_form(self, work: Path, folder: str):
        if not _ffmpeg_has_filter("ass"):
            pytest.skip("ffmpeg built without libass")
        ass = work / folder / "s.ass"
        ass.parent.mkdir(parents=True)
        ass.write_text(ASS)
        r = _run(work, "-vf", f"ass={ff_filter_path(folder + '/s.ass')}")
        assert r.returncode == 0, r.stderr

    def test_the_shipped_single_backslash_form_reproduces_the_customer_error(self, work: Path):
        if not _ffmpeg_has_filter("ass"):
            pytest.skip("ffmpeg built without libass")
        ass = work / "C:" / "with space" / "s.ass"
        ass.parent.mkdir(parents=True)
        ass.write_text(ASS)
        old = "C:/with space/s.ass".replace(":", "\\:")
        r = _run(work, "-vf", f"ass={old}")
        assert r.returncode != 0
        assert "original_size" in r.stderr, r.stderr

    def test_drawtext_textfile_accepts_the_quoted_form(self, work: Path):
        if not _ffmpeg_has_filter("drawtext"):
            pytest.skip("ffmpeg built without drawtext")
        txt = work / "C:" / "it's=odd" / "t.txt"
        txt.parent.mkdir(parents=True)
        txt.write_text("hello")
        quoted = ff_filter_path("C:/it's=odd/t.txt")
        r = _run(work, "-vf", f"drawtext=textfile={quoted}:fontcolor=white")
        assert r.returncode == 0, r.stderr


class TestNoFfmpegErrorIsThrownAway:
    """ffmpeg prints ~200 characters of version banner BEFORE it says anything
    useful, so a raw `stderr[:N]` logs a constant string and discards the
    diagnosis. `ffmpeg_error()` drops the banner and returns the tail.

    This is a drift guard over the whole backend: the class was fixed one call
    site at a time for a year, which means the next occurrence waits for the next
    audit. The ONE exemption is yt-dlp's own stderr, which carries no banner.
    """

    EXEMPT = {"backend/services/ytdlp_service.py"}

    def test_no_module_truncates_ffmpeg_stderr_by_hand(self):
        import re
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for py in (root / "backend").rglob("*.py"):
            rel = py.relative_to(root).as_posix()
            src = py.read_text(encoding="utf-8")
            for m in re.finditer(r"\b\w+\.stderr\[:\d+\]", src):
                line = src[:m.start()].count("\n") + 1
                if rel in self.EXEMPT:
                    continue
                # The helper's own docstring quotes the broken form.
                if "stderr[:200]` logs" in src[max(0, m.start() - 120):m.start() + 60]:
                    continue
                offenders.append(f"{rel}:{line} {m.group(0)}")
        assert not offenders, (
            "these log ffmpeg's version banner instead of the error — "
            f"use ffmpeg_error(result.stderr, N): {offenders}"
        )
