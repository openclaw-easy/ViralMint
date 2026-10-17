# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Provider-agnostic stock media search.

Pexels' scoring is pinned by tests/test_pexels_service.py (it kept its own
suite when the parsing moved into an adapter). This file covers what is new:
Pixabay's response mapped onto the same `StockClip`, and the selection every
search goes through — which provider answers, in what order, and what a
failing provider costs.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.config import settings
from backend.core.exceptions import VideoGenerationError
from backend.services import pexels_service as px
from backend.services import stock_media as sm


def _resp(payload):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=payload)
    return r


def _size(width, height, url="http://x/v.mp4"):
    return {"url": url, "width": width, "height": height, "size": 1000}


def _hit(hit_id=1, duration=12, page_url=None, **sizes):
    """A Pixabay video hit; every size defaults to the empty 0x0 rendition."""
    videos = {"large": _size(0, 0, ""), "medium": _size(0, 0, ""),
              "small": _size(0, 0, ""), "tiny": _size(0, 0, "")}
    videos.update(sizes)
    return {
        "id": hit_id,
        "pageURL": page_url or f"https://pixabay.com/videos/id-{hit_id}/",
        "duration": duration,
        "videos": videos,
    }


def _client(payload=None, side_effect=None):
    client = MagicMock()
    client.get = AsyncMock(return_value=_resp(payload) if side_effect is None else None,
                           side_effect=side_effect)
    return client


@pytest.fixture
def keys(monkeypatch):
    """Set the provider keys for one test; every test starts with none."""
    monkeypatch.setattr(settings, "PEXELS_API_KEY", "")
    monkeypatch.setattr(settings, "PIXABAY_API_KEY", "")
    monkeypatch.setattr(settings, "STOCK_MEDIA_PROVIDER", "auto")

    def _set(pexels="", pixabay="", provider="auto"):
        monkeypatch.setattr(settings, "PEXELS_API_KEY", pexels)
        monkeypatch.setattr(settings, "PIXABAY_API_KEY", pixabay)
        monkeypatch.setattr(settings, "STOCK_MEDIA_PROVIDER", provider)

    return _set


class TestPixabayAdapter:
    async def test_normalizes_a_portrait_hit_into_a_stock_clip(self):
        hit = _hit(7, duration=20, large=_size(1080, 1920, "http://x/large.mp4"))
        provider = sm.PixabayProvider("k", http=_client({"hits": [hit]}))

        clips = await provider.search("ocean", orientation="portrait")

        assert len(clips) == 1
        clip = clips[0]
        assert clip["id"] == 7
        assert clip["provider"] == "pixabay"
        assert clip["download_url"] == "http://x/large.mp4"
        assert (clip["width"], clip["height"]) == (1080, 1920)
        assert clip["page_url"] == "https://pixabay.com/videos/id-7/"
        assert clip["duration"] == 20
        # hd(100) + reasonable-size(50) + 1080/100(10.8) + duration 20/5(4)
        assert clip["quality_score"] == pytest.approx(164.8, abs=0.01)

    async def test_picks_a_landscape_rendition_for_a_landscape_video(self):
        hit = _hit(1, large=_size(1920, 1080, "http://x/hd.mp4"),
                   medium=_size(1280, 720, "http://x/sd.mp4"))
        provider = sm.PixabayProvider("k", http=_client({"hits": [hit]}))

        clips = await provider.search("city", orientation="landscape")

        assert [c["download_url"] for c in clips] == ["http://x/hd.mp4"]

    async def test_an_empty_large_rendition_falls_back_to_medium(self):
        # Pixabay returns large as 0x0 when the video has no 1080p version.
        hit = _hit(1, medium=_size(1280, 720, "http://x/medium.mp4"))
        provider = sm.PixabayProvider("k", http=_client({"hits": [hit]}))

        clips = await provider.search("city", orientation="landscape")

        assert [c["download_url"] for c in clips] == ["http://x/medium.mp4"]

    async def test_skips_clips_that_are_too_small_or_the_wrong_shape(self):
        hits = [
            _hit(1, large=_size(640, 360, "http://x/tiny.mp4")),   # too small
            _hit(2, large=_size(1920, 1080, "http://x/wide.mp4")),  # wrong shape
        ]
        provider = sm.PixabayProvider("k", http=_client({"hits": hits}))

        assert await provider.search("x", orientation="portrait") == []

    async def test_clamps_per_page_into_the_range_the_api_accepts(self):
        hits = []

        for requested, sent in ((1, 3), (500, 200), (15, 15)):
            client = _client({"hits": hits})
            await sm.PixabayProvider("k", http=client).search("x", per_page=requested)
            assert client.get.call_args.kwargs["params"]["per_page"] == sent

    async def test_sends_the_key_safesearch_and_a_popularity_order(self):
        client = _client({"hits": []})

        await sm.PixabayProvider("SECRET", http=client).search("x")

        params = client.get.call_args.kwargs["params"]
        assert params["key"] == "SECRET"
        assert params["q"] == "x"
        assert params["safesearch"] == "true"
        assert params["order"] == "popular"
        assert client.get.call_args.args[0] == sm.PIXABAY_API

    async def test_requires_a_key_and_does_not_call_the_api(self):
        client = _client({"hits": []})
        with pytest.raises(VideoGenerationError):
            await sm.PixabayProvider("", http=client).search("x")
        client.get.assert_not_called()

    async def test_malformed_payloads_produce_no_clips(self):
        for payload in ({}, {"hits": None}, {"hits": []}, {"hits": [{}]},
                        {"hits": [{"id": 1, "videos": None}]}):
            provider = sm.PixabayProvider("k", http=_client(payload))
            assert await provider.search("x") == [], payload

    async def test_longer_clips_rank_first(self):
        hits = [
            _hit(1, duration=4, large=_size(1080, 1920, "http://x/short.mp4")),
            _hit(2, duration=60, large=_size(1080, 1920, "http://x/long.mp4")),
        ]
        provider = sm.PixabayProvider("k", http=_client({"hits": hits}))

        clips = await provider.search("x", orientation="portrait")

        assert [c["id"] for c in clips] == [2, 1]
        assert clips[0]["quality_score"] > clips[1]["quality_score"]


