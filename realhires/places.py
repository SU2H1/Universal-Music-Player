"""Where a song was played (2.4, docs/player-spec.md "3. Where a song was played").

With the setting ``play_location`` on (it is off until the user turns it on in
Settings → Playback), the player asks a free IP geolocation service for this
computer's approximate place once when it starts (or when the setting is
turned on) and again at most every 2 hours while music plays, and the play
history (``musiclib.Library.add_play()``) stores the place with each play.
Your Stats then names the place of each play of a day and lists the places of
a period (``period_places()``, ``place_songs()``).

The service is FreeIPAPI's free API (``DEFAULT_SERVER``): free for commercial
and non-commercial use without an account or a key, over HTTPS, 60 requests a
minute (10 in 10 s) per address; its terms are at ``TERMS_URL``.  The address
is the setting ``location_server``; another service that answers the same
kind of JSON (FreeIPAPI's or ipwho.is's field names) can be used instead.

What goes out: one HTTPS GET of that address with the app's name in the
User-Agent, nothing else.  The service sees the computer's IP address (that is
how it finds the place); no song, path or tag is ever sent.  The places are
kept only in ``library.sqlite`` on this computer, and Settings → Playback →
Forget play locations… clears them (the plays stay).

``GeoClient`` is as polite as the lyrics and MusicBrainz clients: one request
at a time, at least ``MIN_INTERVAL_S`` apart, a 429/503 answer's Retry-After
is kept (no request before it, and the lookup gives up at once instead of
retrying), server errors and network errors are tried ``MAX_ATTEMPTS`` times
in all with a short back-off, and a failure only leaves the place empty.
``PlaceCache`` decides when a lookup is due.  The Rust version
(``rust/crates/core/src/places.rs``) parses, groups and schedules alike
(``rust/crates/core/tests/python_places.json``).
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from . import PROJECT_URL, __version__

DEFAULT_SERVER = "https://free.freeipapi.com/api/v1/json/"
SERVICE_NAME = "FreeIPAPI"
TERMS_URL = "https://freeipapi.com/terms"
MIN_INTERVAL_S = 2.0        # between request starts (FreeIPAPI allows 10 requests in 10 s)
TIMEOUT_S = 15.0
MAX_ATTEMPTS = 3            # per lookup, for server errors (5xx) and network errors
BACKOFF_S = 2.0             # then 4 s
DEFAULT_RETRY_AFTER_S = 600.0   # a 429/503 answer without Retry-After: no request for 10 minutes
MAX_RETRY_AFTER_S = 6 * 3600.0
REFRESH_S = 2 * 3600.0      # a new lookup at most every 2 hours, and only while music plays
MAX_NAME = 100              # characters kept of a city, region or country name
INTERVAL_ENV = "REALHIRES_LOCATION_INTERVAL"  # tests only; never below MIN_INTERVAL_S for freeipapi.com

# the field names of the answer, first match wins: FreeIPAPI's, then ipwho.is's (and ipapi.co's)
CITY_KEYS = ("cityName", "city")
REGION_KEYS = ("regionName", "region")
COUNTRY_KEYS = ("countryName", "country_name", "country")
CODE_KEYS = ("countryCode", "country_code")
LATITUDE_KEYS = ("latitude", "lat")
LONGITUDE_KEYS = ("longitude", "lon", "lng")


class PlaceError(Exception):
    """A lookup failed (the message is a full sentence)."""


class PlaceStopped(Exception):
    """The app is closing."""


def user_agent() -> str:
    """The app's name, version and home page, as for MusicBrainz."""
    return f"UniversalMusicPlayer/{__version__} ( {PROJECT_URL} )"


def normalize_server(server: str) -> str:
    s = (server or "").strip() or DEFAULT_SERVER
    return s if "://" in s else "https://" + s


def host_of(server: str) -> str:
    s = normalize_server(server)
    return urllib.parse.urlsplit(s).netloc or s


