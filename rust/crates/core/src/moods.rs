//! Moods, tempo and key (2.4), as `realhires/moods.py` does them: each song of the
//! library is analysed once, from its audio, and gets a tempo (BPM), an energy
//! (loudness), a brightness (spectral centroid) and a key with its mode, and from these
//! the labels Up-tempo / Mid-tempo / Slow, Energetic / Calm and a mood among Happy,
//! Sad, Chill and Intense.  The labels are estimates.  See the Python module's
//! docstring for the method and the thresholds; every step here is the same, so both
//! versions give the same labels and BPM (vectors `python_moods.json`).  Results are
//! kept in the table `moods` of `library.sqlite` (`MoodStore`).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use realfft::RealFftPlanner;

use crate::dsp::{hanning, median, moving_average_same};
use crate::json::{self, Obj, Style, Value};
use crate::musiclib::Track;

pub const ANALYSER_VERSION: i64 = 1;

// ---- the stretch that is analysed
pub const RATE: u32 = 22050;
pub const START_S: i64 = 30;
pub const LENGTH_S: i64 = 90;
pub const MIN_SECONDS: f64 = 1.0;
pub const SILENCE_DB: f64 = -60.0;

// ---- tempo
pub const ONSET_FFT: usize = 1024;
pub const ONSET_HOP: usize = 256;
pub const FPS: f64 = RATE as f64 / ONSET_HOP as f64;
pub const LOG_GAIN: f64 = 1000.0;
pub const TREND_FRAMES: usize = 43;
pub const MIN_BPM: f64 = 50.0;
pub const MAX_BPM: f64 = 210.0;
pub const LAG_MIN: usize = 25;
pub const LAG_MAX: usize = 103;
pub const PRIOR_BPM: f64 = 120.0;
pub const PRIOR_OCTAVES: f64 = 1.0;
pub const MIN_PULSE: f64 = 0.05;
pub const FASTER_RATIO: f64 = 0.8;
pub const MULTIPLES: usize = 4;
pub const MULTIPLE_KEEP: f64 = 0.5;

// ---- key
pub const CHROMA_FFT: usize = 8192;
pub const CHROMA_HOP: usize = 4096;
pub const CHROMA_MIN_HZ: f64 = 65.0;
pub const CHROMA_MAX_HZ: f64 = 2100.0;
pub const MAJOR_PROFILE: [f64; 12] = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88];
pub const MINOR_PROFILE: [f64; 12] = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17];
pub const PITCH_NAMES: [&str; 12] = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"];

// ---- labels
pub const UPTEMPO_BPM: i64 = 120;
pub const SLOW_BPM: i64 = 90;
pub const ENERGETIC_DB: f64 = -16.0;
pub const KEY_MARGIN: f64 = 0.05;
pub const BRIGHT_HZ: f64 = 1500.0;

pub const TEMPOS: [&str; 3] = ["Up-tempo", "Mid-tempo", "Slow"];
pub const ENERGIES: [&str; 2] = ["Energetic", "Calm"];
pub const MOODS: [&str; 4] = ["Happy", "Sad", "Chill", "Intense"];
pub const MODES: [&str; 2] = ["major", "minor"];

/// The song could not be decoded (the message says why).
#[derive(Clone, Debug, PartialEq)]
pub struct MoodError(pub String);

impl std::fmt::Display for MoodError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// What the analysis found in a song (labels empty for silence or very short audio).
#[derive(Clone, Debug, PartialEq)]
pub struct Mood {
    pub bpm: i64,
    /// dBFS (-200 for silence)
    pub energy: f64,
    /// Hz
    pub brightness: f64,
    /// one of `PITCH_NAMES`, "" when unknown
    pub key: String,
    /// "major" / "minor"
    pub mode: String,
    pub key_strength: f64,
    pub tempo: String,
    pub energy_label: String,
    pub mood: String,
    /// how much audio was analysed
    pub seconds: f64,
}

impl Default for Mood {
    fn default() -> Self {
        Mood {
            bpm: 0,
            energy: -200.0,
            brightness: 0.0,
            key: String::new(),
            mode: String::new(),
            key_strength: 0.0,
            tempo: String::new(),
            energy_label: String::new(),
            mood: String::new(),
            seconds: 0.0,
        }
    }
}

