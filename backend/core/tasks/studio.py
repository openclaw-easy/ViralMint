# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Smart Video Studio job runners.

run_studio_voice  — records the voice takes a project's scenes are waiting for.
run_studio_plan   — plans every open scene's stock footage (+ a first draft's voices).
run_studio_render — render v2: assembles exactly what the editor shows.

The voice endpoint has already marked the scenes `generating` (so a second
request skips them). Here, each scene is recorded through the shared take
cache and its result is landed with `expect`, so it applies only if the scene
is still generating the same line in the same voice. A take that lands nowhere
stays in the cache — asking for it again is instant.
"""
from __future__ import annotations

import json
import logging

from backend.core.exceptions import JobCancelledError, StudioError, StudioNotFoundError, VoiceGenerationError
from backend.core.task_runner import on_unstarted
from backend.services.voice_take_service import VOICE_CONCURRENCY  # noqa: F401 — re-exported (tests, render)

logger = logging.getLogger(__name__)

# project id → the plan job running for it (api/studio._start_plan). Released
# by the runner, so a second "Build storyboard" while one runs gets that job.
PLANS_IN_FLIGHT: dict[str, str] = {}


async def _settle_generating(project_id: str, uids: list[str], user_id: str) -> None:
    """Best effort: scenes this job marked generating but never finished go
    back to a settled state, so no spinner is left without a job behind it."""
    from backend.services import studio_store as store
    try:
        row = await store.get_project(project_id, user_id)
        doc = json.loads(row.doc_json)
        ops = []
        for scene in doc["scenes"]:
            if scene["uid"] in uids and scene["voice"]["state"] == "generating":
                state = "stale" if scene["voice"].get("take_key") else "none"
                ops.append({"op": "voice_result", "uid": scene["uid"], "voice": {"state": state}})
        if ops:
            await store.write_ops(project_id, ops, if_revision=None, internal=True, user_id=user_id)
    except StudioNotFoundError:
        pass                                  # deleted meanwhile — nothing left to settle
    except Exception:  # noqa: BLE001 — settling must never mask the original failure
        logger.warning("studio_voice: could not settle scenes for %s", project_id, exc_info=True)


async def _stamp_cancelled(job_id: str, *, step: str = "Cancelled") -> None:
    """A cancelled studio job gets its end stamped (`completed_at`, a final
    step). The cancel endpoint only flips the status; the terminal-state guard
    in update_job_status keeps cancelled → cancelled while refusing → success."""
    from backend.agents.job_helper import update_job_status
    try:
        await update_job_status(job_id, "cancelled", current_step=step)
    except Exception:  # noqa: BLE001 — the row is already `cancelled`; the stamp is bookkeeping
        logger.debug("could not stamp cancelled job %s", job_id[:8], exc_info=True)


async def _end_job(job_id: str, job_type: str, status: str, user_id: str, *, step: str | None = None,
                   error: str | None = None, output: dict | None = None, quiet: bool = False,
                   result: dict | None = None) -> None:
    """Finish a studio job AND tell the page. create_job announces every job
    (`job_started`), so the Activity panel lists it; without the matching
    `job_complete` / `job_failed` the Activity panel showed finished plan /
    voice jobs as running until the socket reconnected.
    `quiet`: the failure was already surfaced (e.g. a constraint warning).
    `result`: the job_complete payload when it differs from the stored output
    (the render's completion card needs the whole video summary)."""
    from backend.agents.job_helper import update_job_status
    from backend.core.ws_manager import ws_manager
    if status == "failed":
        await update_job_status(job_id, "failed", current_step=step, error_message=error, output_data=output)
        message = {"type": "job_failed", "job_id": job_id, "job_type": job_type, "error": error or "Failed"}
        if quiet:
            message["quiet"] = True
    else:
        await update_job_status(job_id, status, 100 if status == "success" else None, step, output_data=output)
        message = {"type": "job_complete", "job_id": job_id, "job_type": job_type,
                   "result": {**(result if result is not None else (output or {})), "status": status}}
    try:
        await ws_manager.send(message, user_id=user_id)
    except Exception:  # noqa: BLE001 — the job row is already final; the page reconciles on reconnect
        logger.debug("studio job end WS emit failed", exc_info=True)


# Takes record VOICE_CONCURRENCY at a time (voice_take_service). Scenes start
# in order, so scene 1 lands first and the editor's Play can unlock on it while
# the rest still record.


async def run_studio_voice(job_id: str, project_id: str, uids: list[str], user_id: str = "local") -> None:
    import asyncio

    from backend.agents.job_helper import job_cancelled, update_job_status
    from backend.core.ws_manager import ws_manager
    from backend.services import studio_doc as sd
    from backend.services import studio_store as store
    from backend.services import voice_take_service as vts

    logger.info("TASK START studio_voice | job=%s project=%s scenes=%d", job_id[:8], project_id[:8], len(uids))
    landed = 0
    failed_n = 0        # a failed take LANDS too — counted apart, or "voiced" lies
    done_n = 0
    # The scenes this job marked `generating` and has not reached yet. Only
    # THESE are settled in `finally`: a scene this job already finished may
    # have been edited and re-marked by a NEWER voice job meanwhile, and
    # settling it here would silently cancel that job's take.
    pending = list(uids)
    cancelled = False
    # A scene whose landing raised (the project deleted, a write that lost
    # every retry). The job will fail — but only ONCE, after the takes already
    # in flight have finished and landed; takes that have not started don't
    # (they stay in `pending` and are settled in `finally`).
    broken = False

    async def record(uid: str, gate: asyncio.Semaphore) -> None:
        nonlocal broken
        try:
            await _record(uid, gate)
        except BaseException:
            broken = True
            raise

    async def _record(uid: str, gate: asyncio.Semaphore) -> None:
        nonlocal landed, failed_n, done_n, cancelled
        async with gate:
            if broken:
                return                            # left in `pending` → settled in finally
            if cancelled or await job_cancelled(job_id):
                cancelled = True
                return                            # left in `pending` → settled in finally
            row = await store.get_project(project_id, user_id)
            doc = json.loads(row.doc_json)
            scene = next((s for s in doc["scenes"] if s["uid"] == uid), None)
            if scene is None or scene["voice"]["state"] != "generating":
                pending.remove(uid)
                return                            # deleted or edited meanwhile — nothing to land
            line = scene["line"]
            expect = {"line": line, "voice": list(sd.voice_identity(doc["settings"]))}
            try:
                spec = vts.VoiceSpec.from_settings(doc["settings"])
                take = await vts.ensure_take(spec, line)
                voice = {"state": "ready", "take_key": take.key, "duration": take.duration,
                         "cached": take.cached, "error": None}
            except JobCancelledError:
                cancelled = True                  # left in `pending` → settled in finally
                return
            except VoiceGenerationError as e:
                # The EXPECTED failure — a network hiccup from the TTS host, a
                # missing key, an empty synthesis — and therefore the COMMON
                # one. Logged, so a scene sitting at `failed` leaves a trace.
                logger.warning("studio_voice: take failed for scene %s: %s", uid, e)
                voice = {"state": "failed", "error": str(e)[:300]}
            except Exception as e:  # noqa: BLE001 — any take failure lands as failed, never a stuck spinner
                logger.warning("studio_voice: take failed for scene %s: %s", uid, e, exc_info=True)
                voice = {"state": "failed", "error": "The voice couldn't be recorded — try again."}
            row, _ = await store.write_ops(
                project_id, [{"op": "voice_result", "uid": uid, "voice": voice, "expect": expect}],
                if_revision=None, internal=True, user_id=user_id)
            pending.remove(uid)                   # landed — a newer job may re-mark it from here on
            landed += 1
            if voice["state"] == "failed":
                failed_n += 1
            done_n += 1
            await ws_manager.send({"type": "studio_project_updated", "project_id": project_id,
                                   "revision": row.revision}, user_id=user_id)
            await update_job_status(job_id, "running", 2 + 96 * done_n / len(uids), f"Voiced {done_n} of {len(uids)}")

    try:
        await update_job_status(job_id, "running", 2, "Recording voices")
        gate = asyncio.Semaphore(vts.VOICE_CONCURRENCY)
        # return_exceptions: a plain gather raised on the FIRST failure while
        # the sibling takes kept recording behind a job already reported
        # failed, and `finally` settled scenes still being recorded (their
        # takes then landed on scenes no longer marked, or were lost). Every
        # take is awaited; then the first error is raised once.
        results = await asyncio.gather(*(record(uid, gate) for uid in uids), return_exceptions=True)
        error = next((r for r in results if isinstance(r, BaseException)), None)
        if error is not None:
            raise error
        if cancelled:
            logger.info("TASK CANCELLED studio_voice | job=%s after %d/%d", job_id[:8], done_n, len(uids))
            await _stamp_cancelled(job_id, step=f"Cancelled after {done_n} of {len(uids)}")
            return
        await _end_job(job_id, "studio_voice", "success", user_id, step="Voices ready", output={
            "project_id": project_id, "voiced": landed - failed_n, "failed": failed_n,
        })
        logger.info("TASK DONE studio_voice | job=%s voiced=%d failed=%d",
                    job_id[:8], landed - failed_n, failed_n)
    except StudioNotFoundError:
        logger.info("TASK CANCELLED studio_voice | job=%s — project deleted", job_id[:8])
        await _end_job(job_id, "studio_voice", "cancelled", user_id, step="Project deleted")
    except Exception as e:  # noqa: BLE001 — a runner reports, never raises
        logger.exception("TASK FAILED studio_voice | job=%s", job_id[:8])
        await _end_job(job_id, "studio_voice", "failed", user_id, error=str(e)[:300] or "Voicing failed", output={
            "project_id": project_id, "voiced": landed - failed_n, "failed": failed_n})
    finally:
        await _settle_generating(project_id, pending, user_id)


async def _start_first_draft_voices(project_id: str, doc: dict, user_id: str) -> str | None:
    """Mark every unvoiced scene generating and dispatch ONE voice job for them
    — the free local voice only: a first draft never spends the user's own API
    key on its own (the same rule as the editor's auto-revoice,
    studioModel.autoRevoices === "edge_tts")."""
    from backend.agents.job_helper import create_job
    from backend.core import task_runner
    from backend.core.ws_manager import ws_manager
    from backend.services import studio_store as store
    from backend.services.tts_service import is_paid_per_take

    provider = doc["settings"]["voice"]["provider"]
    if is_paid_per_take(provider):
        await ws_manager.send_constraint_warning(
            "studio_plan", "Your voice uses your own API key, so the first draft isn't voiced automatically — "
                           "use Voice all to record it.", user_id=user_id)
        return None
    uids = [s["uid"] for s in doc["scenes"] if s["line"] and s["voice"]["state"] in ("none", "stale", "failed")
            and s["voice"].get("provider") != "own_audio"]       # the user's own audio is never voiced
    if not uids:
        return None
    await store.write_ops(project_id, [
        {"op": "voice_result", "uid": uid, "voice": {"state": "generating"}} for uid in uids
    ], if_revision=None, internal=True, user_id=user_id)
    child = await create_job("studio_voice", user_id, {"project_id": project_id, "scenes": len(uids)}, title="Voicing scenes")
    task_runner.dispatch_light(run_studio_voice(child.id, project_id, uids, user_id))
    return child.id


async def plan_into_project(project_id: str, user_id: str = "local", *, first_draft: bool = False,
                            only_uids: set[str] | None = None) -> dict:
    """The one planning pass — used by the plan job and by the legacy
    /generate/stock job. Binds real stock, picks a concrete library music track,
    writes it all in one internal write and tells the editor. With
    `first_draft`, voicing starts concurrently (the free local voice only).
    Raises StudioNotFoundError if the project is gone."""
    from backend.core.ai_provider import get_ai_client
    from backend.core.ws_manager import ws_manager
    from backend.services import studio_media, studio_music, studio_planner
    from backend.services import studio_store as store
    from backend.services.render_post import load_user_settings

    row = await store.get_project(project_id, user_id)
    doc = json.loads(row.doc_json)
    # A first draft's voices don't depend on the visuals: start them NOW,
    # concurrently with planning, so scene 1 is playable in seconds rather
    # than after the whole stock search.
    voiced_job = await _start_first_draft_voices(project_id, doc, user_id) if first_draft else None
    try:
        ai = get_ai_client(await load_user_settings(user_id))
    except Exception:  # noqa: BLE001 — no AI key configured: plan with keywords
        ai = None
    pexels, pixabay = studio_media.stock_keys()
    ops, warnings = await studio_planner.plan_project(
        doc, project_id=project_id, ai_client=ai, pexels_key=pexels, pixabay_key=pixabay,
        only_uids=only_uids)
    for message in warnings:
        await ws_manager.send_constraint_warning("studio_plan", message, user_id=user_id)
    # A bed that's on needs a CONCRETE track, or the preview is silent and the
    # render drops the music. Set only if still unset when written.
    music_op = await studio_music.default_music_op(
        doc, moods=[o.get("mood") for o in ops if o.get("op") == "plan_result"] or None)
    if music_op:
        ops = [*ops, music_op]
    if ops:
        row, _ = await store.write_ops(project_id, ops, if_revision=None, internal=True, user_id=user_id)
        await ws_manager.send({"type": "studio_project_updated", "project_id": project_id,
                               "revision": row.revision}, user_id=user_id)
    return {"planned": sum(1 for o in ops if o["op"] == "plan_result"), "warnings": warnings,
            "voice_job_id": voiced_job}


async def run_studio_plan(job_id: str, project_id: str, user_id: str = "local", first_draft: bool = False,
                          scene_uids: list[str] | None = None) -> None:
    """Plan a project's visuals; with `first_draft`, voice every scene too —
    but only with the free local voice."""
    from backend.agents.job_helper import update_job_status

    logger.info("TASK START studio_plan | job=%s project=%s first_draft=%s", job_id[:8], project_id[:8], first_draft)
    try:
        await update_job_status(job_id, "running", 5, "Planning scenes")
        planned = await plan_into_project(project_id, user_id, first_draft=first_draft,
                                          only_uids=set(scene_uids) if scene_uids else None)
        await update_job_status(job_id, "running", 70, "Scenes planned")
        await _end_job(job_id, "studio_plan", "success", user_id, step="Plan ready",
                       output={"project_id": project_id, **planned})
        logger.info("TASK DONE studio_plan | job=%s planned=%d", job_id[:8], planned["planned"])
    except StudioNotFoundError:
        logger.info("TASK CANCELLED studio_plan | job=%s — project deleted", job_id[:8])
        await _end_job(job_id, "studio_plan", "cancelled", user_id, step="Project deleted")
    except Exception as e:  # noqa: BLE001 — a runner reports, never raises
        logger.exception("TASK FAILED studio_plan | job=%s", job_id[:8])
        await _end_job(job_id, "studio_plan", "failed", user_id, error=str(e)[:300] or "Planning failed")
    finally:
        if PLANS_IN_FLIGHT.get(project_id) == job_id:
            PLANS_IN_FLIGHT.pop(project_id, None)


async def run_studio_from_legacy(job_id: str, project_id: str, request: dict, user_id: str = "local") -> None:
    """The `POST /api/generate/stock` job: the endpoint already created and
    claimed the project; here it is planned and given the caller's stills,
    then rendered by render v2 — one job, type `generate`. Planning runs here,
    never inside the HTTP request."""
    from backend.agents.job_helper import update_job_status
    from backend.services import studio_autofill
    from backend.services import studio_store as store

    logger.info("TASK START studio_legacy | job=%s project=%s", job_id[:8], project_id[:8])
    try:
        await update_job_status(job_id, "running", 1, "Planning scenes")
        await studio_autofill.prepare_legacy(project_id, request, user_id=user_id)
    except Exception as e:  # noqa: BLE001 — a runner reports, never raises
        logger.exception("TASK FAILED studio_legacy (planning) | job=%s", job_id[:8])
        message = ("That project was deleted." if isinstance(e, StudioNotFoundError) else
                   str(e)[:300] if isinstance(e, StudioError) and str(e) else
                   "Planning the video failed — try again.")
        await _end_job(job_id, "generate", "failed", user_id, error=message)
        await store.abandon_render(project_id, user_id=user_id)   # release the claim — nothing will render
        return
    await run_studio_render(job_id, project_id, user_id)


async def run_studio_render(job_id: str, project_id: str, user_id: str = "local") -> None:
    """Render v2. The endpoint already claimed the project; this always
    releases it — rendered on success, back to draft on failure or cancel —
    and finishes the job through _end_job like every other studio runner."""
    from backend.services import studio_render
    from backend.services import studio_store as store

    logger.info("TASK START studio_render | job=%s project=%s", job_id[:8], project_id[:8])
    try:
        done = await studio_render.render(job_id, project_id, user_id)
        from backend.agents.job_helper import job_cancelled, update_job_status
        if await job_cancelled(job_id):
            # The cancel landed after the last gate: the video is already
            # written. The job stays `cancelled` (the user asked) but keeps the
            # link to the video, and nothing says "Video ready!" — a poller
            # that saw a cancelled job with no output rendered it again.
            logger.info("TASK CANCELLED studio_render | job=%s — the video had already finished; keeping it",
                        job_id[:8])
            await update_job_status(job_id, "cancelled", current_step="Cancelled — the video had already finished",
                                    output_data=done.output)
            return
        await _end_job(job_id, "generate", "success", user_id, step="Video ready!", output=done.output,
                       result=done.result)
        logger.info("TASK DONE studio_render | job=%s", job_id[:8])
        done.after_complete()
    except studio_render.RenderCancelled:
        # The cancel request already flipped the row to `cancelled` and told the
        # page; no event here (a second one would print "Job complete!" for a
        # cancel). The row still needs its END: DELETE /api/jobs only sets the
        # status, so without this stamp a cancelled render had no
        # `completed_at` — the Activity panel showed no end time and a headless
        # poller waiting on it hung.
        logger.info("TASK CANCELLED studio_render | job=%s", job_id[:8])
        await _stamp_cancelled(job_id)
    except Exception as e:  # noqa: BLE001 — a runner reports, never raises
        logger.exception("TASK FAILED studio_render | job=%s", job_id[:8])
        # A StudioError carries a sentence written for the user; anything else
        # (a crash, an OS error) may carry paths or tool output — never shown.
        message = (str(e)[:300] if isinstance(e, StudioError) and str(e) else
                   "The render failed — try rendering again.")
        await _end_job(job_id, "generate", "failed", user_id, error=message)
    finally:
        await store.abandon_render(project_id, user_id=user_id)   # no-op once the render marked it rendered


# Every runner here that a route took something for owes it back when it is
# dropped queued: a coroutine that never started runs no `finally` (the
# render's claim, a plan's seat, a scene left `generating`), and each of those
# holds the project until a restart.

async def _stamp_if_cancelled(job_id: str) -> None:
    from backend.agents.job_helper import job_cancelled
    if await job_cancelled(job_id):
        await _stamp_cancelled(job_id)


async def _release_unstarted_render(job_id: str, project_id: str, user_id: str = "local", **_kw) -> None:
    """The render never started (cancelled while queued, or dropped waiting
    for a slot): the endpoint's claim would stay on the project until a restart."""
    from backend.services import studio_store as store
    await store.abandon_render(project_id, user_id=user_id)
    await _stamp_if_cancelled(job_id)


on_unstarted("run_studio_render")(_release_unstarted_render)
on_unstarted("run_studio_from_legacy")(_release_unstarted_render)


async def _release_unstarted_plan(job_id: str, project_id: str, **_kw) -> None:
    if PLANS_IN_FLIGHT.get(project_id) == job_id:
        PLANS_IN_FLIGHT.pop(project_id, None)
    await _stamp_if_cancelled(job_id)


on_unstarted("run_studio_plan")(_release_unstarted_plan)


async def _release_unstarted_voice(job_id: str, project_id: str, uids: list[str], user_id: str = "local", **_kw) -> None:
    await _settle_generating(project_id, uids, user_id)
    await _stamp_if_cancelled(job_id)


on_unstarted("run_studio_voice")(_release_unstarted_voice)


# ── My audio: listening to the user's recording ─────────────────────────────

AUDIO_PLAN_FAILED = "Your scenes are ready, but finding footage for them didn't finish — use Re-plan visuals."
AUDIO_WATCH_S = 1.0          # how often a listening job looks for a cancel and reports its progress


async def _audio_progress(job_id: str, user_id: str, pct: float, step: str) -> None:
    from backend.agents.job_helper import update_job_status
    from backend.core.ws_manager import ws_manager
    await update_job_status(job_id, "running", progress_pct=pct, current_step=step)
    try:
        await ws_manager.send_progress(job_id, pct, step, user_id)
    except Exception:  # noqa: BLE001 — the row has it; the editor reads the row
        logger.debug("studio_audio progress emit failed", exc_info=True)


async def _tell_editor(project_id: str, user_id: str, revision: int | None = None) -> None:
    """`studio_project_updated` with no revision = "read it again": the listening ended without a
    write (failed, cancelled), and the open editor must stop saying "Listening…"."""
    from backend.core.ws_manager import ws_manager
    try:
        await ws_manager.send({"type": "studio_project_updated", "project_id": project_id,
                               **({"revision": revision} if revision is not None else {})}, user_id=user_id)
    except Exception:  # noqa: BLE001 — the editor's poll is the net under this
        logger.debug("studio_project_updated emit failed", exc_info=True)


async def _no_download_toast(_model: str, _size: str) -> None:
    """The job's own step names the first-run model download."""


async def _listen_and_land(job_id: str, project_id: str, user_id: str):
    """Listen to the project's own copy of the recording, cut it at its sentences, store each cut as
    the scene's take and land the scenes in ONE write. `(lines, landed row | None)`, or None when
    the job was cancelled meanwhile (already stamped). Raises what the listening raises —
    AudioRefused, WhisperModelUnavailableError, StudioNotFoundError — for the runner to word."""
    import asyncio

    from backend.agents.job_helper import job_cancelled
    from backend.services import studio_audio
    from backend.services import studio_store as store
    from backend.services.own_audio import AudioRefused
    from backend.services.whisper_service import whisper_service

    loop = asyncio.get_running_loop()
    latest: dict = {"pct": 2.0, "step": "Opening your audio"}
    downloading = {"on": False}

    async def on_step(text: str) -> None:
        latest.update({"pct": {"Opening your audio": 2.0, "Listening": 10.0}.get(text, 82.0), "step": text})

    def on_fraction(fraction: float) -> None:                    # the Whisper thread
        loop.call_soon_threadsafe(latest.update, {"pct": round(10 + 70 * max(0.0, min(1.0, fraction)), 1),
                                                  "step": "Listening"})

    async def work():
        row = await store.get_project(project_id, user_id)
        doc = json.loads(row.doc_json)
        src = studio_audio.source_file(project_id)
        if src is None:
            raise AudioRefused("source_gone", studio_audio.SOURCE_GONE)
        # Before the listening's own limit: that is for decoding, and a first-run model fetch on a
        # slow line would run it out and read as "took too long".
        downloading["on"] = not whisper_service.is_model_cached(studio_audio.QUALITY)
        await whisper_service.ensure_model(studio_audio.QUALITY, on_download=_no_download_toast, user_id=user_id)
        downloading["on"] = False
        return await studio_audio.listen(src, doc["input"]["audio"]["audio_id"], on_step=on_step,
                                         on_fraction=on_fraction, should_stop=lambda: job_cancelled(job_id),
                                         user_id=user_id)

    await _audio_progress(job_id, user_id, latest["pct"], latest["step"])
    said = dict(latest)
    task = asyncio.create_task(work())
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=AUDIO_WATCH_S)
            if task.done():
                break
            if await job_cancelled(job_id):
                # The job stops now; a Whisper decode nobody waits for stops at its next segment.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await _stamp_cancelled(job_id)
                logger.info("TASK CANCELLED studio_audio | job=%s", job_id[:8])
                return None
            now = ({"pct": 2.0, "step": "Getting the speech model ready (first run only)"}
                   if downloading["on"] else dict(latest))
            if now != said:
                await _audio_progress(job_id, user_id, now["pct"], now["step"])
                said = now
    except asyncio.CancelledError:
        task.cancel()
        raise
    lines, language = task.result()
    if await job_cancelled(job_id):                           # finished in the same breath as the cancel
        await _stamp_cancelled(job_id)
        return None
    await _audio_progress(job_id, user_id, 90, "Saving your scenes")
    row = await studio_audio.land(project_id, lines, language, user_id)
    if row is not None:
        await _tell_editor(project_id, user_id, row.revision)
    return lines, row


