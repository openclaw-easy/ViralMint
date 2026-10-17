# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Generation tools — scripts, Smart Video renders and Motion Graphics.

  - Scripts: write or polish a narration script (synchronous, returns text).
  - Smart Video: a finished mp4 from a script — stock footage matched
    per scene, TTS voiceover, word-by-word captions and background music.
  - Motion Graphics: an AI-written HyperFrames composition, rendered locally.
    Needs the optional HyperFrames engine (Settings → Add-ons).

Every AI step runs on the AI provider key the user configured in Settings.
Long-running calls return {"job_id"}; pair them with wait_for_job, then read
the produced video with list_videos / get_video.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from backend.mcp import client

# The per-item knobs POST /api/downloaded/batch-generate actually reads
# (task_runner.run_batch_generate). Anything else would be silently dropped,
# so the tool refuses it instead.
_BATCH_ITEM_KEYS = frozenset({
    "aspect_ratio", "tts_provider", "tts_voice", "caption_style",
    "caption_enabled", "music_enabled", "music_genre", "custom_script",
    "start_image",
})


def _local_image(ref: Optional[str]) -> Optional[str]:
    """Normalize an image reference for the generator.

    `/api/media/<name>` (an upload through /api/media/upload) passes through;
    anything else is a path on this machine, made absolute here so the
    backend doesn't resolve it against its own working directory.
    """
    if not ref:
        return None
    ref = ref.strip()
    if ref.startswith("/api/media/"):
        return ref
    p = Path(ref).expanduser().resolve()
    if not p.is_file():
        raise ValueError(f"Image not found: {p}")
    return str(p)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def generate_script_from_topic(
        topic: str,
        user_instructions: Optional[str] = None,
        aspect_ratio: str = "9:16",
    ) -> dict:
        """Write a narration script from a topic — synchronous, no rendering.

        Returns the script TEXT directly (not a job_id). One chat call on the
        user's configured AI provider key, typically 5-15s. Use it when the
        user should review or edit the script before committing to a render.

        Typical flow: call this, show the script, then pass the (possibly
        edited) script to generate_smart_video.

        Args:
            topic: Subject of the video (e.g. "Why investors panic when
                Tesla's margins drop"). Either this OR user_instructions
                must be non-empty.
            user_instructions: Detailed brief (tone, hook, CTA, length).
                Combined with topic when both are given.
            aspect_ratio: "9:16" asks for a vertical short-form script;
                anything else asks for a horizontal long-form one. The
                script targets roughly 60-90 seconds spoken either way.

        Returns:
            {"script": "<full script text>"}. The script is written in the
            language of the topic.
        """
        if not (topic or "").strip() and not (user_instructions or "").strip():
            raise ValueError("Provide a topic or user_instructions")
        body: dict = {"topic": topic or "", "aspect_ratio": aspect_ratio}
        if user_instructions:
            body["user_instructions"] = user_instructions
        return await client.post(
            "/api/downloaded/generate-script-from-topic", json=body,
        )

    @mcp.tool()
    async def polish_script(script: str) -> dict:
        """Improve an existing script — fix flow, sharpen the hook, polish grammar.

        Synchronous AI rewrite on the user's configured AI provider key (no
        job_id), ~5-10s. The model is told to keep the topic, structure,
        length and language — it is a polish, not a rewrite, so a strong
        script may come back nearly unchanged.

        Args:
            script: Existing script text, 1-8000 characters. Stage directions
                and timestamps are not added to the output.

        Returns:
            {"script": "<polished script text>"} — pass it straight to
            generate_smart_video.
        """
        text = (script or "").strip()
        if not text:
            raise ValueError("script is empty")
        if len(text) > 8000:
            raise ValueError("script too long (max 8000 characters)")
        return await client.post("/api/downloaded/polish-script", json={"script": text})

    @mcp.tool()
    async def split_scenes(
        script: str,
        aspect_ratio: str = "9:16",
        source_id: Optional[str] = None,
    ) -> dict:
        """Split a script into stock-footage scenes with Pexels search keywords.

        A preview step before generate_smart_video: show the user how the
        script will be cut into scenes and what footage each scene will look
        for. The render does its own planning — this does not have to be
        called first, and its output is not fed into the render.

        Uses the user's configured AI provider key; when no key is set or the
        AI reply is unusable it falls back to a plain ~25-words-per-scene
        split with naive keywords (still a valid response, not an error).

        Args:
            script: Final spoken script (non-empty).
            aspect_ratio: Accepted for forward compatibility; it does not
                change the split today.
            source_id: Optional downloaded_video_id (from list_downloaded)
                whose AI insights are given to the splitter as context.

        Returns:
            {"scenes": [{"text": str, "keywords": [str, ...]}, ...]} —
            4-8 scenes from the AI path, up to 8 from the fallback.
        """
        if not (script or "").strip():
            raise ValueError("script is empty")
        body: dict = {"script": script, "aspect_ratio": aspect_ratio}
        if source_id:
            body["source_id"] = source_id
        return await client.post("/api/generate/split-scenes", json=body)

    @mcp.tool()
    async def generate_smart_video(
        script: str,
        aspect_ratio: str = "9:16",
        source_id: Optional[str] = None,
        tts_provider: str = "edge_tts",
        tts_voice: Optional[str] = None,
        caption_enabled: bool = True,
        caption_style: str = "viral",
        music_enabled: bool = True,
        music_genre: str = "lofi",
        visual_style: Optional[str] = None,
        transition_style: Optional[str] = None,
        user_images: Optional[list[str]] = None,
        start_image: Optional[str] = None,
    ) -> dict:
        """Render a finished short from a script — stock footage + voice + captions + music.

        Runs the whole pipeline in one background job: TTS voiceover →
        Whisper word timings → stock clips matched per scene →
        stitch → music mix → burned-in word-by-word captions → thumbnail →
        AI-drafted YouTube / TikTok titles and descriptions. Stock search
        needs a Pexels or Pixabay key in Settings; without usable footage the render
        degrades to a text-on-background video rather than failing.

        Get a script first with generate_script_from_topic (or polish one the
        user wrote with polish_script).

        Args:
            script: Final spoken script (required, non-empty).
            aspect_ratio: "9:16" (default — TikTok / Shorts / Reels),
                "16:9" (YouTube) or "1:1".
            source_id: Optional downloaded_video_id (from list_downloaded)
                that inspired the video. Recorded for provenance; must exist
                (404 otherwise).
            tts_provider: "edge_tts" (default — local, no key needed,
                400+ voices; see list_tts_voices) or "openai_tts" (needs an
                OpenAI key in Settings; voices alloy / echo / fable / onyx /
                nova / shimmer).
            tts_voice: Voice id for the provider. Omit for the default.
            caption_enabled: False skips burned-in captions.
            caption_style: Caption preset id — built-in viral (default),
                classic, bold, brainrot, neon, minimal, karaoke, glow, urban,
                warm, mono, or a custom preset id from list_caption_styles.
                An unknown id renders as "viral"; to turn captions off use
                caption_enabled=False.
            music_enabled: False skips background music.
            music_genre: lofi (default), cinematic, upbeat, ambient,
                corporate, jazz, hiphop, classical, edm, acoustic, rnb, rock.
                Picked from the local music library; when no track matches
                the genre another track is used and a warning is sent.
            visual_style: Optional footage mood: cinematic, vlog, card,
                educational, documentary, noir, neon, vintage.
            transition_style: Optional scene transition: auto (default
                dissolve), none (hard cuts), fade, slide, zoom, whip.
            user_images: Up to 12 of the user's own stills (local paths or
                /api/media/<name> references). Each claims one scene in
                order from the first; the remaining scenes use stock.
            start_image: One still (local path or /api/media/<name>) animated
                with a Ken Burns zoom/pan as the WHOLE video instead of
                stock. Ignored when user_images is given.

        Returns:
            {"job_id": str}. Pair with wait_for_job (several minutes is
            normal); the finished video appears in list_videos
            (status="ready") with its file path and drafted metadata.
        """
        if not (script or "").strip():
            raise ValueError("script is empty")
        if user_images and len(user_images) > 12:
            raise ValueError("user_images: at most 12 images")
        body: dict = {
            "script": script.strip(),
            "aspect_ratio": aspect_ratio,
            "tts_provider": tts_provider,
            "caption_enabled": bool(caption_enabled),
            "caption_style": caption_style,
            "music_enabled": bool(music_enabled),
            "music_genre": music_genre,
        }
        if source_id:
            body["source_id"] = source_id
        if tts_voice:
            body["tts_voice"] = tts_voice
        if visual_style:
            body["visual_style"] = visual_style
        if transition_style:
            body["transition_style"] = transition_style
        if user_images:
            body["user_images"] = [_local_image(i) for i in user_images]
        if start_image:
            body["start_image"] = _local_image(start_image)
        return await client.post("/api/generate/stock", json=body)

    @mcp.tool()
    async def generate_from_downloaded(
        downloaded_video_id: str,
        aspect_ratio: str = "9:16",
        custom_script: Optional[str] = None,
        tts_provider: Optional[str] = None,
        tts_voice: Optional[str] = None,
        caption_style: Optional[str] = None,
        caption_enabled: Optional[bool] = None,
        music_enabled: Optional[bool] = None,
        music_genre: Optional[str] = None,
        start_image: Optional[str] = None,
    ) -> dict:
        """Make a new short inspired by a downloaded video.

        Without `custom_script`, the AI (on the user's configured provider
        key) writes an ORIGINAL script from the source's analysis, then the
        same pipeline as generate_smart_video renders it. With
        `custom_script`, that exact narration is rendered.

        Args:
            downloaded_video_id: Source id from list_downloaded (404 if
                unknown). Analysis results improve the AI-written script.
            aspect_ratio: "9:16" (default), "16:9" or "1:1".
            custom_script: Render this narration verbatim instead of writing
                one.
            tts_provider: "edge_tts" or "openai_tts". Omit for the user's
                Settings default.
            tts_voice: Voice id for the provider (see list_tts_voices).
            caption_style: Caption preset id (see list_caption_styles). Omit
                for the Settings default.
            caption_enabled: Force captions on/off. Omit for the default.
            music_enabled: Force background music on/off.
            music_genre: Same genres as generate_smart_video.
            start_image: One still (local path or /api/media/<name>) animated
                with a Ken Burns zoom/pan as the whole video instead of stock.

        Returns:
            {"job_id": str}. Pair with wait_for_job; the finished video is
            queryable via list_videos / get_video.
        """
        if not (downloaded_video_id or "").strip():
            raise ValueError("downloaded_video_id is required")
        body: dict = {"aspect_ratio": aspect_ratio}
        for key, value in {
            "custom_script": custom_script,
            "tts_provider": tts_provider,
            "tts_voice": tts_voice,
            "caption_style": caption_style,
            "caption_enabled": caption_enabled,
            "music_enabled": music_enabled,
            "music_genre": music_genre,
        }.items():
            if value is not None:
                body[key] = value
        if start_image:
            body["start_image"] = _local_image(start_image)
        return await client.post(
            f"/api/downloaded/{downloaded_video_id.strip()}/generate", json=body,
        )

    @mcp.tool()
    async def batch_generate(
        items: list[dict],
        shared_settings: Optional[dict] = None,
    ) -> dict:
        """Generate up to 10 videos from downloaded sources in ONE parent job.

        Items run one after another inside a single job (one job_id to wait
        on); a failed item does not stop the rest. Use it for "make all five
        of these into Shorts".

        Args:
            items: 1-10 dicts. Each MUST have `downloaded_video_id` (from
                list_downloaded; every id must exist or the call 404s). Each
                MAY override any of: aspect_ratio, tts_provider, tts_voice,
                caption_style, caption_enabled, music_enabled, music_genre,
                custom_script, start_image — same meanings as
                generate_from_downloaded. Other keys are rejected.
                Example: [{"downloaded_video_id": "abc"},
                          {"downloaded_video_id": "def", "aspect_ratio": "16:9"}]
            shared_settings: Defaults for every item (same keys, minus
                downloaded_video_id); a per-item value wins.

        Returns:
            {"job_id": str, "count": int}. Pair with wait_for_job (allow a
            long timeout — each item is a full render). The terminal
            payload's output is {"succeeded": [{index, child_job_id,
            video_id}], "failed": [{index, child_job_id, video_id, error}]};
            the job fails only when every item failed.
        """
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty list of dicts")
        if len(items) > 10:
            raise ValueError("max 10 items per batch")
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not item.get("downloaded_video_id"):
                raise ValueError(f"items[{i}] must be a dict with `downloaded_video_id` set")
            extra = set(item) - _BATCH_ITEM_KEYS - {"downloaded_video_id"}
            if extra:
                raise ValueError(
                    f"items[{i}] has unsupported keys {sorted(extra)}; "
                    f"allowed: {sorted(_BATCH_ITEM_KEYS)}"
                )
        shared = dict(shared_settings or {})
        extra = set(shared) - _BATCH_ITEM_KEYS
        if extra:
            raise ValueError(
                f"shared_settings has unsupported keys {sorted(extra)}; "
                f"allowed: {sorted(_BATCH_ITEM_KEYS)}"
            )
        body: dict = {"items": items}
        if shared:
            body["shared_settings"] = shared
        return await client.post("/api/downloaded/batch-generate", json=body)

    # ── Motion Graphics (optional HyperFrames engine) ─────────────────────
    # Every route below answers with a 200 envelope when the engine isn't
    # installed — {"ok": false, "error_code": "hyperframes_not_installed",
    # "message", "action_url": "/settings#motion-graphics"} for the actions,
    # an empty result for the read/maintenance routes. The tools pass those
    # through untouched so a caller can branch on error_code.

    @mcp.tool()
    async def motion_compose(
        topic: Optional[str] = None,
        instruction: Optional[str] = None,
        headline: Optional[str] = None,
        style: Optional[str] = None,
        accent: str = "#ffd60a",
        aspect_ratio: str = "9:16",
        duration_seconds: int = 5,
    ) -> dict:
        """Have the AI write a Motion Graphics composition (HyperFrames HTML) and set it live.

        One authoring call on the user's configured AI provider key, then the
        engine's validator; a composition the validator rejects is sent back
        to the model once with the errors, and the job fails (leaving the
        current composition untouched) if it is still invalid. On success
        the new composition becomes the studio's live index.html and the
        previous one is archived as comp_<id>.html (see motion_list_comps).

        Every file staged with motion_stage_asset is handed to the author
        (up to 6) with an instruction to use all of them.

        Quality tracks the configured model: small models often fail the
        validator — suggest a stronger model if jobs keep failing.

        Requires the Motion Graphics engine; otherwise returns
        {"ok": false, "error_code": "hyperframes_not_installed", "message",
        "action_url": "/settings#motion-graphics"} and starts nothing.

        Args:
            topic: The brief — what the video should say/show. Either this
                or `instruction` is required.
            instruction: A requested change to the LIVE composition, e.g.
                "make the headline bigger". The model edits the current
                document and keeps everything else; a live file too large to
                round-trip is rewritten with the change instead (the job's
                current_step says so).
            headline: Text the composition must use verbatim.
            style: Free-text look/mood, e.g. "minimal editorial".
            accent: Hex accent colour (default "#ffd60a").
            aspect_ratio: "9:16" (default), "16:9", "1:1", or the 4K variants
                "9:16-4k", "16:9-4k", "1:1-4k".
            duration_seconds: Length; snapped to the nearest of 5, 10, 15, 30.

        Returns:
            {"job_id": str} — wait_for_job → output {"file": "index.html",
            "archived": "comp_xxxxxxxx.html" | null}. Then motion_render to
            make the MP4.
        """
        t = (topic or "").strip()
        ins = (instruction or "").strip()
        if not t and not ins:
            raise ValueError("Provide a topic (new composition) or an instruction (change)")
        body: dict = {
            "accent": accent,
            "aspect_ratio": aspect_ratio,
            "duration_seconds": int(duration_seconds),
        }
        if t:
            body["topic"] = t
        if ins:
            body["instruction"] = ins
        if headline:
            body["headline"] = headline
        if style:
            body["style"] = style
        return await client.post("/api/generate/motion/studio/author", json=body)

    @mcp.tool()
    async def motion_stage_asset(file_path: str) -> dict:
        """Stage YOUR OWN image / video / audio file into the Motion Graphics project.

        Staged files live in the project's assets/ folder. motion_compose
        automatically hands every staged file to the author, and a
        composition you write yourself (motion_submit_composition) can
        reference them as "assets/<file>".

        Requires the Motion Graphics engine; otherwise returns the
        {"ok": false, "error_code": "hyperframes_not_installed", ...}
        envelope.

        Args:
            file_path: Local path. Images (.png .jpg .jpeg .webp .gif .svg),
                video (.mp4 .mov .webm .m4v) or audio (.mp3 .wav .m4a .aac
                .ogg). Max 200 MB. Other types → 400.

        Returns:
            {"file": "<staged name>", "type": "image"|"video"|"audio",
             "duration": <seconds|null>}. The staged name differs from the
            original (sanitized + random suffix) — use it exactly.
        """
        if not (file_path or "").strip():
            raise ValueError("file_path is required")
        return await client.post_file(
            "/api/generate/motion/studio/assets", file_path=file_path.strip(),
        )

    @mcp.tool()
    async def motion_list_comps() -> dict:
        """List archived Motion Graphics compositions (previous versions).

        Each time a composition is replaced (motion_compose or
        motion_submit_composition) the previous one is archived as
        comp_<id>.html. The live index.html is not listed.

        Returns:
            {"comps": [{"file": "comp_ab12cd34.html", "size": <bytes>,
                        "modified": <unix ts>}, ...]}. {"comps": []} when
            the engine isn't installed.
        """
        return await client.get("/api/generate/motion/studio/comps")

    @mcp.tool()
    async def motion_cleanup_comps(files: Optional[list[str]] = None) -> dict:
        """Delete archived Motion Graphics compositions to reclaim disk.

        Args:
            files: Specific comp_*.html names from motion_list_comps. Omit to
                clear ALL archived compositions AND the studio's export files
                that were already imported into the Library. The live
                index.html, staged assets and Library videos are never
                touched.

        Returns:
            {"removed": [str, ...], "freed_mb": float}. {"removed": [],
            "freed_mb": 0} when the engine isn't installed.
        """
        return await client.post(
            "/api/generate/motion/studio/comps/cleanup", json={"files": files},
        )

    @mcp.tool()
    async def motion_sync_renders() -> dict:
        """Import any new Motion Graphics studio exports into the Library.

        Exports made from the studio's own Export button land in its renders
        folder; this imports the new ones as Library videos. Idempotent —
        already-imported renders are skipped. (motion_render and
        motion_submit_composition(render=True) add to the Library on their
        own and don't need this.)

        Returns:
            {"imported": int, "ids": [generated_video_id, ...]} — read each
            with get_video. {"imported": 0, "ids": []} when the engine isn't
            installed or the import failed (it is retried on the next call).
        """
        return await client.post("/api/generate/motion/studio/sync-renders")

    @mcp.tool()
    async def motion_submit_composition(
        html: str,
        render: bool = True,
        aspect_ratio: str = "9:16",
        title: Optional[str] = None,
    ) -> dict:
        """Submit a HyperFrames composition YOU wrote — validated, set live, optionally rendered.

        Here you are the author (no AI call). The document must follow the
        engine's render contract:
          - root <div data-composition-id="..." data-width data-height
            data-start="0">;
          - ONE paused GSAP timeline registered on window.__timelines under
            the composition id;
          - vendored GSAP only (<script src="assets/gsap.min.js">), no
            CDN / webfonts / external URLs, all CSS inline;
          - deterministic: no Math.random / Date.now / timers / rAF;
          - staged media is referenced as "assets/<file>"
            (motion_stage_asset).

        Validation failures come back as {"ok": false, "issues": [...],
        "hint"} — fix each issue and resubmit the complete document.

        Requires the Motion Graphics engine; otherwise returns the
        {"ok": false, "error_code": "hyperframes_not_installed", ...}
        envelope.

        Args:
            html: The complete document (<!doctype html>…</html>), max 1 MB.
            render: Also render an MP4 into the Library (default True).
                False just sets it live.
            aspect_ratio: "9:16", "16:9", "1:1" (or the "-4k" variants);
                must match the composition's dimensions. Others → 400.
            title: Library title for the rendered video.

        Returns:
            {"ok": true, "issues": [], "file": "index.html",
             "archived": "comp_xxx.html" | null, "job_id": str (only when
             render=True)} — wait_for_job(job_id) → output
            {"generated_video_id": ...}.
        """
        h = (html or "").strip()
        if not h:
            raise ValueError("html is required — the complete composition document")
        body: dict = {"html": h, "render": bool(render), "aspect_ratio": aspect_ratio}
        if title:
            body["title"] = title
        return await client.post("/api/generate/motion/studio/submit", json=body)

    @mcp.tool()
    async def motion_render(
        aspect_ratio: str = "9:16",
        title: Optional[str] = None,
        quality: str = "high",
    ) -> dict:
        """Render the live Motion Graphics composition to an MP4 in the Library.

        Renders locally with the HyperFrames engine — run it after
        motion_compose (or motion_submit_composition with render=False).

        Requires the Motion Graphics engine; otherwise returns the
        {"ok": false, "error_code": "hyperframes_not_installed", ...}
        envelope.

        Args:
            aspect_ratio: "9:16" (default), "16:9", "1:1" or a "-4k" variant.
                Others → 400.
            title: Library title for the video.
            quality: "high" (default — supersampled, crisper text, slower)
                or "standard" (1080p, faster).

        Returns:
            {"job_id": str} — wait_for_job(job_id, timeout_seconds=900) →
            output {"generated_video_id": ...}; then get_video for the path.
        """
        if quality not in ("high", "standard"):
            raise ValueError("quality must be 'high' or 'standard'")
        body: dict = {"aspect_ratio": aspect_ratio, "quality": quality}
        if title:
            body["title"] = title
        return await client.post("/api/generate/motion/studio/export", json=body)
