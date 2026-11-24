"""The MusicBrainz window of the 2.1 player: "Get info from MusicBrainz…" for a song
and "Get album info from MusicBrainz…" for an album (docs/player-spec.md, "Song
information from MusicBrainz").

It reads the songs' tags in a thread, searches MusicBrainz for the album (or the
song) with what the tags say, lists the releases found, reads the chosen
release's track list, matches the songs to its tracks (musicbrainz.match_tracks)
and shows, song by song, what would change.  The fields to write can be chosen,
and "Only fill in empty fields" leaves every filled tag alone.  Save writes each
song's own changes with tagedit.save_tags() (only the tag, never the audio) and
refreshes the library.  Every request goes through one MusicBrainzClient, which
keeps to MusicBrainz's one request per second together with the automatic
lookup.  Results reach Tk through `self.q`.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Dict, List, Optional

from . import musicbrainz as mb
from . import tagedit
from .i18n import N_, tr, tr_name, tr_text
from .player_pages import frame, label
from .player_theme import PillButton
from .player_windows import _window, close_window

if TYPE_CHECKING:  # pragma: no cover
    from .player_gui import PlayerApp

EMPTY = N_("(empty)")


def release_facts(r: mb.Release) -> List[str]:
    """The grey line under a release in the list: date, country, formats, tracks, and
    the status when it is not an official release."""
    parts = [x for x in (r.date, r.country, r.formats) if x]
    if r.track_count:
        parts.append(tr("1 track") if r.track_count == 1 else tr("{n} tracks", n=r.track_count))
    if r.status and r.status != "Official":
        parts.append(tr(r.status))
    return parts


def release_text(r: mb.Release) -> str:
    """One line of the release list."""
    head = tr("{title} — {artist}", title=r.title, artist=r.artist) if r.artist else r.title
    facts = release_facts(r)
    return f"{head}  ·  {' · '.join(facts)}" if facts else head


def preview_lines(songs: List[mb.Song], detail: mb.ReleaseDetail, matched, fields, only_empty: bool):
    """[(text, grey)] lines of the changes preview, and the changes of each song."""
    lines = []
    plans = []
    for s, t in zip(songs, matched):
        name = os.path.basename(s.path)
        if t is None:
            lines.append((tr("{file}: no matching track on this release, left as it is", file=name), True))
            plans.append((s.path, {}))
            continue
        num = f"{t.disc}-{t.number}" if t.disc_count > 1 else str(t.number)
        lines.append((tr("{file} → {number}. {title}", file=name, number=num, title=t.title), False))
        ch = mb.changes_for(s, detail, t, fields, only_empty)
        plans.append((s.path, ch))
        if not ch:
            lines.append(("    " + tr("Nothing to change"), True))
        for f, new in ch.items():
            old = s.tag(f) or tr(EMPTY)
            lines.append(("    " + tr("{field}: {old} → {new}", field=tr(tagedit.LABELS[f]), old=old, new=new), True))
    return lines, plans


class MusicBrainzWindow:
    """Search MusicBrainz, pick a release, see the changes, save them."""

    def __init__(self, app: "PlayerApp", tracks: list, album: bool = False, heading: str = "",
                 subheading: str = ""):
        from .settings import Settings

        self.app, t = app, app.theme
        self.paths: List[str] = [x.path for x in tracks]
        self.album = album or len(self.paths) > 1
        self.cfg = Settings.from_dict(app.cfg.to_dict())
        self.q: "queue.Queue" = queue.Queue()
        self.stop = threading.Event()
        self.client = mb.MusicBrainzClient(self.cfg.musicbrainz_server, stop=self.stop)
        self.songs: List[mb.Song] = []
        self.releases: List[mb.Release] = []
        self.details: Dict[str, Optional[mb.ReleaseDetail]] = {}
        self.detail: Optional[mb.ReleaseDetail] = None
        self.matched: list = []
        self.plans: list = []
        self.busy = self.saving = self.done = False
        self.outcomes: list = []
        self.win = _window(app, tr("MusicBrainz"))
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        self.win.bind("<<Close>>", lambda e: self.cancel())
        box = frame(self.win, t)
        box.pack(fill="both", expand=True, padx=24, pady=18)
        if not heading and len(tracks) == 1:
            heading = tracks[0].display_title
            subheading = " · ".join(x for x in (tracks[0].artist, tracks[0].album) if x)
        label(box, t, tr_name(heading) if heading else tr("{n} songs", n=f"{len(tracks):,}"), "h2",
              wraplength=640, justify="left").pack(anchor="w")
        if subheading:
            label(box, t, subheading, "body", fg="text2", wraplength=640, justify="left").pack(anchor="w")
        row = frame(box, t)
        row.pack(fill="x", pady=(12, 0))
        label(row, t, tr("Album") if self.album else tr("Title"), "body", fg="text2").pack(side="left")
        self.v_title = tk.StringVar()
        self.e_title = ttk.Entry(row, textvariable=self.v_title, width=28, style="P.TEntry", font=t.f("body"))
        self.e_title.pack(side="left", padx=(8, 16), fill="x", expand=True)
        label(row, t, tr("Artist"), "body", fg="text2").pack(side="left")
        self.v_artist = tk.StringVar()
        self.e_artist = ttk.Entry(row, textvariable=self.v_artist, width=22, style="P.TEntry", font=t.f("body"))
        self.e_artist.pack(side="left", padx=(8, 12), fill="x", expand=True)
        for e in (self.e_title, self.e_artist):
            e.bind("<Return>", lambda ev: self.search())
            e.bind("<KP_Enter>", lambda ev: self.search())
        self.search_b = PillButton(row, t, tr("Search"), command=self.search)
        self.search_b.pack(side="left")
        n = len(self.paths)
        self.status = label(box, t, tr("Reading the information of 1 song…") if n == 1 else
                            tr("Reading the information of {n} songs…", n=f"{n:,}"), "small", fg="text2")
        self.status.pack(anchor="w", pady=(6, 0))
        label(box, t, tr("Releases found"), "body", fg="text2").pack(anchor="w", pady=(10, 2))
        self.list = tk.Listbox(box, height=6, activestyle="none", exportselection=False, bd=0,
                               highlightthickness=1, font=t.f("body"))
        t.reg(self.list, bg="bg", fg="text", selectbackground="accent", selectforeground="on_accent",
              highlightbackground="sep", highlightcolor="sep")
        self.list.pack(fill="x")
        self.list.bind("<<ListboxSelect>>", lambda e: self._chosen())
        label(box, t, tr("Changes"), "body", fg="text2").pack(anchor="w", pady=(10, 2))
        self.text = tk.Text(box, height=12, wrap="word", bd=0, highlightthickness=1, font=t.f("body"))
        t.reg(self.text, bg="bg", fg="text", highlightbackground="sep", highlightcolor="sep",
              insertbackground="text")
        self.text.tag_configure("grey", foreground=t.c("text2"))
        t.on_change(lambda: self.text.tag_configure("grey", foreground=t.c("text2")), self.text)
        self.text.configure(state="disabled")
        self.text.pack(fill="both", expand=True)
        opts = frame(box, t)
        opts.pack(fill="x", pady=(8, 0))
        self.field_vars: Dict[str, tk.BooleanVar] = {}
        for i, f in enumerate(tagedit.FIELDS):
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(opts, text=tr(tagedit.LABELS[f]), variable=v, style="P.TCheckbutton",
                            command=self._update_preview).grid(row=i // 5, column=i % 5, sticky="w", padx=(0, 12))
            self.field_vars[f] = v
        self.v_empty = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text=tr("Only fill in empty fields"), variable=self.v_empty, style="P.TCheckbutton",
                        command=self._update_preview).pack(anchor="w", pady=(6, 0))
        self.error = label(box, t, "", "small", fg="error", wraplength=640, justify="left")
        self.error.pack(anchor="w", pady=(8, 0))
        self.summary = label(box, t, "", "body", wraplength=640, justify="left")
        self.summary.pack(anchor="w")
        self.btns = frame(box, t)
        self.btns.pack(fill="x", pady=(10, 0))
        self.save_b = PillButton(self.btns, t, tr("Save"), command=self.save)
        self.save_b.pack(side="right")
        self.cancel_b = PillButton(self.btns, t, tr("Cancel"), kind="plain", command=self.cancel)
        self.cancel_b.pack(side="right", padx=(0, 8))
        label(box, t, tr_text(mb.CREDIT), "small", fg="text2", wraplength=640, justify="left").pack(
            anchor="w", pady=(10, 0))
        self.win.minsize(680, 0)
        self._set_busy(True)
        threading.Thread(target=self._read, daemon=True, name="realhires-mb-read").start()
        self.win.after(100, self._poll)

    # ---- threads ------------------------------------------------------------
    def _read(self) -> None:
        songs = []
        for p in self.paths:
            try:
                songs.append(mb.read_song(p))
            except Exception:  # noqa: BLE001 (an unreadable file is searched without tags)
                songs.append(mb.Song(p, {}, 0.0))
        self.q.put(("songs", songs))

    def _search(self, title: str, artist: str) -> None:
        try:
            found = mb.search_candidates(self.client, self.songs, title, artist, self.album)
            self.q.put(("found", found))
        except mb.MusicBrainzStopped:
            pass
        except mb.MusicBrainzError as exc:
            self.q.put(("error", str(exc)))

    def _lookup(self, rid: str) -> None:
        try:
            self.q.put(("detail", rid, self.client.release(rid)))
        except mb.MusicBrainzStopped:
            pass
        except mb.MusicBrainzError as exc:
            self.q.put(("error", str(exc)))

    def _write(self, plans) -> None:
        out = mb.save_changes(plans, self.cfg, progress=lambda i, n, p: self.q.put(("progress", i, n)))
        try:
            log = mb.LookupLog()
            for o in out:
                if o.ok:
                    log.mark(o.path, mb.SAVED, self.detail.release.id if self.detail else "")
            log.close()
        except Exception:  # noqa: BLE001 (the log only saves asking again)
            pass
        self.q.put(("saved", out))

    def _poll(self) -> None:
        try:
            if not self.win.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "songs":
                    self._songs(rest[0])
                elif kind == "found":
                    self._found(rest[0])
                elif kind == "detail":
                    self._detail(rest[0], rest[1])
                elif kind == "error":
                    self._set_busy(False)
                    self.status.configure(text="")
                    self.error.configure(text=tr_text(rest[0]))
                elif kind == "progress":
                    i, n = rest
                    if i < n:
                        self.status.configure(text=tr("Saving {i} of {n}…", i=f"{i + 1:,}", n=f"{n:,}"))
                elif kind == "saved":
                    self._saved(rest[0])
                    return
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    # ---- the window ---------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = ["disabled"] if busy or self.saving else ["!disabled"]
        for e in (self.e_title, self.e_artist):
            e.state(state)
        ready = not busy and not self.saving and any(ch for _, ch in self.plans)
        self.save_b.set(enabled=ready)

    def _songs(self, songs: List[mb.Song]) -> None:
        self.songs = songs
        if self.album:
            album, artist = mb.album_of(songs)
            if not album and songs:
                album = os.path.basename(os.path.dirname(songs[0].path))
        else:
            s = songs[0] if songs else mb.Song("")
            album = s.tag("title") or os.path.splitext(os.path.basename(s.path))[0]
            artist = s.tag("artist") or s.tag("album_artist")
        self.v_title.set(album)
        self.v_artist.set(artist)
        self.search()

    def search(self) -> None:
        if self.saving or self.done:
            return
        title = tagedit.clean_value(self.v_title.get())
        if not title:
            self.error.configure(text=tr("Type the album to search for.") if self.album else
                                 tr("Type the title to search for."))
            return
        self.error.configure(text="")
        self.status.configure(text=tr("Searching MusicBrainz…"))
        self._set_busy(True)
        threading.Thread(target=self._search, args=(title, tagedit.clean_value(self.v_artist.get())),
                         daemon=True, name="realhires-mb-search").start()

    def _found(self, releases: List[mb.Release]) -> None:
        self.releases = releases
        self.list.delete(0, "end")
        for r in releases:
            self.list.insert("end", release_text(r))
        if not releases:
            self._set_busy(False)
            self.status.configure(text=tr("Nothing found on MusicBrainz. Check the spelling, or search with fewer "
                                          "words."))
            self._show([], [])
            return
        self.list.selection_clear(0, "end")
        self.list.selection_set(0)
        self._chosen()

    def _chosen(self) -> None:
        sel = self.list.curselection()
        if not sel or self.saving or self.done:
            return
        r = self.releases[sel[0]]
        if r.id in self.details:
            self._detail(r.id, self.details[r.id])
            return
        self.status.configure(text=tr("Reading the release…"))
        self._set_busy(True)
        threading.Thread(target=self._lookup, args=(r.id,), daemon=True, name="realhires-mb-release").start()

    def _detail(self, rid: str, detail: Optional[mb.ReleaseDetail]) -> None:
        self.details[rid] = detail
        sel = self.list.curselection()
        if not sel or self.releases[sel[0]].id != rid:
            return  # another release was chosen meanwhile
        self._set_busy(False)
        self.detail = detail
        if detail is None:
            self.status.configure(text=tr("This release could not be read."))
            self._show([], [])
            return
        self.matched = mb.match_tracks(self.songs, detail)
        found = sum(1 for m in self.matched if m is not None)
        self.status.configure(text=tr("{found} of {n} songs found on this release.", found=found,
                                      n=len(self.songs)))
        self._update_preview()

    def fields(self) -> List[str]:
        return [f for f in tagedit.FIELDS if self.field_vars[f].get()]

    def _update_preview(self) -> None:
        if self.detail is None:
            return
        lines, self.plans = preview_lines(self.songs, self.detail, self.matched, self.fields(), self.v_empty.get())
        self._show(lines, self.plans)

    def _show(self, lines, plans) -> None:
        self.plans = plans
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for text, grey in lines:
            self.text.insert("end", text + "\n", ("grey",) if grey else ())
        self.text.configure(state="disabled")
        self._set_busy(self.busy)

    def changes(self) -> list:
        """[(path, changes)] of the songs that would change."""
        return [(p, ch) for p, ch in self.plans if ch]

    def save(self) -> None:
        if self.busy or self.saving or self.done:
            return
        plans = self.changes()
        if not plans:
            self.error.configure(text=tr("Nothing to save: the songs already hold this information."))
            return
        try:
            for _, ch in plans:
                tagedit.check_changes(ch)
        except tagedit.TagError as exc:
            self.error.configure(text=tr_text(str(exc)))
            return
        self.error.configure(text="")
        self.saving = True
        self._set_busy(True)
        self.save_b.pack_forget()
        self.cancel_b.pack_forget()
        self.status.configure(text=tr("Saving {i} of {n}…", i="1", n=f"{len(plans):,}") if len(plans) > 1
                              else tr("Saving…"))
        threading.Thread(target=self._write, args=(plans,), daemon=True, name="realhires-mb-save").start()

    def _saved(self, outcomes: list) -> None:
        self.saving, self.done = False, True
        self.outcomes = outcomes
        self.app.tags_saved([o.path for o in outcomes if o.ok])
        head, lines = tagedit.save_summary(outcomes)
        if all(o.ok for o in outcomes) and not lines:
            self.close()
            return
        self.status.configure(text="")
        self.summary.configure(text="\n\n".join([tr_text(head)] + [tr_text(x) for x in lines]))
        PillButton(self.btns, self.app.theme, tr("Close"), command=self.close).pack(side="right")

    def cancel(self) -> None:
        if not self.saving:
            self.close()

    def close(self) -> None:
        if self.saving:
            return
        self.stop.set()
        if getattr(self.app, "mb_window", None) is self:
            self.app.mb_window = None
        close_window(self.win)


__all__ = ["MusicBrainzWindow", "release_facts", "release_text", "preview_lines"]
