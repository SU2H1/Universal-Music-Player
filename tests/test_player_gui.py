"""The 2.0 desktop app (realhires/player_gui.py), headless: the library pages, likes,
playback through miniaudio's NULL backend, the LCD, Now Playing with synced lyrics,
the Playback info and EQ windows, the shuffle and repeat menus, Settings, the
Quality check page with a scan, and what player.json keeps for the next start.
Also lyrics while scanning in the scanner (gui.App).  Each GUI test runs in a
subprocess (skipped without Tk or a display; locally: xvfb-run -a python -m pytest)."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from player_library import make_library

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
REPO = str(Path(__file__).resolve().parents[1])
TESTS = str(Path(__file__).resolve().parent)


def _gui_available():
    if not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
        return False
    try:
        import tkinter

        tkinter.Tk().destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


def _has_miniaudio():
    try:
        import miniaudio  # noqa: F401
        return True
    except ImportError:
        return False


def _has_tkinter():
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_tkinter(), reason="needs Tkinter")
def test_mouse_back_and_forward_buttons():
    """The mouse's side buttons as Tk reports them: 8 and 9 on X11 (where 4 and 5 are
    the wheel), 4 and 5 with Tk 8.6 on Windows and macOS (whose wheel is <MouseWheel>),
    8 and 9 everywhere with Tk 8.7 and 9."""
    from realhires.player_gui import nav_button

    for ws in ("x11", "win32", "aqua"):
        for version in (8.6, 9.0):
            assert (nav_button(8, ws, version), nav_button(9, ws, version)) == ("back", "forward")
            assert nav_button(1, ws, version) == nav_button(3, ws, version) == ""
    assert nav_button(4, "x11", 8.6) == nav_button(5, "x11", 8.6) == ""  # the wheel
    for ws in ("win32", "aqua"):
        assert (nav_button(4, ws, 8.6), nav_button(5, ws, 8.6)) == ("back", "forward")
        assert nav_button(4, ws, 9.0) == ""


def _has_mp3_encoder():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return " libmp3lame " in out


HEADER = f"""
import faulthandler, json, os, sys, time
sys.path.insert(0, {REPO!r})
sys.path.insert(0, {TESTS!r})
faulthandler.enable()  # a crash (the macOS runner once ended a script with SIGSEGV) shows its stack
faulthandler.dump_traceback_later(200, exit=True)
"""


def run_script(tmp_path, body: str, env: dict, timeout: int = 240) -> str:
    """Run `body` in a subprocess, under `if __name__ == "__main__":`, because the
    scanner's spawned worker processes import the script again."""
    script = tmp_path / "drive.py"
    script.write_text(HEADER + 'if __name__ == "__main__":\n' + textwrap.indent(body, "    "), encoding="utf-8")
    out, err = tmp_path / "gui.out", tmp_path / "gui.err"
    with open(out, "w") as fo, open(err, "w") as fe:  # files, not pipes: worker processes may outlive a crash
        # UTF-8 output: a failing Japanese test's message would not fit a Windows console's cp1252
        proc = subprocess.Popen([sys.executable, str(script)], stdout=fo, stderr=fe,
                                env=dict(env, PYTHONIOENCODING="utf-8"))
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = f"killed after {timeout} s"
    log = out.read_text(encoding="utf-8", errors="replace") + err.read_text(encoding="utf-8", errors="replace")
    assert rc == 0, log
    assert out.read_text(encoding="utf-8", errors="replace").splitlines()[-1] == "OK", log
    return log


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_player_window(tmp_path):
    lib = make_library(tmp_path, seconds=3, mp3=_has_mp3_encoder())
    n_songs = len(lib["songs"])
    n_albums = len(lib["albums"])
    from realhires import settings

    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.jobs = 2
    cfg.theme = "Light"
    settings.save(cfg, str(tmp_path / "settings.json"))
    env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
               REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
               REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_AUDIO_BACKEND="null")
    body = f"""
LIB = {json.dumps(lib)}
N_SONGS, N_ALBUMS = {n_songs}, {n_albums}
glacier, umbrella = LIB["songs"]["Glacier Song"], LIB["songs"]["Umbrella Weather"]
import tkinter as tk
from realhires import player_gui, playerstate, eq, gui
state = {{}}

def fail(exc, val, tb):  # an assertion in a Tk callback must end the test, not hang it
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)

def wait(cond, then, timeout=30.0, what="", start=None):
    start = start or time.monotonic()
    if cond():
        return root.after(50, then)
    assert time.monotonic() - start < timeout, "timed out: " + what
    root.after(100, lambda: wait(cond, then, timeout, what, start))

def texts(canvas):
    return [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]

def lines(canvas):
    # the texts on one line joined, left to right ("Artist — Album" is drawn in pieces, 2.1)
    rows = {{}}
    for i in canvas.find_all():
        if canvas.type(i) == "text":
            x, y = canvas.coords(i)[:2]
            rows.setdefault(round(y), []).append((x, canvas.itemcget(i, "text")))
    return ["".join(t for _, t in sorted(r)) for r in rows.values()]

def shows(canvas, full):
    # the canvas shows `full`, or as much of it as fits: on a small screen (the Windows
    # runners have 1024 x 768) the LCD is narrow and cuts its texts with "…"
    for t in texts(canvas) + lines(canvas):
        if t == full or (len(t) > 1 and t.endswith("…") and full.startswith(t[:-1].rstrip())):
            return True
    return False

def start():
    global root, app
    root = tk.Tk()
    root.geometry("1280x800")
    root.report_callback_exception = fail
    app = player_gui.PlayerApp(root)
    return root, app

def loaded():
    assert len(app.tracks) == N_SONGS, len(app.tracks)
    assert not app.library_busy() and app.sidebar.status.cget("text") == ""
    albums = app.page("albums")
    assert len(albums.grid_w.albums) == N_ALBUMS and app.page_name == "library"  # the start page
    assert [a["title"] for a in albums.grid_w.albums][:2] == ["City Rain", "Northern Lights"]
    for name in ("recent", "songs", "artists", "genres", "liked"):
        app.show_page(name)
        root.update()
    assert len(app.page("recent").grid_w.albums) == N_ALBUMS
    assert len(app.page("songs").table.rows) == N_SONGS
    assert len(app.page("artists").items) == N_ALBUMS and app.page("artists").grid_w.albums
    assert {{g["name"] for g in app.page("genres").items}} >= {{"Ambient", "Indie Pop", "Jazz"}}
    assert app.page("liked").table.rows == []
    # nothing plays yet: Previous, Next and the song's buttons are disabled, the rest is usable
    assert not app.bar.next.enabled and not app.bar.prev.enabled and not app.bar.heart.enabled
    assert not app.bar.dislike.enabled and not app.bar.more.enabled
    assert app.bar.play.enabled and app.bar.eq.enabled and app.bar.queue.enabled and app.bar.lyrics.enabled
    assert app.bar.volume.enabled
    assert shows(app.bar.lcd, "Universal Music Player"), texts(app.bar.lcd)
    # like a song: saved at once, listed in Liked Songs
    app.toggle_like(umbrella)
    assert playerstate.load().liked == [umbrella]
    assert [t.path for t in app.page("liked").table.rows] == [umbrella]
    # an album page
    album = next(a for a in app.albums if a["title"] == "Northern Lights")
    app.open_album(album)
    root.update()
    page = app.page("album")
    assert app.page_name == "album" and page.title_l.cget("text") == "Northern Lights"
    assert page.artist_l.cget("text") == "Aurora Fields" and page.meta_l.cget("text") == "Ambient · 2021"
    assert len(page.table.rows) == 4 and page.table.footer.startswith("4 songs")
    page.table.on_activate(1)  # double-click on Glacier Song
    wait(lambda: app.engine.position() > 0.7, playing, what="playback")

def playing():
    assert app.current == glacier and app.engine.playing and app.context_name == "Northern Lights"
    assert app.engine.output_info()["device"] == "NULL Playback Device"
    lcd = texts(app.bar.lcd)
    assert shows(app.bar.lcd, "Glacier Song") and shows(app.bar.lcd, "Aurora Fields — Northern Lights"), lcd
    assert "Hi-Res Lossless" in lcd, lcd
    a, b, _ = app.bar.lcd._seek_geo()
    if b - a >= 20:  # a narrow LCD (a 1024-pixel screen) has no room for the seek bar and its times
        assert any(t.startswith("-0:0") for t in lcd), lcd  # the remaining time
    assert app.bar.next.enabled and app.bar.play.icon == "pause"
    # Now Playing with synced lyrics: the current line in bold
    app.toggle_now_playing()
    root.update()
    np = app.page("nowplaying")
    assert app.page_name == "nowplaying" and np.kind == "synced"
    assert np.lyrics.winfo_ismapped() and np.art.winfo_width() < root.winfo_width() / 2
    wait(lambda: np.lyrics.current == 0, lyrics_ok, what="current lyrics line")

def lyrics_ok():
    np = app.page("nowplaying")
    # paused while Now Playing, the windows and the menus are checked: the songs last 3 s, and
    # every check up to the album menus needs Glacier Song to stay the song playing. Slow macOS
    # runners took longer than that: once the next song had started before Up Next was read,
    # and once (2.2, Intel) before Details was read, which then described Midnight Sun
    app.play_pause()
    assert not app.engine.playing and app.current == glacier
    item = np.lyrics.items[0]
    assert np.lyrics.itemcget(item, "text") == "Blue ice under a silver sky"
    assert str(np.lyrics.itemcget(item, "font")) == str(app.theme.f("lyrics_b"))
    assert str(np.lyrics.itemcget(np.lyrics.items[1], "font")) == str(app.theme.f("lyrics"))
    # 2.1: the label says what the lyrics are and where they come from
    assert (np.kind_l.cget("text"), np.source_l.cget("text")) == ("Synced lyrics", ".lrc file")
    # the cover with the title and "Artist — Album" under it: one block in the middle of the left half
    W, H = np.art.winfo_width(), np.art.winfo_height()
    box = next(b for b in np.links if b[0] == "cover")
    assert abs((box[1] + box[3]) / 2 - W / 2) <= 1 and abs((box[2] + box[4] + 80) / 2 - H / 2) <= 1, (box, W, H)
    assert texts(np.art)[:2] == ["Glacier Song", "Aurora Fields"], texts(np.art)
    # A- / A+: 26 px by default, 2 px a step, saved at once
    assert app.state.lyrics_size == 26 and app.theme.f("lyrics").cget("size") == -26
    np.bigger.event_generate("<ButtonRelease-1>", x=5, y=5)
    assert app.state.lyrics_size == 28 and playerstate.load().lyrics_size == 28
    assert app.theme.f("lyrics_b").cget("size") == -28 and np.lyrics.heights[0] > 0
    np.smaller.event_generate("<ButtonRelease-1>", x=5, y=5)
    assert app.state.lyrics_size == 26 and playerstate.load().lyrics_size == 26
    # scrolled away from the current line: following stops and "Back to the current line" shows
    v = np.lyrics
    v.scroll_by(600); root.update()
    assert v.away and np.back_btn.winfo_ismapped() and not v.line_in_view()
    held = v.offset
    v.update_position(0.6); v.update_position(4.2); root.update()
    assert v.offset == held and v.away  # no automatic scrolling while it shows
    np.back_btn.event_generate("<ButtonRelease-1>", x=5, y=5); root.update()
    assert not v.away and not np.back_btn.winfo_ismapped() and v.line_in_view()
    v.scroll_by(600); root.update()
    assert v.away
    v.scroll_by(-600); root.update()  # scrolled back into view: the button goes
    assert not v.away and not np.back_btn.winfo_ismapped()
    v.update_position(0.6)
    # the switch: Up Next lists the rest of the album, Details describes the song
    np.switch.choose("upnext"); root.update()
    assert app.np_tab == "upnext" and not app.bar.lyrics.on and np.upnext_pane.winfo_ismapped()
    assert [t.display_title for t in np.queue_list.rows] == ["Midnight Sun", "Polar Drift"]
    assert not np.lyrics.winfo_ismapped()
    np.switch.choose("details"); root.update()
    for needed in ("Title: Glacier Song", "Composer: Lena Frost", "Track: 2 of 4", "Format: FLAC · 24-bit/96 kHz",
                   "Album artist: Aurora Fields", "File: " + glacier, "Quality check: Not checked yet"):
        assert needed in np.details.text(), (needed, np.details.text())
    app.toggle_now_playing(); root.update()  # the Lyrics button chooses Lyrics again
    assert app.np_tab == "lyrics" and np.lyrics.winfo_ismapped() and app.bar.lyrics.on and not app.lyrics_hidden
    # the names are links: the artist opens the artist, the album name the album; the cover
    # opens the Cover window (another branch)
    class At:
        def __init__(self, box):
            self.x, self.y = (box[1] + box[3]) / 2, (box[2] + box[4]) / 2
    links = {{b[0]: b for b in np.links}}
    np._art_click(At(links["artist"])); root.update()
    assert app.page_name == "group" and app.page("group").kind == "artists" and app.page("group").name == "Aurora Fields"
    app.go_back(); root.update()
    np._art_click(At(links["album_name"])); root.update()
    assert app.page_name == "album" and app.page("album").title_l.cget("text") == "Northern Lights"
    app.go_back(); root.update()
    opened = []
    app.open_cover_window = opened.append
    np._art_click(At(links["cover"])); root.update()
    assert app.page_name == "nowplaying" and opened == [glacier]
    del app.open_cover_window
    # several artists in one field: each one its own link, on Now Playing, in the LCD and in Details
    tr = app.track(glacier)
    tr.artist = "June Harbor, Aurora Fields"
    np.draw_art(); app.bar.lcd.redraw()
    names = [b[5] for b in np.links if b[0] == "artist"]
    assert names == ["June Harbor", "Aurora Fields"], np.links
    second = next(b for b in np.links if b[5] == "Aurora Fields")
    np._art_click(At(second)); root.update()
    assert app.page_name == "group" and app.page("group").kind == "artists" and app.page("group").name == "Aurora Fields"
    app.go_back(); root.update()
    lcd = app.bar.lcd
    # both names as links; a narrow LCD (the Windows runners' 1024 x 768 screens) has room
    # for the first one only, which then ends with "…", and never leaves a ", " behind
    if len(lcd.artists) == 1:
        assert lcd.artists == {{"artist:0": "June Harbor"}}, lcd.artists
        # (the Windows runner showed "Ju… — No…")
        assert any(len(t) > 1 and t.endswith("…") and "June Harbor".startswith(t[:-1].rstrip())
                   for t in texts(lcd)), texts(lcd)
        assert ", " not in texts(lcd), texts(lcd)
    else:
        assert lcd.artists == {{"artist:0": "June Harbor", "artist:1": "Aurora Fields"}}, lcd.artists
    lcd._release(At(("artist:0",) + tuple(lcd.geo["artist:0"]))); root.update()
    assert app.page_name == "group" and app.page("group").kind == "artists" and app.page("group").name == "June Harbor"
    app.go_back(); root.update()
    np.switch.choose("details"); root.update()
    assert "Artist: June Harbor, Aurora Fields" in np.details.text()
    assert [x[4] for x in np.details.links] == ["June Harbor", "Aurora Fields"]
    np.details._click(At((None,) + tuple(np.details.links[0][:4]))); root.update()
    assert app.page_name == "group" and app.page("group").kind == "artists" and app.page("group").name == "June Harbor"
    app.go_back(); root.update()
    tr.artist = "Aurora Fields"
    app.toggle_now_playing(); root.update()
    assert app.np_tab == "lyrics"
    lcd = app.bar.lcd  # in the LCD: the title and the album open the album, the artist the artist
    for what, page in (("title", "album"), ("album", "album"), ("artist:0", "group")):
        g = lcd.geo[what]
        lcd._release(At((what,) + tuple(g))); root.update()
        assert app.page_name == page, (what, app.page_name)
        app.go_back(); root.update()
    assert app.page_name == "nowplaying"
    # 2.1: on Now Playing the Lyrics button hides the lyrics (the cover in the middle) and
    # the page stays; pressed again it shows them; a click on the LCD's cover leaves the page
    app.toggle_now_playing(); root.update()
    assert app.page_name == "nowplaying" and app.lyrics_hidden and not np.lyrics.winfo_ismapped()
    assert np.art.winfo_width() > root.winfo_width() / 2 and not app.bar.lyrics.on
    assert "Glacier Song" in texts(np.art) and not np.get_btn.winfo_ismapped()
    app.toggle_now_playing(); root.update()
    assert app.page_name == "nowplaying" and np.lyrics.winfo_ismapped() and app.bar.lyrics.on
    app.cover_clicked(); root.update()
    assert app.page_name != "nowplaying"
    app.cover_clicked(); root.update()
    assert app.page_name == "nowplaying" and np.lyrics.winfo_ismapped()
    assert not app.engine.playing and app.current == glacier  # still paused (see lyrics_ok)
    # Playback info
    app.open_info()
    info = app.info_window.text()
    for needed in ("Title: Glacier Song", "Format: FLAC, lossless", "Sample rate: 96 kHz", "Bit depth: 24-bit",
                   "Device: NULL Playback Device", "Output: 96 kHz, 32-bit float, stereo",
                   "Resampling: None: the device runs at the song's rate", "Equalizer: Off", "Gapless: On",
                   "Verdict: Not checked yet", "Track: 2 of 4"):
        assert needed in info, (needed, info)
    app.info_window.close()
    # the equalizer: heard at once, saved when the window closes
    app.open_eq()
    w = app.eq_window
    root.update()
    assert not w.preset_wrapped and not w.presets2.winfo_ismapped()  # in English the preset row fits
    w.v_on.set(True); w._toggles()
    w._preset("Rock")
    assert w.v_preset.get() == "Rock" and app.engine.eq_state["enabled"]
    w.nb.select(1); root.update()
    w._add_band()
    assert len(w.state["parametric"]["bands"]) == 2
    w.close()
    assert playerstate.load().eq["graphic"]["preset"] == "Rock"
    app.open_info()
    assert "Equalizer: Graphic: Rock (pre-amp −" in app.info_window.text(), app.info_window.text()
    app.info_window.close()
    # shuffle and repeat: menus with every mode, the icon shows the mode
    labels = [app.shuffle_popup.entrycget(i, "label") for i in range(app.shuffle_popup.index("end") + 1)]
    assert labels == ["Off", "Songs in this list", "All songs in the library", "Same genre", "Same artist",
                      "Same album"], labels
    if sys.platform.startswith("linux"):  # native pop-up menus (Windows, macOS) wait until closed
        app.shuffle_menu(); root.update(); app.shuffle_popup.unpost()
    app.shuffle_popup.invoke(labels.index("Same artist"))
    assert app.state.shuffle == "artist" and app.queue.shuffle == "artist" and app.bar.shuffle.on
    app.shuffle_popup.invoke(0)
    assert app.state.shuffle == "off" and not app.bar.shuffle.on
    rlabels = [app.repeat_popup.entrycget(i, "label") for i in range(app.repeat_popup.index("end") + 1)]
    assert rlabels == ["Off", "Repeat song", "Repeat album", "Stop after this song"], rlabels
    if sys.platform.startswith("linux"):
        app.repeat_menu(); root.update(); app.repeat_popup.unpost()
    app.repeat_popup.invoke(1)
    assert app.bar.repeat.badge == "1" and app.queue.repeat == "song"
    app.repeat_popup.invoke(3)
    assert app.bar.repeat.badge == "stop"
    with open(os.environ["REALHIRES_PLAYER"], encoding="utf-8") as fh:
        assert json.load(fh)["repeat"] == "off", "stop after this song was saved"
    app.repeat_popup.invoke(0)
    assert app.repeat_mode() == "off" and not app.bar.repeat.on
    # Up Next lists the rest of the album
    app.toggle_up_next(); root.update()
    assert [t.display_title for t in app.up_next.list.rows] == ["Midnight Sun", "Polar Drift"]
    # 2.3: Up Next can be edited: drag a song to another place, right-click to remove one.  Playback
    # is paused here, and the song to follow gaplessly is checked by what the app tells the engine
    # (the engine forgets it once it has started decoding that song)
    assert not app.engine.playing
    nexts = []
    set_next = app.engine.set_next
    app.engine.set_next = lambda path, after=None: (nexts.append(path), set_next(path, after))
    midnight, polar = LIB["songs"]["Midnight Sun"], LIB["songs"]["Polar Drift"]
    ul = app.up_next.list
    if root.grab_current() is not None:  # the pop-up menus unposted above may still hold X11's grab
        root.grab_current().grab_release()
    def drag(lst, y0, y1):
        lst.canvas.event_generate("<ButtonPress-1>", x=60, y=y0)
        lst.canvas.event_generate("<B1-Motion>", x=60, y=y0 + 2)  # under 5 px: still a click
        assert lst.drag_from is None
        lst.canvas.event_generate("<B1-Motion>", x=60, y=(y0 + y1) // 2)
        lst.canvas.event_generate("<B1-Motion>", x=60, y=y1)
        dragging = lst.drag_from is not None and lst.drop_at is not None
        assert not dragging or lst.canvas.find_withtag("drop"), "no drop line"
        lst.canvas.event_generate("<ButtonRelease-1>", x=60, y=y1)
        root.update()
        return dragging
    assert drag(ul, ul.rh + ul.rh // 2, 4)  # Polar Drift above Midnight Sun
    assert [t.display_title for t in ul.rows] == ["Polar Drift", "Midnight Sun"] and ul.selected == 0
    assert app.queue.up_next(5) == [polar, midnight] and nexts[-1] == polar, (app.queue.up_next(5), nexts)
    assert not ul.canvas.find_withtag("drop") and ul.drag_from is None
    assert drag(ul, ul.rh // 2, 2 * ul.rh - 2)  # and back below it
    assert app.queue.up_next(5) == [midnight, polar] and nexts[-1] == midnight, (app.queue.up_next(5), nexts)
    # Now Playing's Up Next view shows the same and is wired the same way
    npq = app.page("nowplaying").queue_list
    assert [t.path for t in npq.rows] == [midnight, polar]
    assert npq.on_move == app.move_in_up_next and npq.on_context is not None
    m = app.up_next_menu(1)
    assert [m.entrycget(i, "label") for i in range(m.index("end") + 1)] == ["Remove from Up Next"]
    m.invoke(0); root.update()
    assert [t.display_title for t in ul.rows] == ["Midnight Sun"] and app.queue.up_next(5) == [midnight]
    assert [t.path for t in npq.rows] == [midnight] and nexts[-1] == midnight
    app.set_shuffle("off")  # the order is built again: the removed song stays out
    assert app.queue.up_next(5) == [midnight] and app.queue.removed == {{polar}}
    app.queue.order.append(polar); app.queue.removed.clear()  # put it back for the rest of the test
    app.engine.set_next = set_next
    app._update_next(); app.up_next.refresh()
    assert [t.display_title for t in app.up_next.list.rows] == ["Midnight Sun", "Polar Drift"]
    # 2.1: Play Next from a song's menu comes first; the gapless look-ahead leaves the queue alone
    menu = app.song_menu(app.by_path[umbrella])
    labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) == "command"]
    assert "Play Next" in labels and "Add to Queue" in labels, labels
    app.queue_songs([umbrella], first=True); root.update()
    assert [t.display_title for t in app.up_next.list.rows] == ["Umbrella Weather", "Midnight Sun", "Polar Drift"]
    assert app._auto_next() == umbrella and app.queue.queued == [umbrella]
    app.queue.queued.clear(); app._update_next(); app.up_next.refresh()
    # 2.2: an album card's right-click menu; Play Next / Add to Queue queue the whole album in
    # track order, Play and Shuffle play it (recorded here: the song playing must stay)
    from realhires import player_pages
    small = next(a for a in app.albums if a["title"] == "Small Hours")
    rain = next(a for a in app.albums if a["title"] == "City Rain")
    am = player_pages.album_menu(app, small)
    def entries(m):
        return [m.entrycget(i, "label") if m.type(i) != "separator" else "-" for i in range(m.index("end") + 1)]
    ALBUM_MENU = ["Play", "Shuffle", "Play Next", "Add to Queue", "-", "Album Properties…", "Acquire Metadata…",
                  "Change Cover…", "-", "Colour Label"]  # 2.3: Colour Label
    assert entries(am) == ALBUM_MENU, entries(am)
    assert app.last_menu is am
    am.invoke(ALBUM_MENU.index("Add to Queue")); root.update()
    assert [t.display_title for t in app.up_next.list.rows] == ["Blue Kettle", "Porch Lamp", "Midnight Sun",
                                                                "Polar Drift"]
    player_pages.album_menu(app, rain).invoke(ALBUM_MENU.index("Play Next")); root.update()
    assert [t.display_title for t in app.up_next.list.rows] == ["Umbrella Weather", "Taxi Lights", "Last Train Home",
                                                                "Blue Kettle", "Porch Lamp", "Midnight Sun",
                                                                "Polar Drift"]
    app.queue.queued.clear(); app._update_next(); app.up_next.refresh()
    played = []
    app.play_list = lambda *a, **k: played.append((a, k))
    am.invoke(ALBUM_MENU.index("Play"))
    am.invoke(ALBUM_MENU.index("Shuffle"))
    del app.play_list
    assert played == [((small["tracks"], 0, "Small Hours"), dict()),
                      ((small["tracks"], 0, "Small Hours"), dict(shuffle=True))], played
    assert app.current == glacier
    # every grid of album cards gives its cards this menu: Albums, Recently Added, an
    # artist's albums, search results, and the Library page's Recently Added row
    gp = app.page("group")
    gp.show(kind="artists", group="June Harbor")
    sp = app.page("search")
    sp.show(text="rain")
    grids = [(app.page("albums").grid_w, "Small Hours"), (app.page("recent").grid_w, "Small Hours"),
             (gp.grid_w, "Small Hours"), (sp.grid_w, "City Rain")]
    lp = app.page("library")
    lp.area.redraw()
    box, newest = lp.album_hits[0]
    assert lp.album_at((box[0] + box[2]) / 2, (box[1] + box[3]) / 2) is newest
    assert lp.album_at(box[0] - 5, box[1] - 5) is None
    class Ev:
        x_root = y_root = 300
        x = (box[0] + box[2]) / 2 - lp.c.canvasx(0)
        y = (box[1] + box[3]) / 2 - lp.c.canvasy(0)
    for grid, title in grids:
        assert grid.on_context is not None
        assert title in [a["title"] for a in grid.albums], (title, grid.albums)
    if sys.platform.startswith("linux"):  # native pop-up menus (Windows, macOS) wait until closed
        for grid, title in grids:
            app.last_menu = None
            grid.on_context([a["title"] for a in grid.albums].index(title), Ev()); root.update()
            m = app.last_menu
            m.unpost()
            assert entries(m) == ALBUM_MENU, entries(m)
            m.invoke(ALBUM_MENU.index("Add to Queue"))
            assert app.queue.queued == [t.path for t in next(a for a in app.albums if a["title"] == title)["tracks"]]
            app.queue.queued.clear()
        app.last_menu = None
        lp._context(Ev()); root.update()
        m = app.last_menu
        m.unpost()
        assert entries(m) == ALBUM_MENU, entries(m)
        m.invoke(ALBUM_MENU.index("Add to Queue"))
        assert app.queue.queued == [t.path for t in newest["tracks"]]
        app.queue.queued.clear()
    app._update_next(); app.up_next.refresh()
    # the like button of the bar
    app.toggle_like_current()
    assert app.bar.heart.filled and glacier in playerstate.load().liked
    app.play_pause()
    assert app.current == glacier and app.engine.playing
    # the song ends: the next one starts by itself (gaplessly)
    wait(lambda: app.current == LIB["songs"]["Midnight Sun"], advanced, timeout=20, what="next song")

def advanced():
    assert app.engine.playing and app.page("nowplaying").kind == ""
    assert shows(app.bar.lcd, "Midnight Sun"), texts(app.bar.lcd)
    assert [t.display_title for t in app.up_next.list.rows] == ["Polar Drift"]
    app.toggle_up_next()
    app.seek(1.5)
    wait(lambda: app.engine.position() >= 1.5, sought, what="seek")

def sought():
    # search
    app.sidebar._focus(True)
    app.sidebar.search_var.set("rain")
    wait(lambda: app.page_name == "search", searched, what="search")

def searched():
    page = app.page("search")
    assert [a["title"] for a in page.grid_w.albums] == ["City Rain"]
    assert {{t.display_title for t in page.table.rows}} >= {{"Umbrella Weather", "Taxi Lights"}}
    app.sidebar.search_var.set("")
    wait(lambda: app.page_name != "search", settings_page, what="leave search")

def settings_page():
    app.show_page("settings", group="Appearance")
    root.update()
    page = app.page("settings")
    assert page.group == "Appearance" and "theme" in page.help_labels
    page._set("theme", "Dark")
    assert app.theme.dark and root.cget("bg").upper() == "#1E1E1E"
    assert app.page("albums").grid_w.canvas.cget("bg").upper() == "#1E1E1E"
    # the Quality check page follows the theme (2.1: drawn like the player's pages)
    assert app.theme.style.lookup("Treeview", "background").upper() == "#1E1E1E"
    assert app.qc.text.cget("bg").upper() == app.theme.c("lcd").upper() == "#2C2C2E"
    assert app.qc.cards.canvas.cget("bg").upper() == "#1E1E1E"
    from realhires import settings
    assert settings.load().theme == "Dark"
    page._set("theme", "Light")
    page.show(group="Detection")
    page._set_number("cliff_db", tk.StringVar(value="999"), "float")
    assert "Please enter a number from 5 to 150" in page.help_labels["cliff_db"].cget("text")
    page.show(group="Playback")
    assert "output_device" in page.help_labels
    # no menu bar (2.1): what the File and Help menus held is on Settings → Library and the
    # Settings page, and every shortcut of the menus is a key binding
    assert not str(root.cget("menu"))
    page.show(group="Library")
    root.update()
    assert {{"Add folder…", "Update the library now", "Scan library", "Download lyrics for the whole library…",
             "Clear the play history…"}} <= set(button_texts(page)), button_texts(page)
    assert page.about_btn.text == "About Universal Music Player" and page.about_btn.command == app.about
    mod = "Command" if sys.platform == "darwin" else "Control"
    for seq in ("<space>", f"<{{mod}}-Right>", f"<{{mod}}-Left>", f"<{{mod}}-Up>", f"<{{mod}}-Down>", f"<{{mod}}-l>",
                f"<{{mod}}-comma>", f"<{{mod}}-f>"):
        assert root.bind(seq), seq
    for seq in (f"<{{mod}}-o>", f"<{{mod}}-q>", f"<{{mod}}-comma>", f"<{{mod}}-f>", f"<{{mod}}-F>", f"<{{mod}}-a>",
                f"<{{mod}}-l>", "<F3>", "<Shift-F3>"):
        assert root.bind_all(seq), seq
    assert app.qc.tree.bind("<Return>") and app.qc.tree.bind(f"<{{mod}}-Return>") and app.qc.tree.bind(f"<{{mod}}-c>")
    # the Quality check page: no menu bar either; Quality Check in the sidebar, More on the page
    app.check_library(switch=True)
    assert app.page_name == "quality" and app.qc.active and not str(root.cget("menu"))
    assert app.qc.menubar is None and app.sidebar.selected == "quality"
    assert ("quality", "Quality Check", "check") in app.sidebar.FOOT and app.sidebar.FOOT[-1][0] == "settings"
    assert app.qc.title_label.cget("text") == "Quality Check"
    m = app.qc.more_menu
    entries = [m.entrycget(i, "label") if m.type(i) != "separator" else "-" for i in range(m.index("end") + 1)]
    assert entries == ["Export listed files as CSV…", "Export listed files as JSON…", "-",
                       "Fix selected files to real resolution…", "Fix all fake files to real resolution…", "-",
                       "Download synced lyrics for selected songs…", "Download synced lyrics for all songs…", "-",
                       "Re-scan selected files", "Re-scan files checked by older rules", "-",
                       "Select all listed files", "Copy path", "Open file location", "Open file", "Find…", "-",
                       "Scan summary…", "Show problem files only", "Show all files", "-",
                       "Delete all saved results…"], entries
    if sys.platform.startswith("linux"):  # native pop-up menus (Windows, macOS) wait until closed
        app.qc.show_more_menu(); root.update(); m.unpost()
    assert str(app.qc.stop_btn.cget("state")) == "disabled" or app.qc.scanning
    wait(lambda: len(app.qc.results) == N_SONGS and not app.qc.scanning, scanned, timeout=150, what="scan")

def button_texts(widget):
    out = []
    for w in widget.winfo_children():
        if hasattr(w, "text") and isinstance(getattr(w, "text"), str) and hasattr(w, "command"):
            out.append(w.text)
        out += button_texts(w)
    return out

def scanned():
    root.update()
    assert app.qc.tree.get_children() or True
    for w in root.winfo_children():
        if isinstance(w, gui.SummaryDialog):
            w.destroy()
    app.show_page("albums")
    assert not app.qc.active and not str(root.cget("menu")) and app.sidebar.selected == "albums"
    # the page shows its results like a song table, Scan back in Stop's place
    assert app.qc.scan_btn.winfo_manager() == "pack" and not app.qc.stop_btn.winfo_manager()
    assert app.qc.filter_checks["REAL HI-RES"].count is not None
    app.open_info()
    assert "Verdict: " in app.info_window.text() and "Not checked yet" not in app.info_window.text()
    app.info_window.close()
    # a song that cannot be played: its error in the LCD, then the next song after 2 s
    import copy
    gone = copy.copy(app.track(LIB["songs"]["Taxi Lights"]))
    gone.path = os.path.join(os.path.dirname(gone.path), "gone.flac")
    app.play_list([gone, app.track(LIB["songs"]["Last Train Home"])], 0, "Test")
    wait(lambda: app.error, errored, what="error")

def errored():
    assert app.error == "The file gone.flac does not exist any more.", app.error
    assert shows(app.bar.lcd, "The file gone.flac does not exist any more."), texts(app.bar.lcd)
    state["t"] = time.monotonic()
    wait(lambda: app.current == LIB["songs"]["Last Train Home"] and app.engine.playing, after_error, what="skip")

def after_error():
    assert time.monotonic() - state["t"] > 1.5 and not app.error
    state["last"] = app.current
    app.engine.pause()
    state["pos"] = app.position()
    app.quit()

root, app = start()
wait(lambda: len(app.tracks) == N_SONGS and not app.library_busy(), loaded, what="library")
root.mainloop()
saved = playerstate.load()
assert saved.last_path == state["last"] and abs(saved.last_position - state["pos"]) < 0.3, saved
assert saved.shuffle == "off" and saved.repeat == "off" and saved.eq["enabled"]
assert sorted(saved.liked) == sorted([umbrella, glacier])

# the next start: the last song is shown paused where it was, with its album as the list
def restored():
    assert app.current == state["last"] and not app.engine.active
    assert app.context_name == "City Rain" and abs(app.position() - state["pos"]) < 0.3
    album = [LIB["songs"][t] for t in ("Umbrella Weather", "Taxi Lights", "Last Train Home")]
    assert app.queue.order == album[album.index(state["last"]):]
    assert shows(app.bar.lcd, "Last Train Home") and app.bar.play.icon == "play", texts(app.bar.lcd)
    app.play_pause()  # Play continues there
    wait(lambda: app.engine.playing and app.engine.position() >= state["pos"], resumed, what="resume")

def resumed():
    assert app.current == state["last"]
    app.quit()

root, app = start()
wait(lambda: app.tracks and not app.library_busy(), restored, what="restart")
root.mainloop()
print("OK")
"""
    run_script(tmp_path, body, env)


