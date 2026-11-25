//! The pages added to the player after its first version (2.0,
//! `docs/player-spec.md` "Additions"): the Library start page, Folders,
//! Playlists, Recently Played and Your Stats with its details.

use std::collections::HashMap;

use egui::{pos2, vec2, Align, Align2, Color32, CornerRadius, Layout, Rect, RichText, Sense, Stroke, Ui};
use realhires::i18n::{self, tr_name, tr_text};
use realhires::musiclib::{self, Track};
use realhires::paths::{abspath, file_name};
use realhires::settings::LIBRARY_LAYOUTS;
use realhires::stats::{self, PERIODS};
use realhires::{n_, tr};

use crate::icons::{self, Icon};
use crate::leaderboard::Link;
use crate::look::{self, Palette};
use crate::pages::elided;
use crate::player::{count, cover, NameAction, Page, Player};

/// An entry of the Library page (2.3: tiles or a list): its icon, name and count
/// in the language in use, and the page it opens.
#[derive(Clone, Debug)]
pub struct LibraryEntry {
    pub icon: Icon,
    pub name: String,
    pub count: String,
    pub page: Page,
}

/// The height of a row of the Library page as a list (2.3).
pub const LIST_ROW_H: f32 = 44.0;

/// The id of the Library page's tiles or list button (2.3).
pub fn layout_button_id(layout: &str) -> egui::Id {
    egui::Id::unique(("library-layout", layout))
}

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
    icons::paint(ui.painter(), Rect::from_center_size(pos2(rect.left() + 22.0, rect.center().y), vec2(18.0, 18.0)), icon, pal.on_accent);
    ui.painter().text(pos2(rect.left() + 36.0, rect.center().y), Align2::LEFT_CENTER, label, font, pal.on_accent);
    resp
}

pub(crate) fn back_link(ui: &mut Ui, pal: &Palette, label: &str) -> egui::Response {
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

/// The calendar at the top of Your Stats (2.1): the month shown and the day
/// chosen (kept for the session), with the month's minutes per day and the
/// day's plays, read again when the month, the day or the history changes.
pub struct Calendar {
    pub month: (i32, u32),
    pub day: Option<(i32, u32, u32)>,
    /// (month, day, history generation) of what was read
    loaded: Option<((i32, u32), Option<(i32, u32, u32)>, u64)>,
    pub minutes: Vec<f64>,
    pub levels: Vec<u8>,
    pub rows: Vec<stats::DayPlay>,
}

impl Default for Calendar {
    fn default() -> Self {
        Calendar { month: this_month(), day: None, loaded: None, minutes: vec![], levels: vec![], rows: vec![] }
    }
}

fn now_parts() -> stats::LocalParts {
    stats::local_parts(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0))
}

fn this_month() -> (i32, u32) {
    let t = now_parts();
    (t.year, t.month)
}

/// The room the day's plays need to the right of the calendar, and the gap
/// between them (2.3); with less room they go under it.
pub const CALENDAR_SIDE_MIN: f32 = 360.0;
pub const CALENDAR_SIDE_GAP: f32 = 24.0;

/// The width of a day of the calendar on a page `width` wide.
pub fn calendar_cell_width(width: f32) -> f32 {
    (width / 7.0).clamp(30.0, 62.0)
}

/// Whether the day's plays go to the right of the calendar (2.3).
pub fn calendar_side_by_side(width: f32) -> bool {
    width >= 7.0 * calendar_cell_width(width) + CALENDAR_SIDE_MIN
}

/// The links of a card of Your Stats (2.4): its big text, the song it names (for the
/// song menu), and its detail line (an artist).
#[derive(Clone, Debug, Default)]
struct CardLinks {
    big: Option<Link>,
    song: Option<String>,
    detail: Option<Link>,
}

/// The share of the accent colour for `stats::month_levels()` 0..4.
const TINTS: [f32; 5] = [0.0, 0.25, 0.45, 0.7, 1.0];

