# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""What the desktop build must not lose quietly.

PyInstaller only WARNS when it can't take a module, and doesn't follow the data
files a package reads at run time; the packaged app then degrades without an
error while dev and pytest see nothing. The build runs on Python 3.11
(build-desktop.yml) and dev is often newer, so 3.12-only syntax passes every
dev run and PyInstaller then drops the module with a warning."""
from __future__ import annotations

import ast
import importlib.util
import sys
import tokenize
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "desktop" / "scripts"
FROZEN_PY = [*(REPO / "backend").rglob("*.py"), REPO / "launcher.py"]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pep701_only(path: Path) -> list[str]:
    """On 3.12+ the compiler accepts these, so read the tokens: inside an f-string's braces, a string opening with
    the f-string's own delimiter (a single quote mark inside a triple-quoted one is fine), or a backslash. Both are
    SyntaxErrors on 3.11."""
    found = []
    stack: list[str] = []
    with tokenize.open(path) as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            name = tokenize.tok_name[tok.type]
            if name == "FSTRING_START":
                quote = tok.string.lstrip("rRbBfFuU")
                if stack and quote.startswith(stack[-1]):
                    found.append(f"{path}:{tok.start[0]}: nested f-string reuses {quote}")
                stack.append(quote)
            elif name == "FSTRING_END":
                stack.pop()
            elif stack and name == "STRING":
                body = tok.string.lstrip("rRbBuU")
                if body.startswith(stack[-1]):
                    found.append(f"{path}:{tok.start[0]}: {tok.string} inside an f-string using {stack[-1]}")
                if "\\" in tok.string:
                    found.append(f"{path}:{tok.start[0]}: backslash inside an f-string expression")
    return found


@pytest.mark.parametrize("path", FROZEN_PY, ids=lambda p: str(p.relative_to(REPO)))
def test_parses_on_the_build_python(path):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))
    if sys.version_info >= (3, 12):
        assert _pep701_only(path) == []


@pytest.mark.skipif(sys.version_info < (3, 12), reason="on 3.11 the module itself would not compile")
def test_the_token_check_catches_the_real_bug(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("""x = f"{'are' if a else "isn't"}"\n""", encoding="utf-8")
    assert _pep701_only(bad)
    good = tmp_path / "good.py"
    good.write_text("""x = f"{'are' if a else 'not'}"\n""", encoding="utf-8")
    assert _pep701_only(good) == []


# ---- PyInstaller's warnings: lost backend code fails the build

WARN = """
missing module named feedparser - imported by backend.services.news_scraper (delayed)
invalid module named backend.services.some_module - imported by backend.core.tool_runners (delayed, optional)
missing module named pydantic.BaseModel - imported by pydantic (conditional), backend.api.x (top-level)
missing module named fcntl - imported by backend.services.y (conditional)
missing module named numpy_distutils - imported by numpy.f2py.diagnose (delayed, optional)
invalid module named somepkg.tests - imported by somepkg (delayed)
"""


def test_the_warnings_gate_fails_on_lost_code_and_only_that():
    found = _load("check_frozen_warnings").problems(WARN)
    assert [line.split()[3] for line in found] == ["feedparser", "backend.services.some_module"]


def test_the_build_runs_both_gates_after_pyinstaller():
    src = (SCRIPTS / "build-app.sh").read_text(encoding="utf-8")
    pyi = src.index("-m PyInstaller")
    warn = src.index('check_frozen_warnings.py "$REPO_ROOT/build/viralmint/warn-viralmint.txt"')
    data = src.index('check_frozen_data.py "$REPO_ROOT/dist/ViralMint/_internal"')
    package = src.index("# ─── 6. Package")
    assert pyi < warn < package and pyi < data < package


def test_ffmpeg_stays_on_the_verified_line():
    """A new imageio-ffmpeg is a new ffmpeg in every installer, so it stays a decision."""
    assert "imageio-ffmpeg==0.6.0" in (REPO / "requirements.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("package", ["trafilatura", "feedparser"])
def test_imported_packages_are_declared(package):
    lines = (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith(package) for line in lines)


# ---- runtime data files the frozen app reads (trafilatura, justext and tld never shipped)

def test_the_spec_collects_the_data_the_app_reads_at_run_time():
    src = (SCRIPTS / "viralmint.spec").read_text()
    assert 'for p in ("trafilatura", "justext", "tld"):' in src and "datas += collect_data_files(p)" in src
    assert 'collect_data_files("yt_dlp", includes=["extractor/youtube/jsc/_builtin/vendor/*.js"])' in src


def test_the_data_gate_names_what_is_missing_or_empty(tmp_path, capsys):
    gate = _load("check_frozen_data")
    for rel in gate.REQUIRED:
        path = tmp_path.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    assert gate.main(["x", str(tmp_path)]) == 0
    (tmp_path / "trafilatura" / "settings.cfg").unlink()
    (tmp_path / "tld" / "res" / "effective_tld_names.dat.txt").write_text("")
    assert gate.main(["x", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "trafilatura/settings.cfg" in err and "tld/res/effective_tld_names.dat.txt" in err


def test_every_backend_file_the_data_gate_asks_for_exists_in_the_repo():
    gate = _load("check_frozen_data")
    for rel in gate.REQUIRED:
        if rel.startswith("backend/"):
            assert (REPO / rel).is_file(), rel


def test_every_package_file_the_data_gate_asks_for_exists_in_the_install():
    import site
    gate = _load("check_frozen_data")
    roots = [Path(p) for p in site.getsitepackages()]
    for rel in gate.REQUIRED:
        if not rel.startswith("backend/"):
            assert any((root / rel).is_file() for root in roots), rel


def test_a_lost_extractor_is_a_warning_not_a_debug_line(monkeypatch, caplog):
    import logging

    trafilatura = pytest.importorskip("trafilatura")

    from backend.services import news_scraper

    def lost(*_a, **_k):
        raise FileNotFoundError("settings.cfg")
    monkeypatch.setattr(trafilatura, "bare_extraction", lost)
    with caplog.at_level(logging.WARNING, logger=news_scraper.logger.name):
        assert news_scraper._extract_with_trafilatura("<html></html>", "https://x.example/") == {}
    assert "trafilatura extraction error" in caplog.text
