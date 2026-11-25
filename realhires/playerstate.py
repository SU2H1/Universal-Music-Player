"""What the player remembers between runs (2.0): liked and disliked songs, volume,
the shuffle and repeat modes, the equalizer and the user's named EQ presets, the
playlists, the song that was playing, and the size of the lyrics on Now Playing.

One JSON file, `player.json`, next to the saved results (`store.data_dir()`), or
`$REALHIRES_PLAYER`.  The Rust version reads and writes the same file
(`rust/crates/core/src/playerstate.rs`), so both players share likes and EQ.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
import time
from typing import List, Optional

from . import eq
from .playqueue import REPEAT_MODES, SHUFFLE_MODES

PLAYER_ENV = "REALHIRES_PLAYER"
FILE_NAME = "player.json"
STATE_VERSION = 1
MAX_NAME = 80  # characters of a playlist or preset name
# the size of the lyrics on Now Playing (A− / A+), in pixels
LYRICS_SIZE_DEFAULT = 26
LYRICS_SIZE_MIN = 16
LYRICS_SIZE_MAX = 48
LYRICS_SIZE_STEP = 2


# The columns of song lists (2.1, chosen by the user per kind of list, "columns" in
# player.json): every column in the order the tables show them, and the defaults.
SONG_COLUMNS = ("num", "title", "artist", "album", "album_artist", "composer", "genre", "year", "disc", "time",
                "quality", "format", "bit_rate", "added", "file", "like")
COLUMN_KINDS = ("songs", "folders", "album")
DEFAULT_COLUMNS = {"songs": ("title", "artist", "album", "genre", "time", "quality", "like"),
                   "folders": ("title", "artist", "album", "genre", "time", "quality", "like"),
                   "album": ("num", "title", "artist", "album", "genre", "time", "quality", "like")}


def normalize_columns(kind: str, names) -> List[str]:
    """The known column names of `names`, in table order, with Title always there; the
    default when none is left."""
    wanted = {n for n in names if isinstance(n, str) and n in SONG_COLUMNS} if isinstance(names, list) else set()
    if not wanted:
        return list(DEFAULT_COLUMNS[kind])
    wanted.add("title")
    return [c for c in SONG_COLUMNS if c in wanted]


# Colour labels (2.3, "allow user to color code anything"): {kind: {key: "#rrggbb"}} in
# player.json.  The keys: a song's path, an album's key (playqueue.Song.album_key), the
# name of an artist, album artist, genre or composer (case-folded, as the groupings join
# names in any case), a year as written, a playlist's id and a tab's name.
LABEL_KINDS = ("song", "album", "artist", "album_artist", "genre", "year", "composer", "playlist", "tab")
_FOLDED_KINDS = ("artist", "album_artist", "genre", "composer")
# the fixed colours of the Colour Label menu (Apple's system colours, which read on light
# and dark themes); Custom… takes any other colour
LABEL_COLORS = (("Red", "#ff3b30"), ("Orange", "#ff9500"), ("Yellow", "#ffcc00"), ("Green", "#34c759"),
                ("Blue", "#007aff"), ("Purple", "#af52de"), ("Grey", "#8e8e93"))
_HEX_DIGITS = set("0123456789abcdef")


def clean_label_color(value) -> str:
    """"#rrggbb" in lower case, or "" for anything else."""
    v = value.strip().lower() if isinstance(value, str) else ""
    return v if len(v) == 7 and v[0] == "#" and set(v[1:]) <= _HEX_DIGITS else ""


def label_key(kind: str, key) -> str:
    """The key a label of this kind is kept under ("" when there is none): names of
    artists, album artists, genres and composers case-folded, the others as they are."""
    if kind not in LABEL_KINDS or not isinstance(key, str) or not key:
        return ""
    return key.casefold() if kind in _FOLDED_KINDS else key


class InvalidName(ValueError):
    """A playlist or preset name that cannot be used; the message says why."""


def clean_name(name) -> str:
    return " ".join(str(name or "").split())[:MAX_NAME]


def clean_lyrics_size(value) -> int:
    """A lyrics size as saved: a whole number of pixels from LYRICS_SIZE_MIN to
    LYRICS_SIZE_MAX (numbers and numeric text are rounded, half to even); anything
    else gives the default."""
    try:
        size = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return LYRICS_SIZE_DEFAULT
    return min(max(size, LYRICS_SIZE_MIN), LYRICS_SIZE_MAX)


def plain_lyrics_size(size: int) -> int:
    """The size of plain (not synced) lyrics shown at lyrics size `size`."""
    return int(round(clean_lyrics_size(size) * 0.7))


def default_path() -> str:
    from .store import data_dir

    return os.environ.get(PLAYER_ENV) or os.path.join(data_dir(), FILE_NAME)


@dataclass
class PlayerState:
    liked: List[str] = field(default_factory=list)  # absolute paths, sorted
    volume: float = 0.8                            # 0..1
    muted: bool = False
    shuffle: str = "off"
    repeat: str = "off"
    eq: dict = field(default_factory=eq.default_state)
    last_path: str = ""
    last_position: float = 0.0                     # seconds
    disliked: List[str] = field(default_factory=list)   # absolute paths, sorted
    # [{"name", "eq"}]: the user's own equalizer presets, in the order they were made
    eq_presets: List[dict] = field(default_factory=list)
    # [{"id", "name", "created", "songs": [paths]}]
    playlists: List[dict] = field(default_factory=list)
    lyrics_size: int = LYRICS_SIZE_DEFAULT          # pixels, Now Playing's A− / A+
    # {"songs" | "folders" | "album": [column names]}: the columns the user chose (2.1)
    columns: dict = field(default_factory=dict)
    # {kind: {key: "#rrggbb"}}: the colour labels (2.3, LABEL_KINDS)
    labels: dict = field(default_factory=dict)

    # ------------------------------------------------------ colour labels ---
    def label_of(self, kind: str, key) -> Optional[str]:
        """The colour label ("#rrggbb") of a song, album, artist... or None."""
        k = label_key(kind, key)
        return self.labels.get(kind, {}).get(k) if k else None

    def set_label(self, kind: str, key, color: Optional[str]) -> Optional[str]:
        """Give a song, album, artist... a colour label, or take it away (None, or a
        value that is not a colour); returns the label it has now."""
        k = label_key(kind, key)
        if not k:
            return None
        c = clean_label_color(color)
        if c:
            self.labels.setdefault(kind, {})[k] = c
            return c
        kept = self.labels.get(kind, {})
        kept.pop(k, None)
        if not kept:
            self.labels.pop(kind, None)
        return None

    # ------------------------------------------------------- lyrics size ---
    def step_lyrics_size(self, steps: int) -> int:
        """A+ (+1) or A− (-1): LYRICS_SIZE_STEP pixels larger or smaller per step, within
        the limits; returns the new size."""
        self.lyrics_size = clean_lyrics_size(clean_lyrics_size(self.lyrics_size) + steps * LYRICS_SIZE_STEP)
        return self.lyrics_size

    # ------------------------------------------------------ like/dislike ---
    def is_liked(self, path: str) -> bool:
        return path in self.liked

    def is_disliked(self, path: str) -> bool:
        return path in self.disliked

    def set_liked(self, path: str, liked: bool) -> None:
        s = set(self.liked)
        if liked:
            s.add(path)
            self.disliked = sorted(set(self.disliked) - {path})
        else:
            s.discard(path)
        self.liked = sorted(s)

    def set_disliked(self, path: str, disliked: bool) -> None:
        s = set(self.disliked)
        if disliked:
            s.add(path)
            self.liked = sorted(set(self.liked) - {path})
        else:
            s.discard(path)
        self.disliked = sorted(s)

    # ------------------------------------------------------- EQ presets ---
    def eq_preset(self, name: str) -> Optional[dict]:
        return next((p for p in self.eq_presets if p["name"] == name), None)

    def check_preset_name(self, name: str, renaming: str = "") -> str:
        name = clean_name(name)
        if not name:
            raise InvalidName("Give the preset a name.")
        if name.casefold() in {n.casefold() for n in eq.PRESETS} or name.casefold() == eq.CUSTOM.casefold():
            raise InvalidName(f"“{name}” is the name of a built-in preset; choose another one.")
        if any(p["name"].casefold() == name.casefold() and p["name"] != renaming for p in self.eq_presets):
            raise InvalidName(f"You already have a preset called “{name}”.")
        return name

    def save_eq_preset(self, name: str, state: dict, replace: bool = False) -> str:
        """Keep `state` under `name`; with `replace`, an existing preset of that name is updated."""
        clean = clean_name(name)
        old = self.eq_preset(clean)
        if old is not None and replace:
            old["eq"] = eq.normalize(state)
            return clean
        clean = self.check_preset_name(name)
        self.eq_presets.append({"name": clean, "eq": eq.normalize(state)})
        return clean

    def rename_eq_preset(self, old: str, new: str) -> str:
        p = self.eq_preset(old)
        if p is None:
            raise InvalidName(f"There is no preset called “{old}”.")
        p["name"] = self.check_preset_name(new, renaming=old)
        return p["name"]

    def delete_eq_preset(self, name: str) -> None:
        self.eq_presets = [p for p in self.eq_presets if p["name"] != name]

    def matching_eq_preset(self) -> str:
        """The name of the user preset the current EQ is, or "".  Whether the equalizer
        is on is not part of a preset (choosing one keeps the equalizer on), so it is not
        compared: a preset saved while the equalizer was off is still found once it is on."""
        cur = dict(eq.normalize(self.eq), enabled=False)
        return next((p["name"] for p in self.eq_presets if dict(eq.normalize(p["eq"]), enabled=False) == cur), "")

    # -------------------------------------------------------- playlists ---
    def playlist(self, pid: str) -> Optional[dict]:
        return next((p for p in self.playlists if p["id"] == pid), None)

    def unique_playlist_name(self, name: str, skip: str = "") -> str:
        base = clean_name(name) or "New Playlist"
        taken = {p["name"].casefold() for p in self.playlists if p["id"] != skip}
        if base.casefold() not in taken:
            return base
        n = 2
        while f"{base} {n}".casefold() in taken:
            n += 1
        return f"{base} {n}"

    def create_playlist(self, name: str, songs: Optional[List[str]] = None, now: Optional[float] = None) -> str:
        """A new playlist (a taken name gets " 2", " 3"...); returns its id."""
        created = float(now if now is not None else time.time())
        pid = f"{int(created * 1000):x}"
        while self.playlist(pid) is not None:
            pid = f"{int(pid, 16) + 1:x}"
        self.playlists.append({"id": pid, "name": self.unique_playlist_name(name), "created": created, "songs": []})
        if songs:
            self.add_to_playlist(pid, songs)
        return pid

    def rename_playlist(self, pid: str, name: str) -> str:
        p = self.playlist(pid)
        if p is None:
            raise InvalidName("That playlist no longer exists.")
        if not clean_name(name):
            raise InvalidName("Give the playlist a name.")
        p["name"] = self.unique_playlist_name(name, skip=pid)
        return p["name"]

    def delete_playlist(self, pid: str) -> None:
        self.playlists = [p for p in self.playlists if p["id"] != pid]

    def add_to_playlist(self, pid: str, paths: List[str]) -> int:
        """Add songs that are not in the playlist yet, at its end; returns how many."""
        p = self.playlist(pid)
        if p is None:
            return 0
        have = set(p["songs"])
        added = 0
        for path in paths:
            if isinstance(path, str) and path and path not in have:
                p["songs"].append(path)
                have.add(path)
                added += 1
        return added

    def remove_from_playlist(self, pid: str, paths: List[str]) -> None:
        p = self.playlist(pid)
        if p is not None:
            gone = set(paths)
            p["songs"] = [s for s in p["songs"] if s not in gone]

    def move_in_playlist(self, pid: str, path: str, delta: int) -> None:
        """Move a song up (-1) or down (+1)."""
        p = self.playlist(pid)
        if p is None or path not in p["songs"]:
            return
        i = p["songs"].index(path)
        j = min(max(i + delta, 0), len(p["songs"]) - 1)
        p["songs"].insert(j, p["songs"].pop(i))

    # ---------------------------------------------------------- columns ---
    def columns_of(self, kind: str) -> List[str]:
        """The columns a kind of song list shows ("songs", "folders", "album")."""
        return normalize_columns(kind, self.columns.get(kind))

    def set_columns(self, kind: str, names) -> List[str]:
        self.columns[kind] = normalize_columns(kind, list(names))
        return self.columns[kind]

    def toggle_column(self, kind: str, name: str) -> List[str]:
        """Show or hide one column (Title stays)."""
        cols = self.columns_of(kind)
        if name == "title" or name not in SONG_COLUMNS:
            return cols
        return self.set_columns(kind, [c for c in cols if c != name] if name in cols else cols + [name])

    def reset_columns(self, kind: str) -> List[str]:
        self.columns.pop(kind, None)
        return self.columns_of(kind)

    def to_json(self) -> dict:
        return {"_version": STATE_VERSION, "columns": {k: self.columns_of(k) for k in COLUMN_KINDS},
                "liked": sorted(set(self.liked)), "volume": self.volume,
                "muted": self.muted, "shuffle": self.shuffle, "repeat": "off" if self.repeat == "stop" else self.repeat, "eq": eq.normalize(self.eq),
                "last": {"path": self.last_path, "position": self.last_position},
                "disliked": sorted(set(self.disliked)),
                "eq_presets": [{"name": p["name"], "eq": eq.normalize(p["eq"])} for p in self.eq_presets],
                "playlists": [{"id": p["id"], "name": p["name"], "created": p["created"], "songs": list(p["songs"])}
                              for p in self.playlists],
                "lyrics_size": clean_lyrics_size(self.lyrics_size),
                "labels": {kind: dict(sorted(self.labels[kind].items())) for kind in LABEL_KINDS
                           if self.labels.get(kind)}}

    @classmethod
    def from_json(cls, data) -> "PlayerState":
        st = cls()
        if not isinstance(data, dict):
            return st
        liked = data.get("liked")
        if isinstance(liked, list):
            st.liked = sorted({p for p in liked if isinstance(p, str) and p})
        try:
            st.volume = min(max(float(data.get("volume", st.volume)), 0.0), 1.0)
        except (TypeError, ValueError):
            pass
        st.muted = bool(data.get("muted", False))
        st.shuffle = data.get("shuffle") if data.get("shuffle") in SHUFFLE_MODES else "off"
        # "stop after this song" is for the song playing now, never for the next start
        st.repeat = data.get("repeat") if data.get("repeat") in REPEAT_MODES and data.get("repeat") != "stop" \
            else "off"
        st.eq = eq.normalize(data.get("eq"))
        disliked = data.get("disliked")
        if isinstance(disliked, list):
            st.disliked = sorted({p for p in disliked if isinstance(p, str) and p} - set(st.liked))
        presets = data.get("eq_presets")
        if isinstance(presets, list):
            seen = set()
            for p in presets:
                if not isinstance(p, dict):
                    continue
                name = clean_name(p.get("name"))
                if name and name.casefold() not in seen:
                    seen.add(name.casefold())
                    st.eq_presets.append({"name": name, "eq": eq.normalize(p.get("eq"))})
        lists = data.get("playlists")
        if isinstance(lists, list):
            ids = set()
            for p in lists:
                if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not p["id"] or p["id"] in ids:
                    continue
                ids.add(p["id"])
                try:
                    created = float(p.get("created", 0.0))
                except (TypeError, ValueError):
                    created = 0.0
                songs = [x for x in p.get("songs", []) if isinstance(x, str) and x] \
                    if isinstance(p.get("songs"), list) else []
                st.playlists.append({"id": p["id"], "name": clean_name(p.get("name")) or "Playlist",
                                     "created": created, "songs": list(dict.fromkeys(songs))})
        st.lyrics_size = clean_lyrics_size(data.get("lyrics_size", LYRICS_SIZE_DEFAULT))
        cols = data.get("columns")
        if isinstance(cols, dict):
            st.columns = {k: normalize_columns(k, cols[k]) for k in COLUMN_KINDS if k in cols}
        labels = data.get("labels")
        if isinstance(labels, dict):
            for kind in LABEL_KINDS:
                given = labels.get(kind)
                if not isinstance(given, dict):
                    continue
                for key, color in given.items():
                    st.set_label(kind, key, color)
        last = data.get("last")
        if isinstance(last, dict):
            st.last_path = last.get("path") if isinstance(last.get("path"), str) else ""
            try:
                st.last_position = max(float(last.get("position", 0.0)), 0.0)
            except (TypeError, ValueError):
                st.last_position = 0.0
        return st


def load(path: Optional[str] = None) -> PlayerState:
    try:
        with open(path or default_path(), encoding="utf-8") as fh:
            return PlayerState.from_json(json.load(fh))
    except (OSError, ValueError):
        return PlayerState()


def save(state: PlayerState, path: Optional[str] = None) -> str:
    path = path or default_path()
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".player-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state.to_json(), fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
