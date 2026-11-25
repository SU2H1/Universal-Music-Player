"""The 2.0 playback engine (realhires/player.py), with a hand-pulled output and
miniaudio's NULL backend, so no sound hardware is needed."""

import shutil
import subprocess
import time

import numpy as np
import pytest

from realhires import eq, player
from realhires.player import ManualOutput, Player

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
pytestmark = needs_ffmpeg


def song(path, seconds=1.0, rate=44100, channels=2, seed=1, codec=("-c:a", "flac"), **tags):
    """Noise at -12 dBFS (different in every channel), encoded with `codec`."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(-0.25, 0.25, (int(rate * seconds), channels))
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "f64le", "-ar", str(rate), "-ac", str(channels), "-i", "-"]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    subprocess.run(cmd + [*codec, str(path)], input=x.astype("<f8").tobytes(), check=True)
    return str(path)


def decoded(path, fmt="f32le", dtype="<f4", extra=()):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), *extra, "-f", fmt, "-ac", "2", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype).reshape(-1, 2)


def pull_all(p, out, block=1000, limit=60.0):
    """Pull frames until the engine says the queue is finished; (frames, events)."""
    got, events, end = [], [], time.monotonic() + limit
    while time.monotonic() < end:
        got.append(out.pull(block))
        events += p.poll()
        if any(e[0] in ("finished", "error") for e in events):
            break
    return np.concatenate(got) if got else np.zeros((0, 2), np.float32), events


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_plays_exactly_the_decoded_samples_and_counts_what_is_heard(tmp_path):
    a = song(tmp_path / "a.flac", 1.0)
    out = ManualOutput()
    p = Player(out)
    p.set_volume(1.0)
    p.play(a)
    first = out.pull(4410)
    assert out.opened == [44100], "the device must open at the song's rate"
    ref = decoded(a)
    assert np.array_equal(first, ref[:4410])
    assert abs(p.position() - 0.1) < 1e-9 and p.playing and p.current == a
    # volume: gain = volume squared, applied after decoding
    p.set_volume(0.5)
    assert np.allclose(out.pull(1000), ref[4410:5410] * 0.25, atol=1e-7)
    p.set_volume(0.5, muted=True)
    assert not np.any(out.pull(1000)) and player.fmt_gain(p.gain, True) == "Muted"
    p.set_volume(1.0)
    # pause: nothing is handed over and the position stays
    p.pause()
    assert len(out.pull(1000)) == 0 and abs(p.position() - 6410 / 44100) < 1e-9
    p.resume()
    assert np.array_equal(out.pull(500), ref[6410:6910])
    # seek: FFmpeg restarts there, the old buffer is dropped
    p.seek(0.5)
    assert p.position() == pytest.approx(0.5)
    after = out.pull(2000)
    assert np.allclose(after, ref[22050:24050], atol=1e-6)
    assert p.position() == pytest.approx(0.5 + 2000 / 44100)
    rest, events = pull_all(p, out)
    assert events[-1] == ("finished", a) and not p.active
    assert p.position() == pytest.approx(1.0, abs=1e-6)
    p.stop()
    assert p.current is None
    p.close()


def test_gapless_songs_make_one_continuous_stream(tmp_path):
    a = song(tmp_path / "a.flac", 1.3, seed=1)
    b = song(tmp_path / "b.flac", 0.7, seed=2)
    out = ManualOutput()
    p = Player(out)
    p.set_volume(1.0)
    p.play(a)
    p.set_next(b)
    got, events = pull_all(p, out, block=777)
    ref = np.concatenate([decoded(a), decoded(b)])
    assert len(got) == len(ref) and np.array_equal(got, ref), "a gap or a lost sample between the songs"
    assert events == [("advanced", b), ("finished", b)]
    assert out.opened == [44100]  # one device for both
    p.close()


def test_repeat_song_loops_without_a_gap_and_gapless_off_stops(tmp_path):
    a = song(tmp_path / "a.flac", 0.4)
    out = ManualOutput()
    p = Player(out)
    p.set_volume(1.0)
    p.play(a)
    p.set_next(a)  # repeat song: the app sets the same song again whenever it starts
    got, starts = [], 0
    while sum(len(f) for f in got) < 3 * 17640:
        got.append(out.pull(500))
        for e in p.poll():
            assert e == ("advanced", a)
            starts += 1
            if starts < 2:
                p.set_next(a)
    got = np.concatenate(got)
    assert np.array_equal(got[:3 * 17640], np.tile(decoded(a), (3, 1))), "a gap between the repeats"
    assert starts == 2
    p.set_gapless(False)
    p.set_next(a)
    p.play(a)
    got, events = pull_all(p, out)
    assert len(got) == 17640 and events == [("finished", a)], "gapless off still chained the next song"
    p.close()


def test_next_song_at_another_rate_reopens_the_device(tmp_path):
    a = song(tmp_path / "a.flac", 0.5, rate=44100)
    b = song(tmp_path / "b.flac", 0.5, rate=48000, seed=3)
    out = ManualOutput()  # runs at any rate
    p = Player(out)
    p.set_volume(1.0)
    p.play(a)
    p.set_next(b)
    got, events = pull_all(p, out)
    assert out.opened == [44100, 48000]
    assert np.array_equal(got, np.concatenate([decoded(a), decoded(b)]))
    assert events == [("advanced", b), ("finished", b)]
    p.close()


def test_resamples_with_soxr_when_the_device_cannot_run_at_the_songs_rate(tmp_path):
    a = song(tmp_path / "a.flac", 1.0, rate=96000)
    out = ManualOutput(rates=[48000])
    p = Player(out)
    p.set_volume(1.0)
    p.play(a)
    got, events = pull_all(p, out)
    assert out.opened == [48000] and abs(len(got) - 48000) <= 64
    info = p.output_info()
    assert info["rate"] == 0 or info["rate"] == 48000
    want = "soxr" if player.has_soxr() else "FFmpeg's default resampler"
    assert player.resampling_label(96000, 48000, player.resampler_name()) == f"From 96 kHz to 48 kHz ({want})"
    assert player.resampling_label(96000, 96000, want) == "None: the device runs at the song's rate"
    assert player.output_label(48000) == "48 kHz, 32-bit float, stereo"
    p.close()


def test_mono_is_played_on_both_channels_at_full_level(tmp_path):
    m = song(tmp_path / "m.flac", 0.3, channels=1)
    out = ManualOutput()
    p = Player(out)
    p.set_volume(1.0)
    p.play(m)
    got, _ = pull_all(p, out)
    mono = subprocess.run(["ffmpeg", "-v", "error", "-i", m, "-f", "f32le", "-"], capture_output=True,
                          check=True).stdout
    mono = np.frombuffer(mono, "<f4")
    assert np.array_equal(got[:, 0], mono) and np.array_equal(got[:, 1], mono)
    p.close()


def test_equalizer_changes_the_samples_exactly_like_the_reference(tmp_path):
    a = song(tmp_path / "a.flac", 0.25)
    state = eq.apply_preset(eq.default_state(), "Rock")
    state["enabled"] = True
    state["parametric"] = {"enabled": True, "preamp": -2.0,
                           "bands": [{"type": "high_shelf", "freq": 6000.0, "gain": 3.0, "q": 0.8, "on": True}]}
    out = ManualOutput()
    p = Player(out)
    p.set_volume(1.0)
    p.set_eq(state)
    p.play(a)
    got, _ = pull_all(p, out)
    x = decoded(a, "f64le", "<f8")
    ref = x * 10 ** (eq.preamp_db(state) / 20)
    for c in eq.stages(state, 44100):
        bq = eq.Biquad(c, 2)
        ref = np.array([[bq.process(v, ch) for ch, v in enumerate(row)] for row in ref])
    assert len(got) == len(ref)
    assert np.max(np.abs(got - ref)) < 2e-6, "the EQ does not match the reference biquads"
    assert np.max(np.abs(got - x)) > 0.01, "the EQ did nothing"
    assert player.eq_summary(state).startswith("Graphic: Rock, parametric: 1 band (pre-amp −")
    assert player.eq_summary(eq.default_state()) == "Off"
    # turning it off while playing restarts FFmpeg at the position without the EQ
    p.play(a)
    out.pull(2205)
    gen = p._gen
    p.set_eq(eq.default_state())
    assert wait_for(lambda: p._eq_timer is None and p._gen > gen)
    assert p.position() == pytest.approx(0.05)
    clean = out.pull(2205)
    assert np.allclose(clean, x[2205:4410], atol=1e-6)
    p.close()


def test_unreadable_file_is_reported(tmp_path):
    bad = tmp_path / "broken.flac"
    bad.write_bytes(b"this is not audio" * 100)
    out = ManualOutput()
    p = Player(out)
    p.set_volume(1.0)
    p.play(str(bad))
    assert wait_for(lambda: p._events)
    (kind, path, message), = p.poll()
    assert kind == "error" and path == str(bad) and message.startswith("The file broken.flac ")
    assert message.endswith(".") and not p.active
    p.play(str(tmp_path / "gone.flac"))
    assert wait_for(lambda: p._events)
    assert p.poll()[0][2] == "The file gone.flac does not exist any more."
    p.close()


def test_a_failing_next_song_ends_the_stream_cleanly(tmp_path):
    a = song(tmp_path / "a.flac", 0.3)
    bad = tmp_path / "bad.flac"
    bad.write_bytes(b"\0" * 4000)
    out = ManualOutput()
    p = Player(out)
    p.set_volume(1.0)
    p.play(a)
    p.set_next(str(bad))
    got, events = pull_all(p, out)
    assert len(got) == 13230 and events == [("finished", a)]  # the app then tries bad.flac and reports it
    p.close()


def test_formatting_for_the_playback_info():
    assert player.fmt_gain(0.5) == "−6.0 dB" and player.fmt_gain(1.0) == "0.0 dB"
    assert player.fmt_khz(44100) == "44.1 kHz" and player.channels_label(6) == "6 channels"
    assert player.bit_depth_label(24, False, False) == "24-bit"
    assert player.bit_depth_label(32, True, False) == "32-bit float"
    assert player.bit_depth_label(0, False, True) == "—" and player.bit_rate_label(2304000) == "2,304 kbps"


def _null_backend():
    try:
        import miniaudio

        return miniaudio.Backend.NULL
    except ImportError:
        return None


@pytest.mark.skipif(_null_backend() is None, reason="miniaudio not installed")
def test_miniaudio_null_backend_plays_in_real_time(tmp_path):
    a = song(tmp_path / "a.flac", 0.8)
    out = player.MiniaudioOutput(backends=[_null_backend()])
    assert [d.name for d in out.devices()] == ["NULL Playback Device"]
    p = Player(out)
    p.set_volume(1.0)
    t0 = time.monotonic()
    p.play(a)
    assert wait_for(lambda: p.position() > 0.2, 5)
    assert p.output_info()["rate"] == 44100 and out.backend == "Null"
    events = []
    assert wait_for(lambda: events.extend(p.poll()) or events, 10)
    assert events == [("finished", a)] and time.monotonic() - t0 > 0.6, "not played in real time"
    p.close()


# ------------------------------------------------------------ bit-perfect (2.1) ---
def int_song(path, seconds, rate, bits, channels=2, seed=5):
    """Noise at -6 dBFS as a `bits`-bit FLAC (16 or 24)."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(-0.5, 0.5, (int(rate * seconds), channels))
    fmt = ["-sample_fmt", "s16"] if bits == 16 else ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f64le", "-ar", str(rate), "-ac", str(channels), "-i", "-",
                    "-c:a", "flac", *fmt, str(path)], input=x.astype("<f8").tobytes(), check=True)
    return str(path)


