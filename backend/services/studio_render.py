# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Smart Video Studio — render v2.

Renders EXACTLY what is in the project. Nothing is re-planned, re-searched or
re-voiced except where the project itself says "at render":

  1. Refuse on the render checks' blockers (studio_checks).
  2. Takes: every scene's take from the cache; a scene not voiced yet (or
     whose line changed) is recorded now.
  3. Visuals, bounded parallel: your clips as they are; a bound stock clip is
     downloaded from the stock hosts — a dead link is re-searched with a
     warning, never silently; a placeholder searches its query.
  4. Each scene is normalised to EXACTLY its take + the pause after it; a
     clip shorter than that follows the scene's fit (loop / freeze / slow);
     stills get Ken Burns motion.
  5. Narration = takes joined with the pauses; captions from each take's word
     timings shifted to the scene start; stitch with the project transition;
     the music bed cut from its offset, looped to length and mixed under the
     voice; A/V merge; captions burned; finishing steps.
  6. The output is validated (playable, has audio, the planned length) before
     anything is saved — a broken file is a failed render, never a "video".
  7. A GeneratedVideo row with `project_id`; the project becomes `rendered`.

Cancellation is honoured between steps. One failing scene cancels the others;
a take already being recorded finishes and is cached, so a re-render re-uses it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from backend.config import settings
from backend.core.exceptions import JobCancelledError, StudioError, VoiceGenerationError

logger = logging.getLogger(__name__)

DURATION_TOLERANCE_S = 0.75


class RenderCancelled(StudioError):
    pass


@dataclass
class RenderDone:
    """What a finished render hands its job runner: the job's stored output,
    the job_complete payload, and what to run once the job is marked done."""
    result: dict
    output: dict
    after_complete: Callable[[], None] = field(default=lambda: None)


# ── concurrency ─────────────────────────────────────────────────────────────

