//! The music player window (2.0), as `docs/player-spec.md` describes it: a
//! sidebar with the library, a playback bar with the "LCD", the pages
//! (`pages.rs`), the Up Next panel and the Playback info and Equalizer windows.
//! The 1.x scanner (`app::App`) lives on as the Quality check page.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

use egui::{pos2, vec2, Align, Align2, Color32, CornerRadius, Frame, Layout, Margin, Rect, RichText, Sense, Stroke, Ui};
use realhires::i18n::{self, tr_name, tr_text};
use realhires::musiclib::{self, Album, Group, Library, Track};
use realhires::playerstate::{self, PlayerState};
use realhires::playqueue::{repeat_label, shuffle_label, PlayQueue, Song, REPEAT_MODES, SHUFFLE_MODES};
use realhires::pyunicode::casefold;
use realhires::settings::Settings;
use realhires::{n_, tr};

use crate::app::App;
use crate::audio::{Engine, Event};
use crate::icons::{self, Icon};
use crate::look::{self, Palette};

#[derive(Clone, Debug, PartialEq)]
pub enum Page {
    /// the start page with the fourteen entries
    Library,
    AllSongs,
    /// a library folder or one of its subfolders (None: the library folders)
    Folders(Option<String>),
    Albums,
    Album(String),
    Artists,
    AlbumArtists,
    Genres,
    Years,
    Composers,
    Playlists,
    Playlist(String),
    Liked,
    Disliked,
    RecentlyPlayed,
    Recent,
    Stats,
    StatsDetails,
    Search,
    NowPlaying,
    Settings,
    Quality,
}

impl Page {
    /// The page's name in English (also used as an id and as the queue's
    /// name); `shown_title()` is the name in the language in use.
    pub fn title(&self) -> &'static str {
        match self {
            Page::Library => n_!("Library"),
            Page::AllSongs => n_!("All Songs"),
            Page::Folders(_) => n_!("Folders"),
            Page::Albums | Page::Album(_) => n_!("Albums"),
            Page::Artists => n_!("Artists"),
            Page::AlbumArtists => n_!("Album Artists"),
            Page::Genres => n_!("Genres"),
            Page::Years => n_!("Years"),
            Page::Composers => n_!("Composers"),
            Page::Playlists | Page::Playlist(_) => n_!("Playlists"),
            Page::Liked => n_!("Liked Songs"),
            Page::Disliked => n_!("Disliked Songs"),
            Page::RecentlyPlayed => n_!("Recently Played"),
            Page::Recent => n_!("Recently Added"),
            Page::Stats | Page::StatsDetails => n_!("Your Stats"),
            Page::Search => n_!("Search"),
            Page::NowPlaying => n_!("Now Playing"),
            Page::Settings => n_!("Settings"),
            Page::Quality => n_!("Quality check"),
        }
    }

    /// The page's name in the language in use.
    pub fn shown_title(&self) -> &'static str {
        i18n::tr(self.title())
    }
}

/// The names a queue gets from a page rather than from the library (see
/// `Page::title()`; "Songs" is the queue of a single song).
const PAGE_QUEUE_NAMES: [&str; 20] = [
    "Library", "All Songs", "Folders", "Albums", "Artists", "Album Artists", "Genres", "Years", "Composers",
    "Playlists", "Liked Songs", "Disliked Songs", "Recently Played", "Recently Added", "Your Stats", "Search",
    "Now Playing", "Settings", "Quality check", n_!("Songs"),
];

/// The queue's name as "Playing from …" shows it: the queue keeps the English
/// name of a page (or "Search: …"), shown in the language in use; an album,
/// artist or playlist name stays as it is.
pub fn shown_queue_name(name: &str) -> String {
    if PAGE_QUEUE_NAMES.contains(&name) {
        return i18n::tr(name).to_string();
    }
    if let Some(q) = name.strip_prefix("Search: ") {
        return tr!("Search: {query}", query = q);
    }
    tr_name(name).to_string()
}

/// A count in the language in use, with thousands separators:
/// `count(n, n_!("1 song"), n_!("{n} songs"))` ("1 song", "1,234 songs").
pub fn count(n: usize, one: &'static str, many: &'static str) -> String {
    if n == 1 {
        i18n::tr(one).to_string()
    } else {
        i18n::fill(i18n::tr(many), &[("n", realhires::stats::thousands(n as i64))])
    }
}

/// The quality badge of a song ("Hi-Res Lossless", "Lossless" or the codec
/// with its bit rate: the core's text) in the language in use.
pub fn quality_label(t: &Track) -> String {
    tr_text(&musiclib::quality(t))
}

/// "Artist — Album" under a song's title, with the names of missing tags translated.
pub fn artist_album(t: &Track) -> String {
    format!("{} — {}", tr_name(&t.display_artist()), tr_name(&t.display_album()))
}

/// A small window that asks for a name (new playlist, preset, theme...).
pub struct NameDialog {
    pub title: String,
    pub text: String,
    pub action: NameAction,
    pub error: String,
}

#[derive(Clone, Debug, PartialEq)]
pub enum NameAction {
    NewPlaylist(Vec<String>),
    RenamePlaylist(String),
    SaveEqPreset,
    RenameEqPreset(String),
    NewTheme(String),
    RenameTheme(String),
}

/// A question before something is deleted.
#[derive(Clone, Debug, PartialEq)]
pub enum ConfirmAction {
    DeletePlaylist(String),
    DeleteEqPreset(String),
    DeleteTheme(String),
    ClearHistory,
}

/// The song being listened to, for the play history.
struct Listening {
    token: u64,
    path: String,
    started: f64,
    seconds: f64,
    last_pos: f64,
}

fn wall_clock() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

enum LibMsg {
    Progress(usize, usize),
    Done(Vec<Track>),
}

/// The library, grouped for the pages.
#[derive(Default)]
pub struct LibView {
    pub tracks: Vec<Track>,
    pub by_path: HashMap<String, usize>,
    pub albums: Vec<Album>,
    pub album_index: HashMap<String, usize>,
    pub artists: Vec<Group>,
    pub album_artists: Vec<Group>,
    pub genres: Vec<Group>,
    pub years: Vec<Group>,
    pub composers: Vec<Group>,
    /// album indices, newest first
    pub recent: Vec<usize>,
    pub songs: Vec<Song>,
}

impl LibView {
    pub fn new(mut tracks: Vec<Track>) -> LibView {
        musiclib::sort_songs(&mut tracks);
        let by_path = tracks.iter().enumerate().map(|(i, t)| (t.path.clone(), i)).collect();
        let albums = musiclib::albums(&tracks);
        let album_index = albums.iter().enumerate().map(|(i, a)| (a.key.clone(), i)).collect();
        let mut recent: Vec<usize> = (0..albums.len()).collect();
        let newest = |a: &Album| a.tracks.iter().map(|t| t.mtime_ns).max().unwrap_or(0);
        recent.sort_by(|a, b| newest(&albums[*b]).cmp(&newest(&albums[*a])));
        LibView {
            artists: musiclib::artists(&tracks),
            album_artists: musiclib::album_artists(&tracks),
            genres: musiclib::genres(&tracks),
            years: musiclib::years(&tracks),
            composers: musiclib::composers(&tracks),
            songs: tracks.iter().map(Track::song).collect(),
            tracks,
            by_path,
            albums,
            album_index,
            recent,
        }
    }

    pub fn track(&self, path: &str) -> Option<&Track> {
        self.by_path.get(path).map(|i| &self.tracks[*i])
    }

    pub fn album_of(&self, path: &str) -> Option<&Album> {
        let key = self.track(path)?.song().album_key();
        self.album_index.get(&key).map(|i| &self.albums[*i])
    }
}

/// Album covers, made by FFmpeg in a worker thread (`covers::cover_png`).
pub struct Covers {
    tx: Sender<(String, Vec<String>)>,
    rx: Receiver<(String, Option<Vec<u8>>)>,
    tex: HashMap<String, Option<egui::TextureHandle>>,
    pending: HashSet<String>,
}

impl Covers {
    fn new(ctx: &egui::Context) -> Covers {
        let (tx, jobs) = mpsc::channel::<(String, Vec<String>)>();
        let (done, rx) = mpsc::channel();
        let ctx = ctx.clone();
        std::thread::Builder::new()
            .name("player-covers".into())
            .spawn(move || {
                while let Ok((key, paths)) = jobs.recv() {
                    let png = realhires::covers::cover_png(&paths, 480);
                    if done.send((key, png)).is_err() {
                        break;
                    }
                    ctx.request_repaint();
                }
            })
            .expect("cover thread");
        Covers { tx, rx, tex: HashMap::new(), pending: HashSet::new() }
    }

    fn poll(&mut self, ctx: &egui::Context) {
        while let Ok((key, png)) = self.rx.try_recv() {
            self.pending.remove(&key);
            let tex = png.and_then(|d| image::load_from_memory(&d).ok()).map(|img| {
                let rgba = img.to_rgba8();
                let ci = egui::ColorImage::from_rgba_unmultiplied(
                    [rgba.width() as usize, rgba.height() as usize],
                    rgba.as_raw(),
                );
                ctx.load_texture(format!("player cover {key}"), ci, egui::TextureOptions::LINEAR)
            });
            self.tex.insert(key, tex);
        }
    }

    /// The cover of an album (None while it is made, or when it has none).
    pub fn get(&mut self, key: &str, paths: &[String]) -> Option<egui::TextureHandle> {
        if let Some(t) = self.tex.get(key) {
            return t.clone();
        }
        if self.pending.insert(key.to_string()) {
            let _ = self.tx.send((key.to_string(), paths.iter().take(4).cloned().collect()));
        }
        None
    }

