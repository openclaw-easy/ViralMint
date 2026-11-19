# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The trend-velocity endpoint says WHY a reading is unknown, and the
all-niches form returns every reading (plus the rising ones as `alerts`)."""
from pathlib import Path

from fastapi.testclient import TestClient

from backend.services import trend_velocity_service as tv


def _client():
    from backend.main import app
    return TestClient(app, base_url="http://localhost:16899")


def test_a_keyword_lookup_carries_its_note(monkeypatch):
    async def look(keyword):
        return tv.TrendAlert(keyword, 0, 0, 1.0, "unknown", note="lookup_failed")
    monkeypatch.setattr(tv, "check_keyword_velocity", look)
    body = _client().get("/api/scout/trend-velocity", params={"keyword": "cooking"}).json()
    assert body["alert_level"] == "unknown" and body["note"] == "lookup_failed"


def test_the_all_niches_lookup_returns_readings_and_alerts(monkeypatch):
    async def readings(user_id="local"):
        return [tv.TrendAlert("a", 9, 1, 9.0, "spike"), tv.TrendAlert("b", 0, 0, 1.0, "unknown", note="no_data")]
    monkeypatch.setattr(tv, "check_user_keywords_velocity", readings)
    body = _client().get("/api/scout/trend-velocity").json()
    assert [r["keyword"] for r in body["readings"]] == ["a", "b"] and body["readings"][1]["note"] == "no_data"
    assert [r["keyword"] for r in body["alerts"]] == ["a"]


async def test_the_keyword_list_carries_each_note(monkeypatch):
    async def look(keyword):
        return tv.TrendAlert(keyword, 0, 0, 1.0, "unknown", note="lookup_failed") if keyword == "b" \
            else tv.TrendAlert(keyword, 9, 1, 9.0, "spike")
    monkeypatch.setattr(tv, "check_keyword_velocity", look)

    async def instant(_s):
        return None
    monkeypatch.setattr(tv.asyncio, "sleep", instant)
    out = await tv.get_trending_keywords(["a", "b"])
    assert {(r["keyword"], r["alert_level"], r["note"]) for r in out} == {("a", "spike", ""), ("b", "unknown", "lookup_failed")}


def test_the_mcp_tool_documents_unknown_and_both_notes():
    from backend.mcp.tools import intelligence
    src = Path(intelligence.__file__).read_text()
    for word in ('"unknown"', '"lookup_failed"', '"no_data"', '"readings"'):
        assert word in src, word
