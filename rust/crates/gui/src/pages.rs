//! The pages of the player window (2.0, `docs/player-spec.md`): album cards,
//! the album page, song tables, artists, genres, search, Now Playing with the
//! lyrics, and Settings with every function of the 1.x app.

use std::collections::HashMap;
use std::sync::Arc;

use egui::{pos2, vec2, Align, Align2, Color32, CornerRadius, FontId, Galley, Layout, Rect, RichText, Sense, Stroke, Ui};
use realhires::i18n::{self, tr_name, tr_text};
use realhires::musiclib::{self, Album, Track};
use realhires::playerstate;
use realhires::pyunicode::casefold;
use realhires::settings::{Settings, GROUPS};
use realhires::{n_, tr};

use crate::icons::{self, Icon};
use crate::look::{self, Palette};
use crate::player::{cell_text, cover, fmt_time, NpHits, NpTab, Page, Player, ShownLyrics, SongCol};

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
    icons::paint(ui.painter(), Rect::from_center_size(pos2(rect.left() + 22.0, rect.center().y), vec2(18.0, 18.0)), icon, pal.on_accent);
    ui.painter().text(pos2(rect.left() + 36.0, rect.center().y), Align2::LEFT_CENTER, label, font, pal.on_accent);
    resp
}

/// A rounded button without the minimum width of `pill()` (2.3: the Genres page's
/// Select, Merge…, Rename… and Done): filled with the accent colour, or the LCD grey
/// when `accent` is false; greyed out and not clickable when `enabled` is false.
fn pill_button(ui: &mut Ui, pal: &Palette, icon: Option<Icon>, label: &str, accent: bool, enabled: bool) -> egui::Response {
    let font = look::title(13.0);
    let iw = if icon.is_some() { 20.0 } else { 0.0 };
    let w = ui.ctx().fonts_mut(|f| f.layout_no_wrap(label.to_string(), font.clone(), Color32::WHITE).size().x) + iw + 34.0;
    let (rect, resp) = ui.allocate_exact_size(vec2(w, 32.0), if enabled { Sense::click() } else { Sense::hover() });
    let hovered = enabled && resp.hovered();
    let disabled = pal.disabled;
    let (fill, fg) = if accent {
        (if !enabled { disabled } else if hovered { pal.accent.gamma_multiply(0.85) } else { pal.accent }, pal.on_accent)
    } else {
        (if hovered { pal.hover } else { pal.lcd }, if enabled { pal.text } else { disabled })
    };
    ui.painter().rect_filled(rect, CornerRadius::same(8), fill);
    let mut x = rect.left() + 17.0;
    if let Some(icon) = icon {
        icons::paint(ui.painter(), Rect::from_center_size(pos2(x + 7.0, rect.center().y), vec2(14.0, 14.0)), icon, fg);
        x += iw;
    }
    ui.painter().text(pos2(x, rect.center().y), Align2::LEFT_CENTER, label, font, fg);
    if enabled {
        resp.on_hover_cursor(egui::CursorIcon::PointingHand)
    } else {
        resp
    }
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

/// What a piece of a link line opens (Now Playing, the LCD).
#[derive(Clone, Debug, PartialEq)]
pub enum Link {
    Artist(String),
    Album,
}

fn text_width(ui: &Ui, text: &str, font: &FontId) -> f32 {
    ui.ctx().fonts_mut(|f| f.layout_no_wrap(text.to_string(), font.clone(), Color32::WHITE).size().x)
}

/// "Artist, Artist — Album" in pieces: each artist its own link, the album name a link when
/// `album_link`, the separators plain; cut with "…" to fit `width` (the shorter part keeps
/// its room), as the Python app's `artist_line()`.
pub fn artist_line(ui: &Ui, font: FontId, color: Color32, artists: &[String], album: &str, album_link: bool, width: f32) -> Vec<(Arc<Galley>, Option<Link>)> {
    // "Unknown Artist" / "Unknown Album" are shown in the language in use; the links keep the names
    let shown_names: Vec<String> = artists.iter().map(|a| tr_name(a).to_string()).collect();
    let album = tr_name(album);
    let sep = " — ";
    let galley = |t: &str| ui.ctx().fonts_mut(|f| f.layout_no_wrap(t.to_string(), font.clone(), color));
    let joined = shown_names.join(", ");
    let (aw, sw, bw) = (text_width(ui, &joined, &font), text_width(ui, sep, &font), text_width(ui, album, &font));
    let (a_room, b_room) = if aw + sw + bw <= width {
        (aw, bw)
    } else {
        let room = (width - sw).max(0.0);
        if aw <= room / 2.0 {
            (aw, room - aw)
        } else if bw <= room / 2.0 {
            (room - bw, bw)
        } else {
            (room / 2.0, room / 2.0)
        }
    };
    // a name with no room left is dropped with its ", ", and the name before it ends with "…"
    // ("June Harbor… — Album", never "June Harbor,  — Album"), as in the Python app
    let mut out: Vec<(Arc<Galley>, Option<Link>)> = vec![];
    let mut used = 0.0;
    let comma = text_width(ui, ", ", &font);
    for (i, (link, name)) in artists.iter().zip(&shown_names).enumerate() {
        let cw = if i > 0 { comma } else { 0.0 };
        let nw = text_width(ui, name, &font);
        if used + cw + nw <= a_room + 0.5 {
            if i > 0 {
                out.push((galley(", "), None));
            }
            out.push((galley(name), Some(Link::Artist(link.clone()))));
            used += cw + nw;
            continue;
        }
        let room = a_room - used - cw;
        if room > 12.0 {
            if i > 0 {
                out.push((galley(", "), None));
            }
            let mut g = elided(ui, name, font.clone(), color, room);
            if i + 1 < artists.len() && shown(&g) == *name {
                // egui's layout fits a name a fraction of a pixel wider than measured: it still
                // ends with "…", since more artists follow
                g = elided(ui, &format!("{name}…"), font.clone(), color, room);
            }
            out.push((g, Some(Link::Artist(link.clone()))));
        } else if i > 0 {
            let last = &shown_names[i - 1];
            let w = out.pop().map_or(0.0, |(g, _)| g.size().x) + (a_room - used);
            out.push((elided(ui, &format!("{last}…"), font.clone(), color, w + 0.5), Some(Link::Artist(artists[i - 1].clone()))));
        }
        break;
    }
    out.push((galley(sep), None));
    out.push((elided(ui, album, font, color, b_room + 0.5), album_link.then_some(Link::Album)));
    out
}

/// What a galley shows: its glyphs, with the "…" of an elided text (`Galley::text()` is the
/// whole text it was made from).
pub fn shown(g: &Galley) -> String {
    g.rows.iter().flat_map(|r| r.glyphs.iter().map(|gl| gl.chr)).collect()
}

/// Paints link pieces centred at `center` with `painter`; the links get the pointer cursor and
/// an underline on hover.  Returns the rectangles of the links and the link clicked.
pub fn paint_links(ui: &Ui, painter: &egui::Painter, pieces: Vec<(Arc<Galley>, Option<Link>)>, center: egui::Pos2, color: Color32, id: egui::Id) -> (Vec<(Rect, Link)>, Option<Link>) {
    let total: f32 = pieces.iter().map(|(g, _)| g.size().x).sum();
    let mut x = center.x - total / 2.0;
    let mut rects = vec![];
    let mut clicked = None;
    for (i, (g, link)) in pieces.into_iter().enumerate() {
        let r = Rect::from_min_size(pos2(x, center.y - g.size().y / 2.0), g.size());
        x += g.size().x;
        painter.galley(r.min, g, color);
        let Some(link) = link else { continue };
        let resp = ui.interact(r, id.with(i), Sense::click());
        if resp.hovered() {
            painter.line_segment([pos2(r.left(), r.bottom() - 1.0), pos2(r.right(), r.bottom() - 1.0)], Stroke::new(1.0, color));
            ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
        }
        if resp.clicked() {
            clicked = Some(link.clone());
        }
        rects.push((r, link));
    }
    (rects, clicked)
}

/// Details' format line: "FLAC · 24-bit/96 kHz · 2,304 kbps" (lossy: "MP3 · 44.1 kHz · 320
/// kbps"); unknown parts are left out (Python `format_line()`).
pub fn format_line(t: &Track) -> String {
    let mut parts = vec![];
    if !t.codec.is_empty() {
        parts.push(t.codec.to_uppercase());
    }
    if t.sample_rate > 0 {
        let khz = crate::eqwin::khz(t.sample_rate);
        if t.is_lossy || t.bits == 0 {
            parts.push(khz);
        } else {
            let bits = if t.is_float { tr!("{n}-bit float", n = t.bits) } else { tr!("{n}-bit", n = t.bits) };
            parts.push(format!("{bits}/{khz}"));
        }
    }
    if t.bit_rate > 0 {
        let k = (t.bit_rate as f64 / 1000.0).round_ties_even() as i64;
        let digits = k.to_string();
        let mut grouped = String::new();
        for (i, c) in digits.chars().enumerate() {
            if i > 0 && (digits.len() - i) % 3 == 0 {
                grouped.push(',');
            }
            grouped.push(c);
        }
        parts.push(format!("{grouped} kbps"));
    }
    if parts.is_empty() {
        "—".into()
    } else {
        parts.join(" · ")
    }
}

/// Now Playing's Details: (label, value) rows (Python `song_details()`). `verdict` is the
/// saved Quality check (verdict label, summary); `artists` the artists of the artist field.
/// The labels stay English (shown with `i18n::tr()`); the values are as shown.
pub fn song_details(t: &Track, album_tracks: usize, comment: &str, verdict: Option<&(String, String)>, artists: &[String]) -> Vec<(&'static str, String)> {
    let dash = |s: &str| if s.is_empty() { "—".to_string() } else { s.to_string() };
    let track_no = if t.track > 0 && album_tracks >= t.track as usize && album_tracks > 1 {
        tr!("{n} of {total}", n = t.track, total = album_tracks)
    } else if t.track > 0 {
        t.track.to_string()
    } else {
        "—".into()
    };
    let artist = if !artists.is_empty() && !t.artist.is_empty() { artists.join(", ") } else { t.artist.clone() };
    let mut rows = vec![
        (n_!("Title"), t.display_title()),
        (n_!("Artist"), dash(&artist)),
        (n_!("Album"), dash(&t.album)),
        (n_!("Album artist"), dash(&t.album_artist)),
        (n_!("Composer"), dash(&t.composer)),
        (n_!("Genre"), dash(&t.genre)),
        (n_!("Year"), dash(&t.year)),
        (n_!("Track"), track_no),
        (n_!("Disc"), if t.disc > 0 { t.disc.to_string() } else { "—".into() }),
        (n_!("Duration"), fmt_time(t.duration)),
        (n_!("Format"), format_line(t)),
        (n_!("File"), t.path.clone()),
    ];
    if !comment.trim().is_empty() {
        rows.push((n_!("Comment"), comment.trim().to_string()));
    }
    match verdict {
        None => rows.push((n_!("Quality check"), tr!("Not checked yet").into())),
        Some((v, why)) => {
            // the verdict's English label and the core's summary, in the language in use
            rows.push((n_!("Quality check"), i18n::tr(v).to_string()));
            if !why.is_empty() {
                rows.push((n_!("Why"), tr_text(why)));
            }
        }
    }
    rows
}

/// A segmented switch (Now Playing's Lyrics | Up Next | Details): a rounded panel in the LCD
/// colour, the chosen segment raised in the page colour.  Returns the segment clicked.
fn segmented<T: Copy + PartialEq>(ui: &mut Ui, pal: &Palette, options: &[(T, &str)], value: T) -> Option<T> {
    let font = look::title(13.0);
    // the labels are English, shown in the language in use
    let seg = options.iter().map(|(_, t)| text_width(ui, i18n::tr(t), &font)).fold(0.0, f32::max) + 30.0;
    let (rect, _) = ui.allocate_exact_size(vec2(seg * options.len() as f32 + 4.0, 30.0), Sense::hover());
    ui.painter().rect_filled(rect, CornerRadius::same(9), pal.lcd);
    let mut chosen = None;
    for (i, (key, text)) in options.iter().enumerate() {
        let r = Rect::from_min_size(pos2(rect.left() + 2.0 + i as f32 * seg, rect.top() + 2.0), vec2(seg, 26.0));
        let resp = ui.interact(r, ui.scope_id().with(("segment", i)), Sense::click());
        let on = *key == value;
        if on {
            ui.painter().rect_filled(r, CornerRadius::same(7), pal.bg);
        } else if resp.hovered() {
            ui.painter().rect_filled(r, CornerRadius::same(7), pal.hover);
            ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
        }
        ui.painter().text(r.center(), Align2::CENTER_CENTER, i18n::tr(text), font.clone(), if on { pal.text } else { pal.secondary });
        if resp.clicked() {
            chosen = Some(*key);
        }
    }
    chosen
}

/// Lyrics only (2.3): the lyrics this many pixels larger than the chosen size.
pub const LYRICS_WIDE_EXTRA: i64 = 4;
/// A sideways drag this long turns Now Playing's left half (2.3).
pub const SWIPE_PX: f32 = 60.0;
/// Now Playing's Details: the values' column at most this wide (2.3).
const DETAILS_VALUE_W: f32 = 420.0;

/// A filled pill with white text (the "Back to the current line" button), at `rect`.
fn floating_pill(ui: &mut Ui, pal: &Palette, rect: Rect, label: &str, id: egui::Id) -> bool {
    let resp = ui.interact(rect, id, Sense::click());
    let fill = if resp.hovered() { pal.accent.gamma_multiply(0.85) } else { pal.accent };
    ui.painter().rect_filled(rect, CornerRadius::same(16), fill);
    ui.painter().text(rect.center(), Align2::CENTER_CENTER, label, look::title(13.0), Color32::WHITE);
    if resp.hovered() {
        ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
    }
    resp.clicked()
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
            Page::Artists => self.group_grid(ui, pal, "artists"),
            Page::AlbumArtists => self.group_grid(ui, pal, "album_artists"),
            Page::Genres => self.group_grid(ui, pal, "genres"),
            Page::Years => self.group_grid(ui, pal, "years"),
            Page::Composers => self.group_grid(ui, pal, "composers"),
            Page::Group(kind, name) => self.group_item_page(ui, pal, kind, &name),
            Page::AllSongs => {
                page_title(ui, pal, tr!("All Songs"));
                // sorted once, not every frame: that made scrolling a big library lag
                let sort = Some(self.song_sort);
                let list = self.cached_list("all", sort, |p| p.sorted_songs(p.lib.tracks.iter().collect()).iter().map(|t| t.path.clone()).collect());
                ui.horizontal(|ui| {
                    if pill(ui, pal, Icon::Play, tr!("Play")).clicked() {
                        self.play_list(list.to_vec(), Some(&list[0]), "All Songs");
                    }
                    if pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                        self.shuffle_list(list.to_vec(), "All Songs");
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
                let sort = Some(self.song_sort);
                let list = self.cached_list(if liked { "liked" } else { "disliked" }, sort, |p| {
                    let songs: Vec<&Track> = if liked {
                        p.liked_songs()
                    } else {
                        p.lib.tracks.iter().filter(|t| p.state.is_disliked(&t.path)).collect()
                    };
                    p.sorted_songs(songs).iter().map(|t| t.path.clone()).collect()
                });
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
                            self.play_list(list.to_vec(), Some(&list[0]), title);
                        }
                        if liked && pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                            self.shuffle_list(list.to_vec(), title);
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
            Page::Tab(name) => self.tab_page(ui, pal, &name),
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
        let tex = self.covers.get(&key, &self.lib.album_paths[album]);
        let crect = Rect::from_min_size(rect.min, vec2(size, size));
        cover(ui.painter(), crect, tex.as_ref(), &title, 8);
        if let Some(c) = self.label_of("album", &key) {
            crate::labels::paint_cover_dot(ui.painter(), crect, c, pal); // 2.3: its colour label
        }
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
                let paths: Vec<String> = self.lib.albums[album].tracks.iter().map(|t| t.path.clone()).collect();
                self.play_list(paths, None, &title);
            } else {
                self.go(Page::Album(key));
            }
        }
        resp.context_menu(|ui| self.album_menu(ui, album));
    }

    /// The right-click menu of an album card (every album grid) and the album
    /// page's "…" menu (2.3): Play, Shuffle, Play Next, Add to Queue, Album
    /// Properties…, Acquire Metadata… (the MusicBrainz window for the album), as
    /// the album page's buttons do, and Change Cover… (the album's Cover window).
    pub fn album_menu(&mut self, ui: &mut Ui, album: usize) {
        let Some(a) = self.lib.albums.get(album) else {
            ui.close();
            return;
        };
        let title = a.title.clone();
        let paths: Vec<String> = a.tracks.iter().map(|t| t.path.clone()).collect();
        if ui.button(tr!("Play")).clicked() {
            if let Some(first) = paths.first().cloned() {
                self.play_list(paths.clone(), Some(&first), &title);
            }
            ui.close();
        }
        if ui.button(tr!("Shuffle")).clicked() {
            self.shuffle_list(paths.clone(), &title);
            ui.close();
        }
        if ui.button(tr!("Play Next")).clicked() {
            self.queue_songs(&paths, true);
            ui.close();
        }
        if ui.button(tr!("Add to Queue")).clicked() {
            self.queue_songs(&paths, false);
            ui.close();
        }
        ui.separator();
        if ui.add_enabled(self.edit_info.is_none(), egui::Button::new(tr!("Album Properties…"))).clicked() {
            let a = self.lib.albums[album].clone();
            self.edit_album_info(&a);
            ui.close();
        }
        if ui.add_enabled(self.mb_win.is_none(), egui::Button::new(tr!("Acquire Metadata…"))).clicked() {
            let a = self.lib.albums[album].clone();
            self.musicbrainz_album(&a);
            ui.close();
        }
        if ui.button(tr!("Change Cover…")).clicked() {
            if let Some(first) = paths.first() {
                self.open_cover_window(first);
            }
            ui.close();
        }
        ui.separator();
        let key = self.lib.albums[album].key.clone();
        self.label_menu(ui, "album", &key); // 2.3
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
        if self.album_sort_for() != key {
            // another album: in track order again
            self.set_album_sort_for(key);
        }
        ui.horizontal(|ui| {
            let (crect, _) = ui.allocate_exact_size(vec2(220.0, 220.0), Sense::hover());
            let tex = self.covers.get(&a.key, &self.lib.album_paths[idx]);
            cover(ui.painter(), crect, tex.as_ref(), &a.title, 10);
            ui.add_space(22.0);
            ui.vertical(|ui| {
                ui.add_space(70.0);
                ui.label(RichText::new(tr_name(&a.title)).font(look::title(26.0)).color(pal.text));
                // each artist of the field its own link to its page
                let names = {
                    let v = musiclib::split_artists(&a.artist, &self.lib.known);
                    if v.is_empty() { vec![a.artist.clone()] } else { v }
                };
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 0.0;
                    for (i, n) in names.iter().enumerate() {
                        if i > 0 {
                            ui.label(RichText::new(", ").font(look::title(18.0)).color(pal.secondary));
                        }
                        // "Unknown Artist" (an album without one) is shown translated
                        let r = ui.add(egui::Label::new(RichText::new(tr_name(n)).font(look::title(18.0)).color(pal.accent)).sense(Sense::click()));
                        let r = r.on_hover_cursor(egui::CursorIcon::PointingHand);
                        if r.hovered() {
                            ui.painter().line_segment([r.rect.left_bottom(), r.rect.right_bottom()], Stroke::new(1.0, pal.accent));
                        }
                        if r.clicked() {
                            self.open_artist(n);
                        }
                    }
                });
                let refs: Vec<&Track> = a.tracks.iter().collect();
                let meta: Vec<String> = [a.genre.clone(), a.year.clone(), minutes(&refs)].into_iter().filter(|s| !s.is_empty()).collect();
                ui.label(RichText::new(meta.join(" · ")).color(pal.secondary));
                ui.add_space(14.0);
                // wraps on a narrow page instead of running past its edge (2.3)
                ui.horizontal_wrapped(|ui| {
                    if pill(ui, pal, Icon::Play, tr!("Play")).clicked() {
                        self.play_list(paths.clone(), Some(&paths[0]), &a.title);
                    }
                    if pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                        self.shuffle_list(paths.clone(), &a.title);
                    }
                    ui.add_space(6.0);
                    if ui
                        .add_enabled(self.edit_info.is_none(), egui::Button::new(tr!("Album Properties…")))
                        .on_hover_text(tr!("Change the album, album artist, genre, release date… of all its songs"))
                        .clicked()
                    {
                        self.edit_album_info(&a);
                    }
                    if ui
                        .add_enabled(self.mb_win.is_none(), egui::Button::new(tr!("Acquire Metadata…")))
                        .clicked()
                    {
                        self.musicbrainz_album(&a);
                    }
                    // 2.3: the album's menu (as an album card's right-click menu, with Change Cover…)
                    let r = icons::button(ui, Icon::More, 28.0, false, true, tr!("More"));
                    egui::Popup::menu(&r).show(|ui| self.album_menu(ui, idx));
                });
            });
        });
        ui.add_space(18.0);
        // the columns the user chose for album pages (# first by default), in track order
        // until a heading is clicked
        let sort = self.album_sort;
        let list = self.cached_list(&format!("album:{key}"), sort, |_| {
            crate::player::sort_tracks(a.tracks.iter().collect(), sort).iter().map(|t| t.path.clone()).collect()
        });
        self.song_table_kind(ui, pal, &list, &a.title, "album", true);
    }

    /// The right-click menu of a song.
    pub fn song_menu(&mut self, ui: &mut Ui, path: &str) {
        if ui.button(tr!("Play Next")).clicked() {
            self.queue_songs(&[path.to_string()], true);
            ui.close();
        }
        if ui.button(tr!("Add to Queue")).clicked() {
            self.queue_songs(&[path.to_string()], false);
            ui.close();
        }
        ui.separator();
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
        self.label_menu(ui, "song", path); // 2.3
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
            self.get_lyrics(vec![path.to_string()]);
            ui.close();
        }
        if ui.add_enabled(self.edit_info.is_none(), egui::Button::new(tr!("Properties…"))).clicked() {
            self.edit_song_info(path);
            ui.close();
        }
        // song tables select one song at a time, so this is always the right-clicked song
        if ui.add_enabled(self.mb_win.is_none(), egui::Button::new(tr!("Acquire Metadata…"))).clicked() {
            self.musicbrainz_song(path);
            ui.close();
        }
        // 2.3: the Cover window of the song's album (it was "Cover…")
        if ui.button(tr!("Change Cover…")).clicked() {
            self.open_cover_window(path);
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

    /// A table of songs (Songs, Liked Songs, a genre, search results) with the columns
    /// the user chose for song lists.
    pub fn song_table(&mut self, ui: &mut Ui, pal: &Palette, list: &[String], name: &str, sortable: bool) {
        self.song_table_kind(ui, pal, list, name, "songs", sortable);
    }

    /// A table with the columns the user chose for this kind of list ("songs",
    /// "folders", "album"); a right click on its headings chooses them.
    pub fn song_table_kind(&mut self, ui: &mut Ui, pal: &Palette, list: &[String], name: &str, kind: &'static str, sortable: bool) {
        let cols: Vec<SongCol> = self.state.columns_of(kind).iter().filter_map(|c| SongCol::from_name(c)).collect();
        self.table(ui, pal, list, name, cols, Some(kind), sortable, None);
    }

    /// Recently Played: its own columns, with "Played" instead of Genre.
    pub fn song_table_ex(
        &mut self,
        ui: &mut Ui,
        pal: &Palette,
        list: &[String],
        name: &str,
        sortable: bool,
        played: Option<&HashMap<String, f64>>,
    ) {
        let cols = vec![SongCol::Like, SongCol::Title, SongCol::Artist, SongCol::Album, SongCol::Played, SongCol::Time, SongCol::Quality];
        self.table(ui, pal, list, name, cols, None, sortable && played.is_none(), played);
    }

    /// The heading's menu: a check item per column (Title stays) and Reset columns.
    fn columns_menu(&mut self, ui: &mut Ui, kind: &'static str) {
        let shown = self.state.columns_of(kind);
        let mut changed = false;
        for name in realhires::playerstate::SONG_COLUMNS {
            let Some(col) = SongCol::from_name(name) else { continue };
            let mut on = shown.iter().any(|c| c == name);
            let r = ui.add_enabled(name != "title", egui::Checkbox::new(&mut on, i18n::tr(col.info().1)));
            if r.changed() {
                self.state.toggle_column(kind, name);
                changed = true;
            }
        }
        ui.separator();
        if ui.button(tr!("Reset columns")).clicked() {
            self.state.reset_columns(kind);
            changed = true;
            ui.close();
        }
        if changed {
            self.save_state();
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn table(
        &mut self,
        ui: &mut Ui,
        pal: &Palette,
        list: &[String],
        name: &str,
        cols: Vec<SongCol>,
        kind: Option<&'static str>,
        sortable: bool,
        played: Option<&HashMap<String, f64>>,
    ) {
        let w = ui.available_width();
        let heart = cols.contains(&SongCol::Like);
        let heart_w = if heart { 30.0 } else { 6.0 };
        let shown: Vec<SongCol> = cols.iter().copied().filter(|c| *c != SongCol::Like).collect();
        let total: f32 = shown.iter().map(|c| c.info().2).sum::<f32>().max(0.1);
        let usable = w - heart_w;
        let mut xs = vec![];
        let mut ws = vec![];
        let mut x = heart_w;
        for c in &shown {
            xs.push(x);
            let cw = usable * c.info().2 / total;
            ws.push(cw);
            x += cw;
        }
        let sort = kind.map(|k| self.sort_of(k)).unwrap_or(Some(self.song_sort));
        // header
        let (hrect, hresp) = ui.allocate_exact_size(vec2(w, 26.0), Sense::click());
        let mut menu_open = false;
        for (i, col) in shown.iter().enumerate() {
            let r = Rect::from_min_size(pos2(hrect.left() + xs[i], hrect.top()), vec2(ws[i], 26.0));
            let can_sort = sortable && *col != SongCol::Played;
            let resp = ui.interact(r, ui.id().with(("song-head", name, i)), if can_sort { Sense::click() } else { Sense::hover() });
            let mut text = i18n::tr(col.info().0).to_string();
            if can_sort && sort.is_some_and(|(c, _)| c == *col) {
                text += if sort.is_some_and(|(_, rev)| rev) { "  ▼" } else { "  ▲" };
            }
            let color = if resp.hovered() && can_sort { pal.text } else { pal.secondary };
            let right = col.info().3;
            let (px, align) = if right { (r.right() - 6.0, Align2::RIGHT_CENTER) } else { (r.left() + 4.0, Align2::LEFT_CENTER) };
            ui.painter().text(pos2(px, r.center().y), align, text, look::title(11.5), color);
            if resp.clicked() && can_sort {
                match kind {
                    Some(k) => self.set_sort(k, *col),
                    None => {
                        self.song_sort = if self.song_sort.0 == *col { (*col, !self.song_sort.1) } else { (*col, false) };
                    }
                }
            }
            if let Some(k) = kind {
                resp.context_menu(|ui| {
                    menu_open = true;
                    self.columns_menu(ui, k);
                });
            }
        }
        if let Some(k) = kind {
            hresp.context_menu(|ui| {
                menu_open = true;
                self.columns_menu(ui, k);
            });
        }
        let _ = menu_open;
        ui.painter().line_segment([hrect.left_bottom(), hrect.right_bottom()], Stroke::new(0.5, pal.separator));
        let now = self.now.as_ref().map(|n| n.path.clone());
        let mut play = None;
        let mut like = None;
        let qcol = shown.iter().position(|c| *c == SongCol::Quality);
        egui::ScrollArea::vertical().auto_shrink([false, false]).id_salt(("songs", name)).show_rows(ui, 30.0, list.len(), |ui, range| {
            for i in range {
                let path = &list[i];
                let Some(t) = self.lib.track(path) else {
                    // a playlist song whose file is gone
                    let (rect, resp) = ui.allocate_exact_size(vec2(w, 30.0), Sense::click());
                    let p = ui.painter_at(rect);
                    let g = elided(ui, &realhires::paths::file_name(path), look::text(13.0), pal.secondary.gamma_multiply(0.7), usable * 0.5);
                    p.galley(pos2(rect.left() + heart_w + 4.0, rect.center().y - g.size().y / 2.0), g, pal.secondary);
                    p.text(pos2(rect.left() + heart_w + usable * 0.55, rect.center().y), Align2::LEFT_CENTER, tr!("Not in the library"), look::text(12.0), pal.secondary.gamma_multiply(0.7));
                    let p2 = path.clone();
                    resp.context_menu(|ui| self.song_menu(ui, &p2));
                    continue;
                };
                let (rect, resp) = ui.allocate_exact_size(vec2(w, 30.0), Sense::click());
                let selected = self.selected_song.as_deref() == Some(path.as_str());
                let playing = now.as_deref() == Some(path.as_str());
                if selected {
                    ui.painter().rect_filled(rect, CornerRadius::same(5), pal.selection);
                } else if i % 2 == 1 {
                    ui.painter().rect_filled(rect, CornerRadius::same(5), pal.zebra);
                }
                let p = ui.painter_at(rect);
                let heart_rect = Rect::from_center_size(pos2(rect.left() + 15.0, rect.center().y), vec2(16.0, 16.0));
                let liked = self.state.is_liked(path);
                let label = self.label_of("song", path); // 2.3: a dot before the title
                if heart && (liked || resp.hovered()) {
                    icons::paint(&p, heart_rect, if liked { Icon::HeartFilled } else { Icon::Heart }, pal.accent);
                }
                for (k, col) in shown.iter().enumerate() {
                    let v = match col {
                        SongCol::Played => played_label(played.and_then(|m| m.get(path).copied()).unwrap_or(0.0)),
                        SongCol::Num if playing => {
                            icons::paint(&p, Rect::from_center_size(pos2(rect.left() + xs[k] + ws[k] - 12.0, rect.center().y), vec2(15.0, 15.0)), Icon::Speaker, pal.accent);
                            continue;
                        }
                        c => cell_text(t, *c),
                    };
                    let first = *col == SongCol::Title;
                    let color = if first && playing {
                        pal.accent
                    } else if first {
                        pal.text
                    } else if *col == SongCol::Quality {
                        pal.quality_of(t) // 2.3: the Quality column in the quality's colour
                    } else {
                        pal.secondary
                    };
                    let font = if first { look::text(13.0) } else { look::text(12.5) };
                    let (mut left, mut room) = (rect.left() + xs[k] + 4.0, ws[k] - 10.0);
                    if let (true, Some(c)) = (first, label) {
                        crate::labels::paint_dot(&p, pos2(left + crate::labels::DOT / 2.0, rect.center().y), c, None);
                        left += crate::labels::DOT + 6.0;
                        room -= crate::labels::DOT + 6.0;
                    }
                    let g = elided(ui, &v, font, color, room.max(10.0));
                    let x = if col.info().3 { rect.left() + xs[k] + ws[k] - 6.0 - g.size().x } else { left };
                    p.galley(pos2(x, rect.center().y - g.size().y / 2.0), g, color);
                }
                let mut resp = resp;
                if let Some(q) = qcol {
                    let cell = Rect::from_min_size(pos2(rect.left() + xs[q], rect.top()), vec2(ws[q], rect.height()));
                    if ui.rect_contains_pointer(cell) {
                        // the Quality cell's tooltip: the resolution ("24-bit/96 kHz", "MP3 · 320 kbps · 44.1 kHz")
                        resp = resp.on_hover_text_at_pointer(tr_text(&musiclib::quality_detail(t)));
                    }
                }
                if resp.clicked() {
                    if heart && resp.interact_pointer_pos().is_some_and(|q| heart_rect.expand(5.0).contains(q)) {
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

    // ------------------------------------------------------ grouping pages ---

    /// Artists, Album Artists, Genres, Years and Composers: a card each, like the albums
    /// (a mosaic of up to four of its album covers, the name, "N albums · M songs"); a
    /// click opens its page.  On Genres (2.3) the title row has Select: in select mode a
    /// click selects a card, and the row shows "N selected", Merge… (two or more),
    /// Rename… (one) and Done; Ctrl-click (Cmd-click on macOS) selects without it.
    fn group_grid(&mut self, ui: &mut Ui, pal: &Palette, kind: &'static str) {
        let (_, title, noun) = crate::player::GROUPINGS.iter().copied().find(|g| g.0 == kind).unwrap_or(crate::player::GROUPINGS[0]);
        let n = self.lib.groups(kind).len();
        let multi = kind == "genres";
        let selected = if multi { self.group_selected.len() } else { 0 };
        let selecting = multi && (self.group_select_mode || selected > 0);
        ui.horizontal(|ui| {
            ui.vertical(|ui| {
                ui.label(RichText::new(i18n::tr(title)).font(look::title(28.0)).color(pal.text));
                let mut sub = crate::player::plural(n, noun);
                if multi && n > 0 {
                    sub += " · ";
                    sub += &if self.group_select_mode {
                        tr!("Click genres to select them").to_string()
                    } else {
                        tr!("Press Select or {mod}-click genres to merge or rename them", mod = crate::player::MOD_NAME)
                    };
                }
                ui.label(RichText::new(sub).color(pal.secondary));
            });
            if selecting || (multi && n > 0) {
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if selecting {
                        if pill_button(ui, pal, None, tr!("Done"), true, true).clicked() {
                            self.done_selecting();
                        }
                        if pill_button(ui, pal, None, tr!("Rename…"), false, selected == 1).clicked() {
                            self.rename_selected();
                        }
                        if pill_button(ui, pal, None, tr!("Merge…"), false, selected >= 2).clicked() {
                            self.merge_selected();
                        }
                        ui.add_space(4.0);
                        ui.label(RichText::new(tr!("{n} selected", n = selected)).color(pal.secondary));
                    } else if pill_button(ui, pal, Some(Icon::Check), tr!("Select"), false, true).clicked() {
                        self.group_select_mode = true;
                    }
                });
            }
        });
        ui.add_space(12.0);
        let gap = 22.0;
        let width = ui.available_width();
        let cols = ((width + gap) / (180.0 + gap)).floor().max(1.0) as usize;
        let size = ((width + gap) / cols as f32 - gap).min(240.0);
        let rows = n.div_ceil(cols);
        egui::ScrollArea::vertical().auto_shrink([false, false]).id_salt(("groups", kind)).show_rows(ui, size + 44.0 + gap, rows, |ui, range| {
            for r in range {
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = gap;
                    for i in (r * cols)..((r + 1) * cols).min(n) {
                        self.group_card(ui, pal, kind, i, size);
                    }
                });
                ui.add_space(gap - ui.spacing().item_spacing.y);
            }
        });
    }

    /// Up to four album covers, 2 x 2 (fewer repeat), or the one cover there is.
    fn group_mosaic(&mut self, p: &egui::Painter, rect: Rect, albums: &[usize], name: &str) {
        let shown: Vec<usize> = albums.iter().take(4).copied().collect();
        if shown.len() <= 1 {
            let tex = shown.first().and_then(|&a| self.covers.get(&self.lib.albums[a].key, &self.lib.album_paths[a]));
            cover(p, rect, tex.as_ref(), name, 8);
            return;
        }
        let half = rect.width() / 2.0;
        for q in 0..4 {
            let a = shown[q % shown.len()];
            let r = Rect::from_min_size(rect.min + vec2((q % 2) as f32 * half, (q / 2) as f32 * half), vec2(half, half));
            let tex = self.covers.get(&self.lib.albums[a].key, &self.lib.album_paths[a]);
            let radius = CornerRadius {
                nw: if q == 0 { 8 } else { 0 },
                ne: if q == 1 { 8 } else { 0 },
                sw: if q == 2 { 8 } else { 0 },
                se: if q == 3 { 8 } else { 0 },
            };
            match tex {
                Some(t) => {
                    p.add(egui::epaint::RectShape::filled(r, radius, Color32::WHITE).with_texture(t.id(), Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0))));
                }
                None => {
                    p.rect_filled(r, radius, look::current(p.ctx()).placeholder);
                }
            }
        }
    }

    fn group_card(&mut self, ui: &mut Ui, pal: &Palette, kind: &'static str, i: usize, size: f32) {
        let (rect, resp) = ui.allocate_exact_size(vec2(size, size + 44.0), Sense::click());
        if !ui.is_rect_visible(rect) {
            return;
        }
        let name = self.lib.groups(kind)[i].name.clone();
        let songs = self.lib.groups(kind)[i].tracks.len();
        let albums = self.lib.group_albums.get(kind).and_then(|v| v.get(i)).cloned().unwrap_or_default();
        let crect = Rect::from_min_size(rect.min, vec2(size, size));
        self.group_mosaic(ui.painter(), crect, &albums, &name);
        if let Some(c) = label_kind(kind).and_then(|k| self.label_of(k, &name)) {
            crate::labels::paint_cover_dot(ui.painter(), crect, c, pal); // 2.3: its colour label
        }
        let is_sel = kind == "genres" && self.group_selected.iter().any(|s| *s == name);
        if is_sel {
            ui.painter().rect_stroke(crect.expand(3.0), CornerRadius::same(10), Stroke::new(3.0, pal.accent), egui::StrokeKind::Inside);
            let c = crect.right_top() + vec2(-20.0, 20.0);
            ui.painter().circle_filled(c, 13.0, pal.accent);
            icons::paint(ui.painter(), Rect::from_center_size(c, vec2(16.0, 16.0)), Icon::Check, Color32::WHITE);
        } else if resp.hovered() {
            ui.painter().rect_filled(crect, CornerRadius::same(8), Color32::from_black_alpha(30));
        }
        // "Unknown Genre" and the like in the language in use
        let g = elided(ui, tr_name(&name), look::title(13.0), pal.text, size);
        ui.painter().galley(pos2(rect.left(), crect.bottom() + 6.0), g, pal.text);
        let sub = format!("{} · {}", crate::player::plural(albums.len(), "album"), crate::player::plural(songs, "song"));
        let g = elided(ui, &sub, look::text(12.0), pal.secondary, size);
        ui.painter().galley(pos2(rect.left(), crect.bottom() + 24.0), g, pal.secondary);
        let resp = resp.on_hover_cursor(egui::CursorIcon::PointingHand);
        // macOS: Ctrl-click is a right-click there (Cmd-click selects)
        let mac_menu = cfg!(target_os = "macos") && resp.clicked() && ui.input(|i| i.modifiers.ctrl && !i.modifiers.mac_cmd);
        if resp.clicked() && !mac_menu {
            // select mode, or Ctrl-click (Cmd-click on macOS): select or unselect the card
            let toggle = kind == "genres" && (self.group_select_mode || ui.input(|i| i.modifiers.command));
            if toggle {
                if is_sel {
                    self.group_selected.retain(|s| *s != name);
                } else {
                    self.group_selected.push(name.clone());
                }
            } else {
                self.group_selected.clear();
                self.go(Page::Group(kind, name.clone()));
            }
        }
        let menu = egui::Popup::context_menu(&resp);
        let menu = if mac_menu { menu.open_memory(Some(egui::SetOpenCommand::Bool(true))) } else { menu };
        menu.show(|ui| {
            if is_sel && self.group_selected.len() > 1 {
                let k = self.group_selected.len();
                if ui.button(tr!("Merge {n} genres…", n = k)).clicked() {
                    let names = self.group_selected.clone();
                    self.ask_merge_groups(kind, names);
                    ui.close();
                }
                if ui.button(tr!("Clear selection")).clicked() {
                    self.group_selected.clear();
                    ui.close();
                }
            } else {
                self.group_menu(ui, kind, &name, false);
            }
        });
    }

    /// The menu of a genre, year, composer or artist: Open, Play, Shuffle, and for genres,
    /// composers and years Rename… (genres also Merge into…).
    pub fn group_menu(&mut self, ui: &mut Ui, kind: &'static str, name: &str, here: bool) {
        let songs = |p: &Player| -> Vec<String> {
            p.lib.find_group(kind, name).map(|i| p.lib.groups(kind)[i].tracks.iter().map(|t| t.path.clone()).collect()).unwrap_or_default()
        };
        if !here && ui.button(tr!("Open")).clicked() {
            self.go(Page::Group(kind, name.to_string()));
            ui.close();
        }
        if ui.button(tr!("Play")).clicked() {
            let list = songs(self);
            if !list.is_empty() {
                self.play_list(list.clone(), Some(&list[0]), name);
            }
            ui.close();
        }
        if ui.button(tr!("Shuffle")).clicked() {
            let list = songs(self);
            if !list.is_empty() {
                self.shuffle_list(list, name);
            }
            ui.close();
        }
        if matches!(kind, "genres" | "composers" | "years") {
            ui.separator();
            let menu = crate::player::rename_texts(kind).menu;
            if ui.add_enabled(self.tag_job.is_none(), egui::Button::new(i18n::tr(menu))).clicked() {
                self.ask_rename_group(kind, name);
                ui.close();
            }
            if kind == "genres" && ui.add_enabled(self.tag_job.is_none(), egui::Button::new(tr!("Merge into…"))).clicked() {
                self.ask_merge_groups(kind, vec![name.to_string()]);
                ui.close();
            }
        }
        if let Some(k) = label_kind(kind) {
            ui.separator();
            self.label_menu(ui, k, name); // 2.3
        }
    }

    /// One artist, album artist, genre, year or composer: its albums (artists) or songs,
    /// Play, Shuffle and "…" (its menu).
    fn group_item_page(&mut self, ui: &mut Ui, pal: &Palette, kind: &'static str, name: &str) {
        if back_link(ui, pal, tr!("Back")).clicked() {
            self.go_back();
            return;
        }
        // the word before the page's counts, and the page of a group with no songs left
        // (the Python app's `GROUP_TEXTS`)
        let (word, gone) = match kind {
            "artists" => (n_!("Artist"), ""),
            "album_artists" => (n_!("Album artist"), ""),
            "genres" => (n_!("Genre"), n_!("No songs have this genre any more.")),
            "years" => (n_!("Year"), n_!("No songs have this year any more.")),
            _ => (n_!("Composer"), n_!("No songs have this composer any more.")),
        };
        let Some(gi) = self.lib.find_group(kind, name) else {
            page_title(ui, pal, tr_name(name));
            ui.label(RichText::new(i18n::tr(gone)).color(pal.secondary));
            return;
        };
        let gname = self.lib.groups(kind)[gi].name.clone();
        let albums = self.lib.group_albums.get(kind).and_then(|v| v.get(gi)).cloned().unwrap_or_default();
        let songs = self.lib.groups(kind)[gi].tracks.len();
        let cards = matches!(kind, "artists" | "album_artists");
        let id = format!("group:{kind}:{gname}");
        let sort = Some(self.song_sort);
        let list = self.cached_list(&id, if cards { None } else { sort }, |p| {
            let g = &p.lib.groups(kind)[gi];
            if cards {
                // album order, as the albums are shown
                let mut v: Vec<&Track> = g.tracks.iter().collect();
                v.sort_by_cached_key(|t| (p.lib.album_index.get(&t.song().album_key()).copied().unwrap_or(usize::MAX), t.disc, t.track));
                v.iter().map(|t| t.path.clone()).collect()
            } else {
                crate::player::sort_tracks(g.tracks.iter().collect(), sort).iter().map(|t| t.path.clone()).collect()
            }
        });
        ui.horizontal(|ui| {
            ui.vertical(|ui| {
                ui.label(RichText::new(tr_name(&gname)).font(look::title(28.0)).color(pal.text));
                let word = i18n::tr(word);
                let sub = if cards {
                    let counts = tr!(
                        "{a}, {b}",
                        a = crate::player::plural(albums.len(), "album"),
                        b = crate::player::plural(songs, "song")
                    );
                    format!("{word} · {counts}")
                } else {
                    format!("{word} · {}", crate::player::plural(songs, "song"))
                };
                ui.label(RichText::new(sub).color(pal.secondary));
            });
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if matches!(kind, "genres" | "composers" | "years") {
                    ui.menu_button("…", |ui| self.group_menu(ui, kind, &gname, true));
                }
                if pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                    self.shuffle_list(list.to_vec(), &gname);
                }
                if pill(ui, pal, Icon::Play, tr!("Play")).clicked() && !list.is_empty() {
                    self.play_list(list.to_vec(), Some(&list[0]), &gname);
                }
            });
        });
        ui.add_space(12.0);
        if cards {
            self.album_grid(ui, pal, &albums);
        } else {
            self.song_table(ui, pal, &list, &gname, true);
        }
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

    /// Now Playing (2.1): the cover with the title and "Artist — Album" as one block in the
    /// middle of the left half (the album name opens the album, each artist name that artist,
    /// the cover the Cover window); on the right a switch Lyrics | Up Next | Details.  With
    /// the lyrics hidden (the Lyrics button on this page) only the block, in the middle.
    /// 2.3: a back link at the top left; an instrumental song's Lyrics is the block in the
    /// middle with "Instrumental"; Lyrics only fills the page with the lyrics; a sideways
    /// drag or scroll turns the left half to the album's songs (`np_left()`).
    fn now_playing(&mut self, ui: &mut Ui, pal: &Palette) {
        use crate::player::NpMode;
        self.np_hits = NpHits::default();
        let area = ui.available_rect_before_wrap();
        // the way back (2.3): "‹ All Songs" at the top left, when there is a page to go back to
        if let Some(label) = self.back_label() {
            let font = look::title(14.0);
            let galley = ui.painter().layout_no_wrap(label.clone(), font, pal.accent);
            let rect = Rect::from_min_size(area.left_top(), vec2(24.0 + galley.size().x + 6.0, 28.0));
            let resp = ui.interact(rect, ui.scope_id().with("np-back"), Sense::click());
            icons::paint(ui.painter(), Rect::from_center_size(pos2(rect.left() + 9.0, rect.center().y), vec2(18.0, 18.0)), Icon::Back, pal.accent);
            let text_pos = pos2(rect.left() + 24.0, rect.center().y - galley.size().y / 2.0);
            let text_w = galley.size().x;
            ui.painter().galley(text_pos, galley, pal.accent);
            if resp.hovered() {
                ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
                let y = rect.center().y + 9.0;
                ui.painter().line_segment([pos2(text_pos.x, y), pos2(text_pos.x + text_w, y)], Stroke::new(1.0, pal.accent));
            }
            self.np_hits.back_page = Some((rect, label));
            if resp.clicked() {
                self.go_back();
                return;
            }
        }
        let Some(path) = self.now.as_ref().map(|n| n.path.clone()) else {
            ui.add_space(ui.available_height() * 0.35);
            ui.vertical_centered(|ui| {
                ui.label(RichText::new(tr!("Nothing is playing")).font(look::title(20.0)).color(pal.text));
                ui.label(RichText::new(tr!("Pick an album or a song, or press Play.")).color(pal.secondary));
            });
            return;
        };
        let track = self.lib.track(&path).cloned().unwrap_or_else(|| Track { path: path.clone(), ..Default::default() });
        let lyrics = self.lyrics_of_now();
        let mode = self.np_mode();
        let left = match mode {
            NpMode::Wide => None,
            NpMode::Halves => Some(Rect::from_min_max(area.min, pos2(area.center().x, area.bottom()))),
            _ => Some(area),
        };
        if mode == NpMode::Instrumental {
            // the switch stays above the block, so Up Next and Details are at hand
            let top = Rect::from_min_max(area.min, pos2(area.right(), area.top() + 40.0));
            ui.scope_builder(egui::UiBuilder::new().max_rect(top), |ui| {
                ui.vertical_centered(|ui| {
                    ui.add_space(4.0);
                    if let Some(tab) = segmented(ui, pal, &NpTab::ALL, self.np_tab) {
                        self.set_np_tab(tab);
                    }
                });
            });
        }
        if let Some(left) = left {
            if self.np_left(ui, pal, left, &path, &track, mode == NpMode::Instrumental) {
                return; // it opened another page
            }
        }
        if !matches!(mode, NpMode::Halves | NpMode::Wide) {
            return; // the lyrics are hidden (the Lyrics button), or the song is instrumental: the block only
        }
        let rrect = if mode == NpMode::Wide { area } else { Rect::from_min_max(pos2(area.center().x + 10.0, area.top()), area.max) };
        ui.scope_builder(egui::UiBuilder::new().max_rect(rrect), |ui| {
            ui.vertical_centered(|ui| {
                ui.add_space(4.0);
                if let Some(tab) = segmented(ui, pal, &NpTab::ALL, self.np_tab) {
                    self.set_np_tab(tab);
                }
                ui.add_space(8.0);
            });
            match self.np_tab {
                NpTab::Lyrics => self.np_lyrics(ui, pal, &path, lyrics, mode == NpMode::Wide),
                NpTab::UpNext => self.np_up_next(ui, pal),
                NpTab::Details => self.np_details(ui, pal, &track),
            }
        });
    }

    /// Now Playing's left half (or the whole page when the block is in the middle): the
    /// cover block, or the album's songs (`np_songs`, 2.3), with two dots under it that switch
    /// between them; a sideways drag of more than `SWIPE_PX` (either way) or a sideways scroll
    /// turns it.  Returns true when a link opened another page.
    fn np_left(&mut self, ui: &mut Ui, pal: &Palette, left: Rect, path: &str, track: &Track, instrumental: bool) -> bool {
        let album = self.lib.album_of(path).cloned();
        if album.is_some() {
            // the swipe: a drag that started here, or a sideways scroll over it
            let (origin, latest, down, hover, dx) = ui.input(|i| {
                (i.pointer.press_origin(), i.pointer.latest_pos(), i.pointer.primary_down(), i.pointer.hover_pos(), i.smooth_scroll_delta.x)
            });
            if !down {
                self.np_swiped = false;
            } else if let (Some(o), Some(p)) = (origin, latest) {
                let d = p - o;
                if left.contains(o) && !self.np_swiped && d.x.abs() > SWIPE_PX && d.x.abs() > d.y.abs() {
                    self.np_swiped = true; // one turn per drag
                    self.np_songs = !self.np_songs;
                }
            }
            if hover.is_some_and(|h| left.contains(h)) && dx != 0.0 {
                // the content moving left (a swipe to the left) shows the songs, to the right the cover
                self.np_hscroll += dx;
                if self.np_hscroll.abs() > 30.0 {
                    self.set_np_side(self.np_hscroll < 0.0);
                    self.np_hscroll = 0.0;
                }
            } else {
                self.np_hscroll = 0.0;
            }
        }
        let opened = match &album {
            Some(a) if self.np_songs => {
                // below the switch when it stands above the block
                let list = if instrumental { Rect::from_min_max(left.min + vec2(0.0, 40.0), left.max) } else { left };
                self.np_album_songs(ui, pal, list, path, a);
                false
            }
            _ => self.np_block(ui, pal, left, path, track, album.is_some(), instrumental),
        };
        if opened {
            return true;
        }
        if album.is_some() {
            // the dots: the cover, the album's songs
            let y = left.bottom() - 14.0;
            for (k, songs) in [false, true].into_iter().enumerate() {
                let c = pos2(left.center().x + (k as f32 - 0.5) * 18.0, y);
                let r = Rect::from_center_size(c, vec2(18.0, 20.0));
                let resp = ui.interact(r, ui.scope_id().with(("np-dot", k)), Sense::click());
                let on = self.np_songs == songs;
                ui.painter().circle_filled(c, 4.0, if on { pal.text } else { pal.secondary.gamma_multiply(0.45) });
                if resp.hovered() {
                    ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
                }
                if resp.clicked() {
                    self.set_np_side(songs);
                }
                self.np_hits.dots.push(r);
            }
        }
        false
    }

    /// The cover with the title and "Artist — Album" under it (and "Instrumental"), one
    /// block in the middle of `left`.  Returns true when a link opened another page.
    #[allow(clippy::too_many_arguments)]
    fn np_block(&mut self, ui: &mut Ui, pal: &Palette, left: Rect, path: &str, track: &Track, has_album: bool, instrumental: bool) -> bool {
        let key = track.song().album_key();
        let paths: Vec<String> = self.lib.album_of(path).map(|a| a.tracks.iter().map(|t| t.path.clone()).collect()).unwrap_or_else(|| vec![path.to_string()]);
        let tex = self.covers.get_large(&key, &paths);
        // the block: the cover, the title and "Artist — Album" (80 px, and "Instrumental"), in the middle of `left`
        let text_h = if instrumental { 106.0 } else { 80.0 };
        let top = if instrumental { 44.0 } else { 0.0 }; // below the switch
        let room = Rect::from_min_max(pos2(left.left(), left.top() + top), left.max);
        let size = (room.width() - 80.0).min(room.height() - 80.0 - text_h).clamp(120.0, 480.0);
        let crect = Rect::from_min_size(pos2(room.center().x - size / 2.0, room.center().y - (size + text_h) / 2.0), vec2(size, size));
        cover(ui.painter(), crect, tex.as_ref(), &track.display_album(), 12);
        let cresp = ui.interact(crect, ui.scope_id().with("np-cover"), Sense::click());
        if cresp.hovered() {
            ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
        }
        self.np_hits.cover = Some(crect);
        if cresp.clicked() {
            // a click without a drag (a drag is not a click): the Cover window
            self.open_cover_window(path);
        }
        let title = elided(ui, &track.display_title(), look::title(20.0), pal.text, left.width() - 60.0);
        ui.painter().galley(pos2(left.center().x - title.size().x / 2.0, crect.bottom() + 30.0 - title.size().y / 2.0), title, pal.text);
        let artists = self.lib.artists_of(track);
        let pieces = artist_line(ui, look::text(14.0), pal.secondary, &artists, &track.display_album(), has_album, left.width() - 60.0);
        let (rects, clicked) = paint_links(ui, ui.painter(), pieces, pos2(left.center().x, crect.bottom() + 58.0), pal.secondary, ui.scope_id().with("np-links"));
        self.np_hits.links = rects;
        if instrumental {
            let r = ui.painter().text(pos2(left.center().x, crect.bottom() + 86.0), Align2::CENTER_CENTER, tr!("Instrumental"), look::text(14.0), pal.secondary);
            self.np_hits.instrumental = Some(r);
        }
        match clicked {
            Some(Link::Artist(name)) => {
                self.open_artist(&name);
                true
            }
            Some(Link::Album) => {
                self.open_album_of(path);
                true
            }
            None => false,
        }
    }

    /// The album's songs in track order (2.3): number (the speaker for the song playing, whose
    /// title is bold), title, time; a double-click plays one with the album as the list.
    fn np_album_songs(&mut self, ui: &mut Ui, pal: &Palette, left: Rect, path: &str, album: &Album) {
        let width = (left.width() - 80.0).min(560.0);
        let x0 = left.center().x - width / 2.0;
        let title = elided(ui, tr_name(&album.title), look::title(18.0), pal.text, width - 20.0);
        ui.painter().galley(pos2(x0 + 10.0, left.top() + 36.0), title, pal.text);
        let list = Rect::from_min_max(pos2(x0, left.top() + 78.0), pos2(x0 + width, left.bottom() - 36.0));
        let new_album = self.np_songs_album != album.key;
        self.np_songs_album = album.key.clone();
        let mut play = None;
        ui.scope_builder(egui::UiBuilder::new().max_rect(list), |ui| {
            egui::ScrollArea::vertical().id_salt(("np-album-songs", &album.key)).auto_shrink([false, false]).show(ui, |ui| {
                ui.spacing_mut().item_spacing.y = 0.0;
                for t in &album.tracks {
                    let (r, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 36.0), Sense::click());
                    let playing = t.path == path;
                    if resp.hovered() {
                        ui.painter().rect_filled(r.shrink2(vec2(0.0, 1.0)), CornerRadius::same(6), pal.row_hover);
                    }
                    let mid = r.center().y;
                    if playing {
                        icons::paint(ui.painter(), Rect::from_center_size(pos2(r.left() + 22.0, mid), vec2(16.0, 16.0)), Icon::Speaker, pal.accent);
                        if new_album {
                            resp.scroll_to_me(None);
                        }
                    } else if t.track > 0 {
                        ui.painter().text(pos2(r.left() + 30.0, mid), Align2::RIGHT_CENTER, t.track.to_string(), look::text(13.0), pal.secondary);
                    }
                    let when = if t.duration > 0.0 { fmt_time(t.duration) } else { String::new() };
                    let time_w = text_width(ui, &when, &look::text(13.0));
                    ui.painter().text(pos2(r.right() - 12.0, mid), Align2::RIGHT_CENTER, &when, look::text(13.0), pal.secondary);
                    let font = if playing { look::title(13.5) } else { look::text(13.5) };
                    let name = elided(ui, &t.display_title(), font, pal.text, r.width() - 44.0 - time_w - 28.0);
                    ui.painter().galley(pos2(r.left() + 44.0, mid - name.size().y / 2.0), name, pal.text);
                    if resp.double_clicked() {
                        play = Some(t.path.clone());
                    }
                    self.np_hits.album_songs.push((r, t.path.clone(), playing));
                }
            });
        });
        if let Some(p) = play {
            let paths: Vec<String> = album.tracks.iter().map(|t| t.path.clone()).collect();
            self.play_list(paths, Some(&p), &album.title);
        }
    }

    /// Now Playing's Lyrics: the label, A− / A+ and Lyrics only, then the lyrics (synced ones
    /// follow the song unless the user scrolled the current line away; then "Back to the
    /// current line").  `wide` (Lyrics only, 2.3): centred across the page, 4 px larger.
    fn np_lyrics(&mut self, ui: &mut Ui, pal: &Palette, path: &str, lyrics: Option<ShownLyrics>, wide: bool) {
        let Some(lyrics) = lyrics else {
            ui.add_space(ui.available_height() * 0.3);
            ui.vertical_centered(|ui| {
                ui.label(RichText::new(tr!("No lyrics for this song.")).color(pal.secondary));
                ui.add_space(8.0);
                let r = pill(ui, pal, Icon::Lyrics, tr!("Get lyrics"));
                self.np_hits.get_lyrics = Some(r.rect);
                if r.clicked() {
                    self.get_lyrics(vec![path.to_string()]);
                }
            });
            return;
        };
        let (label, source) = self.lyrics_label_of_now();
        let size = self.state.lyrics_size.clamp(playerstate::LYRICS_SIZE_MIN, playerstate::LYRICS_SIZE_MAX);
        ui.horizontal(|ui| {
            // `lyrics::shown_label()`'s English texts, in the language in use
            ui.label(RichText::new(i18n::tr(&label)).font(look::title(13.0)).color(pal.text));
            ui.label(RichText::new(i18n::tr(&source)).font(look::text(11.5)).color(pal.secondary));
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.add_space(12.0);
                // Lyrics only (2.3): the cover goes and the lyrics fill the page; on while they do
                let expand = icons::button(ui, Icon::Expand, 26.0, self.lyrics_wide, true, tr!("Lyrics only"));
                self.np_hits.wide_button = Some(expand.rect);
                if expand.clicked() {
                    self.toggle_lyrics_wide();
                }
                ui.add_space(4.0);
                let small = |t: &str| egui::Button::new(RichText::new(t).font(look::title(13.0))).fill(pal.lcd).corner_radius(8).min_size(vec2(44.0, 26.0));
                let bigger = ui.add_enabled(size < playerstate::LYRICS_SIZE_MAX, small("A+")).on_hover_text(tr!("Larger lyrics"));
                let smaller = ui.add_enabled(size > playerstate::LYRICS_SIZE_MIN, small("A−")).on_hover_text(tr!("Smaller lyrics"));
                self.np_hits.size_buttons = vec![smaller.rect, bigger.rect];
                if bigger.clicked() {
                    self.lyrics_size_step(1);
                }
                if smaller.clicked() {
                    self.lyrics_size_step(-1);
                }
            });
        });
        ui.add_space(4.0);
        let lrect = ui.available_rect_before_wrap();
        self.np_hits.lyrics_rect = Some(lrect);
        let extra = if wide { LYRICS_WIDE_EXTRA } else { 0 };
        let font = look::title((size + extra) as f32);
        let plain_font = look::title((playerstate::plain_lyrics_size(size) + extra) as f32);
        let halign = if wide { Align::Center } else { Align::Min };
        let pos_ms = (self.engine.position().unwrap_or(0.0) * 1000.0) as u32;
        let mut seek_to = None;
        let user_scrolled = ui.rect_contains_pointer(lrect) && ui.input(|i| i.smooth_scroll_delta.y != 0.0);
        let back = std::mem::take(&mut self.lyrics_back);
        let mut cur_in_view = None;
        let mut cur_line = None;
        egui::ScrollArea::vertical().id_salt("lyrics").auto_shrink([false, false]).show(ui, |ui| {
            // Lyrics only: each line centred on the page
            ui.with_layout(Layout::top_down(halign), |ui| {
                ui.add_space(if matches!(lyrics, ShownLyrics::Synced(_)) { lrect.height() * 0.3 } else { 6.0 });
                match &lyrics {
                    ShownLyrics::Synced(lines) => {
                        let cur = lines.iter().rposition(|(ms, _)| *ms <= pos_ms);
                        cur_line = cur;
                        let seen = cur.unwrap_or(0); // before the first line: the first one
                        for (i, (ms, text)) in lines.iter().enumerate() {
                            let is_cur = Some(i) == cur;
                            let color = if is_cur { pal.text } else { pal.secondary.gamma_multiply(0.75) };
                            let shown = if text.trim().is_empty() { "…".to_string() } else { text.clone() };
                            let resp = ui.add(egui::Label::new(RichText::new(shown).font(font.clone()).color(color)).wrap().sense(Sense::click()));
                            ui.add_space(10.0);
                            if resp.hovered() {
                                ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
                            }
                            if resp.clicked() {
                                seek_to = Some(*ms as f64 / 1000.0);
                            }
                            if i == seen {
                                cur_in_view = Some(ui.clip_rect().contains_rect(resp.rect));
                                if back {
                                    resp.scroll_to_me_animation(Some(Align::Center), egui::style::ScrollAnimation::none());
                                }
                            }
                            if is_cur && !back && !self.lyrics_away && self.lyrics_line != Some(i) {
                                resp.scroll_to_me_animation(Some(Align::Center), egui::style::ScrollAnimation::duration(0.35));
                            }
                        }
                    }
                    ShownLyrics::Plain(text) => {
                        ui.add(egui::Label::new(RichText::new(text).font(plain_font.clone()).color(pal.text)).wrap());
                    }
                    ShownLyrics::Instrumental => {
                        ui.label(RichText::new(tr!("This song is instrumental.")).font(plain_font.clone()).color(pal.secondary));
                    }
                }
                ui.add_space(lrect.height() * 0.5);
            });
        });
        if matches!(lyrics, ShownLyrics::Synced(_)) {
            if seek_to.is_some() || back {
                self.lyrics_away = false; // follow the line clicked, or the current one again
                if seek_to.is_some() {
                    self.lyrics_line = None;
                }
            } else if user_scrolled || self.lyrics_check {
                // the user scrolls: following stops while the current line is out of view (checked
                // once more after the last scroll, since a scroll shows a frame later)
                if let Some(v) = cur_in_view {
                    self.lyrics_away = !v;
                }
                self.lyrics_check = user_scrolled;
                ui.ctx().request_repaint();
            } else if self.lyrics_away && cur_in_view == Some(true) {
                self.lyrics_away = false; // the current line came back into view
            }
            if !self.lyrics_away && seek_to.is_none() {
                self.lyrics_line = cur_line;
            }
            if self.lyrics_away {
                let label = tr!("Back to the current line");
                let w = text_width(ui, label, &look::title(13.0)) + 36.0;
                let r = Rect::from_center_size(pos2(lrect.center().x, lrect.bottom() - 34.0), vec2(w, 32.0));
                self.np_hits.back = Some(r);
                if floating_pill(ui, pal, r, label, ui.scope_id().with("lyrics-back")) {
                    self.lyrics_back = true;
                    self.lyrics_away = false;
                    ui.ctx().request_repaint();
                }
            }
        }
        if let Some(s) = seek_to {
            self.seek(s);
        }
    }

    /// Now Playing's Up Next: the queue, as in the Up Next panel (double-click plays a song).
    fn np_up_next(&mut self, ui: &mut Ui, pal: &Palette) {
        ui.horizontal(|ui| {
            ui.add_space(20.0);
            if !self.queue_name.is_empty() {
                let from = tr!("Playing from {name}", name = crate::player::shown_queue_name(&self.queue_name));
                ui.label(RichText::new(from).color(pal.secondary));
            }
        });
        ui.add_space(6.0);
        let upcoming = self.queue.as_ref().map(|q| q.up_next(50)).unwrap_or_default();
        self.np_hits.up_next = upcoming.clone();
        if upcoming.is_empty() {
            ui.horizontal(|ui| {
                ui.add_space(20.0);
                ui.label(RichText::new(tr!("Nothing comes after this song.")).color(pal.secondary));
            });
            return;
        }
        // 2.3: dragged to another place, or removed with a right click (crate::upnext)
        let rows: Vec<(String, String, Option<Color32>)> = upcoming
            .iter()
            .map(|path| {
                let t = self.lib.track(path);
                let title = t.map(|t| t.display_title()).unwrap_or_else(|| path.clone());
                (title, t.map(|t| tr_name(&t.display_artist()).to_string()).unwrap_or_default(), self.label_of("song", path))
            })
            .collect();
        let action = egui::ScrollArea::vertical()
            .id_salt("np-up-next")
            .auto_shrink([false, false])
            .show(ui, |ui| {
                crate::upnext::rows(ui, pal, "now-playing", rows.len(), 44.0, 12.0, |p, rect, i| {
                    let (title, artist, label) = &rows[i];
                    let dx = if let Some(c) = label {
                        crate::labels::paint_dot(p, rect.left_top() + vec2(20.0 + crate::labels::DOT / 2.0, 13.0), *c, None);
                        crate::labels::DOT + 6.0
                    } else {
                        0.0
                    };
                    p.text(rect.left_top() + vec2(20.0 + dx, 5.0), Align2::LEFT_TOP, title, look::title(13.0), pal.text);
                    p.text(rect.left_top() + vec2(20.0, 24.0), Align2::LEFT_TOP, artist, look::text(11.5), pal.secondary);
                })
            })
            .inner;
        self.up_next_action(action);
    }

    /// Now Playing's Details: the song's description, as one block centred in the right half
    /// (2.3; a long value such as the file's path wraps within it, and a block taller than the
    /// page starts at the top and scrolls); its artists are links.
    fn np_details(&mut self, ui: &mut Ui, pal: &Palette, track: &Track) {
        let (comment, verdict) = self.details_extra(&track.path);
        let n_album = self.lib.album_of(&track.path).map(|a| a.tracks.len()).unwrap_or(0);
        let artists = if track.artist.is_empty() { vec![] } else { musiclib::split_artists(&track.artist, &self.lib.known) };
        let rows = song_details(track, n_album, &comment, verdict.as_ref(), &artists);
        // the labels are English (`song_details()`), shown in the language in use
        self.np_hits.details = rows.iter().map(|(k, v)| format!("{}: {v}", i18n::tr(k))).collect();
        let mut open = None;
        let label_w = rows.iter().map(|(k, _)| text_width(ui, i18n::tr(k), &look::text(13.0))).fold(0.0, f32::max) + 18.0;
        let view_h = ui.available_height();
        self.np_hits.details_view = Some(ui.available_rect_before_wrap());
        // the block's height in the last frame (it is known once drawn), to centre it
        let height_id = ui.scope_id().with("np-details-height");
        let last_h: f32 = ui.ctx().data(|d| d.get_temp(height_id)).unwrap_or(0.0);
        let mut block = Rect::NOTHING;
        egui::ScrollArea::vertical().id_salt("np-details").auto_shrink([false, false]).show(ui, |ui| {
            // the values' column: as wide as the longest value, at most DETAILS_VALUE_W (a long path wraps)
            let widest = rows.iter().flat_map(|(_, v)| v.lines()).map(|l| text_width(ui, l, &look::text(13.0))).fold(80.0, f32::max);
            let value_w = widest.min(DETAILS_VALUE_W).min(ui.available_width() - label_w - 44.0).max(80.0);
            let indent = ((ui.available_width() - label_w - ui.spacing().item_spacing.x - value_w) / 2.0).max(20.0);
            ui.add_space(if last_h > 0.0 && last_h + 24.0 < view_h { (view_h - last_h) / 2.0 } else { 4.0 });
            let top = ui.cursor().top();
            for (i, (k, v)) in rows.iter().enumerate() {
                if i > 0 {
                    ui.add_space(7.0);
                }
                let row = ui.horizontal_top(|ui| {
                    ui.add_space(indent);
                    ui.vertical(|ui| {
                        ui.set_width(label_w);
                        ui.label(RichText::new(i18n::tr(k)).font(look::text(13.0)).color(pal.secondary));
                    });
                    ui.vertical(|ui| {
                        ui.set_width(value_w);
                        if *k == "Artist" && !artists.is_empty() {
                            ui.horizontal_wrapped(|ui| {
                                ui.spacing_mut().item_spacing.x = 0.0;
                                for (i, name) in artists.iter().enumerate() {
                                    if i > 0 {
                                        ui.label(RichText::new(", ").font(look::text(13.0)).color(pal.text));
                                    }
                                    let r = ui.add(egui::Link::new(RichText::new(name).font(look::text(13.0)).color(pal.text)));
                                    self.np_hits.detail_links.push((r.rect, name.clone()));
                                    if r.clicked() {
                                        open = Some(name.clone());
                                    }
                                }
                            });
                        } else {
                            ui.add(egui::Label::new(RichText::new(v).font(look::text(13.0)).color(pal.text)).wrap());
                        }
                    });
                });
                block = block.union(Rect::from_min_max(pos2(row.response.rect.left() + indent, top), row.response.rect.max));
            }
            ui.add_space(8.0);
        });
        if (block.height() - last_h).abs() > 0.5 && block.is_positive() {
            ui.ctx().data_mut(|d| d.insert_temp(height_id, block.height()));
            ui.ctx().request_repaint(); // centred in the next frame
        }
        self.np_hits.details_block = block.is_positive().then_some(block);
        if let Some(name) = open {
            self.open_artist(&name);
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
                        ui.painter().rect_filled(rect, CornerRadius::same(6), pal.row_hover);
                    }
                    let color = if sel { pal.on_accent } else { pal.text };
                    let label = elided(ui, i18n::tr(g), look::text(13.0), color, rect.width() - 14.0);
                    ui.painter().galley(pos2(rect.left() + 10.0, rect.center().y - label.size().y / 2.0), label, color);
                    if resp.clicked() {
                        self.settings_group = i;
                    }
                }
                ui.add_space(12.0);
                // under the group list: the About box (2.1: there is no Help menu)
                if crate::style::flat_button(ui, &tr!("About {app}", app = realhires::APP_NAME), true).clicked() {
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
                    if ui
                        .add_enabled(!self.musicbrainz_busy(), egui::Button::new(tr!("Look up the library on MusicBrainz now")))
                        .clicked()
                    {
                        self.lookup_library(true);
                    }
                    if ui.button(tr!("Open the quality checker")).clicked() {
                        self.go(Page::Quality);
                    }
                    if ui.button(tr!("Clear the play history…")).clicked() {
                        self.confirm = Some(crate::player::ConfirmAction::ClearHistory);
                    }
                    // 2.3: custom tabs
                    if ui.button(tr!("Import tab…")).clicked() {
                        self.import_tab_dialog();
                    }
                    if ui
                        .button(tr!("Open tabs folder"))
                        .on_hover_text(realhires::smarttabs::tabs_folder().to_string_lossy())
                        .clicked()
                    {
                        self.open_tabs_folder();
                    }
                });
                ui.label(
                    RichText::new(tr!("Tab files (.json) put into the tabs folder appear in the sidebar and on the Library page."))
                        .color(pal.secondary),
                );
                let problems = self.tab_problems_text();
                if !problems.is_empty() {
                    ui.label(RichText::new(problems).color(crate::style::error_color(pal)));
                }
                let n = self.lib.tracks.len();
                let text = match self.status_line() {
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
        // 2.3: every other colour the app draws; unset, a colour follows the eight above
        // (its button shows the derived colour) until it is changed, and Reset makes it
        // follow them again
        ui.add_space(14.0);
        ui.label(RichText::new(tr!("More colours")).font(look::title(13.0)).color(pal.text));
        ui.label(
            RichText::new(tr!(
                "Unless the theme sets them, these follow the colours above. Reset makes a colour follow them again."
            ))
            .color(pal.secondary),
        );
        ui.add_space(4.0);
        // two colours a row where the page is wide enough, else one (the 1024-pixel screens)
        let per_row = if ui.available_width() >= 660.0 { 2 } else { 1 };
        egui::Grid::new("theme-more-colours").num_columns(3 * per_row).spacing([10.0, 8.0]).show(ui, |ui| {
            for (k, key) in themes::EXTRA_COLOR_KEYS.iter().enumerate() {
                ui.label(i18n::tr(themes::extra_color_label(key)));
                let set = t.extra_color(key).is_some();
                let mut c = look::parse_hex(&t.any_color(key));
                let r = egui::color_picker::color_edit_button_srgba(ui, &mut c, egui::color_picker::Alpha::Opaque);
                if r.changed() {
                    t.set_extra(key, Some(&look::to_hex(c)));
                }
                if set {
                    if ui.small_button(tr!("Reset")).clicked() {
                        t.set_extra(key, None);
                    }
                } else {
                    ui.label(RichText::new(tr!("automatic")).small().color(pal.secondary));
                }
                if k % per_row == per_row - 1 {
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

/// The colour-label kind of a grouping page's cards (2.3: `playerstate::LABEL_KINDS`).
pub fn label_kind(kind: &str) -> Option<&'static str> {
    Some(match kind {
        "artists" => "artist",
        "album_artists" => "album_artist",
        "genres" => "genre",
        "years" => "year",
        "composers" => "composer",
        _ => return None,
    })
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

#[cfg(test)]
mod tests {
    use super::*;

    /// The LCD's "Artist, Artist — Album" at every width, as the Python app's
    /// `test_artist_line_cuts_names_without_a_dangling_comma`: each name a link, cut with "…",
    /// and a name with no room left goes with its ", " ("June Harbor,  — Northern Lig…" before).
    #[test]
    fn artist_line_cuts_names_without_a_dangling_comma() {
        let ctx = egui::Context::default();
        let names = vec!["June Harbor".to_string(), "Aurora Fields".to_string()];
        let mut seen = std::collections::BTreeSet::new();
        let input = egui::RawInput { screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(800.0, 600.0))), ..Default::default() };
        ctx.run_ui(input, |ui| {
            let font = FontId::proportional(12.0);
            let mut width = 400.0;
            while width > 40.0 {
                let pieces = artist_line(ui, font.clone(), Color32::WHITE, &names, "Northern Lights", true, width);
                let line: String = pieces.iter().map(|(g, _)| shown(g)).collect();
                let links: Vec<String> = pieces.iter().filter_map(|(_, l)| match l { Some(Link::Artist(n)) => Some(n.clone()), _ => None }).collect();
                assert_eq!(links, names[..links.len()].to_vec(), "{width}: {line}");
                assert!(!line.contains(",  —") && !line.starts_with(", "), "{width}: {line}");
                assert_eq!(pieces.last().map(|p| p.1.clone()), Some(Some(Link::Album)), "{width}: {line}");
                if links.len() == 1 {
                    assert!(shown(&pieces[0].0).ends_with('…'), "{width}: {line}"); // more artists follow
                }
                if width >= 300.0 {
                    assert_eq!(line, "June Harbor, Aurora Fields — Northern Lights");
                }
                seen.insert(links.len());
                width -= 3.0;
            }
        })
        .drop_without_applying_deltas();
        assert!(seen.contains(&1) && seen.contains(&2), "{seen:?}");
    }

}
