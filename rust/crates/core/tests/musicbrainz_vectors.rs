//! The MusicBrainz port against Python's answers (`python_musicbrainz.json`,
//! written by `rust/tools/gen_mb_vectors.py` from the invented catalogue of
//! `tests/fake_musicbrainz.py`): the same requests, the same parsing and the
//! same choices.

use std::sync::{Arc, Mutex};

use realhires::json::{self, Value};
use realhires::musicbrainz::*;

fn data() -> Value {
    json::parse(include_str!("python_musicbrainz.json")).unwrap()
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

fn i(v: &Value, k: &str) -> i64 {
    match v.get(k) {
        Some(Value::Int(n)) => *n,
        Some(Value::Float(f)) => *f as i64,
        _ => 0,
    }
}

fn f(v: &Value, k: &str) -> f64 {
    match v.get(k) {
        Some(Value::Int(n)) => *n as f64,
        Some(Value::Float(x)) => *x,
        _ => 0.0,
    }
}

fn client(all: &Value, urls: Arc<Mutex<Vec<String>>>) -> MusicBrainzClient {
    let responses: Vec<(String, (u16, String))> = all
        .get("responses")
        .and_then(Value::as_object)
        .unwrap()
        .iter()
        .map(|(u, v)| {
            let a = v.as_array().unwrap();
            let status = match &a[0] {
                Value::Int(n) => *n as u16,
                _ => 0,
            };
            (u.clone(), (status, a[1].as_str().unwrap().to_string()))
        })
        .collect();
    MusicBrainzClient::new(&s(all, "server")).with_min_interval(0.0).with_transport(Box::new(
        move |url: &str, _h: &[(&str, &str)], _t| {
            urls.lock().unwrap().push(url.to_string());
            match responses.iter().find(|(u, _)| u == url) {
                Some((_, (st, body))) => Ok((*st, vec![], body.clone().into_bytes())),
                None => Ok((404, vec![], b"{\"error\": \"Not Found\"}".to_vec())),
            }
        },
    ))
}

fn song(v: &Value) -> Song {
    let tags = v
        .get("tags")
        .and_then(Value::as_object)
        .unwrap()
        .iter()
        .map(|(k, x)| (k.clone(), x.as_str().unwrap().to_string()))
        .collect();
    Song { path: s(v, "path"), tags, duration: f(v, "duration") }
}

fn songs(v: &Value) -> Vec<Song> {
    v.as_array().unwrap().iter().map(song).collect()
}

fn check_release(r: &Release, want: &Value, ctx: &str) {
    assert_eq!(r.id, s(want, "id"), "{ctx}");
    assert_eq!(r.title, s(want, "title"), "{ctx}");
    assert_eq!(r.artist, s(want, "artist"), "{ctx}");
    assert_eq!(r.date, s(want, "date"), "{ctx}");
    assert_eq!(r.country, s(want, "country"), "{ctx}");
    assert_eq!(r.status, s(want, "status"), "{ctx}");
    assert_eq!(r.track_count, i(want, "track_count"), "{ctx}");
    assert_eq!(r.disc_count, i(want, "disc_count"), "{ctx}");
    assert_eq!(r.formats, s(want, "formats"), "{ctx}");
    assert_eq!(r.primary_type, s(want, "primary_type"), "{ctx}");
    let sec: Vec<String> =
        want.get("secondary_types").and_then(Value::as_array).unwrap().iter().map(|x| x.as_str().unwrap().to_string()).collect();
    assert_eq!(r.secondary_types, sec, "{ctx}");
    assert_eq!(r.first_date, s(want, "first_date"), "{ctx}");
    assert_eq!(r.score, i(want, "score"), "{ctx}");
}

fn pos(t: &Option<ReleaseTrack>) -> Value {
    match t {
        Some(t) => Value::Array(vec![Value::Int(t.disc), Value::Int(t.number)]),
        None => Value::Null,
    }
}

fn urls_of(v: &Value) -> Vec<String> {
    v.get("urls").and_then(Value::as_array).unwrap().iter().map(|u| u.as_str().unwrap().to_string()).collect()
}

fn changes_json(ch: &[(String, String)]) -> Value {
    Value::Object(ch.iter().map(|(k, v)| (k.clone(), Value::Str(v.clone()))).collect())
}

#[test]
fn musicbrainz_matches_python() {
    let all = data();
    let mut n = 0;
    for case in all.get("cases").and_then(Value::as_array).unwrap() {
        let kind = s(case, "kind");
        let urls = Arc::new(Mutex::new(vec![]));
        let mut c = client(&all, urls.clone());
        let expect = case.get("expect").unwrap();
        match kind.as_str() {
            "release_query" => assert_eq!(release_query(&s(case, "album"), &s(case, "artist")), expect.as_str().unwrap()),
            "recording_query" => assert_eq!(
                recording_query(&s(case, "title"), &s(case, "artist"), &s(case, "album")),
                expect.as_str().unwrap()
            ),
            "detail" => {
                let d = c.release(&s(case, "id")).unwrap();
                match (d, expect) {
                    (None, Value::Null) => {}
                    (Some(d), want) => {
                        check_release(&d.release, want.get("release").unwrap(), &s(case, "id"));
                        assert_eq!(d.genre, s(want, "genre"));
                        assert_eq!(d.date(), s(want, "date"));
                        let wt = want.get("tracks").and_then(Value::as_array).unwrap();
                        assert_eq!(d.tracks.len(), wt.len());
                        for (t, w) in d.tracks.iter().zip(wt) {
                            assert_eq!((t.disc, t.disc_count, t.number, t.track_count), (
                                i(w, "disc"), i(w, "disc_count"), i(w, "number"), i(w, "track_count")));
                            assert_eq!((t.title.as_str(), t.artist.as_str(), t.recording_id.as_str(), t.composer.as_str()), (
                                s(w, "title").as_str(), s(w, "artist").as_str(), s(w, "recording_id").as_str(),
                                s(w, "composer").as_str()));
                            assert!((t.length - f(w, "length")).abs() < 1e-9);
                        }
                    }
                    (None, _) => panic!("no release {}", s(case, "id")),
                }
                assert_eq!(*urls.lock().unwrap(), urls_of(case));
            }
            "search_releases" => {
                let rs = c.search_releases(&s(case, "album"), &s(case, "artist")).unwrap();
                let want = expect.as_array().unwrap();
                assert_eq!(rs.len(), want.len());
                for (r, w) in rs.iter().zip(want) {
                    check_release(r, w, "search");
                }
                assert_eq!(*urls.lock().unwrap(), urls_of(case));
            }
            "search_recordings" => {
                let recs = c.search_recordings(&s(case, "title"), &s(case, "artist"), &s(case, "album")).unwrap();
                let want = expect.as_array().unwrap();
                assert_eq!(recs.len(), want.len());
                for (r, w) in recs.iter().zip(want) {
                    assert_eq!((r.id.as_str(), r.title.as_str(), r.artist.as_str(), r.score), (
                        s(w, "id").as_str(), s(w, "title").as_str(), s(w, "artist").as_str(), i(w, "score")));
                    assert!((r.length - f(w, "length")).abs() < 1e-9);
                    let wr = w.get("releases").and_then(Value::as_array).unwrap();
                    assert_eq!(r.releases.len(), wr.len());
                    for (rr, x) in r.releases.iter().zip(wr) {
                        check_release(&rr.release, x.get("release").unwrap(), "recording release");
                        assert_eq!((rr.disc, rr.number), (i(x, "disc"), i(x, "number")));
                    }
                }
                assert_eq!(*urls.lock().unwrap(), urls_of(case));
            }
            "match" => {
                let d = c.release(&s(case, "id")).unwrap().unwrap();
                let by_order = matches!(case.get("by_order"), Some(Value::Bool(true)));
                let m = match_tracks(&songs(case.get("songs").unwrap()), &d, by_order);
                assert_eq!(Value::Array(m.iter().map(pos).collect()), *expect, "{}", s(case, "name"));
            }
            "changes" => {
                let d = c.release(&s(case, "id")).unwrap().unwrap();
                let at = case.get("at").and_then(Value::as_array).unwrap();
                let (disc, num) = (match &at[0] { Value::Int(x) => *x, _ => 0 }, match &at[1] { Value::Int(x) => *x, _ => 0 });
                let fields: Vec<String> =
                    case.get("fields").and_then(Value::as_array).unwrap().iter().map(|x| x.as_str().unwrap().to_string()).collect();
                let refs: Vec<&str> = fields.iter().map(String::as_str).collect();
                let only = matches!(case.get("only_empty"), Some(Value::Bool(true)));
                let ch = changes_for(&song(case.get("song").unwrap()), &d, d.track_at(disc, num), &refs, only);
                assert_eq!(changes_json(&ch), *expect);
            }
            "find_album" => {
                let (d, m) = find_album(&mut c, &songs(case.get("songs").unwrap())).unwrap();
                assert_eq!(d.map(|d| Value::Str(d.release.id)).unwrap_or(Value::Null), *expect.get("id").unwrap(), "{}", s(case, "name"));
                assert_eq!(Value::Array(m.iter().map(pos).collect()), *expect.get("tracks").unwrap());
                assert_eq!(*urls.lock().unwrap(), urls_of(case), "{}", s(case, "name"));
            }
            "find_song" => {
                let (d, t) = find_song(&mut c, &song(case.get("song").unwrap())).unwrap();
                assert_eq!(d.map(|d| Value::Str(d.release.id)).unwrap_or(Value::Null), *expect.get("id").unwrap(), "{}", s(case, "name"));
                assert_eq!(pos(&t), *expect.get("track").unwrap());
                assert_eq!(*urls.lock().unwrap(), urls_of(case), "{}", s(case, "name"));
            }
            "candidates" => {
                let album = matches!(case.get("album_mode"), Some(Value::Bool(true)));
                let found = search_candidates(&mut c, &songs(case.get("songs").unwrap()), &s(case, "title"), &s(case, "artist"), album).unwrap();
                let ids: Vec<Value> = found.into_iter().map(|r| Value::Str(r.id)).collect();
                assert_eq!(Value::Array(ids), *expect);
                assert_eq!(*urls.lock().unwrap(), urls_of(case));
            }
            "needs_lookup" => {
                let tags: Vec<(String, String)> = case
                    .get("tags")
                    .and_then(Value::as_object)
                    .unwrap()
                    .iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string()))
                    .collect();
                assert_eq!(Value::Bool(needs_lookup(&tags)), *expect);
            }
            "album_of" => {
                let (a, b) = album_of(&songs(case.get("songs").unwrap()));
                assert_eq!(Value::Array(vec![Value::Str(a), Value::Str(b)]), *expect);
            }
            "genre_name" => assert_eq!(genre_name(&s(case, "name")), expect.as_str().unwrap()),
            "groups" => {
                let g = groups(&songs(case.get("songs").unwrap()));
                let got = Value::Array(
                    g.iter().map(|g| Value::Array(g.iter().map(|s| Value::Str(s.path.clone())).collect())).collect(),
                );
                assert_eq!(got, *expect);
            }
            "user_agent" => {
                let want = expect.as_str().unwrap();
                // the version follows the Cargo version, which equals Python's __version__
                assert_eq!(user_agent(), want);
            }
            other => panic!("unknown case {other}"),
        }
        n += 1;
    }
    assert!(n > 40);
}

