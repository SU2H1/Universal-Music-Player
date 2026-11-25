"""Where a song was played, in the Tk player (2.4, docs/player-spec.md "3. Where a
song was played"): the player's side of `places` (`PlacesMixin`, which `PlayerApp`
inherits), the rows Settings → Playback shows under the location service, the
place of each play in the day list beside the Your Stats calendar, the "Places"
part of Your Stats, and a place's page (`PlacePage`) with the songs played there.

The lookup runs in a thread of its own, one at a time, and its answer comes back
through `place_q`, which `_places_tick()` drains every second; it also starts the
lookups `places.PlaceCache` says are due (at start or when the setting is turned
on, then at most every 2 hours while music plays).  Nothing is asked while the
setting `play_location` is off, which it is until the user turns it on."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox
from typing import TYPE_CHECKING, Dict, List, Optional

from . import places, stats
from .i18n import N_, tr, tr_text
from .musiclib import Track
from .player_pages import Page, frame, label, plural, song_table, track_sort, track_value
from .player_theme import PillButton, fit_text, round_rect
from .player_widgets import Column

if TYPE_CHECKING:  # pragma: no cover
    from .player_gui import PlayerApp

PLACES_TICK_MS = 1000   # how often answers are taken in and due lookups started
ROW_H = 30              # a place's row on Your Stats
HEAD_H = 26             # a place's line in the day list


def draw_pin(c: tk.Canvas, cx: float, cy: float, s: float, color: str, hole: str = "") -> None:
    """A map pin, about s pixels tall, centred on (cx, cy); `hole` is the colour behind it."""
    r = s * 0.3
    top = cy - s * 0.18
    c.create_polygon(cx - r * 0.82, top + r * 0.55, cx, cy + s * 0.48, cx + r * 0.82, top + r * 0.55, fill=color,
                     outline=color)
    c.create_oval(cx - r, top - r, cx + r, top + r, fill=color, outline=color)
    c.create_oval(cx - r * 0.38, top - r * 0.38, cx + r * 0.38, top + r * 0.38, fill=hole or c.cget("bg"),
                  outline="")


# ----------------------------------------------------------- the app's part ---
class PlacesMixin:
    """The place the plays are stored with: looked up in a thread, kept in
    `place_cache`, stored by `play_place()` with each play; forgetting them."""

    def _init_places(self: "PlayerApp") -> None:
        self.place_cache = places.PlaceCache()
        self.place_client: Optional[places.GeoClient] = None
        self.place_gen = 0                      # answers of an earlier setting are dropped
        self.place_stop = threading.Event()     # set when the app closes
        self.place_q: "queue.Queue" = queue.Queue()
        self.place_thread: Optional[threading.Thread] = None
        self.root.after(PLACES_TICK_MS, self._places_tick)

    def _place_client(self: "PlayerApp") -> places.GeoClient:
        server = places.normalize_server(self.cfg.location_server)
        if self.place_client is None or self.place_client.server != server:
            self.place_client = places.GeoClient(server, stop=self.place_stop)
        return self.place_client

    def _places_tick(self: "PlayerApp") -> None:
        try:
            self.places_poll()
        except tk.TclError:
            return
        self.root.after(PLACES_TICK_MS, self._places_tick)

    def places_poll(self: "PlayerApp") -> None:
        """Take in a lookup's answer, and start the lookup that is due."""
        while True:
            try:
                gen, place, error = self.place_q.get_nowait()
            except queue.Empty:
                break
            if gen == self.place_gen:
                self.place_cache.finish(place, error)
                self._place_status_changed()
        if not self.cfg.play_location:
            return
        if self.place_thread is not None and self.place_thread.is_alive():
            return  # one request at a time, also when the setting was changed while one runs
        client = self._place_client()
        if self.place_cache.due(time.monotonic(), self.engine.playing, client.not_before):
            self.lookup_place()

    def lookup_place(self: "PlayerApp") -> None:
        """Ask the location service now, in a thread (one lookup at a time)."""
        if self.place_cache.busy or (self.place_thread is not None and self.place_thread.is_alive()):
            return
        client, gen = self._place_client(), self.place_gen
        self.place_cache.begin(time.monotonic())

        def work() -> None:
            try:
                place, error = places.lookup_now(client)
            except places.PlaceStopped:
                return
            except Exception as exc:  # noqa: BLE001 - a lookup must never stop the app
                place, error = None, str(exc)
            self.place_q.put((gen, place, error))

        self.place_thread = threading.Thread(target=work, name="place lookup", daemon=True)
        self.place_thread.start()
        self._place_status_changed()

    def play_place(self: "PlayerApp") -> Optional[places.Place]:
        """The place a play is stored with now (None while the setting is off)."""
        cache = getattr(self, "place_cache", None)
        return cache.place if cache is not None and self.cfg.play_location else None

    def places_settings_changed(self: "PlayerApp", old, new) -> None:
        """Turned on, off, or another service: start again (a lookup at once when on)."""
        if not hasattr(self, "place_cache"):
            return
        before = (old.play_location, places.normalize_server(old.location_server))
        after = (new.play_location, places.normalize_server(new.location_server))
        if before == after:
            return
        client = self.place_client
        self.place_gen += 1
        self.place_cache.reset()
        self.place_client = None
        if client is not None and client.server == after[1]:  # the same service: its Retry-After still holds
            self._place_client().not_before = client.not_before
        if new.play_location:
            self.places_poll()
        self._settings_page_again()

    def _place_status_changed(self: "PlayerApp") -> None:
        p = self.pages.get("settings")
        lab = getattr(p, "place_status_l", None)
        if lab is not None:
            try:
                lab.configure(text=self.place_status())
            except tk.TclError:
                pass

    def place_status(self: "PlayerApp") -> str:
        """The Settings note on the lookup: off, looking, the place found, or why none."""
        if not self.cfg.play_location:
            return tr("Turned off: no place is asked for or stored.")
        c = self.place_cache
        if c.place is not None:
            when = time.strftime("%H:%M", time.localtime(c.looked_up)) if c.looked_up else ""
            return tr("Approximate place now: {place} (looked up at {time}).", place=places.place_text(c.place),
                      time=when)
        if c.busy:
            return tr("Looking up the approximate place…")
        if c.error:
            return tr("No place was found: {error}", error=tr_text(c.error))
        return tr("Not looked up yet.")

    def place_note(self: "PlayerApp") -> str:
        """Exactly what the setting sends, and to whom."""
        server = places.normalize_server(self.cfg.location_server)
        host = places.host_of(server)
        if server.lower().startswith("https:"):
            return tr("What is sent: one HTTPS request to {host}, with the app's name, when the app starts and at "
                      "most every 2 hours while music plays. The service sees this computer's IP address; no song, "
                      "path or tag is sent, and the places are kept only on this computer.", host=host)
        return tr("What is sent: one unencrypted HTTP request to {host}, with the app's name, when the app starts "
                  "and at most every 2 hours while music plays. The service sees this computer's IP address; no "
                  "song, path or tag is sent, and the places are kept only on this computer.", host=host)

    def places_count(self: "PlayerApp") -> int:
        try:
            return self.lib.places_count()
        except Exception:  # noqa: BLE001
            return 0

    def forget_places(self: "PlayerApp", confirm: bool = True) -> None:
        """Settings → Forget play locations…: clear every stored place, keep the plays."""
        if confirm and not messagebox.askyesno(tr("Forget play locations"), tr(
                "Forget where your songs were played? The plays stay in the play history and in Your Stats, "
                "only their places are removed."), parent=self.root):
            return
        try:
            self.lib.forget_places()
        except Exception:  # noqa: BLE001 - the history must never stop the app
            return
        self._refresh_pages({"stats", "place"})
        self._settings_page_again()

    def _period(self: "PlayerApp") -> tuple:
        return stats.period_range(self.stats_period, time.time())

    def period_places(self: "PlayerApp") -> List[dict]:
        """The places of Your Stats' period, most played first."""
        return places.period_places(self.plays(*self._period()))

    def place_songs(self: "PlayerApp", key: str) -> List[dict]:
        return places.place_songs(self.plays(*self._period()), key)

    def place_name(self: "PlayerApp", key: str) -> str:
        """A place's name (the Back link, the page title); "" when it has no plays in the period."""
        return next((r["name"] for r in self.period_places() if r["key"] == key), "")

    def show_place(self: "PlayerApp", key: str) -> None:
        self.show_page("place", place=key)

    def _places_shutdown(self: "PlayerApp") -> None:
        self.place_stop.set()

    # ---- Settings → Playback ------------------------------------------------
    def places_settings_rows(self: "PlayerApp", page, parent) -> None:
        """Under the location service: exactly what is sent, the lookup's state, how
        many plays have a place, and Forget play locations…."""
        t = page.theme
        box = frame(parent, t)
        box.pack(anchor="w", fill="x", pady=(10, 0))
        page.place_note_l = label(box, t, self.place_note(), "small", fg="text2", wraplength=page.HELP_WIDTH,
                                  justify="left")
        page.place_note_l.pack(anchor="w")
        page.place_status_l = label(box, t, self.place_status(), "body", fg="text", wraplength=page.HELP_WIDTH,
                                    justify="left")
        page.place_status_l.pack(anchor="w", pady=(6, 0))
        row = frame(box, t)
        row.pack(anchor="w", pady=(8, 0))
        n = self.places_count()
        page.place_count_l = label(row, t, tr("Plays with a place: {n}.", n=f"{n:,}") if n
                                   else tr("No play has a place yet."), "small", fg="text2")
        page.place_count_l.pack(side="left")
        page.forget_places_b = PillButton(row, t, tr("Forget play locations…"), kind="plain",
                                          command=self.forget_places)
        page.forget_places_b.pack(side="left", padx=(12, 0))


