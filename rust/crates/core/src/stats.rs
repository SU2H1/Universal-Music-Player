//! Listening statistics ("Your Stats", 2.0), the counterpart of
//! `realhires/stats.py`: the same numbers and texts from the plays logged in
//! `library.sqlite`, checked against `tests/python_stats.json`.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use chrono::{Datelike, Local, TimeZone, Timelike};

use crate::json::{Obj as ObjectBuilder, Value};
use crate::musiclib::Track;
use crate::pyunicode::{casefold, strip};

pub const COUNTED_SECONDS: f64 = 30.0;
pub const PERIODS: [&str; 4] = ["Last 30 days", "This year", "Last year", "All time"];
pub const DEFAULT_PERIOD: &str = "This year";
pub const TOP: usize = 100;
pub const WEEKDAYS: [&str; 7] = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
pub const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
pub const FULL_MONTHS: [&str; 12] = [
    "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December",
];
/// the calendar's tints: 1 (a little music) .. LEVELS (the month's busiest day)
pub const LEVELS: u8 = 4;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Play {
    pub path: String,
    pub started: f64,
    pub seconds: f64,
    pub duration: f64,
    pub title: String,
    pub artist: String,
    pub album: String,
    pub album_artist: String,
    pub genre: String,
    pub year: String,
    pub composer: String,
    /// 2.4: where it was played, None when not known
    pub place: Option<crate::places::Place>,
}

/// 30 s, or half of a shorter song.
pub fn counted(p: &Play) -> bool {
    p.seconds >= COUNTED_SECONDS.min(p.duration.max(2.0) / 2.0)
}

/// (year, month, day, hour, minute, weekday with Monday = 0) of a Unix time.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct LocalParts {
    pub year: i32,
    pub month: u32,
    pub day: u32,
    pub hour: u32,
    pub minute: u32,
    pub wday: u32,
}

/// The computer's local time, as Python's `time.localtime`.
pub fn local_parts(ts: f64) -> LocalParts {
    let secs = ts.floor() as i64;
    match Local.timestamp_opt(secs, 0).earliest() {
        Some(t) => LocalParts {
            year: t.year(),
            month: t.month(),
            day: t.day(),
            hour: t.hour(),
            minute: t.minute(),
            wday: t.weekday().num_days_from_monday(),
        },
        None => LocalParts { year: 1970, month: 1, day: 1, hour: 0, minute: 0, wday: 3 },
    }
}

