"""Editing a song's information (2.0): title, artist, album, album artist,
genre, release date, track, disc and composer.

FLAC, MP3, Ogg Vorbis and Opus files are written natively: only the Vorbis
comment block, the ID3v2 tag or the Ogg comment header pages change (FLAC and
MP3 as the embedded lyrics are, `embed.flac_rewrite()`/`mp3_rewrite()`; Ogg by
`ogg_with_tags()`, which renumbers the following pages when the number of
header pages changes), the audio data is copied byte for byte, and the new file
replaces the old one through a temporary file.  Other formats (M4A with AAC or
ALAC, WAV, AIFF, WavPack, WMA, and Ogg files that are not plain Vorbis or Opus)
are copied by FFmpeg with ``-c copy`` into a new container that carries the new
tags, and the copy replaces the file only when it decodes to exactly the same
samples as the original (`pcm_digest()`): FFmpeg 8's Ogg muxer, for one, drops
the end trimming of Vorbis when it copies.  Only the fields that
were changed are written, an empty value removes a field, and every other tag
and the cover art are kept.

After a file has been written, its saved quality-check results are moved to
its new size and time with the new tags (`ResultStore.retag()`), because its
audio did not change, and the library reads it again.

The cover (2.1, `set_cover()`, `remove_cover()`, at the end of this module) is
written the same way: a FLAC PICTURE block, an ID3v2 APIC frame or an Ogg
METADATA_BLOCK_PICTURE comment natively, an attached picture through FFmpeg's
stream copy for M4A and AIFF.

The Rust version (`rust/crates/core/src/tagedit.rs`) writes the same bytes and
runs the same FFmpeg command; `rust/tools/gen_player_vectors.py` writes the
Python answers it is tested against.
"""

from __future__ import annotations

import os
import hashlib
import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import embed

FIELDS = ("title", "artist", "album", "album_artist", "genre", "date", "track", "disc", "composer")
LABELS = {"title": "Title", "artist": "Artist", "album": "Album", "album_artist": "Album artist",
          "genre": "Genre", "date": "Release date", "track": "Track", "disc": "Disc", "composer": "Composer"}
HINTS = {"date": "a year (2004) or a date (2004-05-28)", "track": "3, or 3/12 with the number of tracks",
         "disc": "1, or 1/2 with the number of discs"}
MAX_VALUE = 1000
NATIVE = (".flac", ".mp3", ".ogg", ".oga", ".opus")
STREAM_TAGS = (".ogg", ".oga", ".opus", ".spx")   # Ogg keeps the tags with the audio stream
ID3_CHUNK = (".aif", ".aiff", ".aifc")            # AIFF: an ID3 chunk holds every field
NO_MUXER = (".ape", ".dsf", ".dff", ".tak", ".mpc", ".ofr", ".tta")  # FFmpeg reads these but cannot write them

_FLAC = {
    "title": ("TITLE", ("TITLE",)),
    "artist": ("ARTIST", ("ARTIST",)),
    "album": ("ALBUM", ("ALBUM",)),
    "album_artist": ("ALBUMARTIST", ("ALBUMARTIST", "ALBUM ARTIST", "ALBUM_ARTIST")),
    "genre": ("GENRE", ("GENRE",)),
    "date": ("DATE", ("DATE", "YEAR")),
    "track": ("TRACKNUMBER", ("TRACKNUMBER", "TRACK")),
    "disc": ("DISCNUMBER", ("DISCNUMBER", "DISC")),
    "composer": ("COMPOSER", ("COMPOSER",)),
}
_FLAC_TOTAL = {"track": ("TRACKTOTAL", ("TRACKTOTAL", "TOTALTRACKS")),
               "disc": ("DISCTOTAL", ("DISCTOTAL", "TOTALDISCS"))}
_ID3 = {"title": b"TIT2", "artist": b"TPE1", "album": b"TALB", "album_artist": b"TPE2", "genre": b"TCON",
        "track": b"TRCK", "disc": b"TPOS", "composer": b"TCOM"}
_NUMBER = re.compile(r"^([0-9]+)(?:/([0-9]+))?$")
_YEAR = re.compile(r"^[0-9]{4}")
_FULL_DATE = re.compile(r"^[0-9]{4}-([0-9]{2})-([0-9]{2})")


class TagError(Exception):
    """The tags could not be changed; the message is a full sentence."""


def clean_value(value) -> str:
    """One line: surrounding space removed, runs of white space (newlines too) made one space."""
    return " ".join(str(value or "").split())


def split_number(value: str) -> Tuple[str, str]:
    """("3", "12") for "3/12", ("3", "") for "3", ("", "") for ""; the value must be checked."""
    m = _NUMBER.match(value.replace(" ", ""))
    if not m:
        return "", ""
    return str(int(m.group(1))), (str(int(m.group(2))) if m.group(2) else "")


def check_changes(changes: Dict[str, str]) -> Dict[str, str]:
    """The changes, cleaned (only known fields; track and disc as "3" or "3/12"), or
    TagError saying what is wrong with the first bad one."""
    out: Dict[str, str] = {}
    for f in FIELDS:
        if f not in changes:
            continue
        v = clean_value(changes[f])
        if len(v) > MAX_VALUE:
            raise TagError(f"The {LABELS[f].lower()} is too long: at most {MAX_VALUE:,} characters fit.")
        if v and f in ("track", "disc"):
            if not _NUMBER.match(v.replace(" ", "")):
                raise TagError(f"The {LABELS[f].lower()} must be a number, such as {HINTS[f]}.")
            n, total = split_number(v)
            v = f"{n}/{total}" if total else n
        if v and f == "date" and not _YEAR.match(v):
            raise TagError("Write the release date as a year (2004) or a date (2004-05-28).")
        out[f] = v
    return out