impl Mood {
    /// "Happy · Up-tempo · 128 BPM" (English; the apps translate the parts).
    pub fn summary(&self) -> String {
        let bpm = if self.bpm > 0 { format!("{} BPM", self.bpm) } else { String::new() };
        [self.mood.as_str(), self.tempo.as_str(), bpm.as_str()].iter().filter(|x| !x.is_empty()).cloned().collect::<Vec<_>>().join(" · ")
    }
}

// ------------------------------------------------------------------ decoding ---

/// (start, length) in whole seconds of the part of a song that is analysed.
pub fn stretch(duration: f64) -> (i64, i64) {
    let d = if duration > 0.0 { duration as i64 } else { 0 };
    ((d - LENGTH_S).min(START_S).max(0), LENGTH_S)
}

/// The FFmpeg arguments (after the program) for the stretch: two channels of 32-bit
/// floats at RATE Hz, which `decode()` averages.
pub fn decode_args(path: &str, start: i64, length: i64) -> Vec<String> {
    let mut a: Vec<String> = ["-v", "error", "-nostdin", "-threads", "1"].iter().map(|s| s.to_string()).collect();
    if start > 0 {
        a.extend(["-ss".to_string(), start.to_string()]);
    }
    a.extend(["-i".to_string(), path.to_string()]);
    for s in ["-map", "0:a:0", "-vn", "-t"] {
        a.push(s.to_string());
    }
    a.push(length.to_string());
    for s in ["-ac", "2", "-ar"] {
        a.push(s.to_string());
    }
    a.push(RATE.to_string());
    for s in ["-f", "f32le", "-acodec", "pcm_f32le", "-"] {
        a.push(s.to_string());
    }
    a
}

#[cfg(not(windows))]
fn find_nice() -> Option<PathBuf> {
    let path = std::env::var_os("PATH").unwrap_or_else(|| "/usr/bin:/bin".into());
    std::env::split_paths(&path).map(|d| d.join("nice")).find(|p| p.is_file())
}

/// The decoding command, below normal priority: through `nice` on macOS and Linux,
/// in the below-normal priority class on Windows.
fn low_priority_command(args: &[String]) -> std::process::Command {
    let ffmpeg = crate::probe::tool("ffmpeg");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let mut cmd = std::process::Command::new(ffmpeg);
        cmd.creation_flags(0x0800_0000 | 0x0000_4000); // CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
        cmd.args(args);
        cmd
    }
    #[cfg(not(windows))]
    {
        match find_nice() {
            Some(nice) => {
                let mut cmd = crate::probe::command(nice);
                cmd.args(["-n", "10"]).arg(ffmpeg).args(args);
                cmd
            }
            None => {
                let mut cmd = crate::probe::command(ffmpeg);
                cmd.args(args);
                cmd
            }
        }
    }
}

/// The mono samples of FFmpeg's two-channel f32le output: (left + right) / 2.
pub fn samples_of(data: &[u8]) -> Vec<f64> {
    data.chunks_exact(8)
        .map(|c| {
            let l = f32::from_le_bytes([c[0], c[1], c[2], c[3]]) as f64;
            let r = f32::from_le_bytes([c[4], c[5], c[6], c[7]]) as f64;
            (l + r) * 0.5
        })
        .collect()
}

/// The analysed stretch of a song as mono samples at RATE Hz.
pub fn decode(path: &str, duration: f64) -> Result<Vec<f64>, MoodError> {
    let (start, length) = stretch(duration);
    let out = low_priority_command(&decode_args(path, start, length))
        .stdin(std::process::Stdio::null())
        .output()
        .map_err(|e| MoodError(format!("FFmpeg could not be started: {e}")))?;
    if !out.status.success() {
        let msg = String::from_utf8_lossy(&out.stderr).trim().to_string();
        let code = out.status.code().map(|c| format!("exit code {c}")).unwrap_or_else(|| "killed".into());
        return Err(MoodError(format!("The song could not be decoded: {}", if msg.is_empty() { code } else { msg })));
    }
    Ok(samples_of(&out.stdout))
}

// ------------------------------------------------------------------ analysis ---

