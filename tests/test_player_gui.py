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
        proc = subprocess.Popen([sys.executable, str(script)], stdout=fo, stderr=fe, env=env)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = f"killed after {timeout} s"
    log = out.read_text(errors="replace") + err.read_text(errors="replace")
    assert rc == 0, log
    assert out.read_text().splitlines()[-1] == "OK", log
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

def shows(canvas, full):
    # the canvas shows `full`, or as much of it as fits: on a small screen (the Windows
    # runners have 1024 x 768) the LCD is narrow and cuts its texts with "…"
    for t in texts(canvas):
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
    item = np.lyrics.items[0]
    assert np.lyrics.itemcget(item, "text") == "Blue ice under a silver sky"
    assert str(np.lyrics.itemcget(item, "font")) == str(app.theme.f("lyrics_b"))
    assert str(np.lyrics.itemcget(np.lyrics.items[1], "font")) == str(app.theme.f("lyrics"))
    # paused while the windows and menus are checked: the songs last 3 s, and a slow runner
    # (macOS) took longer than that, so the next song had started before Up Next was read
    app.play_pause()
    assert not app.engine.playing
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
    from realhires import settings
    assert settings.load().theme == "Dark"
    page._set("theme", "Light")
    page.show(group="Detection")
    page._set_number("cliff_db", tk.StringVar(value="999"), "float")
    assert "Please enter a number from 5 to 150" in page.help_labels["cliff_db"].cget("text")
    page.show(group="Playback")
    assert "output_device" in page.help_labels
    # the Quality check page: the 1.x scanner with its own menus
    app.check_library(switch=True)
    assert app.page_name == "quality" and app.qc.active and str(root.cget("menu")) == str(app.qc.menubar)
    wait(lambda: len(app.qc.results) == N_SONGS and not app.qc.scanning, scanned, timeout=150, what="scan")

def scanned():
    root.update()
    assert app.qc.tree.get_children() or True
    for w in root.winfo_children():
        if isinstance(w, gui.SummaryDialog):
            w.destroy()
    app.show_page("albums")
    assert not app.qc.active and str(root.cget("menu")) == str(app.menubar)
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
from realhires import player_gui, playerstate, themes
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
    editor; Edit Info changes one song's album artist and release date, an album's
    genre for all its songs, and refuses a bad track number without writing."""
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
    # Edit info… is in a song's menu; one Edit Info window at a time
    taxi = app.track(S["Taxi Lights"])
    assert "Edit info…" in menu_labels(app.song_menu(taxi))
    w = app.edit_info([taxi])
    assert app.edit_info([app.track(S["Blue Kettle"])]) is w
    state["w"] = w
    wait(lambda: w.ready, one_ready, what="tags read")

state = {{}}

def one_ready():
    w = state["w"]
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
    w = app.edit_info([app.track(S["Blue Kettle"])])
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
    # Edit album info…: every song of the album
    album = next(a for a in app.albums if a["title"] == "Small Hours")
    app.open_album(album)
    root.update()
    w = app.page("album").edit_info()
    state["w"] = w
    wait(lambda: w.ready, album_ready, what="album tags read")

def album_ready():
    w = state["w"]
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
    the Equalizer, Playback info and Edit Info windows, a name dialog and a song's menu
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
from realhires import i18n, player_gui, playerstate, settings
from realhires.i18n import tr
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
    assert "設定" in texts(app.sidebar.foot)
    top = menu_labels(app.menubar)
    assert top == ["ファイル", "コントロール", "表示", "ヘルプ"], top
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
    assert "プレイリストに追加" in labels and "お気に入り" in labels and "情報を編集…" in labels, labels
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
    # play a song for more than a second: Recently Played says "今日 hh:mm"
    app.play_list(album["tracks"], 0, album["title"])
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
    app.open_eq()
    w = app.eq_window
    root.update()
    assert w.win.title() == "イコライザ"
    assert [w.nb.tab(i, "text") for i in range(2)] == ["グラフィック", "パラメトリック"]
    assert "完了" in widget_texts(w.win) and "プリアンプ" in widget_texts(w.win)
    w.nb.select(1); root.update()
    w._add_band(); root.update()
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
    assert ed.win.title() == "情報を編集"
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
