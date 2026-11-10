# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Shared video utility functions — used across multiple services."""
import logging
import math
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def run_ffmpeg(args: list[str], *, timeout: float = 600, binary_stdout: bool = False) -> subprocess.CompletedProcess:
    """ffmpeg with `args` (an argv list — never a shell), errors only on stderr.
    `binary_stdout` returns stdout as bytes (e.g. decoded PCM)."""
    return subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                          capture_output=True, text=not binary_stdout, timeout=timeout)


# Sinks a clip about something else below an on-topic 1920x1080, which an
# off-topic portrait otherwise beats by ~40 on orientation_fit_adjustment alone
# (and only the top of the pool reaches the AI rank). Every off-topic clip pays
# it, whatever its shape or library: when a slug can't speak for the footage
# ("satisfying"), the vertical still leads.
OFF_TOPIC_PENALTY = 60
_TOPIC_FILLER = frozenset(
    "the and for with from into over under that this your you are was its our their out off about video videos "
    "footage clip clips shot shots stock close closeup slow motion view".split())


def topic_words(words) -> set[str]:
    """Content words of a description. Pixabay's tags can hold several words
    ("honey bee"), so each is split."""
    return {w for x in words for w in str(x).lower().split() if len(w) >= 3 and w not in _TOPIC_FILLER}


def on_topic(candidate: dict, query: str) -> bool | None:
    """Does a clip's description share a word with its query? None = can't
    tell (no description, or a query in a language the English slugs don't
    speak). Loose on purpose (bee/honeybees, dancer/dancing): only a clear miss
    may cost a clip anything, and the AI rank still judges what's left."""
    wanted = topic_words(re.findall(r"[a-z0-9]+", (query or "").lower()))
    described = topic_words(candidate.get("desc_words") or [])
    if not wanted or not described:
        return None
    return any(a in b or b in a or a[:4] == b[:4] for a in wanted for b in described)


def demote_off_topic(candidates: list[dict], query: str) -> list[dict]:
    """Tall targets only: a new best-first list, the input untouched (it may be
    the cached copy)."""
    scored = [{**c, "quality_score": c.get("quality_score", 0) - OFF_TOPIC_PENALTY}
              if on_topic(c, query) is False else c for c in candidates]
    scored.sort(key=lambda x: x.get("quality_score", 0), reverse=True)
    return scored


def orientation_fit_adjustment(w: int, h: int, orientation: str) -> float:
    """Quality-score adjustment for how well a stock candidate fits the target
    orientation once cover-cropped.

    A pool can still mix orientations (Pixabay can't filter, and the Pexels
    portrait search tops up from any orientation when it comes back short).
    A 1920×1080 file keeps only ~31% of its width for a 9:16
    canvas and gets upscaled ~1.8×. Adjustment = orientation-match bonus −
    upscale penalty:
      * +25 when the file's orientation matches the target
      * −30 × (upscale_factor − 1), capped at −60

    For a portrait (1080×1920) target: 1080×1920 → +25, 720×1280 → +10,
    1920×1080 → −23.3.
    """
    if w <= 0 or h <= 0:
        return 0.0
    if orientation == "portrait":
        tw, th = 1080, 1920
    else:
        tw, th = 1920, 1080
    match_bonus = 25.0 if ((w >= h) == (tw >= th)) else 0.0
    upscale = max(tw / w, th / h)
    upscale_penalty = min(max(upscale - 1.0, 0.0) * 30.0, 60.0)
    return match_bonus - upscale_penalty


def probe_duration(file_path: Path | str, default: float = 0.0) -> float:
    """Get media file duration in seconds using ffprobe.

    Returns *default* if the probe fails for any reason (missing file,
    corrupt media, ffprobe not installed, etc.).
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(result.stdout.strip())
    except Exception:
        return default


# The largest timestamp any real media carries — ~10 days. Used as the UPPER
# bound on a user-supplied seconds value so the check is two-sided: NaN and inf
# fail every comparison, so a one-sided `< 0` guard passes them straight through
# to ffmpeg (a Form float parses "nan" and "inf" happily).
MAX_MEDIA_SECONDS = 863999.0


def probe_media(file_path: Path | str) -> tuple[int, int, float]:
    """Get (width, height, duration_seconds) in ONE ffprobe call.

    Prefer this over calling a dimensions probe and `probe_duration`
    back-to-back — a caller that needs both otherwise spawns two subprocesses
    per file, which adds up on a pipeline that touches every clip. Returns 0
    for whichever fields could not be read; callers decide the fallback.

    The three fields fail INDEPENDENTLY. This used to parse all-or-nothing,
    so a container whose format carries no duration — ffprobe prints a
    literal "N/A", and `float("N/A")` raises — threw away perfectly good
    dimensions and returned (0, 0, 0.0). Every dimension consumer defaults to
    9:16 on zeros (`aspect_from_dims`), so a 1920x1080 elementary stream was
    labelled portrait, and the caption geometry took that label: wrong
    margins and font size, burned into the file. Read each field on its own.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return 0, 0, 0.0

    # Output is one value per line, in this order: width, height, duration.
    parts = [p for p in result.stdout.strip().splitlines() if p.strip()]

    def _num(index: int, cast):
        try:
            return cast(parts[index])
        except (IndexError, TypeError, ValueError):
            return cast(0)

    width, height = _num(0, int), _num(1, int)
    duration = _num(2, float)
    # A negative or non-finite duration is as unusable as a missing one, and
    # both reach ffmpeg as a seek bound downstream.
    if not math.isfinite(duration) or duration < 0:
        duration = 0.0
    return width, height, duration


