//! Equalizer maths shared with the Python version (`realhires/eq.py`, 2.0):
//! a ten-band graphic EQ and a parametric EQ, both made of Audio EQ Cookbook
//! biquads, their response curve, the automatic headroom, and the processor
//! the Rust player filters its samples with.  The numbers equal Python's;
//! `tests/python_eq.json` holds Python's answers.

use crate::json::{Obj as ObjectBuilder, Value};

pub const GRAPHIC_FREQS: [f64; 10] = [32.0, 64.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0];
pub const GRAPHIC_LABELS: [&str; 10] = ["32", "64", "125", "250", "500", "1K", "2K", "4K", "8K", "16K"];
pub const GRAPHIC_Q: f64 = std::f64::consts::SQRT_2;
pub const GAIN_LIMIT_DB: f64 = 12.0;
pub const MAX_PARAMETRIC_BANDS: usize = 10;
pub const MIN_FREQ: f64 = 20.0;
pub const MAX_FREQ: f64 = 20000.0;
pub const MIN_Q: f64 = 0.1;
pub const MAX_Q: f64 = 10.0;
pub const BAND_TYPES: [&str; 6] = ["peak", "low_shelf", "high_shelf", "low_pass", "high_pass", "notch"];
pub const CUSTOM: &str = "Custom";
pub const RESPONSE_POINTS: usize = 241;
pub const RESPONSE_RATE: f64 = 48000.0;

pub fn band_type_label(kind: &str) -> &'static str {
    match kind {
        "low_shelf" => "Low shelf",
        "high_shelf" => "High shelf",
        "low_pass" => "Low pass",
        "high_pass" => "High pass",
        "notch" => "Notch",
        _ => "Peak",
    }
}

pub fn has_gain(kind: &str) -> bool {
    matches!(kind, "peak" | "low_shelf" | "high_shelf")
}

