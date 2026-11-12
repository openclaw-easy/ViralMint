# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Enhancing audio must improve it, and enhancing it twice must not ruin it.

Reported as "the chained output sounds rough". Measurement told the story: the
source was ALREADY normalized (the clip pipeline had enhanced it once), and
every further pass re-compressed what dynamics remained while the level stayed
put — LRA 1.4 → 1.0 → 0.8 across two runs, plus a lossy generation each time.

Two root causes:

1. Single-pass loudnorm runs in DYNAMIC mode. That is not a less accurate
   version of normalization — it is a different algorithm that continuously
   rides the gain (audible pumping) and crushes loudness range. Enhance is now
   two-pass: measure, then apply ONE fixed gain with linear=true.

   ffmpeg SILENTLY reverts linear=true to dynamic in two cases
   (af_loudnorm.c), and both are pre-empted in `_linear_filter`. Both traps
   are pinned below, deterministically, off the filter string.

2. Enhancing already-normalized audio is a harmful no-op. The runner measures
   first and passes the file through untouched with an explanatory warning.

These tests shell out to a real ffmpeg — they're skipped where it's absent.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.core.tool_runners import (
    AUDIO_DENOISE_FILTER,
    _is_already_normalized,
    _linear_filter,
    _measure_loudness,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="needs a real ffmpeg")


def _sine(path: Path, seconds: float = 3, volume: str = "0.05") -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=300:duration={seconds}",
         "-af", f"volume={volume}", str(path)],
        capture_output=True, check=True)
    return path


def _measure(path: Path) -> dict:
    m = asyncio.run(_measure_loudness(path))
    assert m is not None, f"could not measure {path}"
    return m


# ── pass 1: the measurement ─────────────────────────────────────────────────

class TestMeasurement:
    def test_it_parses_loudnorms_json_block(self, tmp_path):
        m = _measure(_sine(tmp_path / "a.wav"))
        for key in ("input_i", "input_tp", "input_lra", "input_thresh"):
            assert key in m
            float(m[key])

    def test_a_quiet_file_measures_quiet(self, tmp_path):
        assert float(_measure(_sine(tmp_path / "q.wav", volume="0.02"))["input_i"]) < -20

    def test_digital_silence_is_unusable_not_a_number(self, tmp_path):
        """loudnorm reports "-inf" on silence. That must read as "can't
        measure", never as a level to normalize against — note that
        float("-inf") PARSES, so a bare float() call is not the guard it
        looks like, and -inf reaching the linear filter yields a NaN target
        and an ffmpeg invocation the arg parser rejects."""
        silent = tmp_path / "s.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", "2", str(silent)], capture_output=True, check=True)
        assert asyncio.run(_measure_loudness(silent)) is None

    def test_an_unreadable_file_measures_none(self, tmp_path):
        junk = tmp_path / "j.mp3"
        junk.write_bytes(b"not audio at all")
        assert asyncio.run(_measure_loudness(junk)) is None

    def test_measuring_through_the_denoiser_is_a_different_number(self, tmp_path):
        """The apply pass MUST use this variant: loudnorm receives the
        denoised signal, so a raw measurement makes the linear gain wrong."""
        src = _sine(tmp_path / "d.wav")
        raw = asyncio.run(_measure_loudness(src, through_denoise=False))
        through = asyncio.run(_measure_loudness(src, through_denoise=True))
        assert raw and through
        assert float(raw["input_i"]) != float(through["input_i"])


# ── the skip decision ───────────────────────────────────────────────────────

class TestAlreadyNormalized:
    @pytest.mark.parametrize("i,tp,expected", [
        (-16.0, -1.5, True),    # dead on target
        (-16.9, -1.2, True),    # inside 1 LU
        (-17.1, -1.5, False),   # outside 1 LU (quiet)
        (-16.0, -1.0, True),    # peak exactly on the bound
        (-14.5, -1.5, False),   # outside 1 LU (hot)
        (-16.0, -0.4, False),   # at level but peaking — still worth limiting
    ])
    def test_boundaries(self, i, tp, expected):
        assert _is_already_normalized(
            {"input_i": str(i), "input_tp": str(tp)}) is expected

    def test_an_unmeasurable_file_is_not_already_done(self):
        """Conservative on purpose — "we don't know" must not skip the work."""
        assert _is_already_normalized(None) is False
        assert _is_already_normalized({"input_i": "-inf", "input_tp": "-1.5"}) is False
        assert _is_already_normalized({}) is False


# ── pass 2: the linear filter and ffmpeg's two revert traps ─────────────────