def is_official(server: str) -> bool:
    host = (urllib.parse.urlsplit(normalize_server(server)).hostname or "").rstrip(".").lower()
    return host == "freeipapi.com" or host.endswith(".freeipapi.com")


def default_interval(server: str) -> float:
    try:
        value = float(os.environ.get(INTERVAL_ENV, ""))
    except ValueError:
        value = MIN_INTERVAL_S
    if value != value:  # NaN
        value = MIN_INTERVAL_S
    value = max(value, 0.0)
    return max(value, MIN_INTERVAL_S) if is_official(server) else value


# -------------------------------------------------------------------- places ---
@dataclass(frozen=True)
class Place:
    """An approximate place: names as the service gives them, and the latitude and
    longitude rounded to 2 decimals (about 1 km), None when unknown."""

    city: str = ""
    region: str = ""
    country: str = ""
    country_code: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    @property
    def key(self) -> str:
        return place_key(self)

    def to_json(self) -> dict:
        return {"city": self.city, "region": self.region, "country": self.country,
                "country_code": self.country_code, "latitude": self.latitude, "longitude": self.longitude}


def clean_name(value) -> str:
    """A name from the answer: text only, control characters and runs of white space made
    one space, at most MAX_NAME characters."""
    if not isinstance(value, str):
        return ""
    text = "".join(" " if c.isspace() or ord(c) < 32 or ord(c) == 127 else c for c in value)
    return " ".join(text.split())[:MAX_NAME].strip()


def clean_code(value) -> str:
    """A country code: two ASCII letters, upper case ("" otherwise)."""
    code = clean_name(value)
    return code.upper() if len(code) == 2 and code.isascii() and code.isalpha() else ""


def coordinate(value, limit: float) -> Optional[float]:
    """A latitude (limit 90) or longitude (limit 180) from a JSON number, rounded to
    2 decimals; None for anything else or out of range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    x = float(value)
    if x != x or abs(x) > limit:  # NaN, infinities, out of range
        return None
    r = round(x, 2)
    return 0.0 if r == 0 else r  # never -0.0


def _first(data: dict, keys) -> object:
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    return None


def make_place(city="", region="", country="", country_code="", latitude=None, longitude=None) -> Optional[Place]:
    """A Place from stored or parsed values; None without a city, region or country."""
    p = Place(clean_name(city), clean_name(region), clean_name(country), clean_code(country_code),
              coordinate(latitude, 90.0), coordinate(longitude, 180.0))
    return p if (p.city or p.region or p.country) else None


def parse_place(data) -> Place:
    """The place in a service's JSON answer.  Raises PlaceError when it holds none."""
    if not isinstance(data, dict):
        raise PlaceError("The location service sent no place.")
    if data.get("success") is False:  # ipwho.is answers errors with success: false and a message
        msg = clean_name(data.get("message"))
        raise PlaceError(f"The location service could not find this computer's place: {msg}." if msg
                         else "The location service could not find this computer's place.")
    place = make_place(_first(data, CITY_KEYS), _first(data, REGION_KEYS), _first(data, COUNTRY_KEYS),
                       _first(data, CODE_KEYS), _first(data, LATITUDE_KEYS), _first(data, LONGITUDE_KEYS))
    if place is None:
        raise PlaceError("The location service sent no place.")
    return place


def place_text(place: Optional[Place]) -> str:
    """'Tokyo, Japan' for display: the city, region and country, each once (a region
    or country named like a part before it is left out)."""
    if place is None:
        return ""
    out: List[str] = []
    seen = set()
    for part in (place.city, place.region, place.country):
        k = part.casefold()
        if part and k not in seen:
            seen.add(k)
            out.append(part)
    return ", ".join(out)


def place_key(place: Optional[Place]) -> str:
    """What makes two places the same place (the names, in any case); "" for none."""
    if place is None:
        return ""
    return "\x00".join(p.casefold() for p in (place.city, place.region, place.country))


