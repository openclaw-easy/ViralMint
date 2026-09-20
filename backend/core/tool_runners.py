# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Async runners for the modular /api/tools utilities (OSS variant).

Each runner is a thin wrapper around an existing OSS service (ffmpeg /
whisper / caption / tts / sfx / thumbnail) and follows the pattern:

    try:
        _tool_progress(...) multiple times
        ... do work ...
        _tool_success(job_id, out_path, inputs_to_cleanup, user_id)
    except Exception as e:
        _tool_fail(job_id, e, inputs_to_cleanup, user_id)

OSS adaptation vs the SaaS variant:
  - No billing — every `require_balance` / `bill` call is removed. Users
    bring their own keys.
  - Text-AI tools (hook-analysis / translate / metadata / auto-chapters)
    route through the user's own key via backend.core.ai_provider.get_ai_client.
  - Voiceover + dubbing use the local (free) Edge TTS service.
  - ffmpeg / whisper work runs locally against the OSS services. A few
    helpers the SaaS variant pulled from cloud-only services (silence
    removal, watermark, audio enhance, hook analysis) are implemented
    self-contained here.
"""
import asyncio
import logging
import math
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Shared helpers ─────────────────────────────────────────────────────────

async def _probe_aspect_ratio(video_path: Path) -> str:
    """Return '9:16' / '16:9' / '1:1' for the file's real dimensions.

    Delegates to `video_utils.aspect_label` — the probe here used to be binary
    ("is it landscape?"), so a SQUARE video got 9:16 caption geometry: wrong
    margins and font size, and since the safe-zone floor it would also get the
    vertical chrome inset a 1:1 feed post doesn't need. Same repair
    clip_extractor already received. Defaults to 9:16 on probe failure.
    """
    from backend.services.video_utils import aspect_label
    return await asyncio.to_thread(aspect_label, video_path)


# 9:16 as a ratio, with a 2% tolerance so 1079x1920 or 1080x1918 still counts.
_VERTICAL_RATIO = 9 / 16


def _is_already_vertical_sync(video_path: Path) -> bool:
    """True only when the source is 9:16 or NARROWER — i.e. nothing to crop.

    The old test was "portrait" (w <= h), which made Reframe a no-op on square
    (1080x1080) and 4:5 (1080x1350) sources: they came back byte-identical with
    an "already vertical" notice, even though both have side content a 9:16
    crop would remove. Only a source at or below the 9:16 ratio is genuinely
    done. Unreadable dimensions fall back to False so the reframe still runs —
    re-encoding a video we can't measure is cheaper than silently doing nothing.
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=10,
        )
        w, h = map(int, r.stdout.strip().split(",")[:2])
        if not w or not h:
            return False
        return (w / h) <= _VERTICAL_RATIO * 1.02
    except Exception:
        return False


async def _is_already_vertical(video_path: Path) -> bool:
    return await asyncio.to_thread(_is_already_vertical_sync, video_path)


def _cleanup_paths(*paths: Path):
    """Best-effort delete of scratch files. Never raises."""
    for p in paths:
        if not p:
            continue
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


async def _tool_progress(job_id: str, pct: float, step: str, user_id: str):
    """DB status + WS progress. The Tools UI listens on WS only —
    update_job_status alone is invisible to the frontend."""
    from backend.agents.job_helper import update_job_status
    from backend.core.ws_manager import ws_manager
    await update_job_status(job_id, "running", progress_pct=pct, current_step=step)
    await ws_manager.send_progress(job_id, pct, step, user_id)


async def _tool_success(
    job_id: str, out_path: Path, cleanup: list[Path], user_id: str,
    extra_output: dict | None = None,
    expect: dict | None = None,
) -> bool:
    """Validate the artifact, mark job success, emit job_complete, delete
    input scratch files. Returns True when the artifact was delivered, False
    when the validation gate failed the job instead.

    `expect` (all optional): {"min_duration": float, "orientation": str,
    "audio": bool} — assertions the RUNNER makes about its own output, which
    are fatal on mismatch precisely because the runner declared them. This is
    the hook the geometry class of bug needs: a blur-fill that shrank the
    picture, or a reframe that came back landscape, is invisible to an exit
    code and obvious to ffprobe.

    Returning bool matters for runners that do more work after this call —
    they must be able to tell "delivered" from "gate-failed".
    """
    from backend.agents.job_helper import update_job_status
    from backend.core.exceptions import GenerationError
    from backend.core.ws_manager import ws_manager
    from backend.services.output_validator import validate_output

    # "Verify the artifact before you call it done", as code, at the one point
    # every file-producing tool passes through. A runner that finishes without
    # raising has NOT proved it made something playable — Audio Enhance once
    # reported success over a 0-byte mp3, and each such bug got its own fix
    # afterwards, which means the next one waits for the next audit. Only
    # unambiguous breakage is fatal here (see output_validator's docstring);
    # soft findings never block.
    verdict = await validate_output(out_path, expect)
    if not verdict.ok:
        logger.warning(
            "tool job %s produced an invalid artifact (%s): %s",
            job_id, verdict.fatal_issues[0].check, out_path,
        )
        await _tool_fail(
            job_id, GenerationError(verdict.message()), cleanup, user_id,
            "output_validation",
        )
        return False

    output_data = {"file": str(out_path)}
    if extra_output:
        output_data.update(extra_output)
    await update_job_status(job_id, "success", progress_pct=100, current_step="Done",
                            output_data=output_data)
    await ws_manager.send({
        "type": "job_complete", "job_id": job_id,
        "result": {"download_url": f"/api/tools/download/{job_id}"},
    }, user_id)
    _cleanup_paths(*cleanup)
    return True


async def _tool_fail(job_id: str, err: Exception, cleanup: list[Path], user_id: str, tool: str):
    """Mark job failed, emit job_failed, delete input scratch files."""
    from backend.agents.job_helper import update_job_status
    from backend.core.ws_manager import ws_manager
    logger.error("TASK FAIL  tool:%s | job=%s: %s", tool, job_id[:8], err, exc_info=True)
    await update_job_status(job_id, "failed", error_message=str(err))
    await ws_manager.send({"type": "job_failed", "job_id": job_id, "error": str(err)}, user_id)
    _cleanup_paths(*cleanup)


async def _load_user_settings(user_id: str):
    """Load the UserSettings row for AI-provider routing. Returns None if absent."""
    from backend.database import AsyncSessionLocal
    from backend.models.user_settings import UserSettings
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        row = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
        return row.scalar_one_or_none()


def _strip_json_fence(text: str) -> str:
    """Strip an optional ```json ... ``` fence the model occasionally wraps."""
    out = (text or "").strip()
    if out.startswith("```"):
        out = out.split("\n", 1)[-1] if "\n" in out else out[3:]
        if out.endswith("```"):
            out = out[:-3]
        if out.lstrip().lower().startswith("json"):
            out = out.lstrip()[4:].lstrip()
    return out.strip()


# ── Captions ───────────────────────────────────────────────────────────────

async def run_tool_captions(job_id: str, in_path: Path, style: str, emoji_style: str, user_id: str = "local"):
    from backend.api.tools import tool_out_path
    logger.info("TASK START tool:captions | job=%s style=%s", job_id[:8], style)
    cleanup = [in_path]
    try:
        await _tool_progress(job_id, 5, "Transcribing audio...", user_id)
        from backend.services.whisper_service import whisper_service
        tx = await whisper_service.transcribe(str(in_path))
        segments = tx.get("segments", [])
        if not segments:
            raise ValueError("No speech detected — nothing to caption")

        await _tool_progress(job_id, 45, f"Generating {style} captions...", user_id)
        from backend.services.caption_service import generate_captions_ass, burn_captions
        aspect = await _probe_aspect_ratio(in_path)
        ass_path = await generate_captions_ass(segments, style=style, aspect_ratio=aspect, emoji_style=emoji_style)
        cleanup.append(ass_path)

        await _tool_progress(job_id, 75, "Burning captions into video...", user_id)
        out_path = tool_out_path(job_id, ".mp4")
        await burn_captions(in_path, ass_path, out_path)
        if not out_path.exists():
            raise RuntimeError("Caption burn produced no output")

        await _tool_success(job_id, out_path, cleanup, user_id)
        logger.info("TASK DONE  tool:captions | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "captions")


# ── Reframe (to vertical 9:16) ─────────────────────────────────────────────

async def run_tool_reframe(job_id: str, in_path: Path, user_id: str = "local"):
    from backend.api.tools import tool_out_path
    from backend.core.ws_manager import ws_manager
    import shutil

    logger.info("TASK START tool:reframe | job=%s", job_id[:8])
    cleanup = [in_path]
    try:
        out_path = tool_out_path(job_id, ".mp4")

        # If the source is already 9:16 (or narrower) there's nothing to
        # reframe — return it unchanged with an info constraint warning.
        # NOT "portrait": square and 4:5 sources DO have side content to crop.
        if await _is_already_vertical(in_path):
            await _tool_progress(job_id, 50, "Source already vertical...", user_id)
            await asyncio.to_thread(shutil.copy2, str(in_path), str(out_path))
            await ws_manager.send_constraint_warning(
                constraint="reframe_noop",
                message="Source is already vertical — returned unchanged.",
                severity="info",
                user_id=user_id,
            )
        else:
            await _tool_progress(job_id, 40, "Reframing to 9:16 (blur-fill)...", user_id)
            from backend.services.ffmpeg_service import convert_aspect_ratio
            result_path = await convert_aspect_ratio(
                in_path, target_aspect="9:16", method="blur_fill", output_path=out_path,
            )
            if result_path != out_path and Path(result_path).exists():
                await asyncio.to_thread(shutil.move, str(result_path), str(out_path))
            if not out_path.exists():
                raise RuntimeError("Reframe produced no output")

        # The one thing this tool exists to guarantee. Geometry is invisible
        # to an exit code and obvious to ffprobe: a blur-fill that shrank the
        # picture or a convert that came back landscape both "succeed".
        await _tool_success(job_id, out_path, cleanup, user_id,
                            expect={"orientation": "portrait"})
        logger.info("TASK DONE  tool:reframe | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "reframe")


# ── Audio enhance (denoise + loudness normalize) ───────────────────────────

# The denoise half of the chain. highpass/lowpass trim rumble + hiss, afftdn
# removes broadband noise. Kept separate from loudnorm because the measurement
# pass has to run THROUGH it — see _measure_loudness.
AUDIO_DENOISE_FILTER = "highpass=f=80,lowpass=f=12000,afftdn=nr=12"

# EBU R128 targets. -16 LUFS matches YouTube/TikTok playback reference within
# ~2 LU.
_TARGET_I = -16.0
_TARGET_TP = -1.5

# Dynamic single-pass fallback — used only when the measurement pass fails.
AUDIO_ENHANCE_FILTER = (
    f"{AUDIO_DENOISE_FILTER},loudnorm=I={_TARGET_I}:TP={_TARGET_TP}:LRA=11"
)


async def _measure_loudness(path: Path, through_denoise: bool = False) -> dict | None:
    """Pass 1: decode audio only and return loudnorm's measurement JSON
    (input_i / input_tp / input_lra / input_thresh / target_offset), or None
    when the file can't be measured. No video decode, no output written.

    `through_denoise` prefixes the measurement with the same denoise chain the
    apply pass uses. The enhance path MUST measure this way: loudnorm receives
    the denoised signal, and feeding it raw-audio measurements makes the
    linear gain wrong (the denoiser changes the level between measurement and
    application). Skip decisions measure raw (the default) — "is this file
    already at target AS-IS" is a question about the file, not about the
    filter chain.
    """
    import json as _json
    import re as _re

    af = f"loudnorm=I={_TARGET_I}:TP={_TARGET_TP}:LRA=11:print_format=json"
    if through_denoise:
        af = f"{AUDIO_DENOISE_FILTER},{af}"

    def _run():
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-vn", "-sn", "-dn",
             "-i", str(path), "-af", af, "-f", "null", "-"],
            capture_output=True, text=True, timeout=600,
        )
        # loudnorm prints its JSON block mid-stderr; ffmpeg appends the mux
        # summary AFTER it, so an end-anchored match never fires. Take the
        # last {...} block that parses and carries the expected keys.
        for blob in reversed(_re.findall(r"\{[^{}]*\}", r.stderr)):
            try:
                d = _json.loads(blob)
                for k in ("input_i", "input_tp", "input_lra", "input_thresh"):
                    # loudnorm reports "-inf" for digital silence. Note that
                    # float("-inf") PARSES — a bare float() call is not the
                    # guard it looks like — and feeding -inf into the linear
                    # filter yields a NaN target and an ffmpeg the arg parser
                    # rejects. Demand a finite number.
                    if not math.isfinite(float(d[k])):
                        raise ValueError(f"{k} is not finite: {d[k]!r}")
                return d
            except (KeyError, ValueError):
                continue
        return None

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        return None


