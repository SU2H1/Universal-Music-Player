//! Custom tabs (2.3) against Python's answers (`rust/tools/gen_player_vectors.py` →
//! `python_tabs.json`): the fields' values, rule evaluation and sorting on invented
//! tracks, every message of `check_tab()`, names and file names, the values typed
//! into the tab window, the bytes of tab files, and the tabs folder (loading, saving,
//! importing and deleting).

use std::collections::BTreeMap;
use std::path::Path;

use realhires::json::{self, Value};
use realhires::musiclib::Track;
use realhires::smarttabs::{self, FieldValue, Tab, TabContext};

fn load() -> Value {
    let path = format!("{}/tests/python_tabs.json", env!("CARGO_MANIFEST_DIR"));
    json::parse(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn text(v: &Value) -> String {
    v.as_str().unwrap_or("").to_string()
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array().unwrap().iter().map(text).collect()
}

fn arr<'a>(v: &'a Value, key: &str) -> &'a Vec<Value> {
    v.get(key).unwrap_or_else(|| panic!("no {key}")).as_array().unwrap()
}

fn context(data: &Value) -> TabContext {
    // 2.4: the moods of some of the songs
    let moods: std::collections::HashMap<String, realhires::moods::Mood> = data
        .get("moods")
        .unwrap()
        .as_object()
        .unwrap()
        .iter()
        .map(|(p, m)| {
            let text = |k: &str| m.get(k).unwrap().as_str().unwrap().to_string();
            (p.clone(), realhires::moods::Mood { bpm: m.get("bpm").unwrap().as_i64().unwrap(), mood: text("mood"), tempo: text("tempo"), ..Default::default() })
        })
        .collect();
    TabContext::new(&strings(data.get("liked").unwrap()), &strings(data.get("disliked").unwrap()), data.get("now").unwrap().as_f64().unwrap())
        .with_moods(std::sync::Arc::new(moods))
}

fn tracks(data: &Value) -> Vec<Track> {
    arr(data, "tracks").iter().map(|t| Track::from_json(t).unwrap()).collect()
}

fn same_value(got: &FieldValue, want: &Value) -> bool {
    match (got, want) {
        (FieldValue::Text(a), Value::Str(b)) => a == b,
        (FieldValue::Number(a), Value::Int(_) | Value::Float(_)) => *a == want.as_f64().unwrap(),
        (FieldValue::YesNo(a), Value::Bool(b)) => a == b,
        _ => false,
    }
}

/// A tab as Python's check gives it, for comparing (its file is left out).
fn tab_json(t: &Tab) -> Value {
    t.to_json()
}

#[test]
fn field_values_match_python() {
    let data = load();
    let ctx = context(&data);
    for (t, values) in tracks(&data).iter().zip(arr(&data, "values")) {
        for field in smarttabs::FIELDS {
            let want = values.get(field).unwrap();
            let got = smarttabs::field_value(field, t, &ctx);
            assert!(same_value(&got, want), "{} {field}: {got:?} != {want:?}", t.path);
        }
    }
}

#[test]
fn rules_and_sorting_match_python() {
    let data = load();
    let ctx = context(&data);
    let tracks = tracks(&data);
    for case in arr(&data, "evals") {
        let tab = smarttabs::check_tab(case.get("tab").unwrap()).unwrap();
        let songs = smarttabs::tab_songs(&tab, &tracks, &ctx);
        assert_eq!(songs, strings(case.get("songs").unwrap()), "{}", tab.name);
        let want: Vec<bool> = arr(case, "matches").iter().map(|b| b.as_bool().unwrap()).collect();
        let got: Vec<bool> = tracks.iter().map(|t| smarttabs::matches(&tab, t, &ctx)).collect();
        assert_eq!(got, want, "{}", tab.name);
    }
}

#[test]
fn every_check_message_matches_python() {
    let data = load();
    for case in arr(&data, "checks") {
        let input = case.get("input").unwrap();
        match (smarttabs::check_tab(input), case.get("error").unwrap()) {
            (Ok(t), Value::Null) => assert_eq!(tab_json(&t), *case.get("tab").unwrap(), "{input:?}"),
            (Err(e), Value::Str(want)) => assert_eq!(&e.0, want, "{input:?}"),
            (got, want) => panic!("{input:?}: wanted {want:?}, got {got:?}"),
        }
    }
    let known: Vec<Tab> = ["Hi-Res", "Rock & Roll", "Ünïcode"].iter().map(|n| Tab::new(n)).collect();
    for c in arr(&data, "names") {
        let c = c.as_array().unwrap();
        match (smarttabs::check_name(&text(&c[0]), &known, &text(&c[1])), &c[2], &c[3]) {
            (Ok(n), Value::Str(want), Value::Null) => assert_eq!(&n, want),
            (Err(e), Value::Null, Value::Str(want)) => assert_eq!(&e.0, want),
            (got, ..) => panic!("{:?}: {got:?}", c[0]),
        }
    }
    for c in arr(&data, "uniques") {
        let c = c.as_array().unwrap();
        assert_eq!(smarttabs::unique_name(&text(&c[0]), &strings(&c[1])), text(&c[2]), "{:?}", c[0]);
    }
}

