# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The downloader's own health memory (`backend.download.health`, `util.host_of`).

Moved from the coverage suites of the deleted `ytdlp_service`: host
bucketing, the per-host 429 cooldown, and the failure streak that updates or
rolls back yt-dlp (the trigger for both pip incidents). No pip, no network.
"""
from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import pytest

from backend.download import health
from backend.download.util import host_of


# ── _host_for_url + _empty_host_state ──────────────────────────────────────


class TestHostForUrl:
    def test_extracts_netloc(self):
        assert host_of("https://www.youtube.com/v") == "www.youtube.com"

    def test_strips_port(self):
        assert host_of("https://localhost:8080/v") == "localhost"

    def test_empty_url_returns_unknown(self):
        # Single bucket for orphaned calls — never crash.
        assert host_of(None) == "unknown"
        assert host_of("") == "unknown"

    def test_relative_url_returns_unknown(self):
        # urlparse of "/path" gives empty netloc — bucket as unknown.
        assert host_of("/path/only") == "unknown"


class TestEmptyHostState:
    def test_fresh_state_shape(self):
        st = health._empty_host_state()
        assert st["consecutive_429s"] == 0
        assert st["last_429_time"] == 0.0
        assert st["cooldown_until"] == 0.0


# ── Rate-limit state machine ───────────────────────────────────────────────


class TestRateLimitStateMachine:
    """Per-host cooldown bucketing — a 429 on TikTok must NOT pause YouTube."""

    @pytest.fixture(autouse=True)
    def _fresh_state(self, monkeypatch):
        # Each test starts with a fresh state dict, and leaves none behind.
        monkeypatch.setattr(health, "_rate_limit_by_host", {})

    def test_record_rate_limit_creates_bucket(self):
        health.record_rate_limit("https://www.tiktok.com/v/1")
        assert health._rate_limit_by_host.get("www.tiktok.com") is not None

    def test_cooldown_grows_exponentially(self):
        url = "https://www.tiktok.com/v/1"
        first = health.record_rate_limit(url)
        second = health.record_rate_limit(url)
        third = health.record_rate_limit(url)
        # Each rate-limit hit grows the backoff (capped at MAX).
        assert second >= first
        assert third >= second

    def test_cooldown_per_host(self):
        health.record_rate_limit("https://www.tiktok.com/v/1")
        # YouTube should NOT inherit TikTok's cooldown.
        assert health.cooldown_for("https://www.youtube.com/v/1") == 0.0

    def test_check_cooldown_after_rate_limit(self):
        url = "https://www.tiktok.com/v/1"
        health.record_rate_limit(url)
        wait = health.cooldown_for(url)
        assert wait > 0

    def test_record_success_resets_consecutive_counter(self):
        url = "https://www.tiktok.com/v/1"
        health.record_rate_limit(url)
        health.record_rate_limit(url)
        health.record_success(url)
        state = health._rate_limit_by_host["www.tiktok.com"]
        assert state["consecutive_429s"] == 0

    def test_record_success_on_unseen_host_noop(self):
        # Don't crash if there's no bucket yet.
        health.record_success("https://never-seen.com/v")  # must not raise

    def test_purge_stale_buckets(self):
        url = "https://www.tiktok.com/v/1"
        health.record_rate_limit(url)
        # Force the bucket to "expire" by backdating its last_429_time.
        health._rate_limit_by_host["www.tiktok.com"]["last_429_time"] = time.time() - 999999
        health._purge_stale_rate_limit_buckets()
        assert "www.tiktok.com" not in health._rate_limit_by_host

    def test_check_cooldown_zero_for_unseen_host(self):
        assert health.cooldown_for("https://fresh.example.com/v") == 0.0


# ── The failure streak: forward update, or leave pip alone ──────────────────

class TestCountFailure:
    """`_count_failure`: what an update streak does once a failure counted.
    What may reach it at all is `record_failure`'s rule, pinned in
    test_download_opts_health_messages.py."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(health, "_consecutive_download_failures", 0)

    def test_below_threshold_is_noop(self, monkeypatch):
        called = []
        monkeypatch.setattr(health, "_try_update_ytdlp", lambda: called.append(1) or True)
        health._count_failure()
        health._count_failure()
        assert called == [], "2 failures must not trigger pip"

    def test_a_stale_release_is_updated_and_the_count_resets(self, monkeypatch):
        called = []
        monkeypatch.setattr(health, "_try_update_ytdlp", lambda: called.append(1) or True)
        monkeypatch.setattr(health, "_get_ytdlp_age_days", lambda: 30)
        for _ in range(3):
            health._count_failure()
        assert called == [1]
        assert health._consecutive_download_failures == 0

    def test_a_fresh_release_touches_nothing(self, monkeypatch):
        """A week-old release failing points somewhere else; pip must not run."""
        called = []
        monkeypatch.setattr(health, "_try_update_ytdlp", lambda: called.append(1) or True)
        monkeypatch.setattr(health, "_get_ytdlp_age_days", lambda: 2)
        for _ in range(3):
            health._count_failure()
        assert called == []


class TestTryUpdateYtdlpDevMode:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(health, "_last_update_attempt", 0.0)
        # `_try_update_ytdlp` does `import sys` inside the function, so
        # patch the REAL sys module.
        monkeypatch.setattr(sys, "frozen", False, raising=False)

    def test_forward_update_is_floor_bounded(self, monkeypatch):
        seen = {}

        def fake_run(cmd, capture_output=True, text=True, timeout=120):
            seen["cmd"] = cmd
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        import subprocess as sp
        monkeypatch.setattr(sp, "run", fake_run)
        import importlib
        monkeypatch.setattr(importlib, "reload", lambda mod: mod)

        assert health._try_update_ytdlp() is True
        spec = [c for c in seen["cmd"] if c.startswith("yt-dlp>=")]
        assert spec, "forward update must carry the requirements.txt lower bound"

    def test_pip_failure_returns_false(self, monkeypatch):
        import subprocess as sp
        monkeypatch.setattr(sp, "run",
                            lambda *a, **k: SimpleNamespace(returncode=1, stderr="err", stdout=""))
        assert health._try_update_ytdlp() is False

    def test_cooldown_blocks_second_attempt(self, monkeypatch):
        monkeypatch.setattr(health, "_last_update_attempt", time.time())
        assert health._try_update_ytdlp() is False


class TestConsecutiveFailureTracker:
    def test_module_has_failure_counter(self):
        # The streak counter `record_success` clears and `_count_failure` feeds.
        assert hasattr(health, "_consecutive_download_failures")