def _is_already_normalized(measured: dict | None) -> bool:
    """True when the audio is close enough to target that enhancement can only
    do harm: within 1 LU of -16 LUFS with true peak at or under -1.0 dBTP.
    Conservative on purpose — an unmeasurable file is NOT "already done"."""
    if not measured:
        return False
    try:
        i, tp = float(measured["input_i"]), float(measured["input_tp"])
    except (KeyError, ValueError):
        return False
    if not (math.isfinite(i) and math.isfinite(tp)):
        return False   # digital silence reads as -inf, which is not "done"
    return abs(i - _TARGET_I) <= 1.0 and tp <= -1.0


def _linear_filter(measured: dict) -> str:
    """The pass-2 filter chain: denoise + linear (fixed-gain) loudnorm fed
    with the pass-1 measurement.

    Single-pass loudnorm is not a less accurate version of the same thing — it
    is a DIFFERENT algorithm. One pass runs in DYNAMIC mode, continuously
    riding the gain, which is audible as pumping/breathing on real content and
    crushes loudness range (an already-normalized clip measured LRA 1.4 → 1.0
    → 0.8 across repeat passes while the level stayed at -16). Linear mode
    applies a single gain offset computed from the measurement — transparent,
    and it preserves the source's dynamics.

    ffmpeg SILENTLY reverts linear=true to dynamic mode in two cases
    (af_loudnorm.c), and both must be pre-empted here or high-dynamics content
    gets crushed again:

      * measured_LRA above the LRA target → raise the target to the
        measurement (in linear mode LRA is only this revert threshold).
      * the needed gain would push the measured true peak past TP → ffmpeg
        does NOT apply less gain, it flips to dynamic. Pre-clamp the target
        level to what the TP headroom allows: quieter-but-clean, one fixed
        gain, no pumping.
    """
    try:
        input_i = float(measured["input_i"])
        input_tp = float(measured["input_tp"])
        lra_target = min(50.0, max(11.0, float(measured["input_lra"]) + 1.0))
    except (KeyError, ValueError):
        input_i, input_tp, lra_target = -24.0, -6.0, 11.0
    gain_needed = _TARGET_I - input_i
    headroom = (_TARGET_TP - 0.1) - input_tp  # 0.1 dB margin under the revert check
    eff_target_i = input_i + min(gain_needed, headroom)
    # loudnorm rejects I outside [-70, -5]; attenuation (negative gain) is fine.
    eff_target_i = max(-70.0, min(-5.0, eff_target_i))
    return (
        f"{AUDIO_DENOISE_FILTER},"
        f"loudnorm=I={eff_target_i:.2f}:TP={_TARGET_TP}:LRA={lra_target:g}"
        f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":linear=true"
    )