def common_values(tags: Iterable[Dict[str, str]]) -> Dict[str, Optional[str]]:
    """For the editor: each field's value when every song has the same one, None
    where the songs differ ("Several values")."""
    out: Dict[str, Optional[str]] = {}
    tags = list(tags)
    for f in FIELDS:
        values = {clean_value(t.get(f, "")) for t in tags}
        out[f] = values.pop() if len(values) == 1 else (None if values else "")
    return out


# ------------------------------------------------------------------ FLAC ---
def _put(items: list, key_of: Callable, removes: Iterable, new: list) -> list:
    """Replace the items whose key is in `removes` by `new`, at the place of the first
    one (or at the end when there is none)."""
    removes = set(removes)
    out, placed = [], False
    for it in items:
        if key_of(it) in removes:
            if not placed:
                out.extend(new)
                placed = True
        else:
            out.append(it)
    if not placed:
        out.extend(new)
    return out


def _comment(key: str, value: str) -> List[bytes]:
    return [f"{key}={value}".encode("utf-8")] if value else []


def edit_comments(comments: List[bytes], changes: Dict[str, str]) -> List[bytes]:
    """Vorbis comments (b"KEY=value") with the (checked) changes: each field's comment
    replaced at its place, its other spellings removed."""
    key_of = embed._comment_key
    for f in FIELDS:
        if f not in changes:
            continue
        value = changes[f]
        key, removes = _FLAC[f]
        if f in _FLAC_TOTAL:
            number, total = split_number(value)
            comments = _put(comments, key_of, removes, _comment(key, number))
            if total or not number:  # "3" keeps the number of tracks, "" removes it
                tkey, tremoves = _FLAC_TOTAL[f]
                comments = _put(comments, key_of, tremoves, _comment(tkey, total))
        else:
            comments = _put(comments, key_of, removes, _comment(key, value))
    return comments


def flac_with_tags(data: bytes, changes: Dict[str, str]) -> bytes:
    """The FLAC file with the (checked) changes in its Vorbis comments."""
    return embed.flac_rewrite(data, lambda c: edit_comments(c, changes), "The tags are too long for a FLAC file.")


