# PyInstaller spec for ViralMint OSS — single-bundle desktop build.
#
# Builds one .app/.exe whose entry point is launcher.py: a system-tray app that
# supervises the FastAPI backend and opens the user's browser at
# http://127.0.0.1:16888. The bundle embeds the backend, the React SPA and a
# vendored ffprobe binary. At runtime the tray launcher re-invokes THIS binary
# with `--run-backend` to run uvicorn in a supervised child process — one binary
# that plays both roles, with the backend still in its own process so the tray's
# watchdog can restart it. User data lives in ~/ViralMint (or $VIRALMINT_DATA_DIR).
#
# Build with:
#   bash desktop/scripts/build-app.sh
# or directly:
#   pyinstaller desktop/scripts/viralmint.spec --noconfirm --clean
#
# Simpler than the private installer's two-stage (separate backend bundle +
# launcher) layout — OSS ships ONE binary that self-reinvokes via --run-backend.

# ruff: noqa
import os
import sys

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
)

PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
IS_DARWIN = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")

datas = []
binaries = []
hiddenimports = []

# faster-whisper bundles model configs + ctranslate2 native libs. Actual
# weights download on first use into VIRALMINT_DATA_DIR/whisper-cache.
for pkg in ("faster_whisper", "ctranslate2"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# neonize (WhatsApp). Ships ~30MB native Go library + generated protobuf
# stubs under neonize/proto/** that PyInstaller can't follow statically.
# google.protobuf is explicit because neonize's *_pb2.py files import
# runtime_version from it (only exists in protobuf >=5.26).
for pkg in ("neonize", "google.protobuf"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:
        print(f"WARN: collect_all({pkg!r}) failed — {pkg} may not work in packaged build: {exc}")

# moviepy → imageio_ffmpeg bundles static ffmpeg per platform.
d, b, h = collect_all("imageio_ffmpeg")
datas += d
binaries += b
hiddenimports += h

# Desktop tray launcher — launcher.py is the entry point (below). pystray picks
# its platform backend via lazy import, so PyInstaller's static analysis misses
# it; collect every backend. Pillow (the icon renderer) is handled by its own
# bundled hook. On macOS the Dock-icon + tray backend use pyobjc. tkinter is
# intentionally NOT bundled — the frozen launcher runs tray-only (see
# launcher.py __main__), which sidesteps the flaky bundled-Tcl/Tk on macOS.
hiddenimports += collect_submodules("pystray")
hiddenimports += [
    "pystray._darwin", "pystray._win32", "pystray._xorg", "pystray._appindicator",
]
if IS_DARWIN:
    hiddenimports += ["AppKit", "Foundation", "objc"]

# Playwright Python lib only — Chromium browser (~450MB) is NEVER bundled.
# (OSS doesn't currently use playwright at runtime, but it's a transitive
# dep of tiktok-uploader, so collect it defensively. Filter out any
# locally-cached browser snapshot that might be on the build machine.)
try:
    d, b, h = collect_all("playwright")
    def _is_browser_blob(entry):
        src = entry[0] if isinstance(entry, tuple) else entry
        return any(m in str(src) for m in (".local-browsers", ".cache", "/browsers/", "chromium-"))
    d = [e for e in d if not _is_browser_blob(e)]
    b = [e for e in b if not _is_browser_blob(e)]
    datas += d
    binaries += b
    hiddenimports += h
    hiddenimports += ["playwright.async_api", "playwright.sync_api"]
except Exception as exc:
    print(f"WARN: collect_all('playwright') failed: {exc}")

# ffprobe is NOT shipped by imageio_ffmpeg — desktop/scripts/fetch-ffprobe.sh
# vendors it before this spec runs. Bundle it as a top-level binary so
# backend services (thumbnail_service, ffmpeg_service, ytdlp_service,
# clip_extractor) can find it on PATH at startup.
_ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
_ffprobe_src = os.path.join(PROJECT_ROOT, "desktop", "scripts", "vendor", _ffprobe_name)
if os.path.exists(_ffprobe_src):
    binaries.append((_ffprobe_src, "."))
else:
    print(f"WARN: {_ffprobe_src} not found — falling back to system ffprobe in packaged build")

# Frontend SPA — backend/main.py reads VIRALMINT_FRONTEND_DIST, which the
# launcher's --run-backend child sets to _MEIPASS/frontend/dist at runtime.
_frontend_dist = os.path.join(PROJECT_ROOT, "frontend", "dist")
if os.path.isdir(_frontend_dist):
    datas.append((_frontend_dist, "frontend/dist"))
else:
    raise SystemExit(
        f"ERROR: {_frontend_dist} is missing. Run "
        "`npm install && npm run build` inside frontend/ first."
    )

# Pure-import packages that are loaded dynamically — PyInstaller's static
# analysis can't follow them.
hiddenimports += [
    "aiosqlite",
    "yt_dlp",
    "yt_dlp.extractor",
    # AI provider SDKs — OSS uses BYOK, so the user's Anthropic / OpenAI
    # key calls these directly from the bundled backend (no cloud Lambda).
    "anthropic",
    "openai",
    "google.api_core",
    "telegram",
    "telegram.ext",
    "discord",
    "slack_sdk",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]
hiddenimports += collect_submodules("yt_dlp.extractor")

# Ship the entire backend/ tree so dynamic imports (services, agents,
# messaging channels, models) work in the frozen bundle.
datas += collect_data_files("backend")

# Tray icon — launcher.py loads it from _MEIPASS/icon-192.png (ICON_PATH) to
# draw the system-tray glyph and set the macOS Dock icon.
_icon_src = os.path.join(PROJECT_ROOT, "frontend", "public", "icon-192.png")
if os.path.exists(_icon_src):
    datas.append((_icon_src, "."))

block_cipher = None

a = Analysis(
    [os.path.join(PROJECT_ROOT, "launcher.py")],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Don't exclude `unittest` — yt-dlp and some google-api-client modules do
    # `from unittest.mock import ...` at import time.
    excludes=["tkinter", "pydoc_data"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Platform-specific icon for the EXE stub.
_exe_icon = None
_mac_icns = os.path.join(PROJECT_ROOT, "desktop", "build", "icon.icns")
_win_ico = os.path.join(PROJECT_ROOT, "desktop", "build", "icon.ico")
if IS_DARWIN and os.path.exists(_mac_icns):
    _exe_icon = _mac_icns
elif IS_WINDOWS and os.path.exists(_win_ico):
    _exe_icon = _win_ico

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ViralMint",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windowed on macOS/Windows so no console window pops up. The browser
    # is the UI, not a terminal.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=os.environ.get("CODESIGN_IDENTITY"),
    entitlements_file=os.environ.get("APPLE_ENTITLEMENTS"),
    icon=_exe_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ViralMint",
)

# macOS .app bundle wrapping the onedir.
if IS_DARWIN:
    app = BUNDLE(
        coll,
        name="ViralMint.app",
        icon=_mac_icns if os.path.exists(_mac_icns) else None,
        bundle_identifier="net.viralmint.app",
        info_plist={
            "CFBundleName": "ViralMint",
            "CFBundleDisplayName": "ViralMint",
            "CFBundleShortVersionString": os.environ.get("VIRALMINT_VERSION", "0.1.0"),
            "CFBundleVersion": os.environ.get("VIRALMINT_VERSION", "0.1.0"),
            # Show in Dock — single-window app, browser is the UI.
            "LSUIElement": False,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
        },
    )