/// [start, end) of a period; end None = open.
pub fn period_range(name: &str, now: f64) -> (f64, Option<f64>) {
    let year = local_parts(now).year;
    let jan1 = |y: i32| Local.with_ymd_and_hms(y, 1, 1, 0, 0, 0).earliest().map(|t| t.timestamp() as f64).unwrap_or(0.0);
    match name {
        "Last 30 days" => (now - 30.0 * 86400.0, None),
        "This year" => (jan1(year), None),
        "Last year" => (jan1(year - 1), Some(jan1(year))),
        _ => (0.0, None),
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    pub key: String,
    pub name: String,
    /// songs: the artist; albums: the album artist; else empty
    pub artist: String,
    pub plays: i64,
    pub seconds: f64,
    /// how many different songs (artists, albums, genres)
    pub songs: usize,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Stats {
    pub seconds: f64,
    pub plays: i64,
    pub logged: usize,
    pub songs: usize,
    pub artists: usize,
    pub albums: usize,
    pub genres: usize,
    pub top_songs: Vec<Row>,
    pub top_artists: Vec<Row>,
    pub top_albums: Vec<Row>,
    pub top_genres: Vec<Row>,
    pub by_hour: [f64; 24],
    pub by_weekday: [f64; 7],
    pub by_month: Vec<(String, f64)>,
    pub days: usize,
    pub streak: usize,
    pub busiest_day: Option<(String, f64)>,
    pub first: Option<f64>,
    pub last: Option<f64>,
}

struct Acc {
    row: Row,
    songs: BTreeSet<String>,
}

fn rank(table: HashMap<String, Acc>, with_songs: bool) -> Vec<Row> {
    let mut rows: Vec<Row> = table
        .into_values()
        .map(|a| {
            let mut r = a.row;
            r.songs = if with_songs { a.songs.len() } else { 0 };
            r
        })
        .collect();
    rows.sort_by(|a, b| {
        b.plays
            .cmp(&a.plays)
            .then(b.seconds.partial_cmp(&a.seconds).unwrap_or(std::cmp::Ordering::Equal))
            .then(casefold(&a.name).cmp(&casefold(&b.name)))
            .then(a.key.cmp(&b.key))
    });
    rows.truncate(TOP);
    rows
}

fn days_from_civil(y: i32, m: u32, d: u32) -> i64 {
    chrono::NaiveDate::from_ymd_opt(y, m, d).map(|x| x.num_days_from_ce() as i64).unwrap_or(0)
}

/// Every number of the Stats page for these plays (oldest first); `local`
/// turns a Unix time into local date parts (`local_parts` in the app).
pub fn compute(plays: &[Play], local: &dyn Fn(f64) -> LocalParts) -> Stats {
    let mut st = Stats::default();
    let mut tables: [HashMap<String, Acc>; 4] = Default::default();
    let mut by_month: BTreeMap<String, f64> = BTreeMap::new();
    let mut by_day: BTreeMap<String, f64> = BTreeMap::new();
    let mut days: BTreeSet<i64> = BTreeSet::new();
    for p in plays {
        let c = i64::from(counted(p));
        st.plays += c;
        st.seconds += p.seconds;
        let artist = {
            let a = strip(&p.artist);
            let aa = strip(&p.album_artist);
            if !a.is_empty() {
                a.to_string()
            } else if !aa.is_empty() {
                aa.to_string()
            } else {
                "Unknown Artist".to_string()
            }
        };
        let album = if strip(&p.album).is_empty() { "Unknown Album".to_string() } else { strip(&p.album).to_string() };
        let album_artist =
            if strip(&p.album_artist).is_empty() { artist.clone() } else { strip(&p.album_artist).to_string() };
        let genre = if strip(&p.genre).is_empty() { "Unknown Genre".to_string() } else { strip(&p.genre).to_string() };
        let title = if p.title.is_empty() { p.path.clone() } else { p.title.clone() };
        let entries = [
            (p.path.clone(), title, artist.clone()),
            (casefold(&artist), artist.clone(), String::new()),
            (format!("{}\u{0}{}", casefold(&album), casefold(&album_artist)), album, album_artist),
            (casefold(&genre), genre, String::new()),
        ];
        for (table, (key, name, who)) in tables.iter_mut().zip(entries) {
            let acc = table.entry(key.clone()).or_insert_with(|| Acc {
                row: Row { key, name, artist: who, plays: 0, seconds: 0.0, songs: 0 },
                songs: BTreeSet::new(),
            });
            acc.row.plays += c;
            acc.row.seconds += p.seconds;
            acc.songs.insert(p.path.clone());
        }
        let lp = local(p.started);
        st.by_hour[lp.hour as usize] += p.seconds;
        st.by_weekday[lp.wday as usize] += p.seconds;
        *by_month.entry(format!("{:04}-{:02}", lp.year, lp.month)).or_insert(0.0) += p.seconds;
        *by_day.entry(format!("{:04}-{:02}-{:02}", lp.year, lp.month, lp.day)).or_insert(0.0) += p.seconds;
        days.insert(days_from_civil(lp.year, lp.month, lp.day));
    }
    let (mut streak, mut best, mut prev) = (0usize, 0usize, None::<i64>);
    for &o in &days {
        streak = if prev.is_some_and(|p| o == p + 1) { streak + 1 } else { 1 };
        best = best.max(streak);
        prev = Some(o);
    }
    let [songs, artists, albums, genres] = tables;
    st.logged = plays.len();
    st.songs = songs.len();
    st.artists = artists.len();
    st.albums = albums.len();
    st.genres = genres.len();
    st.top_songs = rank(songs, false);
    st.top_artists = rank(artists, true);
    st.top_albums = rank(albums, true);
    st.top_genres = rank(genres, true);
    st.by_month = by_month.into_iter().collect();
    st.days = days.len();
    st.streak = best;
    st.busiest_day = by_day
        .into_iter()
        .fold(None, |best: Option<(String, f64)>, (d, s)| match &best {
            Some((bd, bs)) if (*bs, bd.as_str()) >= (s, d.as_str()) => best,
            _ => Some((d, s)),
        });
    st.first = plays.first().map(|p| p.started);
    st.last = plays.last().map(|p| p.started);
    st
}

fn whole(x: f64) -> i64 {
    (x + 0.5) as i64
}

/// 1234567 -> "1,234,567" (Python `f"{n:,}"`).
pub fn thousands(n: i64) -> String {
    let s = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, ch) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    if n < 0 {
        format!("-{out}")
    } else {
        out
    }
}

fn plural(n: i64, word: &str) -> String {
    format!("{} {word}{}", thousands(n), if n == 1 { "" } else { "s" })
}

pub fn hour_label(h: usize) -> String {
    format!("{} {}", if h % 12 == 0 { 12 } else { h % 12 }, if h < 12 { "AM" } else { "PM" })
}

pub fn day_label(day: &str) -> String {
    let p: Vec<i64> = day.split('-').map(|x| x.parse().unwrap_or(0)).collect();
    if p.len() != 3 || !(1..=12).contains(&p[1]) {
        return day.to_string();
    }
    format!("{} {} {}", p[2], MONTHS[(p[1] - 1) as usize], p[0])
}

/// (label, big text, detail) of the summary cards; empty without plays.
pub fn cards(st: &Stats) -> Vec<(String, String, String)> {
    if st.logged == 0 {
        return vec![];
    }
    let c = |a: &str, b: String, d: String| (a.to_string(), b, d);
    let minutes = whole(st.seconds / 60.0);
    let hours = st.seconds / 3600.0;
    let about = if hours >= 48.0 {
        format!("About {} days of music.", whole(hours / 24.0))
    } else if hours >= 2.0 {
        format!("About {} hours of music.", whole(hours))
    } else {
        "Your listening has just begun.".to_string()
    };
    let mut out = vec![c("Minutes listened", thousands(minutes), about)];
    if let Some(s) = st.top_songs.first() {
        out.push(c("Your top song", s.name.clone(), format!("{} · {}", s.artist, plural(s.plays, "play"))));
    }
    if let Some(a) = st.top_artists.first() {
        out.push(c(
            "Your top artist",
            a.name.clone(),
            format!("{}, {}", plural(a.plays, "play"), plural(whole(a.seconds / 60.0), "minute")),
        ));
    }
    if let Some(a) = st.top_albums.first() {
        out.push(c("Your top album", a.name.clone(), a.artist.clone()));
    }
    if let Some(g) = st.top_genres.first() {
        let share = if st.seconds > 0.0 { whole(100.0 * g.seconds / st.seconds) } else { 0 };
        out.push(c("Your top genre", g.name.clone(), format!("{share}% of your listening time")));
    }
    out.push(c("Songs you played", thousands(st.songs as i64), format!("by {}", plural(st.artists as i64, "artist"))));
    let hour = (0..24).fold(0, |b, h| if st.by_hour[h] > st.by_hour[b] { h } else { b });
    out.push(c("Your listening hour", hour_label(hour), "when you listen most".into()));
    let wday = (0..7).fold(0, |b, d| if st.by_weekday[d] > st.by_weekday[b] { d } else { b });
    out.push(c("Your listening day", WEEKDAYS[wday].into(), "the day you listen most".into()));
    out.push(c("Longest streak", plural(st.streak as i64, "day"), "in a row with music".into()));
    if let Some((day, secs)) = &st.busiest_day {
        out.push(c("Your biggest day", day_label(day), format!("{} of music", plural(whole(secs / 60.0), "minute"))));
    }
    out
}

// ------------------------------------------------------------------ calendar ---

/// The month `delta` months after (year, month).
pub fn shift_month(year: i32, month: u32, delta: i32) -> (i32, u32) {
    let n = year as i64 * 12 + (month as i64 - 1) + delta as i64;
    (n.div_euclid(12) as i32, (n.rem_euclid(12) + 1) as u32)
}

fn days_in_month(year: i32, month: u32) -> u32 {
    let (ny, nm) = shift_month(year, month, 1);
    match (chrono::NaiveDate::from_ymd_opt(year, month, 1), chrono::NaiveDate::from_ymd_opt(ny, nm, 1)) {
        (Some(a), Some(b)) => (b - a).num_days() as u32,
        _ => 0,
    }
}

/// "November 2026".
pub fn month_title(year: i32, month: u32) -> String {
    format!("{} {year}", FULL_MONTHS[(month.clamp(1, 12) - 1) as usize])
}

/// "Tuesday 24 November 2026".
pub fn day_title(year: i32, month: u32, day: u32) -> String {
    let wday = chrono::NaiveDate::from_ymd_opt(year, month, day).map(|d| d.weekday().num_days_from_monday()).unwrap_or(0);
    format!("{} {day} {} {year}", WEEKDAYS[wday as usize], FULL_MONTHS[(month.clamp(1, 12) - 1) as usize])
}

/// The weeks of a month as the calendar shows them, Monday first: the day
/// numbers, 0 for the cells before the 1st and after the last day.
pub fn month_grid(year: i32, month: u32) -> Vec<[u32; 7]> {
    let first = chrono::NaiveDate::from_ymd_opt(year, month, 1).map(|d| d.weekday().num_days_from_monday()).unwrap_or(0);
    let mut cells: Vec<u32> = vec![0; first as usize];
    cells.extend(1..=days_in_month(year, month));
    while cells.len() % 7 != 0 {
        cells.push(0);
    }
    cells.chunks(7).map(|w| [w[0], w[1], w[2], w[3], w[4], w[5], w[6]]).collect()
}

/// The Unix time of a day's local midnight (Python `time.mktime`; a midnight
/// that a clock change skips is taken with the offset before the change).
pub fn local_midnight(year: i32, month: u32, day: u32) -> f64 {
    let Some(ndt) = chrono::NaiveDate::from_ymd_opt(year, month, day).and_then(|d| d.and_hms_opt(0, 0, 0)) else {
        return 0.0;
    };
    if let Some(t) = Local.from_local_datetime(&ndt).earliest() {
        return t.timestamp() as f64;
    }
    let before = ndt - chrono::Duration::hours(1);
    Local.from_local_datetime(&before).earliest().map(|t| t.timestamp() as f64 + 3600.0).unwrap_or(0.0)
}

/// [start, end) of a month in Unix time: the local midnights of its 1st and of the next month's.
pub fn month_bounds(year: i32, month: u32) -> (f64, f64) {
    let (ny, nm) = shift_month(year, month, 1);
    (local_midnight(year, month, 1), local_midnight(ny, nm, 1))
}

/// [start, end) of a day in Unix time: its local midnight and the next one.
pub fn day_bounds(year: i32, month: u32, day: u32) -> (f64, f64) {
    let next = chrono::NaiveDate::from_ymd_opt(year, month, day).and_then(|d| d.succ_opt());
    let end = next.map(|n| local_midnight(n.year(), n.month(), n.day())).unwrap_or(0.0);
    (local_midnight(year, month, day), end)
}

/// The minutes listened on each day of the month (index 0 is the 1st), by the
/// local day each play started on; plays of other months are left out.
pub fn month_minutes(plays: &[Play], year: i32, month: u32, local: &dyn Fn(f64) -> LocalParts) -> Vec<f64> {
    let mut out = vec![0.0; days_in_month(year, month) as usize];
    for p in plays {
        let lp = local(p.started);
        if (lp.year, lp.month) == (year, month) && (1..=out.len()).contains(&(lp.day as usize)) {
            out[lp.day as usize - 1] += p.seconds / 60.0;
        }
    }
    out
}

/// The calendar's tint of each day: 0 without music, else 1..LEVELS by its
/// minutes against the month's busiest day.
pub fn month_levels(minutes: &[f64]) -> Vec<u8> {
    let top = minutes.iter().copied().fold(0.0f64, f64::max);
    minutes
        .iter()
        .map(|&m| if m <= 0.0 || top <= 0.0 { 0 } else { (LEVELS as f64 * m / top).ceil().clamp(1.0, LEVELS as f64) as u8 })
        .collect()
}

/// How long a play was heard, for the calendar's list: "<1 min", "1 min", "1,234 min".
pub fn minutes_label(seconds: f64) -> String {
    let m = whole(seconds / 60.0);
    if m < 1 {
        "<1 min".to_string()
    } else {
        format!("{} min", thousands(m))
    }
}

/// One play in the calendar's list of a day.
#[derive(Clone, Debug, PartialEq)]
pub struct DayPlay {
    pub path: String,
    pub started: f64,
    /// the local time it started, "14:05"
    pub time: String,
    pub title: String,
    pub artist: String,
    pub seconds: f64,
    /// `minutes_label(seconds)`
    pub minutes: String,
    /// 2.4: where it was played (`places::place_text()`, "" when not known)
    pub place: String,
}

impl DayPlay {
    /// The same JSON as Python's `day_plays()` rows (for the comparison test).
    pub fn to_json(&self) -> Value {
        ObjectBuilder::new()
            .put("path", self.path.as_str())
            .put("started", self.started)
            .put("time", self.time.as_str())
            .put("title", self.title.as_str())
            .put("artist", self.artist.as_str())
            .put("seconds", self.seconds)
            .put("minutes", self.minutes.as_str())
            .put("place", self.place.as_str())
            .build()
    }
}

/// The plays that started on one local day, newest first, as the calendar
/// lists them: the local time ("14:05"), title, artist and minutes heard.
pub fn day_plays(plays: &[Play], year: i32, month: u32, day: u32, local: &dyn Fn(f64) -> LocalParts) -> Vec<DayPlay> {
    let mut rows: Vec<DayPlay> = plays
        .iter()
        .filter_map(|p| {
            let lp = local(p.started);
            if (lp.year, lp.month, lp.day) != (year, month, day) {
                return None;
            }
            let title = strip(&p.title);
            let artist = [strip(&p.artist), strip(&p.album_artist)].into_iter().find(|a| !a.is_empty());
            Some(DayPlay {
                path: p.path.clone(),
                started: p.started,
                time: format!("{:02}:{:02}", lp.hour, lp.minute),
                title: if title.is_empty() { crate::paths::stem(&p.path) } else { title.to_string() },
                artist: artist.unwrap_or("Unknown Artist").to_string(),
                seconds: p.seconds,
                minutes: minutes_label(p.seconds),
                place: crate::places::place_text(p.place.as_ref()),
            })
        })
        .collect();
    rows.reverse(); // the plays come oldest first
    rows
}

/// "3 songs played, 42 minutes" under the day's title; "No music on this day." without plays.
pub fn day_summary(rows: &[DayPlay]) -> String {
    if rows.is_empty() {
        return "No music on this day.".to_string();
    }
    let secs: f64 = rows.iter().map(|r| r.seconds).sum();
    format!("{} played, {}", plural(rows.len() as i64, "song"), plural(whole(secs / 60.0), "minute"))
}

// --------------------------------------------------------------- leaderboard ---

/// The switch of the leaderboard ("Most Played", 2.4).
pub const LEADERBOARD_KINDS: [&str; 3] = ["songs", "artists", "albums"];

/// (artist, album, album artist, genre) of a play, as `compute()` names them.
fn names(p: &Play) -> (String, String, String, String) {
    let (a, aa) = (strip(&p.artist), strip(&p.album_artist));
    let artist = if !a.is_empty() {
        a.to_string()
    } else if !aa.is_empty() {
        aa.to_string()
    } else {
        "Unknown Artist".to_string()
    };
    let album = if strip(&p.album).is_empty() { "Unknown Album".to_string() } else { strip(&p.album).to_string() };
    let album_artist = if aa.is_empty() { artist.clone() } else { aa.to_string() };
    let genre = if strip(&p.genre).is_empty() { "Unknown Genre".to_string() } else { strip(&p.genre).to_string() };
    (artist, album, album_artist, genre)
}

/// A row of the leaderboard (Python's `leaderboard()` dicts).
#[derive(Clone, Debug, PartialEq)]
pub struct LeaderRow {
    /// 1 for the most played; rows with the same plays and minutes share a rank
    pub rank: usize,
    pub key: String,
    pub name: String,
    /// a song's artist or an album's artist; empty for artists
    pub artist: String,
    pub plays: i64,
    pub seconds: f64,
    /// the whole minutes listened (rounded half up)
    pub minutes: i64,
    /// the row's songs, the most played first
    pub paths: Vec<String>,
}

impl LeaderRow {
    /// The same JSON as Python's rows (for the comparison test).
    pub fn to_json(&self) -> Value {
        ObjectBuilder::new()
            .put("rank", self.rank as i64)
            .put("key", self.key.as_str())
            .put("name", self.name.as_str())
            .put("artist", self.artist.as_str())
            .put("plays", self.plays)
            .put("seconds", self.seconds)
            .put("minutes", self.minutes)
            .put("paths", Value::Array(self.paths.iter().map(|p| Value::Str(p.clone())).collect()))
            .build()
    }
}

/// The most played songs, artists or albums of these plays (2.4, "Most Played"): the
/// rows with at least one counted play, by plays, then the whole minutes listened, then
/// the name; rows with the same plays and minutes share a rank (1, 2, 2, 4); at most
/// `top` rows.  Keys are those of `compute()`; an unknown kind gives no rows.
pub fn leaderboard(plays: &[Play], kind: &str, top: usize) -> Vec<LeaderRow> {
    if !LEADERBOARD_KINDS.contains(&kind) {
        return vec![];
    }
    struct Acc {
        row: LeaderRow,
        order: Vec<String>,
        songs: HashMap<String, (i64, f64)>,
    }
    let mut order: Vec<String> = vec![];
    let mut rows: HashMap<String, Acc> = HashMap::new();
    for p in plays {
        let (artist, album, album_artist, _genre) = names(p);
        let (key, name, who) = match kind {
            "songs" => {
                let title = strip(&p.title);
                let name = if title.is_empty() { crate::paths::stem(&p.path) } else { title.to_string() };
                (p.path.clone(), name, artist)
            }
            "artists" => (casefold(&artist), artist, String::new()),
            _ => (format!("{}\u{0}{}", casefold(&album), casefold(&album_artist)), album, album_artist),
        };
        let acc = rows.entry(key.clone()).or_insert_with(|| {
            order.push(key.clone());
            Acc {
                row: LeaderRow { rank: 0, key, name, artist: who, plays: 0, seconds: 0.0, minutes: 0, paths: vec![] },
                order: vec![],
                songs: HashMap::new(),
            }
        });
        let c = i64::from(counted(p));
        acc.row.plays += c;
        acc.row.seconds += p.seconds;
        let s = acc.songs.entry(p.path.clone()).or_insert_with(|| {
            acc.order.push(p.path.clone());
            (0, 0.0)
        });
        s.0 += c;
        s.1 += p.seconds;
    }
    let mut out: Vec<LeaderRow> = vec![];
    for key in order {
        let Some(mut acc) = rows.remove(&key) else { continue };
        if acc.row.plays < 1 {
            continue;
        }
        acc.row.minutes = whole(acc.row.seconds / 60.0);
        let songs = &acc.songs;
        acc.order.sort_by(|a, b| {
            let (x, y) = (songs[a], songs[b]);
            y.0.cmp(&x.0).then(y.1.partial_cmp(&x.1).unwrap_or(std::cmp::Ordering::Equal)).then(a.cmp(b))
        });
        acc.row.paths = acc.order;
        out.push(acc.row);
    }
    out.sort_by(|a, b| {
        b.plays
            .cmp(&a.plays)
            .then(b.minutes.cmp(&a.minutes))
            .then(casefold(&a.name).cmp(&casefold(&b.name)))
            .then(a.key.cmp(&b.key))
    });
    out.truncate(top);
    for i in 0..out.len() {
        let same = i > 0 && (out[i - 1].plays, out[i - 1].minutes) == (out[i].plays, out[i].minutes);
        out[i].rank = if same { out[i - 1].rank } else { i + 1 };
    }
    out
}

/// The songs of leaderboard rows in rank order (an artist's or album's songs, the most
/// played first), each once: what Play and Shuffle of the leaderboard play.
pub fn ranked_songs(rows: &[LeaderRow]) -> Vec<String> {
    let mut seen = std::collections::HashSet::new();
    rows.iter().flat_map(|r| r.paths.iter()).filter(|p| seen.insert(p.as_str())).cloned().collect()
}

/// What the library holds (Python `library_stats`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct LibraryStats {
    pub songs: usize,
    pub albums: usize,
    pub artists: usize,
    pub seconds: f64,
    pub bytes: i64,
    pub formats: Vec<(String, usize)>,
    pub qualities: Vec<(String, usize)>,
    pub rates: Vec<(i64, usize)>,
    pub bits: Vec<(String, usize)>,
}

