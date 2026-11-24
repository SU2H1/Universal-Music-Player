"""A small invented MusicBrainz catalogue and an HTTP server that answers like /ws/2/ (2.1).

Tests must never contact musicbrainz.org.  `FakeMusicBrainz` serves
`/ws/2/release?query=…`, `/ws/2/recording?query=…` and `/ws/2/release/<id>?inc=…`
from `CATALOG` (JSON shaped like MusicBrainz's answers) and records every
request with its User-Agent.  `answer(url)` gives the same answers without a
server, for `rust/tools/gen_mb_vectors.py`.  Every name here is invented.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple


def _ac(name: str, join: str = "") -> dict:
    return {"name": name, "joinphrase": join, "artist": {"id": "a-" + name.lower().replace(" ", "-"), "name": name}}


def _work(title: str, composers: List[str], kind: str = "composer") -> dict:
    return {"type": "performance", "target-type": "work",
            "work": {"id": "w-" + title.lower().replace(" ", "-"), "title": title,
                     "relations": [{"type": kind, "target-type": "artist",
                                    "artist": {"id": "a-" + c.lower(), "name": c}} for c in composers]}}


def _track(n: int, title: str, ms: int, rec: str, artist=None, composers=None, kind="composer") -> dict:
    recording = {"id": rec, "title": title, "length": ms,
                 "artist-credit": artist or [_ac("The Example Band")],
                 "relations": [_work(title, composers, kind)] if composers else []}
    return {"id": "t-" + rec, "position": n, "number": str(n), "title": title, "length": ms,
            "artist-credit": artist or [_ac("The Example Band")], "recording": recording}


NIGHT_TRACKS = [
    ("Neon Streets", 201000, "r-neon", ["Kai Example"]),
    ("Midnight Signal", 245500, "r-signal", ["Kai Example", "Mio Example"]),
    ("Harbour Lights", 187000, "r-harbour", None),
    ("Last Train Home", 263000, "r-train", ["Kai Example"]),
]

RELEASES: Dict[str, dict] = {
    "rel-night-2001": {
        "id": "rel-night-2001", "title": "Night Drive", "status": "Official", "date": "2001-03-07", "country": "JP",
        "artist-credit": [_ac("The Example Band")],
        "release-group": {"id": "rg-night", "primary-type": "Album", "secondary-types": [],
                          "first-release-date": "1999-05-28",
                          "genres": [{"name": "synth-pop", "count": 3}, {"name": "electronic", "count": 5}]},
        "genres": [],
        "media": [{"position": 1, "format": "CD", "track-count": 4,
                   "tracks": [_track(i + 1, t, ms, r, composers=c) for i, (t, ms, r, c) in enumerate(NIGHT_TRACKS)]}],
    },
    "rel-night-deluxe": {
        "id": "rel-night-deluxe", "title": "Night Drive", "status": "Official", "date": "2015-10-01", "country": "XW",
        "artist-credit": [_ac("The Example Band")],
        "release-group": {"id": "rg-night", "primary-type": "Album", "secondary-types": [],
                          "first-release-date": "1999-05-28",
                          "genres": [{"name": "synth-pop", "count": 3}, {"name": "electronic", "count": 5}]},
        "genres": [],
        "media": [{"position": 1, "format": "CD", "track-count": 4,
                   "tracks": [_track(i + 1, t, ms, r, composers=c) for i, (t, ms, r, c) in enumerate(NIGHT_TRACKS)]},
                  {"position": 2, "format": "CD", "track-count": 2,
                   "tracks": [_track(1, "Neon Streets (Demo)", 199000, "r-neon-demo"),
                              _track(2, "Signal Remix", 301000, "r-signal-remix")]}],
    },
    "rel-night-boot": {
        "id": "rel-night-boot", "title": "Night Drive", "status": "Bootleg", "date": "2003", "country": "",
        "artist-credit": [_ac("The Example Band")],
        "release-group": {"id": "rg-night-boot", "primary-type": "Album", "secondary-types": ["Live"],
                          "first-release-date": "2003", "genres": []},
        "genres": [],
        "media": [{"position": 1, "format": "Cassette", "track-count": 2,
                   "tracks": [_track(1, "Neon Streets", 230000, "r-neon-live"),
                              _track(2, "Harbour Lights", 200000, "r-harbour-live")]}],
    },
    "rel-road-single": {
        "id": "rel-road-single", "title": "Lonely Road", "status": "Official", "date": "2010-01-20", "country": "GB",
        "artist-credit": [_ac("Solo Artist")],
        "release-group": {"id": "rg-road-single", "primary-type": "Single", "secondary-types": [],
                          "first-release-date": "2010-01-20", "genres": [{"name": "folk", "count": 2}]},
        "genres": [],
        "media": [{"position": 1, "format": "Digital Media", "track-count": 2,
                   "tracks": [_track(1, "Lonely Road", 222000, "r-road", [_ac("Solo Artist")], ["Ann Writer"], "writer"),
                              _track(2, "Lonely Road (Acoustic)", 230000, "r-road-ac", [_ac("Solo Artist")])]}],
    },
    "rel-road-album": {
        "id": "rel-road-album", "title": "Open Country", "status": "Official", "date": "2010-06-01", "country": "GB",
        "artist-credit": [_ac("Solo Artist")],
        "release-group": {"id": "rg-open", "primary-type": "Album", "secondary-types": [],
                          "first-release-date": "2010-06-01",
                          "genres": [{"name": "folk", "count": 4}, {"name": "country", "count": 4}]},
        "genres": [],
        "media": [{"position": 1, "format": "CD", "track-count": 3,
                   "tracks": [_track(1, "Open Country", 180000, "r-open", [_ac("Solo Artist")]),
                              _track(2, "Lonely Road", 222000, "r-road", [_ac("Solo Artist")], ["Ann Writer"], "writer"),
                              _track(3, "River \"Song\"", 150000, "r-river", [_ac("Solo Artist", " feat. "), _ac("Guest Singer")])]}],
    },
    "rel-road-best": {
        "id": "rel-road-best", "title": "Best Roads", "status": "Official", "date": "2005", "country": "US",
        "artist-credit": [_ac("Various Artists")],
        "release-group": {"id": "rg-best", "primary-type": "Album", "secondary-types": ["Compilation"],
                          "first-release-date": "2005", "genres": []},
        "genres": [],
        "media": [{"position": 1, "format": "CD", "track-count": 10,
                   "tracks": [_track(7, "Lonely Road", 222000, "r-road", [_ac("Solo Artist")])]}],
    },
}


def _summary(rel: dict, score: int) -> dict:
    """A release as a search result shows it (no tracks)."""
    out = {k: v for k, v in rel.items() if k not in ("media", "genres")}
    rg = {k: v for k, v in rel["release-group"].items() if k not in ("genres", "first-release-date")}
    out["release-group"] = rg
    out["score"] = score
    out["track-count"] = sum(m["track-count"] for m in rel["media"])
    out["media"] = [{"position": m["position"], "format": m["format"], "track-count": m["track-count"]}
                    for m in rel["media"]]
    return out


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold()


def _phrases(query: str) -> Dict[str, str]:
    out = {}
    for m in re.finditer(r'(\w+):"((?:[^"\\]|\\.)*)"', query):
        out[m.group(1)] = re.sub(r"\\(.)", r"\1", m.group(2))
    return out


def _credit(ac: list) -> str:
    return "".join(c["name"] + c.get("joinphrase", "") for c in ac)


def search_releases(query: str, limit: int) -> dict:
    q = _phrases(query)
    hits = []
    for rel in RELEASES.values():
        if "release" in q and _fold(q["release"]) not in _fold(rel["title"]):
            continue
        if "artist" in q and _fold(q["artist"]) not in _fold(_credit(rel["artist-credit"])):
            continue
        score = 100 if _fold(q.get("release", "")) == _fold(rel["title"]) else 80
        hits.append(_summary(rel, score))
    return {"created": "2026-11-24T00:00:00Z", "count": len(hits), "offset": 0, "releases": hits[:limit]}


def search_recordings(query: str, limit: int) -> dict:
    q = _phrases(query)
    recs: Dict[str, dict] = {}
    for rel in RELEASES.values():
        if "release" in q and _fold(q["release"]) not in _fold(rel["title"]):
            continue
        for m in rel["media"]:
            for t in m["tracks"]:
                r = t["recording"]
                if "recording" in q and _fold(q["recording"]) not in _fold(r["title"]):
                    continue
                if "artist" in q and _fold(q["artist"]) not in _fold(_credit(r["artist-credit"])):
                    continue
                entry = recs.setdefault(r["id"], {
                    "id": r["id"], "title": r["title"], "length": r["length"], "artist-credit": r["artist-credit"],
                    "score": 100 if _fold(q.get("recording", "")) == _fold(r["title"]) else 70, "releases": []})
                summary = _summary(rel, 0)
                summary.pop("score")
                summary["media"] = [{"position": m["position"], "format": m["format"], "track-count": m["track-count"],
                                     "track-offset": t["position"] - 1,
                                     "track": [{"id": t["id"], "number": t["number"], "title": t["title"],
                                                "length": t["length"]}]}]
                entry["releases"].append(summary)
    hits = sorted(recs.values(), key=lambda r: -r["score"])
    return {"created": "2026-11-24T00:00:00Z", "count": len(hits), "offset": 0, "recordings": hits[:limit]}


def answer(url: str) -> Tuple[int, dict]:
    """(status, JSON) for a /ws/2/ URL, as the fake server answers it."""
    parts = urllib.parse.urlsplit(url)
    params = dict(urllib.parse.parse_qsl(parts.query))
    path = parts.path
    if params.get("fmt") != "json":
        return 400, {"error": "fmt=json is required here"}
    limit = int(params.get("limit", "25"))
    if path == "/ws/2/release":
        return 200, search_releases(params.get("query", ""), limit)
    if path == "/ws/2/recording":
        return 200, search_recordings(params.get("query", ""), limit)
    m = re.match(r"^/ws/2/release/([^/]+)$", path)
    if m:
        rel = RELEASES.get(urllib.parse.unquote(m.group(1)))
        if rel is None:
            return 404, {"error": "Not Found"}
        return 200, rel
    return 404, {"error": "Not Found"}


class FakeMusicBrainz:
    """A threaded HTTP server on 127.0.0.1 answering like MusicBrainz's /ws/2/."""

    def __init__(self):
        self.requests: List[str] = []
        self.agents: List[str] = []
        self.fail_next: List[int] = []   # statuses to answer the next requests with (e.g. 503)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                fake.agents.append(self.headers.get("User-Agent", ""))
                if fake.fail_next:
                    status, body = fake.fail_next.pop(0), {"error": "busy"}
                else:
                    status, body = answer(self.path)
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if status == 503:
                    self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def transport(record: Optional[list] = None):
    """A MusicBrainzClient transport answering from the catalogue without a server."""
    def get(url: str, headers: dict, timeout: float):
        if record is not None:
            record.append(url)
        status, body = answer(url)
        return status, {}, json.dumps(body).encode("utf-8")
    return get
