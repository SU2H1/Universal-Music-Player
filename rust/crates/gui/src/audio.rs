//! The player's audio engine (2.0), as `docs/player-spec.md` describes it.
//!
//! FFmpeg decodes each song to 32-bit float stereo at the output rate; a
//! decoder thread keeps about two seconds of it in `Buffer`, and the output
//! callback (cpal, or a timer thread for the "null" output that tests and
//! machines without a sound device use) takes it from there, runs the
//! equalizer (`realhires::eq::EqProcessor`) and the volume, and reports when a
//! song's first and last samples are heard.  For gapless playback the decoder
//! goes straight on with the next song (`set_next`) when it has the same output
//! rate, so no silence comes between them.
//!
//! Bit-perfect playback (`set_bit_perfect`, 2.1): FFmpeg decodes without `-ar`
//! and without filters (only `pan` copies a mono song to both channels), the
//! gain is exactly 1, and the device is opened at the song's rate in the format
//! `realhires::bitperfect::plan()` picks from what it offers: WASAPI exclusive
//! mode (`audio_wasapi.rs`) on Windows, the ALSA hardware device through cpal on
//! Linux, and on macOS the device at the song's rate (cpal's CoreAudio backend
//! sets the device's nominal rate).  The floats become the device's integers
//! exactly (`bitperfect::int_sample`, in `render_ints()`).  Since 2.3 the
//! equalizer is not bypassed: when it is on, `render_ints()` runs it in 64-bit
//! float and turns the result into the device's integers with TPDF dither of
//! ±1 LSB (`bitperfect::dithered_int_sample`), clamped; with the equalizer off
//! the output stays bit-exact.  When the device cannot be had, the song plays
//! through the normal path and `OutputInfo::bit_perfect` says why.  Gapless
//! joins only songs with the same rate and format.

use std::collections::VecDeque;
use std::io::{BufRead, Read};
use std::process::{Child, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use realhires::bitperfect::{self, Access, Format, Mode, Plan, Song};
use realhires::eq::{EqProcessor, EqState};
use realhires::probe::{command, tool};
use realhires::{n_, tr};

/// The engine always works in stereo.
pub const CHANNELS: usize = 2;
const BLOCK_FRAMES: usize = 4096;
const AHEAD_SECONDS: f64 = 2.0;
/// `REALHIRES_AUDIO_BACKEND=null` plays into a timer instead of a sound device.
pub const BACKEND_ENV: &str = "REALHIRES_AUDIO_BACKEND";

#[derive(Clone, Debug, PartialEq)]
pub enum Event {
    /// The first sample of a song (not of a seek within it) was heard.
    Started { token: u64, path: String },
    /// Its last sample was heard; `chained` when the next song follows gaplessly.
    Ended { token: u64, path: String, chained: bool },
    /// The song could not be decoded.
    Failed { token: u64, path: String, message: String },
    /// The output device failed (unplugged, taken away).
    OutputError(String),
}

/// What the Playback info window shows about the output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct OutputInfo {
    pub device: String,
    pub rate: u32,
    pub channels: u16,
    /// what the device gets: "32-bit float", "24-bit integer in 32-bit"
    pub format: String,
    /// The song's rate when FFmpeg converts it to `rate`.
    pub resampled_from: Option<u32>,
    pub soxr: bool,
    /// the sample format the device is opened with
    pub sample_format: Format,
    /// the Playback info's "Bit-perfect" line ("Off", "Yes: …", "No: …")
    pub bit_perfect: String,
}

pub(crate) enum Item {
    Begin(u64),
    Audio(Vec<f32>, usize),
    Finish(u64, bool),
    Fail(u64, String),
}

#[derive(Default)]
pub(crate) struct Buffer {
    items: VecDeque<Item>,
    frames: usize,
    gen: u64,
}

/// Songs with the same key play one after the other on the open device:
/// rate, sample format, device channels, and whether bit-perfect playback has
/// the device for itself.
type OutKey = (u32, Format, u16, bool);

#[derive(Clone)]
struct Segment {
    id: u64,
    token: u64,
    path: String,
    start: f64,
    rate: u32,
    song: Song,
    /// the bit-perfect decision for this song (None: the setting is off)
    plan: Option<Plan>,
    /// decoded without -ar (bit-perfect at the song's own rate)
    native: bool,
    announce: bool,
}

#[derive(Clone)]
struct Next {
    path: String,
    song: Song,
    /// how the open output would play it (it chains only when equal to the output's)
    key: OutKey,
    plan: Option<Plan>,
    native: bool,
}

pub(crate) struct Shared {
    buf: Mutex<Buffer>,
    paused: AtomicBool,
    gain_bits: AtomicU32,
    eq: Mutex<(u64, EqState)>,
    /// bit-perfect playback: gain exactly 1 (0 when muted); the equalizer is dithered (2.3)
    bit_perfect: AtomicBool,
    heard_seg: AtomicU64,
    heard_frames: AtomicU64,
    segments: Mutex<Vec<Segment>>,
    pub(crate) events: Mutex<Vec<Event>>,
    next: Mutex<Option<Next>>,
    gapless: AtomicBool,
    ids: AtomicU64,
}

impl Shared {
    fn new_id(&self) -> u64 {
        self.ids.fetch_add(1, Ordering::Relaxed) + 1
    }

    fn segment(&self, id: u64) -> Option<Segment> {
        self.segments.lock().unwrap().iter().find(|s| s.id == id).cloned()
    }
}

/// What the output callback keeps between calls.
pub(crate) struct RenderState {
    eq: EqProcessor,
    eq_version: u64,
    scratch: Vec<f32>,
    /// the equalizer's 64-bit output before it is dithered (bit-perfect playback, 2.3)
    wide: Vec<f64>,
    /// SplitMix64 state of the TPDF dither
    dither: u64,
}

impl RenderState {
    pub(crate) fn new(shared: &Shared, rate: u32) -> RenderState {
        let (version, state) = shared.eq.lock().unwrap().clone();
        let seed = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(1);
        RenderState {
            eq: EqProcessor::new(&state, rate as f64, CHANNELS),
            eq_version: version,
            scratch: vec![],
            wide: vec![],
            dither: seed ^ 0x5DEE_CE66_D1CE_4E5B,
        }
    }

