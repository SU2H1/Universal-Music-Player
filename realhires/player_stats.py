"""2.4 (docs/player-spec.md, "2.4" item 2) in the Tk player: the names on Your Stats as
links, and the leaderboard: a section of Your Stats and the "Most Played" page.

Every song, artist, album and genre named on Your Stats (the cards, the top lists of
Details, the day beside the calendar, the leaderboard) is a link: a song plays it with
the songs of that list as the queue, an artist opens the artist's page, an album its
page, a genre its page.  The pointer is a hand over a link, and a right click on a song
opens the song menu.  The helpers here register links as boxes on a canvas (`hits`:
(box, command), `menus`: (box, command taking the event)); `LinkArea` handles the
clicks, the pointer and the right clicks of a canvas that has no handlers of its own.

The leaderboard ranks the most played songs, artists or albums of the period chosen on
Your Stats (`stats.leaderboard()`): rank (the top three on a gold, silver and bronze
badge), cover, title, artist, plays and minutes listened, up to 100 rows, ties sharing
a rank.  Its switch (Songs, Artists, Albums) is `PlayerApp.leaderboard_kind`, kept for
the session and shared by the section and the page.  Play / Shuffle play the ranked
songs in rank order (`stats.ranked_songs()`).
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional, Sequence, Tuple

from . import musiclib, stats
from .i18n import N_, tr, tr_name
from .musiclib import Track
from .player_pages import Page, find_group, known_artists, plural
from .player_theme import fit_text, round_rect
from .player_widgets import draw_cover

MEDALS = ("#d4a017", "#9aa0a6", "#c27a3a")  # the rank badges of the top three: gold, silver, bronze
SECTION_ROWS = 10        # the leaderboard section of Your Stats; the Most Played page shows up to stats.TOP
ROW_H, COVER = 52, 40    # a leaderboard row, its cover
KIND_LABELS = {"songs": N_("Songs"), "artists": N_("Artists"), "albums": N_("Albums")}

Box = Tuple[float, float, float, float]
Hits = List[Tuple[Box, Callable[[], None]]]
Menus = List[Tuple[Box, Callable[[object], None]]]


# ------------------------------------------------------------------ data ---
def period_plays(app) -> list:
    """The plays of the period chosen on Your Stats."""
    since, until = stats.period_range(app.stats_period, time.time())
    return app.plays(since, until)


def boards(plays) -> dict:
    """The three leaderboards of these plays, by kind."""
    return {kind: stats.leaderboard(plays, kind) for kind in stats.LEADERBOARD_KINDS}


def tracks_of(app, paths: Sequence[str]) -> List[Track]:
    """The library's songs of these paths, in their order, each once (gone files left out)."""
    out: List[Track] = []
    seen = set()
    for p in paths:
        t = app.by_path.get(p)
        if t is not None and p not in seen:
            seen.add(p)
            out.append(t)
    return out


def play_from(app, paths: Sequence[str], path: str, name: str) -> None:
    """A click on a song: play it, with the songs of its list (`paths`) as the queue."""
    tracks = tracks_of(app, list(paths) + [path])
    t = app.by_path.get(path)
    if t is not None:
        app.play_list(tracks, tracks.index(t), name)


def song_menu_at(app, paths: Sequence[str], path: str, name: str, e) -> Optional[tk.Menu]:
    """A right click on a song: its menu (whose Play plays it with its list)."""
    t = app.by_path.get(path)
    if t is None:
        return None
    tracks = tracks_of(app, list(paths) + [path])
    m = app.song_menu(t, list_name=name, rows=tracks, index=tracks.index(t))
    m.tk_popup(e.x_root, e.y_root)
    return m


def artist_page(app, name: str) -> Optional[Tuple[str, str]]:
    """The page an artist's name opens: (grouping, name) of Artists or Album Artists, as
    `PlayerApp.open_artist()` finds it; None when the library has no such artist."""
    for n in [name] + musiclib.split_artists(name, known_artists(app))[:1]:
        for kind in ("artists", "album_artists"):
            g = find_group(app, kind, n)
            if g is not None:
                return kind, g["name"]
    return None


