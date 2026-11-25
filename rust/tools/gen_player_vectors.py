"""Writes the Python answers the Rust player core is tested against:
rust/crates/core/tests/python_eq.json, python_queue.json, python_stats.json,
python_eqfiles.json, python_themes.json, python_playerstate.json,
python_tagedit.json (the tag editor and the themes folder),
python_bitperfect.json (bit-perfect playback's decisions and texts), python_covers.json
(the names of covers kept on disk), python_library.json (the Quality column's tooltip
and sort, renaming groups, the EQ preset match) and python_leaderboard.json (2.4: Your
Stats' leaderboard, "Most Played").  Run from the repository root:

    python3 rust/tools/gen_player_vectors.py
"""
import json
import os
import sys

sys.path.insert(0, os.getcwd())
import time  # noqa: E402

from realhires import bitperfect, eq, musiclib, playerstate, stats, tagedit, themes  # noqa: E402
from realhires.musiclib import Track  # noqa: E402
from realhires.playqueue import PlayQueue, Song, SplitMix64, shuffled  # noqa: E402

OUT = os.path.join("rust", "crates", "core", "tests")

cases = []
for rate in (44100.0, 48000.0, 96000.0):
    for kind in eq.BAND_TYPES:
        for freq, gain, q in ((32.0, 6.0, 1.41), (1000.0, -7.5, 0.7), (12000.0, 3.25, 4.0), (30000.0, 6, 1)):
            cases.append({"kind": kind, "freq": freq, "gain": gain, "q": q, "rate": rate,
                          "coeffs": list(eq.biquad(kind, freq, gain, q, rate))})
rock = eq.apply_preset(eq.default_state(), "Rock")
rock["enabled"] = True
mixed = eq.apply_preset(eq.default_state(), "Vocal")
mixed.update({"enabled": True, "prevent_clipping": False})
mixed["graphic"]["preamp"] = -2.5
mixed["parametric"] = {"enabled": True, "preamp": 1.5, "bands": [
    {"type": "low_shelf", "freq": 80.0, "gain": 3.0, "q": 0.707, "on": True},
    {"type": "notch", "freq": 60.0, "gain": 0.0, "q": 8.0, "on": True},
    {"type": "peak", "freq": 2500.0, "gain": -2.0, "q": 2.0, "on": False}]}
broken = {"enabled": True, "graphic": {"gains": [99, -99, "x"], "preamp": "y"},
          "parametric": {"enabled": True, "bands": [{"type": "?", "freq": 1e9, "q": -1}]}}
states = []
for st in (eq.default_state(), rock, mixed, broken):
    states.append({"state": st, "normalized": eq.normalize(st), "preamp": eq.preamp_db(st),
                   "stages_44100": [list(c) for c in eq.stages(st, 44100.0)],
                   "response": [[f, db] for f, db in eq.response(st)][::20]})
with open(os.path.join(OUT, "python_eq.json"), "w", encoding="utf-8") as fh:
    json.dump({"biquads": cases, "states": states, "presets": eq.PRESETS}, fh, indent=0)

songs = []
for a, (artist, genre) in enumerate((("Band A", "Rock"), ("Band B", "Jazz"), ("Band C", ""))):
    for album in range(2):
        for n in range(1, 5):
            songs.append(Song(f"/m/{artist}/{album}/{n:02d}.flac", f"Song {a}{album}{n}", artist,
                              f"{artist} Album {album}", artist if a != 2 else "", genre if album == 0 else "Pop",
                              1 if n < 4 else 2, n))
everything = [s.path for s in songs]
runs = []
for shuffle in ("off", "songs", "library", "genre", "artist", "album"):
    for repeat in ("off", "song", "album", "stop"):
        for seed in (1, 99):
            q = PlayQueue(songs, everything[:10], shuffle=shuffle, repeat=repeat, seed=seed)
            steps = [q.start(everything[2])]
            for op in ("auto", "next", "auto", "prev", "next", "auto", "next", "next", "auto", "next", "auto",
                       "set_shuffle:library", "next", "set_repeat:album", "auto", "auto", "next"):
                if op == "auto":
                    steps.append(q.next(auto=True))
                elif op == "next":
                    steps.append(q.next())
                elif op == "prev":
                    steps.append(q.previous())
                elif op.startswith("set_shuffle:"):
                    q.set_shuffle(op.split(":")[1])
                    steps.append(q.current)
                else:
                    q.set_repeat(op.split(":")[1])
                    steps.append(q.current)
            runs.append({"shuffle": shuffle, "repeat": repeat, "seed": seed, "steps": steps,
                         "up_next": q.up_next(30)})
# disliked songs are left out of shuffled orders
for shuffle in ("off", "songs", "library", "artist"):
    for seed in (5, 77):
        disliked = [everything[1], everything[5], everything[11], everything[17]]
        q = PlayQueue(songs, everything[:10], shuffle=shuffle, repeat="off", seed=seed, disliked=set(disliked))
        steps = [q.start(everything[2])]
        for op in ("auto", "next", "auto", "prev", "next", "auto", "next", "next", "auto", "next", "auto",
                   "set_shuffle:library", "next", "set_repeat:album", "auto", "auto", "next"):
            if op == "auto":
                steps.append(q.next(auto=True))
            elif op == "next":
                steps.append(q.next())
            elif op == "prev":
                steps.append(q.previous())
            elif op.startswith("set_shuffle:"):
                q.set_shuffle(op.split(":")[1])
                steps.append(q.current)
            else:
                q.set_repeat(op.split(":")[1])
                steps.append(q.current)
        runs.append({"shuffle": shuffle, "repeat": "off", "seed": seed, "steps": steps, "up_next": q.up_next(30),
                     "disliked": disliked})
# the user's queue (2.1): Play Next / Add to Queue / Up Next jumps, with each mode
user_ops = ["auto", "play_next:3,4", "add:7", "next", "play_next:9", "auto", "next", "prev", "next",
            "add:1,2,6", "jump:1", "next", "jump:3", "add:8", "set_shuffle:songs", "next", "next", "auto"]
user_runs = []
for shuffle in ("off", "songs", "album"):
    for repeat in ("off", "song", "album"):
        q = PlayQueue(songs, everything[:10], shuffle=shuffle, repeat=repeat, seed=11)
        steps = [q.start(everything[2])]
        for op in user_ops:
            name, _, arg = op.partition(":")
            picked = [everything[int(i)] for i in arg.split(",")] if arg and name in ("play_next", "add") else []
            if name == "auto":
                steps.append(q.next(auto=True))
            elif name == "next":
                steps.append(q.next())
            elif name == "prev":
                steps.append(q.previous())
            elif name == "play_next":
                q.play_next(picked)
                steps.append(q.current)
            elif name == "add":
                q.add_to_queue(picked)
                steps.append(q.current)
            elif name == "jump":
                steps.append(q.jump(int(arg)))
            else:
                q.set_shuffle(arg)
                steps.append(q.current)
            steps.append(q.up_next(6))
        user_runs.append({"shuffle": shuffle, "repeat": repeat, "seed": 11, "steps": steps})
# Up Next edited by the user (2.3): moves and removals within and across its three parts
# (stepped back over, queued, the rest), then the order built again and played on
edit_ops = ["next", "next", "prev", "prev", "play_next:9", "add:3,4",
            "move:0:4", "move:3:0", "move:2:5", "remove:1", "remove:3", "move:5:1", "move:1:7", "move:4:4",
            "move:0:99", "move:99:0", "remove:99", "next", "auto", "prev", "remove:0", "move:0:2", "next",
            "set_shuffle:songs", "remove:2", "move:3:0", "add:7", "move:0:3", "set_repeat:album", "auto", "auto",
            "auto", "auto", "auto", "auto", "prev", "move:0:1", "remove:1", "next", "set_shuffle:off", "auto",
            "jump:2", "auto"]
edit_runs = []
for shuffle in ("off", "songs", "library", "album"):
    for repeat in ("off", "album"):
        for seed in (3, 41):
            q = PlayQueue(songs, everything[:10], shuffle=shuffle, repeat=repeat, seed=seed,
                          disliked={everything[7]})
            q.start(everything[1])
            steps = []
            for op in edit_ops:
                name, _, arg = op.partition(":")
                if name == "move":
                    a, b = arg.split(":")
                    result = q.move_upcoming(int(a), int(b))
                elif name == "remove":
                    result = q.remove_upcoming(int(arg))
                elif name == "auto":
                    result = q.next(auto=True)
                elif name == "next":
                    result = q.next()
                elif name == "prev":
                    result = q.previous()
                elif name == "jump":
                    result = q.jump(int(arg))
                elif name in ("play_next", "add"):
                    picked = [everything[int(i)] for i in arg.split(",")]
                    q.play_next(picked) if name == "play_next" else q.add_to_queue(picked)
                    result = q.current
                elif name == "set_shuffle":
                    q.set_shuffle(arg)
                    result = q.current
                else:
                    q.set_repeat(arg)
                    result = q.current
                steps.append({"result": result, "current": q.current, "up_next": q.up_next(30),
                              "queued": list(q.queued), "pos": q.pos, "top": q.top, "inserted": sorted(q.inserted),
                              "count": q.upcoming_count()})
            edit_runs.append({"shuffle": shuffle, "repeat": repeat, "seed": seed, "disliked": [everything[7]],
                              "start": everything[1], "steps": steps, "removed": sorted(q.removed)})
# several artists in one field (2.1): split_artists() and the groupings it gives
art_tracks = [Track(path=f"/art/{i:02d}.flac", title=f"T{i}", artist=a, album_artist=aa)
              for i, (a, aa) in enumerate([
                  ("Mariah Carey, Michael Bublé", ""), ("Michael Bublé", ""), ("Earth, Wind & Fire", ""),
                  ("Simon & Garfunkel", ""), ("Drake (feat. Rihanna)", ""), ("One;Two", "Two"),
                  ("Michael Bublé & Someone x Other / More", "Michael Bublé"), ("A FEAT. b, B", ""),
                  ("  Mira Calloway ;  ", "Mira Calloway, The Northern Quay"), ("The Northern Quay ft Ann", ""),
                  ("", "Various, Michael Bublé"), ("Ünïcode x ÉCLAIR", ""), ("ÉCLAIR", "")])]
art_known = musiclib.known_artists(art_tracks)
with open(os.path.join(OUT, "python_artists.json"), "w", encoding="utf-8") as fh:
    json.dump({"tracks": [x.to_json() for x in art_tracks], "known": sorted(art_known),
               "split": [[t.artist, musiclib.split_artists(t.artist, art_known)] for t in art_tracks]
               + [[n, musiclib.split_artists(n, art_known)] for n in ("Earth, Wind", "X (ft. Y))", "a;;b", "ab FT. cd")],
               "artists": [[g["name"], [x.path for x in g["tracks"]]] for g in musiclib.artists(art_tracks)],
               "album_artists": [[g["name"], [x.path for x in g["tracks"]]] for g in musiclib.album_artists(art_tracks)]},
              fh, indent=0, ensure_ascii=False)
rng = SplitMix64(2024)
with open(os.path.join(OUT, "python_queue.json"), "w", encoding="utf-8") as fh:
    json.dump({"songs": [s.__dict__ for s in songs], "context": everything[:10], "start": everything[2],
               "ops": ["auto", "next", "auto", "prev", "next", "auto", "next", "next", "auto", "next", "auto",
                       "set_shuffle:library", "next", "set_repeat:album", "auto", "auto", "next"],
               "runs": runs, "user_ops": user_ops, "user_runs": user_runs,
               "edit_ops": edit_ops, "edit_runs": edit_runs,
               "splitmix_2024": [str(rng.next()) for _ in range(5)],
               "shuffled_9": shuffled([str(i) for i in range(12)], SplitMix64(9))}, fh, indent=0)

# ------------------------------------------------------------- statistics ---
def tokyo(ts):
    return time.gmtime(ts + 9 * 3600)


tracks = []
artists_ = ["Mira Calloway", "The Northern Quay", "Juniper Vale", "", "mira calloway"]
for i in range(14):
    a = artists_[i % len(artists_)]
    tracks.append(Track(path=f"/lib/{'AB'[i % 2]}/{'sub/' if i % 3 == 0 else ''}{i:02d}.flac", title=f"Song {i}",
                        artist=a, album=["Paper Lanterns", "Harbour Lights", "", "Static Bloom"][i % 4],
                        album_artist="Various" if i % 5 == 0 else "", genre=["Pop", "jazz", "", "Jazz"][i % 4],
                        composer=["", "Zed", "zed", "Ann"][i % 4], year=["2001", "1999", "x", "", "2024"][i % 5],
                        track=i % 6, disc=1 + i % 2, duration=[20.0, 180.0, 240.5, 61.0][i % 4],
                        codec=["flac", "mp3", "alac", "aac"][i % 4], is_lossy=i % 4 in (1, 3),
                        sample_rate=[44100, 48000, 96000, 192000][i % 4], bits=[16, 0, 24, 0][i % 4],
                        is_float=i == 6, size=1000 * i, mtime_ns=i))
