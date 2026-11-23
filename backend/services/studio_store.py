# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Smart Video Studio — project persistence.

The only module that reads or writes `video_projects` rows. It turns the pure
layers (studio_doc, studio_ops, studio_checks) into stored projects:

  * `write_ops` — the one write path. Applies ops to the stored document and
    persists with `UPDATE … WHERE revision = <the revision it read>`, so two
    writers can never both win. A client write that names a stale revision
    gets StudioConflictError (409); server-side job results retry on a race
    instead, so a finished take is never lost to a concurrent edit.
  * `envelope` — what every read/write returns: document, derived timing,
    render checks and revision.
  * The project's uploaded files (`assets_json`) are read into the op context
    from the same row the write is conditioned on.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import InvalidRequestError

from backend.config import settings
from backend.core.exceptions import StudioConflictError, StudioNotFoundError
from backend.database import AsyncSessionLocal
from backend.models.video_project import VideoProject
from backend.services import studio_doc as sd
from backend.services.studio_ops import OpContext, OpResult, apply_ops

logger = logging.getLogger(__name__)

_INTERNAL_WRITE_ATTEMPTS = 5


def _voice_label(voice_id: str) -> str:
    """A short display name for a voice id: "en-US-AvaMultilingualNeural" →
    "Ava", "Kore" → "Kore". Only used when a caller changes the voice without
    sending a label."""
    name = voice_id.split("-")[-1] if voice_id.count("-") >= 2 else voice_id
    for suffix in ("MultilingualNeural", "Neural"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
    return name

# The free voice a script in this language starts on, when the carried-over
# Edge voice speaks another (a Chinese script would otherwise get "Andrew").
LANGUAGE_VOICES = {
    "zh": ("zh-CN-XiaoxiaoNeural", "Xiaoxiao", "Chinese"),
    "ja": ("ja-JP-NanamiNeural", "Nanami", "Japanese"),
    "ko": ("ko-KR-SunHiNeural", "SunHi", "Korean"),
}


def script_language(text: str) -> str | None:
    """'zh' / 'ja' / 'ko' when the script is mostly that script, 'latin' when it
    has no CJK at all, None when mixed or unsure."""
    han = kana = hangul = latin = 0
    for ch in text:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF:
            kana += 1
        elif 0x3400 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF:
            han += 1
        elif 0xAC00 <= cp <= 0xD7AF:
            hangul += 1
        elif ch.isalpha():
            latin += 1
    cjk = han + kana + hangul
    if cjk == 0:
        return "latin" if latin else None
    if cjk * 4 < latin:              # a CJK name inside an English script
        return None
    if hangul >= max(han, kana):
        return "ko"
    if kana:
        return "ja"
    return "zh"


def match_voice_to_script(settings_doc: dict, script: str) -> str | None:
    """Swap a free Edge voice that can't speak the script's language for that
    language's default; returns a one-line notice when it did. OpenAI voices
    are multilingual — untouched."""
    voice = settings_doc.get("voice") or {}
    if sd.normalise_tts_provider(voice.get("provider")) != "edge_tts":
        return None
    vid = str(voice.get("voice") or "")
    lang = script_language(script)
    voice_lang = vid.split("-")[0].lower() if vid.count("-") >= 2 else ""
    if lang in LANGUAGE_VOICES and voice_lang != lang:
        new_id, label, name = LANGUAGE_VOICES[lang]
        settings_doc["voice"] = {**voice, "voice": new_id, "label": label, "instructions": None}
        return f"Voice matched to {name}: {label} (free). Change it in the Voice panel."
    if lang == "latin" and voice_lang in LANGUAGE_VOICES:
        settings_doc["voice"] = sd.default_settings()["voice"]
        return "Voice matched to the script: Andrew (free). Change it in the Voice panel."
    return None


def assets_of(row: VideoProject) -> dict:
    try:
        raw = json.loads(row.assets_json or "{}")
    except ValueError:
        raw = {}
    imports = raw.get("imports") if isinstance(raw.get("imports"), dict) else {}
    return {"imports": imports}


def _doc(row: VideoProject) -> dict:
    return json.loads(row.doc_json)


def op_context(assets: dict, project_id: str) -> OpContext:
    from backend.services import studio_media
    from backend.services import voice_take_service as vts
    return OpContext(
        take_known=vts.is_cached,
        project_assets={aid: studio_media.doc_asset(a) for aid, a in assets["imports"].items()},
        # Stock binds only through the server's copy of a search result.
        resolve_candidate=studio_media.resolve_candidate_for(project_id),
    )


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def summary(row: VideoProject) -> dict:
    doc = _doc(row)
    return {
        "id": row.id, "title": row.title, "status": row.status, "aspect_ratio": row.aspect_ratio,
        "visual_style": doc["settings"]["visual_style"], "scenes": len(doc["scenes"]),
        "duration_seconds": row.duration_seconds, "last_video_id": row.last_video_id,
        "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
        # `rendering` in the row can be a dead process's leftover claim; the
        # editor only shows a render in progress when one is really running.
        "render_alive": row.status == "rendering" and render_running_here(row.id),
    }


async def envelope(row: VideoProject) -> dict:
    from backend.services import studio_media
    from backend.services.studio_checks import checks_for
    doc = _doc(row)
    assets = assets_of(row)
    return {"project": {
        **summary(row),
        "revision": row.revision,
        "doc": doc,
        "derived": sd.derive_timeline(doc),
        # What blocks a render and what's worth knowing first — the editor's
        # Render dialog and the render endpoint read the same function.
        "checks": checks_for(doc),
        "media": [studio_media.public_asset(row.id, a) for a in assets["imports"].values()],
        # The running render's job, so an editor that mounts mid-render (a
        # reload, or navigating away and back) re-attaches to its progress and
        # its finished-video card instead of showing "Rendering 0%" and then
        # a Render button that invites a second render.
        "render_job_id": await render_job_id(row.id) if row.status == "rendering" else None,
        # Likewise a running plan: a reload mid-draft used to lose its banner.
        "plan_job_id": _plan_job_id(row.id),
    }}


def _plan_job_id(project_id: str) -> str | None:
    from backend.core.tasks.studio import PLANS_IN_FLIGHT
    job = PLANS_IN_FLIGHT.get(project_id)
    return job if job and job != "pending" else None


async def create_project(*, script: str, title: str | None = None, aspect_ratio: str = "9:16",
                         visual_style: str = "cinematic", input_meta: dict | None = None,
                         settings_overrides: dict | None = None,
                         user_id: str = "local") -> VideoProject:
    lines = sd.lines_for_new_project(script)              # StudioDocError → 400
    settings_doc = sd.default_settings(visual_style=visual_style if visual_style in sd.VISUAL_STYLES else "cinematic")
    # The preferences a user (or the legacy /generate/stock adapter) carries
    # into a new project; validate_doc normalises them (an unknown voice
    # provider falls back to the free one).
    for key in ("voice", "captions", "music"):
        if isinstance((settings_overrides or {}).get(key), dict):
            override = settings_overrides[key]
            if key == "voice" and sd.normalise_tts_provider(override.get("provider")) not in (
                    None, settings_doc["voice"]["provider"]):
                # A different provider replaces the voice WHOLE (like the
                # client's mergeRemembered): merged, {"provider": "openai_tts"}
                # would keep the free voice's id and label — "Andrew" in the
                # editor, another voice in the take. validate_doc fills the
                # provider's own default voice.
                settings_doc[key] = dict(override)
            else:
                merged = {**settings_doc[key], **override}
                if (key == "voice" and override.get("voice")
                        and override.get("voice") != settings_doc[key].get("voice")
                        and not override.get("label")):
                    # Same provider, different voice, no label sent (the
                    # /generate/stock adapter, MCP): the merge would keep the
                    # default's "Andrew" while the takes speak as the
                    # requested voice.
                    merged["label"] = _voice_label(override["voice"])
                settings_doc[key] = merged
    if isinstance((settings_overrides or {}).get("transition"), str):
        settings_doc["transition"] = settings_overrides["transition"]
    if isinstance((settings_overrides or {}).get("post"), dict):
        settings_doc["post"] = sd.normalise_post({**settings_doc["post"], **settings_overrides["post"]})
    voice_note = match_voice_to_script(settings_doc, script)
    if voice_note:
        try:
            from backend.core.ws_manager import ws_manager
            await ws_manager.send_constraint_warning("voice_language_matched", voice_note,
                                                     severity="info", user_id=user_id)
        except Exception:  # noqa: BLE001 — a notice must never fail a project
            logger.debug("voice_language_matched notice failed", exc_info=True)
    doc = sd.validate_doc({
        "aspect_ratio": aspect_ratio,
        "input": {"mode": "script", **(input_meta or {})},
        "settings": settings_doc,
        "scenes": [sd.make_scene(line, query=sd.query_from_line(line)) for line in lines],
    }, settle=False)
    row = VideoProject(
        user_id=user_id,
        title=(sd.normalise_line(title)[:120] if title else "") or lines[0][:60],
        status="draft",
        aspect_ratio=doc["aspect_ratio"],
        doc_json=json.dumps(doc, ensure_ascii=False),
        assets_json=json.dumps({"imports": {}}),
        revision=1,
        duration_seconds=sd.derive_timeline(doc)["total"],
        scene_count=len(doc["scenes"]),
        visual_style=doc["settings"]["visual_style"],
    )
    async with AsyncSessionLocal() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def get_project(project_id: str, user_id: str = "local") -> VideoProject:
    async with AsyncSessionLocal() as db:
        row = await db.get(VideoProject, project_id)
    if row is None or row.user_id != user_id:
        raise StudioNotFoundError("That project doesn't exist.")
    return row


_LIST_COLUMNS = (VideoProject.id, VideoProject.title, VideoProject.status, VideoProject.aspect_ratio,
                 VideoProject.scene_count, VideoProject.visual_style, VideoProject.duration_seconds,
                 VideoProject.last_video_id, VideoProject.created_at, VideoProject.updated_at)


async def list_projects(*, limit: int = 24, q: str | None = None, user_id: str = "local") -> list[dict]:
    """The Home list. Reads only the summary columns — never every project's
    whole document."""
    stmt = (select(*_LIST_COLUMNS).where(VideoProject.user_id == user_id)
            .order_by(VideoProject.updated_at.desc()).limit(max(1, min(200, limit))))
    if q and q.strip():
        stmt = stmt.where(VideoProject.title.ilike(f"%{q.strip()[:100]}%"))
    async with AsyncSessionLocal() as db:
        rows = [dict(r._mapping) for r in (await db.execute(stmt)).all()]
    return [{
        "id": r["id"], "title": r["title"], "status": r["status"], "aspect_ratio": r["aspect_ratio"],
        "visual_style": r["visual_style"] or "cinematic", "scenes": r["scene_count"] or 0,
        "duration_seconds": r["duration_seconds"], "last_video_id": r["last_video_id"],
        "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"]),
        "render_alive": r["status"] == "rendering" and render_running_here(r["id"]),
    } for r in rows]


