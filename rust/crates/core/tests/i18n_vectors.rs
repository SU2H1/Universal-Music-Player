//! The translation core against Python's answers (`python_i18n.json`, from
//! `rust/tools/gen_i18n_vectors.py`): the same catalog gives the same texts.

use realhires::i18n;
use realhires::json::{self, Value};

#[test]
fn translations_match_python() {
    let data = json::parse(include_str!("python_i18n.json")).unwrap();
    i18n::set_thread_language("日本語");
    for c in data.get("cases").and_then(Value::as_array).unwrap() {
        let text = c.get("text").and_then(Value::as_str).unwrap();
        assert_eq!(i18n::tr_text(text), c.get("tr_text").and_then(Value::as_str).unwrap(), "tr_text({text:?})");
        assert_eq!(i18n::tr(text), c.get("tr").and_then(Value::as_str).unwrap(), "tr({text:?})");
        assert_eq!(i18n::tr_name(text), c.get("tr_name").and_then(Value::as_str).unwrap(), "tr_name({text:?})");
    }
    for m in data.get("matches").and_then(Value::as_array).unwrap() {
        let m = m.as_array().unwrap();
        let (t, s) = (m[0].as_str().unwrap(), m[1].as_str().unwrap());
        let got = i18n::match_template(t, s);
        match &m[2] {
            Value::Null => assert_eq!(got, None, "{t} / {s}"),
            Value::Object(want) => {
                let mut got = got.expect("a match");
                got.sort();
                let mut want: Vec<(String, String)> =
                    want.iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect();
                want.sort();
                assert_eq!(got, want, "{t} / {s}");
            }
            other => panic!("{other:?}"),
        }
    }
    i18n::set_thread_language("");
}