# Aspect labels the app stores on GeneratedVideo.aspect_ratio and keys its
# Library tile sizing / filter chips off. Anything not near-square and not
# portrait reads as landscape.
def aspect_from_dims(width: int, height: int, default: str = "9:16") -> str:
    """Map raw dimensions onto the app's aspect vocabulary. Pure.

    The 1:1 band is deliberately narrow (±2%) — encoders round odd
    dimensions, so 1080x1082 is square in intent.
    """
    if width <= 0 or height <= 0:
        return default
    ratio = width / height
    if 0.98 <= ratio <= 1.02:
        return "1:1"
    return "16:9" if ratio > 1 else "9:16"


def aspect_label(file_path: Path | str, default: str = "9:16") -> str:
    """Return "9:16" / "16:9" / "1:1" for a media file's real dimensions.

    Callers persist this instead of assuming an aspect: clip extraction only
    reframes a LANDSCAPE source, so a square or 4:5 source passes through
    untouched and was still labelled 9:16 — and the Library renders the tile
    at the label's shape, not the file's. Returns *default* when the probe
    fails so a missing ffprobe never breaks a save.
    """
    w, h, _ = probe_media(file_path)
    if w <= 0 or h <= 0:
        # Loud, because it is not cosmetic: the caption engine takes this
        # label too, so a probe that quietly fails under load burns portrait
        # margins into a landscape video.
        logger.warning(
            "aspect probe failed for %s — falling back to %s; captions and the "
            "Library tile will use that shape", file_path, default,
        )
    return aspect_from_dims(w, h, default=default)


def cover_vf(target_w: int, target_h: int, setsar: bool = True) -> str:
    """FFmpeg filter chain that fills target_w x target_h without distortion.

    Scales up until BOTH dimensions are covered (`increase`), then centre-crops
    the overflow. This is the "cover" behaviour a CSS `object-fit: cover` gives
    you, and it is the only correct pre-step before `zoompan`: zoompan's crop
    window inherits the INPUT aspect and its `s=WxH` then anamorphically
    STRETCHES it, so anything reaching zoompan must already be at the target
    aspect.

    `setsar` is off for chains that continue into another filter which sets it.
    """
    chain = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h}"
    )
    return f"{chain},setsar=1" if setsar else chain


def probe_dimensions(file_path: Path | str) -> tuple[int, int]:
    """Get (width, height) for any media file, still images included.

    Deliberately NOT `probe_media`: that one asks for `format=duration` in the
    same call and a still image reports `N/A` there, so the float() fails and
    the whole probe degrades to (0, 0, 0.0) — an image looks like an
    unreadable file. Returns (0, 0) on real failure.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        parts = [p for p in result.stdout.strip().splitlines() if p.strip()]
        if len(parts) < 2:
            return 0, 0
        return int(parts[0]), int(parts[1])
    except Exception:
        return 0, 0


def ff_filter_path(p) -> str:
    r"""Quote a filesystem path for use as a filter option value inside an
    FFmpeg filtergraph (`ass=`, `subtitles=`, `drawtext=fontfile=/textfile=`).

    A filtergraph string is parsed TWICE: the graph parser splits filters on
    `,;[]` and consumes one level of backslash escapes, then the filter's own
    option parser splits the surviving text on `:` (and `=`). A Windows path
    therefore breaks at the drive colon — `ass=C:/x.ass` reads `C` as the
    filename and `/x.ass` as the next positional option (`original_size`) —
    and the single-escaped `C\:/x.ass` fails the SAME way, because the graph
    parser eats the backslash before the option parser ever sees it. That was
    the shipped form, so no caption was ever burned on Windows, where the data
    dir always carries a drive letter.

    The quoted form is the one that survives both passes for every filter:
    single quotes make the graph parser pass the text through verbatim
    (terminators AND backslashes included), so an option-level `\:` / `\=`
    reaches the option parser intact. A quote inside the path cannot live
    inside the quotes, so it closes them, emits a graph-level `\\\'` (which
    reaches the option parser as `\'`), and reopens. Backslashes are
    forward-slashed first, which every ffmpeg on Windows accepts.

    Proven against `ass`, `subtitles` and `drawtext` in both `-vf` and
    `-filter_complex`, with spaces, apostrophes, `=`, `[x],y;z` and two drive
    letters in the path — see tests/test_ffmpeg_filter_path.py, which re-runs
    the real binary when one is installed.
    """
    s = str(p).replace("\\", "/")
    s = s.replace(":", "\\:").replace("=", "\\=")
    s = s.replace("'", "'\\\\\\''")
    return f"'{s}'"
