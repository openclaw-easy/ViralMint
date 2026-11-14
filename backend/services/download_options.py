# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Per-download yt-dlp options — the single source of truth.

WHY THIS MODULE EXISTS
Downloading is the most load-bearing feature in the app, and it is reached
from several places: the Video Download tool page, the chat planner's
`download_url` action, channel batch downloads, and the internal scout path.
Letting each one build its own yt-dlp option dict is how the download ladder
gets bypassed and how the surfaces drift apart on what "1080p" means. So the
vocabulary, the validation and the translation to yt-dlp live here once, and
every caller takes the same road.

THE SAFETY PROPERTY THAT SHAPES EVERYTHING BELOW
`normalize(None)` and `normalize({})` return {}, and `format_chain(None)`
returns None, which callers MUST treat as "use the existing
FORMAT_FALLBACK_CHAIN unchanged". A caller that passes no options gets a
byte-identical download to what shipped before this module existed. Options
are strictly opt-in; nothing here can regress the default path.

The second property: a quality request must never turn a download that would
have worked into a failure. Every capped ladder therefore ENDS in the same
unconstrained rungs the default chain uses. If a source has nothing at or
below the requested cap, the user gets a larger file rather than an error —
and `download_video` reports the height it actually delivered so the UI can
say so instead of quietly implying the cap was honoured.

