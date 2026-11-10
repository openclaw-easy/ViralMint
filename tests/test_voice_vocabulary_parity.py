# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A picker must offer voices the ENGINE it calls will accept.

This is the "derive, don't duplicate" rule pointed at the voice surface, and
it is here because both hand-typed lists were wrong in a way nothing could
catch:

  - the Voice-over page offered Kore / Puck / Charon / Leda / Orus under
    "OpenAI TTS". Those are Gemini names; OpenAI's speech API has never had
    them.
  - the Translate page offered the same Gemini names for its dub — and the dub
    runs on **Edge TTS**, which answers `ValueError: Invalid voice 'Kore'`. So
    full-dub failed for every voice the page offered, whichever one you picked.

A fallback list is a CLAIM about a real vocabulary that nobody looks at until
it is the only thing left, which is exactly when it has to be right.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src"


def _voice_ids(text: str, const: str) -> set[str]:
    """The voice ids inside one `const NAME = [...]` array literal."""
    start = text.index(f"const {const} = [")
    body = text[start:text.index("\n]", start)]
    ids = set(re.findall(r'voice_id:\s*"([^"]+)"', body))
    ids |= set(re.findall(r'id:\s*"([^"]+)"', body))
    return ids


# ── Edge TTS ────────────────────────────────────────────────────────────────


def _edge_recommended() -> dict[str, str]:
    from backend.services.edge_tts_service import RECOMMENDED_VOICES
    return RECOMMENDED_VOICES


@pytest.mark.parametrize("page,const", [
    ("pages/tools/Voiceover.jsx", "FALLBACK_EDGE_VOICES"),
    ("pages/tools/Translate.jsx", "DUB_VOICES"),
])
def test_every_offered_edge_voice_exists_in_the_engine(page, const):
    offered = _voice_ids((FRONTEND / page).read_text(), const)
    real = set(_edge_recommended())
    assert offered, f"{const} parsed empty — the test, not the app, is broken"
    assert offered <= real, (
        f"{page} offers Edge voices the engine will reject: {sorted(offered - real)}"
    )


@pytest.mark.parametrize("page,const", [
    ("pages/tools/Voiceover.jsx", "FALLBACK_EDGE_VOICES"),
    ("pages/tools/Translate.jsx", "DUB_VOICES"),
])
def test_the_offered_edge_names_match_the_engines_own(page, const):
    """A name that differs makes the voice RENAME ITSELF under the user the
    moment the live list loads. Four of six had drifted."""
    text = (FRONTEND / page).read_text()
    start = text.index(f"const {const} = [")
    body = text[start:text.index("\n]", start)]
    real = _edge_recommended()

    rows = re.findall(r'(?:voice_id|id):\s*"([^"]+)"[^\n]*?(?:name|label):\s*"([^"]+)"', body)
    assert rows, f"{const} rows parsed empty"
    for vid, shown in rows:
        engine = real[vid]
        assert engine in shown, (
            f'{page}: {vid} is shown as "{shown}" but the engine calls it "{engine}"'
        )


# ── OpenAI TTS ──────────────────────────────────────────────────────────────


def test_the_openai_fallback_is_a_real_subset():
    """The Voice-over page's paid-provider fallback used to name Gemini voices,
    which OpenAI rejects — and it is the ONLY list shown whenever
    /api/config/voices is unreachable."""
    import asyncio

    from backend.services.tts_service import TTSProvider, list_voices

    real = {v["voice_id"] for v in asyncio.run(list_voices(TTSProvider.OPENAI_TTS))}
    offered = _voice_ids(
        (FRONTEND / "pages/tools/Voiceover.jsx").read_text(), "FALLBACK_OPENAI_VOICES")
    assert offered, "FALLBACK_OPENAI_VOICES parsed empty"
    assert offered <= real, (
        f"offline OpenAI voices the API will reject: {sorted(offered - real)}"
    )


# ── the route both pages have always called ─────────────────────────────────


def test_the_voices_route_exists():
    """Both pickers GET /api/config/voices/<provider> on mount. The route did
    not exist, so the fallback above was not a fallback — it was the only list
    anyone ever saw, on every load."""
    import asyncio

    from backend.api.config import get_voices

    for provider in ("edge_tts", "openai_tts"):
        payload = asyncio.run(get_voices(provider))
        assert payload["provider"] == provider
        assert isinstance(payload["voices"], list)


def test_an_unknown_provider_is_a_400_not_a_crash():
    import asyncio

    from fastapi import HTTPException

    from backend.api.config import get_voices

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_voices("not_a_provider"))
    assert exc.value.status_code == 400


def test_the_preview_route_is_mounted():
    """Every preview click answered 404 and showed "Preview failed"."""
    from fastapi.testclient import TestClient

    from backend.main import create_app

    with TestClient(create_app()) as client:
        paths = set(client.app.openapi()["paths"])
    assert "/api/tts/preview" in paths
    assert "/api/config/voices/{provider}" in paths


# ── the runner honours the provider it is handed ────────────────────────────


