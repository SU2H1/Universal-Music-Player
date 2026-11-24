//! Song information from MusicBrainz (https://musicbrainz.org), 2.1: the port
//! of `realhires/musicbrainz.py`, which explains it in full.
//!
//! "Get info from MusicBrainz…" searches for an album (or a song), reads the
//! chosen release's track list, matches the user's songs to its tracks and
//! shows what would change; the automatic lookup (`musicbrainz_auto`, off by
//! default) fills in only empty tags of songs it is certain about, and records
//! every song it looked up in the table `musicbrainz` of `library.sqlite`.
//! MusicBrainz asks for at most one request per second and a User-Agent that
//! names the program; only the songs' tags are sent.  Tags are written with
//! `tagedit::save_tags()`, so only the tag of a file changes, never its audio.
//!
//! MusicBrainz's core data is CC0; tags and genres are supplementary data under
//! CC BY-NC-SA 3.0.  Written against the published web-service documentation;
//! no code from MusicBrainz or Picard.  `tests/python_musicbrainz.json` holds
//! Python's answers, so both versions build the same requests and choose alike.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use crate::json::{self, Value};
use crate::lyrics::{self, http_transport, normalize_name, Transport, TransportError};
use crate::settings::Settings;
use crate::tagedit::{self, clean_value, split_number, Changes, TagOutcome, FIELDS};

pub const DEFAULT_SERVER: &str = "https://musicbrainz.org";
pub const MIN_INTERVAL_S: f64 = 1.0;
pub const TIMEOUT_S: f64 = 20.0;
pub const MAX_ATTEMPTS: usize = 4;
pub const MAX_BACKOFF_S: f64 = 8.0;
pub const MAX_RETRY_AFTER_S: f64 = 600.0;
pub const INTERVAL_ENV: &str = "REALHIRES_MUSICBRAINZ_INTERVAL";
pub const SEARCH_LIMIT: usize = 10;
pub const DURATION_TOLERANCE_S: f64 = 3.0;
pub const AUTO_CANDIDATES: usize = 3;
pub const RELEASE_INC: &str = "recordings+artist-credits+release-groups+genres+media+work-rels+recording-level-rels+\
                               work-level-rels+artist-rels";
pub const MISSING_FIELDS: [&str; 6] = ["artist", "album", "album_artist", "date", "track", "genre"];

pub const FILLED: &str = "filled";
pub const COMPLETE: &str = "complete";
pub const NO_MATCH: &str = "no-match";
pub const NO_TAGS: &str = "no-tags";
pub const FAILED: &str = "failed";
pub const SAVED: &str = "saved";

pub const CREDIT: &str = "Song information comes from MusicBrainz (musicbrainz.org), the open music encyclopedia: \
                          its core data is public domain (CC0), its genres are under CC BY-NC-SA 3.0.";
pub const ABOUT_CREDIT: &str = "Song information: MusicBrainz (musicbrainz.org), CC0 core data, genres CC BY-NC-SA 3.0.";

#[derive(Clone, Debug, PartialEq)]
pub enum MbError {
    /// A lookup could not be made (a full sentence).
    Failed(String),
    /// The user stopped the lookups.
    Stopped,
}

impl std::fmt::Display for MbError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MbError::Failed(m) => f.write_str(m),
            MbError::Stopped => f.write_str("stopped"),
        }
    }
}

/// MusicBrainz's recommended form: "Application/version ( contact-url )".
pub fn user_agent() -> String {
    format!("UniversalMusicPlayer/{} ( {} )", crate::VERSION, crate::PROJECT_URL)
}

pub fn normalize_server(server: &str) -> String {
    let mut s = server.trim().trim_end_matches('/').to_string();
    if s.is_empty() {
        s = DEFAULT_SERVER.into();
    }
    if !s.contains("://") {
        s = format!("https://{s}");
    }
    s
}

pub fn is_official(server: &str) -> bool {
    let host = lyrics::hostname(&normalize_server(server)).to_lowercase();
    host == "musicbrainz.org" || host.ends_with(".musicbrainz.org")
}

pub fn default_interval(server: &str) -> f64 {
    let value = std::env::var(INTERVAL_ENV).ok().and_then(|v| v.trim().parse::<f64>().ok()).unwrap_or(MIN_INTERVAL_S);
    let value = if value.is_nan() { MIN_INTERVAL_S } else { value.max(0.0) };
    if is_official(server) {
        value.max(MIN_INTERVAL_S)
    } else {
        value
    }
}

// ------------------------------------------------------------------ queries ---

/// A Lucene phrase: the text in double quotes, backslashes and quotes escaped.
pub fn phrase(text: &str) -> String {
    format!("\"{}\"", clean_value(text).replace('\\', "\\\\").replace('"', "\\\""))
}

pub fn release_query(album: &str, artist: &str) -> String {
    let mut parts = vec![format!("release:{}", phrase(album))];
    if !clean_value(artist).is_empty() {
        parts.push(format!("artist:{}", phrase(artist)));
    }
    parts.join(" AND ")
}

pub fn recording_query(title: &str, artist: &str, album: &str) -> String {
    let mut parts = vec![format!("recording:{}", phrase(title))];
    if !clean_value(artist).is_empty() {
        parts.push(format!("artist:{}", phrase(artist)));
    }
    if !clean_value(album).is_empty() {
        parts.push(format!("release:{}", phrase(album)));
    }
    parts.join(" AND ")
}

// ------------------------------------------------------------------- client ---

/// The start of the last request of any client that uses the real clock, so
/// the lookup window and the automatic lookup together keep one per second.
static SHARED_LAST: std::sync::Mutex<Option<f64>> = std::sync::Mutex::new(None);

