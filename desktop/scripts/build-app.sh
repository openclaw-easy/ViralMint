#!/usr/bin/env bash
# End-to-end OSS desktop build for ViralMint.
#
# Pipeline:
#   1. Build frontend (Vite) → frontend/dist/
#   2. Vendor ffprobe → desktop/scripts/vendor/ffprobe
#   3. Bundle everything (PyInstaller on viralmint.spec) → dist/ViralMint(.app)
#   4. Package per OS → desktop/release/ViralMint-<ver>-(mac.dmg|mac.zip|linux.tar.gz)
#
# Inputs (env):
#   PYTHON_BIN        Python 3.11+ interpreter (default: ./venv/bin/python).
#                     The script installs requirements.txt + PyInstaller into it.
#   VIRALMINT_VERSION Version string written into Info.plist (default: 0.1.0-dev).
#   SKIP_FRONTEND     "1" to reuse existing frontend/dist/.
#   SKIP_FFPROBE      "1" to reuse existing desktop/scripts/vendor/ffprobe.
#
# Output: desktop/release/ with .dmg + .zip on macOS, .tar.gz on Linux.
#
# The OSS build — one binary whose entry point is the launcher.py system-tray
# app; it re-invokes itself with --run-backend to run uvicorn, and the user's
# browser is the UI.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/venv/bin/python}"
VERSION="${VIRALMINT_VERSION:-0.1.0-dev}"
RELEASE_DIR="$REPO_ROOT/desktop/release"
SPEC="desktop/scripts/viralmint.spec"

OS="$(uname -s)"
case "$OS" in
  Darwin)               PLATFORM="mac" ;;
  Linux)                PLATFORM="linux" ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM="win" ;;
  *) echo "Unsupported OS: $OS" >&2; exit 1 ;;
esac

echo "==> Building ViralMint OSS v${VERSION} for ${PLATFORM}"
echo "==> Python: $($PYTHON_BIN --version) at $PYTHON_BIN"

# ─── 0. macOS code-signing keychain (skipped when secrets absent) ─────────────
if [[ "$PLATFORM" == "mac" && -n "${CSC_LINK:-}" && -n "${CSC_KEY_PASSWORD:-}" ]]; then
  echo "==> Setting up macOS code-signing keychain"
  KEYCHAIN_TMP="${RUNNER_TEMP:-/tmp}/viralmint-build.keychain-db"
  KEYCHAIN_PWD="$(openssl rand -hex 16)"
  CERT_P12="${RUNNER_TEMP:-/tmp}/viralmint-cert.p12"

  echo "$CSC_LINK" | base64 --decode > "$CERT_P12"
  security create-keychain -p "$KEYCHAIN_PWD" "$KEYCHAIN_TMP"
  security set-keychain-settings -lut 21600 "$KEYCHAIN_TMP"
  security unlock-keychain -p "$KEYCHAIN_PWD" "$KEYCHAIN_TMP"
  security import "$CERT_P12" -k "$KEYCHAIN_TMP" \
    -P "$CSC_KEY_PASSWORD" -T /usr/bin/codesign -T /usr/bin/security
  security set-key-partition-list -S apple-tool:,apple:,codesign: \
    -s -k "$KEYCHAIN_PWD" "$KEYCHAIN_TMP" >/dev/null
  security list-keychains -d user -s "$KEYCHAIN_TMP" \
    $(security list-keychains -d user | sed 's/[" ]//g')

  CODESIGN_IDENTITY="$(security find-identity -v -p codesigning "$KEYCHAIN_TMP" \
    | awk -F'"' '/Developer ID Application/ {print $2; exit}')"
  if [[ -z "$CODESIGN_IDENTITY" ]]; then
    echo "ERROR: no 'Developer ID Application' identity found in CSC_LINK" >&2
    exit 1
  fi
  export CODESIGN_IDENTITY
  export APPLE_ENTITLEMENTS="$REPO_ROOT/desktop/build/entitlements.mac.plist"
  echo "==> Signing identity: $CODESIGN_IDENTITY"
  rm -f "$CERT_P12"
fi

