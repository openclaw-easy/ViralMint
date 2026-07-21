"""Shared concurrency primitives for in-process task limiting.

Lives in its own module (not in task_runner.py) so services that must not
import task_runner — notably backend/services/clip_extractor.py — can share a
process-wide semaphore without creating an import cycle.
"""
import asyncio


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
# (4-batch sweep instead of one-shot) but UI stays responsive. Tune up if
# you have a big machine and don't care about UI latency during processing.
_ffmpeg_semaphore = asyncio.Semaphore(4)
