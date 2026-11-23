# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Giving a render claim back frees this process's lock even when the row
write fails. The two API doors released the lock only after the write
returned, so a failing write left the project "already rendering" until a
restart."""
import asyncio

import pytest

from backend.database import init_db
from backend.services import studio_store as store

U = "abandon-render-test"


def test_the_lock_is_freed_even_when_the_row_write_fails(monkeypatch):
    asyncio.run(init_db())
    row = asyncio.run(store.create_project(script="Plants sleep in winter. Water less.", user_id=U))
    asyncio.run(store.claim_render(row.id, U))
    assert store.render_running_here(row.id)

    async def broken(*a, **kw):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(store, "finish_render", broken)
    with pytest.raises(RuntimeError):
        asyncio.run(store.abandon_render(row.id, U))
    assert not store.render_running_here(row.id)
    monkeypatch.undo()
    asyncio.run(store.abandon_render(row.id, U))
    assert asyncio.run(store.get_project(row.id, U)).status == "draft"
    asyncio.run(store.delete_project(row.id, U))