def genre_page(app, name: str) -> Optional[str]:
    g = find_group(app, "genres", name)
    return g["name"] if g is not None else None


def album_of(app, name: str, artist: str, paths: Sequence[str] = ()) -> Optional[dict]:
    """The library's album of a row of Your Stats: the album of one of its songs, else the
    album of that name (and artist)."""
    for p in paths:
        t = app.by_path.get(p)
        a = app.album_of(t) if t is not None else None
        if a is not None:
            return a
    named = [a for a in app.albums if a["title"].strip().casefold() == name.strip().casefold()]
    same = [a for a in named if a["artist"].strip().casefold() == artist.strip().casefold()]
    return (same or named or [None])[0]


def open_group(app, kind: str, name: str) -> None:
    app.show_page("group", kind=kind, group=name)


# ------------------------------------------------------------------ links ---
def add_link(c: tk.Canvas, item: int, hits: Hits, fn: Optional[Callable[[], None]], menus: Optional[Menus] = None,
             menu: Optional[Callable[[object], None]] = None) -> Optional[Box]:
    """Make the drawn text `item` a link: its box runs `fn` (and `menu` on a right click)."""
    if fn is None and menu is None:
        return None
    b = c.bbox(item)
    if not b:
        return None
    box = (b[0] - 2, b[1] - 1, b[2] + 2, b[3] + 1)
    if fn is not None:
        hits.append((box, fn))
    if menu is not None and menus is not None:
        menus.append((box, menu))
    return box


def artist_link(app, name: str) -> Optional[Callable[[], None]]:
    target = artist_page(app, name)
    return (lambda: open_group(app, *target)) if target else None


def album_link(app, name: str, artist: str, paths: Sequence[str] = ()) -> Optional[Callable[[], None]]:
    a = album_of(app, name, artist, paths)
    return (lambda: app.open_album(a)) if a is not None else None


def genre_link(app, name: str) -> Optional[Callable[[], None]]:
    g = genre_page(app, name)
    return (lambda: open_group(app, "genres", g)) if g else None


def song_link(app, paths: Sequence[str], path: str, name: str) -> Optional[Callable[[], None]]:
    return (lambda: play_from(app, paths, path, name)) if path in app.by_path else None


def song_menu_link(app, paths: Sequence[str], path: str, name: str) -> Optional[Callable[[object], None]]:
    return (lambda e: song_menu_at(app, paths, path, name, e)) if path in app.by_path else None


def hit_at(boxes, x: float, y: float):
    for box, fn in boxes:
        if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
            return box, fn
    return None


class LinkArea:
    """The links of a canvas that has no click handlers of its own (Details, Most Played):
    a click runs the link under the pointer, a right click (Button-3; Button-2 on macOS)
    its menu, and the pointer is a hand over a link."""

    def __init__(self, canvas: tk.Canvas):
        self.c = canvas
        self.hits: Hits = []
        self.menus: Menus = []
        canvas.bind("<Button-1>", self._click, add="+")
        canvas.bind("<Motion>", self._motion, add="+")
        canvas.bind("<Leave>", lambda e: canvas.configure(cursor=""), add="+")
        canvas.bind("<Button-3>", self._menu, add="+")
        canvas.bind("<Button-2>", self._menu, add="+")

    def clear(self) -> None:
        self.hits, self.menus = [], []

    def _xy(self, e) -> Tuple[float, float]:
        return self.c.canvasx(e.x), self.c.canvasy(e.y)

    def _click(self, e) -> None:
        hit = hit_at(self.hits, *self._xy(e))
        if hit:
            hit[1]()

    def _menu(self, e) -> None:
        hit = hit_at(self.menus, *self._xy(e))
        if hit:
            hit[1](e)

    def _motion(self, e) -> None:
        x, y = self._xy(e)
        self.c.configure(cursor="hand2" if hit_at(self.hits, x, y) else "")