# ------------------------------------------------------------- Your Stats ---
def draw_day_place(page, c: tk.Canvas, x: float, y: float, width: float, text: str) -> float:
    """A line of the day list naming the place of the plays under it; returns the bottom."""
    t = page.theme
    mid = y + HEAD_H / 2 + 2
    draw_pin(c, x + 12, mid, 14, t.c("accent") if text else t.c("text2"))
    c.create_text(x + 26, mid, text=fit_text(t.f("small_b"), text or tr("Place unknown"), int(width - 40), page._fit),
                  anchor="w", fill=t.c("text2"), font=t.f("small_b"), tags=("place_head",))
    return y + HEAD_H


def draw_places(page, y: float) -> float:
    """Your Stats' "Places" part: the period's places with their plays and minutes (a
    click opens the place's songs), or a hint; returns the bottom."""
    t, c, app = page.theme, page.area.c, page.app
    W = c.winfo_width()
    m = page.MARGIN
    rows = getattr(page, "place_rows", None) or []
    page.place_boxes = {}
    c.create_text(m, y + 14, text=tr("Places"), anchor="w", fill=t.c("text"), font=t.f("h2"))
    y += 38
    if not rows:
        if app.cfg.play_location:
            c.create_text(m, y + 10, text=tr("No places yet in this period."), anchor="w", fill=t.c("text2"),
                          font=t.f("body"))
        else:
            text = tr("Turn on Settings → Playback → Remember where songs were played to see where you listen.")
            tid = c.create_text(m, y + 10, text=text, anchor="w", fill=t.c("accent"), font=t.f("body"),
                                width=W - 2 * m)
            box = c.bbox(tid)
            page.hits.append((tuple(box), lambda: app.show_page("settings", group="Playback")))
            y = box[3] - 10
        return y + 40
    for i, r in enumerate(rows):
        box = (m, y, W - m, y + ROW_H)
        fill = t.c("hover") if page.hover == box else t.c("zebra") if i % 2 else ""
        if fill:
            round_rect(c, *box, 6, fill=fill, outline="")
        mid = y + ROW_H / 2
        draw_pin(c, m + 14, mid, 15, t.c("accent"), fill)
        right = tr("{plays} · {minutes} min", plays=plural(r["plays"], "play"),
                   minutes=f"{int(r['seconds'] / 60 + 0.5):,}")
        rid = c.create_text(W - m - 12, mid, text=right, anchor="e", fill=t.c("text2"), font=t.f("body"))
        rw = c.bbox(rid)[2] - c.bbox(rid)[0]
        c.create_text(m + 30, mid, text=fit_text(t.f("body"), r["name"], int(W - 2 * m - rw - 60), page._fit),
                      anchor="w", fill=t.c("text"), font=t.f("body"), tags=("place_row",))
        page.hits.append((box, lambda key=r["key"]: app.show_place(key)))
        page.place_boxes[r["key"]] = box
        y += ROW_H
    return y + 20


