//! The Properties window (2.0 "Edit Info", `docs/player-spec.md` → "Song
//! information"; called Properties since 2.1): the title, artist, album, album
//! artist, genre, release date, track, disc and composer of one song or of an
//! album's songs.  The tags are read with ffprobe in a thread, the changes are
//! checked with `tagedit::check_changes()` and written in a thread with
//! `tagedit::save_tags()`.  The window can be resized and its fields follow its
//! width; egui keeps its size (under the fixed `WINDOW_ID`) for the session.
//!
//! 2.3: Acquire Metadata… (bottom left) opens the MusicBrainz window for the same
//! songs (`EditInfo::acquire`, done by the player); when that saves,
//! `files_changed()` reads the songs again and updates the fields nothing was
//! typed into.  Each field's Search button opens the browser at
//! `tagedit::web_search_url()` and then watches the clipboard
//! (`crate::web::clipboard_text()`, every `WATCH_POLL_MS` for `WATCH_SECONDS`): the
//! first new text copied is put into that field (`tagedit::clipboard_value()`),
//! which flashes in the accent colour.

use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant};

use egui::{Align2, CornerRadius, RichText, Stroke};
use realhires::i18n::{self, tr_text};
use realhires::probe::Tags;
use realhires::settings::Settings;
use realhires::tagedit::{self, Changes, TagOutcome, FIELDS};
use realhires::{n_, tr};

use crate::icons::{self, Icon};
use crate::look::Palette;

enum Msg {
    Read(Vec<Tags>),
    /// the songs read again after another window wrote into them
    Reread(Vec<Tags>),
    Progress(usize, usize),
    Saved(Vec<TagOutcome>),
}

/// The clipboard watch after a field's Search button (2.3).
pub struct Watch {
    /// the field's index in `FIELDS`
    pub field: usize,
    /// what the clipboard held at the click
    pub start: String,
    /// when the watch ends by itself
    pub until: Instant,
    next: Instant,
}

#[derive(PartialEq)]
enum Stage {
    Reading,
    Editing,
    Saving(usize, usize),
    Done(String, Vec<String>),
}

pub struct EditInfo {
    pub paths: Vec<String>,
    /// the song's title (or the album's), and "artist · album" (or "artist · 12 songs")
    pub heading: String,
    pub subheading: String,
    /// Album Properties (the MusicBrainz window of Acquire Metadata… looks the album up)
    pub album: bool,
    focus_done: bool,
    rx: Receiver<Msg>,
    stage: Stage,
    initial: Vec<Option<String>>,
    pub text: Vec<String>,
    pub error: String,
    /// Acquire Metadata… was clicked: the player opens the MusicBrainz window (and clears this)
    pub acquire: bool,
    /// the clipboard watch of a Search button
    pub watch: Option<Watch>,
    /// the line under the fields: "Copy the answer…", "Filled in from the clipboard."
    pub note: String,
    /// the field filled in from the clipboard last, and when (it flashes)
    pub filled: Option<(usize, Instant)>,
    /// where each field's Search button was drawn last (tests click them)
    pub search_rects: Vec<egui::Rect>,
    ctx: egui::Context,
}

/// While a Search button watches the clipboard (translated where it is shown).
pub const WATCHING: &str = n_!("Copy the answer in your browser and it will be filled in here.");
/// After a copied text was put into a field.
pub const FILLED: &str = n_!("Filled in from the clipboard.");
/// How long a filled-in field flashes.
const FLASH: Duration = Duration::from_millis(1200);

/// What the window asks of the player after a frame.
pub enum Outcome {
    Open,
    Closed,
    /// files were written: the library must be read again
    Saved { close: bool },
}

/// Shown (translated) in a field whose songs have different values.
pub const SEVERAL: &str = n_!("Several values: kept unless you type here");
/// The window's title (2.1: "Properties", as the menu items "Properties…" and
/// "Album Properties…"), English; shown with `i18n::tr()`.
pub const TITLE: &str = n_!("Properties");
/// One id for every Properties window, so its size and place stay for the session.
pub const WINDOW_ID: &str = "player-edit-info";
/// The narrowest a field gets.
const MIN_FIELD: f32 = 220.0;

impl EditInfo {
    pub fn new(ctx: &egui::Context, paths: Vec<String>, heading: String, subheading: String, album: bool) -> Self {
        let (tx, rx) = mpsc::channel();
        read_tags(ctx, paths.clone(), tx, false);
        EditInfo {
            paths,
            heading,
            subheading,
            album,
            focus_done: false,
            rx,
            stage: Stage::Reading,
            initial: vec![Some(String::new()); FIELDS.len()],
            text: vec![String::new(); FIELDS.len()],
            error: String::new(),
            acquire: false,
            watch: None,
            note: String::new(),
            filled: None,
            search_rects: vec![egui::Rect::NOTHING; FIELDS.len()],
            ctx: ctx.clone(),
        }
    }

