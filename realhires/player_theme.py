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
from .themes import luminance, mix  # noqa: F401  (the colour maths is shared with the core, 2.3)
from .probe import external_env

FONT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts", "InterVariable.ttf")
DEFAULT_FONT_LABEL = N_("SF Pro (macOS) / Inter")  # shown as tr(DEFAULT_FONT_LABEL)

def palette(theme: dict) -> Dict[str, str]:
    """Every colour the app draws with: the theme's colours (themes.colors(): the eight of
    themes.COLOR_KEYS and the extra ones of 2.3, set by the theme or derived), under the
    names the widgets use, plus a few shades of them."""
    from .themes import colors

    c, dark = colors(theme), bool(theme.get("dark"))
    bg, text, accent = c["background"], c["text"], c["accent"]
    out = dict(c)  # the theme's own keys too: "quality_hires", "verdict_cd", "calendar"...
    out.update({
        "bg": bg, "sidebar": c["sidebar"], "text": text, "text2": c["secondary"], "sep": c["separator"],
        "accent": accent, "lcd": c["panel"], "side_sel": c["selection"], "field": c["field"], "ph": c["placeholder"],
        "hover": c["row_hover"], "zebra": c["zebra"],
        "ph_icon": c["placeholder_icon"], "disabled": c["disabled"],
        "on_accent": c["on_accent"],
        "accent_dark": mix(accent, "#000000", 0.12), "seek": c["separator"], "seek_fill": c["secondary"],
        "error": "#ff453a" if dark else "#d70015", "knob": mix(text, "#ffffff", 0.2) if dark else "#ffffff",
        "badge": c["secondary"],
    })
    return out


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
        """The ttk styles of the player, and the standard ones in the theme's colours (`_standard_styles`)."""
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
        # 2.3: a field the Properties window just filled in from the clipboard flashes in the accent colour
        flash = mix(c["accent"], c["field"], 0.7)
        s.configure("Flash.P.TEntry", fieldbackground=flash, foreground=c["text"], insertcolor=c["text"],
                    bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"], padding=(6, 3), font=body)
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
        self._standard_styles()

    def _standard_styles(self) -> None:
        """The standard ttk styles (TButton, Treeview, ...) in the theme's colours and fonts:
        the Quality check page (the 1.x scanner, gui.App) and the windows it opens use
        them, so they look like the rest of the player.  "Panel." styles are for widgets
        on a rounded panel (the panel colour), "Muted."/"Small."/"Head." labels for
        secondary text, small print and headings."""
        c, s, f = self.colors, self.style, self.fonts
        body, small = f["body"], f["small"]
        bg, panel, field, sep = c["bg"], c["lcd"], c["field"], c["sep"]
        warn = "#ffcc4d" if self.dark else "#9a6700"
        good = "#5fd38a" if self.dark else "#2e7d32"
        s.configure(".", background=bg, foreground=c["text"], font=body, bordercolor=sep, lightcolor=bg,
                    darkcolor=bg, troughcolor=panel, selectbackground=c["accent"], selectforeground=c["on_accent"],
                    fieldbackground=field, insertcolor=c["text"], focuscolor=bg, arrowcolor=c["text2"])
        s.map(".", foreground=[("disabled", c["disabled"])])
        for prefix, back in (("", bg), ("Panel.", panel)):
            s.configure(f"{prefix}TFrame", background=back)
            s.configure(f"{prefix}TLabel", background=back, foreground=c["text"], font=body)
            s.configure(f"{prefix}Muted.TLabel", background=back, foreground=c["text2"], font=body)
            s.configure(f"{prefix}Small.TLabel", background=back, foreground=c["text2"], font=small)
            s.configure(f"{prefix}Head.TLabel", background=back, foreground=c["text"], font=f["h2"])
            s.configure(f"{prefix}Bold.TLabel", background=back, foreground=c["text"], font=f["body_b"])
            s.configure(f"{prefix}Warn.TLabel", background=back, foreground=warn, font=body)
            s.configure(f"{prefix}Good.TLabel", background=back, foreground=good, font=f["h2"])
            s.configure(f"{prefix}Bad.TLabel", background=back, foreground=c["error"], font=f["h2"])
        s.configure("TLabelframe", background=bg, bordercolor=sep, lightcolor=bg, darkcolor=bg, relief="solid",
                    borderwidth=1)
        s.configure("TLabelframe.Label", background=bg, foreground=c["text2"], font=f["small_b"])
        s.configure("TButton", background=panel, foreground=c["text"], bordercolor=panel, lightcolor=panel,
                    darkcolor=panel, focuscolor=panel, padding=(12, 5), font=f["body_b"], relief="flat")
        s.map("TButton", background=[("disabled", panel), ("active", c["hover"])],
              bordercolor=[("active", c["hover"])], lightcolor=[("active", c["hover"])],
              darkcolor=[("active", c["hover"])], foreground=[("disabled", c["disabled"])])
        s.layout("TCheckbutton", s.layout("P.TCheckbutton"))
        s.configure("TCheckbutton", background=bg, foreground=c["text"], font=body, focuscolor=bg, padding=(0, 2))
        s.map("TCheckbutton", background=[("active", bg)], foreground=[("disabled", c["disabled"])])
        s.configure("TRadiobutton", background=bg, foreground=c["text"], font=body, indicatorbackground=field,
                    focuscolor=bg)
        s.map("TRadiobutton", indicatorbackground=[("selected", c["accent"])], background=[("active", bg)])
        for name in ("TEntry", "TSpinbox", "TCombobox"):
            s.configure(name, fieldbackground=field, background=field, foreground=c["text"], bordercolor=field,
                        lightcolor=field, darkcolor=field, insertcolor=c["text"], arrowcolor=c["text2"],
                        padding=(6, 3), selectbackground=c["accent"], selectforeground=c["on_accent"])
            s.map(name, bordercolor=[("focus", c["accent"])], lightcolor=[("focus", field)],
                  fieldbackground=[("readonly", field), ("disabled", panel)],
                  foreground=[("readonly", c["text"]), ("disabled", c["disabled"])])
        s.map("TCombobox", selectbackground=[("readonly", field)], selectforeground=[("readonly", c["text"])])
        s.configure("TNotebook", background=bg, borderwidth=1, tabmargins=(0, 4, 0, 0), bordercolor=sep,
                    lightcolor=bg, darkcolor=bg)
        s.configure("TNotebook.Tab", background=panel, foreground=c["text2"], padding=(14, 5), borderwidth=1,
                    font=f["body_b"], lightcolor=panel, darkcolor=panel, bordercolor=sep)
        s.map("TNotebook.Tab", background=[("selected", bg)], foreground=[("selected", c["text"])],
              lightcolor=[("selected", bg)], darkcolor=[("selected", bg)])
        # a notebook whose tabs are a Segmented control above it
        s.layout("Tabless.TNotebook.Tab", [])
        s.configure("Tabless.TNotebook", background=panel, borderwidth=0, tabmargins=0, bordercolor=panel,
                    lightcolor=panel, darkcolor=panel)
        for prefix, back in (("", bg), ("Panel.", panel)):
            name = f"{prefix}Treeview"
            s.configure(name, background=back, fieldbackground=back, foreground=c["text"], font=body,
                        rowheight=32 if not prefix else 26, borderwidth=0, relief="flat", bordercolor=back,
                        lightcolor=back, darkcolor=back)
            s.map(name, background=[("selected", c["accent"])], foreground=[("selected", c["on_accent"])])
            s.layout(name, [("Treeview.treearea", {"sticky": "nswe"})])
            s.configure(f"{name}.Heading", background=back, foreground=c["text2"], font=f["small_b"],
                        relief="flat", borderwidth=0, bordercolor=back, lightcolor=back, darkcolor=back,
                        padding=(4, 4))
            s.map(f"{name}.Heading", background=[("active", back)], foreground=[("active", c["text"])],
                  lightcolor=[("active", back)], darkcolor=[("active", back)], bordercolor=[("active", back)])
        for orient in ("Vertical", "Horizontal"):
            for prefix, trough, thumb in (("", bg, sep), ("Hide.", bg, bg), ("Panel.", panel, sep),
                                          ("PanelHide.", panel, panel)):
                name = f"{prefix}{orient}.TScrollbar"
                s.layout(name, [(f"{orient}.Scrollbar.trough", {"sticky": "nswe", "children": [
                    (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
                s.configure(name, troughcolor=trough, background=thumb, bordercolor=trough, lightcolor=thumb,
                            darkcolor=thumb, arrowsize=8, gripcount=0, relief="flat", borderwidth=0)
                s.map(name, background=[("active", c["text2"] if "Hide" not in prefix else thumb)])
        for name, trough in (("Horizontal.TProgressbar", panel), ("Panel.Horizontal.TProgressbar", field)):
            s.configure(name, background=c["accent"], troughcolor=trough, bordercolor=trough,
                        lightcolor=c["accent"], darkcolor=c["accent"], thickness=6, borderwidth=0)
        for name, back in (("TPanedwindow", bg), ("Panel.TPanedwindow", panel)):
            s.configure(name, background=back)
        s.configure("Sash", sashthickness=10, gripcount=0, background=bg, bordercolor=bg, lightcolor=bg,
                    darkcolor=bg, handlesize=0)


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
    elif name == "expand":  # two corners pointing outwards (Now Playing's Lyrics only, 2.3)
        q, k = 0.42 * s, 0.2 * s
        for sx, sy in ((-1, -1), (1, 1)):
            x, y = cx + sx * q, cy + sy * q
            c.create_line(x - sx * k, y, x, y, x, y - sy * k, width=w, fill=color, capstyle="round",
                          joinstyle="round", **kw)
            c.create_line(x, y, cx + sx * 0.1 * s, cy + sy * 0.1 * s, width=w, fill=color, capstyle="round", **kw)
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
    elif name in ("mood", "mood_sad"):  # 2.4: a smiling or a frowning face (the Moods page)
        r = 0.42 * s
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=w, **kw)
        for dx in (-0.15, 0.15):
            e = 0.055 * s
            c.create_oval(cx + dx * s - e, cy - 0.13 * s - e, cx + dx * s + e, cy - 0.13 * s + e, fill=color,
                          outline=color, **kw)
        if name == "mood":
            c.create_arc(cx - 0.24 * s, cy - 0.18 * s, cx + 0.24 * s, cy + 0.24 * s, start=200, extent=140,
                         style="arc", outline=color, width=w, **kw)
        else:
            c.create_arc(cx - 0.24 * s, cy + 0.1 * s, cx + 0.24 * s, cy + 0.38 * s, start=20, extent=140,
                         style="arc", outline=color, width=w, **kw)
    elif name == "tempo":  # 2.4: a metronome (the tempo cards)
        c.create_polygon(cx - 0.14 * s, cy - 0.42 * s, cx + 0.14 * s, cy - 0.42 * s, cx + 0.32 * s, cy + 0.4 * s,
                         cx - 0.32 * s, cy + 0.4 * s, fill="", outline=color, width=w, joinstyle="round", **kw)
        c.create_line(cx - 0.24 * s, cy + 0.16 * s, cx + 0.24 * s, cy + 0.16 * s, width=w, fill=color, **kw)
        c.create_line(cx, cy + 0.16 * s, cx + 0.3 * s, cy - 0.36 * s, width=w, fill=color, capstyle="round", **kw)
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
    elif name == "tiles":  # 2.3: the Library page as tiles
        q, g = 0.38 * s, 0.07 * s
        for x0, y0 in ((-q, -q), (g, -q), (-q, g), (g, g)):
            round_rect(c, cx + x0, cy + y0, cx + x0 + q - g, cy + y0 + q - g, 0.07 * s, fill=color, outline="",
                       **kw)
    elif name == "list":  # 2.3: the Library page as a list
        for dy in (-0.3, 0.0, 0.3):
            y = cy + dy * s
            q = 0.07 * s
            c.create_rectangle(cx - 0.42 * s, y - q, cx - 0.28 * s, y + q, fill=color, outline="", **kw)
            c.create_line(cx - 0.14 * s, y, cx + 0.42 * s, y, width=w, fill=color, capstyle="round", **kw)
    elif name == "filter":  # 2.3: a custom tab (a funnel)
        pts = [cx - 0.38 * s, cy - 0.32 * s, cx + 0.38 * s, cy - 0.32 * s, cx + 0.09 * s, cy + 0.04 * s,
               cx + 0.09 * s, cy + 0.36 * s, cx - 0.09 * s, cy + 0.26 * s, cx - 0.09 * s, cy + 0.04 * s]
        c.create_polygon(*pts, fill=color if filled else "", outline=color, width=w, joinstyle="round", **kw)
    elif name in ("up", "down"):  # chevrons pointing up or down (find: previous, next)
        d = -1 if name == "up" else 1
        c.create_line(cx - 0.34 * s, cy - d * 0.16 * s, cx, cy + d * 0.18 * s, cx + 0.34 * s, cy - d * 0.16 * s,
                      width=w * 1.2, fill=color, capstyle="round", joinstyle="round", **kw)


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
        if self.tk.call("tk", "windowingsystem") == "x11":  # elsewhere 4 and 5 are the back and forward buttons
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


class Button(PillButton):
    """A PillButton that also takes ttk.Button's `text`, `command` and `state` through
    configure() and cget(), so code written for ttk buttons (the Quality check page)
    enables, disables and renames it unchanged.  `fill` is the colour role it is drawn
    in when it is not the accent button: the panel colour on the page, the field colour
    on a panel."""

    def __init__(self, master, theme: Theme, text: str, command: Optional[Callable[[], None]] = None,
                 kind: str = "plain", state: str = "normal", fill: str = "lcd", hover_fill: str = "hover",
                 font: str = "body_b", **kw):
        self.fill_role, self.hover_role, self.font_role = fill, hover_fill, font
        super().__init__(master, theme, text, command=command, kind=kind, **kw)
        if state == "disabled":
            self.configure(state="disabled")

    def configure(self, cnf=None, **kw):
        if cnf:
            kw = {**cnf, **kw}
        changed = False
        for key in ("text", "command"):
            if key in kw:
                setattr(self, key, kw.pop(key))
                changed = True
        if "state" in kw:
            self.enabled = str(kw["state"]) != "disabled"
            kw["cursor"] = "hand2" if self.enabled else ""
            changed = True
        result = super().configure(**kw) if kw else None
        if changed:
            self.redraw()
        return result

    config = configure

    def __setitem__(self, key, value) -> None:
        self.configure(**{key: value})

    def cget(self, key):
        if key == "text":
            return self.text
        if key == "command":
            return self.command
        return super().cget(key)

    __getitem__ = cget

    def invoke(self) -> None:
        if self.enabled and self.command:
            self.command()

    def redraw(self) -> None:
        if not hasattr(self, "fill_role"):
            return
        t = self.theme
        font = t.f(self.font_role)
        iw = 20 if self.icon else 0
        w = font.measure(self.text) + iw + 28 + (16 if self.arrow else 0)
        super().configure(width=w)
        self.delete("all")
        if self.kind == "accent":
            fill = t.c("disabled") if not self.enabled else (t.c("accent_dark") if self.hover else t.c("accent"))
            fg = t.c("on_accent")
        else:
            fill = t.c(self.hover_role) if self.hover and self.enabled else t.c(self.fill_role)
            fg = t.c("text") if self.enabled else t.c("disabled")
        round_rect(self, 1, 1, w - 1, self.h - 1, 8, fill=fill, outline="")
        x = 14
        if self.icon:
            draw_icon(self, self.icon, x + 7, self.h / 2, 13, fg)
            x += iw
        self.create_text(x, self.h / 2, text=self.text, anchor="w", fill=fg, font=font)
        if self.arrow:
            ax, ay = w - 18, self.h / 2
            self.create_polygon(ax - 4, ay - 2, ax + 4, ay - 2, ax, ay + 3, fill=fg, outline=fg)


class Segmented(tk.Canvas):
    """A segmented control (one of a few options), drawn like the Equalizer window's
    Graphic / Parametric tabs: the options in bold on a rounded track in the panel
    colour, the chosen one raised in the page colour and the text colour, the others
    grey.  `variable` (a StringVar) holds the chosen option; `command` is called after
    a click changed it."""

    def __init__(self, master, theme: Theme, options: List[str], variable: Optional[tk.StringVar] = None,
                 command: Optional[Callable[[], None]] = None, bg_role: str = "bg", height: int = 30):
        super().__init__(master, height=height, width=40, highlightthickness=0, bd=0, cursor="hand2")
        self.theme, self.options, self.command, self.h, self.bg_role = theme, list(options), command, height, bg_role
        self.var = variable if variable is not None else tk.StringVar(master, value=self.options[0])
        self.hover: Optional[int] = None
        self.spans: List[Tuple[float, float]] = []
        theme.reg(self, bg=bg_role)
        theme.on_change(self.redraw, self)
        self.var.trace_add("write", lambda *a: self.redraw())
        self.bind("<Motion>", lambda e: self._hov(self._at(e.x)))
        self.bind("<Leave>", lambda e: self._hov(None))
        self.bind("<ButtonRelease-1>", self._click)
        self.bind("<Configure>", lambda e: self.redraw())  # squeezed: drawn smaller (see redraw)
        self.redraw()

    def _at(self, x: float) -> Optional[int]:
        for i, (a, b) in enumerate(self.spans):
            if a <= x < b:
                return i
        return None

    def _hov(self, i: Optional[int]) -> None:
        if i != self.hover:
            self.hover = i
            self.redraw()

    def _click(self, e) -> None:
        i = self._at(e.x)
        if i is not None and 0 <= e.y <= self.h:
            self.select(self.options[i], notify=True)

    def get(self) -> str:
        return self.var.get()

    def select(self, option: str, notify: bool = False) -> None:
        changed = option != self.var.get()
        if changed:
            self.var.set(option)
        if notify and changed and self.command is not None:
            self.command()

    def redraw(self) -> None:
        t = self.theme
        font = t.f("body_b")
        widths = [font.measure(o) + 28 for o in self.options]
        natural = sum(widths) + 6
        if int(self.cget("width")) != natural:
            self.configure(width=natural)  # what it asks for; a narrow window may give it less
        have = self.winfo_width()
        w = natural
        if 1 < have < natural:  # less room: less padding, then the small font
            for name, pad in (("body_b", 16), ("small_b", 12), ("small_b", 6)):
                font = t.f(name)
                widths = [font.measure(o) + pad for o in self.options]
                w = sum(widths) + 6
                if w <= have:
                    break
        self.delete("all")
        track = t.c("field") if self.bg_role == "lcd" else t.c("lcd")
        round_rect(self, 0, 0, w, self.h, 9, fill=track, outline="")
        x = 3.0
        self.spans = []
        chosen = self.var.get()
        for opt, ow in zip(self.options, widths):
            i = len(self.spans)
            self.spans.append((x, x + ow))
            if opt == chosen:
                round_rect(self, x, 3, x + ow, self.h - 3, 7, fill=t.c("bg"), outline=t.c("sep"))
                fg = t.c("text")
            else:
                fg = t.c("text") if self.hover == i else t.c("text2")
            self.create_text(x + ow / 2, self.h / 2, text=opt, fill=fg, font=font)
            x += ow


class Chip(tk.Canvas):
    """A pill-shaped toggle with a dot in `color` and an optional count ("Fake lossless 3"):
    on, it is filled with the panel colour and drawn in the text colour; off, it is
    outlined and grey.  It shows and toggles a BooleanVar; `command` runs after a click."""

    def __init__(self, master, theme: Theme, text: str, color: str, variable: tk.BooleanVar,
                 command: Optional[Callable[[], None]] = None, bg_role: str = "bg", height: int = 28):
        super().__init__(master, height=height, width=40, highlightthickness=0, bd=0, cursor="hand2")
        self.theme, self.text, self.color, self.var, self.command = theme, text, color, variable, command
        self.h, self.count, self.hover, self.bg_role = height, None, False, bg_role
        theme.reg(self, bg=bg_role)
        theme.on_change(self.redraw, self)
        self.var.trace_add("write", lambda *a: self.redraw())
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<ButtonRelease-1>", self._click)
        self.redraw()

    def _hov(self, h: bool) -> None:
        self.hover = h
        self.redraw()

    def _click(self, e) -> None:
        if 0 <= e.x <= int(self.cget("width")) and 0 <= e.y <= self.h:
            self.var.set(not self.var.get())
            if self.command is not None:
                self.command()

    def set_count(self, count: Optional[int]) -> None:
        if count != self.count:
            self.count = count
            self.redraw()

    def label(self) -> str:
        return self.text if self.count is None else f"{self.text} {self.count:,}"

    def redraw(self) -> None:
        t = self.theme
        font = t.f("body")
        on = bool(self.var.get())
        text = self.label()
        w = font.measure(text) + 40
        self.configure(width=w)
        self.delete("all")
        if on:
            fill = t.c("hover") if self.hover else t.c("lcd")
            round_rect(self, 1, 1, w - 1, self.h - 1, (self.h - 2) / 2, fill=fill, outline=fill)
            fg, dot = t.c("text"), self.color
        else:
            fill = t.c("hover") if self.hover else t.c(self.bg_role)
            round_rect(self, 1, 1, w - 1, self.h - 1, (self.h - 2) / 2, fill=fill, outline=t.c("sep"))
            fg, dot = t.c("text2"), mix(self.color, t.c(self.bg_role), 0.45)
        r = 4.5
        self.create_oval(14 - r, self.h / 2 - r, 14 + r, self.h / 2 + r, fill=dot, outline=dot)
        self.create_text(26, self.h / 2, text=text, anchor="w", fill=fg, font=font)


class SearchBox(tk.Canvas):
    """A rounded text field like the sidebar's Search box: a magnifier, a borderless
    entry bound to `variable`, and a grey placeholder while it is empty.  Escape empties
    it.  `entry` is the tk.Entry (for focus and key bindings)."""

    def __init__(self, master, theme: Theme, variable: tk.StringVar, placeholder: str = "Search",
                 width: int = 220, bg_role: str = "bg", height: int = 30):
        super().__init__(master, width=width, height=height, highlightthickness=0, bd=0)
        self.theme, self.var, self.h = theme, variable, height
        theme.reg(self, bg=bg_role)
        self.entry = tk.Entry(self, textvariable=variable, relief="flat", bd=0, highlightthickness=0,
                              font=theme.f("body"))
        theme.reg(self.entry, bg="field", fg="text", insertbackground="text", selectbackground="accent",
                  selectforeground="on_accent")
        self.ph = tk.Label(self, text=placeholder, font=theme.f("body"), bd=0, anchor="w", cursor="xterm")
        theme.reg(self.ph, bg="field", fg="text2")
        self.ph.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda e: self._placeholder(), add="+")
        self.entry.bind("<FocusOut>", lambda e: self._placeholder(), add="+")
        self.entry.bind("<Escape>", lambda e: self.var.set(""), add="+")
        variable.trace_add("write", lambda *a: self._placeholder())
        self.bind("<Configure>", lambda e: self.redraw())
        theme.on_change(self.redraw, self)

    def _placeholder(self) -> None:
        try:
            focused = self.focus_get() is self.entry
        except (KeyError, tk.TclError):
            focused = False
        if self.var.get() or focused:
            self.ph.place_forget()
        else:
            self.ph.place(in_=self.entry, x=0, rely=0.5, anchor="w", relwidth=1.0)

    def redraw(self) -> None:
        t = self.theme
        self.delete("all")
        W = self.winfo_width() if self.winfo_width() > 1 else int(self.cget("width"))
        round_rect(self, 0, 0, W, self.h, 8, fill=t.c("field"), outline="")
        draw_icon(self, "search", 16, self.h / 2, 13, t.c("text2"))
        self.create_window(30, self.h / 2, window=self.entry, anchor="w", width=max(20, W - 40),
                           height=self.h - 8)
        self._placeholder()


class RoundFrame(tk.Frame):
    """A frame with rounded corners in the `bg_role` colour on an `outer_role` background
    (four small corner masks).  Give its children at least `radius` of padding."""

    def __init__(self, master, theme: Theme, bg_role: str = "lcd", outer_role: str = "bg", radius: int = 10, **kw):
        super().__init__(master, bd=0, highlightthickness=0, **kw)
        self.theme, self.bg_role, self.outer_role, self.r = theme, bg_role, outer_role, radius
        theme.reg(self, bg=bg_role)
        self.corners = []
        for relx, rely, anchor in ((0, 0, "nw"), (1, 0, "ne"), (0, 1, "sw"), (1, 1, "se")):
            c = tk.Canvas(self, width=radius, height=radius, highlightthickness=0, bd=0)
            c.place(relx=relx, rely=rely, anchor=anchor)
            self.corners.append((anchor, c))
        theme.on_change(self.paint, self)
        self.bind("<Map>", lambda e: self._raise(), add="+")
        self.paint()

    def _raise(self) -> None:
        for _, c in self.corners:
            tk.Misc.tkraise(c)  # (Canvas.lift raises canvas items)

    def paint(self) -> None:
        t, r = self.theme, self.r
        fill = t.c(self.bg_role)
        for anchor, c in self.corners:
            c.configure(bg=t.c(self.outer_role))
            c.delete("all")
            x0 = 0 if "w" in anchor else -r
            y0 = 0 if "n" in anchor else -r
            c.create_oval(x0, y0, x0 + 2 * r, y0 + 2 * r, fill=fill, outline=fill)
        self._raise()


def theme_menu(theme: Theme, menu: tk.Menu) -> None:
    """A menu and its cascades in the theme's colours (X11 draws them itself; Windows and
    macOS keep their own)."""
    menu.configure(relief="flat", bd=0, activeborderwidth=0)
    theme.reg(menu, bg="bg", fg="text", activebackground="accent", activeforeground="on_accent",
              selectcolor="accent", disabledforeground="disabled")
    last = menu.index("end")
    for i in range(0 if last is None else last + 1):
        if menu.type(i) == "cascade":
            theme_menu(theme, menu.nametowidget(menu.entrycget(i, "menu")))


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


__all__ = ["Theme", "IconButton", "PillButton", "Button", "Segmented", "Chip", "SearchBox", "RoundFrame",
           "Slider", "Tooltip", "draw_icon", "round_rect", "fit_text", "theme_menu", "load_bundled_font",
           "default_family", "font_families", "system_is_dark", "LIGHT", "DARK", "DEFAULT_FONT_LABEL", "mix",
           "palette"]
