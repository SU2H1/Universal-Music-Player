"""Canvas widgets of the 2.0 player: the song tables (`SongList`, drawn row by row
for the visible part only, so a library of tens of thousands of songs scrolls
smoothly), the album card grid (`AlbumGrid`) and the cover cache (`CoverCache`,
covers made by FFmpeg in a background thread, with rounded corners)."""

from __future__ import annotations

import base64
import collections
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .i18n import tr, tr_name
from .player_theme import Theme, draw_icon, fit_text, round_rect


def fmt_time(seconds: float) -> str:
    """'3:07', '1:02:03'."""
    s = max(0, int(round(seconds or 0)))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def scroll_units(event) -> int:
    """Mouse-wheel steps of an event (Windows/macOS delta, X11 buttons 4 and 5)."""
    if getattr(event, "num", None) == 4:
        return -1
    if getattr(event, "num", None) == 5:
        return 1
    d = getattr(event, "delta", 0)
    if not d:
        return 0
    return -1 if d > 0 else 1


def show_scrollbar(sb: ttk.Scrollbar, need: bool, bg_role: str = "bg") -> None:
    """A scroll bar only shows when there is something to scroll (it keeps its place,
    so the layout never jumps: it is just drawn in the background colour)."""
    style = "P.Vertical.TScrollbar" if need else ("PHide.Vertical.TScrollbar" if bg_role == "bg"
                                                  else "PHideSide.Vertical.TScrollbar")
    if str(sb.cget("style")) != style:
        sb.configure(style=style)


def bind_wheel(widget: tk.Widget, fn: Callable[[int], None]) -> None:
    widget.bind("<MouseWheel>", lambda e: fn(scroll_units(e)))
    widget.bind("<Button-4>", lambda e: fn(-1))
    widget.bind("<Button-5>", lambda e: fn(1))


@dataclass
class Column:
    key: str
    title: str              # English, marked with N_(); the heading shows tr(title)
    width: int
    stretch: bool = False
    anchor: str = "w"
    kind: str = "text"      # text, heart, num (track number / playing mark), two (title + subtitle)
    key2: str = ""


