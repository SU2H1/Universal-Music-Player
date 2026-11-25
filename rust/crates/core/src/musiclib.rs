//! The music library of the player (2.0), the counterpart of
//! `realhires/musiclib.py`: the same `library.sqlite` (path, size, mtime_ns,
//! the track as JSON), so both versions share the index.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use rusqlite::{params, Connection};

use crate::json::{self, Obj as ObjectBuilder, Value};
use crate::paths::{abspath, file_name, parent};
use crate::playqueue::{number, Song};
use crate::probe::{probe, tag, AudioInfo};
use crate::pyunicode::casefold;
use crate::settings::Settings;

pub const LIBRARY_ENV: &str = "REALHIRES_LIBRARY";
pub const DB_NAME: &str = "library.sqlite";
/// 2: composer tags (older indexes are read again once)
pub const LIBRARY_VERSION: i64 = 2;
/// Shorter listening is not written to the play history.
pub const MIN_LOGGED_SECONDS: f64 = 1.0;

pub fn default_path() -> PathBuf {
    std::env::var_os(LIBRARY_ENV)
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::store::data_dir().join(DB_NAME))
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Track {
    pub path: String,
    pub title: String,
    pub artist: String,
    pub album: String,
    pub album_artist: String,
    pub genre: String,
    pub composer: String,
    pub disc: i64,
    pub track: i64,
    pub year: String,
    pub duration: f64,
    pub codec: String,
    pub sample_rate: i64,
    pub bits: i64,
    pub is_float: bool,
    pub is_lossy: bool,
    pub channels: i64,
    pub bit_rate: i64,
    pub size: i64,
    pub mtime_ns: i64,
}

impl Track {
    pub fn display_title(&self) -> String {
        if self.title.is_empty() {
            crate::paths::stem(&self.path)
        } else {
            self.title.clone()
        }
    }

    pub fn display_artist(&self) -> String {
        if !self.artist.is_empty() {
            self.artist.clone()
        } else if !self.album_artist.is_empty() {
            self.album_artist.clone()
        } else {
            "Unknown Artist".into()
        }
    }

    pub fn display_album(&self) -> String {
        if !self.album.is_empty() {
            return self.album.clone();
        }
        let folder = file_name(&parent(&self.path));
        if folder.is_empty() {
            "Unknown Album".into()
        } else {
            folder
        }
    }

    pub fn song(&self) -> Song {
        Song {
            path: self.path.clone(),
            title: self.display_title(),
            artist: self.artist.clone(),
            album: self.album.clone(),
            album_artist: self.album_artist.clone(),
            genre: self.genre.clone(),
            disc: self.disc,
            track: self.track,
        }
    }

    pub fn to_json(&self) -> Value {
        ObjectBuilder::new()
            .put("album", self.album.as_str())
            .put("album_artist", self.album_artist.as_str())
            .put("artist", self.artist.as_str())
            .put("bit_rate", self.bit_rate)
            .put("bits", self.bits)
            .put("channels", self.channels)
            .put("codec", self.codec.as_str())
            .put("composer", self.composer.as_str())
            .put("disc", self.disc)
            .put("duration", self.duration)
            .put("genre", self.genre.as_str())
            .put("is_float", self.is_float)
            .put("is_lossy", self.is_lossy)
            .put("mtime_ns", self.mtime_ns)
            .put("path", self.path.as_str())
            .put("sample_rate", self.sample_rate)
            .put("size", self.size)
            .put("title", self.title.as_str())
            .put("track", self.track)
            .put("year", self.year.as_str())
            .build()
    }

    pub fn from_json(v: &Value) -> Option<Track> {
        let s = |k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let i = |k: &str| v.get(k).and_then(Value::as_i64).unwrap_or(0);
        let b = |k: &str| v.get(k).is_some_and(Value::truthy);
        Some(Track {
            path: v.get("path")?.as_str()?.to_string(),
            title: s("title"),
            artist: s("artist"),
            album: s("album"),
            album_artist: s("album_artist"),
            genre: s("genre"),
            composer: s("composer"),
            disc: i("disc"),
            track: i("track"),
            year: s("year"),
            duration: v.get("duration").and_then(Value::as_f64).unwrap_or(0.0),
            codec: s("codec"),
            sample_rate: i("sample_rate"),
            bits: i("bits"),
            is_float: b("is_float"),
            is_lossy: b("is_lossy"),
            channels: i("channels"),
            bit_rate: i("bit_rate"),
            size: i("size"),
            mtime_ns: i("mtime_ns"),
        })
    }
}

