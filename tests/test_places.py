"""Where a song was played (2.4): the geolocation client's politeness with a fake clock,
the parsing of answers, the place texts, when a lookup is due, the play history's
place columns (an old library.sqlite migrated, plays with places, forgetting them),
the places of Your Stats, and a lookup through the real HTTP code against FakeGeo on
127.0.0.1 (never the real service)."""

import os
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from fake_geo import ANSWER, FakeGeo  # noqa: E402

from realhires import musiclib, places, settings, stats  # noqa: E402
from realhires.musiclib import Track  # noqa: E402
from realhires.places import Place  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


def answers(*items):
    """A transport that gives these answers in turn and records the headers."""
    seen = []
    todo = list(items)

    def transport(url, headers, timeout):
        seen.append((url, headers))
        a = todo.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a

    return transport, seen


def ok(data=None):
    import json

    return 200, {}, json.dumps(ANSWER if data is None else data).encode("utf-8")


@pytest.fixture
def geo(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = FakeGeo()
    yield server
    server.close()


# ------------------------------------------------------------------- client ---
def test_client_names_itself_sends_nothing_else_and_keeps_its_interval():
    transport, seen = answers(ok(), ok())
    clock = Clock()
    c = places.GeoClient(transport=transport, clock=clock, sleep=clock.sleep)
    assert c.server == places.DEFAULT_SERVER == "https://free.freeipapi.com/api/v1/json/"
    assert c.min_interval == places.MIN_INTERVAL_S == 2.0
    assert c.lookup() == Place("Shinjuku", "Tokyo", "Japan", "JP", 35.69, 139.69)
    c.lookup()
    assert clock.slept == [2.0]  # the second lookup waited for the interval
    url, headers = seen[0]
    assert url == places.DEFAULT_SERVER  # no query, no path: nothing about the songs
    assert set(headers) == {"User-Agent", "Accept"}
    assert re.fullmatch(r"UniversalMusicPlayer/\d+\.\d+\.\d+ \( https://github\.com/SU2H1/Universal-Music-Player \)",
                        headers["User-Agent"])


def test_official_service_is_never_asked_faster_than_the_interval(monkeypatch):
    monkeypatch.setenv(places.INTERVAL_ENV, "0")
    assert places.default_interval(places.DEFAULT_SERVER) == 2.0
    assert places.default_interval("freeipapi.com") == 2.0
    assert places.default_interval("http://127.0.0.1:5000/api/v1/json/") == 0.0
    monkeypatch.setenv(places.INTERVAL_ENV, "nan")
    assert places.default_interval("http://127.0.0.1:5000/") == 2.0
    assert places.normalize_server("  ") == places.DEFAULT_SERVER
    assert places.normalize_server("ipwho.is/") == "https://ipwho.is/"
    assert places.host_of("https://ipwho.is/") == "ipwho.is"


def test_429_and_503_keep_retry_after_and_give_up_at_once():
    transport, seen = answers((429, {"retry-after": "120"}, b""), ok(), (503, {}, b""))
    clock = Clock()
    c = places.GeoClient(transport=transport, clock=clock, sleep=clock.sleep)
    with pytest.raises(places.PlaceError, match=r"free\.freeipapi\.com asked to wait before the next lookup\."):
        c.lookup()
    assert len(seen) == 1 and clock.slept == [] and c.not_before == 220.0
    # before the time asked for, a lookup sends nothing
    with pytest.raises(places.PlaceError, match="asked to wait"):
        c.lookup()
    assert len(seen) == 1
    clock.t = 220.0
    assert c.lookup().city == "Shinjuku"
    # without Retry-After: ten minutes
    with pytest.raises(places.PlaceError):
        c.lookup()
    assert c.not_before == pytest.approx(clock.t + places.DEFAULT_RETRY_AFTER_S)
    assert places.retry_after({"retry-after": "999999"}) == places.MAX_RETRY_AFTER_S
    assert places.retry_after({"retry-after": "soon"}) == places.DEFAULT_RETRY_AFTER_S
    assert places.retry_after({"retry-after": "-5"}) == 0.0


def test_server_and_network_errors_are_tried_three_times_with_a_back_off():
    transport, seen = answers((500, {}, b""), OSError("connection refused"), (502, {}, b""))
    clock = Clock()
    c = places.GeoClient(transport=transport, clock=clock, sleep=clock.sleep)
    with pytest.raises(places.PlaceError) as e:
        c.lookup()
    assert str(e.value) == ("No answer from free.freeipapi.com after 3 attempts (server error (HTTP 502)).")
    assert len(seen) == 3 and clock.slept == [2.0, 4.0]
    transport, seen = answers(OSError("no route"), OSError("no route"), OSError("no route"))
    c = places.GeoClient(transport=transport, clock=clock, sleep=clock.sleep)
    with pytest.raises(places.PlaceError, match=r"could not reach free\.freeipapi\.com: no route"):
        c.lookup()


def test_refused_requests_and_bad_answers_fail_at_once():
    for answer, message in (((403, {}, b"no"), "free.freeipapi.com refused the request (HTTP 403)."),
                            ((404, {}, b""), "free.freeipapi.com refused the request (HTTP 404)."),
                            ((200, {}, b"<html>"), "free.freeipapi.com sent an answer that could not be read."),
                            (ok({"ipAddress": "1.2.3.4"}), "The location service sent no place."),
                            (ok([1, 2]), "The location service sent no place."),
                            (ok({"success": False, "message": "Reserved range"}),
                             "The location service could not find this computer's place: Reserved range."),
                            (ok({"success": False}), "The location service could not find this computer's place."),
                            (ValueError("unknown url type: 'ftp'"),
                             "The location server address 'https://free.freeipapi.com/api/v1/json/' is not valid: "
                             "unknown url type: 'ftp'.")):
        transport, seen = answers(answer)
        clock = Clock()
        with pytest.raises(places.PlaceError) as e:
            places.GeoClient(transport=transport, clock=clock, sleep=clock.sleep).lookup()
        assert str(e.value) == message and len(seen) == 1 and clock.slept == []
    assert places.lookup_now(places.GeoClient(transport=answers((403, {}, b""))[0])) == (
        None, "free.freeipapi.com refused the request (HTTP 403).")


def test_stop_ends_a_wait():
    import threading

    stop = threading.Event()
    transport, seen = answers((500, {}, b""), ok())
    c = places.GeoClient(transport=transport, stop=stop, min_interval=0)
    stop.set()
    with pytest.raises(places.PlaceStopped):
        c.lookup()
    assert seen == []
    stop.clear()
    threading.Timer(0.2, stop.set).start()  # the app closes during the back-off after a server error
    with pytest.raises(places.PlaceStopped):
        c.lookup()
    assert len(seen) == 1


# ------------------------------------------------------------------ parsing ---
def test_answers_of_freeipapi_and_ipwho_is_are_understood():
    assert places.parse_place(ANSWER) == Place("Shinjuku", "Tokyo", "Japan", "JP", 35.69, 139.69)
    ipwho = {"ip": "198.51.100.7", "success": True, "country": "Japan", "country_code": "JP", "region": "Osaka",
             "city": "Osaka", "latitude": 34.6937378, "longitude": 135.5021651}
    assert places.parse_place(ipwho) == Place("Osaka", "Osaka", "Japan", "JP", 34.69, 135.5)
    p = places.parse_place({"cityName": "  San\tFrancisco \n(Financial District) ", "countryName": "United States",
                            "countryCode": "usa", "latitude": "37.79", "longitude": -0.004})
    assert p == Place("San Francisco (Financial District)", "", "United States", "", None, 0.0)
    assert places.parse_place({"countryName": "X" * 300, "latitude": 91, "longitude": True}) == Place(
        country="X" * places.MAX_NAME)
    assert places.parse_place({"city": "A\x00B", "latitude": float("nan")}).city == "A B"
    assert places.coordinate(-180, 180) == -180.0 and places.coordinate(180.0001, 180) is None


def test_place_texts_and_keys():
    assert places.place_text(Place("Shinjuku", "Tokyo", "Japan")) == "Shinjuku, Tokyo, Japan"
    assert places.place_text(Place("Tokyo", "tokyo", "Japan")) == "Tokyo, Japan"
    assert places.place_text(Place("Singapore", "Singapore", "Singapore")) == "Singapore"
    assert places.place_text(Place(country="Japan")) == "Japan"
    assert places.place_text(None) == ""
    assert places.place_key(Place("Tokyo", "", "JAPAN")) == places.place_key(Place("tokyo", "", "Japan"))
    assert places.place_key(None) == "" and Place("A").key == places.place_key(Place("A"))


def test_a_lookup_is_due_at_start_then_every_two_hours_while_music_plays():
    c = places.PlaceCache()
    assert c.due(0.0, playing=False)          # at start, music or not
    c.begin(0.0)
    assert not c.due(0.0, True)               # one at a time
    c.finish(Place("Tokyo", "", "Japan"), wall=1000.0)
    assert c.place.city == "Tokyo" and c.looked_up == 1000.0 and c.error == ""
    assert not c.due(places.REFRESH_S - 1, True)
    assert not c.due(places.REFRESH_S, False)   # nothing plays: no lookup
    assert c.due(places.REFRESH_S, True)
    assert not c.due(places.REFRESH_S, True, not_before=places.REFRESH_S + 5)  # Retry-After holds
    c.begin(places.REFRESH_S)
    c.finish(None, "free.freeipapi.com refused the request (HTTP 403).")
    assert c.place is None and c.error.endswith("(HTTP 403).") and c.looked_up == 1000.0
    assert not c.due(places.REFRESH_S + 60, True)  # a failure is not retried in a loop
    assert c.due(2 * places.REFRESH_S, True)
    c.reset()
    assert c.due(5.0, False) and c.place is None


# ------------------------------------------------------------------ history ---
def test_an_old_library_gets_the_place_columns_and_keeps_its_plays(tmp_path):
    path = tmp_path / "library.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE plays (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, started REAL, seconds REAL, "
               "duration REAL, title TEXT, artist TEXT, album TEXT, album_artist TEXT, genre TEXT, year TEXT, "
               "composer TEXT)")
    db.execute("INSERT INTO plays (path, started, seconds, duration, title, artist, album, album_artist, genre, year, "
               "composer) VALUES ('/m/a.flac', 1000, 40, 200, 'Old', 'Ann', '', '', '', '', '')")
    db.commit()
    db.close()
    lib = musiclib.Library(str(path))
    cols = [r[1] for r in lib.db.execute("PRAGMA table_info(plays)")]
    assert cols[-6:] == list(musiclib.PLACE_COLUMNS)
    (old,) = lib.plays()
    assert old.title == "Old" and old.place is None
    tokyo = places.parse_place(ANSWER)
    assert lib.add_play(Track("/m/b.flac", title="New", duration=100.0), 2000, 50, place=tokyo)
    assert lib.add_play(Track("/m/b.flac", title="New", duration=100.0), 3000, 50)
    lib.close()
    lib = musiclib.Library(str(path))  # opened again: nothing added twice
    assert [p.place for p in lib.plays()] == [None, tokyo, None]
    assert lib.places_count() == 1
    assert lib.forget_places() == 1 and lib.places_count() == 0
    assert [p.place for p in lib.plays()] == [None, None, None] and len(lib.plays()) == 3