class SongList(tk.Frame):
    """A table of songs drawn on a Canvas.  `value(row, key)` gives a cell's text;
    `sort_value(row, key)` what a click on the column heading sorts by."""

    HEADER = 30

    def __init__(self, master, theme: Theme, columns: Sequence[Column], value: Callable[[Any, str], str],
                 sort_value: Optional[Callable[[Any, str], Any]] = None, row_height: int = 32,
                 header: bool = True, zebra: bool = True, on_activate: Optional[Callable[[int], None]] = None,
                 on_heart: Optional[Callable[[Any], None]] = None, on_select: Optional[Callable[[int], None]] = None,
                 is_liked: Optional[Callable[[Any], bool]] = None, is_current: Optional[Callable[[Any], bool]] = None,
                 bg_role: str = "bg", sortable: bool = True, empty_text: str = "",
                 on_context: Optional[Callable[[int, Any], None]] = None,
                 is_dim: Optional[Callable[[Any], bool]] = None):
        super().__init__(master, bd=0, highlightthickness=0)
        self.on_context, self.is_dim = on_context, is_dim
        self.theme, self.columns, self.value, self.sort_value = theme, list(columns), value, sort_value
        self.rh, self.has_header, self.zebra = row_height, header, zebra
        self.on_activate, self.on_heart, self.on_select = on_activate, on_heart, on_select
        self.is_liked, self.is_current = is_liked, is_current
        self.bg_role, self.sortable, self.empty_text = bg_role, sortable and header, empty_text
        self.rows: List[Any] = []
        self.footer = ""                    # a grey line under the last row ("4 songs, 17 minutes")
        self.top = 0.0                      # pixels scrolled
        self.selected: Optional[int] = None
        self.hover: Optional[int] = None
        self.sort_key: Optional[str] = None
        self.sort_rev = False
        self._fit: Dict[tuple, str] = {}
        theme.reg(self, bg=bg_role)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, takefocus=1, height=120, width=200)
        theme.reg(self.canvas, bg=bg_role)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self._yview, style="P.Vertical.TScrollbar")
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self._sb_shown = True
        c = self.canvas
        c.bind("<Configure>", lambda e: self.redraw())
        c.bind("<Motion>", self._motion)
        c.bind("<Leave>", lambda e: self._set_hover(None))
        c.bind("<Button-1>", self._click)
        c.bind("<Double-Button-1>", self._double)
        c.bind("<Return>", lambda e: self._activate(self.selected))
        c.bind("<KP_Enter>", lambda e: self._activate(self.selected))
        c.bind("<Up>", lambda e: self._move(-1))
        c.bind("<Down>", lambda e: self._move(1))
        c.bind("<Prior>", lambda e: self._move(-self._page_rows()))
        c.bind("<Next>", lambda e: self._move(self._page_rows()))
        bind_wheel(c, lambda n: self.scroll(n * 3 * self.rh))
        c.bind("<Button-3>", self._context)
        c.bind("<Button-2>", self._context)  # macOS right click
        theme.on_change(lambda: (self._fit.clear(), self.redraw()), self)  # a new font measures differently

    # ------------------------------------------------------------- data ---
    def set_rows(self, rows: Sequence[Any], keep: bool = False) -> None:
        sel = self.rows[self.selected] if keep and self.selected is not None and self.selected < len(self.rows) \
            else None
        self.rows = list(rows)
        if self.sort_key is not None and self.sort_value is not None:
            self._sort()
        self.selected = self.rows.index(sel) if sel is not None and sel in self.rows else None
        if not keep:
            self.top = 0.0
        self.redraw()

    def _sort(self) -> None:
        key = self.sort_key
        self.rows.sort(key=lambda r: self.sort_value(r, key), reverse=self.sort_rev)  # type: ignore[misc]

    def sort_by(self, key: str) -> None:
        if self.sort_value is None:
            return
        if self.sort_key == key:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_key, self.sort_rev = key, False
        sel = self.rows[self.selected] if self.selected is not None else None
        self._sort()
        self.selected = self.rows.index(sel) if sel is not None else None
        self.redraw()

    # ----------------------------------------------------------- layout ---
    def _layout(self, width: int) -> List[Tuple[Column, int, int]]:
        pad = 16
        fixed = sum(c.width for c in self.columns if not c.stretch)
        stretch = [c for c in self.columns if c.stretch]
        room = max(0, width - 2 * pad - fixed - 8 * len(self.columns))
        total = sum(c.width for c in stretch) or 1
        out, x = [], pad
        for c in self.columns:
            w = int(room * c.width / total) if c.stretch else c.width
            w = max(w, 30 if c.stretch else c.width)
            out.append((c, x, w))
            x += w + 8
        return out

    def _body_height(self) -> int:
        return max(1, self.canvas.winfo_height() - (self.HEADER if self.has_header else 0))

    def _content_height(self) -> int:
        return len(self.rows) * self.rh + (44 if self.footer else 0)

    def _max_top(self) -> float:
        return max(0.0, self._content_height() - self._body_height() + 8)

    def _page_rows(self) -> int:
        return max(1, self._body_height() // self.rh - 1)

    def scroll(self, pixels: float) -> None:
        self.top = min(max(self.top + pixels, 0.0), self._max_top())
        self.redraw()

    def _yview(self, *args) -> None:
        total = max(1, self._content_height())
        if args[0] == "moveto":
            self.top = float(args[1]) * total
        elif args[0] == "scroll":
            n = int(args[1])
            self.top += n * (self._page_rows() * self.rh if args[2] == "pages" else 3 * self.rh)
        self.top = min(max(self.top, 0.0), self._max_top())
        self.redraw()

    def see(self, i: int) -> None:
        y = i * self.rh
        if y < self.top:
            self.top = y
        elif y + self.rh > self.top + self._body_height():
            self.top = y + self.rh - self._body_height()
        self.top = min(max(self.top, 0.0), self._max_top())
        self.redraw()

    def _row_at(self, y: int) -> Optional[int]:
        y -= self.HEADER if self.has_header else 0
        if y < 0:
            return None
        i = int((y + self.top) // self.rh)
        return i if 0 <= i < len(self.rows) else None

    # ----------------------------------------------------------- events ---
    def _motion(self, e) -> None:
        self._set_hover(self._row_at(e.y))

    def _set_hover(self, i: Optional[int]) -> None:
        if i != self.hover:
            self.hover = i
            self.redraw()

    def _column_at(self, x: int) -> Optional[Column]:
        for col, cx, w in self._layout(self.canvas.winfo_width()):
            if cx - 4 <= x <= cx + w + 4:
                return col
        return None

    def _click(self, e) -> None:
        self.canvas.focus_set()
        if self.has_header and e.y < self.HEADER:
            col = self._column_at(e.x)
            if col is not None and self.sortable and col.kind != "heart":
                self.sort_by(col.key)
            return
        i = self._row_at(e.y)
        if i is None:
            return
        col = self._column_at(e.x)
        if col is not None and col.kind == "heart" and self.on_heart is not None:
            self.on_heart(self.rows[i])
            self.redraw()
            return
        self.select(i)

    def select(self, i: Optional[int]) -> None:
        self.selected = i
        if i is not None:
            self.see(i)
        self.redraw()
        if i is not None and self.on_select is not None:
            self.on_select(i)

    def _context(self, e) -> None:
        """Right click: select the row and ask for its menu (on_context(index, event))."""
        i = self._row_at(e.y)
        if i is None or self.on_context is None:
            return
        self.select(i)
        self.on_context(i, e)

    def _double(self, e) -> None:
        i = self._row_at(e.y)
        col = self._column_at(e.x)
        if i is not None and not (col is not None and col.kind == "heart"):
            self._activate(i)

    def _activate(self, i: Optional[int]) -> None:
        if i is not None and self.on_activate is not None and 0 <= i < len(self.rows):
            self.on_activate(i)

    def _move(self, d: int) -> str:
        if not self.rows:
            return "break"
        i = 0 if self.selected is None else min(max(self.selected + d, 0), len(self.rows) - 1)
        self.select(i)
        return "break"

    # ---------------------------------------------------------- drawing ---
    def redraw(self) -> None:
        t, c = self.theme, self.canvas
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if W <= 1:
            return
        cols = self._layout(W)
        head = self.HEADER if self.has_header else 0
        self.top = min(max(self.top, 0.0), self._max_top())
        first = int(self.top // self.rh)
        last = min(len(self.rows), int((self.top + H) // self.rh) + 2)
        body, small = t.f("body"), t.f("small")
        for i in range(first, last):
            row = self.rows[i]
            y = head + i * self.rh - self.top
            sel = i == self.selected
            current = self.is_current(row) if self.is_current else False
            if sel:
                round_rect(c, 6, y + 1, W - 6, y + self.rh - 1, 6, fill=t.c("accent"), outline="")
            elif i == self.hover:
                round_rect(c, 6, y + 1, W - 6, y + self.rh - 1, 6, fill=t.c("hover"), outline="")
            elif self.zebra and i % 2 == 1:
                round_rect(c, 6, y + 1, W - 6, y + self.rh - 1, 6, fill=t.c("zebra"), outline="")
            dim = self.is_dim(row) if self.is_dim else False
            fg = t.c("on_accent") if sel else (t.c("accent") if current else t.c("disabled" if dim else "text"))
            fg2 = t.c("on_accent") if sel else t.c("disabled" if dim else "text2")
            mid = y + self.rh / 2
            for col, x, w in cols:
                if col.kind == "heart":
                    liked = self.is_liked(row) if self.is_liked else False
                    if liked or i == self.hover:
                        color = t.c("on_accent") if sel else (t.c("accent") if liked else t.c("text2"))
                        draw_icon(c, "heart", x + w / 2, mid, 13, color, filled=liked)
                    continue
                if col.kind == "num" and current:
                    draw_icon(c, "speaker", x + w / 2, mid, 13, t.c("on_accent") if sel else t.c("accent"))
                    continue
                if col.kind == "icon":  # value() names the icon
                    draw_icon(c, self.value(row, col.key), x + w / 2, mid, 16, t.c("on_accent") if sel
                              else t.c("accent"))
                    continue
                if col.kind == "two":
                    c.create_text(x, mid - 8, text=fit_text(t.f("body_b"), self.value(row, col.key), w, self._fit),
                                  anchor="w", fill=fg, font=t.f("body_b"))
                    c.create_text(x, mid + 9, text=fit_text(small, self.value(row, col.key2), w, self._fit),
                                  anchor="w", fill=fg2, font=small)
                    continue
                text = fit_text(body, self.value(row, col.key), w, self._fit)
                color = fg if col.kind != "num" and col.key in ("title", "name") else (fg if sel or current else fg2)
                if col.anchor == "e":
                    c.create_text(x + w, mid, text=text, anchor="e", fill=color, font=body)
                elif col.anchor == "center":
                    c.create_text(x + w / 2, mid, text=text, anchor="center", fill=color, font=body)
                else:
                    c.create_text(x, mid, text=text, anchor="w", fill=color, font=body)
        if not self.rows and self.empty_text:
            c.create_text(W / 2, head + 40, text=self.empty_text, fill=t.c("text2"), font=body, width=W - 40)
        if self.rows and self.footer:
            c.create_text(cols[0][1] if cols else 16, head + len(self.rows) * self.rh - self.top + 24,
                          text=self.footer, anchor="w", fill=t.c("text2"), font=small)
        if self.has_header:
            c.create_rectangle(0, 0, W, head, fill=t.c(self.bg_role), outline="")
            for col, x, w in cols:
                if not col.title:
                    continue
                anchor = {"e": "e", "center": "center"}.get(col.anchor, "w")
                xx = x + w if anchor == "e" else x + w / 2 if anchor == "center" else x
                item = c.create_text(xx, head / 2, text=tr(col.title), anchor=anchor, fill=t.c("text2"),
                                     font=t.f("small_b"))
                if self.sort_key == col.key:  # a small triangle: down = A to Z, up = Z to A
                    bx = c.bbox(item)[2] + 8 if anchor == "w" else c.bbox(item)[0] - 8
                    d = -3 if self.sort_rev else 3
                    c.create_polygon(bx - 4, head / 2 - d, bx + 4, head / 2 - d, bx, head / 2 + d,
                                     fill=t.c("text2"), outline="")
            c.create_line(12, head - 1, W - 12, head - 1, fill=t.c("sep"))
        total = max(1, self._content_height())
        body_h = self._body_height()
        if total <= body_h:
            self.sb.set(0.0, 1.0)
        else:
            self.sb.set(self.top / total, min(1.0, (self.top + body_h) / total))
        show_scrollbar(self.sb, total > body_h, self.bg_role)


class CoverCache:
    """Album covers as PhotoImages with rounded corners, made by FFmpeg
    (covers.cover_png) in one background thread, newest requests first."""

    MAX_IMAGES = 600
    MAX_PNG = 4000

    def __init__(self, root: tk.Misc):
        self.root = root
        self.images: "collections.OrderedDict[tuple, tk.PhotoImage]" = collections.OrderedDict()
        self.png: "collections.OrderedDict[tuple, Optional[bytes]]" = collections.OrderedDict()
        self.listeners: List[Callable[[str, int], None]] = []
        self.requests: "queue.LifoQueue" = queue.LifoQueue()
        self.pending: set = set()
        self.done: queue.Queue = queue.Queue()
        self.alive = True
        threading.Thread(target=self._work, daemon=True, name="realhires-covers").start()
        self.root.after(60, self._poll)

    def close(self) -> None:
        self.alive = False
        self.requests.put(None)

    def missing(self, key: str, size: int) -> bool:
        return (key, size) in self.png and self.png[(key, size)] is None

    def get(self, key: str, paths: Sequence[str], size: int) -> Optional[tk.PhotoImage]:
        k = (key, size)
        img = self.images.get(k)
        if img is not None:
            self.images.move_to_end(k)
            return img
        if k in self.png:
            data = self.png[k]
            if data is None:
                return None
            img = self._image(data, size)
            if img is not None:
                self._keep(k, img)
            return img
        if k not in self.pending:
            self.pending.add(k)
            self.requests.put((k, list(paths)[:40]))
        return None

    def _keep(self, k: tuple, img: tk.PhotoImage) -> None:
        self.images[k] = img
        while len(self.images) > self.MAX_IMAGES:
            self.images.popitem(last=False)

    def _image(self, data: bytes, size: int) -> Optional[tk.PhotoImage]:
        try:
            img = tk.PhotoImage(master=self.root, data=base64.b64encode(data).decode("ascii"), format="png")
        except tk.TclError:
            return None
        w, h = img.width(), img.height()
        r = max(3, int(round(min(w, h) * (0.06 if size <= 64 else 0.045))))
        call, name = img.tk.call, img.name
        for y in range(r):
            for x in range(r):
                dx, dy = r - x - 0.5, r - y - 0.5
                if dx * dx + dy * dy > r * r:
                    for px, py in ((x, y), (w - 1 - x, y), (x, h - 1 - y), (w - 1 - x, h - 1 - y)):
                        call(name, "transparency", "set", px, py, 1)
        return img

    def _work(self) -> None:
        from .covers import cover_png

        while self.alive:
            item = self.requests.get()
            if item is None:
                break
            k, paths = item
            try:
                data = cover_png(paths, k[1])
            except Exception:  # noqa: BLE001 - a broken cover must not stop the others
                data = None
            self.done.put((k, data))

    def _poll(self) -> None:
        changed = []
        try:
            while True:
                k, data = self.done.get_nowait()
                self.pending.discard(k)
                self.png[k] = data
                while len(self.png) > self.MAX_PNG:
                    self.png.popitem(last=False)
                if data is not None:
                    img = self._image(data, k[1])
                    if img is not None:
                        self._keep(k, img)
                changed.append(k)
        except queue.Empty:
            pass
        for key, size in changed:
            for fn in list(self.listeners):
                try:
                    fn(key, size)
                except tk.TclError:
                    pass
        if self.alive:
            try:
                self.root.after(60, self._poll)
            except tk.TclError:
                pass


def draw_cover(c: tk.Canvas, theme: Theme, img: Optional[tk.PhotoImage], x: float, y: float, size: int,
               tag: str = "") -> None:
    """The cover at (x, y), or a rounded placeholder with a note."""
    if img is not None:
        c.create_image(x, y, image=img, anchor="nw", tags=tag)
        return
    r = max(3, size * (0.06 if size <= 64 else 0.045))
    round_rect(c, x, y, x + size, y + size, r, fill=theme.c("ph"), outline="", tags=tag)
    draw_icon(c, "note", x + size / 2, y + size / 2, size * 0.36, theme.c("ph_icon"), tag=tag)


def draw_mosaic(c: tk.Canvas, theme: Theme, covers: "CoverCache", quarters: Sequence[tuple], x: float, y: float,
                size: int, tag: str = "") -> None:
    """A playlist picture: the covers of its first four songs' albums, 2 x 2
    (`quarters` = [(album key, [paths])]); fewer songs repeat the covers."""
    if not quarters:
        round_rect(c, x, y, x + size, y + size, max(3, size * 0.045), fill=theme.c("ph"), outline="", tags=tag)
        draw_icon(c, "playlist", x + size / 2, y + size / 2, size * 0.34, theme.c("ph_icon"), tag=tag)
        return
    if len(quarters) == 1:  # one album: its cover alone
        key, paths = quarters[0]
        draw_cover(c, theme, covers.get(key, paths, size), x, y, size, tag)
        return
    half = size // 2 - 1
    tiles = [quarters[i % len(quarters)] for i in range(4)]
    for i, (key, paths) in enumerate(tiles):
        dx, dy = (i % 2) * (half + 2), (i // 2) * (half + 2)
        draw_cover(c, theme, covers.get(key, paths, half), x + dx, y + dy, half, tag)


class AlbumGrid(tk.Frame):
    """Album cards: the square cover, the title (bold) and the artist (grey).
    An item can instead be a playlist ("mosaic": up to four (album key, paths)) or
    the "New Playlist…" card ("new": True)."""

    COVER = 176
    GAP = 24
    MARGIN = 28

    def __init__(self, master, theme: Theme, covers: CoverCache, on_open: Callable[[dict], None],
                 empty_text: str = ""):
        super().__init__(master, bd=0, highlightthickness=0)
        self.theme, self.covers, self.on_open, self.empty_text = theme, covers, on_open, empty_text
        self.albums: List[dict] = []
        self.positions: Dict[str, Tuple[float, float, int]] = {}
        self.hover: Optional[int] = None
        self._fit: Dict[tuple, str] = {}
        theme.reg(self, bg="bg")
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, height=200, width=200)
        theme.reg(self.canvas, bg="bg")
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self._yview, style="P.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self._sbset)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self._sb_shown = True
        self.canvas.bind("<Configure>", lambda e: self.relayout())
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._motion)
        bind_wheel(self.canvas, lambda n: self._yview("scroll", n, "units"))
        covers.listeners.append(self._cover_ready)
        theme.on_change(lambda: (self._fit.clear(), self.relayout()), self)
        self._width = 0

    def _sbset(self, a, b) -> None:
        self.sb.set(a, b)
        show_scrollbar(self.sb, float(a) > 0.0 or float(b) < 1.0)

    def _yview(self, *args) -> None:
        self.canvas.yview(*args)
        self._request_visible()

    def set_albums(self, albums: List[dict], keep: bool = False) -> None:
        self.albums = list(albums)
        if not keep:
            self.canvas.yview_moveto(0)
        self.relayout()

    def _geometry(self, width: int) -> Tuple[int, float]:
        cs, m = self.COVER, self.MARGIN
        cols = max(1, int((width - 2 * m + self.GAP) // (cs + self.GAP)))
        gap = (width - 2 * m - cols * cs) / (cols - 1) if cols > 1 else 0
        return cols, max(float(self.GAP), min(gap, self.GAP * 2.5))

    def relayout(self) -> None:
        t, c = self.theme, self.canvas
        c.delete("all")
        W = c.winfo_width()
        if W <= 1:
            return
        self._width = W
        cols, gap = self._geometry(W)
        cs = self.COVER
        card_h = cs + 58
        self.positions = {}
        for i, a in enumerate(self.albums):
            r, k = divmod(i, cols)
            x = self.MARGIN + k * (cs + gap)
            y = 12 + r * card_h
            self.positions[a["key"]] = (x, y, i)
            self._draw_card(i, a, x, y)
        rows = (len(self.albums) + cols - 1) // cols
        height = 12 + rows * card_h + 12
        if not self.albums and self.empty_text:
            c.create_text(W / 2, 60, text=self.empty_text, fill=t.c("text2"), font=t.f("body"), width=W - 60)
        c.configure(scrollregion=(0, 0, W, max(height, c.winfo_height())))
        self._request_visible()

    def _draw_card(self, i: int, a: dict, x: float, y: float) -> None:
        t, c, cs = self.theme, self.canvas, self.COVER
        tag = f"card{i}"
        c.delete(tag)
        if a.get("new"):
            round_rect(c, x, y, x + cs, y + cs, 8, fill=t.c("hover" if self.hover == i else "lcd"), outline="",
                       tags=tag)
            draw_icon(c, "plus", x + cs / 2, y + cs / 2, cs * 0.28, t.c("accent"), tag=tag)
        elif "mosaic" in a:
            draw_mosaic(c, t, self.covers, a["mosaic"], x, y, cs, tag)
        else:
            img = self.covers.get(a["key"], [tr.path for tr in a["tracks"]], cs) if self._visible(y) else \
                self.covers.images.get((a["key"], cs))
            draw_cover(c, t, img, x, y, cs, tag)
        if self.hover == i:
            round_rect(c, x, y, x + cs, y + cs, 8, fill="", outline=t.c("sep"), width=1, tags=tag)
        c.create_text(x, y + cs + 16, text=fit_text(t.f("card_title"), tr_name(a["title"]), cs, self._fit), anchor="w",
                      fill=t.c("text"), font=t.f("card_title"), tags=tag)
        c.create_text(x, y + cs + 34, text=fit_text(t.f("card_sub"), tr_name(a["artist"]), cs, self._fit), anchor="w",
                      fill=t.c("text2"), font=t.f("card_sub"), tags=tag)

    def _visible(self, y: float) -> bool:
        top = self.canvas.canvasy(0)
        return top - 2 * self.COVER <= y <= top + self.canvas.winfo_height() + self.COVER

    def _request_visible(self) -> None:
        for a in self.albums:
            pos = self.positions.get(a["key"])
            if a.get("new") or "mosaic" in a:
                continue
            if pos and self._visible(pos[1]) and (a["key"], self.COVER) not in self.covers.images:
                self.covers.get(a["key"], [tr.path for tr in a["tracks"]], self.COVER)

    def _cover_ready(self, key: str, size: int) -> None:
        if not self.winfo_exists():
            return
        if size == self.COVER and key in self.positions:
            x, y, i = self.positions[key]
            self._draw_card(i, self.albums[i], x, y)
        if size in (self.COVER, self.COVER // 2 - 1):  # a playlist picture (one cover, or a quarter)
            for a in self.albums:
                if any(q[0] == key for q in a.get("mosaic", ())) and a["key"] in self.positions:
                    x, y, i = self.positions[a["key"]]
                    self._draw_card(i, a, x, y)

    def _index_at(self, e) -> Optional[int]:
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        cs = self.COVER
        for key, (px, py, i) in self.positions.items():
            if px <= x <= px + cs and py <= y <= py + cs + 44:
                return i
        return None

    def _motion(self, e) -> None:
        i = self._index_at(e)
        if i != self.hover:
            old, self.hover = self.hover, i
            for j in (old, i):
                if j is not None and j < len(self.albums):
                    x, y, _ = self.positions[self.albums[j]["key"]]
                    self._draw_card(j, self.albums[j], x, y)
            self.canvas.configure(cursor="hand2" if i is not None else "")

    def _click(self, e) -> None:
        i = self._index_at(e)
        if i is not None:
            self.on_open(self.albums[i])


__all__ = ["SongList", "Column", "AlbumGrid", "CoverCache", "draw_cover", "draw_mosaic", "fmt_time", "bind_wheel",
           "scroll_units", "show_scrollbar"]