    pub fn clear(&mut self) {
        self.tex.clear();
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum ShownLyrics {
    Synced(Vec<(u32, String)>),
    Plain(String),
    Instrumental,
}

/// What the Playback info window shows of the saved Real-Hi-Res verdict
/// (the verdict and its summary, shown in the language in use).
pub struct InfoWindow {
    pub path: String,
    pub verdict: Option<(realhires::verdict::Verdict, String)>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SongCol {
    Title,
    Artist,
    Album,
    Genre,
    Time,
    Quality,
}

pub struct NowPlaying {
    pub token: u64,
    pub path: String,
    /// false once the queue ended or playback was stopped
    pub active: bool,
}

pub struct Player {
    pub scanner: App,
    pub ctx: egui::Context,
    pub state: PlayerState,
    state_dirty: bool,
    state_saved: Instant,
    pub lib: LibView,
    lib_rx: Option<Receiver<LibMsg>>,
    pub lib_progress: Option<(usize, usize)>,
    lib_stop: Arc<AtomicBool>,
    pub page: Page,
    pub back: Vec<Page>,
    pub search: String,
    search_due: Option<Instant>,
    pub search_query: String,
    search_from: Option<Page>,
    pub engine: Engine,
    pub queue: Option<PlayQueue>,
    pub queue_name: String,
    pub now: Option<NowPlaying>,
    /// a message for the LCD's second line, and when playback moves on after it
    pub message: Option<(String, Option<Instant>)>,
    fail_streak: usize,
    lyrics: Option<(String, u64, Option<ShownLyrics>)>,
    pub covers: Covers,
    pub show_up_next: bool,
    pub eq_open: bool,
    pub eq_tab: usize,
    pub info: Option<InfoWindow>,
    pub settings_group: usize,
    settings_draft: Option<Settings>,
    settings_due: Option<Instant>,
    pub lyrics_scrolled: Option<Instant>,
    pub lyrics_line: Option<usize>,
    pub song_sort: (SongCol, bool),
    applied_look: Option<(String, String, u64)>,
    /// the `language` setting in use (egui draws every frame, so a new
    /// language shows at once, whichever window changed the setting)
    applied_language: String,
    pub font_used: String,
    pub selected_song: Option<String>,
    /// "Check new songs when the app starts": scan once the library is read
    check_after_update: bool,
    /// the name chosen on each grouped page (Artists, Genres, Years...)
    pub group_sel: HashMap<&'static str, String>,
    seed: u64,
    // 2.0 additions
    pub custom_themes: Vec<realhires::themes::Theme>,
    /// the themes of the files in the themes folder (read again every 2 s on the Settings page)
    pub file_themes: Vec<realhires::themes::FileTheme>,
    pub file_themes_read: Instant,
    themes_gen: u64,
    /// the Edit Info window (one at a time)
    pub edit_info: Option<crate::editinfo::EditInfo>,
    /// the play history (`library.sqlite`)
    pub history: Option<Library>,
    pub history_gen: u64,
    listening: Option<Listening>,
    pub stats_period: String,
    stats_cache: Option<(String, u64, realhires::stats::Stats)>,
    recent_cache: Option<(u64, Vec<(String, f64)>)>,
    pub name_dialog: Option<NameDialog>,
    pub confirm: Option<ConfirmAction>,
    /// what the last EQ import said
    pub eq_notes: Vec<String>,
    /// the playlist whose page is shown (its songs get Remove/Move)
    pub table_playlist: Option<String>,
}

fn seed_now() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(1)
}

pub fn gain_of(state: &PlayerState) -> f32 {
    if state.muted {
        0.0
    } else {
        (state.volume * state.volume) as f32
    }
}

impl Player {
    pub fn new(ctx: &egui::Context, cfg: Settings, initial: Vec<String>) -> Player {
        realhires::i18n::set_language(&cfg.language);
        let quality_first = !initial.is_empty();
        let scanner = App::new(ctx, cfg, initial);
        let state = playerstate::load(None);
        let tracks = Library::open(None).map(|l| l.tracks()).unwrap_or_default();
        let engine = Engine::new();
        engine.set_gain(gain_of(&state));
        engine.set_eq(&state.eq);
        engine.set_gapless(scanner.cfg.gapless);
        let mut p = Player {
            covers: Covers::new(ctx),
            ctx: ctx.clone(),
            lib: LibView::new(tracks),
            lib_rx: None,
            lib_progress: None,
            lib_stop: Arc::new(AtomicBool::new(false)),
            page: if quality_first { Page::Quality } else { Page::Library },
            back: vec![],
            search: String::new(),
            search_due: None,
            search_query: String::new(),
            search_from: None,
            engine,
            queue: None,
            queue_name: String::new(),
            now: None,
            message: None,
            fail_streak: 0,
            lyrics: None,
            show_up_next: false,
            eq_open: false,
            eq_tab: 0,
            info: None,
            settings_group: 0,
            settings_draft: None,
            settings_due: None,
            lyrics_scrolled: None,
            lyrics_line: None,
            song_sort: (SongCol::Artist, false),
            applied_look: None,
            applied_language: scanner.cfg.language.clone(),
            font_used: String::new(),
            selected_song: None,
            check_after_update: false,
            group_sel: HashMap::new(),
            custom_themes: realhires::themes::load_custom(None),
            file_themes: vec![],
            file_themes_read: Instant::now(),
            themes_gen: 0,
            edit_info: None,
            history: Library::open(None).ok(),
            history_gen: 0,
            listening: None,
            stats_period: realhires::stats::DEFAULT_PERIOD.to_string(),
            stats_cache: None,
            recent_cache: None,
            name_dialog: None,
            confirm: None,
            eq_notes: vec![],
            table_playlist: None,
            seed: seed_now(),
            state,
            state_dirty: false,
            state_saved: Instant::now(),
            scanner,
        };
        p.file_themes = realhires::themes::load_folder(&p.custom_themes, None);
        p.fill_choices();
        p.engine.set_device(&p.scanner.cfg.output_device.clone());
        p.update_library();
        p.check_after_update = p.scanner.cfg.scan_library_on_start && p.lib_rx.is_some();
        // the song of last time, paused where it was
        let last = p.state.last_path.clone();
        if !last.is_empty() && p.lib.by_path.contains_key(&last) && std::path::Path::new(&last).is_file() {
            let pos = p.state.last_position;
            // go on with the song's album, as after playing it from there
            let (list, name) = match p.lib.album_of(&last) {
                Some(a) => (a.tracks.iter().map(|t| t.path.clone()).collect(), a.title.clone()),
                None => (vec![last.clone()], "Songs".to_string()),
            };
            p.make_queue(list, &name);
            if let Some(q) = p.queue.as_mut() {
                q.start(&last);
            }
            let rate = p.lib.track(&last).map(|t| t.sample_rate as u32).unwrap_or(0);
            let token = p.engine.cue(&last, pos, rate);
            p.now = Some(NowPlaying { token, path: last, active: true });
            p.update_next();
        }
        p
    }

    // ------------------------------------------------------------ library ---

    /// Read the tags of new and changed files in the library folders.
    pub fn update_library(&mut self) {
        if self.lib_rx.is_some() {
            return;
        }
        let cfg = self.scanner.cfg.clone();
        if cfg.library_folders.is_empty() {
            return;
        }
        let (tx, rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        self.lib_stop = stop.clone();
        let ctx = self.ctx.clone();
        std::thread::Builder::new()
            .name("library-update".into())
            .spawn(move || {
                let files = musiclib::library_files(&cfg);
                let _ = tx.send(LibMsg::Progress(0, files.len()));
                let Ok(mut lib) = Library::open(None) else {
                    let _ = tx.send(LibMsg::Done(vec![]));
                    return;
                };
                let jobs = realhires::effective_jobs(cfg.jobs).min(8);
                let mut last = Instant::now();
                let tracks = lib.refresh(
                    &files,
                    jobs,
                    &mut |done, total| {
                        if last.elapsed() > Duration::from_millis(200) || done == total {
                            last = Instant::now();
                            let _ = tx.send(LibMsg::Progress(done, total));
                            ctx.request_repaint();
                        }
                    },
                    Some(&stop),
                );
                let _ = tx.send(LibMsg::Done(tracks));
                ctx.request_repaint();
            })
            .expect("library thread");
        self.lib_rx = Some(rx);
        self.lib_progress = Some((0, 0));
    }

    fn poll_library(&mut self) {
        let Some(rx) = &self.lib_rx else { return };
        let mut done = None;
        while let Ok(m) = rx.try_recv() {
            match m {
                LibMsg::Progress(d, t) => self.lib_progress = Some((d, t)),
                LibMsg::Done(tracks) => done = Some(tracks),
            }
        }
        if let Some(tracks) = done {
            self.lib_rx = None;
            self.lib_progress = None;
            self.lib = LibView::new(tracks);
            self.covers.clear();
            if let Some(q) = self.queue.as_mut() {
                q.library = self.lib.songs.clone();
            }
            if std::mem::take(&mut self.check_after_update) && !self.scanner.scanning {
                let page = self.page.clone();
                self.check_library();
                self.scanner.quiet_scan = true;
                self.page = page;
            }
        }
    }

    pub fn library_status(&self) -> Option<String> {
        let (done, total) = self.lib_progress?;
        Some(if total == 0 {
            tr!("Updating the library: looking for songs…").into()
        } else {
            tr!("Updating the library: {done} of {total} songs", done = done, total = total)
        })
    }

    // ---------------------------------------------------------- playback ---

    fn make_queue(&mut self, list: Vec<String>, name: &str) {
        self.seed = self.seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        let mut q = PlayQueue::new(self.lib.songs.clone(), list, &self.state.shuffle, &self.state.repeat, self.seed);
        q.disliked = self.state.disliked.iter().cloned().collect();
        self.queue = Some(q);
        self.queue_name = name.to_string();
    }

    /// Play `list` (paths) from `start` (None: from the first song, or a random
    /// one when shuffle is on); `name` is shown as "Playing from …".
    pub fn play_list(&mut self, list: Vec<String>, start: Option<&str>, name: &str) {
        // songs whose files are gone (in a playlist) are skipped
        let list: Vec<String> = list.into_iter().filter(|p| self.lib.by_path.contains_key(p)).collect();
        if list.is_empty() {
            return;
        }
        self.make_queue(list.clone(), name);
        let first = match start {
            Some(p) => p.to_string(),
            None if self.state.shuffle != "off" => {
                let i = (self.seed >> 33) as usize % list.len();
                list[i].clone()
            }
            None => list[0].clone(),
        };
        let path = self.queue.as_mut().unwrap().start(&first);
        self.fail_streak = 0;
        self.play_path(&path);
    }

    /// The Shuffle button of a page: shuffle on (songs of the list) and play.
    pub fn shuffle_list(&mut self, list: Vec<String>, name: &str) {
        if self.state.shuffle == "off" {
            self.state.shuffle = "songs".into();
            self.state_dirty = true;
        }
        self.play_list(list, None, name);
    }

    fn play_path(&mut self, path: &str) {
        let rate = self.lib.track(path).map(|t| t.sample_rate as u32).unwrap_or(0);
        let token = self.engine.play(path, 0.0, rate);
        self.now = Some(NowPlaying { token, path: path.to_string(), active: true });
        self.message = None;
        self.update_next();
        self.state.last_path = path.to_string();
        self.state.last_position = 0.0;
        self.state_dirty = true;
    }

    /// Tell the engine which song follows gaplessly.
    pub fn update_next(&mut self) {
        let Some(q) = self.queue.as_ref() else {
            self.engine.set_next(None);
            return;
        };
        let Some(cur) = q.current().map(str::to_string) else {
            self.engine.set_next(None);
            return;
        };
        let next = match q.repeat.as_str() {
            "song" => Some(cur.clone()),
            "stop" => None,
            "album" => {
                let key = |p: &str| self.lib.track(p).map(|t| t.song().album_key());
                q.up_next(1).into_iter().next().filter(|n| key(n) == key(&cur))
            }
            _ => q.up_next(1).into_iter().next(),
        };
        match next {
            Some(p) => {
                let rate = self.lib.track(&p).map(|t| t.sample_rate as u32).unwrap_or(0);
                self.engine.set_next(Some((&p, rate)));
            }
            None => self.engine.set_next(None),
        }
    }

    pub fn is_playing(&self) -> bool {
        self.now.as_ref().is_some_and(|n| n.active) && !self.engine.is_paused()
    }

    pub fn toggle_play(&mut self) {
        match &self.now {
            None => {
                let list = self.page_songs();
                let name = self.page.title().to_string();
                self.play_list(list, None, &name);
            }
            Some(n) if !n.active => {
                let p = n.path.clone();
                self.play_path(&p);
            }
            Some(_) => {
                if self.engine.is_paused() {
                    self.engine.resume();
                } else {
                    self.engine.pause();
                    self.remember_position();
                }
            }
        }
    }

    pub fn next(&mut self) {
        let n = self.queue.as_mut().and_then(|q| q.next(false));
        match n {
            Some(p) => self.play_path(&p),
            None => self.stop_at_end(),
        }
    }

    pub fn previous(&mut self) {
        if self.engine.position().unwrap_or(0.0) > 3.0 {
            self.engine.seek(0.0);
            return;
        }
        match self.queue.as_mut().and_then(|q| q.previous()) {
            Some(p) => self.play_path(&p),
            None => self.engine.seek(0.0),
        }
    }

    fn stop_at_end(&mut self) {
        self.engine.stop();
        if let Some(n) = self.now.as_mut() {
            n.active = false;
        }
        self.state.last_position = 0.0;
        self.state_dirty = true;
    }

    pub fn seek(&mut self, seconds: f64) {
        if self.now.as_ref().is_some_and(|n| !n.active) {
            let p = self.now.as_ref().unwrap().path.clone();
            self.play_path(&p);
        }
        self.engine.seek(seconds);
    }

    pub fn set_shuffle(&mut self, mode: &str) {
        self.state.shuffle = mode.to_string();
        if let Some(q) = self.queue.as_mut() {
            q.set_shuffle(mode);
        }
        self.update_next();
        self.state_dirty = true;
    }

    pub fn set_repeat(&mut self, mode: &str) {
        self.state.repeat = mode.to_string();
        if let Some(q) = self.queue.as_mut() {
            q.set_repeat(mode);
        }
        self.update_next();
        self.state_dirty = true;
    }

    pub fn toggle_like(&mut self, path: &str) {
        let liked = self.state.is_liked(path);
        self.state.set_liked(path, !liked);
        self.save_state();
    }

    pub fn set_volume(&mut self, v: f64) {
        self.state.volume = v.clamp(0.0, 1.0);
        self.engine.set_gain(gain_of(&self.state));
        self.state_dirty = true;
    }

    pub fn toggle_mute(&mut self) {
        self.state.muted = !self.state.muted;
        self.engine.set_gain(gain_of(&self.state));
        self.state_dirty = true;
    }

    pub fn set_eq(&mut self) {
        self.engine.set_eq(&self.state.eq);
        self.state_dirty = true;
    }

    fn remember_position(&mut self) {
        if let (Some(n), Some(pos)) = (&self.now, self.engine.position()) {
            self.state.last_path = n.path.clone();
            self.state.last_position = if n.active { pos } else { 0.0 };
            self.state_dirty = true;
        }
    }

    pub fn save_state(&mut self) {
        self.remember_position();
        if let Err(e) = playerstate::save(&self.state, None) {
            eprintln!("Real-Hi-Res: cannot save the player state: {e}");
        }
        self.state_dirty = false;
        self.state_saved = Instant::now();
    }

    fn poll_engine(&mut self) {
        for ev in self.engine.take_events() {
            match ev {
                Event::Started { token, path } => {
                    if self.now.as_ref().is_some_and(|n| n.token == token) {
                        continue;
                    }
                    // a song that followed gaplessly
                    let want = self.queue.as_mut().and_then(|q| q.next(true));
                    if want.as_deref() == Some(path.as_str()) {
                        self.now = Some(NowPlaying { token, path: path.clone(), active: true });
                        self.fail_streak = 0;
                        self.state.last_path = path;
                        self.state_dirty = true;
                        self.update_next();
                    } else if let Some(p) = want {
                        self.play_path(&p);
                    } else {
                        self.stop_at_end();
                    }
                }
                Event::Ended { token, chained, .. } => {
                    if chained || !self.now.as_ref().is_some_and(|n| n.token == token) {
                        continue;
                    }
                    self.fail_streak = 0;
                    match self.queue.as_mut().and_then(|q| q.next(true)) {
                        Some(p) => self.play_path(&p),
                        None => self.stop_at_end(),
                    }
                }
                Event::Failed { token, message, .. } => {
                    if !self.now.as_ref().is_some_and(|n| n.token == token) {
                        continue;
                    }
                    self.fail_streak += 1;
                    let total = self.queue.as_ref().map(|q| q.order.len()).unwrap_or(1).max(1);
                    let go_on = self.fail_streak < total.min(50);
                    self.message = Some((message, go_on.then(|| Instant::now() + Duration::from_secs(2))));
                    if !go_on {
                        self.stop_at_end();
                    }
                }
                Event::OutputError(m) => {
                    self.message = Some((m, None));
                    self.stop_at_end();
                }
            }
        }
        if let Some((_, Some(at))) = &self.message {
            if Instant::now() >= *at {
                self.message.as_mut().unwrap().1 = None;
                // a manual next, so "repeat song" does not loop over a file that fails
                match self.queue.as_mut().and_then(|q| q.next(false)) {
                    Some(p) => {
                        let keep = self.message.take();
                        self.play_path(&p);
                        // the failure stays visible until the next song plays
                        if self.engine.last_error.is_empty() {
                            let _ = keep;
                        }
                    }
                    None => self.stop_at_end(),
                }
            }
        }
    }

    /// The songs of the page shown (for Play with nothing playing).
    pub fn page_songs(&self) -> Vec<String> {
        let all = || self.lib.tracks.iter().map(|t| t.path.clone()).collect::<Vec<_>>();
        match &self.page {
            Page::Album(key) => self
                .lib
                .album_index
                .get(key)
                .map(|i| self.lib.albums[*i].tracks.iter().map(|t| t.path.clone()).collect())
                .unwrap_or_default(),
            Page::Liked => self.liked_songs().iter().map(|t| t.path.clone()).collect(),
            Page::AllSongs => self.sorted_songs(self.lib.tracks.iter().collect()).iter().map(|t| t.path.clone()).collect(),
            Page::Playlist(id) => self.playlist_songs(id),
            Page::Disliked => self.lib.tracks.iter().filter(|t| self.state.is_disliked(&t.path)).map(|t| t.path.clone()).collect(),
            _ => all(),
        }
    }

    pub fn liked_songs(&self) -> Vec<&Track> {
        self.lib.tracks.iter().filter(|t| self.state.is_liked(&t.path)).collect()
    }

    pub fn sorted_songs<'a>(&self, mut v: Vec<&'a Track>) -> Vec<&'a Track> {
        let (col, rev) = self.song_sort;
        let key = |t: &Track| -> (String, String, String) {
            match col {
                SongCol::Title => (casefold(&t.display_title()), casefold(&t.display_artist()), t.path.clone()),
                SongCol::Artist => (casefold(&t.display_artist()), casefold(&t.display_album()), format!("{:04}{:04}", t.disc, t.track)),
                SongCol::Album => (casefold(&t.display_album()), format!("{:04}{:04}", t.disc, t.track), t.path.clone()),
                SongCol::Genre => (casefold(&t.genre), casefold(&t.display_artist()), t.path.clone()),
                SongCol::Time => (format!("{:012.3}", t.duration), String::new(), t.path.clone()),
                SongCol::Quality => (musiclib::quality(t), casefold(&t.display_title()), t.path.clone()),
            }
        };
        v.sort_by_key(|t| key(t));
        if rev {
            v.reverse();
        }
        v
    }

    // ------------------------------------------------------------ lyrics ---

    /// The lyrics of the song playing, read again when lyrics were saved.
    pub fn lyrics_of_now(&mut self) -> Option<ShownLyrics> {
        let path = self.now.as_ref()?.path.clone();
        let gen = self.scanner.lyrics_gen;
        if let Some((p, g, l)) = &self.lyrics {
            if *p == path && *g == gen {
                return l.clone();
            }
        }
        let shown = realhires::lyrics::read_lyrics_file(&path).and_then(|(kind, text, _)| match kind {
            realhires::lyrics::Status::Instrumental => Some(ShownLyrics::Instrumental),
            realhires::lyrics::Status::Synced => {
                let lines = realhires::embed::lrc_entries(&text);
                if lines.is_empty() {
                    let plain = realhires::lyrics::strip_timestamps(&text);
                    (!plain.trim().is_empty()).then_some(ShownLyrics::Plain(plain))
                } else {
                    Some(ShownLyrics::Synced(lines))
                }
            }
            _ => (!text.trim().is_empty()).then(|| ShownLyrics::Plain(text)),
        });
        self.lyrics = Some((path, gen, shown.clone()));
        self.lyrics_line = None;
        shown
    }

    // ------------------------------------------------------------ settings ---

    fn apply_look(&mut self, ctx: &egui::Context) {
        if self.scanner.cfg.language != self.applied_language {
            self.applied_language = self.scanner.cfg.language.clone();
            i18n::set_language(&self.applied_language);
            ctx.request_repaint();
        }
        let want = (self.scanner.cfg.font_family.clone(), self.scanner.cfg.theme.clone(), self.themes_gen);
        if self.applied_look.as_ref() != Some(&want) {
            if self.applied_look.as_ref().is_none_or(|a| a.0 != want.0) {
                self.font_used = look::install_fonts(ctx, &want.0);
            }
            look::install_theme(ctx, &want.1, &self.all_themes());
            self.applied_look = Some(want);
        }
    }

    /// The user's themes changed: save them and draw with them.
    pub fn themes_changed(&mut self) {
        if let Err(e) = realhires::themes::save_custom(&self.custom_themes, None) {
            self.scanner.info = Some((tr!("Themes").into(), tr!("The themes could not be saved: {e}.", e = e)));
        }
        self.file_themes = realhires::themes::load_folder(&self.custom_themes, None);
        self.themes_gen += 1;
        self.fill_choices();
    }

    /// The user's own themes, then those of the themes folder.
    pub fn all_themes(&self) -> Vec<realhires::themes::Theme> {
        let mut all = self.custom_themes.clone();
        all.extend(self.file_themes.iter().map(|f| f.theme.clone()));
        all
    }

    /// The file a theme of the themes folder comes from.
    pub fn theme_file(&self, name: &str) -> Option<&std::path::Path> {
        self.file_themes.iter().find(|f| f.theme.name == name).map(|f| f.file.as_path())
    }

    /// Read the themes folder again; true when its themes changed.
    pub fn reload_file_themes(&mut self) -> bool {
        self.file_themes_read = Instant::now();
        let now = realhires::themes::load_folder(&self.custom_themes, None);
        if now == self.file_themes {
            return false;
        }
        self.file_themes = now;
        self.themes_gen += 1;
        self.fill_choices();
        true
    }

    /// Import a theme file into the user's own themes and use it (Import theme…,
    /// or a .json dropped onto the window).
    pub fn import_theme_path(&mut self, path: &std::path::Path) {
        let all = self.all_themes();
        match std::fs::read(path).map_err(|e| e.to_string()).and_then(|b| {
            realhires::themes::import_theme(&String::from_utf8_lossy(&b), &all).map_err(|e| e.0)
        }) {
            Ok(t) => {
                let name = t.name.clone();
                self.custom_themes.push(t);
                self.themes_changed();
                self.set_theme(&name);
            }
            Err(e) => self.scanner.info = Some((tr!("Import theme").into(), tr_text(&e))),
        }
    }

    /// Open the Edit Info window for one song (one at a time).
    pub fn edit_song_info(&mut self, path: &str) {
        let (heading, sub) = match self.lib.track(path) {
            Some(t) => (
                t.display_title(),
                [t.artist.as_str(), t.album.as_str()].iter().filter(|x| !x.is_empty()).cloned().collect::<Vec<_>>().join(" · "),
            ),
            None => (realhires::paths::file_name(path), String::new()),
        };
        self.edit_info(vec![path.to_string()], heading, sub);
    }

    /// Open the Edit Info window for every song of an album.
    pub fn edit_album_info(&mut self, album: &Album) {
        let n = album.tracks.len();
        let count = count(n, n_!("1 song"), n_!("{n} songs"));
        let sub = [tr_name(&album.artist), count.as_str()].iter().filter(|x| !x.is_empty()).cloned().collect::<Vec<_>>().join(" · ");
        self.edit_info(album.tracks.iter().map(|t| t.path.clone()).collect(), tr_name(&album.title).to_string(), sub);
    }

    /// Open the Edit Info window for these songs; while one is open nothing happens.
    pub fn edit_info(&mut self, paths: Vec<String>, heading: String, subheading: String) {
        let paths: Vec<String> = paths.into_iter().filter(|p| std::path::Path::new(p).is_file()).collect();
        if self.edit_info.is_some() || paths.is_empty() {
            return;
        }
        self.edit_info = Some(crate::editinfo::EditInfo::new(&self.ctx, paths, heading, subheading));
    }

    /// Use the theme called `name` (saved in the settings at once).
    pub fn set_theme(&mut self, name: &str) {
        let mut cfg = self.scanner.cfg.clone();
        cfg.theme = name.to_string();
        self.scanner.apply_settings(cfg.clone());
        if let Some(d) = self.settings_draft.as_mut() {
            d.theme = name.to_string();
        }
    }

    // ----------------------------------------------- history and stats ---

    /// Follow what is heard, and log a song to the play history when it stops
    /// being the current one.
    fn track_listening(&mut self) {
        let cur = self.now.as_ref().filter(|n| n.active).map(|n| (n.token, n.path.clone()));
        let pos = self.engine.position().unwrap_or(0.0);
        let same = match (&self.listening, &cur) {
            (Some(l), Some((token, _))) => l.token == *token,
            (None, None) => true,
            _ => false,
        };
        if !same {
            self.flush_listening();
            if let Some((token, path)) = cur {
                self.listening = Some(Listening { token, path, started: wall_clock(), seconds: 0.0, last_pos: pos });
            }
            return;
        }
        let playing = self.is_playing();
        if let Some(l) = self.listening.as_mut() {
            if playing {
                let d = pos - l.last_pos;
                if d > 0.0 && d < 2.0 {
                    l.seconds += d;
                }
            }
            l.last_pos = pos;
        }
    }

    fn flush_listening(&mut self) {
        let Some(l) = self.listening.take() else { return };
        let track = self.lib.track(&l.path).cloned().unwrap_or_else(|| Track { path: l.path.clone(), ..Default::default() });
        if let Some(h) = &self.history {
            if h.add_play(&track, l.started, l.seconds) {
                self.history_gen += 1;
            }
        }
    }

    /// (stats of the chosen period), computed again when there are new plays.
    pub fn stats(&mut self) -> realhires::stats::Stats {
        let key = (self.stats_period.clone(), self.history_gen);
        if let Some((p, g, st)) = &self.stats_cache {
            if *p == key.0 && *g == key.1 {
                return st.clone();
            }
        }
        let (since, until) = realhires::stats::period_range(&self.stats_period, wall_clock());
        let plays = self.history.as_ref().map(|h| h.plays(since, until)).unwrap_or_default();
        let st = realhires::stats::compute(&plays, &realhires::stats::local_parts);
        self.stats_cache = Some((key.0, key.1, st.clone()));
        st
    }

    /// The songs played last, newest first, with when.
    pub fn recently_played(&mut self) -> Vec<(String, f64)> {
        if let Some((g, v)) = &self.recent_cache {
            if *g == self.history_gen {
                return v.clone();
            }
        }
        let mut v = vec![];
        if let Some(h) = &self.history {
            let paths = h.recently_played(200);
            let plays = h.plays(0.0, None);
            let mut last: HashMap<&str, f64> = HashMap::new();
            for p in &plays {
                last.insert(p.path.as_str(), p.started);
            }
            v = paths.into_iter().map(|p| {
                let t = last.get(p.as_str()).copied().unwrap_or(0.0);
                (p, t)
            }).collect();
        }
        self.recent_cache = Some((self.history_gen, v.clone()));
        v
    }

    pub fn clear_history(&mut self) {
        if let Some(h) = &self.history {
            h.clear_history();
        }
        self.history_gen += 1;
    }

    // ------------------------------------------- playlists and dislikes ---

    pub fn playlist_songs(&self, id: &str) -> Vec<String> {
        self.state.playlist(id).map(|p| p.songs.clone()).unwrap_or_default()
    }

    pub fn new_playlist(&mut self, name: &str, songs: &[String]) -> String {
        let id = self.state.create_playlist(name, songs, wall_clock());
        self.save_state();
        id
    }

    pub fn add_to_playlist(&mut self, id: &str, songs: &[String]) {
        let n = self.state.add_to_playlist(id, songs);
        self.save_state();
        if let Some(p) = self.state.playlist(id) {
            self.message_info(&tr!(
                "{songs} added to “{name}”.",
                songs = crate::style::count(n, n_!("1 song"), n_!("{n} songs")),
                name = p.name
            ));
        }
    }

    fn message_info(&mut self, text: &str) {
        self.scanner.status = text.to_string();
    }

    pub fn toggle_dislike(&mut self, path: &str) {
        let d = self.state.is_disliked(path);
        self.state.set_disliked(path, !d);
        self.save_state();
        if let Some(q) = self.queue.as_mut() {
            q.disliked = self.state.disliked.iter().cloned().collect();
        }
        self.update_next();
    }

    /// Run what a name dialog was opened for; Err keeps it open with the message.
    pub fn apply_name(&mut self, action: NameAction, text: &str) -> Result<(), String> {
        match action {
            NameAction::NewPlaylist(songs) => {
                if realhires::playerstate::clean_name(text).is_empty() {
                    return Err(tr!("Give the playlist a name.").into());
                }
                let id = self.new_playlist(text, &songs);
                if songs.is_empty() {
                    self.go(Page::Playlist(id));
                }
            }
            NameAction::RenamePlaylist(id) => {
                self.state.rename_playlist(&id, text).map_err(|e| e.0)?;
                self.save_state();
            }
            NameAction::SaveEqPreset => {
                let eq = self.state.eq.clone();
                self.state.save_eq_preset(text, &eq, false).map_err(|e| e.0)?;
                self.save_state();
            }
            NameAction::RenameEqPreset(old) => {
                self.state.rename_eq_preset(&old, text).map_err(|e| e.0)?;
                self.save_state();
            }
            NameAction::NewTheme(base) => {
                let all = self.all_themes();
                let name = realhires::themes::check_name(text, &all, "").map_err(|e| e.0)?;
                let mut t = realhires::themes::resolve(&base, self.ctx.global_style().visuals.dark_mode, &all);
                t.name = name.clone();
                self.custom_themes.push(t);
                self.themes_changed();
                self.set_theme(&name);
            }
            NameAction::RenameTheme(old) => {
                let name = realhires::themes::check_name(text, &self.all_themes(), &old).map_err(|e| e.0)?;
                if let Some(t) = self.custom_themes.iter_mut().find(|t| t.name == old) {
                    t.name = name.clone();
                }
                self.themes_changed();
                if self.scanner.cfg.theme == old {
                    self.set_theme(&name);
                }
            }
        }
        Ok(())
    }

    pub fn confirmed(&mut self, action: ConfirmAction) {
        match action {
            ConfirmAction::DeletePlaylist(id) => {
                self.state.delete_playlist(&id);
                self.save_state();
                if self.page == Page::Playlist(id) {
                    self.page = Page::Playlists;
                }
            }
            ConfirmAction::DeleteEqPreset(name) => {
                self.state.delete_eq_preset(&name);
                self.save_state();
            }
            ConfirmAction::DeleteTheme(name) => {
                self.custom_themes.retain(|t| t.name != name);
                self.themes_changed();
                if self.scanner.cfg.theme == name {
                    self.set_theme(realhires::themes::SYSTEM);
                }
            }
            ConfirmAction::ClearHistory => self.clear_history(),
        }
    }

    pub fn ask_name(&mut self, title: &str, text: &str, action: NameAction) {
        self.name_dialog = Some(NameDialog { title: title.into(), text: text.into(), action, error: String::new() });
    }

    /// Import an equalizer preset file's text: applied and kept under its name.
    pub fn import_eq_text(&mut self, text: &str, file_name: &str) -> Result<String, String> {
        let (name, st, notes) = realhires::eq::import_eq(text, file_name).map_err(|e| e.0)?;
        let mut candidate = name.clone();
        let mut n = 2;
        while let Err(e) = self.state.check_preset_name(&candidate, "") {
            if candidate.is_empty() || e.0.contains("built-in") {
                candidate = format!("{name} (imported)");
                if self.state.check_preset_name(&candidate, "").is_ok() {
                    break;
                }
            }
            candidate = format!("{name} {n}");
            n += 1;
            if n > 999 {
                return Err(e.0);
            }
        }
        let saved = self.state.save_eq_preset(&candidate, &st, false).map_err(|e| e.0)?;
        self.state.eq = st;
        self.set_eq();
        self.save_state();
        self.eq_notes = notes;
        Ok(saved)
    }

    /// The Settings page edits a draft that is applied after a short pause.
    pub fn settings_draft(&mut self) -> &mut Settings {
        if self.settings_draft.is_none() {
            self.settings_draft = Some(self.scanner.cfg.clone());
        }
        self.settings_draft.as_mut().unwrap()
    }

    pub fn settings_changed(&mut self) {
        self.settings_due = Some(Instant::now() + Duration::from_millis(400));
    }

    fn apply_settings_due(&mut self, ctx: &egui::Context) {
        let Some(at) = self.settings_due else { return };
        if Instant::now() < at || ctx.input(|i| i.pointer.any_down()) {
            ctx.request_repaint_after(Duration::from_millis(100));
            return;
        }
        self.settings_due = None;
        let Some(new) = self.settings_draft.clone() else { return };
        let old = self.scanner.cfg.clone();
        if new == old {
            return;
        }
        self.scanner.apply_settings(new.clone());
        if new.language != old.language {
            // the next frame is drawn in the new language (no restart needed)
            self.applied_language = new.language.clone();
            i18n::set_language(&new.language);
            ctx.request_repaint();
        }
        if new.gapless != old.gapless {
            self.engine.set_gapless(new.gapless);
        }
        if new.output_device != old.output_device {
            self.engine.set_device(&new.output_device);
        }
        if new.library_folders != old.library_folders || new.extension_set() != old.extension_set() {
            self.lib_stop.store(true, Ordering::Relaxed);
            self.lib_rx = None;
            self.update_library();
        }
    }

    pub fn fill_choices(&mut self) {
        if !self.scanner.choices.contains_key("font_family") {
            let mut fonts = vec![String::new()];
            fonts.extend(look::installed_families());
            self.scanner.choices.insert("font_family", fonts);
        }
        let mut devices = vec![String::new()];
        devices.extend(crate::audio::output_devices());
        self.scanner.choices.insert("output_device", devices);
        self.scanner.choices.insert("theme", realhires::themes::names(&self.all_themes()));
    }

    pub fn add_library_folder(&mut self) {
        if let Some(dir) = rfd::FileDialog::new().pick_folder() {
            let d = dir.to_string_lossy().into_owned();
            let draft = self.settings_draft();
            if !draft.library_folders.contains(&d) {
                draft.library_folders.push(d);
            }
            self.settings_due = Some(Instant::now());
        }
    }

    /// Settings → Library → Check the library for fake files.
    pub fn check_library(&mut self) {
        let folders = self.scanner.cfg.library_folders.clone();
        if folders.is_empty() {
            self.scanner.info =
                Some((tr!("Check the library").into(), tr!("Add your music folder under Settings → Library first.").into()));
            return;
        }
        self.scanner.clear();
        for f in folders {
            self.scanner.add_source(&f);
        }
        self.scanner.start_scan(None, false);
        self.go(Page::Quality);
    }

    pub fn lyrics_for_library(&mut self) {
        let paths: Vec<String> = self.lib.tracks.iter().map(|t| t.path.clone()).collect();
        if paths.is_empty() {
            self.scanner.info = Some((
                tr!("Download synced lyrics").into(),
                tr!("The library is empty; add your music folder under Settings → Library first.").into(),
            ));
            return;
        }
        self.scanner.download_lyrics(paths);
    }

    // -------------------------------------------------------------- pages ---

    pub fn go(&mut self, page: Page) {
        if self.page != page {
            let old = std::mem::replace(&mut self.page, page);
            if !matches!(old, Page::NowPlaying | Page::Search | Page::Quality | Page::Settings) {
                self.back.push(old);
                if self.back.len() > 20 {
                    self.back.remove(0);
                }
            }
            if self.page == Page::Settings {
                self.settings_draft = None;
                self.fill_choices();
            }
        }
    }

    pub fn go_back(&mut self) {
        if let Some(p) = self.back.pop() {
            self.page = p;
        }
    }

    pub fn open_info(&mut self) {
        let Some(n) = &self.now else { return };
        let path = n.path.clone();
        let cfg = &self.scanner.cfg;
        let skey = realhires::store::settings_key(cfg);
        let verdict = self
            .scanner
            .results
            .get(&path)
            .cloned()
            .or_else(|| self.scanner.store.as_ref().and_then(|s| s.lookup(&path, &skey, None)))
            .map(|r| (r.verdict, r.summary.clone()));
        self.info = Some(InfoWindow { path, verdict });
    }

    // ----------------------------------------------------------------- UI ---

    pub fn ui(&mut self, ui: &mut Ui) {
        let ctx = ui.ctx().clone();
        let dropped: Vec<std::path::PathBuf> = ctx.input(|i| {
            i.raw.dropped_files.iter().map(|f| f.path().to_path_buf()).filter(|p| crate::app::is_theme_file(p)).collect()
        });
        for p in dropped {
            self.import_theme_path(&p);
        }
        self.apply_look(&ctx);
        self.scanner.background(&ctx);
        self.covers.poll(&ctx);
        self.poll_library();
        self.poll_engine();
        self.track_listening();
        self.apply_settings_due(&ctx);
        if let Some(at) = self.search_due {
            if Instant::now() >= at {
                self.search_due = None;
                self.search_query = self.search.trim().to_string();
                if self.search_query.is_empty() {
                    if self.page == Page::Search {
                        let back = self.search_from.take().unwrap_or(Page::Albums);
                        self.page = back;
                    }
                } else if self.page != Page::Search {
                    self.search_from = Some(self.page.clone());
                    self.page = Page::Search;
                }
            } else {
                ctx.request_repaint_after(Duration::from_millis(50));
            }
        }
        if self.state_dirty && self.state_saved.elapsed() > Duration::from_secs(10) {
            self.save_state();
        }
        self.keys(&ctx);
        let pal = look::current(&ctx);

        egui::Panel::left("player-sidebar")
            .exact_size(224.0)
            .resizable(false)
            .frame(Frame::new().fill(pal.sidebar).inner_margin(Margin::symmetric(12, 14)))
            .show(ui, |ui| self.sidebar(ui, &pal));
        egui::Panel::top("player-bar")
            .exact_size(70.0)
            .resizable(false)
            .frame(Frame::new().fill(pal.bg).inner_margin(Margin::symmetric(14, 8)))
            .show(ui, |ui| self.playback_bar(ui, &pal));
        if self.show_up_next {
            egui::Panel::right("player-up-next")
                .default_size(300.0)
                .resizable(true)
                .frame(Frame::new().fill(pal.sidebar).inner_margin(Margin::same(14)))
                .show(ui, |ui| self.up_next(ui, &pal));
        }
        egui::CentralPanel::default().frame(Frame::new().fill(pal.bg)).show(ui, |ui| {
            if self.page == Page::Quality {
                self.scanner.page(ui);
            } else {
                Frame::new().inner_margin(Margin::symmetric(28, 18)).show(ui, |ui| self.content(ui, &pal));
            }
        });
        self.windows(&ctx, &pal);
        if self.is_playing() {
            ctx.request_repaint_after(Duration::from_millis(if self.page == Page::NowPlaying { 50 } else { 200 }));
        } else if self.message.as_ref().is_some_and(|m| m.1.is_some()) || self.lib_rx.is_some() {
            ctx.request_repaint_after(Duration::from_millis(200));
        }
    }

    fn keys(&mut self, ctx: &egui::Context) {
        if self.page == Page::Quality || ctx.egui_wants_keyboard_input() {
            return;
        }
        let (space, right, left, cmd_f) = ctx.input(|i| {
            (
                i.key_pressed(egui::Key::Space),
                i.modifiers.command && i.key_pressed(egui::Key::ArrowRight),
                i.modifiers.command && i.key_pressed(egui::Key::ArrowLeft),
                i.modifiers.command && i.key_pressed(egui::Key::F),
            )
        });
        if space {
            self.toggle_play();
        }
        if right && self.now.is_some() {
            self.next();
        }
        if left && self.now.is_some() {
            self.previous();
        }
        if cmd_f {
            ctx.memory_mut(|m| m.request_focus(egui::Id::new("player-search")));
        }
    }

    fn sidebar(&mut self, ui: &mut Ui, pal: &Palette) {
        ui.horizontal(|ui| {
            ui.add_space(4.0);
            ui.label(RichText::new(realhires::APP_NAME).font(look::title(17.0)).color(pal.text));
        });
        ui.add_space(10.0);
        let search = egui::TextEdit::singleline(&mut self.search)
            .id(egui::Id::new("player-search"))
            .hint_text(tr!("Search"))
            .desired_width(f32::INFINITY)
            .margin(Margin::symmetric(8, 5));
        if ui.add(search).changed() {
            self.search_due = Some(Instant::now() + Duration::from_millis(200));
        }
        ui.add_space(10.0);
        let section = |ui: &mut Ui, text: &str| {
            ui.add_space(8.0);
            ui.label(RichText::new(text).font(look::title(11.5)).color(pal.secondary));
            ui.add_space(2.0);
        };
        let height = ui.available_height() - 64.0;
        egui::ScrollArea::vertical().id_salt("sidebar").max_height(height.max(100.0)).auto_shrink([false, true]).show(ui, |ui| {
            if sidebar_item(ui, pal, Icon::Library, tr!("Library"), self.page == Page::Library).clicked() {
                self.back.clear();
                self.go(Page::Library);
            }
            section(ui, tr!("Library"));
            let items: [(Icon, &str, Page); 8] = [
                (Icon::Note, "All Songs", Page::AllSongs),
                (Icon::Folder, "Folders", Page::Folders(None)),
                (Icon::Albums, "Albums", Page::Albums),
                (Icon::Person, "Artists", Page::Artists),
                (Icon::People, "Album Artists", Page::AlbumArtists),
                (Icon::Genres, "Genres", Page::Genres),
                (Icon::Calendar, "Years", Page::Years),
                (Icon::Composer, "Composers", Page::Composers),
            ];
            for (icon, label, page) in items {
                let selected = self.page == page
                    || matches!((&self.page, &page), (Page::Album(_), Page::Albums) | (Page::Folders(Some(_)), Page::Folders(None)));
                if sidebar_item(ui, pal, icon, i18n::tr(label), selected).clicked() {
                    self.back.clear();
                    self.go(page);
                }
            }
            section(ui, tr!("Playlists"));
            if sidebar_item(ui, pal, Icon::Playlist, tr!("Playlists"), self.page == Page::Playlists).clicked() {
                self.go(Page::Playlists);
            }
            if sidebar_item(ui, pal, Icon::Heart, tr!("Liked Songs"), self.page == Page::Liked).clicked() {
                self.go(Page::Liked);
            }
            if sidebar_item(ui, pal, Icon::ThumbDown, tr!("Disliked Songs"), self.page == Page::Disliked).clicked() {
                self.go(Page::Disliked);
            }
            let lists: Vec<(String, String)> = self.state.playlists.iter().map(|p| (p.id.clone(), p.name.clone())).collect();
            for (id, name) in lists {
                let page = Page::Playlist(id);
                if sidebar_item(ui, pal, Icon::Note, &name, self.page == page).clicked() {
                    self.go(page);
                }
            }
            if sidebar_item(ui, pal, Icon::Playlist, tr!("New Playlist…"), false).clicked() {
                self.ask_name(tr!("New Playlist"), tr!("New Playlist"), NameAction::NewPlaylist(vec![]));
            }
            section(ui, tr!("History"));
            let items: [(Icon, &str, Page); 3] = [
                (Icon::Clock, "Recently Played", Page::RecentlyPlayed),
                (Icon::Albums, "Recently Added", Page::Recent),
                (Icon::Chart, "Your Stats", Page::Stats),
            ];
            for (icon, label, page) in items {
                let selected = self.page == page || (page == Page::Stats && self.page == Page::StatsDetails);
                if sidebar_item(ui, pal, icon, i18n::tr(label), selected).clicked() {
                    self.go(page);
                }
            }
        });
        ui.with_layout(Layout::bottom_up(Align::Min), |ui| {
            if let Some(s) = self.library_status() {
                ui.label(RichText::new(s).small().color(pal.secondary));
                ui.add_space(4.0);
            }
            if sidebar_item(ui, pal, Icon::Gear, tr!("Settings"), self.page == Page::Settings).clicked() {
                self.go(Page::Settings);
            }
            if self.page == Page::Quality
                && sidebar_item(ui, pal, Icon::Check, tr!("Quality check"), true).clicked()
            {
                self.go(Page::Quality);
            }
        });
    }

    fn playback_bar(&mut self, ui: &mut Ui, pal: &Palette) {
        let has = self.now.is_some();
        let full = ui.available_rect_before_wrap();
        ui.allocate_ui_with_layout(full.size(), Layout::left_to_right(Align::Center), |ui| {
            ui.spacing_mut().item_spacing.x = 6.0;
            // left: transport
            let shuffle_on = self.state.shuffle != "off";
            let tip = tr!("Shuffle: {mode}", mode = i18n::tr(shuffle_label(&self.state.shuffle)));
            let r = icons::button(ui, Icon::Shuffle, 30.0, shuffle_on, true, &tip);
            egui::Popup::menu(&r).show(|ui| {
                ui.label(RichText::new(tr!("Shuffle")).strong());
                for m in SHUFFLE_MODES {
                    if ui.radio(self.state.shuffle == m, i18n::tr(shuffle_label(m))).clicked() {
                        self.set_shuffle(m);
                        ui.close();
                    }
                }
            });
            if icons::button(ui, Icon::Previous, 34.0, false, has, tr!("Previous")).clicked() {
                self.previous();
            }
            let play_icon = if self.is_playing() { Icon::Pause } else { Icon::Play };
            if icons::button(ui, play_icon, 42.0, false, true, if self.is_playing() { tr!("Pause") } else { tr!("Play") }).clicked() {
                self.toggle_play();
            }
            if icons::button(ui, Icon::Next, 34.0, false, has, tr!("Next")).clicked() {
                self.next();
            }
            let rep = self.state.repeat.as_str();
            let rep_icon = match rep {
                "song" => Icon::RepeatOne,
                "stop" => Icon::RepeatStop,
                _ => Icon::Repeat,
            };
            let tip = tr!("Repeat: {mode}", mode = i18n::tr(repeat_label(rep)));
            let r = icons::button(ui, rep_icon, 30.0, rep != "off", true, &tip);
            egui::Popup::menu(&r).show(|ui| {
                ui.label(RichText::new(tr!("Repeat")).strong());
                for m in REPEAT_MODES {
                    if ui.radio(self.state.repeat == m, i18n::tr(repeat_label(m))).clicked() {
                        self.set_repeat(m);
                        ui.close();
                    }
                }
            });
            // right part is laid out from the right edge
            let right_w = 360.0;
            let lcd_w = (ui.available_width() - right_w - 16.0).clamp(280.0, 720.0);
            let gap = ((ui.available_width() - right_w - lcd_w) / 2.0).max(8.0);
            ui.add_space(gap);
            self.lcd(ui, pal, lcd_w);
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.spacing_mut().item_spacing.x = 4.0;
                let mut v = self.state.volume as f32;
                let slider = ui.add_sized([96.0, 20.0], egui::Slider::new(&mut v, 0.0..=1.0).show_value(false));
                if slider.changed() {
                    self.set_volume(v as f64);
                }
                let vol_icon = if self.state.muted || self.state.volume <= 0.0 { Icon::SpeakerMuted } else { Icon::Speaker };
                if icons::button(ui, vol_icon, 28.0, false, true, if self.state.muted { tr!("Unmute") } else { tr!("Mute") }).clicked() {
                    self.toggle_mute();
                }
                ui.add_space(6.0);
                if icons::button(ui, Icon::Equalizer, 28.0, self.state.eq.enabled, true, tr!("Equalizer")).clicked() {
                    self.eq_open = !self.eq_open;
                }
                if icons::button(ui, Icon::UpNext, 28.0, self.show_up_next, true, tr!("Up Next")).clicked() {
                    self.show_up_next = !self.show_up_next;
                }
                if icons::button(ui, Icon::Lyrics, 28.0, self.page == Page::NowPlaying, true, tr!("Lyrics")).clicked() {
                    if self.page == Page::NowPlaying {
                        self.go_back_from_now();
                    } else {
                        self.go(Page::NowPlaying);
                    }
                }
                let now_path = self.now.as_ref().map(|n| n.path.clone());
                let r = icons::button(ui, Icon::More, 28.0, false, has, tr!("More"));
                if let Some(p) = now_path.clone() {
                    egui::Popup::menu(&r).show(|ui| self.song_menu(ui, &p));
                }
                let disliked = now_path.as_ref().is_some_and(|p| self.state.is_disliked(p));
                if icons::button(ui, Icon::ThumbDown, 28.0, disliked, has, if disliked { tr!("Remove dislike") } else { tr!("Dislike") }).clicked() {
                    if let Some(p) = now_path.clone() {
                        self.toggle_dislike(&p);
                    }
                }
                let liked = now_path.as_ref().is_some_and(|p| self.state.is_liked(p));
                let heart = if liked { Icon::HeartFilled } else { Icon::Heart };
                if icons::button(ui, heart, 28.0, liked, has, if liked { tr!("Unlike") } else { tr!("Like") }).clicked() {
                    if let Some(p) = now_path {
                        self.toggle_like(&p);
                    }
                }
            });
        });
    }

    fn go_back_from_now(&mut self) {
        let back = self.back.pop().unwrap_or(Page::Albums);
        self.page = back;
    }

    /// The panel in the middle of the playback bar.
    fn lcd(&mut self, ui: &mut Ui, pal: &Palette, width: f32) {
        let (rect, _) = ui.allocate_exact_size(vec2(width, 54.0), Sense::hover());
        let p = ui.painter_at(rect.expand(2.0));
        p.rect(rect, CornerRadius::same(8), pal.lcd, Stroke::new(1.0, pal.separator), egui::StrokeKind::Inside);
        let Some((now_path, now_active)) = self.now.as_ref().map(|n| (n.path.clone(), n.active)) else {
            p.text(rect.center(), Align2::CENTER_CENTER, realhires::APP_NAME, look::title(15.0), pal.secondary);
            return;
        };
        let path = now_path;
        let track = self.lib.track(&path).cloned().unwrap_or_else(|| Track { path: path.clone(), ..Default::default() });
        // cover
        let cover_rect = Rect::from_min_size(rect.min + vec2(5.0, 5.0), vec2(44.0, 44.0));
        let key = track.song().album_key();
        let paths: Vec<String> = self.lib.album_of(&path).map(|a| a.tracks.iter().map(|t| t.path.clone()).collect()).unwrap_or_else(|| vec![path.clone()]);
        let tex = self.covers.get(&key, &paths);
        cover(&p, cover_rect, tex.as_ref(), &track.display_album(), 5);
        let cover_resp = ui.interact(cover_rect, ui.id().with("lcd-cover"), Sense::click());
        if cover_resp.on_hover_text(tr!("Now Playing")).clicked() {
            self.go(Page::NowPlaying);
        }
        // texts
        let badge = quality_label(&track);
        let badge_font = look::title(10.0);
        let badge_w = ui.ctx().fonts_mut(|f| f.layout_no_wrap(badge.clone(), badge_font.clone(), pal.text).size().x) + 12.0;
        let text_left = cover_rect.right() + 10.0;
        let text_right = rect.right() - badge_w - 14.0;
        let mid = (text_left + text_right) / 2.0;
        let clip = Rect::from_x_y_ranges(text_left..=text_right, rect.y_range());
        let tp = ui.painter_at(clip);
        tp.text(pos2(mid, rect.top() + 13.0), Align2::CENTER_CENTER, track.display_title(), look::title(13.0), pal.text);
        let second = match &self.message {
            Some((m, _)) => m.clone(),
            None => artist_album(&track),
        };
        let second_color = if self.message.is_some() { pal.accent } else { pal.secondary };
        tp.text(pos2(mid, rect.top() + 29.0), Align2::CENTER_CENTER, second, look::text(11.5), second_color);
        // seek bar
        let dur = track.duration.max(0.0);
        let pos = if now_active { self.engine.position().unwrap_or(0.0) } else { 0.0 };
        let bar = Rect::from_x_y_ranges(text_left + 34.0..=text_right - 38.0, rect.top() + 41.0..=rect.top() + 45.0);
        let resp = ui.interact(bar.expand2(vec2(0.0, 5.0)), ui.id().with("seek"), Sense::click_and_drag());
        let frac = if dur > 0.0 { (pos / dur).clamp(0.0, 1.0) as f32 } else { 0.0 };
        let shown = if resp.dragged() || resp.clicked() {
            resp.interact_pointer_pos().map(|q| ((q.x - bar.left()) / bar.width()).clamp(0.0, 1.0)).unwrap_or(frac)
        } else {
            frac
        };
        p.rect_filled(bar, CornerRadius::same(2), pal.separator);
        p.rect_filled(Rect::from_min_max(bar.min, pos2(bar.left() + bar.width() * shown, bar.bottom())), CornerRadius::same(2), pal.secondary);
        if resp.hovered() || resp.dragged() {
            p.circle_filled(pos2(bar.left() + bar.width() * shown, bar.center().y), 5.0, pal.text);
        }
        if (resp.drag_stopped() || resp.clicked()) && dur > 0.0 {
            self.seek(shown as f64 * dur);
        }
        let t_font = look::text(10.0);
        p.text(pos2(bar.left() - 6.0, bar.center().y), Align2::RIGHT_CENTER, fmt_time(shown as f64 * dur), t_font.clone(), pal.secondary);
        p.text(pos2(bar.right() + 6.0, bar.center().y), Align2::LEFT_CENTER, format!("-{}", fmt_time((dur - shown as f64 * dur).max(0.0))), t_font, pal.secondary);
        // quality badge: a click opens the Playback info
        let brect = Rect::from_center_size(pos2(rect.right() - badge_w / 2.0 - 8.0, rect.center().y), vec2(badge_w, 18.0));
        let bresp = ui.interact(brect, ui.id().with("quality-badge"), Sense::click());
        let bcol = if bresp.hovered() { pal.text } else { pal.secondary };
        p.rect_stroke(brect, CornerRadius::same(4), Stroke::new(1.0, bcol), egui::StrokeKind::Inside);
        p.text(brect.center(), Align2::CENTER_CENTER, badge, badge_font, bcol);
        if bresp.on_hover_text(tr!("Playback info")).clicked() {
            self.open_info();
        }
    }

    fn up_next(&mut self, ui: &mut Ui, pal: &Palette) {
        ui.label(RichText::new(tr!("Up Next")).font(look::title(18.0)).color(pal.text));
        if !self.queue_name.is_empty() {
            let from = tr!("Playing from {name}", name = shown_queue_name(&self.queue_name));
            ui.label(RichText::new(from).color(pal.secondary));
        }
        ui.add_space(8.0);
        let Some(q) = &self.queue else {
            ui.label(RichText::new(tr!("Nothing is queued.")).color(pal.secondary));
            return;
        };
        let upcoming = q.up_next(50);
        let base = q.pos + 1;
        if upcoming.is_empty() {
            ui.label(RichText::new(tr!("Nothing comes after this song.")).color(pal.secondary));
        }
        let mut jump = None;
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            for (i, path) in upcoming.iter().enumerate() {
                let t = self.lib.track(path);
                let title = t.map(|t| t.display_title()).unwrap_or_else(|| path.clone());
                let artist = t.map(|t| tr_name(&t.display_artist()).to_string()).unwrap_or_default();
                let (rect, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 38.0), Sense::click());
                if resp.hovered() {
                    ui.painter().rect_filled(rect, CornerRadius::same(6), pal.hover);
                }
                let p = ui.painter_at(rect);
                p.text(rect.left_top() + vec2(8.0, 4.0), Align2::LEFT_TOP, title, look::text(13.0), pal.text);
                p.text(rect.left_top() + vec2(8.0, 21.0), Align2::LEFT_TOP, artist, look::text(11.0), pal.secondary);
                if resp.double_clicked() {
                    jump = Some(base + i);
                }
            }
        });
        if let Some(i) = jump {
            if let Some(q) = self.queue.as_mut() {
                q.pos = i;
                let p = q.order[i].clone();
                self.play_path(&p);
            }
        }
    }

    fn windows(&mut self, ctx: &egui::Context, pal: &Palette) {
        self.dialogs(ctx, pal);
        if let Some(mut w) = self.edit_info.take() {
            let cfg = self.scanner.cfg.clone();
            match w.show(ctx, pal, &cfg) {
                crate::editinfo::Outcome::Open => self.edit_info = Some(w),
                crate::editinfo::Outcome::Closed => {}
                crate::editinfo::Outcome::Saved { close } => {
                    self.update_library();
                    if !close {
                        self.edit_info = Some(w);
                    }
                }
            }
            if self.edit_info.is_some() {
                return; // Esc belongs to the Edit Info window while it is open
            }
        }
        // Esc closes the topmost of the player's windows
        if ctx.input(|i| i.key_pressed(egui::Key::Escape)) && !ctx.egui_wants_keyboard_input() {
            if self.eq_open {
                self.eq_open = false;
                self.save_state();
            } else if self.info.is_some() {
                self.info = None;
            }
        }
        if self.info.is_some() {
            crate::eqwin::info_window(self, ctx, pal);
        }
        if self.eq_open {
            crate::eqwin::eq_window(self, ctx, pal);
        }
    }

    /// The name and confirmation dialogs.
    fn dialogs(&mut self, ctx: &egui::Context, pal: &Palette) {
        if let Some(mut d) = self.name_dialog.take() {
            let mut open = true;
            let mut done: Option<bool> = None;
            egui::Window::new(d.title.clone())
                .id(egui::Id::new("player-name-dialog"))
                .open(&mut open)
                .collapsible(false)
                .resizable(false)
                .pivot(Align2::CENTER_CENTER)
                .default_pos(ctx.content_rect().center())
                .show(ctx, |ui| {
                    ui.label(tr!("Name"));
                    let r = ui.add(egui::TextEdit::singleline(&mut d.text).desired_width(320.0));
                    r.request_focus();
                    if !d.error.is_empty() {
                        // the core's messages (a taken name...) are English: translated here
                        ui.label(RichText::new(tr_text(&d.error)).color(pal.accent));
                    }
                    ui.add_space(6.0);
                    ui.horizontal(|ui| {
                        if ui.button(RichText::new(tr!("OK")).strong()).clicked() || ui.input(|i| i.key_pressed(egui::Key::Enter)) {
                            done = Some(true);
                        }
                        if ui.button(tr!("Cancel")).clicked() || ui.input(|i| i.key_pressed(egui::Key::Escape)) {
                            done = Some(false);
                        }
                    });
                });
            match (open, done) {
                (false, _) | (_, Some(false)) => {}
                (_, Some(true)) => {
                    if let Err(e) = self.apply_name(d.action.clone(), &d.text) {
                        d.error = e;
                        self.name_dialog = Some(d);
                    }
                }
                _ => self.name_dialog = Some(d),
            }
        }
        if let Some(action) = self.confirm.clone() {
            let (title, text, button) = match &action {
                ConfirmAction::DeletePlaylist(id) => (
                    tr!("Delete playlist"),
                    tr!(
                        "Delete the playlist “{name}”? Its songs stay in your library.",
                        name = self.state.playlist(id).map(|p| p.name.as_str()).unwrap_or("")
                    ),
                    tr!("Delete"),
                ),
                ConfirmAction::DeleteEqPreset(n) => {
                    (tr!("Delete preset"), tr!("Delete the equalizer preset “{name}”?", name = n), tr!("Delete"))
                }
                ConfirmAction::DeleteTheme(n) => {
                    (tr!("Delete theme"), tr!("Delete the theme “{name}”?", name = i18n::tr(n)), tr!("Delete"))
                }
                ConfirmAction::ClearHistory => (
                    tr!("Clear the play history"),
                    tr!("Forget every song played so far? Recently Played and Your Stats start again from nothing.").to_string(),
                    tr!("Clear"),
                ),
            };
            let mut open = true;
            let mut done: Option<bool> = None;
            egui::Window::new(title)
                .id(egui::Id::new("player-confirm"))
                .open(&mut open)
                .collapsible(false)
                .resizable(false)
                .pivot(Align2::CENTER_CENTER)
                .default_pos(ctx.content_rect().center())
                .show(ctx, |ui| {
                    ui.set_max_width(380.0);
                    ui.label(text);
                    ui.add_space(8.0);
                    ui.horizontal(|ui| {
                        if ui.button(RichText::new(button).strong().color(pal.accent)).clicked() {
                            done = Some(true);
                        }
                        if ui.button(tr!("Cancel")).clicked() || ui.input(|i| i.key_pressed(egui::Key::Escape)) {
                            done = Some(false);
                        }
                    });
                });
            if !open || done.is_some() {
                self.confirm = None;
            }
            if done == Some(true) {
                self.confirmed(action);
            }
        }
    }

    pub fn on_exit(&mut self) {
        self.flush_listening();
        self.save_state();
        self.lib_stop.store(true, Ordering::Relaxed);
        self.engine.stop();
    }
}

