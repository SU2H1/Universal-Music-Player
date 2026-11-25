"""Editing a song's information (2.0, `realhires/tagedit.py`) on real files:
FLAC and MP3 natively, the other formats through FFmpeg's stream copy.  The
audio must come out sample for sample the same, the other tags and the cover
must stay, the saved scan result must stay valid with the new tags, and the
library must read the new values.  Also the themes folder."""

import hashlib
import json
import os
import shutil
import subprocess

import pytest

from realhires import musiclib, store, tagedit, themes
from realhires.probe import probe
from realhires.settings import Settings
from realhires.verdict import analyze_file

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _encoders() -> str:
    try:
        return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except OSError:
        return ""


def make(path, codec_args, tags=None):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "2"]
    for k, v in (tags or {"title": "Old", "artist": "Art", "album": "Alb", "album_artist": "AA", "date": "2001",
                         "track": "2/9", "comment": "keep me"}).items():
        cmd += ["-metadata", f"{k}={v}"]
    subprocess.run(cmd + codec_args + [str(path)], check=True)
    return str(path)


def pcm(path) -> str:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "s32le", "-"],
                         capture_output=True, check=True).stdout
    return hashlib.sha1(out).hexdigest()


CHANGES = {"title": "Neue Ünïcode 日本", "album_artist": "", "date": "2020-05-28", "composer": "Bach",
           "track": "04/12", "disc": "1/2", "genre": "Jazz"}


def formats():
    enc = _encoders()
    out = [("flac", ["-c:a", "flac"]), ("m4a", ["-c:a", "aac"]), ("alac.m4a", ["-c:a", "alac"]),
           ("wav", ["-c:a", "pcm_s16le"]), ("aiff", ["-c:a", "pcm_s16be", "-write_id3v2", "1"]),
           ("wv", ["-c:a", "wavpack"])]
    if "libmp3lame" in enc:
        out += [("mp3", ["-c:a", "libmp3lame", "-id3v2_version", "3"]),
                ("v4.mp3", ["-c:a", "libmp3lame", "-id3v2_version", "4"])]
    if "libvorbis" in enc:
        out.append(("ogg", ["-c:a", "libvorbis"]))
    if "libopus" in enc:
        out.append(("opus", ["-c:a", "libopus"]))
    return out


@needs_ffmpeg
@pytest.mark.parametrize("ext,args", formats(), ids=[f[0] for f in formats()])
def test_editing_keeps_the_audio_and_the_other_tags(tmp_path, ext, args):
    song = make(tmp_path / f"song.{ext}", args)
    before = pcm(song)
    out = tagedit.save_tags([song], CHANGES)
    assert len(out) == 1 and out[0].ok, out[0].message
    assert pcm(song) == before, "the audio changed"
    tags = probe(song).tags
    assert tags["title"] == CHANGES["title"] and tags["genre"] == "Jazz"
    assert tags["date"] == "2020-05-28"
    assert tagedit.split_number(tags["track"])[0] == "4"
    assert tags["artist"] == "Art" and tags["album"] == "Alb", "a field that was not changed was lost"
    if ext == "wav":  # RIFF INFO has no album artist, composer or disc
        assert "has no place for the disc and the composer" in out[0].message
    else:
        assert "album_artist" not in tags, "an emptied field must be removed"
        assert tags["composer"] == "Bach" and out[0].message == ""
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".realhires")], "a temporary file was left"


@needs_ffmpeg
def test_flac_keeps_track_totals_cover_and_padding(tmp_path):
    song = make(tmp_path / "a.flac", ["-c:a", "flac"], {"title": "Old", "track": "2", "TRACKTOTAL": "9"})
    with open(song, "rb") as fh:
        size = len(fh.read())
    assert tagedit.save_tags([song], {"track": "3"})[0].ok
    with open(song, "rb") as fh:
        data = fh.read()
    assert b"TRACKNUMBER=3" in data and b"TRACKTOTAL=9" in data, "a number alone keeps the number of tracks"
    assert len(data) == size, "the padding should absorb a small change"
    assert tagedit.save_tags([song], {"track": "3/10"})[0].ok
    with open(song, "rb") as fh:
        data = fh.read()
    assert b"TRACKTOTAL=10" in data and b"TRACKTOTAL=9" not in data
    assert tagedit.save_tags([song], {"track": ""})[0].ok
    with open(song, "rb") as fh:
        data = fh.read()
    assert b"TRACKNUMBER" not in data and b"TRACKTOTAL" not in data