plays = []
t = 1_700_000_000.0
for i in range(60):
    tr = tracks[(i * 7) % len(tracks)]
    t += [600.0, 3600.0 * 5, 86400.0, 86400.0 * 3, 1800.0][i % 5]
    secs = [tr.duration, 5.0, 31.0, tr.duration / 2.0, 0.0 + 12.5][i % 5]
    plays.append(stats.Play(tr.path, t, secs, tr.duration, tr.display_title, tr.artist, tr.album, tr.album_artist,
                            tr.genre, tr.year, tr.composer))
st = stats.compute(plays, localtime=tokyo)
half = stats.compute(plays[:7], localtime=tokyo)
groups = {name: [[g["name"], [x.path for x in g["tracks"]]] for g in fn(tracks)]
          for name, fn in (("artists", musiclib.artists), ("album_artists", musiclib.album_artists),
                           ("genres", musiclib.genres), ("composers", musiclib.composers), ("years", musiclib.years))}
folders = {f: [[list(x) for x in musiclib.folder_view(tracks, f)[0]], [x.path for x in musiclib.folder_view(tracks, f)[1]]]
           for f in ("/lib", "/lib/A", "/lib/B", "/lib/A/sub", "/nope")}
ls = stats.library_stats(tracks)
# the calendar of Your Stats (2.1): every month with plays, a leap February and an empty month; the
# extra plays have no title, only an album artist, and start just before and after midnight in Tokyo
extra = [stats.Play("/lib/x/Night Walk.flac", 1_702_997_940.0, 75.0, 200.0, title="  ", artist=" ",
                    album_artist=" Harbour Choir "),
         stats.Play("/lib/x/Night Walk.flac", 1_702_998_000.0, 29.0, 200.0, title="Night Walk"),
         stats.Play("/lib/y/Long.flac", 1_703_001_600.0, 3600.0 * 21.0 + 31.0, 99999.0, title="Long", artist="Ann")]
cal_plays = sorted(plays + extra, key=lambda p: p.started)
months = sorted({(tokyo(p.started).tm_year, tokyo(p.started).tm_mon) for p in cal_plays}) + [(2024, 2), (2026, 11)]
cal = []
for y, m in months:
    mins = stats.month_minutes(cal_plays, y, m, localtime=tokyo)
    days = []
    for d in range(1, len(mins) + 1):
        rows = stats.day_plays(cal_plays, y, m, d, localtime=tokyo)
        if rows or d == 1:
            days.append({"day": d, "title": stats.day_title(y, m, d), "plays": rows, "summary": stats.day_summary(rows)})
    cal.append({"year": y, "month": m, "title": stats.month_title(y, m), "grid": stats.month_grid(y, m),
                "minutes": mins, "levels": stats.month_levels(mins), "days": days})
with open(os.path.join(OUT, "python_stats.json"), "w", encoding="utf-8") as fh:
    json.dump({"tracks": [x.to_json() for x in tracks], "plays": [p.__dict__ for p in plays], "offset": 9 * 3600,
               "stats": st, "cards": stats.cards(st), "stats_7": half, "cards_7": stats.cards(half),
               "cards_empty": stats.cards(stats.compute([], localtime=tokyo)), "groups": groups, "folders": folders,
               "library": ls, "hours": [stats.hour_label(h) for h in range(24)],
               "calendar_plays": [p.__dict__ for p in extra], "calendar": cal,
               "minutes_labels": [[s, stats.minutes_label(s)] for s in (0.0, 1.0, 29.9, 30.0, 89.9, 90.0, 74_043.0)],
               "shift_month": [[y, m, d, *stats.shift_month(y, m, d)]
                               for y, m, d in ((2026, 11, 1), (2026, 12, 1), (2026, 1, -1), (2026, 3, -27), (1999, 7, 0))]},
              fh, indent=0, ensure_ascii=False)

# ------------------------------------------------- leaderboard (2.4) ---
# the statistics' plays and the calendar's, plus ties (the same plays and whole minutes), a
# song with only short plays (left out), a song without a title and one with a title in
# another case: Most Played's songs, artists and albums, also cut at 3 and 5 rows
board_plays = cal_plays + [
    stats.Play("/lib/t/Tie A.flac", 1_703_100_000.0, 95.0, 200.0, title="Tie A", artist="Zoe", album="Ties"),
    stats.Play("/lib/t/Tie B.flac", 1_703_100_300.0, 100.0, 200.0, title="tie b", artist="zoe", album="ties"),
    stats.Play("/lib/t/Short.flac", 1_703_100_600.0, 4.0, 200.0, title="Short", artist="Zoe", album="Ties"),
    stats.Play("/lib/t/Short.flac", 1_703_100_700.0, 9.0, 200.0, title="Short", artist="Zoe", album="Ties"),
    stats.Play("/lib/t/Ünnamed Song.flac", 1_703_100_900.0, 61.0, 120.0, title=" ", artist="Émile"),
    stats.Play("/lib/t/Tie A.flac", 1_703_101_000.0, 0.0, 200.0, title="Tie A", artist="Zoe", album="Ties"),
]
boards = {kind: {str(top): stats.leaderboard(board_plays, kind, top) for top in (stats.TOP, 5, 3, 0)}
          for kind in stats.LEADERBOARD_KINDS}
with open(os.path.join(OUT, "python_leaderboard.json"), "w", encoding="utf-8") as fh:
    json.dump({"plays": [p.__dict__ for p in board_plays], "kinds": stats.LEADERBOARD_KINDS, "boards": boards,
               "ranked": {kind: stats.ranked_songs(boards[kind][str(stats.TOP)]) for kind in stats.LEADERBOARD_KINDS},
               "empty": stats.leaderboard([], "songs")}, fh, indent=0, ensure_ascii=False)

# ------------------------------------------------------------- EQ files ---
param = eq.apply_preset(eq.default_state(), "Rock")
param["parametric"] = {"enabled": True, "preamp": -1.5, "bands": [
    {"type": "high_shelf", "freq": 8000, "gain": -3, "q": 0.71, "on": True},
    {"type": "notch", "freq": 3000, "gain": 0, "q": 5, "on": False},
    {"type": "low_pass", "freq": 18000.25, "gain": 2, "q": 0.5, "on": True}]}
exports = [{"name": n, "state": s_, "json": eq.export_json(n, s_), "apo": eq.export_apo(n, s_)}
           for n, s_ in (("My Rock", param), ("Flat", eq.default_state()), ("Für Elise ♪", mixed))]
texts = [
    "Preamp: -6.2 dB\nFilter 1: ON PK Fc 105 Hz Gain 3.5 dB Q 0.70\nFilter 2: ON LSC Fc 105 Hz Gain 4.1 dB Q 0.71\n",
    "\ufeff# AutoEQ\r\nPreamp: -3,5 dB\r\nFilter 1: ON PK Fc 105Hz Gain 3.5dB Q 0.7\r\nFilter 2: OFF HP Fc 20 Hz\r\n"
    "Filter 3: ON PK Fc 1000 Hz Gain -2 dB BW Oct 1.0\r\nFilter: ON NO Fc 60 Hz Q 30\r\nFilter 5: ON BP Fc 1 Hz\r\n",
    "Preamp: -20 dB\n" + "".join(f"Filter {i}: ON PK Fc {100 * i} Hz Gain {i - 6} dB Q 1\n" for i in range(1, 13)),
    "GraphicEQ: 20 -1; 50 2; 100 3; 1000 0; 20000 -6",
    "Preamp: -2 dB\nGraphicEQ: 0 5; 25 -14; 16000 14",
    "Preamp: x dB\nfilter 1 : on pk fc 440 hz gain 1 db q 2\nFilter 2: ON LS Fc 80 Hz Gain 2 dB\n",
    eq.export_json("Saved", param), json.dumps({"name": "  ", "eq": mixed}), json.dumps(mixed),
    json.dumps({"hello": 1}), "hello", "", "Filter 1: ON PK Fc x Hz Gain 1 dB Q 1",
]
imports = []
for text in texts:
    try:
        name, state, notes = eq.import_eq(text, "Imported")
        imports.append({"text": text, "name": name, "state": state, "notes": notes})
    except eq.EqFileError as exc:
        imports.append({"text": text, "error": str(exc)})
with open(os.path.join(OUT, "python_eqfiles.json"), "w", encoding="utf-8") as fh:
    json.dump({"exports": exports, "imports": imports}, fh, indent=0, ensure_ascii=False)

# ---------------------------------------------------------------- themes ---
custom = [themes.normalize({"name": " My  Theme ", "dark": True, "colors": {"accent": "#00FF00", "text": "red"}}),
          themes.normalize({"name": "Paper", "colors": {"background": "#fafafa"}})]
cases_t = [{"input": x, "output": themes.normalize(x)} for x in (
    {"name": " My  Theme ", "dark": True, "colors": {"accent": "#00FF00", "text": "red"}}, {"name": ""}, "x",
    {"name": "Paper", "colors": {"background": "#fafafa", "panel": " #ABCDEF "}}, {"name": "N", "colors": 5})]
imports_t = []
for text in (themes.export_theme(custom[0]), themes.export_theme(dict(custom[1], name="Dark")), "{}", "nope",
             json.dumps({"name": "Paper", "colors": {}})):
    try:
        imports_t.append({"text": text, "theme": themes.import_theme(text, custom)})
    except themes.ThemeError as exc:
        imports_t.append({"text": text, "error": str(exc)})
checks = []
for n in ("dark", "My theme", " New  one ", "", "Match the system"):
    try:
        checks.append([n, themes.check_name(n, custom), None])
    except themes.ThemeError as exc:
        checks.append([n, None, str(exc)])
# 2.3: every colour of every built-in theme and of custom themes, set and unset extra colours,
# and exported/imported files with and without them
extra_custom = themes.normalize({"name": "Gold", "dark": False, "colors": {
    "accent": "#ffd60a", "hover": "#123456", "row_hover": "#ABCDEF", "quality_hires": " #00ff00 ",
    "verdict_cd": "#0000ff", "calendar": "nope", "zebra": 5, "unknown_key": "#ffffff"}})
pale = themes.normalize({"name": "Pale accent", "dark": True, "colors": {"accent": "#f0f0f0", "secondary": "#808080"}})
all_colors = {name: themes.colors(dict(t, name=name)) for name, t in themes.BUILTIN.items()}
for t in custom + [extra_custom, pale]:
    all_colors[t["name"]] = themes.colors(t)
extra_imports = []
for text in (themes.export_theme(extra_custom), themes.export_theme(custom[1]),
             json.dumps({"name": "Old file", "dark": True, "colors": {"accent": "#112233"}}),
             json.dumps({"name": "Only extras", "colors": {"field": "#010203", "verdict_error": "#FFFFFF"}})):
    t = themes.import_theme(text, custom)
    extra_imports.append({"text": text, "theme": t, "colors": themes.colors(t), "export": themes.export_theme(t)})
mixes = [[a, b, t, themes.mix(a, b, t)] for a, b, t in (
    ("#ffffff", "#e8e8ed", 0.7), ("#000000", "#ffffff", 0.5), ("#010101", "#020202", 0.5), ("#030303", "#040404", 0.5),
    ("#1e1e1e", "#333336", 0.3), ("#fa2d48", "#000000", 0.12), ("#00ff80", "#ff0080", 0.45))]
lums = [[c, themes.luminance(c)] for c in ("#ffffff", "#ffd60a", "#fa2d48", "#9e9e9e", "#a0a0a0", "#c8c8c8")]
with open(os.path.join(OUT, "python_themes.json"), "w", encoding="utf-8") as fh:
    json.dump({"builtin": themes.BUILTIN, "custom": custom, "names": themes.names(custom), "cases": cases_t,
               "imports": imports_t, "checks": checks, "copy": [themes.copy_name("Midnight", custom),
                                                               themes.copy_name("My Theme", custom)],
               "export": themes.export_theme(custom[0]),
               "extra_keys": themes.EXTRA_COLOR_KEYS,
               "extra_labels": [themes.color_label(k) for k in themes.ALL_COLOR_KEYS],
               "extra_custom": extra_custom, "extra_export": themes.export_theme(extra_custom),
               "all_colors": all_colors, "extra_imports": extra_imports, "mixes": mixes, "luminances": lums,
               "verdict_keys": themes.VERDICT_COLOR_KEYS},
              fh, indent=0, ensure_ascii=False)

