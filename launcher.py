#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
ViralMint Launcher — Toolbox for non-technical users.
Double-click to open. System tray icon + optional GUI window.

Features:
  - System tray with the real ViralMint icon + tri-state status
    (stopped / starting / running), breathing-pulse animation while
    the backend boots.
  - Tray menu: Start / Stop / Restart Service, Open WebUI, Copy URL,
    Show Launcher, Quit.
  - If tkinter available: a warm-themed launcher window with buttons.
  - Close window → minimizes to tray (service keeps running).
  - Quit from tray → confirms if jobs are running, then stops everything.
  - Crash watchdog auto-restarts an unexpectedly-dead backend (crash-loop
    guarded) and snapshots the log tail as a crash report.

This launcher ALWAYS auto-starts the backend on boot AND ALWAYS opens the
browser when the service becomes ready — there is no preferences file and
there are no toggles. Since this is the open-source SOURCE distribution,
the backend is launched by running `run.py` with the project's Python.
"""
import sys
import os
import json
import subprocess
import threading
import webbrowser
import time
import platform
from pathlib import Path

# ─── Constants ─────────────────────────────────────────────────────────────────

PORT = 16888
URL = f"http://localhost:{PORT}"
APP_NAME = "ViralMint"

# PyInstaller bundle detection. This OSS variant ships as a source
# distribution and normally runs NON-frozen (`python launcher.py`), where the
# backend is started via `run.py`. The frozen branch below is kept defensively
# in case a downstream packager freezes the launcher — if a bundled backend
# binary is present it is used, otherwise we degrade gracefully to running
# `run.py` (the source path must never crash if the binary is absent).
FROZEN = getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")

if FROZEN:
    _BUNDLE_ROOT = Path(sys._MEIPASS)
    _DATA_DIR = Path(
        os.environ.get("VIRALMINT_DATA_DIR") or (Path.home() / "ViralMint")
    )
    ROOT = _BUNDLE_ROOT
    ICON_PATH = _BUNDLE_ROOT / "icon-192.png"
    _backend_ext = ".exe" if platform.system() == "Windows" else ""
    BACKEND_BIN = _BUNDLE_ROOT / "backend" / f"viralmint-server{_backend_ext}"
    FRONTEND_DIST = _BUNDLE_ROOT / "frontend" / "dist"
    BACKEND_LOG = _DATA_DIR / "logs" / "launcher-backend.log"
else:
    ROOT = Path(__file__).parent
    _DATA_DIR = ROOT
    ICON_PATH = ROOT / "frontend" / "public" / "icon-192.png"
    BACKEND_BIN = None
    FRONTEND_DIST = None
    BACKEND_LOG = ROOT / "storage" / "tmp" / "launcher-backend.log"


# The launcher had a `~/ViralMint/launcher.json` preferences file with
# two boolean toggles ("auto_start", "auto_open") wired to tray-menu
# checkmarks. Both were removed — the launcher now always auto-starts the
# service on boot AND always opens the browser when the service becomes
# ready. The toggles created more confusion than value ("I checked it, why
# doesn't it open?" / "I unchecked it, why does it still open?") and the fix
# made them moot. A stray launcher.json in the data dir is harmless and
# ignored.


def _read_version() -> str:
    """Read the current app version string. Used in the tray tooltip and the
    tkinter window header. Lookup order:

      1. Import `backend.version.__version__` (present in some layouts;
         absent in this OSS variant — that's fine).
      2. Parse `version = "x.y.z"` from `pyproject.toml` at the repo root
         (stdlib only — tomllib if available, else a simple regex).

    Returns "" if no version can be determined (the header/tooltip hide the
    version segment when empty).
    """
    try:
        from backend.version import __version__
        if isinstance(__version__, str) and __version__:
            return __version__
    except Exception:
        pass

    pyproject = ROOT / "pyproject.toml"
    try:
        if pyproject.exists():
            text = pyproject.read_text(encoding="utf-8")
            # Prefer tomllib (3.11+) for a robust parse; fall back to regex.
            try:
                import tomllib
                data = tomllib.loads(text)
                v = (data.get("project") or {}).get("version")
                if isinstance(v, str) and v:
                    return v
            except Exception:
                pass
            import re
            m = re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', text)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


VERSION = _read_version()


def _apply_platform_app_icon():
    """Override the Python interpreter's app icon with the ViralMint logo.

    macOS: sets the dock icon via NSApplication.
    Windows: sets the AppUserModelID so the taskbar groups under ViralMint
             and picks up the tkinter window icon as the taskbar icon.
    Linux: no-op — window manager reads tkinter's iconphoto directly.
    """
    if not ICON_PATH.exists():
        return

    system = platform.system()
    if system == "Darwin":
        try:
            from AppKit import NSApplication, NSImage
            img = NSImage.alloc().initWithContentsOfFile_(str(ICON_PATH))
            if img:
                NSApplication.sharedApplication().setApplicationIconImage_(img)
        except Exception:
            pass
    elif system == "Windows":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("app.viralmint.launcher")
        except Exception:
            pass

# ─── Backend process management ────────────────────────────────────────────────

_backend_process = None
_backend_log = None
_status_lock = threading.Lock()

_MAX_BACKEND_LOG_BYTES = 5_000_000  # rotate at 5MB
_LAUNCHER_SENTINEL_PORT = 16887     # one-less than service port; single-instance guard
_launcher_sentinel_sock = None      # kept alive for lifetime of launcher process

# ── Crash watchdog state ──
# The tray used to NOTICE a dead backend (_reap_dead_backend flips the icon
# to "stopped") but never restarted it — an unexpected uvicorn/native crash
# left the app dead until the user manually clicked Start. The watchdog
# auto-restarts, bounded by a crash-loop guard so a hard-broken install
# doesn't restart-spin forever.
_user_stop_requested = False        # True while a stop is user-initiated
_pending_unexpected_exit = None     # parked exit code awaiting the poller
_crash_timestamps = []              # recent unexpected-exit times (loop guard)
_CRASH_LOOP_WINDOW_S = 600          # guard window: 10 minutes
_CRASH_LOOP_MAX = 3                 # max auto-restarts inside the window
_CRASH_TAIL_BYTES = 64 * 1024       # how much of the log tail to preserve


def _find_python():
    """Find the best Python executable (venv preferred)."""
    venv_dir = "Scripts" if platform.system() == "Windows" else "bin"
    for name in ("python3", "python"):
        p = ROOT / "venv" / venv_dir / name
        if p.exists():
            return str(p)
    return sys.executable


def _port_listening(port=PORT, host="127.0.0.1"):
    """Fast TCP probe — returns True if something is bound to the port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False


def _find_port_pids(port=PORT):
    """Return PIDs listening on the given TCP port. Cross-platform, no deps."""
    try:
        if platform.system() == "Windows":
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"],
                text=True, stderr=subprocess.DEVNULL,
            )
            needle = f":{port}"
            pids = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and needle in parts[1] and parts[3].upper() == "LISTENING":
                    try:
                        pids.add(int(parts[4]))
                    except ValueError:
                        pass
            return list(pids)
        else:
            out = subprocess.check_output(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                text=True, stderr=subprocess.DEVNULL,
            )
            return [int(p) for p in out.split() if p.strip().isdigit()]
    except Exception:
        return []