def ints(path, bits):
    """What FFmpeg decodes the file to as integers (s16le, or s32le with 24-bit files left-justified)."""
    fmt, dtype = ("s16le", "<i2") if bits == 16 else ("s32le", "<i4")
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", fmt, "-"], capture_output=True,
                         check=True).stdout
    return np.frombuffer(out, dtype)


def rock():
    state = eq.apply_preset(eq.default_state(), "Rock")
    state["enabled"] = True
    return state


@pytest.mark.parametrize("bits,rate,channels,fmt", [(16, 44100, 2, "i16"), (24, 96000, 2, "i32"),
                                                   (16, 48000, 1, "i16")])
def test_bit_perfect_flacs_reach_the_device_as_the_files_integers(tmp_path, bits, rate, channels, fmt):
    path = int_song(tmp_path / f"s{bits}_{channels}.flac", 0.6, rate, bits, channels)
    out = ManualOutput()  # takes every rate and format
    p = Player(out)
    p.set_volume(0.5)     # neither the volume ...
    off = rock()
    off["enabled"] = False
    p.set_eq(off)         # ... nor the equalizer's settings while it is off may touch the samples
    p.set_bit_perfect(True)
    assert p.gain == 1.0 and p.bit_perfect
    p.play(path)
    got, events = pull_all(p, out, block=999)
    assert events[-1] == ("finished", path)
    assert out.opened == [rate] and out.opened_formats == [fmt] and out.exclusive_opened == [True]
    want = ints(path, bits)
    dtype = np.int16 if fmt == "i16" else np.int32
    assert got.dtype == dtype
    if channels == 1:
        assert np.array_equal(got[:, 0], want) and np.array_equal(got[:, 1], want), "mono not copied exactly"
    else:
        assert np.array_equal(got.reshape(-1), want), "the samples were changed on the way"
    p.play(path)
    out.pull(1000)
    info = p.output_info()
    assert info["bit_perfect"] == f"Yes: {bits}-bit, {player.fmt_khz(rate)}, Test output"
    assert info["format"] == ("16-bit integer" if bits == 16 else "24-bit integer in 32-bit")
    assert player.output_label(info["rate"], info["channels"], info["format"]).startswith(player.fmt_khz(rate))
    # the saved volume and equalizer are kept for when the setting is off again
    assert p.eq_state["graphic"]["preset"] == "Rock" and p.set_bit_perfect(False) is None and p.gain == 0.25
    p.close()


