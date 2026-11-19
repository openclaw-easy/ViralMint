# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""clip_extractor.py was split into clip_prompts, clip_windows and clip_caption_timing. Pins the two things that
keep it a no-op:

1. every moved name is still importable from clip_extractor and IS the moved object (callers and tests unchanged);
2. every name a test patches on clip_extractor is still DEFINED there. A patch replaces the module's binding, so a
   function moved out that calls a patched name would silently stop seeing the patch. Moving a patched name, or
   anything that reads one, fails here.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import backend.services.clip_extractor as ce
from backend.services import clip_caption_timing, clip_prompts, clip_windows

MOD = "backend.services.clip_extractor"
NEW = (clip_prompts, clip_windows, clip_caption_timing)
TESTS = Path(__file__).resolve().parent


def _top_names(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    return out - {"logger"}


def _patched_names() -> set[str]:
    names = set()
    for f in TESTS.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        aliases = {MOD}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                aliases |= {a.asname or a.name for a in n.names if a.name == MOD}
            elif isinstance(n, ast.ImportFrom) and n.module:
                aliases |= {a.asname or a.name for a in n.names if f"{n.module}.{a.name}" == MOD}
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not n.args:
                continue
            fn = ast.unparse(n.func)
            if not (fn.endswith("setattr") or fn.endswith("patch") or fn.endswith("patch.object")):
                continue
            first = n.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith(MOD + "."):
                names.add(first.value[len(MOD) + 1:].split(".")[0])
            elif (len(n.args) >= 2 and ast.unparse(first) in aliases
                  and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)):
                names.add(n.args[1].value)
    return names


def test_every_moved_name_is_re_exported_as_the_same_object():
    for module in NEW:
        for name in _top_names(module):
            assert getattr(ce, name) is getattr(module, name), f"{module.__name__}.{name} not re-exported as itself"


def test_the_new_modules_never_import_clip_extractor():
    for module in NEW:
        src = inspect.getsource(module)
        assert "clip_extractor import" not in src and "import backend.services.clip_extractor" not in src, module


def test_every_patched_name_still_lives_in_clip_extractor():
    patched = _patched_names()
    assert "_load_or_transcribe_segments" in patched      # the scan sees patches at all
    moved = set().union(*(_top_names(m) for m in NEW))
    assert not (patched & moved), f"moved names that tests patch on clip_extractor: {sorted(patched & moved)}"


def test_no_moved_function_reads_a_patched_name():
    patched = _patched_names()
    for module in NEW:
        tree = ast.parse(inspect.getsource(module))
        read = {x.id for x in ast.walk(tree) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)}
        assert not (read & patched), f"{module.__name__} reads {sorted(read & patched)}, which tests patch on clip_extractor"
