"""Moods in the Tk player (2.4, docs/player-spec.md "4. Moods, tempo and playlists"):
the background analysis after the library update (its progress in the sidebar), the
Moods entry of the sidebar and the Library page, the Moods page's cards with their
counts and the note that the labels are estimates, a card's page with Create Playlist
(a taken name gets " 2"), the BPM and Mood columns, a tab on moods, Now Playing's
Details, a changed song analysed again, the setting turned off, and the texts in
Japanese.  The window is as small as on the 1024 x 768 CI runners.  Run in a
subprocess, as test_player_gui.py's tests (skipped without Tk or a display; locally:
xvfb-run -a python -m pytest)."""

import json
import os
import shutil
import subprocess

import pytest

import mood_songs
from test_player_gui import _gui_available, _has_miniaudio, run_script
from realhires import settings

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

# the invented songs of the library: (file title, bpm, progression, gain)
SONGS = [("Happy", 128, "major", 100), ("Sad", 72, "minor", 12), ("Intense", 150, "minor", 100),
         ("Chill", 84, "major", 12)]


def make_mood_library(root):
    music = root / "Music" / "Invented Band" / "Feelings"
    music.mkdir(parents=True)
    out = {}
    for n, (title, bpm, prog, gain) in enumerate(SONGS, start=1):
        wav = root / f"{title}.wav"
        mood_songs.write_wav(str(wav), mood_songs.song(bpm, prog, 6, sr=44100, gain=gain), sr=44100, channels=2)
        path = music / f"{n:02d} {title}.flac"
        meta = []
        for k, v in (("title", title), ("artist", "Invented Band"), ("album", "Feelings"), ("track", str(n))):
            meta += ["-metadata", f"{k}={v}"]
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav)] + meta + [str(path)], check=True)
        wav.unlink()
        out[title] = str(path)
    return str(root / "Music"), out


def _env(tmp_path, music, **values):
    cfg = settings.Settings()
    cfg.library_folders = [music]
    cfg.theme = "Light"
    for k, v in values.items():
        setattr(cfg, k, v)
    settings.save(cfg, str(tmp_path / "settings.json"))
    return dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
                REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
                REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
                REALHIRES_COVERS=str(tmp_path / "covers"), REALHIRES_TABS=str(tmp_path / "tabs"),
                REALHIRES_AUDIO_BACKEND="null")


