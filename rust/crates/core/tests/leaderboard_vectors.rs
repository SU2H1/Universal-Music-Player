//! 2.4: Your Stats' leaderboard ("Most Played") against Python's answers
//! (`rust/tools/gen_player_vectors.py` writes `python_leaderboard.json`): the ranking of
//! songs, artists and albums by plays and minutes, shared ranks, the cut at `top` rows
//! and the songs Play and Shuffle take.

use realhires::json::{self, Value};
use realhires::stats::{self, Play};

fn load() -> Value {
    let path = format!("{}/tests/python_leaderboard.json", env!("CARGO_MANIFEST_DIR"));
    json::parse(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

/// Equal JSON, numbers within rounding.
fn same(a: &Value, b: &Value, at: &str) {
    match (a, b) {
        (Value::Object(x), Value::Object(_)) => {
            let mut kx: Vec<&String> = x.iter().map(|(k, _)| k).collect();
            let mut ky: Vec<&String> = b.as_object().unwrap().iter().map(|(k, _)| k).collect();
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
        _ if a.as_f64().is_some() && b.as_f64().is_some() => {
            let (p, q) = (a.as_f64().unwrap(), b.as_f64().unwrap());
            assert!((p - q).abs() <= 1e-9 * p.abs().max(q.abs()).max(1.0), "{p} != {q} at {at}");
        }
        _ => assert_eq!(a, b, "at {at}"),
    }
}

#[test]
fn leaderboards_equal_python() {
    let data = load();
    let plays: Vec<Play> = data
        .get("plays")
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
            // 2.4: plays carry the place they were played at (none in these vectors)
            place: p.get("place").filter(|v| !v.is_null()).map(|v| realhires::places::Place::from_json(v).unwrap()),
        })
        .collect();
    let kinds: Vec<String> = data.get("kinds").unwrap().as_array().unwrap().iter().map(|k| k.as_str().unwrap().to_string()).collect();
    assert_eq!(kinds, stats::LEADERBOARD_KINDS);
    let boards = data.get("boards").unwrap();
    let mut shared = 0;
    for kind in stats::LEADERBOARD_KINDS {
        for top in [stats::TOP, 5, 3, 0] {
            let rows = stats::leaderboard(&plays, kind, top);
            let got = Value::Array(rows.iter().map(|r| r.to_json()).collect());
            same(&got, boards.get(kind).unwrap().get(&top.to_string()).unwrap(), &format!("{kind} top {top}"));
            shared += rows.windows(2).filter(|w| w[0].rank == w[1].rank).count();
        }
        let rows = stats::leaderboard(&plays, kind, stats::TOP);
        let ranked = Value::Array(stats::ranked_songs(&rows).into_iter().map(Value::Str).collect());
        same(&ranked, data.get("ranked").unwrap().get(kind).unwrap(), &format!("ranked {kind}"));
    }
    assert!(shared > 0, "the vectors hold ties");
    assert!(stats::leaderboard(&[], "songs", stats::TOP).is_empty());
    assert_eq!(data.get("empty").unwrap().as_array().unwrap().len(), 0);
    assert!(stats::leaderboard(&plays, "genres", stats::TOP).is_empty(), "not a leaderboard");
}
