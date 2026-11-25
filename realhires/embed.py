"""Lyrics inside the audio file (2.0), the way LRCGET embeds them.

FLAC: the Vorbis comments ``LYRICS`` (synced, the LRC text) and
``UNSYNCEDLYRICS`` (plain).  MP3: the ID3v2 frames ``SYLT`` (synced, one entry
per timed line, milliseconds) and ``USLT`` (plain), language "XXX" and an empty
description.  Only the tag is rewritten: the audio frames are copied byte for
byte, through a temporary file that replaces the original, so a failure never
leaves half a file.  An empty value removes the field.  Other formats are not
supported (as in LRCGET); the caller saves an .lrc file for them instead.

The Rust version (`rust/crates/core/src/embed.rs`) writes byte-identical files,
which `tests/test_rust_parity.py` checks.
"""

from __future__ import annotations

import os
import re
import struct
import tempfile
from typing import List, Optional, Tuple

SUPPORTED = (".flac", ".mp3")
VENDOR = "Real-Hi-Res"
FLAC_SYNCED, FLAC_PLAIN = "LYRICS", "UNSYNCEDLYRICS"
LANG = b"XXX"
NEW_PADDING = 4096  # padding left after the tag when it has to grow
MAX_KEPT_PADDING = 1 << 20  # more room than this (a removed cover picture) is not kept as FLAC padding


class EmbedError(Exception):
    """The file's tag could not be read or written (the message says why)."""


def supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED


# ------------------------------------------------------------------- LRC ---
_STAMP = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def lrc_entries(synced: str) -> List[Tuple[int, str]]:
    """(milliseconds, text) of every timed line, sorted by time; header tags such as
    [ar: ...] are left out, and a line with several timestamps gives several entries."""
    out = []
    for line in synced.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stamps, rest = [], line
        while True:
            m = _STAMP.match(rest.lstrip())
            if not m:
                break
            frac = m.group(3) or "0"
            ms = int(m.group(1)) * 60000 + int(m.group(2)) * 1000 + int(frac.ljust(3, "0")[:3])
            stamps.append(ms)
            rest = rest.lstrip()[m.end():]
        if stamps:
            text = rest.strip()
            out.extend((ms, text) for ms in stamps)
    out.sort(key=lambda e: e[0])  # stable: lines with the same time keep their order
    return out


def looks_synced(text: str) -> bool:
    """Whether lyrics text is really LRC: at least half of its non-empty lines start with
    a timestamp.  Some programs keep LRC in a .txt file or in the plain-lyrics tag (USLT,
    UNSYNCEDLYRICS), and the player should still follow it line by line."""
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    timed = sum(1 for ln in lines if _STAMP.match(ln.lstrip()))
    return timed > 0 and timed * 2 >= len(lines)


# ------------------------------------------------------------------ FLAC ---
def _flac_blocks(data: bytes, start: int) -> Tuple[List[Tuple[int, bytes]], int]:
    """(type, body) of every metadata block, and the offset of the audio frames."""
    pos, blocks = start + 4, []
    while True:
        if pos + 4 > len(data):
            raise EmbedError("The FLAC file ends inside its metadata.")
        head = data[pos]
        kind, last = head & 0x7F, bool(head & 0x80)
        size = int.from_bytes(data[pos + 1:pos + 4], "big")
        body = data[pos + 4:pos + 4 + size]
        if len(body) != size:
            raise EmbedError("The FLAC file ends inside its metadata.")
        blocks.append((kind, body))
        pos += 4 + size
        if last:
            return blocks, pos


def _parse_vorbis(body: bytes) -> Tuple[bytes, List[bytes]]:
    try:
        vlen = struct.unpack_from("<I", body, 0)[0]
        vendor = body[4:4 + vlen]
        count = struct.unpack_from("<I", body, 4 + vlen)[0]
        pos, comments = 8 + vlen, []
        for _ in range(count):
            n = struct.unpack_from("<I", body, pos)[0]
            comments.append(body[pos + 4:pos + 4 + n])
            pos += 4 + n
    except struct.error as exc:
        raise EmbedError("The FLAC file's Vorbis comment is damaged.") from exc
    return vendor, comments


def _vorbis_body(vendor: bytes, comments: List[bytes]) -> bytes:
    out = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(comments))
    for c in comments:
        out += struct.pack("<I", len(c)) + c
    return out


def _comment_key(c: bytes) -> str:
    return c.split(b"=", 1)[0].decode("ascii", "replace").upper()


