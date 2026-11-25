//! Where a song was played (2.4): the port of `realhires/places.py`, which
//! explains it in full.
//!
//! With the setting `play_location` on (off until the user turns it on), the
//! player asks a free IP geolocation service (FreeIPAPI's free API by default,
//! the setting `location_server`) for this computer's approximate place when it
//! starts and again at most every 2 hours while music plays, and stores the
//! place with each play in `library.sqlite`.  Only that one HTTPS GET goes out,
//! with the app's name in the User-Agent: the service sees the IP address, never
//! a song, path or tag.  `GeoClient` sends one request at a time, keeps a
//! 429/503 answer's Retry-After (and gives up at once), tries server and network
//! errors `MAX_ATTEMPTS` times in all, and a failure only leaves the place empty;
//! `PlaceCache` decides when a lookup is due.  `tests/python_places.json` holds
//! Python's answers, so both versions parse, group and schedule alike.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use crate::json::{self, Obj, Value};
use crate::lyrics::{self, http_transport, Transport, TransportError};
use crate::pyunicode::{casefold, is_space, strip};
use crate::stats::{self, Play};

pub const DEFAULT_SERVER: &str = "https://free.freeipapi.com/api/v1/json/";
pub const SERVICE_NAME: &str = "FreeIPAPI";
pub const TERMS_URL: &str = "https://freeipapi.com/terms";
/// between request starts (FreeIPAPI allows 10 requests in 10 s)
pub const MIN_INTERVAL_S: f64 = 2.0;
pub const TIMEOUT_S: f64 = 15.0;
/// per lookup, for server errors (5xx) and network errors
pub const MAX_ATTEMPTS: usize = 3;
/// the first back-off, then twice as long
pub const BACKOFF_S: f64 = 2.0;
/// a 429/503 answer without Retry-After: no request for 10 minutes
pub const DEFAULT_RETRY_AFTER_S: f64 = 600.0;
pub const MAX_RETRY_AFTER_S: f64 = 6.0 * 3600.0;
/// a new lookup at most every 2 hours, and only while music plays
pub const REFRESH_S: f64 = 2.0 * 3600.0;
/// characters kept of a city, region or country name
pub const MAX_NAME: usize = 100;
/// tests only; never below MIN_INTERVAL_S for freeipapi.com
pub const INTERVAL_ENV: &str = "REALHIRES_LOCATION_INTERVAL";

const CITY_KEYS: [&str; 2] = ["cityName", "city"];
const REGION_KEYS: [&str; 2] = ["regionName", "region"];
const COUNTRY_KEYS: [&str; 3] = ["countryName", "country_name", "country"];
const CODE_KEYS: [&str; 2] = ["countryCode", "country_code"];
const LATITUDE_KEYS: [&str; 2] = ["latitude", "lat"];
const LONGITUDE_KEYS: [&str; 3] = ["longitude", "lon", "lng"];

#[derive(Clone, Debug, PartialEq)]
pub enum PlaceError {
    /// A lookup failed (a full sentence).
    Failed(String),
    /// The app is closing.
    Stopped,
}

impl std::fmt::Display for PlaceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PlaceError::Failed(m) => f.write_str(m),
            PlaceError::Stopped => f.write_str("stopped"),
        }
    }
}

/// The app's name, version and home page, as for MusicBrainz.
pub fn user_agent() -> String {
    format!("UniversalMusicPlayer/{} ( {} )", crate::VERSION, crate::PROJECT_URL)
}

pub fn normalize_server(server: &str) -> String {
    let s = strip(server);
    let s = if s.is_empty() { DEFAULT_SERVER } else { s };
    if s.contains("://") {
        s.to_string()
    } else {
        format!("https://{s}")
    }
}

pub fn host_of(server: &str) -> String {
    let s = normalize_server(server);
    let n = lyrics::netloc(&s);
    if n.is_empty() {
        s
    } else {
        n.to_string()
    }
}

pub fn is_official(server: &str) -> bool {
    let host = lyrics::hostname(&normalize_server(server));
    let host = host.trim_end_matches('.');
    host == "freeipapi.com" || host.ends_with(".freeipapi.com")
}

pub fn default_interval(server: &str) -> f64 {
    let value = std::env::var(INTERVAL_ENV).ok().and_then(|v| strip(&v).parse::<f64>().ok()).unwrap_or(MIN_INTERVAL_S);
    let value = if value.is_nan() { MIN_INTERVAL_S } else { value.max(0.0) };
    if is_official(server) {
        value.max(MIN_INTERVAL_S)
    } else {
        value
    }
}

