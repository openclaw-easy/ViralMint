# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""9:16 asks Pexels for portrait first: a Honeybees project had 0 of 38 portrait candidates, so every scene
kept ~26% of a 1920x1080 clip. The ranking downstream can't help when no portrait clip is in the pool; only
the search can. Off-topic clips are demoted on tall targets, never dropped, and the Ken Burns margin no
longer stacks on a crop that already took most of the picture."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from backend.services import stock_search_cache as C
from backend.services import stock_video_service as S


def _video(vid, slug, w, h):
    return {"id": vid, "url": f"https://www.pexels.com/video/{slug}-{vid}/", "duration": 9, "user": {"name": "A"},
            "image": f"https://images.pexels.com/{vid}.jpg",
            "video_files": [{"link": f"https://videos.pexels.com/{vid}.mp4", "width": w, "height": h, "quality": "hd"}]}


def _http(monkeypatch, portrait=(), anywhere=(), status=None, plain_status=None, pixabay_hits=None):
    """Answers like Pexels: `orientation=portrait` → the portrait list, no orientation → the mixed list.
    Pixabay calls (same shared client) answer `pixabay_hits`."""
    calls = []

    async def get(url, params=None, headers=None):
        resp = MagicMock()
        if url == S.PIXABAY_API:
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"hits": list(pixabay_hits or [])})
            return resp
        calls.append(dict(params))
        failing = status or (plain_status if params.get("orientation") != "portrait" else None)
        if failing:
            req = httpx.Request("GET", url)
            resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
                "x", request=req, response=httpx.Response(failing, request=req)))
            return resp
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"videos": list(portrait if params.get("orientation") == "portrait" else anywhere)})
        return resp

    monkeypatch.setattr(S, "_http", MagicMock(get=AsyncMock(side_effect=get)))
    return calls


BEES_TALL = [_video(i, "honey-bees-on-a-comb", 1080, 1920) for i in range(1, 7)]
BEES_WIDE = [_video(i, "bees-at-the-hive", 1920, 1080) for i in range(101, 107)]
OFF_TALL = [_video(i, "woman-dancing-in-a-park", 1080, 1920) for i in range(201, 207)]
OLD = {"id": 900, "width": 1920, "height": 1080, "desc_words": ["honey", "bees"], "quality_score": 1}


