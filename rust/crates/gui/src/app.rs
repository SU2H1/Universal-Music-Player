//! The main window: sources, file list or album/artist cards, filters, find
//! bar, dashboard and status bar (the counterpart of `realhires/gui.py`).

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, Sender};
use std::sync::Arc;
use std::time::Instant;

use egui::{
    Align, Align2, Color32, FontId, Key, KeyboardShortcut, Layout, Modifiers, Pos2, Rect, RichText, Sense, Stroke, Ui,
    Vec2,
};
use egui_extras::{Column, TableBuilder};
use realhires::analysis::imap_get;
use realhires::eta::ScanEta;
use realhires::fix::{fix_advice, fix_outlook, plan_fixes, FIXABLE_VERDICTS};
use realhires::i18n::{self, tr_name, tr_text};
use realhires::library::{group_results, sort_groups, Group, GroupMode};
use realhires::paths::{abspath, file_name, parent};
use realhires::pyfmt::{f, fs, g};
use realhires::settings::{self, Settings};
use realhires::store::{open_store, settings_key, ResultStore};
use realhires::verdict::{saved_note, FileResult, Verdict, VERDICT_ORDER};
use realhires::{n_, tr};

use crate::dialogs::{Confirm, ConfirmAction, FixDoneWindow, FixWindow, SettingsWindow, SummaryWindow};
use crate::lyrics::{lyrics_job, LyricsWindow};
use crate::plots::{self, SpectroTexture, State};
use crate::style::*;
use crate::work::{self, GraphWorker, LookupRequest, Msg, PlotCache, Post, ScanJob};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum View {
    Files,
    Album,
    Artist,
}

impl View {
    pub fn from_setting(s: &str) -> View {
        match s {
            "By album" => View::Album,
            "By artist" => View::Artist,
            _ => View::Files,
        }
    }
    /// The setting value (also a key); show it with `i18n::tr()`.
    pub fn label(self) -> &'static str {
        match self {
            View::Files => n_!("Files"),
            View::Album => n_!("By album"),
            View::Artist => n_!("By artist"),
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Col {
    Verdict,
    Why,
    Claimed,
    Estimated,
    Bandwidth,
    Fix,
    Name,
    Folder,
    Track,
}

/// The dashboard's tabs.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum PlotTab {
    Spectrogram,
    Spectrum,
    Lyrics,
}

const COLUMNS: [(Col, &str); 8] = [
    (Col::Verdict, n_!("Verdict")),
    (Col::Why, n_!("Why")),
    (Col::Claimed, n_!("Claimed")),
    (Col::Estimated, n_!("Real (est.)")),
    (Col::Bandwidth, n_!("Bandwidth")),
    (Col::Fix, n_!("After fix")),
    (Col::Name, n_!("File")),
    (Col::Folder, n_!("Folder")),
];

pub struct GroupFilter {
    pub mode: GroupMode,
    /// the album or artist, its artist (albums) and its counts, for the chip
    pub title: String,
    pub subtitle: String,
    pub problems: usize,
    pub total: usize,
    pub paths: HashSet<String>,
    pub order: Vec<String>,
    pub back: View,
    pub filters: [bool; 7],
}

impl GroupFilter {
    /// "Album: Title — Artist  (2 of 12 fake/suspicious)", in the language in use.
    pub fn label(&self) -> String {
        let head = if self.mode == GroupMode::Album {
            tr!("Album: {title} — {artist}", title = tr_name(&self.title), artist = tr_name(&self.subtitle))
        } else {
            tr!("Artist: {name}", name = tr_name(&self.title))
        };
        format!(
            "{head}  {}",
            tr!("({n} of {total} fake/suspicious)", n = self.problems, total = self.total)
        )
    }
}

pub struct SourceRow {
    pub path: String,
    pub is_dir: bool,
}

pub struct App {
    pub cfg: Settings,
    pub post: Post,
    ctx: egui::Context,
    rx: Receiver<Msg>,
    pub sources: Vec<SourceRow>,
    pub source_files: HashMap<String, Vec<String>>,
    listing: HashMap<String, Arc<AtomicBool>>,
    source_sel: BTreeSet<String>,
    pub results: HashMap<String, FileResult>,
    cache: PlotCache,
    pub store: Option<ResultStore>,
    lookup: Sender<LookupRequest>,
    lookup_gen: u64,
    lookup_busy: usize,
    graphs: GraphWorker,
    graph_failed: HashSet<String>,
    covers: Sender<(String, Vec<String>)>,
    cover_tex: HashMap<String, Option<egui::TextureHandle>>,
    // scanning
    scan: Option<ScanJob>,
    scan_seq: u64,
    pub scanning: bool,
    total: usize,
    done: usize,
    scan_order: Vec<String>,
    scan_ptr: usize,
    scan_workers: usize,
    scan_when_listed: bool,
    eta: Option<ScanEta>,
    scan_started: Instant,
    pub last_elapsed: Option<f64>,
    last_scan_files: Vec<String>,
    quick: bool,
    summary_due: Option<Instant>,
    fix_due: Option<Instant>,
    // the list
    pub show: [bool; 7],
    search: String,
    sort_col: Col,
    sort_rev: bool,
    rows: Vec<String>,
    rows_dirty: bool,
    selection: BTreeSet<String>,
    anchor: Option<usize>,
    current: Option<String>,
    scroll_to: Option<usize>,
    pub view: View,
    groups: Vec<Group>,
    groups_dirty: bool,
    group_filter: Option<GroupFilter>,
    card_current: Option<String>,
    // find
    find_open: bool,
    find_text: String,
    find_matches: Vec<String>,
    find_index: Option<usize>,
    find_focus: bool,
    // dashboard
    pub tab: PlotTab,
    /// the Lyrics tab's text for a path, read again when lyrics are saved
    lyrics_shown: Option<(Option<String>, egui::text::LayoutJob)>,
    spectro: SpectroTexture,
    shown_plot: Option<(String, Option<realhires::analysis::Plot>)>,
    // windows
    pub status: String,
    pub summary: Option<SummaryWindow>,
    pub settings_win: Option<SettingsWindow>,
    pub fix_win: Option<FixWindow>,
    pub fix_done: Option<FixDoneWindow>,
    /// one at a time: LRCLIB wants sequential requests
    pub lyrics_win: Option<LyricsWindow>,
    pub confirm: Option<Confirm>,
    pub info: Option<(String, String)>,
    about: bool,
    pub quit: bool,
    // lyrics while scanning (2.0)
    scan_lyrics: Option<work::ScanLyrics>,
    scan_lyrics_seq: u64,
    /// songs the worker saved lyrics for, found none for, and still has to do
    pub scan_lyrics_counts: (usize, usize, usize),
    last_stopped: bool,
    /// menus of the Settings window that depend on the computer (fonts, devices)
    pub choices: HashMap<&'static str, Vec<String>>,
    /// counts lyrics saved by any worker, so the player reads them again
    pub lyrics_gen: u64,
    /// the next scan ends without the summary and without fixing (2.0)
    pub quiet_scan: bool,
}

fn index_of(v: Verdict) -> usize {
    VERDICT_ORDER.iter().position(|x| *x == v).unwrap()
}

impl App {
    pub fn new(ctx: &egui::Context, cfg: Settings, initial: Vec<String>) -> App {
        let (post, rx) = Post::new(ctx.clone());
        let lookup = work::start_lookup(post.clone());
        let covers = work::start_covers(post.clone());
        let graphs = GraphWorker::start(cfg.clone(), post.clone());
        let show = VERDICT_ORDER.map(|v| cfg.shown_verdicts.iter().any(|s| s == v.as_str()));
        let mut app = App {
            ctx: ctx.clone(),
            store: open_store(&cfg),
            view: View::from_setting(&cfg.view_mode),
            cfg,
            post,
            rx,
            sources: vec![],
            source_files: HashMap::new(),
            listing: HashMap::new(),
            source_sel: BTreeSet::new(),
            results: HashMap::new(),
            cache: PlotCache::new(),
            lookup,
            lookup_gen: 0,
            lookup_busy: 0,
            graphs,
            graph_failed: HashSet::new(),
            covers,
            cover_tex: HashMap::new(),
            scan: None,
            scan_seq: 0,
            scanning: false,
            total: 0,
            done: 0,
            scan_order: vec![],
            scan_ptr: 0,
            scan_workers: 1,
            scan_when_listed: false,
            eta: None,
            scan_started: Instant::now(),
            last_elapsed: None,
            last_scan_files: vec![],
            quick: false,
            summary_due: None,
            fix_due: None,
            show,
            search: String::new(),
            sort_col: Col::Verdict,
            sort_rev: false,
            rows: vec![],
            rows_dirty: true,
            selection: BTreeSet::new(),
            anchor: None,
            current: None,
            scroll_to: None,
            groups: vec![],
            groups_dirty: true,
            group_filter: None,
            card_current: None,
            find_open: false,
            find_text: String::new(),
            find_matches: vec![],
            find_index: None,
            find_focus: false,
            tab: PlotTab::Spectrogram,
            lyrics_shown: None,
            spectro: SpectroTexture::default(),
            shown_plot: None,
            status: tr!("Add a folder or files, then press Scan.").into(),
            summary: None,
            settings_win: None,
            fix_win: None,
            fix_done: None,
            lyrics_win: None,
            confirm: None,
            info: None,
            about: false,
            quit: false,
            scan_lyrics: None,
            scan_lyrics_seq: 0,
            scan_lyrics_counts: (0, 0, 0),
            last_stopped: false,
            choices: HashMap::new(),
            lyrics_gen: 0,
            quiet_scan: false,
        };
        let start = !initial.is_empty();
        for p in initial {
            app.add_source(&p);
        }
        if start {
            app.scan_when_listed = true;
        }
        app
    }

    // ------------------------------------------------------------ sources ---
    pub fn all_files(&self) -> Vec<String> {
        let mut seen = HashSet::new();
        let mut out = vec![];
        for s in &self.sources {
            for f in self.source_files.get(&s.path).into_iter().flatten() {
                if seen.insert(f.as_str()) {
                    out.push(f.clone());
                }
            }
        }
        out
    }

    pub fn add_source(&mut self, path: &str) {
        if path.is_empty() {
            return;
        }
        let path = abspath(path);
        if self.sources.iter().any(|s| s.path == path) {
            self.status = tr!("Already added: {path}", path = path);
            self.source_sel = [path].into();
            return;
        }
        let is_dir = std::path::Path::new(&path).is_dir();
        self.sources.push(SourceRow { path: path.clone(), is_dir });
        let cancel = Arc::new(AtomicBool::new(false));
        self.listing.insert(path.clone(), cancel.clone());
        self.source_files.insert(path.clone(), vec![]);
        self.status = tr!("Looking for audio files in {path} …", path = path);
        work::list_source(path, &self.cfg, self.post.clone(), cancel);
    }

    fn on_listing(&mut self, source: String, batch: Vec<String>) {
        if !self.listing.contains_key(&source) {
            return;
        }
        let known: HashSet<String> = self.all_files().into_iter().collect();
        let new: Vec<String> = batch.iter().filter(|p| !known.contains(*p)).cloned().collect();
        self.source_files.entry(source).or_default().extend(batch);
        self.request_lookup(new);
        self.rows_dirty = true;
        self.status = tr!("Looking for audio files… {n} found so far", n = self.all_files().len());
    }

    fn on_listed(&mut self, source: String) {
        self.listing.remove(&source);
        if !self.sources.iter().any(|s| s.path == source) {
            return;
        }
        self.rows_dirty = true;
        if self.listing.is_empty() {
            if !self.scanning {
                self.status = self.ready_message();
            }
            if self.scan_when_listed {
                self.scan_when_listed = false;
                self.start_scan(None, false);
            }
        }
    }

    pub fn ready_message(&self) -> String {
        let files = self.all_files();
        let total = files.len();
        let empty = self.sources.iter().filter(|s| self.source_files.get(&s.path).is_none_or(|v| v.is_empty())).count();
        let saved = files.iter().filter(|f| self.results.contains_key(*f)).count();
        let mut msg = if self.lookup_busy > 0 {
            if total == 1 {
                tr!("1 audio file found; looking up saved results ({saved} so far)…", saved = saved)
            } else {
                tr!("{n} audio files found; looking up saved results ({saved} so far)…", n = total, saved = saved)
            }
        } else if saved > 0 {
            let rest = total - saved;
            let next = if rest > 0 {
                tr!("Press Scan to analyse the other {n}.", n = rest)
            } else {
                tr!("Nothing new to scan.").to_string()
            };
            let sentence = tr!(
                "{saved} of {total} files were checked before and have not changed: their saved results are shown.",
                saved = saved,
                total = total
            );
            tr!("{sentence} {next}", sentence = sentence, next = next)
        } else {
            let files = count(total, n_!("1 audio file"), n_!("{n} audio files"));
            if self.sources.len() == 1 {
                tr!("{files} ready in 1 source. Press Scan.", files = files)
            } else {
                tr!("{files} ready in {n} sources. Press Scan.", files = files, n = self.sources.len())
            }
        };
        if empty > 0 {
            msg += "  ";
            msg += &tr!("({n} source(s) contain no audio files.)", n = empty);
        }
        msg
    }

    fn wanted(&self) -> Option<f64> {
        if self.quick {
            Some(self.cfg.quick_scan_seconds)
        } else {
            Some(self.cfg.max_seconds).filter(|s| *s > 0.0)
        }
    }

    fn request_lookup(&mut self, paths: Vec<String>) {
        if !paths.is_empty() && self.cfg.save_results {
            self.lookup_busy += 1;
            let _ = self.lookup.send(LookupRequest {
                gen: self.lookup_gen,
                cfg: self.cfg.clone(),
                wanted: self.wanted(),
                paths,
            });
        }
    }

