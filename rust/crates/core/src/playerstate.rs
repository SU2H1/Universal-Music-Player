//! What the player remembers (2.0), the counterpart of
//! `realhires/playerstate.py`: the same `player.json` next to the saved
//! results, so both versions share likes, volume, modes and the equalizer.

use std::path::{Path, PathBuf};

use crate::eq::EqState;
use crate::json::{self, Obj as ObjectBuilder, Value};
use crate::playqueue::{REPEAT_MODES, SHUFFLE_MODES};
use crate::pyunicode::casefold;

pub const PLAYER_ENV: &str = "REALHIRES_PLAYER";
pub const FILE_NAME: &str = "player.json";
pub const STATE_VERSION: i64 = 1;
/// Characters of a playlist or preset name.
pub const MAX_NAME: usize = 80;
/// The size of the lyrics on Now Playing (A− / A+), in pixels.
pub const LYRICS_SIZE_DEFAULT: i64 = 26;
pub const LYRICS_SIZE_MIN: i64 = 16;
pub const LYRICS_SIZE_MAX: i64 = 48;
pub const LYRICS_SIZE_STEP: i64 = 2;

/// A lyrics size as saved (Python `clean_lyrics_size`): a whole number of pixels from
/// LYRICS_SIZE_MIN to LYRICS_SIZE_MAX (numbers and numeric text are rounded, half to
/// even); anything else gives the default.
pub fn clean_lyrics_size(value: Option<&Value>) -> i64 {
    match value.and_then(as_num) {
        Some(v) if v.is_finite() => crate::pyfmt::round_int(v).clamp(LYRICS_SIZE_MIN, LYRICS_SIZE_MAX),
        _ => LYRICS_SIZE_DEFAULT,
    }
}

/// The size of plain (not synced) lyrics shown at lyrics size `size`.
pub fn plain_lyrics_size(size: i64) -> i64 {
    crate::pyfmt::round_int(size.clamp(LYRICS_SIZE_MIN, LYRICS_SIZE_MAX) as f64 * 0.7)
}

/// The columns of song lists (2.1, chosen by the user per kind of list,
/// "columns" in player.json): every column in table order, and the defaults
/// (Python `SONG_COLUMNS`, `DEFAULT_COLUMNS`).
pub const SONG_COLUMNS: [&str; 18] = [
    "num", "title", "artist", "album", "album_artist", "composer", "genre", "year", "disc", "time", "quality", "format",
    "bit_rate", "bpm", "mood", "added", "file", "like",
];
pub const COLUMN_KINDS: [&str; 3] = ["songs", "folders", "album"];

pub fn default_columns(kind: &str) -> Vec<String> {
    let cols: &[&str] = if kind == "album" {
        &["num", "title", "artist", "album", "genre", "time", "quality", "like"]
    } else {
        &["title", "artist", "album", "genre", "time", "quality", "like"]
    };
    cols.iter().map(|c| c.to_string()).collect()
}

/// The known column names of `names`, in table order, with Title always there;
/// the default when none is left (Python `normalize_columns`).
pub fn normalize_columns(kind: &str, names: &[String]) -> Vec<String> {
    let wanted: Vec<&str> = SONG_COLUMNS.iter().copied().filter(|c| names.iter().any(|n| n == c)).collect();
    if wanted.is_empty() {
        return default_columns(kind);
    }
    SONG_COLUMNS.iter().filter(|c| **c == "title" || wanted.contains(c)).map(|c| c.to_string()).collect()
}

/// Colour labels (2.3, "allow user to color code anything"): {kind: {key: "#rrggbb"}} in
/// player.json (Python `LABEL_KINDS`).  The keys: a song's path, an album's key
/// (`playqueue::Song::album_key`), the name of an artist, album artist, genre or composer
/// (case-folded, as the groupings join names in any case), a year as written, a
/// playlist's id and a tab's name.
pub const LABEL_KINDS: [&str; 9] = ["song", "album", "artist", "album_artist", "genre", "year", "composer", "playlist", "tab"];
const FOLDED_KINDS: [&str; 4] = ["artist", "album_artist", "genre", "composer"];
/// The fixed colours of the Colour Label menu (Apple's system colours, which read on
/// light and dark themes); Custom… takes any other colour (Python `LABEL_COLORS`).
pub const LABEL_COLORS: [(&str, &str); 7] = [
    ("Red", "#ff3b30"),
    ("Orange", "#ff9500"),
    ("Yellow", "#ffcc00"),
    ("Green", "#34c759"),
    ("Blue", "#007aff"),
    ("Purple", "#af52de"),
    ("Grey", "#8e8e93"),
];

