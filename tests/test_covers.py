"""Album covers kept on disk (realhires/covers.py: cache_key/cache_name,
cached_cover_png, trim_cache) and in the player's CoverCache, so scrolling
the album grid does not run FFmpeg again, also not after a restart."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from realhires import covers

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
ICON = str(Path(__file__).resolve().parents[1] / "realhires" / "assets" / "icon.png")


def test_cache_key_and_name_are_spelled_out():
    paths = ["/no/such/Music/A/01.flac", "/no/such/Music/A/02.flac", "/no/such/Music/B/03.flac",
             "/no/such/Music/A/04.flac", "/no/such/Music/C/05.flac"]
    key = covers.cache_key(paths, 176)
    assert key == ("v1\t176\n"
                   "/no/such/Music/A/01.flac\t-1\t-1\n/no/such/Music/A/02.flac\t-1\t-1\n"
                   "/no/such/Music/B/03.flac\t-1\t-1\n/no/such/Music/A/04.flac\t-1\t-1\n"
                   "/no/such/Music/A\t-1\n/no/such/Music/B\t-1")  # the fifth song and its folder are not used
    name = covers.cache_name(paths, 176)
    assert name == hashlib.sha1(key.encode("utf-8")).hexdigest() + ".png"
    assert covers.cache_name(paths[:4], 176) == name
    assert covers.cache_name(paths, 220) != name
    assert covers.cache_name(["/no/such/日本語/曲.flac"], 44).endswith(".png")


def test_cache_key_uses_sizes_and_times(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    song = album / "01.flac"
    song.write_bytes(b"x" * 1234)
    os.utime(song, ns=(1_700_000_000_000_000_000, 1_700_000_001_000_000_000))
    os.utime(album, ns=(1_700_000_000_000_000_000, 1_700_000_002_000_000_000))
    assert covers.cache_key([str(song)], 150) == (f"v1\t150\n{song}\t1234\t1700000001000000000\n"
                                                   f"{album}\t1700000002000000000")


def test_cache_dir_follows_the_environment(monkeypatch, tmp_path):
    assert covers.cache_dir() == str(tmp_path / "covers")  # conftest keeps tests away from the user's folder
    monkeypatch.delenv("REALHIRES_COVERS")
    from realhires.store import data_dir

    assert covers.cache_dir() == os.path.join(data_dir(), "covers")


def _album(root: Path, name: str, songs: int = 2, image: bool = True):
    """WAV songs (no embedded picture) and, optionally, a folder.png."""
    d = root / name
    d.mkdir(parents=True)
    paths = []
    for n in range(1, songs + 1):
        p = d / f"{n:02d}.wav"
        p.write_bytes(_WAV)
        paths.append(str(p))
    if image:
        shutil.copy(ICON, d / "folder.png")
    return paths


def _wav() -> bytes:
    import struct

    data = b"\x00\x00" * 800
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16) + b"data" + struct.pack("<I", len(data)) + data)


_WAV = _wav()


@pytest.fixture
def ffmpeg_runs(monkeypatch):
    """Counts the FFmpeg runs of covers._thumbnail."""
    runs = []
    real = covers._thumbnail

    def counting(source, size, embedded):
        runs.append(source)
        return real(source, size, embedded)

    monkeypatch.setattr(covers, "_thumbnail", counting)
    return runs


@needs_ffmpeg
def test_a_cover_is_made_once_then_read_from_disk(tmp_path, ffmpeg_runs):
    paths = _album(tmp_path / "Music", "Album")
    png = covers.cached_cover_png(paths, 64)
    assert png and png.startswith(b"\x89PNG") and ffmpeg_runs
    file = Path(covers.cache_file(paths, 64))
    assert file.read_bytes() == png and file.parent == tmp_path / "covers"
    assert not [n for n in os.listdir(file.parent) if n.endswith(".tmp")]
    os.utime(file, (1_000_000, 1_000_000))
    made = len(ffmpeg_runs)
    assert covers.cached_cover_png(paths, 64) == png
    assert len(ffmpeg_runs) == made  # read from disk, no FFmpeg
    assert file.stat().st_mtime > 1_000_000  # reading touched it, for trim_cache()
    assert covers.cached_cover_png(paths, 32) != png and len(ffmpeg_runs) > made  # one file per size


@needs_ffmpeg
def test_an_album_without_cover_is_remembered(tmp_path, ffmpeg_runs):
    paths = _album(tmp_path / "Music", "Plain", image=False)
    assert covers.cached_cover_png(paths, 64) is None
    file = Path(covers.cache_file(paths, 64))
    assert file.is_file() and file.stat().st_size == 0 and covers.is_cached(paths, 64)
    made = len(ffmpeg_runs)
    assert covers.cached_cover_png(paths, 64) is None and len(ffmpeg_runs) == made


def test_no_mark_when_ffmpeg_could_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(covers, "tool", lambda name: str(tmp_path / "no-ffmpeg"))
    paths = _album(tmp_path / "Music", "Album")
    assert covers.cached_cover_png(paths, 64) is None
    assert not covers.is_cached(paths, 64)  # tried again next time, when FFmpeg is there


@needs_ffmpeg
def test_a_changed_song_or_folder_gives_a_new_entry(tmp_path, ffmpeg_runs):
    paths = _album(tmp_path / "Music", "Album", songs=4)
    first = covers.cache_name(paths, 64)
    covers.cached_cover_png(paths, 64)
    made = len(ffmpeg_runs)
    os.utime(paths[1], ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))  # a re-tagged song
    second = covers.cache_name(paths, 64)
    assert second != first
    covers.cached_cover_png(paths, 64)
    assert len(ffmpeg_runs) > made
    folder = os.path.dirname(paths[0])
    os.utime(folder, ns=(1_600_000_000_000_000_000, 1_600_000_005_000_000_000))  # e.g. a new cover.jpg
    assert covers.cache_name(paths, 64) not in (first, second)
    assert covers.cache_name(paths + ["/elsewhere/05.flac"], 64) == covers.cache_name(paths, 64)


@needs_ffmpeg
def test_a_damaged_file_is_made_again(tmp_path, ffmpeg_runs):
    paths = _album(tmp_path / "Music", "Album")
    png = covers.cached_cover_png(paths, 64)
    file = Path(covers.cache_file(paths, 64))
    file.write_bytes(png[: len(png) // 2])  # cut short
    made = len(ffmpeg_runs)
    assert covers.cached_cover_png(paths, 64) == png and len(ffmpeg_runs) > made
    assert file.read_bytes() == png
    file.write_bytes(b"not a png at all")
    assert covers.cached_cover_png(paths, 64) == png


def test_trim_cache_deletes_the_least_recently_used(tmp_path):
    d = tmp_path / "trim"
    d.mkdir()
    for i, (name, size) in enumerate((("a.png", 5000), ("b.png", 100), ("c.png", 0), ("d.png", 9000),
                                      ("e.png", 4096))):
        p = d / name
        p.write_bytes(b"x" * size)
        os.utime(p, (1_000_000 + i, 1_000_000 + i))  # a is the oldest
    old_tmp, young_tmp, other = d / ".cover-1.tmp", d / ".cover-2.tmp", d / "notes.txt"
    for p in (old_tmp, young_tmp, other):
        p.write_bytes(b"x")
    os.utime(old_tmp, (1_000_000, 1_000_000))
    # usage in 4 KB blocks: a 8 KB, b 4 KB, c 4 KB, d 12 KB, e 4 KB = 32 KB
    assert covers.trim_cache(32 * 1024, str(d)) == 0
    assert covers.trim_cache(20 * 1024, str(d)) == 2  # a and b go
    assert sorted(os.listdir(d)) == [".cover-2.tmp", "c.png", "d.png", "e.png", "notes.txt"]
    assert covers.trim_cache(0, str(d)) == 3
    assert covers.trim_cache(0, str(tmp_path / "missing")) == 0


# ---------------------------------------------------------- the player ---
def _gui_available():
    if not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
        return False
    try:
        import tkinter

        tkinter.Tk().destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


SCROLL_SCRIPT = r'''
import faulthandler, os, sys, time
sys.path.insert(0, REPO)
faulthandler.enable()
faulthandler.dump_traceback_later(450, exit=True)  # (a slow macOS runner took over 2 minutes to make the 36 covers ahead)
import tkinter as tk
from realhires import covers, musiclib
from realhires.player_theme import Theme
from realhires.player_widgets import AlbumGrid, CoverCache

runs = []
outcomes = []   # (source, PNG made, FFmpeg ran to the end, seconds) of every run
real = covers._thumbnail
def counting(source, size, embedded):
    runs.append(source)
    t = time.time()
    data, ok = real(source, size, embedded)
    outcomes.append((source, data is not None, ok, time.time() - t))
    return data, ok
covers._thumbnail = counting

finished = []   # albums (first path) whose cover the background pass has looked at
keys = {}       # first path -> the cache key when its cover was made
real_cached = covers.cached_cover_png
def cached_counting(paths, size):
    keys[list(paths)[0]] = covers.cache_key(paths, size)
    try:
        return real_cached(paths, size)
    except BaseException as e:
        print("cached_cover_png(%r) raised %r" % (list(paths)[0], e), file=sys.stderr, flush=True)
        raise
    finally:
        finished.append(list(paths)[0])
covers.cached_cover_png = cached_counting

written = {}    # cover file -> bytes written, or the error
real_write = covers._write
def writing(file, data):
    real_write(file, data)
    written[file] = len(data) if os.path.isfile(file) else "missing right after writing"
covers._write = writing

tracks = []
for i in range(ALBUMS):
    d = os.path.join(MUSIC, f"Album {i:02d}")
    for n in (1, 2):
        p = os.path.join(d, f"{n:02d}.wav")
        st = os.stat(p)
        tracks.append(musiclib.Track(path=p, title=f"Song {n}", artist="Someone", album=f"Album {i:02d}",
                                     track=n, size=st.st_size, mtime_ns=st.st_mtime_ns))
albums = musiclib.albums(tracks)
assert len(albums) == ALBUMS

root = tk.Tk()
root.geometry("900x560")
theme = Theme(root)
theme.styles()

import _tkinter

def events(limit=1000):
    """Handle the Tk events waiting now, at most `limit` of them.  Not Tk's update:
    on the macOS arm64 runner its first call sometimes never returned (for over 7
    minutes, with the covers made long before), so neither the test's own timeout
    nor its diagnostics could fire."""
    for _ in range(limit):
        if not root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT):
            break

def pump(cond, what, timeout=60):
    t0 = time.time()
    while not cond():
        assert time.time() - t0 < timeout, "timed out: " + (what() if callable(what) else what)
        u0 = time.time()
        events()
        if time.time() - u0 > 2:  # (to find where a hang on a macOS runner sits)
            print("handling Tk events took %.1f s" % (time.time() - u0), file=sys.stderr, flush=True)
        time.sleep(0.01)

def settle(cond, what, timeout=60):
    """Wait for background threads only, without Tk (the covers made ahead go to disk)."""
    t0 = time.time()
    while not cond():
        assert time.time() - t0 < timeout, "timed out: " + (what() if callable(what) else what)
        time.sleep(0.02)

def watchdog():
    # every 30 s: the progress of the covers made ahead and every thread's stack, so a
    # hang shows where it sits (the CI's Apple Silicon runners hung here twice)
    import threading, traceback
    start = time.time()
    while True:
        time.sleep(30)
        print("--- %.0f s: %d FFmpeg runs, %d queued" % (time.time() - start, len(runs), cache.prefetching()
                                                       if "cache" in globals() else -1), file=sys.stderr, flush=True)
        for tid, frame in sys._current_frames().items():
            name = next((t.name for t in threading.enumerate() if t.ident == tid), "?")
            print("thread %s:\n%s" % (name, "".join(traceback.format_stack(frame)[-6:])), file=sys.stderr, flush=True)

import threading as _threading
_threading.Thread(target=watchdog, daemon=True, name="watchdog").start()

def scroll_through(grid, cache):
    """Scroll from the top to the end; return the albums shown."""
    shown = set()
    grid.set_albums(albums)
    pump(lambda: grid.canvas.winfo_height() > 100, "the grid")
    grid.relayout()
    steps = 25
    for i in range(steps + 1):
        grid._yview("moveto", i / steps)
        events()
        top = grid.canvas.canvasy(0)
        bottom = top + grid.canvas.winfo_height()
        visible = [a["key"] for a in albums if top <= grid.positions[a["key"]][1] < bottom]
        pump(lambda: all((k, grid.COVER) in cache.images for k in visible), "covers on screen")
        shown.update(visible)
    return shown

# first session: the library is read, the grid covers are made ahead
cache = CoverCache(root)
cache.library_changed(albums)
def unsaved():
    return [a for a in albums if not covers.is_cached([t.path for t in a["tracks"]], cache.GRID)]

# A cover whose FFmpeg run timed out or could not start is not saved: the app makes
# it when it is shown.  The slow macOS runners (about 3 s per FFmpeg run) had such a
# run once, so the pass is repeated for those albums, and what happened is printed.
todo = albums
for attempt in range(3):
    finished.clear()
    cache.prefetch(todo)
    settle(lambda: len(finished) >= len(todo) and cache.prefetching() == 0,
         lambda: "made ahead: %d FFmpeg runs, %d of %d covers looked at, %d queued" % (
             len(runs), len(finished), len(todo), cache.prefetching()), timeout=300)
    todo = unsaved()
    if not todo:
        break
    print("not saved after pass %d: %s; slow or failed FFmpeg runs: %s" % (
        attempt + 1, [a["key"] for a in todo],
        [o for o in outcomes if not o[2] or o[3] > 10 or os.path.dirname(o[0]) in
         {os.path.dirname(a["tracks"][0].path) for a in todo}]), file=sys.stderr, flush=True)
    for a in todo:
        paths = [t.path for t in a["tracks"]]
        file = covers.cache_file(paths, cache.GRID)
        print("  %s: written %r, cache key then %r, now %r; %d files in the covers folder" % (
            a["key"], written.get(file, "never"), keys.get(paths[0]), covers.cache_key(paths, cache.GRID),
            len(os.listdir(covers.cache_dir()))), file=sys.stderr, flush=True)
assert not todo, [a["key"] for a in todo]
made = len(runs)
assert made >= ALBUMS, made
assert cache.images == {}, "made ahead only on disk"
grid = AlbumGrid(root, theme, cache, lambda a: None)
grid.pack(fill="both", expand=True)
t0 = time.perf_counter()
shown = scroll_through(grid, cache)
first_scroll = time.perf_counter() - t0
assert len(shown) == ALBUMS, len(shown)
assert len(runs) == made, f"FFmpeg ran {len(runs) - made} times while scrolling"
assert all((a["key"], grid.COVER) in cache.images for a in albums)  # all kept for the session
t0 = time.perf_counter()
grid._yview("moveto", 0)
events()
assert len(runs) == made

# the library is read again: unchanged albums keep their covers, a changed one is made again
cache.library_changed(albums)
assert all((a["key"], grid.COVER) in cache.images for a in albums)
changed = albums[3]
t = changed["tracks"][0]
os.utime(t.path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
st = os.stat(t.path)
t.size, t.mtime_ns = st.st_size, st.st_mtime_ns
cache.library_changed(albums)
assert (changed["key"], grid.COVER) not in cache.images
assert sum((a["key"], grid.COVER) in cache.images for a in albums) == ALBUMS - 1
cache.get(changed["key"], [x.path for x in changed["tracks"]], grid.COVER)
pump(lambda: (changed["key"], grid.COVER) in cache.images, "the changed album")
assert len(runs) > made
cache.close()
grid.destroy()

# a restart: everything comes from the covers folder
runs.clear()
cache2 = CoverCache(root)
cache2.library_changed(albums)
grid2 = AlbumGrid(root, theme, cache2, lambda a: None)
grid2.pack(fill="both", expand=True)
t0 = time.perf_counter()
shown = scroll_through(grid2, cache2)
second_scroll = time.perf_counter() - t0
assert len(shown) == ALBUMS and runs == [], runs
print(f"scrolled {ALBUMS} albums: {first_scroll:.2f} s (made ahead), {second_scroll:.2f} s (after a restart), "
      "no FFmpeg run")
cache2.close()
root.destroy()
print("OK")
'''


@needs_ffmpeg
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_scrolling_the_album_grid_does_not_run_ffmpeg_again(tmp_path):
    music = tmp_path / "Music"
    albums = 36
    for i in range(albums):
        _album(music, f"Album {i:02d}")
    script = tmp_path / "scroll.py"
    script.write_text(f"REPO = {str(Path(__file__).resolve().parents[1])!r}\nMUSIC = {str(music)!r}\n"
                      f"ALBUMS = {albums}\n" + SCROLL_SCRIPT, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=480)
    log = proc.stdout + proc.stderr
    assert proc.returncode == 0, log
    assert proc.stdout.splitlines()[-1] == "OK", log
    assert len(os.listdir(tmp_path / "covers")) >= albums
    print(proc.stdout.splitlines()[-2])  # the scroll times (pytest -s shows them)
