"""Desktop GUI (Tkinter, part of the Python standard library).

* Pick folders or files and scan them in parallel.
* Scroll through the results.  By default only problem files are listed:
  FAKE LOSSLESS ("fake FLAC", made from MP3/AAC), FAKE HI-RES (upsampled or
  bit-padded) and SUSPICIOUS.  The filter checkboxes show the rest.
* The dashboard below the list shows the selected file's verdict, a one-line
  reason, key facts, its spectrogram and average spectrum, and buttons to
  open the file or its folder.
* After every scan a summary window shows what percentage of files got each
  verdict.
* "Download synced lyrics" saves LRCLIB lyrics as .lrc files next to the songs
  (as LRCGET does); the Lyrics tab of the dashboard shows them.
* Settings (detection thresholds, scanning, display) are edited in the
  Settings window and saved for the next start (also used by the CLI).

Run with ``realhires-gui`` (installed), ``python -m realhires.gui``, or the
``RealHiRes`` executable from a release package.
"""

from __future__ import annotations

import multiprocessing
import os
import pickle
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError as exc:  # e.g. Debian/Ubuntu Python without python3-tk
    raise SystemExit(
        "The Real-Hi-Res GUI needs Tkinter, which this Python does not have.\n"
        "Install it (Debian/Ubuntu: sudo apt install python3-tk; Fedora: sudo dnf install "
        "python3-tkinter; Homebrew: brew install python-tk), use a release package, or use "
        "the command-line tool `realhires` instead.\n"
        f"(Details: {exc})"
    ) from exc

from . import APP_NAME, PROJECT_URL, __version__, i18n
from .i18n import N_, tr, tr_name, tr_text
from . import settings as settings_mod
from .eta import ScanEta, file_sizes, fmt_about
from .fix import FIXABLE_VERDICTS, fix_advice, fix_outlook
from . import lyrics as lyrics_mod
from .listing import list_source
from .pool import MultiProcessPool, effective_jobs
from .probe import ToolMissingError, external_env, require_tools
from .report import write_csv, write_json
from .store import StoreError, open_store, settings_key
from .store import default_path as store_default_path
from .verdict import (ERROR, FAKE, FAKE_LOSSLESS, LOSSY, NOT_HIRES, PROBLEM_VERDICTS, REAL, SUSPECT,
                      VERDICT_ORDER, FileResult, analyze_file, saved_note, verdict_breakdown)

# The labels are English (tests and other code read them); the GUI shows them through tr().
VERDICT_STYLE = {
    FAKE_LOSSLESS: (N_("Fake lossless"), "#a3122a", N_("Lossless file (FLAC/WAV/ALAC...) made from an MP3/AAC")),
    FAKE: (N_("Fake hi-res"), "#d32f2f", N_("Upsampled and/or 16-bit padded")),
    SUSPECT: (N_("Suspicious"), "#b26a00", N_("Could not be confirmed either way")),
    REAL: (N_("Real hi-res"), "#2e7d32", N_("Genuine high resolution")),
    NOT_HIRES: (N_("CD quality"), "#1565c0", N_("Genuine lossless, not hi-res")),
    LOSSY: (N_("Lossy format"), "#6a1b9a", N_("MP3/AAC/... file")),
    ERROR: (N_("Error"), "#616161", N_("Could not be analysed")),
}
PENDING = "PENDING"  # GUI-only state of a listed file that has not been scanned yet
PENDING_STYLE = (N_("Not scanned"), "#808080")
RUNNING = "RUNNING"  # GUI-only: being analysed right now
RUNNING_STYLE = (N_("Analysing…"), "#0b62c4")
SEARCH_DELAY_MS = 200  # the list is filtered once typing pauses, not on every key
# The fixed rows of the dashboard's facts (keys of App.fact_vars; shown through tr()).
FACTS = (N_("Claimed"), N_("Real (estimated)"), N_("Bandwidth"), N_("Bits used"), N_("Format"),
         N_("Duration"), N_("After fix"))


def verdict_label(verdict: str) -> str:
    """A verdict as the GUI names it ("CD quality" for NOT HI-RES), in the language in use."""
    return tr(VERDICT_STYLE[verdict][0]) if verdict in VERDICT_STYLE else tr(verdict)


def text_width(text: str) -> int:
    """The width of a text in average characters (CJK characters count twice), for widget widths."""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def choice_box(parent, var: tk.StringVar, choices, width: Optional[int] = None,
               translate: bool = True) -> ttk.Combobox:
    """A read-only combobox that shows `choices` translated while `var` keeps holding the
    English choice, as the settings store it (the code compares and saves `var`)."""
    choices = list(choices)
    shown = [tr(c) for c in choices] if translate else list(choices)

    def display(value: str) -> str:
        return shown[choices.index(value)] if value in choices else value

    disp = tk.StringVar(master=parent, value=display(var.get()))
    longest = max((text_width(s) for s in shown), default=0) + 2  # long choices must not be cut off
    width = max(18 if width is None else width, longest)
    box = ttk.Combobox(parent, textvariable=disp, values=shown, state="readonly", width=width)

    def picked(*_):
        d = disp.get()
        value = choices[shown.index(d)] if d in shown else d
        if var.get() != value:
            var.set(value)

    def follow(*_):
        d = display(var.get())
        if disp.get() != d:
            disp.set(d)

    disp.trace_add("write", picked)
    var.trace_add("write", follow)
    box.display_var = disp  # keeps the Tk variable alive as long as the box
    return box


def group_names(g, mode: str):
    """An album's or artist's names as the cards and the chip show them: real names stay as
    they are; the placeholders for missing tags and the artist view's "N albums" are translated."""
    return tr_name(g.title), (tr_name(g.subtitle) if mode == "album" else tr_text(g.subtitle))


def _work(path: str, max_seconds: Optional[float], cfg) -> FileResult:
    """Runs in a worker process.  Must be module-level so it can be pickled."""
    return analyze_file(path, max_seconds=max_seconds, keep_plot=True, settings=cfg)


# ---------------------------------------------------------------- helpers ---
def reveal_in_file_manager(path: str) -> None:
    env = external_env()  # do not leak the frozen app's library path
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)], env=env)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path], env=env)
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) or "."], env=env)


def open_with_default_app(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path], env=external_env())
    else:
        subprocess.Popen(["xdg-open", path], env=external_env())


def fmt_duration(sec: Optional[float]) -> str:
    if sec is None:
        return "?"
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Scanner(threading.Thread):
    """Background thread: posts the saved results of files that have not
    changed, fans the other files out to worker processes, saves their results
    and posts ("total"|"result"|"done"|"error", payload) messages to a queue.
    `force` analyses every file again, ignoring saved results."""

    def __init__(self, files: List[str], cfg, max_seconds: Optional[float], out: queue.Queue,
                 force: bool = False):
        super().__init__(daemon=True)
        self.files, self.cfg, self.max_seconds, self.out = files, cfg, max_seconds, out
        self.force = force
        self.stop_event = threading.Event()

    def run(self) -> None:
        store = None
        try:
            files = list(self.files)
            self.out.put(("total", len(files)))
            store = open_store(self.cfg)
            skey = settings_key(self.cfg)
            todo = files
            if store is not None and not self.force:
                todo = []
                for f in files:
                    if self.stop_event.is_set():
                        break
                    r = store.lookup(f, skey, self.max_seconds)
                    if r is None or (r.saved["outdated"] and self.cfg.rescan_outdated):
                        todo.append(f)
                    else:
                        self.out.put(("result", r))
            if not todo or self.stop_event.is_set():
                self.out.put(("done", "stopped" if self.stop_event.is_set() else None))
                return
            self.out.put(("analysing", todo, file_sizes(todo)))
            ctx = multiprocessing.get_context("spawn")  # safe with a GUI thread running
            workers = min(effective_jobs(self.cfg.jobs), len(todo))
            with MultiProcessPool(workers, mp_context=ctx) as ex:
                futures = {ex.submit(_work, f, self.max_seconds, self.cfg): f for f in todo}
                for fut in as_completed(futures):
                    if self.stop_event.is_set():
                        for other in futures:
                            other.cancel()
                        break
                    try:
                        r = fut.result()
                    except Exception as exc:  # noqa: BLE001 - e.g. a crashed worker
                        r = FileResult(path=os.path.abspath(futures[fut]), verdict=ERROR, reasons=[str(exc)],
                                       summary=f"Could not analyse: {exc}")
                    if store is not None:
                        store.put(r, skey, self.max_seconds, plot=r.plot)
                    self.out.put(("result", r))
            self.out.put(("done", "stopped" if self.stop_event.is_set() else None))
        except Exception as exc:  # noqa: BLE001
            self.out.put(("error", str(exc)))
        finally:
            if store is not None:
                store.close()


def lookup_saved(requests: queue.Queue, out: queue.Queue) -> None:
    """Background thread: looks up saved results for files as soon as they are
    listed, so files checked before show their verdict without a scan.
    Requests are (generation, cfg, wanted_seconds, paths); answers are
    ("saved", generation, [FileResult]) and ("saved_done", generation, None)."""
    store, store_key = None, None
    while True:
        gen, cfg, wanted, paths = requests.get()
        if paths is None:
            break
        key = (getattr(cfg, "save_results", True), getattr(cfg, "saved_graphs_mb", 0))
        if store is None or key != store_key:
            if store is not None:
                store.close()
            store, store_key = open_store(cfg), key
        found = []
        if store is not None:
            skey = settings_key(cfg)
            for path in paths:
                r = store.lookup(path, skey, wanted)
                if r is not None:
                    found.append(r)
                if len(found) >= 200:
                    out.put(("saved", gen, found))
                    found = []
        if found:
            out.put(("saved", gen, found))
        out.put(("saved_done", gen, None))
    if store is not None:
        store.close()


