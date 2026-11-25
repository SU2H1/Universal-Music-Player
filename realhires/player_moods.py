"""Moods, tempo and playlists in the Tk player (2.4, docs/player-spec.md "4. Moods,
tempo and playlists"): the player's side of `moods` (`MoodsMixin`, which `PlayerApp`
inherits), the Moods page (`MoodsPage`: a card per mood and per tempo, with its number
of songs) and a card's page (`MoodPage`: its songs, Play, Shuffle and Create Playlist).

The analysis runs in a thread after every library update, one song at a time
(`moods.run_analysis()`), when the setting `analyse_moods` is on; its progress is shown
in the sidebar's status when no other text is there.  Results reach the window through
the mixin's own queue (`_poll_moods()`), and the pages are redrawn with them at most
every `REDRAW_S` seconds while the job runs.  Each song's result is kept on its Track
as `mood` (a `moods.Mood`, None when not analysed yet), where the BPM and Mood columns
of the song lists and Now Playing's Details find it."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from . import moods
from .i18n import N_, tr
from .musiclib import Track
from .player_collections import ScrollCanvas
from .player_pages import Page, label, plural, song_table
from .player_theme import PillButton, draw_icon, fit_text, mix, round_rect

if TYPE_CHECKING:  # pragma: no cover
    from .player_gui import PlayerApp

POLL_MS = 150     # how often the window looks at the job's queue
REDRAW_S = 2.0    # the pages show new results at most this often while the job runs

# the colour of each card (white text stays readable on all of them)
CARD_COLORS = {"Happy": "#e8650a", "Sad": "#1a6fd8", "Chill": "#0f8f8c", "Intense": "#e0245e",
               "Up-tempo": "#8e44ad", "Mid-tempo": "#5856d6", "Slow": "#3f51b5"}
SECTIONS = ((N_("Moods"), "mood"), (N_("Tempo"), "tempo"))


def card_icon(kind: str, value: str) -> str:
    """A smiling or a frowning face, or a metronome for the tempos."""
    if kind == "tempo":
        return "tempo"
    return "mood_sad" if value in ("Sad", "Intense") else "mood"


def mood_of(t: Optional[Track]) -> Optional[moods.Mood]:
    """The analysis of a song, or None (not analysed yet)."""
    return getattr(t, "mood", None) if t is not None else None


def mood_value(t: Track, key: str) -> str:
    """The BPM and Mood columns of the song lists."""
    m = mood_of(t)
    if key == "bpm":
        return moods.bpm_text(m)
    if key == "mood":
        return tr(m.mood) if m is not None and m.mood else ""
    return ""


def mood_tip(t: Track) -> str:
    """The tooltip of the BPM and Mood columns: everything the analysis found, as an estimate."""
    m = mood_of(t)
    if m is None:
        return tr("Not analysed yet")
    if not m.mood:
        return tr("No mood: the song is silent or too short.")
    parts = [tr(m.mood), tr(m.energy_label)]
    if m.bpm:
        parts.append(tr("{bpm} BPM, {tempo}", bpm=m.bpm, tempo=tr(m.tempo)))
    if m.key:
        parts.append(tr(moods.key_name(m.key, m.mode)))
    return tr("Estimated from the audio: {what}", what=" · ".join(parts))


def detail_rows(m: Optional[moods.Mood]) -> List[tuple]:
    """Now Playing's Details rows for a song's mood, tempo and key (labels English, as
    `player_pages.song_details()` gives them)."""
    dash = "—"
    if m is None:
        return [(N_("Mood (estimated)"), tr("Not analysed yet"))]
    if not m.mood:
        return [(N_("Mood (estimated)"), tr("No mood: the song is silent or too short."))]
    tempo = tr("{bpm} BPM, {tempo}", bpm=m.bpm, tempo=tr(m.tempo)) if m.bpm else dash
    key = tr(moods.key_name(m.key, m.mode)) if m.key else dash
    return [(N_("Mood (estimated)"), tr("{a}, {b}", a=tr(m.mood), b=tr(m.energy_label))),
            (N_("Tempo (estimated)"), tempo), (N_("Key (estimated)"), key)]


# ----------------------------------------------------------- the app's part ---
class MoodsMixin:
    """The moods of the library: read from library.sqlite at start, analysed in the
    background, shown on the Moods page, in the song lists and in Details."""

    def _init_moods(self: "PlayerApp") -> None:
        self.moods: Dict[str, moods.Mood] = {}
        self.mood_q: queue.Queue = queue.Queue()
        self.mood_thread: Optional[threading.Thread] = None
        self.mood_stop = threading.Event()
        self.mood_progress: Optional[Tuple[int, int]] = None
        self.mood_last: Optional[Tuple[int, int]] = None   # (analysed, failed) of the last run
        self.mood_message = ""                             # what Create Playlist made, for the tests
        self._moods_again = False
        self._moods_pending = False
        self._moods_shown = 0.0
        self._mood_status = ""                            # the status text the job set last
        self._moods_ver = 0                               # changes with the results and the library
        self._mood_counts: Optional[Tuple[int, Dict[str, int]]] = None
        try:
            store = moods.MoodStore()
            self.moods = store.all()
            store.close()
        except Exception:  # noqa: BLE001 - a broken index must not stop the app
            pass
        self.root.after(POLL_MS, self._poll_moods)

    def attach_moods(self: "PlayerApp") -> None:
        """Give every track its analysis (set_tracks() calls this)."""
        self._moods_ver += 1
        for t in self.tracks:
            t.mood = self.moods.get(t.path)  # type: ignore[attr-defined]

    def moods_busy(self: "PlayerApp") -> bool:
        return self.mood_thread is not None and self.mood_thread.is_alive()

    def analyse_moods(self: "PlayerApp", force: bool = False) -> bool:
        """Analyse the songs that need it in a thread (after each library update when
        `analyse_moods` is on, or by hand with force).  False when it did not start."""
        if not force and not self.cfg.analyse_moods:
            return False
        if self.moods_busy():
            self._moods_again = True
            return False
        if not self.tracks or self.library_busy():
            return False
        tracks = list(self.tracks)
        stop = threading.Event()
        self.mood_stop = stop
        out = self.mood_q

        def work():
            store = None
            # 2.4: the analysis itself runs in a worker process, not in this thread: its
            # Python-level work held the interpreter lock long enough to starve the window
            # (the macOS Intel CI runner timed out a GUI test while it ran); the process has
            # its own, so the window stays responsive however slow the computer is
            pool = None
            try:
                import multiprocessing

                from .pool import MultiProcessPool

                # below normal priority, as its FFmpeg is: background work never slows the window
                pool = MultiProcessPool(1, mp_context=multiprocessing.get_context("spawn"),
                                        initializer=moods.lower_priority)
            except Exception:  # noqa: BLE001 - no worker process: analyse in this thread
                pool = None

            def analyse(path: str, duration: float) -> moods.Mood:
                if pool is None:
                    return moods.analyse_file(path, duration)
                return pool.submit(moods.analyse_file, path, duration).result()

            try:
                store = moods.MoodStore()
                res = moods.run_analysis(tracks, store, stop,
                                         progress=lambda i, n, p, m: out.put(("progress", i, n, p, m)),
                                         analyse=analyse)
                out.put(("done", res))
            except Exception:  # noqa: BLE001 - the analysis must never stop the app
                out.put(("done", None))
            finally:
                if store is not None:
                    store.close()
                if pool is not None:
                    pool.shutdown(wait=False, cancel_futures=True)

        self._moods_again = False
        self.mood_progress = (0, 0)
        self.mood_thread = threading.Thread(target=work, daemon=True, name="realhires-moods")
        self.mood_thread.start()
        return True

    def stop_moods(self: "PlayerApp") -> None:
        self.mood_stop.set()

    def moods_settings_changed(self: "PlayerApp", old, new) -> None:
        if old.analyse_moods == new.analyse_moods:
            return
        if new.analyse_moods:
            self.analyse_moods()
        else:
            self.stop_moods()
        self._refresh_pages({"moods"})

    def _poll_moods(self: "PlayerApp") -> None:
        done = None
        try:
            while True:
                kind, *rest = self.mood_q.get_nowait()
                if kind == "progress":
                    i, n, path, m = rest
                    self.mood_progress = (i, n)
                    if m is not None:
                        self.moods[path] = m
                        t = self.by_path.get(path)
                        if t is not None:
                            t.mood = m  # type: ignore[attr-defined]
                        self._moods_pending = True
                    if i < n and not self.library_busy():
                        self._set_mood_status(tr("Analysing moods: {done} of {total} songs", done=f"{i:,}",
                                                total=f"{n:,}"))
                else:
                    done = rest[0]
        except queue.Empty:
            pass
        except tk.TclError:
            return
        now = time.monotonic()
        if self._moods_pending and (done is not None or now - self._moods_shown >= REDRAW_S):
            self._moods_pending = False
            self._moods_shown = now
            self.moods_changed()
        if done is not None or (self.mood_progress is not None and not self.moods_busy() and self.mood_q.empty()):
            self._moods_finished(done)
        try:
            self.root.after(POLL_MS, self._poll_moods)
        except tk.TclError:
            pass

    def _set_mood_status(self: "PlayerApp", text: str) -> None:
        """The job's progress in the sidebar's status, the least important of its texts:
        it never replaces another one (the library update, MusicBrainz, a message)."""
        shown = self.sidebar.status.cget("text")
        if not shown or shown == self._mood_status:
            self._mood_status = text
            self.sidebar.set_status(text)

    def _moods_finished(self: "PlayerApp", res) -> None:
        self.mood_progress = None
        self.mood_last = res
        self._set_mood_status("")  # as after a library update, the status is empty again
        if self._moods_again:
            self.analyse_moods()

    def moods_changed(self: "PlayerApp") -> None:
        """New results: the Moods pages, the Library page's count, the tabs, the song
        lists' BPM and Mood columns and Now Playing's Details show them."""
        self._moods_ver += 1
        self.tab_cache_clear()
        self._refresh_pages({"moods", "mood", "library", "tab"})
        for table in getattr(self, "song_tables", []):
            try:
                if table.winfo_exists():
                    table.redraw()
            except tk.TclError:
                pass
        np = self.pages.get("nowplaying")
        if np is not None and self.page_name == "nowplaying" and hasattr(np, "refresh_details"):
            np.refresh_details()

    # ---- what the pages show ----------------------------------------------------
    def mood_counts(self: "PlayerApp") -> Dict[str, int]:
        """The number of songs of each card, worked out once per library and results."""
        if self._mood_counts is None or self._mood_counts[0] != self._moods_ver:
            self._mood_counts = (self._moods_ver, moods.card_counts(self.tracks, self.moods))
        return self._mood_counts[1]

    def mood_tracks(self: "PlayerApp", kind: str, value: str) -> List[Track]:
        paths = moods.card_songs(kind, value, self.tracks, self.moods)
        return [self.by_path[p] for p in paths if p in self.by_path]

    def moods_entry_count(self: "PlayerApp") -> str:
        """What the Library page's Moods entry says under its name."""
        n = moods.analysed_count(self.tracks, self.moods)
        if not n:
            return tr("Not analysed yet")
        return tr("1 song analysed") if n == 1 else tr("{n} songs analysed", n=f"{n:,}")

    def open_mood(self: "PlayerApp", kind: str, value: str) -> None:
        self.show_page("mood", kind=kind, label=value)

    def create_mood_playlist(self: "PlayerApp", kind: str, value: str) -> Optional[str]:
        """Create Playlist: an ordinary playlist of a card's songs ("Sad songs"; a taken
        name gets " 2"); says what was made.  Returns its id (None without songs)."""
        paths = [t.path for t in self.mood_tracks(kind, value)]
        if not paths:
            return None
        pid = self.new_playlist(songs=paths, name=tr(moods.playlist_name(value)), open_page=False)
        p = self.state.playlist(pid) if pid else None
        if p is None:
            return None
        n = len(p["songs"])
        self.mood_message = (tr("Made the playlist “{name}” with 1 song.", name=p["name"]) if n == 1 else
                             tr("Made the playlist “{name}” with {n} songs.", name=p["name"], n=f"{n:,}"))
        msg = self.mood_message
        self.sidebar.set_status(msg)
        self.root.after(4000, lambda: self.sidebar.status.cget("text") == msg and self.sidebar.set_status(""))
        page = self.pages.get("mood")
        if isinstance(page, MoodPage):
            page.made.configure(text=self.mood_message)
        return pid


