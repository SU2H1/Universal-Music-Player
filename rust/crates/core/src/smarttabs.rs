//! Custom tabs (2.3), the counterpart of `realhires/smarttabs.py`: saved sets of
//! rules that pick songs from the library, kept as one JSON file per tab in the
//! tabs folder ("tabs" in `store::data_dir()`, or `$REALHIRES_TABS`).  A tab only
//! names fields, operators and values, so a shared tab file is safe to open.  Both
//! versions give the same answers and write the same bytes (`python_tabs.json`).

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use crate::json::{self, Obj, Value};
use crate::musiclib::Track;
use crate::pyunicode::{casefold, rstrip, strip};

pub const TABS_ENV: &str = "REALHIRES_TABS";
pub const FOLDER_NAME: &str = "tabs";
pub const TAB_FORMAT: &str = "universal-music-player-tab";
pub const TAB_VERSION: i64 = 1;
pub const MAX_FILE_BYTES: u64 = 1 << 20;
/// characters of a tab's name
pub const MAX_NAME: usize = 80;
pub const MAX_RULES: usize = 100;
pub const MAX_LIMIT: i64 = 100_000;
/// larger whole numbers are kept as floats (as Python does)
pub const MAX_INT: i64 = 1_000_000_000_000_000;
/// how much of an unknown field, operator or sort a message repeats
const SHOWN_CHARS: usize = 60;

pub const TEXT_FIELDS: [&str; 11] =
    ["title", "artist", "album", "album_artist", "genre", "composer", "format", "folder", "quality", "mood", "tempo"];
pub const NUMBER_FIELDS: [&str; 7] = ["year", "bits", "sample_rate", "bit_rate", "duration", "added_days", "bpm"];
pub const YESNO_FIELDS: [&str; 3] = ["lossless", "liked", "disliked"];
pub const FIELDS: [&str; 21] = [
    "title", "artist", "album", "album_artist", "genre", "composer", "format", "folder", "quality", "mood", "tempo",
    "year", "bits", "sample_rate", "bit_rate", "duration", "added_days", "bpm", "lossless", "liked", "disliked",
];
pub const TEXT_OPS: [&str; 5] = ["is", "is_not", "contains", "not_contains", "starts_with"];
pub const NUMBER_OPS: [&str; 6] = ["=", "!=", ">", ">=", "<", "<="];
pub const YESNO_OPS: [&str; 1] = ["is"];
pub const YES: &str = "Yes";
pub const NO: &str = "No";
/// the values a field offers in a menu instead of an entry
pub const QUALITY_VALUES: [&str; 3] = ["Hi-Res Lossless", "Lossless", "Lossy"];
/// 2.4: the labels of `moods` (a song not analysed yet has none)
pub const MOOD_VALUES: [&str; 4] = crate::moods::MOODS;
pub const TEMPO_VALUES: [&str; 3] = crate::moods::TEMPOS;
/// "all" then "any", as the tab window's menu lists them
pub const MATCH_MODES: [&str; 2] = ["all", "any"];

/// What the tab window's menus show for a field (English; the apps show it translated).
pub fn field_label(field: &str) -> &'static str {
    match field {
        "title" => "Title",
        "artist" => "Artist",
        "album" => "Album",
        "album_artist" => "Album artist",
        "genre" => "Genre",
        "composer" => "Composer",
        "format" => "Format",
        "folder" => "Folder",
        "quality" => "Quality",
        "year" => "Year",
        "bits" => "Bit depth",
        "sample_rate" => "Sample rate (Hz)",
        "bit_rate" => "Bit rate (kbps)",
        "duration" => "Length (seconds)",
        "added_days" => "Days since added",
        "lossless" => "Lossless",
        "liked" => "Liked",
        "disliked" => "Disliked",
        "mood" => "Mood",
        "tempo" => "Tempo",
        "bpm" => "Tempo (BPM)",
        _ => "",
    }
}

/// The label of an operator for a field ("is at least"), or the operator itself.
pub fn op_label<'a>(field: &str, op: &'a str) -> &'a str {
    match (kind_of(field), op) {
        ("text", "is") => "is",
        ("text", "is_not") => "is not",
        ("text", "contains") => "contains",
        ("text", "not_contains") => "does not contain",
        ("text", "starts_with") => "starts with",
        ("number", "=") => "is",
        ("number", "!=") => "is not",
        ("number", ">") => "is more than",
        ("number", ">=") => "is at least",
        ("number", "<") => "is less than",
        ("number", "<=") => "is at most",
        ("yesno", "is") => "is",
        _ => op,
    }
}