def eq_reference(path, state, rate):
    """What the engine's FFmpeg decodes with the equalizer: float32 stereo (FFmpeg runs the
    biquads in double precision, then hands over floats)."""
    chain = player.filter_chain(player.SongInfo(path, rate, 2, 1.0, 16), rate, state)
    return decoded(path, extra=("-af", chain))


@pytest.mark.parametrize("bits,rate,fmt", [(16, 44100, "i16"), (24, 96000, "i32")])
def test_bit_perfect_with_the_equalizer_on_applies_it_with_dither(tmp_path, bits, rate, fmt):
    """2.3: the equalizer is no longer bypassed: the device still runs at the song's rate in
    its integer format, the volume stays unused, and the changed samples get TPDF dither of
    ±1 LSB before they are rounded; turned off, the output is the file's integers again."""
    path = int_song(tmp_path / f"e{bits}.flac", 1.0, rate, bits)
    out = ManualOutput()
    p = Player(out)
    p.set_volume(0.5)
    state = rock()
    p.set_eq(state)
    p.set_bit_perfect(True)
    assert p.gain == 1.0
    p.play(path)
    got, events = pull_all(p, out, block=999)
    assert events[-1] == ("finished", path)
    assert out.opened == [rate] and out.opened_formats == [fmt] and out.exclusive_opened == [True]
    want = ints(path, bits).reshape(-1, 2)
    assert got.shape == want.shape and not np.array_equal(got.astype(np.int64), want), "the equalizer did nothing"
    top = float(1 << (15 if fmt == "i16" else 31))
    ref = eq_reference(path, state, rate).astype(np.float64) * top
    assert len(ref) == len(got)
    diff = got.astype(np.float64) - ref
    inside = np.abs(ref) < top - 2  # no clamping there
    assert inside.mean() > 0.99 and np.max(np.abs(diff[inside])) < 1.5, "more than the dither and rounding"
    if fmt == "i16":  # TPDF: about half the samples land on another integer than plain rounding gives
        moved = np.mean(got != np.rint(ref))
        assert 0.2 < moved < 0.7, moved
        assert abs(np.mean(diff[inside])) < 0.05, "the dither is not centred"
    p.play(path)
    out.pull(1000)
    info = p.output_info()
    khz = player.fmt_khz(rate)
    assert info["bit_perfect"] == f"No: The equalizer is on, so the samples are changed. The device still runs at {khz}."
    assert info["rate"] == rate and info["sample_format"] == fmt and info["gain"] == 1.0
    # the equalizer off while playing: FFmpeg restarts at the position, the samples are exact again
    gen = p._gen
    off = dict(state, enabled=False)
    p.set_eq(off)
    assert wait_for(lambda: p._eq_timer is None and p._gen > gen)
    pos = int(round(p.position() * rate))
    clean = out.pull(2000)
    assert np.array_equal(clean.astype(np.int64), want[pos:pos + 2000]), "not bit-exact with the equalizer off"
    assert p.output_info()["bit_perfect"].startswith("Yes: ")
    p.close()