def _terminate_process_tree(proc) -> None:
    """Stop a backend subprocess AND its children, per-platform.

    Windows: `proc.terminate()` is TerminateProcess — a hard kill of the
    parent ONLY. The backend's own children (ffmpeg.exe, node.exe for
    HyperFrames, chromium, voxcpm) survive as orphans that burn CPU and can
    hold file locks that fight the next start. `taskkill /T /F` kills the
    whole tree instead.

    POSIX: keep the existing terminate → wait → kill escalation (SIGTERM
    gives uvicorn its graceful lifespan shutdown; child cleanup is handled
    by the backend itself).
    """
    if platform.system() == "Windows":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass
        try:
            proc.wait(timeout=8)
        except Exception:
            # Last resort if taskkill itself failed (rare: access denied).
            try:
                proc.kill()
            except Exception:
                pass
        return

    try:
        proc.terminate()
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass


def _terminate_port_holder(port=PORT):
    """Best-effort: stop whoever owns the port (if not us).

    Windows note: os.kill(pid, SIGTERM) is really TerminateProcess (parent
    only) — use taskkill /T so an externally-started backend's child tree
    (ffmpeg/node/chromium) dies with it, same rationale as
    _terminate_process_tree above.
    """
    import signal
    for pid in _find_port_pids(port):
        try:
            if platform.system() == "Windows":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=15,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort cross-platform clipboard copy with no extra dependencies."""
    sys_name = platform.system()
    try:
        if sys_name == "Darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=False, timeout=2)
            return True
        if sys_name == "Windows":
            subprocess.run(
                "clip", input=text.encode("utf-16-le"),
                shell=True, check=False, timeout=2,
            )
            return True
        for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
            try:
                subprocess.run(cmd, input=text.encode(), check=False, timeout=2)
                return True
            except FileNotFoundError:
                continue
    except Exception:
        pass
    return False


def _running_job_count(timeout: float = 1.0) -> int:
    """Hit /api/jobs?status=running and return the count. 0 on any error
    (including service-not-ready) — fail-quiet so the quit flow never
    blocks on the check itself."""
    if not is_ready(timeout=0.4):
        return 0
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"{URL}/api/jobs?status=running&limit=50", timeout=timeout,
        ) as resp:
            data = json.load(resp)
        # The endpoint shape has shifted historically: support both
        # {"jobs": [...]} and a bare list at the root.
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        return len(jobs) if isinstance(jobs, list) else 0
    except Exception:
        return 0


def _confirm_quit_with_jobs(job_count: int) -> bool:
    """Show a native confirm dialog before quitting with active jobs.
    Returns True if the user wants to quit anyway. Fail-open: if every
    dialog mechanism falls through, return True so the user isn't
    trapped unable to quit.

    Tries in order:
      1. tkinter messagebox (works wherever tk loaded)
      2. macOS osascript dialog
      3. Windows MessageBoxW via ctypes
      4. (no fallback for headless Linux — proceed silently)
    """
    title = f"{APP_NAME} — Quit?"
    plural = "s" if job_count != 1 else ""
    message = (
        f"{job_count} job{plural} still running. "
        "Quitting now will cancel them mid-flight."
    )

    # 1. tkinter — handles cross-platform when imported
    if "tkinter" in sys.modules:
        try:
            from tkinter import messagebox
            return bool(messagebox.askokcancel(title, message + "\n\nQuit anyway?"))
        except Exception:
            pass

    # 2. macOS native
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 f'display dialog "{message}" '
                 f'with title "{APP_NAME}" '
                 f'buttons {{"Cancel", "Quit Anyway"}} '
                 f'default button "Cancel" with icon caution'],
                capture_output=True, text=True, timeout=30,
            )
            return "Quit Anyway" in (result.stdout or "")
        except Exception:
            return True

    # 3. Windows native
    if platform.system() == "Windows":
        try:
            import ctypes
            # MB_OKCANCEL (1) + MB_ICONWARNING (0x30) = 0x31; OK return = 1
            ret = ctypes.windll.user32.MessageBoxW(
                0, message + "\n\nQuit anyway?", title, 0x31,
            )
            return ret == 1
        except Exception:
            return True

    return True


def _write_crash_report(exit_code) -> None:
    """Preserve the tail of the backend log as a timestamped crash report.

    launcher-backend.log is append-mode and rotates at 5MB, so the evidence
    of THIS crash would eventually be buried or rotated away. Snapshotting
    the last 64KB into logs/backend-crash-<ts>.log the moment we notice the
    death gives support/debugging a stable artifact. Best-effort — never
    let diagnostics break the restart path.
    """
    try:
        log_dir = BACKEND_LOG.parent
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        report = log_dir / f"backend-crash-{ts}.log"
        tail = b""
        if BACKEND_LOG.exists():
            with open(BACKEND_LOG, "rb") as f:
                f.seek(max(0, BACKEND_LOG.stat().st_size - _CRASH_TAIL_BYTES))
                tail = f.read()
        header = (
            f"ViralMint backend exited unexpectedly at {ts}\n"
            f"exit code: {exit_code}\n"
            f"launcher version: {VERSION}\n"
            f"platform: {platform.platform()}\n"
            + "-" * 60 + "\n"
        ).encode("utf-8", "replace")
        report.write_bytes(header + tail)
        # Keep only the 10 newest crash reports.
        crashes = sorted(log_dir.glob("backend-crash-*.log"))
        for old in crashes[:-10]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _watchdog_should_restart() -> bool:
    """Crash-loop guard: allow an auto-restart only if fewer than
    _CRASH_LOOP_MAX unexpected exits happened in the last window."""
    now = time.time()
    _crash_timestamps.append(now)
    while _crash_timestamps and now - _crash_timestamps[0] > _CRASH_LOOP_WINDOW_S:
        _crash_timestamps.pop(0)
    return len(_crash_timestamps) <= _CRASH_LOOP_MAX


def _handle_unexpected_exit(exit_code) -> None:
    """Crash watchdog: the launcher-owned backend died without a user stop.
    Snapshot the evidence, then auto-restart unless we're crash-looping.
    Runs on the state-poller thread (never the UI thread)."""
    _write_crash_report(exit_code)
    if not _watchdog_should_restart():
        print(f"Backend crash-looping (> {_CRASH_LOOP_MAX} exits in "
              f"{_CRASH_LOOP_WINDOW_S // 60} min) — not auto-restarting.")
        return
    print(f"Backend exited unexpectedly (code {exit_code}) — auto-restarting.")
    # Give an orphaned half-dead process a beat to release the port.
    deadline = time.time() + 5
    while time.time() < deadline and _port_listening():
        time.sleep(0.25)
    # Zombie-port edge: if something STILL holds the port (a wedged old
    # backend that died without closing its socket), start_service() would
    # see the bound port and no-op "already running" — leaving the app dead
    # behind a green-ish tray. Tree-kill the holder first, then start.
    if _port_listening():
        _terminate_port_holder()
        deadline = time.time() + 5
        while time.time() < deadline and _port_listening():
            time.sleep(0.25)
    # Race guard: the user may have clicked Stop/Quit DURING the port-wait
    # above (stop_service sets _user_stop_requested before killing). Re-check
    # right before restarting — without this, the watchdog resurrects a
    # backend the user deliberately stopped. Checked under the lock so we
    # don't miss a concurrent stop_service() write.
    with _status_lock:
        aborted = _user_stop_requested
    if aborted:
        print("Restart aborted — user stopped the service during recovery.")
        _refresh_tray()
        return
    start_service()
    _refresh_tray()


def _reap_dead_backend():
    """If our own subprocess has exited, close its log file and forget it.

    Without this, an unexpected uvicorn crash leaks the log FD until launcher
    exit — and leaves `_backend_process` pointing at a zombie.

    An UNEXPECTED death (not a user-initiated stop) parks its exit code in
    `_pending_unexpected_exit`; the state poller consumes it via
    `_consume_unexpected_exit()` to trigger the crash watchdog.
    """
    global _backend_process, _backend_log, _pending_unexpected_exit
    with _status_lock:
        if _backend_process is not None and _backend_process.poll() is not None:
            if not _user_stop_requested:
                # Sticky signal: ANY caller may be the one to reap (menu
                # renders call _get_state() too) — park the exit code so
                # the state poller reliably sees it even if it wasn't the
                # reaper.
                _pending_unexpected_exit = _backend_process.returncode
            _backend_process = None
            if _backend_log is not None:
                try:
                    _backend_log.close()
                except Exception:
                    pass
                _backend_log = None


def _consume_unexpected_exit():
    """Poller-only: return-and-clear the parked unexpected-exit code."""
    global _pending_unexpected_exit
    with _status_lock:
        code = _pending_unexpected_exit
        _pending_unexpected_exit = None
    return code


def is_running():
    """True if we own a live backend subprocess OR something is bound to PORT.

    Port-listening is the authoritative signal — the service may have been
    started outside the launcher, or our subprocess may be mid-startup.

    NOTE: this returns True as soon as uvicorn binds the port — which can
    be ~1-2s before the HTTP layer is actually serving requests. For the
    tray UI we use `_get_state()` (which calls `is_ready()` below) so
    "Open WebUI" only enables after a real /health 200. is_running() is
    kept for callers that want the lighter "process alive" semantics.
    """
    _reap_dead_backend()
    with _status_lock:
        if _backend_process is not None and _backend_process.poll() is None:
            return True
    return _port_listening()


def is_ready(timeout=0.4):
    """Deeper readiness check: port bound AND /health returns 200.

    Used by the tray's state machine so we don't flip to "Running" — and
    enable "Open WebUI" — while uvicorn is still registering routers.
    The /health route is mounted before any router include in
    backend/main.py, so this returns True the moment HTTP requests are
    actually being served.
    """
    if not _port_listening():
        return False
    import urllib.request
    try:
        with urllib.request.urlopen(f"{URL}/health", timeout=timeout) as resp:
            return getattr(resp, "status", 200) == 200
    except Exception:
        return False


def _get_state():
    """Tri-state launcher state for the tray UI.

      "running"  — /health returns 200; backend is fully serving
      "starting" — subprocess alive OR port bound, but /health not yet 200
      "stopped"  — nothing alive

    Splitting the binary is_running() into three states is what fixes the
    "click Open WebUI too soon → unloaded page" bug: we no longer flip to
    Running just because the port bound.
    """
    _reap_dead_backend()
    with _status_lock:
        owns_process = _backend_process is not None and _backend_process.poll() is None
    if is_ready(timeout=0.3):
        return "running"
    if owns_process or _port_listening():
        return "starting"
    return "stopped"


def start_service():
    """Start the ViralMint backend server."""
    global _backend_process, _backend_log, _user_stop_requested
    with _status_lock:
        if _backend_process and _backend_process.poll() is None:
            return True
        # Any start (user click, auto-boot, watchdog) re-arms the watchdog.
        _user_stop_requested = False

    # Something external already holds the port — treat as already running.
    if _port_listening():
        return True

    env = os.environ.copy()
    env["PORT"] = str(PORT)
    # Suppress run.py's standalone open_browser() — the launcher is the sole
    # authority over whether/when the browser opens (it opens on /health 200).
    # Without this, run.py auto-opens a tab of its own on top of ours.
    env["VIRALMINT_NO_BROWSER"] = "1"
    # Force Python UTF-8 mode in the backend (PEP 540). On Windows the
    # locale encoding is cp1252, which silently poisons subprocess calls
    # using text=True without encoding= (yt-dlp/ffmpeg output with non-ASCII
    # titles → UnicodeDecodeError), text-mode open() writes (ffmpeg concat
    # lists with non-ASCII paths → UnicodeEncodeError), logging, and stdout.
    env["PYTHONUTF8"] = "1"

    # Route backend output to a log file. A PIPE-with-no-reader setup
    # deadlocks once the kernel pipe buffer fills (verbose SQLAlchemy +
    # uvicorn --reload log), leaving the server stalled but still "alive".
    log_path = BACKEND_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Rotate if oversized — launcher may live for weeks.
    try:
        if log_path.exists() and log_path.stat().st_size > _MAX_BACKEND_LOG_BYTES:
            rotated = log_path.with_name(log_path.name + ".1")
            try:
                rotated.unlink(missing_ok=True)
            except Exception:
                pass
            log_path.rename(rotated)
    except Exception:
        pass

    try:
        kwargs = {}
        if platform.system() == "Windows":
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = info

        _backend_log = open(log_path, "a", buffering=1)

        if FROZEN and BACKEND_BIN and BACKEND_BIN.exists():
            # Packaged mode (defensive): spawn a bundled backend binary if a
            # downstream packager produced one. Not the normal OSS path — the
            # source path below is the supported one.
            cmd = [str(BACKEND_BIN)]
            cwd = str(BACKEND_BIN.parent)
            env["VIRALMINT_FRONTEND_DIST"] = str(FRONTEND_DIST)
            env["VIRALMINT_DATA_DIR"] = str(_DATA_DIR)
            # Enhanced Download — pin Playwright's browser dir into our data
            # dir so Chromium downloads land in ~/ViralMint/playwright-browsers
            # instead of ~/.cache/ms-playwright.
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(_DATA_DIR / "playwright-browsers")
        else:
            # Source distribution (the normal OSS path): run the project's
            # run.py with the venv/system Python. VIRALMINT_NO_BROWSER=1 tells
            # run.py to skip its own browser auto-open (launcher owns that).
            cmd = [_find_python(), str(ROOT / "run.py")]
            cwd = str(ROOT)

        _backend_process = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=_backend_log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        return True
    except Exception as e:
        print(f"Failed to start service: {e}")
        if _backend_log is not None:
            try:
                _backend_log.close()
            except Exception:
                pass
            _backend_log = None
        return False


def stop_service():
    """Stop the ViralMint backend server.

    If we own the subprocess, terminate it. Otherwise the service was started
    outside the launcher — best-effort kill whoever holds the port so the UI
    stays consistent with reality.
    """
    global _backend_process, _backend_log, _user_stop_requested
    with _status_lock:
        # Mark the stop as intentional BEFORE the process dies, so the
        # watchdog never mistakes it for a crash and restarts it.
        _user_stop_requested = True
        proc = _backend_process
        _backend_process = None

    if proc is not None and proc.poll() is None:
        _terminate_process_tree(proc)
    else:
        _terminate_port_holder()

    if _backend_log is not None:
        try:
            _backend_log.close()
        except Exception:
            pass
        _backend_log = None


def open_webui():
    """Open the WebUI in the default browser."""
    webbrowser.open(URL)


def _open_browser_on_ready(ready: bool) -> None:
    """Open the browser when the service has just become ready.

    Called from every "service has just been started" path (boot-time
    auto-flow, tray Start Service, tk window Start button). Restart is
    deliberately NOT a caller — the user's existing browser tab is
    almost certainly still open and pointed at /, no need for another.

    The "Open browser when ready" tray toggle was removed in favor of this
    always-open default. Reasoning: the toggle's only job was to suppress
    the browser open, and after the run.py-also-opens-browser bug was fixed
    (VIRALMINT_NO_BROWSER), keeping the toggle added more confusion than
    utility. Simpler product: you click Start, the browser opens.
    """
    if not ready:
        return
    open_webui()


def wait_for_server(timeout=30):
    """Block until the server is ready or timeout."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(URL, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