pub fn match_label(mode: &str) -> &'static str {
    if mode == "any" {
        "Match any of these rules"
    } else {
        "Match all of these rules"
    }
}

pub fn value_choices(field: &str) -> Option<&'static [&'static str]> {
    match field {
        "quality" => Some(&QUALITY_VALUES[..]),
        "mood" => Some(&MOOD_VALUES[..]),
        "tempo" => Some(&TEMPO_VALUES[..]),
        _ => None,
    }
}

/// A tab (or tab file) that cannot be used; the message says why in a full sentence.
#[derive(Clone, Debug, PartialEq)]
pub struct TabError(pub String);

impl std::fmt::Display for TabError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// What rules need besides the song: the liked and disliked songs, the time now
/// (Unix seconds, for `added_days`) and the songs' moods (2.4, for `bpm`, `mood` and
/// `tempo`; a song not analysed yet has BPM 0 and no labels).
#[derive(Clone, Debug, Default)]
pub struct TabContext {
    pub liked: HashSet<String>,
    pub disliked: HashSet<String>,
    pub now: f64,
    pub moods: std::sync::Arc<std::collections::HashMap<String, crate::moods::Mood>>,
}

impl TabContext {
    pub fn new(liked: &[String], disliked: &[String], now: f64) -> TabContext {
        TabContext { liked: liked.iter().cloned().collect(), disliked: disliked.iter().cloned().collect(), now, ..Default::default() }
    }