# ------------------------------------------------------------------- Ogg ---
def _crc_table() -> List[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        table.append(r & 0xFFFFFFFF)
    return table


_CRC = _crc_table()


def ogg_crc(data: bytes) -> int:
    """The Ogg page checksum (CRC-32, polynomial 0x04C11DB7, not reflected)."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) ^ b) & 0xFF]
    return crc


def _ogg_page_at(data: bytes, pos: int) -> tuple:
    """(header type, granule bytes, serial, sequence, lacing values, body, end) of the page at `pos`."""
    if data[pos:pos + 4] != b"OggS" or len(data) < pos + 27:
        raise TagError("The Ogg file is damaged: a page does not start where it should.")
    nsegs = data[pos + 26]
    segs = list(data[pos + 27:pos + 27 + nsegs])
    start = pos + 27 + nsegs
    end = start + sum(segs)
    if len(segs) != nsegs or end > len(data):
        raise TagError("The Ogg file ends inside a page.")
    serial, seq = struct.unpack_from("<II", data, pos + 14)
    return data[pos + 5], data[pos + 6:pos + 14], serial, seq, segs, data[start:end], end


def _ogg_page(htype: int, granule: bytes, serial: int, seq: int, segs: List[int], body: bytes) -> bytes:
    page = bytearray(b"OggS\x00" + bytes([htype]) + granule + struct.pack("<II", serial, seq) + b"\x00\x00\x00\x00"
                     + bytes([len(segs)]) + bytes(segs) + body)
    page[22:26] = struct.pack("<I", ogg_crc(bytes(page)))
    return bytes(page)


def _ogg_pages_for(packets: List[bytes], serial: int, seq: int) -> List[bytes]:
    """Header packets laid out in pages, as libogg flushes them: up to 255 lacing
    values a page, a packet that goes on marks the next page as continued, granule
    position 0 on a page where a packet ends and -1 on one where none does."""
    pages, segs, body, cont = [], [], bytearray(), False

    def flush(cont_next: bool):
        nonlocal segs, body, cont, seq
        granule = b"\x00" * 8 if any(v < 255 for v in segs) else b"\xff" * 8
        pages.append(_ogg_page(1 if cont else 0, granule, serial, seq, segs, bytes(body)))
        seq += 1
        segs, body, cont = [], bytearray(), cont_next

    for pkt in packets:
        lace = [255] * (len(pkt) // 255) + [len(pkt) % 255]
        pos = 0
        for i, v in enumerate(lace):
            if len(segs) == 255:
                flush(i > 0)
            segs.append(v)
            body += pkt[pos:pos + v]
            pos += v
    if segs:
        flush(False)
    return pages


def _parse_comment_body(b: bytes, start: int) -> Tuple[bytes, List[bytes], int]:
    """(vendor, comments, end offset) of a Vorbis comment structure at `start`."""
    try:
        vlen = struct.unpack_from("<I", b, start)[0]
        vendor = b[start + 4:start + 4 + vlen]
        pos = start + 4 + vlen
        count = struct.unpack_from("<I", b, pos)[0]
        pos += 4
        comments = []
        for _ in range(count):
            n = struct.unpack_from("<I", b, pos)[0]
            if pos + 4 + n > len(b):
                raise struct.error("short")
            comments.append(b[pos + 4:pos + 4 + n])
            pos += 4 + n
    except struct.error as exc:
        raise TagError("The Ogg file's comment header is damaged.") from exc
    if len(vendor) != vlen:
        raise TagError("The Ogg file's comment header is damaged.")
    return vendor, comments, pos


def ogg_with_tags(data: bytes, changes: Dict[str, str]) -> bytes:
    """The Ogg Vorbis or Opus file with the (checked) changes in its comment header.
    The first page and every audio page keep their bytes (only their sequence
    numbers and checksums change when the header takes a different number of
    pages).  TagError for anything else (Ogg FLAC, Speex, several streams...)."""
    return ogg_rewrite(data, lambda comments: edit_comments(comments, changes))


def ogg_rewrite(data: bytes, edit: Callable[[List[bytes]], List[bytes]]) -> bytes:
    """The Ogg Vorbis or Opus file with its comments replaced by edit(comments) (see
    ogg_with_tags())."""
    htype, granule, serial, _, segs, body, pos = _ogg_page_at(data, 0)
    first = body
    if not htype & 2 or any(v == 255 for v in segs) or len(segs) != 1:
        raise TagError("The Ogg file does not start with a stream header page of its own.")
    if first.startswith(b"\x01vorbis"):
        need, magic = 3, b"\x03vorbis"
    elif first.startswith(b"OpusHead"):
        need, magic = 2, b"OpusTags"
    else:
        raise TagError("Only Ogg files with Vorbis or Opus audio can be changed directly.")
    packets, cur, old_pages = [first], bytearray(), 0
    while len(packets) < need:
        if pos >= len(data):
            raise TagError("The Ogg file ends inside its headers.")
        htype, granule, s2, _, segs, body, end = _ogg_page_at(data, pos)
        if s2 != serial:
            raise TagError("The Ogg file holds several streams, which cannot be changed directly.")
        off = 0
        for i, v in enumerate(segs):
            if len(packets) == need:
                raise TagError("The Ogg file's headers share a page with the audio.")
            cur += body[off:off + v]
            off += v
            if v < 255:
                packets.append(bytes(cur))
                cur = bytearray()
        old_pages += 1
        pos = end
    if cur or len(packets) != need:
        raise TagError("The Ogg file's headers share a page with the audio.")
    tags = packets[1]
    if not tags.startswith(magic):
        raise TagError("The Ogg file's comment header is damaged.")
    vendor, comments, end = _parse_comment_body(tags, len(magic))
    rest = tags[end:]  # Vorbis: the framing bit; Opus: padding or binary data, kept
    new_tags = magic + embed._vorbis_body(vendor, edit(comments)) + rest
    pages = _ogg_pages_for([new_tags] + packets[2:], serial, 1)
    out = bytearray(data[:_ogg_page_at(data, 0)[6]])
    for p in pages:
        out += p
    delta = len(pages) - old_pages
    if delta == 0:
        out += data[pos:]
        return bytes(out)
    while pos < len(data) and data[pos:pos + 4] == b"OggS":  # renumber this stream's pages
        htype, granule, s2, seq, segs, body, end = _ogg_page_at(data, pos)
        if s2 == serial:
            out += _ogg_page(htype, granule, s2, seq + delta, segs, body)
        else:
            out += data[pos:end]
        pos = end
    out += data[pos:]
    return bytes(out)


# ------------------------------------------------------------------- ID3 ---
def _text_frame(fid: bytes, text: str, major: int) -> tuple:
    enc, raw, _ = embed._enc(text, major)
    return fid, b"\x00\x00", bytes([enc]) + raw


def mp3_with_tags(data: bytes, changes: Dict[str, str]) -> bytes:
    """The MP3 file with the (checked) changes in its ID3v2 tag (a new ID3v2.4 tag
    when it has none).  ID3v2.3 keeps the release date as TYER (the year) and TDAT
    (day and month), ID3v2.4 as TDRC."""
    def edit(major, frames):
        key_of = lambda fr: fr[0]  # noqa: E731
        for f in FIELDS:
            if f not in changes:
                continue
            value = changes[f]
            if f == "date":
                new = []
                if major == 4:
                    if value:
                        new.append(_text_frame(b"TDRC", value, major))
                elif value:
                    new.append(_text_frame(b"TYER", value[:4], major))
                    m = _FULL_DATE.match(value)
                    if m:
                        new.append(_text_frame(b"TDAT", m.group(2) + m.group(1), major))
                frames = _put(frames, key_of, (b"TDRC", b"TYER", b"TDAT", b"TIME"), new)
            else:
                fid = _ID3[f]
                frames = _put(frames, key_of, (fid,), [_text_frame(fid, value, major)] if value else [])
        return frames

    try:
        return embed.mp3_rewrite(data, edit, "The tags are too long for an ID3 tag.")
    except embed.EmbedError as exc:
        raise TagError(str(exc)) from exc


# ---------------------------------------------------------------- FFmpeg ---
def ffmpeg_args(src: str, dst: str, changes: Dict[str, str]) -> List[str]:
    """The arguments (after the program) that copy `src` to `dst` with new tags."""
    ext = os.path.splitext(src)[1].lower()
    spec = "-metadata:s:a:0" if ext in STREAM_TAGS else "-metadata"
    args = ["-v", "error", "-nostdin", "-y", "-i", src, "-map", "0", "-c", "copy", "-map_metadata", "0",
            "-map_chapters", "0", "-fflags", "+bitexact"]
    for f in FIELDS:
        if f in changes:
            args += [spec, f"{f}={changes[f]}"]
    if ext in ID3_CHUNK:
        args += ["-write_id3v2", "1"]
    return args + [dst]


def cannot_edit(path: str) -> str:
    """Why the tags of this file cannot be changed, or ''."""
    ext = os.path.splitext(path)[1].lower()
    if ext in NO_MUXER:
        return (f"The information in {ext[1:].upper()} files cannot be changed here, because FFmpeg can read "
                f"them but not write them.")
    return ""


def pcm_digest(path: str) -> str:
    """SHA-1 of the file's first audio stream decoded to 32-bit samples (the same
    decoder gives the same samples, so equal digests mean equal audio)."""
    from .probe import _no_window, tool

    h = hashlib.sha1()
    proc = subprocess.Popen([tool("ffmpeg"), "-v", "error", "-nostdin", "-i", path, "-map", "0:a:0",
                             "-f", "s32le", "-acodec", "pcm_s32le", "-"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **_no_window())
    assert proc.stdout is not None
    for block in iter(lambda: proc.stdout.read(1 << 20), b""):
        h.update(block)
    proc.stdout.close()
    if proc.wait() != 0:
        raise TagError("FFmpeg could not decode the file to compare its audio.")
    return h.hexdigest()


def _ffmpeg_copy(path: str, changes: Dict[str, str]) -> None:
    why = cannot_edit(path)
    if why:
        raise TagError(why)
    _ffmpeg_replace(path, lambda tmp: ffmpeg_args(path, tmp, changes), "information")


def _ffmpeg_replace(path: str, make_args: Callable[[str], List[str]], what: str) -> None:
    """Run FFmpeg with make_args(temporary file) and put the copy in the file's place
    when it decodes to exactly the same samples (TagError otherwise)."""
    from .probe import _no_window, tool

    ext = os.path.splitext(path)[1]
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".realhires-tags-", suffix=ext, dir=folder)
    os.close(fd)
    try:
        proc = subprocess.run([tool("ffmpeg")] + make_args(tmp), capture_output=True,
                              stdin=subprocess.DEVNULL, **_no_window())
        if proc.returncode != 0:
            msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise TagError(f"FFmpeg could not copy the file with the new {what}: "
                           f"{msg[-1] if msg else f'exit code {proc.returncode}'}.")
        if pcm_digest(tmp) != pcm_digest(path):
            raise TagError("FFmpeg's copy did not have exactly the same audio as the original, so the file was "
                           "left unchanged.")
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            pass
        embed.replace_retrying(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_tags(path: str, changes: Dict[str, str]) -> str:
    """Write the (checked) changes into the file; returns "native" or "ffmpeg".
    Raises TagError or OSError, and leaves the file as it was then."""
    if not changes:
        return "native"
    ext = os.path.splitext(path)[1].lower()
    if ext in NATIVE:
        with open(path, "rb") as fh:
            data = fh.read()
        try:
            if ext == ".flac":
                new = flac_with_tags(data, changes)
            elif ext == ".mp3":
                new = mp3_with_tags(data, changes)
            else:
                new = ogg_with_tags(data, changes)
        except TagError:
            if ext == ".flac":
                raise
            # ID3v2.2 or unsynchronised MP3 tags, Ogg FLAC or Speex...: FFmpeg copies the file
            _ffmpeg_copy(path, changes)
            return "ffmpeg"
        except embed.EmbedError as exc:
            raise TagError(str(exc)) from exc
        embed.replace_file(path, new)
        return "native"
    _ffmpeg_copy(path, changes)
    return "ffmpeg"


# ------------------------------------------------------- saving songs ---
@dataclass
class TagOutcome:
    path: str
    ok: bool
    message: str = ""                       # a full sentence when it failed or was kept only partly
    tags: Dict[str, str] = field(default_factory=dict)   # what the file holds now (normalised)


def same_value(f: str, wanted: str, got: str) -> bool:
    if f in ("track", "disc"):
        return split_number(wanted)[0] == split_number(got)[0]
    return clean_value(wanted) == clean_value(got)


def _join(words: List[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def kept_note(name: str, changes: Dict[str, str], tags: Dict[str, str]) -> str:
    """'' when the file holds every change, else a sentence saying what its format
    could not keep."""
    lost = [f"the {LABELS[f].lower()}" for f in FIELDS if f in changes and changes[f] and not tags.get(f)]
    other = [f"the {LABELS[f].lower()} reads “{tags[f]}”" for f in FIELDS
             if f in changes and tags.get(f) and not same_value(f, changes[f], tags[f])]
    parts = []
    if lost:
        parts.append(f"its format has no place for {_join(lost)}")
    if other:
        parts.append(_join(other) + ", as far as its format allows")
    return f"{name} was saved, but {'; '.join(parts)}." if parts else ""


def save_tags(paths: List[str], changes: Dict[str, str], cfg=None,
              progress: Optional[Callable[[int, int, str], None]] = None) -> List[TagOutcome]:
    """Write the changes into every file, keep their saved results and update the
    library.  Never raises; one outcome per file."""
    return save_tags_each([(p, changes) for p in paths], cfg, progress)


def save_tags_each(items: List[Tuple[str, Dict[str, str]]], cfg=None,
                   progress: Optional[Callable[[int, int, str], None]] = None) -> List[TagOutcome]:
    """`save_tags()` with changes of each file's own: [(path, changes)].  Never raises."""
    from . import musiclib, store as store_mod
    from .probe import probe

    checked: List[Tuple[str, Optional[Dict[str, str]], str]] = []
    for path, changes in items:
        try:
            checked.append((path, check_changes(changes), ""))
        except TagError as exc:
            checked.append((path, None, str(exc)))
    if all(c is None for _, c, _ in checked):
        return [TagOutcome(p, False, err) for p, _, err in checked]
    results = []
    st = store_mod.open_store(cfg) if cfg is not None and getattr(cfg, "save_results", True) else None
    lib = None
    try:
        lib = musiclib.Library()
    except Exception:  # noqa: BLE001
        lib = None
    try:
        for i, (path, changes, err) in enumerate(checked):
            if progress is not None:
                progress(i, len(checked), path)
            if changes is None:
                results.append(TagOutcome(path, False, err))
                continue
            name = os.path.basename(path)
            try:
                before = os.stat(path)
                write_tags(path, changes)
            except (TagError, OSError) as exc:
                results.append(TagOutcome(path, False, f"Could not save the information of {name}: {exc}"))
                continue
            try:
                tags = probe(path).tags or {}
            except Exception:  # noqa: BLE001
                tags = {}
            if st is not None:
                st.retag(path, before.st_size, before.st_mtime_ns, tags=tags)
            if lib is not None:
                lib.update_file(path)
            results.append(TagOutcome(path, True, kept_note(name, changes, tags), tags))
        if progress is not None:
            progress(len(checked), len(checked), "")
    finally:
        if st is not None:
            st.close()
        if lib is not None:
            lib.close()
    return results


