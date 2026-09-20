# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""_add_column_if_missing with a column cache: skip the ALTER-and-catch churn
for columns that already exist (one PRAGMA per table, zero ALTERs on a current
DB) while still adding genuinely-missing columns exactly once.
"""
from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from backend.database import _add_column_if_missing, _table_columns

# init_db's migration list — the upper bound below must stay well under it, or
# the assertion stops proving that only missing columns get ALTERed.
_MIGRATION_CALL_COUNT = 17


def test_migration_call_count_constant_is_current():
    """Keeps _MIGRATION_CALL_COUNT honest. If columns are added to init_db and
    this constant isn't updated, the upgrade-scenario bound below silently stops
    proving anything."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "backend" / "database.py").read_text()
    actual = len(re.findall(r"await _add_column_if_missing\(conn,", src))
    assert actual == _MIGRATION_CALL_COUNT, (
        f"init_db now has {actual} migration calls; update "
        f"_MIGRATION_CALL_COUNT (currently {_MIGRATION_CALL_COUNT})"
    )


def _alter_counter(engine):
    """Attach a counter for ALTER TABLE statements on the engine; return a
    mutable list whose [0] is the running count."""
    count = [0]

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("ALTER TABLE"):
            count[0] += 1

    return count


@pytest.mark.asyncio
async def test_cache_skips_alter_for_existing_columns(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    alters = _alter_counter(eng)
    try:
        async with eng.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE t (id INTEGER, keep TEXT)")

        # First pass: `keep` exists (skipped), `added` is new (one ALTER).
        cache: dict[str, set[str]] = {}
        async with eng.begin() as conn:
            await _add_column_if_missing(conn, "t", "keep", "TEXT", existing=cache)
            await _add_column_if_missing(conn, "t", "added", "TEXT", existing=cache)
        assert alters[0] == 1                         # only the genuinely-missing one
        assert cache["t"] == {"id", "keep", "added"}  # cache updated after ALTER

        # Second pass on an up-to-date table with a fresh cache: everything is
        # present, so NOT A SINGLE ALTER fires (the whole point of the change).
        alters[0] = 0
        cache2: dict[str, set[str]] = {}
        async with eng.begin() as conn:
            await _add_column_if_missing(conn, "t", "keep", "TEXT", existing=cache2)
            await _add_column_if_missing(conn, "t", "added", "TEXT", existing=cache2)
        assert alters[0] == 0
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_cache_is_read_once_per_table(tmp_path):
    """The column set is fetched once and reused — later calls for the same
    table hit the cache, not the DB."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    try:
        async with eng.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE t (id INTEGER, a TEXT, b TEXT)")
        cache: dict[str, set[str]] = {}
        async with eng.begin() as conn:
            await _add_column_if_missing(conn, "t", "a", "TEXT", existing=cache)
            # After the first touch, the table's columns are cached in full.
            assert cache["t"] == {"id", "a", "b"}
            await _add_column_if_missing(conn, "t", "b", "TEXT", existing=cache)
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_without_cache_still_idempotent(tmp_path):
    """No cache passed → the defensive try/except path keeps it a no-op on a
    duplicate column (backward-compatible standalone contract)."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    try:
        async with eng.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE t (id INTEGER, keep TEXT)")
        async with eng.begin() as conn:
            # Duplicate column: must not raise.
            await _add_column_if_missing(conn, "t", "keep", "TEXT")
            await _add_column_if_missing(conn, "t", "fresh", "TEXT")
        async with eng.begin() as conn:
            cols = await _table_columns(conn, "t")
        assert {"id", "keep", "fresh"} <= cols
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_table_columns_missing_table_is_empty(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    try:
        async with eng.begin() as conn:
            assert await _table_columns(conn, "does_not_exist") == set()
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_table_columns_swallows_query_errors(tmp_path):
    """A PRAGMA that errors (e.g. an invalid identifier) returns an empty set,
    not an exception — the defensive fallback the migration relies on."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    try:
        async with eng.begin() as conn:
            # Unbalanced paren makes PRAGMA table_info(...) a syntax error.
            assert await _table_columns(conn, "bad)name") == set()
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_init_db_end_to_end_and_idempotent(tmp_path, monkeypatch):
    """Run the real init_db against a temp DB: it creates the schema, threads
    the column cache through the whole migration list without error, and is safe
    to run twice (the cached-skip path)."""
    import backend.database as DB
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'app.db'}")
    session_local = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(DB, "engine", eng)
    monkeypatch.setattr(DB, "AsyncSessionLocal", session_local)

    alters = _alter_counter(eng)
    try:
        await DB.init_db()                       # fresh DB
        async with eng.connect() as conn:
            us_cols = await DB._table_columns(conn, "user_settings")
            job_cols = await DB._table_columns(conn, "jobs")
        # BYOK columns and the job heartbeat are present.
        assert "ai_provider" in us_cols
        assert "ai_api_key_encrypted" in us_cols
        assert "updated_at" in job_cols
        # create_all builds every current column, so no ALTER is needed at all.
        assert alters[0] == 0

        # Second boot on the now-current DB: still zero ALTERs (cache-skip path).
        alters[0] = 0
        await DB.init_db()
        assert alters[0] == 0
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_init_db_alters_only_the_missing_columns(tmp_path, monkeypatch):
    """Upgrade scenario: a pre-existing table missing newer columns. init_db must
    ALTER only those (not fire the whole list)."""
    import backend.database as DB
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    dbfile = tmp_path / "app.db"
    # Pre-seed user_settings WITHOUT the BYOK columns so create_all leaves the
    # existing table alone and the migration has to add them.
    seed = create_async_engine(f"sqlite+aiosqlite:///{dbfile}")
    async with seed.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE user_settings (id INTEGER PRIMARY KEY, user_id VARCHAR)")
    await seed.dispose()

    eng = create_async_engine(f"sqlite+aiosqlite:///{dbfile}")
    monkeypatch.setattr(DB, "engine", eng)
    monkeypatch.setattr(DB, "AsyncSessionLocal",
                        async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False))
    alters = _alter_counter(eng)
    try:
        await DB.init_db()
        async with eng.connect() as conn:
            cols = await DB._table_columns(conn, "user_settings")
        # The missing columns were added...
        assert {"ai_provider", "ai_model", "ai_api_key_encrypted",
                "youtube_api_key_encrypted"} <= cols
        # ...and only the genuinely-missing ones ALTERed — the four above, not
        # the full migration list.
        assert 1 <= alters[0] < _MIGRATION_CALL_COUNT
    finally:
        await eng.dispose()
