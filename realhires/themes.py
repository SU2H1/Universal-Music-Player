"""Colour themes of the player (2.0).

A theme is a name, whether it is dark, and eight colours (`COLOR_KEYS`).  Every
other colour the apps draw (2.3, `EXTRA_COLOR_KEYS`: row hover, selection, text
fields, the quality and verdict colours, the calendar...) is derived from those
eight (`derived()`), unless the theme sets it; `colors(theme)` gives them all.
The player comes with the themes of `BUILTIN`; "Match the system" follows the
system's light or dark mode with "Light" and "Dark".  The user's own themes
are kept in `themes.json` next to the saved results (`store.data_dir()`, or
`$REALHIRES_THEMES`) and can be exported and imported as .json files.  Theme
files put into the themes folder (`folder()`, "themes" next to `themes.json`)
appear in the menu as they are (`load_folder()`): they are read again whenever
the menu is built, and can be changed only in their files.  The `theme`
setting holds the chosen name.  The Rust version reads the same file
and has the same built-in themes (`rust/crates/core/src/themes.rs`).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Dict, List, Optional

THEMES_ENV = "REALHIRES_THEMES"
FILE_NAME = "themes.json"
THEME_FILE_FORMAT = "real-hi-res-theme"
FOLDER_NAME = "themes"
MAX_FILE_BYTES = 1 << 20
SYSTEM = "Match the system"
MAX_NAME = 80

COLOR_KEYS = ["background", "sidebar", "text", "secondary", "separator", "accent", "panel", "hover"]
COLOR_LABELS = {"background": "Background", "sidebar": "Sidebar", "text": "Text", "secondary": "Secondary text",
                "separator": "Lines", "accent": "Accent", "panel": "Playback panel", "hover": "Highlight"}


# 2.3 ("allow user to color code anything"): the colours the apps derived from the eight
# before, which a theme may now set too.  Unset, each is `derived()` from the eight, so
# the built-in themes keep their look (except the quality colours, no longer grey).
EXTRA_COLOR_KEYS = ["row_hover", "zebra", "selection", "field", "placeholder", "placeholder_icon", "disabled",
                    "on_accent", "quality_hires", "quality_lossless", "quality_lossy",
                    "verdict_fake_lossless", "verdict_fake_hires", "verdict_suspicious", "verdict_real_hires",
                    "verdict_cd", "verdict_lossy", "verdict_error", "calendar"]
EXTRA_COLOR_LABELS = {
    "row_hover": "Row under the pointer", "zebra": "Every other row", "selection": "Selected item",
    "field": "Text fields", "placeholder": "Missing cover", "placeholder_icon": "Missing cover's icon",
    "disabled": "Unavailable items", "on_accent": "Text on the accent colour",
    "quality_hires": "Quality: Hi-Res Lossless", "quality_lossless": "Quality: Lossless",
    "quality_lossy": "Quality: Lossy",
    "verdict_fake_lossless": "Quality Check: Fake lossless", "verdict_fake_hires": "Quality Check: Fake hi-res",
    "verdict_suspicious": "Quality Check: Suspicious", "verdict_real_hires": "Quality Check: Real hi-res",
    "verdict_cd": "Quality Check: CD quality", "verdict_lossy": "Quality Check: Lossy format",
    "verdict_error": "Quality Check: Error", "calendar": "Your Stats: days with plays",
}
ALL_COLOR_KEYS = COLOR_KEYS + EXTRA_COLOR_KEYS
# the quality colours (the playback bar's badge, the Quality column): Hi-Res Lossless in gold
QUALITY_GOLD_LIGHT = "#c9a227"
QUALITY_GOLD_DARK = "#e0b84a"
# the verdict colours of the Quality Check page (the 1.x app's), on light and dark themes alike
VERDICT_BASE = {"verdict_fake_lossless": "#a3122a", "verdict_fake_hires": "#d32f2f",
                "verdict_suspicious": "#b26a00", "verdict_real_hires": "#2e7d32", "verdict_cd": "#1565c0",
                "verdict_lossy": "#6a1b9a", "verdict_error": "#616161"}
# verdict codes (realhires.verdict) and quality kinds (quality_kind()) -> their colour keys
VERDICT_COLOR_KEYS = {"FAKE LOSSLESS": "verdict_fake_lossless", "FAKE HI-RES": "verdict_fake_hires",
                      "SUSPICIOUS": "verdict_suspicious", "REAL HI-RES": "verdict_real_hires",
                      "NOT HI-RES": "verdict_cd", "LOSSY FORMAT": "verdict_lossy", "ERROR": "verdict_error"}
QUALITY_COLOR_KEYS = {"hires": "quality_hires", "lossless": "quality_lossless", "lossy": "quality_lossy"}


def color_label(key: str) -> str:
    """The English name of a colour key (the editors show it through tr())."""
    return COLOR_LABELS.get(key) or EXTRA_COLOR_LABELS.get(key, key)


def mix(a: str, b: str, t: float) -> str:
    """The colour t of the way from a to b (#rrggbb; each channel rounded half to even)."""
    ra, ga, ba = (int(a[i:i + 2], 16) for i in (1, 3, 5))
    rb, gb, bb = (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t))


