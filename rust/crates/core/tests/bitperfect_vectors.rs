//! Bit-perfect playback's decisions and texts against Python's answers
//! (`rust/tools/gen_player_vectors.py` writes `python_bitperfect.json`): the
//! output rate, format and channels for songs on devices that offer different
//! things, the reasons, the "Bit-perfect" and Output lines, the exact float to
//! integer conversion, and the ALSA hardware device for an output's name.

use realhires::bitperfect::{self, Access, Format, Mode, Song};
use realhires::json::{self, Value};

fn load() -> Value {
    let path = format!("{}/tests/python_bitperfect.json", env!("CARGO_MANIFEST_DIR"));
    json::parse(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn list(v: &Value) -> &Vec<Value> {
    match v {
        Value::Array(a) => a,
        other => panic!("not a list: {other:?}"),
    }
}

fn int(v: &Value, key: &str) -> i64 {
    v.get(key).unwrap().as_i64().unwrap()
}

fn text(v: &Value, key: &str) -> String {
    v.get(key).unwrap().as_str().unwrap().to_string()
}

fn texts(v: &Value) -> Vec<String> {
    list(v).iter().map(|x| x.as_str().unwrap().to_string()).collect()
}

fn song(v: &Value) -> Song {
    Song {
        rate: int(v, "rate") as u32,
        channels: int(v, "channels") as u32,
        bits: int(v, "bits") as u32,
        is_float: v.get("is_float").unwrap().as_bool().unwrap(),
        is_lossy: v.get("is_lossy").unwrap().as_bool().unwrap(),
    }
}

fn modes(v: &Value) -> Vec<Mode> {
    list(v)
        .iter()
        .map(|m| Mode {
            format: Format::from_name(&text(m, "format")),
            min_rate: int(m, "min_rate") as u32,
            max_rate: int(m, "max_rate") as u32,
            channels: int(m, "channels") as u32,
        })
        .collect()
}

fn names(formats: &[Format]) -> Vec<String> {
    formats.iter().map(|f| f.name().to_string()).collect()
}

#[test]
fn plans_reasons_and_texts_equal_python() {
    let d = load();
    let songs: Vec<Song> = list(d.get("songs").unwrap()).iter().map(song).collect();
    let sets: Vec<Vec<Mode>> = list(d.get("mode_sets").unwrap()).iter().map(modes).collect();
    let cases = list(d.get("cases").unwrap());
    assert!(cases.len() > 150);
    for c in cases {
        let s = &songs[int(c, "song") as usize];
        let m = &sets[int(c, "modes") as usize];
        let what = format!("song {s:?} modes {} failure {:?}", int(c, "modes"), text(c, "failure"));
        let p = bitperfect::plan(s, m, &text(c, "failure"));
        assert_eq!(p.rate as i64, int(c, "rate"), "{what}");
        assert_eq!(p.format.name(), text(c, "format"), "{what}");
        assert_eq!(p.channels as i64, int(c, "channels"), "{what}");
        assert_eq!(p.reasons, texts(c.get("reasons").unwrap()), "{what}");
        assert_eq!(p.resampled(), c.get("resampled").unwrap().as_bool().unwrap(), "{what}");
        let access = Access::from_name(&text(c, "access")).unwrap();
        assert_eq!(bitperfect::status(Some(s), Some(&p), access, &text(c, "device"), true), text(c, "status"), "{what}");
        assert_eq!(
            bitperfect::status_eq(Some(s), Some(&p), access, &text(c, "device"), true, true),
            text(c, "status_eq"),
            "{what} with the equalizer on"
        );
        assert_eq!(
            bitperfect::output_label(p.rate, p.format, bitperfect::song_bits(s), p.channels),
            text(c, "output"),
            "{what}"
        );
    }
    for (s, want) in songs.iter().zip(list(d.get("preferred").unwrap())) {
        assert_eq!(names(&bitperfect::preferred_formats(s)), texts(want), "{s:?}");
    }
    for (m, want) in sets.iter().zip(list(d.get("rates").unwrap())) {
        let want: Vec<u32> = list(want).iter().map(|r| r.as_i64().unwrap() as u32).collect();
        assert_eq!(bitperfect::rates_offered(m), want, "{m:?}");
    }
    for p in list(d.get("pick").unwrap()) {
        let rates: Vec<u32> = list(p.get("rates").unwrap()).iter().map(|r| r.as_i64().unwrap() as u32).collect();
        assert_eq!(bitperfect::pick_rate(int(p, "song") as u32, &rates) as i64, int(p, "rate"), "{p:?}");
    }
    let idle = texts(d.get("idle").unwrap());
    assert_eq!(bitperfect::status(None, None, Access::Direct, "", false), idle[0]);
    assert_eq!(bitperfect::status(None, None, Access::Direct, "", true), idle[1]);
    let t = d.get("texts").unwrap();
    assert_eq!(bitperfect::BUSY, text(t, "busy"));
    assert_eq!(bitperfect::LOSSY_NOTE, text(t, "lossy"));
    assert_eq!(bitperfect::EQ_ON, text(t, "eq_on"));
    assert_eq!(bitperfect::EQ_NOTE, text(t, "eq_note"));
    for c in list(d.get("still").unwrap()) {
        let access = Access::from_name(&text(c, "access")).unwrap();
        assert_eq!(bitperfect::still_runs(int(c, "rate") as u32, access, &text(c, "device")), text(c, "text"), "{c:?}");
    }
    assert_eq!(bitperfect::WASAPI_FORMAT_NOTE, text(t, "wasapi"));
    assert_eq!(bitperfect::OFF, text(t, "off"));
    assert_eq!(bitperfect::ON_IDLE, text(t, "idle"));
    let rates: Vec<u32> = list(d.get("standard_rates").unwrap()).iter().map(|r| r.as_i64().unwrap() as u32).collect();
    assert_eq!(bitperfect::STANDARD_RATES.to_vec(), rates);
}

#[test]
fn labels_rates_and_failures_equal_python() {
    let d = load();
    for l in list(d.get("labels").unwrap()) {
        let f = Format::from_name(&text(l, "format")).unwrap();
        assert_eq!(bitperfect::format_label(f, int(l, "bits") as u32), text(l, "label"));
    }
    for k in list(d.get("khz").unwrap()) {
        assert_eq!(bitperfect::fmt_khz(int(k, "hz") as u32), text(k, "text"));
    }
    for f in list(d.get("failures").unwrap()) {
        assert_eq!(bitperfect::failure_reason(&text(f, "detail")), text(f, "text"));
    }
}

#[test]
fn float_samples_become_the_same_integers() {
    let d = load();
    for c in list(d.get("ints").unwrap()) {
        let x = c.get("x").unwrap().as_f64().unwrap();
        for f in [Format::I16, Format::I24, Format::I32] {
            assert_eq!(bitperfect::int_sample(x, f) as i64, int(c, f.name()), "{x} {f:?}");
        }
    }
    // every 16-bit and many 24-bit integers survive the trip through f32
    for v in i16::MIN..=i16::MAX {
        let x = v as f32 / 32768.0;
        assert_eq!(bitperfect::int_sample_f32(x, Format::I16), v as i32);
        assert_eq!(bitperfect::int_sample_f32(x, Format::I32), (v as i32) << 16);
    }
    for v in (-(1 << 23)..(1 << 23)).step_by(997) {
        let x = v as f32 / 8_388_608.0;
        assert_eq!(bitperfect::int_sample_f32(x, Format::I24), v);
        assert_eq!(bitperfect::int_sample_f32(x, Format::I32), v << 8);
    }
}

#[test]
fn dithered_samples_become_the_same_integers() {
    let d = load();
    let cases = list(d.get("dither").unwrap());
    assert!(cases.len() > 100);
    for c in cases {
        let x = c.get("x").unwrap().as_f64().unwrap();
        let dither = c.get("d").unwrap().as_f64().unwrap();
        for f in [Format::I16, Format::I24, Format::I32] {
            assert_eq!(bitperfect::dithered_int_sample(x, f, dither) as i64, int(c, f.name()), "{x} {dither} {f:?}");
        }
    }
}

#[test]
fn alsa_cards_and_devices_equal_python() {
    let d = load();
    let cards = bitperfect::alsa_cards(&text(&d, "cards_text"));
    let want: Vec<(u32, String, String)> = list(d.get("cards").unwrap())
        .iter()
        .map(|c| {
            let c = list(c);
            (c[0].as_i64().unwrap() as u32, c[1].as_str().unwrap().to_string(), c[2].as_str().unwrap().to_string())
        })
        .collect();
    assert_eq!(cards, want);
    for c in list(d.get("devices").unwrap()) {
        assert_eq!(bitperfect::alsa_device(&text(c, "device"), &cards), text(c, "hw"), "{:?}", text(c, "device"));
    }
}