    pub fn remove_sources(&mut self) {
        if self.scanning {
            self.status = tr!("Stop the scan before removing sources.").into();
            return;
        }
        if self.source_sel.is_empty() {
            self.status = tr!("Select a source in the “Sources to scan” list to remove it.").into();
            return;
        }
        let sel = std::mem::take(&mut self.source_sel);
        for s in &sel {
            self.sources.retain(|x| &x.path != s);
            self.source_files.remove(s);
            if let Some(c) = self.listing.remove(s) {
                c.store(true, Ordering::Relaxed);
            }
        }
        let keep: HashSet<String> = self.all_files().into_iter().collect();
        self.results.retain(|p, _| keep.contains(p));
        self.selection.retain(|p| keep.contains(p));
        self.rows_dirty = true;
        self.status = if keep.len() == 1 {
            tr!("Removed {n} source(s). 1 audio file left to scan.", n = sel.len())
        } else {
            tr!("Removed {n} source(s). {files} audio files left to scan.", n = sel.len(), files = keep.len())
        };
    }

    pub fn clear(&mut self) {
        if self.scanning {
            return;
        }
        self.sources.clear();
        self.source_files.clear();
        for c in self.listing.values() {
            c.store(true, Ordering::Relaxed);
        }
        self.listing.clear();
        self.results.clear();
        self.cache.clear();
        self.selection.clear();
        self.current = None;
        self.lookup_gen += 1;
        self.close_group(false);
        self.rows_dirty = true;
        self.status = tr!("Add a folder or files, then press Scan.").into();
    }

    pub fn add_folder_dialog(&mut self) {
        if let Some(d) = rfd::FileDialog::new().set_title(tr!("Choose a music folder")).pick_folder() {
            self.add_source(&d.to_string_lossy());
        }
    }

    pub fn add_files_dialog(&mut self) {
        let exts: Vec<String> =
            self.cfg.extension_set().iter().map(|e| e.trim_start_matches('.').to_string()).collect();
        if let Some(files) = rfd::FileDialog::new()
            .set_title(tr!("Choose audio files"))
            .add_filter(tr!("Audio files"), &exts)
            .add_filter(tr!("All files"), &["*"])
            .pick_files()
        {
            for f in files {
                self.add_source(&f.to_string_lossy());
            }
        }
    }

    // ----------------------------------------------------------- scanning ---
    pub fn start_scan(&mut self, files: Option<Vec<String>>, force: bool) {
        if self.scanning {
            return;
        }
        if self.sources.is_empty() {
            self.info = Some(("Real-Hi-Res".into(), tr!("Add a folder or some files first.").into()));
            return;
        }
        if !self.listing.is_empty() {
            self.scan_when_listed = true;
            self.status = tr!("Still looking for audio files… the scan starts as soon as the list is complete.").into();
            return;
        }
        let subset = files.is_some();
        let files = files.unwrap_or_else(|| self.all_files());
        self.last_scan_files = files.clone();
        if files.is_empty() {
            self.info = Some((
                "Real-Hi-Res".into(),
                tr!("The added folders/files contain no audio files.\n\nCheck the file extensions under Settings → Scanning.")
                    .into(),
            ));
            return;
        }
        if let Err(e) = realhires::probe::require_tools() {
            self.info = Some((tr!("FFmpeg not found").into(), tr_text(&e)));
            return;
        }
        if subset {
            for f in &files {
                self.results.remove(f);
            }
        } else {
            self.results.clear();
            self.cache.clear();
        }
        self.lookup_gen += 1;
        self.stop_scan_lyrics();
        self.scan_lyrics_counts = (0, 0, 0);
        self.total = files.len();
        self.done = 0;
        self.scan_order.clear();
        self.scan_ptr = 0;
        self.eta = None;
        let jobs = realhires::effective_jobs(self.cfg.jobs);
        self.scan_workers = jobs.min(files.len());
        self.scanning = true;
        self.summary_due = None;
        self.fix_due = None;
        self.rows_dirty = true;
        self.refresh();
        self.scan_started = Instant::now();
        self.status = tr!("Scanning {n} files…", n = files.len());
        self.scan_seq += 1;
        let max_seconds = self.wanted();
        self.scan =
            Some(work::start_scan(self.scan_seq, files, self.cfg.clone(), max_seconds, force, jobs, self.post.clone()));
    }

    pub fn stop_scan(&mut self) {
        if let Some(s) = &self.scan {
            s.stop.store(true, Ordering::Relaxed);
            self.status = tr!("Stopping after the files already in progress…").into();
        }
        self.stop_scan_lyrics();
    }

    /// The scan's lyrics worker stops after the song it is doing.
    fn stop_scan_lyrics(&mut self) {
        if let Some(w) = self.scan_lyrics.take() {
            w.stop.store(true, Ordering::SeqCst);
        }
        self.scan_lyrics_counts.2 = 0;
    }

    /// Lyrics while scanning: hand a scanned song to the lyrics worker.
    fn feed_scan_lyrics(&mut self, r: &FileResult) {
        if !self.cfg.lyrics_while_scanning || r.verdict == Verdict::Error {
            return;
        }
        // LRCLIB wants one request at a time: never next to the lyrics window
        if self.lyrics_win.as_ref().is_some_and(|w| w.running) {
            return;
        }
        if self.scan_lyrics.is_none() {
            self.scan_lyrics_seq += 1;
            self.scan_lyrics = Some(work::start_scan_lyrics(self.cfg.clone(), self.scan_lyrics_seq, self.post.clone()));
        }
        let mut light = r.clone();
        light.plot = None;
        light.spectrum = None;
        if let Some(w) = &self.scan_lyrics {
            if w.tx.send((r.path.clone(), Some(light))).is_ok() {
                self.scan_lyrics_counts.2 += 1;
            }
        }
    }

    /// Whether the scan's lyrics worker still has songs to do.
    pub fn scan_lyrics_busy(&self) -> bool {
        self.scan_lyrics.is_some() && self.scan_lyrics_counts.2 > 0
    }

    fn lyrics_status(&self) -> String {
        let (saved, missing, left) = self.scan_lyrics_counts;
        if saved + missing + left == 0 {
            return String::new();
        }
        let mut s = String::from("  ·  ");
        s += &tr!("Lyrics: {saved} saved, {not_found} not found", saved = saved, not_found = missing);
        if left > 0 && self.scan_lyrics.is_some() {
            s += &tr!(", {left} to go", left = left);
        }
        s
    }

    fn finish(&mut self, stopped: bool) {
        self.last_stopped = stopped;
        self.scanning = false;
        self.scan = None;
        self.last_elapsed = Some(self.scan_started.elapsed().as_secs_f64());
        self.rows_dirty = true;
        self.refresh();
        self.update_status(true, stopped);
        if self.selection.is_empty() {
            if let Some(first) = self.rows.first().cloned() {
                self.select_only(&first);
            }
        }
        // the player's background scan at start-up (2.0) neither pops up nor fixes
        let quiet = std::mem::take(&mut self.quiet_scan);
        if !self.results.is_empty() && self.cfg.show_summary_popup && !quiet {
            self.summary_due = Some(Instant::now());
        }
        if !stopped && !quiet {
            self.fix_due = Some(Instant::now());
        }
    }

    fn fix_after_scan(&mut self) {
        let mode = self.cfg.fix_after_scan.clone();
        if mode == "Do nothing" || self.scanning {
            return;
        }
        let scanned: Vec<FileResult> =
            self.last_scan_files.iter().filter_map(|f| self.results.get(f).cloned()).collect();
        let (plans, skipped) = plan_fixes(&scanned, &self.cfg);
        if plans.is_empty() {
            return;
        }
        let fakes: Vec<FileResult> = scanned.into_iter().filter(|r| FIXABLE_VERDICTS.contains(&r.verdict)).collect();
        if mode.starts_with("Ask") {
            let extra = if skipped.is_empty() {
                String::new()
            } else {
                format!(
                    "\n\n{}",
                    tr!("{n} other fake file(s) cannot be changed; the Fix window says why.", n = skipped.len())
                )
            };
            self.confirm = Some(Confirm {
                title: tr!("Fix to real resolution").into(),
                text: format!(
                    "{}{extra}\n\n{}",
                    tr!(
                        "{n} fake file(s) can be fixed to the resolution they really have. Fix them now?",
                        n = plans.len()
                    ),
                    tr!("(Settings → Fixing → After a scan)")
                ),
                action: ConfirmAction::FixAfterScan(fakes),
            });
        } else {
            self.open_fix(fakes, true);
        }
    }

    pub fn open_fix(&mut self, results: Vec<FileResult>, autostart: bool) {
        if results.is_empty() {
            self.status = tr!("Select one or more fake files to fix.").into();
            return;
        }
        self.fix_win = Some(FixWindow::new(&self.cfg, results, autostart));
    }

    fn mark_running(&mut self) -> HashSet<String> {
        while self.scan_ptr < self.scan_order.len() && self.results.contains_key(&self.scan_order[self.scan_ptr]) {
            self.scan_ptr += 1;
        }
        let mut out = HashSet::new();
        for p in &self.scan_order[self.scan_ptr..] {
            if out.len() >= self.scan_workers {
                break;
            }
            if !self.results.contains_key(p) {
                out.insert(p.clone());
            }
        }
        out
    }

    fn counts(&self) -> [usize; 7] {
        let mut c = [0; 7];
        for r in self.results.values() {
            c[index_of(r.verdict)] += 1;
        }
        c
    }

    pub fn update_status(&mut self, fin: bool, stopped: bool) {
        let c = self.counts();
        let fakes = c[0] + c[1];
        let n = self.results.len().max(1);
        let tail = tr!(
            "{fakes} fake ({pct}%), {sus} suspicious, {real} real hi-res",
            fakes = fakes,
            pct = f(100.0 * fakes as f64 / n as f64, 0),
            sus = c[2],
            real = c[3]
        );
        let reused = self.results.values().filter(|r| r.saved.is_some()).count();
        if fin {
            let n = self.results.len();
            let head = if stopped {
                tr!("Stopped")
            } else if self.last_elapsed.is_some() {
                tr!("Finished")
            } else {
                tr!("Saved results")
            };
            let head = if reused > 0 && self.last_elapsed.is_some() {
                tr!("{head}: {n} files ({reused} unchanged, from saved results)", head = head, n = n, reused = reused)
            } else {
                tr!("{head}: {n} files", head = head, n = n)
            };
            self.status = format!("{head}  ·  {tail}{}", self.lyrics_status());
        } else {
            let mut busy = if !self.scan_order.is_empty() {
                tr!("analysing {n} ({workers} at a time)", n = self.scan_order.len(), workers = self.scan_workers)
            } else {
                tr!("looking up saved results").into()
            };
            if let Some(e) = &self.eta {
                busy = tr!("{busy}, {time_left}", busy = busy, time_left = tr_text(&e.text()));
            }
            self.status = format!(
                "{}  ·  {tail}{}",
                tr!(
                    "Scanning: {done}/{total} files, {reused} from saved results, {busy}",
                    done = self.done,
                    total = self.total,
                    reused = reused,
                    busy = busy
                ),
                self.lyrics_status()
            );
        }
    }

    // ------------------------------------------------------------ messages ---
    fn poll(&mut self) {
        let mut dirty = false;
        for _ in 0..2000 {
            let Ok(m) = self.rx.try_recv() else { break };
            match m {
                Msg::Listing { source, batch } => self.on_listing(source, batch),
                Msg::Listed { source } => self.on_listed(source),
                Msg::Saved { gen, results } => {
                    if gen == self.lookup_gen && !self.scanning {
                        let listed: HashSet<String> = self.all_files().into_iter().collect();
                        for r in results {
                            if listed.contains(&r.path) && !self.results.contains_key(&r.path) {
                                self.results.insert(r.path.clone(), r);
                                dirty = true;
                            }
                        }
                    }
                }
                Msg::SavedDone { gen } => {
                    self.lookup_busy = self.lookup_busy.saturating_sub(1);
                    if gen == self.lookup_gen && self.lookup_busy == 0 && self.listing.is_empty() && !self.scanning {
                        self.rows_dirty = true;
                        self.refresh();
                        self.status = self.ready_message();
                        if !self.results.is_empty() && self.selection.is_empty() {
                            if let Some(first) = self.rows.first().cloned() {
                                self.select_only(&first);
                            }
                        }
                    }
                }
                Msg::ScanTotal { scan, total } if scan == self.scan_seq => {
                    self.total = total;
                }
                Msg::Analysing { scan, todo, sizes } if scan == self.scan_seq => {
                    self.scan_workers = realhires::effective_jobs(self.cfg.jobs).min(todo.len());
                    self.scan_order = todo;
                    self.scan_ptr = 0;
                    self.eta = Some(ScanEta::new(sizes));
                }
                Msg::Result { scan, result } if scan == self.scan_seq => {
                    let mut r = *result;
                    self.done += 1;
                    if r.saved.is_none() {
                        if let Some(e) = self.eta.as_mut() {
                            e.add(&r.path);
                        }
                    }
                    if let Some(p) = r.plot.take() {
                        self.cache.put(&r.path, &p);
                    }
                    if self.shown_plot.as_ref().is_some_and(|(p, _)| *p == r.path) {
                        self.shown_plot = None;
                    }
                    self.feed_scan_lyrics(&r);
                    self.results.insert(r.path.clone(), r);
                    dirty = true;
                }
                Msg::ScanDone { scan, stopped } if scan == self.scan_seq => self.finish(stopped),
                Msg::Cover { key, png } => {
                    let tex = png.and_then(|data| image::load_from_memory(&data).ok()).map(|img| {
                        let rgba = img.to_rgba8();
                        let ci = egui::ColorImage::from_rgba_unmultiplied(
                            [rgba.width() as usize, rgba.height() as usize],
                            rgba.as_raw(),
                        );
                        self.post_ctx().load_texture(format!("cover {key}"), ci, egui::TextureOptions::LINEAR)
                    });
                    self.cover_tex.insert(key, tex);
                }
                Msg::Graphs { path, plot } => {
                    self.graphs.busy = None;
                    match plot {
                        Some(p) => {
                            self.cache.put(&path, &p);
                            if let Some(st) = &self.store {
                                st.put_plot(&path, &settings_key(&self.cfg), &p);
                            }
                        }
                        None => {
                            self.graph_failed.insert(path.clone());
                        }
                    }
                    self.shown_plot = None;
                }
                Msg::FixStatus(s) => {
                    if let Some(w) = self.fix_win.as_mut() {
                        w.status = tr_text(&s); // also prepare_gapless()'s progress
                    }
                }
                Msg::FixStart(p) => {
                    if let Some(w) = self.fix_win.as_mut() {
                        w.set_status(&p, tr!("Fixing…").into(), None);
                    }
                }
                Msg::FixDone(o) => {
                    if o.ok {
                        self.add_fixed_result(&o);
                    }
                    if let Some(w) = self.fix_win.as_mut() {
                        w.done(*o);
                    }
                }
                Msg::FixFinished => {
                    if let Some(w) = self.fix_win.as_mut() {
                        w.finished();
                        self.status = tr!("Fix: {status}", status = &w.status);
                        // a pop-up says it is done; it stays when the Fix window is closed
                        self.fix_done = Some(FixDoneWindow::new(&w.outcomes, w.skipped));
                        if w.hidden {
                            self.fix_win = None;
                        }
                    }
                    self.rows_dirty = true;
                }
                Msg::LyricsStart { run, path } => {
                    if let Some(w) = self.lyrics_win.as_mut().filter(|w| w.run == run) {
                        w.started(&path);
                    }
                }
                Msg::LyricsDone { run, outcome } => {
                    if outcome.saved() {
                        self.lyrics_saved(&outcome.path);
                    }
                    if let Some(w) = self.lyrics_win.as_mut().filter(|w| w.run == run) {
                        w.done(*outcome);
                    }
                }
                Msg::LyricsFinished { run, stopped } => {
                    if let Some(w) = self.lyrics_win.as_mut().filter(|w| w.run == run) {
                        w.finished(stopped);
                    }
                }
                Msg::ScanLyrics { worker, outcome } => {
                    if self.scan_lyrics.as_ref().is_some_and(|w| w.id == worker) {
                        self.scan_lyrics_counts.2 = self.scan_lyrics_counts.2.saturating_sub(1);
                        if outcome.saved() {
                            self.scan_lyrics_counts.0 += 1;
                        } else if outcome.status == realhires::lyrics::Status::NotFound {
                            self.scan_lyrics_counts.1 += 1;
                        }
                        if outcome.saved() {
                            self.lyrics_saved(&outcome.path);
                        }
                        let stopped = self.last_stopped;
                        self.update_status(!self.scanning, stopped);
                    }
                }
                _ => {}
            }
        }
        if dirty {
            self.groups_dirty = true;
            if self.scanning {
                self.update_status(false, false);
            } else {
                self.rows_dirty = true;
            }
        }
    }

