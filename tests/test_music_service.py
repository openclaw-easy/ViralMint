# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend.services.music_service — background-music selection + mix.

select_music is pure filesystem + safety logic (user-picked track,
traversal rejection, genre glob, any-track fallback, Pixabay stub).
mix_audio shells out to FFmpeg — mocked so nothing spawns, with an assert
that the argv is a real list (not shell=True), and that a missing/absent
music track short-circuits to voice-only.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.services import music_service as ms
from backend.services.music_service import MUSIC_GENRES, PIXABAY_GENRE_QUERIES


@pytest.fixture
def music_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "MUSIC_DIR", tmp_path)
    return tmp_path


# ── static mappings ────────────────────────────────────────────────────────

def test_genre_query_maps_are_aligned():
    assert set(MUSIC_GENRES) == set(PIXABAY_GENRE_QUERIES)
    assert "lofi" in MUSIC_GENRES and MUSIC_GENRES["lofi"]


# ── select_music ───────────────────────────────────────────────────────────

async def test_user_picked_track_returned(music_dir):
    track = music_dir / "mytrack.mp3"
    track.write_bytes(b"data")
    out = await ms.select_music(track_filename="mytrack.mp3")
    assert out == track


async def test_traversal_track_filename_rejected(music_dir):
    assert await ms.select_music(track_filename="../etc/passwd") is None
    assert await ms.select_music(track_filename="sub/track.mp3") is None


async def test_missing_user_track_falls_back_to_genre(music_dir):
    bundled = music_dir / "lofi_beat.mp3"
    bundled.write_bytes(b"data")
    out = await ms.select_music(genre="lofi", track_filename="ghost.mp3")
    assert out == bundled


async def test_genre_glob_match(music_dir):
    (music_dir / "cinematic_epic.mp3").write_bytes(b"data")
    out = await ms.select_music(genre="cinematic")
    assert out.name == "cinematic_epic.mp3"


async def test_any_track_fallback_when_genre_missing(music_dir):
    other = music_dir / "random.wav"
    other.write_bytes(b"data")
    out = await ms.select_music(genre="edm")
    assert out == other


async def test_empty_file_ignored(music_dir):
    (music_dir / "lofi_empty.mp3").write_bytes(b"")  # zero size → skipped
    out = await ms.select_music(genre="lofi")
    assert out is None


async def test_no_tracks_returns_none(music_dir):
    out = await ms.select_music(genre="lofi")
    assert out is None


async def test_download_from_pixabay_is_stub_none():
    assert await ms._download_from_pixabay("lofi") is None


# ── mix_audio ──────────────────────────────────────────────────────────────

async def test_mix_no_music_returns_voice(tmp_path):
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"v")
    assert await ms.mix_audio(voice, None) == voice
    assert await ms.mix_audio(voice, tmp_path / "missing.mp3") == voice


async def test_mix_success_returns_output_and_argv_list(tmp_path):
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"v")
    music = tmp_path / "bg.mp3"
    music.write_bytes(b"m")
    with patch.object(ms, "probe_duration", return_value=30.0), \
         patch.object(ms.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stderr="")
        out = await ms.mix_audio(voice, music)
    assert out == voice.parent / "voice_mixed.mp3"
    cmd = run.call_args.args[0]
    assert isinstance(cmd, list) and cmd[0] == "ffmpeg"
    assert run.call_args.kwargs.get("shell") in (None, False)
    # normalize=0 keeps the voice at full level (the documented bed contract).
    assert any("normalize=0" in a for a in cmd)


async def test_mix_ffmpeg_failure_returns_voice(tmp_path):
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"v")
    music = tmp_path / "bg.mp3"
    music.write_bytes(b"m")
    with patch.object(ms, "probe_duration", return_value=30.0), \
         patch.object(ms.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=1, stderr="boom")
        out = await ms.mix_audio(voice, music)
    assert out == voice
