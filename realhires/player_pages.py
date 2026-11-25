"""The pages of the 2.0 player's content area: Recently Added, Albums, an album,
Artists, Songs, Genres, Liked Songs, Search, Now Playing and Settings (the
Quality check page is the 1.x scanner, `gui.App`)."""

from __future__ import annotations

import os
import sys
import time
import tkinter as tk
import unicodedata
from tkinter import ttk
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from . import APP_NAME, embed, i18n
from . import lyrics as lyrics_mod
from . import musiclib, playerstate, stats, tagedit
from . import themes as themes_mod
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


class FlowRow(tk.Frame):
    """A row of buttons that goes on in a further line when the page is too narrow for one
    (2.3: on the 1024 px screens of the Windows CI runner the album page's buttons did not fit
    beside its cover, and pack hid the last ones). Widgets are made as children of the row,
    then given to add(); each line is a frame of the row, and the widgets are packed into it."""

    def __init__(self, parent, theme: Theme, gap: int = 10, vgap: int = 8):
        super().__init__(parent, bd=0, highlightthickness=0)
        self.theme = theme
        theme.reg(self, bg="bg")
        self.gap, self.vgap = gap, vgap
        self.items: List[tk.Widget] = []
        self.lines: List[tk.Frame] = []
        self._width = 0
        self.bind("<Configure>", self._resized)

    def add(self, *widgets: tk.Widget) -> None:
        self.items.extend(widgets)
        self.relayout()

    def _resized(self, e) -> None:  # (not _configure: that name is Tk's own)
        if e.width != self._width:
            self._width = e.width
            self.relayout()

    def relayout(self) -> None:
        width = self.winfo_width()
        if width <= 1:  # not laid out yet: one line
            width = 1 << 30
        lines: List[List[tk.Widget]] = []
        cur: List[tk.Widget] = []
        x = 0
        for w in self.items:
            need = w.winfo_reqwidth()
            if cur and x + self.gap + need > width:
                lines.append(cur)
                cur, x = [], 0
            x += (self.gap if cur else 0) + need
            cur.append(w)
        if cur:
            lines.append(cur)
        for w in self.items:
            w.pack_forget()
        for f in self.lines:
            f.pack_forget()
        while len(self.lines) < len(lines):
            self.lines.append(self.theme.reg(tk.Frame(self, bd=0, highlightthickness=0), bg="bg"))
        for i, ws in enumerate(lines):
            f = self.lines[i]
            f.pack(anchor="w", pady=(self.vgap if i else 0, 0))
            for j, w in enumerate(ws):
                w.pack(in_=f, side="left", padx=(self.gap if j else 0, 0))
                # made before the line's frame, so raised above it in the stacking order (Tk's
                # window `raise`: a Canvas's own lift() raises the items drawn on it)
                w.tk.call("raise", str(w), str(f))