# ------------------------------------------------------------ leaderboard ---
def draw_switch(page: Page, c: tk.Canvas, x: float, y: float, hits: Hits, on_change: Callable[[], None]) -> float:
    """The leaderboard's switch (Songs, Artists, Albums) from (x, y); returns its right edge."""
    t, app = page.theme, page.app
    font = t.f("body_b")
    for kind in stats.LEADERBOARD_KINDS:
        text = tr(KIND_LABELS[kind])
        w = font.measure(text) + 26
        on = app.leaderboard_kind == kind
        box = (x, y, x + w, y + 28)
        round_rect(c, *box, 14, fill=t.c("accent") if on else t.c("lcd"), outline="")
        c.create_text(x + w / 2, y + 14, text=text, fill=t.c("on_accent") if on else t.c("text"), font=font)
        if not on:
            hits.append((box, lambda k=kind: (setattr(app, "leaderboard_kind", k), on_change())))
        x += w + 6
    return x


def row_cover(app, kind: str, row: dict) -> Tuple[str, List[str]]:
    """(cover key, paths) of a leaderboard row: the album of its (most played) song."""
    a = album_of(app, row["name"], row["artist"], row["paths"]) if kind == "albums" else None
    if a is None:
        t = next((app.by_path[p] for p in row["paths"] if p in app.by_path), None)
        a = app.album_of(t) if t is not None else None
    if a is not None:
        return a["key"], [x.path for x in a["tracks"]]
    return (row["paths"][0] if row["paths"] else row["key"]), list(row["paths"][:1])


def draw_board(page: Page, c: tk.Canvas, x: float, y: float, width: float, kind: str, rows: List[dict],
               limit: int, hits: Hits, menus: Menus, list_name: str, fit: dict) -> float:
    """Leaderboard rows from (x, y), `width` wide: rank (the top three on a medal), cover,
    name and artist (links), plays and minutes; returns the bottom."""
    t, app = page.theme, page.app
    if not rows:
        c.create_text(x, y + 14, text=tr("No plays yet in this period."), anchor="w", fill=t.c("text2"),
                      font=t.f("body"))
        return y + 40
    queue = stats.ranked_songs(rows)
    right = x + width
    for i, r in enumerate(rows[:limit]):
        top, mid = y, y + ROW_H / 2
        if i % 2:
            round_rect(c, x, top + 1, right, top + ROW_H - 1, 8, fill=t.c("zebra"), outline="")
        rank = r["rank"]
        if rank <= 3:
            color = MEDALS[rank - 1]
            c.create_oval(x + 6, mid - 13, x + 32, mid + 13, fill=color, outline="")
            c.create_text(x + 19, mid, text=str(rank), fill="#ffffff", font=t.f("body_b"))
        else:
            c.create_text(x + 19, mid, text=str(rank), fill=t.c("text2"), font=t.f("body_b"))
        key, paths = row_cover(app, kind, r)
        draw_cover(c, t, app.covers.get(key, paths, COVER), x + 42, mid - COVER / 2, COVER)
        stat = tr("{plays} · {minutes} min", plays=plural(r["plays"], "play"), minutes=f"{r['minutes']:,}")
        sid = c.create_text(right - 12, mid, text=stat, anchor="e", fill=t.c("text2"), font=t.f("small"))
        sb = c.bbox(sid)
        room = int((sb[0] if sb else right - 140) - (x + 94) - 16)
        name_font = t.f("body_b")
        playing = kind == "songs" and r["key"] == app.current
        if kind == "songs":
            fn, menu = song_link(app, queue, r["key"], list_name), song_menu_link(app, queue, r["key"], list_name)
            name = r["name"]
        elif kind == "artists":
            fn, menu, name = artist_link(app, r["name"]), None, tr_name(r["name"])
        else:
            fn, menu, name = album_link(app, r["name"], r["artist"], r["paths"]), None, tr_name(r["name"])
        if kind == "artists":
            nid = c.create_text(x + 94, mid, text=fit_text(name_font, name, room, fit), anchor="w",
                                fill=t.c("accent") if playing else t.c("text"), font=name_font)
            add_link(c, nid, hits, fn)
        else:
            nid = c.create_text(x + 94, mid - 9, text=fit_text(name_font, name, room, fit), anchor="w",
                                fill=t.c("accent") if playing else t.c("text"), font=name_font)
            add_link(c, nid, hits, fn, menus, menu)
            aid = c.create_text(x + 94, mid + 10, text=fit_text(t.f("small"), tr_name(r["artist"]), room, fit),
                                anchor="w", fill=t.c("text2"), font=t.f("small"))
            add_link(c, aid, hits, artist_link(app, r["artist"]))
        if kind == "songs" and menu is not None:  # the whole row opens the song menu on a right click
            menus.append(((x, top, right, top + ROW_H), menu))
        y += ROW_H
    return y + 8


