//! Background work for the desktop app: listing sources, looking up saved
//! results, scanning, drawing graphs, loading covers and fixing.  Every thread
//! posts `Msg`s to the UI thread and asks egui to repaint.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use realhires::analysis::Plot;
use realhires::eta::file_sizes;
use realhires::fix::{prepare_gapless, run_fix, FixOutcome, FixPlan};
use realhires::lyrics::{fetch_for_file, LrclibClient, LyricsOutcome};
use realhires::paths::{abspath, walk_audio_files};
use realhires::settings::Settings;
use realhires::store::{open_store, plot_from_blobs, plot_to_blobs, settings_key};
use realhires::verdict::{analyze_file, FileResult};

pub enum Msg {
    Listing { source: String, batch: Vec<String> },
    Listed { source: String },
    Saved { gen: u64, results: Vec<FileResult> },
    SavedDone { gen: u64 },
    ScanTotal { scan: u64, total: usize },
    Analysing { scan: u64, todo: Vec<String>, sizes: HashMap<String, u64> },
    Result { scan: u64, result: Box<FileResult> },
    ScanDone { scan: u64, stopped: bool },
    Cover { key: String, png: Option<Vec<u8>> },
    Graphs { path: String, plot: Option<Plot> },
    FixStatus(String),
    FixStart(String),
    FixDone(Box<FixOutcome>),
    FixFinished,
    LyricsStart { run: u64, path: String },
    LyricsDone { run: u64, outcome: Box<LyricsOutcome> },
    LyricsFinished { run: u64, stopped: bool },
    /// "Lyrics while scanning" (2.0): one song done by the scan's lyrics worker.
    ScanLyrics { worker: u64, outcome: Box<LyricsOutcome> },
}

/// Sends messages to the UI thread and wakes it up.
#[derive(Clone)]
pub struct Post {
    tx: Sender<Msg>,
    ctx: egui::Context,
}

impl Post {
    pub fn new(ctx: egui::Context) -> (Post, Receiver<Msg>) {
        let (tx, rx) = mpsc::channel();
        (Post { tx, ctx }, rx)
    }
    pub fn send(&self, m: Msg) {
        let _ = self.tx.send(m);
        self.ctx.request_repaint();
    }
}

// ----------------------------------------------------------------- listing ---

/// Find the audio files of one added folder or file, posting them in batches
/// (every 500 files or 0.25 s), so big libraries appear progressively.
pub fn list_source(source: String, cfg: &Settings, post: Post, cancel: Arc<AtomicBool>) {
    let exts = cfg.extension_set();
    let recursive = cfg.recursive;
    std::thread::spawn(move || {
        let mut batch = vec![];
        let mut last = Instant::now();
        walk_audio_files(&source, recursive, &exts, &mut |f| {
            if cancel.load(Ordering::Relaxed) {
                return false;
            }
            batch.push(abspath(&f));
            if batch.len() >= 500 || last.elapsed() > Duration::from_millis(250) {
                post.send(Msg::Listing { source: source.clone(), batch: std::mem::take(&mut batch) });
                last = Instant::now();
            }
            true
        });
        if cancel.load(Ordering::Relaxed) {
            return;
        }
        if !batch.is_empty() {
            post.send(Msg::Listing { source: source.clone(), batch });
        }
        post.send(Msg::Listed { source });
    });
}

// ------------------------------------------------------------ saved results ---

pub struct LookupRequest {
    pub gen: u64,
    pub cfg: Settings,
    pub wanted: Option<f64>,
    pub paths: Vec<String>,
}

/// A thread that answers lookups of saved results for newly listed files, so
/// files checked before show their verdict without a scan.
pub fn start_lookup(post: Post) -> Sender<LookupRequest> {
    let (tx, rx) = mpsc::channel::<LookupRequest>();
    std::thread::spawn(move || {
        let mut store = None;
        let mut store_key = (false, -1i64);
        while let Ok(req) = rx.recv() {
            let key = (req.cfg.save_results, req.cfg.saved_graphs_mb);
            if store.is_none() || key != store_key {
                store = open_store(&req.cfg);
                store_key = key;
            }
            let mut found = vec![];
            if let Some(st) = store.as_ref() {
                let skey = settings_key(&req.cfg);
                for p in &req.paths {
                    if let Some(r) = st.lookup(p, &skey, req.wanted) {
                        found.push(r);
                    }
                    if found.len() >= 200 {
                        post.send(Msg::Saved { gen: req.gen, results: std::mem::take(&mut found) });
                    }
                }
            }
            if !found.is_empty() {
                post.send(Msg::Saved { gen: req.gen, results: found });
            }
            post.send(Msg::SavedDone { gen: req.gen });
        }
    });
    tx
}

// ----------------------------------------------------------------- scanning ---