# ---------------------------------------------------------- player state ---
messy = {"liked": ["/a", "/b", 5], "disliked": ["/b", "/c", "/c", ""], "volume": "0.4",
         "eq_presets": [{"name": " Night  Mode ", "eq": mixed}, {"name": "night mode", "eq": {}}, {"name": ""}, 7],
         "playlists": [{"id": "a1", "name": "  Road ", "created": "12.5", "songs": ["/a", "/a", 3, "/b"]},
                       {"id": "a1", "name": "dupe"}, {"name": "no id"}, {"id": "b2", "name": "", "songs": "x"}],
         "lyrics_size": "31.5",
         "columns": {"songs": ["nope", "year", "artist", 3], "album": [], "folders": "x", "other": ["title"]},
         "labels": {"song": {"/a": "#FF0000", "/b": "red", "/c": 5, "": "#00ff00"},
                    "genre": {"Rock": "#00ff00", "rock": " #0000FF "}, "artist": {"Ann": "#123456"},
                    "album": {"x\u0000ann": "#abcdef"}, "year": {"2001": "#000000"}, "nope": {"a": "#111111"},
                    "playlist": [], "tab": {"Hi-Res": "#c9a227"}, "composer": {"": "#222222"}}}
ps = playerstate.PlayerState.from_json(messy)
# Now Playing's lyrics size (A− / A+): what player.json may hold, and the steps
sizes = [[v, playerstate.PlayerState.from_json({"lyrics_size": v}).lyrics_size]
         for v in (26, 30, "30", " 31 ", 12, 60, 25.5, 26.5, 27.5, 30.49, True, None, "x", [], {}, -5, 1e308,
                   float("nan"), float("inf"), "inf")]
size_steps = []
for start in (26, 47, 48, 16, 17, 99, "x"):
    for steps in (1, -1, 3, -20):
        st = playerstate.PlayerState(lyrics_size=start)
        size_steps.append([start, steps, st.step_lyrics_size(steps), playerstate.plain_lyrics_size(st.lyrics_size)])
ops = playerstate.PlayerState()
pid = ops.create_playlist("Road Trip", ["/m/a", "/m/b", "/m/a"], now=1000.0)
pid2 = ops.create_playlist("road trip", now=1000.0)
ops.add_to_playlist(pid, ["/m/b", "/m/c"])
ops.move_in_playlist(pid, "/m/c", -2)
ops.rename_playlist(pid2, "  Night   Drive ")
ops.set_liked("/m/a", True)
ops.set_disliked("/m/a", True)
ops.set_disliked("/m/z", True)
ops.save_eq_preset("  My   Jazz ", eq.apply_preset(eq.default_state(), "Jazz"))
ops.save_eq_preset("Other", eq.default_state())
ops.rename_eq_preset("Other", "Bright")
ops.step_lyrics_size(1)
ops.step_lyrics_size(1)
ops.toggle_column("songs", "year")
ops.toggle_column("songs", "like")
ops.toggle_column("songs", "title")
ops.toggle_column("album", "file")
ops.set_columns("folders", ["bit_rate", "format"])
ops.toggle_column("folders", "format")
# 2.3: colour labels
label_ops = [("song", "/m/a", "#FF3B30"), ("album", "city\u0000june", "#34c759"), ("artist", "June Harbor", "#007aff"),
             ("album_artist", "ÉMILE", "#af52de"), ("genre", "Indie Pop", "#ff9500"), ("year", "2021", "#ffcc00"),
             ("composer", "Straße", "#8e8e93"), ("playlist", pid, "#123abc"), ("tab", "Hi-Res", "#c9a227"),
             ("song", "/m/b", "#ffffff"), ("song", "/m/b", None), ("genre", "indie pop", "#00ffaa"),
             ("nope", "x", "#ffffff"), ("song", "", "#ffffff"), ("year", "1999", "not a colour"),
             ("tab", "Old", "#000000"), ("tab", "Old", "")]
label_results = [[k, key, c, ops.set_label(k, key, c)] for k, key, c in label_ops]
label_lookups = [[k, key, ops.label_of(k, key)] for k, key in (
    ("song", "/m/a"), ("song", "/m/b"), ("genre", "INDIE POP"), ("artist", "june harbor"), ("album_artist", "émile"),
    ("composer", "STRASSE"), ("composer", "strasse"), ("year", "2021"), ("tab", "hi-res"), ("tab", "Hi-Res"),
    ("nope", "x"), ("playlist", pid))]
errors = []
for name in ("rock", "my jazz", "", "  "):
    try:
        ops.check_preset_name(name)
        errors.append([name, None])
    except playerstate.InvalidName as exc:
        errors.append([name, str(exc)])
with open(os.path.join(OUT, "python_playerstate.json"), "w", encoding="utf-8") as fh:
    json.dump({"messy": messy, "messy_out": ps.to_json(), "ops_out": ops.to_json(), "name_errors": errors,
               "lyrics_sizes": sizes, "lyrics_size_steps": size_steps,
               "lyrics_size_limits": [playerstate.LYRICS_SIZE_MIN, playerstate.LYRICS_SIZE_DEFAULT,
                                      playerstate.LYRICS_SIZE_MAX, playerstate.LYRICS_SIZE_STEP],
               "label_results": label_results, "label_lookups": label_lookups,
               "label_kinds": list(playerstate.LABEL_KINDS), "label_colors": playerstate.LABEL_COLORS},
              fh, indent=0, ensure_ascii=False)
print(len(cases), "biquads,", len(states), "states,", len(runs), "queue runs,", len(plays), "plays,",
      len(imports), "EQ files")


# ------------------------------------------------- tag editor, themes folder ---
import hashlib  # noqa: E402
import struct  # noqa: E402
import tempfile  # noqa: E402


def flac_file(comments, id3=b"", padding=64, vorbis=True):
    info = b"\x10\x00\x10\x00" + b"\x00" * 6 + b"\x0a\xc4\x42\xf0" + b"\x00" * 4 + b"\x00" * 16
    blocks = [(0, info)]
    if vorbis:
        vendor = b"reference libFLAC 1.4.3"
        body = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(comments))
        for c in comments:
            c = c.encode("utf-8")
            body += struct.pack("<I", len(c)) + c
        blocks.append((4, body))
    blocks.append((6, b"\x00\x00\x00\x03" + b"\x00" * 28))  # a PICTURE-like block that must stay
    if padding:
        blocks.append((1, b"\x00" * padding))
    out = id3 + b"fLaC"
    for i, (k, b) in enumerate(blocks):
        out += bytes([k | (0x80 if i == len(blocks) - 1 else 0)]) + len(b).to_bytes(3, "big") + b
    return out + b"\xff\xf8AUDIO-FRAMES" * 3


def id3_tag(major, frames, padding=32):
    body = b""
    for fid, text in frames:
        if isinstance(text, bytes):
            fb = text
        elif major == 4:
            fb = b"\x03" + text.encode("utf-8")
        else:
            fb = b"\x00" + text.encode("latin-1")
        size = (bytes([(len(fb) >> 21) & 0x7F, (len(fb) >> 14) & 0x7F, (len(fb) >> 7) & 0x7F, len(fb) & 0x7F])
                if major == 4 else struct.pack(">I", len(fb)))
        body += fid + size + b"\x00\x00" + fb
    body += b"\x00" * padding
    n = len(body)
    return b"ID3" + bytes([major, 0, 0]) + bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F]) + body


MP3_AUDIO = b"\xff\xfb\x90\x64" + b"MPEG-FRAME" * 4
flacs = {
    "plain": flac_file(["TITLE=Old", "ARTIST=One", "ARTIST=Two", "ALBUM ARTIST=AA", "ALBUMARTIST=AA2",
                        "TRACKNUMBER=2", "TRACKTOTAL=9", "DATE=2001", "LYRICS=[00:01.00] la", "genre=Pop"]),
    "no_padding": flac_file(["TITLE=Old", "DISCNUMBER=1", "TOTALDISCS=2"], padding=0),
    "no_vorbis": flac_file([], vorbis=False),
    "id3_front": flac_file(["TITLE=Old"], id3=id3_tag(3, [(b"TIT2", "Front")])),
}
mp3s = {
    "v23": id3_tag(3, [(b"TIT2", "Old"), (b"TPE1", "Art"), (b"TYER", "2001"), (b"TDAT", "2805"), (b"TRCK", "2/9"),
                       (b"USLT", b"\x00XXX\x00la la")]) + MP3_AUDIO,
    "v24": id3_tag(4, [(b"TIT2", "Old"), (b"TDRC", "2001-02"), (b"TPE2", "AA"), (b"TXXX", b"\x03k\x00v")],
                   padding=0) + MP3_AUDIO,
    "none": MP3_AUDIO,
    "v22": b"ID3\x02\x00\x00\x00\x00\x00\x10" + b"\x00" * 16 + MP3_AUDIO,
}
change_sets = [
    {"title": "Neue Ünïcode 日本", "album_artist": "", "date": "2020-05-28", "composer": "Bach",
     "track": "04/12", "disc": "1", "genre": "Jazz"},
    {"artist": "Solo", "track": "3", "disc": "", "date": "1999"},
    {"album": "  Spaced \n out  ", "date": "", "track": ""},
    {},
]


def written(fn, data, changes):
    try:
        return {"output": fn(data, tagedit.check_changes(changes)).hex()}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


tag_cases = []
for kind, files, fn in (("flac", flacs, tagedit.flac_with_tags), ("mp3", mp3s, tagedit.mp3_with_tags)):
    for name, data in files.items():
        for ch in change_sets:
            tag_cases.append(dict(kind=kind, name=name, input=data.hex(), changes=ch, **written(fn, data, ch)))
checks_tag = []
for ch in change_sets + [{"track": "x"}, {"disc": "1/x"}, {"date": "May 2004"}, {"title": "x" * 1001},
                         {"track": " 3 / 12 "}, {"track": "007"}, {"unknown": "1", "genre": "\tRock\t"}]:
    try:
        checks_tag.append({"changes": ch, "ok": list(tagedit.check_changes(ch).items())})
    except tagedit.TagError as exc:
        checks_tag.append({"changes": ch, "error": str(exc)})
commands = [{"src": src, "dst": "/tmp/out" + os.path.splitext(src)[1], "changes": ch,
             "args": tagedit.ffmpeg_args(src, "/tmp/out" + os.path.splitext(src)[1], tagedit.check_changes(ch))}
            for src in ("/m/a.m4a", "/m/b.OGG", "/m/c.aiff", "/m/d.wav", "/m/e.opus") for ch in change_sets[:2]]
cannot = {p: tagedit.cannot_edit(p) for p in ("a.ape", "b.DSF", "c.flac", "d.m4a", "e.tta")}
notes = []
for ch, got in ((change_sets[0], {"title": "Neue Ünïcode 日本", "track": "4/12", "date": "2020-05-28", "genre": "Jazz"}),
                (change_sets[0], {"title": "Neue Ünïcode 日本", "album_artist": "x", "date": "2020", "genre": "Jazz",
                                  "composer": "Bach", "track": "4", "disc": "1"}),
                (change_sets[1], {"artist": "Solo", "track": "3/9", "date": "1999"})):
    notes.append({"changes": tagedit.check_changes(ch), "tags": got, "note": tagedit.kept_note("x.wav", tagedit.check_changes(ch), got)})
commons = [[{"title": "A", "album": "X"}, {"title": "B", "album": " X "}], [{"genre": "Pop"}], []]
outcomes = [[tagedit.TagOutcome("a", True, ""), tagedit.TagOutcome("b", True, "b was saved, but x.")],
            [tagedit.TagOutcome("a", True, ""), tagedit.TagOutcome("b", False, "Could not."),
             tagedit.TagOutcome("c", False, "Could not.")],
            [tagedit.TagOutcome("a", False, "No.")], [tagedit.TagOutcome("a", True)]]
own = [themes.normalize({"name": "Neon", "dark": True, "colors": {}})]
folder_files = {"b.json": themes.export_theme(dict(themes.BUILTIN["Dark"], name="Dark")),
                "a.JSON": json.dumps({"name": "Neon", "dark": False, "colors": {"accent": "#00FF00"}}),
                "c.json": "nope", ".hidden.json": themes.export_theme(own[0]), "d.txt": themes.export_theme(own[0]),
                "e.json": "﻿" + themes.export_theme({"name": "Sunset", "dark": False,
                                                         "colors": {"accent": "#ff8800"}}),
                "f.json": themes.export_theme({"name": "Neon", "dark": True, "colors": {}})}
