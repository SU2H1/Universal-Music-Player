"""Write rust/crates/core/tests/python_musicbrainz.json: the Python answers of
realhires.musicbrainz for the invented catalogue of tests/fake_musicbrainz.py,
so the Rust port can be checked to build the same requests and make the same
choices (2.1).  Run it after changing realhires/musicbrainz.py:

    python3 rust/tools/gen_mb_vectors.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import fake_musicbrainz as fm  # noqa: E402
from realhires import musicbrainz as mb  # noqa: E402

SERVER = "https://musicbrainz.org"
responses = {}


def client(urls):
    def get(url, headers, timeout):
        urls.append(url)
        status, body = fm.answer(url)
        text = json.dumps(body, ensure_ascii=False)
        responses[url] = [status, text]
        return status, {}, text.encode("utf-8")
    return mb.MusicBrainzClient(SERVER, transport=get, min_interval=0)


def song(path, duration, **tags):
    return {"path": path, "tags": tags, "duration": duration}


def to_song(d):
    return mb.Song(d["path"], dict(d["tags"]), d["duration"])


def pos(t):
    return None if t is None else [t.disc, t.number]


NIGHT = [song("/m/n/01.flac", 201.2, title="Neon Streets", artist="The Example Band", album="Night Drive", track="1"),
         song("/m/n/02.flac", 245.0, title="Midnight Signal", artist="The Example Band", album="Night Drive"),
         song("/m/n/03.flac", 187.0, title="harbour lights", artist="The Example Band", album="Night Drive", track="3"),
         song("/m/n/04.flac", 263.0, title="Last Train Home", artist="The Example Band", album="Night Drive",
              track="4/4", genre="Pop", album_artist="The Example Band")]
WRONG = [dict(NIGHT[0], tags=dict(NIGHT[0]["tags"], track="3"))] + NIGHT[1:]
UNTITLED = [song(f"/m/u/{i}.flac", 0.0) for i in range(4)]
STRANGER = [NIGHT[0], dict(NIGHT[1], tags=dict(NIGHT[1]["tags"], title="Unknown Song"))] + NIGHT[2:]
ROAD = song("/m/road.mp3", 222.4, title="Lonely Road", artist="Solo Artist")


def main() -> None:
    cases = []

    for album, artist in [("Night Drive", "The Example Band"), ('A "quoted" \\ name', ""), ("  Spaced   out ", "x")]:
        cases.append({"kind": "release_query", "album": album, "artist": artist,
                      "expect": mb.release_query(album, artist)})
    for title, artist, album in [("Lonely Road", "Solo Artist", ""), ('River "Song"', "", "A\\B"), ("日本語", "歌手", "")]:
        cases.append({"kind": "recording_query", "title": title, "artist": artist, "album": album,
                      "expect": mb.recording_query(title, artist, album)})

    for rid in ["rel-night-2001", "rel-night-deluxe", "rel-road-album", "rel-road-single", "rel-night-boot", "missing"]:
        urls = []
        d = client(urls).release(rid)
        cases.append({"kind": "detail", "id": rid, "urls": urls,
                      "expect": None if d is None else {
                          "release": asdict(d.release), "genre": d.genre, "date": d.date,
                          "tracks": [asdict(t) for t in d.tracks]}})

    for album, artist in [("Night Drive", "The Example Band"), ("Night", ""), ("Nothing", "Nobody")]:
        urls = []
        rs = client(urls).search_releases(album, artist)
        cases.append({"kind": "search_releases", "album": album, "artist": artist, "urls": urls,
                      "expect": [asdict(r) for r in rs]})
    urls = []
    recs = client(urls).search_recordings("Lonely Road", "Solo Artist")
    cases.append({"kind": "search_recordings", "title": "Lonely Road", "artist": "Solo Artist", "album": "",
                  "urls": urls, "expect": [asdict(r) for r in recs]})

    for name, songs, rid, by_order in [("night", NIGHT, "rel-night-2001", True), ("wrong", WRONG, "rel-night-2001", False),
                                       ("untitled", UNTITLED, "rel-night-2001", True),
                                       ("untitled-strict", UNTITLED, "rel-night-2001", False),
                                       ("deluxe", NIGHT, "rel-night-deluxe", True)]:
        d = client([]).release(rid)
        m = mb.match_tracks([to_song(s) for s in songs], d, by_order)
        cases.append({"kind": "match", "name": name, "songs": songs, "id": rid, "by_order": by_order,
                      "expect": [pos(t) for t in m]})

    for rid, s, at, fields, only_empty in [
            ("rel-night-2001", NIGHT[1], [1, 2], list(mb.FIELDS), True),
            ("rel-night-2001", NIGHT[3], [1, 4], list(mb.FIELDS), True),
            ("rel-night-2001", NIGHT[2], [1, 3], list(mb.FIELDS), False),
            ("rel-night-2001", NIGHT[3], [1, 4], ["genre"], False),
            ("rel-night-deluxe", NIGHT[0], [1, 1], ["disc", "track"], False),
            ("rel-road-album", ROAD, [1, 2], list(mb.FIELDS), False)]:
        d = client([]).release(rid)
        ch = mb.changes_for(to_song(s), d, d.track_at(*at), fields, only_empty)
        cases.append({"kind": "changes", "id": rid, "song": s, "at": at, "fields": fields, "only_empty": only_empty,
                      "expect": ch})

    for name, songs in [("night", NIGHT), ("stranger", STRANGER),
                        ("other-artist", [dict(s, tags=dict(s["tags"], artist="Other Band")) for s in NIGHT])]:
        urls = []
        d, m = mb.find_album(client(urls), [to_song(s) for s in songs])
        cases.append({"kind": "find_album", "name": name, "songs": songs, "urls": urls,
                      "expect": {"id": d.release.id if d else None, "tracks": [pos(t) for t in m]}})

    for name, s in [("road", ROAD), ("no-length", dict(ROAD, duration=0.0)), ("too-long", dict(ROAD, duration=260.0))]:
        urls = []
        d, t = mb.find_song(client(urls), to_song(s))
        cases.append({"kind": "find_song", "name": name, "song": s, "urls": urls,
                      "expect": {"id": d.release.id if d else None, "track": pos(t)}})

    for songs, title, artist, album_mode in [(NIGHT, "Night Drive", "The Example Band", True),
                                             ([ROAD], "Lonely Road", "Solo Artist", False)]:
        urls = []
        found = mb.search_candidates(client(urls), [to_song(s) for s in songs], title, artist, album_mode)
        cases.append({"kind": "candidates", "songs": songs, "title": title, "artist": artist,
                      "album_mode": album_mode, "urls": urls, "expect": [r.id for r in found]})

    for tags in [{"title": "a", "artist": "b"}, {"title": "a"}, {"title": "a", "album_artist": "b"},
                 {"title": "a", "artist": "b", "album": "c", "album_artist": "b", "date": "2000", "track": "1",
                  "genre": "Pop"}]:
        cases.append({"kind": "needs_lookup", "tags": tags, "expect": mb.needs_lookup(tags)})
    for songs in [NIGHT, UNTITLED, [ROAD]]:
        cases.append({"kind": "album_of", "songs": songs, "expect": list(mb.album_of([to_song(s) for s in songs]))})
    for name in ["cool jazz", "r&b", "  hip   hop ", "j-pop", "électro"]:
        cases.append({"kind": "genre_name", "name": name, "expect": mb.genre_name(name)})
    groups = mb.groups([to_song(s) for s in NIGHT + [ROAD] + [dict(NIGHT[0], path="/m/other/01.flac")]])
    cases.append({"kind": "groups", "songs": NIGHT + [ROAD] + [dict(NIGHT[0], path="/m/other/01.flac")],
                  "expect": [[s.path for s in g] for g in groups]})
    cases.append({"kind": "user_agent", "expect": mb.user_agent()})

    out = {"server": SERVER, "responses": responses, "cases": cases}
    path = os.path.join(ROOT, "rust", "crates", "core", "tests", "python_musicbrainz.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"{path}: {len(cases)} cases, {len(responses)} answers")


if __name__ == "__main__":
    main()