async def project_head(project_id: str, user_id: str = "local") -> tuple[int, str]:
    """(revision, status) without reading the document — the editor's poll."""
    async with AsyncSessionLocal() as db:
        found = (await db.execute(select(VideoProject.revision, VideoProject.status)
                                  .where(VideoProject.id == project_id, VideoProject.user_id == user_id))).first()
    if found is None:
        raise StudioNotFoundError("That project doesn't exist.")
    return int(found[0]), str(found[1])


def _project_dir(project_id: str):
    root = (settings.STUDIO_DIR / "projects").resolve()
    path = (root / project_id).resolve()
    return path if path.parent == root else None


async def delete_project(project_id: str, user_id: str = "local") -> None:
    await get_project(project_id, user_id)
    async with AsyncSessionLocal() as db:
        await db.execute(delete(VideoProject).where(VideoProject.id == project_id, VideoProject.user_id == user_id))
        await db.commit()
    # Rendered videos are GeneratedVideo rows in the Library and stay.
    from backend.services import studio_media
    studio_media.forget(project_id)
    path = _project_dir(project_id)
    if path is not None and path.is_dir():
        await asyncio.to_thread(shutil.rmtree, path, True)


async def write_ops(project_id: str, ops: list, *, if_revision: int | None, internal: bool = False,
                    user_id: str = "local") -> tuple[VideoProject, OpResult]:
    """Apply ops and persist under the revision they were applied to.

    StudioOpError (bad edit) and StudioNotFoundError propagate. A stale
    client revision → StudioConflictError. Internal writes retry a lost race.
    """
    attempts = _INTERNAL_WRITE_ATTEMPTS if internal else 1
    for _attempt in range(attempts):
        async with AsyncSessionLocal() as db:
            row = await db.get(VideoProject, project_id)
            if row is None or row.user_id != user_id:
                raise StudioNotFoundError("That project doesn't exist.")
            if if_revision is not None and row.revision != if_revision:
                raise StudioConflictError("This project changed in the meantime.")
            read_revision = row.revision
            result = apply_ops(_doc(row), ops, op_context(assets_of(row), project_id), title=row.title, internal=internal)
            if not internal and not result.changes and not result.created:
                # e.g. set_line with the text the scene already has (a blur
                # right after ⌘↵): valid, but nothing moved — same revision.
                return row, result
            values = {
                "doc_json": json.dumps(result.doc, ensure_ascii=False),
                "revision": read_revision + 1,
                "title": result.title or row.title,
                "aspect_ratio": result.doc["aspect_ratio"],
                "duration_seconds": sd.derive_timeline(result.doc)["total"],
                "scene_count": len(result.doc["scenes"]),
                "visual_style": result.doc["settings"]["visual_style"],
                "updated_at": datetime.utcnow(),
            }
            if not internal and row.status == "rendered":
                values["status"] = "draft"        # a rendered project is still editable
            done = await db.execute(
                update(VideoProject)
                .where(VideoProject.id == project_id, VideoProject.revision == read_revision)
                .values(**values)
            )
            if done.rowcount == 1:
                await db.commit()
                try:
                    await db.refresh(row)
                except InvalidRequestError:
                    # Deleted between the commit and the read-back (a project
                    # deleted mid-voice): that is "gone", which every job
                    # treats as a quiet cancel — not a crash that failed it.
                    raise StudioNotFoundError("That project doesn't exist.") from None
                return row, result
            await db.rollback()
        if not internal:
            break
    raise StudioConflictError("This project changed in the meantime.")