def luminance(c: str) -> float:
    r, g, b = (int(c[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def derived(key: str, colors8: dict, dark: bool) -> str:
    """An extra colour as the eight colours `colors8` (and whether the theme is dark) give it."""
    c = colors8
    if key == "row_hover":
        return mix(c["background"], c["hover"], 0.7)
    if key == "zebra":
        return mix(c["background"], c["hover"], 0.3)
    if key in ("selection", "field", "placeholder"):
        return c["hover"]
    if key == "placeholder_icon":
        return mix(c["hover"], c["secondary"], 0.6)
    if key == "disabled":
        return mix(c["background"], c["secondary"], 0.45)
    if key == "on_accent":
        return "#1d1d1f" if luminance(c["accent"]) > 0.62 else "#ffffff"
    if key == "quality_hires":
        return QUALITY_GOLD_DARK if dark else QUALITY_GOLD_LIGHT
    if key in ("quality_lossless", "calendar"):
        return c["accent"]
    if key == "quality_lossy":
        return c["secondary"]
    if key in VERDICT_BASE:
        return VERDICT_BASE[key]
    raise KeyError(key)


def colors(theme: dict) -> Dict[str, str]:
    """Every colour of a theme: its eight, then each extra colour it sets, else derived."""
    t = normalize(dict(theme, name=theme.get("name") or "Theme")) or normalize({"name": "Light"})
    own = t["colors"]
    out = {k: own[k] for k in COLOR_KEYS}
    for k in EXTRA_COLOR_KEYS:
        out[k] = own.get(k) or derived(k, own, t["dark"])
    return out


def quality_kind(track) -> str:
    """"hires", "lossless" or "lossy": the kind of musiclib.quality()'s text for a track
    (anything with is_lossy, sample_rate, bits and is_float)."""
    if getattr(track, "is_lossy", False):
        return "lossy"
    rate = getattr(track, "sample_rate", 0) or 0
    hires = rate > 48000 or (getattr(track, "bits", 0) or 16) > 16 or bool(getattr(track, "is_float", False))
    return "hires" if hires else "lossless"


def _t(dark: bool, *colors: str) -> dict:
    return {"dark": dark, "colors": dict(zip(COLOR_KEYS, colors))}


BUILTIN: Dict[str, dict] = {
    "Light": _t(False, "#ffffff", "#f5f5f7", "#1d1d1f", "#6e6e73", "#d2d2d7", "#fa2d48", "#f2f2f4", "#e8e8ed"),
    "Dark": _t(True, "#1e1e1e", "#262628", "#f5f5f7", "#a1a1a6", "#3a3a3c", "#fa2d48", "#2c2c2e", "#333336"),
    "Midnight": _t(True, "#0f1221", "#161a2e", "#e8eaf6", "#9aa0c3", "#2a2f4a", "#7c8cff", "#1c2138", "#23294a"),
    "Graphite": _t(True, "#1b1b1d", "#232326", "#ececec", "#9a9a9f", "#38383c", "#ff9f0a", "#2a2a2d", "#313135"),
    "Forest": _t(True, "#121a15", "#18231c", "#e6efe8", "#9db3a3", "#2b3a30", "#4cc38a", "#1d2a22", "#24342a"),
    "Ocean": _t(False, "#f4f8fb", "#e8f0f6", "#102a43", "#52677c", "#c8d6e2", "#0a84ff", "#e9f1f7", "#dbe7f1"),
    "Sepia": _t(False, "#f6efe2", "#ece2cf", "#3b2f22", "#7a6a55", "#d9cbb1", "#c0673a", "#efe5d2", "#e3d6bd"),
    "Rose": _t(False, "#fff7f9", "#fbeaf0", "#3a1d2a", "#8a6574", "#efd3de", "#e0457b", "#f9e6ee", "#f3dbe5"),
}

_HEX = re.compile(r"^#[0-9a-f]{6}$")


class ThemeError(ValueError):
    """A theme name or file that cannot be used; the message says why."""


def default_path() -> str:
    from .store import data_dir

    return os.environ.get(THEMES_ENV) or os.path.join(data_dir(), FILE_NAME)


def clean_name(name) -> str:
    return " ".join(str(name or "").split())[:MAX_NAME]


def normalize(theme) -> Optional[dict]:
    """A valid theme from stored data, or None.  Missing or broken colours are
    taken from "Dark" or "Light"; of the extra colours (2.3) only those set to a valid
    colour are kept (the others stay derived)."""
    if not isinstance(theme, dict):
        return None
    name = clean_name(theme.get("name"))
    if not name:
        return None
    dark = bool(theme.get("dark", False))
    base = BUILTIN["Dark" if dark else "Light"]["colors"]
    given = theme.get("colors") if isinstance(theme.get("colors"), dict) else {}
    colors = {}
    for k in COLOR_KEYS:
        v = given.get(k)
        v = v.strip().lower() if isinstance(v, str) else ""
        colors[k] = v if _HEX.match(v) else base[k]
    for k in EXTRA_COLOR_KEYS:
        v = given.get(k)
        v = v.strip().lower() if isinstance(v, str) else ""
        if _HEX.match(v):
            colors[k] = v
    return {"name": name, "dark": dark, "colors": colors}


def load_custom(path: Optional[str] = None) -> List[dict]:
    try:
        with open(path or default_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    out: List[dict] = []
    seen = set(n.casefold() for n in BUILTIN) | {SYSTEM.casefold()}
    for t in data.get("themes", []) if isinstance(data, dict) else []:
        n = normalize(t)
        if n is not None and n["name"].casefold() not in seen:
            seen.add(n["name"].casefold())
            out.append(n)
    return out


def save_custom(themes: List[dict], path: Optional[str] = None) -> str:
    path = path or default_path()
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    data = {"_version": 1, "themes": [normalize(t) for t in themes if normalize(t) is not None]}
    fd, tmp = tempfile.mkstemp(prefix=".themes-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def names(custom: List[dict]) -> List[str]:
    """Every theme name for the menu: Match the system, the built-in, then the user's."""
    return [SYSTEM] + list(BUILTIN) + [t["name"] for t in custom]


def resolve(name: str, system_dark: bool, custom: List[dict]) -> dict:
    """The theme to draw with; unknown names follow the system."""
    if name in BUILTIN:
        return dict(BUILTIN[name], name=name)
    for t in custom:
        if t["name"] == name:
            return t
    n = "Dark" if system_dark else "Light"
    return dict(BUILTIN[n], name=n)


def check_name(name: str, custom: List[dict], renaming: str = "") -> str:
    clean = clean_name(name)
    if not clean:
        raise ThemeError("Give the theme a name.")
    if clean.casefold() == SYSTEM.casefold() or clean.casefold() in {n.casefold() for n in BUILTIN}:
        raise ThemeError(f"“{clean}” is the name of a built-in theme; choose another one.")
    if any(t["name"].casefold() == clean.casefold() and t["name"] != renaming for t in custom):
        raise ThemeError(f"You already have a theme called “{clean}”.")
    return clean


def copy_name(base: str, custom: List[dict]) -> str:
    """"Midnight copy", "Midnight copy 2"... for a new theme made from `base`."""
    taken = {n.casefold() for n in names(custom)}
    name = clean_name(f"{base} copy")
    n = 2
    while name.casefold() in taken:
        name = clean_name(f"{base} copy {n}")
        n += 1
    return name


def export_theme(theme: dict) -> str:
    t = normalize(theme)
    if t is None:
        raise ThemeError("This theme cannot be exported.")
    data = {"format": THEME_FILE_FORMAT, "version": 1, "name": t["name"], "dark": t["dark"], "colors": t["colors"]}
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def import_theme(text: str, custom: List[dict]) -> dict:
    """A new custom theme from an exported file (a taken name gets " 2", " 3"...)."""
    try:
        data = json.loads(text.lstrip("﻿"))
    except ValueError:
        data = None
    t = normalize(data) if isinstance(data, dict) and isinstance(data.get("colors"), dict) else None
    if t is None:
        raise ThemeError("This file is not a theme of Universal Music Player.")
    taken = {n.casefold() for n in names(custom)}
    base = t["name"]
    n = 2
    while t["name"].casefold() in taken:
        t["name"] = clean_name(f"{base} {n}")
        n += 1
    return t


def folder(path: Optional[str] = None) -> str:
    """The themes folder: "themes" next to themes.json."""
    return os.path.join(os.path.dirname(os.path.abspath(path or default_path())), FOLDER_NAME)


def load_folder(custom: List[dict], folder_path: Optional[str] = None) -> List[dict]:
    """The themes of the .json files in the themes folder, in file-name order, each
    with its "file".  A name that is taken (built-in, the user's own, or an earlier
    file) gets " 2", " 3"...; files that are not themes are left out."""
    d = folder_path or folder()
    try:
        files = sorted(n for n in os.listdir(d) if n.lower().endswith(".json") and not n.startswith("."))
    except OSError:
        return []
    out: List[dict] = []
    for n in files:
        p = os.path.join(d, n)
        try:
            with open(p, "rb") as fh:
                text = fh.read(MAX_FILE_BYTES + 1)
            if len(text) > MAX_FILE_BYTES:
                continue
            t = import_theme(text.decode("utf-8"), custom + out)
        except (OSError, ThemeError, UnicodeDecodeError):
            continue
        t["file"] = p
        out.append(t)
    return out
