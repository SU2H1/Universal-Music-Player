//! The MusicBrainz window (2.1, `docs/player-spec.md` → "Song information from
//! MusicBrainz"), the port of `realhires/player_mb.py`: "Get info from
//! MusicBrainz…" for a song and "Get album info from MusicBrainz…" for an album.
//! The songs' tags are read in a thread, MusicBrainz is searched with them, the
//! chosen release's track list is read and matched to the songs, the changes
//! are shown song by song, and Save writes each song's own changes with
//! `tagedit::save_tags()`.  Every request goes through one client, which keeps
//! to one request per second together with the automatic lookup.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};

use egui::{Align2, RichText};
use realhires::i18n::{self, tr_text};
use realhires::musicbrainz::{self as mb, MbError, MusicBrainzClient, Release, ReleaseDetail, ReleaseTrack, Song};
use realhires::settings::Settings;
use realhires::tagedit::{self, Changes, TagOutcome, FIELDS};
use realhires::{n_, tr};

use crate::look::Palette;

pub const EMPTY: &str = n_!("(empty)");

enum Msg {
    Songs(Vec<Song>),
    Found(Result<Vec<Release>, String>),
    Detail(String, Result<Option<ReleaseDetail>, String>),
    Progress(usize, usize),
    Saved(Vec<TagOutcome>),
}

/// What the status line says (translated when drawn).
#[derive(Clone, Debug, PartialEq)]
pub enum Status {
    Nothing,
    Reading(usize),
    Searching,
    ReadingRelease,
    Found(usize, usize),
    NothingFound,
    Unreadable,
    Saving(usize, usize),
}

/// The grey facts of a release in the list (as `player_mb.release_facts`).
pub fn release_facts(r: &Release) -> Vec<String> {
    let mut parts: Vec<String> =
        [&r.date, &r.country, &r.formats].iter().filter(|x| !x.is_empty()).map(|x| x.to_string()).collect();
    if r.track_count != 0 {
        parts.push(if r.track_count == 1 { tr!("1 track").to_string() } else { tr!("{n} tracks", n = r.track_count) });
    }
    if !r.status.is_empty() && r.status != "Official" {
        parts.push(i18n::tr(&r.status).to_string());
    }
    parts
}

/// One line of the release list.
pub fn release_text(r: &Release) -> String {
    let head = if r.artist.is_empty() {
        r.title.clone()
    } else {
        tr!("{title} — {artist}", title = r.title, artist = r.artist)
    };
    let facts = release_facts(r);
    if facts.is_empty() {
        head
    } else {
        format!("{head}  ·  {}", facts.join(" · "))
    }
}

/// The lines of the changes preview (text, grey) and each song's changes.
pub fn preview_lines(
    songs: &[Song],
    detail: &ReleaseDetail,
    matched: &[Option<ReleaseTrack>],
    fields: &[&str],
    only_empty: bool,
) -> (Vec<(String, bool)>, Vec<(String, Changes)>) {
    let mut lines = vec![];
    let mut plans = vec![];
    // in the release's track order, songs without a track last (in their own order)
    let mut pairs: Vec<(&Song, &Option<ReleaseTrack>)> = songs.iter().zip(matched).collect();
    pairs.sort_by_key(|(_, t)| match t {
        Some(t) => (0, t.disc, t.number),
        None => (1, 0, 0),
    });
    for (s, t) in pairs {
        let name = realhires::paths::file_name(&s.path);
        let Some(t) = t else {
            lines.push((tr!("{file}: no matching track on this release, left as it is", file = name), true));
            plans.push((s.path.clone(), vec![]));
            continue;
        };
        let num = if t.disc_count > 1 { format!("{}-{}", t.disc, t.number) } else { t.number.to_string() };
        lines.push((tr!("{file} → {number}. {title}", file = name, number = num, title = t.title), false));
        let ch = mb::changes_for(s, detail, Some(t), fields, only_empty);
        if ch.is_empty() {
            lines.push((format!("    {}", tr!("Nothing to change")), true));
        }
        for (f, new) in &ch {
            let old = s.tag(f);
            let old = if old.is_empty() { i18n::tr(EMPTY).to_string() } else { old };
            let text = tr!("{field}: {old} → {new}", field = i18n::tr(tagedit::label(f)), old = old, new = new);
            lines.push((format!("    {text}"), true));
        }
        plans.push((s.path.clone(), ch));
    }
    (lines, plans)
}

