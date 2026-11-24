//! The pages added to the player after its first version (2.0,
//! `docs/player-spec.md` "Additions"): the Library start page, Folders,
//! Playlists, Recently Played and Your Stats with its details.

use std::collections::HashMap;

use egui::{pos2, vec2, Align, Align2, Color32, CornerRadius, Layout, Rect, RichText, Sense, Stroke, Ui};
use realhires::i18n::{self, tr_name, tr_text};
use realhires::musiclib::{self, Track};
use realhires::paths::{abspath, file_name};
use realhires::stats::{self, PERIODS};
use realhires::{n_, tr};

use crate::icons::{self, Icon};
use crate::look::{self, Palette};
use crate::pages::elided;
use crate::player::{count, cover, NameAction, Page, Player};

/// The colours of the summary cards, one after the other.
const CARD_COLORS: [u32; 10] =
    [0xfa2d48, 0x7c5cff, 0x0a84ff, 0x30b06e, 0xff9f0a, 0xe0457b, 0x00a3a3, 0x5856d6, 0xc0673a, 0x8e8e93];

fn page_title(ui: &mut Ui, pal: &Palette, text: &str) {
    ui.label(RichText::new(text).font(look::title(28.0)).color(pal.text));
    ui.add_space(12.0);
}

fn pill(ui: &mut Ui, pal: &Palette, icon: Icon, label: &str) -> egui::Response {
    let font = look::title(13.0);
    let w = ui.ctx().fonts_mut(|f| f.layout_no_wrap(label.to_string(), font.clone(), Color32::WHITE).size().x) + 46.0;
    let (rect, resp) = ui.allocate_exact_size(vec2(w.max(110.0), 32.0), Sense::click());
    let fill = if resp.hovered() { pal.accent.gamma_multiply(0.85) } else { pal.accent };
    ui.painter().rect_filled(rect, CornerRadius::same(8), fill);
    icons::paint(ui.painter(), Rect::from_center_size(pos2(rect.left() + 22.0, rect.center().y), vec2(18.0, 18.0)), icon, Color32::WHITE);
    ui.painter().text(pos2(rect.left() + 36.0, rect.center().y), Align2::LEFT_CENTER, label, font, Color32::WHITE);
    resp
}

fn back_link(ui: &mut Ui, pal: &Palette, label: &str) -> egui::Response {
    let font = look::text(13.0);
    let w = ui.ctx().fonts_mut(|f| f.layout_no_wrap(label.to_string(), font.clone(), pal.accent).size().x) + 22.0;
    let (rect, resp) = ui.allocate_exact_size(vec2(w, 24.0), Sense::click());
    icons::paint(ui.painter(), Rect::from_center_size(pos2(rect.left() + 8.0, rect.center().y), vec2(16.0, 16.0)), Icon::Back, pal.accent);
    ui.painter().text(pos2(rect.left() + 18.0, rect.center().y), Align2::LEFT_CENTER, label, font, pal.accent);
    ui.add_space(6.0);
    resp
}

fn songs(n: usize) -> String {
    count(n, n_!("1 song"), n_!("{n} songs"))
}

fn albums(n: usize) -> String {
    count(n, n_!("1 album"), n_!("{n} albums"))
}

fn artists(n: usize) -> String {
    count(n, n_!("1 artist"), n_!("{n} artists"))
}

fn minutes_text(tracks: &[&Track]) -> String {
    let total: f64 = tracks.iter().map(|t| t.duration.max(0.0)).sum();
    let m = (total / 60.0).round() as i64;
    if m >= 60 {
        tr!("{h} hr {m} min", h = m / 60, m = m % 60)
    } else {
        count(m as usize, n_!("1 minute"), n_!("{n} minutes"))
    }
}

/// The short weekday names under the bars (Monday first, as `stats::WEEKDAYS`).
const WEEKDAYS_SHORT: [&str; 7] =
    [n_!("Mon"), n_!("Tue"), n_!("Wed"), n_!("Thu"), n_!("Fri"), n_!("Sat"), n_!("Sun")];

