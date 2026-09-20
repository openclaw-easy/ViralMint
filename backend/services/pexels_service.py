# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Pexels stock footage service.
Searches and downloads royalty-free stock videos matching script content.
"""
import asyncio
import json
import logging
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx

from backend.services.video_utils import probe_duration

from backend.config import settings
from backend.core.exceptions import VideoGenerationError
from backend.core.ws_manager import ws_manager
from backend.services.ffmpeg_service import (
    ffmpeg_error,
    generate_single_kenburns_clip,
    normalize_still,
)

logger = logging.getLogger(__name__)

PEXELS_API = "https://api.pexels.com"

# Shared httpx client — connection pooling across all Pexels calls
_http = httpx.AsyncClient(timeout=30, follow_redirects=True, limits=httpx.Limits(max_connections=10))

# Most scenes one video is cut into. Past this the cuts come faster than an
# idea can land, and it is also the ceiling on how many of the user's own
# images a single video can hold.
MAX_SCENES = 12

# Minimum acceptable resolution for stock clips
MIN_WIDTH_PORTRAIT = 720
MIN_HEIGHT_PORTRAIT = 1280
MIN_WIDTH_LANDSCAPE = 1280
MIN_HEIGHT_LANDSCAPE = 720


async def search_videos(
    query: str,
    orientation: str = "portrait",
    per_page: int = 10,
    api_key: str = "",
) -> list[dict]:
    """
    Search Pexels for stock footage matching a keyword.
    Returns list of video entries with download URLs, sorted by quality.
    """
    if not api_key:
        raise VideoGenerationError("Pexels API key not configured")

    resp = await _http.get(
        f"{PEXELS_API}/videos/search",
        params={"query": query, "orientation": orientation, "per_page": per_page, "size": "medium"},
        headers={"Authorization": api_key},
    )
    resp.raise_for_status()
    data = resp.json()

    min_w = MIN_WIDTH_PORTRAIT if orientation == "portrait" else MIN_WIDTH_LANDSCAPE
    min_h = MIN_HEIGHT_PORTRAIT if orientation == "portrait" else MIN_HEIGHT_LANDSCAPE

    videos = []
    for v in data.get("videos", []):
        # Find the best HD file that matches orientation
        best_file = None
        best_score = 0

        for f in v.get("video_files", []):
            w = f.get("width", 0)
            h = f.get("height", 0)
            quality = f.get("quality", "")

            # Skip files that are too small
            if w < min_w or h < min_h:
                continue

            # Skip wrong orientation
            is_portrait = h > w
            want_portrait = orientation == "portrait"
            if is_portrait != want_portrait:
                continue

            # Score: prefer HD, higher resolution, but not excessively large (>4K wastes bandwidth)
            score = 0
            if quality == "hd":
                score += 100
            if w <= 1920 and h <= 1920:
                score += 50  # prefer reasonable size
            score += min(w, 1920) / 100  # higher res is better up to 1080p

            if score > best_score:
                best_score = score
                best_file = f

        if best_file:
            # Bonus for longer source clips (longer clips = more flexibility, less looping)
            duration = v.get("duration", 0)
            duration_bonus = min(duration / 5, 10)  # up to +10 pts for 50s+ clips

            videos.append({
                "id": v["id"],
                "url": v.get("url", ""),
                "download_url": best_file["link"],
                "width": best_file.get("width", 0),
                "height": best_file.get("height", 0),
                "duration": duration,
                "quality_score": best_score + duration_bonus,
            })

    # Sort by quality score descending (longer + higher quality clips first)
    videos.sort(key=lambda x: x["quality_score"], reverse=True)
    return videos


async def download_clip(url: str, output_path: Path) -> Path:
    """Download a Pexels video clip to local storage."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    async with _http.stream("GET", url, timeout=120) as resp:
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                f.write(chunk)
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise VideoGenerationError(f"Downloaded Pexels clip is empty: {url}")
    return output_path


