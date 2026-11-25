"""Fixtures that synthesise real and fake hi-res files with ffmpeg."""

import functools
import subprocess

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def _saved_results_in_tmp(tmp_path, monkeypatch):
    """Never touch the user's saved results (store.py), library index, player state,
    themes, saved album covers (2.0) or custom tabs (2.3); subprocesses inherit the
    variables.  Tests that need their own files set them again."""
    monkeypatch.setenv("REALHIRES_RESULTS", str(tmp_path / "results.sqlite"))
    monkeypatch.setenv("REALHIRES_LIBRARY", str(tmp_path / "library.sqlite"))
    monkeypatch.setenv("REALHIRES_PLAYER", str(tmp_path / "player.json"))
    monkeypatch.setenv("REALHIRES_THEMES", str(tmp_path / "themes.json"))
    monkeypatch.setenv("REALHIRES_COVERS", str(tmp_path / "covers"))
    monkeypatch.setenv("REALHIRES_TABS", str(tmp_path / "tabs"))


def music_like(fs: int, seconds: float = 6.0, top_hz: float = None, seed: int = 0,
               order: int = 2) -> np.ndarray:
    """Stereo pink-ish noise with a smooth roll-off, standing in for music.

    Spectrum falls ~3 dB/octave (like real music) and, if `top_hz` is given,
    fades out gently above it (``order`` x 6 dB/octave) instead of stopping at a
    brick wall.
    """
    rng = np.random.default_rng(seed)
    n = int(fs * seconds)
    spec = rng.normal(size=(2, n // 2 + 1)) + 1j * rng.normal(size=(2, n // 2 + 1))
    f = np.fft.rfftfreq(n, 1 / fs)
    shape = 1 / np.sqrt(np.maximum(f, 20.0))
    if top_hz:
        shape *= 1 / (1 + (f / top_hz) ** order)
    x = np.fft.irfft(spec * shape, n=n, axis=1).T
    x *= 10 ** (-12 / 20) / np.max(np.abs(x))  # peak -12 dBFS
    return x


def converter(x: np.ndarray, fs: int, noise_dbfs: float = -115.0, seed: int = 9) -> np.ndarray:
    """What an analogue-to-digital converter running at `fs` adds: white
    thermal noise (`noise_dbfs` over a 20 kHz band) and a half-band style
    anti-alias filter, flat to 0.4535*fs and -6 dB at Nyquist."""
    rng = np.random.default_rng(seed)
    x = x + rng.normal(size=x.shape) * 0.707 * 10 ** (noise_dbfs / 20) * np.sqrt(fs / 2 / 20000)
    X = np.fft.rfft(x, axis=0)
    f = np.fft.rfftfreq(x.shape[0], 1 / fs) / fs
    g = np.clip((f - 0.4535) / (0.5465 - 0.4535), 0, 1)
    return np.fft.irfft(X * (0.5 + 0.5 * np.cos(np.pi * g))[:, None], n=x.shape[0], axis=0)


def ffmpeg_write(path, x: np.ndarray, fs: int, out_args):
    """Pipe float64 stereo samples into ffmpeg and encode with `out_args`."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "f64le", "-ar", str(fs), "-ac", str(x.shape[1]),
           "-i", "-", *out_args, str(path)]
    subprocess.run(cmd, input=np.ascontiguousarray(x, dtype="<f8").tobytes(), check=True)
    return str(path)


def ffmpeg_convert(src, dst, out_args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), *out_args, str(dst)], check=True)
    return str(dst)


@functools.lru_cache(maxsize=None)
def _ffmpeg_text(*args) -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", *args], capture_output=True, text=True).stdout


def mp3_encoder():
    """Name of an available MP3 encoder, or None (some FFmpeg builds have none)."""
    encoders = _ffmpeg_text("-encoders")
    for name in ("libmp3lame", "libshine"):
        if f" {name} " in encoders:
            return name
    return None


def hq_resample(rate: int) -> list:
    """High-quality resampling: soxr when FFmpeg has it, else a long swr filter."""
    if "--enable-libsoxr" in _ffmpeg_text("-buildconf"):
        return ["-af", f"aresample={rate}:resampler=soxr"]
    return ["-af", f"aresample={rate}:filter_size=128:cutoff=0.95:phase_shift=10"]


@pytest.fixture(scope="session")
def library(tmp_path_factory):
    """A small fake music library: name -> file path."""
    d = tmp_path_factory.mktemp("library")
    files = {}

    # Genuine 24/96 with ultrasonic content up to ~40 kHz.
    files["real_24_96"] = ffmpeg_write(d / "real_24_96.flac", music_like(96000, top_hz=40000), 96000,
                                       ["-c:a", "flac", "-sample_fmt", "s32"])
    # Genuine 24/192.
    files["real_24_192"] = ffmpeg_write(d / "real_24_192.wav", music_like(192000, 4, top_hz=70000),
                                        192000, ["-c:a", "pcm_s24le"])
    # Genuine 24/48 (hi-res by bit depth only).
    files["real_24_48"] = ffmpeg_write(d / "real_24_48.wav", music_like(48000), 48000, ["-c:a", "pcm_s24le"])

    # CD master (16/44.1) – standard resolution.
    cd = ffmpeg_write(d / "cd_16_44.wav", music_like(44100), 44100, ["-c:a", "pcm_s16le"])
    files["cd_16_44"] = cd

    # Fake: CD master upsampled to 24/96 (soxr, high quality).
    files["fake_up_44_to_96"] = ffmpeg_convert(cd, d / "fake_up_44_to_96.flac",
                                               [*hq_resample(96000), "-c:a", "flac", "-sample_fmt", "s32"])
    # Fake: CD master upsampled to 24/192 with ffmpeg's default resampler.
    files["fake_up_44_to_192"] = ffmpeg_convert(cd, d / "fake_up_44_to_192.wav",
                                                ["-ar", "192000", "-c:a", "pcm_s24le"])
    # Fake: 48 kHz master upsampled to 96 kHz.
    m48 = ffmpeg_write(d / "m48.wav", music_like(48000, seed=3), 48000, ["-c:a", "pcm_s16le"])
    files["fake_up_48_to_96"] = ffmpeg_convert(m48, d / "fake_up_48_to_96.wav",
                                               [*hq_resample(96000), "-c:a", "pcm_s24le"])
    # Fake: the same with ffmpeg's default (soft) resampler, which hides the cliff.
    files["fake_up_48_to_96_swr"] = ffmpeg_convert(m48, d / "fake_up_48_to_96_swr.wav",
                                                   ["-ar", "96000", "-c:a", "pcm_s24le"])

    # Fakes without any change of sample rate: a CD master turned into 24 bits
    # with a volume change (ffmpeg), with dither only, or into float with a gain.
    files["fake_gain_16_to_24"] = ffmpeg_convert(cd, d / "fake_gain_16_to_24.flac",
                                                 ["-af", "volume=-3dB", "-c:a", "flac", "-sample_fmt", "s32"])
    rng = np.random.default_rng(2)
    x16 = np.round(music_like(44100, seed=6) * 32768) / 32768
    dithered = np.round(x16 * 2 ** 23 + rng.random(x16.shape) - rng.random(x16.shape)) / 2 ** 23
    files["fake_dither_16_to_24"] = ffmpeg_write(d / "fake_dither_16_to_24.wav", dithered, 44100,
                                                 ["-c:a", "pcm_s24le"])
    files["fake_gain_16_to_float"] = ffmpeg_convert(cd, d / "fake_gain_16_to_float.wav",
                                                    ["-af", "volume=-1.5dB", "-c:a", "pcm_f32le"])

    # Genuine 96 kHz transfer of a dull analogue recording: nothing above
    # ~10 kHz but the converter's own noise floor, which reaches 43 kHz.
    files["analog_transfer_96"] = ffmpeg_write(
        d / "analog_transfer_96.wav",
        converter(music_like(96000, top_hz=4000, seed=8, order=8), 96000), 96000, ["-c:a", "pcm_s24le"])
    # Fake: a dull 48 kHz analogue transfer (converter noise included) upsampled to 96 kHz.
    dull48 = ffmpeg_write(d / "dull48.wav", converter(music_like(48000, top_hz=6000, seed=8, order=6), 48000),
                          48000, ["-c:a", "pcm_s24le"])
    files["fake_dull_up_48_to_96"] = ffmpeg_convert(dull48, d / "fake_dull_up_48_to_96.wav",
                                                    [*hq_resample(96000), "-c:a", "pcm_s24le"])
    # Fake: 96 kHz master upsampled to 192 kHz.
    m96 = ffmpeg_write(d / "m96.wav", music_like(96000, 4, seed=4), 96000, ["-c:a", "pcm_s24le"])
    files["fake_up_96_to_192"] = ffmpeg_convert(m96, d / "fake_up_96_to_192.wav",
                                                [*hq_resample(192000), "-c:a", "pcm_s24le"])

    # Fake: 16-bit audio zero-padded into a 24/96 container (full bandwidth!).
    s16 = ffmpeg_write(d / "s16_96.wav", music_like(96000, top_hz=40000, seed=5), 96000, ["-c:a", "pcm_s16le"])
    files["fake_padded_16_in_24"] = ffmpeg_convert(s16, d / "fake_padded_16_in_24.flac",
                                                   ["-c:a", "flac", "-sample_fmt", "s32"])
    # Fake: 16-bit audio in a 32-bit float container.
    files["fake_padded_16_in_float"] = ffmpeg_convert(s16, d / "fake_padded_16_in_float.wav",
                                                      ["-c:a", "pcm_f32le"])

    # Fake: MP3 decoded and upsampled to 24/96.
    # (Skipped by the tests when this FFmpeg build has no MP3 encoder.)
    enc = mp3_encoder()
    if enc:
        mp3 = ffmpeg_convert(cd, d / "src.mp3", ["-c:a", enc, "-b:a", "128k"])
        files["fake_from_mp3"] = ffmpeg_convert(mp3, d / "fake_from_mp3.flac",
                                                [*hq_resample(96000), "-c:a", "flac", "-sample_fmt", "s32"])
        # A lossy file is reported as such.
        files["lossy_mp3"] = mp3
        # The classic "fake FLAC": an MP3 decoded into a 16/44.1 FLAC.
        files["fake_flac_cd"] = ffmpeg_convert(mp3, d / "fake_flac_cd.flac",
                                               ["-c:a", "flac", "-sample_fmt", "s16"])

    # Ambiguous: a 96 kHz file whose content gently fades out well below 22 kHz.
    files["dull_96"] = ffmpeg_write(d / "dull_96.wav", music_like(96000, top_hz=4000, seed=7, order=8), 96000,
                                    ["-c:a", "pcm_s24le"])
    # Silence.
    files["silence"] = ffmpeg_write(d / "silence.wav", np.zeros((96000, 2)), 96000, ["-c:a", "pcm_s24le"])
    return files
