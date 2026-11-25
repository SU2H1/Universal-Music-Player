//! Where a song was played (2.4) against Python's answers
//! (`rust/tools/gen_player_vectors.py` → `python_places.json`): parsing the
//! services' answers, the place texts and keys, the places of a period and their
//! songs, the day list's places, when a lookup is due, the client's waits and
//! messages with a fake transport and clock, and the play history's place
//! columns on a real database (an old one migrated).

use std::sync::{Arc, Mutex};

use realhires::json::{self, Value};
use realhires::lyrics::TransportError;
use realhires::musiclib::{self, Track};
use realhires::places::{self, Place, PlaceCache};
use realhires::stats::{self, LocalParts, Play};

fn load(name: &str) -> Value {
    let path = format!("{}/tests/{name}", env!("CARGO_MANIFEST_DIR"));
    json::parse(&std::fs::read_to_string(path).unwrap()).unwrap()
}

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

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

fn f(v: &Value, k: &str) -> f64 {
    v.get(k).and_then(Value::as_f64).unwrap()
}

fn tokyo(ts: f64) -> LocalParts {
    use chrono::{Datelike, Timelike};
    let t = chrono::DateTime::from_timestamp((ts + 9.0 * 3600.0).floor() as i64, 0).unwrap().naive_utc();
    LocalParts { year: t.year(), month: t.month(), day: t.day(), hour: t.hour(), minute: t.minute(), wday: t.weekday().num_days_from_monday() }
}

fn opt(v: Option<f64>) -> Value {
    v.map(Value::Float).unwrap_or(Value::Null)
}

#[test]
fn constants_and_settings_match_python() {
    let data = load("python_places.json");
    assert_eq!(s(&data, "default_server"), places::DEFAULT_SERVER);
    let c = data.get("constants").unwrap();
    assert_eq!(f(c, "min_interval"), places::MIN_INTERVAL_S);
    assert_eq!(f(c, "max_attempts") as usize, places::MAX_ATTEMPTS);
    assert_eq!(f(c, "backoff"), places::BACKOFF_S);
    assert_eq!(f(c, "default_retry_after"), places::DEFAULT_RETRY_AFTER_S);
    assert_eq!(f(c, "max_retry_after"), places::MAX_RETRY_AFTER_S);
    assert_eq!(f(c, "refresh"), places::REFRESH_S);
    assert_eq!(f(c, "max_name") as usize, places::MAX_NAME);
    let cfg = realhires::settings::Settings::default();
    assert!(!cfg.play_location);
    assert_eq!(cfg.location_server, places::DEFAULT_SERVER);
    for name in ["play_location", "location_server"] {
        let info = realhires::settings::Settings::field_info().into_iter().find(|i| i.name == name).unwrap();
        assert_eq!(info.group, "Playback");
    }
    for row in data.get("servers").unwrap().as_array().unwrap() {
        let r = row.as_array().unwrap();
        let server = r[0].as_str().unwrap();
        assert_eq!(places::normalize_server(server), r[1].as_str().unwrap(), "{server:?}");
        assert_eq!(places::host_of(server), r[2].as_str().unwrap(), "{server:?}");
        assert_eq!(places::is_official(server), r[3].as_bool().unwrap(), "{server:?}");
    }
    for row in data.get("retry_after").unwrap().as_array().unwrap() {
        let r = row.as_array().unwrap();
        let headers: Vec<(String, String)> =
            r[0].as_object().unwrap().iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect();
        assert_eq!(places::retry_after(&headers), r[1].as_f64().unwrap(), "{headers:?}");
    }
}

#[test]
fn answers_texts_and_coordinates_match_python() {
    let data = load("python_places.json");
    let answers = data.get("answers").unwrap().as_array().unwrap();
    let parsed = data.get("parsed").unwrap().as_array().unwrap();
    assert_eq!(answers.len(), parsed.len());
    for (i, (a, want)) in answers.iter().zip(parsed).enumerate() {
        match places::parse_place(a) {
            Ok(p) => {
                same(&p.to_json(), want.get("place").expect("Python found a place"), &format!("answer {i}"));
                assert_eq!(places::place_text(Some(&p)), s(want, "text"), "answer {i}");
                assert_eq!(places::place_key(Some(&p)), s(want, "key"), "answer {i}");
                assert_eq!(Place::from_json(&p.to_json()), Some(p));
            }
            Err(e) => assert_eq!(e.to_string(), s(want, "error"), "answer {i}"),
        }
    }
    for row in data.get("coordinates").unwrap().as_array().unwrap() {
        let r = row.as_array().unwrap();
        let got = places::coordinate(Some(&r[0]), r[1].as_f64().unwrap());
        same(&opt(got), &r[2], &format!("coordinate {:?}", r[0]));
    }
    for row in data.get("texts").unwrap().as_array().unwrap() {
        let r = row.as_array().unwrap();
        let parts: Vec<String> = r[0].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
        let p = Place { city: parts[0].clone(), region: parts[1].clone(), country: parts[2].clone(), ..Default::default() };
        assert_eq!(places::place_text(Some(&p)), r[1].as_str().unwrap());
        assert_eq!(places::place_key(Some(&p)), r[2].as_str().unwrap());
    }
}