@pytest.mark.asyncio
async def test_the_voiceover_runner_routes_to_the_provider_it_was_given(monkeypatch, tmp_path):
    """The page has always sent `provider`; the endpoint didn't accept it and
    the runner called Edge unconditionally. So choosing OpenAI handed Edge an
    id like "alloy", which edge_tts rejects, and the job just failed."""
    from backend.core import tool_runners
    from backend.services.tts_service import TTSProvider

    seen = {}

    async def fake_generate_tts(text, provider=None, voice_id=None, api_key="", output_path=None, **_progress):
        seen.update(provider=provider, voice_id=voice_id, api_key=api_key)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\xff\xfb" + b"\0" * 4096)
        return output_path

    monkeypatch.setattr("backend.services.tts_service.generate_tts", fake_generate_tts)
    monkeypatch.setattr("backend.config.settings.OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(tool_runners, "_tool_progress", _noop)
    monkeypatch.setattr(tool_runners, "_tool_success", _noop)
    monkeypatch.setattr(tool_runners, "_tool_fail", _raise)

    await tool_runners.run_tool_voiceover(
        "job-openai", "hello there", "alloy", provider="openai_tts")
    assert seen["provider"] == TTSProvider.OPENAI_TTS
    assert seen["voice_id"] == "alloy"
    assert seen["api_key"] == "sk-test"


@pytest.mark.asyncio
async def test_a_paid_provider_without_a_key_degrades_loudly_to_edge(monkeypatch):
    """Degrade, but never silently: a narration in a voice the user did not
    pick has to say so. And the OpenAI voice id must NOT ride along — Edge
    would reject it and the fallback would fail too."""
    from backend.core import tool_runners
    from backend.services.tts_service import TTSProvider

    seen, warnings = {}, []

    async def fake_generate_tts(text, provider=None, voice_id=None, api_key="", output_path=None, **_progress):
        seen.update(provider=provider, voice_id=voice_id)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\xff\xfb" + b"\0" * 4096)
        return output_path

    async def fake_warn(**kw):
        warnings.append(kw)

    monkeypatch.setattr("backend.services.tts_service.generate_tts", fake_generate_tts)
    monkeypatch.setattr("backend.config.settings.OPENAI_API_KEY", "")
    monkeypatch.setattr("backend.core.ws_manager.ws_manager.send_constraint_warning", fake_warn)
    monkeypatch.setattr(tool_runners, "_tool_progress", _noop)
    monkeypatch.setattr(tool_runners, "_tool_success", _noop)
    monkeypatch.setattr(tool_runners, "_tool_fail", _raise)

    await tool_runners.run_tool_voiceover(
        "job-nokey", "hello there", "alloy", provider="openai_tts")

    assert seen["provider"] == TTSProvider.EDGE_TTS
    assert seen["voice_id"] != "alloy", "an OpenAI voice id must not reach Edge TTS"
    assert warnings and warnings[0]["constraint"] == "tts_fallback"


@pytest.mark.asyncio
async def test_an_unknown_provider_falls_back_rather_than_raising(monkeypatch):
    from backend.core import tool_runners
    from backend.services.tts_service import TTSProvider

    seen = {}

    async def fake_generate_tts(text, provider=None, voice_id=None, api_key="", output_path=None, **_progress):
        seen["provider"] = provider
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\xff\xfb" + b"\0" * 4096)
        return output_path

    monkeypatch.setattr("backend.services.tts_service.generate_tts", fake_generate_tts)
    monkeypatch.setattr(tool_runners, "_tool_progress", _noop)
    monkeypatch.setattr(tool_runners, "_tool_success", _noop)
    monkeypatch.setattr(tool_runners, "_tool_fail", _raise)

    await tool_runners.run_tool_voiceover(
        "job-weird", "hello", "", provider="gemini_tts")
    assert seen["provider"] == TTSProvider.EDGE_TTS


async def _noop(*a, **kw):
    return True


async def _raise(job_id, exc, *a, **kw):
    raise AssertionError(f"runner failed: {exc}")


def test_the_preview_scratch_dir_is_bounded(tmp_path):
    """Each click writes a fresh mp3 on purpose (fresh sample text), so the
    directory grows forever unless something owns it. TMP_DIR must never be
    blanket-purged — it holds the cookie jar and uploaded media — so the
    preview route prunes its OWN directory and nothing else."""
    from backend.api.tts_preview import _KEEP_PREVIEWS, _prune_previews

    keep_me = tmp_path / "cookies.txt"
    keep_me.write_text("not an mp3")
    for i in range(_KEEP_PREVIEWS + 15):
        f = tmp_path / f"edge_tts_v{i}_{i:04d}.mp3"
        f.write_bytes(b"\xff\xfb")
        import os
        os.utime(f, (i, i))          # oldest first

    _prune_previews(tmp_path)

    left = sorted(tmp_path.glob("*.mp3"))
    assert len(left) == _KEEP_PREVIEWS
    assert keep_me.exists(), "the prune must only touch its own mp3s"
    # The newest survive, not an arbitrary subset.
    assert any(f"v{_KEEP_PREVIEWS + 14}_" in f.name for f in left)


def test_pruning_a_missing_dir_is_not_an_error(tmp_path):
    """It runs on the request path; a raise here would turn a working preview
    into a 500."""
    from backend.api.tts_preview import _prune_previews
    _prune_previews(tmp_path / "does-not-exist")