async def trim_and_normalize_clip(
    clip_path: Path,
    duration: float,
    target_w: int,
    target_h: int,
    output_path: Path = None,
    color_grade: bool = True,
) -> Path:
    """Trim a clip to a specific duration, normalize resolution, and apply color grading.
    Uses scale+crop to ensure consistent dimensions across all clips.
    Color grading applies a uniform cinematic look for visual consistency."""
    if output_path is None:
        output_path = clip_path.parent / f"{clip_path.stem}_trimmed.mp4"

    def _trim():
        # Scale to cover target dimensions, then center-crop to exact size
        # This avoids letterboxing and ensures all clips are identical dimensions
        vf_parts = [
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase",
            f"crop={target_w}:{target_h}",
            "setsar=1",
        ]

        if color_grade:
            # Cinematic color grading: slight contrast boost, desaturate slightly,
            # add subtle warm tint, and unify brightness across all clips.
            # This makes mismatched stock clips look like they belong together.
            vf_parts.extend([
                # Normalize levels (auto white-balance to reduce color variation between clips)
                "normalize=blackpt=black:whitept=white:smoothing=20",
                # Cinematic look: boost contrast slightly, add warm tone, reduce saturation
                "eq=contrast=1.08:brightness=0.02:saturation=0.85",
                # Subtle vignette for cinematic framing
                f"vignette=PI/5",
            ])

        vf = ",".join(vf_parts)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(clip_path),
            "-t", str(duration),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-an",  # no audio from stock footage
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            if color_grade:
                # Retry without colour grading. The stated reason used to be a
                # guess in a comment ("normalize may not be available") and the
                # log carried no evidence either way — not the clip, not the
                # filter chain, not ffmpeg's own message — and at INFO, so it
                # scrolled past unnoticed. When most clips in a render take this
                # branch the whole video silently ships ungraded, and nothing in
                # the log says why. Whatever the cause is, the next occurrence
                # now names it.
                logger.warning(
                    "Colour grading failed for %s (dur=%.1fs) — retrying "
                    "without it: %s",
                    clip_path.name, duration, ffmpeg_error(result.stderr),
                )
                vf_basic = (
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                    f"crop={target_w}:{target_h},setsar=1,"
                    f"eq=contrast=1.08:brightness=0.02:saturation=0.85"
                )
                cmd_retry = [
                    "ffmpeg", "-y",
                    "-i", str(clip_path),
                    "-t", str(duration),
                    "-vf", vf_basic,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-r", "30", "-an",
                    str(output_path),
                ]
                result2 = subprocess.run(cmd_retry, capture_output=True, text=True, timeout=60)
                if result2.returncode != 0:
                    logger.warning("Clip trim failed for %s: %s", clip_path.name, ffmpeg_error(result2.stderr))
                    return clip_path
                return output_path
            logger.warning("Clip trim failed for %s: %s", clip_path.name, ffmpeg_error(result.stderr))
            return clip_path
        return output_path

    return await asyncio.to_thread(_trim)


