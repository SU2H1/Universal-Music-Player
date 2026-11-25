//! Moods, tempo and key (2.4) against Python's answers (`rust/tools/gen_mood_vectors.py`
//! → `python_moods.json`): the invented songs of `tests/mood_songs.py`, made here with
//! the same integer arithmetic, so both versions analyse the very same samples; then
//! the labels, keys, tables and texts.  The last tests decode a WAV through FFmpeg and
//! use the `moods` table of a library database.

use std::collections::HashMap;

use realhires::json::{self, Value};
use realhires::moods::{self, Mood, MoodStore};
use realhires::musiclib::Track;

fn load() -> Value {
    let path = format!("{}/tests/python_moods.json", env!("CARGO_MANIFEST_DIR"));
    json::parse(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn arr<'a>(v: &'a Value, key: &str) -> &'a Vec<Value> {
    v.get(key).unwrap_or_else(|| panic!("no {key}")).as_array().unwrap()
}

fn num(v: &Value, key: &str) -> f64 {
    v.get(key).unwrap_or_else(|| panic!("no {key}")).as_f64().unwrap()
}

fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).unwrap_or_else(|| panic!("no {key}")).as_str().unwrap()
}

fn close(a: f64, b: f64, what: &str) {
    let tol = 1e-9 * a.abs().max(b.abs()).max(1e-3);
    assert!((a - b).abs() <= tol, "{what}: {a} != {b}");
}

// ----------------------------------------------------- tests/mood_songs.py again ---

const NOTE_MHZ: [i64; 12] = [261626, 277183, 293665, 311127, 329628, 349228, 369994, 391995, 415305, 440000, 466164, 493883];

fn note(name: &str) -> usize {
    ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"].iter().position(|n| *n == name).unwrap()
}

