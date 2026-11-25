"""2.4 (docs/player-spec.md, "2.4" items 1 and 2): the Folders page scrolls as one page,
the names on Your Stats are links, and the leaderboard ("Most Played").  The core's
leaderboard is checked here and against the Rust vectors; the Tk pages run in a
subprocess, as test_player_gui.py's do (skipped without Tk or a display; locally:
xvfb-run -a python -m pytest)."""

import json
import os
import shutil
from pathlib import Path

import pytest

from player_library import make_library
from test_player_gui import _gui_available, _has_miniaudio, run_script
from realhires import stats
from realhires.stats import Play

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
VECTORS = Path(__file__).resolve().parents[1] / "rust" / "crates" / "core" / "tests" / "python_leaderboard.json"


def _play(path, started, seconds, title="", artist="", album="", album_artist="", duration=200.0):
    return Play(path, started, seconds, duration, title, artist, album, album_artist)


def test_leaderboard_ranks_by_plays_then_minutes_and_ties_share_a_rank():
    plays = [
        _play("/m/a.flac", 1, 60, "Alpha", "Ann", "One"), _play("/m/a.flac", 2, 60, "Alpha", "Ann", "One"),
        _play("/m/b.flac", 3, 120, "Bravo", "Ann", "One"),  # 1 play, 2 min
        _play("/m/c.flac", 4, 119.6, "charlie", "Bob", "Two"),  # 1 play, 2 min (rounded): shares the rank
        _play("/m/d.flac", 5, 300, "Delta", "Bob", "Two"),  # 1 play, 5 min: before the 2-minute ones
        _play("/m/e.flac", 6, 10, "Echo", "Cy", "Three"),  # too short: not a play
        _play("/m/e.flac", 7, 12, "Echo", "Cy", "Three"),
        _play("/m/f.flac", 8, 45, " ", " ", "", album_artist="Dee"),  # no title: the file's name
    ]
    songs = stats.leaderboard(plays, "songs")
    assert [(r["rank"], r["name"], r["plays"], r["minutes"]) for r in songs] == [
        (1, "Alpha", 2, 2), (2, "Delta", 1, 5), (3, "Bravo", 1, 2), (3, "charlie", 1, 2), (5, "f", 1, 1)]
    assert songs[0]["key"] == songs[0]["paths"][0] == "/m/a.flac" and songs[0]["artist"] == "Ann"
    assert songs[4]["artist"] == "Dee" and songs[4]["seconds"] == 45
    artists = stats.leaderboard(plays, "artists")
    assert [(r["rank"], r["name"], r["plays"], r["minutes"], r["artist"]) for r in artists] == [
        (1, "Ann", 3, 4, ""), (2, "Bob", 2, 7, ""), (3, "Dee", 1, 1, "")]
    assert artists[0]["paths"] == ["/m/a.flac", "/m/b.flac"]  # the most played first
    assert artists[1]["paths"] == ["/m/d.flac", "/m/c.flac"]  # one play each: the longer first
    albums = stats.leaderboard(plays, "albums")
    assert [(r["rank"], r["name"], r["artist"]) for r in albums] == [
        (1, "One", "Ann"), (2, "Two", "Bob"), (3, "Unknown Album", "Dee")]
    assert albums[0]["key"] == "one\x00ann"
    # up to `top` rows; Play and Shuffle take the songs in rank order, each once
    assert len(stats.leaderboard(plays, "songs", top=2)) == 2 and stats.leaderboard(plays, "songs", top=0) == []
    assert stats.ranked_songs(artists) == ["/m/a.flac", "/m/b.flac", "/m/d.flac", "/m/c.flac", "/m/f.flac"]
    assert stats.ranked_songs(songs + songs) == [r["key"] for r in songs]
    assert stats.leaderboard([], "albums") == []
    with pytest.raises(ValueError):
        stats.leaderboard(plays, "genres")