class PlotCache:
    """Keeps each file's spectrum + spectrogram on disk instead of in memory,
    so libraries with thousands of files do not fill the RAM."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="realhires-")
        self.index: Dict[str, str] = {}

    def put(self, path: str, plot: Optional[dict]) -> None:
        if not plot:
            return
        fn = os.path.join(self.dir, f"{len(self.index)}.pkl")
        with open(fn, "wb") as fh:
            pickle.dump(plot, fh, protocol=pickle.HIGHEST_PROTOCOL)
        self.index[path] = fn

    def get(self, path: str) -> Optional[dict]:
        fn = self.index.get(path)
        if not fn:
            return None
        try:
            with open(fn, "rb") as fh:
                return pickle.load(fh)  # our own data, written above
        except (OSError, pickle.PickleError, EOFError):
            return None

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir = tempfile.mkdtemp(prefix="realhires-")
        self.index.clear()

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


# ------------------------------------------------------------------ plots ---
class _PlotBase(tk.Canvas):
    L, R, T, B = 52, 12, 24, 28

    def __init__(self, master, **kw):
        super().__init__(master, background="#ffffff", highlightthickness=0, **kw)
        self.result: Optional[FileResult] = None
        self.plot: Optional[dict] = None
        self.waiting = False  # graphs of a saved result are being drawn
        self.bind("<Configure>", lambda e: self.redraw())

    def show(self, result: Optional[FileResult], plot: Optional[dict], waiting: bool = False) -> None:
        self.result, self.plot, self.waiting = result, plot, waiting and not plot
        self.redraw()

    def _message(self, text: str) -> None:
        self.create_text(self.winfo_width() / 2, self.winfo_height() / 2, text=text, fill="#888")

    def _freq_axis(self, nyq: float, x0: float, y_of) -> None:
        kstep = 2 if nyq <= 24000 else 5 if nyq <= 50000 else 10 if nyq <= 100000 else 20
        k = 0
        while k * 1000 <= nyq:
            y = y_of(k * 1000)
            self.create_line(x0 - 4, y, x0, y, fill="#666")
            self.create_text(x0 - 6, y, text=f"{k}k", anchor="e", fill="#555", font=("TkDefaultFont", 8))
            k += kstep


class SpectrogramCanvas(_PlotBase):
    """Time-frequency picture: an upsampled or lossy-sourced file shows a hard
    black area above its cut-off for the whole duration of the track."""

    def __init__(self, master, app: "App", **kw):
        super().__init__(master, **kw)
        self.app = app
        self.configure(background="#1e1e1e")
        self._img = None

    def redraw(self) -> None:
        from .imaging import spectrogram_rgb, to_ppm

        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 80 or h < 60:
            return
        sg = (self.plot or {}).get("spectrogram") if self.plot else None
        if self.result is None:
            self.create_text(w / 2, h / 2, text=tr("Select a scanned file to see its spectrogram"), fill="#aaa")
            return
        if not sg:
            self.create_text(w / 2, h / 2, fill="#aaa",
                             text=tr("Drawing the spectrogram…") if self.waiting else
                             tr("No spectrogram (silent or unreadable file)"))
            return
        L, R, T, B = self.L, self.R, self.T, self.B
        pw, ph = w - L - R, h - T - B
        cfg = self.app.cfg
        rgb = spectrogram_rgb(sg, pw, ph, cfg.spectrogram_range_db, cfg.colormap)
        self._img = tk.PhotoImage(data=to_ppm(rgb), format="PPM")
        self.create_image(L, T, image=self._img, anchor="nw")
        nyq = sg["nyquist"]

        def Y(f):
            return T + ph - ph * f / nyq

        self._freq_axis(nyq, L, Y)
        for c in self.find_withtag("all"):
            if self.type(c) == "text":
                self.itemconfigure(c, fill="#ccc")
        total = sg["shape"][0] * sg["seconds_per_column"]
        for frac in (0, 0.25, 0.5, 0.75, 1.0):
            x = L + frac * pw
            self.create_line(x, T + ph, x, T + ph + 4, fill="#aaa")
            self.create_text(x, T + ph + 13, text=fmt_duration(frac * total),
                             fill="#ccc", font=("TkDefaultFont", 8))
        r = self.result
        marks = []
        if r.upsampled_from:
            marks.append((r.upsampled_from / 2.0, "#4fc3f7",
                          tr("{khz} kHz: limit of a {rate} kHz master", khz=f"{r.upsampled_from / 2000:g}",
                             rate=f"{r.upsampled_from / 1000:g}")))
        if r.lossy_cutoff_hz:
            marks.append((r.lossy_cutoff_hz, "#ff6e6e",
                          tr("{khz} kHz: lossy encoder cut-off", khz=f"{r.lossy_cutoff_hz / 1000:.1f}")))
        for hz, color, label in marks:
            if 0 < hz < nyq:
                y = Y(hz)
                self.create_line(L, y, L + pw, y, fill=color, dash=(6, 4), width=2)
                self.create_text(L + pw - 4, y - 8, text=label, anchor="e", fill=color,
                                 font=("TkDefaultFont", 9, "bold"))
        self.create_text(L, 12, anchor="w", fill="#eee", font=("TkDefaultFont", 9, "bold"),
                         text=tr("Spectrogram (time →, frequency ↑)"))


class SpectrumCanvas(_PlotBase):
    """Average spectrum over the whole file."""

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 80 or h < 60:
            return
        p = self.plot
        if self.result is None or not p or not p.get("freqs"):
            self._message(tr("Select a scanned file to see its spectrum") if self.result is None
                          else tr("Drawing the spectrum…") if self.waiting else tr("No spectrum available"))
            return
        L, R, T, B = self.L, self.R, self.T, self.B
        pw, ph = w - L - R, h - T - B
        nyq = p["nyquist"]
        top = (int(p["peak_db"] + 5) // 10 + 1) * 10
        bottom = max((int(min(p["floor_db"], p["threshold_db"]) - 15) // 10) * 10, top - 200)

        def X(f):
            return L + pw * f / nyq

        def Y(v):
            v = min(max(v, bottom), top)
            return T + ph * (top - v) / (top - bottom)

        step = 10 if top - bottom <= 120 else 20
        lvl = top
        while lvl >= bottom:
            self.create_line(L, Y(lvl), L + pw, Y(lvl), fill="#eeeeee")
            self.create_text(L - 6, Y(lvl), text=f"{lvl}", anchor="e", fill="#666", font=("TkDefaultFont", 8))
            lvl -= step
        kstep = 2 if nyq <= 24000 else 5 if nyq <= 50000 else 10 if nyq <= 100000 else 20
        k = 0
        while k * 1000 <= nyq:
            x = X(k * 1000)
            self.create_line(x, T, x, T + ph, fill="#eeeeee")
            self.create_text(x, T + ph + 12, text=f"{k}k", fill="#666", font=("TkDefaultFont", 8))
            k += kstep
        for i, ref in enumerate((22050, 24000, 44100, 48000)):
            if ref < nyq * 0.9:
                self.create_line(X(ref), T, X(ref), T + ph, fill="#aaaaaa", dash=(2, 3))
                self.create_text(X(ref) + 3, T + ph - 8 - 12 * (i % 2), text=f"{ref / 1000:g}k",
                                 anchor="w", fill="#999", font=("TkDefaultFont", 8))
        color = VERDICT_STYLE.get(self.result.verdict, ("", "#1565c0"))[1]
        if p.get("bandwidth_hz"):
            x = X(p["bandwidth_hz"])
            self.create_line(x, T, x, T + ph, fill=color, dash=(6, 4), width=2)
            anchor, dx = ("e", -4) if x > L + pw - 130 else ("w", 4)
            self.create_text(x + dx, T + 10, text=tr("bandwidth {khz} kHz", khz=f"{p['bandwidth_hz'] / 1000:.1f}"),
                             anchor=anchor, fill=color)
        self.create_line(L, Y(p["floor_db"]), L + pw, Y(p["floor_db"]), fill="#8250df", dash=(4, 4))
        pts = []
        for f, v in zip(p["freqs"], p["db"]):
            pts += [X(f), Y(v)]
        if len(pts) >= 4:
            self.create_line(*pts, fill="#1f2328", width=1)
        self.create_rectangle(L, T, L + pw, T + ph, outline="#999")
        self.create_text(L, 12, anchor="w", font=("TkDefaultFont", 9, "bold"), fill="#333",
                         text=tr("Average spectrum (dB per bin)"))


# ---------------------------------------------------------------- dialogs ---
class SummaryDialog(tk.Toplevel):
    """Pop-up shown after a scan: a donut (pie) chart of the verdicts with a
    legend of percentages.  Clicking a slice or a legend row lists only those
    files."""

    SIZE = 260

    def __init__(self, app: "App", results: List[FileResult], elapsed: Optional[float]):
        super().__init__(app.root)
        self.app = app
        self.title(tr("Scan summary"))
        self.transient(app.root)
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=16)
        frm.grid(sticky="nsew")
        n = len(results)
        problems = sum(1 for r in results if r.verdict in PROBLEM_VERDICTS)
        if elapsed:
            secs = f"{elapsed:.1f}"
            head = (tr("Scanned 1 file in {seconds} s", seconds=secs) if n == 1
                    else tr("Scanned {n} files in {seconds} s", n=n, seconds=secs))
        else:
            head = tr("Scanned 1 file") if n == 1 else tr("Scanned {n} files", n=n)
        ttk.Label(frm, text=head, font=("TkDefaultFont", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        pct_problems = 100.0 * problems / n if n else 0.0
        pct = f"{pct_problems:.1f}"
        ttk.Label(frm, text=(tr("1 file ({pct} %) is fake or suspicious.", pct=pct) if problems == 1 else
                             tr("{n} files ({pct} %) are fake or suspicious.", n=problems, pct=pct))
                  if problems else tr("No fake or suspicious files found."),
                  foreground="#a3122a" if problems else "#2e7d32").grid(row=1, column=0, columnspan=2,
                                                                         sticky="w", pady=(2, 10))
        breakdown = verdict_breakdown(results)
        self.canvas = tk.Canvas(frm, width=self.SIZE, height=self.SIZE, highlightthickness=0,
                                background=self._bg())
        self.canvas.grid(row=2, column=0, sticky="n", padx=(0, 18))
        self._draw_pie(breakdown, n, pct_problems)

        legend = ttk.Frame(frm)
        legend.grid(row=2, column=1, sticky="nw")
        for i, (v, count, pct) in enumerate(breakdown):
            label, color, desc = VERDICT_STYLE[v]
            label, desc = tr(label), tr(desc)
            sw = tk.Canvas(legend, width=14, height=14, highlightthickness=0, background=self._bg())
            sw.create_rectangle(0, 0, 14, 14, fill=color, outline="")
            sw.grid(row=2 * i, column=0, padx=(0, 6), pady=(4, 0), sticky="w")
            name = ttk.Label(legend, text=label, foreground=color, font=("TkDefaultFont", 10, "bold"),
                             cursor="hand2")
            name.grid(row=2 * i, column=1, sticky="w", pady=(4, 0))
            ttk.Label(legend, text=f"{pct:5.1f} %", width=7, anchor="e",
                      font=("TkDefaultFont", 10, "bold")).grid(row=2 * i, column=2, sticky="e", pady=(4, 0))
            ttk.Label(legend, text=tr("1 file") if count == 1 else tr("{n} files", n=count), foreground="#555").grid(
                row=2 * i, column=3, sticky="w", padx=(8, 0), pady=(4, 0))
            ttk.Label(legend, text=desc, foreground="#777", font=("TkDefaultFont", 8)).grid(
                row=2 * i + 1, column=1, columnspan=3, sticky="w")
            for w in (sw, name):
                w.bind("<Button-1>", lambda e, v=v: self._close(only=v))
        ttk.Label(frm, text=tr("Click a slice or a name to list only those files."), foreground="#777",
                  font=("TkDefaultFont", 8)).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.again = tk.BooleanVar(value=app.cfg.show_summary_popup)
        ttk.Checkbutton(frm, text=tr("Show this summary after every scan"), variable=self.again).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 8))
        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text=tr("Show problem files"), command=lambda: self._close(problems_only=True)).pack(side="left")
        ttk.Button(btns, text=tr("Show all files"), command=lambda: self._close(show_all=True)).pack(side="left", padx=4)
        ttk.Button(btns, text=tr("Export CSV…"), command=lambda: app.export("csv", all_results=True)).pack(side="left")
        close = ttk.Button(btns, text=tr("Close"), command=self._close)
        close.pack(side="left", padx=(4, 0))
        close.focus_set()
        self.bind("<Escape>", lambda e: self._close())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_width()) // 2
        y = app.root.winfo_rooty() + (app.root.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _bg(self) -> str:
        try:
            return ttk.Style(self).lookup("TFrame", "background") or "#f0f0f0"
        except tk.TclError:
            return "#f0f0f0"

    def _draw_pie(self, breakdown, total: int, pct_problems: float) -> None:
        c, size, pad = self.canvas, self.SIZE, 8
        box = (pad, pad, size - pad, size - pad)
        start = 90.0  # 12 o'clock, clockwise like most pie charts
        for v, count, pct in breakdown:
            color = VERDICT_STYLE[v][1]
            extent = 360.0 * count / max(total, 1)
            tag = f"slice-{v}"
            if extent >= 359.99:
                c.create_oval(*box, fill=color, outline="white", width=2, tags=(tag,))
            else:
                c.create_arc(*box, start=start, extent=-extent, fill=color, outline="white", width=2,
                             style="pieslice", tags=(tag,))
            c.tag_bind(tag, "<Button-1>", lambda e, v=v: self._close(only=v))
            c.tag_bind(tag, "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: c.configure(cursor=""))
            if pct >= 7:  # label big slices with their percentage
                import math

                mid = math.radians(start - extent / 2)
                r = (size / 2 - pad) * 0.78
                c.create_text(size / 2 + r * math.cos(mid), size / 2 - r * math.sin(mid),
                              text=f"{pct:.0f}%", fill="white", font=("TkDefaultFont", 9, "bold"))
            start -= extent
        hole = size * 0.27  # donut hole with the headline number
        c.create_oval(size / 2 - hole, size / 2 - hole, size / 2 + hole, size / 2 + hole,
                      fill=self._bg(), outline="")
        c.create_text(size / 2, size / 2 - 12, text=f"{pct_problems:.0f}%",
                      font=("TkDefaultFont", 20, "bold"),
                      fill="#a3122a" if pct_problems else "#2e7d32")
        c.create_text(size / 2, size / 2 + 16, text=tr("fake or suspicious"), fill="#555",
                      font=("TkDefaultFont", 8))

    def _close(self, problems_only: bool = False, show_all: bool = False, only: Optional[str] = None) -> None:
        if self.again.get() != self.app.cfg.show_summary_popup:
            self.app.cfg.show_summary_popup = self.again.get()
            self.app.save_settings()
        if only:
            self.app.set_filters([only])
        elif problems_only:
            self.app.set_filters(PROBLEM_VERDICTS)
        elif show_all:
            self.app.set_filters(VERDICT_ORDER)
        self.destroy()


class SettingsDialog(tk.Toplevel):
    """Edit every field of settings.Settings, grouped in tabs."""

    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.title(tr("Settings"))
        self.transient(app.root)
        self.vars: Dict[str, object] = {}
        self.path_fields: set = set()
        outer = ttk.Frame(self, padding=12)
        outer.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        nb = ttk.Notebook(outer)
        nb.grid(row=0, column=0, sticky="nsew")
        self.help_var = tk.StringVar(value=tr("Point at a setting to see what it does."))
        self.language_note: Optional[ttk.Label] = None
        infos = settings_mod.Settings.field_info()
        for group in settings_mod.GROUPS:
            tab = ttk.Frame(nb, padding=10)
            tab.columnconfigure(1, weight=1)
            nb.add(tab, text=tr(group))
            row = 0
            if group == "Detection":
                ttk.Label(tab, text=tr("Changes apply to the next scan."), foreground="#666").grid(
                    row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))
                row += 1
            for info in [i for i in infos if i["group"] == group]:
                widget = self._widget(tab, info)
                lab = ttk.Label(tab, text=tr(info["label"]))
                lab.grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=3)
                widget.grid(row=row, column=1, sticky="w", pady=3)
                for wdg in (lab, widget):
                    wdg.bind("<Enter>", lambda e, h=tr_text(info["help"]): self.help_var.set(h))
                    wdg.bind("<FocusIn>", lambda e, h=tr_text(info["help"]): self.help_var.set(h), add="+")
                row += 1
                if info["name"] == "language":  # shown when another language is chosen
                    self.language_note = ttk.Label(tab, text="", foreground="#9a6700")
                    self.language_note.grid(row=row, column=1, sticky="w", pady=(0, 3))
                    self.vars["language"].trace_add("write", lambda *a: self._language_changed())
                    row += 1
            if group == "Saved results":
                st = app.store.stats() if app.store is not None else None
                where = (tr("{n} file(s), {mb} MB, in {path}", n=st["files"], mb=f"{st['disk_bytes'] / 1e6:.1f}",
                            path=st["path"]) if st
                         else tr("Saving is turned off. Results are kept in {path}", path=store_default_path()))
                ttk.Label(tab, text=where, foreground="#666", wraplength=520, justify="left").grid(
                    row=row, column=0, columnspan=2, sticky="w", pady=(12, 4))
                ttk.Button(tab, text=tr("Delete all saved results…"), command=app.forget_saved).grid(
                    row=row + 1, column=0, sticky="w")
        ttk.Label(outer, textvariable=self.help_var, wraplength=520, foreground="#444",
                  justify="left").grid(row=1, column=0, sticky="ew", pady=(8, 4))
        ttk.Label(outer, text=tr("Saved in {path}", path=settings_mod.default_path()), foreground="#888",
                  font=("TkDefaultFont", 8)).grid(row=2, column=0, sticky="w")
        btns = ttk.Frame(outer)
        btns.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text=tr("Restore defaults"), command=self._defaults).pack(side="left")
        ttk.Button(btns, text=tr("Save"), command=self._save).pack(side="right")
        ttk.Button(btns, text=tr("Cancel"), command=self.destroy).pack(side="right", padx=4)
        self.bind("<Escape>", lambda e: self.destroy())
        self._load(app.cfg)
        self.update_idletasks()
        self.minsize(self.winfo_width(), self.winfo_height())
        self.grab_set()

    def _widget(self, parent, info):
        kind, name = info.get("kind"), info["name"]
        if kind == "bool":
            v = tk.BooleanVar()
            self.vars[name] = v
            return ttk.Checkbutton(parent, variable=v)
        if kind == "choice":
            v = tk.StringVar()
            self.vars[name] = v
            # shown translated, saved in English; long choices must not be cut off.  The languages
            # are shown by their own names, and so are the colour maps.
            return choice_box(parent, v, info["choices"], translate=name not in ("language", "colormap"))
        if kind == "multi":
            frame = ttk.Frame(parent)
            sub = {}
            for i, choice in enumerate(info["choices"]):
                b = tk.BooleanVar()
                sub[choice] = b
                label = verdict_label(choice) if choice in VERDICT_STYLE else tr(choice)
                ttk.Checkbutton(frame, text=label, variable=b).grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 8))
            self.vars[name] = sub
            return frame
        v = tk.StringVar()
        self.vars[name] = v
        if kind == "paths":  # 2.0: folders, separated by os.pathsep (';' on Windows, ':' elsewhere)
            self.path_fields.add(name)
            return ttk.Entry(parent, textvariable=v, width=60)
        if kind == "str":
            return ttk.Entry(parent, textvariable=v, width=60)
        inc = 1 if kind == "int" or (info.get("max") or 0) > 50 else 0.1
        return ttk.Spinbox(parent, textvariable=v, from_=info.get("min") or 0, to=info.get("max") or 1e9,
                           increment=inc, width=10)

    def _load(self, cfg) -> None:
        for name, var in self.vars.items():
            value = getattr(cfg, name)
            if isinstance(var, dict):
                for choice, b in var.items():
                    b.set(choice in value)
            elif isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            elif name in self.path_fields:
                var.set(os.pathsep.join(value))
            else:
                var.set(f"{value:g}" if isinstance(value, float) else str(value))

    def _defaults(self) -> None:
        self._load(settings_mod.Settings())

    def _language_changed(self) -> None:
        """Another language is used only from the next start (the player's Settings page restarts)."""
        if self.language_note is None:
            return
        chosen = i18n.code_of(str(self.vars["language"].get()))
        self.language_note.configure(
            text=tr("The new language is used when the app starts again.") if chosen != i18n.language() else "")

    def _save(self) -> None:
        data, bad = {}, []
        infos = {i["name"]: i for i in settings_mod.Settings.field_info()}
        for name, var in self.vars.items():
            if isinstance(var, dict):
                data[name] = [c for c, b in var.items() if b.get()]
            elif name in self.path_fields:
                data[name] = [p.strip() for p in var.get().split(os.pathsep) if p.strip()]
            else:
                data[name] = var.get()
        new = settings_mod.Settings.from_dict(data)
        for name, raw in data.items():
            if isinstance(raw, str) and infos[name].get("kind") not in ("str", "choice", "paths"):
                try:
                    ok = float(raw) == float(getattr(new, name))
                except ValueError:
                    ok = False
                if not ok:
                    lo, hi = infos[name].get("min"), infos[name].get("max")
                    bad.append(tr("{setting}: '{value}' (allowed {low} – {high})", setting=tr(infos[name]["label"]),
                                  value=raw, low=f"{lo:g}", high=f"{hi:g}"))
        if bad:
            messagebox.showerror(tr("Invalid settings"), tr("Please fix:") + "\n\n" + "\n".join(bad), parent=self)
            return
        self.app.apply_settings(new)
        self.destroy()



class FixDialog(tk.Toplevel):
    """Convert fake files to the resolution they really have (realhires.fix)."""

    def __init__(self, app: "App", results: List[FileResult], autostart: bool = False):
        super().__init__(app.root)
        self.app = app
        self.title(tr("Fix to real resolution"))
        self.transient(app.root)
        self.results = results
        self.plans: list = []
        self.done_paths: Dict[str, str] = {}
        self.outcomes: list = []   # FixOutcome of every file of the last run
        self.skipped = 0           # fake files left alone on purpose
        self.hidden = False        # closed while fixing: finishes in the background
        self.done_popup: Optional["FixDoneDialog"] = None
        self.running = False
        self.q: queue.Queue = queue.Queue()
        cfg = app.cfg
        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(2, weight=1)
        self.head = ttk.Label(frm, font=("TkDefaultFont", 11, "bold"))
        self.head.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frm, foreground="#555", wraplength=760, justify="left",
                  text=tr("Each file is rewritten at the sample rate and bit depth it really contains, so "
                          "its properties show the actual values (e.g. a fake 24/96 upsampled from a CD "
                          "becomes a genuine 16/44.1 file). It keeps its name, tags and cover art, nothing "
                          "audible is lost and it gets smaller. (Only editing the numbers in the file "
                          "would make it play at the wrong speed, so the audio itself is converted.)")).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 8))
        cols = ("change", "status", "output")
        self.tv = ttk.Treeview(frm, columns=cols, show="tree headings", height=min(max(len(results), 3), 10))
        self.tv.heading("#0", text=tr("File"), anchor="w")
        self.tv.heading("change", text=tr("Claimed → real"))
        self.tv.heading("status", text=tr("Status"))
        self.tv.heading("output", text=tr("Result"), anchor="w")
        self.tv.column("#0", width=230)
        self.tv.column("change", width=140, anchor="center", stretch=False)
        self.tv.column("status", width=230)
        self.tv.column("output", width=260)
        self.tv.tag_configure("skip", foreground="#888")
        self.tv.tag_configure("ok", foreground="#2e7d32")
        self.tv.tag_configure("fail", foreground="#c62828")
        ys = ttk.Scrollbar(frm, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=ys.set)
        self.tv.grid(row=2, column=0, columnspan=2, sticky="nsew")
        ys.grid(row=2, column=2, sticky="ns")

        opts = ttk.LabelFrame(frm, text=tr("Options"), padding=8)
        opts.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        opts.columnconfigure(1, weight=1)
        info = {i["name"]: i for i in settings_mod.Settings.field_info()}
        self.v_output = tk.StringVar(value=cfg.fix_output)
        self.v_backup = tk.StringVar(value=cfg.fix_backup)
        self.v_backup_dir = tk.StringVar(value=cfg.fix_backup_folder)
        self.v_folder = tk.StringVar(value=cfg.fix_folder)
        self.v_format = tk.StringVar(value=cfg.fix_format)
        self.v_bits = tk.StringVar(value=cfg.fix_bits)
        self.v_verify = tk.BooleanVar(value=cfg.fix_verify)

        def combo(row, label, var, key):  # the choices are shown translated, `var` keeps the English one
            ttk.Label(opts, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
            choice_box(opts, var, info[key]["choices"], width=40).grid(row=row, column=1, sticky="w", pady=2)

        def folder_row(row, label, var, title):
            ttk.Label(opts, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
            fr = ttk.Frame(opts)
            fr.grid(row=row, column=1, sticky="ew", pady=2)
            fr.columnconfigure(0, weight=1)
            e = ttk.Entry(fr, textvariable=var)
            e.grid(row=0, column=0, sticky="ew")
            b = ttk.Button(fr, text=tr("Browse…"), command=lambda: self._browse(var, title))
            b.grid(row=0, column=1, padx=(4, 0))
            return e, b

        combo(0, tr("Fix files by:"), self.v_output, "fix_output")
        combo(1, tr("Original file:"), self.v_backup, "fix_backup")
        self.backup_widgets = folder_row(2, tr("Backup folder:"), self.v_backup_dir,
                                         tr("Folder for backups of originals"))
        self.copy_widgets = folder_row(3, tr("Copies folder:"), self.v_folder, tr("Folder for corrected copies"))
        combo(4, tr("Output format:"), self.v_format, "fix_format")
        combo(5, tr("Target bit depth:"), self.v_bits, "fix_bits")
        ttk.Checkbutton(opts, text=tr("Re-check each fixed file"), variable=self.v_verify).grid(
            row=6, column=1, sticky="w", pady=(2, 0))
        ttk.Label(opts, foreground="#777", font=("TkDefaultFont", 8),
                  text=tr("Empty backup folder = a 'Real-Hi-Res backups' folder next to each file (never "
                          "scanned).")).grid(row=7, column=1, sticky="w")
        for v in (self.v_backup, self.v_backup_dir):
            v.trace_add("write", lambda *a: self._replan())
        for v in (self.v_output, self.v_folder, self.v_format, self.v_bits):
            v.trace_add("write", lambda *a: self._replan())
        self.note = ttk.Label(frm, foreground="#9a6700", wraplength=760, justify="left")
        self.note.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        bottom = ttk.Frame(frm)
        bottom.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(1, weight=1)
        self.pb = ttk.Progressbar(bottom, length=200, mode="determinate")
        self.pb.grid(row=0, column=0, padx=(0, 8))
        self.status = ttk.Label(bottom, text="")
        self.status.grid(row=0, column=1, sticky="w")
        self.open_btn = ttk.Button(bottom, text=tr("Show in folder"), command=self._open_folder, state="disabled")
        self.open_btn.grid(row=0, column=2, padx=4)
        self.start_btn = ttk.Button(bottom, text=tr("Fix files"), command=self._start)
        self.start_btn.grid(row=0, column=3)
        ttk.Button(bottom, text=tr("Close"), command=self._close).grid(row=0, column=4, padx=(4, 0))
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda e: self._close())
        self._replan()
        self.after(100, self._poll)
        if autostart and self.plans:  # Settings → Fixing → After a scan
            self.after(400, self._start)

    def _cfg(self):
        c = settings_mod.Settings.from_dict(self.app.cfg.to_dict())
        c.fix_output, c.fix_folder = self.v_output.get(), self.v_folder.get()
        c.fix_backup, c.fix_backup_folder = self.v_backup.get(), self.v_backup_dir.get()
        c.fix_format, c.fix_bits, c.fix_verify = self.v_format.get(), self.v_bits.get(), self.v_verify.get()
        return c

    def _replan(self) -> None:
        from .fix import plan_fix, skip_summary

        if self.running:
            return
        cfg = self._cfg()
        replace = cfg.fix_output.startswith("Replace")
        into = cfg.fix_output.endswith("into a folder")
        backup_folder = replace and cfg.fix_backup.startswith("Move")
        for w in self.copy_widgets:
            w.configure(state="normal" if into else "disabled")
        for w in self.backup_widgets:
            w.configure(state="normal" if backup_folder else "disabled")
        self.tv.delete(*self.tv.get_children())
        self.plans = []
        notes = set()
        skipped = []
        for r in self.results:
            plan, why = plan_fix(r, cfg)
            name = os.path.basename(r.path)
            if not plan:
                skipped.append((r, why))
                continue
            if plan:
                self.plans.append(plan)
                notes.update(plan.notes)
                if plan.replace_original:
                    same = os.path.abspath(plan.output) == os.path.abspath(plan.source)
                    out = tr("Same file") if same else tr("Replaced by {name}", name=os.path.basename(plan.output))
                    out = {"folder": tr("{result} (original → backup folder)", result=out),
                           "bak": tr("{result} (original → .bak)", result=out),
                           "none": tr("{result} (original deleted)", result=out)}[plan.backup]
                else:
                    out = plan.output if into else tr("Copy: {name}", name=os.path.basename(plan.output))
                self.tv.insert("", "end", iid=r.path, text=name, values=(plan.description, tr("Ready"), out))
        # files that are not changed come last, in grey, with their reason
        for r, why in skipped:
            self.tv.insert("", "end", iid=r.path, text=os.path.basename(r.path),
                           values=(r.claimed, tr_text(why), ""), tags=("skip",))
        n = len(self.plans)
        self.skipped = len(skipped)
        if not n:
            head = tr("Nothing to fix")
        elif replace:
            head = tr("Fix 1 file: real resolution") if n == 1 else tr("Fix {n} files: real resolution", n=n)
        else:
            head = (tr("Save corrected copies of 1 file: real resolution") if n == 1
                    else tr("Save corrected copies of {n} files: real resolution", n=n))
        self.head.configure(text=head)
        why_not = tr_text(skip_summary(skipped))
        if why_not and skipped:
            why_not = tr("{summary} They are listed last, in grey.", summary=why_not)
        self.note.configure(text="\n".join([why_not] * bool(why_not) + sorted(tr_text(x) for x in notes)))
        self.start_btn.configure(state="normal" if n else "disabled")
        self.pb.configure(maximum=max(n, 1), value=0)
        self.status.configure(text="")

    def _browse(self, var, title) -> None:
        d = filedialog.askdirectory(title=title, parent=self)
        if d:
            var.set(d)

    def _start(self) -> None:
        from .fix import prepare_gapless, run_fix

        if not self.plans or self.running:
            return
        cfg = self._cfg()
        if cfg.fix_output.startswith("Replace") and cfg.fix_backup.startswith("Delete") and not messagebox.askyesno(
                tr("Delete originals?"), tr("Replace {n} file(s) and delete the originals?\n\n"
                                            "There will be no backup. The converted files contain everything the "
                                            "originals really had, but the originals cannot be restored.",
                                            n=len(self.plans)), parent=self, icon="warning"):
            return
        # remember the chosen options
        for k in ("fix_output", "fix_folder", "fix_backup", "fix_backup_folder", "fix_format", "fix_bits",
                  "fix_verify"):
            setattr(self.app.cfg, k, getattr(cfg, k))
        self.app.save_settings()
        self.running = True
        self.outcomes = []
        self.start_btn.configure(state="disabled")
        self.status.configure(text=tr("Fixing…"))
        for p in self.plans:
            self.tv.set(p.source, "status", tr("Waiting…"))
        plans = list(self.plans)

        def worker():
            # Read the joins between album neighbours before any file is replaced.
            everything = list(self.app.results.values()) + list(self.results)
            joins = prepare_gapless(plans, everything, progress=lambda m: self.q.put(("status", tr_text(m))))
            if joins:
                self.q.put(("status", tr("Fixing… ({n} gapless join(s) kept seamless)", n=joins)))
            with ThreadPoolExecutor(max_workers=max(1, min(effective_jobs(cfg.jobs), 8))) as ex:
                futs = {}
                for p in plans:
                    self.q.put(("start", p.source))
                    futs[ex.submit(run_fix, p, cfg)] = p
                for fut in as_completed(futs):
                    self.q.put(("done", fut.result()))
            self.q.put(("finished", None))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status.configure(text=payload)
                elif kind == "start":
                    self.tv.set(payload, "status", tr("Fixing…"))
                elif kind == "done":
                    o = payload
                    self.outcomes.append(o)
                    self.pb["value"] = self.pb["value"] + 1
                    src = o.plan.source
                    if o.ok:
                        size = f"{o.bytes_after / max(o.bytes_before, 1):.0%}"
                        fixed = o.plan.replace_original
                        if o.new_result is not None:
                            now = dict(claimed=o.new_result.claimed, verdict=verdict_label(o.new_result.verdict),
                                       size=size)
                            txt = (tr("Fixed: now {claimed} ({verdict}), {size} of the size", **now) if fixed
                                   else tr("Copy saved: now {claimed} ({verdict}), {size} of the size", **now))
                        else:
                            txt = (tr("Fixed, {size} of the size", size=size) if fixed
                                   else tr("Copy saved, {size} of the size", size=size))
                        self.tv.item(src, tags=("ok",))
                        if not o.plan.replace_original:
                            self.tv.set(src, "output", o.output)
                        self.done_paths[src] = o.output
                        self.app.add_fixed_result(o)
                    else:
                        txt = tr_text(o.message)
                        self.tv.item(src, tags=("fail",))
                    self.tv.set(src, "status", txt)
                elif kind == "finished":
                    self.running = False
                    ok = sum(1 for i in self.tv.get_children() if "ok" in self.tv.item(i, "tags"))
                    bad = sum(1 for i in self.tv.get_children() if "fail" in self.tv.item(i, "tags"))
                    self.status.configure(text=tr("{ok} fixed, {bad} failed.", ok=ok, bad=bad) if bad
                                          else tr("{ok} fixed.", ok=ok))
                    self.open_btn.configure(state="normal" if ok else "disabled")
                    # A pop-up says it is done. It is not modal (a deferred modal box blocks the
                    # whole window on macOS), and it stays when the Fix window is closed.
                    self.done_popup = FixDoneDialog(self.app, self.outcomes, self.skipped,
                                                    None if self.hidden else self)
                    if self.hidden:
                        self.destroy()
                        return
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)

    def _open_folder(self) -> None:
        for i in self.tv.get_children():
            if "ok" in self.tv.item(i, "tags"):
                out = self.done_paths.get(i)
                if out:
                    reveal_in_file_manager(out)
                    return

    def _close(self) -> None:
        if self.running:
            if not messagebox.askyesno(
                    tr("Conversion running"), tr("Conversions are still running. Close anyway? "
                                                 "The files are finished in the background, and a pop-up tells "
                                                 "you when they are done."), parent=self):
                return
            # keep polling while hidden, so the list is updated and the pop-up appears
            self.hidden = True
            self.withdraw()
            return
        self.destroy()


