# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Smart Video Studio — the project document.

Pure: no IO, no database, no clock. Everything that decides what a project
document IS lives here:

  * the enums and limits — ONE definition each (served at GET /studio/enums)
  * script → scene lines: sentence split, long-sentence split, re-packing
    until the script fits SCENE_MAX scenes; never truncation
  * `validate_doc` — turns untrusted JSON (a client write, an undo snapshot,
    an old row) into a clean document built from known fields only, or raises
    StudioDocError. Fields that point at stored files (take keys, your clips'
    asset ids, states) are strict; cosmetic settings fall back to defaults.
  * derived timing — a scene lasts as long as its voice take plus a short gap

`studio_ops` edits documents; this module defines them. A scene's visual is a
stock clip (bound from the planner's or a search's candidates) or one of your
own clips/stills uploaded to the project.
"""
from __future__ import annotations

import math
import re
import secrets
from collections.abc import Callable
from typing import Any

from backend.core.exceptions import StudioDocError
from backend.services.tts_service import (
    _CJK_CHAR_RANGES, DEFAULT_VOICES, TTSProvider, estimate_spoken_seconds, normalise_tts_provider, provider_traits,
)

DOC_VERSION = 1

# One voice take per scene: a line stays short enough that every take is a
# single TTS request.
LINE_MAX = 400
SCENE_MAX = 18
MIN_SCENE_SECONDS = 1.2
TAIL_GAP_DEFAULT = 0.15
TAIL_GAP_MAX = 0.6
FALLBACK_QUERY = "abstract light background"
HOOK_TEXT_MAX = 80                 # caption_service._HOOK_MAX_CHARS
HOOK_SECONDS = 2.5                 # how long the hook title stays on screen
HOOK_AUTO_MAX = 48                 # a title made from the first line: ≤ ~3 lines on a 9:16 frame

ASPECTS = ("9:16", "16:9", "1:1")
PROJECT_STATUSES = ("draft", "rendering", "rendered")
VOICE_STATES = ("none", "generating", "ready", "stale", "failed")
VISUAL_KINDS = ("stock", "user_clip")
VISUAL_STATES = ("bound", "placeholder", "failed")
# Which states each kind can be in. A stock placeholder is filled at render by
# searching its query; your own clip is always bound.
_STATES_FOR_KIND = {
    "stock": ("bound", "placeholder", "failed"),
    "user_clip": ("bound", "failed"),
}
FITS = ("loop", "freeze", "slow")
TRANSITIONS = ("none", "smooth", "dynamic", "punchy")
TTS_PROVIDERS = tuple(p.value for p in TTSProvider)
MUSIC_SOURCES = ("library",)
INPUT_MODES = ("topic", "script", "audio", "template", "sample", "draft", "source")
# The Smart Video look presets (frontend components/create/SmartVideoStyles.jsx STYLES ids).
VISUAL_STYLES = ("cinematic", "vlog", "card", "educational", "documentary", "noir", "neon", "vintage")

_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_TAKE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s*")


# ── small, total helpers ────────────────────────────────────────────────────

def new_uid() -> str:
    """A stable scene id (undo, job results and asset ownership key on it)."""
    return f"u{secrets.token_hex(6)}"


def normalise_line(text: Any) -> str:
    """Collapse whitespace and trim — the one normal form for lines and prompts."""
    return " ".join(str(text or "").split())


def _str(value: Any, limit: int, default: str | None = None) -> str | None:
    if not isinstance(value, str):
        return default
    value = value.strip()
    return value[:limit] if value else default


def _num(value: Any, lo: float, hi: float, default: float | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(min(hi, max(lo, value)))


def _filename(value: Any) -> str | None:
    """A music-library filename: a bare name, never a path."""
    name = _str(value, 255)
    if not name or "/" in name or "\\" in name or name.startswith(".") or "\x00" in name:
        return None
    return name


# ── script → scene lines ────────────────────────────────────────────────────

def split_sentences(script: Any) -> list[str]:
    """Split on sentence enders, keeping the punctuation (CJK full stops too)."""
    text = normalise_line(script)
    return [p.strip() for p in _SENTENCE_SPLIT.split(text) if p.strip()]


def _split_long(sentence: str) -> list[str]:
    """A sentence longer than one scene line breaks at word boundaries, or is
    hard-cut when the script has no spaces (CJK). Never truncated."""
    if len(sentence) <= LINE_MAX:
        return [sentence]
    if not re.search(r"\s", sentence):
        return [sentence[i:i + LINE_MAX] for i in range(0, len(sentence), LINE_MAX)]
    out: list[str] = []
    cur = ""
    for word in sentence.split():
        nxt = f"{cur} {word}" if cur else word
        if len(nxt) > LINE_MAX and cur:
            # `cur` can itself be one over-long token (a URL); flushing it
            # whole would let make_scene cut it at LINE_MAX and drop the
            # rest. Split it the same way — hard-cut when it has no spaces.
            out.extend(_split_long(cur))
            cur = word
        else:
            cur = nxt
    if cur:
        out.extend(_split_long(cur))
    return out


# The shortest scene a short script packs to, in spoken-length units (~4 s) —
# Shorts cut every 2–4 s. A long script still packs up to fit SCENE_MAX.
SCENE_TARGET_MIN_CHARS = 60
# Scenes are packed by how long they take to SAY, not by raw characters: one
# CJK character is spoken in ~1/3.5 s, one Latin character in ~1/15 s (2.5
# words/s). Counting raw characters made a 60-char Chinese scene ~17 s long —
# a slideshow. Mirrored by studioModel.js `spokenUnits`.
CJK_CHAR_WEIGHT = 4.3


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_CHAR_RANGES) or 0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF


def spoken_units(text: str) -> float:
    """A line's length in Latin-character equivalents of speaking time."""
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return (len(text) - cjk) + cjk * CJK_CHAR_WEIGHT