/// "#rrggbb" in lower case, or "" for anything else (Python `clean_label_color`).
pub fn clean_label_color(value: &str) -> String {
    let v = crate::pyunicode::strip(value).to_lowercase();
    if v.len() == 7 && v.starts_with('#') && v[1..].bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
        v
    } else {
        String::new()
    }
}

/// The key a label of this kind is kept under ("" when there is none): names of artists,
/// album artists, genres and composers case-folded, the others as they are (Python
/// `label_key`).
pub fn label_key(kind: &str, key: &str) -> String {
    if !LABEL_KINDS.contains(&kind) || key.is_empty() {
        return String::new();
    }
    if FOLDED_KINDS.contains(&kind) {
        casefold(key)
    } else {
        key.to_string()
    }
}

/// A playlist or preset name that cannot be used; the message says why.
#[derive(Clone, Debug, PartialEq)]
pub struct InvalidName(pub String);

impl std::fmt::Display for InvalidName {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Spaces collapsed, at most MAX_NAME characters (Python `clean_name`).
pub fn clean_name(name: &str) -> String {
    let joined = name.split(|c: char| crate::pyunicode::is_space(c)).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ");
    joined.chars().take(MAX_NAME).collect()
}

#[derive(Clone, Debug, PartialEq)]
pub struct EqPreset {
    pub name: String,
    pub eq: EqState,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Playlist {
    pub id: String,
    pub name: String,
    pub created: f64,
    pub songs: Vec<String>,
}

pub fn default_path() -> PathBuf {
    std::env::var_os(PLAYER_ENV)
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::store::data_dir().join(FILE_NAME))
}

#[derive(Clone, Debug, PartialEq)]
pub struct PlayerState {
    pub liked: Vec<String>,
    pub volume: f64,
    pub muted: bool,
    pub shuffle: String,
    pub repeat: String,
    pub eq: EqState,
    pub last_path: String,
    pub last_position: f64,
    pub disliked: Vec<String>,
    pub eq_presets: Vec<EqPreset>,
    pub playlists: Vec<Playlist>,
    /// pixels, Now Playing's A− / A+
    pub lyrics_size: i64,
    /// the columns the user chose per kind of song list ("songs", "folders", "album")
    pub columns: Vec<(String, Vec<String>)>,
    /// the colour labels (2.3): (kind, key) -> "#rrggbb"
    pub labels: std::collections::BTreeMap<(String, String), String>,
}

impl Default for PlayerState {
    fn default() -> Self {
        PlayerState {
            liked: vec![],
            volume: 0.8,
            muted: false,
            shuffle: "off".into(),
            repeat: "off".into(),
            eq: EqState::default(),
            last_path: String::new(),
            last_position: 0.0,
            disliked: vec![],
            eq_presets: vec![],
            playlists: vec![],
            lyrics_size: LYRICS_SIZE_DEFAULT,
            columns: vec![],
            labels: Default::default(),
        }
    }
}

impl PlayerState {
    pub fn is_liked(&self, path: &str) -> bool {
        self.liked.iter().any(|p| p == path)
    }

    pub fn is_disliked(&self, path: &str) -> bool {
        self.disliked.iter().any(|p| p == path)
    }

    pub fn set_liked(&mut self, path: &str, liked: bool) {
        self.liked.retain(|p| p != path);
        if liked {
            self.liked.push(path.to_string());
            self.disliked.retain(|p| p != path);
        }
        self.liked.sort();
        self.liked.dedup();
    }

    pub fn set_disliked(&mut self, path: &str, disliked: bool) {
        self.disliked.retain(|p| p != path);
        if disliked {
            self.disliked.push(path.to_string());
            self.liked.retain(|p| p != path);
        }
        self.disliked.sort();
        self.disliked.dedup();
    }

    // ---------------------------------------------------- colour labels ---
    /// The colour label ("#rrggbb") of a song, album, artist... or None.
    pub fn label_of(&self, kind: &str, key: &str) -> Option<&str> {
        let k = label_key(kind, key);
        if k.is_empty() {
            return None;
        }
        self.labels.get(&(kind.to_string(), k)).map(String::as_str)
    }

