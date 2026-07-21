# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
import logging
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import event, text
from backend.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args={"check_same_thread": False},  # SQLite only
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Enable WAL mode for concurrent reads + faster writes."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")   # Wait up to 5s on lock contention
    cursor.execute("PRAGMA cache_size=-64000")   # 64MB cache
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """Create all tables. Called once at startup from run.py."""
    # Import all models so Base knows about them
    from backend.models import (  # noqa: F401
        user_settings, user_behavior, feature_flag,
        job, scout_result, downloaded_video, generated_video,
        messaging_config, chat_session, user_profile,
        video_metrics, viral_formula,
        connected_channel, dynamic_template, caption_style,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent column additions for SQLite (no Alembic)
        await _add_column_if_missing(conn, "downloaded_videos", "transcript_segments_json", "TEXT")
        await _add_column_if_missing(conn, "generated_videos", "source_type", "VARCHAR(30)")
        # Clip extraction fields
        await _add_column_if_missing(conn, "generated_videos", "clip_start_seconds", "FLOAT")
        await _add_column_if_missing(conn, "generated_videos", "clip_end_seconds", "FLOAT")
        await _add_column_if_missing(conn, "generated_videos", "clip_virality_score", "FLOAT")
        await _add_column_if_missing(conn, "generated_videos", "clip_hook_score", "FLOAT")
        await _add_column_if_missing(conn, "generated_videos", "clip_hook_type", "VARCHAR(30)")
        await _add_column_if_missing(conn, "generated_videos", "clip_virality_reason", "TEXT")
        await _add_column_if_missing(conn, "generated_videos", "clip_score_breakdown_json", "TEXT")
        await _add_column_if_missing(conn, "generated_videos", "caption_status", "VARCHAR(20)")
        await _add_column_if_missing(conn, "generated_videos", "metadata_status", "VARCHAR(20)")
        # BYOK: per-user encrypted keys (override .env at runtime)
        await _add_column_if_missing(conn, "user_settings", "ai_provider", "VARCHAR(20)")
        await _add_column_if_missing(conn, "user_settings", "ai_model", "VARCHAR(100)")
        await _add_column_if_missing(conn, "user_settings", "ai_api_key_encrypted", "TEXT")
        await _add_column_if_missing(conn, "user_settings", "youtube_api_key_encrypted", "TEXT")

    # Drift sentinel — compare every model's columns against the live DB
    # schema and log a loud warning for any column the model declares but
    # the DB lacks. This converts a future "endpoint 500" silent regression
    # (a model column added without a matching `_add_column_if_missing` line)
    # into a clear startup log line. Runs once, after migrations, before
    # zombie cleanup. Pure observability — never raises.
    await _warn_on_schema_drift()

    # Clean up zombie jobs — any jobs stuck at "running"/"pending" from a previous crash
    await _cleanup_zombie_jobs()


async def _warn_on_schema_drift():
    """Compare model schema vs live DB; log WARN for missing columns.

    Catches the class of regression where a column is added to a model
    but the matching `_add_column_if_missing` call is forgotten in
    init_db. Upgrade-install users hit a 500 on the first endpoint that
    SELECTs the new column; fresh installs work because the table is
    created from scratch. This sentinel makes the drift visible at
    startup so the next regression is caught before users file tickets.
    """
    try:
        async with engine.connect() as conn:
            for table_name, mapper in Base.metadata.tables.items():
                try:
                    rows = await conn.exec_driver_sql(f"PRAGMA table_info({table_name})")
                    db_cols = {r[1] for r in rows.fetchall()}
                except Exception:
                    # Table doesn't exist yet — create_all just made it, no drift possible.
                    continue
                if not db_cols:
                    continue
                model_cols = {c.name for c in mapper.columns}
                missing = model_cols - db_cols
                if missing:
                    logger.warning(
                        "Schema drift detected on table %r: model declares columns "
                        "missing from DB: %s. Add `_add_column_if_missing(conn, %r, ...)` "
                        "lines for each in backend/database.py init_db().",
                        table_name, sorted(missing), table_name,
                    )
    except Exception as e:
        logger.warning("Schema-drift sentinel failed (non-fatal): %s", e)


async def _cleanup_zombie_jobs():
    """Mark jobs stuck at running/pending as failed — they can't recover after restart."""
    try:
        async with AsyncSessionLocal() as db:
            from backend.models.job import Job
            from sqlalchemy import update
            result = await db.execute(
                update(Job)
                .where(Job.status.in_(["running", "pending"]))
                .values(status="failed", error_message="Server restarted — job did not complete")
            )
            if result.rowcount > 0:
                logger.warning(f"Marked {result.rowcount} zombie jobs as failed from previous session")
            await db.commit()
    except Exception as e:
        logger.warning(f"Zombie job cleanup failed: {e}")


async def _add_column_if_missing(conn, table: str, column: str, col_type: str):
    """SQLite-safe column addition — no-op if already exists."""
    try:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
    except Exception:
        pass  # Column already exists — expected for idempotent migrations
