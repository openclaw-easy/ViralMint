# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The JS runtime handed to yt-dlp must reflect the machine, not a guess.

`js_runtimes` was hardcoded to `{"node": {}}`. Explicitly passing the option
REPLACES yt-dlp's default, so this carried two silent failures on a machine
without Node — which is the common case for a packaged desktop install, since
the app bundles no JS runtime:

  * a user with deno but not node got NO runtime, because the pin disabled the
    deno default;
  * with no runtime the EJS solver can't run, n-signature challenges go
    unsolved, and YouTube formats go missing or answer 403 — in a bug report
    that is indistinguishable from a cookie problem.

Also covered: the 403 cookie-drop rung. Supplying ANY cookie makes yt-dlp skip
every player client that can't carry one (android_vr / tv_simply / ios — the
token-free ones), so a 403 is often caused BY the jar and retrying with it can
only fail the same way.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.download import opts as ys


@pytest.fixture(autouse=True)
def _clear_runtime_cache():
    ys._JS_RUNTIMES_RESOLVED = None
    ys._warned_no_js_runtime = False
    yield
    ys._JS_RUNTIMES_RESOLVED = None
    ys._warned_no_js_runtime = False


def _which(available: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in available else None


class TestResolveJsRuntimes:
    def test_both_runtimes_are_offered(self):
        with patch.object(ys.shutil, "which", _which({"deno", "node"})):
            assert ys.resolve_js_runtimes() == {"deno": {}, "node": {}}

    def test_deno_only_is_not_disabled_by_a_node_pin(self):
        """The exact regression: hardcoding node killed the deno default."""
        with patch.object(ys.shutil, "which", _which({"deno"})):
            assert ys.resolve_js_runtimes() == {"deno": {}}

    def test_node_only(self):
        with patch.object(ys.shutil, "which", _which({"node"})):
            assert ys.resolve_js_runtimes() == {"node": {}}

    def test_nothing_installed_warns_once_and_keeps_the_default_shape(self, caplog):
        with patch.object(ys.shutil, "which", _which(set())):
            with caplog.at_level("WARNING"):
                first = ys.resolve_js_runtimes()
                second = ys.resolve_js_runtimes()
        assert first == second == {"deno": {}}
        warnings = [r for r in caplog.records if "No JavaScript runtime" in r.message]
        assert len(warnings) == 1, "the warning must not repeat every download"

    def test_a_negative_result_is_not_cached(self):
        """Installing Node mid-session must take effect on the next download."""
        with patch.object(ys.shutil, "which", _which(set())):
            ys.resolve_js_runtimes()
        with patch.object(ys.shutil, "which", _which({"node"})):
            assert ys.resolve_js_runtimes() == {"node": {}}

    def test_a_positive_result_is_cached(self):
        with patch.object(ys.shutil, "which", _which({"node"})):
            assert ys.resolve_js_runtimes() == {"node": {}}

        def _boom(_name):
            raise AssertionError("a cached result must not re-probe the PATH")

        with patch.object(ys.shutil, "which", _boom):
            assert ys.resolve_js_runtimes() == {"node": {}}

    def test_the_opts_builder_uses_the_resolver(self):
        with patch.object(ys.shutil, "which", _which({"deno"})):
            opts = ys.js_opts()
        assert opts["js_runtimes"] == {"deno": {}}
        assert "remote_components" in opts       # EJS solver still wired


class TestDropCookiesFor403:
    def test_drops_a_cookie_file_and_reports_the_change(self):
        opts = {"cookiefile": "/tmp/c.txt", "format": "best"}
        assert ys.drop_cookies(opts) is True
        assert "cookiefile" not in opts
        assert opts["format"] == "best"          # nothing else touched

    def test_drops_a_browser_jar(self):
        opts = {"cookiesfrombrowser": ("chrome",)}
        assert ys.drop_cookies(opts) is True
        assert opts == {}

    def test_reports_no_change_when_there_were_no_cookies(self):
        """Nothing to escalate — the caller must fall through to its other
        rungs instead of burning an attempt on an identical request."""
        opts = {"format": "best"}
        assert ys.drop_cookies(opts) is False
        assert opts == {"format": "best"}
