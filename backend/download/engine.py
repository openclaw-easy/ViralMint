# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The one attempt loop: the probe and the video download.

Every failed attempt goes through the same three lines:

    error_class = classify(error, url)        # classify.py: the only reader of error text
    step = decide(error_class, state)         # policy.py: the table and the budget
    ... carry the step out ...                # here

There is no other retry logic in the download path. A new site quirk is a
row in the classifier's corpus and, if it needs a new response, a row in the
table; this file does not change.

Threads. yt-dlp is synchronous and Python cannot stop a thread, so:

  * All of it runs on `download_pool` (never the default executor).
  * `await_with_stall_guard` gives up on a download that stops MOVING or
    passes its wall clock. Giving up abandons the thread, it does not stop
    it: the thread still owns the output stem and will sweep it on its way
    out. So a stall or timeout is terminal (`DownloadStalledError`,
    `DownloadTimeoutError`): nothing may retry into that stem.
  * A cancel is checked when a job is admitted, before every step of the
    ladder, during every wait, and every two seconds WHILE BYTES FLOW (the
    progress hook raises yt-dlp's DownloadCancelled, which stops the
    transfer). Between attempts it ends the ladder with the error in hand,
    so the job can say "cancelled" AND why; mid-transfer it ends
    as JobCancelledError. The same hook stops a transfer the caller has
    abandoned, once it moves again.