pub fn fmt_time(sec: f64) -> String {
    crate::style::fmt_duration(Some(sec.floor()))
}

fn sidebar_item(ui: &mut Ui, pal: &Palette, icon: Icon, label: &str, selected: bool) -> egui::Response {
    let (rect, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 28.0), Sense::click());
    if selected {
        ui.painter().rect_filled(rect, CornerRadius::same(6), pal.hover);
    } else if resp.hovered() {
        ui.painter().rect_filled(rect, CornerRadius::same(6), pal.hover.gamma_multiply(0.6));
    }
    icons::paint(ui.painter(), Rect::from_center_size(pos2(rect.left() + 16.0, rect.center().y), vec2(18.0, 18.0)), icon, pal.accent);
    ui.painter().text(pos2(rect.left() + 32.0, rect.center().y), Align2::LEFT_CENTER, label, look::text(13.0), pal.text);
    resp
}

/// A cover, or a coloured square with a note when the album has none.
pub fn cover(p: &egui::Painter, rect: Rect, tex: Option<&egui::TextureHandle>, title: &str, radius: u8) {
    match tex {
        Some(t) => {
            let shape = egui::epaint::RectShape::filled(rect, CornerRadius::same(radius), Color32::WHITE)
                .with_texture(t.id(), Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)));
            p.add(shape);
        }
        None => {
            let base = crate::style::placeholder_color(title);
            p.rect_filled(rect, CornerRadius::same(radius), base);
            icons::paint(p, Rect::from_center_size(rect.center(), rect.size() * 0.45), Icon::Note, Color32::from_white_alpha(170));
        }
    }
}