async def extract_visual_scenes(script: str, ai_client, num_scenes: int = 8) -> list[dict]:
    """Use AI to extract visual scene descriptions from script text.
    Creates one scene per ~10 seconds of video for a cinematic, unhurried feel."""
    prompt = f"""You are a professional video editor choosing stock footage clips for a narrated video.
You must find {num_scenes} clips from Pexels.com that visually match what the narrator is talking about.

THE MOST IMPORTANT RULE: Each clip must DIRECTLY illustrate what the script is saying at that moment.
If the script talks about war → show military footage. If it talks about oil → show oil rigs.
If it talks about economy → show stock markets. STAY ON TOPIC with the script's actual subject matter.

Script:
{script[:3000]}

RULES:
1. Read the script section by section. For each ~10-second chunk, write a Pexels search query that matches EXACTLY what's being discussed.
2. Pexels is a stock footage site — it has real filmed footage of: military, cities, nature, business, technology, food, people, vehicles, buildings, industrial, medical, sports, etc.
3. Pexels does NOT have footage of specific named people or branded logos. So instead of "Trump" use "politician speaking podium". Instead of "Apple iPhone" use "smartphone close up".
4. But Pexels DOES have footage of general categories like: military tanks, fighter jets, warships, oil refineries, desert landscapes, flags waving, protests, soldiers, missiles, nuclear plants, etc.
5. Be SPECIFIC and CONCRETE: "military tank desert" is much better than "dramatic scene". "oil refinery night flames" is better than "energy concept".
6. Every query must directly relate to what the narrator is saying at that point — NO filler scenes like "sunset" or "sky timelapse" unless the script actually talks about those.
7. Vary camera angles: close-up, wide shot, aerial, tracking shot.
8. No two queries should be the same concept.
9. VISUAL CONSISTENCY: maintain a consistent visual tone throughout. If the topic is serious (war, politics, economics), use dark/moody queries ("dark office", "rain city night"). If upbeat (lifestyle, travel), use bright/warm queries ("sunny beach", "golden hour city").
10. Prefer SLOW, CINEMATIC footage over fast action: "slow motion waves", "aerial city slow pan", "close up hands typing slow". Slow footage looks more professional and gives the narrator breathing room.

Return JSON array only (no markdown):
[
  {{"query": "specific pexels search terms"}}
]"""

    try:
        resp = await ai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
        )
        clean = resp.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            scenes = json.loads(clean)
        except json.JSONDecodeError as je:
            from backend.core.ai_retry import ai_fix_json
            scenes = await ai_fix_json(clean, str(je))
        if isinstance(scenes, list) and len(scenes) > 0:
            return scenes[:num_scenes]
    except Exception as e:
        logger.warning(f"Visual scene extraction failed: {e}")

    # Fallback: generic but varied scenes
    fallback = [
        {"query": "dramatic sky timelapse", "mood": "dramatic"},
        {"query": "person watching news screen", "mood": "professional"},
        {"query": "city skyline aerial drone", "mood": "dramatic"},
        {"query": "people walking busy street", "mood": "energetic"},
        {"query": "world map globe spinning", "mood": "professional"},
        {"query": "typing laptop close up", "mood": "professional"},
        {"query": "ocean waves crashing rocks", "mood": "dramatic"},
        {"query": "sunset golden hour silhouette", "mood": "calm"},
        {"query": "highway traffic aerial night", "mood": "energetic"},
        {"query": "cloud timelapse blue sky", "mood": "calm"},
        {"query": "business meeting office", "mood": "professional"},
        {"query": "nature forest aerial view", "mood": "calm"},
        {"query": "sparks welding industrial", "mood": "dramatic"},
        {"query": "coffee cup morning light", "mood": "calm"},
        {"query": "fireworks celebration night", "mood": "energetic"},
    ]
    return fallback[:num_scenes]


# Visual-style → Pexels footage mood. Appended to each scene query so the
# selected style actually changes the look of the stock footage that's pulled.
VISUAL_STYLE_MOODS = {
    "cinematic": "cinematic film",
    "vlog": "handheld vlog lifestyle",
    "card": "moody dark abstract",
    "educational": "clean bright minimal",
    "documentary": "documentary realistic",
    "noir": "black and white noir",
    "neon": "neon night city",
    "vintage": "vintage retro film grain",
}

# Studio transition id → FFmpeg xfade effect (see ffmpeg_service.TRANSITIONS).
# "auto"/None keep the cinematic default ("dissolve"); "none" hard-cuts.
TRANSITION_MAP = {
    "auto": "dissolve",
    "none": "none",
    "fade": "fade",
    "slide": "slideleft",
    "zoom": "smoothright",
    "whip": "wiperight",
}


