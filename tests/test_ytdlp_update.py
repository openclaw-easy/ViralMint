# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Coverage for the yt-dlp version-management surface in ytdlp_service.

Ported from the hosted variant and adapted for the OSS build (whose
`_try_update_ytdlp` is a straight version-bounded pip install — no
rollback-target / MIN_SAFE floor / overlay machinery).

  - `_try_update_ytdlp` — pip install paths (frozen skip, cooldown)
  - `check_ytdlp_version` — periodic staleness check
  - `_record_download_failure` — consecutive-failure escalation
  - `_get_ytdlp_age_days` — age helper

All paths are mocked at the subprocess + asyncio boundary.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock

import backend.services.ytdlp_service as ys


def _reset_update_cooldown():
    """Each test runs from a clean cooldown so the _UPDATE_COOLDOWN guard
    doesn't short-circuit the test."""
    ys._last_update_attempt = 0.0


# ── _try_update_ytdlp (non-frozen path: pip install) ───────────────────────


class TestTryUpdateYtdlpPip:
    def setup_method(self):
        _reset_update_cooldown()

    def test_pip_install_success(self):
        # Mock subprocess.run + importlib.reload so we don't touch real pip.
        with patch("sys.frozen", create=True, new=False), \
             patch("subprocess.run") as run, \
             patch("importlib.reload") as reload_fn:
            run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            result = ys._try_update_ytdlp()
        assert result is True
        # Module reloaded so the new version is picked up.
        reload_fn.assert_called()

    def test_update_spec_is_version_bounded(self):
        """The auto-update must carry a lower bound so a stale interpreter
        can't silently resolve an ancient nightly (the downgrade class)."""
        with patch("sys.frozen", create=True, new=False), \
             patch("subprocess.run") as run, \
             patch("importlib.reload"):
            run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            ys._try_update_ytdlp()
        cmd = run.call_args[0][0]
        # A bounded "yt-dlp>=X.Y.Z" spec, never a bare unbounded "yt-dlp".
        assert any(isinstance(p, str) and p.startswith("yt-dlp>=") for p in cmd)
        assert "yt-dlp" not in cmd

    def test_frozen_build_skips_update(self):
        """In a PyInstaller bundle there's no pip; the update must skip
        without touching subprocess."""
        with patch("sys.frozen", create=True, new=True), \
             patch("subprocess.run") as run:
            result = ys._try_update_ytdlp()
        assert result is False
        run.assert_not_called()

    def test_pip_install_failure_returns_false(self):
        with patch("sys.frozen", create=True, new=False), \
             patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="failed")
            result = ys._try_update_ytdlp()
        assert result is False

    def test_pip_install_timeout_returns_false(self):
        with patch("sys.frozen", create=True, new=False), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("pip", 120)):
            result = ys._try_update_ytdlp()
        assert result is False

    def test_pip_install_exception_returns_false(self):
        with patch("sys.frozen", create=True, new=False), \
             patch("subprocess.run", side_effect=OSError("pip not found")):
            result = ys._try_update_ytdlp()
        assert result is False

    def test_cooldown_blocks_repeat_attempts(self):
        # First attempt sets _last_update_attempt = now. Second attempt
        # within the cooldown window must short-circuit to False.
        with patch("sys.frozen", create=True, new=False), \
             patch("subprocess.run") as run, \
             patch("importlib.reload"):
            run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            ys._try_update_ytdlp()  # First call succeeds
            run.reset_mock()
            second = ys._try_update_ytdlp()  # Second call should skip
        assert second is False
        run.assert_not_called()


# ── _record_download_failure (escalation logic) ────────────────────────────


class TestRecordDownloadFailure:
    def setup_method(self):
        ys._consecutive_download_failures = 0
        _reset_update_cooldown()

    def test_increments_counter(self):
        ys._record_download_failure()
        assert ys._consecutive_download_failures == 1
        ys._record_download_failure()
        assert ys._consecutive_download_failures == 2

    def test_below_threshold_does_not_trigger_update(self):
        """Until N consecutive failures pile up, no update attempt."""
        with patch.object(ys, "_try_update_ytdlp") as upd:
            for _ in range(ys._FAILURE_THRESHOLD_FOR_UPDATE - 1):
                ys._record_download_failure()
            # Threshold not yet hit.
            upd.assert_not_called()

    def test_threshold_triggers_action(self):
        """At the threshold, with an old-enough yt-dlp, the update path
        fires. Pin that `_try_update_ytdlp` gets called."""
        with patch.object(ys, "_try_update_ytdlp", return_value=True) as upd, \
             patch.object(ys, "_get_ytdlp_age_days", return_value=10):
            for _ in range(ys._FAILURE_THRESHOLD_FOR_UPDATE):
                ys._record_download_failure()
            upd.assert_called()

    def test_threshold_with_fresh_ytdlp_skips_update(self):
        """A fresh yt-dlp (<= 7 days) must NOT auto-update even at the
        failure threshold — the problem isn't a stale binary."""
        with patch.object(ys, "_try_update_ytdlp") as upd, \
             patch.object(ys, "_get_ytdlp_age_days", return_value=1):
            for _ in range(ys._FAILURE_THRESHOLD_FOR_UPDATE):
                ys._record_download_failure()
            upd.assert_not_called()


# ── check_ytdlp_version ────────────────────────────────────────────────────


class TestCheckYtdlpVersion:
    def setup_method(self):
        _reset_update_cooldown()

    def test_old_version_triggers_update(self):
        """yt-dlp older than the threshold should trigger an update."""
        with patch.object(ys, "_get_ytdlp_age_days", return_value=999), \
             patch.object(ys, "_try_update_ytdlp", return_value=True) as upd:
            ys.check_ytdlp_version()
        # Old yt-dlp → update path fired.
        upd.assert_called()

    def test_fresh_version_skips_update(self):
        """yt-dlp installed recently should NOT trigger an update."""
        with patch.object(ys, "_get_ytdlp_age_days", return_value=1), \
             patch.object(ys, "_try_update_ytdlp") as upd:
            ys.check_ytdlp_version()
        upd.assert_not_called()


# ── _get_ytdlp_age_days ────────────────────────────────────────────────────


class TestGetYtdlpAgeDays:
    def test_returns_int(self):
        age = ys._get_ytdlp_age_days()
        # Real yt-dlp installed → a non-negative day count; -1 if the
        # version string can't be parsed. Either way, an int.
        assert isinstance(age, int)
        assert age >= -1