/// Calls `each` with the magnitude spectrum of every frame of x (frame i starts at
/// i * hop; no padding).
fn spectra(x: &[f64], nfft: usize, hop: usize, window: &[f64], mut each: impl FnMut(&[f64])) {
    if x.len() < nfft {
        return;
    }
    let count = 1 + (x.len() - nfft) / hop;
    let fft = RealFftPlanner::<f64>::new().plan_fft_forward(nfft);
    let mut input = fft.make_input_vec();
    let mut output = fft.make_output_vec();
    let mut mag = vec![0.0; nfft / 2 + 1];
    for i in 0..count {
        let frame = &x[i * hop..i * hop + nfft];
        for ((d, s), w) in input.iter_mut().zip(frame).zip(window) {
            *d = s * w;
        }
        let _ = fft.process(&mut input, &mut output);
        for (m, z) in mag.iter_mut().zip(&output) {
            *m = z.norm();
        }
        each(&mag);
    }
}

/// (onset envelope, average power spectrum) of the onset frames (Python `onset_envelope`).
pub fn onset_envelope(x: &[f64]) -> (Vec<f64>, Vec<f64>) {
    let w = hanning(ONSET_FFT);
    let scale = 2.0 / w.iter().sum::<f64>();
    let bins = ONSET_FFT / 2 + 1;
    let mut power = vec![0.0; bins];
    let mut prev: Option<Vec<f64>> = None;
    let mut env = Vec::new();
    let mut n = 0usize;
    spectra(x, ONSET_FFT, ONSET_HOP, &w, |mag| {
        let mut comp = vec![0.0; bins];
        for ((c, p), m) in comp.iter_mut().zip(power.iter_mut()).zip(mag) {
            let m = m * scale;
            *p += m * m;
            *c = (LOG_GAIN * m).ln_1p();
        }
        n += 1;
        if let Some(pr) = &prev {
            env.push(comp.iter().zip(pr).map(|(c, p)| (c - p).max(0.0)).sum());
        }
        prev = Some(comp);
    });
    if env.len() >= TREND_FRAMES {
        let trend = moving_average_same(&env, TREND_FRAMES);
        for (e, t) in env.iter_mut().zip(&trend) {
            *e = (*e - t).max(0.0);
        }
    }
    let n = n.max(1) as f64;
    (env, power.into_iter().map(|p| p / n).collect())
}

fn dot(a: &[f64], b: &[f64]) -> f64 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

/// r[l] = Σ env[t]·env[t+l] / Σ env² for l = 0..max_lag (0 beyond the envelope).
pub fn autocorrelation(env: &[f64], max_lag: usize) -> Vec<f64> {
    let mut r = vec![0.0; max_lag + 1];
    let e0 = dot(env, env);
    if e0 <= 0.0 || env.is_empty() {
        return r;
    }
    for lag in 0..=max_lag.min(env.len() - 1) {
        r[lag] = dot(&env[..env.len() - lag], &env[lag..]) / e0;
    }
    r
}

/// The tempo prior's weight for a lag (in onset frames).
pub fn prior(lag: f64) -> f64 {
    let bpm = 60.0 * FPS / lag;
    (-0.5 * ((bpm / PRIOR_BPM).log2() / PRIOR_OCTAVES).powi(2)).exp()
}

fn is_peak(r: &[f64], lag: usize) -> bool {
    lag > 0 && lag + 1 < r.len() && r[lag] > 0.0 && r[lag] >= r[lag - 1] && r[lag] >= r[lag + 1]
}

fn parabola(r: &[f64], lag: usize) -> f64 {
    let (a, b, c) = (r[lag - 1], r[lag], r[lag + 1]);
    let den = a - 2.0 * b + c;
    if den >= 0.0 {
        return lag as f64;
    }
    lag as f64 + (0.5 * (a - c) / den).clamp(-0.5, 0.5)
}

fn peak_near(r: &[f64], centre: f64) -> usize {
    let c = (centre + 0.5).floor() as i64;
    let mut best = 0usize;
    for lag in [c - 1, c, c + 1] {
        if lag <= 0 {
            continue;
        }
        let lag = lag as usize;
        if is_peak(r, lag) && (best == 0 || r[lag] > r[best]) {
            best = lag;
        }
    }
    best
}

