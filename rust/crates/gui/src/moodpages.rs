//! Moods, tempo and playlists in the egui player (2.4, `docs/player-spec.md` "4. Moods,
//! tempo and playlists"), as the Tk player's `player_moods.py` does them: the moods of
//! `library.sqlite` read at start, the background analysis after every library update
//! (one song at a time, `moods::run_analysis()`, when `analyse_moods` is on) with its
//! progress in the sidebar's status (when nothing else is shown there), the Moods page
//! (a card per mood and per tempo with its number of songs), a card's page (its songs,
//! Play, Shuffle, Create Playlist), the BPM and Mood columns and Now Playing's Details
//! rows.  New results are shown at most every `REDRAW` while the job runs; the song
//! lists' cells find them through a thread-local copy (`shown_mood()`), since
//! `cell_text()` has no player.

use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::sync::Arc;
use std::time::{Duration, Instant};

use egui::{pos2, vec2, Align2, Color32, CornerRadius, Rect, RichText, Sense, Ui};
use realhires::moods::{self, Mood, MoodStore};
use realhires::{i18n, n_, tr};

use crate::icons::{self, Icon};
use crate::library_pages::LibraryEntry;
use crate::look::{self, Palette};
use crate::player::{plural, Page, Player};

/// The pages show new results at most this often while the job runs.
pub const REDRAW: Duration = Duration::from_secs(2);
const CARD_W: f32 = 190.0;
const CARD_H: f32 = 104.0;
const GAP: f32 = 14.0;
/// The two parts of the Moods page: its heading and the cards' kind.
pub const SECTIONS: [(&str, &str); 2] = [(n_!("Moods"), "mood"), (n_!("Tempo"), "tempo")];

/// The colour of each card (white text stays readable on all of them; as the Tk app's).
pub fn card_color(label: &str) -> Color32 {
    look::parse_hex(match label {
        "Happy" => "#e8650a",
        "Sad" => "#1a6fd8",
        "Chill" => "#0f8f8c",
        "Intense" => "#e0245e",
        "Up-tempo" => "#8e44ad",
        "Mid-tempo" => "#5856d6",
        _ => "#3f51b5",
    })
}

/// The icon of a card: a smiling or a frowning face, or a metronome for the tempos.
pub fn card_icon(kind: &str, label: &str) -> Icon {
    match (kind, label) {
        ("tempo", _) => Icon::Tempo,
        (_, "Sad" | "Intense") => Icon::MoodSad,
        _ => Icon::Mood,
    }
}

thread_local! {
    static SHOWN: RefCell<Arc<HashMap<String, Mood>>> = RefCell::new(Arc::default());
}

/// The analysis of a song as the window shows it now (for the song lists' cells).
pub fn shown_mood<R>(path: &str, f: impl FnOnce(Option<&Mood>) -> R) -> R {
    SHOWN.with(|s| f(s.borrow().get(path)))
}

fn publish(map: Arc<HashMap<String, Mood>>) {
    SHOWN.with(|s| *s.borrow_mut() = map);
}

/// The BPM and Mood columns of the song lists.
pub fn cell(path: &str, mood_col: bool) -> String {
    shown_mood(path, |m| {
        if mood_col {
            m.filter(|m| !m.mood.is_empty()).map(|m| i18n::tr(&m.mood).to_string()).unwrap_or_default()
        } else {
            moods::bpm_text(m)
        }
    })
}

/// What a click on the BPM heading sorts by (songs not analysed yet first).
pub fn bpm_sort(path: &str) -> String {
    format!("{:06}", shown_mood(path, moods::sort_bpm) + 1)
}

/// The tooltip of the BPM and Mood columns: everything the analysis found, as an estimate.
pub fn mood_tip(path: &str) -> String {
    shown_mood(path, |m| match m {
        None => tr!("Not analysed yet").to_string(),
        Some(m) if m.mood.is_empty() => tr!("No mood: the song is silent or too short.").to_string(),
        Some(m) => {
            let mut parts = vec![i18n::tr(&m.mood).to_string(), i18n::tr(&m.energy_label).to_string()];
            if m.bpm > 0 {
                parts.push(tr!("{bpm} BPM, {tempo}", bpm = m.bpm, tempo = i18n::tr(&m.tempo)));
            }
            if !m.key.is_empty() {
                parts.push(i18n::tr(&moods::key_name(&m.key, &m.mode)).to_string());
            }
            tr!("Estimated from the audio: {what}", what = parts.join(" · "))
        }
    })
}

