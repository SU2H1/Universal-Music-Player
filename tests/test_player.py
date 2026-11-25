"""The 2.0 player's core: EQ maths, the play queue, the player state, lyrics inside
audio files and the music library index."""

import hashlib
import json
import math
import os
import shutil
import subprocess

import numpy as np
import pytest

from realhires import cli, embed, eq, musiclib, playerstate
from realhires.playqueue import PlayQueue, Song, SplitMix64, album_order, shuffled

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


# ------------------------------------------------------------------- EQ ---
def test_eq_biquads_have_the_cookbook_response():
    rate = 48000.0
    for kind, gain in (("peak", 6.0), ("peak", -9.0), ("low_shelf", 4.0), ("high_shelf", -5.0)):
        c = eq.biquad(kind, 1000.0, gain, 1.0, rate)
        at = {"peak": 1000.0, "low_shelf": 20.0, "high_shelf": 20000.0}[kind]
        assert abs(eq.magnitude_db([c], at, rate) - gain) < 0.05, (kind, gain)
    lp = eq.biquad("low_pass", 1000.0, 0.0, math.sqrt(0.5), rate)
    assert abs(eq.magnitude_db([lp], 1000.0, rate) + 3.0103) < 0.01   # -3 dB at the corner
    assert eq.magnitude_db([lp], 100.0, rate) > -0.1
    notch = eq.biquad("notch", 1000.0, 0.0, 2.0, rate)
    assert eq.magnitude_db([notch], 1000.0, rate) < -100
    assert eq.biquad("peak", 30000.0, 6.0, 1.0, 44100.0) == eq.IDENTITY  # above Nyquist: ignored
    assert eq.biquad("peak", 1000.0, 0.0, 1.0, 44100.0) == eq.IDENTITY


def test_eq_state_presets_and_headroom():
    s = eq.normalize({"enabled": True, "graphic": {"gains": [99, -99, 0, 0, 0, 0, 0, 0, 0, "x"], "preamp": 50},
                      "parametric": {"bands": [{"type": "bogus", "freq": 5, "q": 0}] * 20}})
    assert s["graphic"]["gains"][:2] == [12.0, -12.0] and s["graphic"]["gains"][9] == 0.0
    assert s["graphic"]["preamp"] == 12.0 and s["graphic"]["preset"] == eq.CUSTOM
    assert len(s["parametric"]["bands"]) == eq.MAX_PARAMETRIC_BANDS
    assert s["parametric"]["bands"][0] == {"type": "peak", "freq": 20.0, "gain": 0.0, "q": 0.1, "on": True}
    rock = eq.apply_preset(eq.default_state(), "Rock")
    rock["enabled"] = True
    assert rock["graphic"]["preset"] == "Rock" and eq.preset_name(rock["graphic"]["gains"]) == "Rock"
    # "prevent clipping" lowers the preamp so the curve never rises above 0 dB
    assert max(db for _, db in eq.response(rock)) <= 1e-9
    rock["prevent_clipping"] = False
    assert max(db for _, db in eq.response(rock)) > 3
    off = eq.default_state()
    assert eq.stages(off, 44100) == [] and eq.ffmpeg_filter(off, 44100) is None


@needs_ffmpeg
def test_ffmpeg_applies_exactly_the_same_eq():
    """The Python player decodes through FFmpeg's biquad filters; they must give the same
    samples as the reference arithmetic the Rust player uses."""
    s = eq.apply_preset(eq.default_state(), "Electronic")
    s["enabled"] = True
    s["parametric"] = {"enabled": True, "preamp": -1.0,
                       "bands": [{"type": "high_pass", "freq": 25.0, "gain": 0.0, "q": 0.707, "on": True},
                                 {"type": "peak", "freq": 3150.0, "gain": -4.5, "q": 3.0, "on": True}]}
    rate = 44100
    x = np.random.default_rng(3).uniform(-0.25, 0.25, (8000, 2))
    out = subprocess.run(["ffmpeg", "-v", "error", "-f", "f64le", "-ar", str(rate), "-ac", "2", "-i", "-", "-af",
                          f"aformat=sample_fmts=dbl,{eq.ffmpeg_filter(s, rate)}", "-f", "f64le", "-"],
                         input=x.astype("<f8").tobytes(), capture_output=True, check=True).stdout
    y = np.frombuffer(out, "<f8").reshape(-1, 2)
    ref = x * 10 ** (eq.preamp_db(s) / 20)
    for c in eq.stages(s, rate):
        bq = eq.Biquad(c, 2)
        ref = np.array([[bq.process(v, ch) for ch, v in enumerate(row)] for row in ref])
    assert np.max(np.abs(y - ref)) < 1e-10