# ─── 1. Python deps ────────────────────────────────────────────────────────────
echo "==> Installing Python dependencies (requirements.txt + pyinstaller)"
$PYTHON_BIN -m pip install --upgrade pip
$PYTHON_BIN -m pip install -r requirements.txt
$PYTHON_BIN -m pip install "pyinstaller>=6.10,<7"

# ─── 2. Frontend ───────────────────────────────────────────────────────────────
if [[ "${SKIP_FRONTEND:-0}" != "1" ]]; then
  echo "==> Building frontend"
  pushd frontend >/dev/null
  if [[ ! -d node_modules ]]; then
    npm install
  fi
  npm run build
  popd >/dev/null
fi
[[ -d "frontend/dist" ]] || { echo "ERROR: frontend/dist missing after build" >&2; exit 1; }

# ─── 3. ffprobe vendor ─────────────────────────────────────────────────────────
if [[ "${SKIP_FFPROBE:-0}" != "1" ]]; then
  echo "==> Vendoring ffprobe"
  bash "$REPO_ROOT/desktop/scripts/fetch-ffprobe.sh"
fi

# ─── 4. Generate platform icons from icon.png ──────────────────────────────────
ICON_SRC="$REPO_ROOT/desktop/build/icon.png"
[[ -f "$ICON_SRC" ]] || { echo "ERROR: $ICON_SRC missing" >&2; exit 1; }

case "$PLATFORM" in
  mac)
    # icon.icns is shipped pre-built; regenerate only if missing or older
    # than icon.png so devs editing the source PNG see updates.
    ICNS="$REPO_ROOT/desktop/build/icon.icns"
    if [[ ! -f "$ICNS" || "$ICON_SRC" -nt "$ICNS" ]]; then
      echo "==> Generating icon.icns from icon.png"
      ICONSET="$(mktemp -d)/icon.iconset"
      mkdir -p "$ICONSET"
      for sz in 16 32 64 128 256 512; do
        sips -z $sz $sz "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
        sips -z $((sz*2)) $((sz*2)) "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
      done
      sips -z 1024 1024 "$ICON_SRC" --out "$ICONSET/icon_512x512@2x.png" >/dev/null
      iconutil -c icns "$ICONSET" -o "$ICNS"
      rm -rf "$(dirname "$ICONSET")"
    fi
    ;;
  win|linux)
    ICO="$REPO_ROOT/desktop/build/icon.ico"
    if [[ ! -f "$ICO" || "$ICON_SRC" -nt "$ICO" ]]; then
      echo "==> Generating icon.ico from icon.png"
      $PYTHON_BIN - "$ICON_SRC" "$ICO" <<'PY'