class TestProviderSelection:
    def test_auto_puts_pexels_first_so_existing_installs_keep_their_footage(self, keys):
        keys(pexels="p", pixabay="b")
        assert sm.configured_providers() == [sm.StockProvider.PEXELS, sm.StockProvider.PIXABAY]

    def test_auto_skips_providers_without_a_key(self, keys):
        keys(pixabay="b")
        assert sm.configured_providers() == [sm.StockProvider.PIXABAY]

    def test_a_pinned_provider_wins_even_when_both_keys_are_set(self, keys):
        keys(pexels="p", pixabay="b", provider="pixabay")
        assert sm.configured_providers() == [sm.StockProvider.PIXABAY]

    def test_an_unknown_pinned_provider_falls_back_to_auto(self, keys, caplog):
        keys(pexels="p", provider="getty")
        with caplog.at_level("WARNING"):
            assert sm.configured_providers() == [sm.StockProvider.PEXELS]
        assert "STOCK_MEDIA_PROVIDER" in caplog.text

    def test_an_empty_provider_setting_means_auto(self, keys):
        keys(pexels="p", provider="")
        assert sm.configured_providers() == [sm.StockProvider.PEXELS]

    async def test_no_keys_is_a_configuration_error(self, keys):
        keys()
        with pytest.raises(VideoGenerationError) as err:
            await sm.search_clips("x")
        assert "PEXELS_API_KEY" in str(err.value) and "PIXABAY_API_KEY" in str(err.value)

    async def test_a_pinned_provider_without_its_key_names_the_missing_setting(self, keys):
        keys(pexels="p", provider="pixabay")
        with pytest.raises(VideoGenerationError) as err:
            await sm.search_clips("x")
        assert "PIXABAY_API_KEY" in str(err.value)