class TestPortraitFirst:
    async def test_enough_on_topic_portrait_clips_cost_one_call(self, monkeypatch):
        calls = _http(monkeypatch, portrait=BEES_TALL, anywhere=BEES_WIDE)
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert len(calls) == 1 and calls[0]["orientation"] == "portrait"
        assert calls[0]["size"] == "small" and calls[0]["per_page"] == 15
        assert all(c["height"] > c["width"] for c in out) and len(out) == 6

    async def test_a_niche_topic_still_finds_landscape_clips(self, monkeypatch):
        calls = _http(monkeypatch, portrait=[], anywhere=BEES_WIDE)
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert [c.get("orientation") for c in calls] == ["portrait", None]
        assert {c["id"] for c in out} == {v["id"] for v in BEES_WIDE}

    async def test_few_portrait_clips_are_topped_up_portrait_first_without_duplicates(self, monkeypatch):
        tall = BEES_TALL[:2]
        calls = _http(monkeypatch, portrait=tall, anywhere=[tall[0], *BEES_WIDE])
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert len(calls) == 2
        assert [c["id"] for c in out][:2] == [1, 2] and len({c["id"] for c in out}) == len(out) == 8

    async def test_off_topic_portrait_clips_do_not_count_and_do_not_beat_on_topic_landscape(self, monkeypatch):
        calls = _http(monkeypatch, portrait=OFF_TALL, anywhere=BEES_WIDE)
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert len(calls) == 2
        assert {c["id"] for c in out[:6]} == {v["id"] for v in BEES_WIDE}
        assert {c["id"] for c in out[6:]} == {v["id"] for v in OFF_TALL}       # demoted, never lost

    async def test_on_topic_portrait_still_beats_on_topic_landscape(self, monkeypatch):
        _http(monkeypatch, portrait=BEES_TALL[:2], anywhere=BEES_WIDE)
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert out[0]["height"] > out[0]["width"] and out[1]["height"] > out[1]["width"]

    async def test_a_clip_with_no_slug_is_not_called_off_topic(self, monkeypatch):
        bare = [{**_video(301, "x", 1080, 1920), "url": "https://www.pexels.com/video/301/"}]
        _http(monkeypatch, portrait=bare, anywhere=BEES_WIDE)
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert out[0]["id"] == 301

    async def test_a_query_with_no_latin_words_can_not_judge_topic(self, monkeypatch):
        calls = _http(monkeypatch, portrait=OFF_TALL, anywhere=BEES_WIDE)
        out = await S.search_videos("蜜蜂", orientation="portrait", per_page=15, api_key="k")
        assert len(calls) == 1 and {c["id"] for c in out} == {v["id"] for v in OFF_TALL}

    @pytest.mark.parametrize("words,query,expected", [
        (["honeybee", "on", "flower"], "honeybees dancing", True),
        (["bee", "on", "flower"], "honeybees", True),
        (["dancer", "on", "stage"], "dancing couple", True),
        (["honey bee", "hive"], "bees", True),                     # a multi-word Pixabay tag
        (["woman", "in", "a", "park"], "honey bees", False),
        (["a", "the", "of"], "the end of a day", None),
        ([], "honey bees", None),
    ])
    def test_on_topic_reads_the_slug_loosely(self, words, query, expected):
        assert S.on_topic({"desc_words": words}, query) is expected

    async def test_three_on_topic_clips_skip_the_top_up(self, monkeypatch):
        calls = _http(monkeypatch, portrait=BEES_TALL[:3], anywhere=BEES_WIDE)
        await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert len(calls) == 1

    async def test_when_nothing_matches_the_slug_the_vertical_still_wins(self, monkeypatch):
        tall = [_video(i, "person-using-laptop", 1080, 1920) for i in range(401, 407)]
        wide = [_video(i, "person-at-a-desk", 1920, 1080) for i in range(501, 507)]
        _http(monkeypatch, portrait=tall, anywhere=wide)
        out = await S.search_videos("satisfying", orientation="portrait", per_page=15, api_key="k")
        assert {c["id"] for c in out[:6]} == {v["id"] for v in tall}

    async def test_a_full_page_never_grows_past_what_the_caller_asked(self, monkeypatch):
        _http(monkeypatch, portrait=BEES_TALL[:2], anywhere=BEES_WIDE)
        out = await S.search_videos("honey bees", orientation="portrait", per_page=5, api_key="k")
        assert len(out) == 5


class TestTheCache:
    async def test_a_cached_any_orientation_answer_can_not_stand_in_for_the_portrait_search(self, monkeypatch):
        await C.put("pexels", "honey bees", [OLD], orientation="portrait", per_page=15, size="small")
        calls = _http(monkeypatch, portrait=BEES_TALL, anywhere=[])
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert len(calls) == 1 and calls[0]["orientation"] == "portrait"
        assert 900 not in {c["id"] for c in out} and len(out) == 6

    async def test_the_top_up_reads_the_entry_older_builds_wrote(self, monkeypatch):
        await C.put("pexels", "honey bees", [OLD], orientation="portrait", per_page=15, size="small")
        calls = _http(monkeypatch, portrait=[])
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert [c.get("orientation") for c in calls] == ["portrait"]
        assert [c["id"] for c in out] == [900]

    async def test_a_repeat_search_makes_no_calls_at_all(self, monkeypatch):
        calls = _http(monkeypatch, portrait=BEES_TALL[:2], anywhere=BEES_WIDE)
        first = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        again = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert len(calls) == 2 and [c["id"] for c in again] == [c["id"] for c in first]

    async def test_the_cached_list_is_not_stored_with_the_demotion(self, monkeypatch):
        _http(monkeypatch, portrait=OFF_TALL, anywhere=BEES_WIDE)
        first = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        again = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert [c["quality_score"] for c in again] == [c["quality_score"] for c in first]


