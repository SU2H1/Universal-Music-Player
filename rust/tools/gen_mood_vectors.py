#!/usr/bin/env python3
"""Write rust/crates/core/tests/python_moods.json: the Python answers of
realhires/moods.py (2.4) for the Rust port to match (moods_vectors.rs).

The test songs are made with the integer synthesiser of tests/mood_songs.py, which the
Rust test repeats exactly, so both versions analyse the very same samples; FFmpeg is
not involved.  Run it after changing realhires/moods.py or tests/mood_songs.py
(tests/test_moods.py checks that the file is current; it passes another output path).
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import numpy as np  # noqa: E402

import mood_songs  # noqa: E402
from realhires import moods  # noqa: E402
from realhires.musiclib import Track  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "rust", "crates", "core", "tests", "python_moods.json")


def mood_json(m: moods.Mood) -> dict:
    return {"bpm": m.bpm, "energy": m.energy, "brightness": m.brightness, "key": m.key, "mode": m.mode,
            "key_strength": m.key_strength, "tempo": m.tempo, "energy_label": m.energy_label, "mood": m.mood,
            "seconds": m.seconds, "summary": m.summary()}


def analysed(x: np.ndarray) -> dict:
    env, power = moods.onset_envelope(x)
    r = moods.autocorrelation(env, moods.MULTIPLES * moods.LAG_MAX + 2)
    out = mood_json(moods.analyse_samples(x))
    out.update({"tempo_exact": moods.tempo(env) if len(env) else 0.0, "env_len": len(env),
                "env_sum": float(env.sum()), "r": [float(r[k]) for k in (1, 25, 40, 60, 80, 103, 200, 400)],
                "chroma": [float(c) for c in moods.chroma(x)]})
    return out


# the songs, made at 22050 Hz (the analysis rate) and in a few variants
songs = []
for name, bpm, prog, secs, gain in mood_songs.SONGS:
    x = mood_songs.song(bpm, prog, secs, gain=gain).astype(np.float64) / 32768.0
    songs.append({"name": name, "bpm": bpm, "progression": prog, "seconds": secs, "gain": gain, "out": analysed(x)})
variants = []
for bpm, prog, secs, tone, click, gain in ((96, "minor", 6, 6000, 9000, 100), (140, "major", 3, 6000, 9000, 60),
                                          (100, "major", 8, 6000, 0, 100), (66, "minor", 10, 0, 9000, 100)):
    x = mood_songs.song(bpm, prog, secs, tone=tone, click=click, gain=gain).astype(np.float64) / 32768.0
    variants.append({"bpm": bpm, "progression": prog, "seconds": secs, "tone": tone, "click": click, "gain": gain,
                     "out": analysed(x)})
noise = mood_songs.lcg_noise(8, 6000).astype(np.float64) / 32768.0
edge = {"noise": analysed(noise),
        "silence": mood_json(moods.analyse_samples(np.zeros(22050 * 3))),
        "short": mood_json(moods.analyse_samples(np.full(1000, 0.1))),
        "empty": mood_json(moods.analyse_samples(np.zeros(0))),
        "quiet": mood_json(moods.analyse_samples(mood_songs.song(120, "major", 4, gain=0).astype(np.float64)))}

bins = moods.chroma_bins()
f = np.arange(moods.CHROMA_FFT // 2 + 1) * (moods.RATE / moods.CHROMA_FFT)
ok = (f >= moods.CHROMA_MIN_HZ) & (f <= moods.CHROMA_MAX_HZ)
midi = 69.0 + 12.0 * np.log2(f[ok] / 440.0)
margin = float(np.min(np.abs((midi + 0.5) - np.round(midi + 0.5))))
assert margin > 1e-6, margin  # no bin sits on a boundary between two pitch classes

chromas = [[1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0], [0] * 12, [1] * 12,
           [5, 1, 3, 1, 4, 4, 1, 5, 1, 3, 1, 2], [3, 1, 2, 4, 1, 2, 1, 4, 3, 1, 2, 2], [0.2, 0.9, 0.1, 0.7, 0.3, 0.0, 0.8,
                                                                                    0.6, 0.4, 0.5, 0.05, 0.25]]
keys = []
for c in chromas:
    pc, mode, strength = moods.key_of(c)
    keys.append({"chroma": c, "pc": pc, "mode": mode, "strength": strength})
mood_cases = []
for mode in ("major", "minor", ""):
    for ks in (0.0, 0.049, 0.05, 0.3):
        for bright in (800.0, 1500.0, 3000.0):
            for tempo in moods.TEMPOS + ("",):
                for energy in moods.ENERGIES:
                    mood_cases.append([mode, ks, bright, tempo, energy, moods.mood_label(mode, ks, bright, tempo, energy)])

tracks = [Track(f"/m/{i:02d} {t}.flac", title=t, artist=a, size=10 + i, mtime_ns=1000 + i)
          for i, (t, a) in enumerate([("Zebra", "B"), ("apple", "A"), ("Mango", "C"), ("Kiwi", "A"), ("Fig", ""),
                                      ("Date", "D")])]
table = {tracks[0].path: moods.Mood(bpm=128, tempo="Up-tempo", energy_label="Energetic", mood="Happy"),
         tracks[1].path: moods.Mood(bpm=72, tempo="Slow", energy_label="Calm", mood="Sad"),
         tracks[2].path: moods.Mood(bpm=130, tempo="Up-tempo", energy_label="Energetic", mood="Happy"),
         tracks[3].path: moods.Mood(bpm=0, energy_label="Calm", mood="Sad"),
         tracks[5].path: moods.Mood()}
cards = [{"kind": k, "label": label, "songs": moods.card_songs(k, label, tracks, table)} for k, label in moods.CARDS]

data = {
    "constants": {"lag_min": moods.LAG_MIN, "lag_max": moods.LAG_MAX, "trend_frames": moods.TREND_FRAMES,
                  "fps": moods.FPS, "analyser_version": moods.ANALYSER_VERSION},
    "chroma_bins": [int(b) for b in bins], "chroma_margin": margin,
    "songs": songs, "variants": variants, "edge": edge, "keys": keys, "mood_cases": mood_cases,
    "priors": [[lag, moods.prior(lag)] for lag in (25, 30.5, 43, 50, 86.13, 103)],
    "tempo_labels": [[b, moods.tempo_label(b)] for b in (0, -3, 1, 89, 90, 119, 120, 250)],
    "energy_labels": [[e, moods.energy_label(e)] for e in (-200.0, -16.0001, -16.0, -3.0)],
    "stretches": [[d, list(moods.stretch(d))] for d in (0.0, -1.0, 5.5, 89.9, 90.0, 100.7, 119.99, 120.0, 600.0)],
    "commands": [[p, s, n, moods.decode_command(p, s, n)[1:]] for p, s, n in (("/a b/ç.flac", 0, 90),
                                                                             ("x.mp3", 30, 90))],
    "samples": {"bytes": bytes(np.array([0.5, -0.25, 1.0, 1.0, -1.0, 0.125, 3.0], dtype="<f4").tobytes()).hex(),
                "out": [float(v) for v in moods.samples_of(np.array([0.5, -0.25, 1.0, 1.0, -1.0, 0.125, 3.0],
                                                                      dtype="<f4").tobytes())]},
    "labels_text": [[mood_json(m), moods.labels_text(m)] for m in (moods.Mood(tempo="Slow", energy_label="Calm",
                                                                              mood="Sad"), moods.Mood())],
    "key_names": [[k, m, moods.key_name(k, m)] for k, m in (("A", "minor"), ("Eb", "major"), ("", "major"),
                                                           ("C", ""), ("C", "dorian"))],
    "playlist_names": [[label, moods.playlist_name(label)] for _, label in moods.CARDS],
    "notes": {label: moods.CARD_NOTES[label] for _, label in moods.CARDS},
    "estimate_note": moods.ESTIMATE_NOTE,
    "tracks": [t.to_json() for t in tracks],
    "table": {p: mood_json(m) for p, m in table.items()},
    "cards": cards,
    "counts": moods.card_counts(tracks, table),
    "bpm_texts": [[mood_json(m) if m else None, moods.bpm_text(m)] for m in (None, moods.Mood(), moods.Mood(bpm=99))],
}
with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=0, ensure_ascii=False)
print(len(songs), "songs,", len(variants), "variants,", len(mood_cases), "mood cases ->", OUT)