/// The automatic lookup on real FLAC files: the missing tags are filled in,
/// existing ones kept, and a second run asks nothing.
#[test]
fn automatic_lookup_writes_real_files_once() {
    let dir = std::env::temp_dir().join(format!("rhr-mb-{}", std::process::id()));
    let album = dir.join("n");
    std::fs::create_dir_all(&album).unwrap();
    std::env::set_var("REALHIRES_LIBRARY", dir.join("library.sqlite"));
    std::env::set_var("REALHIRES_RESULTS", dir.join("results.sqlite"));
    let ffmpeg = realhires::probe::tool("ffmpeg");
    let make = |name: &str, secs: u32, tags: &[(&str, &str)]| -> String {
        let path = album.join(name).to_string_lossy().into_owned();
        let mut cmd = std::process::Command::new(&ffmpeg);
        cmd.args(["-v", "error", "-y", "-f", "lavfi", "-i", &format!("sine=frequency=440:duration={secs}"), "-ac", "2", "-ar", "44100"]);
        for (k, v) in tags {
            cmd.args(["-metadata", &format!("{k}={v}")]);
        }
        assert!(cmd.arg(&path).status().unwrap().success());
        path
    };
    let base = [("title", ""), ("artist", "The Example Band"), ("album", "Night Drive")];
    let mut paths = vec![];
    for (i, (title, secs, extra)) in [("Neon Streets", 201, ""), ("Midnight Signal", 245, "My Genre"), ("harbour lights", 187, ""), ("Last Train Home", 263, "")]
        .iter()
        .enumerate()
    {
        let mut tags: Vec<(&str, &str)> = base.iter().map(|(k, v)| if *k == "title" { (*k, *title) } else { (*k, *v) }).collect();
        let num = (i + 1).to_string();
        tags.push(("track", &num));
        if !extra.is_empty() {
            tags.push(("genre", extra));
        }
        paths.push(make(&format!("0{}.flac", i + 1), *secs, &tags));
    }
    let all = data();
    let urls = Arc::new(Mutex::new(vec![]));
    let mut c = client(&all, urls.clone());
    let log = LookupLog::open(None).unwrap();
    let out = run_auto(&mut c, &paths, None, &log, &|_| 0.0, |_, _| {}).unwrap();
    assert_eq!(out.iter().filter(|o| o.status == FILLED).count(), 4, "{out:?}");
    let tags = realhires::probe::probe(&paths[0]).unwrap().tags;
    let tag = |t: &realhires::probe::Tags, k: &str| realhires::probe::tag(t, k).unwrap_or("").to_string();
    assert_eq!(tag(&tags, "album_artist"), "The Example Band");
    assert_eq!(tag(&tags, "date"), "1999-05-28");
    assert_eq!(tag(&tags, "genre"), "Electronic");
    assert_eq!(tag(&tags, "composer"), "Kai Example");
    assert_eq!(tag(&realhires::probe::probe(&paths[1]).unwrap().tags, "genre"), "My Genre");
    assert_eq!(log.status(&paths[0]), FILLED);
    assert_eq!(auto_summary(&out), "MusicBrainz filled in the information of 4 songs.");
    let n = urls.lock().unwrap().len();
    assert!(run_auto(&mut c, &paths, None, &log, &|_| 0.0, |_, _| {}).unwrap().is_empty());
    assert_eq!(urls.lock().unwrap().len(), n);
    let _ = std::fs::remove_dir_all(&dir);
}
