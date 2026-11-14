# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The cookie jar file and browser detection (`backend.download.cookie_jar`,
`backend.download.cookies`).

Moved from the coverage suites of the deleted `ytdlp_service`. Nothing here
reads a real browser: detection is driven through patched `platform` /
`shutil` / `Path.exists`, and the one CookieStore gets a fake extractor.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.download import cookie_jar, cookies, engine
from tests import download_fakes
from tests.download_fakes import jar_text

ytdlp = download_fakes.ytdlp        # the scripted yt-dlp fixture, re-exported for this module


# ── Cookie expiration normalization ────────────────────────────────────────


class TestNormalizeCookieExpiration:
    def test_zero_passes_through(self):
        # 0 = session cookie. Keep as-is.
        assert cookie_jar.normalize_expiry("0") == "0"

    def test_unix_seconds_pass_through(self):
        # A normal future Unix timestamp.
        assert cookie_jar.normalize_expiry("1800000000") == "1800000000"

    def test_chrome_microseconds_converted(self):
        # Chrome stores expirations as microseconds since 1601-01-01.
        # 13453825375953964 ≈ year 2027.
        out = cookie_jar.normalize_expiry("13453825375953964")
        assert out is not None
        # Result is the unix-seconds equivalent.
        assert int(out) < cookie_jar._PLAUSIBLE_UNIX_MAX

    def test_non_numeric_returns_none(self):
        assert cookie_jar.normalize_expiry("not-a-number") is None

    def test_negative_value_unrecoverable(self):
        # Negative timestamps don't fit either format — drop the cookie.
        assert cookie_jar.normalize_expiry("-1") is None

    def test_value_too_large_for_either_format(self):
        # Beyond plausible chrome-epoch range too.
        out = cookie_jar.normalize_expiry("99999999999999999999")
        assert out is None


class TestSanitizeCookiesFile:
    def test_no_file_returns_zero(self, tmp_path):
        # Missing file → no work, no error.
        assert cookie_jar.sanitize(tmp_path / "missing.txt") == 0

    def test_valid_file_unchanged(self, tmp_path):
        f = tmp_path / "cookies.txt"
        f.write_text(
            "# Netscape HTTP Cookie File\n"
            ".example.com\tTRUE\t/\tFALSE\t1800000000\tsession\tabc123\n"
        )
        dropped = cookie_jar.sanitize(f)
        assert dropped == 0
        # Comments + valid rows preserved.
        assert ".example.com" in f.read_text()

    def test_converts_chrome_microseconds(self, tmp_path):
        f = tmp_path / "cookies.txt"
        f.write_text(
            ".example.com\tTRUE\t/\tFALSE\t13453825375953964\tsession\tabc\n"
        )
        cookie_jar.sanitize(f)
        # After conversion, expiration field is a plausible unix timestamp.
        line = f.read_text().splitlines()[0]
        exp = line.split("\t")[4]
        assert int(exp) < cookie_jar._PLAUSIBLE_UNIX_MAX

    def test_drops_unrecoverable_lines(self, tmp_path):
        f = tmp_path / "cookies.txt"
        f.write_text(
            ".example.com\tTRUE\t/\tFALSE\t-999\tses\tabc\n"
            ".good.com\tTRUE\t/\tFALSE\t1800000000\tses\txyz\n"
        )
        dropped = cookie_jar.sanitize(f)
        assert dropped == 1
        # The good row survives; the bad one is gone.
        assert ".good.com" in f.read_text()
        assert ".example.com" not in f.read_text()

    def test_preserves_comments_and_httponly_prefix(self, tmp_path):
        f = tmp_path / "cookies.txt"
        f.write_text(
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.example.com\tTRUE\t/\tFALSE\t1800000000\tses\tabc\n"
        )
        cookie_jar.sanitize(f)
        text = f.read_text()
        # Real comment preserved.
        assert "# Netscape" in text
        # #HttpOnly_ prefix is a domain marker — must NOT be treated as comment.
        assert "#HttpOnly_" in text


class TestCountRealCookieLines:
    def test_counts_only_valid_rows(self, tmp_path):
        f = tmp_path / "cookies.txt"
        f.write_text(
            "# comment\n"
            "\n"
            ".example.com\tTRUE\t/\tFALSE\t1800000000\tses\tabc\n"
            "malformed line\n"
            ".other.com\tTRUE\t/\tFALSE\t1800000000\tses\txyz\n"
        )
        assert cookie_jar.count_cookies(f) == 2

    def test_missing_file_returns_zero(self, tmp_path):
        assert cookie_jar.count_cookies(tmp_path / "missing") == 0

    def test_httponly_prefix_counts(self, tmp_path):
        f = tmp_path / "cookies.txt"
        f.write_text(
            "#HttpOnly_.example.com\tTRUE\t/\tFALSE\t1800000000\tses\tabc\n"
        )
        # `#HttpOnly_<domain>` is a real data line, not a comment.
        assert cookie_jar.count_cookies(f) == 1


