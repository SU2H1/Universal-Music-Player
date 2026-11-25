"""Custom tabs (2.3): saved sets of rules that pick songs from the library.

A tab is shown in the sidebar like a playlist, and on the Library page after its
fifteen entries.  It is a small JSON file, so tabs can be shared: no code runs, a
tab only names fields of the songs, operators and values
(docs/player-spec.md, "2.3", "6. Custom tabs")::

    {"format": "universal-music-player-tab", "version": 1, "name": "Hi-Res",
     "match": "all", "rules": [{"field": "quality", "op": "is", "value": "Hi-Res Lossless"}],
     "sort": "title", "descending": false, "limit": 0}

`matches()` and `tab_songs()` evaluate a tab on the library's tracks, `check_tab()`
says in a full sentence what is wrong with one (`TabError`), and the tabs folder
(`tabs_folder()`: "tabs" in `store.data_dir()`, or `$REALHIRES_TABS`) holds one
.json file per tab (`load_tabs()`, `save_tab()`, `delete_tab()`, `export_tab()`,
`import_tab()`).  The Rust version (`rust/crates/core/src/smarttabs.rs`) gives the
same answers and writes the same bytes (vectors `python_tabs.json`).
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Set, Tuple

TABS_ENV = "REALHIRES_TABS"
FOLDER_NAME = "tabs"
TAB_FORMAT = "universal-music-player-tab"
TAB_VERSION = 1
MAX_FILE_BYTES = 1 << 20
MAX_NAME = 80        # characters of a tab's name
MAX_RULES = 100
MAX_LIMIT = 100000
MAX_INT = 10 ** 15   # larger whole numbers are read as floats (the same in Rust)
SHOWN_CHARS = 60     # how much of an unknown field, operator or sort a message repeats

TEXT_FIELDS = ("title", "artist", "album", "album_artist", "genre", "composer", "format", "folder", "quality",
               "mood", "tempo")
NUMBER_FIELDS = ("year", "bits", "sample_rate", "bit_rate", "duration", "added_days", "bpm")
YESNO_FIELDS = ("lossless", "liked", "disliked")
FIELDS = TEXT_FIELDS + NUMBER_FIELDS + YESNO_FIELDS

# what the tab window's menus show (English; the apps show them translated)
FIELD_LABELS = {
    "title": "Title", "artist": "Artist", "album": "Album", "album_artist": "Album artist", "genre": "Genre",
    "composer": "Composer", "format": "Format", "folder": "Folder", "quality": "Quality", "year": "Year",
    "bits": "Bit depth", "sample_rate": "Sample rate (Hz)", "bit_rate": "Bit rate (kbps)",
    "duration": "Length (seconds)", "added_days": "Days since added", "lossless": "Lossless", "liked": "Liked",
    "disliked": "Disliked", "mood": "Mood", "tempo": "Tempo", "bpm": "Tempo (BPM)",
}
TEXT_OPS = ("is", "is_not", "contains", "not_contains", "starts_with")
NUMBER_OPS = ("=", "!=", ">", ">=", "<", "<=")
YESNO_OPS = ("is",)
TEXT_OP_LABELS = {"is": "is", "is_not": "is not", "contains": "contains", "not_contains": "does not contain",
                  "starts_with": "starts with"}
NUMBER_OP_LABELS = {"=": "is", "!=": "is not", ">": "is more than", ">=": "is at least", "<": "is less than",
                    "<=": "is at most"}
YESNO_OP_LABELS = {"is": "is"}
MATCH_LABELS = {"all": "Match all of these rules", "any": "Match any of these rules"}
YES, NO = "Yes", "No"
# the values a field offers in a menu instead of an entry
QUALITY_VALUES = ("Hi-Res Lossless", "Lossless", "Lossy")
# 2.4: the labels of moods.py (moods.MOODS, moods.TEMPOS; a song not analysed yet has none)
MOOD_VALUES = ("Happy", "Sad", "Chill", "Intense")
TEMPO_VALUES = ("Up-tempo", "Mid-tempo", "Slow")
VALUE_CHOICES = {"quality": QUALITY_VALUES, "mood": MOOD_VALUES, "tempo": TEMPO_VALUES}


# the messages without values (the apps show them translated; those with values have patterns)
NOT_A_TAB = "This file is not a tab of Universal Music Player."
UNKNOWN_VERSION = "This tab has a format version that Universal Music Player does not know."
NEWER_VERSION = "This tab was made by a newer version of Universal Music Player. Update the app to use it."
NO_NAME = "Give the tab a name."
BAD_MATCH = "A tab matches “all” or “any” of its rules."
RULES_NOT_A_LIST = "The rules of a tab must be a list."
TOO_MANY_RULES = "A tab can have at most 100 rules."
SORT_NOT_A_FIELD = "A tab is sorted by one of the fields of its rules."
BAD_DESCENDING = "Descending must be true or false."
BAD_LIMIT = "The limit must be a whole number from 0 to 100000 (0: no limit)."
TOO_LARGE = "The file is larger than 1 MB, which no tab is."
UNREADABLE = "The file could not be read."
IN_FOLDER = "This tab is in the tabs folder already."
FIXED_MESSAGES = (NOT_A_TAB, UNKNOWN_VERSION, NEWER_VERSION, NO_NAME, BAD_MATCH, RULES_NOT_A_LIST, TOO_MANY_RULES,
                  SORT_NOT_A_FIELD, BAD_DESCENDING, BAD_LIMIT, TOO_LARGE, UNREADABLE, IN_FOLDER)


class TabError(ValueError):
    """A tab (or tab file) that cannot be used; the message says why in a full sentence."""


@dataclass
class TabContext:
    """What rules need besides the song: the liked and disliked songs, the time now
    (Unix seconds, for `added_days`) and the songs' moods (2.4: path → moods.Mood, for
    `bpm`, `mood` and `tempo`; a song not analysed yet has BPM 0 and no labels)."""

    liked: Set[str] = field(default_factory=set)
    disliked: Set[str] = field(default_factory=set)
    now: float = 0.0
    moods: dict = field(default_factory=dict)


# ---------------------------------------------------------------- fields ---
def kind_of(field_name: str) -> str:
    """"text", "number" or "yesno" ("" for a field tabs do not know)."""
    if field_name in TEXT_FIELDS:
        return "text"
    if field_name in NUMBER_FIELDS:
        return "number"
    if field_name in YESNO_FIELDS:
        return "yesno"
    return ""


def ops_for(field_name: str) -> Tuple[str, ...]:
    return {"text": TEXT_OPS, "number": NUMBER_OPS, "yesno": YESNO_OPS}.get(kind_of(field_name), ())


def op_label(field_name: str, op: str) -> str:
    labels = {"text": TEXT_OP_LABELS, "number": NUMBER_OP_LABELS, "yesno": YESNO_OP_LABELS}.get(kind_of(field_name), {})
    return labels.get(op, op)


def default_value(field_name: str):
    kind = kind_of(field_name)
    if field_name in VALUE_CHOICES:
        return VALUE_CHOICES[field_name][0]
    return {"number": 0, "yesno": True}.get(kind, "")


# the operator a new rule starts with: one that every song passes until a value is given
DEFAULT_OPS = {"text": "contains", "number": ">=", "yesno": "is"}


def default_op(field_name: str) -> str:
    return DEFAULT_OPS.get(kind_of(field_name), "is")


def new_rule(field_name: str = "title") -> dict:
    """A rule for `field_name` with its usual operator and a value of the right kind."""
    return {"field": field_name, "op": default_op(field_name), "value": default_value(field_name)}


def change_field(rule: dict, field_name: str) -> dict:
    """The rule with another field: the operator and the value are kept when they fit it."""
    out = {"field": field_name, "op": rule.get("op"), "value": rule.get("value")}
    if out["op"] not in ops_for(field_name):
        out["op"] = default_op(field_name)
    kind = kind_of(field_name)
    v = out["value"]
    fits = ((kind == "text" and isinstance(v, str)) or (kind == "number" and _is_number(v))
            or (kind == "yesno" and isinstance(v, bool)))
    if field_name in VALUE_CHOICES and v not in VALUE_CHOICES[field_name]:
        fits = False
    if kind_of(rule.get("field", "")) != kind or not fits:
        out["value"] = default_value(field_name)
    return out


def new_tab(name: str = "") -> dict:
    return {"format": TAB_FORMAT, "version": TAB_VERSION, "name": name, "match": "all", "rules": [],
            "sort": "title", "descending": False, "limit": 0}


def _template(name: str, rules: List[dict], sort: str = "title", descending: bool = False) -> dict:
    return dict(new_tab(name), rules=rules, sort=sort, descending=descending)


# "Start from" of New Tab… (the names are shown translated).  Hi-Res is the app's own Hi-Res
# Lossless (lossless with more than 16 bits, floating point or above 48 kHz): the rules of a
# tab are all or any of a flat list, which cannot say "lossless and (24-bit or above 48 kHz)".
TEMPLATES = [
    _template("Hi-Res", [{"field": "quality", "op": "is", "value": "Hi-Res Lossless"}]),
    _template("Lossy", [{"field": "lossless", "op": "is", "value": False}]),
    _template("Added in the last 30 days", [{"field": "added_days", "op": "<=", "value": 30}], sort="added_days"),
    _template("Long songs", [{"field": "duration", "op": ">=", "value": 600}]),
]


def _is_int(v) -> bool:
    """A whole number of JSON that both versions read as one (a 64-bit integer)."""
    return isinstance(v, int) and not isinstance(v, bool) and -2 ** 63 <= v < 2 ** 63


def _number(v):
    """A rule's number as it is kept: whole numbers up to MAX_INT stay whole, larger ones
    and decimals are floats (the Rust version reads a whole number beyond 64 bits as one);
    None when `v` is not a finite number."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, int):
        if abs(v) <= MAX_INT:
            return v
        try:
            v = float(v)
        except OverflowError:
            return None
    return v if math.isfinite(v) else None


