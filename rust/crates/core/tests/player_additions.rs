//! The 2.0 additions of the player core against Python's answers
//! (`rust/tools/gen_player_vectors.py`): statistics and groupings, EQ preset
//! files, themes and the player state with playlists, dislikes and presets.

use realhires::eq::{self, EqState};
use realhires::json::{self, Value};
use realhires::musiclib::{self, Track};
use realhires::playerstate::{self, PlayerState};
use realhires::stats::{self, LocalParts, Play};
use realhires::themes;

/// Equal JSON, numbers within rounding (an int equals the same float).
fn same(a: &Value, b: &Value, at: &str) {
    match (a, b) {
        (Value::Object(x), Value::Object(y)) => {
            let mut kx: Vec<&String> = x.iter().map(|(k, _)| k).collect();
            let mut ky: Vec<&String> = y.iter().map(|(k, _)| k).collect();
            kx.sort();
            ky.sort();
            assert_eq!(kx, ky, "keys at {at}");
            for (k, v) in x {
                same(v, b.get(k).unwrap(), &format!("{at}.{k}"));
            }
        }
        (Value::Array(x), Value::Array(y)) => {
            assert_eq!(x.len(), y.len(), "length at {at}");
            for (i, (p, q)) in x.iter().zip(y).enumerate() {
                same(p, q, &format!("{at}[{i}]"));
            }
        }
        _ if a.as_f64().is_some() && b.as_f64().is_some() && !matches!(a, Value::Bool(_)) => {
            let (p, q) = (a.as_f64().unwrap(), b.as_f64().unwrap());
            assert!((p - q).abs() <= 1e-9 * p.abs().max(q.abs()).max(1.0), "{p} != {q} at {at}");
        }
        _ => assert_eq!(a, b, "at {at}"),
    }
}

/// A POSIX path of the vectors as the library holds it on this system.
fn native(p: &str) -> String {
    if cfg!(windows) && p.starts_with('/') {
        realhires::paths::abspath(p)
    } else {
        p.to_string()
    }
}

/// `native()` applied to every path string inside a vector value.
fn natives(v: &Value) -> Value {
    match v {
        Value::Str(s) => Value::Str(native(s)),
        Value::Array(a) => Value::Array(a.iter().map(natives).collect()),
        other => other.clone(),
    }
}

