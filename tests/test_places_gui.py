"""Where a song was played in the Tk player (2.4), headless in a subprocess, at the size
of the CI runners' screens (1024 x 768), against FakeGeo on 127.0.0.1 (never the real
service): nothing is asked while the setting is off; Settings → Playback's note, the
lookup's state and Forget play locations…; turning the setting on looks the place up
once, plays are stored with it, and no second request follows within 2 hours; the day
list beside the calendar names each play's place, Your Stats' Places part lists the
period's places, and a click opens a place's page with its songs, which play; the
same in Japanese."""

import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from fake_geo import FakeGeo  # noqa: E402
from player_library import make_library  # noqa: E402
from test_player_gui import _gui_available, _has_miniaudio, run_script  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def geo():
    server = FakeGeo()
    yield server
    server.close()


def _env(tmp_path, lib, server, language="English"):
    from realhires import settings

    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    cfg.theme = "Light"
    cfg.language = language
    cfg.location_server = server
    settings.save(cfg, str(tmp_path / "settings.json"))
    return dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
                REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
                REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
                REALHIRES_COVERS=str(tmp_path / "covers"), REALHIRES_TABS=str(tmp_path / "tabs"),
                REALHIRES_AUDIO_BACKEND="null", REALHIRES_LOCATION_INTERVAL="0",
                NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")


COMMON = """
import tkinter as tk
import urllib.request
from realhires import player_gui, player_places, places, stats
from realhires.player_places import PlacePage
player_places.messagebox.askyesno = lambda *a, **k: True  # confirmations say yes

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

def texts(canvas, tag=None):
    items = canvas.find_withtag(tag) if tag else canvas.find_all()
    return [canvas.itemcget(i, "text") for i in items if canvas.type(i) == "text"]

def requests():
    with urllib.request.urlopen(SERVER.replace("/api/v1/json/", "/__requests"), timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))

def start():
    global root, app
    root = tk.Tk()
    root.geometry("1024x740+0+0")
    root.report_callback_exception = fail
    app = player_gui.PlayerApp(root)
    return root, app
"""