// -------------------------------------------------------------------- places ---

/// An approximate place: names as the service gives them, and the latitude and
/// longitude rounded to 2 decimals (about 1 km), None when unknown.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Place {
    pub city: String,
    pub region: String,
    pub country: String,
    pub country_code: String,
    pub latitude: Option<f64>,
    pub longitude: Option<f64>,
}

impl Place {
    pub fn key(&self) -> String {
        place_key(Some(self))
    }

    pub fn to_json(&self) -> Value {
        let num = |v: Option<f64>| v.map(Value::Float).unwrap_or(Value::Null);
        Obj::new()
            .put("city", self.city.as_str())
            .put("region", self.region.as_str())
            .put("country", self.country.as_str())
            .put("country_code", self.country_code.as_str())
            .put("latitude", num(self.latitude))
            .put("longitude", num(self.longitude))
            .build()
    }

    pub fn from_json(v: &Value) -> Option<Place> {
        let s = |k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        make_place(&s("city"), &s("region"), &s("country"), &s("country_code"), v.get("latitude"), v.get("longitude"))
    }
}

/// A name from the answer: control characters and runs of white space made one
/// space, at most MAX_NAME characters (Python `clean_name()`).
pub fn clean_name(text: &str) -> String {
    let spaced: String =
        text.chars().map(|c| if is_space(c) || (c as u32) < 32 || c as u32 == 127 { ' ' } else { c }).collect();
    let joined = spaced.split(' ').filter(|p| !p.is_empty()).collect::<Vec<_>>().join(" ");
    let cut: String = joined.chars().take(MAX_NAME).collect();
    strip(&cut).to_string()
}

fn name_of(v: Option<&Value>) -> String {
    v.and_then(Value::as_str).map(clean_name).unwrap_or_default()
}

/// A country code: two ASCII letters, upper case ("" otherwise).
pub fn clean_code(text: &str) -> String {
    let code = clean_name(text);
    if code.chars().count() == 2 && code.chars().all(|c| c.is_ascii_alphabetic()) {
        code.to_ascii_uppercase()
    } else {
        String::new()
    }
}

/// A latitude (limit 90) or longitude (limit 180) from a JSON number, rounded to
/// 2 decimals; None for anything else or out of range.
pub fn coordinate(v: Option<&Value>, limit: f64) -> Option<f64> {
    let x = match v? {
        Value::Int(i) => *i as f64,
        Value::Float(f) => *f,
        _ => return None,
    };
    coordinate_f64(x, limit)
}

pub fn coordinate_f64(x: f64, limit: f64) -> Option<f64> {
    if !x.is_finite() || x.abs() > limit {
        return None;
    }
    let r = crate::pyfmt::round_to(x, 2);
    Some(if r == 0.0 { 0.0 } else { r })
}

/// A Place from stored or parsed values; None without a city, region or country.
pub fn make_place(
    city: &str,
    region: &str,
    country: &str,
    code: &str,
    latitude: Option<&Value>,
    longitude: Option<&Value>,
) -> Option<Place> {
    let p = Place {
        city: clean_name(city),
        region: clean_name(region),
        country: clean_name(country),
        country_code: clean_code(code),
        latitude: coordinate(latitude, 90.0),
        longitude: coordinate(longitude, 180.0),
    };
    (!p.city.is_empty() || !p.region.is_empty() || !p.country.is_empty()).then_some(p)
}

fn first<'a>(data: &'a Value, keys: &[&str]) -> Option<&'a Value> {
    keys.iter().find_map(|k| data.get(k).filter(|v| !v.is_null()))
}

/// The place in a service's JSON answer.
pub fn parse_place(data: &Value) -> Result<Place, PlaceError> {
    let none = || PlaceError::Failed("The location service sent no place.".into());
    if !matches!(data, Value::Object(_)) {
        return Err(none());
    }
    if data.get("success") == Some(&Value::Bool(false)) {
        let msg = name_of(data.get("message"));
        return Err(PlaceError::Failed(if msg.is_empty() {
            "The location service could not find this computer's place.".into()
        } else {
            format!("The location service could not find this computer's place: {msg}.")
        }));
    }
    let p = Place {
        city: name_of(first(data, &CITY_KEYS)),
        region: name_of(first(data, &REGION_KEYS)),
        country: name_of(first(data, &COUNTRY_KEYS)),
        country_code: clean_code(first(data, &CODE_KEYS).and_then(Value::as_str).unwrap_or("")),
        latitude: coordinate(first(data, &LATITUDE_KEYS), 90.0),
        longitude: coordinate(first(data, &LONGITUDE_KEYS), 180.0),
    };
    if p.city.is_empty() && p.region.is_empty() && p.country.is_empty() {
        return Err(none());
    }
    Ok(p)
}