# ---------------------------------------------------------------- queue ---
def _library():
    songs = []
    for a, (artist, genre) in enumerate((("Band A", "Rock"), ("Band B", "Jazz"))):
        for album in range(2):
            for n in range(1, 5):
                songs.append(Song(f"/m/{artist}/{album}/{n:02d}.flac", f"Song {a}{album}{n}", artist,
                                  f"{artist} Album {album}", artist, genre if album == 0 else "Pop", 1, n))
    return songs


def test_queue_shuffle_modes_draw_from_the_right_songs():
    lib = _library()
    by = {s.path: s for s in lib}
    album0 = [s.path for s in album_order(s for s in lib if s.album == "Band A Album 0")]
    for mode, check in (("album", lambda s: s.album == "Band A Album 0"),
                        ("artist", lambda s: s.artist == "Band A"),
                        ("genre", lambda s: s.genre == "Rock"),
                        ("library", lambda s: True)):
        q = PlayQueue(lib, album0, shuffle=mode, seed=7)
        q.start(album0[1])
        order = [q.current] + q.up_next(100)
        assert order[0] == album0[1] and len(order) == len(set(order))
        assert all(check(by[p]) for p in order), mode
        assert len(order) == sum(1 for s in lib if check(s)), mode
    q = PlayQueue(lib, album0, shuffle="off")
    q.start(album0[1])
    assert q.up_next() == album0[2:]
    everything = [s.path for s in lib]
    q = PlayQueue(lib, everything, shuffle="songs", seed=1)
    q.start(everything[0])
    assert sorted(q.up_next(100)) == sorted(everything[1:]) and q.up_next(100) != everything[1:]
    # the same seed gives the same order (the Rust version reproduces it)
    a, b = PlayQueue(lib, album0, shuffle="library", seed=42), PlayQueue(lib, album0, shuffle="library", seed=42)
    a.start(album0[0]), b.start(album0[0])
    assert a.up_next(100) == b.up_next(100)


def test_queue_repeat_modes():
    lib = _library()
    album0 = [s.path for s in album_order(s for s in lib if s.album == "Band A Album 0")]
    everything = [s.path for s in lib]
    q = PlayQueue(lib, everything, repeat="song")
    q.start(album0[0])
    assert q.next(auto=True) == album0[0]          # the song ended: again
    assert q.next(auto=False) == album0[1]         # the Next button moves on
    q = PlayQueue(lib, everything, repeat="stop")
    q.start(album0[0])
    assert q.next(auto=True) is None and q.repeat == "off"  # stops once, then back to off
    q = PlayQueue(lib, everything, repeat="album")
    q.start(album0[2])
    assert q.next(auto=True) == album0[3]
    assert q.next(auto=True) == album0[0]          # end of the album: from the start again
    q = PlayQueue(lib, everything, repeat="off")
    q.start(everything[-1])
    assert q.next(auto=True) is None
    assert q.previous() is None
    q.start(everything[0]), q.next(), q.next()
    assert q.previous() == everything[1]