    /// The same with the songs' moods (2.4).
    pub fn with_moods(mut self, moods: std::sync::Arc<std::collections::HashMap<String, crate::moods::Mood>>) -> TabContext {
        self.moods = moods;
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Rule {
    pub field: String,
    pub op: String,
    /// a string, a number or a bool, by the field's kind
    pub value: Value,
}

impl Rule {
    pub fn to_json(&self) -> Value {
        Obj::new().put("field", self.field.as_str()).put("op", self.op.as_str()).put("value", self.value.clone()).build()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Tab {
    pub name: String,
    /// "all" or "any"
    pub match_mode: String,
    pub rules: Vec<Rule>,
    pub sort: String,
    pub descending: bool,
    pub limit: i64,
    /// the file in the tabs folder it was read from or saved to
    pub file: Option<PathBuf>,
}

impl Tab {
    pub fn new(name: &str) -> Tab {
        Tab {
            name: name.to_string(),
            match_mode: "all".into(),
            rules: vec![],
            sort: "title".into(),
            descending: false,
            limit: 0,
            file: None,
        }
    }

    /// The tab as its file holds it, keys in the order of the format (without the file).
    pub fn to_json(&self) -> Value {
        Obj::new()
            .put("format", TAB_FORMAT)
            .put("version", TAB_VERSION)
            .put("name", self.name.as_str())
            .put("match", self.match_mode.as_str())
            .put("rules", Value::Array(self.rules.iter().map(Rule::to_json).collect()))
            .put("sort", self.sort.as_str())
            .put("descending", self.descending)
            .put("limit", self.limit)
            .build()
    }
}

// ---------------------------------------------------------------- fields ---

/// "text", "number" or "yesno" ("" for a field tabs do not know).
pub fn kind_of(field: &str) -> &'static str {
    if TEXT_FIELDS.contains(&field) {
        "text"
    } else if NUMBER_FIELDS.contains(&field) {
        "number"
    } else if YESNO_FIELDS.contains(&field) {
        "yesno"
    } else {
        ""
    }
}

pub fn ops_for(field: &str) -> &'static [&'static str] {
    match kind_of(field) {
        "text" => &TEXT_OPS,
        "number" => &NUMBER_OPS,
        "yesno" => &YESNO_OPS,
        _ => &[],
    }
}

pub fn default_value(field: &str) -> Value {
    if let Some(c) = value_choices(field) {
        return Value::Str(c[0].to_string());
    }
    match kind_of(field) {
        "number" => Value::Int(0),
        "yesno" => Value::Bool(true),
        _ => Value::Str(String::new()),
    }
}

/// The operator a new rule starts with: one that every song passes until a value is given.
pub fn default_op(field: &str) -> &'static str {
    match kind_of(field) {
        "text" => "contains",
        "number" => ">=",
        _ => "is",
    }
}

/// A rule for `field` with its usual operator and a value of the right kind.
pub fn new_rule(field: &str) -> Rule {
    Rule { field: field.to_string(), op: default_op(field).to_string(), value: default_value(field) }
}

/// The rule with another field: the operator and the value are kept when they fit it.
pub fn change_field(rule: &Rule, field: &str) -> Rule {
    let mut out = Rule { field: field.to_string(), op: rule.op.clone(), value: rule.value.clone() };
    if !ops_for(field).contains(&out.op.as_str()) {
        out.op = default_op(field).to_string();
    }
    let kind = kind_of(field);
    let v = &out.value;
    let mut fits = match kind {
        "text" => matches!(v, Value::Str(_)),
        "number" => number(v).is_some(),
        "yesno" => matches!(v, Value::Bool(_)),
        _ => false,
    };
    if let Some(c) = value_choices(field) {
        if !v.as_str().is_some_and(|s| c.contains(&s)) {
            fits = false;
        }
    }
    if kind_of(&rule.field) != kind || !fits {
        out.value = default_value(field);
    }
    out
}

fn template(name: &str, rules: Vec<(&str, &str, Value)>, sort: &str) -> Tab {
    let mut t = Tab::new(name);
    t.rules = rules.into_iter().map(|(f, o, v)| Rule { field: f.into(), op: o.into(), value: v }).collect();
    t.sort = sort.into();
    t
}

/// "Start from" of New Tab… (the names are shown translated).  Hi-Res is the app's
/// own Hi-Res Lossless: a tab's rules are all or any of a flat list, which cannot say
/// "lossless and (24-bit or above 48 kHz)" (Python `TEMPLATES`).
pub fn templates() -> Vec<Tab> {
    vec![
        template("Hi-Res", vec![("quality", "is", Value::Str("Hi-Res Lossless".into()))], "title"),
        template("Lossy", vec![("lossless", "is", Value::Bool(false))], "title"),
        template("Added in the last 30 days", vec![("added_days", "<=", Value::Int(30))], "added_days"),
        template("Long songs", vec![("duration", ">=", Value::Int(600))], "title"),
    ]
}

/// A rule's number as it is kept: whole numbers up to MAX_INT stay whole, larger ones
/// and decimals are floats; None when it is not a finite number.
fn number(v: &Value) -> Option<Value> {
    match v {
        Value::Int(i) if i.unsigned_abs() <= MAX_INT as u64 => Some(Value::Int(*i)),
        Value::Int(i) => Some(Value::Float(*i as f64)),
        Value::Float(f) if f.is_finite() => Some(Value::Float(*f)),
        _ => None,
    }
}

pub fn clean_name(name: &str) -> String {
    crate::playerstate::clean_name(name)
}

fn shown(text: &str) -> String {
    if text.chars().count() <= SHOWN_CHARS {
        text.to_string()
    } else {
        text.chars().take(SHOWN_CHARS - 1).collect::<String>() + "…"
    }
}

// ----------------------------------------------------------------- checks ---

/// Rule number `n` of a tab, checked and as it is saved (Python `check_rule`).
pub fn check_rule(rule: &Value, n: usize) -> Result<Rule, TabError> {
    if !matches!(rule, Value::Object(_)) {
        return Err(TabError(format!("Rule {n} is not a rule: a rule has a field, an operator and a value.")));
    }
    let f = match rule.get("field") {
        Some(Value::Str(s)) if !s.is_empty() => s.as_str(),
        _ => return Err(TabError(format!("Rule {n} has no field."))),
    };
    if !FIELDS.contains(&f) {
        return Err(TabError(format!("Rule {n} uses the field “{}”, which tabs do not know.", shown(f))));
    }
    let label = field_label(f);
    let op = match rule.get("op") {
        Some(Value::Str(s)) if !s.is_empty() => s.as_str(),
        _ => return Err(TabError(format!("Rule {n} has no operator."))),
    };
    if !ops_for(f).contains(&op) {
        return Err(TabError(format!("Rule {n}: “{label}” cannot use the operator “{}”.", shown(op))));
    }
    let Some(v) = rule.get("value") else {
        return Err(TabError(format!("Rule {n} has no value.")));
    };
    let value = match kind_of(f) {
        "text" => match v {
            Value::Str(_) => v.clone(),
            _ => return Err(TabError(format!("Rule {n}: the value of “{label}” must be a text."))),
        },
        "number" => match number(v) {
            Some(x) => x,
            None => return Err(TabError(format!("Rule {n}: the value of “{label}” must be a number."))),
        },
        _ => match v {
            Value::Bool(_) => v.clone(),
            _ => return Err(TabError(format!("Rule {n}: the value of “{label}” must be yes or no."))),
        },
    };
    Ok(Rule { field: f.to_string(), op: op.to_string(), value })
}

/// The tab as it is saved, or a TabError saying in a full sentence what is wrong
/// (Python `check_tab`).  Keys that are left out get their defaults.
pub fn check_tab(data: &Value) -> Result<Tab, TabError> {
    if !matches!(data, Value::Object(_)) || data.get("format").and_then(Value::as_str) != Some(TAB_FORMAT) {
        return Err(TabError("This file is not a tab of Universal Music Player.".into()));
    }
    let version = match data.get("version") {
        Some(Value::Int(v)) if *v >= 1 => *v,
        _ => return Err(TabError("This tab has a format version that Universal Music Player does not know.".into())),
    };
    if version > TAB_VERSION {
        return Err(TabError("This tab was made by a newer version of Universal Music Player. Update the app to use it.".into()));
    }
    let name = match data.get("name") {
        Some(Value::Str(s)) => clean_name(s),
        _ => String::new(),
    };
    if name.is_empty() {
        return Err(TabError("Give the tab a name.".into()));
    }
    let match_mode = match data.get("match") {
        None => "all".to_string(),
        Some(Value::Str(s)) if s == "all" || s == "any" => s.clone(),
        _ => return Err(TabError("A tab matches “all” or “any” of its rules.".into())),
    };
    let rules = match data.get("rules") {
        None => vec![],
        Some(Value::Array(a)) => a.clone(),
        _ => return Err(TabError("The rules of a tab must be a list.".into())),
    };
    if rules.len() > MAX_RULES {
        return Err(TabError("A tab can have at most 100 rules.".into()));
    }
    let mut checked = vec![];
    for (i, r) in rules.iter().enumerate() {
        checked.push(check_rule(r, i + 1)?);
    }
    let sort = match data.get("sort") {
        None => "title".to_string(),
        Some(Value::Str(s)) => {
            if !FIELDS.contains(&s.as_str()) {
                return Err(TabError(format!("A tab cannot be sorted by “{}”.", shown(s))));
            }
            s.clone()
        }
        _ => return Err(TabError("A tab is sorted by one of the fields of its rules.".into())),
    };
    let descending = match data.get("descending") {
        None => false,
        Some(Value::Bool(b)) => *b,
        _ => return Err(TabError("Descending must be true or false.".into())),
    };
    let limit = match data.get("limit") {
        None => 0,
        Some(Value::Int(l)) if (0..=MAX_LIMIT).contains(l) => *l,
        _ => return Err(TabError("The limit must be a whole number from 0 to 100000 (0: no limit).".into())),
    };
    Ok(Tab { name, match_mode, rules: checked, sort, descending, limit, file: None })
}

/// The cleaned name for a new or renamed tab, or a TabError (empty, or taken in any case).
pub fn check_name(name: &str, tabs: &[Tab], renaming: &str) -> Result<String, TabError> {
    let clean = clean_name(name);
    if clean.is_empty() {
        return Err(TabError("Give the tab a name.".into()));
    }
    let key = casefold(&clean);
    if tabs.iter().any(|t| casefold(&t.name) == key && t.name != renaming) {
        return Err(TabError(format!("You already have a tab called “{clean}”.")));
    }
    Ok(clean)
}

/// `name`, or "name 2", "name 3"... when it is taken (in any case).
pub fn unique_name(name: &str, taken: &[String]) -> String {
    let keys: HashSet<String> = taken.iter().map(|t| casefold(t)).collect();
    let mut out = name.to_string();
    let mut n = 2;
    while keys.contains(&casefold(&out)) {
        let suffix = format!(" {n}");
        let head: String = name.chars().take(MAX_NAME.saturating_sub(suffix.chars().count())).collect();
        out = clean_name(&(rstrip(&head).to_string() + &suffix));
        n += 1;
    }
    out
}

// ----------------------------------------------------------------- values ---

fn int_text(s: &str) -> bool {
    let d = s.strip_prefix(['+', '-']).unwrap_or(s);
    (1..=15).contains(&d.len()) && d.bytes().all(|b| b.is_ascii_digit())
}

fn float_text(s: &str) -> bool {
    let d = s.strip_prefix(['+', '-']).unwrap_or(s);
    let (mant, exp) = match d.find(['e', 'E']) {
        Some(i) => (&d[..i], Some(&d[i + 1..])),
        None => (d, None),
    };
    let mant_ok = match mant.find('.') {
        Some(i) => {
            let (a, b) = (&mant[..i], &mant[i + 1..]);
            a.bytes().all(|c| c.is_ascii_digit()) && b.bytes().all(|c| c.is_ascii_digit()) && !(a.is_empty() && b.is_empty())
        }
        None => !mant.is_empty() && mant.bytes().all(|c| c.is_ascii_digit()),
    };
    let exp_ok = match exp {
        None => true,
        Some(e) => {
            let e = e.strip_prefix(['+', '-']).unwrap_or(e);
            !e.is_empty() && e.bytes().all(|c| c.is_ascii_digit())
        }
    };
    mant_ok && exp_ok
}

/// What a value typed into the tab window means for `field` (Python `parse_value`):
/// for a number field a whole number or a decimal, else the text as typed.
pub fn parse_value(field: &str, text: &str) -> Value {
    if kind_of(field) != "number" {
        return Value::Str(text.to_string());
    }
    let s = strip(text);
    if int_text(s) {
        if let Ok(i) = s.parse::<i64>() {
            return Value::Int(i);
        }
    }
    if float_text(s) {
        if let Ok(f) = s.parse::<f64>() {
            if f.is_finite() {
                return Value::Float(f);
            }
        }
    }
    Value::Str(text.to_string())
}

/// A rule's value as the tab window's entry shows it.
pub fn value_text(v: &Value) -> String {
    match v {
        Value::Bool(b) => (if *b { YES } else { NO }).to_string(),
        Value::Float(f) => crate::pyfmt::repr_float(*f),
        Value::Int(i) => i.to_string(),
        Value::Str(s) => s.clone(),
        Value::Null => "None".into(),
        other => json::dumps(other, json::Style::DEFAULT),
    }
}

/// The value of a song's field as rules see it.
#[derive(Clone, Debug, PartialEq)]
pub enum FieldValue {
    Text(String),
    Number(f64),
    YesNo(bool),
}

fn year_of(t: &Track) -> i64 {
    let y = &t.year;
    if y.len() == 4 && y.bytes().all(|b| b.is_ascii_digit()) {
        y.parse().unwrap_or(0)
    } else {
        0
    }
}

pub fn field_value(field: &str, t: &Track, ctx: &TabContext) -> FieldValue {
    use FieldValue::*;
    match field {
        "title" => Text(t.display_title()),
        "artist" => Text(if t.artist.is_empty() { t.album_artist.clone() } else { t.artist.clone() }),
        "album" => Text(t.album.clone()),
        "album_artist" => Text(t.album_artist.clone()),
        "genre" => Text(t.genre.clone()),
        "composer" => Text(t.composer.clone()),
        "format" => Text(t.codec.to_uppercase()),
        "folder" => Text(crate::paths::parent(&t.path)),
        "quality" => Text(
            if t.is_lossy {
                "Lossy"
            } else if t.sample_rate > 48000 || (if t.bits == 0 { 16 } else { t.bits }) > 16 || t.is_float {
                "Hi-Res Lossless"
            } else {
                "Lossless"
            }
            .to_string(),
        ),
        "year" => Number(year_of(t) as f64),
        "bits" => Number(if t.is_lossy { 0.0 } else { t.bits as f64 }),
        "sample_rate" => Number(t.sample_rate as f64),
        "bit_rate" => Number(if t.bit_rate != 0 { crate::pyfmt::round_int(t.bit_rate as f64 / 1000.0) as f64 } else { 0.0 }),
        "duration" => Number(t.duration),
        "added_days" => Number((ctx.now - t.mtime_ns as f64 / 1e9) / 86400.0),
        "lossless" => YesNo(!t.is_lossy),
        "liked" => YesNo(ctx.liked.contains(&t.path)),
        "disliked" => YesNo(ctx.disliked.contains(&t.path)),
        "bpm" => Number(ctx.moods.get(&t.path).map_or(0.0, |m| m.bpm as f64)),
        "mood" => Text(ctx.moods.get(&t.path).map(|m| m.mood.clone()).unwrap_or_default()),
        "tempo" => Text(ctx.moods.get(&t.path).map(|m| m.tempo.clone()).unwrap_or_default()),
        _ => Text(String::new()),
    }
}

fn value_number(v: &Value) -> f64 {
    v.as_f64().unwrap_or(f64::NAN)
}

pub fn rule_matches(rule: &Rule, t: &Track, ctx: &TabContext) -> bool {
    match field_value(&rule.field, t, ctx) {
        FieldValue::Text(have) => {
            let a = casefold(strip(&have));
            let want = match &rule.value {
                Value::Str(s) => s.clone(),
                other => value_text(other),
            };
            let b = casefold(strip(&want));
            match rule.op.as_str() {
                "is" => a == b,
                "is_not" => a != b,
                "contains" => a.contains(&b),
                "not_contains" => !a.contains(&b),
                _ => a.starts_with(&b),
            }
        }
        FieldValue::Number(a) => {
            let b = value_number(&rule.value);
            match rule.op.as_str() {
                "=" => a == b,
                "!=" => a != b,
                ">" => a > b,
                ">=" => a >= b,
                "<" => a < b,
                _ => a <= b,
            }
        }
        FieldValue::YesNo(have) => have == rule.value.truthy(),
    }
}

/// Whether the song belongs to the tab (a tab without rules has every song).
pub fn matches(tab: &Tab, t: &Track, ctx: &TabContext) -> bool {
    if tab.rules.is_empty() {
        return true;
    }
    if tab.match_mode == "any" {
        tab.rules.iter().any(|r| rule_matches(r, t, ctx))
    } else {
        tab.rules.iter().all(|r| rule_matches(r, t, ctx))
    }
}

enum SortKey {
    Text(String),
    Number(f64),
    YesNo(bool),
}

fn sort_key(field: &str, t: &Track, ctx: &TabContext) -> SortKey {
    match field_value(field, t, ctx) {
        FieldValue::Text(s) => SortKey::Text(casefold(strip(&s))),
        FieldValue::Number(n) => SortKey::Number(n),
        FieldValue::YesNo(b) => SortKey::YesNo(b),
    }
}

fn cmp_keys(a: &SortKey, b: &SortKey) -> std::cmp::Ordering {
    match (a, b) {
        (SortKey::Text(x), SortKey::Text(y)) => x.cmp(y),
        (SortKey::Number(x), SortKey::Number(y)) => x.partial_cmp(y).unwrap_or(std::cmp::Ordering::Equal),
        (SortKey::YesNo(x), SortKey::YesNo(y)) => x.cmp(y),
        _ => std::cmp::Ordering::Equal,
    }
}

/// The paths of the songs of the tab: the matching tracks sorted by the tab's sort
/// field (descending if asked), then by title, artist and path; at most `limit` of
/// them when it is above 0 (Python `tab_songs`).
pub fn tab_songs<'a>(tab: &Tab, tracks: impl IntoIterator<Item = &'a Track>, ctx: &TabContext) -> Vec<String> {
    let sort = if FIELDS.contains(&tab.sort.as_str()) { tab.sort.as_str() } else { "title" };
    let mut hits: Vec<(SortKey, (String, String), &Track)> = tracks
        .into_iter()
        .filter(|t| matches(tab, t, ctx))
        .map(|t| (sort_key(sort, t, ctx), (casefold(&t.display_title()), casefold(&t.display_artist())), t))
        .collect();
    hits.sort_by(|a, b| a.1.cmp(&b.1).then_with(|| a.2.path.cmp(&b.2.path)));
    hits.sort_by(|a, b| {
        let o = cmp_keys(&a.0, &b.0);
        if tab.descending {
            o.reverse()
        } else {
            o
        }
    });
    if tab.limit > 0 {
        hits.truncate(tab.limit as usize);
    }
    hits.into_iter().map(|h| h.2.path.clone()).collect()
}

