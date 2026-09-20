# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A cancelled tool job must not report success — or failure.

Cancellation is cooperative: `DELETE /api/jobs/{id}` flips the row and nothing
interrupts the coroutine. `update_job_status` deliberately allows
terminal→terminal writes (the zombie sweep self-heals through that), so a runner
that finishes after a cancel used to overwrite the user's "cancelled" with
"success" and announce a completed job. Every file-producing tool funnels through
`_tool_success` / `_tool_fail` and not one of them polled.

The artifact is deliberately KEPT on disk: deleting is the one irreversible move,
and the scratch sweeper already owns an unclaimed tool output. What must not
happen is the status write and the completion event.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core import tool_runners as tru


@pytest.fixture()
def artifact(tmp_path: Path) -> Path:
    """A REAL mp4: the artifact gate ffprobes what it is handed, so fake bytes
    would be rejected as corrupt and mask what these tests are about."""
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    out = tmp_path / "out.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=s=64x64:d=1", str(out)],
        check=True, capture_output=True, timeout=60,
    )
    return out


class TestSuccessYieldsToACancel:
    async def test_no_success_write_and_no_completion_event(self, artifact: Path):
        with patch("backend.agents.job_helper.job_cancelled", new=AsyncMock(return_value=True)), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()) as upd, \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()) as ws:
            ok = await tru._tool_success("job1", artifact, [], "local")
        assert ok is False, "a cancelled job must not report delivery"
        upd.assert_not_awaited()
        assert not ws.await_args_list

    async def test_the_artifact_survives_but_input_scratch_is_cleaned(
            self, artifact: Path, tmp_path: Path):
        scratch = tmp_path / "in.mp4"
        scratch.write_bytes(b"\x00" * 1024)
        with patch("backend.agents.job_helper.job_cancelled", new=AsyncMock(return_value=True)), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()), \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()):
            await tru._tool_success("job1", artifact, [scratch], "local")
        assert artifact.exists(), "deleting the output is irreversible — the sweeper owns it"
        assert not scratch.exists(), "input scratch is cleaned on every path"

    async def test_the_cancel_gate_runs_before_the_artifact_gate(self, tmp_path: Path):
        """A cancelled job needs no verdict, and grading it would route a broken
        artifact into _tool_fail — turning the user's cancel into a red toast."""
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        validate = AsyncMock()
        with patch("backend.agents.job_helper.job_cancelled", new=AsyncMock(return_value=True)), \
             patch("backend.services.output_validator.validate_output", new=validate), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()), \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()):
            ok = await tru._tool_success("job1", empty, [], "local")
        assert ok is False
        validate.assert_not_awaited()

    async def test_an_uncancelled_job_still_succeeds(self, artifact: Path):
        with patch("backend.agents.job_helper.job_cancelled", new=AsyncMock(return_value=False)), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()) as upd, \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()) as ws:
            ok = await tru._tool_success("job1", artifact, [], "local")
        assert ok is True
        assert "success" in [c.args[1] for c in upd.await_args_list if len(c.args) > 1]
        assert "job_complete" in [c.args[0].get("type") for c in ws.await_args_list]


class TestFailureYieldsToACancel:
    async def test_a_cancel_is_not_a_failure(self, tmp_path: Path):
        """Runners funnel every exception here, and work killed mid-flight raises
        whatever it raises."""
        scratch = tmp_path / "in.mp4"
        scratch.write_bytes(b"x")
        with patch("backend.agents.job_helper.job_cancelled", new=AsyncMock(return_value=True)), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()) as upd, \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()) as ws:
            await tru._tool_fail("job1", RuntimeError("killed"), [scratch], "local", "crop")
        upd.assert_not_awaited()
        assert not ws.await_args_list
        assert not scratch.exists()

    async def test_a_real_failure_still_reports(self, tmp_path: Path):
        with patch("backend.agents.job_helper.job_cancelled", new=AsyncMock(return_value=False)), \
             patch("backend.agents.job_helper.update_job_status", new=AsyncMock()) as upd, \
             patch("backend.core.ws_manager.ws_manager.send", new=AsyncMock()) as ws:
            await tru._tool_fail("job1", RuntimeError("boom"), [], "local", "crop")
        assert "failed" in [c.args[1] for c in upd.await_args_list if len(c.args) > 1]
        assert "job_failed" in [c.args[0].get("type") for c in ws.await_args_list]


# ── Two-sided numeric bounds, and the volume guard ─────────────────────────

class TestFormFloatsCannotBeNaN:
    """`start_seconds: float = Form(...)` parses "nan" and "inf" happily, and
    NaN fails EVERY comparison — so a one-sided `< 0` guard passed it through
    every check and handed ffmpeg `-ss nan`."""

    def test_the_bound_is_two_sided_at_both_endpoints(self):
        src = Path("backend/api/tools.py").read_text()
        assert src.count("MAX_MEDIA_SECONDS") >= 4, "trim and gif each need both bounds"
        assert "if start_seconds < 0:" not in src, "a one-sided guard is back"

    def test_the_constant_covers_real_media(self):
        from backend.services.video_utils import MAX_MEDIA_SECONDS
        assert MAX_MEDIA_SECONDS > 24 * 3600      # a day-long recording still passes
        import math
        assert not (0.0 <= float("nan") <= MAX_MEDIA_SECONDS)
        assert not (0.0 <= float("inf") <= MAX_MEDIA_SECONDS)
        assert math.isfinite(MAX_MEDIA_SECONDS)


class TestRetentionProtectsWhatItCannotCheck:
    def test_an_unreachable_volume_keeps_every_job(self):
        """This function is the only thing between a successful tool job and both
        the retention sweep and Activity's Clear button. On a sleeping drive every
        is_file() is False, so every Library item would look prunable."""
        from backend.services.job_retention import is_library_item
        from backend.config import settings

        job = MagicMock()
        job.status = "failed"          # would normally be pruned immediately
        job.output_json = None
        with patch.object(type(settings), "STORAGE_ROOT",
                          property(lambda self: Path("/nonexistent-volume-xyz"))):
            assert is_library_item(job) is True