with tempfile.TemporaryDirectory() as d:
    for n, text in folder_files.items():
        with open(os.path.join(d, n), "w", encoding="utf-8") as fh:
            fh.write(text)
    loaded = [{"theme": {k: v for k, v in t.items() if k != "file"}, "file": os.path.basename(t["file"])}
              for t in themes.load_folder(own, d)]


def ogg_file(first, header_packets, serial=0x1234ABCD, audio_pages=3, other_serial=False):
    """An Ogg file laid out as FFmpeg writes one: the identification header on the
    first page, every other header packet on pages of its own, then audio pages."""
    out = tagedit._ogg_page(2, b"\x00" * 8, serial, 0, [len(first)], first)
    seq = 1
    for pkt in header_packets:
        pages = tagedit._ogg_pages_for([pkt], serial, seq)
        out += b"".join(pages)
        seq += len(pages)
    if other_serial:
        out += tagedit._ogg_page(2, b"\x00" * 8, serial + 1, 0, [5], b"OTHER")
    for i in range(audio_pages):
        body = bytes((i * 7 + k) % 256 for k in range(300))
        out += tagedit._ogg_page(4 if i == audio_pages - 1 else 0, struct.pack("<q", 1024 * (i + 1)), serial,
                                 seq, [255, 45], body)
        seq += 1
    return out


def vorbis_comment(magic, comments, tail):
    vendor = b"Lavf62.0.100"
    body = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(comments))
    for c in comments:
        c = c.encode("utf-8")
        body += struct.pack("<I", len(c)) + c
    return magic + body + tail


VORBIS_ID = b"\x01vorbis" + b"\x00\x00\x00\x00\x02\x44\xac\x00\x00" + b"\x00" * 12 + b"\xb8\x01"
OPUS_HEAD = b"OpusHead\x01\x02\x38\x01\x80\xbb\x00\x00\x00\x00\x00"
SETUP = b"\x05vorbis" + bytes(range(256)) * 12
small = ["TITLE=Old", "ARTIST=One", "ALBUMARTIST=AA", "TRACKNUMBER=2", "TRACKTOTAL=9", "DATE=2001"]
oggs = {
    "vorbis": ogg_file(VORBIS_ID, [vorbis_comment(b"\x03vorbis", small, b"\x01"), SETUP]),
    "vorbis_big": ogg_file(VORBIS_ID, [vorbis_comment(b"\x03vorbis", small + ["COMMENT=" + "z" * 70000],
                                                      b"\x01"), SETUP]),
    "opus": ogg_file(OPUS_HEAD, [vorbis_comment(b"OpusTags", small, b"\x00\x00padding")]),
    "speex": ogg_file(b"Speex   1.2" + b"\x00" * 69, [vorbis_comment(b"", small, b"")]),
    "two_streams": ogg_file(VORBIS_ID, [vorbis_comment(b"\x03vorbis", small, b"\x01")], other_serial=True),
    "not_ogg": b"RIFF....WAVEfmt ",
}
ogg_cases = []
for name, data in oggs.items():
    for ch in change_sets:
        case = {"name": name, "changes": ch}
        try:
            out = tagedit.ogg_with_tags(data, tagedit.check_changes(ch))
            case.update(sha1=hashlib.sha1(out).hexdigest(), size=len(out))
            if name == "vorbis":
                case["output"] = out.hex()
        except tagedit.TagError as exc:
            case["error"] = str(exc)
        ogg_cases.append(case)
crcs = {x: tagedit.ogg_crc(x.encode()) for x in ("", "123456789", "OggS")}

with open(os.path.join(OUT, "python_tagedit.json"), "w", encoding="utf-8") as fh:
    json.dump({"cases": tag_cases, "checks": checks_tag, "commands": commands, "cannot": cannot, "notes": notes,
               "commons": [{"tags": c, "values": tagedit.common_values(c)} for c in commons],
               "summaries": [{"outcomes": [[o.path, o.ok, o.message] for o in oc],
                              "summary": tagedit.save_summary(oc)} for oc in outcomes],
               "folder": {"own": own, "files": folder_files, "loaded": loaded},
               "ogg_inputs": {name: data.hex() for name, data in oggs.items()}, "ogg": ogg_cases,
               "crc": crcs}, fh, indent=0, ensure_ascii=False)

# ---- bit-perfect playback (2.1) ----
bp = bitperfect
bp_songs = [bp.Song(44100, 2, 16), bp.Song(48000, 1, 16), bp.Song(96000, 2, 24), bp.Song(192000, 2, 24),
            bp.Song(88200, 2, 24), bp.Song(176400, 2, 24), bp.Song(96000, 2, 32), bp.Song(48000, 2, 32, True),
            bp.Song(44100, 2, 64, True), bp.Song(44100, 2, 0, False, True), bp.Song(48000, 6, 24),
            bp.Song(44100, 2, 0), bp.Song(22050, 2, 8), bp.Song(352800, 2, 24), bp.Song(11025, 1, 16)]
ladder = (44100, 48000, 88200, 96000, 176400, 192000)
bp_mode_sets = [
    [],
    [bp.Mode(f, r, r, 2) for f in bp.FORMATS for r in ladder],
    [bp.Mode(bp.I16, r, r, 2) for r in (44100, 48000)],
    [bp.Mode(bp.F32, 8000, 384000, 0)],
    [bp.Mode(f, r, r, 2) for f in (bp.I24, bp.I16) for r in (48000, 96000)],
    [bp.Mode(bp.I32, 48000, 48000, 8)],
    [bp.Mode(bp.I16, 44100, 44100, 1)],
    [bp.Mode(bp.I32, 44100, 96000, 2), bp.Mode(bp.I16, 44100, 192000, 2)],
    [bp.Mode(bp.I24, 44100, 44100, 2)],
    [bp.Mode(bp.F32, r, r, 6) for r in (44100, 48000)] + [bp.Mode(bp.I32, 48000, 48000, 4)],
    [bp.Mode("u8", 48000, 48000, 2), bp.Mode(bp.I32, 96000, 96000, 0), bp.Mode(bp.F32, 0, 0, 2)],
]
accesses = [(bp.WASAPI, "Speakers"), (bp.ALSA, "hw:1,3"), (bp.COREAUDIO, "MacBook Speakers"),
            (bp.DIRECT, "Test output")]
bp_cases = []
for si, song in enumerate(bp_songs):
    for mi, ms in enumerate(bp_mode_sets):
        for failure in ("", bp.BUSY):
            if failure and mi not in (0, 1, 3):
                continue
            pl = bp.plan(song, ms, failure)
            access, dev = accesses[(si + mi) % len(accesses)]
            bp_cases.append({"song": si, "modes": mi, "failure": failure, "rate": pl.rate, "format": pl.format,
                             "channels": pl.channels, "reasons": pl.reasons, "resampled": pl.resampled,
                             "access": access, "device": dev, "status": bp.status(song, pl, access, dev),
                             "status_eq": bp.status(song, pl, access, dev, eq_on=True),
                             "output": bp.output_label(pl.rate, pl.format, bp.song_bits(song), pl.channels)})
cards_text = (" 0 [PCH            ]: HDA-Intel - HDA Intel PCH\n"
              "                      HDA Intel PCH at 0xf7f10000 irq 32\n"
              " 1 [DAC            ]: USB-Audio - USB Audio DAC\n"
              "                      Manufacturer USB Audio DAC at usb-0000:00:14.0-1, high speed\n"
              "  2 [NVidia         ]: HDA-Intel - HDA NVidia\n"
              " 3 [Loopback]: Loopback - Loopback\n"
              "x [bad]: no - number\n 4 [Odd] no colon\n 5 [NoDash         ]: just a name\n")
cards = bp.alsa_cards(cards_text)
devices = ["", "default", "hw:1", "hw:1,0", "hw:CARD=DAC,DEV=0", "plughw:CARD=PCH,DEV=3", "front:CARD=NVidia",
           "sysdefault:CARD=dac", "hw:CARD=Unknown,DEV=2", "hw:", "hw:x,y", "USB Audio DAC Analog Stereo",
           "HDA Intel PCH, ALC897 Analog", "HDA NVidia, HDMI 0", "Built-in Audio Analog Stereo",
           "Default ALSA Output (currently PipeWire Media Server)", "hw:2,7", "dmix:CARD=1,DEV=0", "Loopback",
           "loop", "HW:1", "Name with: colon"]
floats = [0.0, -0.0, 1.0, -1.0, 0.5, -0.5, 2.0, -3.0, 1.5 / 32768, 2.5 / 32768, -1.5 / 32768, 32767 / 32768,
          8388607 / 8388608, -8388608 / 8388608, 0.123456789, -0.987654321, 1e-9, 4.656612873077393e-10,
          float("nan"), float("inf"), float("-inf")]
with open(os.path.join(OUT, "python_bitperfect.json"), "w", encoding="utf-8") as fh:
    json.dump({"songs": [s.__dict__ for s in bp_songs],
               "mode_sets": [[m.__dict__ for m in ms] for ms in bp_mode_sets],
               "cases": bp_cases,
               "preferred": [bp.preferred_formats(s) for s in bp_songs],
               "rates": [bp.rates_offered(ms) for ms in bp_mode_sets],
               "pick": [{"song": r, "rates": rs, "rate": bp.pick_rate(r, rs)}
                        for r in (11025, 22050, 44100, 48000, 88200, 96000, 176400, 192000, 352800, 0)
                        for rs in ([], [48000], [44100, 48000], [48000, 96000], [44100, 48000, 88200, 96000],
                                   [8000, 16000, 32000], [192000, 384000], [44100])],
               "ints": [{"x": x, "i16": bp.int_sample(x, bp.I16), "i24": bp.int_sample(x, bp.I24),
                         "i32": bp.int_sample(x, bp.I32)} for x in floats],
               "labels": [{"format": f, "bits": b, "label": bp.format_label(f, b)}
                          for f in bp.FORMATS for b in (0, 8, 16, 24, 32)],
               "khz": [{"hz": hz, "text": bp.fmt_khz(hz)} for hz in (0, 8000, 11025, 22050, 44100, 48000, 88200,
                                                                     96000, 352800, 705600, 12345)],
               "failures": [{"detail": d, "text": bp.failure_reason(d)}
                            for d in ("", "  ", "ALSA: Device or resource busy", "it did not switch to 96 kHz.")],
               "idle": [bp.status(None, None, enabled=False), bp.status(None, None)],
               "cards_text": cards_text, "cards": cards,
               "devices": [{"device": d, "hw": bp.alsa_device(d, cards)} for d in devices],
               "texts": {"busy": bp.BUSY, "lossy": bp.LOSSY_NOTE, "eq_on": bp.EQ_ON, "eq_note": bp.EQ_NOTE,
                         "wasapi": bp.WASAPI_FORMAT_NOTE, "off": bp.OFF,
                         "idle": bp.ON_IDLE},
               # 2.3: the equalizer with bit-perfect playback
               "still": [{"rate": r, "access": a, "device": d, "text": bp.still_runs(r, a, d)}
                         for r in (44100, 96000) for a in (bp.WASAPI, bp.ALSA, bp.COREAUDIO, bp.DIRECT)
                         for d in ("", "hw:1,3", "Speakers")],
               "dither": [{"x": x, "d": d, "i16": bp.dithered_int_sample(x, bp.I16, d),
                           "i24": bp.dithered_int_sample(x, bp.I24, d), "i32": bp.dithered_int_sample(x, bp.I32, d)}
                          for x in floats for d in (0.0, 0.5, -0.5, 0.999, -0.999, 0.25, -0.75, 1.5)],
               "standard_rates": list(bp.STANDARD_RATES)}, fh, indent=0, ensure_ascii=False)
# ------------------------------------------------- covers kept on disk ---
# The names of saved covers (covers.cache_key/cache_name), which both apps share:
# made-up paths that do not exist (every size and time is -1), and real files with
# fixed sizes and times under a folder the Rust test makes itself ("{root}").
from realhires import covers  # noqa: E402