    fn post_ctx(&self) -> egui::Context {
        self.ctx.clone()
    }

    /// A fixed file replaces the fake one in the list (or joins it as a copy).
    pub fn add_fixed_result(&mut self, o: &realhires::fix::FixOutcome) {
        let Some(new) = o.new_result.clone() else { return };
        let Some(out) = o.output.clone() else { return };
        if let Some(st) = self.store.as_mut() {
            st.put(&new, &settings_key(&self.cfg), None, None);
        }
        let src = o.plan.source.clone();
        if o.plan.replace_original {
            for files in self.source_files.values_mut() {
                for f in files.iter_mut() {
                    if *f == src {
                        *f = out.clone();
                    }
                }
            }
            self.results.remove(&src);
        } else {
            let folder = self.sources.iter().find(|s| s.is_dir && out.starts_with(&s.path)).map(|s| s.path.clone());
            match folder {
                Some(sf) => self.source_files.entry(sf).or_default().push(out.clone()),
                None => {
                    self.sources.push(SourceRow { path: out.clone(), is_dir: false });
                    self.source_files.insert(out.clone(), vec![out.clone()]);
                }
            }
        }
        self.results.insert(out, new);
        self.rows_dirty = true;
        self.groups_dirty = true;
    }

    // ---------------------------------------------------------- the list ---
    fn in_group(&self, path: &str) -> bool {
        self.group_filter.as_ref().is_none_or(|g| g.paths.contains(path))
    }

    fn matches(&self, r: &FileResult, q: &str) -> bool {
        self.in_group(&r.path)
            && self.show[index_of(r.verdict)]
            && (q.is_empty()
                || r.path.to_lowercase().contains(q)
                || r.summary.to_lowercase().contains(q)
                || (i18n::language() != "en" && tr_text(&r.summary).to_lowercase().contains(q)))
    }

    fn sort_key(&self, r: &FileResult) -> (i64, i64, String, String) {
        let p = r.path.to_lowercase();
        match self.sort_col {
            Col::Verdict => (index_of(r.verdict) as i64, 0, p, String::new()),
            Col::Bandwidth => ((r.bandwidth_hz.unwrap_or(0.0) * 1000.0) as i64, 0, p, String::new()),
            Col::Fix => (0, 0, tr_text(&fix_outlook(r, &self.cfg)).to_lowercase(), p),
            Col::Claimed => (r.sample_rate as i64, r.claimed_bits.unwrap_or(0) as i64, p, String::new()),
            Col::Estimated => (r.true_rate.unwrap_or(0) as i64, r.effective_bits.unwrap_or(0) as i64, p, String::new()),
            Col::Name => (0, 0, file_name(&r.path).to_lowercase(), String::new()),
            Col::Why => (0, 0, tr_text(&r.summary).to_lowercase(), p),
            Col::Folder => (0, 0, parent(&r.path).to_lowercase(), p),
            Col::Track => {
                let order = self.group_filter.as_ref().map(|g| &g.order);
                let i = order.and_then(|o| o.iter().position(|x| *x == r.path)).unwrap_or(usize::MAX / 2);
                (i as i64, 0, p, String::new())
            }
        }
    }

    /// Rebuild the visible rows: matching results (sorted), then every listed
    /// file that has no result yet, so the user sees what was added.  While a
    /// scan runs, rows stay in place and update; filters apply at the end.
    pub fn refresh(&mut self) {
        if !self.rows_dirty {
            return;
        }
        self.rows_dirty = false;
        let q = self.search.trim().to_lowercase();
        if self.scanning && !self.rows.is_empty() {
            let have: HashSet<String> = self.rows.iter().cloned().collect();
            for p in self.all_files() {
                if !have.contains(&p) && self.in_group(&p) && (q.is_empty() || p.to_lowercase().contains(&q)) {
                    self.rows.push(p);
                }
            }
            return;
        }
        let mut res: Vec<&FileResult> = self.results.values().filter(|r| self.matches(r, &q)).collect();
        res.sort_by_cached_key(|r| self.sort_key(r));
        if self.sort_rev {
            res.reverse();
        }
        let mut rows: Vec<String> = res.iter().map(|r| r.path.clone()).collect();
        let shown: HashSet<String> = rows.iter().cloned().collect();
        for p in self.all_files() {
            if !self.results.contains_key(&p)
                && !shown.contains(&p)
                && (q.is_empty() || p.to_lowercase().contains(&q))
                && self.in_group(&p)
            {
                rows.push(p);
            }
        }
        self.rows = rows;
        let visible: HashSet<&String> = self.rows.iter().collect();
        self.selection.retain(|p| visible.contains(p));
        self.groups_dirty = true;
        if self.find_open {
            self.update_find(false);
        }
    }

    pub fn set_filters(&mut self, verdicts: &[Verdict]) {
        self.show = VERDICT_ORDER.map(|v| verdicts.contains(&v));
        self.rows_dirty = true;
    }

    fn select_only(&mut self, path: &str) {
        self.selection = [path.to_string()].into();
        self.current = Some(path.to_string());
        self.anchor = self.rows.iter().position(|p| p == path);
    }

    pub fn select_all(&mut self) {
        self.selection = self.rows.iter().cloned().collect();
        self.status = tr!("Selected all {n} listed files.", n = self.rows.len());
    }

    fn selected_results(&self) -> Vec<FileResult> {
        self.rows.iter().filter(|p| self.selection.contains(*p)).filter_map(|p| self.results.get(p).cloned()).collect()
    }

    /// The lyrics next to `path` were saved: show them if that song is selected.
    pub fn lyrics_saved(&mut self, path: &str) {
        self.lyrics_gen += 1;
        if self.lyrics_shown.as_ref().is_some_and(|(p, _)| p.as_deref() == Some(path)) {
            self.lyrics_shown = None;
        }
    }

    pub fn download_lyrics(&mut self, paths: Vec<String>) {
        if self.lyrics_win.as_ref().is_some_and(|w| w.running) {
            self.status = tr!("Lyrics are being downloaded already; wait for them or press Stop first.").into();
            return;
        }
        if self.scan_lyrics_busy() {
            self.status =
                tr!("Lyrics are being downloaded for the scanned songs; wait for them or stop the scan first.").into();
            return;
        }
        self.lyrics_win = Some(LyricsWindow::new(self, paths));
    }

    /// Ctrl/Cmd+L, the Get lyrics… button and the menus: the selected songs.
    pub fn lyrics_selected(&mut self) {
        let paths: Vec<String> = if self.view == View::Files {
            self.rows.iter().filter(|p| self.selection.contains(*p)).cloned().collect()
        } else {
            vec![]
        };
        if paths.is_empty() {
            self.status = tr!(
                "Select one or more songs in the Files view (Ctrl/Cmd+A selects all listed), or use Edit → \
                 Download synced lyrics for all songs."
            )
            .into();
            return;
        }
        self.download_lyrics(paths);
    }

    pub fn lyrics_all(&mut self) {
        let files = self.all_files();
        if files.is_empty() {
            self.info = Some((tr!("Download synced lyrics").into(), tr!("Add a folder or files first.").into()));
            return;
        }
        self.download_lyrics(files);
    }

    pub fn fix_selected(&mut self) {
        if self.scanning {
            return;
        }
        let sel: Vec<FileResult> =
            self.selected_results().into_iter().filter(|r| FIXABLE_VERDICTS.contains(&r.verdict)).collect();
        if sel.is_empty() {
            self.info = Some((
                tr!("Fix to real resolution").into(),
                tr!("Select one or more fake hi-res or fake lossless files first.").into(),
            ));
            return;
        }
        self.open_fix(sel, false);
    }

    pub fn fix_all(&mut self) {
        if self.scanning {
            return;
        }
        let all: Vec<FileResult> =
            self.results.values().filter(|r| FIXABLE_VERDICTS.contains(&r.verdict)).cloned().collect();
        if all.is_empty() {
            self.info = Some((
                tr!("Fix to real resolution").into(),
                tr!("No fake hi-res or fake lossless files were found.").into(),
            ));
            return;
        }
        self.open_fix(all, false);
    }

    pub fn rescan_selected(&mut self) {
        let listed: HashSet<String> = self.all_files().into_iter().collect();
        let paths: Vec<String> =
            self.rows.iter().filter(|p| self.selection.contains(*p) && listed.contains(*p)).cloned().collect();
        if paths.is_empty() {
            self.status = tr!("Select the files to analyse again.").into();
            return;
        }
        self.start_scan(Some(paths), true);
    }

    pub fn rescan_outdated(&mut self) {
        let paths: Vec<String> = self
            .results
            .iter()
            .filter(|(_, r)| r.saved.as_ref().is_some_and(|s| s.outdated))
            .map(|(p, _)| p.clone())
            .collect();
        if paths.is_empty() {
            self.status = tr!("No saved results from older detection rules are listed.").into();
            return;
        }
        self.start_scan(Some(paths), true);
    }

    pub fn forget_saved_ask(&mut self) {
        let Some(st) = &self.store else {
            self.info = Some((
                tr!("Saved results").into(),
                tr!("Saving results is turned off (Settings → Saved results).").into(),
            ));
            return;
        };
        let s = st.stats();
        self.confirm = Some(Confirm {
            title: tr!("Delete all saved results").into(),
            text: tr!(
                "Delete the saved results of {n} file(s) ({mb} MB in {path})?\n\nThe files themselves are not touched; they are simply analysed again the next time they are scanned.",
                n = s.files,
                mb = f(s.disk_bytes as f64 / 1e6, 0),
                path = s.path.display()
            ),
            action: ConfirmAction::ForgetSaved,
        });
    }

    pub fn forget_saved(&mut self) {
        let Some(st) = &self.store else { return };
        match st.forget(None) {
            Ok(n) => {
                for r in self.results.values_mut() {
                    r.saved = None;
                }
                self.status =
                    tr!("Deleted {n} saved result(s). The results on screen stay until you scan again.", n = n);
            }
            Err(e) => {
                self.info = Some((
                    tr!("Saved results").into(),
                    tr!("Could not delete the saved results: {error}", error = tr_text(&e.to_string())),
                ))
            }
        }
    }

    pub fn save_settings(&mut self) {
        if let Err(e) = settings::save(&self.cfg, None) {
            self.info = Some((
                tr!("Settings").into(),
                tr!("Could not save settings: {error}", error = tr_text(&e.to_string())),
            ));
        }
    }

    pub fn apply_settings(&mut self, new: Settings) {
        let old_shown = self.cfg.shown_verdicts.clone();
        let relist = new.extension_set() != self.cfg.extension_set() || new.recursive != self.cfg.recursive;
        let detection = settings_key(&new) != settings_key(&self.cfg);
        let store_changed =
            (new.save_results, new.saved_graphs_mb) != (self.cfg.save_results, self.cfg.saved_graphs_mb);
        if new.language != self.cfg.language {
            // egui draws every frame, so the whole app speaks the new language at once
            i18n::set_language(&new.language);
            self.lyrics_shown = None;
        }
        self.cfg = new;
        self.graphs = GraphWorker::start(self.cfg.clone(), self.post.clone());
        if store_changed {
            self.store = open_store(&self.cfg);
        }
        if relist && !self.scanning {
            let sources: Vec<String> = self.sources.iter().map(|s| s.path.clone()).collect();
            self.clear();
            for s in sources {
                self.add_source(&s);
            }
        } else if detection && !self.scanning && !self.results.is_empty() {
            self.results.clear();
            self.cache.clear();
            self.lookup_gen += 1;
            self.rows_dirty = true;
            let files = self.all_files();
            self.request_lookup(files);
        }
        self.save_settings();
        if self.cfg.shown_verdicts != old_shown {
            let vs: Vec<Verdict> = self.cfg.shown_verdicts.iter().filter_map(|s| Verdict::parse(s)).collect();
            self.set_filters(&vs);
        }
        self.spectro = SpectroTexture::default();
        self.status = if detection {
            tr!("Settings saved. Files without a result for the new detection settings are analysed when you press Scan.")
                .into()
        } else {
            tr!("Settings saved.").into()
        };
    }

