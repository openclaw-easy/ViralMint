#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Fail the desktop build when a runtime data file didn't reach the frozen app.

PyInstaller follows imports, not the data files a package reads at run time,
and the app then degrades without an error. Without trafilatura's settings.cfg,
justext's stoplists and tld's suffix list, article extraction fails on every
packaged build and silently falls back to raw <p> tags. Dev has them all.

One file per thing that can be lost, each one a file the running app really
reads. A package's own new data file doesn't need a row, but a new
collect_data_files in viralmint.spec does.

Usage: python desktop/scripts/check_frozen_data.py dist/ViralMint/_internal
"""
from __future__ import annotations

import sys
from pathlib import Path

REQUIRED = (
    "trafilatura/settings.cfg",
    "justext/stoplists/English.txt",
    "tld/res/effective_tld_names.dat.txt",
    "yt_dlp/extractor/youtube/jsc/_builtin/vendor/yt.solver.core.js",
    "backend/services/caption_fonts/NotoEmoji-Bold.ttf",
)


def missing(internal: Path) -> list[str]:
    """The required files that aren't there, or are empty."""
    def present(rel: str) -> bool:
        path = internal.joinpath(*rel.split("/"))
        return path.is_file() and path.stat().st_size > 0
    return [rel for rel in REQUIRED if not present(rel)]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    internal = Path(argv[1])
    lost = missing(internal)
    if lost:
        print(f"ERROR: the frozen app at {internal} is missing runtime data files:", file=sys.stderr)
        for rel in lost:
            print(f"  {rel}", file=sys.stderr)
        print("Add the package to collect_data_files in desktop/scripts/viralmint.spec.", file=sys.stderr)
        return 1
    print(f"OK: {len(REQUIRED)} runtime data files are in {internal}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
