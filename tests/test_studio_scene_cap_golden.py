# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""SCENE_MAX 18 → 40: a script that fit the old pacing packs EXACTLY as before.

tests/fixtures/studio_scene_cap_golden.json was written from studio_doc with
SCENE_MAX = 18 and is not regenerated: a failure here means an existing
script changed shape. A script up to 18 scenes × 130 spoken units keeps the
old 18-scene packing; a longer one gets more ~8 s scenes, up to SCENE_MAX,
instead of fewer, longer ones (or a refusal).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services import studio_doc as sd

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "studio_scene_cap_golden.json"

_WORDS = ("creators batch film reuse every clip three ways and let tools cut shorts "
          "while they sleep and grow channels fast").split()
_CJK = "冬天植物死掉往往不是因为冷而是因为干燥和缺少阳光所以要记得浇水"


def english(chars: int, words_per_sentence: int) -> str:
    """Deterministic prose of about `chars` characters (no RNG: the bytes are the contract)."""
    out, n, i = [], 0, 0
    while n < chars:
        s = " ".join(_WORDS[(i + j) % len(_WORDS)] for j in range(words_per_sentence)).capitalize() + "."
        out.append(s)
        n += len(s) + 1
        i += 1
    return " ".join(out)


def chinese(chars: int, per_sentence: int) -> str:
    out, n, i = [], 0, 0
    while n < chars:
        out.append("".join(_CJK[(i + j) % len(_CJK)] for j in range(per_sentence)) + "。")
        n += per_sentence + 1
        i += 1
    return "".join(out)


def cases() -> list[tuple[str, str]]:
    out = []
    for chars in (80, 300, 700, 1200, 1800, 2300, 2600, 3200, 4500, 7000):
        for wps in (4, 9, 18):
            out.append((f"en-{chars}-{wps}", english(chars, wps)))
    for chars in (60, 200, 400, 540, 800, 1500):
        for per in (8, 20):
            out.append((f"zh-{chars}-{per}", chinese(chars, per)))
    return out


def _units(script: str) -> float:
    return sum(sd.spoken_units(p) for s in sd.split_sentences(script) for p in sd._split_long(s))


@pytest.fixture(scope="module")
def golden() -> dict:
    return {r["name"]: r for r in json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]}


def test_every_case_is_in_the_fixture(golden):
    assert {name for name, _ in cases()} == set(golden)


@pytest.mark.parametrize("name,script", cases())
def test_a_script_within_the_old_pacing_packs_byte_identical(golden, name, script):
    if _units(script) > sd.SCENE_PACE_SCENES * sd.SCENE_TARGET_SOFT_MAX:
        pytest.skip("longer than the old pacing: allowed to change")
    assert sd.plan_lines(script) == golden[name]["lines"]


@pytest.mark.parametrize("name,script", cases())
def test_a_longer_script_gets_more_scenes_never_longer_ones(golden, name, script):
    if _units(script) <= sd.SCENE_PACE_SCENES * sd.SCENE_TARGET_SOFT_MAX:
        pytest.skip("within the old pacing")
    lines = sd.plan_lines(script)
    old = golden[name]["lines"]
    if len(lines) <= sd.SCENE_MAX:
        assert len(lines) >= min(len(old), sd.SCENE_PACE_SCENES)
    assert "".join(lines).replace(" ", "") == "".join(old).replace(" ", "")      # nothing dropped or added


def test_the_cases_cover_both_sides_of_the_line():
    over = [n for n, s in cases() if _units(s) > sd.SCENE_PACE_SCENES * sd.SCENE_TARGET_SOFT_MAX]
    under = [n for n, s in cases() if _units(s) <= sd.SCENE_PACE_SCENES * sd.SCENE_TARGET_SOFT_MAX]
    assert len(over) >= 5 and len(under) >= 20
    assert any(len(sd.plan_lines(s)) > 18 for n, s in cases() if n in over)


def test_the_editors_packing_fixture_matches_studio_doc():
    """frontend/…/__fixtures__/scenePacking.fixture.json is what studioModel.planLines
    is checked against (vitest); it must be what studio_doc builds, so the JS port
    and the server can't drift apart."""
    path = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "components" / "studio"
            / "__fixtures__" / "scenePacking.fixture.json")
    for case in json.loads(path.read_text(encoding="utf-8"))["plans"]:
        assert sd.plan_lines(case["script"]) == case["lines"], case["script"][:60]
