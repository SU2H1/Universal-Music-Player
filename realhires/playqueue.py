"""The play queue of the player (2.0): what plays next, with the shuffle and repeat modes.

Shuffle modes: "off" (the list in its order), "songs" (the songs of the list the
playback was started from, shuffled), "library" (every song in the library),
"genre", "artist" and "album" (the songs that share the current song's genre,
artist or album).  Repeat modes: "off" (stop at the end), "song" (play the
current song again when it ends), "album" (start the current album again when
it ends) and "stop" (stop when the current song ends; it then switches back to
"off").  Disliked songs (`disliked`) are left out whenever the order is
shuffled, unless one of them is the song that was chosen.

Up Next can be edited (2.3): `move_upcoming()` moves one of its songs to
another place and `remove_upcoming()` takes one out, in any of its three parts
(the songs stepped back over with Previous, the queued songs, the rest of the
order).  A removed song (`removed`) stays out when the order is built again
(another shuffle mode, repeat album) until a new list is started with
`start()`.

The shuffled order comes from SplitMix64 and a Fisher-Yates shuffle with a
seed, so the Rust version (`rust/crates/core/src/playqueue.rs`) plays exactly
the same order for the same seed; `rust/crates/core/tests/python_queue.json`
holds this module's answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

SHUFFLE_MODES = ["off", "songs", "library", "genre", "artist", "album"]
SHUFFLE_LABELS = {"off": "Off", "songs": "Songs in this list", "library": "All songs in the library",
                  "genre": "Same genre", "artist": "Same artist", "album": "Same album"}
REPEAT_MODES = ["off", "song", "album", "stop"]
REPEAT_LABELS = {"off": "Off", "song": "Repeat song", "album": "Repeat album",
                 "stop": "Stop after this song"}
MASK = (1 << 64) - 1


class SplitMix64:
    """A tiny, well-known PRNG whose output both versions reproduce bit for bit."""

    def __init__(self, seed: int):
        self.state = seed & MASK

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        """A number in 0..n-1 (n >= 1)."""
        return self.next() % n


def shuffled(items: List[str], rng: SplitMix64) -> List[str]:
    out = list(items)
    for i in range(len(out) - 1, 0, -1):
        j = rng.below(i + 1)
        out[i], out[j] = out[j], out[i]
    return out


@dataclass
class Song:
    """What the queue needs to know about a song."""
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    disc: int = 0
    track: int = 0

    @property
    def artist_key(self) -> str:
        return (self.album_artist or self.artist).strip().casefold()

    @property
    def album_key(self) -> str:
        return f"{self.album.strip().casefold()}\x00{self.artist_key}"

    @property
    def genre_key(self) -> str:
        return self.genre.strip().casefold()


def album_order(songs: Iterable[Song]) -> List[Song]:
    """Disc, then track number, then title and path (songs without numbers last)."""
    return sorted(songs, key=lambda s: (s.disc or 9999, s.track or 9999, s.title.casefold(), s.path))


def number(value) -> int:
    """'3/12' -> 3; anything unreadable -> 0."""
    text = str(value or "").strip().split("/")[0].strip()
    return int(text) if text.isascii() and text.isdigit() else 0


@dataclass
class PlayQueue:
    library: List[Song]
    context: List[str]                       # the list the playback was started from, in its order
    shuffle: str = "off"
    repeat: str = "off"
    seed: int = 0
    order: List[str] = field(default_factory=list)
    pos: int = 0
    disliked: Set[str] = field(default_factory=set)
    # songs the user queued with Play Next / Add to Queue: they play before `order` goes
    # on (each once, whatever the shuffle and repeat modes), and up_next() lists them first
    queued: List[str] = field(default_factory=list)
    top: int = 0  # the furthest position played: after Previous, Next walks back up to it first
    # positions in `order` that hold queued songs: repeat album ignores them (a queued song
    # from another album is an interruption, and the album being repeated goes on after it)
    inserted: Set[int] = field(default_factory=set)
    # songs taken out of Up Next by the user: left out whenever the order is built again,
    # until the next start()
    removed: Set[str] = field(default_factory=set)

    def __post_init__(self):
        self.by_path: Dict[str, Song] = {s.path: s for s in self.library}
        self.rng = SplitMix64(self.seed)
        if self.shuffle not in SHUFFLE_MODES:
            self.shuffle = "off"
        if self.repeat not in REPEAT_MODES:
            self.repeat = "off"

    # ---------------------------------------------------------------- start ---
    def start(self, path: str) -> str:
        """Play `path` now and build the order of the songs after it."""
        self.removed = set()
        self.order = [path] + self._upcoming(path)
        self.pos = self.top = 0
        self.inserted = set()
        return path

    @property
    def current(self) -> Optional[str]:
        return self.order[self.pos] if 0 <= self.pos < len(self.order) else None

    def _pool(self, path: str) -> List[str]:
        """The songs the current shuffle (or repeat album) mode draws from, in list order."""
        cur = self.by_path.get(path)
        mode = self.shuffle
        if self.repeat == "album" and mode not in ("off", "songs", "album"):
            mode = "album"  # repeating the album keeps the playback inside it
        if mode in ("off", "songs"):
            if self.repeat == "album" and cur is not None:
                return [s.path for s in album_order(x for x in self.library if x.album_key == cur.album_key)]
            return list(self.context)
        if mode == "library" or cur is None:
            return [s.path for s in self.library]
        key = {"genre": "genre_key", "artist": "artist_key", "album": "album_key"}[mode]
        want = getattr(cur, key)
        songs = [s for s in self.library if getattr(s, key) == want]
        return [s.path for s in (album_order(songs) if mode == "album" else songs)]

    def _upcoming(self, path: str) -> List[str]:
        pool = self._pool(path)
        if self.shuffle == "off":
            if path in pool:
                pool = pool[pool.index(path) + 1:]
            return [p for p in pool if p not in self.removed]
        return shuffled([p for p in pool if p != path and p not in self.disliked and p not in self.removed],
                        self.rng)

    def set_shuffle(self, mode: str) -> None:
        self.shuffle = mode if mode in SHUFFLE_MODES else "off"
        self._rebuild()

    def set_repeat(self, mode: str) -> None:
        self.repeat = mode if mode in REPEAT_MODES else "off"
        if self.repeat == "album":
            self._rebuild()

    def _rebuild(self) -> None:
        cur = self.current
        if cur is not None:
            self.order = self.order[:self.pos + 1] + self._upcoming(cur)
            self.top = self.pos
            self.inserted = {i for i in self.inserted if i <= self.pos}  # the songs after it are new

    # ----------------------------------------------------------------- moves ---
    def next(self, auto: bool = False) -> Optional[str]:
        """The song to play next: `auto` when the current one ended by itself
        (repeat song and stop after this song apply), otherwise the Next button.
        None means stop."""
        cur = self.current
        if cur is None:
            return None
        if auto and self.repeat == "song":
            return cur
        if auto and self.repeat == "stop":
            self.repeat = "off"
            return None
        if self.queued and self.pos >= self.top:
            nxt = self.queued.pop(0)
            self._insert_order(self.pos + 1, nxt, True)
            self.pos = self.top = self.pos + 1
            return nxt
        if self.pos + 1 < len(self.order):
            nxt = self.order[self.pos + 1]
            if self.repeat == "album" and self.pos + 1 not in self.inserted:
                base = self._album_song()
                a, b = self.by_path.get(base), self.by_path.get(nxt)
                if a is not None and b is not None and a.album_key != b.album_key:
                    # the album being repeated starts again (as in the Rust version; it was the
                    # current song's album, which is another one when that song was queued)
                    return self._restart_album(base)
            self.pos += 1
            self.top = max(self.top, self.pos)
            return nxt
        if self.repeat == "album":
            return self._restart_album(self._album_song())
        return None

    def _album_song(self) -> str:
        """The song whose album repeat album keeps to: the current one, or when that was
        queued, the last one played before it that was not."""
        i = self.pos
        while i > 0 and i in self.inserted:
            i -= 1
        return self.order[i]

    def _restart_album(self, cur: str) -> Optional[str]:
        song = self.by_path.get(cur)
        if song is None:
            return None
        tracks = [s.path for s in album_order(x for x in self.library if x.album_key == song.album_key)
                  if s.path == cur or s.path not in self.removed]
        if self.shuffle != "off":
            tracks = shuffled(tracks, self.rng)
        self.order = self.order[:self.pos + 1] + tracks
        self.inserted = {i for i in self.inserted if i <= self.pos}
        self.pos += 1
        self.top = self.pos
        return self.order[self.pos]

    def previous(self) -> Optional[str]:
        """The song before the current one (None at the start: play it again)."""
        if self.pos > 0:
            self.pos -= 1
            return self.order[self.pos]
        return None

    def up_next(self, count: int = 50) -> List[str]:
        """The songs to come: those played before and stepped back over with Previous, the
        queued ones, then the rest of the order."""
        top = max(self.top, self.pos)
        return (self.order[self.pos + 1:top + 1] + self.queued + self.order[top + 1:top + 1 + count])[:count]

    # ------------------------------------------------------- the user's queue ---
    def play_next(self, paths: List[str]) -> None:
        """Play Next: these songs come right after the current one, before songs queued earlier."""
        self.queued[0:0] = list(paths)

    def add_to_queue(self, paths: List[str]) -> None:
        """Add to Queue: these songs come after the songs queued earlier, then the list goes on."""
        self.queued.extend(paths)

    def jump(self, index: int) -> Optional[str]:
        """Up Next: play the song at `index` of up_next() now.  The songs listed before it
        are skipped (queued ones are dropped); the list goes on after it."""
        top = max(self.top, self.pos)
        back, n = top - self.pos, len(self.queued)
        if index < 0:
            return None
        if index < back:  # a song stepped back over: the queue stays
            self.pos += index + 1
            return self.order[self.pos]
        if index < back + n:
            k = index - back
            song = self.queued[k]
            del self.queued[:k + 1]
            self._insert_order(top + 1, song, True)
            self.pos = self.top = top + 1
            return song
        i = top + 1 + index - back - n
        if i >= len(self.order):
            return None
        self.queued.clear()
        self.pos = self.top = i
        return self.order[i]

    # ------------------------------------------------------- editing Up Next ---
    def _insert_order(self, i: int, path: str, mark: bool = False) -> None:
        """Put `path` at order[i]; `inserted` keeps pointing at the same songs (`mark`:
        this one counts as queued, an interruption for repeat album)."""
        self.inserted = {p + 1 if p >= i else p for p in self.inserted}
        self.order.insert(i, path)
        if mark:
            self.inserted.add(i)

    def _pop_order(self, i: int) -> Tuple[str, bool]:
        """Take order[i] out: (its path, whether it counted as queued)."""
        mark = i in self.inserted
        self.inserted = {p - 1 if p > i else p for p in self.inserted if p != i}
        return self.order.pop(i), mark

    def _take_upcoming(self, index: int) -> Optional[Tuple[str, bool, bool]]:
        """Take Up Next's song number `index` out of its part: (path, whether it counted
        as queued, whether it came from the list), or None when `index` is outside."""
        self.top = max(self.top, self.pos)
        back, n = self.top - self.pos, len(self.queued)
        if index < 0 or self.current is None:
            return None
        if index < back:            # a song stepped back over with Previous
            path, mark = self._pop_order(self.pos + 1 + index)
            self.top -= 1
            return path, mark, True
        if index < back + n:        # a queued song
            return self.queued.pop(index - back), True, False
        i = self.top + 1 + index - back - n
        if i >= len(self.order):
            return None
        path, mark = self._pop_order(i)
        return path, mark, True

    def upcoming_count(self) -> int:
        """How many songs Up Next holds in all (up_next() shows the first ones)."""
        top = max(self.top, self.pos)
        return (top - self.pos) + len(self.queued) + max(0, len(self.order) - top - 1)

    def move_upcoming(self, src: int, dst: int) -> bool:
        """Move Up Next's song number `src` (0 = the next song) to place `dst` (counted in
        the list after the move), into the part of the song it is now placed before (at
        the very end: the rest of the order).  So a song moved before the queued songs
        becomes queued, and a queued song moved among the list's songs becomes part of the
        order (still an interruption for repeat album).  False when either is outside Up
        Next or they are equal."""
        total = self.upcoming_count()
        if not (0 <= src < total and 0 <= dst < total) or src == dst:
            return False
        taken = self._take_upcoming(src)
        if taken is None:
            return False
        path, mark, _ = taken
        back, n = self.top - self.pos, len(self.queued)
        if dst < back:
            self._insert_order(self.pos + 1 + dst, path, mark)
            self.top += 1
        elif dst < back + n:
            self.queued.insert(dst - back, path)
        else:
            self._insert_order(self.top + 1 + dst - back - n, path, mark)
        return True

    def remove_upcoming(self, index: int) -> Optional[str]:
        """Take Up Next's song number `index` out of the queue and return its path (None
        when outside).  A song of the list (stepped back over, or still to come) stays out
        when the order is built again, unless it is the song playing."""
        taken = self._take_upcoming(index)
        if taken is None:
            return None
        path, _, from_list = taken
        if from_list and path != self.current:
            self.removed.add(path)
        return path