def play(path, started, seconds, place, title="", artist="", duration=200.0):
    return stats.Play(path, started, seconds, duration, title, artist, place=place)


TOKYO = Place("Tokyo", "Tokyo", "Japan", "JP", 35.69, 139.69)
OSAKA = Place("Osaka", "Osaka", "Japan", "JP", 34.69, 135.5)


def test_the_places_of_a_period_and_their_songs():
    plays = [play("/m/a.flac", 10, 120, TOKYO, "Alpha", "Ann"), play("/m/b.flac", 20, 10, TOKYO, "Beta"),
             play("/m/a.flac", 30, 200, Place("tokyo", "TOKYO", "japan"), "Alpha", "Ann"),
             play("/m/c.flac", 40, 300, OSAKA, "Gamma", "", 250.0), play("/m/d.flac", 50, 999, None, "Delta")]
    rows = places.period_places(plays)
    assert [(r["name"], r["plays"], r["logged"], r["seconds"], r["songs"]) for r in rows] == [
        ("Tokyo, Japan", 2, 3, 330.0, 2), ("Osaka, Japan", 1, 1, 300.0, 1)]
    assert (rows[0]["first"], rows[0]["last"], rows[0]["latitude"], rows[0]["country_code"]) == (10, 30, 35.69, "JP")
    assert places.place_of_row(rows[0]) == TOKYO
    songs = places.place_songs(plays, rows[0]["key"])
    assert [(s["title"], s["artist"], s["plays"], s["seconds"]) for s in songs] == [
        ("Alpha", "Ann", 2, 320.0), ("Beta", "Unknown Artist", 0, 10.0)]
    assert places.place_songs(plays, "") == [] and places.period_places([]) == []


