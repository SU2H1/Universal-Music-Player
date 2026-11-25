"""Custom tabs in the Tk player (2.3), headless in a subprocess, at the size of the CI
runners' screens (1024 x 768): the tabs folder read at start (with a file that is not a
tab), the sidebar's Tabs section and the Library page's entries, a tab's page and its
"…" menu, Now Playing's back link naming the tab, New Tab… with a template and with
rules made by hand (the live count, check errors that keep the window open, a taken
name), Edit Tab… renaming the tab and its file, a tab on likes, Export Tab…, Delete
Tab…, Import tab…, files copied into the folder appearing when the Library page is
shown, Settings → Library, and the same in Japanese."""

import json
import os
import shutil

import pytest

from player_library import make_library
from realhires import smarttabs
from test_player_gui import _gui_available, _has_miniaudio, _has_mp3_encoder, run_script

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _env(tmp_path, lib, language="English"):
    from realhires import settings

    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.theme = "Light"
    cfg.language = language
    settings.save(cfg, str(tmp_path / "settings.json"))
    tabs = tmp_path / "tabs"
    tabs.mkdir()
    (tabs / "Hi-Res.json").write_text(smarttabs.tab_text(smarttabs.TEMPLATES[0]), encoding="utf-8")
    (tabs / "notes.json").write_text("{not a tab", encoding="utf-8")
    return dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
                REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
                REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
                REALHIRES_COVERS=str(tmp_path / "covers"), REALHIRES_TABS=str(tabs), REALHIRES_AUDIO_BACKEND="null")