# ─── Tray Icon Rendering ──────────────────────────────────────────────────────

_TRAY_ICON_CACHE = {}  # populated on first _build_tray_icons() call


def _build_tray_icons():
    """Pre-compute one icon per tri-state (plus a 2nd 'starting' frame for
    the breathing pulse). PIL ops aren't free and the animation thread
    swaps the icon ~every 750ms — so we cache once and re-bind references.

    Variants:
      stopped    — grayscale at high alpha (the "service is off" look)
      running    — untouched full-color logo
      starting_a — full color tinted ~40% toward warm orange
      starting_b — full color tinted ~18% toward warm orange
                   (the alternation creates a gentle "breathing" pulse —
                    cheaper than a true rotating spinner and unambiguous
                    against macOS's dark/light menubar)
    """
    from PIL import Image, ImageOps

    # 128px source downscales crisply for the macOS menubar's ~22pt cell
    # without the soft fringe a 64px source picks up at Retina 2×/3× density.
    size = 128

    if ICON_PATH.exists():
        try:
            base = Image.open(ICON_PATH).convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    else:
        base = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    r, g, b, a = base.split()

    # Stopped: grayscale with a high alpha (0.92). Keeps the "service is off"
    # cue (visibly less saturated than the colored running state) while
    # rendering the logo glyph crisply.
    gray = ImageOps.grayscale(Image.merge("RGB", (r, g, b)))
    stopped = Image.merge("RGBA", (gray, gray, gray, a))
    stopped.putalpha(a.point(lambda p: int(p * 0.92)))

    # Starting: warm orange tint over the base — clearly distinct from
    # yellow (which read as a "warning"), softer than pure red, and
    # contrasts cleanly with both the gray "stopped" and full-color
    # "running" states. Two frames at different tint strengths drive
    # the breathing pulse.
    def _tint(strength):
        """Composite a warm orange over the base at `strength` (0..1).
        Overlay alpha is masked by the base's own alpha so transparent
        pixels stay transparent (no square halo around the logo).
        """
        orange_rgb = (251, 146, 60)  # Tailwind orange-400
        overlay = Image.new("RGBA", (size, size), orange_rgb + (0,))
        overlay.putalpha(a.point(lambda p: int(p * strength)))
        return Image.alpha_composite(base, overlay)

    starting_a = _tint(0.40)
    starting_b = _tint(0.18)

    return {
        "running": base,
        "stopped": stopped,
        "starting_a": starting_a,
        "starting_b": starting_b,
    }


