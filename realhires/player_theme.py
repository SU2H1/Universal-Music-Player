"""Look of the 2.0 player (Tkinter): colours, fonts, ttk styles, vector icons and
a few small Canvas widgets (icon buttons, sliders, tooltips).

Icons are drawn as shapes on a Canvas, never as emoji or rare Unicode symbols,
which several Linux fonts cannot show.  The fonts follow docs/player-spec.md:
on macOS the system font (SF Pro), on Windows and Linux the bundled Inter
(realhires/assets/fonts/InterVariable.ttf, SIL Open Font License), loaded for
this program only; Settings → Appearance → Font overrides both.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Tuple

from .i18n import N_
from .probe import external_env

FONT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts", "InterVariable.ttf")
DEFAULT_FONT_LABEL = N_("SF Pro (macOS) / Inter")  # shown as tr(DEFAULT_FONT_LABEL)

def mix(a: str, b: str, t: float) -> str:
    """The colour t of the way from a to b (#rrggbb)."""
    ra, ga, ba = (int(a[i:i + 2], 16) for i in (1, 3, 5))
    rb, gb, bb = (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t))


def luminance(c: str) -> float:
    r, g, b = (int(c[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def palette(theme: dict) -> Dict[str, str]:
    """Every colour the app draws with, from a theme's eight (themes.COLOR_KEYS)."""
    c, dark = theme["colors"], bool(theme.get("dark"))
    bg, text, accent = c["background"], c["text"], c["accent"]
    return {
        "bg": bg, "sidebar": c["sidebar"], "text": text, "text2": c["secondary"], "sep": c["separator"],
        "accent": accent, "lcd": c["panel"], "side_sel": c["hover"], "field": c["hover"], "ph": c["hover"],
        "hover": mix(bg, c["hover"], 0.7), "zebra": mix(bg, c["hover"], 0.3),
        "ph_icon": mix(c["hover"], c["secondary"], 0.6), "disabled": mix(bg, c["secondary"], 0.45),
        "on_accent": "#1d1d1f" if luminance(accent) > 0.62 else "#ffffff",
        "accent_dark": mix(accent, "#000000", 0.12), "seek": c["separator"], "seek_fill": c["secondary"],
        "error": "#ff453a" if dark else "#d70015", "knob": mix(text, "#ffffff", 0.2) if dark else "#ffffff",
        "badge": c["secondary"],
    }


def _builtin(name: str) -> Dict[str, str]:
    from .themes import BUILTIN

    return palette(BUILTIN[name])


LIGHT = _builtin("Light")
DARK = _builtin("Dark")

# pixel sizes (negative Tk sizes are pixels) and weights of the named fonts
FONT_SPECS = {
    "title": (-28, "bold"), "big": (-30, "bold"), "h1": (-22, "bold"), "h2": (-17, "bold"),
    "body": (-13, "normal"), "body_b": (-13, "bold"), "small": (-11, "normal"), "small_b": (-11, "bold"),
    "side": (-13, "normal"), "side_head": (-11, "bold"), "badge": (-9, "bold"), "time": (-10, "normal"),
    "lyrics": (-24, "normal"), "lyrics_b": (-24, "bold"), "plain": (-17, "normal"), "lcd_title": (-13, "bold"),
    "lcd_sub": (-12, "normal"), "card_title": (-13, "bold"), "card_sub": (-12, "normal"),
    "tile": (-14, "bold"), "stat_big": (-30, "bold"), "stat_label": (-12, "bold"), "stat_detail": (-12, "normal"),
}


# ------------------------------------------------------------------ fonts ---
_LOADED: Dict[str, bool] = {}


def load_bundled_font() -> bool:
    """Make the bundled Inter available to this program only (call before tk.Tk()).
    Windows: AddFontResourceExW(FR_PRIVATE); Linux and other X11 systems: fontconfig's
    FcConfigAppFontAddFile.  macOS uses its own system font.  False when it failed."""
    if "inter" in _LOADED:
        return _LOADED["inter"]
    ok = False
    if os.path.isfile(FONT_FILE) and sys.platform != "darwin":
        try:
            import ctypes

            if sys.platform.startswith("win"):
                FR_PRIVATE = 0x10
                ok = ctypes.windll.gdi32.AddFontResourceExW(FONT_FILE, FR_PRIVATE, 0) > 0  # type: ignore[attr-defined]
            else:
                fc = ctypes.CDLL("libfontconfig.so.1")
                fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                fc.FcConfigAppFontAddFile.restype = ctypes.c_int
                ok = bool(fc.FcConfigAppFontAddFile(None, os.fsencode(FONT_FILE)))
        except (OSError, AttributeError):
            ok = False
    _LOADED["inter"] = ok
    return ok


def default_family(root: tk.Misc) -> str:
    """SF Pro on macOS, Inter elsewhere (when it could be loaded), else the UI font."""
    families = set(tkfont.families(root))
    if sys.platform == "darwin":
        for name in ("SF Pro Display", "SF Pro", "SF Pro Text", ".AppleSystemUIFont"):
            if name in families:
                return name
    else:
        for name in ("Inter Variable", "Inter"):
            if name in families:
                return name
    return tkfont.nametofont("TkDefaultFont", root).actual("family")


def font_families(root: tk.Misc) -> List[str]:
    """Installed font families for Settings → Appearance → Font (no vertical '@' fonts)."""
    return sorted({f for f in tkfont.families(root) if f and not f.startswith("@")}, key=str.casefold)


# --------------------------------------------------------------- dark mode ---
def system_is_dark() -> bool:
    """The system's dark mode where it can be read (macOS, Windows, GNOME), else light."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True,
                                 timeout=3, env=external_env()).stdout
            return "dark" in out.lower()
        if sys.platform.startswith("win"):
            import winreg

            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, text=True, timeout=3, env=external_env()).stdout
        return "dark" in out.lower()
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


# ------------------------------------------------------------------ theme ---
class Theme:
    """The current colours and fonts.  Widgets register which option takes which
    colour (`reg`), Canvas widgets register a redraw hook (`on_change`), and
    `apply()` repaints all of them at once when the theme or the font changes."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.dark = False
        self.name = "Light"
        self.colors: Dict[str, str] = dict(LIGHT)
        self.family = default_family(root)
        self.fonts: Dict[str, tkfont.Font] = {}
        for name, (size, weight) in FONT_SPECS.items():
            self.fonts[name] = tkfont.Font(root, name=f"rhr_{name}", family=self.family, size=size, weight=weight)
        self._items: List[tuple] = []
        self._hooks: List[Callable[[], None]] = []
        self.style = ttk.Style(root)
        if "clam" in self.style.theme_names():
            self.style.theme_use("clam")

    def c(self, name: str) -> str:
        return self.colors[name]

    def f(self, name: str) -> tkfont.Font:
        return self.fonts[name]

    def reg(self, widget, **options):
        """widget.configure(option=colour of the role) now and whenever the theme changes."""
        self._items.append((widget, options))
        self._paint(widget, options)
        return widget

    def _paint(self, widget, options) -> None:
        try:
            widget.configure(**{opt: self.colors[role] for opt, role in options.items()})
        except tk.TclError:
            pass

    def on_change(self, fn: Callable[[], None], owner: Optional[tk.Misc] = None) -> None:
        """Call `fn` after every theme or font change (forgotten once `owner` is destroyed)."""
        self._hooks.append((owner, fn))

    def configure(self, theme_setting: str, family_setting: str) -> None:
        """Choose the palette and the font family (without repainting): the theme of that
        name (built-in, the user's own or from the themes folder, themes.resolve()); unknown names follow
        the system."""
        from . import themes

        own = themes.load_custom()
        custom = own + themes.load_folder(own)  # and the theme files of the themes folder
        known = theme_setting in themes.BUILTIN or any(t["name"] == theme_setting for t in custom)
        theme = themes.resolve(theme_setting, False if known else system_is_dark(), custom)
        self.name = theme["name"]
        self.dark = bool(theme["dark"])
        self.colors = palette(theme)
        families = set(tkfont.families(self.root))
        self.family = family_setting if family_setting and family_setting in families else default_family(self.root)
        for f in self.fonts.values():
            f.configure(family=self.family)

    def apply(self, theme_setting: str, family_setting: str) -> None:
        self.configure(theme_setting, family_setting)
        self.styles()
        alive = []
        for widget, options in self._items:
            try:
                if widget.winfo_exists():
                    self._paint(widget, options)
                    alive.append((widget, options))
            except tk.TclError:
                continue
        self._items = alive
        hooks = []
        for owner, fn in self._hooks:
            try:
                if owner is not None and not owner.winfo_exists():
                    continue
                hooks.append((owner, fn))
                fn()
            except tk.TclError:
                pass
        self._hooks = hooks

    def prune(self) -> None:
        """Forget destroyed widgets (pages that rebuild themselves call this)."""
        def alive(w) -> bool:
            try:
                return w is None or bool(w.winfo_exists())
            except tk.TclError:
                return False
        self._items = [(w, o) for w, o in self._items if alive(w)]
        self._hooks = [(w, fn) for w, fn in self._hooks if alive(w)]

    def _checkboxes(self) -> None:
        """Rounded check boxes with a tick (clam's own draw an X), as images that are
        redrawn in the new colours when the theme changes."""
        c = self.colors
        looks = {"off": (c["bg"], c["text2"], None), "on": (c["accent"], c["accent"], c["on_accent"]),
                 "offd": (c["bg"], c["disabled"], None), "ond": (c["disabled"], c["disabled"], c["bg"])}
        first = not hasattr(self, "_check_img")
        if first:
            self._check_img = {k: tk.PhotoImage(master=self.root, width=24, height=16) for k in looks}
        for key, (fill, border, mark) in looks.items():
            _paint_checkbox(self._check_img[key], c["bg"], fill, border, mark)
        if first:
            img = self._check_img
            self.style.element_create("P.Checkbutton.indicator", "image", img["off"],
                                      ("selected", "disabled", img["ond"]), ("selected", img["on"]),
                                      ("disabled", img["offd"]))
            self.style.layout("P.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
                ("P.Checkbutton.indicator", {"side": "left", "sticky": ""}),
                ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                    ("Checkbutton.label", {"sticky": "nswe"})]})]})])

    def styles(self) -> None:
        """The ttk styles of the player (the Quality check page keeps the standard ones)."""
        c, s = self.colors, self.style
        body = self.fonts["body"]
        s.configure("P.TFrame", background=c["bg"])
        s.configure("Side.TFrame", background=c["sidebar"])
        s.configure("P.TLabel", background=c["bg"], foreground=c["text"], font=body)
        s.configure("P2.TLabel", background=c["bg"], foreground=c["text2"], font=self.fonts["small"])
        s.configure("P.Treeview", background=c["bg"], fieldbackground=c["bg"], foreground=c["text"], font=body,
                    rowheight=30, borderwidth=0, relief="flat")
        s.map("P.Treeview", background=[("selected", c["accent"])], foreground=[("selected", c["on_accent"])])
        s.layout("P.Treeview", [("P.Treeview.treearea", {"sticky": "nswe"})])
        s.configure("P.Treeview.Heading", background=c["bg"], foreground=c["text2"], relief="flat",
                    font=self.fonts["small_b"], borderwidth=0)
        s.map("P.Treeview.Heading", background=[("active", c["hover"])])
        for orient in ("Vertical", "Horizontal"):
            for prefix, trough, thumb in (("P", c["bg"], c["sep"]), ("PHide", c["bg"], c["bg"]),
                                          ("PHideSide", c["sidebar"], c["sidebar"])):
                name = f"{prefix}.{orient}.TScrollbar"
                s.layout(name, [(f"{orient}.Scrollbar.trough", {"sticky": "nswe", "children": [
                    (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
                s.configure(name, troughcolor=trough, background=thumb, bordercolor=trough, lightcolor=thumb,
                            darkcolor=thumb, arrowsize=8, gripcount=0, relief="flat", borderwidth=0)
                s.map(name, background=[("active", c["text2"] if prefix == "P" else thumb)])
        for name, bg in (("P.TEntry", c["field"]), ("Search.TEntry", c["field"])):
            s.configure(name, fieldbackground=bg, foreground=c["text"], insertcolor=c["text"], bordercolor=bg,
                        lightcolor=bg, darkcolor=bg, padding=(6, 3), font=body)
            s.map(name, bordercolor=[("focus", c["accent"])], lightcolor=[("focus", bg)])
        s.configure("P.TCombobox", fieldbackground=c["field"], background=c["field"], foreground=c["text"],
                    arrowcolor=c["text2"], bordercolor=c["field"], lightcolor=c["field"], darkcolor=c["field"],
                    padding=(6, 3), selectbackground=c["field"], selectforeground=c["text"])
        s.map("P.TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["text"])],
              selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["text"])],
              bordercolor=[("focus", c["accent"])])
        s.configure("P.TSpinbox", fieldbackground=c["field"], background=c["field"], foreground=c["text"],
                    arrowcolor=c["text2"], bordercolor=c["field"], lightcolor=c["field"], darkcolor=c["field"],
                    insertcolor=c["text"], padding=(6, 2))
        s.map("P.TSpinbox", bordercolor=[("focus", c["accent"])])
        self.root.option_add("*TCombobox*Listbox.background", c["bg"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["on_accent"])
        self.root.option_add("*TCombobox*Listbox.font", body)
        self._checkboxes()
        s.configure("P.TCheckbutton", background=c["bg"], foreground=c["text"], font=body, focuscolor=c["bg"],
                    padding=(0, 2))
        s.map("P.TCheckbutton", background=[("active", c["bg"])], foreground=[("disabled", c["disabled"])])
        s.configure("Accent.TButton", background=c["accent"], foreground=c["on_accent"], bordercolor=c["accent"],
                    lightcolor=c["accent"], darkcolor=c["accent"], focuscolor=c["accent"], padding=(16, 6),
                    font=self.fonts["body_b"], relief="flat")
        s.map("Accent.TButton", background=[("active", c["accent_dark"]), ("disabled", c["disabled"])],
              lightcolor=[("active", c["accent_dark"])], darkcolor=[("active", c["accent_dark"])])
        s.configure("P.TButton", background=c["lcd"], foreground=c["text"], bordercolor=c["sep"],
                    lightcolor=c["lcd"], darkcolor=c["lcd"], focuscolor=c["lcd"], padding=(12, 5), font=body,
                    relief="flat")
        s.map("P.TButton", background=[("active", c["hover"]), ("disabled", c["bg"])],
              foreground=[("disabled", c["disabled"])])
        s.configure("P.TNotebook", background=c["bg"], borderwidth=1, tabmargins=(0, 4, 0, 0),
                    bordercolor=c["sep"], lightcolor=c["bg"], darkcolor=c["bg"])
        s.configure("P.TNotebook.Tab", background=c["lcd"], foreground=c["text2"], padding=(14, 5), borderwidth=1,
                    font=self.fonts["body_b"], lightcolor=c["lcd"], darkcolor=c["lcd"], bordercolor=c["sep"])
        s.map("P.TNotebook.Tab", background=[("selected", c["bg"])], foreground=[("selected", c["text"])],
              lightcolor=[("selected", c["bg"])], darkcolor=[("selected", c["bg"])])
        s.configure("P.TRadiobutton", background=c["bg"], foreground=c["text"], font=body,
                    indicatorbackground=c["field"], focuscolor=c["bg"])
        s.map("P.TRadiobutton", indicatorbackground=[("selected", c["accent"])])


# --------------------------------------------------------------- drawing ---
def _rgb(color: str) -> Tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _inside_round(px: float, py: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    return x0 <= px <= x1 and y0 <= py <= y1 and (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def _near_tick(px: float, py: float, width: float = 1.05) -> bool:
    for (ax, ay), (bx, by) in (((4.0, 8.3), (6.9, 11.2)), ((6.9, 11.2), (12.2, 5.0))):
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        if (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2 <= width * width:
            return True
    return False


def _paint_checkbox(img: tk.PhotoImage, bg: str, fill: str, border: str, mark: Optional[str]) -> None:
    """A 16 px rounded check box (anti-aliased against `bg`) in a 24 px wide image whose
    right 8 px stay transparent, the gap before the label."""
    cols = {"bg": _rgb(bg), "fill": _rgb(fill), "border": _rgb(border), "mark": _rgb(mark or fill)}
    rows = []
    n = 4
    for y in range(16):
        row = []
        for x in range(16):
            acc = [0.0, 0.0, 0.0]
            for sy in range(n):
                for sx in range(n):
                    px, py = x + (sx + 0.5) / n, y + (sy + 0.5) / n
                    if not _inside_round(px, py, 0.5, 0.5, 15.5, 15.5, 4.0):
                        k = "bg"
                    elif mark and _near_tick(px, py):
                        k = "mark"
                    elif _inside_round(px, py, 1.7, 1.7, 14.3, 14.3, 3.0):
                        k = "fill"
                    else:
                        k = "border"
                    for i in range(3):
                        acc[i] += cols[k][i]
            row.append("#%02x%02x%02x" % tuple(int(round(v / (n * n))) for v in acc))
        rows.append("{" + " ".join(row) + "}")
    img.put(" ".join(rows), to=(0, 0))


def round_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, r: float, **kw):
    """A filled rounded rectangle (a smoothed polygon)."""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r, x2, y2 - r,
           x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
           x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def _heart_points(cx: float, cy: float, s: float) -> List[float]:
    pts = []
    k = s / 34.0
    for i in range(48):
        t = 2 * math.pi * i / 48
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts += [cx + x * k, cy - y * k - s * 0.04]
    return pts


def draw_icon(c: tk.Canvas, name: str, cx: float, cy: float, s: float, color: str, tag: str = "icon",
              filled: bool = False, badge: str = "", bg: str = "", badge_font=None, level: float = 1.0) -> None:
    """Draw the icon `name`, about s pixels tall, centred on (cx, cy)."""
    w = max(1.5, s * 0.09)
    kw = {"tags": tag}
    if name == "play":
        c.create_polygon(cx - 0.32 * s, cy - 0.44 * s, cx - 0.32 * s, cy + 0.44 * s, cx + 0.48 * s, cy,
                         fill=color, outline=color, joinstyle="round", width=1, **kw)
    elif name == "pause":
        for dx in (-0.2, 0.2):
            c.create_rectangle(cx + (dx - 0.11) * s, cy - 0.42 * s, cx + (dx + 0.11) * s, cy + 0.42 * s,
                               fill=color, outline=color, **kw)
    elif name in ("next", "prev"):
        sign = 1 if name == "next" else -1
        for x0 in (-0.5, 0.0):
            a, b = cx + sign * x0 * s, cx + sign * (x0 + 0.5) * s
            c.create_polygon(a, cy - 0.32 * s, a, cy + 0.32 * s, b, cy, fill=color, outline=color, **kw)
    elif name == "shuffle":
        ah = (s * 0.22, s * 0.26, s * 0.1)
        c.create_line(cx - 0.48 * s, cy + 0.28 * s, cx - 0.18 * s, cy + 0.28 * s, cx + 0.12 * s, cy - 0.28 * s,
                      cx + 0.48 * s, cy - 0.28 * s, width=w, fill=color, arrow="last", arrowshape=ah, **kw)
        c.create_line(cx - 0.48 * s, cy - 0.28 * s, cx - 0.18 * s, cy - 0.28 * s, cx + 0.12 * s, cy + 0.28 * s,
                      cx + 0.48 * s, cy + 0.28 * s, width=w, fill=color, arrow="last", arrowshape=ah, **kw)
    elif name == "repeat":
        ah = (s * 0.2, s * 0.24, s * 0.1)
        c.create_line(cx - 0.44 * s, cy + 0.08 * s, cx - 0.44 * s, cy - 0.26 * s, cx + 0.4 * s, cy - 0.26 * s,
                      width=w, fill=color, arrow="last", arrowshape=ah, joinstyle="round", **kw)
        c.create_line(cx + 0.44 * s, cy - 0.08 * s, cx + 0.44 * s, cy + 0.26 * s, cx - 0.4 * s, cy + 0.26 * s,
                      width=w, fill=color, arrow="last", arrowshape=ah, joinstyle="round", **kw)
        if badge == "1":
            c.create_text(cx, cy + 0.01 * s, text="1", fill=color, font=badge_font, **kw)
        elif badge == "stop":
            q = 0.1 * s
            c.create_rectangle(cx - q, cy - q, cx + q, cy + q, fill=color, outline=color, **kw)
    elif name == "heart":
        pts = _heart_points(cx, cy, s)
        if filled:
            c.create_polygon(pts, fill=color, outline=color, smooth=True, **kw)
        else:
            c.create_polygon(pts, fill="", outline=color, width=max(1.2, s * 0.08), smooth=True, **kw)
    elif name == "lyrics":
        x1, y1, x2, y2 = cx - 0.46 * s, cy - 0.4 * s, cx + 0.46 * s, cy + 0.22 * s
        c.create_line(x1 + 0.15 * s, y1, x2 - 0.15 * s, y1, x2, y1 + 0.15 * s, x2, y2 - 0.15 * s, x2 - 0.15 * s, y2,
                      cx - 0.1 * s, y2, cx - 0.3 * s, cy + 0.44 * s, cx - 0.28 * s, y2, x1 + 0.15 * s, y2,
                      x1, y2 - 0.15 * s, x1, y1 + 0.15 * s, x1 + 0.15 * s, y1, width=w, fill=color,
                      joinstyle="round", **kw)
        for dy, ln in ((-0.18, 0.26), (0.0, 0.18)):
            c.create_line(cx - 0.26 * s, cy + dy * s, cx + ln * s, cy + dy * s, width=w, fill=color,
                          capstyle="round", **kw)
    elif name == "queue":
        for i in (-1, 0, 1):
            y = cy + i * 0.3 * s
            r = 0.07 * s
            c.create_oval(cx - 0.44 * s - r, y - r, cx - 0.44 * s + r, y + r, fill=color, outline=color, **kw)
            c.create_line(cx - 0.24 * s, y, cx + 0.46 * s, y, width=w, fill=color, capstyle="round", **kw)
    elif name == "eq":
        for dx, k in ((-0.32, 0.18), (0.0, -0.2), (0.32, 0.08)):
            x = cx + dx * s
            c.create_line(x, cy - 0.44 * s, x, cy + 0.44 * s, width=max(1.0, w * 0.8), fill=color, **kw)
            c.create_rectangle(x - 0.1 * s, cy + (k - 0.06) * s, x + 0.1 * s, cy + (k + 0.06) * s, fill=color,
                               outline=color, **kw)
    elif name == "speaker":
        c.create_polygon(cx - 0.46 * s, cy - 0.14 * s, cx - 0.26 * s, cy - 0.14 * s, cx - 0.02 * s, cy - 0.38 * s,
                         cx - 0.02 * s, cy + 0.38 * s, cx - 0.26 * s, cy + 0.14 * s, cx - 0.46 * s, cy + 0.14 * s,
                         fill=color, outline=color, **kw)
        if level <= 0:
            q = 0.14 * s
            for a, b in ((-1, 1), (1, -1)):
                c.create_line(cx + 0.3 * s - q, cy + a * q, cx + 0.3 * s + q, cy + b * q, width=w, fill=color,
                              capstyle="round", **kw)
        else:
            for r, need in ((0.2, 0.0), (0.38, 0.5)):
                if level > need:
                    rr = r * s
                    c.create_arc(cx - 0.02 * s - rr, cy - rr, cx - 0.02 * s + rr, cy + rr, start=-50, extent=100,
                                 style="arc", outline=color, width=w, **kw)
    elif name == "search":
        r = 0.3 * s
        c.create_oval(cx - 0.08 * s - r, cy - 0.08 * s - r, cx - 0.08 * s + r, cy - 0.08 * s + r, outline=color,
                      width=w, **kw)
        c.create_line(cx + 0.15 * s, cy + 0.15 * s, cx + 0.44 * s, cy + 0.44 * s, width=w * 1.2, fill=color,
                      capstyle="round", **kw)
    elif name == "clock":
        r = 0.44 * s
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=w, **kw)
        c.create_line(cx, cy - 0.26 * s, cx, cy, cx + 0.18 * s, cy + 0.1 * s, width=w, fill=color,
                      capstyle="round", joinstyle="round", **kw)
    elif name == "artist":
        r = 0.2 * s
        c.create_oval(cx - r, cy - 0.42 * s, cx + r, cy - 0.42 * s + 2 * r, outline=color, width=w, **kw)
        c.create_arc(cx - 0.4 * s, cy + 0.04 * s, cx + 0.4 * s, cy + 0.84 * s, start=0, extent=180, style="arc",
                     outline=color, width=w, **kw)
        c.create_line(cx - 0.4 * s, cy + 0.44 * s, cx + 0.4 * s, cy + 0.44 * s, width=w, fill=color, **kw)
    elif name == "album":
        round_rect(c, cx - 0.44 * s, cy - 0.44 * s, cx + 0.44 * s, cy + 0.44 * s, 0.12 * s, fill="", outline=color,
                   width=w, **kw)
        r = 0.2 * s
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=w, **kw)
    elif name == "note":
        c.create_oval(cx - 0.36 * s, cy + 0.14 * s, cx + 0.02 * s, cy + 0.42 * s, fill=color, outline=color, **kw)
        c.create_line(cx + 0.0 * s, cy + 0.28 * s, cx + 0.0 * s, cy - 0.42 * s, cx + 0.34 * s, cy - 0.26 * s,
                      width=w, fill=color, joinstyle="round", capstyle="round", **kw)
    elif name == "genre":
        q = 0.38 * s
        for dx in (-1, 1):
            for dy in (-1, 1):
                x0, y0 = cx + (dx * 0.23 - 0.17) * s, cy + (dy * 0.23 - 0.17) * s
                round_rect(c, x0, y0, x0 + q - 0.04 * s, y0 + q - 0.04 * s, 0.07 * s, fill="", outline=color,
                           width=w, **kw)
    elif name == "gear":
        pts = []
        for i in range(32):
            a = 2 * math.pi * i / 32
            r = (0.46 if (i // 2) % 2 == 0 else 0.33) * s
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        c.create_polygon(pts, fill="", outline=color, width=w, joinstyle="round", **kw)
        r = 0.14 * s
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=w, **kw)
    elif name == "check":
        r = 0.44 * s
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=w, **kw)
        c.create_line(cx - 0.2 * s, cy + 0.02 * s, cx - 0.05 * s, cy + 0.18 * s, cx + 0.22 * s, cy - 0.16 * s,
                      width=w, fill=color, capstyle="round", joinstyle="round", **kw)
    elif name == "close":
        q = 0.3 * s
        c.create_line(cx - q, cy - q, cx + q, cy + q, width=w, fill=color, capstyle="round", **kw)
        c.create_line(cx - q, cy + q, cx + q, cy - q, width=w, fill=color, capstyle="round", **kw)
    elif name == "back":
        c.create_line(cx + 0.16 * s, cy - 0.36 * s, cx - 0.2 * s, cy, cx + 0.16 * s, cy + 0.36 * s, width=w * 1.2,
                      fill=color, capstyle="round", joinstyle="round", **kw)
    elif name == "info":
        r = 0.44 * s
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=w, **kw)
        c.create_line(cx, cy - 0.04 * s, cx, cy + 0.24 * s, width=w * 1.1, fill=color, capstyle="round", **kw)
        q = 0.05 * s
        c.create_oval(cx - q, cy - 0.22 * s - q, cx + q, cy - 0.22 * s + q, fill=color, outline=color, **kw)
    elif name == "library":  # books on a shelf
        for x0, x1, top in ((-0.44, -0.24, -0.34), (-0.18, 0.0, -0.42)):
            c.create_rectangle(cx + x0 * s, cy + top * s, cx + x1 * s, cy + 0.42 * s, outline=color, width=w, **kw)
        c.create_polygon(cx + 0.08 * s, cy - 0.3 * s, cx + 0.24 * s, cy - 0.36 * s, cx + 0.46 * s, cy + 0.36 * s,
                         cx + 0.3 * s, cy + 0.42 * s, fill="", outline=color, width=w, joinstyle="round", **kw)
    elif name == "folder":
        c.create_line(cx - 0.46 * s, cy + 0.36 * s, cx - 0.46 * s, cy - 0.34 * s, cx - 0.12 * s, cy - 0.34 * s,
                      cx - 0.02 * s, cy - 0.22 * s, cx + 0.46 * s, cy - 0.22 * s, cx + 0.46 * s, cy + 0.36 * s,
                      cx - 0.46 * s, cy + 0.36 * s, width=w, fill=color, joinstyle="round", **kw)
        c.create_line(cx - 0.46 * s, cy - 0.1 * s, cx + 0.46 * s, cy - 0.1 * s, width=w, fill=color, **kw)
    elif name == "mic":
        round_rect(c, cx - 0.16 * s, cy - 0.46 * s, cx + 0.16 * s, cy + 0.1 * s, 0.16 * s, fill="", outline=color,
                   width=w, **kw)
        c.create_arc(cx - 0.3 * s, cy - 0.28 * s, cx + 0.3 * s, cy + 0.26 * s, start=180, extent=180, style="arc",
                     outline=color, width=w, **kw)
        c.create_line(cx, cy + 0.26 * s, cx, cy + 0.44 * s, width=w, fill=color, **kw)
        c.create_line(cx - 0.18 * s, cy + 0.44 * s, cx + 0.18 * s, cy + 0.44 * s, width=w, fill=color,
                      capstyle="round", **kw)
    elif name == "calendar":
        round_rect(c, cx - 0.44 * s, cy - 0.36 * s, cx + 0.44 * s, cy + 0.42 * s, 0.1 * s, fill="", outline=color,
                   width=w, **kw)
        c.create_line(cx - 0.44 * s, cy - 0.12 * s, cx + 0.44 * s, cy - 0.12 * s, width=w, fill=color, **kw)
        for dx in (-0.22, 0.22):
            c.create_line(cx + dx * s, cy - 0.46 * s, cx + dx * s, cy - 0.26 * s, width=w, fill=color,
                          capstyle="round", **kw)
        q = 0.05 * s
        for dx in (-0.22, 0.0, 0.22):
            for dy in (0.06, 0.24):
                c.create_oval(cx + dx * s - q, cy + dy * s - q, cx + dx * s + q, cy + dy * s + q, fill=color,
                              outline=color, **kw)
    elif name == "piano":  # composers
        round_rect(c, cx - 0.46 * s, cy - 0.36 * s, cx + 0.46 * s, cy + 0.36 * s, 0.08 * s, fill="", outline=color,
                   width=w, **kw)
        for dx in (-0.15, 0.15):
            c.create_line(cx + dx * s, cy + 0.0 * s, cx + dx * s, cy + 0.36 * s, width=w, fill=color, **kw)
        for dx in (-0.31, 0.0, 0.31):
            c.create_rectangle(cx + (dx - 0.07) * s, cy - 0.36 * s, cx + (dx + 0.07) * s, cy + 0.04 * s, fill=color,
                               outline=color, **kw)
    elif name == "playlist":
        for dy, ln in ((-0.32, 0.44), (-0.1, 0.44), (0.12, 0.06)):
            c.create_line(cx - 0.44 * s, cy + dy * s, cx + ln * s, cy + dy * s, width=w, fill=color,
                          capstyle="round", **kw)
        c.create_oval(cx + 0.06 * s, cy + 0.22 * s, cx + 0.3 * s, cy + 0.42 * s, fill=color, outline=color, **kw)
        c.create_line(cx + 0.29 * s, cy + 0.32 * s, cx + 0.29 * s, cy - 0.02 * s, cx + 0.44 * s, cy + 0.06 * s,
                      width=w, fill=color, joinstyle="round", capstyle="round", **kw)
    elif name in ("thumbs_down", "thumbs_up"):
        sign = 1 if name == "thumbs_up" else -1
        pts = [(-0.2, 0.44), (-0.2, -0.04), (0.0, -0.44), (0.1, -0.44), (0.12, -0.34), (0.06, -0.1), (0.36, -0.1),
               (0.44, -0.02), (0.38, 0.38), (0.3, 0.44)]
        flat = []
        for x, y in pts:
            flat += [cx + x * s, cy + sign * y * s]
        c.create_polygon(flat, fill=color if filled else "", outline=color, width=max(1.2, s * 0.08),
                         joinstyle="round", **kw)
        c.create_rectangle(cx - 0.46 * s, cy + sign * -0.04 * s, cx - 0.28 * s, cy + sign * 0.44 * s,
                           fill=color if filled else "", outline=color, width=max(1.2, s * 0.08), **kw)
    elif name == "history":
        r = 0.42 * s
        c.create_arc(cx - r, cy - r, cx + r, cy + r, start=120, extent=-300, style="arc", outline=color, width=w,
                     **kw)
        ax, ay = cx + r * math.cos(math.radians(120)), cy - r * math.sin(math.radians(120))
        c.create_polygon(ax - 0.16 * s, ay - 0.02 * s, ax + 0.06 * s, ay - 0.12 * s, ax + 0.02 * s, ay + 0.12 * s,
                         fill=color, outline=color, **kw)
        c.create_line(cx, cy - 0.22 * s, cx, cy, cx + 0.16 * s, cy + 0.1 * s, width=w, fill=color,
                      capstyle="round", joinstyle="round", **kw)
    elif name == "stats":
        for dx, top in ((-0.33, 0.1), (-0.11, -0.3), (0.11, -0.05), (0.33, -0.42)):
            c.create_rectangle(cx + (dx - 0.08) * s, cy + top * s, cx + (dx + 0.08) * s, cy + 0.42 * s, fill=color,
                               outline=color, **kw)
    elif name == "plus":
        q = 0.36 * s
        c.create_line(cx - q, cy, cx + q, cy, width=w * 1.2, fill=color, capstyle="round", **kw)
        c.create_line(cx, cy - q, cx, cy + q, width=w * 1.2, fill=color, capstyle="round", **kw)
    elif name == "dots":
        q = 0.08 * s
        for dx in (-0.3, 0.0, 0.3):
            c.create_oval(cx + dx * s - q, cy - q, cx + dx * s + q, cy + q, fill=color, outline=color, **kw)
    elif name == "chevron":
        c.create_line(cx - 0.14 * s, cy - 0.34 * s, cx + 0.18 * s, cy, cx - 0.14 * s, cy + 0.34 * s, width=w,
                      fill=color, capstyle="round", joinstyle="round", **kw)


# ---------------------------------------------------------------- widgets ---
class Tooltip:
    """A small label that appears when the pointer rests on a widget."""

    def __init__(self, widget: tk.Widget, theme: Theme, text: Callable[[], str]):
        self.widget, self.theme, self.text = widget, theme, text
        self.tip: Optional[tk.Toplevel] = None
        self.job: Optional[str] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self.job = self.widget.after(650, self._show)

    def _cancel(self) -> None:
        if self.job is not None:
            try:
                self.widget.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None

    def _show(self) -> None:
        self.job = None
        text = self.text()
        if not text:
            return
        t = self.theme
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        tk.Label(self.tip, text=text, bg=t.c("lcd"), fg=t.c("text"), font=t.f("small"), padx=8, pady=4,
                 highlightthickness=1, highlightbackground=t.c("sep")).pack()
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class IconButton(tk.Canvas):
    """A flat button showing a vector icon."""

    def __init__(self, master, theme: Theme, icon: str, command: Optional[Callable[[], None]] = None,
                 size: int = 32, icon_size: int = 18, tooltip: Optional[Callable[[], str]] = None,
                 bg_role: str = "bg", primary: bool = False):
        super().__init__(master, width=size, height=size, highlightthickness=0, bd=0, cursor="hand2")
        self.theme, self.icon, self.command, self.size, self.icon_size = theme, icon, command, size, icon_size
        self.bg_role, self.primary = bg_role, primary
        self.enabled, self.on, self.filled, self.badge, self.level = True, False, False, "", 1.0
        self.hover = False
        theme.reg(self, bg=bg_role)
        theme.on_change(self.redraw, self)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        if tooltip is not None:
            Tooltip(self, theme, tooltip)
        self.redraw()

    def _set_hover(self, h: bool) -> None:
        self.hover = h
        self.redraw()

    def _click(self, e) -> None:
        if self.enabled and self.command is not None and 0 <= e.x < self.size and 0 <= e.y < self.size:
            self.command()

    def set(self, icon: Optional[str] = None, enabled: Optional[bool] = None, on: Optional[bool] = None,
            filled: Optional[bool] = None, badge: Optional[str] = None, level: Optional[float] = None) -> None:
        for name, value in (("icon", icon), ("enabled", enabled), ("on", on), ("filled", filled), ("badge", badge),
                            ("level", level)):
            if value is not None:
                setattr(self, name, value)
        self.configure(cursor="hand2" if self.enabled else "")
        self.redraw()

    def redraw(self) -> None:
        t = self.theme
        self.delete("all")
        if not self.enabled:
            color = t.c("disabled")
        elif self.on or (self.icon in ("heart", "thumbs_down") and self.filled):
            color = t.c("accent")
        elif self.hover or self.primary:
            color = t.c("text")
        else:
            color = t.c("text2")
        if self.hover and self.enabled:
            round_rect(self, 2, 2, self.size - 2, self.size - 2, 7, fill=t.c("hover"), outline="")
        draw_icon(self, self.icon, self.size / 2, self.size / 2, self.icon_size, color, filled=self.filled,
                  badge=self.badge, badge_font=t.f("badge"), level=self.level)


class Slider(tk.Canvas):
    """A thin slider (volume, EQ bands): click or drag; double-click resets it."""

    def __init__(self, master, theme: Theme, from_: float, to: float, value: float,
                 command: Callable[[float, bool], None], length: int = 100, orient: str = "horizontal",
                 origin: Optional[float] = None, reset: Optional[float] = None, bg_role: str = "bg",
                 thickness: int = 20):
        w, h = (length, thickness) if orient == "horizontal" else (thickness, length)
        super().__init__(master, width=w, height=h, highlightthickness=0, bd=0, cursor="hand2")
        self.theme, self.lo, self.hi, self.value, self.command = theme, from_, to, value, command
        self.orient, self.origin, self.reset_to = orient, (from_ if origin is None else origin), reset
        self.enabled = True
        self.pad = 8
        theme.reg(self, bg=bg_role)
        theme.on_change(self.redraw, self)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", lambda e: self._drag(e, False))
        self.bind("<B1-Motion>", lambda e: self._drag(e, False))
        self.bind("<ButtonRelease-1>", lambda e: self._drag(e, True))
        self.bind("<Double-Button-1>", self._reset)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda e: self._step(1))
        self.bind("<Button-5>", lambda e: self._step(-1))

    def _length(self) -> float:
        n = self.winfo_width() if self.orient == "horizontal" else self.winfo_height()
        return max(1.0, (n if n > 1 else int(self.cget("width" if self.orient == "horizontal" else "height")))
                   - 2 * self.pad)

    def _pos(self, v: float) -> float:
        frac = (v - self.lo) / (self.hi - self.lo)
        if self.orient == "horizontal":
            return self.pad + frac * self._length()
        return self.pad + (1 - frac) * self._length()

    def _value_at(self, e) -> float:
        if self.orient == "horizontal":
            frac = (e.x - self.pad) / self._length()
        else:
            frac = 1 - (e.y - self.pad) / self._length()
        return self.lo + min(max(frac, 0.0), 1.0) * (self.hi - self.lo)

    def _drag(self, e, final: bool) -> None:
        if not self.enabled:
            return
        self.value = self._value_at(e)
        self.redraw()
        self.command(self.value, final)

    def _reset(self, _e=None) -> None:
        if self.enabled and self.reset_to is not None:
            self.value = self.reset_to
            self.redraw()
            self.command(self.value, True)

    def _step(self, direction: int) -> None:
        if self.enabled:
            self.value = min(max(self.value + direction * (self.hi - self.lo) / 40, self.lo), self.hi)
            self.redraw()
            self.command(self.value, True)

    def _wheel(self, e) -> None:
        self._step(1 if e.delta > 0 else -1)

    def set(self, value: float, enabled: Optional[bool] = None) -> None:
        self.value = min(max(value, self.lo), self.hi)
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "")
        self.redraw()

    def redraw(self) -> None:
        t = self.theme
        self.delete("all")
        w = self.winfo_width() if self.winfo_width() > 1 else int(self.cget("width"))
        h = self.winfo_height() if self.winfo_height() > 1 else int(self.cget("height"))
        track = t.c("seek")
        fill = t.c("disabled") if not self.enabled else (t.c("accent") if self.orient == "vertical"
                                                          else t.c("seek_fill"))
        a, b, o = self._pos(self.lo), self._pos(self.hi), self._pos(self.origin)
        v = self._pos(self.value)
        if self.orient == "horizontal":
            y = h / 2
            self.create_line(a, y, b, y, fill=track, width=4, capstyle="round")
            self.create_line(o, y, v, y, fill=fill, width=4, capstyle="round")
            knob = (v, y)
        else:
            x = w / 2
            self.create_line(x, a, x, b, fill=track, width=4, capstyle="round")
            self.create_line(x - 5, o, x + 5, o, fill=t.c("sep"), width=1)
            self.create_line(x, o, x, v, fill=fill, width=4, capstyle="round")
            knob = (x, v)
        r = 6
        self.create_oval(knob[0] - r, knob[1] - r, knob[0] + r, knob[1] + r, fill=t.c("knob"),
                         outline=t.c("sep") if not t.dark else t.c("knob"))


class PillButton(tk.Canvas):
    """A rounded button with an optional icon: filled with the accent colour
    ("accent", Apple Music's Play and Shuffle) or the LCD grey ("plain")."""

    def __init__(self, master, theme: Theme, text: str, command: Optional[Callable[[], None]] = None,
                 icon: str = "", kind: str = "accent", bg_role: str = "bg", height: int = 32, arrow: bool = False):
        super().__init__(master, height=height, width=40, highlightthickness=0, bd=0, cursor="hand2")
        self.theme, self.text, self.command, self.icon, self.kind = theme, text, command, icon, kind
        self.h, self.enabled, self.hover, self.arrow = height, True, False, arrow
        theme.reg(self, bg=bg_role)
        theme.on_change(self.redraw, self)
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<ButtonRelease-1>", self._click)
        self.redraw()

    def _hov(self, h: bool) -> None:
        self.hover = h
        self.redraw()

    def _click(self, e) -> None:
        if self.enabled and self.command and 0 <= e.x <= int(self.cget("width")) and 0 <= e.y <= self.h:
            self.command()

    def set(self, enabled: Optional[bool] = None, text: Optional[str] = None) -> None:
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "")
        if text is not None:
            self.text = text
        self.redraw()

    def redraw(self) -> None:
        t = self.theme
        font = t.f("body_b")
        iw = 20 if self.icon else 0
        w = font.measure(self.text) + iw + 34 + (16 if self.arrow else 0)
        self.configure(width=w)
        self.delete("all")
        if self.kind == "accent":
            fill = t.c("disabled") if not self.enabled else (t.c("accent_dark") if self.hover else t.c("accent"))
            fg = t.c("on_accent")
        else:
            fill = t.c("hover") if self.hover and self.enabled else t.c("lcd")
            fg = t.c("text") if self.enabled else t.c("disabled")
        round_rect(self, 1, 1, w - 1, self.h - 1, 8, fill=fill, outline="")
        x = 17
        if self.icon:
            draw_icon(self, self.icon, x + 7, self.h / 2, 13, fg)
            x += iw
        self.create_text(x, self.h / 2, text=self.text, anchor="w", fill=fg, font=font)
        if self.arrow:  # a menu opens below
            ax, ay = w - 20, self.h / 2
            self.create_polygon(ax - 4, ay - 2, ax + 4, ay - 2, ax, ay + 3, fill=fg, outline=fg)


def fit_text(font: tkfont.Font, text: str, width: int, cache: Optional[dict] = None) -> str:
    """`text` cut with an ellipsis so that it fits in `width` pixels."""
    if width <= 0:
        return ""
    key = (str(font), text, width)
    if cache is not None and key in cache:
        return cache[key]
    out = text
    if font.measure(text) > width:
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid].rstrip() + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        out = text[:lo].rstrip() + "…" if lo else ""
    if cache is not None:
        if len(cache) > 20000:
            cache.clear()
        cache[key] = out
    return out


__all__ = ["Theme", "IconButton", "PillButton", "Slider", "Tooltip", "draw_icon", "round_rect", "fit_text",
           "load_bundled_font",
           "default_family", "font_families", "system_is_dark", "LIGHT", "DARK", "DEFAULT_FONT_LABEL", "mix",
           "palette"]