/// A summary card of Your Stats in the language in use: the label and the
/// detail are the core's texts; the big text is a name on the "top" cards.
fn shown_card(label: &str, big: &str, detail: &str) -> (String, String, String) {
    let name_card = matches!(label, "Your top song" | "Your top artist" | "Your top album" | "Your top genre");
    let big = if name_card { tr_name(big).to_string() } else { tr_text(big) };
    let detail = if label == "Your top album" { tr_name(detail).to_string() } else { tr_text(detail) };
    (tr_text(label), big, detail)
}

impl Player {
    /// Up to four album covers in a square (playlists).
    fn mosaic(&mut self, p: &egui::Painter, rect: Rect, paths: &[String], name: &str) {
        let mut keys: Vec<(String, Vec<String>)> = vec![];
        for path in paths {
            if let Some(a) = self.lib.album_of(path) {
                if !keys.iter().any(|(k, _)| *k == a.key) {
                    keys.push((a.key.clone(), a.tracks.iter().take(4).map(|t| t.path.clone()).collect()));
                }
            }
            if keys.len() == 4 {
                break;
            }
        }
        if keys.len() < 4 {
            let tex = keys.first().and_then(|(k, ps)| self.covers.get(k, ps));
            cover(p, rect, if keys.is_empty() { None } else { tex.as_ref() }, name, 8);
            return;
        }
        let half = rect.width() / 2.0;
        for (i, (k, ps)) in keys.iter().enumerate() {
            let r = Rect::from_min_size(rect.min + vec2((i % 2) as f32 * half, (i / 2) as f32 * half), vec2(half, half));
            let tex = self.covers.get(k, ps);
            cover(p, r, tex.as_ref(), name, 0);
        }
        p.rect_stroke(rect, CornerRadius::same(8), Stroke::new(0.0, Color32::TRANSPARENT), egui::StrokeKind::Inside);
    }

    // -------------------------------------------------------------- library ---

