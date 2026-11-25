//! Bit-perfect playback (2.1): the decisions and the texts, shared by both
//! players (the port of `realhires/bitperfect.py`; `tests/bitperfect_vectors.rs`
//! checks it against Python's answers in `python_bitperfect.json`).
//!
//! With the setting `bit_perfect` on, the player sends each song's samples to
//! the sound device as they are in the file: at the song's own sample rate,
//! without the volume or resampling, and with the device to itself where the
//! system allows it (WASAPI exclusive mode on Windows, the ALSA hardware device
//! on Linux; on macOS the device is switched to the song's rate).  Since 2.3 the
//! equalizer is applied when it is on (in 64-bit float, with TPDF dither of
//! ±1 LSB when the device takes integers, `dithered_int_sample()`); everything
//! else stays, and `status_eq(…, true)` says so.  The engine asks the device what it offers (`Mode`s), `plan()`
//! chooses the output rate and sample format, and `status()` and
//! `output_label()` give the Playback info's lines.
//!
//! * Output format: 16-bit songs I16, I32, I24, F32; 24-bit songs I32, I24,
//!   F32; 32-bit integer songs I32, F32, I24; float and lossy songs F32, I32,
//!   I24, I16 (the first one the device has at the chosen rate).  The engine
//!   decodes to 32-bit float, which holds integers of up to 24 bits exactly,
//!   and `int_sample()` turns floats into integers as round(x * 2^(bits - 1)),
//!   clamped, so the device gets the file's own integers.
//! * The rate is the song's when the device offers it; otherwise the next
//!   higher rate (a whole multiple of the song's when there is one), else the
//!   highest lower rate (one the song's rate is a multiple of, if any).

use crate::pyfmt;

/// The sample formats the engine writes.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum Format {
    I16,
    I24,
    I32,
    #[default]
    F32,
}

impl Format {
    pub const ALL: [Format; 4] = [Format::I16, Format::I24, Format::I32, Format::F32];

    /// The name Python uses ("i16", "i24", "i32", "f32").
    pub fn name(self) -> &'static str {
        match self {
            Format::I16 => "i16",
            Format::I24 => "i24",
            Format::I32 => "i32",
            Format::F32 => "f32",
        }
    }

    pub fn from_name(name: &str) -> Option<Format> {
        Format::ALL.into_iter().find(|f| f.name() == name)
    }

    /// The bits of an integer format's container (32 for F32).
    pub fn container_bits(self) -> u32 {
        match self {
            Format::I16 => 16,
            Format::I24 => 24,
            Format::I32 | Format::F32 => 32,
        }
    }
}

/// How the device was reached.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Access {
    /// Windows: WASAPI exclusive mode.
    Wasapi,
    /// Linux: the ALSA hardware device (hw:N).
    Alsa,
    /// macOS: the device's nominal sample rate set to the song's.
    CoreAudio,
    /// The test and null outputs (no system mixer in between).
    Direct,
}

impl Access {
    pub fn name(self) -> &'static str {
        match self {
            Access::Wasapi => "wasapi",
            Access::Alsa => "alsa",
            Access::CoreAudio => "coreaudio",
            Access::Direct => "direct",
        }
    }

    pub fn from_name(name: &str) -> Option<Access> {
        [Access::Wasapi, Access::Alsa, Access::CoreAudio, Access::Direct].into_iter().find(|a| a.name() == name)
    }
}

pub const OFF: &str = "Off";
pub const ON_IDLE: &str = "On";
pub const BUSY: &str = "The device is in use by another program, so the sound is mixed by the system.";
pub const LOSSY_NOTE: &str = "The song is decoded from a lossy format, so it is played as decoded.";
/// 2.3: the equalizer works with bit-perfect playback (it was bypassed before).
pub const EQ_ON: &str = "The equalizer is on, so the samples are changed.";
pub const EQ_NOTE: &str = "Bit-perfect playback is on: with the equalizer on, the sound is not bit-perfect. \
                           The device still gets the song's own sample rate.";
