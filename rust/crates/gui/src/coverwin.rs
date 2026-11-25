//! The Cover window (2.1, `docs/player-spec.md` → "Inspecting and changing a
//! cover"), the counterpart of Python's `player_windows.CoverWindow`: the cover
//! of a song's album as large as the window allows, its size in pixels, format
//! and where it comes from (`covers::cover_source()`), and Change cover…, Search
//! Cover Online (2.3: Google Images in the browser, `covers::cover_search_url()`),
//! Save image as…, Remove cover (confirmed in the window), Show album and Close
//! (Esc).  Reading the cover and changing or removing it run in threads
//! (`tagedit::set_cover()`/`remove_cover()`), for all the album's songs or this
//! song only, with a progress line and `tagedit::cover_summary()` afterwards.

use std::sync::mpsc::{self, Receiver, Sender};

use egui::{pos2, vec2, Align2, Color32, CornerRadius, Rect, RichText, Sense};
use realhires::covers::{self, CoverSource};
use realhires::i18n::{tr_name, tr_text};
use realhires::musiclib::{Album, Track};
use realhires::settings::Settings;
use realhires::stats::thousands;
use realhires::tagedit::{self, TagOutcome};
use realhires::tr;

use crate::look::Palette;

/// The largest picture made for the window (egui scales it to fit).
const SHOW_MAX: u32 = 2048;

enum Msg {
    Source(u64, Option<CoverSource>, Option<egui::ColorImage>),
    Progress(usize, usize),
    Done { removing: bool, outcomes: Vec<TagOutcome>, notes: Vec<String> },
}

/// What the window asks of the player after a frame.
#[derive(Debug, PartialEq)]
pub enum Outcome {
    Open,
    Closed,
    /// Show album: the album's key (the window closed)
    ShowAlbum(String),
    /// covers were changed or removed: the covers are drawn again and the library reads the songs
    Changed,
}

pub struct CoverWin {
    pub path: String,
    pub album_key: Option<String>,
    pub heading: String,
    pub subheading: String,
    /// the album's songs (this one alone when it has no album)
    pub album_paths: Vec<String>,
    /// "All N songs of this album" (else "This song only")
    pub all_songs: bool,
    /// "Also save it as cover.jpg in the album's folder"
    pub also_folder: bool,
    /// Search Cover Online's address: Google Images for the album artist and album
    /// (a song without an album: its artist and title), from the song's tags
    pub search_url: String,
    pub source: Option<CoverSource>,
    pub reading: bool,
    pub busy: bool,
    removing: bool,
    progress: Option<(usize, usize)>,
    /// the summary after changing or removing (headline and sentences, one a line)
    pub summary: String,
    pub status: String,
    /// "Remove the cover…?" is asked in the window
    pub confirm_remove: bool,
    pub outcomes: Vec<TagOutcome>,
    tex: Option<egui::TextureHandle>,
    ctx: egui::Context,
    tx: Sender<Msg>,
    rx: Receiver<Msg>,
    gen: u64,
    cfg: Settings,
}