def join_sentences(a: str, b: str) -> str:
    """Two sentences as one line: CJK text joins without a space."""
    if not a:
        return b
    if not b:
        return a
    return f"{a}{b}" if _is_cjk(a[-1]) and _is_cjk(b[0]) else f"{a} {b}"


def _group(sentences: list[str], target: float | None = None) -> list[str]:
    """One or two sentences per scene (~2.5–6 s spoken); a long script packs more."""
    total = sum(spoken_units(s) for s in sentences)
    t = target if target is not None else max(SCENE_TARGET_MIN_CHARS, math.ceil(total / 18))
    max_sentences = 99 if t > 160 else 2
    lines: list[str] = []
    cur = ""
    count = 0
    for s in sentences:
        nxt = join_sentences(cur, s)
        if cur and (spoken_units(nxt) > t or len(nxt) > LINE_MAX or count >= max_sentences):
            lines.append(cur)
            cur = s
            count = 1
        else:
            cur = nxt
            count += 1
    if cur:
        lines.append(cur)
    return lines


def pack_scenes(sentences: list[str]) -> list[str]:
    """Scene lines for a script, re-packed with a growing target until they fit
    SCENE_MAX. Returns more than SCENE_MAX only when even full 400-char scenes
    can't hold the script — the caller refuses that rather than dropping text."""
    parts = [p for s in sentences for p in _split_long(s)]
    lines = _group(parts)
    target = max(SCENE_TARGET_MIN_CHARS, math.ceil(sum(spoken_units(p) for p in parts) / 18))
    ceiling = LINE_MAX * CJK_CHAR_WEIGHT
    while len(lines) > SCENE_MAX and target < ceiling:
        target = min(ceiling, math.ceil(target * 1.25))
        lines = _group(parts, target)
    return lines


def plan_lines(script: Any) -> list[str]:
    return pack_scenes(split_sentences(script))


def lines_for_new_project(script: Any) -> list[str]:
    lines = plan_lines(script)
    if not lines:
        raise StudioDocError("Write at least one sentence to build a storyboard.")
    if len(lines) > SCENE_MAX:
        raise StudioDocError(
            "This script is too long for one video (about 2½ minutes at most). "
            "Shorten it or split it into two videos.")
    return lines


def hook_title(doc: dict) -> str | None:
    """The on-screen title for the video's first seconds, or None: the user's
    own text, else the first spoken line (a script's hook sentence)."""
    captions = (doc.get("settings") or {}).get("captions") or {}
    if not captions.get("enabled") or not captions.get("hook"):
        return None
    text = normalise_line(captions.get("hook_text"))
    if text:
        return clip_title(text) or None
    first = next((normalise_line(s.get("line")) for s in doc.get("scenes") or [] if normalise_line(s.get("line"))), "")
    return auto_title(first) or None