/// The Python app on Windows: miniaudio opens a WASAPI exclusive stream only in the device's Default Format.
pub const WASAPI_FORMAT_NOTE: &str = "Windows gives the device to this app alone only in the format chosen in its \
                                      Sound settings (the device's Properties, Advanced, Default Format).";

/// The rates a device with a range of rates is tried at, as miniaudio and cpal list them.
pub const STANDARD_RATES: [u32; 16] = [
    8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000, 705600, 768000,
];

/// What the decision needs to know about a song.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Song {
    pub rate: u32,
    pub channels: u32,
    /// bits per sample of the file (0: unknown)
    pub bits: u32,
    pub is_float: bool,
    pub is_lossy: bool,
}

/// One thing the device offers: a format at rates `min_rate..=max_rate` with
/// `channels` channels (0: any number).  `format` None: one the engine cannot write.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Mode {
    pub format: Option<Format>,
    pub min_rate: u32,
    pub max_rate: u32,
    pub channels: u32,
}

impl Mode {
    pub fn new(format: Format, min_rate: u32, max_rate: u32, channels: u32) -> Mode {
        Mode { format: Some(format), min_rate, max_rate, channels }
    }
}

/// What the device is opened with for a song, and why it is not bit-perfect.
#[derive(Clone, Debug, PartialEq)]
pub struct Plan {
    pub rate: u32,
    pub format: Format,
    /// the device's channels (2 = stereo)
    pub channels: u32,
    /// full sentences; any makes it "No"
    pub reasons: Vec<String>,
}

impl Plan {
    pub fn resampled(&self) -> bool {
        self.reasons.iter().any(|r| r.starts_with("The device cannot play "))
    }
}

/// 96000 -> "96 kHz", 44100 -> "44.1 kHz" (Python's `f"{hz / 1000:g} kHz"`).
pub fn fmt_khz(hz: u32) -> String {
    if hz == 0 {
        "—".into()
    } else {
        format!("{} kHz", pyfmt::g(hz as f64 / 1000.0))
    }
}

/// The integer bits the decision works with: 0 for float and lossy songs, 24 when unknown.
pub fn song_bits(song: &Song) -> u32 {
    if song.is_float || song.is_lossy {
        0
    } else if song.bits > 0 {
        song.bits
    } else {
        24
    }
}

/// "32-bit float", "16-bit integer", "24-bit integer in 32-bit" (`bits`: the
/// song's integer bits, 0 for float and lossy songs).
pub fn format_label(format: Format, bits: u32) -> String {
    if format == Format::F32 {
        return "32-bit float".into();
    }
    let size = format.container_bits();
    if bits > 0 && bits < size {
        format!("{bits}-bit integer in {size}-bit")
    } else {
        format!("{size}-bit integer")
    }
}

pub fn channels_label(n: u32) -> String {
    match n {
        1 => "mono".into(),
        2 => "stereo".into(),
        n => format!("{n} channels"),
    }
}

/// The Playback info's Output line: "96 kHz, 24-bit integer in 32-bit, stereo".
pub fn output_label(rate: u32, format: Format, bits: u32, channels: u32) -> String {
    if rate == 0 {
        return "Not playing".into();
    }
    format!("{}, {}, {}", fmt_khz(rate), format_label(format, bits), channels_label(channels))
}

pub fn access_label(access: Access, rate: u32, device: &str) -> String {
    match access {
        Access::Wasapi => "exclusive mode (WASAPI)".into(),
        Access::Alsa => format!("ALSA hardware device {device}").trim_end().to_string(),
        Access::CoreAudio => format!("the device runs at {} (CoreAudio)", fmt_khz(rate)),
        Access::Direct => {
            if device.is_empty() {
                "the output device".into()
            } else {
                device.into()
            }
        }
    }
}

/// Why the device could not be had for this app alone (it then plays mixed).
pub fn failure_reason(detail: &str) -> String {
    let detail = detail.trim().trim_end_matches('.');
    if detail.is_empty() {
        "The device could not be opened for this app alone, so the sound is mixed by the system.".into()
    } else {
        format!("The device could not be opened for this app alone ({detail}), so the sound is mixed by the system.")
    }
}