class TestRateLimit:
    async def test_a_429_does_not_spend_a_second_call(self, monkeypatch):
        calls = _http(monkeypatch, status=429)
        with pytest.raises(httpx.HTTPStatusError):
            await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert len(calls) == 1

    async def test_a_429_still_answers_from_the_cached_fallback(self, monkeypatch):
        await C.put("pexels", "honey bees", [OLD], orientation="portrait", per_page=15, size="small")
        _http(monkeypatch, status=429)
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert [c["id"] for c in out] == [900]

    async def test_a_broken_portrait_search_falls_back(self, monkeypatch):
        answers = iter([httpx.ConnectError("boom"), None])
        ok = MagicMock()
        ok.raise_for_status = MagicMock()
        ok.json = MagicMock(return_value={"videos": BEES_WIDE})

        async def get(url, params=None, headers=None):
            a = next(answers)
            if a:
                raise a
            return ok
        monkeypatch.setattr(S, "_http", MagicMock(get=AsyncMock(side_effect=get)))
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert len(out) == 6

    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_a_failed_top_up_keeps_the_portrait_clips_it_has(self, monkeypatch, status):
        calls = _http(monkeypatch, portrait=BEES_TALL[:2], plain_status=status)
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert len(calls) == 2 and [c["id"] for c in out] == [1, 2]

    async def test_nothing_found_and_a_failed_top_up_still_says_it_failed(self, monkeypatch):
        _http(monkeypatch, portrait=[], plain_status=500)
        with pytest.raises(httpx.HTTPStatusError):
            await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")

    async def test_a_refusal_is_seen_even_when_the_search_survives_it(self, monkeypatch):
        _http(monkeypatch, portrait=BEES_TALL[:2], plain_status=429)
        refused = C.watch_refusals()
        out = await S.search_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert out and refused == ["pexels"]


class TestOtherFramesAreUnchanged:
    async def test_landscape_sends_the_same_single_request_and_cache_key(self, monkeypatch):
        calls = _http(monkeypatch, anywhere=BEES_WIDE)
        await S.search_videos("honey bees", orientation="landscape", per_page=15, api_key="k")
        assert calls == [{"query": "honey bees", "per_page": 15, "size": "small"}]
        assert await C.get("pexels", "honey bees", orientation="landscape", per_page=15, size="small")

    async def test_a_search_all_scenes_pool_stays_on_topic_end_to_end(self, monkeypatch):
        _http(monkeypatch, portrait=OFF_TALL, anywhere=BEES_WIDE)
        pools = await S.search_all_scenes([{"query": "honey bees"}], "portrait", 5.0, "k")
        assert {c["id"] for c in pools[0][1][:6]} == {v["id"] for v in BEES_WIDE}


def _hit(hid, tags):
    return {"id": hid, "pageURL": f"https://pixabay.com/videos/{hid}/", "duration": 9, "views": 5000, "likes": 40,
            "tags": tags, "user": "B",
            "videos": {"large": {"url": f"https://cdn.pixabay.com/{hid}.mp4", "width": 1920, "height": 1080,
                                 "thumbnail": "https://cdn.pixabay.com/t.jpg"}}}


OFF_HITS = [_hit(i, "woman, park, dancing") for i in range(601, 605)]