made_up = [
    (["/no/such/Music/Album/01 One.flac"], 176),
    (["/no/such/Music/Album/01 One.flac", "/no/such/Music/Album/02 Two.flac", "/no/such/Music/Album/CD2/03.flac",
      "/no/such/Music/Album/04 Four.flac", "/no/such/Music/Other/05.flac"], 176),
    (["/no/such/音楽/宇多田ヒカル/First Love/01 Automatic.flac", "/no/such/Música/Ça va/02 Été.mp3"], 220),
    (["/no/such/a b/cé.wav"] * 3, 480),
    (["/no/such/song.flac"], 44),
    ([], 150),
]
cover_names = [{"paths": paths, "size": size, "key": covers.cache_key(paths, size),
                "name": covers.cache_name(paths, size)} for paths, size in made_up]
cover_layout = {
    "files": [{"path": "Album A/01.flac", "bytes": 1234, "mtime_ns": 1_700_000_001_234_567_800},
              {"path": "Album A/02.flac", "bytes": 0, "mtime_ns": 1_700_000_002_000_000_000},
              {"path": "Album B/Disc 1/01.wav", "bytes": 44, "mtime_ns": 999_999_999_900},
              {"path": "Album B/Disc 2/02.wav", "bytes": 70000, "mtime_ns": 1_800_000_000_000_000_100}],
    "folders": [{"path": "Album A", "mtime_ns": 1_700_000_100_000_000_000},
                {"path": "Album B/Disc 1", "mtime_ns": 1_700_000_200_000_000_500},
                {"path": "Album B/Disc 2", "mtime_ns": 1_700_000_300_000_000_000}],
}
cover_files = []
with tempfile.TemporaryDirectory() as root:
    for f in cover_layout["files"]:
        p = root + "/" + f["path"]
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"\0" * f["bytes"])
        os.utime(p, ns=(f["mtime_ns"], f["mtime_ns"]))
    for f in cover_layout["folders"]:
        os.utime(root + "/" + f["path"], ns=(f["mtime_ns"], f["mtime_ns"]))
    for rel, size in ((["Album A/01.flac", "Album A/02.flac"], 176),
                      (["Album B/Disc 1/01.wav", "Album B/Disc 2/02.wav", "Album A/missing.flac"], 480)):
        paths = [root + "/" + r for r in rel]
        cover_files.append({"paths": rel, "size": size, "key": covers.cache_key(paths, size).replace(root, "{root}")})

with open(os.path.join(OUT, "python_covers.json"), "w", encoding="utf-8") as fh:
    json.dump({"version": covers.CACHE_VERSION, "paths": covers.CACHE_PATHS, "names": cover_names,
               "layout": cover_layout, "files": cover_files}, fh, indent=0, ensure_ascii=False)
# -------------------------------------- quality column, renaming groups ---
# python_library.json: the Quality column's tooltip and sort, the EQ preset match,
# and renaming a genre, composer or year
q_tracks = list(tracks) + [
    Track(path="/q/a.ogg", codec="vorbis", is_lossy=True, sample_rate=48000, bit_rate=0),
    Track(path="/q/b.opus", codec="opus", is_lossy=True, sample_rate=0, bit_rate=128499),
    Track(path="/q/c.wav", codec="pcm_s24le", sample_rate=0, bits=24),
    Track(path="/q/d.flac", codec="flac", sample_rate=88200, bits=0),
    Track(path="/q/e.wav", codec="pcm_f32le", sample_rate=192000, bits=32, is_float=True),
    Track(path="/q/f.mp3", codec="mp3", is_lossy=True, sample_rate=44100, bit_rate=320000),
]
quality = [{"track": x.to_json(), "quality": musiclib.quality(x), "detail": musiclib.quality_detail(x),
            "sort": list(musiclib.quality_sort(x))} for x in q_tracks]
dates = [[old, year, tagedit.renamed_date(old, year)] for old, year in (
    ("2001-05-03", "2004"), ("2001", "1999"), ("", "2004"), ("May 2001", "2004"), (" 1987-11 ", "1990"),
    ("12345", "2000"), ("199", "2000"), ("２００１", "2002"))]
group_names = []
for kind, name in (("genres", "  Rock   Pop "), ("genres", ""), ("composers", "Ann"), ("composers", " "),
                   ("years", "2004"), ("years", " 1999 "), ("years", "99"), ("years", "20045"),
                   ("years", "２００４"), ("years", ""), ("genres", "x" * 1001)):
    try:
        group_names.append({"kind": kind, "name": name, "ok": tagedit.check_group_name(kind, name)})
    except tagedit.TagError as exc:
        group_names.append({"kind": kind, "name": name, "error": str(exc)})
group_cases = [{"kind": k, "name": n, "tags": tg, "changes": tagedit.group_changes(k, n, tg)} for k, n, tg in (
    ("genres", "Jazz", {"genre": "jazz", "date": "2001"}), ("composers", "Ann", {}),
    ("years", "2004", {"date": "2001-05-03"}), ("years", "2004", {}), ("years", "1990", {"date": "Spring"}))]
pst = playerstate.PlayerState()
pst.eq = eq.apply_preset(eq.default_state(), "Rock")
pst.eq["enabled"] = False
pst.save_eq_preset("Quiet", pst.eq)
matches = []
for enabled, nudge in ((False, 0.0), (True, 0.0), (True, 0.5)):
    pst.eq = eq.apply_preset(eq.default_state(), "Rock")
    pst.eq["enabled"] = enabled
    pst.eq["graphic"]["gains"][0] += nudge
    matches.append({"eq": eq.normalize(pst.eq), "match": pst.matching_eq_preset()})
with open(os.path.join(OUT, "python_library.json"), "w", encoding="utf-8") as fh:
    json.dump({"quality": quality, "dates": dates, "group_names": group_names, "group_changes": group_cases,
               "presets": pst.to_json()["eq_presets"], "matches": matches}, fh, indent=0, ensure_ascii=False)

# ------------------------------------------------------------ covers (2.1) ---
import random  # noqa: E402
import zlib  # noqa: E402

from realhires import covers, embed  # noqa: E402


def png_image(w, h, color=2, bits=8, palette=0, noise=0):
    rng = random.Random(w * 1000 + h)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bits, color, 0, 0, 0))
    if palette:
        out += chunk(b"PLTE", bytes(range(palette * 3)))
    raw = bytes(rng.getrandbits(8) for _ in range(noise)) if noise else b"\x00" * h
    return out + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def jpeg_image(w, h, sof=0xC0, comps=3, fill=False):
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof_seg = bytes([0xFF, sof]) + struct.pack(">HBHHB", 8 + 3 * comps, 8, h, w, comps) + bytes(3 * comps)
    return b"\xff\xd8" + app0 + (b"\xff\xff" if fill else b"") + sof_seg + b"\xff\xda\x00\x08SCANDATA\xff\xd9"


images = {
    "png_rgb": png_image(3, 2),
    "png_rgba16": png_image(640, 480, color=6, bits=16),
    "png_gray": png_image(9, 9, color=0, bits=1),
    "png_palette": png_image(12, 7, color=3, bits=4, palette=5),
    "png_palette_no_plte": png_image(12, 7, color=3, bits=4),
    "jpeg": jpeg_image(1200, 1000),
    "jpeg_progressive": jpeg_image(800, 600, sof=0xC2, fill=True),
    "jpeg_gray": jpeg_image(31, 17, comps=1),
    "jpeg_no_sof": b"\xff\xd8\xff\xe0\x00\x04ab\xff\xda\x00\x02",
    "jpeg_short": b"\xff\xd8\xff",
    "gif": b"GIF89a\x01\x00\x01\x00\x00\x00\x00;",
    "bmp": b"BM" + b"\x00" * 30,
    "webp": b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8,
    "text": b"hello",
    "empty": b"",
    "big_png": png_image(150, 150, noise=150 * 150 * 3),
}
infos = {}
for name, data in images.items():
    info = covers.image_info(data)
    infos[name] = {"format": covers.image_format(data),
                   "info": None if info is None else [info.mime, info.width, info.height, info.depth, info.colors]}
checks_pic = []
for name, mime in (("png_rgb", ""), ("jpeg", "image/jpeg"), ("jpeg", "image/JPG"), ("png_rgb", "image/gif"),
                   ("gif", ""), ("text", "image/png"), ("png_palette_no_plte", "")):
    try:
        tagedit.check_picture(images[name], mime)
        checks_pic.append({"image": name, "mime": mime, "error": ""})
    except tagedit.TagError as exc:
        checks_pic.append({"image": name, "mime": mime, "error": str(exc)})


def picture_struct(kind, mime=b"image/png", desc=b"", payload=b"PIXELS"):
    return (struct.pack(">II", kind, len(mime)) + mime + struct.pack(">I", len(desc)) + desc
            + struct.pack(">IIIII", 1, 1, 24, 0, len(payload)) + payload)


def flac_with_blocks(extra, padding=64):
    base = flac_file(["TITLE=Old", "ARTIST=One"], padding=0)
    blocks, audio = embed._flac_blocks(base, 0)
    blocks = [b for b in blocks if b[0] != 6] + extra + ([(1, b"\x00" * padding)] if padding else [])
    out = b"fLaC"
    for i, (k, b) in enumerate(blocks):
        out += bytes([k | (0x80 if i == len(blocks) - 1 else 0)]) + len(b).to_bytes(3, "big") + b
    return out + base[audio:]


cover_flacs = {
    "plain": flacs["plain"],                     # its PICTURE-like block is a front cover
    "no_padding": flacs["no_padding"],
    "no_vorbis": flacs["no_vorbis"],
    "id3_front": flacs["id3_front"],
    "mixed": flac_with_blocks([(6, picture_struct(4, desc=b"back")), (2, b"APPLDATA"), (6, picture_struct(0)),
                               (6, picture_struct(3, b"image/jpeg")), (6, b"\x00\x00")]),
    "none": flac_with_blocks([], padding=10),
    "not_flac": b"OggS-not-a-flac",
}


def id3_flagged(major, frames, padding=16):
    body = b""
    for fid, flags, fb in frames:
        size = embed._to_syncsafe(len(fb)) if major == 4 else struct.pack(">I", len(fb))
        body += fid + size + flags + fb
    body += b"\x00" * padding
    return b"ID3" + bytes([major, 0, 0]) + embed._to_syncsafe(len(body)) + body


def apic(kind, mime=b"image/png"):
    return b"\x00" + mime + b"\x00" + bytes([kind]) + b"desc\x00" + b"PIXELS"


cover_mp3s = {
    "v23": id3_flagged(3, [(b"TIT2", b"\x00\x00", b"\x00Old"), (b"APIC", b"\x00\x00", apic(4)),
                           (b"APIC", b"\x00\x00", apic(0)), (b"APIC", b"\x00\x80", apic(3)),
                           (b"APIC", b"\x00\x00", apic(3, b"image/jpeg"))]) + MP3_AUDIO,
    "v24": id3_flagged(4, [(b"TIT2", b"\x00\x00", b"\x03Old"), (b"APIC", b"\x00\x00", apic(3)),
                           (b"APIC", b"\x00\x01", apic(3)), (b"TPE1", b"\x00\x00", b"\x03Art")], padding=0) + MP3_AUDIO,
    "no_apic": mp3s["v23"],
    "none": MP3_AUDIO,
    "v22": mp3s["v22"],
    "short_apic": id3_flagged(3, [(b"APIC", b"\x00\x00", b"\x00x"), (b"APIC", b"\x00\x00", b"")]) + MP3_AUDIO,
}


def mbp(kind):
    return "METADATA_BLOCK_PICTURE=" + tagedit.b64encode(picture_struct(kind)).decode()


cover_oggs = {
    "vorbis": ogg_file(VORBIS_ID, [vorbis_comment(b"\x03vorbis", small + [mbp(4), "COVERART=xyz", mbp(3), mbp(0),
                                                                          "metadata_block_picture=!!!!!!!!"], b"\x01"),
                                   SETUP]),
    "vorbis_plain": oggs["vorbis"],
    "opus": ogg_file(OPUS_HEAD, [vorbis_comment(b"OpusTags", small + [mbp(3)], b"\x00\x00padding")]),
    "speex": oggs["speex"],
}
picks = [("png_rgb", images["png_rgb"]), ("jpeg", images["jpeg"]), ("remove", None)]
cover_cases = []
for kind, files, fn in (("flac", cover_flacs, tagedit.flac_with_cover), ("mp3", cover_mp3s, tagedit.mp3_with_cover),
                        ("ogg", cover_oggs, tagedit.ogg_with_cover)):
    for name, data in files.items():
        for pick, image in picks:
            case = {"kind": kind, "name": name, "image": pick}
            try:
                out, n = fn(data, image)
                case.update(removed=n, sha1=hashlib.sha1(out).hexdigest(), size=len(out))
                if len(out) < 4000:
                    case["output"] = out.hex()
            except tagedit.TagError as exc:
                case["error"] = str(exc)
            cover_cases.append(case)
