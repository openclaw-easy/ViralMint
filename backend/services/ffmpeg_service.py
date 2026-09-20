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
from backend.services.video_utils import cover_vf, ff_filter_path, probe_dimensions, probe_duration


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
            "-vf", f"subtitles={ff_filter_path(srt_path)}:force_style='FontSize={font_size},PrimaryColour=&Hffffff&,OutlineColour=&H000000&,Outline=2,Alignment=2'",
            "-c:a", "copy",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.warning(f"FFmpeg captioning failed: {ffmpeg_error(result.stderr)}")
            # Return original video without captions rather than failing
            return video_path

        srt_path.unlink(missing_ok=True)
        return output_path

    return await asyncio.to_thread(_caption)


def ffmpeg_error(stderr: str, limit: int = 400) -> str:
    """The part of ffmpeg's stderr that says what went wrong.

    ffmpeg prints ~200 characters of version banner and build configuration
    BEFORE it says anything useful, so `stderr[:200]` logs a constant string
    and throws the error away — every thumbnail failure logged the identical
    "ffmpeg version 7.1 Copyright ... configuration: --prefix=" with no cause
    attached.

    Drops the banner and returns the tail, which is where the diagnosis lives
    ("No such file or directory", "Invalid data found when processing input",
    "moov atom not found").
    """
    if not stderr:
        return "(no stderr)"
    lines = [
        ln for ln in stderr.splitlines()
        if ln.strip() and not ln.startswith(("ffmpeg version", "  built with",
                                             "  configuration:", "  lib"))
    ]
    return "\n".join(lines)[-limit:] if lines else stderr[-limit:]


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
        # Temp-then-replace, and refuse an empty frame: this output is
        # backfilled as a row's permanent thumbnail and served from the
        # thumbnails cache, so a half-written or 0-byte JPEG at the final
        # path is a broken image the Library keeps showing.
        import uuid
        tmp = output_path.with_name(
            f"{output_path.stem}.{uuid.uuid4().hex[:8]}.tmp{output_path.suffix}")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-ss", str(timestamp),
            "-vframes", "1",
            "-q:v", "2",
            str(tmp),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.warning("Thumbnail extraction failed for %s: %s",
                               video_path.name, ffmpeg_error(result.stderr))
                return None
            if not tmp.exists() or tmp.stat().st_size == 0:
                logger.warning("Thumbnail extraction produced an empty file for %s",
                               video_path.name)
                return None
            tmp.replace(output_path)
        finally:
            tmp.unlink(missing_ok=True)
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


# Longest edge a normalized still may keep. The Ken Burns chain scales it to
# 2x the output frame anyway, so anything above this is decoded, held in
# memory and thrown away — a 108 MP camera PNG is ~400 MB of RGB for ONE frame.
MAX_STILL_DIMENSION = 4096


def _capped_still_dimensions(width: int, height: int) -> tuple[int, int]:
    """Scale (width, height) down so the longest edge fits MAX_STILL_DIMENSION,
    preserving aspect. Dimensions stay even — odd ones break some encoders."""
    longest = max(width, height)
    if longest <= MAX_STILL_DIMENSION or longest <= 0:
        out_w, out_h = width, height
    else:
        scale = MAX_STILL_DIMENSION / longest
        out_w = max(2, int(width * scale))
        out_h = max(2, int(height * scale))
    return out_w - (out_w % 2), out_h - (out_h % 2)


