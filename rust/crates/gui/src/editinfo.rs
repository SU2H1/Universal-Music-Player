//! The Edit Info window (2.0, `docs/player-spec.md` → "Song information"):
//! the title, artist, album, album artist, genre, release date, track, disc
//! and composer of one song or of an album's songs.  The tags are read with
//! ffprobe in a thread, the changes are checked with `tagedit::check_changes()`
//! and written in a thread with `tagedit::save_tags()`.

use std::sync::mpsc::{self, Receiver};

use egui::{Align2, RichText};
use realhires::i18n::{self, tr_text};
use realhires::probe::Tags;
use realhires::settings::Settings;
use realhires::tagedit::{self, Changes, TagOutcome, FIELDS};
use realhires::{n_, tr};

use crate::look::Palette;

enum Msg {
    Read(Vec<Tags>),
    Progress(usize, usize),
    Saved(Vec<TagOutcome>),
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
    focus_done: bool,
    rx: Receiver<Msg>,
    stage: Stage,
    initial: Vec<Option<String>>,
    pub text: Vec<String>,
    pub error: String,
}

/// What the window asks of the player after a frame.
pub enum Outcome {
    Open,
    Closed,
    /// files were written: the library must be read again
    Saved { close: bool },
}

/// Shown (translated) in a field whose songs have different values.
pub const SEVERAL: &str = n_!("Several values: kept unless you type here");

impl EditInfo {
    pub fn new(ctx: &egui::Context, paths: Vec<String>, heading: String, subheading: String) -> Self {
        let (tx, rx) = mpsc::channel();
        let list = paths.clone();
        let ctx = ctx.clone();
        let _ = std::thread::Builder::new().name("edit-info-read".into()).spawn(move || {
            let tags = list.iter().map(|p| realhires::probe::probe(p).map(|i| i.tags).unwrap_or_default()).collect();
            let _ = tx.send(Msg::Read(tags));
            ctx.request_repaint();
        });
        EditInfo {
            paths,
            heading,
            subheading,
            focus_done: false,
            rx,
            stage: Stage::Reading,
            initial: vec![Some(String::new()); FIELDS.len()],
            text: vec![String::new(); FIELDS.len()],
            error: String::new(),
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
        FIELDS
            .iter()
            .enumerate()
            .filter(|(i, _)| {
                let now = tagedit::clean_value(&self.text[*i]);
                match &self.initial[*i] {
                    Some(v) => now != tagedit::clean_value(v),
                    None => !now.is_empty(),
                }
            })
            .map(|(i, f)| (f.to_string(), self.text[i].clone()))
            .collect()
    }

    pub fn set(&mut self, field: &str, value: &str) {
        if let Some(i) = FIELDS.iter().position(|f| *f == field) {
            self.text[i] = value.to_string();
        }
    }

    /// Check and start saving; Ok(false) when nothing changed.
    pub fn save(&mut self, ctx: &egui::Context, cfg: &Settings) -> bool {
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

    pub fn show(&mut self, ctx: &egui::Context, pal: &Palette, cfg: &Settings) -> Outcome {
        let saved = self.poll();
        let mut result = match saved {
            Some(close) => Outcome::Saved { close },
            None => Outcome::Open,
        };
        let mut close = false;
        let n = self.paths.len();
        egui::Window::new(tr!("Edit Info"))
            .id(egui::Id::new("player-edit-info"))
            .collapsible(false)
            .resizable(false)
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
                    if ui.button(tr!("Close")).clicked() || ui.input(|i| i.key_pressed(egui::Key::Escape)) {
                        close = true;
                    }
                    return;
                }
                egui::Grid::new("edit-info-fields").num_columns(2).spacing([14.0, 6.0]).show(ui, |ui| {
                    for (i, f) in FIELDS.iter().enumerate() {
                        ui.label(i18n::tr(tagedit::label(f)));
                        ui.vertical(|ui| {
                            let mut edit = egui::TextEdit::singleline(&mut self.text[i]).desired_width(400.0);
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
                            let h = tagedit::hint(f);
                            if !h.is_empty() {
                                ui.label(RichText::new(i18n::tr(h)).small().color(pal.secondary));
                            }
                        });
                        ui.end_row();
                    }
                });
                if !self.error.is_empty() {
                    ui.add_space(4.0);
                    // `tagedit::check_changes()`'s English message, translated when shown
                    ui.label(RichText::new(tr_text(&self.error)).color(egui::Color32::from_rgb(0xe0, 0x30, 0x30)));
                }
                ui.add_space(8.0);
                ui.horizontal(|ui| {
                    let saving = self.saving();
                    if ui.add_enabled(editing, egui::Button::new(RichText::new(tr!("Save")).strong())).clicked()
                        || (editing && ui.input(|i| i.key_pressed(egui::Key::Enter)))
                    {
                        if !self.save(ctx, cfg) {
                            close = true;
                        }
                    }
                    if ui.add_enabled(!saving, egui::Button::new(tr!("Cancel"))).clicked()
                        || (!saving && ui.input(|i| i.key_pressed(egui::Key::Escape)))
                    {
                        close = true;
                    }
                });
            });
        if close {
            result = match result {
                Outcome::Saved { .. } => Outcome::Saved { close: true },
                _ => Outcome::Closed,
            };
        }
        result
    }
}