    pub fn library_home(&mut self, ui: &mut Ui, pal: &Palette) {
        let recent_n = self.recently_played().len();
        let year = {
            let saved = self.stats_period.clone();
            self.stats_period = "This year".into(); // a period key (`stats::PERIODS`)
            let st = self.stats();
            self.stats_period = saved;
            st
        };
        let l = &self.lib;
        let tiles: Vec<(Icon, &str, String, Page)> = vec![
            (Icon::Note, tr!("All Songs"), songs(l.tracks.len()), Page::AllSongs),
            (
                Icon::Folder,
                tr!("Folders"),
                count(self.scanner.cfg.library_folders.len(), n_!("1 folder"), n_!("{n} folders")),
                Page::Folders(None),
            ),
            (Icon::Albums, tr!("Albums"), albums(l.albums.len()), Page::Albums),
            (Icon::Person, tr!("Artists"), artists(l.artists.len()), Page::Artists),
            (
                Icon::People,
                tr!("Album Artists"),
                count(l.album_artists.len(), n_!("1 album artist"), n_!("{n} album artists")),
                Page::AlbumArtists,
            ),
            (Icon::Genres, tr!("Genres"), count(l.genres.len(), n_!("1 genre"), n_!("{n} genres")), Page::Genres),
            (Icon::Calendar, tr!("Years"), count(l.years.len(), n_!("1 year"), n_!("{n} years")), Page::Years),
            (
                Icon::Composer,
                tr!("Composers"),
                count(l.composers.len(), n_!("1 composer"), n_!("{n} composers")),
                Page::Composers,
            ),
            (
                Icon::Playlist,
                tr!("Playlists"),
                count(self.state.playlists.len(), n_!("1 playlist"), n_!("{n} playlists")),
                Page::Playlists,
            ),
            (Icon::Heart, tr!("Liked Songs"), songs(self.state.liked.len()), Page::Liked),
            (Icon::ThumbDown, tr!("Disliked Songs"), songs(self.state.disliked.len()), Page::Disliked),
            (Icon::Clock, tr!("Recently Played"), songs(recent_n), Page::RecentlyPlayed),
            (Icon::Albums, tr!("Recently Added"), albums(l.albums.len().min(50)), Page::Recent),
            (
                Icon::Chart,
                tr!("Your Stats"),
                tr!("{n} minutes this year", n = stats::thousands((year.seconds / 60.0).round() as i64)),
                Page::Stats,
            ),
        ];
        egui::ScrollArea::vertical().id_salt("library-home").auto_shrink([false, false]).show(ui, |ui| {
            page_title(ui, pal, tr!("Library"));
            if self.lib.tracks.is_empty() {
                ui.label(RichText::new(self.library_status().unwrap_or_else(|| {
                    tr!("Add your music folder in Settings → Library to fill your library.").into()
                })).color(pal.secondary));
                ui.add_space(8.0);
                if self.library_status().is_none() && pill(ui, pal, Icon::Folder, tr!("Choose a music folder…")).clicked() {
                    self.add_library_folder();
                }
                ui.add_space(14.0);
            }
            let gap = 12.0;
            let width = ui.available_width();
            let cols = ((width + gap) / (210.0 + gap)).floor().max(1.0) as usize;
            let tw = (width + gap) / cols as f32 - gap;
            for row in tiles.chunks(cols) {
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = gap;
                    for (icon, name, count, page) in row {
                        let (rect, resp) = ui.allocate_exact_size(vec2(tw, 74.0), Sense::click());
                        let fill = if resp.hovered() { pal.hover } else { pal.lcd };
                        ui.painter().rect_filled(rect, CornerRadius::same(10), fill);
                        icons::paint(ui.painter(), Rect::from_center_size(rect.left_top() + vec2(26.0, 26.0), vec2(22.0, 22.0)), *icon, pal.accent);
                        let g = elided(ui, name, look::title(14.0), pal.text, tw - 56.0);
                        ui.painter().galley(rect.left_top() + vec2(46.0, 16.0), g, pal.text);
                        let g = elided(ui, count, look::text(12.0), pal.secondary, tw - 56.0);
                        ui.painter().galley(rect.left_top() + vec2(46.0, 38.0), g, pal.secondary);
                        if resp.clicked() {
                            self.go(page.clone());
                        }
                    }
                });
                ui.add_space(gap - ui.spacing().item_spacing.y);
            }
            if year.logged > 0 {
                ui.add_space(14.0);
                let (rect, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 64.0), Sense::click());
                let top = look::parse_hex("#fa2d48");
                let fill = if resp.hovered() { top.gamma_multiply(0.9) } else { top };
                ui.painter().rect_filled(rect, CornerRadius::same(12), fill);
                ui.painter().text(rect.left_center() + vec2(20.0, -10.0), Align2::LEFT_CENTER, tr!("Your Stats"), look::title(16.0), Color32::WHITE);
                ui.painter().text(
                    rect.left_center() + vec2(20.0, 12.0),
                    Align2::LEFT_CENTER,
                    tr!(
                        "{minutes} minutes of music this year, {songs} by {artists}.",
                        minutes = stats::thousands((year.seconds / 60.0).round() as i64),
                        songs = songs(year.songs),
                        artists = artists(year.artists)
                    ),
                    look::text(13.0),
                    Color32::WHITE,
                );
                if resp.clicked() {
                    self.stats_period = "This year".into();
                    self.go(Page::Stats);
                }
            }
            if !self.lib.recent.is_empty() {
                ui.add_space(18.0);
                ui.label(RichText::new(tr!("Recently Added")).font(look::title(18.0)).color(pal.text));
                ui.add_space(8.0);
                let albums: Vec<usize> = self.lib.recent.iter().take(12).copied().collect();
                egui::ScrollArea::horizontal().id_salt("home-recent").show(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 18.0;
                        for a in albums {
                            self.album_card(ui, pal, a, 150.0);
                        }
                    });
                });
            }
        });
    }

    // -------------------------------------------------------------- folders ---

    pub fn folders_page(&mut self, ui: &mut Ui, pal: &Palette, folder: Option<String>) {
        let roots: Vec<String> = self.scanner.cfg.library_folders.iter().map(|f| abspath(f)).collect();
        let sep = std::path::MAIN_SEPARATOR;
        let under = |root: &str, t: &Track| t.path.starts_with(&format!("{}{sep}", root.trim_end_matches(sep)));
        // with one library folder, Folders opens it at once
        let folder = folder.or_else(|| (roots.len() == 1).then(|| roots[0].clone()));
        let Some(folder) = folder else {
            page_title(ui, pal, tr!("Folders"));
            if roots.is_empty() {
                ui.label(RichText::new(tr!("No music folder yet: add one in Settings → Library.")).color(pal.secondary));
                return;
            }
            let rows: Vec<(String, String, usize)> = roots
                .iter()
                .map(|r| (r.clone(), r.clone(), self.lib.tracks.iter().filter(|t| under(r, t)).count()))
                .collect();
            self.folder_rows(ui, pal, &rows);
            return;
        };
        // breadcrumb from the library folder that holds it
        let root = roots.iter().filter(|r| folder == **r || folder.starts_with(&format!("{}{sep}", r.trim_end_matches(sep)))).max_by_key(|r| r.len()).cloned();
        ui.horizontal_wrapped(|ui| {
            if roots.len() > 1 && back_link(ui, pal, tr!("Folders")).clicked() {
                self.go(Page::Folders(None));
            }
            if let Some(root) = &root {
                let mut path = root.clone();
                let mut parts = vec![(file_name(root), root.clone())];
                for piece in folder[root.len()..].split(sep).filter(|p| !p.is_empty()) {
                    path = format!("{}{sep}{piece}", path.trim_end_matches(sep));
                    parts.push((piece.to_string(), path.clone()));
                }
                for (i, (name, p)) in parts.iter().enumerate() {
                    if i > 0 {
                        ui.label(RichText::new("›").color(pal.secondary));
                    }
                    let last = i + 1 == parts.len();
                    let text = RichText::new(name).color(if last { pal.text } else { pal.accent });
                    if ui.add(egui::Label::new(text).sense(Sense::click())).clicked() && !last {
                        self.go(Page::Folders(Some(p.clone())));
                    }
                }
            }
        });
        ui.add_space(6.0);
        let (subs, songs) = musiclib::folder_view(&self.lib.tracks, &folder);
        page_title(ui, pal, &file_name(&folder));
        let mut all: Vec<Track> =
            self.lib.tracks.iter().filter(|t| t.path.starts_with(&format!("{}{sep}", folder.trim_end_matches(sep)))).cloned().collect();
        all.sort_by(|a, b| a.path.cmp(&b.path));
        let paths: Vec<String> = all.iter().map(|t| t.path.clone()).collect();
        if !paths.is_empty() {
            ui.horizontal(|ui| {
                let name = file_name(&folder);
                if pill(ui, pal, Icon::Play, tr!("Play")).clicked() {
                    self.play_list(paths.clone(), Some(&paths[0]), &name);
                }
                if pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                    self.shuffle_list(paths.clone(), &name);
                }
            });
            ui.add_space(10.0);
        }
        if !subs.is_empty() {
            self.folder_rows(ui, pal, &subs);
            ui.add_space(10.0);
        }
        if !songs.is_empty() {
            let list: Vec<String> = songs.iter().map(|t| t.path.clone()).collect();
            let name = file_name(&folder);
            self.song_table(ui, pal, &list, &name, false);
        }
    }

    fn folder_rows(&mut self, ui: &mut Ui, pal: &Palette, rows: &[(String, String, usize)]) {
        for (path, name, n) in rows {
            let (rect, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 36.0), Sense::click());
            if resp.hovered() {
                ui.painter().rect_filled(rect, CornerRadius::same(6), pal.hover);
            }
            icons::paint(ui.painter(), Rect::from_center_size(pos2(rect.left() + 18.0, rect.center().y), vec2(20.0, 20.0)), Icon::Folder, pal.accent);
            let g = elided(ui, name, look::text(13.5), pal.text, rect.width() - 160.0);
            ui.painter().galley(pos2(rect.left() + 38.0, rect.center().y - g.size().y / 2.0), g, pal.text);
            ui.painter().text(rect.right_center() - vec2(10.0, 0.0), Align2::RIGHT_CENTER, songs(*n), look::text(12.0), pal.secondary);
            if resp.clicked() {
                self.go(Page::Folders(Some(path.clone())));
            }
        }
    }

    // ------------------------------------------------------------ playlists ---

    pub fn playlists_page(&mut self, ui: &mut Ui, pal: &Palette) {
        page_title(ui, pal, tr!("Playlists"));
        if pill(ui, pal, Icon::Playlist, tr!("New Playlist…")).clicked() {
            self.ask_name(tr!("New Playlist"), tr!("New Playlist"), NameAction::NewPlaylist(vec![]));
        }
        ui.add_space(14.0);
        if self.state.playlists.is_empty() {
            ui.label(
                RichText::new(tr!("You have no playlists yet. Make one here, or choose “Add to playlist” in any song's menu."))
                    .color(pal.secondary),
            );
            return;
        }
        let lists: Vec<(String, String, Vec<String>)> =
            self.state.playlists.iter().map(|p| (p.id.clone(), p.name.clone(), p.songs.clone())).collect();
        let gap = 22.0;
        let width = ui.available_width();
        let cols = ((width + gap) / (180.0 + gap)).floor().max(1.0) as usize;
        let size = ((width + gap) / cols as f32 - gap).min(220.0);
        egui::ScrollArea::vertical().id_salt("playlists").auto_shrink([false, false]).show(ui, |ui| {
            for row in lists.chunks(cols) {
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = gap;
                    for (id, name, songs) in row {
                        let (rect, resp) = ui.allocate_exact_size(vec2(size, size + 44.0), Sense::click());
                        let crect = Rect::from_min_size(rect.min, vec2(size, size));
                        let p = ui.painter().clone();
                        self.mosaic(&p, crect, songs, name);
                        if resp.hovered() {
                            ui.painter().rect_filled(crect, CornerRadius::same(8), Color32::from_black_alpha(30));
                        }
                        let g = elided(ui, name, look::title(13.0), pal.text, size);
                        ui.painter().galley(pos2(rect.left(), crect.bottom() + 6.0), g, pal.text);
                        ui.painter().text(pos2(rect.left(), crect.bottom() + 24.0), Align2::LEFT_TOP, self::songs(songs.len()), look::text(12.0), pal.secondary);
                        if resp.clicked() {
                            self.go(Page::Playlist(id.clone()));
                        }
                    }
                });
                ui.add_space(gap - ui.spacing().item_spacing.y);
            }
        });
    }

    pub fn playlist_page(&mut self, ui: &mut Ui, pal: &Palette, id: &str) {
        let Some(pl) = self.state.playlist(id).cloned() else {
            self.go(Page::Playlists);
            return;
        };
        if back_link(ui, pal, tr!("Playlists")).clicked() {
            self.go(Page::Playlists);
            return;
        }
        let songs = pl.songs.clone();
        ui.horizontal(|ui| {
            let (crect, _) = ui.allocate_exact_size(vec2(180.0, 180.0), Sense::hover());
            let p = ui.painter().clone();
            self.mosaic(&p, crect, &songs, &pl.name);
            ui.add_space(22.0);
            ui.vertical(|ui| {
                ui.add_space(60.0);
                ui.label(RichText::new(&pl.name).font(look::title(26.0)).color(pal.text));
                let tracks: Vec<&Track> = songs.iter().filter_map(|p| self.lib.track(p)).collect();
                let meta = tr!("{a}, {b}", a = self::songs(songs.len()), b = minutes_text(&tracks));
                ui.label(RichText::new(meta).color(pal.secondary));
                ui.add_space(12.0);
                ui.horizontal(|ui| {
                    if !songs.is_empty() {
                        if pill(ui, pal, Icon::Play, tr!("Play")).clicked() {
                            self.play_list(songs.clone(), None, &pl.name);
                        }
                        if pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                            self.shuffle_list(songs.clone(), &pl.name);
                        }
                    }
                    if ui.button(tr!("Rename…")).clicked() {
                        self.ask_name(tr!("Rename Playlist"), &pl.name, NameAction::RenamePlaylist(pl.id.clone()));
                    }
                    if ui.button(tr!("Delete…")).clicked() {
                        self.confirm = Some(crate::player::ConfirmAction::DeletePlaylist(pl.id.clone()));
                    }
                });
            });
        });
        ui.add_space(16.0);
        if songs.is_empty() {
            ui.label(RichText::new(tr!("This playlist is empty. Add songs with “Add to playlist” in any song's menu.")).color(pal.secondary));
            return;
        }
        self.table_playlist = Some(pl.id.clone());
        self.song_table(ui, pal, &songs, &pl.name, false);
    }

    // ------------------------------------------------------ recently played ---

    pub fn recently_played_page(&mut self, ui: &mut Ui, pal: &Palette) {
        page_title(ui, pal, tr!("Recently Played"));
        let rows = self.recently_played();
        if rows.is_empty() {
            ui.label(RichText::new(tr!("Songs you play appear here.")).color(pal.secondary));
            return;
        }
        ui.horizontal(|ui| {
            let list: Vec<String> = rows.iter().map(|(p, _)| p.clone()).collect();
            if pill(ui, pal, Icon::Play, tr!("Play")).clicked() {
                self.play_list(list, None, "Recently Played");
            }
            if ui.button(tr!("Clear history…")).clicked() {
                self.confirm = Some(crate::player::ConfirmAction::ClearHistory);
            }
        });
        ui.add_space(10.0);
        let list: Vec<String> = rows.iter().map(|(p, _)| p.clone()).collect();
        let played: HashMap<String, f64> = rows.into_iter().collect();
        self.song_table_ex(ui, pal, &list, "Recently Played", false, Some(&played));
    }

    // ------------------------------------------------------------ your stats ---

    fn period_menu(&mut self, ui: &mut Ui) {
        let mut period = self.stats_period.clone();
        // the periods are shown translated and kept in English
        egui::ComboBox::from_id_salt("stats-period").selected_text(i18n::tr(&period)).width(150.0).show_ui(ui, |ui| {
            for p in PERIODS {
                ui.selectable_value(&mut period, p.to_string(), i18n::tr(p));
            }
        });
        if period != self.stats_period {
            self.stats_period = period;
        }
    }

    pub fn stats_page(&mut self, ui: &mut Ui, pal: &Palette) {
        ui.horizontal(|ui| {
            ui.label(RichText::new(tr!("Your Stats")).font(look::title(28.0)).color(pal.text));
            ui.add_space(16.0);
            self.period_menu(ui);
        });
        ui.add_space(12.0);
        let st = self.stats();
        let cards: Vec<(String, String, String)> = stats::cards(&st).iter().map(|(l, b, d)| shown_card(l, b, d)).collect();
        if cards.is_empty() {
            ui.label(RichText::new(tr!("No listening yet in this period. Play some music and come back.")).color(pal.secondary));
            return;
        }
        egui::ScrollArea::vertical().id_salt("stats").auto_shrink([false, false]).show(ui, |ui| {
            let gap = 14.0;
            let width = ui.available_width();
            let cols = ((width + gap) / (300.0 + gap)).floor().clamp(1.0, 3.0) as usize;
            let cw = (width + gap) / cols as f32 - gap;
            for (r, row) in cards.chunks(cols).enumerate() {
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = gap;
                    for (i, (label, big, detail)) in row.iter().enumerate() {
                        let k = r * cols + i;
                        let height = 140.0;
                        let (rect, _) = ui.allocate_exact_size(vec2(cw, height), Sense::hover());
                        let base = crate::style::hex(CARD_COLORS[k % CARD_COLORS.len()]);
                        ui.painter().rect_filled(rect, CornerRadius::same(14), base);
                        let p = ui.painter_at(rect);
                        p.text(rect.left_top() + vec2(18.0, 16.0), Align2::LEFT_TOP, label, look::title(12.5), Color32::from_white_alpha(220));
                        let g = elided(ui, big, look::title(if k == 0 { 40.0 } else { 26.0 }), Color32::WHITE, cw - 36.0);
                        p.galley(rect.left_top() + vec2(18.0, 38.0), g, Color32::WHITE);
                        let g = elided(ui, detail, look::text(13.0), Color32::from_white_alpha(230), cw - 36.0);
                        p.galley(pos2(rect.left() + 18.0, rect.bottom() - 30.0), g, Color32::WHITE);
                    }
                });
                ui.add_space(gap - ui.spacing().item_spacing.y);
            }
            ui.add_space(8.0);
            if pill(ui, pal, Icon::Chart, tr!("Details")).clicked() {
                self.go(Page::StatsDetails);
            }
        });
    }

    pub fn stats_details_page(&mut self, ui: &mut Ui, pal: &Palette) {
        if back_link(ui, pal, tr!("Your Stats")).clicked() {
            self.go(Page::Stats);
            return;
        }
        ui.horizontal(|ui| {
            ui.label(RichText::new(tr!("Details")).font(look::title(28.0)).color(pal.text));
            ui.add_space(16.0);
            self.period_menu(ui);
        });
        ui.add_space(10.0);
        let st = self.stats();
        let lib = stats::library_stats(&self.lib.tracks);
        egui::ScrollArea::vertical().id_salt("stats-details").auto_shrink([false, false]).show(ui, |ui| {
            if st.logged == 0 {
                ui.label(RichText::new(tr!("No listening yet in this period. Play some music and come back.")).color(pal.secondary));
            } else {
                section(ui, pal, tr!("Totals"));
                let totals = [
                    (tr!("Minutes"), stats::thousands((st.seconds / 60.0).round() as i64)),
                    (tr!("Plays"), stats::thousands(st.plays)),
                    (tr!("Songs"), stats::thousands(st.songs as i64)),
                    (tr!("Artists"), stats::thousands(st.artists as i64)),
                    (tr!("Albums"), stats::thousands(st.albums as i64)),
                    (tr!("Genres"), stats::thousands(st.genres as i64)),
                    (tr!("Days with music"), stats::thousands(st.days as i64)),
                    (tr!("Longest streak"), count(st.streak, n_!("1 day"), n_!("{n} days"))),
                ];
                ui.horizontal_wrapped(|ui| {
                    ui.spacing_mut().item_spacing = vec2(10.0, 10.0);
                    for (k, v) in totals {
                        let (rect, _) = ui.allocate_exact_size(vec2(160.0, 62.0), Sense::hover());
                        ui.painter().rect_filled(rect, CornerRadius::same(10), pal.lcd);
                        let g = elided(ui, k, look::text(12.0), pal.secondary, 136.0);
                        ui.painter().galley(rect.left_top() + vec2(14.0, 10.0), g, pal.secondary);
                        ui.painter().text(rect.left_top() + vec2(14.0, 28.0), Align2::LEFT_TOP, v, look::title(20.0), pal.text);
                    }
                });
                section(ui, pal, tr!("By hour of the day (minutes)"));
                let hours: Vec<String> =
                    (0..24).map(|h| if h % 3 == 0 { tr_text(&stats::hour_label(h)) } else { String::new() }).collect();
                bars(ui, pal, &st.by_hour.map(|x| x / 60.0), &hours);
                section(ui, pal, tr!("By day of the week (minutes)"));
                let days: Vec<String> = WEEKDAYS_SHORT.iter().map(|d| i18n::tr(d).to_string()).collect();
                bars(ui, pal, &st.by_weekday.map(|x| x / 60.0), &days);
                if st.by_month.len() > 1 {
                    section(ui, pal, tr!("By month (minutes)"));
                    let vals: Vec<f64> = st.by_month.iter().map(|(_, s)| s / 60.0).collect();
                    let labels: Vec<String> = st
                        .by_month
                        .iter()
                        .map(|(m, _)| {
                            let (y, mo) = m.split_at(4);
                            let mo: usize = mo.trim_start_matches('-').parse().unwrap_or(1);
                            tr!("{month} {year}", month = i18n::tr(stats::MONTHS[mo.clamp(1, 12) - 1]), year = y)
                        })
                        .collect();
                    bars(ui, pal, &vals, &labels);
                }
                ui.add_space(8.0);
                ui.columns(2, |cols| {
                    top_list(&mut cols[0], pal, tr!("Top songs"), &st.top_songs, true);
                    top_list(&mut cols[1], pal, tr!("Top artists"), &st.top_artists, false);
                });
                ui.columns(2, |cols| {
                    top_list(&mut cols[0], pal, tr!("Top albums"), &st.top_albums, true);
                    top_list(&mut cols[1], pal, tr!("Top genres"), &st.top_genres, false);
                });
            }
            section(ui, pal, tr!("Your library"));
            let gb = lib.bytes as f64 / 1e9;
            ui.label(
                RichText::new(tr!(
                    "{songs}, {albums} by {artists}; {time} of music, {size}.",
                    songs = songs(lib.songs),
                    albums = albums(lib.albums),
                    artists = artists(lib.artists),
                    time = {
                        let m = (lib.seconds / 60.0).round() as i64;
                        if m >= 60 {
                            tr!("{h} hr {m} min", h = stats::thousands(m / 60), m = m % 60)
                        } else {
                            tr!("{m} min", m = m)
                        }
                    },
                    size = if gb >= 1.0 {
                        format!("{} GB", realhires::pyfmt::f(gb, 1))
                    } else {
                        format!("{} MB", realhires::pyfmt::f(gb * 1000.0, 0))
                    }
                ))
                .color(pal.text),
            );
            ui.add_space(6.0);
            // the core's names ("Hi-Res Lossless", "24-bit", "lossy"), translated as the Python app does
            let words = |rows: &[(String, usize)]| -> Vec<(String, usize)> {
                rows.iter().map(|(k, n)| (tr_text(k), *n)).collect()
            };
            ui.columns(4, |cols| {
                kv_list(&mut cols[0], pal, tr!("Quality"), &words(&lib.qualities));
                kv_list(&mut cols[1], pal, tr!("Formats"), &lib.formats);
                kv_list(
                    &mut cols[2],
                    pal,
                    tr!("Sample rates"),
                    &lib.rates.iter().map(|(r, n)| (if *r > 0 { crate::eqwin::khz(*r) } else { "?".into() }, *n)).collect::<Vec<_>>(),
                );
                kv_list(&mut cols[3], pal, tr!("Bit depths"), &words(&lib.bits));
            });
        });
    }
}

