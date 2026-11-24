//! The pages of the player window (2.0, `docs/player-spec.md`): album cards,
//! the album page, song tables, artists, genres, search, Now Playing with the
//! lyrics, and Settings with every function of the 1.x app.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use egui::{pos2, vec2, Align, Align2, Color32, CornerRadius, FontId, Galley, Layout, Rect, RichText, Sense, Stroke, Ui};
use realhires::i18n::{self, tr_name, tr_text};
use realhires::musiclib::{Album, Track};
use realhires::pyunicode::casefold;
use realhires::settings::{Settings, GROUPS};
use realhires::{n_, tr};

use crate::icons::{self, Icon};
use crate::look::{self, Palette};
use crate::player::{artist_album, cover, fmt_time, quality_label, Page, Player, ShownLyrics, SongCol};

/// One line of text, cut with "…" to `width`.
pub fn elided(ui: &Ui, text: &str, font: FontId, color: Color32, width: f32) -> Arc<Galley> {
    let mut job = egui::text::LayoutJob::single_section(text.to_string(), egui::TextFormat::simple(font, color));
    job.wrap = egui::text::TextWrapping { max_width: width.max(10.0), max_rows: 1, break_anywhere: true, overflow_character: Some('…') };
    ui.ctx().fonts_mut(|f| f.layout_job(job))
}

fn page_title(ui: &mut Ui, pal: &Palette, text: &str) {
    ui.label(RichText::new(text).font(look::title(28.0)).color(pal.text));
    ui.add_space(12.0);
}

/// A filled accent button with an icon (Play, Shuffle).
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

fn minutes(tracks: &[&Track]) -> String {
    let total: f64 = tracks.iter().map(|t| t.duration.max(0.0)).sum();
    let m = (total / 60.0).round() as i64;
    let songs = crate::style::count(tracks.len(), n_!("1 song"), n_!("{n} songs"));
    let time = if m >= 60 {
        tr!("{h} hr {m} min", h = m / 60, m = m % 60)
    } else {
        crate::style::count(m as usize, n_!("1 minute"), n_!("{n} minutes"))
    };
    tr!("{a}, {b}", a = songs, b = time)
}

fn matches(t: &Track, q: &str) -> bool {
    [t.display_title(), t.display_artist(), t.display_album(), t.genre.clone(), t.album_artist.clone()]
        .iter()
        .any(|f| casefold(f).contains(q))
}

