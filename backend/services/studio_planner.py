# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Smart Video Studio — planning: scene LINES → scene VISUALS, in one pass.

  1. Each scene's line is one timed chunk → `extract_visual_scenes`: one stock
     search per scene, in order, with two fallbacks and a mood (one AI call
     with the user's own key).
  2. `search_all_scenes` pools Pexels + Pixabay candidates per scene (24 h cache).
  3. `rank_scene_candidates` (one AI call) orders each pool by meaning.
  4. The best candidate is BOUND (no clip used twice when an alternative
     exists) and up to 5 alternates are kept; every candidate is remembered so
     a later bind is trusted.

This module only COMPUTES ops. The runner lands them with the internal
`plan_result` op, which skips any scene the user pinned, bound their own clip
to or re-wrote meanwhile. Every degradation — no AI key, an AI failure, no
stock key, a search failure — becomes a warning, never an error: a plan with
keyword queries is still a plan.
"""
from __future__ import annotations

import logging

from backend.services import studio_doc as sd

logger = logging.getLogger(__name__)

MAX_ALTERNATES = 5


def _is_portrait(c: dict) -> bool | None:
    w, h = c.get("width"), c.get("height")
    if not (isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0):
        return None
    return h > w


def prefer_native_orientation(candidates: list[dict], want_portrait: bool, window: int = 3,
                              off_topic: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """Bring the best-ranked clip already in the frame's orientation to the
    front when it sits within the top `window`. A landscape clip centre-cropped
    to 9:16 keeps ~31% of its width, upscaled ~2× — soft, faces cut — and most
    such binds had a vertical clip in the same results. Order beyond that is
    untouched: relevance still decides the rest. A clip in `off_topic` (its
    description shares nothing with the query) doesn't jump a top clip that IS
    about it: a pool can hold many verticals since the search asks for them
    first. A top clip just as off-topic has no such claim."""
    if not candidates or _is_portrait(candidates[0]) in (None, want_portrait):
        return candidates
    for j, c in enumerate(candidates[:window]):
        if _is_portrait(c) == want_portrait and (
                c.get("provider_id") not in off_topic or candidates[0].get("provider_id") in off_topic):
            return [c] + candidates[:j] + candidates[j + 1:]
    return candidates


def _open(scene: dict) -> bool:
    return bool(scene["line"]) and not sd.is_fixed_visual(scene)


async def plan_project(doc: dict, *, project_id: str, ai_client, pexels_key: str, pixabay_key: str = "",
                       only_uids: set[str] | None = None) -> tuple[list[dict], list[str]]:
    from backend.services import clip_rank_service, stock_search_cache, studio_media
    from backend.services import stock_video_service as svs

    spoken = [(i, s) for i, s in enumerate(doc["scenes"]) if s["line"]
              and (only_uids is None or s["uid"] in only_uids)]
    if not spoken:
        return [], ["Write at least one line to plan the visuals."]
    if not any(_open(s) for _, s in spoken):
        return [], ["Every scene already has a visual you chose — nothing to plan."]
    warnings: list[str] = []

    timeline = sd.derive_timeline(doc)
    chunks = [{
        "start": timeline["starts"][i], "end": timeline["starts"][i] + timeline["durations"][i],
        "duration": timeline["durations"][i], "text": s["line"],
    } for i, s in spoken]

    extracted = None
    if ai_client is None:
        warnings.append("No AI key is set — scenes are planned from keywords in each line. "
                        "Add one in Settings for smarter footage picks.")
    else:
        try:
            extracted = await svs.extract_visual_scenes(chunks, ai_client)
        except Exception:  # noqa: BLE001 — degrade to keyword queries
            logger.warning("studio plan: scene extraction failed", exc_info=True)
            warnings.append("Scene planning AI didn't answer — using keywords from each line.")
    if not extracted or len(extracted) != len(chunks):
        extracted = [{"query": s["visual"].get("query") or sd.query_from_line(s["line"]),
                      "mood": s.get("mood")} for _, s in spoken]

    pools: dict[int, list[dict]] = {}
    if pexels_key or pixabay_key:
        refused = stock_search_cache.watch_refusals()
        try:
            results = await svs.search_all_scenes(
                extracted, studio_media.orientation(doc["aspect_ratio"]), timeline["total"], pexels_key,
                pixabay_key)
            pools = {idx: pool for idx, pool in results}
        except Exception:  # noqa: BLE001
            logger.warning("studio plan: stock search failed", exc_info=True)
            warnings.append("Stock search didn't answer — scenes keep their search terms. Try Re-plan in a minute.")
        if refused:
            warnings.append("Stock footage search hit its hourly limit, so some scenes may have fewer clips or none. "
                            "Try Re-plan in an hour.")
    else:
        warnings.append(studio_media.NO_KEY_MESSAGE)

    items = [{"scene_idx": k, "text": chunks[k]["text"], "mood": extracted[k].get("mood") or "",
              "candidates": pools.get(k) or []} for k in range(len(chunks))]
    ranked: dict[int, list[dict]] = {}
    if any(it["candidates"] for it in items):
        ranked = await clip_rank_service.rank_scene_candidates(items, ai_client)
    want_portrait = doc["aspect_ratio"] == "9:16"

    used: set[tuple[str, str]] = set()
    ops: list[dict] = []
    for k, (_i, scene) in enumerate(spoken):
        meta = extracted[k] if isinstance(extracted[k], dict) else {}
        query = str(meta.get("query") or scene["visual"].get("query") or sd.query_from_line(scene["line"]))[:100]
        best_first = list(ranked.get(k) or [])
        best_first += [c for c in (pools.get(k) or []) if c not in best_first]
        candidates = [c for c in (studio_media.candidate_from_result(r, query) for r in best_first) if c]
        off_topic = {str(r.get("id")).removeprefix("pixabay_") for r in best_first
                     if svs.on_topic(r, query) is False} if want_portrait else frozenset()
        candidates = prefer_native_orientation(candidates, want_portrait, off_topic=off_topic)
        studio_media.remember(project_id, candidates)
        if not _open(scene):
            continue
        fresh = [c for c in candidates if (c["provider"], c["provider_id"]) not in used]
        top = (fresh or candidates)[0] if candidates else None
        if top is not None:
            used.add((top["provider"], top["provider_id"]))
        alternates = ([top] + [c for c in candidates if c is not top])[:MAX_ALTERNATES] if top else []
        visual = {
            "kind": "stock", "state": "bound" if top else "placeholder",
            "query": query, "asset": dict(top) if top else None, "alternates": alternates,
            "fit": "loop", "pinned": False, "reason": None,
            # The line this visual was picked for: an edited line shows
            # "clip picked for the old line — Re-plan this scene".
            "planned_for": scene["line"],
        }
        ops.append({"op": "plan_result", "uid": scene["uid"], "expect_line": scene["line"], "visual": visual,
                    "mood": str(meta.get("mood") or "")[:20] or None})
    return ops, warnings