pub fn rate_reason(song_rate: u32, out_rate: u32) -> String {
    format!("The device cannot play {}, so the song is resampled to {}.", fmt_khz(song_rate), fmt_khz(out_rate))
}

/// What bit-perfect playback still does while the equalizer changes the samples:
/// the device at the plan's rate, reached as `access` says.
pub fn still_runs(rate: u32, access: Access, device: &str) -> String {
    let khz = fmt_khz(rate);
    match access {
        Access::Wasapi => format!("The device still runs at {khz} in exclusive mode."),
        Access::Alsa if !device.trim().is_empty() => format!("The ALSA hardware device {device} still runs at {khz}."),
        Access::Alsa => format!("The ALSA hardware device still runs at {khz}."),
        Access::CoreAudio => format!("The device still runs at {khz} (CoreAudio)."),
        Access::Direct => format!("The device still runs at {khz}."),
    }
}

/// The Playback info's "Bit-perfect" line: "Off", "Yes: 24-bit, 96 kHz,
/// exclusive mode (WASAPI)", "Yes (decoded lossy audio): …", or "No: " and
/// every reason that applies.  `song`/`plan` None: nothing plays ("On").
pub fn status(song: Option<&Song>, plan: Option<&Plan>, access: Access, device: &str, enabled: bool) -> String {
    status_eq(song, plan, access, device, enabled, false)
}

/// `status()` with `eq_on`: the equalizer changes the song's samples (2.3); when that
/// is the only reason, the line also says what the device still does ("… The device
/// still runs at 96 kHz in exclusive mode.").
pub fn status_eq(song: Option<&Song>, plan: Option<&Plan>, access: Access, device: &str, enabled: bool, eq_on: bool) -> String {
    if !enabled {
        return OFF.into();
    }
    let (Some(song), Some(plan)) = (song, plan) else { return ON_IDLE.into() };
    if !plan.reasons.is_empty() || eq_on {
        let mut reasons = plan.reasons.clone();
        if eq_on {
            reasons.push(EQ_ON.into());
            if plan.reasons.is_empty() {
                reasons.push(still_runs(plan.rate, access, device));
            }
        }
        if song.is_lossy {
            reasons.push(LOSSY_NOTE.into());
        }
        return format!("No: {}", reasons.join(" "));
    }
    let what = if song_bits(song) > 0 {
        if song.bits > 0 {
            format!("{}-bit", song.bits)
        } else {
            format_label(plan.format, 0)
        }
    } else {
        "32-bit float".into()
    };
    let head = if song.is_lossy { "Yes (decoded lossy audio)" } else { "Yes" };
    format!("{head}: {what}, {}, {}", fmt_khz(plan.rate), access_label(access, plan.rate, device))
}

/// The formats a song is best played in, best first, then the others.
pub fn preferred_formats(song: &Song) -> Vec<Format> {
    use Format::*;
    let bits = song_bits(song);
    let mut pref = if bits == 0 {
        vec![F32, I32, I24, I16]
    } else if bits <= 16 {
        vec![I16, I32, I24, F32]
    } else if bits <= 24 {
        vec![I32, I24, F32]
    } else {
        vec![I32, F32, I24]
    };
    for f in [I32, I24, F32, I16] {
        if !pref.contains(&f) {
            pref.push(f);
        }
    }
    pref
}

/// The modes the player can use: known formats, at least two channels (or any).
pub fn usable(modes: &[Mode]) -> Vec<Mode> {
    modes.iter().copied().filter(|m| m.format.is_some() && m.channels != 1 && m.max_rate >= m.min_rate && m.min_rate > 0).collect()
}

/// Every rate the modes offer (a range counts with its standard rates and its ends).
pub fn rates_offered(modes: &[Mode]) -> Vec<u32> {
    let mut out: Vec<u32> = vec![];
    for m in usable(modes) {
        if m.min_rate == m.max_rate {
            out.push(m.min_rate);
            continue;
        }
        out.extend(STANDARD_RATES.iter().copied().filter(|r| (m.min_rate..=m.max_rate).contains(r)));
        out.push(m.min_rate);
        out.push(m.max_rate);
    }
    out.sort_unstable();
    out.dedup();
    out
}

