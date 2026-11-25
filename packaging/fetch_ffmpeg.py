"""Download static FFmpeg + FFprobe builds for the current platform into build/ffmpeg.

This is run by the release workflow on GitHub's build machines so that the
release packages work without a separate FFmpeg installation.

Sources (both publish ffmpeg *and* ffprobe as stand-alone executables):
  * Windows x64, Linux x64 / arm64: BtbN/FFmpeg-Builds, LGPL variant
    https://github.com/BtbN/FFmpeg-Builds
  * macOS arm64:                     Martin Riedl's static builds
    https://ffmpeg.martin-riedl.de
  * macOS x86_64 (Intel):            evermeet.cx static builds
    https://evermeet.cx/ffmpeg/

Usage: python packaging/fetch_ffmpeg.py [--dest DIR]
"""

import argparse
import io
import os
import platform
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile

BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
RIEDL = "https://ffmpeg.martin-riedl.de/redirect/latest/macos/{arch}/release/{tool}.zip"
EVERMEET_INFO = "https://evermeet.cx/ffmpeg/info/{tool}/release"
# When evermeet.cx refuses (it answered HTTP 429 for half an hour to GitHub's macOS
# runners on 2026-11-25), the same evermeet.cx binaries are taken from this
# repository's latest release, whose Rust macOS Intel package carries them unchanged.
REPO = "SU2H1/Universal-Music-Player"
LATEST_RELEASE = "https://github.com/{repo}/releases/latest"
RELEASE_ASSET = "https://github.com/{repo}/releases/download/{tag}/UniversalMusicPlayer-Rust-{version}-macos-x86_64.zip"
EVERMEET_ATTEMPTS = 3   # then the release's copy
EVERMEET_MAX_WAIT_S = 60

NOTICE = """FFmpeg is bundled with this package as separate, unmodified executables
(ffmpeg, ffprobe) that Real-Hi-Res runs as external programs.

FFmpeg is free software licensed under the LGPL v2.1+ (Windows/Linux builds from
BtbN/FFmpeg-Builds, LGPL variant) or the GPL (macOS builds from
ffmpeg.martin-riedl.de for Apple Silicon and evermeet.cx for Intel).
FFmpeg source code: https://ffmpeg.org/download.html
Build recipes: https://github.com/BtbN/FFmpeg-Builds, https://ffmpeg.martin-riedl.de
and https://evermeet.cx/ffmpeg/
FFmpeg is a trademark of Fabrice Bellard.
"""


ATTEMPTS = 6          # a download is tried this many times
MAX_WAIT_S = 300      # the longest wait between two tries (also for Retry-After)


def _wait_before_retry(error: Exception, attempt: int, max_wait: float = MAX_WAIT_S) -> float:
    """Seconds to wait after a failed try: the server's Retry-After for 429/503
    (evermeet.cx answers 429 when several CI jobs download at once), else
    15, 30, 60... seconds."""
    wait = 15.0 * 2 ** attempt
    headers = getattr(error, "headers", None)
    retry_after = headers.get("Retry-After") if headers is not None else None
    if retry_after:
        try:
            wait = max(float(retry_after), 1.0)
        except ValueError:
            pass
    return min(wait, max_wait)


