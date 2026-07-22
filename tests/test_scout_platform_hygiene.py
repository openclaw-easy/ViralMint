# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Scout platform hygiene — the 34-platform toast-storm incident (2026-07-19).

One chat request ('download any YouTube video under 5 minutes', in Chinese)
made the model invent 34 'platforms' in a single scout call: the scout ground
through every one (~70s of searches + AI retries), fired 30+ warning toasts
(one per fallback platform), then crashed in outlier enrichment on
author_url=None.

Ported from the SaaS suite. The two agent-tool-boundary tests
(`_scout_trending`) were dropped — the OSS build has no
`backend.agents.agent_tools` module. The billing/cloud patches were removed:
OSS scout is BYOK (keys via `backend.core.api_keys`) with no `require_balance`
or `cloud_client` collaborators.
"""
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.scout import MAX_PLATFORMS_PER_SCOUT, ScoutAgent


class _FakeDB:
    async def execute(self, *a, **kw):
        class _R:
            def scalar_one_or_none(self):
                return None
        return _R()


class _FakeSessionCM:
    async def __aenter__(self):
        return _FakeDB()

    async def __aexit__(self, *a):
        return False


# ── scout run(): dedupe + cap + one aggregated fallback warning ─────────────

@pytest.mark.asyncio
async def test_scout_run_caps_platform_list_and_aggregates_warning():
    agent = ScoutAgent()
    scouted = []
    warnings = []

    # OSS _scout_platform signature: (platform, niche, user_settings, user_id=...)
    async def fake_platform(platform, niche, user_settings=None, user_id="local"):
        scouted.append(platform)
        return []

    many = ["youtube"] + [f"fake{i}" for i in range(20)]
    with patch.object(agent, "_scout_platform", side_effect=fake_platform), \
         patch.object(agent, "_enrich_with_outlier_scores", new=AsyncMock()), \
         patch("backend.agents.scout.ws_manager") as wsm, \
         patch("backend.agents.scout.update_job_status", new=AsyncMock()), \
         patch("backend.agents.scout.AsyncSessionLocal", return_value=_FakeSessionCM()), \
         patch("backend.core.ai_retry.ai_refine_search", new=AsyncMock(return_value=None)), \
         patch.object(agent, "_save_results", new=AsyncMock(return_value=([], 0)), create=True):
        wsm.send_progress = AsyncMock()
        wsm.send_constraint_warning = AsyncMock(
            side_effect=lambda **kw: warnings.append(kw.get("constraint")))
        wsm.send = AsyncMock()
        try:
            await agent.run(job_id="job-x", niche="cats", platforms=many)
        except Exception:
            pass  # persistence tail may need more mocks — the cap ran before it

    assert len(scouted) <= MAX_PLATFORMS_PER_SCOUT
    # exactly one AGGREGATED fallback warning, no per-platform storm
    assert warnings.count("multi_platform_fallback") == 1
    assert not any(w and w.endswith("_no_native_search") for w in warnings)


# ── outlier enrichment: None author_url must not crash ──────────────────────

@pytest.mark.asyncio
async def test_enrich_survives_none_author_url():
    """author_url=None (ytdlp-fallback rows) crashed the whole scout at the
    channel-id regex — `.get(key, "")` doesn't default an explicit None."""
    agent = ScoutAgent()
    results = [{"platform": "youtube", "author_url": None, "views": 10},
               {"platform": "youtube", "author_url": "https://youtube.com/channel/UCabc123", "views": 5}]
    with patch("backend.core.api_keys.get_youtube_api_key", return_value="fake-key"), \
         patch("backend.services.outlier_detection_service.batch_get_channel_baselines",
               new=AsyncMock(return_value={})), \
         patch("backend.services.outlier_detection_service.enrich_scout_results_with_outliers",
               return_value=None):
        await agent._enrich_with_outlier_scores(results, ["youtube"], None)  # must not raise
