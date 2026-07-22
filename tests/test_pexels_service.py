# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend/services/pexels_service.py.

Covers the parsing / quality-scoring / sorting logic of `search_videos`,
the streaming `download_clip` (empty-download guard), and the AI-driven
`extract_visual_scenes` (fence stripping, JSON parse, fallback list). The
module-level shared httpx client `_http` is patched so NO real network I/O
happens; the AI client is a mock.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services import pexels_service as px
from backend.core.exceptions import VideoGenerationError


def _resp(payload):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=payload)
    return r


def _vfile(w, h, quality="hd", link="http://x/f.mp4"):
    return {"width": w, "height": h, "quality": quality, "link": link}


class TestSearchVideos:
    async def test_no_api_key_raises(self):
        with pytest.raises(VideoGenerationError):
            await px.search_videos("cats", api_key="")

    async def test_parses_and_scores_portrait(self):
        payload = {"videos": [
            {"id": 1, "url": "u1", "duration": 30,
             "video_files": [_vfile(1080, 1920, "hd")]},
        ]}
        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=_resp(payload))
        with patch.object(px, "_http", mock_http):
            out = await px.search_videos("ocean", orientation="portrait", api_key="k")
        assert len(out) == 1
        v = out[0]
        assert v["id"] == 1
        assert v["download_url"] == "http://x/f.mp4"
        assert v["width"] == 1080 and v["height"] == 1920
        # hd(100) + reasonable-size(50) + 1080/100(10.8) = 160.8; +dur bonus min(30/5,10)=6
        assert v["quality_score"] == pytest.approx(166.8, abs=0.01)

    async def test_sends_authorization_header(self):
        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=_resp({"videos": []}))
        with patch.object(px, "_http", mock_http):
            await px.search_videos("x", api_key="SECRET")
        assert mock_http.get.call_args.kwargs["headers"]["Authorization"] == "SECRET"

    async def test_filters_too_small_and_wrong_orientation(self):
        payload = {"videos": [
            # too small for portrait
            {"id": 1, "url": "", "duration": 5, "video_files": [_vfile(320, 480)]},
            # landscape file while portrait requested
            {"id": 2, "url": "", "duration": 5, "video_files": [_vfile(1920, 1080)]},
        ]}
        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=_resp(payload))
        with patch.object(px, "_http", mock_http):
            out = await px.search_videos("x", orientation="portrait", api_key="k")
        assert out == []

    async def test_sorted_by_quality_score_desc(self):
        payload = {"videos": [
            {"id": 1, "url": "", "duration": 5,   # small dur bonus
             "video_files": [_vfile(720, 1280, "sd")]},
            {"id": 2, "url": "", "duration": 60,  # long clip, hd
             "video_files": [_vfile(1080, 1920, "hd")]},
        ]}
        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=_resp(payload))
        with patch.object(px, "_http", mock_http):
            out = await px.search_videos("x", orientation="portrait", api_key="k")
        assert [v["id"] for v in out] == [2, 1]
        assert out[0]["quality_score"] >= out[1]["quality_score"]

    async def test_landscape_orientation_accepts_wide(self):
        payload = {"videos": [
            {"id": 9, "url": "", "duration": 10,
             "video_files": [_vfile(1920, 1080, "hd")]},
        ]}
        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=_resp(payload))
        with patch.object(px, "_http", mock_http):
            out = await px.search_videos("x", orientation="landscape", api_key="k")
        assert len(out) == 1 and out[0]["id"] == 9

    async def test_video_with_no_usable_file_skipped(self):
        payload = {"videos": [
            {"id": 3, "url": "", "duration": 5, "video_files": []},
        ]}
        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=_resp(payload))
        with patch.object(px, "_http", mock_http):
            out = await px.search_videos("x", api_key="k")
        assert out == []

    async def test_big_but_wrong_orientation_file_skipped(self):
        # large enough in both dims for portrait mins, but landscape shape → skipped
        payload = {"videos": [
            {"id": 4, "url": "", "duration": 5, "video_files": [_vfile(1920, 1280, "hd")]},
        ]}
        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=_resp(payload))
        with patch.object(px, "_http", mock_http):
            out = await px.search_videos("x", orientation="portrait", api_key="k")
        assert out == []


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def aiter_bytes(self, chunk_size=65536):
        for c in self._chunks:
            yield c


