# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""One origin allowlist, enforced on HTTP *and* on the chat WebSocket.

A local server is reachable from every page the user has open. The HTTP CSRF
middleware has always rejected a foreign Origin on a state-changing request,
but Starlette's `@app.middleware("http")` never sees a websocket scope — so
`ws://127.0.0.1:16888/ws/chat` accepted a handshake from anywhere, and whoever
opened it could drive the planner (dispatching real jobs, cancelling the
user's) and read every event the app streams back.

Both checks now read `backend.core.origins`. LAN mode (`HOST=0.0.0.0`, a
documented option) has an origin nobody can enumerate in advance, so the strict
check is skipped there — on both surfaces, or the socket would contradict the
router.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="vm-origins-"))
os.environ["VIRALMINT_DATA_DIR"] = str(_TMP)
os.environ.setdefault("DEBUG", "false")

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.main import create_app
from backend.messaging import manager as messaging_manager


@pytest.fixture(scope="module")
def client():
    async def _noop(*a, **k):
        return None
    messaging_manager.messaging.start_all = _noop      # type: ignore[assignment]
    messaging_manager.messaging.stop_all = _noop       # type: ignore[assignment]
    with TestClient(create_app()) as c:
        yield c


class TestTheAllowlistIsShared:
    def test_the_middleware_reads_the_shared_module(self):
        """Rule #32: two copies of a security rule drift, and the copy that
        drifts is the one nobody is testing."""
        src = Path("backend/main.py").read_text()
        assert "from backend.core.origins import" in src
        assert "urlparse(header)" not in src, "main.py re-implements the check"

    def test_loopback_origins_pass_and_a_stranger_does_not(self):
        from backend.core.origins import origin_ok
        from backend.config import settings
        assert origin_ok(f"http://127.0.0.1:{settings.PORT}")
        assert origin_ok(f"http://localhost:{settings.PORT}")
        assert not origin_ok("https://evil.example")
        assert not origin_ok("http://127.0.0.1.evil.example")

    def test_an_absent_header_is_not_judged_here(self):
        """Each caller decides what a missing Origin means (both allow it, for
        non-browser clients). This helper only judges a header that exists."""
        from backend.core.origins import origin_ok
        assert not origin_ok(None)
        assert not origin_ok("")


class TestChatSocketOrigin:
    def test_a_foreign_origin_cannot_open_the_chat_socket(self, client):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/chat", headers={"origin": "https://evil.example"}
            ):
                pass

    def test_our_own_origin_connects(self, client):
        from backend.config import settings
        with client.websocket_connect(
            "/ws/chat", headers={"origin": f"http://127.0.0.1:{settings.PORT}"}
        ) as ws:
            ws.close()

    def test_no_origin_still_connects(self, client):
        """A messaging bridge or a CLI sends no Origin — that path must keep
        working, or the Telegram/Slack channels break."""
        with client.websocket_connect("/ws/chat") as ws:
            ws.close()

    def test_lan_mode_skips_the_strict_check(self, client, monkeypatch):
        """HOST=0.0.0.0 is the documented way to drive the UI from a phone; its
        Origin is the machine's LAN IP, unknown ahead of time."""
        from backend.core import origins
        monkeypatch.setattr(origins.settings, "HOST", "0.0.0.0")
        with client.websocket_connect(
            "/ws/chat", headers={"origin": "http://192.168.1.44:16888"}
        ) as ws:
            ws.close()


class TestMediaServesImagesOnly:
    def test_the_cookie_jar_is_not_downloadable(self, client):
        """TMP_DIR is shared scratch — it also holds yt-dlp's cookie jar, i.e.
        the user's logged-in YouTube/TikTok session."""
        from backend.config import settings
        jar = Path(settings.TMP_DIR) / "browser_cookies.txt"
        jar.parent.mkdir(parents=True, exist_ok=True)
        jar.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n")
        r = client.get("/api/media/browser_cookies.txt")
        assert r.status_code == 404
        assert "secret" not in r.text

    def test_a_real_image_still_serves(self, client):
        from backend.config import settings
        png = Path(settings.TMP_DIR) / "ref.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        r = client.get("/api/media/ref.png")
        assert r.status_code == 200
        assert r.content.startswith(b"\x89PNG")

    def test_a_directory_is_not_a_file(self, client):
        from backend.config import settings
        d = Path(settings.TMP_DIR) / "weird.png"
        d.mkdir(parents=True, exist_ok=True)
        assert client.get("/api/media/weird.png").status_code == 404
