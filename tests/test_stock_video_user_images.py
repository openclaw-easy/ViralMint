# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The user's own images filling scenes of a stock video.

The seams that matter here are the ones that fail SILENTLY: a scene that got
searched on Pexels when an image already owned it (wasted quota, and the wrong
footage if the image later fails), and clips that reach the stitcher out of
the script's order.
"""
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import backend.services.pexels_service as px
from backend.agents.generator_video import resolve_still_input


def _has_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


needs_ffmpeg = pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")

SCRIPT = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo"


def _image(path, color="red"):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=600x600",
         "-frames:v", "1", str(path)],
        capture_output=True, timeout=30, check=True,
    )
    return path


class _Harness:
    """Stubs everything below build_stock_video except the still pipeline,
    which is the thing under test and runs for real."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.searched: list[int] = []
        self.stitched_with: list[Path] = []
        self.warnings: list[str] = []

    def __enter__(self):
        tmp = self.tmp_path

        async def fake_search(query, orientation="portrait", per_page=10):
            self.searched.append(query)
            return [{"id": abs(hash(query)) % 100000, "duration": 12,
                     "download_url": "http://x/c.mp4", "quality_score": 100}]

        async def fake_trim(src, dur, w, h, out, *a, **k):
            Path(out).write_bytes(b"stock")
            return Path(out)

        async def fake_stitch(clips, output, **k):
            self.stitched_with = list(clips)
            Path(output).write_bytes(b"stitched")
            return Path(output)

        async def fake_warn(constraint=None, message=None, severity=None, **k):
            self.warnings.append(constraint)

        self._patches = [
            patch("backend.services.stock_media.search_clips", side_effect=fake_search),
            patch.object(px, "download_clip", AsyncMock(return_value=tmp / "raw.mp4")),
            patch.object(px, "trim_and_normalize_clip", side_effect=fake_trim),
            patch.object(px, "probe_duration", return_value=4.0),
            patch("backend.services.ffmpeg_service.stitch_clips", side_effect=fake_stitch),
            patch.object(px.ws_manager, "send_constraint_warning", side_effect=fake_warn),
            patch("shutil.move"),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False

    @property
    def placed(self) -> list[str]:
        """The clips the SCENES produced, one per scene.

        Excludes `pexels_loop_*`: when the clips total less than the voice,
        build_stock_video re-trims existing ones onto the end to fill the gap.
        Those are extra copies of scenes already counted, and including them
        makes every per-scene assertion here wrong.
        """
        return [p.name for p in self.stitched_with
                if not p.name.startswith("pexels_loop_")]


# ── resolve_still_input ────────────────────────────────────────────────────

def test_a_media_url_resolves_under_the_upload_dir(tmp_path, monkeypatch):
    from backend.config import settings
    settings.TMP_DIR.mkdir(parents=True, exist_ok=True)
    f = settings.TMP_DIR / "abc123.png"
    f.write_bytes(b"x")
    try:
        assert resolve_still_input("/api/media/abc123.png") == f
    finally:
        f.unlink(missing_ok=True)


def test_a_media_url_cannot_traverse_out_of_the_upload_dir():
    """Basename-only resolution — the same guard /api/media/{filename} uses
    when it serves these files back."""
    assert resolve_still_input("/api/media/../../../../etc/passwd") is None


def test_a_local_path_that_exists_resolves(tmp_path):
    f = tmp_path / "photo.png"
    f.write_bytes(b"x")
    assert resolve_still_input(str(f)) == f


def test_a_missing_reference_is_none_not_an_exception():
    """One unusable image is one image fewer, not a failed render."""
    assert resolve_still_input("/nope/missing.png") is None
    assert resolve_still_input("") is None
    assert resolve_still_input("/api/media/never-uploaded.png") is None


# ── Placement ──────────────────────────────────────────────────────────────

@needs_ffmpeg
async def test_a_scene_an_image_owns_is_never_searched_on_pexels(tmp_path):
    imgs = [_image(tmp_path / "a.png"), _image(tmp_path / "b.png")]
    with _Harness(tmp_path) as h:
        await px.build_stock_video(
            SCRIPT, voice_path=None, aspect_ratio="9:16",
            ai_client=None, output_path=tmp_path / "final.mp4",
            user_images=imgs,
        )
    # Whatever the scene count is, two of them are owned — so exactly two
    # fewer Pexels searches than scenes were issued.
    assert len(h.placed) - len(h.searched) == 2, (
        f"{len(h.searched)} searches for {len(h.placed)} scenes"
    )


@needs_ffmpeg
async def test_the_user_images_land_on_the_opening_scenes_in_order(tmp_path):
    imgs = [_image(tmp_path / "a.png"), _image(tmp_path / "b.png")]
    with _Harness(tmp_path) as h:
        await px.build_stock_video(
            SCRIPT, voice_path=None, aspect_ratio="9:16",
            ai_client=None, output_path=tmp_path / "final.mp4",
            user_images=imgs,
        )
    names = h.placed
    assert names[0].startswith("user_scene_000")
    assert names[1].startswith("user_scene_001")
    assert not any(n.startswith("user_scene") for n in names[2:]), names


@needs_ffmpeg
async def test_stock_clips_keep_the_scripts_own_order_around_the_stills(tmp_path):
    """The stills and the stock clips are produced by two different routes;
    they have to interleave back onto the script's order, not append."""
    imgs = [_image(tmp_path / "a.png")]
    with _Harness(tmp_path) as h:
        await px.build_stock_video(
            SCRIPT, voice_path=None, aspect_ratio="9:16",
            ai_client=None, output_path=tmp_path / "final.mp4",
            user_images=imgs,
        )
    stock = [n for n in h.placed if n.startswith("pexels_")]
    indices = [int(n.split("_")[1].split(".")[0]) for n in stock]
    assert indices == sorted(indices), indices
    assert indices[0] == 1, "scene 0 belongs to the user image"


@needs_ffmpeg
async def test_every_scene_can_be_a_user_image(tmp_path):
    imgs = [_image(tmp_path / f"{i}.png") for i in range(4)]
    with _Harness(tmp_path) as h:
        await px.build_stock_video(
            "short script here", voice_path=None,
            aspect_ratio="9:16", ai_client=None,
            output_path=tmp_path / "final.mp4", user_images=imgs,
        )
    assert all(n.startswith("user_scene") for n in h.placed), h.placed
    assert h.searched == []


# ── Degradation ────────────────────────────────────────────────────────────

@needs_ffmpeg
async def test_an_unreadable_image_gives_its_scene_back_to_stock(tmp_path):
    """It has to lose the scene BEFORE the Pexels searches are issued, or the
    scene ends up with neither an image nor any footage."""
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    good = _image(tmp_path / "good.png")

    with _Harness(tmp_path) as h:
        await px.build_stock_video(
            SCRIPT, voice_path=None, aspect_ratio="9:16",
            ai_client=None, output_path=tmp_path / "final.mp4",
            user_images=[bad, good],
        )

    assert "user_image_unreadable" in h.warnings
    names = h.placed
    # Only the good one placed, and every other scene still has footage.
    assert sum(n.startswith("user_scene") for n in names) == 1
    assert len(names) - len(h.searched) == 1


@needs_ffmpeg
async def test_more_images_than_the_script_has_room_for_is_said_out_loud(tmp_path):
    """Silently dropping the user's last photos is the failure mode this
    warning exists to prevent."""
    imgs = [_image(tmp_path / f"{i}.png") for i in range(14)]
    with _Harness(tmp_path) as h:
        await px.build_stock_video(
            "tiny", voice_path=None, aspect_ratio="9:16",
            ai_client=None, output_path=tmp_path / "final.mp4",
            user_images=imgs,
        )
    assert "user_images_over_scene_count" in h.warnings
    assert len(h.placed) == px.MAX_SCENES, h.placed


@needs_ffmpeg
async def test_bringing_images_raises_the_scene_count_to_fit_them(tmp_path):
    """A 3-scene grid would have used 3 of 6 images and said nothing."""
    imgs = [_image(tmp_path / f"{i}.png") for i in range(6)]
    with _Harness(tmp_path) as h:
        await px.build_stock_video(
            "tiny", voice_path=None, aspect_ratio="9:16",
            ai_client=None, output_path=tmp_path / "final.mp4",
            user_images=imgs,
        )
    assert len(h.placed) == 6, h.placed
    assert all(n.startswith("user_scene") for n in h.placed), h.placed
    assert h.warnings == []


@needs_ffmpeg
async def test_the_normalized_stills_are_cleaned_up(tmp_path):
    from backend.config import settings
    settings.TMP_DIR.mkdir(parents=True, exist_ok=True)
    before = set(settings.TMP_DIR.glob("user_still_*"))

    imgs = [_image(tmp_path / "a.png")]
    with _Harness(tmp_path):
        await px.build_stock_video(
            SCRIPT, voice_path=None, aspect_ratio="9:16",
            ai_client=None, output_path=tmp_path / "final.mp4",
            user_images=imgs,
        )
    assert set(settings.TMP_DIR.glob("user_still_*")) == before


@needs_ffmpeg
async def test_no_images_behaves_exactly_as_before(tmp_path):
    with _Harness(tmp_path) as h:
        res = await px.build_stock_video(
            SCRIPT, voice_path=None, aspect_ratio="9:16",
            ai_client=None, output_path=tmp_path / "final.mp4",
        )
    assert res is not None
    assert len(h.searched) == len(h.placed)
    assert not any(n.startswith("user_scene") for n in h.placed)
    assert h.warnings == []


# ── The two image asks are different asks ──────────────────────────────────

class TestStartImageVersusUserImages:
    """`start_image` says "make the WHOLE video out of this one picture".
    `user_images` says "put these into the scenes of a normal video". Both
    can arrive on one request and only one can win."""

    async def test_per_scene_images_take_precedence_over_a_start_image(self):
        from backend.agents.generator import GeneratorAgent

        with patch("backend.agents.generator.generate_kenburns_video",
                   AsyncMock()) as kb, \
             patch("backend.agents.generator.generate_stock_video",
                   AsyncMock(return_value=Path("/out.mp4"))) as stock:
            out = await GeneratorAgent()._generate_video(
                "script", None, "9:16", None,
                start_image="/api/media/a.png", user_images=["/api/media/b.png"],
            )

        assert out == Path("/out.mp4")
        kb.assert_not_called(), "the whole-video route would have discarded the scenes"
        assert stock.call_args.kwargs["user_images"] == ["/api/media/b.png"]

    async def test_a_start_image_alone_still_takes_the_whole_video_route(self):
        from backend.agents.generator import GeneratorAgent

        with patch("backend.agents.generator.generate_kenburns_video",
                   AsyncMock(return_value=Path("/kb.mp4"))) as kb, \
             patch("backend.agents.generator.generate_stock_video",
                   AsyncMock()) as stock:
            out = await GeneratorAgent()._generate_video(
                "script", None, "9:16", None, start_image="/api/media/a.png",
            )

        assert out == Path("/kb.mp4")
        kb.assert_awaited_once()
        stock.assert_not_called()


class TestNoPexelsKey:
    async def test_images_alone_can_still_build_a_video_without_a_pexels_key(self, tmp_path):
        """Every scene the user brought an image for needs no stock footage,
        so a missing key is not a reason to refuse."""
        import backend.agents.generator_video as gv

        img = _image(tmp_path / "a.png")
        with patch.object(gv.settings, "PEXELS_API_KEY", ""), \
             patch("backend.services.pexels_service.build_stock_video",
                   AsyncMock(return_value=Path("/out.mp4"))) as build:
            out = await gv.generate_stock_video(
                "script", None, "9:16", None, user_images=[str(img)])

        assert out == Path("/out.mp4")
        assert build.call_args.kwargs["user_images"] == [img]

    async def test_no_key_and_no_images_still_declines(self):
        import backend.agents.generator_video as gv

        with patch.object(gv.settings, "PEXELS_API_KEY", ""):
            assert await gv.generate_stock_video("script", None, "9:16", None) is None

    async def test_references_that_do_not_resolve_are_dropped_not_fatal(self, tmp_path):
        import backend.agents.generator_video as gv

        img = _image(tmp_path / "a.png")
        with patch.object(gv.settings, "PEXELS_API_KEY", "k"), \
             patch("backend.services.pexels_service.build_stock_video",
                   AsyncMock(return_value=Path("/out.mp4"))) as build:
            await gv.generate_stock_video(
                "script", None, "9:16", None,
                user_images=["/nope/gone.png", str(img)])

        assert build.call_args.kwargs["user_images"] == [img]


class TestNothingGoesMissingQuietly:
    """The whole feature is "your own photos are in the video". Every path
    that drops one has to say so."""

    async def test_a_reference_that_went_stale_is_reported(self, tmp_path):
        import backend.agents.generator_video as gv

        warned = []

        async def fake_warn(constraint=None, message=None, severity=None, **k):
            warned.append((constraint, message))

        img = _image(tmp_path / "a.png")
        with patch.object(gv.settings, "PEXELS_API_KEY", "k"), \
             patch.object(gv.ws_manager, "send_constraint_warning", side_effect=fake_warn), \
             patch("backend.services.pexels_service.build_stock_video",
                   AsyncMock(return_value=Path("/out.mp4"))):
            await gv.generate_stock_video(
                "script", None, "9:16", None,
                user_images=["/api/media/swept-away.png", str(img)])

        assert [c for c, _m in warned] == ["user_image_missing"]
        assert "swept-away.png" in warned[0][1]

    async def test_a_superseded_start_image_is_still_a_fallback(self):
        """Per-scene images take precedence, but if every one of them turns
        out to be unusable the user offered two pictures and must not end up
        seeing neither."""
        from backend.agents.generator import GeneratorAgent

        with patch("backend.agents.generator.generate_stock_video",
                   AsyncMock(return_value=None)), \
             patch("backend.agents.generator.generate_kenburns_video",
                   AsyncMock(return_value=Path("/kb.mp4"))) as kb:
            out = await GeneratorAgent()._generate_video(
                "script", None, "9:16", None,
                start_image="/api/media/a.png", user_images=["/api/media/gone.png"],
            )

        assert out == Path("/kb.mp4")
        kb.assert_awaited_once()

    async def test_the_text_fallback_still_wins_when_there_is_no_image_at_all(self):
        from backend.agents.generator import GeneratorAgent

        with patch("backend.agents.generator.generate_stock_video",
                   AsyncMock(return_value=None)), \
             patch("backend.services.ffmpeg_service.generate_text_video",
                   AsyncMock(return_value=Path("/text.mp4"))):
            out = await GeneratorAgent()._generate_video("script", None, "9:16", None)

        assert out == Path("/text.mp4")