impl Player {
    pub fn content(&mut self, ui: &mut Ui, pal: &Palette) {
        let page = self.page.clone();
        let needs_songs = !matches!(
            page,
            Page::Settings | Page::NowPlaying | Page::Quality | Page::Library | Page::Stats | Page::StatsDetails
                | Page::Playlists | Page::Playlist(_) | Page::RecentlyPlayed
        );
        if self.lib.tracks.is_empty() && needs_songs {
            self.empty_library(ui, pal);
            return;
        }
        self.table_playlist = None;
        match page {
            Page::Library => self.library_home(ui, pal),
            Page::Recent => {
                page_title(ui, pal, tr!("Recently Added"));
                let list = self.lib.recent.clone();
                self.album_grid(ui, pal, &list);
            }
            Page::Albums => {
                page_title(ui, pal, tr!("Albums"));
                let list: Vec<usize> = (0..self.lib.albums.len()).collect();
                self.album_grid(ui, pal, &list);
            }
            Page::Album(key) => self.album_page(ui, pal, &key),
            Page::Artists => self.group_page(ui, pal, "artists"),
            Page::AlbumArtists => self.group_page(ui, pal, "album_artists"),
            Page::Genres => self.group_page(ui, pal, "genres"),
            Page::Years => self.group_page(ui, pal, "years"),
            Page::Composers => self.group_page(ui, pal, "composers"),
            Page::AllSongs => {
                page_title(ui, pal, tr!("All Songs"));
                let list: Vec<String> = self.sorted_songs(self.lib.tracks.iter().collect()).iter().map(|t| t.path.clone()).collect();
                ui.horizontal(|ui| {
                    if pill(ui, pal, Icon::Play, tr!("Play")).clicked() {
                        self.play_list(list.clone(), Some(&list[0]), "All Songs");
                    }
                    if pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                        self.shuffle_list(list.clone(), "All Songs");
                    }
                });
                ui.add_space(10.0);
                self.song_table(ui, pal, &list, "All Songs", true);
            }
            Page::Folders(folder) => self.folders_page(ui, pal, folder),
            Page::Playlists => self.playlists_page(ui, pal),
            Page::Playlist(id) => self.playlist_page(ui, pal, &id),
            Page::Liked | Page::Disliked => {
                let liked = page == Page::Liked;
                // the English name is the queue's (and the table's id); shown translated
                let title = if liked { "Liked Songs" } else { "Disliked Songs" };
                page_title(ui, pal, i18n::tr(title));
                let songs: Vec<&Track> = if liked {
                    self.liked_songs()
                } else {
                    self.lib.tracks.iter().filter(|t| self.state.is_disliked(&t.path)).collect()
                };
                let list: Vec<String> = self.sorted_songs(songs).iter().map(|t| t.path.clone()).collect();
                if list.is_empty() {
                    let hint = if liked {
                        tr!("Songs you like appear here. Click the heart next to a song, or in the playback bar while it plays.")
                    } else {
                        tr!("Songs you dislike appear here, and shuffling leaves them out. Dislike a song with the thumbs-down button in the playback bar or from a song's menu.")
                    };
                    ui.label(RichText::new(hint).color(pal.secondary));
                } else {
                    ui.horizontal(|ui| {
                        if pill(ui, pal, Icon::Play, tr!("Play")).clicked() {
                            self.play_list(list.clone(), Some(&list[0]), title);
                        }
                        if liked && pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                            self.shuffle_list(list.clone(), title);
                        }
                    });
                    ui.add_space(10.0);
                    self.song_table(ui, pal, &list, title, true);
                }
            }
            Page::RecentlyPlayed => self.recently_played_page(ui, pal),
            Page::Stats => self.stats_page(ui, pal),
            Page::StatsDetails => self.stats_details_page(ui, pal),
            Page::Search => self.search_page(ui, pal),
            Page::NowPlaying => self.now_playing(ui, pal),
            Page::Settings => self.settings_page(ui, pal),
            Page::Quality => {}
        }
    }

    fn empty_library(&mut self, ui: &mut Ui, pal: &Palette) {
        ui.add_space(ui.available_height() * 0.3);
        ui.vertical_centered(|ui| {
            icons::paint(ui.painter(), Rect::from_center_size(ui.cursor().center_top() + vec2(0.0, 30.0), vec2(64.0, 64.0)), Icon::Note, pal.secondary);
            ui.add_space(72.0);
            if let Some(s) = self.library_status() {
                ui.label(RichText::new(s).font(look::title(18.0)).color(pal.text));
                return;
            }
            let has_folders = !self.scanner.cfg.library_folders.is_empty();
            let head = if has_folders { tr!("No songs found in your music folders") } else { tr!("Add your music folder in Settings to start") };
            ui.label(RichText::new(head).font(look::title(18.0)).color(pal.text));
            ui.add_space(6.0);
            ui.label(
                RichText::new(tr!(
                    "{app} plays your music and checks whether its hi-res and lossless files are real.",
                    app = realhires::APP_NAME
                ))
                .color(pal.secondary),
            );
            ui.add_space(14.0);
            if pill(ui, pal, Icon::Albums, tr!("Choose a music folder…")).clicked() {
                self.add_library_folder();
            }
        });
    }

    pub fn album_card(&mut self, ui: &mut Ui, pal: &Palette, album: usize, size: f32) {
        let (rect, resp) = ui.allocate_exact_size(vec2(size, size + 44.0), Sense::click());
        if !ui.is_rect_visible(rect) {
            return;
        }
        let a = &self.lib.albums[album];
        let key = a.key.clone();
        let (title, artist) = (a.title.clone(), a.artist.clone());
        let paths: Vec<String> = a.tracks.iter().map(|t| t.path.clone()).collect();
        let tex = self.covers.get(&key, &paths);
        let crect = Rect::from_min_size(rect.min, vec2(size, size));
        cover(ui.painter(), crect, tex.as_ref(), &title, 8);
        if resp.hovered() {
            ui.painter().rect_filled(crect, CornerRadius::same(8), Color32::from_black_alpha(40));
            let play = Rect::from_center_size(crect.left_bottom() + vec2(24.0, -24.0), vec2(32.0, 32.0));
            ui.painter().circle_filled(play.center(), 16.0, pal.accent);
            icons::paint(ui.painter(), play.shrink(7.0), Icon::Play, Color32::WHITE);
        }
        let g = elided(ui, tr_name(&title), look::title(13.0), pal.text, size);
        ui.painter().galley(pos2(rect.left(), crect.bottom() + 6.0), g, pal.text);
        let g = elided(ui, tr_name(&artist), look::text(12.0), pal.secondary, size);
        ui.painter().galley(pos2(rect.left(), crect.bottom() + 24.0), g, pal.secondary);
        if resp.clicked() {
            let quick = resp.interact_pointer_pos().is_some_and(|p| (p - (crect.left_bottom() + vec2(24.0, -24.0))).length() < 17.0);
            if quick {
                self.play_list(paths, None, &title);
            } else {
                self.go(Page::Album(key));
            }
        }
    }

    fn album_grid(&mut self, ui: &mut Ui, pal: &Palette, albums: &[usize]) {
        let gap = 22.0;
        let width = ui.available_width();
        let cols = ((width + gap) / (180.0 + gap)).floor().max(1.0) as usize;
        let size = ((width + gap) / cols as f32 - gap).min(240.0);
        let rows = albums.len().div_ceil(cols);
        egui::ScrollArea::vertical().auto_shrink([false, false]).id_salt(("albums", self.page.title())).show_rows(
            ui,
            size + 44.0 + gap,
            rows,
            |ui, range| {
                for r in range {
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = gap;
                        for &a in albums.iter().skip(r * cols).take(cols) {
                            self.album_card(ui, pal, a, size);
                        }
                    });
                    ui.add_space(gap - ui.spacing().item_spacing.y);
                }
            },
        );
    }

    fn album_page(&mut self, ui: &mut Ui, pal: &Palette, key: &str) {
        let Some(&idx) = self.lib.album_index.get(key) else {
            self.go(Page::Albums);
            return;
        };
        if !self.back.is_empty() && back_link(ui, pal, tr!("Back")).clicked() {
            self.go_back();
            return;
        }
        let a: Album = self.lib.albums[idx].clone();
        let paths: Vec<String> = a.tracks.iter().map(|t| t.path.clone()).collect();
        ui.horizontal(|ui| {
            let (crect, _) = ui.allocate_exact_size(vec2(220.0, 220.0), Sense::hover());
            let tex = self.covers.get(&a.key, &paths);
            cover(ui.painter(), crect, tex.as_ref(), &a.title, 10);
            ui.add_space(22.0);
            ui.vertical(|ui| {
                ui.add_space(70.0);
                ui.label(RichText::new(tr_name(&a.title)).font(look::title(26.0)).color(pal.text));
                ui.label(RichText::new(tr_name(&a.artist)).font(look::title(18.0)).color(pal.accent));
                let meta: Vec<&str> = [a.genre.as_str(), a.year.as_str()].into_iter().filter(|s| !s.is_empty()).collect();
                if !meta.is_empty() {
                    ui.label(RichText::new(meta.join(" · ")).color(pal.secondary));
                }
                ui.add_space(14.0);
                ui.horizontal(|ui| {
                    if pill(ui, pal, Icon::Play, tr!("Play")).clicked() {
                        self.play_list(paths.clone(), Some(&paths[0]), &a.title);
                    }
                    if pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                        self.shuffle_list(paths.clone(), &a.title);
                    }
                    ui.add_space(6.0);
                    if ui
                        .add_enabled(self.edit_info.is_none(), egui::Button::new(tr!("Edit album info…")))
                        .on_hover_text(tr!("Change the album, album artist, genre, release date… of all its songs"))
                        .clicked()
                    {
                        self.edit_album_info(&a);
                    }
                });
            });
        });
        ui.add_space(18.0);
        let now = self.now.as_ref().map(|n| n.path.clone());
        let mut play = None;
        let mut like = None;
        egui::ScrollArea::vertical().auto_shrink([false, false]).id_salt(("album", key)).show(ui, |ui| {
            let multi_disc = a.tracks.iter().any(|t| t.disc > 1);
            let mut last_disc = 0;
            for t in &a.tracks {
                if multi_disc && t.disc != last_disc {
                    last_disc = t.disc;
                    ui.add_space(6.0);
                    ui.label(RichText::new(tr!("Disc {n}", n = t.disc.max(1))).font(look::title(13.0)).color(pal.secondary));
                }
                let (rect, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 38.0), Sense::click());
                let selected = self.selected_song.as_deref() == Some(t.path.as_str());
                let playing = now.as_deref() == Some(t.path.as_str());
                if selected {
                    ui.painter().rect_filled(rect, CornerRadius::same(6), pal.hover);
                } else if resp.hovered() {
                    ui.painter().rect_filled(rect, CornerRadius::same(6), pal.hover.gamma_multiply(0.6));
                }
                ui.painter().line_segment([rect.left_bottom() + vec2(36.0, 0.0), rect.right_bottom()], Stroke::new(0.5, pal.separator));
                let p = ui.painter_at(rect);
                if playing {
                    icons::paint(&p, Rect::from_center_size(pos2(rect.left() + 18.0, rect.center().y), vec2(16.0, 16.0)), Icon::Speaker, pal.accent);
                } else {
                    let n = if t.track > 0 { t.track.to_string() } else { String::new() };
                    p.text(pos2(rect.left() + 26.0, rect.center().y), Align2::RIGHT_CENTER, n, look::text(13.0), pal.secondary);
                }
                let title_color = if playing { pal.accent } else { pal.text };
                let g = elided(ui, &t.display_title(), look::text(13.5), title_color, rect.width() - 200.0);
                p.galley(pos2(rect.left() + 40.0, rect.center().y - g.size().y / 2.0), g, title_color);
                if t.artist != a.artist && !t.artist.is_empty() {
                    // shown only when a track's artist differs from the album's
                }
                p.text(pos2(rect.right() - 10.0, rect.center().y), Align2::RIGHT_CENTER, fmt_time(t.duration), look::text(12.5), pal.secondary);
                let heart = Rect::from_center_size(pos2(rect.right() - 76.0, rect.center().y), vec2(18.0, 18.0));
                let liked = self.state.is_liked(&t.path);
                if liked || resp.hovered() {
                    icons::paint(&p, heart, if liked { Icon::HeartFilled } else { Icon::Heart }, pal.accent);
                }
                if resp.clicked() {
                    if resp.interact_pointer_pos().is_some_and(|q| heart.expand(4.0).contains(q)) {
                        like = Some(t.path.clone());
                    } else {
                        self.selected_song = Some(t.path.clone());
                    }
                }
                if resp.double_clicked() {
                    play = Some(t.path.clone());
                }
                let path = t.path.clone();
                resp.context_menu(|ui| self.song_menu(ui, &path));
            }
            ui.add_space(10.0);
            let refs: Vec<&Track> = a.tracks.iter().collect();
            ui.label(RichText::new(minutes(&refs)).color(pal.secondary));
        });
        if let Some(p) = like {
            self.toggle_like(&p);
        }
        if let Some(p) = play {
            self.play_list(paths, Some(&p), &a.title);
        }
        if ui.input(|i| i.key_pressed(egui::Key::Enter)) && !ui.ctx().egui_wants_keyboard_input() {
            if let Some(p) = self.selected_song.clone().filter(|p| a.tracks.iter().any(|t| t.path == *p)) {
                let paths: Vec<String> = a.tracks.iter().map(|t| t.path.clone()).collect();
                self.play_list(paths, Some(&p), &a.title);
            }
        }
    }

    /// The right-click menu of a song.
    pub fn song_menu(&mut self, ui: &mut Ui, path: &str) {
        let liked = self.state.is_liked(path);
        if ui.button(if liked { tr!("Unlike") } else { tr!("Like") }).clicked() {
            self.toggle_like(path);
            ui.close();
        }
        let disliked = self.state.is_disliked(path);
        if ui.button(if disliked { tr!("Remove dislike") } else { tr!("Dislike") }).clicked() {
            self.toggle_dislike(path);
            ui.close();
        }
        ui.menu_button(tr!("Add to playlist"), |ui| {
            let lists: Vec<(String, String)> = self.state.playlists.iter().map(|p| (p.id.clone(), p.name.clone())).collect();
            for (id, name) in lists {
                if ui.button(name).clicked() {
                    self.add_to_playlist(&id, &[path.to_string()]);
                    ui.close();
                }
            }
            if !self.state.playlists.is_empty() {
                ui.separator();
            }
            if ui.button(tr!("New Playlist…")).clicked() {
                self.ask_name(tr!("New Playlist"), tr!("New Playlist"), crate::player::NameAction::NewPlaylist(vec![path.to_string()]));
                ui.close();
            }
        });
        if let Some(id) = self.table_playlist.clone() {
            ui.separator();
            if ui.button(tr!("Remove from this playlist")).clicked() {
                self.state.remove_from_playlist(&id, &[path.to_string()]);
                self.save_state();
                ui.close();
            }
            if ui.button(tr!("Move up")).clicked() {
                self.state.move_in_playlist(&id, path, -1);
                self.save_state();
                ui.close();
            }
            if ui.button(tr!("Move down")).clicked() {
                self.state.move_in_playlist(&id, path, 1);
                self.save_state();
                ui.close();
            }
            ui.separator();
        }
        if let Some(a) = self.lib.album_of(path) {
            let key = a.key.clone();
            if ui.button(tr!("Go to the album")).clicked() {
                self.go(Page::Album(key));
                ui.close();
            }
        }
        if ui.button(tr!("Get lyrics…")).clicked() {
            self.scanner.download_lyrics(vec![path.to_string()]);
            ui.close();
        }
        if ui.add_enabled(self.edit_info.is_none(), egui::Button::new(tr!("Edit info…"))).clicked() {
            self.edit_song_info(path);
            ui.close();
        }
        if ui.button(tr!("Check its quality")).clicked() {
            self.scanner.clear();
            self.scanner.add_source(path);
            self.scanner.start_scan(None, false);
            self.go(Page::Quality);
            ui.close();
        }
        if ui.button(tr!("Show in folder")).clicked() {
            crate::style::reveal_in_file_manager(path);
            ui.close();
        }
    }

    /// A table of songs (Songs, Liked Songs, a genre, search results).
    pub fn song_table(&mut self, ui: &mut Ui, pal: &Palette, list: &[String], name: &str, sortable: bool) {
        self.song_table_ex(ui, pal, list, name, sortable, None);
    }

    /// The same, with a "Played" column instead of Genre (Recently Played).
    pub fn song_table_ex(
        &mut self,
        ui: &mut Ui,
        pal: &Palette,
        list: &[String],
        name: &str,
        sortable: bool,
        played: Option<&HashMap<String, f64>>,
    ) {
        let w = ui.available_width();
        // the English headings are ids; they are shown translated
        let cols: [(SongCol, &str, f32); 6] = [
            (SongCol::Title, n_!("Title"), 0.30),
            (SongCol::Artist, n_!("Artist"), 0.20),
            (SongCol::Album, n_!("Album"), 0.22),
            (SongCol::Genre, if played.is_some() { n_!("Played") } else { n_!("Genre") }, 0.11),
            (SongCol::Time, n_!("Time"), 0.06),
            (SongCol::Quality, n_!("Quality"), 0.11),
        ];
        let sortable = sortable && played.is_none();
        let heart_w = 30.0;
        let usable = w - heart_w;
        let mut xs = vec![];
        let mut x = heart_w;
        for (_, _, f) in &cols {
            xs.push(x);
            x += usable * f;
        }
        // header
        let (hrect, _) = ui.allocate_exact_size(vec2(w, 26.0), Sense::hover());
        for (i, (col, label, f)) in cols.iter().enumerate() {
            let r = Rect::from_min_size(pos2(hrect.left() + xs[i], hrect.top()), vec2(usable * f, 26.0));
            let resp = ui.interact(r, ui.id().with(("song-head", *label)), if sortable { Sense::click() } else { Sense::hover() });
            let arrow = if sortable && self.song_sort.0 == *col {
                if self.song_sort.1 { "  ▼" } else { "  ▲" }
            } else {
                ""
            };
            let color = if resp.hovered() { pal.text } else { pal.secondary };
            // cut with "…" to the column, so a long heading never runs into the next one
            let g = elided(ui, &format!("{}{arrow}", i18n::tr(label)), look::title(11.5), color, r.width() - 6.0);
            ui.painter().galley(pos2(r.left() + 4.0, r.center().y - g.size().y / 2.0), g, color);
            if resp.clicked() {
                if self.song_sort.0 == *col {
                    self.song_sort.1 = !self.song_sort.1;
                } else {
                    self.song_sort = (*col, false);
                }
            }
        }
        ui.painter().line_segment([hrect.left_bottom(), hrect.right_bottom()], Stroke::new(0.5, pal.separator));
        let now = self.now.as_ref().map(|n| n.path.clone());
        let mut play = None;
        let mut like = None;
        egui::ScrollArea::vertical().auto_shrink([false, false]).id_salt(("songs", name)).show_rows(ui, 30.0, list.len(), |ui, range| {
            for i in range {
                let path = &list[i];
                let Some(t) = self.lib.track(path).cloned() else {
                    // a playlist song whose file is gone
                    let (rect, resp) = ui.allocate_exact_size(vec2(w, 30.0), Sense::click());
                    let p = ui.painter_at(rect);
                    let g = elided(ui, &realhires::paths::file_name(path), look::text(13.0), pal.secondary.gamma_multiply(0.7), usable * 0.5);
                    p.galley(pos2(rect.left() + xs[0] + 4.0, rect.center().y - g.size().y / 2.0), g, pal.secondary);
                    p.text(pos2(rect.left() + xs[2] + 4.0, rect.center().y), Align2::LEFT_CENTER, tr!("Not in the library"), look::text(12.0), pal.secondary.gamma_multiply(0.7));
                    let p2 = path.clone();
                    resp.context_menu(|ui| self.song_menu(ui, &p2));
                    continue;
                };
                let (rect, resp) = ui.allocate_exact_size(vec2(w, 30.0), Sense::click());
                let selected = self.selected_song.as_deref() == Some(path.as_str());
                let playing = now.as_deref() == Some(path.as_str());
                if selected {
                    ui.painter().rect_filled(rect, CornerRadius::same(5), pal.hover);
                } else if i % 2 == 1 {
                    ui.painter().rect_filled(rect, CornerRadius::same(5), if pal.dark { Color32::from_white_alpha(6) } else { Color32::from_black_alpha(6) });
                }
                let p = ui.painter_at(rect);
                let heart = Rect::from_center_size(pos2(rect.left() + 15.0, rect.center().y), vec2(16.0, 16.0));
                let liked = self.state.is_liked(path);
                if liked || resp.hovered() {
                    icons::paint(&p, heart, if liked { Icon::HeartFilled } else { Icon::Heart }, pal.accent);
                }
                let fourth = match played {
                    Some(m) => played_label(m.get(path).copied().unwrap_or(0.0)),
                    None => t.genre.clone(),
                };
                let vals = [
                    t.display_title(),
                    tr_name(&t.display_artist()).to_string(),
                    tr_name(&t.display_album()).to_string(),
                    fourth,
                    fmt_time(t.duration),
                    quality_label(&t),
                ];
                for (k, v) in vals.iter().enumerate() {
                    let color = if k == 0 && playing { pal.accent } else if k == 0 { pal.text } else { pal.secondary };
                    let font = if k == 0 { look::text(13.0) } else { look::text(12.5) };
                    let g = elided(ui, v, font, color, usable * cols[k].2 - 10.0);
                    p.galley(pos2(rect.left() + xs[k] + 4.0, rect.center().y - g.size().y / 2.0), g, color);
                }
                if resp.clicked() {
                    if resp.interact_pointer_pos().is_some_and(|q| heart.expand(5.0).contains(q)) {
                        like = Some(path.clone());
                    } else {
                        self.selected_song = Some(path.clone());
                    }
                }
                if resp.double_clicked() {
                    play = Some(path.clone());
                }
                let p2 = path.clone();
                resp.context_menu(|ui| self.song_menu(ui, &p2));
            }
        });
        if let Some(p) = like {
            self.toggle_like(&p);
        }
        if let Some(p) = play {
            self.play_list(list.to_vec(), Some(&p), name);
        }
        if ui.input(|i| i.key_pressed(egui::Key::Enter)) && !ui.ctx().egui_wants_keyboard_input() {
            if let Some(p) = self.selected_song.clone().filter(|p| list.contains(p)) {
                self.play_list(list.to_vec(), Some(&p), name);
            }
        }
    }

    pub fn side_list(&mut self, ui: &mut Ui, pal: &Palette, names: &[(String, usize)], selected: &Option<String>) -> Option<String> {
        let mut pick = None;
        egui::ScrollArea::vertical().id_salt(("side", self.page.title())).auto_shrink([false, false]).show(ui, |ui| {
            for (name, n) in names {
                let (rect, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 30.0), Sense::click());
                let sel = selected.as_deref() == Some(name.as_str());
                if sel {
                    ui.painter().rect_filled(rect, CornerRadius::same(6), pal.accent);
                } else if resp.hovered() {
                    ui.painter().rect_filled(rect, CornerRadius::same(6), pal.hover);
                }
                let color = if sel { Color32::WHITE } else { pal.text };
                let g = elided(ui, tr_name(name), look::text(13.0), color, rect.width() - 50.0);
                ui.painter().galley(pos2(rect.left() + 10.0, rect.center().y - g.size().y / 2.0), g, color);
                ui.painter().text(pos2(rect.right() - 8.0, rect.center().y), Align2::RIGHT_CENTER, n.to_string(), look::text(11.0), if sel { Color32::WHITE } else { pal.secondary });
                if resp.clicked() {
                    pick = Some(name.clone());
                }
            }
        });
        pick
    }

    /// Artists, Album Artists (album cards), Genres, Years, Composers (song
    /// tables): the names on the left, the chosen one's music on the right.
    fn group_page(&mut self, ui: &mut Ui, pal: &Palette, kind: &'static str) {
        let (title, groups, cards) = match kind {
            "artists" => ("Artists", &self.lib.artists, true),
            "album_artists" => ("Album Artists", &self.lib.album_artists, true),
            "genres" => ("Genres", &self.lib.genres, false),
            "years" => ("Years", &self.lib.years, false),
            _ => ("Composers", &self.lib.composers, false),
        };
        page_title(ui, pal, i18n::tr(title));
        let names: Vec<(String, usize)> = groups.iter().map(|g| (g.name.clone(), g.tracks.len())).collect();
        if self.group_sel.get(kind).is_none_or(|s| !names.iter().any(|(n, _)| n == s)) {
            if let Some((n, _)) = names.first() {
                self.group_sel.insert(kind, n.clone());
            }
        }
        let sel = self.group_sel.get(kind).cloned();
        let height = ui.available_height();
        ui.horizontal_top(|ui| {
            ui.allocate_ui_with_layout(vec2(240.0, height), Layout::top_down(Align::Min), |ui| {
                if let Some(p) = self.side_list(ui, pal, &names, &sel) {
                    self.group_sel.insert(kind, p);
                }
            });
            ui.add_space(20.0);
            ui.vertical(|ui| {
                let Some(name) = sel else { return };
                let groups = match kind {
                    "artists" => &self.lib.artists,
                    "album_artists" => &self.lib.album_artists,
                    "genres" => &self.lib.genres,
                    "years" => &self.lib.years,
                    _ => &self.lib.composers,
                };
                let Some(g) = groups.iter().find(|g| g.name == name) else { return };
                let tracks = g.tracks.clone();
                ui.horizontal(|ui| {
                    ui.label(RichText::new(tr_name(&name)).font(look::title(22.0)).color(pal.text));
                    ui.add_space(14.0);
                    let list: Vec<String> = tracks.iter().map(|t| t.path.clone()).collect();
                    if pill(ui, pal, Icon::Play, tr!("Play")).clicked() {
                        self.play_list(list.clone(), Some(&list[0]), &name);
                    }
                    if pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                        self.shuffle_list(list, &name);
                    }
                });
                ui.add_space(12.0);
                if cards {
                    let mut keys: Vec<String> = vec![];
                    for t in &tracks {
                        let key = t.song().album_key();
                        if !keys.contains(&key) {
                            keys.push(key);
                        }
                    }
                    let albums: Vec<usize> = keys.iter().filter_map(|k| self.lib.album_index.get(k).copied()).collect();
                    self.album_grid(ui, pal, &albums);
                } else {
                    let list: Vec<String> = self.sorted_songs(tracks.iter().collect()).iter().map(|t| t.path.clone()).collect();
                    self.song_table(ui, pal, &list, &name, true);
                }
            });
        });
    }

    fn search_page(&mut self, ui: &mut Ui, pal: &Palette) {
        let q = casefold(&self.search_query);
        page_title(ui, pal, &tr!("Results for “{query}”", query = self.search_query));
        let albums: Vec<usize> = self
            .lib
            .albums
            .iter()
            .enumerate()
            .filter(|(_, a)| casefold(&a.title).contains(&q) || casefold(&a.artist).contains(&q) || casefold(&a.genre).contains(&q))
            .map(|(i, _)| i)
            .take(60)
            .collect();
        let songs: Vec<String> = self.lib.tracks.iter().filter(|t| matches(t, &q)).take(2000).map(|t| t.path.clone()).collect();
        if albums.is_empty() && songs.is_empty() {
            ui.label(RichText::new(tr!("Nothing in the library matches.")).color(pal.secondary));
            return;
        }
        if !albums.is_empty() {
            ui.label(RichText::new(tr!("Albums")).font(look::title(17.0)).color(pal.text));
            ui.add_space(6.0);
            egui::ScrollArea::horizontal().id_salt("search-albums").show(ui, |ui| {
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 18.0;
                    for a in &albums {
                        self.album_card(ui, pal, *a, 150.0);
                    }
                });
            });
            ui.add_space(14.0);
        }
        if !songs.is_empty() {
            ui.label(RichText::new(tr!("Songs")).font(look::title(17.0)).color(pal.text));
            ui.add_space(4.0);
            // the queue's (and the table's) name; `shown_queue_name()` shows it translated
            let name = format!("Search: {}", self.search_query);
            self.song_table(ui, pal, &songs, &name, false);
        }
    }

    // --------------------------------------------------------- now playing ---

    fn now_playing(&mut self, ui: &mut Ui, pal: &Palette) {
        let Some(path) = self.now.as_ref().map(|n| n.path.clone()) else {
            ui.add_space(ui.available_height() * 0.35);
            ui.vertical_centered(|ui| {
                ui.label(RichText::new(tr!("Nothing is playing")).font(look::title(20.0)).color(pal.text));
                ui.label(RichText::new(tr!("Pick an album or a song, or press Play.")).color(pal.secondary));
            });
            return;
        };
        let track = self.lib.track(&path).cloned().unwrap_or_else(|| Track { path: path.clone(), ..Default::default() });
        let key = track.song().album_key();
        let paths: Vec<String> = self.lib.album_of(&path).map(|a| a.tracks.iter().map(|t| t.path.clone()).collect()).unwrap_or_else(|| vec![path.clone()]);
        let tex = self.covers.get(&key, &paths);
        let lyrics = self.lyrics_of_now();
        let area = ui.available_rect_before_wrap();
        let show_lyrics = matches!(lyrics, Some(ShownLyrics::Synced(_)) | Some(ShownLyrics::Plain(_)));
        if !show_lyrics {
            // the cover in the centre, with the title under it
            let size = (area.height() - 150.0).min(area.width() - 40.0).clamp(120.0, 480.0);
            let crect = Rect::from_center_size(pos2(area.center().x, area.top() + 20.0 + size / 2.0), vec2(size, size));
            cover(ui.painter(), crect, tex.as_ref(), &track.display_album(), 12);
            let p = ui.painter();
            p.text(pos2(area.center().x, crect.bottom() + 26.0), Align2::CENTER_CENTER, track.display_title(), look::title(20.0), pal.text);
            p.text(pos2(area.center().x, crect.bottom() + 50.0), Align2::CENTER_CENTER, artist_album(&track), look::text(14.0), pal.secondary);
            let label_y = crect.bottom() + 70.0;
            ui.scope_builder(egui::UiBuilder::new().max_rect(Rect::from_center_size(pos2(area.center().x, label_y + 22.0), vec2(320.0, 40.0))), |ui| {
                ui.vertical_centered(|ui| {
                    if matches!(lyrics, Some(ShownLyrics::Instrumental)) {
                        ui.label(RichText::new(tr!("Instrumental")).color(pal.secondary));
                    } else {
                        ui.label(RichText::new(tr!("No lyrics for this song.")).color(pal.secondary));
                        if ui.button(tr!("Get lyrics")).clicked() {
                            self.scanner.download_lyrics(vec![path.clone()]);
                        }
                    }
                });
            });
            return;
        }
        // cover on the left, lyrics on the right
        let half = area.width() / 2.0;
        let size = (half - 60.0).min(area.height() - 120.0).clamp(120.0, 480.0);
        let crect = Rect::from_center_size(pos2(area.left() + half / 2.0, area.top() + 10.0 + size / 2.0), vec2(size, size));
        cover(ui.painter(), crect, tex.as_ref(), &track.display_album(), 12);
        ui.painter().text(pos2(crect.center().x, crect.bottom() + 24.0), Align2::CENTER_CENTER, track.display_title(), look::title(18.0), pal.text);
        ui.painter().text(pos2(crect.center().x, crect.bottom() + 46.0), Align2::CENTER_CENTER, artist_album(&track), look::text(13.0), pal.secondary);
        let lrect = Rect::from_min_max(pos2(area.left() + half + 10.0, area.top()), area.max);
        let pos_ms = (self.engine.position().unwrap_or(0.0) * 1000.0) as u32;
        let mut seek_to = None;
        ui.scope_builder(egui::UiBuilder::new().max_rect(lrect), |ui| {
            if ui.rect_contains_pointer(lrect) && ui.input(|i| i.smooth_scroll_delta.y != 0.0) {
                self.lyrics_scrolled = Some(Instant::now());
            }
            let follow = self.lyrics_scrolled.is_none_or(|t| t.elapsed() > Duration::from_secs(3));
            egui::ScrollArea::vertical().id_salt("lyrics").auto_shrink([false, false]).show(ui, |ui| {
                ui.add_space(lrect.height() * 0.3);
                match &lyrics {
                    Some(ShownLyrics::Synced(lines)) => {
                        let cur = lines.iter().rposition(|(ms, _)| *ms <= pos_ms);
                        for (i, (ms, text)) in lines.iter().enumerate() {
                            let is_cur = Some(i) == cur;
                            let color = if is_cur { pal.text } else { pal.secondary.gamma_multiply(0.75) };
                            let shown = if text.trim().is_empty() { "…".to_string() } else { text.clone() };
                            let resp = ui.add(egui::Label::new(RichText::new(shown).font(look::title(24.0)).color(color)).wrap().sense(Sense::click()));
                            ui.add_space(10.0);
                            if resp.hovered() {
                                ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
                            }
                            if resp.clicked() {
                                seek_to = Some(*ms as f64 / 1000.0);
                                self.lyrics_scrolled = None;
                            }
                            if is_cur && follow && self.lyrics_line != Some(i) {
                                resp.scroll_to_me_animation(Some(Align::Center), egui::style::ScrollAnimation::duration(0.35));
                            }
                        }
                        if follow {
                            self.lyrics_line = cur;
                        }
                    }
                    Some(ShownLyrics::Plain(text)) => {
                        ui.add(egui::Label::new(RichText::new(text).font(look::title(18.0)).color(pal.text)).wrap());
                    }
                    _ => {}
                }
                ui.add_space(lrect.height() * 0.5);
            });
        });
        if let Some(s) = seek_to {
            self.seek(s);
        }
    }

    // ------------------------------------------------------------ settings ---

    fn settings_page(&mut self, ui: &mut Ui, pal: &Palette) {
        page_title(ui, pal, tr!("Settings"));
        let height = ui.available_height();
        ui.horizontal_top(|ui| {
            ui.allocate_ui_with_layout(vec2(180.0, height), Layout::top_down(Align::Min), |ui| {
                for (i, g) in GROUPS.iter().enumerate() {
                    let (rect, resp) = ui.allocate_exact_size(vec2(170.0, 30.0), Sense::click());
                    let sel = self.settings_group == i;
                    if sel {
                        ui.painter().rect_filled(rect, CornerRadius::same(6), pal.accent);
                    } else if resp.hovered() {
                        ui.painter().rect_filled(rect, CornerRadius::same(6), pal.hover);
                    }
                    let color = if sel { Color32::WHITE } else { pal.text };
                    let label = elided(ui, i18n::tr(g), look::text(13.0), color, rect.width() - 14.0);
                    ui.painter().galley(pos2(rect.left() + 10.0, rect.center().y - label.size().y / 2.0), label, color);
                    if resp.clicked() {
                        self.settings_group = i;
                    }
                }
                ui.add_space(12.0);
                if ui.button(tr!("About {app}", app = realhires::APP_NAME)).clicked() {
                    self.scanner.show_about();
                }
            });
            ui.add_space(18.0);
            ui.vertical(|ui| {
                let group = GROUPS[self.settings_group.min(GROUPS.len() - 1)];
                ui.label(RichText::new(i18n::tr(group)).font(look::title(20.0)).color(pal.text));
                ui.add_space(8.0);
                egui::ScrollArea::vertical().id_salt("settings-page").auto_shrink([false, false]).show(ui, |ui| {
                    self.settings_actions(ui, pal, group);
                    let mut draft = self.settings_draft().clone();
                    let before = draft.clone();
                    let mut error = None;
                    egui::Grid::new(("settings-grid", group)).num_columns(2).spacing([16.0, 10.0]).show(ui, |ui| {
                        for info in Settings::field_info().into_iter().filter(|i| i.group == group) {
                            if let Some(e) = crate::dialogs::setting_row(ui, &info, &mut draft, &self.scanner.choices) {
                                error = Some(e);
                            }
                        }
                    });
                    if let Some(e) = error {
                        ui.label(RichText::new(tr_text(&e)).color(pal.accent));
                    }
                    if draft != before {
                        *self.settings_draft() = draft;
                        self.settings_changed();
                    }
                    if group == "Appearance" {
                        if !self.font_used.is_empty() {
                            ui.add_space(4.0);
                            ui.label(RichText::new(tr!("Font in use: {font}.", font = self.font_used)).color(pal.secondary));
                        }
                        ui.add_space(14.0);
                        ui.label(RichText::new(tr!("Themes")).font(look::title(15.0)).color(pal.text));
                        ui.add_space(4.0);
                        self.theme_editor(ui, pal);
                    }
                    ui.add_space(16.0);
                    if ui.button(tr!("Restore the defaults of this group")).clicked() {
                        let defaults = Settings::default();
                        let draft = self.settings_draft();
                        for info in Settings::field_info().into_iter().filter(|i| i.group == group) {
                            if let Some(v) = defaults.get(info.name) {
                                let _ = draft.set(info.name, v);
                            }
                        }
                        self.settings_changed();
                    }
                });
            });
        });
    }

    /// The buttons above a settings group: what the 1.x app did from its menus.
    fn settings_actions(&mut self, ui: &mut Ui, pal: &Palette, group: &str) {
        let mut any = true;
        match group {
            "Library" => {
                ui.horizontal_wrapped(|ui| {
                    let busy = self.library_status().is_some();
                    if ui.add_enabled(!busy, egui::Button::new(tr!("Update the library now"))).clicked() {
                        self.update_library();
                    }
                    if ui
                        .button(tr!("Scan library"))
                        .on_hover_text(tr!("Check the library folders for fake hi-res and fake lossless files in the quality checker"))
                        .clicked()
                    {
                        self.check_library();
                    }
                    if ui.button(tr!("Download lyrics for the whole library…")).clicked() {
                        self.lyrics_for_library();
                    }
                    if ui.button(tr!("Open the quality checker")).clicked() {
                        self.go(Page::Quality);
                    }
                    if ui.button(tr!("Clear the play history…")).clicked() {
                        self.confirm = Some(crate::player::ConfirmAction::ClearHistory);
                    }
                });
                let n = self.lib.tracks.len();
                let text = match self.library_status() {
                    Some(s) => s,
                    None => tr!(
                        "{songs} in {albums}.",
                        songs = crate::style::count(n, n_!("1 song"), n_!("{n} songs")),
                        albums = crate::style::count(self.lib.albums.len(), n_!("1 album"), n_!("{n} albums"))
                    ),
                };
                ui.label(RichText::new(text).color(pal.secondary));
            }
            "Playback" => {
                ui.horizontal(|ui| {
                    if ui.button(tr!("Equalizer…")).clicked() {
                        self.eq_open = true;
                    }
                    if ui.button(tr!("List the output devices again")).clicked() {
                        self.fill_choices();
                    }
                });
            }
            "Lyrics" => {
                ui.horizontal_wrapped(|ui| {
                    if ui.button(tr!("Download lyrics for the whole library…")).clicked() {
                        self.lyrics_for_library();
                    }
                });
            }
            "Saved results" => {
                match &self.scanner.store {
                    Some(st) => {
                        let s = st.stats();
                        ui.label(RichText::new(tr!(
                            "{results} saved for {files} in {path} ({mb} MB, of which graphs {graphs} MB).",
                            results = crate::style::count(s.results as usize, n_!("1 result"), n_!("{n} results")),
                            files = crate::style::count(s.files as usize, n_!("1 file"), n_!("{n} files")),
                            path = s.path.display(),
                            mb = realhires::pyfmt::f(s.disk_bytes as f64 / 1e6, 1),
                            graphs = realhires::pyfmt::f(s.graph_bytes as f64 / 1e6, 1)
                        )).color(pal.secondary));
                    }
                    None => {
                        ui.label(RichText::new(tr!("Saving results is turned off, or the saved results cannot be opened.")).color(pal.secondary));
                    }
                }
                if ui.button(tr!("Delete all saved results…")).clicked() {
                    self.scanner.forget_saved_ask();
                }
            }
            "Detection" | "Scanning" | "Display" | "Fixing" => {
                ui.horizontal_wrapped(|ui| {
                    if ui.button(tr!("Open the quality checker")).clicked() {
                        self.go(Page::Quality);
                    }
                });
            }
            _ => any = false,
        }
        if any {
            ui.add_space(12.0);
        }
    }
}