def _normalize_still_sync(src: Path, dst: Path) -> tuple[int, int]:
    """Write ONE opaque, bounded, single-frame RGB PNG at `dst`.

    Deliberately a single ffmpeg invocation over a black `color` source rather
    than a straight transcode, because DROPPING an alpha channel and
    COMPOSITING one are different operations and only the second is correct.
    The Ken Burns chain ends on `format=yuv420p`, which discards alpha and
    leaves whatever RGB happened to sit underneath — so a background-removed
    cut-out rendered as a garbage-fringed subject with no error anywhere.
    Probed on a white disc cut out over hidden green: the transparent field
    came back (0, 127, 0) before this pass and (0, 0, 0) after.

    Overlaying onto black is a no-op for the opaque images that are the common
    case, so every still can take the same path. It also gives us frame 0 of
    an animated GIF/WebP for free, and caps a camera-sized panorama before it
    ever reaches zoompan.

    Returns the normalized (width, height).
    """
    src, dst = Path(src), Path(dst)
    width, height = probe_dimensions(src)
    if width <= 0 or height <= 0:
        raise VideoGenerationError(
            f"Couldn't read that image: no still frame in {src.name}"
        )

    out_w, out_h = _capped_still_dimensions(width, height)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        # An infinite black canvas at the target size...
        "-f", "lavfi", "-i", f"color=c=black:s={out_w}x{out_h}",
        "-i", str(src),
        "-filter_complex",
        # ...with the (possibly transparent, possibly animated) source scaled
        # onto it. `shortest` plus `-frames:v 1` together make this frame 0 of
        # a GIF/animated WebP and the only frame of a still.
        f"[1:v]scale={out_w}:{out_h}:flags=lanczos[fg];"
        # `format=rgb` composites in RGB — blending straight onto the YUV
        # canvas tints the semi-transparent edge pixels of a cut-out.
        f"[0:v][fg]overlay=shortest=1:format=rgb,format=rgb24",
        "-frames:v", "1",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise VideoGenerationError(
            "Couldn't read that image: "
            f"{(result.stderr or '').strip()[-200:] or 'ffmpeg failed'}"
        )
    return out_w, out_h


async def normalize_still(src: Path, dst: Path) -> tuple[int, int]:
    """Async wrapper for _normalize_still_sync. See its docstring."""
    return await asyncio.to_thread(_normalize_still_sync, src, dst)


# ── Ken Burns motion grammar ───────────────────────────────────────────────

_KENBURNS_EFFECTS = ["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up"]


def _kenburns_image_vf(effect: str, frames: int, out_w: int, out_h: int, fps: int) -> str:
    """Build the FFmpeg -vf chain that turns a still image into a Ken Burns
    clip of exactly (out_w x out_h).

    ONE motion grammar, ONE place to fix it — shared by every still-to-video
    path so a fix here cannot reach one of them and miss the other.

    Two bugs this builder is the fix for:

    1. DISTORTION. The old chain scaled with `scale=8000:-1` (preserving the
       SOURCE aspect) and then leaned on zoompan's `s=WxH`. But zoompan's crop
       window keeps the INPUT aspect and `s=` anamorphically STRETCHES it to
       the output size, so any image whose aspect differs from the target
       (a square photo into a 9:16 video, a phone panorama into 16:9) came out
       visibly squeezed or elongated. Fix: cover-crop to the target aspect
       FIRST, at 2x target resolution for subpixel-smooth motion. zoompan's
       window then has the same aspect as `s=` and the scale is distortion-free.

    2. FROZEN PANS. The pan expressions used `(iw/1.3 - out_w)` as the travel
       range — mixing input-space pixels (iw) with output-space pixels (out_w).
       That range overshoots zoompan's own clamp of `iw - iw/zoom`, so a "pan"
       sat pinned against one edge for the first half of its duration and then
       lurched across. The correct travel range is `(iw - iw/zoom)`.

    2x target is also plenty of headroom for smooth zoompan; the old 8000px
    wide scale decoded and held a huge intermediate frame for no visible gain.
    """
    if effect == "zoom_in":
        zp = f"z='1+0.5*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    elif effect == "zoom_out":
        zp = f"z='1.5-0.5*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    elif effect == "pan_left":
        zp = f"z='1.3':x='(iw-iw/zoom)*(1-on/{frames})':y='ih/2-(ih/zoom/2)'"
    elif effect == "pan_right":
        zp = f"z='1.3':x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)'"
    else:  # pan_up
        zp = f"z='1.3':x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*(1-on/{frames})'"

    pre_w, pre_h = out_w * 2, out_h * 2
    return (
        f"{cover_vf(pre_w, pre_h, setsar=False)},"
        f"zoompan={zp}:d={frames}:s={out_w}x{out_h}:fps={fps},"
        f"format=yuv420p"
    )


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

        tmp_clips = []
        # Normalized copies of the source stills — scratch, deleted alongside
        # the clips below.
        tmp_stills: list[Path] = []

        for idx, img_path in enumerate(image_paths):
            effect = random.choice(_KENBURNS_EFFECTS)
            clip_path = _tmp(f"kb_clip_{idx:03d}.mp4")

            # Normalize FIRST — the chain below ends on `format=yuv420p`,
            # which discards alpha instead of compositing it, so a
            # background-removed cut-out would render as a garbage-fringed
            # subject with no error. Normalizing here rather than at the
            # caller means no still-image entry point can forget to.
            try:
                norm_path = _tmp(f"kb_norm_{idx:03d}.png")
                _normalize_still_sync(img_path, norm_path)
                tmp_stills.append(norm_path)
                img_path = norm_path
            except Exception as e:
                logger.warning(
                    f"Ken Burns still {idx} could not be normalized "
                    f"({img_path}): {e} — using it as-is"
                )

            cmd = [
                "ffmpeg", "-y",
                "-i", str(img_path),
                "-vf", _kenburns_image_vf(
                    effect, frames_per_image, out_w, out_h, fps),
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
                for clip in tmp_clips + tmp_stills:
                    clip.unlink(missing_ok=True)
                if final_video != output_path:
                    final_video.unlink(missing_ok=True)
                return output_path

        # No audio or merge failed — move video to output
        import shutil
        shutil.move(str(final_video), str(output_path))
        for clip in tmp_clips + tmp_stills:
            clip.unlink(missing_ok=True)
        return output_path

    return await asyncio.to_thread(_generate)


async def generate_single_kenburns_clip(
    image_path: Path,
    duration: float,
    output_path: Path,
    target_w: int = 1080,
    target_h: int = 1920,
    fps: int = 30,
    normalize: bool = True,
) -> Path:
    """Turn ONE still into ONE silent clip of exactly `duration` seconds at
    target_w x target_h, with a Ken Burns move.

    The per-scene counterpart to generate_kenburns_video, which owns a whole
    video: this one produces a clip that stitches alongside stock footage, so
    it must land on the same geometry and carry no audio track.

    `normalize=False` is for callers that already ran normalize_still() —
    typically because they needed to know whether the image was READABLE
    before committing to a plan around it, and re-running the pass would be
    pure duplicated work. Everyone else should leave it on; see
    normalize_still's docstring for what goes wrong without it.
    """
    image_path, output_path = Path(image_path), Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    scratch: Path | None = None
    if normalize:
        scratch = _tmp(f"kb_one_{uuid4().hex[:6]}.png")
        await normalize_still(image_path, scratch)
        image_path = scratch

    def _generate():
        frames = max(1, int(duration * fps))
        effect = random.choice(_KENBURNS_EFFECTS)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(image_path),
            "-vf", _kenburns_image_vf(effect, frames, target_w, target_h, fps),
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-t", str(duration),
            "-an",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0 or not output_path.exists():
            raise VideoGenerationError(
                f"Ken Burns clip failed: {result.stderr[:300]}"
            )
        return output_path

    try:
        return await asyncio.to_thread(_generate)
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)


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