def _is_number(v) -> bool:
    return _number(v) is not None


def clean_name(name) -> str:
    return " ".join(str(name or "").split())[:MAX_NAME]


def _shown(text: str) -> str:
    return text if len(text) <= SHOWN_CHARS else text[:SHOWN_CHARS - 1] + "…"


# ----------------------------------------------------------------- checks ---
def check_rule(rule, n: int) -> dict:
    """Rule number `n` of a tab, checked (TabError) and as it is saved."""
    if not isinstance(rule, dict):
        raise TabError(f"Rule {n} is not a rule: a rule has a field, an operator and a value.")
    f = rule.get("field")
    if not isinstance(f, str) or not f:
        raise TabError(f"Rule {n} has no field.")
    if f not in FIELDS:
        raise TabError(f"Rule {n} uses the field “{_shown(f)}”, which tabs do not know.")
    label = FIELD_LABELS[f]
    op = rule.get("op")
    if not isinstance(op, str) or not op:
        raise TabError(f"Rule {n} has no operator.")
    if op not in ops_for(f):
        raise TabError(f"Rule {n}: “{label}” cannot use the operator “{_shown(op)}”.")
    if "value" not in rule:
        raise TabError(f"Rule {n} has no value.")
    v = rule["value"]
    kind = kind_of(f)
    if kind == "text" and not isinstance(v, str):
        raise TabError(f"Rule {n}: the value of “{label}” must be a text.")
    if kind == "number":
        v = _number(v)
        if v is None:
            raise TabError(f"Rule {n}: the value of “{label}” must be a number.")
    if kind == "yesno" and not isinstance(v, bool):
        raise TabError(f"Rule {n}: the value of “{label}” must be yes or no.")
    return {"field": f, "op": op, "value": v}


