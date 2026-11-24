"""The texts the desktop apps translate (2.1), for the catalog check and for translators.

    python tests/i18n_keys.py            # every text missing from realhires/assets/lang/ja.json
    python tests/i18n_keys.py --all      # every text, with where it is used
    python tests/i18n_keys.py --unused   # catalog entries no app uses any more
    python tests/i18n_keys.py --merge    # rebuild realhires/assets/lang/ja.json from i18n/ja/*.json

The translations are kept in ``i18n/ja/*.json`` (one file per part of the
apps, each ``{"strings": {...}, "patterns": {...}}``); ``--merge`` joins them,
in file-name order, into the catalog the apps read.  A text translated in two
files keeps the first file's translation, and ``--merge`` names such texts.

Texts come from the literal first argument of ``tr("…")`` calls in the Python
apps (read with ``ast``, so implicitly joined literals count as one text), of
``tr!("…")`` calls in the Rust apps, and from the tables of the shared core
that the apps show translated: the Settings fields (groups, labels, help,
choices), the shuffle and repeat modes, the equalizer's presets and band
types, the theme colours and built-in themes, the Edit Info labels and hints,
the Your Stats periods, the verdict names and the placeholder names such as
"Unknown Album".
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from typing import Dict, List, Set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PY_FILES = ["realhires/gui.py", "realhires/player_gui.py", "realhires/player_pages.py",
            "realhires/player_collections.py", "realhires/player_widgets.py", "realhires/player_windows.py",
            "realhires/player_theme.py", "realhires/player_mb.py"]
RUST_DIR = "rust/crates/gui/src"
TR_NAMES = {"tr", "N_"}
CATALOG = os.path.join(ROOT, "realhires", "assets", "lang", "ja.json")
SOURCES = os.path.join(ROOT, "i18n", "ja")


def python_keys(path: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        if name not in TR_NAMES:
            continue
        a = node.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            out.setdefault(a.value, []).append(f"{path}:{node.lineno}")
    return out


_RUST_TR = re.compile(r'\b(?:tr|n_)!\(\s*"((?:[^"\\]|\\.)*)"', re.S)


def _rust_unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        n = s[i + 1]
        if n == "\n":  # a line continuation: the newline and the next line's indent are dropped
            i += 2
            while i < len(s) and s[i] in " \t\n\r":
                i += 1
            continue
        if n == "u" and s[i + 2] == "{":
            end = s.index("}", i)
            out.append(chr(int(s[i + 3:end], 16)))
            i = end + 1
            continue
        out.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'"}[n])
        i += 2
    return "".join(out)


def rust_keys(path: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        src = fh.read()
    for m in _RUST_TR.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        out.setdefault(_rust_unescape(m.group(1)), []).append(f"{path}:{line}")
    return out


def core_keys() -> Dict[str, List[str]]:
    """The core's tables that the apps show translated."""
    from realhires import eq, musiclib, playqueue, settings, stats, tagedit, themes, i18n  # noqa: F401
    out: Dict[str, List[str]] = {}

    def add(text: str, where: str) -> None:
        if text and text.strip():
            out.setdefault(text, []).append(where)

    for g in settings.GROUPS:
        add(g, "settings.GROUPS")
    for f in settings.Settings.field_info():
        add(f["label"], f"settings.{f['name']}.label")
        if f["name"] != "jobs":  # names this computer's CPU count: translated by a pattern
            add(f["help"], f"settings.{f['name']}.help")
        if f["kind"] in ("choice", "multi") and f["name"] not in ("colormap", "language", "shown_verdicts"):
            for c in f["choices"] or []:
                add(c, f"settings.{f['name']}.choices")
    for v in playqueue.SHUFFLE_LABELS.values():
        add(v, "playqueue.SHUFFLE_LABELS")
    for v in playqueue.REPEAT_LABELS.values():
        add(v, "playqueue.REPEAT_LABELS")
    for name in eq.PRESET_NAMES:
        add(name, "eq.PRESETS")
    add(eq.CUSTOM, "eq.CUSTOM")
    for v in eq.BAND_TYPE_LABELS.values():
        add(v, "eq.BAND_TYPE_LABELS")
    for v in themes.COLOR_LABELS.values():
        add(v, "themes.COLOR_LABELS")
    for name in themes.BUILTIN:
        add(name, "themes.BUILTIN")
    add(themes.SYSTEM, "themes.SYSTEM")
    for v in tagedit.LABELS.values():
        add(v, "tagedit.LABELS")
    for v in tagedit.HINTS.values():
        add(v, "tagedit.HINTS")
    for label in stats.PERIODS:
        add(label, "stats.PERIODS")
    for label in stats.WEEKDAYS + stats.MONTHS:
        add(label, "stats.WEEKDAYS/MONTHS")
    for name in i18n.PLACEHOLDER_NAMES:
        add(name, "i18n.PLACEHOLDER_NAMES")
    for v in i18n.VERDICT_NAMES:
        add(v, "i18n.VERDICT_NAMES")
    return out