    /// Give a song, album, artist... a colour label, or take it away (None, or a value
    /// that is not a colour); returns the label it has now.
    pub fn set_label(&mut self, kind: &str, key: &str, color: Option<&str>) -> Option<String> {
        let k = label_key(kind, key);
        if k.is_empty() {
            return None;
        }
        let c = clean_label_color(color.unwrap_or(""));
        if c.is_empty() {
            self.labels.remove(&(kind.to_string(), k));
            None
        } else {
            self.labels.insert((kind.to_string(), k), c.clone());
            Some(c)
        }
    }

    // ------------------------------------------------------ lyrics size ---
    /// A+ (+1) or A− (-1): LYRICS_SIZE_STEP pixels larger or smaller per step, within the
    /// limits; returns the new size.
    pub fn step_lyrics_size(&mut self, steps: i64) -> i64 {
        let now = self.lyrics_size.clamp(LYRICS_SIZE_MIN, LYRICS_SIZE_MAX);
        self.lyrics_size = now.saturating_add(steps.saturating_mul(LYRICS_SIZE_STEP)).clamp(LYRICS_SIZE_MIN, LYRICS_SIZE_MAX);
        self.lyrics_size
    }

    // ------------------------------------------------------- EQ presets ---
    pub fn eq_preset(&self, name: &str) -> Option<&EqPreset> {
        self.eq_presets.iter().find(|p| p.name == name)
    }

    pub fn check_preset_name(&self, name: &str, renaming: &str) -> Result<String, InvalidName> {
        let name = clean_name(name);
        if name.is_empty() {
            return Err(InvalidName("Give the preset a name.".into()));
        }
        let key = casefold(&name);
        if crate::eq::PRESETS.iter().any(|(n, _)| casefold(n) == key) || key == casefold(crate::eq::CUSTOM) {
            return Err(InvalidName(format!("“{name}” is the name of a built-in preset; choose another one.")));
        }
        if self.eq_presets.iter().any(|p| casefold(&p.name) == key && p.name != renaming) {
            return Err(InvalidName(format!("You already have a preset called “{name}”.")));
        }
        Ok(name)
    }

    /// Keep `state` under `name`; with `replace`, a preset of that name is updated.
    pub fn save_eq_preset(&mut self, name: &str, state: &EqState, replace: bool) -> Result<String, InvalidName> {
        let clean = clean_name(name);
        if replace {
            if let Some(p) = self.eq_presets.iter_mut().find(|p| p.name == clean) {
                p.eq = state.clone();
                return Ok(clean);
            }
        }
        let clean = self.check_preset_name(name, "")?;
        self.eq_presets.push(EqPreset { name: clean.clone(), eq: state.clone() });
        Ok(clean)
    }

    pub fn rename_eq_preset(&mut self, old: &str, new: &str) -> Result<String, InvalidName> {
        if self.eq_preset(old).is_none() {
            return Err(InvalidName(format!("There is no preset called “{old}”.")));
        }
        let name = self.check_preset_name(new, old)?;
        if let Some(p) = self.eq_presets.iter_mut().find(|p| p.name == old) {
            p.name = name.clone();
        }
        Ok(name)
    }

    pub fn delete_eq_preset(&mut self, name: &str) {
        self.eq_presets.retain(|p| p.name != name);
    }

    /// The name of the user preset the current EQ is, or "".  Whether the
    /// equalizer is on is not part of a preset (choosing one keeps the equalizer
    /// on), so it is not compared (Python `matching_eq_preset`).
    pub fn matching_eq_preset(&self) -> String {
        self.eq_presets
            .iter()
            .find(|p| EqState { enabled: self.eq.enabled, ..p.eq.clone() } == self.eq)
            .map(|p| p.name.clone())
            .unwrap_or_default()
    }

    // -------------------------------------------------------- playlists ---
    pub fn playlist(&self, id: &str) -> Option<&Playlist> {
        self.playlists.iter().find(|p| p.id == id)
    }

    pub fn unique_playlist_name(&self, name: &str, skip: &str) -> String {
        let mut base = clean_name(name);
        if base.is_empty() {
            base = "New Playlist".into();
        }
        let taken: Vec<String> = self.playlists.iter().filter(|p| p.id != skip).map(|p| casefold(&p.name)).collect();
        if !taken.contains(&casefold(&base)) {
            return base;
        }
        let mut n = 2;
        while taken.contains(&casefold(&format!("{base} {n}"))) {
            n += 1;
        }
        format!("{base} {n}")
    }