for kind, name, fn in (("ogg", "vorbis", tagedit.ogg_with_cover), ("flac", "mixed", tagedit.flac_with_cover),
                       ("mp3", "v24", tagedit.mp3_with_cover)):
    data = {"ogg": cover_oggs, "flac": cover_flacs, "mp3": cover_mp3s}[kind][name]
    out, n = fn(data, images["big_png"])  # a picture over several pages (Ogg), or a tag that grows
    cover_cases.append({"kind": kind, "name": name, "image": "big_png", "removed": n,
                        "sha1": hashlib.sha1(out).hexdigest(), "size": len(out)})
types = {"mbp": {c: tagedit.mbp_type(c.encode()) for c in (mbp(3), mbp(0), mbp(4), mbp(21), "METADATA_BLOCK_PICTURE=",
                                                            "METADATA_BLOCK_PICTURE=AAAA", "X=AAAAAwAA",
                                                            "METADATA_BLOCK_PICTURE=AAAA*wAA", "nothing")},
         "apic": [[major, flags.hex(), body.hex(), tagedit.apic_type(major, flags, body)]
                  for major, flags, body in ((3, b"\x00\x00", apic(3)), (3, b"\x00\x20", apic(3)),
                                             (4, b"\x00\x40", apic(3)), (4, b"\x00\x80", apic(4)),
                                             (3, b"\x00\x00", b"\x00image/png"), (3, b"\x00\x00", b""),
                                             (4, b"\x00\x00", b"\x01\x00\x03"))],
         "streams": [[a, c, tagedit.cover_stream(a, c)] for a in (True, False)
                     for c in ("", "Other", "Cover (front)", " cover  (FRONT) ", "Cover (back)", "Artist/performer")]}
cover_commands = []
for src in ("/m/a.m4a", "/m/b.AIFF", "/m/c.mp3"):
    for image in ("/tmp/realhires-cover-x.jpg", None):
        for streams in ([], [(1, True)], [(1, False), (2, True), (3, True)]):
            cover_commands.append({"src": src, "image": image, "streams": streams,
                                   "args": tagedit.ffmpeg_cover_args(src, image, "/tmp/out", streams)})
cannot_cover = {p: tagedit.cannot_hold_cover(p) for p in ("a.wav", "b.WV", "c.ape", "d.dsf", "e.flac", "f.M4A", "g.aif",
                                                           "h.opus", "i.wma", "j.mka", "noext", "k.caf")}
T = tagedit.TagOutcome
cover_summaries = []
for removing, oc, notes in ((False, [T("a", True), T("b", True)], []), (False, [T("a", True)], []),
                            (False, [], []), (False, [T("a", False, "WAV x."), T("b", False, "WAV x."), T("c", True)],
                                              [tagedit.folder_saved_note("/m/Album/cover.png")]),
                            (False, [T("a", False, "No.")], [tagedit.folder_failed_note("/m/cover.jpg", "denied")]),
                            (True, [T("a", True), T("b", True, "b had no cover inside.")],
                             [tagedit.folder_kept_note("/m/Folder.JPG")]),
                            (True, [T("a", True, "a had no cover inside.")], []),
                            (True, [T("a", True, "a."), T("b", True, "b.")], []),
                            (True, [T("a", True), T("b", True, "b."), T("c", True, "c."), T("d", False, "No.")], []),
                            (True, [T("a", True, "a."), T("d", False, "No.")], ["", "x"]),
                            (True, [T(str(i), True) for i in range(1200)], [])):
    cover_summaries.append({"removing": removing, "outcomes": [[o.path, o.ok, o.message] for o in oc], "notes": notes,
                            "summary": tagedit.cover_summary(oc, removing, notes)})
texts = {"where": [[k, p, covers.where_text(k, p)] for k, p in (("embedded", "/m/A/01 Song.flac"),
                                                                 ("folder", "/m/A/cover.jpg"),
                                                                 ("embedded", "日本.mp3"))],
         "details": [[w, h, f, n, covers.details_text(w, h, f, n)]
                     for w, h, f, n in ((1200, 1200, "JPEG", 251187), (0, 0, "", 12), (64, 48, "PNG", 1023),
                                        (12000, 9000, "WebP", 3 * 1024 * 1024), (5, 0, "GIF", 0))]}
with open(os.path.join(OUT, "python_covers_embed.json"), "w", encoding="utf-8") as fh:
    json.dump({"images": {k: v.hex() for k, v in images.items()}, "infos": infos, "checks": checks_pic,
               "flac_inputs": {k: v.hex() for k, v in cover_flacs.items()},
               "mp3_inputs": {k: v.hex() for k, v in cover_mp3s.items()},
               "ogg_inputs": {k: v.hex() for k, v in cover_oggs.items()}, "cases": cover_cases,
               "pictures": {k: tagedit.flac_picture(images[k]).hex() for k in ("png_rgb", "png_palette", "jpeg")},
               "apic_bodies": {k: tagedit.apic_body(images[k]).hex() for k in ("png_rgb", "jpeg")},
               "types": types, "commands": cover_commands, "cannot": cannot_cover, "summaries": cover_summaries,
               "texts": texts}, fh, indent=0, ensure_ascii=False)
print(len(cover_cases), "cover cases")

# ------------------------------------------- searching the web (2.3) ---
# python_search.json: Search Cover Online (covers.cover_search_url()), the Properties
# window's Search buttons (tagedit.web_search_url()) and what a copied text fills in
# (tagedit.clipboard_value())
cover_search = [{"args": list(a), "query": covers.cover_search_query(*a), "url": covers.cover_search_url(*a)}
                for a in (("The Paper Boats", "Mira Calloway", "City Rain", "Taxi Lights"),
                          ("", "Mira Calloway", "City Rain", "Taxi Lights"),
                          ("  ", " The  Paper\tBoats ", " Small Hours ", ""),
                          ("", "Mira Calloway", "", "Taxi Lights"),
                          ("The Paper Boats", "", "", "Taxi Lights"),
                          ("", "", "", "Taxi Lights"), ("", "", "", ""), ("", "", "City Rain", ""),
                          ("宇多田ヒカル", "", "初恋", ""), ("AC/DC", "", "Back in Black (Remastered) & More + 100% #1", ""),
                          ("Sigur Rós", "", "Ágætis byrjun", "x"), ("", "Björk", "", "Jóga \"Live\""),
                          ("a　b", "", "c d", ""), ("~-_.", "", "!*'();:@=$,?[]", ""))]
song = {"title": "Taxi Lights", "artist": "Mira Calloway", "album": "City Rain", "album_artist": "The Paper Boats"}
value_sets = [song, dict(song, artist=""), dict(song, artist=None, album_artist="The  Paper Boats"),
              {"title": None, "artist": None, "album": "Small Hours", "album_artist": "The Paper Boats"},
              {"title": "", "artist": "Mira Calloway", "album": "Small Hours", "album_artist": ""},
              {"title": None, "album": None, "album_artist": "The Paper Boats"}, {"album": "Small Hours"},
              {}, {"title": 'Say "Hello" (Live)', "artist": "Björk & Friends"},
              {"title": "\"\"", "album": "\"Heroes\"", "artist": "David Bowie"},
              {"title": "初恋", "artist": "宇多田ヒカル"}, {"title": "  ", "album": " Ágætis  byrjun ", "artist": "Sigur Rós"}]
web_search = []
for values in value_sets:
    for language in ("en", "ja", "日本語", "English", "fr"):
        for field in tagedit.FIELDS + ("unknown_field",):
            if language in ("English", "fr") and field not in ("composer", "date"):
                continue
            web_search.append({"field": field, "values": {k: (v or "") for k, v in values.items()},
                               "language": language, "query": tagedit.web_search_query(field, values, language),
                               "url": tagedit.web_search_url(field, values, language)})
names = {lang: {f: tagedit.search_name(f, lang) for f in tagedit.FIELDS + ("album_artist", "other_thing")}
         for lang in ("en", "ja", "日本語", "")}
clipboard = [[t, tagedit.clipboard_value(t)] for t in (
    "John Williams", "  John   Williams \n", "\n\n  \nHans Zimmer\nLisa Gerrard", "a\rb", "\r\n x \r\n",
    "  Ryuichi Sakamoto\x85more", "\x0b\x0c\x1c\x1d\x1eFirst", "tab\tinside　wide", "", "   ",
    "x" * 999 + " y", "x" * 1000 + "y", "x" * 1500, "é" * 1001, " " + "z" * 999 + "  more", "作曲：坂本龍一\n編曲")]
with open(os.path.join(OUT, "python_search.json"), "w", encoding="utf-8") as fh:
    json.dump({"cover": cover_search, "web": web_search, "names": names, "clipboard": clipboard,
               "watch": [tagedit.WATCH_SECONDS, tagedit.WATCH_POLL_MS]}, fh, indent=0, ensure_ascii=False)
print(len(cover_search), "cover searches,", len(web_search), "web searches")

# --------------------------------------------------------------- custom tabs (2.3) ---
from realhires import smarttabs  # noqa: E402

NOW = 1_764_000_000.0
DAY_NS = 86_400 * 10 ** 9
tab_tracks = [
    Track("/music/Jazz/Night/01 Blue Hour.flac", title="Blue Hour", artist="Mira Calloway", album="Night",
          album_artist="Mira Calloway", genre="Jazz", composer="Ann Lee", disc=1, track=1, year="2021",
          duration=312.5, codec="flac", sample_rate=96000, bits=24, channels=2, size=10, mtime_ns=int(NOW * 1e9) - 3 * DAY_NS),
    Track("/music/Jazz/Night/02 Échos.flac", title="Échos", artist="Mira Calloway", album="Night",
          album_artist="Mira Calloway", genre="jazz", disc=1, track=2, year="2021", duration=600.0, codec="flac",
          sample_rate=44100, bits=16, channels=2, size=10, mtime_ns=int(NOW * 1e9) - 40 * DAY_NS),
    Track("/music/Pop/Rain/03 rain.mp3", title="", artist="", album="Rain", album_artist="The Paper Boats",
          genre="Indie Pop", year="1999-05", duration=201.0, codec="mp3", sample_rate=44100, bits=0, is_lossy=True,
          channels=2, bit_rate=320500, size=10, mtime_ns=int(NOW * 1e9) - 30 * DAY_NS),
    Track("/music/Pop/Rain/04 Taxi Lights.m4a", title="Taxi Lights", artist="The Paper Boats; Mira Calloway",
          album="Rain", genre="Indie Pop", composer="Bach", year="19xx", duration=59.9, codec="aac",
          sample_rate=48000, bits=0, is_lossy=True, channels=2, bit_rate=255500, size=10,
          mtime_ns=int(NOW * 1e9) - 400 * DAY_NS),
    Track("/music/Classical/大地/05 交響曲.wav", title="交響曲", artist="東京フィル", album="大地",
          genre="Classical", composer="Mahler", year="2010", duration=3600.0, codec="pcm_f32le",
          sample_rate=192000, bits=32, is_float=True, channels=2, size=10, mtime_ns=int(NOW * 1e9) + DAY_NS),
    Track("/music/Classical/大地/06 Coda.aiff", title="  Coda  ", artist="TOKYO phil", album="大地",
          genre="", year="", duration=30.25, codec="pcm_s24be", sample_rate=48000, bits=24, channels=1, size=10,
          mtime_ns=int(NOW * 1e9) - 3 * DAY_NS),
]
tab_liked = ["/music/Jazz/Night/02 Échos.flac", "/music/Pop/Rain/03 rain.mp3"]
tab_disliked = ["/music/Pop/Rain/04 Taxi Lights.m4a"]
# 2.4: the moods of four of the songs (the others are not analysed yet)
from realhires import moods as moods_mod  # noqa: E402

tab_moods = {"/music/Jazz/Night/01 Blue Hour.flac": {"bpm": 72, "mood": "Sad", "tempo": "Slow"},
             "/music/Jazz/Night/02 Échos.flac": {"bpm": 128, "mood": "Happy", "tempo": "Up-tempo"},
             "/music/Pop/Rain/03 rain.mp3": {"bpm": 104, "mood": "Chill", "tempo": "Mid-tempo"},
             "/music/Classical/大地/05 交響曲.wav": {"bpm": 0, "mood": "Sad", "tempo": ""}}
tab_ctx = smarttabs.context(tab_liked, tab_disliked, NOW,
                            {p: moods_mod.Mood(bpm=m["bpm"], mood=m["mood"], tempo=m["tempo"]) for p, m in tab_moods.items()})
