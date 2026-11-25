"""2.3: Change Cover… in the menus and Search Cover Online (docs/player-spec.md, "1."),
and the Properties window's Acquire Metadata… and Search buttons with the clipboard
watch ("12.").  The core's addresses and the copied-text rule, then the Tk player
headless in a subprocess (skipped without Tk or a display; locally:
xvfb-run -a python -m pytest).  The browser and the clipboard are fakes: the tests
never open a real browser."""

import json
import os
import urllib.parse

import pytest

from player_library import make_library
from realhires import covers, i18n, tagedit
from test_player_gui import _gui_available, needs_ffmpeg, run_script


def _query(url: str, key: str = "q") -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)[key][0]


def test_cover_search_looks_for_the_album_or_the_song():
    url = covers.cover_search_url("The Paper Boats", "Mira Calloway", "City Rain", "Taxi Lights")
    assert url == "https://www.google.com/search?tbm=isch&q=The+Paper+Boats+City+Rain"
    # no album artist: the artist; no album: the artist and the title; unknown parts left out
    assert covers.cover_search_query("", "Mira Calloway", "City Rain", "x") == "Mira Calloway City Rain"
    assert covers.cover_search_query("", "Mira Calloway", "", "Taxi Lights") == "Mira Calloway Taxi Lights"
    assert covers.cover_search_query("The Paper Boats", "", "", "Taxi Lights") == "The Paper Boats Taxi Lights"
    assert covers.cover_search_query("", "", "", "Taxi Lights") == "Taxi Lights"
    assert covers.cover_search_query("  ", " The  Paper\tBoats ", " Small Hours ", "") == "The Paper Boats Small Hours"
    assert covers.cover_search_url("", "", "", "") == "https://www.google.com/search?tbm=isch&q="
    url = covers.cover_search_url("AC/DC", "", "Back in Black & More + 100%", "")
    assert _query(url) == "AC/DC Back in Black & More + 100%" and "&q=AC%2FDC+Back+in+Black+%26" in url
    assert _query(covers.cover_search_url("宇多田ヒカル", "", "初恋", "")) == "宇多田ヒカル 初恋"


def test_web_search_for_a_song_and_for_an_album():
    song = {"title": "Taxi Lights", "artist": "Mira Calloway", "album": "City Rain", "album_artist": "The Paper Boats"}
    url = tagedit.web_search_url("composer", song)
    assert url == "https://www.google.com/search?q=%22Taxi+Lights%22+Mira+Calloway+composer"
    assert tagedit.web_search_query("date", song) == '"Taxi Lights" Mira Calloway release date'
    assert tagedit.web_search_query("track", song) == '"Taxi Lights" Mira Calloway track number'
    assert tagedit.web_search_query("disc", song) == '"Taxi Lights" Mira Calloway disc number'
    assert tagedit.web_search_query("album_artist", song) == '"Taxi Lights" Mira Calloway album artist'
    # several songs (no title): the album and its album artist (else the artist)
    album = {"title": None, "artist": None, "album": "Small Hours", "album_artist": "June Harbor"}
    assert tagedit.web_search_query("genre", album) == '"Small Hours" June Harbor genre'
    assert tagedit.web_search_query("genre", dict(album, album_artist="", artist="X")) == '"Small Hours" X genre'
    assert tagedit.web_search_query("composer", dict(song, artist="")) == '"Taxi Lights" The Paper Boats composer'
    assert tagedit.web_search_query("composer", {}) == "composer"
    # the value's own double quotes are dropped, so the quoted phrase stays one phrase
    assert tagedit.web_search_query("date", {"title": 'Say "Hello"', "artist": "A"}) == '"Say Hello" A release date'
    # in Japanese, the field's Japanese name (as the window's labels)
    assert tagedit.web_search_query("composer", song, "ja") == '"Taxi Lights" Mira Calloway 作曲者'
    assert tagedit.web_search_query("date", song, "日本語") == '"Taxi Lights" Mira Calloway リリース日'
    assert tagedit.search_name("track", "fr") == "track number"


def test_japanese_search_names_agree_with_the_field_labels():
    ja = i18n.load_catalog("ja").strings
    for f in tagedit.FIELDS:
        label = ja[tagedit.LABELS[f]]
        name = tagedit.search_name(f, "ja")
        # "Track"/"Disc" are searched for as the track and disc numbers (トラック番号, ディスク番号)
        assert name == (label + "番号" if f in ("track", "disc") else label), (f, name, label)
        assert ja["Search the web for the {field}"].count("{field}") == 1