impl CoverWin {
    pub fn new(ctx: &egui::Context, path: &str, album: Option<&Album>, track: Option<&Track>, cfg: &Settings) -> CoverWin {
        let (tx, rx) = mpsc::channel();
        let mut album_paths: Vec<String> = match album {
            Some(a) => a.tracks.iter().map(|t| t.path.clone()).filter(|p| std::path::Path::new(p).is_file()).collect(),
            None => vec![],
        };
        if !album_paths.iter().any(|p| p == path) {
            album_paths.insert(0, path.to_string());
        }
        let n = album_paths.len();
        let (heading, subheading) = match (album, track) {
            (Some(a), _) => {
                let count = crate::player::plural(n, "song");
                let artist = tr_name(&a.artist);
                (tr_name(&a.title).to_string(), [artist, count.as_str()].iter().filter(|x| !x.is_empty()).cloned().collect::<Vec<_>>().join(" · "))
            }
            (None, Some(t)) => (
                t.display_title(),
                [t.artist.as_str(), t.album.as_str()].iter().filter(|x| !x.is_empty()).cloned().collect::<Vec<_>>().join(" · "),
            ),
            (None, None) => (realhires::paths::file_name(path), String::new()),
        };
        let search_url = match track {
            Some(t) => covers::cover_search_url(&t.album_artist, &t.artist, &t.album, &t.display_title()),
            None => covers::cover_search_url("", "", "", &realhires::paths::stem(path)),
        };
        let mut w = CoverWin {
            search_url,
            path: path.to_string(),
            album_key: album.map(|a| a.key.clone()),
            heading,
            subheading,
            all_songs: n > 1,
            album_paths,
            also_folder: false,
            source: None,
            reading: true,
            busy: false,
            removing: false,
            progress: None,
            summary: String::new(),
            status: tr!("Reading the cover…").into(),
            confirm_remove: false,
            outcomes: vec![],
            tex: None,
            ctx: ctx.clone(),
            tx,
            rx,
            gen: 0,
            cfg: cfg.clone(),
        };
        w.read();
        w
    }

    /// The songs whose cover is shown: this one first, then the album's.
    pub fn shown_paths(&self) -> Vec<String> {
        let mut v = vec![self.path.clone()];
        v.extend(self.album_paths.iter().filter(|p| **p != self.path).cloned());
        v
    }

    /// The songs that Change cover… and Remove cover work on.
    pub fn target_paths(&self) -> Vec<String> {
        if self.all_songs { self.album_paths.clone() } else { vec![self.path.clone()] }
    }

    fn folder(&self) -> String {
        realhires::paths::parent(&realhires::paths::abspath(&self.path))
    }

    pub fn idle(&self) -> bool {
        !self.busy && !self.reading
    }

    fn read(&mut self) {
        self.reading = true;
        self.gen += 1;
        let (gen, paths, tx, ctx) = (self.gen, self.shown_paths(), self.tx.clone(), self.ctx.clone());
        let _ = std::thread::Builder::new().name("cover-read".into()).spawn(move || {
            let src = covers::cover_source(&paths);
            let image = src.as_ref().and_then(|s| covers::fit_png(&s.data, SHOW_MAX, false)).and_then(|png| {
                let rgba = image::load_from_memory(&png).ok()?.to_rgba8();
                Some(egui::ColorImage::from_rgba_unmultiplied([rgba.width() as usize, rgba.height() as usize], rgba.as_raw()))
            });
            let _ = tx.send(Msg::Source(gen, src, image));
            ctx.request_repaint();
        });
    }

    fn start(&mut self, image: Option<Vec<u8>>) {
        self.busy = true;
        self.removing = image.is_none();
        self.summary.clear();
        self.progress = None;
        let paths = self.target_paths();
        let n = paths.len();
        let k = thousands(n as i64);
        self.status = match (image.is_none(), n > 1) {
            (true, true) => tr!("Removing the cover of {n} songs…", n = k),
            (true, false) => tr!("Removing the cover…").to_string(),
            (false, true) => tr!("Changing the cover of {n} songs…", n = k),
            (false, false) => tr!("Changing the cover…").to_string(),
        };
        let (tx, ctx, cfg, folder, also) = (self.tx.clone(), self.ctx.clone(), self.cfg.clone(), self.folder(), self.also_folder);
        let _ = std::thread::Builder::new().name("cover-save".into()).spawn(move || {
            let progress = {
                let (tx, ctx) = (tx.clone(), ctx.clone());
                move |i: usize, n: usize, _: &str| {
                    let _ = tx.send(Msg::Progress(i, n));
                    ctx.request_repaint();
                }
            };
            let mut notes = vec![];
            let outcomes = match &image {
                None => {
                    let out = tagedit::remove_cover(&paths, Some(&cfg), progress);
                    if let Some(kept) = covers::folder_image(&folder) {
                        notes.push(tagedit::folder_kept_note(&kept));
                    }
                    out
                }
                Some(img) => {
                    let out = tagedit::set_cover(&paths, img, "", Some(&cfg), progress);
                    if also {
                        notes.push(match covers::save_folder_cover(&folder, img) {
                            Ok(p) => tagedit::folder_saved_note(&p),
                            Err(e) => tagedit::folder_failed_note(&covers::folder_cover_name(&folder, img), &e.to_string()),
                        });
                    }
                    out
                }
            };
            let _ = tx.send(Msg::Done { removing: image.is_none(), outcomes, notes });
            ctx.request_repaint();
        });
    }

