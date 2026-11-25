"""2.3 "Colours" (docs/player-spec.md, "9. Colours"): every colour the apps draw is a
theme colour (themes.EXTRA_COLOR_KEYS, derived from the eight unless the theme sets
it, themes.colors()), the quality colours, the theme editor's More colours, and colour
labels (playerstate labels, "Colour Label ▸" in the menus, the dots).  The core here,
the Tk player headlessly in a subprocess (skipped without Tk or a display; locally:
xvfb-run -a python -m pytest).  The Rust version: rust/crates/core/tests/player_additions.rs
(the vectors) and labels::tests in rust/crates/gui."""

import json
import os

import pytest

from realhires import playerstate, themes
from test_player_gui import _gui_available, _has_miniaudio, _has_mp3_encoder, needs_ffmpeg, run_script
from player_library import make_library


# ---------------------------------------------------------------- the core ---
def test_every_builtin_theme_keeps_its_look_and_gets_the_quality_colours():
    for name, t in themes.BUILTIN.items():
        c = themes.colors(dict(t, name=name))
        assert list(c) == themes.COLOR_KEYS + themes.EXTRA_COLOR_KEYS
        assert all(c[k] == t["colors"][k] for k in themes.COLOR_KEYS)
        # the derivations the apps used before 2.3
        assert c["row_hover"] == themes.mix(c["background"], c["hover"], 0.7)
        assert c["zebra"] == themes.mix(c["background"], c["hover"], 0.3)
        assert c["selection"] == c["field"] == c["placeholder"] == c["hover"]
        assert c["placeholder_icon"] == themes.mix(c["hover"], c["secondary"], 0.6)
        assert c["disabled"] == themes.mix(c["background"], c["secondary"], 0.45)
        assert c["on_accent"] == ("#1d1d1f" if themes.luminance(c["accent"]) > 0.62 else "#ffffff")
        # the quality colours are no longer grey: gold, the accent, the secondary text
        assert c["quality_hires"] == ("#e0b84a" if t["dark"] else "#c9a227")
        assert c["quality_lossless"] == c["accent"] == c["calendar"]
        assert c["quality_lossy"] == c["secondary"]
        assert [c[k] for k in themes.VERDICT_COLOR_KEYS.values()] == \
            ["#a3122a", "#d32f2f", "#b26a00", "#2e7d32", "#1565c0", "#6a1b9a", "#616161"]
    # the Light theme's values, as the apps drew them
    light = themes.colors(themes.resolve("Light", False, []))
    assert (light["row_hover"], light["zebra"], light["disabled"]) == ("#efeff2", "#f8f8fa", "#bebec0")
    assert light["on_accent"] == "#ffffff" and themes.colors(themes.resolve("Forest", False, []))["on_accent"] == "#1d1d1f"
    # a pale accent gets dark text on it
    pale = themes.normalize({"name": "Pale", "colors": {"accent": "#ffd60a"}})
    assert themes.colors(pale)["on_accent"] == "#1d1d1f"


def test_extra_colours_are_kept_only_when_set_and_valid():
    t = themes.normalize({"name": "Mine", "dark": True, "colors": {
        "accent": "#00ff00", "row_hover": " #ABCDEF ", "quality_hires": "#00aa88", "calendar": "red",
        "verdict_cd": 7, "nonsense": "#ffffff"}})
    assert {k for k in t["colors"] if k not in themes.COLOR_KEYS} == {"row_hover", "quality_hires"}
    c = themes.colors(t)
    assert (c["row_hover"], c["quality_hires"]) == ("#abcdef", "#00aa88")
    assert c["calendar"] == c["quality_lossless"] == "#00ff00"  # unset: derived from the new accent
    assert c["zebra"] == themes.mix(c["background"], c["hover"], 0.3)
    assert "nonsense" not in c
    assert themes.color_label("row_hover") == "Row under the pointer"
    assert themes.color_label("accent") == "Accent"


