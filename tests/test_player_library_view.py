"""2.3 (docs/player-spec.md, "2.3" items 2, 3 and 13) in the Tk player: the Library
page as tiles or a list (the setting library_layout and the two buttons by the
title), choosing genres to merge or rename (Select, "N selected", Merge…, Rename…,
Done; Ctrl-click), and Your Stats with the day's songs beside the calendar.  The GUI
tests run in a subprocess, as test_player_gui.py's do (skipped without Tk or a
display; locally: xvfb-run -a python -m pytest)."""

import json
import os
import shutil
import subprocess

import pytest

from player_library import make_library
from test_player_gui import _gui_available, _has_miniaudio, run_script
from realhires import settings
from realhires.store import settings_key

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_library_layout_setting(tmp_path):
    """Settings → Appearance → Library page: "Tiles" (the default) or "List", kept in
    settings.json, not a Detection setting; the file is read the same way by both versions."""
    info = {f["name"]: f for f in settings.Settings.field_info()}
    f = info["library_layout"]
    assert (f["group"], f["label"], f["kind"], f["default"]) == ("Appearance", "Library page", "choice", "Tiles")
    assert f["choices"] == settings.LIBRARY_LAYOUTS == ["Tiles", "List"]
    names = [x["name"] for x in settings.Settings.field_info()]
    assert names.index("library_layout") == names.index("font_family") + 1
    cfg = settings.Settings()
    assert cfg.library_layout == "Tiles"
    cfg.set_value("library_layout", "List")
    assert settings_key(cfg) == settings_key(settings.Settings())  # saved results stay valid
    path = tmp_path / "settings.json"
    settings.save(cfg, str(path))
    assert settings.load(str(path)).library_layout == "List"
    assert '  "library_layout": "List",\n' in path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        cfg.set_value("library_layout", "Grid")
    assert settings.Settings.from_dict({"library_layout": "Grid"}).library_layout == "Tiles"
    # a file as the Rust version writes it (the same keys, two spaces, "_version" last)
    rust = '{\n  "language": "English",\n  "font_family": "",\n  "library_layout": "List",\n  "_version": 160\n}'
    path.write_text(rust, encoding="utf-8")
    assert settings.load(str(path)).library_layout == "List"


def test_rust_reads_the_library_layout():
    """The Rust version reads library_layout from a file the Python version saved."""
    from test_rust_parity import run_rust, rust_cli

    if rust_cli() is None:
        pytest.skip("Rust CLI not built (cargo build --release in rust/)")
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "settings.json")
        cfg = settings.Settings()
        cfg.library_layout = "List"
        settings.save(cfg, path)
        out = run_rust("--settings", path, "--list-settings").stdout
    line = next(x for x in out.splitlines() if x.startswith("library_layout "))
    assert line.split("=", 1)[1].split("#")[0].strip() == "'List'", line


def _env(tmp_path, lib, **cfg_values):
    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.theme = "Light"
    for k, v in cfg_values.items():
        setattr(cfg, k, v)
    settings.save(cfg, str(tmp_path / "settings.json"))
    return dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
                REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
                REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
                REALHIRES_COVERS=str(tmp_path / "covers"), REALHIRES_AUDIO_BACKEND="null")