/// The song's rate when offered, else the next higher one (a whole multiple of
/// the song's when there is one), else the highest lower one (preferring rates
/// the song's rate is a multiple of).
pub fn pick_rate(song_rate: u32, rates: &[u32]) -> u32 {
    if rates.is_empty() || rates.contains(&song_rate) || song_rate == 0 {
        return if song_rate > 0 || rates.is_empty() { song_rate } else { *rates.iter().max().unwrap() };
    }
    let higher: Vec<u32> = rates.iter().copied().filter(|r| *r > song_rate).collect();
    if !higher.is_empty() {
        let multiples: Vec<u32> = higher.iter().copied().filter(|r| r % song_rate == 0).collect();
        return *if multiples.is_empty() { &higher } else { &multiples }.iter().min().unwrap();
    }
    let lower: Vec<u32> = rates.iter().copied().filter(|r| *r < song_rate).collect();
    let family: Vec<u32> = lower.iter().copied().filter(|r| song_rate % r == 0).collect();
    *if family.is_empty() { &lower } else { &family }.iter().max().unwrap()
}

/// The rate, format and channels for `song` on a device that offers `modes`
/// ([] = anything), with the reasons it is not bit-perfect.  `failure`: why the
/// device could not be had alone (the song then plays through the system's
/// mixer, as `modes` say).
pub fn plan(song: &Song, modes: &[Mode], failure: &str) -> Plan {
    let good = usable(modes);
    let prefs = preferred_formats(song);
    let (rate, format, channels) = if good.is_empty() {
        (song.rate, prefs[0], 2)
    } else {
        let rate = pick_rate(song.rate, &rates_offered(&good));
        let here: Vec<Mode> = good.iter().copied().filter(|m| m.min_rate <= rate && rate <= m.max_rate).collect();
        let format = prefs.iter().copied().find(|f| here.iter().any(|m| m.format == Some(*f))).unwrap_or(prefs[0]);
        let counts: Vec<u32> = here.iter().filter(|m| m.format == Some(format)).map(|m| m.channels).collect();
        let channels =
            if counts.is_empty() || counts.iter().any(|c| *c == 0 || *c == 2) { 2 } else { *counts.iter().min().unwrap() };
        (rate, format, channels)
    };
    let mut reasons: Vec<String> = vec![];
    if song.rate > 0 && rate != song.rate {
        reasons.push(rate_reason(song.rate, rate));
    }
    if !failure.is_empty() {
        reasons.push(failure.to_string());
    }
    let bits = song_bits(song);
    if bits > 0 {
        if format == Format::I16 && bits > 16 {
            reasons.push(format!("The device has no {bits}-bit format, so the samples are converted."));
        } else if bits > 24 {
            reasons.push(format!("The {bits}-bit samples are played with 24-bit precision."));
        }
    } else if format != Format::F32 {
        reasons.push("The device has no 32-bit float format, so the samples are converted.".into());
    } else if song.is_float && song.bits > 32 {
        reasons.push(format!("The {}-bit float samples are played as 32-bit float.", song.bits));
    }
    if song.channels > 2 {
        reasons.push(format!("This {}-channel song is mixed down to stereo.", song.channels));
    }
    Plan { rate, format, channels, reasons }
}

/// A float sample as the device's integer: round(x * 2^(bits - 1)), clamped,
/// halves to even as Python's `round()` (a float of a file's integer of up to
/// 24 bits gives that integer back, shifted left).  NaN gives 0.
pub fn int_sample(x: f64, format: Format) -> i32 {
    let top = (1i64 << (format.container_bits() - 1)) as f64;
    let v = x * top;
    if v.is_nan() {
        return 0;
    }
    v.clamp(-top, top - 1.0).round_ties_even() as i32
}