# ------------------------------------------- renaming and merging groups ---
# The Genres, Composers and Years pages rename a genre, composer or year in every
# song of it, and merge genres, through save_tags_each(): only the tag changes.
GROUP_FIELD = {"genres": "genre", "composers": "composer", "years": "date"}
GROUP_NOUN = {"genres": "genre", "composers": "composer", "years": "year"}


def renamed_date(old: str, year: str) -> str:
    """The release date with its year replaced and the rest kept: "2001-05-03" with
    2004 gives "2004-05-03"; a date without a year ("", "May 2001") gives "2004"."""
    old = clean_value(old)
    return year + old[4:] if _YEAR.match(old) else year


def check_group_name(kind: str, name: str) -> str:
    """The new name of a genre, composer or year, cleaned, or TagError."""
    name = clean_value(name)
    noun = GROUP_NOUN[kind]
    if not name:
        raise TagError(f"Give the {noun} a name." if kind != "years" else "Write the year, such as 2004.")
    if kind == "years" and not re.match(r"^[0-9]{4}$", name):
        raise TagError("Write the year as four digits, such as 2004.")
    if len(name) > MAX_VALUE:
        raise TagError(f"The {noun} is too long: at most {MAX_VALUE:,} characters fit.")
    return name


def group_changes(kind: str, name: str, tags: Dict[str, str]) -> Dict[str, str]:
    """What a song whose tags are `tags` gets when its genre, composer or year becomes `name`."""
    if kind == "years":
        return {"date": renamed_date(tags.get("date", ""), name)}
    return {GROUP_FIELD[kind]: name}


