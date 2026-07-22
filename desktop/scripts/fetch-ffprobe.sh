#!/usr/bin/env bash
# Download a static ffprobe binary matching the host platform.
#
# Why: imageio_ffmpeg (our bundled ffmpeg source) only ships ffmpeg, not
# ffprobe. Several backend services (thumbnail_service, ffmpeg_service,
# ytdlp_service, clip_extractor) shell out to "ffprobe" and fail in the
# packaged desktop build without it.
#
# Output: desktop/scripts/vendor/ffprobe  (or ffprobe.exe on Windows)
#         PyInstaller picks it up via viralmint.spec's `binaries` list.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENDOR="$REPO_ROOT/desktop/scripts/vendor"
mkdir -p "$VENDOR"

OS="$(uname -s)"
ARCH="$(uname -m)"

# Skip if we already fetched a valid binary — keeps local rebuilds fast.
if [[ -x "$VENDOR/ffprobe" ]] || [[ -f "$VENDOR/ffprobe.exe" ]]; then
  echo "==> ffprobe already vendored at $VENDOR, skipping download"
  ls -lh "$VENDOR"/ffprobe* 2>/dev/null || true
  exit 0
fi

case "$OS" in
  Darwin)
    # osxexperts.net is maintained by the same author as evermeet.cx and
    # publishes per-arch ffprobe zips. evermeet.cx itself only builds x86_64.
    if [[ "$ARCH" == "arm64" ]]; then
      URL="https://www.osxexperts.net/ffprobe71arm.zip"
    else
      URL="https://www.osxexperts.net/ffprobe71intel.zip"
    fi
    echo "==> Fetching macOS ffprobe ($ARCH) from $URL"
    curl -fL --retry 3 -o "$VENDOR/ffprobe.zip" "$URL"
    unzip -o -q "$VENDOR/ffprobe.zip" -d "$VENDOR"
    rm "$VENDOR/ffprobe.zip"
    chmod +x "$VENDOR/ffprobe"
    ;;

  Linux)
    # Static linux64 ffprobe. Prefer BtbN's GitHub-hosted build (reliable +
    # UA-agnostic); fall back to johnvansickle.com (the classic mirror, but it
    # intermittently 4xx's CI runners — a 415 there is what broke the build).
    # A browser UA avoids the picky-WAF rejections both hosts can throw.
    UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    BTBN="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
    JVS="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    fetched=0
    for URL in "$BTBN" "$JVS"; do
      echo "==> Fetching Linux static ffprobe from $URL"
      if curl -fL --retry 3 --retry-all-errors -A "$UA" -o "$VENDOR/ff.tar.xz" "$URL"; then
        fetched=1; break
      fi
      echo "==> source failed, trying next"
    done
    [[ "$fetched" == "1" ]] || { echo "fetch-ffprobe.sh: all Linux ffprobe sources failed" >&2; exit 1; }
    tar -xJf "$VENDOR/ff.tar.xz" -C "$VENDOR"
    # Archive layout differs per source (ffmpeg-*-amd64-static/ vs
    # ffmpeg-*-linux64-gpl/bin/) — locate ffprobe wherever it landed.
    FF="$(find "$VENDOR" -type f -name ffprobe 2>/dev/null | head -1)"
    [[ -n "$FF" ]] || { echo "fetch-ffprobe.sh: ffprobe not found in archive" >&2; exit 1; }
    mv "$FF" "$VENDOR/ffprobe"
    chmod +x "$VENDOR/ffprobe"
    rm -f "$VENDOR/ff.tar.xz"
    find "$VENDOR" -maxdepth 1 -type d -name 'ffmpeg-*' -exec rm -rf {} +
    ;;

  MINGW*|MSYS*|CYGWIN*)
    # gyan.dev essentials: the de-facto Windows static build. Ships ffprobe.exe
    # inside ffmpeg-<version>-essentials_build/bin/.
    URL="https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    echo "==> Fetching Windows ffprobe from $URL"
    curl -fL --retry 3 -o "$VENDOR/ffmpeg-win.zip" "$URL"
    unzip -o -q "$VENDOR/ffmpeg-win.zip" -d "$VENDOR"
    mv "$VENDOR"/ffmpeg-*-essentials_build/bin/ffprobe.exe "$VENDOR/ffprobe.exe"
    rm -rf "$VENDOR"/ffmpeg-*-essentials_build "$VENDOR/ffmpeg-win.zip"
    ;;

  *)
    echo "fetch-ffprobe.sh: unsupported OS: $OS" >&2
    exit 1
    ;;
esac

echo "==> ffprobe vendored:"
ls -lh "$VENDOR"/ffprobe*