/// "Hi-Res Lossless", "Lossless" or the codec with its bit rate.
pub fn quality(t: &Track) -> String {
    if t.is_lossy {
        let kbps = if t.bit_rate > 0 {
            format!(" {} kbps", crate::pyfmt::round_int(t.bit_rate as f64 / 1000.0))
        } else {
            String::new()
        };
        return format!("{}{kbps}", t.codec.to_uppercase());
    }
    let bits = if t.bits == 0 { 16 } else { t.bits };
    if t.sample_rate > 48000 || bits > 16 || t.is_float {
        "Hi-Res Lossless".into()
    } else {
        "Lossless".into()
    }
}

/// "24-bit/96 kHz"
pub fn resolution(t: &Track) -> String {
    if t.sample_rate == 0 {
        return String::new();
    }
    let khz = format!("{} kHz", crate::pyfmt::g(t.sample_rate as f64 / 1000.0));
    if t.is_lossy || t.bits == 0 {
        return khz;
    }
    format!("{}-bit{}/{khz}", t.bits, if t.is_float { " float" } else { "" })
}

/// The tooltip of the song tables' Quality column (which shows `quality()`):
/// the resolution ("24-bit/96 kHz"), and for a lossy song its codec, bit rate
/// and sample rate ("MP3 · 320 kbps · 44.1 kHz") (Python `quality_detail`).
pub fn quality_detail(t: &Track) -> String {
    if t.is_lossy {
        let mut parts = vec![t.codec.to_uppercase()];
        if t.bit_rate > 0 {
            parts.push(format!("{} kbps", crate::pyfmt::round_int(t.bit_rate as f64 / 1000.0)));
        }
        if t.sample_rate > 0 {
            parts.push(format!("{} kHz", crate::pyfmt::g(t.sample_rate as f64 / 1000.0)));
        }
        return parts.into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join(" · ");
    }
    let r = resolution(t);
    if r.is_empty() {
        quality(t)
    } else {
        r
    }
}

/// What a click on the Quality heading sorts by: the bit depth (lossy songs,
/// which have none, first), then the sample rate (then integer before float,
/// then the bit rate) (Python `quality_sort`).
pub fn quality_sort(t: &Track) -> (i64, i64, bool, i64) {
    (t.bits, t.sample_rate, t.is_float, t.bit_rate)
}

pub fn track_from_info(info: &AudioInfo, size: i64, mtime_ns: i64) -> Track {
    let t = |k: &str| tag(&info.tags, k).unwrap_or("").to_string();
    let bits =
        if info.is_lossy { 0 } else { info.claimed_bits.map(i64::from).unwrap_or(if info.is_float { 32 } else { 0 }) };
    Track {
        path: info.path.clone(),
        title: t("title"),
        artist: t("artist"),
        album: t("album"),
        album_artist: t("album_artist"),
        genre: t("genre"),
        composer: t("composer"),
        disc: number(&t("disc")),
        track: number(&t("track")),
        year: t("date").chars().take(4).collect(),
        duration: info.duration.unwrap_or(0.0),
        codec: info.codec.clone(),
        sample_rate: info.sample_rate as i64,
        bits,
        is_float: info.is_float,
        is_lossy: info.is_lossy,
        channels: info.channels as i64,
        bit_rate: info.bit_rate.unwrap_or(0),
        size,
        mtime_ns,
    }
}

const PLAYS_TABLE: &str = "CREATE TABLE IF NOT EXISTS plays (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, \
     started REAL, seconds REAL, duration REAL, title TEXT, artist TEXT, album TEXT, album_artist TEXT, genre TEXT, \
     year TEXT, composer TEXT);";

/// 2.4: where each song was played (`places`); older databases get the columns, their plays none.
pub const PLACE_COLUMNS: [&str; 6] =
    ["place_city", "place_region", "place_country", "place_country_code", "place_latitude", "place_longitude"];
const PLACE_TYPES: [&str; 6] = ["TEXT", "TEXT", "TEXT", "TEXT", "REAL", "REAL"];

