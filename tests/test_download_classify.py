# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""classify(): every corpus wording gets its class.

A corpus row with a `changed` note is a reading that differs from the code
this package replaced (three classifier orders in ytdlp_service.py, deleted
2026-11-13). Until that deletion a second half of this file rebuilt the old
orders and failed on any undeclared difference; the notes are what it left.
"""
import json
from pathlib import Path

import pytest

from backend.download.classify import ErrorClass, classifiable_text, classify

CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "download_error_corpus.json").read_text()
)["rows"]
IDS = [r["label"] for r in CORPUS]


@pytest.mark.parametrize("row", CORPUS, ids=IDS)
def test_corpus_row_gets_its_class(row):
    assert classify(row["error"], row["url"]).value == row["expect"]


def test_corpus_labels_are_unique_and_every_class_has_a_row():
    assert len(set(IDS)) == len(IDS)
    covered = {r["expect"] for r in CORPUS}
    # OUTPUT_INVALID is typed, never read from text.
    assert covered == {c.value for c in ErrorClass} - {"output_invalid"}


def test_typed_error_carries_its_own_class():
    class Bad(Exception):
        error_class = ErrorClass.OUTPUT_INVALID

    assert classify(Bad("HTTP Error 403: Forbidden")) is ErrorClass.OUTPUT_INVALID


@pytest.mark.parametrize("junk", ["", "   ", None, 0, b"\xff", Exception(), ValueError("x" * 100_000)])
def test_classify_is_total(junk):
    assert classify(junk, "not a url") in set(ErrorClass)
    assert classify(junk, None) in set(ErrorClass)


def test_url_words_never_classify():
    url = "https://example.com/private-video/removed/403/429/captcha?geo-blocked=1"
    assert classifiable_text(f"ERROR: something odd at {url}", url) == "error: something odd at "
    assert classify(f"ERROR: something odd at {url}", url) is ErrorClass.UNKNOWN


def test_non_english_and_multiline_text_is_unknown_not_a_crash():
    assert classify("ERROR: 该视频不存在\n  File \"x.py\", line 1\nTraceback…", "https://b23.tv/x") is ErrorClass.UNKNOWN
