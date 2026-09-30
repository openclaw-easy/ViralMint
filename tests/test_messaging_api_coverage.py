# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""backend/api/messaging.py — the /api/messaging router, over HTTP.

A minimal FastAPI app mounts only this router (no lifespan, no real bots),
and the module's `messaging` manager is swapped for one holding fake
channels, so every route runs end-to-end without a token or a network.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from backend.api import messaging as api
from backend.messaging.manager import MessagingManager


def _fake(name, *, configured=True, send_ok=True, status=None):
    ch = MagicMock()
    ch.channel_name = name
    ch.configure = AsyncMock(return_value={"awaiting": True, "pair_code": "ABC234"})
    ch.disconnect = AsyncMock(return_value=True)
    ch.send_test = AsyncMock(return_value=send_ok)
    ch.send = AsyncMock(return_value=send_ok)
    ch.is_configured = AsyncMock(return_value=configured)
    ch.status = MagicMock(return_value=status or {"connected": configured})
    return ch


@pytest.fixture
def channels(monkeypatch):
    mgr = MessagingManager()
    chans = {n: _fake(n) for n in ("telegram", "whatsapp", "discord", "slack")}
    for c in chans.values():
        mgr.register(c)
    monkeypatch.setattr(api, "messaging", mgr)
    return chans


@pytest.fixture
def empty_manager(monkeypatch):
    monkeypatch.setattr(api, "messaging", MessagingManager())


@pytest_asyncio.fixture
async def http():
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


# ── status ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_reports_every_channel(http, channels):
    channels["discord"].status.return_value = {"connected": False, "pair_code": "XYZ789"}
    r = await http.get("/api/messaging/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"telegram", "whatsapp", "discord", "slack"}
    assert body["discord"] == {"connected": False, "pair_code": "XYZ789"}
    for c in channels.values():
        c.status.assert_called_once_with("local")


@pytest.mark.asyncio
async def test_status_with_no_channels_registered(http, empty_manager):
    body = (await http.get("/api/messaging/status")).json()
    assert body["slack"] == {"connected": False, "installed": False}


# ── connect ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_connect_trims_and_passes_the_token(http, channels):
    r = await http.post("/api/messaging/telegram/connect", json={"bot_token": "  123:abc "})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "awaiting": True, "pair_code": "ABC234"}
    channels["telegram"].configure.assert_awaited_once_with("local", bot_token="123:abc")


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body,detail", [
    ("telegram", {"bot_token": "  "}, "bot_token is required"),
    ("discord", {"bot_token": ""}, "bot_token is required"),
    ("slack", {"bot_token": "xoxb-1", "app_token": " "}, "bot_token and app_token are required"),
])
async def test_blank_tokens_are_a_400(http, channels, path, body, detail):
    r = await http.post(f"/api/messaging/{path}/connect", json=body)
    assert r.status_code == 400 and r.json()["detail"] == detail
    channels[path].configure.assert_not_called()


@pytest.mark.asyncio
async def test_missing_body_fields_are_a_422(http, channels):
    r = await http.post("/api/messaging/slack/connect", json={"bot_token": "xoxb-1"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_discord_and_slack_and_whatsapp_connect(http, channels):
    r = await http.post("/api/messaging/discord/connect", json={"bot_token": " t "})
    assert r.status_code == 200
    channels["discord"].configure.assert_awaited_once_with("local", bot_token="t")

    r = await http.post("/api/messaging/slack/connect", json={"bot_token": " xoxb-1", "app_token": "xapp-1 "})
    assert r.status_code == 200
    channels["slack"].configure.assert_awaited_once_with("local", bot_token="xoxb-1", app_token="xapp-1")

    channels["whatsapp"].configure.return_value = {"awaiting_qr": True}
    r = await http.post("/api/messaging/whatsapp/connect")
    assert r.json() == {"ok": True, "awaiting_qr": True}
    channels["whatsapp"].configure.assert_awaited_once_with("local")


@pytest.mark.asyncio
async def test_a_value_error_is_a_400_with_its_message(http, channels):
    channels["telegram"].configure.side_effect = ValueError("Invalid Telegram bot token.")
    r = await http.post("/api/messaging/telegram/connect", json={"bot_token": "x"})
    assert r.status_code == 400 and r.json()["detail"] == "Invalid Telegram bot token."


@pytest.mark.asyncio
async def test_an_unexpected_error_is_a_500(http, channels):
    channels["slack"].configure.side_effect = RuntimeError("socket exploded")
    r = await http.post("/api/messaging/slack/connect", json={"bot_token": "xoxb-1", "app_token": "xapp-1"})
    assert r.status_code == 500 and r.json()["detail"] == "Connect failed: socket exploded"


@pytest.mark.asyncio
async def test_an_unregistered_channel_is_a_500(http, empty_manager):
    r = await http.post("/api/messaging/whatsapp/connect")
    assert r.status_code == 500 and r.json()["detail"] == "Whatsapp channel not registered"
    r = await http.post("/api/messaging/discord/disconnect")
    assert r.status_code == 500
    r = await http.post("/api/messaging/slack/test")
    assert r.status_code == 500


# ── disconnect / test ────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["telegram", "whatsapp", "discord", "slack"])
async def test_disconnect(http, channels, name):
    r = await http.post(f"/api/messaging/{name}/disconnect")
    assert r.status_code == 200 and r.json() == {"ok": True}
    channels[name].disconnect.assert_awaited_once_with("local")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["telegram", "whatsapp", "discord", "slack"])
async def test_test_message_ok_and_not_connected_hint(http, channels, name):
    r = await http.post(f"/api/messaging/{name}/test")
    assert r.status_code == 200 and r.json() == {"ok": True}
    channels[name].send_test.return_value = False
    r = await http.post(f"/api/messaging/{name}/test")
    assert r.status_code == 400 and r.json()["detail"] == api.NOT_CONNECTED_HINTS[name]


@pytest.mark.asyncio
async def test_hint_for_an_unknown_channel_name(monkeypatch):
    mgr = MessagingManager()
    mgr.register(_fake("feishu", send_ok=False))
    monkeypatch.setattr(api, "messaging", mgr)
    with pytest.raises(api.HTTPException) as ei:
        await api._test_channel("feishu")
    assert ei.value.status_code == 400 and ei.value.detail == "Could not send — channel not ready."