// ------------------------------------------------------------------ files ---

/// The tabs folder: `$REALHIRES_TABS`, else "tabs" in the data folder.
pub fn tabs_folder() -> PathBuf {
    std::env::var_os(TABS_ENV)
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::store::data_dir().join(FOLDER_NAME))
}

const RESERVED: [&str; 22] = [
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2",
    "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
];

/// The file name a tab is saved under: its name with the characters file systems do
/// not allow replaced by "_", and ".json" (Python `tab_file_name`).
pub fn tab_file_name(name: &str) -> String {
    let mapped: String = clean_name(name)
        .chars()
        .map(|c| if "\\/:*?\"<>|".contains(c) || (c as u32) < 32 || c as u32 == 127 { '_' } else { c })
        .collect();
    let mut out = mapped.trim_matches(|c| c == ' ' || c == '.').to_string();
    if out.is_empty() {
        out = "tab".into();
    }
    let first = out.split('.').next().unwrap_or("").to_uppercase();
    if RESERVED.contains(&first.as_str()) {
        out = format!("_{out}");
    }
    out + ".json"
}

/// The text of a tab file (checked first): keys in the order of the format, two
/// spaces of indent, UTF-8 characters as they are, a newline at the end.
pub fn tab_text(tab: &Tab) -> Result<String, TabError> {
    let t = check_tab(&tab.to_json())?;
    let style = json::Style { indent: Some(2), compact: false, ensure_ascii: false, sort_keys: false };
    Ok(json::dumps(&t.to_json(), style) + "\n")
}

