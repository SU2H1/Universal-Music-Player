"""Universal Music Player 2.0: the desktop app, a music player that looks like Apple Music
(Tkinter).  docs/player-spec.md describes it; the Rust version
(rust/crates/gui) implements the same specification.

* The sidebar (Search, Library, Playlists, Settings), the playback bar on top
  (transport, the "LCD" with the song and the seek bar, likes, lyrics, Up Next,
  the equalizer and the volume) and the pages of the content area.
* The library is the tag index of the folders in Settings → Library
  (musiclib.py, library.sqlite), refreshed in the background on every start.
* Playback goes through realhires.player (FFmpeg + miniaudio), the queue
  through playqueue.py, and likes, volume, modes and EQ are kept in
  player.json (playerstate.py).
* The 1.x app (scanner, Fix, lyrics download, saved results) is the
  Quality check page, unchanged (gui.App inside this window).
"""

from __future__ import annotations

import copy
import multiprocessing
import os
import queue
import random
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Callable, Dict, List, Optional

from . import APP_NAME, PROJECT_URL, __version__, gui, i18n, musiclib, playerstate, probe, stats, themes
from .i18n import N_, tr, tr_name, tr_text
from . import lyrics as lyrics_mod
from . import settings as settings_mod
from .musiclib import Track
from .player import Player, SongInfo
from . import player as player_mod
from .player_collections import (FoldersPage, LibraryPage, PlaylistPage, PlaylistsPage, RecentlyPlayedPage,
                                 StatsDetailsPage, StatsPage, missing_track)
from .player_pages import (AlbumPage, AlbumsPage, GroupPage, NowPlayingPage, Page, SearchPage, SettingsPage,
                           SongsPage, album_order, frame, label, plural)
from .player_theme import (IconButton, Slider, Theme, draw_icon, fit_text, load_bundled_font, round_rect,
                           system_is_dark)
from .player_widgets import Column, CoverCache, SongList, bind_wheel, draw_cover, fmt_time
from .player_windows import EditInfoWindow, EqualizerWindow, PlaybackInfoWindow, ask_name
from .player_mb import MusicBrainzWindow
from . import musicbrainz
from .playqueue import REPEAT_LABELS, REPEAT_MODES, SHUFFLE_LABELS, SHUFFLE_MODES, PlayQueue, SplitMix64

SEARCH_DELAY_MS = 200
TICK_MS = 100
SKIP_AFTER_ERROR_MS = 2000
MOD = "Command" if sys.platform == "darwin" else "Control"
ACC = "Cmd" if sys.platform == "darwin" else "Ctrl"


# ------------------------------------------------------ the 1.x scanner ---
class QualityCheck(gui.App):
    """The 1.x app as the Quality check page: unchanged, except that it lives in the
    player's window, tells the player about saved lyrics and settings, and that
    the background check of the library at start shows no pop-ups."""

    def __init__(self, root, player: "PlayerApp", parent, initial=None, cfg=None):
        self.player = player
        self.quiet_scan = False      # the check of the library at start (Settings → Library)
        self._quiet_finished = False
        super().__init__(root, initial=initial, cfg=cfg, parent=parent)
        self.quit_callback = player.quit
        self.view_menu.add_separator()
        self.view_menu.add_command(label=tr("Back to the music library"), command=lambda: player.show_page("library"))

    def lyrics_saved(self, path: str) -> None:
        super().lyrics_saved(path)
        self.player.lyrics_changed(path)

    def apply_settings(self, new) -> None:
        old = self.cfg
        super().apply_settings(new)
        self.player.settings_changed(old, new)

    def _finish(self, stopped: bool) -> None:
        self._quiet_finished, self.quiet_scan = self.quiet_scan, False
        super()._finish(stopped)

    def _summary_after_scan(self) -> None:
        if not self._quiet_finished:
            super()._summary_after_scan()

    def _fix_after_scan(self) -> None:
        if not self._quiet_finished:
            super()._fix_after_scan()


# -------------------------------------------------------------- sidebar ---
class Sidebar(tk.Frame):
    """Search; Library (the start page); "Library": All Songs ... Composers;
    "Playlists": Liked Songs, Disliked Songs, the playlists and New Playlist…;
    "History": Recently Played, Recently Added, Your Stats; Settings at the bottom."""

    WIDTH = 220
    ROW, HEAD = 28, 26
    LIBRARY = [("songs", N_("All Songs"), "note"), ("folders", N_("Folders"), "folder"),
               ("albums", N_("Albums"), "album"), ("artists", N_("Artists"), "artist"),
               ("album_artists", N_("Album Artists"), "mic"), ("genres", N_("Genres"), "genre"),
               ("years", N_("Years"), "calendar"), ("composers", N_("Composers"), "piano")]
    HISTORY = [("played", N_("Recently Played"), "history"), ("recent", N_("Recently Added"), "clock"),
               ("stats", N_("Your Stats"), "stats")]
    PAGES = {"library"} | {k for k, _, _ in LIBRARY + HISTORY} | {"liked", "disliked", "settings"}

    def __init__(self, master, app: "PlayerApp"):
        super().__init__(master, width=self.WIDTH, bd=0, highlightthickness=0)
        self.app, t = app, app.theme
        t.reg(self, bg="sidebar")
        self.pack_propagate(False)
        self.selected = "library"
        self.hover: Optional[str] = None
        # the search box: a rounded field with an icon and a borderless entry
        self.search_c = tk.Canvas(self, height=34, highlightthickness=0, bd=0)
        t.reg(self.search_c, bg="sidebar")
        self.search_c.pack(fill="x", padx=12, pady=(14, 6))
        self.search_var = tk.StringVar()
        self.entry = tk.Entry(self.search_c, textvariable=self.search_var, relief="flat", bd=0,
                              highlightthickness=0, font=t.f("body"))
        t.reg(self.entry, bg="field", fg="text", insertbackground="text")
        self.placeholder = True
        self.search_c.bind("<Configure>", lambda e: self._draw_search())
        self.entry.bind("<FocusIn>", lambda e: self._focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._focus(False))
        self.entry.bind("<Escape>", lambda e: (self.search_var.set(""), self.app.root.focus_set()))
        self.search_var.trace_add("write", lambda *a: self._typed())
        self._show_placeholder()
        t.on_change(self._draw_search)
        bottom = tk.Frame(self, bd=0, highlightthickness=0)
        t.reg(bottom, bg="sidebar")
        bottom.pack(side="bottom", fill="x", padx=8, pady=(0, 10))
        self.status = tk.Label(bottom, text="", font=t.f("small"), anchor="w", justify="left",
                               wraplength=self.WIDTH - 32, bd=0)
        t.reg(self.status, bg="sidebar", fg="text2")
        self.status.pack(side="bottom", fill="x", padx=10, pady=(6, 0))
        self.foot = tk.Canvas(bottom, height=32, highlightthickness=0, bd=0)
        t.reg(self.foot, bg="sidebar")
        self.foot.pack(side="bottom", fill="x")
        self.foot.bind("<Button-1>", lambda e: self.app.show_page("settings"))
        self.foot.bind("<Motion>", lambda e: self._hover("settings"))
        self.foot.bind("<Leave>", lambda e: self._hover(None))
        # the pages (scrolls when there are many playlists)
        self.nav = tk.Canvas(self, highlightthickness=0, bd=0, height=200)
        t.reg(self.nav, bg="sidebar")
        self.nav.pack(fill="both", expand=True, padx=8)
        self.nav.bind("<Button-1>", self._click)
        self.nav.bind("<Motion>", lambda e: self._hover(self._key_at(self.nav.canvasy(e.y))))
        self.nav.bind("<Leave>", lambda e: self._hover(None))
        bind_wheel(self.nav, lambda n: self.nav.yview_scroll(n, "units"))
        self.nav.bind("<Configure>", lambda e: self.redraw())
        self.foot.bind("<Configure>", lambda e: self.redraw())
        t.on_change(self.redraw)

    # ---- search ---------------------------------------------------------
    def _draw_search(self) -> None:
        t, c = self.app.theme, self.search_c
        c.delete("all")
        W = c.winfo_width()
        if W <= 1:
            return
        round_rect(c, 0, 1, W, 33, 8, fill=t.c("field"), outline="")
        draw_icon(c, "search", 17, 17, 14, t.c("text2"))
        c.create_window(32, 17, window=self.entry, anchor="w", width=W - 40, height=22)
        self.entry.configure(fg=t.c("text2") if self.placeholder else t.c("text"))

    def _show_placeholder(self) -> None:
        self.placeholder = True
        self.entry.delete(0, "end")
        self.entry.insert(0, tr("Search"))
        self.entry.configure(fg=self.app.theme.c("text2"))

    def _focus(self, on: bool) -> None:
        if on and self.placeholder:
            self.placeholder = False
            self.entry.delete(0, "end")
            self.entry.configure(fg=self.app.theme.c("text"))
        elif not on and not self.search_var.get():
            self._show_placeholder()

    def _typed(self) -> None:
        if not self.placeholder:
            self.app.search_typed(self.search_var.get())

    def focus_search(self) -> None:
        self.entry.focus_set()

    # ---- items ----------------------------------------------------------
    def items(self) -> List[tuple]:
        lists = [(f"playlist:{p['id']}", p["name"], "playlist") for p in self.app.state.playlists]
        library = [(k, tr(text), icon) for k, text, icon in self.LIBRARY]
        history = [(k, tr(text), icon) for k, text, icon in self.HISTORY]
        return ([("library", tr("Library"), "library"), ("head", tr("Library"), "")] + library
                + [("head", tr("Playlists"), ""), ("liked", tr("Liked Songs"), "heart"),
                   ("disliked", tr("Disliked Songs"), "thumbs_down")] + lists
                + [("new_playlist", tr("New Playlist…"), "plus"), ("head", tr("History"), "")] + history)

    def _rows(self):
        rows, y = [], 2
        for key, text, icon in self.items():
            h = self.HEAD if key == "head" else self.ROW
            if key == "head":
                y += 10
            rows.append((key, text, icon, y, h))
            y += h
        return rows

    def _key_at(self, y: float) -> Optional[str]:
        for key, _, _, top, h in self._rows():
            if key != "head" and top <= y < top + h:
                return key
        return None

    def _hover(self, key: Optional[str]) -> None:
        if key != self.hover:
            self.hover = key
            self.redraw()

    def _click(self, e) -> None:
        key = self._key_at(self.nav.canvasy(e.y))
        if key == "new_playlist":
            self.app.new_playlist()
        elif key and key.startswith("playlist:"):
            self.app.open_playlist(key.split(":", 1)[1])
        elif key:
            self.app.show_page(key)

    def select(self, key: Optional[str]) -> None:
        self.selected = key or ""
        self.redraw()

    def _item(self, c: tk.Canvas, key: str, text: str, icon: str, y: float, h: float, W: int) -> None:
        t = self.app.theme
        sel = key == self.selected
        if sel or key == self.hover:
            round_rect(c, 4, y + 1, W - 4, y + h - 1, 7, fill=t.c("side_sel") if sel else t.c("hover"), outline="")
        draw_icon(c, icon, 24, y + h / 2, 15, t.c("accent"), filled=icon == "heart")
        c.create_text(44, y + h / 2, text=fit_text(t.f("side"), text, W - 52, self.app.fit_cache), anchor="w",
                      fill=t.c("text2") if key == "new_playlist" else t.c("text"), font=t.f("side"))

    def redraw(self) -> None:
        t, c = self.app.theme, self.nav
        c.delete("all")
        W = c.winfo_width()
        if W > 1:
            rows = self._rows()
            for key, text, icon, y, h in rows:
                if key == "head":
                    c.create_text(12, y + h - 8, text=text, anchor="w", fill=t.c("text2"), font=t.f("side_head"))
                else:
                    self._item(c, key, text, icon, y, h, W)
            end = rows[-1][3] + rows[-1][4] + 8
            c.configure(scrollregion=(0, 0, W, max(end, c.winfo_height())), yscrollincrement=self.ROW)
        f = self.foot
        f.delete("all")
        if f.winfo_width() > 1:
            self._item(f, "settings", tr("Settings"), "gear", 1, 30, f.winfo_width())

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)