    /// Make the picture in this file the cover of the chosen songs (Change cover…).
    pub fn change_to(&mut self, image_path: &std::path::Path) -> bool {
        if !self.idle() {
            return false;
        }
        let name = image_path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let data = match std::fs::read(image_path) {
            Ok(d) => d,
            Err(e) => {
                self.summary = tr!("{file} cannot be used: {error}", file = &name, error = tr_text(&e.to_string()));
                return false;
            }
        };
        if let Err(e) = tagedit::check_picture(&data, "") {
            self.summary = tr!("{file} cannot be used: {error}", file = &name, error = tr_text(&e.to_string()));
            return false;
        }
        self.start(Some(data));
        true
    }

    /// Remove the covers inside the chosen songs (after the confirmation).
    pub fn remove(&mut self) -> bool {
        if !self.idle() {
            return false;
        }
        self.confirm_remove = false;
        self.start(None);
        true
    }

    /// Search Cover Online (2.3): the browser looks for the cover; a picture saved
    /// from it can then be chosen with Change cover….
    pub fn search_online(&mut self) {
        self.status = match crate::web::open_url(&self.search_url) {
            Ok(()) => String::new(),
            Err(e) => tr!("The browser could not be opened: {error}", error = e),
        };
    }

    /// Save image as…: the picture as it is stored.
    pub fn save_image_to(&mut self, path: &std::path::Path) -> bool {
        let Some(src) = &self.source else { return false };
        match std::fs::write(path, &src.data) {
            Ok(()) => {
                let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                self.status = tr!("Saved the picture as {file}.", file = name);
                true
            }
            Err(e) => {
                self.status = tr!("Could not save the picture: {error}", error = e);
                false
            }
        }
    }

    /// Take what the threads posted; true once covers were changed or removed.
    fn poll(&mut self) -> bool {
        let mut changed = false;
        while let Ok(m) = self.rx.try_recv() {
            match m {
                Msg::Source(gen, src, image) => {
                    if gen != self.gen {
                        continue;
                    }
                    self.reading = false;
                    self.source = src;
                    self.tex = image.map(|ci| self.ctx.load_texture("player cover window", ci, egui::TextureOptions::LINEAR));
                    if !self.busy {
                        self.status.clear();
                    }
                }
                Msg::Progress(i, n) => self.progress = (i < n).then_some((i, n)),
                Msg::Done { removing, outcomes, notes } => {
                    self.busy = false;
                    self.progress = None;
                    self.status.clear();
                    // `tagedit::cover_summary()`'s English texts, in the language in use
                    let (head, lines) = tagedit::cover_summary(&outcomes, removing, &notes);
                    self.summary = std::iter::once(head).chain(lines).map(|l| tr_text(&l)).collect::<Vec<_>>().join("\n");
                    self.outcomes = outcomes;
                    changed = true;
                    self.read();
                }
            }
        }
        changed
    }