/// A tab from the text of a file.
pub fn parse_tab(text: &str) -> Result<Tab, TabError> {
    let data = json::parse(text.trim_start_matches('\u{feff}'))
        .map_err(|_| TabError("This file is not a tab of Universal Music Player.".into()))?;
    check_tab(&data)
}

/// Whether a .json file's text says it is a tab (its "format"), even a broken one.
pub fn is_tab_text(text: &str) -> bool {
    json::parse(text.trim_start_matches('\u{feff}'))
        .ok()
        .is_some_and(|d| d.get("format").and_then(Value::as_str) == Some(TAB_FORMAT))
}

/// Whether a file (a .json dropped onto the window) is a tab file.
pub fn is_tab_file(path: &Path) -> bool {
    std::fs::metadata(path).is_ok_and(|m| m.is_file() && m.len() <= MAX_FILE_BYTES)
        && std::fs::read(path).ok().and_then(|b| String::from_utf8(b).ok()).is_some_and(|t| is_tab_text(&t))
}

fn read(path: &Path) -> Result<String, TabError> {
    let meta = std::fs::metadata(path).map_err(|_| TabError("The file could not be read.".into()))?;
    if meta.len() > MAX_FILE_BYTES {
        return Err(TabError("The file is larger than 1 MB, which no tab is.".into()));
    }
    let bytes = std::fs::read(path).map_err(|_| TabError("The file could not be read.".into()))?;
    String::from_utf8(bytes).map_err(|_| TabError("This file is not a tab of Universal Music Player.".into()))
}

