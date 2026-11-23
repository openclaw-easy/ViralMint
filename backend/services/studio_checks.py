# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Smart Video Studio — what blocks a render, and what's worth knowing first.

One function answers both for every surface: the editor's Render dialog
reads `checks` from the project envelope, and the render endpoint refuses
with the same first blocker — so the button and the server can't disagree.

  * blockers: a scene with no line, a scene with no usable visual, anything
    still generating.
  * warnings: lines changed since they were voiced, scenes not voiced yet
    (both voiced at render), a music bed on with no track picked.

Pure: the take cache is passed in (`take_known`) — it decides whether a `ready`
scene's own take still exists (a cleared cache means it is re-voiced).
"""
from __future__ import annotations

from collections.abc import Callable


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _scene_list(scenes: list[dict], doc: dict) -> str:
    index = {s["uid"]: i for i, s in enumerate(doc["scenes"])}
    return ", ".join(str(index[s["uid"]] + 1) for s in scenes)


def recorded_lost(scene: dict, take_known: Callable[[str], bool]) -> bool:
    """The scene says `ready`, but the take it recorded is gone from disk."""
    voice = scene.get("voice") or {}
    return voice.get("state") == "ready" and not take_known(voice.get("take_key"))


def effective_voice_state(scene: dict, take_known: Callable[[str], bool]) -> str:
    """A `ready` scene whose own take is gone is really unvoiced."""
    return "none" if recorded_lost(scene, take_known) else (scene.get("voice") or {}).get("state", "none")


def needs_voice(scene: dict, take_known: Callable[[str], bool]) -> bool:
    """Voice all / Voice this scene records it: has a line, and is unvoiced,
    stale, failed, or ready with its take lost. Never a cut of the user's own
    audio: that is the recording, not a voice to record."""
    if (scene.get("voice") or {}).get("provider") == "own_audio":
        return False
    return bool(scene.get("line")) and effective_voice_state(scene, take_known) in ("none", "stale", "failed")


def compute_checks(doc: dict, take_known: Callable[[str], bool]) -> dict:
    scenes = doc["scenes"]
    own = [s for s in scenes if (s.get("voice") or {}).get("provider") == "own_audio"]
    spoken = [s for s in scenes if s["line"] and s not in own]
    blockers: list[str] = []
    warnings: list[str] = []

    if not scenes:
        blockers.append("This video has no scenes yet")
    lost = [s for s in own if recorded_lost(s, take_known)]
    if lost:
        # Never voiced instead: a video made from your audio speaks only in your audio.
        blockers.append(f"Scene {_scene_list(lost, doc)}'s audio {_plural(len(lost), 'is', 'are')} no longer "
                        "on this computer — start a new video from your recording")

    empty = [s for s in scenes if not s["line"]]
    if empty:
        blockers.append(f"Scene {_scene_list(empty, doc)} {_plural(len(empty), 'has', 'have')} no line yet "
                        "— write it or delete the scene")
    failed = [s for s in scenes if s["visual"]["state"] == "failed"]
    if failed:
        blockers.append(f"Scene {_scene_list(failed, doc)} {_plural(len(failed), 'has', 'have')} no usable visual "
                        "— replace it")
    busy = [s for s in scenes if s["voice"]["state"] == "generating"]
    if busy:
        blockers.append(f"Scene {_scene_list(busy, doc)} {_plural(len(busy), 'is', 'are')} still being voiced "
                        "— wait for it to finish")

    stale = sum(1 for s in spoken if s["voice"]["state"] == "stale")
    if stale:
        warnings.append(f"{stale} line{_plural(stale, ' has', 's have')} changed since voiced — re-recorded at render")
    unvoiced = sum(1 for s in spoken if effective_voice_state(s, take_known) in ("none", "failed"))
    if unvoiced:
        warnings.append(f"{unvoiced} scene{_plural(unvoiced, ' isn', 's aren')}'t voiced yet — voiced at render")
    music = doc["settings"]["music"]
    if music["enabled"] and not music.get("track"):
        warnings.append("No music track picked — one from your library is used at render "
                        "(none if your library is empty)")
    return {"blockers": blockers, "warnings": warnings}


def checks_for(doc: dict) -> dict:
    """`compute_checks` against the real take cache."""
    from backend.services import voice_take_service as vts
    return compute_checks(doc, vts.is_cached)