# ── _atomic_write_text ─────────────────────────────────────────────────────


class TestAtomicWriteText:
    def test_writes_content(self, tmp_path):
        path = tmp_path / "x.txt"
        cookie_jar.atomic_write(path, "hello")
        assert path.read_text() == "hello"

    def test_replaces_existing(self, tmp_path):
        path = tmp_path / "x.txt"
        path.write_text("old")
        cookie_jar.atomic_write(path, "new")
        assert path.read_text() == "new"

    def test_cleans_up_temp_on_failure(self, tmp_path, monkeypatch):
        """If the final replace fails, the .tmp file should be cleaned up.
        We force a replace failure by monkeypatching Path.replace."""
        path = tmp_path / "x.txt"
        tmp_file_path = path.with_suffix(path.suffix + ".tmp")

        def failing_replace(self, target):
            raise OSError("disk full")

        monkeypatch.setattr(type(path), "replace", failing_replace)

        with pytest.raises(OSError):
            cookie_jar.atomic_write(path, "content")

        # Tmp file should be cleaned up so a partial write doesn't linger.
        assert not tmp_file_path.exists()


# ── _harden_cookie_file_perms ──────────────────────────────────────────────


class TestHardenCookieFilePerms:
    @pytest.mark.skipif(os.name == "nt", reason="Unix permissions don't apply to Windows")
    def test_sets_0600(self, tmp_path):
        f = tmp_path / "cookies.txt"
        f.write_text("x")
        f.chmod(0o644)
        cookie_jar.harden_permissions(f)
        mode = f.stat().st_mode & 0o777
        assert mode == 0o600

    def test_doesnt_raise_on_missing(self, tmp_path):
        # The function logs + swallows. Must not raise.
        cookie_jar.harden_permissions(tmp_path / "missing")


# ── Which browser the cookies come from ────────────────────────────────────

class TestDetectCookieBrowser:
    def test_linux_which_hit(self, monkeypatch):
        monkeypatch.setattr(cookies.platform, "system", lambda: "Linux")
        monkeypatch.setattr(cookies.shutil, "which",
                            lambda name: "/usr/bin/brave-browser" if "brave" in name else None)
        assert cookies.detect_browser() == "brave"

    def test_darwin_app_bundle_detection(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cookies.platform, "system", lambda: "Darwin")
        real_exists = Path.exists

        def exists(self):
            if str(self) == "/Applications/Google Chrome.app":
                return True
            if str(self).startswith("/Applications/"):
                return False                 # only Chrome is "installed", whatever this machine has
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", exists)
        assert cookies.detect_browser() == "chrome"

    def test_positive_result_cached(self, monkeypatch, tmp_path):
        """A found browser is looked for once per store, not on every status
        read or download (the old process-wide `_BROWSER_COOKIE_SOURCE`)."""
        monkeypatch.setattr(cookies.platform, "system", lambda: "Linux")
        calls = {"n": 0}

        def which(name):
            calls["n"] += 1
            return "/usr/bin/google-chrome" if "chrome" in name else None

        monkeypatch.setattr(cookies.shutil, "which", which)
        store = cookies.CookieStore(
            tmp_path / "browser_cookies.txt", detect=cookies.detect_browser,
            extract=lambda browser, dest: dest.write_text(jar_text()),
            diagnose=lambda b: "error", disabled=lambda: False,
        )
        assert store.status().browser == "chrome"
        before = calls["n"]
        assert before > 0
        assert store.status().browser == "chrome"
        assert store.jar_for_attempt() == store.jar_path
        assert calls["n"] == before, "second call must hit the store's cache"


# ── A cookie refresh never leaves stale cookie options behind ──────────────

@pytest.mark.asyncio
async def test_refresh_pops_stale_cookie_keys_before_reapply(ytdlp, tmp_path):
    """Was `_refresh_cookies` popping `cookiefile` / `cookiesfrombrowser`
    before re-applying: when a refresh comes back with no jar, the next
    attempt carries NO cookie option, not the stale one."""
    bot = ("ERROR: [youtube] abc: Sign in to confirm you're not a bot. "
           "Use --cookies-from-browser or --cookies for the authentication.")
    ytdlp.script = [bot, "ok"]
    refreshes = []
    ytdlp.store.refresh = lambda: refreshes.append(1)        # the refresh yields nothing usable (None)
    result = await engine.run(engine.Request(
        url="https://www.youtube.com/watch?v=abc", output_dir=tmp_path,
        stem="vid1", extract_audio=False))
    first, second = ytdlp.calls
    assert refreshes == [1]
    assert first["cookiefile"] == str(ytdlp.store.jar_path)
    assert "cookiefile" not in second and "cookiesfrombrowser" not in second
    assert Path(result["video_path"]).exists()