    // --------------------------------------------------------- album views ---
    pub fn switch_view(&mut self, v: View) {
        if v != View::Files && self.group_filter.is_some() {
            self.close_group(false);
        }
        self.view = v;
        self.groups_dirty = true;
    }

    fn regroup(&mut self) {
        if !self.groups_dirty || self.view == View::Files {
            return;
        }
        self.groups_dirty = false;
        let mode = if self.view == View::Album { GroupMode::Album } else { GroupMode::Artist };
        let q = self.search.trim().to_lowercase();
        let res: Vec<&FileResult> = self.results.values().collect();
        let mut groups = group_results(res, mode);
        groups
            .retain(|g| q.is_empty() || g.title.to_lowercase().contains(&q) || g.subtitle.to_lowercase().contains(&q));
        sort_groups(&mut groups, self.cfg.group_sort == "Name");
        for g in &groups {
            let key = format!("{}:{}", self.view.label(), g.key);
            if !self.cover_tex.contains_key(&key) {
                self.cover_tex.insert(key.clone(), None);
                let _ = self.covers.send((key, g.paths.iter().take(4).cloned().collect()));
            }
        }
        self.groups = groups;
        if self.find_open {
            self.update_find(false);
        }
    }

    fn open_group(&mut self, g: &Group) {
        let mode = if self.view == View::Album { GroupMode::Album } else { GroupMode::Artist };
        self.group_filter = Some(GroupFilter {
            mode,
            title: g.title.clone(),
            subtitle: g.subtitle.clone(),
            problems: g.problems(),
            total: g.total,
            paths: g.paths.iter().cloned().collect(),
            order: g.paths.clone(),
            back: self.view,
            filters: self.show,
        });
        self.show = [true; 7];
        self.view = View::Files;
        self.sort_col = Col::Track;
        self.sort_rev = false;
        self.rows_dirty = true;
        self.refresh();
        let first = self
            .rows
            .iter()
            .find(|p| self.results.get(*p).is_some_and(|r| r.is_problem()))
            .or(self.rows.first())
            .cloned();
        if let Some(p) = first {
            self.select_only(&p);
            self.scroll_to = self.anchor;
        }
    }

    fn close_group(&mut self, switch: bool) {
        let Some(gf) = self.group_filter.take() else { return };
        self.show = gf.filters;
        if self.sort_col == Col::Track {
            self.sort_col = Col::Verdict;
            self.sort_rev = false;
        }
        if switch {
            self.view = gf.back;
        }
        self.rows_dirty = true;
        self.groups_dirty = true;
    }

    // ---------------------------------------------------------------- find ---
    fn find_candidates(&self) -> Vec<(String, String)> {
        if self.view == View::Files {
            self.rows
                .iter()
                .map(|p| {
                    let text = match self.results.get(p) {
                        Some(r) => {
                            let tags: Vec<&str> = r.tags.iter().map(|(_, v)| v.as_str()).collect();
                            let mut text = format!(
                                "{} {} {} {} {} {}",
                                verdict_style(r.verdict).0,
                                r.summary,
                                r.claimed(),
                                r.estimated(),
                                p,
                                tags.join(" ")
                            );
                            if i18n::language() != "en" {
                                // what the list shows, so the find bar also finds the translated texts
                                text += &format!(" {} {}", verdict_label(r.verdict), tr_text(&r.summary));
                            }
                            text
                        }
                        None if i18n::language() != "en" => format!("{PENDING_LABEL} {} {p}", i18n::tr(PENDING_LABEL)),
                        None => format!("{PENDING_LABEL} {p}"),
                    };
                    (p.clone(), text)
                })
                .collect()
        } else {
            self.groups
                .iter()
                .map(|g| {
                    (g.key.clone(), format!("{} {} {} {}", g.title, g.subtitle, tr_name(&g.title), tr_name(&g.subtitle)))
                })
                .collect()
        }
    }

    fn update_find(&mut self, reset: bool) {
        let q = self.find_text.trim().to_lowercase();
        let prev = self.find_index.and_then(|i| self.find_matches.get(i).cloned());
        self.find_matches = if q.is_empty() {
            vec![]
        } else {
            self.find_candidates().into_iter().filter(|(_, t)| t.to_lowercase().contains(&q)).map(|(k, _)| k).collect()
        };
        self.find_index = if self.find_matches.is_empty() {
            None
        } else if reset || prev.as_ref().is_none_or(|p| !self.find_matches.contains(p)) {
            Some(0)
        } else {
            self.find_matches.iter().position(|m| Some(m) == prev.as_ref())
        };
        if reset {
            self.find_jump();
        }
    }

    fn find_step(&mut self, delta: i64) {
        if !self.find_open {
            self.open_find();
            return;
        }
        if !self.find_matches.is_empty() {
            let n = self.find_matches.len() as i64;
            self.find_index = Some(((self.find_index.unwrap_or(0) as i64 + delta).rem_euclid(n)) as usize);
            self.find_jump();
        }
    }

    fn find_jump(&mut self) {
        let Some(cur) = self.find_index.and_then(|i| self.find_matches.get(i).cloned()) else { return };
        if self.view == View::Files {
            self.select_only(&cur);
            self.scroll_to = self.anchor;
        } else {
            self.card_current = Some(cur);
        }
    }

    fn open_find(&mut self) {
        self.find_open = true;
        self.find_focus = true;
        self.update_find(true);
    }

    // ------------------------------------------------------------ shortcuts ---
    fn shortcuts(&mut self, ctx: &egui::Context) {
        let cmd = Modifiers::COMMAND;
        let sc = |m: Modifiers, k: Key| KeyboardShortcut::new(m, k);
        let pressed = |s: KeyboardShortcut| ctx.input_mut(|i| i.consume_shortcut(&s));
        if pressed(sc(cmd, Key::O)) {
            self.add_folder_dialog();
        }
        if pressed(sc(cmd, Key::Q)) {
            self.quit = true;
        }
        if pressed(sc(cmd, Key::Comma)) {
            self.settings_win = Some(SettingsWindow::new(&self.cfg));
        }
        if pressed(sc(cmd | Modifiers::SHIFT, Key::F)) {
            self.fix_selected();
        }
        if pressed(sc(cmd, Key::F)) {
            self.open_find();
        }
        if pressed(sc(Modifiers::SHIFT, Key::F3)) {
            self.find_step(-1);
        }
        if pressed(sc(Modifiers::NONE, Key::F3)) {
            self.find_step(1);
        }
        // Esc closes the topmost window (messages and confirmations handle it themselves)
        if self.info.is_none()
            && self.confirm.is_none()
            && ctx.input_mut(|i| i.consume_key(Modifiers::NONE, Key::Escape))
        {
            if self.fix_done.is_some() {
                self.fix_done = None;
            } else if self.lyrics_win.as_ref().is_some_and(|w| !w.running) {
                self.lyrics_win = None;
            } else if self.fix_win.as_ref().is_some_and(|w| !w.is_running() && !w.hidden) {
                self.fix_win = None;
            } else if self.settings_win.is_some() {
                self.settings_win = None;
            } else if self.summary.is_some() {
                self.summary = None;
            } else if self.about {
                self.about = false;
            } else if self.find_open {
                self.find_open = false;
                self.find_matches.clear();
                self.find_index = None;
            }
        }
        let typing = ctx.egui_wants_keyboard_input();
        let dialogs = self.settings_win.is_some()
            || self.fix_win.as_ref().is_some_and(|w| !w.hidden)
            || self.summary.is_some()
            || self.confirm.is_some()
            || self.lyrics_win.is_some()
            || self.fix_done.is_some();
        if typing || dialogs {
            return;
        }
        if pressed(sc(cmd, Key::L)) {
            self.lyrics_selected();
        }
        if pressed(sc(cmd, Key::A)) {
            self.select_all();
        }
        if pressed(sc(cmd, Key::C)) {
            self.copy_paths(ctx);
        }
        if pressed(sc(cmd, Key::Enter)) {
            self.secondary_action();
        }
        if pressed(sc(Modifiers::NONE, Key::Enter)) {
            self.primary_action();
        }
        if (pressed(sc(Modifiers::NONE, Key::Delete)) || pressed(sc(Modifiers::NONE, Key::Backspace)))
            && !self.source_sel.is_empty()
        {
            self.remove_sources();
        }
        let down = pressed(sc(Modifiers::NONE, Key::ArrowDown));
        let up = pressed(sc(Modifiers::NONE, Key::ArrowUp));
        if (down || up) && self.view == View::Files && !self.rows.is_empty() {
            let i = self.current.as_ref().and_then(|c| self.rows.iter().position(|p| p == c));
            let n = match (i, down) {
                (None, _) => 0,
                (Some(i), true) => (i + 1).min(self.rows.len() - 1),
                (Some(i), false) => i.saturating_sub(1),
            };
            let p = self.rows[n].clone();
            self.select_only(&p);
            self.scroll_to = Some(n);
        }
    }

    fn copy_paths(&mut self, ctx: &egui::Context) {
        let paths: Vec<String> = self.rows.iter().filter(|p| self.selection.contains(*p)).cloned().collect();
        if !paths.is_empty() {
            ctx.copy_text(paths.join("\n"));
            self.status = tr!("Copied {n} path(s).", n = paths.len());
        }
    }

    fn primary_action(&mut self) {
        if self.cfg.double_click == "Open file" {
            self.open_current_file();
        } else {
            self.reveal_current();
        }
    }

    fn secondary_action(&mut self) {
        if self.cfg.double_click == "Open file" {
            self.reveal_current();
        } else {
            self.open_current_file();
        }
    }

    fn reveal_current(&mut self) {
        if let Some(p) = &self.current {
            reveal_in_file_manager(p);
        }
    }

    fn open_current_file(&mut self) {
        if let Some(p) = &self.current {
            open_with_default_app(p);
        }
    }

    // ------------------------------------------------------------- drawing ---
    /// The whole 1.x window: the work in the background plus the page.
    pub fn ui(&mut self, ui: &mut Ui) {
        let ctx = ui.ctx().clone();
        self.background(&ctx);
        self.page(ui);
    }

    /// What has to happen every frame, also while another page of the player
    /// is shown (2.0): messages from the workers, the pop-ups and dialogs.
    pub fn background(&mut self, ctx: &egui::Context) {
        let ctx = ctx.clone();
        self.poll();
        if self.summary_due.is_some_and(|t| t.elapsed().as_millis() >= 150) {
            self.summary_due = None;
            if !self.scanning && !self.results.is_empty() {
                self.summary = Some(SummaryWindow::new(self));
            }
        }
        if self.fix_due.is_some_and(|t| t.elapsed().as_millis() >= 400) {
            self.fix_due = None;
            self.fix_after_scan();
        }
        if self.scanning {
            if self.eta.is_some() {
                self.update_status(false, false);
            }
            ctx.request_repaint_after(std::time::Duration::from_millis(500));
        }
        if self.summary_due.is_some() || self.fix_due.is_some() {
            ctx.request_repaint_after(std::time::Duration::from_millis(100));
        }
        crate::dialogs::show_all(self, &ctx);
        if self.about {
            let mut open = true;
            egui::Window::new(tr!("About {app}", app = realhires::APP_NAME)).id(egui::Id::unique("about-window")).open(&mut open).collapsible(false).resizable(false).anchor(Align2::CENTER_CENTER, Vec2::ZERO).show(&ctx, |ui| {
                ui.heading(format!("{} {}", realhires::APP_NAME, realhires::VERSION));
                ui.label(tr!("A music player that finds fake hi-res audio (upsampled or 16-bit padded) and fake lossless files made from MP3/AAC, by analysing the decoded audio itself."));
                ui.label(tr!("This is the Rust version; it shares its settings, saved results, library and player state with the Python version."));
                ui.label(tr_text(realhires::lyrics::ABOUT_CREDIT));
                ui.label(tr!("Windows and Linux use the Inter typeface by The Inter Project Authors (SIL Open Font License 1.1)."));
                ui.horizontal_wrapped(|ui| {
                    ui.hyperlink_to("LRCLIB", "https://lrclib.net");
                    ui.hyperlink_to("LRCGET", "https://github.com/tranxuanthang/lrcget");
                    ui.hyperlink_to("Inter", "https://rsms.me/inter/");
                });
                ui.hyperlink(realhires::PROJECT_URL);
            });
            self.about = open;
        }
        if self.quit {
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
        }
    }

    pub fn show_about(&mut self) {
        self.about = true;
    }

    /// The Quality check page: the 1.x window's panels.
    pub fn page(&mut self, ui: &mut Ui) {
        let ctx = ui.ctx().clone();
        self.refresh();
        self.regroup();
        self.shortcuts(&ctx);
        let dropped: Vec<String> = ctx.input(|i| {
            i.raw
                .dropped_files
                .iter()
                .filter(|f| !is_theme_file(f.path()))
                .map(|f| f.path().to_string_lossy().into_owned())
                .filter(|p| !p.is_empty())
                .collect()
        });
        for p in dropped {
            self.add_source(&p);
        }
        egui::Panel::top("menu").show(ui, |ui| self.menu_bar(ui));
        egui::Panel::top("toolbar").show(ui, |ui| self.toolbar(ui));
        egui::Panel::top("sources").show(ui, |ui| self.sources_panel(ui));
        egui::Panel::top("filters").show(ui, |ui| self.filter_bar(ui));
        if self.find_open {
            egui::Panel::top("find").show(ui, |ui| self.find_bar(ui));
        }
        egui::Panel::bottom("status").show(ui, |ui| self.status_bar(ui));
        egui::Panel::bottom("dashboard")
            .resizable(true)
            .default_size(330.0)
            .min_size(160.0)
            .show(ui, |ui| self.dashboard(ui));
        egui::CentralPanel::default().show(ui, |ui| {
            if self.view == View::Files {
                self.table(ui);
            } else {
                self.cards(ui);
            }
        });
    }