def test_the_day_list_names_each_play_s_place():
    plays = [play("/m/a.flac", 1_700_000_000, 60, TOKYO, "Alpha"), play("/m/b.flac", 1_700_000_100, 60, None, "Beta")]
    rows = stats.day_plays(plays, 2023, 11, 14, localtime=lambda ts: __import__("time").gmtime(ts))
    assert [(r["title"], r["place"]) for r in rows] == [("Beta", ""), ("Alpha", "Tokyo, Japan")]


def test_settings_are_off_by_default_and_not_detection_settings():
    from realhires.store import settings_key

    cfg = settings.Settings()
    assert cfg.play_location is False and cfg.location_server == places.DEFAULT_SERVER
    info = {f["name"]: f for f in settings.Settings.field_info()}
    assert info["play_location"]["group"] == info["location_server"]["group"] == "Playback"
    assert "IP address" in info["play_location"]["help"] and "never a song, path or tag" in info["play_location"]["help"]
    on = settings.Settings(play_location=True, location_server="http://127.0.0.1:1/")
    assert settings_key(on) == settings_key(cfg)


# ------------------------------------------------------------- over HTTP ---
def test_a_lookup_through_http_against_the_fake_service(geo):
    c = places.GeoClient(geo.url, min_interval=0)
    assert c.lookup() == Place("Shinjuku", "Tokyo", "Japan", "JP", 35.69, 139.69)
    path, headers = geo.requests[0]
    assert path == "/api/v1/json/" and headers["user-agent"] == places.user_agent()
    geo.status = (503, {"Retry-After": "30"})
    with pytest.raises(places.PlaceError, match="asked to wait"):
        c.lookup()
    assert len(geo.requests) == 2 and c.not_before > c.clock()


def test_every_message_of_a_lookup_is_translated():
    from realhires import i18n

    messages = ["free.freeipapi.com asked to wait before the next lookup.",
                "No answer from free.freeipapi.com after 3 attempts (server error (HTTP 502)).",
                "No answer from free.freeipapi.com after 3 attempts (could not reach free.freeipapi.com: "
                "no answer in time).",
                "free.freeipapi.com refused the request (HTTP 403).",
                "free.freeipapi.com sent an answer that could not be read.",
                "The location service sent no place.",
                "The location service could not find this computer's place.",
                "The location service could not find this computer's place: Reserved range.",
                "The location server address 'ftp://x/' is not valid: unknown url type: 'ftp'."]
    before = i18n.language()
    try:
        i18n.set_language("日本語")
        for m in messages:
            shown = i18n.tr_text(m)
            assert shown != m and not any(w in shown for w in ("answer", "server error", "could not")), (m, shown)
        assert i18n.tr_text(messages[1]) == "3回試しましたがfree.freeipapi.comから応答がありません（サーバーエラー（HTTP 502））。"
    finally:
        i18n.set_language(before)