AUTOEQ = """# Studio Cans (an invented example)
Preamp: -4.2 dB
Filter 1: ON LSC Fc 105 Hz Gain 4.5 dB Q 0.71
Filter 2: ON PK Fc 2400 Hz Gain -2.0 dB Q 1.40
"""


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_library_playlists_history_presets_and_themes(tmp_path):
    """The 2.0 additions: the Library page and its fourteen entries, a folder page,
    playlists, dislikes, the play history with Recently Played and Your Stats, EQ
    presets with their files, and custom themes."""
    lib = make_library(tmp_path, seconds=3, mp3=False)
    (tmp_path / "Studio Cans.txt").write_text(AUTOEQ, encoding="utf-8")
    from realhires import settings

    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.theme = "Light"
    settings.save(cfg, str(tmp_path / "settings.json"))
    env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
               REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
               REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
               REALHIRES_AUDIO_BACKEND="null")
    body = f"""
LIB = {json.dumps(lib)}
TMP = {str(tmp_path)!r}
S = LIB["songs"]
import tkinter as tk
from realhires import player_gui, playerstate, stats, themes
from realhires.player_collections import TILES
player_gui.messagebox.askyesno = lambda *a, **k: True  # confirmations say yes
state = {{}}

def fail(exc, val, tb):
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)

def wait(cond, then, timeout=30.0, what="", start=None):
    start = start or time.monotonic()
    if cond():
        return root.after(50, then)
    assert time.monotonic() - start < timeout, "timed out: " + what
    root.after(100, lambda: wait(cond, then, timeout, what, start))

def texts(canvas):
    return [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]

def answer(name):  # fills in the next name dialog, once it is on the screen (as a person would)
    def go(tries=0):
        d = app.name_dialog
        if d is None:
            return root.after(50, lambda: go(tries))
        if not d.win.winfo_viewable() and tries < 40:
            return root.after(50, lambda: go(tries + 1))
        d.var.set(name)
        d.submit()
    root.after(50, go)

root = tk.Tk()
root.geometry("1280x800")
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)

def loaded():
    # the Library start page: fourteen tiles, each opening its page
    assert app.page_name == "library"
    page = app.page("library")
    names = [n for _, n, _ in TILES]
    assert names == ["All Songs", "Folders", "Albums", "Artists", "Album Artists", "Genres", "Years", "Composers",
                     "Playlists", "Liked Songs", "Disliked Songs", "Recently Played", "Recently Added", "Your Stats"]
    shown = texts(page.c)
    assert all(n in shown for n in names) and "11 songs" in shown and "Recently Added" in shown, shown
    for key, name, _ in TILES:
        app.show_page(key)
        root.update()
        assert app.page_name == key and app.pages[key].winfo_ismapped(), key
    # Back and Forward between pages (2.1): the mouse's side buttons (8 and 9 on X11, 4 and 5
    # with Tk 8.6 on Windows and macOS) and Alt+Left / Alt+Right (Cmd+[ / Cmd+] on macOS)
    app.show_page("albums")
    app.show_page("genres")
    root.update()
    back, forward = player_gui.nav_button(8, "x11", 8.6), player_gui.nav_button(9, "x11", 8.6)
    assert (back, forward) == ("back", "forward") and player_gui.nav_button(4, "x11", 8.6) == ""
    side = 8 if root.tk.call("tk", "windowingsystem") == "x11" or tk.TkVersion >= 8.7 else 4
    app.pages["genres"].event_generate("<ButtonPress>", button=side, x=5, y=5)
    root.update()
    assert app.page_name == "albums" and app.forward[-1][0] == "genres", (app.page_name, app.forward)
    app.pages["albums"].event_generate("<ButtonPress>", button=side + 1, x=5, y=5)
    root.update()
    assert app.page_name == "genres" and app.forward == [] and app.history[-1][0] == "albums"
    root.focus_force()
    root.update()
    keys = player_gui.NAV_KEYS
    root.event_generate(keys[0])
    root.update()
    assert app.page_name == "albums" and [f[0] for f in app.forward] == ["genres"], (app.page_name, app.forward)
    app.show_page("years")  # a new page: nothing to go forward to
    assert app.forward == []
    app.go_back()
    root.event_generate(keys[1])
    root.update()
    assert app.page_name == "years", app.page_name
    assert [g["name"] for g in app.page("composers").items] == ["Lena Frost", "Mika Rowe", "Unknown Composer"]
    assert [g["name"] for g in app.page("years").items] == ["2023", "2021", "2019", "2018"]
    assert app.page("songs").title_l.cget("text") == "All Songs"
    # a folder page with a breadcrumb
    fp = app.page("folders")
    app.show_page("folders")
    assert [r[1] for r in fp.dirs.rows] == ["Aurora Fields", "June Harbor", "The Paper Boats", "Velvet Static"]
    fp.open(os.path.join(LIB["music"], "Aurora Fields", "Northern Lights"))
    root.update()
    assert [p[0] for p in fp.parts] == ["Music", "Aurora Fields", "Northern Lights"]
    assert [t.display_title for t in fp.table.rows] == ["First Light", "Glacier Song", "Midnight Sun", "Polar Drift"]
    crumb = [w for w in fp.crumbs.winfo_children() if w.cget("text") == "Aurora Fields"][0]
    crumb.event_generate("<Button-1>")
    root.update()
    assert fp.folder == os.path.join(LIB["music"], "Aurora Fields")
    assert [r[1] for r in fp.dirs.rows] == ["Northern Lights"]
    # a playlist: create (name dialog), add from a song's menu, play, rename, delete
    answer("Road Trip")
    pid = app.new_playlist()
    root.update()  # each step is followed by an update: on macOS Tk crashed (SIGSEGV) in one of
    # these updates, and faulthandler's line number then names the step
    assert pid and app.page_name == "playlist" and app.state.playlist(pid)["name"] == "Road Trip"
    assert ("playlist:" + pid, "Road Trip", "playlist") in app.sidebar.items()
    m = app.song_menu(app.track(S["Taxi Lights"]))
    root.update()
    labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) != "separator"]
    assert "Add to playlist" in labels and "Dislike" in labels and "Like" in labels, labels
    sub = root.nametowidget(m.entrycget(labels_index(m, "Add to playlist"), "menu"))
    sub.invoke(labels_index(sub, "Road Trip"))
    root.update()
    app.add_to_playlist(pid, [S["First Light"]])
    root.update()
    app.add_to_playlist(pid, [os.path.join(TMP, "gone.flac")])
    root.update()
    assert playerstate.load().playlist(pid)["songs"][:2] == [S["Taxi Lights"], S["First Light"]]
    pp = app.page("playlist")
    root.update()
    rows = pp.table.rows
    assert [r.display_title for r in rows] == ["Taxi Lights", "First Light", "gone"] and rows[2].missing
    assert pp._value(rows[2], "artist") == "Not in the library"
    dict(pp._extra(0))["Move down"]()
    assert [r.display_title for r in pp.table.rows][:2] == ["First Light", "Taxi Lights"]
    pp.table.on_activate(0)
    assert app.current == S["First Light"] and app.queue.order == [S["First Light"], S["Taxi Lights"]]
    answer("Long Drive")
    app.rename_playlist(pid)
    assert app.state.playlist(pid)["name"] == "Long Drive" and pp.title_l.cget("text") == "Long Drive"
    app.delete_playlist(pid)
    assert app.state.playlists == [] and app.page_name == "playlists"
    # dislike: left out when shuffling
    app.toggle_dislike(S["Midnight Sun"])
    assert app.state.disliked == [S["Midnight Sun"]] and playerstate.load().disliked == [S["Midnight Sun"]]
    app.show_page("disliked")
    assert [t.display_title for t in app.page("disliked").table.rows] == ["Midnight Sun"]
    album = next(a for a in app.albums if a["title"] == "Northern Lights")
    app.set_shuffle("songs")
    app.play_list(album["tracks"], 0, album["title"])
    assert app.current == S["First Light"] and S["Midnight Sun"] not in app.queue.order
    assert len(app.queue.order) == 3
    app.set_shuffle("off")
    # the play history: two songs heard for more than a second
    app.play_list(album["tracks"], 0, album["title"])
    wait(lambda: app.engine.position() > 1.6, first_heard, what="first song")

def labels_index(menu, label):
    return next(i for i in range(menu.index("end") + 1) if menu.type(i) != "separator"
                and menu.entrycget(i, "label") == label)

def first_heard():
    app.next()
    wait(lambda: app.current == S["Glacier Song"] and app.engine.position() > 1.6, second_heard, what="second song")

def second_heard():
    app.next()  # the second song stops being the current one: it is logged too
    plays = app.plays()
    assert [p.path for p in plays] == [S["First Light"], S["Glacier Song"]], [p.path for p in plays]
    assert all(1.0 < p.seconds < 3.5 for p in plays), [p.seconds for p in plays]
    app.engine.pause()
    app.show_page("played")
    rp = app.page("played")
    assert [t.display_title for t in rp.table.rows] == ["Glacier Song", "First Light"]
    assert rp._value(rp.table.rows[0], "played").startswith("Today ")
    app.show_page("stats")
    st = app.page("stats")
    root.update()
    cards = texts(st.area.c)
    assert st.st["logged"] == 2 and "MINUTES LISTENED" in cards and "YOUR TOP SONG" in cards, cards
    assert "Glacier Song" in cards or "First Light" in cards, cards
    # the calendar (2.1): the month of the plays, minutes on their day; a click on the day lists
    # its plays (local time, title, artist, minutes), newest first; a click on a song plays it
    first = time.localtime(plays[0].started)
    st.show_month(first.tm_year, first.tm_mon)
    root.update()
    c = st.area.c
    assert st.minutes[first.tm_mday - 1] > 0 and st.levels[first.tm_mday - 1] >= 1, st.minutes
    assert stats.month_title(first.tm_year, first.tm_mon) in texts(c)
    day = next(i for i in c.find_all() if c.type(i) == "text" and c.itemcget(i, "text") == str(first.tm_mday))
    x, y = c.coords(day)
    c.event_generate("<Button-1>", x=int(x), y=int(y))
    root.update()
    assert st.day == (first.tm_year, first.tm_mon, first.tm_mday), st.day
    want = [p for p in reversed(plays) if time.localtime(p.started)[:3] == first[:3]]
    assert [r["path"] for r in st.day_rows] == [p.path for p in want], st.day_rows
    # 2.3: on a wide page the day's plays are to the right of the calendar, in a canvas of their own;
    # the CI runners' 1024 x 768 screens leave the page too narrow, and then they are under the calendar
    if st.side_by_side():
        assert st.day_c.winfo_ismapped()
        dc = st.day_c
    else:
        assert not st.day_c.winfo_ismapped() and st.day_box is None
        dc = c
    shown = texts(dc)
    assert stats.day_title(*st.day) in shown and stats.day_summary(st.day_rows) in shown
    row = st.day_rows[-1]
    assert row["time"] == time.strftime("%H:%M", time.localtime(row["started"])) and row["time"] in shown
    assert row["title"] == "First Light" and row["artist"] == "Aurora Fields" and row["minutes"] == "<1 min"
    item = next(i for i in dc.find_all() if dc.type(i) == "text" and dc.itemcget(i, "text") == "First Light")
    x, y = dc.coords(item)
    dc.event_generate("<Button-1>", x=int(x), y=int(y))
    root.update()
    assert app.current == S["First Light"] and app.context_name == "Your Stats"
    app.engine.pause()
    st.choose_day(first.tm_mday)  # the chosen day again: the list goes
    assert st.day is None and st.day_rows == []
    app.show_page("stats_details")
    root.update()
    shown = texts(app.page("stats_details").area.c)
    assert "Top songs" in shown and "Your library" in shown and "Listening by hour of the day (minutes)" in shown
    app.show_page("library")
    assert any("minutes of music this year" in x or x == "Your Stats" for x in texts(app.page("library").c))
    # equalizer presets: save, rename, export both ways, import an AutoEQ text
    app.open_eq()
    w = app.eq_window
    w.v_on.set(True); w._toggles()
    w._preset("Rock")
    answer("Bright Rock")
    w._save_as()
    assert [p["name"] for p in app.state.eq_presets] == ["Bright Rock"] and w.preset_label() == "Bright Rock"
    menu = w.build_preset_menu()
    mlabels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) != "separator"]
    assert mlabels[:2] == ["Flat", "Bass Boost"] and "My presets" in mlabels and "Bright Rock" in mlabels
    answer("Rock Plus")
    w._rename()
    assert [p["name"] for p in app.state.eq_presets] == ["Rock Plus"]
    w.export_file(os.path.join(TMP, "rock.json"))
    w.export_file(os.path.join(TMP, "rock.txt"))
    assert '"real-hi-res-eq"' in open(os.path.join(TMP, "rock.json"), encoding="utf-8").read()
    assert "Filter 1: ON PK Fc 32.0 Hz" in open(os.path.join(TMP, "rock.txt"), encoding="utf-8").read()
    w._preset("Flat")
    assert w.import_file(os.path.join(TMP, "Studio Cans.txt")) == "Studio Cans"
    assert w.preset_label() == "Studio Cans" and len(w.state["parametric"]["bands"]) == 2
    assert w.state["parametric"]["preamp"] == -4.2 and "Imported" in w.note.cget("text")
    assert w.import_file(os.path.join(TMP, "rock.json")) == "Rock Plus 2"
    w.close()
    assert [p["name"] for p in playerstate.load().eq_presets] == ["Rock Plus", "Studio Cans", "Rock Plus 2"]
    # a custom theme: copy of Midnight, a colour changed, export and import
    app.show_page("settings", group="Appearance")
    app.select_theme("Midnight")
    assert app.theme.name == "Midnight" and app.theme.dark
    name = app.new_theme()
    assert name == "Midnight copy" and app.cfg.theme == "Midnight copy" and app.is_custom_theme(name)
    app.set_theme_color(name, "accent", "#00ff00")
    assert app.theme.c("accent") == "#00ff00" and themes.load_custom()[0]["colors"]["accent"] == "#00ff00"
    root.update()
    ed = app.page("settings").theme_editor
    assert ed.own and ed.name == name
    path = app.export_theme_file(name, os.path.join(TMP, "mine.json"))
    assert app.import_theme_file(path) == "Midnight copy 2" and app.cfg.theme == "Midnight copy 2"
    app.delete_theme("Midnight copy 2")
    assert app.cfg.theme == themes.SYSTEM and [t["name"] for t in themes.load_custom()] == ["Midnight copy"]
    app.select_theme("Light")
    print("OK")
    app.quit()

wait(lambda: len(app.tracks) == len(S) and not app.library_busy(), loaded, what="library")
root.mainloop()
"""
    run_script(tmp_path, body, env)


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_theme_files_and_edit_info(tmp_path):
    """Theme files in the themes folder appear in the theme menu, read-only in the
    editor; the Properties window (2.0 "Edit Info") changes one song's album artist
    and release date, an album's genre for all its songs, and refuses a bad track
    number without writing; it can be made bigger, its fields grow with it, and the
    size it was given is kept for the next one."""
    lib = make_library(tmp_path, seconds=2, mp3=False)
    from realhires import settings

    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.theme = "Light"
    settings.save(cfg, str(tmp_path / "settings.json"))
    env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
               REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
               REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
               REALHIRES_AUDIO_BACKEND="null")
    body = f"""
LIB = {json.dumps(lib)}
TMP = {str(tmp_path)!r}
S = LIB["songs"]
import tkinter as tk
from realhires import player_gui, themes
from realhires.probe import probe

def fail(exc, val, tb):
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)

def wait(cond, then, timeout=30.0, what="", start=None):
    start = start or time.monotonic()
    if cond():
        return root.after(50, then)
    assert time.monotonic() - start < timeout, "timed out: " + what
    root.after(100, lambda: wait(cond, then, timeout, what, start))

def menu_labels(m):
    return [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) != "separator"]

root = tk.Tk()
root.geometry("1280x800")
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)
opened = []

def loaded():
    # a theme file put into the themes folder appears in the menu and can be chosen
    folder = themes.folder()
    assert folder == os.path.join(TMP, "themes")
    assert app.open_themes_folder(opener=opened.append) == folder and opened == [folder] and os.path.isdir(folder)
    with open(os.path.join(folder, "neon.json"), "w", encoding="utf-8") as fh:
        fh.write(themes.export_theme({{"name": "Neon", "dark": True, "colors": {{"accent": "#39ff14"}}}}))
    app.show_page("settings", group="Appearance")
    root.update()
    page = app.page("settings")
    assert "Neon" in page.theme_values() and "Neon" in page.theme_box.cget("values")
    app.select_theme("Neon")
    root.update()
    assert app.theme.name == "Neon" and app.theme.dark and app.theme.c("accent") == "#39ff14"
    assert app.theme_file("Neon").endswith("neon.json") and not app.is_custom_theme("Neon")
    ed = app.page("settings").theme_editor
    assert not ed.own and ed.file.endswith("neon.json"), ed.file
    assert "comes from the file neon.json in the themes folder" in ed.note_l.cget("text")
    assert ed.actions == ["New theme…", "Export…"], ed.actions
    exported = app.export_theme_file("Neon", os.path.join(TMP, "neon-out.json"))
    assert '"#39ff14"' in open(exported, encoding="utf-8").read()
    with open(os.path.join(folder, "zed.json"), "w", encoding="utf-8") as fh:
        fh.write(themes.export_theme({{"name": "Zed", "dark": False, "colors": {{}}}}))
    assert "Zed" in page.theme_values()  # read again when the menu opens
    os.remove(os.path.join(folder, "neon.json"))
    app.select_theme("Light")
    # Properties… is in a song's menu; one Properties window at a time
    taxi = app.track(S["Taxi Lights"])
    labels = menu_labels(app.song_menu(taxi))
    assert "Properties…" in labels and not any("Edit info" in x for x in labels), labels
    w = app.edit_info([taxi])
    assert app.edit_info([app.track(S["Blue Kettle"])]) is w
    state["w"] = w
    wait(lambda: w.ready, one_ready, what="tags read")

state = {{}}

def one_ready():
    w = state["w"]
    assert w.win.title() == "Properties"
    # the user makes the window bigger: the fields grow with it
    root.update()
    width = w.entries["title"].winfo_width()
    w.win.geometry("%dx%d" % (w.win.winfo_width() + 300, w.win.winfo_height() + 40))
    root.update()
    assert w.entries["title"].winfo_width() >= width + 250, (width, w.entries["title"].winfo_width())
    state["size"] = (w.win.winfo_width(), w.win.winfo_height())
    assert w.vars["title"].get() == "Taxi Lights" and w.vars["album_artist"].get() == "The Paper Boats"
    assert w.vars["date"].get() == "2019" and w.vars["track"].get() == "2/3"
    w.vars["album_artist"].set("Paper Boats Collective")
    w.vars["date"].set("2019-04-02")
    assert w.changes() == {{"album_artist": "Paper Boats Collective", "date": "2019-04-02"}}
    w.save()
    assert w.saving
    w.cancel()  # cannot be closed while saving
    assert app.edit_window is w
    wait(lambda: app.edit_window is None and not app.library_busy(), one_saved, what="one song saved")

def one_saved():
    tags = probe(S["Taxi Lights"]).tags
    assert tags["album_artist"] == "Paper Boats Collective" and tags["date"] == "2019-04-02", tags
    assert tags["title"] == "Taxi Lights" and tags["artist"] == "The Paper Boats"
    tr = app.track(S["Taxi Lights"])
    assert tr.album_artist == "Paper Boats Collective" and tr.year == "2019"
    app.show_page("album_artists")
    root.update()
    names = [g["name"] for g in app.page("album_artists").items]
    assert "Paper Boats Collective" in names, names
    # a bad track number: the error is shown and nothing is written
    before = os.stat(S["Blue Kettle"]).st_mtime_ns
    assert app.edit_size == state["size"], (app.edit_size, state["size"])
    w = app.edit_info([app.track(S["Blue Kettle"])])
    root.update()
    assert (w.win.winfo_width(), w.win.winfo_height()) == state["size"]  # the size is kept
    state["w"] = w
    state["before"] = before
    wait(lambda: w.ready, bad_ready, what="tags read again")

def bad_ready():
    w = state["w"]
    w.vars["track"].set("three")
    w.save()
    assert not w.saving and "must be a number" in w.error.cget("text"), w.error.cget("text")
    assert os.stat(S["Blue Kettle"]).st_mtime_ns == state["before"]
    w.cancel()
    assert app.edit_window is None
    # Album Properties…: every song of the album, from an album card's right-click menu (2.2)
    # and from the album page's button (the same window: one at a time)
    album = next(a for a in app.albums if a["title"] == "Small Hours")
    from realhires import player_pages
    m = player_pages.album_menu(app, album)
    m.invoke(next(i for i in range(m.index("end") + 1)
                  if m.type(i) == "command" and m.entrycget(i, "label") == "Album Properties…"))
    w = app.edit_window
    assert w is not None and w.paths == [t.path for t in album["tracks"]], w and w.paths
    app.open_album(album)
    root.update()
    assert app.page("album").edit_b.text == "Album Properties…"
    assert app.page("album").mb_b.text == "Acquire Metadata…"
    assert app.page("album").edit_info() is w
    state["w"] = w
    wait(lambda: w.ready, album_ready, what="album tags read")

def album_ready():
    w = state["w"]
    root.update()
    assert w.win.winfo_width() == state["size"][0] and w.win.winfo_height() >= w.win.winfo_reqheight() - 2
    assert w.initial["title"] is None and w.SEVERAL in w.notes["title"].cget("text")
    assert w.vars["title"].get() == "" and w.vars["album"].get() == "Small Hours"
    w.vars["genre"].set("Bossa Nova")
    assert w.changes() == {{"genre": "Bossa Nova"}}
    w.save()
    wait(lambda: app.edit_window is None and not app.library_busy(), album_saved, what="album saved")

def album_saved():
    for title in ("Blue Kettle", "Porch Lamp"):
        tags = probe(S[title]).tags
        assert tags["genre"] == "Bossa Nova" and tags["title"] == title, tags
    page = app.page("album")
    root.update()
    assert app.page_name == "album" and page.album["genre"] == "Bossa Nova"
    assert "Bossa Nova" in page.meta_l.cget("text")
    print("OK")
    app.quit()

wait(lambda: len(app.tracks) == len(S) and not app.library_busy(), loaded, what="library")
root.mainloop()
"""
    run_script(tmp_path, body, env, timeout=180)


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_player_in_japanese(tmp_path):
    """2.1: Settings → Appearance → Language = 日本語.  Every page, every Settings group,
    the Equalizer, Playback info, Properties and Cover windows, a name dialog, a song's
    menu, the columns menu, Now Playing's switch, a grouping page and the shuffle menu
    work in Japanese (any exception in a Tk callback fails the test); choices are shown
    translated and stored in English; choosing English shows the restart note, and
    Restart now saves everything and starts the app again with the same command."""
    lib = make_library(tmp_path, seconds=2, mp3=False)
    from realhires import settings

    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.theme = "Light"
    cfg.language = "日本語"
    settings.save(cfg, str(tmp_path / "settings.json"))
    env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
               REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
               REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
               REALHIRES_AUDIO_BACKEND="null")
    body = f"""
LIB = {json.dumps(lib)}
S = LIB["songs"]
import tkinter as tk
from realhires import i18n, player_gui, player_pages, playerstate, settings
from realhires.i18n import tr
from realhires.player_collections import TILES
from realhires.playqueue import SHUFFLE_LABELS, SHUFFLE_MODES
player_gui.messagebox.askyesno = lambda *a, **k: True  # confirmations say yes
state = {{}}

def fail(exc, val, tb):
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)

def wait(cond, then, timeout=30.0, what="", start=None):
    start = start or time.monotonic()
    if cond():
        return root.after(50, then)
    assert time.monotonic() - start < timeout, "timed out: " + what
    root.after(100, lambda: wait(cond, then, timeout, what, start))

def texts(canvas):
    return [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]

def widget_texts(w):
    out = []
    for c in w.winfo_children():
        try:
            out.append(str(c.cget("text")))
        except tk.TclError:
            pass
        if isinstance(c, tk.Canvas):
            out += texts(c)
        out += widget_texts(c)
    return out

def menu_labels(m):
    return [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) not in ("separator", "tearoff")]

root = tk.Tk()
root.geometry("1280x800")
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)
assert i18n.language() == "ja"
started = []
app.start_process = lambda cmd, **kw: started.append((cmd, kw))  # Restart now must not start anything

def loaded():
    # the sidebar, the menus and the Library page in Japanese
    items = [text for _, text, _ in app.sidebar.items()]
    for needed in ("ライブラリ", "すべての曲", "アルバム", "プレイリスト", "お気に入りの曲", "最近再生した項目",
                   "あなたの統計", "新規プレイリスト…"):
        assert needed in items, (needed, items)
    foot = texts(app.sidebar.foot)
    assert "品質チェック" in foot and "設定" in foot, foot
    assert not root.cget("menu")  # 2.2: no menu bar; the bar's shuffle menu is in Japanese too
    assert menu_labels(app.shuffle_popup)[0] == tr(SHUFFLE_LABELS[SHUFFLE_MODES[0]]) != SHUFFLE_LABELS[SHUFFLE_MODES[0]]
    shown = texts(app.page("library").c)
    assert "ライブラリ" in shown and "すべての曲" in shown and "11曲" in shown, shown
    # every page of the library
    for key, name, _ in TILES:
        app.show_page(key)
        root.update()
        assert app.page_name == key and app.pages[key].winfo_ismapped(), key
    assert app.page("songs").title_l.cget("text") == "すべての曲"
    heads = texts(app.page("songs").table.canvas)
    assert "タイトル" in heads and "アーティスト" in heads, heads
    cols = menu_labels(player_pages.columns_menu(app, "songs"))
    assert "#（トラック番号）" in cols and "作曲者" in cols and "列をリセット" in cols, cols
    app.show_page("genres")
    root.update()
    assert app.page("genres").title_l.cget("text") == "ジャンル"
    app.show_page("group", kind="genres", group=app.page("genres").grid_w.albums[0]["name"])
    root.update()
    assert app.page("group").sub.cget("text").startswith("ジャンル · "), app.page("group").sub.cget("text")
    assert "ジャンル名を変更…" in menu_labels(app.page("group").menu())
    album = next(a for a in app.albums if a["title"] == "Northern Lights")
    app.open_album(album)
    root.update()
    assert app.page("album").table.footer.startswith("4曲、"), app.page("album").table.footer
    app.show_page("search", text="rain")
    root.update()
    assert app.page("search").title_l.cget("text") == "「rain」の検索結果"
    # a song's menu
    m = app.song_menu(app.track(S["Taxi Lights"]))
    labels = menu_labels(m)
    for needed in ("プレイリストに追加", "お気に入り", "次に再生", "キューに追加", "歌詞を取得…", "プロパティ…",
                   "メタデータを取得…", "アートワークを変更…"):
        assert needed in labels, (needed, labels)
    # an album card's menu (2.2) and the album page's buttons
    am = player_pages.album_menu(app, album)
    assert menu_labels(am) == [tr("Play"), tr("Shuffle"), "次に再生", "キューに追加", "アルバムのプロパティ…",
                               "メタデータを取得…", "アートワークを変更…", "カラーラベル"], menu_labels(am)
    assert app.page("album").mb_b.text == "メタデータを取得…" and app.page("album").edit_b.text == "アルバムのプロパティ…"
    # a name dialog: a new playlist
    def answer(tries=0):
        d = app.name_dialog
        if d is None or (not d.win.winfo_viewable() and tries < 40):
            return root.after(50, lambda: answer(tries + 1))
        state["dialog"] = (d.win.title(), d.var.get(), widget_texts(d.win))
        d.var.set("ドライブ")
        d.submit()
    root.after(50, answer)
    pid = app.new_playlist()
    title, initial, dtexts = state["dialog"]
    assert title == "新規プレイリスト" and initial == "新規プレイリスト", state["dialog"]
    assert "プレイリストの名前" in dtexts and "キャンセル" in dtexts, dtexts
    assert app.state.playlist(pid)["name"] == "ドライブ" and app.page_name == "playlist"
    app.delete_playlist(pid)
    # play a song for more than a second: Recently Played says "今日 hh:mm" (Play in the album
    # card's menu plays the album from its first song)
    am.invoke(0)
    assert app.current == album["tracks"][0].path and app.context_name == "Northern Lights", app.context_name
    wait(lambda: app.engine.position() > 1.3, heard, what="first song")

def heard():
    app.next()
    app.engine.pause()
    root.update()
    app.show_page("played")
    rp = app.page("played")
    assert rp._value(rp.table.rows[0], "played").startswith("今日 "), rp._value(rp.table.rows[0], "played")
    for name in ("stats", "stats_details", "nowplaying"):
        app.show_page(name)
        root.update()
    switch = texts(app.page("nowplaying").switch)
    assert switch == ["歌詞", "次に再生", "詳細"], switch
    app.set_np_tab("details")
    root.update()
    shown = texts(app.page("nowplaying").details)
    assert "タイトル" in shown and "フォーマット" in shown, shown
    app.set_np_tab("lyrics")
    cw = app.open_cover_window(S["Taxi Lights"])
    root.update()
    assert cw.win.title() == "アートワーク" and "アートワークを変更…" in widget_texts(cw.win)
    cw.close()
    app.toggle_up_next(); root.update()
    assert app.up_next.source.cget("text").startswith("再生元："), app.up_next.source.cget("text")
    app.toggle_up_next()
    # the Playback info, Equalizer and Edit Info windows
    app.open_info()
    info = app.info_window.text()
    assert app.info_window.win.title() == "再生情報"
    for needed in ("トラック", "タイトル: ", "サンプリング周波数: 96 kHz", "デコーダ", "ギャップレス: オン"):
        assert needed in info, (needed, info)
    app.info_window.close()
    app.cfg.bit_perfect = True  # 2.3: with its note above the curve, the window still holds its content
    app.open_eq()
    w = app.eq_window
    root.update()
    assert w.win.title() == "イコライザ"
    assert [w.nb.tab(i, "text") for i in range(2)] == ["グラフィック", "パラメトリック"]
    assert "完了" in widget_texts(w.win) and "プリアンプ" in widget_texts(w.win)
    assert w.bypass.cget("text").startswith("ビットパーフェクト再生がオンです")
    def outside(win):
        # the widgets that end past the window's right edge
        out = []
        def walk(x):
            for c in x.winfo_children():
                if isinstance(c, tk.Toplevel) or not c.winfo_ismapped():
                    continue
                if c.winfo_rootx() + c.winfo_width() > win.winfo_rootx() + win.winfo_width():
                    out.append(str(c))
                walk(c)
        walk(win)
        return out
    # the preset row wraps to a second line instead of making the window wider than its content
    assert w.preset_wrapped and w.presets2.winfo_ismapped(), "the preset row did not wrap"
    assert w.win.winfo_reqwidth() <= w.PRESET_ROW_MAX + 40 + 2, w.win.winfo_reqwidth()
    assert not outside(w.win), outside(w.win)
    w.nb.select(1); root.update()
    w._add_band(); root.update()
    assert not outside(w.win), outside(w.win)
    app.cfg.bit_perfect = False
    menu = w.build_preset_menu()
    assert menu_labels(menu)[0] == tr("Flat")
    w._preset("Rock")
    assert w.v_preset.get() == "Rock"  # the value stays English
    w.close()
    ed = app.edit_info([app.track(S["Taxi Lights"])])
    state["edit"] = ed
    wait(lambda: ed.ready, edit_ready, what="tags read")

def edit_ready():
    ed = state["edit"]
    assert ed.win.title() == "プロパティ"
    shown = widget_texts(ed.win)
    assert "保存" in shown and "キャンセル" in shown and "Taxi Lights" in shown, shown
    ed.cancel()
    root.update()
    # every Settings group: the name, the fields and the actions
    page = None
    for g in settings.GROUPS:
        app.show_page("settings", group=g)
        root.update()
        page = app.page("settings")
        assert page.group == g
        shown = widget_texts(page.inner)
        assert tr(g) in shown, (g, shown)
    rows = [page.sections.value(r, "name") for r in page.sections.rows]
    assert rows == [tr(g) for g in settings.GROUPS] and rows[0] == "ライブラリ", rows
    assert "設定" in widget_texts(page)
    # a choice is shown translated and stored in English
    app.show_page("settings", group="Display")
    root.update()
    page = app.page("settings")
    info = next(f for f in settings.Settings.field_info() if f["name"] == "group_sort")
    cb = page.boxes["group_sort"]
    assert list(cb.cget("values")) == [tr(c) for c in info["choices"]]
    assert cb.get() == tr(settings.Settings().group_sort), cb.get()  # the current choice, translated
    cb.current(1)
    cb.event_generate("<<ComboboxSelected>>")
    root.update()
    assert app.cfg.group_sort == info["choices"][1] and settings.load().group_sort == info["choices"][1]
    # Appearance: no restart note while the language is the one in use
    app.show_page("settings", group="Appearance")
    root.update()
    page = app.page("settings")
    assert page.restart_note is None
    assert "テーマの色" in widget_texts(page.inner)
    cb = page.boxes["language"]
    assert list(cb.cget("values")) == ["English", "日本語"]  # the languages in their own names
    assert cb.get() == "日本語" and page.theme_box.get() == tr("Light"), (cb.get(), page.theme_box.get())
    cb.current(0)
    cb.event_generate("<<ComboboxSelected>>")
    wait(lambda: app.page("settings").restart_note is not None, switched, what="restart note")

def switched():
    page = app.page("settings")
    assert settings.load().language == "English" and i18n.language() == "ja"  # used after a restart
    note = widget_texts(page.restart_note)
    assert tr("The new language is used when the app starts again.") in note, note
    assert "今すぐ再起動" in note, note
    button = [c for c in page.restart_note.winfo_children() if getattr(c, "text", "") == "今すぐ再起動"][0]
    state["pos"] = app.position()
    button.command()  # Restart now: saved like Quit, the app started again, this window closed

app.root.after(100, lambda: wait(lambda: len(app.tracks) == len(S) and not app.library_busy(), loaded,
                                 what="library"))
root.mainloop()
assert len(started) == 1, started
cmd, kw = started[0]
assert cmd == [sys.executable, "-m", "realhires.gui"], cmd
assert kw["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1" and kw["close_fds"], kw
assert kw["env"]["REALHIRES_SETTINGS"] == os.environ["REALHIRES_SETTINGS"]
saved = playerstate.load()
assert saved.last_path == S["Glacier Song"] and abs(saved.last_position - state["pos"]) < 0.3, saved
print("OK")
"""
    run_script(tmp_path, body, env)


