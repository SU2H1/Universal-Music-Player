"""The pages of the 2.0 player's content area: Recently Added, Albums, an album,
Artists, Songs, Genres, Liked Songs, Search, Now Playing and Settings (the
Quality check page is the 1.x scanner, `gui.App`)."""

from __future__ import annotations

import os
import time
import tkinter as tk
import unicodedata
from tkinter import ttk
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from . import embed, i18n
from . import lyrics as lyrics_mod
from . import musiclib
from . import settings as settings_mod
from .i18n import N_, tr, tr_name, tr_text
from .musiclib import Track
from .player_theme import DEFAULT_FONT_LABEL, IconButton, PillButton, Theme, fit_text, font_families, round_rect
from .player_widgets import AlbumGrid, Column, SongList, bind_wheel, draw_cover, fmt_time, show_scrollbar

if TYPE_CHECKING:  # pragma: no cover
    from .player_gui import PlayerApp


# ------------------------------------------------------------- helpers ---
def label(parent, theme: Theme, text: str = "", font: str = "body", fg: str = "text", bg: str = "bg",
          **kw) -> tk.Label:
    w = tk.Label(parent, text=text, font=theme.f(font), anchor=kw.pop("anchor", "w"), bd=0, **kw)
    return theme.reg(w, fg=fg, bg=bg)


def frame(parent, theme: Theme, bg: str = "bg", **kw) -> tk.Frame:
    return theme.reg(tk.Frame(parent, bd=0, highlightthickness=0, **kw), bg=bg)


def track_value(t: Track, key: str) -> str:
    if key == "title":
        return t.display_title
    if key == "artist":
        return tr("Not in the library") if getattr(t, "missing", False) else tr_name(t.display_artist)
    if key == "album":
        return tr_name(t.display_album)
    if key == "genre":
        return t.genre
    if key == "time":
        return fmt_time(t.duration)
    if key == "quality":
        return tr_text(musiclib.quality(t))
    if key == "num":
        return str(t.track) if t.track else ""
    if key == "year":
        return t.year
    if key == "composer":
        return t.composer
    return ""


def track_sort(t: Track, key: str):
    if key == "time":
        return t.duration
    if key == "num":
        return (t.disc or 9999, t.track or 9999, t.display_title.casefold())
    return (track_value(t, key).casefold(), t.display_title.casefold(), t.path)


def album_order(tracks: List[Track]) -> List[Track]:
    return [t for a in musiclib.albums(tracks) for t in a["tracks"]]


# the counts the pages show ("1 song", "12 songs"), as texts to translate
COUNTS = {
    "song": (N_("1 song"), N_("{n} songs")), "album": (N_("1 album"), N_("{n} albums")),
    "minute": (N_("1 minute"), N_("{n} minutes")), "artist": (N_("1 artist"), N_("{n} artists")),
    "album artist": (N_("1 album artist"), N_("{n} album artists")), "genre": (N_("1 genre"), N_("{n} genres")),
    "year": (N_("1 year"), N_("{n} years")), "composer": (N_("1 composer"), N_("{n} composers")),
    "folder": (N_("1 folder"), N_("{n} folders")), "playlist": (N_("1 playlist"), N_("{n} playlists")),
    "play": (N_("1 play"), N_("{n} plays")),
}


def plural(n: int, one: str, many: str = "") -> str:
    forms = COUNTS.get(one)
    if forms is None:
        return f"{n:,} {one if n == 1 else (many or one + 's')}"
    return tr(forms[0]) if n == 1 else tr(forms[1], n=f"{n:,}")


def text_width(text: str) -> int:
    """The width of a text in average characters (ttk widths): a Japanese character counts twice."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


SONG_COLUMNS = [Column("title", N_("Title"), 300, True), Column("artist", N_("Artist"), 200, True),
                Column("album", N_("Album"), 200, True), Column("genre", N_("Genre"), 110),
                Column("time", N_("Time"), 52, anchor="e"), Column("quality", N_("Quality"), 118),
                Column("like", "", 30, kind="heart")]


class Page(tk.Frame):
    """One page of the content area."""

    list_name = ""

    def __init__(self, app: "PlayerApp"):
        # a requested size: a gridded frame that asks for 0x0 (only placed children) is never shown
        super().__init__(app.content, bd=0, highlightthickness=0, width=400, height=300)
        self.app, self.theme = app, app.theme
        self.theme.reg(self, bg="bg")
        if self.list_name:  # a class's list name (N_()) in the language in use ("Playing from …")
            self.list_name = tr(self.list_name)

    def show(self, **kw) -> None:
        pass

    def refresh(self) -> None:
        """The library changed."""

    def songs(self) -> List[Track]:
        """What the Play button of the playback bar plays when nothing plays."""
        return album_order(self.app.tracks)

    def likes_changed(self) -> None:
        pass

    def song_changed(self) -> None:
        pass

    def heading(self, parent, title: str, buttons: bool = True, back: bool = False):
        """The page title, a grey line under it, and Play / Shuffle."""
        t = self.theme
        top = frame(parent, t)
        if back:
            IconButton(top, t, "back", command=self.app.go_back, size=30, icon_size=16,
                       tooltip=lambda: tr("Back")).pack(side="left", padx=(0, 6))
        box = frame(top, t)
        box.pack(side="left", fill="x", expand=True)
        title_l = label(box, t, title, "title")
        title_l.pack(anchor="w")
        sub = label(box, t, "", "body", fg="text2")
        sub.pack(anchor="w")
        if buttons:
            PillButton(top, t, tr("Shuffle"), icon="shuffle", command=lambda: self._play(True)).pack(side="right")
            PillButton(top, t, tr("Play"), icon="play", command=lambda: self._play(False)).pack(side="right",
                                                                                                padx=(0, 10))
        return top, title_l, sub

    def _play(self, shuffle: bool) -> None:
        songs = self.songs()
        if songs:
            self.app.play_list(songs, 0, self.list_name, shuffle=shuffle)


def song_table(page: Page, parent, columns=None, value=None, extra_menu=None, sort_value=None, **kw) -> SongList:
    """A song table with likes, the playing mark and the song menu (right click);
    `extra_menu(index)` gives more (label, command) entries, e.g. a playlist's."""
    app = page.app
    table: SongList

    def context(i: int, e) -> None:
        app.song_menu(table.rows[i], extra_menu(i) if extra_menu else (), list_name=page.list_name,
                      rows=table.rows, index=i).tk_popup(e.x_root, e.y_root)

    table = SongList(parent, page.theme, columns or SONG_COLUMNS, value or track_value, sort_value or track_sort,
                     on_heart=lambda r: app.toggle_like(r.path), is_liked=lambda r: app.is_liked(r.path),
                     is_current=lambda r: r.path == app.current, on_context=context,
                     is_dim=lambda r: getattr(r, "missing", False), **kw)
    return table