    fn menu_bar(&mut self, ui: &mut Ui) {
        egui::MenuBar::new().ui(ui, |ui| {
            ui.menu_button(tr!("File"), |ui| {
                if ui.button(tr!("Add Folder…")).clicked() {
                    self.add_folder_dialog();
                }
                if ui.button(tr!("Add Files…")).clicked() {
                    self.add_files_dialog();
                }
                ui.separator();
                if ui.button(tr!("Export listed files as CSV…")).clicked() {
                    self.export(false, false);
                }
                if ui.button(tr!("Export listed files as JSON…")).clicked() {
                    self.export(true, false);
                }
                ui.separator();
                if ui.button(tr!("Delete all saved results…")).clicked() {
                    self.forget_saved_ask();
                }
                ui.separator();
                if ui.button(tr!("Quit")).clicked() {
                    self.quit = true;
                }
            });
            ui.menu_button(tr!("Edit"), |ui| {
                if ui.button(tr!("Open file location")).clicked() {
                    self.reveal_current();
                }
                if ui.button(tr!("Open file")).clicked() {
                    self.open_current_file();
                }
                if ui.button(tr!("Select all listed files")).clicked() {
                    self.select_all();
                }
                if ui.button(tr!("Copy path")).clicked() {
                    let ctx = ui.ctx().clone();
                    self.copy_paths(&ctx);
                }
                ui.separator();
                if ui.button(tr!("Find…")).clicked() {
                    self.open_find();
                }
                ui.separator();
                if ui.button(tr!("Fix selected files to real resolution…")).clicked() {
                    self.fix_selected();
                }
                if ui.button(tr!("Fix all fake files to real resolution…")).clicked() {
                    self.fix_all();
                }
                ui.separator();
                if ui
                    .add(
                        egui::Button::new(tr!("Download synced lyrics for selected songs…")).shortcut_text(
                            ui.ctx().format_shortcut(&KeyboardShortcut::new(Modifiers::COMMAND, Key::L)),
                        ),
                    )
                    .clicked()
                {
                    self.lyrics_selected();
                }
                if ui.button(tr!("Download synced lyrics for all songs…")).clicked() {
                    self.lyrics_all();
                }
                ui.separator();
                if ui.button(tr!("Re-scan selected files")).clicked() {
                    self.rescan_selected();
                }
                if ui.button(tr!("Re-scan files checked by older rules")).clicked() {
                    self.rescan_outdated();
                }
                ui.separator();
                if ui.button(tr!("Settings…")).clicked() {
                    self.settings_win = Some(SettingsWindow::new(&self.cfg));
                }
            });
            ui.menu_button(tr!("View"), |ui| {
                for v in [View::Files, View::Album, View::Artist] {
                    if ui.button(i18n::tr(v.label())).clicked() {
                        self.switch_view(v);
                    }
                }
                ui.separator();
                if ui.button(tr!("Scan summary…")).clicked() {
                    if self.results.is_empty() {
                        self.info = Some((tr!("Scan summary").into(), tr!("Nothing scanned yet.").into()));
                    } else {
                        self.summary = Some(SummaryWindow::new(self));
                    }
                }
                if ui.button(tr!("Show problem files only")).clicked() {
                    self.set_filters(&[Verdict::FakeLossless, Verdict::Fake, Verdict::Suspect]);
                }
                if ui.button(tr!("Show all files")).clicked() {
                    self.set_filters(&VERDICT_ORDER);
                }
            });
            ui.menu_button(tr!("Help"), |ui| {
                if ui.button(tr!("About {app}", app = realhires::APP_NAME)).clicked() {
                    self.about = true;
                }
            });
        });
    }