HELPERS = """
import tkinter as tk
from realhires import moods, player_gui, player_pages, player_moods, settings, smarttabs
from realhires.i18n import tr
player_gui.messagebox.askyesno = lambda *a, **k: True
player_gui.messagebox.showinfo = lambda *a, **k: None

def fail(exc, val, tb):
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)

def wait(cond, then, timeout=90.0, what="", start=None):
    start = start or time.monotonic()
    if cond():
        return root.after(50, then)
    assert time.monotonic() - start < timeout, "timed out: " + what
    root.after(100, lambda: wait(cond, then, timeout, what, start))

def texts(canvas):
    return [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]

def click(canvas, box):
    x, y = (box[0] + box[2]) / 2 - canvas.canvasx(0), (box[1] + box[3]) / 2 - canvas.canvasy(0)
    canvas.event_generate("<Motion>", x=int(x), y=int(y))
    canvas.event_generate("<Button-1>", x=int(x), y=int(y))
    root.update()

def press(button):
    button.event_generate("<ButtonRelease-1>", x=5, y=5)
    root.update()

root = tk.Tk()
root.geometry("1024x740+0+0")  # the CI runners' 1024 x 768 screens
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)
"""


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_moods_in_the_player(tmp_path):
    music, songs = make_mood_library(tmp_path)
    env = _env(tmp_path, music)
    body = f"""
S = {json.dumps(songs)}
""" + HELPERS + """
status = []

def watch():  # the sidebar's status while the job runs
    status.append(app.sidebar.status.cget("text"))
    if app.moods_busy() or not app.moods:
        root.after(30, watch)

def analysed():
    print("analysed", {os.path.basename(p): (m.bpm, m.mood, m.tempo) for p, m in app.moods.items()}, flush=True)
    assert any(s.startswith("Analysing moods: ") and s.endswith(" of 4 songs") for s in status), status
    assert app.sidebar.status.cget("text") == ""  # cleared when the job ends
    assert app.mood_last == (4, 0)
    want = {"Happy": ("Happy", "Up-tempo"), "Sad": ("Sad", "Slow"), "Intense": ("Intense", "Up-tempo"),
            "Chill": ("Chill", "Slow")}
    for title, (mood, tempo) in want.items():
        m = app.moods[S[title]]
        assert (m.mood, m.tempo) == (mood, tempo), (title, m)
        assert app.by_path[S[title]].mood is m  # the track carries it, for the columns and Details
    # the sidebar's Library list and the Library page have a Moods entry
    keys = [k for k, _, _ in app.sidebar.items()]
    assert keys.index("moods") == keys.index("composers") + 1, keys
    page = app.page("library")
    app.show_page("library")
    root.update()
    entries = {e[0]: e for e in page.entries()}
    assert entries["moods"][1:4] == ("Moods", "mood", "4 songs analysed"), entries["moods"]
    assert "4 songs analysed" in texts(page.c)
    click(page.c, next(box for box, key in page.entry_hits if key == "moods"))
    assert app.page_name == "moods" and app.sidebar.selected == "moods"
    moods_page()

def moods_page():
    page = app.page("moods")
    root.update()
    shown = texts(page.c)
    assert "Moods" in shown and "Tempo" in shown and "4 of 4 songs analysed" in shown, shown
    assert tr(moods.ESTIMATE_NOTE) in shown
    assert set(page.card_hits) == {label for _, label in moods.CARDS}
    counts = app.mood_counts()
    assert counts == {"Happy": 1, "Sad": 1, "Chill": 1, "Intense": 1, "Up-tempo": 2, "Mid-tempo": 0, "Slow": 2}
    assert shown.count("1 song") == 4 and shown.count("2 songs") == 2 and "0 songs" in shown, shown
    W = page.c.winfo_width()
    assert all(b[2] <= W - page.MARGIN + 1 for b in page.card_hits.values()), (W, page.card_hits)  # no card cut off
    click(page.c, page.card_hits["Up-tempo"])
    card_page()

def card_page():
    page = app.page("mood")
    root.update()
    assert app.page_name == "mood" and (page.kind, page.value) == ("tempo", "Up-tempo")
    assert [r.path for r in page.table.rows] == [S["Happy"], S["Intense"]]
    assert page.title_l.cget("text") == "Up-tempo" and page.sub.cget("text").startswith("2 songs")
    assert app.sidebar.selected == "moods" and app.page_label("mood", app._page_args()) == "Up-tempo"
    assert page.note.cget("text") == moods.ESTIMATE_NOTE
    # Create Playlist: an ordinary playlist, named after the card; again: " 2"
    assert page.create_b.winfo_ismapped() and page.create_b.winfo_rootx() + page.create_b.winfo_width() <= (
        root.winfo_rootx() + root.winfo_width())
    press(page.create_b)
    names = [p["name"] for p in app.state.playlists]
    assert names == ["Up-tempo songs"], names
    assert app.state.playlists[0]["songs"] == [S["Happy"], S["Intense"]]
    assert page.made.cget("text") == "Made the playlist “Up-tempo songs” with 2 songs."
    assert page.made.cget("text") == app.sidebar.status.cget("text")
    press(page.create_b)
    assert [p["name"] for p in app.state.playlists] == ["Up-tempo songs", "Up-tempo songs 2"]
    assert any(k == f"playlist:{app.state.playlists[1]['id']}" for k, _, _ in app.sidebar.items())
    # the BPM and Mood columns, chosen as the other columns
    m = player_pages.columns_menu(app, "songs")
    labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) == "checkbutton"]
    assert "BPM (estimated)" in labels and "Mood (estimated)" in labels, labels
    player_pages.set_columns(app, "songs", app.state.set_columns("songs", app.state.columns_of("songs") + ["bpm", "mood"]))
    root.update()
    keys = [c.key for c in page.table.columns]
    assert keys == ["title", "artist", "album", "genre", "time", "quality", "bpm", "mood", "like"], keys
    bpm = app.moods[S["Happy"]].bpm
    assert player_pages.track_value(app.by_path[S["Happy"]], "bpm") == str(bpm) and abs(bpm - 128) <= 2
    assert player_pages.track_value(app.by_path[S["Intense"]], "mood") == "Intense"
    tip = player_pages.track_tip(app.by_path[S["Happy"]], "mood")
    assert tip.startswith("Estimated from the audio: Happy · Energetic · ") and "C major" in tip, tip
    shown = texts(page.table.canvas)
    assert str(bpm) in shown and "Intense" in shown and "BPM" in shown, shown
    # sorting by BPM: the slower first
    page.table.sort_by("bpm")
    assert [r.path for r in page.table.rows] == [S["Happy"], S["Intense"]]
    page.table.sort_by("bpm")
    assert [r.path for r in page.table.rows] == [S["Intense"], S["Happy"]]
    tab()

def tab():
    # a tab on moods: Mood is Sad or tempo is Up-tempo
    smarttabs.save_tab(dict(smarttabs.new_tab("Moody"), match="any", sort="bpm", rules=[
        {"field": "mood", "op": "is", "value": "Sad"}, {"field": "tempo", "op": "is", "value": "Up-tempo"}]))
    app.reload_tabs()
    got = [t.path for t in app.tab_tracks("Moody")]
    assert got == [S["Sad"], S["Happy"], S["Intense"]], got  # sorted by BPM
    details()

def details():
    app.play_list(app.mood_tracks("mood", "Happy"), 0, "Happy")
    app.show_page("nowplaying")
    app.set_np_tab("details")
    root.update()
    text = app.page("nowplaying").details.text()
    assert "Mood (estimated): Happy, Energetic" in text, text
    assert f"Tempo (estimated): {app.moods[S['Happy']].bpm} BPM, Up-tempo" in text, text
    assert "Key (estimated): C major" in text, text
    app.engine.pause()
    changed()

def changed():
    # a song changed on disk is analysed again after the library update, the others not
    st = os.stat(S["Chill"])
    os.utime(S["Chill"], ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    app.mood_last = None
    app.refresh_library()
    wait(lambda: app.mood_last is not None and not app.moods_busy() and not app.library_busy(), turned_off,
         what="the analysis of the changed song")

def turned_off():
    assert app.mood_last == (1, 0), app.mood_last
    new = settings.Settings.from_dict(app.cfg.to_dict())
    new.analyse_moods = False
    app.apply_settings(new)
    assert settings.load().analyse_moods is False
    app.show_page("moods")
    root.update()
    shown = texts(app.page("moods").c)
    assert "4 of 4 songs analysed. Analysing is turned off in Settings → Library." in shown, shown
    assert app.analyse_moods() is False and app.analyse_moods(force=True) is True
    wait(lambda: not app.moods_busy(), done, what="a run by hand")

def done():
    assert app.mood_last == (0, 0)  # nothing left to analyse
    print("OK")
    app.quit()

root.after(30, watch)
wait(lambda: len(app.moods) == 4 and not app.moods_busy() and not app.library_busy() and app.mood_last is not None,
     analysed, what="the mood analysis")
root.mainloop()
"""
    run_script(tmp_path, body, env)
    # the same library in Japanese: the table answers at once, nothing is analysed again
    env = _env(tmp_path, music, language="日本語")
    body = HELPERS + """
def check():
    assert app.mood_last == (0, 0), app.mood_last
    items = [text for _, text, _ in app.sidebar.items()]
    assert "ムード" in items, items
    app.show_page("moods")
    root.update()
    shown = texts(app.page("moods").c)
    for want in ("ムード", "テンポ", "楽しい", "悲しい", "チル", "激しい", "アップテンポ", "ミドルテンポ", "スロー",
                 "4曲中4曲を解析済み", "長調・にぎやか", "90 BPM未満"):
        assert want in shown, (want, shown)
    assert any("推定" in s for s in shown), shown
    app.open_mood("mood", "Sad")
    root.update()
    page = app.page("mood")
    assert page.title_l.cget("text") == "悲しい" and page.create_b.text == "プレイリストを作成"
    press(page.create_b)
    assert app.state.playlists[-1]["name"] == "悲しい曲", app.state.playlists[-1]
    assert page.made.cget("text") == "1曲のプレイリスト「悲しい曲」を作成しました。", page.made.cget("text")
    app.play_list(page.table.rows, 0, page.list_name)
    app.show_page("nowplaying")
    app.set_np_tab("details")
    root.update()
    text = app.page("nowplaying").details.text()
    assert "ムード（推定）: 悲しい、穏やか" in text and "調（推定）: イ短調" in text, text
    app.engine.pause()
    print("OK")
    app.quit()

wait(lambda: app.tracks and not app.library_busy() and not app.moods_busy() and app.mood_last is not None, check,
     what="the library")
root.mainloop()
"""
    run_script(tmp_path, body, env)