fn plays_of(data: &Value) -> Vec<Play> {
    data.get("plays")
        .unwrap()
        .as_array()
        .unwrap()
        .iter()
        .map(|p| Play {
            path: s(p, "path"),
            started: f(p, "started"),
            seconds: f(p, "seconds"),
            duration: f(p, "duration"),
            title: s(p, "title"),
            artist: s(p, "artist"),
            album: s(p, "album"),
            album_artist: s(p, "album_artist"),
            genre: s(p, "genre"),
            year: s(p, "year"),
            composer: s(p, "composer"),
            place: p.get("place").filter(|v| !v.is_null()).map(|v| Place::from_json(v).unwrap()),
        })
        .collect()
}

#[test]
fn places_of_a_period_their_songs_and_the_day_list_match_python() {
    let data = load("python_places.json");
    let plays = plays_of(&data);
    let rows = places::period_places(&plays);
    same(&Value::Array(rows.iter().map(|r| r.to_json()).collect()), data.get("period").unwrap(), "period");
    let songs = data.get("songs").unwrap().as_object().unwrap();
    assert_eq!(songs.len(), rows.len());
    for (key, want) in songs {
        let got = places::place_songs(&plays, key);
        same(&Value::Array(got.iter().map(|r| r.to_json()).collect()), want, &format!("songs {key:?}"));
    }
    assert!(places::place_songs(&plays, "").is_empty());
    for day in data.get("days").unwrap().as_array().unwrap() {
        let d = day.as_array().unwrap();
        let (y, m, dd) = (d[0].as_f64().unwrap() as i32, d[1].as_f64().unwrap() as u32, d[2].as_f64().unwrap() as u32);
        let got = stats::day_plays(&plays, y, m, dd, &tokyo);
        same(&Value::Array(got.iter().map(|r| r.to_json()).collect()), &d[3], &format!("day {y}-{m}-{dd}"));
    }
}

#[test]
fn when_a_lookup_is_due_matches_python() {
    let data = load("python_places.json");
    let tokyo_place = Place {
        city: "Tokyo".into(),
        region: "Tokyo".into(),
        country: "Japan".into(),
        country_code: "JP".into(),
        latitude: Some(35.69),
        longitude: Some(139.69),
    };
    let mut cache = PlaceCache::default();
    for (i, step) in data.get("cache").unwrap().as_array().unwrap().iter().enumerate() {
        let st = step.get("step").unwrap().as_array().unwrap();
        let out = match st[0].as_str().unwrap() {
            "due" => Value::Bool(cache.due(st[1].as_f64().unwrap(), st[2].as_bool().unwrap(), st[3].as_f64().unwrap())),
            "begin" => {
                cache.begin(st[1].as_f64().unwrap());
                Value::Null
            }
            "finish" => {
                let ok = st[1].as_bool().unwrap();
                cache.finish(ok.then(|| tokyo_place.clone()), if ok { "" } else { "It failed." }, st[2].as_f64().unwrap());
                Value::Null
            }
            _ => {
                cache.reset();
                Value::Null
            }
        };
        let at = format!("step {i} {st:?}");
        same(&out, step.get("out").unwrap(), &at);
        same(&cache.place.as_ref().map(Place::to_json).unwrap_or(Value::Null), step.get("place").unwrap(), &at);
        assert_eq!(cache.error, s(step, "error"), "{at}");
        same(&opt(cache.looked_up), step.get("looked_up").unwrap(), &at);
        assert_eq!(cache.busy, step.get("busy").unwrap().as_bool().unwrap(), "{at}");
        same(&opt(cache.last), step.get("last").unwrap(), &at);
    }
}