class TestSearchAcrossProviders:
    async def test_answers_from_the_first_configured_provider(self, keys):
        keys(pexels="p", pixabay="b")
        pexels_payload = {"videos": [{
            "id": 11, "url": "https://pexels.com/v/11", "duration": 20,
            "video_files": [{"link": "http://x/pexels.mp4", "width": 1080,
                             "height": 1920, "quality": "hd"}],
        }]}
        client = _client(pexels_payload)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sm, "_http", client)
            clips = await sm.search_clips("ocean", orientation="portrait")

        assert [c["provider"] for c in clips] == ["pexels"]
        assert clips[0]["download_url"] == "http://x/pexels.mp4"
        assert len(client.get.call_args_list) == 1, "Pixabay should not be asked when Pexels answers"

    async def test_a_failing_provider_hands_the_query_to_the_next_one(self, keys):
        keys(pexels="p", pixabay="b")

        def route(url, **kwargs):
            if "pexels" in url:
                raise httpx.ConnectError("pexels is down")
            return _resp({"hits": [_hit(5, large=_size(1080, 1920, "http://x/pixabay.mp4"))]})

        client = MagicMock()
        client.get = AsyncMock(side_effect=route)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sm, "_http", client)
            clips = await sm.search_clips("ocean", orientation="portrait")

        assert [(c["provider"], c["id"]) for c in clips] == [("pixabay", 5)]

    async def test_empty_results_fall_through_too(self, keys):
        keys(pexels="p", pixabay="b")

        def route(url, **kwargs):
            if "pexels" in url:
                return _resp({"videos": []})
            return _resp({"hits": [_hit(5, large=_size(1080, 1920))]})

        client = MagicMock()
        client.get = AsyncMock(side_effect=route)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sm, "_http", client)
            clips = await sm.search_clips("ocean", orientation="portrait")

        assert [c["provider"] for c in clips] == ["pixabay"]

    async def test_a_timeout_is_a_failure_not_a_crash(self, keys):
        keys(pexels="p", pixabay="b")
        client = MagicMock()
        client.get = AsyncMock(side_effect=httpx.ReadTimeout("too slow"))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sm, "_http", client)
            assert await sm.search_clips("ocean") == []

    async def test_a_pinned_provider_never_falls_back(self, keys):
        keys(pexels="p", pixabay="b", provider="pexels")
        client = MagicMock()
        client.get = AsyncMock(side_effect=httpx.ConnectError("down"))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sm, "_http", client)
            assert await sm.search_clips("ocean") == []

        # only the pinned provider was contacted
        assert len(client.get.call_args_list) == 1


class TestPipelineUsesTheProviderLayer:
    """The stock-video pipeline asks for clips and nothing else: whatever the
    adapter returned has to carry the whole render without provider fields."""

    async def test_a_pixabay_response_drives_the_whole_pipeline(self, keys, tmp_path):
        keys(pixabay="b")

        ids: dict[str, int] = {}

        def route(url, params=None, **kwargs):
            query = (params or {}).get("q", "")
            hit_id = ids.setdefault(query, len(ids) + 1)
            return _resp({"hits": [_hit(
                hit_id, duration=30,
                large=_size(1080, 1920, f"http://cdn.pixabay.com/{hit_id}.mp4"),
            )]})

        client = MagicMock()
        client.get = AsyncMock(side_effect=route)
        downloaded: list[str] = []

        async def fake_download(url, output_path):
            downloaded.append(url)
            return output_path

        with patch.object(sm, "_http", client), \
             patch.object(px, "download_clip", side_effect=fake_download), \
             patch.object(px, "trim_and_normalize_clip", AsyncMock(side_effect=lambda *a, **k: a[4])), \
             patch.object(px, "probe_duration", return_value=30.0), \
             patch("backend.services.ffmpeg_service.stitch_clips",
                   AsyncMock(return_value=tmp_path / "stitched.mp4")), \
             patch("shutil.move"):
            result = await px.build_stock_video(
                "alpha bravo charlie delta echo", voice_path=None,
                aspect_ratio="9:16", ai_client=None,
                output_path=tmp_path / "final.mp4")

        assert result == tmp_path / "final.mp4"
        # every search went to Pixabay, and no Pexels key was set to fall back to
        assert {c.args[0] for c in client.get.call_args_list} == {sm.PIXABAY_API}
        assert downloaded, "the pipeline downloaded the clips the adapter returned"
        assert all("cdn.pixabay.com" in url for url in downloaded)
        assert len(set(downloaded)) == len(downloaded), "distinct scenes kept distinct clips"

    async def test_no_configured_provider_degrades_instead_of_crashing(self, keys, tmp_path):
        """A missing key is already checked before this runs; if one is pulled
        while a render is queued, the job loses its footage, not the process."""
        keys()

        with patch.object(px, "download_clip", AsyncMock()), \
             patch("backend.services.ffmpeg_service.stitch_clips", AsyncMock()):
            result = await px.build_stock_video(
                "alpha bravo charlie delta echo", voice_path=None,
                aspect_ratio="9:16", ai_client=None,
                output_path=tmp_path / "final.mp4")

        assert result is None