def _get_tray_icon(state, frame=0):
    """Return the cached PIL Image for `state` (and optional anim frame).

    `frame` is only consulted when state == "starting" (alternates
    between starting_a and starting_b for the pulse). Other states are
    always the same image.
    """
    global _TRAY_ICON_CACHE
    if not _TRAY_ICON_CACHE:
        _TRAY_ICON_CACHE = _build_tray_icons()
    if state == "starting":
        return _TRAY_ICON_CACHE["starting_a" if frame == 0 else "starting_b"]
    return _TRAY_ICON_CACHE.get(state, _TRAY_ICON_CACHE["stopped"])


# ─── System Tray (pystray) ────────────────────────────────────────────────────

_tray_icon = None
_gui_callback = None  # set by GUI if tkinter is available
_last_tray_state = None
_starting_anim_stop = threading.Event()
_starting_anim_thread = None


def _start_starting_animation():
    """Begin the breathing-pulse animation while the tray sits in
    "starting" state. Daemon thread alternates icon frames every 750ms;
    exits when state leaves "starting" or when stop is signaled.

    Cheap: just swaps a pre-cached PIL Image; no PIL ops per frame, no
    menu rebuild. Two-frame breathing reads as "actively loading"
    without the macOS tray-cell jitter that a faster spinner would
    cause.
    """
    global _starting_anim_thread
    if _starting_anim_thread and _starting_anim_thread.is_alive():
        return
    _starting_anim_stop.clear()

    def _loop():
        frame = 0
        while not _starting_anim_stop.is_set():
            # Bail if we've left "starting" — _refresh_tray will set the
            # final running/stopped icon and we shouldn't fight it.
            if _get_state() != "starting":
                return
            if _tray_icon:
                try:
                    _tray_icon.icon = _get_tray_icon("starting", frame=frame)
                except Exception:
                    pass
                frame = 1 - frame
            # Use Event.wait() so a stop signal exits promptly instead of
            # waiting out the full 750ms tick.
            if _starting_anim_stop.wait(0.75):
                return

    _starting_anim_thread = threading.Thread(
        target=_loop, daemon=True, name="launcher-tray-anim",
    )
    _starting_anim_thread.start()


