# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Shared fakes for the messaging channel coverage tests.

Nothing here touches a network: every channel's transport (PTB, discord.py,
slack-sdk, neonize) is replaced by a fake in the test that needs it. The DB is
the isolated test DB conftest.py points VIRALMINT_DATA_DIR at.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select


async def reset_channel_rows(channel: str) -> None:
    from backend.database import AsyncSessionLocal, init_db
    from backend.models.messaging_config import MessagingConfig
    await init_db()
    async with AsyncSessionLocal() as db:
        await db.execute(delete(MessagingConfig).where(MessagingConfig.channel == channel))
        await db.commit()


async def add_row(channel: str, **fields):
    from backend.database import AsyncSessionLocal
    from backend.models.messaging_config import MessagingConfig
    async with AsyncSessionLocal() as db:
        row = MessagingConfig(channel=channel, **fields)
        db.add(row)
        await db.commit()
        return row.id


async def get_rows(channel: str, user_id: str | None = None):
    from backend.database import AsyncSessionLocal
    from backend.models.messaging_config import MessagingConfig
    async with AsyncSessionLocal() as db:
        q = select(MessagingConfig).where(MessagingConfig.channel == channel)
        if user_id is not None:
            q = q.where(MessagingConfig.user_id == user_id)
        return list((await db.execute(q)).scalars().all())


def capture_ws(monkeypatch):
    """Record every ws_manager.send / send_constraint_warning call."""
    sent, warnings = [], []

    async def fake_send(msg, user_id="local"):
        sent.append((msg, user_id))

    async def fake_warn(**kw):
        warnings.append(kw)

    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send", fake_send)
    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send_constraint_warning", fake_warn)
    return SimpleNamespace(sent=sent, warnings=warnings,
                           types=lambda: [m.get("type") for m, _ in sent])


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """send_with_retry's back-off sleeps become no-ops."""
    from backend.messaging import _shared
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(_shared.asyncio, "sleep", fake_sleep)
    return slept