_CLAUSE_END = re.compile(r"[,;:!?.。！？，；：—]")


def auto_title(line: str) -> str:
    """The title a first line makes on its own: its first sentence when that
    fits HOOK_AUTO_MAX, else its opening clause. A whole 100-char line as a
    title covered most of the frame in five lines; but always cutting at the
    first comma lost the payoff — "Your bread isn't the problem, your yeast
    is." became "Your bread isn't the problem"."""
    line = normalise_line(line)
    first = split_sentences(line)[0] if line else line
    if len(first) <= HOOK_AUTO_MAX:
        return first[:-1] if first[-1:] in ".。" and len(first) > 1 else first
    m = _CLAUSE_END.search(line)
    if m and m.start() >= 12:
        line = line[:m.start()] if line[m.start()] not in "!?！？" else line[:m.start() + 1]
    return clip_title(line, HOOK_AUTO_MAX)


def hook_is_auto(doc: dict) -> bool:
    """True when the opening title is the first line's own words (no title of
    the user's) — then the captions under it would say the same words twice,
    so they wait until the title is gone (render + preview)."""
    captions = (doc.get("settings") or {}).get("captions") or {}
    return bool(captions.get("enabled") and captions.get("hook") and not normalise_line(captions.get("hook_text"))
                and hook_title(doc))


def normalise_hook_text(value: Any) -> str | None:
    """A user's own opening-title text in its stored form: whitespace
    collapsed, cut to HOOK_TEXT_MAX, blank → None (= the first line). The one
    rule for the Inspector's `captions.hook_text` op and the legacy door."""
    if not isinstance(value, str):
        return None
    return normalise_line(value)[:HOOK_TEXT_MAX] or None


def hook_overrides(hook: Any = None, hook_text: Any = None) -> dict:
    """The `captions` fields the legacy door (/generate/stock, MCP) carries into
    a new project for the opening title.
    `hook=None` says nothing — the project default (on) applies; only a real
    bool is sent. `hook_text` is kept only when non-blank."""
    out: dict = {}
    if isinstance(hook, bool):
        out["hook"] = hook
    text = normalise_hook_text(hook_text)
    if text:
        out["hook_text"] = text
    return out