def all_keys() -> Dict[str, List[str]]:
    keys: Dict[str, List[str]] = {}

    def merge(d: Dict[str, List[str]]) -> None:
        for k, v in d.items():
            keys.setdefault(k, []).extend(v)

    for p in PY_FILES:
        if os.path.exists(os.path.join(ROOT, p)):
            merge(python_keys(p))
    for name in sorted(os.listdir(os.path.join(ROOT, RUST_DIR))):
        if name.endswith(".rs"):
            merge(rust_keys(f"{RUST_DIR}/{name}"))
    merge(core_keys())
    return keys


def catalog() -> dict:
    with open(CATALOG, encoding="utf-8") as fh:
        return json.load(fh)


def merged() -> tuple:
    """The catalog built from i18n/ja/*.json, and the texts translated differently in two files."""
    strings: Dict[str, str] = {}
    patterns: Dict[str, str] = {}
    origin: Dict[str, str] = {}
    conflicts: List[str] = []
    for name in sorted(os.listdir(SOURCES)) if os.path.isdir(SOURCES) else []:
        if not name.endswith(".json"):
            continue
        with open(os.path.join(SOURCES, name), encoding="utf-8") as fh:
            part = json.load(fh)
        for table, out in (("strings", strings), ("patterns", patterns)):
            for k, v in (part.get(table) or {}).items():
                if not isinstance(v, str) or not v:
                    continue
                if k in out:
                    if out[k] != v:
                        conflicts.append(f"{k!r}: {origin[table + k]} {out[k]!r} / {name} {v!r}")
                    continue
                out[k] = v
                origin[table + k] = name
    return {"language": "日本語", "strings": strings, "patterns": patterns}, conflicts


def catalog_text(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def main(argv: List[str]) -> int:
    if "--merge" in argv:
        data, conflicts = merged()
        with open(CATALOG + ".tmp", "w", encoding="utf-8", newline="\n") as fh:
            fh.write(catalog_text(data))
        os.replace(CATALOG + ".tmp", CATALOG)
        for c in conflicts:
            print("translated differently:", c, file=sys.stderr)
        print(f"{CATALOG}: {len(data['strings'])} texts, {len(data['patterns'])} patterns", file=sys.stderr)
        return 0
    keys = all_keys()
    cat = merged()[0]  # the sources, so a translation counts before --merge has run
    strings: Dict[str, str] = cat.get("strings", {})
    if "--unused" in argv:
        unused = [k for k in strings if k not in keys]
        print(json.dumps(unused, ensure_ascii=False, indent=1))
        return 0
    if "--all" in argv:
        for k, where in keys.items():
            print(json.dumps(k, ensure_ascii=False), "  #", ", ".join(where[:3]))
        return 0
    files = [a for a in argv if not a.startswith("--")]
    missing = {k: w for k, w in keys.items() if k not in strings
               and (not files or any(any(x.startswith(f) for f in files) for x in w))}
    print(json.dumps({k: "" for k in missing}, ensure_ascii=False, indent=1))
    print(f"{len(missing)} of {len(keys)} texts have no Japanese translation", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
