# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for the scout `_save_results` display-payload contract.

The 2026-05-12 bug fix: previously, scout dedup dropped duplicate
results from the display payload. So a second-scout of the same
niche returned 50 raw results, 45 were dupes, and only 5 hit the UI.
Users thought scout was weak; in fact, they were just paying for
fresh data that was being silently filtered out.

The fix: every input result becomes a display row. Net-new inserts
get a fresh DB id; duplicates get the EXISTING DB row's id but with
the FRESH stats from this scout (views/likes/virality drift over
time — showing the snapshot value would feel broken).

These tests pin that contract.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_scout_result(video_id: str, platform: str, **overrides) -> dict:
    """Shape-compatible with what _scout_platform produces."""
    base = {
        "video_id": video_id,
        "platform": platform,
        "video_url": f"https://example.com/{video_id}",
        "title": f"Title {video_id}",
        "author": "Test Author",
        "views": 1000,
        "likes": 50,
        "comments": 5,
        "virality_score": 7.5,
    }
    base.update(overrides)
    return base


def _make_fake_session(existing_keys_to_ids: dict[tuple[str, str], str]):
    """Return a mock session that:
      - reports the given (video_id, platform) → id pairs as already
        in the table (for the dedup pre-fetch)
      - assigns new ids to anything inserted via db.add(sr); db.flush()
    """
    fetched_rows = [
        (id_, video_id, platform)
        for (video_id, platform), id_ in existing_keys_to_ids.items()
    ]

    fetched_result = MagicMock()
    fetched_result.fetchall = MagicMock(return_value=fetched_rows)

    next_id = [1000]

    def _fake_flush_side_effect():
        # New row's id assigned at flush time — simulate that.
        if fake_session._pending_row is not None:
            fake_session._pending_row.id = f"new-{next_id[0]}"
            next_id[0] += 1
            fake_session._pending_row = None

    def _fake_add(row):
        fake_session._pending_row = row

    fake_session = MagicMock()
    fake_session._pending_row = None
    fake_session.execute = AsyncMock(return_value=fetched_result)
    fake_session.add = MagicMock(side_effect=_fake_add)
    fake_session.flush = AsyncMock(side_effect=_fake_flush_side_effect)
    fake_session.commit = AsyncMock()

    class FakeCM:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *a):
            return False

    return FakeCM, fake_session