async def build_stock_video(
    script: str,
    voice_path: Path,
    pexels_api_key: str,
    aspect_ratio: str = "9:16",
    ai_client=None,
    output_path: Path = None,
    visual_style: str = None,
    transition_style: str = None,
    user_images: list[Path] | None = None,
) -> Path:
    """
    Full stock footage video pipeline:
    1. AI extracts visual scene descriptions from script (one per ~7s of audio)
    2. Any images the user brought claim the opening scenes
    3. Search Pexels for the REMAINING scenes with quality filtering
    4. Download best-matching HD clips
    5. Normalize all clips to consistent resolution
    6. Trim clips to match voice timing — total MUST equal voice duration
    7. Stitch clips with smooth transitions
    8. Merge voice audio (video loops if needed to match full audio)
    Returns final video path.

    `user_images` are stills the user supplied. Each one claims a scene in
    order, starting at scene 0 — the hook, which is the scene most worth
    giving to the creator's own material — and is animated into it with a Ken
    Burns move for exactly that scene's slot. Scenes with no user image fall
    to Pexels as before, and no Pexels search is even ISSUED for a scene an
    image already owns.
    """
    if output_path is None:
        output_path = settings.GENERATED_DIR / f"stock_{hash(script) & 0xFFFFFFFF:08x}.mp4"

    orientation = "portrait" if aspect_ratio == "9:16" else "landscape"
    if aspect_ratio == "9:16":
        target_w, target_h = 1080, 1920
    else:
        target_w, target_h = 1920, 1080

    # Get voice duration for timing — this is the target video length
    voice_duration = 60
    if voice_path and voice_path.exists():
        probed = probe_duration(voice_path, default=60)
        voice_duration = probed + 0.5

    logger.info(f"Voice duration: {voice_duration:.1f}s — building stock video to match")

    # Step 1a: Normalize the user's own images BEFORE anything is planned
    # around them. This is what makes a still safe to animate (see
    # ffmpeg_service.normalize_still), and doing it first means an unreadable
    # file is one image fewer rather than a scene that renders to nothing —
    # the scene falls back to stock exactly as if it had never been claimed.
    ready_images: list[Path] = []
    for img in (user_images or []):
        try:
            norm = settings.TMP_DIR / f"user_still_{len(ready_images):03d}_{uuid4().hex[:8]}.png"
            await normalize_still(img, norm)
            ready_images.append(norm)
        except Exception as e:
            logger.warning(f"User image unusable, falling back to stock for its scene ({img}): {e}")
            await ws_manager.send_constraint_warning(
                constraint="user_image_unreadable",
                message=(
                    f"Couldn't read \"{Path(img).name}\" — that scene will use "
                    f"stock footage instead."
                ),
                severity="warning",
            )

    # Step 1b: Extract visual scenes — one per ~10 seconds for cinematic pacing
    # Fewer, longer clips look more professional than rapid-fire cuts.
    # The user's images raise the FLOOR on the scene count: bringing 8 images
    # to a 30-second script would otherwise give 3 scenes and silently use 3
    # of the 8. The ceiling of 12 still holds — see the warning below.
    num_scenes = max(3, min(MAX_SCENES, max(int(voice_duration / 10), len(ready_images))))
    if ai_client:
        scenes = await extract_visual_scenes(script, ai_client, num_scenes=num_scenes)
    else:
        words = script.split()
        scenes = [{"query": w, "mood": "neutral", "priority": i + 1}
                  for i, w in enumerate(w for w in words if len(w) > 4 and w.isalpha()) if i < 8]
    # Bias every scene's Pexels query toward the chosen visual style so the
    # footage that comes back actually matches the look the user picked.
    mood = VISUAL_STYLE_MOODS.get((visual_style or "").lower())
    if mood:
        for s in scenes:
            q = (s.get("query") or "").strip()
            s["query"] = f"{q} {mood}".strip()

    # The scene grid has to be at least as big as the number of images the
    # user brought, or their last photos are silently dropped. `num_scenes` is
    # only a REQUEST — the AI extractor can return fewer, and the no-AI
    # fallback below ignores it entirely and derives scenes from the script's
    # own long words. So the floor is enforced here, after the fact, where
    # both branches have already produced their answer.
    while len(scenes) < min(len(ready_images), MAX_SCENES):
        scenes.append({"query": "cinematic b-roll", "mood": "neutral",
                       "priority": len(scenes) + 1})
    scenes = scenes[:MAX_SCENES]

    logger.info(f"Stock video: {len(scenes)} scenes for {voice_duration:.0f}s video: {[s.get('query') for s in scenes]}")

    # Which scene each user image owns. In order from scene 0; anything past
    # the scene count cannot be placed, and the user is told rather than left
    # to notice their last photos missing from the finished video.
    images_by_scene: dict[int, Path] = {
        i: img for i, img in enumerate(ready_images[:len(scenes)])
    }
    if len(ready_images) > len(scenes):
        unused = len(ready_images) - len(scenes)
        logger.info(f"{unused} user image(s) beyond the {len(scenes)}-scene grid — not placed")
        await ws_manager.send_constraint_warning(
            constraint="user_images_over_scene_count",
            message=(
                f"This script only has room for {len(scenes)} scenes, so "
                f"{unused} of your images weren't used. A longer script fits more."
            ),
            severity="info",
        )

    # Step 2: Search clips for the scenes no image claimed (parallel search,
    # then parallel download)
    used_video_ids = set()

    # Scene index -> (clip path, real duration). Keyed by SCENE rather than
    # accumulated into a flat list because user stills and stock clips are
    # produced by two different routes and have to interleave back into the
    # script's own order at the end.
    clips_by_scene: dict[int, tuple[Path, float]] = {}

    # Target duration per clip (will be redistributed later if some fail)
    target_per_clip = voice_duration / len(scenes) if scenes else 10

    # Phase A: Search all scenes in parallel to find clip URLs
    async def _search_scene(i: int, scene: dict) -> tuple[int, dict | None]:
        # A scene the user's own image owns costs no Pexels quota and no
        # download — it is never searched for in the first place.
        if i in images_by_scene:
            return i, None
        query = scene.get("query", "")
        if not query:
            return i, None
        try:
            # Prefer longer clips from Pexels (min_duration filter via query)
            results = await search_videos(
                query, orientation=orientation, per_page=15, api_key=pexels_api_key,
            )
            # Prefer clips that are at least as long as our target duration
            long_results = [r for r in results if r.get("duration", 0) >= target_per_clip]
            candidates = long_results if long_results else results

            if not candidates:
                # Fallback: try broader search
                broad_query = " ".join(query.split()[:2]) if len(query.split()) > 2 else query
                candidates = await search_videos(
                    broad_query, orientation=orientation, per_page=10, api_key=pexels_api_key,
                )
            if not candidates:
                candidates = await search_videos(
                    "cinematic aerial landscape", orientation=orientation, per_page=5, api_key=pexels_api_key,
                )
            return i, candidates[0] if candidates else None
        except Exception as e:
            logger.warning(f"Search failed for '{query}': {e}")
            return i, None

    search_tasks = [_search_scene(i, scene) for i, scene in enumerate(scenes)]
    search_results = await asyncio.gather(*search_tasks)

    # Deduplicate and collect chosen clips
    chosen_clips = []
    for i, chosen in sorted(search_results, key=lambda x: x[0]):
        if chosen and chosen["id"] not in used_video_ids:
            used_video_ids.add(chosen["id"])
            chosen_clips.append((i, chosen))

    # Phase B: Download and process all clips in parallel
    async def _download_and_process(i: int, chosen: dict) -> tuple[int, Path | None, float]:
        try:
            clip_raw = settings.TMP_DIR / f"pexels_raw_{i:03d}.mp4"
            await download_clip(chosen["download_url"], clip_raw)

            clip_out = settings.TMP_DIR / f"pexels_{i:03d}.mp4"
            trimmed = await trim_and_normalize_clip(
                clip_raw, target_per_clip, target_w, target_h, clip_out,
            )

            # Probe actual trimmed duration
            actual_dur = probe_duration(trimmed, default=target_per_clip)

            # Clean up raw
            if clip_raw != trimmed:
                clip_raw.unlink(missing_ok=True)

            return i, trimmed, actual_dur
        except Exception as e:
            logger.warning(f"Failed to download/process clip {i}: {e}")
            return i, None, 0

    # Run downloads in parallel (limit concurrency to 4 to avoid hammering Pexels)
    sem = asyncio.Semaphore(4)

    async def _limited_download(i, chosen):
        async with sem:
            return await _download_and_process(i, chosen)

    download_tasks = [_limited_download(i, chosen) for i, chosen in chosen_clips]
    download_results = await asyncio.gather(*download_tasks)

    for i, path, dur in download_results:
        if path:
            clips_by_scene[i] = (path, dur)

    # Step 3: Animate each user still into the scene it claimed. One Ken Burns
    # pass gives it both its motion and exactly the scene's slot, so it stitches
    # against the stock clips with no further normalization — and NOT the
    # trim-and-normalize route the clips take, which would fight the zoom.
    # normalize=False because these were normalized in step 1a.
    for scene_idx, still in images_by_scene.items():
        out = settings.TMP_DIR / f"user_scene_{scene_idx:03d}_{uuid4().hex[:6]}.mp4"
        try:
            clip = await generate_single_kenburns_clip(
                image_path=still, duration=target_per_clip, output_path=out,
                target_w=target_w, target_h=target_h, normalize=False,
            )
        except Exception as e:
            # One bad still costs its own scene, never the render. The scene
            # simply has no clip; the loop-extension step below covers the gap.
            logger.warning(f"Could not animate user image for scene {scene_idx}: {e}")
            continue
        # Probe rather than assume: the ledger below has to match what is
        # actually on disk, or the video comes out shorter than the voice.
        clips_by_scene[scene_idx] = (
            clip, probe_duration(clip, default=target_per_clip),
        )

    # Flatten back into the script's own order.
    ordered = [clips_by_scene[i] for i in sorted(clips_by_scene)]
    clip_paths = [c for c, _d in ordered]
    clip_actual_durations = [d for _c, d in ordered]

    for still in ready_images:
        still.unlink(missing_ok=True)

    if not clip_paths:
        logger.warning("No stock clips downloaded — cannot build stock video")
        return None

    # Step 4: If total clip duration is less than voice, extend by looping clips
    total_clip_duration = sum(clip_actual_durations)
    if total_clip_duration < voice_duration - 1:
        shortage = voice_duration - total_clip_duration
        logger.info(f"Clips total {total_clip_duration:.1f}s, voice is {voice_duration:.1f}s — "
                     f"extending by {shortage:.1f}s via clip looping")
        # Loop existing clips to fill the gap
        loop_idx = 0
        while shortage > 2 and loop_idx < len(clip_paths) * 3:  # max 3 full loops
            src_clip = clip_paths[loop_idx % len(clip_paths)]
            src_dur = clip_actual_durations[loop_idx % len(clip_actual_durations)]
            extend_dur = min(src_dur, shortage)

            loop_out = settings.TMP_DIR / f"pexels_loop_{len(clip_paths):03d}.mp4"
            looped = await trim_and_normalize_clip(
                src_clip, extend_dur, target_w, target_h, loop_out,
            )
            clip_paths.append(looped)
            clip_actual_durations.append(extend_dur)
            shortage -= extend_dur
            loop_idx += 1

    # Step 5: Stitch clips with smooth cinematic transitions
    from backend.services.ffmpeg_service import stitch_clips

    stitched = settings.TMP_DIR / "stock_stitched.mp4"
    # Transition from the studio's Transitions picker; "auto"/None keeps the
    # cinematic dissolve default (0.8s — smooth, not jarring).
    transition = TRANSITION_MAP.get((transition_style or "").lower(), "dissolve")
    stitched = await stitch_clips(clip_paths, stitched, transition=transition, transition_duration=0.8)

    # Step 6: Merge voice audio — use voice duration as the master length
    if voice_path and voice_path.exists():
        final = await _merge_video_audio_full(stitched, voice_path, output_path, voice_duration)
    else:
        import shutil
        shutil.move(str(stitched), str(output_path))
        final = output_path

    # Cleanup tmp clips
    for p in clip_paths:
        p.unlink(missing_ok=True)
    stitched.unlink(missing_ok=True)

    logger.info(f"Stock video built: {final} ({len(clip_paths)} clips, {voice_duration:.0f}s)")
    return final


async def _merge_video_audio_full(
    video_path: Path, audio_path: Path, output_path: Path, target_duration: float,
) -> Path:
    """Merge video + audio, ensuring output matches full audio duration.
    If video is shorter than audio, loop the video. Never truncate audio."""
    def _merge():
        # Use -stream_loop to loop video if it's shorter than audio
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",  # loop video infinitely
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-t", str(target_duration),  # cut to exact audio length
            "-map", "0:v:0", "-map", "1:a:0",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.warning(f"Full merge failed, trying simple merge: {ffmpeg_error(result.stderr)}")
            # Fallback: simple merge without loop (may be slightly short)
            cmd_simple = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", str(audio_path),
                "-c:v", "copy", "-c:a", "aac",
                str(output_path),
            ]
            result2 = subprocess.run(cmd_simple, capture_output=True, text=True, timeout=300)
            if result2.returncode != 0:
                raise VideoGenerationError(f"FFmpeg merge failed: {ffmpeg_error(result2.stderr, 500)}")
        return output_path

    return await asyncio.to_thread(_merge)