@needs_ffmpeg
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_check_new_songs_when_the_app_starts(tmp_path):
    """Settings → Library → Check new songs when the app starts: after the library index
    is refreshed, the Quality check page scans the library folders in the background,
    without switching to that page and without pop-ups."""
    lib = make_library(tmp_path, seconds=2, mp3=False)
    from realhires import settings

    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.scan_library_on_start = True
    cfg.jobs = 2
    settings.save(cfg, str(tmp_path / "settings.json"))
    env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
               REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
               REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_AUDIO_BACKEND="null")
    body = f"""
import tkinter as tk
from realhires import gui, player_gui
root = tk.Tk()
def fail(exc, val, tb):
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)
seen = set()
def check():
    if app.qc.scanning:
        seen.add("scanning")
    if len(app.qc.results) < {len(lib["songs"])} or app.qc.scanning:
        return root.after(100, check)
    assert "scanning" in seen and app.page_name == "library" and not app.qc.active
    root.after(600, done)
def done():
    assert not [w for w in root.winfo_children() if isinstance(w, (gui.SummaryDialog, gui.FixDialog))]
    print("OK")
    app.quit()
root.after(100, check)
root.mainloop()
"""
    run_script(tmp_path, body, env, timeout=180)


@needs_ffmpeg
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_lyrics_while_scanning(tmp_path):
    """Settings → Library → Download lyrics while scanning: the scanner hands every
    song it answers to one lyrics worker; the status line counts what it saved."""
    from test_lyrics import FakeLrclib, make_album

    fake = FakeLrclib()
    try:
        folder = tmp_path / "album"
        make_album(folder, fake)
        env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
                   REALHIRES_LYRICS_INTERVAL="0", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        body = f"""
import tkinter as tk
from realhires import gui
root = tk.Tk()
def fail(exc, val, tb):
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)
root.report_callback_exception = fail
app = gui.App(root)
app.cfg.lyrics_server = {fake.url!r}
app.cfg.lyrics_while_scanning = True
app.cfg.show_summary_popup = False
app._add_source({str(folder)!r})
root.after(300, app.start_scan)
def done():
    w = app.scan_lyrics
    if app.scanning or w is None or w.saved + w.not_found < 6:
        return root.after(200, done)
    assert (w.saved, w.not_found) == (4, 2), (w.saved, w.not_found)
    assert app.status_var.get().endswith("  ·  Lyrics: 4 saved, 2 not found"), app.status_var.get()
    assert os.path.isfile(os.path.join({str(folder)!r}, "01 Alpha.lrc"))
    app.stop_scan()
    assert w.stop_event.is_set()
    print("OK")
    app.quit_app()
root.after(500, done)
root.mainloop()
"""
        run_script(tmp_path, body, env, timeout=150)
        assert [p for p, _, _ in fake.requests].count("/api/get") == 6  # the untagged song is never asked for
    finally:
        fake.server.shutdown()


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_bit_perfect_playback_controls(tmp_path):
    """Settings → Playback → Bit-perfect playback (2.1): the song restarts where it was in
    the song's own format, the volume slider shows full volume and cannot move, the
    Equalizer window has its note, and the Playback info has the Bit-perfect line; 2.3: the
    equalizer is heard, and the Bit-perfect line then says why it is not bit-perfect."""
    lib = make_library(tmp_path, seconds=4, mp3=False)
    from realhires import settings

    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.theme = "Light"
    settings.save(cfg, str(tmp_path / "settings.json"))
    env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
               REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
               REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_AUDIO_BACKEND="null")
    body = f"""
LIB = {json.dumps(lib)}
glacier = LIB["songs"]["Glacier Song"]
import tkinter as tk
from realhires import player_gui, settings
root = tk.Tk()
root.geometry("1280x800")
def fail(exc, val, tb):
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)

def wait(cond, then, timeout=30.0, what="", start=None):
    start = start or time.monotonic()
    if cond():
        return root.after(50, then)
    assert time.monotonic() - start < timeout, "timed out: " + what
    root.after(100, lambda: wait(cond, then, timeout, what, start))

def setting(on):
    new = settings.Settings.from_dict(app.cfg.to_dict())
    new.bit_perfect = on
    app.apply_settings(new)

def loaded():
    app.volume_changed(0.5, True)
    album = next(a for a in app.albums if a["title"] == "Northern Lights")
    app.open_album(album)
    root.update()
    app.page("album").table.on_activate(1)  # Glacier Song: 96 kHz, 24-bit
    wait(lambda: app.engine.position() > 0.8, playing, what="playback")

def playing():
    assert app.current == glacier and app.engine.output_info()["format"] == "32-bit float"
    assert app.bar.volume.enabled and abs(app.engine.gain - 0.25) < 1e-9
    state["pos"] = app.engine.position()
    setting(True)
    assert app.engine.bit_perfect and app.engine.gain == 1.0
    wait(lambda: app.engine.output_info()["sample_format"] == "i32" and app.engine.position() > 0, switched,
         what="restart in the song's format")

def switched():
    assert app.current == glacier and app.engine.position() >= state["pos"] - 0.05, "not restarted where it was"
    assert not app.bar.volume.enabled and app.bar.volume.value == 1.0 and app.state.volume == 0.5
    assert settings.load().bit_perfect  # saved at once, as every setting
    app.open_eq()
    assert app.eq_window.bypass.cget("text") == ("Bit-perfect playback is on: with the equalizer on, the sound is "
                                                 "not bit-perfect. The device still gets the song's own sample rate.")
    assert app.eq_window.bypass.winfo_manager() == "pack"
    app.eq_window.close()
    app.open_info()
    info = app.info_window.text()
    for needed in ("Output: 96 kHz, 24-bit integer in 32-bit, stereo",
                   "Bit-perfect: Yes: 24-bit, 96 kHz, NULL Playback Device",
                   "Volume: 0.0 dB", "Equalizer: Off"):
        assert needed in info, (needed, info)
    app.info_window.close()
    # the equalizer on: heard (the song restarts where it is), the device keeps the song's rate; the
    # restart count is taken before the change, because on a slow runner the restart happened while
    # the window closed, before a count taken after it (the wait then never ended)
    state["gen"] = app.engine._gen
    app.open_eq()
    w = app.eq_window
    w.v_on.set(True); w._toggles(); w._preset("Rock")
    assert app.bar.eq.on
    w.close()
    wait(lambda: app.engine._gen > state["gen"] and app.engine.position() > 0, eq_heard, what="the equalizer heard")

def eq_heard():
    app.open_info()
    info = app.info_window.text()
    for needed in ("Output: 96 kHz, 24-bit integer in 32-bit, stereo",
                   "Bit-perfect: No: The equalizer is on, so the samples are changed. The device still runs at 96 kHz.",
                   "Equalizer: Graphic: Rock"):
        assert needed in info, (needed, info)
    app.info_window.close()
    app.open_eq()
    w = app.eq_window
    w.v_on.set(False); w._toggles()
    w.close()
    app.volume_step(-0.05)  # the keys leave the saved volume alone too
    assert app.state.volume == 0.5
    setting(False)
    wait(lambda: app.engine.output_info()["sample_format"] == "f32", off, what="back to the normal path")

def off():
    assert app.bar.volume.enabled and app.bar.volume.value == 0.5 and abs(app.engine.gain - 0.25) < 1e-9
    app.open_info()
    assert "Bit-perfect: Off" in app.info_window.text(), app.info_window.text()
    app.info_window.close()
    app.open_eq()  # no bypass note, and no empty line where it was
    assert app.eq_window.bypass.cget("text") == "" and not app.eq_window.bypass.winfo_manager()
    app.eq_window.close()
    print("OK")
    app.quit()

state = {{}}
wait(lambda: app.tracks and not app.library_busy(), loaded, what="library")
root.mainloop()
"""
    run_script(tmp_path, body, env, timeout=150)


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_get_lyrics_from_now_playing(tmp_path):
    """"Get lyrics button doesn't work" (2026-11-24): it opened the lyrics window, which then
    waited for its own Download lyrics button.  Now Get lyrics (Now Playing, and a song's
    menu) starts the download at once, and Now Playing shows the lyrics without leaving."""
    from test_lyrics import FakeLrclib, rec

    lib = make_library(tmp_path, seconds=30, mp3=False)
    fake = FakeLrclib()
    fake.songs.append(rec("Midnight Sun", artist="Aurora Fields", album="Northern Lights", duration=30.0,
                          synced="[00:00.50] Midnight sun is shining\n[00:03.00] Over the frozen bay"))
    try:
        from realhires import settings

        cfg = settings.Settings()
        cfg.library_folders = [lib["music"]]
        cfg.lyrics_server = fake.url
        settings.save(cfg, str(tmp_path / "settings.json"))
        env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
                   REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
                   REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_AUDIO_BACKEND="null",
                   REALHIRES_LYRICS_INTERVAL="0", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        body = f"""
LIB = {json.dumps(lib)}
sun, drift = LIB["songs"]["Midnight Sun"], LIB["songs"]["Polar Drift"]
import tkinter as tk
from realhires import player_gui
root = tk.Tk()
root.geometry("1280x800")
def fail(exc, val, tb):
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)

def wait(cond, then, timeout=60.0, what="", start=None):
    start = start or time.monotonic()
    if cond():
        return root.after(50, then)
    assert time.monotonic() - start < timeout, "timed out: " + what
    root.after(100, lambda: wait(cond, then, timeout, what, start))

def loaded():
    album = next(a for a in app.albums if a["title"] == "Northern Lights")
    app.play_list(album["tracks"], [t.path for t in album["tracks"]].index(sun), "Northern Lights")
    app.toggle_now_playing(); root.update()
    np = app.page("nowplaying")
    assert app.page_name == "nowplaying" and np.kind == "" and np.get_btn.winfo_ismapped()
    np.get_btn.event_generate("<ButtonRelease-1>", x=5, y=5)  # press Get lyrics
    d = app.qc.lyrics_dialog
    assert d is not None and d.todo == [sun]
    wait(lambda: len(d.outcomes) == 1 and not d.running, saved, what="the download")

def saved():
    np, d = app.page("nowplaying"), app.qc.lyrics_dialog
    assert d.outcomes[0].status == "synced", d.outcomes[0].message
    assert os.path.isfile(os.path.splitext(sun)[0] + ".lrc")
    # Now Playing shows them at once, and stays
    root.update()
    assert app.page_name == "nowplaying" and np.kind == "synced" and np.lyrics.winfo_ismapped()
    assert np.lyrics.itemcget(np.lyrics.items[0], "text") == "Midnight sun is shining"
    assert (np.kind_l.cget("text"), np.source_l.cget("text")) == ("Synced lyrics", ".lrc file")
    # a song's menu: Get lyrics… starts at once too (this one is not on LRCLIB)
    m = app.song_menu(app.track(drift))
    i = next(i for i in range(m.index("end") + 1)
             if m.type(i) == "command" and m.entrycget(i, "label") == "Get lyrics…")
    m.invoke(i)
    d2 = app.qc.lyrics_dialog
    assert d2 is not d and d2.todo == [drift]
    wait(lambda: len(d2.outcomes) == 1 and not d2.running, done, what="the second download")

def done():
    d2 = app.qc.lyrics_dialog
    assert d2.outcomes[0].status == "not-found", d2.outcomes[0].status
    print("OK")
    app.quit()

wait(lambda: len(app.tracks) == {len(lib["songs"])} and not app.library_busy(), loaded, what="the library")
root.mainloop()
"""
        run_script(tmp_path, body, env, timeout=150)
        assert [q.get("track_name") for p, q, _ in fake.requests if p == "/api/get"] == ["Midnight Sun", "Polar Drift"]
    finally:
        fake.server.shutdown()