def rename_group(kind: str, paths: List[str], name: str, cfg=None,
                 progress: Optional[Callable[[int, int, str], None]] = None) -> List[TagOutcome]:
    """Give every song in `paths` the genre, composer or year `name` (a year replaces
    only the year of each song's release date).  Never raises; one outcome per file."""
    try:
        name = check_group_name(kind, name)
    except TagError as exc:
        return [TagOutcome(p, False, str(exc)) for p in paths]
    items = []
    for path in paths:
        tags: Dict[str, str] = {}
        if kind == "years":
            from .probe import probe

            try:
                tags = probe(path).tags or {}
            except Exception:  # noqa: BLE001 - an unreadable file fails when it is written
                tags = {}
        items.append((path, group_changes(kind, name, tags)))
    return save_tags_each(items, cfg, progress)


def save_summary(outcomes: List[TagOutcome]) -> Tuple[str, List[str]]:
    """(headline, sentences) for the editor after saving."""
    ok = [o for o in outcomes if o.ok]
    bad = [o for o in outcomes if not o.ok]
    n = len(ok)
    head = (f"Saved the information of {n:,} song{'s' if n != 1 else ''}." if ok and not bad else
            f"Saved {n:,} of {len(outcomes):,} songs." if ok else
            "Nothing was saved.")
    lines: List[str] = []
    for o in ok + bad:
        if o.message and o.message not in lines:
            lines.append(o.message)
    return head, lines


# ------------------------------------------------------------- the cover ---
# The Cover window (2.1, docs/player-spec.md "Inspecting and changing a cover"):
# a JPEG or PNG picture becomes the songs' front cover, or their covers are
# removed.  FLAC gets a PICTURE metadata block, MP3 an ID3v2 APIC frame, Ogg
# Vorbis and Opus a METADATA_BLOCK_PICTURE comment (the FLAC picture structure in
# base64), each of type 3 (front cover), at the place of the file's first picture,
# and the earlier covers go: pictures of type 3, and of type 0 ("Other"), which
# iTunes and FFmpeg write for a cover.  Other pictures (back cover, booklet...)
# stay.  Only the tag changes, as for the information above.  M4A and AIFF get
# the picture through FFmpeg's stream copy as an attached picture, kept only when
# the copy decodes to the same samples; other formats have no place for a picture
# that FFmpeg can write and are left as they are.  The Rust version writes the same
# bytes (rust/crates/core/src/tagedit.rs, python_covers_embed.json).

FRONT_COVER = 3
COVER_TYPES = (3, 0)
MAX_PICTURE = 16_000_000
PICTURE_FFMPEG = (".m4a", ".m4b", ".mp4", ".alac", ".aif", ".aiff", ".aifc")
NO_PICTURE = (".wav", ".wave", ".w64", ".rf64", ".caf", ".au", ".snd", ".aac", ".adts", ".ac3", ".dts", ".amr")
MBP = "METADATA_BLOCK_PICTURE"
_B64 = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def check_picture(image: bytes, mime: str = ""):
    """The picture's covers.PictureInfo, or TagError when it cannot be a cover (not
    a JPEG or PNG image, or over MAX_PICTURE bytes)."""
    from . import covers

    m = (mime or "").strip().lower()
    if m and (m if m != "image/jpg" else "image/jpeg") not in ("image/jpeg", "image/png"):
        raise TagError("Only JPEG and PNG pictures can be used as a cover.")
    if len(image) > MAX_PICTURE:
        raise TagError(f"The picture is too large: a cover can have at most {MAX_PICTURE // 1_000_000} MB.")
    info = covers.image_info(image)
    if info is None:
        raise TagError("The picture is not a JPEG or PNG image that could be read.")
    return info


