//! Synced lyrics from LRCLIB (https://lrclib.net), the way LRCGET does it
//! (the counterpart of `realhires/lyrics.py`, with the same rules, messages
//! and files).
//!
//! Every song is looked up by the title, artist, album and length in its tags.
//! Synced lyrics are saved as `<name>.lrc` next to the song; when LRCLIB only
//! has plain (unsynced) lyrics they are saved as `<name>.txt`, and a song
//! LRCLIB lists as instrumental gets an `.lrc` holding `[au: instrumental]`, so
//! it is not looked up again.  These are the conventions of LRCGET, the
//! official LRCLIB client, so files made by either program are understood by
//! the other.
//!
//! LRCLIB is a free service and asks batch clients to behave: requests go out
//! one at a time, with a pause between them, the program names itself in the
//! User-Agent, and a 429/503 answer's Retry-After is always honoured (with
//! some jitter).
//!
//! Lyrics files are never deleted: an existing `.txt` stays when a synced
//! `.lrc` is added next to it, and existing files are only overwritten when
//! the user chose "Download again and replace them".
//!
//! Credits: the lyrics come from LRCLIB and its contributors, and this feature
//! is modelled on LRCGET.  Both are by tranxuanthang and MIT-licensed (LRCGET:
//! "Copyright (c) 2023 tranxuanthang", <https://github.com/tranxuanthang/lrcget>;
//! the LRCLIB server: "Copyright (c) 2024 tranxuanthang",
//! <https://github.com/tranxuanthang/lrclib>).  This module is a port of
//! Real-Hi-Res's own `realhires/lyrics.py` and contains no code from either;
//! it follows LRCGET's conventions and LRCLIB's published API.  [`CREDIT`] and
//! [`ABOUT_CREDIT`] are shown in the desktop app.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use unicode_normalization::UnicodeNormalization;

use crate::json::{self, Value};
use crate::paths::{abspath, extension, file_name, parent};
use crate::probe::{probe, tag, Tags};
use crate::pyfmt::round_int;
use crate::pyunicode::{casefold, is_alnum, is_space, is_word, rstrip, strip};
use crate::settings::Settings;
use crate::verdict::FileResult;

pub const DEFAULT_SERVER: &str = "https://lrclib.net";
pub use crate::PROJECT_URL;
/// LRCGET's mark for instrumental tracks.
pub const INSTRUMENTAL_LRC: &str = "[au: instrumental]";
/// LRCLIB matches a record whose length is within ±2 s.
pub const DURATION_TOLERANCE_S: f64 = 2.0;
/// LRCLIB rejects longer durations; such songs are looked up without one.
pub const MAX_DURATION_S: i64 = 3600;
/// Between request starts: ~85/min, below the ~90/min of an identified client.
pub const MIN_INTERVAL_S: f64 = 0.7;
/// Pause after each answer (LRCLIB asks for 200-500 ms in batch work).
pub const MIN_GAP_S: f64 = 0.25;
pub const TIMEOUT_S: f64 = 15.0;
/// Per request, for 429/503/5xx answers and network errors.
pub const MAX_ATTEMPTS: u32 = 4;
pub const MAX_BACKOFF_S: f64 = 8.0;
pub const MAX_RETRY_AFTER_S: f64 = 3600.0;
/// A lyrics file read for display is cut here.
pub const MAX_LYRICS_BYTES: u64 = 512 * 1024;
/// Tests only; never below MIN_INTERVAL_S for lrclib.net.
pub const INTERVAL_ENV: &str = "REALHIRES_LYRICS_INTERVAL";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Status {
    Synced,
    Plain,
    Instrumental,
    PlainOnly,
    NotFound,
    Skipped,
    NoTags,
    Failed,
}

impl Status {
    /// The CLI's label (Python `STATUS_LABEL`).
    pub fn label(self) -> &'static str {
        match self {
            Status::Synced => "Synced lyrics saved",
            Status::Plain => "Plain lyrics saved",
            Status::Instrumental => "Marked instrumental",
            Status::PlainOnly => "Only plain lyrics",
            Status::NotFound => "Not found",
            Status::Skipped => "Skipped",
            Status::NoTags => "No title/artist",
            Status::Failed => "Failed",
        }
    }

    /// Python `SAVED_STATUSES`.
    pub fn saves(self) -> bool {
        matches!(self, Status::Synced | Status::Plain | Status::Instrumental)
    }
}

/// Shown in the lyrics window (the same text as the Python version's `CREDIT`).
pub const CREDIT: &str = "Lyrics are provided by LRCLIB (lrclib.net) and its contributors. This window works like \
                          LRCGET by tranxuanthang, the official LRCLIB client.";
/// Shown in the About box (Python `ABOUT_CREDIT`).
pub const ABOUT_CREDIT: &str = "Synced lyrics come from LRCLIB (lrclib.net) and its contributors. The lyrics feature \
                                is modelled on LRCGET by tranxuanthang (MIT License), the official LRCLIB client.";

pub const EXISTING_CHOICES: [&str; 3] =
    ["Skip songs that have synced lyrics", "Skip songs that have any lyrics", "Download again and replace them"];
pub const EXISTING_CLI: [(&str, &str); 3] =
    [("skip-synced", EXISTING_CHOICES[0]), ("skip-any", EXISTING_CHOICES[1]), ("replace", EXISTING_CHOICES[2])];

/// A lookup failed (network, server error, or an answer that could not be
/// read), or the user stopped the downloads.
#[derive(Clone, Debug, PartialEq)]
pub enum LyricsError {
    Failed(String),
    Stopped,
}

impl std::fmt::Display for LyricsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LyricsError::Failed(m) => f.write_str(m),
            LyricsError::Stopped => f.write_str("stopped"),
        }
    }
}

impl std::error::Error for LyricsError {}

fn failed(msg: String) -> LyricsError {
    LyricsError::Failed(msg)
}

/// LRCLIB asks every client to name itself, its version and its home page.
pub fn user_agent() -> String {
    format!("{} v{} ({PROJECT_URL})", crate::APP_NAME, crate::VERSION)
}

/// `urlsplit(url).netloc`.
pub(crate) fn netloc(url: &str) -> &str {
    let rest = match url.find("://") {
        Some(i) => &url[i + 3..],
        None => match url.strip_prefix("//") {
            Some(r) => r,
            None => return "",
        },
    };
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    &rest[..end]
}

/// `urlsplit(url).hostname` (lower case, without user and port).
pub(crate) fn hostname(url: &str) -> String {
    let n = netloc(url);
    let n = n.rsplit_once('@').map(|(_, h)| h).unwrap_or(n);
    let host = if let Some(rest) = n.strip_prefix('[') {
        rest.split(']').next().unwrap_or("")
    } else {
        n.split(':').next().unwrap_or("")
    };
    host.to_lowercase()
}

pub fn is_official(server: &str) -> bool {
    let host = hostname(server);
    let host = host.trim_end_matches('.');
    host == "lrclib.net" || host.ends_with(".lrclib.net")
}

pub fn default_interval(server: &str) -> f64 {
    let value = match std::env::var(INTERVAL_ENV) {
        Ok(v) => v.trim().parse::<f64>().unwrap_or(MIN_INTERVAL_S),
        Err(_) => MIN_INTERVAL_S,
    };
    let value = if value.is_nan() { 0.0 } else { value.max(0.0) };
    if is_official(server) {
        value.max(MIN_INTERVAL_S)
    } else {
        value
    }
}

// ------------------------------------------------------------------ HTTP ---

/// What a transport can fail with.
#[derive(Clone, Debug)]
pub enum TransportError {
    /// The address cannot be used at all (Python's ValueError).
    Invalid(String),
    /// The server's certificate could not be verified: not retried.
    Certificate(String),
    /// No connection or no answer: retried.
    Network(String),
}

/// (status, lower-case headers, body)
pub type Answer = (u16, Vec<(String, String)>, Vec<u8>);
pub type Transport = Box<dyn FnMut(&str, &[(&str, &str)], f64) -> Result<Answer, TransportError> + Send>;

