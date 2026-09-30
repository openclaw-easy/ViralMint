# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""No yt-dlp work may run on the shared default executor.

`asyncio.to_thread` hands every call to ONE process-wide pool, and
`asyncio.wait_for` cannot cancel a thread — so a yt-dlp call that never
returns keeps its worker forever. Enough of those and every request in the
app that touches `to_thread` hangs until a restart. yt-dlp belongs on
`executors.download_pool`, where a wedged call can only ever hold a
download worker.

This walks every backend module and finds each nested function that runs
yt-dlp (imports it, builds a YoutubeDL, or calls a module-level helper that
does), then fails if any of them is handed to `asyncio.to_thread`.
"""
import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"


def _runs_ytdlp(fn: ast.AST, helpers: set[str]) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Import) and any(a.name == "yt_dlp" for a in node.names):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "YoutubeDL":
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in helpers:
            return True
    return False


def _offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    # Module-level sync helpers that run yt-dlp themselves.
    helpers = {
        n.name for n in tree.body
        if isinstance(n, ast.FunctionDef) and _runs_ytdlp(n, set())
    }
    found = []
    for outer in ast.walk(tree):
        if not isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local_ytdlp = {
            n.name for n in outer.body
            if isinstance(n, ast.FunctionDef) and _runs_ytdlp(n, helpers)
        } | helpers
        for call in ast.walk(outer):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "to_thread"
                    and call.args
                    and isinstance(call.args[0], ast.Name)
                    and call.args[0].id in local_ytdlp):
                found.append(f"{path.relative_to(BACKEND.parent)}:{call.lineno} "
                             f"to_thread({call.args[0].id})")
    return found


def test_no_ytdlp_call_runs_on_the_default_executor():
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        offenders += _offenders(path)
    assert not offenders, (
        "yt-dlp work handed to asyncio.to_thread — use "
        "backend.core.executors.download_pool.run instead:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_actually_detects_the_pattern(tmp_path):
    """The guard must fail on the shape it exists to forbid."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import asyncio\n"
        "async def f():\n"
        "    def _search():\n"
        "        import yt_dlp\n"
        "        return yt_dlp\n"
        "    return await asyncio.to_thread(_search)\n"
    )
    assert _offenders_rel(bad)


def _offenders_rel(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    for outer in ast.walk(tree):
        if isinstance(outer, ast.AsyncFunctionDef):
            names = {n.name for n in outer.body
                     if isinstance(n, ast.FunctionDef) and _runs_ytdlp(n, set())}
            return [c for c in ast.walk(outer)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "to_thread" and c.args
                    and isinstance(c.args[0], ast.Name) and c.args[0].id in names]
    return []
