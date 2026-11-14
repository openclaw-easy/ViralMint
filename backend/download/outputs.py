# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""What a download leaves on disk: finding it, checking it, repairing it,
cleaning up after it, and reading the subtitles that came with it.

The rule every caller saves a Library row on: a success names a file that
exists, is big enough to be a real video, and has the stream we asked for
`validate_output` is that check.
"""
from __future__ import annotations

import glob
import logging
import re
from pathlib import Path

from backend.core.exceptions import DownloadError
from backend.download.classify import ErrorClass

logger = logging.getLogger(__name__)


# Output sanity-check thresholds. A real video — even a 5-second TikTok at
# the lowest quality yt-dlp would ever produce — is at least ~50KB and >2
# seconds. Anything smaller is an init-segment-only artifact or a
# truncated download.
_MIN_OUTPUT_BYTES = 50 * 1024     # 50 KB
_MIN_OUTPUT_DURATION_S = 2.0


class OutputInvalidError(DownloadError):
    """A downloaded file that is not a usable video: too small, too short, or
    the wrong stream. Typed, so nothing has to recognise it by its message:
    `classify` reads `error_class` and the table sends it to the next format."""
    error_class = ErrorClass.OUTPUT_INVALID


def validate_output(path: Path, source_label: str = "download") -> None:
    """Reject downloads that produced an unusable file.

    Raises OutputInvalidError with a descriptive message if the file
    looks truncated or like a captured init-segment / UI-sound artifact
    rather than the actual video. The bad file is deleted before raising
    so we never leave junk in storage for the analyzer to choke on.

    The error is typed (`error_class = OUTPUT_INVALID`): the table sends it
    to the next format, because the same format would produce the same file.
    """
    if not path or not path.exists():
        raise OutputInvalidError(f"{source_label}: output file missing on disk")
    size = path.stat().st_size
    if size < _MIN_OUTPUT_BYTES:
        try:
            path.unlink()
        except OSError:
            pass
        raise OutputInvalidError(
            f"{source_label}: output too small ({size} bytes < {_MIN_OUTPUT_BYTES}) — "
            f"likely an init-segment or captured UI sound, not the actual video"
        )
    # Duration probe is best-effort — a corrupt container can defeat ffprobe,
    # in which case we let it pass on the size check alone (50KB is already a
    # strong signal). Don't fail validation just because ffprobe couldn't read.
    try:
        from backend.services.video_utils import probe_duration
        duration = probe_duration(path, default=0.0)
    except Exception:
        duration = 0.0
    if 0 < duration < _MIN_OUTPUT_DURATION_S:
        try:
            path.unlink()
        except OSError:
            pass
        raise OutputInvalidError(
            f"{source_label}: output duration {duration:.2f}s < {_MIN_OUTPUT_DURATION_S}s — "
            f"likely captured wrong stream"
        )


def find_video_file(output_dir: Path, filename_stem: str) -> Path | None:
    """Find the actual downloaded video file — yt-dlp may change the extension after merging."""
    video_exts = [".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv"]
    for ext in video_exts:
        candidate = output_dir / f"{filename_stem}{ext}"
        if candidate.exists():
            return candidate
    # Fallback: glob for any file matching the stem
    matches = list(output_dir.glob(f"{glob.escape(filename_stem)}.*"))
    # Exclude subtitle files
    matches = [m for m in matches if m.suffix.lower() not in (".srt", ".vtt", ".ass", ".json", ".txt")]
    if matches:
        # Return the largest file (most likely the video)
        return max(matches, key=lambda p: p.stat().st_size)
    return None


def extract_audio_locally(video_path: Path, audio_dir: Path, filename_stem: str) -> Path | None:
    """Extract audio from a local video file using FFmpeg instead of re-downloading."""
    import subprocess
    audio_path = audio_dir / f"{filename_stem}_audio.mp3"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "libmp3lame",
             "-ab", "192k", "-ar", "44100", str(audio_path)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0 and audio_path.exists() and audio_path.stat().st_size > 0:
            return audio_path
        from backend.services.ffmpeg_service import ffmpeg_error
        logger.warning("FFmpeg audio extraction returned %s: %s",
                       result.returncode, ffmpeg_error(result.stderr, 200))
    except subprocess.TimeoutExpired:
        logger.warning(f"FFmpeg audio extraction timed out for {video_path}")
    except FileNotFoundError:
        logger.warning("FFmpeg not found — cannot extract audio locally")
    return None


def cleanup_subtitle_files(output_dir: Path, filename_stem: str):
    """Remove downloaded subtitle files after they've been parsed."""
    for ext in ("srt", "vtt"):
        for f in output_dir.glob(f"{glob.escape(filename_stem)}.*.{ext}"):
            try:
                f.unlink()
            except Exception:
                pass