/// Whether two paths name the same file (Python `os.path.samefile`).
fn same_file(a: &Path, b: &Path) -> bool {
    let (Ok(ma), Ok(mb)) = (std::fs::metadata(a), std::fs::metadata(b)) else { return false };
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let _ = (&ma, &mb);
        ma.dev() == mb.dev() && ma.ino() == mb.ino()
    }
    #[cfg(not(unix))]
    {
        let _ = (ma, mb);
        let key = |p: &Path| casefold(&crate::paths::abspath(&p.to_string_lossy()));
        key(a) == key(b)
    }
}

static TEMP_COUNT: AtomicU64 = AtomicU64::new(0);

fn write(path: &Path, text: &str) -> std::io::Result<()> {
    let dir = path.parent().filter(|d| !d.as_os_str().is_empty()).unwrap_or(Path::new("."));
    let tmp = dir.join(format!(".tab-{}-{}.tmp", std::process::id(), TEMP_COUNT.fetch_add(1, Ordering::Relaxed)));
    if let Err(e) = std::fs::write(&tmp, text.as_bytes()).and_then(|_| std::fs::rename(&tmp, path)) {
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }
    Ok(())
}

/// The tabs of the .json files in the tabs folder, in file-name order, each with its
/// file; a name that is taken (in any case) gets " 2", " 3"...  Also the files that
/// are not tabs, as (file name, why) (Python `load_tabs`).
pub fn load_tabs(folder: Option<&Path>) -> (Vec<Tab>, Vec<(String, String)>) {
    let d = folder.map(Path::to_path_buf).unwrap_or_else(tabs_folder);
    let Ok(entries) = std::fs::read_dir(&d) else { return (vec![], vec![]) };
    let mut names: Vec<String> = entries
        .filter_map(|e| e.ok()?.file_name().into_string().ok())
        .filter(|n| !n.starts_with('.') && n.to_ascii_lowercase().ends_with(".json"))
        .collect();
    names.sort();
    let mut tabs: Vec<Tab> = vec![];
    let mut skipped = vec![];
    for n in names {
        let p = d.join(&n);
        if !p.is_file() {
            continue;
        }
        match read(&p).and_then(|text| parse_tab(&text)) {
            Ok(mut t) => {
                let taken: Vec<String> = tabs.iter().map(|x| x.name.clone()).collect();
                t.name = unique_name(&t.name, &taken);
                t.file = Some(p);
                tabs.push(t);
            }
            Err(e) => skipped.push((n, e.0)),
        }
    }
    (tabs, skipped)
}

