"""Album cover thumbnails for the album/artist views (FFmpeg only, no Pillow).

The cover is taken from the first track that has an embedded picture, or else
from a cover image in the album folder (cover.jpg, folder.jpg, front.jpg ...).
FFmpeg scales and crops it to a square PNG, which Tk can show directly (Tk
cannot decode JPEG by itself).

Covers are kept on disk (`cached_cover_png()`), one PNG per album and size in
`covers/` in the data folder (`cache_dir()`), shared by the Python and the Rust
app, so a cover is made by FFmpeg only once and not again after a restart.  An
album without a cover is remembered as an empty file.  The file name
(`cache_name()`) changes when one of the album's first four songs or one of
their folders changes, so a new cover image or a re-tagged song gives a new
entry; `trim_cache()` keeps the folder under `CACHE_LIMIT_BYTES` by deleting
the least recently used files.

For the Cover window (2.1), cover_source() says where that cover comes from and
gives the picture as it is stored, image_info() reads a JPEG or PNG header as
metaflac does, fit_png() makes the picture to show, and save_folder_cover()
writes cover.jpg (cover.png) into the album's folder.  Search Cover Online
(2.3) opens Google Images with the words of cover_search_url().
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from .probe import _no_window, tool

FOLDER_IMAGES = ("cover", "folder", "front", "album", "albumart", "albumartsmall", "artwork")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")

COVERS_ENV = "REALHIRES_COVERS"        # the covers folder (tests point it at a temporary one)
CACHE_VERSION = 1                      # part of every key: a new way of making covers gives new names
CACHE_PATHS = 4                        # the songs of an album that a cover is made from (and keyed by)
CACHE_LIMIT_BYTES = 500 * 1024 * 1024  # trim_cache() keeps the folder under this
TEMP_MAX_AGE_S = 3600                  # a temp file older than this was left by a crash
_BLOCK = 4096                          # trim_cache() counts each file as whole 4 KB blocks, as on disk
PNG_START = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def _thumbnail(source: str, size: int, embedded: bool) -> Tuple[Optional[bytes], bool]:
    """(PNG bytes or None, whether FFmpeg ran to the end).  False means it could
    not be started or timed out, so "no picture" is not certain."""
    vf = f"scale={size}:{size}:force_original_aspect_ratio=increase,crop={size}:{size}"
    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-i", source]
    cmd += (["-map", "0:v:0"] if embedded else []) + ["-an", "-frames:v", "1", "-vf", vf,
                                                       "-f", "image2pipe", "-c:v", "png", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30, **_no_window())
    except (OSError, subprocess.TimeoutExpired):
        return None, False
    data = proc.stdout
    return (data if proc.returncode == 0 and data.startswith(b"\x89PNG") else None), True


def folder_image(folder: str) -> Optional[str]:
    """Best cover-like image file in `folder`, if any."""
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    images = [n for n in names if os.path.splitext(n)[1].lower() in IMAGE_EXTS]
    for stem in FOLDER_IMAGES:
        for n in images:
            base = os.path.splitext(n)[0].lower()
            if base == stem or base.startswith(stem + "_") or base.startswith("albumart_"):
                return os.path.join(folder, n)
    return os.path.join(folder, images[0]) if len(images) == 1 else None


def _make(tracks: Sequence[str], size: int) -> Tuple[Optional[bytes], bool]:
    """(cover, certain): certain is False when an FFmpeg run failed to start or
    timed out on the way to "no cover"."""
    certain = True
    for t in tracks[:3]:
        data, ok = _thumbnail(t, size, embedded=True)
        certain = certain and ok
        if data:
            return data, True
    seen = set()
    for t in tracks:
        folder = os.path.dirname(t)
        if folder in seen:
            continue
        seen.add(folder)
        img = folder_image(folder)
        if img:
            data, ok = _thumbnail(img, size, embedded=False)
            certain = certain and ok
            if data:
                return data, True
    return None, certain


def cover_png(tracks: Iterable[str], size: int = 150) -> Optional[bytes]:
    """PNG bytes of a size×size cover for an album/artist, or None."""
    return _make(list(tracks), size)[0]


# ------------------------------------------------------------- on disk ---
def cache_dir() -> str:
    """`$REALHIRES_COVERS`, else `covers` in the data folder (`store.data_dir()`)."""
    env = os.environ.get(COVERS_ENV)
    if env:
        return env
    from .store import data_dir

    return os.path.join(data_dir(), "covers")


def _stat(path: str) -> Tuple[int, int]:
    """(size, st_mtime_ns) of a file or folder, (-1, -1) when it cannot be read."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return -1, -1
    return st.st_size, st.st_mtime_ns


