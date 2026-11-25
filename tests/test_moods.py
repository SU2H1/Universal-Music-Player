"""Moods, tempo and playlists (2.4, docs/player-spec.md "4. Moods, tempo and
playlists"): the analysis on invented songs (tests/mood_songs.py: clicks at known
tempos over major and minor chord progressions, loud and quiet), decoding through
FFmpeg, the labels' rules, the `moods` table of library.sqlite and analysing again when
a song changes, the background job, the cards and Create Playlist's names, the tab
fields bpm, mood and tempo, the BPM and Mood columns, the setting, and that the Rust
vectors are current.  The Tk pages are tested in test_moods_gui.py."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

import mood_songs
from realhires import moods, musiclib, playerstate, settings, smarttabs
from realhires.musiclib import Track
from realhires.store import settings_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

# what the invented songs are, as the analysis must find them
EXPECTED = {"happy": ("C", "major", "Up-tempo", "Energetic", "Happy"),
            "sad": ("A", "minor", "Slow", "Calm", "Sad"),
            "intense": ("A", "minor", "Up-tempo", "Energetic", "Intense"),
            "chill": ("C", "major", "Slow", "Calm", "Chill"),
            "mid": ("C", "major", "Mid-tempo", "Energetic", "Happy"),
            "fast": ("C", "major", "Up-tempo", "Energetic", "Happy")}


def samples(bpm, prog, secs, **kw):
    return mood_songs.song(bpm, prog, secs, **kw).astype(np.float64) / 32768.0


def test_invented_songs_get_their_tempo_key_and_labels():
    for name, bpm, prog, secs, gain in mood_songs.SONGS:
        m = moods.analyse_samples(samples(bpm, prog, secs, gain=gain))
        assert abs(m.bpm - bpm) <= 2, (name, m)
        assert (m.key, m.mode, m.tempo, m.energy_label, m.mood) == EXPECTED[name], (name, m)
        assert m.seconds == secs and m.brightness > 0 and m.key_strength >= moods.KEY_MARGIN
        assert m.summary() == f"{m.mood} · {m.tempo} · {m.bpm} BPM"


@pytest.mark.parametrize("prog", ["major", "minor"])
def test_tempo_within_two_bpm_from_slow_to_fast(prog):
    """Clicks from 56 to 200 BPM: the tempo within ±2 BPM (an even pulse at half the
    lag is the beat, so 174 is not taken for 87) and the mode right."""
    for bpm in range(56, 201, 12):
        m = moods.analyse_samples(samples(bpm, prog, 8))
        assert abs(m.bpm - bpm) <= 2 and m.mode == prog, (bpm, m)
        assert m.tempo == ("Up-tempo" if m.bpm >= 120 else "Slow" if m.bpm < 90 else "Mid-tempo")


def test_loud_and_quiet_songs():
    loud = moods.analyse_samples(samples(128, "major", 8))
    quiet = moods.analyse_samples(samples(128, "major", 8, gain=12))
    assert loud.energy > moods.ENERGETIC_DB + 3 > moods.ENERGETIC_DB - 3 > quiet.energy
    assert abs((loud.energy - quiet.energy) - 20 * np.log10(100 / 12)) < 0.3
    assert (loud.energy_label, quiet.energy_label) == ("Energetic", "Calm")
    # quieter but up-tempo is still lively; the same chords at 72 BPM and quiet are calm
    assert quiet.mood == "Happy"
    assert moods.analyse_samples(samples(72, "major", 8, gain=12)).mood == "Chill"


def test_noise_silence_and_short_audio():
    noise = moods.analyse_samples(mood_songs.lcg_noise(8, 6000).astype(np.float64) / 32768.0)
    assert noise.bpm == 0 and noise.tempo == "" and noise.mood  # no beat, but a mood
    for x in (np.zeros(22050 * 3), np.zeros(0), np.full(1000, 0.1)):
        m = moods.analyse_samples(x)
        assert (m.bpm, m.key, m.mode, m.tempo, m.energy_label, m.mood) == (0, "", "", "", "", ""), m
        assert m.labels() == {"tempo": "", "energy": "", "mood": ""}
    assert moods.analyse_samples(np.zeros(100)).energy == -200.0
    # chords without clicks have no clear beat
    assert moods.analyse_samples(samples(100, "major", 8, click=0)).bpm == 0


def test_the_rules_of_the_labels():
    assert [moods.tempo_label(b) for b in (0, 60, 89, 90, 119, 120, 200)] == [
        "", "Slow", "Slow", "Mid-tempo", "Mid-tempo", "Up-tempo", "Up-tempo"]
    assert (moods.energy_label(-16.0), moods.energy_label(-16.01)) == ("Energetic", "Calm")
    ml = moods.mood_label
    assert ml("major", 0.3, 500, "Up-tempo", "Calm") == "Happy"
    assert ml("major", 0.3, 500, "Mid-tempo", "Energetic") == "Happy"
    assert ml("major", 0.3, 500, "Slow", "Energetic") == "Chill"   # a loud ballad
    assert ml("minor", 0.3, 5000, "Mid-tempo", "Calm") == "Sad"
    assert ml("minor", 0.3, 500, "Up-tempo", "Calm") == "Intense"
    # an unclear key: the brightness decides
    assert ml("minor", 0.01, 3000, "Slow", "Calm") == "Chill"
    assert ml("major", 0.01, 800, "Up-tempo", "Energetic") == "Intense"
    assert moods.key_name("Eb", "major") == "Eb major" and moods.key_name("", "minor") == ""
    assert len(moods.KEY_NAMES) == 24 and len(set(moods.KEY_NAMES)) == 24
    # a C major triad and an A minor one
    assert moods.key_of([1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0])[:2] == (0, "major")
    assert moods.key_of([1, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0])[:2] == (9, "minor")
    # the constants the docstring names hold together
    assert moods.LAG_MIN == int(np.ceil(60 * moods.FPS / moods.MAX_BPM))
    assert moods.LAG_MAX == int(np.floor(60 * moods.FPS / moods.MIN_BPM))
    assert moods.TREND_FRAMES == round(moods.TREND_S * moods.FPS)


def test_the_stretch_and_the_command():
    assert moods.stretch(0) == (0, 90) and moods.stretch(60) == (0, 90)
    assert moods.stretch(100.9) == (10, 90) and moods.stretch(400) == (30, 90)
    cmd = moods.decode_command("/a/b.flac", 30, 90)
    assert cmd[1:] == ["-v", "error", "-nostdin", "-threads", "1", "-ss", "30", "-i", "/a/b.flac", "-map", "0:a:0",
                       "-vn", "-t", "90", "-ac", "2", "-ar", "22050", "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    assert "-ss" not in moods.decode_command("x", 0, 90)
    data = np.array([0.5, -0.25, 1.0, 1.0, 3.0], dtype="<f4").tobytes()
    assert list(moods.samples_of(data)) == [0.125, 1.0]  # a frame cut short is left out


def _flac(path, name, bpm, prog, secs, gain, sr=44100, channels=2, **tags):
    wav = str(path) + ".wav"
    mood_songs.write_wav(wav, mood_songs.song(bpm, prog, secs, sr=sr, gain=gain), sr=sr, channels=channels)
    meta = []
    for k, v in dict({"title": name}, **tags).items():
        meta += ["-metadata", f"{k}={v}"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav] + meta + [str(path)], check=True)
    os.remove(wav)
    return str(path)


@needs_ffmpeg
def test_songs_decoded_through_ffmpeg(tmp_path):
    """FLAC and WAV, stereo and mono, at 44.1 and 48 kHz: the same labels as from the
    samples; a long song is analysed from 30 s in, for 90 s; a file that is not audio
    raises MoodError with a sentence."""
    for name, bpm, prog, gain, sr, ch in (("happy", 128, "major", 100, 44100, 2), ("sad", 72, "minor", 12, 48000, 1),
                                          ("intense", 150, "minor", 100, 44100, 1)):
        p = _flac(tmp_path / f"{name}.flac", name, bpm, prog, 12, gain, sr=sr, channels=ch)
        m = moods.analyse_file(p, 12.0)
        assert abs(m.bpm - bpm) <= 2 and (m.key, m.mode, m.tempo, m.energy_label, m.mood) == EXPECTED[name], (name, m)
    wav = str(tmp_path / "long.wav")
    mood_songs.write_wav(wav, mood_songs.song(96, "minor", 130), sr=22050)
    t0 = time.perf_counter()
    m = moods.analyse_file(wav, 130.0)
    took = time.perf_counter() - t0
    assert abs(m.seconds - 90) < 0.01 and abs(m.bpm - 96) <= 2 and m.mode == "minor", m
    print(f"a 90 s stretch analysed in {took:.2f} s")
    bad = tmp_path / "not audio.flac"
    bad.write_text("nope")
    with pytest.raises(moods.MoodError, match="^The song could not be decoded: "):
        moods.analyse_file(str(bad))


@needs_ffmpeg
def test_the_table_analyses_a_song_once_until_it_changes(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    a = _flac(music / "a.flac", "A", 128, "major", 6, 100)
    b = _flac(music / "b.flac", "B", 72, "minor", 6, 12)
    lib = musiclib.Library(str(tmp_path / "library.sqlite"))
    tracks = lib.refresh([a, b])
    store = moods.MoodStore(str(tmp_path / "library.sqlite"))
    assert [t.path for t in store.todo(tracks)] == [a, b]
    seen = []
    assert moods.run_analysis(tracks, store, progress=lambda i, n, p, m: seen.append((i, n, p, m.mood)),
                              pause=0) == (2, 0)
    assert seen == [(1, 2, a, "Happy"), (2, 2, b, "Sad")]
    got = store.all()
    assert (got[a].bpm, got[a].mood, got[a].tempo, got[a].energy_label, got[a].key, got[a].mode) == (
        128, "Happy", "Up-tempo", "Energetic", "C", "major")
    assert got[b].mood == "Sad" and store.get(b).tempo == "Slow"
    # the row as it is kept (the Rust version reads and writes the same)
    row = sqlite_row(tmp_path / "library.sqlite", a)
    assert row[:3] == (a, os.path.getsize(a), os.stat(a).st_mtime_ns) and row[-1] == moods.ANALYSER_VERSION
    assert row[8] == '{"energy": "Energetic", "mood": "Happy", "tempo": "Up-tempo"}'
    # unchanged songs are not analysed again
    assert store.todo(tracks) == [] and moods.run_analysis(tracks, store, pause=0) == (0, 0)
    # a changed song is (the library index reads it again first)
    st = os.stat(b)
    os.utime(b, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    tracks = lib.refresh([a, b])
    assert [t.path for t in store.todo(tracks)] == [b]
    assert moods.run_analysis(tracks, store, pause=0) == (1, 0)
    # a new analyser analyses every song again; the old results are shown until then
    monkeypatch.setattr(moods, "ANALYSER_VERSION", moods.ANALYSER_VERSION + 1)
    assert len(store.todo(tracks)) == 2 and store.all()[a].mood == "Happy"
    assert store.forget() == 2 and store.all() == {}
    store.close()
    lib.close()


def sqlite_row(db, path):
    import sqlite3

    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT * FROM moods WHERE path = ?", (path,)).fetchone()
    finally:
        con.close()


def test_the_job_goes_on_after_a_bad_song_and_stops_when_asked(tmp_path):
    store = moods.MoodStore(str(tmp_path / "library.sqlite"))
    tracks = [Track(f"/m/{n}.flac", size=1, mtime_ns=n) for n in range(4)]
    calls = []

    def analyse(path, duration):
        calls.append(path)
        if path.endswith("1.flac"):
            raise moods.MoodError("The song could not be decoded: broken")
        if path.endswith("2.flac"):
            raise ValueError("anything else")
        return moods.Mood(bpm=100, tempo="Mid-tempo", energy_label="Calm", mood="Chill")

    progress = []
    assert moods.run_analysis(tracks, store, progress=lambda *a: progress.append((a[0], a[1], a[3] is not None)),
                              pause=0, analyse=analyse) == (2, 2)
    assert progress == [(1, 4, True), (2, 4, False), (3, 4, False), (4, 4, True)]
    # the failed songs are tried again next time, the others not
    assert [t.path for t in store.todo(tracks)] == ["/m/1.flac", "/m/2.flac"]
    stop = threading.Event()
    stop.set()
    calls.clear()
    assert moods.run_analysis(tracks, store, stop=stop, analyse=analyse) == (0, 0) and calls == []
    # a stop while it runs ends it after the song
    stop.clear()
    seen = []

    def stopping(path, duration):
        seen.append(path)
        stop.set()
        return moods.Mood(mood="Sad")
    assert moods.run_analysis(tracks, store, stop=stop, pause=0, analyse=stopping) == (1, 0) and len(seen) == 1
    store.close()


def test_cards_and_the_playlists_they_make():
    tracks = [Track("/m/z.flac", title="Zebra"), Track("/m/a.flac", title="apple"), Track("/m/k.flac", title="Kiwi"),
              Track("/m/n.flac", title="New")]
    table = {"/m/z.flac": moods.Mood(bpm=128, tempo="Up-tempo", energy_label="Energetic", mood="Happy"),
             "/m/a.flac": moods.Mood(bpm=130, tempo="Up-tempo", energy_label="Energetic", mood="Happy"),
             "/m/k.flac": moods.Mood(bpm=0, energy_label="Calm", mood="Sad")}
    assert moods.card_songs("mood", "Happy", tracks, table) == ["/m/a.flac", "/m/z.flac"]  # as All Songs sorts
    assert moods.card_songs("tempo", "Up-tempo", tracks, table) == ["/m/a.flac", "/m/z.flac"]
    assert moods.card_songs("tempo", "Slow", tracks, table) == []
    assert moods.card_songs("mood", "", tracks, table) == []
    counts = moods.card_counts(tracks, table)
    assert counts == {"Happy": 2, "Sad": 1, "Chill": 0, "Intense": 0, "Up-tempo": 2, "Mid-tempo": 0, "Slow": 0}
    assert moods.analysed_count(tracks, table) == 3
    assert [label for _, label in moods.CARDS] == list(moods.MOODS + moods.TEMPOS)
    assert moods.PLAYLIST_NAMES == ("Happy songs", "Sad songs", "Chill songs", "Intense songs", "Up-tempo songs",
                                    "Mid-tempo songs", "Slow songs")
    # Create Playlist makes an ordinary playlist; a taken name gets " 2"
    st = playerstate.PlayerState()
    first = st.create_playlist(moods.playlist_name("Sad"), ["/m/k.flac"], now=1.0)
    second = st.create_playlist(moods.playlist_name("Sad"), ["/m/k.flac"], now=2.0)
    assert [st.playlist(p)["name"] for p in (first, second)] == ["Sad songs", "Sad songs 2"]
    assert st.playlist(second)["songs"] == ["/m/k.flac"]
    assert (moods.bpm_text(None), moods.bpm_text(moods.Mood()), moods.bpm_text(moods.Mood(bpm=99))) == ("", "", "99")


def test_tab_fields_bpm_mood_and_tempo():
    assert smarttabs.kind_of("bpm") == "number" and smarttabs.kind_of("mood") == smarttabs.kind_of("tempo") == "text"
    assert smarttabs.VALUE_CHOICES["mood"] == moods.MOODS == smarttabs.MOOD_VALUES
    assert smarttabs.VALUE_CHOICES["tempo"] == moods.TEMPOS == smarttabs.TEMPO_VALUES
    assert [smarttabs.FIELD_LABELS[f] for f in ("bpm", "mood", "tempo")] == ["Tempo (BPM)", "Mood", "Tempo"]
    assert smarttabs.new_rule("mood") == {"field": "mood", "op": "contains", "value": "Happy"}
    assert smarttabs.change_field({"field": "quality", "op": "is", "value": "Lossy"}, "tempo")["value"] == "Up-tempo"
    tracks = [Track("/m/a.flac", title="A"), Track("/m/b.flac", title="B"), Track("/m/c.flac", title="C")]
    ctx = smarttabs.context(now=0, moods={"/m/a.flac": moods.Mood(bpm=128, mood="Happy", tempo="Up-tempo"),
                                          "/m/b.flac": moods.Mood(bpm=72, mood="Sad", tempo="Slow")})
    assert [smarttabs.field_value("bpm", t, ctx) for t in tracks] == [128, 72, 0]
    assert [smarttabs.field_value("mood", t, ctx) for t in tracks] == ["Happy", "Sad", ""]

    def songs(rules, **kw):
        tab = smarttabs.check_tab(dict(smarttabs.new_tab("T"), rules=rules, **kw))
        return smarttabs.tab_songs(tab, tracks, ctx)
    assert songs([{"field": "mood", "op": "is", "value": "Sad"}]) == ["/m/b.flac"]
    assert songs([{"field": "tempo", "op": "is", "value": "up-tempo"}]) == ["/m/a.flac"]
    assert songs([{"field": "bpm", "op": ">=", "value": 100}]) == ["/m/a.flac"]
    assert songs([], sort="bpm", descending=True) == ["/m/a.flac", "/m/b.flac", "/m/c.flac"]
    # without moods (a song not analysed yet) a rule on them matches nothing
    assert smarttabs.tab_songs(smarttabs.check_tab(dict(smarttabs.new_tab("T"), rules=[
        {"field": "mood", "op": "is", "value": "Happy"}])), tracks, smarttabs.context()) == []


def test_bpm_and_mood_columns_and_the_setting():
    assert playerstate.SONG_COLUMNS.index("bpm") == playerstate.SONG_COLUMNS.index("bit_rate") + 1
    assert playerstate.SONG_COLUMNS.index("mood") == playerstate.SONG_COLUMNS.index("bpm") + 1
    assert playerstate.normalize_columns("songs", ["mood", "bpm", "title"]) == ["title", "bpm", "mood"]
    assert "bpm" not in playerstate.DEFAULT_COLUMNS["songs"]  # chosen by the user
    info = {f["name"]: f for f in settings.Settings.field_info()}
    f = info["analyse_moods"]
    assert (f["group"], f["label"], f["kind"], f["default"]) == ("Library", "Analyse moods and tempo", "bool", True)
    names = list(info)
    assert names.index("analyse_moods") == names.index("lyrics_while_scanning") + 1
    cfg = settings.Settings()
    cfg.analyse_moods = False
    assert settings_key(cfg) == settings_key(settings.Settings())  # saved scan results stay valid
    rust = open(os.path.join(ROOT, "rust", "crates", "core", "src", "settings.rs"), encoding="utf-8").read()
    assert 'analyse_moods: bool = true, "Library", "Analyse moods and tempo"' in rust


def test_both_versions_keep_the_same_table():
    rust = open(os.path.join(ROOT, "rust", "crates", "core", "src", "moods.rs"), encoding="utf-8").read()
    table = re.search(r'const TABLE: &str = "(.*?)";', rust, re.S).group(1)
    assert re.sub(r"\\\s*", "", table) == moods._TABLE
    assert f"pub const ANALYSER_VERSION: i64 = {moods.ANALYSER_VERSION};" in rust
    for name in ("UPTEMPO_BPM", "SLOW_BPM", "ENERGETIC_DB", "KEY_MARGIN", "BRIGHT_HZ", "MIN_PULSE", "FASTER_RATIO",
                 "PRIOR_BPM", "LENGTH_S", "START_S", "SILENCE_DB"):
        value = getattr(moods, name)
        assert re.search(rf"pub const {name}: \w+ = {re.escape(repr(value))};", rust), name


def test_the_rust_vectors_are_current(tmp_path):
    out = tmp_path / "moods.json"
    subprocess.run([sys.executable, os.path.join(ROOT, "rust", "tools", "gen_mood_vectors.py"), str(out)],
                   check=True, capture_output=True)
    committed = os.path.join(ROOT, "rust", "crates", "core", "tests", "python_moods.json")
    # floats within rounding: another CPU or NumPy build gives other last digits (the Rust
    # test compares the features within a tolerance too); BPM, labels and texts exactly
    diff = _first_difference(json.loads(out.read_text(encoding="utf-8")), json.load(open(committed, encoding="utf-8")))
    assert diff is None, f"run rust/tools/gen_mood_vectors.py: {diff}"


def _first_difference(a, b, path="$"):
    """Where two JSON values differ (floats within 1e-6 relative), or None."""
    import math
    if isinstance(a, bool) or isinstance(b, bool):
        return None if a is b else f"{path}: {a!r} != {b!r}"
    if isinstance(a, float) or isinstance(b, float):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9):
            return None
        return f"{path}: {a!r} != {b!r}"
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return f"{path}: keys {sorted(set(a) ^ set(b))} differ"
        for k in a:
            d = _first_difference(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: {len(a)} items != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {a!r} != {b!r}"


@pytest.mark.skipif(sys.platform == "win32", reason="os.getpriority is POSIX only")
def test_the_analysis_worker_runs_below_normal_priority():
    # the Tk app analyses in a worker process (2.4), started with moods.lower_priority()
    import multiprocessing

    from realhires.pool import MultiProcessPool

    mine = os.getpriority(os.PRIO_PROCESS, 0)
    pool = MultiProcessPool(1, mp_context=multiprocessing.get_context("spawn"), initializer=moods.lower_priority)
    try:
        theirs = pool.submit(os.getpriority, os.PRIO_PROCESS, 0).result(timeout=60)
    finally:
        pool.shutdown()
    assert theirs == min(mine + 10, 19), (mine, theirs)
