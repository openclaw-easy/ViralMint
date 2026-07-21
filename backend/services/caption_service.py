# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Word-by-word animated caption renderer.
Generates ASS (Advanced SubStation Alpha) subtitle files with per-word highlighting.
This is THE key visual feature for viral short-form content.
"""
import logging
import re
import subprocess
import sys
import asyncio
from pathlib import Path

from backend.config import settings
from backend.core.exceptions import VideoGenerationError

logger = logging.getLogger(__name__)

# ── Auto Emoji Mapping ───────────────────────────────────────────────────────

EMOJI_KEYWORDS = {
    # Money/finance
    "money": "💰", "cash": "💵", "save": "💰", "invest": "📈",
    "rich": "🤑", "expensive": "💸", "budget": "📊", "profit": "💹",
    "dollar": "💵", "earn": "💰", "income": "💰", "cost": "💸",
    # Emotions
    "amazing": "🤩", "incredible": "😱", "love": "❤️", "hate": "😤",
    "happy": "😊", "sad": "😢", "angry": "😡", "surprised": "😲",
    "crazy": "🤪", "wow": "😮", "funny": "😂", "scary": "😨",
    "beautiful": "😍", "awesome": "🔥", "perfect": "👌",
    # Actions
    "subscribe": "🔔", "like": "👍", "share": "📤", "comment": "💬",
    "click": "👆", "watch": "👀", "listen": "👂", "learn": "📚",
    "stop": "🛑", "wait": "⏳", "think": "🤔", "remember": "💭",
    # Objects
    "phone": "📱", "computer": "💻", "food": "🍕", "house": "🏠",
    "car": "🚗", "book": "📖", "music": "🎵", "video": "🎬",
    "coffee": "☕", "water": "💧", "brain": "🧠", "heart": "❤️",
    # Concepts
    "time": "⏰", "secret": "🤫", "warning": "⚠️", "tip": "💡",
    "fire": "🔥", "growth": "📈", "success": "🏆", "fail": "❌",
    "number": "🔢", "first": "1️⃣", "new": "✨", "free": "🆓",
    "important": "‼️", "question": "❓", "idea": "💡", "goal": "🎯",
    "world": "🌍", "power": "⚡", "king": "👑", "game": "🎮",
    "mistake": "❌", "wrong": "❌", "right": "✅", "yes": "✅",
    "no": "❌", "best": "🏆", "worst": "👎", "top": "🔝",
}


def insert_emojis_into_words(words: list[dict], style: str = "moderate") -> list[dict]:
    """
    Insert emojis after matching keywords in word list.

    Styles:
    - "none": no emojis
    - "minimal": emoji every 4-5 keyword matches
    - "moderate": emoji every 2-3 keyword matches
    - "heavy": emoji on every keyword match

    Mutates word dicts in place (adds emoji to text field).
    """
    if style == "none":
        return words

    interval = {"minimal": 5, "moderate": 3, "heavy": 1}.get(style, 3)
    match_count = 0

    for w in words:
        text_lower = re.sub(r"[^a-z]", "", w["text"].lower())
        emoji = EMOJI_KEYWORDS.get(text_lower)
        if emoji:
            match_count += 1
            if match_count % interval == 0:
                w["text"] = w["text"] + " " + emoji

    return words

# ── Caption Style Presets ──────────────────────────────────────────────────────

# Caption styles. Note on positioning:
# - `alignment` is ASS numpad: 1=bot-L, 2=bot-C, 3=bot-R, 5=mid-C, 8=top-C, etc.
#   ALL "active" styles use alignment=2 (bottom-center) so margin_v actually
#   pushes the text up from the bottom edge — that's how libass interprets it.
#   alignment=5 (mid-center) ignores margin_v and pins to the literal center
#   of the frame, which looked like a placement bug on every video.
# - margin_v_portrait / margin_v_landscape — pixel offset from the BOTTOM edge,
#   different per aspect ratio because absolute pixels don't translate. A
#   1920-tall portrait at 480px-from-bottom puts text at ~75% screen height
#   (the lower-third TikTok zone); the same 480px on a 1080-tall landscape
#   would land in the upper half. _build_ass_header picks the right one.
# - A single `margin_v` field is kept on every style as a fallback (and for
#   custom styles loaded from the DB, which only ship that one field) — it's
#   used when the dual portrait/landscape fields aren't set.
CAPTION_STYLES = {
    "viral": {
        "font": "Arial Bold",
        "font_size_portrait": 56,
        "font_size_landscape": 42,
        "primary_color": "&H00FFFFFF",       # white (ASS uses BGR, &HBBGGRR)
        "highlight_color": "&H0000FFFF",     # yellow
        "outline_color": "&H00000000",       # black
        "outline_width": 3,
        "shadow_depth": 1,
        "alignment": 2,                      # bottom-center (was 5 = literal middle, broken)
        "margin_v": 80,                      # fallback for DB/custom styles
        "margin_v_portrait": 480,            # text bottom ≈ 75% down on 1920px frame (lower-third)
        "margin_v_landscape": 120,           # text bottom ≈ 89% down on 1080px frame
        "words_per_group": 6,
    },
    "classic": {
        "font": "Arial",
        "font_size_portrait": 42,
        "font_size_landscape": 32,
        "primary_color": "&H00FFFFFF",
        "highlight_color": "&H00FFFFFF",     # no highlight
        "outline_color": "&H00000000",
        "outline_width": 2,
        "shadow_depth": 0,
        "alignment": 2,                      # bottom-center
        "margin_v": 40,
        "margin_v_portrait": 100,            # near-bottom subtitle position
        "margin_v_landscape": 50,
        "words_per_group": 8,
    },
    "bold": {
        "font": "Impact",
        "font_size_portrait": 64,
        "font_size_landscape": 48,
        "primary_color": "&H00FFFFFF",
        "highlight_color": "&H0000FF00",     # green
        "outline_color": "&H00000000",
        "outline_width": 4,
        "shadow_depth": 2,
        "alignment": 2,                      # was 5 = literal middle, broken
        "margin_v": 60,
        "margin_v_portrait": 460,            # lower-third for big animated callouts
        "margin_v_landscape": 110,
        "words_per_group": 4,
    },
    "brainrot": {
        # Big, centered, 1–2 words at a time — the recognizable gameplay-overlay
        # TikTok look. Bottom-anchored (alignment 2) with a large margin_v so the
        # text sits near the vertical CENTER on a 1920px frame (alignment 5 is
        # broken in our renderer, same as the other styles note).
        "font": "Impact",
        "font_size_portrait": 84,
        "font_size_landscape": 60,
        "primary_color": "&H00FFFFFF",       # white
        "highlight_color": "&H0000FFFF",     # yellow (ASS BGR)
        "outline_color": "&H00000000",       # black
        "outline_width": 6,
        "shadow_depth": 3,
        "alignment": 2,
        "margin_v": 300,
        "margin_v_portrait": 840,            # ≈ vertical center on a 1920px frame
        "margin_v_landscape": 300,
        "words_per_group": 2,
    },
    "neon": {
        "font": "Arial Bold",
        "font_size_portrait": 58,
        "font_size_landscape": 44,
        "primary_color": "&H00FFAAFF",       # pink/magenta
        "highlight_color": "&H0000FFFF",     # cyan
        "outline_color": "&H00330033",       # dark purple outline
        "outline_width": 3,
        "shadow_depth": 2,
        "alignment": 2,                      # was 5 = literal middle, broken
        "margin_v": 70,
        "margin_v_portrait": 460,
        "margin_v_landscape": 110,
        "words_per_group": 6,
    },
    "minimal": {
        "font": "Arial",
        "font_size_portrait": 40,
        "font_size_landscape": 30,
        "primary_color": "&H00FFFFFF",       # white
        "highlight_color": "&H00FFFFFF",     # no highlight
        "outline_color": "&H00333333",       # subtle gray outline
        "outline_width": 1,
        "shadow_depth": 0,
        "alignment": 2,                      # bottom-center
        "margin_v": 30,
        "margin_v_portrait": 80,             # very bottom — minimal is supposed to feel subtle
        "margin_v_landscape": 40,
        "words_per_group": 10,               # long phrases
    },
    "karaoke": {
        "font": "Arial Bold",
        "font_size_portrait": 52,
        "font_size_landscape": 40,
        "primary_color": "&H00AAAAAA",       # gray (unspoken)
        "highlight_color": "&H0000FFFF",     # yellow (spoken)
        "outline_color": "&H00000000",
        "outline_width": 3,
        "shadow_depth": 1,
        "alignment": 2,                      # bottom-center
        "margin_v": 50,
        "margin_v_portrait": 140,
        "margin_v_landscape": 70,
        "words_per_group": 7,
    },
    "glow": {
        "font": "Arial Bold",
        "font_size_portrait": 60,
        "font_size_landscape": 46,
        "primary_color": "&H00FFFFFF",       # white
        "highlight_color": "&H0066CCFF",     # orange-gold
        "outline_color": "&H000066CC",       # dark orange outline
        "outline_width": 4,
        "shadow_depth": 3,
        "alignment": 2,                      # was 5 = literal middle, broken
        "margin_v": 75,
        "margin_v_portrait": 470,
        "margin_v_landscape": 115,
        "words_per_group": 6,
    },
    # ── Themed look pack ─────────────────────────────────────────────────────
    # Named looks. Fonts stick to cross-platform system families (Arial Black /
    # Georgia / Verdana ship on macOS + Windows; Linux resolves close metrics
    # via fontconfig) — resolve_caption_font still swaps in script-aware fonts
    # for CJK etc.
    "urban": {
        # Bold street look: heavy black-outlined white with a hot-orange pop.
        "font": "Arial Black",
        "font_size_portrait": 62,
        "font_size_landscape": 46,
        "primary_color": "&H00FFFFFF",       # white
        "highlight_color": "&H00008CFF",     # hot orange (RGB 255,140,0)
        "outline_color": "&H00000000",       # hard black
        "outline_width": 5,
        "shadow_depth": 0,
        "alignment": 2,
        "margin_v": 115,
        "margin_v_portrait": 470,
        "margin_v_landscape": 115,
        "words_per_group": 3,
    },
    "warm": {
        # Cozy lifestyle look: cream text, amber highlight, soft brown edge.
        "font": "Georgia",
        "font_size_portrait": 54,
        "font_size_landscape": 40,
        "primary_color": "&H00E1F5FF",       # cream (RGB 255,245,225)
        "highlight_color": "&H0078C8FF",     # amber (RGB 255,200,120)
        "outline_color": "&H000A1E3C",       # deep warm brown (RGB 60,30,10)
        "outline_width": 3,
        "shadow_depth": 2,
        "alignment": 2,
        "margin_v": 110,
        "margin_v_portrait": 460,
        "margin_v_landscape": 110,
        "words_per_group": 5,
    },
    "mono": {
        # Monochrome editorial look: zero saturation, thin edge, calm pacing.
        "font": "Verdana",
        "font_size_portrait": 50,
        "font_size_landscape": 38,
        "primary_color": "&H00FFFFFF",       # white
        "highlight_color": "&H00C8C8C8",     # light gray (RGB 200,200,200)
        "outline_color": "&H00141414",       # near-black
        "outline_width": 2,
        "shadow_depth": 1,
        "alignment": 2,
        "margin_v": 105,
        "margin_v_portrait": 450,
        "margin_v_landscape": 105,
        "words_per_group": 5,
    },
}


# ── Script-aware font fallback ────────────────────────────────────────────────
#
# Every built-in style pins a Latin display font (Arial Bold / Impact). libass
# does NOT glyph-fallback per character the way a browser does, so any script
# those fonts lack — Chinese/Japanese/Korean, Arabic, Thai, … — renders as
# tofu boxes (□□□□) burned permanently into the video. When the caption text
# contains such a script, swap the style's font for a platform system font that
# covers it. These fonts also cover Latin, so mixed-language text stays
# readable; pure-Latin (and Cyrillic/Greek, which Arial covers) keeps the
# style's designed font.

# Ordered — first match wins. Kana before Han (Japanese text contains Han, so
# kana is the discriminator); Hangul before Han for the same reason.
_SCRIPT_RANGES: list[tuple[str, tuple[tuple[int, int], ...]]] = [
    ("kana", ((0x3040, 0x30FF), (0x31F0, 0x31FF))),
    ("hangul", ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F))),
    ("han", ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))),
    ("arabic", ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF))),
    ("hebrew", ((0x0590, 0x05FF),)),
    ("devanagari", ((0x0900, 0x097F),)),
    ("thai", ((0x0E00, 0x0E7F),)),
]

# System fonts guaranteed (macOS/Windows) or conventional (Linux: Noto via
# fonts-noto-cjk etc.) per platform. libass resolves the name via CoreText /
# DirectWrite / fontconfig at burn time.
_SCRIPT_FONTS: dict[str, dict[str, str]] = {
    "darwin": {
        "han": "PingFang SC", "kana": "Hiragino Sans", "hangul": "Apple SD Gothic Neo",
        "arabic": "Geeza Pro", "hebrew": "Arial Hebrew",
        "devanagari": "Kohinoor Devanagari", "thai": "Thonburi",
    },
    "win32": {
        "han": "Microsoft YaHei", "kana": "Yu Gothic UI", "hangul": "Malgun Gothic",
        "arabic": "Segoe UI", "hebrew": "Segoe UI",
        "devanagari": "Nirmala UI", "thai": "Leelawadee UI",
    },
    "linux": {
        "han": "Noto Sans CJK SC", "kana": "Noto Sans CJK JP", "hangul": "Noto Sans CJK KR",
        "arabic": "Noto Sans Arabic", "hebrew": "Noto Sans Hebrew",
        "devanagari": "Noto Sans Devanagari", "thai": "Noto Sans Thai",
    },
}


def detect_non_latin_script(text: str) -> str | None:
    """Return the first non-Latin script (per _SCRIPT_RANGES order) present in
    `text`, or None when the default Latin fonts can render everything."""
    if not text:
        return None
    present: set[str] = set()
    for ch in text:
        cp = ord(ch)
        if cp < 0x0370:  # Latin / Latin-1 / extended — Arial territory
            continue
        for name, ranges in _SCRIPT_RANGES:
            if any(lo <= cp <= hi for lo, hi in ranges):
                present.add(name)
                break
    for name, _ in _SCRIPT_RANGES:  # honor priority order (kana > han, …)
        if name in present:
            return name
    return None


def resolve_caption_font(preferred_font: str, text: str) -> str:
    """The style's designed font, unless `text` needs a script it can't render —
    then a platform system font with coverage for that script."""
    script = detect_non_latin_script(text)
    if not script:
        return preferred_font
    platform = sys.platform if sys.platform in _SCRIPT_FONTS else "linux"
    font = _SCRIPT_FONTS[platform].get(script, preferred_font)
    if font != preferred_font:
        logger.info(
            "Caption text contains %s script — overriding font %r → %r",
            script, preferred_font, font,
        )
    return font


async def _load_custom_style(style_id: str) -> dict | None:
    """Load a custom caption style from the database by ID."""
    try:
        from backend.database import AsyncSessionLocal
        from backend.models.caption_style import CaptionStyle
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(CaptionStyle).where(CaptionStyle.id == style_id))
            s = result.scalar_one_or_none()
            if s:
                return {
                    "font": s.font,
                    "font_size_portrait": s.font_size_portrait,
                    "font_size_landscape": s.font_size_landscape,
                    "primary_color": s.primary_color,
                    "highlight_color": s.highlight_color,
                    "outline_color": s.outline_color,
                    "outline_width": s.outline_width,
                    "shadow_depth": s.shadow_depth,
                    "alignment": s.alignment,
                    "margin_v": s.margin_v,
                    "words_per_group": s.words_per_group,
                }
    except Exception as e:
        logger.warning(f"Failed to load custom caption style {style_id}: {e}")
    return None


def _format_ass_time(seconds: float) -> str:
    """Convert seconds to ASS time format: H:MM:SS.cc"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _build_ass_header(style_config: dict, aspect_ratio: str, resolution: tuple[int, int]) -> str:
    """Build ASS file header with style definitions."""
    style = style_config
    width, height = resolution
    is_portrait = aspect_ratio == "9:16"

    font_size = style["font_size_portrait"] if is_portrait else style["font_size_landscape"]

    # Pick portrait or landscape margin_v. Built-in styles (CAPTION_STYLES)
    # ship with both; custom styles loaded from the DB only have a single
    # `margin_v` — fall back to it. Final fallback (80) covers the unlikely
    # case where a custom style is missing all three.
    margin_v_key = "margin_v_portrait" if is_portrait else "margin_v_landscape"
    margin_v = style.get(margin_v_key) or style.get("margin_v") or 80

    return f"""[Script Info]
Title: ViralMint Captions
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style['font']},{font_size},{style['primary_color']},&H000000FF,{style['outline_color']},&H80000000,-1,0,0,0,100,100,0,0,1,{style['outline_width']},{style['shadow_depth']},{style['alignment']},20,20,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _extract_word_timestamps(segments: list[dict]) -> list[dict]:
    """
    Extract flat list of words with timestamps from Whisper segments.
    Each segment may have 'words' (if word_timestamps=True was used).
    Falls back to splitting segment text evenly across segment duration.
    Validates all timestamps to prevent downstream crashes.
    """
    words = []
    last_end = 0.0  # track for monotonicity enforcement

    for seg in segments:
        if not isinstance(seg, dict):
            continue

        if "words" in seg and seg["words"]:
            # Whisper provided word-level timestamps
            for w in seg["words"]:
                if not isinstance(w, dict):
                    continue
                text = (w.get("word") or w.get("text") or "").strip()
                if not text:
                    continue
                try:
                    start = float(w.get("start", 0))
                    end = float(w.get("end", 0))
                except (TypeError, ValueError):
                    continue
                # Guard: end must be > start, timestamps must be non-negative
                if start < 0:
                    start = 0
                if end <= start:
                    end = start + 0.1  # minimum 100ms word duration
                # Enforce monotonicity — prevent overlapping timestamps
                if start < last_end:
                    start = last_end
                if end <= start:
                    end = start + 0.1
                last_end = end
                words.append({"text": text, "start": start, "end": end})
        else:
            # Fall back: split segment text evenly
            seg_text = seg.get("text", "")
            if not isinstance(seg_text, str):
                continue
            seg_words = seg_text.strip().split()
            if not seg_words:
                continue
            try:
                seg_start = max(float(seg.get("start", 0)), 0)
                seg_end = float(seg.get("end", seg_start + 1))
            except (TypeError, ValueError):
                continue
            if seg_end <= seg_start:
                seg_end = seg_start + len(seg_words) * 0.3  # ~300ms per word fallback
            duration = seg_end - seg_start
            per_word = duration / len(seg_words)
            for i, w in enumerate(seg_words):
                w_start = max(seg_start + i * per_word, last_end)
                w_end = w_start + per_word
                last_end = w_end
                words.append({"text": w, "start": w_start, "end": w_end})

    return [w for w in words if w.get("text")]


# Phrase-aware line building (caption overhaul).
#
# The old grouper cut every `words_per_group` words blind — mid-sentence,
# mid-phrase — and each highlight event only spanned the active word's own
# start→end. Whisper words have natural gaps (breaths, pauses), so the
# caption VANISHED between words and between groups: the user saw 2-3-word
# fragments flash on and off. Two rules fix it:
#   1. Lines break at natural points (sentence punctuation, real speech
#      pauses, a per-style word/char budget) instead of a blind count.
#   2. Display is CONTINUOUS: each word's highlight event runs until the
#      next word starts, and the line holds on screen until the next line
#      appears (bounded by _LINE_HOLD_S so text doesn't linger through
#      long silences).
_SENTENCE_END = (".", "!", "?", "…", "。", "！", "？")
_TRAILING_QUOTES = "\"'”’)»]"
_PAUSE_BREAK_S = 0.6   # speech gap that forces a new line
_LINE_HOLD_S = 1.0     # max linger after a line's last word before clearing


def _max_chars_for(style: dict) -> int:
    """Char budget per caption line, derived from the portrait font size so
    bigger presets get shorter lines (ASS wraps overflow to a second visual
    line; the budget keeps us at 1-2 wrapped lines max)."""
    font_size = style.get("font_size_portrait") or 56
    return max(12, int(2200 / max(int(font_size), 1)))


def _group_words_into_lines(
    words: list[dict], max_words: int, max_chars: int,
) -> list[list[dict]]:
    """Split timed words into caption lines at natural boundaries: sentence
    punctuation, speech pauses > _PAUSE_BREAK_S, or the word/char budget."""
    lines: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for w in words:
        text = w["text"]
        if cur:
            prev = cur[-1]
            gap = float(w["start"]) - float(prev["end"])
            prev_token = prev["text"].rstrip(_TRAILING_QUOTES)
            if (
                len(cur) >= max_words
                or cur_chars + 1 + len(text) > max_chars
                or gap > _PAUSE_BREAK_S
                or prev_token.endswith(_SENTENCE_END)
            ):
                lines.append(cur)
                cur = []
                cur_chars = 0
        cur.append(w)
        cur_chars += (1 if cur_chars else 0) + len(text)
    if cur:
        lines.append(cur)

    # Orphan control: a 1-word line reads as a flash (the exact complaint
    # this grouper fixes), so fold it back into the previous line when it
    # continues that line's sentence — allowing the word budget to stretch
    # by 2 for a complete phrase. Char budget still applies.
    merged: list[list[dict]] = []
    for line in lines:
        if merged and len(line) == 1:
            prev = merged[-1]
            prev_token = prev[-1]["text"].rstrip(_TRAILING_QUOTES)
            gap = float(line[0]["start"]) - float(prev[-1]["end"])
            prev_chars = sum(len(w["text"]) for w in prev) + len(prev) - 1
            if (
                not prev_token.endswith(_SENTENCE_END)
                and gap <= _PAUSE_BREAK_S
                and len(prev) + 1 <= max_words + 2
                and prev_chars + 1 + len(line[0]["text"]) <= max_chars
            ):
                prev.append(line[0])
                continue
        merged.append(line)
    return merged


def _generate_ass_events(words: list[dict], style: dict) -> str:
    """
    Generate ASS dialogue events with word-by-word highlighting over
    phrase-length lines. The full line stays on screen for its whole
    duration; the highlight walks word to word with no display gaps.
    """
    max_words = style.get("words_per_group", 6)
    highlight = style.get("highlight_color", "&H0000FFFF")
    primary = style.get("primary_color", "&H00FFFFFF")
    lines = _group_words_into_lines(words, max_words, _max_chars_for(style))
    events = []

    for li, line in enumerate(lines):
        line_start = float(line[0]["start"])
        last_word_end = float(line[-1]["end"])
        if li + 1 < len(lines):
            next_start = float(lines[li + 1][0]["start"])
            # Hold the line until the next one appears, unless the silence
            # is long — then clear after a short linger.
            line_end = (
                next_start
                if next_start - last_word_end <= _LINE_HOLD_S
                else last_word_end + _LINE_HOLD_S * 0.6
            )
        else:
            line_end = last_word_end + 0.5
        if line_end <= line_start:
            continue  # degenerate line

        # Continuous per-word segments: word i is highlighted from its own
        # start (or the line start, for word 0) until word i+1 starts (or
        # the line end, for the last word). Pauses inside the line keep the
        # previous word highlighted instead of blanking the caption.
        bounds = [line_start]
        for w in line[1:]:
            bounds.append(max(float(w["start"]), bounds[-1]))
        bounds.append(max(line_end, bounds[-1]))

        for active_idx in range(len(line)):
            seg_start, seg_end = bounds[active_idx], bounds[active_idx + 1]
            if seg_end <= seg_start:
                continue  # zero-length segment (stacked timestamps)

            parts = []
            for j, w in enumerate(line):
                if j == active_idx:
                    parts.append(f"{{\\1c{highlight}\\b1}}{w['text']}{{\\1c{primary}\\b0}}")
                else:
                    parts.append(w["text"])

            text = " ".join(parts)
            start_ts = _format_ass_time(seg_start)
            end_ts = _format_ass_time(seg_end)
            events.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{text}")

    return "\n".join(events)


async def generate_captions_ass(
    segments: list[dict],
    style: str = "viral",
    aspect_ratio: str = "9:16",
    output_path: Path = None,
    emoji_style: str = "moderate",
) -> Path:
    """
    Generate ASS subtitle file with word-by-word animation.

    Args:
        segments: Whisper segments with word timestamps.
                  Each: {"start": float, "end": float, "text": str, "words": [...]}
        style: Caption style preset name.
        aspect_ratio: "9:16", "1:1" or "16:9".
        output_path: Where to write the ASS file.
        emoji_style: "none" | "minimal" | "moderate" | "heavy"

    Returns:
        Path to the generated ASS file.
    """
    if output_path is None:
        from uuid import uuid4
        output_path = settings.TMP_DIR / f"captions_{uuid4().hex[:8]}.ass"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    style_config = CAPTION_STYLES.get(style) or await _load_custom_style(style) or CAPTION_STYLES["viral"]

    if aspect_ratio == "9:16":
        resolution = (1080, 1920)
    elif aspect_ratio == "1:1":
        resolution = (1080, 1080)
    else:  # 16:9
        resolution = (1920, 1080)

    # Extract word-level timestamps
    words = _extract_word_timestamps(segments)
    if not words:
        logger.warning("No words found in segments — generating empty caption file")
        output_path.write_text("")
        return output_path

    # Auto-insert emojis based on keyword matching
    words = insert_emojis_into_words(words, emoji_style)

    # Script-aware font fallback: if the caption text needs a script the
    # style's Latin display font can't render (CJK, Arabic, …), swap in a
    # platform system font with coverage — otherwise libass burns tofu boxes.
    # Copy-on-write: CAPTION_STYLES entries are shared module state.
    full_text = " ".join(w["text"] for w in words)
    fallback_font = resolve_caption_font(style_config["font"], full_text)
    if fallback_font != style_config["font"]:
        style_config = {**style_config, "font": fallback_font}

    # Build ASS file
    header = _build_ass_header(style_config, aspect_ratio, resolution)
    events = _generate_ass_events(words, style_config)

    content = header + events + "\n"
    output_path.write_text(content, encoding="utf-8")

    logger.info(f"ASS captions generated: {output_path} ({len(words)} words, style={style}, emoji={emoji_style})")
    return output_path


def _check_ffmpeg_ass_support() -> bool:
    """Check if FFmpeg was built with libass support (required for ASS captions)."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-filters"],
            capture_output=True, text=True, timeout=10,
        )
        return "ass" in result.stdout and "libass" in result.stdout
    except Exception:
        return False


