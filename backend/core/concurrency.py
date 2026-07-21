"""Shared concurrency primitives for in-process task limiting.

Lives here (not in task_runner.py) so modules upstream of task_runner —
notably backend/services/ytdlp_service.py — can import the semaphores
without creating an import cycle. task_runner.py re-exports the same
names for backward compatibility with existing callers.

Three pools:

  _task_semaphore        Heavy CPU/disk-bound work: yt-dlp downloads
                         (the actual streaming + ffmpeg merge phase),
                         Whisper transcription, video generation.
                         Cap=2 prioritizes robustness over throughput
                         (tightened from 3 on 2026-04-27): three
                         simultaneous YouTube streams from one IP
                         consistently tripped per-IP throttling; two
                         is enough parallelism for batch downloads
                         while staying under YouTube's bot-detection
                         radar.

  _light_task_semaphore  Fast cloud-bound work: AI image gen (~5-10s),
                         AI music gen (~30-90s mostly polling), top-
                         level download wrappers (the actual heavy
                         lifting is gated inside download_video by
                         _task_semaphore, so the wrapper itself is
                         lightweight orchestration).

  _probe_semaphore       yt-dlp metadata-only probes. CPU-bound during
                         player-JS parsing + signature decipher (5-10s
                         solo, longer under contention). Capped
                         separately from _task_semaphore so a queue of
                         pending probes can't stretch the actual
                         download timeout (the 2026-04-27 regression
                         pathology). Cap=2 mirrors the heavy pool —
                         probes also hit YouTube's InnerTube API and
                         contribute to per-IP rate-limit pressure,
                         so the same robustness-over-throughput
                         tradeoff applies.
"""
import asyncio


_task_semaphore = asyncio.Semaphore(2)
_light_task_semaphore = asyncio.Semaphore(10)
_probe_semaphore = asyncio.Semaphore(2)

# Process-level cap on parallel ffmpeg subprocesses kicked off from a
# single long-running job (e.g. the per-clip parallel pipeline in
# clip_extractor that would otherwise fire N=max_clips ffmpegs at once).
# Each ffmpeg uses multiple threads internally; without a cap, an 18-clip
# extract spawns 18 parallel ffmpegs ≈ 100+ threads competing for cores
# and pegs the CPU hard enough that even a /api/jobs DB read gets pushed
# off the scheduler — UI feels frozen during heavy jobs.
#
# Cap=4 leaves plenty of headroom on a typical 8-core laptop while still
# parallelising the per-clip phases. Wall-clock time on big jobs goes up
# (5-batch sweep instead of one-shot) but UI stays responsive — the
# trade-off the user explicitly asked for. Tune up if you have a big
# machine and don't care about UI latency during processing.
_ffmpeg_semaphore = asyncio.Semaphore(4)