# -------------------------------------------------------------------- client ---
Transport = Callable[[str, Dict[str, str], float], tuple]


class GeoClient:
    """One lookup at a time (one client for the app, used from one thread at a time)."""

    def __init__(self, server: str = DEFAULT_SERVER, *, stop: Optional[threading.Event] = None,
                 transport: Optional[Transport] = None, min_interval: Optional[float] = None,
                 timeout: float = TIMEOUT_S, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        from .lyrics import _urlopen

        self.server = normalize_server(server)
        self.stop = stop
        self.transport = transport or _urlopen
        self.min_interval = default_interval(self.server) if min_interval is None else min_interval
        self.timeout = timeout
        self.clock = clock
        self._sleep = sleep
        self._last_start: Optional[float] = None
        self.not_before = 0.0  # no request before this time (Retry-After)
        self.requests = 0

    @property
    def host(self) -> str:
        return host_of(self.server)

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.stop is not None:
            if self.stop.wait(seconds):
                raise PlaceStopped()
        else:
            self._sleep(seconds)

    def _throttle(self) -> None:
        if self.stop is not None and self.stop.is_set():
            raise PlaceStopped()
        due = self.not_before
        if self._last_start is not None:
            due = max(due, self._last_start + self.min_interval)
        self._wait(due - self.clock())

    def lookup(self) -> Place:
        """This computer's approximate place.  Raises PlaceError / PlaceStopped."""
        from .lyrics import _network_reason

        headers = {"User-Agent": user_agent(), "Accept": "application/json"}
        if self.stop is not None and self.stop.is_set():
            raise PlaceStopped()
        if self.clock() < self.not_before:
            raise PlaceError(f"{self.host} asked to wait before the next lookup.")
        backoff, problem = BACKOFF_S, ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            self._last_start = self.clock()
            self.requests += 1
            try:
                status, hdrs, body = self.transport(self.server, headers, self.timeout)
            except ValueError as exc:  # an address that is not http(s)://
                raise PlaceError(f"The location server address {self.server!r} is not valid: {exc}.") from exc
            except (OSError, http.client.HTTPException) as exc:
                if isinstance(getattr(exc, "reason", exc), ssl.SSLCertVerificationError):
                    raise PlaceError(f"Could not connect to {self.host} securely: {_network_reason(exc)}.") from exc
                problem = f"could not reach {self.host}: {_network_reason(exc)}"
            else:
                if status == 200:
                    try:
                        data = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise PlaceError(f"{self.host} sent an answer that could not be read.") from exc
                    return parse_place(data)
                if status in (429, 503):
                    self.not_before = self.clock() + retry_after(hdrs)
                    raise PlaceError(f"{self.host} asked to wait before the next lookup.")
                if status < 500:
                    raise PlaceError(f"{self.host} refused the request (HTTP {status}).")
                problem = f"server error (HTTP {status})"
            if attempt < MAX_ATTEMPTS:
                self._wait(backoff)
                backoff *= 2
        raise PlaceError(f"No answer from {self.host} after {MAX_ATTEMPTS} attempts ({problem}).")


def retry_after(headers: Dict[str, str]) -> float:
    """The seconds a 429/503 answer asks to wait (Retry-After in seconds; else
    DEFAULT_RETRY_AFTER_S), at most MAX_RETRY_AFTER_S."""
    try:
        value = float((headers or {}).get("retry-after", ""))
    except ValueError:
        return DEFAULT_RETRY_AFTER_S
    if value != value:
        return DEFAULT_RETRY_AFTER_S
    return min(max(value, 0.0), MAX_RETRY_AFTER_S)


# ---------------------------------------------------------------- when to ask ---
class PlaceCache:
    """The place the plays are stored with, and when to look it up again: at once the
    first time (when the app starts with the setting on, or when it is turned on), then
    only while music plays and REFRESH_S after the last lookup began, and never before
    the time a 429/503 answer asked for.  A failed lookup leaves the place empty until a
    later lookup finds it.  Times are the monotonic clock's; `looked_up` is wall time."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.place: Optional[Place] = None
        self.last: Optional[float] = None   # when the last lookup began
        self.busy = False
        self.error = ""
        self.looked_up: Optional[float] = None  # wall time of the last lookup that found the place

    def due(self, now: float, playing: bool, not_before: float = 0.0) -> bool:
        if self.busy or now < not_before:
            return False
        if self.last is None:
            return True
        return playing and now - self.last >= REFRESH_S

    def begin(self, now: float) -> None:
        self.busy = True
        self.last = now

    def finish(self, place: Optional[Place], error: str = "", wall: Optional[float] = None) -> None:
        self.busy = False
        self.place = place
        self.error = "" if place is not None else (error or "The location service sent no place.")
        if place is not None:
            self.looked_up = time.time() if wall is None else wall


def lookup_now(client: GeoClient) -> tuple:
    """(place, error text) of one lookup; PlaceStopped passes through."""
    try:
        return client.lookup(), ""
    except PlaceError as exc:
        return None, str(exc)


# --------------------------------------------------------------- statistics ---
def period_places(plays: Iterable) -> List[dict]:
    """The places of these plays (`stats.Play` with their `place`), most played first:
    {"key", "name" (place_text), "city", "region", "country", "country_code", "latitude",
    "longitude" (of the first play there), "plays" (counted, stats.counted()), "logged",
    "seconds", "songs" (different songs), "first", "last" (start times)}.  Plays without
    a place are left out."""
    from .stats import TOP, counted

    groups: Dict[str, dict] = {}
    songs: Dict[str, set] = {}
    for p in plays:
        place = getattr(p, "place", None)
        key = place_key(place)
        if not key:
            continue
        d = groups.get(key)
        if d is None:
            d = groups[key] = {"key": key, "name": place_text(place), "city": place.city, "region": place.region,
                               "country": place.country, "country_code": place.country_code,
                               "latitude": place.latitude, "longitude": place.longitude, "plays": 0, "logged": 0,
                               "seconds": 0.0, "songs": 0, "first": p.started, "last": p.started}
            songs[key] = set()
        d["plays"] += 1 if counted(p) else 0
        d["logged"] += 1
        d["seconds"] += p.seconds
        d["first"] = min(d["first"], p.started)
        d["last"] = max(d["last"], p.started)
        songs[key].add(p.path)
    for key, d in groups.items():
        d["songs"] = len(songs[key])
    return sorted(groups.values(), key=lambda d: (-d["plays"], -d["seconds"], d["name"].casefold(), d["key"]))[:TOP]


def place_songs(plays: Iterable, key: str) -> List[dict]:
    """The songs played at the place `key`, most played first: {"path", "title",
    "artist", "plays" (counted), "logged", "seconds", "last"}."""
    from .stats import counted

    rows: Dict[str, dict] = {}
    for p in plays:
        if place_key(getattr(p, "place", None)) != key or not key:
            continue
        r = rows.get(p.path)
        if r is None:
            r = rows[p.path] = {"path": p.path,
                                "title": p.title.strip() or os.path.splitext(os.path.basename(p.path))[0],
                                "artist": p.artist.strip() or p.album_artist.strip() or "Unknown Artist",
                                "plays": 0, "logged": 0, "seconds": 0.0, "last": p.started}
        r["plays"] += 1 if counted(p) else 0
        r["logged"] += 1
        r["seconds"] += p.seconds
        r["last"] = max(r["last"], p.started)
    return sorted(rows.values(), key=lambda r: (-r["plays"], -r["seconds"], r["title"].casefold(), r["path"]))


def place_of_row(row: dict) -> Optional[Place]:
    """The Place of a row of period_places()."""
    return make_place(row.get("city", ""), row.get("region", ""), row.get("country", ""),
                      row.get("country_code", ""), row.get("latitude"), row.get("longitude"))