def _stop_starting_animation():
    """Signal the pulse thread to exit. Safe to call repeatedly."""
    _starting_anim_stop.set()


def _refresh_tray():
    """Update tray icon + rebuild menu to reflect current state.

    Skips the menu rebuild when the state hasn't changed — rebuilding
    the native NSMenu on every poll tick flickers on macOS.

    Tri-state ("running" / "starting" / "stopped") replaces the old
    binary so "Open WebUI" stays disabled while uvicorn is still
    booting. The pulse animation thread is started/stopped in lockstep
    with entering/leaving the "starting" state.
    """
    global _last_tray_state
    if not _tray_icon:
        return
    state = _get_state()
    if state == _last_tray_state:
        return

    if state == "starting":
        # Static frame; the animation thread will start swapping.
        _tray_icon.icon = _get_tray_icon("starting", frame=0)
        _start_starting_animation()
    else:
        _stop_starting_animation()
        _tray_icon.icon = _get_tray_icon(state)

    _tray_icon.menu = _build_tray_menu()
    _tray_icon.update_menu()
    _update_tray_title()
    _last_tray_state = state


def _start_state_poller(slow_interval=2.0, fast_interval=0.5):
    """Daemon thread that refreshes the tray whenever backend state changes.

    Two cadences:
      - fast (500ms) while in "starting" — so the icon flips to green
        the moment /health goes 200, with no perceptible lag
      - slow (2s)    when settled on "running" or "stopped" — keeps the
        tray in sync with externally-triggered start/stop without
        burning CPU on idle polling

    Keeps the tray in sync even when the service is started or stopped
    from outside the launcher (e.g. `python run.py` in a shell).
    """
    def _loop():
        while True:
            interval = fast_interval if _last_tray_state == "starting" else slow_interval
            time.sleep(interval)
            try:
                # Crash watchdog: the poller is the ONE consumer of the
                # parked unexpected-exit signal (any caller may reap, but
                # only the poller reacts). _handle_unexpected_exit snapshots
                # the log tail + auto-restarts (crash-loop-guarded).
                _reap_dead_backend()
                exit_code = _consume_unexpected_exit()
                if exit_code is not None:
                    _handle_unexpected_exit(exit_code)
                _refresh_tray()
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True, name="launcher-state-poll").start()


_AUTO_OPEN_DONE = False  # guard so the auto-open / notify fires once per session


def _auto_start_and_open():
    """Auto-start the backend if it isn't already up, then open the browser
    when /health goes 200. Safe to call multiple times — the
    `_AUTO_OPEN_DONE` flag and the `is_running()` guard make this a no-op on
    re-entry.

    The auto-open is gated on the launcher being the one that started
    the service: if the backend was already up when the launcher booted
    (another launcher started it, a `python run.py` shell, etc.), we
    don't pop a browser tab on every relaunch. That would be intrusive.
    """
    global _AUTO_OPEN_DONE
    if _AUTO_OPEN_DONE:
        return

    # Backend already running (external session, or a previous launcher
    # instance) — don't auto-open and don't fight whatever's there.
    if is_running():
        _AUTO_OPEN_DONE = True
        return

    started = start_service()
    _refresh_tray()
    if not started:
        _AUTO_OPEN_DONE = True
        return

    def _wait_and_open():
        global _AUTO_OPEN_DONE
        ready = wait_for_server(30)
        _refresh_tray()
        if ready:
            # Opening the browser IS the "service ready" signal — no native
            # notification. (A macOS `display notification` path was
            # mis-attributed to "Script Editor", which then launched when the
            # user clicked the banner.)
            _open_browser_on_ready(ready)
        _AUTO_OPEN_DONE = True

    threading.Thread(
        target=_wait_and_open, daemon=True, name="launcher-auto-open",
    ).start()


def _tray_start(icon, item):
    if is_running():
        return
    start_service()
    _refresh_tray()
    if _gui_callback:
        _gui_callback("starting")

    def _wait():
        ready = wait_for_server(30)
        _refresh_tray()
        if _gui_callback:
            _gui_callback("ready" if ready else "failed")
        # Always open the browser on a successful manual Start — the
        # tray Start Service action is an explicit user request, and
        # they expect to land in the UI. (See _open_browser_on_ready
        # for the rationale behind dropping the auto_open toggle.)
        _open_browser_on_ready(ready)

    threading.Thread(target=_wait, daemon=True).start()


def _do_stop():
    """Background worker: stop backend, refresh UI when it's actually down."""
    stop_service()
    _refresh_tray()
    if _gui_callback:
        _gui_callback("stopped")


def _tray_stop(icon, item):
    if not is_running():
        return
    threading.Thread(target=_do_stop, daemon=True, name="launcher-stop").start()


def _tray_open(icon, item):
    # Defense-in-depth: the menu only enables this when state == "running",
    # but the menu can briefly lag a real state change. Use the deeper
    # readiness check here so an early click-through still gets the
    # right behavior (no-op vs open a half-loaded browser tab).
    if _get_state() == "running":
        open_webui()


def _tray_show(icon, item):
    if _gui_callback:
        _gui_callback("show")


