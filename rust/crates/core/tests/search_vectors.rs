//! Searching the web (2.3) against Python's answers (`rust/tools/gen_player_vectors.py`
//! writes `python_search.json`): Search Cover Online's Google Images address
//! (`covers::cover_search_url()`), the Properties window's Search buttons
//! (`tagedit::web_search_url()`, both languages) and what a copied text fills in
//! (`tagedit::clipboard_value()`).

use realhires::covers;
use realhires::json::{self, Value};
use realhires::tagedit;

fn load() -> Value {
    let path = format!("{}/tests/python_search.json", env!("CARGO_MANIFEST_DIR"));
    json::parse(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn list(v: &Value) -> &Vec<Value> {
    v.as_array().expect("a list")
}

fn text(v: &Value) -> &str {
    v.as_str().expect("a text")
}

#[test]
fn cover_searches_are_the_same_as_in_python() {
    let d = load();
    let cases = list(d.get("cover").unwrap());
    assert!(cases.len() >= 10);
    for c in cases {
        let a: Vec<&str> = list(c.get("args").unwrap()).iter().map(text).collect();
        assert_eq!(covers::cover_search_query(a[0], a[1], a[2], a[3]), text(c.get("query").unwrap()), "{a:?}");
        assert_eq!(covers::cover_search_url(a[0], a[1], a[2], a[3]), text(c.get("url").unwrap()), "{a:?}");
    }
    assert_eq!(
        covers::cover_search_url("The Paper Boats", "", "City Rain", ""),
        "https://www.google.com/search?tbm=isch&q=The+Paper+Boats+City+Rain"
    );
}

#[test]
fn web_searches_are_the_same_as_in_python() {
    let d = load();
    let cases = list(d.get("web").unwrap());
    assert!(cases.len() > 100);
    for c in cases {
        let values: Vec<(&str, &str)> =
            c.get("values").unwrap().as_object().unwrap().iter().map(|(k, v)| (k.as_str(), text(v))).collect();
        let (field, language) = (text(c.get("field").unwrap()), text(c.get("language").unwrap()));
        assert_eq!(tagedit::web_search_query(field, &values, language), text(c.get("query").unwrap()), "{field} {language} {values:?}");
        assert_eq!(tagedit::web_search_url(field, &values, language), text(c.get("url").unwrap()), "{field} {language} {values:?}");
    }
    for (lang, names) in d.get("names").unwrap().as_object().unwrap() {
        for (field, name) in names.as_object().unwrap() {
            assert_eq!(tagedit::search_name(field, lang), text(name), "{field} {lang}");
        }
    }
    let watch: Vec<i64> = list(d.get("watch").unwrap()).iter().map(|v| v.as_i64().unwrap()).collect();
    assert_eq!(watch, [tagedit::WATCH_SECONDS as i64, tagedit::WATCH_POLL_MS as i64]);
}

#[test]
fn copied_texts_fill_in_what_python_fills_in() {
    let d = load();
    for c in list(d.get("clipboard").unwrap()) {
        let pair = list(c);
        assert_eq!(tagedit::clipboard_value(text(&pair[0])), text(&pair[1]), "{:?}", text(&pair[0]));
    }
}