    pub fn editing(&self) -> bool {
        self.stage == Stage::Editing
    }

    /// Other windows (MusicBrainz) wrote into these files: read the songs again and
    /// show the new values in the fields nothing was typed into.
    pub fn files_changed(&mut self, paths: &[String]) {
        if !self.editing() || !paths.iter().any(|p| self.paths.contains(p)) {
            return;
        }
        let (tx, rx) = mpsc::channel();
        self.rx = rx;
        read_tags(&self.ctx, self.paths.clone(), tx, true);
    }

    /// What the Search buttons search with: the fields as they are now (several
    /// songs: without the title, so the album is searched for).
    pub fn search_values(&self) -> Vec<(&'static str, String)> {
        ["title", "artist", "album", "album_artist"]
            .into_iter()
            .map(|f| {
                let i = FIELDS.iter().position(|x| *x == f).unwrap_or(0);
                let v = if f == "title" && self.paths.len() > 1 { String::new() } else { tagedit::clean_value(&self.text[i]) };
                (f, v)
            })
            .collect()
    }

    /// The Google search of a field's Search button.
    pub fn search_url(&self, field: &str) -> String {
        let values = self.search_values();
        let refs: Vec<(&str, &str)> = values.iter().map(|(k, v)| (*k, v.as_str())).collect();
        tagedit::web_search_url(field, &refs, i18n::language())
    }

    /// A field's Search button: the browser searches for it, and the next text
    /// copied within two minutes is put into the field.
    pub fn search(&mut self, field: usize) {
        if !self.editing() {
            return;
        }
        self.stop_watch();
        let url = self.search_url(FIELDS[field]);
        if let Err(e) = crate::web::open_url(&url) {
            self.note = tr!("The browser could not be opened: {error}", error = e);
            return;
        }
        let now = Instant::now();
        self.watch = Some(Watch {
            field,
            start: crate::web::clipboard_text(),
            until: now + Duration::from_secs(tagedit::WATCH_SECONDS),
            next: now + Duration::from_millis(tagedit::WATCH_POLL_MS),
        });
        self.note = i18n::tr(WATCHING).to_string();
        self.ctx.request_repaint_after(Duration::from_millis(tagedit::WATCH_POLL_MS));
    }

    pub fn stop_watch(&mut self) {
        if self.watch.take().is_some() {
            self.note.clear();
        }
    }

    /// Look at the clipboard once (`now` is the time to compare with the watch's
    /// end); the field filled in, or None.
    pub fn watch_step(&mut self, now: Instant) -> Option<usize> {
        let w = self.watch.as_ref()?;
        if now > w.until {
            self.stop_watch();
            return None;
        }
        let text = crate::web::clipboard_text();
        let value = if text != w.start { tagedit::clipboard_value(&text) } else { String::new() };
        if value.is_empty() {
            return None;
        }
        let field = w.field;
        self.stop_watch();
        self.text[field] = value;
        self.filled = Some((field, Instant::now()));
        self.note = i18n::tr(FILLED).to_string();
        self.ctx.request_repaint();
        Some(field)
    }

    /// The watch's look at the clipboard when it is due (every `WATCH_POLL_MS`).
    fn tick(&mut self) {
        let Some(w) = self.watch.as_mut() else { return };
        let now = Instant::now();
        if now >= w.next {
            w.next = now + Duration::from_millis(tagedit::WATCH_POLL_MS);
            self.watch_step(now);
        }
        if let Some(w) = &self.watch {
            self.ctx.request_repaint_after(w.next.saturating_duration_since(now));
        }
    }

    pub fn reading(&self) -> bool {
        self.stage == Stage::Reading
    }

    pub fn saving(&self) -> bool {
        matches!(self.stage, Stage::Saving(..))
    }

    /// The fields whose text differs from what they started with (compared as
    /// `clean_value()`s; a "several values" field when anything was typed).
    pub fn changes(&self) -> Changes {
        FIELDS.iter().enumerate().filter(|(i, _)| self.changed(*i)).map(|(i, f)| (f.to_string(), self.text[i].clone())).collect()
    }

    fn changed(&self, i: usize) -> bool {
        let now = tagedit::clean_value(&self.text[i]);
        match &self.initial[i] {
            Some(v) => now != tagedit::clean_value(v),
            None => !now.is_empty(),
        }
    }