pub struct ScanJob {
    pub stop: Arc<AtomicBool>,
}

/// Post the saved result of every unchanged file, analyse the others in
/// `jobs` threads, save each new result with its graphs and post it.  `force`
/// analyses every file again.
pub fn start_scan(
    id: u64,
    files: Vec<String>,
    cfg: Settings,
    max_seconds: Option<f64>,
    force: bool,
    jobs: usize,
    post: Post,
) -> ScanJob {
    let stop = Arc::new(AtomicBool::new(false));
    let stop2 = stop.clone();
    std::thread::spawn(move || {
        post.send(Msg::ScanTotal { scan: id, total: files.len() });
        let mut store = open_store(&cfg);
        let skey = settings_key(&cfg);
        let mut todo = files.clone();
        if let (Some(st), false) = (store.as_ref(), force) {
            todo.clear();
            for f in &files {
                if stop2.load(Ordering::Relaxed) {
                    break;
                }
                match st.lookup(f, &skey, max_seconds) {
                    Some(r) if !(r.saved.as_ref().is_some_and(|s| s.outdated) && cfg.rescan_outdated) => {
                        post.send(Msg::Result { scan: id, result: Box::new(r) })
                    }
                    _ => todo.push(f.clone()),
                }
            }
        }
        if todo.is_empty() || stop2.load(Ordering::Relaxed) {
            post.send(Msg::ScanDone { scan: id, stopped: stop2.load(Ordering::Relaxed) });
            return;
        }
        post.send(Msg::Analysing { scan: id, sizes: file_sizes(todo.iter()), todo: todo.clone() });
        let (rtx, rrx) = mpsc::channel::<FileResult>();
        let next = Arc::new(AtomicUsize::new(0));
        let todo = Arc::new(todo);
        let cfg = Arc::new(cfg);
        let workers = jobs.min(todo.len()).max(1);
        for _ in 0..workers {
            let (rtx, next, todo, cfg, stop) = (rtx.clone(), next.clone(), todo.clone(), cfg.clone(), stop2.clone());
            std::thread::spawn(move || loop {
                if stop.load(Ordering::Relaxed) {
                    break;
                }
                let i = next.fetch_add(1, Ordering::SeqCst);
                let Some(f) = todo.get(i) else { break };
                let r = analyze_file(f, max_seconds, false, true, Some(&cfg));
                if rtx.send(r).is_err() {
                    break;
                }
            });
        }
        drop(rtx);
        for r in rrx {
            if let Some(st) = store.as_mut() {
                st.put(&r, &skey, max_seconds, r.plot.as_ref());
            }
            post.send(Msg::Result { scan: id, result: Box::new(r) });
        }
        post.send(Msg::ScanDone { scan: id, stopped: stop2.load(Ordering::Relaxed) });
    });
    ScanJob { stop }
}

// ------------------------------------------------------------------ graphs ---

/// Draws the graphs of saved results whose graphs were not saved, one at a
/// time; only the latest request waits.
/// The next file to draw graphs for, and how many seconds of it.
type GraphJob = Arc<Mutex<Option<(String, Option<f64>)>>>;

pub struct GraphWorker {
    next: GraphJob,
    wake: Sender<()>,
    pub busy: Option<String>,
}

impl GraphWorker {
    pub fn start(cfg: Settings, post: Post) -> GraphWorker {
        let next: GraphJob = Arc::new(Mutex::new(None));
        let (wake, rx) = mpsc::channel::<()>();
        let slot = next.clone();
        std::thread::spawn(move || {
            while rx.recv().is_ok() {
                let job = slot.lock().unwrap().take();
                if let Some((path, secs)) = job {
                    let r = analyze_file(&path, secs, false, true, Some(&cfg));
                    post.send(Msg::Graphs { path, plot: r.plot });
                }
            }
        });
        GraphWorker { next, wake, busy: None }
    }

    pub fn request(&mut self, path: &str, seconds: Option<f64>) {
        *self.next.lock().unwrap() = Some((path.to_string(), seconds));
        self.busy = Some(path.to_string());
        let _ = self.wake.send(());
    }
}

// ------------------------------------------------------------------- covers ---

pub fn start_covers(post: Post) -> Sender<(String, Vec<String>)> {
    let (tx, rx) = mpsc::channel::<(String, Vec<String>)>();
    std::thread::spawn(move || {
        while let Ok((key, tracks)) = rx.recv() {
            let png = realhires::covers::cover_png(&tracks, 150);
            post.send(Msg::Cover { key, png });
        }
    });
    tx
}

// --------------------------------------------------------------------- fix ---