def test_bit_perfect_equalizer_on_a_float_device_is_not_dithered(tmp_path):
    """A device that takes floats (macOS) gets the equalizer's float32 output as it is."""
    path = int_song(tmp_path / "f.flac", 0.5, 48000, 24)
    out = ManualOutput(formats=["f32"])
    p = Player(out)
    state = rock()
    p.set_eq(state)
    p.set_bit_perfect(True)
    p.play(path)
    got, _ = pull_all(p, out)
    assert out.opened_formats == ["f32"] and got.dtype == np.float32
    assert np.array_equal(got, eq_reference(path, state, 48000))
    p.close()


def test_bit_perfect_decodes_without_ar_or_filters():
    cmd = player.decode_command("x.flac", 0.0, 96000, None, native=True)
    assert "-ar" not in cmd and "-af" not in cmd and cmd[-1] == "pipe:1"
    mono = player.SongInfo("m.flac", 48000, 1, 1.0, 16)
    assert player.filter_chain(mono, 48000, None) == "pan=stereo|c0=c0|c1=c0"
    assert "-ar" in player.decode_command("x.flac", 0.0, 48000, None)


def test_exact_float_to_integer_conversion():
    x = np.array([[0.0, -1.0], [1.0, 0.5], [-0.25, 32767 / 32768], [np.nan, 2.0]], np.float32)
    assert player.device_samples(x, "i16").tolist() == [[0, -32768], [32767, 16384], [-8192, 32767], [0, 32767]]
    assert player.device_samples(x, "i32")[1].tolist() == [2147483647, 1073741824]
    for fmt in ("i16", "i24", "i32"):
        flat = [bp_int(v, fmt) for v in x.reshape(-1)]
        assert player.device_samples(x, fmt).reshape(-1).tolist() == flat
    assert player.device_bytes(player.device_samples(x[:1], "i24"), "i24") == b"\0\0\0\0\0\x80"
    wide = player.device_samples(x[:2], "f32", channels=4)
    assert wide.shape == (2, 4) and not wide[:, 2:].any()