# Scene encodes across ALL renders share this many slots. Without a bound an
# 18-scene render started 18 libx264 encodes at once (3 renders → 54), filled
# the default thread pool and stalled every other asyncio.to_thread caller.
ENCODE_SLOTS = max(2, (os.cpu_count() or 4) // 2)
_encode_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def encode_slots() -> asyncio.Semaphore:
    """The process-wide encode semaphore for the running loop (a Semaphore is
    bound to the loop it first waits on; tests run many loops)."""
    loop = asyncio.get_running_loop()
    sem = _encode_slots.get(loop)
    if sem is None:
        sem = _encode_slots[loop] = asyncio.Semaphore(ENCODE_SLOTS)
    return sem


class _Clock:
    """Where a render's wall time goes: one log line at the end, step by step,
    plus per-scene totals (they overlap, so they sum past the visuals step).
    Render speed is tuned from these, not from guesses."""

    def __init__(self) -> None:
        self._start = self._last = time.monotonic()
        self.steps: list[tuple[str, float]] = []
        self.sums: dict[str, float] = {}

    def mark(self, name: str) -> None:
        now = time.monotonic()
        self.steps.append((name, now - self._last))
        self._last = now

    def add(self, name: str, seconds: float) -> None:
        self.sums[name] = self.sums.get(name, 0.0) + seconds

    def line(self) -> str:
        steps = " ".join(f"{n}={t:.1f}" for n, t in self.steps)
        sums = " ".join(f"{n}={t:.1f}" for n, t in sorted(self.sums.items()))
        return f"total={time.monotonic() - self._start:.1f}s | {steps} | scene sums: {sums}"


# Voice takes still recording after their render was cancelled (kept alive:
# a take is cached when it finishes, so the next render re-uses it).
_inflight_takes: set[asyncio.Future] = set()


# ── ffmpeg helpers (argv lists, never a shell) ─────────────────────────────

def _encode() -> list[str]:
    """Scene-clip encode args (an intermediate: the caption burn or the final
    encode re-encodes it)."""
    from backend.services.video_utils import intermediate_x264
    return ["-an", "-c:v", "libx264", *intermediate_x264("20", "veryfast"), "-pix_fmt", "yuv420p", "-r", "30"]


OUTRO_HOLD_S = 0.85
FALLBACK_TITLE = "Untitled Video"        # GeneratorAgent._generate_metadata's placeholder when no AI title came back


def _ff(args: list[str], timeout: int = 600) -> None:
    from backend.services.video_utils import run_ffmpeg
    proc = run_ffmpeg(args, timeout=timeout)
    if proc.returncode != 0:
        # The full tool output is for the log; the job's message is read by the
        # user and must not carry file paths or ffmpeg internals.
        logger.warning("studio render: ffmpeg failed (%s): %s", args[:2], (proc.stderr or "").strip()[-2000:])
        raise StudioError("A video processing step failed — try rendering again.")


def voice_failed(number: int, e: VoiceGenerationError) -> StudioError:
    """Which scene's voice failed, and why. The voice services word their
    reasons for the user (the editor already shows the same text on the scene
    after Voice all). The generic "try rendering again" sent people into a
    retry that fails the same way — a take that can't be recorded is a
    property of that line in that voice, so the message names both."""
    reason = " ".join(str(e).split())[:220] or "The voice couldn't be recorded."
    return StudioError(f"Scene {number}'s voice failed: {reason} "
                       "Scenes already voiced are kept, so the next render only records what's left.")


def fit_clip(src: Path, target: float, fit: str, out: Path) -> Path:
    """Exactly `target` seconds of video from a normalised clip.

    loop   — repeat the clip · freeze — hold the last frame ·
    slow   — play at ≥ 0.75× speed, then hold the last frame for any remainder.
    """
    from backend.services.video_utils import probe_duration

    have = probe_duration(src, 0.0)
    if have <= 0:
        raise StudioError("A scene's clip has no duration.")
    t = f"{target:.3f}"
    if have >= target - 0.04:
        _ff(["-i", str(src), "-t", t, *_encode(), str(out)])
    elif fit == "freeze":
        _ff(["-i", str(src), "-vf", f"tpad=stop_mode=clone:stop_duration={target - have + 0.1:.3f}", "-t", t, *_encode(), str(out)])
    elif fit == "slow":
        rate = max(0.75, have / target)
        remainder = max(0.0, target - have / rate)
        # `fps=30` between the retime and the pad is load-bearing on ffmpeg
        # 7.1: after setpts, 7.1's tpad adds NOTHING, so a slowed clip came out
        # 2.67s for a 3.5s slot and the scene fell out of sync with its voice
        # (8.x pads fine). Normalising timestamps first makes both agree; 30
        # is the render rate _encode() writes anyway.
        vf = f"setpts=PTS/{rate:.4f}" + (
            f",fps=30,tpad=stop_mode=clone:stop_duration={remainder + 0.1:.3f}" if remainder > 0.01 else "")
        _ff(["-i", str(src), "-vf", vf, "-t", t, *_encode(), str(out)])
    else:
        _ff(["-stream_loop", "-1", "-i", str(src), "-t", t, *_encode(), str(out)])
    return out


def prepare_music(track: Path, offset: float, total: float, out: Path) -> Path:
    """The bed from `offset`, looped to the video's length."""
    _ff(["-stream_loop", "-1", "-ss", f"{max(0.0, offset):.3f}", "-i", str(track), "-t", f"{total:.3f}",
         "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(out)])
    return out


async def frame_cover(clean: Path, title: str | None, style: str, aspect: str, at: float, out: Path) -> Path | None:
    """A frame thumbnail taken from the video BEFORE captions were burned, with
    only the title on it — set in the caption style's own face, so it reads
    like the video's opening. A frame of the finished video would carry a
    half-sentence caption ("Here are three amazing tips to"). None on any
    failure; the caller falls back."""
    from backend.services import studio_doc as sd
    from backend.services.caption_service import ass_filter, generate_captions_ass
    tmp = out.with_name(f"{out.stem}.{uuid.uuid4().hex[:8]}.tmp.jpg")
    ass = out.with_name(f"{out.stem}.{uuid.uuid4().hex[:8]}.ass")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        vf = "null"
        text = sd.clip_title(sd.normalise_line(title)) if title else ""
        if text:
            await generate_captions_ass([], style=style, aspect_ratio=aspect, output_path=ass,
                                        hook_text=text, hook_duration=30.0, emoji_style="none")
            vf = ass_filter(ass)
        # -copyts: an input seek (-ss before -i) restarts timestamps at 0, so
        # the title's \fad fade-in was drawn at its first frame — a cover with
        # a ghost of a title. Keeping the source timestamps draws it at `at`.
        await asyncio.to_thread(_ff, ["-copyts", "-ss", f"{max(0.0, at):.2f}", "-i", str(clean), "-frames:v", "1",
                                      "-vf", vf, "-q:v", "2", str(tmp)], 120)
        if not tmp.is_file() or tmp.stat().st_size < 1024:
            return None
        await asyncio.to_thread(os.replace, tmp, out)
        return out
    except Exception:  # noqa: BLE001 — a cover is a convenience
        logger.warning("studio render: clean frame cover failed", exc_info=True)
        return None
    finally:
        tmp.unlink(missing_ok=True)
        ass.unlink(missing_ok=True)


# ── the render ──────────────────────────────────────────────────────────────

async def render(job_id: str, project_id: str, user_id: str = "local") -> RenderDone:
    from backend.agents.job_helper import job_cancelled, update_job_status
    from backend.core.ws_manager import ws_manager
    from backend.services import render_post, studio_media
    from backend.services import studio_doc as sd
    from backend.services import studio_store as store
    from backend.services import voice_take_service as vts
    from backend.services.audio_utils import concat_audio
    from backend.services.caption_service import burn_captions, generate_captions_ass
    from backend.services.ffmpeg_service import (
        ASPECT_DIMS, add_audio_to_video, generate_single_kenburns_clip, stitch_with_style,
    )
    from backend.services.music_service import mix_audio
    from backend.services.output_validator import validate_output
    from backend.services.stock_video_service import (
        _color_grade_filters, resolve_visual_style, trim_and_normalize_clip,
    )
    from backend.services.studio_checks import checks_for

    async def progress(pct: float, step: str) -> None:
        await update_job_status(job_id, "running", pct, step)

    async def gate() -> None:
        if await job_cancelled(job_id):
            raise RenderCancelled("Render cancelled.")

    warnings: list[tuple[str, str]] = []

    async def warn(code: str, message: str) -> None:
        warnings.append((code, message))
        await ws_manager.send_constraint_warning(code, message, user_id=user_id)

    scratch = settings.TMP_DIR / f"studio_render_{job_id[:8]}"
    scratch.mkdir(parents=True, exist_ok=True)
    details: dict[str, asyncio.Task] = {}
    clock = _Clock()
    scene_count = 0
    try:
        await progress(2, "Checking the project")
        row = await store.get_project(project_id, user_id)
        doc = json.loads(row.doc_json)
        assets = store.assets_of(row)
        blockers = checks_for(doc)["blockers"]
        if blockers:
            raise StudioError(blockers[0])
        s = doc["settings"]
        # Finishing steps from the user's Settings, with this project's own
        # choices on top.
        user_settings = await render_post.load_user_settings(user_id)
        post = render_post.options_from_settings(user_settings, caption_style=s["captions"]["style"],
                                                 overrides=s.get("post"))
        needs_timings = s["captions"]["enabled"] or post.needs_word_timings
        aspect = doc["aspect_ratio"]
        width, height = ASPECT_DIMS.get(aspect, (1080, 1920))
        lut = resolve_visual_style(s["visual_style"])["lut"]
        gap = float(s["tail_gap_s"])
        scenes = doc["scenes"]
        scene_count = len(scenes)
        landing: list[dict] = []
        await gate()

        clock.mark("setup")
        # ── 2. takes ──
        await progress(8, "Voices")
        spec = vts.VoiceSpec.from_settings(s)
        identity = list(sd.voice_identity(s))
        takes: dict[str, vts.Take] = {}
        take_slots = asyncio.Semaphore(vts.VOICE_CONCURRENCY)

        async def resolve_take(number: int, scene: dict) -> None:
            # A ready scene renders with the take it already has — the one the
            # user listened to.
            take = vts.recorded_take(scene["voice"].get("take_key")) if scene["voice"]["state"] == "ready" else None
            if take is None:
                async with take_slots:
                    # Shielded: when a sibling take fails the TaskGroup cancels
                    # this one mid-request, but an OpenAI take has already
                    # spent the user's key — cancelled, it would be spent,
                    # never cached, and spent AGAIN on the retry. Finished,
                    # ensure_take caches it for reuse.
                    synth = asyncio.ensure_future(vts.ensure_take(spec, scene["line"]))
                    _inflight_takes.add(synth)               # a shielded task needs a strong reference
                    synth.add_done_callback(_inflight_takes.discard)
                    try:
                        await progress(8, f"Recording voice — scene {number} of {len(scenes)}")
                        take = await asyncio.shield(synth)
                    except JobCancelledError:
                        raise RenderCancelled("Render cancelled.") from None
                    except VoiceGenerationError as e:
                        raise voice_failed(number, e) from e
            takes[scene["uid"]] = take

        await _run_all([resolve_take(n, scene) for n, scene in enumerate(scenes, 1)])
        for scene in scenes:                       # landed in scene order, as before
            take = takes[scene["uid"]]
            if scene["voice"].get("take_key") != take.key or scene["voice"]["state"] != "ready":
                landing.append({"op": "voice_result", "uid": scene["uid"], "expect": {"line": scene["line"], "voice": identity},
                                "voice": {"state": "ready", "take_key": take.key, "duration": take.duration,
                                          "cached": take.cached, "error": None}})
        # Word timings (Whisper, one decode at a time app-wide) start NOW and run
        # alongside the visuals instead of one by one after them.
        if needs_timings:
            for scene in scenes:
                key = takes[scene["uid"]].key
                if key not in details:
                    details[key] = asyncio.create_task(vts.take_details(key))
        durations = [takes[sc["uid"]].duration + gap for sc in scenes]
        # The last shot holds a beat after the last word, under the music's
        # fade — otherwise a video cuts 0.2–0.5 s after the final word,
        # mid-motion. Render-only: the editor's timing doesn't include it.
        shots = sd.second_shots(scenes, durations)     # the preview's cut points (before the hold)
        durations[-1] += OUTRO_HOLD_S
        starts = [sum(durations[:i]) for i in range(len(scenes))]
        total = sum(durations)
        await gate()

        clock.mark("voices")
        # ── 3. visuals ──
        await progress(20, "Gathering visuals")
        # Cheap blockers first: a missing clip fails the render BEFORE any
        # scene starts downloading or encoding.
        for i, scene in enumerate(scenes):
            visual = scene["visual"]
            if visual["kind"] == "user_clip":
                record = store.find_asset_record(assets, (visual.get("asset") or {}).get("asset_id"))
                if record is None or studio_media.asset_file(project_id, record) is None:
                    raise StudioError(f"Scene {i + 1}'s clip is missing from the project — add it again.")
        download_slots = asyncio.Semaphore(5)
        provenance: dict[int, dict] = {}

        async def stock_file(i: int, query: str | None, bound: dict | None) -> tuple[Path, str]:
            dest = scratch / f"stock_{i:02d}.mp4"
            tried: set[str] = set()
            if bound and bound.get("download_url"):
                try:
                    async with download_slots:
                        await studio_media.download(bound["download_url"], dest)
                    provenance[i] = {"scene": i, "source": bound.get("provider"), "id": bound.get("provider_id"),
                                     "creator": bound.get("creator"), "license": bound.get("license"),
                                     "fetched_at": time.time()}
                    return dest, "video"
                except Exception as e:  # noqa: BLE001 — replaced below, with a warning
                    logger.warning("studio render: bound clip for scene %d failed: %s", i + 1, e)
                    tried.add(str(bound.get("provider_id")))
                    await warn("stock_clip_replaced",
                               f"Scene {i + 1}'s clip is no longer available — it was replaced with a similar one.")
            try:
                found = await studio_media.search_candidates(project_id, query or sd.FALLBACK_QUERY, aspect)
            except StudioError as e:              # no stock key — say so instead of a generic failure
                raise StudioError(f"Scene {i + 1} has no footage yet: {e}") from e
            for candidate in found:
                if candidate["provider_id"] in tried:
                    continue
                try:
                    async with download_slots:
                        await studio_media.download(candidate["download_url"], dest)
                except Exception:  # noqa: BLE001 — try the next candidate
                    continue
                provenance[i] = {"scene": i, "source": candidate["provider"], "id": candidate["provider_id"],
                                 "creator": candidate.get("creator"), "license": candidate.get("license"),
                                 "fetched_at": time.time()}
                return dest, "video"
            raise StudioError(f"Scene {i + 1} has no usable footage — replace its visual and render again.")

        async def visual_file(i: int, scene: dict) -> tuple[Path, str]:
            visual = scene["visual"]
            kind = visual["kind"]
            if kind == "user_clip":
                record = store.find_asset_record(assets, (visual.get("asset") or {}).get("asset_id"))
                path = studio_media.asset_file(project_id, record) if record else None
                if path is None:
                    raise StudioError(f"Scene {i + 1}'s clip is missing from the project — add it again.")
                provenance[i] = {"scene": i, "source": "user", "id": record["asset_id"]}
                return path, record.get("media") or "video"
            return await stock_file(i, visual.get("query"), visual.get("asset"))

        async def scene_clip(i: int, scene: dict) -> Path:
            began = time.monotonic()
            source, media = await visual_file(i, scene)
            clock.add("fetch", time.monotonic() - began)
            out = scratch / f"scene_{i:02d}.mp4"
            queued = time.monotonic()
            async with encode_slots():
                clock.add("encode_wait", time.monotonic() - queued)
                encoding = time.monotonic()
                if media == "image":
                    await generate_single_kenburns_clip(source, durations[i], out, target_w=width, target_h=height,
                                                        grade_filters=_color_grade_filters(lut))
                else:
                    shot = shots.get(scene["uid"])
                    second = await _second_source(i, shot) if shot else None
                    first_len = shot["cut"] if second else durations[i]
                    norm_path = scratch / f"norm_{i:02d}.mp4"
                    normalised = Path(await trim_and_normalize_clip(source, first_len, width, height,
                                                                    output_path=norm_path, lut_preset=lut))
                    fit = scene["visual"].get("fit") or "loop"
                    if second is None:
                        await asyncio.to_thread(_fit_or_keep, normalised, norm_path, durations[i], fit, out)
                    else:
                        a = scratch / f"scene_{i:02d}_a.mp4"
                        b_norm = scratch / f"norm_{i:02d}_b.mp4"
                        b = scratch / f"scene_{i:02d}_b.mp4"
                        rest = durations[i] - first_len
                        await asyncio.to_thread(_fit_or_keep, normalised, norm_path, first_len, fit, a)
                        b_normalised = Path(await trim_and_normalize_clip(second, rest, width, height,
                                                                          output_path=b_norm, lut_preset=lut))
                        await asyncio.to_thread(_fit_or_keep, b_normalised, b_norm, rest, fit, b)
                        await asyncio.to_thread(_join_shots, a, b, out)
                clock.add("encode", time.monotonic() - encoding)
            return out

        async def _second_source(i: int, shot: dict) -> Path | None:
            """The alternate's file, or None (the scene keeps one shot — a
            missing second clip is never worth failing a render)."""
            dest = scratch / f"stock_{i:02d}_b.mp4"
            try:
                async with download_slots:
                    await studio_media.download(shot["asset"]["download_url"], dest)
                return dest
            except Exception as e:  # noqa: BLE001
                logger.info("studio render: second shot for scene %d skipped: %s", i + 1, e)
                return None

        clips = await _run_all([scene_clip(i, sc) for i, sc in enumerate(scenes)])
        clock.mark("visuals")
        if landing:
            try:
                await store.write_ops(project_id, landing, if_revision=None, internal=True, user_id=user_id)
            except Exception:  # noqa: BLE001 — showing results in the editor must not fail the render
                logger.warning("studio render: could not land take/visual results", exc_info=True)
        await gate()

        # ── 5. assembly ──
        await progress(55, "Assembling")
        items: list = []
        for n, scene in enumerate(scenes):
            take = takes[scene["uid"]]
            items.append(take.path)
            pause = gap + (OUTRO_HOLD_S if n == len(scenes) - 1 else 0.0)
            if pause > 0:
                items.append((take.path, pause))
        narration = await concat_audio(items, scratch / "narration.mp3")
        clock.mark("narration")

        captions_file = None
        caption_status = "skipped"
        segments: list[dict] = []
        if needs_timings:
            for i, scene in enumerate(scenes):
                take = takes[scene["uid"]]
                task = details.get(take.key)
                meta = (await task if task is not None else await vts.take_details(take.key)) or {}
                # `take` was resolved from this scene's line in this voice, so its
                # timings are the scene's. (Comparing meta.text to the raw line
                # dropped every scene a pronunciation rule touched.)
                words = meta.get("words")
                segment = {"start": starts[i], "end": starts[i] + take.duration, "text": scene["line"]}
                if words:
                    segment["words"] = [{"word": w["word"], "start": starts[i] + w["start"], "end": starts[i] + w["end"]}
                                        for w in words]
                segments.append(segment)
        if s["captions"]["enabled"]:
            hook_duration = min(sd.HOOK_SECONDS, total)
            caption_segments = after_hook(segments, hook_duration) if sd.hook_is_auto(doc) else segments
            captions_file = await generate_captions_ass(caption_segments, style=s["captions"]["style"], aspect_ratio=aspect,
                                                        output_path=scratch / "captions.ass",
                                                        emoji_style=post.emoji_style,
                                                        hook_text=sd.hook_title(doc),
                                                        hook_duration=hook_duration)

        clock.mark("timings")
        stitched = await stitch_with_style(list(clips), scratch / "stitched.mp4", s["transition"])
        clock.mark("stitch")
        await progress(72, "Mixing audio")
        audio = narration
        music = s["music"]
        if music["enabled"]:
            track = await _music_track(project_id, doc, music, user_id, warn)
            if track is not None:
                bed = await asyncio.to_thread(prepare_music, track, float(music["offset_s"]), total, scratch / "music.mp3")
                # The editor's preview fades the bed in and out over fade_s — the
                # render does exactly the same, so what you heard is what you get.
                fade = float(music["fade_s"])
                audio = await mix_audio(narration, bed, scratch / "mixed.mp3", music_volume_db=float(music["volume_db"]),
                                        fade_in_s=fade, fade_out_s=max(fade, OUTRO_HOLD_S))
        audio = await render_post.add_sfx(audio, segments, post, warn)
        # The whole mix to the short-form delivery level (-14 LUFS) in one
        # linear gain — the voice alone was -16 and the music bed only
        # lowered it, so renders shipped at -16…-17 and sounded quiet next to
        # everything else in the feed.
        audio = await render_post.deliver_loudness(audio, scratch / "delivered.mp3", warn)
        final = Path(await add_audio_to_video(stitched, audio, scratch / "merged.mp4"))
        merged = final
        clock.mark("audio")
        if captions_file is not None:
            burned = Path(await burn_captions(final, captions_file, scratch / "captioned.mp4"))
            if burned == final:
                await warn("captions_failed", "Captions couldn't be burned in — the video was rendered without them.")
                caption_status = "failed"
            else:
                final = burned
                caption_status = "applied"
        from backend.services.video_utils import intermediates_are_fast
        if final == merged and intermediates_are_fast():
            # Nothing re-encoded the fast intermediates (captions off, or the
            # burn fell back): give the deliverable its one real encode.
            final = await asyncio.to_thread(_final_encode, final, scratch / "final.mp4")
        clock.mark("captions")
        # Auto-zoom — before validation, so the checked file is the saved one.
        final = await render_post.finish_video(final, segments, post, warn)
        clock.mark("finish")
        await gate()

        # ── 6. validate ──
        await progress(86, "Checking the finished video")
        check = await validate_output(final, expect={"min_duration": max(0.5, total - DURATION_TOLERANCE_S), "audio": True})
        if not check.ok:
            raise StudioError(f"The rendered video didn't pass its checks: {check.message()}")
        if check.duration and abs(check.duration - total) > DURATION_TOLERANCE_S:
            raise StudioError(f"The rendered video is {check.duration:.1f}s, the project is {total:.1f}s — "
                              "it wasn't saved. Try rendering again.")
        await gate()
        clock.mark("check")

        # ── 7. persist ──
        await progress(92, "Saving")
        return await _persist(project_id, row, doc, spec, total, provenance, final, narration, warnings, user_id,
                              user_settings=user_settings, caption_status=caption_status, clean=stitched,
                              cover_at=min(max(0.8, durations[0] * 0.5), max(0.0, total - 0.2)))
    finally:
        clock.mark("persist")
        logger.info("studio render timing %s (%d scenes): %s", job_id, scene_count, clock.line())
        for task in details.values():
            if not task.done():
                task.cancel()           # a take's timings are cached by whoever finishes them later
        shutil.rmtree(scratch, ignore_errors=True)


def after_hook(segments: list[dict], hook_s: float) -> list[dict]:
    """Caption segments without the words spoken while an auto title (the
    first line's own words) is on screen — the same words twice, at the top
    and the bottom, would be the first thing every video shows.
    The preview hides captions for the same HOOK_SECONDS (SequencePlayer)."""
    from backend.services import studio_doc as sd
    out: list[dict] = []
    for seg in segments:
        words = seg.get("words")
        if words:
            kept = [w for w in words if w["start"] >= hook_s]
            if kept:
                text = ""
                for w in kept:
                    text = sd.join_sentences(text, str(w["word"]).strip())
                out.append({**seg, "start": kept[0]["start"], "words": kept, "text": text})
        elif seg["start"] >= hook_s:
            out.append(seg)
    return out


async def _run_all(coros: list) -> list:
    """Run coroutines together, in order; the FIRST failure cancels the rest
    (asyncio.gather would let them keep downloading and encoding after the
    render had already failed). The original exception is raised, not an
    ExceptionGroup."""
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(c) for c in coros]
    except BaseExceptionGroup as eg:
        first = next((e for e in eg.exceptions if not isinstance(e, asyncio.CancelledError)), eg.exceptions[0])
        raise first from None
    return [t.result() for t in tasks]


def _final_encode(src: Path, out: Path) -> Path:
    """The deliverable's encode at the render preset, audio copied as is."""
    from backend.services.video_utils import encode_preset
    _ff(["-i", str(src), "-map", "0", "-c:v", "libx264", "-preset", encode_preset(), "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out)])
    return out


def _join_shots(a: Path, b: Path, out: Path) -> Path:
    """Two normalised shots of one scene, back to back (same size and rate)."""
    from backend.services.video_utils import intermediate_x264
    _ff(["-i", str(a), "-i", str(b), "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
         "-c:v", "libx264", *intermediate_x264("18", "veryfast"), "-pix_fmt", "yuv420p", "-an", str(out)])
    return out


def _fit_or_keep(normalised: Path, norm_path: Path, target: float, fit: str, out: Path) -> Path:
    """The normalise pass already encodes EXACTLY `target` seconds at the
    render's format when the source is long enough (the usual stock case) —
    re-encoding it again only cost CPU and quality. Keep it then; otherwise
    (a short clip, or the normaliser fell back to returning its input) fit it."""
    from backend.services.video_utils import probe_duration
    if normalised == norm_path and normalised.is_file() and probe_duration(normalised, 0.0) >= target - 0.04:
        os.replace(normalised, out)
        return out
    return fit_clip(normalised, target, fit, out)


async def _music_track(project_id, doc, music, user_id, warn) -> Path | None:
    from backend.services import studio_music
    from backend.services import studio_store as store

    if not music.get("track"):
        # A project planned before tracks were picked at plan time, or music
        # switched on later: pick one now (and keep it on the project) rather
        # than silently rendering without the music the user left on.
        music_op = await studio_music.default_music_op(doc)
        if music_op is None:
            await warn("music_missing", "Music is on but your music library is empty — the video was rendered without music.")
            return None
        await store.write_ops(project_id, [music_op], if_revision=None, internal=True, user_id=user_id)
        await warn("music_auto_picked", f"No music track was picked, so “{music_op['track_name']}” from your library was used.")
        music = {**music, "track": music_op["track"]}
    try:
        from backend.services.music_service import library_path
        path = library_path(music["track"])
        if path.is_file():
            return path
    except Exception:  # noqa: BLE001 — a bad or missing track name
        pass
    await warn("music_missing", "The music track isn't in your library anymore — the video was rendered without music.")
    return None


async def source_lineage(source_id: str | None) -> dict:
    """The GeneratedVideo lineage columns for the source a project was made
    from: the DownloadedVideo id, the scout result that found it, and a SHORT
    niche (the source's own, else the scout query's — never a sentence). All
    None when the project has no source or the row is gone; a lookup failure
    never fails a render."""
    empty = {"source_downloaded_video_id": None, "source_scout_result_id": None, "niche": None}
    if not isinstance(source_id, str) or not source_id.strip():
        return empty
    try:
        from sqlalchemy import select

        from backend.database import AsyncSessionLocal
        from backend.models.downloaded_video import DownloadedVideo
        from backend.models.scout_result import ScoutResult
        async with AsyncSessionLocal() as db:
            src = (await db.execute(select(DownloadedVideo).where(DownloadedVideo.id == source_id))).scalar_one_or_none()
            if src is None:
                return empty
            niche = (getattr(src, "niche", None) or "").strip() or None
            scout_id = getattr(src, "scout_result_id", None) or None
            if not niche and scout_id:
                niche = ((await db.execute(select(ScoutResult.niche).where(ScoutResult.id == scout_id)))
                         .scalar_one_or_none() or "").strip() or None
        return {"source_downloaded_video_id": src.id, "source_scout_result_id": scout_id, "niche": niche}
    except Exception:  # noqa: BLE001 — lineage is metadata; the video is already rendered
        logger.warning("studio render: could not resolve the source lineage for %s", source_id[:8], exc_info=True)
        return empty


async def _metadata(script: str, niche: str, user_settings) -> dict:
    """YouTube + TikTok titles/descriptions from the user's AI key — the same
    writer the one-shot Smart Video uses. Empty pieces when there is no key or
    the call fails: metadata is a convenience, never a failed render."""
    try:
        from backend.agents.generator import GeneratorAgent
        return await GeneratorAgent()._generate_metadata(script, niche, user_settings) or {}
    except Exception as e:  # noqa: BLE001
        logger.info("studio render: no AI metadata (%s)", e)
        return {"youtube": {}, "tiktok": {}}


async def _persist(project_id, row, doc, spec, total, provenance, final, narration, warnings, user_id, *,
                   user_settings=None, caption_status: str | None = None, clean: Path | None = None,
                   cover_at: float = 1.0) -> RenderDone:
    from backend.core.task_runner import spawn_background
    from backend.database import AsyncSessionLocal
    from backend.messaging.base import NotificationEvent
    from backend.messaging.manager import messaging
    from backend.models.generated_video import GeneratedVideo
    from backend.services import studio_store as store
    from backend.services.ffmpeg_service import extract_thumbnail
    from backend.services.video_utils import probe_duration

    s = doc["settings"]
    stem = f"studio_{project_id[:8]}_{uuid.uuid4().hex[:8]}"
    settings.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    video_out = settings.GENERATED_DIR / f"{stem}.mp4"
    voice_out = settings.AUDIO_DIR / f"{stem}_voice.mp3"
    await asyncio.to_thread(shutil.move, str(final), str(video_out))
    await asyncio.to_thread(shutil.copyfile, str(narration), str(voice_out))

    script = " ".join(sc["line"] for sc in doc["scenes"])
    # The project remembers the source it was made from (`input.source_id`),
    # and the Library's lineage reads GeneratedVideo.source_downloaded_video_id.
    # Read first: its niche grounds the titles below.
    lineage = await source_lineage((doc.get("input") or {}).get("source_id"))

    metadata = await _metadata(script, lineage.get("niche") or "", user_settings)
    youtube, tiktok = dict(metadata.get("youtube") or {}), dict(metadata.get("tiktok") or {})
    # The metadata writer's own fallback title is not a title: the project's
    # is better (and the cover would print "Untitled Video").
    ai_titled = bool(youtube.get("title")) and youtube.get("title") != FALLBACK_TITLE
    if not ai_titled:
        youtube["title"] = row.title
    title = youtube.get("title") or row.title

    thumbnail = None
    try:
        if clean is not None and clean.is_file():
            thumbnail = await frame_cover(clean, title, s["captions"]["style"], doc["aspect_ratio"], cover_at,
                                          settings.THUMBNAILS_DIR / f"{video_out.stem}_thumb.jpg")
        if thumbnail is None:
            thumbnail = await extract_thumbnail(video_out)
    except Exception:  # noqa: BLE001 — a cover is a convenience
        logger.warning("studio render: thumbnail failed", exc_info=True)

    duration = await asyncio.to_thread(probe_duration, video_out, total)
    user_clips = sum(1 for sc in doc["scenes"] if sc["visual"]["kind"] == "user_clip")
    video = GeneratedVideo(
        user_id=user_id, title=title, script=script, video_path=str(video_out), audio_path=str(voice_out),
        thumbnail_path=str(thumbnail) if thumbnail else None, aspect_ratio=doc["aspect_ratio"],
        duration_seconds=int(round(duration)), voice_id=spec.voice, gen_tier="free", source_type="smart_video",
        source_downloaded_video_id=lineage["source_downloaded_video_id"],
        source_scout_result_id=lineage["source_scout_result_id"], niche=lineage["niche"],
        youtube_title=youtube.get("title"), youtube_description=youtube.get("description"),
        youtube_tags_json=json.dumps(youtube.get("tags") or []), tiktok_title=tiktok.get("title"),
        tiktok_description=tiktok.get("description"),
        status="ready", project_id=project_id, caption_status=caption_status,
        metadata_status="ai_generated" if ai_titled else "fallback",
    )
    async with AsyncSessionLocal() as db:
        db.add(video)
        await db.commit()
        await db.refresh(video)
    await store.finish_render(project_id, video_id=video.id, thumbnail_path=str(thumbnail) if thumbnail else None,
                              user_id=user_id)

    result = {
        "generated_video_id": video.id, "title": video.title, "aspect_ratio": video.aspect_ratio,
        "source_type": "smart_video", "user_clip_count": user_clips, "project_id": project_id,
        "youtube_title": youtube.get("title"), "youtube_description": youtube.get("description"),
        "youtube_tags": youtube.get("tags") or [], "tiktok_title": tiktok.get("title"),
        "tiktok_description": tiktok.get("description"),
        "settings": {
            "aspect_ratio": doc["aspect_ratio"], "visual_style": s["visual_style"], "tts_provider": spec.provider,
            "tts_voice": spec.voice, "caption_enabled": s["captions"]["enabled"], "caption_style": s["captions"]["style"],
            "music_enabled": s["music"]["enabled"], "music_genre": s["music"]["genre"],
        },
    }
    output = {
        "generated_video_id": video.id, "project_id": project_id,
        "stock_sources": [provenance[i] for i in sorted(provenance)],
        "warnings": [code for code, _ in warnings],
    }
    try:
        from backend.core.user_intelligence import UserIntelligence
        await UserIntelligence().record_event("video_generated", {
            "generated_video_id": video.id, "project_id": project_id, "source_type": "smart_video",
            "aspect_ratio": doc["aspect_ratio"], "visual_style": s["visual_style"], "tts_provider": spec.provider,
            "scenes": len(doc["scenes"]), "duration_seconds": round(total, 1), "user_clips": user_clips,
            "first_render": not row.last_video_id,
        }, user_id)
    except Exception:  # noqa: BLE001 — behaviour tracking never fails a finished render
        logger.debug("record_event failed", exc_info=True)

    def notify() -> None:
        spawn_background(messaging.notify(NotificationEvent.VIDEO_GENERATED, user_id=user_id, title=video.title,
                                          video_id=video.id, duration_seconds=video.duration_seconds))

    return RenderDone(result=result, output=output, after_complete=notify)
