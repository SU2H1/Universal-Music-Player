//! The Download synced lyrics window and the dashboard's Lyrics tab (the
//! counterparts of `LyricsDialog` and `App._show_lyrics` in `realhires/gui.py`).
//! Songs are looked up on LRCLIB one at a time by one worker thread, and their
//! lyrics are saved as `<name>.lrc` next to them, as LRCGET does.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use egui::text::LayoutJob;
use egui::{Align2, Color32, FontId, RichText, TextFormat, Vec2};
use realhires::eta::{fmt_about, ScanEta};
use realhires::lyrics::{
    default_interval, existing_lyrics, lyrics_summary, read_lyrics_file, short_skip, short_status, sidecar,
    skip_reason, splitlines, LyricsOutcome, Status, CREDIT, EXISTING_CHOICES,
};
use realhires::i18n::{self, tr_text};
use realhires::paths::file_name;
use realhires::probe::tag;
use realhires::tr;
use realhires::settings::Settings;
use realhires::verdict::FileResult;

use crate::app::App;
use crate::dialogs::{Confirm, ConfirmAction};
use crate::style::{hex, reveal_in_file_manager};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Look {
    Ready,
    Ok,
    Plain,
    Skip,
    Miss,
    Fail,
    Busy,
}

impl Look {
    fn of(s: Status) -> Look {
        match s {
            Status::Synced | Status::Instrumental => Look::Ok,
            Status::Plain => Look::Plain,
            Status::Skipped => Look::Skip,
            Status::NoTags | Status::NotFound | Status::PlainOnly => Look::Miss,
            Status::Failed => Look::Fail,
        }
    }

    fn color(self, normal: Color32) -> Color32 {
        match self {
            Look::Ready => normal,
            Look::Ok => hex(0x2e7d32),
            Look::Plain => hex(0x1565c0),
            Look::Skip => hex(0x888888),
            Look::Miss => hex(0x9a6700),
            Look::Fail => hex(0xc62828),
            Look::Busy => hex(0x0b62c4),
        }
    }
}

struct Row {
    path: String,
    name: String,
    song: String,
    status: String,
    saved: String,
    look: Look,
}

static RUNS: AtomicU64 = AtomicU64::new(1);

pub struct LyricsWindow {
    paths: Vec<String>,
    pub todo: Vec<String>,
    rows: Vec<Row>,
    messages: HashMap<String, String>,
    selected: Option<String>,
    scroll_to: Option<String>,
    pub outcomes: Vec<LyricsOutcome>,
    pub saved_paths: Vec<String>,
    pub running: bool,
    pub run: u64,
    stop: Arc<AtomicBool>,
    eta: Option<ScanEta>,
    status_time: Instant,
    existing: String,
    plain: bool,
    instrumental: bool,
    search: bool,
    pub head: String,
    pub note: String,
    pub status: String,
}

impl Drop for LyricsWindow {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst); // a closed window stops its downloads
    }
}

/// "artist – title" from a scan result's tags, "" when either is missing.
fn song(results: &HashMap<String, FileResult>, path: &str) -> String {
    let Some(r) = results.get(path) else { return String::new() };
    let get = |k: &str| tag(&r.tags, k).filter(|v| !v.is_empty());
    match (get("artist").or_else(|| get("album_artist")), get("title")) {
        (Some(a), Some(t)) => format!("{a} – {t}"),
        _ => String::new(),
    }
}

impl LyricsWindow {
    pub fn new(app: &App, paths: Vec<String>) -> LyricsWindow {
        let mut seen = HashSet::new();
        let paths: Vec<String> = paths.into_iter().filter(|p| seen.insert(p.clone())).collect();
        let cfg = &app.cfg;
        let mut w = LyricsWindow {
            paths,
            todo: vec![],
            rows: vec![],
            messages: HashMap::new(),
            selected: None,
            scroll_to: None,
            outcomes: vec![],
            saved_paths: vec![],
            running: false,
            run: 0,
            stop: Arc::new(AtomicBool::new(false)),
            eta: None,
            status_time: Instant::now(),
            existing: cfg.lyrics_existing.clone(),
            plain: cfg.lyrics_plain,
            instrumental: cfg.lyrics_instrumental,
            search: cfg.lyrics_search,
            head: String::new(),
            note: String::new(),
            status: String::new(),
        };
        w.replan(app);
        w
    }