def test_queue_play_next_and_add_to_queue():
    """2.1: Play Next / Add to Queue from a song's menu, and Up Next jumps over them."""
    lib = _library()
    album0 = [s.path for s in album_order(s for s in lib if s.album == "Band A Album 0")]
    other = [s.path for s in lib if s.album != "Band A Album 0"][:4]
    for repeat in ("off", "album"):
        q = PlayQueue(lib, album0, repeat=repeat)
        q.start(album0[0])
        q.add_to_queue([other[0]])
        q.play_next([other[1], other[2]])  # before the song queued earlier
        q.add_to_queue([other[3]])
        assert q.up_next(6) == [other[1], other[2], other[0], other[3], album0[1], album0[2]]
        assert q.next(auto=True) == other[1]  # queued songs play whatever the album
        assert q.next() == other[2] and q.previous() == other[1] and q.next() == other[2]
        assert q.jump(1) == other[3] and q.queued == []  # the queued song before it is dropped
        assert q.next(auto=True) == album0[1]  # then the list goes on
    q = PlayQueue(lib, album0)
    q.start(album0[0])
    q.add_to_queue([other[0]])
    assert q.jump(2) == album0[2] and q.queued == []  # a jump into the list drops the queue
    assert q.jump(99) is None and q.current == album0[2]


def test_up_next_can_be_reordered_and_songs_removed():
    """2.3: Up Next's songs can be dragged to another place and removed with a right click."""
    lib = _library()
    album0 = [s.path for s in album_order(s for s in lib if s.album == "Band A Album 0")]
    a, b, c, d = album0
    q = PlayQueue(lib, album0)
    q.start(a)
    assert q.move_upcoming(2, 0) and q.up_next() == [d, b, c]      # d dragged to the top
    assert q.move_upcoming(0, 2) and q.up_next() == [b, c, d]      # and back to the end
    assert not q.move_upcoming(0, 3) and not q.move_upcoming(1, 1) and not q.move_upcoming(-1, 0)
    assert q.up_next() == [b, c, d]
    assert q.remove_upcoming(1) == c and q.up_next() == [b, d] and q.removed == {c}
    assert q.remove_upcoming(5) is None and q.remove_upcoming(-1) is None and q.current == a
    assert q.next() == b and q.next() == d and q.next() is None    # c is skipped
    # a removed song stays out when the order is built again, until a new list starts
    q.start(a)
    assert q.removed == set() and q.up_next() == [b, c, d]
    q.remove_upcoming(0)
    q.set_shuffle("songs")
    assert sorted(q.up_next()) == sorted([c, d])
    q.set_shuffle("off")
    assert q.up_next() == [c, d]
    q = PlayQueue(lib, album0, repeat="album")
    q.start(c)
    q.remove_upcoming(0)                                            # d
    assert q.next(auto=True) == a and q.up_next() == [b, c]         # the album again, without d
    # the three parts: songs stepped back over, queued songs, the rest of the list
    other = [s.path for s in lib if s.album != "Band A Album 0"][:3]
    q = PlayQueue(lib, album0)
    q.start(a)
    q.next(), q.next(), q.previous(), q.previous()                  # back at a: b and c were played
    q.play_next([other[0]])
    q.add_to_queue([other[1]])
    assert q.up_next() == [b, c, other[0], other[1], d] and q.top == 2
    assert q.move_upcoming(3, 0)                                    # a queued song before b: stepped back
    assert q.up_next() == [other[1], b, c, other[0], d] and q.queued == [other[0]] and q.top == 3
    assert q.move_upcoming(1, 3)                                    # b before d: the rest of the list
    assert q.up_next() == [other[1], c, other[0], b, d] and q.top == 2
    assert q.move_upcoming(1, 2)                                    # c before b: still the list, after the queue
    assert q.up_next() == [other[1], other[0], c, b, d] and q.queued == [other[0]] and q.top == 1
    assert q.move_upcoming(2, 0)                                    # c to the very top: stepped back again
    assert q.up_next() == [c, other[1], other[0], b, d] and q.top == 2
    assert q.remove_upcoming(2) == other[0] and q.queued == [] and other[0] not in q.removed
    assert q.remove_upcoming(0) == c and c in q.removed and q.top == 1
    assert [q.next(), q.next(), q.next(), q.next()] == [other[1], b, d, None]
    # a queued song moved among the list's songs is still an interruption for repeat album
    q = PlayQueue(lib, album0, repeat="album")
    q.start(a)
    q.add_to_queue([other[2]])
    assert q.up_next() == [other[2], b, c, d]
    assert q.move_upcoming(0, 1) and q.queued == [] and q.up_next() == [b, other[2], c, d]
    assert [q.next(auto=True) for _ in range(5)] == [b, other[2], c, d, a]


