# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""FFmpeg stitching, captions, and thumbnail generation."""
import asyncio
import logging
import random
import subprocess
from pathlib import Path
from uuid import uuid4
from backend.config import settings
from backend.core.exceptions import VideoGenerationError
from backend.services.video_utils import probe_duration


def _tmp(name: str) -> Path:
    """Return a unique temp file path to prevent collisions between concurrent jobs."""
    return settings.TMP_DIR / f"{uuid4().hex[:8]}_{name}"

logger = logging.getLogger(__name__)


TRANSITIONS = [
    "fade", "fadeblack", "fadewhite", "wipeleft", "wiperight",
    "wipeup", "wipedown", "slideleft", "slideright", "slideup",
    "slidedown", "dissolve", "smoothleft", "smoothright",
]


async def stitch_clips(
    clip_paths: list[Path],
    output_path: Path = None,
    transition: str = "random",
    transition_duration: float = 0.7,
) -> Path:
    """
    Concatenate multiple video clips with smooth transitions using FFmpeg xfade.

    transition: "random" picks a random effect per cut, or specify one from TRANSITIONS.
                "none" uses simple concat (fastest, no re-encoding).
    transition_duration: seconds for each transition (0.5-1.0 works well).
    """
    if not clip_paths:
        raise VideoGenerationError("No clips to stitch")

    if len(clip_paths) == 1:
        return clip_paths[0]

    if output_path is None:
        output_path = settings.GENERATED_DIR / "stitched.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if transition == "none":
        return await _stitch_concat(clip_paths, output_path)

    return await _stitch_xfade(clip_paths, output_path, transition, transition_duration)