    pub fn set(&mut self, field: &str, value: &str) {
        if let Some(i) = FIELDS.iter().position(|f| *f == field) {
            self.text[i] = value.to_string();
        }
    }

    /// Check and start saving; Ok(false) when nothing changed.
    pub fn save(&mut self, ctx: &egui::Context, cfg: &Settings) -> bool {
        self.stop_watch();
        let changes = self.changes();
        if changes.is_empty() {
            return false;
        }
        if let Err(e) = tagedit::check_changes(&changes) {
            self.error = e.0;
            return true;
        }
        self.error.clear();
        let (tx, rx) = mpsc::channel();
        self.rx = rx;
        self.stage = Stage::Saving(0, self.paths.len());
        let (paths, cfg, ctx) = (self.paths.clone(), cfg.clone(), ctx.clone());
        let _ = std::thread::Builder::new().name("edit-info-save".into()).spawn(move || {
            let progress = tx.clone();
            let c = ctx.clone();
            let out = tagedit::save_tags(&paths, &changes, Some(&cfg), move |i, n, _| {
                let _ = progress.send(Msg::Progress(i, n));
                c.request_repaint();
            });
            let _ = tx.send(Msg::Saved(out));
            ctx.request_repaint();
        });
        true
    }

    /// Take what the threads posted; Some(close) once saving has finished.
    fn poll(&mut self) -> Option<bool> {
        let mut finished = None;
        while let Ok(m) = self.rx.try_recv() {
            match m {
                Msg::Read(tags) => {
                    for (i, (_, v)) in tagedit::common_values(&tags).into_iter().enumerate() {
                        self.text[i] = v.clone().unwrap_or_default();
                        self.initial[i] = v;
                    }
                    self.stage = Stage::Editing;
                }
                Msg::Reread(tags) => {
                    if self.stage != Stage::Editing {
                        continue;
                    }
                    for (i, (_, v)) in tagedit::common_values(&tags).into_iter().enumerate() {
                        if !self.changed(i) {
                            self.text[i] = v.clone().unwrap_or_default();
                        }
                        self.initial[i] = v;
                    }
                }
                Msg::Progress(i, n) => self.stage = Stage::Saving(i, n),
                Msg::Saved(out) => {
                    let (head, lines) = tagedit::save_summary(&out);
                    let clean = out.iter().all(|o| o.ok && o.message.is_empty());
                    self.stage = Stage::Done(head, lines);
                    finished = Some(clean);
                }
            }
        }
        finished
    }