class FixDoneDialog(tk.Toplevel):
    """Pop-up shown when "Fix to real resolution" has finished: how many files
    were fixed, how much smaller they are, where the originals went, what the
    re-check found, and what failed."""

    def __init__(self, app: "App", outcomes: list, skipped: int = 0, over: Optional[tk.Misc] = None):
        from .fix import done_summary

        super().__init__(app.root)
        self.app = app
        self.title(tr("Fixing finished"))
        self.transient(over or app.root)
        self.resizable(False, False)

        def label(v: str) -> str:
            if i18n.language() != "en":  # the verdict names inside the sentences, translated
                return verdict_label(v)
            name = VERDICT_STYLE.get(v, (v,))[0]
            return name[0].lower() + name[1:] if name[1:2].islower() else name

        head, lines = done_summary(outcomes, skipped, label)
        self.headline, self.lines = head, lines  # as the core wrote them
        head, lines = tr_text(head), [tr_text(line) for line in lines]
        failed = any(not o.ok for o in outcomes)
        self.outputs = [o.output for o in outcomes if o.ok and o.output]
        frm = ttk.Frame(self, padding=16)
        frm.grid(sticky="nsew")
        ttk.Label(frm, text=head, font=("TkDefaultFont", 12, "bold"),
                  foreground="#a3122a" if failed or not self.outputs else "#2e7d32").grid(
            row=0, column=0, sticky="w")
        for i, line in enumerate(lines):
            ttk.Label(frm, text=line, wraplength=460, justify="left").grid(
                row=i + 1, column=0, sticky="w", pady=(8 if i == 0 else 4, 0))
        btns = ttk.Frame(frm)
        btns.grid(row=len(lines) + 1, column=0, sticky="e", pady=(14, 0))
        ttk.Button(btns, text=tr("Show in folder"), command=self._show,
                   state="normal" if self.outputs else "disabled").pack(side="left")
        close = ttk.Button(btns, text=tr("Close"), command=self.destroy)
        close.pack(side="left", padx=(4, 0))
        close.focus_set()
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.destroy())
        self.update_idletasks()
        ref = over if over is not None and over.winfo_viewable() else app.root
        x = ref.winfo_rootx() + (ref.winfo_width() - self.winfo_width()) // 2
        y = ref.winfo_rooty() + (ref.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.lift()

    def _show(self) -> None:
        if self.outputs:
            reveal_in_file_manager(self.outputs[0])


class LyricsDialog(tk.Toplevel):
    """Download synced lyrics from LRCLIB for songs (realhires.lyrics), the way LRCGET does:
    one request at a time, <name>.lrc next to each song."""

    OPTION_KEYS = ("lyrics_existing", "lyrics_plain", "lyrics_instrumental", "lyrics_search")

    def __init__(self, app: "App", paths: List[str], autostart: bool = False):
        super().__init__(app.root)
        self.app = app
        self.title(tr("Download synced lyrics"))
        self.transient(app.root)
        self.paths = list(dict.fromkeys(paths))
        self.todo: List[str] = []
        self.messages: Dict[str, str] = {}  # path -> the full sentence, shown for the selected row
        self.outcomes: list = []
        self.saved_paths: List[str] = []
        self.running = False
        self.stop_event = threading.Event()
        self.eta: Optional[ScanEta] = None
        self._status_time = 0.0
        self.q: queue.Queue = queue.Queue()
        cfg = app.cfg
        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(2, weight=1)
        self.head = ttk.Label(frm, font=("TkDefaultFont", 11, "bold"))
        self.head.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frm, foreground="#555", wraplength=760, justify="left",
                  text=tr("Each song is looked up by the title, artist, album and length in its tags on LRCLIB "
                          "(lrclib.net), a free, open lyrics database, and its synced lyrics are saved as an .lrc "
                          "file with the same name next to it. Most music players find that file and show the "
                          "lyrics line by line in time with the music. The audio files themselves are not "
                          "changed.")).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))
        cols = ("song", "status", "saved")
        self.tv = ttk.Treeview(frm, columns=cols, show="tree headings",
                               height=min(max(len(self.paths), 3), 12))
        self.tv.heading("#0", text=tr("File"), anchor="w")
        self.tv.heading("song", text=tr("Artist – title"), anchor="w")
        self.tv.heading("status", text=tr("Status"), anchor="w")
        self.tv.heading("saved", text=tr("Saved as"), anchor="w")
        self.tv.column("#0", width=210)
        self.tv.column("song", width=210)
        self.tv.column("status", width=260)
        self.tv.column("saved", width=170)
        for tag, color in (("ok", "#2e7d32"), ("plain", "#1565c0"), ("skip", "#888"), ("miss", "#9a6700"),
                           ("fail", "#c62828"), ("busy", "#0b62c4")):
            self.tv.tag_configure(tag, foreground=color)
        ys = ttk.Scrollbar(frm, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=ys.set)
        self.tv.grid(row=2, column=0, sticky="nsew")
        ys.grid(row=2, column=1, sticky="ns")
        self.detail = ttk.Label(frm, foreground="#444", wraplength=760, justify="left",
                                text=tr("Select a song to see the details."))
        self.detail.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.tv.bind("<<TreeviewSelect>>", lambda e: self._show_detail())

        opts = ttk.LabelFrame(frm, text=tr("Options"), padding=8)
        opts.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        opts.columnconfigure(1, weight=1)
        self.v_existing = tk.StringVar(value=cfg.lyrics_existing)
        self.v_plain = tk.BooleanVar(value=cfg.lyrics_plain)
        self.v_instrumental = tk.BooleanVar(value=cfg.lyrics_instrumental)
        self.v_search = tk.BooleanVar(value=cfg.lyrics_search)
        ttk.Label(opts, text=tr("Songs that already have lyrics:")).grid(row=0, column=0, sticky="w", padx=(0, 8))
        choice_box(opts, self.v_existing, lyrics_mod.EXISTING_CHOICES, width=36).grid(
            row=0, column=1, sticky="w", pady=2)
        for row, (text, var) in enumerate((
                (tr("Save plain lyrics as .txt when a song has no synced lyrics"), self.v_plain),
                (tr("Mark instrumental songs with an .lrc holding [au: instrumental]"), self.v_instrumental),
                (tr("Search by title and artist when there is no exact match (same length ±2 s)"),
                 self.v_search)), 1):
            ttk.Checkbutton(opts, text=text, variable=var).grid(row=row, column=0, columnspan=2, sticky="w")
        for v in (self.v_existing, self.v_plain, self.v_instrumental, self.v_search):
            v.trace_add("write", lambda *a: self._replan())
        self.note = ttk.Label(frm, foreground="#666", wraplength=760, justify="left")
        self.note.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        bottom = ttk.Frame(frm)
        bottom.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(1, weight=1)
        self.pb = ttk.Progressbar(bottom, length=200, mode="determinate")
        self.pb.grid(row=0, column=0, padx=(0, 8))
        self.status = ttk.Label(bottom, text="", wraplength=420, justify="left")
        self.status.grid(row=0, column=1, sticky="w")
        self.open_btn = ttk.Button(bottom, text=tr("Show in folder"), command=self._open_folder, state="disabled")
        self.open_btn.grid(row=0, column=2, padx=4)
        self.start_btn = ttk.Button(bottom, text=tr("Download lyrics"), command=self._start)
        self.start_btn.grid(row=0, column=3)
        self.stop_btn = ttk.Button(bottom, text=tr("Stop"), command=self._stop, state="disabled")
        self.stop_btn.grid(row=0, column=4, padx=(4, 0))
        ttk.Button(bottom, text=tr("Close"), command=self._close).grid(row=0, column=5, padx=(4, 0))
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda e: self._close())
        self._replan()
        self.after(100, self._poll)
        if autostart and self.todo:
            self.after(400, self._start)

    def _cfg(self):
        c = settings_mod.Settings.from_dict(self.app.cfg.to_dict())
        c.lyrics_existing, c.lyrics_plain = self.v_existing.get(), self.v_plain.get()
        c.lyrics_instrumental, c.lyrics_search = self.v_instrumental.get(), self.v_search.get()
        return c

    def _song(self, path: str) -> str:
        r = self.app.results.get(path)
        tags = r.tags if r is not None else {}
        artist, title = tags.get("artist") or tags.get("album_artist"), tags.get("title")
        return f"{artist} – {title}" if artist and title else ""

    def _replan(self) -> None:
        if self.running:
            return
        cfg = self._cfg()
        self.tv.delete(*self.tv.get_children())
        self.todo, skipped = [], []
        self.messages = {}
        for p in self.paths:
            why = lyrics_mod.skip_reason(p, cfg)
            (skipped if why else self.todo).append((p, why))
        for p, _ in self.todo:
            self.tv.insert("", "end", iid=p, text=os.path.basename(p), values=(self._song(p), tr("Ready"), ""))
            self.messages[p] = f"{p}\n" + tr("Will be looked up on LRCLIB by its tags.")
        for p, why in skipped:  # listed last, in grey, as in the Fix window
            lrc, txt = lyrics_mod.existing_lyrics(p)
            self.tv.insert("", "end", iid=p, text=os.path.basename(p), tags=("skip",),
                           values=(self._song(p), tr_text(lyrics_mod.short_skip(why)),
                                   os.path.basename(lrc or txt or "")))
            self.messages[p] = f"{p}\n{tr_text(why)}"
        self.todo = [p for p, _ in self.todo]
        n = len(self.todo)
        if n:
            head = tr("Download synced lyrics for 1 song") if n == 1 else tr("Download synced lyrics for {n} songs", n=n)
        else:
            head = tr("Nothing to look up")
        self.head.configure(text=head)
        client_interval = lyrics_mod.default_interval(cfg.lyrics_server)
        lines = []
        if n:
            about = fmt_about(n * (client_interval + 0.35) * (1.3 if cfg.lyrics_search else 1.0))
            lines.append(tr("LRCLIB asks programs to send one request at a time, so this takes {about}.",
                            about=tr_text(about)))
        if skipped:
            lines.append(tr("1 song already has lyrics and is skipped (listed last, in grey); choose 'Download "
                            "again and replace them' to look them up again.") if len(skipped) == 1 else
                         tr("{n} songs already have lyrics and are skipped (listed last, in grey); choose "
                            "'Download again and replace them' to look them up again.", n=len(skipped)))
        lines.append(tr("Songs without title and artist tags cannot be looked up. Lyrics files are never deleted."))
        lines.append(tr_text(lyrics_mod.CREDIT))
        self.note.configure(text="\n".join(lines))
        self.start_btn.configure(state="normal" if n else "disabled")
        self.pb.configure(maximum=max(n, 1), value=0)
        self.status.configure(text="")

    def _start(self) -> None:
        if self.running or not self.todo:
            return
        cfg = self._cfg()
        for k in self.OPTION_KEYS:  # remember the chosen options
            setattr(self.app.cfg, k, getattr(cfg, k))
        self.app.save_settings()
        self.running = True
        self.stop_event.clear()
        self.outcomes = []
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        todo = list(self.todo)
        for p in todo:
            self.tv.set(p, "status", tr("Waiting…"))
        self.eta = ScanEta({p: 1 for p in todo})
        self._update_status()
        results = dict(self.app.results)  # read by the worker thread, never changed by it
        stop = self.stop_event

        def worker():
            client = lyrics_mod.LrclibClient(cfg.lyrics_server, stop=stop)
            try:
                for p in todo:
                    if stop.is_set():
                        break
                    self.q.put(("start", p))
                    try:
                        out = lyrics_mod.fetch_for_file(p, client, cfg, results.get(p))
                    except lyrics_mod.LyricsStopped:
                        break
                    self.q.put(("done", out))
            finally:
                self.q.put(("finished", stop.is_set()))

        threading.Thread(target=worker, daemon=True).start()

    def _update_status(self) -> None:
        done, n = len(self.outcomes), len(self.todo)
        self._status_time = time.time()
        self.status.configure(text=tr("Looking up {n} of {total}…", n=min(done + 1, n), total=n) + " "
                              + (tr_text(self.eta.text()) if self.eta else ""))

    def _poll(self) -> None:
        tags = {lyrics_mod.SYNCED: "ok", lyrics_mod.INSTRUMENTAL: "ok", lyrics_mod.PLAIN: "plain",
                lyrics_mod.SKIPPED: "skip", lyrics_mod.NO_TAGS: "miss", lyrics_mod.NOT_FOUND: "miss",
                lyrics_mod.PLAIN_ONLY: "miss", lyrics_mod.FAILED: "fail"}
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "start":
                    if self.tv.exists(payload):
                        self.tv.set(payload, "status", tr("Looking up…"))
                        self.tv.item(payload, tags=("busy",))
                        self.tv.see(payload)
                elif kind == "done":
                    o = payload
                    self.outcomes.append(o)
                    self.pb["value"] = len(self.outcomes)
                    if self.eta is not None:
                        self.eta.add(o.path)
                    if self.tv.exists(o.path):
                        if o.track is not None:
                            self.tv.set(o.path, "song", o.track.label)
                        self.tv.set(o.path, "status", tr_text(lyrics_mod.short_status(o)))
                        self.tv.set(o.path, "saved", os.path.basename(o.written[0]) if o.written else "")
                        self.tv.item(o.path, tags=(tags.get(o.status, ""),))
                        self.messages[o.path] = f"{o.path}\n{tr_text(o.message)}"
                        if o.path in self.tv.selection():
                            self._show_detail()
                    if o.saved:
                        self.saved_paths.append(o.written[0])
                        self.app.lyrics_saved(o.path)
                    if self.running:
                        self._update_status()
                elif kind == "finished":
                    self.running = False
                    self.stop_btn.configure(state="disabled")
                    text = tr_text(lyrics_mod.lyrics_summary(self.outcomes))
                    text = text[:1].upper() + text[1:]
                    self.status.configure(text=tr("Stopped. {summary}", summary=text) if payload else text)
                    self.open_btn.configure(state="normal" if self.saved_paths else "disabled")
                    n, saved = len(self.todo), len(self.saved_paths)
                    self.head.configure(text=tr("Lyrics saved for {saved} of 1 song", saved=saved) if n == 1
                                        else tr("Lyrics saved for {saved} of {n} songs", saved=saved, n=n))
        except queue.Empty:
            pass
        if self.running and time.time() - self._status_time > 1.0:
            self._update_status()  # the time left keeps counting down between songs
        if self.winfo_exists():
            self.after(100, self._poll)

    def _show_detail(self) -> None:
        sel = self.tv.selection()
        self.detail.configure(text=self.messages.get(sel[0], "") if sel else tr("Select a song to see the details."))

    def _stop(self) -> None:
        if self.running:
            self.stop_event.set()
            self.stop_btn.configure(state="disabled")
            self.status.configure(text=tr("Stopping after the current song…"))

    def _open_folder(self) -> None:
        if self.saved_paths:
            reveal_in_file_manager(self.saved_paths[0])

    def _close(self) -> None:
        if self.running:
            if not messagebox.askyesno(tr("Downloading lyrics"), tr("Lyrics are still being downloaded. Stop and "
                                                                    "close? Lyrics saved so far are kept."),
                                       parent=self):
                return
            self.stop_event.set()
        self.destroy()


