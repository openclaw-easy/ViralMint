#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
ViralMint entry point.
Run: python run.py
Starts FastAPI server and opens the browser.
"""
import sys
import os
import time
import signal
import platform
import shutil
import subprocess
import threading
import webbrowser
from pathlib import Path

# Ensure we're running from project root
ROOT = Path(__file__).parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# ─── Startup checks ───────────────────────────────────────────────────────────

def check_python_version():
    if sys.version_info < (3, 11):
        print(f"❌ Python 3.11+ required. You have {sys.version}")
        sys.exit(1)
    print(f"✅ Python {sys.version.split()[0]}")

def check_env_file():
    env_path = ROOT / ".env"
    if not env_path.exists():
        example = ROOT / ".env.example"
        if example.exists():
            shutil.copy(example, env_path)
            print("📋 Created .env from .env.example — please fill in your API keys")
        else:
            print("⚠️  No .env file found. Create one from .env.example")

def ensure_storage_dirs():
    for d in ["videos", "audio", "generated", "thumbnails", "tmp"]:
        (ROOT / "storage" / d).mkdir(parents=True, exist_ok=True)
    print("✅ Storage directories ready")

def check_imagemagick():
    if shutil.which("magick") or shutil.which("convert"):
        print("✅ ImageMagick found")
        return
    system = platform.system()
    instructions = {
        "Darwin":  "brew install imagemagick",
        "Linux":   "sudo apt install imagemagick",
        "Windows": "Download from https://imagemagick.org/script/download.php\n"
                   "           (choose ImageMagick-7.x-Q16-x64-static.exe)",
    }
    print(f"""
❌ ImageMagick not found — required for video subtitle rendering.
   Install it:
     {instructions.get(system, 'See https://imagemagick.org/script/download.php')}
   Then re-run: python run.py
""")
    sys.exit(1)

def check_node():
    if not shutil.which("node"):
        print("❌ Node.js not found — required to build the frontend.")
        print("   Install from https://nodejs.org (v18+)")
        sys.exit(1)
    result = subprocess.run(["node", "--version"], capture_output=True, text=True)
    print(f"✅ Node.js {result.stdout.strip()}")

def build_frontend():
    """Install npm deps and build if dist/ doesn't exist or is stale."""
    fe_dir = ROOT / "frontend"
    dist_dir = fe_dir / "dist"
    if not (fe_dir / "node_modules").exists():
        print("📦 Installing frontend dependencies...")
        subprocess.run(["npm", "install"], cwd=fe_dir, check=True)
    if not dist_dir.exists():
        print("🏗️  Building frontend...")
        subprocess.run(["npm", "run", "build"], cwd=fe_dir, check=True)
        print("✅ Frontend built")

# ─── Process management ───────────────────────────────────────────────────────

processes = []

def start_uvicorn(host="127.0.0.1", port=16888, reload=False):
    cmd = [
        sys.executable, "-m", "uvicorn",
        "backend.main:app",
        f"--host={host}",
        f"--port={port}",
    ]
    if reload:
        cmd.append("--reload")
    p = subprocess.Popen(cmd, cwd=ROOT)
    processes.append(p)
    print(f"✅ API server started at http://localhost:{port} (PID {p.pid})")
    return p

def open_browser(port=16888, delay=2.5):
    # The launcher owns browser-opening (it sets VIRALMINT_NO_BROWSER=1 and
    # opens the tab itself once /health goes 200). When launched by the tray
    # launcher, skip run.py's own auto-open so the user doesn't get two tabs.
    # Standalone `python run.py` (var unset) keeps the original behavior.
    if os.environ.get("VIRALMINT_NO_BROWSER"):
        return
    def _open():
        time.sleep(delay)
        webbrowser.open(f"http://localhost:{port}")
    threading.Thread(target=_open, daemon=True).start()

def shutdown(sig=None, frame=None):
    print("\n🛑 Shutting down ViralMint...")
    for p in processes:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            p.kill()
    sys.exit(0)

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("\n🔥 ViralMint starting up...\n")

    check_python_version()
    check_env_file()
    ensure_storage_dirs()
    check_imagemagick()
    check_node()

    # Run DB migrations (create tables)
    from backend.database import init_db
    import asyncio
    asyncio.run(init_db())
    print("✅ Database ready")

    build_frontend()

    port = int(os.getenv("PORT", "16888"))
    dev_mode = os.getenv("DEBUG", "true").lower() == "true"
    start_uvicorn(port=port, reload=dev_mode)
    open_browser(port=port)

    print(f"\n✨ ViralMint running at http://localhost:{port}\n")
    print("   Press Ctrl+C to stop\n")

    # Keep alive — wait for child processes
    try:
        for p in processes:
            p.wait()
    except KeyboardInterrupt:
        shutdown()
