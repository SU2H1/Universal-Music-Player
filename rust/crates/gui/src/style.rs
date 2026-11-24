//! Colours, labels and small helpers shared by the windows of the desktop app.

use egui::Color32;
use realhires::n_;
use realhires::verdict::Verdict;

pub fn hex(c: u32) -> Color32 {
    Color32::from_rgb((c >> 16) as u8, (c >> 8) as u8, c as u8)
}

/// (label, colour, description) of each verdict, as in the Python app.  The
/// label and description are English (the label is also matched by code);
/// show them with `realhires::i18n::tr()` or `verdict_label()`.
pub fn verdict_style(v: Verdict) -> (&'static str, Color32, &'static str) {
    match v {
        Verdict::FakeLossless => {
            (n_!("Fake lossless"), hex(0xa3122a), n_!("Lossless file (FLAC/WAV/ALAC...) made from an MP3/AAC"))
        }
        Verdict::Fake => (n_!("Fake hi-res"), hex(0xd32f2f), n_!("Upsampled and/or 16-bit padded")),
        Verdict::Suspect => (n_!("Suspicious"), hex(0xb26a00), n_!("Could not be confirmed either way")),
        Verdict::Real => (n_!("Real hi-res"), hex(0x2e7d32), n_!("Genuine high resolution")),
        Verdict::NotHires => (n_!("CD quality"), hex(0x1565c0), n_!("Genuine lossless, not hi-res")),
        Verdict::Lossy => (n_!("Lossy format"), hex(0x6a1b9a), n_!("MP3/AAC/... file")),
        Verdict::Error => (n_!("Error"), hex(0x616161), n_!("Could not be analysed")),
    }
}

/// The verdict's label in the language in use ("Fake hi-res", "偽ハイレゾ").
pub fn verdict_label(v: Verdict) -> &'static str {
    realhires::i18n::tr(verdict_style(v).0)
}

pub const PENDING_LABEL: &str = n_!("Not scanned");
pub const PENDING_COLOR: u32 = 0x808080;
pub const RUNNING_LABEL: &str = n_!("Analysing…");
pub const RUNNING_COLOR: u32 = 0x0b62c4;
pub const RING_FAKE: u32 = 0xd32f2f;
pub const RING_SUS: u32 = 0xef8f00;
pub const RING_OK: u32 = 0x43a047;
pub const FOUND: u32 = 0xfff59d;
pub const FOUND_CUR: u32 = 0xffb74d;

/// A count in the language in use: `count(n, n_!("1 file"), n_!("{n} files"))`
/// ("1 file", "3 files"; Japanese gives both forms the same text).
pub fn count(n: usize, one: &'static str, many: &'static str) -> String {
    if n == 1 {
        realhires::i18n::tr(one).to_string()
    } else {
        realhires::i18n::fill(realhires::i18n::tr(many), &[("n", n.to_string())])
    }
}

/// "3:07" or "1:02:03".
pub fn fmt_duration(sec: Option<f64>) -> String {
    let Some(s) = sec else { return "?".into() };
    let total = s.round_ties_even().max(0.0) as u64;
    let (h, m, s) = (total / 3600, (total / 60) % 60, total % 60);
    if h > 0 {
        format!("{h}:{m:02}:{s:02}")
    } else {
        format!("{m}:{s:02}")
    }
}

/// Open the file's folder in Explorer, Finder or the Linux file manager.
pub fn reveal_in_file_manager(path: &str) {
    let mut cmd;
    if cfg!(windows) {
        cmd = realhires::probe::command("explorer");
        cmd.arg(format!("/select,{}", path.replace('/', "\\")));
    } else if cfg!(target_os = "macos") {
        cmd = realhires::probe::command("open");
        cmd.args(["-R", path]);
    } else {
        cmd = realhires::probe::command("xdg-open");
        let dir = realhires::paths::parent(path);
        cmd.arg(if dir.is_empty() { ".".to_string() } else { dir });
    }
    let _ = cmd.spawn();
}

/// Open the file with its default application.
pub fn open_with_default_app(path: &str) {
    let mut cmd = realhires::probe::command(if cfg!(windows) {
        "explorer"
    } else if cfg!(target_os = "macos") {
        "open"
    } else {
        "xdg-open"
    });
    cmd.arg(path);
    let _ = cmd.spawn();
}

/// A stable muted colour for an album placeholder.
pub fn placeholder_color(title: &str) -> Color32 {
    let hue = title.chars().map(|c| c as u32).sum::<u32>() % 360;
    let (h, l, s) = (hue as f32 / 360.0, 0.42f32, 0.35f32);
    let q = if l < 0.5 { l * (1.0 + s) } else { l + s - l * s };
    let p = 2.0 * l - q;
    let f = |t: f32| {
        let t = t.rem_euclid(1.0);
        let v = if t < 1.0 / 6.0 {
            p + (q - p) * 6.0 * t
        } else if t < 0.5 {
            q
        } else if t < 2.0 / 3.0 {
            p + (q - p) * (2.0 / 3.0 - t) * 6.0
        } else {
            p
        };
        (v * 255.0) as u8
    };
    Color32::from_rgb(f(h + 1.0 / 3.0), f(h), f(h - 1.0 / 3.0))
}