def _count_requests(geo):
    """FakeGeo answers /__requests with the number of lookups it got (for the subprocess)."""
    original = geo.httpd.RequestHandlerClass.do_GET

    def do_get(handler):
        if handler.path == "/__requests":
            body = json.dumps(sum(1 for p, _ in geo.requests if p != "/__requests")).encode()
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        original(handler)

    geo.httpd.RequestHandlerClass.do_GET = do_get


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_play_locations_in_the_player(tmp_path, geo):
    lib = make_library(tmp_path, seconds=2, mp3=False)
    _count_requests(geo)
    env = _env(tmp_path, lib, geo.url)
    body = f"""
LIB = {json.dumps(lib)}
SERVER = {geo.url!r}
{COMMON}

def loaded():
    assert len(app.tracks) == len(LIB["songs"]), len(app.tracks)
    # the setting is off: nothing was asked, and plays get no place
    assert app.cfg.play_location is False and app.play_place() is None
    assert requests() == 0
    app.show_page("settings", group="Playback")
    root.update()
    sp = app.page("settings")
    note = sp.place_note_l.cget("text")
    assert note.startswith("What is sent: one unencrypted HTTP request to 127.0.0.1:") and "IP address" in note, note
    assert "no song, path or tag is sent" in note
    assert sp.place_status_l.cget("text") == "Turned off: no place is asked for or stored."
    assert sp.place_count_l.cget("text") == "No play has a place yet."
    assert sp.forget_places_b.winfo_ismapped()
    # the rows fit the narrow page of a 1024-pixel screen
    assert sp.forget_places_b.winfo_rootx() + sp.forget_places_b.winfo_width() <= root.winfo_rootx() + 1024
    # turned on: the place is looked up at once, in a thread
    sp._set("play_location", True)
    wait(lambda: app.place_cache.place is not None, found, what="the lookup")

def found():
    assert requests() == 1
    assert app.play_place() == places.Place("Shinjuku", "Tokyo", "Japan", "JP", 35.69, 139.69)
    sp = app.page("settings")
    root.update()
    status = sp.place_status_l.cget("text")
    assert status.startswith("Approximate place now: Shinjuku, Tokyo, Japan (looked up at "), status
    # two songs played: both are stored with the place
    tracks = [t for t in app.tracks if t.display_title in ("First Light", "Glacier Song")]
    app.play_list(tracks, 0, "Test")
    wait(lambda: len(app.plays()) >= 2, played, what="two plays", timeout=40)

def played():
    if app.engine.playing:
        app.play_pause()
    got = app.plays()
    assert [p.place.city for p in got[:2]] == ["Shinjuku", "Shinjuku"], got
    # no second lookup: the next is due 2 hours after the first, while music plays
    app.places_poll()
    assert requests() == 1 and not app.place_cache.due(time.monotonic(), True)
    # a play somewhere else, earlier today, for the Places part
    osaka = places.Place("Osaka", "Osaka", "Japan", "JP", 34.69, 135.5)
    t = app.by_path[LIB["songs"]["Blue Kettle"]]
    now = time.time()
    lt = time.localtime(now)
    early = max(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 30, 0, 1, -1)), now - 7200)
    app.lib.add_play(t, early, 120.0, place=osaka)
    app.lib.add_play(t, early + 200, 90.0, place=osaka)
    app.show_page("stats")
    page = app.page("stats")
    page.refresh()
    root.update()
    assert [(r["name"], r["plays"]) for r in page.place_rows] == [("Osaka, Japan", 2), ("Shinjuku, Tokyo, Japan", 2)]
    page.choose_day(lt.tm_mday)
    root.update()
    canvas = page.day_c if page.day_box is not None else page.area.c
    heads = texts(canvas, "place_head")
    assert heads == ["Shinjuku, Tokyo, Japan", "Osaka, Japan"], heads
    c = page.area.c
    c.yview_moveto(1.0)
    root.update()
    assert "Places" in texts(c), texts(c)
    assert texts(c, "place_row") == ["Osaka, Japan", "Shinjuku, Tokyo, Japan"]
    # a click on a place opens its page with the songs played there
    key = page.place_rows[1]["key"]
    x0, y0, x1, y1 = page.place_boxes[key]
    c.event_generate("<Button-1>", x=int(x0 + 40), y=int((y0 + y1) / 2 - c.canvasy(0)))
    root.update()
    pp = app.page("place")
    assert app.page_name == "place" and isinstance(pp, PlacePage) and pp.key == key
    assert pp.title_l.cget("text") == "Shinjuku, Tokyo, Japan"
    assert sorted(r.display_title for r in pp.table.rows) == ["First Light", "Glacier Song"]
    assert pp.sub.cget("text") == "This year: 0 minutes, 2 plays", pp.sub.cget("text")
    assert app.back_label() == "Your Stats" and app.page_label("place", {{"place": key}}) == "Shinjuku, Tokyo, Japan"
    pp.table.on_activate(0)
    wait(lambda: app.current == pp.table.rows[0].path, from_place, what="playing from the place")

def from_place():
    assert app.context_name == "Shinjuku, Tokyo, Japan", app.context_name
    app.play_pause()
    # Settings → Forget play locations…: the plays stay, without their places
    n = len(app.plays())
    app.show_page("settings", group="Playback")
    root.update()
    sp = app.page("settings")
    assert sp.place_count_l.cget("text").startswith("Plays with a place: "), sp.place_count_l.cget("text")
    sp.forget_places_b.command()  # the button: asks (yes), then forgets
    root.update()
    assert app.lib.places_count() == 0 and len(app.plays()) >= n
    root.update()
    assert app.page("settings").place_count_l.cget("text") == "No play has a place yet."
    # turned off: the place is dropped and nothing more is asked
    app.page("settings")._set("play_location", False)
    root.update()
    assert app.play_place() is None and app.place_cache.place is None
    assert app.page("settings").place_status_l.cget("text") == "Turned off: no place is asked for or stored."
    app.show_page("stats")
    page = app.page("stats")
    page.refresh()
    root.update()
    page.area.c.yview_moveto(1.0)
    root.update()
    hint = "Turn on Settings → Playback → Remember where songs were played to see where you listen."
    assert page.place_rows == [] and hint in texts(page.area.c)
    assert requests() == 1
    print("OK")
    root.destroy()

start()
wait(lambda: not app.library_busy() and len(app.tracks) == len(LIB["songs"]), loaded, what="the library")
root.mainloop()
"""
    run_script(tmp_path, body, env)
    assert all(h.get("user-agent", "").startswith("UniversalMusicPlayer/") for p, h in geo.requests
               if p != "/__requests")
    assert [p for p, _ in geo.requests if p != "/__requests"] == ["/api/v1/json/"]


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_play_locations_in_japanese(tmp_path, geo):
    lib = make_library(tmp_path, seconds=2, mp3=False)
    _count_requests(geo)
    env = _env(tmp_path, lib, geo.url, language="日本語")
    body = f"""
LIB = {json.dumps(lib)}
SERVER = {geo.url!r}
{COMMON}

def loaded():
    app.show_page("settings", group="Playback")
    root.update()
    sp = app.page("settings")
    assert sp.place_status_l.cget("text") == "オフです：場所を問い合わせることも保存することもありません。", sp.place_status_l.cget("text")
    assert sp.place_note_l.cget("text").startswith("送信される内容："), sp.place_note_l.cget("text")
    sp._set("play_location", True)
    wait(lambda: app.place_cache.place is not None, found, what="the lookup")

def found():
    root.update()
    status = app.page("settings").place_status_l.cget("text")
    assert status.startswith("現在のおおよその場所：Shinjuku, Tokyo, Japan"), status
    t = app.tracks[0]
    app.lib.add_play(t, time.time() - 60, 100.0, place=app.play_place())
    app.show_page("stats")
    page = app.page("stats")
    page.refresh()
    root.update()
    page.area.c.yview_moveto(1.0)
    root.update()
    assert "場所" in texts(page.area.c) and texts(page.area.c, "place_row") == ["Shinjuku, Tokyo, Japan"]
    app.show_place(page.place_rows[0]["key"])
    root.update()
    assert app.page("place").sub.cget("text").startswith("今年："), app.page("place").sub.cget("text")
    print("OK")
    root.destroy()

start()
wait(lambda: not app.library_busy() and len(app.tracks) == len(LIB["songs"]), loaded, what="the library")
root.mainloop()
"""
    run_script(tmp_path, body, env)
