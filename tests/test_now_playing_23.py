"""Now Playing's 2.3 requests in the Tk player, headless (docs/player-spec.md, "2.3"):
an instrumental song's cover in the centre (4), the back link at the top left (5),
Lyrics only (10), swiping the cover to the album's songs (11) and Details in the centre
(14).  The GUI test runs in a subprocess, as in test_player_gui.py."""

import json
import os
import shutil

import pytest

from player_library import make_library
from test_player_gui import _gui_available, _has_miniaudio, run_script

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@needs_ffmpeg
@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_now_playing_back_wide_instrumental_swipe_and_details(tmp_path):
    lib = make_library(tmp_path, seconds=3, mp3=False)
    sun = lib["songs"]["Midnight Sun"]
    with open(os.path.splitext(sun)[0] + ".lrc", "w", encoding="utf-8") as f:
        f.write("[au: instrumental]\n")  # LRCGET's mark for an instrumental song
    from realhires import settings

    cfg = settings.Settings()
    cfg.library_folders = [lib["music"]]
    settings.save(cfg, str(tmp_path / "settings.json"))
    env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
               REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
               REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
               REALHIRES_COVERS=str(tmp_path / "covers"), REALHIRES_AUDIO_BACKEND="null")
    body = f"""
LIB = {json.dumps(lib)}
S = LIB["songs"]
import tkinter as tk
from realhires import player_gui, player_pages
root = tk.Tk()
root.geometry("1280x800")
def fail(exc, val, tb):
    import traceback
    traceback.print_exception(exc, val, tb)
    os._exit(1)
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)
covers_opened = []
app.open_cover_window = lambda path: covers_opened.append(path)  # (the Cover window has its own tests)

def wait(cond, then, timeout=60.0, what="", start=None):
    start = start or time.monotonic()
    if cond():
        return root.after(50, then)
    assert time.monotonic() - start < timeout, "timed out: " + what
    root.after(100, lambda: wait(cond, then, timeout, what, start))

def settle():
    for _ in range(3):
        root.update()

def album(title):
    return next(a for a in app.albums if a["title"] == title)

def play(title, song):
    tracks = album(title)["tracks"]
    app.play_list(tracks, [t.path for t in tracks].index(S[song]), title)
    app.engine.pause()  # the 3-second songs must not end during the checks
    settle()

def click(widget, x, y):
    widget.event_generate("<ButtonPress-1>", x=x, y=y)
    widget.event_generate("<ButtonRelease-1>", x=x, y=y)
    settle()

def texts(canvas, tag="all"):
    return [canvas.itemcget(i, "text") for i in canvas.find_withtag(tag) if canvas.type(i) == "text"]

def loaded():
    np = app.page("nowplaying")
    # 5. the back link names the page Now Playing returns to, and goes there
    app.show_page("songs")
    play("Northern Lights", "Glacier Song")
    app.cover_clicked()
    settle()
    assert app.page_name == "nowplaying" and np.back_link.winfo_ismapped()
    assert np.back_link.text == "All Songs", np.back_link.text
    assert np.back_link.winfo_x() < 40 and np.back_link.winfo_y() < 40  # the top left
    click(np.back_link, 5, 10)
    assert app.page_name == "songs" and app.forward[-1][0] == "nowplaying"
    app.go_forward()
    settle()
    app.open_album(album("City Rain"))
    app.toggle_now_playing()
    settle()
    assert np.back_link.text == "City Rain"
    app._search("glacier")
    settle()
    assert app.page_name == "search"
    app.cover_clicked()
    settle()
    assert np.back_link.text == "Search"
    click(np.back_link, 5, 10)
    assert app.page_name == "search" and app.pages["search"].text == "glacier"  # its text and results
    app.cover_clicked()
    settle()
    app.history.clear()
    np.update_back()
    settle()
    assert not np.back_link.winfo_ismapped(), "no page to go back to: no link"

    # 10. Lyrics only: the cover goes, the lyrics fill the page, centred and 4 px larger; Esc
    assert np.mode() == "halves" and np.kind == "synced" and np.art.winfo_ismapped()
    size = app.state.lyrics_size
    assert -app.theme.f("lyrics").cget("size") == size
    assert np.wide_btn.winfo_ismapped() and not np.wide_btn.on
    click(np.wide_btn, 5, 5)
    assert app.lyrics_wide and np.mode() == "wide" and np.wide_btn.on
    assert not np.art.winfo_ismapped() and np.right.place_info()["relwidth"] == "1"
    assert -app.theme.f("lyrics").cget("size") == size + player_pages.LYRICS_WIDE_EXTRA
    lv = np.lyrics
    first = lv.items[0]
    assert lv.itemcget(first, "anchor") == "n" and lv.itemcget(first, "justify") == "center"
    assert abs(lv.coords(first)[0] - lv.winfo_width() / 2) < 1
    assert np.switch.winfo_ismapped(), "the switch stays"
    app.set_np_tab("upnext")  # Up Next keeps the two halves
    settle()
    assert np.mode() == "halves" and np.art.winfo_ismapped()
    app.set_np_tab("lyrics")
    settle()
    assert np.mode() == "wide"
    root.focus_force()
    np.event_generate("<Escape>")
    settle()
    assert not app.lyrics_wide and np.mode() == "halves" and np.art.winfo_ismapped()
    assert -app.theme.f("lyrics").cget("size") == size and lv.itemcget(lv.items[0], "anchor") == "nw"
    app.toggle_lyrics_wide()
    app.toggle_lyrics_wide()  # pressed again: the cover is back
    assert np.mode() == "halves"

    # 4. an instrumental song: the cover in the centre with "Instrumental", the switch above
    play("Northern Lights", "Midnight Sun")
    assert np.kind == "instrumental" and np.mode() == "instrumental", (np.kind, np.mode())
    assert np.art.place_info()["relwidth"] == "1" and np.switch.winfo_ismapped()
    assert not np.lyrics_pane.winfo_ismapped() and not np.get_btn.winfo_ismapped()
    assert texts(np.art, "instrumental") == ["Instrumental"]
    cover = next(l for l in np.links if l[0] == "cover")
    assert abs((cover[1] + cover[3]) / 2 - np.art.winfo_width() / 2) < 1, "the cover in the middle"
    app.toggle_lyrics_wide()  # Lyrics only does not apply to an instrumental song
    settle()
    assert np.mode() == "instrumental"
    app.toggle_lyrics_wide()
    app.set_np_tab("details")  # Details keeps the two halves
    settle()
    assert np.mode() == "halves" and np.art.place_info()["relwidth"] == "0.5"
    app.set_np_tab("lyrics")
    settle()

    # 11. a sideways drag turns the left half to the album's songs; a click is still a click
    play("Northern Lights", "Glacier Song")
    cover = next(l for l in np.links if l[0] == "cover")
    cx, cy = int((cover[1] + cover[3]) / 2), int((cover[2] + cover[4]) / 2)
    click(np.art, cx, cy)
    assert covers_opened == [S["Glacier Song"]], covers_opened
    np.art.event_generate("<ButtonPress-1>", x=cx, y=cy)
    np.art.event_generate("<B1-Motion>", x=cx - 30, y=cy + 2)
    settle()
    assert app.np_side == "cover", "30 px is not a swipe yet"
    np.art.event_generate("<B1-Motion>", x=cx - 90, y=cy + 4)
    np.art.event_generate("<ButtonRelease-1>", x=cx - 90, y=cy + 4)
    settle()
    assert app.np_side == "songs" and len(covers_opened) == 1, "a drag is not a click"
    titles = texts(np.art, "song")
    assert titles == ["First Light", "Glacier Song", "Midnight Sun", "Polar Drift"], titles
    playing = np.art.find_withtag("playing")
    assert len(playing) == 1 and np.art.itemcget(playing[0], "text") == "Glacier Song"
    assert np.art.itemcget(playing[0], "font") == str(app.theme.f("body_b")), "the song playing in bold"
    assert [d[0] for d in np.dots] == ["cover", "songs"]
    # a double-click plays a song with the album as the list
    i, x0, y0, x1, y1 = np.song_rows[3]
    for _ in range(2):  # a double-click (Tk refuses to generate <Double-Button-1> itself)
        click(np.art, int(x0 + 60), int((y0 + y1) / 2))
    app.engine.pause()
    settle()
    assert app.current == S["Polar Drift"] and app.context_name == "Northern Lights"
    assert np.art.itemcget(np.art.find_withtag("playing")[0], "text") == "Polar Drift"
    # it follows the song: another album's songs
    play("City Rain", "Taxi Lights")
    assert texts(np.art, "song") == ["Umbrella Weather", "Taxi Lights", "Last Train Home"]
    # the dots switch, and a drag the other way turns back
    side, a, b, c, d = np.dots[0]
    click(np.art, int((a + c) / 2), int((b + d) / 2))
    assert app.np_side == "cover" and texts(np.art, "song") == []
    np.art.event_generate("<ButtonPress-1>", x=cx, y=cy)
    np.art.event_generate("<B1-Motion>", x=cx + 80, y=cy)
    np.art.event_generate("<ButtonRelease-1>", x=cx + 80, y=cy)
    settle()
    assert app.np_side == "songs"
    np.art.event_generate("<ButtonPress-1>", x=cx, y=cy + 40)
    np.art.event_generate("<B1-Motion>", x=cx + 80, y=cy + 40)
    np.art.event_generate("<ButtonRelease-1>", x=cx + 80, y=cy + 40)
    settle()
    assert app.np_side == "cover"
    # a sideways scroll: to the right the songs, to the left the cover
    np.swipe(1)
    assert app.np_side == "songs"
    np.swipe(1)
    assert app.np_side == "songs"
    np.swipe(-1)
    assert app.np_side == "cover"
    # X11 reports a sideways scroll as Shift-Button-4/5; Windows and macOS as Shift-MouseWheel (on
    # Windows, Button-4/5 are the mouse's side buttons)
    x11 = root.tk.call("tk", "windowingsystem") == "x11"
    if x11:
        np.art.event_generate("<Shift-Button-5>", x=cx, y=cy)
    else:
        np.art.event_generate("<Shift-MouseWheel>", x=cx, y=cy, delta=-120)
    settle()
    assert app.np_side == "songs"
    if x11:
        np.art.event_generate("<Shift-Button-4>", x=cx, y=cy)
    else:
        np.art.event_generate("<Shift-MouseWheel>", x=cx, y=cy, delta=120)
    settle()
    assert app.np_side == "cover"
    assert len(covers_opened) == 1

    # 14. Details: one block in the middle of the right half
    app.set_np_tab("details")
    settle()
    dv = np.details
    x0, y0, x1, y1 = dv.block
    W, H = dv.winfo_width(), dv.winfo_height()
    assert x1 - x0 > 100 and y1 - y0 > 100, dv.block
    assert abs((x0 + x1) / 2 - W / 2) < 2 or x0 <= dv.PAD + 1, (dv.block, W)
    assert abs((y0 + y1) / 2 - H / 2) < 2, (dv.block, H)
    assert x1 - x0 <= W - dv.PAD
    # in a short window the block starts at the top and scrolls
    root.geometry("1280x400")
    settle()
    x0, y0, x1, y1 = dv.block
    assert y1 - y0 + dv.PAD > dv.winfo_height() and y0 <= dv.PAD, (dv.block, dv.winfo_height())
    print("OK")
    app.quit()

wait(lambda: len(app.tracks) == {len(lib["songs"])} and not app.library_busy(), loaded, what="the library")
root.mainloop()
"""
    run_script(tmp_path, body, env, timeout=150)