class TestDownloadClip:
    async def test_writes_bytes(self, tmp_path):
        out = tmp_path / "sub" / "clip.mp4"
        mock_http = MagicMock()
        mock_http.stream = MagicMock(return_value=_FakeStream([b"aaa", b"bbb"]))
        with patch.object(px, "_http", mock_http):
            res = await px.download_clip("http://x/v.mp4", out)
        assert res == out
        assert out.read_bytes() == b"aaabbb"

    async def test_empty_download_raises(self, tmp_path):
        out = tmp_path / "clip.mp4"
        mock_http = MagicMock()
        mock_http.stream = MagicMock(return_value=_FakeStream([]))
        with patch.object(px, "_http", mock_http):
            with pytest.raises(VideoGenerationError):
                await px.download_clip("http://x/v.mp4", out)


class TestExtractVisualScenes:
    async def test_parses_plain_json_array(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value='[{"query": "military tank desert"}, {"query": "oil refinery"}]')
        out = await px.extract_visual_scenes("script", ai, num_scenes=8)
        assert out == [{"query": "military tank desert"}, {"query": "oil refinery"}]

    async def test_strips_code_fence(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value='```json\n[{"query": "city aerial"}]\n```')
        out = await px.extract_visual_scenes("script", ai)
        assert out == [{"query": "city aerial"}]

    async def test_truncates_to_num_scenes(self):
        ai = MagicMock()
        items = [{"query": f"q{i}"} for i in range(10)]
        import json as _json
        ai.chat = AsyncMock(return_value=_json.dumps(items))
        out = await px.extract_visual_scenes("script", ai, num_scenes=3)
        assert len(out) == 3

    async def test_ai_exception_returns_fallback(self):
        ai = MagicMock()
        ai.chat = AsyncMock(side_effect=RuntimeError("down"))
        out = await px.extract_visual_scenes("script", ai, num_scenes=4)
        assert len(out) == 4
        assert all("query" in s for s in out)

    async def test_empty_list_falls_back(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="[]")
        out = await px.extract_visual_scenes("script", ai, num_scenes=5)
        # empty parse → falls through to the built-in fallback list
        assert len(out) == 5

    async def test_json_error_routes_through_ai_fix_json(self):
        ai = MagicMock()
        ai.chat = AsyncMock(return_value="[{not valid json}]")
        with patch("backend.core.ai_retry.ai_fix_json",
                   AsyncMock(return_value=[{"query": "recovered"}])):
            out = await px.extract_visual_scenes("script", ai, num_scenes=4)
        assert out == [{"query": "recovered"}]


def _proc(returncode=0, stderr=""):
    return MagicMock(returncode=returncode, stderr=stderr)