def test_a_copied_text_fills_in_its_first_line():
    assert tagedit.clipboard_value("  John   Williams \nscore") == "John Williams"
    assert tagedit.clipboard_value("\n\n \r\nHans Zimmer") == "Hans Zimmer"
    assert tagedit.clipboard_value(" Ryuichi Sakamoto\x85x") == "Ryuichi Sakamoto"
    assert tagedit.clipboard_value("") == tagedit.clipboard_value(" \n\t") == ""
    assert tagedit.clipboard_value("x" * 1500) == "x" * tagedit.MAX_VALUE
    assert tagedit.clipboard_value("x" * 999 + " y") == "x" * 999  # cut, then the space trimmed
    assert (tagedit.WATCH_SECONDS, tagedit.WATCH_POLL_MS) == (120, 500)


@needs_ffmpeg
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_change_cover_search_online_and_properties_search(tmp_path):
    from fake_musicbrainz import FakeMusicBrainz

    lib = make_library(tmp_path, seconds=2, mp3=False)
    from realhires import musicbrainz, settings

    fake = FakeMusicBrainz()  # Acquire Metadata… searches at once: never musicbrainz.org
    try:
        cfg = settings.Settings()
        cfg.library_folders = [lib["music"]]
        cfg.theme = "Light"
        cfg.musicbrainz_server = fake.url
        settings.save(cfg, str(tmp_path / "settings.json"))
        env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
                   REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
                   REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
                   REALHIRES_COVERS=str(tmp_path / "covers"), REALHIRES_AUDIO_BACKEND="null",
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        env[musicbrainz.INTERVAL_ENV] = "0"
        run_script(tmp_path, _SCRIPT.replace("@LIB@", json.dumps(lib)), env, timeout=180)
    finally:
        fake.close()