fn section(ui: &mut Ui, pal: &Palette, text: &str) {
    ui.add_space(14.0);
    ui.label(RichText::new(text).font(look::title(17.0)).color(pal.text));
    ui.add_space(6.0);
}

/// A bar chart with a label under every bar that has one.
fn bars(ui: &mut Ui, pal: &Palette, values: &[f64], labels: &[String]) {
    let (rect, _) = ui.allocate_exact_size(vec2(ui.available_width().min(900.0), 130.0), Sense::hover());
    let p = ui.painter_at(rect);
    let max = values.iter().copied().fold(0.0f64, f64::max).max(1e-9);
    let n = values.len().max(1) as f32;
    let slot = rect.width() / n;
    let chart = Rect::from_min_max(rect.min, pos2(rect.right(), rect.bottom() - 18.0));
    p.line_segment([chart.left_bottom(), chart.right_bottom()], Stroke::new(0.5, pal.separator));
    for (i, v) in values.iter().enumerate() {
        let h = (v / max) as f32 * (chart.height() - 14.0);
        let x = rect.left() + slot * i as f32;
        let bar = Rect::from_min_max(pos2(x + slot * 0.18, chart.bottom() - h), pos2(x + slot * 0.82, chart.bottom()));
        p.rect_filled(bar, CornerRadius::same(3), pal.accent);
        if *v > 0.0 && slot > 26.0 {
            p.text(pos2(bar.center().x, bar.top() - 2.0), Align2::CENTER_BOTTOM, format!("{}", v.round() as i64), look::text(9.5), pal.secondary);
        }
        if let Some(l) = labels.get(i).filter(|l| !l.is_empty()) {
            p.text(pos2(x + slot / 2.0, rect.bottom() - 2.0), Align2::CENTER_BOTTOM, l, look::text(10.0), pal.secondary);
        }
    }
}