class ScanLyrics(threading.Thread):
    """Lyrics while scanning (2.0): one worker thread with one LrclibClient looks up the
    songs a scan has answered, one at a time, as LRCLIB asks, following Settings → Lyrics
    (lyrics_existing, lyrics_save_as, ...).  Posts ("scan_lyrics", self, LyricsOutcome)."""

    def __init__(self, cfg, out: queue.Queue):
        super().__init__(daemon=True, name="realhires-scan-lyrics")
        self.cfg, self.out = cfg, out
        self.todo: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.seen: set = set()
        self.saved = self.not_found = 0

    def add(self, path: str, result=None) -> None:
        if path not in self.seen and not self.stop_event.is_set():
            self.seen.add(path)
            self.todo.put((path, result))

    def stop(self) -> None:
        self.stop_event.set()
        self.todo.put((None, None))

    def count(self, outcome) -> None:
        """Called in the GUI thread for this worker's outcomes."""
        if outcome.saved:
            self.saved += 1
        elif outcome.status == lyrics_mod.NOT_FOUND:
            self.not_found += 1

    def run(self) -> None:
        client = lyrics_mod.LrclibClient(self.cfg.lyrics_server, stop=self.stop_event)
        while not self.stop_event.is_set():
            path, result = self.todo.get()
            if path is None or self.stop_event.is_set():
                break
            try:
                outcome = lyrics_mod.fetch_for_file(path, client, self.cfg, result)
            except lyrics_mod.LyricsStopped:
                break
            if outcome.status != lyrics_mod.SKIPPED:
                self.out.put(("scan_lyrics", self, outcome))