# ------------------------------------------------------------------ LCD ---
class Lcd(tk.Canvas):
    """The middle of the playback bar: the cover, the title, "Artist — Album", the seek
    bar with the elapsed and remaining time, and the quality badge."""

    MAX_W = 720
    COVER = 44

    def __init__(self, master, app: "PlayerApp"):
        super().__init__(master, height=64, highlightthickness=0, bd=0)
        self.app, t = app, app.theme
        t.reg(self, bg="bg")
        self.track: Optional[Track] = None
        self.error = ""
        self.pos = self.dur = 0.0
        self.drag: Optional[float] = None
        self._fit: dict = {}
        self.geo: Dict[str, tuple] = {}
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion_drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Motion>", self._motion)
        t.on_change(lambda: (self._fit.clear(), self.redraw()))
        app.covers.listeners.append(lambda key, size: size == self.COVER and self.redraw())

    def set_song(self, track: Optional[Track], error: str = "") -> None:
        self.track, self.error = track, error
        self.redraw()

    def set_time(self, pos: float, dur: float) -> None:
        if (round(pos, 1), dur) != (round(self.pos, 1), self.dur):
            self.pos, self.dur = pos, dur
            self._draw_seek()

    def _panel(self):
        W = self.winfo_width()
        w = min(W - 16, self.MAX_W)
        x0 = (W - w) / 2
        return x0, 4, x0 + w, 60

    def redraw(self) -> None:
        t = self.app.theme
        self.delete("all")
        if self.winfo_width() <= 1:
            return
        x0, y0, x1, y1 = self._panel()
        round_rect(self, x0, y0, x1, y1, 9, fill=t.c("lcd"), outline="")
        self.geo = {}
        trk = self.track
        if trk is None:
            self.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=APP_NAME, fill=t.c("text2"),
                             font=t.f("lcd_title"))
            return
        cy = (y0 + y1) / 2
        cx0 = x0 + 6
        album = self.app.album_of(trk)
        key = album["key"] if album else trk.path
        img = self.app.covers.get(key, [x.path for x in album["tracks"]] if album else [trk.path], self.COVER)
        draw_cover(self, t, img, cx0, cy - self.COVER / 2, self.COVER)
        self.geo["cover"] = (cx0, cy - self.COVER / 2, cx0 + self.COVER, cy + self.COVER / 2)
        badge = tr_text(musiclib.quality(trk))
        bf = t.f("badge")
        bw = bf.measure(badge) + 14
        bx1 = x1 - 10
        bx0 = bx1 - bw
        round_rect(self, bx0, cy - 9, bx1, cy + 9, 5, fill="", outline=t.c("badge"), width=1)
        self.create_text((bx0 + bx1) / 2, cy, text=badge, fill=t.c("badge"), font=bf)
        self.geo["badge"] = (bx0 - 4, cy - 12, bx1 + 4, cy + 12)
        tx0, tx1 = cx0 + self.COVER + 14, bx0 - 14
        mid = (tx0 + tx1) / 2
        width = max(40, int(tx1 - tx0))
        self.create_text(mid, y0 + 14, text=fit_text(t.f("lcd_title"), trk.display_title, width, self._fit),
                         fill=t.c("text"), font=t.f("lcd_title"))
        if self.error:
            sub, color = tr_text(self.error), t.c("error")
        else:
            sub, color = f"{tr_name(trk.display_artist)} — {tr_name(trk.display_album)}", t.c("text2")
        self.create_text(mid, y0 + 30, text=fit_text(t.f("lcd_sub"), sub, width, self._fit), fill=color,
                         font=t.f("lcd_sub"))
        self.geo["text"] = (tx0, tx1)
        self._draw_seek()

    def _seek_geo(self):
        tx0, tx1 = self.geo.get("text", (0, 0))
        y = 48
        return tx0 + 40, tx1 - 40, y

    def _draw_seek(self) -> None:
        t = self.app.theme
        self.delete("seek")
        if self.track is None or "text" not in self.geo:
            return
        a, b, y = self._seek_geo()
        if b - a < 20:
            return
        dur = self.dur or self.track.duration or 0.0
        pos = self.drag if self.drag is not None else self.pos
        frac = min(max(pos / dur, 0.0), 1.0) if dur else 0.0
        self.create_line(a, y, b, y, fill=t.c("seek"), width=3, capstyle="round", tags="seek")
        if frac > 0:
            self.create_line(a, y, a + (b - a) * frac, y, fill=t.c("seek_fill"), width=3, capstyle="round",
                             tags="seek")
        if self.drag is not None:
            x = a + (b - a) * frac
            self.create_oval(x - 5, y - 5, x + 5, y + 5, fill=t.c("knob"), outline=t.c("seek_fill"), tags="seek")
        tf = t.f("time")
        self.create_text(a - 8, y, text=fmt_time(pos), anchor="e", fill=t.c("text2"), font=tf, tags="seek")
        left = max(0.0, dur - pos)
        self.create_text(b + 8, y, text="-" + fmt_time(left), anchor="w", fill=t.c("text2"), font=tf, tags="seek")

    def _hit(self, name: str, x: float, y: float) -> bool:
        g = self.geo.get(name)
        return bool(g) and g[0] <= x <= g[2] and g[1] <= y <= g[3]

    def _on_seek_bar(self, x: float, y: float) -> bool:
        if self.track is None or "text" not in self.geo:
            return False
        a, b, sy = self._seek_geo()
        return a - 4 <= x <= b + 4 and abs(y - sy) <= 8

    def _frac(self, x: float) -> float:
        a, b, _ = self._seek_geo()
        return min(max((x - a) / max(b - a, 1), 0.0), 1.0)

    def _motion(self, e) -> None:
        hand = self._hit("cover", e.x, e.y) or self._hit("badge", e.x, e.y) or self._on_seek_bar(e.x, e.y)
        self.configure(cursor="hand2" if hand else "")

    def _press(self, e) -> None:
        if self._on_seek_bar(e.x, e.y):
            dur = self.dur or (self.track.duration if self.track else 0.0)
            self.drag = self._frac(e.x) * dur
            self._draw_seek()

    def _motion_drag(self, e) -> None:
        if self.drag is not None:
            dur = self.dur or (self.track.duration if self.track else 0.0)
            self.drag = self._frac(e.x) * dur
            self._draw_seek()

    def _release(self, e) -> None:
        if self.drag is not None:
            target, self.drag = self.drag, None
            self.pos = target
            self.app.seek(target)
            self._draw_seek()
        elif self._hit("cover", e.x, e.y):
            self.app.toggle_now_playing()
        elif self._hit("badge", e.x, e.y):
            self.app.open_info()