class TestSaveResultsDedupDisplay:
    """The post-2026-05-12 contract: dedup is STORAGE-only. Every
    input result appears in the display payload."""

    @pytest.mark.asyncio
    async def test_all_new_results_returned_with_fresh_ids(self):
        """First-time scout of a niche: no duplicates, all results new."""
        from backend.agents.scout import ScoutAgent

        cm, _session = _make_fake_session(existing_keys_to_ids={})

        with patch("backend.agents.scout.AsyncSessionLocal", return_value=cm()):
            results = [
                _fake_scout_result("a1", "youtube"),
                _fake_scout_result("b2", "youtube"),
                _fake_scout_result("c3", "tiktok"),
            ]
            display, new_count = await ScoutAgent()._save_results(
                results, job_id="job-1", niche="ai tools", user_id="local",
            )

        assert len(display) == 3
        assert new_count == 3
        # All ids assigned by the fake flush — none from the
        # (empty) existing map.
        for d in display:
            assert d["id"].startswith("new-")

    @pytest.mark.asyncio
    async def test_all_duplicates_still_returned_with_existing_ids(self):
        """Second-scout: every result is a duplicate. The bug was that
        these came back EMPTY. Now they come back with EXISTING ids."""
        from backend.agents.scout import ScoutAgent

        existing = {
            ("a1", "youtube"): "id-aaa",
            ("b2", "youtube"): "id-bbb",
            ("c3", "tiktok"): "id-ccc",
        }
        cm, session = _make_fake_session(existing_keys_to_ids=existing)

        with patch("backend.agents.scout.AsyncSessionLocal", return_value=cm()):
            results = [
                _fake_scout_result("a1", "youtube"),
                _fake_scout_result("b2", "youtube"),
                _fake_scout_result("c3", "tiktok"),
            ]
            display, new_count = await ScoutAgent()._save_results(
                results, job_id="job-2", niche="ai tools", user_id="local",
            )

        # Display payload covers ALL results — the bug fix.
        assert len(display) == 3
        # None were net-new inserts.
        assert new_count == 0
        # Each row carries the EXISTING DB id.
        assert display[0]["id"] == "id-aaa"
        assert display[1]["id"] == "id-bbb"
        assert display[2]["id"] == "id-ccc"
        # db.add was never called — no redundant DB writes.
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_new_and_duplicates(self):
        """Realistic case: most results dedupe, some are new."""
        from backend.agents.scout import ScoutAgent

        existing = {
            ("a1", "youtube"): "id-aaa",
            ("b2", "youtube"): "id-bbb",
        }
        cm, session = _make_fake_session(existing_keys_to_ids=existing)

        with patch("backend.agents.scout.AsyncSessionLocal", return_value=cm()):
            results = [
                _fake_scout_result("a1", "youtube"),         # dupe
                _fake_scout_result("brand-new", "youtube"),  # new
                _fake_scout_result("b2", "youtube"),         # dupe
                _fake_scout_result("c3", "tiktok"),          # new
            ]
            display, new_count = await ScoutAgent()._save_results(
                results, job_id="job-3", niche="ai tools", user_id="local",
            )

        assert len(display) == 4
        assert new_count == 2  # only 2 net-new inserts
        # Order preserved.
        assert display[0]["id"] == "id-aaa"
        assert display[1]["id"].startswith("new-")
        assert display[2]["id"] == "id-bbb"
        assert display[3]["id"].startswith("new-")
        # add called exactly twice (once per new row).
        assert session.add.call_count == 2

    @pytest.mark.asyncio
    async def test_fresh_stats_used_for_duplicates(self):
        """A duplicate's display row should carry the FRESH stats from
        this run (views/likes/virality drift over time), NOT the stale
        DB snapshot. Tests this by passing high-value stats on a key
        that already exists — we expect those high values in the output."""
        from backend.agents.scout import ScoutAgent

        existing = {("a1", "youtube"): "id-aaa"}
        cm, _session = _make_fake_session(existing_keys_to_ids=existing)

        with patch("backend.agents.scout.AsyncSessionLocal", return_value=cm()):
            # Fresh stats — much higher than whatever was originally
            # scouted.
            results = [
                _fake_scout_result(
                    "a1", "youtube",
                    views=999999, likes=50000, comments=2000,
                    virality_score=9.5,
                ),
            ]
            display, _new_count = await ScoutAgent()._save_results(
                results, job_id="job-4", niche="ai tools", user_id="local",
            )

        # Existing id preserved.
        assert display[0]["id"] == "id-aaa"
        # FRESH stats in the display payload.
        assert display[0]["views"] == 999999
        assert display[0]["likes"] == 50000
        assert display[0]["comments"] == 2000
        assert display[0]["virality_score"] == 9.5

    @pytest.mark.asyncio
    async def test_requested_platform_preserved_for_duplicates(self):
        """The `requested_platform` field (used for grouping in the WS
        dispatch) must survive the dedup path. Without it, cross-post
        results land in the wrong tab."""
        from backend.agents.scout import ScoutAgent

        existing = {("a1", "youtube"): "id-aaa"}
        cm, _session = _make_fake_session(existing_keys_to_ids=existing)

        with patch("backend.agents.scout.AsyncSessionLocal", return_value=cm()):
            results = [
                _fake_scout_result(
                    "a1", "youtube",
                    # User asked to scout "bilibili" but the URL is a
                    # YouTube cross-post — group under bilibili in the UI.
                    requested_platform="bilibili",
                ),
            ]
            display, _ = await ScoutAgent()._save_results(
                results, job_id="job-5", niche="ai tools", user_id="local",
            )

        assert display[0]["requested_platform"] == "bilibili"
        assert display[0]["platform"] == "youtube"  # truth of the URL

    @pytest.mark.asyncio
    async def test_empty_input_yields_empty_display(self):
        """Defensive: empty input list, no crashes, empty output."""
        from backend.agents.scout import ScoutAgent

        cm, _session = _make_fake_session(existing_keys_to_ids={})

        with patch("backend.agents.scout.AsyncSessionLocal", return_value=cm()):
            display, new_count = await ScoutAgent()._save_results(
                [], job_id="job-6", niche="x", user_id="local",
            )
        assert display == []
        assert new_count == 0
