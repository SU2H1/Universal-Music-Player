"""Listening statistics of the player (2.0): "Your Stats".

The player logs every song it plays (`musiclib.Library.add_play()`, the
`plays` table of `library.sqlite`).  This module turns those plays into the
numbers of the Stats page: a summary in the style of a yearly music review
(`cards()`: minutes listened, top song, artist, album and genre, listening hour
and day, longest streak...) and the details (`compute()`: top lists, listening
by hour, weekday and month), plus what the library holds (`library_stats()`).

A play counts (for "plays" and the top lists) once at least 30 seconds of the
song, or half of a shorter song, were heard; every logged second counts for
the minutes.  Hours, weekdays and days are local time.  The calendar at the
top of the Stats page (2.1) uses `month_grid()`, `month_minutes()`,
`month_levels()` and `day_plays()`, with the plays between the local
midnights of `month_bounds()` and `day_bounds()`.  The Rust version computes
the same numbers and texts (`rust/crates/core/src/stats.rs`), checked against
`rust/crates/core/tests/python_stats.json`.
"""

from __future__ import annotations

import calendar
import math
import os
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from .places import Place

COUNTED_SECONDS = 30.0
PERIODS = ["Last 30 days", "This year", "Last year", "All time"]
DEFAULT_PERIOD = "This year"
TOP = 100
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
FULL_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December"]
LEVELS = 4  # the calendar's tints: 1 (a little music) .. LEVELS (the month's busiest day)


@dataclass
class Play:
    path: str
    started: float
    seconds: float
    duration: float
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    year: str = ""
    composer: str = ""
    place: Optional["Place"] = None  # 2.4: where it was played (places.Place), None when not known


def counted(p: Play) -> bool:
    """Whether a play counts as a play (30 s, or half of a shorter song)."""
    return p.seconds >= min(COUNTED_SECONDS, max(p.duration, 2.0) / 2.0)


def _local(ts: float, localtime: Callable) -> Tuple[int, int, int, int, int]:
    t = localtime(ts)
    return t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_wday


def period_range(name: str, now: float, localtime: Callable = time.localtime,
                 mktime: Callable = time.mktime) -> Tuple[float, Optional[float]]:
    """[start, end) of a period in Unix time; end None = open."""
    year = localtime(now).tm_year
    if name == "Last 30 days":
        return now - 30 * 86400.0, None
    if name == "This year":
        return float(mktime((year, 1, 1, 0, 0, 0, 0, 1, -1))), None
    if name == "Last year":
        return float(mktime((year - 1, 1, 1, 0, 0, 0, 0, 1, -1))), float(mktime((year, 1, 1, 0, 0, 0, 0, 1, -1)))
    return 0.0, None


def _rank(items: Iterable[dict]) -> List[dict]:
    return sorted(items, key=lambda d: (-d["plays"], -d["seconds"], d["name"].casefold(), d["key"]))[:TOP]