# --------------------------------------------------------------- albums ---
class AlbumsPage(Page):
    """Album cards: all albums by title, or (Recently Added) the newest first."""

    def __init__(self, app, title: str, recent: bool = False):
        super().__init__(app)
        self.title, self.recent, self.list_name = title, recent, title
        t = self.theme
        head, self.title_l, self.sub = self.heading(self, title, buttons=False)
        head.pack(fill="x", padx=28, pady=(22, 6))
        self.empty = frame(self, t)
        self.empty_text = label(self.empty, t, "", "h2", fg="text2", anchor="center")
        self.empty_text.pack(pady=(80, 6))
        self.empty_sub = label(self.empty, t, "", "body", fg="text2", anchor="center", justify="center",
                               wraplength=460)
        self.empty_sub.pack(pady=(0, 14))
        self.choose = PillButton(self.empty, t, tr("Choose a music folder…"), command=app.choose_music_folder)
        self.choose.pack()
        self.grid_w = AlbumGrid(self, t, app.covers, app.open_album)
        self.albums: List[dict] = []
        self.refresh()

    def refresh(self) -> None:
        albums = list(self.app.albums)
        if self.recent:
            albums.sort(key=lambda a: max((tr.mtime_ns for tr in a["tracks"]), default=0), reverse=True)
        self.albums = albums
        n = len(albums)
        self.sub.configure(text=plural(n, "album") if n else "")
        if albums:
            self.empty.pack_forget()
            self.grid_w.pack(fill="both", expand=True)
            self.grid_w.set_albums(albums, keep=True)
            return
        self.grid_w.pack_forget()
        self.empty.pack(fill="both", expand=True)
        if not self.app.cfg.library_folders:
            self.empty_text.configure(text=tr("Add your music folder in Settings to start"))
            self.empty_sub.configure(text=tr("Universal Music Player lists every song in your music folders, plays "
                                             "them, and checks them for fake hi-res and fake lossless files."))
            self.choose.pack()
        else:
            busy = self.app.library_busy()
            self.empty_text.configure(text=tr("Reading your music library…") if busy else tr("No songs found"))
            self.empty_sub.configure(text="" if busy else tr("Your music folders hold no audio files with the "
                                                            "extensions set in Settings → Scanning."))
            self.choose.pack_forget()

    def songs(self) -> List[Track]:
        return [tr for a in self.albums for tr in a["tracks"]]


class AlbumPage(Page):
    COVER = 220

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        self.album: Optional[dict] = None
        top = frame(self, t)
        top.pack(fill="x", padx=28, pady=(18, 10))
        IconButton(top, t, "back", command=app.go_back, size=30, icon_size=16,
                   tooltip=lambda: tr("Back")).pack(side="left", anchor="n", padx=(0, 6))
        self.cover = tk.Canvas(top, width=self.COVER, height=self.COVER, highlightthickness=0, bd=0)
        t.reg(self.cover, bg="bg")
        self.cover.pack(side="left")
        info = frame(top, t)
        info.pack(side="left", fill="both", expand=True, padx=(24, 0))
        self.title_l = label(info, t, "", "big", wraplength=600, justify="left")
        self.title_l.pack(anchor="w", pady=(60, 2))
        self.artist_l = label(info, t, "", "h2", fg="accent", cursor="hand2")
        self.artist_l.pack(anchor="w")
        self.artist_l.bind("<Button-1>", lambda e: self.album and app.open_artist(self.album["artist"]))
        self.meta_l = label(info, t, "", "body", fg="text2")
        self.meta_l.pack(anchor="w", pady=(2, 14))
        btns = frame(info, t)
        btns.pack(anchor="w")
        PillButton(btns, t, tr("Play"), icon="play", command=lambda: self._play(False)).pack(side="left")
        PillButton(btns, t, tr("Shuffle"), icon="shuffle", command=lambda: self._play(True)).pack(side="left",
                                                                                                  padx=(10, 0))
        self.edit_b = PillButton(btns, t, tr("Edit album info…"), kind="plain", command=self.edit_info)
        self.edit_b.pack(side="left", padx=(10, 0))
        self.mb_b = PillButton(btns, t, tr("Get album info from MusicBrainz…"), kind="plain",
                               command=self.musicbrainz)
        self.mb_b.pack(side="left", padx=(10, 0))
        cols = [Column("num", "", 34, anchor="e", kind="num"), Column("title", "", 400, True),
                Column("like", "", 30, kind="heart"), Column("time", "", 52, anchor="e")]
        self.table = song_table(self, self, cols, header=False, sortable=False,
                                on_activate=lambda i: self.app.play_list(self.table.rows, i, self.list_name))
        self.table.pack(fill="both", expand=True, padx=(16, 16))
        app.covers.listeners.append(self._cover_ready)
        t.on_change(self._draw_cover)

    def show(self, album: Optional[dict] = None, **kw) -> None:
        if album is not None:
            self.album = album
        self._fill()

    def refresh(self) -> None:
        if self.album is not None:
            fresh = self.app.album_by_key.get(self.album["key"])
            if fresh is None and self.album["tracks"]:  # its title or album artist was edited
                first = self.app.track(self.album["tracks"][0].path)
                fresh = self.app.album_of(first) if first is not None else None
            if fresh is not None:
                self.album = fresh
        self._fill()

    def edit_info(self):
        """Edit album info…: the Edit Info window for every song of the album."""
        if self.album is not None:
            return self.app.edit_album_info(self.album)
        return None

    def musicbrainz(self):
        """Get album info from MusicBrainz… (2.1): the MusicBrainz window for the album."""
        if self.album is not None:
            return self.app.album_musicbrainz(self.album)
        return None

    def _fill(self) -> None:
        a = self.album
        if a is None:
            return
        self.list_name = tr_name(a["title"])
        self.title_l.configure(text=tr_name(a["title"]))
        self.artist_l.configure(text=tr_name(a["artist"]))
        meta = " · ".join(x for x in (a["genre"], a["year"]) if x)
        self.meta_l.configure(text=meta)
        self.table.set_rows(a["tracks"])
        minutes = int(round(sum(tr.duration for tr in a["tracks"]) / 60))
        self.table.footer = tr("{a}, {b}", a=plural(len(a["tracks"]), "song"), b=plural(minutes, "minute"))
        self.table.redraw()
        self._draw_cover()

    def _draw_cover(self) -> None:
        if self.album is None:
            return
        c = self.cover
        c.delete("all")
        img = self.app.covers.get(self.album["key"], [tr.path for tr in self.album["tracks"]], self.COVER)
        draw_cover(c, self.theme, img, 0, 0, self.COVER)

    def _cover_ready(self, key: str, size: int) -> None:
        if self.album is not None and key == self.album["key"] and size == self.COVER and self.winfo_exists():
            self._draw_cover()

    def songs(self) -> List[Track]:
        return list(self.album["tracks"]) if self.album else []

    def likes_changed(self) -> None:
        self.table.redraw()

    def song_changed(self) -> None:
        self.table.redraw()