pub const PRESETS: [(&str, [f64; 10]); 17] = [
    ("Flat", [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    ("Bass Boost", [6.0, 5.0, 4.0, 2.5, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    ("Bass Cut", [-6.0, -5.0, -4.0, -2.5, -1.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    ("Treble Boost", [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 2.5, 4.0, 5.0, 6.0]),
    ("Treble Cut", [0.0, 0.0, 0.0, 0.0, 0.0, -1.0, -2.5, -4.0, -5.0, -6.0]),
    ("Vocal", [-2.0, -2.0, -1.0, 1.0, 3.0, 3.5, 3.0, 1.5, 0.0, -1.0]),
    ("Loudness", [5.0, 4.0, 2.0, 0.0, -1.0, -1.0, 0.0, 1.0, 3.0, 4.0]),
    ("Late Night", [3.0, 2.5, 1.5, 0.5, 0.0, 0.0, 0.5, 1.5, 2.0, 2.0]),
    ("Small Speakers", [5.0, 4.5, 3.5, 2.0, 1.0, 0.0, -0.5, -1.0, -1.5, -2.0]),
    ("Acoustic", [3.0, 3.0, 2.0, 0.5, 1.0, 1.0, 2.0, 2.5, 2.0, 1.0]),
    ("Classical", [3.0, 2.5, 2.0, 1.0, -1.0, -1.0, 0.0, 1.5, 2.5, 3.0]),
    ("Electronic", [4.0, 3.5, 1.0, 0.0, -1.5, 1.0, 0.5, 1.0, 3.5, 4.5]),
    ("Hip-Hop", [4.5, 4.0, 1.5, 2.5, -1.0, -1.0, 1.0, -0.5, 1.5, 2.5]),
    ("Jazz", [3.0, 2.0, 1.0, 2.0, -1.5, -1.5, 0.0, 1.0, 2.0, 3.0]),
    ("Pop", [-1.0, -0.5, 0.0, 1.5, 3.5, 3.5, 1.5, 0.0, -0.5, -1.0]),
    ("Rock", [4.5, 3.5, 2.5, 1.0, -0.5, -1.0, 0.5, 2.0, 3.0, 3.5]),
    ("Spoken Word", [-3.0, -2.0, 0.0, 1.0, 3.0, 4.0, 4.0, 3.0, 1.5, 0.0]),
];

/// b0, b1, b2, a1, a2 (normalised by a0)
pub type Coeffs = [f64; 5];
pub const IDENTITY: Coeffs = [1.0, 0.0, 0.0, 0.0, 0.0];

#[derive(Clone, Debug, PartialEq)]
pub struct Band {
    pub kind: String,
    pub freq: f64,
    pub gain: f64,
    pub q: f64,
    pub on: bool,
}

impl Default for Band {
    fn default() -> Self {
        Band { kind: "peak".into(), freq: 1000.0, gain: 0.0, q: 1.0, on: true }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct EqState {
    pub enabled: bool,
    pub prevent_clipping: bool,
    pub graphic_enabled: bool,
    pub preset: String,
    pub graphic_preamp: f64,
    pub gains: [f64; 10],
    pub parametric_enabled: bool,
    pub parametric_preamp: f64,
    pub bands: Vec<Band>,
}

impl Default for EqState {
    fn default() -> Self {
        EqState {
            enabled: false,
            prevent_clipping: true,
            graphic_enabled: true,
            preset: "Flat".into(),
            graphic_preamp: 0.0,
            gains: [0.0; 10],
            parametric_enabled: false,
            parametric_preamp: 0.0,
            bands: vec![Band::default()],
        }
    }
}

/// Python `_num`: float(value), the default when that fails or is NaN, clamped.
fn num(v: Option<&Value>, default: f64, lo: f64, hi: f64) -> f64 {
    let x = match v {
        Some(Value::Int(i)) => *i as f64,
        Some(Value::Float(f)) => *f,
        Some(Value::Bool(b)) => {
            if *b {
                1.0
            } else {
                0.0
            }
        }
        Some(Value::Str(s)) => match crate::pyunicode::strip(s).replace('_', "").parse::<f64>() {
            Ok(f) => f,
            Err(_) => return default,
        },
        _ => return default,
    };
    if x.is_nan() {
        return default;
    }
    x.max(lo).min(hi)
}

fn truthy(v: Option<&Value>, default: bool) -> bool {
    v.map(|v| v.truthy()).unwrap_or(default)
}

pub fn preset_name(gains: &[f64; 10]) -> String {
    for (name, values) in PRESETS {
        if gains.iter().zip(values.iter()).all(|(a, b)| (a - b).abs() < 1e-9) {
            return name.to_string();
        }
    }
    CUSTOM.to_string()
}

impl EqState {
    /// Python `normalize`: a valid state from whatever was stored.
    pub fn from_json(v: &Value) -> EqState {
        let mut out = EqState::default();
        if !matches!(v, Value::Object(_)) {
            return out;
        }
        out.enabled = truthy(v.get("enabled"), out.enabled);
        out.prevent_clipping = truthy(v.get("prevent_clipping"), true);
        if let Some(g @ Value::Object(_)) = v.get("graphic") {
            out.graphic_enabled = truthy(g.get("enabled"), true);
            out.graphic_preamp = num(g.get("preamp"), 0.0, -GAIN_LIMIT_DB, GAIN_LIMIT_DB);
            if let Some(Value::Array(gains)) = g.get("gains") {
                if gains.len() == 10 {
                    for (i, x) in gains.iter().enumerate() {
                        out.gains[i] = num(Some(x), 0.0, -GAIN_LIMIT_DB, GAIN_LIMIT_DB);
                    }
                }
            }
            out.preset = match g.get("preset").and_then(Value::as_str) {
                Some(p) if p == CUSTOM || PRESETS.iter().any(|(n, _)| *n == p) => p.to_string(),
                _ => preset_name(&out.gains),
            };
        }
        if let Some(p @ Value::Object(_)) = v.get("parametric") {
            out.parametric_enabled = truthy(p.get("enabled"), false);
            out.parametric_preamp = num(p.get("preamp"), 0.0, -GAIN_LIMIT_DB, GAIN_LIMIT_DB);
            if let Some(Value::Array(bands)) = p.get("bands") {
                out.bands = bands
                    .iter()
                    .take(MAX_PARAMETRIC_BANDS)
                    .filter(|b| matches!(b, Value::Object(_)))
                    .map(Band::from_json)
                    .collect();
            }
        }
        out
    }

    pub fn to_json(&self) -> Value {
        let bands: Vec<Value> = self.bands.iter().map(Band::to_json).collect();
        let graphic = ObjectBuilder::new()
            .put("enabled", self.graphic_enabled)
            .put("preset", self.preset.as_str())
            .put("preamp", self.graphic_preamp)
            .put("gains", Value::Array(self.gains.iter().map(|g| Value::Float(*g)).collect()))
            .build();
        let parametric = ObjectBuilder::new()
            .put("enabled", self.parametric_enabled)
            .put("preamp", self.parametric_preamp)
            .put("bands", Value::Array(bands))
            .build();
        ObjectBuilder::new()
            .put("enabled", self.enabled)
            .put("prevent_clipping", self.prevent_clipping)
            .put("graphic", graphic)
            .put("parametric", parametric)
            .build()
    }

    pub fn apply_preset(&mut self, name: &str) {
        if let Some((n, values)) = PRESETS.iter().find(|(n, _)| *n == name) {
            self.gains = *values;
            self.preset = n.to_string();
        }
    }
}

impl Band {
    pub fn from_json(b: &Value) -> Band {
        let kind = b.get("type").and_then(Value::as_str).filter(|k| BAND_TYPES.contains(k)).unwrap_or("peak");
        Band {
            kind: kind.to_string(),
            freq: num(b.get("freq"), 1000.0, MIN_FREQ, MAX_FREQ),
            gain: num(b.get("gain"), 0.0, -GAIN_LIMIT_DB, GAIN_LIMIT_DB),
            q: num(b.get("q"), 1.0, MIN_Q, MAX_Q),
            on: truthy(b.get("on"), true),
        }
    }

    pub fn to_json(&self) -> Value {
        ObjectBuilder::new()
            .put("type", self.kind.as_str())
            .put("freq", self.freq)
            .put("gain", self.gain)
            .put("q", self.q)
            .put("on", self.on)
            .build()
    }
}

/// Normalised RBJ cookbook coefficients (Python `eq.biquad`).
pub fn biquad(kind: &str, freq: f64, gain_db: f64, q: f64, rate: f64) -> Coeffs {
    if rate <= 0.0 || freq <= 0.0 || freq >= 0.49 * rate || q <= 0.0 {
        return IDENTITY;
    }
    if has_gain(kind) && gain_db == 0.0 {
        return IDENTITY;
    }
    let a = 10f64.powf(gain_db / 40.0);
    let w0 = 2.0 * std::f64::consts::PI * freq / rate;
    let (cw, sw) = (w0.cos(), w0.sin());
    let alpha = sw / (2.0 * q);
    let (b0, b1, b2, a0, a1, a2);
    match kind {
        "peak" => {
            b0 = 1.0 + alpha * a;
            b1 = -2.0 * cw;
            b2 = 1.0 - alpha * a;
            a0 = 1.0 + alpha / a;
            a1 = -2.0 * cw;
            a2 = 1.0 - alpha / a;
        }
        "low_shelf" | "high_shelf" => {
            let sa = 2.0 * a.sqrt() * alpha;
            if kind == "low_shelf" {
                b0 = a * ((a + 1.0) - (a - 1.0) * cw + sa);
                b1 = 2.0 * a * ((a - 1.0) - (a + 1.0) * cw);
                b2 = a * ((a + 1.0) - (a - 1.0) * cw - sa);
                a0 = (a + 1.0) + (a - 1.0) * cw + sa;
                a1 = -2.0 * ((a - 1.0) + (a + 1.0) * cw);
                a2 = (a + 1.0) + (a - 1.0) * cw - sa;
            } else {
                b0 = a * ((a + 1.0) + (a - 1.0) * cw + sa);
                b1 = -2.0 * a * ((a - 1.0) + (a + 1.0) * cw);
                b2 = a * ((a + 1.0) + (a - 1.0) * cw - sa);
                a0 = (a + 1.0) - (a - 1.0) * cw + sa;
                a1 = 2.0 * ((a - 1.0) - (a + 1.0) * cw);
                a2 = (a + 1.0) - (a - 1.0) * cw - sa;
            }
        }
        "low_pass" => {
            b0 = (1.0 - cw) / 2.0;
            b1 = 1.0 - cw;
            b2 = (1.0 - cw) / 2.0;
            a0 = 1.0 + alpha;
            a1 = -2.0 * cw;
            a2 = 1.0 - alpha;
        }
        "high_pass" => {
            b0 = (1.0 + cw) / 2.0;
            b1 = -(1.0 + cw);
            b2 = (1.0 + cw) / 2.0;
            a0 = 1.0 + alpha;
            a1 = -2.0 * cw;
            a2 = 1.0 - alpha;
        }
        "notch" => {
            b0 = 1.0;
            b1 = -2.0 * cw;
            b2 = 1.0;
            a0 = 1.0 + alpha;
            a1 = -2.0 * cw;
            a2 = 1.0 - alpha;
        }
        _ => return IDENTITY,
    }
    [b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0]
}

/// Every active biquad, graphic bands first (Python `eq.stages`).
pub fn stages(s: &EqState, rate: f64) -> Vec<Coeffs> {
    let mut out = vec![];
    if !s.enabled {
        return out;
    }
    if s.graphic_enabled {
        for (f, g) in GRAPHIC_FREQS.iter().zip(s.gains.iter()) {
            let c = biquad("peak", *f, *g, GRAPHIC_Q, rate);
            if c != IDENTITY {
                out.push(c);
            }
        }
    }
    if s.parametric_enabled {
        for b in s.bands.iter().filter(|b| b.on) {
            let c = biquad(&b.kind, b.freq, b.gain, b.q, rate);
            if c != IDENTITY {
                out.push(c);
            }
        }
    }
    out
}

/// The combined gain of the biquads at one frequency, in dB.
pub fn magnitude_db(coeffs: &[Coeffs], freq: f64, rate: f64) -> f64 {
    let w = 2.0 * std::f64::consts::PI * freq / rate;
    let (zr, zi) = (w.cos(), -w.sin()); // e^{-jw}
    let (z2r, z2i) = (zr * zr - zi * zi, 2.0 * zr * zi);
    let mut total = 0.0;
    for [b0, b1, b2, a1, a2] in coeffs {
        let (nr, ni) = (b0 + b1 * zr + b2 * z2r, b1 * zi + b2 * z2i);
        let (dr, di) = (1.0 + a1 * zr + a2 * z2r, a1 * zi + a2 * z2i);
        let mag = (nr.hypot(ni)) / (dr.hypot(di));
        total += 20.0 * mag.max(1e-12).log10();
    }
    total
}

pub fn response_freqs() -> Vec<f64> {
    (0..RESPONSE_POINTS).map(|i| 20.0 * 10f64.powf(3.0 * i as f64 / (RESPONSE_POINTS - 1) as f64)).collect()
}

/// (frequency, dB) of the whole EQ including the preamp, for drawing the curve.
pub fn response(s: &EqState, rate: f64) -> Vec<(f64, f64)> {
    let coeffs = stages(s, rate);
    let pre = preamp_db(s, RESPONSE_RATE);
    response_freqs().into_iter().map(|f| (f, magnitude_db(&coeffs, f, rate) + pre)).collect()
}

/// The gain before the filters: the preamps, lowered by the curve's largest
/// boost when "prevent clipping" is on (Python `eq.preamp_db`).
pub fn preamp_db(s: &EqState, rate: f64) -> f64 {
    if !s.enabled {
        return 0.0;
    }
    let mut pre = 0.0;
    if s.graphic_enabled {
        pre += s.graphic_preamp;
    }
    if s.parametric_enabled {
        pre += s.parametric_preamp;
    }
    if s.prevent_clipping {
        let coeffs = stages(s, rate);
        let peak = response_freqs()
            .into_iter()
            .map(|f| magnitude_db(&coeffs, f, rate))
            .fold(f64::NEG_INFINITY, f64::max)
            .max(f64::NEG_INFINITY);
        let peak = if peak.is_finite() { peak } else { 0.0 } + pre;
        if peak > 0.0 {
            pre -= peak;
        }
    }
    pre
}

/// Filters interleaved samples through the EQ (transposed direct form II,
/// double precision state), as the Python reference `eq.Biquad` does.
pub struct EqProcessor {
    rate: f64,
    channels: usize,
    gain: f64,
    coeffs: Vec<Coeffs>,
    z1: Vec<f64>,
    z2: Vec<f64>,
}

impl EqProcessor {
    pub fn new(state: &EqState, rate: f64, channels: usize) -> EqProcessor {
        let mut p = EqProcessor { rate, channels, gain: 1.0, coeffs: vec![], z1: vec![], z2: vec![] };
        p.set(state);
        p
    }

    /// New settings; the filter memory is kept when the stages stay the same.
    pub fn set(&mut self, state: &EqState) {
        let coeffs = stages(state, self.rate);
        self.gain = 10f64.powf(preamp_db(state, RESPONSE_RATE) / 20.0);
        if coeffs.len() != self.coeffs.len() {
            self.z1 = vec![0.0; coeffs.len() * self.channels];
            self.z2 = vec![0.0; coeffs.len() * self.channels];
        }
        self.coeffs = coeffs;
    }

    pub fn is_active(&self) -> bool {
        !self.coeffs.is_empty() || (self.gain - 1.0).abs() > 1e-12
    }

    /// One sample of channel `c` through the pre-amp and the stages (64-bit float).
    #[inline]
    fn sample(&mut self, x: f64, c: usize) -> f64 {
        let ch = self.channels.max(1);
        let mut x = x * self.gain;
        for (k, [b0, b1, b2, a1, a2]) in self.coeffs.iter().enumerate() {
            let idx = k * ch + c;
            let y = b0 * x + self.z1[idx];
            self.z1[idx] = b1 * x - a1 * y + self.z2[idx];
            self.z2[idx] = b2 * x - a2 * y;
            x = y;
        }
        x
    }

    pub fn process(&mut self, samples: &mut [f32]) {
        if !self.is_active() {
            return;
        }
        let ch = self.channels.max(1);
        for (i, s) in samples.iter_mut().enumerate() {
            *s = self.sample(*s as f64, i % ch) as f32;
        }
    }

    /// The same in 64-bit float throughout (2.3: bit-perfect playback with the equalizer
    /// on dithers this into the device's integers, `bitperfect::dithered_int_sample()`).
    pub fn process_f64(&mut self, samples: &mut [f64]) {
        if !self.is_active() {
            return;
        }
        let ch = self.channels.max(1);
        for (i, s) in samples.iter_mut().enumerate() {
            *s = self.sample(*s, i % ch);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json;

    #[test]
    fn coefficients_and_states_equal_python() {
        let data = json::parse(include_str!("../tests/python_eq.json")).unwrap();
        for case in data.get("biquads").unwrap().as_array().unwrap() {
            let c = biquad(
                case.get("kind").unwrap().as_str().unwrap(),
                case.get("freq").unwrap().as_f64().unwrap(),
                case.get("gain").unwrap().as_f64().unwrap(),
                case.get("q").unwrap().as_f64().unwrap(),
                case.get("rate").unwrap().as_f64().unwrap(),
            );
            let want: Vec<f64> =
                case.get("coeffs").unwrap().as_array().unwrap().iter().map(|v| v.as_f64().unwrap()).collect();
            for (a, b) in c.iter().zip(want.iter()) {
                assert!((a - b).abs() <= 1e-12 * b.abs().max(1.0), "{case:?}: {c:?}");
            }
        }
        for st in data.get("states").unwrap().as_array().unwrap() {
            let s = EqState::from_json(st.get("state").unwrap());
            assert_eq!(s, EqState::from_json(st.get("normalized").unwrap()));
            assert_eq!(EqState::from_json(&s.to_json()), s);
            let pre = st.get("preamp").unwrap().as_f64().unwrap();
            assert!((preamp_db(&s, RESPONSE_RATE) - pre).abs() < 1e-9, "{pre}");
            let stg = stages(&s, 44100.0);
            let want = st.get("stages_44100").unwrap().as_array().unwrap();
            assert_eq!(stg.len(), want.len());
            let r = response(&s, RESPONSE_RATE);
            for (i, pair) in st.get("response").unwrap().as_array().unwrap().iter().enumerate() {
                let p = pair.as_array().unwrap();
                let (f, db) = r[i * 20];
                assert!((f - p[0].as_f64().unwrap()).abs() < 1e-9);
                assert!((db - p[1].as_f64().unwrap()).abs() < 1e-9, "{db} vs {:?}", p[1]);
            }
        }
        let presets = data.get("presets").unwrap().as_object().unwrap();
        assert_eq!(presets.len(), PRESETS.len());
        for ((name, values), (pn, pv)) in presets.iter().zip(PRESETS.iter()) {
            assert_eq!(name, pn);
            let v: Vec<f64> = values.as_array().unwrap().iter().map(|x| x.as_f64().unwrap()).collect();
            assert_eq!(v, pv.to_vec());
        }
    }

    #[test]
    fn processor_matches_the_reference_and_skips_when_flat() {
        let mut s = EqState::default();
        s.enabled = true;
        s.apply_preset("Rock");
        let mut p = EqProcessor::new(&s, 44100.0, 2);
        let mut buf: Vec<f32> = (0..2000).map(|i| ((i as f32) * 0.37).sin() * 0.2).collect();
        let orig = buf.clone();
        p.process(&mut buf);
        assert!(buf.iter().zip(orig.iter()).any(|(a, b)| (a - b).abs() > 1e-4));
        let mut flat = EqProcessor::new(&EqState::default(), 44100.0, 2);
        let mut same = orig.clone();
        flat.process(&mut same);
        assert_eq!(same, orig);
        // the 64-bit path gives the same samples before they are rounded to f32
        let mut wide: Vec<f64> = orig.iter().map(|x| *x as f64).collect();
        EqProcessor::new(&s, 44100.0, 2).process_f64(&mut wide);
        assert!(buf.iter().zip(wide.iter()).all(|(a, b)| *a == *b as f32));
        let mut flat_wide: Vec<f64> = orig.iter().map(|x| *x as f64).collect();
        flat.process_f64(&mut flat_wide);
        assert!(flat_wide.iter().zip(orig.iter()).all(|(a, b)| *a == *b as f64));
    }
}

// ------------------------------------------------------------- preset files ---
// Python `export_json`, `export_apo` and `import_eq`: Real-Hi-Res's own .json
// presets and Equalizer APO / AutoEQ text, with the same output and notes.

pub const EQ_FILE_FORMAT: &str = "real-hi-res-eq";

fn apo_type(kind: &str) -> &'static str {
    match kind {
        "low_shelf" => "LSC",
        "high_shelf" => "HSC",
        "low_pass" => "LPQ",
        "high_pass" => "HPQ",
        "notch" => "NO",
        _ => "PK",
    }
}

fn apo_in(t: &str) -> Option<&'static str> {
    Some(match t.to_ascii_uppercase().as_str() {
        "PK" | "PEQ" | "MODAL" => "peak",
        "LS" | "LSC" | "LSQ" => "low_shelf",
        "HS" | "HSC" | "HSQ" => "high_shelf",
        "LP" | "LPQ" => "low_pass",
        "HP" | "HPQ" => "high_pass",
        "NO" => "notch",
        _ => return None,
    })
}

#[derive(Clone, Debug, PartialEq)]
pub struct EqFileError(pub String);

impl std::fmt::Display for EqFileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// A preset as Real-Hi-Res's own .json file.
pub fn export_json(name: &str, state: &EqState) -> String {
    let data = ObjectBuilder::new()
        .put("format", EQ_FILE_FORMAT)
        .put("version", 1i64)
        .put("name", name)
        .put("eq", state.to_json())
        .build();
    let style = crate::json::Style { indent: Some(2), compact: false, ensure_ascii: false, sort_keys: true };
    crate::json::dumps(&data, style) + "\n"
}

/// A preset as Equalizer APO / AutoEQ text.
pub fn export_apo(name: &str, state: &EqState) -> String {
    let f = crate::pyfmt::f;
    let mut s = state.clone();
    s.enabled = true;
    let mut lines = vec![
        format!("# {name}"),
        "# Equalizer preset exported by Universal Music Player".to_string(),
        format!("Preamp: {} dB", f(preamp_db(&s, RESPONSE_RATE), 1)),
    ];
    let mut n = 0;
    if s.graphic_enabled {
        for (fr, g) in GRAPHIC_FREQS.iter().zip(s.gains.iter()) {
            if g.abs() >= 1e-9 {
                n += 1;
                lines.push(format!("Filter {n}: ON PK Fc {} Hz Gain {} dB Q {}", f(*fr, 1), f(*g, 1), f(GRAPHIC_Q, 2)));
            }
        }
    }
    if s.parametric_enabled {
        for b in &s.bands {
            n += 1;
            let gain = if has_gain(&b.kind) { format!(" Gain {} dB", f(b.gain, 1)) } else { String::new() };
            lines.push(format!(
                "Filter {n}: {} {} Fc {} Hz{gain} Q {}",
                if b.on { "ON" } else { "OFF" },
                apo_type(&b.kind),
                f(b.freq, 1),
                f(b.q, 2)
            ));
        }
    }
    lines.join("\n") + "\n"
}

fn parse_float(text: &str) -> Option<f64> {
    let v: f64 = text.replace(',', ".").parse().ok()?;
    v.is_finite().then_some(v)
}

/// `[-+]?[\d.,]+` at the start of `s`: (the number text, the rest).
fn number_prefix(s: &str, signed: bool) -> Option<(&str, &str)> {
    let b = s.as_bytes();
    let mut i = 0;
    if signed && i < b.len() && (b[i] == b'-' || b[i] == b'+') {
        i += 1;
    }
    let start_digits = i;
    while i < b.len() && (b[i].is_ascii_digit() || b[i] == b'.' || b[i] == b',') {
        i += 1;
    }
    (i > start_digits).then(|| (&s[..i], &s[i..]))
}

fn starts_ci<'a>(s: &'a str, word: &str) -> Option<&'a str> {
    (s.len() >= word.len() && s.is_char_boundary(word.len()) && s[..word.len()].eq_ignore_ascii_case(word))
        .then(|| &s[word.len()..])
}

/// `\bkw\s+NUMBER\s*unit` anywhere in `s` (case-insensitive).
fn find_value(s: &str, kw: &str, unit: Option<&str>, signed: bool) -> Option<f64> {
    let lower = s.to_ascii_lowercase();
    let mut from = 0;
    while let Some(i) = lower[from..].find(kw).map(|i| i + from) {
        from = i + 1;
        let boundary = i == 0 || !lower.as_bytes()[i - 1].is_ascii_alphanumeric() && lower.as_bytes()[i - 1] != b'_';
        if !boundary {
            continue;
        }
        let after = &s[i + kw.len()..];
        let trimmed = after.trim_start();
        if trimmed.len() == after.len() {
            continue; // \s+ needs at least one space
        }
        let Some((num, rest)) = number_prefix(trimmed, signed) else { continue };
        if let Some(u) = unit {
            if starts_ci(rest.trim_start(), u).is_none() {
                continue;
            }
        }
        return parse_float(num);
    }
    None
}

fn graphic_from_points(points: &[(f64, f64)]) -> [f64; 10] {
    let mut pts: Vec<(f64, f64)> = points.iter().copied().filter(|p| p.0 > 0.0).collect();
    pts.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let mut out = [0.0; 10];
    for (k, f) in GRAPHIC_FREQS.iter().enumerate() {
        let (first, last) = (pts[0], pts[pts.len() - 1]);
        let g = if *f <= first.0 {
            first.1
        } else if *f >= last.0 {
            last.1
        } else {
            let mut g = first.1;
            for w in pts.windows(2) {
                let ((f0, g0), (f1, g1)) = (w[0], w[1]);
                if f0 <= *f && *f <= f1 {
                    g = if f1 == f0 { g0 } else { g0 + (g1 - g0) * (f / f0).ln() / (f1 / f0).ln() };
                    break;
                }
            }
            g
        };
        out[k] = if g.is_nan() { 0.0 } else { g.clamp(-GAIN_LIMIT_DB, GAIN_LIMIT_DB) };
    }
    out
}

/// (name, state, notes) from the text of a preset file (Python `import_eq`).
pub fn import_eq(text: &str, fallback_name: &str) -> Result<(String, EqState, Vec<String>), EqFileError> {
    let text = text.trim_start_matches('\u{feff}');
    let mut notes: Vec<String> = vec![];
    if let Ok(data @ Value::Object(_)) = crate::json::parse(text) {
        if let Some(e @ Value::Object(_)) = data.get("eq") {
            let name = match data.get("name").and_then(Value::as_str) {
                Some(n) if !crate::pyunicode::strip(n).is_empty() => crate::pyunicode::strip(n).to_string(),
                _ => crate::pyunicode::strip(fallback_name).to_string(),
            };
            return Ok((name, EqState::from_json(e), notes));
        }
        if data.get("graphic").is_some() || data.get("parametric").is_some() {
            return Ok((fallback_name.to_string(), EqState::from_json(&data), notes));
        }
        return Err(EqFileError("This .json file is not an equalizer preset of Universal Music Player.".into()));
    }
    let mut preamp = 0.0;
    let mut bands: Vec<Band> = vec![];
    let mut graphic: Option<[f64; 10]> = None;
    let mut skipped: Vec<String> = vec![];
    let mut found = false;
    for raw in crate::lyrics::splitlines(text) {
        let line = raw.split('#').next().unwrap_or("").trim();
        if line.is_empty() {
            continue;
        }
        if let Some(rest) = starts_ci(line, "preamp") {
            let rest = rest.trim_start();
            if let Some(rest) = rest.strip_prefix(':') {
                if let Some((num, after)) = number_prefix(rest.trim_start(), true) {
                    if starts_ci(after.trim_start(), "db").is_some() {
                        if let Some(v) = parse_float(num) {
                            preamp += v;
                            found = true;
                        }
                        continue;
                    }
                }
            }
        }
        if let Some(rest) = starts_ci(line, "graphiceq") {
            if let Some(rest) = rest.trim_start().strip_prefix(':') {
                let mut points = vec![];
                for part in rest.split(';') {
                    let xs: Vec<&str> = part.split_whitespace().collect();
                    if xs.len() == 2 {
                        if let (Some(a), Some(b)) = (parse_float(xs[0]), parse_float(xs[1])) {
                            if a > 0.0 {
                                points.push((a, b));
                            }
                        }
                    }
                }
                if !points.is_empty() {
                    graphic = Some(graphic_from_points(&points));
                    found = true;
                }
                continue;
            }
        }
        let Some(rest) = starts_ci(line, "filter") else { continue };
        let rest = rest.trim_start().trim_start_matches(|c: char| c.is_ascii_digit()).trim_start();
        let Some(rest) = rest.strip_prefix(':') else { continue };
        let rest = rest.trim_start();
        let (on, rest) = if let Some(r) = starts_ci(rest, "off") {
            (false, r)
        } else if let Some(r) = starts_ci(rest, "on") {
            (true, r)
        } else {
            continue;
        };
        let after = rest.trim_start();
        if after.len() == rest.len() {
            continue;
        }
        let kind_len = after.bytes().take_while(|b| b.is_ascii_alphabetic()).count();
        if kind_len == 0 {
            continue;
        }
        found = true;
        let (kind_text, params) = after.split_at(kind_len);
        let Some(kind) = apo_in(kind_text) else {
            let up = kind_text.to_ascii_uppercase();
            if !skipped.contains(&up) {
                skipped.push(up);
            }
            continue;
        };
        let Some(fc) = find_value(params, "fc", Some("hz"), true) else { continue };
        let gain = find_value(params, "gain", Some("db"), true).unwrap_or(0.0);
        let mut q = find_value(params, "q", None, false);
        if q.is_none() {
            if let Some(oct) = find_bw(params) {
                if oct != 0.0 {
                    let n = 2f64.powf(oct);
                    q = Some(n.sqrt() / (n - 1.0));
                }
            }
        }
        let q = q.unwrap_or(if matches!(kind, "low_shelf" | "high_shelf" | "low_pass" | "high_pass") {
            0.5f64.sqrt()
        } else {
            1.0
        });
        let b = ObjectBuilder::new().put("type", kind).put("freq", fc).put("gain", gain).put("q", q).put("on", on).build();
        bands.push(Band::from_json(&b));
    }
    if !found {
        return Err(EqFileError(
            "This file is not an equalizer preset Universal Music Player can read: it reads its own .json presets and \
             Equalizer APO or AutoEQ text (ParametricEQ or GraphicEQ)."
                .into(),
        ));
    }
    if !skipped.is_empty() {
        notes.push(format!("Filters of the type {} are not supported and were left out.", skipped.join(", ")));
    }
    if bands.len() > MAX_PARAMETRIC_BANDS {
        notes.push(format!("The file has {} filters; the first {MAX_PARAMETRIC_BANDS} were imported.", bands.len()));
        bands.truncate(MAX_PARAMETRIC_BANDS);
    }
    if preamp.abs() > GAIN_LIMIT_DB {
        notes.push(format!("The pre-amp of {} dB was limited to 12 dB.", crate::pyfmt::f(preamp, 1)));
    }
    let clamp = |x: f64| x.clamp(-GAIN_LIMIT_DB, GAIN_LIMIT_DB);
    let mut st = EqState { enabled: true, ..EqState::default() };
    match graphic {
        Some(g) => {
            st.graphic_enabled = true;
            st.gains = g;
            st.preset = preset_name(&g);
            st.graphic_preamp = if bands.is_empty() { clamp(preamp) } else { 0.0 };
        }
        None => st.graphic_enabled = false,
    }
    if !bands.is_empty() {
        st.parametric_enabled = true;
        st.parametric_preamp = clamp(preamp);
        st.bands = bands;
    }
    Ok((fallback_name.to_string(), st, notes))
}

/// `\bbw\s+oct\s+NUMBER`.
fn find_bw(s: &str) -> Option<f64> {
    let lower = s.to_ascii_lowercase();
    let mut from = 0;
    while let Some(i) = lower[from..].find("bw").map(|i| i + from) {
        from = i + 1;
        if i > 0 && (lower.as_bytes()[i - 1].is_ascii_alphanumeric() || lower.as_bytes()[i - 1] == b'_') {
            continue;
        }
        let after = &s[i + 2..];
        let t = after.trim_start();
        if t.len() == after.len() {
            continue;
        }
        let Some(rest) = starts_ci(t, "oct") else { continue };
        let t2 = rest.trim_start();
        if t2.len() == rest.len() {
            continue;
        }
        if let Some((num, _)) = number_prefix(t2, false) {
            return parse_float(num);
        }
    }
    None
}
