"""Moods, tempo and key (2.4, docs/player-spec.md "4. Moods, tempo and playlists").

Each song of the library is analysed once on this computer, from its audio: FFmpeg
decodes at most `LENGTH_S` seconds of it (from `START_S` seconds in, or as late as a
whole `LENGTH_S` stretch still fits, or all of a song shorter than that) as two channels
at `RATE` Hz (`decode_command()`, below normal priority), which are averaged to mono, and
NumPy measures four things in about a tenth of a second (a 90 s stretch, decoding
included, takes about 0.35 s):

* **tempo** (BPM): a spectral-flux onset envelope (`ONSET_FFT`-point frames every
  `ONSET_HOP` samples, log-compressed magnitudes, the rise summed over the bins, the
  local mean of `TREND_S` seconds taken off), its autocorrelation over the lags of
  `MIN_BPM`..`MAX_BPM`, weighted by a log-normal tempo prior around `PRIOR_BPM` with a
  width of `PRIOR_OCTAVES` octaves; the strongest weighted peak wins, if it lies at
  least `MIN_PULSE` above the median of the lags searched (noise has no beat).  A peak at half
  that lag that is at least `FASTER_RATIO` as strong means the beat is twice as fast
  (the prior alone prefers 87 to 174 BPM for an even pulse), and the lag is refined with
  a parabola through the peak and through its peaks at 2, 3 and 4 times the lag
  (`MULTIPLES`, those at least `MULTIPLE_KEEP` as strong); rounded to a whole BPM.
* **energy**: the RMS level of the mono stretch in dBFS (a full-scale square wave
  reads 0 dB, a full-scale sine −3 dB; silence −200).
* **brightness**: the spectral centroid of the average power spectrum, in Hz.
* **key**: a chroma vector (the average magnitude spectrum of `CHROMA_FFT`-point
  frames, each bin from `CHROMA_MIN_HZ` to `CHROMA_MAX_HZ` added to the nearest pitch
  class) correlated with the 24 rotations of the Krumhansl-Schmuckler major and minor
  key profiles; the best one gives the key and its mode, and `key_strength` is how far
  the best major and the best minor correlation lie apart.

From these come the labels (estimates, as the apps say):

* tempo: **Up-tempo** from `UPTEMPO_BPM` BPM, **Slow** below `SLOW_BPM`, else
  **Mid-tempo**;
* energy: **Energetic** from `ENERGETIC_DB` dBFS, else **Calm**;
* mood, from a valence and an arousal: the valence is the mode (major is positive), or,
  when the key is unclear (`key_strength` below `KEY_MARGIN`), the brightness (from
  `BRIGHT_HZ` positive); the arousal is high when Energetic counts 1, Up-tempo 1 and
  Slow −1 add up to at least 1.  Positive and high: **Happy**; positive and low:
  **Chill**; negative and high: **Intense**; negative and low: **Sad**.

A stretch quieter than `SILENCE_DB` or shorter than `MIN_SECONDS` gets no labels.

Results are kept in the table `moods` of `library.sqlite` (`MoodStore`), keyed by the
path, size and modification time, so a song is analysed once until it changes, and
`ANALYSER_VERSION` makes every song be analysed again when the analysis changes.
`run_analysis()` is the background job of both apps.  The Rust version
(`rust/crates/core/src/moods.rs`) gives the same labels and BPM (vectors
`python_moods.json`).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

ANALYSER_VERSION = 1

# ---- the stretch that is analysed --------------------------------------------
RATE = 22050          # Hz, mono
START_S = 30          # seconds into the song
LENGTH_S = 90         # seconds analysed at most
MIN_SECONDS = 1.0     # shorter audio gets no labels
SILENCE_DB = -60.0    # quieter audio gets no labels

# ---- tempo -------------------------------------------------------------------
ONSET_FFT = 1024
ONSET_HOP = 256
FPS = RATE / ONSET_HOP  # onset frames per second (86.13)
LOG_GAIN = 1000.0     # log(1 + LOG_GAIN * magnitude), magnitudes normalised to a full-scale sine = 1
TREND_S = 0.5         # the local mean taken off the onset envelope
TREND_FRAMES = 43     # TREND_S in onset frames
MIN_BPM, MAX_BPM = 50.0, 210.0
LAG_MIN = 25          # ceil(60 * FPS / MAX_BPM): the shortest beat period searched, in onset frames
LAG_MAX = 103         # floor(60 * FPS / MIN_BPM): the longest
PRIOR_BPM = 120.0
PRIOR_OCTAVES = 1.0
MIN_PULSE = 0.05     # the best peak must lie this far above the median of the lags searched
FASTER_RATIO = 0.8
MULTIPLES = 4
MULTIPLE_KEEP = 0.5
FRAMES_PER_BLOCK = 512  # STFT frames computed at once (bounded memory, short calls)

# ---- key ---------------------------------------------------------------------
CHROMA_FFT = 8192
CHROMA_HOP = 4096
CHROMA_MIN_HZ = 65.0
CHROMA_MAX_HZ = 2100.0
# Krumhansl & Kessler (1982), from the tonic up
MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)
PITCH_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")

# ---- labels ------------------------------------------------------------------
UPTEMPO_BPM = 120
SLOW_BPM = 90
ENERGETIC_DB = -16.0
KEY_MARGIN = 0.05
BRIGHT_HZ = 1500.0

TEMPOS = ("Up-tempo", "Mid-tempo", "Slow")
ENERGIES = ("Energetic", "Calm")
MOODS = ("Happy", "Sad", "Chill", "Intense")
MODES = ("major", "minor")


class MoodError(Exception):
    """The song could not be decoded (the message says why)."""


@dataclass
class Mood:
    """What the analysis found in a song (`labels` empty for silence or very short audio)."""

    bpm: int = 0
    energy: float = -200.0       # dBFS (-200 for silence)
    brightness: float = 0.0      # Hz
    key: str = ""                # "A" (one of PITCH_NAMES), "" when unknown
    mode: str = ""               # "major" / "minor"
    key_strength: float = 0.0
    tempo: str = ""              # one of TEMPOS
    energy_label: str = ""       # one of ENERGIES
    mood: str = ""               # one of MOODS
    seconds: float = 0.0         # how much audio was analysed

    def labels(self) -> Dict[str, str]:
        return {"tempo": self.tempo, "energy": self.energy_label, "mood": self.mood}



    def summary(self) -> str:
        """"Happy · Up-tempo · 128 BPM" (English; the apps translate the parts)."""
        return " · ".join(x for x in (self.mood, self.tempo, f"{self.bpm} BPM" if self.bpm else "") if x)


# ------------------------------------------------------------------- decoding ---
def stretch(duration: float) -> Tuple[int, int]:
    """(start, length) in whole seconds of the part of a song that is analysed: from
    START_S seconds in, or as late as a LENGTH_S stretch still fits, or the whole song."""
    d = int(duration) if duration and duration > 0 else 0
    return max(0, min(START_S, d - LENGTH_S)), LENGTH_S


def decode_command(path: str, start: int, length: int) -> List[str]:
    """The FFmpeg command for the stretch: two channels (a mono song twice, more channels
    mixed down by FFmpeg) of 32-bit floats at RATE Hz, which `decode()` averages."""
    from .probe import tool

    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-threads", "1"]
    if start > 0:
        cmd += ["-ss", str(start)]
    return cmd + ["-i", path, "-map", "0:a:0", "-vn", "-t", str(length), "-ac", "2", "-ar", str(RATE),
                  "-f", "f32le", "-acodec", "pcm_f32le", "-"]


def _low_priority(cmd: List[str]) -> Tuple[List[str], dict]:
    """The command run below normal priority: through `nice` on macOS and Linux, in
    the below-normal priority class on Windows."""
    from .probe import _no_window

    kw = _no_window()
    if sys.platform == "win32":
        kw["creationflags"] = kw.get("creationflags", 0) | 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
        return cmd, kw
    nice = shutil.which("nice", path=kw.get("env", {}).get("PATH") or os.defpath)
    return ([nice, "-n", "10"] + cmd if nice else cmd), kw


def lower_priority() -> None:
    """Put the calling process below normal priority, like its FFmpeg (the Tk app's
    analysis worker process calls it when it starts), so background analysis never
    takes the CPU from the window or the sound.  Failing to change it changes nothing."""
    try:
        if sys.platform == "win32":
            import ctypes

            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:  # noqa: BLE001
        pass


def samples_of(data: bytes) -> np.ndarray:
    """The mono samples (float64) of FFmpeg's two-channel f32le output: (left + right) / 2."""
    n = len(data) // 8
    pairs = np.frombuffer(data[:n * 8], dtype="<f4").astype(np.float64).reshape(-1, 2)
    return (pairs[:, 0] + pairs[:, 1]) * 0.5