def bp_int(v, fmt):
    from realhires import bitperfect

    return bitperfect.int_sample(float(v), fmt)


def test_bit_perfect_resamples_when_the_device_lacks_the_rate(tmp_path):
    path = int_song(tmp_path / "hi.flac", 0.5, 96000, 24)
    out = ManualOutput(rates=[44100, 48000])
    p = Player(out)
    p.set_bit_perfect(True)
    p.play(path)
    got, _ = pull_all(p, out)
    assert out.opened == [48000] and abs(len(got) - 24000) <= 64
    p.play(path)
    out.pull(500)
    assert p.output_info()["bit_perfect"] == "No: The device cannot play 96 kHz, so the song is resampled to 48 kHz."
    p.close()


def test_bit_perfect_joins_songs_gaplessly_only_at_the_same_rate_and_format(tmp_path):
    # b and c last longer than the 0.35 s buffer, so each is still being decoded when its
    # "advanced" event is read and the test names the song after it (a song that fits in
    # the buffer is decoded to its end before it is heard, and on a slow runner its end
    # came before the test's set_next())
    a = int_song(tmp_path / "a.flac", 0.4, 44100, 16, seed=1)
    b = int_song(tmp_path / "b.flac", 1.0, 44100, 16, seed=2)
    c = int_song(tmp_path / "c.flac", 1.0, 48000, 16, seed=3)
    d = int_song(tmp_path / "d.flac", 0.3, 48000, 24, seed=4)
    out = ManualOutput()
    p = Player(out)
    p.set_bit_perfect(True)
    p.play(a)
    p.set_next(b, after=a)
    got, events = [], []
    order = {a: b, b: c, c: d}
    while not any(e[0] in ("finished", "error") for e in events):
        got.append(out.pull(800))
        for e in p.poll():
            events.append(e)
            if e[0] == "advanced" and e[1] in order:
                p.set_next(order[e[1]], after=e[1])
    assert [e for e in events] == [("advanced", b), ("advanced", c), ("advanced", d), ("finished", d)]
    # a and b share one device; c needs 48 kHz, d a 32-bit format: the device is reopened each time
    assert out.opened == [44100, 48000, 48000] and out.opened_formats == ["i16", "i16", "i32"]
    first = np.concatenate([g for g in got if g.dtype == np.int16])
    assert np.array_equal(first.reshape(-1)[:len(ints(a, 16)) + len(ints(b, 16))],
                          np.concatenate([ints(a, 16), ints(b, 16)])), "a gap between a and b"
    p.close()


