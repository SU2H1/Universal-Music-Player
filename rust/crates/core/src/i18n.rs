//! Languages of the desktop apps (2.1): English and Japanese, the port of
//! `realhires/i18n.py`.
//!
//! Every text the apps show is written in English and passed through `tr!()`,
//! which looks it up in the catalog of the chosen language.  The catalog is the
//! Python package's `realhires/assets/lang/ja.json`, included at build time, so
//! both apps say the same thing in both languages.  A text missing from the
//! catalog is shown in English.
//!
//! `strings` holds exact texts (with `{name}` placeholders that `tr!(text,
//! name = value)` fills in after translating); `patterns` holds templates for
//! the finished texts of the shared core, which `tr_text()` matches: each
//! `{name}` takes the shortest non-empty text that lets the rest match, as in
//! Python.  The command-line tool always writes English.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::OnceLock;

use crate::json::{self, Value};

/// The choices of the `language` setting, each in its own language.
pub const LANGUAGES: [&str; 2] = ["English", "日本語"];
pub const DEFAULT_LANGUAGE: &str = "English";
/// Names the core puts in place of a missing tag; `tr_name()` translates only these.
pub const PLACEHOLDER_NAMES: [&str; 7] = [
    "Unknown Artist",
    "Unknown Album",
    "Unknown Genre",
    "Unknown Composer",
    "Unknown Year",
    "Unknown artist",
    "Unknown album",
];
/// The verdicts as the result files store them; the apps show them translated.
pub const VERDICT_NAMES: [&str; 7] =
    ["FAKE LOSSLESS", "FAKE HI-RES", "SUSPICIOUS", "REAL HI-RES", "NOT HI-RES", "LOSSY FORMAT", "ERROR"];
const CODES: [&str; 2] = ["en", "ja"];

const JA_JSON: &str = include_str!("../../../../realhires/assets/lang/ja.json");

static CURRENT: AtomicU8 = AtomicU8::new(0);

thread_local! {
    /// A language for this thread only (tests run several apps in one process).
    static THREAD_LANG: std::cell::Cell<u8> = const { std::cell::Cell::new(u8::MAX) };
}

fn current() -> u8 {
    let t = THREAD_LANG.with(|c| c.get());
    if t != u8::MAX {
        t
    } else {
        CURRENT.load(Ordering::Relaxed)
    }
}

fn index_of(code: &str) -> u8 {
    CODES.iter().position(|c| *c == code).unwrap_or(0) as u8
}

/// Use this language on the calling thread only (tests; "" goes back to the
/// process-wide language).
pub fn set_thread_language(language: &str) {
    let v = if language.is_empty() { u8::MAX } else { index_of(code_of(language)) };
    THREAD_LANG.with(|c| c.set(v));
}

/// The texts of one language.
#[derive(Debug, Default)]
pub struct Catalog {
    pub strings: HashMap<String, String>,
    pub patterns: Vec<(String, String)>,
    pub name: String,
}

impl Catalog {
    pub fn from_json(text: &str) -> Catalog {
        let mut cat = Catalog::default();
        let Ok(v) = json::parse(text) else { return cat };
        if let Some(Value::Object(items)) = v.get("strings") {
            for (k, val) in items {
                if let Some(s) = val.as_str().filter(|s| !s.is_empty()) {
                    cat.strings.insert(k.clone(), s.to_string());
                }
            }
        }
        if let Some(Value::Object(items)) = v.get("patterns") {
            for (k, val) in items {
                if let Some(s) = val.as_str().filter(|s| !s.is_empty()) {
                    if !placeholders(k).is_empty() {
                        cat.patterns.push((k.clone(), s.to_string()));
                    }
                }
            }
        }
        cat.name = v.get("language").and_then(Value::as_str).unwrap_or("").to_string();
        cat
    }

    /// The translation of one whole text (exact, then a pattern), or None.
    pub fn text(&self, text: &str) -> Option<String> {
        if let Some(hit) = self.strings.get(text) {
            return Some(hit.clone());
        }
        for (template, translated) in &self.patterns {
            if let Some(values) = match_template(template, text) {
                let values: Vec<(String, String)> = values
                    .into_iter()
                    .map(|(k, v)| {
                        let t = self.strings.get(&v).cloned().unwrap_or(v);
                        (k, t)
                    })
                    .collect();
                let refs: Vec<(&str, String)> = values.iter().map(|(k, v)| (k.as_str(), v.clone())).collect();
                return Some(fill(translated, &refs));
            }
        }
        None
    }
}

fn catalog(code: u8) -> Option<&'static Catalog> {
    static JA: OnceLock<Catalog> = OnceLock::new();
    match code {
        1 => Some(JA.get_or_init(|| Catalog::from_json(JA_JSON))),
        _ => None,
    }
}