def compute(plays: List[Play], localtime: Callable = time.localtime) -> dict:
    """Every number of the Stats page for these plays (oldest first)."""
    seconds = 0.0
    n_counted = 0
    songs: Dict[str, dict] = {}
    artists: Dict[str, dict] = {}
    albums: Dict[str, dict] = {}
    genres: Dict[str, dict] = {}
    by_hour = [0.0] * 24
    by_weekday = [0.0] * 7
    by_month: Dict[str, float] = {}
    by_day: Dict[str, float] = {}
    days: set = set()
    for p in plays:
        c = 1 if counted(p) else 0
        n_counted += c
        seconds += p.seconds
        artist = p.artist.strip() or p.album_artist.strip() or "Unknown Artist"
        album = p.album.strip() or "Unknown Album"
        album_artist = p.album_artist.strip() or artist
        genre = p.genre.strip() or "Unknown Genre"
        entries = (
            (songs, p.path, {"name": p.title or p.path, "artist": artist}),
            (artists, artist.casefold(), {"name": artist}),
            (albums, album.casefold() + "\x00" + album_artist.casefold(), {"name": album, "artist": album_artist}),
            (genres, genre.casefold(), {"name": genre}),
        )
        for table, key, base in entries:
            d = table.get(key)
            if d is None:
                d = table[key] = dict(base, key=key, plays=0, seconds=0.0, songs=set())
            d["plays"] += c
            d["seconds"] += p.seconds
            d["songs"].add(p.path)
        y, m, dd, hour, wday = _local(p.started, localtime)
        by_hour[hour] += p.seconds
        by_weekday[wday] += p.seconds
        month = f"{y:04d}-{m:02d}"
        by_month[month] = by_month.get(month, 0.0) + p.seconds
        day = f"{y:04d}-{m:02d}-{dd:02d}"
        by_day[day] = by_day.get(day, 0.0) + p.seconds
        days.add(date(y, m, dd).toordinal())
    streak = best = 0
    prev = None
    for o in sorted(days):
        streak = streak + 1 if prev is not None and o == prev + 1 else 1
        best = max(best, streak)
        prev = o
    busiest = max(by_day.items(), key=lambda kv: (kv[1], kv[0])) if by_day else None

    def out(table: Dict[str, dict], with_songs: bool) -> List[dict]:
        rows = []
        for d in table.values():
            r = {k: v for k, v in d.items() if k != "songs"}
            if with_songs:
                r["songs"] = len(d["songs"])
            rows.append(r)
        return _rank(rows)

    return {
        "seconds": seconds, "plays": n_counted, "logged": len(plays),
        "songs": len(songs), "artists": len(artists), "albums": len(albums), "genres": len(genres),
        "top_songs": out(songs, False), "top_artists": out(artists, True), "top_albums": out(albums, True),
        "top_genres": out(genres, True),
        "by_hour": by_hour, "by_weekday": by_weekday, "by_month": sorted(by_month.items()),
        "days": len(days), "streak": best, "busiest_day": list(busiest) if busiest else None,
        "first": plays[0].started if plays else None, "last": plays[-1].started if plays else None,
    }


def _whole(x: float) -> int:
    return int(x + 0.5)


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def hour_label(h: int) -> str:
    return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"


def day_label(day: str) -> str:
    y, m, d = (int(x) for x in day.split("-"))
    return f"{d} {MONTHS[m - 1]} {y}"


def cards(st: dict) -> List[Tuple[str, str, str]]:
    """(label, big text, detail) of the summary cards; empty without plays."""
    if not st["logged"]:
        return []
    minutes = _whole(st["seconds"] / 60.0)
    hours = st["seconds"] / 3600.0
    if hours >= 48:
        about = f"About {_whole(hours / 24.0)} days of music."
    elif hours >= 2:
        about = f"About {_whole(hours)} hours of music."
    else:
        about = "Your listening has just begun."
    out = [("Minutes listened", f"{minutes:,}", about)]
    if st["top_songs"]:
        s = st["top_songs"][0]
        out.append(("Your top song", s["name"], f"{s['artist']} · {_plural(s['plays'], 'play')}"))
    if st["top_artists"]:
        a = st["top_artists"][0]
        out.append(("Your top artist", a["name"],
                    f"{_plural(a['plays'], 'play')}, {_plural(_whole(a['seconds'] / 60.0), 'minute')}"))
    if st["top_albums"]:
        a = st["top_albums"][0]
        out.append(("Your top album", a["name"], a["artist"]))
    if st["top_genres"]:
        g = st["top_genres"][0]
        share = _whole(100.0 * g["seconds"] / st["seconds"]) if st["seconds"] > 0 else 0
        out.append(("Your top genre", g["name"], f"{share}% of your listening time"))
    out.append(("Songs you played", f"{st['songs']:,}", f"by {_plural(st['artists'], 'artist')}"))
    hour = max(range(24), key=lambda h: (st["by_hour"][h], -h))
    out.append(("Your listening hour", hour_label(hour), "when you listen most"))
    wday = max(range(7), key=lambda d: (st["by_weekday"][d], -d))
    out.append(("Your listening day", WEEKDAYS[wday], "the day you listen most"))
    out.append(("Longest streak", _plural(st["streak"], "day"), "in a row with music"))
    if st["busiest_day"]:
        day, secs = st["busiest_day"]
        out.append(("Your biggest day", day_label(day), f"{_plural(_whole(secs / 60.0), 'minute')} of music"))
    return out


# ----------------------------------------------------------------- calendar ---
def shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    """The month `delta` months after (year, month)."""
    n = year * 12 + (month - 1) + delta
    return n // 12, n % 12 + 1


def month_title(year: int, month: int) -> str:
    """'November 2026'."""
    return f"{FULL_MONTHS[month - 1]} {year}"


