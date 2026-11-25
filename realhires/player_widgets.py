"""Canvas widgets of the 2.0 player: the song tables (`SongList`, drawn row by row
for the visible part only, so a library of tens of thousands of songs scrolls
smoothly), the album card grid (`AlbumGrid`) and the cover cache (`CoverCache`,
covers from the covers folder on disk or made by FFmpeg in a background thread,
with rounded corners, kept for the session)."""

from __future__ import annotations

import base64
import collections
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .covers import CACHE_PATHS, trim_cache
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
    if widget.tk.call("tk", "windowingsystem") == "x11":  # elsewhere 4 and 5 are the back and forward buttons
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
    tip: bool = False       # hovering a cell shows tip_value(row, key) (the Quality column's resolution)


LABEL_DOT = 8  # 2.3: the colour label's dot, in pixels


def draw_label_dot(c: tk.Canvas, cx: float, cy: float, color: str, size: float = LABEL_DOT,
                   outline: str = "", tags=()) -> int:
    """A colour label (2.3): a dot `size` pixels wide centred on (cx, cy), in a 2-pixel ring
    of `outline` when it sits on something of a similar colour (a cover, a selected row)."""
    r = size / 2
    if outline:
        c.create_oval(cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2, fill=outline, outline="", tags=tags)
    return c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="", tags=tags)