class CardGrid(ttk.Frame):
    """Music-player style grid of album or artist cards: cover art, a ring
    showing how many of the songs are fake/suspicious, and the names."""

    CW, CH, COVER, PAD = 184, 246, 150, 12
    RING_FAKE, RING_SUS, RING_OK, RING_BG = "#d32f2f", "#f39c12", "#43a047", "#e6e6e6"

    def __init__(self, master, app: "App"):
        super().__init__(master)
        import tkinter.font as tkfont

        self.app = app
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, background="#ffffff", highlightthickness=0)
        self.vs = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vs.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vs.grid(row=0, column=1, sticky="ns")
        self.groups: list = []
        self.mode = "album"
        self.images: Dict[str, tk.PhotoImage] = {}
        self.image_items: Dict[str, int] = {}
        self.positions: Dict[str, tuple] = {}
        self.found: set = set()
        self.current: Optional[str] = None
        self.f_title = tkfont.Font(family="TkDefaultFont", size=10, weight="bold")
        self.f_sub = tkfont.Font(family="TkDefaultFont", size=9)
        self.f_ring = tkfont.Font(family="TkDefaultFont", size=9, weight="bold")
        self._cols = 0
        self.canvas.bind("<Configure>", lambda e: self._maybe_relayout())
        for w in (self.canvas,):
            w.bind("<MouseWheel>", self._wheel)
            w.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
            w.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))

    @staticmethod
    def _ph_tag(key: str) -> str:
        """Canvas tag for a card's placeholder.  Album names can contain spaces,
        braces and other characters Tcl rejects in tags, so use a hash."""
        import hashlib

        return "ph" + hashlib.md5(key.encode("utf-8", "replace")).hexdigest()[:12]

    def _wheel(self, e) -> None:
        step = -1 * (e.delta // 120 if abs(e.delta) >= 120 else (1 if e.delta > 0 else -1))
        self.canvas.yview_scroll(step * 3, "units")

    def set_groups(self, groups, mode: str) -> None:
        self.groups, self.mode = groups, mode
        self.relayout()

    def _maybe_relayout(self) -> None:
        cols = max(1, (self.canvas.winfo_width() - self.PAD) // self.CW)
        if cols != self._cols:
            self.relayout()

    def _fit(self, text: str, font, width: int) -> str:
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def relayout(self) -> None:
        c = self.canvas
        c.delete("all")
        self.image_items.clear()
        self.positions.clear()
        w = max(c.winfo_width(), self.CW + self.PAD)
        self._cols = cols = max(1, (w - self.PAD) // self.CW)
        if not self.groups:
            msg = (tr("Scan some files first: albums and artists are read from the files' tags "
                      "(untagged files are grouped by folder).") if not self.app.results else
                   tr("No album or artist matches the search."))
            c.create_text(w / 2, 60, text=msg, fill="#888", width=w - 40)
            c.configure(scrollregion=(0, 0, w, 120))
            return
        for i, g in enumerate(self.groups):
            x = self.PAD + (i % cols) * self.CW
            y = self.PAD + (i // cols) * self.CH
            self.positions[g.key] = (x, y)
            self._draw_card(g, x, y)
        rows = (len(self.groups) + cols - 1) // cols
        c.configure(scrollregion=(0, 0, w, self.PAD + rows * self.CH))
        self.app.request_covers(self.groups, self.mode)

    def _draw_card(self, g, x: int, y: int) -> None:
        c, S = self.canvas, self.COVER
        tag = f"card{id(g)}"
        hl = self.current == g.key
        found = g.key in self.found
        c.create_rectangle(x - 6, y - 6, x + self.CW - 18, y + self.CH - 14,
                           fill="#fff3c4" if found else "#ffffff",
                           outline="#f39c12" if hl else ("#e0c060" if found else "#ffffff"),
                           width=3 if hl else 1, tags=(tag, "cardbg"))
        img = self.images.get(f"{self.mode}:{g.key}")
        if img is not None:
            self.image_items[g.key] = c.create_image(x, y, image=img, anchor="nw", tags=(tag,))
        else:  # placeholder until (or unless) a cover is found
            hue = sum(map(ord, g.title)) % 360
            import colorsys

            r, gg, b = colorsys.hls_to_rgb(hue / 360, 0.42, 0.35)
            col = f"#{int(r * 255):02x}{int(gg * 255):02x}{int(b * 255):02x}"
            c.create_rectangle(x, y, x + S, y + S, fill=col, outline="", tags=(tag, self._ph_tag(g.key)))
            initials = "".join(wd[0] for wd in g.title.split()[:2]).upper() or "?"
            c.create_text(x + S / 2, y + S / 2, text=initials, fill="white",
                          font=("TkDefaultFont", 28, "bold"), tags=(tag, self._ph_tag(g.key)))
        # ring badge (bottom-right corner of the cover)
        cx, cy, R, r = x + S - 8, y + S - 8, 27, 18
        c.create_oval(cx - R - 3, cy - R - 3, cx + R + 3, cy + R + 3, fill="white", outline="", tags=(tag,))
        total = max(g.total, 1)
        start = 90.0
        for count, color in ((g.fake, self.RING_FAKE), (g.suspicious, self.RING_SUS),
                             (g.total - g.problems, self.RING_OK)):
            if count <= 0:
                continue
            ext = 360.0 * count / total
            if ext >= 359.99:
                c.create_oval(cx - R, cy - R, cx + R, cy + R, fill=color, outline="", tags=(tag,))
            else:
                c.create_arc(cx - R, cy - R, cx + R, cy + R, start=start, extent=-ext, fill=color,
                             outline="", style="pieslice", tags=(tag,))
            start -= ext
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="white", outline="", tags=(tag,))
        if self.app.cfg.card_label.startswith("Songs"):
            label = f"{g.problems}/{g.total}"
        else:
            label = f"{g.problem_pct:.0f}%"
        c.create_text(cx, cy, text=label, font=self.f_ring,
                      fill=self.RING_FAKE if g.fake else (self.RING_SUS if g.suspicious else self.RING_OK),
                      tags=(tag,))
        tw = self.CW - 28
        title, subtitle = group_names(g, self.mode)
        c.create_text(x, y + S + 10, anchor="nw", text=self._fit(title, self.f_title, tw),
                      font=self.f_title, fill="#1f2328", tags=(tag,))
        c.create_text(x, y + S + 29, anchor="nw", text=self._fit(subtitle, self.f_sub, tw),
                      font=self.f_sub, fill="#666", tags=(tag,))
        if g.problems:
            parts = []
            if g.fake:
                parts.append(tr("{n} fake", n=g.fake))
            if g.suspicious:
                parts.append(tr("{n} suspicious", n=g.suspicious))
            parts = " + ".join(parts)
            line = (tr("{parts} of 1 song", parts=parts) if g.total == 1
                    else tr("{parts} of {total} songs", parts=parts, total=g.total))
            col = self.RING_FAKE if g.fake else self.RING_SUS
        else:
            line = tr("All 1 song fine") if g.total == 1 else tr("All {n} songs fine", n=g.total)
            col = self.RING_OK
        c.create_text(x, y + S + 46, anchor="nw", text=self._fit(line, self.f_sub, tw), font=self.f_sub,
                      fill=col, tags=(tag,))
        c.tag_bind(tag, "<Button-1>", lambda e, g=g: self.app.open_group(g, self.mode))
        c.tag_bind(tag, "<Enter>", lambda e: c.configure(cursor="hand2"))
        c.tag_bind(tag, "<Leave>", lambda e: c.configure(cursor=""))

    def set_cover(self, key: str, png: Optional[bytes]) -> None:
        """A cover arrived from the loader thread: show it on its card."""
        import base64

        mode, _, gkey = key.partition(":")
        if not png:
            return
        try:
            img = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"), format="png")
        except tk.TclError:
            return
        self.images[key] = img
        if mode == self.mode and gkey in self.positions:
            x, y = self.positions[gkey]
            self.canvas.delete(self._ph_tag(gkey))
            item = self.canvas.create_image(x, y, image=img, anchor="nw")
            self.canvas.tag_lower(item)
            self.canvas.tag_lower("cardbg")
            for g in self.groups:
                if g.key == gkey:
                    self.canvas.addtag_withtag(f"card{id(g)}", item)
                    break

    def highlight(self, found: set, current: Optional[str]) -> None:
        self.found, self.current = found, current
        top = self.canvas.yview()[0]
        self.relayout()
        if current in self.positions:
            y = self.positions[current][1]
            total = float(self.canvas.cget("scrollregion").split()[3] or 1)
            view_h = self.canvas.winfo_height()
            vis_top, vis_bot = top * total, top * total + view_h
            if not (vis_top <= y and y + self.CH <= vis_bot):
                self.canvas.yview_moveto(max(0.0, (y - 10) / total))
            else:
                self.canvas.yview_moveto(top)
        else:
            self.canvas.yview_moveto(top)

# -------------------------------------------------------------------- app ---
class App(ttk.Frame):
    COLUMNS = ("verdict", "why", "claimed", "estimated", "bandwidth", "fix", "name", "folder")
    HEADINGS = {"verdict": N_("Verdict"), "why": N_("Why"), "claimed": N_("Claimed"), "estimated": N_("Real (est.)"),
                "bandwidth": N_("Bandwidth"), "fix": N_("After fix"), "name": N_("File"), "folder": N_("Folder")}

    def __init__(self, root: tk.Tk, initial: Optional[List[str]] = None, cfg=None, parent=None):
        # `parent`: the 2.0 player shows this app as its "Quality check" page inside its own
        # window; then the menu bar is only built (the player shows it on that page), the
        # keyboard shortcuts work while `active`, and quitting asks the player (`quit_callback`).
        self.embedded = parent is not None
        super().__init__(parent if parent is not None else root, padding=8)
        self.root = root
        self.active = True
        self.quit_callback = None
        self.cfg = cfg or settings_mod.load()
        self.sources: List[str] = []                 # added folders/files, in order
        self.source_files: Dict[str, List[str]] = {}  # source -> listed audio files (absolute)
        self.listing: Dict[str, threading.Event] = {}  # sources still being listed -> cancel flag
        self.scan_order: List[str] = []
        self.last_scan_files: List[str] = []
        self.eta: Optional[ScanEta] = None          # time left for the files being analysed
        self._status_time = 0.0
        self.scan_ptr = 0
        self.scan_workers = 1
        self.scan_when_listed = False
        self.results: Dict[str, FileResult] = {}
        self.cache = PlotCache()
        self.store = open_store(self.cfg)            # saved results (main thread's connection)
        self.lookup_gen = 0                         # bumped whenever listed files' results are dropped
        self.lookup_requests: queue.Queue = queue.Queue()
        self.lookup_busy = 0                        # lookups not answered yet
        self.graph_pool = None                      # one worker that draws graphs of saved results
        self.graph_busy: Optional[str] = None
        self.graph_next: Optional[str] = None
        self.graph_failed: set = set()
        self.queue: queue.Queue = queue.Queue()
        threading.Thread(target=lookup_saved, args=(self.lookup_requests, self.queue), daemon=True).start()
        self.scanner: Optional[Scanner] = None
        self.lyrics_dialog: Optional[LyricsDialog] = None  # one at a time: LRCLIB wants sequential requests
        self.total = self.done = 0
        self.scanning = False
        self.scan_started = 0.0
        self.last_elapsed: Optional[float] = None
        self.sort_col, self.sort_rev = "verdict", False
        self.view_var = tk.StringVar(value=self.cfg.view_mode)
        self.group_filter: Optional[dict] = None   # {"mode","key","label","paths","back","filters"}
        self.cover_requests: queue.Queue = queue.Queue()
        self.cover_seen: set = set()
        self.find_var = tk.StringVar()
        self.find_matches: List[str] = []
        self.find_index = -1
        self.find_open = False
        self._regroup_pending = False
        self.show_vars = {v: tk.BooleanVar(value=v in self.cfg.shown_verdicts) for v in VERDICT_ORDER}
        self.search_var = tk.StringVar()
        self._search_job: Optional[str] = None
        self._values_cache: Dict[str, tuple] = {}  # path -> (result, row values)
        self.quick_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value=tr("Add a folder or files, then press Scan."))
        self.scan_lyrics: Optional[ScanLyrics] = None  # lyrics while scanning (Settings → Library)
        self._last_stopped = False
        self._build_menu()
        self._build()
        if not self.embedded:
            self.grid(sticky="nsew")
            root.columnconfigure(0, weight=1)
            root.rowconfigure(0, weight=1)
            root.protocol("WM_DELETE_WINDOW", self.quit_app)
        if self.view_var.get() != "Files":
            self.after(50, self.switch_view)
        if initial:
            for p in initial:
                self._add_source(p)
            self.after(200, self.start_scan)  # waits for the listing to finish
        self.after(100, self._poll)

    # ---------- layout ---------------------------------------------------
    def _build_menu(self) -> None:
        mod = "Command" if sys.platform == "darwin" else "Control"
        acc = "Cmd" if sys.platform == "darwin" else "Ctrl"
        m = tk.Menu(self.root)
        f = tk.Menu(m, tearoff=False)
        f.add_command(label=tr("Add Folder…"), accelerator=f"{acc}+O", command=self.add_folder)
        f.add_command(label=tr("Add Files…"), command=self.add_files)
        f.add_separator()
        f.add_command(label=tr("Export listed files as CSV…"), command=lambda: self.export("csv"))
        f.add_command(label=tr("Export listed files as JSON…"), command=lambda: self.export("json"))
        f.add_separator()
        f.add_command(label=tr("Delete all saved results…"), command=self.forget_saved)
        f.add_separator()
        f.add_command(label=tr("Quit"), accelerator=f"{acc}+Q", command=self.quit_app)
        m.add_cascade(label=tr("File"), menu=f)
        e = tk.Menu(m, tearoff=False)
        e.add_command(label=tr("Open file location"), accelerator="Enter", command=self.reveal)
        e.add_command(label=tr("Open file"), accelerator=f"{acc}+Enter", command=self.open_file)
        e.add_command(label=tr("Select all listed files"), accelerator=f"{acc}+A", command=self.select_all)
        e.add_command(label=tr("Copy path"), accelerator=f"{acc}+C", command=self.copy_path)
        e.add_separator()
        e.add_command(label=tr("Find…"), accelerator=f"{acc}+F", command=self.open_find)
        e.add_separator()
        e.add_command(label=tr("Fix selected files to real resolution…"), accelerator=f"{acc}+Shift+F",
                      command=self.fix_selected)
        e.add_command(label=tr("Fix all fake files to real resolution…"), command=self.fix_all)
        e.add_separator()
        e.add_command(label=tr("Download synced lyrics for selected songs…"), accelerator=f"{acc}+L",
                      command=self.lyrics_selected)
        e.add_command(label=tr("Download synced lyrics for all songs…"), command=self.lyrics_all)
        e.add_separator()
        e.add_command(label=tr("Re-scan selected files"), command=self.rescan_selected)
        e.add_command(label=tr("Re-scan files checked by older rules"), command=self.rescan_outdated)
        e.add_separator()
        e.add_command(label=tr("Settings…"), accelerator=f"{acc}+,", command=self.open_settings)
        m.add_cascade(label=tr("Edit"), menu=e)
        v = tk.Menu(m, tearoff=False)
        for mode in (N_("Files"), N_("By album"), N_("By artist")):  # the view_mode values, shown translated
            v.add_command(label=tr(mode), command=lambda mode=mode: self.set_view(mode))
        v.add_separator()
        v.add_command(label=tr("Scan summary…"), command=self.show_summary)
        v.add_command(label=tr("Show problem files only"), command=lambda: self.set_filters(PROBLEM_VERDICTS))
        v.add_command(label=tr("Show all files"), command=lambda: self.set_filters(VERDICT_ORDER))
        m.add_cascade(label=tr("View"), menu=v)
        self.view_menu = v
        h = tk.Menu(m, tearoff=False)
        h.add_command(label=tr("About {name}", name=APP_NAME), command=self.about)
        m.add_cascade(label=tr("Help"), menu=h)
        self.menubar = m
        if not self.embedded:
            self.root.configure(menu=m)

        def key(fn):  # embedded: the shortcuts belong to the player while another page is shown
            return lambda e: fn(e) if self.active else None

        self.root.bind_all(f"<{mod}-o>", key(lambda e: self.add_folder()))
        self.root.bind_all(f"<{mod}-q>", lambda e: self.quit_app())
        self.root.bind_all(f"<{mod}-comma>", key(lambda e: self.open_settings()))
        self.root.bind_all(f"<{mod}-f>", key(lambda e: self.open_find()))
        self.root.bind_all(f"<{mod}-F>", key(lambda e: self.fix_selected()))  # Shift+F
        self.root.bind_all(f"<{mod}-a>", key(self.select_all))
        self.root.bind_all(f"<{mod}-l>", key(self.lyrics_selected))
        self.root.bind_all("<F3>", key(lambda e: self.find_step(1)))
        self.root.bind_all("<Shift-F3>", key(lambda e: self.find_step(-1)))

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text=tr("Add Folder…"), command=self.add_folder).pack(side="left")
        ttk.Button(bar, text=tr("Add Files…"), command=self.add_files).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text=tr("Clear"), command=self.clear).pack(side="left", padx=(4, 12))
        self.quick_cb = ttk.Checkbutton(bar, variable=self.quick_var)
        self.quick_cb.pack(side="left")
        self._update_quick_label()
        ttk.Button(bar, text="⚙ " + tr("Settings…"), command=self.open_settings).pack(side="right")
        self.stop_btn = ttk.Button(bar, text=tr("Stop"), command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side="right", padx=(0, 12))
        self.scan_btn = ttk.Button(bar, text=tr("Scan"), command=self.start_scan)
        self.scan_btn.pack(side="right", padx=(0, 4))

        src = ttk.LabelFrame(self, text=tr("Sources to scan"), padding=(6, 4))
        src.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        src.columnconfigure(0, weight=1)
        self.src_tree = ttk.Treeview(src, columns=("kind", "files", "problems"), height=3,
                                     show="tree headings", selectmode="extended")
        self.src_tree.heading("#0", text=tr("Folder / file"), anchor="w")
        self.src_tree.heading("kind", text=tr("Type"))
        self.src_tree.heading("files", text=tr("Audio files"))
        self.src_tree.heading("problems", text=tr("Fake / suspicious"))
        self.src_tree.column("#0", width=560, stretch=True)
        self.src_tree.column("kind", width=70, anchor="center", stretch=False)
        self.src_tree.column("files", width=150, anchor="center", stretch=False)
        self.src_tree.column("problems", width=130, anchor="center", stretch=False)
        self.src_tree.tag_configure("empty", foreground="#c62828")
        self.src_tree.tag_configure("busy", foreground="#808080")
        sys_ = ttk.Scrollbar(src, orient="vertical", command=self.src_tree.yview)
        self.src_tree.configure(yscrollcommand=sys_.set)
        self.src_tree.grid(row=0, column=0, sticky="ew")
        sys_.grid(row=0, column=1, sticky="ns")
        side = ttk.Frame(src)
        side.grid(row=0, column=2, sticky="n", padx=(6, 0))
        self.remove_btn = ttk.Button(side, text=tr("Remove"), command=self.remove_sources)
        self.remove_btn.pack(fill="x")
        self.src_hint = ttk.Label(src, foreground="#666",
                                  text=tr("Nothing added yet. Click “Add Folder…” or “Add Files…”: the files "
                                          "found are listed below straight away, then press Scan."))
        self.src_hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.src_tree.bind("<Delete>", lambda e: self.remove_sources())
        self.src_tree.bind("<BackSpace>", lambda e: self.remove_sources())

        vbar = ttk.Frame(self)
        vbar.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(vbar, text=tr("View:")).pack(side="left")
        for label in ("Files", "By album", "By artist"):  # the value stays English (view_mode)
            ttk.Radiobutton(vbar, text=tr(label), value=label, variable=self.view_var, style="Toolbutton",
                            command=self.switch_view).pack(side="left", padx=(4, 0))
        self.grid_opts = ttk.Frame(vbar)
        ttk.Label(self.grid_opts, text=tr("Rings show:")).pack(side="left", padx=(16, 4))
        self.card_label_var = tk.StringVar(value=self.cfg.card_label)
        choice_box(self.grid_opts, self.card_label_var, [N_("Percentage"), N_("Songs (x of n)")],
                   width=14).pack(side="left")
        ttk.Label(self.grid_opts, text=tr("Sort:")).pack(side="left", padx=(12, 4))
        self.group_sort_var = tk.StringVar(value=self.cfg.group_sort)
        choice_box(self.grid_opts, self.group_sort_var, [N_("Most fake first"), N_("Name")],
                   width=14).pack(side="left")
        self.card_label_var.trace_add("write", lambda *a: self._grid_option_changed())
        self.group_sort_var.trace_add("write", lambda *a: self._grid_option_changed())
        self.chip = ttk.Frame(vbar)
        self.chip_label = ttk.Label(self.chip, foreground="#0b62c4", font=("TkDefaultFont", 9, "bold"))
        self.chip_label.pack(side="left", padx=(16, 6))
        self.chip_back = ttk.Button(self.chip, command=self.close_group)
        self.chip_back.pack(side="left")

        filt = ttk.Frame(self)
        filt.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(filt, text=tr("Show:")).pack(side="left")
        self.filter_checks: Dict[str, ttk.Checkbutton] = {}
        for v in VERDICT_ORDER:
            cb = ttk.Checkbutton(filt, text=verdict_label(v), variable=self.show_vars[v],
                                 command=self.refresh)
            cb.pack(side="left", padx=(6, 0))
            self.filter_checks[v] = cb
        entry = ttk.Entry(filt, textvariable=self.search_var, width=24)
        entry.pack(side="right")
        ttk.Label(filt, text=tr("Search:")).pack(side="right", padx=(0, 4))
        self.search_var.trace_add("write", lambda *a: self._search_changed())

        # browser-style find bar (Ctrl/Cmd+F)
        self.find_frame = ttk.Frame(self, padding=(4, 3))
        self.find_frame.grid(row=4, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(self.find_frame, text=tr("Find:")).pack(side="left")
        self.find_entry = ttk.Entry(self.find_frame, textvariable=self.find_var, width=34)
        self.find_entry.pack(side="left", padx=(4, 4))
        ttk.Button(self.find_frame, text="▲", width=3, command=lambda: self.find_step(-1)).pack(side="left")
        ttk.Button(self.find_frame, text="▼", width=3, command=lambda: self.find_step(1)).pack(side="left", padx=(2, 0))
        self.find_count = ttk.Label(self.find_frame, text="", foreground="#555")
        self.find_count.pack(side="left", padx=(8, 0))
        ttk.Button(self.find_frame, text="✕", width=3, command=self.close_find).pack(side="right")
        ttk.Label(self.find_frame, text=tr("Enter = next, Shift+Enter = previous, Esc = close"),
                  foreground="#888", font=("TkDefaultFont", 8)).pack(side="right", padx=8)
        self.find_entry.bind("<Return>", lambda e: self.find_step(1))
        self.find_entry.bind("<KP_Enter>", lambda e: self.find_step(1))
        self.find_entry.bind("<Shift-Return>", lambda e: self.find_step(-1))
        self.find_entry.bind("<Escape>", lambda e: self.close_find())
        self.find_var.trace_add("write", lambda *a: self.update_find(reset=True))
        self.find_frame.grid_remove()

        panes = ttk.PanedWindow(self, orient="vertical")
        panes.grid(row=5, column=0, sticky="nsew")
        self.rowconfigure(5, weight=1)

        # result list
        table = ttk.Frame(panes)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=self.COLUMNS, show="headings", selectmode="extended",
                                 height=7)
        widths = {"verdict": 105, "why": 400, "claimed": 70, "estimated": 85, "bandwidth": 82,
                  "fix": 262, "name": 230, "folder": 220}
        for c in self.COLUMNS:
            self.tree.heading(c, text=tr(self.HEADINGS[c]), command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=widths[c], stretch=c in ("why", "name", "folder"),
                             anchor="w" if c in ("name", "folder", "verdict", "why", "fix") else "center")
        for v, (_, color, _) in VERDICT_STYLE.items():
            self.tree.tag_configure(v, foreground=color)
        self.tree.tag_configure(PENDING, foreground=PENDING_STYLE[1])
        self.tree.tag_configure(RUNNING, foreground=RUNNING_STYLE[1])
        self.tree.tag_configure("found", background="#fff59d")
        self.tree.tag_configure("found_cur", background="#ffb74d")
        ys = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        self.tree.bind("<Double-1>", lambda e: self._primary_action())
        self.tree.bind("<Return>", lambda e: self._primary_action())
        mod = "Command" if sys.platform == "darwin" else "Control"
        self.tree.bind(f"<{mod}-Return>", lambda e: self._secondary_action())
        self.tree.bind(f"<{mod}-c>", lambda e: self.copy_path())
        self.tree.bind("<Button-3>", self._context_menu)
        self.tree.bind("<Button-2>", self._context_menu)  # macOS right click
        self.cards = CardGrid(table, self)
        self.cards.grid(row=0, column=0, columnspan=2, rowspan=2, sticky="nsew")
        self.cards.grid_remove()
        self._table_widgets = (self.tree, ys, xs)
        panes.add(table, weight=3)

        # dashboard
        dash = ttk.PanedWindow(panes, orient="horizontal")
        info = ttk.Frame(dash, padding=(4, 6, 8, 4))
        info.columnconfigure(1, weight=1)
        info.rowconfigure(5, weight=1)
        self.badge = tk.Label(info, text="", fg="white", bg="#9e9e9e", padx=10, pady=3,
                              font=("TkDefaultFont", 11, "bold"))
        self.badge.grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar(value=tr("Select a file in the list"))
        ttk.Label(info, textvariable=self.name_var, font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        self.why_var = tk.StringVar(value="")
        self.why_label = ttk.Label(info, textvariable=self.why_var, wraplength=420, justify="left",
                                   font=("TkDefaultFont", 10))
        self.why_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 6))
        facts = ttk.Frame(info)
        facts.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.fact_vars: Dict[str, tk.StringVar] = {}
        for i, key in enumerate(FACTS):  # the keys stay English (fact_vars), the labels are translated
            ttk.Label(facts, text=tr("{fact}:", fact=tr(key)), foreground="#666").grid(
                row=i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 4))
            var = tk.StringVar(value="–")
            self.fact_vars[key] = var
            # "After fix" is the last and longest: it spans the second pair of columns too
            ttk.Label(facts, textvariable=var).grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w",
                                                    padx=(0, 18), columnspan=3 if key == "After fix" else 1)
        btns = ttk.Frame(info)
        btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 6))
        self.open_folder_btn = ttk.Button(btns, text=tr("Open Folder"), command=self.reveal, state="disabled")
        self.open_folder_btn.pack(side="left")
        self.open_file_btn = ttk.Button(btns, text=tr("Open File"), command=self.open_file, state="disabled")
        self.open_file_btn.pack(side="left", padx=4)
        self.copy_btn = ttk.Button(btns, text=tr("Copy Path"), command=self.copy_path, state="disabled")
        self.copy_btn.pack(side="left")
        self.fix_btn = ttk.Button(btns, text=tr("Fix to real resolution…"), command=self.fix_selected,
                                  state="disabled")
        self.fix_btn.pack(side="left", padx=(12, 0))
        self.lyrics_btn = ttk.Button(btns, text=tr("Get lyrics…"), command=self.lyrics_selected, state="disabled")
        self.lyrics_btn.pack(side="left", padx=(4, 0))
        ttk.Label(info, text=tr("Details"), foreground="#666").grid(row=4, column=0, columnspan=2, sticky="w")
        txt_frame = ttk.Frame(info)
        txt_frame.grid(row=5, column=0, columnspan=2, sticky="nsew")
        txt_frame.rowconfigure(0, weight=1)
        txt_frame.columnconfigure(0, weight=1)
        self.text = tk.Text(txt_frame, wrap="word", height=4, relief="flat", padx=6, pady=4,
                            background="#fafafa", foreground="#1f2328", font=("TkDefaultFont", 9))
        tys = ttk.Scrollbar(txt_frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=tys.set, state="disabled")
        self.text.grid(row=0, column=0, sticky="nsew")
        tys.grid(row=0, column=1, sticky="ns")
        dash.add(info, weight=2)
        info.bind("<Configure>", lambda e: self.why_label.configure(wraplength=max(200, e.width - 20)))

        plots = ttk.Notebook(dash)
        self.spectrogram = SpectrogramCanvas(plots, self, height=200)
        self.spectrum = SpectrumCanvas(plots, height=200)
        plots.add(self.spectrogram, text=tr("Spectrogram"))
        plots.add(self.spectrum, text=tr("Average spectrum"))
        lyr = ttk.Frame(plots)
        lyr.rowconfigure(0, weight=1)
        lyr.columnconfigure(0, weight=1)
        self.lyrics_text = tk.Text(lyr, wrap="word", height=8, width=30, relief="flat", padx=10, pady=6,
                                   background="#fafafa", foreground="#1f2328", font=("TkDefaultFont", 10))
        lys = ttk.Scrollbar(lyr, orient="vertical", command=self.lyrics_text.yview)
        self.lyrics_text.configure(yscrollcommand=lys.set, state="disabled")
        self.lyrics_text.tag_configure("time", foreground="#9aa0a6", font=("TkFixedFont", 9))
        self.lyrics_text.tag_configure("muted", foreground="#666", font=("TkDefaultFont", 9))
        self.lyrics_text.grid(row=0, column=0, sticky="nsew")
        lys.grid(row=0, column=1, sticky="ns")
        plots.add(lyr, text=tr("Lyrics"))
        self.plots = plots
        dash.add(plots, weight=3)
        panes.add(dash, weight=2)

        status = ttk.Frame(self)
        status.grid(row=6, column=0, sticky="ews", pady=(6, 0))
        self.rowconfigure(6, weight=0, minsize=24)
        status.columnconfigure(1, weight=1)
        self.progress = ttk.Progressbar(status, length=220, mode="determinate")
        self.progress.grid(row=0, column=0, padx=(0, 8))
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=1, sticky="w")
        self.outdated_var = tk.StringVar(value="")
        self.outdated_label = ttk.Label(status, textvariable=self.outdated_var, foreground="#8a5a00")
        self.outdated_btn = ttk.Button(status, text=tr("Re-scan them"), command=self.rescan_outdated)

        self.menu = tk.Menu(self.root, tearoff=False)
        self.menu.add_command(label=tr("Open file location"), command=self.reveal)
        self.menu.add_command(label=tr("Open file"), command=self.open_file)
        self.menu.add_command(label=tr("Copy path"), command=self.copy_path)
        self.menu.add_separator()
        self.menu.add_command(label=tr("Fix to real resolution…"), command=self.fix_selected)
        self.menu.add_command(label=tr("Download synced lyrics…"), command=self.lyrics_selected)
        self.menu.add_command(label=tr("Re-scan"), command=self.rescan_selected)

    def _update_quick_label(self) -> None:
        self.quick_cb.configure(text=tr("Quick scan (first {seconds} s)", seconds=f"{self.cfg.quick_scan_seconds:g}"))

    # ---------- settings -------------------------------------------------
    def open_settings(self) -> None:
        SettingsDialog(self)

    def save_settings(self) -> None:
        try:
            settings_mod.save(self.cfg)
        except OSError as exc:
            messagebox.showwarning(tr("Settings"), tr("Could not save settings: {error}", error=tr_text(str(exc))))

    def apply_settings(self, new) -> None:
        old_shown = set(self.cfg.shown_verdicts)
        relist = (new.extension_set() != self.cfg.extension_set() or new.recursive != self.cfg.recursive)
        detection = settings_key(new) != settings_key(self.cfg)
        store_changed = (new.save_results, new.saved_graphs_mb) != (self.cfg.save_results, self.cfg.saved_graphs_mb)
        fixing = new.fix_bits != self.cfg.fix_bits  # changes the "After fix" column
        self.cfg = new
        if fixing and not detection:
            self._values_cache.clear()
            self.refresh()
            self.show_details()
        if store_changed:
            if self.store is not None:
                self.store.close()
            self.store = open_store(new)
        if relist and not self.scanning:  # file list depends on these: list the sources again
            sources = list(self.sources)
            self.clear()
            for src in sources:
                self._add_source(src)
        elif detection and not self.scanning and self.results:
            # the results on screen were made with other detection settings: show the
            # results saved for the new ones, the other files wait for a scan
            self.results.clear()
            self.cache.clear()
            self.lookup_gen += 1
            self.refresh()
            self.show_details()
            self._update_source_counts()
            self._request_lookup(self.all_files())
        self.save_settings()
        if set(new.shown_verdicts) != old_shown:
            self.set_filters(new.shown_verdicts)
        self._update_quick_label()
        self.spectrogram.redraw()
        self.status_var.set(tr("Settings saved. Files without a result for the new detection settings are "
                               "analysed when you press Scan.") if detection else tr("Settings saved."))

    # ---------- sources --------------------------------------------------
    def _add_source(self, path: str) -> None:
        """Add a folder/file and list its audio files right away (in the
        background), so the user sees exactly what will be scanned."""
        if not path:
            return
        path = os.path.abspath(path)
        if path in self.sources:
            self.status_var.set(tr("Already added: {path}", path=path))
            if self.src_tree.exists(path):
                self.src_tree.selection_set(path)
                self.src_tree.see(path)
            return
        self.sources.append(path)
        kind = tr("Folder") if os.path.isdir(path) else tr("File")
        self.src_tree.insert("", "end", iid=path, text=path, values=(kind, tr("counting…"), ""),
                             tags=("busy",))
        self.src_tree.see(path)
        self.src_hint.grid_remove()
        self.src_tree.configure(height=min(max(len(self.sources), 2), 5))
        cancel = threading.Event()
        self.listing[path] = cancel
        self.source_files[path] = []
        self.status_var.set(tr("Looking for audio files in {path} …", path=path))
        threading.Thread(target=list_source, args=(path, self.cfg, self.queue, cancel), daemon=True).start()

    def _on_listing(self, source: str, batch: List[str]) -> None:
        """A batch of files found while a folder is still being walked."""
        if source not in self.sources or source not in self.listing:
            return
        known = set(self.all_files())
        self.source_files[source].extend(batch)
        n = len(self.source_files[source])
        if self.src_tree.exists(source):
            self.src_tree.set(source, "files", tr("{n} so far…", n=n))
        q = self._filter_state()[1]
        for path in batch:
            if path not in known and not self.tree.exists(path) and (not q or q in path.lower()):
                self.tree.insert("", "end", iid=path, values=self._pending_values(path), tags=(PENDING,))
        self._request_lookup([p for p in batch if p not in known])
        self.status_var.set(tr("Looking for audio files… {n} found so far", n=len(known) + len(batch)))

    def _on_listed(self, source: str, files: Optional[List[str]] = None) -> None:
        self.listing.pop(source, None)
        if source not in self.sources:  # removed while listing
            return
        if files is not None:
            self.source_files[source] = files
            self._request_lookup(files)
        files = self.source_files.get(source, [])
        if self.src_tree.exists(source):
            n = len(files)
            if n:
                self.src_tree.item(source, values=(self.src_tree.set(source, "kind"),
                                                   tr("1 file") if n == 1 else tr("{n} files", n=n), ""), tags=())
            else:
                why = (tr("not an audio file (check Settings → Scanning → File extensions)")
                       if os.path.isfile(source) else tr("no audio files found"))
                self.src_tree.item(source, values=(self.src_tree.set(source, "kind"), why, ""),
                                   tags=("empty",))
        self.refresh()
        if not self.listing:
            if not self.scanning:
                self.status_var.set(self._ready_message())
            if self.scan_when_listed:
                self.scan_when_listed = False
                self.start_scan()

    def _ready_message(self) -> str:
        files = self.all_files()
        total = len(files)
        empty = [s for s in self.sources if not self.source_files.get(s)]
        saved = sum(1 for f in files if f in self.results)
        if self.lookup_busy:
            msg = (tr("1 audio file found; looking up saved results ({saved} so far)…", saved=saved) if total == 1
                   else tr("{n} audio files found; looking up saved results ({saved} so far)…", n=total, saved=saved))
        elif saved:
            rest = total - saved
            msg = tr("{saved} of {total} files were checked before and have not changed: their saved results "
                     "are shown.", saved=saved, total=total)
            msg = tr("{sentence} {next}", sentence=msg,
                     next=tr("Press Scan to analyse the other {n}.", n=rest) if rest else tr("Nothing new to scan."))
        else:
            n, ns = total, len(self.sources)
            files = tr("1 audio file") if n == 1 else tr("{n} audio files", n=n)
            msg = (tr("{files} ready in 1 source. Press Scan.", files=files) if ns == 1
                   else tr("{files} ready in {n} sources. Press Scan.", files=files, n=ns))
        if empty:
            msg += "  " + tr("({n} source(s) contain no audio files.)", n=len(empty))
        return msg

    # ---------- saved results --------------------------------------------
    def _wanted(self) -> Optional[float]:
        return self.cfg.quick_scan_seconds if self.quick_var.get() else (self.cfg.max_seconds or None)

    def _request_lookup(self, paths) -> None:
        """Show saved results for these newly listed files (background lookup)."""
        if paths and self.cfg.save_results:
            self.lookup_busy += 1
            self.lookup_requests.put((self.lookup_gen, self.cfg, self._wanted(), list(paths)))

    def _on_saved(self, gen: int, results: List[FileResult]) -> bool:
        if gen != self.lookup_gen or self.scanning:
            return False
        listed = set(self.all_files())
        added = False
        for r in results:
            if r.path in listed and r.path not in self.results:
                self.results[r.path] = r
                self._insert(r)
                added = True
        return added

    def _on_saved_done(self, gen: int) -> None:
        self.lookup_busy = max(0, self.lookup_busy - 1)
        if gen == self.lookup_gen and not self.lookup_busy and not self.listing and not self.scanning:
            self._update_filter_counts()
            self._update_source_counts()
            self._update_outdated_notice()
            self.status_var.set(self._ready_message())
            if self.results and not self.tree.selection():
                kids = self.tree.get_children()
                if kids:
                    self.tree.selection_set(kids[0])
                    self.tree.focus(kids[0])

    def _update_outdated_notice(self) -> None:
        n = sum(1 for r in self.results.values() if r.saved and r.saved.get("outdated"))
        if n and not self.scanning:
            self.outdated_var.set(tr("1 saved result was made by older detection rules.") if n == 1 else
                                  tr("{n} saved results were made by older detection rules.", n=n))
            self.outdated_label.grid(row=0, column=2, sticky="e", padx=(8, 4))
            self.outdated_btn.grid(row=0, column=3, sticky="e")
        else:
            self.outdated_label.grid_remove()
            self.outdated_btn.grid_remove()

    def rescan_selected(self) -> None:
        paths = [p for p in self.tree.selection() if p in set(self.all_files())]
        if not paths:
            self.status_var.set(tr("Select the files to analyse again."))
            return
        self.start_scan(files=paths, force=True)

    def rescan_outdated(self) -> None:
        paths = [p for p, r in self.results.items() if r.saved and r.saved.get("outdated")]
        if not paths:
            self.status_var.set(tr("No saved results from older detection rules are listed."))
            return
        self.start_scan(files=paths, force=True)

    def forget_saved(self) -> None:
        if self.store is None:
            messagebox.showinfo(tr("Saved results"), tr("Saving results is turned off (Settings → Saved results)."))
            return
        st = self.store.stats()
        if not messagebox.askyesno(
                tr("Delete all saved results"),
                tr("Delete the saved results of {n} file(s) ({mb} MB in {path})?\n\nThe files themselves are "
                   "not touched; they are simply analysed again the next time they are scanned.",
                   n=st["files"], mb=f"{st['disk_bytes'] / 1e6:.0f}", path=st["path"]), icon="warning"):
            return
        try:
            n = self.store.forget()
        except StoreError as exc:
            messagebox.showerror(tr("Saved results"),
                                 tr("Could not delete the saved results: {error}", error=tr_text(str(exc))))
            return
        for r in self.results.values():
            r.saved = None
        self._update_outdated_notice()
        self.status_var.set(tr("Deleted {n} saved result(s). The results on screen stay until you scan again.", n=n))

    def _want_graphs(self, r: FileResult) -> bool:
        """Draw the graphs of a saved result whose graphs were not saved (one at
        a time, in a separate process; only the latest request waits)."""
        if r.path in self.graph_failed or not os.path.exists(r.path):
            return False
        if self.graph_busy is not None:
            if self.graph_busy != r.path:
                self.graph_next = r.path
            return True
        if self.graph_pool is None:
            self.graph_pool = MultiProcessPool(1, mp_context=multiprocessing.get_context("spawn"))
        self.graph_busy = r.path
        partial_scan = r.duration and r.analysed_seconds < r.duration - 1
        fut = self.graph_pool.submit(_work, r.path, r.analysed_seconds if partial_scan else None, self.cfg)
        fut.add_done_callback(lambda f, path=r.path: self.queue.put(
            ("graphs", path, f.result().plot if not f.cancelled() and f.exception() is None else None)))
        return True

    def _on_graphs(self, path: str, plot: Optional[dict]) -> None:
        self.graph_busy = None
        if plot:
            self.cache.put(path, plot)
            if self.store is not None:
                self.store.put_plot(path, settings_key(self.cfg), plot)
        else:
            self.graph_failed.add(path)
        if self._current_path() == path:
            self.show_details()
        nxt, self.graph_next = self.graph_next, None
        if nxt and nxt == self._current_path() and nxt in self.results and self.cache.get(nxt) is None:
            self._want_graphs(self.results[nxt])

    def all_files(self) -> List[str]:
        seen, out = set(), []
        for src in self.sources:
            for f in self.source_files.get(src, []):
                if f not in seen:
                    seen.add(f)
                    out.append(f)
        return out

    def remove_sources(self) -> None:
        if self.scanning:
            self.status_var.set(tr("Stop the scan before removing sources."))
            return
        sel = list(self.src_tree.selection())
        if not sel:
            self.status_var.set(tr("Select a source in the “Sources to scan” list to remove it."))
            return
        for src in sel:
            if src in self.sources:
                self.sources.remove(src)
            self.source_files.pop(src, None)
            cancel = self.listing.pop(src, None)
            if cancel:
                cancel.set()
            self.src_tree.delete(src)
        keep = set(self.all_files())
        for path in [p for p in self.results if p not in keep]:
            del self.results[path]
        if not self.sources:
            self.src_hint.grid()
        self.refresh()
        self.show_details()
        n = len(keep)
        self.status_var.set(tr("Removed {n} source(s). 1 audio file left to scan.", n=len(sel)) if n == 1 else
                            tr("Removed {n} source(s). {files} audio files left to scan.", n=len(sel), files=n))

    def _update_source_counts(self) -> None:
        for src in self.sources:
            if not self.src_tree.exists(src) or src in self.listing:
                continue
            files = self.source_files.get(src, [])
            done = [self.results[f] for f in files if f in self.results]
            probs = sum(1 for r in done if r.is_problem)
            self.src_tree.set(src, "problems", tr("{n} of {total}", n=probs, total=len(done)) if done else "")

    def add_folder(self) -> None:
        d = filedialog.askdirectory(title=tr("Choose a music folder"))
        if d:
            self._add_source(d)

    def add_files(self) -> None:
        pattern = " ".join(f"*{e}" for e in sorted(self.cfg.extension_set()))
        files = filedialog.askopenfilenames(title=tr("Choose audio files"),
                                            filetypes=[(tr("Audio files"), pattern), (tr("All files"), "*")])
        for f in files:
            self._add_source(f)

    def clear(self) -> None:
        if self.scanning:
            return
        self.sources.clear()
        self.source_files.clear()
        for cancel in self.listing.values():
            cancel.set()
        self.listing.clear()
        self.results.clear()
        self.cache.clear()
        self.lookup_gen += 1
        self._update_outdated_notice()
        self.src_tree.delete(*self.src_tree.get_children())
        self.src_hint.grid()
        self.refresh()
        self.show_details()
        self.progress["value"] = 0
        for v, cb in self.filter_checks.items():
            cb.configure(text=verdict_label(v))
        self.status_var.set(tr("Add a folder or files, then press Scan."))

    # ---------- scanning -------------------------------------------------
    def start_scan(self, files: Optional[List[str]] = None, force: bool = False) -> None:
        """Scan the listed files: saved results of unchanged files are shown at
        once, the others are analysed.  `files` (with `force`) analyses just
        these files again, keeping the other results."""
        if self.scanning:
            return
        if not self.sources:
            messagebox.showinfo("Real-Hi-Res", tr("Add a folder or some files first."))
            return
        if self.listing:
            self.scan_when_listed = True
            self.status_var.set(tr("Still looking for audio files… the scan starts as soon as the list is complete."))
            return
        subset = files is not None
        files = list(files) if subset else self.all_files()
        self.last_scan_files = list(files)
        if not files:
            messagebox.showinfo("Real-Hi-Res", tr("The added folders/files contain no audio files.\n\n"
                                                  "Check the file extensions under Settings → Scanning."))
            return
        try:
            require_tools()
        except ToolMissingError as exc:
            messagebox.showerror(tr("FFmpeg not found"), tr_text(str(exc)))
            return
        if subset:
            for f in files:
                self.results.pop(f, None)
        else:
            self.results.clear()
            self.cache.clear()
        if self.scan_lyrics is not None:  # lyrics while scanning: a new scan starts a new worker
            self.scan_lyrics.stop()
            self.scan_lyrics = None
        self.lookup_gen += 1  # answers of lookups still running are no longer wanted
        self.total, self.done = len(files), 0
        self.scan_order, self.scan_ptr = [], 0  # set when the scanner says which files it analyses
        self.eta = None
        self.scan_workers = min(effective_jobs(self.cfg.jobs), len(files))
        self.scanning = True
        self._update_outdated_notice()
        self.refresh()
        self.show_details()
        self._update_source_counts()
        self.scan_started = time.time()
        self.progress.configure(mode="determinate", maximum=max(len(files), 1), value=0)
        self.status_var.set(tr("Scanning {n} files…", n=len(files)))
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        max_seconds = self.cfg.quick_scan_seconds if self.quick_var.get() else (self.cfg.max_seconds or None)
        self.scanner = Scanner(files, self.cfg, max_seconds, self.queue, force=force)
        self.scanner.start()

    def _mark_running(self) -> None:
        """Show which files are being analysed right now.  The pools hand out
        work in submission order, so these are the first unfinished files."""
        while self.scan_ptr < len(self.scan_order) and self.scan_order[self.scan_ptr] in self.results:
            self.scan_ptr += 1
        shown = 0
        i = self.scan_ptr
        while i < len(self.scan_order) and shown < self.scan_workers:
            path = self.scan_order[i]
            if path not in self.results:
                shown += 1
                if self.tree.exists(path) and PENDING in self.tree.item(path, "tags"):
                    self.tree.item(path, values=(tr(RUNNING_STYLE[0]), tr("Analysing…"), "", "", "", "",
                                                 os.path.basename(path), os.path.dirname(path)),
                                   tags=(RUNNING,))
            i += 1

    def stop_scan(self) -> None:
        if self.scanner:
            self.scanner.stop_event.set()
            self.status_var.set(tr("Stopping after the files already in progress…"))
            self.stop_btn.configure(state="disabled")
        if self.scan_lyrics is not None:
            self.scan_lyrics.stop()  # after the song it is looking up now

    def _lyrics_while_scanning(self, r: FileResult) -> None:
        """Settings → Library → Download lyrics while scanning: every song the scan
        answers goes to one lyrics worker (one LRCLIB request at a time)."""
        if not getattr(self.cfg, "lyrics_while_scanning", False):
            return
        if self.scan_lyrics is None:
            self.scan_lyrics = ScanLyrics(settings_mod.Settings.from_dict(self.cfg.to_dict()), self.queue)
            self.scan_lyrics.start()
        self.scan_lyrics.add(r.path, r)

    def _on_scan_lyrics(self, worker, outcome) -> None:
        if outcome.saved:
            self.lyrics_saved(outcome.path)
        if worker is self.scan_lyrics:  # not a worker of an earlier scan
            worker.count(outcome)
            self._update_status(final=not self.scanning, stopped=self._last_stopped)

    def _lyrics_tail(self) -> str:
        w = self.scan_lyrics
        return ("  ·  " + tr("Lyrics: {saved} saved, {not_found} not found", saved=w.saved, not_found=w.not_found)
                if w is not None else "")

    def _poll(self) -> None:
        dirty = False
        try:
            for _ in range(500):
                kind, payload, *rest = self.queue.get_nowait()
                if kind == "total":
                    self.total = payload
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=max(payload, 1), value=0)
                    if payload == 0:
                        self.status_var.set(tr("No audio files found in the selected sources."))
                elif kind == "cover":
                    self.cards.set_cover(payload, rest[0])
                elif kind == "listing":
                    self._on_listing(payload, rest[0])
                elif kind == "listed":
                    self._on_listed(payload, rest[0])
                elif kind == "saved":
                    dirty = self._on_saved(payload, rest[0]) or dirty
                elif kind == "saved_done":
                    self._on_saved_done(payload)
                elif kind == "graphs":
                    self._on_graphs(payload, rest[0])
                elif kind == "analysing":
                    self.scan_order, self.scan_ptr = list(payload), 0
                    self.eta = ScanEta(rest[0])
                    self.scan_workers = min(effective_jobs(self.cfg.jobs), len(payload))
                    self._mark_running()
                elif kind == "result":
                    self.done += 1
                    if self.eta is not None and payload.saved is None:
                        self.eta.add(payload.path)
                    self.cache.put(payload.path, payload.plot)
                    payload.plot = None
                    self.results[payload.path] = payload
                    self._insert(payload)
                    if payload.path in self.tree.selection():
                        self.show_details()  # the selected file just finished: show it live
                    self._lyrics_while_scanning(payload)
                    dirty = True
                elif kind == "scan_lyrics":
                    self._on_scan_lyrics(payload, rest[0])
                elif kind == "done":
                    self._finish(stopped=payload == "stopped")
                elif kind == "error":
                    self._finish(stopped=True)
                    messagebox.showerror(tr("Scan failed"), tr_text(payload))
        except queue.Empty:
            pass
        if dirty:
            self._schedule_regroup()
        if dirty and self.scanning:
            self.progress["value"] = self.done
            self._update_status()
            self._update_source_counts()
            self._mark_running()
        elif self.scanning and self.eta is not None and time.time() - self._status_time > 1.0:
            self._update_status()  # the time left keeps counting down between results
        self.after(100, self._poll)

    def _finish(self, stopped: bool) -> None:
        self.scanning = False
        self._last_stopped = stopped
        self.last_elapsed = time.time() - self.scan_started
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=max(self.total, 1), value=self.done)
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._update_status(final=True, stopped=stopped)
        self.refresh()
        self._update_source_counts()
        self._update_outdated_notice()
        kids = self.tree.get_children()
        if kids and not self.tree.selection():
            self.tree.selection_set(kids[0])
            self.tree.focus(kids[0])
        if self.results and self.cfg.show_summary_popup:
            self.after(150, self._summary_after_scan)
        if not stopped:
            self.after(400, self._fix_after_scan)

    def _fix_after_scan(self) -> None:
        """Settings → Fixing → After a scan: ask, or fix the scanned fake files."""
        from .fix import plan_fixes

        mode = self.cfg.fix_after_scan
        if mode == "Do nothing" or self.scanning:
            return
        scanned = [self.results[f] for f in self.last_scan_files if f in self.results]
        plans, skipped = plan_fixes(scanned, self.cfg)
        if not plans:
            return
        if mode.startswith("Ask"):
            text = tr("{n} fake file(s) can be fixed to the resolution they really have. Fix them now?", n=len(plans))
            if skipped:
                text += "\n\n" + tr("{n} other fake file(s) cannot be changed; the Fix window says why.",
                                     n=len(skipped))
            if not messagebox.askyesno(
                    tr("Fix to real resolution"),
                    text + "\n\n" + tr("(Settings → Fixing → After a scan)")):
                return
        FixDialog(self, [r for r in scanned if r.verdict in FIXABLE_VERDICTS], autostart=True)

    def _summary_after_scan(self) -> None:
        """The pop-up after a scan.  It is shown 150 ms later, so another scan may have
        started (and emptied the list) meanwhile: then it is skipped silently, rather
        than showing "Nothing scanned yet", a modal box that blocked the window."""
        if not self.scanning and self.results:
            SummaryDialog(self, list(self.results.values()), self.last_elapsed)

    def show_summary(self) -> None:
        if not self.results:
            messagebox.showinfo(tr("Scan summary"), tr("Nothing scanned yet."))
            return
        SummaryDialog(self, list(self.results.values()), self.last_elapsed)

    def _counts(self) -> Dict[str, int]:
        c = {v: 0 for v in VERDICT_ORDER}
        for r in self.results.values():
            c[r.verdict] = c.get(r.verdict, 0) + 1
        return c

    def _update_filter_counts(self) -> Dict[str, int]:
        c = self._counts()
        for v, cb in self.filter_checks.items():
            cb.configure(text=f"{verdict_label(v)} ({c[v]})")
        return c

    def _update_status(self, final: bool = False, stopped: bool = False) -> None:
        c = self._update_filter_counts()
        fakes = c[FAKE] + c[FAKE_LOSSLESS]
        n = max(len(self.results), 1)
        tail = tr("{fake} fake ({pct} %), {suspicious} suspicious, {real} real hi-res", fake=fakes,
                  pct=f"{100 * fakes / n:.0f}", suspicious=c[SUSPECT], real=c[REAL])
        reused = sum(1 for r in self.results.values() if r.saved)
        if final:
            head = (tr("Stopped") if stopped else tr("Finished") if self.last_elapsed is not None
                    else tr("Saved results"))
            files = (tr("{head}: {n} files ({reused} unchanged, from saved results)", head=head, n=len(self.results),
                        reused=reused) if reused and self.last_elapsed is not None
                     else tr("{head}: {n} files", head=head, n=len(self.results)))
            self.status_var.set(f"{files}  ·  {tail}{self._lyrics_tail()}")
        else:
            busy = (tr("analysing {n} ({workers} at a time)", n=len(self.scan_order), workers=self.scan_workers)
                    if self.scan_order else tr("looking up saved results"))
            if self.eta is not None:
                busy = tr("{busy}, {time_left}", busy=busy, time_left=tr_text(self.eta.text()))
            self._status_time = time.time()
            busy = tr("Scanning: {done}/{total} files, {reused} from saved results, {busy}", done=self.done,
                      total=self.total, reused=reused, busy=busy)
            self.status_var.set(f"{busy}  ·  {tail}{self._lyrics_tail()}")

    # ---------- views: files / by album / by artist ----------------------
    def set_view(self, mode: str) -> None:
        self.view_var.set(mode)
        self.switch_view()

    def switch_view(self) -> None:
        mode = self.view_var.get()
        if mode != "Files" and self.group_filter:
            self.close_group(switch=False)
        grid_mode = mode != "Files"
        for w in self._table_widgets:
            (w.grid_remove if grid_mode else w.grid)()
        if grid_mode:
            self.cards.grid()
            self.grid_opts.pack(side="left")
            self.regroup()
        else:
            self.cards.grid_remove()
            self.grid_opts.pack_forget()
        if self.cfg.view_mode != mode:
            self.cfg.view_mode = mode
            self.save_settings()
        if self.find_open:
            self.update_find(reset=True)

    def _grid_option_changed(self) -> None:
        changed = False
        for name, var in (("card_label", self.card_label_var), ("group_sort", self.group_sort_var)):
            if getattr(self.cfg, name) != var.get():
                setattr(self.cfg, name, var.get())
                changed = True
        if changed:
            self.save_settings()
            self.regroup()

    def current_groups(self):
        from .library import group_results, sort_groups

        mode = "album" if self.view_var.get() == "By album" else "artist"
        groups = group_results(list(self.results.values()), mode)
        q = self.search_var.get().strip().lower()
        if q:
            groups = [g for g in groups if q in g.title.lower() or q in g.subtitle.lower()]
        return mode, sort_groups(groups, self.cfg.group_sort)

    def regroup(self) -> None:
        self._regroup_pending = False
        if self.view_var.get() == "Files":
            return
        mode, groups = self.current_groups()
        self.cards.set_groups(groups, mode)
        if self.find_open:
            self.update_find(reset=False)

    def _schedule_regroup(self) -> None:
        if not self._regroup_pending and self.view_var.get() != "Files":
            self._regroup_pending = True
            self.after(800, self.regroup)

    def request_covers(self, groups, mode: str) -> None:
        """Queue cover look-ups (in display order) for a background thread."""
        for g in groups:
            key = f"{mode}:{g.key}"
            if key not in self.cover_seen:
                self.cover_seen.add(key)
                self.cover_requests.put((key, list(g.cover_candidates)))
        if not getattr(self, "_cover_thread", None):
            self._cover_thread = threading.Thread(target=self._cover_worker, daemon=True)
            self._cover_thread.start()

    def _cover_worker(self) -> None:
        from .covers import cover_png

        while True:
            key, paths = self.cover_requests.get()
            try:
                png = cover_png(paths, CardGrid.COVER)
            except Exception:  # noqa: BLE001 - a broken cover must not stop the others
                png = None
            self.queue.put(("cover", key, png))

    def open_group(self, g, mode: str) -> None:
        """Clicking a card: list that album's/artist's songs (all verdicts)."""
        back = self.view_var.get()
        filters = {v: var.get() for v, var in self.show_vars.items()}
        title, subtitle = group_names(g, mode)
        label = (tr("Album: {title} — {artist}", title=title, artist=subtitle) if mode == "album"
                 else tr("Artist: {name}", name=title))
        label += "  " + tr("({n} of {total} fake/suspicious)", n=g.problems, total=g.total)
        self.group_filter = {"mode": mode, "key": g.key, "paths": set(g.paths), "back": back,
                             "filters": filters, "order": list(g.paths)}
        for var in self.show_vars.values():
            var.set(True)
        self.view_var.set("Files")
        self.switch_view()
        self.chip_label.configure(text=label)
        self.chip_back.configure(text="◀ " + (tr("Back to albums") if mode == "album" else tr("Back to artists")))
        self.chip.pack(side="left")
        self.sort_col, self.sort_rev = "track", False
        self.refresh()
        kids = self.tree.get_children()
        if kids:
            first = next((k for k in kids if self.results.get(k) and self.results[k].is_problem), kids[0])
            self.tree.selection_set(first)
            self.tree.focus(first)
            self.tree.see(first)

    def close_group(self, switch: bool = True) -> None:
        gf = self.group_filter
        if not gf:
            return
        self.group_filter = None
        self.chip.pack_forget()
        for v, on in gf["filters"].items():
            self.show_vars[v].set(on)
        if self.sort_col == "track":
            self.sort_col, self.sort_rev = "verdict", False
        if switch:
            self.view_var.set(gf["back"])
            self.switch_view()
        self.refresh()

    # ---------- find (Ctrl/Cmd+F) -----------------------------------------
    def open_find(self) -> None:
        self.find_open = True
        self.find_frame.grid()
        self.find_entry.focus_set()
        self.find_entry.select_range(0, "end")
        self.update_find(reset=True)

    def close_find(self) -> None:
        self.find_open = False
        self.find_frame.grid_remove()
        self.find_matches, self.find_index = [], -1
        self._apply_find_highlight()
        self.tree.focus_set()

    def _find_candidates(self) -> List[tuple]:
        """[(id, searchable text)] of what is currently shown."""
        if self.view_var.get() == "Files":
            out = []
            for iid in self.tree.get_children():
                vals = self.tree.item(iid, "values")
                r = self.results.get(iid)
                extra = " ".join((r.tags or {}).values()) if r else ""
                out.append((iid, " ".join(str(v) for v in vals) + " " + extra))
            return out
        return [(g.key, f"{g.title} {g.subtitle}") for g in self.cards.groups]

    def update_find(self, reset: bool = False) -> None:
        if not self.find_open:
            return
        q = self.find_var.get().strip().lower()
        prev = self.find_matches[self.find_index] if 0 <= self.find_index < len(self.find_matches) else None
        self.find_matches = [i for i, text in self._find_candidates() if q and q in text.lower()]
        if not self.find_matches:
            self.find_index = -1
        elif reset or prev not in self.find_matches:
            self.find_index = 0
        else:
            self.find_index = self.find_matches.index(prev)
        self._apply_find_highlight(jump=reset)

    def find_step(self, delta: int) -> str:
        if not self.find_open:
            self.open_find()
            return "break"
        if self.find_matches:
            self.find_index = (self.find_index + delta) % len(self.find_matches)
            self._apply_find_highlight(jump=True)
        return "break"

    def _apply_find_highlight(self, jump: bool = False) -> None:
        q = self.find_var.get().strip()
        n = len(self.find_matches)
        if self.find_open:
            self.find_count.configure(
                text=(tr("{n} of {total}", n=self.find_index + 1, total=n) if n else (tr("No matches") if q else "")),
                foreground="#555" if n or not q else "#c62828")
        current = self.find_matches[self.find_index] if 0 <= self.find_index < n else None
        if self.view_var.get() == "Files":
            found = set(self.find_matches)
            for iid in self.tree.get_children():
                tags = tuple(t for t in self.tree.item(iid, "tags") if t not in ("found", "found_cur"))
                if iid == current:
                    tags += ("found_cur",)
                elif iid in found:
                    tags += ("found",)
                self.tree.item(iid, tags=tags)
            if current and jump:
                self.tree.see(current)
                self.tree.selection_set(current)
                self.tree.focus(current)
        else:
            self.cards.highlight(set(self.find_matches), current if jump or current else None)

    # ---------- table ----------------------------------------------------
    def set_filters(self, verdicts) -> None:
        for v, var in self.show_vars.items():
            var.set(v in verdicts)
        self.refresh()

    def _in_group(self, path: str) -> bool:
        return self.group_filter is None or path in self.group_filter["paths"]

    def _search_changed(self) -> None:
        """Filter once typing pauses: refreshing a big list on every key
        (and again on every deleted key) made the window stutter."""
        if self._search_job is not None:
            self.after_cancel(self._search_job)
        self._search_job = self.after(SEARCH_DELAY_MS, self._run_search)

    def _run_search(self) -> None:
        self.refresh()  # refresh() also clears the pending search job

    def _filter_state(self):
        """The verdict boxes and search text, read once (each Tk variable read
        is a call into Tcl, far too slow to repeat for every row)."""
        shown = {v for v, var in self.show_vars.items() if var.get()}
        return shown, self.search_var.get().strip().lower()

    def _matches(self, r: FileResult, state=None) -> bool:
        if not self._in_group(r.path):
            return False
        shown, q = state or self._filter_state()
        if r.verdict not in shown and r.verdict in self.show_vars:
            return False
        return (not q or q in r.path.lower() or q in r.summary.lower()
                or (i18n.language() != "en" and q in self._values(r)[1].lower()))  # the "Why" as shown

    def _values(self, r: FileResult):
        cached = self._values_cache.get(r.path)
        if cached is not None and cached[0] is r:
            return cached[1]
        bw = f"{r.bandwidth_hz / 1000:.1f} kHz" if r.bandwidth_hz else "-"
        values = (verdict_label(r.verdict), tr_text(r.summary), r.claimed, tr_text(r.estimated), bw,
                  tr_text(fix_outlook(r, self.cfg)), os.path.basename(r.path), os.path.dirname(r.path))
        self._values_cache[r.path] = (r, values)
        return values

    def _add_row(self, iid: str, values: tuple, tag: str) -> None:
        """Treeview.insert, without ttk's slow Python-side option formatting:
        tuples go to Tcl as lists directly."""
        self.tree.tk.call(self.tree._w, "insert", "", "end", "-id", iid, "-values", values, "-tags", (tag,))

    def _pending_values(self, path: str):
        why = tr("Waiting in the scan queue…") if self.scanning else tr("Not scanned yet: press Scan")
        return (tr(PENDING_STYLE[0]), why, "", "", "", "", os.path.basename(path), os.path.dirname(path))

    def _search_ok(self, path: str, summary: str = "") -> bool:
        q = self.search_var.get().strip().lower()
        return not q or q in path.lower() or q in summary.lower()

    def _insert(self, r: FileResult) -> None:
        """Show a new result: replace its "not scanned" row, or hide it when
        the current filters exclude its verdict."""
        exists = self.tree.exists(r.path)
        if self._matches(r) or (self.scanning and exists):
            # while scanning every row stays visible and updates in place;
            # the verdict filters are applied when the scan has finished
            if exists:
                self.tree.item(r.path, values=self._values(r), tags=(r.verdict,))
            else:
                self.tree.insert("", "end", iid=r.path, values=self._values(r), tags=(r.verdict,))
        elif exists:
            was_selected = r.path in self.tree.selection()
            self.tree.delete(r.path)
            if was_selected:
                self.show_details()

    def _sort_key(self, r: FileResult):
        c = self.sort_col
        if c == "verdict":
            return (VERDICT_ORDER.index(r.verdict), r.path.lower())
        if c == "bandwidth":
            return (r.bandwidth_hz or 0.0, r.path.lower())
        if c == "fix":
            return (self._values(r)[5].lower(), r.path.lower())  # as shown
        if c == "claimed":
            return (r.sample_rate, r.claimed_bits or 0, r.path.lower())
        if c == "estimated":
            return (r.true_rate or 0, r.effective_bits or 0, r.path.lower())
        if c == "name":
            return os.path.basename(r.path).lower()
        if c == "why":
            return (self._values(r)[1].lower(), r.path.lower())  # as shown
        if c == "track" and self.group_filter:
            order = self.group_filter["order"]
            return order.index(r.path) if r.path in order else len(order)
        return r.path.lower()

    def sort_by(self, col: str) -> None:
        self.sort_rev = (not self.sort_rev) if col == self.sort_col else False
        self.sort_col = col
        self.refresh()

    def refresh(self) -> None:
        if self._search_job is not None:  # a refresh now covers the pending search
            self.after_cancel(self._search_job)
            self._search_job = None
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        state = self._filter_state()
        rows = sorted((r for r in self.results.values() if self._matches(r, state)),
                      key=self._sort_key, reverse=self.sort_rev)
        shown = set()
        for r in rows:
            self._add_row(r.path, self._values(r), r.verdict)
            shown.add(r.path)
        # Listed files that have not been scanned yet are always shown, so the
        # user can see what was added.
        q = state[1]
        pending = None
        for path in self.all_files():
            if (path not in self.results and path not in shown and (not q or q in path.lower())
                    and self._in_group(path)):
                pending = pending or self._pending_values(path)[:2]
                self._add_row(path, pending + ("", "", "", "", os.path.basename(path), os.path.dirname(path)),
                              PENDING)
                shown.add(path)
        keep = [i for i in selected if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        for c in self.COLUMNS:
            arrow = (" ▼" if self.sort_rev else " ▲") if c == self.sort_col else ""
            self.tree.heading(c, text=tr(self.HEADINGS[c]) + arrow)
        if self.results:
            self._update_status(final=not self.scanning)
        if self.view_var.get() != "Files":
            self._schedule_regroup()
        elif self.find_open:
            self.update_find(reset=False)

    # ---------- dashboard ------------------------------------------------
    def _current(self) -> Optional[FileResult]:
        sel = self.tree.selection()
        return self.results.get(sel[0]) if sel else None

    def _current_path(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _set_text(self, parts) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for chunk in parts:
            self.text.insert("end", chunk)
        self.text.configure(state="disabled")

    def show_details(self) -> None:
        r = self._current()
        path = self._current_path()
        state = "normal" if path else "disabled"
        for b in (self.open_folder_btn, self.open_file_btn, self.copy_btn, self.lyrics_btn):
            b.configure(state=state)
        self._show_lyrics(path)
        from .fix import FIXABLE_VERDICTS
        fixable = any(self.results.get(i) is not None and self.results[i].verdict in FIXABLE_VERDICTS
                      for i in self.tree.selection())
        self.fix_btn.configure(state="normal" if fixable and not self.scanning else "disabled")
        if r is None and path:  # listed but not scanned yet
            self.badge.configure(text=tr(PENDING_STYLE[0]).upper(), bg=PENDING_STYLE[1])
            self.name_var.set(os.path.basename(path))
            self.why_var.set(tr("Waiting in the scan queue…") if self.scanning else
                             tr("This file will be analysed when you press Scan."))
            for v in self.fact_vars.values():
                v.set("–")
            self._set_text([path + "\n"])
            self.spectrogram.show(None, None)
            self.spectrum.show(None, None)
            return
        if r is None:
            self.badge.configure(text="", bg="#9e9e9e")
            self.name_var.set(tr("Select a file in the list"))
            self.why_var.set("")
            for v in self.fact_vars.values():
                v.set("–")
            self._set_text([])
            self.spectrogram.show(None, None)
            self.spectrum.show(None, None)
            return
        color = VERDICT_STYLE[r.verdict][1]
        self.badge.configure(text=verdict_label(r.verdict).upper(), bg=color)
        self.name_var.set(os.path.basename(r.path))
        self.why_var.set(tr_text(r.summary or (r.reasons[0] if r.reasons else "")))
        bits = r.effective_bits
        if bits is not None and r.is_float and bits > 24:
            bits = 24
        self.fact_vars["Claimed"].set(r.claimed)
        self.fact_vars["Real (estimated)"].set(tr_text(r.estimated))
        self.fact_vars["Bandwidth"].set(f"{r.bandwidth_hz / 1000:.1f} kHz" if r.bandwidth_hz else "–")
        self.fact_vars["Bits used"].set(tr("{bits} of {claimed}", bits=bits, claimed=r.claimed_bits)
                                        if bits and r.claimed_bits else (str(bits) if bits else "–"))
        self.fact_vars["Format"].set(tr("{codec}, {channels} ch", codec=r.codec, channels=r.channels)
                                     if r.codec else "–")
        self.fact_vars["Duration"].set(fmt_duration(r.duration) if r.duration else "?")
        self.fact_vars["After fix"].set(tr_text(fix_outlook(r, self.cfg)) or "–")
        # the reasons, notes and advice come finished from the shared core: tr_text() translates them
        parts = [f"• {tr_text(x)}\n" for x in r.reasons] + [f"• {tr_text(x)}\n" for x in r.notes]
        advice = fix_advice(r, self.cfg)  # what Fix does with it, and whether it stays fake afterwards
        if advice:
            parts.append(f"• {tr_text(advice)}\n")
        if r.saved:
            parts.insert(0, f"• {tr_text(saved_note(r))}\n")
        if r.cliffs_db:
            deep = r.deep_cliffs_db or {}
            parts.append("• " + tr("Drop at source Nyquists (narrow/deep): {values}", values=", ".join(
                f"{k / 2000:g} kHz {v:.0f}/{deep.get(k, float('nan')):.0f} dB"
                for k, v in r.cliffs_db.items() if v == v)) + "\n")
        if r.converter_floor_db is not None and r.converter_floor_db == r.converter_floor_db:
            parts.append("• " + tr("26-40 kHz band: {db} dB relative to the rounding noise",
                                   db=f"{r.converter_floor_db:+.0f}") + "\n")
        if r.grid_step:
            parts.append("• " + tr("Value grid: step {step} (24-bit LSBs), {bits}-bit master, gain {gain} dB",
                                   step=f"{r.grid_step:.1f}", bits=r.grid_bits, gain=f"{r.grid_gain_db:+.1f}") + "\n")
        parts.append(f"\n{r.path}\n")
        self._set_text(parts)
        plot = self.cache.get(r.path)
        if plot is None and r.saved and self.store is not None:
            plot = self.store.get_plot(r.path, settings_key(self.cfg))
            if plot:
                self.cache.put(r.path, plot)
        waiting = plot is None and r.verdict != ERROR and self._want_graphs(r)
        self.spectrogram.show(r, plot, waiting)
        self.spectrum.show(r, plot, waiting)

    def select_all(self, event=None):
        """Ctrl/Cmd+A: select every file in the list, as it is filtered now, so that
        e.g. "Fake lossless + Fake hi-res, Ctrl+A, Fix" fixes all of them."""
        if event is not None:
            try:
                w = self.root.focus_get()
            except (KeyError, tk.TclError):  # focus in a combobox pop-down
                w = None
            if w is not None and w.winfo_toplevel() is not self.root:
                return None  # another window (Settings, Fix...) has the keyboard
            if isinstance(w, (tk.Entry, tk.Text, tk.Spinbox, ttk.Entry, ttk.Spinbox)):
                return None  # text fields keep their own select-all
            if w is self.src_tree:
                self.src_tree.selection_set(self.src_tree.get_children())
                return "break"
        if self.view_var.get() != "Files":
            self.status_var.set(tr("Open an album or artist, or switch to the Files view, to select files."))
            return "break"
        items = self.tree.get_children()
        if items:
            self.tree.selection_set(items)
            self.tree.focus_set()
            self.status_var.set(tr("Selected {n} listed file(s).", n=len(items)))
        return "break"

    def _show_lyrics(self, path: Optional[str]) -> None:
        """The Lyrics tab: the .lrc (or .txt) next to the selected song, timestamps in grey."""
        t = self.lyrics_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        found = lyrics_mod.read_lyrics_file(path) if path else None
        if path and found is None:
            acc = "Cmd" if sys.platform == "darwin" else "Ctrl"
            t.insert("end", tr("No lyrics next to this song yet.") + "\n\n", "muted")
            t.insert("end", tr("Get lyrics… ({key}) looks the song up on LRCLIB and saves its synced lyrics "
                               "as {file} next to it.", key=f"{acc}+L",
                               file=os.path.basename(lyrics_mod.sidecar(path, '.lrc'))), "muted")
        elif found is not None:
            kind, text, file = found
            head = {lyrics_mod.SYNCED: tr("Synced lyrics"), lyrics_mod.PLAIN: tr("Plain lyrics (no timing)"),
                    lyrics_mod.INSTRUMENTAL: tr("Instrumental")}[kind]
            t.insert("end", f"{head} · {os.path.basename(file)}\n\n", "muted")
            if kind == lyrics_mod.INSTRUMENTAL:
                t.insert("end", tr("LRCLIB lists this song as instrumental: it has no lyrics."), "muted")
            for line in ([] if kind == lyrics_mod.INSTRUMENTAL else text.splitlines()):
                m = re.match(r"^((?:\s*\[[^\]]*\])+)\s?(.*)$", line) if kind == lyrics_mod.SYNCED else None
                if m and re.match(r"\s*\[\d", m.group(1)):
                    t.insert("end", m.group(1).strip() + "  ", "time")
                    t.insert("end", m.group(2) + "\n")
                else:  # plain text, or an LRC header such as [ar: ...]
                    t.insert("end", line + "\n", "muted" if m else ())
        t.configure(state="disabled")

    def lyrics_saved(self, path: str) -> None:
        """LyricsDialog saved lyrics for `path`: show them if that song is selected."""
        if self._current_path() == path:
            self._show_lyrics(path)

    def download_lyrics(self, paths: List[str]) -> None:
        d = self.lyrics_dialog
        if d is not None and d.winfo_exists():
            if d.running:
                d.lift()
                self.status_var.set(tr("Lyrics are being downloaded already; wait for them or press Stop first."))
                return
            d.destroy()
        self.lyrics_dialog = LyricsDialog(self, paths)

    def lyrics_selected(self, event=None):
        """Ctrl/Cmd+L, the Download lyrics… button and the menus: the selected songs."""
        if event is not None:
            try:
                w = self.root.focus_get()
            except (KeyError, tk.TclError):
                w = None
            if w is not None and w.winfo_toplevel() is not self.root:
                return None  # another window has the keyboard
        paths = list(self.tree.selection()) if self.view_var.get() == "Files" else []
        if not paths:
            self.status_var.set(tr("Select one or more songs in the Files view (Ctrl/Cmd+A selects all listed), "
                                   "or use Edit → Download synced lyrics for all songs."))
            return "break"
        self.download_lyrics(paths)
        return "break"

    def lyrics_all(self) -> None:
        files = self.all_files()
        if not files:
            messagebox.showinfo(tr("Download synced lyrics"), tr("Add a folder or files first."))
            return
        self.download_lyrics(files)

    def fix_selected(self) -> None:
        if self.scanning:
            self.status_var.set(tr("Wait for the scan to finish before fixing files."))
            return
        rs = [self.results[i] for i in self.tree.selection() if i in self.results]
        if not rs:
            self.status_var.set(tr("Select one or more scanned fake files to fix."))
            return
        FixDialog(self, rs)

    def fix_all(self) -> None:
        from .fix import FIXABLE_VERDICTS

        if self.scanning:
            self.status_var.set(tr("Wait for the scan to finish before fixing files."))
            return
        rs = [r for r in self.results.values() if r.verdict in FIXABLE_VERDICTS]
        if not rs:
            messagebox.showinfo(tr("Fix to real resolution"), tr("No fake hi-res or fake lossless files to fix."))
            return
        FixDialog(self, rs)

    def add_fixed_result(self, outcome) -> None:
        """Show a freshly converted file (and drop a replaced original)."""
        if outcome.plan.replace_original and outcome.plan.source in self.results:
            del self.results[outcome.plan.source]
            if self.tree.exists(outcome.plan.source):
                self.tree.delete(outcome.plan.source)
        r = outcome.new_result
        if r is None:
            return
        if r.path in self.results and self.tree.exists(r.path):
            self.tree.delete(r.path)
        self.results[r.path] = r
        if self.store is not None:
            self.store.put(r, settings_key(self.cfg))
        self.refresh()
        self._update_source_counts()

    def _primary_action(self) -> None:
        (self.open_file if self.cfg.double_click == "Open file" else self.reveal)()

    def _secondary_action(self) -> None:
        (self.reveal if self.cfg.double_click == "Open file" else self.open_file)()

    def reveal(self) -> None:
        path = self._current_path()
        if path:
            try:
                reveal_in_file_manager(path)
            except OSError as exc:
                messagebox.showerror("Real-Hi-Res", tr("Could not open the folder: {error}", error=tr_text(str(exc))))

    def open_file(self) -> None:
        path = self._current_path()
        if path:
            try:
                open_with_default_app(path)
            except OSError as exc:
                messagebox.showerror("Real-Hi-Res", tr("Could not open the file: {error}", error=tr_text(str(exc))))

    def copy_path(self) -> None:
        paths = list(self.tree.selection())
        if paths:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(paths))
            self.status_var.set(tr("Copied {n} path(s) to the clipboard.", n=len(paths)))

    def _context_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    def export(self, kind: str, all_results: bool = False) -> None:
        if not self.results:
            messagebox.showinfo("Real-Hi-Res", tr("Nothing to export yet; run a scan first."))
            return
        path = filedialog.asksaveasfilename(
            title=tr("Export {format}", format=kind.upper()), defaultextension=f".{kind}",
            initialfile=f"realhires-report.{kind}", filetypes=[(kind.upper(), f"*.{kind}")])
        if not path:
            return
        visible = [self.results[i] for i in self.tree.get_children()]
        rows = list(self.results.values()) if all_results or not visible else visible
        (write_csv if kind == "csv" else write_json)(rows, path)
        self.status_var.set(tr("Exported {n} row(s) to {path}", n=len(rows), path=path))

    def about(self) -> None:
        messagebox.showinfo(
            tr("About {name}", name=APP_NAME),
            tr("{name} {version}: the Real-Hi-Res quality checker", name=APP_NAME, version=__version__) + "\n\n"
            + tr("Finds fake hi-res audio (upsampled or 16-bit padded) and fake lossless files (FLAC/WAV/ALAC "
                 "made from MP3/AAC) by analysing the audio itself.") + "\n\n"
            + f"{tr_text(lyrics_mod.ABOUT_CREDIT)}\n\n" + PROJECT_URL)

    def quit_app(self) -> None:
        if self.embedded and self.quit_callback is not None:
            self.quit_callback()  # the player saves its state, then calls shutdown()
            return
        self.shutdown()
        self.root.destroy()

    def shutdown(self) -> None:
        """Stop the background work and close the saved results (before the window goes)."""
        if self.scanner and self.scanning:
            self.scanner.stop_event.set()
        if self.lyrics_dialog is not None:
            self.lyrics_dialog.stop_event.set()
        if self.scan_lyrics is not None:
            self.scan_lyrics.stop()
        self.cache.close()
        self.lookup_requests.put((0, None, None, None))
        if self.graph_pool is not None:
            self.graph_pool.shutdown(wait=False, cancel_futures=True)
        if self.store is not None:
            self.store.close()


def main(argv=None) -> int:
    """The desktop app (2.0: the music player, with this app as its Quality check
    page).  Paths on the command line open the Quality check page and scan them."""
    from .player_gui import main as player_main

    return player_main(argv)


def scanner_main(argv=None) -> int:
    """The 1.x scanner on its own, in a window of its own."""
    multiprocessing.freeze_support()
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # e.g. started over SSH or on a server without a desktop
        print(f"Real-Hi-Res cannot open its window: {exc}\n"
              "The GUI needs a graphical desktop (X11, or Wayland with XWayland). "
              "On a server or over SSH use the command-line tool instead "
              "(realhires / realhires-cli).", file=sys.stderr)
        return 1
    cfg = settings_mod.load()
    i18n.set_language(cfg.language)
    root.title(tr("Real-Hi-Res {version} — fake hi-res & fake lossless detector", version=__version__))
    root.geometry("1280x820")
    root.minsize(900, 600)
    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png")
    try:
        root.iconphoto(True, tk.PhotoImage(file=icon))
    except (tk.TclError, OSError):
        pass  # icon is cosmetic only
    style = ttk.Style(root)
    if sys.platform.startswith("linux") and "clam" in style.theme_names():
        style.theme_use("clam")
        # clam paints the bar in the window colour, which makes it invisible.
        style.configure("Horizontal.TProgressbar", background="#4a90d9")
    style.configure("Treeview", rowheight=22)
    App(root, initial=[a for a in args if not a.startswith("-")], cfg=cfg)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