def flac_picture(image: bytes) -> bytes:
    """The FLAC picture structure of a front cover: the body of a PICTURE block and,
    in base64, the value of an Ogg METADATA_BLOCK_PICTURE comment (empty description;
    size and depth from the image's header, as metaflac reads them)."""
    info = check_picture(image)
    mime = info.mime.encode("ascii")
    return (struct.pack(">II", FRONT_COVER, len(mime)) + mime + struct.pack(">I", 0)
            + struct.pack(">IIIII", info.width, info.height, info.depth, info.colors, len(image)) + image)


def _put_picture(items: list, is_picture: Callable, is_cover: Callable, new) -> Tuple[list, int]:
    """The items without the covers, with `new` (unless None) at the place of the
    first picture or else at the end; and how many covers were left out."""
    out, removed, placed = [], 0, new is None
    for it in items:
        if is_picture(it):
            if not placed:
                out.append(new)
                placed = True
            if is_cover(it):
                removed += 1
                continue
        out.append(it)
    if not placed:
        out.append(new)
    return out, removed


def picture_type(structure: bytes) -> int:
    """The picture type of a FLAC picture structure (-1 when it is too short)."""
    return struct.unpack_from(">I", structure)[0] if len(structure) >= 4 else -1


def flac_with_cover(data: bytes, image: Optional[bytes]) -> Tuple[bytes, int]:
    """The FLAC file with `image` as its front cover, or without its covers when
    `image` is None; and how many covers were replaced or removed."""
    new = None if image is None else (6, flac_picture(image))
    removed = 0

    def edit(blocks):
        nonlocal removed
        out, removed = _put_picture(blocks, lambda b: b[0] == 6, lambda b: picture_type(b[1]) in COVER_TYPES, new)
        return out

    try:
        return embed.flac_rewrite_blocks(data, edit, "The picture is too large for a FLAC file."), removed
    except embed.EmbedError as exc:
        raise TagError(str(exc)) from exc


def apic_body(image: bytes) -> bytes:
    """An ID3v2.3/2.4 APIC frame's body: Latin-1, the MIME type, type 3, no description."""
    info = check_picture(image)
    return b"\x00" + info.mime.encode("ascii") + b"\x00" + bytes([FRONT_COVER]) + b"\x00" + image


def apic_type(major: int, flags: bytes, body: bytes) -> int:
    """The picture type of an APIC frame (-1 when its body cannot be read: compressed,
    encrypted, grouped or unsynchronised frames)."""
    if len(flags) > 1 and flags[1] & (0xE0 if major == 3 else 0x4F):
        return -1
    i = body.find(b"\x00", 1)
    return body[i + 1] if 0 < i and i + 1 < len(body) else -1


def mp3_with_cover(data: bytes, image: Optional[bytes]) -> Tuple[bytes, int]:
    """The MP3 file with `image` as its front cover (APIC), or without its covers when
    `image` is None; and how many covers were replaced or removed."""
    new = None if image is None else (b"APIC", b"\x00\x00", apic_body(image))
    removed = 0

    def edit(major, frames):
        nonlocal removed
        out, removed = _put_picture(frames, lambda f: f[0] == b"APIC",
                                    lambda f: apic_type(major, f[1], f[2]) in COVER_TYPES, new)
        return out

    try:
        return embed.mp3_rewrite(data, edit, "The picture is too large for an ID3 tag."), removed
    except embed.EmbedError as exc:
        raise TagError(str(exc)) from exc


def b64encode(data: bytes) -> bytes:
    """Standard base64 with padding (as the Rust version writes it)."""
    import base64

    return base64.b64encode(data)


def mbp_type(comment: bytes) -> int:
    """The picture type of a METADATA_BLOCK_PICTURE comment, from the first eight
    base64 characters of its value (-1 when they cannot be read)."""
    value = comment.split(b"=", 1)[1] if b"=" in comment else b""
    head = value[:8]
    if len(head) < 8 or any(c not in _B64 for c in head):
        return -1
    n = 0
    for c in head:
        n = (n << 6) | _B64.index(c)
    return n >> 16


def ogg_with_cover(data: bytes, image: Optional[bytes]) -> Tuple[bytes, int]:
    """The Ogg Vorbis or Opus file with `image` as its front cover (a
    METADATA_BLOCK_PICTURE comment), or without its covers when `image` is None; and
    how many covers were replaced or removed."""
    new = None if image is None else MBP.encode("ascii") + b"=" + b64encode(flac_picture(image))
    removed = 0

    def edit(comments):
        nonlocal removed
        out, removed = _put_picture(comments, lambda c: embed._comment_key(c) == MBP,
                                    lambda c: mbp_type(c) in COVER_TYPES, new)
        return out

    return ogg_rewrite(data, edit), removed


def cover_stream(attached_pic: bool, comment: str) -> bool:
    """Whether a video stream FFmpeg shows is a cover: an attached picture whose
    comment (FFmpeg's name of the picture type) is "Cover (front)", "Other" or none."""
    return bool(attached_pic) and clean_value(comment).lower() in ("", "other", "cover (front)")