/// The tempo in BPM, not rounded (0 when the envelope shows no beat).
pub fn tempo(env: &[f64]) -> f64 {
    let (lo, hi) = (LAG_MIN, LAG_MAX);
    let r = autocorrelation(env, MULTIPLES * hi + 2);
    let (mut best, mut score) = (0usize, 0.0f64);
    for lag in lo..=hi {
        if is_peak(&r, lag) {
            let s = r[lag] * prior(lag as f64);
            if s > score {
                best = lag;
                score = s;
            }
        }
    }
    if best == 0 || r[best] - median(&r[lo..=hi]) < MIN_PULSE {
        return 0.0;
    }
    loop {
        let half = peak_near(&r, best as f64 / 2.0);
        if half == 0 || half < lo || r[half] < FASTER_RATIO * r[best] {
            break;
        }
        best = half;
    }
    let p = parabola(&r, best);
    let (mut lags, mut ks) = (p, 1.0);
    for k in 2..=MULTIPLES {
        let q = peak_near(&r, k as f64 * p);
        if q != 0 && r[q] >= MULTIPLE_KEEP * r[best] {
            lags += parabola(&r, q);
            ks += k as f64;
        }
    }
    60.0 * FPS / (lags / ks)
}

/// The pitch class (0 = C) of every bin of a CHROMA_FFT spectrum, -1 outside the range.
pub fn chroma_bins() -> Vec<i64> {
    (0..=CHROMA_FFT / 2)
        .map(|k| {
            let f = k as f64 * (RATE as f64 / CHROMA_FFT as f64);
            if (CHROMA_MIN_HZ..=CHROMA_MAX_HZ).contains(&f) {
                let midi = 69.0 + 12.0 * (f / 440.0).log2();
                ((midi + 0.5).floor() as i64).rem_euclid(12)
            } else {
                -1
            }
        })
        .collect()
}

/// The 12 pitch-class sums of the average magnitude spectrum of CHROMA_FFT frames.
pub fn chroma(x: &[f64]) -> [f64; 12] {
    let padded;
    let x = if x.len() < CHROMA_FFT {
        let mut v = x.to_vec();
        v.resize(CHROMA_FFT, 0.0);
        padded = v;
        &padded[..]
    } else {
        x
    };
    let mut total = vec![0.0; CHROMA_FFT / 2 + 1];
    let mut n = 0usize;
    spectra(x, CHROMA_FFT, CHROMA_HOP, &hanning(CHROMA_FFT), |mag| {
        for (t, m) in total.iter_mut().zip(mag) {
            *t += m;
        }
        n += 1;
    });
    let n = n.max(1) as f64;
    let bins = chroma_bins();
    let mut c = [0.0; 12];
    for (pc, slot) in c.iter_mut().enumerate() {
        *slot = total.iter().zip(&bins).filter(|(_, b)| **b == pc as i64).map(|(t, _)| t / n).sum();
    }
    c
}

/// Pearson's correlation of two vectors (0 when one of them is flat).
pub fn correlation(a: &[f64], b: &[f64]) -> f64 {
    let ma = a.iter().sum::<f64>() / a.len() as f64;
    let mb = b.iter().sum::<f64>() / b.len() as f64;
    let a: Vec<f64> = a.iter().map(|x| x - ma).collect();
    let b: Vec<f64> = b.iter().map(|x| x - mb).collect();
    let den = (dot(&a, &a) * dot(&b, &b)).sqrt();
    if den > 0.0 {
        dot(&a, &b) / den
    } else {
        0.0
    }
}

/// (pitch class of the tonic, "major"/"minor", strength) of a chroma vector.
pub fn key_of(c: &[f64]) -> (usize, &'static str, f64) {
    let mut best = [(-2.0f64, 0usize); 2];
    for (m, profile) in [MAJOR_PROFILE, MINOR_PROFILE].iter().enumerate() {
        for k in 0..12 {
            let rotated: Vec<f64> = (0..12).map(|pc| profile[(pc + 12 - k) % 12]).collect();
            let r = correlation(c, &rotated);
            if r > best[m].0 {
                best[m] = (r, k);
            }
        }
    }
    let ((rmaj, kmaj), (rmin, kmin)) = (best[0], best[1]);
    if rmaj >= rmin {
        (kmaj, "major", rmaj - rmin)
    } else {
        (kmin, "minor", rmin - rmaj)
    }
}