fn add_place_columns(db: &Connection) -> rusqlite::Result<()> {
    let have: Vec<String> = {
        let mut st = db.prepare("PRAGMA table_info(plays)")?;
        let rows = st.query_map([], |r| r.get::<_, String>(1))?;
        rows.filter_map(Result::ok).collect()
    };
    for (name, kind) in PLACE_COLUMNS.iter().zip(PLACE_TYPES) {
        if !have.iter().any(|h| h == name) {
            db.execute(&format!("ALTER TABLE plays ADD COLUMN {name} {kind}"), [])?;
        }
    }
    Ok(())
}

fn stat(path: &str) -> Option<(i64, i64)> {
    crate::store::stat(path).ok()
}

pub struct Library {
    pub path: PathBuf,
    db: Connection,
}

impl Library {
    pub fn open(path: Option<&Path>) -> rusqlite::Result<Library> {
        let path = path.map(Path::to_path_buf).unwrap_or_else(default_path);
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let db = Connection::open(&path)?;
        db.busy_timeout(std::time::Duration::from_secs(30))?;
        db.execute_batch(
            "CREATE TABLE IF NOT EXISTS tracks (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, data TEXT);
             CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);",
        )?;
        db.execute_batch(PLAYS_TABLE)?;
        add_place_columns(&db)?; // 2.4
        let version: Option<String> =
            db.query_row("SELECT value FROM meta WHERE key = 'version'", [], |r| r.get(0)).ok();
        if version.is_some_and(|v| v.trim().parse::<i64>().unwrap_or(0) < LIBRARY_VERSION) {
            db.execute("DELETE FROM tracks", [])?; // read every file again for the new tags
        }
        db.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?1)", params![LIBRARY_VERSION.to_string()])?;
        Ok(Library { path, db })
    }

    /// Log that `t` was listened to for `seconds` from `started` (Unix time),
    /// with a copy of its tags (Python `Library.add_play`), without a place.
    pub fn add_play(&self, t: &Track, started: f64, seconds: f64) -> bool {
        self.add_play_at(t, started, seconds, None)
    }

    /// `add_play()` with the place it was played at (2.4; Python's `place=`).
    pub fn add_play_at(&self, t: &Track, started: f64, seconds: f64, place: Option<&crate::places::Place>) -> bool {
        if seconds < MIN_LOGGED_SECONDS {
            return false;
        }
        let text = |f: fn(&crate::places::Place) -> &String| place.map(|p| f(p).clone());
        self.db
            .execute(
                &format!(
                    "INSERT INTO plays (path, started, seconds, duration, title, artist, album, album_artist, genre, \
                     year, composer, {}) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, \
                     ?16, ?17)",
                    PLACE_COLUMNS.join(", ")
                ),
                params![
                    t.path,
                    started,
                    seconds,
                    t.duration,
                    t.display_title(),
                    t.artist,
                    t.album,
                    t.album_artist,
                    t.genre,
                    t.year,
                    t.composer,
                    text(|p| &p.city),
                    text(|p| &p.region),
                    text(|p| &p.country),
                    text(|p| &p.country_code),
                    place.and_then(|p| p.latitude),
                    place.and_then(|p| p.longitude)
                ],
            )
            .is_ok()
    }

    /// The plays that started in [since, until), oldest first, each with its
    /// place (None for plays logged without one, as every play before 2.4).
    pub fn plays(&self, since: f64, until: Option<f64>) -> Vec<crate::stats::Play> {
        let q = format!(
            "SELECT path, started, seconds, duration, title, artist, album, album_artist, genre, year, composer, {} \
             FROM plays WHERE started >= ?1 AND started < ?2 ORDER BY started, id",
            PLACE_COLUMNS.join(", ")
        );
        let Ok(mut st) = self.db.prepare(&q) else { return vec![] };
        let s = |r: &rusqlite::Row, i: usize| r.get::<_, Option<String>>(i).ok().flatten().unwrap_or_default();
        let rows = st.query_map(params![since, until.unwrap_or(f64::INFINITY)], |r| {
            let o = |i: usize| r.get::<_, Option<String>>(i).ok().flatten();
            let n = |i: usize| r.get::<_, Option<f64>>(i).ok().flatten().map(crate::json::Value::Float);
            let place = match (o(11), o(12), o(13)) {
                (None, None, None) => None,
                (city, region, country) => crate::places::make_place(
                    &city.unwrap_or_default(),
                    &region.unwrap_or_default(),
                    &country.unwrap_or_default(),
                    &o(14).unwrap_or_default(),
                    n(15).as_ref(),
                    n(16).as_ref(),
                ),
            };
            Ok(crate::stats::Play {
                path: s(r, 0),
                started: r.get(1)?,
                seconds: r.get(2)?,
                duration: r.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
                title: s(r, 4),
                artist: s(r, 5),
                album: s(r, 6),
                album_artist: s(r, 7),
                genre: s(r, 8),
                year: s(r, 9),
                composer: s(r, 10),
                place,
            })
        });
        rows.map(|it| it.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    /// Clear the place of every play (2.4, Settings → Forget play locations…);
    /// the plays stay.  Returns how many plays had one.
    pub fn forget_places(&self) -> usize {
        let set = PLACE_COLUMNS.iter().map(|c| format!("{c} = NULL")).collect::<Vec<_>>().join(", ");
        let any = PLACE_COLUMNS.iter().map(|c| format!("{c} IS NOT NULL")).collect::<Vec<_>>().join(" OR ");
        self.db.execute(&format!("UPDATE plays SET {set} WHERE {any}"), []).unwrap_or(0)
    }

    /// How many plays have a place.
    pub fn places_count(&self) -> usize {
        let any = PLACE_COLUMNS.iter().map(|c| format!("{c} IS NOT NULL")).collect::<Vec<_>>().join(" OR ");
        self.db
            .query_row(&format!("SELECT COUNT(*) FROM plays WHERE {any}"), [], |r| r.get::<_, i64>(0))
            .map(|n| n as usize)
            .unwrap_or(0)
    }

    /// The songs played last, newest first, each once.
    pub fn recently_played(&self, limit: usize) -> Vec<String> {
        let Ok(mut st) = self.db.prepare(
            "SELECT path, MAX(started) AS last FROM plays GROUP BY path ORDER BY last DESC, path LIMIT ?1",
        ) else {
            return vec![];
        };
        st.query_map(params![limit as i64], |r| r.get::<_, String>(0))
            .map(|it| it.filter_map(Result::ok).collect())
            .unwrap_or_default()
    }

    pub fn clear_history(&self) -> usize {
        self.db.execute("DELETE FROM plays", []).unwrap_or(0)
    }

    pub fn tracks(&self) -> Vec<Track> {
        let Ok(mut st) = self.db.prepare("SELECT data FROM tracks ORDER BY path") else { return vec![] };
        let rows = st.query_map([], |r| r.get::<_, String>(0));
        let Ok(rows) = rows else { return vec![] };
        rows.filter_map(|r| r.ok()).filter_map(|d| json::parse(&d).ok()).filter_map(|v| Track::from_json(&v)).collect()
    }

    pub fn put(&self, t: &Track) {
        let style = json::Style { indent: None, compact: false, ensure_ascii: false, sort_keys: true };
        let _ = self.db.execute(
            "INSERT OR REPLACE INTO tracks VALUES (?1, ?2, ?3, ?4)",
            params![t.path, t.size, t.mtime_ns, json::dumps(&t.to_json(), style)],
        );
    }

    /// Bring the library up to date with `files` (Python `Library.refresh`).
    pub fn refresh(
        &mut self,
        files: &[String],
        jobs: usize,
        progress: &mut dyn FnMut(usize, usize),
        stop: Option<&AtomicBool>,
    ) -> Vec<Track> {
        let files: Vec<String> = files.iter().map(|f| abspath(f)).collect();
        let wanted: std::collections::HashSet<&String> = files.iter().collect();
        let mut known: HashMap<String, (i64, i64)> = HashMap::new();
        if let Ok(mut st) = self.db.prepare("SELECT path, size, mtime_ns FROM tracks") {
            if let Ok(rows) =
                st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?, r.get::<_, i64>(2)?)))
            {
                for (p, s, m) in rows.flatten() {
                    known.insert(p, (s, m));
                }
            }
        }
        let tx = self.db.unchecked_transaction().ok();
        for p in known.keys().filter(|p| !wanted.contains(p)) {
            let _ = self.db.execute("DELETE FROM tracks WHERE path = ?1", params![p]);
        }
        let todo: Vec<(String, i64, i64)> = files
            .iter()
            .filter_map(|f| stat(f).map(|(s, m)| (f.clone(), s, m)))
            .filter(|(f, s, m)| known.get(f) != Some(&(*s, *m)))
            .collect();
        let results = crate::fix::run_parallel(&todo, jobs.max(1), |(path, size, mtime)| {
            if stop.is_some_and(|s| s.load(Ordering::Relaxed)) {
                return None;
            }
            probe(path).ok().map(|info| track_from_info(&info, *size, *mtime))
        });
        for (i, t) in results.into_iter().enumerate() {
            if let Some(t) = t {
                self.put(&t);
            }
            progress(i + 1, todo.len());
        }
        if let Some(tx) = tx {
            let _ = tx.commit();
        }
        self.tracks()
    }

    /// A file was changed by Real-Hi-Res (lyrics embedded, fixed): read it again.
    pub fn update_file(&self, path: &str) {
        if let (Some((size, mtime)), Ok(info)) = (stat(path), probe(path)) {
            self.put(&track_from_info(&info, size, mtime));
        }
    }
}