def picture_streams(path: str) -> List[Tuple[int, bool]]:
    """(index, cover_stream()) of every video stream in the file, by ffprobe."""
    import json

    from .probe import _no_window, tool

    cmd = [tool("ffprobe"), "-v", "error", "-show_entries",
           "stream=index,codec_type:stream_disposition=attached_pic:stream_tags=comment", "-of", "json", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=60, **_no_window())
        streams = json.loads(proc.stdout or b"{}").get("streams", []) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        streams = None
    if streams is None:
        raise TagError("FFmpeg could not read the file's streams.")
    out = []
    for st in streams:
        if st.get("codec_type") != "video":
            continue
        tags = {str(k).lower(): str(v) for k, v in (st.get("tags") or {}).items()}
        attached = (st.get("disposition") or {}).get("attached_pic", 0) == 1
        out.append((int(st.get("index", 0)), cover_stream(attached, tags.get("comment", ""))))
    return out


def ffmpeg_cover_args(src: str, image: Optional[str], dst: str, streams: List[Tuple[int, bool]]) -> List[str]:
    """The arguments (after the program) that copy `src` to `dst` with the picture file
    `image` as its front cover (the first video stream, after the audio; the file's
    other pictures follow it), or without its covers when `image` is None."""
    args = ["-v", "error", "-nostdin", "-y", "-i", src]
    if image is not None:
        args += ["-i", image, "-map", "0", "-map", "-0:v?", "-map", "1"]
        for i, cover in streams:
            if not cover:
                args += ["-map", f"0:{i}"]
    else:
        args += ["-map", "0"]
        for i, cover in streams:
            if cover:
                args += ["-map", f"-0:{i}"]
    args += ["-c", "copy", "-map_metadata", "0", "-map_chapters", "0", "-fflags", "+bitexact"]
    if image is not None:
        args += ["-disposition:v:0", "attached_pic", "-metadata:s:v:0", "comment=Cover (front)"]
    if os.path.splitext(src)[1].lower() in ID3_CHUNK:
        args += ["-write_id3v2", "1"]
    return args + [dst]


def cannot_hold_cover(path: str) -> str:
    """Why the cover of this file cannot be changed, or ''."""
    ext = os.path.splitext(path)[1].lower()
    name = ext[1:].upper() or "These"
    if ext in NO_MUXER:
        return f"The cover of {name} files cannot be changed here, because FFmpeg can read them but not write them."
    if ext in NO_PICTURE:
        return f"{name} files have no place for a cover picture, so they were left as they are."
    if ext not in NATIVE and ext not in PICTURE_FFMPEG:
        return f"The cover of {name} files cannot be changed here, because FFmpeg cannot write pictures into them."
    return ""


def _ffmpeg_cover(path: str, image: Optional[bytes]) -> str:
    streams = picture_streams(path)
    if image is None and not any(cover for _, cover in streams):
        return "none"
    picture = None
    try:
        if image is not None:
            from .covers import image_format

            fd, picture = tempfile.mkstemp(prefix="realhires-cover-",
                                           suffix=".png" if image_format(image) == "PNG" else ".jpg")
            with os.fdopen(fd, "wb") as fh:
                fh.write(image)
        _ffmpeg_replace(path, lambda tmp: ffmpeg_cover_args(path, picture, tmp, streams), "cover")
    finally:
        if picture is not None:
            try:
                os.unlink(picture)
            except OSError:
                pass
    return "ffmpeg"


def write_cover(path: str, image: Optional[bytes]) -> str:
    """Make `image` the file's front cover, or remove its covers when it is None.
    Returns "native", "ffmpeg", or "none" when there was no cover to remove (the file
    is not touched then).  Raises TagError or OSError, and leaves the file as it was then."""
    why = cannot_hold_cover(path)
    if why:
        raise TagError(why)
    if image is not None:
        check_picture(image)
    ext = os.path.splitext(path)[1].lower()
    if ext in NATIVE:
        with open(path, "rb") as fh:
            data = fh.read()
        try:
            if ext == ".flac":
                new, n = flac_with_cover(data, image)
            elif ext == ".mp3":
                new, n = mp3_with_cover(data, image)
            else:
                new, n = ogg_with_cover(data, image)
        except TagError:
            if ext != ".mp3":
                raise
            return _ffmpeg_cover(path, image)  # ID3v2.2 or unsynchronised tags: FFmpeg copies the file
        if image is None and n == 0:
            return "none"
        embed.replace_file(path, new)
        return "native"
    return _ffmpeg_cover(path, image)


def _cover_files(paths: List[str], image: Optional[bytes], cfg,
                 progress: Optional[Callable[[int, int, str], None]]) -> List[TagOutcome]:
    from . import musiclib, store as store_mod
    from .probe import probe

    verb = "remove" if image is None else "change"
    results = []
    st = store_mod.open_store(cfg) if cfg is not None and getattr(cfg, "save_results", True) else None
    try:
        lib = musiclib.Library()
    except Exception:  # noqa: BLE001
        lib = None
    try:
        for i, path in enumerate(paths):
            if progress is not None:
                progress(i, len(paths), path)
            name = os.path.basename(path)
            why = cannot_hold_cover(path)
            if why:
                results.append(TagOutcome(path, False, why))
                continue
            try:
                before = os.stat(path)
                how = write_cover(path, image)
            except (TagError, OSError) as exc:
                results.append(TagOutcome(path, False, f"Could not {verb} the cover of {name}: {exc}"))
                continue
            if how == "none":
                results.append(TagOutcome(path, True, f"{name} had no cover inside."))
                continue
            try:
                tags = probe(path).tags or {}
            except Exception:  # noqa: BLE001
                tags = {}
            if st is not None:
                st.retag(path, before.st_size, before.st_mtime_ns, tags=tags)
            if lib is not None:
                lib.update_file(path)
            results.append(TagOutcome(path, True, "", tags))
        if progress is not None:
            progress(len(paths), len(paths), "")
    finally:
        if st is not None:
            st.close()
        if lib is not None:
            lib.close()
    return results