def cache_key(paths: Iterable[str], size: int) -> str:
    """The text whose SHA-1 names a cover on disk.  Both versions build it
    byte for byte the same way (Rust: `covers::cache_key`), lines joined with
    "\\n" (no newline at the end), encoded as UTF-8:

        v1<TAB><size>                       CACHE_VERSION and the size in pixels
        <path><TAB><bytes><TAB><mtime_ns>   for each of the first four paths, in order
        <folder><TAB><mtime_ns>             for each distinct folder of those paths
                                            (os.path.dirname), in first-seen order

    Numbers are decimal integers; a file or folder that cannot be read gives
    -1 for its size and time.
    """
    paths = list(paths)[:CACHE_PATHS]
    lines = [f"v{CACHE_VERSION}\t{int(size)}"]
    folders: List[str] = []
    for p in paths:
        n, mtime = _stat(p)
        lines.append(f"{p}\t{n}\t{mtime}")
        folder = os.path.dirname(p)
        if folder not in folders:
            folders.append(folder)
    for folder in folders:
        lines.append(f"{folder}\t{_stat(folder)[1]}")
    return "\n".join(lines)


def cache_name(paths: Iterable[str], size: int) -> str:
    """The file name of a cover in the covers folder: the SHA-1 (40 lower-case
    hex digits) of `cache_key()`, then ".png"."""
    key = cache_key(paths, size).encode("utf-8", "surrogateescape")
    return hashlib.sha1(key).hexdigest() + ".png"


def cache_file(paths: Iterable[str], size: int) -> str:
    return os.path.join(cache_dir(), cache_name(paths, size))


def _valid(data: bytes) -> bool:
    """A whole PNG (signature and IEND chunk), or the empty "no cover" mark."""
    return not data or (data.startswith(PNG_START) and data.endswith(PNG_END))


def read_cached(file: str) -> Tuple[bool, Optional[bytes]]:
    """(found, cover) of one cache file; found is False when it is missing,
    unreadable or damaged.  Reading touches its modification time, which is
    what `trim_cache()` goes by."""
    try:
        with open(file, "rb") as fh:
            data = fh.read()
    except OSError:
        return False, None
    if not _valid(data):
        return False, None
    try:
        os.utime(file, None)
    except OSError:
        pass
    return True, data or None


def _write(file: str, data: bytes) -> None:
    """Write through a temp file and os.replace(), so a reader never sees half a
    file; failing to write only costs making the cover again next time."""
    folder = os.path.dirname(file)
    tmp = None
    try:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".cover-", suffix=".tmp", dir=folder)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, file)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass


def is_cached(paths: Iterable[str], size: int) -> bool:
    """Whether the covers folder has this cover (or its "no cover" mark)."""
    try:
        return os.path.isfile(cache_file(paths, size))
    except (OSError, ValueError):
        return False


def cached_cover_png(paths: Iterable[str], size: int) -> Optional[bytes]:
    """`cover_png()` of the first four paths, from the covers folder when it has
    it (None for an album remembered without a cover), else made with FFmpeg and
    saved there.  A damaged or unreadable file is made again.  When FFmpeg could
    not run, nothing is saved, so the album is tried again next time."""
    paths = list(paths)[:CACHE_PATHS]
    try:
        file: Optional[str] = cache_file(paths, size)
    except (OSError, ValueError):
        file = None
    if file is not None:
        found, data = read_cached(file)
        if found:
            return data
    data, certain = _make(paths, size)
    if file is not None and (data is not None or certain):
        _write(file, data or b"")
    return data