def section_heading(page: Page, c: tk.Canvas, x: float, y: float, width: float, hits: Hits,
                    on_change: Callable[[], None], see_all: bool) -> float:
    """"Leaderboard" (or nothing on the Most Played page), the switch, and See All."""
    t = page.theme
    if see_all:
        c.create_text(x, y + 14, text=tr("Leaderboard"), anchor="w", fill=t.c("text"), font=t.f("h2"))
        sid = c.create_text(x + width, y + 14, text=tr("See All"), anchor="e", fill=t.c("accent"),
                            font=t.f("body_b"))
        add_link(c, sid, hits, lambda: page.app.show_page("most_played"))
        y += 36
    draw_switch(page, c, x, y, hits, on_change)
    return y + 40


# ------------------------------------------------------------- Most Played ---
class MostPlayedPage(Page):
    """The leaderboard of the period chosen on Your Stats as a page of its own (2.4): the
    period's menu, Play and Shuffle (the ranked songs in rank order), the switch and up
    to 100 rows."""

    list_name = N_("Most Played")

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        top, self.title_l, self.sub = self.heading(self, tr("Most Played"), back=True)
        top.pack(fill="x", padx=28, pady=(22, 6))
        self.period = tk.StringVar(value=tr(app.stats_period))
        self.period_box = cb = ttk.Combobox(top, textvariable=self.period, values=[tr(p) for p in stats.PERIODS],
                                            state="readonly", width=14, style="P.TCombobox", font=t.f("body"))
        cb.pack(side="right", padx=(0, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self._period())
        from .player_collections import ScrollCanvas  # noqa: PLC0415 (that module imports this one)

        self.area = ScrollCanvas(self, self, self._draw)
        self.area.pack(fill="both", expand=True)
        self.links = LinkArea(self.area.c)
        self.rows: List[dict] = []
        self._fit: dict = {}
        app.covers.listeners.append(lambda key, size: size == COVER and self.winfo_exists()
                                    and app.page_name == "most_played" and self.area.redraw())

    def _period(self) -> None:
        i = self.period_box.current()
        if 0 <= i < len(stats.PERIODS):
            self.app.stats_period = stats.PERIODS[i]
        self.refresh()

    def show(self, **kw) -> None:
        self.period.set(tr(self.app.stats_period))
        self.refresh()

    def refresh(self) -> None:
        self.rows = stats.leaderboard(period_plays(self.app), self.app.leaderboard_kind)
        self.sub.configure(text=tr(self.app.stats_period))
        self.area.redraw()

    def song_changed(self) -> None:
        if self.app.leaderboard_kind == "songs":
            self.area.redraw()

    def songs(self) -> List[Track]:
        """What Play and Shuffle play: the ranked songs in rank order."""
        return tracks_of(self.app, stats.ranked_songs(self.rows))

    def _draw(self) -> float:
        c = self.area.c
        W = c.winfo_width()
        self.links.clear()
        y = section_heading(self, c, 28, 6, W - 56, self.links.hits, self.refresh, see_all=False)
        return draw_board(self, c, 28, y, W - 56, self.app.leaderboard_kind, self.rows, stats.TOP,
                          self.links.hits, self.links.menus, self.list_name, self._fit) + 20


__all__ = ["MostPlayedPage", "LinkArea", "draw_board", "section_heading", "period_plays", "boards", "add_link",
           "artist_link", "album_link", "genre_link", "song_link", "song_menu_link", "play_from", "tracks_of",
           "MEDALS", "SECTION_ROWS"]
