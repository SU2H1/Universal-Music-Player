"""Song information from MusicBrainz (https://musicbrainz.org), 2.1.

MusicBrainz is the open music encyclopedia behind MusicBrainz Picard
(https://github.com/metabrainz/picard).  The player uses its web service
(``/ws/2/``, JSON) to fill in or correct the tags of the user's songs: the
title, artist, album, album artist, release date, track and disc numbers,
genre and composer.

* By hand: "Get info from MusicBrainz…" searches for the album (or the song),
  lists the releases found, reads the chosen release's track list, matches the
  user's songs to its tracks (by disc and track number, then by title, then,
  for the same number of songs and tracks, in order) and shows what would
  change before anything is written.
* Automatically (the setting ``musicbrainz_auto``, off by default): after the
  library index is updated, songs that miss some information are looked up
  once; only a confident match is used (same album and artist, every song of
  the album found on the release by number or title, lengths within 3 s), and
  only empty tags are filled in, never replaced.  Every song looked up is
  recorded in the table ``musicbrainz`` of ``library.sqlite``, so it is not
  asked about again until the file changes.

MusicBrainz asks programs to send at most one request per second and to name
themselves in the User-Agent ("Application/version ( contact )"); a client
that sends more is answered with HTTP 503.  ``MusicBrainzClient`` sends one
request at a time, at least ``MIN_INTERVAL_S`` apart, and waits and retries on
503.  Only the tags of the songs are sent, never a path or a file name.

Tags are written with ``tagedit.save_tags()``, so only the tag of a file
changes, never its audio, and saved scan results and the library follow.

MusicBrainz's core data is released under CC0; tags and genres are
supplementary data under CC BY-NC-SA 3.0 (https://musicbrainz.org/doc/About/Data_License).
This module was written for Universal Music Player against the published
web-service documentation and contains no code from MusicBrainz or Picard.
The Rust version (``rust/crates/core/src/musicbrainz.rs``) builds the same
requests and makes the same choices (``rust/crates/core/tests/python_musicbrainz.json``).
"""

from __future__ import annotations

import http.client
import json
import os
import sqlite3
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import PROJECT_URL, __version__
from .lyrics import Transport, _network_reason, _urlopen, normalize_name, _same_artist as same_artist
from .tagedit import FIELDS, clean_value, split_number

DEFAULT_SERVER = "https://musicbrainz.org"
MIN_INTERVAL_S = 1.0         # MusicBrainz: never more than one call per second
TIMEOUT_S = 20.0
MAX_ATTEMPTS = 4             # per request, for 503/5xx answers and network errors
MAX_BACKOFF_S = 8.0
MAX_RETRY_AFTER_S = 600.0
INTERVAL_ENV = "REALHIRES_MUSICBRAINZ_INTERVAL"  # tests only; never below MIN_INTERVAL_S for musicbrainz.org
SEARCH_LIMIT = 10
DURATION_TOLERANCE_S = 3.0   # a song and a MusicBrainz track are the same length within this
AUTO_CANDIDATES = 3          # releases read at most per album in automatic mode
# everything a release lookup needs: tracks, credits, the release group (first date, genres) and composers
RELEASE_INC = ("recordings+artist-credits+release-groups+genres+media+work-rels+recording-level-rels+"
               "work-level-rels+artist-rels")
# a song is looked up automatically when one of these is empty (composers are often unknown, so not this one)
MISSING_FIELDS = ("artist", "album", "album_artist", "date", "track", "genre")
TYPE_RANK = {"Album": 0, "EP": 1, "Single": 2}

# statuses of the automatic lookup, kept in the table `musicbrainz`
FILLED, COMPLETE, NO_MATCH, NO_TAGS, FAILED, SAVED = "filled", "complete", "no-match", "no-tags", "failed", "saved"

CREDIT = ("Song information comes from MusicBrainz (musicbrainz.org), the open music encyclopedia: "
          "its core data is public domain (CC0), its genres are under CC BY-NC-SA 3.0.")
ABOUT_CREDIT = "Song information: MusicBrainz (musicbrainz.org), CC0 core data, genres CC BY-NC-SA 3.0."