/// A verifying TLS setup: the system's certificates (or $SSL_CERT_FILE),
/// else the bundled Mozilla roots.
fn tls_config() -> Arc<rustls::ClientConfig> {
    static CONFIG: OnceLock<Arc<rustls::ClientConfig>> = OnceLock::new();
    CONFIG
        .get_or_init(|| {
            let mut roots = rustls::RootCertStore::empty();
            for cert in rustls_native_certs::load_native_certs().certs {
                let _ = roots.add(cert);
            }
            if roots.is_empty() {
                roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
            }
            let provider = Arc::new(rustls::crypto::ring::default_provider());
            let config = rustls::ClientConfig::builder_with_provider(provider)
                .with_safe_default_protocol_versions()
                .expect("TLS versions")
                .with_root_certificates(roots)
                .with_no_client_auth();
            Arc::new(config)
        })
        .clone()
}

fn env_any(names: &[&str]) -> Option<String> {
    names.iter().find_map(|n| std::env::var(n).ok().filter(|v| !v.trim().is_empty()))
}

/// Whether `host` is excluded from the proxy by $no_proxy (as Python's
/// `urllib.request.proxy_bypass_environment`, plus addresses of this computer).
fn bypass_proxy(host: &str) -> bool {
    if host == "localhost" || host == "::1" || host.starts_with("127.") {
        return true;
    }
    let Some(list) = env_any(&["no_proxy", "NO_PROXY"]) else {
        return false;
    };
    if list.trim() == "*" {
        return true;
    }
    list.split(',').map(|n| n.trim().trim_start_matches("*.").trim_start_matches('.').to_lowercase()).any(|name| {
        let name = name.split(':').next().unwrap_or("").to_string();
        !name.is_empty() && (host == name || host.ends_with(&format!(".{name}")))
    })
}

/// The proxy for a URL, from $https_proxy / $http_proxy as Python's urllib does.
fn proxy_for(url: &str) -> Option<String> {
    let host = hostname(url);
    if bypass_proxy(&host) {
        return None;
    }
    if url.to_lowercase().starts_with("https:") {
        env_any(&["https_proxy", "HTTPS_PROXY"])
    } else {
        env_any(&["http_proxy", "HTTP_PROXY"])
    }
}

/// The real transport: one ureq agent per proxy, so connections are reused.
pub fn http_transport() -> Transport {
    let mut agents: HashMap<Option<String>, ureq::Agent> = HashMap::new();
    Box::new(move |url: &str, headers: &[(&str, &str)], timeout: f64| {
        let scheme = url.split("://").next().unwrap_or("").to_lowercase();
        if scheme != "http" && scheme != "https" {
            return Err(TransportError::Invalid(format!("unknown url type: {scheme}")));
        }
        let proxy = proxy_for(url);
        let agent = match agents.get(&proxy) {
            Some(a) => a.clone(),
            None => {
                let t = Duration::from_secs_f64(timeout);
                let mut b = ureq::AgentBuilder::new()
                    .tls_config(tls_config())
                    .timeout_connect(t)
                    .timeout_read(t)
                    .timeout_write(t)
                    .redirects(5);
                if let Some(p) = &proxy {
                    match ureq::Proxy::new(p) {
                        Ok(p) => b = b.proxy(p),
                        Err(e) => return Err(TransportError::Network(format!("the proxy {p} cannot be used: {e}"))),
                    }
                }
                let a = b.build();
                agents.insert(proxy.clone(), a.clone());
                a
            }
        };
        let mut req = agent.get(url);
        for (k, v) in headers {
            req = req.set(k, v);
        }
        let resp = match req.call() {
            Ok(r) => r,
            Err(ureq::Error::Status(_, r)) => r,
            Err(ureq::Error::Transport(t)) => return Err(transport_error(&t)),
        };
        let status = resp.status();
        let hdrs: Vec<(String, String)> = resp
            .headers_names()
            .into_iter()
            .map(|n| {
                let v = resp.header(&n).unwrap_or("").to_string();
                (n.to_lowercase(), v)
            })
            .collect();
        let mut body = vec![];
        if let Err(e) = resp.into_reader().take(64 << 20).read_to_end(&mut body) {
            return Err(TransportError::Network(io_reason(&e)));
        }
        Ok((status, hdrs, body))
    })
}

fn io_reason(e: &std::io::Error) -> String {
    if matches!(e.kind(), std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock) {
        "no answer in time".into()
    } else {
        e.to_string()
    }
}