async def run_studio_audio(job_id: str, project_id: str, user_id: str = "local") -> None:
    """My audio: listen to the project's own copy of the recording, cut it at its sentences, store each
    cut as the scene's take, land the scenes in ONE write, then plan their visuals like any new
    project. The project is never left "listening": that state IS this job's row
    (studio_audio.envelope_fields), so every end below — and a crash — releases it."""
    from backend.agents.job_helper import job_cancelled, update_job_status
    from backend.core.exceptions import WhisperModelUnavailableError
    from backend.services import studio_audio
    from backend.services.own_audio import AudioRefused

    logger.info("TASK START studio_audio | job=%s project=%s", job_id[:8], project_id[:8])

    async def fail(message: str, code: str | None = None) -> None:
        output = {"project_id": project_id, **({"refused": code} if code else {})}
        # A cancel and a failure can both be true: the backstop in update_job_status keeps
        # `cancelled` and records the reason; no job_failed for something the user already stopped.
        if await job_cancelled(job_id):
            await update_job_status(job_id, "failed", error_message=message, output_data=output)
            return
        await _end_job(job_id, "studio_audio", "failed", user_id, error=message, output=output)

    try:
        heard = await _listen_and_land(job_id, project_id, user_id)
        if heard is None:
            return
        lines, row = heard
        # The scenes exist from here on: whatever the footage search does, the listening succeeded.
        planned, note = None, None
        try:
            await _audio_progress(job_id, user_id, 92, "Finding footage")
            planned = await plan_into_project(project_id, user_id)
        except StudioNotFoundError:
            raise
        except Exception:  # noqa: BLE001 — a failed plan leaves placeholders; Re-plan is one click
            logger.warning("studio_audio: planning after the landing failed | job=%s", job_id[:8], exc_info=True)
            note = AUDIO_PLAN_FAILED
            from backend.core.ws_manager import ws_manager
            await ws_manager.send_constraint_warning("studio_plan", note, user_id=user_id)
        await _end_job(job_id, "studio_audio", "success", user_id, step="Scenes ready", output={
            "project_id": project_id, "scenes": len(lines), "landed": row is not None,
            "planned": (planned or {}).get("planned", 0), **({"warning": note} if note else {})})
        logger.info("TASK DONE studio_audio | job=%s scenes=%d", job_id[:8], len(lines))
    except StudioNotFoundError:
        logger.info("TASK CANCELLED studio_audio | job=%s — project deleted", job_id[:8])
        await _end_job(job_id, "studio_audio", "cancelled", user_id, step="Project deleted")
    except JobCancelledError:
        logger.info("TASK CANCELLED studio_audio | job=%s", job_id[:8])
        await _stamp_cancelled(job_id)
    except AudioRefused as e:
        logger.info("TASK REFUSED studio_audio | job=%s code=%s", job_id[:8], e.code)
        await fail(str(e), e.code)
    except WhisperModelUnavailableError as e:
        await fail(str(e))
    except Exception as e:  # noqa: BLE001 — a runner reports, never raises; never a path or a trace on screen
        logger.exception("TASK FAILED studio_audio | job=%s", job_id[:8])
        await fail(studio_audio.TOOK_TOO_LONG if str(e) == studio_audio.TOOK_TOO_LONG else studio_audio.LISTEN_FAILED)
    finally:
        await _tell_editor(project_id, user_id)


async def _release_unstarted_audio(job_id: str, project_id: str, user_id: str = "local", **_kw) -> None:
    """Dropped while it queued: nothing was claimed (the job row is the listening state and it is
    already terminal), so the end is stamped and the open editor is told to read the project again."""
    await _stamp_if_cancelled(job_id)
    await _tell_editor(project_id, user_id)


on_unstarted("run_studio_audio")(_release_unstarted_audio)
