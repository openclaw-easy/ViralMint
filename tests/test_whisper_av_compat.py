# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""faster-whisper's audio decoder must work against the INSTALLED PyAV.

PyAV 19 removed `av.open(..., metadata_errors=)`; faster-whisper 1.2.1 still
passes it, so every transcription raised TypeError while the whole suite stayed
green — Whisper is mocked everywhere else. requirements.txt caps `av<19` for
this; this test is what notices if a resolve ever drifts past the cap or a
faster-whisper release changes the call. No model download: decode only.
"""
import struct
import wave

import pytest

faster_whisper_audio = pytest.importorskip("faster_whisper.audio")


def test_decode_audio_runs_on_installed_pyav(tmp_path):
    path = tmp_path / "tone.wav"
    rate = 16000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", (i % 64) * 256) for i in range(rate // 2)))

    samples = faster_whisper_audio.decode_audio(str(path), sampling_rate=rate)

    assert len(samples) == pytest.approx(rate // 2, abs=rate // 100)