/// Every audio file in the library folders.
pub fn library_files(cfg: &Settings) -> Vec<String> {
    let folders: Vec<String> =
        cfg.library_folders.iter().filter(|f| !f.is_empty() && Path::new(f).is_dir()).cloned().collect();
    if folders.is_empty() {
        return vec![];
    }
    crate::paths::collect(&folders, true, &cfg.extension_set(), &mut |_| {})
}

// ---------------------------------------------------------------- browsing ---

pub fn sort_songs(tracks: &mut [Track]) {
    tracks.sort_by(|a, b| {
        (casefold(&a.display_title()), casefold(&a.display_artist()), &a.path).cmp(&(
            casefold(&b.display_title()),
            casefold(&b.display_artist()),
            &b.path,
        ))
    });
}

#[derive(Clone, Debug)]
pub struct Album {
    pub key: String,
    pub title: String,
    pub artist: String,
    pub year: String,
    pub genre: String,
    pub tracks: Vec<Track>,
}

pub fn albums(tracks: &[Track]) -> Vec<Album> {
    let mut groups: Vec<(String, Vec<Track>)> = vec![];
    let mut index: HashMap<String, usize> = HashMap::new();
    for t in tracks {
        let key = t.song().album_key();
        match index.get(&key) {
            Some(i) => groups[*i].1.push(t.clone()),
            None => {
                index.insert(key.clone(), groups.len());
                groups.push((key, vec![t.clone()]));
            }
        }
    }
    let n = |x: i64| if x == 0 { 9999 } else { x };
    let mut out: Vec<Album> = groups
        .into_iter()
        .map(|(key, mut ts)| {
            ts.sort_by(|a, b| {
                (n(a.disc), n(a.track), casefold(&a.display_title()), &a.path).cmp(&(
                    n(b.disc),
                    n(b.track),
                    casefold(&b.display_title()),
                    &b.path,
                ))
            });
            let first = &ts[0];
            Album {
                key,
                title: first.display_album(),
                artist: if first.album_artist.is_empty() { first.display_artist() } else { first.album_artist.clone() },
                year: ts.iter().find(|t| !t.year.is_empty()).map(|t| t.year.clone()).unwrap_or_default(),
                genre: ts.iter().find(|t| !t.genre.is_empty()).map(|t| t.genre.clone()).unwrap_or_default(),
                tracks: ts,
            }
        })
        .collect();
    out.sort_by(|a, b| (casefold(&a.title), casefold(&a.artist)).cmp(&(casefold(&b.title), casefold(&b.artist))));
    out
}