#[cfg(test)]
mod tests {
    //! Drives the whole player headlessly with the null audio output: the
    //! library index, playback, a gapless song change with the queue, lyrics,
    //! likes, shuffle and repeat, the EQ and info windows, search, Settings,
    //! the Quality check page, and the state saved for the next start.
    use super::*;
    use std::process::Command;

    fn ffmpeg(args: &[&str]) {
        let st = Command::new(realhires::probe::tool("ffmpeg")).args(["-v", "error", "-y"]).args(args).status().unwrap();
        assert!(st.success(), "{args:?}");
    }

    fn song(dir: &std::path::Path, album: &str, title: &str, artist: &str, genre: &str, n: u32, rate: u32, secs: f64) -> String {
        let d = dir.join(album);
        std::fs::create_dir_all(&d).unwrap();
        let f = d.join(format!("{n:02} {title}.flac"));
        let src = format!("sine=frequency={}:sample_rate={rate}:duration={secs}", 300 + 40 * n);
        let meta = [format!("title={title}"), format!("artist={artist}"), format!("album={album}"), format!("track={n}"), format!("genre={genre}")];
        let mut args: Vec<&str> = vec!["-f", "lavfi", "-i", &src];
        for m in &meta {
            args.extend(["-metadata", m.as_str()]);
        }
        let out = f.to_string_lossy().into_owned();
        args.push(&out);
        ffmpeg(&args);
        realhires::paths::abspath(&out)
    }