def clip_title(text: str, limit: int = HOOK_TEXT_MAX) -> str:
    """`text` within `limit` chars, cut at the last word boundary with "…" —
    a hard cut left "…until the end of" on screen.
    Text without spaces (CJK) is cut at the limit."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit - 1)
    if cut < int(limit * 0.6):
        cut = limit - 1
    return text[:cut].rstrip(" ,;:.—-") + "…"


def estimate_line_seconds(line: str) -> float:
    """What an unvoiced line is shown as (estimated, until it is voiced)."""
    return max(MIN_SCENE_SECONDS, float(math.ceil(estimate_spoken_seconds(line))))


# ── building blocks ─────────────────────────────────────────────────────────

POST_TOGGLES = ("auto_zoom", "sfx")
from backend.services.render_post import EMOJI_STYLES as CAPTION_EMOJI_STYLES  # noqa: E402 — one list


def default_post() -> dict:
    return {**{key: None for key in POST_TOGGLES}, "caption_emoji": None}


def normalise_post(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = {key: raw.get(key) if isinstance(raw.get(key), bool) else None for key in POST_TOGGLES}
    out["caption_emoji"] = raw.get("caption_emoji") if raw.get("caption_emoji") in CAPTION_EMOJI_STYLES else None
    return out


def default_settings(**overrides: Any) -> dict:
    settings = {
        "visual_style": "cinematic",
        # New projects start on the free local voice.
        "voice": {
            "provider": TTSProvider.EDGE_TTS.value,
            "voice": DEFAULT_VOICES[TTSProvider.EDGE_TTS],
            "label": "Andrew", "model": None, "instructions": None,
        },
        # `urban` + the hook title: bold 3-word captions and the first line as an
        # on-screen title for its first seconds.
        "captions": {"enabled": True, "style": "urban", "hook": True, "hook_text": None},
        "music": {
            "enabled": True, "source": "library", "track": None, "track_name": None,
            "genre": "lofi", "volume_db": -14.0, "offset_s": 0.0, "fade_s": 0.5,
        },
        "transition": "none",
        "tail_gap_s": TAIL_GAP_DEFAULT,
        # Finishing steps (render_post): each None = follow the user's
        # Settings at render; True/False = this project's own choice. Sound
        # effects start OFF for a new project (the effects are synthesised
        # tones — they read as amateur on a first video); the Finishing panel
        # turns them on.
        "post": {**default_post(), "sfx": False},
    }
    # Groups merge per field: a caller carrying the user's remembered captions
    # {enabled, style} must not wipe a default it doesn't know about (the hook
    # title) — the same merge studioApi.mergeRemembered does.
    for key, value in overrides.items():
        if key in _MERGED_GROUPS and isinstance(value, dict) and isinstance(settings.get(key), dict):
            settings[key] = {**settings[key], **value}
        else:
            settings[key] = value
    return settings


_MERGED_GROUPS = frozenset({"captions", "music", "post"})


def stock_visual(query: str | None, prev: dict | None = None, *, bind_top: bool = True) -> dict:
    """An unpinned stock visual for `query`.

    With `bind_top`, the scene's best remaining candidate (alternates[0]) is
    bound again, so a scene a removed clip sends back to stock gets the footage
    it had. Without it (split) the scene is a placeholder that the render fills
    by searching `query`.
    """
    prev = prev or {}
    alternates = [a for a in (prev.get("alternates") or []) if isinstance(a, dict)][:5]
    top = alternates[0] if (bind_top and alternates) else None
    return {
        "kind": "stock",
        "state": "bound" if top else "placeholder",
        "query": _str(query, 100) or _str(prev.get("query"), 100) or FALLBACK_QUERY,
        "asset": dict(top) if top else None,
        "alternates": alternates,
        "fit": prev.get("fit") if prev.get("fit") in FITS else "loop",
        "pinned": False,
        "reason": None,
    }


def make_scene(line: str, *, query: str | None = None, mood: str | None = None,
               visual: dict | None = None) -> dict:
    return {
        "uid": new_uid(),
        "line": normalise_line(line)[:LINE_MAX],
        "voice": {"state": "none"},
        "visual": visual or stock_visual(query, bind_top=False),
        "mood": _str(mood, 20),
    }


def is_fixed_visual(scene: dict) -> bool:
    """A scene planning may not reassign: the user chose it (pinned) or it is
    their own clip."""
    v = scene.get("visual") or {}
    return bool(v.get("pinned")) or v.get("kind") == "user_clip"


def voice_identity(settings: dict) -> tuple:
    """What makes two voices the same voice (a change re-records every take)."""
    v = (settings or {}).get("voice") or {}
    return (v.get("provider"), v.get("voice"), v.get("model"), v.get("instructions"))


# ── derived timing ──────────────────────────────────────────────────────────

def scene_seconds(scene: dict, tail_gap: float) -> tuple[float, bool]:
    """(duration, estimated). Only a READY take times a scene — a stale take
    is the OLD line, so its length says nothing about the new one."""
    voice = scene.get("voice") or {}
    if voice.get("state") == "ready" and voice.get("duration"):
        return float(voice["duration"]) + tail_gap, False
    return estimate_line_seconds(scene.get("line") or "") + tail_gap, True


# A stock scene this long cuts to a second clip halfway through: one shot held
# 5+ s reads as a slideshow, and the planner already found up to 5 alternates
# per scene. The preview shows the same cut (studioModel.secondShots).
SECOND_SHOT_MIN_S = 5.0


def second_shots(scenes: list[dict], durations: list[float]) -> dict[str, dict]:
    """{scene uid: {"asset": alternate, "cut": seconds into the scene}} for every
    long, unpinned, bound stock scene with an alternate no other scene shows."""
    used = {(v.get("asset") or {}).get("provider_id") for v in (sc.get("visual") or {} for sc in scenes)
            if v.get("kind") == "stock" and v.get("asset")}
    out: dict[str, dict] = {}
    for sc, dur in zip(scenes, durations):
        v = sc.get("visual") or {}
        asset = v.get("asset") or {}
        if (v.get("kind") != "stock" or v.get("state") != "bound" or not asset or v.get("pinned")
                or dur < SECOND_SHOT_MIN_S):
            continue
        alt = next((a for a in v.get("alternates") or []
                    if a.get("provider_id") and a.get("download_url") and a["provider_id"] not in used), None)
        if alt is None:
            continue
        used.add(alt["provider_id"])
        out[sc["uid"]] = {"asset": alt, "cut": round(dur / 2, 2)}
    return out


def derive_timeline(doc: dict) -> dict:
    gap = float((doc.get("settings") or {}).get("tail_gap_s", TAIL_GAP_DEFAULT))
    starts, durations, estimated = [], [], []
    t = 0.0
    for scene in doc.get("scenes") or []:
        dur, est = scene_seconds(scene, gap)
        starts.append(round(t, 3))
        durations.append(round(dur, 3))
        estimated.append(est)
        t += dur
    return {"starts": starts, "durations": durations, "estimated": estimated, "total": round(t, 3)}


# ── validation ──────────────────────────────────────────────────────────────

_ASSET_STR_FIELDS = {
    "asset_id": 60, "provider": 20, "provider_id": 60,
    "download_url": 2000, "preview_url": 2000, "preview_asset_id": 60, "thumbnail": 2000,
    "creator": 120, "license": 60, "media": 10, "query": 100, "title": 200,
}
_ASSET_NUM_FIELDS = {
    "native_duration": (0, 36000), "width": (0, 16384), "height": (0, 16384),
}


def _asset(raw: Any) -> dict | None:
    """Known fields only. URLs must be http(s) or our own /api/ paths — a
    document never carries a file path or a javascript:/file: URL."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for key, limit in _ASSET_STR_FIELDS.items():
        value = _str(raw.get(key), limit)
        if value is None:
            continue
        if (key.endswith("url") or key == "thumbnail") and not value.startswith(("https://", "http://", "/api/")):
            continue
        out[key] = value
    for key, (lo, hi) in _ASSET_NUM_FIELDS.items():
        value = raw.get(key)
        if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and lo <= value <= hi:
            out[key] = value
    return out or None