def test_a_busy_device_plays_the_normal_way_and_says_why(tmp_path):
    path = int_song(tmp_path / "s.flac", 0.3, 44100, 16)
    out = ManualOutput(busy=True)
    p = Player(out)
    p.set_volume(0.5)
    p.set_bit_perfect(True)
    p.play(path)
    first = out.pull(1000)
    assert out.opened_formats == ["f32"] and out.exclusive_opened == [False]
    assert first.dtype == np.float32 and np.array_equal(first, decoded(path)[:1000]), "gain 1, no EQ even so"
    assert p.output_info()["bit_perfect"] == ("No: The device is in use by another program, so the sound is "
                                               "mixed by the system.")
    p.close()


def test_turning_bit_perfect_on_restarts_the_song_at_its_position(tmp_path):
    path = int_song(tmp_path / "s.flac", 1.0, 44100, 16)
    out = ManualOutput()
    p = Player(out)
    p.set_volume(1.0)
    p.play(path)
    out.pull(4410)
    assert out.opened_formats == ["f32"] and p.output_info()["bit_perfect"] == "Off"
    p.set_bit_perfect(True)
    assert p.position() == pytest.approx(0.1)
    got = out.pull(2000)
    assert out.opened_formats == ["f32", "i16"] and np.array_equal(got.reshape(-1), ints(path, 16)[8820:12820])
    p.set_bit_perfect(False)
    assert out.pull(100).dtype == np.float32 and out.opened_formats[-1] == "f32"
    p.close()


def test_bit_perfect_lossy_song_says_it_is_decoded(tmp_path):
    try:
        path = song(tmp_path / "l.m4a", 0.4, rate=44100, codec=("-c:a", "aac", "-b:a", "128k"))
    except subprocess.CalledProcessError:
        pytest.skip("no AAC encoder")
    out = ManualOutput()
    p = Player(out)
    p.set_bit_perfect(True)
    p.play(path)
    out.pull(500)
    assert out.opened_formats == ["f32"]
    assert p.output_info()["bit_perfect"] == "Yes (decoded lossy audio): 32-bit float, 44.1 kHz, Test output"
    p.close()


@pytest.mark.skipif(_null_backend() is None, reason="miniaudio not installed")
def test_bit_perfect_through_miniaudio_builds_the_device_in_the_songs_format(tmp_path):
    path = int_song(tmp_path / "s24.flac", 0.6, 96000, 24)
    out = player.MiniaudioOutput(backends=[_null_backend()])
    p = Player(out)
    p.set_bit_perfect(True)
    p.play(path)
    assert wait_for(lambda: p.position() > 0.2, 5)
    info = p.output_info()
    assert info["rate"] == 96000 and info["sample_format"] == "i32" and info["format"] == "24-bit integer in 32-bit"
    assert info["bit_perfect"] == "Yes: 24-bit, 96 kHz, NULL Playback Device"
    assert out._device.format.name == "SIGNED32" and out._device.nchannels == 2
    events = []
    assert wait_for(lambda: events.extend(p.poll()) or events, 10)
    assert events == [("finished", path)]
    p.close()


def test_windows_note_when_the_exclusive_format_is_fixed(tmp_path):
    """The Python app on Windows gets an exclusive stream only in the device's Default Format
    (miniaudio 0.11), so a song in another rate says where that format is set."""
    from realhires import bitperfect

    class FixedFormat(ManualOutput):
        def exclusive(self, device):
            ex = super().exclusive(device)
            ex.note = bitperfect.WASAPI_FORMAT_NOTE
            return ex

    path = int_song(tmp_path / "s.flac", 0.3, 44100, 16)
    out = FixedFormat(rates=[48000], formats=["i32"])
    p = Player(out)
    p.set_bit_perfect(True)
    p.play(path)
    out.pull(500)
    assert p.output_info()["bit_perfect"] == ("No: The device cannot play 44.1 kHz, so the song is resampled to "
                                               "48 kHz. " + bitperfect.WASAPI_FORMAT_NOTE)
    p.close()