def _tray_quit(icon, item):
    """Quit, but check for running jobs first.

    The /api/jobs?status=running probe is fail-quiet (0 on any error /
    service-not-ready), so a stopped backend or a one-off network
    hiccup never blocks the user from quitting. When jobs ARE running,
    we surface a native confirm dialog so the user can opt out of the
    cancel-everything-mid-flight side effect.
    """
    count = _running_job_count()
    if count > 0 and not _confirm_quit_with_jobs(count):
        return
    stop_service()
    icon.stop()
    if _gui_callback:
        _gui_callback("quit")


def _do_restart():
    """Background worker: stop, wait for port to release, start, refresh."""
    stop_service()
    deadline = time.time() + 8
    while time.time() < deadline and _port_listening():
        time.sleep(0.25)
    start_service()
    _refresh_tray()  # flip to "starting" + start the pulse animation
    ready = wait_for_server(30)
    _refresh_tray()
    if _gui_callback:
        _gui_callback("ready" if ready else "failed")


def _tray_restart(icon, item):
    if _gui_callback:
        _gui_callback("starting")
    threading.Thread(target=_do_restart, daemon=True, name="launcher-restart").start()


def _tray_copy_url(icon, item):
    _copy_to_clipboard(URL)


def _build_tray_menu():
    """Build a fresh tray menu reflecting current service state.

    Layout (top → bottom):
      [status header — disabled]
      Start / Stop  ·  Restart
      ─────────────
      Open WebUI  ·  Copy URL
      ─────────────
      Show Launcher (GUI mode only)
      ─────────────
      Quit ViralMint

    Tri-state header + button-enabled rules:
      stopped  — Start enabled; Stop/Restart/Open disabled
      starting — Start disabled ("Starting…"); Stop enabled (cancel);
                 Open disabled with "(starting…)" suffix so the user
                 sees that the click would have been premature
      running  — Start disabled; Stop/Restart/Open all enabled
    """
    import pystray
    from pystray import MenuItem as Item

    state = _get_state()
    # Emoji renders in its native color even on disabled macOS menu items
    # (the OS keeps colored glyphs); plain ○ inherits the disabled-gray
    # tone, giving a clean traffic-light contrast without a per-item
    # color API.
    if state == "running":
        header = f"🟢  Running · port {PORT}"
    elif state == "starting":
        header = f"🟠  Starting · port {PORT}"
    else:
        header = "○  Stopped"

    is_running_state = state == "running"
    is_starting = state == "starting"
    is_stopped = state == "stopped"

    start_label = "▶  Starting…" if is_starting else "▶  Start Service"
    open_label = "↗  Open WebUI (starting…)" if is_starting else "↗  Open WebUI"

    # Unicode glyph prefixes — pystray has no per-item icon API, but macOS
    # renders these crisply and they match the ●/○ status header style.
    # Double space separates glyph from label so it lines up visually.
    # The user prefers these monochrome Unicode glyphs over colored emoji.

    items = [
        Item(header, None, enabled=False),
        pystray.Menu.SEPARATOR,
        Item(start_label, _tray_start, enabled=is_stopped),
        # Stop is enabled during "starting" too, so the user can cancel
        # a slow boot without having to wait for /health to time out.
        Item("■  Stop Service", _tray_stop, enabled=not is_stopped),
        Item("↻  Restart Service", _tray_restart, enabled=is_running_state),
        pystray.Menu.SEPARATOR,
        # Open WebUI ONLY when /health says we're ready — this is the
        # core fix: previously enabled-on-port-bound let users click into
        # an unloaded page.
        # No `default=` flag — used to set `default=is_running_state`
        # which on macOS pystray fires the item's action when the user
        # LEFT-CLICKS the menubar icon directly (instead of showing the
        # menu). That created an "unintended browser open" surprise:
        # click Start, service goes running, then any reflexive left-
        # click on the tray icon → browser opens. Removed deliberately;
        # the user has to choose Open WebUI from the menu explicitly.
        # Worth it for predictable behavior — DO NOT re-add `default=`.
        Item(open_label, _tray_open, enabled=is_running_state),
        Item("⧉  Copy URL", _tray_copy_url),
    ]
    # The "Auto-start on launch" and "Open browser when ready" toggle
    # items used to sit here, backed by ~/ViralMint/launcher.json.
    # Removed — the launcher now always auto-starts the service and always
    # opens the browser when the service becomes ready. Two fewer surfaces
    # to misconfigure; non-technical users never had to ask "should I
    # check that?".
    if _gui_callback:
        items.append(pystray.Menu.SEPARATOR)
        items.append(Item("▢  Show Launcher", _tray_show))
    items.append(pystray.Menu.SEPARATOR)
    items.append(Item("⏻  Quit ViralMint", _tray_quit))
    return pystray.Menu(*items)


def create_tray():
    """Create the system tray icon (call .run() to start it).

    Initial icon reflects whatever state the backend is actually in
    when the launcher boots (not always "stopped" — the user may have
    a running server from a previous session).
    """
    global _tray_icon, _last_tray_state
    import pystray

    initial_state = _get_state()
    _tray_icon = pystray.Icon(
        APP_NAME,
        _get_tray_icon(initial_state),
        f"{APP_NAME} — Stopped",  # title is overwritten by _update_tray_title below
        menu=_build_tray_menu(),
    )
    _last_tray_state = initial_state
    _update_tray_title()
    if initial_state == "starting":
        _start_starting_animation()
    return _tray_icon


def _update_tray_title():
    """Keep the tray tooltip text in sync with status. Version (read from
    pyproject.toml at module load) is appended so the user sees what release
    they're on without having to open the web UI."""
    if not _tray_icon:
        return
    state = _get_state()
    ver = f" · v{VERSION}" if VERSION else ""
    if state == "running":
        _tray_icon.title = f"{APP_NAME} — Running (port {PORT}){ver}"
    elif state == "starting":
        _tray_icon.title = f"{APP_NAME} — Starting…{ver}"
    else:
        _tray_icon.title = f"{APP_NAME} — Stopped{ver}"


# ─── GUI Window (tkinter — optional) ──────────────────────────────────────────