    fn cfg(&self, base: &Settings) -> Settings {
        let mut c = base.clone();
        c.lyrics_existing = self.existing.clone();
        c.lyrics_plain = self.plain;
        c.lyrics_instrumental = self.instrumental;
        c.lyrics_search = self.search;
        c
    }

    fn replan(&mut self, app: &App) {
        if self.running {
            return;
        }
        let cfg = self.cfg(&app.cfg);
        self.rows.clear();
        self.messages.clear();
        let mut todo = vec![];
        let mut skipped = vec![];
        for p in &self.paths {
            let why = skip_reason(p, &cfg);
            if why.is_empty() {
                todo.push(p.clone());
            } else {
                skipped.push((p.clone(), why));
            }
        }
        for p in &todo {
            self.rows.push(Row {
                path: p.clone(),
                name: file_name(p),
                song: song(&app.results, p),
                status: tr!("Ready").into(),
                saved: String::new(),
                look: Look::Ready,
            });
            self.messages.insert(p.clone(), format!("{p}\n{}", tr!("Will be looked up on LRCLIB by its tags.")));
        }
        for (p, why) in &skipped {
            // listed last, in grey, as in the Fix window
            let (lrc, txt) = existing_lyrics(p);
            self.rows.push(Row {
                path: p.clone(),
                name: file_name(p),
                song: song(&app.results, p),
                status: tr_text(&short_skip(why)),
                saved: lrc.or(txt).map(|f| file_name(&f)).unwrap_or_default(),
                look: Look::Skip,
            });
            self.messages.insert(p.clone(), format!("{p}\n{}", tr_text(why)));
        }
        let n = todo.len();
        self.todo = todo;
        self.head = if n == 1 {
            tr!("Download synced lyrics for 1 song").into()
        } else if n > 0 {
            tr!("Download synced lyrics for {n} songs", n = n)
        } else {
            tr!("Nothing to look up").into()
        };
        let interval = default_interval(&cfg.lyrics_server);
        let mut lines = vec![];
        if n > 0 {
            let secs = n as f64 * (interval + 0.35) * if cfg.lyrics_search { 1.3 } else { 1.0 };
            lines.push(tr!(
                "LRCLIB asks programs to send one request at a time, so this takes {about}.",
                about = tr_text(&fmt_about(secs))
            ));
        }
        if !skipped.is_empty() {
            lines.push(if skipped.len() == 1 {
                tr!(
                    "1 song already has lyrics and is skipped (listed last, in grey); choose 'Download again and \
                     replace them' to look them up again."
                )
                .to_string()
            } else {
                tr!(
                    "{n} songs already have lyrics and are skipped (listed last, in grey); choose 'Download again \
                     and replace them' to look them up again.",
                    n = skipped.len()
                )
            });
        }
        lines.push(tr!("Songs without title and artist tags cannot be looked up. Lyrics files are never deleted.").into());
        // the credit to LRCLIB and LRCGET (test_lrclib_and_lrcget_are_credited), in the language in use
        let credit: String = CREDIT.into();
        lines.push(tr_text(&credit));
        self.note = lines.join("\n");
        self.status.clear();
    }

    pub fn start(&mut self, app: &mut App) {
        if self.running || self.todo.is_empty() {
            return;
        }
        let cfg = self.cfg(&app.cfg);
        // remember the chosen options
        app.cfg.lyrics_existing = cfg.lyrics_existing.clone();
        app.cfg.lyrics_plain = cfg.lyrics_plain;
        app.cfg.lyrics_instrumental = cfg.lyrics_instrumental;
        app.cfg.lyrics_search = cfg.lyrics_search;
        app.save_settings();
        self.running = true;
        self.stop = Arc::new(AtomicBool::new(false));
        self.outcomes.clear();
        self.run = RUNS.fetch_add(1, Ordering::SeqCst);
        let todo = self.todo.clone();
        for r in self.rows.iter_mut().filter(|r| todo.contains(&r.path)) {
            r.status = tr!("Waiting…").into();
        }
        self.eta = Some(ScanEta::new(todo.iter().map(|p| (p.clone(), 1u64)).collect()));
        self.update_status();
        // read by the worker thread, never changed by it
        let results: HashMap<String, FileResult> =
            todo.iter().filter_map(|p| app.results.get(p).map(|r| (p.clone(), r.clone()))).collect();
        crate::work::start_lyrics(todo, cfg, results, self.stop.clone(), self.run, app.post.clone());
    }