fn load(name: &str) -> Value {
    let path = format!("{}/tests/{name}", env!("CARGO_MANIFEST_DIR"));
    json::parse(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

fn strings(v: &Value) -> Value {
    v.clone()
}

#[test]
fn stats_groupings_and_folders_match_python() {
    let data = load("python_stats.json");
    let tracks: Vec<Track> = data.get("tracks").unwrap().as_array().unwrap().iter().filter_map(Track::from_json).collect();
    let plays_of = |key: &str| -> Vec<Play> {
        data.get(key)
            .unwrap()
            .as_array()
            .unwrap()
            .iter()
            .map(|p| Play {
                path: s(p, "path"),
                started: p.get("started").unwrap().as_f64().unwrap(),
                seconds: p.get("seconds").unwrap().as_f64().unwrap(),
                duration: p.get("duration").unwrap().as_f64().unwrap(),
                title: s(p, "title"),
                artist: s(p, "artist"),
                album: s(p, "album"),
                album_artist: s(p, "album_artist"),
                genre: s(p, "genre"),
                year: s(p, "year"),
                composer: s(p, "composer"),
                place: None,
            })
            .collect()
    };
    let plays = plays_of("plays");
    let offset = data.get("offset").unwrap().as_f64().unwrap();
    let tokyo = move |ts: f64| {
        let t = chrono::DateTime::from_timestamp((ts + offset).floor() as i64, 0).unwrap().naive_utc();
        use chrono::{Datelike, Timelike};
        LocalParts {
            year: t.year(),
            month: t.month(),
            day: t.day(),
            hour: t.hour(),
            minute: t.minute(),
            wday: t.weekday().num_days_from_monday(),
        }
    };
    let cards = |st: &stats::Stats| {
        Value::Array(
            stats::cards(st)
                .into_iter()
                .map(|(a, b, c)| Value::Array(vec![Value::Str(a), Value::Str(b), Value::Str(c)]))
                .collect(),
        )
    };
    let st = stats::compute(&plays, &tokyo);
    same(&st.to_json(), data.get("stats").unwrap(), "stats");
    same(&cards(&st), data.get("cards").unwrap(), "cards");
    let half = stats::compute(&plays[..7], &tokyo);
    same(&half.to_json(), data.get("stats_7").unwrap(), "stats_7");
    same(&cards(&half), data.get("cards_7").unwrap(), "cards_7");
    same(&cards(&stats::compute(&[], &tokyo)), data.get("cards_empty").unwrap(), "cards_empty");
    let hours: Vec<Value> = (0..24).map(|h| Value::Str(stats::hour_label(h))).collect();
    same(&Value::Array(hours), data.get("hours").unwrap(), "hours");

    // the calendar of Your Stats (2.1)
    let mut cal_plays = plays.clone();
    cal_plays.extend(plays_of("calendar_plays"));
    cal_plays.sort_by(|a, b| a.started.partial_cmp(&b.started).unwrap());
    let months = data.get("calendar").unwrap().as_array().unwrap();
    assert!(months.len() >= 5);
    for c in months {
        let y = c.get("year").unwrap().as_f64().unwrap() as i32;
        let m = c.get("month").unwrap().as_f64().unwrap() as u32;
        let at = format!("calendar {y}-{m}");
        assert_eq!(stats::month_title(y, m), s(c, "title"), "{at}");
        let grid = Value::Array(
            stats::month_grid(y, m)
                .iter()
                .map(|w| Value::Array(w.iter().map(|d| Value::Int(*d as i64)).collect()))
                .collect(),
        );
        same(&grid, c.get("grid").unwrap(), &format!("{at} grid"));
        let minutes = stats::month_minutes(&cal_plays, y, m, &tokyo);
        same(&Value::Array(minutes.iter().map(|x| Value::Float(*x)).collect()), c.get("minutes").unwrap(), &format!("{at} minutes"));
        let levels = Value::Array(stats::month_levels(&minutes).iter().map(|l| Value::Int(*l as i64)).collect());
        same(&levels, c.get("levels").unwrap(), &format!("{at} levels"));
        for d in c.get("days").unwrap().as_array().unwrap() {
            let day = d.get("day").unwrap().as_f64().unwrap() as u32;
            assert_eq!(stats::day_title(y, m, day), s(d, "title"), "{at}-{day}");
            let rows = stats::day_plays(&cal_plays, y, m, day, &tokyo);
            same(&Value::Array(rows.iter().map(|r| r.to_json()).collect()), d.get("plays").unwrap(), &format!("{at}-{day}"));
            assert_eq!(stats::day_summary(&rows), s(d, "summary"), "{at}-{day}");
        }
    }
    for pair in data.get("minutes_labels").unwrap().as_array().unwrap() {
        let pair = pair.as_array().unwrap();
        assert_eq!(stats::minutes_label(pair[0].as_f64().unwrap()), pair[1].as_str().unwrap());
    }
    for row in data.get("shift_month").unwrap().as_array().unwrap() {
        let n: Vec<i64> = row.as_array().unwrap().iter().map(|v| v.as_f64().unwrap() as i64).collect();
        assert_eq!(stats::shift_month(n[0] as i32, n[1] as u32, n[2] as i32), (n[3] as i32, n[4] as u32), "{n:?}");
    }

    let groups = data.get("groups").unwrap();
    for (name, list) in [
        ("artists", musiclib::artists(&tracks)),
        ("album_artists", musiclib::album_artists(&tracks)),
        ("genres", musiclib::genres(&tracks)),
        ("composers", musiclib::composers(&tracks)),
        ("years", musiclib::years(&tracks)),
    ] {
        let got = Value::Array(
            list.iter()
                .map(|g| {
                    Value::Array(vec![
                        Value::Str(g.name.clone()),
                        Value::Array(g.tracks.iter().map(|t| Value::Str(t.path.clone())).collect()),
                    ])
                })
                .collect(),
        );
        same(&got, groups.get(name).unwrap(), name);
    }
    // the vectors hold POSIX paths; on Windows the library holds them as
    // os.path.abspath() makes them ("D:\\lib\\A"), so compare in that form
    let tracks: Vec<Track> = tracks
        .iter()
        .map(|t| {
            let mut t = t.clone();
            t.path = native(&t.path);
            t
        })
        .collect();
    for (folder, want) in data.get("folders").unwrap().as_object().unwrap() {
        let want = &natives(want);
        let (subs, songs) = musiclib::folder_view(&tracks, &native(folder));
        let got = Value::Array(vec![
            Value::Array(
                subs.into_iter()
                    .map(|(p, n, c)| Value::Array(vec![Value::Str(p), Value::Str(n), Value::Int(c as i64)]))
                    .collect(),
            ),
            Value::Array(songs.into_iter().map(|t| Value::Str(t.path)).collect()),
        ]);
        same(&got, want, folder);
    }
    let ls = stats::library_stats(&tracks);
    let pairs = |v: &[(String, usize)]| {
        Value::Array(v.iter().map(|(k, n)| Value::Array(vec![Value::Str(k.clone()), Value::Int(*n as i64)])).collect())
    };
    let got = json::Obj::new()
        .put("songs", ls.songs as i64)
        .put("albums", ls.albums as i64)
        .put("artists", ls.artists as i64)
        .put("seconds", ls.seconds)
        .put("bytes", ls.bytes)
        .put("formats", pairs(&ls.formats))
        .put("qualities", pairs(&ls.qualities))
        .put(
            "rates",
            Value::Array(ls.rates.iter().map(|(r, n)| Value::Array(vec![Value::Int(*r), Value::Int(*n as i64)])).collect()),
        )
        .put("bits", pairs(&ls.bits))
        .build();
    same(&got, data.get("library").unwrap(), "library");
}

#[test]
fn eq_files_match_python() {
    let data = load("python_eqfiles.json");
    for e in data.get("exports").unwrap().as_array().unwrap() {
        let st = EqState::from_json(e.get("state").unwrap());
        let name = s(e, "name");
        assert_eq!(eq::export_apo(&name, &st), s(e, "apo"), "{name}");
        let ours = json::parse(&eq::export_json(&name, &st)).unwrap();
        same(&ours, &json::parse(&s(e, "json")).unwrap(), &name);
    }
    for case in data.get("imports").unwrap().as_array().unwrap() {
        let text = s(case, "text");
        match eq::import_eq(&text, "Imported") {
            Ok((name, st, notes)) => {
                assert_eq!(name, s(case, "name"), "{text:?}");
                same(&st.to_json(), case.get("state").expect(&text), &text);
                same(&Value::Array(notes.into_iter().map(Value::Str).collect()), case.get("notes").unwrap(), &text);
            }
            Err(e) => assert_eq!(e.0, s(case, "error"), "{text:?}"),
        }
    }
}

#[test]
fn themes_match_python() {
    let data = load("python_themes.json");
    let builtin = data.get("builtin").unwrap();
    assert_eq!(builtin.as_object().unwrap().len(), themes::BUILTIN.len());
    for (name, t) in builtin.as_object().unwrap() {
        let ours = themes::builtin(name).unwrap();
        assert_eq!(ours.dark, t.get("dark").unwrap().as_bool().unwrap(), "{name}");
        for (i, k) in themes::COLOR_KEYS.iter().enumerate() {
            assert_eq!(ours.colors[i], s(t.get("colors").unwrap(), k), "{name} {k}");
        }
    }
    let custom: Vec<themes::Theme> =
        data.get("custom").unwrap().as_array().unwrap().iter().filter_map(themes::normalize).collect();
    same(&Value::Array(themes::names(&custom).into_iter().map(Value::Str).collect()), data.get("names").unwrap(), "names");
    for c in data.get("cases").unwrap().as_array().unwrap() {
        let got = themes::normalize(c.get("input").unwrap()).map(|t| t.to_json()).unwrap_or(Value::Null);
        same(&got, c.get("output").unwrap(), "normalize");
    }
    for c in data.get("imports").unwrap().as_array().unwrap() {
        match themes::import_theme(&s(c, "text"), &custom) {
            Ok(t) => same(&t.to_json(), c.get("theme").unwrap(), "import"),
            Err(e) => assert_eq!(e.0, s(c, "error")),
        }
    }
    for c in data.get("checks").unwrap().as_array().unwrap() {
        let c = c.as_array().unwrap();
        match themes::check_name(c[0].as_str().unwrap(), &custom, "") {
            Ok(n) => assert_eq!(Some(n.as_str()), c[1].as_str()),
            Err(e) => assert_eq!(Some(e.0.as_str()), c[2].as_str()),
        }
    }
    let copies = data.get("copy").unwrap().as_array().unwrap();
    assert_eq!(themes::copy_name("Midnight", &custom), copies[0].as_str().unwrap());
    assert_eq!(themes::copy_name("My Theme", &custom), copies[1].as_str().unwrap());
    assert_eq!(themes::export_theme(&custom[0]), s(&data, "export"));
    // 2.3: the extra colours, derived or set, and theme files with and without them
    let keys: Vec<String> = data.get("extra_keys").unwrap().as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
    assert_eq!(keys, themes::EXTRA_COLOR_KEYS.map(str::to_string).to_vec());
    let labels: Vec<&str> = data.get("extra_labels").unwrap().as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
    let ours: Vec<&str> = themes::COLOR_KEYS.iter().chain(themes::EXTRA_COLOR_KEYS.iter()).map(|k| themes::any_color_label(k)).collect();
    assert_eq!(labels, ours);
    let extra_custom = themes::normalize(data.get("extra_custom").unwrap()).unwrap();
    same(&extra_custom.to_json(), data.get("extra_custom").unwrap(), "extra_custom");
    assert_eq!(themes::export_theme(&extra_custom), s(&data, "extra_export"));
    let pale = themes::normalize(&json::parse(r##"{"name": "Pale accent", "dark": true, "colors": {"accent": "#f0f0f0", "secondary": "#808080"}}"##).unwrap()).unwrap();
    let all = data.get("all_colors").unwrap();
    let mut checked = 0;
    for (name, want) in all.as_object().unwrap() {
        let t = themes::builtin(name)
            .or_else(|| custom.iter().find(|t| &t.name == name).cloned())
            .or_else(|| (extra_custom.name == *name).then(|| extra_custom.clone()))
            .or_else(|| (pale.name == *name).then(|| pale.clone()))
            .unwrap_or_else(|| panic!("no theme {name}"));
        let got = themes::colors(&t);
        assert_eq!(got.len(), want.as_object().unwrap().len(), "{name}");
        for (k, v) in got {
            assert_eq!(Some(v.as_str()), want.get(k).and_then(Value::as_str), "{name} {k}");
            assert_eq!(t.any_color(k), v, "{name} {k}");
        }
        checked += 1;
    }
    assert_eq!(checked, themes::BUILTIN.len() + 4);
    for c in data.get("extra_imports").unwrap().as_array().unwrap() {
        let t = themes::import_theme(&s(c, "text"), &custom).unwrap();
        same(&t.to_json(), c.get("theme").unwrap(), "extra import");
        for (k, v) in themes::colors(&t) {
            assert_eq!(Some(v.as_str()), c.get("colors").unwrap().get(k).and_then(Value::as_str), "import {k}");
        }
        assert_eq!(themes::export_theme(&t), s(c, "export"));
    }
    for m in data.get("mixes").unwrap().as_array().unwrap() {
        let m = m.as_array().unwrap();
        assert_eq!(themes::mix(m[0].as_str().unwrap(), m[1].as_str().unwrap(), m[2].as_f64().unwrap()), m[3].as_str().unwrap(), "{m:?}");
    }
    for l in data.get("luminances").unwrap().as_array().unwrap() {
        let l = l.as_array().unwrap();
        assert_eq!(themes::luminance(l[0].as_str().unwrap()), l[1].as_f64().unwrap(), "{l:?}");
    }
    for (code, key) in data.get("verdict_keys").unwrap().as_object().unwrap() {
        assert_eq!(themes::verdict_color_key(code), key.as_str());
    }
    // setting and clearing an extra colour keeps the order of EXTRA_COLOR_KEYS
    let mut t = extra_custom.clone();
    t.set_extra("calendar", Some(" #AABBCC "));
    t.set_extra("row_hover", None);
    t.set_extra("zebra", Some("nope"));
    t.set_extra("accent", Some("#000000"));
    let set: Vec<&str> = t.extra.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(set, ["quality_hires", "verdict_cd", "calendar"]);
    assert_eq!(t.extra_color("calendar"), Some("#aabbcc"));
    assert_eq!(t.any_color("row_hover"), themes::derived("row_hover", &t));
}

#[test]
fn player_state_matches_python() {
    let data = load("python_playerstate.json");
    let st = PlayerState::from_json(data.get("messy").unwrap());
    same(&st.to_json(), data.get("messy_out").unwrap(), "messy");
    let mut ops = PlayerState::default();
    let pid = ops.create_playlist("Road Trip", &["/m/a".into(), "/m/b".into(), "/m/a".into()], 1000.0);
    let pid2 = ops.create_playlist("road trip", &[], 1000.0);
    ops.add_to_playlist(&pid, &["/m/b".into(), "/m/c".into()]);
    ops.move_in_playlist(&pid, "/m/c", -2);
    ops.rename_playlist(&pid2, "  Night   Drive ").unwrap();
    ops.set_liked("/m/a", true);
    ops.set_disliked("/m/a", true);
    ops.set_disliked("/m/z", true);
    let mut jazz = EqState::default();
    jazz.apply_preset("Jazz");
    ops.save_eq_preset("  My   Jazz ", &jazz, false).unwrap();
    ops.save_eq_preset("Other", &EqState::default(), false).unwrap();
    ops.rename_eq_preset("Other", "Bright").unwrap();
    ops.step_lyrics_size(1);
    ops.step_lyrics_size(1);
    ops.toggle_column("songs", "year");
    ops.toggle_column("songs", "like");
    ops.toggle_column("songs", "title");
    ops.toggle_column("album", "file");
    ops.set_columns("folders", &["bit_rate".to_string(), "format".to_string()]);
    ops.toggle_column("folders", "format");
    // 2.3: colour labels
    for r in data.get("label_results").unwrap().as_array().unwrap() {
        let r = r.as_array().unwrap();
        let key = if r[0].as_str() == Some("playlist") { pid.clone() } else { r[1].as_str().unwrap().to_string() };
        let got = ops.set_label(r[0].as_str().unwrap(), &key, r[2].as_str());
        assert_eq!(got.as_deref(), r[3].as_str(), "{r:?}");
    }
    for r in data.get("label_lookups").unwrap().as_array().unwrap() {
        let r = r.as_array().unwrap();
        let key = if r[0].as_str() == Some("playlist") { pid.clone() } else { r[1].as_str().unwrap().to_string() };
        assert_eq!(ops.label_of(r[0].as_str().unwrap(), &key), r[2].as_str(), "{r:?}");
    }
    let kinds: Vec<&str> = data.get("label_kinds").unwrap().as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
    assert_eq!(kinds, playerstate::LABEL_KINDS.to_vec());
    for (i, c) in data.get("label_colors").unwrap().as_array().unwrap().iter().enumerate() {
        let c = c.as_array().unwrap();
        assert_eq!((c[0].as_str().unwrap(), c[1].as_str().unwrap()), playerstate::LABEL_COLORS[i]);
    }
    same(&ops.to_json(), data.get("ops_out").unwrap(), "ops");
    same(&PlayerState::from_json(&ops.to_json()).to_json(), data.get("ops_out").unwrap(), "labels round trip");
    // Now Playing's lyrics size (A− / A+)
    let limits: Vec<i64> =
        data.get("lyrics_size_limits").unwrap().as_array().unwrap().iter().map(|v| v.as_i64().unwrap()).collect();
    assert_eq!(
        limits,
        vec![
            playerstate::LYRICS_SIZE_MIN,
            playerstate::LYRICS_SIZE_DEFAULT,
            playerstate::LYRICS_SIZE_MAX,
            playerstate::LYRICS_SIZE_STEP
        ]
    );
    for case in data.get("lyrics_sizes").unwrap().as_array().unwrap() {
        let case = case.as_array().unwrap();
        let obj = Value::Object(vec![("lyrics_size".to_string(), case[0].clone())]);
        assert_eq!(PlayerState::from_json(&obj).lyrics_size, case[1].as_i64().unwrap(), "{case:?}");
    }
    for case in data.get("lyrics_size_steps").unwrap().as_array().unwrap() {
        let case = case.as_array().unwrap();
        let obj = Value::Object(vec![("lyrics_size".to_string(), case[0].clone())]);
        let mut st = PlayerState::from_json(&obj);
        assert_eq!(st.step_lyrics_size(case[1].as_i64().unwrap()), case[2].as_i64().unwrap(), "{case:?}");
        assert_eq!(playerstate::plain_lyrics_size(st.lyrics_size), case[3].as_i64().unwrap(), "{case:?}");
    }
    for e in data.get("name_errors").unwrap().as_array().unwrap() {
        let e = e.as_array().unwrap();
        let got = ops.check_preset_name(e[0].as_str().unwrap(), "").err().map(|x| x.0);
        assert_eq!(got.as_deref(), e[1].as_str(), "{e:?}");
    }
    let _ = strings(&Value::Null);
    let _ = playerstate::clean_name("x");
}


/// Several artists in one field (2.1): the split and the groupings it gives equal Python's.
#[test]
fn several_artists_split_as_in_python() {
    let path = format!("{}/tests/python_artists.json", env!("CARGO_MANIFEST_DIR"));
    let data = realhires::json::parse(&std::fs::read_to_string(path).unwrap()).unwrap();
    let tracks: Vec<musiclib::Track> =
        data.get("tracks").unwrap().as_array().unwrap().iter().filter_map(musiclib::Track::from_json).collect();
    let known = musiclib::known_artists(&tracks);
    let mut want_known: Vec<String> =
        data.get("known").unwrap().as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
    let mut got_known: Vec<String> = known.iter().cloned().collect();
    want_known.sort();
    got_known.sort();
    assert_eq!(got_known, want_known);
    for case in data.get("split").unwrap().as_array().unwrap() {
        let c = case.as_array().unwrap();
        let want: Vec<String> = c[1].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
        assert_eq!(musiclib::split_artists(c[0].as_str().unwrap(), &known), want, "{:?}", c[0]);
    }
    for (name, groups) in [("artists", musiclib::artists(&tracks)), ("album_artists", musiclib::album_artists(&tracks))] {
        let got: Vec<(String, Vec<String>)> =
            groups.iter().map(|g| (g.name.clone(), g.tracks.iter().map(|t| t.path.clone()).collect())).collect();
        let want: Vec<(String, Vec<String>)> = data
            .get(name)
            .unwrap()
            .as_array()
            .unwrap()
            .iter()
            .map(|g| {
                let g = g.as_array().unwrap();
                (g[0].as_str().unwrap().to_string(), g[1].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect())
            })
            .collect();
        assert_eq!(got, want, "{name}");
    }
}