def check_tab(data) -> dict:
    """The tab as it is saved (without its "file"), or TabError saying in a full sentence
    what is wrong: not a tab, a format version this app does not know, no name, an
    unknown field or operator, a value of the wrong kind, a wrong sort or limit.  Keys
    that are left out get their defaults (match all, no rules, sorted by title, no limit)."""
    if not isinstance(data, dict) or data.get("format") != TAB_FORMAT:
        raise TabError(NOT_A_TAB)
    version = data.get("version")
    if not _is_int(version) or version < 1:
        raise TabError(UNKNOWN_VERSION)
    if version > TAB_VERSION:
        raise TabError(NEWER_VERSION)
    name = clean_name(data.get("name")) if isinstance(data.get("name"), str) else ""
    if not name:
        raise TabError(NO_NAME)
    match = data.get("match", "all")
    if match not in ("all", "any"):
        raise TabError(BAD_MATCH)
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise TabError(RULES_NOT_A_LIST)
    if len(rules) > MAX_RULES:
        raise TabError(TOO_MANY_RULES)
    checked = [check_rule(r, i + 1) for i, r in enumerate(rules)]
    sort = data.get("sort", "title")
    if not isinstance(sort, str):
        raise TabError(SORT_NOT_A_FIELD)
    if sort not in FIELDS:
        raise TabError(f"A tab cannot be sorted by “{_shown(sort)}”.")
    descending = data.get("descending", False)
    if not isinstance(descending, bool):
        raise TabError(BAD_DESCENDING)
    limit = data.get("limit", 0)
    if not _is_int(limit) or limit < 0 or limit > MAX_LIMIT:
        raise TabError(BAD_LIMIT)
    return {"format": TAB_FORMAT, "version": TAB_VERSION, "name": name, "match": match, "rules": checked,
            "sort": sort, "descending": descending, "limit": limit}


