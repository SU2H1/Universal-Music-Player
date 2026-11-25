//! 2.4 (`docs/player-spec.md`, "2.4" item 2) in the Rust player: the names on Your
//! Stats as links, and the leaderboard: a section of Your Stats and the "Most Played"
//! page (`Page::MostPlayed`), as the Python app's `realhires/player_stats.py`.
//!
//! Every song, artist, album and genre named on Your Stats (the cards, the top lists of
//! Details, the day beside the calendar, the leaderboard) is a link (`Link`): a song
//! plays it with the songs of that list as the queue, an artist opens the artist's page,
//! an album its page, a genre its page.  The pointer is a hand over a link, and a right
//! click on a song opens the song menu.
//!
//! The leaderboard ranks the most played songs, artists or albums of the period chosen
//! on Your Stats (`stats::leaderboard()`): rank (the top three on a gold, silver and
//! bronze badge), cover, title, artist, plays and minutes listened, up to 100 rows, ties
//! sharing a rank.  Its switch is `Player::leaderboard_kind`, kept for the session and
//! shared by the section and the page; Play / Shuffle play the ranked songs in rank
//! order (`stats::ranked_songs()`).

use std::sync::Arc;

use egui::{pos2, vec2, Align, Align2, Color32, CornerRadius, Layout, Rect, Sense, Ui};
use realhires::i18n::{self, tr_name};
use realhires::pyunicode::casefold;
use realhires::stats::{self, LeaderRow};
use realhires::{musiclib, n_, tr};

use crate::look::{self, Palette};
use crate::pages::{elided, pill_button};
use crate::player::{count, cover, Page, Player};
use crate::icons::Icon;

/// The rank badges of the top three: gold, silver, bronze.
pub const MEDALS: [Color32; 3] = [Color32::from_rgb(0xd4, 0xa0, 0x17), Color32::from_rgb(0x9a, 0xa0, 0xa6), Color32::from_rgb(0xc2, 0x7a, 0x3a)];
/// The rows of the leaderboard section of Your Stats (the Most Played page shows up to `stats::TOP`).
pub const SECTION_ROWS: usize = 10;
/// A leaderboard row and its cover.
pub const ROW_H: f32 = 52.0;
const COVER: f32 = 40.0;
const KIND_LABELS: [(&str, &str); 3] = [("songs", n_!("Songs")), ("artists", n_!("Artists")), ("albums", n_!("Albums"))];