def _voice(raw: Any, line: str, *, take_known: Callable[[str], bool] | None, settle: bool, where: str) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    state = raw.get("state") or "none"
    if state not in VOICE_STATES:
        raise StudioDocError(f"{where}: unknown voice state {state!r}")
    if not line:
        return {"state": "none"}          # a scene with no line has no take
    key = raw.get("take_key") if isinstance(raw.get("take_key"), str) and _TAKE_KEY_RE.match(raw["take_key"]) else None
    if state == "generating" and settle:
        state = "stale" if key else "none"
    if state in ("ready", "stale"):
        if not key:
            raise StudioDocError(f"{where}: a {state} voice has no take")
        if take_known is not None and not take_known(key):
            raise StudioDocError(f"{where}: unknown voice take")
    out: dict = {"state": state}
    if key and state in ("ready", "stale", "generating"):
        out["take_key"] = key
    duration = _num(raw.get("duration"), 0.05, 120, None)
    if duration and "take_key" in out:
        out["duration"] = duration
    if raw.get("cached") is True:
        out["cached"] = True
    # The voice a stale take was recorded in (studio_ops._voice_moved). Omitted otherwise.
    take_voice = raw.get("take_voice")
    if (state == "stale" and "take_key" in out and isinstance(take_voice, list) and len(take_voice) == 4
            and all(v is None or isinstance(v, str) for v in take_voice)):
        out["take_voice"] = take_voice
    if state == "failed":
        out["error"] = _str(raw.get("error"), 300)
    return out