# --------------------------------------------------------- playback bar ---
class PlaybackBar(tk.Frame):
    def __init__(self, master, app: "PlayerApp"):
        super().__init__(master, height=64, bd=0, highlightthickness=0)
        self.app, t = app, app.theme
        t.reg(self, bg="bg")
        self.pack_propagate(False)
        left = frame(self, t)
        left.pack(side="left", padx=(16, 8))
        self.shuffle = IconButton(left, t, "shuffle", command=app.shuffle_menu, size=32, icon_size=17,
                                  tooltip=lambda: tr("Shuffle: {mode}", mode=tr(SHUFFLE_LABELS[app.state.shuffle])))
        self.prev = IconButton(left, t, "prev", command=app.previous, size=36, icon_size=17, primary=True,
                               tooltip=lambda: tr("Previous"))
        self.play = IconButton(left, t, "play", command=app.play_pause, size=40, icon_size=22, primary=True,
                               tooltip=lambda: tr("Pause") if app.engine.playing else tr("Play"))
        self.next = IconButton(left, t, "next", command=app.next, size=36, icon_size=17, primary=True,
                               tooltip=lambda: tr("Next"))
        self.repeat = IconButton(left, t, "repeat", command=app.repeat_menu, size=32, icon_size=17,
                                 tooltip=lambda: tr("Repeat: {mode}", mode=tr(REPEAT_LABELS[app.repeat_mode()])))
        for b in (self.shuffle, self.prev, self.play, self.next, self.repeat):
            b.pack(side="left", padx=1)
        right = frame(self, t)
        right.pack(side="right", padx=(8, 16))
        self.heart = IconButton(right, t, "heart", command=app.toggle_like_current, size=32, icon_size=16,
                                tooltip=lambda: tr("Unlike this song") if app.current and app.is_liked(app.current)
                                else tr("Like this song"))
        self.dislike = IconButton(right, t, "thumbs_down", command=app.toggle_dislike_current, size=32,
                                  icon_size=16, tooltip=lambda: tr("Remove dislike") if app.current and
                                  app.is_disliked(app.current) else tr("Dislike this song"))
        self.more = IconButton(right, t, "dots", command=app.more_menu, size=32, icon_size=17,
                               tooltip=lambda: tr("More for this song"))
        self.lyrics = IconButton(right, t, "lyrics", command=app.toggle_now_playing, size=32, icon_size=17,
                                 tooltip=lambda: tr("Lyrics and cover"))
        self.queue = IconButton(right, t, "queue", command=app.toggle_up_next, size=32, icon_size=17,
                                tooltip=lambda: tr("Up Next"))
        self.eq = IconButton(right, t, "eq", command=app.open_eq, size=32, icon_size=17,
                             tooltip=lambda: tr("Equalizer"))
        self.speaker = IconButton(right, t, "speaker", command=app.toggle_mute, size=30, icon_size=16,
                                  tooltip=lambda: tr("Unmute") if app.state.muted else tr("Mute"))
        self.volume = Slider(right, t, 0.0, 1.0, app.state.volume, command=app.volume_changed, length=90)
        for b in (self.heart, self.dislike, self.more, self.lyrics, self.queue, self.eq):
            b.pack(side="left", padx=0)
        self.speaker.pack(side="left", padx=(8, 0))
        self.volume.pack(side="left")
        self.lcd = Lcd(self, app)
        self.lcd.pack(side="left", fill="both", expand=True)

    def update_controls(self) -> None:
        """When nothing plays only Previous, Next and the song's buttons (heart, dislike,
        "…") are disabled; volume, EQ, Lyrics and Up Next stay usable."""
        app = self.app
        has = app.current is not None
        st = app.state
        self.play.set(icon="pause" if app.engine.playing else "play", enabled=True)
        self.prev.set(enabled=has)
        self.next.set(enabled=has)
        self.shuffle.set(on=st.shuffle != "off")
        mode = app.repeat_mode()
        self.repeat.set(on=mode != "off", badge={"song": "1", "stop": "stop"}.get(mode, ""))
        self.heart.set(enabled=has, filled=bool(has and app.is_liked(app.current)))
        self.dislike.set(enabled=has, filled=bool(has and app.is_disliked(app.current)))
        self.more.set(enabled=has)
        self.lyrics.set(on=app.page_name == "nowplaying")
        self.queue.set(on=app.up_next_shown)
        self.eq.set(on=bool(st.eq.get("enabled")))
        self.speaker.set(level=0.0 if st.muted else st.volume)
        self.volume.set(st.volume, enabled=True)


class UpNextPanel(tk.Frame):
    WIDTH = 300

    def __init__(self, master, app: "PlayerApp"):
        super().__init__(master, width=self.WIDTH, bd=0, highlightthickness=0)
        self.app, t = app, app.theme
        t.reg(self, bg="sidebar")
        self.pack_propagate(False)
        label(self, t, tr("Up Next"), "h2", bg="sidebar").pack(anchor="w", padx=18, pady=(16, 0))
        self.source = label(self, t, "", "small", fg="text2", bg="sidebar", wraplength=self.WIDTH - 36,
                            justify="left")
        self.source.pack(anchor="w", padx=18, pady=(2, 8))
        self.list = SongList(self, t, [Column("title", "", 200, True, kind="two", key2="artist")],
                             lambda r, k: r.display_title if k == "title" else tr_name(r.display_artist),
                             header=False, zebra=False, row_height=48, bg_role="sidebar", on_activate=app.jump_to,
                             empty_text=tr("Nothing is queued."))
        self.list.pack(fill="both", expand=True, padx=(4, 0))

    def refresh(self) -> None:
        app = self.app
        q = app.queue
        self.source.configure(text=tr("Playing from {name}", name=app.context_name)
                              if q is not None and app.context_name else "")
        rows = [app.by_path[p] for p in (q.up_next(50) if q is not None else []) if p in app.by_path]
        self.list.set_rows(rows)