def download(url: str, attempts: int = ATTEMPTS, max_wait: float = MAX_WAIT_S, final_url: list = None) -> bytes:
    """The body of `url`, tried `attempts` times after HTTP 429, 5xx or network
    errors.  `final_url`, when given, receives the address after redirects."""
    import time
    import urllib.error

    for attempt in range(attempts):
        print(f"Downloading {url}", flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "realhires-build"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
                if final_url is not None:
                    final_url.append(r.geturl())
                return data
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == attempts - 1:
                raise
            error: Exception = e
        except (urllib.error.URLError, OSError) as e:  # network trouble, a cut-off transfer
            if attempt == attempts - 1:
                raise
            error = e
        wait = _wait_before_retry(error, attempt, max_wait)
        print(f"  failed ({error}); trying again in {wait:.0f} s", flush=True)
        time.sleep(wait)
    raise AssertionError("unreachable")


def make_executable(path: str) -> None:
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _save(src, dest_dir: str, name: str, executable: bool) -> None:
    out_path = os.path.join(dest_dir, name)
    with open(out_path, "wb") as out:
        shutil.copyfileobj(src, out)
    if executable:
        make_executable(out_path)


def fetch_btbn_windows(dest: str) -> None:
    data = download(BTBN + "ffmpeg-master-latest-win64-lgpl.zip")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.namelist():
            base = os.path.basename(m)
            if base in ("ffmpeg.exe", "ffprobe.exe"):
                with z.open(m) as src:
                    _save(src, dest, base, executable=False)
            elif base == "LICENSE.txt":
                with z.open(m) as src:
                    _save(src, dest, "FFMPEG-LICENSE.txt", executable=False)


def fetch_btbn_linux(dest: str, arch: str) -> None:
    data = download(BTBN + f"ffmpeg-master-latest-{arch}-lgpl.tar.xz")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as t:
        for m in t.getmembers():
            base = os.path.basename(m.name)
            if not m.isfile():
                continue
            if base in ("ffmpeg", "ffprobe"):
                _save(t.extractfile(m), dest, base, executable=True)
            elif base == "LICENSE.txt":
                _save(t.extractfile(m), dest, "FFMPEG-LICENSE.txt", executable=False)


def fetch_riedl_macos(dest: str, arch: str) -> None:
    for tool in ("ffmpeg", "ffprobe"):
        data = download(RIEDL.format(arch=arch, tool=tool))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for m in z.namelist():
                if os.path.basename(m) == tool:
                    with z.open(m) as src:
                        _save(src, dest, tool, executable=True)


def fetch_evermeet_macos(dest: str) -> None:
    """Intel-only static builds that support older macOS than most others; when
    evermeet.cx refuses, the same builds from the latest release (`fetch_released_macos()`)."""
    import json
    import urllib.error

    try:
        for tool in ("ffmpeg", "ffprobe"):
            info = json.loads(download(EVERMEET_INFO.format(tool=tool), EVERMEET_ATTEMPTS, EVERMEET_MAX_WAIT_S))
            data = download(info["download"]["zip"]["url"], EVERMEET_ATTEMPTS, EVERMEET_MAX_WAIT_S)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for m in z.namelist():
                    if os.path.basename(m) == tool:
                        with z.open(m) as src:
                            _save(src, dest, tool, executable=True)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"evermeet.cx refused ({e}); taking its FFmpeg from the latest release instead", flush=True)
        fetch_released_macos(dest)


def released_asset(final_url: str, repo: str = REPO) -> str:
    """The Rust macOS Intel package of the release that `releases/latest` led to
    (".../releases/tag/v2.3.0" -> ".../download/v2.3.0/UniversalMusicPlayer-Rust-2.3.0-macos-x86_64.zip")."""
    tag = final_url.rstrip("/").rsplit("/", 1)[-1]
    if not tag.startswith("v") or "/releases/tag/" not in final_url:
        raise ValueError(f"no release tag in {final_url}")
    return RELEASE_ASSET.format(repo=repo, tag=tag, version=tag[1:])


def fetch_released_macos(dest: str, repo: str = REPO) -> None:
    """ffmpeg and ffprobe from the latest release's Rust macOS Intel package, which
    carries evermeet.cx's builds as they are (ad-hoc signed; the packaging signs
    them again).  check_compat.py then checks their architecture and minimum macOS."""
    where: list = []
    download(LATEST_RELEASE.format(repo=repo), final_url=where)
    url = released_asset(where[-1], repo)
    data = download(url)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for tool in ("ffmpeg", "ffprobe"):
            member = next((m for m in z.namelist() if m.endswith("/Contents/MacOS/" + tool)), None)
            if member is None:
                raise SystemExit(f"{url} holds no {tool}")
            with z.open(member) as src:
                _save(src, dest, tool, executable=True)
    print(f"FFmpeg taken from {url}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "..", "build", "ffmpeg"))
    args = ap.parse_args()
    dest = os.path.abspath(args.dest)
    os.makedirs(dest, exist_ok=True)

    machine = platform.machine().lower()
    if sys.platform == "win32":
        fetch_btbn_windows(dest)
    elif sys.platform == "darwin":
        if machine in ("arm64", "aarch64"):
            fetch_riedl_macos(dest, "arm64")
        else:
            fetch_evermeet_macos(dest)
    elif sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        fetch_btbn_linux(dest, "linux64")
    elif sys.platform.startswith("linux") and machine in ("aarch64", "arm64"):
        fetch_btbn_linux(dest, "linuxarm64")
    else:
        print(f"No prebuilt FFmpeg configured for {sys.platform}/{machine}", file=sys.stderr)
        return 1
    with open(os.path.join(dest, "FFMPEG-NOTICE.txt"), "w", encoding="utf-8") as f:
        f.write(NOTICE)
    found = sorted(os.listdir(dest))
    print("FFmpeg ready in", dest, "->", found)
    exe = ".exe" if sys.platform == "win32" else ""
    missing = [t for t in ("ffmpeg", "ffprobe") if t + exe not in found]
    if missing:
        print("ERROR: missing " + ", ".join(missing), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