/// Sequential MusicBrainz requests (one client per batch, used from one thread).
pub struct MusicBrainzClient {
    pub server: String,
    stop: Option<Arc<AtomicBool>>,
    transport: Transport,
    pub min_interval: f64,
    pub timeout: f64,
    clock: Box<dyn Fn() -> f64 + Send>,
    sleep: Box<dyn FnMut(f64) + Send>,
    last_start: Option<f64>,
    not_before: f64,
    /// true while the real clock is used: requests are spaced with every other such client
    shared: bool,
    pub requests: usize,
}

impl MusicBrainzClient {
    pub fn new(server: &str) -> Self {
        let server = normalize_server(server);
        let min_interval = default_interval(&server);
        MusicBrainzClient {
            server,
            stop: None,
            transport: http_transport(),
            min_interval,
            timeout: TIMEOUT_S,
            clock: Box::new(lyrics::monotonic),
            sleep: Box::new(|s| std::thread::sleep(Duration::from_secs_f64(s))),
            last_start: None,
            not_before: 0.0,
            shared: true,
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
        self.shared = false;
        self
    }

    pub fn with_min_interval(mut self, v: f64) -> Self {
        self.min_interval = v;
        self
    }

    pub fn host(&self) -> String {
        let n = lyrics::netloc(&self.server);
        if n.is_empty() {
            self.server.clone()
        } else {
            n.to_string()
        }
    }

    fn stopped(&self) -> bool {
        self.stop.as_ref().is_some_and(|s| s.load(Ordering::Relaxed))
    }

    fn wait(&mut self, seconds: f64) -> Result<(), MbError> {
        if !(seconds > 0.0) {
            return Ok(());
        }
        if self.stop.is_some() {
            let end = (self.clock)() + seconds;
            loop {
                if self.stopped() {
                    return Err(MbError::Stopped);
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

    fn throttle(&mut self) -> Result<(), MbError> {
        if self.stopped() {
            return Err(MbError::Stopped);
        }
        let mut due = self.not_before;
        if let Some(s) = self.last_start {
            due = due.max(s + self.min_interval);
        }
        if !self.shared {
            let now = (self.clock)();
            return self.wait(due - now);
        }
        // one request at a time across the app: hold the gate while waiting
        let mut last = SHARED_LAST.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(s) = *last {
            due = due.max(s + self.min_interval);
        }
        let now = (self.clock)();
        self.wait(due - now)?;
        *last = Some((self.clock)());
        Ok(())
    }

    pub fn url(&self, path: &str, params: &[(String, String)]) -> String {
        let mut all = params.to_vec();
        all.push(("fmt".into(), "json".into()));
        format!("{}/ws/2/{path}?{}", self.server, lyrics::urlencode(&all))
    }

    /// The parsed JSON answer (an object), or None for 404.
    pub fn request(&mut self, path: &str, params: &[(String, String)]) -> Result<Option<Value>, MbError> {
        let url = self.url(path, params);
        let ua = user_agent();
        let headers = [("User-Agent", ua.as_str()), ("Accept", "application/json")];
        let host = self.host();
        let mut backoff = 1.0f64;
        let mut problem = String::new();
        for attempt in 1..=MAX_ATTEMPTS {
            self.throttle()?;
            self.last_start = Some((self.clock)());
            self.requests += 1;
            let wait;
            match (self.transport)(&url, &headers, self.timeout) {
                Err(TransportError::Invalid(e)) => {
                    return Err(MbError::Failed(format!(
                        "The MusicBrainz server address {} is not valid: {e}.",
                        lyrics::repr(&self.server)
                    )))
                }
                Err(TransportError::Certificate(e)) => {
                    return Err(MbError::Failed(format!(
                        "Could not connect to {host} securely: the certificate of the server could not be verified \
                         ({e})."
                    )))
                }
                Err(TransportError::Network(e)) => {
                    problem = format!("could not reach {host}: {e}");
                    wait = backoff;
                }
                Ok((status, hdrs, body)) => {
                    if status == 200 {
                        let bad = || MbError::Failed(format!("{host} sent an answer that could not be read."));
                        let text = std::str::from_utf8(&body).map_err(|_| bad())?;
                        let v = json::parse(text).map_err(|_| bad())?;
                        return Ok(matches!(v, Value::Object(_)).then_some(v));
                    }
                    if status == 404 {
                        return Ok(None);
                    }
                    if status == 429 || status == 503 {
                        let ra = hdrs
                            .iter()
                            .find(|(k, _)| k == "retry-after")
                            .and_then(|(_, v)| v.trim().parse::<f64>().ok())
                            .filter(|v| !v.is_nan());
                        wait = match ra {
                            Some(v) => v.clamp(0.0, MAX_RETRY_AFTER_S),
                            None => backoff.max(self.min_interval),
                        };
                        self.not_before = (self.clock)() + wait;
                        problem =
                            if status == 503 { "the server asked to slow down" } else { "too many requests" }.into();
                    } else if status >= 500 {
                        problem = format!("server error (HTTP {status})");
                        wait = backoff;
                    } else {
                        return Err(MbError::Failed(format!(
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
        Err(MbError::Failed(format!("No answer from {host} after {MAX_ATTEMPTS} attempts ({problem}).")))
    }

    pub fn search_releases(&mut self, album: &str, artist: &str) -> Result<Vec<Release>, MbError> {
        let params = [("query".to_string(), release_query(album, artist)), ("limit".into(), SEARCH_LIMIT.to_string())];
        let data = self.request("release", &params)?;
        Ok(objects(data.as_ref().and_then(|d| d.get("releases"))).map(parse_release).collect())
    }

    pub fn search_recordings(&mut self, title: &str, artist: &str, album: &str) -> Result<Vec<Recording>, MbError> {
        let params = [
            ("query".to_string(), recording_query(title, artist, album)),
            ("limit".into(), SEARCH_LIMIT.to_string()),
        ];
        let data = self.request("recording", &params)?;
        Ok(objects(data.as_ref().and_then(|d| d.get("recordings"))).map(parse_recording).collect())
    }

    pub fn release(&mut self, mbid: &str) -> Result<Option<ReleaseDetail>, MbError> {
        let path = format!("release/{}", quote_segment(mbid));
        let data = self.request(&path, &[("inc".to_string(), RELEASE_INC.to_string())])?;
        Ok(data.as_ref().map(parse_detail))
    }
}

/// `urllib.parse.quote(s, safe="")`.
fn quote_segment(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

fn api_message(body: &[u8]) -> String {
    let Ok(text) = std::str::from_utf8(body) else { return String::new() };
    match json::parse(text) {
        Ok(v @ Value::Object(_)) => match v.get("error") {
            Some(Value::Str(m)) if !crate::pyunicode::strip(m).is_empty() => format!(": {m}"),
            _ => String::new(),
        },
        _ => String::new(),
    }
}

// ------------------------------------------------------------------- data ---

fn objects(v: Option<&Value>) -> impl Iterator<Item = &Value> {
    v.and_then(Value::as_array).into_iter().flatten().filter(|x| matches!(x, Value::Object(_)))
}

/// Python's `int(v)` or 0.
fn int_of(v: Option<&Value>) -> i64 {
    match v {
        Some(Value::Int(i)) => *i,
        Some(Value::Float(f)) if f.is_finite() => f.trunc() as i64,
        Some(Value::Bool(b)) => *b as i64,
        Some(Value::Str(s)) => crate::pyunicode::strip(s).parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

fn str_of(v: Option<&Value>) -> String {
    match v {
        Some(Value::Str(s)) => clean_value(s),
        _ => String::new(),
    }
}

/// An artist credit as MusicBrainz prints it ("A feat. B").
pub fn credit(ac: Option<&Value>) -> String {
    let mut out = String::new();
    for c in objects(ac) {
        let name = match c.get("name").and_then(Value::as_str).filter(|s| !s.is_empty()) {
            Some(n) => n.to_string(),
            None => c.get("artist").and_then(|a| a.get("name")).and_then(Value::as_str).unwrap_or("").to_string(),
        };
        out.push_str(&name);
        out.push_str(c.get("joinphrase").and_then(Value::as_str).unwrap_or(""));
    }
    clean_value(&out)
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Release {
    pub id: String,
    pub title: String,
    pub artist: String,
    pub date: String,
    pub country: String,
    pub status: String,
    pub track_count: i64,
    pub disc_count: i64,
    /// "CD", "2×CD", "CD + DVD"
    pub formats: String,
    pub primary_type: String,
    pub secondary_types: Vec<String>,
    /// the release group's first release date (lookups only)
    pub first_date: String,
    /// search results only
    pub score: i64,
}

fn formats(media: &[&Value]) -> String {
    let mut counts: Vec<(String, usize)> = vec![];
    for m in media {
        let mut f = str_of(m.get("format"));
        if f.is_empty() {
            f = "Medium".into();
        }
        match counts.iter_mut().find(|(n, _)| *n == f) {
            Some(c) => c.1 += 1,
            None => counts.push((f, 1)),
        }
    }
    counts.iter().map(|(f, n)| if *n == 1 { f.clone() } else { format!("{n}×{f}") }).collect::<Vec<_>>().join(" + ")
}

pub fn parse_release(d: &Value) -> Release {
    let empty = Value::Object(vec![]);
    let rg = d.get("release-group").filter(|v| matches!(v, Value::Object(_))).unwrap_or(&empty);
    let media: Vec<&Value> = objects(d.get("media")).collect();
    let mut tracks = int_of(d.get("track-count"));
    if tracks == 0 {
        tracks = media.iter().map(|m| int_of(m.get("track-count"))).sum();
    }
    Release {
        id: str_of(d.get("id")),
        title: str_of(d.get("title")),
        artist: credit(d.get("artist-credit")),
        date: str_of(d.get("date")),
        country: str_of(d.get("country")),
        status: str_of(d.get("status")),
        track_count: tracks,
        disc_count: media.len() as i64,
        formats: formats(&media),
        primary_type: str_of(rg.get("primary-type")),
        secondary_types: rg
            .get("secondary-types")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(clean_value)
            .collect(),
        first_date: str_of(rg.get("first-release-date")),
        score: int_of(d.get("score")),
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ReleaseTrack {
    pub disc: i64,
    pub disc_count: i64,
    pub number: i64,
    pub track_count: i64,
    pub title: String,
    pub artist: String,
    /// seconds (0 = unknown)
    pub length: f64,
    pub recording_id: String,
    pub composer: String,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ReleaseDetail {
    pub release: Release,
    pub tracks: Vec<ReleaseTrack>,
    pub genre: String,
}

impl ReleaseDetail {
    /// The date written into the songs: the album's first release, else this release's.
    pub fn date(&self) -> String {
        if self.release.first_date.is_empty() {
            self.release.date.clone()
        } else {
            self.release.first_date.clone()
        }
    }

    pub fn track_at(&self, disc: i64, number: i64) -> Option<&ReleaseTrack> {
        self.tracks.iter().find(|t| t.disc == disc && t.number == number)
    }
}

/// "cool jazz" → "Cool Jazz".
pub fn genre_name(name: &str) -> String {
    clean_value(name)
        .split(' ')
        .map(|w| {
            let mut c = w.chars();
            match c.next() {
                Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
                None => String::new(),
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// The genre with the most votes of the first list that has one.
pub fn top_genre(lists: &[Option<&Value>]) -> String {
    for list in lists {
        let mut best: Option<(i64, String)> = None;
        for g in objects(*list) {
            let name = str_of(g.get("name"));
            if name.is_empty() {
                continue;
            }
            let key = (-int_of(g.get("count")), name);
            if best.as_ref().is_none_or(|b| key < *b) {
                best = Some(key);
            }
        }
        if let Some((_, name)) = best {
            return genre_name(&name);
        }
    }
    String::new()
}

/// The composers of the works a recording performs ("writer" when no composer is named).
pub fn composers(recording: &Value) -> String {
    let mut composer: Vec<String> = vec![];
    let mut writer: Vec<String> = vec![];
    for rel in objects(recording.get("relations")) {
        if rel.get("target-type").and_then(Value::as_str) != Some("work") {
            continue;
        }
        let Some(work) = rel.get("work").filter(|w| matches!(w, Value::Object(_))) else { continue };
        for wr in objects(work.get("relations")) {
            let list = match wr.get("type").and_then(Value::as_str) {
                Some("composer") => &mut composer,
                Some("writer") => &mut writer,
                _ => continue,
            };
            let name = str_of(wr.get("artist").and_then(|a| a.get("name")));
            if !name.is_empty() && !list.contains(&name) {
                list.push(name);
            }
        }
    }
    if composer.is_empty() { writer } else { composer }.join(", ")
}

pub fn parse_detail(d: &Value) -> ReleaseDetail {
    let rel = parse_release(d);
    let media: Vec<&Value> = objects(d.get("media")).collect();
    let mut tracks = vec![];
    for (mi, m) in media.iter().enumerate() {
        let mut disc = int_of(m.get("position"));
        if disc == 0 {
            disc = mi as i64 + 1;
        }
        let mtracks: Vec<&Value> = objects(m.get("tracks")).collect();
        let mut count = int_of(m.get("track-count"));
        if count == 0 {
            count = mtracks.len() as i64;
        }
        for (ti, t) in mtracks.iter().enumerate() {
            let empty = Value::Object(vec![]);
            let rec = t.get("recording").filter(|r| matches!(r, Value::Object(_))).unwrap_or(&empty);
            let mut length = int_of(t.get("length"));
            if length == 0 {
                length = int_of(rec.get("length"));
            }
            let mut number = int_of(t.get("position"));
            if number == 0 {
                number = ti as i64 + 1;
            }
            let mut title = str_of(t.get("title"));
            if title.is_empty() {
                title = str_of(rec.get("title"));
            }
            let mut artist = credit(t.get("artist-credit"));
            if artist.is_empty() {
                artist = credit(rec.get("artist-credit"));
            }
            if artist.is_empty() {
                artist = rel.artist.clone();
            }
            tracks.push(ReleaseTrack {
                disc,
                disc_count: media.len() as i64,
                number,
                track_count: count,
                title,
                artist,
                length: length as f64 / 1000.0,
                recording_id: str_of(rec.get("id")),
                composer: composers(rec),
            });
        }
    }
    let rg = d.get("release-group").filter(|v| matches!(v, Value::Object(_)));
    let genre = top_genre(&[rg.and_then(|r| r.get("genres")), d.get("genres")]);
    ReleaseDetail { release: rel, tracks, genre }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct RecordingRelease {
    pub release: Release,
    pub disc: i64,
    pub number: i64,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Recording {
    pub id: String,
    pub title: String,
    pub artist: String,
    pub length: f64,
    pub score: i64,
    pub releases: Vec<RecordingRelease>,
}

pub fn parse_recording(d: &Value) -> Recording {
    let mut out = Recording {
        id: str_of(d.get("id")),
        title: str_of(d.get("title")),
        artist: credit(d.get("artist-credit")),
        length: int_of(d.get("length")) as f64 / 1000.0,
        score: int_of(d.get("score")),
        releases: vec![],
    };
    for r in objects(d.get("releases")) {
        let first = objects(r.get("media")).next();
        let mut disc = first.map_or(0, |m| int_of(m.get("position")));
        if disc == 0 {
            disc = 1;
        }
        let number = first.map_or(0, |m| int_of(m.get("track-offset"))) + 1;
        out.releases.push(RecordingRelease { release: parse_release(r), disc, number });
    }
    out
}

// ------------------------------------------------------------------- songs ---

/// One of the user's songs: its path, its tags (`tagedit::FIELDS`) and its length.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Song {
    pub path: String,
    pub tags: Vec<(String, String)>,
    pub duration: f64,
}

impl Song {
    pub fn tag(&self, name: &str) -> String {
        self.tags.iter().find(|(k, _)| k == name).map(|(_, v)| clean_value(v)).unwrap_or_default()
    }

    pub fn number(&self) -> i64 {
        split_number(&self.tag("track")).0.parse().unwrap_or(0)
    }

    pub fn disc(&self) -> i64 {
        split_number(&self.tag("disc")).0.parse().unwrap_or(0)
    }
}

pub fn same_title(a: &str, b: &str) -> bool {
    let na = normalize_name(a);
    !na.is_empty() && na == normalize_name(b)
}

pub fn same_artist(a: &str, b: &str) -> bool {
    lyrics::same_artist(a, b)
}

/// True when either length is unknown or they differ by at most 3 s.
pub fn length_ok(a: f64, b: f64) -> bool {
    a == 0.0 || b == 0.0 || (a - b).abs() <= DURATION_TOLERANCE_S
}

fn or1(n: i64) -> i64 {
    if n == 0 {
        1
    } else {
        n
    }
}

/// The release track of each song (None = not found): by disc and track number
/// (when the title or the length agrees), then by title, then, when as many
/// songs as tracks are left, in order.
pub fn match_tracks(songs: &[Song], detail: &ReleaseDetail, by_order: bool) -> Vec<Option<ReleaseTrack>> {
    let mut out: Vec<Option<ReleaseTrack>> = vec![None; songs.len()];
    let mut used: Vec<(i64, i64)> = vec![];
    for (i, s) in songs.iter().enumerate() {
        if s.number() == 0 {
            continue;
        }
        let Some(t) = detail.track_at(or1(s.disc()), s.number()) else { continue };
        if used.contains(&(t.disc, t.number)) {
            continue;
        }
        let title = s.tag("title");
        if !title.is_empty()
            && !same_title(&title, &t.title)
            && !(s.duration != 0.0 && t.length != 0.0 && (s.duration - t.length).abs() <= DURATION_TOLERANCE_S)
        {
            continue;
        }
        used.push((t.disc, t.number));
        out[i] = Some(t.clone());
    }
    for (i, s) in songs.iter().enumerate() {
        let title = s.tag("title");
        if out[i].is_some() || title.is_empty() {
            continue;
        }
        let mut best: Option<((f64, i64, i64), &ReleaseTrack)> = None;
        for t in &detail.tracks {
            if used.contains(&(t.disc, t.number)) || !same_title(&title, &t.title) || !length_ok(s.duration, t.length) {
                continue;
            }
            let diff = if s.duration != 0.0 && t.length != 0.0 { (s.duration - t.length).abs() } else { 0.0 };
            let key = (diff, t.disc, t.number);
            if best.as_ref().is_none_or(|(b, _)| key.partial_cmp(b) == Some(std::cmp::Ordering::Less)) {
                best = Some((key, t));
            }
        }
        if let Some((_, t)) = best {
            used.push((t.disc, t.number));
            out[i] = Some(t.clone());
        }
    }
    let mut left: Vec<usize> = (0..songs.len()).filter(|i| out[*i].is_none()).collect();
    let left_tracks: Vec<&ReleaseTrack> =
        detail.tracks.iter().filter(|t| !used.contains(&(t.disc, t.number))).collect();
    if by_order && !left.is_empty() && left.len() == left_tracks.len() {
        left.sort_by(|a, b| {
            let ka = (or1(songs[*a].disc()), songs[*a].number(), songs[*a].path.as_str());
            let kb = (or1(songs[*b].disc()), songs[*b].number(), songs[*b].path.as_str());
            ka.cmp(&kb)
        });
        for (i, t) in left.into_iter().zip(left_tracks) {
            out[i] = Some(t.clone());
        }
    }
    out
}

/// What MusicBrainz says each field of this track should hold.
pub fn proposed(detail: &ReleaseDetail, t: &ReleaseTrack) -> Vec<(String, String)> {
    let rel = &detail.release;
    vec![
        ("title".into(), t.title.clone()),
        ("artist".into(), t.artist.clone()),
        ("album".into(), rel.title.clone()),
        ("album_artist".into(), rel.artist.clone()),
        ("genre".into(), detail.genre.clone()),
        ("date".into(), detail.date()),
        ("track".into(), if t.track_count != 0 { format!("{}/{}", t.number, t.track_count) } else { t.number.to_string() }),
        ("disc".into(), if t.disc_count > 1 { format!("{}/{}", t.disc, t.disc_count) } else { String::new() }),
        ("composer".into(), t.composer.clone()),
    ]
}

fn same(f: &str, a: &str, b: &str) -> bool {
    if f == "track" || f == "disc" {
        let sa = split_number(a);
        return sa == split_number(b) && !sa.0.is_empty();
    }
    a == b
}

/// The tags to write into `song`: MusicBrainz's values that differ from the
/// song's, for the chosen fields; with `only_empty`, only where its tag is empty.
pub fn changes_for(song: &Song, detail: &ReleaseDetail, t: Option<&ReleaseTrack>, fields: &[&str], only_empty: bool) -> Changes {
    let Some(t) = t else { return vec![] };
    let new = proposed(detail, t);
    let mut out = vec![];
    for f in FIELDS {
        if !fields.contains(&f) {
            continue;
        }
        let v = clean_value(new.iter().find(|(k, _)| k == f).map(|(_, v)| v.as_str()).unwrap_or(""));
        if v.is_empty() {
            continue;
        }
        let cur = song.tag(f);
        if only_empty && !cur.is_empty() {
            continue;
        }
        if same(f, &v, &cur) {
            continue;
        }
        out.push((f.to_string(), v));
    }
    out
}

/// (album, artist) to search for: the album the most songs share, and its album
/// artist (else the artist the most songs share).
pub fn album_of(songs: &[Song]) -> (String, String) {
    let common = |name: &str| -> String {
        let mut counts: Vec<(String, usize)> = vec![];
        for s in songs {
            let v = s.tag(name);
            if v.is_empty() {
                continue;
            }
            match counts.iter_mut().find(|(k, _)| *k == v) {
                Some(c) => c.1 += 1,
                None => counts.push((v, 1)),
            }
        }
        let mut best: Option<&(String, usize)> = None;
        for c in &counts {
            if best.is_none_or(|b| c.1 > b.1) {
                best = Some(c);
            }
        }
        best.map(|b| b.0.clone()).unwrap_or_default()
    };
    let aa = common("album_artist");
    (common("album"), if aa.is_empty() { common("artist") } else { aa })
}

fn date_key(d: &str) -> &str {
    if d.is_empty() {
        "9999"
    } else {
        d
    }
}

/// The releases that are this album, best first.
pub fn rank_releases(releases: &[Release], album: &str, artist: &str, songs: usize) -> Vec<Release> {
    let n = songs as i64;
    let mut out: Vec<Release> = releases
        .iter()
        .filter(|r| {
            same_title(&r.title, album)
                && (artist.is_empty() || same_artist(&r.artist, artist))
                && (r.track_count == 0 || r.track_count >= n)
        })
        .cloned()
        .collect();
    out.sort_by(|a, b| {
        let k = |r: &Release| {
            (
                (r.track_count != n) as u8,
                (r.status != "Official") as u8,
                -r.score,
                date_key(&r.date).to_string(),
                r.id.clone(),
            )
        };
        k(a).cmp(&k(b))
    });
    out
}

fn type_rank(t: &str) -> u8 {
    match t {
        "Album" => 0,
        "EP" => 1,
        "Single" => 2,
        _ => 3,
    }
}

/// How suitable a release is for a single song (smaller is better).
pub fn release_preference(r: &Release) -> (u8, u8, u8, String, String) {
    (
        (r.status != "Official") as u8,
        (!r.secondary_types.is_empty()) as u8,
        type_rank(&r.primary_type),
        date_key(&r.date).to_string(),
        r.id.clone(),
    )
}

/// Whether the automatic lookup should ask about a song.
pub fn needs_lookup(tags: &[(String, String)]) -> bool {
    let has = |f: &str| tags.iter().any(|(k, v)| k == f && !clean_value(v).is_empty());
    has("title") && (has("artist") || has("album_artist")) && !MISSING_FIELDS.iter().all(|f| has(f))
}

/// The release that is certainly these songs' album, and the track of each song.
pub fn find_album(client: &mut MusicBrainzClient, songs: &[Song]) -> Result<(Option<ReleaseDetail>, Vec<Option<ReleaseTrack>>), MbError> {
    let none = vec![None; songs.len()];
    let (album, artist) = album_of(songs);
    if album.is_empty() {
        return Ok((None, none));
    }
    let found = rank_releases(&client.search_releases(&album, &artist)?, &album, &artist, songs.len());
    for r in found.iter().take(AUTO_CANDIDATES) {
        let Some(detail) = client.release(&r.id)? else { continue };
        let m = match_tracks(songs, &detail, false);
        if m.iter().all(Option::is_some) {
            return Ok((Some(detail), m));
        }
    }
    Ok((None, none))
}

/// The recording that is certainly this song, on its most suitable release.
pub fn find_song(client: &mut MusicBrainzClient, song: &Song) -> Result<(Option<ReleaseDetail>, Option<ReleaseTrack>), MbError> {
    let title = song.tag("title");
    let mut artist = song.tag("artist");
    if artist.is_empty() {
        artist = song.tag("album_artist");
    }
    if title.is_empty() || artist.is_empty() {
        return Ok((None, None));
    }
    let mut best: Option<((u8, u8, u8, String, String), Recording, RecordingRelease)> = None;
    for rec in client.search_recordings(&title, &artist, "")? {
        if !same_title(&rec.title, &title) || !same_artist(&rec.artist, &artist) {
            continue;
        }
        if !(song.duration != 0.0 && rec.length != 0.0 && (song.duration - rec.length).abs() <= DURATION_TOLERANCE_S) {
            continue;
        }
        for rr in &rec.releases {
            let key = release_preference(&rr.release);
            if best.as_ref().is_none_or(|(b, _, _)| key < *b) {
                best = Some((key, rec.clone(), rr.clone()));
            }
        }
    }
    let Some((_, rec, rr)) = best else { return Ok((None, None)) };
    let Some(detail) = client.release(&rr.release.id)? else { return Ok((None, None)) };
    if let Some(t) = detail.tracks.iter().find(|t| t.recording_id == rec.id).cloned() {
        return Ok((Some(detail), Some(t)));
    }
    match detail.track_at(rr.disc, rr.number).cloned() {
        Some(t) if same_title(&t.title, &title) => Ok((Some(detail), Some(t))),
        _ => Ok((None, None)),
    }
}

/// The releases the window lists for a search (see the Python docstring).
pub fn search_candidates(
    client: &mut MusicBrainzClient,
    songs: &[Song],
    query_title: &str,
    query_artist: &str,
    album_mode: bool,
) -> Result<Vec<Release>, MbError> {
    if album_mode {
        let mut found = client.search_releases(query_title, query_artist)?;
        let n = songs.len() as i64;
        found.sort_by(|a, b| {
            let k = |r: &Release| {
                (
                    (!same_title(&r.title, query_title)) as u8,
                    if r.track_count == n {
                        0u8
                    } else if r.track_count > n {
                        1
                    } else {
                        2
                    },
                    -r.score,
                    (r.status != "Official") as u8,
                    date_key(&r.date).to_string(),
                    r.id.clone(),
                )
            };
            k(a).cmp(&k(b))
        });
        return Ok(found);
    }
    let dur = songs.first().map_or(0.0, |s| s.duration);
    let mut recs = client.search_recordings(query_title, query_artist, "")?;
    recs.sort_by(|a, b| {
        let k = |r: &Recording| ((!same_title(&r.title, query_title)) as u8, (!length_ok(dur, r.length)) as u8, -r.score);
        k(a).cmp(&k(b))
    });
    let mut out: Vec<Release> = vec![];
    for rec in recs {
        let mut rels = rec.releases.clone();
        rels.sort_by_key(|x| release_preference(&x.release));
        for rr in rels {
            if !out.iter().any(|r| r.id == rr.release.id) {
                out.push(rr.release);
            }
        }
    }
    Ok(out)
}

// --------------------------------------------------------- the lookup log ---

const TABLE: &str = "CREATE TABLE IF NOT EXISTS musicbrainz (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, \
                     status TEXT, release TEXT, time REAL)";

/// Which songs were looked up (table `musicbrainz` of library.sqlite).
pub struct LookupLog {
    db: rusqlite::Connection,
}

fn stat_of(path: &str) -> Option<(i64, i64)> {
    crate::store::stat(path).ok()
}

impl LookupLog {
    pub fn open(path: Option<&std::path::Path>) -> rusqlite::Result<LookupLog> {
        let path = path.map(std::path::Path::to_path_buf).unwrap_or_else(crate::musiclib::default_path);
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let db = rusqlite::Connection::open(&path)?;
        db.busy_timeout(Duration::from_secs(30))?;
        db.execute(TABLE, [])?;
        Ok(LookupLog { db })
    }

    pub fn done(&self, path: &str, size: i64, mtime_ns: i64) -> bool {
        self.db
            .query_row("SELECT size, mtime_ns FROM musicbrainz WHERE path = ?", [path], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?))
            })
            .map(|(s, m)| s == size && m == mtime_ns)
            .unwrap_or(false)
    }

    pub fn status(&self, path: &str) -> String {
        self.db.query_row("SELECT status FROM musicbrainz WHERE path = ?", [path], |r| r.get(0)).unwrap_or_default()
    }

    pub fn mark(&self, path: &str, status: &str, release: &str) {
        let (size, mtime) = stat_of(path).unwrap_or((-1, -1));
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64());
        let _ = self.db.execute(
            "INSERT OR REPLACE INTO musicbrainz VALUES (?, ?, ?, ?, ?, ?)",
            rusqlite::params![path, size, mtime, status, release, now],
        );
    }

    pub fn forget(&self) -> usize {
        self.db.execute("DELETE FROM musicbrainz", []).unwrap_or(0)
    }
}

// ------------------------------------------------------- automatic lookup ---

#[derive(Clone, Debug, Default, PartialEq)]
pub struct AutoOutcome {
    pub path: String,
    pub status: String,
    pub changes: Changes,
    pub release: String,
    pub message: String,
}

/// The tags a library index entry knows, for `needs_lookup()`.
pub fn index_tags(t: &crate::musiclib::Track) -> Vec<(String, String)> {
    let num = |n: i64| if n != 0 { n.to_string() } else { String::new() };
    vec![
        ("title".into(), t.title.clone()),
        ("artist".into(), t.artist.clone()),
        ("album".into(), t.album.clone()),
        ("album_artist".into(), t.album_artist.clone()),
        ("genre".into(), t.genre.clone()),
        ("date".into(), t.year.clone()),
        ("track".into(), num(t.track)),
        ("disc".into(), num(t.disc)),
        ("composer".into(), t.composer.clone()),
    ]
}

/// A song's tags as the file holds them now (ffprobe).
pub fn read_song(path: &str, duration: f64) -> Result<Song, crate::probe::Error> {
    let info = crate::probe::probe(path)?;
    let tags = FIELDS
        .iter()
        .map(|f| (f.to_string(), clean_value(crate::probe::tag(&info.tags, f).unwrap_or(""))))
        .collect();
    let d = info.duration.filter(|d| *d != 0.0).unwrap_or(duration);
    Ok(Song { path: path.to_string(), tags, duration: d })
}

/// The songs as the albums they belong to (folder and album tag; a song without
/// an album tag is a group of its own), in the order first seen.
pub fn groups(songs: &[Song]) -> Vec<Vec<Song>> {
    let mut keys: Vec<(String, String)> = vec![];
    let mut out: Vec<Vec<Song>> = vec![];
    for s in songs {
        let album = normalize_name(&s.tag("album"));
        let key = if album.is_empty() { (String::new(), s.path.clone()) } else { (crate::paths::parent(&s.path), album) };
        match keys.iter().position(|k| *k == key) {
            Some(i) => out[i].push(s.clone()),
            None => {
                keys.push(key);
                out.push(vec![s.clone()]);
            }
        }
    }
    out
}

/// Look one album (or one song without an album tag) up and plan the tags to fill in.
pub fn auto_group(client: &mut MusicBrainzClient, songs: &[Song]) -> Result<Vec<AutoOutcome>, MbError> {
    let (detail, matched) = if songs[0].tag("album").is_empty() {
        let (d, t) = find_song(client, &songs[0])?;
        (d, vec![t])
    } else {
        find_album(client, songs)?
    };
    let mut out = vec![];
    for (s, t) in songs.iter().zip(matched) {
        match (&detail, t) {
            (Some(d), Some(t)) => {
                let ch = changes_for(s, d, Some(&t), &FIELDS, true);
                let status = if ch.is_empty() { COMPLETE } else { FILLED };
                out.push(AutoOutcome {
                    path: s.path.clone(),
                    status: status.into(),
                    changes: ch,
                    release: d.release.id.clone(),
                    message: String::new(),
                });
            }
            _ => out.push(AutoOutcome { path: s.path.clone(), status: NO_MATCH.into(), ..Default::default() }),
        }
    }
    Ok(out)
}

/// Write each song's own changes with `tagedit::save_tags()`.
pub fn save_changes(plans: &[(String, Changes)], cfg: Option<&Settings>, mut progress: impl FnMut(usize, usize, &str)) -> Vec<TagOutcome> {
    let mut out = vec![];
    for (i, (path, changes)) in plans.iter().enumerate() {
        progress(i, plans.len(), path);
        if changes.is_empty() {
            out.push(TagOutcome { path: path.clone(), ok: true, message: String::new(), tags: vec![] });
            continue;
        }
        out.extend(tagedit::save_tags(std::slice::from_ref(path), changes, cfg, |_, _, _| {}));
    }
    progress(plans.len(), plans.len(), "");
    out
}

/// The automatic lookup of these songs (see the Python docstring).  Err only
/// when stopped.
pub fn run_auto(
    client: &mut MusicBrainzClient,
    paths: &[String],
    cfg: Option<&Settings>,
    log: &LookupLog,
    known: &dyn Fn(&str) -> Option<(Vec<(String, String)>, f64)>,
    mut progress: impl FnMut(usize, usize),
) -> Result<Vec<AutoOutcome>, MbError> {
    let mut outcomes = vec![];
    let mut todo = vec![];
    for p in paths {
        let Some((size, mtime)) = stat_of(p) else { continue };
        if log.done(p, size, mtime) {
            continue;
        }
        let hint = known(p);
        let song = match &hint {
            Some((tags, d)) if !needs_lookup(tags) => Song { path: p.clone(), tags: tags.clone(), duration: *d },
            _ => match read_song(p, hint.as_ref().map_or(0.0, |h| h.1)) {
                Ok(s) => s,
                Err(_) => continue,
            },
        };
        if !needs_lookup(&song.tags) {
            let searchable = !song.tag("title").is_empty() && !(song.tag("artist").is_empty() && song.tag("album_artist").is_empty());
            log.mark(p, if searchable { COMPLETE } else { NO_TAGS }, "");
            continue;
        }
        todo.push(song);
    }
    let albums = groups(&todo);
    for (gi, group) in albums.iter().enumerate() {
        progress(gi, albums.len());
        let planned = match auto_group(client, group) {
            Ok(p) => p,
            Err(MbError::Stopped) => return Err(MbError::Stopped),
            Err(MbError::Failed(m)) => {
                for s in group {
                    outcomes.push(AutoOutcome {
                        path: s.path.clone(),
                        status: FAILED.into(),
                        message: m.clone(),
                        ..Default::default()
                    });
                }
                continue;
            }
        };
        for mut o in planned {
            if o.status == FILLED {
                let res = save_changes(&[(o.path.clone(), o.changes.clone())], cfg, |_, _, _| {});
                if let Some(r) = res.first().filter(|r| !r.ok) {
                    o.status = FAILED.into();
                    o.message = r.message.clone();
                }
            }
            if o.status != FAILED {
                log.mark(&o.path, &o.status, &o.release);
            }
            outcomes.push(o);
        }
    }
    progress(albums.len(), albums.len());
    Ok(outcomes)
}

/// One sentence for the status line after an automatic lookup.
pub fn auto_summary(outcomes: &[AutoOutcome]) -> String {
    if outcomes.is_empty() {
        return "Every song already has its information.".into();
    }
    let filled = outcomes.iter().filter(|o| o.status == FILLED).count() as i64;
    let failed: Vec<&AutoOutcome> = outcomes.iter().filter(|o| o.status == FAILED).collect();
    let mut text = if filled > 0 {
        format!(
            "MusicBrainz filled in the information of {} song{}.",
            crate::stats::thousands(filled),
            if filled != 1 { "s" } else { "" }
        )
    } else {
        "MusicBrainz had nothing to add.".to_string()
    };
    if !failed.is_empty() {
        text.push_str(&format!(" {} could not be looked up: {}", crate::stats::thousands(failed.len() as i64), failed[0].message));
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    #[test]
    fn client_keeps_one_request_per_second_and_retries_503() {
        let slept: Arc<Mutex<Vec<f64>>> = Arc::new(Mutex::new(vec![]));
        let now = Arc::new(Mutex::new(100.0f64));
        let (n2, s2) = (now.clone(), slept.clone());
        let answers = Arc::new(Mutex::new(vec![
            (503u16, vec![("retry-after".to_string(), "4".to_string())], vec![]),
            (503, vec![], vec![]),
            (200, vec![], b"{\"releases\": []}".to_vec()),
            (200, vec![], b"{\"releases\": []}".to_vec()),
        ]));
        let a2 = answers.clone();
        let agents = Arc::new(Mutex::new(vec![]));
        let ag2 = agents.clone();
        let mut c = MusicBrainzClient::new(DEFAULT_SERVER)
            .with_transport(Box::new(move |_u: &str, h: &[(&str, &str)], _t| {
                ag2.lock().unwrap().push(h.iter().find(|(k, _)| *k == "User-Agent").unwrap().1.to_string());
                Ok(a2.lock().unwrap().remove(0))
            }))
            .with_clock(move || *n2.lock().unwrap(), {
                let n3 = now.clone();
                move |s| {
                    s2.lock().unwrap().push(s);
                    *n3.lock().unwrap() += s;
                }
            });
        assert_eq!(c.min_interval, 1.0);
        assert!(c.search_releases("x", "").unwrap().is_empty());
        assert!(c.search_releases("x", "").unwrap().is_empty());
        let s = slept.lock().unwrap().clone();
        assert_eq!(s[0], 4.0);
        assert!(s[1] >= 1.0 && s[2] >= 0.99);
        assert_eq!(c.requests, 4);
        assert!(agents.lock().unwrap()[0].starts_with("UniversalMusicPlayer/"));
        let mut bad = MusicBrainzClient::new("http://127.0.0.1:1")
            .with_min_interval(0.0)
            .with_transport(Box::new(|_u: &str, _h: &[(&str, &str)], _t| Ok((400, vec![], b"{\"error\": \"Invalid query\"}".to_vec()))));
        assert_eq!(
            bad.search_releases("x", "").unwrap_err(),
            MbError::Failed("127.0.0.1:1 refused the request (HTTP 400: Invalid query).".into())
        );
        let stop = Arc::new(AtomicBool::new(true));
        let mut st = MusicBrainzClient::new(DEFAULT_SERVER).with_stop(stop);
        assert_eq!(st.search_releases("x", "").unwrap_err(), MbError::Stopped);
    }
}
