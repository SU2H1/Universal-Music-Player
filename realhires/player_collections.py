"""The 2.0 player's Library start page and the pages added with it: Folders,
Playlists and a playlist, Recently Played, and Your Stats with its Details
(docs/player-spec.md, "Additions")."""

from __future__ import annotations

import os
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Tuple

from . import i18n, musiclib, stats
from .i18n import N_, tr, tr_name, tr_text
from .musiclib import Track
from .player_pages import (Page, album_order, frame, label, plural, song_table, track_sort,
                           track_value)
from .player_theme import IconButton, PillButton, draw_icon, fit_text, mix, round_rect
from .player_widgets import (AlbumGrid, Column, SongList, bind_wheel, draw_cover, draw_mosaic,
                             show_scrollbar)


# the summary cards of Your Stats, each in its own colour (white text stays readable on all)
CARD_COLORS = ["#e0245e", "#e8650a", "#23955a", "#1a6fd8", "#8e44ad", "#c2185b", "#0f8f8c", "#5856d6",
               "#b5651d", "#3f51b5"]


def fmt_played(ts: float, now: Optional[float] = None) -> str:
    """'Today 14:05', 'Yesterday', or the date ('3 Nov 2026')."""
    now = time.time() if now is None else now
    t, n = time.localtime(ts), time.localtime(now)
    if (t.tm_year, t.tm_yday) == (n.tm_year, n.tm_yday):
        return tr("Today {time}", time=f"{t.tm_hour:02d}:{t.tm_min:02d}")
    y = time.localtime(now - 86400)
    if (t.tm_year, t.tm_yday) == (y.tm_year, y.tm_yday):
        return tr("Yesterday")
    return tr_text(stats.day_label(f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"))


# the weekdays under the bars of Your Stats → Details (stats.WEEKDAYS cut to three letters)
WEEKDAYS_SHORT = [N_("Mon"), N_("Tue"), N_("Wed"), N_("Thu"), N_("Fri"), N_("Sat"), N_("Sun")]


def missing_track(path: str) -> Track:
    """A song of a playlist or the history whose file is not in the library."""
    t = Track(path)
    t.missing = True  # type: ignore[attr-defined]
    return t


class ScrollCanvas(tk.Frame):
    """A Canvas that scrolls vertically (wheel, scroll bar) and redraws on resize."""

    def __init__(self, master, page: Page, draw: Callable[[], float]):
        super().__init__(master, bd=0, highlightthickness=0)
        t = page.theme
        t.reg(self, bg="bg")
        self.draw_fn = draw
        self.c = tk.Canvas(self, highlightthickness=0, bd=0, width=300, height=200)
        t.reg(self.c, bg="bg")
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.c.yview, style="P.Vertical.TScrollbar")
        self.c.configure(yscrollcommand=lambda a, b: (self.sb.set(a, b), show_scrollbar(
            self.sb, float(a) > 0.0 or float(b) < 1.0)))
        self.sb.pack(side="right", fill="y")
        self.c.pack(side="left", fill="both", expand=True)
        self.c.bind("<Configure>", lambda e: self.redraw())
        bind_wheel(self.c, lambda n: self.c.yview_scroll(n, "units"))
        t.on_change(self.redraw, self)

    def redraw(self) -> None:
        if self.c.winfo_width() <= 1:
            return
        self.c.delete("all")
        height = self.draw_fn()
        self.c.configure(scrollregion=(0, 0, self.c.winfo_width(), max(height, self.c.winfo_height())),
                         yscrollincrement=24)


# ------------------------------------------------------------------ library ---
TILES = [  # page, name (English: shown as tr(name)), icon
    ("songs", N_("All Songs"), "note"), ("folders", N_("Folders"), "folder"), ("albums", N_("Albums"), "album"),
    ("artists", N_("Artists"), "artist"), ("album_artists", N_("Album Artists"), "mic"),
    ("genres", N_("Genres"), "genre"), ("years", N_("Years"), "calendar"), ("composers", N_("Composers"), "piano"),
    ("playlists", N_("Playlists"), "playlist"), ("liked", N_("Liked Songs"), "heart"),
    ("disliked", N_("Disliked Songs"), "thumbs_down"), ("played", N_("Recently Played"), "history"),
    ("recent", N_("Recently Added"), "clock"), ("stats", N_("Your Stats"), "stats"),
]


class LibraryPage(Page):
    """The start page: the fourteen entries as tiles, the newest albums, and a
    Your Stats banner when there are plays this year."""

    list_name = N_("Library")
    TILE_W, TILE_H, GAP, MARGIN = 180, 66, 14, 28  # TILE_W: the narrowest a tile may be
    CARD = 150

    def __init__(self, app):
        super().__init__(app)
        self.area = ScrollCanvas(self, self, self._draw)
        self.area.pack(fill="both", expand=True)
        self.c = self.area.c
        self.hits: List[Tuple[tuple, Callable[[], None]]] = []
        self.hover: Optional[tuple] = None
        self.counts: Dict[str, str] = {}
        self.newest: List[dict] = []
        self.year_minutes = 0
        self._fit: dict = {}
        self.c.bind("<Button-1>", self._click)
        self.c.bind("<Motion>", self._motion)
        app.covers.listeners.append(lambda key, size: size == self.CARD and self.winfo_exists()
                                    and self.area.redraw())
        self.refresh()

    def refresh(self) -> None:
        app, tracks = self.app, self.app.tracks
        n = len

        def count(k: int, word: str) -> str:
            return plural(k, word)

        played = app.recently_played()
        start, _ = stats.period_range(stats.DEFAULT_PERIOD, time.time())
        secs = sum(p.seconds for p in app.plays(start, None))
        self.year_minutes = int(secs / 60 + 0.5)
        self.counts = {
            "songs": count(n(tracks), "song"),
            "folders": count(n({os.path.dirname(t.path) for t in tracks}), "folder"),
            "albums": count(n(app.albums), "album"),
            "artists": count(n(musiclib.artists(tracks)), "artist"),
            "album_artists": count(n(musiclib.album_artists(tracks)), "album artist"),
            "genres": count(n(musiclib.genres(tracks)), "genre"),
            "years": count(n(musiclib.years(tracks)), "year"),
            "composers": count(n(musiclib.composers(tracks)), "composer"),
            "playlists": count(n(app.state.playlists), "playlist"),
            "liked": count(n(app.state.liked), "song"),
            "disliked": count(n(app.state.disliked), "song"),
            "played": count(n(played), "song"),
            "recent": count(n(app.albums), "album"),
            "stats": (tr("{n} minutes this year", n=f"{self.year_minutes:,}") if self.year_minutes
                      else tr("No plays yet")),
        }
        self.newest = sorted(app.albums, key=lambda a: max((x.mtime_ns for x in a["tracks"]), default=0),
                             reverse=True)[:12]
        self.area.redraw()

    def _draw(self) -> float:
        t, c = self.theme, self.c
        W = c.winfo_width()
        self.hits = []
        m = self.MARGIN
        c.create_text(m, 40, text=tr("Library"), anchor="w", fill=t.c("text"), font=t.f("title"))
        cols = max(1, int((W - 2 * m + self.GAP) // (self.TILE_W + self.GAP)))
        if i18n.language() != "en":  # fewer, wider tiles rather than cut names (Japanese names are longer)
            widest = max(t.f("tile").measure(tr(name)) for _, name, _ in TILES)
            while cols > 1 and (W - 2 * m - (cols - 1) * self.GAP) / cols - 66 < widest:
                cols -= 1
        tw = (W - 2 * m - (cols - 1) * self.GAP) / cols
        y0 = 78
        for i, (key, name, icon) in enumerate(TILES):
            r, k = divmod(i, cols)
            x = m + k * (tw + self.GAP)
            y = y0 + r * (self.TILE_H + self.GAP)
            box = (x, y, x + tw, y + self.TILE_H)
            hov = self.hover == box
            round_rect(c, *box, 10, fill=t.c("hover" if hov else "lcd"), outline="")
            draw_icon(c, icon, x + 30, y + self.TILE_H / 2, 22, t.c("accent"), filled=icon in ("heart",))
            c.create_text(x + 56, y + 24, text=fit_text(t.f("tile"), tr(name), int(tw - 66), self._fit), anchor="w",
                          fill=t.c("text"), font=t.f("tile"))
            c.create_text(x + 56, y + 44, text=fit_text(t.f("small"), self.counts.get(key, ""), int(tw - 66),
                                                        self._fit), anchor="w", fill=t.c("text2"), font=t.f("small"))
            self.hits.append((box, lambda key=key: self.app.show_page(key)))
        y = y0 + ((len(TILES) + cols - 1) // cols) * (self.TILE_H + self.GAP) + 20
        if self.newest:
            c.create_text(m, y + 12, text=tr("Recently Added"), anchor="w", fill=t.c("text"), font=t.f("h2"))
            more = c.create_text(W - m, y + 12, text=tr("See All"), anchor="e", fill=t.c("accent"),
                                 font=t.f("body_b"))
            bb = c.bbox(more)
            self.hits.append(((bb[0], bb[1], bb[2], bb[3]), lambda: self.app.show_page("recent")))
            y += 32
            n = max(1, int((W - 2 * m + 20) // (self.CARD + 20)))
            gap = (W - 2 * m - n * self.CARD) / max(n - 1, 1) if n > 1 else 0
            for i, a in enumerate(self.newest[:n]):
                x = m + i * (self.CARD + gap)
                img = self.app.covers.get(a["key"], [tr.path for tr in a["tracks"]], self.CARD)
                draw_cover(c, t, img, x, y, self.CARD)
                c.create_text(x, y + self.CARD + 15, text=fit_text(t.f("card_title"), tr_name(a["title"]), self.CARD,
                                                                   self._fit), anchor="w", fill=t.c("text"),
                              font=t.f("card_title"))
                c.create_text(x, y + self.CARD + 32, text=fit_text(t.f("card_sub"), tr_name(a["artist"]), self.CARD,
                                                                   self._fit), anchor="w", fill=t.c("text2"),
                              font=t.f("card_sub"))
                self.hits.append(((x, y, x + self.CARD, y + self.CARD + 40), lambda a=a: self.app.open_album(a)))
            y += self.CARD + 56
        if self.year_minutes:
            box = (m, y, W - m, y + 84)
            accent = t.c("accent")
            round_rect(c, *box, 14, fill=mix(accent, "#000000", 0.1) if self.hover == box else accent, outline="")
            draw_icon(c, "stats", m + 40, y + 42, 30, t.c("on_accent"))
            c.create_text(m + 76, y + 30, text=tr("Your Stats"), anchor="w", fill=t.c("on_accent"), font=t.f("h2"))
            c.create_text(m + 76, y + 56, text=tr("You listened to {n} minutes of music this year.",
                                                  n=f"{self.year_minutes:,}"),
                          anchor="w", fill=t.c("on_accent"), font=t.f("body"))
            draw_icon(c, "chevron", W - m - 28, y + 42, 18, t.c("on_accent"))
            self.hits.append((box, lambda: self.app.show_page("stats")))
            y += 84
        return y + 30

    def _hit(self, e) -> Optional[tuple]:
        x, y = self.c.canvasx(e.x), self.c.canvasy(e.y)
        for box, fn in self.hits:
            if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                return box, fn
        return None

    def _click(self, e) -> None:
        hit = self._hit(e)
        if hit:
            hit[1]()

    def _motion(self, e) -> None:
        hit = self._hit(e)
        box = hit[0] if hit else None
        self.c.configure(cursor="hand2" if hit else "")
        if box != self.hover:
            self.hover = box
            self.area.redraw()

    def songs(self) -> List[Track]:
        return album_order(self.app.tracks)


# ------------------------------------------------------------------ folders ---
class FoldersPage(Page):
    """The library folders as the roots; a folder shows its subfolders above its songs,
    with a breadcrumb whose parts can be clicked."""

    list_name = N_("Folders")

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        self.folder: Optional[str] = None
        top = frame(self, t)
        top.pack(fill="x", padx=28, pady=(22, 8))
        self.crumbs = frame(top, t)
        self.crumbs.pack(side="left", fill="x", expand=True)
        PillButton(top, t, tr("Shuffle"), icon="shuffle", command=lambda: self._play(True)).pack(side="right")
        PillButton(top, t, tr("Play"), icon="play", command=lambda: self._play(False)).pack(side="right",
                                                                                            padx=(0, 10))
        self.sub = label(self, t, "", "body", fg="text2")
        self.sub.pack(anchor="w", padx=28, pady=(0, 6))
        self.dirs = SongList(self, t, [Column("icon", "", 30, kind="icon"), Column("name", "", 300, True),
                                       Column("count", "", 90, anchor="e")],
                             lambda r, k: "folder" if k == "icon" else (r[1] if k == "name" else plural(r[2], "song")),
                             header=False, zebra=False, row_height=36,
                             on_select=lambda i: self.open(self.dirs.rows[i][0]))
        self.table = song_table(self, self, on_activate=lambda i: app.play_list(self.table.rows, i, self.list_name))
        self.parts: List[Tuple[str, Optional[str]]] = []

    def roots(self) -> List[str]:
        return [os.path.abspath(f) for f in self.app.cfg.library_folders if os.path.isdir(f)]

    def show(self, folder: Optional[str] = None, **kw) -> None:
        if folder is not None or "folder" in kw:
            self.folder = folder
        self.refresh()

    def open(self, folder: Optional[str]) -> None:
        self.folder = folder
        self.refresh()

    def refresh(self) -> None:
        roots = self.roots()
        if self.folder is None and len(roots) == 1:
            self.folder = roots[0]
        if self.folder is not None and not any(self.folder == r or self.folder.startswith(r.rstrip(os.sep) + os.sep)
                                               for r in roots):
            self.folder = roots[0] if len(roots) == 1 else None
        tracks = self.app.tracks
        if self.folder is None:
            subs = [(r, os.path.basename(r.rstrip(os.sep)) or r,
                     sum(1 for x in tracks if x.path.startswith(r.rstrip(os.sep) + os.sep))) for r in roots]
            songs: List[Track] = []
        else:
            subs, songs = musiclib.folder_view(tracks, self.folder)
        self._crumbs(roots)
        self.dirs.set_rows(subs)
        self.dirs.selected = None
        self.table.set_rows(songs)
        below = self._below()
        self.sub.configure(text=tr("{a}, {b}", a=plural(len(subs), "folder"), b=plural(len(below), "song"))
                           if (subs or below) else tr("No songs in this folder."))
        self.dirs.pack_forget()
        self.table.pack_forget()
        if subs:
            self.dirs.configure(height=min(len(subs), 7) * 36 + 6)
            self.dirs.pack(fill="x" if songs else "both", expand=not songs, padx=12, pady=(0, 8))
        if songs:
            self.table.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.list_name = self.parts[-1][0] if self.parts else tr("Folders")

    def _crumbs(self, roots: List[str]) -> None:
        """'Music › Jazz › 1999': every part opens its folder."""
        t = self.theme
        for w in self.crumbs.winfo_children():
            w.destroy()
        t.prune()
        parts: List[Tuple[str, Optional[str]]] = []
        if len(roots) != 1:
            parts.append((tr("Folders"), None))
        if self.folder is not None:
            root = next(r for r in roots if self.folder == r or self.folder.startswith(r.rstrip(os.sep) + os.sep))
            parts.append((os.path.basename(root.rstrip(os.sep)) or root, root))
            rel = os.path.relpath(self.folder, root)
            if rel != ".":
                cur = root
                for piece in rel.split(os.sep):
                    cur = os.path.join(cur, piece)
                    parts.append((piece, cur))
        self.parts = parts
        for i, (name, path) in enumerate(parts):
            last = i == len(parts) - 1
            if i:
                label(self.crumbs, t, "›", "title", fg="text2").pack(side="left", padx=8)
            lab = label(self.crumbs, t, name, "title", fg="text" if last else "accent",
                        cursor="" if last else "hand2")
            lab.pack(side="left")
            if not last:
                lab.bind("<Button-1>", lambda e, p=path: self.open(p))

    def _below(self) -> List[Track]:
        if self.folder is None:
            roots = self.roots()
            return [x for x in self.app.tracks if any(x.path.startswith(r.rstrip(os.sep) + os.sep) for r in roots)]
        prefix = self.folder.rstrip(os.sep) + os.sep
        return [x for x in self.app.tracks if x.path.startswith(prefix)]

    def songs(self) -> List[Track]:
        return album_order(self._below())

    def likes_changed(self) -> None:
        self.table.redraw()

    def song_changed(self) -> None:
        self.table.redraw()


# ---------------------------------------------------------------- playlists ---
class PlaylistsPage(Page):
    list_name = N_("Playlists")

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        top = frame(self, t)
        top.pack(fill="x", padx=28, pady=(22, 8))
        box = frame(top, t)
        box.pack(side="left", fill="x", expand=True)
        label(box, t, tr("Playlists"), "title").pack(anchor="w")
        self.sub = label(box, t, "", "body", fg="text2")
        self.sub.pack(anchor="w")
        PillButton(top, t, tr("New Playlist…"), icon="plus", command=lambda: app.new_playlist()).pack(side="right")
        self.grid_w = AlbumGrid(self, t, app.covers, self._open)
        self.grid_w.pack(fill="both", expand=True)
        self.refresh()

    def refresh(self) -> None:
        app = self.app
        items = []
        for p in app.state.playlists:
            items.append({"key": p["id"], "title": p["name"], "artist": plural(len(p["songs"]), "song"),
                          "mosaic": app.playlist_mosaic(p), "tracks": []})
        items.append({"key": "__new__", "title": tr("New Playlist…"), "artist": "", "new": True, "tracks": []})
        self.sub.configure(text=plural(len(app.state.playlists), "playlist"))
        self.grid_w.set_albums(items, keep=True)

    def _open(self, item: dict) -> None:
        if item.get("new"):
            self.app.new_playlist()
        else:
            self.app.open_playlist(item["key"])

    def songs(self) -> List[Track]:
        out: List[Track] = []
        for p in self.app.state.playlists:
            out += [t for t in self.app.playlist_tracks(p["id"]) if not getattr(t, "missing", False)]
        return out


class PlaylistPage(Page):
    COVER = 220

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        self.pid: Optional[str] = None
        top = frame(self, t)
        top.pack(fill="x", padx=28, pady=(18, 10))
        IconButton(top, t, "back", command=app.go_back, size=30, icon_size=16,
                   tooltip=lambda: tr("Back")).pack(side="left", anchor="n", padx=(0, 6))
        self.cover = tk.Canvas(top, width=self.COVER, height=self.COVER, highlightthickness=0, bd=0)
        t.reg(self.cover, bg="bg")
        self.cover.pack(side="left")
        info = frame(top, t)
        info.pack(side="left", fill="both", expand=True, padx=(24, 0))
        label(info, t, tr("Playlist"), "small_b", fg="text2").pack(anchor="w", pady=(58, 0))
        self.title_l = label(info, t, "", "big", wraplength=600, justify="left")
        self.title_l.pack(anchor="w", pady=(0, 2))
        self.meta_l = label(info, t, "", "body", fg="text2")
        self.meta_l.pack(anchor="w", pady=(2, 14))
        btns = frame(info, t)
        btns.pack(anchor="w")
        PillButton(btns, t, tr("Play"), icon="play", command=lambda: self._play(False)).pack(side="left")
        PillButton(btns, t, tr("Shuffle"), icon="shuffle", command=lambda: self._play(True)).pack(side="left",
                                                                                                  padx=(10, 0))
        PillButton(btns, t, tr("Rename…"), kind="plain", command=lambda: self.pid and app.rename_playlist(self.pid)
                   ).pack(side="left", padx=(18, 0))
        PillButton(btns, t, tr("Delete…"), kind="plain", command=lambda: self.pid and app.delete_playlist(self.pid)
                   ).pack(side="left", padx=(8, 0))
        self.pos: Dict[int, int] = {}
        cols = [Column("pos", "", 34, anchor="e", kind="num"), Column("title", N_("Title"), 300, True),
                Column("artist", N_("Artist"), 200, True), Column("album", N_("Album"), 200, True),
                Column("time", N_("Time"), 52, anchor="e"), Column("like", "", 30, kind="heart")]
        self.table = song_table(self, self, cols, value=self._value, extra_menu=self._extra, sortable=False,
                                on_activate=lambda i: self.app.play_list(self.table.rows, i, self.list_name),
                                empty_text=tr("This playlist is empty. Add songs with “Add to playlist” in a "
                                              "song's menu (right click)."))
        self.table.pack(fill="both", expand=True, padx=(16, 16))
        app.covers.listeners.append(lambda key, size: size in (self.COVER, self.COVER // 2 - 1) and self.winfo_exists()
                                    and self._draw_cover())
        t.on_change(self._draw_cover)

    def _value(self, row: Track, key: str) -> str:
        if key == "pos":
            return str(self.pos.get(id(row), ""))
        return track_value(row, key)

    def _extra(self, i: int):
        row = self.table.rows[i]
        pid = self.pid
        return [(tr("Remove from this playlist"), lambda: self.app.playlist_remove(pid, row.path)),
                (tr("Move up"), lambda: self.app.playlist_move(pid, row.path, -1)),
                (tr("Move down"), lambda: self.app.playlist_move(pid, row.path, 1))]

    def show(self, pid: Optional[str] = None, **kw) -> None:
        if pid is not None:
            self.pid = pid
        self.refresh()

    def refresh(self) -> None:
        p = self.app.state.playlist(self.pid) if self.pid else None
        if p is None:
            self.title_l.configure(text="")
            self.table.set_rows([])
            return
        self.list_name = p["name"]
        rows = self.app.playlist_tracks(self.pid)
        self.pos = {id(r): i + 1 for i, r in enumerate(rows)}
        self.title_l.configure(text=p["name"])
        minutes = int(round(sum(r.duration for r in rows) / 60))
        self.meta_l.configure(text=tr("{a}, {b}", a=plural(len(rows), "song"), b=plural(minutes, "minute")))
        self.table.set_rows(rows, keep=True)
        self._draw_cover()

    def _draw_cover(self) -> None:
        p = self.app.state.playlist(self.pid) if self.pid else None
        self.cover.delete("all")
        if p is not None:
            draw_mosaic(self.cover, self.theme, self.app.covers, self.app.playlist_mosaic(p), 0, 0, self.COVER)

    def songs(self) -> List[Track]:
        return list(self.table.rows)

    def likes_changed(self) -> None:
        self.table.redraw()

    def song_changed(self) -> None:
        self.table.redraw()


# ----------------------------------------------------------- recently played ---
class RecentlyPlayedPage(Page):
    list_name = N_("Recently Played")

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        head, self.title_l, self.sub = self.heading(self, tr("Recently Played"))
        head.pack(fill="x", padx=28, pady=(22, 8))
        PillButton(head, t, tr("Clear history…"), kind="plain", command=app.clear_history).pack(side="right",
                                                                                                padx=(0, 10))
        self.when: Dict[str, float] = {}
        cols = [Column("title", N_("Title"), 300, True), Column("artist", N_("Artist"), 200, True),
                Column("album", N_("Album"), 200, True), Column("played", N_("Played"), 120),
                Column("time", N_("Time"), 52, anchor="e"), Column("like", "", 30, kind="heart")]
        self.table = song_table(self, self, cols, value=self._value, sort_value=self._sort,
                                on_activate=lambda i: app.play_list(self.table.rows, i, self.list_name),
                                empty_text=tr("Songs you play appear here."))
        self.table.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.refresh()

    def _value(self, row: Track, key: str) -> str:
        if key == "played":
            ts = self.when.get(row.path)
            return fmt_played(ts) if ts else ""
        return track_value(row, key)

    def _sort(self, row: Track, key: str):
        if key == "played":
            return -self.when.get(row.path, 0.0)
        return track_sort(row, key)

    def refresh(self) -> None:
        app = self.app
        paths = app.recently_played()
        self.when = app.last_played()
        rows = [app.by_path.get(p) or missing_track(p) for p in paths]
        self.table.set_rows(rows)
        self.sub.configure(text=plural(len(rows), "song") if rows else "")

    def songs(self) -> List[Track]:
        return [r for r in self.table.rows if not getattr(r, "missing", False)]

    def likes_changed(self) -> None:
        self.table.redraw()

    def song_changed(self) -> None:
        self.table.redraw()


# --------------------------------------------------------------- your stats ---
class StatsPage(Page):
    """A period menu and the summary cards (stats.cards()), each in its own colour."""

    list_name = N_("Your Stats")

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        top = frame(self, t)
        top.pack(fill="x", padx=28, pady=(22, 8))
        box = frame(top, t)
        box.pack(side="left", fill="x", expand=True)
        label(box, t, tr("Your Stats"), "title").pack(anchor="w")
        self.sub = label(box, t, "", "body", fg="text2")
        self.sub.pack(anchor="w")
        PillButton(top, t, tr("Details"), icon="stats", command=lambda: app.show_page("stats_details")).pack(
            side="right")
        self.period = tk.StringVar(value=tr(app.stats_period))  # the period shown; app.stats_period is English
        self.period_box = cb = ttk.Combobox(top, textvariable=self.period, values=[tr(p) for p in stats.PERIODS],
                                            state="readonly", width=14, style="P.TCombobox", font=t.f("body"))
        cb.pack(side="right", padx=(0, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self._period())
        self.area = ScrollCanvas(self, self, self._draw)
        self.area.pack(fill="both", expand=True)
        self.st: Optional[dict] = None
        self._fit: dict = {}

    def _period(self) -> None:
        i = self.period_box.current()
        if 0 <= i < len(stats.PERIODS):
            self.app.stats_period = stats.PERIODS[i]
        self.refresh()

    def show(self, **kw) -> None:
        self.period.set(tr(self.app.stats_period))
        self.refresh()

    def refresh(self) -> None:
        self.st = self.app.compute_stats()
        st = self.st
        period = tr(self.app.stats_period)
        self.sub.configure(text=tr("{period}: {minutes}, {plays}", period=period,
                                   minutes=plural(int(st["seconds"] / 60 + 0.5), "minute"),
                                   plays=plural(st["plays"], "play")) if st["logged"] else period)
        self.area.redraw()

    def _draw(self) -> float:
        t, c = self.theme, self.area.c
        W = c.winfo_width()
        m, gap = 28, 16
        cards = stats.cards(self.st) if self.st else []
        if not cards:
            c.create_text(W / 2, 120, text=tr("No listening yet in this period. Play some music and come back."),
                          fill=t.c("text2"), font=t.f("h2"), width=W - 80)
            return 200
        cols = 3 if W >= 760 else 2 if W >= 480 else 1
        cw = (W - 2 * m - (cols - 1) * gap) / cols
        ch = 150
        y = 8
        for i, (lab, big, detail) in enumerate(cards):
            # stats.cards() gives English texts: the label and the detail are the core's sentences, the
            # big text a name from the library (top song, artist...) or a value such as "3 PM"
            big = tr_name(big) if lab in ("Your top song", "Your top artist", "Your top album",
                                          "Your top genre") else tr_text(big)
            lab, detail = tr_text(lab), tr_text(detail)
            r, k = divmod(i, cols)
            x, y = m + k * (cw + gap), 8 + r * (ch + gap)
            color = CARD_COLORS[i % len(CARD_COLORS)]
            round_rect(c, x, y, x + cw, y + ch, 16, fill=color, outline="")
            c.create_text(x + 20, y + 26, text=lab.upper(), anchor="w", fill=mix(color, "#ffffff", 0.78),
                          font=t.f("stat_label"))
            c.create_text(x + 20, y + 72, text=fit_text(t.f("stat_big"), big, int(cw - 40), self._fit), anchor="w",
                          fill="#ffffff", font=t.f("stat_big"))
            c.create_text(x + 20, y + 118, text=fit_text(t.f("stat_detail"), detail, int(cw - 40), self._fit),
                          anchor="w", fill=mix(color, "#ffffff", 0.85), font=t.f("stat_detail"))
        rows = (len(cards) + cols - 1) // cols
        return 8 + rows * (ch + gap) + 20


class StatsDetailsPage(Page):
    """The totals, listening by hour, weekday and month, the top 50 lists and what
    the library holds."""

    list_name = N_("Your Stats")

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        top = frame(self, t)
        top.pack(fill="x", padx=28, pady=(18, 8))
        IconButton(top, t, "back", command=app.go_back, size=30, icon_size=16,
                   tooltip=lambda: tr("Back")).pack(side="left", padx=(0, 6))
        box = frame(top, t)
        box.pack(side="left", fill="x", expand=True)
        label(box, t, tr("Details"), "title").pack(anchor="w")
        self.sub = label(box, t, "", "body", fg="text2")
        self.sub.pack(anchor="w")
        self.area = ScrollCanvas(self, self, self._draw)
        self.area.pack(fill="both", expand=True)
        self.st: Optional[dict] = None
        self.lib: Optional[dict] = None
        self._fit: dict = {}

    def show(self, **kw) -> None:
        self.refresh()

    def refresh(self) -> None:
        self.st = self.app.compute_stats()
        self.lib = stats.library_stats(self.app.tracks)
        self.sub.configure(text=tr("{period} · Your Stats", period=tr(self.app.stats_period)))
        self.area.redraw()

    # ---- drawing helpers ------------------------------------------------
    def _heading(self, y: float, text: str) -> float:
        t = self.theme
        self.area.c.create_text(28, y + 12, text=text, anchor="w", fill=t.c("text"), font=t.f("h2"))
        return y + 36

    def _bars(self, y: float, values: List[float], labels: List[str], every: int = 1, height: int = 110) -> float:
        t, c = self.theme, self.area.c
        W = c.winfo_width()
        x0, x1 = 28, W - 28
        n = max(len(values), 1)
        slot = (x1 - x0) / n
        top = max(values) if values and max(values) > 0 else 1.0
        c.create_line(x0, y + height, x1, y + height, fill=t.c("sep"))
        for i, v in enumerate(values):
            h = (height - 4) * v / top
            bx = x0 + i * slot + slot * 0.18
            if h > 0.5:
                round_rect(c, bx, y + height - h, bx + slot * 0.64, y + height, min(4, slot * 0.2),
                           fill=t.c("accent"), outline="")
            if i % every == 0 and i < len(labels):
                c.create_text(x0 + i * slot + slot / 2, y + height + 12, text=labels[i], fill=t.c("text2"),
                              font=t.f("small"))
        return y + height + 34

    def _top(self, x: float, y: float, width: float, title: str, rows: List[dict], with_artist: bool) -> float:
        t, c = self.theme, self.area.c
        c.create_text(x, y + 12, text=title, anchor="w", fill=t.c("text"), font=t.f("h2"))
        y += 34
        if not rows:
            c.create_text(x, y + 10, text=tr("Nothing yet."), anchor="w", fill=t.c("text2"), font=t.f("body"))
            return y + 30
        for i, r in enumerate(rows[:50], start=1):
            right = tr("{plays} · {minutes} min", plays=plural(r["plays"], "play"),
                       minutes=f"{int(r['seconds'] / 60 + 0.5):,}")
            rid = c.create_text(x + width, y + 10, text=right, anchor="e", fill=t.c("text2"), font=t.f("small"))
            rw = c.bbox(rid)[2] - c.bbox(rid)[0]
            c.create_text(x, y + 10, text=f"{i}", anchor="w", fill=t.c("text2"), font=t.f("small_b"))
            name = tr_name(r["name"]) + (f" — {tr_name(r['artist'])}" if with_artist and r.get("artist") else "")
            c.create_text(x + 30, y + 10, text=fit_text(t.f("body"), name, int(width - rw - 44), self._fit),
                          anchor="w", fill=t.c("text"), font=t.f("body"))
            y += 24
        return y + 12

    def _pairs(self, y: float, pairs: List[Tuple[str, str]]) -> float:
        t, c = self.theme, self.area.c
        for k, v in pairs:
            c.create_text(28, y + 10, text=k, anchor="w", fill=t.c("text2"), font=t.f("body"))
            c.create_text(220, y + 10, text=v, anchor="w", fill=t.c("text"), font=t.f("body"),
                          width=c.winfo_width() - 250)
            y += 24
        return y + 10

    def _draw(self) -> float:
        from .fix import fmt_bytes
        from .player import fmt_khz

        t, c = self.theme, self.area.c
        W = c.winfo_width()
        st, lib = self.st, self.lib
        y = 8.0
        if st is None or lib is None:
            return 100
        if not st["logged"]:
            c.create_text(28, y + 14, text=tr("No listening yet in this period. Play some music and come back."),
                          anchor="w", fill=t.c("text2"), font=t.f("body"))
            y += 50
        else:
            totals = [(f"{int(st['seconds'] / 60 + 0.5):,}", tr("minutes")), (f"{st['plays']:,}", tr("plays")),
                      (f"{st['songs']:,}", tr("songs")), (f"{st['artists']:,}", tr("artists")),
                      (f"{st['albums']:,}", tr("albums")), (f"{st['genres']:,}", tr("genres")),
                      (f"{st['days']:,}", tr("days with music")),
                      (f"{st['streak']:,}", tr("longest streak (days)"))]
            cols = 4
            bw = (W - 56 - (cols - 1) * 12) / cols
            for i, (big, lab) in enumerate(totals):
                r, k = divmod(i, cols)
                x, yy = 28 + k * (bw + 12), y + r * 84
                round_rect(c, x, yy, x + bw, yy + 72, 12, fill=t.c("lcd"), outline="")
                c.create_text(x + 16, yy + 28, text=big, anchor="w", fill=t.c("accent"), font=t.f("h1"))
                c.create_text(x + 16, yy + 54, text=lab, anchor="w", fill=t.c("text2"), font=t.f("small"))
            y += 2 * 84 + 14
            hours = [s / 60 for s in st["by_hour"]]
            y = self._heading(y, tr("Listening by hour of the day (minutes)"))
            y = self._bars(y, hours, [tr_text(stats.hour_label(h)) for h in range(24)], every=3)
            y = self._heading(y, tr("By weekday"))
            y = self._bars(y, [s / 60 for s in st["by_weekday"]], [tr(d) for d in WEEKDAYS_SHORT])
            months = st["by_month"]
            years = {m[:4] for m, _ in months}
            labels = [tr(stats.MONTHS[int(m[5:7]) - 1]) if len(years) == 1 else
                      tr("{month} {year}", month=tr(stats.MONTHS[int(m[5:7]) - 1]), year=m[2:4]) for m, _ in months]
            y = self._heading(y, tr("By month"))
            y = self._bars(y, [s / 60 for _, s in months], labels, every=max(1, len(months) // 12 + 1))
            half = (W - 56 - 32) / 2
            y1 = self._top(28, y, half, tr("Top songs"), st["top_songs"], True)
            y2 = self._top(28 + half + 32, y, half, tr("Top artists"), st["top_artists"], False)
            y = max(y1, y2)
            y1 = self._top(28, y, half, tr("Top albums"), st["top_albums"], True)
            y2 = self._top(28 + half + 32, y, half, tr("Top genres"), st["top_genres"], False)
            y = max(y1, y2)
        y = self._heading(y, tr("Your library"))
        hours = lib["seconds"] / 3600.0
        length = (tr("{n} hours", n=f"{hours:,.1f}") if hours < 48 else tr("{n} days", n=f"{hours / 24:,.1f}"))
        sep = tr(", ")
        pairs = [(tr("Songs"), f"{lib['songs']:,}"), (tr("Albums"), f"{lib['albums']:,}"),
                 (tr("Artists"), f"{lib['artists']:,}"), (tr("Total length"), length),
                 (tr("Size"), fmt_bytes(lib["bytes"]) if lib["bytes"] else "—"),
                 (tr("Formats"), sep.join(f"{k} {v:,}" for k, v in lib["formats"]) or "—"),
                 (tr("Quality"), sep.join(f"{tr_text(k)} {v:,}" for k, v in lib["qualities"])),
                 (tr("Sample rates"), sep.join(f"{tr_text(fmt_khz(k))} {v:,}" for k, v in lib["rates"]) or "—"),
                 (tr("Bit depths"), sep.join(f"{tr_text(k)} {v:,}" for k, v in lib["bits"]) or "—")]
        return self._pairs(y, pairs) + 20


__all__ = ["LibraryPage", "FoldersPage", "PlaylistsPage", "PlaylistPage", "RecentlyPlayedPage", "StatsPage",
           "StatsDetailsPage", "fmt_played", "missing_track", "TILES"]