class TestLinearFilter:
    def test_it_asks_for_linear_mode_and_carries_the_measurement(self):
        f = _linear_filter({"input_i": "-24.0", "input_tp": "-6.0",
                            "input_lra": "5.0", "input_thresh": "-34.0"})
        assert "linear=true" in f
        assert "measured_I=-24.0" in f and "measured_TP=-6.0" in f
        assert f.startswith(AUDIO_DENOISE_FILTER)

    def test_a_high_lra_source_raises_the_lra_target(self):
        """Trap 1: with measured_LRA above the LRA target ffmpeg abandons
        linear mode. In linear mode LRA is ONLY that revert threshold, so
        raising it is free — and not raising it crushes the dynamics this
        whole change exists to preserve."""
        f = _linear_filter({"input_i": "-24.0", "input_tp": "-6.0",
                            "input_lra": "23.6", "input_thresh": "-34.0"})
        lra = float(f.split("LRA=")[1].split(":")[0])
        assert lra > 23.6

    def test_a_hot_peak_clamps_the_target_instead_of_reverting(self):
        """Trap 2: if the needed gain would push the measured true peak past
        TP, ffmpeg does NOT apply less gain — it flips to dynamic. Pre-clamp
        the target into the headroom: quieter-but-clean beats loud-and-pumping."""
        # -30 LUFS but peaking at -1.0: +14 LU of gain would put the peak at
        # +13 dBTP, far past the -1.5 target.
        f = _linear_filter({"input_i": "-30.0", "input_tp": "-1.0",
                            "input_lra": "4.0", "input_thresh": "-40.0"})
        target_i = float(f.split("loudnorm=I=")[1].split(":")[0])
        assert target_i < -16.0, "target must be pulled down into the headroom"
        # The peak is ALREADY above TP, so the headroom is negative and the
        # correct move is a slight ATTENUATION — one fixed gain, still linear.
        assert -31.0 < target_i < -30.0, target_i

    def test_a_headroom_rich_source_gets_the_real_target(self):
        f = _linear_filter({"input_i": "-24.0", "input_tp": "-12.0",
                            "input_lra": "4.0", "input_thresh": "-34.0"})
        assert float(f.split("loudnorm=I=")[1].split(":")[0]) == pytest.approx(-16.0)

    def test_a_malformed_measurement_does_not_raise(self):
        """This runs on the success path of a job — it must degrade, not throw."""
        assert "linear=true" in _linear_filter(
            {"input_i": "nope", "input_tp": "-6.0",
             "input_lra": "5.0", "input_thresh": "-34.0"})

    def test_the_target_stays_inside_loudnorms_accepted_range(self):
        """loudnorm rejects I outside [-70, -5] outright."""
        for tp in ("-0.1", "-40.0"):
            f = _linear_filter({"input_i": "-60.0", "input_tp": tp,
                                "input_lra": "4.0", "input_thresh": "-70.0"})
            assert -70.0 <= float(f.split("loudnorm=I=")[1].split(":")[0]) <= -5.0


# ── end to end, against real ffmpeg ─────────────────────────────────────────

class TestRealRender:
    def test_it_hits_the_target_without_reverting_to_dynamic(self, tmp_path):
        src = _sine(tmp_path / "in.wav")
        measured = asyncio.run(_measure_loudness(src, through_denoise=True))
        out = tmp_path / "out.wav"
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-af", _linear_filter(measured),
             str(out)],
            capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-500:]
        assert "not applying linear" not in r.stderr.lower(), (
            "ffmpeg silently reverted to dynamic mode — the pumping is back"
        )
        assert abs(float(_measure(out)["input_i"]) - (-16.0)) < 2.0

    def test_enhancing_an_enhanced_file_barely_moves_it(self, tmp_path):
        """The whole point. Under dynamic single-pass, each repeat pass ate
        loudness range while the level stayed put."""
        src = _sine(tmp_path / "in.wav")
        first = tmp_path / "one.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-af",
             _linear_filter(asyncio.run(_measure_loudness(src, through_denoise=True))),
             str(first)], capture_output=True, check=True, timeout=300)

        m1 = _measure(first)
        # The runner would now SKIP — that's the real protection.
        assert _is_already_normalized(m1), (
            f"an enhanced file must read as already-normalized, got "
            f"{m1['input_i']} LUFS / {m1['input_tp']} dBTP"
        )


# ── A quiet voice must not come out muffled (2026-11-12) ─────────────────────
# Ported from the hosted repo's "under the water" report. afftdn without noise
# tracking assumes a fixed -50 dB floor, so on a quiet recording the voice's
# own high end sits under that floor and is removed: speech-like audio lost
# 2.8 dB of its 2-8 kHz band at -35 dB and 7 dB at -40 dB. `tn=1` tracks the
# real floor and keeps the voice.

def _speechlike(path: Path, gain_db: float) -> Path:
    """Harmonic 'syllables' (180 Hz-5.2 kHz, 3 Hz envelope) over pink noise."""
    voice = ("aevalsrc='(0.5+0.5*sin(2*PI*3*t))*(sin(2*PI*180*t)+0.6*sin(2*PI*360*t)"
             "+0.4*sin(2*PI*1100*t)+0.3*sin(2*PI*2500*t)+0.25*sin(2*PI*3800*t)"
             "+0.2*sin(2*PI*5200*t))*0.15':s=48000:d=6")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", voice,
         "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.01:sample_rate=48000:duration=6",
         "-filter_complex", f"[0:a][1:a]amix=inputs=2:normalize=0,volume={gain_db}dB",
         "-ac", "1", str(path)],
        capture_output=True, check=True)
    return path


def _treble_share_db(path: Path) -> float:
    """2-8 kHz RMS minus full-band RMS (dB)."""
    def rms(af: str) -> float:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af",
                            f"{af}astats=measure_overall=RMS_level", "-f", "null", "-"],
                           capture_output=True, text=True)
        line = next(l for l in r.stderr.splitlines() if "RMS level dB" in l)
        return float(line.rsplit(" ", 1)[-1])
    return rms("highpass=f=2000,lowpass=f=8000,") - rms("")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("gain_db", [0, -35, -40])
def test_the_denoiser_keeps_a_quiet_voice_treble(tmp_path, gain_db):
    src = _speechlike(tmp_path / "in.wav", gain_db)
    out = tmp_path / "out.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(src), "-af", AUDIO_DENOISE_FILTER, str(out)],
                   capture_output=True, check=True)
    before, after = _treble_share_db(src), _treble_share_db(out)
    assert after >= before - 2.0, f"treble lost at {gain_db} dB: {before:.1f} -> {after:.1f} (muffled)"