    fn toolbar(&mut self, ui: &mut Ui) {
        ui.horizontal(|ui| {
            if ui.button(tr!("Add Folder…")).clicked() {
                self.add_folder_dialog();
            }
            if ui.button(tr!("Add Files…")).clicked() {
                self.add_files_dialog();
            }
            if ui.add_enabled(!self.scanning, egui::Button::new(tr!("Clear"))).clicked() {
                self.clear();
            }
            ui.add_space(12.0);
            ui.checkbox(&mut self.quick, tr!("Quick scan (first {seconds} s)", seconds = g(self.cfg.quick_scan_seconds)));
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui.button(format!("⚙ {}", tr!("Settings…"))).clicked() {
                    self.settings_win = Some(SettingsWindow::new(&self.cfg));
                }
                ui.add_space(12.0);
                if ui.add_enabled(self.scanning, egui::Button::new(tr!("Stop"))).clicked() {
                    self.stop_scan();
                }
                let scan = egui::Button::new(RichText::new(tr!("Scan")).strong());
                if ui.add_enabled(!self.scanning, scan).clicked() {
                    self.start_scan(None, false);
                }
            });
        });
    }

    fn sources_panel(&mut self, ui: &mut Ui) {
        let mut remove = false;
        ui.horizontal(|ui| {
            ui.label(RichText::new(tr!("Sources to scan")).strong());
            if !self.sources.is_empty() {
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if ui
                        .add_enabled(!self.source_sel.is_empty(), egui::Button::new(tr!("Remove")))
                        .on_hover_text(tr!("Remove the selected sources (Delete key)"))
                        .clicked()
                    {
                        remove = true;
                    }
                });
            }
        });
        if self.sources.is_empty() {
            ui.label(RichText::new(tr!("Nothing added yet. Click “Add Folder…” or “Add Files…” (or drop folders here): the files found are listed below straight away, then press Scan.")).color(hex(0x666666)));
            return;
        }
        let text = ui.visuals().text_color();
        let mut clicked = None;
        egui::ScrollArea::vertical().id_salt("sources").max_height(96.0).auto_shrink([false, true]).show(ui, |ui| {
            egui::Grid::new("srcgrid").striped(true).num_columns(4).min_col_width(80.0).spacing([24.0, 2.0]).show(
                ui,
                |ui| {
                    ui.label(RichText::new(tr!("Folder / file")).color(hex(0x666666)));
                    ui.label(RichText::new(tr!("Type")).color(hex(0x666666)));
                    ui.label(RichText::new(tr!("Audio files")).color(hex(0x666666)));
                    ui.label(RichText::new(tr!("Fake / suspicious")).color(hex(0x666666)));
                    ui.end_row();
                    for s in &self.sources {
                        let files = self.source_files.get(&s.path).map(Vec::as_slice).unwrap_or(&[]);
                        let listing = self.listing.contains_key(&s.path);
                        let empty = files.is_empty() && !listing;
                        let (count, color) = if listing {
                            (tr!("{n} so far…", n = files.len()), hex(0x808080))
                        } else if empty {
                            let why = if s.is_dir {
                                tr!("no audio files found")
                            } else {
                                tr!("not an audio file (check Settings → Scanning → File extensions)")
                            };
                            (why.to_string(), hex(0xc62828))
                        } else {
                            (count(files.len(), n_!("1 file"), n_!("{n} files")), text)
                        };
                        let sel = self.source_sel.contains(&s.path);
                        let label = RichText::new(&s.path).color(if empty { hex(0xc62828) } else { text });
                        if ui.add(egui::Button::selectable(sel, label)).clicked() {
                            clicked = Some(s.path.clone());
                        }
                        ui.label(if s.is_dir { tr!("Folder") } else { tr!("File") });
                        ui.label(RichText::new(count).color(color));
                        let done: Vec<&FileResult> = files.iter().filter_map(|f| self.results.get(f)).collect();
                        let probs = done.iter().filter(|r| r.is_problem()).count();
                        ui.label(if done.is_empty() || listing {
                            String::new()
                        } else {
                            tr!("{n} of {total}", n = probs, total = done.len())
                        });
                        ui.end_row();
                    }
                },
            );
        });
        if let Some(c) = clicked {
            if ui.input(|i| i.modifiers.command || i.modifiers.shift) {
                if !self.source_sel.remove(&c) {
                    self.source_sel.insert(c);
                }
            } else if self.source_sel.len() == 1 && self.source_sel.contains(&c) {
                self.source_sel.clear();
            } else {
                self.source_sel = [c].into();
            }
        }
        if remove {
            self.remove_sources();
        }
    }

    fn filter_bar(&mut self, ui: &mut Ui) {
        ui.horizontal(|ui| {
            ui.label(tr!("View:"));
            for v in [View::Files, View::Album, View::Artist] {
                if ui.add(egui::Button::selectable(self.view == v, i18n::tr(v.label()))).clicked() && self.view != v {
                    self.switch_view(v);
                }
            }
            if self.view != View::Files {
                ui.add_space(16.0);
                ui.label(tr!("Rings show:"));
                let before = (self.cfg.card_label.clone(), self.cfg.group_sort.clone());
                // the setting values stay English; the menus show them translated
                egui::ComboBox::from_id_salt("ring").selected_text(i18n::tr(&self.cfg.card_label)).show_ui(ui, |ui| {
                    for c in [n_!("Percentage"), n_!("Songs (x of n)")] {
                        ui.selectable_value(&mut self.cfg.card_label, c.to_string(), i18n::tr(c));
                    }
                });
                ui.label(tr!("Sort:"));
                egui::ComboBox::from_id_salt("gsort").selected_text(i18n::tr(&self.cfg.group_sort)).show_ui(ui, |ui| {
                    for c in [n_!("Most fake first"), n_!("Name")] {
                        ui.selectable_value(&mut self.cfg.group_sort, c.to_string(), i18n::tr(c));
                    }
                });
                if before != (self.cfg.card_label.clone(), self.cfg.group_sort.clone()) {
                    self.groups_dirty = true;
                    self.save_settings();
                }
            }
            if let Some(gf) = &self.group_filter {
                ui.add_space(16.0);
                ui.label(RichText::new(gf.label()).color(hex(0x0b62c4)).strong());
                let back = if gf.mode == GroupMode::Album { tr!("Back to albums") } else { tr!("Back to artists") };
                let back = format!("◀ {back}");
                if ui.button(back).clicked() {
                    self.close_group(true);
                }
            }
        });
        ui.horizontal(|ui| {
            ui.label(tr!("Show:"));
            let c = self.counts();
            for (i, v) in VERDICT_ORDER.iter().enumerate() {
                let (_, color, _) = verdict_style(*v);
                let label = verdict_label(*v);
                let text = if self.results.is_empty() { label.to_string() } else { format!("{label} ({})", c[i]) };
                if ui.checkbox(&mut self.show[i], RichText::new(text).color(color)).changed() {
                    self.rows_dirty = true;
                }
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui
                    .add(
                        egui::TextEdit::singleline(&mut self.search)
                            .desired_width(180.0)
                            .hint_text(tr!("file, folder or reason")),
                    )
                    .changed()
                {
                    self.rows_dirty = true;
                    self.groups_dirty = true;
                }
                ui.label(tr!("Search:"));
            });
        });
    }

    fn find_bar(&mut self, ui: &mut Ui) {
        ui.horizontal(|ui| {
            ui.label(tr!("Find:"));
            let resp = ui.add(egui::TextEdit::singleline(&mut self.find_text).desired_width(260.0));
            if self.find_focus {
                resp.request_focus();
                self.find_focus = false;
            }
            if resp.changed() {
                self.update_find(true);
            }
            if resp.lost_focus() && ui.input(|i| i.key_pressed(Key::Enter)) {
                let back = ui.input(|i| i.modifiers.shift);
                self.find_step(if back { -1 } else { 1 });
                resp.request_focus();
            }
            if ui.button("▲").clicked() {
                self.find_step(-1);
            }
            if ui.button("▼").clicked() {
                self.find_step(1);
            }
            let n = self.find_matches.len();
            if n > 0 {
                ui.label(tr!("{n} of {total}", n = self.find_index.unwrap_or(0) + 1, total = n));
            } else if !self.find_text.trim().is_empty() {
                ui.label(RichText::new(tr!("No matches")).color(hex(0xc62828)));
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui.button("✕").clicked() {
                    self.find_open = false;
                    self.find_matches.clear();
                    self.find_index = None;
                }
                ui.label(
                    RichText::new(tr!("Enter = next, Shift+Enter = previous, Esc = close")).small().color(hex(0x888888)),
                );
            });
        });
    }

    fn status_bar(&mut self, ui: &mut Ui) {
        ui.horizontal(|ui| {
            let frac = if self.total > 0 { self.done as f32 / self.total as f32 } else { 0.0 };
            ui.add(egui::ProgressBar::new(frac).desired_width(220.0));
            ui.label(&self.status);
            let outdated = self.results.values().filter(|r| r.saved.as_ref().is_some_and(|s| s.outdated)).count();
            if outdated > 0 && !self.scanning {
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if ui.button(tr!("Re-scan them")).clicked() {
                        self.rescan_outdated();
                    }
                    let text = if outdated == 1 {
                        tr!("1 saved result was made by older detection rules.").to_string()
                    } else {
                        tr!("{n} saved results were made by older detection rules.", n = outdated)
                    };
                    ui.label(RichText::new(text).color(hex(0x8a5a00)));
                });
            }
        });
    }

    fn row_texts(&self, path: &str, running: &HashSet<String>) -> ([String; 8], Color32) {
        let name = file_name(path);
        let folder = parent(path);
        match self.results.get(path) {
            Some(r) => {
                let color = verdict_style(r.verdict).1;
                let bw = match r.bandwidth_hz {
                    Some(b) if b != 0.0 => format!("{} kHz", f(b / 1000.0, 1)),
                    _ => "-".into(),
                };
                // the core's texts in the language in use
                let fix = tr_text(&fix_outlook(r, &self.cfg));
                let why = tr_text(&r.summary);
                let est = tr_text(&r.estimated());
                ([verdict_label(r.verdict).into(), why, r.claimed(), est, bw, fix, name, folder], color)
            }
            None if running.contains(path) => (
                [
                    i18n::tr(RUNNING_LABEL).into(),
                    tr!("Analysing…").into(),
                    String::new(),
                    String::new(),
                    String::new(),
                    String::new(),
                    name,
                    folder,
                ],
                hex(RUNNING_COLOR),
            ),
            None => {
                let why = if self.scanning { tr!("Waiting in the scan queue…") } else { tr!("Not scanned yet: press Scan") };
                (
                    [
                        i18n::tr(PENDING_LABEL).into(),
                        why.into(),
                        String::new(),
                        String::new(),
                        String::new(),
                        String::new(),
                        name,
                        folder,
                    ],
                    hex(PENDING_COLOR),
                )
            }
        }
    }

    fn table(&mut self, ui: &mut Ui) {
        let running = if self.scanning { self.mark_running() } else { HashSet::new() };
        let found: HashSet<&String> = self.find_matches.iter().collect();
        let cur_found = self.find_index.and_then(|i| self.find_matches.get(i)).cloned();
        let mut clicked: Option<(usize, Modifiers, bool)> = None;
        let mut sort: Option<Col> = None;
        let mut menu_action: Option<&'static str> = None;
        let row_h = 20.0;
        let mut tb = TableBuilder::new(ui)
            .id_salt("files")
            .striped(true)
            .resizable(true)
            .sense(Sense::click())
            .cell_layout(Layout::left_to_right(Align::Center))
            .column(Column::initial(105.0).clip(true))
            .column(Column::initial(380.0).clip(true))
            .column(Column::initial(70.0).clip(true))
            .column(Column::initial(85.0).clip(true))
            .column(Column::initial(82.0).clip(true))
            .column(Column::initial(262.0).clip(true))
            .column(Column::initial(230.0).clip(true))
            .column(Column::remainder().clip(true))
            .min_scrolled_height(0.0);
        if let Some(i) = self.scroll_to.take() {
            tb = tb.scroll_to_row(i, Some(Align::Center));
        }
        let rows = &self.rows;
        tb.header(22.0, |mut h| {
            for (c, title) in COLUMNS {
                h.col(|ui| {
                    let arrow = if self.sort_col == c {
                        if self.sort_rev {
                            " ▼"
                        } else {
                            " ▲"
                        }
                    } else {
                        ""
                    };
                    if ui
                        .add(
                            egui::Button::new(RichText::new(format!("{}{arrow}", i18n::tr(title))).strong())
                                .frame(false),
                        )
                        .clicked()
                    {
                        sort = Some(c);
                    }
                });
            }
        })
        .body(|body| {
            body.rows(row_h, rows.len(), |mut row| {
                let i = row.index();
                let path = &rows[i];
                let (texts, color) = self.row_texts(path, &running);
                row.set_selected(self.selection.contains(path));
                let bg = if cur_found.as_ref() == Some(path) {
                    Some(hex(FOUND_CUR))
                } else if found.contains(path) {
                    Some(hex(FOUND))
                } else {
                    None
                };
                for (k, t) in texts.iter().enumerate() {
                    row.col(|ui| {
                        if let Some(bg) = bg {
                            ui.painter().rect_filled(ui.max_rect(), 0.0, bg);
                        }
                        let text = if k == 0 {
                            RichText::new(t).color(color).strong()
                        } else if k == 1 {
                            RichText::new(t).color(color)
                        } else {
                            RichText::new(t)
                        };
                        ui.add(egui::Label::new(text).truncate().selectable(false));
                    });
                }
                let resp = row.response();
                if resp.clicked() {
                    clicked = Some((i, resp.ctx.input(|x| x.modifiers), false));
                }
                if resp.double_clicked() {
                    clicked = Some((i, Modifiers::NONE, true));
                }
                if resp.secondary_clicked() && !self.selection.contains(path) {
                    clicked = Some((i, Modifiers::NONE, false));
                }
                resp.context_menu(|ui| {
                    for (label, act) in [
                        (n_!("Open file location"), "reveal"),
                        (n_!("Open file"), "open"),
                        (n_!("Copy path"), "copy"),
                        (n_!("Fix to real resolution…"), "fix"),
                        (n_!("Download synced lyrics…"), "lyrics"),
                        (n_!("Re-scan"), "rescan"),
                    ] {
                        if act == "fix" {
                            ui.separator();
                        }
                        if ui.button(i18n::tr(label)).clicked() {
                            menu_action = Some(act);
                            ui.close();
                        }
                    }
                });
            });
        });
        if let Some(c) = sort {
            self.sort_rev = if c == self.sort_col { !self.sort_rev } else { false };
            self.sort_col = c;
            self.rows_dirty = true;
        }
        if let Some((i, m, double)) = clicked {
            let path = self.rows[i].clone();
            if let (true, Some(a)) = (m.shift, self.anchor) {
                let (lo, hi) = (a.min(i), a.max(i));
                self.selection = self.rows[lo..=hi].iter().cloned().collect();
                self.current = Some(path);
            } else if m.command {
                if !self.selection.remove(&path) {
                    self.selection.insert(path.clone());
                }
                self.current = Some(path);
                self.anchor = Some(i);
            } else {
                self.select_only(&path);
            }
            if double {
                self.primary_action();
            }
        }
        match menu_action {
            Some("reveal") => self.reveal_current(),
            Some("open") => self.open_current_file(),
            Some("copy") => {
                let ctx = ui.ctx().clone();
                self.copy_paths(&ctx);
            }
            Some("fix") => self.fix_selected(),
            Some("lyrics") => self.lyrics_selected(),
            Some("rescan") => self.rescan_selected(),
            _ => {}
        }
        if self.rows.is_empty() && !self.results.is_empty() {
            ui.label(
                RichText::new(tr!("No files match the current filters. Tick more verdicts above, or clear the search."))
                    .color(hex(0x777777)),
            );
        }
    }

    fn cards(&mut self, ui: &mut Ui) {
        let (cw, ch, cover) = (196.0f32, 240.0f32, 150.0f32);
        let found: HashSet<&String> = self.find_matches.iter().collect();
        let mut open: Option<usize> = None;
        if self.groups.is_empty() {
            ui.label(
                RichText::new(if self.results.is_empty() {
                    tr!("Scan some files to see their albums and artists.")
                } else {
                    tr!("No albums match the search.")
                })
                .color(hex(0x777777)),
            );
            return;
        }
        let scroll_to = self.card_current.clone();
        egui::ScrollArea::vertical().id_salt("cards").auto_shrink([false, false]).show(ui, |ui| {
            let cols = ((ui.available_width() / cw).floor() as usize).max(1);
            for (row_i, chunk) in self.groups.chunks(cols).enumerate() {
                ui.horizontal(|ui| {
                    for (j, gr) in chunk.iter().enumerate() {
                        let idx = row_i * cols + j;
                        let (rect, resp) = ui.allocate_exact_size(Vec2::new(cw, ch), Sense::click());
                        if scroll_to.as_ref() == Some(&gr.key) {
                            ui.scroll_to_rect(rect, Some(Align::Center));
                        }
                        let p = ui.painter_at(rect);
                        let hl = self.card_current.as_ref() == Some(&gr.key);
                        let is_found = found.contains(&gr.key);
                        let card = rect.shrink(6.0);
                        p.rect_filled(card, 4.0, if is_found { hex(0xfff3c4) } else { ui.visuals().extreme_bg_color });
                        if hl || resp.hovered() {
                            p.rect_stroke(
                                card,
                                4.0,
                                Stroke::new(if hl { 3.0 } else { 1.0 }, hex(0xf39c12)),
                                egui::StrokeKind::Inside,
                            );
                        }
                        let crect = Rect::from_min_size(card.min + Vec2::new(7.0, 7.0), Vec2::splat(cover));
                        let key = format!("{}:{}", self.view.label(), gr.key);
                        match self.cover_tex.get(&key).and_then(|t| t.as_ref()) {
                            Some(tex) => {
                                p.image(
                                    tex.id(),
                                    crect,
                                    Rect::from_min_max(Pos2::ZERO, Pos2::new(1.0, 1.0)),
                                    Color32::WHITE,
                                );
                            }
                            None => {
                                p.rect_filled(crect, 0.0, placeholder_color(&gr.title));
                                let initials: String = gr
                                    .title
                                    .split_whitespace()
                                    .take(2)
                                    .filter_map(|w| w.chars().next())
                                    .collect::<String>()
                                    .to_uppercase();
                                p.text(
                                    crect.center(),
                                    Align2::CENTER_CENTER,
                                    if initials.is_empty() { "?".into() } else { initials },
                                    FontId::proportional(34.0),
                                    Color32::WHITE,
                                );
                            }
                        }
                        ring(&p, crect.max - Vec2::new(8.0, 8.0), gr, self.cfg.card_label.starts_with("Songs"));
                        let tx = crect.left();
                        let mut y = crect.bottom() + 8.0;
                        let tw = cw - 26.0;
                        let subtitle = if self.view == View::Artist {
                            count(gr.albums, n_!("1 album"), n_!("{n} albums"))
                        } else {
                            tr_name(&gr.subtitle).to_string()
                        };
                        for (text, font, color) in [
                            (tr_name(&gr.title).to_string(), FontId::proportional(13.0), ui.visuals().strong_text_color()),
                            (subtitle, FontId::proportional(11.5), hex(0x777777)),
                            card_line(gr),
                        ] {
                            let galley = p.layout(text, font, color, tw);
                            let first = galley.rows.first().map(|r| r.rect().height()).unwrap_or(14.0);
                            p.with_clip_rect(Rect::from_min_size(Pos2::new(tx, y), Vec2::new(tw, first + 1.0))).galley(
                                Pos2::new(tx, y),
                                galley,
                                color,
                            );
                            y += first + 3.0;
                        }
                        if resp.clicked() {
                            open = Some(idx);
                        }
                    }
                });
            }
        });
        if let Some(i) = open {
            let gr = self.groups[i].clone();
            self.open_group(&gr);
        }
    }

    fn dashboard(&mut self, ui: &mut Ui) {
        let cur = self.current.clone();
        let r = cur.as_ref().and_then(|p| self.results.get(p)).cloned();
        egui::Panel::left("info").resizable(true).default_size(480.0).min_size(260.0).show(ui, |ui| {
            ui.add_space(4.0);
            ui.horizontal(|ui| {
                let (text, color) = match (&r, &cur) {
                    (Some(r), _) => (verdict_label(r.verdict).to_uppercase(), verdict_style(r.verdict).1),
                    (None, Some(_)) => (i18n::tr(PENDING_LABEL).to_uppercase(), hex(PENDING_COLOR)),
                    _ => (String::new(), hex(0x9e9e9e)),
                };
                if !text.is_empty() {
                    egui::Frame::new().fill(color).inner_margin(egui::Margin::symmetric(10, 3)).corner_radius(3).show(
                        ui,
                        |ui| {
                            ui.label(RichText::new(text).color(Color32::WHITE).strong().size(14.0));
                        },
                    );
                }
                let name =
                    cur.as_ref().map(|p| file_name(p)).unwrap_or_else(|| tr!("Select a file in the list").into());
                ui.label(RichText::new(name).strong());
            });
            ui.add_space(6.0);
            let why = match (&r, &cur) {
                (Some(r), _) => {
                    if r.summary.is_empty() {
                        tr_text(&r.reasons.first().cloned().unwrap_or_default())
                    } else {
                        tr_text(&r.summary)
                    }
                }
                (None, Some(_)) => {
                    if self.scanning {
                        tr!("Waiting in the scan queue…").into()
                    } else {
                        tr!("This file will be analysed when you press Scan.").into()
                    }
                }
                _ => String::new(),
            };
            ui.label(RichText::new(why).size(14.0));
            ui.add_space(6.0);
            egui::Grid::new("facts").num_columns(4).spacing([6.0, 2.0]).show(ui, |ui| {
                let facts = self.facts(r.as_ref());
                for (i, (k, v)) in facts.iter().enumerate() {
                    ui.label(RichText::new(tr!("{fact}:", fact = i18n::tr(k))).color(hex(0x666666)));
                    ui.label(v);
                    if i % 2 == 1 {
                        ui.end_row();
                    }
                }
            });
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                let has = cur.is_some();
                if ui.add_enabled(has, egui::Button::new(tr!("Open Folder"))).clicked() {
                    self.reveal_current();
                }
                if ui.add_enabled(has, egui::Button::new(tr!("Open File"))).clicked() {
                    self.open_current_file();
                }
                if ui.add_enabled(has, egui::Button::new(tr!("Copy Path"))).clicked() {
                    let ctx = ui.ctx().clone();
                    self.copy_paths(&ctx);
                }
                let fixable = self
                    .selection
                    .iter()
                    .any(|p| self.results.get(p).is_some_and(|r| FIXABLE_VERDICTS.contains(&r.verdict)));
                ui.add_space(12.0);
                if ui.add_enabled(fixable && !self.scanning, egui::Button::new(tr!("Fix to real resolution…"))).clicked()
                {
                    self.fix_selected();
                }
                if ui.add_enabled(has, egui::Button::new(tr!("Get lyrics…"))).clicked() {
                    self.lyrics_selected();
                }
            });
            ui.add_space(4.0);
            ui.label(RichText::new(tr!("Details")).color(hex(0x666666)));
            egui::ScrollArea::vertical().id_salt("details").auto_shrink([false, false]).show(ui, |ui| {
                let text = self.details(r.as_ref(), cur.as_deref());
                ui.add(egui::Label::new(RichText::new(text).size(12.5)).wrap());
            });
        });
        egui::CentralPanel::default().show(ui, |ui| {
            ui.horizontal(|ui| {
                for (t, label) in [
                    (PlotTab::Spectrogram, tr!("Spectrogram")),
                    (PlotTab::Spectrum, tr!("Average spectrum")),
                    (PlotTab::Lyrics, tr!("Lyrics")),
                ] {
                    if ui.add(egui::Button::selectable(self.tab == t, label)).clicked() {
                        self.tab = t;
                    }
                }
            });
            if self.tab == PlotTab::Lyrics {
                // shown for every selected song, scanned or not
                let normal = ui.visuals().text_color();
                if self.lyrics_shown.as_ref().is_none_or(|(p, _)| *p != cur) {
                    self.lyrics_shown = Some((cur.clone(), lyrics_job(cur.as_deref(), normal)));
                }
                let job = self.lyrics_shown.as_ref().map(|(_, j)| j.clone()).unwrap_or_default();
                egui::ScrollArea::vertical().id_salt("lyrics").auto_shrink([false, false]).show(ui, |ui| {
                    ui.add(egui::Label::new(job).wrap());
                });
                return;
            }
            let plot = self.plot_for(r.as_ref());
            let state = match (&r, &plot) {
                (None, _) if cur.is_some() => State::Pending,
                (None, _) => State::Nothing,
                (Some(_), Some(p)) => State::Shown(p),
                (Some(r), None) if r.verdict != Verdict::Error && self.graphs.busy.as_deref() == Some(&r.path) => {
                    State::Waiting
                }
                _ => State::Missing,
            };
            if self.tab == PlotTab::Spectrum {
                plots::spectrum(ui, r.as_ref(), state);
            } else {
                plots::spectrogram(
                    ui,
                    r.as_ref(),
                    state,
                    &mut self.spectro,
                    self.cfg.spectrogram_range_db,
                    &self.cfg.colormap,
                );
            }
        });
    }

    /// The selected file's graphs: from the cache, the saved results, or drawn now.
    fn plot_for(&mut self, r: Option<&FileResult>) -> Option<realhires::analysis::Plot> {
        let r = r?;
        if let Some((p, plot)) = &self.shown_plot {
            if *p == r.path && (plot.is_some() || self.graphs.busy.as_deref() != Some(&r.path)) {
                return plot.clone();
            }
        }
        let mut plot = self.cache.get(&r.path);
        if plot.is_none() && r.saved.is_some() {
            if let Some(st) = &self.store {
                plot = st.get_plot(&r.path, &settings_key(&self.cfg));
                if let Some(p) = &plot {
                    self.cache.put(&r.path, p);
                }
            }
        }
        if plot.is_none()
            && r.verdict != Verdict::Error
            && !self.graph_failed.contains(&r.path)
            && std::path::Path::new(&r.path).exists()
            && self.graphs.busy.as_deref() != Some(&r.path)
            && !self.scanning
        {
            let partial = r.duration.is_some_and(|d| r.analysed_seconds < d - 1.0);
            self.graphs.request(&r.path, if partial { Some(r.analysed_seconds) } else { None });
        }
        self.shown_plot = Some((r.path.clone(), plot.clone()));
        plot
    }

    /// The facts grid: (English name, shown with `i18n::tr()`; value).
    fn facts(&self, r: Option<&FileResult>) -> Vec<(&'static str, String)> {
        let dash = "–".to_string();
        let Some(r) = r else {
            return [
                n_!("Claimed"),
                n_!("Real (estimated)"),
                n_!("Bandwidth"),
                n_!("Bits used"),
                n_!("Format"),
                n_!("Duration"),
                n_!("After fix"),
            ]
            .iter()
            .map(|k| (*k, dash.clone()))
            .collect();
        };
        let mut bits = r.effective_bits;
        if r.is_float && bits.is_some_and(|b| b > 24) {
            bits = Some(24);
        }
        vec![
            ("Claimed", r.claimed()),
            ("Real (estimated)", tr_text(&r.estimated())),
            (
                "Bandwidth",
                r.bandwidth_hz
                    .filter(|b| *b != 0.0)
                    .map(|b| format!("{} kHz", f(b / 1000.0, 1)))
                    .unwrap_or(dash.clone()),
            ),
            (
                "Bits used",
                match (bits.filter(|b| *b != 0), r.claimed_bits.filter(|b| *b != 0)) {
                    (Some(b), Some(c)) => tr!("{bits} of {claimed}", bits = b, claimed = c),
                    (Some(b), None) => b.to_string(),
                    _ => dash.clone(),
                },
            ),
            (
                "Format",
                if r.codec.is_empty() {
                    dash.clone()
                } else {
                    tr!("{codec}, {channels} ch", codec = &r.codec, channels = r.channels)
                },
            ),
            ("Duration", if r.duration.is_some_and(|d| d != 0.0) { fmt_duration(r.duration) } else { "?".into() }),
            ("After fix", Some(tr_text(&fix_outlook(r, &self.cfg))).filter(|s| !s.is_empty()).unwrap_or(dash.clone())),
        ]
    }

    fn details(&self, r: Option<&FileResult>, path: Option<&str>) -> String {
        let Some(r) = r else {
            return path.map(|p| format!("{p}\n")).unwrap_or_default();
        };
        // the core's reasons and notes in the language in use
        let mut parts: Vec<String> =
            r.reasons.iter().chain(&r.notes).map(|x| format!("• {}\n", tr_text(x))).collect();
        // what Fix does with it, and whether it stays fake afterwards
        if let Some(advice) = fix_advice(r, &self.cfg) {
            parts.push(format!("• {}\n", tr_text(&advice)));
        }
        if r.saved.is_some() {
            parts.insert(0, format!("• {}\n", tr_text(&saved_note(r))));
        }
        if !r.cliffs_db.is_empty() {
            let items: Vec<String> = r
                .cliffs_db
                .iter()
                .filter(|(_, v)| !v.is_nan())
                .map(|(k, v)| {
                    format!(
                        "{} kHz {}/{} dB",
                        g(*k as f64 / 2000.0),
                        f(*v, 0),
                        f(imap_get(&r.deep_cliffs_db, *k).unwrap_or(f64::NAN), 0)
                    )
                })
                .collect();
            parts.push(format!(
                "• {}\n",
                tr!("Drop at source Nyquists (narrow/deep): {values}", values = items.join(", "))
            ));
        }
        if let Some(c) = r.converter_floor_db.filter(|c| !c.is_nan()) {
            parts.push(format!(
                "• {}\n",
                tr!("26-40 kHz band: {db} dB relative to the rounding noise", db = fs(c, 0))
            ));
        }
        if let Some(s) = r.grid_step.filter(|s| *s != 0.0) {
            parts.push(format!(
                "• {}\n",
                tr!(
                    "Value grid: step {step} (24-bit LSBs), {bits}-bit master, gain {gain} dB",
                    step = f(s, 1),
                    bits = r.grid_bits.map(|b| b.to_string()).unwrap_or_default(),
                    gain = fs(r.grid_gain_db.unwrap_or(0.0), 1)
                )
            ));
        }
        parts.push(format!("\n{}\n", r.path));
        parts.concat()
    }

    pub fn export(&mut self, json: bool, all: bool) {
        let rows: Vec<FileResult> = if all {
            let mut v: Vec<FileResult> = self.results.values().cloned().collect();
            v.sort_by(|a, b| a.path.cmp(&b.path));
            v
        } else {
            self.rows.iter().filter_map(|p| self.results.get(p).cloned()).collect()
        };
        if rows.is_empty() {
            self.info = Some((tr!("Export").into(), tr!("No scanned files are listed.").into()));
            return;
        }
        let (name, ext) = if json { ("realhires-report.json", "json") } else { ("realhires-report.csv", "csv") };
        let Some(path) = rfd::FileDialog::new().set_file_name(name).add_filter(ext.to_uppercase(), &[ext]).save_file()
        else {
            return;
        };
        let p = path.to_string_lossy().into_owned();
        let res = if json { realhires::report::write_json(&rows, &p) } else { realhires::report::write_csv(&rows, &p) };
        match res {
            Ok(()) => self.status = tr!("Exported {n} files to {path}", n = rows.len(), path = &p),
            Err(e) => {
                self.info = Some((
                    tr!("Export").into(),
                    tr!("Could not write {path}: {error}", path = &p, error = tr_text(&e.to_string())),
                ))
            }
        }
    }
}

