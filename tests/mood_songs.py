"""Invented test songs for the mood analysis (2.4), made with integer arithmetic only,
so that the Rust test (`rust/crates/core/tests/moods_vectors.rs`) makes the very same
samples and both versions analyse identical audio.

A song is a click on every beat (a 10 ms burst of noise from a 31-bit LCG, decaying
linearly) over a chord progression of triangle waves (a bass note an octave below the
root, then the three notes of the chord), one chord per bar of four beats:

* "major": C - F - G - C (I - IV - V - I in C major)
* "minor": Am - Dm - Em - Am (i - iv - v - i in A minor)

Every division has a non-negative numerator, or is floored (`div_euclid` in Rust), so
Python's `//` and Rust give the same numbers.  `song()` returns int16 samples (mono);
`write_wav()` writes them as a 16-bit WAV (several channels repeat the samples).
NumPy's int64 `//` floors as Python's does.
"""

from __future__ import annotations

import struct
from typing import Dict, List

import numpy as np

# the notes of octave 4 in millihertz (equal temperament, A4 = 440 Hz), C first
NOTE_MHZ = [261626, 277183, 293665, 311127, 329628, 349228, 369994, 391995, 415305, 440000, 466164, 493883]
NAMES = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}
# (root, notes) per bar; the notes as (name, octave)
PROGRESSIONS: Dict[str, List[List[tuple]]] = {
    "major": [[("C", 2), ("C", 4), ("E", 4), ("G", 4)], [("F", 2), ("F", 4), ("A", 4), ("C", 5)],
              [("G", 2), ("G", 4), ("B", 4), ("D", 5)], [("C", 2), ("C", 4), ("E", 4), ("G", 4)]],
    "minor": [[("A", 2), ("A", 3), ("C", 4), ("E", 4)], [("D", 2), ("D", 4), ("F", 4), ("A", 4)],
              [("E", 2), ("E", 4), ("G", 4), ("B", 4)], [("A", 2), ("A", 3), ("C", 4), ("E", 4)]],
}


def period(name: str, octave: int, sr: int) -> int:
    """The period in samples of a note (rounded to a whole sample)."""
    f = NOTE_MHZ[NAMES[name]]
    if octave >= 4:
        num, den = sr * 1000, f << (octave - 4)
    else:
        num, den = sr * 1000 << (4 - octave), f
    return (2 * num + den) // (2 * den)


def triangle(n, p: int, amp: int):
    """A triangle wave of period p and amplitude amp at sample(s) n (-amp..amp)."""
    return amp * 4 * abs(2 * (n % p) - p) // (2 * p) - amp


def song(bpm: int, progression: str, seconds: int, sr: int = 22050, tone: int = 6000, click: int = 9000,
         gain: int = 100) -> np.ndarray:
    """The samples (int16, mono) of an invented song: clicks at `bpm` over the chords of
    `progression`, `gain` in percent (100: about -12 dBFS RMS)."""
    n_total = seconds * sr
    out = np.zeros(n_total, dtype=np.int64)
    # the chords: one per bar (4 beats), the progression repeated
    bar = 0
    while True:
        start = bar * 4 * 60 * sr // bpm
        if start >= n_total:
            break
        end = min(n_total, (bar + 1) * 4 * 60 * sr // bpm)
        n = np.arange(start, end, dtype=np.int64)
        for i, (name, octave) in enumerate(PROGRESSIONS[progression][bar % 4]):
            amp = tone * 3 // 2 if i == 0 else tone  # the bass a little louder
            out[start:end] += triangle(n, period(name, octave, sr), amp)
        bar += 1
    # the clicks
    length = sr // 100
    x = 12345
    beat = 0
    while True:
        at = beat * 60 * sr // bpm
        if at >= n_total:
            break
        for k in range(min(length, n_total - at)):
            x = (1103515245 * x + 12345) % (1 << 31)
            noise = (x >> 15) - 32768          # -32768..32767
            env = click * (length - k) // length
            out[at + k] += noise * env // 32768  # floored
        beat += 1
    return np.clip(out * gain // 100, -32768, 32767).astype(np.int16)


def lcg_noise(seconds: int, amp: int, sr: int = 22050, seed: int = 777) -> np.ndarray:
    """Noise without a beat (int16, mono): the clicks' LCG at every sample."""
    out = np.zeros(seconds * sr, dtype=np.int64)
    x = seed
    for i in range(len(out)):
        x = (1103515245 * x + 12345) % (1 << 31)
        out[i] = ((x >> 15) - 32768) * amp // 32768
    return out.astype(np.int16)


def write_wav(path: str, samples, sr: int = 22050, channels: int = 1) -> None:
    data = np.repeat(np.asarray(samples, dtype="<i2"), channels).tobytes()
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE")
        fh.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sr, sr * channels * 2, channels * 2, 16))
        fh.write(b"data" + struct.pack("<I", len(data)) + bytes(data))


# the songs of the tests and the vectors: (name, bpm, progression, seconds, gain)
SONGS = [
    ("happy", 128, "major", 20, 100),
    ("sad", 72, "minor", 20, 12),
    ("intense", 150, "minor", 20, 100),
    ("chill", 84, "major", 20, 12),
    ("mid", 104, "major", 16, 100),
    ("fast", 174, "major", 16, 100),
]