    /// A uniform number in [0, 1).
    fn uniform(&mut self) -> f64 {
        self.dither = self.dither.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.dither;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        ((z ^ (z >> 31)) >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }

    /// TPDF dither in LSB: the sum of two uniform numbers in [-0.5, 0.5), within ±1.
    fn tpdf(&mut self) -> f64 {
        self.uniform() + self.uniform() - 1.0
    }

    /// The equalizer as it is set now (taken over between callbacks, never waited for).
    fn update_eq(&mut self, shared: &Shared) {
        if let Ok(eq) = shared.eq.try_lock() {
            if eq.0 != self.eq_version {
                self.eq.set(&eq.1);
                self.eq_version = eq.0;
            }
        }
    }
}

/// Take the stereo frames that are due into `st.scratch` (silence where nothing is).
fn take_due(shared: &Shared, st: &mut RenderState, frames: usize) {
    st.scratch.clear();
    st.scratch.resize(frames * CHANNELS, 0.0);
    if !shared.paused.load(Ordering::Relaxed) {
        let mut filled = 0usize;
        let mut buf = shared.buf.lock().unwrap();
        while filled < frames {
            let Some(item) = buf.items.front_mut() else { break };
            match item {
                Item::Audio(data, pos) => {
                    let n = ((data.len() - *pos) / CHANNELS).min(frames - filled);
                    st.scratch[filled * CHANNELS..(filled + n) * CHANNELS]
                        .copy_from_slice(&data[*pos..*pos + n * CHANNELS]);
                    *pos += n * CHANNELS;
                    filled += n;
                    shared.heard_frames.fetch_add(n as u64, Ordering::Relaxed);
                    if *pos >= data.len() {
                        buf.items.pop_front();
                    }
                    buf.frames = buf.frames.saturating_sub(n);
                }
                _ => {
                    let item = buf.items.pop_front().unwrap();
                    marker(shared, item);
                }
            }
        }
        // markers right after the last sample (a song's end) are due now too
        while matches!(buf.items.front(), Some(Item::Finish(..)) | Some(Item::Fail(..))) {
            let item = buf.items.pop_front().unwrap();
            marker(shared, item);
        }
    }
}

/// The gain of what is due: the volume, or with bit-perfect playback exactly 1 (0 when muted).
fn render_gain(shared: &Shared) -> f32 {
    let gain = f32::from_bits(shared.gain_bits.load(Ordering::Relaxed));
    if shared.bit_perfect.load(Ordering::Relaxed) {
        if gain > 0.0 {
            1.0
        } else {
            0.0
        }
    } else {
        gain
    }
}

/// Fill `out` (interleaved, `dev_channels` per frame; channels after the first
/// two stay silent) with what is due, as floats: the equalizer and the gain (with
/// bit-perfect playback a gain of exactly 1, and the equalizer only when it is on).
pub(crate) fn render(shared: &Shared, st: &mut RenderState, out: &mut [f32], dev_channels: usize) {
    let dev_channels = dev_channels.max(1);
    let frames = out.len() / dev_channels;
    take_due(shared, st, frames);
    let gain = render_gain(shared);
    st.update_eq(shared);
    st.eq.process(&mut st.scratch);
    for (f, frame) in out.chunks_exact_mut(dev_channels).enumerate() {
        let l = (st.scratch[f * 2] * gain).clamp(-1.0, 1.0);
        let r = (st.scratch[f * 2 + 1] * gain).clamp(-1.0, 1.0);
        if dev_channels == 1 {
            frame[0] = 0.5 * (l + r);
        } else {
            frame[0] = l;
            frame[1] = r;
            for x in frame[2..].iter_mut() {
                *x = 0.0;
            }
        }
    }
}

/// Fill `out` with what is due as the integers of `format` (I16, I24 or I32 at the
/// container's scale), for a device that bit-perfect playback opened: the floats as
/// they are (`bitperfect::int_sample`), so the device gets the file's own integers;
/// with the equalizer on, its 64-bit output with TPDF dither of ±1 LSB, rounded and
/// clamped (`bitperfect::dithered_int_sample`).
pub(crate) fn render_ints(shared: &Shared, st: &mut RenderState, out: &mut [i32], dev_channels: usize, format: Format) {
    let dev_channels = dev_channels.max(1);
    let frames = out.len() / dev_channels;
    take_due(shared, st, frames);
    let gain = render_gain(shared) as f64;
    st.update_eq(shared);
    let eq_on = st.eq.is_active() && gain > 0.0;
    if eq_on {
        st.wide.clear();
        st.wide.extend(st.scratch.iter().map(|x| *x as f64 * gain));
        st.eq.process_f64(&mut st.wide);
    }
    for (f, frame) in out.chunks_exact_mut(dev_channels).enumerate() {
        let mut lr = [0i32; 2];
        for (c, v) in lr.iter_mut().enumerate() {
            let i = f * CHANNELS + c;
            *v = if eq_on {
                let d = st.tpdf();
                bitperfect::dithered_int_sample(st.wide[i], format, d)
            } else {
                bitperfect::int_sample(st.scratch[i] as f64 * gain, format)
            };
        }
        if dev_channels == 1 {
            frame[0] = ((lr[0] as i64 + lr[1] as i64) / 2) as i32;
        } else {
            frame[0] = lr[0];
            frame[1] = lr[1];
            for x in frame[2..].iter_mut() {
                *x = 0;
            }
        }
    }
}

fn marker(shared: &Shared, item: Item) {
    let event = match item {
        Item::Begin(id) => {
            shared.heard_seg.store(id, Ordering::Relaxed);
            shared.heard_frames.store(0, Ordering::Relaxed);
            match shared.segment(id) {
                Some(s) if s.announce => Some(Event::Started { token: s.token, path: s.path }),
                _ => None,
            }
        }
        Item::Finish(id, chained) => {
            shared.segment(id).map(|s| Event::Ended { token: s.token, path: s.path, chained })
        }
        Item::Fail(id, message) => shared.segment(id).map(|s| Event::Failed { token: s.token, path: s.path, message }),
        Item::Audio(..) => None,
    };
    if let Some(e) = event {
        shared.events.lock().unwrap().push(e);
    }
}

pub fn has_soxr() -> bool {
    realhires::fix::has_soxr()
}

/// The first line of `ffmpeg -version`, for the Playback info.
pub fn ffmpeg_version() -> String {
    static V: OnceLock<String> = OnceLock::new();
    V.get_or_init(|| {
        command(tool("ffmpeg"))
            .arg("-version")
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
            .ok()
            .and_then(|o| String::from_utf8_lossy(&o.stdout).lines().next().map(str::to_string))
            .unwrap_or_default()
    })
    .clone()
}

/// What bit-perfect playback needs to know about a file (ffprobe; rate 0 when unreadable).
pub fn song_of_file(path: &str) -> Song {
    match realhires::probe::probe(path) {
        Ok(i) => Song {
            rate: i.sample_rate,
            channels: i.channels,
            bits: i.claimed_bits.unwrap_or(0),
            is_float: i.is_float,
            is_lossy: i.is_lossy,
        },
        Err(_) => Song::default(),
    }
}

/// The FFmpeg arguments that decode `path` from `start` seconds for the engine.
/// `native` (bit-perfect at the song's own rate): no `-ar`, the samples as they are.
pub fn decode_args(path: &str, start: f64, song_rate: u32, out_rate: u32, mono: bool, native: bool) -> Vec<String> {
    let mut args: Vec<String> =
        ["-nostdin", "-hide_banner", "-v", "error"].iter().map(|s| s.to_string()).collect();
    if start > 0.0 {
        args.extend(["-ss".into(), format!("{start:.3}")]);
    }
    args.extend(["-i".into(), path.into(), "-map".into(), "0:a:0".into(), "-vn".into()]);
    let mut filters: Vec<String> = vec![];
    if mono {
        filters.push("pan=stereo|c0=c0|c1=c0".into());
    }
    if !native && song_rate != 0 && song_rate != out_rate {
        filters.push(if has_soxr() {
            format!("aresample={out_rate}:resampler=soxr:precision=28")
        } else {
            format!("aresample={out_rate}:filter_size=256:cutoff=0.97:phase_shift=10")
        });
    }
    if !filters.is_empty() {
        args.extend(["-af".into(), filters.join(",")]);
    }
    args.extend(["-ac".to_string(), "2".to_string()]);
    if !native {
        args.extend(["-ar".to_string(), out_rate.to_string()]);
    }
    args.extend(["-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"].iter().map(|s| s.to_string()));
    args
}

struct Job {
    seg: Segment,
    gen: u64,
    mono: bool,
    key: OutKey,
}

fn spawn_decoder(shared: Arc<Shared>, job: Job) {
    std::thread::Builder::new()
        .name("player-decoder".into())
        .spawn(move || decode_loop(shared, job))
        .expect("decoder thread");
}

enum Decoded {
    /// frames pushed, whether FFmpeg ended well, its error text
    Done(usize, bool, String),
    Cancelled,
    Failed(String),
}

/// Run FFmpeg for `job` from `start` seconds, dropping the first `skip`
/// frames, and push its audio into the buffer while it is not cancelled.
fn run_decoder(shared: &Shared, job: &Job, gen: u64, start: f64, mut skip: usize) -> Decoded {
    let args = decode_args(&job.seg.path, start, job.seg.song.rate, job.seg.rate, job.mono, job.seg.native);
    let child = command(tool("ffmpeg"))
        .args(&args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();
    let mut child: Child = match child {
        Ok(c) => c,
        Err(e) => return Decoded::Failed(tr!("FFmpeg could not be started: {e}.", e = e)),
    };
    let stderr = child.stderr.take().map(|err| {
        std::thread::spawn(move || {
            std::io::BufReader::new(err).lines().map_while(Result::ok).collect::<Vec<String>>().join(" ")
        })
    });
    let mut out = child.stdout.take().unwrap();
    let ahead = (AHEAD_SECONDS * job.seg.rate as f64) as usize;
    let mut bytes = vec![0u8; BLOCK_FRAMES * CHANNELS * 4];
    let mut have = 0usize;
    let mut decoded = 0usize;
    let mut cancelled = false;
    loop {
        // wait while enough is buffered
        loop {
            let (frames, g) = {
                let b = shared.buf.lock().unwrap();
                (b.frames, b.gen)
            };
            if g != gen {
                cancelled = true;
                break;
            }
            if frames < ahead {
                break;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        if cancelled {
            break;
        }
        let n = match out.read(&mut bytes[have..]) {
            Ok(0) | Err(_) => 0,
            Ok(n) => n,
        };
        have += n;
        let whole = have - have % (CHANNELS * 4);
        if whole > 0 && (n == 0 || have == bytes.len()) {
            let mut samples: Vec<f32> =
                bytes[..whole].chunks_exact(4).map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]])).collect();
            bytes.copy_within(whole..have, 0);
            have -= whole;
            if skip > 0 {
                let drop = skip.min(samples.len() / CHANNELS);
                samples.drain(..drop * CHANNELS);
                skip -= drop;
            }
            let frames = samples.len() / CHANNELS;
            if frames > 0 {
                decoded += frames;
                let mut buf = shared.buf.lock().unwrap();
                if buf.gen != gen {
                    cancelled = true;
                    break;
                }
                buf.frames += frames;
                buf.items.push_back(Item::Audio(samples, 0));
            }
        }
        if n == 0 {
            break;
        }
    }
    if cancelled {
        let _ = child.kill();
        let _ = child.wait();
        return Decoded::Cancelled;
    }
    let ok = child.wait().map(|s| s.success()).unwrap_or(false);
    let errors = stderr.and_then(|h| h.join().ok()).unwrap_or_default();
    Decoded::Done(decoded, ok, errors)
}

fn decode_loop(shared: Arc<Shared>, mut job: Job) {
    loop {
        let gen = job.gen;
        let alive = |s: &Shared| s.buf.lock().unwrap().gen == gen;
        {
            let mut buf = shared.buf.lock().unwrap();
            if buf.gen != gen {
                return;
            }
            buf.items.push_back(Item::Begin(job.seg.id));
        }
        let mut result = run_decoder(&shared, &job, gen, job.seg.start, 0);
        // FFmpeg cannot seek in some short files (FLAC without a seek table) and
        // then gives nothing: decode from the start and drop the audio before
        if matches!(result, Decoded::Done(0, true, _)) && job.seg.start > 0.0 {
            let skip = (job.seg.start * job.seg.rate as f64).round() as usize;
            result = run_decoder(&shared, &job, gen, 0.0, skip);
        }
        let (decoded, ok, errors) = match result {
            Decoded::Done(d, ok, e) => (d, ok, e),
            Decoded::Cancelled => return,
            Decoded::Failed(why) => {
                let mut buf = shared.buf.lock().unwrap();
                if buf.gen == gen {
                    buf.items.push_back(Item::Fail(job.seg.id, why));
                }
                return;
            }
        };
        let failed = decoded == 0 && !ok;
        // gapless: go straight on with the next song when the open output plays it as it is
        // (the same rate, and with bit-perfect playback the same format)
        let next = if !failed && shared.gapless.load(Ordering::Relaxed) {
            let mut slot = shared.next.lock().unwrap();
            match slot.as_ref() {
                Some(n) if n.key == job.key => slot.take(),
                _ => None,
            }
        } else {
            None
        };
        let mut buf = shared.buf.lock().unwrap();
        if buf.gen != gen {
            return;
        }
        if failed {
            let why = if errors.trim().is_empty() {
                tr!("FFmpeg could not decode this file.").to_string()
            } else {
                tr!("FFmpeg could not decode this file: {errors}", errors = errors.trim())
            };
            buf.items.push_back(Item::Fail(job.seg.id, why));
            return;
        }
        buf.items.push_back(Item::Finish(job.seg.id, next.is_some()));
        drop(buf);
        let Some(n) = next else { return };
        let seg = Segment {
            id: shared.new_id(),
            token: shared.new_id(),
            path: n.path.clone(),
            start: 0.0,
            rate: job.seg.rate,
            song: n.song,
            plan: n.plan.clone(),
            native: n.native,
            announce: true,
        };
        shared.segments.lock().unwrap().push(seg.clone());
        let mono = if n.song.channels > 0 { n.song.channels == 1 } else { song_channels(&n.path) == 1 };
        job = Job { seg, gen, mono, key: job.key };
        if !alive(&shared) {
            return;
        }
    }
}