def set_cover(paths: List[str], image: bytes, mime: str = "", cfg=None,
              progress: Optional[Callable[[int, int, str], None]] = None) -> List[TagOutcome]:
    """Make the JPEG or PNG `image` the front cover of every file, keep their saved
    results and update the library.  Never raises; one outcome per file (ok with no
    message: changed; not ok: left as it was, the message says why)."""
    try:
        check_picture(image, mime)
    except TagError as exc:
        return [TagOutcome(p, False, str(exc)) for p in paths]
    return _cover_files(list(paths), image, cfg, progress)


def remove_cover(paths: List[str], cfg=None,
                 progress: Optional[Callable[[int, int, str], None]] = None) -> List[TagOutcome]:
    """Remove the covers inside every file (set_cover()'s outcomes; ok with a message:
    the file had no cover inside and was not touched)."""
    return _cover_files(list(paths), None, cfg, progress)


def cover_summary(outcomes: List[TagOutcome], removing: bool = False,
                  notes: Iterable[str] = ()) -> Tuple[str, List[str]]:
    """(headline, sentences) for the Cover window after changing or removing covers;
    `notes` (about the folder image) come last."""
    changed = [o for o in outcomes if o.ok and not o.message]
    empty = [o for o in outcomes if o.ok and o.message]
    bad = [o for o in outcomes if not o.ok]
    n, total = len(changed), len(outcomes)
    s = "s" if n != 1 else ""
    if removing:
        head = (f"Removed the cover from {n:,} song{s}." if changed and n == total else
                f"Removed the cover from {n:,} of {total:,} songs." if changed else
                ("The song had no cover inside." if total == 1 else "None of the songs had a cover inside.")
                if empty and not bad else
                "No cover was removed.")
    else:
        head = (f"Changed the cover of {n:,} song{s}." if changed and n == total else
                f"Changed the cover of {n:,} of {total:,} songs." if changed else
                "No song was changed.")
    lines: List[str] = []
    if empty and (changed or bad):
        lines.append(empty[0].message if len(empty) == 1 else f"{len(empty):,} of the songs had no cover inside.")
    for o in bad:
        if o.message not in lines:
            lines.append(o.message)
    for note in notes:
        if note and note not in lines:
            lines.append(note)
    return head, lines


def folder_saved_note(path: str) -> str:
    return f"The picture was also saved as {os.path.basename(path)} in the album's folder."


def folder_failed_note(path: str, error) -> str:
    return f"Could not save {os.path.basename(path)} in the album's folder: {error}"


def folder_kept_note(path: str) -> str:
    return (f"{os.path.basename(path)} in the album's folder was not deleted; it is shown for the songs "
            f"that have no cover inside.")


# ------------------------------------- Properties: searching the web (2.3) ---
# Every field of the Properties window has a Search button: it opens the browser at a
# Google search for the field (`web_search_url()`), and the window then fills in the
# first text copied within WATCH_SECONDS (`clipboard_value()`), until Save.
WEB_SEARCH_URL = "https://www.google.com/search"
WATCH_SECONDS = 120     # how long the window watches the clipboard after a Search
WATCH_POLL_MS = 500     # how often it looks
# The field's name in a search, in lower case, and in Japanese (as the window's labels:
# "トラック" and "ディスク" with 番号, as "track number" and "disc number" in English).
SEARCH_NAMES = {"title": "title", "artist": "artist", "album": "album", "album_artist": "album artist",
                "genre": "genre", "date": "release date", "track": "track number", "disc": "disc number",
                "composer": "composer"}
SEARCH_NAMES_JA = {"title": "タイトル", "artist": "アーティスト", "album": "アルバム",
                   "album_artist": "アルバムアーティスト", "genre": "ジャンル", "date": "リリース日",
                   "track": "トラック番号", "disc": "ディスク番号", "composer": "作曲者"}


def search_name(field: str, language: str = "en") -> str:
    """The field's name in a web search: "composer", "release date", "track number"…, or its
    Japanese name when `language` is Japanese (a code or the setting's value)."""
    from .i18n import code_of

    names = SEARCH_NAMES_JA if code_of(language) == "ja" else SEARCH_NAMES
    return names.get(field, field.replace("_", " "))


def _unquoted(value) -> str:
    """A value to put in double quotes: one line, without double quotes of its own."""
    return clean_value(clean_value(value).replace('"', ""))


def web_search_query(field: str, values, language: str = "en") -> str:
    """What the Search button of `field` looks for.  `values` holds the window's title,
    artist, album and album artist (None or "" when unknown): with a title (one song)
    '"<title>" <artist> <field>', else (several songs, Album Properties) '"<album>"
    <album artist> <field>'; the artist and the album artist stand in for each other, and
    unknown parts are left out."""
    def get(key):
        return values.get(key) if hasattr(values, "get") else None

    title = _unquoted(get("title"))
    if title:
        parts = [f'"{title}"', clean_value(get("artist")) or clean_value(get("album_artist"))]
    else:
        album = _unquoted(get("album"))
        parts = [f'"{album}"' if album else "", clean_value(get("album_artist")) or clean_value(get("artist"))]
    parts.append(search_name(field, language))
    return " ".join(p for p in parts if p)


def web_search_url(field: str, values, language: str = "en") -> str:
    """The Google search the Search button of `field` opens (`web_search_query()`)."""
    import urllib.parse

    return WEB_SEARCH_URL + "?" + urllib.parse.urlencode([("q", web_search_query(field, values, language))])


def clipboard_value(text) -> str:
    """What a text copied to the clipboard puts into a field: its first line that is not
    empty, as one line (`clean_value()`), cut to MAX_VALUE characters; "" for none."""
    for line in str(text or "").splitlines():
        value = clean_value(line)
        if value:
            return clean_value(value[:MAX_VALUE])
    return ""