#[derive(Clone, Debug)]
pub struct Group {
    pub name: String,
    pub tracks: Vec<Track>,
}

fn group_by(tracks: &[Track], name_of: impl Fn(&Track) -> String) -> Vec<Group> {
    let mut groups: Vec<Group> = vec![];
    let mut index: HashMap<String, usize> = HashMap::new();
    for t in tracks {
        let name = name_of(t);
        let key = casefold(crate::pyunicode::strip(&name));
        match index.get(&key) {
            Some(i) => groups[*i].tracks.push(t.clone()),
            None => {
                index.insert(key, groups.len());
                groups.push(Group { name, tracks: vec![t.clone()] });
            }
        }
    }
    for g in groups.iter_mut() {
        sort_songs(&mut g.tracks);
    }
    groups.sort_by_key(|g| casefold(&g.name));
    groups
}

// ------------------------------------------------- several artists in one ---
// 2.1 (Python musiclib "several artists in one"): ';' and "feat."/"ft."/"featuring" always
// separate artists; ", ", " & ", " x " and " / " only when one of the names they give is an
// artist of the library on its own (`known_artists()`).  Separator letters match in any case
// (ASCII only).

pub const ALWAYS_SEPARATORS: [&str; 9] =
    [";", " feat. ", " feat ", " ft. ", " ft ", " featuring ", " (feat. ", " (ft. ", " (featuring "];