# ------------------------------------------------------------ groupings ---
GROUPINGS = {  # page: (title, musiclib grouping, noun, what the right side shows)
    "artists": (N_("Artists"), musiclib.artists, "artist", "albums"),
    "album_artists": (N_("Album Artists"), musiclib.album_artists, "album artist", "albums"),
    "genres": (N_("Genres"), musiclib.genres, "genre", "songs"),
    "years": (N_("Years"), musiclib.years, "year", "songs"),
    "composers": (N_("Composers"), musiclib.composers, "composer", "songs"),
}


class GroupPage(Page):
    """Artists, Album Artists, Genres, Years, Composers: the names on the left with
    their song counts, the chosen one's albums (cards) or songs (a table) on the right."""

    def __init__(self, app, kind: str):
        super().__init__(app)
        t = self.theme
        self.kind = kind
        self.title, self.group_fn, self.noun, self.shows = GROUPINGS[kind]
        self.title = tr(self.title)
        self.list_name = self.title
        head, self.title_l, self.sub = self.heading(self, self.title, buttons=False)
        head.pack(fill="x", padx=28, pady=(22, 8))
        body = frame(self, t)
        body.pack(fill="both", expand=True)
        self.left = SongList(body, t, [Column("name", "", 200, True), Column("count", "", 44, anchor="e")],
                             lambda r, k: tr_name(r["name"]) if k == "name" else f"{len(r['tracks']):,}", header=False,
                             zebra=False, on_select=self._chosen, on_activate=self._activate, row_height=34)
        self.left.configure(width=270)
        self.left.pack(side="left", fill="y", padx=(12, 0))
        self.left.pack_propagate(False)
        frame(body, t, bg="sep", width=1).pack(side="left", fill="y", padx=(8, 0))
        self.right = frame(body, t)
        self.right.pack(side="left", fill="both", expand=True)
        rh, self.r_title, self.r_sub = self.heading(self.right, "", buttons=True)
        self.r_title.configure(font=t.f("h1"))
        rh.pack(fill="x", padx=24, pady=(6, 8))
        self.items: List[dict] = []
        self.chosen: Optional[dict] = None
        if self.shows == "albums":
            self.grid_w = AlbumGrid(self.right, t, app.covers, app.open_album)
            self.grid_w.pack(fill="both", expand=True)
        else:
            self.table = song_table(self, self.right, on_activate=lambda i: app.play_list(
                self.table.rows, i, self.list_name))
            self.table.pack(fill="both", expand=True, padx=(8, 12))
        self.refresh()

    def refresh(self) -> None:
        self.items = self.group_fn(self.app.tracks)
        self.sub.configure(text=plural(len(self.items), self.noun) if self.items else "")
        names = [i["name"] for i in self.items]
        keep = self.chosen["name"] if self.chosen else None
        self.left.set_rows(self.items, keep=True)
        idx = names.index(keep) if keep in names else (0 if self.items else None)
        self.left.selected = idx
        self.left.redraw()
        self._chosen(idx)

    def _chosen(self, i: Optional[int]) -> None:
        self.chosen = self.items[i] if i is not None and i < len(self.items) else None
        item = self.chosen
        self.r_title.configure(text=tr_name(item["name"]) if item else "")
        self.list_name = tr_name(item["name"]) if item else self.title
        if self.shows == "albums":
            albums = musiclib.albums(item["tracks"]) if item else []
            self.r_sub.configure(text=tr("{a}, {b}", a=plural(len(albums), "album"),
                                         b=plural(len(item["tracks"]), "song")) if item else "")
            self.grid_w.set_albums(albums)
        else:
            self.r_sub.configure(text=plural(len(item["tracks"]), "song") if item else "")
            self.table.set_rows(item["tracks"] if item else [])

    def _activate(self, i: int) -> None:
        self._chosen(i)
        self._play(False)

    def choose(self, name: str) -> None:
        for i, item in enumerate(self.items):
            if item["name"].casefold() == name.casefold():
                self.left.select(i)
                return

    def songs(self) -> List[Track]:
        if not self.chosen:
            return []
        return album_order(self.chosen["tracks"]) if self.shows == "albums" else list(self.table.rows)

    def likes_changed(self) -> None:
        if self.shows == "songs":
            self.table.redraw()

    def song_changed(self) -> None:
        if self.shows == "songs":
            self.table.redraw()


# ---------------------------------------------------------------- songs ---
class SongsPage(Page):
    """All Songs, Liked Songs or Disliked Songs."""

    EMPTY = {"all": N_("No songs yet. Add your music folder in Settings."),
             "liked": N_("Songs you like appear here. Click the heart of a song to like it."),
             "disliked": N_("Songs you dislike appear here, and shuffles leave them out. Use the thumbs-down "
                            "button or a song's menu to dislike it.")}

    def __init__(self, app, title: str, which: str = "all"):
        super().__init__(app)
        self.title, self.which, self.list_name = title, which, title
        head, self.title_l, self.sub = self.heading(self, title)
        head.pack(fill="x", padx=28, pady=(22, 8))
        self.table = song_table(self, self, on_activate=lambda i: app.play_list(self.table.rows, i, self.list_name),
                                empty_text=tr(self.EMPTY[which]))
        self.table.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.refresh()

    def rows(self) -> List[Track]:
        tracks = musiclib.sort_songs(self.app.tracks)
        if self.which != "all":
            wanted = set(self.app.state.liked if self.which == "liked" else self.app.state.disliked)
            tracks = [t for t in tracks if t.path in wanted]
        return tracks

    def refresh(self) -> None:
        rows = self.rows()
        self.table.set_rows(rows, keep=True)
        minutes = int(round(sum(t.duration for t in rows) / 60))
        self.sub.configure(text=tr("{a}, {b}", a=plural(len(rows), "song"), b=plural(minutes, "minute"))
                           if rows else "")

    def songs(self) -> List[Track]:
        return list(self.table.rows)

    def likes_changed(self) -> None:
        if self.which != "all":
            self.refresh()
        else:
            self.table.redraw()

    def song_changed(self) -> None:
        self.table.redraw()