fn song_channels(path: &str) -> i64 {
    realhires::probe::probe(path).map(|i| i.channels as i64).unwrap_or(2)
}

/// What bit-perfect playback found on the device it opened (the songs after
/// the first one are planned from this, as probing the open device would find
/// it busy), or why it could not have it (`failure`).
#[derive(Clone, Debug, Default)]
struct BpDevice {
    /// the device as the user chose it ("" = the system's default)
    chosen: String,
    access: Option<Access>,
    label: String,
    modes: Vec<Mode>,
    failure: String,
    /// when it was asked (a device that could not be had is asked again after `REPROBE`)
    probed: Option<Instant>,
}

/// How long a device that bit-perfect playback could not have is not asked again
/// (seeking and every song that does not follow gaplessly open the output again).
const REPROBE: Duration = Duration::from_secs(10);

impl BpDevice {
    fn access(&self) -> Access {
        self.access.unwrap_or(Access::Direct)
    }
}

enum Output {
    None,
    Cpal {
        _stream: cpal::Stream,
        info: OutputInfo,
        device: String,
        rates: Vec<(u32, u32)>,
        default_rate: u32,
        key: OutKey,
        bp: Option<BpDevice>,
    },
    #[cfg(windows)]
    Wasapi { _out: crate::audio_wasapi::Exclusive, info: OutputInfo, key: OutKey, bp: BpDevice },
    Null { stop: Arc<AtomicBool>, info: OutputInfo, key: OutKey, bp: Option<BpDevice> },
    /// Tests pull the samples themselves (`Engine::pull`).
    Manual { state: RenderState, info: OutputInfo, key: OutKey, bp: Option<BpDevice> },
}

impl Output {
    fn info(&self) -> Option<&OutputInfo> {
        match self {
            Output::Cpal { info, .. } | Output::Null { info, .. } | Output::Manual { info, .. } => Some(info),
            #[cfg(windows)]
            Output::Wasapi { info, .. } => Some(info),
            Output::None => None,
        }
    }

    fn key(&self) -> Option<OutKey> {
        match self {
            Output::Cpal { key, .. } | Output::Null { key, .. } | Output::Manual { key, .. } => Some(*key),
            #[cfg(windows)]
            Output::Wasapi { key, .. } => Some(*key),
            Output::None => None,
        }
    }

    fn bp(&self) -> Option<&BpDevice> {
        match self {
            Output::Cpal { bp, .. } | Output::Null { bp, .. } | Output::Manual { bp, .. } => bp.as_ref(),
            #[cfg(windows)]
            Output::Wasapi { bp, .. } => Some(bp),
            Output::None => None,
        }
    }
}

/// How a song is opened: the output rate, the bit-perfect decision, whether
/// FFmpeg decodes it without -ar, and the output's key.
struct Opened {
    rate: u32,
    plan: Option<Plan>,
    native: bool,
    key: OutKey,
}

/// A test device for `Engine::manual_device`: the rates and formats it takes
/// (empty: any), and whether another program holds it.
#[derive(Clone, Debug, Default)]
pub struct TestDevice {
    pub rates: Vec<u32>,
    pub formats: Vec<Format>,
    pub busy: bool,
}

/// The engine the player window owns (one per app).
pub struct Engine {
    shared: Arc<Shared>,
    output: Output,
    manual: bool,
    test_device: TestDevice,
    device: String,
    bit_perfect: bool,
    pub last_error: String,
    current: Option<Segment>,
}

fn null_backend() -> bool {
    std::env::var(BACKEND_ENV).map(|v| v.eq_ignore_ascii_case("null")).unwrap_or(false)
}

/// The names of the system's output devices.
pub fn output_devices() -> Vec<String> {
    if null_backend() {
        return vec![];
    }
    let host = cpal::default_host();
    let mut names: Vec<String> = host
        .output_devices()
        .map(|it| it.filter_map(|d| d.description().ok().map(|x| x.name().to_string())).collect())
        .unwrap_or_default();
    names.dedup();
    names
}

/// The sample format in English (kept in `OutputInfo`; the Playback info
/// window shows it with `i18n::tr()`).
fn format_label(f: cpal::SampleFormat) -> String {
    use cpal::SampleFormat as F;
    match f {
        F::F32 => n_!("32-bit float").into(),
        F::F64 => n_!("64-bit float").into(),
        F::I16 | F::U16 => n_!("16-bit").into(),
        F::I24 | F::U24 => n_!("24-bit").into(),
        F::I32 | F::U32 => n_!("32-bit").into(),
        F::I8 | F::U8 => n_!("8-bit").into(),
        other => format!("{other:?}"),
    }
}

fn format_of(f: cpal::SampleFormat) -> Option<Format> {
    match f {
        cpal::SampleFormat::I16 => Some(Format::I16),
        cpal::SampleFormat::I24 => Some(Format::I24),
        cpal::SampleFormat::I32 => Some(Format::I32),
        cpal::SampleFormat::F32 => Some(Format::F32),
        _ => None,
    }
}

/// Why opening a device for this app alone failed, as the reason the Playback info gives.
#[cfg_attr(windows, allow(dead_code))]
fn cpal_failure(e: &cpal::Error) -> String {
    if e.kind() == cpal::ErrorKind::DeviceBusy {
        bitperfect::BUSY.into()
    } else {
        bitperfect::failure_reason(&e.to_string())
    }
}

impl Engine {
    pub fn new() -> Engine {
        Engine::with(false)
    }

    /// An engine without a device, whose samples `pull()` takes (tests).
    pub fn manual() -> Engine {
        Engine::with(true)
    }

    /// A test engine whose pretend device takes only `device`'s rates and formats.
    pub fn manual_device(device: TestDevice) -> Engine {
        let mut e = Engine::with(true);
        e.test_device = device;
        e
    }

    fn with(manual: bool) -> Engine {
        let shared = Arc::new(Shared {
            buf: Mutex::new(Buffer::default()),
            paused: AtomicBool::new(false),
            gain_bits: AtomicU32::new(1.0f32.to_bits()),
            eq: Mutex::new((1, EqState::default())),
            bit_perfect: AtomicBool::new(false),
            heard_seg: AtomicU64::new(0),
            heard_frames: AtomicU64::new(0),
            segments: Mutex::new(vec![]),
            events: Mutex::new(vec![]),
            next: Mutex::new(None),
            gapless: AtomicBool::new(true),
            ids: AtomicU64::new(0),
        });
        Engine {
            shared,
            output: Output::None,
            manual,
            test_device: TestDevice::default(),
            device: String::new(),
            bit_perfect: false,
            last_error: String::new(),
            current: None,
        }
    }

    pub fn set_device(&mut self, name: &str) {
        if self.device != name {
            self.device = name.to_string();
            self.restart_here();
        }
    }

    /// Bit-perfect playback on or off: from the next song, and at once for the
    /// song playing, which restarts at its position (the device is opened again).
    pub fn set_bit_perfect(&mut self, on: bool) {
        if self.bit_perfect != on {
            self.bit_perfect = on;
            self.shared.bit_perfect.store(on, Ordering::Relaxed);
            self.restart_here();
        }
    }

    pub fn bit_perfect(&self) -> bool {
        self.bit_perfect
    }

    /// Open the output again and go on with the song where it is now.
    fn restart_here(&mut self) {
        if let Some(cur) = self.heard_segment().or_else(|| self.current.clone()) {
            let pos = self.position().unwrap_or(cur.start);
            let paused = self.is_paused();
            self.output = Output::None;
            self.start(&cur.path, pos, cur.song, Some(cur.token), paused);
        } else {
            self.output = Output::None;
        }
    }

    pub fn set_gain(&self, gain: f32) {
        self.shared.gain_bits.store(gain.max(0.0).to_bits(), Ordering::Relaxed);
    }

    /// The gain the samples get (exactly 1, or 0 when muted, with bit-perfect playback).
    pub fn gain(&self) -> f32 {
        let g = f32::from_bits(self.shared.gain_bits.load(Ordering::Relaxed));
        if self.bit_perfect {
            if g > 0.0 {
                1.0
            } else {
                0.0
            }
        } else {
            g
        }
    }

    pub fn set_eq(&self, state: &EqState) {
        let mut eq = self.shared.eq.lock().unwrap();
        if eq.1 != *state {
            eq.0 += 1;
            eq.1 = state.clone();
        }
    }

    pub fn set_gapless(&self, on: bool) {
        self.shared.gapless.store(on, Ordering::Relaxed);
    }