    fn update_status(&mut self) {
        let (done, n) = (self.outcomes.len(), self.todo.len());
        self.status_time = Instant::now();
        self.status = format!(
            "{} {}",
            tr!("Looking up {n} of {total}…", n = (done + 1).min(n), total = n),
            tr_text(&self.eta.as_ref().map(ScanEta::text).unwrap_or_default())
        );
    }

    pub fn started(&mut self, path: &str) {
        if let Some(r) = self.rows.iter_mut().find(|r| r.path == path) {
            r.status = tr!("Looking up…").into();
            r.look = Look::Busy;
            self.scroll_to = Some(path.to_string());
        }
    }

    pub fn done(&mut self, o: LyricsOutcome) {
        if let Some(e) = self.eta.as_mut() {
            e.add(&o.path);
        }
        if let Some(r) = self.rows.iter_mut().find(|r| r.path == o.path) {
            if let Some(t) = &o.track {
                r.song = t.label();
            }
            r.status = tr_text(&short_status(&o));
            r.saved = o.written.first().map(|f| file_name(f)).unwrap_or_default();
            r.look = Look::of(o.status);
            self.messages.insert(o.path.clone(), format!("{}\n{}", o.path, tr_text(&o.message)));
        }
        if o.saved() {
            self.saved_paths.push(o.written[0].clone());
        }
        self.outcomes.push(o);
        if self.running {
            self.update_status();
        }
    }

    pub fn finished(&mut self, stopped: bool) {
        self.running = false;
        let text = tr_text(&lyrics_summary(&self.outcomes));
        let mut c = text.chars();
        let cap = c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default();
        self.status = if stopped { tr!("Stopped. {summary}", summary = cap) } else { cap };
        let n = self.todo.len();
        let saved = self.saved_paths.len();
        self.head = if n == 1 {
            tr!("Lyrics saved for {saved} of 1 song", saved = saved)
        } else {
            tr!("Lyrics saved for {saved} of {n} songs", saved = saved, n = n)
        };
    }

    pub fn stop(&mut self) {
        if self.running {
            self.stop.store(true, Ordering::SeqCst);
            self.status = tr!("Stopping after the current song…").into();
        }
    }

    pub fn is_stopping(&self) -> bool {
        self.stop.load(Ordering::SeqCst)
    }