values = [{f: smarttabs.field_value(f, t, tab_ctx) for f in smarttabs.FIELDS} for t in tab_tracks]


def R(field, op, value):
    return {"field": field, "op": op, "value": value}


def T(name="T", rules=(), match="all", sort="title", descending=False, limit=0):
    return {"format": smarttabs.TAB_FORMAT, "version": 1, "name": name, "match": match, "rules": list(rules),
            "sort": sort, "descending": descending, "limit": limit}


eval_tabs = [T("Empty"), T("Empty any", match="any")] + list(smarttabs.TEMPLATES)
texts_for = {"title": ["blue hour", " BLUE", "rain", "", "交", "coda", "Échos", "échos"],
             "artist": ["mira calloway", "Mira", "paper", ""], "album": ["night", "大地", ""],
             "album_artist": ["", "the paper boats"], "genre": ["jazz", "pop", ""], "composer": ["", "bach"],
             "format": ["flac", "MP3", "PCM_"], "folder": ["/music/jazz/night", "/music/classical", "rain"],
             "quality": ["Hi-Res Lossless", "lossless", "Lossy"], "mood": ["Happy", "sad", "Chill", ""],
             "tempo": ["Up-tempo", "slow", "Mid-tempo", ""]}
for f, vals in texts_for.items():
    for v in vals:
        for op in smarttabs.TEXT_OPS:
            eval_tabs.append(T(f"{f} {op} {v}", [R(f, op, v)]))
nums_for = {"year": [0, 1999, 2021], "bits": [0, 16, 24], "sample_rate": [44100, 48000, 96000.0],
            "bit_rate": [0, 256, 320], "duration": [59.9, 600, 3600.0], "added_days": [-1, 3, 30, 30.0001],
            "bpm": [0, 72, 120, 128.0]}
for f, vals in nums_for.items():
    for v in vals:
        for op in smarttabs.NUMBER_OPS:
            eval_tabs.append(T(f"{f} {op} {v}", [R(f, op, v)]))
for f in smarttabs.YESNO_FIELDS:
    for v in (True, False):
        eval_tabs.append(T(f"{f} {v}", [R(f, "is", v)]))
eval_tabs += [
    T("any", [R("genre", "is", "jazz"), R("lossless", "is", False)], match="any"),
    T("all", [R("genre", "is", "jazz"), R("liked", "is", True)]),
    T("hires any", [R("bits", ">=", 24), R("sample_rate", ">", 48000)], match="any"),
]
for f in smarttabs.FIELDS:
    for desc in (False, True):
        eval_tabs.append(T(f"sort {f} {desc}", sort=f, descending=desc))
eval_tabs += [T("limit 2", sort="duration", descending=True, limit=2), T("limit 9", limit=9),
              T("limit 1 any", [R("lossless", "is", True), R("liked", "is", True)], match="any", limit=1)]
evals = []
for tab in eval_tabs:
    t = smarttabs.check_tab(tab)
    evals.append({"tab": tab, "songs": smarttabs.tab_songs(t, tab_tracks, tab_ctx),
                  "matches": [smarttabs.matches(t, x, tab_ctx) for x in tab_tracks]})

good = T("Good", [R("year", ">=", 2000)])
check_inputs = [
    None, [], "x", {}, {"format": "real-hi-res-theme", "version": 1, "name": "x"},
    {"format": smarttabs.TAB_FORMAT}, dict(good, version=0), dict(good, version="1"), dict(good, version=1.0),
    dict(good, version=True), dict(good, version=2), dict(good, version=10 ** 30), dict(good, name=""),
    dict(good, name="  "), dict(good, name=5), {k: v for k, v in good.items() if k != "name"},
    dict(good, name="  Spaced   name\tok "), dict(good, name="x" * 100), dict(good, match="some"),
    dict(good, match=None), dict(good, match=1), {k: v for k, v in good.items() if k not in ("match", "rules")},
    dict(good, rules={}), dict(good, rules=None), dict(good, rules=[R("year", ">=", 1)] * 101),
    dict(good, rules=[R("year", ">=", 1)] * 100), dict(good, rules=[5]), dict(good, rules=[[]]),
    dict(good, rules=[{"op": "is", "value": "x"}]), dict(good, rules=[R("", "is", "x")]),
    dict(good, rules=[R(5, "is", "x")]), dict(good, rules=[R("colour", "is", "red")]),
    dict(good, rules=[R("x" * 80, "is", "red")]), dict(good, rules=[{"field": "title", "value": "x"}]),
    dict(good, rules=[R("title", "", "x")]), dict(good, rules=[R("title", ">", "x")]),
    dict(good, rules=[R("year", "contains", 1)]), dict(good, rules=[R("liked", "!=", True)]),
    dict(good, rules=[{"field": "title", "op": "is"}]), dict(good, rules=[R("title", "is", 5)]),
    dict(good, rules=[R("title", "is", None)]), dict(good, rules=[R("year", "=", "2000")]),
    dict(good, rules=[R("year", "=", True)]), dict(good, rules=[R("year", "=", None)]),
    dict(good, rules=[R("year", "=", float("nan"))]), dict(good, rules=[R("year", "=", float("inf"))]),
    dict(good, rules=[R("year", "=", 10 ** 16)]), dict(good, rules=[R("year", "=", 10 ** 15)]),
    dict(good, rules=[R("year", "=", -(10 ** 15) - 1)]), dict(good, rules=[R("year", "=", 10 ** 30)]),
    dict(good, rules=[R("year", "=", 2.5)]), dict(good, rules=[R("liked", "is", 1)]),
    dict(good, rules=[R("liked", "is", "yes")]), dict(good, rules=[R("year", ">", 1), R("bits", "=", "x")]),
    dict(good, rules=[dict(R("title", "is", "x"), extra=1)]), dict(good, sort="colour"), dict(good, sort=None),
    dict(good, sort=3), dict(good, sort="y" * 70), dict(good, sort="added_days"), dict(good, descending=1),
    dict(good, descending=None), dict(good, descending=True), dict(good, limit=-1), dict(good, limit=1.5),
    dict(good, limit=10.0), dict(good, limit=True), dict(good, limit=100001), dict(good, limit=100000),
    dict(good, limit=None), {"format": smarttabs.TAB_FORMAT, "version": 1, "name": "Defaults"},
    dict(good, extra={"a": 1}), dict(good, rules=[R("quality", "is", "anything")]),
]
checks_tab = []
for x in check_inputs:
    try:
        checks_tab.append({"input": x, "tab": smarttabs.check_tab(x), "error": None})
    except smarttabs.TabError as exc:
        checks_tab.append({"input": x, "tab": None, "error": str(exc)})
known_tabs = [T("Hi-Res"), T("Rock & Roll"), T("Ünïcode")]
name_checks = []
for n, renaming in (("hi-res", ""), ("Hi-Res", "Hi-Res"), ("HI-RES", "Hi-Res"), ("  ", ""), ("new  one", ""),
                    ("ünïcode", ""), ("x" * 90, ""), ("Rock & Roll", "Hi-Res")):
    try:
        name_checks.append([n, renaming, smarttabs.check_name(n, known_tabs, renaming), None])
    except smarttabs.TabError as exc:
        name_checks.append([n, renaming, None, str(exc)])
uniques = [[n, taken, smarttabs.unique_name(n, taken)] for n, taken in (
    ("Hi-Res", []), ("Hi-Res", ["hi-res"]), ("Hi-Res", ["HI-RES", "Hi-Res 2"]), ("x" * 80, ["x" * 80]),
    ("y" * 79 + " ", ["y" * 79 + " "]), ("Ä", ["ä", "ä 2", "Ä 3"]))]
file_names = [[n, smarttabs.tab_file_name(n)] for n in (
    "Hi-Res", "a/b: c?", "CON", "con.json", "Com1", "LPT9 tab", "lpt", " .. ", "...hidden", "trailing. ",
    'q"u<o>t|e*s', "tab\x01name\x7f", "日本語のタブ", "  spaced   out  ", "", "x" * 120, "ß", "aux.", "back\\slash")]
text_tabs = list(smarttabs.TEMPLATES) + [T(
    "Ünïcode “quotes” \\ \"q\" 日本", [R("title", "contains", 'a "b" \\ c\n\t\x01'), R("year", ">=", 1999),
                                      R("duration", "<", 2.5), R("liked", "is", True), R("bits", "=", 10 ** 16)],
    match="any", sort="year", descending=True, limit=25)]
texts = [{"tab": t, "text": smarttabs.tab_text(t)} for t in text_tabs]
parses = [[f, text, smarttabs.parse_value(f, text)] for f in ("year", "title", "lossless") for text in (
    "24", " 24 ", "+5", "-0", "2.5", "1e3", "1E-2", ".5", "5.", "1,000", "abc", "", "  ", "0x10", "1_000",
    "999999999999999", "9999999999999999", "1e400", "nan", "inf", "２０", "- 5", "1.2.3", "e5", "+.5e+2")]
value_texts = [[v, smarttabs.value_text(v)] for v in (True, False, 0, 24, -3, 2.5, 30.0, 1e16, 1e-07, "text", "")]
changes = []
for rule, f in ((R("title", "contains", "x"), "artist"), (R("title", "contains", "x"), "year"),
                (R("year", ">=", 5), "bits"), (R("year", ">=", 5), "title"), (R("liked", "is", False), "disliked"),
                (R("liked", "is", False), "quality"), (R("title", "is", "Lossy"), "quality"),
                (R("title", "is", "nope"), "quality"), (R("quality", "is", "Lossless"), "title"),
                (R("year", "!=", 2.5), "duration"), (R("title", "starts_with", "a"), "lossless")):
    changes.append({"rule": rule, "field": f, "out": smarttabs.change_field(rule, f)})
labels = {"fields": smarttabs.FIELD_LABELS,
          "ops": {f: {op: smarttabs.op_label(f, op) for op in smarttabs.ops_for(f)} for f in smarttabs.FIELDS},
          "kinds": {f: smarttabs.kind_of(f) for f in smarttabs.FIELDS + ("nope",)},
          "defaults": {f: smarttabs.new_rule(f) for f in smarttabs.FIELDS},
          "match": smarttabs.MATCH_LABELS, "templates": smarttabs.TEMPLATES}