/// "Tokyo, Japan" for display: the city, region and country, each once.
pub fn place_text(place: Option<&Place>) -> String {
    let Some(p) = place else { return String::new() };
    let mut out: Vec<&str> = vec![];
    let mut seen: Vec<String> = vec![];
    for part in [&p.city, &p.region, &p.country] {
        let k = casefold(part);
        if !part.is_empty() && !seen.contains(&k) {
            seen.push(k);
            out.push(part);
        }
    }
    out.join(", ")
}

/// What makes two places the same place (the names, in any case); "" for none.
pub fn place_key(place: Option<&Place>) -> String {
    match place {
        None => String::new(),
        Some(p) => [&p.city, &p.region, &p.country].iter().map(|s| casefold(s)).collect::<Vec<_>>().join("\u{0}"),
    }
}

// -------------------------------------------------------------------- client ---

/// One lookup at a time (one client for the app, used from one thread at a time).
pub struct GeoClient {
    pub server: String,
    stop: Option<Arc<AtomicBool>>,
    transport: Transport,
    pub min_interval: f64,
    pub timeout: f64,
    clock: Box<dyn Fn() -> f64 + Send>,
    sleep: Box<dyn FnMut(f64) + Send>,
    last_start: Option<f64>,
    /// no request before this time (Retry-After)
    pub not_before: f64,
    pub requests: usize,
}