# ------------------------------------------------------------------- the pages ---
class MoodsPage(Page):
    """A card per mood (Happy, Sad, Chill, Intense) and per tempo (Up-tempo, Mid-tempo,
    Slow), each with its colour, its number of songs and what it means; a click opens
    its songs.  The page says that the labels are estimates, and how far the analysis is."""

    list_name = N_("Moods")
    MARGIN, CARD_W, CARD_H, GAP = 28, 190, 104, 14

    def __init__(self, app):
        super().__init__(app)
        self.area = ScrollCanvas(self, self, self._draw)
        self.area.pack(fill="both", expand=True)
        self.c = self.area.c
        self.hits: List[Tuple[tuple, Callable[[], None]]] = []
        self.card_hits: Dict[str, tuple] = {}   # where each card was drawn, by its label (for the tests)
        self.hover: Optional[tuple] = None
        self._fit: dict = {}
        self.c.bind("<Button-1>", self._click)
        self.c.bind("<Motion>", self._motion)
        self.c.bind("<Leave>", lambda e: self._set_hover(None))

    def refresh(self) -> None:
        self.area.redraw()

    def show(self, **kw) -> None:
        self.refresh()

    def status_text(self) -> str:
        app = self.app
        n, total = moods.analysed_count(app.tracks, app.moods), len(app.tracks)
        text = tr("{n} of {total} songs analysed", n=f"{n:,}", total=f"{total:,}")
        if not app.cfg.analyse_moods:
            return tr("{text}. Analysing is turned off in Settings → Library.", text=text)
        if app.moods_busy():
            return tr("{text}, analysing…", text=text)
        return text

    def _draw(self) -> float:
        t, c, app = self.theme, self.c, self.app
        W = c.winfo_width()
        m = self.MARGIN
        self.hits, self.card_hits = [], {}
        c.create_text(m, 40, text=tr("Moods"), anchor="w", fill=t.c("text"), font=t.f("title"))
        c.create_text(m, 72, text=self.status_text(), anchor="w", fill=t.c("text2"), font=t.f("body"),
                      width=max(100, W - 2 * m))
        note = c.create_text(m, 94, text=tr(moods.ESTIMATE_NOTE), anchor="nw", fill=t.c("text2"),
                             font=t.f("small"), width=max(100, W - 2 * m))
        y = c.bbox(note)[3] + 18
        counts = app.mood_counts()
        cols = max(1, int((W - 2 * m + self.GAP) // (self.CARD_W + self.GAP)))
        cw = (W - 2 * m - (cols - 1) * self.GAP) / cols
        for title, kind in SECTIONS:
            c.create_text(m, y + 12, text=tr(title), anchor="w", fill=t.c("text"), font=t.f("h2"))
            y += 34
            cards = [label_ for k, label_ in moods.CARDS if k == kind]
            for i, name in enumerate(cards):
                r, k = divmod(i, cols)
                x0 = m + k * (cw + self.GAP)
                y0 = y + r * (self.CARD_H + self.GAP)
                box = (x0, y0, x0 + cw, y0 + self.CARD_H)
                color = CARD_COLORS[name]
                round_rect(c, *box, 14, fill=mix(color, "#000000", 0.15) if self.hover == box else color, outline="")
                draw_icon(c, card_icon(kind, name), x0 + cw - 26, y0 + 26, 22, "#ffffff")
                c.create_text(x0 + 16, y0 + 26, text=fit_text(t.f("h2"), tr(name), int(cw - 56), self._fit),
                              anchor="w", fill="#ffffff", font=t.f("h2"))
                c.create_text(x0 + 16, y0 + 54, text=plural(counts.get(name, 0), "song"), anchor="w",
                              fill="#ffffff", font=t.f("body_b"))
                c.create_text(x0 + 16, y0 + 78, text=fit_text(t.f("small"), tr(moods.CARD_NOTES[name]),
                                                              int(cw - 28), self._fit),
                              anchor="w", fill="#ffffff", font=t.f("small"))
                self.hits.append((box, lambda k=kind, n=name: app.open_mood(k, n)))
                self.card_hits[name] = box
            y += ((len(cards) + cols - 1) // cols) * (self.CARD_H + self.GAP) + 10
        return y + 20

    def _hit(self, e) -> Optional[tuple]:
        x, y = self.c.canvasx(e.x), self.c.canvasy(e.y)
        return next(((b, fn) for b, fn in self.hits if b[0] <= x <= b[2] and b[1] <= y <= b[3]), None)

    def _click(self, e) -> None:
        hit = self._hit(e)
        if hit:
            hit[1]()

    def _set_hover(self, box: Optional[tuple]) -> None:
        self.c.configure(cursor="hand2" if box else "")
        if box != self.hover:
            self.hover = box
            self.area.redraw()

    def _motion(self, e) -> None:
        hit = self._hit(e)
        self._set_hover(hit[0] if hit else None)

    def songs(self) -> List[Track]:
        return [t for t in self.app.tracks if mood_of(t) is not None and mood_of(t).mood]


class MoodPage(Page):
    """A card's songs: its name, "N songs, M minutes", Play, Shuffle, Create Playlist (an
    ordinary playlist of these songs), a line saying the labels are estimates, and the
    songs (sorted as All Songs; the columns the user chose for song lists)."""

    def __init__(self, app):
        super().__init__(app)
        t = self.theme
        self.kind, self.value = "mood", ""
        top, self.title_l, self.sub = self.heading(self, "")
        top.pack(fill="x", padx=28, pady=(22, 4))
        self.create_b = PillButton(top, t, tr("Create Playlist"), icon="plus", kind="plain",
                                   command=self.create_playlist)
        self.create_b.pack(side="right", padx=(0, 10))
        self.note = label(self, t, tr(moods.ESTIMATE_NOTE), "small", fg="text2", wraplength=700, justify="left")
        self.note.pack(fill="x", padx=28)
        self.made = label(self, t, "", "small", fg="accent")
        self.made.pack(fill="x", padx=28, pady=(2, 6))
        self.table = song_table(self, self, kind="songs",
                                empty_text=tr("No song has this label yet. Songs get their labels while they are "
                                              "analysed."),
                                on_activate=lambda i: app.play_list(self.table.rows, i, self.list_name))
        self.table.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.bind("<Configure>", lambda e: self.note.configure(wraplength=max(200, e.width - 56)))

    def show(self, kind: Optional[str] = None, label: Optional[str] = None, **kw) -> None:
        if kind is not None and label is not None and (kind, label) != (self.kind, self.value):
            self.kind, self.value = kind, label
            self.made.configure(text="")
        self.refresh()

    def refresh(self) -> None:
        rows = self.app.mood_tracks(self.kind, self.value)
        self.title_l.configure(text=tr(self.value))
        self.list_name = tr(self.value)
        minutes = int(round(sum(r.duration for r in rows) / 60))
        self.sub.configure(text=tr("{a}, {b}", a=plural(len(rows), "song"), b=plural(minutes, "minute")))
        self.create_b.set(enabled=bool(rows))
        self.table.set_rows(rows, keep=True)

    def create_playlist(self) -> Optional[str]:
        return self.app.create_mood_playlist(self.kind, self.value)

    def songs(self) -> List[Track]:
        return list(self.table.rows)

    def likes_changed(self) -> None:
        self.table.redraw()

    def song_changed(self) -> None:
        self.table.redraw()