#[test]
fn names_values_and_texts_match_python() {
    let data = load();
    for c in arr(&data, "file_names") {
        let c = c.as_array().unwrap();
        assert_eq!(smarttabs::tab_file_name(&text(&c[0])), text(&c[1]), "{:?}", c[0]);
    }
    for c in arr(&data, "texts") {
        let tab = smarttabs::check_tab(c.get("tab").unwrap()).unwrap();
        let want = text(c.get("text").unwrap());
        assert_eq!(smarttabs::tab_text(&tab).unwrap(), want);
        // and the file reads back as the same tab
        assert_eq!(smarttabs::parse_tab(&want).unwrap(), tab);
    }
    for c in arr(&data, "parses") {
        let c = c.as_array().unwrap();
        let got = smarttabs::parse_value(&text(&c[0]), &text(&c[1]));
        assert_eq!(got, c[2], "{:?} {:?}", c[0], c[1]);
    }
    for c in arr(&data, "value_texts") {
        let c = c.as_array().unwrap();
        assert_eq!(smarttabs::value_text(&c[0]), text(&c[1]), "{:?}", c[0]);
    }
    for c in arr(&data, "changes") {
        let rule = smarttabs::check_rule(c.get("rule").unwrap(), 1).unwrap();
        let got = smarttabs::change_field(&rule, &text(c.get("field").unwrap()));
        assert_eq!(got.to_json(), *c.get("out").unwrap(), "{rule:?}");
    }
    let labels = data.get("labels").unwrap();
    for field in smarttabs::FIELDS {
        assert_eq!(smarttabs::field_label(field), text(labels.get("fields").unwrap().get(field).unwrap()));
        let ops = labels.get("ops").unwrap().get(field).unwrap().as_object().unwrap();
        let mine: Vec<&str> = smarttabs::ops_for(field).to_vec();
        assert_eq!(mine, ops.iter().map(|(k, _)| k.as_str()).collect::<Vec<_>>(), "{field}");
        for (op, label) in ops {
            assert_eq!(smarttabs::op_label(field, op), text(label), "{field} {op}");
        }
        assert_eq!(smarttabs::kind_of(field), text(labels.get("kinds").unwrap().get(field).unwrap()));
        assert_eq!(smarttabs::new_rule(field).to_json(), *labels.get("defaults").unwrap().get(field).unwrap());
    }
    assert_eq!(smarttabs::kind_of("nope"), "");
    for mode in smarttabs::MATCH_MODES {
        assert_eq!(smarttabs::match_label(mode), text(labels.get("match").unwrap().get(mode).unwrap()));
    }
    let templates: Vec<Value> = smarttabs::templates().iter().map(Tab::to_json).collect();
    assert_eq!(&templates, labels.get("templates").unwrap().as_array().unwrap());
}

fn listing(d: &Path) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    if let Ok(entries) = std::fs::read_dir(d) {
        for e in entries.flatten() {
            let name = e.file_name().into_string().unwrap();
            out.insert(name, std::fs::read_to_string(e.path()).unwrap());
        }
    }
    out
}

fn want_listing(v: &Value) -> BTreeMap<String, String> {
    v.as_object().unwrap().iter().map(|(k, x)| (k.clone(), text(x))).collect()
}

