# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""AutoEmoji in BURNED captions must be a glyph, never a box.

libass cannot draw colour-bitmap emoji fonts (Apple Color Emoji = sbix, Noto
Color Emoji = CBDT), and its glyph fallback lands on them, so every emoji
`insert_emojis_into_words` added burned as tofu ("A video ▢ with 10"). The fix: a bundled MONOCHROME outline emoji font
(caption_fonts/NotoEmoji-Bold.ttf, OFL), named explicitly around every emoji
run (`{\\fnNoto Emoji}…{\\fn<caption font>}`) and handed to libass via
`fontsdir=` (`ass_filter`). Without the font, emoji are left out entirely.
"""
import asyncio
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.services import caption_service as cs

OVERRIDE = "{\\fn" + cs.EMOJI_FONT_NAME + "}"


def _segments(words: list[str]) -> list[dict]:
    return [{
        "start": 0.0, "end": len(words) * 0.5, "text": " ".join(words),
        "words": [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.45}
                  for i, w in enumerate(words)],
    }]


def _events(ass_text: str) -> str:
    return ass_text.split("[Events]", 1)[1]


def _unwrapped_emoji(events: str) -> list[str]:
    """Emoji runs that are NOT inside an emoji-font override."""
    stripped = re.sub(re.escape(OVERRIDE) + r".*?\{\\fn[^}]*\}", "", events)
    return cs._EMOJI_RUN_RE.findall(stripped)


def _build(tmp_path: Path, style: str, words: list[str], **kw) -> str:
    out = tmp_path / f"{style}.ass"
    asyncio.run(cs.generate_captions_ass(
        _segments(words), style=style, aspect_ratio="9:16", output_path=out,
        emoji_style="heavy", **kw,
    ))
    return out.read_text(encoding="utf-8")


ALL_KEYWORDS = list(cs.EMOJI_KEYWORDS)


class TestEveryEmojiNamesTheOutlineFont:
    @pytest.mark.parametrize("style", ["viral", "bold", "karaoke"])
    def test_every_inserted_emoji_is_wrapped(self, tmp_path, style):
        ass = _build(tmp_path, style, ALL_KEYWORDS)
        events = _events(ass)
        assert OVERRIDE in events
        # Every emoji value in the map reached the file…
        for emoji in set(cs.EMOJI_KEYWORDS.values()):
            assert emoji in events, emoji
        # …and none of them outside the emoji-font override.
        assert _unwrapped_emoji(events) == []

    def test_switches_back_to_the_caption_font(self, tmp_path):
        ass = _build(tmp_path, "bold", ["a", "video", "here"])
        font = cs.CAPTION_STYLES["bold"]["font"]
        assert f"{OVERRIDE}🎬{{\\fn{font}}}" in _events(ass)

    def test_highlighting_still_walks_word_by_word(self, tmp_path):
        ass = _build(tmp_path, "viral", ["a", "video", "here"])
        dialogues = [l for l in _events(ass).splitlines() if l.startswith("Dialogue:")]
        assert len(dialogues) == 3
        hl = cs.CAPTION_STYLES["viral"]["highlight_color"]
        # the active word (with its emoji) is the highlighted one
        assert f"{{\\1c{hl}\\b1}}video {OVERRIDE}🎬" in dialogues[1]

    def test_hook_emoji_wrapped(self, tmp_path):
        ass = _build(tmp_path, "viral", ["hi"], hook_text="Stop scrolling 🔥 now")
        hook = next(l for l in ass.splitlines() if ",Hook," in l)
        assert f"{OVERRIDE}🔥" in hook

    def test_keycap_is_one_run(self):
        # 1️⃣ = "1" + VS16 + U+20E3 — must not split across two fonts.
        out = cs.emojify_ass_text("top 1️⃣", "Arial Bold")
        assert out == f"top {OVERRIDE}1️⃣{{\\fnArial Bold}}"

    def test_plain_text_untouched(self):
        assert cs.emojify_ass_text("Price: 10 © 2026 x", "Arial") == "Price: 10 © 2026 x"


class TestNoFontMeansNoEmoji:
    def test_missing_font_burns_no_emoji(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "EMOJI_FONT_FILE", tmp_path / "gone.ttf")
        ass = _build(tmp_path, "viral", ALL_KEYWORDS)
        events = _events(ass)
        assert cs._EMOJI_RUN_RE.findall(events) == []
        assert OVERRIDE not in events
        assert "fontsdir" not in cs.ass_filter(tmp_path / "x.ass")

    def test_missing_font_strips_emoji_from_imported_text(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "EMOJI_FONT_FILE", tmp_path / "gone.ttf")
        assert cs.emojify_ass_text("so good 🔥🔥 yes", "Arial") == "so good yes"


class TestBundledFont:
    def test_font_and_license_ship_together(self):
        assert cs.EMOJI_FONT_FILE.is_file()
        assert (cs.EMOJI_FONT_DIR / "OFL.txt").is_file()
        assert "Open Font License" in (cs.EMOJI_FONT_DIR / "OFL.txt").read_text()
        # installer-size budget
        assert cs.EMOJI_FONT_FILE.stat().st_size < 1_500_000

    def test_ass_filter_passes_fontsdir_through_the_escaper(self, tmp_path):
        from backend.services.video_utils import ff_filter_path
        f = cs.ass_filter(tmp_path / "s.ass")
        assert f.startswith(f"ass={ff_filter_path((tmp_path / 's.ass').resolve())}")
        assert f.endswith(f":fontsdir={ff_filter_path(cs.EMOJI_FONT_DIR)}")

    def test_font_covers_every_mapped_emoji_and_libass_sees_it(self):
        ttLib = pytest.importorskip("fontTools.ttLib")
        font = ttLib.TTFont(str(cs.EMOJI_FONT_FILE))
        # libass 0.17 picks the FIRST (3,1)/(3,10) cmap; a BMP-only (3,1)
        # table first hides every emoji above U+FFFF (they burn as boxes).
        ms = [(t.platformID, t.platEncID) for t in font["cmap"].tables if t.platformID == 3]
        assert ms and ms[0] == (3, 10), ms
        cmap = next(t for t in font["cmap"].tables if (t.platformID, t.platEncID) == (3, 10)).cmap
        missing = sorted({
            f"U+{ord(ch):04X}" for e in cs.EMOJI_KEYWORDS.values() for ch in e
            if ord(ch) not in cmap and ch not in "0123456789#*"
        })
        assert not missing, missing
        names = {r.toUnicode() for r in font["name"].names if r.nameID in (1, 16)}
        assert cs.EMOJI_FONT_NAME in names


def _ffmpeg() -> str | None:
    bundled = Path.home() / ".viralmint" / "bin" / "ffmpeg"
    return str(bundled) if bundled.exists() else shutil.which("ffmpeg")


@pytest.mark.skipif(_ffmpeg() is None, reason="ffmpeg not installed")
def test_real_libass_finds_every_emoji_glyph(tmp_path):
    """Burn every mapped emoji with the real binary; libass must not report a
    single missing glyph (a missing glyph IS the box)."""
    ff = _ffmpeg()
    if " ass " not in subprocess.run([ff, "-hide_banner", "-filters"],
                                     capture_output=True, text=True).stdout:
        pytest.skip("ffmpeg built without libass")
    ass = _build(tmp_path, "viral", ALL_KEYWORDS)
    (tmp_path / "c.ass").write_text(ass, encoding="utf-8")
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=1080x1920:d=40", "-pix_fmt", "yuv420p", "in.mp4"],
                   cwd=tmp_path, check=True, capture_output=True, timeout=120)
    r = subprocess.run([ff, "-hide_banner", "-v", "verbose", "-y", "-i", "in.mp4",
                        "-vf", cs.ass_filter(tmp_path / "c.ass"), "-f", "null", "-"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-800:]
    assert f"({cs.EMOJI_FONT_NAME}," in r.stderr, "emoji font never selected"
    not_found = [l for l in r.stderr.splitlines() if "not found" in l and "Glyph" in l]
    assert not not_found, not_found[:5]


@pytest.mark.skipif(_ffmpeg() is None, reason="ffmpeg not installed")
def test_fontsdir_survives_a_drive_colon_path(tmp_path):
    """`fontsdir=` goes through the same escaper as `ass=`: a folder literally
    named "C:" (plus an apostrophe and `=`) must still load the font."""
    from backend.services.video_utils import ff_filter_path
    ff = _ffmpeg()
    folder = "C:/it's odd=x"
    (tmp_path / folder / "fonts").mkdir(parents=True)
    shutil.copy(cs.EMOJI_FONT_FILE, tmp_path / folder / "fonts")
    (tmp_path / folder / "s.ass").write_text(
        _build(tmp_path, "viral", ["a", "video"]), encoding="utf-8")
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=1080x1920:d=1", "in.mp4"],
                   cwd=tmp_path, check=True, capture_output=True, timeout=60)
    vf = f"ass={ff_filter_path(folder + '/s.ass')}:fontsdir={ff_filter_path(folder + '/fonts')}"
    r = subprocess.run([ff, "-hide_banner", "-v", "verbose", "-y", "-i", "in.mp4",
                        "-vf", vf, "-f", "null", "-"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    assert f"({cs.EMOJI_FONT_NAME}, 700, 0) -> NotoEmoji-Bold" in r.stderr