def decode(path: str, duration: float = 0.0) -> np.ndarray:
    """The analysed stretch of a song as mono samples at RATE Hz (MoodError)."""
    start, length = stretch(duration)
    cmd, kw = _low_priority(decode_command(path, start, length))
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL, **kw)
        except OSError as exc:
            raise MoodError(f"FFmpeg could not be started: {exc}") from exc
        if proc.returncode != 0:
            err.seek(0)
            msg = err.read().decode("utf-8", "replace").strip()
            raise MoodError(f"The song could not be decoded: {msg or f'exit code {proc.returncode}'}")
    return samples_of(proc.stdout)


# ------------------------------------------------------------------- analysis ---
def _spectra(x: np.ndarray, nfft: int, hop: int, window: np.ndarray) -> Iterable[np.ndarray]:
    """The magnitude spectra of the frames of x (frame i starts at i * hop; no padding),
    FRAMES_PER_BLOCK frames at a time."""
    count = 1 + (len(x) - nfft) // hop if len(x) >= nfft else 0
    for b in range(0, count, FRAMES_PER_BLOCK):
        m = min(count, b + FRAMES_PER_BLOCK) - b
        frames = np.lib.stride_tricks.as_strided(x[b * hop:], shape=(m, nfft), strides=(x.strides[0] * hop, x.strides[0]),
                                                 writeable=False)
        yield np.abs(np.fft.rfft(frames * window, axis=-1))