def flac_rewrite(data: bytes, edit, too_long: str = "The tag is too long for a FLAC file.") -> bytes:
    """The FLAC file with its Vorbis comments replaced by edit(comments) (a list of
    b"KEY=value"); every other metadata block and the audio stay as they are."""
    def edit_blocks(blocks):
        vendor, comments = VENDOR.encode(), []
        vi = next((i for i, (k, _) in enumerate(blocks) if k == 4), None)
        if vi is not None:
            vendor, comments = _parse_vorbis(blocks[vi][1])
        body = _vorbis_body(vendor, edit(comments))
        if vi is not None:
            blocks[vi] = (4, body)
        else:
            blocks.insert(1, (4, body))  # right after STREAMINFO
        return blocks

    return flac_rewrite_blocks(data, edit_blocks, too_long)


def flac_rewrite_blocks(data: bytes, edit, too_long: str = "The tag is too long for a FLAC file.") -> bytes:
    """The FLAC file with its metadata blocks replaced by edit(blocks) (a list of
    (type, body)); the padding is laid out again and the audio stays as it is."""
    start = 0
    if data[:3] == b"ID3":  # some FLAC files carry an ID3 tag in front; keep it
        size = _syncsafe(data[6:10])
        start = 10 + size + (10 if data[5] & 0x10 else 0)
    if data[start:start + 4] != b"fLaC":
        raise EmbedError("This is not a FLAC file.")
    blocks, audio = _flac_blocks(data, start)
    old_meta = audio - start - 4
    blocks = edit(blocks)
    if any(len(b) >= 1 << 24 for _, b in blocks):
        raise EmbedError(too_long)
    # keep the metadata the same size when the padding allows it, so players that
    # read the header see the same layout; else leave fresh padding
    blocks = [b for b in blocks if b[0] != 1]
    used = sum(4 + len(b) for _, b in blocks)
    pad = old_meta - used - 4
    if pad < 0 or pad > MAX_KEPT_PADDING:
        pad = NEW_PADDING
    blocks.append((1, b"\x00" * pad))
    out = bytearray(data[:start] + b"fLaC")
    for i, (kind, b) in enumerate(blocks):
        last = 0x80 if i == len(blocks) - 1 else 0
        out += bytes([kind | last]) + len(b).to_bytes(3, "big") + b
    out += data[audio:]
    return bytes(out)


def flac_with_lyrics(data: bytes, synced: str, plain: str) -> bytes:
    def edit(comments):
        comments = [c for c in comments if _comment_key(c) not in (FLAC_SYNCED, FLAC_PLAIN)]
        if synced:
            comments.append(f"{FLAC_SYNCED}={synced}".encode("utf-8"))
        if plain:
            comments.append(f"{FLAC_PLAIN}={plain}".encode("utf-8"))
        return comments

    return flac_rewrite(data, edit, "The lyrics are too long for a FLAC tag.")


def flac_lyrics(data: bytes) -> Tuple[str, str]:
    start = 0
    if data[:3] == b"ID3":
        start = 10 + _syncsafe(data[6:10]) + (10 if data[5] & 0x10 else 0)
    if data[start:start + 4] != b"fLaC":
        return "", ""
    blocks, _ = _flac_blocks(data, start)
    synced = plain = ""
    for kind, body in blocks:
        if kind == 4:
            for c in _parse_vorbis(body)[1]:
                key = _comment_key(c)
                value = c.split(b"=", 1)[1].decode("utf-8", "replace") if b"=" in c else ""
                if key == FLAC_SYNCED and not synced:
                    synced = value
                elif key == FLAC_PLAIN and not plain:
                    plain = value
    return synced, plain


# ------------------------------------------------------------------- ID3 ---
def _syncsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _to_syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _id3_frames(data: bytes) -> Tuple[int, List[Tuple[bytes, bytes, bytes]], int]:
    """(major version, [(id, flags, body)], tag length including header) of a leading
    ID3v2.3/2.4 tag; (4, [], 0) when there is none."""
    if data[:3] != b"ID3":
        return 4, [], 0
    major, flags = data[3], data[5]
    if major not in (3, 4):
        raise EmbedError(f"The MP3 file has an ID3v2.{major} tag, which cannot be updated.")
    if flags & 0x80:
        raise EmbedError("The MP3 file's ID3 tag uses unsynchronisation, which cannot be updated.")
    size = _syncsafe(data[6:10])
    total = 10 + size + (10 if (major == 4 and flags & 0x10) else 0)
    pos, end = 10, 10 + size
    if flags & 0x40:  # extended header: skipped (it is not written again)
        ext = _syncsafe(data[10:14]) if major == 4 else struct.unpack(">I", data[10:14])[0] + 4
        pos += ext
    frames = []
    while pos + 10 <= end:
        fid = data[pos:pos + 4]
        if fid == b"\x00\x00\x00\x00" or not re.fullmatch(rb"[A-Z0-9]{4}", fid):
            break  # padding
        n = _syncsafe(data[pos + 4:pos + 8]) if major == 4 else struct.unpack(">I", data[pos + 4:pos + 8])[0]
        fflags = data[pos + 8:pos + 10]
        body = data[pos + 10:pos + 10 + n]
        if len(body) != n:
            raise EmbedError("The MP3 file's ID3 tag is damaged.")
        frames.append((fid, fflags, body))
        pos += 10 + n
    return major, frames, total