class TestTrimAndNormalizeClip:
    async def test_success_returns_output_path(self, tmp_path):
        clip = tmp_path / "in.mp4"
        clip.write_bytes(b"x")
        out = tmp_path / "out.mp4"
        with patch.object(px.subprocess, "run", return_value=_proc(0)):
            res = await px.trim_and_normalize_clip(clip, 5.0, 1080, 1920, out)
        assert res == out

    async def test_color_grade_fail_then_retry_success(self, tmp_path):
        clip = tmp_path / "in.mp4"
        clip.write_bytes(b"x")
        out = tmp_path / "out.mp4"
        with patch.object(px.subprocess, "run",
                          side_effect=[_proc(1, "bad filter"), _proc(0)]) as run:
            res = await px.trim_and_normalize_clip(clip, 5.0, 1080, 1920, out, color_grade=True)
        assert res == out
        assert run.call_count == 2

    async def test_both_attempts_fail_returns_original(self, tmp_path):
        clip = tmp_path / "in.mp4"
        clip.write_bytes(b"x")
        out = tmp_path / "out.mp4"
        with patch.object(px.subprocess, "run",
                          side_effect=[_proc(1, "e1"), _proc(1, "e2")]):
            res = await px.trim_and_normalize_clip(clip, 5.0, 1080, 1920, out, color_grade=True)
        assert res == clip

    async def test_no_color_grade_fail_returns_original(self, tmp_path):
        clip = tmp_path / "in.mp4"
        clip.write_bytes(b"x")
        out = tmp_path / "out.mp4"
        with patch.object(px.subprocess, "run", return_value=_proc(1, "e")):
            res = await px.trim_and_normalize_clip(clip, 5.0, 1080, 1920, out, color_grade=False)
        assert res == clip


class TestMergeVideoAudioFull:
    async def test_success(self, tmp_path):
        out = tmp_path / "final.mp4"
        with patch.object(px.subprocess, "run", return_value=_proc(0)):
            res = await px._merge_video_audio_full(
                tmp_path / "v.mp4", tmp_path / "a.mp3", out, 60.0)
        assert res == out

    async def test_loop_fail_then_simple_success(self, tmp_path):
        out = tmp_path / "final.mp4"
        with patch.object(px.subprocess, "run",
                          side_effect=[_proc(1, "loop bad"), _proc(0)]) as run:
            res = await px._merge_video_audio_full(
                tmp_path / "v.mp4", tmp_path / "a.mp3", out, 60.0)
        assert res == out and run.call_count == 2

    async def test_both_fail_raises(self, tmp_path):
        out = tmp_path / "final.mp4"
        with patch.object(px.subprocess, "run",
                          side_effect=[_proc(1, "e1"), _proc(1, "e2")]):
            with pytest.raises(VideoGenerationError):
                await px._merge_video_audio_full(
                    tmp_path / "v.mp4", tmp_path / "a.mp3", out, 60.0)


class TestBuildStockVideo:
    async def test_happy_path_no_voice(self, tmp_path):
        out = tmp_path / "final.mp4"
        candidate = {"id": 1, "download_url": "http://x/c.mp4", "duration": 12,
                     "quality_score": 100}

        async def fake_search(query, orientation="portrait", per_page=10, api_key=""):
            # unique id per query so dedup keeps them all
            return [{**candidate, "id": abs(hash(query)) % 100000}]

        stitched = tmp_path / "stitched.mp4"

        with patch.object(px, "search_videos", side_effect=fake_search), \
             patch.object(px, "download_clip", AsyncMock(return_value=tmp_path / "raw.mp4")), \
             patch.object(px, "trim_and_normalize_clip",
                          AsyncMock(side_effect=lambda *a, **k: a[4] if len(a) > 4 else tmp_path / "t.mp4")), \
             patch.object(px, "probe_duration", return_value=10.0), \
             patch("backend.services.ffmpeg_service.stitch_clips",
                   AsyncMock(return_value=stitched)), \
             patch("shutil.move") as mv:
            res = await px.build_stock_video(
                "alpha bravo charlie delta echo foxtrot golf hotel india juliet",
                voice_path=None, pexels_api_key="k", aspect_ratio="9:16",
                ai_client=None, output_path=out)
        assert res == out
        mv.assert_called_once()

    async def test_no_clips_returns_none(self, tmp_path):
        out = tmp_path / "final.mp4"
        with patch.object(px, "search_videos", AsyncMock(return_value=[])):
            res = await px.build_stock_video(
                "alpha bravo charlie delta echo foxtrot",
                voice_path=None, pexels_api_key="k", aspect_ratio="16:9",
                ai_client=None, output_path=out)
        assert res is None
