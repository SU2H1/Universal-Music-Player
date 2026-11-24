"""Song information from MusicBrainz (2.1): the client's politeness, the queries,
the parsing of releases, matching songs to tracks, the tags written, and the
automatic lookup on real files, all against the invented catalogue of
tests/fake_musicbrainz.py (never musicbrainz.org)."""

import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from fake_musicbrainz import RELEASES, FakeMusicBrainz, transport  # noqa: E402

from realhires import musicbrainz as mb  # noqa: E402
from realhires.probe import probe  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


def client(urls=None, **kw):
    return mb.MusicBrainzClient("https://musicbrainz.org", transport=transport(urls), min_interval=0, **kw)


def night(over=None, paths=None):
    base = [("Neon Streets", "1", 201.2), ("Midnight Signal", "", 245.0), ("harbour lights", "3", 187.0),
            ("Last Train Home", "4/4", 263.0)]
    out = []
    for i, (title, num, dur) in enumerate(base):
        tags = {"title": title, "artist": "The Example Band", "album": "Night Drive", "track": num}
        tags.update((over or {}).get(i, {}))
        out.append(mb.Song((paths or [f"/music/night/{i + 1:02d}.flac" for i in range(4)])[i], tags, dur))
    return out


# ------------------------------------------------------------------ client ---
def test_client_names_itself_and_keeps_one_request_per_second():
    seen = []

    def fake(url, headers, timeout):
        seen.append(headers)
        return 200, {}, b'{"releases": []}'

    clock = Clock()
    c = mb.MusicBrainzClient(transport=fake, clock=clock, sleep=clock.sleep)
    assert c.min_interval == mb.MIN_INTERVAL_S == 1.0
    for _ in range(3):
        c.search_releases("Night Drive")
    assert clock.slept == [1.0, 1.0]
    assert re.fullmatch(r"UniversalMusicPlayer/\d+\.\d+\.\d+ \( https://github\.com/SU2H1/Universal-Music-Player \)",
                        seen[0]["User-Agent"])


def test_official_server_is_never_asked_faster_than_once_a_second(monkeypatch):
    monkeypatch.setenv(mb.INTERVAL_ENV, "0")
    assert mb.default_interval("https://musicbrainz.org") == 1.0
    assert mb.default_interval("beta.musicbrainz.org") == 1.0
    assert mb.default_interval("http://127.0.0.1:5000") == 0.0


def test_client_waits_on_503_gives_up_and_refuses_bad_requests():
    answers = [(503, {"retry-after": "4"}, b""), (503, {}, b""), (200, {}, b'{"releases": []}')]
    clock = Clock()
    c = mb.MusicBrainzClient(transport=lambda u, h, t: answers.pop(0), clock=clock, sleep=clock.sleep)
    assert c.search_releases("x") == []
    assert clock.slept[0] == 4.0 and clock.slept[1] >= 1.0 and c.requests == 3

    c = mb.MusicBrainzClient(transport=lambda u, h, t: (503, {}, b""), clock=clock, sleep=clock.sleep)
    with pytest.raises(mb.MusicBrainzError, match="after 4 attempts"):
        c.search_releases("x")
    assert c.requests == 4

    c = mb.MusicBrainzClient(transport=lambda u, h, t: (400, {}, b'{"error": "Invalid query"}'),
                             clock=clock, sleep=clock.sleep)
    with pytest.raises(mb.MusicBrainzError, match=r"refused the request \(HTTP 400: Invalid query\)"):
        c.search_releases("x")
    assert c.requests == 1

    c = mb.MusicBrainzClient(transport=lambda u, h, t: (404, {}, b""), clock=clock, sleep=clock.sleep)
    assert c.release("nope") is None


def test_stop_ends_a_wait():
    import threading

    stop = threading.Event()
    stop.set()
    c = mb.MusicBrainzClient(transport=transport(), stop=stop)
    with pytest.raises(mb.MusicBrainzStopped):
        c.search_releases("Night Drive")


def test_queries_are_lucene_phrases():
    assert mb.release_query("Night Drive", "The Example Band") == 'release:"Night Drive" AND artist:"The Example Band"'
    assert mb.recording_query('River "Song"', "", "A\\B") == 'recording:"River \\"Song\\"" AND release:"A\\\\B"'
    urls = []
    client(urls).search_recordings("Lonely Road", "Solo Artist")
    assert urls == ["https://musicbrainz.org/ws/2/recording?query=recording%3A%22Lonely+Road%22+AND+artist%3A%22"
                    "Solo+Artist%22&limit=10&fmt=json"]