def test_splitmix_matches_the_reference_values():
    rng = SplitMix64(1234567)
    # reference values of SplitMix64 (Steele, Lea, Flood) for this seed
    assert [rng.next() for _ in range(3)] == [6457827717110365317, 3203168211198807973, 9817491932198370423]
    assert shuffled(["a", "b", "c", "d"], SplitMix64(9)) == shuffled(["a", "b", "c", "d"], SplitMix64(9))


# --------------------------------------------------------- player state ---
def test_player_state_round_trip(tmp_path):
    path = str(tmp_path / "player.json")
    st = playerstate.load(path)
    assert st.volume == 0.8 and st.liked == [] and st.eq == eq.default_state()
    st.set_liked("/m/b.flac", True), st.set_liked("/m/a.flac", True), st.set_liked("/m/b.flac", False)
    st.repeat, st.shuffle, st.volume = "stop", "genre", 0.5
    st.eq = eq.apply_preset(st.eq, "Jazz")
    playerstate.save(st, path)
    again = playerstate.load(path)
    assert again.liked == ["/m/a.flac"] and again.shuffle == "genre" and again.volume == 0.5
    assert again.repeat == "off"  # "stop after this song" is for the song playing, never the next start
    assert again.eq["graphic"]["preset"] == "Jazz"
    (tmp_path / "player.json").write_text("{broken", encoding="utf-8")
    assert playerstate.load(path).liked == []


def test_lyrics_size_is_kept_within_its_limits(tmp_path):
    """Now Playing's A− / A+: 16 to 48 px in steps of 2, 26 by default, saved in player.json."""
    path = str(tmp_path / "player.json")
    st = playerstate.load(path)
    assert st.lyrics_size == playerstate.LYRICS_SIZE_DEFAULT == 26
    assert st.step_lyrics_size(1) == 28 and st.step_lyrics_size(-3) == 22
    playerstate.save(st, path)
    assert json.loads((tmp_path / "player.json").read_text(encoding="utf-8"))["lyrics_size"] == 22
    assert playerstate.load(path).lyrics_size == 22
    for _ in range(30):
        st.step_lyrics_size(1)
    assert st.lyrics_size == playerstate.LYRICS_SIZE_MAX == 48
    assert st.step_lyrics_size(-100) == playerstate.LYRICS_SIZE_MIN == 16
    for saved, size in ((30, 30), ("34", 34), (12, 16), (99, 48), (27.5, 28), ("big", 26), (None, 26),
                        (float("nan"), 26), (float("inf"), 26)):
        assert playerstate.PlayerState.from_json({"lyrics_size": saved}).lyrics_size == size, saved
    assert playerstate.PlayerState.from_json({}).lyrics_size == 26  # a player.json of 2.0.0
    assert playerstate.plain_lyrics_size(26) == 18 and playerstate.plain_lyrics_size(48) == 34


# ------------------------------------------------------- embedded lyrics ---
SYNCED = "[ar: Band]\n[00:00.50] First line ♪\n[00:01.20][00:02.40] Twice\n[00:03.00] 日本語の行"
PLAIN = "First line ♪\nTwice\n日本語の行"


def _pcm(path):
    return hashlib.sha256(subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s32le", "-"],
                                         capture_output=True, check=True).stdout).hexdigest()


def _has_encoder(name):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return f" {name} " in out


@needs_ffmpeg
@pytest.mark.parametrize("name,args", [("a.flac", ["-c:a", "flac"]),
                                       ("b3.mp3", ["-c:a", "libmp3lame", "-id3v2_version", "3"]),
                                       ("b4.mp3", ["-c:a", "libmp3lame", "-id3v2_version", "4"])])