pub fn tempo_label(bpm: i64) -> &'static str {
    if bpm <= 0 {
        ""
    } else if bpm >= UPTEMPO_BPM {
        "Up-tempo"
    } else if bpm < SLOW_BPM {
        "Slow"
    } else {
        "Mid-tempo"
    }
}

pub fn energy_label(energy_db: f64) -> &'static str {
    if energy_db >= ENERGETIC_DB {
        "Energetic"
    } else {
        "Calm"
    }
}

/// Happy, Sad, Chill or Intense (Python `mood_label`).
pub fn mood_label(mode: &str, key_strength: f64, brightness: f64, tempo: &str, energy: &str) -> &'static str {
    let positive = if key_strength >= KEY_MARGIN && !mode.is_empty() { mode == "major" } else { brightness >= BRIGHT_HZ };
    let arousal = i32::from(energy == "Energetic") + i32::from(tempo == "Up-tempo") - i32::from(tempo == "Slow");
    match (positive, arousal >= 1) {
        (true, true) => "Happy",
        (true, false) => "Chill",
        (false, true) => "Intense",
        (false, false) => "Sad",
    }
}

/// The mood of mono samples at RATE Hz (full scale ±1).
pub fn analyse_samples(x: &[f64]) -> Mood {
    let seconds = x.len() as f64 / RATE as f64;
    let ms = if x.is_empty() { 0.0 } else { dot(x, x) / x.len() as f64 };
    let energy = 10.0 * ms.max(1e-20).log10();
    let mut out = Mood { energy, seconds, ..Default::default() };
    if seconds < MIN_SECONDS || x.len() < ONSET_FFT || energy < SILENCE_DB {
        return out;
    }
    let (env, power) = onset_envelope(x);
    let total: f64 = power.iter().sum();
    let weighted: f64 = power.iter().enumerate().map(|(k, p)| k as f64 * (RATE as f64 / ONSET_FFT as f64) * p).sum();
    out.brightness = if total > 0.0 { weighted / total } else { 0.0 };
    out.bpm = (tempo(&env) + 0.5).floor() as i64;
    let (pc, mode, strength) = key_of(&chroma(x));
    out.key = PITCH_NAMES[pc].to_string();
    out.mode = mode.to_string();
    out.key_strength = strength;
    out.tempo = tempo_label(out.bpm).to_string();
    out.energy_label = energy_label(energy).to_string();
    out.mood = mood_label(mode, strength, out.brightness, &out.tempo, &out.energy_label).to_string();
    out
}

/// Decode and analyse one song.
pub fn analyse_file(path: &str, duration: f64) -> Result<Mood, MoodError> {
    Ok(analyse_samples(&decode(path, duration)?))
}

/// "A minor", "Eb major" ("" when unknown; the apps translate it).
pub fn key_name(key: &str, mode: &str) -> String {
    if !key.is_empty() && MODES.contains(&mode) {
        format!("{key} {mode}")
    } else {
        String::new()
    }
}

// ----------------------------------------------------------------- the table ---

const TABLE: &str = "CREATE TABLE IF NOT EXISTS moods (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, bpm INTEGER, \
                     energy REAL, brightness REAL, key TEXT, mode TEXT, labels TEXT, version INTEGER)";

/// The labels as the table keeps them (Python `labels_text`).
pub fn labels_text(m: &Mood) -> String {
    let v = Obj::new().put("tempo", m.tempo.as_str()).put("energy", m.energy_label.as_str()).put("mood", m.mood.as_str()).build();
    json::dumps(&v, Style { indent: None, compact: false, ensure_ascii: false, sort_keys: true })
}

fn pick(labels: &Option<Value>, name: &str, allowed: &[&str]) -> String {
    labels
        .as_ref()
        .and_then(|v| v.get(name))
        .and_then(Value::as_str)
        .filter(|s| allowed.contains(s))
        .unwrap_or("")
        .to_string()
}