/// What the window asks of the player after a frame.
pub enum Outcome {
    Open,
    Closed,
    /// files were written: the library must be read again
    Saved { close: bool },
}

pub struct MbWindow {
    pub paths: Vec<String>,
    pub album: bool,
    pub heading: String,
    pub subheading: String,
    tx: Sender<Msg>,
    rx: Receiver<Msg>,
    stop: Arc<AtomicBool>,
    client: Arc<Mutex<MusicBrainzClient>>,
    ctx: egui::Context,
    pub songs: Vec<Song>,
    pub query_title: String,
    pub query_artist: String,
    pub releases: Vec<Release>,
    pub selected: Option<usize>,
    details: HashMap<String, Option<ReleaseDetail>>,
    pub detail: Option<ReleaseDetail>,
    matched: Vec<Option<ReleaseTrack>>,
    pub fields: Vec<bool>,
    pub only_empty: bool,
    pub lines: Vec<(String, bool)>,
    pub plans: Vec<(String, Changes)>,
    pub status: Status,
    /// an English message from the core (translated when drawn)
    pub error: String,
    pub busy: bool,
    pub saving: bool,
    /// the save summary (English, translated when drawn) once saving has finished
    pub done: Option<(String, Vec<String>)>,
    pub outcomes: Vec<TagOutcome>,
    cfg: Settings,
}

impl Drop for MbWindow {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

impl MbWindow {
    pub fn new(ctx: &egui::Context, cfg: &Settings, paths: Vec<String>, album: bool, heading: String, subheading: String) -> Self {
        let (tx, rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        let client = MusicBrainzClient::new(&cfg.musicbrainz_server).with_stop(stop.clone());
        let album = album || paths.len() > 1;
        let w = MbWindow {
            status: Status::Reading(paths.len()),
            paths,
            album,
            heading,
            subheading,
            tx,
            rx,
            stop,
            client: Arc::new(Mutex::new(client)),
            ctx: ctx.clone(),
            songs: vec![],
            query_title: String::new(),
            query_artist: String::new(),
            releases: vec![],
            selected: None,
            details: HashMap::new(),
            detail: None,
            matched: vec![],
            fields: vec![true; FIELDS.len()],
            only_empty: false,
            lines: vec![],
            plans: vec![],
            error: String::new(),
            busy: true,
            saving: false,
            done: None,
            outcomes: vec![],
            cfg: cfg.clone(),
        };
        let (tx, list, ctx) = (w.tx.clone(), w.paths.clone(), ctx.clone());
        let _ = std::thread::Builder::new().name("mb-read".into()).spawn(move || {
            let songs = list
                .iter()
                .map(|p| mb::read_song(p, 0.0).unwrap_or(Song { path: p.clone(), tags: vec![], duration: 0.0 }))
                .collect();
            let _ = tx.send(Msg::Songs(songs));
            ctx.request_repaint();
        });
        w
    }

    fn spawn(&self, name: &str, job: impl FnOnce(&mut MusicBrainzClient) -> Option<Msg> + Send + 'static) {
        let (tx, client, ctx) = (self.tx.clone(), self.client.clone(), self.ctx.clone());
        let _ = std::thread::Builder::new().name(name.into()).spawn(move || {
            let msg = {
                let mut c = client.lock().unwrap_or_else(|e| e.into_inner());
                job(&mut c)
            };
            if let Some(m) = msg {
                let _ = tx.send(m);
                ctx.request_repaint();
            }
        });
    }

    fn err(e: MbError) -> Option<String> {
        match e {
            MbError::Stopped => None,
            MbError::Failed(m) => Some(m),
        }
    }

    /// Search with the query fields (Enter or Search).
    pub fn search(&mut self) {
        if self.saving || self.done.is_some() {
            return;
        }
        let title = tagedit::clean_value(&self.query_title);
        if title.is_empty() {
            self.error = if self.album { "Type the album to search for." } else { "Type the title to search for." }.into();
            return;
        }
        self.error.clear();
        self.status = Status::Searching;
        self.busy = true;
        let (songs, artist, album) = (self.songs.clone(), tagedit::clean_value(&self.query_artist), self.album);
        self.spawn("mb-search", move |c| match mb::search_candidates(c, &songs, &title, &artist, album) {
            Ok(found) => Some(Msg::Found(Ok(found))),
            Err(e) => Self::err(e).map(|m| Msg::Found(Err(m))),
        });
    }

    /// Choose the release at this row of the list.
    pub fn choose(&mut self, index: usize) {
        if index >= self.releases.len() || self.saving || self.done.is_some() {
            return;
        }
        self.selected = Some(index);
        let id = self.releases[index].id.clone();
        if let Some(d) = self.details.get(&id).cloned() {
            self.got_detail(&id, d);
            return;
        }
        self.status = Status::ReadingRelease;
        self.busy = true;
        self.spawn("mb-release", move |c| match c.release(&id) {
            Ok(d) => Some(Msg::Detail(id, Ok(d))),
            Err(e) => Self::err(e).map(|m| Msg::Detail(id, Err(m))),
        });
    }

    fn got_detail(&mut self, id: &str, d: Option<ReleaseDetail>) {
        self.details.insert(id.to_string(), d.clone());
        if self.selected.and_then(|i| self.releases.get(i)).map(|r| r.id.as_str()) != Some(id) {
            return; // another release was chosen meanwhile
        }
        self.busy = false;
        self.detail = d;
        match &self.detail {
            None => {
                self.status = Status::Unreadable;
                self.lines.clear();
                self.plans.clear();
            }
            Some(d) => {
                self.matched = mb::match_tracks(&self.songs, d, true);
                let found = self.matched.iter().filter(|m| m.is_some()).count();
                self.status = Status::Found(found, self.songs.len());
                self.update_preview();
            }
        }
    }

    pub fn chosen_fields(&self) -> Vec<&'static str> {
        FIELDS.iter().zip(&self.fields).filter(|(_, on)| **on).map(|(f, _)| *f).collect()
    }