def test_leaderboard_keeps_a_hundred_rows():
    # two songs each with the same minutes: they share a rank, by name
    plays = [_play(f"/m/{i:03d}.flac", i, 31 + 60 * (i // 2), f"Song {i:03d}", "Ann", "One") for i in range(130)]
    rows = stats.leaderboard(plays, "songs")
    assert len(rows) == stats.TOP == 100
    assert [(r["rank"], r["name"]) for r in rows[:3]] == [(1, "Song 128"), (1, "Song 129"), (3, "Song 126")]
    assert rows[-1]["rank"] == 99


def test_rust_vectors_hold_pythons_answers():
    """python_leaderboard.json is what rust/tools/gen_player_vectors.py writes from this
    module; the Rust core is compared with it in leaderboard_vectors.rs."""
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    plays = [Play(**p) for p in data["plays"]]
    assert data["kinds"] == stats.LEADERBOARD_KINDS
    for kind in stats.LEADERBOARD_KINDS:
        for top, rows in data["boards"][kind].items():
            assert stats.leaderboard(plays, kind, int(top)) == rows, (kind, top)
        assert stats.ranked_songs(data["boards"][kind][str(stats.TOP)]) == data["ranked"][kind]
    ranks = [r["rank"] for k in stats.LEADERBOARD_KINDS for r in data["boards"][k]["100"]]
    assert len(ranks) != len(set(ranks)), "the vectors hold ties"


# ------------------------------------------------------------------ the Tk player ---
def _many_folders(lib: dict) -> str:
    """A folder of 25 subfolders (a song each) and 30 songs of its own, copied from the
    invented library (the copies keep the tags)."""
    many = Path(lib["music"]) / "Many"
    src = lib["songs"]["Porch Lamp"]
    for i in range(25):
        d = many / f"Sub {i:02d}"
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, d / "song.flac")
    for i in range(30):
        shutil.copy(lib["songs"]["Low Pass"], many / f"{i:02d} direct.flac")
    return str(many.resolve())


def _env(tmp_path, lib, **cfg_values):
    from realhires import settings

    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.theme = "Light"
    for k, v in cfg_values.items():
        setattr(cfg, k, v)
    settings.save(cfg, str(tmp_path / "settings.json"))
    return dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
                REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
                REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
                REALHIRES_COVERS=str(tmp_path / "covers"), REALHIRES_TABS=str(tmp_path / "tabs"),
                REALHIRES_AUDIO_BACKEND="null")


HELPERS = """
import tkinter as tk
from realhires import player_gui, player_collections, player_stats, stats
from realhires.player_theme import PillButton
state = {}

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

X11 = None

def wheel(widget, steps, x=60, y=60):  # mouse-wheel steps down (+) or up (-), as each system reports them
    for _ in range(abs(steps)):
        if X11:
            widget.event_generate("<Button-5>" if steps > 0 else "<Button-4>", x=x, y=y)
        else:
            widget.event_generate("<MouseWheel>", delta=-120 if steps > 0 else 120, x=x, y=y)
        root.update()

def same(shown, text):  # a text as drawn, perhaps cut with an ellipsis to fit
    return shown == text or (shown.endswith("…") and len(shown) > 3 and text.startswith(shown[:-1]))

def items(c, text):  # the canvas texts `text`: (id, bbox), top to bottom
    out = [(i, c.bbox(i)) for i in c.find_all() if c.type(i) == "text" and same(c.itemcget(i, "text"), text)]
    return sorted(out, key=lambda x: x[1][1])

def show(c, box):  # scroll a ScrollCanvas so that the box is in view
    region = [float(v) for v in str(c.cget("scrollregion")).split()]
    if len(region) == 4 and region[3] > 0:
        c.yview_moveto(max(0.0, (box[1] - 120) / region[3]))
    root.update()

def press_at(c, box, seq="<Button-1>"):  # a click (or right click) in the middle of a box drawn on a canvas
    x, y = (box[0] + box[2]) / 2 - c.canvasx(0), (box[1] + box[3]) / 2 - c.canvasy(0)
    c.event_generate("<Motion>", x=int(x), y=int(y))
    c.event_generate(seq, x=int(x), y=int(y), rootx=300, rooty=300)
    root.update()

def click_text(c, text, pick=lambda b: True, seq="<Button-1>", area=None):
    found = [b for _, b in items(c, text) if pick(b)]
    assert found, (text, sorted({c.itemcget(i, "text") for i in c.find_all() if c.type(i) == "text"}))
    if area is not None:
        show(c, found[0])
        found = [b for _, b in items(c, text) if pick(b)]
    press_at(c, found[0], seq)

def press(button):  # a PillButton or IconButton, as a click releases it
    button.event_generate("<ButtonRelease-1>", x=5, y=5)
    root.update()

def labels(m):
    return [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) != "separator"]

def unpost(m):  # an unposted pop-up menu may still hold X11's grab, which takes the next events
    m.unpost()
    root.update()
    if root.grab_current() is not None:
        root.grab_current().grab_release()

root = tk.Tk()
root.geometry("1024x768")  # the CI runners' screens
root.report_callback_exception = fail
X11 = root.tk.call("tk", "windowingsystem") == "x11"
app = player_gui.PlayerApp(root)
"""


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_folders_scroll_as_one_list_and_stats_links_and_leaderboard(tmp_path):
    """The Folders page: the subfolders and the songs in one list that scrolls with the
    wheel (MouseWheel on Windows and macOS, Button-4/5 on X11) and the scroll bar, a click
    opening a subfolder; Your Stats: the cards' names, the leaderboard (switch, ranks,
    See All), Details' top lists and the day beside the calendar as links with the hand
    pointer and the song menu; the Most Played page with Play and its name for Now
    Playing's back link."""
    lib = make_library(tmp_path, seconds=2, mp3=False)
    many = _many_folders(lib)
    env = _env(tmp_path, lib)
    body = f"""
LIB = {json.dumps(lib)}
S = LIB["songs"]
MANY = {many!r}
""" + HELPERS + """
def loaded():
    app.engine.pause()
    # ---- 1. Folders: one list, the subfolders above the songs
    app.show_page("folders", folder=MANY)
    root.update()
    p = app.page("folders")
    t = p.table
    rows = t.rows
    assert len(rows) == 55 and all(isinstance(r, player_collections.FolderRow) for r in rows[:25]), rows[:3]
    assert not any(isinstance(r, player_collections.FolderRow) for r in rows[25:])
    assert [r.name for r in rows[:3]] == ["Sub 00", "Sub 01", "Sub 02"] and t.has_header
    assert not [w for w in p.winfo_children() if isinstance(w, player_collections.SongList) and w is not t], \\
        "no second list of subfolders"
    assert t.winfo_height() > 400, t.winfo_height()  # the list fills the page
    assert t._max_top() > 1000
    # the mouse wheel scrolls the one list: the subfolders go and the songs come up
    wheel(t.canvas, 6)
    assert t.top == 6 * 3 * t.rh, t.top
    wheel(t.canvas, 100)
    assert t.top == t._max_top(), (t.top, t._max_top())
    shown = [t.canvas.itemcget(i, "text") for i in t.canvas.find_all() if t.canvas.type(i) == "text"]
    assert "Sub 00" not in shown and "Low Pass" in shown, shown
    wheel(t.canvas, -100)
    assert t.top == 0
    # the scroll bar: what dragging its handle asks for
    t._yview("moveto", "0.5")
    assert abs(t.top - 0.5 * t._content_height()) < 1, t.top
    t._yview("moveto", "0")
    # sorting keeps the subfolders first
    t.sort_by("title")
    assert all(isinstance(r, player_collections.FolderRow) for r in t.rows[:25])
    t.sort_by("title")
    assert [r.name for r in t.rows[:2]] == ["Sub 00", "Sub 01"] and t.sort_rev
    # a click on a subfolder opens it (the pointer is a hand over it); a double click does not
    # open the next one too
    root.update()
    y = t.HEADER + 2 * t.rh + t.rh // 2
    t.canvas.event_generate("<Motion>", x=120, y=y)
    root.update()
    assert str(t.canvas.cget("cursor")) == "hand2"
    t.canvas.event_generate("<Motion>", x=120, y=t.HEADER + 30 * t.rh + 5)  # a song: no hand
    root.update()
    assert str(t.canvas.cget("cursor")) == ""
    t.canvas.event_generate("<Button-1>", x=120, y=y, time=5000)
    t.canvas.event_generate("<ButtonRelease-1>", x=120, y=y, time=5050)
    t.canvas.event_generate("<Button-1>", x=120, y=y, time=5150)  # the second click of a double click
    root.update()
    assert p.folder == os.path.join(MANY, "Sub 02"), p.folder
    assert [getattr(r, "title", "") for r in t.rows] == ["Porch Lamp"] and t.has_header
    assert app.current is None, "the double click played nothing"
    # the library folder: only subfolders, without headings
    app.show_page("folders", folder=os.path.abspath(LIB["music"]))
    root.update()
    assert not t.has_header and all(isinstance(r, player_collections.FolderRow) for r in t.rows)
    # a song's right click: its menu (posted on Linux only: tk_popup waits on Windows and macOS)
    app.show_page("folders", folder=MANY)
    root.update()
    if sys.platform.startswith("linux"):
        app.last_menu = None
        t.canvas.event_generate("<Button-3>", x=120, y=t.HEADER + 26 * t.rh + 5, rootx=300, rooty=300)
        root.update()
        m = app.last_menu
        assert m is not None and "Play Next" in labels(m), m
        unpost(m)
        app.last_menu = None
        t.canvas.event_generate("<Button-3>", x=120, y=t.HEADER + 5, rootx=300, rooty=300)  # a subfolder: none
        root.update()
        assert app.last_menu is None
    stats_page()

def stats_page():
    # ---- 2. Your Stats: plays of three songs, all time
    now = time.time()
    for i, (title, secs) in enumerate([("First Light", 60), ("First Light", 60), ("First Light", 60),
                                       ("First Light", 60), ("Porch Lamp", 60), ("Porch Lamp", 60),
                                       ("Taxi Lights", 40)]):
        app.lib.add_play(app.by_path[S[title]], now - 600 * (i + 1), secs)
    state["day"] = time.localtime(now - 600)
    app.stats_period = "All time"
    app.show_page("stats")
    root.update()
    st = app.page("stats")
    c = st.area.c
    rows = st.boards["songs"]
    assert [(r["rank"], r["key"]) for r in rows] == [(1, S["First Light"]), (2, S["Porch Lamp"]), (3, S["Taxi Lights"])]
    texts = {c.itemcget(i, "text") for i in c.find_all() if c.type(i) == "text"}
    assert {"Leaderboard", "See All", "Songs", "Artists", "Albums"} <= texts, texts
    # the top song's card: the hand pointer, a click plays it with the period's ranking as the queue
    big = str(st.theme.f("stat_big"))
    card = [b for i, b in items(c, "First Light") if c.itemcget(i, "font") == big]
    assert card, items(c, "First Light")
    show(c, card[0])
    card = [b for i, b in items(c, "First Light") if c.itemcget(i, "font") == big]
    x, y = (card[0][0] + card[0][2]) / 2 - c.canvasx(0), (card[0][1] + card[0][3]) / 2 - c.canvasy(0)
    c.event_generate("<Motion>", x=int(x), y=int(y))
    root.update()
    assert str(c.cget("cursor")) == "hand2"
    press_at(c, card[0])
    assert app.current == S["First Light"] and app.context_name == "Your Stats", (app.current, app.context_name)
    assert app.queue.context == [S["First Light"], S["Porch Lamp"], S["Taxi Lights"]]
    app.engine.pause()
    # the top artist's and album's cards open their pages
    click_text(c, "Aurora Fields", lambda b: b[3] - b[1] > 28, area=True)
    assert (app.page_name, app.page("group").kind, app.page("group").name) == ("group", "artists", "Aurora Fields")
    app.go_back()
    root.update()
    click_text(c, "Northern Lights", lambda b: b[3] - b[1] > 28, area=True)
    assert app.page_name == "album" and app.page("album").album["title"] == "Northern Lights"
    app.go_back()
    root.update()
    # the leaderboard: a right click on a song opens its menu
    if sys.platform.startswith("linux"):
        app.last_menu = None
        click_text(c, "Porch Lamp", lambda b: b[3] - b[1] < 28, seq="<Button-3>", area=True)
        m = app.last_menu
        assert m is not None and labels(m)[0] == "Play" and "Play Next" in labels(m), m
        unpost(m)
    # the switch: Albums; a click on an album opens it, the artist under it the artist
    click_text(c, "Albums", area=True)
    assert app.leaderboard_kind == "albums"
    click_text(c, "Small Hours", lambda b: b[3] - b[1] < 28, area=True)
    assert app.page_name == "album" and app.page("album").album["title"] == "Small Hours"
    app.go_back()
    root.update()
    click_text(c, "June Harbor", lambda b: b[3] - b[1] < 28, area=True)
    assert (app.page_name, app.page("group").name) == ("group", "June Harbor")
    app.go_back()
    root.update()
    # See All: the Most Played page, and its name on Now Playing's back link
    click_text(c, "See All", area=True)
    assert app.page_name == "most_played" and app.page_label("most_played", {}) == "Most Played"
    mp = app.page("most_played")
    mc = mp.area.c
    texts = {mc.itemcget(i, "text") for i in mc.find_all() if mc.type(i) == "text"}
    assert {"Northern Lights", "Small Hours", "City Rain"} <= texts, texts
    click_text(mc, "Songs")
    assert app.leaderboard_kind == "songs"
    for rank, title in (("1", "First Light"), ("2", "Porch Lamp"), ("3", "Taxi Lights")):
        r = [b for _, b in items(mc, rank) if b[0] < 60][0]
        n = items(mc, title)[0][1]
        assert abs((r[1] + r[3]) / 2 - (n[1] + n[3]) / 2) < 16, (rank, title, r, n)
    play = [w for w in mp.winfo_children()[0].winfo_children() if isinstance(w, PillButton)]
    press(next(b for b in play if b.text == "Play"))
    assert app.current == S["First Light"] and app.context_name == "Most Played"
    assert app.queue.context == [S["First Light"], S["Porch Lamp"], S["Taxi Lights"]]
    app.engine.pause()
    app.show_page("nowplaying")
    root.update()
    assert app.back_label() == "Most Played"
    app.go_back()
    root.update()
    # ---- Details: the top lists' names are links
    app.show_page("stats_details")
    root.update()
    dc = app.page("stats_details").area.c
    click_text(dc, "Jazz", area=True)
    assert (app.page_name, app.page("group").kind, app.page("group").name) == ("group", "genres", "Jazz")
    app.show_page("stats_details")
    root.update()
    click_text(dc, "City Rain", area=True)
    assert app.page_name == "album" and app.page("album").album["title"] == "City Rain"
    app.show_page("stats_details")
    root.update()
    click_text(dc, "Taxi Lights", area=True)
    assert app.current == S["Taxi Lights"]
    assert app.queue.context[:3] == [S["First Light"], S["Porch Lamp"], S["Taxi Lights"]]
    app.engine.pause()
    # ---- the day beside the calendar (or under it): the artist is a link
    d = state["day"]
    app.show_page("stats")
    st.show_month(d.tm_year, d.tm_mon)
    st.choose_day(d.tm_mday)
    root.update()
    day_c = st.day_c if st.side_by_side() else c
    click_text(day_c, "The Paper Boats", area=None if day_c is st.day_c else True)
    assert (app.page_name, app.page("group").name) == ("group", "The Paper Boats"), app.page_name
    print("OK")
    app.quit()

wait(lambda: len(app.tracks) == len(S) + 55 and not app.library_busy(), loaded, what="library")
root.mainloop()
"""
    run_script(tmp_path, body, env)


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_leaderboard_in_japanese(tmp_path):
    """The leaderboard's texts in Japanese, on Your Stats and the Most Played page."""
    lib = make_library(tmp_path, seconds=2, mp3=False)
    env = _env(tmp_path, lib, language="日本語")
    body = f"""
LIB = {json.dumps(lib)}
S = LIB["songs"]
""" + HELPERS + """
def loaded():
    from realhires.i18n import tr
    app.engine.pause()
    app.lib.add_play(app.by_path[S["First Light"]], time.time() - 600, 60)
    app.stats_period = "All time"
    app.show_page("stats")
    root.update()
    c = app.page("stats").area.c
    texts = {c.itemcget(i, "text") for i in c.find_all() if c.type(i) == "text"}
    assert {"ランキング", "すべて表示", "曲", "アーティスト", "アルバム"} <= texts, texts
    assert tr("Most Played") == "よく再生した項目"
    app.show_page("most_played")
    root.update()
    mp = app.page("most_played")
    assert mp.title_l.cget("text") == "よく再生した項目"
    assert app.page_label("most_played", {}) == "よく再生した項目"
    mp.rows = []
    mp.area.redraw()
    texts = {mp.area.c.itemcget(i, "text") for i in mp.area.c.find_all() if mp.area.c.type(i) == "text"}
    assert "この期間にはまだ再生がありません。" in texts, texts
    print("OK")
    app.quit()

wait(lambda: len(app.tracks) == len(S) and not app.library_busy(), loaded, what="library")
root.mainloop()
"""
    run_script(tmp_path, body, env)