async def update_assets(project_id: str, mutate, *, user_id: str = "local", guard=None) -> VideoProject:
    """Change the project's asset index (uploaded files) under the same
    revision guard as document writes, retrying a lost race with a short
    backoff. `mutate` gets a copy and returns the new {imports}.

    `guard(doc, assets)` runs on the SAME row the write is conditioned on and
    may raise to refuse (e.g. "a scene still uses this clip") — a check made
    before the call could be stale by the time the write lands.

    An upload already on disk must never be lost to a race: when the
    optimistic retries all lose, the write is made once more while holding
    SQLite's write lock, so it can't lose again."""
    import copy
    for attempt in range(_INTERNAL_WRITE_ATTEMPTS):
        async with AsyncSessionLocal() as db:
            row = await db.get(VideoProject, project_id)
            if row is None or row.user_id != user_id:
                raise StudioNotFoundError("That project doesn't exist.")
            read_revision = row.revision
            current = assets_of(row)
            if guard is not None:
                guard(_doc(row), current)
            new_assets = mutate(copy.deepcopy(current))
            done = await db.execute(
                update(VideoProject)
                .where(VideoProject.id == project_id, VideoProject.revision == read_revision)
                .values(assets_json=json.dumps(new_assets, ensure_ascii=False),
                        revision=read_revision + 1, updated_at=datetime.utcnow())
            )
            if done.rowcount == 1:
                await db.commit()
                await db.refresh(row)
                return row
            await db.rollback()
        await asyncio.sleep(0.02 * (2 ** attempt))
    return await _update_assets_locked(project_id, mutate, user_id=user_id, guard=guard)


