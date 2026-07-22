# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""API smoke suite — boots the real FastAPI app against a throwaway SQLite DB
and exercises every parameter-free GET route plus a handful of cheap POSTs.

Goal is COVERAGE OF THE ROUTER + WIRING LAYER, not behavioural assertions: we
assert only that each endpoint executes without a 5xx (a handler crash). Routes
that reach out to the network (scout suggest, news, search-demand) are excluded
so the suite stays hermetic and fast. Messaging startup is stubbed to a no-op so
the lifespan doesn't try to open real Telegram/WhatsApp/Slack/Discord sessions.
"""
import os
import tempfile
from pathlib import Path

# Point the app's data dir at a throwaway location BEFORE backend.config is
# imported so the DB/storage/.env all land in a temp dir, never the repo root.
_TMP = Path(tempfile.mkdtemp(prefix="vm-smoke-"))
os.environ["VIRALMINT_DATA_DIR"] = str(_TMP)
os.environ.setdefault("DEBUG", "false")

import pytest
from fastapi.routing import APIRoute
from starlette.testclient import TestClient

from backend.main import create_app
from backend.messaging import manager as messaging_manager

# Substrings marking network-bound or intentionally-excluded GET routes.
_SKIP_SUBSTRINGS = (
    "suggest",
    "search-demand",
    "/news",
    "docs",
    "openapi",
)


@pytest.fixture(scope="module")
def client():
    # Stub messaging so the lifespan startup never opens real channel sessions.
    async def _noop(*a, **k):
        return None

    messaging_manager.messaging.start_all = _noop  # type: ignore[assignment]
    messaging_manager.messaging.stop_all = _noop  # type: ignore[assignment]

    app = create_app()
    with TestClient(app) as c:
        yield c


def _get_routes(app):
    out = []
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        if "GET" not in r.methods:
            continue
        if "{" in r.path:  # requires a path param — skip
            continue
        if any(s in r.path for s in _SKIP_SUBSTRINGS):
            continue
        out.append(r.path)
    return sorted(set(out))


def test_app_builds_and_has_routes(client):
    routes = _get_routes(client.app)
    assert len(routes) > 5


def test_parameterless_get_routes_do_not_5xx(client):
    """Every hermetic GET route must execute without a server error."""
    failures = []
    for path in _get_routes(client.app):
        resp = client.get(path)
        # <500 = handler ran to completion (200/redirect/4xx validation all fine).
        if resp.status_code >= 500:
            failures.append(f"{path} -> {resp.status_code}")
    assert not failures, "GET routes returned 5xx: " + "; ".join(failures)


def test_jobs_list_ok(client):
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    assert "jobs" in resp.json()


def test_videos_list_ok(client):
    resp = client.get("/api/videos")
    assert resp.status_code == 200


def test_settings_get_ok(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200


def test_unknown_api_route_no_5xx(client):
    # Unknown paths are handled by the SPA catch-all (serves index.html when a
    # built frontend/dist exists, else 404) — either way, never a server error.
    resp = client.get("/api/definitely-not-a-real-route-xyz")
    assert resp.status_code < 500