def day_title(year: int, month: int, day: int) -> str:
    """'Tuesday 24 November 2026'."""
    return f"{WEEKDAYS[date(year, month, day).weekday()]} {day} {FULL_MONTHS[month - 1]} {year}"


def month_grid(year: int, month: int) -> List[List[int]]:
    """The weeks of a month as the calendar shows them, Monday first: the day
    numbers, 0 for the cells before the 1st and after the last day."""
    cells = [0] * date(year, month, 1).weekday() + list(range(1, calendar.monthrange(year, month)[1] + 1))
    cells += [0] * (-len(cells) % 7)
    return [cells[i:i + 7] for i in range(0, len(cells), 7)]


def _midnight(year: int, month: int, day: int, mktime: Callable) -> float:
    return float(mktime((year, month, day, 0, 0, 0, 0, 1, -1)))


def month_bounds(year: int, month: int, mktime: Callable = time.mktime) -> Tuple[float, float]:
    """[start, end) of a month in Unix time: the local midnights of its 1st and of the next month's."""
    ny, nm = shift_month(year, month, 1)
    return _midnight(year, month, 1, mktime), _midnight(ny, nm, 1, mktime)


def day_bounds(year: int, month: int, day: int, mktime: Callable = time.mktime) -> Tuple[float, float]:
    """[start, end) of a day in Unix time: its local midnight and the next one."""
    nxt = date(year, month, day) + timedelta(days=1)
    return _midnight(year, month, day, mktime), _midnight(nxt.year, nxt.month, nxt.day, mktime)


def month_minutes(plays: Iterable[Play], year: int, month: int, localtime: Callable = time.localtime) -> List[float]:
    """The minutes listened on each day of the month (index 0 is the 1st), by the
    local day each play started on; plays of other months are left out."""
    out = [0.0] * calendar.monthrange(year, month)[1]
    for p in plays:
        y, m, d, _, _ = _local(p.started, localtime)
        if (y, m) == (year, month):
            out[d - 1] += p.seconds / 60.0
    return out


def month_levels(minutes: List[float]) -> List[int]:
    """The calendar's tint of each day: 0 without music, else 1..LEVELS by its
    minutes against the month's busiest day."""
    top = max(minutes, default=0.0)
    return [0 if m <= 0.0 or top <= 0.0 else min(LEVELS, max(1, math.ceil(LEVELS * m / top))) for m in minutes]


def minutes_label(seconds: float) -> str:
    """How long a play was heard, for the calendar's list: '<1 min', '1 min', '1,234 min'."""
    m = _whole(seconds / 60.0)
    return "<1 min" if m < 1 else f"{m:,} min"


def day_plays(plays: Iterable[Play], year: int, month: int, day: int,
              localtime: Callable = time.localtime) -> List[dict]:
    """The plays that started on one local day, newest first, as the calendar
    lists them: the local time ("14:05"), title, artist, minutes heard and (2.4) the
    place it was played at (places.place_text(), "" when not known)."""
    from .places import place_text

    rows = []
    for p in plays:
        t = localtime(p.started)
        if (t.tm_year, t.tm_mon, t.tm_mday) != (year, month, day):
            continue
        rows.append({
            "path": p.path, "started": p.started, "time": f"{t.tm_hour:02d}:{t.tm_min:02d}",
            "title": p.title.strip() or os.path.splitext(os.path.basename(p.path))[0],
            "artist": p.artist.strip() or p.album_artist.strip() or "Unknown Artist",
            "seconds": p.seconds, "minutes": minutes_label(p.seconds), "place": place_text(p.place),
        })
    rows.reverse()  # the plays come oldest first
    return rows


def day_summary(rows: List[dict]) -> str:
    """'3 songs played, 42 minutes' under the day's title; 'No music on this day.' without plays."""
    if not rows:
        return "No music on this day."
    return f"{_plural(len(rows), 'song')} played, {_plural(_whole(sum(r['seconds'] for r in rows) / 60.0), 'minute')}"


# -------------------------------------------------------------- leaderboard ---
LEADERBOARD_KINDS = ["songs", "artists", "albums"]  # 2.4: the switch of the leaderboard ("Most Played")


def _names(p: Play) -> Tuple[str, str, str, str]:
    """(artist, album, album artist, genre) of a play, as compute() names them."""
    artist = p.artist.strip() or p.album_artist.strip() or "Unknown Artist"
    return (artist, p.album.strip() or "Unknown Album", p.album_artist.strip() or artist,
            p.genre.strip() or "Unknown Genre")