async def _update_assets_locked(project_id: str, mutate, *, user_id: str, guard=None) -> VideoProject:
    """Read-modify-write inside ONE write transaction: the no-op UPDATE takes
    SQLite's write lock first, so no other writer can commit between the read
    and the write."""
    import copy
    async with AsyncSessionLocal() as db:
        await db.execute(update(VideoProject).where(VideoProject.id == project_id)
                         .values(revision=VideoProject.revision))
        row = (await db.execute(select(VideoProject).where(VideoProject.id == project_id)
                                .execution_options(populate_existing=True))).scalar_one_or_none()
        if row is None or row.user_id != user_id:
            await db.rollback()
            raise StudioNotFoundError("That project doesn't exist.")
        current = assets_of(row)
        if guard is not None:
            try:
                guard(_doc(row), current)
            except BaseException:
                await db.rollback()
                raise
        new_assets = mutate(copy.deepcopy(current))
        await db.execute(update(VideoProject).where(VideoProject.id == project_id).values(
            assets_json=json.dumps(new_assets, ensure_ascii=False), revision=VideoProject.revision + 1,
            updated_at=datetime.utcnow()))
        await db.commit()
        await db.refresh(row)
        return row


def find_asset_record(assets: dict, asset_id: str) -> dict | None:
    """An uploaded asset, by id."""
    return assets["imports"].get(asset_id)