    /// Draw the window; `esc`: whether Esc belongs to it this frame.
    pub fn show(&mut self, ctx: &egui::Context, pal: &Palette, esc: bool) -> Outcome {
        let mut result = if self.poll() { Outcome::Changed } else { Outcome::Open };
        let mut close = false;
        let mut show_album = false;
        let mut pick = false;
        let mut save = false;
        let n = self.album_paths.len();
        let mut open = true;
        // never narrower than the row of buttons in the language in use (it is one row, as in Python)
        let row = button_row_width(ctx);
        egui::Window::new(tr!("Cover"))
            .id(egui::Id::new("player-cover"))
            .open(&mut open)
            .collapsible(false)
            .resizable(true)
            .default_size(vec2(row.max(620.0), 760.0))
            .min_size(vec2(row.max(560.0), 560.0))
            .pivot(Align2::CENTER_CENTER)
            .default_pos(ctx.content_rect().center())
            .show(ctx, |ui| {
                ui.label(RichText::new(&self.heading).font(crate::look::title(18.0)).color(pal.text));
                if !self.subheading.is_empty() {
                    ui.label(RichText::new(&self.subheading).color(pal.secondary));
                }
                ui.add_space(8.0);
                let side = (ui.available_height() - 230.0).min(ui.available_width()).max(140.0);
                let (area, _) = ui.allocate_exact_size(vec2(ui.available_width(), side), Sense::hover());
                let p = ui.painter_at(area);
                match (&self.tex, &self.source) {
                    (Some(tex), Some(_)) => {
                        let size = tex.size_vec2();
                        let scale = (area.width() / size.x).min(area.height() / size.y);
                        let rect = Rect::from_center_size(area.center(), size * scale);
                        let shape = egui::epaint::RectShape::filled(rect, CornerRadius::same(6), Color32::WHITE)
                            .with_texture(tex.id(), Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)));
                        p.add(shape);
                    }
                    (_, src) => {
                        let text = if self.reading {
                            tr!("Reading the cover…")
                        } else if src.is_some() {
                            tr!("The picture cannot be shown.")
                        } else {
                            tr!("No cover")
                        };
                        p.rect_filled(Rect::from_center_size(area.center(), vec2(side, side)), CornerRadius::same(6), pal.lcd);
                        p.text(area.center(), Align2::CENTER_CENTER, text, crate::look::title(16.0), pal.secondary);
                    }
                }
                ui.add_space(8.0);
                match &self.source {
                    Some(src) => {
                        // the core's texts, in the language in use
                        ui.label(RichText::new(tr_text(&src.details())).color(pal.text));
                        ui.label(RichText::new(tr_text(&src.where_text())).color(pal.secondary));
                    }
                    None if !self.reading => {
                        ui.label(RichText::new(tr!("No cover")).color(pal.text));
                        ui.label(RichText::new(tr!("Neither the songs nor the album's folder hold a picture.")).color(pal.secondary));
                    }
                    None => {}
                }
                ui.add_space(6.0);
                ui.add_enabled_ui(self.idle(), |ui| {
                    if n > 1 {
                        ui.radio_value(&mut self.all_songs, true, tr!("All {n} songs of this album", n = thousands(n as i64)));
                        ui.radio_value(&mut self.all_songs, false, tr!("This song only"));
                    }
                    ui.checkbox(&mut self.also_folder, tr!("Also save it as cover.jpg in the album's folder"));
                });
                let status = match self.progress {
                    Some((i, total)) if total > 1 => {
                        let (i, n) = (thousands(i as i64 + 1), thousands(total as i64));
                        if self.removing {
                            tr!("Removing the cover of {i} of {n}…", i = i, n = n)
                        } else {
                            tr!("Changing the cover of {i} of {n}…", i = i, n = n)
                        }
                    }
                    _ => self.status.clone(),
                };
                if !status.is_empty() {
                    ui.label(RichText::new(status).small().color(pal.secondary));
                }
                if !self.summary.is_empty() {
                    ui.label(RichText::new(&self.summary).color(pal.text));
                }
                ui.add_space(6.0);
                if self.confirm_remove {
                    let k = self.target_paths().len();
                    let question = if k > 1 {
                        tr!(
                            "Remove the cover inside all {n} songs of this album? A picture in the album's folder is not deleted.",
                            n = thousands(k as i64)
                        )
                    } else {
                        tr!("Remove the cover inside this song? A picture in the album's folder is not deleted.").to_string()
                    };
                    ui.label(question);
                    ui.horizontal(|ui| {
                        if ui.button(RichText::new(tr!("Remove")).strong().color(pal.accent)).clicked() {
                            self.remove();
                        }
                        if ui.button(tr!("Cancel")).clicked() || (esc && ui.input(|i| i.key_pressed(egui::Key::Escape))) {
                            self.confirm_remove = false;
                        }
                    });
                    return;
                }
                ui.horizontal(|ui| {
                    let idle = self.idle();
                    if ui.add_enabled(idle, egui::Button::new(RichText::new(tr!("Change cover…")).strong())).clicked() {
                        pick = true;
                    }
                    if ui.button(tr!("Search Cover Online")).clicked() {
                        self.search_online();
                    }
                    if ui.add_enabled(idle && self.source.is_some(), egui::Button::new(tr!("Save image as…"))).clicked() {
                        save = true;
                    }
                    if ui.add_enabled(idle, egui::Button::new(tr!("Remove cover"))).clicked() {
                        self.confirm_remove = true;
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.add_enabled(!self.busy, egui::Button::new(tr!("Close"))).clicked() {
                            close = true;
                        }
                        if ui.add_enabled(!self.busy && self.album_key.is_some(), egui::Button::new(tr!("Show album"))).clicked() {
                            show_album = true;
                        }
                    });
                });
                if esc && !self.busy && ui.input(|i| i.key_pressed(egui::Key::Escape)) {
                    close = true;
                }
            });
        if pick {
            let file = rfd::FileDialog::new()
                .set_title(tr!("Choose the new cover"))
                .add_filter(tr!("JPEG or PNG images"), &["jpg", "jpeg", "png", "JPG", "JPEG", "PNG"])
                .pick_file();
            if let Some(f) = file {
                self.change_to(&f);
            }
        }
        if save {
            if let Some(src) = &self.source {
                let ext = src.ext();
                let file = rfd::FileDialog::new()
                    .set_title(tr!("Save the cover as"))
                    .set_file_name(format!("cover{ext}"))
                    .add_filter(
                        if src.format.is_empty() { tr!("Image").to_string() } else { tr!("{format} image", format = src.format) },
                        &[&ext[1..]],
                    )
                    .save_file();
                if let Some(f) = file {
                    self.save_image_to(&f);
                }
            }
        }
        if (close || !open) && !self.busy {
            return Outcome::Closed;
        }
        if show_album {
            if let Some(key) = self.album_key.clone() {
                result = Outcome::ShowAlbum(key);
            }
        }
        result
    }
}