# ------------------------------------------------------------ a place's page ---
class PlacePage(Page):
    """A place of Your Stats: its name, the period's plays and minutes there, Play,
    Shuffle and the songs played there, most played first."""

    list_name = N_("Places")

    def __init__(self, app: "PlayerApp"):
        super().__init__(app)
        self.key: Optional[str] = None
        self.name = ""
        top, self.title_l, self.sub = self.heading(self, "", back=True)
        top.pack(fill="x", padx=28, pady=(22, 8))
        self.counts: Dict[str, dict] = {}
        cols = [Column("title", N_("Title"), 300, True), Column("artist", N_("Artist"), 200, True),
                Column("album", N_("Album"), 200, True), Column("played", N_("Played"), 120),
                Column("time", N_("Time"), 52, anchor="e"), Column("like", "", 30, kind="heart")]
        self.table = song_table(self, self, cols, value=self._value, sort_value=self._sort,
                                on_activate=lambda i: app.play_list(self.table.rows, i, self.list_name),
                                empty_text=tr("No song was played at this place in this period."))
        self.table.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    def _value(self, row: Track, key: str) -> str:
        """The Played column: when the song was last played at this place."""
        from .player_collections import fmt_played

        if key == "played":
            c = self.counts.get(row.path)
            return fmt_played(c["last"]) if c else ""
        return track_value(row, key)

    def _sort(self, row: Track, key: str):
        if key == "played":
            return -(self.counts.get(row.path) or {}).get("last", 0.0)
        return track_sort(row, key)

    def show(self, place: Optional[str] = None, **kw) -> None:
        if place is not None:
            self.key = place
        self.refresh()

    def refresh(self) -> None:
        from .player_collections import missing_track

        app = self.app
        row = next((r for r in app.period_places() if r["key"] == self.key), None)
        if row is not None:
            self.name = row["name"]
        self.title_l.configure(text=self.name)
        self.list_name = self.name or tr("Places")
        songs = app.place_songs(self.key or "")
        self.counts = {s["path"]: s for s in songs}
        self.table.set_rows([app.by_path.get(s["path"]) or missing_track(s["path"]) for s in songs])
        if row is None:
            self.sub.configure(text=tr("No song was played at this place in this period."))
        else:
            self.sub.configure(text=tr("{period}: {minutes}, {plays}", period=tr(app.stats_period),
                                       minutes=plural(int(row["seconds"] / 60 + 0.5), "minute"),
                                       plays=plural(row["plays"], "play")))

    def songs(self) -> List[Track]:
        return [r for r in self.table.rows if not getattr(r, "missing", False)]

    def likes_changed(self) -> None:
        self.table.redraw()

    def song_changed(self) -> None:
        self.table.redraw()