# Renders running in THIS process, registered before the claim is written and
# released only when the runner has fully stopped. It closes what the job table
# can't: the gap between the claim and its job row, and a render whose job was
# cancelled but whose runner is still finishing an ffmpeg step (a new claim
# then would run two renders into the same project). A dead process leaves it
# empty.
_live_renders: set[str] = set()


def render_running_here(project_id: str) -> bool:
    return project_id in _live_renders


def release_render(project_id: str) -> None:
    """The render runner (or the endpoint that failed to start it) is done."""
    _live_renders.discard(project_id)


async def abandon_render(project_id: str, user_id: str = "local") -> None:
    """Give a render claim back: the project returns to draft (a no-op once the
    render marked it rendered) AND this process's lock on it is freed — always
    both, the lock even when the row write fails, or the project stays
    "already rendering" until a restart. For every path where a claimed render
    will not run, or has ended."""
    try:
        await finish_render(project_id, user_id=user_id)
    finally:
        release_render(project_id)


async def render_alive(project_id: str) -> bool:
    """A render of this project is running — here, or as a live job of a
    draining predecessor process (lifespan handoff)."""
    return render_running_here(project_id) or await _render_job_alive(project_id)


async def _render_job_alive(project_id: str) -> bool:
    """Is a pending/running render job for this project on the books? The
    boot sweep fails a dead process's jobs (database.sweep_stale_jobs) but
    never touched the project's `rendering` claim — without this, one crash
    mid-render made every later render 409 forever."""
    return await render_job_id(project_id) is not None


async def render_job_id(project_id: str) -> str | None:
    """The id of this project's pending/running render job, or None."""
    from backend.models.job import Job
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Job.id, Job.input_json).where(Job.job_type == "generate",
                                                 Job.status.in_(["pending", "running"]))
        )).all()
    for job_id, raw in rows:
        try:
            if (json.loads(raw or "{}") or {}).get("project_id") == project_id:
                return job_id
        except ValueError:
            continue
    return None


async def _try_claim(project_id: str, user_id: str) -> tuple[VideoProject | None, bool]:
    async with AsyncSessionLocal() as db:
        done = await db.execute(
            update(VideoProject)
            .where(VideoProject.id == project_id, VideoProject.user_id == user_id, VideoProject.status != "rendering")
            .values(status="rendering", updated_at=datetime.utcnow())
        )
        await db.commit()
        row = await db.get(VideoProject, project_id)
    return row, done.rowcount == 1