    /// Draws the window; false when it is closed.
    pub fn show(&mut self, app: &mut App, ctx: &egui::Context) -> bool {
        if self.running && self.status_time.elapsed().as_secs_f64() > 1.0 {
            self.update_status(); // the time left keeps counting down between songs
        }
        if self.running {
            ctx.request_repaint_after(std::time::Duration::from_millis(500));
        }
        let mut open = true;
        let mut close = false;
        let before = (self.existing.clone(), self.plain, self.instrumental, self.search);
        egui::Window::new(tr!("Download synced lyrics"))
            .id(egui::Id::unique("lyrics-window"))
            .open(&mut open)
            .collapsible(false)
            .default_width(860.0)
            .resizable(true)
            .pivot(Align2::CENTER_CENTER)
            .default_pos(ctx.content_rect().center())
            .show(ctx, |ui| {
                ui.label(RichText::new(&self.head).strong().size(15.0));
                ui.label(
                    RichText::new(tr!(
                        "Each song is looked up by the title, artist, album and length in its tags on LRCLIB \
                         (lrclib.net), a free, open lyrics database, and its synced lyrics are saved as an .lrc file \
                         with the same name next to it. Most music players find that file and show the lyrics line by \
                         line in time with the music. The audio files themselves are not changed."
                    ))
                    .color(hex(0x555555)),
                );
                ui.add_space(6.0);
                let normal = ui.visuals().text_color();
                egui::ScrollArea::vertical().max_height(260.0).id_salt("lyricsrows").show(ui, |ui| {
                    egui::Grid::new("lyricsgrid").num_columns(4).striped(true).spacing([14.0, 3.0]).show(ui, |ui| {
                        for h in [tr!("File"), tr!("Artist – title"), tr!("Status"), tr!("Saved as")] {
                            ui.label(RichText::new(h).strong());
                        }
                        ui.end_row();
                        for r in &self.rows {
                            let color = r.look.color(normal);
                            let sel = self.selected.as_deref() == Some(r.path.as_str());
                            let cell = |ui: &mut egui::Ui, text: &str, width: f32| {
                                ui.allocate_ui_with_layout(
                                    Vec2::new(width, 18.0),
                                    egui::Layout::left_to_right(egui::Align::Center),
                                    |ui| {
                                        ui.set_width(width);
                                        ui.add(
                                            egui::Label::new(RichText::new(text).color(color))
                                                .truncate()
                                                .sense(egui::Sense::click()),
                                        )
                                        .on_hover_text(text)
                                    },
                                )
                                .inner
                            };
                            let name = ui.add(egui::Button::selectable(sel, RichText::new(&r.name).color(color)));
                            let a = cell(ui, &r.song, 210.0);
                            let b = cell(ui, &r.status, 280.0);
                            let c = cell(ui, &r.saved, 170.0);
                            if self.scroll_to.as_deref() == Some(r.path.as_str()) {
                                name.scroll_to_me(Some(egui::Align::Center));
                            }
                            if name.clicked() || a.clicked() || b.clicked() || c.clicked() {
                                self.selected = Some(r.path.clone());
                            }
                            ui.end_row();
                        }
                    });
                });
                self.scroll_to = None;
                let detail = self
                    .selected
                    .as_ref()
                    .and_then(|p| self.messages.get(p).cloned())
                    .unwrap_or_else(|| tr!("Select a song to see the details.").into());
                ui.label(RichText::new(detail).color(hex(0x444444)));
                ui.add_space(6.0);
                ui.add_enabled_ui(!self.running, |ui| {
                    egui::Frame::group(ui.style()).show(ui, |ui| {
                        ui.label(RichText::new(tr!("Options")).strong());
                        ui.horizontal(|ui| {
                            ui.label(tr!("Songs that already have lyrics:"));
                            // the setting's values stay English; the menu shows them translated
                            egui::ComboBox::from_id_salt("lyrics_existing")
                                .selected_text(i18n::tr(&self.existing))
                                .width(300.0)
                                .show_ui(ui, |ui| {
                                    for c in EXISTING_CHOICES {
                                        ui.selectable_value(&mut self.existing, c.to_string(), i18n::tr(c));
                                    }
                                });
                        });
                        ui.checkbox(&mut self.plain, tr!("Save plain lyrics as .txt when a song has no synced lyrics"));
                        ui.checkbox(
                            &mut self.instrumental,
                            tr!("Mark instrumental songs with an .lrc holding [au: instrumental]"),
                        );
                        ui.checkbox(
                            &mut self.search,
                            tr!("Search by title and artist when there is no exact match (same length ±2 s)"),
                        );
                    });
                });
                ui.add_space(4.0);
                ui.label(RichText::new(&self.note).color(hex(0x666666)));
                ui.add_space(8.0);
                ui.horizontal(|ui| {
                    let n = self.todo.len().max(1);
                    ui.add(egui::ProgressBar::new(self.outcomes.len() as f32 / n as f32).desired_width(200.0));
                    ui.label(&self.status);
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.button(tr!("Close")).clicked() {
                            close = true;
                        }
                        if ui.add_enabled(self.running && !self.is_stopping(), egui::Button::new(tr!("Stop"))).clicked() {
                            self.stop();
                        }
                        let can_start = !self.running && !self.todo.is_empty();
                        if ui
                            .add_enabled(can_start, egui::Button::new(RichText::new(tr!("Download lyrics")).strong()))
                            .clicked()
                        {
                            self.start(app);
                        }
                        let can_show = !self.running && !self.saved_paths.is_empty();
                        if ui.add_enabled(can_show, egui::Button::new(tr!("Show in folder"))).clicked() {
                            reveal_in_file_manager(&self.saved_paths[0]);
                        }
                    });
                });
            });
        if (self.existing.clone(), self.plain, self.instrumental, self.search) != before {
            self.replan(app);
        }
        if !open || close {
            if self.running {
                app.confirm = Some(Confirm {
                    title: tr!("Downloading lyrics").into(),
                    text: tr!("Lyrics are still being downloaded. Stop and close? Lyrics saved so far are kept.").into(),
                    action: ConfirmAction::CloseLyrics,
                });
                return true;
            }
            return false;
        }
        true
    }
}