/// Now Playing's Details rows for a song's mood, tempo and key (labels English, shown
/// with `i18n::tr()`, as `pages::song_details()` gives them).
pub fn detail_rows(m: Option<&Mood>) -> Vec<(&'static str, String)> {
    let dash = || "—".to_string();
    match m {
        None => vec![(n_!("Mood (estimated)"), tr!("Not analysed yet").into())],
        Some(m) if m.mood.is_empty() => vec![(n_!("Mood (estimated)"), tr!("No mood: the song is silent or too short.").into())],
        Some(m) => vec![
            (n_!("Mood (estimated)"), tr!("{a}, {b}", a = i18n::tr(&m.mood), b = i18n::tr(&m.energy_label))),
            (
                n_!("Tempo (estimated)"),
                if m.bpm > 0 { tr!("{bpm} BPM, {tempo}", bpm = m.bpm, tempo = i18n::tr(&m.tempo)) } else { dash() },
            ),
            (n_!("Key (estimated)"), if m.key.is_empty() { dash() } else { i18n::tr(&moods::key_name(&m.key, &m.mode)).to_string() }),
        ],
    }
}

pub enum MoodMsg {
    Progress(usize, usize, String, Option<Mood>),
    Done(Option<(usize, usize)>),
}

/// The player's moods: the results, the background job and what the pages drew last.
pub struct MoodState {
    pub map: Arc<HashMap<String, Mood>>,
    rx: Option<Receiver<MoodMsg>>,
    stop: Arc<AtomicBool>,
    pub progress: Option<(usize, usize)>,
    /// (analysed, failed) of the last run
    pub last: Option<(usize, usize)>,
    again: bool,
    pending: bool,
    shown_at: Instant,
    /// changes with the results
    pub gen: u64,
    /// what Create Playlist made last
    pub message: String,
    /// where the Moods page drew each card, and the card page's Create Playlist (for the tests)
    pub card_rects: Vec<(String, Rect)>,
    pub create_rect: Option<Rect>,
    counts: Option<((u64, u64), Vec<(&'static str, usize)>)>,
}

impl Default for MoodState {
    fn default() -> Self {
        MoodState {
            map: Arc::default(),
            rx: None,
            stop: Arc::new(AtomicBool::new(false)),
            progress: None,
            last: None,
            again: false,
            pending: false,
            shown_at: Instant::now(),
            gen: 0,
            message: String::new(),
            card_rects: vec![],
            create_rect: None,
            counts: None,
        }
    }
}

impl Player {
    // ------------------------------------------------------------- the job ---

    /// The moods analysed before (library.sqlite), read when the player starts.
    pub fn load_moods(&mut self) {
        if let Ok(store) = MoodStore::open(None) {
            self.moods.map = Arc::new(store.all());
        }
        publish(self.moods.map.clone());
    }

    pub fn moods_busy(&self) -> bool {
        self.moods.rx.is_some()
    }

    /// Analyse the songs that need it in a thread (after each library update when
    /// `analyse_moods` is on, or by hand with `force`).  False when it did not start.
    pub fn analyse_moods(&mut self, force: bool) -> bool {
        if !force && !self.scanner.cfg.analyse_moods {
            return false;
        }
        if self.moods_busy() {
            self.moods.again = true;
            return false;
        }
        if self.lib.tracks.is_empty() || self.lib_progress.is_some() {
            return false;
        }
        let tracks = self.lib.tracks.clone();
        let (tx, rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        self.moods.stop = stop.clone();
        let ctx = self.ctx.clone();
        let spawned = std::thread::Builder::new().name("moods".into()).spawn(move || {
            let res = MoodStore::open(None).ok().map(|store| {
                let p = tx.clone();
                let c = ctx.clone();
                moods::run_analysis(
                    &tracks,
                    &store,
                    Some(&stop),
                    &mut |i, n, path, m| {
                        let _ = p.send(MoodMsg::Progress(i, n, path.to_string(), m.cloned()));
                        c.request_repaint();
                    },
                    moods::PAUSE,
                    &moods::analyse_file,
                )
            });
            let _ = tx.send(MoodMsg::Done(res));
            ctx.request_repaint();
        });
        if spawned.is_err() {
            return false;
        }
        self.moods.again = false;
        self.moods.rx = Some(rx);
        self.moods.progress = Some((0, 0));
        true
    }

    pub fn stop_moods(&mut self) {
        self.moods.stop.store(true, Ordering::Relaxed);
    }

    /// `analyse_moods` was turned on or off in Settings.
    pub fn moods_setting_changed(&mut self, on: bool) {
        if on {
            self.analyse_moods(false);
        } else {
            self.stop_moods();
        }
    }

    /// Every frame: the job's results and progress.
    pub fn poll_moods(&mut self) {
        let mut done = None;
        if let Some(rx) = &self.moods.rx {
            let mut fresh: Vec<(String, Mood)> = vec![];
            while let Ok(m) = rx.try_recv() {
                match m {
                    MoodMsg::Progress(i, n, path, mood) => {
                        self.moods.progress = Some((i, n));
                        if let Some(mood) = mood {
                            fresh.push((path, mood));
                        }
                    }
                    MoodMsg::Done(res) => done = Some(res),
                }
            }
            if !fresh.is_empty() {
                let map = Arc::make_mut(&mut self.moods.map);
                for (p, m) in fresh {
                    map.insert(p, m);
                }
                self.moods.pending = true;
            }
        }
        if self.moods.pending && (done.is_some() || self.moods.shown_at.elapsed() >= REDRAW) {
            self.moods.pending = false;
            self.moods.shown_at = Instant::now();
            self.moods_changed();
        } else if self.moods.pending {
            self.ctx.request_repaint_after(REDRAW);
        }
        if let Some(res) = done {
            self.moods.rx = None;
            self.moods.progress = None;
            self.moods.last = res;
            if std::mem::take(&mut self.moods.again) {
                self.analyse_moods(false);
            }
        }
    }

    /// New results: the pages, the song lists, the tabs and Details show them.
    fn moods_changed(&mut self) {
        self.moods.gen += 1;
        publish(self.moods.map.clone());
        self.forget_lists();
    }

    /// The sidebar's status while the job runs (the least important of its texts).
    pub fn mood_status_line(&self) -> Option<String> {
        match self.moods.progress {
            Some((i, n)) if i < n => Some(tr!(
                "Analysing moods: {done} of {total} songs",
                done = realhires::stats::thousands(i as i64),
                total = realhires::stats::thousands(n as i64)
            )),
            _ => None,
        }
    }

    // -------------------------------------------------------- what is shown ---

    /// The number of songs of every card, worked out once per library and results.
    pub fn mood_counts(&mut self) -> Vec<(&'static str, usize)> {
        let key = (self.lib.gen, self.moods.gen);
        if let Some((k, v)) = &self.moods.counts {
            if *k == key {
                return v.clone();
            }
        }
        let v = moods::card_counts(&self.lib.tracks, &self.moods.map);
        self.moods.counts = Some((key, v.clone()));
        v
    }

    /// A card's songs in the sort chosen for song lists (worked out once per library,
    /// results and sort, never per frame).
    pub fn mood_list(&mut self, kind: &str, label: &str) -> Arc<Vec<String>> {
        let id = format!("mood:{}:{kind}:{label}", self.moods.gen);
        let sort = Some(self.song_sort);
        let (kind, label) = (kind.to_string(), label.to_string());
        self.cached_list(&id, sort, move |p| {
            let paths = moods::card_songs(&kind, &label, &p.lib.tracks, &p.moods.map);
            let songs: Vec<&realhires::musiclib::Track> = paths.iter().filter_map(|x| p.lib.track(x)).collect();
            p.sorted_songs(songs).iter().map(|t| t.path.clone()).collect()
        })
    }

    /// The Library page's Moods entry.
    pub fn moods_library_entry(&self) -> LibraryEntry {
        let n = moods::analysed_count(&self.lib.tracks, &self.moods.map);
        let count = match n {
            0 => tr!("Not analysed yet").to_string(),
            1 => tr!("1 song analysed").to_string(),
            n => tr!("{n} songs analysed", n = realhires::stats::thousands(n as i64)),
        };
        LibraryEntry { icon: Icon::Mood, name: tr!("Moods").to_string(), count, page: Page::Moods }
    }

    /// The Moods page's line under its title: how far the analysis is.
    pub fn moods_status_text(&self) -> String {
        let n = moods::analysed_count(&self.lib.tracks, &self.moods.map);
        let text = tr!(
            "{n} of {total} songs analysed",
            n = realhires::stats::thousands(n as i64),
            total = realhires::stats::thousands(self.lib.tracks.len() as i64)
        );
        if !self.scanner.cfg.analyse_moods {
            tr!("{text}. Analysing is turned off in Settings → Library.", text = text)
        } else if self.moods_busy() {
            tr!("{text}, analysing…", text = text)
        } else {
            text
        }
    }

    /// Create Playlist: an ordinary playlist of a card's songs ("Sad songs"; a taken name
    /// gets " 2"); says what was made.  Returns its id (None without songs).
    pub fn create_mood_playlist(&mut self, kind: &str, label: &str) -> Option<String> {
        let paths = moods::card_songs(kind, label, &self.lib.tracks, &self.moods.map);
        if paths.is_empty() {
            return None;
        }
        let id = self.new_playlist(i18n::tr(&moods::playlist_name(label)), &paths);
        let p = self.state.playlist(&id)?;
        let n = p.songs.len();
        self.moods.message = if n == 1 {
            tr!("Made the playlist “{name}” with 1 song.", name = p.name)
        } else {
            tr!("Made the playlist “{name}” with {n} songs.", name = p.name, n = realhires::stats::thousands(n as i64))
        };
        Some(id)
    }

    // ------------------------------------------------------------- the pages ---

    /// The Moods page: a card per mood and per tempo, each with its colour, number of
    /// songs and meaning; a click opens its songs.  It says the labels are estimates.
    pub fn moods_page(&mut self, ui: &mut Ui, pal: &Palette) {
        let counts = self.mood_counts();
        let status = self.moods_status_text();
        self.moods.card_rects.clear();
        let mut open = None;
        egui::ScrollArea::vertical().id_salt("moods-page").auto_shrink([false, false]).show(ui, |ui| {
            ui.label(RichText::new(tr!("Moods")).font(look::title(28.0)).color(pal.text));
            ui.add_space(4.0);
            ui.label(RichText::new(status).color(pal.secondary));
            ui.add_space(2.0);
            ui.label(RichText::new(i18n::tr(moods::ESTIMATE_NOTE)).small().color(pal.secondary));
            for (title, kind) in SECTIONS {
                ui.add_space(14.0);
                ui.label(RichText::new(i18n::tr(title)).font(look::title(18.0)).color(pal.text));
                ui.add_space(8.0);
                let cards: Vec<&str> = moods::CARDS.iter().filter(|(k, _)| *k == kind).map(|(_, l)| *l).collect();
                let w = ui.available_width();
                let cols = (((w + GAP) / (CARD_W + GAP)).floor() as usize).max(1);
                let cw = (w - (cols - 1) as f32 * GAP) / cols as f32;
                for row in cards.chunks(cols) {
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = GAP;
                        for label in row {
                            let n = counts.iter().find(|(l, _)| l == label).map_or(0, |(_, n)| *n);
                            let (rect, resp) = ui.allocate_exact_size(vec2(cw, CARD_H), Sense::click());
                            let color = card_color(label);
                            let fill = if resp.hovered() { color.gamma_multiply(0.85) } else { color };
                            let p = ui.painter();
                            p.rect_filled(rect, CornerRadius::same(14), fill);
                            icons::paint(p, Rect::from_center_size(pos2(rect.right() - 26.0, rect.top() + 26.0), vec2(22.0, 22.0)), card_icon(kind, label), Color32::WHITE);
                            let name = crate::pages::elided(ui, i18n::tr(label), look::title(18.0), Color32::WHITE, cw - 56.0);
                            ui.painter().galley(pos2(rect.left() + 16.0, rect.top() + 26.0 - name.size().y / 2.0), name, Color32::WHITE);
                            ui.painter().text(pos2(rect.left() + 16.0, rect.top() + 54.0), Align2::LEFT_CENTER, plural(n, "song"), look::title(13.0), Color32::WHITE);
                            let note = crate::pages::elided(ui, i18n::tr(moods::card_note(label)), look::text(12.0), Color32::WHITE, cw - 28.0);
                            ui.painter().galley(pos2(rect.left() + 16.0, rect.top() + 78.0 - note.size().y / 2.0), note, Color32::WHITE);
                            let resp = resp.on_hover_cursor(egui::CursorIcon::PointingHand);
                            if resp.clicked() {
                                open = Some((kind, label.to_string()));
                            }
                            self.moods.card_rects.push((label.to_string(), rect));
                        }
                    });
                    ui.add_space(GAP - ui.spacing().item_spacing.y);
                }
            }
            ui.add_space(20.0);
        });
        if let Some((kind, label)) = open {
            self.go(Page::Mood(kind, label));
        }
    }

    /// A card's page: its name, "N songs, M minutes", Play, Shuffle, Create Playlist, a
    /// line saying the labels are estimates, and the songs.
    pub fn mood_page(&mut self, ui: &mut Ui, pal: &Palette, kind: &'static str, label: &str) {
        let list = self.mood_list(kind, label);
        let shown = i18n::tr(label).to_string();
        ui.label(RichText::new(&shown).font(look::title(28.0)).color(pal.text));
        let secs: f64 = list.iter().filter_map(|p| self.lib.track(p)).map(|t| t.duration.max(0.0)).sum();
        let minutes = realhires::pyfmt::round_int(secs / 60.0).max(0) as usize;
        ui.label(RichText::new(tr!("{a}, {b}", a = plural(list.len(), "song"), b = plural(minutes, "minute"))).color(pal.secondary));
        ui.add_space(10.0);
        let mut create = false;
        ui.horizontal_wrapped(|ui| {
            if !list.is_empty() {
                if pill(ui, pal, Icon::Play, tr!("Play"), true).clicked() {
                    self.play_list(list.to_vec(), Some(&list[0]), &shown);
                }
                if pill(ui, pal, Icon::Shuffle, tr!("Shuffle"), true).clicked() {
                    self.shuffle_list(list.to_vec(), &shown);
                }
                let r = pill(ui, pal, Icon::Playlist, tr!("Create Playlist"), false);
                self.moods.create_rect = Some(r.rect);
                if r.clicked() {
                    create = true;
                }
            } else {
                self.moods.create_rect = None;
            }
        });
        if create {
            self.create_mood_playlist(kind, label);
        }
        ui.add_space(6.0);
        ui.label(RichText::new(i18n::tr(moods::ESTIMATE_NOTE)).small().color(pal.secondary));
        if !self.moods.message.is_empty() {
            ui.label(RichText::new(&self.moods.message).small().color(pal.accent));
        }
        ui.add_space(8.0);
        if list.is_empty() {
            ui.label(RichText::new(tr!("No song has this label yet. Songs get their labels while they are analysed.")).color(pal.secondary));
            return;
        }
        self.song_table(ui, pal, &list, &shown, true);
    }
}

/// A rounded button with an icon: filled with the accent colour (Play, Shuffle) or grey.
fn pill(ui: &mut Ui, pal: &Palette, icon: Icon, label: &str, accent: bool) -> egui::Response {
    let font = look::title(13.0);
    let w = ui.ctx().fonts_mut(|f| f.layout_no_wrap(label.to_string(), font.clone(), Color32::WHITE).size().x) + 46.0;
    let (rect, resp) = ui.allocate_exact_size(vec2(w.max(110.0), 32.0), Sense::click());
    let (fill, fg) = if accent { (pal.accent, pal.on_accent) } else { (pal.lcd, pal.text) };
    let fill = if resp.hovered() { fill.gamma_multiply(0.85) } else { fill };
    ui.painter().rect_filled(rect, CornerRadius::same(8), fill);
    icons::paint(ui.painter(), Rect::from_center_size(pos2(rect.left() + 22.0, rect.center().y), vec2(18.0, 18.0)), icon, fg);
    ui.painter().text(pos2(rect.left() + 36.0, rect.center().y), Align2::LEFT_CENTER, label, font, fg);
    resp
}

#[cfg(test)]
mod tests {
    //! The moods in the egui player, as `tests/test_moods_gui.py` checks them in the Tk
    //! one, with the same invented songs (`tests/mood_songs.py`'s integer synthesiser):
    //! the background analysis after the library update and its progress in the sidebar,
    //! the Moods entries, the Moods page's cards at 1024 px, a card's page with Create
    //! Playlist (a taken name gets " 2"), the BPM and Mood columns, a tab on moods,
    //! Details, a changed song analysed again, the setting off, and Japanese.
    use super::*;
    use crate::player::SongCol;
    use realhires::{musiclib, playerstate};