@needs_ffmpeg
def test_m4a_keeps_its_cover(tmp_path):
    song = make(tmp_path / "a.m4a", ["-c:a", "aac"])
    cover = tmp_path / "c.png"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=32x32:d=1", "-frames:v", "1",
                    str(cover)], check=True)
    with_cover = str(tmp_path / "b.m4a")
    subprocess.run(["ffmpeg", "-v", "error", "-i", song, "-i", str(cover), "-map", "0", "-map", "1", "-c", "copy",
                    "-disposition:v", "attached_pic", with_cover], check=True)
    assert tagedit.save_tags([with_cover], {"album": "New"})[0].ok
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:stream_disposition="
                              "attached_pic", "-of", "json", with_cover], capture_output=True, text=True).stdout
    kinds = [(s["codec_type"], s["disposition"]["attached_pic"]) for s in json.loads(streams)["streams"]]
    assert ("video", 1) in kinds, kinds


@needs_ffmpeg
def test_saved_result_and_library_follow_the_new_tags(tmp_path):
    song = make(tmp_path / "a.flac", ["-c:a", "flac"])
    cfg = Settings()
    skey = store.settings_key(cfg)
    st = store.open_store(cfg)
    st.put(analyze_file(song, settings=cfg), skey)
    lib = musiclib.Library()
    lib.refresh([song])
    out = tagedit.save_tags([song], {"album": "Brand New", "album_artist": "Someone"}, cfg)
    assert out[0].ok, out[0].message
    again = st.lookup(song, skey)
    assert again is not None, "the saved result was dropped although only the tags changed"
    assert again.tags["album"] == "Brand New" and again.tags["album_artist"] == "Someone"
    st.close()
    track = next(t for t in lib.tracks() if t.path == os.path.abspath(song))
    assert track.album == "Brand New" and track.album_artist == "Someone"
    lib.close()


def test_typed_values_are_checked():
    assert tagedit.check_changes({"track": " 3 / 12 ", "disc": "01", "genre": " Rock\n"}) == {
        "genre": "Rock", "track": "3/12", "disc": "1"}
    for bad, word in (({"track": "three"}, "track"), ({"disc": "1/x"}, "disc"), ({"date": "May 2004"}, "date"),
                      ({"title": "x" * 1001}, "too long")):
        with pytest.raises(tagedit.TagError, match=word):
            tagedit.check_changes(bad)
    assert tagedit.common_values([{"album": "X", "title": "A"}, {"album": " X", "title": "B"}]) == {
        **{f: "" for f in tagedit.FIELDS}, "album": "X", "title": None}


def test_formats_ffmpeg_cannot_write_are_refused(tmp_path):
    p = tmp_path / "a.ape"
    p.write_bytes(b"MAC \x00\x00")
    out = tagedit.save_tags([str(p)], {"title": "x"})
    assert not out[0].ok and "APE files cannot be changed here" in out[0].message
    assert p.read_bytes() == b"MAC \x00\x00"


def test_damaged_files_are_left_alone(tmp_path):
    p = tmp_path / "a.flac"
    p.write_bytes(b"fLaC\x00\x00\x00\x22" + b"\x00" * 10)
    out = tagedit.save_tags([str(p)], {"title": "x"})
    assert not out[0].ok and "metadata" in out[0].message
    assert p.read_bytes() == b"fLaC\x00\x00\x00\x22" + b"\x00" * 10
    head, lines = tagedit.save_summary(out)
    assert head == "Nothing was saved." and lines == [out[0].message]


def test_themes_folder_files_appear_in_the_menu(tmp_path, monkeypatch):
    monkeypatch.setenv("REALHIRES_THEMES", str(tmp_path / "themes.json"))
    folder = themes.folder()
    assert folder == str(tmp_path / "themes")
    os.makedirs(folder)
    with open(os.path.join(folder, "neon.json"), "w", encoding="utf-8") as fh:
        fh.write(themes.export_theme({"name": "Neon", "dark": True, "colors": {"accent": "#39ff14"}}))
    with open(os.path.join(folder, "broken.json"), "w", encoding="utf-8") as fh:
        fh.write("{")
    own = themes.load_custom()
    files = themes.load_folder(own)
    assert [t["name"] for t in files] == ["Neon"] and files[0]["file"].endswith("neon.json")
    assert "Neon" in themes.names(own + files)
    assert themes.resolve("Neon", False, own + files)["colors"]["accent"] == "#39ff14"
    themes.save_custom(own + files)  # a folder theme saved by mistake must not keep its file name
    assert all("file" not in t for t in themes.load_custom())


