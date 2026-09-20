# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Sound effects auto-placement service.
Detects emphasis moments from word timestamps and inserts contextual SFX.
Uses FFmpeg to mix SFX into the audio track at precise timestamps.
"""
import asyncio
import logging
import re
import subprocess
from enum import Enum
from pathlib import Path

from backend.config import settings
from backend.core.exceptions import VideoGenerationError
from backend.services.ffmpeg_service import ffmpeg_error

logger = logging.getLogger(__name__)

SFX_DIR = settings.STORAGE_ROOT / "sfx"

# Hard ceiling on how many effects one mix can lay down. Shared with the
# per-style caps below so the two can't drift: the mixer used to stop at 15
# while "heavy" planned up to 25, so a heavy run silently dropped a third of
# its effects and still reported the planned count back to the user.
MAX_MIXED_SFX = 25

# Per-style density caps and spacing. Named constants because the planner and
# the mixer must agree on them.
STYLE_MAX_SFX = {"minimal": 5, "moderate": 12, "heavy": 25}
STYLE_MIN_INTERVAL_S = {"minimal": 10.0, "moderate": 5.0, "heavy": 3.0}


class SFXType(str, Enum):
    WHOOSH = "whoosh"
    DING = "ding"
    BASS_DROP = "bass_drop"
    POP = "pop"
    SWOOSH = "swoosh"
    NOTIFICATION = "notification"


# Keywords that trigger specific SFX
SFX_TRIGGER_KEYWORDS = {
    # Numbers / stats → ding
    "percent": SFXType.DING,
    "number": SFXType.DING,
    "million": SFXType.DING,
    "billion": SFXType.DING,
    "thousand": SFXType.DING,
    "dollars": SFXType.DING,
    "first": SFXType.DING,
    "second": SFXType.DING,
    "third": SFXType.DING,
    # Emphasis / surprise → pop
    "amazing": SFXType.POP,
    "incredible": SFXType.POP,
    "secret": SFXType.POP,
    "important": SFXType.POP,
    "shocking": SFXType.POP,
    "crazy": SFXType.POP,
    "wow": SFXType.POP,
    "free": SFXType.POP,
    "best": SFXType.POP,
    "worst": SFXType.POP,
    # Transitions → whoosh
    "but": SFXType.WHOOSH,
    "however": SFXType.WHOOSH,
    "instead": SFXType.WHOOSH,
    "actually": SFXType.WHOOSH,
    "finally": SFXType.WHOOSH,
    # Alerts → notification
    "warning": SFXType.NOTIFICATION,
    "tip": SFXType.NOTIFICATION,
    "remember": SFXType.NOTIFICATION,
    "listen": SFXType.NOTIFICATION,
    "stop": SFXType.NOTIFICATION,
}


def _get_sfx_path(sfx_type: SFXType) -> Path | None:
    """Get path to the SFX audio file. Returns None if not found."""
    path = SFX_DIR / f"{sfx_type.value}.mp3"
    if path.exists():
        return path
    # Try wav fallback
    path = SFX_DIR / f"{sfx_type.value}.wav"
    if path.exists():
        return path
    return None


async def auto_place_sfx(
    word_timestamps: list[dict],
    clip_boundaries: list[float] | None = None,
    style: str = "moderate",
) -> list[dict]:
    """
    Analyze word timestamps and decide where to place sound effects.

    Args:
        word_timestamps: [{"text": "word", "start": 1.0, "end": 1.5}, ...]
        clip_boundaries: timestamps where video clips change (for whoosh SFX)
        style: "none" | "minimal" | "moderate" | "heavy"

    Returns:
        [{"timestamp": 3.5, "sfx_type": "ding", "volume_db": -10}, ...]
    """
    if style == "none" or not word_timestamps:
        return []

    # Ensure SFX directory and files exist before attempting placement
    if not SFX_DIR.exists():
        try:
            ensure_sfx_dir()
        except Exception as e:
            logger.warning(f"SFX dir creation failed, skipping SFX: {e}")
            return []

    # Min interval between SFX to avoid overlap
    min_interval = STYLE_MIN_INTERVAL_S.get(style, 5.0)
    max_sfx = min(STYLE_MAX_SFX.get(style, 12), MAX_MIXED_SFX)

    placements = []
    last_sfx_time = -min_interval  # allow first SFX immediately

    # 1. Place whoosh at clip boundaries (transitions)
    if clip_boundaries:
        for boundary in clip_boundaries:
            if boundary > 0.5 and boundary - last_sfx_time >= min_interval:
                sfx_path = _get_sfx_path(SFXType.WHOOSH)
                if sfx_path:
                    placements.append({
                        "timestamp": boundary,
                        "sfx_type": SFXType.WHOOSH.value,
                        "sfx_path": str(sfx_path),
                        "volume_db": -10,
                    })
                    last_sfx_time = boundary

    # 2. Scan words for trigger keywords
    for w in word_timestamps:
        if len(placements) >= max_sfx:
            break

        # Guard: skip words with missing/invalid timestamps
        try:
            w_start = float(w.get("start", -1))
            w_end = float(w.get("end", -1))
        except (TypeError, ValueError):
            continue
        if w_start < 0 or w_end < 0 or not w.get("text"):
            continue

        text_lower = re.sub(r"[^a-z]", "", w["text"].lower())
        sfx_type = SFX_TRIGGER_KEYWORDS.get(text_lower)

        if sfx_type and w_start - last_sfx_time >= min_interval:
            sfx_path = _get_sfx_path(sfx_type)
            if sfx_path:
                placements.append({
                    "timestamp": w["start"],
                    "sfx_type": sfx_type.value,
                    "sfx_path": str(sfx_path),
                    "volume_db": -10,
                })
                last_sfx_time = w["start"]

    # 3. Detect pauses (gaps > 0.8s between words) → bass drop for drama
    if style in ("moderate", "heavy"):
        for i in range(1, len(word_timestamps)):
            if len(placements) >= max_sfx:
                break
            try:
                curr_start = float(word_timestamps[i].get("start", 0))
                prev_end = float(word_timestamps[i - 1].get("end", 0))
            except (TypeError, ValueError):
                continue
            gap = curr_start - prev_end
            if gap > 0.8 and curr_start - last_sfx_time >= min_interval:
                sfx_path = _get_sfx_path(SFXType.BASS_DROP)
                if sfx_path:
                    placements.append({
                        "timestamp": word_timestamps[i]["start"] - 0.2,  # slightly before resume
                        "sfx_type": SFXType.BASS_DROP.value,
                        "sfx_path": str(sfx_path),
                        "volume_db": -12,
                    })
                    last_sfx_time = word_timestamps[i]["start"]

    # Sort by timestamp
    placements.sort(key=lambda p: p["timestamp"])
    logger.info(f"SFX auto-placement: {len(placements)} effects (style={style})")
    return placements


async def mix_sfx_into_audio(
    audio_path: Path,
    sfx_placements: list[dict],
    output_path: Path = None,
) -> Path:
    """
    Mix SFX into audio track using FFmpeg adelay + amix filters.

    Each SFX is delayed to its placement timestamp, volume-adjusted,
    then mixed with the main audio track.
    """
    if not sfx_placements:
        return audio_path

    if output_path is None:
        output_path = audio_path.parent / f"{audio_path.stem}_sfx.mp3"

    def _mix():
        # Build FFmpeg command with multiple SFX inputs. MAX_MIXED_SFX bounds
        # the filtergraph; it matches the densest style's cap so "heavy" is
        # honoured rather than quietly truncated.
        placements = sfx_placements[:MAX_MIXED_SFX]

        inputs = ["-i", str(audio_path)]
        filter_parts = []

        actual_idx = 0  # track real input index (some may be skipped)
        for i, p in enumerate(placements):
            sfx_path = p.get("sfx_path")
            if not sfx_path or not Path(sfx_path).exists():
                continue

            # Guard: negative or absurd timestamps
            try:
                ts = float(p.get("timestamp", -1))
            except (TypeError, ValueError):
                continue
            if ts < 0:
                continue

            actual_idx += 1
            input_idx = actual_idx
            inputs.extend(["-i", sfx_path])

            delay_ms = max(int(ts * 1000), 0)
            vol_db = p.get("volume_db", -10)

            # Delay and volume-adjust each SFX
            sfx_label = f"sfx{len(filter_parts)}"
            filter_parts.append(
                f"[{input_idx}:a]adelay={delay_ms}|{delay_ms},volume={vol_db}dB[{sfx_label}]"
            )

        if not filter_parts:
            return audio_path

        # Mix all SFX with the main audio.
        # normalize=0 is LOAD-BEARING: amix defaults to normalize=1, which
        # divides the output by the number of *currently-active* inputs. The
        # SFX are short clips adelay'd across the timeline, so at the start ALL
        # of them are still "active" (pre-roll silence, not yet at EOF) and the
        # voice gets scaled by ~1/n_inputs; as each SFX hits EOF the divisor
        # drops and the voice swells progressively LOUDER toward the end (a
        # ~20 dB monotonic ramp with ~15 SFX). normalize=0 sums instead, so the
        # voice stays at full level and the (already volume-trimmed) SFX overlay
        # on top; alimiter then catches any summed peak so the sum can't clip.
        sfx_labels = "".join(f"[sfx{i}]" for i in range(len(filter_parts)))
        n_inputs = len(filter_parts) + 1
        filter_complex = ";".join(filter_parts) + (
            f";[0:a]{sfx_labels}amix=inputs={n_inputs}:duration=first:"
            f"dropout_transition=2:normalize=0[mixraw];[mixraw]alimiter=limit=0.95[out]"
        )

        cmd = (
            ["ffmpeg", "-y"]
            + inputs
            + ["-filter_complex", filter_complex]
            + ["-map", "[out]"]
            + ["-c:a", "libmp3lame", "-q:a", "2"]
            + [str(output_path)]
        )

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.warning(f"SFX mixing failed, returning original: {ffmpeg_error(result.stderr, 400)}")
            return audio_path
        return output_path

    return await asyncio.to_thread(_mix)


def ensure_sfx_dir():
    """Create the SFX directory and generate basic SFX files using FFmpeg."""
    SFX_DIR.mkdir(parents=True, exist_ok=True)

    # Generate basic SFX using FFmpeg's audio synthesis if files don't exist
    sfx_specs = {
        "ding": "sine=frequency=880:duration=0.3,afade=t=out:st=0.1:d=0.2",
        "pop": "sine=frequency=1200:duration=0.15,afade=t=out:st=0.05:d=0.1",
        "whoosh": "anoisesrc=d=0.4:c=pink,bandpass=f=2000:w=1000,afade=t=in:d=0.1,afade=t=out:st=0.2:d=0.2",
        "bass_drop": "sine=frequency=80:duration=0.5,afade=t=out:st=0.1:d=0.4",
        "swoosh": "anoisesrc=d=0.3:c=pink,bandpass=f=3000:w=2000,afade=t=in:d=0.05,afade=t=out:st=0.15:d=0.15",
        "notification": "sine=frequency=660:duration=0.15,sine=frequency=880:duration=0.15,afade=t=out:st=0.2:d=0.1",
    }

    for name, filter_expr in sfx_specs.items():
        path = SFX_DIR / f"{name}.mp3"
        if path.exists():
            continue

        # For notification (two tones), need a different approach
        if name == "notification":
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "sine=frequency=660:duration=0.15",
                "-f", "lavfi", "-i", "sine=frequency=880:duration=0.15",
                "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1,afade=t=out:st=0.2:d=0.1[out]",
                "-map", "[out]",
                str(path),
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", filter_expr,
                str(path),
            ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                logger.info(f"Generated SFX: {path}")
            else:
                logger.warning(f"Failed to generate SFX {name}: {ffmpeg_error(result.stderr, 200)}")
        except Exception as e:
            logger.warning(f"SFX generation failed for {name}: {e}")