ASPECT_DIMS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1":  (1080, 1080),
    "4:5":  (1080, 1350),
}


async def _probe_dimensions(video_path: Path) -> tuple[int, int]:
    """(width, height) of the first video stream, or (0, 0) if unreadable —
    callers treat 0 as 'unknown' and fall back to the safe path."""
    def _run() -> tuple[int, int]:
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", str(video_path)],
                capture_output=True, text=True, timeout=10,
            )
            w, h = probe.stdout.strip().split(",")[:2]
            return int(w), int(h)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            logger.debug("Could not probe video dimensions: %s", e)
            return 0, 0
    return await asyncio.to_thread(_run)


def pick_reframe_method(src_w: int, src_h: int, target_aspect: str) -> str:
    """Choose crop vs blur_fill for a re-frame. Pure — unit-tested.

    blur_fill is the right look when NARROWING (16:9 -> 9:16): the whole frame
    is preserved as a centered band over a blurred backdrop, which is the
    familiar short-form treatment.

    WIDENING it is actively bad: fitting a 9:16 source inside 16:9 leaves the
    real content as a narrow full-height strip, ~1/3 of the width, with blur
    either side. When the source is ITSELF a blur_fill composite — which every
    ViralMint short is, since that's how shorts are built from landscape
    sources — the result nests a second box and the picture lands at roughly a
    third of the frame in each direction. Cropping to fill keeps the pixels
    sharp and full-frame; on an already-composited source it lands almost
    exactly on the original content band. The cost is the top/bottom margin —
    on a true portrait source that can clip a burned caption, which still beats
    rendering it at a third scale.
    """
    tw, th = ASPECT_DIMS.get(target_aspect, (1920, 1080))
    if not src_w or not src_h:
        return "blur_fill"          # unknown source → the safe, lossless look
    # 2% tolerance so a near-identical aspect (1.77 vs 1.78) isn't "widening".
    return "crop" if (tw / th) > (src_w / src_h) * 1.02 else "blur_fill"


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
      - "auto": crop when widening, blur_fill when narrowing (see
        `pick_reframe_method`) — what callers should use unless the user
        explicitly picked a look
      - "letterbox": add black bars to fill (no content lost)
      - "crop": crop center to fill (content lost at edges)
      - "blur_fill": blurred+scaled background behind original (modern look)

    Returns path to converted video.
    """
    if method == "auto":
        src_w, src_h = await _probe_dimensions(video_path)
        method = pick_reframe_method(src_w, src_h, target_aspect)
        logger.info("convert_aspect_ratio: auto → %s (src %sx%s → %s)",
                    method, src_w, src_h, target_aspect)

    if output_path is None:
        stem = video_path.stem
        output_path = video_path.parent / f"{stem}_{target_aspect.replace(':', 'x')}_{method}.mp4"

    if output_path.exists():
        return output_path

    def _run():
        target_w, target_h = ASPECT_DIMS.get(target_aspect, (1920, 1080))

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
        common = [
            "ffmpeg", "-y", "-i", str(video_path),
            filter_flag, vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        ]
        # Audio is untouched by an aspect conversion — copy it instead of
        # paying a 128k AAC generation every pass. Chained tools stack these:
        # reframe → captions → watermark used to re-encode the same voice
        # track three times for three geometry operations. The copy is
        # invalid for codecs mp4 can't carry (Opus/Vorbis out of a WebM/MKV
        # source), so the re-encode stays as the second rung.
        last_err = ""
        for label, aargs in (("copy", ["-c:a", "copy"]),
                             ("re-encode", ["-c:a", "aac", "-b:a", "128k"])):
            result = subprocess.run(common + aargs + [str(output_path)],
                                    capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                return output_path
            last_err = result.stderr[-500:]
            logger.warning("Aspect conversion (%s audio) failed: %s", label, last_err[:200])
            # A failed pass leaves a partial file behind; drop it so the next
            # attempt isn't fooled by it and a failure can't look like a result.
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass
        logger.error(f"Aspect ratio conversion failed: {last_err}")
        raise VideoGenerationError(f"FFmpeg aspect conversion failed: {last_err[:200]}")

    return await asyncio.to_thread(_run)


def _format_srt_time(seconds: float) -> str:
    """Convert seconds to SRT time format (HH:MM:SS,mmm)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def has_audio_stream_sync(media_path: Path) -> bool:
    """Blocking core of `has_audio_stream`, for callers already on a thread.

    One implementation, two entry points — a second ffprobe-for-audio helper
    is exactly how two call sites end up disagreeing about what "has audio"
    means on a probe failure.
    """
    media_path = Path(media_path)
    if not media_path.exists():
        return False
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