What `run` raises is always one of ours (backend.core.exceptions), carrying
`error_class` and `detail` (yt-dlp's raw text, for the log):

    ExtractorMissingError   yt-dlp has no way into this page (no extractor, a bot check,
                            a 403 / 404 it couldn't get past)
    VideoUnavailableError   private, removed, live, too long, …
    BotDetectionError       a sign-in wall
    RateLimitError          429; the host cooldown is armed
    DownloadStalledError / DownloadTimeoutError   abandoned
    DownloadError           everything else, in words
"""
from __future__ import annotations

import asyncio
import glob
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.config import settings
from backend.core.concurrency import _probe_semaphore
from backend.core.exceptions import (
    BotDetectionError,
    DownloadError,
    DownloadStalledError,
    DownloadTimeoutError,
    ExtractorMissingError,
    JobCancelledError,
    RateLimitError,
    VideoUnavailableError,
)
from backend.core.executors import download_pool
from backend.download import cookies, health, messages, opts, outputs, progress
from backend.download.classify import ErrorClass, classify
from backend.download.policy import (
    DROP_COOKIES, DROP_EXTRAS, DROP_SUBTITLES, MANUAL_MERGE, REFRESH_COOKIES,
    Budget, LadderState, Outcome, ProbeVerdict, Step, StepKind, decide, probe_verdict,
)
from backend.download.util import host_of

logger = logging.getLogger(__name__)

VIDEO_MAX_DURATION_S = 7200       # 2 h: longer video sources are archived streams
PROBE_TIMEOUT_S = 120             # YouTube's player-client cascade can take 60-90 s; do not raise this again
GENERIC_PROBE_TIMEOUT_S = 30
SUBTITLE_LANGS = ["en", "zh", "ja", "ko", "es", "fr", "de", "pt", "ru", "ar"]


@dataclass(frozen=True)
class Request:
    """One download. `stem` is a concrete file stem (never a yt-dlp template):
    every file the attempt writes starts with it, which is what lets the
    stall watchdog measure it and the cleanup find it."""
    url: str
    output_dir: Path
    stem: str
    options: dict = field(default_factory=dict)   # normalized download_options
    extract_audio: bool = True         # also write <stem>_audio.mp3 for transcription
    job_id: str | None = None
    user_id: str = "local"


@dataclass(frozen=True)
class ProbeResult:
    force_generic: bool = False        # only the generic extractor saw the video
    info: dict | None = None           # the metadata, when the probe got any


def _tag(exc: Exception, error_class: ErrorClass, raw: object) -> Exception:
    exc.error_class = error_class
    exc.detail = str(raw)[:500]
    return exc


def _ytdlp_version() -> str:
    try:
        import importlib.metadata
        return importlib.metadata.version("yt-dlp")
    except Exception:  # noqa: BLE001
        return "unknown"


def _exception_for(step: Step, error_class: ErrorClass, raw: object, url: str) -> Exception:
    """The exception a finished ladder raises, with its sentence for the person."""
    store = cookies.store()
    hint = store.hint()
    try:
        browser = cookies.browser_label(store.status().browser)
    except Exception:  # noqa: BLE001 — naming the browser is advice; a read problem must not change the error
        browser = None
    sentence = messages.for_class(error_class, raw, url, cookie_hint=hint, browser=browser)
    if step.kind is StepKind.ESCALATE:
        return _tag(ExtractorMissingError(sentence), error_class, raw)
    exc_type = {
        Outcome.UNAVAILABLE: VideoUnavailableError,
        Outcome.SIGN_IN: BotDetectionError,
        Outcome.RATE_LIMITED: RateLimitError,
    }.get(step.outcome, DownloadError)
    return _tag(exc_type(sentence), error_class, raw)


def terminal_exception(error: BaseException, url: str) -> Exception:
    """Our exception for a yt-dlp error that ended something outside the
    ladder (a channel listing, a subtitle fetch): the class's final answer,
    in words. Never ExtractorMissingError: that names a page the download
    path couldn't get into, which is not what a listing or a subtitle fetch is."""
    error_class = classify(error, url)
    spent = LadderState(budget=Budget(max_attempts=1, max_wait_s=0.0))
    step = decide(error_class, spent)
    if step.kind is StepKind.ESCALATE:
        return _tag(DownloadError(messages.for_class(error_class, error, url)), error_class, error)
    return _exception_for(step, error_class, error, url)


# ── The probe ────────────────────────────────────────────────────────────────

def _probe_once(url: str, *, with_cookies: bool, generic: bool) -> dict | None:
    import yt_dlp
    jar = cookies.store().jar_for_attempt() if with_cookies else None
    o = opts.build(url, jar=jar, socket_timeout=15, extractor_retries=1, retries=1, **opts.ONE_ITEM)
    if generic:
        # yt-dlp's generic extractor finds plain HLS/DASH and embedded players.
        o["force_generic_extractor"] = True
    with yt_dlp.YoutubeDL(o) as ydl:
        return opts.single_video_info(ydl.extract_info(url, download=False))


async def probe(url: str, *, max_duration_s: float) -> ProbeResult:
    """Metadata only, before the download itself.

    Ends the download here only on a CERTAIN answer: a live or upcoming
    stream (yt-dlp would record it until the wall clock), a source longer
    than `max_duration_s`, a video that is gone, or a page only a browser
    can read. Anything else is inconclusive and the download gets its try.
    Never waits between tries (`Budget.probe`).
    """
    with_cookies, generic, info = True, False, None
    async with _probe_semaphore:
        for _ in range(Budget.probe().max_attempts):
            timeout = GENERIC_PROBE_TIMEOUT_S if generic else PROBE_TIMEOUT_S
            try:
                info = await asyncio.wait_for(
                    download_pool.run(_probe_once, url, with_cookies=with_cookies, generic=generic),
                    timeout=timeout,
                )
                break
            except asyncio.TimeoutError:
                if generic:
                    raise _tag(ExtractorMissingError(messages.for_class(ErrorClass.UNSUPPORTED_URL, "", url)),
                               ErrorClass.UNSUPPORTED_URL, "generic probe timed out") from None
                raise _tag(DownloadError(
                    f"Couldn't read this link: {messages.site_name(url)} didn't answer in {timeout} seconds."),
                    ErrorClass.TRANSIENT_NETWORK, "probe timed out") from None
            except VideoUnavailableError:
                raise                                    # a playlist with nothing playable
            except Exception as e:  # noqa: BLE001 — every probe error is read by the classifier
                error_class = classify(e, url)
                verdict = probe_verdict(error_class)
                logger.info("[DOWNLOAD_STEP] probe host=%s class=%s verdict=%s",
                            host_of(url), error_class.value, verdict.value)
                if generic:
                    # The generic extractor was the last thing to try.
                    raise _tag(ExtractorMissingError(messages.for_class(ErrorClass.UNSUPPORTED_URL, e, url)),
                               ErrorClass.UNSUPPORTED_URL, e) from e
                if verdict is ProbeVerdict.RETRY_WITHOUT_COOKIES and with_cookies:
                    cookies.store().mark_unreadable(e)
                    with_cookies = False
                    continue
                if verdict is ProbeVerdict.TRY_GENERIC:
                    generic = True
                    continue
                if verdict is ProbeVerdict.ESCALATE:
                    raise _tag(ExtractorMissingError(messages.for_class(error_class, e, url)), error_class, e) from e
                if verdict is ProbeVerdict.UNAVAILABLE:
                    raise _tag(VideoUnavailableError(messages.for_class(error_class, e, url)), error_class, e) from e
                logger.warning("Probe of %s said nothing certain (%s): %s",
                               url[:80], error_class.value, str(e)[:200])
                break

    if info:
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise _tag(VideoUnavailableError("Live streams and upcoming streams cannot be downloaded."),
                       ErrorClass.UNAVAILABLE, "live")
        duration = info.get("duration") or 0
        if duration and duration > max_duration_s:
            raise _tag(VideoUnavailableError(
                f"This source is {duration / 3600:.1f} hours long — the limit is "
                f"{max_duration_s / 3600:g} hours. Very long recordings (archived "
                f"livestreams, multi-hour streams) are not supported."),
                ErrorClass.UNAVAILABLE, "too long")
    return ProbeResult(force_generic=generic and info is not None, info=info)


# ── The download ─────────────────────────────────────────────────────────────

class _Attempts:
    """The ladder for one request. Lives on a `download_pool` thread."""

    def __init__(self, req: Request, *, force_generic: bool, loop, hooks: tuple, abandoned: threading.Event):
        from backend.services import download_options
        self.req, self.loop = req, loop
        self.url, self.dir, self.stem = req.url, req.output_dir, req.stem
        self.store = cookies.store()
        self.started = time.monotonic()
        self.extras_dropped = False
        # Set when the caller stopped waiting (stall, wall clock). The thread
        # can't be stopped mid-call, but it starts nothing new and keeps no file.
        self.abandoned = abandoned

        jar = self.store.jar_for_attempt()
        progress_hook, pp_hook = hooks
        common = dict(
            outtmpl=str(self.dir / self.stem) + ".%(ext)s",
            progress_hooks=[progress_hook],
            # Suspends the stall watchdog while ffmpeg merges or transcodes.
            postprocessor_hooks=[pp_hook],
            # The table owns retrying. yt-dlp's own budget stays small, except
            # for fragments: one bad segment must not fail a long HLS stream.
            retries=3, extractor_retries=2, file_access_retries=3, fragment_retries=10,
            **opts.ONE_ITEM,
        )
        self.extras: list = []
        overrides = download_options.ydl_overrides(req.options)
        self.extras = overrides.pop("_vm_postprocessors", [])
        self.chain = download_options.format_chain(req.options) or list(opts.FORMAT_CHAIN)
        self.base = opts.build(
            self.url, jar=jar, socket_timeout=60, extract_flat=False,
            # Spaces yt-dlp's own requests out; sites rate-limit bursts.
            sleep_interval=3, max_sleep_interval=8, sleep_interval_requests=1,
            writesubtitles=True, writeautomaticsub=True,
            subtitleslangs=SUBTITLE_LANGS, subtitlesformat="srt/vtt/best",
            **common,
        )
        # The person's options last, so an explicit choice wins; extras are
        # appended, never assigned over the postprocessors already there.
        self.base.update(overrides)
        if self.extras:
            self.base["postprocessors"] = [*(self.base.get("postprocessors") or []), *self.extras]
        budget = Budget.video()
        if force_generic:
            self.base["force_generic_extractor"] = True

        self.state = LadderState(
            budget=budget,
            formats_left=len(self.chain) - 1,
            has_cookies=jar is not None,
            can_refresh_cookies=self.store.refresh_allowed(),
            subtitles_on=bool(self.base.get("writesubtitles")),
            extras_attached=bool(self.extras),
        )

    # ── one attempt ──────────────────────────────────────────────────────────

    def _attempt(self, fmt: str) -> tuple[dict, Path]:
        import yt_dlp
        o = {**self.base, "format": fmt}
        if self.state.budget.attempts > 1:
            # A retry checks the chosen format's URL first: catches stale
            # signed URLs that "succeed" and then trickle.
            o["check_formats"] = "selected"
        with yt_dlp.YoutubeDL(o) as ydl:
            info = opts.single_video_info(ydl.extract_info(self.url, download=True))
        if not info:
            raise DownloadError("yt-dlp returned no information for this link.")
        return info, self._output(info)

    def _output(self, info: dict) -> Path:
        """The file this attempt produced, checked. Raises when there is none."""
        path = outputs.find_video_file(self.dir, self.stem)
        if not path:
            raise DownloadError("Download finished but no video file is on disk.")
        outputs.validate_output(path, source_label="yt-dlp")      # OutputInvalidError when it isn't a video
        return path

    def _metadata_only(self) -> dict:
        """After a manual merge: the info dict, without downloading again."""
        import yt_dlp
        try:
            o = opts.build(self.url, jar=self.store.jar_for_attempt(), **opts.ONE_ITEM)
            with yt_dlp.YoutubeDL(o) as ydl:
                return opts.single_video_info(ydl.extract_info(self.url, download=False)) or {"id": self.stem}
        except Exception:  # noqa: BLE001 — the file is what matters; metadata is a bonus
            return {"id": self.stem}

    def _manual_merge(self) -> tuple[dict, Path] | None:
        """yt-dlp's merge failed with the streams on disk: merge them with ffmpeg."""
        merged = outputs.try_manual_merge(self.dir, self.stem)
        if not merged:
            return None
        try:
            outputs.validate_output(merged, source_label="manual-merge")
        except outputs.OutputInvalidError as bad:
            logger.warning("Manual merge produced an unusable file: %s", bad)
            return None
        return self._metadata_only(), merged

    # ── the ladder ───────────────────────────────────────────────────────────

    def run(self) -> dict:
        import yt_dlp
        fmt_idx = 0
        failed: tuple[ErrorClass, Exception] | None = None      # the last failed attempt
        try:
            while True:
                self._stop_if_abandoned()
                # A cancel that landed during the wait (or while a recovery ran)
                # ends the ladder here: no further transfer starts.
                if failed and progress.job_cancelled_from_thread(self.req.job_id, self.loop):
                    self._raise_cancelled(*failed)
                try:
                    info, path = self._attempt(self.chain[fmt_idx])
                    self._stop_if_abandoned(path)
                    return self._succeed(info, path)
                except VideoUnavailableError:
                    raise                                 # a playlist with nothing playable: certain
                except yt_dlp.utils.DownloadCancelled as stopped:
                    # The progress hook stopped the transfer: the caller gave up
                    # on it (stall, wall clock) or the person cancelled the job.
                    self._stop_if_abandoned()
                    if progress.job_cancelled_from_thread(self.req.job_id, self.loop):
                        logger.info("Download of %s stopped mid-transfer — the job was cancelled", self.url[:80])
                        raise JobCancelledError("Cancelled during the transfer") from None
                    # yt-dlp also raises this family for its own stop rules; none
                    # is configured here, so without a cancel it is a plain failure.
                    raise _tag(DownloadError(messages.for_class(ErrorClass.UNKNOWN, stopped, self.url)),
                               ErrorClass.UNKNOWN, stopped) from stopped
                except (yt_dlp.utils.DownloadError, DownloadError) as e:
                    error, from_ytdlp = e, isinstance(e, yt_dlp.utils.DownloadError)
                except Exception as e:  # noqa: BLE001 — a bug in yt-dlp or in us: one honest failure
                    logger.exception("Download of %s failed outside yt-dlp's own errors", self.url[:80])
                    raise _tag(DownloadError(messages.for_class(ErrorClass.UNKNOWN, e, self.url)),
                               ErrorClass.UNKNOWN, e) from e

                self._stop_if_abandoned()                 # nobody is waiting: no step, no wait
                error_class = classify(error, self.url)
                failed = (error_class, error)
                if progress.job_cancelled_from_thread(self.req.job_id, self.loop):
                    self._raise_cancelled(error_class, error)

                step = decide(error_class, self.state)
                logger.warning(
                    "[DOWNLOAD_STEP] host=%s class=%s step=%s do=%s wait=%.0fs attempt=%d/%d: %s",
                    host_of(self.url), error_class.value, step.kind.value, ",".join(step.do) or "-",
                    step.wait_s, self.state.budget.attempts, self.state.budget.max_attempts,
                    str(error)[:200],
                )
                if error_class is ErrorClass.COOKIE_SOURCE:
                    self.store.mark_unreadable(error)

                merged = self._manual_merge() if MANUAL_MERGE in step.do else None
                if merged:
                    return self._succeed(*merged)
                self._apply(step)
                self.state.record(error_class, step)
                if REFRESH_COOKIES in step.do:
                    self.state.has_cookies = "cookiefile" in self.base

                if step.kind in (StepKind.ESCALATE, StepKind.STOP):
                    self._fail(step, error_class, error, from_ytdlp)
                outputs.cleanup_partial_files(self.dir, self.stem)
                if step.kind is StepKind.NEXT_FORMAT:
                    fmt_idx += 1
                elif step.wait_s:
                    progress.cancellable_sleep(step.wait_s, self.req.job_id, self.loop)
        finally:
            # Every exit, including one the caller stopped waiting for: an
            # abandoned thread still ends here and sweeps its own partial files.
            try:
                outputs.cleanup_partial_files(self.dir, self.stem)
            except Exception as e:  # noqa: BLE001 — cleanup must never mask the real error
                logger.warning("Partial-file cleanup failed (non-fatal): %s", e)

    def _raise_cancelled(self, error_class: ErrorClass, error: Exception) -> None:
        """A cancel is not a download failure: it ends the ladder with the
        error in hand (the job keeps it as the reason) and feeds no streak."""
        logger.info("Download of %s stops here — the job was cancelled", self.url[:80])
        sentence = messages.for_class(error_class, error, self.url)
        raise _tag(DownloadError(f"Download cancelled: {sentence}"), error_class, error) from error

    def _stop_if_abandoned(self, produced: Path | None = None) -> None:
        """Nobody is waiting for this download any more: the job has already
        failed as stalled or timed out. Keep no file that no Library row will
        own, and start no further attempt."""
        if not self.abandoned.is_set():
            return
        if produced is not None:
            for leftover in (produced, *self.dir.glob(f"{glob.escape(self.stem)}.*")):
                try:
                    leftover.unlink()
                except OSError:
                    pass
        logger.info("Abandoned download of %s ends here", self.url[:80])
        raise _tag(DownloadError("Download abandoned after it stalled."), ErrorClass.UNKNOWN, "abandoned")

    def _apply(self, step: Step) -> None:
        """Change the options for the next attempt, as the step says."""
        if DROP_COOKIES in step.do:
            opts.drop_cookies(self.base)
        if REFRESH_COOKIES in step.do:
            opts.drop_cookies(self.base)
            jar = self.store.refresh()
            if jar is not None:
                self.base["cookiefile"] = str(jar)
        if DROP_SUBTITLES in step.do:
            self.base["writesubtitles"] = False
            self.base["writeautomaticsub"] = False
        if DROP_EXTRAS in step.do:
            # The video is already on disk; only the embed extras failed. yt-dlp
            # sees the finished file on the retry and skips to postprocessing.
            self.base["postprocessors"] = [
                pp for pp in (self.base.get("postprocessors") or []) if pp not in self.extras]
            self.extras_dropped = True

    def _fail(self, step: Step, error_class: ErrorClass, error: Exception, from_ytdlp: bool) -> None:
        outputs.cleanup_partial_files(self.dir, self.stem)
        if step.outcome is Outcome.RATE_LIMITED:
            health.record_rate_limit(self.url)
        counted = health.record_failure(error_class, self.url) if from_ytdlp else False
        b = self.state.budget
        logger.error(
            "[DOWNLOAD_FAILED] host=%s class=%s end=%s attempts=%d waited_s=%.0f elapsed_s=%.0f "
            "yt_dlp=%s streak=%s detail=%s",
            host_of(self.url), error_class.value, (step.outcome or step.kind).value, b.attempts,
            b.waited_s, time.monotonic() - self.started, _ytdlp_version(), counted, str(error)[:200],
        )
        raise _exception_for(step, error_class, error, self.url) from error

    # ── success ──────────────────────────────────────────────────────────────

    def _succeed(self, info: dict, path: Path) -> dict:
        health.record_success(self.url)
        b = self.state.budget
        logger.info("[DOWNLOAD_OK] host=%s attempts=%d waited_s=%.0f elapsed_s=%.0f",
                    host_of(self.url), b.attempts, b.waited_s,
                    time.monotonic() - self.started)
        size_mb = round(path.stat().st_size / (1024 * 1024), 2)
        # Subtitles are parsed for the transcript, then removed, unless the
        # person asked for the subtitle FILE: then the sidecar is the deliverable.
        subtitles = outputs.collect_subtitles(self.dir, self.stem, info=info)
        kept_subtitle_paths: list[str] = []
        if self.req.options.get("subtitles") == "file":
            for ext in ("srt", "vtt"):
                kept_subtitle_paths.extend(str(p) for p in self.dir.glob(f"{glob.escape(self.stem)}.*.{ext}"))
        else:
            outputs.cleanup_subtitle_files(self.dir, self.stem)

        audio_path = None
        if self.req.extract_audio and not progress.job_cancelled_from_thread(self.req.job_id, self.loop):
            audio_dir = settings.AUDIO_DIR.resolve()
            audio_dir.mkdir(parents=True, exist_ok=True)
            audio_path = outputs.extract_audio_locally(path, audio_dir, self.stem)
            if not audio_path:
                logger.warning("Couldn't extract audio from %s (continuing without)", path.name)

        categories = info.get("categories")
        return {
            "video_path": str(path),
            "audio_path": str(audio_path) if audio_path and Path(audio_path).exists() else None,
            "duration": info.get("duration", 0),
            "file_size_mb": size_mb,
            "title": info.get("title"),
            "views": info.get("view_count", 0),
            "likes": info.get("like_count", 0),
            "comments": info.get("comment_count", 0),
            "upload_date": info.get("upload_date"),
            "uploader": info.get("uploader"),
            "uploader_url": info.get("uploader_url"),
            "thumbnail": info.get("thumbnail"),
            "description": info.get("description"),
            "subtitles": subtitles,
            "chapters": outputs.extract_chapters(info),
            "tags": info.get("tags") or [],
            "category": categories[0] if categories else info.get("category"),
            # What was delivered against what was asked: a quality cap degrades
            # instead of failing, so the two can differ and the UI says so.
            "height": info.get("height"),
            "requested_quality": self.req.options.get("quality"),
            "kept_subtitle_paths": kept_subtitle_paths,
            "option_postprocessors_dropped": self.extras_dropped,
        }


async def run(req: Request, *, force_generic: bool = False) -> dict:
    """Download `req` with yt-dlp: runs the ladder on the download pool and
    watches it for progress."""
    if not req.stem or "%" in req.stem:
        raise ValueError("Request.stem must be a concrete file stem")
    loop = asyncio.get_running_loop()
    tracker = progress.DownloadProgress()
    abandoned = threading.Event()
    hooks = (
        progress.make_progress_hook(
            tracker=tracker, job_id=req.job_id, user_id=req.user_id, loop=loop,
            what="",
            stop_now=abandoned.is_set,
            stop_poll=(lambda: progress.job_cancelled_from_thread(req.job_id, loop)) if req.job_id else None,
        ),
        progress.make_postprocessor_hook(tracker),
    )

    def work() -> dict:
        return _Attempts(req, force_generic=force_generic, loop=loop, hooks=hooks, abandoned=abandoned).run()

    def footprint() -> int:
        # Everything the attempt writes sits under its stem, in the output
        # folder and (the audio extracted from a video) in the audio folder.
        return progress.stem_footprint((req.output_dir, req.stem), (settings.AUDIO_DIR, req.stem))

    await progress.raise_if_cancelled_at_admission(req.job_id, req.url)
    tracker.reset(queued=True)
    try:
        return await progress.await_with_stall_guard(
            download_pool.run(work, on_admit=tracker.mark_started), tracker,
            hard_timeout=progress.VIDEO_HARD_TIMEOUT_S,
            stall_timeout=settings.DOWNLOAD_STALL_TIMEOUT_S,
            url=req.url,
            label="Download",
            footprint=footprint,
        )
    except (DownloadStalledError, DownloadTimeoutError) as gave_up:
        abandoned.set()
        logger.error("[DOWNLOAD_FAILED] host=%s class=abandoned end=%s yt_dlp=%s detail=%s",
                     host_of(req.url), type(gave_up).__name__, _ytdlp_version(), str(gave_up)[:200])
        raise