fn progression(name: &str) -> [[(&'static str, i64); 4]; 4] {
    if name == "major" {
        [
            [("C", 2), ("C", 4), ("E", 4), ("G", 4)],
            [("F", 2), ("F", 4), ("A", 4), ("C", 5)],
            [("G", 2), ("G", 4), ("B", 4), ("D", 5)],
            [("C", 2), ("C", 4), ("E", 4), ("G", 4)],
        ]
    } else {
        [
            [("A", 2), ("A", 3), ("C", 4), ("E", 4)],
            [("D", 2), ("D", 4), ("F", 4), ("A", 4)],
            [("E", 2), ("E", 4), ("G", 4), ("B", 4)],
            [("A", 2), ("A", 3), ("C", 4), ("E", 4)],
        ]
    }
}

fn period(name: &str, octave: i64, sr: i64) -> i64 {
    let f = NOTE_MHZ[note(name)];
    let (num, den) = if octave >= 4 { (sr * 1000, f << (octave - 4)) } else { ((sr * 1000) << (4 - octave), f) };
    (2 * num + den).div_euclid(2 * den)
}

fn triangle(n: i64, p: i64, amp: i64) -> i64 {
    (amp * 4 * (2 * n.rem_euclid(p) - p).abs()).div_euclid(2 * p) - amp
}

fn song(bpm: i64, prog: &str, seconds: i64, sr: i64, tone: i64, click: i64, gain: i64) -> Vec<i16> {
    let total = seconds * sr;
    let mut out = vec![0i64; total as usize];
    let chords = progression(prog);
    let mut bar = 0i64;
    loop {
        let start = bar * 4 * 60 * sr / bpm;
        if start >= total {
            break;
        }
        let end = total.min((bar + 1) * 4 * 60 * sr / bpm);
        for (i, (name, octave)) in chords[(bar % 4) as usize].iter().enumerate() {
            let amp = if i == 0 { tone * 3 / 2 } else { tone };
            let p = period(name, *octave, sr);
            for n in start..end {
                out[n as usize] += triangle(n, p, amp);
            }
        }
        bar += 1;
    }
    let length = sr / 100;
    let mut x: i64 = 12345;
    let mut beat = 0i64;
    loop {
        let at = beat * 60 * sr / bpm;
        if at >= total {
            break;
        }
        for k in 0..length.min(total - at) {
            x = (1103515245 * x + 12345).rem_euclid(1 << 31);
            let noise = (x >> 15) - 32768;
            let env = click * (length - k) / length;
            out[(at + k) as usize] += (noise * env).div_euclid(32768);
        }
        beat += 1;
    }
    out.iter().map(|v| (v * gain).div_euclid(100).clamp(-32768, 32767) as i16).collect()
}

fn lcg_noise(seconds: i64, amp: i64) -> Vec<i16> {
    let mut x: i64 = 777;
    (0..seconds * 22050)
        .map(|_| {
            x = (1103515245 * x + 12345).rem_euclid(1 << 31);
            (((x >> 15) - 32768) * amp).div_euclid(32768) as i16
        })
        .collect()
}

fn floats(s: &[i16]) -> Vec<f64> {
    s.iter().map(|v| *v as f64 / 32768.0).collect()
}

fn check_mood(got: &Mood, want: &Value, what: &str) {
    assert_eq!(got.bpm, num(want, "bpm") as i64, "{what}: bpm");
    for (g, k) in [(&got.key, "key"), (&got.mode, "mode"), (&got.tempo, "tempo"), (&got.energy_label, "energy_label"), (&got.mood, "mood")] {
        assert_eq!(g, text(want, k), "{what}: {k}");
    }
    assert_eq!(got.summary(), text(want, "summary"), "{what}: summary");
    close(got.energy, num(want, "energy"), &format!("{what}: energy"));
    close(got.brightness, num(want, "brightness"), &format!("{what}: brightness"));
    close(got.key_strength, num(want, "key_strength"), &format!("{what}: key strength"));
    close(got.seconds, num(want, "seconds"), &format!("{what}: seconds"));
}

fn check_analysis(x: &[f64], want: &Value, what: &str) {
    check_mood(&moods::analyse_samples(x), want, what);
    let (env, _) = moods::onset_envelope(x);
    assert_eq!(env.len() as f64, num(want, "env_len"), "{what}: envelope length");
    close(env.iter().sum(), num(want, "env_sum"), &format!("{what}: envelope sum"));
    close(moods::tempo(&env), num(want, "tempo_exact"), &format!("{what}: tempo"));
    let r = moods::autocorrelation(&env, moods::MULTIPLES * moods::LAG_MAX + 2);
    for (lag, w) in [1usize, 25, 40, 60, 80, 103, 200, 400].iter().zip(arr(want, "r")) {
        assert!((r[*lag] - w.as_f64().unwrap()).abs() < 1e-12, "{what}: r[{lag}] {} != {}", r[*lag], w.as_f64().unwrap());
    }
    for (g, w) in moods::chroma(x).iter().zip(arr(want, "chroma")) {
        close(*g, w.as_f64().unwrap(), &format!("{what}: chroma"));
    }
}

#[test]
fn constants_and_chroma_bins_match_python() {
    let data = load();
    let c = data.get("constants").unwrap();
    assert_eq!(num(c, "lag_min") as usize, moods::LAG_MIN);
    assert_eq!(num(c, "lag_max") as usize, moods::LAG_MAX);
    assert_eq!(num(c, "trend_frames") as usize, moods::TREND_FRAMES);
    assert_eq!(num(c, "fps"), moods::FPS);
    assert_eq!(num(c, "analyser_version") as i64, moods::ANALYSER_VERSION);
    assert_eq!((60.0 * moods::FPS / moods::MAX_BPM).ceil() as usize, moods::LAG_MIN);
    assert_eq!((60.0 * moods::FPS / moods::MIN_BPM).floor() as usize, moods::LAG_MAX);
    let want: Vec<i64> = arr(&data, "chroma_bins").iter().map(|v| v.as_i64().unwrap()).collect();
    assert_eq!(moods::chroma_bins(), want);
}

#[test]
fn invented_songs_get_python_labels_and_bpm() {
    let data = load();
    for s in arr(&data, "songs") {
        let x = floats(&song(num(s, "bpm") as i64, text(s, "progression"), num(s, "seconds") as i64, 22050, 6000, 9000, num(s, "gain") as i64));
        let want = s.get("out").unwrap();
        check_analysis(&x, want, text(s, "name"));
        assert!((moods::analyse_samples(&x).bpm - num(s, "bpm") as i64).abs() <= 2, "{}", text(s, "name"));
    }
    for v in arr(&data, "variants") {
        let what = format!("variant {} {}", num(v, "bpm"), text(v, "progression"));
        let x = floats(&song(
            num(v, "bpm") as i64,
            text(v, "progression"),
            num(v, "seconds") as i64,
            22050,
            num(v, "tone") as i64,
            num(v, "click") as i64,
            num(v, "gain") as i64,
        ));
        check_analysis(&x, v.get("out").unwrap(), &what);
    }
    let edge = data.get("edge").unwrap();
    check_analysis(&floats(&lcg_noise(8, 6000)), edge.get("noise").unwrap(), "noise");
    check_mood(&moods::analyse_samples(&vec![0.0; 22050 * 3]), edge.get("silence").unwrap(), "silence");
    check_mood(&moods::analyse_samples(&vec![0.1; 1000]), edge.get("short").unwrap(), "short");
    check_mood(&moods::analyse_samples(&[]), edge.get("empty").unwrap(), "empty");
    let quiet: Vec<f64> = song(120, "major", 4, 22050, 6000, 9000, 0).iter().map(|v| *v as f64).collect();
    check_mood(&moods::analyse_samples(&quiet), edge.get("quiet").unwrap(), "quiet");
}

#[test]
fn keys_labels_and_texts_match_python() {
    let data = load();
    for k in arr(&data, "keys") {
        let c: Vec<f64> = arr(k, "chroma").iter().map(|v| v.as_f64().unwrap()).collect();
        let (pc, mode, strength) = moods::key_of(&c);
        assert_eq!((pc as f64, mode), (num(k, "pc"), text(k, "mode")), "{c:?}");
        close(strength, num(k, "strength"), "key strength");
    }
    for case in arr(&data, "mood_cases") {
        let c = case.as_array().unwrap();
        let got = moods::mood_label(c[0].as_str().unwrap(), c[1].as_f64().unwrap(), c[2].as_f64().unwrap(), c[3].as_str().unwrap(), c[4].as_str().unwrap());
        assert_eq!(got, c[5].as_str().unwrap(), "{c:?}");
    }
    for p in arr(&data, "priors") {
        let p = p.as_array().unwrap();
        close(moods::prior(p[0].as_f64().unwrap()), p[1].as_f64().unwrap(), "prior");
    }
    for t in arr(&data, "tempo_labels") {
        let t = t.as_array().unwrap();
        assert_eq!(moods::tempo_label(t[0].as_i64().unwrap()), t[1].as_str().unwrap());
    }
    for e in arr(&data, "energy_labels") {
        let e = e.as_array().unwrap();
        assert_eq!(moods::energy_label(e[0].as_f64().unwrap()), e[1].as_str().unwrap());
    }
    for s in arr(&data, "stretches") {
        let s = s.as_array().unwrap();
        let want = s[1].as_array().unwrap();
        assert_eq!(moods::stretch(s[0].as_f64().unwrap()), (want[0].as_i64().unwrap(), want[1].as_i64().unwrap()), "{s:?}");
    }
    for c in arr(&data, "commands") {
        let c = c.as_array().unwrap();
        let want: Vec<String> = c[3].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
        assert_eq!(moods::decode_args(c[0].as_str().unwrap(), c[1].as_i64().unwrap(), c[2].as_i64().unwrap()), want);
    }
    let s = data.get("samples").unwrap();
    let bytes: Vec<u8> = (0..text(s, "bytes").len() / 2).map(|i| u8::from_str_radix(&text(s, "bytes")[2 * i..2 * i + 2], 16).unwrap()).collect();
    let want: Vec<f64> = arr(s, "out").iter().map(|v| v.as_f64().unwrap()).collect();
    assert_eq!(moods::samples_of(&bytes), want);
    for l in arr(&data, "labels_text") {
        let l = l.as_array().unwrap();
        let m = Mood {
            tempo: text(&l[0], "tempo").into(),
            energy_label: text(&l[0], "energy_label").into(),
            mood: text(&l[0], "mood").into(),
            ..Default::default()
        };
        assert_eq!(moods::labels_text(&m), l[1].as_str().unwrap());
    }
    for k in arr(&data, "key_names") {
        let k = k.as_array().unwrap();
        assert_eq!(moods::key_name(k[0].as_str().unwrap(), k[1].as_str().unwrap()), k[2].as_str().unwrap());
    }
    for (p, (_, label)) in arr(&data, "playlist_names").iter().zip(moods::CARDS) {
        let p = p.as_array().unwrap();
        assert_eq!(p[0].as_str().unwrap(), label);
        assert_eq!(moods::playlist_name(label), p[1].as_str().unwrap());
        assert_eq!(moods::card_note(label), text(data.get("notes").unwrap(), label));
    }
    assert_eq!(moods::ESTIMATE_NOTE, text(&data, "estimate_note"));
    for b in arr(&data, "bpm_texts") {
        let b = b.as_array().unwrap();
        let m = (!b[0].is_null()).then(|| Mood { bpm: num(&b[0], "bpm") as i64, ..Default::default() });
        assert_eq!(moods::bpm_text(m.as_ref()), b[1].as_str().unwrap());
    }
}

fn table(data: &Value) -> HashMap<String, Mood> {
    let t = data.get("table").unwrap().as_object().unwrap();
    t.iter()
        .map(|(p, m)| {
            (
                p.clone(),
                Mood {
                    bpm: num(m, "bpm") as i64,
                    tempo: text(m, "tempo").into(),
                    energy_label: text(m, "energy_label").into(),
                    mood: text(m, "mood").into(),
                    ..Default::default()
                },
            )
        })
        .collect()
}

#[test]
fn cards_match_python() {
    let data = load();
    let tracks: Vec<Track> = arr(&data, "tracks").iter().map(|t| Track::from_json(t).unwrap()).collect();
    let moods_of = table(&data);
    for (c, (kind, label)) in arr(&data, "cards").iter().zip(moods::CARDS) {
        assert_eq!((text(c, "kind"), text(c, "label")), (kind, label));
        let want: Vec<String> = arr(c, "songs").iter().map(|v| v.as_str().unwrap().to_string()).collect();
        assert_eq!(moods::card_songs(kind, label, &tracks, &moods_of), want, "{label}");
    }
    let counts = data.get("counts").unwrap();
    for (label, n) in moods::card_counts(&tracks, &moods_of) {
        assert_eq!(n as f64, num(counts, label), "{label}");
    }
}

// ------------------------------------------------------------ files and table ---

fn temp_dir(name: &str) -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!("realhires-moods-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn write_wav(path: &std::path::Path, samples: &[i16], sr: u32, channels: u16) {
    let mut data = Vec::new();
    for v in samples {
        for _ in 0..channels {
            data.extend_from_slice(&v.to_le_bytes());
        }
    }
    let mut out = Vec::new();
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + data.len() as u32).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&channels.to_le_bytes());
    out.extend_from_slice(&sr.to_le_bytes());
    out.extend_from_slice(&(sr * channels as u32 * 2).to_le_bytes());
    out.extend_from_slice(&(channels * 2).to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&(data.len() as u32).to_le_bytes());
    out.extend_from_slice(&data);
    std::fs::write(path, out).unwrap();
}