def _enc(text: str, major: int) -> Tuple[int, bytes, bytes]:
    """(encoding byte, encoded text, terminator): UTF-8 for ID3v2.4, UTF-16 with a
    byte order mark for ID3v2.3 (which has no UTF-8)."""
    if major == 4:
        return 3, text.encode("utf-8"), b"\x00"
    return 1, b"\xff\xfe" + text.encode("utf-16-le"), b"\x00\x00"


def _uslt(plain: str, major: int) -> bytes:
    enc, text, term = _enc(plain, major)
    _, desc, _ = _enc("", major)
    return bytes([enc]) + LANG + desc + term + text


def _sylt(entries: List[Tuple[int, str]], major: int) -> bytes:
    enc, _, term = _enc("", major)
    _, desc, _ = _enc("", major)
    out = bytes([enc]) + LANG + bytes([2, 1]) + desc + term  # 2 = milliseconds, 1 = lyrics
    for ms, line in entries:
        out += _enc(line, major)[1] + term + struct.pack(">I", ms)
    return out


def mp3_rewrite(data: bytes, edit, too_long: str = "The tag is too long for an ID3 tag.") -> bytes:
    """The MP3 file with its ID3v2 frames replaced by edit(major, frames) (a list of
    (id, flags, body)); the audio after the tag stays as it is."""
    major, frames, total = _id3_frames(data)
    body = b""
    for fid, fflags, fb in edit(major, frames):
        size = _to_syncsafe(len(fb)) if major == 4 else struct.pack(">I", len(fb))
        body += fid + size + fflags + fb
    old_body = total - 10 if total else 0
    pad = old_body - len(body)
    if pad < 0 or not total:
        pad = NEW_PADDING
    body += b"\x00" * pad
    if len(body) >= 1 << 28:
        raise EmbedError(too_long)
    header = b"ID3" + bytes([major, 0, 0]) + _to_syncsafe(len(body))
    return header + body + data[total:]


def mp3_with_lyrics(data: bytes, synced: str, plain: str) -> bytes:
    def edit(major, frames):
        frames = [f for f in frames if f[0] not in (b"USLT", b"SYLT")]
        entries = lrc_entries(synced) if synced else []
        if entries:
            frames.append((b"SYLT", b"\x00\x00", _sylt(entries, major)))
        if plain:
            frames.append((b"USLT", b"\x00\x00", _uslt(plain, major)))
        return frames

    return mp3_rewrite(data, edit, "The lyrics are too long for an ID3 tag.")


def mp3_lyrics(data: bytes) -> Tuple[str, str]:
    """(synced lyrics as LRC, plain lyrics) from the SYLT and USLT frames."""
    try:
        major, frames, _ = _id3_frames(data)
    except EmbedError:
        return "", ""
    synced = plain = ""
    for fid, _, body in frames:
        if fid == b"USLT" and not plain and len(body) > 4:
            plain = _decode_after_desc(body[4:], body[0])
        elif fid == b"SYLT" and not synced and len(body) > 6:
            synced = _sylt_to_lrc(body)
    return synced, plain


def _split_text(b: bytes, enc: int) -> Tuple[str, bytes]:
    """One terminated string of encoding `enc` and the bytes after it."""
    if enc in (1, 2):
        i = 0
        while i + 1 < len(b) and b[i:i + 2] != b"\x00\x00":
            i += 2
        raw, rest = b[:i], b[i + 2:]
        codec = "utf-16" if enc == 1 else "utf-16-be"
        return raw.decode(codec, "replace") if raw else "", rest
    i = b.find(b"\x00")
    i = len(b) if i < 0 else i
    return b[:i].decode("utf-8" if enc == 3 else "latin-1", "replace"), b[i + 1:]


def _decode_after_desc(b: bytes, enc: int) -> str:
    _, rest = _split_text(b, enc)
    if enc in (1, 2):
        return rest.decode("utf-16" if enc == 1 else "utf-16-be", "replace").rstrip("\x00")
    return rest.decode("utf-8" if enc == 3 else "latin-1", "replace").rstrip("\x00")


