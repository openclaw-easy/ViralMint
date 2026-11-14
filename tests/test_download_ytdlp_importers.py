# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Which backend modules may import yt-dlp.

Downloads, probes and metadata reads go through backend/download/. The
modules below use yt-dlp for something else (searching, listing a channel). A new importer fails this test, so
adding one is a decision: either route it through backend/download/ or add
it here with the reason.
"""
import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"

ALLOWED = {
    "agents/scout.py": "yt-dlp search backends (ytsearch, bilisearch, …) for Scout",
    "services/channel_reader.py": "reads a channel's listing for channel analysis",
}


def _imports_ytdlp(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "yt_dlp" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "yt_dlp":
            return True
    return False


def test_only_known_modules_import_ytdlp_outside_the_download_package():
    importers = {
        str(p.relative_to(BACKEND)) for p in BACKEND.rglob("*.py")
        if "download" != p.relative_to(BACKEND).parts[0] and _imports_ytdlp(p)
    }
    assert importers == set(ALLOWED), (
        f"new: {sorted(importers - set(ALLOWED))}; gone (remove from ALLOWED): {sorted(set(ALLOWED) - importers)}"
    )