impl GeoClient {
    pub fn new(server: &str) -> Self {
        let server = normalize_server(server);
        let min_interval = default_interval(&server);
        GeoClient {
            server,
            stop: None,
            transport: http_transport(),
            min_interval,
            timeout: TIMEOUT_S,
            clock: Box::new(lyrics::monotonic),
            sleep: Box::new(|s| std::thread::sleep(Duration::from_secs_f64(s))),
            last_start: None,
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

    pub fn with_clock(mut self, clock: impl Fn() -> f64 + Send + 'static, sleep: impl FnMut(f64) + Send + 'static) -> Self {
        self.clock = Box::new(clock);
        self.sleep = Box::new(sleep);
        self
    }

    pub fn with_min_interval(mut self, v: f64) -> Self {
        self.min_interval = v;
        self
    }

    /// The time of the client's clock (the monotonic clock unless a test set one).
    pub fn now(&self) -> f64 {
        (self.clock)()
    }

    pub fn host(&self) -> String {
        host_of(&self.server)
    }

    fn stopped(&self) -> bool {
        self.stop.as_ref().is_some_and(|s| s.load(Ordering::Relaxed))
    }

    fn wait(&mut self, seconds: f64) -> Result<(), PlaceError> {
        if !(seconds > 0.0) {
            return Ok(());
        }
        if self.stop.is_some() {
            let end = (self.clock)() + seconds;
            loop {
                if self.stopped() {
                    return Err(PlaceError::Stopped);
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

    fn throttle(&mut self) -> Result<(), PlaceError> {
        if self.stopped() {
            return Err(PlaceError::Stopped);
        }
        let mut due = self.not_before;
        if let Some(s) = self.last_start {
            due = due.max(s + self.min_interval);
        }
        let now = (self.clock)();
        self.wait(due - now)
    }

    /// This computer's approximate place.
    pub fn lookup(&mut self) -> Result<Place, PlaceError> {
        let ua = user_agent();
        let headers = [("User-Agent", ua.as_str()), ("Accept", "application/json")];
        let host = self.host();
        if self.stopped() {
            return Err(PlaceError::Stopped);
        }
        let wait_text = || PlaceError::Failed(format!("{host} asked to wait before the next lookup."));
        if (self.clock)() < self.not_before {
            return Err(wait_text());
        }
        let mut backoff = BACKOFF_S;
        let mut problem = String::new();
        for attempt in 1..=MAX_ATTEMPTS {
            self.throttle()?;
            self.last_start = Some((self.clock)());
            self.requests += 1;
            let server = self.server.clone();
            match (self.transport)(&server, &headers, self.timeout) {
                Err(TransportError::Invalid(e)) => {
                    return Err(PlaceError::Failed(format!(
                        "The location server address {} is not valid: {e}.",
                        lyrics::repr(&self.server)
                    )))
                }
                Err(TransportError::Certificate(e)) => {
                    return Err(PlaceError::Failed(format!(
                        "Could not connect to {host} securely: the certificate of the server could not be verified \
                         ({e})."
                    )))
                }
                Err(TransportError::Network(e)) => problem = format!("could not reach {host}: {e}"),
                Ok((status, hdrs, body)) => {
                    if status == 200 {
                        let bad = || PlaceError::Failed(format!("{host} sent an answer that could not be read."));
                        let text = std::str::from_utf8(&body).map_err(|_| bad())?;
                        let v = json::parse(text).map_err(|_| bad())?;
                        return parse_place(&v);
                    }
                    if status == 429 || status == 503 {
                        self.not_before = (self.clock)() + retry_after(&hdrs);
                        return Err(wait_text());
                    }
                    if status < 500 {
                        return Err(PlaceError::Failed(format!("{host} refused the request (HTTP {status}).")));
                    }
                    problem = format!("server error (HTTP {status})");
                }
            }
            if attempt < MAX_ATTEMPTS {
                self.wait(backoff)?;
                backoff *= 2.0;
            }
        }
        Err(PlaceError::Failed(format!("No answer from {host} after {MAX_ATTEMPTS} attempts ({problem}).")))
    }
}

/// The seconds a 429/503 answer asks to wait (Retry-After in seconds; else
/// DEFAULT_RETRY_AFTER_S), at most MAX_RETRY_AFTER_S.
pub fn retry_after(headers: &[(String, String)]) -> f64 {
    let v = headers.iter().find(|(k, _)| k == "retry-after").map(|(_, v)| strip(v).parse::<f64>());
    match v {
        Some(Ok(x)) if !x.is_nan() => x.clamp(0.0, MAX_RETRY_AFTER_S),
        _ => DEFAULT_RETRY_AFTER_S,
    }
}

/// (place, error text) of one lookup; `Stopped` gives (None, "").
pub fn lookup_now(client: &mut GeoClient) -> (Option<Place>, String) {
    match client.lookup() {
        Ok(p) => (Some(p), String::new()),
        Err(PlaceError::Failed(e)) => (None, e),
        Err(PlaceError::Stopped) => (None, String::new()),
    }
}

// ---------------------------------------------------------------- when to ask ---

/// The place the plays are stored with, and when to look it up again (Python
/// `PlaceCache`): at once the first time, then only while music plays and
/// REFRESH_S after the last lookup began, never before a Retry-After.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct PlaceCache {
    pub place: Option<Place>,
    /// when the last lookup began (monotonic)
    pub last: Option<f64>,
    pub busy: bool,
    pub error: String,
    /// wall time of the last lookup that found the place
    pub looked_up: Option<f64>,
}

impl PlaceCache {
    pub fn reset(&mut self) {
        *self = PlaceCache::default();
    }

    pub fn due(&self, now: f64, playing: bool, not_before: f64) -> bool {
        if self.busy || now < not_before {
            return false;
        }
        match self.last {
            None => true,
            Some(last) => playing && now - last >= REFRESH_S,
        }
    }

    pub fn begin(&mut self, now: f64) {
        self.busy = true;
        self.last = Some(now);
    }

    pub fn finish(&mut self, place: Option<Place>, error: &str, wall: f64) {
        self.busy = false;
        self.error = if place.is_some() {
            String::new()
        } else if error.is_empty() {
            "The location service sent no place.".into()
        } else {
            error.to_string()
        };
        if place.is_some() {
            self.looked_up = Some(wall);
        }
        self.place = place;
    }
}

// --------------------------------------------------------------- statistics ---

/// One place of a period (Python `period_places()` rows).
#[derive(Clone, Debug, PartialEq)]
pub struct PlaceRow {
    pub key: String,
    pub name: String,
    pub place: Place,
    pub plays: i64,
    pub logged: i64,
    pub seconds: f64,
    pub songs: usize,
    pub first: f64,
    pub last: f64,
}

impl PlaceRow {
    pub fn to_json(&self) -> Value {
        let num = |v: Option<f64>| v.map(Value::Float).unwrap_or(Value::Null);
        Obj::new()
            .put("key", self.key.as_str())
            .put("name", self.name.as_str())
            .put("city", self.place.city.as_str())
            .put("region", self.place.region.as_str())
            .put("country", self.place.country.as_str())
            .put("country_code", self.place.country_code.as_str())
            .put("latitude", num(self.place.latitude))
            .put("longitude", num(self.place.longitude))
            .put("plays", self.plays)
            .put("logged", self.logged)
            .put("seconds", self.seconds)
            .put("songs", self.songs as i64)
            .put("first", self.first)
            .put("last", self.last)
            .build()
    }
}

/// The places of these plays, most played first (at most `stats::TOP`); plays
/// without a place are left out.
pub fn period_places(plays: &[Play]) -> Vec<PlaceRow> {
    let mut rows: Vec<PlaceRow> = vec![];
    let mut songs: Vec<Vec<&str>> = vec![];
    for p in plays {
        let Some(place) = p.place.as_ref() else { continue };
        let key = place_key(Some(place));
        let i = match rows.iter().position(|r| r.key == key) {
            Some(i) => i,
            None => {
                rows.push(PlaceRow {
                    key,
                    name: place_text(Some(place)),
                    place: place.clone(),
                    plays: 0,
                    logged: 0,
                    seconds: 0.0,
                    songs: 0,
                    first: p.started,
                    last: p.started,
                });
                songs.push(vec![]);
                rows.len() - 1
            }
        };
        let r = &mut rows[i];
        r.plays += stats::counted(p) as i64;
        r.logged += 1;
        r.seconds += p.seconds;
        r.first = r.first.min(p.started);
        r.last = r.last.max(p.started);
        if !songs[i].contains(&p.path.as_str()) {
            songs[i].push(p.path.as_str());
        }
    }
    for (r, s) in rows.iter_mut().zip(&songs) {
        r.songs = s.len();
    }
    rows.sort_by(|a, b| {
        b.plays
            .cmp(&a.plays)
            .then(b.seconds.partial_cmp(&a.seconds).unwrap_or(std::cmp::Ordering::Equal))
            .then_with(|| casefold(&a.name).cmp(&casefold(&b.name)))
            .then_with(|| a.key.cmp(&b.key))
    });
    rows.truncate(stats::TOP);
    rows
}

/// A song played at a place (Python `place_songs()` rows).
#[derive(Clone, Debug, PartialEq)]
pub struct PlaceSong {
    pub path: String,
    pub title: String,
    pub artist: String,
    pub plays: i64,
    pub logged: i64,
    pub seconds: f64,
    pub last: f64,
}

impl PlaceSong {
    pub fn to_json(&self) -> Value {
        Obj::new()
            .put("path", self.path.as_str())
            .put("title", self.title.as_str())
            .put("artist", self.artist.as_str())
            .put("plays", self.plays)
            .put("logged", self.logged)
            .put("seconds", self.seconds)
            .put("last", self.last)
            .build()
    }
}

/// The songs played at the place `key`, most played first.
pub fn place_songs(plays: &[Play], key: &str) -> Vec<PlaceSong> {
    let mut rows: Vec<PlaceSong> = vec![];
    if key.is_empty() {
        return rows;
    }
    for p in plays {
        if place_key(p.place.as_ref()) != key {
            continue;
        }
        let i = match rows.iter().position(|r| r.path == p.path) {
            Some(i) => i,
            None => {
                let title = strip(&p.title);
                let artist = [strip(&p.artist), strip(&p.album_artist)].into_iter().find(|a| !a.is_empty());
                rows.push(PlaceSong {
                    path: p.path.clone(),
                    title: if title.is_empty() { crate::paths::stem(&p.path) } else { title.to_string() },
                    artist: artist.unwrap_or("Unknown Artist").to_string(),
                    plays: 0,
                    logged: 0,
                    seconds: 0.0,
                    last: p.started,
                });
                rows.len() - 1
            }
        };
        let r = &mut rows[i];
        r.plays += stats::counted(p) as i64;
        r.logged += 1;
        r.seconds += p.seconds;
        r.last = r.last.max(p.started);
    }
    rows.sort_by(|a, b| {
        b.plays
            .cmp(&a.plays)
            .then(b.seconds.partial_cmp(&a.seconds).unwrap_or(std::cmp::Ordering::Equal))
            .then_with(|| casefold(&a.title).cmp(&casefold(&b.title)))
            .then_with(|| a.path.cmp(&b.path))
    });
    rows
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn texts_and_coordinates() {
        let p = Place { city: "Tokyo".into(), region: "tokyo".into(), country: "Japan".into(), ..Default::default() };
        assert_eq!(place_text(Some(&p)), "Tokyo, Japan");
        assert_eq!(coordinate_f64(-0.004, 180.0), Some(0.0));
        assert_eq!(coordinate_f64(91.0, 90.0), None);
        assert_eq!(clean_name("  a\t\u{0}b  "), "a b");
        assert_eq!(normalize_server(" "), DEFAULT_SERVER);
        assert!(is_official(DEFAULT_SERVER) && !is_official("http://127.0.0.1:1/"));
    }
}