fn card_line(gr: &Group) -> (String, FontId, Color32) {
    let font = FontId::proportional(11.5);
    if gr.problems() > 0 {
        let mut parts = vec![];
        if gr.fake > 0 {
            parts.push(tr!("{n} fake", n = gr.fake));
        }
        if gr.suspicious > 0 {
            parts.push(tr!("{n} suspicious", n = gr.suspicious));
        }
        let parts = parts.join(" + ");
        let line = if gr.total == 1 {
            tr!("{parts} of 1 song", parts = parts)
        } else {
            tr!("{parts} of {total} songs", parts = parts, total = gr.total)
        };
        (line, font, hex(if gr.fake > 0 { RING_FAKE } else { RING_SUS }))
    } else {
        let line = if gr.total == 1 {
            tr!("All 1 song fine").to_string()
        } else {
            tr!("All {n} songs fine", n = gr.total)
        };
        (line, font, hex(RING_OK))
    }
}

/// The ring badge of a card: fake red, suspicious orange, fine green.
fn ring(p: &egui::Painter, center: Pos2, gr: &Group, songs: bool) {
    let (outer, inner) = (27.0f32, 18.0f32);
    p.circle_filled(center, outer + 3.0, Color32::WHITE);
    let total = gr.total.max(1) as f32;
    let mut start = 0.0f32;
    for (count, color) in [(gr.fake, RING_FAKE), (gr.suspicious, RING_SUS), (gr.total - gr.problems(), RING_OK)] {
        if count == 0 {
            continue;
        }
        let ext = count as f32 / total * std::f32::consts::TAU;
        arc(p, center, (outer + inner) / 2.0, start, start + ext, outer - inner, hex(color));
        start += ext;
    }
    let label = if songs { format!("{}/{}", gr.problems(), gr.total) } else { format!("{}%", f(gr.problem_pct(), 0)) };
    let color = if gr.fake > 0 {
        RING_FAKE
    } else if gr.suspicious > 0 {
        RING_SUS
    } else {
        RING_OK
    };
    p.text(center, Align2::CENTER_CENTER, label, FontId::proportional(11.0), hex(color));
}

/// A thick arc, clockwise from 12 o'clock (angles in radians).
pub fn arc(p: &egui::Painter, c: Pos2, radius: f32, a0: f32, a1: f32, width: f32, color: Color32) {
    let n = (((a1 - a0) / std::f32::consts::TAU * 96.0).ceil() as usize).max(2);
    let pts: Vec<Pos2> = (0..=n)
        .map(|i| {
            let a = a0 + (a1 - a0) * i as f32 / n as f32;
            Pos2::new(c.x + radius * a.sin(), c.y - radius * a.cos())
        })
        .collect();
    p.add(egui::Shape::line(pts, Stroke::new(width, color)));
}

#[cfg(test)]
mod tests {
    //! Drives the whole app headlessly (egui needs no window to run a frame):
    //! listing, scanning, the summary pop-up, saved results on the next start,
    //! the album view and a real fix.
    use super::*;
    use std::process::Command;
    use std::time::{Duration, Instant};

    fn ffmpeg(args: &[&str]) {
        let st = Command::new(realhires::probe::tool("ffmpeg"))
            .args(["-v", "error", "-y"])
            .args(args)
            .status()
            .expect("ffmpeg");
        assert!(st.success(), "{args:?}");
    }