def cleanup_partial_files(output_dir: Path, filename_stem: str):
    """Remove leftover .part files and intermediate format files from failed downloads."""
    if not filename_stem:
        return
    cleaned = 0
    # Every pattern starts with "<stem>." so that sweeping `vid1` can never
    # touch `vid10`'s files, and the stem is escaped so brackets in a name are
    # not read as a glob.
    stem = glob.escape(filename_stem)
    # Incomplete transfers: `.part`, HLS/DASH fragments (`.part-Frag12`), and
    # yt-dlp's resume bookkeeping (`.ytdl`).
    partial = [*output_dir.glob(f"{stem}.*.part"), *output_dir.glob(f"{stem}.*.part-Frag*"),
               *output_dir.glob(f"{stem}.*.ytdl")]
    for f in partial:
        try:
            f.unlink()
            cleaned += 1
        except Exception:
            pass
    # Intermediate format files (e.g. .f398.mp4, .f251.webm) left by failed merges
    for f in output_dir.glob(f"{stem}.f[0-9]*.*"):
        try:
            f.unlink()
            cleaned += 1
        except Exception:
            pass
    # Temp files from yt-dlp merge operations
    for f in output_dir.glob(f"{stem}.temp.*"):
        try:
            f.unlink()
            cleaned += 1
        except Exception:
            pass
    if cleaned:
        logger.info(f"Cleaned up {cleaned} partial/intermediate file(s) for {filename_stem}")


def try_manual_merge(output_dir: Path, filename_stem: str) -> Path | None:
    """
    When yt-dlp's merge/rename fails, try to merge video+audio streams manually with ffmpeg.
    yt-dlp downloads separate streams as .f{N}.{ext} files — we find the largest video
    and audio streams and merge them.
    """
    import subprocess

    # Find intermediate stream files: e.g. video_id.f398.mp4, video_id.f251.webm
    stream_files = sorted(output_dir.glob(f"{glob.escape(filename_stem)}.f[0-9]*.*"), key=lambda p: p.stat().st_size, reverse=True)
    if len(stream_files) < 2:
        return None

    # Identify video and audio streams by probing with ffprobe
    video_stream = None
    audio_stream = None
    for f in stream_files:
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_streams", "-print_format", "json", str(f)],
                capture_output=True, text=True, timeout=10,
            )
            if probe.returncode != 0:
                continue
            import json as _json
            info = _json.loads(probe.stdout)
            for s in info.get("streams", []):
                if s.get("codec_type") == "video" and not video_stream:
                    video_stream = f
                elif s.get("codec_type") == "audio" and not audio_stream:
                    audio_stream = f
        except Exception:
            continue

    if not video_stream or not audio_stream:
        # Fallback: assume largest is video, second largest is audio
        video_stream = stream_files[0]
        audio_stream = stream_files[1]

    output_path = output_dir / f"{filename_stem}.mp4"
    logger.info(f"Manual merge: {video_stream.name} + {audio_stream.name} → {output_path.name}")

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_stream), "-i", str(audio_stream),
             "-c", "copy", "-movflags", "+faststart", str(output_path)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            # Clean up intermediate streams
            for f in stream_files:
                try:
                    f.unlink()
                except Exception:
                    pass
            # Also clean temp files
            for f in output_dir.glob(f"{glob.escape(filename_stem)}.temp.*"):
                try:
                    f.unlink()
                except Exception:
                    pass
            logger.info(f"Manual merge successful: {output_path} ({output_path.stat().st_size / 1024 / 1024:.1f}MB)")
            return output_path
        else:
            from backend.services.ffmpeg_service import ffmpeg_error
            logger.warning("Manual merge failed (exit %s): %s", result.returncode, ffmpeg_error(result.stderr, 200))
    except FileNotFoundError:
        logger.warning("ffmpeg not found — cannot perform manual merge")
    except subprocess.TimeoutExpired:
        logger.warning("Manual merge timed out after 300s")
    except Exception as e:
        logger.warning(f"Manual merge error: {e}")

    return None