# --------------------------------------------------------------- search ---
class SearchPage(Page):
    list_name = N_("Search results")

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        head, self.title_l, self.sub = self.heading(self, tr("Search"), buttons=False)
        head.pack(fill="x", padx=28, pady=(22, 4))
        self.alb_head = label(self, t, tr("Albums"), "h2")
        self.alb_head.pack(anchor="w", padx=28, pady=(6, 0))
        self.grid_box = frame(self, t, height=AlbumGrid.COVER + 84)  # one row of cards
        self.grid_box.pack(fill="x")
        self.grid_box.pack_propagate(False)
        self.grid_w = AlbumGrid(self.grid_box, t, app.covers, app.open_album)
        self.grid_w.pack(fill="both", expand=True)
        self.song_head = label(self, t, tr("Songs"), "h2")
        self.song_head.pack(anchor="w", padx=28, pady=(4, 2))
        self.table = song_table(self, self, on_activate=lambda i: app.play_list(self.table.rows, i, self.list_name))
        self.table.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.text = ""

    def show(self, text: str = "", **kw) -> None:
        self.text = text
        self.refresh()

    def refresh(self) -> None:
        q = self.text.strip().casefold()
        tracks = [t for t in self.app.tracks if q and any(q in x.casefold() for x in (
            t.display_title, t.artist, t.album_artist, t.display_album, t.genre))]
        albums = [a for a in self.app.albums if q and any(q in x.casefold() for x in (
            a["title"], a["artist"], a["genre"]))]
        self.title_l.configure(text=tr("Results for “{query}”", query=self.text.strip()))
        self.sub.configure(text=tr("{a}, {b}", a=plural(len(albums), "album"), b=plural(len(tracks), "song")) if (
            albums or tracks) else tr("Nothing in your library matches this."))
        self.grid_w.set_albums(albums)
        self.grid_box.configure(height=AlbumGrid.COVER + 84 if albums else 1)
        self.table.set_rows(musiclib.sort_songs(tracks))

    def songs(self) -> List[Track]:
        return list(self.table.rows)

    def likes_changed(self) -> None:
        self.table.redraw()

    def song_changed(self) -> None:
        self.table.redraw()


# ---------------------------------------------------------- now playing ---
class LyricsView(tk.Canvas):
    """Synced lyrics: every line, the current one bold in the text colour and the
    others grey, kept about a third from the top unless the user scrolled in the
    last 3 seconds; a click on a line seeks there.  Plain lyrics: the text."""

    PAD = 40
    GAP = 16
    USER_HOLD_S = 3.0

    def __init__(self, master, app: "PlayerApp"):
        super().__init__(master, highlightthickness=0, bd=0)
        self.app, self.theme = app, app.theme
        self.theme.reg(self, bg="bg")
        self.kind = ""
        self.entries: List[tuple] = []
        self.plain = ""
        self.items: List[int] = []
        self.tops: List[float] = []
        self.heights: List[float] = []
        self.current = -1
        self.offset = 0.0
        self.target = 0.0
        self.total = 1.0
        self.user_time = 0.0
        self._anim: Optional[str] = None
        self.bind("<Configure>", lambda e: self.layout())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        bind_wheel(self, self._wheel)
        self.theme.on_change(self.layout, self)

    def set_lyrics(self, kind: str, text: str) -> None:
        self.kind = kind
        self.entries = embed.lrc_entries(text) if kind == lyrics_mod.SYNCED else []
        if kind == lyrics_mod.SYNCED and not self.entries:
            self.kind, self.plain = lyrics_mod.PLAIN, lyrics_mod.strip_timestamps(text)
        else:
            self.plain = text if kind == lyrics_mod.PLAIN else ""
        self.current, self.offset, self.target, self.user_time = -1, 0.0, 0.0, 0.0
        self.layout()

    def layout(self) -> None:
        t = self.theme
        self.delete("all")
        self.items, self.tops, self.heights = [], [], []
        W, H = self.winfo_width(), self.winfo_height()
        if W <= 1:
            return
        width = max(100, W - 2 * self.PAD)
        y = self.PAD
        if self.kind == lyrics_mod.SYNCED:
            bold, normal = t.f("lyrics_b"), t.f("lyrics")
            for i, (_, line) in enumerate(self.entries):
                item = self.create_text(self.PAD, y, text=line or " ", anchor="nw", width=width, font=bold,
                                        fill=t.c("text2"), tags=("line",))
                box = self.bbox(item)
                h = (box[3] - box[1]) if box else bold.metrics("linespace")
                if i != self.current:
                    self.itemconfigure(item, font=normal)
                else:
                    self.itemconfigure(item, fill=t.c("text"))
                self.items.append(item)
                self.tops.append(y)
                self.heights.append(h)
                y += h + self.GAP
        else:
            text = self.plain if self.kind == lyrics_mod.PLAIN else tr("This song is instrumental.")
            item = self.create_text(self.PAD, y, text=text, anchor="nw", width=width, font=t.f("plain"),
                                    fill=t.c("text") if self.kind == lyrics_mod.PLAIN else t.c("text2"))
            box = self.bbox(item)
            y += (box[3] - box[1]) if box else 0
        self.total = y + self.PAD + (H * 2 / 3 if self.kind == lyrics_mod.SYNCED else 0)
        self.configure(scrollregion=(0, 0, W, max(self.total, H)))
        self._scroll_to(self.offset)

    def _scroll_to(self, offset: float) -> None:
        H = self.winfo_height()
        self.offset = min(max(offset, 0.0), max(0.0, self.total - H))
        self.yview_moveto(self.offset / max(self.total, H, 1))

    def update_position(self, seconds: float) -> None:
        if self.kind != lyrics_mod.SYNCED or not self.entries:
            return
        ms = seconds * 1000.0
        idx = -1
        for i, (t_ms, _) in enumerate(self.entries):
            if t_ms <= ms:
                idx = i
            else:
                break
        if idx != self.current:
            t = self.theme
            if 0 <= self.current < len(self.items):
                self.itemconfigure(self.items[self.current], font=t.f("lyrics"), fill=t.c("text2"))
            self.current = idx
            if 0 <= idx < len(self.items):
                self.itemconfigure(self.items[idx], font=t.f("lyrics_b"), fill=t.c("text"))
        if time.monotonic() - self.user_time < self.USER_HOLD_S:
            return
        if 0 <= self.current < len(self.tops):
            target = self.tops[self.current] + self.heights[self.current] / 2 - self.winfo_height() / 3
        else:
            target = 0.0
        if abs(target - self.target) > 0.5 or abs(self.offset - target) > 0.5:
            self.target = target
            if self._anim is None:
                self._animate()

    def _animate(self) -> None:
        self._anim = None
        if time.monotonic() - self.user_time < self.USER_HOLD_S:
            return
        diff = self.target - self.offset
        if abs(diff) < 0.6:
            self._scroll_to(self.target)
            return
        self._scroll_to(self.offset + diff * 0.18)
        self._anim = self.after(16, self._animate)

    def _wheel(self, n: int) -> None:
        self.user_time = time.monotonic()
        self._scroll_to(self.offset + n * 48)

    def _line_at(self, e) -> Optional[int]:
        y = self.canvasy(e.y)
        for i, (top, h) in enumerate(zip(self.tops, self.heights)):
            if top <= y <= top + h:
                return i
        return None

    def _motion(self, e) -> None:
        self.configure(cursor="hand2" if self.kind == lyrics_mod.SYNCED and self._line_at(e) is not None else "")

    def _click(self, e) -> None:
        if self.kind != lyrics_mod.SYNCED:
            return
        i = self._line_at(e)
        if i is not None:
            self.user_time = 0.0
            self.app.seek(self.entries[i][0] / 1000.0)