    /// Draw the window; `keys`: whether Esc and Enter belong to it this frame (not
    /// while the MusicBrainz window of Acquire Metadata… is open over it).
    pub fn show(&mut self, ctx: &egui::Context, pal: &Palette, cfg: &Settings, keys: bool) -> Outcome {
        let saved = self.poll();
        self.tick();
        let mut result = match saved {
            Some(close) => Outcome::Saved { close },
            None => Outcome::Open,
        };
        let mut close = false;
        let n = self.paths.len();
        egui::Window::new(i18n::tr(TITLE))
            .id(egui::Id::new(WINDOW_ID))
            .collapsible(false)
            .resizable([true, true])
            .default_width(560.0)
            .min_width(420.0)
            .pivot(Align2::CENTER_CENTER)
            .default_pos(ctx.content_rect().center())
            .show(ctx, |ui| {
                let editing = self.stage == Stage::Editing;
                ui.label(RichText::new(&self.heading).font(crate::look::title(18.0)).color(pal.text));
                if !self.subheading.is_empty() {
                    ui.label(RichText::new(&self.subheading).color(pal.secondary));
                }
                ui.add_space(8.0);
                match &self.stage {
                    Stage::Reading => {
                        let text = if n == 1 {
                            tr!("Reading the information of 1 song…").to_string()
                        } else {
                            tr!("Reading the information of {n} songs…", n = realhires::stats::thousands(n as i64))
                        };
                        ui.label(RichText::new(text).color(pal.secondary));
                    }
                    Stage::Saving(i, total) => {
                        let text = tr!("Saving {i} of {n}…", i = (i + 1).min(*total), n = total);
                        ui.label(RichText::new(text).color(pal.secondary));
                    }
                    _ => {}
                }
                if let Stage::Done(head, lines) = &self.stage {
                    // `tagedit::save_summary()`'s English texts, translated when shown
                    ui.label(RichText::new(tr_text(head)).strong());
                    for l in lines {
                        ui.label(tr_text(l));
                    }
                    ui.add_space(8.0);
                    if ui.button(tr!("Close")).clicked() || (keys && ui.input(|i| i.key_pressed(egui::Key::Escape))) {
                        close = true;
                    }
                    return;
                }
                let mut search = None;
                let watching = self.watch.as_ref().map(|w| w.field);
                let flash = self.filled.filter(|(_, t)| t.elapsed() < FLASH);
                egui::Grid::new("edit-info-fields").num_columns(2).spacing([14.0, 6.0]).show(ui, |ui| {
                    for (i, f) in FIELDS.iter().enumerate() {
                        ui.label(i18n::tr(tagedit::label(f)));
                        ui.vertical(|ui| {
                            ui.horizontal(|ui| {
                                // the fields grow with the window (the grid's last column takes the rest),
                                // with room for the Search button at their right end
                                let width = (ui.available_width() - 32.0).max(MIN_FIELD);
                                let mut edit = egui::TextEdit::singleline(&mut self.text[i]).desired_width(width);
                                if self.initial[i].is_none() {
                                    edit = edit.hint_text(i18n::tr(SEVERAL));
                                }
                                let r = ui.add_enabled(editing, edit);
                                // the cursor starts in Title for one song, in Album for an album
                                let first = if n == 1 { "title" } else { "album" };
                                if editing && !self.focus_done && *f == first {
                                    r.request_focus();
                                    self.focus_done = true;
                                }
                                // a field just filled in from the clipboard flashes in the accent colour
                                if let Some((_, t)) = flash.filter(|(fi, _)| *fi == i) {
                                    let a = 1.0 - t.elapsed().as_secs_f32() / FLASH.as_secs_f32();
                                    let p = ui.painter();
                                    p.rect_filled(r.rect, CornerRadius::same(4), pal.accent.gamma_multiply(0.30 * a));
                                    p.rect_stroke(r.rect, CornerRadius::same(4), Stroke::new(2.0, pal.accent.gamma_multiply(a)), egui::StrokeKind::Outside);
                                    ui.ctx().request_repaint();
                                }
                                // 2.3: search the web for this field ("Search the web for the composer")
                                let tip = tr!("Search the web for the {field}", field = tagedit::search_name(f, i18n::language()));
                                let b = icons::button(ui, Icon::Search, 24.0, watching == Some(i), editing, &tip);
                                self.search_rects[i] = b.rect;
                                if b.clicked() {
                                    search = Some(i);
                                }
                            });
                            let h = tagedit::hint(f);
                            if !h.is_empty() {
                                ui.label(RichText::new(i18n::tr(h)).small().color(pal.secondary));
                            }
                        });
                        ui.end_row();
                    }
                });
                if let Some(i) = search {
                    self.search(i);
                }
                if !self.note.is_empty() {
                    ui.add_space(4.0);
                    ui.label(RichText::new(&self.note).small().color(pal.secondary));
                }
                if !self.error.is_empty() {
                    ui.add_space(4.0);
                    // `tagedit::check_changes()`'s English message, translated when shown
                    ui.label(RichText::new(tr_text(&self.error)).color(egui::Color32::from_rgb(0xe0, 0x30, 0x30)));
                }
                ui.add_space(8.0);
                ui.horizontal(|ui| {
                    let saving = self.saving();
                    // 2.3: the MusicBrainz window for the same songs, at the bottom left
                    if ui.add_enabled(!saving, egui::Button::new(tr!("Acquire Metadata…"))).clicked() {
                        self.acquire = true;
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.add_enabled(editing, egui::Button::new(RichText::new(tr!("Save")).strong())).clicked()
                            || (keys && editing && ui.input(|i| i.key_pressed(egui::Key::Enter)))
                        {
                            if !self.save(ctx, cfg) {
                                close = true;
                            }
                        }
                        if ui.add_enabled(!saving, egui::Button::new(tr!("Cancel"))).clicked()
                            || (keys && !saving && ui.input(|i| i.key_pressed(egui::Key::Escape)))
                        {
                            close = true;
                        }
                    });
                });
            });
        if close {
            self.stop_watch();
            result = match result {
                Outcome::Saved { .. } => Outcome::Saved { close: true },
                _ => Outcome::Closed,
            };
        }
        result
    }
}

/// Read the songs' tags with ffprobe in a thread (`again`: after another window wrote them).
fn read_tags(ctx: &egui::Context, paths: Vec<String>, tx: mpsc::Sender<Msg>, again: bool) {
    let ctx = ctx.clone();
    let _ = std::thread::Builder::new().name("edit-info-read".into()).spawn(move || {
        let tags = paths.iter().map(|p| realhires::probe::probe(p).map(|i| i.tags).unwrap_or_default()).collect();
        let _ = tx.send(if again { Msg::Reread(tags) } else { Msg::Read(tags) });
        ctx.request_repaint();
    });
}