    fn frame(ctx: &egui::Context, app: &mut App) {
        let input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(Pos2::ZERO, Vec2::new(1280.0, 880.0))),
            ..Default::default()
        };
        ctx.run_ui(input, |ui| app.ui(ui)).drop_without_applying_deltas();
    }

    fn run_until(ctx: &egui::Context, app: &mut App, what: &str, done: impl Fn(&App) -> bool) {
        let t0 = Instant::now();
        while !done(app) {
            assert!(t0.elapsed() < Duration::from_secs(120), "timed out waiting for {what}: {}", app.status);
            frame(ctx, app);
            std::thread::sleep(Duration::from_millis(30));
        }
    }

    fn verdict(app: &App, name: &str) -> Verdict {
        app.results.values().find(|r| r.path.ends_with(name)).unwrap_or_else(|| panic!("no result for {name}")).verdict
    }

    #[test]
    fn scan_summary_saved_results_albums_and_fix() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-gui-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Artist").join("Album");
        std::fs::create_dir_all(&music).unwrap();
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        let p = |n: &str| music.join(n).to_string_lossy().into_owned();
        let noise = |rate: u32| format!("anoisesrc=r={rate}:a=0.2:c=pink:d=4:seed=7");
        ffmpeg(&["-f", "lavfi", "-i", &noise(96000), "-c:a", "pcm_s24le", "-ac", "2", &p("01 real.wav")]);
        ffmpeg(&["-f", "lavfi", "-i", &noise(44100), "-c:a", "pcm_s16le", "-ac", "2", &p("02 cd.wav")]);
        ffmpeg(&[
            "-f",
            "lavfi",
            "-i",
            &noise(44100),
            "-ar",
            "96000",
            "-c:a",
            "pcm_s24le",
            "-ac",
            "2",
            &p("03 fake.wav"),
        ]);

        let ctx = egui::Context::default();
        let dir = music.to_string_lossy().into_owned();
        let mut app = App::new(&ctx, Settings::default(), vec![dir.clone()]);
        run_until(&ctx, &mut app, "the scan", |a| a.results.len() == 3 && !a.scanning);
        assert_eq!(verdict(&app, "01 real.wav"), Verdict::Real);
        assert_eq!(verdict(&app, "02 cd.wav"), Verdict::NotHires);
        assert_eq!(verdict(&app, "03 fake.wav"), Verdict::Fake);
        assert!(app.status.starts_with("Finished: 3 files"), "{}", app.status);
        // the problem filter is the default, so only the fake file is listed
        assert_eq!(app.rows.len(), 1);
        run_until(&ctx, &mut app, "the summary pop-up", |a| a.summary.is_some());
        // the selected file's graphs come from the scan
        app.select_only(&app.rows[0].clone());
        frame(&ctx, &mut app);
        assert!(app.shown_plot.as_ref().is_some_and(|(_, p)| p.as_ref().is_some_and(|p| p.spectrogram.is_some())));

        // Album view: one album, one card.
        app.summary = None;
        app.switch_view(View::Album);
        frame(&ctx, &mut app);
        assert_eq!(app.groups.len(), 1);
        assert_eq!(app.groups[0].title, "Album");
        assert_eq!((app.groups[0].fake, app.groups[0].total), (1, 3));
        let g = app.groups[0].clone();
        app.open_group(&g);
        assert_eq!(app.view, View::Files);
        assert_eq!(app.rows.len(), 3); // every verdict of the album, in track order
        assert!(app.rows[0].ends_with("01 real.wav"));
        app.close_group(true);
        assert_eq!(app.view, View::Album);
        app.switch_view(View::Files);

        // A second start lists the folder and shows the saved results without a scan.
        let ctx2 = egui::Context::default();
        let mut again = App::new(&ctx2, Settings::default(), vec![]);
        again.add_source(&dir);
        run_until(&ctx2, &mut again, "the saved results", |a| {
            a.results.len() == 3 && a.lookup_busy == 0 && a.listing.is_empty()
        });
        assert!(again.results.values().all(|r| r.saved.is_some()));
        assert!(again.status.contains("3 of 3 files were checked before"), "{}", again.status);
        assert!(!again.scanning);

        // The list, the facts and the details say what Fix does with the fake file.
        let fake_path = app.results.values().find(|r| r.verdict == Verdict::Fake).unwrap().path.clone();
        let (texts, _) = app.row_texts(&fake_path, &HashSet::new());
        assert_eq!(texts[5], "Fixable to 16/44.1");
        let facts = app.facts(app.results.get(&fake_path));
        assert!(facts.contains(&("After fix", "Fixable to 16/44.1".to_string())), "{facts:?}");
        let details = app.details(app.results.get(&fake_path), Some(&fake_path));
        assert!(
            details.contains("• Fix to real resolution converts it to 16/44.1, the resolution it really has"),
            "{details}"
        );

        // Fix the fake file in place, with the Fix window closed (hidden) while it works:
        // the list is still updated and the pop-up still appears.
        let fake: Vec<FileResult> = app.results.values().filter(|r| r.verdict == Verdict::Fake).cloned().collect();
        app.open_fix(fake, false);
        let mut w = app.fix_win.take().unwrap();
        w.start(&mut app);
        w.hidden = true;
        app.fix_win = Some(w);
        run_until(&ctx, &mut app, "the fix", |a| a.status.starts_with("Fix: "));
        assert_eq!(app.status, "Fix: 1 fixed.");
        assert!(music.join("Real-Hi-Res backups").join("03 fake.wav").exists());
        let fixed = app.results.values().find(|r| r.path.ends_with("03 fake.wav")).unwrap();
        assert_eq!((fixed.verdict, fixed.claimed()), (Verdict::NotHires, "16/44.1".to_string()));
        assert!(app.fix_win.is_none());
        let done = app.fix_done.as_ref().expect("the pop-up after fixing");
        assert_eq!(done.headline, "Fixed 1 file to their real resolution");
        assert!(done.lines.iter().any(|l| l.starts_with("They now take ")), "{:?}", done.lines);
        assert!(done.lines.iter().any(|l| l == "Re-check of the fixed files: 1 CD quality."), "{:?}", done.lines);
        frame(&ctx, &mut app);
        assert!(app.fix_done.is_some());

        // Synced lyrics from a fake LRCLIB on this computer, then the Lyrics tab shows them.
        let server = fake_lrclib();
        std::env::set_var(realhires::lyrics::INTERVAL_ENV, "0");
        let song_dir = tmp.join("Lyrics");
        std::fs::create_dir_all(&song_dir).unwrap();
        let song = song_dir.join("Song.flac").to_string_lossy().into_owned();
        ffmpeg(&[
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-metadata",
            "title=Song",
            "-metadata",
            "artist=Band",
            "-c:a",
            "flac",
            &song,
        ]);
        app.cfg.lyrics_server = server;
        app.fix_done = None;
        app.download_lyrics(vec![song.clone()]);
        let w = app.lyrics_win.as_ref().unwrap();
        assert_eq!(w.head, "Download synced lyrics for 1 song");
        assert!(w.note.starts_with("LRCLIB asks programs to send one request at a time, so this takes"), "{}", w.note);
        let mut w = app.lyrics_win.take().unwrap();
        w.start(&mut app);
        app.lyrics_win = Some(w);
        run_until(&ctx, &mut app, "the lyrics", |a| a.lyrics_win.as_ref().is_some_and(|w| !w.running));
        let w = app.lyrics_win.as_ref().unwrap();
        assert_eq!(w.status, "1 with synced lyrics.");
        assert_eq!(w.head, "Lyrics saved for 1 of 1 song");
        let lrc = song_dir.join("Song.lrc");
        assert_eq!(std::fs::read_to_string(&lrc).unwrap(), "[00:01.00] la la\n[00:02.50] second line\n");
        // a second window skips the song, which now has an .lrc
        app.lyrics_win = None;
        app.download_lyrics(vec![song.clone()]);
        assert_eq!(app.lyrics_win.as_ref().unwrap().head, "Nothing to look up");
        app.lyrics_win = None;
        app.current = Some(song.clone());
        app.tab = PlotTab::Lyrics;
        frame(&ctx, &mut app);
        let text = app.lyrics_shown.as_ref().map(|(_, j)| j.text.clone()).unwrap_or_default();
        assert!(text.starts_with("Synced lyrics · Song.lrc") && text.contains("[00:01.00]  la la"), "{text}");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// The texts of a frame, joined by newlines (what the window shows).  Two
    /// frames are run first, since egui lays a new window or grid out unseen.
    fn frame_texts(ctx: &egui::Context, app: &mut App) -> String {
        frame(ctx, app);
        frame(ctx, app);
        fn collect(shape: &egui::epaint::Shape, out: &mut Vec<String>) {
            match shape {
                egui::epaint::Shape::Text(t) => out.push(t.galley.text().to_string()),
                egui::epaint::Shape::Vec(v) => v.iter().for_each(|s| collect(s, out)),
                _ => {}
            }
        }
        let input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(Pos2::ZERO, Vec2::new(1280.0, 880.0))),
            ..Default::default()
        };
        let out = ctx.run_ui(input, |ui| app.ui(ui));
        let mut texts = vec![];
        for c in &out.shapes {
            collect(&c.shape, &mut texts);
        }
        out.drop_without_applying_deltas();
        texts.join("\n")
    }

    /// The Japanese of an English text, which must have one.
    fn ja(text: &str) -> String {
        let t = realhires::i18n::japanese().strings.get(text).cloned().unwrap_or_else(|| panic!("no Japanese for {text:?}"));
        assert_ne!(t, text);
        t
    }

    /// The Quality check page and its windows in Japanese (2.1): the list, the
    /// dashboard, the status line, the summary, every Settings tab, the Fix
    /// window and the pop-up after it, a confirmation, the lyrics window and
    /// the Lyrics tab.
    #[test]
    fn quality_check_page_in_japanese() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        realhires::i18n::set_thread_language("日本語");
        let tmp = std::env::temp_dir().join(format!("rhr-gui-ja-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Artist").join("Album");
        std::fs::create_dir_all(&music).unwrap();
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        let p = |n: &str| music.join(n).to_string_lossy().into_owned();
        let noise = |rate: u32| format!("anoisesrc=r={rate}:a=0.2:c=pink:d=4:seed=7");
        ffmpeg(&["-f", "lavfi", "-i", &noise(44100), "-c:a", "pcm_s16le", "-ac", "2", &p("01 cd.wav")]);
        ffmpeg(&["-f", "lavfi", "-i", &noise(44100), "-ar", "96000", "-c:a", "pcm_s24le", "-ac", "2", &p("02 fake.wav")]);

        let ctx = egui::Context::default();
        let dir = music.to_string_lossy().into_owned();
        let mut app = App::new(&ctx, Settings::default(), vec![dir.clone()]);
        assert_eq!(app.status, ja("Looking for audio files in {path} …").replace("{path}", &dir));
        run_until(&ctx, &mut app, "the scan", |a| a.results.len() == 2 && !a.scanning);
        // the status line, the list and the dashboard
        assert!(app.status.starts_with(&ja("Finished")), "{}", app.status);
        assert!(!app.status.contains("fake"), "{}", app.status);
        let fake = app.results.values().find(|r| r.verdict == Verdict::Fake).unwrap().clone();
        let (texts, _) = app.row_texts(&fake.path, &HashSet::new());
        assert_eq!(texts[0], ja("Fake hi-res"));
        app.select_only(&fake.path);
        let page = frame_texts(&ctx, &mut app);
        for t in ["File", "Edit", "View", "Help", "Scan", "Sources to scan", "Verdict", "Why", "After fix", "Details",
            "Spectrogram", "Average spectrum", "Lyrics", "Open Folder", "Get lyrics…", "Show:", "Search:"]
        {
            assert!(page.contains(&ja(t)), "{t:?} is not shown in Japanese:\n{page}");
        }
        assert!(page.contains(&ja("Fake hi-res").to_uppercase()), "the badge:\n{page}");
        let facts = app.facts(Some(&fake));
        assert!(page.contains(&tr!("{fact}:", fact = i18n::tr(facts[0].0))), "{page}");

        // the summary pop-up
        run_until(&ctx, &mut app, "the summary pop-up", |a| a.summary.is_some());
        let summary = frame_texts(&ctx, &mut app);
        for t in ["Scan summary", "fake or suspicious", "Show this summary after every scan", "Show problem files"] {
            assert!(summary.contains(&ja(t)), "{t:?} is not shown in Japanese:\n{summary}");
        }
        let head = ja("Scanned {n} files in {seconds} s").replace("{n}", "2");
        assert!(summary.contains(head.split("{seconds}").next().unwrap()), "{summary}");
        app.summary = None;

        // every tab of the Settings window: the group names, the first setting and the buttons
        for (i, group) in settings::GROUPS.iter().enumerate() {
            let mut w = SettingsWindow::new(&app.cfg);
            w.tab = i;
            app.settings_win = Some(w);
            let shown = frame_texts(&ctx, &mut app);
            for g in settings::GROUPS {
                assert!(shown.contains(i18n::tr(g)), "the tab {g:?} is not shown:\n{shown}");
            }
            let first = Settings::field_info().into_iter().find(|f| f.group == *group).unwrap();
            assert!(shown.contains(i18n::tr(first.label)), "{:?} is not shown on {group}:\n{shown}", first.label);
            for t in ["Settings", "Restore defaults", "Cancel", "Save"] {
                assert!(shown.contains(&ja(t)), "{t:?} is not shown in Japanese on {group}:\n{shown}");
            }
        }
        app.settings_win = None;

        // the album view
        app.switch_view(View::Album);
        let cards = frame_texts(&ctx, &mut app);
        assert!(cards.contains(&ja("Rings show:")) && cards.contains(&ja("By album")), "{cards}");
        assert!(cards.contains(&tr!("{parts} of {total} songs", parts = tr!("{n} fake", n = 1), total = 2)), "{cards}");
        app.switch_view(View::Files);

        // the Fix window, then the pop-up after fixing
        app.open_fix(vec![fake.clone()], false);
        let fix = frame_texts(&ctx, &mut app);
        for t in ["Fix to real resolution", "Fix 1 file: real resolution", "Claimed → real", "Fix files", "Close"] {
            assert!(fix.contains(&ja(t)), "{t:?} is not shown in Japanese:\n{fix}");
        }
        app.fix_win = None;
        app.fix_done = Some(FixDoneWindow::new(&[], 0));
        let done = frame_texts(&ctx, &mut app);
        assert!(done.contains(&ja("Fixing finished")) && done.contains(&ja("Show in folder")), "{done}");
        app.fix_done = None;

        // a confirmation
        app.forget_saved_ask();
        let ask = frame_texts(&ctx, &mut app);
        for t in ["Delete all saved results", "Yes", "No"] {
            assert!(ask.contains(&ja(t)), "{t:?} is not shown in Japanese:\n{ask}");
        }
        app.confirm = None;

        // the lyrics window and the Lyrics tab of a song without lyrics
        app.download_lyrics(vec![fake.path.clone()]);
        assert_eq!(app.lyrics_win.as_ref().unwrap().head, ja("Download synced lyrics for 1 song"));
        let lyrics = frame_texts(&ctx, &mut app);
        for t in ["Download synced lyrics", "Artist – title", "Download lyrics", "Options", "Songs that already have lyrics:"] {
            assert!(lyrics.contains(&ja(t)), "{t:?} is not shown in Japanese:\n{lyrics}");
        }
        app.lyrics_win = None;
        app.tab = PlotTab::Lyrics;
        let tab = frame_texts(&ctx, &mut app);
        assert!(tab.contains(&ja("No lyrics next to this song yet.")), "{tab}");
        app.tab = PlotTab::Spectrogram;
        let graphs = frame_texts(&ctx, &mut app);
        assert!(
            graphs.contains(&ja("Spectrogram (time →, frequency ↑)")) || graphs.contains(&ja("Drawing the spectrogram…")),
            "{graphs}"
        );

        // English again on this thread
        realhires::i18n::set_thread_language("");
        assert_eq!(tr!("Scan summary"), "Scan summary");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// One song on a fake LRCLIB: /api/get answers with its synced lyrics.
    fn fake_lrclib() -> String {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut s) = stream else { continue };
                let mut req = vec![];
                let mut buf = [0u8; 1024];
                while !req.windows(4).any(|w| w == b"\r\n\r\n") {
                    match s.read(&mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => req.extend_from_slice(&buf[..n]),
                    }
                }
                let line = String::from_utf8_lossy(&req).lines().next().unwrap_or("").to_string();
                let (status, body) = if line.starts_with("GET /api/get?") && line.contains("track_name=Song") {
                    (
                        "200 OK",
                        r#"{"id": 1, "trackName": "Song", "artistName": "Band", "albumName": "", "duration": 4.0, "instrumental": false, "plainLyrics": "la la\nsecond line", "syncedLyrics": "[00:01.00] la la\n[00:02.50] second line"}"#,
                    )
                } else {
                    ("404 Not Found", r#"{"message": "Failed to find specified track"}"#)
                };
                let _ = write!(
                    s,
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        url
    }
}

/// A .json file dropped onto the window is a theme for the player, not a
/// source to scan.
pub fn is_theme_file(path: &std::path::Path) -> bool {
    path.extension().is_some_and(|e| e.eq_ignore_ascii_case("json")) && path.is_file()
}