# the tabs folder: which files are tabs, and a series of saves, imports and deletes
folder_tab_files = {
    "b.json": smarttabs.tab_text(T("Hi-Res")), "A.JSON": smarttabs.tab_text(T("hi-res")),
    "c.json": "nope", "d.json": json.dumps({"format": smarttabs.TAB_FORMAT, "version": 3, "name": "Future"}),
    ".hidden.json": smarttabs.tab_text(T("Hidden")), "e.txt": smarttabs.tab_text(T("Text")),
    "f.json": "﻿" + smarttabs.tab_text(T("Bom")), "g.json": smarttabs.tab_text(T("HI-RES")),
    "h.json": json.dumps(dict(T("Bad"), rules=[R("colour", "is", "red")])), "i.json": "[1, 2]",
    "j.json": json.dumps({"format": smarttabs.TAB_FORMAT, "version": 1, "name": "Short"}, indent=None),
}
with tempfile.TemporaryDirectory() as d:
    for n, text in folder_tab_files.items():
        with open(os.path.join(d, n), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    os.makedirs(os.path.join(d, "dir.json"))
    with open(os.path.join(d, "big.json"), "wb") as fh:
        fh.write(b" " * (smarttabs.MAX_FILE_BYTES + 1))
    with open(os.path.join(d, "latin1.json"), "wb") as fh:
        fh.write(b"\xff\xfe{")
    got, skipped = smarttabs.load_tabs(d)
    folder_loaded = {"tabs": [dict({k: v for k, v in t.items() if k != "file"}, file=os.path.basename(t["file"]))
                              for t in got], "skipped": [list(s) for s in skipped]}


def listing(d):
    out = {}
    for n in sorted(os.listdir(d)):
        with open(os.path.join(d, n), encoding="utf-8") as fh:
            out[n] = fh.read()
    return out


ops = [
    {"op": "save", "tab": T("Hi-Res", [R("quality", "is", "Hi-Res Lossless")])},
    {"op": "save", "tab": T("Hi-Res", [R("bits", ">=", 24)])},  # (never names that differ only in case:
    {"op": "save", "tab": T("Renamed", [R("bits", ">=", 24)]), "old": "Hi-Res 2.json"},  # file systems differ)
    {"op": "save", "tab": T("Hi-Res", [R("lossless", "is", True)]), "old": "Hi-Res.json"},
    {"op": "save", "tab": T("a/b: c?")},
    {"op": "save", "tab": T("Bad", [R("year", "contains", "x")])},
    {"op": "save", "tab": T("  ")},
    {"op": "import", "file": "shared.json", "text": smarttabs.tab_text(T("Hi-Res", [R("year", ">", 2000)]))},
    {"op": "import", "file": "again.json", "text": smarttabs.tab_text(T("Hi-Res"))},
    {"op": "import", "file": "x" * 3 + ".json", "text": smarttabs.tab_text(T("Renamed 2"))},
    {"op": "import", "file": "theme.json", "text": themes.export_theme(themes.normalize({"name": "Neon", "colors": {}}))},
    {"op": "import", "file": "broken.json", "text": "{"},
    {"op": "import", "file": "inside", "text": ""},
    {"op": "delete", "file": "Renamed.json"},
    {"op": "delete", "file": "missing.json"},
    {"op": "save", "tab": T("日本語のタブ", [R("artist", "contains", "東京")], sort="year", descending=True, limit=3)},
]
op_results = []
with tempfile.TemporaryDirectory() as root:
    d = os.path.join(root, "tabs")
    outside = os.path.join(root, "outside")
    os.makedirs(outside)
    for o in ops:
        res = {}
        try:
            if o["op"] == "save":
                old = os.path.join(d, o["old"]) if o.get("old") else None
                t = smarttabs.save_tab(o["tab"], d, old)
                res = {"name": t["name"], "file": os.path.basename(t["file"])}
            elif o["op"] == "import":
                if o["file"] == "inside":
                    src = os.path.join(d, "Hi-Res.json")
                else:
                    src = os.path.join(outside, o["file"])
                    with open(src, "w", encoding="utf-8", newline="") as fh:
                        fh.write(o["text"])
                t = smarttabs.import_tab(src, d)
                res = {"name": t["name"], "file": os.path.basename(t["file"])}
            else:
                res = {"deleted": smarttabs.delete_tab({"file": os.path.join(d, o["file"])})}
        except smarttabs.TabError as exc:
            res = {"error": str(exc)}
        res["listing"] = listing(d) if os.path.isdir(d) else {}
        op_results.append(res)

with open(os.path.join(OUT, "python_tabs.json"), "w", encoding="utf-8") as fh:
    json.dump({"now": NOW, "liked": tab_liked, "disliked": tab_disliked, "moods": tab_moods,
               "tracks": [x.to_json() for x in tab_tracks], "values": values, "evals": evals,
               "checks": checks_tab, "names": name_checks, "uniques": uniques, "file_names": file_names,
               "texts": texts, "parses": parses, "value_texts": value_texts, "changes": changes, "labels": labels,
               "folder": folder_loaded, "ops": ops, "op_results": op_results},
              fh, indent=0, ensure_ascii=False)
print(len(evals), "tab evaluations,", len(checks_tab), "tab checks,", len(op_results), "tab file operations")

# ------------------------------------------------- where songs were played (2.4) ---
from realhires import places  # noqa: E402

geo_answers = [
    places_answer := {"ipVersion": 4, "ipAddress": "198.51.100.7", "latitude": 35.68951, "longitude": 139.69171,
                      "countryName": "Japan", "countryCode": "JP", "cityName": "Shinjuku", "regionName": "Tokyo"},
    {"ip": "198.51.100.8", "success": True, "country": "Japan", "country_code": "jp", "region": "Osaka",
     "city": "Osaka", "latitude": 34.6937378, "longitude": 135.5021651},
    {"city": "  San\tFrancisco \n(Financial District) ", "country_name": "United States", "country": "US",
     "country_code": "usa", "latitude": "37.79", "longitude": -0.004},
    {"countryName": "X" * 300, "latitude": 91, "longitude": True},
    {"cityName": "A\x00B\x7fC", "regionName": None, "region": "Kantō", "latitude": 1e308, "longitude": -180},
    {"cityName": "", "regionName": "", "countryName": "   "},
    {"success": False, "message": "Reserved range"},
    {"success": False},
    {"cityName": 5, "countryName": ["Japan"], "latitude": 12.345, "longitude": 0.005},
    {"cityName": "Ünïcødé  名古屋", "countryName": "日本", "countryCode": "JP", "latitude": -33.8688, "longitude": 151.209},
    [1, 2],
    "Tokyo",
]
geo_parsed = []
for a in geo_answers:
    try:
        p = places.parse_place(a)
        geo_parsed.append({"place": p.to_json(), "text": places.place_text(p), "key": places.place_key(p)})
    except places.PlaceError as exc:
        geo_parsed.append({"error": str(exc)})
coords = [[v, lim, places.coordinate(v, lim)] for v, lim in
          ((35.68951, 90), (-0.004, 180), (0.005, 180), (0.015, 180), (2.675, 90), (-90, 90), (90.0001, 90),
           (180, 180), (-180.5, 180), (1.005, 90), (139.69171, 180))]
texts = [[list(x), places.place_text(places.Place(*x)), places.place_key(places.Place(*x))] for x in
         (("Tokyo", "tokyo", "Japan"), ("Singapore", "Singapore", "Singapore"), ("", "", "Japan"),
          ("Straße", "STRASSE", "Deutschland"), ("Paris", "Île-de-France", "France"), ("", "Texas", "Texas"))]


def gplay(path, started, seconds, place, title="", artist="", album_artist="", duration=200.0):
    return stats.Play(path, started, seconds, duration, title, artist, album_artist=album_artist, place=place)


TOKYO_P = places.Place("Tokyo", "Tokyo", "Japan", "JP", 35.69, 139.69)
OSAKA_P = places.Place("Osaka", "Osaka", "Japan", "JP", 34.69, 135.5)
place_plays = [gplay("/m/a.flac", 1_700_000_000, 120, TOKYO_P, "Alpha", "Ann"),
               gplay("/m/b.flac", 1_700_000_600, 10, TOKYO_P, "Beta"),
               gplay("/m/a.flac", 1_700_003_600, 200, places.Place("tokyo", "TOKYO", "japan"), "Alpha", "Ann"),
               gplay("/m/c.flac", 1_700_090_000, 300, OSAKA_P, "Gamma", "", "Band", 250.0),
               gplay("/m/d.flac", 1_700_090_100, 999, None, "Delta"),
               gplay("/m/e.flac", 1_700_090_200, 25, OSAKA_P, " ", "", "", 40.0),
               gplay("/m/f.flac", 1_700_090_300, 61, places.Place(country="Japan"), "Zeta", "Zed"),
               gplay("/m/g.flac", 1_700_090_400, 61, places.Place("Ōsaka", "", "Japan"), "Eta", "Zed"),
               gplay("/m/c.flac", 1_700_090_500, 30, OSAKA_P, "Gamma", "", "Band", 250.0)]
period = places.period_places(place_plays)
place_song_lists = {r["key"]: places.place_songs(place_plays, r["key"]) for r in period}
day_rows = [[y, m, d, stats.day_plays(place_plays, y, m, d, localtime=tokyo)]
            for y, m, d in ((2023, 11, 15), (2023, 11, 16))]
# when a lookup is due: (now, playing, not_before) -> due; then begin/finish steps
cache = places.PlaceCache()
cache_steps = []
for step in (("due", 0.0, False, 0.0), ("begin", 0.0), ("due", 0.0, True, 0.0), ("finish", True, 1000.0),
             ("due", 7199.0, True, 0.0), ("due", 7200.0, False, 0.0), ("due", 7200.0, True, 7300.0),
             ("due", 7300.0, True, 7300.0), ("begin", 7300.0), ("finish", False, 9000.0),
             ("due", 7400.0, True, 0.0), ("due", 14500.0, True, 0.0), ("reset",), ("due", 3.0, False, 5.0),
             ("due", 5.0, False, 5.0)):
    if step[0] == "due":
        out = cache.due(step[1], step[2], step[3])
    elif step[0] == "begin":
        out = cache.begin(step[1])
    elif step[0] == "finish":
        cache.finish(TOKYO_P if step[1] else None, "" if step[1] else "It failed.", wall=step[2])
        out = None
    else:
        cache.reset()
        out = None
    cache_steps.append({"step": list(step), "out": out, "place": cache.place.to_json() if cache.place else None,
                        "error": cache.error, "looked_up": cache.looked_up, "busy": cache.busy, "last": cache.last})


class GeoClock:
    def __init__(self):
        self.t = 100.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def geo_run(script):
    """Lookups through a fake transport: each answer is [status, headers, body] or ["network", why]
    or ["invalid", why]; returns what each lookup gave and how the client waited."""
    todo = [list(a) for a in script["answers"]]
    seen = []

    def transport(url, headers, timeout):
        seen.append([url, sorted(headers)])
        a = todo.pop(0)
        if a[0] == "network":
            raise OSError(a[1])
        if a[0] == "invalid":
            raise ValueError(a[1])
        return a[0], a[1], a[2].encode("utf-8")

    clock = GeoClock()
    c = places.GeoClient(script.get("server", places.DEFAULT_SERVER), transport=transport, clock=clock,
                         sleep=clock.sleep)
    results = []
    for advance in script["lookups"]:
        clock.t += advance
        try:
            results.append({"place": c.lookup().to_json()})
        except places.PlaceError as exc:
            results.append({"error": str(exc)})
        results[-1].update(requests=c.requests, not_before=c.not_before, t=clock.t)
    return {"script": script, "results": results, "slept": clock.slept, "seen": seen}


import json as _json  # noqa: E402

GOOD = [200, {}, _json.dumps(places_answer)]
geo_runs = [geo_run(s) for s in (
    {"answers": [GOOD, GOOD], "lookups": [0, 0]},
    {"answers": [GOOD, GOOD], "lookups": [0, 5]},
    {"answers": [[429, {"retry-after": "120"}, ""], GOOD, [503, {}, ""]], "lookups": [0, 60, 60, 0]},
    {"answers": [[500, {}, ""], ["network", "connection refused"], [502, {}, ""]], "lookups": [0]},
    {"answers": [["network", "no route"]] * 3, "lookups": [0]},
    {"answers": [[500, {}, ""], GOOD], "lookups": [0]},
    {"answers": [[403, {}, "no"]], "lookups": [0]},
    {"answers": [[404, {}, ""]], "lookups": [0]},
    {"answers": [[200, {}, "<html>"]], "lookups": [0]},
    {"answers": [[200, {}, '{"ipAddress": "1.2.3.4"}']], "lookups": [0]},
    {"answers": [[200, {}, '{"success": false, "message": "Reserved range"}']], "lookups": [0]},
    {"answers": [["invalid", "unknown url type: ftp"]], "lookups": [0], "server": "ftp://example.org/"},
    {"answers": [[503, {"retry-after": "soon"}, ""], [429, {"retry-after": "99999999"}, ""]],
     "lookups": [0, 600, 0]},
    {"answers": [GOOD], "lookups": [0], "server": "  ipwho.is/  "},
)]
servers = [[s, places.normalize_server(s), places.host_of(s), places.is_official(s)] for s in
           ("", "  ", places.DEFAULT_SERVER, "freeipapi.com", "https://FREE.freeipapi.com./x", "ipwho.is/",
            "http://127.0.0.1:5000/api/v1/json/", "https://notfreeipapi.com/")]
with open(os.path.join(OUT, "python_places.json"), "w", encoding="utf-8") as fh:
    json.dump({"default_server": places.DEFAULT_SERVER, "constants": {
        "min_interval": places.MIN_INTERVAL_S, "max_attempts": places.MAX_ATTEMPTS, "backoff": places.BACKOFF_S,
        "default_retry_after": places.DEFAULT_RETRY_AFTER_S, "max_retry_after": places.MAX_RETRY_AFTER_S,
        "refresh": places.REFRESH_S, "max_name": places.MAX_NAME},
        "answers": geo_answers, "parsed": geo_parsed, "coordinates": coords, "texts": texts,
        "plays": [dict(p.__dict__, place=p.place.to_json() if p.place else None) for p in place_plays],
        "period": period, "songs": place_song_lists, "days": day_rows, "cache": cache_steps, "runs": geo_runs,
        "servers": servers, "retry_after": [[h, places.retry_after(h)] for h in (
            {}, {"retry-after": "120"}, {"retry-after": " 7.5 "}, {"retry-after": "-5"}, {"retry-after": "x"},
            {"retry-after": "1e9"}, {"retry-after": "nan"})]},
        fh, indent=0, ensure_ascii=False)
print(len(geo_parsed), "geolocation answers,", len(period), "places,", len(geo_runs), "client runs")