# ------------------------------------------------------------ the app ---
class PlayerApp:
    """The main window."""

    def __init__(self, root: tk.Tk, initial: Optional[List[str]] = None):
        self.root = root
        cfg = settings_mod.load()
        i18n.set_language(cfg.language)  # 2.1: the language is chosen once per start (Settings → Appearance)
        self.theme = Theme(root)
        self.theme.configure(cfg.theme, cfg.font_family)
        self.theme.styles()
        self.theme.style.configure("Treeview", rowheight=22)                      # the Quality check page,
        self.theme.style.configure("Horizontal.TProgressbar", background="#4a90d9")  # as 1.x had them
        self.theme.reg(root, bg="bg")
        self.fit_cache: dict = {}
        self.state = playerstate.load()
        self.engine = Player(device=cfg.output_device, gapless=cfg.gapless)
        self.engine.set_volume(self.state.volume, self.state.muted)
        self.engine.set_eq(self.state.eq)
        self.covers = CoverCache(root)
        self.q: queue.Queue = queue.Queue()
        self.tracks: List[Track] = []
        self.by_path: Dict[str, Track] = {}
        self.albums: List[dict] = []
        self.album_by_key: Dict[str, dict] = {}
        self._album_key: Dict[str, str] = {}
        self.queue: Optional[PlayQueue] = None
        self.context_name = ""
        self.current: Optional[str] = None
        self.error = ""
        self.resume_pos = 0.0
        self.fail_streak = 0
        self._skip_job: Optional[str] = None
        self._restored = False
        self.page_name = ""
        self.pages: Dict[str, tk.Frame] = {}
        self.dirty: set = set()
        self.history: List[tuple] = []
        self.up_next_shown = False
        self.eq_window: Optional[EqualizerWindow] = None
        self.info_window: Optional[PlaybackInfoWindow] = None
        self.edit_window: Optional[EditInfoWindow] = None  # one Edit Info window at a time
        self.mb_window: Optional[MusicBrainzWindow] = None  # one MusicBrainz window at a time (2.1)
        self.mb_thread: Optional[threading.Thread] = None   # the automatic MusicBrainz lookup
        self.mb_stop = threading.Event()
        self.mb_last: Optional[list] = None
        self.mb_forced = False
        self._info_time = 0.0
        self._search_job: Optional[str] = None
        self._search_from: Optional[tuple] = None
        self.lib_thread: Optional[threading.Thread] = None
        self.lib_stop = threading.Event()
        self.lib_again = False
        self.lib_then_check = False
        self._busy = False
        self.shuffle_var = tk.StringVar(value=self.state.shuffle)
        self.repeat_var = tk.StringVar(value=self.state.repeat)
        self.stats_period = stats.DEFAULT_PERIOD
        self.listen: Optional[dict] = None          # the song being listened to, for the play history
        self.name_dialog = None                     # an open NameDialog (tests fill it in)
        self.last_menu: Optional[tk.Menu] = None    # the song menu shown last
        self.lib = musiclib.Library()                # the main thread's connection (index, play history)
        self.lib_pending = False
        self.argv: List[str] = list(initial or [])  # the command line, for restart()
        self.start_process: Callable = subprocess.Popen  # how restart() starts the app again (tests replace it)
        self._build()
        self.qc = QualityCheck(root, self, parent=self.pages["quality"], initial=initial, cfg=cfg)
        self.qc.pack(fill="both", expand=True)
        self._build_menu()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self._bind_keys()
        try:
            self.set_tracks(self.lib.tracks())
        except Exception as exc:  # noqa: BLE001 - a broken index must not stop the app
            self.sidebar.set_status(tr("The library index could not be read: {error}", error=exc))
        self.show_page("quality" if initial else "library")
        if self.cfg.library_folders:
            self.refresh_library(then_check=self.cfg.scan_library_on_start)
        self.root.after(TICK_MS, self._tick)

    @property
    def cfg(self):
        return self.qc.cfg if hasattr(self, "qc") else settings_mod.load()

    # ---- layout ---------------------------------------------------------
    def _build(self) -> None:
        t = self.theme
        self.sidebar = Sidebar(self.root, self)
        self.sidebar.pack(side="left", fill="y")
        frame(self.root, t, bg="sep", width=1).pack(side="left", fill="y")
        main = frame(self.root, t)
        main.pack(side="left", fill="both", expand=True)
        self.bar = PlaybackBar(main, self)
        self.bar.pack(side="top", fill="x")
        frame(main, t, bg="sep", height=1).pack(side="top", fill="x")
        body = frame(main, t)
        body.pack(side="top", fill="both", expand=True)
        self.content = frame(body, t)
        self.content.pack(side="left", fill="both", expand=True)
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        self.up_next_sep = frame(body, t, bg="sep", width=1)
        self.up_next = UpNextPanel(body, self)
        quality = tk.Frame(self.content, bd=0, highlightthickness=0)  # the 1.x look, unchanged
        self.pages["quality"] = quality
        self.factories: Dict[str, Callable[[], Page]] = {
            "library": lambda: LibraryPage(self),
            "songs": lambda: SongsPage(self, tr("All Songs")),
            "folders": lambda: FoldersPage(self),
            "albums": lambda: AlbumsPage(self, tr("Albums")),
            "album": lambda: AlbumPage(self),
            "artists": lambda: GroupPage(self, "artists"),
            "album_artists": lambda: GroupPage(self, "album_artists"),
            "genres": lambda: GroupPage(self, "genres"),
            "years": lambda: GroupPage(self, "years"),
            "composers": lambda: GroupPage(self, "composers"),
            "playlists": lambda: PlaylistsPage(self),
            "playlist": lambda: PlaylistPage(self),
            "liked": lambda: SongsPage(self, tr("Liked Songs"), "liked"),
            "disliked": lambda: SongsPage(self, tr("Disliked Songs"), "disliked"),
            "played": lambda: RecentlyPlayedPage(self),
            "recent": lambda: AlbumsPage(self, tr("Recently Added"), recent=True),
            "stats": lambda: StatsPage(self),
            "stats_details": lambda: StatsDetailsPage(self),
            "search": lambda: SearchPage(self),
            "nowplaying": lambda: NowPlayingPage(self),
            "settings": lambda: SettingsPage(self),
        }

    def _build_menu(self) -> None:
        m = tk.Menu(self.root)
        f = tk.Menu(m, tearoff=False)
        f.add_command(label=tr("New Playlist…"), command=lambda: self.new_playlist())
        f.add_separator()
        f.add_command(label=tr("Add Music Folder…"), command=self.choose_music_folder)
        f.add_command(label=tr("Update the Library Now"), command=self.refresh_library)
        f.add_command(label=tr("Scan Library"), command=lambda: self.check_library(switch=True))
        f.add_command(label=tr("Download Lyrics for the Whole Library…"), command=self.lyrics_for_library)
        f.add_command(label=tr("Clear the Play History…"), command=self.clear_history)
        f.add_separator()
        f.add_command(label=tr("Settings…"), accelerator=f"{ACC}+,", command=lambda: self.show_page("settings"))
        f.add_separator()
        f.add_command(label=tr("Quit"), accelerator=f"{ACC}+Q", command=self.quit)
        m.add_cascade(label=tr("File"), menu=f)
        c = tk.Menu(m, tearoff=False)
        c.add_command(label=tr("Play / Pause"), accelerator="Space", command=self.play_pause)
        c.add_command(label=tr("Next"), accelerator=f"{ACC}+Right", command=self.next)
        c.add_command(label=tr("Previous"), accelerator=f"{ACC}+Left", command=self.previous)
        c.add_separator()
        c.add_cascade(label=tr("Shuffle"), menu=self._mode_menu(c, True))
        c.add_cascade(label=tr("Repeat"), menu=self._mode_menu(c, False))
        c.add_separator()
        c.add_command(label=tr("Volume Up"), accelerator=f"{ACC}+Up", command=lambda: self.volume_step(0.05))
        c.add_command(label=tr("Volume Down"), accelerator=f"{ACC}+Down", command=lambda: self.volume_step(-0.05))
        c.add_command(label=tr("Mute"), command=self.toggle_mute)
        c.add_separator()
        c.add_command(label=tr("Equalizer…"), command=self.open_eq)
        c.add_command(label=tr("Playback Info…"), command=self.open_info)
        m.add_cascade(label=tr("Controls"), menu=c)
        v = tk.Menu(m, tearoff=False)
        v.add_command(label=tr("Library"), command=lambda: self.show_page("library"))
        v.add_separator()
        from .player_collections import TILES
        for key, text, _ in TILES:
            v.add_command(label=tr(text), command=lambda k=key: self.show_page(k))
        v.add_separator()
        v.add_command(label=tr("Now Playing and Lyrics"), accelerator=f"{ACC}+L", command=self.toggle_now_playing)
        v.add_command(label=tr("Up Next"), command=self.toggle_up_next)
        v.add_separator()
        v.add_command(label=tr("Quality Check"), command=lambda: self.show_page("quality"))
        v.add_command(label=tr("Settings"), command=lambda: self.show_page("settings"))
        m.add_cascade(label=tr("View"), menu=v)
        h = tk.Menu(m, tearoff=False)
        h.add_command(label=tr("About {app}", app=APP_NAME), command=self.about)
        m.add_cascade(label=tr("Help"), menu=h)
        self.menubar = m
        self.shuffle_popup = self._mode_menu(self.root, True)
        self.repeat_popup = self._mode_menu(self.root, False)
        for menu in (m, self.qc.menubar, self.qc.menu, self.shuffle_popup, self.repeat_popup):
            self._theme_menu(menu)

    def theme_menu(self, menu: tk.Menu) -> None:
        self._theme_menu(menu)

    def _theme_menu(self, menu: tk.Menu) -> None:
        """Menus in the theme's colours (X11 draws them itself; Windows and macOS keep theirs)."""
        menu.configure(relief="flat", bd=0, activeborderwidth=0)
        self.theme.reg(menu, bg="bg", fg="text", activebackground="accent", activeforeground="on_accent",
                       selectcolor="accent", disabledforeground="disabled")
        last = menu.index("end")
        for i in range(0 if last is None else last + 1):
            if menu.type(i) == "cascade":
                sub = self.root.nametowidget(menu.entrycget(i, "menu"))
                self._theme_menu(sub)

    def _mode_menu(self, parent, shuffle: bool) -> tk.Menu:
        menu = tk.Menu(parent, tearoff=False)
        modes, labels = (SHUFFLE_MODES, SHUFFLE_LABELS) if shuffle else (REPEAT_MODES, REPEAT_LABELS)
        var = self.shuffle_var if shuffle else self.repeat_var
        for mode in modes:
            menu.add_radiobutton(label=tr(labels[mode]), value=mode, variable=var,
                                 command=(lambda m=mode: self.set_shuffle(m)) if shuffle
                                 else (lambda m=mode: self.set_repeat(m)))
        return menu

    def _bind_keys(self) -> None:
        r = self.root

        def mine(fn):  # the player's keys; the Quality check page keeps its own
            def handler(e):
                if self.qc.active:
                    return None
                w = e.widget
                cls = w.winfo_class() if isinstance(w, tk.Misc) else ""
                if fn is self.play_pause and cls in ("Entry", "TEntry", "Text", "TCombobox", "TSpinbox", "Listbox"):
                    return None
                fn()
                return "break"
            return handler

        r.bind("<space>", mine(self.play_pause))
        r.bind(f"<{MOD}-Right>", mine(self.next))
        r.bind(f"<{MOD}-Left>", mine(self.previous))
        r.bind(f"<{MOD}-Up>", mine(lambda: self.volume_step(0.05)))
        r.bind(f"<{MOD}-Down>", mine(lambda: self.volume_step(-0.05)))
        r.bind(f"<{MOD}-f>", mine(self.sidebar.focus_search))
        r.bind(f"<{MOD}-l>", mine(self.toggle_now_playing))
        r.bind(f"<{MOD}-comma>", mine(lambda: self.show_page("settings")))

    # ---- pages ----------------------------------------------------------
    def page(self, name: str):
        if name not in self.pages:
            self.pages[name] = self.factories[name]()
        return self.pages[name]

    def show_page(self, name: str, remember: bool = True, **kw) -> None:
        if self.page_name and self.page_name != name and remember and name != "search":
            self.history.append((self.page_name, self._page_args()))
            del self.history[:-20]
        if self.page_name == "search" and name != "search":
            self._search_from = None
        old = self.pages.get(self.page_name)
        if old is not None and self.page_name != name:
            old.grid_remove()
        page = self.page(name)
        page.grid(row=0, column=0, sticky="nsew")
        self.page_name = name
        if name == "quality":
            self.qc.active = True
            self.root.configure(menu=self.qc.menubar)
        else:
            self.qc.active = False
            self.root.configure(menu=self.menubar)
            if name in self.dirty:
                self.dirty.discard(name)
                page.refresh()
            page.show(**kw)
        if name == "playlist":
            pid = self.pages["playlist"].pid
            self.sidebar.select(f"playlist:{pid}" if pid else None)
        else:
            self.sidebar.select(name if name in Sidebar.PAGES else None)
        self.bar.update_controls()

    def _page_args(self) -> dict:
        p = self.pages.get(self.page_name)
        if self.page_name == "album" and isinstance(p, AlbumPage):
            return {"album": p.album}
        if self.page_name == "settings" and isinstance(p, SettingsPage):
            return {"group": p.group}
        if self.page_name == "playlist" and isinstance(p, PlaylistPage):
            return {"pid": p.pid}
        if self.page_name == "folders" and isinstance(p, FoldersPage):
            return {"folder": p.folder}
        return {}

    def go_back(self) -> None:
        if self.history:
            name, args = self.history.pop()
            self.show_page(name, remember=False, **args)
        else:
            self.show_page("library", remember=False)

    def open_album(self, album: dict) -> None:
        self.show_page("album", album=album)

    def open_artist(self, name: str) -> None:
        self.show_page("artists")
        self.page("artists").choose(name)

    def toggle_now_playing(self) -> None:
        if self.page_name == "nowplaying":
            self.go_back()
        elif self.current is not None:
            self.show_page("nowplaying")

    def toggle_up_next(self) -> None:
        self.up_next_shown = not self.up_next_shown
        if self.up_next_shown:
            self.up_next_sep.pack(side="left", fill="y")
            self.up_next.pack(side="left", fill="y")
            self.up_next.refresh()
        else:
            self.up_next.pack_forget()
            self.up_next_sep.pack_forget()
        self.bar.update_controls()

    def search_typed(self, text: str) -> None:
        if self._search_job is not None:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(SEARCH_DELAY_MS, lambda: self._search(text))

    def _search(self, text: str) -> None:
        self._search_job = None
        if text.strip():
            if self.page_name != "search":
                self._search_from = (self.page_name, self._page_args())
            self.show_page("search", text=text)
        elif self.page_name == "search":
            name, args = self._search_from or ("library", {})
            self._search_from = None
            self.show_page(name, remember=False, **args)

    # ---- library --------------------------------------------------------
    def set_tracks(self, tracks: List[Track]) -> None:
        self.tracks = list(tracks)
        self.by_path = {t.path: t for t in self.tracks}
        self.albums = musiclib.albums(self.tracks)
        self.album_by_key = {a["key"]: a for a in self.albums}
        self._album_key = {t.path: a["key"] for a in self.albums for t in a["tracks"]}
        self.dirty = set(self.pages) - {"quality"}
        if self.page_name in self.dirty:
            self.dirty.discard(self.page_name)
            self.pages[self.page_name].refresh()
        if not self._restored and self.tracks:
            self._restore_last()
        if self.current is not None:
            self.bar.lcd.set_song(self.by_path.get(self.current), self.error)

    def track(self, path: Optional[str]) -> Optional[Track]:
        return self.by_path.get(path) if path else None

    def album_of(self, track: Track) -> Optional[dict]:
        return self.album_by_key.get(self._album_key.get(track.path, ""))

    def library_busy(self) -> bool:
        """An update is running, or its result has not reached the window yet."""
        return self.lib_pending or (self.lib_thread is not None and self.lib_thread.is_alive())

    def refresh_library(self, then_check: bool = False) -> None:
        """Re-read the tags of new and changed files in a background thread."""
        self.lib_then_check = self.lib_then_check or then_check
        if self.library_busy():
            self.lib_again = True
            return
        cfg = settings_mod.Settings.from_dict(self.cfg.to_dict())
        stop = self.lib_stop
        out = self.q
        out.put(("lib_status", tr("Updating the library: looking for songs…")))

        def work():
            lib = None
            try:
                lib = musiclib.Library()
                files = musiclib.library_files(cfg)
                out.put(("lib_status", tr("Updating the library: {n} songs found…", n=f"{len(files):,}")))
                tracks = lib.refresh(files, progress=lambda d, n: out.put(("lib_progress", d, n)), stop=stop)
                out.put(("lib_done", tracks))
            except Exception as exc:  # noqa: BLE001
                out.put(("lib_error", str(exc)))
            finally:
                if lib is not None:
                    lib.close()

        self.lib_pending = True  # until "lib_done" or "lib_error" has been handled
        self.lib_thread = threading.Thread(target=work, daemon=True, name="realhires-library")
        self.lib_thread.start()
        if self.page_name in ("albums", "recent") and not self.tracks:
            self.pages[self.page_name].refresh()

    def _library_done(self, tracks: List[Track]) -> None:
        self.set_tracks(tracks)
        self.sidebar.set_status("")
        if self.lib_again:
            self.lib_again = False
            self.refresh_library()
            return
        if self.lib_then_check:
            self.lib_then_check = False
            self.check_library(switch=False)
        self.lookup_library()

    def choose_music_folder(self) -> None:
        d = filedialog.askdirectory(title=tr("Choose a music folder"), parent=self.root)
        if not d:
            return
        d = os.path.abspath(d)
        folders = list(self.cfg.library_folders)
        if d not in folders:
            folders.append(d)
            new = settings_mod.Settings.from_dict(self.cfg.to_dict())
            new.library_folders = folders
            self.apply_settings(new)
        if self.page_name == "settings":
            self.page("settings").build()

    def check_library(self, switch: bool = True) -> None:
        """Quality-check the library folders (the Quality check page's scanner)."""
        qc = self.qc
        folders = [f for f in self.cfg.library_folders if os.path.isdir(f)]
        if switch:
            self.show_page("quality")
        if qc.scanning or not folders or (not switch and not self.tracks):
            return
        qc.clear()
        for f in folders:
            qc._add_source(f)
        qc.quiet_scan = not switch
        qc.start_scan()

    def lyrics_for_library(self) -> None:
        if not self.tracks:
            messagebox.showinfo(tr("Download lyrics"), tr("The library has no songs yet. Add your music folder in "
                                                          "Settings → Library first."), parent=self.root)
            return
        self.qc.download_lyrics([t.path for t in musiclib.sort_songs(self.tracks)])

    def get_lyrics_current(self) -> None:
        if self.current:
            self.qc.download_lyrics([self.current])

    def lyrics_changed(self, path: str) -> None:
        p = self.pages.get("nowplaying")
        if isinstance(p, NowPlayingPage):
            p.lyrics_changed(path)

    # ---- settings -------------------------------------------------------
    def apply_settings(self, new) -> None:
        self.qc.apply_settings(new)  # saves them, updates the scanner, then settings_changed()

    def settings_changed(self, old, new) -> None:
        if (old.theme, old.font_family) != (new.theme, new.font_family):
            self.theme.apply(new.theme, new.font_family)
            self._settings_page_again()
        if old.language != new.language:  # the restart note under Settings → Appearance → Language
            self._settings_page_again()
        if old.output_device != new.output_device:
            self.engine.set_device(new.output_device)
        if old.gapless != new.gapless:
            self.engine.set_gapless(new.gapless)
            self._update_next()
        if (list(old.library_folders), old.extension_set(), old.recursive) != (
                list(new.library_folders), new.extension_set(), new.recursive):
            if new.library_folders:
                self.refresh_library()
            else:
                self.set_tracks([])

    # ---- pages that follow the player state ---------------------------------
    def _refresh_pages(self, names) -> None:
        """Refresh these pages now when shown, else when they are shown next."""
        for name in names:
            if name in self.pages:
                if name == self.page_name:
                    self.pages[name].refresh()
                else:
                    self.dirty.add(name)

    def _settings_page_again(self) -> None:
        p = self.pages.get("settings")
        if p is not None and self.page_name == "settings":
            self.root.after_idle(p.build)

    # ---- dislikes -------------------------------------------------------------
    def is_disliked(self, path: str) -> bool:
        return self.state.is_disliked(path)

    def toggle_dislike(self, path: str) -> None:
        """Disliked songs are left out of shuffles; liking one removes the dislike."""
        self.state.set_disliked(path, not self.state.is_disliked(path))
        q = self.queue
        if q is not None:
            q.disliked = set(self.state.disliked)
            if q.shuffle != "off":
                q.set_shuffle(q.shuffle)
                self._update_next()
                if self.up_next_shown:
                    self.up_next.refresh()
        self._likes_changed()

    def toggle_dislike_current(self) -> None:
        if self.current:
            self.toggle_dislike(self.current)

    # ---- the song menu --------------------------------------------------------
    def song_menu(self, track: Track, extras=(), list_name: str = "", rows=None, index=None) -> tk.Menu:
        """The menu of a song (right click, or "…" for the song playing)."""
        m = tk.Menu(self.root, tearoff=False)
        path = track.path
        missing = getattr(track, "missing", False)
        if rows is not None and index is not None and not missing:
            m.add_command(label=tr("Play"), command=lambda: self.play_list(list(rows), index, list_name))
            m.add_separator()
        m.add_command(label=tr("Unlike") if self.is_liked(path) else tr("Like"), command=lambda: self.toggle_like(path))
        m.add_command(label=tr("Remove dislike") if self.is_disliked(path) else tr("Dislike"),
                      command=lambda: self.toggle_dislike(path))
        sub = tk.Menu(m, tearoff=False)
        for p in self.state.playlists:
            sub.add_command(label=p["name"], command=lambda pid=p["id"]: self.add_to_playlist(pid, [path]))
        if self.state.playlists:
            sub.add_separator()
        sub.add_command(label=tr("New Playlist…"), command=lambda: self.new_playlist([path]))
        m.add_cascade(label=tr("Add to playlist"), menu=sub)
        if extras:
            m.add_separator()
            for text, cmd in extras:
                m.add_command(label=text, command=cmd)
        m.add_separator()
        album = None if missing else self.album_of(track)
        if album is not None:
            m.add_command(label=tr("Show album"), command=lambda: self.open_album(album))
        if os.path.exists(path):
            m.add_command(label=tr("Edit info…"), command=lambda: self.edit_info([track]))
            m.add_command(label=tr("Get info from MusicBrainz…"), command=lambda: self.musicbrainz([track]))
            m.add_command(label=tr("Show in folder"), command=lambda: gui.reveal_in_file_manager(path))
        if path == self.current:
            m.add_command(label=tr("Playback Info…"), command=self.open_info)
        self._theme_menu(m)
        self.last_menu = m
        return m

    # ---- song information ------------------------------------------------------
    def edit_info(self, tracks: List[Track], heading: str = "", subheading: str = "") -> Optional[EditInfoWindow]:
        """The Edit Info window for these songs; while one is open it is brought to the
        front instead (one at a time)."""
        w = self.edit_window
        if w is not None:
            try:
                if w.win.winfo_exists():
                    w.win.lift()
                    w.win.focus_force()
                    return w
            except tk.TclError:
                pass
            self.edit_window = None
        tracks = [t for t in tracks if not getattr(t, "missing", False)]
        if not tracks:
            return None
        self.edit_window = EditInfoWindow(self, tracks, heading, subheading)
        return self.edit_window

    def musicbrainz(self, tracks: List[Track], album: bool = False, heading: str = "",
                    subheading: str = "") -> Optional[MusicBrainzWindow]:
        """The MusicBrainz window for these songs (2.1); one at a time, as Edit Info."""
        w = self.mb_window
        if w is not None:
            try:
                if w.win.winfo_exists():
                    w.win.lift()
                    w.win.focus_force()
                    return w
            except tk.TclError:
                pass
            self.mb_window = None
        tracks = [t for t in tracks if not getattr(t, "missing", False) and os.path.exists(t.path)]
        if not tracks:
            return None
        self.mb_window = MusicBrainzWindow(self, tracks, album, heading, subheading)
        return self.mb_window

    def album_musicbrainz(self, album: dict) -> Optional[MusicBrainzWindow]:
        n = len(album["tracks"])
        return self.musicbrainz(album["tracks"], True, tr_name(album["title"]),
                                " · ".join(x for x in (tr_name(album["artist"]), plural(n, "song")) if x))

    # ---- the automatic MusicBrainz lookup (2.1) ---------------------------------------
    def musicbrainz_busy(self) -> bool:
        return self.mb_thread is not None and self.mb_thread.is_alive()

    def lookup_library(self, force: bool = False) -> bool:
        """Look the library's songs that miss information up on MusicBrainz in a thread
        (after each library update when `musicbrainz_auto` is on, or by hand with force)."""
        if self.musicbrainz_busy() or not self.tracks or (not force and not self.cfg.musicbrainz_auto):
            return False
        self.mb_forced = force
        cfg = settings_mod.Settings.from_dict(self.cfg.to_dict())
        known = {t.path: (musicbrainz.index_tags(t), t.duration) for t in self.tracks}
        paths = [t.path for t in self.tracks]
        out, stop = self.q, self.mb_stop

        def work():
            try:
                client = musicbrainz.MusicBrainzClient(cfg.musicbrainz_server, stop=stop)
                res = musicbrainz.run_auto(client, paths, cfg, known=known.get,
                                           progress=lambda i, n: out.put(("mb_progress", i, n)))
                out.put(("mb_done", res))
            except musicbrainz.MusicBrainzStopped:
                out.put(("mb_done", None))
            except Exception as exc:  # noqa: BLE001 - the lookup must never stop the app
                out.put(("mb_done", [musicbrainz.AutoOutcome("", musicbrainz.FAILED, message=str(exc))]))

        self.mb_thread = threading.Thread(target=work, daemon=True, name="realhires-mb-auto")
        self.mb_thread.start()
        return True

    def _musicbrainz_done(self, outcomes) -> None:
        if outcomes is None:
            self.sidebar.set_status("")
            return
        filled = [o.path for o in outcomes if o.status == musicbrainz.FILLED]
        # after an automatic run that asked nothing the status stays quiet; a run by hand always answers
        quiet = not outcomes and not self.mb_forced
        self.sidebar.set_status("" if quiet else tr_text(musicbrainz.auto_summary(outcomes)))
        self.mb_last = outcomes
        if filled:
            self.refresh_library()

    def edit_album_info(self, album: dict) -> Optional[EditInfoWindow]:
        n = len(album["tracks"])
        return self.edit_info(album["tracks"], tr_name(album["title"]),
                              " · ".join(x for x in (tr_name(album["artist"]), plural(n, "song")) if x))

    def tags_saved(self, paths: List[str]) -> None:
        """Edit Info saved new information into these files (the library index already
        holds it): read the library again, so every page and the LCD show it."""
        if paths:
            self.refresh_library()

    def more_menu(self) -> None:
        if not self.current:
            return
        t = self.track(self.current) or Track(self.current)
        b = self.bar.more
        self.song_menu(t).tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    # ---- playlists ------------------------------------------------------------
    def _playlist_name(self, name: str) -> str:
        clean = playerstate.clean_name(name)
        if not clean:
            raise playerstate.InvalidName(tr("Give the playlist a name."))
        return clean

    def new_playlist(self, songs=None, name: Optional[str] = None, open_page: bool = True) -> Optional[str]:
        """A new playlist (asks for its name), with `songs` in it; returns its id."""
        if name is None:
            name = ask_name(self, tr("New Playlist"), tr("Name of the playlist"),
                            self.state.unique_playlist_name(tr("New Playlist")), check=self._playlist_name,
                            ok=tr("Create"))
        if not name:
            return None
        pid = self.state.create_playlist(name, list(songs or []))
        self.save_state()
        self.playlists_changed()
        if open_page:
            self.open_playlist(pid)
        return pid

    def rename_playlist(self, pid: str, name: Optional[str] = None) -> None:
        p = self.state.playlist(pid)
        if p is None:
            return
        if name is None:
            name = ask_name(self, tr("Rename Playlist"), tr("New name of the playlist"), p["name"],
                            check=self._playlist_name, ok=tr("Rename"))
        if name:
            self.state.rename_playlist(pid, name)
            self.save_state()
            self.playlists_changed()

    def delete_playlist(self, pid: str, confirm: bool = True) -> None:
        p = self.state.playlist(pid)
        if p is None:
            return
        if confirm and not messagebox.askyesno(tr("Delete Playlist"), tr(
                "Delete the playlist “{name}”? The songs themselves are not deleted.", name=p["name"]),
                parent=self.root):
            return
        self.state.delete_playlist(pid)
        self.save_state()
        if self.page_name == "playlist" and self.pages["playlist"].pid == pid:
            self.history = [h for h in self.history if h[1].get("pid") != pid]
            self.show_page("playlists", remember=False)
        self.playlists_changed()

    def open_playlist(self, pid: str) -> None:
        self.show_page("playlist", pid=pid)

    def add_to_playlist(self, pid: str, paths) -> int:
        n = self.state.add_to_playlist(pid, list(paths))
        p = self.state.playlist(pid)
        self.save_state()
        self.playlists_changed()
        if p is not None:
            added = (tr("Added 1 song to “{name}”.", name=p["name"]) if n == 1 else
                     tr("Added {n} songs to “{name}”.", n=n, name=p["name"]))
            self.sidebar.set_status(added if n else tr("Already in “{name}”.", name=p["name"]))
            self.root.after(4000, lambda: self.sidebar.set_status("") if not self.library_busy() else None)
        return n

    def playlist_remove(self, pid: str, path: str) -> None:
        self.state.remove_from_playlist(pid, [path])
        self.save_state()
        self.playlists_changed()

    def playlist_move(self, pid: str, path: str, delta: int) -> None:
        self.state.move_in_playlist(pid, path, delta)
        self.save_state()
        self.playlists_changed()

    def playlist_tracks(self, pid: str) -> List[Track]:
        p = self.state.playlist(pid)
        return [self.by_path.get(x) or missing_track(x) for x in p["songs"]] if p else []

    def playlist_mosaic(self, p: dict) -> List[tuple]:
        """(album key, paths) of the covers of a playlist's picture: the albums of its
        first songs, up to four different ones."""
        out, seen = [], set()
        for path in p["songs"]:
            t = self.by_path.get(path)
            if t is None:
                continue
            album = self.album_of(t)
            key = album["key"] if album else t.path
            if key in seen:
                continue
            seen.add(key)
            out.append((key, [x.path for x in album["tracks"]] if album else [t.path]))
            if len(out) == 4:
                break
        return out

    def playlists_changed(self) -> None:
        self.sidebar.redraw()
        self._refresh_pages({"playlists", "playlist", "library"})

    # ---- play history and stats -------------------------------------------------
    def _listen_start(self, path: str) -> None:
        self._listen_end()
        self.listen = {"path": path, "started": time.time(), "seconds": 0.0, "last": None}

    def _listen_tick(self, pos: float) -> None:
        """Count the position's forward steps between updates while playing (seeking
        does not count)."""
        li = self.listen
        if li is None or li["path"] != self.current or self.engine.current != self.current:
            return
        if li["last"] is not None and 0.0 < pos - li["last"] <= 1.5:
            li["seconds"] += pos - li["last"]
        li["last"] = pos

    def _listen_end(self) -> None:
        """The song stopped being the current one: log what was heard (a second or more)."""
        li, self.listen = self.listen, None
        if li is None or li["seconds"] < musiclib.MIN_LOGGED_SECONDS:
            return
        t = self.by_path.get(li["path"]) or Track(li["path"])
        try:
            self.lib.add_play(t, li["started"], li["seconds"])
        except Exception:  # noqa: BLE001 - the history must never stop playback
            return
        self._refresh_pages({"played", "library", "stats", "stats_details"})

    def plays(self, since: float = 0.0, until: Optional[float] = None):
        try:
            return self.lib.plays(since, until)
        except Exception:  # noqa: BLE001
            return []

    def recently_played(self) -> List[str]:
        try:
            return self.lib.recently_played(200)
        except Exception:  # noqa: BLE001
            return []

    def last_played(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for p in self.plays():
            out[p.path] = max(out.get(p.path, 0.0), p.started)
        return out

    def compute_stats(self) -> dict:
        since, until = stats.period_range(self.stats_period, time.time())
        return stats.compute(self.plays(since, until))

    def clear_history(self, confirm: bool = True) -> None:
        if confirm and not messagebox.askyesno(tr("Clear the play history"), tr(
                "Forget every song you have played? Recently Played and Your Stats start again from nothing."),
                parent=self.root):
            return
        self.lib.clear_history()
        self._refresh_pages({"played", "library", "stats", "stats_details"})

    # ---- themes ----------------------------------------------------------------
    def all_themes(self) -> List[dict]:
        """The user's own themes, then those of the files in the themes folder (read
        again each time, so a file put there appears at once)."""
        own = themes.load_custom()
        return own + themes.load_folder(own)

    def theme_names(self) -> List[str]:
        return themes.names(self.all_themes())

    def theme_file(self, name: str) -> str:
        """The file of a theme from the themes folder, or ''."""
        return next((t.get("file", "") for t in self.all_themes() if t["name"] == name), "")

    def open_themes_folder(self, opener: Optional[Callable[[str], None]] = None) -> str:
        """Settings → Appearance → Open themes folder (made when it does not exist yet)."""
        folder = themes.folder()
        try:
            os.makedirs(folder, exist_ok=True)
            (opener or gui.open_with_default_app)(folder)
        except OSError as exc:
            messagebox.showerror(tr("Open themes folder"), tr("The themes folder {folder} could not be opened: "
                                                              "{error}", folder=folder, error=exc), parent=self.root)
        return folder

    def current_theme(self) -> dict:
        """The theme drawn now (its name, dark, and the eight colours)."""
        custom = self.all_themes()
        name = self.cfg.theme
        known = name in themes.BUILTIN or any(t["name"] == name for t in custom)
        return themes.resolve(name, False if known else system_is_dark(), custom)

    def is_custom_theme(self, name: str) -> bool:
        return any(t["name"] == name for t in themes.load_custom())

    def select_theme(self, name: str) -> None:
        new = settings_mod.Settings.from_dict(self.cfg.to_dict())
        new.theme = name
        self.apply_settings(new)

    def new_theme(self, name: Optional[str] = None) -> Optional[str]:
        """A copy of the theme drawn now, named by themes.copy_name(); it is selected."""
        custom = themes.load_custom()
        base = self.current_theme()
        name = themes.check_name(name, custom) if name else themes.copy_name(base["name"], custom)
        custom.append(themes.normalize({"name": name, "dark": base["dark"], "colors": dict(base["colors"])}))
        themes.save_custom(custom)
        self.select_theme(name)
        self._settings_page_again()
        return name

    def rename_theme(self, old: str, new: Optional[str] = None) -> None:
        custom = themes.load_custom()
        if new is None:
            new = ask_name(self, tr("Rename Theme"), tr("New name of the theme"), old,
                           check=lambda n: themes.check_name(n, custom, renaming=old), ok=tr("Rename"))
        if not new:
            return
        new = themes.check_name(new, custom, renaming=old)
        for t in custom:
            if t["name"] == old:
                t["name"] = new
        themes.save_custom(custom)
        if self.cfg.theme == old:
            self.select_theme(new)
        self._settings_page_again()

    def delete_theme(self, name: str, confirm: bool = True) -> None:
        if confirm and not messagebox.askyesno(tr("Delete Theme"), tr("Delete your theme “{name}”?", name=name),
                                               parent=self.root):
            return
        themes.save_custom([t for t in themes.load_custom() if t["name"] != name])
        if self.cfg.theme == name:
            self.select_theme(themes.SYSTEM)
        self._settings_page_again()

    def _edit_theme(self, name: str, change: Callable[[dict], None]) -> None:
        custom = themes.load_custom()
        for t in custom:
            if t["name"] == name:
                change(t)
        themes.save_custom(custom)
        if self.cfg.theme == name:
            self.theme.apply(self.cfg.theme, self.cfg.font_family)
        self._settings_page_again()

    def set_theme_color(self, name: str, key: str, value: str) -> None:
        self._edit_theme(name, lambda t: t["colors"].__setitem__(key, value.lower()))

    def set_theme_dark(self, name: str, dark: bool) -> None:
        self._edit_theme(name, lambda t: t.__setitem__("dark", bool(dark)))

    def export_theme_file(self, name: str, path: Optional[str] = None) -> Optional[str]:
        theme = next((t for t in self.all_themes() if t["name"] == name), None) or self.current_theme()
        if path is None:
            path = filedialog.asksaveasfilename(parent=self.root, title=tr("Export the theme"),
                                                initialfile=f"{theme['name']}.json", defaultextension=".json",
                                                filetypes=[(tr("{app} theme", app=APP_NAME), "*.json")])
        if not path:
            return None
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(themes.export_theme(theme))
        return path

    def import_theme_file(self, path: Optional[str] = None) -> Optional[str]:
        """A theme from a .json file (a taken name gets " 2"); it is selected."""
        if path is None:
            path = filedialog.askopenfilename(parent=self.root, title=tr("Import a theme"),
                                              filetypes=[(tr("{app} theme", app=APP_NAME), "*.json"),
                                                         (tr("All files"), "*")])
        if not path:
            return None
        custom = themes.load_custom()
        try:
            with open(path, encoding="utf-8") as fh:
                theme = themes.import_theme(fh.read(), custom)
        except (OSError, themes.ThemeError) as exc:
            messagebox.showerror(tr("Import a theme"), tr_text(str(exc)), parent=self.root)
            return None
        custom.append(theme)
        themes.save_custom(custom)
        self.select_theme(theme["name"])
        self._settings_page_again()
        return theme["name"]


    def output_devices(self):
        try:
            return self.engine.output.devices()
        except Exception:  # noqa: BLE001
            return []

    # ---- playback -------------------------------------------------------
    def play_list(self, tracks: List[Track], index: int = 0, name: str = "", shuffle: bool = False) -> None:
        """Play `tracks` (the list shown) from `index`; Shuffle starts a random song with
        shuffle "Songs in this list" on."""
        chosen = tracks[min(max(index, 0), len(tracks) - 1)] if tracks else None
        tracks = [t for t in tracks if not getattr(t, "missing", False)]  # files that are gone are skipped
        if not tracks:
            return
        index = tracks.index(chosen) if chosen in tracks else 0
        paths = [t.path for t in tracks]
        if shuffle:
            if self.state.shuffle == "off":
                self.set_shuffle("songs", rebuild=False)
            index = random.randrange(len(paths))
        self.queue = PlayQueue(library=[t.song() for t in self.tracks] or [t.song() for t in tracks],
                               context=paths, shuffle=self.state.shuffle, repeat=self.state.repeat,
                               seed=random.getrandbits(64), disliked=set(self.state.disliked))
        self.context_name = name
        self.fail_streak = 0
        self._play_path(self.queue.start(paths[min(max(index, 0), len(paths) - 1)]))

    def _play_path(self, path: str, position: float = 0.0) -> None:
        if self._skip_job is not None:
            self.root.after_cancel(self._skip_job)
            self._skip_job = None
        t = self.by_path.get(path)
        info = SongInfo(path, t.sample_rate, t.channels or 2, t.duration) if t is not None and t.sample_rate \
            else None
        self._listen_start(path)
        self.current, self.error, self.resume_pos = path, "", 0.0
        self.engine.play(path, position, info)
        self._update_next()
        self._song_changed()

    def _auto_next(self) -> Optional[str]:
        """What the queue plays when the current song ends by itself (asked of a copy)."""
        q = self.queue
        if q is None or not self.cfg.gapless:
            return None
        c = copy.copy(q)
        c.order = list(q.order)
        c.rng = SplitMix64(0)
        c.rng.state = q.rng.state
        return c.next(auto=True)

    def _update_next(self) -> None:
        if self.current is not None and self.engine.active:
            self.engine.set_next(self._auto_next(), after=self.current)

    def _song_changed(self) -> None:
        trk = self.track(self.current)
        if trk is None and self.current:  # not (or no longer) in the index: its file name
            trk = Track(self.current)
        self.bar.lcd.set_song(trk, self.error)
        self.bar.update_controls()
        if self.up_next_shown:
            self.up_next.refresh()
        for p in self.pages.values():
            if isinstance(p, Page):
                p.song_changed()
        if self.info_window is not None:
            self.info_window.refresh()

    def _restore_last(self) -> None:
        """At start the LCD shows the song of the last session, paused where it was."""
        self._restored = True
        st = self.state
        t = self.by_path.get(st.last_path) if st.last_path else None
        if t is None or self.current is not None:
            return
        album = self.album_of(t)
        tracks = album["tracks"] if album else [t]
        self.queue = PlayQueue(library=[x.song() for x in self.tracks], context=[x.path for x in tracks],
                               shuffle=st.shuffle, repeat=st.repeat, seed=random.getrandbits(64),
                               disliked=set(st.disliked))
        self.queue.start(t.path)
        self.context_name = tr_name(album["title"]) if album else ""
        self.current = t.path
        self.resume_pos = min(st.last_position, max(0.0, t.duration - 1.0)) if t.duration else st.last_position
        self._song_changed()

    def position(self) -> float:
        return self.engine.position() if self.engine.active else self.resume_pos

    def play_pause(self) -> None:
        if self.engine.active:
            self.engine.toggle()
        elif self.current is not None:
            self._play_path(self.current, self.resume_pos)
        else:
            page = self.pages.get(self.page_name)
            songs = page.songs() if isinstance(page, Page) else album_order(self.tracks)
            if not songs and self.page_name == "quality":
                songs = album_order(self.tracks)
            self.play_list(songs, 0, getattr(page, "list_name", "") or tr("Library"))
        self.bar.update_controls()

    def next(self) -> None:
        if self.queue is None or self.current is None:
            return
        nxt = self.queue.next()
        if nxt:
            self._play_path(nxt)
        else:
            self._stopped()

    def previous(self) -> None:
        if self.current is None:
            return
        if self.position() > 3.0 or self.queue is None:
            self.seek(0.0)
            return
        prev = self.queue.previous()
        if prev:
            self._play_path(prev)
        else:
            self.seek(0.0)

    def seek(self, seconds: float) -> None:
        if self.engine.active:
            self.engine.seek(seconds)
        elif self.current is not None:
            self.resume_pos = max(0.0, seconds)
            self.bar.lcd.set_time(self.resume_pos, self.engine.duration or getattr(self.track(self.current),
                                                                                   "duration", 0.0))

    def jump_to(self, index: int) -> None:
        """Up Next: play that song now (the queue continues after it)."""
        q = self.queue
        if q is None:
            return
        upcoming = q.up_next(50)
        if 0 <= index < len(upcoming):
            q.pos += 1 + index
            self._play_path(q.current)  # type: ignore[arg-type]

    def _stopped(self) -> None:
        """The queue has ended: the last song stays in the LCD, at its start."""
        self._listen_end()
        self.engine.stop()
        self.resume_pos = 0.0
        self._song_changed()

    def _engine_event(self, ev: tuple) -> None:
        kind, path = ev[0], ev[1]
        if kind == "advanced":
            nxt = self.queue.next(auto=True) if self.queue else None
            self._sync_repeat()
            if nxt == path:
                self._listen_start(path)
                self.current, self.error = path, ""
                self._update_next()
                self._song_changed()
            elif nxt:
                self._play_path(nxt)
            else:
                self._stopped()
        elif kind == "finished":
            nxt = self.queue.next(auto=True) if self.queue else None
            self._sync_repeat()
            if nxt:
                self._play_path(nxt)
            else:
                self._stopped()
        elif kind == "error":
            self.current, self.error = path, ev[2]
            self.fail_streak += 1
            self._song_changed()
            total = len(set(self.queue.order)) if self.queue else 1
            if self.fail_streak < total:
                self._skip_job = self.root.after(SKIP_AFTER_ERROR_MS, lambda: self._skip_failed(path))

    def _skip_failed(self, path: str) -> None:
        self._skip_job = None
        if self.current != path or not self.error or self.queue is None:
            return
        nxt = self.queue.next()
        if nxt:
            err = self.error
            self._play_path(nxt)
            if not self.engine.active:
                self.error = err
        else:
            self.resume_pos = 0.0
            self.bar.update_controls()

    def repeat_mode(self) -> str:
        return self.queue.repeat if self.queue is not None else self.state.repeat

    def _sync_repeat(self) -> None:
        if self.queue is not None and self.queue.repeat != self.state.repeat:
            self.state.repeat = self.queue.repeat  # "stop after this song" switched itself off
            self.repeat_var.set(self.state.repeat)
            self.save_state()

    def shuffle_menu(self) -> None:
        b = self.bar.shuffle
        self.shuffle_var.set(self.state.shuffle)
        self.shuffle_popup.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def repeat_menu(self) -> None:
        b = self.bar.repeat
        self.repeat_var.set(self.repeat_mode())
        self.repeat_popup.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def set_shuffle(self, mode: str, rebuild: bool = True) -> None:
        mode = mode if mode in SHUFFLE_MODES else "off"
        self.state.shuffle = mode
        self.shuffle_var.set(mode)
        if rebuild and self.queue is not None:
            self.queue.set_shuffle(mode)
            self._update_next()
        self.save_state()
        if self.up_next_shown:
            self.up_next.refresh()
        self.bar.update_controls()

    def set_repeat(self, mode: str) -> None:
        mode = mode if mode in REPEAT_MODES else "off"
        self.state.repeat = mode
        self.repeat_var.set(mode)
        if self.queue is not None:
            self.queue.set_repeat(mode)
            self._update_next()
        self.save_state()
        if self.up_next_shown:
            self.up_next.refresh()
        self.bar.update_controls()

    def volume_changed(self, value: float, final: bool) -> None:
        self.state.volume = min(max(value, 0.0), 1.0)
        self.engine.set_volume(self.state.volume, self.state.muted)
        self.bar.speaker.set(level=0.0 if self.state.muted else self.state.volume)
        if final:
            self.save_state()

    def volume_step(self, d: float) -> None:
        self.volume_changed(self.state.volume + d, True)
        self.bar.volume.set(self.state.volume)

    def toggle_mute(self) -> None:
        self.state.muted = not self.state.muted
        self.engine.set_volume(self.state.volume, self.state.muted)
        self.save_state()
        self.bar.update_controls()

    # ---- likes ----------------------------------------------------------
    def is_liked(self, path: str) -> bool:
        return self.state.is_liked(path)

    def toggle_like(self, path: str) -> None:
        self.state.set_liked(path, not self.state.is_liked(path))
        self._likes_changed()

    def _likes_changed(self) -> None:
        self.save_state()
        for p in self.pages.values():
            if isinstance(p, Page):
                p.likes_changed()
        self._refresh_pages({"library"})
        self.bar.update_controls()

    def toggle_like_current(self) -> None:
        if self.current:
            self.toggle_like(self.current)

    def save_state(self) -> None:
        st = self.state
        if st.repeat == "stop":  # "Stop after this song" is for the song playing now, never saved
            st = copy.copy(st)
            st.repeat = "off"
        try:
            playerstate.save(st)
        except OSError as exc:
            self.sidebar.set_status(tr("The player state could not be saved: {error}", error=exc))

    # ---- equalizer and info ---------------------------------------------
    def open_eq(self) -> None:
        if self.eq_window is not None and self.eq_window.win.winfo_exists():
            self.eq_window.win.lift()
            return
        self.eq_window = EqualizerWindow(self)

    def set_eq(self, state: dict) -> None:
        self.state.eq = state
        self.engine.set_eq(state)
        self.bar.update_controls()
        if self.info_window is not None:
            self.info_window.refresh()

    def save_eq(self, state: dict) -> None:
        self.set_eq(state)
        self.save_state()

    def open_info(self) -> None:
        if self.info_window is not None and self.info_window.win.winfo_exists():
            self.info_window.refresh()
            self.info_window.win.lift()
            return
        self.info_window = PlaybackInfoWindow(self)

    def playback_info(self) -> List[tuple]:
        """The sections of the Playback info window."""
        from .fix import fmt_bytes

        t = self.track(self.current)
        dash = "—"
        if t is None:
            return [(tr("Track"), [(tr("Title"), tr("Nothing is playing"))])]
        album = self.album_of(t)
        count = len(album["tracks"]) if album else 0
        track_no = (tr("{track} of {count}", track=t.track, count=count) if t.track and count >= t.track
                    else (str(t.track) if t.track else dash))
        track = [(tr("Title"), t.display_title), (tr("Artist"), t.artist or dash), (tr("Album"), t.album or dash),
                 (tr("Album artist"), t.album_artist or dash), (tr("Genre"), t.genre or dash),
                 (tr("Year"), t.year or dash), (tr("Track"), track_no), (tr("Disc"), str(t.disc) if t.disc else dash),
                 (tr("Duration"), fmt_time(t.duration)), (tr("File"), t.path),
                 (tr("Size"), fmt_bytes(t.size) if t.size else dash)]
        fmt = (tr("{codec}, lossy", codec=t.codec.upper()) if t.is_lossy else
               tr("{codec}, lossless", codec=t.codec.upper())) if t.codec else dash
        decoder = [(tr("Format"), fmt),
                   (tr("Sample rate"), tr_text(player_mod.fmt_khz(t.sample_rate))),
                   (tr("Bit depth"), tr_text(player_mod.bit_depth_label(t.bits, t.is_float, t.is_lossy))),
                   (tr("Channels"), tr_text(player_mod.channels_label(t.channels))),
                   (tr("Bit rate"), tr_text(player_mod.bit_rate_label(t.bit_rate))),
                   (tr("Decoder"), player_mod.ffmpeg_version())]
        info = self.engine.output_info()
        playing = self.engine.active and self.engine.current == t.path
        rate = info["rate"] if playing else 0
        output = [(tr("Device"), info["device"] or tr("System default")),
                  (tr("Output"), tr_text(player_mod.output_label(rate))),
                  (tr("Resampling"), tr_text(player_mod.resampling_label(t.sample_rate, rate,
                                                                         player_mod.resampler_name()))
                   if rate else dash),
                  (tr("Volume"), tr_text(player_mod.fmt_gain(0.0 if self.state.muted else self.state.volume ** 2,
                                                             self.state.muted))),
                  (tr("Equalizer"), tr_text(player_mod.eq_summary(self.state.eq))),
                  (tr("Gapless"), tr("On") if self.cfg.gapless else tr("Off"))]
        quality = [(tr("Verdict"), tr("Not checked yet"))]
        store = self.qc.store
        if store is not None:
            try:
                from .store import settings_key

                r = store.lookup(t.path, settings_key(self.cfg))
            except Exception:  # noqa: BLE001
                r = None
            if r is not None:
                quality = [(tr("Verdict"), tr(gui.VERDICT_STYLE.get(r.verdict, (r.verdict,))[0])),
                           (tr("Why"), tr_text(r.summary) if r.summary else dash)]
        return [(tr("Track"), track), (tr("Decoder"), decoder), (tr("Output"), output),
                (tr("Quality check"), quality)]

    # ---- the clock --------------------------------------------------------
    def _tick(self) -> None:
        try:
            for ev in self.engine.poll():
                self._engine_event(ev)
            self._drain()
            pos = self.position()
            t = self.track(self.current)
            dur = (self.engine.duration if self.engine.active else 0.0) or (t.duration if t else 0.0)
            self.bar.lcd.set_time(pos, dur)
            playing = self.engine.playing
            if (self.bar.play.icon == "pause") != playing:
                self.bar.play.set(icon="pause" if playing else "play")
            if playing and pos > 0.3:
                self.fail_streak = 0
            if playing:
                self._listen_tick(pos)
            if self.page_name == "nowplaying":
                self.pages["nowplaying"].update_position(pos)
            if self.info_window is not None and time.monotonic() - self._info_time > 1.0:
                self._info_time = time.monotonic()
                if self.info_window.win.winfo_exists():
                    self.info_window.refresh()
        except tk.TclError:
            return
        self.root.after(TICK_MS, self._tick)

    def _drain(self) -> None:
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "lib_status":
                    self.sidebar.set_status(rest[0])
                elif kind == "lib_progress":
                    done, total = rest
                    self.sidebar.set_status(tr("Updating the library: {done} of {total} songs", done=f"{done:,}",
                                               total=f"{total:,}"))
                elif kind == "lib_done":
                    self.lib_pending = False
                    self._library_done(rest[0])
                elif kind == "lib_error":
                    self.lib_pending = False
                    self.sidebar.set_status(tr("The library could not be updated: {error}", error=rest[0]))
                elif kind == "mb_progress":
                    i, n = rest
                    if i < n and not self.library_busy():
                        self.sidebar.set_status(tr("Getting song information from MusicBrainz: {i} of {n} albums",
                                                   i=f"{i + 1:,}", n=f"{n:,}"))
                elif kind == "mb_done":
                    self._musicbrainz_done(rest[0])
        except queue.Empty:
            pass

    # ---- the rest -------------------------------------------------------
    def about(self) -> None:
        messagebox.showinfo(
            tr("About {app}", app=APP_NAME),
            f"{APP_NAME} {__version__}\n\n"
            + tr("A music player (formerly Real-Hi-Res) that also finds fake hi-res audio (upsampled or 16-bit "
                 "padded) and fake lossless files (FLAC/WAV/ALAC made from MP3/AAC) by analysing the audio itself.")
            + f"\n\n{tr_text(lyrics_mod.ABOUT_CREDIT)}\n{tr_text(musicbrainz.ABOUT_CREDIT)}\n\n"
            + tr("On Windows and Linux the app uses the Inter typeface by Rasmus Andersson, under the SIL Open Font "
                 "License.") + "\n\n" + PROJECT_URL, parent=self.root)

    def restart_command(self) -> List[str]:
        """How to start the app again with the same arguments: the frozen program
        itself, else this Python with `-m realhires.gui`."""
        if getattr(sys, "frozen", False):
            return [sys.executable] + list(self.argv)
        return [sys.executable, "-m", "realhires.gui"] + list(self.argv)

    def restart(self) -> None:
        """Settings → Appearance → Language → Restart now: save everything as Quit does,
        then start the app again (a new language is chosen when the app starts)."""
        self._shutdown()
        kw = probe._no_window()  # the user's environment, and no console window on Windows
        kw["env"] = dict(kw["env"], PYINSTALLER_RESET_ENVIRONMENT="1")  # a program of its own, not a child
        kw["close_fds"] = True
        try:
            self.start_process(self.restart_command(), **kw)
        except OSError as exc:
            messagebox.showerror(APP_NAME, tr("{app} could not start again: {error}. Please start it yourself.",
                                              app=APP_NAME, error=exc), parent=self.root)
        self.root.destroy()

    def quit(self) -> None:
        self._shutdown()
        self.root.destroy()

    def _shutdown(self) -> None:
        """Save the player state and stop every thread and timer (Quit and Restart)."""
        if self.eq_window is not None:
            try:
                self.eq_window.close()
            except tk.TclError:
                pass
        st = self.state
        st.last_path = self.current or ""
        st.last_position = self.position() if self.current else 0.0
        self._listen_end()
        self.save_state()
        self.lib_stop.set()
        self.mb_stop.set()
        self.engine.close()
        self.covers.close()
        self.qc.shutdown()
        self.lib.close()
        try:  # pending timers would call into destroyed widgets
            for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.tk.call("after", "cancel", job)  # (after_cancel would delete widgets' commands)
        except tk.TclError:
            pass


def main(argv=None) -> int:
    multiprocessing.freeze_support()
    args = list(sys.argv[1:] if argv is None else argv)
    load_bundled_font()  # before Tk creates its fonts
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # e.g. started over SSH or on a server without a desktop
        print(f"{APP_NAME} cannot open its window: {exc}\n"
              "The app needs a graphical desktop (X11, or Wayland with XWayland). "
              "On a server or over SSH use the command-line tool instead "
              "(realhires / realhires-cli).", file=sys.stderr)
        return 1
    root.title(APP_NAME)
    root.geometry("1280x800")
    root.minsize(980, 620)
    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png")
    try:
        root.iconphoto(True, tk.PhotoImage(file=icon))
    except (tk.TclError, OSError):
        pass  # the icon is cosmetic only
    app = PlayerApp(root, initial=[a for a in args if not a.startswith("-")])
    app.argv = args  # restart() starts the app again with the same arguments
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
