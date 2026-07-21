# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for the caption-rendering ports from the hosted variant.

Covers the placement fix (bottom-anchored alignment), the new caption styles,
script-aware font fallback for non-Latin text, the CJK homophone-correction
helper (fail-open), and the hook-overlay parameters on generate_captions_ass.
"""
import inspect

from backend.services import caption_service as cs
from backend.services.caption_service import CAPTION_STYLES


class TestCaptionPlacement:
    def test_active_styles_are_bottom_anchored(self):
        # Regression: viral/bold/neon/glow shipped alignment=5 (frame-center,
        # which ignores margin_v). They must be alignment=2 (bottom).
        for style in ("viral", "bold", "neon", "glow"):
            if style in CAPTION_STYLES:
                assert CAPTION_STYLES[style].get("alignment") == 2, style


class TestNewStyles:
    def test_new_styles_present(self):
        for style in ("brainrot", "urban", "warm", "mono"):
            assert style in CAPTION_STYLES

    def test_every_style_has_margin_v(self):
        # The existing suite asserts required keys; the ported styles must keep
        # the margin_v fallback so header building never KeyErrors.
        for name, cfg in CAPTION_STYLES.items():
            assert "margin_v" in cfg, name


class TestScriptFontFallback:
    def test_detects_cjk(self):
        assert cs.detect_non_latin_script("你好世界这是测试") is not None

    def test_latin_is_none(self):
        assert cs.detect_non_latin_script("hello world 123") is None

    def test_resolve_font_swaps_for_cjk(self):
        # A Latin-only preferred font must be replaced with a CJK-capable one
        # for CJK text, but left alone for Latin text.
        latin = cs.resolve_caption_font("Montserrat", "hello world")
        cjk = cs.resolve_caption_font("Montserrat", "你好世界")
        assert latin == "Montserrat"
        assert isinstance(cjk, str) and cjk  # resolved to some concrete font


class TestScriptAlign:
    def _seg(self, text, words):
        return {
            "text": text,
            "start": words[0][1],
            "end": words[-1][2],
            "words": [{"word": w, "start": s, "end": e} for w, s, e in words],
        }

    def test_fail_open_on_non_cjk(self):
        segs = [self._seg("hello world", [("hello", 0, 0.5), ("world", 0.5, 1.0)])]
        # A non-CJK script must leave the ASR segments untouched.
        assert cs.align_script_to_segments("totally different english", segs) == segs

    def test_empty_inputs_return_segments(self):
        segs = [self._seg("你好", [("你", 0, 0.5), ("好", 0.5, 1.0)])]
        assert cs.align_script_to_segments("", segs) == segs
        assert cs.align_script_to_segments("你好", []) == []

    def test_cjk_returns_same_length_list(self):
        segs = [self._seg("你好", [("你", 0, 0.5), ("好", 0.5, 1.0)])]
        out = cs.align_script_to_segments("你好", segs)
        assert isinstance(out, list) and len(out) == 1


class TestHookOverlaySignature:
    def test_generate_captions_ass_accepts_hook_params(self):
        params = inspect.signature(cs.generate_captions_ass).parameters
        assert "hook_text" in params
        assert "hook_duration" in params
        # Backward-compatible: hook is optional/off by default.
        assert params["hook_text"].default is None