# ------------------------------------------------------------------ parsing ---
def test_release_lookup_gives_tracks_genre_composers_and_first_date():
    d = client().release("rel-night-deluxe")
    assert d.release.title == "Night Drive" and d.release.artist == "The Example Band"
    assert d.release.formats == "2×CD" and d.release.disc_count == 2 and d.release.track_count == 6
    assert d.genre == "Electronic"            # the most votes; "synth-pop" has fewer
    assert d.date == "1999-05-28"             # the album's first release, not this 2015 edition
    t = d.track_at(1, 2)
    assert (t.title, t.track_count, t.disc_count, t.length, t.composer) == (
        "Midnight Signal", 4, 2, 245.5, "Kai Example, Mio Example")
    assert d.track_at(2, 1).title == "Neon Streets (Demo)"
    road = client().release("rel-road-album")
    assert road.track_at(1, 2).composer == "Ann Writer"          # a writer when no composer is named
    assert road.track_at(1, 3).artist == "Solo Artist feat. Guest Singer"
    assert road.genre == "Country"            # a tie: the name first in order
    assert mb.genre_name("cool jazz") == "Cool Jazz"


# ----------------------------------------------------------------- matching ---
def test_songs_are_matched_by_number_then_title_then_order():
    d = client().release("rel-night-2001")
    m = mb.match_tracks(night(), d)
    assert [(t.disc, t.number) for t in m] == [(1, 1), (1, 2), (1, 3), (1, 4)]
    # a wrong number whose title and length disagree is not trusted; the title finds the track
    songs = night({0: {"track": "3"}})
    m = mb.match_tracks(songs, d, by_order=False)
    assert m[0].number == 1 and m[2].number == 3
    # untitled songs in the same number as the tracks: in order
    songs = [mb.Song(f"/x/{i}.flac", {}, 0.0) for i in range(4)]
    assert [t.number for t in mb.match_tracks(songs, d)] == [1, 2, 3, 4]
    assert mb.match_tracks(songs, d, by_order=False) == [None] * 4


def test_changes_fill_in_or_correct_only_what_differs():
    d = client().release("rel-night-2001")
    songs = night({3: {"genre": "Pop", "album_artist": "The Example Band"}})
    m = mb.match_tracks(songs, d)
    assert mb.changes_for(songs[1], d, m[1], only_empty=True) == {
        "album_artist": "The Example Band", "genre": "Electronic", "date": "1999-05-28", "track": "2/4",
        "composer": "Kai Example, Mio Example"}
    assert mb.changes_for(songs[3], d, m[3], only_empty=True) == {"date": "1999-05-28", "composer": "Kai Example"}
    full = mb.changes_for(songs[2], d, m[2])
    assert full["title"] == "Harbour Lights" and full["track"] == "3/4" and "disc" not in full
    assert mb.changes_for(songs[3], d, m[3], fields=["genre"]) == {"genre": "Electronic"}
    deluxe = client().release("rel-night-deluxe")
    assert mb.changes_for(songs[0], deluxe, deluxe.track_at(1, 1), fields=["disc", "track"]) == {
        "disc": "1/2", "track": "1/4"}


def test_automatic_mode_takes_only_a_certain_album():
    urls = []
    c = client(urls)
    d, m = mb.find_album(c, night())
    assert d.release.id == "rel-night-2001"   # four tracks for four songs, official
    assert all(t is not None for t in m)
    # a song that is not on the album: nothing is certain
    songs = night({1: {"title": "Unknown Song"}})
    d, m = mb.find_album(client(), songs)
    assert d is None and m == [None] * 4
    # a wrong album artist finds nothing
    assert mb.find_album(client(), night({i: {"artist": "Other Band"} for i in range(4)}))[0] is None


def test_a_song_without_album_gets_the_original_album_not_the_single_or_a_compilation():
    s = mb.Song("/x/road.mp3", {"title": "Lonely Road", "artist": "Solo Artist"}, 222.4)
    d, t = mb.find_song(client(), s)
    assert d.release.id == "rel-road-album" and t.number == 2
    assert mb.changes_for(s, d, t) == {"album": "Open Country", "album_artist": "Solo Artist", "genre": "Country",
                                       "date": "2010-06-01", "track": "2/3", "composer": "Ann Writer"}
    # without a length a namesake cannot be ruled out
    assert mb.find_song(client(), mb.Song("/x/r.mp3", s.tags, 0.0)) == (None, None)
    assert mb.find_song(client(), mb.Song("/x/r.mp3", s.tags, 260.0)) == (None, None)