fn transport_error(t: &ureq::Transport) -> TransportError {
    use std::error::Error as _;
    if matches!(t.kind(), ureq::ErrorKind::InvalidUrl | ureq::ErrorKind::UnknownScheme) {
        return TransportError::Invalid(t.message().map(str::to_string).unwrap_or_else(|| t.kind().to_string()));
    }
    let mut src: Option<&(dyn std::error::Error + 'static)> = t.source();
    let mut reasons = vec![];
    while let Some(e) = src {
        if let Some(rustls::Error::InvalidCertificate(c)) = e.downcast_ref::<rustls::Error>() {
            return TransportError::Certificate(c.to_string());
        }
        if let Some(io) = e.downcast_ref::<std::io::Error>() {
            if let Some(rustls::Error::InvalidCertificate(c)) =
                io.get_ref().and_then(|i| i.downcast_ref::<rustls::Error>())
            {
                return TransportError::Certificate(c.to_string());
            }
            if matches!(io.kind(), std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock) {
                return TransportError::Network("no answer in time".into());
            }
        }
        reasons.push(e.to_string());
        src = e.source();
    }
    let text = t.message().map(str::to_string).or_else(|| reasons.last().cloned()).unwrap_or_else(|| t.to_string());
    if text.to_lowercase().contains("certificate") {
        return TransportError::Certificate(text);
    }
    TransportError::Network(text)
}

/// `urllib.parse.urlencode` (quote_plus: spaces become '+').
pub fn urlencode(params: &[(String, String)]) -> String {
    fn quote(s: &str) -> String {
        let mut out = String::new();
        for b in s.bytes() {
            match b {
                b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => out.push(b as char),
                b' ' => out.push('+'),
                _ => out.push_str(&format!("%{b:02X}")),
            }
        }
        out
    }
    params.iter().map(|(k, v)| format!("{}={}", quote(k), quote(v))).collect::<Vec<_>>().join("&")
}

/// Python's repr() of a string, for messages.
pub(crate) fn repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::from(q);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == q => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

pub(crate) fn monotonic() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// Sequential, polite LRCLIB requests (one client per batch, used from one thread).
pub struct LrclibClient {
    pub server: String,
    stop: Option<Arc<AtomicBool>>,
    transport: Transport,
    pub min_interval: f64,
    min_gap: f64,
    pub timeout: f64,
    clock: Box<dyn Fn() -> f64 + Send>,
    sleep: Box<dyn FnMut(f64) + Send>,
    rng: u64,
    last_start: Option<f64>,
    last_end: Option<f64>,
    /// set by Retry-After; also holds for the next song after a failure
    not_before: f64,
    pub requests: usize,
}

impl LrclibClient {
    pub fn new(server: &str) -> Self {
        let mut s = server.trim().trim_end_matches('/').to_string();
        if s.is_empty() {
            s = DEFAULT_SERVER.into();
        }
        if !s.contains("://") {
            s = format!("https://{s}");
        }
        static SEED: AtomicU64 = AtomicU64::new(0);
        let seed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(1)
            ^ SEED.fetch_add(0x9E37_79B9_7F4A_7C15, Ordering::Relaxed);
        let min_interval = default_interval(&s);
        LrclibClient {
            server: s,
            stop: None,
            transport: http_transport(),
            min_interval,
            min_gap: MIN_GAP_S.min(min_interval),
            timeout: TIMEOUT_S,
            clock: Box::new(monotonic),
            sleep: Box::new(|s| std::thread::sleep(Duration::from_secs_f64(s))),
            rng: seed | 1,
            last_start: None,
            last_end: None,
            not_before: 0.0,
            requests: 0,
        }
    }

    pub fn with_stop(mut self, stop: Arc<AtomicBool>) -> Self {
        self.stop = Some(stop);
        self
    }

    pub fn with_transport(mut self, t: Transport) -> Self {
        self.transport = t;
        self
    }

    pub fn with_clock(
        mut self,
        clock: impl Fn() -> f64 + Send + 'static,
        sleep: impl FnMut(f64) + Send + 'static,
    ) -> Self {
        self.clock = Box::new(clock);
        self.sleep = Box::new(sleep);
        self
    }

    pub fn with_seed(mut self, seed: u64) -> Self {
        self.rng = seed | 1;
        self
    }

    pub fn with_min_interval(mut self, v: f64) -> Self {
        self.min_interval = v;
        self.min_gap = MIN_GAP_S.min(v);
        self
    }

    /// `urlsplit(server).netloc or server`.
    pub fn host(&self) -> String {
        let n = netloc(&self.server);
        if n.is_empty() {
            self.server.clone()
        } else {
            n.to_string()
        }
    }

    fn stopped(&self) -> bool {
        self.stop.as_ref().is_some_and(|s| s.load(Ordering::Relaxed))
    }

    fn uniform(&mut self, hi: f64) -> f64 {
        // xorshift64*: jitter only needs to be spread out, not secret
        self.rng ^= self.rng >> 12;
        self.rng ^= self.rng << 25;
        self.rng ^= self.rng >> 27;
        let x = self.rng.wrapping_mul(0x2545_F491_4F6C_DD1D) >> 11;
        hi * (x as f64 / (1u64 << 53) as f64)
    }

    fn wait(&mut self, seconds: f64) -> Result<(), LyricsError> {
        if !(seconds > 0.0) {
            return Ok(());
        }
        if self.stop.is_some() {
            let end = (self.clock)() + seconds;
            loop {
                if self.stopped() {
                    return Err(LyricsError::Stopped);
                }
                let left = end - (self.clock)();
                if left <= 0.0 {
                    return Ok(());
                }
                (self.sleep)(left.min(0.05));
            }
        }
        (self.sleep)(seconds);
        Ok(())
    }

    fn throttle(&mut self) -> Result<(), LyricsError> {
        if self.stopped() {
            return Err(LyricsError::Stopped);
        }
        let mut due = self.not_before;
        if let Some(s) = self.last_start {
            due = due.max(s + self.min_interval);
        }
        if let Some(e) = self.last_end {
            due = due.max(e + self.min_gap);
        }
        let now = (self.clock)();
        self.wait(due - now)
    }

    fn retry_after(headers: &[(String, String)]) -> Option<f64> {
        let v = headers.iter().find(|(k, _)| k == "retry-after")?.1.trim().parse::<f64>().ok()?;
        if v.is_nan() {
            return None;
        }
        Some(v.clamp(0.0, MAX_RETRY_AFTER_S))
    }

    /// The parsed JSON answer, or None for 404.
    pub fn request(&mut self, path: &str, params: &[(String, String)]) -> Result<Option<Value>, LyricsError> {
        let url = format!("{}{path}?{}", self.server, urlencode(params));
        let ua = user_agent();
        let headers = [("User-Agent", ua.as_str()), ("Accept", "application/json")];
        let mut backoff = 1.0f64;
        let mut problem = String::new();
        let host = self.host();
        for attempt in 1..=MAX_ATTEMPTS {
            self.throttle()?;
            self.last_start = Some((self.clock)());
            self.requests += 1;
            let wait;
            match (self.transport)(&url, &headers, self.timeout) {
                Err(TransportError::Invalid(e)) => {
                    return Err(failed(format!("The LRCLIB server address {} is not valid: {e}.", repr(&self.server))))
                }
                Err(TransportError::Certificate(e)) => {
                    self.last_end = Some((self.clock)());
                    return Err(failed(format!(
                        "Could not connect to {host} securely: the certificate of the server could not be verified \
                         ({e})."
                    )));
                }
                Err(TransportError::Network(e)) => {
                    self.last_end = Some((self.clock)());
                    problem = format!("could not reach {host}: {e}");
                    wait = backoff;
                }
                Ok((status, hdrs, body)) => {
                    self.last_end = Some((self.clock)());
                    if status == 200 {
                        let text = std::str::from_utf8(&body)
                            .map_err(|_| failed(format!("{host} sent an answer that could not be read.")))?;
                        return json::parse(text)
                            .map(Some)
                            .map_err(|_| failed(format!("{host} sent an answer that could not be read.")));
                    }
                    if status == 404 {
                        return Ok(None);
                    }
                    if status == 429 || status == 503 {
                        let mut w = Self::retry_after(&hdrs).unwrap_or(if status == 503 { 1.0 } else { backoff });
                        w += self.uniform(0.3 * w); // LRCLIB asks for up to 30 % jitter
                        self.not_before = (self.clock)() + w;
                        problem = if status == 429 {
                            "too many requests, the server asked to wait".into()
                        } else {
                            "the server is busy".into()
                        };
                        wait = w;
                    } else if status >= 500 {
                        problem = format!("server error (HTTP {status})");
                        wait = backoff;
                    } else {
                        return Err(failed(format!(
                            "{host} refused the request (HTTP {status}{}).",
                            api_message(&body)
                        )));
                    }
                }
            }
            if attempt < MAX_ATTEMPTS {
                self.wait(wait)?;
                backoff = (backoff * 2.0).min(MAX_BACKOFF_S);
            }
        }
        Err(failed(format!("No answer from {host} after {MAX_ATTEMPTS} attempts ({problem}).")))
    }

    pub fn get(&mut self, track: &Track) -> Result<Option<Value>, LyricsError> {
        Ok(self.request("/api/get", &track.params())?.filter(|v| matches!(v, Value::Object(_))))
    }

    pub fn search(&mut self, track: &Track) -> Result<Vec<Value>, LyricsError> {
        let params = [("track_name".to_string(), track.title.clone()), ("artist_name".into(), track.primary_artist())];
        Ok(match self.request("/api/search", &params)? {
            Some(Value::Array(items)) => items.into_iter().filter(|v| matches!(v, Value::Object(_))).collect(),
            _ => vec![],
        })
    }
}

fn api_message(body: &[u8]) -> String {
    let Ok(text) = std::str::from_utf8(body) else {
        return String::new();
    };
    match json::parse(text) {
        Ok(v @ Value::Object(_)) => match v.get("message") {
            Some(Value::Str(m)) if !strip(m).is_empty() => format!(": {m}"),
            _ => String::new(),
        },
        _ => String::new(),
    }
}

// ---------------------------------------------------------------- tracks ---

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Track {
    pub path: String,
    pub title: String,
    pub artist: String,
    pub album: String,
    pub duration: Option<f64>,
}

impl Track {
    pub fn new(path: &str, title: &str, artist: &str, album: &str, duration: Option<f64>) -> Self {
        Track { path: path.into(), title: title.into(), artist: artist.into(), album: album.into(), duration }
    }

    /// FFmpeg joins several artist tags with ';' — the first one is the main artist.
    pub fn primary_artist(&self) -> String {
        let first = strip(self.artist.split(';').next().unwrap_or(""));
        if first.is_empty() {
            self.artist.clone()
        } else {
            first.to_string()
        }
    }

    pub fn label(&self) -> String {
        format!("{} – {}", self.artist, self.title)
    }

    pub fn params(&self) -> Vec<(String, String)> {
        let artists: Vec<&str> = self.artist.split(';').map(strip).filter(|a| !a.is_empty()).collect();
        let mut p = vec![("track_name".to_string(), self.title.clone()), ("artist_name".into(), artists.join(", "))];
        if !self.album.is_empty() {
            p.push(("album_name".into(), self.album.clone()));
        }
        if let Some(d) = self.duration.filter(|d| *d != 0.0 && d.is_finite()) {
            let r = round_int(d);
            if (1..=MAX_DURATION_S).contains(&r) {
                p.push(("duration".into(), r.to_string()));
            }
        }
        p
    }
}

/// The track to look up from tags, or the sentence saying why there is none.
pub fn track_from_tags(path: &str, tags: &Tags, duration: Option<f64>) -> Result<Track, String> {
    let get = |k: &str| tag(tags, k).filter(|v| !v.is_empty());
    let title = strip(get("title").unwrap_or("")).to_string();
    let artist = strip(get("artist").or_else(|| get("album_artist")).unwrap_or("")).to_string();
    if title.is_empty() || artist.is_empty() {
        let missing = if title.is_empty() && artist.is_empty() {
            "title or artist tags"
        } else if title.is_empty() {
            "title tag"
        } else {
            "artist tag"
        };
        return Err(format!("The file has no {missing}, so LRCLIB cannot be asked for its lyrics."));
    }
    let album = strip(get("album").unwrap_or("")).to_string();
    Ok(Track { path: path.into(), title, artist, album, duration })
}

/// The track to look up, from a scan result when there is one, else from the
/// file's tags.  Err(LyricsError) when the file cannot be read.
pub fn read_track(path: &str, result: Option<&FileResult>) -> Result<Result<Track, String>, LyricsError> {
    if let Some(r) = result {
        if !r.tags.is_empty() && r.duration.is_some_and(|d| d != 0.0) {
            return Ok(track_from_tags(path, &r.tags, r.duration));
        }
    }
    match probe(path) {
        Ok(info) => Ok(track_from_tags(path, &info.tags, info.duration)),
        Err(e) => Err(failed(format!("The tags could not be read: {e}"))),
    }
}

// ---------------------------------------------------------------- lyrics ---

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Lyrics {
    pub synced: String,
    pub plain: String,
    pub instrumental: bool,
    pub lrclib_id: Option<i64>,
    pub title: String,
    pub artist: String,
    pub album: String,
    pub duration: Option<f64>,
    /// "exact" (/api/get) or "search" (/api/search)
    pub how: String,
}

fn text(v: Option<&Value>) -> String {
    match v {
        Some(Value::Str(s)) if !strip(s).is_empty() => s.clone(),
        _ => String::new(),
    }
}

/// Python `float(value)` for a JSON value.
fn num(v: Option<&Value>) -> Option<f64> {
    match v? {
        Value::Int(i) => Some(*i as f64),
        Value::Float(f) => Some(*f),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::Str(s) => strip(s).replace('_', "").parse().ok(),
        _ => None,
    }
}

fn truthy(v: Option<&Value>) -> bool {
    v.is_some_and(|v| v.truthy())
}

/// A usable LRCLIB record as Lyrics (synced > plain > instrumental, as LRCGET), else None.
pub fn parse_record(rec: &Value, how: &str) -> Option<Lyrics> {
    if !matches!(rec, Value::Object(_)) {
        return None;
    }
    let mut synced = text(rec.get("syncedLyrics"));
    let mut plain = text(rec.get("plainLyrics"));
    let mut instrumental = truthy(rec.get("instrumental")) || is_instrumental_lrc(&synced);
    if instrumental && plain.is_empty() && (synced.is_empty() || is_instrumental_lrc(&synced)) {
        synced.clear();
    } else if !synced.is_empty() || !plain.is_empty() {
        instrumental = false;
    }
    if synced.is_empty() && plain.is_empty() && !instrumental {
        return None;
    }
    if !synced.is_empty() && plain.is_empty() {
        plain = strip_timestamps(&synced);
    }
    let lrclib_id = match rec.get("id") {
        Some(Value::Int(i)) => Some(*i),
        _ => None,
    };
    let name = if truthy(rec.get("trackName")) { rec.get("trackName") } else { rec.get("name") };
    Some(Lyrics {
        synced,
        plain,
        instrumental,
        lrclib_id,
        title: text(name),
        artist: text(rec.get("artistName")),
        album: text(rec.get("albumName")),
        duration: num(rec.get("duration")),
        how: how.into(),
    })
}

pub fn is_instrumental_lrc(text: &str) -> bool {
    let low = text.to_lowercase();
    low.contains("[au:") && low.contains("instrumental")
}

/// Python's `str.splitlines()`.
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut out = vec![];
    let mut start = 0;
    let mut it = s.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        if matches!(
            c,
            '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}'
        ) {
            out.push(&s[start..i]);
            let mut end = i + c.len_utf8();
            if c == '\r' {
                if let Some((_, '\n')) = it.peek() {
                    it.next();
                    end += 1;
                }
            }
            start = end;
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// `^(\s*\[[^\]]*\])+\s*` removed from a line.
fn strip_line_timestamps(line: &str) -> &str {
    let mut pos = 0;
    let mut matched = false;
    loop {
        let rest = &line[pos..];
        let lead = rest.len() - rest.trim_start_matches(is_space).len();
        let after = &rest[lead..];
        if !after.starts_with('[') {
            break;
        }
        match after[1..].find(']') {
            Some(j) => {
                pos += lead + 1 + j + 1;
                matched = true;
            }
            None => break,
        }
    }
    if !matched {
        return line;
    }
    line[pos..].trim_start_matches(is_space)
}

pub fn strip_timestamps(synced: &str) -> String {
    let lines: Vec<&str> = splitlines(synced).into_iter().map(strip_line_timestamps).collect();
    strip(&lines.join("\n")).to_string()
}

/// `[(\[（【].*?[)\]）】]` replaced by a space.
fn sub_brackets(s: &str) -> String {
    let chars: Vec<char> = s.chars().collect();
    let mut out = String::new();
    let mut i = 0;
    while i < chars.len() {
        if matches!(chars[i], '(' | '[' | '（' | '【') {
            let close = chars[i + 1..]
                .iter()
                .position(|c| matches!(c, ')' | ']' | '）' | '】') || *c == '\n')
                .filter(|j| chars[i + 1 + j] != '\n');
            if let Some(j) = close {
                out.push(' ');
                i += j + 2;
                continue;
            }
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

/// Where `.*$` can end from `from`: the end, or a final newline (None when a
/// newline comes earlier).
fn line_end(chars: &[char], from: usize) -> Option<usize> {
    match chars[from..].iter().position(|c| *c == '\n') {
        None => Some(chars.len()),
        Some(j) if from + j == chars.len() - 1 => Some(from + j),
        Some(_) => None,
    }
}

fn starts_with_at(chars: &[char], at: usize, word: &str) -> bool {
    let w: Vec<char> = word.chars().collect();
    chars.len() >= at + w.len() && chars[at..at + w.len()] == w[..]
}

/// `\s(feat|ft|featuring)\.?\s.*$` replaced by a space.
fn sub_feat(s: &str) -> String {
    let chars: Vec<char> = s.chars().collect();
    for i in 0..chars.len() {
        if !is_space(chars[i]) {
            continue;
        }
        for w in ["feat", "ft", "featuring"] {
            if !starts_with_at(&chars, i + 1, w) {
                continue;
            }
            let mut k = i + 1 + w.chars().count();
            let space_at = |k: usize| k < chars.len() && is_space(chars[k]);
            if k < chars.len() && chars[k] == '.' && space_at(k + 1) {
                k += 2;
            } else if space_at(k) {
                k += 1;
            } else {
                continue;
            }
            if let Some(end) = line_end(&chars, k) {
                let mut out: String = chars[..i].iter().collect();
                out.push(' ');
                out.extend(&chars[end..]);
                return out;
            }
        }
    }
    s.to_string()
}

/// `\s[-–]\s.*\b(remaster(ed)?|version|edit|mix|live|mono|stereo|demo|bonus)\b.*$` replaced by a space.
fn sub_version(s: &str) -> String {
    const WORDS: [&str; 10] =
        ["remastered", "remaster", "version", "edit", "mix", "live", "mono", "stereo", "demo", "bonus"];
    let chars: Vec<char> = s.chars().collect();
    let n = chars.len();
    for i in 0..n.saturating_sub(2) {
        if !(is_space(chars[i]) && matches!(chars[i + 1], '-' | '–') && is_space(chars[i + 2])) {
            continue;
        }
        let mut j = i + 3;
        while j <= n {
            if j > i + 3 && chars[j - 1] == '\n' {
                break; // `.*` cannot cross a newline
            }
            if j < n && !is_word(chars[j - 1]) {
                for w in WORDS {
                    if !starts_with_at(&chars, j, w) {
                        continue;
                    }
                    let k = j + w.chars().count();
                    if k < n && is_word(chars[k]) {
                        continue;
                    }
                    if let Some(end) = line_end(&chars, k) {
                        let mut out: String = chars[..i].iter().collect();
                        out.push(' ');
                        out.extend(&chars[end..]);
                        return out;
                    }
                }
            }
            j += 1;
        }
    }
    s.to_string()
}

fn words(s: &str) -> Vec<&str> {
    s.split(|c: char| !is_alnum(c)).filter(|w| !w.is_empty()).collect()
}

/// Comparable form of a title or artist: case, punctuation, "(Remastered
/// 2011)" and "feat. X" parts do not matter.  Letters of every script are kept.
pub fn normalize_name(name: &str) -> String {
    let base = casefold(&name.nfkc().collect::<String>());
    let cut = sub_version(&sub_feat(&sub_brackets(&base)));
    for s in [cut.as_str(), base.as_str()] {
        let w = words(s);
        if !w.is_empty() {
            return w.join(" ");
        }
    }
    strip(&base).to_string()
}

pub(crate) fn same_artist(a: &str, b: &str) -> bool {
    let (na, nb) = (normalize_name(a), normalize_name(b));
    if na.is_empty() || nb.is_empty() {
        return false;
    }
    na == nb || format!(" {nb} ").contains(&format!(" {na} ")) || format!(" {na} ").contains(&format!(" {nb} "))
}

/// The search result that is this song: same title and artist and, when the
/// song's length is known, a length within ±2 s (as /api/get requires).
/// Synced lyrics win, then the closest length, then the same album.
pub fn best_match(records: &[Value], track: &Track) -> Option<Lyrics> {
    let want_title = normalize_name(&track.title);
    let mut best: Option<(Lyrics, (u8, f64, u8))> = None;
    for rec in records {
        let Some(lyr) = parse_record(rec, "search") else {
            continue;
        };
        if normalize_name(&lyr.title) != want_title || !same_artist(&lyr.artist, &track.primary_artist()) {
            continue;
        }
        let album_ok = !track.album.is_empty() && normalize_name(&lyr.album) == normalize_name(&track.album);
        let diff = match track.duration.filter(|d| *d != 0.0) {
            Some(d) => match lyr.duration {
                Some(l) if !((l - d).abs() > DURATION_TOLERANCE_S) => (l - d).abs(),
                _ => continue,
            },
            None => {
                if !track.album.is_empty() && !album_ok {
                    continue; // without a length, only the same album makes it safe
                }
                0.0
            }
        };
        let kind = if !lyr.synced.is_empty() {
            0
        } else if !lyr.plain.is_empty() {
            1
        } else {
            2
        };
        let key = (kind, diff, if album_ok { 0 } else { 1 });
        let better = match &best {
            None => true,
            Some((_, k)) => key.partial_cmp(k) == Some(std::cmp::Ordering::Less),
        };
        if better {
            best = Some((lyr, key));
        }
    }
    best.map(|(l, _)| l)
}

/// /api/get (exact title, artist, album and length), then optionally /api/search.
pub fn find_lyrics(client: &mut LrclibClient, track: &Track, search: bool) -> Result<Option<Lyrics>, LyricsError> {
    let lyr = client.get(track)?.and_then(|r| parse_record(&r, "exact"));
    if let Some(l) = &lyr {
        if !l.synced.is_empty() || l.instrumental || !search {
            return Ok(lyr); // only plain lyrics: a search may still find a synced copy of the same song
        }
    }
    if search {
        if let Some(found) = best_match(&client.search(track)?, track) {
            let better = match &lyr {
                None => true,
                Some(l) => !found.synced.is_empty() && l.synced.is_empty(),
            };
            if better {
                return Ok(Some(found));
            }
        }
    }
    Ok(lyr)
}

// ----------------------------------------------------------------- files ---

/// `<folder>/<name>.lrc` for `<folder>/<name>.flac`.
pub fn sidecar(path: &str, ext: &str) -> String {
    let e = extension(&file_name(path));
    format!("{}{ext}", &path[..path.len() - e.len()])
}

/// (the .lrc, the .txt) next to a song, each None when it does not exist.
pub fn existing_lyrics(path: &str) -> (Option<String>, Option<String>) {
    let (lrc, txt) = (sidecar(path, ".lrc"), sidecar(path, ".txt"));
    (Path::new(&lrc).is_file().then_some(lrc), Path::new(&txt).is_file().then_some(txt))
}

fn replace_mode(cfg: &Settings) -> bool {
    cfg.lyrics_existing == EXISTING_CHOICES[2]
}

/// Why a song is not looked up at all ("" = look it up), per Settings → Lyrics.
pub fn skip_reason(path: &str, cfg: &Settings) -> String {
    if replace_mode(cfg) {
        return String::new();
    }
    let (lrc, txt) = existing_lyrics(path);
    if let Some(lrc) = lrc {
        let with_what =
            if is_instrumental_lrc(&read_head(&lrc, 4096)) { "an instrumental mark" } else { "synced lyrics" };
        return format!("Already has {with_what} ({}).", file_name(&lrc));
    }
    let mode = save_mode(cfg);
    if mode != "lrc" {
        let (synced, plain) = crate::embed::embedded_lyrics(path);
        let text = if synced.is_empty() { &plain } else { &synced };
        if !text.is_empty() && is_instrumental_lrc(text) {
            return "Already has an instrumental mark inside the file.".into();
        }
        if !synced.is_empty() {
            return "Already has synced lyrics inside the file.".into();
        }
        if !plain.is_empty() && (mode == "plain" || cfg.lyrics_existing == EXISTING_CHOICES[1]) {
            return "Already has lyrics inside the file.".into();
        }
    }
    if let Some(txt) = txt {
        if cfg.lyrics_existing == EXISTING_CHOICES[1] {
            return format!("Already has lyrics ({}).", file_name(&txt));
        }
    }
    String::new()
}

/// "lrc", "embed", "both" or "plain" from Settings -> Lyrics -> Save lyrics as (2.0).
pub fn save_mode(cfg: &Settings) -> &'static str {
    use crate::settings::LYRICS_SAVE_CHOICES as C;
    let v = cfg.lyrics_save_as.as_str();
    if v == C[1] {
        "embed"
    } else if v == C[2] {
        "both"
    } else if v == C[3] {
        "plain"
    } else {
        "lrc"
    }
}

fn read_head(path: &str, size: u64) -> String {
    let mut buf = vec![];
    match std::fs::File::open(path) {
        Ok(f) => {
            let _ = f.take(size).read_to_end(&mut buf);
            String::from_utf8_lossy(&buf).into_owned()
        }
        Err(_) => String::new(),
    }
}

/// Write UTF-8 text with '\n' line ends through a temporary file, so a song
/// never ends up with half a lyrics file.
pub fn write_text(path: &str, text: &str) -> std::io::Result<()> {
    let norm = text.replace("\r\n", "\n").replace('\r', "\n");
    let lines: Vec<&str> = norm.split('\n').map(rstrip).collect();
    let data = format!("{}\n", lines.join("\n").trim_matches('\n'));
    let folder = parent(&abspath(path));
    static N: AtomicU64 = AtomicU64::new(0);
    let mut tries = 0;
    let (tmp, mut file) = loop {
        let seed =
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
        let name = format!(
            ".realhires-lyrics-{:x}{:x}{:x}.tmp",
            std::process::id(),
            seed & 0xffff_ffff,
            N.fetch_add(1, Ordering::Relaxed)
        );
        let tmp = Path::new(&folder).join(name);
        match std::fs::OpenOptions::new().write(true).create_new(true).open(&tmp) {
            Ok(f) => break (tmp, f),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists && tries < 100 => tries += 1,
            Err(e) => return Err(e),
        }
    };
    let res = file.write_all(data.as_bytes()).and_then(|_| file.sync_all());
    drop(file);
    let res = res.and_then(|_| std::fs::rename(&tmp, path));
    if res.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    res
}

/// (kind, text, file) of the lyrics next to a song, for display; kind is
/// Synced, Instrumental or Plain.  None when there is none.
pub fn read_lyrics_file(path: &str) -> Option<(Status, String, String)> {
    let (lrc, txt) = existing_lyrics(path);
    for (file, is_lrc) in [(lrc, true), (txt, false)] {
        let Some(file) = file else { continue };
        let mut raw = vec![];
        match std::fs::File::open(&file) {
            Ok(f) => {
                if f.take(MAX_LYRICS_BYTES).read_to_end(&mut raw).is_err() {
                    continue;
                }
            }
            Err(_) => continue,
        }
        let body = raw.strip_prefix(b"\xef\xbb\xbf".as_slice()).unwrap_or(&raw);
        let text = String::from_utf8_lossy(body).into_owned();
        if is_lrc {
            let kind = if is_instrumental_lrc(&text) { Status::Instrumental } else { Status::Synced };
            return Some((kind, text, file));
        }
        return Some((Status::Plain, text, file));
    }
    // 2.0: lyrics inside the file (MP3, FLAC)
    crate::embed::lyrics_of(path).map(|(kind, text)| (kind, text, path.to_string()))
}

#[derive(Clone, Debug, PartialEq)]
pub struct LyricsOutcome {
    pub path: String,
    pub status: Status,
    /// a full sentence, shown verbatim
    pub message: String,
    pub track: Option<Track>,
    pub written: Vec<String>,
    /// "exact" or "search" when lyrics were found
    pub how: String,
}

impl LyricsOutcome {
    pub fn new(path: &str, status: Status, message: String) -> Self {
        LyricsOutcome { path: path.into(), status, message, track: None, written: vec![], how: String::new() }
    }

    fn with(mut self, track: Option<Track>, how: &str) -> Self {
        self.track = track;
        self.how = how.into();
        self
    }

    pub fn saved(&self) -> bool {
        self.status.saves() && !self.written.is_empty()
    }
}

/// A few words for a table cell (the desktop app); `message` holds the full sentence.
pub fn short_status(o: &LyricsOutcome) -> String {
    if o.status.saves() && o.written.is_empty() && o.message.starts_with("Would") {
        return "Would be saved (dry run)".into();
    }
    match o.status {
        Status::Synced => format!("Synced lyrics saved{}", if o.how == "search" { ", found by searching" } else { "" }),
        Status::Plain => "Plain lyrics saved (no synced ones)".into(),
        Status::Instrumental => {
            if o.written.is_empty() { "Instrumental, not marked" } else { "Marked instrumental" }.into()
        }
        Status::Skipped => short_skip(&o.message),
        Status::Failed => format!("Failed: {}", o.message),
        Status::PlainOnly => "Only plain lyrics, not saved".into(),
        Status::NotFound => "Not found on LRCLIB".into(),
        Status::NoTags => "No title or artist tag".into(),
    }
}

/// "Already has synced lyrics (Song.lrc)." -> "Already has synced lyrics".
pub fn short_skip(reason: &str) -> String {
    if reason.starts_with("Already") {
        return reason.split(" (").next().unwrap_or("").trim_end_matches('.').to_string();
    }
    "Kept the existing .txt".into()
}

pub fn save_lyrics(path: &str, lyr: &Lyrics, cfg: &Settings, track: Option<Track>, dry_run: bool) -> LyricsOutcome {
    let replace = replace_mode(cfg);
    let (lrc, txt) = (sidecar(path, ".lrc"), sidecar(path, ".txt"));
    let (lrc_name, txt_name, name) = (file_name(&lrc), file_name(&txt), file_name(path));
    let via = if lyr.how == "search" { " (found by searching)" } else { "" };
    let how = lyr.how.as_str();
    let mut mode = save_mode(cfg);
    let mut note = "";
    if mode != "lrc" && !crate::embed::supported(path) {
        mode = "lrc";
        note = " Lyrics can only be embedded in MP3 and FLAC files.";
    }
    let mut files: Vec<(String, String)> = vec![];
    let mut inside: Option<(String, String)> = None;
    let status;
    let msg;
    if !lyr.synced.is_empty() {
        let plain = if lyr.plain.is_empty() { strip_timestamps(&lyr.synced) } else { lyr.plain.clone() };
        if mode == "plain" {
            status = Status::Plain;
            inside = Some((String::new(), plain));
            msg = format!("Embedded the lyrics in {name}, without timing{via}.");
        } else {
            status = Status::Synced;
            if mode == "lrc" || mode == "both" {
                files.push((lrc.clone(), lyr.synced.clone()));
            }
            if mode == "embed" || mode == "both" {
                inside = Some((lyr.synced.clone(), plain));
            }
            msg = match mode {
                "lrc" => format!("Saved synced lyrics as {lrc_name}{via}."),
                "embed" => format!("Embedded synced lyrics in {name}{via}."),
                _ => format!("Saved synced lyrics as {lrc_name} and embedded them in {name}{via}."),
            };
        }
    } else if !lyr.plain.is_empty() {
        if !cfg.lyrics_plain {
            let what = if mode == "lrc" { "saving them as .txt" } else { "saving plain lyrics" };
            return LyricsOutcome::new(
                path,
                Status::PlainOnly,
                format!("LRCLIB has only plain (unsynced) lyrics for this song, and {what} is turned off."),
            )
            .with(track, how);
        }
        let keep_txt = Path::new(&txt).is_file() && !replace;
        if mode == "lrc" && keep_txt {
            return LyricsOutcome::new(
                path,
                Status::Skipped,
                format!("LRCLIB has only plain lyrics for this song, so the existing {txt_name} was kept."),
            )
            .with(track, how);
        }
        status = Status::Plain;
        if mode == "lrc" {
            files.push((txt.clone(), lyr.plain.clone()));
            msg = format!("LRCLIB has only plain lyrics for this song; saved them as {txt_name}{via}.");
        } else if mode == "both" && !keep_txt {
            files.push((txt.clone(), lyr.plain.clone()));
            inside = Some((String::new(), lyr.plain.clone()));
            msg = format!(
                "LRCLIB has only plain lyrics for this song; saved them as {txt_name} and embedded them in {name}{via}."
            );
        } else {
            inside = Some((String::new(), lyr.plain.clone()));
            msg = format!("LRCLIB has only plain lyrics for this song; embedded them in {name}{via}.");
        }
    } else {
        if !cfg.lyrics_instrumental {
            return LyricsOutcome::new(
                path,
                Status::Instrumental,
                "LRCLIB lists this song as instrumental; marking instrumental songs is turned off, so nothing was \
                 saved."
                    .into(),
            )
            .with(track, how);
        }
        if (mode == "lrc" || mode == "both") && Path::new(&txt).is_file() && !replace {
            return LyricsOutcome::new(
                path,
                Status::Skipped,
                format!("LRCLIB lists this song as instrumental, but it has lyrics in {txt_name}, which were kept."),
            )
            .with(track, how);
        }
        status = Status::Instrumental;
        if mode == "lrc" || mode == "both" {
            files.push((lrc.clone(), INSTRUMENTAL_LRC.to_string()));
        }
        if mode == "embed" || mode == "both" {
            inside = Some((INSTRUMENTAL_LRC.to_string(), INSTRUMENTAL_LRC.to_string()));
        } else if mode == "plain" {
            inside = Some((String::new(), INSTRUMENTAL_LRC.to_string()));
        }
        msg = match mode {
            "lrc" => format!("LRCLIB lists this song as instrumental; marked it in {lrc_name}{via}."),
            "both" => format!("LRCLIB lists this song as instrumental; marked it in {lrc_name} and in {name}{via}."),
            _ => format!("LRCLIB lists this song as instrumental; marked it in {name}{via}."),
        };
    }
    let msg = format!("{msg}{note}");
    if dry_run {
        let mut c = msg.chars();
        let first = c.next().map(|f| f.to_lowercase().collect::<String>()).unwrap_or_default();
        return LyricsOutcome::new(path, status, format!("Would save: {first}{}", c.as_str())).with(track, how);
    }
    let mut written = vec![];
    for (target, text) in &files {
        if let Err(e) = write_text(target, text) {
            let mut o = LyricsOutcome::new(path, Status::Failed, format!("Could not save {}: {e}", file_name(target)))
                .with(track, how);
            o.written = written;
            return o;
        }
        written.push(target.clone());
    }
    if let Some((synced, plain)) = &inside {
        let before = crate::store::stat(path).ok();
        if let Err(e) = crate::embed::embed_lyrics(path, synced, plain) {
            let mut o = LyricsOutcome::new(path, Status::Failed, format!("Could not embed the lyrics in {name}: {e}"))
                .with(track, how);
            o.written = written;
            return o;
        }
        written.push(path.to_string());
        // the audio is unchanged, so its saved scan results stay valid
        if let (Some((size, mtime)), Some(st)) = (before, crate::store::open_store(cfg)) {
            st.retag(path, size, mtime);
        }
    }
    let mut o = LyricsOutcome::new(path, status, msg).with(track, how);
    o.written = written;
    o
}

/// Look one song up and save its lyrics.  Never fails, except with
/// LyricsError::Stopped.
pub fn fetch_for_file(
    path: &str,
    client: &mut LrclibClient,
    cfg: &Settings,
    result: Option<&FileResult>,
    dry_run: bool,
) -> Result<LyricsOutcome, LyricsError> {
    let why = skip_reason(path, cfg);
    if !why.is_empty() {
        return Ok(LyricsOutcome::new(path, Status::Skipped, why));
    }
    let track = match read_track(path, result) {
        Ok(Ok(t)) => t,
        Ok(Err(why)) => return Ok(LyricsOutcome::new(path, Status::NoTags, why)),
        Err(LyricsError::Stopped) => return Err(LyricsError::Stopped),
        Err(LyricsError::Failed(m)) => return Ok(LyricsOutcome::new(path, Status::Failed, m)),
    };
    let lyr = match find_lyrics(client, &track, cfg.lyrics_search) {
        Ok(l) => l,
        Err(LyricsError::Stopped) => return Err(LyricsError::Stopped),
        Err(LyricsError::Failed(m)) => return Ok(LyricsOutcome::new(path, Status::Failed, m).with(Some(track), "")),
    };
    Ok(match lyr {
        None => LyricsOutcome::new(path, Status::NotFound, "LRCLIB has no lyrics for this song (yet).".into())
            .with(Some(track), ""),
        Some(l) => save_lyrics(path, &l, cfg, Some(track), dry_run),
    })
}

/// The outcomes of `paths`, one song at a time, passed to `each`.  Stops
/// quietly when the client's stop flag is set.
pub fn run_lyrics(
    paths: &[String],
    cfg: &Settings,
    results: &HashMap<String, FileResult>,
    dry_run: bool,
    client: &mut LrclibClient,
    mut each: impl FnMut(LyricsOutcome),
) {
    for path in paths {
        if client.stopped() {
            return;
        }
        match fetch_for_file(path, client, cfg, results.get(path), dry_run) {
            Ok(o) => each(o),
            Err(_) => return,
        }
    }
}

/// "12 with synced lyrics, 3 with plain lyrics, 5 not found, 20 skipped."
pub fn lyrics_summary(outcomes: &[LyricsOutcome]) -> String {
    let words = [
        (Status::Synced, "with synced lyrics"),
        (Status::Plain, "with plain lyrics"),
        (Status::Instrumental, "instrumental"),
        (Status::PlainOnly, "with only plain lyrics (not saved)"),
        (Status::NotFound, "not found on LRCLIB"),
        (Status::Skipped, "skipped (already had lyrics)"),
        (Status::NoTags, "without title/artist tags"),
        (Status::Failed, "failed"),
    ];
    let parts: Vec<String> = words
        .iter()
        .filter_map(|(k, w)| {
            let n = outcomes.iter().filter(|o| o.status == *k).count();
            (n > 0).then(|| format!("{n} {w}"))
        })
        .collect();
    if parts.is_empty() {
        "nothing to do.".into()
    } else {
        format!("{}.", parts.join(", "))
    }
}

/// One request to the LRCLIB server (CLI --check-lyrics-server).
pub fn check_server(client: &mut LrclibClient) -> Result<String, LyricsError> {
    let track =
        Track::new("", "I Want to Live", "Borislav Slavov", "Baldur's Gate 3 (Original Game Soundtrack)", Some(233.0));
    let rec = client.get(&track)?;
    let found = if rec.as_ref().and_then(|r| parse_record(r, "exact")).is_some() {
        "an answer with lyrics"
    } else {
        "an answer"
    };
    Ok(format!("{} can be reached: the test lookup got {found}.", client.server))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    #[allow(clippy::too_many_arguments)]
    fn rec(
        title: &str,
        artist: &str,
        album: &str,
        duration: f64,
        synced: &str,
        plain: &str,
        instr: bool,
        id: i64,
    ) -> Value {
        let mut o = vec![
            ("id".to_string(), Value::Int(id)),
            ("trackName".into(), Value::Str(title.into())),
            ("artistName".into(), Value::Str(artist.into())),
            ("albumName".into(), Value::Str(album.into())),
            ("duration".into(), Value::Float(duration)),
            ("instrumental".into(), Value::Bool(instr)),
        ];
        o.push(("plainLyrics".into(), if plain.is_empty() { Value::Null } else { Value::Str(plain.into()) }));
        o.push(("syncedLyrics".into(), if synced.is_empty() { Value::Null } else { Value::Str(synced.into()) }));
        Value::Object(o)
    }

    #[test]
    fn client_is_polite() {
        let now = Arc::new(Mutex::new(100.0f64));
        let calls: Arc<Mutex<Vec<(f64, String, String)>>> = Arc::default();
        let body =
            json::dumps(&rec("Song", "Band", "Album", 200.0, "[00:01.00] la", "", false, 1), json::Style::COMPACT);
        let mut answers = vec![
            (429u16, vec![("retry-after".to_string(), "2".to_string())], b"{\"name\": \"RateLimitExceeded\"}".to_vec()),
            (200, vec![], body.into_bytes()),
            (404, vec![], b"{}".to_vec()),
        ]
        .into_iter();
        let (n1, c1) = (now.clone(), calls.clone());
        let transport: Transport = Box::new(move |url, headers, _| {
            let ua = headers.iter().find(|(k, _)| *k == "User-Agent").unwrap().1.to_string();
            let t = *n1.lock().unwrap();
            c1.lock().unwrap().push((t, url.to_string(), ua));
            *n1.lock().unwrap() += 0.1;
            Ok(answers.next().unwrap())
        });
        let (n2, n3) = (now.clone(), now.clone());
        let mut c = LrclibClient::new("https://lrclib.net")
            .with_transport(transport)
            .with_clock(move || *n2.lock().unwrap(), move |s| *n3.lock().unwrap() += s)
            .with_seed(1);
        let lyr = find_lyrics(&mut c, &Track::new("", "Song", "Band", "Album", Some(200.4)), true).unwrap().unwrap();
        assert!(!lyr.synced.is_empty() && lyr.how == "exact");
        let calls_v = calls.lock().unwrap().clone();
        assert_eq!(calls_v[0].2, format!("Universal Music Player v{} (https://github.com/SU2H1/Universal-Music-Player)", crate::VERSION));
        assert_eq!(
            calls_v[0].1,
            "https://lrclib.net/api/get?track_name=Song&artist_name=Band&album_name=Album&duration=200"
        );
        let waited = calls_v[1].0 - (calls_v[0].0 + 0.1);
        assert!((2.0..=2.6 + 1e-9).contains(&waited), "{waited}");
        assert!(c.get(&Track::new("", "Other", "Band", "", None)).unwrap().is_none());
        let calls_v = calls.lock().unwrap().clone();
        assert!(calls_v[2].0 - calls_v[1].0 >= MIN_INTERVAL_S - 1e-9);
        assert!(calls_v[2].0 - (calls_v[1].0 + 0.1) >= MIN_GAP_S - 1e-9);
        assert_eq!(default_interval("https://lrclib.net"), MIN_INTERVAL_S);
        assert_eq!(LrclibClient::new("lrclib.net").server, "https://lrclib.net");
        assert!(is_official("https://www.LRCLIB.net./x") && !is_official("http://127.0.0.1:8080"));
    }

    #[test]
    fn client_gives_up_and_keeps_waiting() {
        let now = Arc::new(Mutex::new(0.0f64));
        let calls: Arc<Mutex<Vec<f64>>> = Arc::default();
        let (n1, c1) = (now.clone(), calls.clone());
        let (n2, n3) = (now.clone(), now.clone());
        let mut c = LrclibClient::new("https://lrclib.net")
            .with_transport(Box::new(move |_, _, _| {
                c1.lock().unwrap().push(*n1.lock().unwrap());
                Ok((503, vec![("retry-after".into(), "5".into())], vec![]))
            }))
            .with_clock(move || *n2.lock().unwrap(), move |s| *n3.lock().unwrap() += s)
            .with_seed(2);
        let err = c.get(&Track::new("", "Song", "Band", "", None)).unwrap_err();
        assert!(err.to_string().contains("after 4 attempts"), "{err}");
        let v = calls.lock().unwrap().clone();
        assert_eq!(v.len(), MAX_ATTEMPTS as usize);
        assert!(v.windows(2).all(|w| w[1] - w[0] >= 5.0));
        let last = *now.lock().unwrap();
        calls.lock().unwrap().clear();
        assert!(c.get(&Track::new("", "Next", "Band", "", None)).is_err());
        assert!(calls.lock().unwrap()[0] - last >= 5.0);
        let mut bad = LrclibClient::new("http://127.0.0.1:1")
            .with_transport(Box::new(|_, _, _| Ok((400, vec![], b"{\"message\": \"duration: bad\"}".to_vec()))));
        let err = bad.get(&Track::new("", "Song", "Band", "", None)).unwrap_err().to_string();
        assert!(err.contains("refused the request (HTTP 400: duration: bad)"), "{err}");
        let mut c = LrclibClient::new("https://lrclib.net")
            .with_transport(Box::new(|_, _, _| Err(TransportError::Certificate("UnknownIssuer".into()))))
            .with_clock(monotonic, |_| {});
        let err = c.get(&Track::new("", "Song", "Band", "", None)).unwrap_err().to_string();
        assert!(err.contains("Could not connect to lrclib.net securely"), "{err}");
        assert_eq!(c.requests, 1);
        let stop = Arc::new(AtomicBool::new(true));
        let mut c = LrclibClient::new("https://lrclib.net").with_stop(stop);
        assert_eq!(c.get(&Track::new("", "Song", "Band", "", None)), Err(LyricsError::Stopped));
    }

    #[test]
    fn records_and_search_matching() {
        let synced_a = "[00:00.50] First line of Alpha\n[00:02.00] Second line of Alpha";
        let r = parse_record(&rec("A", "", "", 1.0, synced_a, "", false, 1), "exact").unwrap();
        assert_eq!(r.synced, synced_a);
        assert_eq!(r.plain, "First line of Alpha\nSecond line of Alpha");
        assert_eq!(parse_record(&rec("A", "", "", 1.0, "", "words", false, 1), "exact").unwrap().plain, "words");
        let inst = parse_record(&rec("A", "", "", 1.0, "", "", true, 1), "exact").unwrap();
        assert!(inst.instrumental && inst.synced.is_empty() && inst.plain.is_empty());
        assert!(parse_record(&rec("A", "", "", 1.0, "", "", false, 1), "exact").is_none());
        assert!(parse_record(&Value::Str("nonsense".into()), "exact").is_none());
        let track = Track::new("", "Song", "Queen;David Bowie", "Album", Some(200.0));
        let found = vec![
            rec("Song", "Queen", "", 205.0, "[00:01.00] too long", "", false, 1),
            rec("Song", "Someone Else", "", 200.0, "[00:01.00] other artist", "", false, 2),
            rec("Song", "Queen", "", 201.5, "", "plain words", false, 3),
            rec("Song (Remastered 2011)", "Queen & David Bowie", "", 199.0, "[00:01.00] yes", "", false, 4),
            rec("Another Song", "Queen", "", 200.0, "[00:01.00] other title", "", false, 5),
        ];
        assert_eq!(best_match(&found, &track).unwrap().lrclib_id, Some(4));
        assert_eq!(best_match(&found[..3], &track).unwrap().lrclib_id, Some(3));
        assert!(best_match(&found[..2], &track).is_none());
        assert!(best_match(&found, &Track::new("", "Song", "Queen", "Other album", None)).is_none());
        assert_eq!(normalize_name("夜に駆ける"), "夜に駆ける");
        assert_eq!(normalize_name("Song - 2011 Remastered Version"), "song");
        assert_eq!(normalize_name("Hello feat. Someone"), "hello");
        assert_eq!(normalize_name("Ｓｔｒａßｅ (Live)"), "strasse");
        assert_eq!(normalize_name("(Intro)"), "intro");
        assert_eq!(Track::new("", "Song", "A;B", "", None).params()[1].1, "A, B");
        assert_eq!(strip_timestamps("[ar: x]\n[00:01.00][00:02.00]  la la\r\nplain"), "la la\nplain");
        assert_eq!(urlencode(&[("a b".into(), "Café/&+".into())]), "a+b=Caf%C3%A9%2F%26%2B");
    }

    #[test]
    fn files_and_skip_rules() {
        let dir = std::env::temp_dir().join(format!("realhires-lyrics-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let song = dir.join("Song.flac").to_string_lossy().into_owned();
        std::fs::write(&song, b"").unwrap();
        let mut cfg = Settings::default();
        assert_eq!(skip_reason(&song, &cfg), "");
        std::fs::write(dir.join("Song.txt"), "words").unwrap();
        assert_eq!(skip_reason(&song, &cfg), "");
        cfg.lyrics_existing = EXISTING_CHOICES[1].into();
        assert_eq!(skip_reason(&song, &cfg), "Already has lyrics (Song.txt).");
        write_text(&sidecar(&song, ".lrc"), &format!("{INSTRUMENTAL_LRC}  \r\n\r\n")).unwrap();
        assert_eq!(std::fs::read_to_string(dir.join("Song.lrc")).unwrap(), "[au: instrumental]\n");
        cfg.lyrics_existing = EXISTING_CHOICES[0].into();
        assert_eq!(skip_reason(&song, &cfg), "Already has an instrumental mark (Song.lrc).");
        cfg.lyrics_existing = EXISTING_CHOICES[2].into();
        assert_eq!(skip_reason(&song, &cfg), "");
        assert_eq!(read_lyrics_file(&song).unwrap().0, Status::Instrumental);
        assert!(std::fs::read_dir(&dir).unwrap().all(|e| !e
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".realhires")));
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[cfg(test)]
mod python_tests {
    use super::*;

    fn pairs(text: &str) -> Vec<(String, String)> {
        json::parse(text)
            .unwrap()
            .as_array()
            .unwrap()
            .iter()
            .map(|p| {
                let a = p.as_array().unwrap();
                (a[0].as_str().unwrap().to_string(), a[1].as_str().unwrap().to_string())
            })
            .collect()
    }

    /// The names and expected forms come from `realhires.lyrics.normalize_name()`.
    #[test]
    fn names_are_normalized_as_python_does() {
        for (name, want) in pairs(include_str!("../tests/python_names.json")) {
            assert_eq!(normalize_name(&name), want, "{name:?}");
        }
        for (text, want) in pairs(include_str!("../tests/python_timestamps.json")) {
            assert_eq!(strip_timestamps(&text), want, "{text:?}");
        }
    }
}