/// How wide the window's row of buttons is (Change cover…, Search Cover Online,
/// Save image as…, Remove cover, then Show album and Close at the right), with
/// the window's margins and a gap between the two groups.
fn button_row_width(ctx: &egui::Context) -> f32 {
    let style = ctx.global_style();
    let font = egui::TextStyle::Button.resolve(&style);
    let pad = style.spacing.button_padding.x * 2.0 + style.spacing.item_spacing.x;
    let labels = [
        tr!("Change cover…"),
        tr!("Search Cover Online"),
        tr!("Save image as…"),
        tr!("Remove cover"),
        tr!("Show album"),
        tr!("Close"),
    ];
    let text: f32 = labels.iter().map(|l| ctx.fonts_mut(|f| f.layout_no_wrap(l.to_string(), font.clone(), Color32::WHITE).size().x)).sum();
    // the first is drawn bold (a little wider), 24 px between the groups, the window's margins
    text * 1.04 + pad * labels.len() as f32 + 24.0 + 2.0 * style.spacing.window_margin.leftf()
}

/// Draw the player's Cover window, if one is open, and do what it asks; true
/// when it was open this frame (Esc then belonged to it, or to a window above it).
pub fn cover_window(p: &mut crate::player::Player, ctx: &egui::Context, pal: &Palette) -> bool {
    let Some(mut w) = p.cover_win.take() else { return false };
    let esc = p.edit_info.is_none() && p.name_dialog.is_none() && p.confirm.is_none();
    match w.show(ctx, pal, esc) {
        Outcome::Open => p.cover_win = Some(w),
        Outcome::Closed => {}
        Outcome::ShowAlbum(_) => {
            p.cover_win = Some(w);
            show_album(p);
        }
        Outcome::Changed => {
            // the songs' covers changed: the library reads the songs again, and the cover cache
            // makes the covers of the albums whose songs changed again (library_changed())
            p.update_library();
            p.cover_win = Some(w);
        }
    }
    true
}