/// The period's three leaderboards, by kind.
pub type Boards = Arc<Vec<(&'static str, Vec<LeaderRow>)>>;

/// What a click on a name of Your Stats does.
#[derive(Clone, Debug, PartialEq)]
pub enum Link {
    /// play `path`, with `list` as the queue named `name`
    Play { list: Vec<String>, path: String, name: &'static str },
    /// open a page (an artist, album or genre)
    Open(Page),
}

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

impl Player {
    // ------------------------------------------------------------- data ---

    /// The leaderboards of the period chosen on Your Stats, made again when the period
    /// or the plays change.
    pub fn boards(&mut self) -> Boards {
        if let Some((p, g, b)) = &self.boards_cache {
            if *p == self.stats_period && *g == self.history_gen {
                return b.clone();
            }
        }
        let (since, until) = stats::period_range(&self.stats_period, now());
        let plays = self.history.as_ref().map(|h| h.plays(since, until)).unwrap_or_default();
        let b: Boards = Arc::new(stats::LEADERBOARD_KINDS.iter().map(|k| (*k, stats::leaderboard(&plays, k, stats::TOP))).collect());
        self.boards_cache = Some((self.stats_period.clone(), self.history_gen, b.clone()));
        b
    }

    /// The period's leaderboard of `kind`.
    pub fn board(&mut self, kind: &str) -> Vec<LeaderRow> {
        self.boards().iter().find(|(k, _)| *k == kind).map(|(_, rows)| rows.clone()).unwrap_or_default()
    }

    // ------------------------------------------------------------ links ---

    /// The page an artist's name opens (Artists, else Album Artists), as `open_artist()`
    /// finds it; None when the library has no such artist.
    pub fn artist_link(&self, name: &str) -> Option<Link> {
        let mut names = vec![name.to_string()];
        names.extend(musiclib::split_artists(name, &self.lib.known).into_iter().take(1));
        for n in &names {
            for kind in ["artists", "album_artists"] {
                if let Some(i) = self.lib.find_group(kind, n) {
                    return Some(Link::Open(Page::Group(kind, self.lib.groups(kind)[i].name.clone())));
                }
            }
        }
        None
    }

    pub fn genre_link(&self, name: &str) -> Option<Link> {
        self.lib.find_group("genres", name).map(|i| Link::Open(Page::Group("genres", self.lib.genres[i].name.clone())))
    }

    /// The album page of a row of Your Stats: the album of one of its songs, else the
    /// album of that name (and artist).
    pub fn album_link(&self, name: &str, artist: &str, paths: &[String]) -> Option<Link> {
        if let Some(a) = paths.iter().find_map(|p| self.lib.album_of(p)) {
            return Some(Link::Open(Page::Album(a.key.clone())));
        }
        let (n, a) = (casefold(name.trim()), casefold(artist.trim()));
        let named: Vec<&musiclib::Album> = self.lib.albums.iter().filter(|x| casefold(x.title.trim()) == n).collect();
        named.iter().find(|x| casefold(x.artist.trim()) == a).or(named.first()).map(|x| Link::Open(Page::Album(x.key.clone())))
    }

    /// A song as a link: it plays with `list` as the queue (None when its file is gone).
    pub fn song_link(&self, list: &[String], path: &str, name: &'static str) -> Option<Link> {
        self.lib.by_path.contains_key(path).then(|| Link::Play { list: list.to_vec(), path: path.to_string(), name })
    }

    /// Do what a link says.
    pub fn follow(&mut self, link: Link) {
        match link {
            Link::Play { mut list, path, name } => {
                if !list.contains(&path) {
                    list.push(path.clone());
                }
                let mut seen = std::collections::HashSet::new();
                list.retain(|p| seen.insert(p.clone()));
                self.play_list(list, Some(&path), name);
            }
            Link::Open(page) => self.go(page),
        }
    }

    /// `rect` (a name drawn there) as a link: the hand pointer, a click returns the link,
    /// a right click on a song (`song`) opens the song menu.
    pub fn link_rect(&mut self, ui: &mut Ui, rect: Rect, id: impl std::hash::Hash + std::fmt::Debug, link: Option<Link>, song: Option<&str>) -> Option<Link> {
        if link.is_none() && song.is_none() {
            return None;
        }
        let resp = ui.interact(rect, ui.scope_id().with(("stats-link", id)), Sense::click());
        let resp = if link.is_some() { resp.on_hover_cursor(egui::CursorIcon::PointingHand) } else { resp };
        if let Some(path) = song.filter(|p| self.lib.by_path.contains_key(*p)) {
            let path = path.to_string();
            resp.context_menu(|ui| self.song_menu(ui, &path));
        }
        if resp.clicked() {
            link
        } else {
            None
        }
    }

    /// A name drawn as a link where the layout puts it (elided to `width`).
    pub fn link_label(
        &mut self,
        ui: &mut Ui,
        text: &str,
        font: egui::FontId,
        color: Color32,
        width: f32,
        link: Option<Link>,
        song: Option<&str>,
    ) -> Option<Link> {
        let g = elided(ui, text, font, color, width);
        let (rect, _) = ui.allocate_exact_size(g.size(), Sense::hover());
        ui.painter().galley(rect.min, g, color);
        self.link_rect(ui, rect, (text, rect.min.x as i32, rect.min.y as i32), link, song)
    }

    // ------------------------------------------------------- leaderboard ---

    /// The switch: Songs, Artists, Albums.
    fn board_switch(&mut self, ui: &mut Ui, pal: &Palette) {
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 6.0;
            for (kind, label) in KIND_LABELS {
                let on = self.leaderboard_kind == kind;
                if pill_button(ui, pal, None, i18n::tr(label), on, true).clicked() {
                    self.leaderboard_kind = stats::LEADERBOARD_KINDS.iter().copied().find(|k| *k == kind).unwrap_or("songs");
                }
            }
        });
    }

    /// The cover of a leaderboard row: the album of its (most played) song.
    fn row_cover(&mut self, kind: &str, r: &LeaderRow) -> Option<egui::TextureHandle> {
        let album = if kind == "albums" {
            match self.album_link(&r.name, &r.artist, &r.paths) {
                Some(Link::Open(Page::Album(key))) => self.lib.album_index.get(&key).copied(),
                _ => None,
            }
        } else {
            None
        }
        .or_else(|| r.paths.iter().find_map(|p| self.lib.album_of(p).and_then(|a| self.lib.album_index.get(&a.key).copied())));
        let i = album?;
        let key = self.lib.albums[i].key.clone();
        let paths = self.lib.album_paths[i].clone();
        self.covers.get(&key, &paths)
    }

    /// One leaderboard row, `w` wide: rank (the top three on a medal), cover, name and
    /// artist (links), plays and minutes.
    fn board_row(&mut self, ui: &mut Ui, pal: &Palette, kind: &str, r: &LeaderRow, i: usize, queue: &[String], name: &'static str) -> Option<Link> {
        let w = ui.available_width();
        let (rect, _) = ui.allocate_exact_size(vec2(w, ROW_H), Sense::hover());
        // the whole row of a song opens its menu on a right click (the links come after it, on top)
        let row = (kind == "songs").then(|| ui.interact(rect, ui.scope_id().with(("board-row", kind, i)), Sense::click()));
        let p = ui.painter_at(rect);
        if i % 2 == 1 {
            p.rect_filled(rect.shrink2(vec2(0.0, 1.0)), CornerRadius::same(8), pal.zebra);
        }
        let mid = rect.center().y;
        let badge = pos2(rect.left() + 19.0, mid);
        if r.rank <= 3 {
            p.circle_filled(badge, 13.0, MEDALS[r.rank - 1]);
            p.text(badge, Align2::CENTER_CENTER, r.rank.to_string(), look::title(13.0), Color32::WHITE);
        } else {
            p.text(badge, Align2::CENTER_CENTER, r.rank.to_string(), look::title(13.0), pal.secondary);
        }
        let tex = self.row_cover(kind, r);
        cover(&p, Rect::from_min_size(pos2(rect.left() + 42.0, mid - COVER / 2.0), vec2(COVER, COVER)), tex.as_ref(), &r.name, 5);
        let stat = tr!(
            "{plays} · {minutes} min",
            plays = count(r.plays.max(0) as usize, n_!("1 play"), n_!("{n} plays")),
            minutes = stats::thousands(r.minutes)
        );
        let srect = p.text(pos2(rect.right() - 12.0, mid), Align2::RIGHT_CENTER, stat, look::text(12.0), pal.secondary);
        let room = (srect.left() - (rect.left() + 94.0) - 16.0).max(20.0);
        let playing = kind == "songs" && self.now.as_ref().is_some_and(|n| n.path == r.key);
        let color = if playing { pal.accent } else { pal.text };
        let (shown, link, song) = match kind {
            "songs" => (r.name.clone(), self.song_link(queue, &r.key, name), Some(r.key.as_str())),
            "artists" => (tr_name(&r.name).to_string(), self.artist_link(&r.name), None),
            _ => (tr_name(&r.name).to_string(), self.album_link(&r.name, &r.artist, &r.paths), None),
        };
        let mut out = None;
        let g = elided(ui, &shown, look::title(13.0), color, room);
        let y = if kind == "artists" { mid - g.size().y / 2.0 } else { mid - g.size().y + 1.0 };
        let nrect = Rect::from_min_size(pos2(rect.left() + 94.0, y), g.size());
        ui.painter().galley(nrect.min, g, color);
        if let Some(l) = self.link_rect(ui, nrect, ("board-name", kind, i), link, song) {
            out = Some(l);
        }
        if kind != "artists" {
            let g = elided(ui, tr_name(&r.artist), look::text(12.0), pal.secondary, room);
            let arect = Rect::from_min_size(pos2(rect.left() + 94.0, mid + 2.0), g.size());
            ui.painter().galley(arect.min, g, pal.secondary);
            let link = self.artist_link(&r.artist);
            if let Some(l) = self.link_rect(ui, arect, ("board-artist", kind, i), link, None) {
                out = Some(l);
            }
        }
        if let (Some(resp), Some(path)) = (row, song.filter(|p| self.lib.by_path.contains_key(*p))) {
            let path = path.to_string();
            resp.context_menu(|ui| self.song_menu(ui, &path));
        }
        out
    }

    /// Leaderboard rows (up to `limit`), or "No plays yet in this period.".
    fn board_rows(&mut self, ui: &mut Ui, pal: &Palette, rows: &[LeaderRow], limit: usize, name: &'static str) -> Option<Link> {
        if rows.is_empty() {
            ui.label(egui::RichText::new(tr!("No plays yet in this period.")).color(pal.secondary));
            return None;
        }
        let kind = self.leaderboard_kind;
        let queue = stats::ranked_songs(rows);
        let mut out = None;
        for (i, r) in rows.iter().take(limit).enumerate() {
            if let Some(l) = self.board_row(ui, pal, kind, r, i, &queue, name) {
                out = Some(l);
            }
        }
        out
    }

    /// The leaderboard of Your Stats: "Leaderboard", See All (the Most Played page), the
    /// switch and the top rows of the period.
    pub fn leaderboard_section(&mut self, ui: &mut Ui, pal: &Palette) {
        ui.add_space(10.0);
        ui.horizontal(|ui| {
            ui.label(egui::RichText::new(tr!("Leaderboard")).font(look::title(17.0)).color(pal.text));
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                let see = ui.add(egui::Label::new(egui::RichText::new(tr!("See All")).font(look::title(13.0)).color(pal.accent)).sense(Sense::click()));
                if see.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                    self.go(Page::MostPlayed);
                }
            });
        });
        ui.add_space(6.0);
        self.board_switch(ui, pal);
        ui.add_space(8.0);
        let rows = self.board(self.leaderboard_kind);
        if let Some(l) = self.board_rows(ui, pal, &rows, SECTION_ROWS, "Your Stats") {
            self.follow(l);
        }
        ui.add_space(12.0);
    }

    /// The leaderboard as a page of its own ("Most Played"): Back, the period's menu,
    /// Play and Shuffle (the ranked songs in rank order), the switch and up to 100 rows.
    pub fn most_played_page(&mut self, ui: &mut Ui, pal: &Palette) {
        if crate::library_pages::back_link(ui, pal, tr!("Your Stats")).clicked() {
            self.go(Page::Stats);
            return;
        }
        let rows = self.board(self.leaderboard_kind);
        let ranked = stats::ranked_songs(&rows);
        ui.horizontal_wrapped(|ui| {
            ui.label(egui::RichText::new(tr!("Most Played")).font(look::title(28.0)).color(pal.text));
            ui.add_space(16.0);
            self.period_menu(ui);
            ui.add_space(8.0);
            if !ranked.is_empty() {
                if pill_button(ui, pal, Some(Icon::Play), tr!("Play"), true, true).clicked() {
                    self.play_list(ranked.clone(), Some(&ranked[0]), "Most Played");
                }
                if pill_button(ui, pal, Some(Icon::Shuffle), tr!("Shuffle"), true, true).clicked() {
                    self.shuffle_list(ranked.clone(), "Most Played");
                }
            }
        });
        ui.add_space(10.0);
        self.board_switch(ui, pal);
        ui.add_space(8.0);
        // the switch may have changed the kind
        let rows = self.board(self.leaderboard_kind);
        if rows.is_empty() {
            self.board_rows(ui, pal, &rows, stats::TOP, "Most Played");
            return;
        }
        let kind = self.leaderboard_kind;
        let queue = stats::ranked_songs(&rows);
        let mut out = None;
        egui::ScrollArea::vertical().id_salt("most-played").auto_shrink([false, false]).show_rows(ui, ROW_H, rows.len().min(stats::TOP), |ui, range| {
            for i in range {
                if let Some(l) = self.board_row(ui, pal, kind, &rows[i], i, &queue, "Most Played") {
                    out = Some(l);
                }
            }
        });
        if let Some(l) = out {
            self.follow(l);
        }
    }
}