def collect_subtitles(
    output_dir: Path, video_id: str, info: dict | None = None,
) -> list[dict] | None:
    """Find downloaded subtitle files and parse the best one into text segments.

    Language-aware picker, in priority order:
      1. Creator subtitle in the video's primary language (info.language).
      2. Auto subtitle in the video's primary language.
      3. Creator subtitle in English (most likely original for our scout sources).
      4. Auto subtitle in English.
      5. Any creator subtitle.
      6. First available file (final fallback).

    Why auto-in-target beats creator-in-wrong-language: the analyzer +
    generator consume this transcript to understand the video's actual
    content. An auto-generated transcript in the right language is more
    useful than a human-quality transcript in the wrong one.

    Why this exists: the previous picker ran `glob.glob()` (which returns
    files in alphabetic order) and used `[0]`, which meant the FIRST
    subtitle alphabetically won. With our 10-language download set
    (`["en","zh","ja","ko","es","fr","de","pt","ru","ar"]`), Arabic comes
    first alphabetically — so any English video whose creator also provided
    Arabic subtitles got an Arabic transcript. Visible downstream in the
    `transcript_language` field on the Library detail page; the Analyzer
    and Generator both consumed the wrong language.

    `info` is the yt-dlp info dict from extract_info(download=True). When
    provided, `info["language"]` gives the original-language code and
    `info["subtitles"]` / `info["automatic_captions"]` distinguish creator
    subs from auto-generated. When None, we fall through to filename-based
    heuristics (legacy behavior — preserved for safety).
    """
    # yt-dlp saves subtitles as: {video_id}.{lang}.{ext}
    patterns = [
        str(output_dir / f"{video_id}.*.srt"),
        str(output_dir / f"{video_id}.*.vtt"),
    ]
    sub_files = []
    for pattern in patterns:
        sub_files.extend(glob.glob(pattern))

    if not sub_files:
        return None

    # Build per-file metadata. yt-dlp's filename pattern is
    # `{video_id}.{lang}.{ext}` where `lang` may carry a region tag
    # (e.g. "en-US", "zh-Hans"). We normalize to the primary tag (before
    # the first hyphen) so "en-US" matches info.language="en".
    files = []
    for f in sub_files:
        stem = Path(f).stem  # e.g. "video_id.en-US"
        parts = stem.split(".")
        raw_lang = parts[-1] if len(parts) > 1 else ""
        primary = raw_lang.lower().split("-", 1)[0]
        files.append({"file": f, "raw_lang": raw_lang, "primary": primary})

    # When yt-dlp metadata is available, it's the source of truth for
    # creator-vs-auto. info["subtitles"] keys are creator-provided langs;
    # info["automatic_captions"] are auto-generated. We tag each file
    # against those dicts; absent metadata, we fall back to the legacy
    # ".auto." filename heuristic (which is mostly a no-op since yt-dlp
    # writes both creator and auto subs to the same filename pattern,
    # but kept for defense in depth).
    # Only creator langs are needed: `_is_creator` treats "not in
    # creator_langs" as auto/stale, so a separate auto_langs set added
    # nothing (it sat here unused until the 2026-07-28 audit).
    creator_langs: set[str] = set()
    if info:
        creator_langs = {l.lower() for l in (info.get("subtitles") or {}).keys()}

    def _is_creator(entry: dict) -> bool:
        if info:
            raw = entry["raw_lang"].lower()
            primary = entry["primary"]
            # Trust metadata when present: a lang is creator-provided ONLY
            # if it's in info["subtitles"]. If it's in automatic_captions
            # (or not listed at all — could be a stale file from a previous
            # download), treat as non-creator. Otherwise a leftover file
            # would beat a real auto-caption in the same lang.
            return raw in creator_langs or primary in creator_langs
        # No metadata — fall back to filename heuristic. yt-dlp's default
        # writer uses the same filename pattern for both creator and auto
        # subs, so this rarely fires; preserved as a safety net.
        return ".auto." not in Path(entry["file"]).name

    # Normalize the video's primary language. Strip region tag and
    # lowercase for a stable comparison.
    target_primary = ""
    if info:
        raw = (info.get("language") or "").lower()
        if raw:
            target_primary = raw.split("-", 1)[0]

    def _find(want_creator: bool, want_lang_primary: str | None) -> dict | None:
        for entry in files:
            is_cr = _is_creator(entry)
            if want_creator and not is_cr:
                continue
            if not want_creator and is_cr:
                continue
            if want_lang_primary and entry["primary"] != want_lang_primary:
                continue
            return entry
        return None

    chosen_entry = (
        # Right language wins regardless of creator/auto.
        (target_primary and _find(True, target_primary))
        or (target_primary and _find(False, target_primary))
        # Then English (creator > auto), the most likely original.
        or _find(True, "en")
        or _find(False, "en")
        # Last resort: any creator sub at all, then any file.
        or _find(True, None)
        or files[0]
    )

    chosen = chosen_entry["file"]

    try:
        text = Path(chosen).read_text(encoding="utf-8", errors="replace")
        segments = parse_subtitle_text(text, chosen)
        source = "creator_subtitles" if _is_creator(chosen_entry) else "auto_subtitles"

        # Log when language picker rescues us from a mismatch — useful for
        # confirming the fix is doing real work in production logs.
        if target_primary and chosen_entry["primary"] != target_primary:
            logger.info(
                "Subtitle picker: video lang=%s, no matching sub available — using %s (%s)",
                target_primary, chosen_entry["raw_lang"], source,
            )

        return {
            "text": segments_to_text(segments),
            "language": chosen_entry["raw_lang"],
            "source": source,
            "segments": segments,
            "file": str(chosen),
        }
    except Exception as e:
        logger.warning(f"Failed to parse subtitle file {chosen}: {e}")
        return None