#[test]
fn songs_decoded_through_ffmpeg_get_their_labels() {
    if realhires::probe::find_tool("ffmpeg").is_none() {
        eprintln!("no ffmpeg: skipped");
        return;
    }
    let d = temp_dir("files");
    for (name, bpm, prog, gain, mood, tempo) in
        [("happy", 128, "major", 100, "Happy", "Up-tempo"), ("sad", 72, "minor", 12, "Sad", "Slow"), ("intense", 150, "minor", 100, "Intense", "Up-tempo")]
    {
        let p = d.join(format!("{name}.wav"));
        write_wav(&p, &song(bpm, prog, 20, 44100, 6000, 9000, gain), 44100, 2);
        let m = moods::analyse_file(&p.to_string_lossy(), 20.0).unwrap();
        assert!((m.bpm - bpm).abs() <= 2, "{name}: {m:?}");
        assert_eq!((m.mood.as_str(), m.tempo.as_str(), m.mode.as_str()), (mood, tempo, prog), "{name}: {m:?}");
        assert!((m.seconds - 20.0).abs() < 0.01, "{m:?}");
    }
    // a song longer than 120 s is analysed from 30 s in, for 90 s
    let p = d.join("long.wav");
    write_wav(&p, &song(96, "minor", 130, 22050, 6000, 9000, 100), 22050, 1);
    let t0 = std::time::Instant::now();
    let m = moods::analyse_file(&p.to_string_lossy(), 130.0).unwrap();
    eprintln!("a 90 s stretch analysed in {:.2} s", t0.elapsed().as_secs_f64());
    assert!((m.seconds - 90.0).abs() < 0.01 && (m.bpm - 96).abs() <= 2, "{m:?}");
    let err = moods::analyse_file(&d.join("missing.flac").to_string_lossy(), 0.0).unwrap_err();
    assert!(err.0.starts_with("The song could not be decoded: "), "{err:?}");
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn the_moods_table_keeps_results_until_a_song_changes() {
    let d = temp_dir("table");
    let db = d.join("library.sqlite");
    let store = MoodStore::open(Some(&db)).unwrap();
    let t = |p: &str, size: i64, mtime: i64| Track { path: p.into(), size, mtime_ns: mtime, duration: 3.0, ..Default::default() };
    let tracks = vec![t("/a.flac", 1, 10), t("/b.flac", 2, 20), t("/c.flac", 3, 30)];
    assert_eq!(store.todo(&tracks).len(), 3);
    let calls = std::cell::RefCell::new(Vec::new());
    let analyse = |p: &str, _d: f64| -> Result<Mood, moods::MoodError> {
        calls.borrow_mut().push(p.to_string());
        if p == "/c.flac" {
            return Err(moods::MoodError("The song could not be decoded: no".into()));
        }
        Ok(Mood { bpm: 128, key: "A".into(), mode: "minor".into(), tempo: "Up-tempo".into(), energy_label: "Energetic".into(), mood: "Intense".into(), energy: -9.5, ..Default::default() })
    };
    let mut seen = vec![];
    let (done, failed) = moods::run_analysis(&tracks, &store, None, &mut |i, n, p, m| seen.push((i, n, p.to_string(), m.is_some())), std::time::Duration::ZERO, &analyse);
    assert_eq!((done, failed), (2, 1));
    assert_eq!(seen, vec![(1, 3, "/a.flac".into(), true), (2, 3, "/b.flac".into(), true), (3, 3, "/c.flac".into(), false)]);
    let all = store.all();
    assert_eq!(all.len(), 2);
    let a = &all["/a.flac"];
    assert_eq!((a.bpm, a.key.as_str(), a.mode.as_str(), a.mood.as_str(), a.tempo.as_str(), a.energy_label.as_str()), (128, "A", "minor", "Intense", "Up-tempo", "Energetic"));
    assert_eq!(a.energy, -9.5);
    // the failed song is tried again; a changed song too, an unchanged one is not
    let changed = vec![t("/a.flac", 1, 10), t("/b.flac", 2, 21), t("/c.flac", 3, 30)];
    let again: Vec<String> = store.todo(&changed).into_iter().map(|t| t.path).collect();
    assert_eq!(again, vec!["/b.flac".to_string(), "/c.flac".to_string()]);
    // a stop before the first song analyses nothing
    let stop = std::sync::atomic::AtomicBool::new(true);
    calls.borrow_mut().clear();
    assert_eq!(moods::run_analysis(&changed, &store, Some(&stop), &mut |_, _, _, _| {}, std::time::Duration::ZERO, &analyse), (0, 0));
    assert!(calls.borrow().is_empty());
    // rows of an older analyser are analysed again (and shown until then)
    drop(store);
    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.execute("UPDATE moods SET version = 0 WHERE path = '/a.flac'", []).unwrap();
    drop(conn);
    let store = MoodStore::open(Some(&db)).unwrap();
    assert!(store.todo(&changed).iter().any(|t| t.path == "/a.flac"));
    assert!(store.get("/a.flac").is_some_and(|m| m.mood == "Intense"));
    assert_eq!(store.forget(), 2);
    assert!(store.all().is_empty());
    let _ = std::fs::remove_dir_all(&d);
}