/// A chevron pointing left or right.
fn chevron(p: &egui::Painter, c: egui::Pos2, s: f32, right: bool, color: Color32) {
    let (h, d) = (s * 0.3, if right { -1.0 } else { 1.0 });
    let stroke = Stroke::new((s * 0.1).max(1.5), color);
    p.add(egui::Shape::line(
        vec![pos2(c.x + d * h * 0.3, c.y - h * 0.7), pos2(c.x - d * h * 0.4, c.y), pos2(c.x + d * h * 0.3, c.y + h * 0.7)],
        stroke,
    ));
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
    /// Read the calendar's month (and the chosen day) from the play history when
    /// it changed; the plays come between local midnights.
    pub fn load_calendar(&mut self) {
        let cal = &mut self.calendar;
        let key = (cal.month, cal.day, self.history_gen);
        if cal.loaded == Some(key) {
            return;
        }
        let (y, m) = cal.month;
        let plays = |(since, until): (f64, f64)| self.history.as_ref().map(|h| h.plays(since, Some(until))).unwrap_or_default();
        cal.minutes = stats::month_minutes(&plays(stats::month_bounds(y, m)), y, m, &stats::local_parts);
        cal.levels = stats::month_levels(&cal.minutes);
        cal.rows = match cal.day {
            Some((y, m, d)) => stats::day_plays(&plays(stats::day_bounds(y, m, d)), y, m, d, &stats::local_parts),
            None => vec![],
        };
        cal.loaded = Some(key);
    }

    /// Show the month `delta` months after the one shown.
    pub fn calendar_step(&mut self, delta: i32) {
        let (y, m) = self.calendar.month;
        self.calendar.month = stats::shift_month(y, m, delta);
    }

    /// List the plays of this day of the month shown; the chosen day again hides the list.
    pub fn calendar_choose(&mut self, day: u32) {
        let (y, m) = self.calendar.month;
        self.calendar.day = if self.calendar.day == Some((y, m, day)) { None } else { Some((y, m, day)) };
    }

    /// A click on a song of the day's list: play the day's songs (each once) from it.
    pub fn calendar_play(&mut self, i: usize) {
        let Some(row) = self.calendar.rows.get(i) else { return };
        let start = row.path.clone();
        let mut list: Vec<String> = vec![];
        for r in &self.calendar.rows {
            if !list.contains(&r.path) {
                list.push(r.path.clone());
            }
        }
        self.play_list(list, Some(&start), "Your Stats");
    }

    /// The calendar, then the chosen day's plays (or the hint): to the right of the
    /// calendar, as tall as it and scrolling inside it, when the page has room for the
    /// calendar and `CALENDAR_SIDE_MIN` more (2.3); under it otherwise.
    fn calendar_ui(&mut self, ui: &mut Ui, pal: &Palette) {
        let width = ui.available_width();
        let cw = calendar_cell_width(width);
        if !calendar_side_by_side(width) {
            self.calendar_grid(ui, pal, cw);
            ui.add_space(12.0);
            self.calendar_day_ui(ui, pal);
            return;
        }
        let height = self.calendar_height();
        ui.horizontal_top(|ui| {
            ui.spacing_mut().item_spacing.x = 0.0;
            self.calendar_grid(ui, pal, cw);
            ui.add_space(CALENDAR_SIDE_GAP);
            let rest = ui.available_width();
            ui.allocate_ui_with_layout(vec2(rest, height), Layout::top_down(Align::Min), |ui| {
                ui.set_min_size(vec2(rest, height));
                egui::ScrollArea::vertical().id_salt("calendar-day").max_height(height).auto_shrink([false, true]).show(ui, |ui| {
                    ui.spacing_mut().item_spacing.x = 8.0;
                    self.calendar_day_ui(ui, pal);
                });
            });
        });
        ui.add_space(12.0);
    }

    /// The calendar's height: the month's title, the weekdays and the weeks.
    fn calendar_height(&self) -> f32 {
        let (year, month) = self.calendar.month;
        64.0 + 44.0 * stats::month_grid(year, month).len() as f32
    }

    fn calendar_grid(&mut self, ui: &mut Ui, pal: &Palette, cw: f32) {
        self.load_calendar();
        let (year, month) = self.calendar.month;
        let weeks = stats::month_grid(year, month);
        let ch = 44.0;
        let (rect, _) = ui.allocate_exact_size(vec2(cw * 7.0, 64.0 + ch * weeks.len() as f32), Sense::hover());
        let p = ui.painter().clone();
        let (x0, top) = (rect.left(), rect.top());
        // the core's month name, in the language in use
        let title = p.text(pos2(x0, top + 16.0), Align2::LEFT_CENTER, tr_text(&stats::month_title(year, month)), look::title(18.0), pal.text);
        let total = (self.calendar.minutes.iter().sum::<f64>() + 0.5) as usize;
        if total > 0 {
            p.text(pos2(title.right() + 10.0, top + 17.0), Align2::LEFT_CENTER, crate::player::plural(total, "minute"), look::text(13.0), pal.secondary);
        }
        let newest = (year, month) >= this_month();
        let mut step = 0;
        for (cx, delta, on) in [(rect.right() - 50.0, -1, true), (rect.right() - 16.0, 1, !newest)] {
            let r = Rect::from_center_size(pos2(cx, top + 16.0), vec2(30.0, 30.0));
            let resp = ui.interact(r, ui.scope_id().with(("calendar-month", delta)), if on { Sense::click() } else { Sense::hover() });
            if on && resp.hovered() {
                p.rect_filled(r, CornerRadius::same(8), pal.hover);
            }
            chevron(&p, r.center(), 18.0, delta > 0, if on { pal.accent } else { pal.disabled });
            if on && resp.on_hover_text(if delta < 0 { tr!("Previous month") } else { tr!("Next month") }).clicked() {
                step = delta;
            }
        }
        let mut y = top + 42.0;
        for (k, name) in WEEKDAYS_SHORT.iter().enumerate() {
            p.text(pos2(x0 + k as f32 * cw + cw / 2.0, y + 8.0), Align2::CENTER_CENTER, i18n::tr(name), look::title(11.0), pal.secondary);
        }
        y += 22.0;
        let now = now_parts();
        let today = (now.year, now.month, now.day);
        let mut chosen_day = None;
        for week in &weeks {
            for (k, &d) in week.iter().enumerate() {
                if d == 0 {
                    continue;
                }
                let cell = Rect::from_min_max(pos2(x0 + k as f32 * cw + 2.0, y + 2.0), pos2(x0 + (k + 1) as f32 * cw - 2.0, y + ch - 2.0));
                let resp = ui.interact(cell, ui.scope_id().with(("calendar-day", year, month, d)), Sense::click());
                let level = self.calendar.levels.get(d as usize - 1).copied().unwrap_or(0);
                // 2.3: the tint is the theme's "Your Stats: days with plays" colour
                let mut fill = look::mix(pal.lcd, pal.calendar, TINTS[level as usize]);
                if resp.hovered() {
                    fill = look::mix(fill, pal.text, 0.1);
                }
                let chosen = self.calendar.day == Some((year, month, d));
                let is_today = today == (year, month, d);
                p.rect_filled(cell, CornerRadius::same(8), fill);
                if chosen || is_today {
                    let stroke = if chosen { Stroke::new(3.0, pal.text) } else { Stroke::new(2.0, pal.accent) };
                    p.rect_stroke(cell, CornerRadius::same(8), stroke, egui::StrokeKind::Inside);
                }
                let color = if level >= 3 { pal.on_accent } else { pal.text };
                let font = if chosen || is_today { look::title(13.0) } else { look::text(13.0) };
                p.text(cell.center(), Align2::CENTER_CENTER, d.to_string(), font, color);
                if resp.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                    chosen_day = Some(d);
                }
            }
            y += ch;
        }
        if step != 0 {
            self.calendar_step(step);
        }
        if let Some(d) = chosen_day {
            self.calendar_choose(d);
        }
    }

    fn calendar_day_ui(&mut self, ui: &mut Ui, pal: &Palette) {
        self.load_calendar();
        let Some((y, m, d)) = self.calendar.day else {
            ui.add_space(2.0);
            ui.label(RichText::new(tr!("Click a day to see the songs you played on it.")).color(pal.secondary));
            ui.add_space(14.0);
            return;
        };
        // the core's texts, in the language in use
        ui.label(RichText::new(tr_text(&stats::day_title(y, m, d))).font(look::title(17.0)).color(pal.text));
        ui.label(RichText::new(tr_text(&stats::day_summary(&self.calendar.rows))).color(pal.secondary));
        ui.add_space(8.0);
        let width = ui.available_width();
        let rest = width - 70.0 - 90.0;
        let (tw, row_h) = (rest * 0.55, 30.0);
        let aw = rest - tw - 16.0;
        let now = self.now.as_ref().map(|n| n.path.clone());
        let mut play = None;
        let mut follow = None;
        let rows = self.calendar.rows.clone();
        // 2.4: a line naming the place of the plays under it, when the day's plays have places
        let with_places = rows.iter().any(|r| !r.place.is_empty());
        let mut place: Option<String> = None;
        let mut heads = vec![];
        for (i, r) in rows.iter().enumerate() {
            if with_places && place.as_deref() != Some(r.place.as_str()) {
                place = Some(r.place.clone());
                crate::places::day_place_line(ui, pal, &r.place, width);
                heads.push(r.place.clone());
            }
            let (rect, _) = ui.allocate_exact_size(vec2(width, row_h), Sense::hover());
            let playable = self.lib.by_path.contains_key(&r.path);
            let resp = ui.interact(rect, ui.scope_id().with(("calendar-play", i)), if playable { Sense::click() } else { Sense::hover() });
            let p = ui.painter_at(rect);
            if playable && resp.hovered() {
                p.rect_filled(rect, CornerRadius::same(6), pal.row_hover);
            } else if i % 2 == 1 {
                p.rect_filled(rect, CornerRadius::same(6), pal.zebra);
            }
            let mid = rect.center().y;
            let playing = now.as_deref() == Some(r.path.as_str());
            p.text(pos2(rect.left() + 12.0, mid), Align2::LEFT_CENTER, &r.time, look::text(13.0), pal.secondary);
            let color = if playing { pal.accent } else if playable { pal.text } else { pal.secondary };
            let font = if playing { look::title(13.0) } else { look::text(13.0) };
            let mut artist_rect = Rect::NOTHING;
            for (x, g) in [
                (70.0, elided(ui, &r.title, font, color, tw)),
                (70.0 + tw + 16.0, elided(ui, tr_name(&r.artist), look::text(13.0), pal.secondary, aw)),
            ] {
                let h = g.size().y;
                artist_rect = Rect::from_min_size(pos2(rect.left() + x, mid - h / 2.0), g.size());
                p.galley(artist_rect.min, g, color);
            }
            p.text(pos2(rect.right() - 12.0, mid), Align2::RIGHT_CENTER, tr_text(&r.minutes), look::text(13.0), pal.secondary);
            if playable {
                // 2.4: a right click on a song opens its menu
                let path = r.path.clone();
                resp.context_menu(|ui| self.song_menu(ui, &path));
            }
            if playable && resp.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                play = Some(i);
            }
            // 2.4: the artist is a link of its own (on top of the row)
            let link = self.artist_link(&r.artist);
            if let Some(l) = self.link_rect(ui, artist_rect, ("day-artist", i), link, None) {
                follow = Some(l);
            }
        }
        self.places.day_heads = heads;
        if let Some(i) = play {
            self.calendar_play(i);
        }
        if let Some(l) = follow {
            self.follow(l);
        }
        ui.add_space(18.0);
    }
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

    /// The Library page's entries in their order (2.3): the fifteen, then the
    /// entries that come after them (`more_library_entries()`), each with its count
    /// and the page it opens.
    pub fn library_entries(&mut self) -> Vec<LibraryEntry> {
        let recent_n = self.recently_played().len();
        let year = self.year_stats();
        let l = &self.lib;
        let entry = |icon, name: &str, count: String, page| LibraryEntry { icon, name: name.to_string(), count, page };
        let mut out = vec![
            entry(Icon::Note, tr!("All Songs"), songs(l.tracks.len()), Page::AllSongs),
            entry(Icon::Folder, tr!("Folders"), count(self.scanner.cfg.library_folders.len(), n_!("1 folder"), n_!("{n} folders")), Page::Folders(None)),
            entry(Icon::Albums, tr!("Albums"), albums(l.albums.len()), Page::Albums),
            entry(Icon::Person, tr!("Artists"), artists(l.artists.len()), Page::Artists),
            entry(Icon::People, tr!("Album Artists"), count(l.album_artists.len(), n_!("1 album artist"), n_!("{n} album artists")), Page::AlbumArtists),
            entry(Icon::Genres, tr!("Genres"), count(l.genres.len(), n_!("1 genre"), n_!("{n} genres")), Page::Genres),
            entry(Icon::Calendar, tr!("Years"), count(l.years.len(), n_!("1 year"), n_!("{n} years")), Page::Years),
            entry(Icon::Composer, tr!("Composers"), count(l.composers.len(), n_!("1 composer"), n_!("{n} composers")), Page::Composers),
            self.moods_library_entry(), // 2.4
            entry(Icon::Playlist, tr!("Playlists"), count(self.state.playlists.len(), n_!("1 playlist"), n_!("{n} playlists")), Page::Playlists),
            entry(Icon::Heart, tr!("Liked Songs"), songs(self.state.liked.len()), Page::Liked),
            entry(Icon::ThumbDown, tr!("Disliked Songs"), songs(self.state.disliked.len()), Page::Disliked),
            entry(Icon::Clock, tr!("Recently Played"), songs(recent_n), Page::RecentlyPlayed),
            entry(Icon::Albums, tr!("Recently Added"), albums(l.albums.len().min(50)), Page::Recent),
            entry(
                Icon::Chart,
                tr!("Your Stats"),
                tr!("{n} minutes this year", n = stats::thousands((year.seconds / 60.0).round() as i64)),
                Page::Stats,
            ),
        ];
        out.extend(self.more_library_entries());
        out
    }

    /// Entries after the fifteen: the custom tabs (2.3), with their songs.
    fn more_library_entries(&mut self) -> Vec<LibraryEntry> {
        self.tab_library_entries()
    }

    fn year_stats(&mut self) -> stats::Stats {
        let saved = self.stats_period.clone();
        self.stats_period = "This year".into(); // a period key (`stats::PERIODS`)
        let st = self.stats();
        self.stats_period = saved;
        st
    }

    /// Settings → Appearance → Library page, from the buttons at the right of the
    /// Library page's title (2.3): saved at once.
    pub fn set_library_layout(&mut self, layout: &str) {
        if !LIBRARY_LAYOUTS.contains(&layout) || self.scanner.cfg.library_layout == layout {
            return;
        }
        let mut new = self.scanner.cfg.clone();
        new.library_layout = layout.to_string();
        self.scanner.apply_settings(new);
        self.set_draft_library_layout(layout);
    }

    pub fn library_layout(&self) -> &'static str {
        LIBRARY_LAYOUTS.iter().copied().find(|l| *l == self.scanner.cfg.library_layout).unwrap_or(LIBRARY_LAYOUTS[0])
    }

    /// The tiles and list buttons at the right of the title; the one in use is drawn on.
    fn layout_buttons(&mut self, ui: &mut Ui, pal: &Palette) {
        let current = self.library_layout();
        let mut chosen = None;
        ui.spacing_mut().item_spacing.x = 6.0;
        // right to left: List, then Tiles
        for (layout, icon) in [("List", Icon::ListView), ("Tiles", Icon::Tiles)] {
            let (rect, _) = ui.allocate_exact_size(vec2(32.0, 32.0), Sense::hover());
            let resp = ui.interact(rect, layout_button_id(layout), Sense::click()).on_hover_cursor(egui::CursorIcon::PointingHand);
            let on = current == layout;
            if resp.hovered() {
                ui.painter().rect_filled(rect, CornerRadius::same(8), pal.hover);
            } else if on {
                ui.painter().rect_filled(rect, CornerRadius::same(8), pal.lcd);
            }
            icons::paint(ui.painter(), Rect::from_center_size(rect.center(), vec2(16.0, 16.0)), icon, if on { pal.accent } else { pal.secondary });
            if resp.clicked() {
                chosen = Some(layout);
            }
        }
        if let Some(layout) = chosen {
            self.set_library_layout(layout);
        }
    }

    fn library_tiles(&mut self, ui: &mut Ui, pal: &Palette, entries: &[LibraryEntry]) {
        let gap = 12.0;
        let width = ui.available_width();
        let cols = ((width + gap) / (210.0 + gap)).floor().max(1.0) as usize;
        let tw = (width + gap) / cols as f32 - gap;
        for row in entries.chunks(cols) {
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = gap;
                for e in row {
                    let (rect, resp) = ui.allocate_exact_size(vec2(tw, 74.0), Sense::click());
                    let fill = if resp.hovered() { pal.hover } else { pal.lcd };
                    ui.painter().rect_filled(rect, CornerRadius::same(10), fill);
                    icons::paint(ui.painter(), Rect::from_center_size(rect.left_top() + vec2(26.0, 26.0), vec2(22.0, 22.0)), e.icon, pal.accent);
                    let g = elided(ui, &e.name, look::title(14.0), pal.text, tw - 56.0);
                    ui.painter().galley(rect.left_top() + vec2(46.0, 16.0), g, pal.text);
                    let g = elided(ui, &e.count, look::text(12.0), pal.secondary, tw - 56.0);
                    ui.painter().galley(rect.left_top() + vec2(46.0, 38.0), g, pal.secondary);
                    if resp.clicked() {
                        self.go(e.page.clone());
                    }
                }
            });
            ui.add_space(gap - ui.spacing().item_spacing.y);
        }
    }

    /// One row per entry, the width of the page (2.3): the icon in the accent colour,
    /// the name in bold, its count on the right in grey, a line between rows.
    fn library_list(&mut self, ui: &mut Ui, pal: &Palette, entries: &[LibraryEntry]) {
        let width = ui.available_width();
        let saved = ui.spacing().item_spacing.y;
        ui.spacing_mut().item_spacing.y = 0.0;
        for (i, e) in entries.iter().enumerate() {
            let (rect, resp) = ui.allocate_exact_size(vec2(width, LIST_ROW_H), Sense::click());
            let resp = resp.on_hover_cursor(egui::CursorIcon::PointingHand);
            let p = ui.painter();
            if resp.hovered() {
                p.rect_filled(rect, CornerRadius::same(8), pal.row_hover);
            }
            icons::paint(p, Rect::from_center_size(pos2(rect.left() + 24.0, rect.center().y), vec2(20.0, 20.0)), e.icon, pal.accent);
            let count = p.text(pos2(rect.right() - 14.0, rect.center().y), Align2::RIGHT_CENTER, &e.count, look::text(13.0), pal.secondary);
            let g = elided(ui, &e.name, look::title(14.0), pal.text, (count.left() - rect.left() - 66.0).max(20.0));
            let h = g.size().y;
            ui.painter().galley(pos2(rect.left() + 50.0, rect.center().y - h / 2.0), g, pal.text);
            if i + 1 < entries.len() {
                ui.painter().hline(rect.left() + 50.0..=rect.right(), rect.bottom(), Stroke::new(1.0, pal.separator));
            }
            if resp.clicked() {
                self.go(e.page.clone());
            }
        }
        ui.spacing_mut().item_spacing.y = saved;
        ui.add_space(12.0);
    }

    pub fn library_home(&mut self, ui: &mut Ui, pal: &Palette) {
        let year = self.year_stats();
        let entries = self.library_entries();
        egui::ScrollArea::vertical().id_salt("library-home").auto_shrink([false, false]).show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new(tr!("Library")).font(look::title(28.0)).color(pal.text));
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| self.layout_buttons(ui, pal));
            });
            ui.add_space(12.0);
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
            if self.library_layout() == "List" {
                self.library_list(ui, pal, &entries);
            } else {
                self.library_tiles(ui, pal, &entries);
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
            self.table_folders = rows;
            self.song_table_kind(ui, pal, &[], tr!("Folders"), "folders", false);
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
        // 2.4 ("Folder cannot be scrolled"): the subfolders and the songs are one scrolling
        // list (the subfolders are rows of the song table, above the songs). They used to be
        // drawn straight into the page, which does not scroll, so a folder with more
        // subfolders than the window holds could not be scrolled, and the songs' table
        // below them got only the room that was left (none when the subfolders filled it).
        if !subs.is_empty() || !songs.is_empty() {
            let list: Vec<String> = songs.iter().map(|t| t.path.clone()).collect();
            let name = file_name(&folder);
            self.table_folders = subs;
            self.song_table_kind(ui, pal, &list, &name, "folders", false);
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
                        if let Some(c) = self.label_of("playlist", id) {
                            crate::labels::paint_cover_dot(&p, crect, c, pal); // 2.3: its colour label
                        }
                        if resp.hovered() {
                            ui.painter().rect_filled(crect, CornerRadius::same(8), Color32::from_black_alpha(30));
                        }
                        let g = elided(ui, name, look::title(13.0), pal.text, size);
                        ui.painter().galley(pos2(rect.left(), crect.bottom() + 6.0), g, pal.text);
                        ui.painter().text(pos2(rect.left(), crect.bottom() + 24.0), Align2::LEFT_TOP, self::songs(songs.len()), look::text(12.0), pal.secondary);
                        if resp.clicked() {
                            self.go(Page::Playlist(id.clone()));
                        }
                        resp.context_menu(|ui| self.playlist_menu(ui, id)); // 2.3
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

    pub(crate) fn period_menu(&mut self, ui: &mut Ui) {
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
        let english = stats::cards(&st);
        let cards: Vec<(String, String, String)> = english.iter().map(|(l, b, d)| shown_card(l, b, d)).collect();
        // 2.4: the cards' names are links (the big text, and the artist in the detail)
        let links: Vec<CardLinks> = english.iter().map(|(l, _, _)| self.card_links(l, &st)).collect();
        let mut follow = None;
        egui::ScrollArea::vertical().id_salt("stats").auto_shrink([false, false]).show(ui, |ui| {
            self.calendar_ui(ui, pal);
            if cards.is_empty() {
                ui.label(RichText::new(tr!("No listening yet in this period. Play some music and come back.")).color(pal.secondary));
                return;
            }
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
                        let big_rect = Rect::from_min_size(rect.left_top() + vec2(18.0, 38.0), g.size());
                        p.galley(big_rect.min, g, Color32::WHITE);
                        let g = elided(ui, detail, look::text(13.0), Color32::from_white_alpha(230), cw - 36.0);
                        let detail_rect = Rect::from_min_size(pos2(rect.left() + 18.0, rect.bottom() - 30.0), g.size());
                        p.galley(detail_rect.min, g, Color32::WHITE);
                        let l = links[k].clone();
                        if let Some(x) = self.link_rect(ui, big_rect, ("card-big", k), l.big, l.song.as_deref()) {
                            follow = Some(x);
                        }
                        if let Some(x) = self.link_rect(ui, detail_rect, ("card-detail", k), l.detail, None) {
                            follow = Some(x);
                        }
                    }
                });
                ui.add_space(gap - ui.spacing().item_spacing.y);
            }
            self.leaderboard_section(ui, pal); // 2.4
            ui.add_space(8.0);
            self.places_section(ui, pal); // 2.4: the period's places
            if pill(ui, pal, Icon::Chart, tr!("Details")).clicked() {
                self.go(Page::StatsDetails);
            }
        });
        if let Some(l) = follow {
            self.follow(l);
        }
    }

    /// The links of a summary card (2.4): the top song plays (the period's most played
    /// songs as the queue) and its artist opens their page; the top artist, album (and its
    /// artist) and genre open theirs.
    fn card_links(&mut self, label: &str, st: &stats::Stats) -> CardLinks {
        let mut out = CardLinks::default();
        match label {
            "Your top song" => {
                if let Some(s) = st.top_songs.first() {
                    let queue = stats::ranked_songs(&self.board("songs"));
                    out.big = self.song_link(&queue, &s.key, "Your Stats");
                    out.song = Some(s.key.clone());
                    out.detail = self.artist_link(&s.artist);
                }
            }
            "Your top artist" => out.big = st.top_artists.first().and_then(|a| self.artist_link(&a.name)),
            "Your top album" => {
                if let Some(a) = st.top_albums.first() {
                    let paths = self.board("albums").into_iter().find(|r| r.key == a.key).map(|r| r.paths).unwrap_or_default();
                    out.big = self.album_link(&a.name, &a.artist, &paths);
                    out.detail = self.artist_link(&a.artist);
                }
            }
            "Your top genre" => out.big = st.top_genres.first().and_then(|g| self.genre_link(&g.name)),
            _ => {}
        }
        out
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
                // 2.4: the names are links
                let mut follow = None;
                ui.columns(2, |cols| {
                    follow = follow.take().or(self.top_list(&mut cols[0], pal, tr!("Top songs"), &st.top_songs, true, "songs"));
                    follow = follow.take().or(self.top_list(&mut cols[1], pal, tr!("Top artists"), &st.top_artists, false, "artists"));
                });
                ui.columns(2, |cols| {
                    follow = follow.take().or(self.top_list(&mut cols[0], pal, tr!("Top albums"), &st.top_albums, true, "albums"));
                    follow = follow.take().or(self.top_list(&mut cols[1], pal, tr!("Top genres"), &st.top_genres, false, "genres"));
                });
                if let Some(l) = follow {
                    self.follow(l);
                }
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

impl Player {
    /// A top list of Details ("Top songs"…): rank, name and artist (2.4: links; a right
    /// click on a song opens its menu), plays and minutes.
    #[allow(clippy::too_many_arguments)]
    fn top_list(&mut self, ui: &mut Ui, pal: &Palette, title: &str, rows: &[stats::Row], with_artist: bool, kind: &str) -> Option<Link> {
        section(ui, pal, title);
        let queue: Vec<String> = if kind == "songs" { rows.iter().take(50).map(|r| r.key.clone()).collect() } else { vec![] };
        let album_paths: HashMap<String, Vec<String>> =
            if kind == "albums" { self.board("albums").into_iter().map(|r| (r.key, r.paths)).collect() } else { HashMap::new() };
        let mut out = None;
        for (i, r) in rows.iter().take(50).enumerate() {
            ui.horizontal(|ui| {
                ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                    ui.label(RichText::new(format!("{:>2}", i + 1)).color(pal.secondary).monospace());
                    let artist = if with_artist && !r.artist.is_empty() { tr_name(&r.artist).to_string() } else { String::new() };
                    let room = (ui.available_width() - 140.0).max(60.0);
                    let name_room = if artist.is_empty() { room } else { room * 2.0 / 3.0 };
                    let (link, song) = match kind {
                        "songs" => (self.song_link(&queue, &r.key, "Your Stats"), Some(r.key.as_str())),
                        "artists" => (self.artist_link(&r.name), None),
                        "albums" => (self.album_link(&r.name, &r.artist, album_paths.get(&r.key).map(|v| v.as_slice()).unwrap_or(&[])), None),
                        _ => (self.genre_link(&r.name), None),
                    };
                    if let Some(l) = self.link_label(ui, tr_name(&r.name), look::text(13.0), pal.text, name_room, link, song) {
                        out = Some(l);
                    }
                    if !artist.is_empty() {
                        ui.label(RichText::new("—").color(pal.secondary));
                        let link = self.artist_link(&r.artist);
                        if let Some(l) = self.link_label(ui, &artist, look::text(13.0), pal.text, room - name_room, link, None) {
                            out = Some(l);
                        }
                    }
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
        out
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

fn kv_list(ui: &mut Ui, pal: &Palette, title: &str, rows: &[(String, usize)]) {
    ui.label(RichText::new(title).font(look::title(13.5)).color(pal.text));
    for (k, n) in rows {
        ui.horizontal(|ui| {
            ui.label(RichText::new(k).color(pal.text));
            ui.label(RichText::new(stats::thousands(*n as i64)).color(pal.secondary));
        });
    }
}