impl Player {
    /// Settings → Appearance: the themes folder, and make, edit and export themes.
    fn theme_editor(&mut self, ui: &mut Ui, pal: &Palette) {
        use realhires::themes;
        if self.file_themes_read.elapsed() > std::time::Duration::from_secs(2) {
            self.reload_file_themes();
        }
        ui.ctx().request_repaint_after(std::time::Duration::from_secs(2));
        let chosen = self.settings_draft().theme.clone();
        let dark_now = ui.ctx().global_style().visuals.dark_mode;
        let all = self.all_themes();
        let current = themes::resolve(&chosen, dark_now, &all);
        let custom = self.custom_themes.iter().position(|t| t.name == chosen);
        let file = self.theme_file(&chosen).map(|p| p.to_path_buf());
        ui.label(RichText::new(tr!("Theme files (.json) put into the themes folder appear in this menu.")).color(pal.secondary));
        ui.horizontal_wrapped(|ui| {
            if ui.button(tr!("Open themes folder")).on_hover_text(themes::folder(None).to_string_lossy()).clicked() {
                let dir = themes::folder(None);
                match std::fs::create_dir_all(&dir) {
                    Ok(()) => crate::style::open_with_default_app(&dir.to_string_lossy()),
                    Err(e) => {
                        self.scanner.info =
                            Some((tr!("Themes folder").into(), tr!("The themes folder could not be made: {e}.", e = e)))
                    }
                }
            }
            if ui.button(tr!("Import theme…")).clicked() {
                if let Some(path) = rfd::FileDialog::new().add_filter(tr!("Universal Music Player theme"), &["json"]).pick_file() {
                    self.import_theme_path(&path);
                }
            }
        });
        ui.add_space(8.0);
        ui.horizontal_wrapped(|ui| {
            if ui.button(tr!("New theme…")).on_hover_text(tr!("A copy of the theme in use, to change as you like")).clicked() {
                let name = themes::copy_name(&current.name, &all);
                let _ = self.apply_name(crate::player::NameAction::NewTheme(current.name.clone()), &name);
            }
            if ui.button(tr!("Export…")).clicked() {
                if let Some(path) = rfd::FileDialog::new()
                    .add_filter(tr!("Universal Music Player theme"), &["json"])
                    .set_file_name(format!("{}.json", current.name))
                    .save_file()
                {
                    if let Err(e) = std::fs::write(&path, themes::export_theme(&current)) {
                        self.scanner.info = Some((tr!("Export theme").into(), tr!("The theme could not be saved: {e}.", e = e)));
                    }
                }
            }
            if let Some(i) = custom {
                if ui.button(tr!("Rename…")).clicked() {
                    let n = self.custom_themes[i].name.clone();
                    self.ask_name(tr!("Rename Theme"), &n.clone(), crate::player::NameAction::RenameTheme(n));
                }
                if ui.button(tr!("Delete…")).clicked() {
                    self.confirm = Some(crate::player::ConfirmAction::DeleteTheme(self.custom_themes[i].name.clone()));
                }
            }
        });
        if let Some(f) = file {
            ui.add_space(6.0);
            let name = f.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            ui.label(
                RichText::new(tr!(
                    "“{theme}” comes from the file {file} in the themes folder. Change that file, or make a copy you can \
                     change here with New theme…",
                    theme = chosen,
                    file = name
                ))
                .color(pal.secondary),
            );
            return;
        }
        ui.add_space(6.0);
        let Some(i) = custom else {
            ui.label(
                RichText::new(tr!(
                    "“{theme}” is a built-in theme and cannot be changed; “New theme…” makes a copy you can edit.",
                    theme = if chosen == themes::SYSTEM {
                        tr!("{theme} ({current})", theme = i18n::tr(&chosen), current = i18n::tr(&current.name))
                    } else {
                        i18n::tr(&chosen).to_string()
                    }
                ))
                .color(pal.secondary),
            );
            return;
        };
        let mut t = self.custom_themes[i].clone();
        let before = t.clone();
        egui::Grid::new("theme-colours").num_columns(4).spacing([14.0, 8.0]).show(ui, |ui| {
            ui.label(tr!("Dark theme"));
            ui.checkbox(&mut t.dark, "");
            ui.end_row();
            for (k, key) in themes::COLOR_KEYS.iter().enumerate() {
                ui.label(i18n::tr(themes::color_label(key)));
                let mut c = look::parse_hex(&t.colors[k]);
                if egui::color_picker::color_edit_button_srgba(ui, &mut c, egui::color_picker::Alpha::Opaque).changed() {
                    t.colors[k] = look::to_hex(c);
                }
                if k % 2 == 1 {
                    ui.end_row();
                }
            }
        });
        if t != before {
            self.custom_themes[i] = t;
            self.themes_changed();
        }
    }
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

/// "Today 14:05", "Yesterday 09:12" or "24 Nov 2026".
pub fn played_label(ts: f64) -> String {
    if ts <= 0.0 {
        return String::new();
    }
    let now = realhires::stats::local_parts(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0));
    let t = realhires::stats::local_parts(ts);
    let day = |p: &realhires::stats::LocalParts| chrono::NaiveDate::from_ymd_opt(p.year, p.month, p.day);
    let secs = ts.floor() as i64;
    let minute = chrono::DateTime::from_timestamp(secs, 0).map(|d| d.with_timezone(&chrono::Local).format("%H:%M").to_string()).unwrap_or_default();
    match (day(&now), day(&t)) {
        (Some(a), Some(b)) if a == b => tr!("Today {time}", time = minute),
        // "Yesterday" as the Python app shows it, with the time after it
        (Some(a), Some(b)) if a.pred_opt() == Some(b) => format!("{} {minute}", tr!("Yesterday")),
        _ => tr_text(&realhires::stats::day_label(&format!("{:04}-{:02}-{:02}", t.year, t.month, t.day))),
    }
}