    /// A new playlist (a taken name gets " 2", " 3"...); returns its id.
    pub fn create_playlist(&mut self, name: &str, songs: &[String], now: f64) -> String {
        let mut id = format!("{:x}", (now * 1000.0) as i64);
        while self.playlist(&id).is_some() {
            id = format!("{:x}", i64::from_str_radix(&id, 16).unwrap_or(0) + 1);
        }
        let name = self.unique_playlist_name(name, "");
        self.playlists.push(Playlist { id: id.clone(), name, created: now, songs: vec![] });
        self.add_to_playlist(&id, songs);
        id
    }

    pub fn rename_playlist(&mut self, id: &str, name: &str) -> Result<String, InvalidName> {
        if self.playlist(id).is_none() {
            return Err(InvalidName("That playlist no longer exists.".into()));
        }
        if clean_name(name).is_empty() {
            return Err(InvalidName("Give the playlist a name.".into()));
        }
        let name = self.unique_playlist_name(name, id);
        if let Some(p) = self.playlists.iter_mut().find(|p| p.id == id) {
            p.name = name.clone();
        }
        Ok(name)
    }

    pub fn delete_playlist(&mut self, id: &str) {
        self.playlists.retain(|p| p.id != id);
    }

    /// Add songs that are not in the playlist yet, at its end; returns how many.
    pub fn add_to_playlist(&mut self, id: &str, paths: &[String]) -> usize {
        let Some(p) = self.playlists.iter_mut().find(|p| p.id == id) else { return 0 };
        let mut added = 0;
        for path in paths {
            if !path.is_empty() && !p.songs.contains(path) {
                p.songs.push(path.clone());
                added += 1;
            }
        }
        added
    }

    pub fn remove_from_playlist(&mut self, id: &str, paths: &[String]) {
        if let Some(p) = self.playlists.iter_mut().find(|p| p.id == id) {
            p.songs.retain(|s| !paths.contains(s));
        }
    }

    /// Move a song up (-1) or down (+1).
    pub fn move_in_playlist(&mut self, id: &str, path: &str, delta: i64) {
        let Some(p) = self.playlists.iter_mut().find(|p| p.id == id) else { return };
        let Some(i) = p.songs.iter().position(|s| s == path) else { return };
        let j = (i as i64 + delta).clamp(0, p.songs.len() as i64 - 1) as usize;
        let s = p.songs.remove(i);
        p.songs.insert(j, s);
    }

    // ------------------------------------------------------------ columns ---
    /// The columns a kind of song list shows ("songs", "folders", "album").
    pub fn columns_of(&self, kind: &str) -> Vec<String> {
        match self.columns.iter().find(|(k, _)| k == kind) {
            Some((_, c)) => normalize_columns(kind, c),
            None => default_columns(kind),
        }
    }

    pub fn set_columns(&mut self, kind: &str, names: &[String]) -> Vec<String> {
        let cols = normalize_columns(kind, names);
        self.columns.retain(|(k, _)| k != kind);
        self.columns.push((kind.to_string(), cols.clone()));
        cols
    }

    /// Show or hide one column (Title stays).
    pub fn toggle_column(&mut self, kind: &str, name: &str) -> Vec<String> {
        let mut cols = self.columns_of(kind);
        if name == "title" || !SONG_COLUMNS.contains(&name) {
            return cols;
        }
        if cols.iter().any(|c| c == name) {
            cols.retain(|c| c != name);
        } else {
            cols.push(name.to_string());
        }
        self.set_columns(kind, &cols)
    }

    pub fn reset_columns(&mut self, kind: &str) -> Vec<String> {
        self.columns.retain(|(k, _)| k != kind);
        self.columns_of(kind)
    }