#[test]
fn the_tabs_folder_matches_python() {
    let data = load();
    let tmp = std::env::temp_dir().join(format!("rhr-tabs-vectors-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    // which files of a folder are tabs
    let d = tmp.join("folder");
    std::fs::create_dir_all(d.join("dir.json")).unwrap();
    let tab = |name: &str| smarttabs::tab_text(&Tab::new(name)).unwrap();
    let files: Vec<(&str, String)> = vec![
        ("b.json", tab("Hi-Res")),
        ("A.JSON", tab("hi-res")),
        ("c.json", "nope".into()),
        ("d.json", format!("{{\"format\": \"{}\", \"version\": 3, \"name\": \"Future\"}}", smarttabs::TAB_FORMAT)),
        (".hidden.json", tab("Hidden")),
        ("e.txt", tab("Text")),
        ("f.json", format!("\u{feff}{}", tab("Bom"))),
        ("g.json", tab("HI-RES")),
        (
            "h.json",
            format!(
                "{{\"format\": \"{}\", \"version\": 1, \"name\": \"Bad\", \"rules\": [{{\"field\": \"colour\", \"op\": \"is\", \"value\": \"red\"}}]}}",
                smarttabs::TAB_FORMAT
            ),
        ),
        ("i.json", "[1, 2]".into()),
        ("j.json", format!("{{\"format\": \"{}\", \"version\": 1, \"name\": \"Short\"}}", smarttabs::TAB_FORMAT)),
    ];
    for (n, t) in &files {
        std::fs::write(d.join(n), t).unwrap();
    }
    std::fs::write(d.join("big.json"), vec![b' '; smarttabs::MAX_FILE_BYTES as usize + 1]).unwrap();
    std::fs::write(d.join("latin1.json"), b"\xff\xfe{").unwrap();
    let (tabs, skipped) = smarttabs::load_tabs(Some(&d));
    let folder = data.get("folder").unwrap();
    let want = arr(folder, "tabs");
    assert_eq!(tabs.len(), want.len(), "{tabs:?}");
    for (t, w) in tabs.iter().zip(want) {
        let file = t.file.as_ref().unwrap().file_name().unwrap().to_string_lossy().into_owned();
        assert_eq!(file, text(w.get("file").unwrap()));
        let mut w = w.clone();
        if let Value::Object(o) = &mut w {
            o.retain(|(k, _)| k != "file");
        }
        assert_eq!(t.to_json(), w);
    }
    let want: Vec<(String, String)> = arr(folder, "skipped")
        .iter()
        .map(|s| {
            let s = s.as_array().unwrap();
            (text(&s[0]), text(&s[1]))
        })
        .collect();
    assert_eq!(skipped, want);

    // a series of saves, imports and deletes
    let root = tmp.join("ops");
    let d = root.join("tabs");
    let outside = root.join("outside");
    std::fs::create_dir_all(&outside).unwrap();
    for (o, res) in arr(&data, "ops").iter().zip(arr(&data, "op_results")) {
        let op = text(o.get("op").unwrap());
        let got: Result<Option<Tab>, String> = match op.as_str() {
            "save" => {
                let old = o.get("old").map(|n| d.join(text(n)));
                match smarttabs::check_tab(o.get("tab").unwrap()) {
                    Err(e) => Err(e.0),
                    Ok(tab) => smarttabs::save_tab(&tab, Some(&d), old.as_deref()).map(Some).map_err(|e| e.0),
                }
            }
            "import" => {
                let name = text(o.get("file").unwrap());
                let src = if name == "inside" {
                    d.join("Hi-Res.json")
                } else {
                    let p = outside.join(&name);
                    std::fs::write(&p, text(o.get("text").unwrap())).unwrap();
                    p
                };
                smarttabs::import_tab(&src, Some(&d)).map(Some).map_err(|e| e.0)
            }
            _ => {
                let mut t = Tab::new("x");
                t.file = Some(d.join(text(o.get("file").unwrap())));
                assert_eq!(smarttabs::delete_tab(&t), res.get("deleted").unwrap().as_bool().unwrap());
                Ok(None)
            }
        };
        match got {
            Ok(Some(t)) => {
                assert_eq!(t.name, text(res.get("name").unwrap()), "{o:?}");
                let file = t.file.as_ref().unwrap().file_name().unwrap().to_string_lossy().into_owned();
                assert_eq!(file, text(res.get("file").unwrap()), "{o:?}");
            }
            Ok(None) => {}
            Err(e) => assert_eq!(e, text(res.get("error").unwrap()), "{o:?}"),
        }
        assert_eq!(listing(&d), want_listing(res.get("listing").unwrap()), "{o:?}");
    }
    let _ = std::fs::remove_dir_all(&tmp);
}

#[test]
fn tab_files_are_recognised() {
    let tmp = std::env::temp_dir().join(format!("rhr-tabs-recognised-{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();
    let tab = tmp.join("t.json");
    std::fs::write(&tab, smarttabs::tab_text(&smarttabs::templates()[0]).unwrap()).unwrap();
    let broken = tmp.join("b.json");
    std::fs::write(&broken, format!("{{\"format\": \"{}\"}}", smarttabs::TAB_FORMAT)).unwrap();
    let theme = tmp.join("theme.json");
    std::fs::write(&theme, "{\"name\": \"Neon\", \"colors\": {}}").unwrap();
    assert!(smarttabs::is_tab_file(&tab) && smarttabs::is_tab_file(&broken));
    assert!(!smarttabs::is_tab_file(&theme) && !smarttabs::is_tab_file(&tmp.join("missing.json")));
    let _ = std::fs::remove_dir_all(&tmp);
}