def leaderboard(plays: Iterable[Play], kind: str = "songs", top: int = TOP) -> List[dict]:
    """The most played songs, artists or albums of these plays (2.4, "Most Played"):
    rows {"rank", "key", "name", "artist", "plays", "seconds", "minutes", "paths"} of the
    rows with at least one counted play, by plays, then the whole minutes listened, then
    the name; rows with the same plays and minutes share a rank (1, 2, 2, 4); at most
    `top` rows.  Keys are those of compute() (a song's path, the artist and the album and
    its artist case-folded); "artist" is a song's artist or an album's artist (empty for
    artists); "paths" are the row's songs, the most played first."""
    if kind not in LEADERBOARD_KINDS:
        raise ValueError(f"not a leaderboard: {kind!r}")
    rows: Dict[str, dict] = {}
    for p in plays:
        artist, album, album_artist, _genre = _names(p)
        if kind == "songs":
            key, name, who = p.path, p.title.strip() or os.path.splitext(os.path.basename(p.path))[0], artist
        elif kind == "artists":
            key, name, who = artist.casefold(), artist, ""
        else:
            key, name, who = album.casefold() + "\x00" + album_artist.casefold(), album, album_artist
        d = rows.get(key)
        if d is None:
            d = rows[key] = {"key": key, "name": name, "artist": who, "plays": 0, "seconds": 0.0, "songs": {}}
        c = 1 if counted(p) else 0
        d["plays"] += c
        d["seconds"] += p.seconds
        s = d["songs"].setdefault(p.path, [0, 0.0])
        s[0] += c
        s[1] += p.seconds
    out = []
    for d in rows.values():
        if d["plays"] < 1:
            continue
        songs = d.pop("songs")
        d["minutes"] = _whole(d["seconds"] / 60.0)
        d["paths"] = sorted(songs, key=lambda path: (-songs[path][0], -songs[path][1], path))
        out.append(d)
    out.sort(key=lambda d: (-d["plays"], -d["minutes"], d["name"].casefold(), d["key"]))
    out = out[:max(0, top)]
    for i, d in enumerate(out):
        same = i and (out[i - 1]["plays"], out[i - 1]["minutes"]) == (d["plays"], d["minutes"])
        d["rank"] = out[i - 1]["rank"] if same else i + 1
    return out


def ranked_songs(rows: Iterable[dict]) -> List[str]:
    """The songs of leaderboard rows in rank order (an artist's or album's songs, the most
    played first), each once: what Play and Shuffle of the leaderboard play."""
    out: List[str] = []
    seen = set()
    for d in rows:
        for path in d["paths"]:
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def library_stats(tracks) -> dict:
    """What the library holds: counts, total length and size, and how many
    songs of each format, quality, sample rate and bit depth."""
    from .musiclib import albums, artists

    tracks = list(tracks)
    formats: Dict[str, int] = {}
    qualities = {"Hi-Res Lossless": 0, "Lossless": 0, "Lossy": 0}
    rates: Dict[int, int] = {}
    bits: Dict[str, int] = {}
    for t in tracks:
        fmt = (t.codec or "?").upper()
        formats[fmt] = formats.get(fmt, 0) + 1
        if t.is_lossy:
            qualities["Lossy"] += 1
        elif (t.bits or 0) > 16 or t.is_float or (t.sample_rate or 0) > 48000:
            qualities["Hi-Res Lossless"] += 1
        else:
            qualities["Lossless"] += 1
        rates[int(t.sample_rate or 0)] = rates.get(int(t.sample_rate or 0), 0) + 1
        b = "lossy" if t.is_lossy else ("32-bit float" if t.is_float else (f"{t.bits}-bit" if t.bits else "?"))
        bits[b] = bits.get(b, 0) + 1
    return {
        "songs": len(tracks), "albums": len(albums(tracks)), "artists": len(artists(tracks)),
        "seconds": sum(float(t.duration or 0.0) for t in tracks), "bytes": sum(int(t.size or 0) for t in tracks),
        "formats": sorted(formats.items(), key=lambda kv: (-kv[1], kv[0])),
        "qualities": [[k, v] for k, v in qualities.items()],
        "rates": sorted(rates.items()),
        "bits": sorted(bits.items(), key=lambda kv: (-kv[1], kv[0])),
    }