def check_name(name, tabs: Sequence[dict], renaming: str = "") -> str:
    """The cleaned name for a new or renamed tab, or TabError (empty, or taken in any case)."""
    clean = clean_name(name)
    if not clean:
        raise TabError(NO_NAME)
    key = clean.casefold()
    if any(t["name"].casefold() == key and t["name"] != renaming for t in tabs):
        raise TabError(f"You already have a tab called “{clean}”.")
    return clean


def unique_name(name: str, taken: Iterable[str]) -> str:
    """`name`, or "name 2", "name 3"... when it is taken (in any case)."""
    keys = {t.casefold() for t in taken}
    out, n = name, 2
    while out.casefold() in keys:
        suffix = f" {n}"
        out = clean_name(name[:MAX_NAME - len(suffix)].rstrip() + suffix)
        n += 1
    return out


# ----------------------------------------------------------------- values ---
_INT_TEXT = re.compile(r"[+-]?[0-9]{1,15}\Z")
_FLOAT_TEXT = re.compile(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?\Z")


def parse_value(field_name: str, text: str):
    """What a value typed into the tab window means for `field_name`: for a number field
    a whole number or a decimal ("24", "44.1", "1e3"; anything else stays text, which
    check_tab() refuses with its message), else the text as typed."""
    if kind_of(field_name) != "number":
        return text
    s = text.strip()
    if _INT_TEXT.match(s):
        return int(s)
    if _FLOAT_TEXT.match(s):
        v = float(s)
        if math.isfinite(v):
            return v
    return text


def value_text(value) -> str:
    """A rule's value as the tab window's entry shows it."""
    if isinstance(value, bool):
        return YES if value else NO
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _year(t) -> int:
    y = t.year or ""
    return int(y) if len(y) == 4 and y.isascii() and y.isdigit() else 0


def field_value(field_name: str, t, ctx: TabContext):
    """The value of a song's field as rules see it (a musiclib.Track)."""
    if field_name == "title":
        return t.display_title
    if field_name == "artist":
        return t.artist or t.album_artist
    if field_name in ("album", "album_artist", "genre", "composer"):
        return getattr(t, field_name)
    if field_name == "format":
        return t.codec.upper()
    if field_name == "folder":
        return os.path.dirname(t.path)
    if field_name == "quality":
        if t.is_lossy:
            return "Lossy"
        return "Hi-Res Lossless" if t.sample_rate > 48000 or (t.bits or 16) > 16 or t.is_float else "Lossless"
    if field_name == "year":
        return _year(t)
    if field_name == "bits":
        return 0 if t.is_lossy else int(t.bits or 0)
    if field_name == "sample_rate":
        return int(t.sample_rate or 0)
    if field_name == "bit_rate":
        return round(t.bit_rate / 1000) if t.bit_rate else 0
    if field_name == "duration":
        return float(t.duration or 0.0)
    if field_name == "added_days":
        return (ctx.now - t.mtime_ns / 1e9) / 86400.0
    if field_name == "lossless":
        return not t.is_lossy
    if field_name == "liked":
        return t.path in ctx.liked
    if field_name == "disliked":
        return t.path in ctx.disliked
    if field_name in ("bpm", "mood", "tempo"):
        m = ctx.moods.get(t.path)
        if field_name == "bpm":
            return int(m.bpm) if m is not None else 0
        return (m.mood if field_name == "mood" else m.tempo) if m is not None else ""
    return ""


def rule_matches(rule: dict, t, ctx: TabContext) -> bool:
    f, op, want = rule["field"], rule["op"], rule["value"]
    kind = kind_of(f)
    have = field_value(f, t, ctx)
    if kind == "text":
        a, b = str(have).strip().casefold(), str(want).strip().casefold()
        if op == "is":
            return a == b
        if op == "is_not":
            return a != b
        if op == "contains":
            return b in a
        if op == "not_contains":
            return b not in a
        return a.startswith(b)
    if kind == "number":
        a, b = float(have), float(want)
        return {"=": a == b, "!=": a != b, ">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
    return bool(have) == bool(want)


def matches(tab: dict, t, ctx: TabContext) -> bool:
    """Whether the song `t` belongs to the tab (a tab without rules has every song)."""
    rules = tab.get("rules") or []
    if not rules:
        return True
    if tab.get("match") == "any":
        return any(rule_matches(r, t, ctx) for r in rules)
    return all(rule_matches(r, t, ctx) for r in rules)


def _sort_value(field_name: str, t, ctx: TabContext):
    v = field_value(field_name, t, ctx)
    kind = kind_of(field_name)
    if kind == "text":
        return str(v).strip().casefold()
    if kind == "number":
        return float(v)
    return bool(v)


def tab_songs(tab: dict, tracks: Iterable, ctx: TabContext) -> List[str]:
    """The paths of the songs of the tab: the matching tracks sorted by the tab's sort
    field (descending if asked), then by title, artist and path; at most `limit` of
    them when it is above 0."""
    hits = [t for t in tracks if matches(tab, t, ctx)]
    hits.sort(key=lambda t: (t.display_title.casefold(), t.display_artist.casefold(), t.path))
    sort = tab.get("sort") if tab.get("sort") in FIELDS else "title"
    hits.sort(key=lambda t: _sort_value(sort, t, ctx), reverse=bool(tab.get("descending")))
    limit = tab.get("limit") or 0
    if limit > 0:
        hits = hits[:limit]
    return [t.path for t in hits]


# ------------------------------------------------------------------ files ---
def tabs_folder() -> str:
    """The tabs folder: `$REALHIRES_TABS`, else "tabs" in the data folder."""
    from .store import data_dir

    return os.environ.get(TABS_ENV) or os.path.join(data_dir(), FOLDER_NAME)


_BAD_CHARS = set('\\/:*?"<>|')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def tab_file_name(name: str) -> str:
    """The file name a tab is saved under: its name with the characters file systems do
    not allow replaced by "_", and ".json" ("Hi-Res" → "Hi-Res.json")."""
    out = "".join("_" if ch in _BAD_CHARS or ord(ch) < 32 or ord(ch) == 127 else ch for ch in clean_name(name))
    out = out.strip(" .")
    if not out:
        out = "tab"
    if out.split(".")[0].upper() in _RESERVED:
        out = "_" + out
    return out + ".json"


def tab_text(tab: dict) -> str:
    """The text of a tab file (checked first): keys in the order of the format, two
    spaces of indent, UTF-8 characters as they are, a newline at the end."""
    t = check_tab(tab)
    data = {"format": t["format"], "version": t["version"], "name": t["name"], "match": t["match"],
            "rules": [{"field": r["field"], "op": r["op"], "value": r["value"]} for r in t["rules"]],
            "sort": t["sort"], "descending": t["descending"], "limit": t["limit"]}
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def parse_tab(text: str) -> dict:
    """A tab from the text of a file (TabError when it is not one)."""
    try:
        data = json.loads(text.lstrip("﻿"))
    except ValueError:
        raise TabError(NOT_A_TAB) from None
    return check_tab(data)


def is_tab_text(text: str) -> bool:
    """Whether a .json file's text says it is a tab (its "format"), even a broken one."""
    try:
        data = json.loads(text.lstrip("﻿"))
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("format") == TAB_FORMAT


def _read(path: str) -> str:
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            raise TabError(TOO_LARGE)
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise TabError(UNREADABLE) from None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise TabError(NOT_A_TAB) from None


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _write(path: str, text: str) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".tab-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_tabs(folder: Optional[str] = None) -> Tuple[List[dict], List[Tuple[str, str]]]:
    """The tabs of the .json files in the tabs folder, in file-name order, each with its
    "file"; a name that is taken (in any case) gets " 2", " 3"...  Also the files that
    are not tabs, as (file name, why)."""
    d = folder or tabs_folder()
    try:
        names = sorted(n for n in os.listdir(d) if n.lower().endswith(".json") and not n.startswith("."))
    except OSError:
        return [], []
    tabs: List[dict] = []
    skipped: List[Tuple[str, str]] = []
    for n in names:
        p = os.path.join(d, n)
        if not os.path.isfile(p):
            continue
        try:
            t = parse_tab(_read(p))
        except TabError as exc:
            skipped.append((n, str(exc)))
            continue
        t["name"] = unique_name(t["name"], [x["name"] for x in tabs])
        t["file"] = p
        tabs.append(t)
    return tabs, skipped


def save_tab(tab: dict, folder: Optional[str] = None, old_file: Optional[str] = None) -> dict:
    """Write a tab into the tabs folder (checked first, through a temp file) as
    tab_file_name() of its name ("Hi-Res 2.json" when another file has that name);
    `old_file`, the file it was read from, is replaced: written over when the name gives
    the same file, else deleted once the new file is there.  Returns the tab with its
    "file"."""
    t = check_tab(tab)
    d = folder or tabs_folder()
    try:
        os.makedirs(d, exist_ok=True)
        stem = tab_file_name(t["name"])[:-5]
        n = 1
        while True:
            target = os.path.join(d, stem + (f" {n}" if n > 1 else "") + ".json")
            if not os.path.exists(target) or (old_file and _same_file(target, old_file)):
                break
            n += 1
        _write(target, tab_text(t))
        if old_file and os.path.exists(old_file) and not _same_file(old_file, target):
            os.remove(old_file)
    except OSError as exc:
        raise TabError(f"The tab could not be saved: {exc}") from None
    t["file"] = target
    return t


def delete_tab(tab: dict) -> bool:
    """Delete a tab's file; False when it was not there."""
    try:
        os.remove(tab.get("file") or "")
        return True
    except OSError:
        return False


def export_tab(tab: dict, path: str) -> str:
    """Write a tab file somewhere else (to share it)."""
    text = tab_text(tab)
    try:
        _write(path, text)
    except OSError as exc:
        raise TabError(f"The tab could not be exported: {exc}") from None
    return path


def import_tab(path: str, folder: Optional[str] = None) -> dict:
    """Copy a tab file into the tabs folder (a taken name gets " 2", " 3"...); returns
    the tab with its new "file".  TabError when the file is not a tab, or is in the
    tabs folder already."""
    d = folder or tabs_folder()
    if os.path.isdir(d) and _same_file(os.path.dirname(os.path.abspath(path)), d):
        raise TabError(IN_FOLDER)
    t = parse_tab(_read(path))
    tabs, _ = load_tabs(d)
    t["name"] = unique_name(t["name"], [x["name"] for x in tabs])
    return save_tab(t, d)


def context(liked: Iterable[str] = (), disliked: Iterable[str] = (), now: float = 0.0,
            moods: Optional[dict] = None) -> TabContext:
    return TabContext(set(liked), set(disliked), float(now), moods if moods is not None else {})


__all__ = ["TabError", "TabContext", "FIELDS", "FIELD_LABELS", "TEMPLATES", "check_tab", "check_rule", "check_name",
           "matches", "tab_songs", "load_tabs", "save_tab", "delete_tab", "export_tab", "import_tab",
           "tab_file_name", "tab_text", "tabs_folder", "parse_value", "value_text", "field_value"]