    const WIDTH: f32 = 1024.0;
    const HEIGHT: f32 = 740.0;

    // ---- the invented songs (tests/mood_songs.py)
    const NOTE_MHZ: [i64; 12] = [261626, 277183, 293665, 311127, 329628, 349228, 369994, 391995, 415305, 440000, 466164, 493883];

    fn period(note: usize, octave: i64, sr: i64) -> i64 {
        let f = NOTE_MHZ[note];
        let (num, den) = if octave >= 4 { (sr * 1000, f << (octave - 4)) } else { ((sr * 1000) << (4 - octave), f) };
        (2 * num + den).div_euclid(2 * den)
    }

    fn song(bpm: i64, major: bool, seconds: i64, sr: i64, gain: i64) -> Vec<i16> {
        // C - F - G - C, or Am - Dm - Em - Am: (note, octave) per bar, the bass first
        let chords: [[(usize, i64); 4]; 4] = if major {
            [[(0, 2), (0, 4), (4, 4), (7, 4)], [(5, 2), (5, 4), (9, 4), (0, 5)], [(7, 2), (7, 4), (11, 4), (2, 5)], [(0, 2), (0, 4), (4, 4), (7, 4)]]
        } else {
            [[(9, 2), (9, 3), (0, 4), (4, 4)], [(2, 2), (2, 4), (5, 4), (9, 4)], [(4, 2), (4, 4), (7, 4), (11, 4)], [(9, 2), (9, 3), (0, 4), (4, 4)]]
        };
        let (tone, click) = (6000i64, 9000i64);
        let total = seconds * sr;
        let mut out = vec![0i64; total as usize];
        let mut bar = 0;
        while bar * 4 * 60 * sr / bpm < total {
            let (start, end) = (bar * 4 * 60 * sr / bpm, total.min((bar + 1) * 4 * 60 * sr / bpm));
            for (i, (note, octave)) in chords[(bar % 4) as usize].iter().enumerate() {
                let amp = if i == 0 { tone * 3 / 2 } else { tone };
                let p = period(*note, *octave, sr);
                for n in start..end {
                    out[n as usize] += (amp * 4 * (2 * n.rem_euclid(p) - p).abs()).div_euclid(2 * p) - amp;
                }
            }
            bar += 1;
        }
        let (length, mut x, mut beat) = (sr / 100, 12345i64, 0i64);
        while beat * 60 * sr / bpm < total {
            let at = beat * 60 * sr / bpm;
            for k in 0..length.min(total - at) {
                x = (1103515245 * x + 12345).rem_euclid(1 << 31);
                out[(at + k) as usize] += (((x >> 15) - 32768) * (click * (length - k) / length)).div_euclid(32768);
            }
            beat += 1;
        }
        out.iter().map(|v| (v * gain).div_euclid(100).clamp(-32768, 32767) as i16).collect()
    }