/// Write a tab into the tabs folder (checked first, through a temp file) as
/// `tab_file_name()` of its name ("Hi-Res 2.json" when another file has that name);
/// `old_file`, the file it was read from, is written over when the name gives the
/// same file, else deleted once the new file is there (Python `save_tab`).
pub fn save_tab(tab: &Tab, folder: Option<&Path>, old_file: Option<&Path>) -> Result<Tab, TabError> {
    let mut t = check_tab(&tab.to_json())?;
    let d = folder.map(Path::to_path_buf).unwrap_or_else(tabs_folder);
    let err = |e: std::io::Error| TabError(format!("The tab could not be saved: {e}"));
    std::fs::create_dir_all(&d).map_err(err)?;
    let file_name = tab_file_name(&t.name);
    let stem = &file_name[..file_name.len() - 5];
    let mut n = 1;
    let target = loop {
        let target = d.join(if n > 1 { format!("{stem} {n}.json") } else { format!("{stem}.json") });
        if !target.exists() || old_file.is_some_and(|o| same_file(&target, o)) {
            break target;
        }
        n += 1;
    };
    write(&target, &tab_text(&t)?).map_err(err)?;
    if let Some(o) = old_file {
        if o.exists() && !same_file(o, &target) {
            std::fs::remove_file(o).map_err(err)?;
        }
    }
    t.file = Some(target);
    Ok(t)
}