/// The Japanese catalog (tests and tools).
pub fn japanese() -> &'static Catalog {
    catalog(1).expect("the Japanese catalog")
}

fn is_ident_start(c: char) -> bool {
    c.is_ascii_alphabetic() || c == '_'
}

fn is_ident(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

/// The `{name}` placeholders of a text: (start, end, name), byte offsets.
fn spans(text: &str) -> Vec<(usize, usize, &str)> {
    let mut out = vec![];
    let b = text.as_bytes();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'{' {
            let rest = &text[i + 1..];
            let mut chars = rest.char_indices();
            if let Some((_, c0)) = chars.next() {
                if is_ident_start(c0) {
                    let mut end = None;
                    for (j, c) in rest.char_indices().skip(1) {
                        if c == '}' {
                            end = Some(j);
                            break;
                        }
                        if !is_ident(c) {
                            break;
                        }
                    }
                    if let Some(j) = end {
                        out.push((i, i + 1 + j + 1, &text[i + 1..i + 1 + j]));
                        i = i + 1 + j + 1;
                        continue;
                    }
                }
            }
        }
        i += 1;
    }
    out
}

/// The placeholder names of a text, in order.
pub fn placeholders(text: &str) -> Vec<String> {
    spans(text).into_iter().map(|(_, _, n)| n.to_string()).collect()
}

/// Put the values into the `{name}` placeholders; unknown names stay.
pub fn fill(template: &str, values: &[(&str, String)]) -> String {
    if values.is_empty() {
        return template.to_string();
    }
    let mut out = String::with_capacity(template.len() + 16);
    let mut pos = 0;
    for (start, end, name) in spans(template) {
        out.push_str(&template[pos..start]);
        match values.iter().find(|(k, _)| *k == name) {
            Some((_, v)) => out.push_str(v),
            None => out.push_str(&template[start..end]),
        }
        pos = end;
    }
    out.push_str(&template[pos..]);
    out
}

/// The placeholder values when `text` is `template` filled in, else None
/// (each placeholder takes the shortest non-empty text that lets the rest match).
pub fn match_template(template: &str, text: &str) -> Option<Vec<(String, String)>> {
    let sp = spans(template);
    if sp.is_empty() {
        return (text == template).then(Vec::new);
    }
    let mut lits: Vec<&str> = vec![];
    let mut names: Vec<&str> = vec![];
    let mut pos = 0;
    for (start, end, name) in &sp {
        lits.push(&template[pos..*start]);
        names.push(name);
        pos = *end;
    }
    lits.push(&template[pos..]);
    if !text.starts_with(lits[0]) {
        return None;
    }
    let mut got: Vec<(String, String)> = names.iter().map(|n| (n.to_string(), String::new())).collect();

    fn walk(i: usize, pos: usize, text: &str, lits: &[&str], got: &mut Vec<(String, String)>) -> bool {
        let lit = lits[i + 1];
        let last = i == got.len() - 1;
        if last {
            if !text.ends_with(lit) {
                return false;
            }
            let end = text.len() - lit.len();
            if end <= pos {
                return false;
            }
            got[i].1 = text[pos..end].to_string();
            return true;
        }
        // the capture holds at least one character
        let mut start = pos + text[pos..].chars().next().map_or(1, char::len_utf8);
        loop {
            if start > text.len() {
                return false;
            }
            let Some(off) = text[start..].find(lit) else { return false };
            let at = start + off;
            if walk(i + 1, at + lit.len(), text, lits, got) {
                got[i].1 = text[pos..at].to_string();
                return true;
            }
            start = at + text[at..].chars().next().map_or(1, char::len_utf8);
        }
    }

    walk(0, lits[0].len(), text, &lits, &mut got).then_some(got)
}

/// The code of a `language` setting value ("日本語" → "ja"); unknown gives "en".
pub fn code_of(language: &str) -> &'static str {
    if let Some(i) = LANGUAGES.iter().position(|l| *l == language) {
        return CODES[i];
    }
    CODES.iter().find(|c| **c == language).copied().unwrap_or("en")
}

/// Use this language from now on (a setting value or a code); returns its code.
pub fn set_language(language: &str) -> &'static str {
    let code = code_of(if language.is_empty() { DEFAULT_LANGUAGE } else { language });
    CURRENT.store(index_of(code), Ordering::Relaxed);
    code
}

/// The code of the language in use ("en", "ja").
pub fn language() -> &'static str {
    CODES[current() as usize]
}

/// `text` in the language in use (the catalog's text, or `text` itself).
pub fn tr(text: &str) -> &str {
    match catalog(current()) {
        Some(cat) => cat.strings.get(text).map(String::as_str).unwrap_or(text),
        None => text,
    }
}

/// A name from the library: translated only when it is one of the core's
/// placeholders for a missing tag ("Unknown Album"), never a real name.
pub fn tr_name(name: &str) -> &str {
    if PLACEHOLDER_NAMES.contains(&name) {
        tr(name)
    } else {
        name
    }
}