def test_search_candidates_for_the_window():
    songs = night()
    found = mb.search_candidates(client(), songs, "Night Drive", "The Example Band", album_mode=True)
    assert [r.id for r in found] == ["rel-night-2001", "rel-night-deluxe", "rel-night-boot"]
    one = [mb.Song("/x/road.mp3", {"title": "Lonely Road", "artist": "Solo Artist"}, 222.0)]
    found = mb.search_candidates(client(), one, "Lonely Road", "Solo Artist", album_mode=False)
    assert [r.id for r in found] == ["rel-road-album", "rel-road-single", "rel-road-best"]


def test_needs_lookup():
    assert mb.needs_lookup({"title": "a", "artist": "b"})
    assert not mb.needs_lookup({"title": "a"})
    full = {"title": "a", "artist": "b", "album": "c", "album_artist": "b", "date": "2000", "track": "1",
            "genre": "Pop"}
    assert not mb.needs_lookup(full)


# ----------------------------------------------------- the automatic lookup ---
def _flac(path, tags):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration={}".format(tags.pop("_s")),
           "-ac", "2", "-ar", "44100"]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    subprocess.run(cmd + [str(path)], check=True)
    return str(path)


@pytest.fixture
def fake_mb(monkeypatch):
    monkeypatch.setenv(mb.INTERVAL_ENV, "0")
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    fake = FakeMusicBrainz()
    yield fake
    fake.close()


def test_automatic_lookup_fills_in_missing_tags_once(tmp_path, fake_mb):
    album = tmp_path / "Night Drive"
    album.mkdir()
    paths = [
        _flac(album / "01.flac", {"title": "Neon Streets", "artist": "The Example Band", "album": "Night Drive",
                                  "track": "1", "_s": 201}),
        _flac(album / "02.flac", {"title": "Midnight Signal", "artist": "The Example Band", "album": "Night Drive",
                                  "track": "2", "genre": "My Genre", "_s": 245}),
        _flac(album / "03.flac", {"title": "Harbour Lights", "artist": "The Example Band", "album": "Night Drive",
                                  "track": "3", "_s": 187}),
        _flac(album / "04.flac", {"title": "Last Train Home", "artist": "The Example Band", "album": "Night Drive",
                                  "track": "4", "_s": 263}),
        _flac(tmp_path / "road.flac", {"title": "Lonely Road", "artist": "Solo Artist", "_s": 222}),
        _flac(tmp_path / "untagged.flac", {"_s": 3}),
    ]
    c = mb.MusicBrainzClient(fake_mb.url)
    out = mb.run_auto(c, paths)
    by = {os.path.basename(o.path): o for o in out}
    assert by["01.flac"].status == mb.FILLED and by["road.flac"].status == mb.FILLED
    assert "untagged.flac" not in by
    t1 = probe(paths[0]).tags
    assert (t1["album_artist"], t1["date"], t1["genre"], t1["composer"]) == (
        "The Example Band", "1999-05-28", "Electronic", "Kai Example")
    assert t1["title"] == "Neon Streets" and t1["track"].startswith("1")
    assert probe(paths[1]).tags["genre"] == "My Genre"          # never replaced
    road = probe(paths[4]).tags
    assert (road["album"], road["track"].split("/")[0]) == ("Open Country", "2")
    assert all("UniversalMusicPlayer/" in a for a in fake_mb.agents)
    assert mb.auto_summary(out).startswith("MusicBrainz filled in the information of 5 songs.")
    # a second run asks nothing
    n = len(fake_mb.requests)
    assert mb.run_auto(c, paths) == []
    assert len(fake_mb.requests) == n
    log = mb.LookupLog()
    assert log.status(paths[5]) == mb.NO_TAGS and log.status(paths[0]) == mb.FILLED
    log.close()


def test_failed_lookups_are_asked_again(tmp_path, fake_mb):
    p = _flac(tmp_path / "road.flac", {"title": "Lonely Road", "artist": "Solo Artist", "_s": 222})
    fake_mb.fail_next = [500] * 4
    out = mb.run_auto(mb.MusicBrainzClient(fake_mb.url), [p])
    assert [o.status for o in out] == [mb.FAILED] and "after 4 attempts" in out[0].message
    assert "could not be looked up" in mb.auto_summary(out)
    out = mb.run_auto(mb.MusicBrainzClient(fake_mb.url), [p])
    assert [o.status for o in out] == [mb.FILLED]


def test_settings_default_to_no_automatic_lookup():
    from realhires.settings import Settings

    s = Settings()
    assert s.musicbrainz_auto is False and s.musicbrainz_server == mb.DEFAULT_SERVER


def test_catalogue_is_invented():
    assert all(r["id"].startswith("rel-") for r in RELEASES.values())