def _visual(raw: Any, *, where: str) -> dict:
    if not isinstance(raw, dict):
        raise StudioDocError(f"{where}: no visual")
    kind = raw.get("kind")
    if kind not in VISUAL_KINDS:
        raise StudioDocError(f"{where}: unknown visual kind {kind!r}")
    state = raw.get("state")
    if state not in _STATES_FOR_KIND[kind]:
        raise StudioDocError(f"{where}: {state!r} is not a valid state for a {kind} visual")
    asset = _asset(raw.get("asset"))
    if kind == "user_clip" and not (asset or {}).get("asset_id"):
        raise StudioDocError(f"{where}: your clip has no asset")
    if kind == "stock" and state == "bound" and not asset:
        state = "placeholder"             # nothing bound — the render searches `query`
    if kind == "stock" and state == "placeholder":
        asset = None
    alternates_raw = raw.get("alternates") if isinstance(raw.get("alternates"), list) else []
    alternates = [a for a in (_asset(x) for x in alternates_raw[:5]) if a]
    return {
        "kind": kind,
        "state": state,
        "query": _str(raw.get("query"), 100),
        "asset": asset,
        "alternates": alternates,
        "fit": raw.get("fit") if raw.get("fit") in FITS else "loop",
        "pinned": raw.get("pinned") is True,
        "reason": _str(raw.get("reason"), 200),
        # The line this visual was planned for: a scene whose line changed
        # since shows "planned for an earlier line" until it is re-planned.
        "planned_for": _str(raw.get("planned_for"), LINE_MAX),
    }