class MusicBrainzError(Exception):
    """A lookup could not be made (the message is a full sentence)."""


class MusicBrainzStopped(Exception):
    """The user stopped the lookups."""


def user_agent() -> str:
    """MusicBrainz's recommended form: "Application/version ( contact-url )"."""
    return f"UniversalMusicPlayer/{__version__} ( {PROJECT_URL} )"


def normalize_server(server: str) -> str:
    s = (server or "").strip().rstrip("/") or DEFAULT_SERVER
    return s if "://" in s else "https://" + s


def is_official(server: str) -> bool:
    host = (urllib.parse.urlsplit(normalize_server(server)).hostname or "").lower()
    return host == "musicbrainz.org" or host.endswith(".musicbrainz.org")


def default_interval(server: str) -> float:
    try:
        value = float(os.environ.get(INTERVAL_ENV, ""))
    except ValueError:
        value = MIN_INTERVAL_S
    value = max(value, 0.0)
    return max(value, MIN_INTERVAL_S) if is_official(server) else value


# ------------------------------------------------------------------ queries ---
def phrase(text: str) -> str:
    """A Lucene phrase: the text in double quotes, with backslashes and quotes escaped."""
    return '"' + clean_value(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def release_query(album: str, artist: str = "") -> str:
    parts = [f"release:{phrase(album)}"]
    if clean_value(artist):
        parts.append(f"artist:{phrase(artist)}")
    return " AND ".join(parts)


def recording_query(title: str, artist: str = "", album: str = "") -> str:
    parts = [f"recording:{phrase(title)}"]
    if clean_value(artist):
        parts.append(f"artist:{phrase(artist)}")
    if clean_value(album):
        parts.append(f"release:{phrase(album)}")
    return " AND ".join(parts)


# ------------------------------------------------------------------- client ---
class MusicBrainzClient:
    """Sequential MusicBrainz requests (one client per batch, used from one thread)."""

    def __init__(self, server: str = DEFAULT_SERVER, *, stop: Optional[threading.Event] = None,
                 transport: Optional[Transport] = None, min_interval: Optional[float] = None,
                 timeout: float = TIMEOUT_S, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.server = normalize_server(server)
        self.stop = stop
        self.transport = transport or _urlopen
        self.min_interval = default_interval(self.server) if min_interval is None else min_interval
        self.timeout = timeout
        self.clock = clock
        self._sleep = sleep
        self._last_start: Optional[float] = None
        self._not_before = 0.0
        self.requests = 0

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.server).netloc or self.server

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.stop is not None:
            if self.stop.wait(seconds):
                raise MusicBrainzStopped()
        else:
            self._sleep(seconds)

    def _throttle(self) -> None:
        if self.stop is not None and self.stop.is_set():
            raise MusicBrainzStopped()
        due = self._not_before
        if self._last_start is not None:
            due = max(due, self._last_start + self.min_interval)
        self._wait(due - self.clock())

    def url(self, path: str, params: Sequence[Tuple[str, str]]) -> str:
        return f"{self.server}/ws/2/{path}?{urllib.parse.urlencode(list(params) + [('fmt', 'json')])}"

    def request(self, path: str, params: Sequence[Tuple[str, str]]) -> Optional[dict]:
        """The parsed JSON answer, or None for 404.  Raises MusicBrainzError / MusicBrainzStopped."""
        url = self.url(path, params)
        headers = {"User-Agent": user_agent(), "Accept": "application/json"}
        backoff, problem = 1.0, ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            self._last_start = self.clock()
            self.requests += 1
            try:
                status, hdrs, body = self.transport(url, headers, self.timeout)
            except ValueError as exc:
                raise MusicBrainzError(f"The MusicBrainz server address {self.server!r} is not valid: {exc}.") from exc
            except (OSError, http.client.HTTPException) as exc:
                if isinstance(getattr(exc, "reason", exc), ssl.SSLCertVerificationError):
                    raise MusicBrainzError(f"Could not connect to {self.host} securely: {_network_reason(exc)}.") from exc
                problem, wait = f"could not reach {self.host}: {_network_reason(exc)}", backoff
            else:
                if status == 200:
                    try:
                        data = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise MusicBrainzError(f"{self.host} sent an answer that could not be read.") from exc
                    return data if isinstance(data, dict) else None
                if status == 404:
                    return None
                if status in (429, 503):
                    try:
                        wait = min(max(float(hdrs.get("retry-after", "")), 0.0), MAX_RETRY_AFTER_S)
                    except ValueError:
                        wait = max(backoff, self.min_interval)
                    self._not_before = self.clock() + wait
                    problem = "the server asked to slow down" if status == 503 else "too many requests"
                elif status >= 500:
                    problem, wait = f"server error (HTTP {status})", backoff
                else:
                    raise MusicBrainzError(f"{self.host} refused the request (HTTP {status}{_api_message(body)}).")
            if attempt < MAX_ATTEMPTS:
                self._wait(wait)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
        raise MusicBrainzError(f"No answer from {self.host} after {MAX_ATTEMPTS} attempts ({problem}).")

    def search_releases(self, album: str, artist: str = "", limit: int = SEARCH_LIMIT) -> List["Release"]:
        data = self.request("release", [("query", release_query(album, artist)), ("limit", str(limit))])
        return [parse_release(r) for r in (data or {}).get("releases") or [] if isinstance(r, dict)]

    def search_recordings(self, title: str, artist: str = "", album: str = "",
                          limit: int = SEARCH_LIMIT) -> List["Recording"]:
        data = self.request("recording", [("query", recording_query(title, artist, album)), ("limit", str(limit))])
        return [parse_recording(r) for r in (data or {}).get("recordings") or [] if isinstance(r, dict)]

    def release(self, mbid: str) -> Optional["ReleaseDetail"]:
        data = self.request(f"release/{urllib.parse.quote(mbid, safe='')}", [("inc", RELEASE_INC)])
        return parse_detail(data) if data else None


def _api_message(body: bytes) -> str:
    try:
        msg = json.loads(body.decode("utf-8")).get("error")
    except (UnicodeDecodeError, ValueError, AttributeError):
        return ""
    return f": {msg}" if isinstance(msg, str) and msg.strip() else ""


# ------------------------------------------------------------------- data ---
def credit(ac) -> str:
    """An artist credit as MusicBrainz prints it ("A feat. B")."""
    out = []
    for c in ac or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or (c.get("artist") or {}).get("name") or ""
        out.append(f"{name}{c.get('joinphrase') or ''}")
    return clean_value("".join(out))


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _str(v) -> str:
    return clean_value(v) if isinstance(v, str) else ""


@dataclass
class Release:
    id: str
    title: str = ""
    artist: str = ""
    date: str = ""
    country: str = ""
    status: str = ""
    track_count: int = 0
    disc_count: int = 0
    formats: str = ""            # "CD", "2×CD", "CD + DVD"
    primary_type: str = ""       # Album, Single, EP, ...
    secondary_types: List[str] = field(default_factory=list)  # Compilation, Live, ...
    first_date: str = ""         # the release group's first release date (lookups only)
    score: int = 0               # search results only


def _formats(media) -> str:
    counts: List[Tuple[str, int]] = []
    for m in media or []:
        if not isinstance(m, dict):
            continue
        fmt = _str(m.get("format")) or "Medium"
        for i, (f, n) in enumerate(counts):
            if f == fmt:
                counts[i] = (f, n + 1)
                break
        else:
            counts.append((fmt, 1))
    return " + ".join(f if n == 1 else f"{n}×{f}" for f, n in counts)


def parse_release(d: dict) -> Release:
    rg = d.get("release-group") if isinstance(d.get("release-group"), dict) else {}
    media = [m for m in d.get("media") or [] if isinstance(m, dict)]
    tracks = _int(d.get("track-count")) or sum(_int(m.get("track-count")) for m in media)
    return Release(id=_str(d.get("id")), title=_str(d.get("title")), artist=credit(d.get("artist-credit")),
                   date=_str(d.get("date")), country=_str(d.get("country")), status=_str(d.get("status")),
                   track_count=tracks, disc_count=len(media), formats=_formats(media),
                   primary_type=_str(rg.get("primary-type")),
                   secondary_types=[_str(t) for t in rg.get("secondary-types") or [] if isinstance(t, str)],
                   first_date=_str(rg.get("first-release-date")), score=_int(d.get("score")))


@dataclass
class ReleaseTrack:
    disc: int                    # position of the medium, 1...
    disc_count: int
    number: int                  # position on the medium, 1...
    track_count: int             # tracks on that medium
    title: str = ""
    artist: str = ""
    length: float = 0.0          # seconds (0 = unknown)
    recording_id: str = ""
    composer: str = ""


@dataclass
class ReleaseDetail:
    release: Release
    tracks: List[ReleaseTrack] = field(default_factory=list)
    genre: str = ""

    @property
    def date(self) -> str:
        """The date written into the songs: the first release of the album, else this release's."""
        return self.release.first_date or self.release.date

    def track_at(self, disc: int, number: int) -> Optional[ReleaseTrack]:
        for t in self.tracks:
            if t.disc == disc and t.number == number:
                return t
        return None


def genre_name(name: str) -> str:
    """"cool jazz" -> "Cool Jazz" (MusicBrainz writes genres in lower case)."""
    return " ".join(w[:1].upper() + w[1:] for w in clean_value(name).split(" "))


def top_genre(*lists) -> str:
    """The genre with the most votes of the first list that has one (ties: the name first in order)."""
    for genres in lists:
        best: Optional[Tuple[int, str]] = None
        for g in genres or []:
            if not isinstance(g, dict) or not _str(g.get("name")):
                continue
            key = (-_int(g.get("count")), _str(g.get("name")))
            if best is None or key < best:
                best = key
        if best is not None:
            return genre_name(best[1])
    return ""


def composers(recording: dict) -> str:
    """The composers of the works a recording performs ("writer" when no composer is named)."""
    by_type: Dict[str, List[str]] = {"composer": [], "writer": []}
    for rel in recording.get("relations") or []:
        if not isinstance(rel, dict) or rel.get("target-type") != "work" or not isinstance(rel.get("work"), dict):
            continue
        for wr in rel["work"].get("relations") or []:
            if not isinstance(wr, dict) or wr.get("type") not in by_type:
                continue
            name = _str((wr.get("artist") or {}).get("name"))
            if name and name not in by_type[wr["type"]]:
                by_type[wr["type"]].append(name)
    return ", ".join(by_type["composer"] or by_type["writer"])


def parse_detail(d: dict) -> ReleaseDetail:
    rel = parse_release(d)
    media = [m for m in d.get("media") or [] if isinstance(m, dict)]
    tracks: List[ReleaseTrack] = []
    for mi, m in enumerate(media):
        disc = _int(m.get("position")) or mi + 1
        mtracks = [t for t in m.get("tracks") or [] if isinstance(t, dict)]
        count = _int(m.get("track-count")) or len(mtracks)
        for ti, t in enumerate(mtracks):
            rec = t.get("recording") if isinstance(t.get("recording"), dict) else {}
            length = _int(t.get("length")) or _int(rec.get("length"))
            tracks.append(ReleaseTrack(
                disc=disc, disc_count=len(media), number=_int(t.get("position")) or ti + 1, track_count=count,
                title=_str(t.get("title")) or _str(rec.get("title")),
                artist=credit(t.get("artist-credit")) or credit(rec.get("artist-credit")) or rel.artist,
                length=length / 1000.0, recording_id=_str(rec.get("id")), composer=composers(rec)))
    rg = d.get("release-group") if isinstance(d.get("release-group"), dict) else {}
    return ReleaseDetail(rel, tracks, top_genre(rg.get("genres"), d.get("genres")))


@dataclass
class RecordingRelease:
    release: Release
    disc: int
    number: int


@dataclass
class Recording:
    id: str
    title: str = ""
    artist: str = ""
    length: float = 0.0
    score: int = 0
    releases: List[RecordingRelease] = field(default_factory=list)


def parse_recording(d: dict) -> Recording:
    out = Recording(id=_str(d.get("id")), title=_str(d.get("title")), artist=credit(d.get("artist-credit")),
                    length=_int(d.get("length")) / 1000.0, score=_int(d.get("score")))
    for r in d.get("releases") or []:
        if not isinstance(r, dict):
            continue
        media = [m for m in r.get("media") or [] if isinstance(m, dict)]
        m = media[0] if media else {}
        out.releases.append(RecordingRelease(parse_release(r), _int(m.get("position")) or 1,
                                             _int(m.get("track-offset")) + 1))
    return out


# ------------------------------------------------------------------- songs ---
@dataclass
class Song:
    """One of the user's songs: its path, its tags (tagedit.FIELDS) and its length."""
    path: str
    tags: Dict[str, str] = field(default_factory=dict)
    duration: float = 0.0

    def tag(self, name: str) -> str:
        return clean_value(self.tags.get(name, ""))

    @property
    def number(self) -> int:
        return _int(split_number(self.tag("track"))[0])

    @property
    def disc(self) -> int:
        return _int(split_number(self.tag("disc"))[0])


def same_title(a: str, b: str) -> bool:
    na = normalize_name(a)
    return bool(na) and na == normalize_name(b)


def length_ok(a: float, b: float) -> bool:
    """True when either length is unknown or they differ by at most DURATION_TOLERANCE_S."""
    return not a or not b or abs(a - b) <= DURATION_TOLERANCE_S


def match_tracks(songs: Sequence[Song], detail: ReleaseDetail, by_order: bool = True) -> List[Optional[ReleaseTrack]]:
    """The release track of each song (None = not found): first by disc and track
    number (when the title or the length agrees), then by title, then, when as
    many songs as tracks are left, in the order of disc, track number and path."""
    out: List[Optional[ReleaseTrack]] = [None] * len(songs)
    used: set = set()

    def key(t: ReleaseTrack) -> Tuple[int, int]:
        return (t.disc, t.number)

    for i, s in enumerate(songs):
        if not s.number:
            continue
        t = detail.track_at(s.disc or 1, s.number)
        if t is None or key(t) in used:
            continue
        if s.tag("title") and not same_title(s.tag("title"), t.title) and not (
                s.duration and t.length and abs(s.duration - t.length) <= DURATION_TOLERANCE_S):
            continue
        out[i] = t
        used.add(key(t))
    for i, s in enumerate(songs):
        if out[i] is not None or not s.tag("title"):
            continue
        cands = [t for t in detail.tracks if key(t) not in used and same_title(s.tag("title"), t.title)
                 and length_ok(s.duration, t.length)]
        if not cands:
            continue
        t = min(cands, key=lambda t: (abs(s.duration - t.length) if s.duration and t.length else 0.0, t.disc, t.number))
        out[i] = t
        used.add(key(t))
    left_songs = [i for i in range(len(songs)) if out[i] is None]
    left_tracks = [t for t in detail.tracks if key(t) not in used]
    if by_order and left_songs and len(left_songs) == len(left_tracks):
        order = sorted(left_songs, key=lambda i: (songs[i].disc or 1, songs[i].number or 0, songs[i].path))
        for i, t in zip(order, left_tracks):
            out[i] = t
    return out


def proposed(detail: ReleaseDetail, t: ReleaseTrack) -> Dict[str, str]:
    """What MusicBrainz says each field of this track should hold."""
    rel = detail.release
    return {"title": t.title, "artist": t.artist, "album": rel.title, "album_artist": rel.artist,
            "genre": detail.genre, "date": detail.date,
            "track": f"{t.number}/{t.track_count}" if t.track_count else str(t.number),
            "disc": f"{t.disc}/{t.disc_count}" if t.disc_count > 1 else "",
            "composer": t.composer}


def _same(f: str, a: str, b: str) -> bool:
    if f in ("track", "disc"):
        return split_number(a) == split_number(b) and bool(split_number(a)[0])
    return a == b


def changes_for(song: Song, detail: ReleaseDetail, t: Optional[ReleaseTrack],
                fields: Iterable[str] = FIELDS, only_empty: bool = False) -> Dict[str, str]:
    """The tags to write into `song` (field -> new value): MusicBrainz's values
    that differ from the song's, for the chosen fields; with `only_empty`, only
    where the song's tag is empty.  An empty MusicBrainz value never clears a tag."""
    if t is None:
        return {}
    wanted = set(fields)
    new = proposed(detail, t)
    out: Dict[str, str] = {}
    for f in FIELDS:
        if f not in wanted:
            continue
        v = clean_value(new.get(f, ""))
        if not v:
            continue
        cur = song.tag(f)
        if only_empty and cur:
            continue
        if _same(f, v, cur):
            continue
        out[f] = v
    return out


def album_of(songs: Sequence[Song]) -> Tuple[str, str]:
    """(album, artist) to search for: the album tag the most songs share, and its album
    artist (else the artist the most songs share)."""
    def common(name: str) -> str:
        counts: Dict[str, int] = {}
        order: List[str] = []
        for s in songs:
            v = s.tag(name)
            if v:
                if v not in counts:
                    order.append(v)
                counts[v] = counts.get(v, 0) + 1
        return max(order, key=lambda v: (counts[v], -order.index(v))) if order else ""

    return common("album"), common("album_artist") or common("artist")


def rank_releases(releases: Sequence[Release], album: str, artist: str, songs: int) -> List[Release]:
    """The releases that are this album (same title and artist, room for every song),
    best first: as many tracks as songs, official, the search score, the earliest date."""
    out = [r for r in releases if same_title(r.title, album) and (not artist or same_artist(r.artist, artist))
           and (not r.track_count or r.track_count >= songs)]
    return sorted(out, key=lambda r: (0 if r.track_count == songs else 1, 0 if r.status == "Official" else 1,
                                      -r.score, r.date or "9999", r.id))


def release_preference(r: Release) -> tuple:
    """How suitable a release is for a single song: official, not a compilation or live album, an
    album rather than an EP or a single, early."""
    return (0 if r.status == "Official" else 1, 1 if r.secondary_types else 0, TYPE_RANK.get(r.primary_type, 3),
            r.date or "9999", r.id)


def needs_lookup(tags: Dict[str, str]) -> bool:
    """Whether the automatic lookup should ask about a song: it has a title and an
    artist to search with, and one of MISSING_FIELDS is empty."""
    has = lambda f: bool(clean_value(tags.get(f, "")))  # noqa: E731
    return has("title") and (has("artist") or has("album_artist")) and not all(has(f) for f in MISSING_FIELDS)


def find_album(client: MusicBrainzClient, songs: Sequence[Song]) -> Tuple[Optional[ReleaseDetail], List[Optional[ReleaseTrack]]]:
    """The release that is certainly these songs' album (automatic mode), and the track of each song."""
    album, artist = album_of(songs)
    if not album:
        return None, [None] * len(songs)
    found = rank_releases(client.search_releases(album, artist), album, artist, len(songs))
    for r in found[:AUTO_CANDIDATES]:
        detail = client.release(r.id)
        if detail is None:
            continue
        m = match_tracks(songs, detail, by_order=False)
        if all(t is not None for t in m):
            return detail, m
    return None, [None] * len(songs)


def find_song(client: MusicBrainzClient, song: Song) -> Tuple[Optional[ReleaseDetail], Optional[ReleaseTrack]]:
    """The recording that is certainly this song (same title and artist, length within 3 s)
    on its most suitable release (automatic mode, for a song without an album tag)."""
    title, artist = song.tag("title"), song.tag("artist") or song.tag("album_artist")
    if not title or not artist:
        return None, None
    best: Optional[Tuple[tuple, Recording, RecordingRelease]] = None
    for rec in client.search_recordings(title, artist):
        if not same_title(rec.title, title) or not same_artist(rec.artist, artist):
            continue
        if not (song.duration and rec.length and abs(song.duration - rec.length) <= DURATION_TOLERANCE_S):
            continue  # without both lengths a namesake cannot be ruled out
        for rr in rec.releases:
            key = release_preference(rr.release)
            if best is None or key < best[0]:
                best = (key, rec, rr)
    if best is None:
        return None, None
    _, rec, rr = best
    detail = client.release(rr.release.id)
    if detail is None:
        return None, None
    for t in detail.tracks:
        if t.recording_id == rec.id:
            return detail, t
    t = detail.track_at(rr.disc, rr.number)
    return (detail, t) if t is not None and same_title(t.title, title) else (None, None)


def search_candidates(client: MusicBrainzClient, songs: Sequence[Song], query_title: str, query_artist: str,
                      album_mode: bool) -> List[Release]:
    """The releases the window lists for a search: for an album (several songs, or the
    user searched by album) a release search; for one song a recording search,
    whose releases are listed (each once, in the order found)."""
    if album_mode:
        found = client.search_releases(query_title, query_artist)
        n = len(songs)
        return sorted(found, key=lambda r: (0 if same_title(r.title, query_title) else 1,
                                            0 if r.track_count == n else 1 if r.track_count > n else 2,
                                            -r.score, 0 if r.status == "Official" else 1, r.date or "9999", r.id))
    out: List[Release] = []
    seen: set = set()
    recs = client.search_recordings(query_title, query_artist)
    dur = songs[0].duration if songs else 0.0
    recs = sorted(recs, key=lambda r: (0 if same_title(r.title, query_title) else 1,
                                       0 if length_ok(dur, r.length) else 1, -r.score))
    for rec in recs:
        for rr in sorted(rec.releases, key=lambda x: release_preference(x.release)):
            if rr.release.id not in seen:
                seen.add(rr.release.id)
                out.append(rr.release)
    return out


# --------------------------------------------------------- the lookup log ---
_TABLE = ("CREATE TABLE IF NOT EXISTS musicbrainz (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, "
          "status TEXT, release TEXT, time REAL)")


class LookupLog:
    """Which songs were looked up (table `musicbrainz` of library.sqlite), so the automatic
    lookup asks about each song once until the file changes.  One per thread."""

    def __init__(self, path: Optional[str] = None):
        from .musiclib import default_path

        self.path = path or default_path()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.execute(_TABLE)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def done(self, path: str, size: int, mtime_ns: int) -> bool:
        row = self.db.execute("SELECT size, mtime_ns FROM musicbrainz WHERE path = ?", (path,)).fetchone()
        return row is not None and row[0] == size and row[1] == mtime_ns

    def status(self, path: str) -> str:
        row = self.db.execute("SELECT status FROM musicbrainz WHERE path = ?", (path,)).fetchone()
        return row[0] if row else ""

    def mark(self, path: str, status: str, release: str = "") -> None:
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime_ns
        except OSError:
            size, mtime = -1, -1
        self.db.execute("INSERT OR REPLACE INTO musicbrainz VALUES (?, ?, ?, ?, ?, ?)",
                        (path, size, mtime, status, release, time.time()))
        self.db.commit()

    def forget(self) -> int:
        n = self.db.execute("DELETE FROM musicbrainz").rowcount
        self.db.commit()
        return n


# ------------------------------------------------------- automatic lookup ---
@dataclass
class AutoOutcome:
    path: str
    status: str                       # FILLED, COMPLETE, NO_MATCH, NO_TAGS, FAILED
    changes: Dict[str, str] = field(default_factory=dict)
    release: str = ""
    message: str = ""


def read_song(path: str, duration: float = 0.0) -> Song:
    """A song's tags as the file holds them now (ffprobe)."""
    from .probe import probe

    info = probe(path)
    tags = info.tags or {}
    return Song(path, {f: clean_value(tags.get(f, "")) for f in FIELDS}, float(info.duration or duration or 0.0))


def groups(songs: Sequence[Song]) -> List[List[Song]]:
    """The songs as the albums they belong to (folder and album tag; a song without an
    album tag is a group of its own), in the order first seen."""
    out: Dict[tuple, List[Song]] = {}
    for s in songs:
        album = normalize_name(s.tag("album"))
        key = (os.path.dirname(s.path), album) if album else ("", s.path)
        out.setdefault(key, []).append(s)
    return list(out.values())


def auto_group(client: MusicBrainzClient, songs: Sequence[Song]) -> List[AutoOutcome]:
    """Look one album (or one song without an album tag) up and plan the tags to fill in."""
    if not songs[0].tag("album"):
        detail, t = find_song(client, songs[0])
        matched: List[Optional[ReleaseTrack]] = [t]
    else:
        detail, matched = find_album(client, songs)
    out = []
    for s, t in zip(songs, matched):
        if detail is None or t is None:
            out.append(AutoOutcome(s.path, NO_MATCH))
            continue
        ch = changes_for(s, detail, t, only_empty=True)
        out.append(AutoOutcome(s.path, FILLED if ch else COMPLETE, ch, detail.release.id))
    return out


def save_changes(plans: Sequence[Tuple[str, Dict[str, str]]], cfg=None,
                 progress: Optional[Callable[[int, int, str], None]] = None):
    """Write each song's own changes with tagedit.save_tags(); one TagOutcome per song."""
    from .tagedit import TagOutcome, save_tags

    out = []
    for i, (path, changes) in enumerate(plans):
        if progress is not None:
            progress(i, len(plans), path)
        if not changes:
            out.append(TagOutcome(path, True))
            continue
        out.extend(save_tags([path], changes, cfg))
    if progress is not None:
        progress(len(plans), len(plans), "")
    return out


def run_auto(client: MusicBrainzClient, paths: Sequence[str], cfg=None, log: Optional[LookupLog] = None,
             durations: Optional[Dict[str, float]] = None,
             progress: Optional[Callable[[int, int], None]] = None) -> List[AutoOutcome]:
    """The automatic lookup of these songs: those not looked up since they last changed and
    missing some information are grouped into albums, looked up and given the missing tags.
    Raises MusicBrainzStopped when stopped; a failed lookup marks the songs FAILED (asked again
    next time)."""
    own_log = log is None
    log = log or LookupLog()
    outcomes: List[AutoOutcome] = []
    try:
        todo: List[Song] = []
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            if log.done(p, st.st_size, st.st_mtime_ns):
                continue
            try:
                song = read_song(p, (durations or {}).get(p, 0.0))
            except Exception:  # noqa: BLE001 - an unreadable file is not looked up
                continue
            if not needs_lookup(song.tags):
                searchable = song.tag("title") and (song.tag("artist") or song.tag("album_artist"))
                log.mark(p, COMPLETE if searchable else NO_TAGS)
                continue
            todo.append(song)
        albums = groups(todo)
        for gi, group in enumerate(albums):
            if progress is not None:
                progress(gi, len(albums))
            try:
                planned = auto_group(client, group)
            except MusicBrainzError as exc:
                for s in group:
                    outcomes.append(AutoOutcome(s.path, FAILED, message=str(exc)))
                continue
            for o in planned:
                if o.status == FILLED:
                    res = save_changes([(o.path, o.changes)], cfg)[0]
                    if not res.ok:
                        o.status, o.message = FAILED, res.message
                outcomes.append(o)
                if o.status != FAILED:
                    log.mark(o.path, o.status, o.release)
        if progress is not None:
            progress(len(albums), len(albums))
    finally:
        if own_log:
            log.close()
    return outcomes


def auto_summary(outcomes: Sequence[AutoOutcome]) -> str:
    """One sentence for the status line after an automatic lookup."""
    filled = sum(1 for o in outcomes if o.status == FILLED)
    failed = [o for o in outcomes if o.status == FAILED]
    if not outcomes:
        return "Every song already has its information."
    text = (f"MusicBrainz filled in the information of {filled:,} song{'s' if filled != 1 else ''}."
            if filled else "MusicBrainz had nothing to add.")
    if failed:
        text += f" {len(failed):,} could not be looked up: {failed[0].message}"
    return text