class TestPixabayFollowsTheOffTopicRule:
    async def test_pixabay_has_no_orientation_to_ask_for(self, monkeypatch):
        resp = MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value={"hits": []}))
        http = MagicMock(get=AsyncMock(return_value=resp))
        monkeypatch.setattr(S, "_http", http)
        await S.search_pixabay_videos("honey bees", orientation="portrait", per_page=15, api_key="k")
        assert http.get.await_count == 1 and "orientation" not in http.get.await_args.kwargs["params"]

    async def test_tall_target_demotes_an_off_topic_clip_only(self, monkeypatch):
        _http(monkeypatch, pixabay_hits=[*OFF_HITS, _hit(700, "honey bee, hive")])
        out = await S.search_pixabay_videos("bees", orientation="portrait", per_page=15, api_key="k")
        assert out[0]["id"] == "pixabay_700"
        assert out[0]["quality_score"] - out[1]["quality_score"] > 50

    async def test_a_cached_list_is_never_demoted_twice(self, monkeypatch):
        _http(monkeypatch, pixabay_hits=OFF_HITS)
        first = await S.search_pixabay_videos("bees", orientation="portrait", per_page=15, api_key="k")
        again = await S.search_pixabay_videos("bees", orientation="portrait", per_page=15, api_key="k")
        assert [c["quality_score"] for c in again] == [c["quality_score"] for c in first]

    async def test_wide_targets_are_unchanged(self, monkeypatch):
        _http(monkeypatch, pixabay_hits=[*OFF_HITS, _hit(700, "honey bee, hive")])
        out = await S.search_pixabay_videos("bees", orientation="landscape", per_page=15, api_key="k")
        assert len({c["quality_score"] for c in out}) == 1 and out[0]["id"] == "pixabay_601"

    async def test_off_topic_landscapes_no_longer_fill_the_window_ahead_of_a_vertical(self, monkeypatch):
        tall = [_video(i, "person-using-laptop", 1080, 1920) for i in range(401, 407)]
        _http(monkeypatch, portrait=tall, pixabay_hits=OFF_HITS)
        pools = await S.search_all_scenes([{"query": "satisfying"}], "portrait", 5.0, "k", "k")
        assert all(c["height"] > c["width"] for c in pools[0][1][:3])


class TestPreferNative:
    LAND = {"provider_id": "land", "width": 1920, "height": 1080}
    TALL = {"provider_id": "tall", "width": 1080, "height": 1920}

    def test_an_on_topic_vertical_still_moves_up(self):
        from backend.services.studio_planner import prefer_native_orientation
        assert prefer_native_orientation([self.LAND, self.TALL], True)[0] is self.TALL

    def test_an_off_topic_vertical_does_not_jump_a_clip_the_ranking_chose(self):
        from backend.services.studio_planner import prefer_native_orientation
        assert prefer_native_orientation([self.LAND, self.TALL], True, off_topic={"tall"})[0] is self.LAND

    def test_the_next_vertical_is_still_tried(self):
        from backend.services.studio_planner import prefer_native_orientation
        other = {"provider_id": "other", "width": 1080, "height": 1920}
        assert prefer_native_orientation([self.LAND, self.TALL, other], True, off_topic={"tall"})[0] is other

    def test_a_vertical_jumps_a_top_clip_that_is_just_as_off_topic(self):
        from backend.services.studio_planner import prefer_native_orientation
        assert prefer_native_orientation([self.LAND, self.TALL], True, off_topic={"land", "tall"})[0] is self.TALL


def _plan(monkeypatch, aspect, pool, refuse=False):
    """The real plan_project, with the search, the extractor and the rank stubbed."""
    from backend.services import clip_rank_service, studio_doc as sd, studio_planner

    async def extract(chunks, ai):
        return [{"query": "honey bees", "mood": None} for _ in chunks]

    async def search_all(*a, **kw):
        if refuse:
            C.note_refusal("pexels")
        return [(0, [dict(c) for c in pool])]

    async def rank(items, ai):
        return {it["scene_idx"]: it["candidates"] for it in items}
    monkeypatch.setattr(S, "extract_visual_scenes", extract)
    monkeypatch.setattr(S, "search_all_scenes", search_all)
    monkeypatch.setattr(clip_rank_service, "rank_scene_candidates", rank)
    doc = sd.validate_doc({"aspect_ratio": aspect, "settings": sd.default_settings(),
                           "scenes": [sd.make_scene("Bees talk by dancing.")]}, settle=False)
    return asyncio.run(studio_planner.plan_project(doc, project_id="p", ai_client=object(), pexels_key="k"))