fn mood_of_row(bpm: Option<i64>, energy: Option<f64>, brightness: Option<f64>, key: Option<String>, mode: Option<String>, labels: Option<String>) -> Mood {
    let lab = labels.and_then(|t| json::parse(&t).ok()).filter(|v| v.as_object().is_some());
    let key = key.unwrap_or_default();
    let mode = mode.unwrap_or_default();
    Mood {
        bpm: bpm.unwrap_or(0),
        energy: energy.unwrap_or(0.0),
        brightness: brightness.unwrap_or(0.0),
        key: if PITCH_NAMES.contains(&key.as_str()) { key } else { String::new() },
        mode: if MODES.contains(&mode.as_str()) { mode } else { String::new() },
        key_strength: 0.0,
        tempo: pick(&lab, "tempo", &TEMPOS),
        energy_label: pick(&lab, "energy", &ENERGIES),
        mood: pick(&lab, "mood", &MOODS),
        seconds: 0.0,
    }
}

/// The table `moods` of library.sqlite: one row per analysed song.  One per thread.
pub struct MoodStore {
    db: rusqlite::Connection,
}

impl MoodStore {
    pub fn open(path: Option<&Path>) -> rusqlite::Result<MoodStore> {
        let path = path.map(Path::to_path_buf).unwrap_or_else(crate::musiclib::default_path);
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let db = rusqlite::Connection::open(&path)?;
        db.busy_timeout(Duration::from_secs(30))?;
        db.execute(TABLE, [])?;
        Ok(MoodStore { db })
    }

    pub fn put(&self, path: &str, size: i64, mtime_ns: i64, m: &Mood) -> rusqlite::Result<()> {
        self.db.execute(
            "INSERT OR REPLACE INTO moods VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rusqlite::params![path, size, mtime_ns, m.bpm, m.energy, m.brightness, m.key, m.mode, labels_text(m), ANALYSER_VERSION],
        )?;
        Ok(())
    }

    pub fn get(&self, path: &str) -> Option<Mood> {
        self.db
            .query_row("SELECT bpm, energy, brightness, key, mode, labels FROM moods WHERE path = ?", [path], |r| {
                Ok(mood_of_row(r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?))
            })
            .ok()
    }

    /// Every analysed song (also those of an older analyser, until analysed again).
    pub fn all(&self) -> HashMap<String, Mood> {
        let mut out = HashMap::new();
        if let Ok(mut st) = self.db.prepare("SELECT path, bpm, energy, brightness, key, mode, labels FROM moods") {
            if let Ok(rows) = st.query_map([], |r| {
                Ok((r.get::<_, String>(0)?, mood_of_row(r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?)))
            }) {
                for (p, m) in rows.flatten() {
                    out.insert(p, m);
                }
            }
        }
        out
    }

    /// The tracks not analysed yet, changed since, or analysed by another
    /// ANALYSER_VERSION, in the order given.
    pub fn todo(&self, tracks: &[Track]) -> Vec<Track> {
        let mut known: HashMap<String, (i64, i64, i64)> = HashMap::new();
        if let Ok(mut st) = self.db.prepare("SELECT path, size, mtime_ns, version FROM moods") {
            if let Ok(rows) = st.query_map([], |r| {
                Ok((r.get::<_, String>(0)?, (r.get::<_, Option<i64>>(1)?.unwrap_or(-1), r.get::<_, Option<i64>>(2)?.unwrap_or(-1), r.get::<_, Option<i64>>(3)?.unwrap_or(0))))
            }) {
                for (p, k) in rows.flatten() {
                    known.insert(p, k);
                }
            }
        }
        tracks.iter().filter(|t| known.get(&t.path) != Some(&(t.size, t.mtime_ns, ANALYSER_VERSION))).cloned().collect()
    }

    pub fn forget(&self) -> usize {
        self.db.execute("DELETE FROM moods", []).unwrap_or(0)
    }
}

// --------------------------------------------------------- the background job ---

/// Between two songs, so the analysis stays in the background.
pub const PAUSE: Duration = Duration::from_millis(50);