pub const MAYBE_SEPARATORS: [&str; 4] = [", ", " & ", " x ", " / "];

/// `text` cut at every separator of `seps` (the longest that matches at a place).
fn split_on(text: &str, seps: &[&str]) -> Vec<String> {
    let mut seps: Vec<Vec<char>> = seps.iter().map(|s| s.chars().collect()).collect();
    seps.sort_by_key(|s| std::cmp::Reverse(s.len())); // stable, as Python's sorted()
    let chars: Vec<char> = text.chars().collect();
    let (mut out, mut start, mut i, n) = (vec![], 0usize, 0usize, chars.len());
    'outer: while i < n {
        for sep in &seps {
            let m = sep.len();
            if i + m <= n
                && chars[i..i + m].iter().zip(sep).all(|(c, x)| c == x || (c.is_ascii() && c.to_ascii_lowercase() == *x))
            {
                out.push(chars[start..i].iter().collect());
                i += m;
                start = i;
                continue 'outer;
            }
        }
        i += 1;
    }
    out.push(chars[start..].iter().collect());
    out
}

fn clean_names(parts: Vec<String>) -> Vec<String> {
    let mut out = vec![];
    for p in parts {
        let mut p = crate::pyunicode::strip(&p).to_string();
        if p.ends_with(')') && p.matches('(').count() < p.matches(')').count() {
            p.pop(); // "A (feat. B)" leaves "B)"
            p = crate::pyunicode::strip(&p).to_string();
        }
        if !p.is_empty() {
            out.push(p);
        }
    }
    out
}

/// The artists an artist field always stands for (split at ';' and "feat.").
pub fn artist_pieces(name: &str) -> Vec<String> {
    clean_names(split_on(name, &ALWAYS_SEPARATORS))
}

/// Casefolded names that stand on their own in an artist or album artist field.
pub fn known_artists(tracks: &[Track]) -> std::collections::HashSet<String> {
    let mut known = std::collections::HashSet::new();
    for t in tracks {
        for field in [&t.artist, &t.album_artist] {
            for piece in artist_pieces(field) {
                known.insert(casefold(&piece));
            }
        }
    }
    known
}

/// The artists of an artist field, in order and without repeats.
pub fn split_artists(name: &str, known: &std::collections::HashSet<String>) -> Vec<String> {
    let mut out: Vec<String> = vec![];
    let mut seen = std::collections::HashSet::new();
    for piece in artist_pieces(name) {
        let parts = clean_names(split_on(&piece, &MAYBE_SEPARATORS));
        let names = if parts.len() > 1 && parts.iter().any(|p| known.contains(&casefold(p))) { parts } else { vec![piece] };
        for n in names {
            if seen.insert(casefold(&n)) {
                out.push(n);
            }
        }
    }
    out
}

/// As `group_by()`, but a track may belong to several groups (once to each).
fn group_by_many(tracks: &[Track], names_of: impl Fn(&Track) -> Vec<String>) -> Vec<Group> {
    let mut groups: Vec<Group> = vec![];
    let mut index: HashMap<String, usize> = HashMap::new();
    for t in tracks {
        let mut done = std::collections::HashSet::new();
        for name in names_of(t) {
            let key = casefold(crate::pyunicode::strip(&name));
            if !done.insert(key.clone()) {
                continue;
            }
            match index.get(&key) {
                Some(i) => groups[*i].tracks.push(t.clone()),
                None => {
                    index.insert(key, groups.len());
                    groups.push(Group { name, tracks: vec![t.clone()] });
                }
            }
        }
    }
    for g in groups.iter_mut() {
        sort_songs(&mut g.tracks);
    }
    groups.sort_by_key(|g| casefold(&g.name));
    groups
}

