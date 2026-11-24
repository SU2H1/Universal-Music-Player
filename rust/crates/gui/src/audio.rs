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

use std::collections::VecDeque;
use std::io::{BufRead, Read};
use std::process::{Child, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
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
    pub format: String,
    /// The song's rate when FFmpeg converts it to `rate`.
    pub resampled_from: Option<u32>,
    pub soxr: bool,
}

enum Item {
    Begin(u64),
    Audio(Vec<f32>, usize),
    Finish(u64, bool),
    Fail(u64, String),
}

#[derive(Default)]
struct Buffer {
    items: VecDeque<Item>,
    frames: usize,
    gen: u64,
}

#[derive(Clone)]
struct Segment {
    id: u64,
    token: u64,
    path: String,
    start: f64,
    rate: u32,
    song_rate: u32,
    announce: bool,
}

#[derive(Clone)]
struct Next {
    path: String,
    song_rate: u32,
    /// The output rate this song would get on its own.
    out_rate: u32,
}

struct Shared {
    buf: Mutex<Buffer>,
    paused: AtomicBool,
    gain_bits: AtomicU32,
    eq: Mutex<(u64, EqState)>,
    heard_seg: AtomicU64,
    heard_frames: AtomicU64,
    segments: Mutex<Vec<Segment>>,
    events: Mutex<Vec<Event>>,
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
struct RenderState {
    eq: EqProcessor,
    eq_version: u64,
    scratch: Vec<f32>,
}

impl RenderState {
    fn new(shared: &Shared, rate: u32) -> RenderState {
        let (version, state) = shared.eq.lock().unwrap().clone();
        RenderState { eq: EqProcessor::new(&state, rate as f64, CHANNELS), eq_version: version, scratch: vec![] }
    }
}

/// Fill `out` (interleaved, `dev_channels` per frame) with what is due.
fn render(shared: &Shared, st: &mut RenderState, out: &mut [f32], dev_channels: usize) {
    let dev_channels = dev_channels.max(1);
    let frames = out.len() / dev_channels;
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
    if let Ok(eq) = shared.eq.try_lock() {
        if eq.0 != st.eq_version {
            st.eq.set(&eq.1);
            st.eq_version = eq.0;
        }
    }
    st.eq.process(&mut st.scratch);
    let gain = f32::from_bits(shared.gain_bits.load(Ordering::Relaxed));
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

/// The FFmpeg arguments that decode `path` from `start` seconds for the engine.
pub fn decode_args(path: &str, start: f64, song_rate: u32, out_rate: u32, mono: bool) -> Vec<String> {
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
    if song_rate != 0 && song_rate != out_rate {
        filters.push(if has_soxr() {
            format!("aresample={out_rate}:resampler=soxr:precision=28")
        } else {
            format!("aresample={out_rate}:filter_size=256:cutoff=0.97:phase_shift=10")
        });
    }
    if !filters.is_empty() {
        args.extend(["-af".into(), filters.join(",")]);
    }
    args.extend(
        ["-ac", "2", "-ar", &out_rate.to_string(), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
            .iter()
            .map(|s| s.to_string()),
    );
    args
}

struct Job {
    seg: Segment,
    gen: u64,
    mono: bool,
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
    let args = decode_args(&job.seg.path, start, job.seg.song_rate, job.seg.rate, job.mono);
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
        // gapless: go straight on with the next song when it has the same output rate
        let next = if !failed && shared.gapless.load(Ordering::Relaxed) {
            let mut slot = shared.next.lock().unwrap();
            match slot.as_ref() {
                Some(n) if n.out_rate == job.seg.rate => slot.take(),
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
            song_rate: n.song_rate,
            announce: true,
        };
        shared.segments.lock().unwrap().push(seg.clone());
        job = Job { seg, gen, mono: song_channels(&n.path) == 1 };
        if !alive(&shared) {
            return;
        }
    }
}

fn song_channels(path: &str) -> i64 {
    realhires::probe::probe(path).map(|i| i.channels as i64).unwrap_or(2)
}

enum Output {
    None,
    Cpal { _stream: cpal::Stream, info: OutputInfo, device: String, rates: Vec<(u32, u32)>, default_rate: u32 },
    Null { stop: Arc<AtomicBool>, info: OutputInfo },
    /// Tests pull the samples themselves (`Engine::pull`).
    Manual { state: RenderState, info: OutputInfo },
}

/// The engine the player window owns (one per app).
pub struct Engine {
    shared: Arc<Shared>,
    output: Output,
    manual: bool,
    device: String,
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

impl Engine {
    pub fn new() -> Engine {
        Engine::with(false)
    }

    /// An engine without a device, whose samples `pull()` takes (tests).
    pub fn manual() -> Engine {
        Engine::with(true)
    }

    fn with(manual: bool) -> Engine {
        let shared = Arc::new(Shared {
            buf: Mutex::new(Buffer::default()),
            paused: AtomicBool::new(false),
            gain_bits: AtomicU32::new(1.0f32.to_bits()),
            eq: Mutex::new((1, EqState::default())),
            heard_seg: AtomicU64::new(0),
            heard_frames: AtomicU64::new(0),
            segments: Mutex::new(vec![]),
            events: Mutex::new(vec![]),
            next: Mutex::new(None),
            gapless: AtomicBool::new(true),
            ids: AtomicU64::new(0),
        });
        Engine { shared, output: Output::None, manual, device: String::new(), last_error: String::new(), current: None }
    }

    pub fn set_device(&mut self, name: &str) {
        if self.device != name {
            self.device = name.to_string();
            if let Some(cur) = self.current.clone() {
                // reopen on the new device where the song is now
                let pos = self.position().unwrap_or(cur.start);
                let paused = self.is_paused();
                self.output = Output::None;
                self.start(&cur.path, pos, cur.song_rate, Some(cur.token), paused);
            }
        }
    }

    pub fn set_gain(&self, gain: f32) {
        self.shared.gain_bits.store(gain.max(0.0).to_bits(), Ordering::Relaxed);
    }

    pub fn gain(&self) -> f32 {
        f32::from_bits(self.shared.gain_bits.load(Ordering::Relaxed))
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

    /// The song to go on with gaplessly when this one ends (None = stop there).
    pub fn set_next(&self, next: Option<(&str, u32)>) {
        *self.shared.next.lock().unwrap() =
            next.map(|(p, r)| Next { path: p.to_string(), song_rate: r, out_rate: self.rate_for(r) });
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
            _ if song_rate == 0 => 48_000,
            _ => song_rate,
        }
    }

    pub fn info(&self) -> Option<OutputInfo> {
        let mut info = match &self.output {
            Output::Cpal { info, .. } | Output::Null { info, .. } | Output::Manual { info, .. } => info.clone(),
            Output::None => return None,
        };
        if let Some(seg) = self.heard_segment().or_else(|| self.current.clone()) {
            info.resampled_from = (seg.song_rate != 0 && seg.song_rate != info.rate).then_some(seg.song_rate);
        }
        Some(info)
    }

    /// Play `path` from `start` seconds; returns the token of this song.
    pub fn play(&mut self, path: &str, start: f64, song_rate: u32) -> u64 {
        self.start(path, start, song_rate, None, false)
    }

    fn start(&mut self, path: &str, start: f64, song_rate: u32, token: Option<u64>, paused: bool) -> u64 {
        let song_rate = if song_rate == 0 {
            realhires::probe::probe(path).map(|i| i.sample_rate as u32).unwrap_or(0)
        } else {
            song_rate
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
        let rate = match self.open_for(song_rate) {
            Ok(r) => r,
            Err(e) => {
                self.last_error = e.clone();
                let token = token.unwrap_or_else(|| self.shared.new_id());
                let seg = Segment {
                    id: self.shared.new_id(),
                    token,
                    path: path.into(),
                    start,
                    rate: 0,
                    song_rate,
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
        let seg = Segment { id: self.shared.new_id(), token, path: path.into(), start, rate, song_rate, announce };
        {
            let mut segs = self.shared.segments.lock().unwrap();
            if segs.len() > 64 {
                segs.drain(..32);
            }
            segs.push(seg.clone());
        }
        self.current = Some(seg.clone());
        let mono = realhires::probe::probe(path).map(|i| i.channels == 1).unwrap_or(false);
        spawn_decoder(self.shared.clone(), Job { seg, gen, mono });
        token
    }

    /// Make sure an output runs at the right rate for a song of `song_rate`.
    fn open_for(&mut self, song_rate: u32) -> Result<u32, String> {
        if self.manual || null_backend() {
            let rate = if song_rate == 0 { 48_000 } else { song_rate };
            let same = match &self.output {
                Output::Null { info, .. } | Output::Manual { info, .. } => info.rate == rate,
                _ => false,
            };
            if !same {
                let info = OutputInfo {
                    // English, shown with `i18n::tr()`
                    device: if self.manual { n_!("Test output").into() } else { n_!("Null output (no sound)").into() },
                    rate,
                    channels: 2,
                    format: n_!("32-bit float").into(),
                    resampled_from: None,
                    soxr: has_soxr(),
                };
                if let Output::Null { stop, .. } = &self.output {
                    stop.store(true, Ordering::Relaxed);
                }
                self.output = if self.manual {
                    Output::Manual { state: RenderState::new(&self.shared, rate), info }
                } else {
                    let stop = Arc::new(AtomicBool::new(false));
                    spawn_null(self.shared.clone(), rate, stop.clone());
                    Output::Null { stop, info }
                };
            }
            return Ok(rate);
        }
        let host = cpal::default_host();
        let device = if self.device.is_empty() {
            host.default_output_device()
        } else {
            host.output_devices().ok().and_then(|mut it| {
                it.find(|d| d.description().map(|x| x.name() == self.device).unwrap_or(false))
            })
            .or_else(|| host.default_output_device())
        };
        let Some(device) = device else {
            self.output = Output::None;
            return Err(tr!("No audio output device was found.").into());
        };
        let name = device.description().map(|d| d.name().to_string()).unwrap_or_else(|_| n_!("Output").into());
        let config = choose_config(&device, song_rate)?;
        let rate = config.sample_rate();
        if let Output::Cpal { info, device: dev, .. } = &self.output {
            if info.rate == rate && *dev == name {
                return Ok(rate);
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
        };
        self.output = Output::Cpal { _stream: stream, info, device: name, rates, default_rate };
        Ok(rate)
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
    pub fn cue(&mut self, path: &str, start: f64, song_rate: u32) -> u64 {
        self.start(path, start, song_rate, None, true)
    }

    pub fn seek(&mut self, seconds: f64) {
        if let Some(cur) = self.heard_segment().or_else(|| self.current.clone()) {
            let paused = self.is_paused();
            self.start(&cur.path, seconds.max(0.0), cur.song_rate, Some(cur.token), paused);
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
        F::F32 => stream_of::<f32>(device, config, shared).map_err(err),
        F::I32 => stream_of::<i32>(device, config, shared).map_err(err),
        F::I16 => stream_of::<i16>(device, config, shared).map_err(err),
        F::U16 => stream_of::<u16>(device, config, shared).map_err(err),
        other => Err(tr!("The audio device wants {format} samples, which this player cannot write.", format = format!("{other:?}"))),
    }
}

fn stream_of<T>(device: &cpal::Device, config: &cpal::StreamConfig, shared: Arc<Shared>) -> Result<cpal::Stream, cpal::Error>
where
    T: cpal::SizedSample + cpal::FromSample<f32>,
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
                *d = T::from_sample(*s);
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
        let ta = e.play(&a, 0.0, 44_100);
        assert_eq!(e.info().unwrap().rate, 44_100);
        e.set_next(Some((&b, 44_100)));
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
        e.play(&a, 0.0, 44_100);
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
        let tb = e.play(&bad.to_string_lossy(), 0.0, 44_100);
        let (_, ev) = pull_until(&mut e, 1000, |ev| ev.iter().any(|x| matches!(x, Event::Failed { .. })));
        assert!(
            ev.iter().any(|x| matches!(x, Event::Failed { token, message, .. } if *token == tb && message.contains("FFmpeg"))),
            "{ev:?}"
        );

        // a different rate is resampled for a fixed output (the null and test outputs follow the song)
        let args = decode_args(&a, 1.5, 96_000, 48_000, true);
        let joined = args.join(" ");
        assert!(joined.contains("-ss 1.500") && joined.contains("aresample=48000") && joined.contains("pan=stereo"), "{joined}");
        assert!(!decode_args(&a, 0.0, 48_000, 48_000, false).join(" ").contains("-af"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