def onset_envelope(x: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """(onset envelope, average power spectrum) of the onset frames: for each frame
    after the first, the sum over the bins of the rise of log(1 + LOG_GAIN * |X|) since
    the frame before (magnitudes scaled so that a full-scale sine reads 1), with the
    mean of the TREND_S seconds around it taken off and negative values set to 0."""
    w = np.hanning(ONSET_FFT)
    scale = 2.0 / w.sum()
    flux: List[np.ndarray] = []
    power = np.zeros(ONSET_FFT // 2 + 1)
    prev = None
    n = 0
    for mag in _spectra(x, ONSET_FFT, ONSET_HOP, w):
        mag = mag * scale
        power += (mag * mag).sum(axis=0)
        n += len(mag)
        comp = np.log1p(LOG_GAIN * mag)
        rows = comp if prev is None else np.vstack([prev[None, :], comp])
        flux.append(np.maximum(np.diff(rows, axis=0), 0.0).sum(axis=1))
        prev = comp[-1]
    env = np.concatenate(flux) if flux else np.zeros(0)
    if len(env) >= TREND_FRAMES:
        env = np.maximum(env - np.convolve(env, np.ones(TREND_FRAMES) / TREND_FRAMES, mode="same"), 0.0)
    return env, power / max(n, 1)


def autocorrelation(env: np.ndarray, max_lag: int) -> np.ndarray:
    """r[l] = Σ env[t]·env[t+l] / Σ env² for l = 0..max_lag (0 beyond the envelope)."""
    r = np.zeros(max_lag + 1)
    e0 = float(np.dot(env, env))
    if e0 <= 0:
        return r
    for lag in range(min(max_lag, len(env) - 1) + 1):
        r[lag] = float(np.dot(env[:len(env) - lag], env[lag:])) / e0
    return r


def median(v) -> float:
    return float(np.median(np.asarray(v, dtype=np.float64)))


def prior(lag: float) -> float:
    """The tempo prior's weight for an autocorrelation lag (in onset frames)."""
    bpm = 60.0 * FPS / lag
    return math.exp(-0.5 * (math.log2(bpm / PRIOR_BPM) / PRIOR_OCTAVES) ** 2)


def _is_peak(r: np.ndarray, lag: int) -> bool:
    return 0 < lag < len(r) - 1 and r[lag] > 0 and r[lag] >= r[lag - 1] and r[lag] >= r[lag + 1]


def _parabola(r: np.ndarray, lag: int) -> float:
    """The lag of the top of the parabola through r[lag-1..lag+1] (within ±0.5)."""
    a, b, c = float(r[lag - 1]), float(r[lag]), float(r[lag + 1])
    den = a - 2.0 * b + c
    if den >= 0:
        return float(lag)
    return lag + min(0.5, max(-0.5, 0.5 * (a - c) / den))


def _peak_near(r: np.ndarray, centre: float) -> int:
    """The highest peak of r at the whole lag nearest to `centre` or one beside it (0: none)."""
    c = int(math.floor(centre + 0.5))
    best = 0
    for lag in (c - 1, c, c + 1):
        if _is_peak(r, lag) and (best == 0 or r[lag] > r[best]):
            best = lag
    return best


def tempo(env: np.ndarray) -> float:
    """The tempo in BPM, not rounded (0 when the envelope shows no beat)."""
    lo, hi = LAG_MIN, LAG_MAX
    r = autocorrelation(env, MULTIPLES * hi + 2)
    best, score = 0, 0.0
    for lag in range(lo, hi + 1):
        if _is_peak(r, lag):
            s = float(r[lag]) * prior(lag)
            if s > score:
                best, score = lag, s
    if best == 0 or r[best] - median(r[lo:hi + 1]) < MIN_PULSE:
        return 0.0
    while True:  # an even pulse at half the lag is the beat
        half = _peak_near(r, best / 2.0)
        if half == 0 or half < lo or r[half] < FASTER_RATIO * r[best]:
            break
        best = half
    p = _parabola(r, best)
    lags, ks = p, 1.0
    for k in range(2, MULTIPLES + 1):
        q = _peak_near(r, k * p)
        if q and r[q] >= MULTIPLE_KEEP * r[best]:
            lags += _parabola(r, q)
            ks += k
    return 60.0 * FPS / (lags / ks)


def chroma_bins() -> np.ndarray:
    """The pitch class (0 = C) of every bin of a CHROMA_FFT spectrum, -1 outside
    CHROMA_MIN_HZ..CHROMA_MAX_HZ (the nearest equal-tempered note, A4 = 440 Hz)."""
    f = np.arange(CHROMA_FFT // 2 + 1) * (RATE / CHROMA_FFT)
    out = np.full(len(f), -1, dtype=np.int64)
    ok = (f >= CHROMA_MIN_HZ) & (f <= CHROMA_MAX_HZ)
    midi = 69.0 + 12.0 * np.log2(f[ok] / 440.0)
    out[ok] = np.floor(midi + 0.5).astype(np.int64) % 12
    return out


def chroma(x: np.ndarray) -> np.ndarray:
    """The 12 pitch-class sums of the average magnitude spectrum of CHROMA_FFT frames."""
    if len(x) < CHROMA_FFT:
        x = np.concatenate([x, np.zeros(CHROMA_FFT - len(x))])
    total = np.zeros(CHROMA_FFT // 2 + 1)
    n = 0
    for mag in _spectra(x, CHROMA_FFT, CHROMA_HOP, np.hanning(CHROMA_FFT)):
        total += mag.sum(axis=0)
        n += len(mag)
    mean = total / max(n, 1)
    bins = chroma_bins()
    return np.array([float(mean[bins == pc].sum()) for pc in range(12)])


def correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson's correlation of two vectors (0 when one of them is flat)."""
    a = np.asarray(a, dtype=np.float64) - float(np.mean(a))
    b = np.asarray(b, dtype=np.float64) - float(np.mean(b))
    den = math.sqrt(float(np.dot(a, a)) * float(np.dot(b, b)))
    return float(np.dot(a, b)) / den if den > 0 else 0.0


def key_of(c: Sequence[float]) -> Tuple[int, str, float]:
    """(pitch class of the tonic, "major"/"minor", strength) of a chroma vector: the
    best of the 24 rotated key profiles, and how far it lies above the best of the
    other mode (ties go to major, then to the lower pitch class)."""
    best = {}
    for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
        top, tonic = -2.0, 0
        for k in range(12):
            r = correlation(c, [profile[(pc - k) % 12] for pc in range(12)])
            if r > top:
                top, tonic = r, k
        best[mode] = (top, tonic)
    (rmaj, kmaj), (rmin, kmin) = best["major"], best["minor"]
    if rmaj >= rmin:
        return kmaj, "major", rmaj - rmin
    return kmin, "minor", rmin - rmaj


def tempo_label(bpm: int) -> str:
    if bpm <= 0:
        return ""
    if bpm >= UPTEMPO_BPM:
        return "Up-tempo"
    return "Slow" if bpm < SLOW_BPM else "Mid-tempo"


def energy_label(energy_db: float) -> str:
    return "Energetic" if energy_db >= ENERGETIC_DB else "Calm"


def mood_label(mode: str, key_strength: float, brightness: float, tempo_name: str, energy_name: str) -> str:
    """Happy, Sad, Chill or Intense from the valence (the mode, or the brightness when
    the key is unclear) and the arousal (energy and tempo); see the module's docstring."""
    positive = mode == "major" if key_strength >= KEY_MARGIN and mode else brightness >= BRIGHT_HZ
    arousal = int(energy_name == "Energetic") + int(tempo_name == "Up-tempo") - int(tempo_name == "Slow")
    if positive:
        return "Happy" if arousal >= 1 else "Chill"
    return "Intense" if arousal >= 1 else "Sad"


def analyse_samples(x) -> Mood:
    """The mood of mono samples at RATE Hz (full scale ±1)."""
    x = np.asarray(x, dtype=np.float64)
    seconds = len(x) / RATE
    ms = float(np.dot(x, x)) / len(x) if len(x) else 0.0
    energy = 10.0 * math.log10(max(ms, 1e-20))
    out = Mood(energy=energy, seconds=seconds)
    if seconds < MIN_SECONDS or len(x) < ONSET_FFT or energy < SILENCE_DB:
        return out
    env, power = onset_envelope(x)
    total = float(power.sum())
    freqs = np.arange(len(power)) * (RATE / ONSET_FFT)
    out.brightness = float(np.dot(freqs, power)) / total if total > 0 else 0.0
    out.bpm = int(math.floor(tempo(env) + 0.5))
    pc, mode, strength = key_of(chroma(x))
    out.key, out.mode, out.key_strength = PITCH_NAMES[pc], mode, strength
    out.tempo = tempo_label(out.bpm)
    out.energy_label = energy_label(energy)
    out.mood = mood_label(mode, strength, out.brightness, out.tempo, out.energy_label)
    return out


def analyse_file(path: str, duration: float = 0.0) -> Mood:
    """Decode and analyse one song (MoodError when it cannot be decoded)."""
    return analyse_samples(decode(path, duration))


def key_name(key: str, mode: str) -> str:
    """"A minor", "Eb major" ("" when unknown; the apps translate it)."""
    return f"{key} {mode}" if key and mode in MODES else ""


KEY_NAMES = tuple(key_name(k, m) for m in MODES for k in PITCH_NAMES)


# ------------------------------------------------------------------- the table ---
_TABLE = ("CREATE TABLE IF NOT EXISTS moods (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, bpm INTEGER, "
          "energy REAL, brightness REAL, key TEXT, mode TEXT, labels TEXT, version INTEGER)")


def labels_text(m: Mood) -> str:
    return json.dumps(m.labels(), ensure_ascii=False, sort_keys=True)


def _mood_of_row(row) -> Mood:
    bpm, energy, brightness, key, mode, labels = row
    try:
        lab = json.loads(labels or "{}")
    except ValueError:
        lab = {}
    lab = lab if isinstance(lab, dict) else {}

    def pick(name: str, allowed) -> str:
        v = lab.get(name, "")
        return v if v in allowed else ""
    return Mood(bpm=int(bpm or 0), energy=float(energy or 0.0), brightness=float(brightness or 0.0),
                key=key if key in PITCH_NAMES else "", mode=mode if mode in MODES else "",
                tempo=pick("tempo", TEMPOS), energy_label=pick("energy", ENERGIES), mood=pick("mood", MOODS))


class MoodStore:
    """The table `moods` of library.sqlite: one row per analysed song (path, size,
    mtime_ns, bpm, energy, brightness, key, mode, labels as JSON, version).  One per
    thread, as for the library index."""

    def __init__(self, path: Optional[str] = None):
        from .musiclib import default_path

        self.path = path or default_path()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.execute(_TABLE)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def put(self, path: str, size: int, mtime_ns: int, m: Mood) -> None:
        self.db.execute("INSERT OR REPLACE INTO moods VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (path, int(size), int(mtime_ns), int(m.bpm), float(m.energy), float(m.brightness), m.key,
                         m.mode, labels_text(m), ANALYSER_VERSION))
        self.db.commit()

    def get(self, path: str) -> Optional[Mood]:
        row = self.db.execute("SELECT bpm, energy, brightness, key, mode, labels FROM moods WHERE path = ?",
                              (path,)).fetchone()
        return _mood_of_row(row) if row else None

    def all(self) -> Dict[str, Mood]:
        """Every analysed song (also those of an older analyser, until they are analysed again)."""
        return {r[0]: _mood_of_row(r[1:]) for r in self.db.execute(
            "SELECT path, bpm, energy, brightness, key, mode, labels FROM moods")}

    def todo(self, tracks: Iterable) -> list:
        """The tracks (musiclib.Track) not analysed yet, changed since, or analysed by
        another ANALYSER_VERSION, in the order given."""
        known = {p: (s, m, v) for p, s, m, v in self.db.execute("SELECT path, size, mtime_ns, version FROM moods")}
        return [t for t in tracks if known.get(t.path) != (t.size, t.mtime_ns, ANALYSER_VERSION)]

    def forget(self) -> int:
        n = self.db.execute("DELETE FROM moods").rowcount
        self.db.commit()
        return n


# -------------------------------------------------------------- the background job ---
PAUSE_S = 0.05  # between two songs, so the analysis stays in the background


def run_analysis(tracks: Sequence, store: MoodStore, stop: Optional[threading.Event] = None,
                 progress: Optional[Callable[[int, int, str, Optional[Mood]], None]] = None,
                 pause: float = PAUSE_S, analyse: Callable[[str, float], Mood] = analyse_file) -> Tuple[int, int]:
    """Analyse the tracks that need it, one at a time, saving each result at once;
    `progress(done, total, path, mood)` after each song (mood None when it could not be
    decoded: it is tried again next time).  Returns (analysed, failed)."""
    todo = store.todo(tracks)
    done = failed = 0
    for i, t in enumerate(todo):
        if stop is not None and stop.is_set():
            break
        try:
            m: Optional[Mood] = analyse(t.path, float(t.duration or 0.0))
        except Exception:  # noqa: BLE001 - MoodError, or anything else: one bad file never stops the job
            m = None
        if m is not None:
            try:
                store.put(t.path, t.size, t.mtime_ns, m)
                done += 1
            except sqlite3.Error:
                failed += 1
        else:
            failed += 1
        if progress is not None:
            progress(i + 1, len(todo), t.path, m)
        if pause > 0 and not (stop is not None and stop.is_set()):
            time.sleep(pause)
    return done, failed


# ----------------------------------------------------------------- the pages ---
CARDS: Tuple[Tuple[str, str], ...] = tuple(("mood", m) for m in MOODS) + tuple(("tempo", t) for t in TEMPOS)
# what each card says under its name (English; the apps translate it)
CARD_NOTES = {
    "Happy": "Major key, lively", "Sad": "Minor key, calm", "Chill": "Major key, calm",
    "Intense": "Minor key, lively", "Up-tempo": "120 BPM and faster", "Mid-tempo": "90 to 119 BPM",
    "Slow": "Slower than 90 BPM",
}
ESTIMATE_NOTE = ("Moods, tempo and key are estimated from the audio on this computer, so they are a guess, "
                 "not a fact about the song.")


def label_of(kind: str, m: Optional[Mood]) -> str:
    if m is None:
        return ""
    return m.mood if kind == "mood" else m.tempo if kind == "tempo" else m.energy_label if kind == "energy" else ""


def card_songs(kind: str, label: str, tracks: Iterable, moods: Dict[str, Mood]) -> List[str]:
    """The paths of the songs of a card, sorted as All Songs is (title, artist, path)."""
    from .musiclib import sort_songs

    return [t.path for t in sort_songs(t for t in tracks if label and label_of(kind, moods.get(t.path)) == label)]


def card_counts(tracks: Iterable, moods: Dict[str, Mood]) -> Dict[str, int]:
    """{label: number of songs} for every card (0 included)."""
    out = {label: 0 for _, label in CARDS}
    for t in tracks:
        m = moods.get(t.path)
        if m is not None:
            for kind in ("mood", "tempo"):
                lab = label_of(kind, m)
                if lab in out:
                    out[lab] += 1
    return out


def analysed_count(tracks: Iterable, moods: Dict[str, Mood]) -> int:
    return sum(1 for t in tracks if t.path in moods)


def playlist_name(label: str) -> str:
    """The name Create Playlist gives a card's songs ("Sad songs"; translated by the apps)."""
    return f"{label} songs"


PLAYLIST_NAMES = tuple(playlist_name(label) for _, label in CARDS)


def bpm_text(m: Optional[Mood]) -> str:
    """The BPM column: "128", or "" when not analysed or without a beat."""
    return str(m.bpm) if m is not None and m.bpm > 0 else ""


def sort_bpm(m: Optional[Mood]) -> int:
    return m.bpm if m is not None else -1


__all__ = ["Mood", "MoodError", "MoodStore", "analyse_samples", "analyse_file", "decode", "run_analysis",
           "card_songs", "card_counts", "playlist_name", "key_name", "MOODS", "TEMPOS", "ENERGIES", "CARDS"]