pub fn library_stats(tracks: &[Track]) -> LibraryStats {
    let mut formats: HashMap<String, usize> = HashMap::new();
    let mut q = [0usize; 3];
    let mut rates: BTreeMap<i64, usize> = BTreeMap::new();
    let mut bits: HashMap<String, usize> = HashMap::new();
    for t in tracks {
        let f = if t.codec.is_empty() { "?".to_string() } else { t.codec.to_uppercase() };
        *formats.entry(f).or_insert(0) += 1;
        if t.is_lossy {
            q[2] += 1;
        } else if t.bits > 16 || t.is_float || t.sample_rate > 48000 {
            q[0] += 1;
        } else {
            q[1] += 1;
        }
        *rates.entry(t.sample_rate).or_insert(0) += 1;
        let b = if t.is_lossy {
            "lossy".to_string()
        } else if t.is_float {
            "32-bit float".to_string()
        } else if t.bits > 0 {
            format!("{}-bit", t.bits)
        } else {
            "?".to_string()
        };
        *bits.entry(b).or_insert(0) += 1;
    }
    let by_count = |m: HashMap<String, usize>| {
        let mut v: Vec<(String, usize)> = m.into_iter().collect();
        v.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
        v
    };
    LibraryStats {
        songs: tracks.len(),
        albums: crate::musiclib::albums(tracks).len(),
        artists: crate::musiclib::artists(tracks).len(),
        seconds: tracks.iter().map(|t| t.duration).sum(),
        bytes: tracks.iter().map(|t| t.size).sum(),
        formats: by_count(formats),
        qualities: vec![
            ("Hi-Res Lossless".into(), q[0]),
            ("Lossless".into(), q[1]),
            ("Lossy".into(), q[2]),
        ],
        rates: rates.into_iter().collect(),
        bits: by_count(bits),
    }
}