COMMON = """
import tkinter as tk
from realhires import player_gui, player_tabs, smarttabs, i18n
from realhires.player_tabs import TabPage, TabWindow
player_tabs.messagebox.askyesno = lambda *a, **k: True  # confirmations say yes
errors = []
player_tabs.messagebox.showerror = lambda *a, **k: errors.append(a)

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

def side_click(key, button="<Button-1>"):
    # a click on a sidebar row, where the sidebar drew it
    sb = app.sidebar
    for k, _, _, y, h in sb._rows():
        if k == key:
            sb.nav.yview_moveto(0)
            root.update()
            top = sb.nav.canvasy(0)
            if not (top <= y and y + h <= top + sb.nav.winfo_height()):  # scrolled away: bring it in
                sb.nav.yview_moveto(max(0.0, (y - 40) / float(sb.nav.cget("scrollregion").split()[3])))
                root.update()
            sb.nav.event_generate(button, x=60, y=int(y + h / 2 - sb.nav.canvasy(0)))
            root.update()
            return
    raise AssertionError(f"no sidebar row {key}: {[r[0] for r in sb._rows()]}")

def start():
    global root, app
    root = tk.Tk()
    root.geometry("1024x740+0+0")
    root.report_callback_exception = fail
    app = player_gui.PlayerApp(root)
    return root, app
"""


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_custom_tabs_in_the_player(tmp_path):
    mp3 = _has_mp3_encoder()
    lib = make_library(tmp_path, seconds=2, mp3=mp3)
    env = _env(tmp_path, lib)
    tabs = str(tmp_path / "tabs")
    body = f"""
LIB = {json.dumps(lib)}
TABS = {tabs!r}
OUT = {str(tmp_path)!r}
MP3 = {mp3!r}
HIRES = ["Blue Kettle", "First Light", "Glacier Song", "Midnight Sun", "Polar Drift", "Porch Lamp"]
{COMMON}

def loaded():
    assert len(app.tracks) == len(LIB["songs"]), len(app.tracks)
    # the folder was read at start: the tab, and the file that is not one
    assert [t["name"] for t in app.tabs] == ["Hi-Res"], app.tabs
    assert app.tab_problems == [("notes.json", "This file is not a tab of Universal Music Player.")]
    items = app.sidebar.items()
    keys = [k for k, _, _ in items]
    assert ("tab:Hi-Res", "Hi-Res", "filter") in items and "new_tab" in keys
    heads = [t for k, t, _ in items if k == "head"]
    assert heads == ["Library", "Playlists", "Tabs", "History"], heads
    assert keys.index("new_playlist") < keys.index("tab:Hi-Res") < keys.index("new_tab") < keys.index("played")
    # the Library page lists the tab after its fourteen entries, with its songs
    page = app.page("library")
    entries = page.entries()
    assert len(entries) == 15 and entries[-1][:4] == ("tab:Hi-Res", "Hi-Res", "filter", "6 songs"), entries[-1]
    assert "Hi-Res" in texts(page.c) and "6 songs" in texts(page.c)
    # a click on the sidebar's tab opens its page
    side_click("tab:Hi-Res")
    tp = app.page("tab")
    assert app.page_name == "tab" and isinstance(tp, TabPage) and app.sidebar.selected == "tab:Hi-Res"
    assert tp.title_l.cget("text") == "Hi-Res" and tp.sub.cget("text") == "6 songs, 0 minutes", tp.sub.cget("text")
    assert [t.display_title for t in tp.table.rows] == HIRES
    m = tp.menu()
    assert [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) != "separator"] == [
        "Edit Tab…", "Export Tab…", "Delete Tab…", "Colour Label"]
    # play from the tab: Now Playing's back link names it
    tp.table.on_activate(1)
    wait(lambda: app.current == LIB["songs"]["First Light"], playing, what="playback")

def playing():
    assert app.context_name == "Hi-Res"
    app.toggle_now_playing()
    root.update()
    assert app.page_name == "nowplaying" and app.back_label() == "Hi-Res", app.back_label()
    app.play_pause()
    app.go_back()
    assert app.page_name == "tab" and app.page("tab").name == "Hi-Res"
    # New Tab…: from the sidebar, then a template
    side_click("new_tab")
    w = app.tab_window
    assert isinstance(w, TabWindow) and w.win.title() == "New Tab" and w.name_var.get() == "New Tab"
    assert w.template_cb is not None and w.rows == []
    w.template_cb.current(2)  # Lossy
    w.template_cb.event_generate("<<ComboboxSelected>>")
    root.update()
    assert w.name_var.get() == "Lossy" and [r.rule() for r in w.rows] == [{{"field": "lossless", "op": "is",
                                                                          "value": False}}]
    want = "2 songs match" if MP3 else "0 songs match"
    wait(lambda: w.count.cget("text") == want, lossy, what=want)

def lossy():
    w = app.tab_window
    # rules by hand: genre is Jazz, or genre is Ambient
    w.template_cb.current(0)
    w.template_cb.event_generate("<<ComboboxSelected>>")
    root.update()
    w.name_var.set("Jazz and Ambient")
    w.add_rule()
    w.add_rule()
    assert len(w.rows) == 2 and w.rows[0].rule() == {{"field": "title", "op": "contains", "value": ""}}
    for i, genre in enumerate(("jazz", "ambient")):
        row = w.rows[i]
        row.field_cb.current(smarttabs.FIELDS.index("genre"))
        row.field_cb.event_generate("<<ComboboxSelected>>")
        root.update()
        row = w.rows[i]
        row.op_cb.current(0)  # is
        row.value_var.set(genre)
    w.match_cb.current(1)  # any
    w.match_cb.event_generate("<<ComboboxSelected>>")
    root.update()
    wait(lambda: w.count.cget("text") == "6 songs match", by_hand, what="6 songs match")

def by_hand():
    w = app.tab_window
    # a value of the wrong kind and a taken name are refused, and the window stays open
    w.add_rule()
    row = w.rows[2]
    row.field_cb.current(smarttabs.FIELDS.index("year"))
    row.field_cb.event_generate("<<ComboboxSelected>>")
    root.update()
    w.rows[2].value_var.set("two thousand")
    w.save()
    assert app.tab_window is w and w.error.cget("text") == "Rule 3: the value of “Year” must be a number."
    w.remove_rule(w.rows[2])
    w.name_var.set("hi-res")
    w.save()
    assert app.tab_window is w and w.error.cget("text") == "You already have a tab called “hi-res”."
    # more rules than fit: the list scrolls, and the window still fits the screen
    for _ in range(9):
        w.add_rule()
    root.update()
    assert w.rules_sb.winfo_ismapped()
    assert w.win.winfo_reqheight() <= 740 and w.win.winfo_reqwidth() <= 1024, (w.win.winfo_reqwidth(),
                                                                                 w.win.winfo_reqheight())
    for _ in range(9):
        w.remove_rule(w.rows[-1])
    w.name_var.set("Jazz and Ambient")
    w.save()
    assert app.tab_window is None
    assert sorted(os.listdir(TABS)) == ["Hi-Res.json", "Jazz and Ambient.json", "notes.json"]
    saved = smarttabs.parse_tab(open(os.path.join(TABS, "Jazz and Ambient.json"), encoding="utf-8").read())
    assert saved["match"] == "any" and [r["value"] for r in saved["rules"]] == ["jazz", "ambient"]
    # the new tab's page is shown, and the sidebar lists it
    tp = app.page("tab")
    assert app.page_name == "tab" and tp.name == "Jazz and Ambient" and len(tp.table.rows) == 6
    assert "tab:Jazz and Ambient" in [k for k, _, _ in app.sidebar.items()]
    # Edit Tab…: renamed, with only Jazz; the file follows the name, the page and history too
    app.show_page("library")
    app.open_tab("Jazz and Ambient")
    app.set_label("tab", "Jazz and Ambient", "#34c759")  # a colour label, drawn after the name
    assert app.sidebar.label_of("tab:Jazz and Ambient") == "#34c759"
    w = app.edit_tab("Jazz and Ambient")
    assert w.win.title() == "Edit Tab" and w.template_cb is None and w.name_var.get() == "Jazz and Ambient"
    w.remove_rule(w.rows[1])
    w.name_var.set("Jazz")
    w.save()
    assert sorted(os.listdir(TABS)) == ["Hi-Res.json", "Jazz.json", "notes.json"]
    assert app.page("tab").name == "Jazz" and app.page("tab").title_l.cget("text") == "Jazz"
    assert [t.display_title for t in app.page("tab").table.rows] == ["Blue Kettle", "Porch Lamp"]
    assert app.history[-1] == ("library", {{}}) and app.sidebar.selected == "tab:Jazz"
    # the colour label follows the new name
    assert app.label_of("tab", "Jazz") == "#34c759" and app.label_of("tab", "Jazz and Ambient") is None
    assert app.sidebar.label_of("tab:Jazz") == "#34c759"
    # a tab on likes follows them
    w = app.new_tab()
    w.name_var.set("Loved")
    w.add_rule()
    row = w.rows[0]
    row.field_cb.current(smarttabs.FIELDS.index("liked"))
    row.field_cb.event_generate("<<ComboboxSelected>>")
    root.update()
    assert w.rows[0].rule() == {{"field": "liked", "op": "is", "value": True}}
    w.save()
    assert app.page("tab").name == "Loved" and app.page("tab").table.rows == []
    app.toggle_like(LIB["songs"]["Porch Lamp"])
    assert [t.display_title for t in app.page("tab").table.rows] == ["Porch Lamp"]
    assert ("tab:Loved", "Loved", "filter", "1 song") == app.page("library").entries()[-1][:4]
    # Export Tab…, Delete Tab…, Import tab…
    out = app.export_tab_file("Jazz", path=os.path.join(OUT, "shared.json"))
    assert open(out, encoding="utf-8").read() == smarttabs.tab_text(app.tab("Jazz"))
    assert app.delete_tab("Jazz")
    assert "Jazz.json" not in os.listdir(TABS) and app.tab("Jazz") is None
    assert app.label_of("tab", "Jazz") is None  # its label went with it
    assert "tab:Jazz" not in [k for k, _, _ in app.sidebar.items()]
    assert app.import_tab_file(out) == "Jazz" and app.page_name == "tab" and app.page("tab").name == "Jazz"
    assert app.import_tab_file(os.path.join(TABS, "Jazz.json")) is None
    assert errors and errors[-1][1] == "This tab is in the tabs folder already.", errors
    # a file copied into the folder appears when the Library page is shown
    with open(os.path.join(TABS, "Short.json"), "w", encoding="utf-8") as fh:
        fh.write(smarttabs.tab_text(dict(smarttabs.new_tab("Short"), rules=[{{"field": "duration", "op": "<", "value": 60}}])))
    app.show_page("library")
    assert app.tab("Short") is not None and "tab:Short" in [k for k, _, _ in app.sidebar.items()]
    assert app.page("library").entries()[-1][:4] == ("tab:Short", "Short", "filter", f"{{len(LIB['songs'])}} songs")
    # Settings → Library: Import tab…, Open tabs folder, and the file that is not a tab
    app.show_page("settings", group="Library")
    root.update()
    sp = app.page("settings")
    assert "notes.json" in sp.tab_note.cget("text") and sp.tab_note.winfo_ismapped()
    opened = []
    assert app.open_tabs_folder(opener=opened.append) == TABS and opened == [TABS]
    # the sidebar's right click (menus are posted only on Linux: tk_popup waits on Windows and macOS)
    app.show_page("library")
    if sys.platform.startswith("linux"):
        side_click("tab:Short", "<Button-3>")
        m = app.last_menu
        assert [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) != "separator"] == [
            "Edit Tab…", "Export Tab…", "Delete Tab…", "Colour Label"]
        m.unpost()
    print("OK")
    root.destroy()

start()
wait(lambda: not app.library_busy() and len(app.tracks) == len(LIB["songs"]), loaded, what="the library")
root.mainloop()
"""
    run_script(tmp_path, body, env)


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_custom_tabs_in_japanese(tmp_path):
    lib = make_library(tmp_path, seconds=2, mp3=False)
    env = _env(tmp_path, lib, language="日本語")
    body = f"""
LIB = {json.dumps(lib)}
{COMMON}

def loaded():
    heads = [t for k, t, _ in app.sidebar.items() if k == "head"]
    assert heads == ["ライブラリ", "プレイリスト", "タブ", "履歴"], heads
    assert ("new_tab", "新規タブ…", "plus") in app.sidebar.items()
    assert app.page("library").entries()[-1][1:4] == ("Hi-Res", "filter", "6曲")
    app.open_tab("Hi-Res")
    m = app.page("tab").menu()
    assert [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) != "separator"] == [
        "タブを編集…", "タブを書き出す…", "タブを削除…", "カラーラベル"]
    w = app.new_tab()
    assert w.win.title() == "新規タブ" and w.name_var.get() == "新規タブ"
    w.template_cb.current(1)
    w.template_cb.event_generate("<<ComboboxSelected>>")
    root.update()
    assert w.name_var.get() == "ハイレゾ" and w.rows[0].field_var.get() == "音質"
    assert w.rows[0].op_var.get() == "が次と一致" and w.rows[0].value_var.get() == "ハイレゾロスレス"
    w.add_rule()
    w.rows[1].field_cb.current(smarttabs.FIELDS.index("year"))
    w.rows[1].field_cb.event_generate("<<ComboboxSelected>>")
    root.update()
    w.rows[1].value_var.set("abc")
    w.save()
    assert w.error.cget("text") == "ルール2：「年」の値は数値にしてください。", w.error.cget("text")
    w.rows[1].value_var.set("2019")
    wait(lambda: w.count.cget("text") == "4曲が一致", lambda: saved(w), what="the count")

def saved(w):
    root.update()
    assert w.win.winfo_reqwidth() <= 1024, w.win.winfo_reqwidth()
    w.save()
    assert app.page_name == "tab" and app.page("tab").name == "ハイレゾ"
    assert app.page("tab").sub.cget("text") == "4曲、0分", app.page("tab").sub.cget("text")
    app.show_page("settings", group="Library")
    root.update()
    assert "notes.json" in app.page("settings").tab_note.cget("text")
    print("OK")
    root.destroy()

start()
wait(lambda: not app.library_busy() and len(app.tracks) == len(LIB["songs"]), loaded, what="the library")
root.mainloop()
"""
    run_script(tmp_path, body, env)