class NowPlayingPage(Page):
    """The cover on the left half and the lyrics on the right; without lyrics the
    cover in the middle with the title, the artist and "Get lyrics"."""

    SIZES = (160, 200, 240, 280, 320, 360, 400, 440, 480)

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        self.art = tk.Canvas(self, highlightthickness=0, bd=0)
        t.reg(self.art, bg="bg")
        self.lyrics = LyricsView(self, app)
        self.get_btn = PillButton(self.art, t, tr("Get lyrics"), icon="lyrics", command=app.get_lyrics_current)
        self.kind = ""
        self.path: Optional[str] = None
        self.art.bind("<Configure>", lambda e: self.draw_art())
        app.covers.listeners.append(lambda key, size: self.draw_art())
        t.on_change(self.draw_art)
        self._place()

    def show(self, **kw) -> None:
        self.song_changed(force=True)

    def song_changed(self, force: bool = False) -> None:
        path = self.app.current
        if path == self.path and not force:
            return
        self.path = path
        self.load_lyrics()

    def load_lyrics(self) -> None:
        found = lyrics_mod.read_lyrics_file(self.path) if self.path else None
        if found is None:
            self.kind = ""
        else:
            kind, text, _ = found
            self.kind = kind
            self.lyrics.set_lyrics(kind, text)
        self._place()
        self.draw_art()
        self.update_position(self.app.position())

    def _place(self) -> None:
        if self.kind:
            self.art.place(relx=0, rely=0, relwidth=0.5, relheight=1)
            self.lyrics.place(relx=0.5, rely=0, relwidth=0.5, relheight=1)
        else:
            self.lyrics.place_forget()
            self.art.place(relx=0, rely=0, relwidth=1, relheight=1)

    def draw_art(self) -> None:
        t, c = self.theme, self.art
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if W <= 1:
            return
        trk = self.app.track(self.path) if self.path else None
        if trk is None:
            c.create_text(W / 2, H / 2, text=tr("Nothing is playing"), fill=t.c("text2"), font=t.f("h2"))
            return
        text_h = 0 if self.kind else 120
        room = min(W - 80, H - 80 - text_h)
        size = max([s for s in self.SIZES if s <= room] or [self.SIZES[0]])
        album = self.app.album_of(trk)
        key = album["key"] if album else trk.path
        paths = [x.path for x in album["tracks"]] if album else [trk.path]
        img = self.app.covers.get(key, paths, size)
        x = (W - size) / 2
        y = (H - size - text_h) / 2
        draw_cover(c, t, img, x, y, size)
        if not self.kind:
            fit = {}
            c.create_text(W / 2, y + size + 30, text=fit_text(t.f("h1"), trk.display_title, W - 60, fit),
                          fill=t.c("text"), font=t.f("h1"))
            c.create_text(W / 2, y + size + 58, text=fit_text(t.f("body"), f"{tr_name(trk.display_artist)} — "
                                                              f"{tr_name(trk.display_album)}", W - 60, fit),
                          fill=t.c("text2"), font=t.f("body"))
            c.create_window(W / 2, y + size + 96, window=self.get_btn)

    def update_position(self, seconds: float) -> None:
        if self.kind:
            self.lyrics.update_position(seconds)

    def lyrics_changed(self, path: str) -> None:
        if path == self.path:
            self.load_lyrics()


# ------------------------------------------------------------- settings ---
def theme_label(name: str) -> str:
    """A theme's name as menus show it: the built-in themes and "Match the system"
    translated, the user's own themes and theme files as they are named."""
    from . import themes

    return tr(name) if name in themes.BUILTIN or name == themes.SYSTEM else name