fn row_json(r: &Row, with_songs: bool, with_artist: bool) -> Value {
    let mut o = ObjectBuilder::new()
        .put("key", r.key.as_str())
        .put("name", r.name.as_str())
        .put("plays", r.plays)
        .put("seconds", r.seconds);
    if with_artist {
        o = o.put("artist", r.artist.as_str());
    }
    if with_songs {
        o = o.put("songs", r.songs as i64);
    }
    o.build()
}

impl Stats {
    /// The same JSON as Python's `compute()` (for the comparison test).
    pub fn to_json(&self) -> Value {
        let rows = |v: &[Row], songs: bool, artist: bool| {
            Value::Array(v.iter().map(|r| row_json(r, songs, artist)).collect())
        };
        let nums = |v: &[f64]| Value::Array(v.iter().map(|x| Value::Float(*x)).collect());
        ObjectBuilder::new()
            .put("seconds", self.seconds)
            .put("plays", self.plays)
            .put("logged", self.logged as i64)
            .put("songs", self.songs as i64)
            .put("artists", self.artists as i64)
            .put("albums", self.albums as i64)
            .put("genres", self.genres as i64)
            .put("top_songs", rows(&self.top_songs, false, true))
            .put("top_artists", rows(&self.top_artists, true, false))
            .put("top_albums", rows(&self.top_albums, true, true))
            .put("top_genres", rows(&self.top_genres, true, false))
            .put("by_hour", nums(&self.by_hour))
            .put("by_weekday", nums(&self.by_weekday))
            .put(
                "by_month",
                Value::Array(
                    self.by_month
                        .iter()
                        .map(|(m, s)| Value::Array(vec![Value::Str(m.clone()), Value::Float(*s)]))
                        .collect(),
                ),
            )
            .put("days", self.days as i64)
            .put("streak", self.streak as i64)
            .put(
                "busiest_day",
                match &self.busiest_day {
                    Some((d, s)) => Value::Array(vec![Value::Str(d.clone()), Value::Float(*s)]),
                    None => Value::Null,
                },
            )
            .put("first", self.first.map(Value::Float).unwrap_or(Value::Null))
            .put("last", self.last.map(Value::Float).unwrap_or(Value::Null))
            .build()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn calendar_bounds_are_local_midnights() {
        for (y, m, d) in [(2026, 11, 24), (2024, 2, 29), (2023, 12, 31), (2026, 3, 29), (2026, 10, 25)] {
            let (start, end) = day_bounds(y, m, d);
            let a = local_parts(start);
            assert_eq!((a.year, a.month, a.day, a.minute), (y, m, d, 0), "{y}-{m}-{d}");
            let b = local_parts(end - 1.0);
            assert_eq!((b.year, b.month, b.day), (y, m, d), "the last second of {y}-{m}-{d}");
            assert!((82_800.0..=90_000.0).contains(&(end - start)), "{y}-{m}-{d}: {}", end - start);
        }
        let (start, end) = month_bounds(2026, 12);
        assert_eq!(start, day_bounds(2026, 12, 1).0);
        assert_eq!(end, day_bounds(2027, 1, 1).0);
        assert_eq!(month_grid(2026, 11).len(), 6);
        assert_eq!(
            month_grid(2021, 2),
            vec![[1, 2, 3, 4, 5, 6, 7], [8, 9, 10, 11, 12, 13, 14], [15, 16, 17, 18, 19, 20, 21], [22, 23, 24, 25, 26, 27, 28]]
        );
    }
}