async def has_audio_stream(media_path: Path) -> bool:
    """Quick ffprobe check: does this file contain any audio stream?

    Used by whisper_service to fail fast with a clear error when handed
    a video-only file (e.g. a download that missed the audio track) —
    faster-whisper's own error in that case is an opaque "tuple index
    out of range" from deep inside its decoder.
    """
    return await asyncio.to_thread(has_audio_stream_sync, media_path)


async def extract_frame_at(
    video_path: Path,
    timestamp: float,
    output_path: Path,
    quality: int = 2,
    scale_width: int | None = None,
) -> Path | None:
    """Extract a single frame at an exact timestamp.

    Generalised counterpart to `extract_thumbnail` — the caller owns the
    output path, so it can write into a cache keyed however it likes instead
    of bouncing through the thumbnails directory under a derived name.

    Args:
        video_path: Source file. Must exist on disk.
        timestamp: Seconds into the video. Out-of-range values are clamped by
            ffmpeg's own seek; the extracted frame is the closest one
            at-or-before.
        output_path: Where to write the JPEG. Parent dir is created.
        quality: -q:v value (1=best, 31=worst). 2 matches extract_thumbnail.
        scale_width: optional output width in px, aspect preserved. Callers
            that only ever display a small frame (the Clipper bench's
            132px-wide IN/OUT panes) should pass it — encoding a full 1080p
            JPEG per drag settle is bytes and CPU nobody sees.

    Returns:
        The output path on success, None on failure (logged). An empty file
        counts as a failure: a 0-byte JPEG renders as a broken image, which
        is worse than no image at all.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _extract():
        # Write to a unique temp sibling, then os.replace: this file is a
        # cache entry served with immutable headers, and building it in
        # place let a concurrent request stat a half-written JPEG and pin
        # the truncated bytes in the browser for a year.
        import uuid
        tmp = output_path.with_name(
            f"{output_path.stem}.{uuid.uuid4().hex[:8]}.tmp{output_path.suffix}")
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(max(0.0, float(timestamp))),  # -ss BEFORE -i = fast seek
            "-i", str(video_path),
            "-vframes", "1",
            "-q:v", str(quality),
        ]
        if scale_width:
            # -2 keeps the height even and the aspect intact.
            cmd += ["-vf", f"scale={int(scale_width)}:-2"]
        cmd.append(str(tmp))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.warning(
                    "Frame extract failed at t=%.2fs for %s: %s",
                    timestamp, video_path.name, ffmpeg_error(result.stderr, 200),
                )
                return None
            if not tmp.exists() or tmp.stat().st_size == 0:
                logger.warning("Frame extract produced empty file at t=%.2fs", timestamp)
                return None
            tmp.replace(output_path)
        finally:
            tmp.unlink(missing_ok=True)
        return output_path

    return await asyncio.to_thread(_extract)


# Filmstrip sampling bounds — see extract_filmstrip.
MIN_CELL_SEC = 0.2       # densest useful sampling; also caps the ffmpeg spawns
TAIL_MARGIN_SEC = 0.25   # keep the last seek inside the last decodable frame


async def extract_filmstrip(
    video_path: Path,
    output_path: Path,
    count: int = 32,
    tile_height: int = 64,
    duration: float | None = None,
    concurrency: int = 6,
) -> Path | None:
    """Render a horizontal contact sheet: `count` frames tiled 1 row deep.

    This is the scrubbing background behind Clipper's cutting bench — one
    image request instead of `count` of them, so a wide timeline paints in a
    single paint.

    Why per-frame fast seek and not `-vf fps=…`: an fps filter decodes the
    ENTIRE source (minutes on a one-hour podcast, which is exactly the input
    Clipper exists for), and its frame count drifts with rounding — a short
    tile flushes padded with black. Seeking to each timestamp with `-ss`
    before `-i` is O(1) per frame and lands on exact times, so cell i
    genuinely means `duration * (i + 0.5) / count`. The tiling pass then
    reads an image sequence, where the input count is known exactly and the
    tile is always complete.

    Args:
        video_path: Source file. Must exist.
        output_path: Where the JPEG strip lands. Parent dir is created.
        count: Number of cells (clamped 4..96).
        tile_height: Cell height in px (clamped 24..160). Width follows the
            source aspect, rounded even for the encoder.
        duration: Source duration if the caller already knows it; probed when
            omitted.
        concurrency: Parallel ffmpeg seeks. 6 keeps a long source fast
            without saturating a laptop's cores.

    Returns:
        `output_path` on success, None on failure (logged). A partial
        extraction is a failure: a strip with holes would mislead the user
        about where they are in the video.
    """
    if not video_path.exists():
        return None

    count = max(4, min(int(count), 96))
    tile_height = max(24, min(int(tile_height), 160))

    if duration is None or duration <= 0:
        duration = await asyncio.to_thread(probe_duration, video_path, 0.0)
    if not duration or duration <= 0:
        logger.warning("filmstrip: could not probe duration for %s", video_path.name)
        return None

    # Never sample denser than MIN_CELL_SEC. A wide timeline over a 9-second
    # TikTok would otherwise ask for cells 60ms apart — dozens of ffmpeg
    # spawns for frames that repeat, and (before the tail clamp below) a
    # guaranteed failure on the last one.
    count = min(count, max(4, int(duration / MIN_CELL_SEC)))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    work = settings.TMP_DIR / f"strip_{uuid4().hex[:12]}"
    work.mkdir(parents=True, exist_ok=True)

    try:
        # Cell centres, so the first frame (often a black fade-in) isn't the
        # one that represents the opening.
        #
        # The tail clamp is load-bearing. A container's reported duration runs
        # to the END of the last frame, so the final cell's centre —
        # duration × (count-0.5)/count — can sit past the last frame's
        # presentation time, and `-ss` there decodes nothing. One missing
        # frame fails the whole strip, so a 6s source asked for 32 cells
        # produced no timeline at all. Clamping the SEEK (not the cell's
        # nominal time) keeps every cell's position on the timeline honest and
        # costs at most a slightly-early final thumbnail.
        seek_ceiling = max(0.0, duration - TAIL_MARGIN_SEC)
        stamps = [min(duration * (i + 0.5) / count, seek_ceiling) for i in range(count)]
        sem = asyncio.Semaphore(max(1, int(concurrency)))

        async def _one(i: int, ts: float):
            out = work / f"f{i + 1:03d}.jpg"
            async with sem:
                got = await extract_frame_at(
                    video_path=video_path, timestamp=ts, output_path=out,
                    quality=6,  # strip cells are ~100px wide; q6 halves the bytes
                )
                if got:
                    return got
                # A fixed ceiling can't know the source's frame interval — a
                # 1fps video's last frame is a whole second before the end.
                # Step back once rather than losing the strip.
                retry = max(0.0, ts - max(1.0, duration * 0.02))
                if retry >= ts:
                    return None
                return await extract_frame_at(
                    video_path=video_path, timestamp=retry, output_path=out, quality=6,
                )

        # Probe the two ENDS before committing to the middle. A strip needs
        # every cell (see the docstring — one with holes would mislead the user
        # about where they are), so if either end fails the strip fails anyway
        # and the other 30 spawns buy nothing. Without this, an unreadable
        # source costs 2N ffmpeg spawns — every cell fails and every failure
        # retries once — plus N log lines, and pays it again every time that
        # source is selected, because the bench rebuilds the strip each visit.
        #
        # BOTH ends are probed, not just the first, because the two realistic
        # damage shapes differ. A download cancelled mid-write leaves a valid
        # header, a plausible duration and no usable data at all — that fails
        # cell 0. A download truncated at the END still decodes its opening
        # frames and fails everything after: measured on a 45s source cut to
        # 7% of its bytes, cell 0 succeeded and 31 cells then failed for 61
        # ffmpeg spawns. Probing the tail catches that one for two.
        #
        # Bailing on the tail is outcome-identical to running the full set:
        # `_one` already retries a step-back for low-frame-rate sources, so a
        # tail that fails here would have failed the `len(ok) < count` check
        # below regardless.
        last_i = len(stamps) - 1
        first = await _one(0, stamps[0])
        if not first:
            logger.warning(
                "filmstrip: %s decodes no frames — skipping the remaining %d "
                "cells (corrupt or partially-downloaded source?)",
                video_path.name, last_i,
            )
            return None

        last = await _one(last_i, stamps[last_i]) if last_i else first
        if not last:
            logger.warning(
                "filmstrip: %s decodes its opening frames but not its end — "
                "skipping the remaining %d cells (truncated download?)",
                video_path.name, max(0, last_i - 1),
            )
            return None

        middle = await asyncio.gather(
            *(_one(i, ts) for i, ts in enumerate(stamps) if 0 < i < last_i),
            return_exceptions=True,
        )
        results = [first, last, *middle] if last_i else [first]
        ok = [r for r in results if r and not isinstance(r, Exception)]
        if len(ok) < count:
            logger.warning(
                "filmstrip: only %d/%d frames extracted from %s — no strip",
                len(ok), count, video_path.name,
            )
            return None

        def _tile():
            # Temp-then-replace, same reason as extract_frame_at: the strip
            # is served immutable, so a half-written tile must never be
            # observable at the final path.
            import uuid
            tmp = output_path.with_name(
                f"{output_path.stem}.{uuid.uuid4().hex[:8]}.tmp{output_path.suffix}")
            cmd = [
                "ffmpeg", "-y",
                "-f", "image2",
                "-start_number", "1",
                "-i", str(work / "f%03d.jpg"),
                # scale=-2 keeps the width even (some JPEG encoders reject odd
                # chroma widths) and preserves aspect, so a cell is never
                # squashed.
                "-vf", f"scale=-2:{tile_height},tile={count}x1",
                "-frames:v", "1",
                "-q:v", "5",
                str(tmp),
            ]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    logger.warning("filmstrip tile failed for %s: %s",
                                   video_path.name, r.stderr[-300:])
                    return None
                if not tmp.exists() or tmp.stat().st_size == 0:
                    logger.warning("filmstrip tile produced an empty file for %s",
                                   video_path.name)
                    return None
                tmp.replace(output_path)
            finally:
                tmp.unlink(missing_ok=True)
            return output_path

        return await asyncio.to_thread(_tile)
    finally:
        # Clean up our own scratch explicitly — TMP_DIR holds yt-dlp's cookie
        # jar and uploaded media, so it has no blanket purge to fall back on.
        def _cleanup():
            for p in work.glob("*"):
                p.unlink(missing_ok=True)
            work.rmdir()
        try:
            await asyncio.to_thread(_cleanup)
        except OSError:
            pass