async def run_tool_audio_enhance(job_id: str, in_path: Path, user_id: str = "local"):
    from backend.api.tools import tool_out_path
    from backend.core.ws_manager import ws_manager
    from backend.services.clip_extractor import _has_video_stream
    from backend.services.ffmpeg_service import has_audio_stream
    logger.info("TASK START tool:audio_enhance | job=%s", job_id[:8])
    cleanup = [in_path]
    try:
        # Nothing to enhance without an audio track — say so instead of
        # handing ffmpeg a filtergraph it can't satisfy and surfacing its
        # stderr as the failure reason.
        if not await has_audio_stream(in_path):
            await _tool_fail(
                job_id, RuntimeError("This file has no audio track to enhance."),
                cleanup, user_id, "audio_enhance",
            )
            return

        # Measure first — enhancing already-normalized audio is a harmful
        # no-op. It cannot improve the level and it costs dynamics (another
        # denoise smear + another compression pass) plus a lossy re-encode
        # generation. Same pass-through contract as the no-audio-track case
        # above, with a warning that says why.
        raw = await _measure_loudness(in_path)
        if _is_already_normalized(raw):
            import shutil
            out_path = tool_out_path(job_id, in_path.suffix.lower() or ".mp4")
            await asyncio.to_thread(shutil.copy2, str(in_path), str(out_path))
            await ws_manager.send_constraint_warning(
                constraint="audio_enhance_noop",
                message=(f"Audio is already at target loudness "
                         f"({raw['input_i']} LUFS) — returned unchanged. "
                         "Re-enhancing normalized audio would only degrade it."),
                severity="info",
                user_id=user_id,
            )
            await _tool_success(job_id, out_path, cleanup, user_id)
            logger.info("TASK DONE  tool:audio_enhance (no-op, already normalized) | job=%s",
                        job_id[:8])
            return

        await _tool_progress(job_id, 20, "Denoising + normalizing audio...", user_id)
        has_video = await asyncio.to_thread(_has_video_stream, in_path)
        # The container follows what we ENCODE: an audio-only source (the tool
        # accepts podcasts) has no video stream to copy, and re-encoding a
        # .webm to H.264 is rejected outright by the WebM muxer.
        out_path = tool_out_path(job_id, ".mp4" if has_video else ".mp3")

        # Pass 1 of two: measure the DENOISED signal. loudnorm receives
        # afftdn's output, so measuring the raw audio makes the linear gain
        # wrong — the denoiser changes the level between measurement and
        # application. On failure we fall back to the old single-pass filter,
        # which normalizes but in dynamic mode.
        measured = await _measure_loudness(in_path, through_denoise=True)
        af = _linear_filter(measured) if measured else AUDIO_ENHANCE_FILTER

        def _enhance():
            # highpass/lowpass trim rumble + hiss; afftdn denoise; loudnorm
            # applies ONE fixed gain from the pass-1 measurement (linear=true)
            # instead of continuously riding it, which is what made repeat
            # passes audibly pump and crushed loudness range.
            base = ["ffmpeg", "-y", "-i", str(in_path), "-af", af]
            if not has_video:
                res = subprocess.run(
                    base + ["-c:a", "libmp3lame", "-q:a", "2", str(out_path)],
                    capture_output=True, text=True, timeout=900,
                )
                if res.returncode != 0:
                    raise RuntimeError(f"Audio enhance failed: {res.stderr[:400]}")
                return
            # Two passes: `-c:v copy` into .mp4 is invalid for VP8/VP9, and
            # .webm is both an accepted extension and what most screen
            # recorders export — that combination used to fail the job with a
            # raw "Could not find tag for codec vp9" and leave a 0-byte stub.
            # The re-encode pass works for any source we accept.
            attempts = (
                ("copy", ["-c:v", "copy"]),
                ("re-encode", ["-c:v", "libx264", "-preset", "veryfast",
                               "-crf", "20", "-pix_fmt", "yuv420p"]),
            )
            tail = ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    str(out_path)]
            last_err = ""
            for label, vargs in attempts:
                res = subprocess.run(base + vargs + tail,
                                     capture_output=True, text=True, timeout=900)
                if res.returncode == 0:
                    return
                last_err = res.stderr[-400:]
                logger.warning("audio enhance (%s pass) failed: %s", label, last_err)
                # A failed pass leaves a 0-byte/partial file behind; drop it so
                # the next pass isn't fooled by it and a failure can't be
                # mistaken for a result.
                try:
                    out_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise RuntimeError(f"Audio enhance failed: {last_err}")
        await asyncio.to_thread(_enhance)

        # Never report success over a missing/empty file — the download would
        # be 0 bytes and the job would still read "success".
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("Audio enhance produced no output")

        await _tool_success(job_id, out_path, cleanup, user_id)
        logger.info("TASK DONE  tool:audio_enhance | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "audio_enhance")


# ── Watermark (logo overlay) ───────────────────────────────────────────────

_WATERMARK_POS = {
    "top-left":     "{m}:{m}",
    "top-right":    "main_w-overlay_w-{m}:{m}",
    "bottom-left":  "{m}:main_h-overlay_h-{m}",
    "bottom-right": "main_w-overlay_w-{m}:main_h-overlay_h-{m}",
}


async def run_tool_watermark(
    job_id: str, in_path: Path, logo_path: Path,
    position: str, opacity: float, size_pct: float,
    user_id: str = "local",
):
    from backend.api.tools import tool_out_path
    logger.info("TASK START tool:watermark | job=%s pos=%s", job_id[:8], position)
    cleanup = [in_path, logo_path]
    try:
        await _tool_progress(job_id, 20, "Applying watermark...", user_id)
        out_path = tool_out_path(job_id, ".mp4")

        opacity = max(0.05, min(1.0, float(opacity)))
        size_pct = max(2.0, min(40.0, float(size_pct)))
        margin = 24
        overlay_xy = _WATERMARK_POS.get(position, _WATERMARK_POS["bottom-right"]).format(m=margin)

        def _apply():
            # Scale the logo to size_pct of the video width, apply opacity via
            # colorchannelmixer on the alpha channel, then overlay.
            fc = (
                f"[1:v]format=rgba,colorchannelmixer=aa={opacity:.3f},"
                f"scale=iw*{size_pct/100.0:.4f}*main_w/iw:-1[wm];"
                f"[0:v][wm]overlay={overlay_xy}:format=auto"
            )
            # main_w isn't available inside the [1:v] chain; use a two-step
            # scale referencing the main input width via scale2ref instead.
            fc = (
                f"[1:v][0:v]scale2ref=w=iw*{size_pct/100.0:.4f}:h=ow/mdar[wm][base];"
                f"[wm]format=rgba,colorchannelmixer=aa={opacity:.3f}[wmo];"
                f"[base][wmo]overlay={overlay_xy}:format=auto"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", str(in_path),
                "-i", str(logo_path),
                "-filter_complex", fc,
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "copy",
                str(out_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if res.returncode != 0:
                raise RuntimeError(f"Watermark render failed: {res.stderr[:400]}")
        await asyncio.to_thread(_apply)
        if not out_path.exists():
            raise RuntimeError("Watermark rendering failed — the logo or video may be invalid")

        await _tool_success(job_id, out_path, cleanup, user_id)
        logger.info("TASK DONE  tool:watermark | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "watermark")


# ── Remove silence (FFmpeg silencedetect → trim gaps) ──────────────────────

def _parse_silences(stderr: str) -> list[tuple[float, float]]:
    """Parse ffmpeg silencedetect stderr into [(start, end), ...] ranges."""
    silences: list[tuple[float, float]] = []
    cur_start = None
    for line in stderr.splitlines():
        line = line.strip()
        if "silence_start:" in line:
            try:
                cur_start = float(line.split("silence_start:")[1].strip().split()[0])
            except (ValueError, IndexError):
                cur_start = None
        elif "silence_end:" in line and cur_start is not None:
            try:
                end = float(line.split("silence_end:")[1].strip().split()[0].rstrip("|"))
                silences.append((cur_start, end))
            except (ValueError, IndexError):
                pass
            cur_start = None
    return silences


async def run_tool_remove_silence(job_id: str, in_path: Path, user_id: str = "local"):
    from backend.api.tools import tool_out_path
    from backend.core.ws_manager import ws_manager
    from backend.services.clip_extractor import (
        _has_video_stream, _silence_removal_timeout,
    )
    from backend.services.video_utils import probe_duration
    import shutil

    logger.info("TASK START tool:remove_silence | job=%s", job_id[:8])
    cleanup = [in_path]
    try:
        has_video = await asyncio.to_thread(_has_video_stream, in_path)
        # Audio-only sources are a first-class input here (the tool is pointed
        # at podcasts): they have no video stream to re-encode and come back
        # as mp3.
        out_path = tool_out_path(job_id, ".mp4" if has_video else ".mp3")

        await _tool_progress(job_id, 15, "Detecting silence...", user_id)

        # Both passes read the WHOLE timeline, so their runtime scales with the
        # source. The old flat caps (600s detect / 900s cut) were sized for
        # short clips; on a long recording they fired as a raw TimeoutExpired
        # carrying the entire ffmpeg argv into the UI.
        source_duration = await asyncio.to_thread(probe_duration, in_path, 0.0)
        timeout_s = _silence_removal_timeout(source_duration)

        def _detect():
            cmd = [
                "ffmpeg", "-i", str(in_path),
                "-af", "silencedetect=noise=-35dB:d=0.6",
                "-f", "null", "-",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
            return res.stderr
        stderr = await asyncio.to_thread(_detect)
        silences = _parse_silences(stderr)

        duration = await asyncio.to_thread(probe_duration, in_path, 0.0)
        # Build the keep-segments = the gaps between detected silences.
        keep: list[tuple[float, float]] = []
        cursor = 0.0
        for s_start, s_end in silences:
            if s_start - cursor > 0.05:
                keep.append((cursor, s_start))
            cursor = s_end
        if duration > 0 and duration - cursor > 0.05:
            keep.append((cursor, duration))

        if not silences or not keep:
            # Returned UNCHANGED, so the output keeps the SOURCE's container —
            # naming a copied .webm/.m4a ".mp4"/".mp3" would hand the browser
            # a content-type the bytes don't match. `out_path` above is named
            # for what the cut pass would ENCODE; nothing is encoded here.
            noop_path = tool_out_path(job_id, in_path.suffix.lower() or out_path.suffix)
            await asyncio.to_thread(shutil.copy2, str(in_path), str(noop_path))
            await ws_manager.send_constraint_warning(
                constraint="silence_noop",
                message="No removable silence detected — returned unchanged.",
                severity="info",
                user_id=user_id,
            )
            await _tool_success(job_id, noop_path, cleanup, user_id)
            logger.info("TASK DONE  tool:remove_silence (noop) | job=%s", job_id[:8])
            return

        await _tool_progress(job_id, 60, f"Cutting {len(silences)} silent gaps...", user_id)

        def _cut():
            # Build a single filter_complex that trims+concats the keep ranges.
            v_parts, a_parts = [], []
            for i, (a, b) in enumerate(keep):
                v_parts.append(
                    f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}];"
                )
                a_parts.append(
                    f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}];"
                )
            n = len(keep)
            if has_video:
                concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
                fc = (
                    "".join(v_parts) + "".join(a_parts)
                    + f"{concat_inputs}concat=n={n}:v=1:a=1[vout][aout]"
                )
                codec_args = [
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                ]
            else:
                concat_inputs = "".join(f"[a{i}]" for i in range(n))
                fc = ("".join(a_parts)
                      + f"{concat_inputs}concat=n={n}:v=0:a=1[aout]")
                codec_args = ["-map", "[aout]", "-c:a", "libmp3lame", "-q:a", "2"]
            cmd = [
                "ffmpeg", "-y", "-i", str(in_path),
                "-filter_complex", fc, *codec_args,
                str(out_path),
            ]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
            except subprocess.TimeoutExpired as e:
                # The default repr embeds the whole argv (absolute paths, the
                # entire filtergraph) and reaches the user verbatim.
                raise RuntimeError(
                    f"Silence removal timed out after {timeout_s // 60} min on a "
                    f"{source_duration / 60:.0f} min source. Trim it into shorter "
                    f"pieces and run them separately."
                ) from e
            if res.returncode != 0:
                raise RuntimeError(f"Silence cut failed: {res.stderr[:400]}")
        await asyncio.to_thread(_cut)

        await _tool_success(job_id, out_path, cleanup, user_id)
        logger.info("TASK DONE  tool:remove_silence | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "remove_silence")


# ── Merge clips ────────────────────────────────────────────────────────────

_MERGE_CLIPS_TARGETS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1":  (1080, 1080),
}


def _probe_dims_sync(video_path: Path) -> tuple[int, int]:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, timeout=10,
    )
    w, h = map(int, r.stdout.strip().split(","))
    return w, h


def _resolve_merge_target(first_clip: Path, target_aspect: str) -> tuple[int, int, str]:
    """Pick (width, height, label) for the merge output. `auto` snaps the
    first clip's aspect to the nearest of 9:16/16:9/1:1."""
    if target_aspect in _MERGE_CLIPS_TARGETS:
        w, h = _MERGE_CLIPS_TARGETS[target_aspect]
        return w, h, target_aspect
    try:
        sw, sh = _probe_dims_sync(first_clip)
        ratio = sw / sh if sh else 1.0
    except Exception:
        ratio = 9 / 16
    options = {"9:16": 9 / 16, "1:1": 1.0, "16:9": 16 / 9}
    label = min(options.keys(), key=lambda k: abs(options[k] - ratio))
    w, h = _MERGE_CLIPS_TARGETS[label]
    return w, h, label