    fn flac(dir: &std::path::Path, n: usize, title: &str, bpm: i64, major: bool, gain: i64) -> String {
        let wav = dir.join(format!("{title}.wav"));
        let samples = song(bpm, major, 6, 44100, gain);
        let mut data = Vec::new();
        for v in &samples {
            data.extend_from_slice(&v.to_le_bytes());
            data.extend_from_slice(&v.to_le_bytes());
        }
        let mut f = Vec::new();
        f.extend_from_slice(b"RIFF");
        f.extend_from_slice(&(36 + data.len() as u32).to_le_bytes());
        f.extend_from_slice(b"WAVEfmt ");
        for v in [16u32.to_le_bytes().to_vec(), 1u16.to_le_bytes().to_vec(), 2u16.to_le_bytes().to_vec(), 44100u32.to_le_bytes().to_vec(), (44100u32 * 4).to_le_bytes().to_vec(), 4u16.to_le_bytes().to_vec(), 16u16.to_le_bytes().to_vec()] {
            f.extend_from_slice(&v);
        }
        f.extend_from_slice(b"data");
        f.extend_from_slice(&(data.len() as u32).to_le_bytes());
        f.extend_from_slice(&data);
        std::fs::write(&wav, f).unwrap();
        let out = dir.join(format!("{n:02} {title}.flac"));
        let meta = [format!("title={title}"), "artist=Invented Band".into(), "album=Feelings".into(), format!("track={n}")];
        let mut args: Vec<String> = ["-v", "error", "-y", "-i"].iter().map(|s| s.to_string()).collect();
        args.push(wav.to_string_lossy().into_owned());
        for m in meta {
            args.extend(["-metadata".to_string(), m]);
        }
        args.push(out.to_string_lossy().into_owned());
        let st = std::process::Command::new(realhires::probe::tool("ffmpeg")).args(&args).status().unwrap();
        assert!(st.success());
        let _ = std::fs::remove_file(&wav);
        realhires::paths::abspath(&out.to_string_lossy())
    }