#[cfg(test)]
mod tests {
    //! 2.4 items 1 and 2 in the Rust player, headlessly in a 1024 x 768 window (the CI
    //! runners' screens): the Folders page scrolls as one list with the mouse wheel, the
    //! trackpad and the scroll bar; the names on Your Stats are links (cards, Details,
    //! the day beside the calendar) with the hand pointer and the song menu; the
    //! leaderboard section and the Most Played page (switch, ranks, Play, the page's name
    //! on Now Playing's back link), also in Japanese.
    use super::*;
    use egui::{Event, MouseWheelSource, MouseWheelUnit, PointerButton, Pos2, TouchPhase};
    use realhires::settings::Settings;
    use realhires::{musiclib, playerstate};
    use std::process::Command;
    use std::time::{Duration, Instant};

    const W: f32 = 1024.0;
    const H: f32 = 768.0;

    fn song(dir: &std::path::Path, album: &str, title: &str, artist: &str, genre: &str, n: u32) -> String {
        let d = dir.join(album);
        std::fs::create_dir_all(&d).unwrap();
        let out = d.join(format!("{n:02} {title}.flac")).to_string_lossy().into_owned();
        let src = format!("sine=frequency={}:sample_rate=44100:duration=2", 300 + 40 * n);
        let meta = [format!("title={title}"), format!("artist={artist}"), format!("album={album}"), format!("track={n}"), format!("genre={genre}")];
        let mut args: Vec<&str> = vec!["-v", "error", "-y", "-f", "lavfi", "-i", &src];
        for m in &meta {
            args.extend(["-metadata", m.as_str()]);
        }
        args.push(&out);
        assert!(Command::new(realhires::probe::tool("ffmpeg")).args(&args).status().unwrap().success());
        realhires::paths::abspath(&out)
    }