fn part_join(code: &str) -> &'static str {
    if code == "ja" {
        "；"
    } else {
        "; "
    }
}

fn sentence_join(code: &str) -> &'static str {
    if code == "ja" {
        ""
    } else {
        " "
    }
}

/// Split where Python's `(?<=[.!?])\s+(?=[A-Z"'(])` splits.
fn sentences(text: &str) -> Vec<&str> {
    let mut out = vec![];
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    let mut start = 0;
    let mut i = 0;
    while i < chars.len() {
        let (at, c) = chars[i];
        if c.is_whitespace() && i > 0 && matches!(chars[i - 1].1, '.' | '!' | '?') {
            let mut j = i;
            while j < chars.len() && chars[j].1.is_whitespace() {
                j += 1;
            }
            if j < chars.len() && (chars[j].1.is_ascii_uppercase() || matches!(chars[j].1, '"' | '\'' | '(')) {
                out.push(&text[start..at]);
                start = chars[j].0;
                i = j;
                continue;
            }
            i = j;
            continue;
        }
        i += 1;
    }
    out.push(&text[start..]);
    out
}

/// A finished text from the shared core in the language in use: the whole
/// text, else its "; " parts, else its sentences (untranslated parts stay).
pub fn tr_text(text: &str) -> String {
    let code = language();
    let Some(cat) = catalog(current()) else { return text.to_string() };
    if text.is_empty() {
        return String::new();
    }
    if let Some(hit) = cat.text(text) {
        return hit;
    }
    if text.contains("; ") {
        let parts: Vec<&str> = text.split("; ").collect();
        let out: Vec<Option<String>> = parts.iter().map(|p| cat.text(p)).collect();
        if out.iter().any(Option::is_some) {
            return parts
                .iter()
                .zip(out)
                .map(|(p, o)| o.unwrap_or_else(|| p.to_string()))
                .collect::<Vec<_>>()
                .join(part_join(code));
        }
    }
    let ss = sentences(text);
    if ss.len() > 1 {
        let out: Vec<Option<String>> = ss.iter().map(|s| cat.text(s)).collect();
        if out.iter().any(Option::is_some) {
            return ss
                .iter()
                .zip(out)
                .map(|(s, o)| o.unwrap_or_else(|| s.to_string()))
                .collect::<Vec<_>>()
                .join(sentence_join(code));
        }
    }
    text.to_string()
}

/// `tr!("Text")` translates a text; `tr!("Playing from {name}", name = x)`
/// translates it and fills in the placeholders.
#[macro_export]
macro_rules! tr {
    ($s:literal) => {
        $crate::i18n::tr($s)
    };
    ($s:literal, $($k:ident = $v:expr),+ $(,)?) => {
        $crate::i18n::fill($crate::i18n::tr($s), &[$((stringify!($k), ($v).to_string())),+])
    };
}

/// `n_!("Text")` marks a literal in a table as a text to translate; it is
/// translated where it is shown, with `i18n::tr()`.
#[macro_export]
macro_rules! n_ {
    ($s:literal) => {
        $s
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn templates_match_and_fill_as_in_python() {
        assert_eq!(placeholders("{n} of {total} songs"), vec!["n", "total"]);
        assert_eq!(fill("{n} of {total}", &[("n", "3".into()), ("total", "12".into())]), "3 of 12");
        assert_eq!(fill("{n} and {x}", &[("n", "3".into())]), "3 and {x}");
        let got = match_template("Upsampled from {src} kHz: nothing above {top} kHz", "Upsampled from 44.1 kHz: nothing above 22.05 kHz");
        assert_eq!(got, Some(vec![("src".into(), "44.1".into()), ("top".into(), "22.05".into())]));
        assert_eq!(match_template("{a} kHz", " kHz"), None);
        assert_eq!(match_template("{a}, {b}", "x, y, z"), Some(vec![("a".into(), "x".into()), ("b".into(), "y, z".into())]));
        assert_eq!(match_template("曲 {n} 件", "曲 3 件"), Some(vec![("n".into(), "3".into())]));
        assert_eq!(sentences("One 1.5 kHz. Two! three. (Four)"), vec!["One 1.5 kHz.", "Two! three.", "(Four)"]);
    }

    #[test]
    fn languages_switch() {
        assert_eq!(code_of("日本語"), "ja");
        assert_eq!(code_of("Klingon"), "en");
        set_thread_language("English");
        assert_eq!(tr("Library"), "Library");
        set_thread_language("日本語");
        assert_eq!(tr("Library"), "ライブラリ");
        assert_eq!(tr_name("Unknown Album"), tr("Unknown Album"));
        set_thread_language("");
        assert!(!japanese().strings.is_empty());
    }
}
