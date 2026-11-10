#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Fail the desktop build when PyInstaller says part of our backend can't load in the frozen app.

PyInstaller only WARNS, and the app degrades quietly: a module with syntax the
build's Python can't compile (the build is on 3.11, dev often newer), or a
third-party package we import that isn't in requirements.txt (present in dev
venvs only as somebody else's dependency), is dropped without a word. Dev and
pytest see none of it.

Fails on:
  invalid module named backend.*                     our own file doesn't compile under the build's Python
  missing module named <top> - imported by backend.* a third-party package we import isn't in a clean install

Dotted names (pydantic.BaseModel) and the standard library (fcntl on Windows, msvcrt on Unix) are PyInstaller noise.

Usage: python desktop/scripts/check_frozen_warnings.py build/viralmint/warn-viralmint.txt
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LINE = re.compile(r"^(missing|invalid) module named (\S+) - imported by (.*)$")
OURS = re.compile(r"(^|[\s,])backend[.\s(]")


def problems(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        m = LINE.match(raw.strip())
        if not m:
            continue
        kind, name, importers = m.groups()
        name = name.strip("'\"")
        if kind == "invalid":
            if name == "backend" or name.startswith("backend."):
                out.append(raw.strip())
        elif "." not in name and name not in sys.stdlib_module_names and OURS.search(importers):
            out.append(raw.strip())
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"ERROR: {path} not found: PyInstaller's layout changed, so this gate is blind. Fix the path.")
        return 2
    found = problems(path.read_text(encoding="utf-8", errors="replace"))
    for line in found:
        print(f"ERROR: {line}")
    if found:
        print("A frozen build would lose this code without a word: add the package to requirements.txt, or fix the syntax for the build's Python.")
        return 1
    print("OK: every backend module and third-party import made it into the bundle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