/// Per performing artist: each artist of the track's artist field (`split_artists()`).
pub fn artists(tracks: &[Track]) -> Vec<Group> {
    let known = known_artists(tracks);
    let mut cache: HashMap<String, Vec<String>> = HashMap::new();
    let mut names = |field: String| -> Vec<String> {
        cache
            .entry(field.clone())
            .or_insert_with(|| {
                let v = split_artists(&field, &known);
                if v.is_empty() { vec![field] } else { v }
            })
            .clone()
    };
    let lists: Vec<Vec<String>> = tracks.iter().map(|t| names(t.display_artist())).collect();
    let by_path: HashMap<&str, usize> = tracks.iter().enumerate().map(|(i, t)| (t.path.as_str(), i)).collect();
    group_by_many(tracks, |t| lists[by_path[t.path.as_str()]].clone())
}

/// Per album artist (else the track's artist), each artist of the field on its own.
pub fn album_artists(tracks: &[Track]) -> Vec<Group> {
    let known = known_artists(tracks);
    let mut cache: HashMap<String, Vec<String>> = HashMap::new();
    let lists: Vec<Vec<String>> = tracks
        .iter()
        .map(|t| {
            let a = crate::pyunicode::strip(&t.album_artist);
            let field = if a.is_empty() { t.display_artist() } else { a.to_string() };
            cache
                .entry(field.clone())
                .or_insert_with(|| {
                    let v = split_artists(&field, &known);
                    if v.is_empty() { vec![field] } else { v }
                })
                .clone()
        })
        .collect();
    let by_path: HashMap<&str, usize> = tracks.iter().enumerate().map(|(i, t)| (t.path.as_str(), i)).collect();
    group_by_many(tracks, |t| lists[by_path[t.path.as_str()]].clone())
}

pub fn composers(tracks: &[Track]) -> Vec<Group> {
    group_by(tracks, |t| {
        let c = crate::pyunicode::strip(&t.composer);
        if c.is_empty() {
            "Unknown Composer".into()
        } else {
            c.to_string()
        }
    })
}

/// Per year, newest first, "Unknown Year" last.
pub fn years(tracks: &[Track]) -> Vec<Group> {
    let mut g = group_by(tracks, |t| {
        if t.year.len() == 4 && t.year.bytes().all(|b| b.is_ascii_digit()) {
            t.year.clone()
        } else {
            "Unknown Year".into()
        }
    });
    g.sort_by(|a, b| {
        let k = |x: &Group| (x.name == "Unknown Year", -x.name.parse::<i64>().unwrap_or(0), x.name.clone());
        k(a).cmp(&k(b))
    });
    g
}

/// Disc, track number, title (as on an album).
pub fn album_sorted(tracks: &mut [Track]) {
    let n = |x: i64| if x == 0 { 9999 } else { x };
    tracks.sort_by(|a, b| {
        (n(a.disc), n(a.track), casefold(&a.display_title()), &a.path).cmp(&(
            n(b.disc),
            n(b.track),
            casefold(&b.display_title()),
            &b.path,
        ))
    });
}

/// (subfolders, songs) of `folder`: the folders right under it that hold songs,
/// as (path, name, songs in it and below), sorted by name, and the songs
/// directly in it, in album order (Python `folder_view`).
pub fn folder_view(tracks: &[Track], folder: &str) -> (Vec<(String, String, usize)>, Vec<Track>) {
    let folder = abspath(folder);
    let sep = std::path::MAIN_SEPARATOR;
    let prefix = format!("{}{sep}", folder.trim_end_matches(sep));
    let mut subs: HashMap<String, usize> = HashMap::new();
    let mut here = vec![];
    for t in tracks {
        if parent(&t.path) == folder {
            here.push(t.clone());
        } else if let Some(rest) = t.path.strip_prefix(&prefix) {
            let first = rest.split(sep).next().unwrap_or("");
            *subs.entry(format!("{prefix}{first}")).or_insert(0) += 1;
        }
    }
    let mut folders: Vec<(String, String, usize)> = subs.into_iter().map(|(p, n)| (p.clone(), file_name(&p), n)).collect();
    folders.sort_by(|a, b| (casefold(&a.1), &a.0).cmp(&(casefold(&b.1), &b.0)));
    album_sorted(&mut here);
    (folders, here)
}

pub fn genres(tracks: &[Track]) -> Vec<Group> {
    group_by(tracks, |t| {
        let g = crate::pyunicode::strip(&t.genre);
        if g.is_empty() {
            "Unknown Genre".into()
        } else {
            g.to_string()
        }
    })
}