#[test]
fn the_client_waits_and_answers_as_in_python() {
    let data = load("python_places.json");
    for (n, run) in data.get("runs").unwrap().as_array().unwrap().iter().enumerate() {
        let script = run.get("script").unwrap();
        let answers: Vec<Value> = script.get("answers").unwrap().as_array().unwrap().clone();
        let todo = Arc::new(Mutex::new(answers.into_iter()));
        let seen: Arc<Mutex<Vec<(String, Vec<String>)>>> = Arc::default();
        let (todo2, seen2) = (todo.clone(), seen.clone());
        let transport: realhires::lyrics::Transport = Box::new(move |url: &str, headers: &[(&str, &str)], _t: f64| {
            let mut names: Vec<String> = headers.iter().map(|(k, _)| k.to_string()).collect();
            names.sort();
            seen2.lock().unwrap().push((url.to_string(), names));
            let a = todo2.lock().unwrap().next().expect("an answer");
            let a = a.as_array().unwrap();
            match a[0].as_str() {
                Some("network") => Err(TransportError::Network(a[1].as_str().unwrap().into())),
                Some("invalid") => Err(TransportError::Invalid(a[1].as_str().unwrap().into())),
                _ => {
                    let hdrs = a[1].as_object().unwrap().iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect();
                    Ok((a[0].as_f64().unwrap() as u16, hdrs, a[2].as_str().unwrap().as_bytes().to_vec()))
                }
            }
        });
        let t = Arc::new(Mutex::new(100.0f64));
        let slept: Arc<Mutex<Vec<f64>>> = Arc::default();
        let (t1, t2, sl) = (t.clone(), t.clone(), slept.clone());
        let server = script.get("server").and_then(Value::as_str).unwrap_or(places::DEFAULT_SERVER);
        let mut c = places::GeoClient::new(server).with_transport(transport).with_clock(
            move || *t1.lock().unwrap(),
            move |secs| {
                sl.lock().unwrap().push(secs);
                *t2.lock().unwrap() += secs;
            },
        );
        c.min_interval = places::default_interval(&c.server);
        let results = run.get("results").unwrap().as_array().unwrap();
        for (i, advance) in script.get("lookups").unwrap().as_array().unwrap().iter().enumerate() {
            *t.lock().unwrap() += advance.as_f64().unwrap();
            let want = &results[i];
            let at = format!("run {n} lookup {i}");
            match c.lookup() {
                Ok(p) => same(&p.to_json(), want.get("place").expect(&at), &at),
                Err(e) => assert_eq!(e.to_string(), s(want, "error"), "{at}"),
            }
            assert_eq!(c.requests as f64, f(want, "requests"), "{at}");
            same(&Value::Float(c.not_before), want.get("not_before").unwrap(), &at);
            same(&Value::Float(*t.lock().unwrap()), want.get("t").unwrap(), &at);
        }
        same(&Value::Array(slept.lock().unwrap().iter().map(|x| Value::Float(*x)).collect()), run.get("slept").unwrap(), &format!("run {n} slept"));
        let want_seen = run.get("seen").unwrap().as_array().unwrap();
        let got_seen = seen.lock().unwrap();
        assert_eq!(got_seen.len(), want_seen.len(), "run {n} requests");
        for ((url, names), w) in got_seen.iter().zip(want_seen) {
            let w = w.as_array().unwrap();
            assert_eq!(url, w[0].as_str().unwrap(), "run {n}");
            let wn: Vec<String> = w[1].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
            assert_eq!(names, &wn, "run {n}");
        }
    }
    assert!(places::user_agent().starts_with(&format!("UniversalMusicPlayer/{} ( ", realhires::VERSION)));
}

#[test]
fn an_old_library_gets_the_place_columns_and_plays_keep_their_places() {
    let dir = std::env::temp_dir().join(format!("umc-places-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("library.sqlite");
    {
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute_batch(
            "CREATE TABLE plays (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, started REAL, seconds REAL, \
             duration REAL, title TEXT, artist TEXT, album TEXT, album_artist TEXT, genre TEXT, year TEXT, composer TEXT);
             INSERT INTO plays (path, started, seconds, duration, title, artist, album, album_artist, genre, year, \
             composer) VALUES ('/m/a.flac', 1000, 40, 200, 'Old', 'Ann', '', '', '', '', '');",
        )
        .unwrap();
    }
    let tokyo_place = places::parse_place(&json::parse(r#"{"cityName": "Shinjuku", "regionName": "Tokyo", "countryName": "Japan",
        "countryCode": "JP", "latitude": 35.68951, "longitude": 139.69171}"#).unwrap()).unwrap();
    {
        let lib = musiclib::Library::open(Some(&path)).unwrap();
        let plays = lib.plays(0.0, None);
        assert_eq!(plays.len(), 1);
        assert_eq!((plays[0].title.as_str(), plays[0].place.as_ref()), ("Old", None));
        let t = Track { path: "/m/b.flac".into(), title: "New".into(), duration: 100.0, ..Default::default() };
        assert!(lib.add_play_at(&t, 2000.0, 50.0, Some(&tokyo_place)));
        assert!(lib.add_play(&t, 3000.0, 50.0));
    }
    let lib = musiclib::Library::open(Some(&path)).unwrap(); // opened again: the columns are not added twice
    let got: Vec<Option<Place>> = lib.plays(0.0, None).into_iter().map(|p| p.place).collect();
    assert_eq!(got, vec![None, Some(tokyo_place), None]);
    assert_eq!(lib.places_count(), 1);
    assert_eq!(lib.forget_places(), 1);
    assert_eq!(lib.places_count(), 0);
    assert_eq!(lib.plays(0.0, None).len(), 3);
    assert!(lib.plays(0.0, None).iter().all(|p| p.place.is_none()));
    drop(lib);
    let _ = std::fs::remove_dir_all(&dir);
}