/// Read the gapless joins first (before any file is replaced), then fix the
/// files in up to 8 threads.
pub fn start_fix(mut plans: Vec<FixPlan>, everything: Vec<FileResult>, cfg: Settings, jobs: usize, post: Post) {
    std::thread::spawn(move || {
        let p2 = post.clone();
        let joins = prepare_gapless(&mut plans, &everything, &mut |m| p2.send(Msg::FixStatus(m.to_string())));
        if joins > 0 {
            post.send(Msg::FixStatus(realhires::tr!("Fixing… ({n} gapless join(s) kept seamless)", n = joins)));
        }
        let next = AtomicUsize::new(0);
        std::thread::scope(|s| {
            for _ in 0..jobs.clamp(1, 8).min(plans.len().max(1)) {
                s.spawn(|| loop {
                    let i = next.fetch_add(1, Ordering::SeqCst);
                    let Some(p) = plans.get(i) else { break };
                    post.send(Msg::FixStart(p.source.clone()));
                    let o = run_fix(p, &cfg);
                    post.send(Msg::FixDone(Box::new(o)));
                });
            }
        });
        post.send(Msg::FixFinished);
    });
}

// ------------------------------------------------------------------ lyrics ---

/// Look the songs up on LRCLIB one at a time (LRCLIB wants sequential
/// requests), with one client, until done or stopped.
pub fn start_lyrics(
    todo: Vec<String>,
    cfg: Settings,
    results: HashMap<String, FileResult>,
    stop: Arc<AtomicBool>,
    run: u64,
    post: Post,
) {
    std::thread::spawn(move || {
        let mut client = LrclibClient::new(&cfg.lyrics_server).with_stop(stop.clone());
        for p in &todo {
            if stop.load(Ordering::SeqCst) {
                break;
            }
            post.send(Msg::LyricsStart { run, path: p.clone() });
            match fetch_for_file(p, &mut client, &cfg, results.get(p), false) {
                Ok(o) => post.send(Msg::LyricsDone { run, outcome: Box::new(o) }),
                Err(_) => break, // stopped
            }
        }
        post.send(Msg::LyricsFinished { run, stopped: stop.load(Ordering::SeqCst) });
    });
}

/// The scan's lyrics worker (2.0, `lyrics_while_scanning`): one thread with one
/// LRCLIB client that takes the scanned songs in turn, as LRCLIB asks.
pub struct ScanLyrics {
    pub tx: Sender<(String, Option<FileResult>)>,
    pub stop: Arc<AtomicBool>,
    pub id: u64,
}

pub fn start_scan_lyrics(cfg: Settings, id: u64, post: Post) -> ScanLyrics {
    let (tx, rx) = mpsc::channel::<(String, Option<FileResult>)>();
    let stop = Arc::new(AtomicBool::new(false));
    let flag = stop.clone();
    std::thread::spawn(move || {
        let mut client = LrclibClient::new(&cfg.lyrics_server).with_stop(flag.clone());
        while let Ok((path, result)) = rx.recv() {
            if flag.load(Ordering::SeqCst) {
                break;
            }
            match fetch_for_file(&path, &mut client, &cfg, result.as_ref(), false) {
                Ok(o) => post.send(Msg::ScanLyrics { worker: id, outcome: Box::new(o) }),
                Err(_) => break, // stopped
            }
        }
    });
    ScanLyrics { tx, stop, id }
}

// --------------------------------------------------------------- plot cache ---

/// Keeps each file's graphs on disk instead of in memory, so libraries with
/// thousands of files do not fill the RAM.
pub struct PlotCache {
    dir: PathBuf,
    index: HashMap<String, PathBuf>,
}

impl PlotCache {
    pub fn new() -> PlotCache {
        let nanos =
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
        let dir = std::env::temp_dir().join(format!("realhires-{}-{nanos:x}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        PlotCache { dir, index: HashMap::new() }
    }

    pub fn put(&mut self, path: &str, plot: &Plot) {
        let (g, s) = plot_to_blobs(plot);
        let file = self.dir.join(format!("{}.plot", self.index.len()));
        let mut data = (g.len() as u64).to_le_bytes().to_vec();
        data.extend_from_slice(&g);
        if let Some(s) = s {
            data.extend_from_slice(&s);
        }
        if std::fs::File::create(&file).and_then(|mut f| f.write_all(&data)).is_ok() {
            self.index.insert(path.to_string(), file);
        }
    }

    pub fn get(&self, path: &str) -> Option<Plot> {
        let file = self.index.get(path)?;
        let mut data = vec![];
        std::fs::File::open(file).ok()?.read_to_end(&mut data).ok()?;
        let n = u64::from_le_bytes(data.get(..8)?.try_into().ok()?) as usize;
        let g = data.get(8..8 + n)?;
        let s = &data[8 + n..];
        plot_from_blobs(g, if s.is_empty() { None } else { Some(s) })
    }

    pub fn clear(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
        let _ = std::fs::create_dir_all(&self.dir);
        self.index.clear();
    }
}

impl Drop for PlotCache {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}
