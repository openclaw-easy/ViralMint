# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Smart Video Studio API.

Every project read/write returns the envelope from studio_store.envelope:
`{project: {id, title, status, revision, doc, derived, checks, media, …}}`.

Edits go through PATCH as ops under optimistic concurrency (`if_revision`);
work that takes time (planning, voice takes, the render) has its own
endpoint and runs as a job whose result lands back through internal ops.

Errors: every refusal is `{"detail": {"code", "message", …}}` (studio_error),
so the editor can branch on the code and always has a sentence to show.
"""
from __future__ import annotations

import asyncio
import json
import re
import logging
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from backend.core.exceptions import (
    StudioConflictError, StudioDocError, StudioMediaError, StudioNotFoundError, StudioOpError,
)
from backend.services import studio_doc as sd
from backend.services import studio_store as store

logger = logging.getLogger(__name__)
router = APIRouter()

USER = "local"


def studio_error(status: int, code: str, message: str, **extra) -> HTTPException:
    """The one error shape for /api/studio: always a `code` and a `message`."""
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


_OP_PREFIX = re.compile(r"^[a-z_]+(?: [a-z_.]+)?: ")


def plain_op_error(e: Exception) -> str:
    """An op error as the user reads it: without the internal op name
    ("set_setting voice: choose a supported voice." reached the toast)."""
    text = _OP_PREFIX.sub("", str(e), count=1)
    return text[:1].upper() + text[1:] if text else "That edit wasn't valid."


def _not_found(e: StudioNotFoundError) -> HTTPException:
    return studio_error(404, "not_found", str(e))


class StartBody(BaseModel):
    mode: Literal["script"] = "script"
    script: str = Field(default="", max_length=20000)
    title: str | None = Field(default=None, max_length=200)
    topic: str | None = Field(default=None, max_length=200)
    sample_id: str | None = Field(default=None, max_length=60)
    # The Library source this project was drafted from ("Use as inspiration",
    # `/stock?source=`): recorded on the project and carried onto the rendered
    # video's lineage columns (studio_render.source_lineage).
    source_id: str | None = Field(default=None, max_length=60, pattern=r"^[A-Za-z0-9_-]+$")


async def _check_caption_style(style: object, code: str) -> None:
    """An unknown caption style id renders as `viral` (the ASS builder's silent
    fallback) — refuse it at the write instead. Sentinels like "none" are not
    styles either: captions are turned off with `captions.enabled`."""
    from backend.services.caption_service import captions_disabled, style_id_known
    if await style_id_known(style):
        return
    if captions_disabled(style):
        raise studio_error(400, code, "To turn captions off, set captions.enabled to false — \"none\" is not a caption style.")
    raise studio_error(400, code, f"Unknown caption style {style!r} — pick one from /api/captions/styles.",
                       field="captions.style")


class CreateBody(BaseModel):
    start: StartBody
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    visual_style: str = "cinematic"
    # Optional overrides of the new project's settings (the editor remembers
    # the last voice and caption style); anything unknown falls back to the
    # defaults through studio_doc's normalisation.
    settings: dict | None = None
    # plan: bind real stock right away. first_draft: plan, then voice every
    # scene with the free local voice — never a voice that spends a key.
    plan: bool = False
    first_draft: bool = False


class OpsBody(BaseModel):
    if_revision: int | None = None
    ops: list[dict] = Field(default_factory=list, max_length=100)


class PreviewBody(BaseModel):
    candidate: dict


class VoiceBody(BaseModel):
    scene_uids: list[str] | Literal["all"] = "all"


@router.get("/studio/enums")
async def studio_enums():
    return sd.enums()


@router.get("/studio/projects")
async def list_projects(limit: int = 24, q: str | None = None):
    return {"items": await store.list_projects(limit=limit, q=q, user_id=USER)}


@router.post("/studio/projects")
async def create_project(body: CreateBody):
    style = ((body.settings or {}).get("captions") or {}).get("style") if isinstance(body.settings, dict) else None
    if style is not None:
        await _check_caption_style(style, "invalid")
    settings = body.settings if body.settings else await saved_settings()
    try:
        row = await store.create_project(
            script=body.start.script, title=body.start.title or body.start.topic,
            aspect_ratio=body.aspect_ratio, visual_style=body.visual_style,
            input_meta={"topic": body.start.topic, "title": body.start.title, "sample_id": body.start.sample_id,
                        "source_id": body.start.source_id},
            settings_overrides=settings, user_id=USER,
        )
    except StudioDocError as e:
        raise studio_error(400, "invalid", str(e))
    plan_job_id = await _start_plan(row.id, first_draft=body.first_draft) if (body.plan or body.first_draft) else None
    await _record("studio_project_created", {
        "project_id": row.id, "scenes": len(json.loads(row.doc_json)["scenes"]), "aspect_ratio": row.aspect_ratio,
        "visual_style": body.visual_style, "first_draft": body.first_draft, "plan": body.plan,
        "from_topic": bool(body.start.topic), "from_sample": bool(body.start.sample_id),
    })
    return {"plan_job_id": plan_job_id, **await store.envelope(row)}


async def saved_settings() -> dict | None:
    """A new project's voice and captions from the user's saved Settings when
    the caller sent none (a new browser, MCP) — one memory of "my voice". A
    stored "viral" is the old column default nobody chose, so it reads as unset."""
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.models.user_settings import UserSettings
    async with AsyncSessionLocal() as db:
        prefs = (await db.execute(select(UserSettings).where(UserSettings.user_id == USER))).scalar_one_or_none()
    if prefs is None:
        return None
    out: dict = {}
    if prefs.tts_provider in sd.TTS_PROVIDERS:
        voice = {"provider": prefs.tts_provider}
        if prefs.preferred_tts_voice:
            voice["voice"] = prefs.preferred_tts_voice
        out["voice"] = voice
    captions: dict = {}
    if isinstance(prefs.caption_enabled, bool):
        captions["enabled"] = prefs.caption_enabled
    style = prefs.caption_style
    if style and style != "viral" and await caption_service_known(style):
        captions["style"] = style
    if captions:
        out["captions"] = captions
    return out or None


async def caption_service_known(style: str) -> bool:
    from backend.services.caption_service import style_id_known
    try:
        return await style_id_known(style)
    except Exception:  # noqa: BLE001
        return False


async def _record(event_type: str, data: dict) -> None:
    """Every meaningful studio action feeds the user model. Never blocks."""
    try:
        from backend.core.user_intelligence import UserIntelligence
        await UserIntelligence().record_event(event_type, data, USER)
    except Exception:  # noqa: BLE001
        logger.debug("record_event %s failed", event_type, exc_info=True)


# ── My audio ────────────────────────────────────────────────────────────────

def _audio_refused(e) -> HTTPException:
    return studio_error(400, e.code if isinstance(getattr(e, "code", None), str) else "invalid", str(e))


async def _start_listening(project_id: str) -> str:
    """The listening job of an audio project (Whisper: the heavy lane)."""
    from backend.agents.job_helper import create_job
    from backend.core import task_runner
    from backend.core.tasks import studio as studio_tasks
    from backend.services import studio_audio
    job = await create_job(studio_audio.JOB_TYPE, USER, {"project_id": project_id}, title="Listening to your audio")
    task_runner.dispatch(studio_tasks.run_studio_audio(job.id, project_id, USER))
    return job.id


@router.post("/studio/projects/from-audio")
async def create_project_from_audio(file: UploadFile = File(...), title: str | None = Form(None),
                                    aspect_ratio: str = Form("9:16"), visual_style: str = Form("cinematic"),
                                    settings: str | None = Form(None)):
    """My audio: a project that speaks in the user's own recording. Checked before anything is
    made — 400 `media` (not audio or video) / `too_large` / `no_audio` / `too_short` / `too_long`
    — then an empty `input.mode="audio"` project holding its OWN COPY of the recording, and the
    listening job that lands its scenes. `{job_id, project}`; the editor shows "Listening…" from
    `derived.audio_listening`."""
    from backend.config import settings as app_settings
    from backend.services import studio_audio
    from backend.services.own_audio import AudioRefused
    overrides = None
    if settings:
        try:
            overrides = json.loads(settings)
        except ValueError:
            overrides = None
        if not isinstance(overrides, dict):
            raise studio_error(400, "invalid", "Those settings weren't valid.")
        style = (overrides.get("captions") or {}).get("style") if isinstance(overrides.get("captions"), dict) else None
        if style is not None:
            await _check_caption_style(style, "invalid")
    overrides = overrides or await saved_settings()

    name = file.filename or "audio"
    suffix = Path(name).suffix.lower()
    if suffix not in studio_audio.UPLOAD_SUFFIXES:
        raise studio_error(400, "media", studio_audio.BAD_TYPE)
    await asyncio.to_thread(app_settings.TMP_DIR.mkdir, parents=True, exist_ok=True)
    part = app_settings.TMP_DIR / f"studio_audio_in_{secrets.token_hex(8)}{suffix}"
    size = 0
    try:
        # Off the event loop, 1 MB at a time, and never past the limit.
        out = await asyncio.to_thread(open, part, "wb")
        try:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > studio_audio.MAX_BYTES:
                    raise studio_error(400, "too_large", studio_audio.TOO_LARGE)
                await asyncio.to_thread(out.write, chunk)
        finally:
            await asyncio.to_thread(out.close)
        if size == 0:
            raise studio_error(400, "no_audio", studio_audio.EMPTY)
        try:
            row = await studio_audio.create(part, (title or "").strip() or name, aspect_ratio=aspect_ratio,
                                            visual_style=visual_style, settings_overrides=overrides, user_id=USER)
        except AudioRefused as e:
            raise _audio_refused(e)
        except StudioDocError as e:
            raise studio_error(400, "invalid", str(e))
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — never echo an internal error (paths, ffmpeg output) to the page
        logger.warning("studio: the audio %r couldn't be made a project", name, exc_info=True)
        raise studio_error(400, "media", "That file couldn't be read — try another file.")
    finally:
        await asyncio.to_thread(part.unlink, True)
    try:
        job_id = await _start_listening(row.id)
    except BaseException:
        await store.delete_project(row.id, USER)          # nothing will ever listen to it
        raise
    await _record("studio_project_created", {"project_id": row.id, "scenes": 0, "aspect_ratio": row.aspect_ratio,
                                             "own_audio": True})
    return {"job_id": job_id, **await store.envelope(row)}


@router.post("/studio/projects/{project_id}/listen")
async def listen_again(project_id: str):
    """Try the listening again on the project's own copy of the recording: after a failed or
    stopped one. 400 `invalid` unless it is an audio project still without scenes, 409 `busy` while
    one runs, 400 `source_gone` when the copy is no longer on this computer."""
    from backend.services import studio_audio
    try:
        row = await store.get_project(project_id, USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    doc = json.loads(row.doc_json)
    if not sd.is_audio_mode(doc) or doc["scenes"]:
        raise studio_error(400, "invalid", studio_audio.NOT_WAITING)
    running = await studio_audio.live_job_id(project_id)
    if running:
        raise studio_error(409, "busy", studio_audio.BUSY, job_id=running)
    if studio_audio.source_file(project_id) is None:
        raise studio_error(400, "source_gone", studio_audio.SOURCE_GONE)
    job_id = await _start_listening(project_id)
    return {"job_id": job_id, **await store.envelope(row)}


async def _start_plan(project_id: str, *, first_draft: bool, scene_uids: list[str] | None = None) -> str:
    """One plan per project at a time (a plan is two AI calls plus a stock
    search per scene against the Pexels rate limit): a second request while
    one runs gets the running job. Planning is network I/O — the light pool."""
    from backend.agents.job_helper import create_job
    from backend.core import task_runner
    from backend.core.tasks import studio as studio_tasks
    running = studio_tasks.PLANS_IN_FLIGHT.get(project_id)
    if running:
        return running
    studio_tasks.PLANS_IN_FLIGHT[project_id] = "pending"        # taken before the await below
    try:
        job = await create_job("studio_plan", USER, {"project_id": project_id, "first_draft": first_draft,
                                                     **({"scene_uids": scene_uids} if scene_uids else {})},
                               title="Re-planning a scene" if scene_uids else "Planning scenes")
    except BaseException:
        studio_tasks.PLANS_IN_FLIGHT.pop(project_id, None)
        raise
    studio_tasks.PLANS_IN_FLIGHT[project_id] = job.id
    task_runner.dispatch_light(studio_tasks.run_studio_plan(job.id, project_id, USER, first_draft, scene_uids))
    return job.id


class PlanBody(BaseModel):
    scene_uids: list[str] | None = None


@router.post("/studio/projects/{project_id}/plan")
async def plan_project(project_id: str, first_draft: bool = False, body: PlanBody | None = None):
    """One scene-planning AI call + stock search + re-rank. Lands only on
    scenes you haven't chosen (pinned) or put your own clip on. `scene_uids`
    plans just those scenes ("Re-plan this scene")."""
    try:
        await store.get_project(project_id, USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    uids = [u for u in (body.scene_uids if body else None) or [] if isinstance(u, str)][:sd.SCENE_MAX] or None
    return {"job_id": await _start_plan(project_id, first_draft=first_draft, scene_uids=uids)}



class AssistBody(BaseModel):
    uid: str = Field(max_length=40)
    action: Literal["punchier", "shorter", "clearer", "hook", "custom"]
    instruction: str | None = Field(default=None, max_length=300)


@router.post("/studio/projects/{project_id}/assist")
async def assist_scene(project_id: str, body: AssistBody):
    """One AI call (the user's key): up to three rewrites of one scene's line —
    punchier, shorter, clearer, hook variants for the opening, or the
    creator's own instruction. Nothing is changed here: the editor applies a
    pick with the ordinary `set_line` op. `{lines: [..], query: str | null}`."""
    from backend.services import studio_assist
    try:
        row = await store.get_project(project_id, USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    doc = json.loads(row.doc_json)
    if not any(s["uid"] == body.uid for s in doc["scenes"]):
        raise studio_error(404, "not_found", "That scene no longer exists.")
    if body.action == "custom" and not (body.instruction or "").strip():
        raise studio_error(400, "blocked", "Say what you'd like changed.")
    try:
        return await studio_assist.suggest(doc, body.uid, body.action, body.instruction)
    except studio_assist.AssistError as e:
        raise studio_error(502, "assist_failed", str(e))


async def resolve_project_id(project_id: str) -> str:
    """`latest` = the most recently edited project (chat/MCP: "change the
    video I just made" without knowing its id)."""
    if project_id != "latest":
        return project_id
    items = await store.list_projects(limit=1, user_id=USER)
    if not items:
        raise studio_error(404, "not_found", "There are no Smart Video projects yet.")
    return items[0]["id"]


class EditBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=500)


@router.post("/studio/projects/{project_id}/edit")
async def edit_project_in_words(project_id: str, body: EditBody):
    """One AI call (the user's key) turns a sentence ("make the hook
    stronger", "use drone footage in scene 3") into the editor's own ops on
    THIS project (studio_edit); a re-render is its own step.
    `{applied: [sentences], summary, project}`."""
    from backend.services import studio_edit
    project_id = await resolve_project_id(project_id)
    try:
        return await studio_edit.edit_project(project_id, body.instruction, user_id=USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    except studio_edit.EditError as e:
        raise studio_error(400, "edit_failed", str(e))
    except StudioOpError as e:
        raise studio_error(400, "invalid_edit", plain_op_error(e))
    except StudioConflictError:
        raise studio_error(409, "revision_conflict", "This project changed in the meantime — try again.")


@router.get("/studio/projects/{project_id}")
async def get_project(project_id: str, known_revision: int | None = None):
    """The project envelope. With `known_revision` (the editor's poll while
    something generates): when the stored revision is still that one, a cheap
    `{unchanged: true, revision, status, render_alive}` instead of the full
    envelope — WS `studio_project_updated` still triggers full refetches.
    `latest` reads the most recently edited project."""
    project_id = await resolve_project_id(project_id)
    try:
        if known_revision is not None:
            revision, status = await store.project_head(project_id, USER)
            if revision == known_revision:
                from backend.services import studio_audio
                return {"unchanged": True, "revision": revision, "status": status,
                        "render_alive": status == "rendering" and await store.render_alive(project_id),
                        # Present only while a listening job runs: the editor keeps its poll on.
                        **({"audio_alive": True} if await studio_audio.live_job_id(project_id) else {})}
        return await store.envelope(await store.get_project(project_id, USER))
    except StudioNotFoundError as e:
        raise _not_found(e)


@router.patch("/studio/projects/{project_id}")
async def edit_project(project_id: str, body: OpsBody):
    for op in body.ops:
        if isinstance(op, dict) and op.get("op") == "set_setting" and op.get("path") == "captions.style":
            await _check_caption_style(op.get("value"), "invalid_edit")
    try:
        row, result = await store.write_ops(project_id, body.ops, if_revision=body.if_revision, user_id=USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    except StudioOpError as e:
        raise studio_error(400, "invalid_edit", plain_op_error(e))
    except StudioConflictError as e:
        latest = await store.envelope(await store.get_project(project_id, USER))
        raise studio_error(409, "revision_conflict", str(e), **latest)
    return {**await store.envelope(row), "created": result.created, "changes": result.changes}


@router.delete("/studio/projects/{project_id}")
async def delete_project(project_id: str):
    """Refused while the project renders: deleting mid-render would leave the
    render running, write a Library video for a deleted project and orphan
    its files. Voice is NOT blocked: deleting mid-voice cancels that job
    quietly by design."""
    try:
        await store.get_project(project_id, USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    if await store.render_alive(project_id):
        raise studio_error(409, "busy", "This project is still rendering — "
                                        "delete it once that finishes (or cancel it first).")
    try:
        await store.delete_project(project_id, USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    return {"ok": True}


@router.post("/studio/projects/{project_id}/voice")
async def voice_scenes(project_id: str, body: VoiceBody):
    """Record voice takes for scenes that need one (none / stale / failed, or
    ready with its take gone from disk). Scenes are marked generating in the
    same write that authorises the job, so a concurrent request skips them.
    A voice that needs an API key is refused here when none is set — not
    failed scene by scene."""
    from backend.agents.job_helper import create_job
    from backend.core import task_runner
    from backend.core.exceptions import VoiceGenerationError
    from backend.core.tasks import studio as studio_tasks
    from backend.services import voice_take_service as vts
    from backend.services.studio_checks import needs_voice
    from backend.services.tts_service import PROVIDER_INFO, TTSProvider, tts_api_key

    try:
        row = await store.get_project(project_id, USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    doc = json.loads(row.doc_json)
    try:
        spec = vts.VoiceSpec.from_settings(doc["settings"])
    except VoiceGenerationError as e:
        raise studio_error(400, "unsupported_voice", str(e))
    wanted = set(s["uid"] for s in doc["scenes"]) if body.scene_uids == "all" else set(body.scene_uids)
    targets = [s for s in doc["scenes"] if s["uid"] in wanted and needs_voice(s, vts.is_cached)]
    if not targets:
        return {"job_id": None, **await store.envelope(row)}
    info = PROVIDER_INFO[TTSProvider(spec.provider)]
    if info["needs_key"] and not tts_api_key(spec.provider):
        raise studio_error(400, "needs_key", f"{info['label']} needs an API key (OPENAI_API_KEY) — add it, "
                                             "or switch to the free Edge voice.")

    uids = [s["uid"] for s in targets]
    # Marked under the revision that was READ: a voice switch landing in the
    # awaits above (e.g. Edge → OpenAI) must not record takes in a voice the
    # user didn't ask for. A concurrent write that left the voice alone (a
    # plan job landing) is retried; anything else asks the user to try again.
    read_identity = sd.voice_identity(doc["settings"])
    for attempt in range(3):
        try:
            row, _ = await store.write_ops(project_id, [
                {"op": "voice_result", "uid": uid, "voice": {"state": "generating"}} for uid in uids
            ], if_revision=row.revision, internal=True, user_id=USER)
            break
        except StudioConflictError:
            try:
                row = await store.get_project(project_id, USER)
            except StudioNotFoundError as e:
                raise _not_found(e)
            latest = json.loads(row.doc_json)
            by_uid = {s["uid"]: s for s in latest["scenes"]}
            unchanged = sd.voice_identity(latest["settings"]) == read_identity and all(
                uid in by_uid and by_uid[uid]["line"] == s["line"]
                and needs_voice(by_uid[uid], vts.is_cached)
                for uid, s in ((t["uid"], t) for t in targets))
            if not unchanged or attempt == 2:
                raise studio_error(409, "revision_conflict", "This project changed in the meantime — try again.",
                                   **await store.envelope(row))

    try:
        job = await create_job("studio_voice", USER, {"project_id": project_id, "scenes": len(uids)},
                               title="Voicing scenes")
    except BaseException:
        # The mark above is a status claim, and nothing is going to record
        # these scenes. Released here, or they spin with no job behind them —
        # blocking Render ("still being voiced") and skipped by Voice all —
        # until a restart's boot sweep settles them.
        await studio_tasks._settle_generating(project_id, uids, USER)
        raise
    # Edge and OpenAI voices are network I/O — the light pool.
    task_runner.dispatch_light(studio_tasks.run_studio_voice(job.id, project_id, uids, USER))
    return {"job_id": job.id, **await store.envelope(row)}


@router.get("/studio/voice/{take_key}")
async def voice_take_audio(take_key: str, request: Request):
    from backend.api.videos import serve_media_with_range
    from backend.services import voice_take_service as vts
    path = vts.take_path(take_key)                  # validates the key before touching the disk
    if path is None:
        raise studio_error(404, "not_found", "No such voice take.")
    return serve_media_with_range(path, request)


@router.get("/studio/voice/{take_key}/meta")
async def voice_take_meta(take_key: str):
    from backend.services import voice_take_service as vts
    meta = await vts.take_details(take_key)
    if meta is None:
        raise studio_error(404, "not_found", "No such voice take.")
    return {"key": take_key, "duration": meta.get("duration"), "chars": meta.get("chars"),
            "provider": meta.get("provider"), "peaks": meta.get("peaks") or [], "words": meta.get("words")}


# ── media ───────────────────────────────────────────────────────────────────

@router.get("/studio/projects/{project_id}/candidates")
async def stock_candidates(project_id: str, q: str = ""):
    """Stock search for this project. Results are remembered server-side so a
    later bind_visual binds OUR copy of the candidate."""
    from backend.services import studio_media
    try:
        row = await store.get_project(project_id, USER)
        return {"candidates": await studio_media.search_candidates(project_id, q, row.aspect_ratio)}
    except StudioNotFoundError as e:
        raise _not_found(e)
    except StudioMediaError as e:
        raise studio_error(400, "media", str(e))


@router.post("/studio/projects/{project_id}/media/preview")
async def stock_preview(project_id: str, body: PreviewBody):
    from backend.services import studio_media
    try:
        await store.get_project(project_id, USER)
        canonical = studio_media.resolve_candidate_for(project_id)(body.candidate)
        if canonical is None:
            raise StudioMediaError("That clip isn't one of this project's search results — search again.")
        name = await studio_media.ensure_preview(canonical)
    except StudioNotFoundError as e:
        raise _not_found(e)
    except StudioMediaError as e:
        raise studio_error(400, "media", str(e))
    return {"url": f"/api/studio/preview/{name}"}


@router.get("/studio/preview/{name}")
async def serve_preview(name: str, request: Request):
    from backend.api.videos import serve_media_with_range
    from backend.services import studio_media
    path = studio_media.preview_path(name)
    if path is None:
        raise studio_error(404, "not_found", "No such preview.")
    return serve_media_with_range(path, request)


async def _add_imports(project_id: str, records: list[dict]):
    def mutate(assets):
        for r in records:
            assets["imports"][r["asset_id"]] = r
        return assets
    return await store.update_assets(project_id, mutate, user_id=USER)


@router.post("/studio/projects/{project_id}/media/upload")
async def upload_media(project_id: str, file: UploadFile = File(...)):
    from backend.services import studio_media
    from backend.services.studio_media import IMAGE_SUFFIXES, UPLOAD_MAX_BYTES as MAX_FILE_BYTES
    try:
        await store.get_project(project_id, USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    name = file.filename or "upload"
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix not in IMAGE_SUFFIXES and suffix not in studio_media.VIDEO_SUFFIXES:
        raise studio_error(400, "media", "Upload a video (mp4, mov, webm) or an image.")
    folder = studio_media.project_assets_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / f".upload_{studio_media.new_asset_id()}{suffix}"
    size = 0
    try:
        # Writes off the event loop (up to MAX_FILE_BYTES, 1 MB at a time).
        out = await asyncio.to_thread(open, part, "wb")
        try:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise StudioMediaError(f"That file is over {MAX_FILE_BYTES // (1024 * 1024)} MB.")
                await asyncio.to_thread(out.write, chunk)
        finally:
            await asyncio.to_thread(out.close)
        if suffix in IMAGE_SUFFIXES:
            record = await studio_media.add_image(project_id, part, title=name, source="upload")
        else:
            record = await studio_media.add_video(project_id, part, title=name, source="upload")
    except StudioMediaError as e:
        raise studio_error(400, "media", str(e))
    except Exception:  # noqa: BLE001 — never echo an internal error (paths, ffmpeg output) to the page
        logger.warning("studio: upload %r couldn't be read", name, exc_info=True)
        raise studio_error(400, "media", "That file couldn't be read — try another file.")
    finally:
        part.unlink(missing_ok=True)
    try:
        row = await _add_imports(project_id, [record])
    except BaseException:
        for poster in (False, True):          # never an unindexed file in the project folder
            leftover = studio_media.asset_file(project_id, record, poster=poster)
            if leftover is not None:
                leftover.unlink(missing_ok=True)
        raise
    return {"asset": studio_media.public_asset(project_id, record), **await store.envelope(row)}


@router.get("/studio/media/{project_id}/{asset_id}")
async def serve_project_media(project_id: str, asset_id: str, request: Request, poster: bool = False):
    from backend.api.videos import serve_media_with_range
    from backend.services import studio_media
    if not studio_media.is_asset_id(asset_id):
        raise studio_error(404, "not_found", "No such media.")
    try:
        row = await store.get_project(project_id, USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    record = store.find_asset_record(store.assets_of(row), asset_id)
    path = studio_media.asset_file(project_id, record, poster=poster) if record else None
    if path is None:
        raise studio_error(404, "not_found", "No such media.")
    return serve_media_with_range(path, request)


def _rendering() -> HTTPException:
    return studio_error(409, "already_rendering", "This project is rendering — try again once the render finishes.")


# ── render v2 ───────────────────────────────────────────────────────────────

@router.post("/studio/projects/{project_id}/render")
async def render_project(project_id: str):
    """Render exactly what's in the project. Refused with the blockers (400) or
    while another render of this project runs (409 already_rendering).
    Answers the job id plus the envelope."""
    from backend.agents.job_helper import create_job
    from backend.core import task_runner
    from backend.core.tasks import studio as studio_tasks
    from backend.services.studio_checks import checks_for

    try:
        row = await store.get_project(project_id, USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    blockers = checks_for(json.loads(row.doc_json))["blockers"]
    if blockers:
        raise studio_error(400, "blocked", blockers[0], blockers=blockers)
    try:
        await store.claim_render(project_id, USER)
    except StudioConflictError as e:
        raise studio_error(409, "already_rendering", str(e))
    try:
        job = await create_job("generate", USER, {"project_id": project_id, "source": "studio"}, title=row.title)
        task_runner.dispatch(studio_tasks.run_studio_render(job.id, project_id, USER))
    except Exception:
        await store.abandon_render(project_id, user_id=USER)     # release the claim — nothing is rendering
        raise
    doc = json.loads(row.doc_json)
    await _record("studio_render_started", {
        "project_id": project_id, "via": "studio", "scenes": len(doc["scenes"]),
        "aspect_ratio": doc["aspect_ratio"], "tts_provider": doc["settings"]["voice"]["provider"],
        "user_clips": sum(1 for s in doc["scenes"] if s["visual"]["kind"] == "user_clip"),
    })
    return {"job_id": job.id, **await store.envelope(await store.get_project(project_id, USER))}


@router.delete("/studio/projects/{project_id}/media/{asset_id}")
async def delete_media(project_id: str, asset_id: str):
    """Remove an uploaded clip from the project. Refused (409) while a scene
    still uses it, so no scene is left pointing at a deleted file."""
    from backend.services import studio_media
    if not studio_media.is_asset_id(asset_id):
        raise studio_error(404, "not_found", "No such media.")
    try:
        row = await store.get_project(project_id, USER)
    except StudioNotFoundError as e:
        raise _not_found(e)
    record = store.assets_of(row)["imports"].get(asset_id)
    if record is None:
        raise studio_error(404, "not_found", "No such media.")
    # A render reads the clip from disk while it runs; unbind-then-delete used
    # to pull it out from under the render ("clip missing"), and on Windows the
    # unlink of an open file raised a 500 after the index was already updated.
    if await store.render_alive(project_id):
        raise _rendering()
    files = [studio_media.asset_file(project_id, record), studio_media.asset_file(project_id, record, poster=True)]

    def still_used(doc, assets):
        # On the SAME row the removal is written to: a bind that landed after
        # the read above is seen here, so no scene is left on a deleted file.
        in_use = [i + 1 for i, s in enumerate(doc["scenes"])
                  if s["visual"]["kind"] == "user_clip" and (s["visual"].get("asset") or {}).get("asset_id") == asset_id]
        if in_use:
            raise studio_error(409, "in_use",
                               f"Scene {', '.join(map(str, in_use))} still uses this clip — change "
                               f"{'it' if len(in_use) == 1 else 'them'} first.", scenes=in_use)

    def forget(assets):
        assets["imports"].pop(asset_id, None)
        return assets

    try:
        row = await store.update_assets(project_id, forget, user_id=USER, guard=still_used)
    except StudioNotFoundError as e:
        raise _not_found(e)
    for path in {f for f in files if f is not None}:
        path.unlink(missing_ok=True)
    return await store.envelope(row)