/// Delete a tab's file; false when it was not there.
pub fn delete_tab(tab: &Tab) -> bool {
    tab.file.as_ref().is_some_and(|f| std::fs::remove_file(f).is_ok())
}

/// Write a tab file somewhere else (to share it).
pub fn export_tab(tab: &Tab, path: &Path) -> Result<PathBuf, TabError> {
    write(path, &tab_text(tab)?).map_err(|e| TabError(format!("The tab could not be exported: {e}")))?;
    Ok(path.to_path_buf())
}

/// Copy a tab file into the tabs folder (a taken name gets " 2", " 3"...); the tab
/// with its new file, or a TabError when the file is not a tab or is in the tabs
/// folder already (Python `import_tab`).
pub fn import_tab(path: &Path, folder: Option<&Path>) -> Result<Tab, TabError> {
    let d = folder.map(Path::to_path_buf).unwrap_or_else(tabs_folder);
    let abs = PathBuf::from(crate::paths::abspath(&path.to_string_lossy()));
    if d.is_dir() && abs.parent().is_some_and(|p| same_file(p, &d)) {
        return Err(TabError("This tab is in the tabs folder already.".into()));
    }
    let mut t = parse_tab(&read(path)?)?;
    let (tabs, _) = load_tabs(Some(&d));
    let taken: Vec<String> = tabs.iter().map(|x| x.name.clone()).collect();
    t.name = unique_name(&t.name, &taken);
    save_tab(&t, Some(&d), None)
}