def _crop_to_aspect_sync(src: Path, dst: Path, target_w: int, target_h: int):
    """Scale+center-crop a clip to exactly target_w x target_h and re-encode
    to a uniform codec so the concat is clean.

    "Normalized" has to mean the stream LAYOUT too, not just codec and fps. A
    SILENT source has no audio for `-c:a aac` to encode, so it normalized to
    video-only — and the concat demuxer takes its layout from the FIRST file.
    A silent intro card in front of a talking video therefore merged to a file
    with NO audio stream at all: the user's audio silently discarded, job
    "success". In the other order the audio track ended early. Giving every
    clip a real (silent) track heals both, and the crossfade path with it.
    """
    from backend.services.ffmpeg_service import has_audio_stream_sync

    vf = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},setsar=1"
    )
    src_has_audio = has_audio_stream_sync(src)

    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if not src_has_audio:
        # An INFINITE silence source — it must be bounded, or the encode
        # never ends. `-shortest` bounds it to the video stream.
        cmd += ["-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += ["-vf", vf]
    if not src_has_audio:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    cmd += [
        "-r", "30",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        str(dst),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        raise RuntimeError(f"Clip normalize failed: {res.stderr[:400]}")


async def run_tool_merge_clips(
    job_id: str,
    in_paths: list[Path],
    target_aspect: str = "auto",
    user_id: str = "local",
    transition: str = "none",
):
    """Stitch N user clips into one video. Sequential per-clip re-encode keeps
    RAM/CPU bounded (parallel ffmpegs thrash)."""
    from backend.api.tools import tool_out_path
    from backend.services.ffmpeg_service import stitch_clips

    logger.info(
        "TASK START tool:merge_clips | job=%s clips=%d target=%s",
        job_id[:8], len(in_paths), target_aspect,
    )
    cleanup: list[Path] = list(in_paths)
    stitch_transition = "fade" if transition == "crossfade" else "none"
    try:
        await _tool_progress(job_id, 5, "Probing clip dimensions...", user_id)
        target_w, target_h, label = await asyncio.to_thread(
            _resolve_merge_target, in_paths[0], target_aspect,
        )
        logger.info("tool:merge_clips | job=%s target=%dx%d (%s)", job_id[:8], target_w, target_h, label)

        normalized: list[Path] = []
        n = len(in_paths)
        for i, src in enumerate(in_paths):
            pct_start = 10 + (70 * i / n)
            await _tool_progress(job_id, pct_start, f"Cropping clip {i+1}/{n} to {label}...", user_id)
            norm_path = src.with_name(f"{src.stem}_n.mp4")
            await asyncio.to_thread(_crop_to_aspect_sync, src, norm_path, target_w, target_h)
            normalized.append(norm_path)
            cleanup.append(norm_path)

        await _tool_progress(job_id, 85, f"Stitching {n} clips...", user_id)
        out_path = tool_out_path(job_id, ".mp4")
        result = await stitch_clips(normalized, output_path=out_path, transition=stitch_transition)
        if result != out_path and Path(result).exists():
            import shutil
            await asyncio.to_thread(shutil.move, str(result), str(out_path))

        await _tool_success(job_id, out_path, cleanup, user_id)
        logger.info("TASK DONE  tool:merge_clips | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "merge_clips")


# ── Translate (captions + optional Edge-TTS dub) ───────────────────────────

_TRANSLATE_BATCH_SIZE = 20


def _translation_prompt(target_language: str, texts: list[str]) -> str:
    """Build the per-batch translation prompt. Strict 1:1 contract: the output
    array must have exactly the same length as the input."""
    indexed = "\n".join(f"  [{i}] {t!r}" for i, t in enumerate(texts))
    return f"""You translate video captions. Translate each line below into natural, conversational {target_language}. Preserve meaning — do NOT shorten, expand, or reorder lines.

Source lines (each line is one caption segment):
{indexed}

Respond with ONLY a JSON array of strings, one per line, in the same order. The array MUST have exactly {len(texts)} elements — same as input.

Example shape:
["translated line 0", "translated line 1", ...]

Respond with the JSON array only — no markdown, no commentary."""


async def _translate_segments(segments: list[dict], target_language: str, user_settings) -> list[dict]:
    """AI-translate Whisper segments into the target language. Returns segments
    with the same start/end timing and translated `text`.

    Batches in groups of _TRANSLATE_BATCH_SIZE — one AI call per batch. This
    used to be a SINGLE call carrying every segment, which meant a long video
    either blew the token budget or came back as truncated JSON, and either way
    the whole job died after the Whisper pass had already run.

    The 1:1 contract is real — a translation list shorter than its input slides
    every later caption onto the wrong timestamp — but enforcing it by raising
    is too brittle: one batch coming back 19-for-20 throws away everything
    translated so far. A short batch is split and retried in halves down to
    single lines, where the count can't be wrong; only a line that fails even
    alone keeps its source text, marked `untranslated`.
    """
    from backend.core.ai_provider import get_ai_client

    compact = [
        {"start": float(s.get("start", 0)), "end": float(s.get("end", 0)),
         "text": (s.get("text") or "").strip(), **{k: v for k, v in s.items()
                                                   if k not in ("start", "end", "text")}}
        for s in segments if (s.get("text") or "").strip()
    ]
    if not compact:
        return []

    ai = get_ai_client(user_settings)
    out: list[dict] = []

    for start in range(0, len(compact), _TRANSLATE_BATCH_SIZE):
        batch = compact[start:start + _TRANSLATE_BATCH_SIZE]
        source_texts = [s["text"] for s in batch]
        translated, failed_idx = await _translate_batch_resilient(
            ai, target_language, source_texts)
        if failed_idx:
            logger.warning(
                "translate: %d/%d line(s) kept their source text after "
                "per-line retries", len(failed_idx), len(source_texts))
        for i, (src_seg, txt) in enumerate(zip(batch, translated)):
            new_seg = {**src_seg, "text": txt}
            # DROP the per-word array — Whisper's `words` are the SOURCE
            # language's words with source-aligned timings. Keeping them makes
            # the caption renderer prefer word-level data over `text` and burn
            # the original words onto the video, which reads as "nothing was
            # translated". Without them the renderer falls back to the segment
            # text and spreads it evenly across the segment.
            new_seg.pop("words", None)
            if i in failed_idx:
                new_seg["untranslated"] = True
            out.append(new_seg)

    if not out:
        raise RuntimeError("Translation produced no segments")
    # Degrading a few lines is the point of the split-and-retry; degrading
    # EVERY line is not a translation. Shipping that would burn a full render
    # to hand back the source captions under a "translated" label, so fail
    # loudly instead. (A provider-level outage raises AIProviderError and
    # never reaches here — this catches a model that answers, badly, every
    # time.)
    if all(s.get("untranslated") for s in out):
        raise RuntimeError(
            "Translation failed — the model never returned a usable result. "
            "Try again, or pick a different target language."
        )
    return out


async def _translate_batch_resilient(
    ai, target_language: str, texts: list[str], attempts: int = 2,
) -> tuple[list[str], set[int]]:
    """`_translate_one_batch` with divide-and-conquer recovery.

    Returns (translations, indexes that kept their source text). Splitting is
    the fix for a model that merges or drops a line: halving the batch changes
    the shape of the request, and a single-line request has essentially no way
    to come back with the wrong count. Extra calls happen only on failure.
    """
    import json as _json
    try:
        return await _translate_one_batch(ai, target_language, texts, attempts), set()
    except (RuntimeError, ValueError, _json.JSONDecodeError) as e:
        if len(texts) <= 1:
            # One line that won't translate even alone. Keeping the source text
            # preserves every other caption's timing — dropping or padding it
            # would slide the rest of the video out of sync.
            logger.warning("translate: line kept untranslated (%s): %r", e, texts[:1])
            return list(texts), {0}
        mid = len(texts) // 2
        logger.info("translate: batch of %d failed (%s) — retrying as %d + %d",
                    len(texts), e, mid, len(texts) - mid)
        # attempts=1 below: the split IS the retry (see _translate_one_batch),
        # so re-retrying at every level would fan one bad 20-line batch out to
        # ~60 AI calls.
        left, lf = await _translate_batch_resilient(ai, target_language, texts[:mid], 1)
        right, rf = await _translate_batch_resilient(ai, target_language, texts[mid:], 1)
        return left + right, lf | {mid + i for i in rf}


async def _translate_one_batch(
    ai, target_language: str, texts: list[str], attempts: int = 2,
) -> list[str]:
    """Send one batch to the AI; parse + validate; retry once on a shape error."""
    import json as _json
    response = ""
    for attempt in range(max(1, attempts)):
        try:
            stricter = ("\n\nIMPORTANT: respond with ONLY the JSON array. "
                        "No code fences, no commentary, no extra text.")
            prompt = _translation_prompt(target_language, texts)
            response = await ai.chat(
                messages=[{"role": "user",
                           "content": prompt if attempt == 0 else prompt + stricter}],
                max_tokens=2048,
            )
            arr = _json.loads(_strip_json_fence(response))
            if not isinstance(arr, list):
                raise ValueError(f"Expected list, got {type(arr).__name__}")
            if len(arr) != len(texts):
                raise ValueError(
                    f"Translation count mismatch: input {len(texts)}, output {len(arr)}")
            if not all(isinstance(x, str) for x in arr):
                raise ValueError("Non-string in translation output")
            return arr
        except (_json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "Translate batch parse failed (attempt %d/%d): %s — raw: %r",
                attempt + 1, max(1, attempts), e, response[:200],
            )
            if attempt == max(1, attempts) - 1:
                raise RuntimeError(f"Translation failed after retry: {e}")

    raise RuntimeError("Translation flow ended unexpectedly")


async def run_tool_translate(
    job_id: str,
    in_path: Path,
    target_language: str,
    mode: str,                    # "captions_only" | "full_dub"
    voice_id: str,
    caption_style: str,
    emoji_style: str,
    user_id: str = "local",
):
    """Translate captions (and optionally dub the audio with Edge TTS) into
    another language."""
    from backend.api.tools import tool_out_path
    from backend.config import settings

    logger.info(
        "TASK START tool:translate | job=%s target=%s mode=%s",
        job_id[:8], target_language, mode,
    )
    cleanup: list[Path] = [in_path]
    try:
        user_settings = await _load_user_settings(user_id)

        await _tool_progress(job_id, 10, "Transcribing source audio...", user_id)
        from backend.services.whisper_service import whisper_service
        tx = await whisper_service.transcribe(str(in_path))
        segments = tx.get("segments", [])
        if not segments:
            raise ValueError("No speech detected — nothing to translate.")

        await _tool_progress(job_id, 35, f"Translating {len(segments)} segments...", user_id)
        translated_segments = await _translate_segments(segments, target_language, user_settings)

        out_path = tool_out_path(job_id, ".mp4")
        captioned_input = in_path

        if mode == "full_dub":
            await _tool_progress(job_id, 55, "Generating dubbed voiceover (Edge TTS)...", user_id)
            full_text = " ".join(s.get("text", "") for s in translated_segments).strip()
            if not full_text:
                raise ValueError("Translation produced empty text — try a different target language.")

            from backend.services.edge_tts_service import generate_voice
            settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            dub_audio = settings.AUDIO_DIR / f"{job_id}_dub.mp3"
            cleanup.append(dub_audio)
            await generate_voice(full_text, voice_id=voice_id or None, output_path=dub_audio)

            await _tool_progress(job_id, 75, "Muxing audio onto video...", user_id)
            from backend.services.ffmpeg_service import add_audio_to_video
            settings.TMP_DIR.mkdir(parents=True, exist_ok=True)
            dubbed_video = settings.TMP_DIR / f"{job_id}_dubbed.mp4"
            cleanup.append(dubbed_video)
            await add_audio_to_video(in_path, dub_audio, dubbed_video)
            captioned_input = dubbed_video

        await _tool_progress(job_id, 88, "Burning translated captions...", user_id)
        from backend.services.caption_service import generate_captions_ass, burn_captions
        aspect = await _probe_aspect_ratio(captioned_input)
        ass_path = await generate_captions_ass(
            translated_segments,
            style=caption_style,
            aspect_ratio=aspect,
            emoji_style=emoji_style,
        )
        cleanup.append(ass_path)
        await burn_captions(captioned_input, ass_path, out_path)
        if not out_path.exists():
            raise RuntimeError("Caption burn produced no output")

        await _tool_success(job_id, out_path, cleanup, user_id, extra_output={
            "target_language": target_language, "mode": mode,
        })
        logger.info("TASK DONE  tool:translate | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "translate")


# ── Hook analysis (Whisper first 10s → AI score) ───────────────────────────

_HOOK_PROMPT = """You are a short-form video hook expert. Analyze the opening
of this video based on its first ~10 seconds of spoken transcript.

TRANSCRIPT (first 10s):
{transcript}

Return STRICT JSON only — no markdown fences:
{{
  "score": <integer 1-10>,
  "verdict": "<one short sentence on why it works or doesn't>",
  "issues": ["<problem 1>", "<problem 2>"],
  "suggested_hooks": ["<rewritten opening 1>", "<rewritten opening 2>", "<rewritten opening 3>"]
}}"""


async def run_tool_hook_analysis(job_id: str, in_path: Path, user_id: str = "local"):
    """Analyze the opening 10s of a video and return a structured hook score +
    improvement suggestions in the job's output_data (no downloadable file)."""
    import json as _json
    from backend.agents.job_helper import update_job_status
    from backend.core.ws_manager import ws_manager

    logger.info("TASK START tool:hook_analysis | job=%s", job_id[:8])
    cleanup = [in_path]
    clip_audio = None
    try:
        from backend.core.ai_provider import get_ai_client

        user_settings = await _load_user_settings(user_id)

        await _tool_progress(job_id, 25, "Extracting opening audio...", user_id)
        clip_audio = in_path.with_suffix(".hook.mp3")
        cleanup.append(clip_audio)

        def _extract():
            cmd = [
                "ffmpeg", "-y", "-i", str(in_path),
                "-t", "10", "-vn", "-c:a", "libmp3lame", "-q:a", "4",
                str(clip_audio),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(f"Opening-audio extract failed: {res.stderr[:400]}")
        await asyncio.to_thread(_extract)

        await _tool_progress(job_id, 45, "Transcribing opening audio...", user_id)
        from backend.services.whisper_service import whisper_service
        tx = await whisper_service.transcribe(str(clip_audio))
        transcript = (tx.get("text") or "").strip()
        if not transcript:
            raise RuntimeError("No speech detected in the first 10 seconds.")

        await _tool_progress(job_id, 70, "Asking AI to score the hook...", user_id)
        ai = get_ai_client(user_settings)
        raw = await ai.chat(
            messages=[{"role": "user", "content": _HOOK_PROMPT.format(transcript=transcript[:2000])}],
            max_tokens=700,
        )
        text = _strip_json_fence(raw)
        try:
            result = _json.loads(text)
        except _json.JSONDecodeError:
            raise RuntimeError(f"AI returned malformed JSON: {text[:200]!r}")

        await update_job_status(
            job_id, "success", progress_pct=100, current_step="Done",
            output_data=result,
        )
        await ws_manager.send({"type": "job_complete", "job_id": job_id, "result": result}, user_id)
        _cleanup_paths(*cleanup)
        logger.info("TASK DONE  tool:hook_analysis | job=%s score=%s", job_id[:8], result.get("score"))
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "hook_analysis")


# ── Voiceover (Edge TTS, free) ─────────────────────────────────────────────

async def run_tool_voiceover(
    job_id: str, text: str, voice_id: str,
    video_path: Path | None = None, user_id: str = "local",
    provider: str = "edge_tts",
):
    """Narrate a script, optionally over an uploaded video.

    `provider` is honoured rather than ignored. The page has always offered
    both providers, and this runner used to call Edge TTS unconditionally with
    whatever `voice_id` arrived — so picking OpenAI handed Edge an id it does
    not know ("alloy"), which edge_tts rejects with a bare ValueError and the
    whole job failed. A paid provider with no key degrades to Edge and SAYS
    so, the same rule the Smart Video pipeline follows: a render that speaks
    in a voice the user did not pick must never be silent about it.
    """
    from backend.api.tools import tool_out_path
    from backend.config import settings
    from backend.services.tts_service import (
        DEFAULT_VOICES, PROVIDER_INFO, TTSProvider, generate_tts)

    logger.info("TASK START tool:voiceover | job=%s chars=%d", job_id[:8], len(text))
    cleanup: list[Path] = []
    if video_path:
        cleanup.append(video_path)
    try:
        try:
            tts_provider = TTSProvider(provider or "edge_tts")
        except ValueError:
            tts_provider = TTSProvider.EDGE_TTS

        api_key = settings.OPENAI_API_KEY if tts_provider == TTSProvider.OPENAI_TTS else ""
        if PROVIDER_INFO[tts_provider]["needs_key"] and not api_key:
            from backend.core.ws_manager import ws_manager
            await ws_manager.send_constraint_warning(
                constraint="tts_fallback",
                severity="warning",
                message=(
                    f"{PROVIDER_INFO[tts_provider]['label']} needs an API key — "
                    f"add one in Settings to use it. Narrated with the free "
                    f"Edge voice instead."
                ),
            )
            tts_provider = TTSProvider.EDGE_TTS
            voice_id = ""   # an OpenAI voice id is not an Edge voice id

        label = PROVIDER_INFO[tts_provider]["label"]
        await _tool_progress(job_id, 30, f"Generating voice ({label})...", user_id)

        mp3_path = tool_out_path(job_id, ".mp3")
        await generate_tts(
            text,
            provider=tts_provider,
            voice_id=voice_id or DEFAULT_VOICES[tts_provider],
            api_key=api_key,
            output_path=mp3_path,
        )

        out_path = mp3_path
        if video_path and video_path.exists():
            await _tool_progress(job_id, 75, "Overlaying voice on video...", user_id)
            from backend.services.ffmpeg_service import add_audio_to_video
            out_path = tool_out_path(job_id, ".mp4")
            await add_audio_to_video(video_path, mp3_path, out_path)
            cleanup.append(mp3_path)

        await _tool_success(job_id, out_path, cleanup, user_id)
        logger.info("TASK DONE  tool:voiceover | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "voiceover")


# ── Transform (flip / rotate / loop / volume) ──────────────────────────────

async def run_tool_transform(
    job_id: str, in_path: Path, operation: str, amount: str = "", user_id: str = "local",
):
    """Quick local FFmpeg transforms: flip / rotate / loop / volume / mute."""
    from backend.api.tools import tool_out_path

    logger.info("TASK START tool:transform | job=%s op=%s", job_id[:8], operation)
    cleanup = [in_path]
    try:
        out_path = tool_out_path(job_id, ".mp4")

        # "Remove audio" on a video that never had any is a no-op that looks
        # like a broken tool — you get a file back and nothing changed. Say so
        # instead. Still produce the output: the user's goal (a video with no
        # sound) is already true, so failing would be wrong.
        if operation == "mute":
            from backend.core.ws_manager import ws_manager
            from backend.services.ffmpeg_service import has_audio_stream
            if not await has_audio_stream(in_path):
                await ws_manager.send_constraint_warning(
                    constraint="mute_no_audio", severity="info",
                    message=(
                        "This video had no audio track to begin with — the "
                        "result is the same video, unchanged."
                    ),
                    user_id=user_id,
                )

        def _run():
            base = ["ffmpeg", "-y"]
            if operation == "mute":
                # Strip every audio track outright. Measured against the
                # `volume=0` it replaces in the UI (ffmpeg 7.1): volume=0
                # leaves a SILENT AAC stream in the file — it re-encodes the
                # audio to silence rather than removing it, so the bytes and
                # the stream are both still there. `-an` drops it, and
                # skipping the AAC pass makes this near-instant on a long
                # clip. Video is stream-copied either way.
                cmd = base + ["-i", str(in_path), "-c:v", "copy", "-an",
                              "-movflags", "+faststart", str(out_path)]
            elif operation == "loop":
                try:
                    n = max(2, min(20, int(float(amount or "2"))))
                except (ValueError, TypeError):
                    n = 2
                cmd = base + ["-stream_loop", str(n - 1), "-i", str(in_path),
                              "-c", "copy", str(out_path)]
            elif operation == "volume":
                try:
                    vol = max(0.0, min(8.0, float(amount)))
                except (ValueError, TypeError):
                    vol = 1.0
                cmd = base + ["-i", str(in_path), "-af", f"volume={vol}",
                              "-c:v", "copy", "-c:a", "aac", str(out_path)]
            else:
                vf = {
                    "flip_h": "hflip",
                    "flip_v": "vflip",
                    "rotate_cw": "transpose=1",
                    "rotate_ccw": "transpose=2",
                    "rotate_180": "transpose=2,transpose=2",
                }.get(operation)
                if not vf:
                    raise RuntimeError(f"Unknown transform operation: {operation}")
                cmd = base + ["-i", str(in_path), "-vf", vf,
                              "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                              "-pix_fmt", "yuv420p", "-c:a", "copy", str(out_path)]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if res.returncode != 0:
                raise RuntimeError(f"Transform failed: {res.stderr[:400]}")

        await _tool_progress(job_id, 40, "Processing...", user_id)
        await asyncio.to_thread(_run)
        await _tool_success(job_id, out_path, cleanup, user_id)
        logger.info("TASK DONE  tool:transform | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "transform")


# ── Music visualizer ───────────────────────────────────────────────────────

_VIZ_PALETTES = {
    "sunset": {"bg": "0x1A0E2E", "c": "0xFFD93D"},
    "ocean":  {"bg": "0x081428", "c": "0x4FC3F7"},
    "neon":   {"bg": "0x0A0A1E", "c": "0xFF2ED1"},
    "mono":   {"bg": "0x111111", "c": "0xFFFFFF"},
}
_VIZ_DIMS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}


async def run_tool_music_visualizer(
    job_id: str, audio_path: Path,
    style: str = "waves", palette: str = "sunset", aspect: str = "9:16",
    user_id: str = "local",
):
    """Turn an audio file into an animated visualizer video synced to the
    audio. Pure local FFmpeg."""
    from backend.api.tools import tool_out_path
    from backend.services.video_utils import probe_duration

    logger.info("TASK START tool:music_visualizer | job=%s style=%s", job_id[:8], style)
    cleanup: list[Path] = [audio_path]
    try:
        pal = _VIZ_PALETTES.get(palette, _VIZ_PALETTES["sunset"])
        W, H = _VIZ_DIMS.get(aspect, _VIZ_DIMS["9:16"])
        dur = probe_duration(audio_path, default=0.0)
        if dur <= 0:
            raise RuntimeError("Could not read the audio file")
        out_path = tool_out_path(job_id, ".mp4")

        await _tool_progress(job_id, 40, "Rendering visualizer...", user_id)

        def _render():
            if style == "bars":
                fc = f"[0:a]showcqt=s={W}x{H}:axis=0,format=yuv420p[v]"
                inputs = ["-i", str(audio_path)]
                amap = "0:a"
            elif style == "spectrum":
                fc = (f"[0:a]showspectrum=s={W}x{H}:mode=combined:color=intensity:"
                      f"scale=cbrt:slide=scroll,format=yuv420p[v]")
                inputs = ["-i", str(audio_path)]
                amap = "0:a"
            else:  # waves
                wave_h = max(160, H // 4)
                fc = (
                    f"[1:a]showwaves=s={W}x{wave_h}:mode=cline:colors={pal['c']}|{pal['c']},"
                    f"format=rgba,colorkey=0x000000:0.30:0.10[w];"
                    f"[0:v][w]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
                )
                inputs = ["-f", "lavfi", "-i", f"color=c={pal['bg']}:s={W}x{H}:r=30",
                          "-i", str(audio_path)]
                amap = "1:a"
            cmd = (
                ["ffmpeg", "-y"] + inputs +
                ["-filter_complex", fc, "-map", "[v]", "-map", amap,
                 "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out_path)]
            )
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if res.returncode != 0:
                raise RuntimeError(f"Visualizer render failed: {res.stderr[:400]}")
        await asyncio.to_thread(_render)

        await _tool_success(job_id, out_path, cleanup, user_id)
        logger.info("TASK DONE  tool:music_visualizer | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "music_visualizer")


# ── GIF (two-pass palette) ─────────────────────────────────────────────────

async def run_tool_gif(
    job_id: str, in_path: Path,
    fps: int = 15, width: int = 540,
    start_seconds: float = 0.0, duration_seconds: float = 0.0,
    user_id: str = "local",
):
    """Convert video → animated GIF with two-pass palette generation."""
    logger.info("TASK START tool:gif | job=%s", job_id[:8])
    cleanup = [in_path]
    palette_path = None
    try:
        from backend.api.tools import tool_out_path

        await _tool_progress(job_id, 15, "Building color palette...", user_id)

        trim_args: list[str] = []
        if start_seconds > 0:
            trim_args.extend(["-ss", f"{start_seconds:.2f}"])
        if duration_seconds > 0:
            trim_args.extend(["-t", f"{duration_seconds:.2f}"])

        scale_fps = f"fps={fps},scale={width}:-1:flags=lanczos"
        palette_path = tool_out_path(job_id, "_palette.png")
        cleanup.append(palette_path)

        def _palette():
            cmd = (
                ["ffmpeg", "-y"] + trim_args + ["-i", str(in_path),
                 "-vf", f"{scale_fps},palettegen=stats_mode=diff",
                 str(palette_path)]
            )
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(f"GIF palette failed: {res.stderr[:400]}")
        await asyncio.to_thread(_palette)

        await _tool_progress(job_id, 60, "Encoding GIF...", user_id)
        out_path = tool_out_path(job_id, ".gif")

        def _encode():
            cmd = (
                ["ffmpeg", "-y"] + trim_args + ["-i", str(in_path),
                 "-i", str(palette_path),
                 "-lavfi", f"{scale_fps} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5",
                 str(out_path)]
            )
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if res.returncode != 0:
                raise RuntimeError(f"GIF encode failed: {res.stderr[:400]}")
        await asyncio.to_thread(_encode)

        await _tool_success(job_id, out_path, cleanup, user_id)
        logger.info("TASK DONE  tool:gif | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "gif")


# ── Speed ──────────────────────────────────────────────────────────────────

def _build_speed_filters(speed: float, keep_pitch: bool) -> tuple[str, str]:
    """Build (video_filter, audio_filter) for the ffmpeg speed-change. Pure
    function so it's unit-testable without invoking ffmpeg."""
    v_filter = f"setpts={1.0 / speed:.6f}*PTS"
    if not keep_pitch:
        a_filter = f"asetrate=44100*{speed:.6f},aresample=44100"
        return v_filter, a_filter
    remaining = speed
    parts: list[str] = []
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.6f}")
    return v_filter, ",".join(parts)


async def run_tool_speed(
    job_id: str, in_path: Path,
    speed: float = 1.5, keep_pitch: bool = True, user_id: str = "local",
):
    """Apply a video/audio speed change via FFmpeg setpts + atempo."""
    logger.info("TASK START tool:speed | job=%s speed=%s pitch=%s", job_id[:8], speed, keep_pitch)
    cleanup = [in_path]
    try:
        from backend.api.tools import tool_out_path

        await _tool_progress(job_id, 20, f"Re-encoding at {speed}x speed...", user_id)
        v_filter, a_filter = _build_speed_filters(speed, keep_pitch)
        out_path = tool_out_path(job_id, ".mp4")

        def _encode():
            cmd = [
                "ffmpeg", "-y", "-i", str(in_path),
                "-filter:v", v_filter,
                "-filter:a", a_filter,
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
                str(out_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if res.returncode != 0:
                raise RuntimeError(f"Speed re-encode failed: {res.stderr[:400]}")
        await asyncio.to_thread(_encode)

        await _tool_success(job_id, out_path, cleanup, user_id)
        logger.info("TASK DONE  tool:speed | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "speed")


# ── Metadata (AI: title / description / tags / caption) ────────────────────

_METADATA_PROMPT = """Generate SEO-optimized metadata for a short-form video.

{input_block}

Return STRICT JSON only — no markdown fences:
{{
  "youtube_title": "<= 70 chars, click-worthy",
  "youtube_description": "<2-3 sentence description with a call to action>",
  "tags": ["<tag1>", "<tag2>", "... up to 12 relevant tags"],
  "tiktok_caption": "<short punchy caption with 3-5 hashtags>"
}}"""


async def run_tool_metadata(
    job_id: str, text: str = "", topic: str = "",
    in_path: Path | None = None, user_id: str = "local",
):
    """Generate YT + TikTok metadata from a transcript / video / topic."""
    import json as _json
    logger.info("TASK START tool:metadata | job=%s mode=%s",
                job_id[:8], "file" if in_path else ("text" if text else "topic"))
    cleanup: list[Path] = []
    if in_path:
        cleanup.append(in_path)

    try:
        from backend.api.tools import tool_out_path
        from backend.core.ai_provider import get_ai_client

        if in_path:
            await _tool_progress(job_id, 20, "Transcribing audio (~30-60s on CPU)...", user_id)
            from backend.services.whisper_service import whisper_service
            tx = await whisper_service.transcribe(str(in_path))
            transcript = (tx.get("text") or "").strip()
            if not transcript:
                raise RuntimeError(
                    "No speech detected — try a video with clear audio or "
                    "switch to the text/topic input mode."
                )
            input_block = f"VIDEO TRANSCRIPT:\n{transcript[:8000]}"
        elif text:
            input_block = f"SCRIPT / TRANSCRIPT:\n{text[:8000]}"
        else:
            input_block = f"TOPIC:\n{topic}"

        await _tool_progress(job_id, 60, "Generating metadata...", user_id)
        prompt = _METADATA_PROMPT.format(input_block=input_block)

        user_settings = await _load_user_settings(user_id)
        ai = get_ai_client(user_settings)
        raw = await ai.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=900,
        )

        out_text = _strip_json_fence(raw)
        try:
            parsed = _json.loads(out_text)
        except _json.JSONDecodeError:
            raise RuntimeError(f"AI returned malformed JSON: {out_text[:200]!r}")

        out_path = tool_out_path(job_id, ".json")
        out_path.write_text(_json.dumps(parsed, indent=2), encoding="utf-8")

        await _tool_success(job_id, out_path, cleanup, user_id, extra_output={"metadata": parsed})
        logger.info("TASK DONE  tool:metadata | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "metadata")


# ── Auto chapters (Whisper segments → AI clusters) ─────────────────────────

_CHAPTERS_PROMPT = """Generate YouTube chapter markers from this transcript.

Transcript segments (each line is `[MM:SS] text`):
{segments_text}

Total video duration: {duration_label}

TASK
Group the segments into {target_label} chapters. Each chapter must:
  - Start at a natural topic boundary (not mid-sentence)
  - Have a 2-6 word title that's specific to that section's content
  - First chapter MUST start at 00:00 (YouTube requirement)
  - Chapters must be ordered by start time, ascending
  - Each chapter must be at least 30 seconds long

Return STRICT JSON only — no markdown fences:
{{
  "chapters": [
    {{"start_seconds": 0, "title": "Intro"}},
    {{"start_seconds": 47, "title": "The Hook"}}
  ]
}}"""


def _format_chapters_text(chapters: list[dict]) -> str:
    """Render a chapter list into the `MM:SS Title` plain text format YouTube
    accepts when pasted into a video description."""
    lines = []
    for ch in chapters:
        secs = max(0, int(ch.get("start_seconds", 0)))
        mm, ss = divmod(secs, 60)
        hh = 0
        if mm >= 60:
            hh, mm = divmod(mm, 60)
        stamp = f"{hh:01d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        title = str(ch.get("title", "")).strip() or "Chapter"
        lines.append(f"{stamp} {title}")
    return "\n".join(lines) + ("\n" if lines else "")


async def run_tool_auto_chapters(
    job_id: str, in_path: Path, target_count: int = 0, user_id: str = "local",
):
    """Whisper-transcribe → AI clusters into chapters → write a `MM:SS Title`
    text file ready to paste into a YT description."""
    import json as _json
    logger.info("TASK START tool:auto_chapters | job=%s target=%d", job_id[:8], target_count)
    cleanup = [in_path]

    try:
        from backend.api.tools import tool_out_path
        from backend.core.ai_provider import get_ai_client
        from backend.services.whisper_service import whisper_service

        await _tool_progress(job_id, 20, "Transcribing audio (~30-60s on CPU)...", user_id)
        tx = await whisper_service.transcribe(str(in_path))
        segments = tx.get("segments") or []
        if not segments:
            raise RuntimeError(
                "No speech detected — auto-chapters needs a transcript. "
                "Make sure the audio is intelligible and try again."
            )

        duration = max((s.get("end") or 0) for s in segments) or sum(
            (s.get("end", 0) - s.get("start", 0)) for s in segments
        )
        duration = max(int(duration), 0)
        mm_total, ss_total = divmod(duration, 60)
        duration_label = f"{mm_total}m {ss_total}s"

        if target_count <= 0:
            target_count = max(3, min(15, duration // 150))
        target_label = f"exactly {target_count}"

        lines: list[str] = []
        for s in segments[:600]:
            t = (s.get("text") or "").strip()
            if not t:
                continue
            start = int(s.get("start", 0))
            mm, ss = divmod(start, 60)
            lines.append(f"[{mm:02d}:{ss:02d}] {t}")
        segments_text = "\n".join(lines)

        await _tool_progress(job_id, 70, "Clustering chapters...", user_id)
        prompt = _CHAPTERS_PROMPT.format(
            segments_text=segments_text,
            duration_label=duration_label,
            target_label=target_label,
        )

        user_settings = await _load_user_settings(user_id)
        ai = get_ai_client(user_settings)
        raw = await ai.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
        )

        text = _strip_json_fence(raw)
        try:
            parsed = _json.loads(text)
            chapters = parsed.get("chapters", []) if isinstance(parsed, dict) else []
        except _json.JSONDecodeError:
            raise RuntimeError(f"AI returned malformed JSON: {text[:200]!r}")

        valid: list[dict] = []
        for ch in chapters:
            try:
                start = int(ch.get("start_seconds", 0))
            except (TypeError, ValueError):
                continue
            if start < 0 or start > duration:
                continue
            valid.append({"start_seconds": start, "title": str(ch.get("title", "")).strip()})
        valid.sort(key=lambda c: c["start_seconds"])
        if not valid:
            raise RuntimeError("AI produced no usable chapters")
        if valid[0]["start_seconds"] != 0:
            valid.insert(0, {"start_seconds": 0, "title": valid[0]["title"] or "Intro"})

        plain_text = _format_chapters_text(valid)
        out_path = tool_out_path(job_id, ".txt")
        out_path.write_text(plain_text, encoding="utf-8")

        await _tool_success(
            job_id, out_path, cleanup, user_id,
            extra_output={"chapters": valid, "count": len(valid)},
        )
        logger.info("TASK DONE  tool:auto_chapters | job=%s chapters=%d", job_id[:8], len(valid))
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "auto_chapters")


# ── Trim ───────────────────────────────────────────────────────────────────

async def run_tool_trim(
    job_id: str, in_path: Path,
    start_seconds: float, end_seconds: float, user_id: str = "local",
):
    """Keep the [start, end] segment of a video, dropping the rest. Pure
    FFmpeg via the shared `extract_clip` helper (vertical=False preserves the
    source aspect)."""
    logger.info("TASK START tool:trim | job=%s %.2f-%.2f", job_id[:8], start_seconds, end_seconds)
    cleanup = [in_path]
    try:
        from backend.api.tools import tool_out_path
        from backend.services.ffmpeg_service import extract_clip
        from backend.services.video_utils import probe_duration

        await _tool_progress(job_id, 20, "Trimming...", user_id)
        duration = await asyncio.to_thread(probe_duration, in_path, 0.0)
        end = end_seconds
        if duration > 0:
            end = min(end_seconds, duration)
        if end - start_seconds < 0.5:
            raise ValueError(
                f"Trim range too short ({end - start_seconds:.2f}s) — need at least 0.5s."
            )

        out_path = tool_out_path(job_id, ".mp4")
        await extract_clip(in_path, float(start_seconds), float(end), out_path, vertical=False)
        if not out_path.exists():
            raise RuntimeError("Trim produced no output")

        await _tool_success(
            job_id, out_path, cleanup, user_id,
            extra_output={"start_seconds": float(start_seconds), "end_seconds": float(end)},
        )
        logger.info("TASK DONE  tool:trim | job=%s", job_id[:8])
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "trim")


# ── Subtitles (Whisper → .srt / .vtt / .txt) ───────────────────────────────

def _format_ts(seconds: float, vtt: bool = False) -> str:
    seconds = max(0.0, float(seconds))
    hh = int(seconds // 3600)
    mm = int((seconds % 3600) // 60)
    ss = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    sep = "." if vtt else ","
    return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ms:03d}"


def _build_subtitle_file(segments: list[dict], fmt: str, out_path: Path):
    """Write Whisper segments out as SRT / VTT / TXT."""
    if fmt == "txt":
        text = " ".join((s.get("text") or "").strip() for s in segments).strip()
        out_path.write_text(text + "\n", encoding="utf-8")
        return out_path

    vtt = fmt == "vtt"
    lines: list[str] = []
    if vtt:
        lines.append("WEBVTT\n")
    for i, s in enumerate(segments, 1):
        start = _format_ts(s.get("start", 0), vtt=vtt)
        end = _format_ts(s.get("end", 0), vtt=vtt)
        body = (s.get("text") or "").strip()
        if not vtt:
            lines.append(str(i))
        lines.append(f"{start} --> {end}")
        lines.append(body)
        lines.append("")
    out_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return out_path


async def run_tool_subtitles(
    job_id: str, in_path: Path, fmt: str = "srt", user_id: str = "local",
):
    """Transcribe a video with Whisper and emit a downloadable subtitle file
    (SRT / VTT) or a plain transcript (TXT). No burn-in."""
    logger.info("TASK START tool:subtitles | job=%s fmt=%s", job_id[:8], fmt)
    cleanup = [in_path]
    try:
        from backend.api.tools import tool_out_path
        from backend.services.whisper_service import whisper_service

        await _tool_progress(job_id, 20, "Transcribing audio (~30-60s on CPU)...", user_id)
        tx = await whisper_service.transcribe(str(in_path))
        segments = tx.get("segments") or []
        if not segments:
            raise ValueError("No speech detected — nothing to transcribe.")

        await _tool_progress(job_id, 80, f"Writing .{fmt} file...", user_id)
        out_path = tool_out_path(job_id, f".{fmt}")
        await asyncio.to_thread(_build_subtitle_file, segments, fmt, out_path)

        await _tool_success(
            job_id, out_path, cleanup, user_id,
            extra_output={"format": fmt, "segment_count": len(segments)},
        )
        logger.info("TASK DONE  tool:subtitles | job=%s fmt=%s segs=%d", job_id[:8], fmt, len(segments))
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "subtitles")


# ── Auto-zoom (Whisper words → zoom pulses) ────────────────────────────────

async def run_tool_auto_zoom(
    job_id: str, in_path: Path,
    zoom_factor: float = 1.15, words_per_group: int = 3, user_id: str = "local",
):
    """Add subtle zoom "punch-in" pulses on spoken words for energy. Whisper
    word timings drive the pulses; the shared `apply_auto_zoom` helper does the
    FFmpeg zoompan work."""
    from backend.core.ws_manager import ws_manager
    import shutil

    logger.info(
        "TASK START tool:auto_zoom | job=%s zoom=%.2f wpg=%d",
        job_id[:8], zoom_factor, words_per_group,
    )
    cleanup = [in_path]
    noop = False
    try:
        from backend.api.tools import tool_out_path
        from backend.services.whisper_service import whisper_service
        from backend.services.ffmpeg_service import apply_auto_zoom

        await _tool_progress(job_id, 20, "Transcribing audio (~30-60s on CPU)...", user_id)
        tx = await whisper_service.transcribe(str(in_path))
        segments = tx.get("segments") or []
        words: list[dict] = []
        for seg in segments:
            for w in seg.get("words") or []:
                token = w.get("word") or w.get("text")
                if token and w.get("start") is not None:
                    words.append({
                        "text": token,
                        "start": float(w.get("start", 0)),
                        "end": float(w.get("end", 0)),
                    })

        out_path = tool_out_path(job_id, ".mp4")
        if not words:
            noop = True
            await asyncio.to_thread(shutil.copy2, str(in_path), str(out_path))
            await ws_manager.send_constraint_warning(
                constraint="auto_zoom_noop",
                message="No clear speech detected — returned unchanged (zoom pulses need word timings).",
                severity="info",
                user_id=user_id,
            )
        else:
            await _tool_progress(job_id, 55, f"Applying zoom pulses to {len(words)} words...", user_id)
            result_path = await apply_auto_zoom(
                in_path, words, output_path=out_path,
                zoom_factor=float(zoom_factor), words_per_group=int(words_per_group),
            )
            # apply_auto_zoom hands back the SOURCE only when no usable word
            # group survived its own filtering; an ffmpeg failure now RAISES
            # (before, it returned the source and this branch reported a
            # byte-identical copy as a successful zoom).
            if result_path != out_path and not out_path.exists():
                noop = True
                await asyncio.to_thread(shutil.copy2, str(in_path), str(out_path))
                await ws_manager.send_constraint_warning(
                    constraint="auto_zoom_noop",
                    message="No usable word timings — returned unchanged (zoom pulses need spoken words).",
                    severity="info",
                    user_id=user_id,
                )

        await _tool_success(job_id, out_path, cleanup, user_id)
        logger.info("TASK DONE  tool:auto_zoom | job=%s words=%d noop=%s", job_id[:8], len(words), noop)
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "auto_zoom")


# ── Video compressor (free, local ffmpeg) ──────────────────────────────────


# level → (CRF, audio bitrate). CRF is the quality knob libx264 actually
# respects; higher = smaller + softer. The five steps are spaced ~3-4 CRF
# apart because a smaller gap is not visible and a bigger one skips a useful
# trade. Audio rides along: a 32-CRF video next to a 192k track is wasted
# bytes, and a 18-CRF video next to a 64k track sounds broken.
_COMPRESS_LEVELS: dict[str, tuple[int, str]] = {
    "maximum": (32, "64k"),
    "high":    (28, "96k"),
    "medium":  (24, "128k"),
    "low":     (21, "160k"),
    "minimal": (18, "192k"),
}

# label → target height. Width is derived from the source aspect so we never
# distort; "original" keeps the source resolution and only re-encodes.
_COMPRESS_HEIGHTS: dict[str, int] = {
    "original": 0,
    "1280x720": 720,
    "854x480":  480,
    "640x360":  360,
    "426x240":  240,
}


def compress_settings(level: str) -> tuple[int, str]:
    """(crf, audio_bitrate) for a compression level. Unknown → the default.

    Pure so the mapping is testable without invoking ffmpeg — same reason
    `_build_speed_filters` is pure.
    """
    return _COMPRESS_LEVELS.get((level or "").lower(), _COMPRESS_LEVELS["high"])


def compress_scale_filter(
    src_w: int, src_h: int, resolution: str,
) -> tuple[str, bool]:
    """Build the scale filter for a compress run.

    Returns ``(vf, downscaled)``. `vf` is "" when no scaling should happen —
    either the caller asked for "original", the source dimensions are unknown,
    or the requested height is TALLER than the source.

    That last case is the one worth naming: picking "HD 720p" for a 480p clip
    must not re-encode it up to 720p. Upscaling invents pixels, makes the file
    BIGGER, and the user asked to compress. We keep the source size and report
    `downscaled=False` so the runner can say so.

    Height is the axis because these presets are named by height and the
    sources are a mix of portrait and landscape; width comes from `-2`, which
    both preserves aspect and rounds to an even number (libx264 rejects odd
    dimensions).
    """
    target_h = _COMPRESS_HEIGHTS.get(resolution or "original", 0)
    if not target_h or src_h <= 0:
        return "", False
    if target_h >= src_h:
        return "", False
    return f"scale=-2:{target_h}:flags=lanczos", True


async def run_tool_compress(
    job_id: str,
    in_path: Path,
    resolution: str = "original",
    level: str = "high",
    user_id: str = "local",
):
    """Re-encode a video smaller — for email, chat apps and upload limits.

    Single-pass CRF rather than two-pass bitrate targeting: CRF holds a
    consistent *quality* across the whole clip, which is what "make this
    smaller without it looking bad" actually means. Two-pass only wins when
    you must hit an exact byte budget, and we don't promise one — quoting a
    predicted file size we can't honour on a high-motion clip would be worse
    than quoting nothing.
    """
    from backend.api.tools import tool_out_path
    from backend.core.ws_manager import ws_manager
    from backend.services.video_utils import probe_media

    logger.info(
        "TASK START tool:compress | job=%s res=%s level=%s",
        job_id[:8], resolution, level,
    )
    cleanup = [in_path]
    try:
        in_bytes = in_path.stat().st_size if in_path.exists() else 0
        src_w, src_h, _dur = await asyncio.to_thread(probe_media, in_path)
        crf, audio_bitrate = compress_settings(level)
        vf, downscaled = compress_scale_filter(src_w, src_h, resolution)

        if resolution != "original" and not downscaled and src_h > 0:
            await ws_manager.send_constraint_warning(
                constraint="compress_upscale", severity="info",
                message=(
                    f"This video is already {src_w}×{src_h} — kept its size "
                    f"rather than scaling up to {resolution}, which would have "
                    "made the file bigger."
                ),
                user_id=user_id,
            )

        out_path = tool_out_path(job_id, ".mp4")

        def _encode():
            cmd = ["ffmpeg", "-y", "-i", str(in_path)]
            if vf:
                cmd += ["-vf", vf]
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", audio_bitrate,
                # Puts the moov atom up front so the result streams/scrubs
                # immediately instead of needing a full download first.
                "-movflags", "+faststart",
                str(out_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if res.returncode != 0:
                raise RuntimeError(f"Compression failed: {res.stderr[-400:]}")

        await _tool_progress(job_id, 25, "Compressing...", user_id)
        await asyncio.to_thread(_encode)

        out_bytes = out_path.stat().st_size if out_path.exists() else 0
        # An already-compressed source can come out BIGGER. That's a real
        # outcome, not a failure — report both numbers and let the user judge
        # rather than silently handing back a worse file or erroring on work
        # that did exactly what was asked.
        if out_bytes and in_bytes and out_bytes >= in_bytes:
            await ws_manager.send_constraint_warning(
                constraint="compress_no_gain", severity="warning",
                message=(
                    "This video was already well compressed — the result "
                    f"({out_bytes // 1024} KB) isn't smaller than the original "
                    f"({in_bytes // 1024} KB). Try a lower resolution or a "
                    "stronger level."
                ),
                user_id=user_id,
            )

        saved_pct = (
            round((1 - out_bytes / in_bytes) * 100, 1)
            if in_bytes and out_bytes else 0.0
        )
        await _tool_success(
            job_id, out_path, cleanup, user_id,
            extra_output={
                "input_bytes": in_bytes,
                "output_bytes": out_bytes,
                "saved_pct": saved_pct,
                "resolution": resolution,
                "scaled": downscaled,
                "level": level,
            },
        )
        logger.info(
            "TASK DONE  tool:compress | job=%s %d→%d bytes (%.1f%%)",
            job_id[:8], in_bytes, out_bytes, saved_pct,
        )
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "compress")


# ── Video cropper (free, local ffmpeg) ─────────────────────────────────────


# Below this many pixels on either axis there's no video left to speak of, and
# libx264 starts refusing sizes outright. Rejecting here gives a sentence the
# user can act on instead of an ffmpeg stderr dump.
CROP_MIN_PIXELS = 16


def crop_rect(
    src_w: int, src_h: int, x: float, y: float, w: float, h: float,
) -> tuple[int, int, int, int]:
    """Normalized crop box (0-1, origin top-left) → ffmpeg's (w, h, x, y) px.

    Pure, so the arithmetic that decides what the user actually gets is
    testable without ffmpeg.

    Three things happen here that all matter:

    * **Clamp into the frame.** A normalized box is user input arriving over
      HTTP. An out-of-range crop is an ffmpeg error, not a message anyone can
      read, so the box is pinned inside the source rather than trusted.
    * **Round every value to even.** libx264 rejects odd dimensions outright,
      and with yuv420p chroma an odd OFFSET shifts colour against luma — so
      offsets are rounded too, not just sizes.
    * **Re-clamp after rounding.** Rounding a size up can push `x + w` one
      pixel past the edge; ffmpeg fails on that, and it only shows up on
      certain source sizes, which is the worst kind of bug to ship.

    Raises ValueError when the box is too small to be a video.
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError("Couldn't read this video's dimensions to crop it.")

    x = min(max(float(x), 0.0), 1.0)
    y = min(max(float(y), 0.0), 1.0)
    w = min(max(float(w), 0.0), 1.0 - x)
    h = min(max(float(h), 0.0), 1.0 - y)

    cx = int(round(src_w * x)) & ~1
    cy = int(round(src_h * y)) & ~1
    cw = int(round(src_w * w)) & ~1
    ch = int(round(src_h * h)) & ~1

    # Rounding can overshoot the edge — pull back inside, then re-even.
    cw = min(cw, src_w - cx) & ~1
    ch = min(ch, src_h - cy) & ~1

    if cw < CROP_MIN_PIXELS or ch < CROP_MIN_PIXELS:
        raise ValueError(
            "That crop area is too small — drag a larger box over the frame."
        )
    return cw, ch, cx, cy


async def run_tool_crop(
    job_id: str,
    in_path: Path,
    x: float, y: float, w: float, h: float,
    user_id: str = "local",
):
    """Crop a video to a rectangle. Local FFmpeg only.

    The manual counterpart to Reframe: Reframe decides the framing for you;
    this one does exactly what you drew and nothing else. Audio is
    stream-copied — cropping is a video-only operation and re-encoding the
    track would cost quality for no reason.
    """
    from backend.api.tools import tool_out_path
    from backend.services.video_utils import probe_media

    logger.info("TASK START tool:crop | job=%s box=%.3f,%.3f,%.3f,%.3f",
                job_id[:8], x, y, w, h)
    cleanup = [in_path]
    try:
        src_w, src_h, _dur = await asyncio.to_thread(probe_media, in_path)
        cw, ch, cx, cy = crop_rect(src_w, src_h, x, y, w, h)

        out_path = tool_out_path(job_id, ".mp4")

        def _encode():
            cmd = [
                "ffmpeg", "-y", "-i", str(in_path),
                "-vf", f"crop={cw}:{ch}:{cx}:{cy}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                # Copy, don't re-encode: a crop doesn't touch the audio.
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(out_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if res.returncode != 0:
                raise RuntimeError(f"Crop failed: {res.stderr[-400:]}")

        await _tool_progress(job_id, 30, "Cropping...", user_id)
        await asyncio.to_thread(_encode)

        await _tool_success(
            job_id, out_path, cleanup, user_id,
            extra_output={
                "source_width": src_w, "source_height": src_h,
                "width": cw, "height": ch, "x": cx, "y": cy,
            },
        )
        logger.info("TASK DONE  tool:crop | job=%s %dx%d → %dx%d at %d,%d",
                    job_id[:8], src_w, src_h, cw, ch, cx, cy)
    except Exception as e:
        await _tool_fail(job_id, e, cleanup, user_id, "crop")