def parse_subtitle_text(content: str, filepath: str) -> list[dict]:
    """Parse SRT or VTT subtitle content into segments with timestamps."""

    segments = []

    if filepath.endswith(".vtt"):
        # WebVTT format
        # Skip header lines
        lines = content.split("\n")
        i = 0
        while i < len(lines) and not re.match(r"\d{2}:\d{2}", lines[i]):
            i += 1

        while i < len(lines):
            line = lines[i].strip()
            # Match timestamp line: 00:00:01.000 --> 00:00:04.000
            ts_match = re.match(
                r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})", line
            )
            if ts_match:
                start = _ts_to_seconds(ts_match.group(1))
                end = _ts_to_seconds(ts_match.group(2))
                i += 1
                text_lines = []
                while i < len(lines) and lines[i].strip():
                    # Strip VTT tags like <c> </c> and positioning
                    cleaned = re.sub(r"<[^>]+>", "", lines[i].strip())
                    if cleaned:
                        text_lines.append(cleaned)
                    i += 1
                text = " ".join(text_lines).strip()
                if text:
                    segments.append({"start": start, "end": end, "text": text})
            else:
                i += 1
    else:
        # SRT format
        blocks = re.split(r"\n\s*\n", content.strip())
        for block in blocks:
            lines = block.strip().split("\n")
            if len(lines) < 2:
                continue
            # Find timestamp line
            for j, line in enumerate(lines):
                ts_match = re.match(
                    r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})", line
                )
                if ts_match:
                    start = _ts_to_seconds(ts_match.group(1))
                    end = _ts_to_seconds(ts_match.group(2))
                    text = " ".join(l.strip() for l in lines[j + 1:] if l.strip())
                    text = re.sub(r"<[^>]+>", "", text)  # strip HTML tags
                    if text:
                        segments.append({"start": start, "end": end, "text": text})
                    break

    return segments


def _ts_to_seconds(ts: str) -> float:
    """Convert timestamp like '00:01:23,456' or '00:01:23.456' to seconds."""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + s


def segments_to_text(segments: list[dict]) -> str:
    """Join subtitle segments into a single text string, deduplicating consecutive identical lines."""
    seen = []
    for seg in segments:
        text = seg["text"].strip()
        if not seen or text != seen[-1]:
            seen.append(text)
    return " ".join(seen)


def extract_chapters(info: dict) -> list[dict] | None:
    """Extract chapter markers from yt-dlp metadata."""
    chapters = info.get("chapters")
    if not chapters:
        return None

    return [
        {
            "start": ch.get("start_time", 0),
            "end": ch.get("end_time", 0),
            "title": ch.get("title", ""),
        }
        for ch in chapters
        if ch.get("title")
    ]


# Outer bounds for the metadata-only helpers. A healthy call returns in