GUI_HELPERS = """
import tkinter as tk
import tkinter.ttk
from realhires import player_gui, player_pages, settings, stats
from realhires.probe import probe
player_gui.messagebox.askyesno = lambda *a, **k: True  # confirmations say yes
player_gui.messagebox.showinfo = lambda *a, **k: None
state = {}

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

def texts(canvas):
    return [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]

def click(canvas, box, **kw):  # a click in the middle of a box drawn on a canvas
    x, y = (box[0] + box[2]) / 2 - canvas.canvasx(0), (box[1] + box[3]) / 2 - canvas.canvasy(0)
    canvas.event_generate("<Button-1>", x=int(x), y=int(y), **kw)
    root.update()

def press(button):  # a PillButton or IconButton, as a click releases it
    button.event_generate("<ButtonRelease-1>", x=5, y=5)
    root.update()

def answer(name):  # fills in the next name dialog, once it is on the screen
    def go(tries=0):
        d = app.name_dialog
        if d is None:
            return root.after(50, lambda: go(tries))
        if not d.win.winfo_viewable() and tries < 40:
            return root.after(50, lambda: go(tries + 1))
        state["choices"] = list(d.entry.cget("values")) if isinstance(d.entry, tk.ttk.Combobox) else None
        state["initial"] = d.var.get()
        d.var.set(name)
        d.submit()
    root.after(50, go)

def genre_of(path):
    return probe(path).tags.get("genre", "")

root = tk.Tk()
root.geometry("1280x800")
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)
"""


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_library_list_and_choosing_genres(tmp_path):
    """The Library page as a list and back (the buttons by the title, saved at once, and
    the Settings page's menu), then the Genres page's Select: a click selects instead of
    opening, "N selected", Merge… from two and Rename… with one, Done and Esc, Ctrl-click
    without select mode, and the merge and the rename writing the files."""
    lib = make_library(tmp_path, seconds=2, mp3=False)
    env = _env(tmp_path, lib)
    body = f"""
LIB = {json.dumps(lib)}
S = LIB["songs"]
""" + GUI_HELPERS + """
def loaded():
    page = app.page("library")
    root.update()
    assert app.page_name == "library" and page.layout() == "Tiles"
    keys = [e[0] for e in page.entries()]
    assert keys == ["songs", "folders", "albums", "artists", "album_artists", "genres", "years", "composers",
                    "playlists", "liked", "disliked", "played", "recent", "stats"], keys
    tiles = dict((key, box) for box, key in page.entry_hits)
    assert len(tiles) == 14 and tiles["songs"][3] - tiles["songs"][1] == page.TILE_H
    assert set(page.layout_hits) == {"Tiles", "List"}
    # the list button: one row per entry, the width of the page, saved at once
    click(page.c, page.layout_hits["List"])
    assert app.cfg.library_layout == "List" and settings.load().library_layout == "List"
    assert page.layout() == "List"
    rows = [box for box, key in page.entry_hits]
    W = page.c.winfo_width()
    assert len(rows) == 14 and all(b[3] - b[1] == page.ROW_H == 44 for b in rows), rows
    assert all((b[0], b[2]) == (page.MARGIN, W - page.MARGIN) for b in rows)
    assert all(rows[i + 1][1] == rows[i][3] for i in range(13))  # one under the other
    shown = texts(page.c)
    assert "All Songs" in shown and "11 songs" in shown and "Your Stats" in shown and "Recently Added" in shown
    lines = [i for i in page.c.find_all() if page.c.type(i) == "line"]
    assert len(lines) >= 13  # a line between the rows
    # a row opens its page as a tile does
    genres_row = next(box for box, key in page.entry_hits if key == "genres")
    click(page.c, genres_row)
    assert app.page_name == "genres"
    app.go_back()
    assert app.page_name == "library" and page.layout() == "List"
    # the Settings page shows the same setting (translated choices, the English value kept)
    app.show_page("settings", group="Appearance")
    root.update()
    sp = app.page("settings")
    box = sp.boxes["library_layout"]
    assert list(box.cget("values")) == ["Tiles", "List"] and box.get() == "List"
    box.current(0)
    box.event_generate("<<ComboboxSelected>>")
    root.update()
    assert app.cfg.library_layout == "Tiles" and settings.load().library_layout == "Tiles"
    app.show_page("library")
    root.update()
    assert page.layout() == "Tiles" and len([1 for _, k in page.entry_hits]) == 14
    # the tiles button again changes nothing; the list button and the tiles button switch back and forth
    click(page.c, page.layout_hits["Tiles"])
    assert app.cfg.library_layout == "Tiles"
    genres()

def genres():
    app.show_page("genres")
    root.update()
    page = app.page("genres")
    names = [c["title"] for c in page.grid_w.albums]
    assert names == ["Ambient", "Electronic", "Indie Pop", "Jazz"], names
    # the title row: Select, and a hint naming the key of this system
    assert page.select_b.winfo_ismapped() and not page.done_b.winfo_ismapped()
    key = "Cmd" if sys.platform == "darwin" else "Ctrl"  # on macOS Ctrl-click is a right-click
    assert f"Press Select or {key}-click genres to merge or rename them" in page.sub.cget("text"), page.sub.cget("text")
    press(page.select_b)
    assert page.grid_w.select_mode and page.done_b.winfo_ismapped() and not page.select_b.winfo_ismapped()
    assert page.count_l.cget("text") == "0 selected" and "Click genres to select them" in page.sub.cget("text")
    assert not page.merge_b.enabled and not page.rename_b.enabled
    g = page.grid_w
    def card(name):
        x, y, _i = g.positions[next(c["key"] for c in g.albums if c["title"] == name)]
        return (x, y, x + g.COVER, y + g.COVER)
    click(g.canvas, card("Electronic"))  # selects instead of opening
    assert app.page_name == "genres" and [c["title"] for c in g.selected_items()] == ["Electronic"]
    assert page.count_l.cget("text") == "1 selected" and page.rename_b.enabled and not page.merge_b.enabled
    click(g.canvas, card("Indie Pop"))
    click(g.canvas, card("Jazz"))
    click(g.canvas, card("Jazz"))  # again: unselected
    assert [c["title"] for c in g.selected_items()] == ["Electronic", "Indie Pop"]
    assert page.count_l.cget("text") == "2 selected" and page.merge_b.enabled and not page.rename_b.enabled
    # Done: out of select mode, nothing selected; a click opens again
    press(page.done_b)
    assert not g.select_mode and not g.selected and page.select_b.winfo_ismapped()
    # Esc leaves select mode as Done does
    press(page.select_b)
    click(g.canvas, card("Jazz"))
    # a key goes to the widget with the focus: on the macOS runner the window is not the active one,
    # so the click's focus_set() does not move it; Esc is handled for the whole window
    root.focus_force()
    root.update()
    root.event_generate("<Escape>")
    root.update()
    assert not g.select_mode and not g.selected
    # Ctrl-click (Cmd-click on macOS: Tk's Mod1) selects without select mode; the title row shows the buttons
    mod = 0x8 if sys.platform == "darwin" else 0x4
    click(g.canvas, card("Electronic"), state=mod)
    click(g.canvas, card("Indie Pop"), state=mod)
    assert not g.select_mode and app.page_name == "genres"
    assert [c["title"] for c in g.selected_items()] == ["Electronic", "Indie Pop"]
    assert page.done_b.winfo_ismapped() and page.merge_b.enabled and page.count_l.cget("text") == "2 selected"
    # Merge…: the name in one dialog (the selected genres, the biggest first), then the files change
    answer("Pop")
    press(page.merge_b)
    wait(lambda: app.tag_job is not None, merging, what="the merge to start")

def merging():
    job = app.tag_job
    assert state["choices"] == ["Indie Pop", "Electronic"] and state["initial"] == "Indie Pop", state
    page = app.page("genres")
    assert not page.grid_w.selected and not page.grid_w.select_mode  # done with the selection
    state["job"] = job
    wait(lambda: job.done and not app.library_busy(), merged, what="genres merged")

def merged():
    job = state["job"]
    assert all(o.ok for o in job.outcomes) and len(job.outcomes) == 5, job.outcomes
    assert {genre_of(S[t]) for t in ("Carrier Wave", "Low Pass", "Umbrella Weather")} == {"Pop"}
    job.close()
    page = app.page("genres")
    assert [c["title"] for c in page.grid_w.albums] == ["Ambient", "Jazz", "Pop"]
    # Rename… with exactly one genre selected
    press(page.select_b)
    g = page.grid_w
    i = [c["title"] for c in g.albums].index("Jazz")
    g.toggle(i)
    assert page.rename_b.enabled
    answer("Smooth Jazz")
    press(page.rename_b)
    wait(lambda: app.tag_job is not None and app.tag_job is not job, renaming, what="the rename to start")

def renaming():
    job = app.tag_job
    assert state["initial"] == "Jazz"
    assert not app.page("genres").grid_w.select_mode
    state["job"] = job
    wait(lambda: job.done and not app.library_busy(), renamed, what="genre renamed")

def renamed():
    state["job"].close()
    assert genre_of(S["Blue Kettle"]) == "Smooth Jazz" and genre_of(S["Porch Lamp"]) == "Smooth Jazz"
    # a page shown again starts without a selection
    page = app.page("genres")
    press(page.select_b)
    app.show_page("library")
    app.show_page("genres")
    root.update()
    assert not page.grid_w.select_mode and page.select_b.winfo_ismapped()
    print("OK")
    app.quit()

wait(lambda: len(app.tracks) == len(S) and not app.library_busy(), loaded, what="library")
root.mainloop()
"""
    run_script(tmp_path, body, env)


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_stats_day_beside_the_calendar(tmp_path):
    """Your Stats: the hint and the chosen day's plays to the right of the calendar, as
    tall as it, scrolling inside it when longer (the page does not scroll then); a click
    plays the song; on a page narrower than the calendar plus 360 px they go under it."""
    lib = make_library(tmp_path, seconds=2, mp3=False)
    env = _env(tmp_path, lib)
    body = f"""
LIB = {json.dumps(lib)}
S = LIB["songs"]
""" + GUI_HELPERS + """
def loaded():
    # 30 plays today, between midnight and now: a list taller than the calendar
    now = time.time()
    t0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    for i in range(30):
        tr = app.tracks[i % len(app.tracks)]
        app.lib.add_play(tr, t0 + (now - t0) * (i + 1) / 32, 30 + i)
    app.engine.pause()
    app.show_page("stats")
    root.update()
    st = app.page("stats")
    st.refresh()
    root.update()
    W = st.area.c.winfo_width()
    if not st.side_by_side(W):
        # the CI runners' 1024 x 768 screens leave the page narrower than the calendar plus 360 px:
        # then only the layout under the calendar can be checked here
        assert W - 2 * st.MARGIN < 7 * st.cell_width(W) + st.SIDE_MIN, W
        print("narrow screen: the day's plays go under the calendar")
        return narrow_from_start()
    assert st.cell_width(W) == st.CELL_W, W
    # no day chosen: the hint, beside the calendar
    assert st.day_c.winfo_ismapped() and "Click a day to see the songs you played on it." in texts(st.day_c)
    assert "Click a day to see the songs you played on it." not in texts(st.area.c)
    x, y, w, h = st.day_box
    cal_right = st.MARGIN + 7 * st.CELL_W
    assert x == cal_right + st.SIDE_GAP and abs(x + w - (W - st.MARGIN)) < 1 and w >= st.SIDE_MIN - st.SIDE_GAP
    # its height is the calendar's: six or five weeks of 44 px under the title and the weekdays
    weeks = len(stats.month_grid(*st.month))
    assert h == 42 + 22 + weeks * st.CELL_H, (h, weeks)
    assert st.day_c.winfo_height() == h and st.day_c.winfo_width() == w
    today = time.localtime()
    st.show_month(today.tm_year, today.tm_mon)
    st.choose_day(today.tm_mday)
    root.update()
    dc = st.day_c
    shown = texts(dc)
    assert stats.day_title(*st.day) in shown and len(st.day_rows) == 30
    assert stats.day_title(*st.day) not in texts(st.area.c)
    # longer than the calendar: it scrolls inside, the page does not
    assert st.day_list_scrolls()
    page_top = st.area.c.yview()[0]
    st._day_wheel(3)
    root.update()
    assert dc.yview()[0] > 0 and st.area.c.yview()[0] == page_top
    # a click on a song (the newest is at the top) plays it, with the day's songs as the list
    dc.yview_moveto(0)
    root.update()
    first = st.day_rows[0]
    item = next(i for i in dc.find_all() if dc.type(i) == "text" and dc.itemcget(i, "text") == first["title"])
    cx, cy = dc.coords(item)
    dc.event_generate("<Button-1>", x=int(cx) + 5, y=int(cy))
    root.update()
    assert app.current == first["path"] and app.context_name == "Your Stats", (app.current, first)
    app.engine.pause()
    # the summary cards start under the calendar, not under the list
    cards = [i for i in st.area.c.find_all() if st.area.c.type(i) == "text"
             and st.area.c.itemcget(i, "text") == "MINUTES LISTENED"]
    assert cards and st.area.c.coords(cards[0])[1] < y + h + 60, st.area.c.coords(cards[0])
    # narrower than the calendar plus 360 px: the list goes under the calendar, in the page
    root.geometry("900x800")
    wait(lambda: st.area.c.winfo_width() < 7 * st.CELL_W + st.SIDE_MIN + 2 * st.MARGIN, narrow, what="narrow")

def narrow_from_start():
    st = app.page("stats")
    today = time.localtime()
    st.show_month(today.tm_year, today.tm_mon)
    st.choose_day(today.tm_mday)
    root.update()
    narrow()

def narrow():
    st = app.page("stats")
    st.area.redraw()
    root.update()
    assert not st.side_by_side() and st.day_box is None and not st.day_c.winfo_ismapped()
    shown = texts(st.area.c)
    assert stats.day_title(*st.day) in shown and st.day_rows[0]["title"] in shown
    print("OK")
    app.quit()

wait(lambda: len(app.tracks) == len(S) and not app.library_busy(), loaded, what="library")
root.mainloop()
"""
    run_script(tmp_path, body, env)