NOT HANDLED HERE, ON PURPOSE
There is no "audio only" or "subtitles only" quality. `download_video`
already extracts a companion .mp3 next to every video for the transcript
pipeline, and the Export Subtitles tool already turns a video into an
.srt/.vtt. This module covers options that modify the VIDEO download —
including embedding subtitles/thumbnail/metadata INTO the container, which no
existing path does.
"""

from __future__ import annotations

import re

# Requested max height → the ladder of yt-dlp format selectors to try.
#
# Each ladder is capped rungs first, then the unconstrained tail so a cap can
# never cause an outright failure (see module docstring). "best" has no capped
# rung at all — it IS the uncapped tail.
#
# Note these deliberately do NOT restate `format_sort`: that lives in
# `_yt_dlp_base_opts()` and applies uniformly. Format SELECTORS gate
# eligibility; format SORT picks the best of what's eligible. Both are needed.
_UNCAPPED_TAIL = ["bestvideo+bestaudio/best", "best"]

QUALITY_LADDERS: dict[str, list[str]] = {
    "best": list(_UNCAPPED_TAIL),
    "2160p": ["bestvideo[height<=2160]+bestaudio/best[height<=2160]", *_UNCAPPED_TAIL],
    "1440p": ["bestvideo[height<=1440]+bestaudio/best[height<=1440]", *_UNCAPPED_TAIL],
    "1080p": ["bestvideo[height<=1080]+bestaudio/best[height<=1080]", *_UNCAPPED_TAIL],
    "720p":  ["bestvideo[height<=720]+bestaudio/best[height<=720]",   *_UNCAPPED_TAIL],
    "480p":  ["bestvideo[height<=480]+bestaudio/best[height<=480]",   *_UNCAPPED_TAIL],
}

QUALITY_CHOICES = tuple(QUALITY_LADDERS)

#: How subtitles are attached. "off" = untouched (today's behaviour — note the
#: downloader ALREADY writes sidecar subs for its own transcript pipeline, so
#: "off" does not mean "no subtitle files exist"; it means the user asked for
#: nothing extra). "embed" muxes them into the container as a selectable
#: track. "file" keeps the sidecar .srt next to the video and leaves it there.
SUBTITLE_MODES = ("off", "embed", "file")

#: Container to remux into. None = whatever yt-dlp's merge produced.
CONTAINER_CHOICES = ("mp4", "mkv")

_MAX_SUB_LANGS = 5

#: A subtitle language code, and nothing else. yt-dlp's `subtitleslangs`
#: accepts REGEXES — ".*" pulls every track a video has, which is the exact
#: fan-out the "all" rejection below exists to prevent, just spelled
#: differently. Since these values can come from an LLM, shape-check them
#: rather than blacklisting the spellings we happened to think of.
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")


def normalize(raw: dict | None) -> dict:
    """Validate + canonicalize a raw options dict from any surface.

    Unknown keys are DROPPED rather than rejected: these dicts can arrive from
    an LLM (the chat planner) as well as from our own UI, and failing a whole
    download because a model invented `--audio-quality` would be a worse
    outcome than ignoring it. Invalid VALUES for known keys are also dropped,
    for the same reason — the result is a plain download, never an exception.

    Returns {} when nothing usable was supplied, which every caller reads as
    "default path, untouched".
    """
    if not isinstance(raw, dict):
        return {}

    out: dict = {}

    q = str(raw.get("quality") or "").strip().lower()
    # Accept the bare numbers people actually type ("1080", "4k") as well as
    # the canonical ids — an LLM will produce both.
    q = {"4k": "2160p", "2k": "1440p", "2160": "2160p", "1440": "1440p",
         "1080": "1080p", "720": "720p", "480": "480p",
         "highest": "best", "max": "best"}.get(q, q)
    if q in QUALITY_LADDERS:
        out["quality"] = q

    subs = str(raw.get("subtitles") or "").strip().lower()
    if subs in SUBTITLE_MODES and subs != "off":
        out["subtitles"] = subs
        langs = raw.get("sub_langs")
        if isinstance(langs, str):
            langs = [x.strip() for x in langs.split(",")]
        if isinstance(langs, (list, tuple)):
            clean = [str(x).strip().lower() for x in langs if str(x).strip()]
            # Cap the fan-out: "all" (or a ".*" regex — yt-dlp accepts those
            # here) on a heavily-subtitled YouTube video pulls 100+ sidecar
            # files and slows the download for no user benefit.
            # ("all" is itself a valid-looking 3-letter code, so the shape
            # check can't catch it — it needs naming.)
            clean = [x for x in clean
                     if x != "all" and _LANG_RE.match(x)][:_MAX_SUB_LANGS]
            if clean:
                out["sub_langs"] = clean

    container = str(raw.get("container") or "").strip().lower()
    if container in CONTAINER_CHOICES:
        out["container"] = container

    for flag in ("thumbnail", "metadata"):
        if raw.get(flag) is True:
            out[flag] = True

    return out


def format_chain(options: dict | None) -> list[str] | None:
    """Format-selector ladder for these options, or None for the default.

    None is meaningful: it tells `download_video` to use its module-level
    FORMAT_FALLBACK_CHAIN untouched, which is what every pre-existing caller
    gets. Do not "helpfully" return the 720p ladder here — that ladder is
    similar to the default but not identical, and silently swapping it would
    change behaviour for callers that asked for nothing.
    """
    opts = options or {}
    return QUALITY_LADDERS.get(opts.get("quality")) if opts.get("quality") else None


def ydl_overrides(options: dict | None) -> dict:
    """yt-dlp option overrides for everything that is not format selection.

    Returns {} for empty options. Postprocessors are appended by the caller so
    they compose with whatever the download path already installed, rather
    than clobbering it.
    """
    # normalize() is idempotent, so calling it here is safe whether the caller
    # passed a raw dict or one this module already cleaned.
    opts = normalize(options)
    if not opts:
        return {}

    over: dict = {}
    post: list[dict] = []

    subs = opts.get("subtitles")
    if subs:
        langs = opts.get("sub_langs") or ["en"]
        over["writesubtitles"] = True
        # Auto-generated captions are the only ones most videos have. Without
        # this, "subtitles: on" silently produces nothing on the majority of
        # YouTube URLs — which reads as a broken toggle, not a missing source.
        over["writeautomaticsub"] = True
        over["subtitleslangs"] = langs
        if subs == "embed":
            over["subtitlesformat"] = "srt/best"
            post.append({"key": "FFmpegEmbedSubtitle"})

    if opts.get("thumbnail"):
        over["writethumbnail"] = True
        post.append({"key": "EmbedThumbnail"})

    if opts.get("metadata"):
        # add_chapters mirrors --embed-chapters; the two ride one postprocessor.
        post.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})

    # Container is a PREFERENCE, never a mandate. yt-dlp's merge_output_format
    # takes a '/'-separated preference list; a bare "mp4" would FORCE codec
    # pairs that can't be stream-copied into mp4 (vp9+opus — YouTube's usual
    # bestvideo+bestaudio pick) into the container anyway and fail ffmpeg's
    # merge, turning a working download into an error. "mp4/mkv" gives mp4
    # whenever the codecs allow and falls back to mkv otherwise — the same
    # degrade-don't-fail contract the quality ladders follow. The delivered
    # extension is reported back (`ext` in the download summary) so the UI can
    # say "you asked for mp4, this one needed mkv" instead of implying the
    # request was honoured.
    container = opts.get("container")
    if container:
        over["merge_output_format"] = container if container == "mkv" else f"{container}/mkv"
    elif opts.get("thumbnail"):
        # No container chosen but cover-art embedding requested: EmbedThumbnail
        # hard-raises on webm (yt-dlp supports mp3/mkv/mka/ogg/m4a/mp4/mov
        # only), and an unconstrained YouTube merge routinely produces webm.
        # Steer merges toward containers that can hold the art. Single-stream
        # downloads (no merge) can still land on webm — the postprocessor-drop
        # recovery in the download ladder (policy.decide's POSTPROCESSING
        # row) catches that case.
        over["merge_output_format"] = "mp4/mkv"

    if post:
        over["_vm_postprocessors"] = post
    return over