class SongList(tk.Frame):
    """A table of songs drawn on a Canvas.  `value(row, key)` gives a cell's text;
    `sort_value(row, key)` what a click on the column heading sorts by;
    `tip_value(row, key)` the tooltip of a cell of a column with `tip`.  With
    `on_move(src, dst)` its rows can be dragged to another place with the primary
    button (Up Next, 2.3): a press becomes a drag after `DRAG_START` pixels, a line
    in the accent colour shows the gap the row will drop into, the list scrolls
    while the pointer is near its top or bottom edge, and `dst` is the row's index
    after the move.

    Rows for which `special(row)` is true (2.4: the subfolders of the Folders page) are
    drawn by `draw_special(canvas, row, x0, x1, y, height, tags, selected)` instead of the
    columns, stay first when the list is sorted, have no heart or tooltip, and a click
    on one activates it (`on_activate`) as a link does.

    Only the visible rows are drawn.  A scroll step moves the drawn rows and draws
    only the rows that come into view, and moving the pointer to another row only
    recolours the two rows' backgrounds, so a library of tens of thousands of songs
    scrolls smoothly."""

    HEADER = 30
    TIP_DELAY_MS = 450
    DRAG_START = 5        # pixels the pointer must move before a press becomes a drag
    DRAG_EDGE = 18        # pixels from the top or bottom edge where a drag scrolls the list
    DRAG_SCROLL_MS = 40   # how often it scrolls then

    def __init__(self, master, theme: Theme, columns: Sequence[Column], value: Callable[[Any, str], str],
                 sort_value: Optional[Callable[[Any, str], Any]] = None, row_height: int = 32,
                 header: bool = True, zebra: bool = True, on_activate: Optional[Callable[[int], None]] = None,
                 on_heart: Optional[Callable[[Any], None]] = None, on_select: Optional[Callable[[int], None]] = None,
                 is_liked: Optional[Callable[[Any], bool]] = None, is_current: Optional[Callable[[Any], bool]] = None,
                 bg_role: str = "bg", sortable: bool = True, empty_text: str = "",
                 on_context: Optional[Callable[[int, Any], None]] = None,
                 is_dim: Optional[Callable[[Any], bool]] = None,
                 tip_value: Optional[Callable[[Any, str], str]] = None,
                 on_heading_context: Optional[Callable[[Any], None]] = None,
                 on_move: Optional[Callable[[int, int], None]] = None,
                 label_of: Optional[Callable[[Any], Optional[str]]] = None,
                 cell_color: Optional[Callable[[Any, str], Optional[str]]] = None,
                 special: Optional[Callable[[Any], bool]] = None,
                 draw_special: Optional[Callable[..., None]] = None):
        super().__init__(master, bd=0, highlightthickness=0)
        self.special, self.draw_special = special, draw_special  # 2.4: rows drawn as links
        self._special_click: Optional[int] = None  # the event time of the last click on one
        self.on_context, self.is_dim, self.tip_value = on_context, is_dim, tip_value
        # 2.3: `label_of(row)` the colour label drawn as a dot before the title (None: no dot),
        # `cell_color(row, key)` a cell's own text colour (the Quality column's quality colour)
        self.label_of, self.cell_color = label_of, cell_color
        self.on_move = on_move
        self._press: Optional[Tuple[int, int]] = None   # (row, y) of a press that may become a drag
        self.drag_from: Optional[int] = None            # the row being dragged
        self.drop_at: Optional[int] = None              # the gap it would go to (0 = above the first row)
        self._drag_y = 0                                # where the pointer is during a drag
        self._drag_job: Optional[str] = None            # scrolling near an edge
        self.on_heading_context = on_heading_context  # right click on the headings (choose the columns)
        self.column_kind: Optional[str] = None
        self.column_prefix: List[Column] = []
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
        self._drawn: Dict[int, int] = {}    # row index -> its background item, for the rows on the canvas
        self._drawn_top = 0.0               # self.top when the drawn rows were placed
        self._cols: List[Tuple[Column, int, int]] = []
        self.full_redraws = 0               # counted for the tests: a scroll step must not redraw everything
        self._tip_cell: Optional[tuple] = None
        self._tip_job: Optional[str] = None
        self._tip_win: Optional[tk.Toplevel] = None
        self.tip_text = ""                  # the tooltip shown, "" when none
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
        c.bind("<Leave>", lambda e: (self._set_hover(None), self.hide_tip()))
        c.bind("<Button-1>", self._click)
        c.bind("<Double-Button-1>", self._double)
        c.bind("<B1-Motion>", self._drag_motion)
        c.bind("<ButtonRelease-1>", self._drag_end)
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
        self.cancel_drag()  # the rows changed under a drag: it ends here
        self.rows = list(rows)
        if self.sort_key is not None and self.sort_value is not None:
            self._sort()
        self.selected = self.rows.index(sel) if sel is not None and sel in self.rows else None
        if not keep:
            self.top = 0.0
        self.redraw()

    def set_columns(self, columns: Sequence[Column]) -> None:
        """Show other columns (the user chose them); a sort by a hidden column ends."""
        self.columns = list(columns)
        if self.sort_key is not None and all(c.key != self.sort_key for c in self.columns):
            self.sort_key, self.sort_rev = None, False
        self.redraw()

    def _is_special(self, row: Any) -> bool:
        return self.special is not None and self.draw_special is not None and bool(self.special(row))

    def _sort(self) -> None:
        key = self.sort_key
        first = [r for r in self.rows if self._is_special(r)]  # special rows stay first, in their order
        rest = [r for r in self.rows if not self._is_special(r)] if first else self.rows
        rest.sort(key=lambda r: self.sort_value(r, key), reverse=self.sort_rev)  # type: ignore[misc]
        self.rows = first + rest if first else rest

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
        self._scroll_to(self.top + pixels)

    def _yview(self, *args) -> None:
        total = max(1, self._content_height())
        top = self.top
        if args[0] == "moveto":
            top = float(args[1]) * total
        elif args[0] == "scroll":
            n = int(args[1])
            top += n * (self._page_rows() * self.rh if args[2] == "pages" else 3 * self.rh)
        self._scroll_to(top)

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
        i = self._row_at(e.y)
        self._set_hover(i)
        self._tip_motion(e)
        if self.special is not None:  # a hand over the rows that are links
            link = i is not None and self._is_special(self.rows[i])
            self.canvas.configure(cursor="hand2" if link else "")

    def _set_hover(self, i: Optional[int]) -> None:
        if i == self.hover:
            return
        old, self.hover = self.hover, i
        if not self._drawn and (old is not None or i is not None):
            return
        for j in (old, i):  # only these two rows change: their background and the hover heart
            if j is not None and j in self._drawn:
                self._paint_row_bg(j)

    def _column_at(self, x: int) -> Optional[Column]:
        for col, cx, w in self._layout(self.canvas.winfo_width()):
            if cx - 4 <= x <= cx + w + 4:
                return col
        return None

    def _click(self, e) -> None:
        self.hide_tip()
        self.canvas.focus_set()
        if self.has_header and e.y < self.HEADER:
            col = self._column_at(e.x)
            if col is not None and self.sortable and col.kind != "heart":
                self.sort_by(col.key)
            return
        i = self._row_at(e.y)
        if i is None:
            return
        if self._is_special(self.rows[i]):  # a link: one click opens it
            self.selected = i
            self._special_click = getattr(e, "time", 0)
            self._activate(i)
            return
        self._special_click = None
        col = self._column_at(e.x)
        if col is not None and col.kind == "heart" and self.on_heart is not None:
            self.on_heart(self.rows[i])
            self.redraw()
            return
        self.select(i)
        if self.on_move is not None:
            self._press = (i, e.y)

    # -------------------------------------------------- dragging a row ---
    def _gap_at(self, y: int) -> int:
        """The gap between rows nearest to `y` (0 = above the first row)."""
        y -= self.HEADER if self.has_header else 0
        return min(max(int(round((y + self.top) / self.rh)), 0), len(self.rows))

    def _drag_motion(self, e) -> None:
        if self._press is None:
            return
        row, y0 = self._press
        if self.drag_from is None:
            if abs(e.y - y0) < self.DRAG_START or row >= len(self.rows):
                return
            self.drag_from = row
            self.hide_tip()
            self.canvas.configure(cursor="fleur")
        self._drag_y = e.y
        self.drop_at = self._gap_at(e.y)
        self._draw_drop()
        if self._edge_step() and self._drag_job is None:
            self._drag_job = self.after(self.DRAG_SCROLL_MS, self._drag_scroll)

    def _edge_step(self) -> float:
        """How far to scroll for the pointer near an edge during a drag (0: not near one)."""
        head = self.HEADER if self.has_header else 0
        if self._drag_y < head + self.DRAG_EDGE and self.top > 0:
            return -self.rh / 3
        if self._drag_y > self.canvas.winfo_height() - self.DRAG_EDGE and self.top < self._max_top():
            return self.rh / 3
        return 0.0

    def _drag_scroll(self) -> None:
        """Scroll along while a drag holds the pointer near an edge."""
        self._drag_job = None
        step = self._edge_step() if self.drag_from is not None else 0.0
        if not step:
            return
        self._scroll_to(self.top + step)
        self.drop_at = self._gap_at(self._drag_y)
        self._draw_drop()
        self._drag_job = self.after(self.DRAG_SCROLL_MS, self._drag_scroll)

    def _drag_end(self, e) -> None:
        src = self.drag_from
        self.cancel_drag()
        if src is None:
            return
        gap = self._gap_at(e.y)
        dst = gap if gap <= src else gap - 1
        if dst != src and self.on_move is not None and 0 <= dst < len(self.rows):
            self.on_move(src, dst)

    def cancel_drag(self) -> None:
        """End a drag (or a press that could become one) without moving anything."""
        if self._drag_job is not None:
            try:
                self.after_cancel(self._drag_job)
            except tk.TclError:
                pass
            self._drag_job = None
        was = self.drag_from is not None
        self._press = self.drag_from = self.drop_at = None
        if was:
            self.canvas.configure(cursor="")
            self.canvas.delete("drop")

    def _draw_drop(self) -> None:
        """The line in the accent colour, with a small circle at its left end, that shows
        the gap the dragged row drops into."""
        c = self.canvas
        c.delete("drop")
        if self.drag_from is None or self.drop_at is None:
            return
        head = self.HEADER if self.has_header else 0
        y = head + self.drop_at * self.rh - self.top
        y = min(max(y, head + 2), c.winfo_height() - 2)
        W, accent = c.winfo_width(), self.theme.c("accent")
        c.create_line(16, y, W - 10, y, fill=accent, width=2, tags="drop")
        c.create_oval(6, y - 4, 14, y + 4, outline=accent, width=2, tags="drop")

    def select(self, i: Optional[int]) -> None:
        self.selected = i
        if i is not None:
            self.see(i)
        self.redraw()
        if i is not None and self.on_select is not None:
            self.on_select(i)

    def _context(self, e) -> None:
        """Right click: select the row and ask for its menu (on_context(index, event)); on
        the headings, the menu of the columns (on_heading_context(event))."""
        self.hide_tip()
        if self.has_header and e.y < self.HEADER:
            if self.on_heading_context is not None:
                self.on_heading_context(e)
            return
        i = self._row_at(e.y)
        if i is None or self.on_context is None or self._is_special(self.rows[i]):
            return
        self.select(i)
        self.on_context(i, e)

    def _double(self, e) -> None:
        if self._special_click is not None and 0 <= getattr(e, "time", 0) - self._special_click < 1000:
            # the second click of a double click on a link: the first one opened it already
            self._special_click = None
            return
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

    # ---------------------------------------------------------- tooltip ---
    def _tip_motion(self, e) -> None:
        cell = None
        if self.tip_value is not None and not (self.has_header and e.y < self.HEADER):
            i, col = self._row_at(e.y), self._column_at(e.x)
            if i is not None and col is not None and col.tip and not self._is_special(self.rows[i]):
                cell = (i, col.key)
        if cell == self._tip_cell:
            return
        self.hide_tip()
        self._tip_cell = cell
        if cell is not None:
            x, y = e.x_root + 12, e.y_root + 18
            self._tip_job = self.after(self.TIP_DELAY_MS, lambda: self.show_tip(cell[0], cell[1], x, y))

    def show_tip(self, i: int, key: str, x: Optional[int] = None, y: Optional[int] = None) -> str:
        """Show the tooltip of row i's `key` cell (near the pointer); returns its text."""
        self._tip_job = None
        if self.tip_value is None or not 0 <= i < len(self.rows):
            return ""
        text = self.tip_value(self.rows[i], key)
        if self._tip_win is not None:
            self._tip_win.destroy()
            self._tip_win = None
        self.tip_text = text
        if not text:
            return ""
        t = self.theme
        if x is None or y is None:
            col = next(((c, cx, w) for c, cx, w in self._layout(self.canvas.winfo_width()) if c.key == key), None)
            x = self.canvas.winfo_rootx() + (col[1] if col else 0)
            y = self.canvas.winfo_rooty() + (self.HEADER if self.has_header else 0) + int((i + 1) * self.rh
                                                                                           - self.top) + 2
        self._tip_win = w = tk.Toplevel(self)
        w.wm_overrideredirect(True)
        tk.Label(w, text=text, bg=t.c("lcd"), fg=t.c("text"), font=t.f("small"), padx=8, pady=4,
                 highlightthickness=1, highlightbackground=t.c("sep")).pack()
        w.wm_geometry(f"+{x}+{y}")
        return text

    def hide_tip(self) -> None:
        if self._tip_job is not None:
            try:
                self.after_cancel(self._tip_job)
            except tk.TclError:
                pass
            self._tip_job = None
        if self._tip_win is not None:
            try:
                self._tip_win.destroy()
            except tk.TclError:
                pass
            self._tip_win = None
        self._tip_cell = None
        self.tip_text = ""

    # ---------------------------------------------------------- drawing ---
    def _row_fill(self, i: int) -> str:
        t = self.theme
        if i == self.selected:
            return t.c("accent")
        if i == self.hover:
            return t.c("hover")
        if self.zebra and i % 2 == 1:
            return t.c("zebra")
        return ""

    def _paint_row_bg(self, i: int) -> None:
        """A row's background and its hover heart, after the pointer moved."""
        c, t = self.canvas, self.theme
        c.itemconfigure(self._drawn[i], fill=self._row_fill(i))
        c.delete(f"hh{i}")
        if i == self.hover and i != self.selected and any(col.kind == "heart" for col, _, _ in self._cols) \
                and i < len(self.rows) and not self._is_special(self.rows[i]):
            row = self.rows[i]
            if not (self.is_liked(row) if self.is_liked else False):
                head = self.HEADER if self.has_header else 0
                mid = head + i * self.rh - self.top + self.rh / 2
                for col, x, w in self._cols:
                    if col.kind == "heart":
                        draw_icon(c, "heart", x + w / 2, mid, 13, t.c("text2"), tag=("row", f"r{i}", f"hh{i}"))
                if self.has_header:
                    c.tag_raise("head")

    def _draw_row(self, i: int, head: int, W: int) -> None:
        t, c = self.theme, self.canvas
        row = self.rows[i]
        y = head + i * self.rh - self.top
        tags = ("row", f"r{i}")
        sel = i == self.selected
        current = self.is_current(row) if self.is_current else False
        self._drawn[i] = round_rect(c, 6, y + 1, W - 6, y + self.rh - 1, 6, fill=self._row_fill(i), outline="",
                                    tags=tags)
        if self._is_special(row):
            self.draw_special(c, row, 6, W - 6, y, self.rh, tags, sel)  # type: ignore[misc]
            return
        dim = self.is_dim(row) if self.is_dim else False
        fg = t.c("on_accent") if sel else (t.c("accent") if current else t.c("disabled" if dim else "text"))
        fg2 = t.c("on_accent") if sel else t.c("disabled" if dim else "text2")
        mid = y + self.rh / 2
        body, small = t.f("body"), t.f("small")
        for col, x, w in self._cols:
            if col.kind == "heart":
                liked = self.is_liked(row) if self.is_liked else False
                if liked or i == self.hover:
                    color = t.c("on_accent") if sel else (t.c("accent") if liked else t.c("text2"))
                    draw_icon(c, "heart", x + w / 2, mid, 13, color, filled=liked,
                              tag=tags if liked else tags + (f"hh{i}",))
                continue
            if col.kind == "num" and current:
                draw_icon(c, "speaker", x + w / 2, mid, 13, t.c("on_accent") if sel else t.c("accent"), tag=tags)
                continue
            if col.kind == "icon":  # value() names the icon
                draw_icon(c, self.value(row, col.key), x + w / 2, mid, 16, t.c("on_accent") if sel
                          else t.c("accent"), tag=tags)
                continue
            dot = self.label_of(row) if self.label_of is not None and col.key in ("title", "name") \
                and col.kind in ("text", "two") else None
            if dot:  # 2.3: the colour label, a dot before the title
                draw_label_dot(c, x + LABEL_DOT / 2, mid - (8 if col.kind == "two" else 0), dot,
                               outline=t.c("on_accent") if sel else "", tags=tags + ("label",))
                x, w = x + LABEL_DOT + 6, max(10, w - LABEL_DOT - 6)
            if col.kind == "two":
                c.create_text(x, mid - 8, text=fit_text(t.f("body_b"), self.value(row, col.key), w, self._fit),
                              anchor="w", fill=fg, font=t.f("body_b"), tags=tags)
                c.create_text(x, mid + 9, text=fit_text(small, self.value(row, col.key2), w, self._fit),
                              anchor="w", fill=fg2, font=small, tags=tags)
                continue
            text = fit_text(body, self.value(row, col.key), w, self._fit)
            color = fg if col.kind != "num" and col.key in ("title", "name") else (fg if sel or current else fg2)
            own = self.cell_color(row, col.key) if self.cell_color is not None and not sel and not dim else None
            if own:  # 2.3: e.g. the Quality column in the quality's colour
                color = own
            if col.anchor == "e":
                c.create_text(x + w, mid, text=text, anchor="e", fill=color, font=body, tags=tags)
            elif col.anchor == "center":
                c.create_text(x + w / 2, mid, text=text, anchor="center", fill=color, font=body, tags=tags)
            else:
                c.create_text(x, mid, text=text, anchor="w", fill=color, font=body, tags=tags)

    def _visible_range(self, H: int) -> Tuple[int, int]:
        first = int(self.top // self.rh)
        return first, min(len(self.rows), int((self.top + H) // self.rh) + 2)

    def _scroll_to(self, top: float) -> None:
        """Scroll: the drawn rows move, the rows that leave the view are deleted and
        only the rows that come into it are drawn."""
        self.hide_tip()
        top = min(max(top, 0.0), self._max_top())
        c = self.canvas
        W, H = c.winfo_width(), c.winfo_height()
        if top == self.top and self._drawn:
            return
        if W <= 1 or not self._drawn or abs(top - self._drawn_top) > H:
            self.top = top
            self.redraw()
            return
        c.move("row", 0, self._drawn_top - top)
        self.top = self._drawn_top = top
        first, last = self._visible_range(H)
        for i in [j for j in self._drawn if not first <= j < last]:
            c.delete(f"r{i}")
            del self._drawn[i]
        head = self.HEADER if self.has_header else 0
        for i in range(first, last):
            if i not in self._drawn:
                self._draw_row(i, head, W)
        if self.has_header:
            c.tag_raise("head")
        self._draw_drop()
        self._update_scrollbar()

    def redraw(self) -> None:
        t, c = self.theme, self.canvas
        self.full_redraws += 1
        c.delete("all")
        self._drawn = {}
        W, H = c.winfo_width(), c.winfo_height()
        if W <= 1:
            return
        cols = self._cols = self._layout(W)
        head = self.HEADER if self.has_header else 0
        self.top = self._drawn_top = min(max(self.top, 0.0), self._max_top())
        first, last = self._visible_range(H)
        body, small = t.f("body"), t.f("small")
        for i in range(first, last):
            self._draw_row(i, head, W)
        if not self.rows and self.empty_text:
            c.create_text(W / 2, head + 40, text=self.empty_text, fill=t.c("text2"), font=body, width=W - 40)
        if self.rows and self.footer:
            c.create_text(cols[0][1] if cols else 16, head + len(self.rows) * self.rh - self.top + 24,
                          text=self.footer, anchor="w", fill=t.c("text2"), font=small, tags=("row", "foot"))
        if self.has_header:
            c.create_rectangle(0, 0, W, head, fill=t.c(self.bg_role), outline="", tags="head")
            for col, x, w in cols:
                if not col.title:
                    continue
                anchor = {"e": "e", "center": "center"}.get(col.anchor, "w")
                xx = x + w if anchor == "e" else x + w / 2 if anchor == "center" else x
                item = c.create_text(xx, head / 2, text=tr(col.title), anchor=anchor, fill=t.c("text2"),
                                     font=t.f("small_b"), tags="head")
                if self.sort_key == col.key:  # a small triangle: down = A to Z, up = Z to A
                    bx = c.bbox(item)[2] + 8 if anchor == "w" else c.bbox(item)[0] - 8
                    d = -3 if self.sort_rev else 3
                    c.create_polygon(bx - 4, head / 2 - d, bx + 4, head / 2 - d, bx, head / 2 + d,
                                     fill=t.c("text2"), outline="", tags="head")
            c.create_line(12, head - 1, W - 12, head - 1, fill=t.c("sep"), tags="head")
        self._draw_drop()
        self._update_scrollbar()

    def _update_scrollbar(self) -> None:
        total = max(1, self._content_height())
        body_h = self._body_height()
        if total <= body_h:
            self.sb.set(0.0, 1.0)
        else:
            self.sb.set(self.top / total, min(1.0, (self.top + body_h) / total))
        show_scrollbar(self.sb, total > body_h, self.bg_role)


def rounded_photo(master: tk.Misc, data: bytes, size: int) -> Optional[tk.PhotoImage]:
    """A PNG cover as a PhotoImage whose corners are transparent (rounded, as the
    player draws covers), or None when Tk cannot read it."""
    try:
        img = tk.PhotoImage(master=master, data=base64.b64encode(data).decode("ascii"), format="png")
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


class CoverCache:
    """Album covers as PhotoImages with rounded corners.  One background thread
    gets them with covers.cached_cover_png() (from the covers folder on disk,
    else made by FFmpeg and saved there for the next time): first the covers
    asked for on screen, newest first, then, after prefetch(), the grid-size
    covers of every album that the covers folder does not have yet.

    A cover once shown stays for the session: every size up to GRID (the album
    grid, its mosaic quarters, the LCD) without limit, larger ones (the album
    page, Now Playing) up to MAX_LARGE, the least recently shown going first.
    library_changed() drops the covers of albums whose songs changed."""

    GRID = 176          # AlbumGrid.COVER, the size prefetch() makes ahead
    MAX_LARGE = 200
    POLL_MS = 60           # while nothing is asked for
    FAST_POLL_MS = 16      # while covers are on their way (one from disk takes a few ms)
    POLL_BUDGET_S = 0.012  # PhotoImages made per poll, so a burst of covers never stalls scrolling

    def __init__(self, root: tk.Misc, trim: bool = True):
        self.root = root
        self.images: Dict[tuple, tk.PhotoImage] = {}
        self._large: "collections.OrderedDict[tuple, None]" = collections.OrderedDict()
        self.none: set = set()                   # (key, size) of albums without a cover
        self.listeners: List[Callable[[str, int], None]] = []
        self.pending: set = set()
        self.signatures: Dict[str, tuple] = {}   # album key -> its first songs (path, size, mtime)
        self._gen = 0                            # bumped when covers are dropped; older answers are stale
        self._dropped: Dict[str, int] = {}       # album key -> the generation it was dropped in
        self._cv = threading.Condition()
        self._front: List[tuple] = []            # asked for on screen (served newest first)
        self._back: "collections.deque" = collections.deque()  # made ahead (in album order)
        self.done: queue.Queue = queue.Queue()
        self.alive = True
        threading.Thread(target=self._work, daemon=True, name="realhires-covers").start()
        if trim:
            threading.Thread(target=trim_cache, daemon=True, name="realhires-covers-trim").start()
        self._job: Optional[str] = None
        self._fast = False
        self._schedule(self.POLL_MS)

    def _schedule(self, ms: int) -> None:
        try:
            if self._job is not None:
                self.root.after_cancel(self._job)
            self._job = self.root.after(ms, self._poll)
        except tk.TclError:
            self._job = None
        self._fast = ms < self.POLL_MS

    def close(self) -> None:
        with self._cv:
            self.alive = False
            self._front.clear()
            self._back.clear()
            self._cv.notify_all()
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def missing(self, key: str, size: int) -> bool:
        return (key, size) in self.none

    def get(self, key: str, paths: Sequence[str], size: int) -> Optional[tk.PhotoImage]:
        k = (key, size)
        img = self.images.get(k)
        if img is not None:
            if k in self._large:
                self._large.move_to_end(k)
            return img
        if k in self.none or k in self.pending:
            return None
        self.pending.add(k)
        with self._cv:
            self._front.append((k, list(paths)[:CACHE_PATHS], self._gen))
            self._cv.notify()
        if self.alive and not self._fast:
            self._schedule(self.FAST_POLL_MS)
        return None

    def prefetch(self, albums: Sequence[dict], size: int = GRID) -> None:
        """Make the covers of `albums` ({"key", "tracks"}) at `size` that the
        covers folder does not have yet, after everything asked for on screen."""
        jobs = [((a["key"], size), [t.path for t in a["tracks"][:CACHE_PATHS]], self._gen) for a in albums
                if (a["key"], size) not in self.images and (a["key"], size) not in self.none]
        with self._cv:
            self._back = collections.deque(jobs)
            self._cv.notify()

    def prefetching(self) -> int:
        """How many albums the background pass has still to look at."""
        with self._cv:
            return len(self._back)

    def library_changed(self, albums: Sequence[dict]) -> None:
        """The library was read again: keep every cover whose album has the same
        first songs (path, size and time) as before, drop the others."""
        new = {a["key"]: tuple((t.path, t.size, t.mtime_ns) for t in a["tracks"][:CACHE_PATHS]) for a in albums}
        gone = {key for key, sig in self.signatures.items() if new.get(key) != sig}
        self.signatures = new
        if not gone:
            return
        self._gen += 1
        for k in [k for k in self.images if k[0] in gone]:
            del self.images[k]
            self._large.pop(k, None)
        self.none = {k for k in self.none if k[0] not in gone}
        self.pending = {k for k in self.pending if k[0] not in gone}
        self._dropped.update((key, self._gen) for key in gone)

    def _keep(self, k: tuple, img: tk.PhotoImage) -> None:
        self.images[k] = img
        if k[1] > self.GRID:
            self._large[k] = None
            self._large.move_to_end(k)
            while len(self._large) > self.MAX_LARGE:
                old, _ = self._large.popitem(last=False)
                self.images.pop(old, None)

    def _image(self, data: bytes, size: int) -> Optional[tk.PhotoImage]:
        return rounded_photo(self.root, data, size)

    def _next_job(self) -> Optional[tuple]:
        with self._cv:
            while self.alive and not self._front and not self._back:
                self._cv.wait()
            if not self.alive:
                return None
            if self._front:
                return self._front.pop() + (True,)
            return self._back.popleft() + (False,)

    def _work(self) -> None:
        from . import covers

        while True:
            job = self._next_job()
            if job is None:
                break
            k, paths, gen, shown = job
            try:
                if not shown:  # made ahead: only on disk, the screen asks for it when it is shown
                    if not covers.is_cached(paths, k[1]):
                        covers.cached_cover_png(paths, k[1])
                    continue
                data = covers.cached_cover_png(paths, k[1])
            except Exception:  # noqa: BLE001 - a broken cover must not stop the others
                if not shown:
                    continue
                data = None
            self.done.put((k, gen, data))

    def _poll(self) -> None:
        self._job = None
        changed = []
        t0 = time.perf_counter()
        more = False
        try:
            while True:
                if time.perf_counter() - t0 > self.POLL_BUDGET_S:
                    more = True
                    break
                k, gen, data = self.done.get_nowait()
                if gen < self._dropped.get(k[0], -1):
                    continue  # made from songs that have changed since
                self.pending.discard(k)
                img = self._image(data, k[1]) if data is not None else None
                if img is None:
                    self.none.add(k)
                else:
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
            self._schedule(10 if more else self.FAST_POLL_MS if self.pending else self.POLL_MS)


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
    An item can instead be a mosaic ("mosaic": up to four (album key, paths): a
    playlist, or an artist, genre, year... of the grouping pages) or the "New
    Playlist…" card ("new": True).

    Only the cards near the visible part are drawn, and the rest as they scroll into
    view, so showing and resizing a grid of thousands of albums stays quick; a mosaic
    uses the album covers of the grid's size, halved (no extra covers are made).
    With `multi`, Ctrl-click (Cmd-click on macOS, where Ctrl-click is a right-click)
    selects several cards, and so does a plain click in select mode (`select_mode`,
    2.3); `selected` holds their keys and `on_selection()` is told.
    `on_context(index, event)` makes a right-click menu."""

    COVER = CoverCache.GRID
    GAP = 24
    MARGIN = 28

    def __init__(self, master, theme: Theme, covers: CoverCache, on_open: Callable[[dict], None],
                 empty_text: str = "", on_context: Optional[Callable[[int, Any], None]] = None,
                 multi: bool = False, on_selection: Optional[Callable[[], None]] = None,
                 label_of: Optional[Callable[[dict], Optional[str]]] = None):
        super().__init__(master, bd=0, highlightthickness=0)
        self.theme, self.covers, self.on_open, self.empty_text = theme, covers, on_open, empty_text
        self.label_of = label_of  # 2.3: a card's colour label, a dot at its cover's bottom-left corner
        self.on_context, self.multi, self.on_selection = on_context, multi, on_selection
        self.albums: List[dict] = []
        self.positions: Dict[str, Tuple[float, float, int]] = {}
        self.selected: set = set()          # keys of the selected cards (multi)
        self.select_mode = False            # (multi) a click selects instead of opening (2.3)
        self.hover: Optional[int] = None
        self._fit: Dict[tuple, str] = {}
        self._drawn: set = set()            # indices of the cards on the canvas
        self._cols, self._gap = 1, float(self.GAP)
        self._mosaic_of: Dict[str, List[int]] = {}  # album key -> the mosaic cards that show its cover
        self._halves: Dict[str, tuple] = {}         # album key -> (cover, the cover halved)
        theme.reg(self, bg="bg")
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, height=200, width=200, takefocus=1)
        theme.reg(self.canvas, bg="bg")
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self._yview, style="P.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self._sbset)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self._sb_shown = True
        c = self.canvas
        c.bind("<Configure>", lambda e: self.relayout())
        c.bind("<Button-1>", self._click)
        if c.tk.call("tk", "windowingsystem") == "aqua":  # macOS: Cmd-click selects, Ctrl-click is a right-click
            c.bind("<Command-Button-1>", self._toggle_click)
            c.bind("<Control-Button-1>", self._context)
        else:
            c.bind("<Control-Button-1>", self._toggle_click)
        c.bind("<Button-3>", self._context)
        c.bind("<Button-2>", self._context)  # macOS right click
        c.bind("<Escape>", lambda e: self.clear_selection())
        c.bind("<Motion>", self._motion)
        bind_wheel(c, lambda n: self._yview("scroll", n, "units"))
        covers.listeners.append(self._cover_ready)
        theme.on_change(lambda: (self._fit.clear(), self.relayout()), self)
        self._width = 0

    def _sbset(self, a, b) -> None:
        self.sb.set(a, b)
        show_scrollbar(self.sb, float(a) > 0.0 or float(b) < 1.0)

    def _yview(self, *args) -> None:
        self.canvas.yview(*args)
        self._draw_visible()

    def set_albums(self, albums: List[dict], keep: bool = False) -> None:
        self.albums = list(albums)
        keys = {a["key"] for a in self.albums}
        self.selected &= keys
        self._mosaic_of = {}
        for i, a in enumerate(self.albums):
            for q in a.get("mosaic", ()):
                self._mosaic_of.setdefault(q[0], []).append(i)
        if not keep:
            self.canvas.yview_moveto(0)
        self.relayout()

    def _geometry(self, width: int) -> Tuple[int, float]:
        cs, m = self.COVER, self.MARGIN
        cols = max(1, int((width - 2 * m + self.GAP) // (cs + self.GAP)))
        gap = (width - 2 * m - cols * cs) / (cols - 1) if cols > 1 else 0
        return cols, max(float(self.GAP), min(gap, self.GAP * 2.5))

    @property
    def card_h(self) -> int:
        return self.COVER + 58

    def relayout(self) -> None:
        t, c = self.theme, self.canvas
        c.delete("all")
        self._drawn = set()
        W = c.winfo_width()
        if W <= 1:
            return
        self._width = W
        cols, gap = self._cols, self._gap = self._geometry(W)
        cs, card_h = self.COVER, self.card_h
        self.positions = {}
        for i, a in enumerate(self.albums):
            r, k = divmod(i, cols)
            self.positions[a["key"]] = (self.MARGIN + k * (cs + gap), 12 + r * card_h, i)
        rows = (len(self.albums) + cols - 1) // cols
        height = 12 + rows * card_h + 12
        if not self.albums and self.empty_text:
            c.create_text(W / 2, 60, text=self.empty_text, fill=t.c("text2"), font=t.f("body"), width=W - 60)
        c.configure(scrollregion=(0, 0, W, max(height, c.winfo_height())))
        self._draw_visible()

    def _pos(self, i: int) -> Tuple[float, float]:
        r, k = divmod(i, self._cols)
        return self.MARGIN + k * (self.COVER + self._gap), 12 + r * self.card_h

    def visible_range(self) -> Tuple[int, int]:
        """The indices of the cards in view, and a row above and below."""
        top = self.canvas.canvasy(0)
        h = max(self.canvas.winfo_height(), 1)
        r0 = max(0, int((top - 12) // self.card_h) - 1)
        r1 = int((top + h - 12) // self.card_h) + 2
        return r0 * self._cols, min(len(self.albums), r1 * self._cols)

    def _draw_visible(self) -> None:
        """Draw the cards that came into view (each card is drawn once per layout)."""
        if not self._width:
            return
        a0, a1 = self.visible_range()
        for i in range(a0, a1):
            if i not in self._drawn:
                x, y = self._pos(i)
                self._draw_card(i, self.albums[i], x, y)

    def _draw_card(self, i: int, a: dict, x: float, y: float) -> None:
        t, c, cs = self.theme, self.canvas, self.COVER
        tag = f"card{i}"
        c.delete(tag)
        self._drawn.add(i)
        if a.get("new"):
            round_rect(c, x, y, x + cs, y + cs, 8, fill=t.c("hover" if self.hover == i else "lcd"), outline="",
                       tags=tag)
            draw_icon(c, "plus", x + cs / 2, y + cs / 2, cs * 0.28, t.c("accent"), tag=tag)
        elif "mosaic" in a:
            self._draw_mosaic(a["mosaic"], x, y, tag, a.get("icon", "playlist"))
        else:
            draw_cover(c, t, self.covers.get(a["key"], [tr.path for tr in a["tracks"]], cs), x, y, cs, tag)
        if a["key"] in self.selected:
            round_rect(c, x - 3, y - 3, x + cs + 3, y + cs + 3, 10, fill="", outline=t.c("accent"), width=3,
                       tags=tag)
            c.create_oval(x + cs - 34, y + 8, x + cs - 8, y + 34, fill=t.c("accent"), outline="", tags=tag)
            draw_icon(c, "check", x + cs - 21, y + 21, 22, t.c("on_accent"), tag=tag)
        elif self.hover == i:
            round_rect(c, x, y, x + cs, y + cs, 8, fill="", outline=t.c("sep"), width=1, tags=tag)
        dot = self.label_of(a) if self.label_of is not None and not a.get("new") else None
        if dot:  # 2.3: the colour label at the cover's bottom-left corner, ringed to stand out on it
            draw_label_dot(c, x + 14, y + cs - 14, dot, outline=t.c("bg"),
                           tags=(tag, "label", f"label{i}"))
        c.create_text(x, y + cs + 16, text=fit_text(t.f("card_title"), tr_name(a["title"]), cs, self._fit), anchor="w",
                      fill=t.c("text"), font=t.f("card_title"), tags=tag)
        c.create_text(x, y + cs + 34, text=fit_text(t.f("card_sub"), tr_name(a["artist"]), cs, self._fit), anchor="w",
                      fill=t.c("text2"), font=t.f("card_sub"), tags=tag)

    def _half(self, key: str, paths: Sequence[str]) -> Optional[tk.PhotoImage]:
        """An album's cover at half the grid's size: the grid-size cover, subsampled."""
        img = self.covers.get(key, paths, self.COVER)
        if img is None:
            return None
        h = self._halves.get(key)
        if h is None or h[0] is not img:
            try:
                h = (img, img.subsample(2, 2))
            except tk.TclError:
                return None
            self._halves[key] = h
        return h[1]

    def _draw_mosaic(self, quarters: Sequence[tuple], x: float, y: float, tag: str, icon: str) -> None:
        """Up to four album covers, 2 x 2 (fewer repeat), or the one cover there is."""
        t, c, cs = self.theme, self.canvas, self.COVER
        if not quarters:
            round_rect(c, x, y, x + cs, y + cs, max(3, cs * 0.045), fill=t.c("ph"), outline="", tags=tag)
            draw_icon(c, icon, x + cs / 2, y + cs / 2, cs * 0.34, t.c("ph_icon"), tag=tag)
            return
        if len(quarters) == 1:
            key, paths = quarters[0]
            draw_cover(c, t, self.covers.get(key, paths, cs), x, y, cs, tag)
            return
        half = cs // 2
        for n in range(4):
            key, paths = quarters[n % len(quarters)]
            draw_cover(c, t, self._half(key, paths), x + (n % 2) * half, y + (n // 2) * half, half, tag)

    def _cover_ready(self, key: str, size: int) -> None:
        if not self.winfo_exists() or size != self.COVER:
            return
        pos = self.positions.get(key)
        if pos is not None and pos[2] in self._drawn:
            self._draw_card(pos[2], self.albums[pos[2]], pos[0], pos[1])
        for i in self._mosaic_of.get(key, ()):
            if i in self._drawn and i < len(self.albums):
                x, y = self._pos(i)
                self._draw_card(i, self.albums[i], x, y)

    def _index_at(self, e) -> Optional[int]:
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        cs = self.COVER
        r = int((y - 12) // self.card_h)
        k = int((x - self.MARGIN) // (cs + self._gap)) if x >= self.MARGIN else -1
        if r < 0 or not 0 <= k < self._cols:
            return None
        i = r * self._cols + k
        if i >= len(self.albums):
            return None
        px, py = self._pos(i)
        return i if px <= x <= px + cs and py <= y <= py + cs + 44 else None

    def _redraw(self, i: Optional[int]) -> None:
        if i is not None and i < len(self.albums) and i in self._drawn:
            x, y = self._pos(i)
            self._draw_card(i, self.albums[i], x, y)

    def _motion(self, e) -> None:
        i = self._index_at(e)
        if i != self.hover:
            old, self.hover = self.hover, i
            for j in (old, i):
                self._redraw(j)
            self.canvas.configure(cursor="hand2" if i is not None else "")

    def _click(self, e) -> None:
        self.canvas.focus_set()
        i = self._index_at(e)
        if self.multi and self.select_mode:  # select mode: a click selects or unselects the card
            if i is not None:
                self.toggle(i)
            return
        if self.selected:
            self.clear_selection()
        if i is not None:
            self.on_open(self.albums[i])

    def set_select_mode(self, on: bool) -> None:
        """Select mode (2.3): a click toggles a card instead of opening it.  Leaving it
        clears the selection."""
        self.select_mode = bool(on) and self.multi
        if not self.select_mode:
            self.clear_selection()

    def _toggle_click(self, e) -> str:
        i = self._index_at(e)
        if i is None or not self.multi:
            return self._click(e) or "break"
        self.toggle(i)
        return "break"

    def toggle(self, i: int) -> None:
        """Select or unselect card i (Ctrl/Cmd-click)."""
        key = self.albums[i]["key"]
        if key in self.selected:
            self.selected.discard(key)
        else:
            self.selected.add(key)
        self._redraw(i)
        if self.on_selection is not None:
            self.on_selection()

    def clear_selection(self) -> None:
        old, self.selected = self.selected, set()
        for key in old:
            pos = self.positions.get(key)
            if pos is not None:
                self._redraw(pos[2])
        if old and self.on_selection is not None:
            self.on_selection()

    def selected_items(self) -> List[dict]:
        return [a for a in self.albums if a["key"] in self.selected]

    def _context(self, e) -> None:
        i = self._index_at(e)
        if i is not None and self.on_context is not None:
            self.on_context(i, e)


__all__ = ["SongList", "Column", "AlbumGrid", "CoverCache", "draw_cover", "draw_mosaic", "fmt_time", "bind_wheel",
           "scroll_units", "show_scrollbar", "rounded_photo"]