def format_label(t: Track) -> str:
    """The Format column: the codec ("FLAC", "MP3"; PCM for WAV and AIFF samples)."""
    c = (t.codec or "").lower()
    return "PCM" if c.startswith("pcm_") else c.upper()


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
    if key == "album_artist":
        return t.album_artist
    if key == "disc":
        return str(t.disc) if t.disc else ""
    if key == "format":
        return format_label(t)
    if key == "bit_rate":
        return f"{round(t.bit_rate / 1000):,} kbps" if t.bit_rate else ""
    if key == "added":
        if not t.mtime_ns:
            return ""
        lt = time.localtime(t.mtime_ns / 1e9)
        return tr_text(stats.day_label(f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"))
    if key == "file":
        return os.path.basename(t.path)
    return ""


def track_sort(t: Track, key: str):
    title = t.display_title.casefold()
    if key == "time":
        return t.duration
    if key == "num":
        return (t.disc or 9999, t.track or 9999, title)
    if key == "disc":
        return (t.disc or 9999, t.track or 9999, title)
    if key == "quality":  # bits, then rate (musiclib.quality_sort)
        return (musiclib.quality_sort(t), title, t.path)
    if key == "bit_rate":
        return (t.bit_rate, title, t.path)
    if key == "added":
        return (t.mtime_ns, title, t.path)
    return (track_value(t, key).casefold(), title, t.path)


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


# The columns a song list can show (2.1: the user chooses them per kind of list,
# playerstate.SONG_COLUMNS, kept in player.json), and their names in the menu.
COLUMN_DEFS = {
    "num": Column("num", "#", 34, anchor="e", kind="num"),
    "title": Column("title", N_("Title"), 300, True),
    "artist": Column("artist", N_("Artist"), 200, True),
    "album": Column("album", N_("Album"), 200, True),
    "album_artist": Column("album_artist", N_("Album Artist"), 170, True),
    "composer": Column("composer", N_("Composer"), 150, True),
    "genre": Column("genre", N_("Genre"), 110),
    "year": Column("year", N_("Year"), 48, anchor="e"),
    "disc": Column("disc", N_("Disc"), 40, anchor="e"),
    "time": Column("time", N_("Time"), 52, anchor="e"),
    "quality": Column("quality", N_("Quality"), 118, tip=True),
    "format": Column("format", N_("Format"), 64),
    "bit_rate": Column("bit_rate", N_("Bit rate"), 86, anchor="e"),
    "added": Column("added", N_("Date added"), 100),
    "file": Column("file", N_("File"), 180, True),
    "like": Column("like", "", 30, kind="heart"),
}
COLUMN_LABELS = {"num": N_("# (track number)"), "like": N_("Heart (like)")}


def text_width(text: str) -> int:
    """The width of a text in average characters (ttk widths): a Japanese character counts twice."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def song_columns(names) -> List[Column]:
    return [COLUMN_DEFS[n] for n in names if n in COLUMN_DEFS]


SONG_COLUMNS = song_columns(playerstate.DEFAULT_COLUMNS["songs"])


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


def song_table(page: Page, parent, columns=None, value=None, extra_menu=None, sort_value=None,
               kind: Optional[str] = None, prefix=(), **kw) -> SongList:
    """A song table with likes, the playing mark and the song menu (right click);
    `extra_menu(index)` gives more (label, command) entries, e.g. a playlist's.
    With `kind` ("songs", "folders", "album") it shows the columns the user chose
    for that kind of list (after the `prefix` columns), and a right click on its
    headings lets them choose."""
    app = page.app
    table: SongList

    def context(i: int, e) -> None:
        app.song_menu(table.rows[i], extra_menu(i) if extra_menu else (), list_name=page.list_name,
                      rows=table.rows, index=i).tk_popup(e.x_root, e.y_root)

    def heading_menu(e) -> None:
        m = columns_menu(app, kind)
        m.tk_popup(e.x_root, e.y_root)

    if kind is not None:
        columns = list(prefix) + song_columns(app.state.columns_of(kind))
    kw.setdefault("label_of", lambda r: app.label_of("song", r.path))  # 2.3: the colour label's dot
    kw.setdefault("cell_color", lambda r, key: track_color(page.theme, r, key))
    table = SongList(parent, page.theme, columns or SONG_COLUMNS, value or track_value, sort_value or track_sort,
                     on_heart=lambda r: app.toggle_like(r.path), is_liked=lambda r: app.is_liked(r.path),
                     is_current=lambda r: r.path == app.current, on_context=context,
                     is_dim=lambda r: getattr(r, "missing", False), tip_value=track_tip,
                     on_heading_context=heading_menu if kind is not None else None, **kw)
    if kind is not None:
        table.column_kind, table.column_prefix = kind, list(prefix)
        _song_tables(app).append(table)
    return table


def _song_tables(app) -> list:
    """The song tables whose columns follow the user's choice (pruned when destroyed)."""
    if not hasattr(app, "song_tables"):
        app.song_tables = []
    return app.song_tables


def columns_menu(app, kind: str) -> tk.Menu:
    """The menu of a song table's headings: a check item per column (Title stays) and
    Reset columns."""
    m = tk.Menu(app.root, tearoff=False)
    shown = app.state.columns_of(kind)
    m.vars = []  # type: ignore[attr-defined]  # the check items' variables live as long as the menu
    for name in playerstate.SONG_COLUMNS:
        v = tk.BooleanVar(master=m, value=name in shown)
        m.vars.append(v)  # type: ignore[attr-defined]
        m.add_checkbutton(label=tr(COLUMN_LABELS.get(name, COLUMN_DEFS[name].title)), variable=v,
                          state="disabled" if name == "title" else "normal",
                          command=lambda n=name: set_columns(app, kind, app.state.toggle_column(kind, n)))
    m.add_separator()
    m.add_command(label=tr("Reset columns"), command=lambda: set_columns(app, kind, app.state.reset_columns(kind)))
    app.theme_menu(m)
    app.last_menu = m
    return m


def set_columns(app, kind: str, names: List[str]) -> None:
    """The user chose the columns of a kind of list: save them, and show them in every
    table of that kind."""
    app.save_state()
    tables = _song_tables(app)
    for table in list(tables):
        try:
            if not table.winfo_exists():
                tables.remove(table)
                continue
        except tk.TclError:
            tables.remove(table)
            continue
        if table.column_kind == kind:
            table.set_columns(table.column_prefix + song_columns(names))


def track_color(theme, t: Track, key: str) -> Optional[str]:
    """A cell's own text colour (2.3): the Quality column in the quality's colour (gold for
    Hi-Res Lossless, the accent for Lossless, grey for lossy, unless the theme sets them)."""
    if key == "quality" and not getattr(t, "missing", False):
        return theme.c(themes_mod.QUALITY_COLOR_KEYS[themes_mod.quality_kind(t)])
    return None


def track_tip(t: Track, key: str) -> str:
    """The tooltip of a cell: the Quality column's resolution ("24-bit/96 kHz")."""
    if key == "quality" and not getattr(t, "missing", False):
        return tr_text(musiclib.quality_detail(t))
    return ""


# --------------------------------------------------------------- albums ---
def album_menu(app, album: dict) -> tk.Menu:
    """The menu of an album card (right click, 2.2) and of the album page's "…" button
    (2.3): Play, Shuffle, Play Next, Add to Queue, then Album Properties… (the Properties
    window for its songs), Acquire Metadata… (the MusicBrainz window for the album, as the
    album page's button) and Change Cover… (the Cover window of the album, 2.3)."""
    m = tk.Menu(app.root, tearoff=False)
    name = tr_name(album["title"])  # "Playing from …", as the album page
    paths = [t.path for t in album["tracks"]]  # in track order
    m.add_command(label=tr("Play"), command=lambda: app.play_list(album["tracks"], 0, name))
    m.add_command(label=tr("Shuffle"), command=lambda: app.play_list(album["tracks"], 0, name, shuffle=True))
    m.add_command(label=tr("Play Next"), command=lambda: app.queue_songs(paths, first=True))
    m.add_command(label=tr("Add to Queue"), command=lambda: app.queue_songs(paths, first=False))
    m.add_separator()
    m.add_command(label=tr("Album Properties…"), command=lambda: app.edit_album_info(album))
    m.add_command(label=tr("Acquire Metadata…"), command=lambda: app.album_musicbrainz(album))
    first = next((t.path for t in album["tracks"] if not getattr(t, "missing", False)), "")
    m.add_command(label=tr("Change Cover…"), command=lambda: app.open_cover_window(first))
    m.add_separator()
    app.add_label_menu(m, "album", album["key"])  # 2.3
    app.theme_menu(m)
    app.last_menu = m
    return m


def album_context(app, grid: Callable[[], AlbumGrid]) -> Callable[[int, Any], None]:
    """An AlbumGrid's on_context: the album menu of the card right-clicked."""
    def context(i: int, e) -> None:
        albums = grid().albums
        if 0 <= i < len(albums) and not albums[i].get("new") and "mosaic" not in albums[i]:
            album_menu(app, albums[i]).tk_popup(e.x_root, e.y_root)
    return context


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
        self.grid_w = AlbumGrid(self, t, app.covers, app.open_album,
                                on_context=album_context(app, lambda: self.grid_w),
                                label_of=lambda a: app.label_of("album", a["key"]))
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
        # the album artist: each artist of the field ("A, B") its own link to its page
        self.artist_box = frame(info, t)
        self.artist_box.pack(anchor="w")
        self.artist_links: List[tk.Label] = []
        self.artist_l: Optional[tk.Label] = None  # the first link
        self.meta_l = label(info, t, "", "body", fg="text2")
        self.meta_l.pack(anchor="w", pady=(2, 14))
        btns = FlowRow(info, t)  # wraps on a narrow page instead of hiding the last buttons (2.3)
        btns.pack(anchor="w", fill="x")
        play_b = PillButton(btns, t, tr("Play"), icon="play", command=lambda: self._play(False))
        shuffle_b = PillButton(btns, t, tr("Shuffle"), icon="shuffle", command=lambda: self._play(True))
        self.edit_b = PillButton(btns, t, tr("Album Properties…"), kind="plain", command=self.edit_info)
        self.mb_b = PillButton(btns, t, tr("Acquire Metadata…"), kind="plain",
                               command=self.musicbrainz)
        # 2.3: the album's menu (as an album card's right-click menu, with Change Cover…)
        self.more_b = IconButton(btns, t, "dots", command=self.show_menu, size=32, icon_size=18,
                                 tooltip=lambda: tr("More"))
        btns.add(play_b, shuffle_b, self.edit_b, self.mb_b, self.more_b)
        # the columns the user chose for album pages (the track number first by default),
        # sortable; in track order at first
        self.table = song_table(self, self, kind="album",
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
        """Album Properties…: the Properties window for every song of the album."""
        if self.album is not None:
            return self.app.edit_album_info(self.album)
        return None

    def musicbrainz(self):
        """Acquire Metadata… (2.1 "Get album info from MusicBrainz…"): the MusicBrainz
        window for the album."""
        if self.album is not None:
            return self.app.album_musicbrainz(self.album)
        return None

    def menu(self) -> Optional[tk.Menu]:
        """The "…" button's menu: the album card's menu (album_menu())."""
        return album_menu(self.app, self.album) if self.album is not None else None

    def show_menu(self) -> None:
        m = self.menu()
        if m is not None:
            b = self.more_b
            m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def _fill(self) -> None:
        a = self.album
        if a is None:
            return
        if a["key"] != getattr(self, "_shown_key", None):  # another album: in track order again
            self._shown_key = a["key"]
            self.table.sort_key, self.table.sort_rev = None, False
        self.list_name = tr_name(a["title"])
        self.title_l.configure(text=tr_name(a["title"]))
        self._artist_links(a["artist"])
        meta = " · ".join(x for x in (a["genre"], a["year"]) if x)
        self.meta_l.configure(text=meta)
        self.table.set_rows(a["tracks"])
        minutes = int(round(sum(tr.duration for tr in a["tracks"]) / 60))
        self.table.footer = tr("{a}, {b}", a=plural(len(a["tracks"]), "song"), b=plural(minutes, "minute"))
        self.table.redraw()
        self._draw_cover()

    def _artist_links(self, field: str) -> None:
        """One link per artist of the field (musiclib.split_artists()), ", " between them."""
        t = self.theme
        for w in self.artist_box.winfo_children():
            w.destroy()
        t.prune()
        names = musiclib.split_artists(field, known_artists(self.app)) or ([field] if field else [])
        self.artist_links = []
        for i, name in enumerate(names):
            if i:
                label(self.artist_box, t, ", ", "h2", fg="text2", padx=0).pack(side="left")
            link = label(self.artist_box, t, tr_name(name), "h2", fg="accent", cursor="hand2", padx=0)
            link.pack(side="left")
            link.bind("<Button-1>", lambda e, n=name: self.app.open_artist(n))
            self.artist_links.append(link)
        self.artist_l = self.artist_links[0] if self.artist_links else None

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
GROUPINGS = {  # page: (title, musiclib grouping, noun, what its page shows, icon of a card without covers)
    "artists": (N_("Artists"), musiclib.artists, "artist", "albums", "artist"),
    "album_artists": (N_("Album Artists"), musiclib.album_artists, "album artist", "albums", "mic"),
    "genres": (N_("Genres"), musiclib.genres, "genre", "songs", "genre"),
    "years": (N_("Years"), musiclib.years, "year", "songs", "calendar"),
    "composers": (N_("Composers"), musiclib.composers, "composer", "songs", "piano"),
}
RENAMABLE = ("genres", "composers", "years")  # their menus have Rename… (Genres also Merge into…)
UNKNOWN = {"genres": "Unknown Genre", "composers": "Unknown Composer", "years": "Unknown Year"}
GROUP_TEXTS = {  # page: (its grid without cards, the word before a page's counts, its page with no songs left)
    "artists": (N_("No artists yet."), N_("Artist"), ""),
    "album_artists": (N_("No album artists yet."), N_("Album artist"), ""),
    "genres": (N_("No genres yet."), N_("Genre"), N_("No songs have this genre any more.")),
    "years": (N_("No years yet."), N_("Year"), N_("No songs have this year any more.")),
    "composers": (N_("No composers yet."), N_("Composer"), N_("No songs have this composer any more.")),
}
MOD_NAME = "Cmd" if sys.platform == "darwin" else "Ctrl"


def _library_cache(app) -> dict:
    """What is worked out once per library (app.tracks is a new list after each update)."""
    cache = getattr(app, "_lib_cache", None)
    if cache is None or cache.get("tracks") is not app.tracks:
        cache = {"tracks": app.tracks, "order": {a["key"]: i for i, a in enumerate(app.albums)}}
        app._lib_cache = cache
    return cache


def library_groups(app, kind: str) -> List[dict]:
    """The groups of a grouping page ({"name", "tracks", "albums": album keys in album
    order}), made once per library, so showing or scrolling a page never regroups."""
    cache = _library_cache(app)
    if kind not in cache:
        order, key_of = cache["order"], app._album_key
        out = []
        for g in GROUPINGS[kind][1](app.tracks):
            keys = {key_of.get(t.path, "") for t in g["tracks"]} - {""}
            out.append(dict(g, albums=sorted(keys, key=lambda k: order.get(k, 1 << 30))))
        cache[kind] = out
    return cache[kind]


def known_artists(app) -> set:
    cache = _library_cache(app)
    if "known" not in cache:
        cache["known"] = musiclib.known_artists(app.tracks)
    return cache["known"]


def find_group(app, kind: str, name: str) -> Optional[dict]:
    key = name.strip().casefold()
    return next((g for g in library_groups(app, kind) if g["name"].strip().casefold() == key), None)


def group_card(app, kind: str, g: dict) -> dict:
    """A card of a grouping page: a mosaic of up to four of its album covers, the name,
    "N albums · M songs"."""
    quarters = []
    for key in g["albums"][:4]:
        a = app.album_by_key.get(key)
        if a is not None:
            quarters.append((key, [t.path for t in a["tracks"][:4]]))
    sub = f"{plural(len(g['albums']), 'album')} · {plural(len(g['tracks']), 'song')}"
    return {"key": f"{kind}:{g['name'].strip().casefold()}", "title": g["name"], "artist": sub, "mosaic": quarters,
            "icon": GROUPINGS[kind][4], "tracks": g["tracks"], "name": g["name"]}


class GroupPage(Page):
    """Artists, Album Artists, Genres, Years, Composers: a card each (a mosaic of up to
    four of its album covers, the name in bold, "N albums · M songs" in grey), in the
    grouping's order; a click opens its page ("group").  A card's menu (right click)
    plays it, and on Genres, Composers and Years renames it.  On Genres (2.3) the title
    row has Select: in select mode a click selects a card, and the row shows "N
    selected", Merge… (two or more), Rename… (one) and Done; Ctrl-click (Cmd-click on
    macOS) selects without select mode."""

    def __init__(self, app, kind: str):
        super().__init__(app)
        t = self.theme
        self.kind = kind
        self.title, self.group_fn, self.noun, self.shows, _icon = GROUPINGS[kind]
        self.title = tr(self.title)
        self.list_name = self.title
        head, self.title_l, self.sub = self.heading(self, self.title, buttons=False)
        head.pack(fill="x", padx=28, pady=(22, 6))
        # the title row's buttons of Genres (2.3)
        self.select_b = PillButton(head, t, tr("Select"), icon="check", kind="plain", command=self.start_selecting)
        self.done_b = PillButton(head, t, tr("Done"), command=self.done_selecting)
        self.rename_b = PillButton(head, t, tr("Rename…"), kind="plain", command=lambda: self.rename_selected())
        self.merge_b = PillButton(head, t, tr("Merge…"), kind="plain", command=lambda: self.merge_selected())
        self.count_l = label(head, t, "", "body", fg="text2")
        self.items: List[dict] = []
        self.grid_w = AlbumGrid(self, t, app.covers, self._open, on_context=self._context,
                                multi=kind == "genres", on_selection=self._selection,
                                empty_text=tr(GROUP_TEXTS[kind][0]),
                                label_of=lambda a: app.label_of(app.GROUP_LABEL_KINDS[kind], a.get("name", "")))
        self.grid_w.pack(fill="both", expand=True)
        self.grid_w.canvas.bind("<Escape>", lambda e: self.done_selecting())
        self.refresh()

    def show(self, **kw) -> None:
        if self.grid_w.select_mode or self.grid_w.selected:  # a page shown again starts without a selection
            self.done_selecting()

    def refresh(self) -> None:
        self.items = library_groups(self.app, self.kind)
        self.grid_w.set_albums([group_card(self.app, self.kind, g) for g in self.items], keep=True)
        self._selection()

    def selecting(self) -> bool:
        """The title row shows the selection's buttons: in select mode, or with cards
        selected by Ctrl/Cmd-click."""
        return self.grid_w.multi and (self.grid_w.select_mode or bool(self.grid_w.selected))

    def _selection(self) -> None:
        n = len(self.grid_w.selected)
        text = plural(len(self.items), self.noun) if self.items else ""
        if self.grid_w.multi and self.items:
            text += " · " + (tr("Click genres to select them") if self.grid_w.select_mode else
                             tr("Press Select or {mod}-click genres to merge or rename them", mod=MOD_NAME))
        self.sub.configure(text=text)
        for w in (self.select_b, self.done_b, self.rename_b, self.merge_b, self.count_l):
            w.pack_forget()
        if self.selecting():
            self.count_l.configure(text=tr("{n} selected", n=n))
            self.merge_b.set(enabled=n >= 2)
            self.rename_b.set(enabled=n == 1)
            self.done_b.pack(side="right")
            self.rename_b.pack(side="right", padx=(0, 10))
            self.merge_b.pack(side="right", padx=(0, 10))
            self.count_l.pack(side="right", padx=(0, 14))
        elif self.grid_w.multi and self.items:
            self.select_b.pack(side="right")

    def start_selecting(self) -> None:
        """Select: a click on a card now selects it."""
        self.grid_w.set_select_mode(True)
        self._selection()

    def done_selecting(self) -> None:
        """Done (or Esc): out of select mode, nothing selected."""
        self.grid_w.set_select_mode(False)
        self.grid_w.clear_selection()
        self._selection()

    def _open(self, card: dict) -> None:
        self.app.show_page("group", kind=self.kind, group=card["name"])

    def choose(self, name: str) -> None:
        """Open the page of `name` (as a click on its card)."""
        g = find_group(self.app, self.kind, name)
        if g is not None:
            self.app.show_page("group", kind=self.kind, group=g["name"])

    def _context(self, i: int, e) -> None:
        card = self.grid_w.albums[i]
        sel = self.grid_w.selected_items()
        if len(sel) > 1 and card["key"] in self.grid_w.selected:
            m = self.selection_menu()
        else:
            m = group_menu(self.app, self.kind, card["name"])
        m.tk_popup(e.x_root, e.y_root)

    def selection_menu(self) -> tk.Menu:
        m = tk.Menu(self.app.root, tearoff=False)
        n = len(self.grid_w.selected)
        m.add_command(label=tr("Merge {n} genres…", n=n), command=self.merge_selected)
        m.add_command(label=tr("Clear selection"), command=self.grid_w.clear_selection)
        self.app.theme_menu(m)
        self.app.last_menu = m
        return m

    def merge_selected(self, target: Optional[str] = None, confirm: bool = True):
        """Merge… (or Merge N genres…): the selected genres become one; its name is asked
        for in one dialog (the selected genres to choose from, the biggest first, or a new
        name), then the number of files is confirmed."""
        names = [c["name"] for c in self.grid_w.selected_items()]
        if len(names) < 2:
            return None
        job = merge_groups(self.app, self.kind, names, target, confirm)
        if job is not None:
            self.done_selecting()
        return job

    def rename_selected(self, new: Optional[str] = None, confirm: bool = True):
        """Rename… with exactly one genre selected."""
        names = [c["name"] for c in self.grid_w.selected_items()]
        if len(names) != 1:
            return None
        job = rename_group(self.app, self.kind, names[0], new, confirm)
        if job is not None:
            self.done_selecting()
        return job


class GroupItemPage(Page):
    """One artist, album artist, genre, year or composer (a card of its grouping page):
    its albums as cards (artists) or its songs (the others), Play, Shuffle and "…"
    (its menu: Rename…, Merge into…)."""

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        self.kind, self.name, self.old = "artists", "", ""
        self.item: Optional[dict] = None
        top, self.title_l, self.sub = self.heading(self, "", back=True)
        top.pack(fill="x", padx=28, pady=(22, 6))
        self.more_b = IconButton(top, t, "dots", command=self._more, size=32, icon_size=18,
                                 tooltip=lambda: tr("More"))
        self.more_b.pack(side="right", padx=(0, 10))
        self.grid_w = AlbumGrid(self, t, app.covers, app.open_album,
                                on_context=album_context(app, lambda: self.grid_w),
                                label_of=lambda a: app.label_of("album", a["key"]))
        self.table = song_table(self, self, kind="songs",
                                on_activate=lambda i: app.play_list(self.table.rows, i, self.list_name))

    def show(self, kind: Optional[str] = None, group: Optional[str] = None, **kw) -> None:
        if kind is not None and group is not None and (kind, group) != (self.kind, self.name):
            self.kind, self.name, self.old = kind, group, ""
        self.refresh()

    def renamed(self, new: str) -> None:
        """Its songs are being given a new name: follow them to it."""
        self.old, self.name = self.name, new

    def refresh(self) -> None:
        g = find_group(self.app, self.kind, self.name) or (find_group(self.app, self.kind, self.old)
                                                            if self.old else None)
        self.item = g
        _empty, word, gone = GROUP_TEXTS[self.kind]
        word = tr(word)
        self.title_l.configure(text=tr_name(g["name"] if g else self.name))
        self.list_name = tr_name(g["name"] if g else self.name)
        if self.kind in RENAMABLE:
            self.more_b.pack(side="right", padx=(0, 10))
        else:
            self.more_b.pack_forget()
        if GROUPINGS[self.kind][3] == "albums":
            albums = [self.app.album_by_key[k] for k in (g["albums"] if g else []) if k in self.app.album_by_key]
            self.sub.configure(text=f"{word} · " + tr("{a}, {b}", a=plural(len(albums), "album"),
                                                       b=plural(len(g["tracks"]), "song")) if g else "")
            self.table.pack_forget()
            self.grid_w.pack(fill="both", expand=True)
            self.grid_w.set_albums(albums)
        else:
            self.sub.configure(text=f"{word} · {plural(len(g['tracks']), 'song')}" if g else tr(gone))
            self.grid_w.pack_forget()
            self.table.pack(fill="both", expand=True, padx=12, pady=(0, 8))
            self.table.set_rows(g["tracks"] if g else [], keep=True)

    def menu(self) -> tk.Menu:
        return group_menu(self.app, self.kind, self.item["name"] if self.item else self.name, here=True)

    def _more(self) -> None:
        b = self.more_b
        self.menu().tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def songs(self) -> List[Track]:
        if self.item is None:
            return []
        return album_order(self.item["tracks"]) if GROUPINGS[self.kind][3] == "albums" else list(self.table.rows)

    def likes_changed(self) -> None:
        self.table.redraw()

    def song_changed(self) -> None:
        self.table.redraw()


# ------------------------------------------------- renaming and merging ---
def group_menu(app, kind: str, name: str, here: bool = False) -> tk.Menu:
    """The menu of a genre, year, composer or artist: Open, Play, Shuffle, and for
    genres, composers and years Rename… (genres also Merge into…)."""
    m = tk.Menu(app.root, tearoff=False)

    def songs() -> List[Track]:
        g = find_group(app, kind, name)
        return album_order(g["tracks"]) if g else []
    shown = tr_name(name)  # "Playing from …"
    if not here:
        m.add_command(label=tr("Open"), command=lambda: app.show_page("group", kind=kind, group=name))
    m.add_command(label=tr("Play"), command=lambda: songs() and app.play_list(songs(), 0, shown))
    m.add_command(label=tr("Shuffle"), command=lambda: songs() and app.play_list(songs(), 0, shown, shuffle=True))
    if kind in RENAMABLE:
        m.add_separator()
        m.add_command(label=tr(RENAME_TEXTS[kind]["menu"]), command=lambda: rename_group(app, kind, name))
        if kind == "genres":
            m.add_command(label=tr("Merge into…"), command=lambda: merge_groups(app, kind, [name]))
    if kind in app.GROUP_LABEL_KINDS:  # 2.3
        m.add_separator()
        app.add_label_menu(m, app.GROUP_LABEL_KINDS[kind], name)
    app.theme_menu(m)
    app.last_menu = m
    return m


# The texts of Rename… for each grouping that has it, whole sentences for the translations
RENAME_TEXTS = {
    "genres": {
        "menu": N_("Rename genre…"), "title": N_("Rename genre"),
        "prompt_unknown": N_("Give the songs without a genre this genre:"),
        "prompt": N_("New name of the genre “{name}”:"),
        "ask_unknown": N_("Give {n} without a genre the genre “{new}”?"),
        "ask": N_("Rename the genre “{name}” to “{new}”? The genre of {n} changes."),
        "same": N_("Every song already has the genre “{new}”: nothing to change."),
    },
    "composers": {
        "menu": N_("Rename composer…"), "title": N_("Rename composer"),
        "prompt_unknown": N_("Give the songs without a composer this composer:"),
        "prompt": N_("New name of the composer “{name}”:"),
        "ask_unknown": N_("Give {n} without a composer the composer “{new}”?"),
        "ask": N_("Rename the composer “{name}” to “{new}”? The composer of {n} changes."),
        "same": N_("Every song already has the composer “{new}”: nothing to change."),
    },
    "years": {
        "menu": N_("Rename year…"), "title": N_("Rename year"),
        "prompt_unknown": N_("Give the songs without a year this year:"),
        "prompt": N_("Change the year {name} of these songs to:"),
        "ask_unknown": N_("Give {n} without a year the year “{new}”?"),
        "ask": N_("Change the year {name} to {new}? The release date of {n} changes (only its year)."),
        "same": N_("Every song already has the year “{new}”: nothing to change."),
    },
}


def _value_of(kind: str, t: Track) -> str:
    return t.year if kind == "years" else tagedit.clean_value(getattr(t, tagedit.GROUP_FIELD[kind]))


def _check(kind: str) -> Callable[[str], str]:
    def check(name: str) -> str:
        try:
            return tagedit.check_group_name(kind, name)
        except tagedit.TagError as exc:
            raise ValueError(str(exc)) from exc  # the name dialog shows the message
    return check


def rename_group(app, kind: str, name: str, new: Optional[str] = None, confirm: bool = True):
    """Rename… a genre, composer or year: every song of it gets the new name (a year
    only replaces the year of the release date).  Returns the TagJobWindow, or None."""
    texts = RENAME_TEXTS[kind]
    g = find_group(app, kind, name)
    if g is None:
        return None
    unknown = g["name"] == UNKNOWN.get(kind)
    if new is None:
        prompt = tr(texts["prompt_unknown"]) if unknown else tr(texts["prompt"], name=g["name"])
        new = ask_name_dialog(app, tr(texts["title"]), prompt, "" if unknown else g["name"], _check(kind),
                              N_("Rename"))
    if not new:
        return None
    new = tagedit.check_group_name(kind, new)
    old = g["name"]

    def what(n: str) -> str:
        return tr(texts["ask_unknown"], n=n, new=new) if unknown else tr(texts["ask"], name=old, new=new, n=n)
    return _retag(app, kind, [g], new, confirm, tr(texts["title"]), what)


def merge_groups(app, kind: str, names: List[str], target: Optional[str] = None, confirm: bool = True):
    """Merge into… (one genre) or Merge N genres… (several): their songs get one genre."""
    groups = [g for g in (find_group(app, kind, n) for n in names) if g is not None]
    if not groups:
        return None
    if target is None:
        if len(groups) == 1:
            mine = groups[0]["name"].strip().casefold()
            others = [g["name"] for g in library_groups(app, kind)
                      if g["name"].strip().casefold() != mine and g["name"] != UNKNOWN.get(kind)]
            target = ask_name_dialog(app, tr("Merge into"),
                                     tr("Merge the genre “{name}” into which genre? Choose one, or type a new name.",
                                        name=tr_name(groups[0]["name"])), "", _check(kind), N_("Merge"),
                                     choices=others)
        else:
            biggest = sorted(groups, key=lambda g: -len(g["tracks"]))
            names_ = [g["name"] for g in biggest if g["name"] != UNKNOWN.get(kind)]
            target = ask_name_dialog(app, tr("Merge {count} genres", count=len(groups)),
                                     tr("Merge {count} genres into one. Its name:", count=len(groups)),
                                     names_[0] if names_ else "", _check(kind), N_("Merge"), choices=names_)
    if not target:
        return None
    target = tagedit.check_group_name(kind, target)
    first = tr_name(groups[0]["name"])
    listed = tr(", ").join(tr("“{name}”", name=tr_name(g["name"])) for g in groups)
    count = len(groups)

    def what(n: str) -> str:
        if count == 1:
            return tr("Merge the genre “{name}” into “{target}”? The genre of {n} becomes “{target}”.",
                      name=first, target=target, n=n)
        return tr("Merge {count} genres ({listed}) into “{target}”? The genre of {n} becomes “{target}”.",
                  count=count, listed=listed, target=target, n=n)
    return _retag(app, kind, groups, target, confirm, tr("Merge genres"), what)


def ask_name_dialog(app, title, prompt, initial, check, ok, choices=None) -> Optional[str]:
    from .player_windows import ask_name

    return ask_name(app, title, prompt, initial, check=check, ok=ok, choices=choices)


def _retag(app, kind: str, groups: List[dict], new: str, confirm: bool, title: str,
           what: Callable[[str], str]):
    """Confirm (`what(n)` is the question, n the number of songs), then write the new
    name into the files that do not have it yet."""
    from tkinter import messagebox

    from .player_windows import TagJobWindow

    job = getattr(app, "tag_job", None)
    if job is not None and not job.done:
        messagebox.showinfo(title, tr("Songs are still being changed; try again when that has finished."),
                            parent=app.root)
        return None
    seen, tracks = set(), []
    for g in groups:
        for t in g["tracks"]:
            if t.path not in seen and _value_of(kind, t) != new:
                seen.add(t.path)
                tracks.append(t)
    if not tracks:
        messagebox.showinfo(title, tr(RENAME_TEXTS[kind]["same"], new=new), parent=app.root)
        return None
    n = plural(len(tracks), "song")
    if confirm and not messagebox.askyesno(
            title, tr("{question} Only the information in the files is changed; the music itself is not touched.",
                      question=what(n)), parent=app.root):
        return None
    page = app.pages.get("group")
    if isinstance(page, GroupItemPage) and page.kind == kind and any(page.item is g for g in groups):
        page.renamed(new)
    app.tag_job = TagJobWindow(app, title, kind, [t.path for t in tracks], new)
    return app.tag_job


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
        self.table = song_table(self, self, kind="songs", empty_text=tr(self.EMPTY[which]),
                                on_activate=lambda i: app.play_list(self.table.rows, i, self.list_name))
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
        self.grid_w = AlbumGrid(self.grid_box, t, app.covers, app.open_album,
                                on_context=album_context(app, lambda: self.grid_w),
                                label_of=lambda a: app.label_of("album", a["key"]))
        self.grid_w.pack(fill="both", expand=True)
        self.song_head = label(self, t, tr("Songs"), "h2")
        self.song_head.pack(anchor="w", padx=28, pady=(4, 2))
        self.table = song_table(self, self, kind="songs",
                                on_activate=lambda i: app.play_list(self.table.rows, i, self.list_name))
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
NP_TABS = (("lyrics", N_("Lyrics")), ("upnext", N_("Up Next")), ("details", N_("Details")))  # shown as tr()


def fit_pair(font, a: str, sep: str, b: str, width: int, cache: Optional[dict] = None) -> tuple:
    """`a`, `b` cut with "…" so that "a sep b" fits in `width` pixels; the shorter keeps
    its room and the longer gets the rest (each at least half)."""
    aw, sw, bw = font.measure(a), font.measure(sep), font.measure(b)
    if aw + sw + bw <= width:
        return a, b
    room = max(0, width - sw)
    if aw <= room / 2:
        return a, fit_text(font, b, int(room - aw), cache)
    if bw <= room / 2:
        return fit_text(font, a, int(room - bw), cache), b
    return fit_text(font, a, int(room / 2), cache), fit_text(font, b, int(room / 2), cache)


def artist_line(font, artists: List[str], album_link: Optional[str], album: str, width: int,
                cache: Optional[dict] = None) -> List[tuple]:
    """"Artist, Artist — Album" in pieces (text, link, artist name): each artist its own
    link ("artist"), the album name a link ("album_name") when `album_link` is given, the
    separators plain; cut with "…" to fit in `width` pixels (the shorter part keeps its room).
    A name with no room left is dropped with its ", ", and the name before it ends with "…"
    ("June Harbor… — Album", never "June Harbor,  — Album")."""
    sep = " — "
    shown = [tr_name(a) for a in artists]  # "Unknown Artist" in the language in use; the links keep the name
    joined, album_shown = fit_pair(font, ", ".join(shown), sep, tr_name(album), width, cache)
    budget = font.measure(joined)
    pieces: List[tuple] = []
    used = 0
    for i, name in enumerate(artists):
        comma = font.measure(", ") if i else 0
        w = font.measure(shown[i])
        if used + comma + w <= budget:
            if i:
                pieces.append((", ", "", None))
            pieces.append((shown[i], "artist", name))
            used += comma + w
            continue
        cut = fit_text(font, shown[i], int(budget - used - comma), cache)
        if cut:
            if i:
                pieces.append((", ", "", None))
            pieces.append((cut, "artist", name))
        elif pieces:  # no room for this name: the last one shown ends with "…"
            last = pieces[-1]
            room = font.measure(last[0]) + int(budget - used)
            pieces[-1] = (fit_text(font, last[0] + "…", room, cache), last[1], last[2])
        break
    pieces.append((sep, "", None))
    pieces.append((album_shown, "album_name" if album_link is not None else "", None))
    return pieces


def format_line(t: Track) -> str:
    """Details' format line: "FLAC · 24-bit/96 kHz · 2,304 kbps" (lossy: "MP3 · 44.1 kHz ·
    320 kbps"); parts that are unknown are left out."""
    parts = [t.codec.upper()] if t.codec else []
    khz = f"{t.sample_rate / 1000:g} kHz" if t.sample_rate else ""
    if khz:
        if t.is_lossy or not t.bits:
            parts.append(khz)
        else:
            bits = tr("{n}-bit float", n=t.bits) if t.is_float else tr("{n}-bit", n=t.bits)
            parts.append(f"{bits}/{khz}")
    if t.bit_rate:
        parts.append(f"{round(t.bit_rate / 1000):,} kbps")
    return " · ".join(parts) or "—"


def song_details(t: Track, album_tracks: int, comment: str, verdict: Optional[tuple],
                 artists: Optional[List[str]] = None) -> List[tuple]:
    """Now Playing's Details: (label, value) rows.  `verdict` is (verdict label, summary)
    of a saved Quality check result, or None; `artists` the artists of the artist field
    (`musiclib.split_artists()`), shown as ", "-joined links.
    The labels stay English (DetailsView shows them as tr(label)); the values are shown."""
    dash = "—"
    if t.track and album_tracks >= t.track and album_tracks > 1:
        track_no = tr("{n} of {total}", n=t.track, total=album_tracks)
    else:
        track_no = str(t.track) if t.track else dash
    artist = ", ".join(artists) if artists and t.artist else t.artist
    rows = [(N_("Title"), t.display_title), (N_("Artist"), artist or dash), (N_("Album"), t.album or dash),
            (N_("Album artist"), t.album_artist or dash), (N_("Composer"), t.composer or dash),
            (N_("Genre"), t.genre or dash), (N_("Year"), t.year or dash), (N_("Track"), track_no),
            (N_("Disc"), str(t.disc) if t.disc else dash), (N_("Duration"), fmt_time(t.duration)),
            (N_("Format"), format_line(t)), (N_("File"), t.path)]
    if comment.strip():
        rows.append((N_("Comment"), comment.strip()))
    if verdict is None:
        rows.append((N_("Quality check"), tr("Not checked yet")))
    else:
        rows.append((N_("Quality check"), tr(verdict[0])))
        if verdict[1]:
            rows.append((N_("Why"), tr_text(verdict[1])))
    return rows


def read_comment(path: str) -> str:
    """The comment tag of a song ("" when it has none, or the file cannot be read); ffprobe,
    so call it off the window's thread."""
    import json
    import subprocess

    from .probe import _no_window, tool

    try:
        out = subprocess.run([tool("ffprobe"), "-v", "error", "-show_entries", "format_tags:stream_tags",
                              "-of", "json", path], capture_output=True, timeout=20, **_no_window()).stdout
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    for src in [data.get("format", {})] + list(data.get("streams", [])):
        for k, v in (src.get("tags") or {}).items():
            if k.strip().casefold() == "comment" and isinstance(v, str) and v.strip():
                return v.strip()
    return ""


class Segmented(tk.Canvas):
    """A segmented switch (Now Playing's Lyrics | Up Next | Details): a rounded panel in the
    LCD colour, the chosen segment raised in the page colour."""

    H = 30

    def __init__(self, master, theme: Theme, options, command: Callable[[str], None], value: str = ""):
        super().__init__(master, height=self.H, width=300, highlightthickness=0, bd=0)
        self.theme, self.options, self.command = theme, list(options), command
        self.value = value or self.options[0][0]
        self.hover: Optional[str] = None
        self.boxes: List[tuple] = []
        theme.reg(self, bg="bg")
        theme.on_change(self.redraw, self)
        self.bind("<Motion>", lambda e: self._hov(self._at(e.x)))
        self.bind("<Leave>", lambda e: self._hov(None))
        self.bind("<ButtonRelease-1>", self._click)
        self.redraw()

    def _at(self, x: float) -> Optional[str]:
        return next((k for k, x0, x1 in self.boxes if x0 <= x <= x1), None)

    def _hov(self, key: Optional[str]) -> None:
        if key != self.hover:
            self.hover = key
            self.configure(cursor="hand2" if key and key != self.value else "")
            self.redraw()

    def _click(self, e) -> None:
        key = self._at(e.x)
        if key is not None:
            self.choose(key)

    def choose(self, key: str) -> None:
        self.set(key)
        self.command(key)

    def set(self, key: str) -> None:
        if key != self.value:
            self.value = key
            self.redraw()

    def redraw(self) -> None:
        t = self.theme
        font = t.f("body_b")
        seg = max(font.measure(tr(text)) for _, text in self.options) + 30
        w = seg * len(self.options) + 4
        self.configure(width=w)
        self.delete("all")
        round_rect(self, 0, 0, w, self.H, 9, fill=t.c("lcd"), outline="")
        self.boxes = []
        for i, (key, text) in enumerate(self.options):
            x0, x1 = 2 + i * seg, 2 + (i + 1) * seg
            self.boxes.append((key, x0, x1))
            if key == self.value:
                round_rect(self, x0, 2, x1, self.H - 2, 7, fill=t.c("bg"), outline="")
            elif key == self.hover:
                round_rect(self, x0, 2, x1, self.H - 2, 7, fill=t.c("hover"), outline="")
            self.create_text((x0 + x1) / 2, self.H / 2, text=tr(text), font=font,
                             fill=t.c("text") if key == self.value else t.c("text2"))


class LyricsView(tk.Canvas):
    """Synced lyrics: every line, the current one bold in the text colour and the others
    grey, kept about a third from the top; a click on a line seeks there.  When the user
    scrolls the current line out of view, following stops and `on_away(True)` shows the
    "Back to the current line" button until it is pressed (`back_to_line()`) or the user
    scrolls the line back into view (2.1).  Plain lyrics: the text, scrollable.  Sizes
    from the theme's lyrics fonts (A− / A+)."""

    PAD = 40
    GAP = 16

    def __init__(self, master, app: "PlayerApp", on_away: Optional[Callable[[bool], None]] = None):
        super().__init__(master, highlightthickness=0, bd=0)
        self.app, self.theme = app, app.theme
        self.on_away = on_away
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
        self.away = False      # the user scrolled the current line out of view
        self.followed = -2     # the line scrolled to last (-2: none yet)
        self.wide = False      # Lyrics only (2.3): the lines centred across the whole page
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
        self.current, self.offset, self.target, self.followed = -1, 0.0, 0.0, -2
        self._set_away(False)
        self.layout()

    def _set_away(self, away: bool) -> None:
        if away != self.away:
            self.away = away
            if self.on_away is not None:
                self.on_away(away)

    def layout(self) -> None:
        t = self.theme
        self.delete("all")
        self.items, self.tops, self.heights = [], [], []
        W, H = self.winfo_width(), self.winfo_height()
        if W <= 1:
            return
        width = max(100, W - 2 * self.PAD)
        y = self.PAD / 2
        # Lyrics only (2.3): each line centred on the page
        x, anchor, justify = (W / 2, "n", "center") if self.wide else (self.PAD, "nw", "left")
        if self.kind == lyrics_mod.SYNCED:
            bold, normal = t.f("lyrics_b"), t.f("lyrics")
            for i, (_, line) in enumerate(self.entries):
                item = self.create_text(x, y, text=line or " ", anchor=anchor, width=width, font=bold,
                                        fill=t.c("text2"), tags=("line",), justify=justify)
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
            item = self.create_text(x, y, text=text, anchor=anchor, width=width, font=t.f("plain"),
                                    fill=t.c("text") if self.kind == lyrics_mod.PLAIN else t.c("text2"),
                                    justify=justify)
            box = self.bbox(item)
            y += (box[3] - box[1]) if box else 0
        self.total = y + self.PAD + (H * 2 / 3 if self.kind == lyrics_mod.SYNCED else 0)
        self.configure(scrollregion=(0, 0, W, max(self.total, H)))
        if self.kind == lyrics_mod.SYNCED and not self.away:
            self._scroll_to(self._line_target())  # the current line stays where it was
            self.target = self.offset
        else:
            self._scroll_to(self.offset)

    def _scroll_to(self, offset: float) -> None:
        H = self.winfo_height()
        self.offset = min(max(offset, 0.0), max(0.0, self.total - H))
        self.yview_moveto(self.offset / max(self.total, H, 1))

    def _line_target(self) -> float:
        """The offset that shows the current line a third from the top."""
        if 0 <= self.current < len(self.tops):
            return self.tops[self.current] + self.heights[self.current] / 2 - self.winfo_height() / 3
        return 0.0

    def line_in_view(self) -> bool:
        """The current line (the first one before it starts) is wholly in view."""
        i = self.current if 0 <= self.current < len(self.tops) else 0
        if not self.tops:
            return True
        return self.offset - 1 <= self.tops[i] and self.tops[i] + self.heights[i] <= self.offset + \
            self.winfo_height() + 1

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
        if self.away:
            if self.line_in_view():  # the user scrolled the current line back into view
                self.followed = self.current
                self._set_away(False)
            return
        if self.current != self.followed:  # a new line: scroll it smoothly to a third from the top
            self.followed = self.current
            self.target = self._line_target()
            if self._anim is None and abs(self.target - self.offset) > 0.5:
                self._animate()

    def _animate(self) -> None:
        self._anim = None
        if self.away:
            return
        diff = self.target - self.offset
        if abs(diff) < 0.6:
            self._scroll_to(self.target)
            return
        self._scroll_to(self.offset + diff * 0.18)
        self._anim = self.after(16, self._animate)

    def _stop_animation(self) -> None:
        if self._anim is not None:
            self.after_cancel(self._anim)
            self._anim = None

    def _wheel(self, n: int) -> None:
        self.scroll_by(n * 48)

    def scroll_by(self, pixels: float) -> None:
        """The user scrolls: following stops while the current line is out of view."""
        self._stop_animation()
        self._scroll_to(self.offset + pixels)
        self.target = self.offset
        if self.kind == lyrics_mod.SYNCED and self.entries:
            self.followed = self.current
            self._set_away(not self.line_in_view())

    def back_to_line(self) -> None:
        """The "Back to the current line" button: there at once, and following again."""
        self._stop_animation()
        self._scroll_to(self._line_target())
        self.target = self.offset
        self.followed = self.current
        self._set_away(False)

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
            self.followed = -2  # follow the line clicked
            self._set_away(False)
            self.app.seek(self.entries[i][0] / 1000.0)


class DetailsView(tk.Canvas):
    """Now Playing's Details: two columns, the labels grey on the left, as one block centred
    in the view (2.3; a long value such as the file's path wraps within it), scrollable
    when it is taller than the view.  The artists of the Artist row are links
    (`on_artist(name)`)."""

    PAD = 28
    MAX_VALUE_W = 420  # the values' column at most this wide (pixels)

    def __init__(self, master, theme: Theme, on_artist: Optional[Callable[[str], None]] = None):
        super().__init__(master, highlightthickness=0, bd=0)
        self.theme, self.on_artist = theme, on_artist
        self.rows: List[tuple] = []
        self.artists: List[str] = []
        self.links: List[tuple] = []    # (x0, y0, x1, y1, artist) in canvas coordinates
        self.hover: Optional[int] = None
        self.offset = 0.0
        self.total = 1.0
        self.block = (0.0, 0.0, 0.0, 0.0)  # where the rows are drawn (x0, y0, x1, y1), for the tests
        theme.reg(self, bg="bg")
        self.bind("<Configure>", lambda e: self.layout())
        self.bind("<Motion>", lambda e: self._hov(self._link_at(e)))
        self.bind("<Leave>", lambda e: self._hov(None))
        self.bind("<ButtonRelease-1>", self._click)
        bind_wheel(self, lambda n: self._scroll_to(self.offset + n * 40))
        theme.on_change(self.layout, self)

    def set_rows(self, rows: List[tuple], artists: Optional[List[str]] = None) -> None:
        artists = list(artists or [])
        if rows != self.rows or artists != self.artists:
            self.rows, self.artists = list(rows), artists
            self.offset = 0.0
            self.layout()

    def _link_at(self, e) -> Optional[int]:
        x, y = self.canvasx(e.x), self.canvasy(e.y)
        return next((i for i, (x0, y0, x1, y1, _) in enumerate(self.links) if x0 <= x <= x1 and y0 <= y <= y1), None)

    def _hov(self, i: Optional[int]) -> None:
        if i != self.hover:
            self.hover = i
            self.configure(cursor="hand2" if i is not None else "")
            self.delete("underline")
            if i is not None and i < len(self.links):
                x0, _, x1, y1, _ = self.links[i]
                self.create_line(x0, y1 - 1, x1, y1 - 1, fill=self.theme.c("text"), tags="underline")

    def _click(self, e) -> None:
        i = self._link_at(e)
        if i is not None and self.on_artist is not None:
            self.on_artist(self.links[i][4])

    def _artist_row(self, x: float, y: float, right: float) -> float:
        """The Artist row as links, wrapped at `right`; returns its height."""
        t = self.theme
        font, line = t.f("body"), t.f("body").metrics("linespace")
        x0, top = x, y
        for i, name in enumerate(self.artists):
            for text, link in (((", ", False),) if i else ()) + ((name, True),):
                w = font.measure(text)
                if x + w > right and x > x0 and link:
                    x, y = x0, y + line
                self.create_text(x, y, text=text, anchor="nw", font=font, fill=t.c("text"))
                if link:
                    self.links.append((x, y, x + w, y + line, name))
                x += w
        return y + line - top

    def text(self) -> str:
        return "\n".join(f"{tr(k)}: {v}" for k, v in self.rows)

    def layout(self) -> None:
        t = self.theme
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W <= 1:
            return
        lf, vf = t.f("body"), t.f("body")
        lw = max([lf.measure(tr(k)) for k, _ in self.rows] or [80]) + 18
        # the values' column: as wide as the longest value, at most MAX_VALUE_W and the room left
        widest = max([vf.measure(line) for _, v in self.rows for line in str(v).splitlines()] or [80])
        vw = max(80, min(widest, self.MAX_VALUE_W, W - self.PAD * 2 - lw))
        x0 = max(self.PAD, (W - lw - vw) / 2)
        y = 0.0
        self.links, self.hover = [], None
        for k, v in self.rows:
            self.create_text(x0, y, text=tr(k), anchor="nw", font=lf, fill=t.c("text2"), tags=("block",))
            if k == "Artist" and self.artists and v == ", ".join(self.artists):
                h = self._artist_row(x0 + lw, y, x0 + lw + vw)
            else:
                item = self.create_text(x0 + lw, y, text=v, anchor="nw", font=vf, fill=t.c("text"), width=vw,
                                        tags=("block",))
                box = self.bbox(item)
                h = (box[3] - box[1]) if box else 0
            y += max(h, lf.metrics("linespace")) + 9
        block = max(0.0, y - 9)
        # in the middle of the view; from the top (and scrolling) when it is taller
        top = (H - block) / 2 if block + self.PAD <= H else self.PAD / 2
        self.move("all", 0, top)
        self.links = [(a, b + top, c, d + top, n) for a, b, c, d, n in self.links]
        self.block = (x0, top, x0 + lw + vw, top + block)
        self.total = top + block + self.PAD / 2
        self.configure(scrollregion=(0, 0, W, max(self.total, H)))
        self._scroll_to(self.offset)

    def _scroll_to(self, offset: float) -> None:
        H = self.winfo_height()
        self.offset = min(max(offset, 0.0), max(0.0, self.total - H))
        self.yview_moveto(self.offset / max(self.total, H, 1))


from .player_theme import draw_icon  # noqa: E402

LYRICS_WIDE_EXTRA = 4   # Lyrics only: the lyrics this many pixels larger than the chosen size (2.3)
SWIPE_PX = 60           # a sideways drag this long turns Now Playing's left half (2.3)
DRAG_START_PX = 5       # a press that moves further is a drag, not a click


class BackLink(tk.Canvas):
    """Now Playing's way back (2.3): a chevron and the name of the page it returns to, in
    the accent colour, underlined under the pointer; a click calls `command`."""

    H = 28

    def __init__(self, master, theme: Theme, command: Callable[[], None]):
        super().__init__(master, height=self.H, width=40, highlightthickness=0, bd=0, cursor="hand2")
        self.theme, self.command, self.text, self.hover = theme, command, "", False
        theme.reg(self, bg="bg")
        theme.on_change(self.redraw, self)
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<ButtonRelease-1>", self._click)

    def _hov(self, on: bool) -> None:
        self.hover = on
        self.redraw()

    def _click(self, e) -> None:
        if 0 <= e.x <= int(self.cget("width")) and 0 <= e.y <= self.H:
            self.command()

    def set(self, text: str) -> None:
        if text != self.text:
            self.text = text
            self.redraw()

    def redraw(self) -> None:
        t = self.theme
        font = t.f("body_b")
        w = 24 + font.measure(self.text) + 6
        self.configure(width=w)
        self.delete("all")
        color = t.c("accent")
        draw_icon(self, "back", 10, self.H / 2, 16, color)
        self.create_text(24, self.H / 2, text=self.text, anchor="w", fill=color, font=font, tags=("label",))
        if self.hover:
            y = self.H / 2 + font.metrics("linespace") / 2
            self.create_line(24, y, w - 6, y, fill=color)


class NowPlayingPage(Page):
    """The cover with the title and "Artist — Album" under it, as one block in the middle of
    the left half (the album name opens the album, each artist name that artist, the cover
    the Cover window);
    on the right a switch Lyrics | Up Next | Details (`app.np_tab`).  Lyrics: a label
    ("Synced lyrics" / "Lyrics (not synced)", where they come from), A− / A+, Lyrics only
    and the lyrics; without lyrics "Get lyrics".  With the lyrics hidden (the Lyrics button
    on this page, `app.lyrics_hidden`) only the block, in the middle of the page.

    2.3: an instrumental song's Lyrics view is the block in the middle too, with
    "Instrumental" under it (`mode()`); Lyrics only (`app.lyrics_wide`) hides the cover and
    the lyrics fill the page; a sideways drag (or a horizontal scroll) turns the left half
    to the album's songs (`app.np_side`, two dots under it); a back link at the top left
    names the page it returns to (`app.back_label()`); Details is one centred block."""

    SIZES = (160, 200, 240, 280, 320, 360, 400, 440, 480)
    SONG_ROW = 36  # the album's songs in the left half (2.3)

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        self.art = tk.Canvas(self, highlightthickness=0, bd=0)
        t.reg(self.art, bg="bg")
        self.links: List[tuple] = []          # (what, x0, y0, x1, y1) on the art canvas
        self.hover_link: Optional[str] = None
        self.song_rows: List[tuple] = []      # the album's songs shown: (index, x0, y0, x1, y1)
        self.dots: List[tuple] = []           # the two dots: (side, x0, y0, x1, y1)
        self.songs_first = 0                  # the first of the album's songs shown (scrolled)
        self.songs_key: Optional[str] = None  # the album they are of
        self.songs_hover: Optional[int] = None
        self.press: Optional[tuple] = None    # where the button went down (x, y), for the swipe
        self.dragged = False                  # the press moved: no click
        self.swiped = False                   # this press turned the left half already
        self.art.bind("<Configure>", lambda e: self.draw_art())
        self.art.bind("<Motion>", self._art_motion)
        self.art.bind("<Leave>", lambda e: self._art_leave())
        self.art.bind("<ButtonPress-1>", self._art_press)
        self.art.bind("<B1-Motion>", self._art_drag)
        self.art.bind("<ButtonRelease-1>", self._art_click)
        self.art.bind("<Double-Button-1>", self._art_double)
        bind_wheel(self.art, self._art_wheel)
        self.art.bind("<Shift-MouseWheel>", lambda e: self.swipe(-1 if e.delta > 0 else 1))
        if self.art.tk.call("tk", "windowingsystem") == "x11":
            # a sideways scroll (X11 buttons 6 and 7, which Tk reports as Shift-Button-4/5)
            self.art.bind("<Shift-Button-4>", lambda e: self.swipe(-1))
            self.art.bind("<Shift-Button-5>", lambda e: self.swipe(1))
        self.right = frame(self, t)
        self.switch = Segmented(self.right, t, NP_TABS, app.set_np_tab, app.np_tab)
        self.switch.pack(side="top", pady=(18, 6))
        self.body = frame(self.right, t)
        self.body.pack(side="top", fill="both", expand=True)
        # Lyrics
        self.lyrics_pane = frame(self.body, t)
        head = frame(self.lyrics_pane, t)
        head.pack(side="top", fill="x", padx=(LyricsView.PAD, LyricsView.PAD - 8), pady=(6, 4))
        self.kind_l = label(head, t, "", font="body_b")
        self.kind_l.pack(side="left")
        self.source_l = label(head, t, "", font="small", fg="text2")
        self.source_l.pack(side="left", padx=(8, 0))
        # Lyrics only (2.3): the cover goes and the lyrics fill the page; on while they do
        self.wide_btn = IconButton(head, t, "expand", command=app.toggle_lyrics_wide, size=28, icon_size=15,
                                   tooltip=lambda: tr("Lyrics only"))
        self.wide_btn.pack(side="right", padx=(6, 0))
        self.bigger = PillButton(head, t, "A+", kind="plain", height=26, command=lambda: app.lyrics_size_step(1))
        self.bigger.pack(side="right")
        self.smaller = PillButton(head, t, "A−", kind="plain", height=26, command=lambda: app.lyrics_size_step(-1))
        self.smaller.pack(side="right", padx=(0, 6))
        self.head = head
        self.lyrics = LyricsView(self.lyrics_pane, app, on_away=self._away)
        self.lyrics.pack(side="top", fill="both", expand=True)
        self.back_btn = PillButton(self.lyrics_pane, t, tr("Back to the current line"),
                                   command=self.lyrics.back_to_line)
        self.empty = frame(self.lyrics_pane, t)  # no lyrics: a note and "Get lyrics"
        self.empty_l = label(self.empty, t, tr("No lyrics for this song."), fg="text2", anchor="center")
        self.empty_l.pack(pady=(0, 10))
        self.get_btn = PillButton(self.empty, t, tr("Get lyrics"), icon="lyrics", command=app.get_lyrics_current)
        self.get_btn.pack()
        # Up Next
        self.upnext_pane = frame(self.body, t)
        self.source = label(self.upnext_pane, t, "", "small", fg="text2")
        self.source.pack(side="top", anchor="w", padx=24, pady=(4, 6))
        self.queue_list = SongList(self.upnext_pane, t, [Column("title", "", 200, True, kind="two", key2="artist")],
                                   lambda r, k: r.display_title if k == "title" else tr_name(r.display_artist),
                                   header=False, zebra=False, row_height=48, on_activate=app.jump_to,
                                   empty_text=tr("Nothing comes after this song."), on_move=app.move_in_up_next,
                                   on_context=lambda i, e: app.up_next_menu(i).tk_popup(e.x_root, e.y_root),
                                   label_of=lambda r: app.label_of("song", r.path))
        self.queue_list.pack(side="top", fill="both", expand=True, padx=(12, 8))
        self.queue_paths: Optional[list] = None
        # Details
        self.details = DetailsView(self.body, t, on_artist=app.open_artist)
        self.detail_extra: Dict[str, tuple] = {}  # path -> (comment, verdict), read once per song
        self.detail_busy: set = set()
        self.kind = ""
        self.label = ("", "")
        self.path: Optional[str] = None
        # the way back, at the top left (2.3): "‹ All Songs", "‹ Search", "‹ <album>"…
        self.back_link = BackLink(self, t, command=app.go_back)
        app.covers.listeners.append(lambda key, size: self.draw_art())
        t.on_change(self.draw_art)
        self._place()

    # ---- state -------------------------------------------------------------
    def show(self, **kw) -> None:
        self.song_changed(force=True)
        self.update_back()

    def update_back(self) -> None:
        """The back link at the top left, naming the page it returns to; none when there
        is no page to go back to (2.3)."""
        text = self.app.back_label()
        if text:
            self.back_link.set(text)
            self.back_link.place(x=16, y=10)
            tk.Misc.tkraise(self.back_link)  # (Canvas.tkraise raises canvas items)
        else:
            self.back_link.place_forget()

    def song_changed(self, force: bool = False) -> None:
        path = self.app.current
        if path == self.path and not force:
            return
        self.path = path
        self.queue_paths = None
        if path is not None and path not in self.detail_busy:
            self.detail_extra.pop(path, None)  # its saved verdict may be new: read again
        self.load_lyrics()

    def load_lyrics(self) -> None:
        """Read the song's lyrics again and lay the page out (the song, the lyrics, the
        switch or the hidden state changed)."""
        found = lyrics_mod.read_lyrics_file(self.path) if self.path else None
        if found is None:
            self.kind, self.label = "", ("", "")
        else:
            kind, text, file = found
            self.lyrics.set_lyrics(kind, text)
            self.kind = self.lyrics.kind  # synced lyrics without a timed line show as plain
            self.label = lyrics_mod.shown_label(self.kind, self.path, file)
        self.kind_l.configure(text=tr(self.label[0]) if self.label[0] else "")
        self.source_l.configure(text=tr(self.label[1]) if self.label[1] else "")
        self.size_changed()
        self._place()
        self.draw_art()
        self.update_position(self.app.position())

    def mode(self) -> str:
        """How the page is laid out: "none" (nothing plays), "centred" (only the block in the
        middle: the lyrics hidden), "instrumental" (an instrumental song's Lyrics: the
        block in the middle, the switch above it), "wide" (Lyrics only: the lyrics fill
        the page) or "halves" (2.3)."""
        if self.path is None:
            return "none"
        if self.app.lyrics_hidden:
            return "centred"
        if self.app.np_tab == "lyrics":
            if self.kind == lyrics_mod.INSTRUMENTAL:
                return "instrumental"
            if self.kind and self.app.lyrics_wide:
                return "wide"
        return "halves"

    def showing_right(self) -> bool:
        """The right half (the switch and Lyrics, Up Next or Details) is shown."""
        return self.mode() in ("halves", "wide")

    def showing_lyrics(self) -> bool:
        return self.showing_right() and self.app.np_tab == "lyrics" and bool(self.kind)

    def tab_changed(self) -> None:
        self.switch.set(self.app.np_tab)
        self._place()
        self.draw_art()
        self.update_position(self.app.position())

    def size_changed(self) -> None:
        """A− / A+ or Lyrics only: the lyrics fonts have a new size."""
        size = self.app.state.lyrics_size
        self.smaller.set(enabled=size > playerstate.LYRICS_SIZE_MIN)
        self.bigger.set(enabled=size < playerstate.LYRICS_SIZE_MAX)
        self.wide_btn.set(on=self.app.lyrics_wide)
        self.lyrics.wide = self.mode() == "wide"
        self.lyrics.layout()

    def _away(self, away: bool) -> None:
        if away:
            self.back_btn.place(relx=0.5, rely=1.0, anchor="s", y=-22)
            tk.Misc.tkraise(self.back_btn)  # (Canvas.lift raises canvas items)
        else:
            self.back_btn.place_forget()

    def _raise_back(self) -> None:
        if self.back_link.winfo_manager():
            tk.Misc.tkraise(self.back_link)

    def _place(self) -> None:
        mode = self.mode()
        wide = mode == "wide"
        if self.lyrics.wide != wide:
            self.lyrics.wide = wide
            self.lyrics.layout()
        if mode in ("none", "centred", "instrumental"):
            self.art.place(relx=0, rely=0, relwidth=1, relheight=1)
            if mode == "instrumental":  # only the switch, above the block (Up Next and Details stay at hand)
                self.body.pack_forget()
                self.right.place_forget()  # (forgets relwidth and relheight: the switch's own size)
                self.right.place(relx=0.5, rely=0, anchor="n")
                tk.Misc.tkraise(self.right)
            else:
                self.right.place_forget()
            self._raise_back()
            return
        if not self.body.winfo_manager():
            self.body.pack(side="top", fill="both", expand=True)
        if wide:  # Lyrics only: no cover, the lyrics use the whole page
            self.art.place_forget()
            self.right.place(relx=0, rely=0, relwidth=1, relheight=1, anchor="nw")
        else:
            self.art.place(relx=0, rely=0, relwidth=0.5, relheight=1)
            self.right.place(relx=0.5, rely=0, relwidth=0.5, relheight=1, anchor="nw")
        self._raise_back()
        tab = self.app.np_tab
        for pane, on in ((self.lyrics_pane, tab == "lyrics"), (self.upnext_pane, tab == "upnext"),
                         (self.details, tab == "details")):
            if on:
                pane.pack(fill="both", expand=True)
            else:
                pane.pack_forget()
        if tab == "lyrics":
            if self.kind:
                self.empty.place_forget()
                if not self.lyrics.winfo_manager():
                    self.head.pack(side="top", fill="x", padx=(LyricsView.PAD, LyricsView.PAD - 8), pady=(6, 4))
                    self.lyrics.pack(side="top", fill="both", expand=True)
                self._away(self.lyrics.away)
            else:
                self.head.pack_forget()
                self.lyrics.pack_forget()
                self._away(False)
                self.empty.place(relx=0.5, rely=0.42, anchor="center")
        elif tab == "upnext":
            self.refresh_queue()
        else:
            self.refresh_details()

    # ---- Up Next and Details -----------------------------------------------------
    def refresh_queue(self, keep: bool = False) -> None:
        """`keep`: the scroll position and the selected song stay (Up Next was edited)."""
        app = self.app
        q = app.queue
        paths = app._shown_up_next()
        if paths != self.queue_paths:
            self.queue_paths = paths
            self.queue_list.set_rows([app.by_path[p] for p in paths], keep=keep)
        self.source.configure(text=tr("Playing from {name}", name=app.context_name)
                              if q is not None and app.context_name else "")

    def refresh_details(self) -> None:
        trk = self.app.track(self.path)
        if trk is None:
            self.details.set_rows([])
            return
        album = self.app.album_of(trk)
        extra = self.detail_extra.get(trk.path)
        if extra is None:
            self._read_details(trk.path)
            extra = ("", None)
        artists = musiclib.split_artists(trk.artist, self.app.known_artists) if trk.artist else []
        self.details.set_rows(song_details(trk, len(album["tracks"]) if album else 0, extra[0], extra[1], artists),
                              artists)

    def _read_details(self, path: str) -> None:
        """The comment tag (ffprobe) and the saved verdict, in a thread; then Details again."""
        if path in self.detail_busy:
            return
        self.detail_busy.add(path)
        cfg = settings_mod.Settings.from_dict(self.app.cfg.to_dict())
        out: dict = {}

        def work():
            out["comment"] = read_comment(path)
            out["verdict"] = None
            try:
                from . import gui
                from .store import open_store, settings_key

                store = open_store(cfg)
                if store is not None:
                    try:
                        r = store.lookup(path, settings_key(cfg))
                    finally:
                        store.close()
                    if r is not None:
                        out["verdict"] = (gui.VERDICT_STYLE.get(r.verdict, (r.verdict,))[0], r.summary or "")
            except Exception:  # noqa: BLE001 - no saved results: "Not checked yet"
                pass

        def wait(th):
            if th.is_alive():
                self.after(60, lambda: wait(th))
                return
            self.detail_busy.discard(path)
            self.detail_extra[path] = (out.get("comment", ""), out.get("verdict"))
            if path == self.path and self.app.np_tab == "details":
                self.refresh_details()

        import threading

        th = threading.Thread(target=work, daemon=True, name="realhires-details")
        th.start()
        self.after(60, lambda: wait(th))

    # ---- the cover block ---------------------------------------------------------
    def draw_art(self) -> None:
        """The left half (or the whole page in the centred layouts): the cover block, or
        the album's songs (`app.np_side`, 2.3), and the two dots that switch between them."""
        t, c = self.theme, self.art
        c.delete("all")
        self.links, self.song_rows, self.dots = [], [], []
        W, H = c.winfo_width(), c.winfo_height()
        if W <= 1:
            return
        trk = self.app.track(self.path) if self.path else None
        if trk is None:
            c.create_text(W / 2, H / 2, text=tr("Nothing is playing"), fill=t.c("text2"), font=t.f("h2"))
            return
        album = self.app.album_of(trk)
        if album is not None and self.app.np_side == "songs":
            self._draw_songs(album, W, H)
        else:
            self._draw_block(trk, album, W, H)
        if album is not None:
            self._draw_dots(W, H)
        self._underline()

    def _draw_block(self, trk: Track, album: Optional[dict], W: int, H: int) -> None:
        """The cover with the title and "Artist — Album" under it (and "Instrumental" for an
        instrumental song's Lyrics), one block in the middle."""
        t, c = self.theme, self.art
        instrumental = self.mode() == "instrumental"
        text_h = 80 + (26 if instrumental else 0)  # the lines under the cover
        top = 56 if instrumental else 0  # below the switch
        room = min(W - 80, H - top - 80 - text_h)
        size = max([s for s in self.SIZES if s <= room] or [self.SIZES[0]])
        key = album["key"] if album else trk.path
        paths = [x.path for x in album["tracks"]] if album else [trk.path]
        img = self.app.covers.get(key, paths, size)
        x = (W - size) / 2
        y = top + (H - top - size - text_h) / 2
        draw_cover(c, t, img, x, y, size)
        self.links.append(("cover", x, y, x + size, y + size, None))  # the Cover window
        fit: dict = {}
        c.create_text(W / 2, y + size + 30, text=fit_text(t.f("h1"), trk.display_title, W - 60, fit),
                      fill=t.c("text"), font=t.f("h1"))
        pieces = artist_line(t.f("body"), self.app.artists_of(trk), trk.display_album if album is not None else None,
                             trk.display_album, W - 60, fit)
        font, h = t.f("body"), t.f("body").metrics("linespace")
        left, ty = W / 2 - sum(font.measure(p[0]) for p in pieces) / 2, y + size + 58
        for text, what, name in pieces:
            c.create_text(left, ty, text=text, anchor="w", fill=t.c("text2"), font=font)
            if what:
                self.links.append((what, left, ty - h / 2, left + font.measure(text), ty + h / 2, name))
            left += font.measure(text)
        if instrumental:
            c.create_text(W / 2, ty + 28, text=tr("Instrumental"), fill=t.c("text2"), font=t.f("body"),
                          tags=("instrumental",))

    def _draw_songs(self, album: dict, W: int, H: int) -> None:
        """The album's songs in track order: number (the speaker for the song playing, whose
        title is bold), title, time; hover; a double-click plays one with the album as the
        list (2.3)."""
        t, c = self.theme, self.art
        rows = list(album["tracks"])
        if album["key"] != self.songs_key:  # another album: from the top, with the song playing in view
            self.songs_key, self.songs_first = album["key"], 0
            self._songs_to_current(rows, H)
        width = min(W - 80, 560)
        x0 = (W - width) / 2
        fit: dict = {}
        top = 52 + (40 if self.mode() == "instrumental" else 0)  # under the back link (and the switch)
        c.create_text(x0 + 10, top, text=fit_text(t.f("h2"), tr_name(album["title"]), width - 20, fit), anchor="nw",
                      fill=t.c("text"), font=t.f("h2"))
        list_top = top + 44
        shown = self._songs_shown(H - (top - 52))
        self.songs_first = min(max(self.songs_first, 0), max(0, len(rows) - shown))
        body, bold = t.f("body"), t.f("body_b")
        R = self.SONG_ROW
        for k in range(shown):
            i = self.songs_first + k
            if i >= len(rows):
                break
            r = rows[i]
            y0 = list_top + k * R
            mid = y0 + R / 2
            if i == self.songs_hover:
                round_rect(c, x0, y0 + 1, x0 + width, y0 + R - 1, 6, fill=t.c("hover"), outline="")
            playing = r.path == self.path
            if playing:
                draw_icon(c, "speaker", x0 + 22, mid, 13, t.c("accent"))
            elif r.track:
                c.create_text(x0 + 30, mid, text=str(r.track), anchor="e", fill=t.c("text2"), font=body)
            when = fmt_time(r.duration) if r.duration else ""
            c.create_text(x0 + width - 12, mid, text=when, anchor="e", fill=t.c("text2"), font=body)
            room = width - 44 - body.measure(when) - 28
            c.create_text(x0 + 44, mid, text=fit_text(bold if playing else body, r.display_title, room, fit),
                          anchor="w", fill=t.c("text"), font=bold if playing else body,
                          tags=("song", "playing") if playing else ("song",))
            self.song_rows.append((i, x0, y0, x0 + width, y0 + R))

    def _songs_shown(self, H: int) -> int:
        """How many of the album's songs fit between the album's name and the dots."""
        return max(1, int((H - 52 - 44 - 44) // self.SONG_ROW))

    def _songs_to_current(self, rows: List[Track], H: int) -> None:
        i = next((k for k, r in enumerate(rows) if r.path == self.path), 0)
        shown = self._songs_shown(H)
        if i >= self.songs_first + shown:
            self.songs_first = i - shown + 1

    def _draw_dots(self, W: int, H: int) -> None:
        """Two dots at the bottom: the cover, the album's songs; the one shown is filled."""
        t, c = self.theme, self.art
        y = H - 22
        for k, side in enumerate(("cover", "songs")):
            x = W / 2 + (k - 0.5) * 18
            on = self.app.np_side == side
            r = 4
            c.create_oval(x - r, y - r, x + r, y + r, fill=t.c("text") if on else t.c("disabled"), outline="",
                          tags=("dot", side))
            self.dots.append((side, x - 9, y - 10, x + 9, y + 10))

    def _link_at(self, x: float, y: float) -> Optional[int]:
        return next((i for i, (_, x0, y0, x1, y1, _) in enumerate(self.links) if x0 <= x <= x1 and y0 <= y <= y1),
                    None)

    def _song_at(self, x: float, y: float) -> Optional[int]:
        return next((i for i, x0, y0, x1, y1 in self.song_rows if x0 <= x <= x1 and y0 <= y <= y1), None)

    def _dot_at(self, x: float, y: float) -> Optional[str]:
        return next((side for side, x0, y0, x1, y1 in self.dots if x0 <= x <= x1 and y0 <= y <= y1), None)

    def _art_motion(self, e) -> None:
        self._art_hover(self._link_at(e.x, e.y))
        song = self._song_at(e.x, e.y)
        if song != self.songs_hover:
            self.songs_hover = song
            self.draw_art()
        if self.hover_link is None:
            self.art.configure(cursor="hand2" if self._dot_at(e.x, e.y) else "")

    def _art_hover(self, i: Optional[int]) -> None:
        if i != self.hover_link:
            self.hover_link = i
            self.art.configure(cursor="hand2" if i is not None else "")
            self._underline()

    def _art_leave(self) -> None:
        self._art_hover(None)
        if self.songs_hover is not None:
            self.songs_hover = None
            self.draw_art()

    def _underline(self) -> None:
        """The artist or album name under the pointer is underlined (not the cover)."""
        self.art.delete("underline")
        i = self.hover_link
        if i is not None and i < len(self.links) and self.links[i][0] != "cover":
            _, x0, _, x1, y1, _ = self.links[i]
            self.art.create_line(x0, y1 - 1, x1, y1 - 1, fill=self.theme.c("text2"), tags="underline")

    # ---- the swipe (2.3) ------------------------------------------------------------
    def _art_press(self, e) -> None:
        self.press, self.dragged, self.swiped = (e.x, e.y), False, False

    def _art_drag(self, e) -> None:
        if self.press is None:
            return
        dx, dy = e.x - self.press[0], e.y - self.press[1]
        if abs(dx) > DRAG_START_PX or abs(dy) > DRAG_START_PX:
            self.dragged = True
        if not self.swiped and abs(dx) > SWIPE_PX and abs(dx) > abs(dy):
            self.swiped = True  # one turn per drag, either way
            self.app.set_np_side("cover" if self.app.np_side == "songs" else "songs")

    def swipe(self, direction: int) -> None:
        """A sideways scroll or trackpad swipe: to the right (1) the album's songs, to the
        left (-1) the cover again."""
        if self.path is not None:
            self.app.set_np_side("songs" if direction > 0 else "cover")

    def _art_wheel(self, n: int) -> None:
        if self.app.np_side != "songs" or not self.song_rows:
            return
        self.songs_first += n * 3
        self.draw_art()

    def _art_click(self, e) -> None:
        dragged, self.press = self.dragged, None
        self.dragged = False
        if dragged:
            return  # a drag (or a swipe) is not a click
        side = self._dot_at(e.x, e.y)
        if side is not None:
            self.app.set_np_side(side)
            return
        i = self._link_at(e.x, e.y)
        trk = self.app.track(self.path) if self.path else None
        if i is None or trk is None:
            return
        what, *_, name = self.links[i]
        if what == "artist":
            self.app.open_artist(name)
        elif what == "cover":
            self.app.open_cover_window(trk.path)
        else:
            album = self.app.album_of(trk)
            if album is not None:
                self.app.open_album(album)

    def _art_double(self, e) -> None:
        self._art_press(e)  # (a second press soon after a click comes here, not to _art_press)
        i = self._song_at(e.x, e.y)
        if i is not None:
            self.play_song(i)

    def play_song(self, i: int) -> None:
        """The album's song i (track order) plays, with the album as the list."""
        trk = self.app.track(self.path) if self.path else None
        album = self.app.album_of(trk) if trk is not None else None
        if album is not None and 0 <= i < len(album["tracks"]):
            self.app.play_list(list(album["tracks"]), i, tr_name(album["title"]))

    # ---- every tick ----------------------------------------------------------------
    def update_position(self, seconds: float) -> None:
        if self.showing_lyrics():
            self.lyrics.update_position(seconds)
        elif self.showing_right() and self.app.np_tab == "upnext":
            self.refresh_queue()

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
        self.values: Dict[str, tk.Label] = {}      # the hex value under each label
        self.resets: Dict[str, PillButton] = {}    # More colours: Reset, for each colour the theme sets
        all_colors = themes.colors(theme)
        for i, key in enumerate(themes.COLOR_KEYS):
            self._swatch_cell(grid, i, key, theme["colors"][key], own)
        # 2.3: every other colour the app draws; unset, its swatch shows the derived colour
        label(self, t, tr("More colours"), "body_b").pack(anchor="w", pady=(16, 0))
        label(self, t, tr("Unless the theme sets them, these follow the colours above. Reset makes a colour follow "
                          "them again."), "small", fg="text2", wraplength=SettingsPage.HELP_WIDTH - 80,
              justify="left").pack(anchor="w", pady=(2, 4))
        more = frame(self, t)
        more.pack(anchor="w")
        for i, key in enumerate(themes.EXTRA_COLOR_KEYS):
            set_here = key in theme["colors"]
            self._swatch_cell(more, i, key, all_colors[key], own, derived=not set_here)
        self.own = own

    def _swatch_cell(self, grid, i: int, key: str, value: str, own: bool, derived: bool = False) -> None:
        """A colour of the editor: its swatch (a click changes it), its name and value, and
        for an extra colour the theme sets, Reset (2.3)."""
        from . import themes

        t = self.app.theme
        cell = frame(grid, t)
        cell.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 28), pady=4)
        sw = tk.Canvas(cell, width=30, height=30, highlightthickness=0, bd=0, cursor="hand2")
        t.reg(sw, bg="bg")
        sw.pack(side="left")
        round_rect(sw, 1, 1, 29, 29, 7, fill=value, outline=t.c("sep"))
        sw.bind("<Button-1>", lambda e, k=key: self._pick(k))
        box = frame(cell, t)
        box.pack(side="left", padx=(10, 0))
        label(box, t, tr(themes.color_label(key)), "body").pack(anchor="w")
        line = frame(box, t)
        line.pack(anchor="w")
        shown = tr("{color} (automatic)", color=value) if derived else value
        self.values[key] = label(line, t, shown, "small", fg="text2")
        self.values[key].pack(side="left")
        if key in themes.EXTRA_COLOR_KEYS and not derived and own:
            b = PillButton(line, t, tr("Reset"), kind="plain", height=20,
                           command=lambda k=key: self.app.set_theme_color(self.name, k, None))
            b.pack(side="left", padx=(8, 0))
            self.resets[key] = b
        self.swatches[key] = sw

    def _pick(self, key: str) -> None:
        from tkinter import messagebox

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
        color = key.capitalize() if i18n.language() == "en" and key in themes_mod.COLOR_KEYS \
            else tr(themes_mod.color_label(key))
        got = app.ask_color(themes_mod.colors(theme)[key], f"{theme_label(theme['name'])}: {color}")
        if got:
            app.set_theme_color(self.name, key, got)


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
        side = frame(body, t, width=210)
        side.pack(side="left", fill="y", padx=(12, 0))
        side.pack_propagate(False)
        # under the group list: About (there is no Help menu, docs/player-spec.md 2.1)
        self.about_btn = PillButton(side, t, tr("About {app}", app=APP_NAME), kind="plain", command=app.about)
        self.about_btn.pack(side="bottom", anchor="w", padx=(6, 0), pady=(8, 18))
        side.configure(width=max(210, int(self.about_btn.cget("width")) + 10))
        self.sections = SongList(side, t, [Column("name", "", 160, True)], lambda r, k: tr(r["name"]), header=False,
                                 zebra=False, on_select=self._section, row_height=34)
        self.sections.pack(side="top", fill="both", expand=True)
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
                    (tr("Clear the play history…"), app.clear_history),
                    (tr("Import tab…"), app.import_tab_file),  # 2.3: custom tabs
                    (tr("Open tabs folder"), app.open_tabs_folder)]
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
        if self.group == "Library":  # 2.3: where tab files go, and the files there that are not tabs
            label(parent, t, tr("Tab files (.json) put into the tabs folder appear in the sidebar and on the "
                                "Library page."), "small", fg="text2", wraplength=self.HELP_WIDTH,
                  justify="left").pack(anchor="w", pady=(4, 0))
            self.tab_note = label(parent, t, app.tab_problems_text(), "small", fg="error",
                                  wraplength=self.HELP_WIDTH, justify="left")
            if app.tab_problems:
                self.tab_note.pack(anchor="w", pady=(4, 0))

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
           "NowPlayingPage", "SettingsPage", "LyricsView", "track_value", "track_sort", "album_order",
           "album_menu", "album_context"]