/// Analyse the tracks that need it, one at a time, saving each result at once;
/// `progress(done, total, path, mood)` after each song (mood None when it could not
/// be decoded: it is tried again next time).  Returns (analysed, failed).
pub fn run_analysis(
    tracks: &[Track],
    store: &MoodStore,
    stop: Option<&AtomicBool>,
    progress: &mut dyn FnMut(usize, usize, &str, Option<&Mood>),
    pause: Duration,
    analyse: &dyn Fn(&str, f64) -> Result<Mood, MoodError>,
) -> (usize, usize) {
    let todo = store.todo(tracks);
    let (mut done, mut failed) = (0, 0);
    let stopped = || stop.is_some_and(|s| s.load(Ordering::Relaxed));
    for (i, t) in todo.iter().enumerate() {
        if stopped() {
            break;
        }
        let m = analyse(&t.path, t.duration.max(0.0)).ok();
        match &m {
            Some(m) if store.put(&t.path, t.size, t.mtime_ns, m).is_ok() => done += 1,
            _ => failed += 1,
        }
        progress(i + 1, todo.len(), &t.path, m.as_ref());
        if !pause.is_zero() && !stopped() {
            std::thread::sleep(pause);
        }
    }
    (done, failed)
}

// ------------------------------------------------------------------ the pages ---

/// The cards of the Moods page, in their order: (kind, label).
pub const CARDS: [(&str, &str); 7] = [
    ("mood", "Happy"),
    ("mood", "Sad"),
    ("mood", "Chill"),
    ("mood", "Intense"),
    ("tempo", "Up-tempo"),
    ("tempo", "Mid-tempo"),
    ("tempo", "Slow"),
];

/// What each card says under its name (English; the apps translate it).
pub fn card_note(label: &str) -> &'static str {
    match label {
        "Happy" => "Major key, lively",
        "Sad" => "Minor key, calm",
        "Chill" => "Major key, calm",
        "Intense" => "Minor key, lively",
        "Up-tempo" => "120 BPM and faster",
        "Mid-tempo" => "90 to 119 BPM",
        "Slow" => "Slower than 90 BPM",
        _ => "",
    }
}

pub const ESTIMATE_NOTE: &str =
    "Moods, tempo and key are estimated from the audio on this computer, so they are a guess, not a fact about the song.";

pub fn label_of<'a>(kind: &str, m: Option<&'a Mood>) -> &'a str {
    match (kind, m) {
        ("mood", Some(m)) => &m.mood,
        ("tempo", Some(m)) => &m.tempo,
        ("energy", Some(m)) => &m.energy_label,
        _ => "",
    }
}

/// The paths of the songs of a card, sorted as All Songs is (title, artist, path).
pub fn card_songs(kind: &str, label: &str, tracks: &[Track], moods: &HashMap<String, Mood>) -> Vec<String> {
    if label.is_empty() {
        return vec![];
    }
    let mut hits: Vec<Track> = tracks.iter().filter(|t| label_of(kind, moods.get(&t.path)) == label).cloned().collect();
    crate::musiclib::sort_songs(&mut hits);
    hits.into_iter().map(|t| t.path).collect()
}

/// The number of songs of every card (0 included), in CARDS order.
pub fn card_counts(tracks: &[Track], moods: &HashMap<String, Mood>) -> Vec<(&'static str, usize)> {
    let mut out: Vec<(&'static str, usize)> = CARDS.iter().map(|(_, l)| (*l, 0)).collect();
    for t in tracks {
        if let Some(m) = moods.get(&t.path) {
            for kind in ["mood", "tempo"] {
                let lab = label_of(kind, Some(m));
                if let Some(slot) = out.iter_mut().find(|(l, _)| *l == lab) {
                    slot.1 += 1;
                }
            }
        }
    }
    out
}

pub fn analysed_count(tracks: &[Track], moods: &HashMap<String, Mood>) -> usize {
    tracks.iter().filter(|t| moods.contains_key(&t.path)).count()
}

/// The name Create Playlist gives a card's songs ("Sad songs"; translated by the apps).
pub fn playlist_name(label: &str) -> String {
    format!("{label} songs")
}

/// The BPM column: "128", or "" when not analysed or without a beat.
pub fn bpm_text(m: Option<&Mood>) -> String {
    match m {
        Some(m) if m.bpm > 0 => m.bpm.to_string(),
        _ => String::new(),
    }
}

pub fn sort_bpm(m: Option<&Mood>) -> i64 {
    m.map_or(-1, |m| m.bpm)
}