    pub fn to_json(&self) -> Value {
        let mut liked = self.liked.clone();
        liked.sort();
        liked.dedup();
        let mut columns = ObjectBuilder::new();
        for k in COLUMN_KINDS {
            columns = columns.put(k, Value::Array(self.columns_of(k).into_iter().map(Value::Str).collect()));
        }
        ObjectBuilder::new()
            .put("_version", STATE_VERSION)
            .put("columns", columns.build())
            .put("liked", Value::Array(liked.into_iter().map(Value::Str).collect()))
            .put("volume", self.volume)
            .put("muted", self.muted)
            .put("shuffle", self.shuffle.as_str())
            // "stop after this song" is for the song playing now, never for the next start
            .put("repeat", if self.repeat == "stop" { "off" } else { self.repeat.as_str() })
            .put("eq", self.eq.to_json())
            .put(
                "last",
                ObjectBuilder::new().put("path", self.last_path.as_str()).put("position", self.last_position).build(),
            )
            .put("disliked", {
                let mut d = self.disliked.clone();
                d.sort();
                d.dedup();
                Value::Array(d.into_iter().map(Value::Str).collect())
            })
            .put(
                "eq_presets",
                Value::Array(
                    self.eq_presets
                        .iter()
                        .map(|p| ObjectBuilder::new().put("name", p.name.as_str()).put("eq", p.eq.to_json()).build())
                        .collect(),
                ),
            )
            .put(
                "playlists",
                Value::Array(
                    self.playlists
                        .iter()
                        .map(|p| {
                            ObjectBuilder::new()
                                .put("id", p.id.as_str())
                                .put("name", p.name.as_str())
                                .put("created", p.created)
                                .put("songs", Value::Array(p.songs.iter().cloned().map(Value::Str).collect()))
                                .build()
                        })
                        .collect(),
                ),
            )
            .put("lyrics_size", self.lyrics_size.clamp(LYRICS_SIZE_MIN, LYRICS_SIZE_MAX))
            .put("labels", {
                let mut labels = ObjectBuilder::new();
                for kind in LABEL_KINDS {
                    let mut of = ObjectBuilder::new();
                    let mut any = false;
                    for ((k, key), c) in &self.labels {
                        if k == kind {
                            of = of.put(key, c.as_str());
                            any = true;
                        }
                    }
                    if any {
                        labels = labels.put(kind, of.build());
                    }
                }
                labels.build()
            })
            .build()
    }