def test_lyrics_are_embedded_without_touching_the_audio(tmp_path, name, args):
    if "libmp3lame" in args and not _has_encoder("libmp3lame"):
        pytest.skip("no MP3 encoder")
    p = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-metadata", "title=曲名", "-metadata", "artist=Bänd", *args, str(p)], check=True)
    before = _pcm(p)
    embed.embed_lyrics(str(p), SYNCED, PLAIN)
    synced, plain = embed.embedded_lyrics(str(p))
    assert plain == PLAIN and _pcm(p) == before
    if name.endswith(".flac"):
        assert synced == SYNCED
    else:  # SYLT keeps the timed lines
        assert synced.splitlines() == ["[00:00.50] First line ♪", "[00:01.20] Twice", "[00:02.40] Twice",
                                       "[00:03.00] 日本語の行"]
    tags = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(p)],
                          capture_output=True, text=True, encoding="utf-8").stdout
    assert "曲名" in tags and "First line" in tags  # other tags kept, FFmpeg sees the lyrics
    embed.embed_lyrics(str(p), "", "")
    assert embed.embedded_lyrics(str(p)) == ("", "") and _pcm(p) == before
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".realhires")]


def test_lrc_entries():
    assert embed.lrc_entries(SYNCED) == [(500, "First line ♪"), (1200, "Twice"), (2400, "Twice"),
                                         (3000, "日本語の行")]
    assert embed.lrc_entries("[01:02.5]x\n[1:02.345] y") == [(62345, "y"), (62500, "x")]
    with pytest.raises(embed.EmbedError):
        embed.embed_lyrics(__file__, "x", "x")


# --------------------------------------------------------------- library ---
@needs_ffmpeg
def test_library_index(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    (music / "B").mkdir(parents=True)
    tags = [("01.flac", "Alpha", "1"), ("02.flac", "Beta", "2"), ("B/03.flac", "Gamma", "1")]
    for name, title, track in tags:
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-metadata", f"title={title}",
                        "-metadata", "artist=Band", "-metadata", f"album={'Two' if '/' in name else 'One'}",
                        "-metadata", "genre=Jazz", "-metadata", f"track={track}/9", "-ar", "96000",
                        "-sample_fmt", "s32", "-c:a", "flac", str(music / name)], check=True)
    from realhires.settings import Settings

    cfg = Settings()
    cfg.library_folders = [str(music)]
    lib = musiclib.Library(str(tmp_path / "library.sqlite"))
    files = musiclib.library_files(cfg)
    assert len(files) == 3
    tracks = lib.refresh(files)
    assert [t.title for t in musiclib.sort_songs(tracks)] == ["Alpha", "Beta", "Gamma"]
    t = next(t for t in tracks if t.title == "Beta")
    assert (t.artist, t.album, t.genre, t.track, t.sample_rate, t.bits) == ("Band", "One", "Jazz", 2, 96000, 24)
    assert musiclib.quality(t) == "Hi-Res Lossless" and musiclib.resolution(t) == "24-bit/96 kHz"
    assert [a["title"] for a in musiclib.albums(tracks)] == ["One", "Two"]
    assert [g["name"] for g in musiclib.genres(tracks)] == ["Jazz"]
    # nothing changed: nothing is read again; a removed file disappears
    calls = []
    monkeypatch.setattr("realhires.probe.probe", lambda p: calls.append(p))
    (music / "B" / "03.flac").unlink()
    assert len(lib.refresh(musiclib.library_files(cfg))) == 2 and calls == []
    lib.close()


def test_listening_is_counted_from_where_the_song_starts():
    # 2.4: the time before the first position update counts (a busy computer updates it
    # less often), steps over 1.5 s still count as seeks, as before
    import types

    pytest.importorskip("tkinter")
    from realhires.player_gui import PlayerApp

    fake = types.SimpleNamespace(listen=None, current="a.flac", engine=types.SimpleNamespace(current="a.flac"),
                                 _listen_end=lambda: None)
    PlayerApp._listen_start(fake, "a.flac", 0.0)
    for pos in (0.7, 1.2):
        PlayerApp._listen_tick(fake, pos)
    assert abs(fake.listen["seconds"] - 1.2) < 1e-9
    PlayerApp._listen_tick(fake, 5.0)  # a seek
    assert abs(fake.listen["seconds"] - 1.2) < 1e-9 and fake.listen["last"] == 5.0
    PlayerApp._listen_start(fake, "a.flac")  # no known start: from the first update, as before
    PlayerApp._listen_tick(fake, 0.7)
    assert fake.listen["seconds"] == 0.0