def _clip(cid, words, w, h):
    return {"id": cid, "source": "pexels", "download_url": f"https://videos.pexels.com/{cid}.mp4", "width": w,
            "height": h, "duration": 9, "desc_words": words, "quality_score": 1}


def _bound(ops):
    return next(o for o in ops if o["op"] == "plan_result")["visual"]["asset"]["provider_id"]


@pytest.mark.parametrize("aspect", ["16:9", "1:1"])
def test_wide_frames_keep_the_top_3_rule(monkeypatch, aspect):
    pool = [_clip("1", ["honey", "bees"], 1080, 1920), _clip("2", ["woman", "park"], 1920, 1080)]
    assert _bound(_plan(monkeypatch, aspect, pool)[0]) == "2"


def test_tall_frame_keeps_an_off_topic_vertical_from_jumping_an_on_topic_clip(monkeypatch):
    pool = [_clip("1", ["honey", "bees"], 1920, 1080), _clip("2", ["woman", "park"], 1080, 1920)]
    assert _bound(_plan(monkeypatch, "9:16", pool)[0]) == "1"


def test_a_refused_search_tells_the_person_in_plain_words(monkeypatch):
    pool = [_clip("1", ["honey", "bees"], 1080, 1920)]
    _, warnings = _plan(monkeypatch, "9:16", pool, refuse=True)
    assert any("hourly limit" in w and "Re-plan" in w for w in warnings)
    _, warnings = _plan(monkeypatch, "9:16", pool)
    assert not any("hourly limit" in w for w in warnings)


class TestKenBurnsMargin:
    @pytest.mark.parametrize("src,target,static,expected", [
        ((1920, 1080), (1080, 1920), True, 1.0),
        ((1920, 1080), (1080, 1920), False, 1.03),
        ((3840, 2160), (1080, 1920), True, 1.0),
        ((1080, 1920), (1920, 1080), False, 1.03),
        ((1080, 1920), (1080, 1920), True, 1.12),
        ((1080, 1920), (1080, 1920), False, 1.10),
        ((720, 1280), (1080, 1920), False, 1.10),
        ((1920, 1080), (1920, 1080), True, 1.12),
        ((1920, 1080), (1080, 1080), True, 1.12),      # 1:1 keeps 56%: not heavy
        ((0, 0), (1080, 1920), True, 1.12),            # a failed probe: as before
    ])
    def test_the_margin(self, src, target, static, expected):
        assert S.kenburns_scale(*src, *target, static) == expected


def _vf_of_run(monkeypatch, src_dims, preset):
    """The filter trim_and_normalize_clip really hands ffmpeg, no ffmpeg run."""
    from pathlib import Path
    seen = {}

    def run(cmd, **kw):
        seen["vf"] = cmd[cmd.index("-vf") + 1]
        return MagicMock(returncode=0, stderr="")
    monkeypatch.setattr(S.subprocess, "run", run)
    monkeypatch.setattr(S, "probe_dimensions", lambda p: src_dims)
    monkeypatch.setattr(S.random, "choice", lambda seq: preset)
    asyncio.run(S.trim_and_normalize_clip(Path("/tmp/x.mp4"), 4.0, 1080, 1920, output_path=Path("/tmp/y.mp4")))
    return seen["vf"]


def test_the_render_filter_on_a_landscape_clip_has_no_stacked_zoom(monkeypatch):
    vf = _vf_of_run(monkeypatch, (1920, 1080), S._KENBURNS_PRESETS[0])
    assert vf.startswith("scale=1080:1920:") and "crop=1080:1920" in vf


def test_the_render_filter_on_a_portrait_clip_is_unchanged(monkeypatch):
    assert _vf_of_run(monkeypatch, (1080, 1920), S._KENBURNS_PRESETS[0]).startswith("scale=1209:2150:")
    assert _vf_of_run(monkeypatch, (1080, 1920), S._KENBURNS_PRESETS[1]).startswith("scale=1188:2112:")