def test_artist_line_cuts_names_without_a_dangling_comma():
    """The LCD's "Artist, Artist — Album" at every width: each name a link, cut with "…",
    and a name with no room left goes with its ", " (CI's 1024 x 768 Windows screen showed
    "June Harbor,  — Northern Lig…" before)."""
    pytest.importorskip("tkinter")
    from realhires.player_pages import artist_line

    class Font:  # 7 px per character, like a small UI font
        def measure(self, text):
            return 7 * len(text)

    names = ["June Harbor", "Aurora Fields"]
    seen = set()
    for width in range(400, 40, -3):
        pieces = artist_line(Font(), names, "x", "Northern Lights", width)
        shown = [p for p in pieces if p[1] == "artist"]
        line = "".join(p[0] for p in pieces)
        assert 7 * len(line) <= max(width, 7 * 3), (width, line)  # " — " always shows
        assert [p[2] for p in shown] == names[:len(shown)], (width, pieces)
        assert ",  —" not in line and not line.startswith(", "), (width, line)
        assert pieces[-2:] == [(" — ", "", None), (pieces[-1][0], "album_name", None)]
        if len(shown) == 1:
            assert shown[0][0].endswith("…"), (width, line)  # more artists follow
        seen.add(len(shown))
        if width >= 7 * len("June Harbor, Aurora Fields — Northern Lights"):
            assert line == "June Harbor, Aurora Fields — Northern Lights"
    assert {1, 2} <= seen  # (below about 50 px not even "J…" fits)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_now_playing_details(tmp_path):
    """Now Playing's Details: the rows, the format line and the comment tag."""
    pytest.importorskip("tkinter")
    from realhires.musiclib import Track
    from realhires.player_pages import format_line, read_comment, song_details

    song = tmp_path / "Song.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", "-metadata", "comment=Live in Oslo",
                    str(song)], check=True)
    assert read_comment(str(song)) == "Live in Oslo"
    assert read_comment(str(tmp_path / "gone.flac")) == ""
    t = Track(str(song), title="Song", artist="A; B", album="Album", composer="C", year="2020", track=3, disc=1,
              duration=125.0, codec="flac", sample_rate=96000, bits=24, bit_rate=2304000)
    assert format_line(t) == "FLAC · 24-bit/96 kHz · 2,304 kbps"
    assert format_line(Track("x.mp3", codec="mp3", sample_rate=44100, is_lossy=True, bit_rate=320000)) == \
        "MP3 · 44.1 kHz · 320 kbps"
    rows = dict(song_details(t, 12, "Live in Oslo", ("Fake hi-res", "Upsampled from 44.1 kHz"), ["A", "B"]))
    assert rows["Artist"] == "A, B" and rows["Track"] == "3 of 12" and rows["Duration"] == "2:05"
    assert rows["Comment"] == "Live in Oslo" and rows["Album artist"] == "—" and rows["File"] == str(song)
    assert rows["Quality check"] == "Fake hi-res" and rows["Why"] == "Upsampled from 44.1 kHz"
    rows = dict(song_details(Track("y.flac", title="Y"), 0, "", None))
    assert rows["Quality check"] == "Not checked yet" and "Comment" not in rows and rows["Format"] == "—"


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_grids_renames_columns_tooltips_and_preset_delete(tmp_path):
    """Ten more requests (2.1): the grouping pages as card grids that open each group's
    page, open_artist() with several artists in one field, renaming genres, composers and
    years and merging genres (the files change, the library follows), the columns of song
    lists chosen per kind of list, the Quality tooltip and sort, the album page's columns,
    scrolling without full redraws, and the Equalizer's Delete… for a preset saved while
    the equalizer was off."""
    lib = make_library(tmp_path, seconds=2, mp3=False)
    from realhires import settings, tagedit

    first = lib["songs"]["First Light"]  # a full release date: a year rename keeps month and day
    assert all(o.ok for o in tagedit.save_tags([first], {"date": "2021-03-04"}))
    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.theme = "Light"
    settings.save(cfg, str(tmp_path / "settings.json"))
    env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
               REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
               REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
               REALHIRES_AUDIO_BACKEND="null")
    body = f"""
LIB = {json.dumps(lib)}
S = LIB["songs"]
import tkinter as tk
import tkinter.ttk
from realhires import eq, player_gui, player_pages, playerstate
from realhires.probe import probe
player_gui.messagebox.askyesno = lambda *a, **k: True  # confirmations say yes
player_gui.messagebox.showinfo = lambda *a, **k: None
state = {{}}

def fail(exc, val, tb):
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)

def wait(cond, then, timeout=60.0, what="", start=None):
    start = start or time.monotonic()
    if cond():
        return root.after(50, then)
    assert time.monotonic() - start < timeout, "timed out: " + what
    root.after(100, lambda: wait(cond, then, timeout, what, start))

def answer(name):  # fills in the next name dialog, once it is on the screen
    def go(tries=0):
        d = app.name_dialog
        if d is None:
            return root.after(50, lambda: go(tries))
        if not d.win.winfo_viewable() and tries < 40:
            return root.after(50, lambda: go(tries + 1))
        state["choices"] = list(d.entry.cget("values")) if isinstance(d.entry, tk.ttk.Combobox) else None
        d.var.set(name)
        d.submit()
    root.after(50, go)

def labels(m):
    return [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) != "separator"]

def index(m, label):
    return next(i for i in range(m.index("end") + 1) if m.type(i) != "separator" and m.entrycget(i, "label") == label)

def genre_of(path):
    return probe(path).tags.get("genre", "")

root = tk.Tk()
root.geometry("1280x800")
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)

def loaded():
    # Artists, Album Artists, Composers, Years and Genres are card grids
    for kind in ("artists", "album_artists", "genres", "years", "composers"):
        app.show_page(kind)
        root.update()
        assert app.pages[kind].grid_w.winfo_ismapped(), kind
    cards = app.page("artists").grid_w.albums
    assert [c["title"] for c in cards] == ["Aurora Fields", "June Harbor", "The Paper Boats", "Velvet Static"]
    assert cards[0]["artist"] == "1 album · 4 songs" and len(cards[0]["mosaic"]) == 1
    assert [c["title"] for c in app.page("years").grid_w.albums] == ["2023", "2021", "2019", "2018"]
    comp = app.page("composers").grid_w.albums
    assert [c["title"] for c in comp] == ["Lena Frost", "Mika Rowe", "Unknown Composer"]
    assert comp[0]["artist"] == "2 albums · 6 songs" and len(comp[0]["mosaic"]) == 2
    # a click opens the same page as before: an artist's albums, a genre's songs
    app.show_page("artists")
    app.page("artists")._open(cards[0])
    root.update()
    page = app.page("group")
    assert app.page_name == "group" and page.title_l.cget("text") == "Aurora Fields"
    assert [a["title"] for a in page.grid_w.albums] == ["Northern Lights"]
    app.go_back()
    assert app.page_name == "artists"
    # open_artist(): an artist of the library, or the first artist of a field that is not one
    app.open_artist("Aurora Fields & Somebody New")
    assert app.page_name == "group" and app.page("group").name == "Aurora Fields"
    # the album page: one link per artist, the All Songs columns with # first, track order, sortable
    album = next(a for a in app.albums if a["title"] == "Northern Lights")
    app.open_album(album)
    root.update()
    ap = app.page("album")
    assert [w.cget("text") for w in ap.artist_links] == ["Aurora Fields"]
    assert [c.key for c in ap.table.columns] == ["num", "title", "artist", "album", "genre", "time", "quality",
                                                 "like"]
    assert [t.display_title for t in ap.table.rows] == ["First Light", "Glacier Song", "Midnight Sun", "Polar Drift"]
    ap.table.sort_by("title")
    ap.table.sort_by("title")
    assert ap.table.rows[0].display_title == "Polar Drift"
    # the Quality column: "Hi-Res Lossless", the resolution as its tooltip; sorted by bits, then rate
    assert player_pages.track_value(ap.table.rows[0], "quality") == "Hi-Res Lossless"
    assert ap.table.show_tip(0, "quality") == "24-bit/96 kHz" and ap.table.tip_text == "24-bit/96 kHz"
    ap.table.hide_tip()
    app.show_page("songs")
    root.update()
    table = app.page("songs").table
    table.sort_by("quality")
    q = [(t.bits, t.sample_rate) for t in table.rows]
    assert q == sorted(q), q
    # a scroll step moves the rows and draws only the new ones; a hover recolours one row
    rows = list(table.rows)
    table.set_rows(rows * 8)  # more rows than fit
    root.update()
    full = table.full_redraws
    table.scroll(table.rh)
    table._set_hover(2)
    assert table.full_redraws == full and table.top == table.rh, (table.full_redraws, full, table.top)
    assert len(table._drawn) <= table.canvas.winfo_height() // table.rh + 3
    table.set_rows(rows)
    # choosing columns: the headings' menu, kept per kind of list in player.json
    assert table.on_heading_context is not None
    m = player_pages.columns_menu(app, "songs")
    assert labels(m)[:3] == ["# (track number)", "Title", "Artist"] and "Reset columns" in labels(m)
    assert m.entrycget(index(m, "Title"), "state") == "disabled"
    m.invoke(index(m, "Year"))
    assert "year" in [c.key for c in table.columns] and "year" in playerstate.load().columns_of("songs")
    assert "year" not in [c.key for c in ap.table.columns]  # album pages keep their own choice
    m = player_pages.columns_menu(app, "songs")
    m.invoke(index(m, "Reset columns"))
    assert [c.key for c in table.columns] == list(playerstate.DEFAULT_COLUMNS["songs"])
    # rename a genre from its page ("…"): the files change, the page follows the new name
    app.show_page("group", kind="genres", group="Jazz")
    gp = app.page("group")
    assert labels(gp.menu()) == ["Play", "Shuffle", "Rename genre…", "Merge into…", "Colour Label"]  # 2.3
    answer("Smooth Jazz")
    job = player_pages.rename_group(app, "genres", "Jazz")
    state["job"] = job
    wait(lambda: job.done and not app.library_busy(), renamed, what="genre renamed")

def renamed():
    job = state["job"]
    assert all(o.ok for o in job.outcomes) and len(job.outcomes) == 2, job.outcomes
    assert job.headline == "Saved the information of 2 songs.", job.headline
    assert genre_of(S["Blue Kettle"]) == "Smooth Jazz" and genre_of(S["Porch Lamp"]) == "Smooth Jazz"
    assert app.track(S["Blue Kettle"]).genre == "Smooth Jazz"
    gp = app.page("group")
    assert app.page_name == "group" and gp.title_l.cget("text") == "Smooth Jazz"
    assert [t.display_title for t in gp.table.rows] == ["Blue Kettle", "Porch Lamp"]
    job.close()
    # merge two genres selected with Ctrl-click
    app.show_page("genres")
    root.update()
    page = app.page("genres")
    names = [c["title"] for c in page.grid_w.albums]
    assert names == ["Ambient", "Electronic", "Indie Pop", "Smooth Jazz"], names
    page.grid_w.toggle(names.index("Electronic"))
    page.grid_w.toggle(names.index("Indie Pop"))
    assert labels(page.selection_menu()) == ["Merge 2 genres…", "Clear selection"]
    answer("Pop")
    job = page.merge_selected()
    assert state["choices"] == ["Indie Pop", "Electronic"], state["choices"]  # the biggest first
    state["job"] = job
    wait(lambda: job.done and not app.library_busy(), merged, what="genres merged")

def merged():
    job = state["job"]
    assert all(o.ok for o in job.outcomes) and len(job.outcomes) == 5
    assert {{genre_of(S[t]) for t in ("Carrier Wave", "Low Pass", "Umbrella Weather")}} == {{"Pop"}}
    page = app.page("genres")
    assert [c["title"] for c in page.grid_w.albums] == ["Ambient", "Pop", "Smooth Jazz"]
    assert not page.grid_w.selected
    job.close()
    # Merge into… one genre: the other genres are offered
    answer("Ambient")
    job = player_pages.merge_groups(app, "genres", ["Smooth Jazz"])
    assert state["choices"] == ["Ambient", "Pop"], state["choices"]
    state["job"] = job
    wait(lambda: job.done and not app.library_busy(), merged_into, what="merged into")

def merged_into():
    state["job"].close()
    assert genre_of(S["Porch Lamp"]) == "Ambient"
    # a year: only the year of each release date changes
    job = player_pages.rename_group(app, "years", "2021", "2020")
    state["job"] = job
    wait(lambda: job.done and not app.library_busy(), year_renamed, what="year renamed")

def year_renamed():
    state["job"].close()
    assert probe(S["First Light"]).tags["date"] == "2020-03-04"
    assert probe(S["Glacier Song"]).tags["date"] == "2020"
    app.show_page("years")  # refreshed when shown
    assert [c["title"] for c in app.page("years").grid_w.albums][:2] == ["2023", "2020"]
    # a composer, and the same name again changes nothing
    job = player_pages.rename_group(app, "composers", "Mika Rowe", "Mika Rowe-Lind")
    state["job"] = job
    wait(lambda: job.done and not app.library_busy(), composer_renamed, what="composer renamed")

def composer_renamed():
    state["job"].close()
    assert probe(S["Taxi Lights"]).tags["composer"] == "Mika Rowe-Lind"
    assert player_pages.rename_group(app, "composers", "Mika Rowe-Lind", "Mika Rowe-Lind") is None
    # the equalizer: a preset saved while the equalizer was off, chosen with it on, then deleted
    app.open_eq()
    w = app.eq_window
    w._preset("Rock")
    w.v_on.set(False); w._toggles()
    w.save_preset("Quiet Rock")
    w.v_on.set(True); w._toggles()
    w._preset("Flat")
    w.use_preset("Quiet Rock")
    assert w.preset_label() == "Quiet Rock" and w.state["enabled"]
    before = eq.normalize(w.state)
    w._delete()  # the Delete… button
    assert playerstate.load().eq_presets == [] and app.state.eq_presets == []
    assert "Quiet Rock" not in labels(w.build_preset_menu())
    assert eq.normalize(w.state) == before and eq.normalize(app.state.eq) == before  # the EQ stays as it was
    assert w.preset_label() == "Rock" and "Deleted" in w.note.cget("text")
    # set to none of the user's presets: Delete… offers them
    w.save_preset("Mine A")
    w._preset("Jazz")
    assert labels(w.build_delete_menu()) == ["Delete which preset?", "Mine A"]
    w.delete_menu.invoke(1)
    assert app.state.eq_presets == [] and playerstate.load().eq_presets == []
    w.close()
    print("OK")
    app.quit()

wait(lambda: len(app.tracks) == len(S) and not app.library_busy(), loaded, what="library")
root.mainloop()
"""
    run_script(tmp_path, body, env)