    // ---- driving the window
    fn frame_with(ctx: &egui::Context, p: &mut Player, events: Vec<egui::Event>) -> Vec<String> {
        fn walk(shape: &egui::Shape, out: &mut Vec<String>) {
            match shape {
                egui::Shape::Text(t) => out.push(t.galley.text().to_string()),
                egui::Shape::Vec(v) => v.iter().for_each(|s| walk(s, out)),
                _ => {}
            }
        }
        let input = egui::RawInput { screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(WIDTH, HEIGHT))), events, ..Default::default() };
        let output = ctx.run_ui(input, |ui| p.ui(ui));
        let mut out = vec![];
        output.shapes.iter().for_each(|s| walk(&s.shape, &mut out));
        output.drop_without_applying_deltas();
        out
    }

    fn texts(ctx: &egui::Context, p: &mut Player) -> Vec<String> {
        for _ in 0..3 {
            frame_with(ctx, p, vec![]);
        }
        frame_with(ctx, p, vec![])
    }

    fn click(ctx: &egui::Context, p: &mut Player, at: egui::Pos2) {
        frame_with(ctx, p, vec![egui::Event::PointerMoved(at)]);
        for pressed in [true, false] {
            let e = egui::Event::PointerButton { pos: at, button: egui::PointerButton::Primary, pressed, modifiers: Default::default() };
            frame_with(ctx, p, vec![e]);
        }
        frame_with(ctx, p, vec![egui::Event::PointerMoved(pos2(5.0, HEIGHT - 5.0))]);
    }

    fn run_until(ctx: &egui::Context, p: &mut Player, what: &str, mut seen: impl FnMut(&Player), done: impl Fn(&Player) -> bool) {
        let t0 = Instant::now();
        while !done(p) {
            assert!(t0.elapsed() < Duration::from_secs(90), "timed out waiting for {what}");
            frame_with(ctx, p, vec![]);
            seen(p);
            std::thread::sleep(Duration::from_millis(15));
        }
    }

    fn has(t: &[String], s: &str) -> bool {
        t.iter().any(|x| x == s)
    }

    #[test]
    fn moods_pages_playlists_columns_tabs_and_details() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        i18n::set_thread_language("en");
        let tmp = std::env::temp_dir().join(format!("rhr-moods-ui-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music").join("Invented Band").join("Feelings");
        std::fs::create_dir_all(&music).unwrap();
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        std::env::set_var(realhires::covers::COVERS_ENV, tmp.join("covers"));
        let happy = flac(&music, 1, "Happy", 128, true, 100);
        let sad = flac(&music, 2, "Sad", 72, false, 12);
        let intense = flac(&music, 3, "Intense", 150, false, 100);
        let chill = flac(&music, 4, "Chill", 84, true, 12);
        let mut cfg = realhires::settings::Settings::default();
        cfg.library_folders = vec![tmp.join("Music").to_string_lossy().into_owned()];
        assert!(cfg.analyse_moods, "on by default");
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);

        // ---- the analysis after the library update, its progress in the sidebar
        let mut status = vec![];
        run_until(&ctx, &mut p, "the mood analysis", |p| status.extend(p.status_line()), |p| {
            p.lib.tracks.len() == 4 && p.lib_progress.is_none() && !p.moods_busy() && p.moods.last.is_some()
        });
        assert!(status.iter().any(|s| s.starts_with("Analysing moods: ") && s.ends_with(" of 4 songs")), "{status:?}");
        assert_eq!(p.status_line(), None, "cleared when the job ends");
        assert_eq!(p.moods.last, Some((4, 0)));
        for (path, mood, tempo) in [(&happy, "Happy", "Up-tempo"), (&sad, "Sad", "Slow"), (&intense, "Intense", "Up-tempo"), (&chill, "Chill", "Slow")] {
            let m = &p.moods.map[path.as_str()];
            assert_eq!((m.mood.as_str(), m.tempo.as_str()), (mood, tempo), "{path}: {m:?}");
        }
        let bpm = p.moods.map[happy.as_str()].bpm;
        assert!((bpm - 128).abs() <= 2);

        // ---- the Library page's entry, after Composers, and the sidebar's
        let entries = p.library_entries();
        let i = entries.iter().position(|e| e.page == Page::Moods).unwrap();
        assert_eq!(entries[i - 1].page, Page::Composers);
        assert_eq!((entries[i].name.as_str(), entries[i].count.as_str()), ("Moods", "4 songs analysed"));
        let t = texts(&ctx, &mut p);
        assert!(has(&t, "Moods") && has(&t, "4 songs analysed"), "{t:?}");

        // ---- the Moods page: the cards, their counts, the note that they are estimates
        p.go(Page::Moods);
        let t = texts(&ctx, &mut p);
        for s in ["Moods", "Tempo", "4 of 4 songs analysed", moods::ESTIMATE_NOTE, "Happy", "Sad", "Chill", "Intense", "Up-tempo", "Mid-tempo", "Slow", "Major key, lively", "Slower than 90 BPM"] {
            assert!(has(&t, s), "{s}: {t:?}");
        }
        assert_eq!(t.iter().filter(|s| *s == "1 song").count(), 4, "{t:?}");
        assert_eq!(t.iter().filter(|s| *s == "2 songs").count(), 2, "{t:?}");
        assert_eq!(p.moods.card_rects.len(), 7);
        assert!(p.moods.card_rects.iter().all(|(_, r)| r.right() <= WIDTH && r.width() >= CARD_W - 1.0), "{:?}", p.moods.card_rects);
        let up = p.moods.card_rects.iter().find(|(l, _)| l == "Up-tempo").unwrap().1;
        click(&ctx, &mut p, up.center());
        assert_eq!(p.page, Page::Mood("tempo", "Up-tempo".into()));
        assert_eq!(p.page_label(&p.page.clone()), "Up-tempo");

        // ---- the card's page: Create Playlist, again with " 2"
        let t = texts(&ctx, &mut p);
        for s in ["Up-tempo", "2 songs, 0 minutes", "Play", "Shuffle", "Create Playlist", moods::ESTIMATE_NOTE] {
            assert!(has(&t, s), "{s}: {t:?}");
        }
        assert_eq!(*p.mood_list("tempo", "Up-tempo"), vec![happy.clone(), intense.clone()]);
        let create = p.moods.create_rect.expect("Create Playlist");
        assert!(create.right() <= WIDTH, "{create:?}");
        click(&ctx, &mut p, create.center());
        let names: Vec<&str> = p.state.playlists.iter().map(|x| x.name.as_str()).collect();
        assert_eq!(names, ["Up-tempo songs"]);
        assert_eq!(p.state.playlists[0].songs, vec![happy.clone(), intense.clone()]);
        assert_eq!(p.moods.message, "Made the playlist “Up-tempo songs” with 2 songs.");
        assert!(has(&texts(&ctx, &mut p), "Made the playlist “Up-tempo songs” with 2 songs."));
        click(&ctx, &mut p, create.center());
        assert_eq!(p.state.playlists.iter().map(|x| x.name.as_str()).collect::<Vec<_>>(), ["Up-tempo songs", "Up-tempo songs 2"]);

        // ---- the BPM and Mood columns, chosen as the other columns
        let cols = p.state.columns_of("songs");
        p.state.set_columns("songs", &[cols, vec!["bpm".into(), "mood".into()]].concat());
        let t = texts(&ctx, &mut p);
        assert!(has(&t, "BPM") && has(&t, "Mood") && has(&t, &bpm.to_string()) && has(&t, "Intense"), "{t:?}");
        let track = p.lib.track(&happy).unwrap().clone();
        assert_eq!(crate::player::cell_text(&track, SongCol::Bpm), bpm.to_string());
        assert_eq!(crate::player::cell_text(&track, SongCol::Mood), "Happy");
        assert_eq!(SongCol::Bpm.info().1, "BPM (estimated)");
        let tip = mood_tip(&happy);
        assert!(tip.starts_with("Estimated from the audio: Happy · Energetic · ") && tip.contains("C major"), "{tip}");

        // ---- a tab on moods, sorted by BPM
        use realhires::smarttabs::{self, Rule, Tab};
        let rule = |f: &str, v: &str| Rule { field: f.into(), op: "is".into(), value: realhires::json::Value::Str(v.into()) };
        let tab = Tab { match_mode: "any".into(), sort: "bpm".into(), rules: vec![rule("mood", "Sad"), rule("tempo", "Up-tempo")], ..Tab::new("Moody") };
        smarttabs::save_tab(&tab, None, None).unwrap();
        p.reload_tabs();
        assert_eq!(*p.tab_list("Moody"), vec![sad.clone(), happy.clone(), intense.clone()]);

        // ---- Details on Now Playing
        p.play_list(vec![happy.clone()], Some(&happy), "Happy");
        p.np_tab = crate::player::NpTab::Details;
        p.go(Page::NowPlaying);
        texts(&ctx, &mut p);
        let d = p.np_hits.details.join("\n");
        assert!(d.contains("Mood (estimated): Happy, Energetic"), "{d}");
        assert!(d.contains(&format!("Tempo (estimated): {bpm} BPM, Up-tempo")) && d.contains("Key (estimated): C major"), "{d}");

        // ---- a changed song is analysed again after the library update, the others not
        let f = std::fs::File::options().write(true).open(&chill).unwrap();
        f.set_modified(std::time::SystemTime::now() + Duration::from_secs(5)).unwrap();
        drop(f);
        p.moods.last = None;
        p.update_library();
        run_until(&ctx, &mut p, "the changed song", |_| {}, |p| p.lib_progress.is_none() && !p.moods_busy() && p.moods.last.is_some());
        assert_eq!(p.moods.last, Some((1, 0)));

        // ---- the setting off: nothing runs after an update; by hand it still can
        p.scanner.cfg.analyse_moods = false;
        p.moods_setting_changed(false);
        p.go(Page::Moods);
        assert!(has(&texts(&ctx, &mut p), "4 of 4 songs analysed. Analysing is turned off in Settings → Library."));
        assert!(!p.analyse_moods(false));
        assert!(p.analyse_moods(true));
        run_until(&ctx, &mut p, "a run by hand", |_| {}, |p| !p.moods_busy());
        assert_eq!(p.moods.last, Some((0, 0)));

        // ---- Japanese
        i18n::set_thread_language("ja");
        let t = texts(&ctx, &mut p);
        for s in ["ムード", "テンポ", "楽しい", "悲しい", "チル", "激しい", "アップテンポ", "ミドルテンポ", "スロー", "長調・にぎやか", "90 BPM未満"] {
            assert!(has(&t, s), "{s}: {t:?}");
        }
        assert!(t.iter().any(|s| s.starts_with("4曲中4曲を解析済み")), "{t:?}");
        p.go(Page::Mood("mood", "Sad".into()));
        let t = texts(&ctx, &mut p);
        assert!(has(&t, "悲しい") && has(&t, "プレイリストを作成"), "{t:?}");
        let id = p.create_mood_playlist("mood", "Sad").unwrap();
        assert_eq!(p.state.playlist(&id).unwrap().name, "悲しい曲");
        assert_eq!(p.moods.message, "1曲のプレイリスト「悲しい曲」を作成しました。");
        assert_eq!(detail_rows(p.moods.map.get(sad.as_str()))[2].1, "イ短調");
        i18n::set_thread_language("en");
        p.on_exit();
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