class ThemeEditor(tk.Frame):
    """Settings → Appearance: New theme…, Export…, and for the user's own themes
    Rename…, Delete…, "Dark theme" and a colour button for each of the eight
    colours, which change the app at once.  Built-in themes and those of the theme
    files in the themes folder cannot be edited here."""

    def __init__(self, master, app: "PlayerApp"):
        from . import themes

        super().__init__(master, bd=0, highlightthickness=0)
        t = app.theme
        self.app = app
        t.reg(self, bg="bg")
        theme = app.current_theme()
        own = app.is_custom_theme(theme["name"])
        self.name = theme["name"]
        self.file = "" if own else app.theme_file(theme["name"])
        label(self, t, tr("Theme colours"), "body_b").pack(anchor="w")
        shown = theme_label(theme["name"])
        if own:
            note = tr("“{name}” is your own theme: a click on a colour changes it, and the app at once.", name=shown)
        elif self.file:
            note = tr("“{name}” comes from the file {file} in the themes folder. Change that file, or make a copy "
                      "you can change here with New theme…", name=shown, file=os.path.basename(self.file))
        else:
            note = tr("“{name}” is a built-in theme and cannot be changed. “New theme…” makes a copy of it that you "
                      "can change.", name=shown)
        self.note_l = label(self, t, note, "small", fg="text2", wraplength=SettingsPage.HELP_WIDTH, justify="left")
        self.note_l.pack(anchor="w", pady=(2, 8))
        btns = frame(self, t)
        btns.pack(anchor="w")
        acts = [(N_("New theme…"), app.new_theme), (N_("Export…"), lambda: app.export_theme_file(self.name))]
        if own:
            acts += [(N_("Rename…"), lambda: app.rename_theme(self.name)),
                     (N_("Delete…"), lambda: app.delete_theme(self.name))]
        self.actions = [text for text, _ in acts]  # in English (the buttons show them translated)
        for i, (text, cmd) in enumerate(acts):
            PillButton(btns, t, tr(text), kind="accent" if i == 0 else "plain", command=cmd).pack(
                side="left", padx=(0 if i == 0 else 8, 0))
        if own:
            self.v_dark = tk.BooleanVar(value=bool(theme["dark"]))
            ttk.Checkbutton(self, text=tr("Dark theme"), variable=self.v_dark, style="P.TCheckbutton",
                            command=lambda: app.set_theme_dark(self.name, self.v_dark.get())).pack(anchor="w",
                                                                                                  pady=(10, 0))
        grid = frame(self, t)
        grid.pack(anchor="w", pady=(10, 0))
        self.swatches: Dict[str, tk.Canvas] = {}
        for i, key in enumerate(themes.COLOR_KEYS):
            cell = frame(grid, t)
            cell.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 28), pady=4)
            sw = tk.Canvas(cell, width=30, height=30, highlightthickness=0, bd=0, cursor="hand2")
            t.reg(sw, bg="bg")
            sw.pack(side="left")
            value = theme["colors"][key]
            round_rect(sw, 1, 1, 29, 29, 7, fill=value, outline=t.c("sep"))
            sw.bind("<Button-1>", lambda e, k=key: self._pick(k))
            box = frame(cell, t)
            box.pack(side="left", padx=(10, 0))
            label(box, t, tr(themes.COLOR_LABELS[key]), "body").pack(anchor="w")
            label(box, t, value, "small", fg="text2").pack(anchor="w")
            self.swatches[key] = sw
        self.own = own

    def _pick(self, key: str) -> None:
        from tkinter import colorchooser, messagebox

        from . import themes as themes_mod

        app = self.app
        if not self.own:
            what = (tr("Themes from the themes folder are changed in their files.") if self.file else
                    tr("Built-in themes cannot be changed."))
            if not messagebox.askyesno(tr("Theme file") if self.file else tr("Built-in theme"),
                                       tr("{what} Make a copy of “{name}” and change that?", what=what,
                                          name=theme_label(self.name)), parent=app.root):
                return
            name = app.new_theme()
            if not name:
                return
            self.name = name
        theme = app.current_theme()
        color = key.capitalize() if i18n.language() == "en" else tr(themes_mod.COLOR_LABELS[key])
        got = colorchooser.askcolor(color=theme["colors"][key], parent=app.root,
                                    title=f"{theme_label(theme['name'])}: {color}")
        if got and got[1]:
            app.set_theme_color(self.name, key, got[1])