def _clean_decode(path) -> str:
    """FFmpeg's warnings while decoding the whole file (a bad page checksum shows here)."""
    return subprocess.run(["ffmpeg", "-v", "warning", "-nostdin", "-i", str(path), "-f", "null", "-"],
                          capture_output=True, text=True).stderr.strip()


@needs_ffmpeg
@pytest.mark.parametrize("ext,codec", [("ogg", "libvorbis"), ("opus", "libopus")])
def test_ogg_headers_over_several_pages(tmp_path, ext, codec):
    if codec not in _encoders():
        pytest.skip(f"this FFmpeg has no {codec} encoder")
    big = "Long title " * 9000  # about 100 KB: the comment header spans two pages
    # too long for a command line on Windows (32 KB), so FFmpeg reads the tags from a metadata file
    meta = tmp_path / "meta.txt"
    meta.write_text(f";FFMETADATA1\ntitle={big}\nartist=Art\ncomment=keep me\n", encoding="utf-8")
    song = str(tmp_path / f"big.{ext}")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-i",
                    str(meta), "-map", "0", "-map_metadata", "1", "-ac", "2", "-c:a", codec, song], check=True)
    assert probe(song).tags["title"] == big.strip()
    before = pcm(song)
    out = tagedit.save_tags([song], {"title": "Short", "date": "1999"})  # the header shrinks to one page
    assert out[0].ok, out[0].message
    assert pcm(song) == before and _clean_decode(song) == ""
    tags = probe(song).tags
    assert (tags["title"], tags["artist"], tags["date"]) == ("Short", "Art", "1999")
    with open(song, "rb") as fh:
        data = fh.read()
    grown = tmp_path / f"grown.{ext}"
    grown.write_bytes(tagedit.ogg_with_tags(data, {"title": big.strip()}))  # and grows again
    assert pcm(grown) == before and _clean_decode(grown) == ""
    assert probe(str(grown)).tags["title"] == big.strip()
    assert b"keep me" in grown.read_bytes()


def test_ogg_crc_and_refusals():
    assert tagedit.ogg_crc(b"") == 0 and tagedit.ogg_crc(b"123456789") == 0x89A1897F  # CRC-32/MPEG-2 without xor
    with pytest.raises(tagedit.TagError):
        tagedit.ogg_with_tags(b"OggS" + b"\x00" * 30, {"title": "x"})
    with pytest.raises(tagedit.TagError):
        tagedit.ogg_with_tags(b"not an ogg file at all, just some bytes", {"title": "x"})


def test_replacing_a_file_waits_while_windows_says_it_is_in_use(tmp_path, monkeypatch):
    """2.4: on Windows a song that FFmpeg is reading (the mood analysis, a scan) or an
    antivirus scanner holds cannot be replaced for a moment; the replace tries again
    instead of failing the save, and gives up after REPLACE_RETRY_S."""
    from realhires import embed

    src, dst = tmp_path / "new.flac", tmp_path / "song.flac"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    real = os.replace
    calls = []

    def busy_twice(a, b):
        calls.append(a)
        if len(calls) <= 2:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        real(a, b)

    monkeypatch.setattr(embed.os, "name", "nt")
    monkeypatch.setattr(embed.os, "replace", busy_twice)
    embed.replace_retrying(str(src), str(dst))
    assert len(calls) == 3 and dst.read_bytes() == b"new"
    # still in use after the time allowed: the error comes through
    monkeypatch.setattr(embed.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(32, "in use")))
    with pytest.raises(PermissionError):
        embed.replace_retrying(str(src), str(dst), timeout=0.2)
    # elsewhere a rename never waits for readers: no retry
    monkeypatch.setattr(embed.os, "name", "posix")
    calls.clear()
    monkeypatch.setattr(embed.os, "replace", busy_twice)
    with pytest.raises(PermissionError):
        embed.replace_retrying(str(src), str(dst))
    assert len(calls) == 1
