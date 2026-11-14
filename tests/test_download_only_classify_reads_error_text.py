# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Only backend/download/classify.py may test for words inside error text.

The old download code matched error substrings in about 34 places, in three
different orders. In the rebuilt package a string-literal containment test
(`"http error 403" in something`) outside classify.py fails this test unless
it is listed in ALLOWED with the reason it is not error text.
"""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "backend" / "download"

# (file name, the string literal): why it is not a test on error text.
ALLOWED: dict[tuple[str, str], str] = {
    ("messages.py", "youtube"): "tests a URL's host, to say \"YouTube\"",
    ("outputs.py", ".auto."): "tests a subtitle FILE NAME for the auto-caption marker",
    ("progress.py", "%"): "tests an output stem for an unexpanded yt-dlp template",
    ("engine.py", "%"): "tests an output stem for an unexpanded yt-dlp template",
    ("engine.py", "cookiefile"): "tests the options dict for a cookie jar",
    ("info.py", "youtube.com"): "tests a URL, to normalise a channel link",
    ("info.py", "watch?v="): "tests a URL, to tell a single video from a channel",
    ("info.py", "youtu.be/"): "tests a URL, to tell a single video from a channel",
}


def _literal_containment_tests(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            continue
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            yield node.lineno, node.left.value


def test_no_error_text_matching_outside_classify():
    offenders = []
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "classify.py":
            continue
        for lineno, literal in _literal_containment_tests(ast.parse(path.read_text())):
            if (path.name, literal) not in ALLOWED:
                offenders.append(f"{path.name}:{lineno}  {literal!r} in …")
    assert not offenders, (
        "String-literal containment tests outside classify.py. If one reads error "
        "text, move it into classify(); if it does not, add it to ALLOWED with the "
        "reason:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_sees_a_containment_test():
    tree = ast.parse('if "http error 403" in str(e):\n    pass\nif key not in seen:\n    pass\n')
    assert list(_literal_containment_tests(tree)) == [(1, "http error 403")]
