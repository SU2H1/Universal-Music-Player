#!/usr/bin/env bash
# Build, verify and package one release asset.  Used by every CI job (Windows
# via Git Bash, macOS, Linux inside an AlmaLinux 8 container) and usable locally.
#
#   ASSET=UniversalMusicPlayer-linux-x64 bash packaging/ci_build.sh
#
# Environment:
#   ASSET          name of the archive to produce (required); the version from
#                  realhires/__init__.py is inserted after "UniversalMusicPlayer-", so
#                  UniversalMusicPlayer-windows-x64 becomes UniversalMusicPlayer-1.8.1-windows-x64
#   PYTHON         python to use (default: python)
#   MACOS_MIN      macOS only: minimum macOS the package must support (default 12.0)
#   MAX_GLIBC      Linux only: newest glibc the package may require (default 2.28)
#   SKIP_FETCH=1   use an FFmpeg already in build/ffmpeg (or none)
#   SKIP_TESTS=1   skip pytest
# Output: out/<ASSET>.{zip,dmg,tar.gz}, e.g. out/UniversalMusicPlayer-1.8.1-linux-x64.tar.gz
set -euo pipefail
: "${ASSET:?set ASSET}"
PY="${PYTHON:-python}"
VERSION="$("$PY" -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('realhires/__init__.py').read()).group(1))")"
case "$ASSET" in
  UniversalMusicPlayer-"$VERSION"-*) ;;
  UniversalMusicPlayer-*) ASSET="UniversalMusicPlayer-$VERSION-${ASSET#UniversalMusicPlayer-}" ;;
esac
echo "Building $ASSET (version $VERSION)"
export REALHIRES_MACOS_MIN="${MACOS_MIN:-12.0}"
MAX_GLIBC="${MAX_GLIBC:-2.28}"
OS="$(uname -s)"
mkdir -p out

echo "::group::FFmpeg"
[ "${SKIP_FETCH:-0}" = 1 ] || "$PY" packaging/fetch_ffmpeg.py
export PATH="$PWD/build/ffmpeg:$PATH"
ffmpeg -hide_banner -version | sed -n 1p || true
echo "::endgroup::"

if [ "${SKIP_TESTS:-0}" != 1 ]; then
  echo "::group::Tests"
  "$PY" -m pytest -q
  echo "::endgroup::"
fi

echo "::group::PyInstaller"
"$PY" -m PyInstaller --noconfirm packaging/realhires.spec
# 2.1: the Japanese catalog must be inside the build, or the app silently stays English
if ! find dist -path '*realhires/assets/lang/ja.json' | grep -q .; then
    echo "ERROR: realhires/assets/lang/ja.json is missing from the build (packaging/realhires.spec)" >&2
    exit 1
fi
echo "::endgroup::"

case "$OS" in
  Darwin)
    APP=dist/UniversalMusicPlayer.app
    CLI="$APP/Contents/MacOS/realhires-cli"
    "$PY" packaging/check_compat.py "$APP" --max-macos "$REALHIRES_MACOS_MIN" --arch "$(uname -m)"
    # Apple Silicon refuses to run unsigned code, so the ad-hoc signature that
    # PyInstaller applies must be intact there.  Intel Macs run unsigned code.
    if codesign --verify --deep --strict "$APP"; then
      echo "Code signature (ad-hoc) is valid."
    elif [ "$(uname -m)" = arm64 ]; then
      echo "ERROR: invalid code signature on an arm64 build" >&2; exit 1
    else
      echo "Note: app is not signed (fine on Intel Macs)."
    fi
    ;;
  Linux)
    CLI=dist/UniversalMusicPlayer/realhires-cli
    "$PY" packaging/check_compat.py dist/UniversalMusicPlayer --max-glibc "$MAX_GLIBC"
    ;;
  *)
    CLI=dist/UniversalMusicPlayer/realhires-cli.exe
    "$PY" packaging/check_windows.py dist/UniversalMusicPlayer
    ;;
esac

echo "::group::Smoke test"
"$PY" packaging/smoke_test.py "$CLI"
echo "::endgroup::"

echo "::group::Package"
case "$OS" in
  Darwin)
    rm -rf pkg && mkdir -p pkg/UniversalMusicPlayer
    ditto "$APP" pkg/UniversalMusicPlayer/UniversalMusicPlayer.app
    cp packaging/README-RELEASE.txt pkg/UniversalMusicPlayer/README.txt
    cp realhires/assets/fonts/LICENSE-Inter.txt pkg/UniversalMusicPlayer/
    # Zip (keeps symlinks and permissions inside the .app)
    ditto -c -k --keepParent pkg/UniversalMusicPlayer "out/$ASSET.zip"
    # Disk image with the usual drag-to-Applications shortcut
    ln -s /Applications pkg/UniversalMusicPlayer/Applications
    for i in 1 2 3 4 5; do  # hdiutil is occasionally "Resource busy" on CI
      hdiutil create -volname "Universal Music Player" -srcfolder pkg/UniversalMusicPlayer -ov -format UDZO \
        "out/$ASSET.dmg" && break
      echo "hdiutil failed, retrying ($i)"; sleep $((i * 5))
    done
    [ -f "out/$ASSET.dmg" ]
    ;;
  Linux)
    cp packaging/README-RELEASE.txt dist/UniversalMusicPlayer/README.txt
    cp realhires/assets/fonts/LICENSE-Inter.txt dist/UniversalMusicPlayer/
    cp packaging/linux/install.sh packaging/linux/uninstall.sh dist/UniversalMusicPlayer/
    cp realhires/assets/icon.png dist/UniversalMusicPlayer/realhires.png
    chmod +x dist/UniversalMusicPlayer/install.sh dist/UniversalMusicPlayer/uninstall.sh
    tar -C dist -czf "out/$ASSET.tar.gz" UniversalMusicPlayer
    ;;
  *)
    cp packaging/README-RELEASE.txt dist/UniversalMusicPlayer/README.txt
    cp realhires/assets/fonts/LICENSE-Inter.txt dist/UniversalMusicPlayer/
    (cd dist && 7z a -tzip "../out/$ASSET.zip" UniversalMusicPlayer > /dev/null)
    ;;
esac
ls -la out
echo "::endgroup::"
