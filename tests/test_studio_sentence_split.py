# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""A script is cut into scenes at sentence ends, and only there: a "." inside a
number, a link or an ellipsis is not one. Each part used to be voiced as its own
scene ("$9." / "99")."""
from pathlib import Path

import pytest

from backend.services import studio_doc as sd

SPLIT_CASES = [
    ("It costs 3.5 dollars a day. The hive holds 60,000 bees.", ["It costs 3.5 dollars a day.", "The hive holds 60,000 bees."]),
    ("Wait... what? I paid $9.99, not $10. Go to example.com now!",
     ["Wait...", "what?", "I paid $9.99, not $10.", "Go to example.com now!"]),
    ("你好。世界！好吗？ OK.", ["你好。", "世界！", "好吗？", "OK."]),
    ("What happened here?! Nobody knows!!! Really?Yes.", ["What happened here?!", "Nobody knows!!!", "Really?", "Yes."]),
]


@pytest.mark.parametrize("script,parts", SPLIT_CASES)
def test_numbers_links_and_ellipses_are_never_cut_into_sentences(script, parts):
    assert sd.split_sentences(script) == parts


@pytest.mark.parametrize("line,title", [
    ("Honeybees live in colonies of up to 60,000 workers.", "Honeybees live in colonies of up to 60,000…"),
    ("It costs 3.5 dollars a day to keep a hive alive, and here is why.", "It costs 3.5 dollars a day to keep a hive alive"),
])
def test_the_opening_title_never_cuts_inside_a_number(line, title):
    assert sd.auto_title(line) == title


def test_the_js_splitter_uses_the_same_rule():
    src = (Path(__file__).resolve().parents[1] / "frontend/src/components/studio/studioModel.js").read_text()
    assert "(?<=[.!?])\\s+|(?<=[!?。！？])(?![.!?。！？])\\s*" in src