    /// Tests: the song set to follow gaplessly.
    #[cfg(test)]
    pub fn next_path(&self) -> Option<String> {
        self.shared.next.lock().unwrap().as_ref().map(|n| n.path.clone())
    }

    /// The song to go on with gaplessly when this one ends (None = stop there).
    pub fn set_next(&self, next: Option<(&str, Song)>) {
        let next = next.map(|(p, song)| {
            let song = if song.rate == 0 { song_of_file(p) } else { song };
            let o = self.plan_on_open_output(&song);
            Next { path: p.to_string(), song, key: o.key, plan: o.plan, native: o.native }
        });
        *self.shared.next.lock().unwrap() = next;
    }

    /// How the open output would play `song` (without opening anything).
    fn plan_on_open_output(&self, song: &Song) -> Opened {
        let Some(key) = self.output.key() else {
            return Opened { rate: song.rate, plan: None, native: false, key: (0, Format::F32, 0, false) };
        };
        if !self.bit_perfect {
            let rate = self.rate_for(song.rate);
            return Opened { rate, plan: None, native: false, key: (rate, key.1, key.2, false) };
        }
        let bp = self.output.bp().cloned().unwrap_or_default();
        if key.3 {
            let plan = bitperfect::plan(song, &bp.modes, "");
            let native = plan.rate == song.rate;
            return Opened { rate: plan.rate, key: (plan.rate, plan.format, plan.channels as u16, true), plan: Some(plan), native };
        }
        // bit-perfect playback could not have the device: the normal path (gain 1)
        let rate = self.rate_for(song.rate);
        let plan = bitperfect::plan(song, &[Mode::new(Format::F32, rate, rate, 2)], &bp.failure);
        Opened { rate, plan: Some(plan), native: false, key: (rate, key.1, key.2, false) }
    }

    /// The output rate a song of `song_rate` gets on the open output.
    pub fn rate_for(&self, song_rate: u32) -> u32 {
        match &self.output {
            Output::Cpal { rates, default_rate, .. } => {
                if song_rate > 0 && rates.iter().any(|(lo, hi)| *lo <= song_rate && song_rate <= *hi) {
                    song_rate
                } else {
                    *default_rate
                }
            }
            _ => self.test_rate(song_rate),
        }
    }

    /// The rate the test and null outputs run a song at on the normal path.
    fn test_rate(&self, song_rate: u32) -> u32 {
        let rates = &self.test_device.rates;
        if song_rate == 0 {
            rates.first().copied().unwrap_or(48_000)
        } else if rates.is_empty() || rates.contains(&song_rate) {
            song_rate
        } else {
            rates[0]
        }
    }

    pub fn info(&self) -> Option<OutputInfo> {
        let mut info = self.output.info()?.clone();
        let seg = self.heard_segment().or_else(|| self.current.clone());
        if let Some(seg) = &seg {
            info.resampled_from = (seg.song.rate != 0 && seg.song.rate != info.rate).then_some(seg.song.rate);
        }
        let key = self.output.key().unwrap_or((0, Format::F32, 0, false));
        if key.3 {
            let bits = seg.as_ref().map(|s| bitperfect::song_bits(&s.song)).unwrap_or(0);
            info.format = bitperfect::format_label(key.1, bits);
        }
        info.bit_perfect = if !self.bit_perfect {
            bitperfect::OFF.into()
        } else {
            let bp = self.output.bp().cloned().unwrap_or_default();
            match seg.as_ref().filter(|s| s.plan.is_some() && s.rate > 0) {
                Some(s) => {
                    // 2.3: the equalizer is heard with bit-perfect playback too, and then says so
                    let eq = self.shared.eq.lock().unwrap().1.clone();
                    let eq_on = EqProcessor::new(&eq, s.rate as f64, CHANNELS).is_active();
                    bitperfect::status_eq(Some(&s.song), s.plan.as_ref(), bp.access(), &bp.label, true, eq_on)
                }
                None => bitperfect::ON_IDLE.into(),
            }
        };
        Some(info)
    }

    /// Play `path` from `start` seconds; returns the token of this song.
    /// `song` comes from the library (rate 0: ffprobe reads it).
    pub fn play(&mut self, path: &str, start: f64, song: Song) -> u64 {
        self.start(path, start, song, None, false)
    }

    fn start(&mut self, path: &str, start: f64, song: Song, token: Option<u64>, paused: bool) -> u64 {
        let song = if song.rate == 0 || song.channels == 0 {
            let probed = song_of_file(path);
            if probed.rate > 0 {
                probed
            } else {
                song
            }
        } else {
            song
        };
        let gen = {
            let mut buf = self.shared.buf.lock().unwrap();
            buf.gen += 1;
            buf.items.clear();
            buf.frames = 0;
            buf.gen
        };
        self.shared.paused.store(paused, Ordering::Relaxed);
        *self.shared.next.lock().unwrap() = None;
        self.shared.heard_seg.store(0, Ordering::Relaxed);
        self.shared.heard_frames.store(0, Ordering::Relaxed);
        let opened = match self.open_for(&song) {
            Ok(o) => o,
            Err(e) => {
                self.last_error = e.clone();
                let token = token.unwrap_or_else(|| self.shared.new_id());
                let seg = Segment {
                    id: self.shared.new_id(),
                    token,
                    path: path.into(),
                    start,
                    rate: 0,
                    song,
                    plan: None,
                    native: false,
                    announce: true,
                };
                self.shared.segments.lock().unwrap().push(seg.clone());
                self.shared.events.lock().unwrap().push(Event::Failed { token, path: path.into(), message: e });
                self.current = Some(seg);
                return token;
            }
        };
        self.last_error.clear();
        let announce = token.is_none();
        let token = token.unwrap_or_else(|| self.shared.new_id());
        let seg = Segment {
            id: self.shared.new_id(),
            token,
            path: path.into(),
            start,
            rate: opened.rate,
            song,
            plan: opened.plan,
            native: opened.native,
            announce,
        };
        {
            let mut segs = self.shared.segments.lock().unwrap();
            if segs.len() > 64 {
                segs.drain(..32);
            }
            segs.push(seg.clone());
        }
        self.current = Some(seg.clone());
        let mono = song.channels == 1;
        spawn_decoder(self.shared.clone(), Job { seg, gen, mono, key: opened.key });
        token
    }

    /// Make sure an output runs the right way for `song`.
    fn open_for(&mut self, song: &Song) -> Result<Opened, String> {
        if self.manual || null_backend() {
            return Ok(self.open_test_output(song));
        }
        if self.bit_perfect {
            match self.open_bit_perfect(song) {
                Ok(o) => return Ok(o),
                Err(bp) => return self.open_shared(song, Some(bp)),
            }
        }
        self.open_shared(song, None)
    }

    /// The test and null outputs: the song's rate (or the test device's), and with
    /// bit-perfect playback the format `bitperfect::plan()` picks from the test device.
    fn open_test_output(&mut self, song: &Song) -> Opened {
        // English, shown with `i18n::tr()`
        let name = if self.manual { n_!("Test output") } else { n_!("Null output (no sound)") };
        let normal = self.test_rate(song.rate);
        let dev = self.test_device.clone();
        let (rate, format, channels, plan, exclusive, bp) = if !self.bit_perfect {
            (normal, Format::F32, 2u32, None, false, None)
        } else if dev.busy {
            let plan = bitperfect::plan(song, &[Mode::new(Format::F32, normal, normal, 2)], bitperfect::BUSY);
            let bp = BpDevice { access: Some(Access::Direct), label: name.into(), failure: bitperfect::BUSY.into(), ..Default::default() };
            (normal, Format::F32, 2, Some(plan), false, Some(bp))
        } else {
            let modes: Vec<Mode> = if dev.rates.is_empty() && dev.formats.is_empty() {
                vec![]
            } else {
                let rates: Vec<u32> =
                    if dev.rates.is_empty() { bitperfect::STANDARD_RATES.to_vec() } else { dev.rates.clone() };
                let formats: Vec<Format> = if dev.formats.is_empty() { Format::ALL.to_vec() } else { dev.formats.clone() };
                formats.iter().flat_map(|f| rates.iter().map(move |r| Mode::new(*f, *r, *r, 2))).collect()
            };
            let plan = bitperfect::plan(song, &modes, "");
            let bp = BpDevice { access: Some(Access::Direct), label: name.into(), modes, ..Default::default() };
            (plan.rate, plan.format, plan.channels, Some(plan), true, Some(bp))
        };
        let rate = if rate == 0 { 48_000 } else { rate };
        let key: OutKey = (rate, format, channels as u16, exclusive);
        if self.output.key() != Some(key) || !matches!(self.output, Output::Null { .. } | Output::Manual { .. }) {
            let info = OutputInfo {
                device: name.into(),
                rate,
                channels: channels as u16,
                format: n_!("32-bit float").into(),
                resampled_from: None,
                soxr: has_soxr(),
                sample_format: format,
                bit_perfect: String::new(),
            };
            if let Output::Null { stop, .. } = &self.output {
                stop.store(true, Ordering::Relaxed);
            }
            self.output = if self.manual {
                Output::Manual { state: RenderState::new(&self.shared, rate), info, key, bp: bp.clone() }
            } else {
                let stop = Arc::new(AtomicBool::new(false));
                spawn_null(self.shared.clone(), rate, stop.clone());
                Output::Null { stop, info, key, bp: bp.clone() }
            };
        } else {
            // the same device keeps running: the reason (busy or not) is the latest one
            match &mut self.output {
                Output::Null { bp: b, .. } | Output::Manual { bp: b, .. } => *b = bp,
                _ => {}
            }
        }
        let native = plan.as_ref().is_some_and(|p| exclusive && p.rate == song.rate);
        Opened { rate, plan, native, key }
    }