/// What the Lyrics tab shows for a song: the .lrc (or .txt) next to it, with
/// the timestamps in grey.
pub fn lyrics_job(path: Option<&str>, normal: Color32) -> LayoutJob {
    let mut job = LayoutJob::default();
    let muted = TextFormat { font_id: FontId::proportional(12.5), color: hex(0x666666), ..Default::default() };
    let time = TextFormat { font_id: FontId::monospace(11.5), color: hex(0x9aa0a6), ..Default::default() };
    let plain = TextFormat { font_id: FontId::proportional(14.0), color: normal, ..Default::default() };
    let Some(path) = path else { return job };
    match read_lyrics_file(path) {
        None => {
            let acc = if cfg!(target_os = "macos") { "Cmd" } else { "Ctrl" };
            job.append(&format!("{}\n\n", tr!("No lyrics next to this song yet.")), 0.0, muted.clone());
            job.append(
                &tr!(
                    "Get lyrics… ({key}) looks the song up on LRCLIB and saves its synced lyrics as {file} next to it.",
                    key = format!("{acc}+L"),
                    file = file_name(&sidecar(path, ".lrc"))
                ),
                0.0,
                muted,
            );
        }
        Some((kind, text, file)) => {
            let head = match kind {
                Status::Synced => tr!("Synced lyrics"),
                Status::Plain => tr!("Plain lyrics (no timing)"),
                _ => tr!("Instrumental"),
            };
            job.append(&format!("{head} · {}\n\n", file_name(&file)), 0.0, muted.clone());
            if kind == Status::Instrumental {
                job.append(tr!("LRCLIB lists this song as instrumental: it has no lyrics."), 0.0, muted);
                return job;
            }
            for line in splitlines(&text) {
                let split = if kind == Status::Synced { bracket_prefix(line) } else { None };
                match split {
                    Some((stamps, rest)) if is_time(stamps) => {
                        job.append(&format!("{}  ", stamps.trim()), 0.0, time.clone());
                        job.append(&format!("{rest}\n"), 0.0, plain.clone());
                    }
                    // plain text, or an LRC header such as [ar: ...]
                    Some(_) => job.append(&format!("{line}\n"), 0.0, muted.clone()),
                    None => job.append(&format!("{line}\n"), 0.0, plain.clone()),
                }
            }
        }
    }
    job
}

/// `^((?:\s*\[[^\]]*\])+)\s?(.*)$`: (the bracketed prefix, the rest).
fn bracket_prefix(line: &str) -> Option<(&str, &str)> {
    let mut pos = 0;
    loop {
        let rest = &line[pos..];
        let after = rest.trim_start_matches(char::is_whitespace);
        if !after.starts_with('[') {
            break;
        }
        match after[1..].find(']') {
            Some(j) => pos += rest.len() - after.len() + j + 2,
            None => break,
        }
    }
    if pos == 0 {
        return None;
    }
    let mut rest = &line[pos..];
    if let Some(c) = rest.chars().next().filter(|c| c.is_whitespace()) {
        rest = &rest[c.len_utf8()..];
    }
    Some((&line[..pos], rest))
}

/// `\s*\[\d`: a timestamp rather than a header such as `[ar: ...]`.
fn is_time(stamps: &str) -> bool {
    let s = stamps.trim_start();
    s.strip_prefix('[').and_then(|r| r.chars().next()).is_some_and(|c| c.is_ascii_digit())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lyrics_lines_split_timestamps_from_words() {
        assert_eq!(bracket_prefix("[00:01.00][00:02.00]  la la"), Some(("[00:01.00][00:02.00]", " la la")));
        assert_eq!(bracket_prefix("[ar: The Band]"), Some(("[ar: The Band]", "")));
        assert_eq!(bracket_prefix("no stamps"), None);
        assert!(is_time(" [00:01.00]") && !is_time("[ar: x]"));
    }
}