    pub fn update_preview(&mut self) {
        let Some(d) = &self.detail else { return };
        let (lines, plans) = preview_lines(&self.songs, d, &self.matched, &self.chosen_fields(), self.only_empty);
        self.lines = lines;
        self.plans = plans;
    }

    /// The songs that would change.
    pub fn changes(&self) -> Vec<(String, Changes)> {
        self.plans.iter().filter(|(_, c)| !c.is_empty()).cloned().collect()
    }

    pub fn can_save(&self) -> bool {
        !self.busy && !self.saving && self.done.is_none() && !self.changes().is_empty()
    }

    /// Start saving; false when there is nothing to save.
    pub fn save(&mut self) -> bool {
        if self.busy || self.saving || self.done.is_some() {
            return true;
        }
        let plans = self.changes();
        if plans.is_empty() {
            self.error = "Nothing to save: the songs already hold this information.".into();
            return false;
        }
        for (_, ch) in &plans {
            if let Err(e) = tagedit::check_changes(ch) {
                self.error = e.0;
                return true;
            }
        }
        self.error.clear();
        self.saving = true;
        self.busy = true;
        self.status = Status::Saving(0, plans.len());
        let (tx, ctx, cfg) = (self.tx.clone(), self.ctx.clone(), self.cfg.clone());
        let release = self.detail.as_ref().map(|d| d.release.id.clone()).unwrap_or_default();
        let _ = std::thread::Builder::new().name("mb-save".into()).spawn(move || {
            let p = tx.clone();
            let c = ctx.clone();
            let out = mb::save_changes(&plans, Some(&cfg), move |i, n, _| {
                let _ = p.send(Msg::Progress(i, n));
                c.request_repaint();
            });
            if let Ok(log) = mb::LookupLog::open(None) {
                for o in out.iter().filter(|o| o.ok) {
                    log.mark(&o.path, mb::SAVED, &release);
                }
            }
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
                Msg::Songs(songs) => {
                    self.songs = songs;
                    if self.album {
                        let (mut album, artist) = mb::album_of(&self.songs);
                        if album.is_empty() {
                            if let Some(s) = self.songs.first() {
                                album = realhires::paths::file_name(&realhires::paths::parent(&s.path));
                            }
                        }
                        self.query_title = album;
                        self.query_artist = artist;
                    } else if let Some(s) = self.songs.first() {
                        let t = s.tag("title");
                        self.query_title = if t.is_empty() { realhires::paths::stem(&s.path) } else { t };
                        let a = s.tag("artist");
                        self.query_artist = if a.is_empty() { s.tag("album_artist") } else { a };
                    }
                    self.search();
                }
                Msg::Found(Ok(found)) => {
                    self.releases = found;
                    if self.releases.is_empty() {
                        self.busy = false;
                        self.status = Status::NothingFound;
                        self.lines.clear();
                        self.plans.clear();
                    } else {
                        self.choose(0);
                    }
                }
                Msg::Found(Err(e)) | Msg::Detail(_, Err(e)) => {
                    self.busy = false;
                    self.status = Status::Nothing;
                    self.error = e;
                }
                Msg::Detail(id, Ok(d)) => self.got_detail(&id, d),
                Msg::Progress(i, n) => {
                    if i < n {
                        self.status = Status::Saving(i, n);
                    }
                }
                Msg::Saved(out) => {
                    let (head, lines) = tagedit::save_summary(&out);
                    let clean = out.iter().all(|o| o.ok && o.message.is_empty());
                    self.outcomes = out;
                    self.saving = false;
                    self.busy = false;
                    self.status = Status::Nothing;
                    self.done = Some((head, lines));
                    finished = Some(clean);
                }
            }
        }
        finished
    }