enum JobMsg {
    Progress(usize, usize),
    Saved(Vec<TagOutcome>),
}

/// Renaming a genre, composer or year, or merging genres (2.1): `tagedit::rename_group()`
/// writes the new name into every file in a thread; the window shows how far it is, then
/// the outcome as Edit Info does (`tagedit::save_summary()`, files that could not be
/// changed included).  The player reads the library again when it is done.
pub struct RenameJob {
    pub title: String,
    pub kind: &'static str,
    pub name: String,
    pub paths: Vec<String>,
    rx: Receiver<JobMsg>,
    progress: (usize, usize),
    pub outcomes: Option<Vec<TagOutcome>>,
    reported: bool,
}

impl RenameJob {
    pub fn new(ctx: &egui::Context, title: String, kind: &'static str, paths: Vec<String>, name: String, cfg: Settings) -> Self {
        let (tx, rx) = mpsc::channel();
        let (list, new, ctx2) = (paths.clone(), name.clone(), ctx.clone());
        let _ = std::thread::Builder::new().name("rename-group".into()).spawn(move || {
            let progress_tx = tx.clone();
            let ctx3 = ctx2.clone();
            let out = tagedit::rename_group(kind, &list, &new, Some(&cfg), move |i, n, _| {
                let _ = progress_tx.send(JobMsg::Progress(i, n));
                ctx3.request_repaint();
            });
            let _ = tx.send(JobMsg::Saved(out));
            ctx2.request_repaint();
        });
        let n = paths.len();
        RenameJob { title, kind, name, paths, rx, progress: (0, n), outcomes: None, reported: false }
    }

    pub fn done(&self) -> bool {
        self.outcomes.is_some()
    }

    /// Draw the window; (still open, the files were just written).
    pub fn show(&mut self, ctx: &egui::Context, pal: &Palette) -> (bool, bool) {
        while let Ok(m) = self.rx.try_recv() {
            match m {
                JobMsg::Progress(i, n) => self.progress = (i, n),
                JobMsg::Saved(out) => self.outcomes = Some(out),
            }
        }
        let finished = self.outcomes.is_some() && !self.reported;
        if finished {
            self.reported = true;
        }
        let mut close = false;
        let n = self.paths.len();
        // "Giving 3 songs the genre “Jazz”", whole sentences per grouping (as the Python app's)
        let (one, many) = match self.kind {
            "genres" => (n_!("Giving 1 song the genre “{name}”"), n_!("Giving {n} songs the genre “{name}”")),
            "composers" => (n_!("Giving 1 song the composer “{name}”"), n_!("Giving {n} songs the composer “{name}”")),
            _ => (n_!("Giving 1 song the year “{name}”"), n_!("Giving {n} songs the year “{name}”")),
        };
        let heading = i18n::fill(
            i18n::tr(if n == 1 { one } else { many }),
            &[("n", realhires::stats::thousands(n as i64)), ("name", self.name.clone())],
        );
        egui::Window::new(self.title.clone())
            .id(egui::Id::new("player-rename-job"))
            .collapsible(false)
            .resizable(false)
            .pivot(Align2::CENTER_CENTER)
            .default_pos(ctx.content_rect().center())
            .show(ctx, |ui| {
                ui.set_max_width(460.0);
                ui.label(RichText::new(heading).strong());
                ui.add_space(6.0);
                match &self.outcomes {
                    None => {
                        let (i, total) = self.progress;
                        let text = tr!(
                            "Saving {i} of {n}…",
                            i = realhires::stats::thousands((i + 1).min(total.max(1)) as i64),
                            n = realhires::stats::thousands(total as i64)
                        );
                        ui.label(RichText::new(text).color(pal.secondary));
                        ui.add(egui::ProgressBar::new(if total > 0 { i as f32 / total as f32 } else { 0.0 }));
                    }
                    Some(out) => {
                        // `tagedit::save_summary()`'s English texts, translated when shown
                        let (head, lines) = tagedit::save_summary(out);
                        ui.label(tr_text(&head));
                        for l in lines.iter().take(12) {
                            ui.label(RichText::new(tr_text(l)).color(pal.secondary));
                        }
                        if lines.len() > 12 {
                            let more = realhires::stats::thousands((lines.len() - 12) as i64);
                            ui.label(RichText::new(tr!("… and {n} more.", n = more)).color(pal.secondary));
                        }
                        ui.add_space(6.0);
                        if ui.button(tr!("Close")).clicked() || ui.input(|i| i.key_pressed(egui::Key::Escape)) {
                            close = true;
                        }
                    }
                }
            });
        (!close, finished)
    }
}