    /// One frame of the 1024 x 768 window with these events: every text drawn (and where),
    /// the narrow filled rectangles (a scroll bar's handle) and the pointer's shape.
    struct Drawn {
        texts: Vec<(String, Rect)>,
        bars: Vec<Rect>,
        cursor: egui::CursorIcon,
    }

    fn run(ctx: &egui::Context, p: &mut Player, events: Vec<Event>) -> Drawn {
        fn walk(shape: &egui::Shape, d: &mut Drawn) {
            match shape {
                egui::Shape::Text(t) => d.texts.push((t.galley.text().to_string(), t.visual_bounding_rect())),
                egui::Shape::Rect(r) if r.rect.width() <= 12.0 && r.rect.height() >= 16.0 && r.rect.left() > 600.0 => {
                    d.bars.push(r.rect)
                }
                egui::Shape::Vec(v) => v.iter().for_each(|s| walk(s, d)),
                _ => {}
            }
        }
        let raw = egui::RawInput { screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(W, H))), events, ..Default::default() };
        let out = ctx.run_ui(raw, |ui| p.ui(ui));
        let mut d = Drawn { texts: vec![], bars: vec![], cursor: out.platform_output.cursor_icon };
        out.shapes.iter().for_each(|s| walk(&s.shape, &mut d));
        out.drop_without_applying_deltas();
        d
    }

    fn frames(ctx: &egui::Context, p: &mut Player) -> Drawn {
        run(ctx, p, vec![]);
        run(ctx, p, vec![]);
        run(ctx, p, vec![])
    }

    fn shown(d: &Drawn, text: &str) -> bool {
        d.texts.iter().any(|(t, r)| t == text && r.bottom() > 0.0 && r.top() < H)
    }

    /// Where the text `text` was drawn on the visible page (the first such text when `first`).
    fn at(d: &Drawn, text: &str, pick: impl Fn(&Rect) -> bool) -> Pos2 {
        let names: Vec<&str> = d.texts.iter().map(|(t, _)| t.as_str()).collect();
        d.texts
            .iter()
            .find(|(t, r)| t == text && r.bottom() > 70.0 && r.top() < H && pick(r))
            .unwrap_or_else(|| panic!("no {text:?} in {names:?}"))
            .1
            .center()
    }

    /// Scroll the page (the wheel over its middle) until `text` (one that `pick` accepts) is
    /// well inside the window: down first, then up.
    fn reveal(ctx: &egui::Context, p: &mut Player, text: &str, pick: impl Fn(&Rect) -> bool) -> Drawn {
        let inside = |d: &Drawn| d.texts.iter().any(|(t, r)| t == text && r.top() > 200.0 && r.bottom() < H - 60.0 && pick(r));
        let mid = pos2(620.0, 420.0);
        // the page scrolls on for a few frames after a wheel step: wait until it stands still
        let settled = |ctx: &egui::Context, p: &mut Player| {
            let mut last = frames(ctx, p);
            for _ in 0..120 {
                let d = run(ctx, p, vec![]);
                let same = d.texts.len() == last.texts.len() && d.texts.iter().zip(&last.texts).all(|(a, b)| a.1 == b.1);
                last = d;
                if same {
                    break;
                }
            }
            last
        };
        for (dy, steps) in [(-150.0, 14), (150.0, 30)] {
            let d = settled(ctx, p);
            if inside(&d) {
                return d;
            }
            for _ in 0..steps {
                run(ctx, p, vec![Event::PointerMoved(mid), wheel(mid, dy, MouseWheelSource::Wheel, TouchPhase::Move)]);
                let d = settled(ctx, p);
                if inside(&d) {
                    return d;
                }
            }
        }
        panic!("{text:?} never came into view");
    }

    fn press(pos: Pos2, button: PointerButton, pressed: bool) -> Event {
        Event::PointerButton { pos, button, pressed, modifiers: Default::default() }
    }

    fn click(ctx: &egui::Context, p: &mut Player, pos: Pos2, button: PointerButton) {
        run(ctx, p, vec![Event::PointerMoved(pos)]);
        run(ctx, p, vec![press(pos, button, true)]);
        run(ctx, p, vec![press(pos, button, false)]);
    }

    fn wheel(_pos: Pos2, dy: f32, source: MouseWheelSource, phase: TouchPhase) -> Event {
        Event::MouseWheel { unit: MouseWheelUnit::Point, delta: vec2(0.0, dy), phase, source, modifiers: egui::Modifiers::NONE }
    }

    #[test]
    fn folders_scroll_stats_links_and_most_played() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        realhires::i18n::set_thread_language("English");
        let tmp = std::env::temp_dir().join(format!("rhr-stats-24-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        std::env::set_var(realhires::covers::COVERS_ENV, tmp.join("covers"));
        let a1 = song(&music, "Paper Lanterns", "Lanterns Over Water", "Mira Calloway", "Pop", 1);
        let a2 = song(&music, "Paper Lanterns", "Paper in the Rain", "Mira Calloway", "Pop", 2);
        let b1 = song(&music, "Harbour Lights", "Tidewater", "The Northern Quay", "Jazz", 1);
        // a folder of 25 subfolders (a song each) and 30 songs of its own
        let many = music.join("Many");
        for i in 0..25 {
            let d = many.join(format!("Sub {i:02}"));
            std::fs::create_dir_all(&d).unwrap();
            std::fs::copy(&a2, d.join("song.flac")).unwrap();
        }
        for i in 0..30 {
            std::fs::copy(&b1, many.join(format!("{i:02} direct.flac"))).unwrap();
        }
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        cfg.theme = "Light".into();
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        let t0 = Instant::now();
        while !(p.lib.tracks.len() == 58 && p.library_status().is_none()) {
            assert!(t0.elapsed() < Duration::from_secs(120), "the library index");
            run(&ctx, &mut p, vec![]);
            std::thread::sleep(Duration::from_millis(20));
        }

        // ---- 1. Folders: the subfolders and the songs are one list that scrolls
        let many_dir = realhires::paths::abspath(&many.to_string_lossy());
        p.go(Page::Folders(Some(many_dir.clone())));
        let d = frames(&ctx, &mut p);
        assert!(shown(&d, "Sub 00") && !shown(&d, "Sub 24"), "the first subfolders, not the last");
        assert!(shown(&d, "Title"), "the songs' headings above the list");
        let list = at(&d, "Sub 03", |_| true);
        // the mouse wheel: the subfolders scroll away and the songs come up in the same list
        run(&ctx, &mut p, vec![Event::PointerMoved(list), wheel(list, -500.0, MouseWheelSource::Wheel, TouchPhase::Move)]);
        let d = frames(&ctx, &mut p);
        assert!(!shown(&d, "Sub 00") && shown(&d, "Sub 24") && shown(&d, "Tidewater"), "scrolled by the wheel");
        let tide = at(&d, "Tidewater", |_| true).y;
        let sub24 = at(&d, "Sub 24", |_| true).y;
        assert!(tide > sub24, "the songs below the subfolders");
        // the trackpad (smooth scrolling) moves the same list on
        run(&ctx, &mut p, vec![wheel(list, 0.0, MouseWheelSource::Trackpad, TouchPhase::Start)]);
        for _ in 0..5 {
            run(&ctx, &mut p, vec![wheel(list, -60.0, MouseWheelSource::Trackpad, TouchPhase::Move)]);
        }
        run(&ctx, &mut p, vec![wheel(list, 0.0, MouseWheelSource::Trackpad, TouchPhase::End)]);
        let d = frames(&ctx, &mut p);
        let moved = d.texts.iter().filter(|(t, _)| t == "Sub 24").map(|(_, r)| r.center().y).next();
        assert!(moved.is_none_or(|y| y < sub24 - 200.0), "scrolled by the trackpad: {sub24} -> {moved:?}");
        // the scroll bar: its handle dragged to the bottom shows the last songs
        let d = run(&ctx, &mut p, vec![Event::PointerMoved(pos2(W - 5.0, 400.0))]);
        let d2 = frames(&ctx, &mut p);
        let bar = d.bars.iter().chain(d2.bars.iter()).copied().max_by(|a, b| a.height().total_cmp(&b.height())).expect("a scroll bar");
        run(&ctx, &mut p, vec![Event::PointerMoved(bar.center())]);
        run(&ctx, &mut p, vec![press(bar.center(), PointerButton::Primary, true)]);
        for k in 1..=10 {
            run(&ctx, &mut p, vec![Event::PointerMoved(pos2(bar.center().x, bar.center().y + 60.0 * k as f32))]);
        }
        run(&ctx, &mut p, vec![press(pos2(bar.center().x, H - 2.0), PointerButton::Primary, false)]);
        let d = frames(&ctx, &mut p);
        let tides: Vec<f32> = d.texts.iter().filter(|(t, r)| t == "Tidewater" && r.top() < H).map(|(_, r)| r.bottom()).collect();
        assert!(d.texts.iter().all(|(t, _)| !t.starts_with("Sub ")) && tides.iter().any(|y| *y > H - 80.0), "dragged to the end: {tides:?}");
        // the root folder: only subfolders (no headings); a click opens one
        p.go(Page::Folders(Some(realhires::paths::abspath(&music.to_string_lossy()))));
        let d = frames(&ctx, &mut p);
        assert!(shown(&d, "Many") && !shown(&d, "Title"));
        click(&ctx, &mut p, at(&d, "Many", |r| r.top() > 150.0), PointerButton::Primary);
        assert_eq!(p.page, Page::Folders(Some(many_dir.clone())));

        // ---- 2. Your Stats: plays of three songs, all time
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64();
        for (i, (path, secs)) in [(&a1, 60.0), (&a1, 60.0), (&a1, 60.0), (&a1, 60.0), (&b1, 60.0), (&b1, 60.0), (&a2, 40.0)].iter().enumerate() {
            let track = p.lib.track(path).unwrap().clone();
            assert!(p.history.as_ref().unwrap().add_play(&track, now - 600.0 * (i as f64 + 1.0), *secs));
        }
        p.history_gen += 1;
        p.stats_period = "All time".into();
        p.go(Page::Stats);
        let d = reveal(&ctx, &mut p, "Leaderboard", |_| true);
        assert!(shown(&d, "See All") && shown(&d, "Songs") && shown(&d, "Artists"));
        let ranks = p.board("songs");
        assert_eq!(ranks.iter().map(|r| (r.rank, r.key.clone())).collect::<Vec<_>>(), vec![(1, a1.clone()), (2, b1.clone()), (3, a2.clone())]);
        // the top song's card: the hand pointer, a click plays it with the period's ranking as the queue
        let d = reveal(&ctx, &mut p, "Lanterns Over Water", |r| r.height() > 16.0);
        let card_song = at(&d, "Lanterns Over Water", |r| r.height() > 16.0);
        let d = run(&ctx, &mut p, vec![Event::PointerMoved(card_song)]);
        let d = if d.cursor == egui::CursorIcon::PointingHand { d } else { run(&ctx, &mut p, vec![Event::PointerMoved(card_song)]) };
        assert_eq!(d.cursor, egui::CursorIcon::PointingHand, "the hand over a link");
        click(&ctx, &mut p, card_song, PointerButton::Primary);
        assert_eq!(p.now.as_ref().map(|n| n.path.clone()), Some(a1.clone()));
        assert_eq!(p.queue_name, "Your Stats");
        assert_eq!(p.queue.as_ref().unwrap().context, vec![a1.clone(), b1.clone(), a2.clone()], "the ranked songs as the queue");
        p.engine.pause();
        // the top artist's card opens the artist
        let d = reveal(&ctx, &mut p, "Mira Calloway", |r| r.height() > 16.0);
        click(&ctx, &mut p, at(&d, "Mira Calloway", |r| r.height() > 16.0), PointerButton::Primary);
        assert_eq!(p.page, Page::Group("artists", "Mira Calloway".into()));
        p.go_back();
        // the leaderboard: a right click on a song opens its menu
        let d = reveal(&ctx, &mut p, "Tidewater", |r| r.height() < 16.0 && r.left() > 300.0);
        let row = at(&d, "Tidewater", |r| r.height() < 16.0 && r.left() > 300.0);
        click(&ctx, &mut p, row, PointerButton::Secondary);
        let d = frames(&ctx, &mut p);
        assert!(shown(&d, "Play Next") && shown(&d, "Add to Queue"), "the song menu");
        run(&ctx, &mut p, vec![Event::Key { key: egui::Key::Escape, physical_key: None, pressed: true, repeat: false, modifiers: Default::default() }]);
        let d = frames(&ctx, &mut p);
        assert!(!shown(&d, "Play Next"), "Esc closed the menu");
        // the switch: Albums, a click on an album opens it; an artist name under it opens the artist
        p.go(Page::Stats);
        let d = reveal(&ctx, &mut p, "Albums", |r| r.left() > 240.0);
        click(&ctx, &mut p, at(&d, "Albums", |r| r.left() > 240.0), PointerButton::Primary);
        assert_eq!(p.leaderboard_kind, "albums");
        let d = reveal(&ctx, &mut p, "Paper Lanterns", |r| r.height() < 16.0);
        let album_key = p.lib.album_of(&a1).unwrap().key.clone();
        click(&ctx, &mut p, at(&d, "Paper Lanterns", |r| r.height() < 16.0), PointerButton::Primary);
        assert_eq!(p.page, Page::Album(album_key.clone()));
        p.go(Page::Stats);
        let d = reveal(&ctx, &mut p, "The Northern Quay", |r| r.height() < 16.0);
        click(&ctx, &mut p, at(&d, "The Northern Quay", |r| r.height() < 16.0), PointerButton::Primary);
        assert_eq!(p.page, Page::Group("artists", "The Northern Quay".into()));
        // See All: the Most Played page, its name on Now Playing's back link
        p.go(Page::Stats);
        let d = reveal(&ctx, &mut p, "See All", |_| true);
        click(&ctx, &mut p, at(&d, "See All", |_| true), PointerButton::Primary);
        assert_eq!(p.page, Page::MostPlayed);
        assert_eq!(p.page_label(&Page::MostPlayed), "Most Played");
        let d = frames(&ctx, &mut p);
        assert!(shown(&d, "Most Played") && shown(&d, "Paper Lanterns") && shown(&d, "Harbour Lights"));
        click(&ctx, &mut p, at(&d, "Songs", |r| r.left() > 240.0), PointerButton::Primary);
        assert_eq!(p.leaderboard_kind, "songs");
        let d = frames(&ctx, &mut p);
        for (n, t) in [("1", "Lanterns Over Water"), ("2", "Tidewater"), ("3", "Paper in the Rain")] {
            let (rank, row) = (at(&d, n, |r| r.left() < 400.0 && r.top() > 150.0), at(&d, t, |_| true));
            assert!((rank.y - row.y).abs() < 14.0, "rank {n} beside {t}");
        }
        click(&ctx, &mut p, at(&d, "Play", |r| r.top() < 200.0 && r.left() > 240.0), PointerButton::Primary);
        assert_eq!(p.queue_name, "Most Played");
        assert_eq!(p.queue.as_ref().unwrap().context, vec![a1.clone(), b1.clone(), a2.clone()]);
        assert_eq!(p.now.as_ref().map(|n| n.path.clone()), Some(a1.clone()), "rank order");
        p.engine.pause();
        p.go(Page::NowPlaying);
        assert_eq!(p.back_label().as_deref(), Some("Most Played"));
        p.go_back();

        // ---- Details: the top lists' names are links
        p.go(Page::StatsDetails);
        let d = reveal(&ctx, &mut p, "Jazz", |_| true);
        click(&ctx, &mut p, at(&d, "Jazz", |_| true), PointerButton::Primary);
        assert_eq!(p.page, Page::Group("genres", "Jazz".into()));
        p.go(Page::StatsDetails);
        let d = reveal(&ctx, &mut p, "Harbour Lights", |_| true);
        click(&ctx, &mut p, at(&d, "Harbour Lights", |_| true), PointerButton::Primary);
        assert_eq!(p.page, Page::Album(p.lib.album_of(&b1).unwrap().key.clone()));

        // ---- the day beside the calendar: the artist is a link
        let today = realhires::stats::local_parts(now - 600.0);
        p.calendar.month = (today.year, today.month);
        p.calendar.day = Some((today.year, today.month, today.day));
        p.go(Page::Stats);
        let title = realhires::stats::day_title(today.year, today.month, today.day);
        let d = reveal(&ctx, &mut p, &title, |_| true);
        let below = at(&d, &title, |_| true).y;
        let day_artist = d
            .texts
            .iter()
            .filter(|(t, r)| t == "The Northern Quay" && r.top() > below && r.top() < below + 300.0 && r.bottom() < H - 30.0)
            .map(|(_, r)| r.center())
            .next()
            .expect("the artist in the day's list");
        click(&ctx, &mut p, day_artist, PointerButton::Primary);
        assert_eq!(p.page, Page::Group("artists", "The Northern Quay".into()));

        // ---- in Japanese
        realhires::i18n::set_thread_language("日本語");
        p.go(Page::MostPlayed);
        let d = frames(&ctx, &mut p);
        let ja = realhires::i18n::tr("Most Played");
        assert!(ja != "Most Played" && shown(&d, ja), "{ja}");
        assert!(shown(&d, realhires::i18n::tr("Songs")) && shown(&d, realhires::i18n::tr("Albums")));
        p.go(Page::Stats);
        let d = reveal(&ctx, &mut p, realhires::i18n::tr("Leaderboard"), |_| true);
        assert!(shown(&d, realhires::i18n::tr("See All")) && realhires::i18n::tr("Leaderboard") != "Leaderboard");
        realhires::i18n::set_thread_language("English");
        p.on_exit();
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