    fn status_text(&self) -> String {
        match &self.status {
            Status::Nothing => String::new(),
            Status::Reading(1) => tr!("Reading the information of 1 song…").to_string(),
            Status::Reading(n) => tr!("Reading the information of {n} songs…", n = realhires::stats::thousands(*n as i64)),
            Status::Searching => tr!("Searching MusicBrainz…").to_string(),
            Status::ReadingRelease => tr!("Reading the release…").to_string(),
            Status::Found(f, n) => tr!("{found} of {n} songs found on this release.", found = f, n = n),
            Status::NothingFound => {
                tr!("Nothing found on MusicBrainz. Check the spelling, or search with fewer words.").to_string()
            }
            Status::Unreadable => tr!("This release could not be read.").to_string(),
            Status::Saving(i, n) => tr!("Saving {i} of {n}…", i = (i + 1).min(*n), n = n),
        }
    }

    pub fn show(&mut self, ctx: &egui::Context, pal: &Palette) -> Outcome {
        let mut result = match self.poll() {
            Some(close) => Outcome::Saved { close },
            None => Outcome::Open,
        };
        let mut close = false;
        egui::Window::new(tr!("MusicBrainz"))
            .id(egui::Id::new("player-musicbrainz"))
            .collapsible(false)
            .resizable(true)
            .default_width(680.0)
            .pivot(Align2::CENTER_CENTER)
            .default_pos(ctx.content_rect().center())
            .show(ctx, |ui| {
                let heading = if self.heading.is_empty() {
                    tr!("{n} songs", n = realhires::stats::thousands(self.paths.len() as i64))
                } else {
                    i18n::tr_name(&self.heading).to_string()
                };
                ui.label(RichText::new(heading).font(crate::look::title(18.0)).color(pal.text));
                if !self.subheading.is_empty() {
                    ui.label(RichText::new(&self.subheading).color(pal.secondary));
                }
                ui.add_space(8.0);
                let editable = !self.busy && !self.saving && self.done.is_none();
                ui.horizontal(|ui| {
                    ui.label(RichText::new(if self.album { tr!("Album") } else { tr!("Title") }).color(pal.secondary));
                    let r1 = ui.add_enabled(editable, egui::TextEdit::singleline(&mut self.query_title).desired_width(240.0));
                    ui.label(RichText::new(tr!("Artist")).color(pal.secondary));
                    let r2 = ui.add_enabled(editable, egui::TextEdit::singleline(&mut self.query_artist).desired_width(180.0));
                    let enter = (r1.lost_focus() || r2.lost_focus()) && ui.input(|i| i.key_pressed(egui::Key::Enter));
                    if ui.add_enabled(editable, egui::Button::new(tr!("Search"))).clicked() || (editable && enter) {
                        self.search();
                    }
                });
                let status = self.status_text();
                if !status.is_empty() {
                    ui.label(RichText::new(status).small().color(pal.secondary));
                }
                ui.add_space(6.0);
                ui.label(RichText::new(tr!("Releases found")).color(pal.secondary));
                let mut pick = None;
                egui::ScrollArea::vertical().id_salt("mb-releases").max_height(130.0).show(ui, |ui| {
                    for (i, r) in self.releases.iter().enumerate() {
                        let sel = self.selected == Some(i);
                        if ui.selectable_label(sel, release_text(r)).clicked() && !sel {
                            pick = Some(i);
                        }
                    }
                });
                if let Some(i) = pick {
                    if editable {
                        self.choose(i);
                    }
                }
                ui.add_space(6.0);
                ui.label(RichText::new(tr!("Changes")).color(pal.secondary));
                egui::ScrollArea::vertical().id_salt("mb-changes").max_height(240.0).show(ui, |ui| {
                    for (text, grey) in &self.lines {
                        let t = RichText::new(text);
                        ui.label(if *grey { t.color(pal.secondary) } else { t.color(pal.text) });
                    }
                });
                ui.add_space(6.0);
                let mut changed = false;
                ui.horizontal_wrapped(|ui| {
                    for (i, f) in FIELDS.iter().enumerate() {
                        changed |= ui.add_enabled(editable, egui::Checkbox::new(&mut self.fields[i], i18n::tr(tagedit::label(f)))).changed();
                    }
                });
                changed |= ui.add_enabled(editable, egui::Checkbox::new(&mut self.only_empty, tr!("Only fill in empty fields"))).changed();
                if changed {
                    self.update_preview();
                }
                if !self.error.is_empty() {
                    ui.add_space(4.0);
                    ui.label(RichText::new(tr_text(&self.error)).color(egui::Color32::from_rgb(0xe0, 0x30, 0x30)));
                }
                if let Some((head, lines)) = &self.done {
                    ui.add_space(6.0);
                    ui.label(RichText::new(tr_text(head)).strong());
                    for l in lines {
                        ui.label(tr_text(l));
                    }
                }
                ui.add_space(8.0);
                ui.horizontal(|ui| {
                    if self.done.is_some() {
                        if ui.button(tr!("Close")).clicked() || ui.input(|i| i.key_pressed(egui::Key::Escape)) {
                            close = true;
                        }
                        return;
                    }
                    if ui.add_enabled(self.can_save(), egui::Button::new(RichText::new(tr!("Save")).strong())).clicked() {
                        self.save();
                    }
                    if ui.add_enabled(!self.saving, egui::Button::new(tr!("Cancel"))).clicked()
                        || (!self.saving && ui.input(|i| i.key_pressed(egui::Key::Escape)))
                    {
                        close = true;
                    }
                });
                ui.add_space(6.0);
                ui.label(RichText::new(tr_text(mb::CREDIT)).small().color(pal.secondary));
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

impl MbWindow {
    /// Tests: answer from this client instead of musicbrainz.org.
    #[cfg(test)]
    pub fn set_client(&mut self, client: MusicBrainzClient) {
        self.client = Arc::new(Mutex::new(client));
    }
}

/// Tests: a client answering from Python's recorded catalogue answers
/// (`rust/crates/core/tests/python_musicbrainz.json`).
#[cfg(test)]
pub(crate) fn test_client() -> MusicBrainzClient {
    use realhires::json::{self, Value};
    let data = json::parse(include_str!("../../core/tests/python_musicbrainz.json")).unwrap();
    let answers: Vec<(String, u16, String)> = data
        .get("responses")
        .and_then(Value::as_object)
        .unwrap()
        .iter()
        .map(|(u, v)| {
            let a = v.as_array().unwrap();
            let st = match &a[0] {
                Value::Int(n) => *n as u16,
                _ => 0,
            };
            (u.clone(), st, a[1].as_str().unwrap().to_string())
        })
        .collect();
    MusicBrainzClient::new(mb::DEFAULT_SERVER).with_min_interval(0.0).with_transport(Box::new(
        move |url: &str, _h: &[(&str, &str)], _t| match answers.iter().find(|(u, _, _)| u == url) {
            Some((_, st, body)) => Ok((*st, vec![], body.clone().into_bytes())),
            None => Ok((404, vec![], b"{}".to_vec())),
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use egui::{pos2, vec2, Rect};
    use std::time::{Duration, Instant};

    fn frame(ctx: &egui::Context, w: &mut MbWindow) -> String {
        let input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1280.0, 840.0))),
            ..Default::default()
        };
        let pal = crate::look::palette(false);
        let out = ctx.run_ui(input, |ui| {
            let _ = w.show(ui.ctx(), &pal);
        });
        let mut text = String::new();
        fn walk(shape: &egui::Shape, out: &mut String) {
            match shape {
                egui::Shape::Text(t) => {
                    out.push_str(t.galley.text());
                    out.push('\n');
                }
                egui::Shape::Vec(v) => v.iter().for_each(|s| walk(s, out)),
                _ => {}
            }
        }
        for c in &out.shapes {
            walk(&c.shape, &mut text);
        }
        out.drop_without_applying_deltas();
        text
    }

    #[test]
    fn musicbrainz_window_finds_previews_and_saves() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-mbwin-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let album = tmp.join("Night Drive");
        std::fs::create_dir_all(&album).unwrap();
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var(realhires::musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        let ffmpeg = realhires::probe::tool("ffmpeg");
        let mut paths = vec![];
        for (i, (title, secs)) in [("Neon Streets", 201), ("Midnight Signal", 245), ("Harbour Lights", 187), ("Last Train Home", 263)]
            .iter()
            .enumerate()
        {
            let p = album.join(format!("0{}.flac", i + 1)).to_string_lossy().into_owned();
            let ok = std::process::Command::new(&ffmpeg)
                .args(["-v", "error", "-y", "-f", "lavfi", "-i", &format!("anullsrc=r=44100:cl=stereo"), "-t", &secs.to_string()])
                .args(["-metadata", &format!("title={title}"), "-metadata", "artist=The Example Band"])
                .args(["-metadata", "album=Night Drive", &p])
                .status()
                .unwrap()
                .success();
            assert!(ok);
            paths.push(p);
        }
        let ctx = egui::Context::default();
        let cfg = Settings::default();
        let mut w = MbWindow::new(&ctx, &cfg, paths.clone(), true, "Night Drive".into(), String::new());
        w.set_client(test_client());
        let t0 = Instant::now();
        while w.detail.is_none() || w.busy {
            assert!(t0.elapsed() < Duration::from_secs(60), "timed out: {:?} {}", w.status, w.error);
            frame(&ctx, &mut w);
            std::thread::sleep(Duration::from_millis(20));
        }
        assert_eq!((w.query_title.as_str(), w.query_artist.as_str()), ("Night Drive", "The Example Band"));
        assert_eq!(w.detail.as_ref().unwrap().release.id, "rel-night-2001");
        let text = frame(&ctx, &mut w);
        assert!(text.contains("Night Drive — The Example Band  ·  2001-03-07 · JP · CD · 4 tracks"), "{text}");
        assert!(text.contains("01.flac → 1. Neon Streets"), "{text}");
        assert!(text.contains("Album artist: (empty) → The Example Band"), "{text}");
        assert!(text.contains("4 of 4 songs found on this release."), "{text}");
        // the fields to write and "only empty" redraw the changes
        let genre = FIELDS.iter().position(|f| *f == "genre").unwrap();
        w.fields[genre] = false;
        w.update_preview();
        assert!(!w.lines.iter().any(|(l, _)| l.contains("Genre:")));
        // Japanese, on this thread only
        i18n::set_thread_language("日本語");
        let ja = frame(&ctx, &mut w);
        i18n::set_thread_language("");
        assert!(ja.contains("見つかったリリース") && ja.contains("変更内容") && ja.contains("4トラック"), "{ja}");
        assert!(w.save());
        let t0 = Instant::now();
        while w.done.is_none() {
            assert!(t0.elapsed() < Duration::from_secs(60), "saving timed out");
            frame(&ctx, &mut w);
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(w.outcomes.iter().all(|o| o.ok), "{:?}", w.outcomes);
        let tags = realhires::probe::probe(&paths[1]).unwrap().tags;
        let tag = |k: &str| realhires::probe::tag(&tags, k).unwrap_or("").to_string();
        assert_eq!(tag("album_artist"), "The Example Band");
        assert_eq!(tag("date"), "1999-05-28");
        assert_eq!(tag("composer"), "Kai Example, Mio Example");
        assert_eq!(tag("genre"), "");
        assert_eq!(mb::LookupLog::open(None).unwrap().status(&paths[1]), mb::SAVED);
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
