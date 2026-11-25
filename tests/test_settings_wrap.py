"""The Settings page's texts wrap within the page (2.4): at the 1024 x 768 screens of
the Windows and macOS CI runners the page is narrower than SettingsPage.HELP_WIDTH, and
the help texts ran past its right edge."""

import os

import pytest

from test_player_gui import _gui_available, _has_miniaudio, run_script


@pytest.mark.skipif(not _has_miniaudio(), reason="miniaudio not installed")
@pytest.mark.skipif(not _gui_available(), reason="needs Tkinter and a display")
def test_settings_texts_wrap_within_the_page(tmp_path):
    env = dict(os.environ, REALHIRES_SETTINGS=str(tmp_path / "settings.json"),
               REALHIRES_PLAYER=str(tmp_path / "player.json"), REALHIRES_LIBRARY=str(tmp_path / "library.sqlite"),
               REALHIRES_RESULTS=str(tmp_path / "results.sqlite"), REALHIRES_THEMES=str(tmp_path / "themes.json"),
               REALHIRES_COVERS=str(tmp_path / "covers"), REALHIRES_TABS=str(tmp_path / "tabs"),
               REALHIRES_AUDIO_BACKEND="null")
    body = """
import time
import tkinter as tk
from realhires import player_gui
root = tk.Tk()
root.geometry("1024x740+0+0")
app = player_gui.PlayerApp(root)
checked = 0
for group in ("Library", "Playback", "Appearance", "Lyrics"):
    app.show_page("settings", group=group)
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)
    page = app.pages["settings"]
    right = page.canvas.winfo_rootx() + page.canvas.winfo_width()
    stack = list(page.inner.winfo_children())
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if isinstance(w, tk.Label) and w.winfo_ismapped() and int(float(str(w.cget("wraplength")))) > 0:
            checked += 1
            assert w.winfo_rootx() + w.winfo_reqwidth() <= right, (group, w.cget("text")[:60])
    # and narrowed no more than the page needs (on a wide screen they keep HELP_WIDTH);
    # the window cannot be made wider than the CI runners' 1024 x 768 screens, so this is
    # checked at the width the screen gives
    for w in page.help_labels.values():
        if w.winfo_ismapped():
            room = right - w.winfo_rootx() - 24
            assert int(float(str(w.cget("wraplength")))) >= min(page.HELP_WIDTH, room) - 1, (group, room)
assert checked > 10, checked
root.destroy()
print("OK")
"""
    run_script(tmp_path, body, env)