async def claim_render(project_id: str, user_id: str = "local") -> VideoProject:
    """Atomically mark the project rendering; a second render is refused.

    A claim whose render job no longer exists (the process died mid-render)
    is released and taken over, so a crash can't lock a project."""
    if render_running_here(project_id):                        # checked and taken with no await between
        raise StudioConflictError("This project is already rendering.")
    _live_renders.add(project_id)
    try:
        row, claimed = await _try_claim(project_id, user_id)
        if row is None or row.user_id != user_id:
            raise StudioNotFoundError("That project doesn't exist.")
        if not claimed and not await _render_job_alive(project_id):
            logger.warning("studio: releasing a render claim with no live job on project %s", project_id[:8])
            await finish_render(project_id, user_id=user_id)          # → draft (only while `rendering`)
            row, claimed = await _try_claim(project_id, user_id)
        if not claimed:
            raise StudioConflictError("This project is already rendering.")
    except BaseException:
        release_render(project_id)
        raise
    return row


async def finish_render(project_id: str, *, video_id: str | None = None, thumbnail_path: str | None = None,
                        user_id: str = "local") -> None:
    """Release the render claim: rendered (with the new video) or back to draft."""
    values = {"status": "rendered" if video_id else "draft", "updated_at": datetime.utcnow(),
              "revision": VideoProject.revision + 1}
    if video_id:
        values["last_video_id"] = video_id
        if thumbnail_path:
            values["thumbnail_path"] = thumbnail_path
    async with AsyncSessionLocal() as db:
        await db.execute(update(VideoProject)
                         .where(VideoProject.id == project_id, VideoProject.user_id == user_id,
                                VideoProject.status == "rendering")
                         .values(**values))
        await db.commit()


STUDIO_JOB_TYPES = ("studio_voice", "studio_plan")


async def _projects_with_live_studio_jobs() -> set[str]:
    from backend.models.job import Job
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Job.input_json).where(
            Job.job_type.in_(STUDIO_JOB_TYPES), Job.status.in_(["pending", "running"])))).scalars().all()
    live: set[str] = set()
    for raw in rows:
        try:
            pid = (json.loads(raw or "{}") or {}).get("project_id")
        except ValueError:
            continue
        if isinstance(pid, str):
            live.add(pid)
    return live


async def settle_orphaned_generating() -> int:
    """Scenes left `generating` by a process that died mid-job go back to a
    settled state, or they'd block Render ("still generating") and be skipped
    by Voice all forever (every status claim needs a release path).

    Per project: a project with a still pending/running studio job is left
    alone — that job may belong to a predecessor draining through a restart
    handoff and is about to land its result. Runs at boot AND each time the
    handoff watcher fails a predecessor's jobs, so a quick restart (jobs still
    "fresh" at boot) can't leave scenes generating for good."""
    live_projects = await _projects_with_live_studio_jobs()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(VideoProject.id, VideoProject.user_id)
                                 .where(VideoProject.doc_json.like('%"generating"%')))).all()
    settled = 0
    for project_id, user_id in rows:
        if project_id in live_projects:
            continue
        try:
            doc = _doc(await get_project(project_id, user_id))
        except StudioNotFoundError:
            continue
        ops = []
        for scene in doc["scenes"]:
            if scene["voice"].get("state") == "generating":
                state = "stale" if scene["voice"].get("take_key") else "none"
                ops.append({"op": "voice_result", "uid": scene["uid"], "voice": {"state": state}})
        if ops:
            try:
                await write_ops(project_id, ops, if_revision=None, internal=True, user_id=user_id)
                settled += len(ops)
            except Exception:  # noqa: BLE001 — one odd project never stops the sweep
                logger.warning("studio: could not settle generating scenes on %s", project_id[:8], exc_info=True)
    return settled