/// Show album: close the Cover window and open its album.
pub fn show_album(p: &mut crate::player::Player) {
    let Some(w) = p.cover_win.take() else { return };
    if w.busy {
        p.cover_win = Some(w);
        return;
    }
    if let Some(key) = w.album_key {
        p.go(crate::player::Page::Album(key));
    }
}

#[cfg(test)]
mod tests {
    //! The Cover window inside the whole player, headless: Cover… opens it for a
    //! song, the folder image is shown, Save image as… writes it, Change cover…
    //! embeds a JPEG in the album's songs and saves cover.jpg, Remove cover for
    //! one song, the library reads the changed songs, Show album and Esc.
    use super::*;
    use crate::player::{Page, Player};
    use std::process::Command;
    use std::time::{Duration, Instant};

    fn ffmpeg(args: &[&str]) {
        let st = Command::new(realhires::probe::tool("ffmpeg")).args(["-v", "error", "-y"]).args(args).status().unwrap();
        assert!(st.success(), "{args:?}");
    }

    fn frame(ctx: &egui::Context, p: &mut Player, keys: Vec<egui::Event>) {
        let input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1280.0, 840.0))),
            events: keys,
            ..Default::default()
        };
        ctx.run_ui(input, |ui| p.ui(ui)).drop_without_applying_deltas();
    }

    fn run_until(ctx: &egui::Context, p: &mut Player, what: &str, done: impl Fn(&Player) -> bool) {
        let t0 = Instant::now();
        while !done(p) {
            assert!(t0.elapsed() < Duration::from_secs(90), "timed out waiting for {what}");
            frame(ctx, p, vec![]);
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn win(p: &Player) -> &CoverWin {
        p.cover_win.as_ref().expect("the Cover window")
    }

    #[test]
    fn cover_window_shows_changes_and_removes_covers() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-cover-win-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let album = tmp.join("Music").join("Paper Lanterns");
        std::fs::create_dir_all(&album).unwrap();
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(realhires::musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(realhires::playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        let mut songs = vec![];
        for (n, title) in [(1, "Lanterns Over Water"), (2, "Paper in the Rain")] {
            let f = realhires::paths::abspath(&album.join(format!("0{n} {title}.flac")).to_string_lossy());
            let meta = [format!("title={title}"), "artist=Mira Calloway".into(), "album=Paper Lanterns".into(), format!("track={n}")];
            let mut args = vec!["-f", "lavfi", "-i", "sine=frequency=440:duration=1"];
            for m in &meta {
                args.extend(["-metadata", m.as_str()]);
            }
            args.push(&f);
            ffmpeg(&args);
            songs.push(f);
        }
        let folder_png = album.join("cover.png");
        ffmpeg(&["-f", "lavfi", "-i", "color=c=blue:s=600x600:d=1", "-frames:v", "1", &folder_png.to_string_lossy()]);
        let jpg = tmp.join("new.jpg");
        ffmpeg(&["-f", "lavfi", "-i", "testsrc=s=320x240:d=1", "-frames:v", "1", &jpg.to_string_lossy()]);
        let jpeg = std::fs::read(&jpg).unwrap();
        let mut cfg = Settings::default();
        cfg.library_folders = vec![tmp.join("Music").to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 2);

        p.open_cover_window(&songs[1]);
        run_until(&ctx, &mut p, "the cover read", |p| win(p).idle());
        let w = win(&p);
        assert_eq!(w.heading, "Paper Lanterns");
        assert_eq!(w.subheading, "Mira Calloway · 2 songs");
        assert!(w.all_songs && w.target_paths() == songs);
        assert!(w.tex.is_some(), "the picture is shown");
        let src = w.source.as_ref().unwrap();
        assert_eq!((src.kind, src.where_text()), ("folder", "cover.png in the album's folder".to_string()));
        assert!(src.details().starts_with("600 × 600 pixels · PNG · "), "{}", src.details());
        let saved = tmp.join("saved.png");
        assert!(p.cover_win.as_mut().unwrap().save_image_to(&saved));
        assert_eq!(std::fs::read(&saved).unwrap(), std::fs::read(&folder_png).unwrap());

        // Change cover… for the whole album, with cover.jpg in the folder
        let mtimes: Vec<i64> = songs.iter().map(|s| p.lib.track(s).unwrap().mtime_ns).collect();
        {
            let w = p.cover_win.as_mut().unwrap();
            w.also_folder = true;
            assert!(w.change_to(&jpg) && w.busy);
        }
        frame(&ctx, &mut p, vec![egui::Event::Key {
            key: egui::Key::Escape,
            physical_key: None,
            pressed: true,
            repeat: false,
            modifiers: Default::default(),
        }]);
        assert!(p.cover_win.is_some(), "Esc does not close the window while it works");
        run_until(&ctx, &mut p, "the album's cover changed", |p| win(p).idle());
        assert_eq!(
            win(&p).summary,
            "Changed the cover of 2 songs.\nThe picture was also saved as cover.jpg in the album's folder."
        );
        for s in &songs {
            let src = covers::cover_source(std::slice::from_ref(s)).unwrap();
            assert!(src.kind == "embedded" && src.data == jpeg, "{s}");
        }
        assert_eq!(std::fs::read(album.join("cover.jpg")).unwrap(), jpeg);
        assert_eq!(win(&p).source.as_ref().unwrap().where_text(), "Inside 02 Paper in the Rain.flac");
        run_until(&ctx, &mut p, "the library reading the changed songs", |p| {
            songs.iter().zip(&mtimes).all(|(s, m)| p.lib.track(s).is_some_and(|t| t.mtime_ns != *m))
        });

        // Remove cover for this song only, after the confirmation
        {
            let w = p.cover_win.as_mut().unwrap();
            w.all_songs = false;
            w.confirm_remove = true;
        }
        frame(&ctx, &mut p, vec![]);
        assert!(win(&p).confirm_remove && win(&p).idle());
        assert!(p.cover_win.as_mut().unwrap().remove());
        run_until(&ctx, &mut p, "the song's cover removed", |p| win(p).idle());
        let summary = win(&p).summary.clone();
        assert!(summary.starts_with("Removed the cover from 1 song.\n"), "{summary}");
        assert!(summary.contains("in the album's folder was not deleted"), "{summary}");
        assert_eq!(covers::cover_source(&songs[1..]).unwrap().kind, "folder");
        assert_eq!(win(&p).source.as_ref().unwrap().where_text(), "Inside 01 Lanterns Over Water.flac");

        // Show album closes the window and opens the album; Esc closes it
        let key = win(&p).album_key.clone().unwrap();
        p.open_cover_window(&songs[0]);
        assert_eq!(win(&p).path, songs[0], "another song's Cover window replaces an idle one");
        show_album(&mut p);
        assert!(p.cover_win.is_none() && p.page == Page::Album(key));
        p.open_cover_window(&songs[0]);
        run_until(&ctx, &mut p, "the cover read again", |p| win(p).idle());
        frame(&ctx, &mut p, vec![egui::Event::Key {
            key: egui::Key::Escape,
            physical_key: None,
            pressed: true,
            repeat: false,
            modifiers: Default::default(),
        }]);
        assert!(p.cover_win.is_none(), "Esc closes the Cover window");
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