def test_theme_files_carry_the_extra_colours_that_are_set(tmp_path):
    mine = themes.normalize({"name": "Mine", "colors": {"accent": "#123456", "verdict_fake_hires": "#0000ff"}})
    text = themes.export_theme(mine)
    data = json.loads(text)
    assert data["colors"]["verdict_fake_hires"] == "#0000ff" and "row_hover" not in data["colors"]
    back = themes.import_theme(text, [])
    assert back == mine and themes.colors(back) == themes.colors(mine)
    # an older file without them keeps working: every extra colour derived
    old = themes.import_theme(json.dumps({"name": "Old", "dark": True, "colors": {"accent": "#ff9f0a"}}), [])
    assert set(old["colors"]) == set(themes.COLOR_KEYS)
    assert themes.colors(old)["quality_hires"] == "#e0b84a" and themes.colors(old)["calendar"] == "#ff9f0a"
    # saved with the user's themes, and read from the themes folder
    path = str(tmp_path / "themes.json")
    themes.save_custom([mine], path)
    assert themes.load_custom(path) == [mine]
    folder = tmp_path / "themes"
    folder.mkdir()
    (folder / "neon.json").write_text(themes.export_theme(dict(mine, name="Neon")), encoding="utf-8")
    neon = themes.load_folder([mine], str(folder))[0]
    assert neon["name"] == "Neon" and themes.colors(neon)["verdict_fake_hires"] == "#0000ff"


def test_quality_kind():
    class T:
        def __init__(self, **kw):
            self.is_lossy, self.sample_rate, self.bits, self.is_float = False, 44100, 16, False
            self.__dict__.update(kw)
    assert themes.quality_kind(T()) == "lossless"
    assert themes.quality_kind(T(bits=24)) == themes.quality_kind(T(sample_rate=96000)) == "hires"
    assert themes.quality_kind(T(is_float=True, bits=32)) == "hires"
    assert themes.quality_kind(T(is_lossy=True, sample_rate=96000)) == "lossy"
    assert themes.quality_kind(T(bits=0)) == "lossless"  # unknown bits count as 16


def test_colour_labels_are_kept_in_player_json(tmp_path):
    st = playerstate.PlayerState()
    assert st.set_label("song", "/m/a.flac", "#FF3B30") == "#ff3b30"
    assert st.set_label("genre", "Indie Pop", "#34c759") == "#34c759"
    assert st.label_of("genre", "INDIE POP") == "#34c759"  # names in any case are one group
    assert st.label_of("song", "/M/A.FLAC") is None         # a path is not
    assert st.set_label("album", "city rain\x00the paper boats", "#007aff") == "#007aff"
    assert st.set_label("year", "2021", "not a colour") is None and st.label_of("year", "2021") is None
    assert st.set_label("nope", "x", "#ffffff") is None and st.set_label("song", "", "#ffffff") is None
    st.set_label("tab", "Hi-Res", "#c9a227")
    st.set_label("tab", "Hi-Res", None)
    assert "tab" not in st.labels
    path = str(tmp_path / "player.json")
    playerstate.save(st, path)
    data = json.load(open(path, encoding="utf-8"))
    assert data["labels"] == {"song": {"/m/a.flac": "#ff3b30"}, "album": {"city rain\x00the paper boats": "#007aff"},
                              "genre": {"indie pop": "#34c759"}}
    again = playerstate.load(path)
    assert again.labels == st.labels and again.label_of("genre", "indie pop") == "#34c759"
    # files of older versions have none
    assert playerstate.PlayerState.from_json({"liked": []}).labels == {}
    assert [name for name, _ in playerstate.LABEL_COLORS] == ["Red", "Orange", "Yellow", "Green", "Blue",
                                                              "Purple", "Grey"]