def _sylt_to_lrc(body: bytes) -> str:
    enc, fmt = body[0], body[4]
    _, rest = _split_text(body[6:], enc)
    lines = []
    while rest:
        text, rest = _split_text(rest, enc)
        if len(rest) < 4:
            break
        t = struct.unpack(">I", rest[:4])[0]
        rest = rest[4:]
        if fmt == 2:
            lines.append(f"[{t // 60000:02d}:{t // 1000 % 60:02d}.{t % 1000 // 10:02d}] {text}".rstrip())
    return "\n".join(lines)


# -------------------------------------------------------------- the files ---
def _tag_bytes(path: str, ext: str) -> bytes:
    """Only the part of the file the tag lives in, never the audio: for MP3 the leading
    ID3v2 tag; for FLAC a minimal stream of "fLaC" and the Vorbis comment block."""
    with open(path, "rb") as fh:
        head = fh.read(10)
        lead = b""
        if head[:3] == b"ID3" and len(head) == 10:
            fh.seek(0)
            lead = fh.read(10 + _syncsafe(head[6:10]) + (10 if head[5] & 0x10 else 0))
        if ext == ".mp3":
            return lead
        fh.seek(len(lead))
        if fh.read(4) != b"fLaC":
            return b""
        while True:
            h = fh.read(4)
            if len(h) < 4:
                break
            size = int.from_bytes(h[1:4], "big")
            if h[0] & 0x7F == 4:
                return b"fLaC" + bytes([0x84]) + h[1:4] + fh.read(size)
            if h[0] & 0x80:
                break
            fh.seek(size, os.SEEK_CUR)
        return b"fLaC" + bytes([0x81, 0, 0, 0])  # no Vorbis comment: an empty, last padding block


def embedded_lyrics(path: str) -> Tuple[str, str]:
    """(synced, plain) lyrics inside an MP3 or FLAC file ('' when none or unreadable)."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED:
        return "", ""
    try:
        data = _tag_bytes(path, ext)
    except OSError:
        return "", ""
    try:
        if ext == ".flac":
            return flac_lyrics(data)
        return mp3_lyrics(data)
    except EmbedError:
        return "", ""


def embed_lyrics(path: str, synced: str, plain: str) -> None:
    """Write the lyrics into the file's tag (replacing earlier ones); raises EmbedError
    or OSError.  The file's audio data is not changed."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED:
        raise EmbedError("Lyrics can only be embedded in MP3 and FLAC files.")
    with open(path, "rb") as fh:
        data = fh.read()
    new = flac_with_lyrics(data, synced, plain) if ext == ".flac" else mp3_with_lyrics(data, synced, plain)
    replace_file(path, new)


# 2.4: on Windows a file that another program has open (FFmpeg reading it for the mood
# analysis or a scan, an antivirus scanner, the search indexer) cannot be replaced for a
# moment; os.replace() then fails with a sharing violation, so it is tried again briefly
REPLACE_RETRY_S = 5.0


def replace_retrying(tmp: str, path: str, timeout: float = REPLACE_RETRY_S) -> None:
    """os.replace(tmp, path), tried again for up to `timeout` seconds while Windows says
    the file is in use (PermissionError); elsewhere a rename never waits for readers."""
    import time

    deadline = time.monotonic() + timeout
    delay = 0.05
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.4)


def replace_file(path: str, data: bytes) -> None:
    """Write `data` over the file through a temporary file in its folder (keeping its
    permissions), so a failure never leaves half a file."""
    ext = os.path.splitext(path)[1].lower()
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".realhires-embed-", suffix=ext, dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            pass
        replace_retrying(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def plain_from_synced(synced: str) -> str:
    """The text of synced lyrics without timestamps (for the plain field)."""
    from .lyrics import strip_timestamps

    return strip_timestamps(synced)


def lyrics_of(path: str) -> Optional[Tuple[str, str, str]]:
    """(kind, text, where) of the lyrics embedded in a song, for display (see
    lyrics.read_lyrics_file for the order with the files next to it): the synced lyrics,
    else the plain ones, which count as synced when they are LRC (`looks_synced`)."""
    from .lyrics import INSTRUMENTAL, PLAIN, SYNCED, is_instrumental_lrc

    synced, plain = embedded_lyrics(path) if supported(path) else ("", "")
    for text in (synced, plain):
        if text:
            if is_instrumental_lrc(text):
                return INSTRUMENTAL, text, "embedded"
            return (SYNCED if text is synced or looks_synced(text) else PLAIN), text, "embedded"
    return None