class SettingsPage(Page):
    """Every setting, one section per settings.GROUPS entry, plus the actions of
    the 1.x app (scan, lyrics, saved results).  Every change is saved at once."""

    HELP_WIDTH = 600

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        self.group = settings_mod.GROUPS[0]
        head = frame(self, t)
        head.pack(fill="x", padx=28, pady=(22, 8))
        label(head, t, tr("Settings"), "title").pack(anchor="w")
        label(head, t, tr("Saved in {path}", path=settings_mod.default_path()), "small", fg="text2").pack(anchor="w")
        body = frame(self, t)
        body.pack(fill="both", expand=True)
        self.sections = SongList(body, t, [Column("name", "", 160, True)], lambda r, k: tr(r["name"]), header=False,
                                 zebra=False, on_select=self._section, row_height=34)
        self.sections.configure(width=210)
        self.sections.pack(side="left", fill="y", padx=(12, 0))
        self.sections.pack_propagate(False)
        self.sections.set_rows([{"name": g} for g in settings_mod.GROUPS])
        frame(body, t, bg="sep", width=1).pack(side="left", fill="y", padx=(8, 0))
        outer = frame(body, t)
        outer.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0, bd=0)
        t.reg(self.canvas, bg="bg")
        sb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview, style="P.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=lambda a, b: (sb.set(a, b), show_scrollbar(
            sb, float(a) > 0.0 or float(b) < 1.0)))
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.inner = frame(self.canvas, t)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        bind_wheel(self.canvas, lambda n: self.canvas.yview_scroll(n, "units"))
        self.help_labels: Dict[str, tk.Label] = {}
        self.vars: Dict[str, Any] = {}
        self.boxes: Dict[str, ttk.Combobox] = {}  # the choice menus, by setting
        self.restart_note: Optional[tk.Frame] = None
        self.sections.selected = 0
        self.build()

    def show(self, group: Optional[str] = None, **kw) -> None:
        if group in settings_mod.GROUPS:
            self.group = group
            self.sections.selected = settings_mod.GROUPS.index(group)
            self.sections.redraw()
        self.build()

    def refresh(self) -> None:
        pass

    def _section(self, i: int) -> None:
        self.group = settings_mod.GROUPS[i]
        self.build()

    # ---- building -------------------------------------------------------
    def build(self) -> None:
        t, cfg = self.theme, self.app.cfg
        for w in self.inner.winfo_children():
            w.destroy()
        t.prune()
        self.help_labels, self.vars, self.boxes, self.restart_note = {}, {}, {}, None
        box = frame(self.inner, t)
        box.pack(fill="both", expand=True, padx=28, pady=(6, 24))
        label(box, t, tr(self.group), "h1").pack(anchor="w", pady=(0, 10))
        if self.group == "Detection":
            label(box, t, tr("Changes apply to the next scan on the Quality check page."), "body",
                  fg="text2").pack(anchor="w", pady=(0, 8))
        for info in settings_mod.Settings.field_info():
            if info["group"] == self.group:
                self._field(box, info, getattr(cfg, info["name"]))
        self._actions(box)
        self.canvas.yview_moveto(0)

    def _field(self, parent, info: dict, value) -> None:
        t = self.theme
        name, kind = info["name"], info.get("kind")
        row = frame(parent, t)
        row.pack(fill="x", anchor="w", pady=(8, 2))
        if kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(row, text=tr(info["label"]), variable=var, style="P.TCheckbutton",
                            command=lambda: self._set(name, var.get())).pack(anchor="w")
        else:
            label(row, t, tr(info["label"]), "body_b").pack(anchor="w", pady=(0, 4))
            if kind == "paths":
                self._paths(row, name, value)
            elif name == "output_device":
                self._device(row, value)
            elif name == "font_family":
                self._font(row, value)
            elif name == "theme":
                self._theme(row, value)
            elif kind == "choice":
                # the menu shows the choices translated (the languages in their own names) and the
                # settings keep the English value
                choices = list(info["choices"])
                shown = choices if name == "language" else [tr(c) for c in choices]
                var = tk.StringVar(value=shown[choices.index(value)] if value in choices else value)
                self.vars[name] = var  # kept alive: Tk forgets a variable Python no longer holds
                width = max(18, max(text_width(c) for c in shown) + 2)
                cb = ttk.Combobox(row, textvariable=var, values=shown, state="readonly", width=width,
                                  style="P.TCombobox", font=t.f("body"))
                cb.pack(anchor="w")
                cb.bind("<<ComboboxSelected>>", lambda e, cb=cb: cb.current() >= 0 and self._set(
                    name, choices[cb.current()]))
                self.boxes[name] = cb
            elif kind == "multi":
                from .gui import VERDICT_STYLE

                sub = frame(row, t)
                sub.pack(anchor="w")
                bools = {}
                for i, choice in enumerate(info["choices"]):
                    b = tk.BooleanVar(value=choice in value)
                    bools[choice] = b
                    ttk.Checkbutton(sub, text=tr(VERDICT_STYLE.get(choice, (choice,))[0]), variable=b,
                                    style="P.TCheckbutton",
                                    command=lambda: self._set(name, [c for c, v in bools.items() if v.get()])
                                    ).grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 14))
            elif kind == "str":
                var = tk.StringVar(value=value)
                e = ttk.Entry(row, textvariable=var, width=60, style="P.TEntry", font=t.f("body"))
                e.pack(anchor="w")
                for ev in ("<FocusOut>", "<Return>"):
                    e.bind(ev, lambda _e: self._set(name, var.get()))
            else:
                var = tk.StringVar(value=f"{value:g}" if isinstance(value, float) else str(value))
                inc = 1 if kind == "int" or (info.get("max") or 0) > 50 else 0.1
                sp = ttk.Spinbox(row, textvariable=var, from_=info.get("min") or 0, to=info.get("max") or 1e9,
                                 increment=inc, width=10, style="P.TSpinbox", font=t.f("body"),
                                 command=lambda: self._set_number(name, var, kind))
                sp.pack(anchor="w")
                for ev in ("<FocusOut>", "<Return>"):
                    sp.bind(ev, lambda _e: self._set_number(name, var, kind))
        help_l = label(row, t, tr_text(info["help"]), "small", fg="text2", wraplength=self.HELP_WIDTH, justify="left")
        help_l.pack(anchor="w", pady=(3, 0))
        self.help_labels[name] = help_l
        if name == "language":
            self._language_note(row, value)
        if name == "theme":
            files = frame(row, t)
            files.pack(anchor="w", fill="x", pady=(10, 0))
            label(files, t, tr("Theme files (.json) put into the themes folder appear in this menu."), "small",
                  fg="text2").pack(side="left")
            PillButton(files, t, tr("Import theme…"), kind="plain", command=self.app.import_theme_file).pack(
                side="left", padx=(12, 0))
            PillButton(files, t, tr("Open themes folder"), kind="plain", command=self.app.open_themes_folder).pack(
                side="left", padx=(8, 0))
            self.theme_editor = ThemeEditor(row, self.app)
            self.theme_editor.pack(anchor="w", fill="x", pady=(12, 0))

    def _language_note(self, parent, value: str) -> None:
        """Another language than the one the app runs in was chosen: it is used after a
        restart (the note is in the language in use)."""
        if i18n.code_of(value) == i18n.language():
            return
        t = self.theme
        note = frame(parent, t)
        note.pack(anchor="w", fill="x", pady=(8, 0))
        label(note, t, tr("The new language is used when the app starts again."), "body", fg="accent",
              wraplength=self.HELP_WIDTH - 160, justify="left").pack(side="left")
        PillButton(note, t, tr("Restart now"), command=self.app.restart).pack(side="left", padx=(12, 0))
        self.restart_note = note

    def _theme(self, parent, value: str) -> None:
        t = self.theme
        names = self.app.theme_names()
        self.theme_names = names
        shown = [theme_label(n) for n in names]
        var = tk.StringVar(value=theme_label(value if value in names else names[0]))
        self.vars["theme"] = var  # kept alive: Tk forgets a variable Python no longer holds
        cb = ttk.Combobox(parent, textvariable=var, values=shown, state="readonly",
                          width=max(24, max(text_width(n) for n in shown) + 2), style="P.TCombobox",
                          font=t.f("body"), postcommand=self.theme_values)
        cb.pack(anchor="w")
        cb.bind("<<ComboboxSelected>>", lambda e: cb.current() >= 0 and self.app.select_theme(
            self.theme_names[cb.current()]))
        self.theme_box = cb

    def theme_values(self) -> List[str]:
        """The theme menu again, when it opens: theme files may have been put into the
        themes folder or taken out of it.  Returns the names (the menu shows them translated)."""
        names = self.app.theme_names()
        self.theme_names = names
        shown = [theme_label(n) for n in names]
        self.theme_box.configure(values=shown, width=max(24, max(text_width(n) for n in shown) + 2))
        return names

    def _paths(self, parent, name: str, value: List[str]) -> None:
        t = self.theme
        lb = tk.Listbox(parent, height=max(3, min(len(value), 8)), width=70, activestyle="none",
                        relief="flat", bd=0, highlightthickness=1, font=t.f("body"), exportselection=False)
        t.reg(lb, bg="lcd", fg="text", selectbackground="accent", selectforeground="on_accent",
              highlightbackground="sep", highlightcolor="sep")
        for p in value:
            lb.insert("end", p)
        if not value:
            lb.insert("end", tr("No music folder yet"))
            lb.itemconfigure(0, foreground=t.c("text2"))
        lb.pack(anchor="w")
        btns = frame(parent, t)
        btns.pack(anchor="w", pady=(6, 0))
        PillButton(btns, t, tr("Add folder…"), kind="plain", command=self.app.choose_music_folder).pack(side="left")

        def remove():
            sel = [lb.get(i) for i in lb.curselection()]
            keep = [p for p in self.app.cfg.library_folders if p not in sel]
            if sel and keep != self.app.cfg.library_folders:
                self._set(name, keep)
                self.build()

        PillButton(btns, t, tr("Remove"), kind="plain", command=remove).pack(side="left", padx=(8, 0))

    def _device(self, parent, value: str) -> None:
        t = self.theme
        default = tr("System default")
        names = [""] + [d.name for d in self.app.output_devices()]  # "" = the system's default device
        if value and value not in names:
            names.append(value)
        shown = [default] + names[1:]
        var = tk.StringVar(value=value or default)
        self.vars["output_device"] = var  # kept alive: Tk forgets a variable Python no longer holds
        cb = ttk.Combobox(parent, textvariable=var, values=shown, state="readonly",
                          width=max(30, max(text_width(n) for n in shown) + 2), style="P.TCombobox", font=t.f("body"))
        cb.pack(anchor="w")
        cb.bind("<<ComboboxSelected>>",
                lambda e: cb.current() >= 0 and self._set("output_device", names[cb.current()]))

    def _font(self, parent, value: str) -> None:
        t = self.theme
        names = [""] + font_families(self)  # "" = the default font
        shown = [tr(DEFAULT_FONT_LABEL)] + names[1:]
        var = tk.StringVar(value=value or shown[0])
        self.vars["font_family"] = var  # kept alive: Tk forgets a variable Python no longer holds
        cb = ttk.Combobox(parent, textvariable=var, values=shown, state="readonly", width=36, style="P.TCombobox",
                          font=t.f("body"))
        cb.pack(anchor="w")
        cb.bind("<<ComboboxSelected>>",
                lambda e: cb.current() >= 0 and self._set("font_family", names[cb.current()]))

    def _actions(self, parent) -> None:
        t, app = self.theme, self.app
        acts = []
        if self.group == "Library":
            acts = [(tr("Update the library now"), app.refresh_library),
                    (tr("Scan library"), lambda: app.check_library(switch=True)),
                    (tr("Download lyrics for the whole library…"), app.lyrics_for_library),
                    (tr("Look up the library on MusicBrainz now"), lambda: app.lookup_library(force=True)),
                    (tr("Open the quality checker"), lambda: app.show_page("quality")),
                    (tr("Clear the play history…"), app.clear_history)]
        elif self.group == "Playback":
            acts = [(tr("Equalizer…"), app.open_eq)]
        elif self.group == "Saved results":
            acts = [(tr("Delete all saved results…"), app.qc.forget_saved)]
            st = app.qc.store.stats() if app.qc.store is not None else None
            from .store import default_path as store_path

            where = (tr("{files} file(s), {mb} MB, in {path}", files=f"{st['files']:,}",
                        mb=f"{st['disk_bytes'] / 1e6:.1f}", path=st["path"]) if st
                     else tr("Saving is turned off. Results are kept in {path}", path=store_path()))
            label(parent, t, where, "small", fg="text2", wraplength=self.HELP_WIDTH, justify="left").pack(
                anchor="w", pady=(14, 0))
        elif self.group in ("Detection", "Scanning", "Display", "Fixing"):
            acts = [(tr("Open the quality checker"), lambda: app.show_page("quality"))]
        if acts:
            row = frame(parent, t)
            row.pack(anchor="w", pady=(18, 0))
            for i, (text, cmd) in enumerate(acts):  # two per row, so they never run off the page
                PillButton(row, t, text, kind="accent" if i == 0 else "plain", command=cmd).grid(
                    row=i // 2, column=i % 2, sticky="w", padx=(0, 10), pady=(0, 8))

    # ---- saving ---------------------------------------------------------
    def _set_number(self, name: str, var: tk.StringVar, kind: Optional[str]) -> None:
        raw = var.get().strip()
        try:
            value = int(raw) if kind == "int" else float(raw)
        except ValueError:
            self._bad(name)
            return
        if value != getattr(self.app.cfg, name):
            self._set(name, value)

    def _bad(self, name: str) -> None:
        info = next(i for i in settings_mod.Settings.field_info() if i["name"] == name)
        lo, hi = info.get("min"), info.get("max")
        ask = (tr("Please enter a number from {low} to {high}.", low=f"{lo:g}", high=f"{hi:g}")
               if lo is not None and hi is not None else tr("Please enter a number."))
        lab = self.help_labels.get(name)
        if lab is not None:
            lab.configure(text=tr("{error} {help}", error=ask, help=tr_text(info["help"])), fg=self.theme.c("error"))

    def _set(self, name: str, value) -> None:
        cfg = self.app.cfg
        data = cfg.to_dict()
        data[name] = value
        new = settings_mod.Settings.from_dict(data)
        got = getattr(new, name)
        same = got == value or (isinstance(got, (int, float)) and isinstance(value, (int, float))
                                and float(got) == float(value))
        if not same:
            self._bad(name)
            return
        lab = self.help_labels.get(name)
        if lab is not None:
            info = next(i for i in settings_mod.Settings.field_info() if i["name"] == name)
            lab.configure(text=tr_text(info["help"]), fg=self.theme.c("text2"))
        self.app.apply_settings(new)


__all__ = ["Page", "AlbumsPage", "AlbumPage", "GroupPage", "SongsPage", "SearchPage",
           "NowPlayingPage", "SettingsPage", "LyricsView", "track_value", "track_sort", "album_order"]