def _run_with_gui():
    """Launch with tkinter window + system tray."""
    import tkinter as tk
    from tkinter import font as tkfont

    root = tk.Tk()
    root.title(f"{APP_NAME} Launcher")
    root.resizable(False, False)

    win_w, win_h = 400, 360
    sx, sy = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{win_w}x{win_h}+{(sx - win_w) // 2}+{(sy - win_h) // 3}")

    # ── Theme ──
    # Warm-glass aesthetic matching the web UI. tkinter has no dark-mode
    # autoswitch and this surface is dev-only (frozen builds skip tk
    # entirely), so we ship the light variant only.
    BG       = "#f5f0ea"   # warm cream — matches body bg in light mode
    BG_CARD  = "#ffffff"
    BORDER   = "#e0d8d0"   # warm divider
    GRAY     = "#a8a09a"   # warm muted
    TEXT_DIM = "#6b6560"   # warm secondary
    # The body of _run_with_gui references GREEN/RED/BLUE as role names —
    # bound to the warm palette so the color values live in one place.
    GREEN    = "#c96442"   # terracotta — primary action (Start)
    RED      = "#dc2626"   # danger — Stop
    BLUE     = "#0097a7"   # teal — secondary action (Open WebUI)

    root.configure(bg=BG)

    # Window icon
    if ICON_PATH.exists():
        try:
            _tk_icon = tk.PhotoImage(file=str(ICON_PATH))
            root.iconphoto(True, _tk_icon)
        except Exception:
            pass

    # Fonts
    title_font = tkfont.Font(family="Helvetica Neue", size=20, weight="bold")
    status_font = tkfont.Font(family="Helvetica Neue", size=11)
    btn_font = tkfont.Font(family="Helvetica Neue", size=13, weight="bold")
    foot_font = tkfont.Font(family="Helvetica Neue", size=9)

    # ── Header ──
    hdr = tk.Frame(root, bg=BG)
    hdr.pack(fill="x", padx=32, pady=(28, 0))
    tk.Label(hdr, text=APP_NAME, font=title_font, fg=GREEN, bg=BG).pack(side="left")
    # Version badge — pulled from pyproject.toml so this never drifts.
    # Hides itself if version lookup failed.
    ver_text = f"v{VERSION}" if VERSION else ""
    if ver_text:
        ver = tk.Label(hdr, text=ver_text, font=foot_font, fg=GRAY, bg=BG)
        ver.pack(side="left", padx=(8, 0), pady=(6, 0))

    # ── Status bar ──
    sf = tk.Frame(root, bg=BG)
    sf.pack(fill="x", padx=32, pady=(14, 0))

    dot_cv = tk.Canvas(sf, width=14, height=14, bg=BG, highlightthickness=0)
    dot_cv.pack(side="left", padx=(0, 8))
    dot_id = dot_cv.create_oval(2, 2, 12, 12, fill=GRAY, outline="")

    status_lbl = tk.Label(sf, text="Service stopped", font=status_font, fg=TEXT_DIM, bg=BG)
    status_lbl.pack(side="left")

    # ── Card ──
    card = tk.Frame(root, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1)
    card.pack(fill="x", padx=32, pady=(22, 0))
    inner = tk.Frame(card, bg=BG_CARD)
    inner.pack(padx=22, pady=22, fill="x")

    # ── UI update helper ──
    # Hover/active variants of the role colors. Slightly darker so the
    # button feels pressed without changing hue.
    GREEN_H = "#b85838"   # terracotta darker
    RED_H   = "#b91c1c"
    BLUE_H  = "#00838f"   # teal darker
    DISABLED_BG = "#ede5dc"   # warm-gray surface for disabled buttons
    DISABLED_FG = "#bdb4ac"

    def set_ui(state):
        """state: 'running' | 'stopped' | 'starting' | 'failed'"""
        if state == "running":
            dot_cv.itemconfig(dot_id, fill=GREEN)
            status_lbl.config(text=f"Service running on port {PORT}", fg=GREEN)
            ss_btn.config(text="Stop Service", bg=RED, activebackground=RED_H, state="normal")
            web_btn.config(state="normal", bg=BLUE, fg="#ffffff", activebackground=BLUE_H)
            restart_lbl.config(fg=TEXT_DIM, cursor="hand2")
        elif state == "stopped":
            dot_cv.itemconfig(dot_id, fill=GRAY)
            status_lbl.config(text="Service stopped", fg=TEXT_DIM)
            ss_btn.config(text="Start Service", bg=GREEN, activebackground=GREEN_H, state="normal")
            web_btn.config(state="disabled", bg=DISABLED_BG, fg=DISABLED_FG)
            restart_lbl.config(fg=DISABLED_FG, cursor="arrow")
        elif state == "starting":
            dot_cv.itemconfig(dot_id, fill="#fb923c")  # Tailwind orange-400 (matches tray pulse)
            status_lbl.config(text="Starting service...", fg="#fb923c")
            ss_btn.config(text="Starting...", state="disabled")
            restart_lbl.config(fg=DISABLED_FG, cursor="arrow")
        elif state == "failed":
            dot_cv.itemconfig(dot_id, fill=RED)
            status_lbl.config(text="Failed to start service", fg=RED)
            ss_btn.config(text="Start Service", bg=GREEN, activebackground=GREEN_H, state="normal")
            restart_lbl.config(fg=DISABLED_FG, cursor="arrow")
        _update_tray_title()

    # ── Button handlers ──
    def on_toggle():
        if is_running():
            set_ui("starting")
            root.update()
            stop_service()
            set_ui("stopped")
            _refresh_tray()
        else:
            set_ui("starting")
            root.update()
            ok = start_service()
            if ok:
                def _wait():
                    ready = wait_for_server(30)
                    root.after(0, lambda: _finish_start(ready))
                threading.Thread(target=_wait, daemon=True).start()
            else:
                set_ui("failed")

    def _finish_start(ready):
        set_ui("running" if ready else "failed")
        _refresh_tray()
        # Always open the browser after a successful manual Start from
        # the tk window's button — same rationale as _tray_start above.
        _open_browser_on_ready(ready)

    # ── Start/Stop button — primary action, fg=white on terracotta ──
    ss_btn = tk.Button(
        inner, text="Start Service", font=btn_font,
        bg=GREEN, fg="#ffffff", activebackground=GREEN_H, activeforeground="#ffffff",
        relief="flat", cursor="hand2", bd=0, pady=12,
        command=on_toggle,
    )
    ss_btn.pack(fill="x", pady=(0, 12))

    # ── Open WebUI button — secondary action ──
    web_btn = tk.Button(
        inner, text="Open WebUI", font=btn_font,
        bg=DISABLED_BG, fg=DISABLED_FG,
        activebackground=BLUE_H, activeforeground="#ffffff",
        relief="flat", cursor="hand2", bd=0, pady=12, state="disabled",
        command=lambda: open_webui() if is_running() else None,
    )
    web_btn.pack(fill="x")

    # ── Action row (restart · copy URL) ──
    def on_restart():
        if not is_running():
            return
        set_ui("starting")
        threading.Thread(target=_do_restart, daemon=True, name="launcher-restart").start()

    def _flash(label, old_text, new_text):
        """Brief text flash for copy/open feedback."""
        label.config(text=new_text)
        root.after(1100, lambda: label.config(text=old_text))

    def on_copy():
        ok = _copy_to_clipboard(URL)
        _flash(copy_lbl, "⧉  Copy URL", "✓  Copied!" if ok else "✕  Copy failed")

    actions = tk.Frame(inner, bg=BG_CARD)
    actions.pack(fill="x", pady=(12, 0))

    def _mk_link(parent, text, on_click, disabled=False):
        color = DISABLED_FG if disabled else TEXT_DIM
        lbl = tk.Label(
            parent, text=text, font=foot_font, fg=color, bg=BG_CARD,
            cursor="arrow" if disabled else "hand2",
        )
        lbl.bind("<Button-1>", lambda _e: on_click())
        lbl.bind("<Enter>", lambda _e: lbl.config(fg=GREEN) if lbl.cget("cursor") == "hand2" else None)
        lbl.bind("<Leave>", lambda _e: lbl.config(fg=TEXT_DIM) if lbl.cget("cursor") == "hand2" else None)
        return lbl

    def _bullet():
        tk.Label(actions, text="·", font=foot_font, fg=GRAY, bg=BG_CARD).pack(side="left", padx=6)

    restart_lbl = _mk_link(actions, "↻  Restart", on_restart, disabled=True)
    restart_lbl.pack(side="left")
    _bullet()
    copy_lbl = _mk_link(actions, "⧉  Copy URL", on_copy)
    copy_lbl.pack(side="left")

    # ── Footer ──
    tk.Label(
        root, text=f"{URL}  •  Close window to minimize to tray",
        font=foot_font, fg=TEXT_DIM, bg=BG,
    ).pack(side="bottom", pady=(0, 16))

    # ── Wire tray ↔ GUI ──
    global _gui_callback

    def gui_cb(event):
        if event == "quit":
            root.after(0, root.destroy)
        elif event == "show":
            root.after(0, root.deiconify)
        elif event == "starting":
            root.after(0, lambda: set_ui("starting"))
        elif event == "ready":
            root.after(0, lambda: set_ui("running"))
        elif event == "stopped":
            root.after(0, lambda: set_ui("stopped"))
        elif event == "failed":
            root.after(0, lambda: set_ui("failed"))

    _gui_callback = gui_cb

    # Start tray
    tray = create_tray()
    threading.Thread(target=tray.run, daemon=True).start()

    # Keep tray in sync with real backend state (external start/stop too)
    _start_state_poller()

    # Auto-start and auto-open the browser. Runs on a background thread so
    # the tk mainloop can paint the "Starting…" state without blocking on
    # wait_for_server.
    threading.Thread(
        target=_auto_start_and_open, daemon=True, name="launcher-auto",
    ).start()

    # Keep the tkinter UI in sync with real state too. Uses the same
    # tri-state source of truth as the tray (_get_state) so the GUI's
    # orange "Starting…" dot lights up correctly during boot AND when
    # the service is started from outside the launcher.
    gui_last_state = [None]

    def _gui_sync():
        # _get_state returns "starting"/"running"/"stopped" — the GUI's
        # set_ui() already accepts the same string vocabulary.
        real = _get_state()
        if real != gui_last_state[0]:
            gui_last_state[0] = real
            set_ui(real)
        # Faster polling while transitioning so the green dot lands
        # ASAP after /health goes 200; idle cadence stays at 2s.
        root.after(500 if real == "starting" else 2000, _gui_sync)

    # Sync immediately on window open — otherwise the GUI shows the
    # default "stopped" state for ~2s before the first poll lands, even
    # if the backend was already up when the launcher booted.
    root.after(0, _gui_sync)

    # ── Window close → minimize to tray ──
    def on_close():
        if is_running():
            root.withdraw()
        else:
            stop_service()
            if _tray_icon:
                _tray_icon.stop()
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()

    # Final cleanup
    stop_service()
    if _tray_icon:
        try:
            _tray_icon.stop()
        except Exception:
            pass


# ─── Tray-only fallback ───────────────────────────────────────────────────────

def _run_tray_only():
    """System tray only — when tkinter is not available (or when frozen,
    where tkinter is intentionally skipped).

    Auto-start fires AFTER the tray icon exists so the
    `_refresh_tray()` calls inside the auto-open chain land on a real
    icon, not a None reference. The state poller is started in the same
    window so the menu starts ticking immediately.
    """
    ver = f" v{VERSION}" if VERSION else ""
    print(f"\n  {APP_NAME} Launcher{ver} (tray mode)")
    print(f"  Right-click the tray icon to control the service.")
    print(f"  Service URL: {URL}\n")

    tray = create_tray()
    _start_state_poller()
    threading.Thread(
        target=_auto_start_and_open, daemon=True, name="launcher-auto",
    ).start()
    tray.run()  # blocks until Quit
    stop_service()


# ─── Single-instance + signal handling ────────────────────────────────────────

def _acquire_single_instance():
    """Bind a loopback sentinel socket so only one launcher runs at a time.

    Returns the bound socket (kept alive by the caller). Returns None if the
    port is already taken — meaning another launcher is live.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _LAUNCHER_SENTINEL_PORT))
        s.listen(1)
        return s
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return None


def _install_signal_handlers():
    """Ensure Ctrl+C / SIGTERM stops the backend instead of orphaning it."""
    import signal

    def _handle(signum, frame):
        try:
            stop_service()
        finally:
            sys.exit(0)

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            # Non-main thread on some platforms; safe to skip.
            pass


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.chdir(ROOT)

    _launcher_sentinel_sock = _acquire_single_instance()
    if _launcher_sentinel_sock is None:
        print(f"{APP_NAME} launcher is already running. Exiting.")
        sys.exit(1)

    _install_signal_handlers()
    _apply_platform_app_icon()

    # Packaged builds may ship with whatever Tcl/Tk was bundled on the CI
    # runner, which can crash on newer macOS versions. Product UX is tray +
    # system browser anyway, so the tkinter status window is dead weight when
    # frozen.
    if getattr(sys, "frozen", False):
        _run_tray_only()
    else:
        try:
            import tkinter  # noqa: F401
            _run_with_gui()
        except ImportError:
            print("Note: tkinter not available, using tray-only mode")
            _run_tray_only()