    /// The device as the user chose it (or the system's default).
    fn cpal_device(&self) -> Option<cpal::Device> {
        let host = cpal::default_host();
        if self.device.is_empty() {
            host.default_output_device()
        } else {
            host.output_devices()
                .ok()
                .and_then(|mut it| it.find(|d| d.description().map(|x| x.name() == self.device).unwrap_or(false)))
                .or_else(|| host.default_output_device())
        }
    }

    /// The normal path: the song's rate when the device runs at it, else its
    /// default rate (FFmpeg resamples).  `bp`: bit-perfect playback could not
    /// have the device (gain 1 even so; the Playback info says why).
    fn open_shared(&mut self, song: &Song, bp: Option<BpDevice>) -> Result<Opened, String> {
        let song_rate = song.rate;
        let Some(device) = self.cpal_device() else {
            self.output = Output::None;
            return Err(tr!("No audio output device was found.").into());
        };
        let name = device.description().map(|d| d.name().to_string()).unwrap_or_else(|_| n_!("Output").into());
        let config = choose_config(&device, song_rate)?;
        let rate = config.sample_rate();
        let plan = bp.as_ref().map(|b| bitperfect::plan(song, &[Mode::new(Format::F32, rate, rate, 2)], &b.failure));
        if let Output::Cpal { info, device: dev, key, bp: old, .. } = &mut self.output {
            if info.rate == rate && *dev == name && !key.3 {
                *old = bp;
                let key = *key;
                return Ok(Opened { rate, plan, native: false, key });
            }
        }
        self.output = Output::None; // close the old stream first
        let format = config.sample_format();
        let channels = config.channels();
        let rates: Vec<(u32, u32)> = device
            .supported_output_configs()
            .map(|it| {
                it.filter(|r| matches!(r.sample_format(), cpal::SampleFormat::F32 | cpal::SampleFormat::I32 | cpal::SampleFormat::I16 | cpal::SampleFormat::U16))
                    .map(|r| (r.min_sample_rate(), r.max_sample_rate()))
                    .collect()
            })
            .unwrap_or_default();
        let default_rate = device.default_output_config().map(|c| c.sample_rate()).unwrap_or(rate);
        let stream = build_stream(&device, &config.config(), format, self.shared.clone())?;
        stream.play().map_err(|e| tr!("The audio output could not start: {e}.", e = e))?;
        let info = OutputInfo {
            device: name.clone(),
            rate,
            channels,
            format: format_label(format),
            resampled_from: None,
            soxr: has_soxr(),
            sample_format: format_of(format).unwrap_or(Format::F32),
            bit_perfect: String::new(),
        };
        let key: OutKey = (rate, format_of(format).unwrap_or(Format::F32), channels, false);
        self.output = Output::Cpal { _stream: stream, info, device: name, rates, default_rate, key, bp };
        Ok(Opened { rate, plan, native: false, key })
    }

    /// Bit-perfect playback: the device for this app alone, at the song's rate
    /// in the best format it has.  Err: why it could not be had (the normal
    /// path then plays the song).
    fn open_bit_perfect(&mut self, song: &Song) -> Result<Opened, BpDevice> {
        // the device open now for this app alone: plan from what it offered (probing it would find it busy)
        let open_bp = match (&self.output, self.output.key()) {
            (o, Some(k)) if k.3 => o.bp().filter(|b| b.chosen == self.device).cloned(),
            (o, Some(_)) => {
                // it could not be had a moment ago: the normal path again, without closing it to ask
                if let Some(b) = o.bp().filter(|b| {
                    b.chosen == self.device && !b.failure.is_empty() && b.probed.is_some_and(|t| t.elapsed() < REPROBE)
                }) {
                    return Err(b.clone());
                }
                None
            }
            _ => None,
        };
        let found = match open_bp {
            Some(b) => b,
            None => {
                self.output = Output::None; // close the device first, or it is busy for the probe
                self.probe_bit_perfect()?
            }
        };
        let plan = bitperfect::plan(song, &found.modes, "");
        let key: OutKey = (plan.rate, plan.format, plan.channels as u16, true);
        let native = plan.rate == song.rate;
        if self.output.key() == Some(key) {
            return Ok(Opened { rate: plan.rate, plan: Some(plan), native, key });
        }
        self.output = Output::None;
        self.open_exclusive(&found, &plan).map_err(|failure| BpDevice { failure, ..found.clone() })?;
        Ok(Opened { rate: plan.rate, plan: Some(plan), native, key })
    }

    /// What the device offers for this app alone (Windows: WASAPI exclusive
    /// mode; Linux: the ALSA hardware device; macOS: the device's rates).
    fn probe_bit_perfect(&self) -> Result<BpDevice, BpDevice> {
        let stamp = |mut b: BpDevice| {
            b.probed = Some(Instant::now());
            b
        };
        self.probe_device().map(stamp).map_err(stamp)
    }

    fn probe_device(&self) -> Result<BpDevice, BpDevice> {
        let chosen = self.device.clone();
        #[cfg(windows)]
        {
            match crate::audio_wasapi::probe(&chosen) {
                Ok((label, modes)) => Ok(BpDevice { chosen, access: Some(Access::Wasapi), label, modes, ..Default::default() }),
                Err(e) => Err(BpDevice { chosen, access: Some(Access::Wasapi), label: e.device.clone(), failure: e.reason(), ..Default::default() }),
            }
        }
        #[cfg(target_os = "linux")]
        {
            let (hw, device) = self.alsa_hw_device();
            let fail = |failure: String| BpDevice { chosen: chosen.clone(), access: Some(Access::Alsa), label: hw.clone(), failure, ..Default::default() };
            let Some(device) = device else {
                // the details are English, as the core's sentence around them; shown with `tr_text()`
                return Err(fail(bitperfect::failure_reason(&tr!("the ALSA device {hw} was not found", hw = &hw))));
            };
            let modes = supported_modes(&device).map_err(|e| fail(cpal_failure(&e)))?;
            if modes.is_empty() {
                return Err(fail(bitperfect::failure_reason(n_!("it takes no stereo format this player writes"))));
            }
            Ok(BpDevice { chosen: chosen.clone(), access: Some(Access::Alsa), label: hw.clone(), modes, ..Default::default() })
        }
        #[cfg(target_os = "macos")]
        {
            let fail = |failure: String| BpDevice { chosen: chosen.clone(), access: Some(Access::CoreAudio), failure, ..Default::default() };
            let Some(device) = self.cpal_device() else {
                return Err(fail(bitperfect::failure_reason(n_!("no output device was found"))));
            };
            let label = device.description().map(|d| d.name().to_string()).unwrap_or_else(|_| n_!("Output").into());
            let modes = supported_modes(&device).map_err(|e| fail(cpal_failure(&e)))?;
            if modes.is_empty() {
                return Err(fail(bitperfect::failure_reason(n_!("CoreAudio lists no sample rate for it"))));
            }
            Ok(BpDevice { chosen: chosen.clone(), access: Some(Access::CoreAudio), label, modes, ..Default::default() })
        }
        #[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
        {
            Err(BpDevice { chosen, failure: bitperfect::failure_reason(n_!("this system has no way to do that")), ..Default::default() })
        }
    }

    /// Linux: the ALSA hardware device of the chosen output's card (or card 0),
    /// as "hw:1", and cpal's device for it (cpal lists it as "hw:CARD=…,DEV=…").
    #[cfg(target_os = "linux")]
    fn alsa_hw_device(&self) -> (String, Option<cpal::Device>) {
        let cards = std::fs::read_to_string("/proc/asound/cards").map(|t| bitperfect::alsa_cards(&t)).unwrap_or_default();
        let host = cpal::default_host();
        // the chosen device's ALSA name ("front:CARD=PCH,DEV=0"), or "default"
        let chosen = if self.device.is_empty() {
            "default".to_string()
        } else {
            host.output_devices()
                .ok()
                .and_then(|mut it| it.find(|d| d.description().map(|x| x.name() == self.device).unwrap_or(false)))
                .and_then(|d| d.id().ok().map(|i| i.id().to_string()))
                .unwrap_or_else(|| self.device.clone())
        };
        let hw = bitperfect::alsa_device(&chosen, &cards);
        let device = host.output_devices().ok().and_then(|mut it| {
            it.find(|d| {
                d.id().map(|i| i.id().starts_with("hw:") && bitperfect::alsa_device(i.id(), &cards) == hw).unwrap_or(false)
            })
        });
        (hw, device)
    }

    /// Open the device found by `probe_bit_perfect` as `plan` says.  Err: the reason.
    fn open_exclusive(&mut self, found: &BpDevice, plan: &Plan) -> Result<(), String> {
        let key: OutKey = (plan.rate, plan.format, plan.channels as u16, true);
        let info = OutputInfo {
            device: found.label.clone(),
            rate: plan.rate,
            channels: plan.channels as u16,
            format: bitperfect::format_label(plan.format, 0),
            resampled_from: None,
            soxr: has_soxr(),
            sample_format: plan.format,
            bit_perfect: String::new(),
        };
        #[cfg(windows)]
        {
            let out = crate::audio_wasapi::open(&found.chosen, plan.rate, plan.format, plan.channels, self.shared.clone())
                .map_err(|e| e.reason())?;
            self.output = Output::Wasapi { _out: out, info, key, bp: found.clone() };
            Ok(())
        }
        #[cfg(not(windows))]
        {
            #[cfg(target_os = "linux")]
            let device = self.alsa_hw_device().1;
            #[cfg(not(target_os = "linux"))]
            let device = self.cpal_device();
            let Some(device) = device else {
                return Err(bitperfect::failure_reason(n_!("the device is gone")));
            };
            let config = cpal::StreamConfig {
                channels: plan.channels as u16,
                sample_rate: plan.rate,
                buffer_size: cpal::BufferSize::Default,
            };
            let stream = build_exact_stream(&device, &config, plan.format, self.shared.clone()).map_err(|e| cpal_failure(&e))?;
            stream.play().map_err(|e| bitperfect::failure_reason(&e.to_string()))?;
            let rates = vec![(plan.rate, plan.rate)];
            self.output = Output::Cpal {
                _stream: stream,
                info,
                device: found.label.clone(),
                rates,
                default_rate: plan.rate,
                key,
                bp: Some(found.clone()),
            };
            Ok(())
        }
    }

