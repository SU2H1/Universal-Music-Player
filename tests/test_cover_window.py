"""The Cover window of the Tk player (2.1, `player_windows.CoverWindow`), headless
in a subprocess: Cover… in the song menu, where the shown cover comes from (the
folder image, then the picture inside a song), Save image as…, Change cover… for
the whole album with the folder copy, Remove cover for one song, the covers drawn
again, Show album, and one window at a time."""

import json
import os

import pytest

from player_library import make_library
from test_player_gui import _gui_available, needs_ffmpeg, run_script


@needs_ffmpeg
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_cover_window(tmp_path):
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
import subprocess
import tkinter as tk
from realhires import covers, player_gui

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

def menu_labels(m):
    return [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) != "separator"]

root = tk.Tk()
root.geometry("1280x800")
root.report_callback_exception = fail
app = player_gui.PlayerApp(root)
state = {{}}
folder = os.path.dirname(S["Taxi Lights"])
album_songs = [S["Umbrella Weather"], S["Taxi Lights"], S["Last Train Home"]]

def idle(w):
    return not w.busy and not w.reading

def loaded():
    taxi = app.track(S["Taxi Lights"])
    assert "Change Cover…" in menu_labels(app.song_menu(taxi))
    w = app.open_cover_window(S["Taxi Lights"])
    assert app.open_cover_window(S["Taxi Lights"]) is w, "one Cover window at a time"
    state["w"] = w
    assert w.target_paths() == album_songs and w.scope.get() == "album"
    wait(lambda: idle(w) and w.photo is not None, shown, what="the cover read and drawn")

def shown():
    w = state["w"]
    assert w.source.kind == "folder" and w.where.cget("text") == "cover.png in the album's folder"
    assert w.details.cget("text").startswith("600 × 600 pixels · PNG · "), w.details.cget("text")
    assert w.canvas.find_all() and w.photo.width() <= w.canvas.winfo_width()
    out = os.path.join(TMP, "saved.png")
    assert w.save_image_to(out)
    assert open(out, "rb").read() == open(os.path.join(folder, "cover.png"), "rb").read()
    jpg = os.path.join(TMP, "new.jpg")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:d=1", "-frames:v", "1",
                    jpg], check=True)
    state["jpg"] = open(jpg, "rb").read()
    state["key"] = key = app.album_of(app.track(S["Taxi Lights"]))["key"]
    app.covers.get(key, album_songs, 64)
    wait(lambda: (key, 64) in app.covers.images, cached, what="the old cover in the cache")

def cached():
    w = state["w"]
    state["old"] = app.covers.images[(state["key"], 64)]
    jpg = os.path.join(TMP, "new.jpg")
    w.also_folder.set(True)
    assert w.change_to(jpg) and w.busy
    w.close()  # not while it works
    assert app.cover_window is w
    wait(lambda: idle(w) and not app.library_busy(), changed, what="the album's cover changed")

def changed():
    w = state["w"]
    assert w.summary_text == ("Changed the cover of 3 songs.\\n"
                              "The picture was also saved as cover.jpg in the album's folder."), w.summary_text
    for p in album_songs:
        src = covers.cover_source([p])
        assert src.kind == "embedded" and src.data == state["jpg"], p
    assert open(os.path.join(folder, "cover.jpg"), "rb").read() == state["jpg"]
    assert w.where.cget("text") == "Inside 02 Taxi Lights.flac" and w.details.cget("text").startswith("320 × 240")
    key = state["key"]
    assert app.covers.images.get((key, 64)) is not state["old"], "the old cover is still in the cache"
    app.covers.get(key, album_songs, 64)
    wait(lambda: (key, 64) in app.covers.images, recached, what="the new cover in the cache")

def recached():
    w = state["w"]
    assert app.covers.images[(state["key"], 64)] is not state["old"] and (state["key"], 64) not in app.covers.none
    w.scope.set("song")
    assert w.remove(confirm=False)
    wait(lambda: idle(w) and not app.library_busy(), removed, what="the song's cover removed")

def removed():
    w = state["w"]
    lines = w.summary_text.splitlines()
    assert lines[0] == "Removed the cover from 1 song.", w.summary_text
    assert "in the album's folder was not deleted" in lines[1], w.summary_text
    assert covers.cover_source([S["Taxi Lights"]]).kind == "folder"
    assert covers.cover_source([S["Umbrella Weather"]]).kind == "embedded"
    assert w.where.cget("text") == "Inside 01 Umbrella Weather.flac", w.where.cget("text")
    w.show_album()
    root.update()
    assert app.page_name == "album" and app.page("album").album["title"] == "City Rain"
    assert app.cover_window is None
    w = app.open_cover_window(S["Blue Kettle"])
    assert w.scope.get() == "album" and len(w.target_paths()) == 2
    w.win.event_generate("<<Close>>")
    root.update()
    assert app.cover_window is None
    print("OK")
    app.quit()

wait(lambda: len(app.tracks) == len(S) and not app.library_busy(), loaded, what="library")
root.mainloop()
"""
    run_script(tmp_path, body, env, timeout=180)