# Cache the check — FFmpeg build doesn't change during runtime
_ffmpeg_has_ass: bool | None = None


async def burn_captions(
    video_path: Path,
    ass_path: Path,
    output_path: Path = None,
) -> Path:
    """Burn ASS captions into video using FFmpeg's libass filter."""
    global _ffmpeg_has_ass

    if output_path is None:
        output_path = video_path.parent / f"{video_path.stem}_captioned.mp4"

    if not ass_path.exists() or ass_path.stat().st_size == 0:
        logger.warning("Empty or missing ASS file — returning original video")
        return video_path

    # Check libass support once
    if _ffmpeg_has_ass is None:
        _ffmpeg_has_ass = _check_ffmpeg_ass_support()

    if not _ffmpeg_has_ass:
        logger.error(
            "FFmpeg is missing libass support — captions CANNOT be burned. "
            "Install FFmpeg with libass: brew install homebrew-ffmpeg/ffmpeg/ffmpeg "
            "(macOS) or sudo apt install ffmpeg libass-dev (Linux)"
        )
        return video_path

    def _burn():
        # Escape path for FFmpeg filter (colons and backslashes)
        escaped_ass = str(ass_path.resolve()).replace("\\", "/").replace(":", "\\:")

        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vf", f"ass={escaped_ass}",
            "-c:a", "copy",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.error(f"ASS caption burn FFmpeg error: {result.stderr[:500]}")
            return video_path  # Return original on failure
        logger.info(f"Captions burned successfully: {output_path}")
        return output_path

    return await asyncio.to_thread(_burn)