# ---------------------------------------------------------- the Tk player ---
@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_colours_in_the_player(tmp_path):
    lib = make_library(tmp_path, seconds=3, mp3=_has_mp3_encoder())
    from realhires import settings

    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.jobs = 2
    cfg.theme = "Light"
    settings.save(cfg, str(tmp_path / "settings.json"))
    env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
               REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
               REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
               REALHIRES_COVERS=str(tmp_path / "covers"), REALHIRES_AUDIO_BACKEND="null")
    body = f"""
LIB = {json.dumps(lib)}
import tkinter as tk
from realhires import player_gui, playerstate, themes, gui
from realhires.player_widgets import SongList

def fail(exc, val, tb):  # an assertion in a Tk callback must end the test, not hang it
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)

def items(canvas, text):
    return [i for i in canvas.find_all() if canvas.type(i) == "text" and canvas.itemcget(i, "text") == text]

def entry(menu, label):
    # the index of a menu entry by its label (menus are invoked, never posted: tk_popup is
    # modal on Windows and macOS)
    for i in range(menu.index("end") + 1):
        if menu.type(i) not in ("separator", "tearoff") and menu.entrycget(i, "label") == label:
            return i
    raise AssertionError(f"no {{label!r}} in the menu")

def choose(menu_fn, label):
    # open "Colour Label" of the menu menu_fn() makes and choose `label` in it
    m = menu_fn()
    entry(m, "Colour Label")
    sub = app.last_label_menu
    assert [sub.entrycget(i, "label") for i in range(sub.index("end") + 1) if sub.type(i) != "separator"] == \\
        ["Red", "Orange", "Yellow", "Green", "Blue", "Purple", "Grey", "Custom…", "None"]
    sub.invoke(entry(sub, label))
    root.update()

root = tk.Tk()
root.geometry("1024x768")  # the Windows and macOS runners' screens
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)
t0 = time.monotonic()
while app.library_busy() or not app.tracks:
    assert time.monotonic() - t0 < 60, "the library"
    root.update(); time.sleep(0.05)
t = app.theme

# the quality colours in the Quality column (Light: gold, the accent, the secondary text)
app.show_page("songs"); root.update()
table = app.page("songs").table
c = table.canvas
assert t.c("quality_hires") == "#c9a227" and t.c("quality_lossless") == "#fa2d48"
assert items(c, "Hi-Res Lossless") and all(c.itemcget(i, "fill") == "#c9a227" for i in items(c, "Hi-Res Lossless"))
assert items(c, "Lossless") and all(c.itemcget(i, "fill") == "#fa2d48" for i in items(c, "Lossless"))
for i in c.find_all():
    if c.type(i) == "text" and c.itemcget(i, "text").startswith("MP3 "):
        assert c.itemcget(i, "fill") == "#6e6e73"
# ... and on the playback bar's badge (outline and text)
glacier = next(x for x in app.tracks if x.path == LIB["songs"]["Glacier Song"])
app.play_list([glacier], 0, "Northern Lights"); root.update()
app.engine.pause()
lcd = app.bar.lcd
lcd.redraw(); root.update()
badge = lcd.find_withtag("badge")
assert len(badge) == 2, badge
assert {{lcd.itemcget(i, "outline" if lcd.type(i) == "polygon" else "fill") for i in badge}} == {{"#c9a227"}}
print("quality colours", flush=True)

# a colour label from the song menu: saved at once, a dot before the title
umbrella = next(x for x in app.tracks if x.path == LIB["songs"]["Umbrella Weather"])
choose(lambda: app.song_menu(umbrella), "Red")
assert playerstate.load().label_of("song", umbrella.path) == "#ff3b30"
dots = c.find_withtag("label")
assert dots and {{c.itemcget(i, "fill") for i in dots}} == {{"#ff3b30"}}
row = table.rows.index(umbrella)
title = next(i for i in c.find_withtag(f"r{{row}}") if c.type(i) == "text" and c.itemcget(i, "text") == "Umbrella Weather")
assert c.coords(dots[0])[2] < c.bbox(title)[0], "the dot before the title"
# Custom…: the colour chooser (injected: a test never opens the real one)
asked = []
app.color_chooser = lambda **kw: (asked.append(kw), ((18, 52, 86), "#123456"))[1]
choose(lambda: app.song_menu(umbrella), "Custom…")
assert asked and asked[0]["color"] == "#ff3b30"
assert app.state.label_of("song", umbrella.path) == "#123456"
assert {{c.itemcget(i, "fill") for i in c.find_withtag("label")}} == {{"#123456"}}
# Up Next shows it too
app.play_list([glacier, umbrella], 0, "Mix"); app.engine.pause(); root.update()
app.toggle_up_next(); root.update()
app.up_next.refresh(); root.update()
uc = app.up_next.list.canvas
assert app.up_next.winfo_ismapped()
assert {{uc.itemcget(i, "fill") for i in uc.find_withtag("label")}} == {{"#123456"}}
app.toggle_up_next(); root.update()
# None takes it away
choose(lambda: app.song_menu(umbrella), "None")
assert playerstate.load().label_of("song", umbrella.path) is None and not c.find_withtag("label")
print("song labels", flush=True)

# an album card's, a genre card's and a playlist's labels
album = next(a for a in app.albums if a["title"] == "Northern Lights")
from realhires import player_pages
choose(lambda: player_pages.album_menu(app, album), "Green")
assert app.state.label_of("album", album["key"]) == "#34c759"
app.show_page("albums"); root.update()
g = app.page("albums").grid_w
assert {{g.canvas.itemcget(i, "fill") for i in g.canvas.find_withtag("label")}} >= {{"#34c759"}}
choose(lambda: player_pages.group_menu(app, "genres", "Jazz"), "Purple")
assert playerstate.load().label_of("genre", "jazz") == "#af52de"
app.show_page("genres"); root.update()
gg = app.page("genres").grid_w
assert "#af52de" in {{gg.canvas.itemcget(i, "fill") for i in gg.canvas.find_withtag("label")}}
pid = app.state.create_playlist("Road Trip", [glacier.path]); app.save_state(); app.sidebar.redraw()
choose(lambda: app.playlist_menu(pid), "Orange")
assert playerstate.load().label_of("playlist", pid) == "#ff9500"
nav = app.sidebar.nav
dot = nav.find_withtag(f"label:playlist:{{pid}}")
name = next(i for i in nav.find_all() if nav.type(i) == "text" and nav.itemcget(i, "text") == "Road Trip")
assert dot and nav.itemcget(dot[-1], "fill") == "#ff9500" and nav.coords(dot[-1])[0] > nav.bbox(name)[2]
print("card and playlist labels", flush=True)

# More colours: an own theme; an extra colour set with the (injected) chooser reaches the
# widgets, the verdict colours and the calendar come from the theme, Reset makes it derived
name = app.new_theme()
app.show_page("settings", group="Appearance"); root.update()
editor = app.page("settings").theme_editor
assert set(editor.swatches) == set(themes.COLOR_KEYS + themes.EXTRA_COLOR_KEYS)
assert editor.values["row_hover"].cget("text") == "#efeff2 (automatic)" and not editor.resets
app.color_chooser = lambda **kw: ((0, 0, 0), "#00aa88")
editor._pick("quality_hires"); root.update()
assert themes.load_custom()[0]["colors"]["quality_hires"] == "#00aa88"
assert t.c("quality_hires") == "#00aa88"
lcd.redraw(); root.update()
assert {{lcd.itemcget(i, "outline" if lcd.type(i) == "polygon" else "fill") for i in lcd.find_withtag("badge")}} == {{"#00aa88"}}
editor = app.page("settings").theme_editor
assert editor.values["quality_hires"].cget("text") == "#00aa88" and set(editor.resets) == {{"quality_hires"}}
for key, value in (("row_hover", "#ffeedd"), ("verdict_fake_hires", "#0000ff"), ("calendar", "#00ff00")):
    app.set_theme_color(name, key, value)
assert t.c("hover") == "#ffeedd"  # the row under the pointer
assert gui.verdict_color(t, "FAKE HI-RES") == "#0000ff" and gui.verdict_color(t, "REAL HI-RES") == "#2e7d32"
qc = app.qc
assert qc.filter_checks["FAKE HI-RES"].color == "#0000ff"
# the calendar: days with plays tinted with the theme's colour
app.lib.add_play(glacier, time.time() - 120, 100.0)
app.show_page("stats"); root.update()
sp = app.page("stats")
sp._load_calendar() if hasattr(sp, "_load_calendar") else None
sp.area.redraw() if hasattr(sp.area, "redraw") else None
root.update()
base = t.c("lcd")
tints = {{themes.mix(base, "#00ff00", x) for x in sp.TINTS[1:]}}
fills = {{sp.area.c.itemcget(i, "fill") for i in sp.area.c.find_all() if sp.area.c.type(i) == "polygon"}}
assert fills & tints, (fills, tints)
# Reset: derived again
app.show_page("settings", group="Appearance"); root.update()
editor = app.page("settings").theme_editor
editor.resets["quality_hires"].command(); root.update()
assert "quality_hires" not in themes.load_custom()[0]["colors"] and t.c("quality_hires") == "#c9a227"
assert app.page("settings").theme_editor.values["quality_hires"].cget("text") == "#c9a227 (automatic)"
# the dark theme's gold
app.select_theme("Dark"); root.update()
assert t.c("quality_hires") == "#e0b84a" and t.c("verdict_fake_hires") == "#d32f2f"
app.select_theme(name); root.update()
print("more colours", flush=True)
root.destroy()
print("OK")
"""
    log = run_script(tmp_path, body, env)
    for step in ("quality colours", "song labels", "card and playlist labels", "more colours"):
        assert step in log, log