from PIL import Image
import sys
src, dest = sys.argv[1], sys.argv[2]
Image.open(src).save(dest, format="ICO",
    sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
PY
    fi
    ;;
esac

# ─── 5. Run PyInstaller ────────────────────────────────────────────────────────
echo "==> Cleaning previous build output"
rm -rf "$REPO_ROOT/build" "$REPO_ROOT/dist"

echo "==> Running PyInstaller"
VIRALMINT_VERSION="$VERSION" $PYTHON_BIN -m PyInstaller "$SPEC" --noconfirm --clean

# ─── 6. Package ────────────────────────────────────────────────────────────────
mkdir -p "$RELEASE_DIR"
rm -f "$RELEASE_DIR"/*.dmg "$RELEASE_DIR"/*.zip "$RELEASE_DIR"/*.tar.gz "$RELEASE_DIR"/*.exe || true

case "$PLATFORM" in
  mac)
    APP_PATH="dist/ViralMint.app"
    [[ -d "$APP_PATH" ]] || { echo "ERROR: $APP_PATH not produced" >&2; exit 1; }

    if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
      echo "==> Deep-signing $APP_PATH with hardened runtime"
      while IFS= read -r -d '' f; do
        codesign --force --options runtime --timestamp \
          --sign "$CODESIGN_IDENTITY" "$f" >/dev/null
      done < <(find "$APP_PATH" -type f \
        \( -name '*.dylib' -o -name '*.so' -o -name 'Python' \
           -o -name 'ViralMint' -o -name 'ffmpeg' -o -name 'ffprobe' \
           -o -name 'yt-dlp' \) -print0)
      codesign --force --deep --options runtime --timestamp \
        --entitlements "$APPLE_ENTITLEMENTS" \
        --sign "$CODESIGN_IDENTITY" "$APP_PATH"
      codesign --verify --strict --verbose=2 "$APP_PATH"
    else
      echo "==> Ad-hoc signing .app (no Developer ID — Gatekeeper will warn other users)"
      codesign --force --deep --sign - "$APP_PATH" || true
    fi

    ZIP_NAME="ViralMint-${VERSION}-mac.zip"
    echo "==> Zipping $APP_PATH → $RELEASE_DIR/$ZIP_NAME"
    (cd dist && ditto -c -k --keepParent "ViralMint.app" "$RELEASE_DIR/$ZIP_NAME")

    DMG_NAME="ViralMint-${VERSION}-mac.dmg"
    VOLICON="$REPO_ROOT/desktop/build/icon.icns"   # generated from icon.png above
    if command -v create-dmg >/dev/null 2>&1; then
      echo "==> Building DMG via create-dmg"
      create-dmg \
        --volname "ViralMint" \
        ${VOLICON:+--volicon "$VOLICON"} \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "ViralMint.app" 150 200 \
        --app-drop-link 450 200 \
        "$RELEASE_DIR/$DMG_NAME" \
        "$APP_PATH" || true
    else
      echo "==> Building DMG via hdiutil (with ViralMint volume icon)"
      STAGING=$(mktemp -d)
      cp -R "$APP_PATH" "$STAGING/"
      ln -s /Applications "$STAGING/Applications"
      # Give the mounted installer volume the ViralMint icon: drop
      # .VolumeIcon.icns at the volume root and set the volume's custom-icon
      # flag so Finder shows the logo instead of the generic disk icon.
      if [[ -f "$VOLICON" ]]; then
        cp "$VOLICON" "$STAGING/.VolumeIcon.icns"
      fi
      TMP_DMG="$(mktemp -u).dmg"
      hdiutil create -volname "ViralMint" -srcfolder "$STAGING" \
        -ov -format UDRW "$TMP_DMG" >/dev/null
      if [[ -f "$VOLICON" ]]; then
        MNT="$(mktemp -d)"
        if hdiutil attach "$TMP_DMG" -nobrowse -noautoopen -mountpoint "$MNT" >/dev/null; then
          # SetFile (Xcode CLT) marks the volume as having a custom icon.
          if command -v SetFile >/dev/null 2>&1; then SetFile -a C "$MNT" || true
          elif xcrun --find SetFile >/dev/null 2>&1; then xcrun SetFile -a C "$MNT" || true
          fi
          hdiutil detach "$MNT" >/dev/null 2>&1 || hdiutil detach "$MNT" -force >/dev/null 2>&1 || true
        fi
        rm -rf "$MNT"
      fi
      hdiutil convert "$TMP_DMG" -format UDZO -o "$RELEASE_DIR/$DMG_NAME" >/dev/null
      rm -f "$TMP_DMG"
      rm -rf "$STAGING"
    fi
    ;;

  linux)
    DIR="dist/ViralMint"
    [[ -d "$DIR" ]] || { echo "ERROR: $DIR not produced" >&2; exit 1; }
    TAR_NAME="ViralMint-${VERSION}-linux.tar.gz"
    echo "==> Tarballing $DIR → $RELEASE_DIR/$TAR_NAME"
    tar -czf "$RELEASE_DIR/$TAR_NAME" -C dist ViralMint
    ;;

  win)
    DIR="dist/ViralMint"
    [[ -d "$DIR" ]] || { echo "ERROR: $DIR not produced" >&2; exit 1; }
    ZIP_NAME="ViralMint-${VERSION}-win.zip"
    echo "==> Zipping $DIR → $RELEASE_DIR/$ZIP_NAME"
    (cd dist && zip -r "$RELEASE_DIR/$ZIP_NAME" ViralMint)
    ;;
esac

echo ""
echo "==> Build complete. Artifacts:"
ls -lh "$RELEASE_DIR"