    pub fn from_json(data: &Value) -> PlayerState {
        let mut st = PlayerState::default();
        if !matches!(data, Value::Object(_)) {
            return st;
        }
        if let Some(Value::Array(liked)) = data.get("liked") {
            let mut v: Vec<String> =
                liked.iter().filter_map(Value::as_str).filter(|p| !p.is_empty()).map(str::to_string).collect();
            v.sort();
            v.dedup();
            st.liked = v;
        }
        if let Some(v) = data.get("volume").and_then(as_num) {
            if !v.is_nan() {
                st.volume = v.max(0.0).min(1.0);
            }
        }
        st.muted = data.get("muted").is_some_and(Value::truthy);
        st.shuffle =
            data.get("shuffle").and_then(Value::as_str).filter(|m| SHUFFLE_MODES.contains(m)).unwrap_or("off").into();
        // "stop after this song" is for the song playing now, never for the next start
        st.repeat = data
            .get("repeat")
            .and_then(Value::as_str)
            .filter(|m| REPEAT_MODES.contains(m) && *m != "stop")
            .unwrap_or("off")
            .into();
        st.eq = EqState::from_json(data.get("eq").unwrap_or(&Value::Null));
        if let Some(Value::Array(d)) = data.get("disliked") {
            let mut v: Vec<String> = d
                .iter()
                .filter_map(Value::as_str)
                .filter(|p| !p.is_empty() && !st.liked.iter().any(|l| l == p))
                .map(str::to_string)
                .collect();
            v.sort();
            v.dedup();
            st.disliked = v;
        }
        if let Some(Value::Array(ps)) = data.get("eq_presets") {
            let mut seen: Vec<String> = vec![];
            for p in ps {
                if !matches!(p, Value::Object(_)) {
                    continue;
                }
                let name = clean_name(p.get("name").and_then(Value::as_str).unwrap_or(""));
                if !name.is_empty() && !seen.contains(&casefold(&name)) {
                    seen.push(casefold(&name));
                    st.eq_presets.push(EqPreset { name, eq: EqState::from_json(p.get("eq").unwrap_or(&Value::Null)) });
                }
            }
        }
        if let Some(Value::Array(ls)) = data.get("playlists") {
            for p in ls {
                let Some(id) = p.get("id").and_then(Value::as_str).filter(|s| !s.is_empty()) else { continue };
                if st.playlists.iter().any(|x| x.id == id) {
                    continue;
                }
                let name = clean_name(p.get("name").and_then(Value::as_str).unwrap_or(""));
                let mut songs: Vec<String> = vec![];
                if let Some(Value::Array(ss)) = p.get("songs") {
                    for x in ss.iter().filter_map(Value::as_str).filter(|x| !x.is_empty()) {
                        if !songs.iter().any(|s| s == x) {
                            songs.push(x.to_string());
                        }
                    }
                }
                st.playlists.push(Playlist {
                    id: id.to_string(),
                    name: if name.is_empty() { "Playlist".into() } else { name },
                    created: p.get("created").and_then(as_num).filter(|v| !v.is_nan()).unwrap_or(0.0),
                    songs,
                });
            }
        }
        st.lyrics_size = clean_lyrics_size(data.get("lyrics_size"));
        if let Some(cols @ Value::Object(_)) = data.get("columns") {
            for k in COLUMN_KINDS {
                if let Some(v) = cols.get(k) {
                    let names: Vec<String> = match v {
                        Value::Array(a) => a.iter().filter_map(Value::as_str).map(str::to_string).collect(),
                        _ => vec![],
                    };
                    st.columns.push((k.to_string(), normalize_columns(k, &names)));
                }
            }
        }
        if let Some(labels @ Value::Object(_)) = data.get("labels") {
            for kind in LABEL_KINDS {
                if let Some(Value::Object(given)) = labels.get(kind) {
                    for (key, color) in given {
                        st.set_label(kind, key, color.as_str());
                    }
                }
            }
        }
        if let Some(last @ Value::Object(_)) = data.get("last") {
            st.last_path = last.get("path").and_then(Value::as_str).unwrap_or("").to_string();
            st.last_position = last.get("position").and_then(as_num).filter(|v| !v.is_nan()).unwrap_or(0.0).max(0.0);
        }
        st
    }
}

fn as_num(v: &Value) -> Option<f64> {
    match v {
        Value::Int(i) => Some(*i as f64),
        Value::Float(f) => Some(*f),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::Str(s) => crate::pyunicode::strip(s).parse().ok(),
        _ => None,
    }
}

pub fn load(path: Option<&Path>) -> PlayerState {
    let p = path.map(Path::to_path_buf).unwrap_or_else(default_path);
    match std::fs::read_to_string(&p).ok().and_then(|t| json::parse(&t).ok()) {
        Some(v) => PlayerState::from_json(&v),
        None => PlayerState::default(),
    }
}

pub fn save(state: &PlayerState, path: Option<&Path>) -> std::io::Result<PathBuf> {
    let p = path.map(Path::to_path_buf).unwrap_or_else(default_path);
    if let Some(dir) = p.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let mut tmp = p.clone().into_os_string();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    let style = json::Style { indent: Some(2), compact: false, ensure_ascii: false, sort_keys: true };
    std::fs::write(&tmp, json::dumps(&state.to_json(), style) + "\n")?;
    std::fs::rename(&tmp, &p)?;
    Ok(p)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        let dir = std::env::temp_dir().join(format!("rhr-player-{}", std::process::id()));
        let path = dir.join("player.json");
        let mut st = load(Some(&path));
        assert_eq!(st, PlayerState::default());
        st.set_liked("/m/b.flac", true);
        st.set_liked("/m/a.flac", true);
        st.set_liked("/m/b.flac", false);
        st.repeat = "stop".into();
        st.shuffle = "genre".into();
        st.volume = 0.5;
        st.eq.apply_preset("Jazz");
        save(&st, Some(&path)).unwrap();
        let again = load(Some(&path));
        assert_eq!(again.liked, vec!["/m/a.flac".to_string()]);
        assert_eq!((again.shuffle.as_str(), again.repeat.as_str(), again.volume), ("genre", "off", 0.5));
        assert_eq!(again.eq.preset, "Jazz");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_preset_saved_with_the_equalizer_off_is_found_once_it_is_on() {
        let mut st = PlayerState::default();
        st.eq.apply_preset("Rock");
        st.eq.enabled = false;
        let saved = st.eq.clone();
        st.save_eq_preset("Quiet", &saved, false).unwrap();
        st.eq.enabled = true; // choosing a preset keeps the equalizer on
        assert_eq!(st.matching_eq_preset(), "Quiet");
        st.eq.gains[0] += 0.5; // changed since: no preset any more
        assert_eq!(st.matching_eq_preset(), "");
        st.eq = saved;
        st.delete_eq_preset("Quiet");
        assert!(st.eq_presets.is_empty() && st.matching_eq_preset().is_empty());
    }
}