def _usage(n: int) -> int:
    return max(1, -(-n // _BLOCK)) * _BLOCK


def trim_cache(limit_bytes: int = CACHE_LIMIT_BYTES, folder: Optional[str] = None) -> int:
    """Delete the least recently used covers (oldest modification time first)
    until the folder holds at most `limit_bytes` (each file counted as whole
    4 KB blocks, an empty one as one block), and temp files older than an hour.
    Returns the number of covers deleted."""
    folder = folder or cache_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    now = time.time()
    entries = []
    for name in names:
        path = os.path.join(folder, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if name.endswith(".tmp"):
            if now - st.st_mtime > TEMP_MAX_AGE_S:
                try:
                    os.remove(path)
                except OSError:
                    pass
            continue
        if name.endswith(".png"):
            entries.append((st.st_mtime_ns, name, _usage(st.st_size)))
    total = sum(e[2] for e in entries)
    deleted = 0
    for _, name, used in sorted(entries):
        if total <= limit_bytes:
            break
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            continue
        total -= used
        deleted += 1
    return deleted


# ------------------------------------------------- Search Cover Online (2.3) ---
IMAGE_SEARCH_URL = "https://www.google.com/search"  # with tbm=isch: Google Images


def _part(value) -> str:
    """One part of a search: surrounding space removed, runs of white space made one space."""
    return " ".join(str(value or "").split())


def cover_search_query(album_artist: str = "", artist: str = "", album: str = "", title: str = "") -> str:
    """The words Search Cover Online looks for: "<album artist (else artist)> <album>", or, for
    a song without an album, "<artist (else album artist)> <title>"; unknown parts are left out."""
    album = _part(album)
    if album:
        parts = [_part(album_artist) or _part(artist), album]
    else:
        parts = [_part(artist) or _part(album_artist), _part(title)]
    return " ".join(p for p in parts if p)


def cover_search_url(album_artist: str = "", artist: str = "", album: str = "", title: str = "") -> str:
    """Google Images searching for the cover (`cover_search_query()`), as the browser opens it."""
    q = cover_search_query(album_artist, artist, album, title)
    return IMAGE_SEARCH_URL + "?" + urllib.parse.urlencode([("tbm", "isch"), ("q", q)])


__all__ = ["cover_png", "folder_image", "cache_dir", "cache_key", "cache_name", "cache_file", "cached_cover_png",
           "is_cached", "read_cached", "trim_cache", "COVERS_ENV", "CACHE_LIMIT_BYTES", "CACHE_PATHS"]
# ------------------------------------------------- inspecting a cover (2.1) ---
# The Cover window (docs/player-spec.md, "Inspecting and changing a cover") shows
# where the cover comes from and the picture as it is stored.  The Rust version
# (rust/crates/core/src/covers.rs) reads the same headers and gives the same texts.

@dataclass
class PictureInfo:
    """What a FLAC PICTURE block records about an image (metaflac's rules)."""
    mime: str      # "image/jpeg" or "image/png"
    width: int
    height: int
    depth: int     # bits per pixel
    colors: int    # the palette's size for an indexed PNG, else 0


def _be32(b: bytes, at: int) -> int:
    return int.from_bytes(b[at:at + 4], "big")


def _png_info(data: bytes) -> Optional[PictureInfo]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos, palette = 8, None
    while len(data) - pos > 12:
        clen, kind = _be32(data, pos), data[pos + 4:pos + 8]
        if kind == b"IHDR" and clen == 13 and len(data) >= pos + 21:
            w, h, bits, color = _be32(data, pos + 8), _be32(data, pos + 12), data[pos + 16], data[pos + 17]
            if color == 3:  # indexed: 8-bit samples, the palette's size from PLTE
                palette = (w, h)
            else:
                channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color, 0)
                return PictureInfo("image/png", w, h, bits * channels, 0)
        elif palette is not None and kind == b"PLTE":
            return PictureInfo("image/png", palette[0], palette[1], 24, clen // 3)
        if clen + 12 > len(data) - pos:
            return None
        pos += 12 + clen
    return None


_SOF = (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)


def _jpeg_info(data: bytes) -> Optional[PictureInfo]:
    if not data.startswith(b"\xff\xd8"):
        return None
    pos, n = 2, len(data)
    while True:
        while pos < n and data[pos] != 0xFF:   # the next marker
            pos += 1
        while pos < n and data[pos] == 0xFF:   # and its fill bytes
            pos += 1
        if pos >= n or data[pos] in (0xDA, 0xD9):  # start of scan or end of image: no size
            return None
        marker = data[pos]
        pos += 1
        if n - pos < 2:
            return None
        seg = (data[pos] << 8) | data[pos + 1]
        if marker in _SOF:
            if seg < 8 or n - pos < seg:
                return None
            h, w = (data[pos + 3] << 8) | data[pos + 4], (data[pos + 5] << 8) | data[pos + 6]
            return PictureInfo("image/jpeg", w, h, data[pos + 2] * data[pos + 7], 0)
        if seg < 2 or n - pos < seg:
            return None
        pos += seg


def image_info(data: bytes) -> Optional[PictureInfo]:
    """The type, size and depth of a JPEG or PNG image, read from its header (None
    for anything else, or a header that cannot be read)."""
    return _png_info(data) or _jpeg_info(data)


def image_format(data: bytes) -> str:
    """"JPEG", "PNG", "GIF", "BMP", "WebP", or "" for something else."""
    if data.startswith(b"\xff\xd8"):
        return "JPEG"
    if data.startswith(b"\x89PNG"):
        return "PNG"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if data[:2] == b"BM":
        return "BMP"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    return ""


MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "BMP": "image/bmp", "WebP": "image/webp"}


@dataclass
class CoverSource:
    """Where the cover shown for some songs comes from, and the picture as stored."""
    kind: str          # "embedded" (inside a song) or "folder" (an image file next to it)
    path: str          # the song, or the image file
    data: bytes = b""
    format: str = ""   # image_format()
    width: int = 0
    height: int = 0

    @property
    def mime(self) -> str:
        return MIME.get(self.format, "application/octet-stream")

    @property
    def ext(self) -> str:
        """The file extension to save the picture with (".jpg", ".png" ...)."""
        return {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "BMP": ".bmp", "WebP": ".webp"}.get(self.format, ".img")

    @property
    def where(self) -> str:
        """"Inside 01 Song.flac" or "cover.jpg in the album's folder"."""
        return where_text(self.kind, self.path)

    @property
    def details(self) -> str:
        """"1,200 × 1,200 pixels · JPEG · 245.3 KB"."""
        return details_text(self.width, self.height, self.format, len(self.data))


def where_text(kind: str, path: str) -> str:
    name = os.path.basename(path)
    return f"Inside {name}" if kind == "embedded" else f"{name} in the album's folder"


def details_text(width: int, height: int, fmt: str, size: int) -> str:
    from .fix import fmt_bytes

    parts = [f"{width:,} × {height:,} pixels"] if width and height else []
    return " · ".join(parts + [fmt or "Unknown format", fmt_bytes(size)])


def _embedded_picture(path: str) -> bytes:
    """The first picture inside a song as it is stored (FFmpeg's first video stream,
    which is what cover_png() shows), or b""."""
    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-i", path, "-map", "0:v:0", "-c", "copy",
           "-frames:v", "1", "-f", "image2pipe", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30, stdin=subprocess.DEVNULL, **_no_window())
    except (OSError, subprocess.TimeoutExpired):
        return b""
    return proc.stdout if proc.returncode == 0 else b""


def _probe_size(path: str) -> Tuple[int, int]:
    """Width and height of the first picture of a file, by ffprobe (images that are
    not JPEG or PNG)."""
    cmd = [tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
           "-of", "csv=p=0", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                              **_no_window())
        w, h = proc.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return 0, 0


def _source(kind: str, path: str, data: bytes) -> CoverSource:
    info = image_info(data)
    if info is not None:
        w, h = info.width, info.height
    else:
        w, h = _probe_size(path)
    return CoverSource(kind, path, data, image_format(data), w, h)


def cover_source(tracks: Iterable[str]) -> Optional[CoverSource]:
    """Where the cover that cover_png() shows for these songs comes from: the
    picture inside the first of the first three songs that has one, else the
    folder image, with the picture's bytes, size and format.  None when there is
    no cover."""
    tracks = list(tracks)
    for t in tracks[:3]:
        data = _embedded_picture(t)
        if data:
            return _source("embedded", t, data)
    seen = set()
    for t in tracks:
        folder = os.path.dirname(t)
        if folder in seen:
            continue
        seen.add(folder)
        img = folder_image(folder)
        if img:
            try:
                with open(img, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if data:
                return _source("folder", img, data)
    return None


def fit_png(data: bytes, size: int, enlarge: bool = False) -> Optional[bytes]:
    """The picture as a PNG that fits into size × size (aspect kept; a smaller one is
    enlarged only with `enlarge`), made by FFmpeg, for showing it (Tk cannot decode JPEG)."""
    if enlarge:
        vf = f"scale=w={size}:h={size}:force_original_aspect_ratio=decrease"
    else:
        vf = f"scale=w='min({size},iw)':h='min({size},ih)':force_original_aspect_ratio=decrease"
    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-i", "pipe:0", "-frames:v", "1", "-vf", vf,
           "-f", "image2pipe", "-c:v", "png", "-"]
    try:
        proc = subprocess.run(cmd, input=data, capture_output=True, timeout=30, **_no_window())
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout
    return out if proc.returncode == 0 and out.startswith(b"\x89PNG") else None


def folder_cover_name(folder: str, data: bytes) -> str:
    """Where "Also save it as cover.jpg in the album's folder" writes: cover.jpg, or
    cover.png for a PNG, keeping the spelling of one that is already there (Cover.JPG)."""
    name = "cover.png" if image_format(data) == "PNG" else "cover.jpg"
    try:
        for n in sorted(os.listdir(folder)):
            if n.lower() == name:
                return os.path.join(folder, n)
    except OSError:
        pass
    return os.path.join(folder, name)


def save_folder_cover(folder: str, data: bytes) -> str:
    """Write the picture as cover.jpg (cover.png) into the folder, through a temporary
    file; returns its path.  Raises OSError."""
    from .embed import replace_file

    path = folder_cover_name(folder, data)
    replace_file(path, data)
    return path


__all__ = ["cover_png", "folder_image", "image_info", "image_format", "cover_source", "CoverSource",
           "PictureInfo", "fit_png", "save_folder_cover", "folder_cover_name",
           "cover_search_query", "cover_search_url"]
