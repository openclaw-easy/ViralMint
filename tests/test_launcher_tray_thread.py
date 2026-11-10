# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""macOS tray updates must run on the main thread (macOS 27 aborted the
launcher ~9 s after launch on an off-main tray call).

pystray's `icon` / `menu` / `title` setters call `NSStatusItem` from whatever
thread assigns them. The launcher's state poller, pulse animation, auto-start,
stop and restart threads all refresh the tray, and macOS 27 aborts the process
(EXC_BREAKPOINT under `-[NSStatusItem setMenu:]`) on an off-main call.
`_on_tray_thread` queues such calls on the main operation queue instead, and
every tray mutation in launcher.py goes through it.
"""
import ast
import re
import sys
import threading
import types
from pathlib import Path

import pytest

import launcher


class _FakeQueue:
    def __init__(self):
        self.queued = []

    def addOperationWithBlock_(self, fn):
        self.queued.append(fn)

    def drain(self):
        while self.queued:
            self.queued.pop(0)()


def _fake_foundation(monkeypatch):
    queue = _FakeQueue()
    mod = types.SimpleNamespace(
        NSOperationQueue=types.SimpleNamespace(mainQueue=lambda: queue))
    monkeypatch.setitem(sys.modules, "Foundation", mod)
    return queue


def _in_thread(fn):
    t = threading.Thread(target=fn)
    t.start()
    t.join()


class _FakeIcon:
    """Records which thread each pystray setter ran on."""

    def __init__(self):
        self.calls = []

    def _record(self, what):
        self.calls.append((what, threading.current_thread() is threading.main_thread()))

    icon = property(lambda s: None, lambda s, v: s._record("icon"))
    menu = property(lambda s: None, lambda s, v: s._record("menu"))
    title = property(lambda s: None, lambda s, v: s._record("title"))
    visible = property(lambda s: None, lambda s, v: s._record("visible"))


def test_darwin_off_main_thread_is_queued_not_run(monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    queue = _fake_foundation(monkeypatch)
    ran = []
    _in_thread(lambda: launcher._on_tray_thread(lambda: ran.append(1)))
    assert ran == [] and len(queue.queued) == 1
    queue.drain()
    assert ran == [1]


def test_darwin_main_thread_runs_inline(monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    queue = _fake_foundation(monkeypatch)
    ran = []
    launcher._on_tray_thread(lambda: ran.append(1))
    assert ran == [1] and queue.queued == []


def test_other_platforms_run_inline_from_any_thread(monkeypatch):
    for platform in ("win32", "linux"):
        monkeypatch.setattr(launcher.sys, "platform", platform)
        ran = []
        _in_thread(lambda: launcher._on_tray_thread(lambda: ran.append(1)))
        assert ran == [1]


def test_exception_in_tray_fn_is_swallowed(monkeypatch, capsys):
    """An exception escaping an ObjC block would abort the process."""
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    queue = _fake_foundation(monkeypatch)

    def boom():
        raise RuntimeError("tray boom")

    _in_thread(lambda: launcher._on_tray_thread(boom))
    queue.drain()  # must not raise
    launcher._on_tray_thread(boom)  # inline path must not raise either
    assert "tray boom" in capsys.readouterr().err


def test_refresh_from_worker_thread_mutates_only_on_main(monkeypatch):
    """The full refresh path: probe on the worker, setters on the main thread."""
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    queue = _fake_foundation(monkeypatch)
    icon = _FakeIcon()
    monkeypatch.setattr(launcher, "_tray_icon", icon)
    monkeypatch.setattr(launcher, "_last_tray_state", None)
    monkeypatch.setattr(launcher, "_get_state", lambda: "running")
    monkeypatch.setattr(launcher, "_get_tray_icon", lambda state, frame=0: object())
    # The real builder imports pystray, whose darwin backend needs the real
    # Foundation module this test replaces.
    monkeypatch.setattr(launcher, "_build_tray_menu", lambda state: object())

    _in_thread(launcher._refresh_tray)
    _in_thread(launcher._update_tray_title)
    assert icon.calls == []  # nothing touched the icon on the worker
    queue.drain()
    assert [c for c, _ in icon.calls] == ["icon", "menu", "title", "title"]
    assert all(on_main for _, on_main in icon.calls)
    assert launcher._last_tray_state == "running"

    # Same state again → no rebuild (the NSMenu flicker guard).
    icon.calls.clear()
    _in_thread(launcher._refresh_tray)
    queue.drain()
    assert icon.calls == []


def test_setup_shows_icon_on_main_thread(monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    queue = _fake_foundation(monkeypatch)
    icon = _FakeIcon()
    _in_thread(lambda: launcher._tray_setup(icon))  # pystray calls setup on a worker
    assert icon.calls == []
    queue.drain()
    assert icon.calls == [("visible", True)]


def test_stale_pulse_frame_is_dropped(monkeypatch):
    icon = _FakeIcon()
    monkeypatch.setattr(launcher, "_tray_icon", icon)
    monkeypatch.setattr(launcher, "_get_tray_icon", lambda state, frame=0: object())
    monkeypatch.setattr(launcher, "_last_tray_state", "running")
    launcher._set_starting_frame(1)
    assert icon.calls == []
    monkeypatch.setattr(launcher, "_last_tray_state", "starting")
    launcher._set_starting_frame(1)
    assert [c for c, _ in icon.calls] == ["icon"]


_TRAY_THREAD_ONLY = {
    "_call_tray_fn", "_tray_setup", "_set_starting_frame", "_apply_tray_state",
    "_set_tray_title", "create_tray",
}


def test_setters_only_in_tray_thread_functions():
    """Drift guard: a pystray setter outside the tray-thread functions is a
    future off-main call waiting to happen."""
    src = Path(launcher.__file__).read_text()
    tree = ast.parse(src)
    allowed = [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name in _TRAY_THREAD_ONLY]
    assert {n.name for n in allowed} == _TRAY_THREAD_ONLY  # a rename must update this list
    nested_in_allowed = {id(inner) for n in allowed for inner in ast.walk(n)} - {id(n) for n in allowed}
    offenders = []
    for node in ast.walk(tree):
        if (not isinstance(node, ast.FunctionDef) or node.name in _TRAY_THREAD_ONLY
                or id(node) in nested_in_allowed):
            continue
        body = ast.get_source_segment(src, node)
        for inner in ast.walk(node):
            if isinstance(inner, ast.FunctionDef) and inner.name in _TRAY_THREAD_ONLY:
                body = body.replace(ast.get_source_segment(src, inner), "")
        if re.search(r"\b\w*icon\w*\.(icon|menu|title|visible)\s*=|\.update_menu\(\)", body):
            offenders.append(node.name)
    assert offenders == [], offenders


class _RunningIcon:
    _running = True

    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


def test_signal_exit_ends_the_tray_loop_instead_of_raising(monkeypatch):
    """A SystemExit raised inside a native callback aborts the process."""
    icon = _RunningIcon()
    monkeypatch.setattr(launcher, "_tray_icon", icon)
    quits = []
    monkeypatch.setattr(launcher, "_gui_callback", quits.append)
    launcher._exit_after_signal()  # must not raise
    assert icon.stopped == 1 and quits == ["quit"]


def test_signal_exit_without_a_running_loop_exits(monkeypatch):
    monkeypatch.setattr(launcher, "_tray_icon", None)
    with pytest.raises(SystemExit):
        launcher._exit_after_signal()
    icon = _RunningIcon()
    icon._running = False
    monkeypatch.setattr(launcher, "_tray_icon", icon)
    with pytest.raises(SystemExit):
        launcher._exit_after_signal()
    assert icon.stopped == 0


def test_real_main_queue_runs_the_hop():
    """macOS only: the hop must hand PyObjC something it can bridge to a block
    (a `functools.partial` is refused: "Cannot create native callable"), and
    the main run loop must execute it on the main thread."""
    if sys.platform != "darwin":
        pytest.skip("macOS main queue")
    Foundation = pytest.importorskip("Foundation")
    ran = []
    _in_thread(lambda: launcher._on_tray_thread(
        lambda: ran.append(threading.current_thread() is threading.main_thread())))
    assert ran == []
    Foundation.NSRunLoop.mainRunLoop().runUntilDate_(
        Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.5))
    assert ran == [True]