_SCRIPT = """
LIB = @LIB@
S = LIB["songs"]
import tkinter as tk
from realhires import i18n, player_gui, player_pages, tagedit
from realhires.probe import probe

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

def entries(m):
    return [m.entrycget(i, "label") if m.type(i) != "separator" else "-" for i in range(m.index("end") + 1)]

def invoke(m, text):
    m.invoke(entries(m).index(text))
    root.update()

root = tk.Tk()
root.geometry("1280x800")
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)
opened = []
app.url_opener = opened.append      # never a real browser
clip = ["what was copied before"]
app.clipboard_reader = lambda: clip[0]
state = {}
city = [S["Umbrella Weather"], S["Taxi Lights"], S["Last Train Home"]]

def loaded():
    # 1. Change Cover… in a song's menu: the Cover window of the song's album; Acquire
    # Metadata… right after Properties…, Change Cover… after it
    taxi = app.track(S["Taxi Lights"])
    labels = [x for x in entries(app.song_menu(taxi)) if x != "-"]
    i = labels.index("Properties…")
    assert labels[i:i + 3] == ["Properties…", "Acquire Metadata…", "Change Cover…"], labels
    assert "Cover…" not in labels
    invoke(app.last_menu, "Change Cover…")
    w = app.cover_window
    assert w is not None and w.path == S["Taxi Lights"] and w.target_paths() == city, w and w.target_paths()
    # Search Cover Online, right after Change cover…: Google Images for the album artist and album
    slaves = w.change_b.master.pack_slaves()
    assert slaves.index(w.search_b) == slaves.index(w.change_b) + 1
    assert w.search_b.text == "Search Cover Online"
    w.search_b.command()
    assert opened == ["https://www.google.com/search?tbm=isch&q=The+Paper+Boats+City+Rain"], opened
    w.win.event_generate("<<Close>>")
    root.update()
    assert app.cover_window is None
    # an album card's menu: Album Properties…, Acquire Metadata…, Change Cover…
    small = next(a for a in app.albums if a["title"] == "Small Hours")
    m = player_pages.album_menu(app, small)
    assert entries(m) == ["Play", "Shuffle", "Play Next", "Add to Queue", "-", "Album Properties…",
                          "Acquire Metadata…", "Change Cover…", "-", "Colour Label"], entries(m)
    invoke(m, "Change Cover…")
    w = app.cover_window
    assert w is not None and w.target_paths() == [t.path for t in small["tracks"]]
    w.search_online()
    assert opened[-1] == "https://www.google.com/search?tbm=isch&q=June+Harbor+Small+Hours", opened
    w.close()
    # the album page's "…" button: the same menu
    app.open_album(small)
    root.update()
    page = app.page("album")
    assert page.more_b.winfo_ismapped()
    assert entries(page.menu()) == entries(m)
    invoke(page.menu(), "Change Cover…")
    assert app.cover_window is not None and app.cover_window.target_paths() == [t.path for t in small["tracks"]]
    app.cover_window.close()
    # 12. Properties for one song: Acquire Metadata… at the bottom left, Search per field
    w = app.edit_info([taxi])
    state["w"] = w
    assert w.acquire_b.master is w.btns and w.acquire_b.pack_info()["side"] == "left"
    assert all(not b.enabled for b in w.search_bs.values()), "not before the tags are read"
    wait(lambda: w.ready, one_ready, what="the tags")

def one_ready():
    w = state["w"]
    assert set(w.search_bs) == set(tagedit.FIELDS) and all(b.enabled for b in w.search_bs.values())
    assert w.search_tip("composer") == "Search the web for the composer"
    assert w.search_tip("date") == "Search the web for the release date"
    # the composer's Search: the browser, then the clipboard is watched
    w.search_bs["composer"].command()
    assert opened[-1] == "https://www.google.com/search?q=%22Taxi+Lights%22+The+Paper+Boats+composer", opened
    assert w.watch["field"] == "composer" and w.watch_note.cget("text") == w.WATCHING
    assert w.watch_step() is None and w.vars["composer"].get() == "Mika Rowe"  # nothing new copied yet
    clip[0] = "   \\n "  # copied, but empty: still watching
    assert w.watch_step() is None and w.watch is not None
    clip[0] = "  John   Williams \\nfilm composer"
    wait(lambda: w.watch is None, filled, what="the copied text filled in")

def filled():
    w = state["w"]
    assert w.vars["composer"].get() == "John Williams" and w.filled == "composer"
    assert w.watch_note.cget("text") == w.FILLED
    assert str(w.entries["composer"].cget("style")) == "Flash.P.TEntry"
    assert w.changes() == {"composer": "John Williams"}
    assert probe(S["Taxi Lights"]).tags["composer"] == "Mika Rowe", "not saved before Save"
    # another field's Search replaces the watch; after two minutes it ends by itself
    w.search("date")
    assert opened[-1].endswith("+release+date") and w.watch["field"] == "date"
    w.search("genre")
    assert w.watch["field"] == "genre"
    now = time.monotonic()
    w.clock = lambda: now + tagedit.WATCH_SECONDS + 1
    clip[0] = "Jazz"
    assert w.watch_step() is None and w.watch is None and w.watch_note.cget("text") == ""
    assert w.vars["genre"].get() == "Indie Pop", "nothing is filled in after two minutes"
    w.clock = time.monotonic
    # in Japanese the tooltips and searches name the field in Japanese
    i18n.set_language("ja")
    assert w.search_tip("composer") == "作曲者をウェブで検索", w.search_tip("composer")
    assert w.search_url("composer").endswith("+%E4%BD%9C%E6%9B%B2%E8%80%85"), w.search_url("composer")
    i18n.set_language("en")
    # the MusicBrainz window changes a field: it is read again unless something was typed into it
    w.vars["title"].set("Taxi Lights (typed)")
    out = tagedit.save_tags([S["Taxi Lights"]], {"genre": "City Pop", "title": "Taxi Lights"}, app.cfg)
    assert all(o.ok for o in out)
    w.files_changed([S["Taxi Lights"]])
    wait(lambda: w.vars["genre"].get() == "City Pop", reread, what="the fields read again")

def reread():
    w = state["w"]
    assert w.vars["title"].get() == "Taxi Lights (typed)" and w.vars["composer"].get() == "John Williams"
    assert w.changes() == {"title": "Taxi Lights (typed)", "composer": "John Williams"}, w.changes()
    # Acquire Metadata…: the MusicBrainz window for the same song; this window stays
    w.search("artist")
    mb = w.acquire()
    assert mb is not None and app.mb_window is mb and mb.paths == [S["Taxi Lights"]] and not mb.album
    assert app.edit_window is w
    mb.close()
    # closing the window ends the watch
    w.cancel()
    assert app.edit_window is None and w.watch is None
    assert probe(S["Taxi Lights"]).tags["composer"] == "Mika Rowe"
    # Album Properties…: the album is searched for, and Acquire Metadata… looks the album up
    small = next(a for a in app.albums if a["title"] == "Small Hours")
    w = app.edit_album_info(small)
    state["w"] = w
    wait(lambda: w.ready, album_ready, what="the album's tags")

def album_ready():
    w = state["w"]
    w.search("date")
    assert opened[-1] == "https://www.google.com/search?q=%22Small+Hours%22+June+Harbor+release+date", opened
    mb = w.acquire()
    assert mb is not None and mb.album and mb.paths == [t.path for t in next(
        a for a in app.albums if a["title"] == "Small Hours")["tracks"]]
    mb.close()
    w.save()  # nothing changed: closes, and the watch ends
    assert app.edit_window is None and w.watch is None
    print("OK")
    app.quit()

wait(lambda: len(app.tracks) == len(S) and not app.library_busy(), loaded, what="library")
root.mainloop()
"""
