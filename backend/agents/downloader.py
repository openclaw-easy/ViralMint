# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Agent 3a: Download orchestrator — downloads videos from scout results."""
import asyncio
import json
import logging
import shutil
from pathlib import Path
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.scout_result import ScoutResult
from backend.models.downloaded_video import DownloadedVideo
from backend.services.ytdlp_service import download_video
from backend.core.ws_manager import ws_manager
from backend.agents.job_helper import job_cancelled, update_job_status
from backend.config import settings
from backend.core.exceptions import (
    DownloadStalledError,
    DownloadTimeoutError,
    RateLimitError,
    VideoUnavailableError,
)

logger = logging.getLogger(__name__)

MIN_DISK_SPACE_MB = 500  # Minimum free disk space required to start downloads

# Inter-download delay to avoid triggering YouTube rate limits.
# YouTube starts throttling after ~10-15 rapid sequential downloads from one IP.
# Jittered (randomized) to avoid fingerprint-able fixed-interval patterns.
from backend.core.http_utils import jittered_delay


class DownloadAgent:
    async def run(
        self,
        job_id: str,
        scout_result_ids: list[str],
        user_id: str = "local",
    ):
        """Download videos for given scout result IDs."""
        logger.info("DOWNLOAD START | job=%s | %d scout results", job_id[:8], len(scout_result_ids))

        # Pre-check disk space
        try:
            free_mb = shutil.disk_usage(settings.VIDEOS_DIR).free / (1024 * 1024)
            if free_mb < MIN_DISK_SPACE_MB:
                msg = f"Low disk space: {free_mb:.0f}MB free (need {MIN_DISK_SPACE_MB}MB). Downloads may fail."
                logger.warning(msg)
                await ws_manager.send_constraint_warning(
                    constraint="disk_space", message=msg, severity="warning", user_id=user_id,
                )
        except Exception:
            pass  # Non-critical check — continue even if it fails

        await update_job_status(job_id, "running", progress_pct=0, current_step="Starting downloads...")

        total = len(scout_result_ids)
        downloaded = []
        errors = []  # Collect actual error details per video
        rate_limited = False
        cancelled = False

        for i, sr_id in enumerate(scout_result_ids):
            # Cooperative cancellation, polled before each transfer: the one
            # in flight cannot be interrupted and is kept, but nothing after it
            # starts. This runner never polled, so a cancelled scout download
            # pulled every video anyway.
            if await job_cancelled(job_id):
                cancelled = True
                logger.info("DOWNLOAD CANCELLED | job=%s (stopped before %d/%d)",
                            job_id[:8], i + 1, total)
                break

            # If we hit a rate limit, skip remaining videos — they'll all fail too
            if rate_limited:
                errors.append(f"Video {i + 1}/{total}: Skipped (rate-limited)")
                continue

            step = f"Downloading video {i + 1}/{total}"
            await ws_manager.send_progress(job_id, (i / total) * 100, step, user_id)
            await update_job_status(job_id, "running", progress_pct=(i / total) * 100, current_step=step)

            try:
                # Load scout result
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(ScoutResult).where(ScoutResult.id == sr_id))
                    sr = result.scalar_one_or_none()

                if not sr:
                    logger.warning(f"Scout result {sr_id} not found — skipping")
                    errors.append(f"Video {i + 1}/{total}: Scout result not found")
                    continue

                logger.info("DOWNLOAD video %d/%d | platform=%s url=%s", i + 1, total, sr.platform, sr.video_url[:80])

                # Inter-download delay for batches (skip first video)
                if i > 0:
                    await asyncio.sleep(jittered_delay())

                # Download
                dl_result = await download_video(
                    url=sr.video_url,
                    output_dir=settings.VIDEOS_DIR,
                    filename=sr.video_id,
                    extract_audio=True,
                )

                # Save to DB
                async with AsyncSessionLocal() as db:
                    # Extract subtitle transcript if available
                    subtitles = dl_result.get("subtitles")
                    transcript = None
                    transcript_language = None
                    transcript_source = None
                    transcript_segments_json = None
                    if subtitles and isinstance(subtitles, dict) and subtitles.get("text"):
                        transcript = subtitles["text"]
                        transcript_language = subtitles.get("language")
                        transcript_source = subtitles.get("source", "auto_subtitles")
                        # Save segments with timestamps — enables clip extraction without Whisper
                        if subtitles.get("segments"):
                            transcript_segments_json = json.dumps(subtitles["segments"])
                            logger.info(f"Saved {len(subtitles['segments'])} subtitle segments for {sr.title[:40]}")

                    chapters = dl_result.get("chapters")
                    tags = dl_result.get("tags")
                    category = dl_result.get("category")

                    dv = DownloadedVideo(
                        user_id=user_id,
                        scout_result_id=sr_id,
                        title=sr.title,
                        platform=sr.platform,
                        video_path=dl_result.get("video_path"),
                        audio_path=dl_result.get("audio_path"),
                        duration_seconds=dl_result.get("duration"),
                        file_size_mb=dl_result.get("file_size_mb"),
                        transcript=transcript,
                        transcript_language=transcript_language,
                        transcript_source=transcript_source,
                        transcript_segments_json=transcript_segments_json,
                        chapters_json=json.dumps(chapters) if chapters else None,
                        tags_json=json.dumps(tags) if tags else None,
                        category=category,
                    )
                    db.add(dv)

                    # Mark scout result as downloaded
                    sr_update = await db.execute(select(ScoutResult).where(ScoutResult.id == sr_id))
                    sr_row = sr_update.scalar_one_or_none()
                    if sr_row:
                        sr_row.is_downloaded = True

                    await db.commit()
                    await db.refresh(dv)
                    downloaded.append(dv.id)
                    logger.info("DOWNLOAD OK | id=%s path=%s", dv.id[:8], dv.video_path)

            except RateLimitError as e:
                rate_limited = True
                error_detail = f"Video {i + 1}/{total} '{sr.title[:40] if sr else sr_id[:8]}': {e}"
                errors.append(error_detail)
                logger.warning(f"Rate limited on video {i + 1}/{total}, skipping remaining: {e}")
                await ws_manager.send_constraint_warning(
                    constraint="rate_limit",
                    message=f"YouTube is rate-limiting downloads. {len(downloaded)}/{total} downloaded so far. Try again later for the rest.",
                    severity="warning",
                    user_id=user_id,
                )

            except VideoUnavailableError as e:
                error_detail = f"Video {i + 1}/{total} '{sr.title[:40] if sr else sr_id[:8]}': {e}"
                errors.append(error_detail)
                logger.warning(f"Video unavailable {sr_id}: {e}")

            except (DownloadStalledError, DownloadTimeoutError) as e:
                # An ABANDONED download: its worker thread is still running and
                # still owns `<video_id>*.part` — its cleanup will delete them
                # later. The AI URL-fix retry below writes into that same stem,
                # so it must not run here; the error is recorded and the batch
                # moves on.
                error_detail = f"Video {i + 1}/{total} '{sr.title[:40] if sr else sr_id[:8]}': {e}"
                errors.append(error_detail)
                logger.warning(f"Download abandoned {sr_id}: {e}")

            except Exception as first_error:
                error_detail = f"Video {i + 1}/{total} '{sr.title[:40] if sr else sr_id[:8]}': {first_error}"

                # AI-assisted retry: ask AI to fix the URL
                try:
                    from backend.core.ai_retry import ai_fix_url
                    from backend.models.user_settings import UserSettings

                    logger.info(f"Download failed for {sr_id}, attempting AI-assisted URL fix...")
                    async with AsyncSessionLocal() as db:
                        us_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
                        user_settings = us_result.scalar_one_or_none()

                    corrected_url = await ai_fix_url(sr.video_url, str(first_error), user_settings)
                    if corrected_url:
                        logger.info(f"Retrying download with AI-corrected URL: {corrected_url}")
                        dl_result = await download_video(
                            url=corrected_url,
                            output_dir=settings.VIDEOS_DIR,
                            filename=sr.video_id,
                            extract_audio=True,
                        )
                        # Save to DB with full metadata (same as success path above)
                        async with AsyncSessionLocal() as db:
                            subtitles = dl_result.get("subtitles")
                            transcript = None
                            transcript_language = None
                            transcript_source = None
                            transcript_segments_json = None
                            if subtitles and isinstance(subtitles, dict) and subtitles.get("text"):
                                transcript = subtitles["text"]
                                transcript_language = subtitles.get("language")
                                transcript_source = subtitles.get("source", "auto_subtitles")
                                if subtitles.get("segments"):
                                    transcript_segments_json = json.dumps(subtitles["segments"])

                            chapters = dl_result.get("chapters")
                            tags = dl_result.get("tags")
                            category = dl_result.get("category")

                            dv = DownloadedVideo(
                                user_id=user_id,
                                scout_result_id=sr_id,
                                title=sr.title,
                                platform=sr.platform,
                                video_path=dl_result.get("video_path"),
                                audio_path=dl_result.get("audio_path"),
                                duration_seconds=dl_result.get("duration"),
                                file_size_mb=dl_result.get("file_size_mb"),
                                transcript=transcript,
                                transcript_language=transcript_language,
                                transcript_source=transcript_source,
                                transcript_segments_json=transcript_segments_json,
                                chapters_json=json.dumps(chapters) if chapters else None,
                                tags_json=json.dumps(tags) if tags else None,
                                category=category,
                            )
                            db.add(dv)
                            sr_update = await db.execute(select(ScoutResult).where(ScoutResult.id == sr_id))
                            sr_row = sr_update.scalar_one_or_none()
                            if sr_row:
                                sr_row.is_downloaded = True
                            await db.commit()
                            await db.refresh(dv)
                            downloaded.append(dv.id)
                        continue  # Success on retry — skip the error recording
                except Exception as retry_error:
                    logger.warning(f"AI-assisted retry also failed for {sr_id}: {retry_error}")

                errors.append(error_detail)
                logger.error(f"Failed to download {sr_id}: {first_error}")
                await ws_manager.send_constraint_warning(
                    constraint="download",
                    message=f"Download failed: {first_error}",
                    severity="warning",
                    user_id=user_id,
                )

        # Build informative error message with actual details
        error_summary = None
        if errors:
            error_summary = f"Failed {len(errors)}/{total} download(s):\n" + "\n".join(errors)

        # Re-polled after the loop: a cancel landing during the LAST transfer is
        # never seen by the loop gate. The status stays "cancelled", the reason
        # is kept, and neither job_complete nor job_failed goes out for work
        # the user already stopped.
        if cancelled or await job_cancelled(job_id):
            step = f"Cancelled — {len(downloaded)}/{total} downloaded before stopping"
            if errors:
                step += f" ({len(errors)} also failed — see details)"
            await update_job_status(
                job_id, "cancelled",
                current_step=step,
                error_message=error_summary,
                output_data={"downloaded_ids": downloaded, "cancelled": True},
            )
            return downloaded

        if len(downloaded) == 0 and total > 0:
            user_msg = (
                f"YouTube is rate-limiting downloads from this IP. Try again in 10-15 minutes."
                if rate_limited else
                f"All {total} download(s) failed. They may be unavailable or region-blocked."
            )
            await update_job_status(
                job_id, "failed",
                progress_pct=100,
                current_step=f"Downloaded 0/{total} videos — all downloads failed",
                error_message=error_summary or f"Failed to download all {total} video(s).",
            )
            await ws_manager.send({
                "type": "job_failed",
                "job_id": job_id,
                "error": user_msg,
            }, user_id)
        else:
            await update_job_status(
                job_id, "success",
                progress_pct=100,
                current_step=f"Downloaded {len(downloaded)}/{total} videos",
                output_data={"downloaded_ids": downloaded},
                error_message=error_summary if errors else None,
            )
            await ws_manager.send({
                "type": "job_complete",
                "job_id": job_id,
                "result": {"downloaded": len(downloaded), "total": total},
            }, user_id)

        return downloaded