    pub fn pause(&self) {
        self.shared.paused.store(true, Ordering::Relaxed);
    }

    pub fn resume(&self) {
        self.shared.paused.store(false, Ordering::Relaxed);
    }

    pub fn is_paused(&self) -> bool {
        self.shared.paused.load(Ordering::Relaxed)
    }

    /// Paused at `start` without a sound: for the song remembered from last time.
    pub fn cue(&mut self, path: &str, start: f64, song: Song) -> u64 {
        self.start(path, start, song, None, true)
    }

    pub fn seek(&mut self, seconds: f64) {
        if let Some(cur) = self.heard_segment().or_else(|| self.current.clone()) {
            let paused = self.is_paused();
            self.start(&cur.path, seconds.max(0.0), cur.song, Some(cur.token), paused);
        }
    }

    pub fn stop(&mut self) {
        let mut buf = self.shared.buf.lock().unwrap();
        buf.gen += 1;
        buf.items.clear();
        buf.frames = 0;
        drop(buf);
        *self.shared.next.lock().unwrap() = None;
        self.shared.heard_seg.store(0, Ordering::Relaxed);
        self.current = None;
    }

    fn heard_segment(&self) -> Option<Segment> {
        let id = self.shared.heard_seg.load(Ordering::Relaxed);
        if id == 0 {
            None
        } else {
            self.shared.segment(id)
        }
    }

    /// The token and path of the song being heard (or about to be).
    pub fn now(&self) -> Option<(u64, String)> {
        self.heard_segment().or_else(|| self.current.clone()).map(|s| (s.token, s.path))
    }

    /// Seconds into the song being heard.
    pub fn position(&self) -> Option<f64> {
        match self.heard_segment() {
            Some(s) if s.rate > 0 => {
                Some(s.start + self.shared.heard_frames.load(Ordering::Relaxed) as f64 / s.rate as f64)
            }
            _ => self.current.as_ref().map(|s| s.start),
        }
    }

    pub fn take_events(&self) -> Vec<Event> {
        std::mem::take(&mut *self.shared.events.lock().unwrap())
    }

    /// Whether `frames` frames are buffered, or the decoder has reached the end.
    pub fn ready(&self, frames: usize) -> bool {
        let buf = self.shared.buf.lock().unwrap();
        buf.frames >= frames || matches!(buf.items.back(), Some(Item::Finish(_, false)) | Some(Item::Fail(..)))
    }

    /// Seconds of audio decoded but not heard yet.
    pub fn buffered(&self) -> f64 {
        let frames = self.shared.buf.lock().unwrap().frames;
        let rate = self.info().map(|i| i.rate).unwrap_or(48_000).max(1);
        frames as f64 / rate as f64
    }

    /// Tests: take `frames` stereo frames as a sound device would.
    pub fn pull(&mut self, frames: usize) -> Vec<f32> {
        let mut out = vec![0.0f32; frames * CHANNELS];
        if let Output::Manual { state, .. } = &mut self.output {
            render(&self.shared, state, &mut out, CHANNELS);
        }
        out
    }

    /// Tests: take `frames` frames as the device's integers (the open output's
    /// format, which must be an integer one), as a cpal or WASAPI stream writes them.
    pub fn pull_ints(&mut self, frames: usize) -> Vec<i32> {
        let format = self.output.key().map(|k| k.1).unwrap_or(Format::F32);
        assert!(format != Format::F32, "the test output takes floats");
        let mut out = vec![0i32; frames * CHANNELS];
        if let Output::Manual { state, .. } = &mut self.output {
            render_ints(&self.shared, state, &mut out, CHANNELS, format);
        }
        out
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        self.stop();
        if let Output::Null { stop, .. } = &self.output {
            stop.store(true, Ordering::Relaxed);
        }
    }
}

fn spawn_null(shared: Arc<Shared>, rate: u32, stop: Arc<AtomicBool>) {
    std::thread::Builder::new()
        .name("player-null-output".into())
        .spawn(move || {
            let mut st = RenderState::new(&shared, rate);
            let block = 1024usize;
            let mut out = vec![0.0f32; block * CHANNELS];
            let start = Instant::now();
            let mut done: u64 = 0;
            while !stop.load(Ordering::Relaxed) {
                render(&shared, &mut st, &mut out, CHANNELS);
                done += block as u64;
                let due = start + Duration::from_secs_f64(done as f64 / rate as f64);
                if let Some(wait) = due.checked_duration_since(Instant::now()) {
                    std::thread::sleep(wait);
                }
            }
        })
        .expect("null output thread");
}

/// What a cpal device offers bit-perfect playback: every format the engine
/// writes, with its channels and rates.
#[cfg(not(windows))]
fn supported_modes(device: &cpal::Device) -> Result<Vec<Mode>, cpal::Error> {
    Ok(device
        .supported_output_configs()?
        .filter_map(|r| {
            format_of(r.sample_format()).map(|f| Mode::new(f, r.min_sample_rate(), r.max_sample_rate(), r.channels() as u32))
        })
        .collect())
}

/// The song's own rate when the device can run at it (stereo preferred, float
/// preferred), else the device's default configuration.
fn choose_config(device: &cpal::Device, song_rate: u32) -> Result<cpal::SupportedStreamConfig, String> {
    use cpal::SampleFormat as F;
    let rank = |f: F| match f {
        F::F32 => 0,
        F::I32 => 1,
        F::I24 => 2,
        F::I16 => 3,
        _ => 9,
    };
    let usable = |f: F| matches!(f, F::F32 | F::I32 | F::I16 | F::U16);
    if song_rate > 0 {
        if let Ok(ranges) = device.supported_output_configs() {
            let mut best: Option<(i32, cpal::SupportedStreamConfigRange)> = None;
            for r in ranges {
                if !usable(r.sample_format()) || r.min_sample_rate() > song_rate || r.max_sample_rate() < song_rate {
                    continue;
                }
                let score = rank(r.sample_format()) * 10 + if r.channels() == 2 { 0 } else { 5 };
                if best.as_ref().is_none_or(|(s, _)| score < *s) {
                    best = Some((score, r));
                }
            }
            if let Some((_, r)) = best {
                return Ok(r.with_sample_rate(song_rate));
            }
        }
    }
    let def = device.default_output_config().map_err(|e| tr!("The audio device has no usable format: {e}.", e = e))?;
    if usable(def.sample_format()) {
        return Ok(def);
    }
    // a default in a format we do not write: the same rate in one we do
    let rate = def.sample_rate();
    device
        .supported_output_configs()
        .ok()
        .and_then(|it| {
            it.filter(|r| usable(r.sample_format()) && r.min_sample_rate() <= rate && r.max_sample_rate() >= rate)
                .min_by_key(|r| rank(r.sample_format()))
        })
        .map(|r| r.with_sample_rate(rate))
        .ok_or_else(|| tr!("The audio device supports no sample format this player can write.").to_string())
}

fn build_stream(
    device: &cpal::Device,
    config: &cpal::StreamConfig,
    format: cpal::SampleFormat,
    shared: Arc<Shared>,
) -> Result<cpal::Stream, String> {
    use cpal::SampleFormat as F;
    let err = |e: cpal::Error| tr!("The audio output could not be opened: {e}.", e = e);
    match format {
        F::F32 => stream_of::<f32>(device, config, shared, |x| x).map_err(err),
        F::I32 => stream_of::<i32>(device, config, shared, <i32 as cpal::FromSample<f32>>::from_sample_).map_err(err),
        F::I16 => stream_of::<i16>(device, config, shared, <i16 as cpal::FromSample<f32>>::from_sample_).map_err(err),
        F::U16 => stream_of::<u16>(device, config, shared, <u16 as cpal::FromSample<f32>>::from_sample_).map_err(err),
        other => Err(tr!("The audio device wants {format} samples, which this player cannot write.", format = format!("{other:?}"))),
    }
}

/// A stream that writes the floats as the device's integers exactly
/// (`render_ints()`: round(x * 2^(bits - 1)), clamped; with the equalizer on, dithered).
#[cfg(not(windows))]
fn build_exact_stream(
    device: &cpal::Device,
    config: &cpal::StreamConfig,
    format: Format,
    shared: Arc<Shared>,
) -> Result<cpal::Stream, cpal::Error> {
    match format {
        Format::F32 => stream_of::<f32>(device, config, shared, |x| x),
        Format::I16 => int_stream_of::<i16>(device, config, shared, Format::I16, |x| x as i16),
        Format::I32 => int_stream_of::<i32>(device, config, shared, Format::I32, |x| x),
        Format::I24 => int_stream_of::<cpal::I24>(device, config, shared, Format::I24, cpal::I24::new_unchecked),
    }
}