def _settings(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise StudioDocError("the project has no settings")
    base = default_settings()
    voice_raw = raw.get("voice") if isinstance(raw.get("voice"), dict) else {}
    provider = normalise_tts_provider(voice_raw.get("provider"))
    if provider is None:
        # An unsupported provider gives way to the free default voice WHOLE:
        # keeping its voice id would have Edge asked to speak as, say, a
        # cloned-voice id — every take would fail.
        provider, voice_raw = base["voice"]["provider"], dict(base["voice"])
    captions_raw = raw.get("captions") if isinstance(raw.get("captions"), dict) else {}
    music_raw = raw.get("music") if isinstance(raw.get("music"), dict) else {}
    bm = base["music"]
    return {
        "visual_style": raw.get("visual_style") if raw.get("visual_style") in VISUAL_STYLES else base["visual_style"],
        "voice": {
            "provider": provider,
            "voice": _str(voice_raw.get("voice"), 120) or DEFAULT_VOICES[TTSProvider(provider)],
            "label": _str(voice_raw.get("label"), 120),
            "model": _str(voice_raw.get("model"), 60),
            "instructions": _str(voice_raw.get("instructions"), 300),
        },
        # "none" is the legacy "no captions" sentinel: the ASS builder maps an
        # unknown style to `viral`, so it must turn captions OFF here, before
        # anything reaches the renderer.
        "captions": {
            "enabled": captions_raw.get("enabled") is not False and _str(captions_raw.get("style"), 40) != "none",
            "style": (_str(captions_raw.get("style"), 40) or "viral") if _str(captions_raw.get("style"), 40) != "none" else "viral",
            # Absent = no hook title.
            "hook": captions_raw.get("hook") is True,
            "hook_text": _str(captions_raw.get("hook_text"), HOOK_TEXT_MAX),
        },
        "music": {
            "enabled": music_raw.get("enabled") is not False,
            "source": "library",
            "track": _filename(music_raw.get("track")),
            "track_name": _str(music_raw.get("track_name"), 200),
            "genre": _str(music_raw.get("genre"), 40) or bm["genre"],
            "volume_db": _num(music_raw.get("volume_db"), -30, 0, bm["volume_db"]),
            "offset_s": _num(music_raw.get("offset_s"), 0, 600, bm["offset_s"]),
            "fade_s": _num(music_raw.get("fade_s"), 0, 5, bm["fade_s"]),
        },
        "transition": raw.get("transition") if raw.get("transition") in TRANSITIONS else "none",
        "tail_gap_s": _num(raw.get("tail_gap_s"), 0, TAIL_GAP_MAX, TAIL_GAP_DEFAULT),
        "post": normalise_post(raw.get("post")),
    }


def _input(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "mode": raw.get("mode") if raw.get("mode") in INPUT_MODES else "script",
        "topic": _str(raw.get("topic"), 200),
        "title": _str(raw.get("title"), 120),
        "sample_id": _str(raw.get("sample_id"), 60),
        "source_id": _str(raw.get("source_id"), 60),
        "audio_id": _str(raw.get("audio_id"), 60),
        "template_id": _str(raw.get("template_id"), 60),
    }


def validate_doc(
    raw: Any,
    *,
    take_known: Callable[[str], bool] | None = None,
    settle: bool = True,
) -> dict:
    """A clean project document from untrusted JSON, or StudioDocError.

    Args:
        raw: the candidate document.
        take_known: when given, every ready/stale take key must pass it (a
            document may not invent a voice take).
        settle: turn in-flight states into settled ones (no job is running for
            a restored snapshot): a generating voice → stale/none. Off for the
            server's own writes.
    """
    if not isinstance(raw, dict):
        raise StudioDocError("not a project document")
    scenes_raw = raw.get("scenes")
    if not isinstance(scenes_raw, list) or not scenes_raw:
        raise StudioDocError("a project needs at least one scene")
    if len(scenes_raw) > SCENE_MAX:
        raise StudioDocError(f"a video can have at most {SCENE_MAX} scenes")
    seen: set[str] = set()
    scenes = []
    for i, scene in enumerate(scenes_raw):
        where = f"scene {i + 1}"
        if not isinstance(scene, dict):
            raise StudioDocError(f"{where}: not a scene")
        uid = scene.get("uid")
        if not isinstance(uid, str) or not _UID_RE.match(uid) or uid in seen:
            raise StudioDocError(f"{where}: missing or duplicate scene id")
        seen.add(uid)
        if scene.get("line") is not None and not isinstance(scene.get("line"), str):
            raise StudioDocError(f"{where}: the line must be text")
        line = normalise_line(scene.get("line"))
        if len(line) > LINE_MAX:
            raise StudioDocError(f"{where}: a line can be at most {LINE_MAX} characters — split this scene")
        scenes.append({
            "uid": uid,
            "line": line,
            "voice": _voice(scene.get("voice"), line, take_known=take_known, settle=settle, where=where),
            "visual": _visual(scene.get("visual"), where=where),
            "mood": _str(scene.get("mood"), 20),
        })
    return {
        "v": DOC_VERSION,
        "aspect_ratio": raw.get("aspect_ratio") if raw.get("aspect_ratio") in ASPECTS else "9:16",
        "input": _input(raw.get("input")),
        "settings": _settings(raw.get("settings")),
        "scenes": scenes,
    }


def enums() -> dict:
    """Every Studio enum, for GET /api/studio/enums."""
    return {
        "aspects": list(ASPECTS), "project_statuses": list(PROJECT_STATUSES),
        "voice_states": list(VOICE_STATES), "visual_kinds": list(VISUAL_KINDS),
        "visual_states": list(VISUAL_STATES), "fits": list(FITS), "transitions": list(TRANSITIONS),
        "tts_providers": list(TTS_PROVIDERS),
        "music_sources": list(MUSIC_SOURCES), "visual_styles": list(VISUAL_STYLES),
        "limits": {"line_max": LINE_MAX, "scene_max": SCENE_MAX, "tail_gap_max": TAIL_GAP_MAX},
        # Per provider: needs a key, spends that key per take (a first draft
        # never auto-voices with such a provider) — one answer every screen uses.
        "voice_providers": provider_traits(),
        "post_toggles": list(POST_TOGGLES), "caption_emoji_styles": list(CAPTION_EMOJI_STYLES),
    }


# A starting stock query per line until the planner binds real candidates.
_STOP = frozenset((
    "the a an and or but if then than that this these those with without into onto from your you our we they "
    "them their there here what when where which who whom why how is are was were be been being have has had do "
    "does did not no yes just very really more most less least much many some any every each one two three four "
    "five six seven eight nine ten first second third next last also only even still about over under again once "
    "all can will would should could may might must its it's i'm you're don't dont doesn't isn't aren't wasn't "
    "let's lets get got make made take takes took going thing things stuff way ways right like know people "
    "person time day days week year years"
).split())


def query_from_line(line: str) -> str:
    words = "".join(ch.lower() if ch.isalnum() or ch in " -" else " " for ch in line).split()
    picked: list[str] = []
    for word in words:
        # ASCII only: a CJK sentence has no spaces, so it would come out as
        # ONE "word" — the whole line as a Pexels query.
        if len(word) > 3 and word.isascii() and word not in _STOP and word not in picked:
            picked.append(word)
        if len(picked) == 3:
            break
    return " ".join(picked) or FALLBACK_QUERY