/// A float sample changed by the equalizer as the device's integer: round(x *
/// 2^(bits - 1) + d), clamped, where `d` is the TPDF dither in LSB (the sum of
/// two uniform numbers in [-0.5, 0.5), so within ±1 LSB).  The engines add it
/// only to samples the equalizer changed, so with the equalizer off the output
/// stays bit-exact (`int_sample()`).  NaN gives 0.
pub fn dithered_int_sample(x: f64, format: Format, d: f64) -> i32 {
    let top = (1i64 << (format.container_bits() - 1)) as f64;
    let v = x * top;
    if v.is_nan() {
        return 0;
    }
    (v + d).clamp(-top, top - 1.0).round_ties_even() as i32
}

/// The same for the audio callback (f32 in, no NaN check needed beyond it).
#[inline]
pub fn int_sample_f32(x: f32, format: Format) -> i32 {
    int_sample(x as f64, format)
}

/// The sound cards in /proc/asound/cards: (index, id, name), for example
/// " 0 [PCH            ]: HDA-Intel - HDA Intel PCH" gives (0, "PCH", "HDA Intel PCH").
pub fn alsa_cards(text: &str) -> Vec<(u32, String, String)> {
    let mut out = vec![];
    for line in text.lines() {
        let s = line.trim_start_matches([' ', '\t']);
        let digits: String = s.chars().take_while(|c| c.is_ascii_digit()).collect();
        let s = s[digits.len()..].trim_start_matches([' ', '\t']);
        if digits.is_empty() || !s.starts_with('[') {
            continue;
        }
        let Some(end) = s.find(']') else { continue };
        let card_id = s[1..end].trim().to_string();
        let rest = &s[end + 1..];
        let Some(rest) = rest.strip_prefix(':') else { continue };
        let rest = rest.trim();
        let name = match rest.find(" - ") {
            Some(sep) => rest[sep + 3..].trim().to_string(),
            None => rest.to_string(),
        };
        let Ok(index) = digits.parse::<u32>() else { continue };
        out.push((index, card_id, name));
    }
    out
}

fn ascii_digits(s: &str) -> Option<u32> {
    if !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) {
        s.parse().ok()
    } else {
        None
    }
}

/// The ALSA hardware device ("hw:1", "hw:0,3") for the chosen output device:
/// its card and device when the name is an ALSA one ("hw:CARD=PCH,DEV=3",
/// "plughw:1,0", "front:CARD=Audio,DEV=0"), else the card whose name the
/// device's name contains (the longest such), else card 0.
pub fn alsa_device(device: &str, cards: &[(u32, String, String)]) -> String {
    let d = device.trim();
    let mut card: Option<u32> = None;
    let mut dev = 0u32;
    if let Some((prefix, args)) = d.split_once(':') {
        if !prefix.contains(' ') {
            for (i, part) in args.split(',').enumerate() {
                let (mut key, mut value, has_eq) = match part.split_once('=') {
                    Some((k, v)) => (k.trim().to_string(), v.trim().to_string(), true),
                    None => (part.trim().to_string(), String::new(), false),
                };
                if !has_eq {
                    value = key;
                    key = match i {
                        0 => "CARD".into(),
                        1 => "DEV".into(),
                        _ => String::new(),
                    };
                }
                match key.to_uppercase().as_str() {
                    "CARD" => {
                        if let Some(n) = ascii_digits(&value) {
                            card = Some(n);
                        } else if let Some((n, _, _)) =
                            cards.iter().find(|(_, id, _)| id.to_ascii_lowercase() == value.to_ascii_lowercase())
                        {
                            card = Some(*n);
                        }
                    }
                    "DEV" => {
                        if let Some(n) = ascii_digits(&value) {
                            dev = n;
                        }
                    }
                    _ => {}
                }
            }
        }
    }
    if card.is_none() {
        let low = d.to_ascii_lowercase();
        let mut best = 0usize;
        for (n, _, name) in cards {
            let nm = name.trim().to_ascii_lowercase();
            if nm.chars().count() >= 3 && low.contains(&nm) && (card.is_none() || nm.chars().count() > best) {
                card = Some(*n);
                best = nm.chars().count();
            }
        }
    }
    let card = card.unwrap_or(0);
    if dev == 0 {
        format!("hw:{card}")
    } else {
        format!("hw:{card},{dev}")
    }
}