/// A stream of the device's integers (`render_ints()` in `format`, then `convert`ed
/// to cpal's sample type).
#[cfg(not(windows))]
fn int_stream_of<T>(
    device: &cpal::Device,
    config: &cpal::StreamConfig,
    shared: Arc<Shared>,
    format: Format,
    convert: fn(i32) -> T,
) -> Result<cpal::Stream, cpal::Error>
where
    T: cpal::SizedSample + Send + 'static,
{
    let channels = config.channels as usize;
    let mut st = RenderState::new(&shared, config.sample_rate);
    let mut tmp: Vec<i32> = vec![];
    let errs = shared.clone();
    device.build_output_stream::<T, _, _>(
        config.clone(),
        move |data: &mut [T], _| {
            tmp.resize(data.len(), 0);
            render_ints(&shared, &mut st, &mut tmp, channels, format);
            for (d, s) in data.iter_mut().zip(tmp.iter()) {
                *d = convert(*s);
            }
        },
        move |e| {
            errs.events.lock().unwrap().push(Event::OutputError(tr!("The audio output failed: {e}.", e = e)));
        },
        None,
    )
}

fn stream_of<T>(
    device: &cpal::Device,
    config: &cpal::StreamConfig,
    shared: Arc<Shared>,
    convert: fn(f32) -> T,
) -> Result<cpal::Stream, cpal::Error>
where
    T: cpal::SizedSample + Send + 'static,
{
    let channels = config.channels as usize;
    let mut st = RenderState::new(&shared, config.sample_rate);
    let mut tmp: Vec<f32> = vec![];
    let errs = shared.clone();
    device.build_output_stream::<T, _, _>(
        config.clone(),
        move |data: &mut [T], _| {
            tmp.resize(data.len(), 0.0);
            render(&shared, &mut st, &mut tmp, channels);
            for (d, s) in data.iter_mut().zip(tmp.iter()) {
                *d = convert(*s);
            }
        },
        move |e| {
            errs.events.lock().unwrap().push(Event::OutputError(tr!("The audio output failed: {e}.", e = e)));
        },
        None,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(dir: &std::path::Path, name: &str, seconds: f64, rate: u32, extra: &[&str]) -> String {
        let p = dir.join(name);
        let st = command(tool("ffmpeg"))
            .args(["-v", "error", "-y", "-f", "lavfi", "-i"])
            .arg(format!("sine=frequency=440:sample_rate={rate}:duration={seconds}"))
            .args(extra)
            .arg(&p)
            .status()
            .unwrap();
        assert!(st.success());
        p.to_string_lossy().into_owned()
    }

    fn rate(r: u32) -> Song {
        Song { rate: r, channels: 1, bits: 16, ..Default::default() }
    }

    fn pull_until(e: &mut Engine, frames: usize, want: impl Fn(&[Event]) -> bool) -> (Vec<f32>, Vec<Event>) {
        let mut all = vec![];
        let mut events = vec![];
        let t0 = Instant::now();
        while t0.elapsed() < Duration::from_secs(20) {
            while !e.ready(frames) && t0.elapsed() < Duration::from_secs(20) {
                std::thread::sleep(Duration::from_millis(2));
            }
            all.extend(e.pull(frames));
            events.extend(e.take_events());
            if want(&events) {
                break;
            }
        }
        (all, events)
    }

    #[test]
    fn plays_seeks_chains_gaplessly_and_reports_failures() {
        let dir = std::env::temp_dir().join(format!("rhr-audio-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let a = tone(&dir, "a.flac", 1.0, 44_100, &[]);
        let b = tone(&dir, "b.flac", 0.5, 44_100, &[]);
        let mut e = Engine::manual();
        let ta = e.play(&a, 0.0, rate(44_100));
        assert_eq!(e.info().unwrap().rate, 44_100);
        e.set_next(Some((&b, rate(44_100))));
        let (samples, events) = pull_until(&mut e, 1000, |ev| ev.iter().any(|x| matches!(x, Event::Ended { path, .. } if *path == b)));
        assert!(matches!(&events[0], Event::Started { token, path } if *token == ta && *path == a), "{events:?}");
        assert!(matches!(&events[1], Event::Ended { path, chained: true, .. } if *path == a), "{events:?}");
        assert!(matches!(&events[2], Event::Started { path, .. } if *path == b), "{events:?}");
        assert!(matches!(&events[3], Event::Ended { path, chained: false, .. } if *path == b), "{events:?}");
        // one continuous sine: 1.5 s of samples with no silent run between the songs
        let left: Vec<f32> = samples.iter().step_by(2).copied().collect();
        let first = left.iter().position(|x| x.abs() > 1e-4).unwrap();
        let last = left.iter().rposition(|x| x.abs() > 1e-4).unwrap();
        let run = left[first..=last].windows(8).filter(|w| w.iter().all(|x| x.abs() < 1e-6)).count();
        assert_eq!(run, 0, "silence between gapless songs");
        assert!(((last - first) as f64 / 44_100.0 - 1.5).abs() < 0.01, "{}", (last - first) as f64 / 44_100.0);

        // volume and EQ apply to what is pulled
        e.play(&a, 0.0, rate(44_100));
        let (loud, _) = pull_until(&mut e, 4096, |_| true);
        e.set_gain(0.25);
        let (quiet, _) = pull_until(&mut e, 4096, |_| true);
        let peak = |v: &[f32]| v.iter().fold(0f32, |m, x| m.max(x.abs()));
        assert!((peak(&quiet) / peak(&loud) - 0.25).abs() < 0.02, "{} {}", peak(&quiet), peak(&loud));
        e.set_gain(1.0);
        let mut eq = EqState::default();
        eq.enabled = true;
        eq.prevent_clipping = false;
        eq.graphic_enabled = true;
        eq.gains = [-12.0; 10];
        e.set_eq(&eq);
        let _ = e.pull(8192);
        let (cut, _) = pull_until(&mut e, 4096, |_| true);
        assert!(peak(&cut) < 0.5 * peak(&loud), "{} {}", peak(&cut), peak(&loud));
        e.set_eq(&EqState::default());

        // seek: the position follows, no new Started event
        e.seek(0.5);
        let (_, ev) = pull_until(&mut e, 2205, |_| true);
        assert!(ev.iter().all(|x| !matches!(x, Event::Started { .. })), "{ev:?}");
        let pos = e.position().unwrap();
        assert!((0.5..0.6).contains(&pos), "{pos}");
        // pause: nothing is taken
        e.pause();
        let before = e.position().unwrap();
        let silent = e.pull(4410);
        assert!(silent.iter().all(|x| *x == 0.0));
        assert_eq!(e.position().unwrap(), before);
        e.resume();

        // a file FFmpeg cannot read
        let bad = dir.join("bad.flac");
        std::fs::write(&bad, b"not audio at all").unwrap();
        let tb = e.play(&bad.to_string_lossy(), 0.0, rate(44_100));
        let (_, ev) = pull_until(&mut e, 1000, |ev| ev.iter().any(|x| matches!(x, Event::Failed { .. })));
        assert!(
            ev.iter().any(|x| matches!(x, Event::Failed { token, message, .. } if *token == tb && message.contains("FFmpeg"))),
            "{ev:?}"
        );

        // a different rate is resampled for a fixed output (the null and test outputs follow the song)
        let args = decode_args(&a, 1.5, 96_000, 48_000, true, false);
        let joined = args.join(" ");
        assert!(joined.contains("-ss 1.500") && joined.contains("aresample=48000") && joined.contains("pan=stereo"), "{joined}");
        assert!(!decode_args(&a, 0.0, 48_000, 48_000, false, false).join(" ").contains("-af"));
        // bit-perfect: no -ar and no filters but the mono copy
        let native = decode_args(&a, 0.0, 96_000, 96_000, false, true).join(" ");
        assert!(!native.contains("-ar") && !native.contains("-af"), "{native}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A FLAC of noise at -6 dBFS with `bits` bits (16 or 24).
    fn noise(dir: &std::path::Path, name: &str, seconds: f64, rate: u32, bits: u32, channels: u32) -> String {
        let p = dir.join(name);
        let fmt: &[&str] = if bits == 16 { &["-sample_fmt", "s16"] } else { &["-sample_fmt", "s32", "-bits_per_raw_sample", "24"] };
        let st = command(tool("ffmpeg"))
            .args(["-v", "error", "-y", "-f", "lavfi", "-i"])
            .arg(format!("anoisesrc=r={rate}:a=0.5:d={seconds}:seed={}", bits + channels))
            .args(["-ac", &channels.to_string(), "-c:a", "flac"])
            .args(fmt)
            .arg(&p)
            .status()
            .unwrap();
        assert!(st.success());
        p.to_string_lossy().into_owned()
    }

    /// What FFmpeg decodes a file to as integers (s16le, or s32le with 24-bit files left-justified).
    fn ffmpeg_ints(path: &str, bits: u32) -> Vec<i32> {
        let fmt = if bits == 16 { "s16le" } else { "s32le" };
        let out = command(tool("ffmpeg")).args(["-v", "error", "-i", path, "-f", fmt, "-"]).output().unwrap();
        if bits == 16 {
            out.stdout.chunks_exact(2).map(|b| i16::from_le_bytes([b[0], b[1]]) as i32).collect()
        } else {
            out.stdout.chunks_exact(4).map(|b| i32::from_le_bytes([b[0], b[1], b[2], b[3]])).collect()
        }
    }

    fn pull_ints_until_end(e: &mut Engine, frames: usize) -> (Vec<i32>, Vec<Event>) {
        let mut all = vec![];
        let mut events = vec![];
        let t0 = Instant::now();
        while t0.elapsed() < Duration::from_secs(30) {
            while !e.ready(frames) && t0.elapsed() < Duration::from_secs(30) {
                std::thread::sleep(Duration::from_millis(2));
            }
            all.extend(e.pull_ints(frames));
            events.extend(e.take_events());
            if events.iter().any(|x| matches!(x, Event::Ended { chained: false, .. } | Event::Failed { .. })) {
                break;
            }
        }
        (all, events)
    }

    #[test]
    fn bit_perfect_flacs_reach_the_device_as_their_integers() {
        let dir = std::env::temp_dir().join(format!("rhr-bitperfect-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        for (bits, rate, channels, format) in
            [(16, 44_100, 2, Format::I16), (24, 96_000, 2, Format::I32), (16, 48_000, 1, Format::I16)]
        {
            let path = noise(&dir, &format!("n{bits}_{channels}.flac"), 0.5, rate, bits, channels);
            let mut e = Engine::manual();
            e.set_gain(0.25); // neither the volume (50 %) ...
            let mut eq = EqState::default();
            eq.enabled = false;
            eq.graphic_enabled = true;
            eq.apply_preset("Rock");
            e.set_eq(&eq); // ... nor the equalizer's settings while it is off may touch the samples
            e.set_bit_perfect(true);
            assert_eq!(e.gain(), 1.0);
            e.play(&path, 0.0, song_of_file(&path));
            let info = e.info().unwrap();
            assert_eq!((info.rate, info.sample_format), (rate, format), "{path}");
            let (got, events) = pull_ints_until_end(&mut e, 999);
            assert!(events.iter().any(|x| matches!(x, Event::Ended { chained: false, .. })), "{events:?}");
            let want = ffmpeg_ints(&path, bits);
            let frames = want.len() / channels as usize;
            if channels == 1 {
                let left: Vec<i32> = got.iter().step_by(2).copied().take(frames).collect();
                let right: Vec<i32> = got.iter().skip(1).step_by(2).copied().take(frames).collect();
                assert_eq!(left, want, "mono left");
                assert_eq!(right, want, "mono right");
            } else {
                assert_eq!(&got[..want.len()], &want[..], "{path}: the samples were changed on the way");
            }
            e.play(&path, 0.0, song_of_file(&path));
            let _ = e.pull(500);
            let khz = if rate == 44_100 { "44.1" } else if rate == 48_000 { "48" } else { "96" };
            assert_eq!(e.info().unwrap().bit_perfect, format!("Yes: {bits}-bit, {khz} kHz, Test output"));
            assert_eq!(
                e.info().unwrap().format,
                if bits == 16 { "16-bit integer" } else { "24-bit integer in 32-bit" }
            );
            e.set_bit_perfect(false);
            assert_eq!(e.gain(), 0.25);
            assert_eq!(e.info().unwrap().bit_perfect, "Off");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// What FFmpeg decodes a file to as floats (what the engine's decoder gets).
    fn ffmpeg_floats(path: &str) -> Vec<f32> {
        let out = command(tool("ffmpeg")).args(["-v", "error", "-i", path, "-f", "f32le", "-ac", "2", "-"]).output().unwrap();
        out.stdout.chunks_exact(4).map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]])).collect()
    }

    #[test]
    fn bit_perfect_with_the_equalizer_on_applies_it_with_dither() {
        // 2.3: the equalizer is no longer bypassed: the device keeps the song's rate and its
        // integer format, the gain stays 1, and the changed samples get TPDF dither of ±1 LSB
        let dir = std::env::temp_dir().join(format!("rhr-bitperfect-eq-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        for (bits, rate, format) in [(16, 44_100, Format::I16), (24, 96_000, Format::I32)] {
            let path = noise(&dir, &format!("e{bits}.flac"), 0.6, rate, bits, 2);
            let mut e = Engine::manual();
            e.set_gain(0.25);
            let mut eq = EqState::default();
            eq.enabled = true;
            eq.graphic_enabled = true;
            eq.apply_preset("Rock");
            e.set_eq(&eq);
            e.set_bit_perfect(true);
            assert_eq!(e.gain(), 1.0);
            e.play(&path, 0.0, song_of_file(&path));
            let info = e.info().unwrap();
            assert_eq!((info.rate, info.sample_format), (rate, format), "{path}");
            let (got, events) = pull_ints_until_end(&mut e, 999);
            assert!(events.iter().any(|x| matches!(x, Event::Ended { chained: false, .. })), "{events:?}");
            let want = ffmpeg_ints(&path, bits);
            assert!(got.len() >= want.len());
            let got = &got[..want.len()];
            assert_ne!(got, &want[..], "the equalizer did nothing");
            // the reference: the equalizer in 64-bit float on the decoded floats
            let mut wide: Vec<f64> = ffmpeg_floats(&path).into_iter().map(|x| x as f64).collect();
            assert_eq!(wide.len(), want.len());
            EqProcessor::new(&eq, rate as f64, CHANNELS).process_f64(&mut wide);
            let top = (1i64 << (format.container_bits() - 1)) as f64;
            let (mut n, mut worst, mut sum, mut moved) = (0usize, 0f64, 0f64, 0usize);
            for (g, r) in got.iter().zip(&wide) {
                let r = r * top;
                if r.abs() >= top - 2.0 {
                    continue; // clamped
                }
                let d = *g as f64 - r;
                worst = worst.max(d.abs());
                sum += d;
                n += 1;
                if *g as f64 != r.round_ties_even() {
                    moved += 1;
                }
            }
            assert!(n as f64 > 0.99 * got.len() as f64);
            assert!(worst < 1.5, "{path}: more than the dither and rounding: {worst}");
            if format == Format::I16 {
                let share = moved as f64 / n as f64;
                assert!((0.2..0.7).contains(&share), "TPDF moves about half the samples: {share}");
                assert!((sum / n as f64).abs() < 0.05, "the dither is not centred");
            }
            // the Playback info says why, and what still holds
            e.play(&path, 0.0, song_of_file(&path));
            let t0 = Instant::now();
            while !e.ready(2500) && t0.elapsed() < Duration::from_secs(20) {
                std::thread::sleep(Duration::from_millis(2));
            }
            let _ = e.pull_ints(500);
            let khz = if rate == 44_100 { "44.1" } else { "96" };
            assert_eq!(
                e.info().unwrap().bit_perfect,
                format!("No: The equalizer is on, so the samples are changed. The device still runs at {khz} kHz.")
            );
            // the equalizer off while playing: the samples are exact again
            e.set_eq(&EqState::default());
            let more = e.pull_ints(2000);
            assert_eq!(&more[..], &want[1000..5000], "not bit-exact with the equalizer off");
            assert!(e.info().unwrap().bit_perfect.starts_with("Yes: "));
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn bit_perfect_resamples_reports_busy_devices_and_chains_only_alike_songs() {
        let dir = std::env::temp_dir().join(format!("rhr-bitperfect2-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        // a rate the device does not have: resampled, and the Playback info says so
        let hi = noise(&dir, "hi.flac", 0.4, 96_000, 24, 2);
        let mut e = Engine::manual_device(TestDevice { rates: vec![44_100, 48_000], ..Default::default() });
        e.set_bit_perfect(true);
        e.play(&hi, 0.0, song_of_file(&hi));
        let _ = e.pull(500);
        let info = e.info().unwrap();
        assert_eq!(info.rate, 48_000);
        assert_eq!(info.bit_perfect, "No: The device cannot play 96 kHz, so the song is resampled to 48 kHz.");
        let args = decode_args(&hi, 0.0, 96_000, 48_000, false, false).join(" ");
        assert!(args.contains("aresample=48000"), "{args}");

        // a busy device: the normal path, gain 1 and no EQ even so
        let cd = noise(&dir, "cd.flac", 0.3, 44_100, 16, 2);
        let mut e = Engine::manual_device(TestDevice { busy: true, ..Default::default() });
        e.set_gain(0.25);
        e.set_bit_perfect(true);
        e.play(&cd, 0.0, song_of_file(&cd));
        let (got, _) = pull_until(&mut e, 1000, |_| true);
        let info = e.info().unwrap();
        assert_eq!(info.sample_format, Format::F32);
        assert_eq!(info.bit_perfect, format!("No: {}", bitperfect::BUSY));
        let want = ffmpeg_ints(&cd, 16);
        let back: Vec<i32> = got.iter().take(2000).map(|x| bitperfect::int_sample_f32(*x, Format::I16)).collect();
        assert_eq!(back, want[..2000].to_vec(), "gain 1 on the normal path too");

        // gapless only between songs of the same rate and format
        let a = noise(&dir, "a.flac", 0.3, 44_100, 16, 2);
        let b = noise(&dir, "b.flac", 0.2, 44_100, 16, 1);
        let c = noise(&dir, "c.flac", 0.2, 44_100, 24, 2);
        // probed first: probing b between play() and set_next() took longer on a busy CI runner
        // than decoding all 0.3 s of a, which then ended before it had a next song to join
        let (sa, sb, sc) = (song_of_file(&a), song_of_file(&b), song_of_file(&c));
        let mut e = Engine::manual();
        e.set_bit_perfect(true);
        e.play(&a, 0.0, sa);
        e.set_next(Some((&b, sb)));
        let (_, ev) = pull_until(&mut e, 700, |ev| ev.iter().any(|x| matches!(x, Event::Ended { chained: false, .. })));
        let chained: Vec<bool> = ev.iter().filter_map(|x| if let Event::Ended { chained, .. } = x { Some(*chained) } else { None }).collect();
        assert_eq!(chained, vec![true, false], "a and b (16-bit, 44.1 kHz) join: {ev:?}");
        e.play(&a, 0.0, sa);
        e.set_next(Some((&c, sc)));
        let (_, ev) = pull_until(&mut e, 700, |ev| ev.iter().any(|x| matches!(x, Event::Ended { .. })));
        assert!(
            ev.iter().any(|x| matches!(x, Event::Ended { path, chained: false, .. } if *path == a)),
            "c needs a 32-bit format, so a ends on its own: {ev:?}"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