    fn frame(ctx: &egui::Context, p: &mut Player) {
        let input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1280.0, 840.0))),
            ..Default::default()
        };
        ctx.run_ui(input, |ui| p.ui(ui)).drop_without_applying_deltas();
    }

    /// A few frames (a new window takes some to reach its size), and every text the last one drew.
    fn texts(ctx: &egui::Context, p: &mut Player) -> String {
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
        for _ in 0..3 {
            frame(ctx, p);
        }
        let input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1280.0, 840.0))),
            ..Default::default()
        };
        let output = ctx.run_ui(input, |ui| p.ui(ui));
        let mut out = String::new();
        output.shapes.iter().for_each(|s| walk(&s.shape, &mut out));
        output.drop_without_applying_deltas();
        out
    }

    fn run_until(ctx: &egui::Context, p: &mut Player, what: &str, done: impl Fn(&Player) -> bool) {
        let t0 = Instant::now();
        while !done(p) {
            assert!(t0.elapsed() < Duration::from_secs(90), "timed out waiting for {what}");
            frame(ctx, p);
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn library_playback_lyrics_likes_modes_windows_and_state() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-ui-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        let a1 = song(&music, "Paper Lanterns", "Lanterns Over Water", "Mira Calloway", "Pop", 1, 44_100, 2.0);
        let a2 = song(&music, "Paper Lanterns", "Paper in the Rain", "Mira Calloway", "Pop", 2, 44_100, 1.5);
        let b1 = song(&music, "Harbour Lights", "Tidewater", "The Northern Quay", "Jazz", 1, 48_000, 1.5);
        std::fs::write(
            realhires::lyrics::sidecar(&a1, ".lrc"),
            "[00:00.20]Lanterns over water\n[00:01.00]Paper in the rain\n[00:01.60]Folded stars\n",
        )
        .unwrap();
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();

        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg.clone(), vec![]);
        assert_eq!(p.page, Page::Library);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 3 && p.lib_rx.is_none());
        assert_eq!(p.lib.albums.len(), 2);
        assert_eq!((p.lib.artists.len(), p.lib.genres.len()), (2, 2));
        // the Library page and every one of its entries
        let music_dir = realhires::paths::abspath(&music.to_string_lossy());
        for page in [
            Page::Library,
            Page::AllSongs,
            Page::Folders(None),
            Page::Folders(Some(music_dir.clone())),
            Page::Folders(Some(format!("{music_dir}{}Paper Lanterns", std::path::MAIN_SEPARATOR))),
            Page::Albums,
            Page::Artists,
            Page::AlbumArtists,
            Page::Genres,
            Page::Years,
            Page::Composers,
            Page::Playlists,
            Page::Liked,
            Page::Disliked,
            Page::RecentlyPlayed,
            Page::Recent,
            Page::Stats,
            Page::StatsDetails,
        ] {
            p.go(page);
            frame(&ctx, &mut p);
        }
        assert_eq!(p.lib.album_artists.len(), 2);
        let (subs, songs) = musiclib::folder_view(&p.lib.tracks, &music_dir);
        assert_eq!((subs.len(), songs.len()), (2, 0));

        // play the album from its first song; the next song follows gaplessly
        let key = p.lib.track(&a1).unwrap().song().album_key();
        p.go(Page::Album(key.clone()));
        frame(&ctx, &mut p);
        let album: Vec<String> = p.page_songs();
        assert_eq!(album, vec![a1.clone(), a2.clone()]);
        p.play_list(album.clone(), Some(&a1), "Paper Lanterns");
        assert_eq!(p.now.as_ref().unwrap().path, a1);
        run_until(&ctx, &mut p, "playback", |p| p.engine.position().unwrap_or(0.0) > 0.3);
        assert!(p.is_playing());
        assert_eq!(p.engine.info().unwrap().device, "Null output (no sound)");

        // Now Playing reads the synced lyrics
        p.go(Page::NowPlaying);
        frame(&ctx, &mut p);
        match p.lyrics_of_now() {
            Some(ShownLyrics::Synced(lines)) => assert_eq!(lines.len(), 3),
            other => panic!("no synced lyrics: {other:?}"),
        }

        // likes are saved at once
        p.toggle_like(&a1);
        assert!(playerstate::load(None).is_liked(&a1));
        p.go(Page::Liked);
        frame(&ctx, &mut p);
        assert_eq!(p.page_songs(), vec![a1.clone()]);

        // the song ends and the next one of the album plays, without a gap
        run_until(&ctx, &mut p, "the second song", |p| p.now.as_ref().is_some_and(|n| n.path == a2));
        assert_eq!(p.queue.as_ref().unwrap().current(), Some(a2.as_str()));
        // then the album ends: playback stops (repeat off)
        run_until(&ctx, &mut p, "the end of the album", |p| p.now.as_ref().is_some_and(|n| !n.active));
        frame(&ctx, &mut p);
        // both songs are in the play history, the newest first
        let recent: Vec<String> = p.recently_played().into_iter().map(|(x, _)| x).collect();
        assert_eq!(recent, vec![a2.clone(), a1.clone()]);
        let st = p.stats();
        assert_eq!((st.logged, st.songs), (2, 2), "{st:?}");
        assert!(!realhires::stats::cards(&st).is_empty());
        for page in [Page::Library, Page::RecentlyPlayed, Page::Stats, Page::StatsDetails] {
            p.go(page);
            frame(&ctx, &mut p);
        }

        // playlists: make one, add, play from it, rename, delete
        p.apply_name(NameAction::NewPlaylist(vec![b1.clone()]), "Road Trip").unwrap();
        let pid = p.state.playlists[0].id.clone();
        p.add_to_playlist(&pid, &[a2.clone(), "/gone/song.flac".to_string()]);
        assert_eq!(p.playlist_songs(&pid), vec![b1.clone(), a2.clone(), "/gone/song.flac".to_string()]);
        p.go(Page::Playlist(pid.clone()));
        frame(&ctx, &mut p);
        p.play_list(p.playlist_songs(&pid), None, "Road Trip");
        assert_eq!(p.queue.as_ref().unwrap().context, vec![b1.clone(), a2.clone()], "missing files are skipped");
        p.engine.stop();
        p.apply_name(NameAction::RenamePlaylist(pid.clone()), "Night Drive").unwrap();
        assert_eq!(playerstate::load(None).playlists[0].name, "Night Drive");
        assert!(p.apply_name(NameAction::NewPlaylist(vec![]), "   ").is_err());
        p.confirmed(ConfirmAction::DeletePlaylist(pid));
        assert!(playerstate::load(None).playlists.is_empty());

        // a disliked song is left out of a shuffle
        p.toggle_dislike(&a2);
        assert!(playerstate::load(None).is_disliked(&a2));
        p.set_shuffle("library");
        p.play_list(vec![a1.clone(), a2.clone(), b1.clone()], Some(&a1), "Shuffle");
        assert!(!p.queue.as_ref().unwrap().order.contains(&a2), "{:?}", p.queue.as_ref().unwrap().order);
        p.set_shuffle("off");
        p.toggle_dislike(&a2);
        p.engine.stop();

        // EQ presets: save, rename, import an AutoEQ file, export both ways
        p.state.eq.apply_preset("Jazz");
        p.apply_name(NameAction::SaveEqPreset, "My Jazz").unwrap();
        assert!(p.apply_name(NameAction::SaveEqPreset, "rock").is_err());
        p.apply_name(NameAction::RenameEqPreset("My Jazz".into()), "Late Jazz").unwrap();
        assert_eq!(p.state.matching_eq_preset(), "Late Jazz");
        let auto = "Preamp: -6.2 dB\nFilter 1: ON PK Fc 105 Hz Gain 3.5 dB Q 0.70\nFilter 2: ON LSC Fc 105 Hz Gain 4.1 dB Q 0.71\n";
        assert_eq!(p.import_eq_text(auto, "HD 600").unwrap(), "HD 600");
        assert_eq!(p.import_eq_text(auto, "HD 600").unwrap(), "HD 600 2");
        assert!(p.state.eq.parametric_enabled && p.state.eq.bands.len() == 2);
        assert!(realhires::eq::export_apo("HD 600", &p.state.eq).contains("Filter 2: ON LSC Fc 105.0 Hz Gain 4.1 dB Q 0.71"));
        assert!(realhires::eq::import_eq(&realhires::eq::export_json("x", &p.state.eq), "y").is_ok());
        assert_eq!(playerstate::load(None).eq_presets.len(), 3);
        p.eq_open = true;
        frame(&ctx, &mut p);
        p.eq_open = false;

        // a theme of one's own: made from Midnight, a colour changed, in use
        p.set_theme("Midnight");
        frame(&ctx, &mut p);
        assert!(ctx.global_style().visuals.dark_mode);
        p.apply_name(NameAction::NewTheme("Midnight".into()), "My Night").unwrap();
        assert_eq!(p.scanner.cfg.theme, "My Night");
        let mine = p.custom_themes.iter().position(|t| t.name == "My Night").unwrap();
        p.custom_themes[mine].colors[5] = "#00ff00".into();
        p.themes_changed();
        frame(&ctx, &mut p);
        assert_eq!(crate::look::current(&ctx).accent, crate::look::parse_hex("#00ff00"));
        assert_eq!(realhires::themes::load_custom(None)[mine].colors[5], "#00ff00");
        p.go(Page::Settings);
        p.settings_group = realhires::settings::GROUPS.iter().position(|g| *g == "Appearance").unwrap();
        frame(&ctx, &mut p);
        p.confirmed(ConfirmAction::DeleteTheme("My Night".into()));
        assert_eq!(p.scanner.cfg.theme, realhires::themes::SYSTEM);
        p.set_theme("Light");
        frame(&ctx, &mut p);

        // shuffle and repeat modes go to the queue and the saved state
        p.play_list(album.clone(), Some(&a1), "Paper Lanterns");
        let first = p.now.as_ref().unwrap().token;
        p.set_shuffle("library");
        p.set_repeat("song");
        assert_eq!((p.queue.as_ref().unwrap().shuffle.as_str(), p.queue.as_ref().unwrap().repeat.as_str()), ("library", "song"));
        // the same song again, gaplessly, as a new play of it
        run_until(&ctx, &mut p, "repeat song", |p| p.now.as_ref().is_some_and(|n| n.token != first));
        assert_eq!(p.now.as_ref().unwrap().path, a1);
        p.set_repeat("off");
        p.set_shuffle("off");
        // paused from here on: the song lasts 2 s, and the quality scan below took longer on
        // the Windows runner, so the next song had started before quitting
        p.toggle_play();
        assert!(!p.is_playing());

        // the windows
        p.open_info();
        p.eq_open = true;
        p.state.eq = realhires::eq::EqState::default();
        p.state.eq.enabled = true;
        p.state.eq.apply_preset("Rock");
        p.set_eq();
        frame(&ctx, &mut p);
        let t = p.lib.track(&a1).unwrap().clone();
        let secs = crate::eqwin::info_sections(&t, 2, p.engine.info().as_ref(), &p.state, true, None);
        let flat: Vec<String> = secs.iter().flat_map(|(_, r)| r.iter().map(|(k, v)| format!("{k}={v}"))).collect();
        assert!(flat.contains(&"Sample rate=44.1 kHz".to_string()), "{flat:?}");
        assert!(flat.contains(&"Track=1 of 2".to_string()), "{flat:?}");
        assert!(flat.iter().any(|x| x.starts_with("Equalizer=Graphic: Rock")), "{flat:?}");
        assert!(flat.contains(&"Verdict=Not checked yet".to_string()), "{flat:?}");

        // search
        p.search = "tidewater".into();
        p.search_due = Some(Instant::now());
        frame(&ctx, &mut p);
        assert_eq!(p.page, Page::Search);
        p.search.clear();
        p.search_due = Some(Instant::now());
        frame(&ctx, &mut p);
        assert_ne!(p.page, Page::Search);

        // Settings: the menus that depend on the computer are filled
        p.go(Page::Settings);
        frame(&ctx, &mut p);
        assert!(p.scanner.choices.contains_key("font_family") && p.scanner.choices.contains_key("output_device"));
        p.settings_draft().gapless = false;
        p.settings_due = Some(Instant::now());
        frame(&ctx, &mut p);
        assert!(!p.scanner.cfg.gapless && !realhires::settings::load(None).gapless);

        // Quality check: the 1.x scanner on the library folders
        p.check_library();
        assert_eq!(p.page, Page::Quality);
        run_until(&ctx, &mut p, "the quality scan", |p| !p.scanner.scanning && p.scanner.results.len() == 3);
        p.open_info();
        assert!(p.info.as_ref().unwrap().verdict.is_some());

        // quitting remembers the song; the next start shows it paused there
        p.on_exit();
        drop(p);
        let st = playerstate::load(None);
        assert_eq!(st.last_path, a1);
        assert_eq!((st.eq.preset.as_str(), st.eq.enabled), ("Rock", true));
        let mut again = Player::new(&ctx, realhires::settings::load(None), vec![]);
        frame(&ctx, &mut again);
        assert_eq!(again.now.as_ref().map(|n| n.path.clone()), Some(a1.clone()));
        assert!(!again.is_playing() && again.engine.is_paused());
        assert!(again.state.is_liked(&a1));
        drop(again);
        let _ = std::fs::remove_dir_all(&tmp);
        let _ = b1;
    }

    #[test]
    fn edit_info_and_theme_files() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-edit-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        let a1 = song(&music, "Paper Lanterns", "Lanterns Over Water", "Mira Calloway", "Pop", 1, 44_100, 1.0);
        let a2 = song(&music, "Paper Lanterns", "Paper in the Rain", "Mira Calloway", "Pop", 2, 44_100, 1.0);
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 2 && p.lib_rx.is_none());

        // a theme file put into the themes folder appears in the menu, as it is
        let folder = realhires::themes::folder(None);
        assert_eq!(folder, tmp.join("themes"));
        std::fs::create_dir_all(&folder).unwrap();
        let neon = realhires::themes::normalize(&realhires::json::parse(
            r##"{"name": "Neon", "dark": true, "colors": {"accent": "#39ff14"}}"##).unwrap()).unwrap();
        std::fs::write(folder.join("neon.json"), realhires::themes::export_theme(&neon)).unwrap();
        std::fs::write(folder.join("broken.json"), "{").unwrap();
        assert!(p.reload_file_themes());
        assert!(p.scanner.choices["theme"].contains(&"Neon".to_string()));
        p.set_theme("Neon");
        p.go(Page::Settings);
        frame(&ctx, &mut p);
        assert_eq!(p.theme_file("Neon"), Some(folder.join("neon.json").as_path()));
        assert_eq!(look::current(&ctx).accent, look::parse_hex("#39ff14"));
        assert!(realhires::themes::load_custom(None).is_empty(), "a folder theme is not copied into themes.json");
        // a dropped (or imported) theme file becomes one of the user's own themes
        let sunset = tmp.join("sunset.json");
        let mut t = neon.clone();
        t.name = "Sunset".into();
        std::fs::write(&sunset, realhires::themes::export_theme(&t)).unwrap();
        assert!(crate::app::is_theme_file(&sunset) && !crate::app::is_theme_file(&tmp.join("missing.json")));
        p.import_theme_path(&sunset);
        assert_eq!(p.scanner.cfg.theme, "Sunset");
        assert_eq!(realhires::themes::load_custom(None)[0].name, "Sunset");
        // a second file called Neon gets a free name
        std::fs::write(folder.join("other.json"), realhires::themes::export_theme(&neon)).unwrap();
        p.reload_file_themes();
        assert!(p.scanner.choices["theme"].contains(&"Neon 2".to_string()));

        // Edit Info on one song: the composer and the release date
        p.go(Page::AllSongs);
        p.edit_song_info(&a1);
        run_until(&ctx, &mut p, "the tags", |p| !p.edit_info.as_ref().unwrap().reading());
        assert_eq!(p.edit_info.as_ref().unwrap().text[0], "Lanterns Over Water");
        {
            let cfg = p.scanner.cfg.clone();
            let w = p.edit_info.as_mut().unwrap();
            w.set("composer", "Lena Frost");
            w.set("date", "2004-05-28");
            assert!(w.save(&ctx, &cfg));
        }
        run_until(&ctx, &mut p, "saving", |p| p.edit_info.is_none());
        run_until(&ctx, &mut p, "the library", |p| {
            p.lib_rx.is_none() && p.lib.track(&a1).is_some_and(|t| t.composer == "Lena Frost")
        });
        let tags = realhires::probe::probe(&a1).unwrap().tags;
        assert_eq!(realhires::probe::tag(&tags, "date"), Some("2004-05-28"));
        assert_eq!(realhires::probe::tag(&tags, "title"), Some("Lanterns Over Water"), "an unchanged field stays");

        // a bad track number is refused and nothing is written
        p.edit_song_info(&a2);
        run_until(&ctx, &mut p, "the tags", |p| !p.edit_info.as_ref().unwrap().reading());
        {
            let cfg = p.scanner.cfg.clone();
            let w = p.edit_info.as_mut().unwrap();
            w.set("track", "two");
            assert!(w.save(&ctx, &cfg));
            assert!(w.error.contains("must be a number"), "{}", w.error);
        }
        frame(&ctx, &mut p);
        assert!(p.edit_info.is_some());
        assert_eq!(realhires::probe::tag(&realhires::probe::probe(&a2).unwrap().tags, "track"), Some("2"));
        p.edit_info = None;

        // Edit album info: the album artist and the genre of every song of the album
        let key = p.lib.track(&a1).unwrap().song().album_key();
        p.go(Page::Album(key));
        frame(&ctx, &mut p);
        let album = p.lib.album_of(&a1).unwrap().clone();
        p.edit_album_info(&album);
        assert_eq!(p.edit_info.as_ref().unwrap().subheading, "Mira Calloway · 2 songs");
        run_until(&ctx, &mut p, "the tags", |p| !p.edit_info.as_ref().unwrap().reading());
        {
            let w = p.edit_info.as_ref().unwrap();
            assert_eq!(w.text[2], "Paper Lanterns");
            assert_eq!(w.text[0], "", "the titles differ, so the field starts empty");
            assert!(w.changes().is_empty());
        }
        frame(&ctx, &mut p);
        {
            let cfg = p.scanner.cfg.clone();
            let w = p.edit_info.as_mut().unwrap();
            w.set("genre", " Jazz ");
            w.set("album_artist", "Various Lanterns");
            assert_eq!(
                w.changes(),
                vec![("album_artist".to_string(), "Various Lanterns".to_string()), ("genre".to_string(), " Jazz ".to_string())]
            );
            assert!(w.save(&ctx, &cfg));
        }
        run_until(&ctx, &mut p, "saving the album", |p| p.edit_info.is_none());
        run_until(&ctx, &mut p, "the library", |p| {
            p.lib_rx.is_none()
                && [&a1, &a2].iter().all(|a| p.lib.track(a).is_some_and(|t| t.genre == "Jazz" && t.album_artist == "Various Lanterns"))
        });
        assert_eq!(p.lib.album_of(&a1).unwrap().tracks.len(), 2, "the album stays one album");
        assert_eq!(p.lib.track(&a2).unwrap().title, "Paper in the Rain");
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// The player in Japanese (2.1): every page, every Settings group and the
    /// windows draw Japanese texts.  Only this thread uses Japanese, so the
    /// tests that run at the same time keep English.
    #[test]
    fn japanese_pages_and_windows() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        realhires::i18n::set_thread_language("日本語");
        let tmp = std::env::temp_dir().join(format!("rhr-player-ja-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        // long enough to be logged in the play history (`musiclib::MIN_LOGGED_SECONDS`)
        let a1 = song(&music, "Paper Lanterns", "Lanterns Over Water", "Mira Calloway", "Pop", 1, 44_100, 2.0);
        let a2 = song(&music, "Paper Lanterns", "Paper in the Rain", "Mira Calloway", "Pop", 2, 44_100, 1.5);
        let b1 = song(&music, "Harbour Lights", "Tidewater", "The Northern Quay", "Jazz", 1, 48_000, 1.0);
        // the setting stays English: `set_language()` would switch every thread of the test run
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 3 && p.lib_rx.is_none());
        let has = |text: &str, want: &[&str], what: &str| {
            for w in want {
                assert!(text.contains(w), "{what}: no {w:?} in\n{text}");
            }
        };

        // the start page and the sidebar
        let t = texts(&ctx, &mut p);
        has(&t, &["ライブラリ", "すべての曲", "お気に入りの曲", "新規プレイリスト…", "履歴", "あなたの統計", "設定", "3曲", "アルバム2枚"], "Library");
        for english in ["All Songs", "Recently Played", "Liked Songs", "Settings", "3 songs"] {
            assert!(!t.contains(english), "English {english:?} on the Library page:\n{t}");
        }
        // every page before anything was played
        let music_dir = realhires::paths::abspath(&music.to_string_lossy());
        let key = p.lib.track(&a1).unwrap().song().album_key();
        let pages: Vec<(Page, Vec<&str>)> = vec![
            (Page::AllSongs, vec!["すべての曲", "タイトル", "アーティスト", "音質", "再生", "シャッフル", "ロスレス"]),
            (Page::Folders(None), vec!["Music", "再生"]),
            (Page::Folders(Some(music_dir.clone())), vec!["Paper Lanterns", "2曲"]),
            (Page::Albums, vec!["アルバム", "Harbour Lights"]),
            (Page::Album(key.clone()), vec!["戻る", "アルバムの情報を編集…", "2曲、"]),
            (Page::Artists, vec!["アーティスト", "Mira Calloway"]),
            (Page::AlbumArtists, vec!["アルバムアーティスト"]),
            (Page::Genres, vec!["ジャンル", "Jazz"]),
            (Page::Years, vec!["年", "不明な年"]),
            (Page::Composers, vec!["作曲者", "不明な作曲者"]),
            (Page::Playlists, vec!["プレイリスト", "まだプレイリストがありません"]),
            (Page::Liked, vec!["お気に入りの曲", "お気に入りにした曲がここに表示されます"]),
            (Page::Disliked, vec!["好きじゃない曲", "シャッフルでは再生されなくなります"]),
            (Page::RecentlyPlayed, vec!["最近再生した項目", "再生した曲がここに表示されます。"]),
            (Page::Recent, vec!["最近追加した項目"]),
            (Page::Stats, vec!["あなたの統計", "今年", "この期間にはまだ再生がありません"]),
            (Page::StatsDetails, vec!["詳細", "あなたのライブラリ", "サンプリング周波数", "ビット深度"]),
            (Page::NowPlaying, vec!["再生中の曲はありません", "アルバムか曲を選ぶか、再生ボタンを押してください。"]),
        ];
        for (page, want) in pages {
            p.go(page.clone());
            let t = texts(&ctx, &mut p);
            has(&t, &want, &format!("{page:?}"));
        }

        // playing: the playback bar, Now Playing, Up Next and the play history
        p.go(Page::Album(key.clone()));
        let album = p.page_songs();
        p.play_list(album.clone(), Some(&a1), "Paper Lanterns");
        p.show_up_next = true;
        p.go(Page::NowPlaying);
        let t = texts(&ctx, &mut p);
        has(&t, &["次に再生", "再生元：Paper Lanterns", "Paper in the Rain", "ロスレス"], "Up Next");
        p.show_up_next = false;
        run_until(&ctx, &mut p, "the end of the album", |p| p.now.as_ref().is_some_and(|n| !n.active));
        frame(&ctx, &mut p);
        p.play_list(vec![b1.clone()], None, "All Songs");
        p.show_up_next = true;
        let t = texts(&ctx, &mut p);
        has(&t, &["この曲には歌詞がありません。", "歌詞を取得", "再生元：すべての曲", "この曲の後に再生する曲はありません。"], "Now Playing");
        p.show_up_next = false;
        p.engine.stop();
        for (page, want) in [
            (Page::RecentlyPlayed, vec!["再生日時", "今日 ", "履歴を消去…"]),
            (Page::Stats, vec!["詳細"]),
            (Page::StatsDetails, vec!["合計", "時間帯別の再生時間（分）", "曜日別の再生時間（分）", "月", "トップソング", "回再生"]),
            (Page::Library, vec!["今年は"]),
        ] {
            p.go(page.clone());
            let t = texts(&ctx, &mut p);
            has(&t, &want, &format!("{page:?} after playing"));
        }

        // a playlist with a song whose file is gone
        p.apply_name(NameAction::NewPlaylist(vec![a1.clone(), "/gone/song.flac".to_string()]), "Road Trip").unwrap();
        let pid = p.state.playlists[0].id.clone();
        p.go(Page::Playlist(pid.clone()));
        let t = texts(&ctx, &mut p);
        has(&t, &["Road Trip", "名前を変更…", "削除…", "ライブラリにありません", "2曲、"], "a playlist");

        // search
        p.search = "lanterns".into();
        p.search_due = Some(Instant::now());
        let t = texts(&ctx, &mut p);
        assert_eq!(p.page, Page::Search);
        has(&t, &["「lanterns」の検索結果", "アルバム", "曲"], "Search");
        p.search.clear();
        p.search_due = Some(Instant::now());
        frame(&ctx, &mut p);

        // every Settings group: its name, its rows and its buttons in Japanese
        p.go(Page::Settings);
        for (i, group) in realhires::settings::GROUPS.iter().enumerate() {
            p.settings_group = i;
            let shown = realhires::i18n::tr(group);
            assert_ne!(shown, *group, "the group {group:?} has no Japanese name");
            let t = texts(&ctx, &mut p);
            has(&t, &[shown, "このグループを初期設定に戻す"], group);
            let more: &[&str] = match *group {
                "Library" => &["ライブラリを今すぐ更新", "ライブラリをスキャン", "再生履歴を消去…"],
                "Playback" => &["イコライザ…", "出力デバイスを再読み込み"],
                "Appearance" => &["テーマ", "使用中のフォントは", "テーマフォルダを開く", "新規テーマ…", "内蔵のテーマなので変更できません"],
                "Saved results" => &["保存されたすべての結果を削除…"],
                "Detection" => &["品質チェックを開く"],
                _ => &[],
            };
            has(&t, more, group);
        }

        // the windows: Equalizer (both tabs), Playback info, a name and a confirmation dialog, Edit Info
        p.play_list(vec![a1.clone()], None, "Songs");
        p.engine.pause();
        p.eq_open = true;
        p.eq_tab = 0;
        let t = texts(&ctx, &mut p);
        has(&t, &["イコライザ", "プリセット", "イコライザをオン", "グラフィック", "グラフィックイコライザを使う", "プリアンプ"], "Equalizer");
        p.eq_tab = 1;
        let t = texts(&ctx, &mut p);
        has(&t, &["パラメトリックイコライザを使う", "バンドを追加", "周波数", "ゲイン"], "Equalizer, parametric");
        p.eq_open = false;
        p.open_info();
        let t = texts(&ctx, &mut p);
        has(&t, &["再生情報", "トラック", "デコーダ", "サンプリング周波数", "ヌル出力（音は出ません）", "判定", "まだチェックしていません", "コピー"], "Playback info");
        p.info = None;
        p.ask_name(tr!("New Playlist"), tr!("New Playlist"), NameAction::NewPlaylist(vec![]));
        let t = texts(&ctx, &mut p);
        has(&t, &["新規プレイリスト", "名前", "キャンセル"], "the name dialog");
        assert_eq!(p.name_dialog.as_ref().unwrap().text, "新規プレイリスト", "the proposed name is translated");
        p.name_dialog = None;
        p.confirm = Some(ConfirmAction::DeletePlaylist(pid));
        let t = texts(&ctx, &mut p);
        has(&t, &["プレイリストを削除", "プレイリスト「Road Trip」を削除しますか？", "削除"], "the confirmation");
        p.confirm = None;
        p.edit_song_info(&a2);
        run_until(&ctx, &mut p, "the tags", |p| !p.edit_info.as_ref().unwrap().reading());
        let t = texts(&ctx, &mut p);
        has(&t, &["情報を編集", "タイトル", "保存", "キャンセル"], "Edit Info");
        p.edit_info = None;
        frame(&ctx, &mut p);
        p.engine.stop();
        drop(p);
        realhires::i18n::set_thread_language("");
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