fn top_list(ui: &mut Ui, pal: &Palette, title: &str, rows: &[stats::Row], with_artist: bool) {
    section(ui, pal, title);
    for (i, r) in rows.iter().take(50).enumerate() {
        ui.horizontal(|ui| {
            ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                ui.label(RichText::new(format!("{:>2}", i + 1)).color(pal.secondary).monospace());
                let mut text = tr_name(&r.name).to_string();
                if with_artist && !r.artist.is_empty() {
                    text = format!("{text} — {}", tr_name(&r.artist));
                }
                let w = (ui.available_width() - 140.0).max(60.0);
                let g = elided(ui, &text, look::text(13.0), pal.text, w);
                ui.label(g);
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    ui.label(
                        RichText::new(tr!(
                            "{plays} · {minutes} min",
                            plays = count(r.plays.max(0) as usize, n_!("1 play"), n_!("{n} plays")),
                            minutes = stats::thousands((r.seconds / 60.0).round() as i64)
                        ))
                        .color(pal.secondary),
                    );
                });
            });
        });
    }
}

fn kv_list(ui: &mut Ui, pal: &Palette, title: &str, rows: &[(String, usize)]) {
    ui.label(RichText::new(title).font(look::title(13.5)).color(pal.text));
    for (k, n) in rows {
        ui.horizontal(|ui| {
            ui.label(RichText::new(k).color(pal.text));
            ui.label(RichText::new(stats::thousands(*n as i64)).color(pal.secondary));
        });
    }
}