async def _stitch_concat(clip_paths: list[Path], output_path: Path) -> Path:
    """Simple concat without transitions (fastest, no re-encoding)."""
    def _run():
        concat_file = _tmp("concat.txt")
        concat_file.parent.mkdir(parents=True, exist_ok=True)
        with open(concat_file, "w") as f:
            for clip in clip_paths:
                f.write(f"file '{clip.resolve()}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        concat_file.unlink(missing_ok=True)
        if result.returncode != 0:
            raise VideoGenerationError(f"FFmpeg stitch failed: {result.stderr[:500]}")
        return output_path

    return await asyncio.to_thread(_run)


async def _stitch_xfade(
    clip_paths: list[Path],
    output_path: Path,
    transition: str,
    transition_duration: float,
) -> Path:
    """Stitch clips with xfade transitions between them."""
    def _run():
        # Probe each clip's duration
        durations = [probe_duration(clip, default=5.0) for clip in clip_paths]

        td = transition_duration

        # Build inputs
        inputs = []
        for clip in clip_paths:
            inputs.extend(["-i", str(clip)])

        # Build xfade filter chain
        # Each xfade offset = cumulative duration of all previous clips minus
        # cumulative transition durations already applied
        filter_parts = []
        prev_label = "0:v"
        cumulative_offset = 0.0

        for i in range(1, len(clip_paths)):
            # Pick transition effect
            if transition == "random":
                effect = random.choice(TRANSITIONS)
            else:
                effect = transition if transition in TRANSITIONS else "fade"

            cumulative_offset += durations[i - 1] - td
            # Ensure offset is positive
            offset = max(cumulative_offset, 0.1)
            out_label = f"v{i}" if i < len(clip_paths) - 1 else "outv"

            filter_parts.append(
                f"[{prev_label}][{i}:v]xfade=transition={effect}:duration={td}:offset={offset:.3f}[{out_label}]"
            )
            prev_label = out_label

        cmd = (
            ["ffmpeg", "-y"]
            + inputs
            + ["-filter_complex", ";".join(filter_parts)]
            + ["-map", "[outv]"]
            + ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p"]
            + [str(output_path)]
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.warning(f"xfade stitch failed, falling back to concat: {result.stderr[:400]}")
            # Fallback to simple concat
            concat_file = _tmp("concat.txt")
            concat_file.parent.mkdir(parents=True, exist_ok=True)
            with open(concat_file, "w") as f:
                for clip in clip_paths:
                    f.write(f"file '{clip.resolve()}'\n")
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
                "-c", "copy",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            concat_file.unlink(missing_ok=True)
            if result.returncode != 0:
                raise VideoGenerationError(f"FFmpeg stitch failed: {result.stderr[:500]}")
        return output_path

    return await asyncio.to_thread(_run)


async def add_audio_to_video(
    video_path: Path,
    audio_path: Path,
    output_path: Path = None,
) -> Path:
    """Merge audio track onto a video."""
    if output_path is None:
        output_path = video_path.parent / f"{video_path.stem}_with_audio.mp4"

    def _merge():
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise VideoGenerationError(f"FFmpeg audio merge failed: {result.stderr[:500]}")
        return output_path

    return await asyncio.to_thread(_merge)


async def add_captions(
    video_path: Path,
    segments: list[dict],
    output_path: Path = None,
    font_size: int = 24,
) -> Path:
    """Burn captions/subtitles into a video using FFmpeg + ASS subtitles."""
    if output_path is None:
        output_path = video_path.parent / f"{video_path.stem}_captioned.mp4"

    def _caption():
        # Generate SRT file
        srt_path = _tmp("captions.srt")
        srt_path.parent.mkdir(parents=True, exist_ok=True)

        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segments, 1):
                start = _format_srt_time(seg["start"])
                end = _format_srt_time(seg["end"])
                f.write(f"{i}\n{start} --> {end}\n{seg['text']}\n\n")

        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vf", f"subtitles={srt_path}:force_style='FontSize={font_size},PrimaryColour=&Hffffff&,OutlineColour=&H000000&,Outline=2,Alignment=2'",
            "-c:a", "copy",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.warning(f"FFmpeg captioning failed: {result.stderr[:300]}")
            # Return original video without captions rather than failing
            return video_path

        srt_path.unlink(missing_ok=True)
        return output_path

    return await asyncio.to_thread(_caption)


async def extract_thumbnail(
    video_path: Path,
    output_path: Path = None,
    timestamp: float = 2.0,
) -> Path:
    """Extract a thumbnail frame from a video."""
    if output_path is None:
        output_path = settings.THUMBNAILS_DIR / f"{video_path.stem}_thumb.jpg"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _extract():
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-ss", str(timestamp),
            "-vframes", "1",
            "-q:v", "2",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.warning(f"Thumbnail extraction failed: {result.stderr[:200]}")
            return None
        return output_path

    return await asyncio.to_thread(_extract)


async def extract_clip(
    video_path: Path,
    start: float,
    end: float,
    output_path: Path,
    vertical: bool = True,
) -> Path:
    """Extract a segment from a video. Converts to 9:16 with blur-fill if source is landscape."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start

    def _run():
        # Probe source dimensions
        is_landscape = False
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", str(video_path)],
                capture_output=True, text=True, timeout=10,
            )
            w, h = map(int, probe.stdout.strip().split(","))
            is_landscape = w > h
        except Exception as e:
            logger.debug(f"Could not probe video dimensions: {e}")

        if vertical and is_landscape:
            vf = (
                "split[original][bg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,boxblur=20:5[blurred];"
                "[original]scale=1080:1920:force_original_aspect_ratio=decrease[scaled];"
                "[blurred][scaled]overlay=(W-w)/2:(H-h)/2"
            )
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", str(video_path),
                "-t", str(duration),
                "-filter_complex", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                str(output_path),
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", str(video_path),
                "-t", str(duration),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                str(output_path),
            ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise VideoGenerationError(f"Clip extraction timed out after 10 minutes (start={start:.1f}s, end={end:.1f}s)")
        if result.returncode != 0:
            raise VideoGenerationError(f"Clip extraction failed: {result.stderr[:500]}")
        if not output_path.exists() or output_path.stat().st_size < 1000:
            raise VideoGenerationError(f"Clip extraction produced empty or invalid file: {output_path}")
        return output_path

    return await asyncio.to_thread(_run)


async def generate_text_video(
    script: str,
    audio_path: Path = None,
    output_path: Path = None,
    aspect_ratio: str = "9:16",
    duration: int = 60,
) -> Path:
    """
    Fallback video generator: creates a text-on-dark-background video.
    Uses Pillow to render text frames as PNG images, then encodes with ffmpeg.
    Works without any external API keys — just needs ffmpeg + Pillow.
    """
    if output_path is None:
        output_path = settings.GENERATED_DIR / f"text_video_{hash(script) & 0xFFFFFFFF:08x}.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _generate():
        from PIL import Image, ImageDraw, ImageFont
        import textwrap

        if aspect_ratio == "9:16":
            width, height = 1080, 1920
            fontsize = 42
            wrap_width = 28
        else:
            width, height = 1920, 1080
            fontsize = 36
            wrap_width = 50

        # Get audio duration if available
        vid_duration = duration
        if audio_path and audio_path.exists():
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
                    capture_output=True, text=True, timeout=10,
                )
                vid_duration = int(float(probe.stdout.strip())) + 1
            except Exception as e:
                logger.debug(f"Could not probe audio duration: {e}")

        # Try to load a nice font, fall back to default
        font = None
        for font_path in [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/SFNSText.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]:
            try:
                font = ImageFont.truetype(font_path, fontsize)
                break
            except (IOError, OSError):
                continue
        if font is None:
            font = ImageFont.load_default()

        # Word-wrap the script into screens
        wrapped = textwrap.fill(script, width=wrap_width)
        all_lines = wrapped.split("\n")

        # Group lines into screens (5 lines each)
        lines_per_screen = 5
        screens = []
        for i in range(0, len(all_lines), lines_per_screen):
            screens.append("\n".join(all_lines[i:i + lines_per_screen]))
        if not screens:
            screens = ["(no script)"]

        secs_per_screen = max(vid_duration // len(screens), 3)

        # Generate one PNG per screen, then encode each as a clip
        bg_color = (17, 24, 39)  # #111827 dark blue-gray
        text_color = (255, 255, 255)
        tmp_clips = []

        for idx, text in enumerate(screens):
            # Render text onto image
            img = Image.new("RGB", (width, height), bg_color)
            draw = ImageDraw.Draw(img)
            bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=16)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            x = (width - text_w) // 2
            y = (height - text_h) // 2
            draw.multiline_text((x, y), text, fill=text_color, font=font, spacing=16)

            # Save as PNG
            img_path = _tmp(f"textframe_{idx:03d}.png")
            img.save(str(img_path))

            # Encode PNG as video clip with ffmpeg
            clip_path = _tmp(f"textclip_{idx:03d}.mp4")
            clip_dur = secs_per_screen if idx < len(screens) - 1 else max(vid_duration - secs_per_screen * idx, 2)

            cmd = [
                "ffmpeg", "-y",
                "-loop", "1",
                "-i", str(img_path),
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-t", str(clip_dur),
                "-r", "24",
                str(clip_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and clip_path.exists():
                tmp_clips.append(clip_path)
            else:
                logger.warning(f"Text clip {idx} failed: {result.stderr[:300]}")
            img_path.unlink(missing_ok=True)

        if not tmp_clips:
            raise VideoGenerationError("Failed to generate text video clips")

        # Concat all clips
        concat_file = _tmp("text_concat.txt")
        with open(concat_file, "w") as f:
            for clip in tmp_clips:
                f.write(f"file '{clip.resolve()}'\n")

        video_only = _tmp("text_video_noaudio.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(video_only),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise VideoGenerationError(f"FFmpeg concat failed: {result.stderr[:300]}")

        # Merge audio if available
        if audio_path and audio_path.exists():
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_only),
                "-i", str(audio_path),
                "-c:v", "copy", "-c:a", "aac",
                "-shortest",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                video_only.unlink(missing_ok=True)
                for clip in tmp_clips:
                    clip.unlink(missing_ok=True)
                concat_file.unlink(missing_ok=True)
                return output_path

        # No audio or merge failed — use video only
        import shutil
        shutil.move(str(video_only), str(output_path))
        for clip in tmp_clips:
            clip.unlink(missing_ok=True)
        concat_file.unlink(missing_ok=True)
        return output_path

    return await asyncio.to_thread(_generate)


async def generate_kenburns_video(
    image_paths: list[Path],
    audio_path: Path = None,
    output_path: Path = None,
    aspect_ratio: str = "9:16",
    duration_per_image: int = 5,
) -> Path:
    """
    Create a video from one or more images with Ken Burns effects (zoom, pan).
    Used for the free Stock Footage tier image-to-video mode.

    Effects applied randomly per image:
    - zoom_in: centered zoom from 1.0x to 1.5x
    - zoom_out: centered zoom from 1.5x to 1.0x
    - pan_left: slow pan from right to left at 1.3x zoom
    - pan_right: slow pan from left to right at 1.3x zoom
    - pan_up: slow pan from bottom to top at 1.3x zoom

    If audio_path is provided, total duration is matched to audio length and
    images are distributed evenly across it.
    """
    if not image_paths:
        raise VideoGenerationError("No images provided for Ken Burns video")

    if output_path is None:
        output_path = settings.GENERATED_DIR / "kenburns_video.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if aspect_ratio == "9:16":
        out_w, out_h = 1080, 1920
    else:
        out_w, out_h = 1920, 1080

    fps = 30

    def _generate():
        # Determine total duration
        total_duration = len(image_paths) * duration_per_image
        if audio_path and audio_path.exists():
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
                    capture_output=True, text=True, timeout=10,
                )
                total_duration = int(float(probe.stdout.strip())) + 1
            except Exception as e:
                logger.debug(f"Could not probe audio duration for Ken Burns: {e}")

        per_image = max(total_duration // len(image_paths), 3)
        frames_per_image = per_image * fps

        effects = ["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up"]
        tmp_clips = []

        for idx, img_path in enumerate(image_paths):
            effect = random.choice(effects)
            clip_path = _tmp(f"kb_clip_{idx:03d}.mp4")

            # zoompan filter expressions (d = total frames for this image)
            d = frames_per_image
            if effect == "zoom_in":
                zp = f"z='1+0.5*on/{d}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            elif effect == "zoom_out":
                zp = f"z='1.5-0.5*on/{d}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            elif effect == "pan_left":
                zp = f"z='1.3':x='(iw/1.3-{out_w})*(1-on/{d})':y='ih/2-(ih/zoom/2)'"
            elif effect == "pan_right":
                zp = f"z='1.3':x='(iw/1.3-{out_w})*on/{d}':y='ih/2-(ih/zoom/2)'"
            else:  # pan_up
                zp = f"z='1.3':x='iw/2-(iw/zoom/2)':y='(ih/1.3-{out_h})*(1-on/{d})'"

            # Scale source image to high res for zoompan, then apply effect
            cmd = [
                "ffmpeg", "-y",
                "-i", str(img_path),
                "-vf", (
                    f"scale=8000:-1,"
                    f"zoompan={zp}:d={d}:s={out_w}x{out_h}:fps={fps},"
                    f"format=yuv420p"
                ),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-t", str(per_image),
                str(clip_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and clip_path.exists():
                tmp_clips.append(clip_path)
            else:
                logger.warning(f"Ken Burns clip {idx} failed: {result.stderr[:300]}")

        if not tmp_clips:
            raise VideoGenerationError("All Ken Burns clips failed to generate")

        # Stitch clips together
        if len(tmp_clips) == 1:
            final_video = tmp_clips[0]
        else:
            # Use xfade for crossfade transitions between clips
            xfade_duration = 0.5
            inputs = []
            filter_parts = []
            for i, clip in enumerate(tmp_clips):
                inputs.extend(["-i", str(clip)])

            # Build xfade filter chain
            if len(tmp_clips) == 2:
                offset = per_image - xfade_duration
                filter_parts.append(
                    f"[0:v][1:v]xfade=transition=fade:duration={xfade_duration}:offset={offset}[outv]"
                )
                map_label = "[outv]"
            else:
                # Chain xfades for 3+ clips
                prev = "0:v"
                for i in range(1, len(tmp_clips)):
                    offset = per_image * i - xfade_duration * i
                    out_label = f"v{i}" if i < len(tmp_clips) - 1 else "outv"
                    filter_parts.append(
                        f"[{prev}][{i}:v]xfade=transition=fade:duration={xfade_duration}:offset={offset}[{out_label}]"
                    )
                    prev = out_label
                map_label = "[outv]"

            final_video = _tmp("kb_stitched.mp4")
            cmd = (
                ["ffmpeg", "-y"]
                + inputs
                + ["-filter_complex", ";".join(filter_parts)]
                + ["-map", map_label]
                + ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]
                + [str(final_video)]
            )
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                # Fallback: simple concat without crossfade
                logger.warning(f"xfade stitch failed, falling back to concat: {result.stderr[:300]}")
                concat_file = _tmp("kb_concat.txt")
                with open(concat_file, "w") as f:
                    for clip in tmp_clips:
                        f.write(f"file '{clip.resolve()}'\n")
                final_video = _tmp("kb_stitched.mp4")
                cmd = [
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", str(concat_file),
                    "-c", "copy",
                    str(final_video),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    raise VideoGenerationError(f"Ken Burns stitch failed: {result.stderr[:300]}")
                concat_file.unlink(missing_ok=True)

        # Merge audio if provided
        if audio_path and audio_path.exists():
            cmd = [
                "ffmpeg", "-y",
                "-i", str(final_video),
                "-i", str(audio_path),
                "-c:v", "copy", "-c:a", "aac",
                "-shortest",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                # Clean up temp clips
                for clip in tmp_clips:
                    clip.unlink(missing_ok=True)
                if final_video != output_path:
                    final_video.unlink(missing_ok=True)
                return output_path

        # No audio or merge failed — move video to output
        import shutil
        shutil.move(str(final_video), str(output_path))
        for clip in tmp_clips:
            clip.unlink(missing_ok=True)
        return output_path

    return await asyncio.to_thread(_generate)


async def apply_auto_zoom(
    video_path: Path,
    word_timestamps: list[dict],
    output_path: Path = None,
    zoom_factor: float = 1.15,
    words_per_group: int = 3,
) -> Path:
    """
    Apply subtle zoom pulses on highlighted caption words using FFmpeg zoompan.

    Each word group triggers a smooth zoom-in then zoom-out, creating a
    "pop" effect that draws attention to the currently spoken text.

    Args:
        video_path: Input video (should already have captions burned in).
        word_timestamps: List of {"text", "start", "end"} from Whisper.
        zoom_factor: Max zoom level (1.15 = 15% zoom). Keep subtle.
        words_per_group: Group N words per zoom pulse (matches caption grouping).

    Returns:
        Path to the zoomed video.
    """
    if not word_timestamps:
        return video_path

    # Clamp zoom_factor to safe range
    zoom_factor = max(1.01, min(zoom_factor, 1.5))

    if output_path is None:
        output_path = video_path.parent / f"{video_path.stem}_zoomed.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _run():
        # Probe video dimensions and fps
        probe_cmd = [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "csv=p=0", str(video_path),
        ]
        try:
            probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("ffprobe timed out for auto-zoom, returning original")
            return video_path
        try:
            parts = probe.stdout.strip().split(",")
            vid_w, vid_h = int(parts[0]), int(parts[1])
            if vid_w <= 0 or vid_h <= 0:
                raise ValueError("Invalid dimensions")
            # Parse frame rate (e.g. "30/1" or "30000/1001")
            fps_parts = parts[2].split("/")
            fps = float(fps_parts[0]) / float(fps_parts[1]) if len(fps_parts) == 2 else float(fps_parts[0])
        except (ValueError, IndexError):
            vid_w, vid_h, fps = 1080, 1920, 30.0

        # Filter valid word timestamps (must have numeric start/end >= 0)
        valid_words = []
        for w in word_timestamps:
            try:
                s, e = float(w.get("start", -1)), float(w.get("end", -1))
                if s >= 0 and e > s:
                    valid_words.append({"text": w.get("text", ""), "start": s, "end": e})
            except (TypeError, ValueError):
                continue
        if not valid_words:
            return video_path

        # Group words into zoom events (matching caption word groups)
        zoom_events = []
        for i in range(0, len(valid_words), words_per_group):
            group = valid_words[i:i + words_per_group]
            if not group:
                continue
            start = group[0]["start"]
            end = group[-1]["end"]
            dur = end - start
            if dur < 0.05:
                continue  # skip zero/tiny-duration groups
            # Center of group is the zoom peak
            mid = (start + end) / 2
            zoom_events.append({"start": start, "mid": mid, "end": end})

        if not zoom_events:
            return video_path

        # Build zoompan-style zoom using the geq/scale approach:
        # We use a sendcmd + zoompan alternative: generate a smooth zoom expression.
        #
        # Strategy: Use a single complex expression for the zoom level based on time (t).
        # For each zoom event, contribute a smooth pulse: zoom = 1 + (factor-1) * pulse(t)
        # where pulse is a triangular or sine wave centered on mid.
        #
        # To keep the filter manageable, we use the crop+scale approach:
        # 1. Scale up the video slightly (to zoom_factor * original)
        # 2. Use crop with animated x,y,w,h to simulate zoom in/out
        #
        # Simpler approach: use setpts + zoompan on a per-frame basis.
        # But zoompan requires still images. Instead, use the crop filter with
        # time-based expressions.

        # Build a piece-wise zoom expression using 'between(t,start,end)' checks.
        # z(t) = 1 + sum_over_events[ (zoom_factor-1) * sin(pi*(t-start)/(end-start)) * between(t,start,end) ]
        # Capped to manageable number of events to avoid FFmpeg filter string limits.
        # Each event adds ~80 chars; FFmpeg has a ~10K char limit on filter expressions.
        max_events = min(40, len(zoom_events))  # 40 events × ~80 chars = ~3200 chars — safe
        events_to_use = zoom_events[:max_events]

        zf = zoom_factor - 1.0  # e.g. 0.15

        # Build the zoom expression z(t)
        zoom_parts = []
        for ev in events_to_use:
            dur = max(ev["end"] - ev["start"], 0.1)
            # Sine pulse: peaks at midpoint
            zoom_parts.append(
                f"{zf}*sin(PI*(t-{ev['start']:.3f})/{dur:.3f})*between(t,{ev['start']:.3f},{ev['end']:.3f})"
            )

        if not zoom_parts:
            return video_path

        zoom_expr = "1+" + "+".join(zoom_parts)

        # Crop dimensions: crop to (w/z, h/z) centered, then scale back to original
        # crop=w/z:h/z:(w-w/z)/2:(h-h/z)/2, scale=w:h
        crop_w = f"{vid_w}/({zoom_expr})"
        crop_h = f"{vid_h}/({zoom_expr})"
        crop_x = f"({vid_w}-{crop_w})/2"
        crop_y = f"({vid_h}-{crop_h})/2"

        vf = (
            f"crop=w={crop_w}:h={crop_h}:x={crop_x}:y={crop_y},"
            f"scale={vid_w}:{vid_h}:flags=lanczos"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "copy",
            str(output_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            logger.warning("Auto-zoom FFmpeg timed out after 600s, returning original")
            return video_path
        if result.returncode != 0:
            logger.warning(f"Auto-zoom failed, returning original: {result.stderr[:400]}")
            return video_path
        return output_path

    return await asyncio.to_thread(_run)


async def convert_aspect_ratio(
    video_path: Path,
    target_aspect: str = "16:9",
    method: str = "letterbox",
    output_path: Path = None,
) -> Path:
    """
    Convert video between aspect ratios.

    target_aspect: "16:9" or "9:16"
    method:
      - "letterbox": add black bars to fill (no content lost)
      - "crop": crop center to fill (content lost at edges)
      - "blur_fill": blurred+scaled background behind original (modern look)

    Returns path to converted video.
    """
    if output_path is None:
        stem = video_path.stem
        output_path = video_path.parent / f"{stem}_{target_aspect.replace(':', 'x')}_{method}.mp4"

    if output_path.exists():
        return output_path

    def _run():
        ASPECT_MAP = {
            "16:9": (1920, 1080),
            "9:16": (1080, 1920),
            "1:1":  (1080, 1080),
            "4:5":  (1080, 1350),
        }
        target_w, target_h = ASPECT_MAP.get(target_aspect, (1920, 1080))

        if method == "letterbox":
            # Pad with black bars — no content lost
            vf = (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black"
            )
        elif method == "crop":
            # Center crop — loses edges
            vf = (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h}"
            )
        elif method == "blur_fill":
            # Blurred scaled background + sharp original on top
            vf = (
                f"split[original][bg];"
                f"[bg]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h},boxblur=20:5[blurred];"
                f"[original]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[scaled];"
                f"[blurred][scaled]overlay=(W-w)/2:(H-h)/2"
            )
        else:
            vf = (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black"
            )

        filter_flag = "-filter_complex" if method == "blur_fill" else "-vf"
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            filter_flag, vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error(f"Aspect ratio conversion failed: {result.stderr[:500]}")
            raise VideoGenerationError(f"FFmpeg aspect conversion failed: {result.stderr[:200]}")
        return output_path

    return await asyncio.to_thread(_run)


def _format_srt_time(seconds: float) -> str:
    """Convert seconds to SRT time format (HH:MM:SS,mmm)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


async def has_audio_stream(media_path: Path) -> bool:
    """Quick ffprobe check: does this file contain any audio stream?

    Used by whisper_service to fail fast with a clear error when handed
    a video-only file (e.g. a download that missed the audio track) —
    faster-whisper's own error in that case is an opaque "tuple index
    out of range" from deep inside its decoder.
    """
    media_path = Path(media_path)
    if not media_path.exists():
        return False

    def _probe() -> bool:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet",
                 "-select_streams", "a",
                 "-show_entries", "stream=index",
                 "-of", "csv=p=0",
                 str(media_path)],
                capture_output=True, text=True, timeout=15,
            )
            return bool(result.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # On probe failure, default to "yes" so we don't block transcription
            # for files that might still work — whisper's own error path is
            # the safety net.
            return True

    return await asyncio.to_thread(_probe)
