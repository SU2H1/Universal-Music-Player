//! Where a song was played, in the egui player (2.4, `docs/player-spec.md` "3. Where
//! a song was played"), as the Tk player's `player_places.py` does it: the place the
//! plays are stored with (`PlacesState`, looked up in a thread of its own, one at a
//! time, when `places::PlaceCache` says it is due: at start or when the setting is
//! turned on, then at most every 2 hours while music plays), the rows Settings →
//! Playback shows under the location service, the place of each play in the day list
//! of the Your Stats calendar, Your Stats' "Places" part, and a place's page with the
//! songs played there.  Nothing is asked while `play_location` is off, which it is
//! until the user turns it on.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::sync::Arc;
use std::time::Duration;

use egui::{pos2, vec2, Align2, Color32, CornerRadius, Painter, Rect, RichText, Sense, Ui};
use realhires::i18n::tr_text;
use realhires::places::{self, GeoClient, Place, PlaceCache, PlaceRow};
use realhires::settings::Settings;
use realhires::{n_, tr};

use crate::icons::{self, Icon};
use crate::look::{self, Palette};
use crate::pages::elided;
use crate::player::{plural, ConfirmAction, Page, Player};

/// A place's row on Your Stats, and a place's line in the day list.
pub const ROW_H: f32 = 30.0;
pub const HEAD_H: f32 = 26.0;

/// A lookup's answer: (generation, the client back, place, error text).
type Answer = (u64, GeoClient, Option<Place>, String);

/// The place the plays are stored with, and the lookup.
pub struct PlacesState {
    pub cache: PlaceCache,
    /// None while a lookup's thread holds it
    client: Option<GeoClient>,
    /// answers of an earlier setting are dropped
    gen: u64,
    rx: Option<Receiver<Answer>>,
    /// set when the app closes
    pub stop: Arc<AtomicBool>,
    /// the period's places for Your Stats (period, history generation, rows)
    rows: Option<(String, u64, Vec<PlaceRow>)>,
    /// the last name of each place shown (a place's page keeps it when another period has no plays there)
    names: HashMap<String, String>,
    /// where Your Stats drew each place (for the tests)
    pub hits: Vec<(String, Rect)>,
    /// the places the day list named, in order (for the tests)
    pub day_heads: Vec<String>,
    /// Forget play locations… as drawn last (for the tests)
    pub forget_button: Option<Rect>,
}

impl Default for PlacesState {
    fn default() -> Self {
        PlacesState {
            cache: PlaceCache::default(),
            client: None,
            gen: 0,
            rx: None,
            stop: Arc::new(AtomicBool::new(false)),
            rows: None,
            names: HashMap::new(),
            hits: vec![],
            day_heads: vec![],
            forget_button: None,
        }
    }
}

fn wall_clock() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// A map pin, about `s` pixels tall, centred on `c`; `hole` is the colour behind it.
pub fn paint_pin(p: &Painter, c: egui::Pos2, s: f32, color: Color32, hole: Color32) {
    let r = s * 0.3;
    let top = c.y - s * 0.18;
    p.add(egui::Shape::convex_polygon(
        vec![pos2(c.x - r * 0.82, top + r * 0.55), pos2(c.x + r * 0.82, top + r * 0.55), pos2(c.x, c.y + s * 0.48)],
        color,
        egui::Stroke::NONE,
    ));
    p.circle_filled(pos2(c.x, top), r, color);
    p.circle_filled(pos2(c.x, top), r * 0.38, hole);
}

/// A line of the day list naming the place of the plays under it ("" = not known).
pub fn day_place_line(ui: &mut Ui, pal: &Palette, text: &str, width: f32) {
    let (rect, _) = ui.allocate_exact_size(vec2(width, HEAD_H), Sense::hover());
    let p = ui.painter_at(rect);
    let mid = rect.center().y + 2.0;
    paint_pin(&p, pos2(rect.left() + 12.0, mid), 14.0, if text.is_empty() { pal.secondary } else { pal.accent }, pal.bg);
    let shown = if text.is_empty() { tr!("Place unknown").to_string() } else { text.to_string() };
    let g = elided(ui, &shown, look::title(11.5), pal.secondary, width - 40.0);
    let h = g.size().y;
    p.galley(pos2(rect.left() + 26.0, mid - h / 2.0), g, pal.secondary);
}

impl Player {
    fn place_client(&mut self) -> Option<&mut GeoClient> {
        let server = places::normalize_server(&self.scanner.cfg.location_server);
        if self.places.rx.is_some() {
            return None; // a lookup's thread holds it: one request at a time
        }
        if self.places.client.as_ref().is_none_or(|c| c.server != server) {
            self.places.client = Some(GeoClient::new(&server).with_stop(self.places.stop.clone()));
        }
        self.places.client.as_mut()
    }

    /// Every frame: take in a lookup's answer, and start the lookup that is due.
    pub fn places_tick(&mut self) {
        if let Some(rx) = &self.places.rx {
            match rx.try_recv() {
                Ok((gen, client, place, error)) => {
                    self.places.rx = None;
                    let server = places::normalize_server(&self.scanner.cfg.location_server);
                    if gen == self.places.gen {
                        self.places.cache.finish(place, &error, wall_clock());
                    }
                    // the same service keeps its client (and its Retry-After)
                    if client.server == server && self.places.client.is_none() {
                        self.places.client = Some(client);
                    }
                }
                Err(mpsc::TryRecvError::Disconnected) => {
                    // the thread ended without an answer (the app is closing)
                    self.places.rx = None;
                    if self.places.cache.busy {
                        self.places.cache.finish(None, "", wall_clock());
                    }
                }
                Err(mpsc::TryRecvError::Empty) => self.ctx.request_repaint_after(Duration::from_millis(100)),
            }
        }
        if !self.scanner.cfg.play_location {
            return;
        }
        let playing = self.is_playing();
        let cache = self.places.cache.clone();
        let Some(client) = self.place_client() else { return };
        if cache.due(client.now(), playing, client.not_before) {
            self.lookup_place();
        }
    }

    /// Ask the location service now, in a thread (one lookup at a time).
    pub fn lookup_place(&mut self) {
        if self.places.cache.busy {
            return;
        }
        let Some(client) = self.place_client() else { return };
        let now = client.now();
        let Some(mut client) = self.places.client.take() else { return };
        self.places.cache.begin(now);
        let (tx, rx) = mpsc::channel();
        let gen = self.places.gen;
        let ctx = self.ctx.clone();
        std::thread::Builder::new()
            .name("place lookup".into())
            .spawn(move || {
                let (place, error) = places::lookup_now(&mut client);
                let _ = tx.send((gen, client, place, error));
                ctx.request_repaint();
            })
            .ok();
        self.places.rx = Some(rx);
        self.ctx.request_repaint_after(Duration::from_millis(100));
    }

    /// The place a play is stored with now (None while the setting is off).
    pub fn play_place(&self) -> Option<&Place> {
        if self.scanner.cfg.play_location {
            self.places.cache.place.as_ref()
        } else {
            None
        }
    }

    /// Turned on, off, or another service: start again (a lookup at once when on).
    pub fn places_settings_changed(&mut self, old: &Settings, new: &Settings) {
        let before = (old.play_location, places::normalize_server(&old.location_server));
        let after = (new.play_location, places::normalize_server(&new.location_server));
        if before == after {
            return;
        }
        // a lookup still running is answered into the old generation, and dropped; the same
        // service keeps its client (and its Retry-After)
        self.places.gen += 1;
        self.places.cache.reset();
        if self.places.client.as_ref().is_some_and(|c| c.server != after.1) {
            self.places.client = None;
        }
        if new.play_location {
            self.places_tick();
        }
    }

    /// The Settings note on the lookup: off, looking, the place found, or why none.
    pub fn place_status(&self) -> String {
        if !self.scanner.cfg.play_location {
            return tr!("Turned off: no place is asked for or stored.").to_string();
        }
        let c = &self.places.cache;
        if let Some(p) = &c.place {
            let time = c
                .looked_up
                .map(|t| {
                    let lp = realhires::stats::local_parts(t);
                    format!("{:02}:{:02}", lp.hour, lp.minute)
                })
                .unwrap_or_default();
            return tr!("Approximate place now: {place} (looked up at {time}).", place = places::place_text(Some(p)), time = time);
        }
        if c.busy {
            return tr!("Looking up the approximate place…").to_string();
        }
        if !c.error.is_empty() {
            return tr!("No place was found: {error}", error = tr_text(&c.error));
        }
        tr!("Not looked up yet.").to_string()
    }

    /// Exactly what the setting sends, and to whom.
    pub fn place_note(&self) -> String {
        let server = places::normalize_server(&self.scanner.cfg.location_server);
        let host = places::host_of(&server);
        if server.to_lowercase().starts_with("https:") {
            tr!(
                "What is sent: one HTTPS request to {host}, with the app's name, when the app starts and at most every 2 \
                 hours while music plays. The service sees this computer's IP address; no song, path or tag is sent, \
                 and the places are kept only on this computer.",
                host = host
            )
        } else {
            tr!(
                "What is sent: one unencrypted HTTP request to {host}, with the app's name, when the app starts and at \
                 most every 2 hours while music plays. The service sees this computer's IP address; no song, path or \
                 tag is sent, and the places are kept only on this computer.",
                host = host
            )
        }
    }

    /// Settings → Forget play locations… (after the confirmation): clear every stored place, keep the plays.
    pub fn forget_places(&mut self) {
        if let Some(h) = &self.history {
            h.forget_places();
        }
        self.history_gen += 1;
        self.places.rows = None;
    }

    /// The places of Your Stats' period, most played first (worked out again when the
    /// period or the history changes, never per frame).
    pub fn period_places(&mut self) -> Vec<PlaceRow> {
        if let Some((period, gen, rows)) = &self.places.rows {
            if *period == self.stats_period && *gen == self.history_gen {
                return rows.clone();
            }
        }
        let rows = places::period_places(&self.period_plays());
        for r in &rows {
            self.places.names.insert(r.key.clone(), r.name.clone());
        }
        self.places.rows = Some((self.stats_period.clone(), self.history_gen, rows.clone()));
        rows
    }

    fn period_plays(&self) -> Vec<realhires::stats::Play> {
        let (since, until) = realhires::stats::period_range(&self.stats_period, wall_clock());
        self.history.as_ref().map(|h| h.plays(since, until)).unwrap_or_default()
    }

    /// A place's name ("" when it was never shown).
    pub fn place_name(&self, key: &str) -> String {
        self.places.names.get(key).cloned().unwrap_or_default()
    }

    /// The rows under the location service on Settings → Playback.
    pub fn places_settings_ui(&mut self, ui: &mut Ui, pal: &Palette) {
        ui.add_space(14.0);
        ui.add(egui::Label::new(RichText::new(self.place_note()).color(pal.secondary)).wrap());
        ui.add_space(6.0);
        ui.add(egui::Label::new(RichText::new(self.place_status()).color(pal.text)).wrap());
        ui.add_space(6.0);
        let n = self.history.as_ref().map(|h| h.places_count()).unwrap_or(0);
        ui.horizontal_wrapped(|ui| {
            let text = if n > 0 {
                tr!("Plays with a place: {n}.", n = realhires::stats::thousands(n as i64))
            } else {
                tr!("No play has a place yet.").to_string()
            };
            ui.label(RichText::new(text).color(pal.secondary));
            let r = ui.button(tr!("Forget play locations…"));
            self.places.forget_button = Some(r.rect);
            if r.clicked() {
                self.confirm = Some(ConfirmAction::ForgetPlaces);
            }
        });
        if self.places.cache.busy {
            ui.ctx().request_repaint_after(Duration::from_millis(200));
        }
    }

    /// Your Stats' "Places" part: the period's places with their plays and minutes (a
    /// click opens the place's songs), or a hint.
    pub fn places_section(&mut self, ui: &mut Ui, pal: &Palette) {
        let rows = self.period_places();
        ui.add_space(6.0);
        ui.label(RichText::new(tr!("Places")).font(look::title(17.0)).color(pal.text));
        ui.add_space(6.0);
        self.places.hits.clear();
        if rows.is_empty() {
            if self.scanner.cfg.play_location {
                ui.label(RichText::new(tr!("No places yet in this period.")).color(pal.secondary));
            } else {
                let hint = tr!("Turn on Settings → Playback → Remember where songs were played to see where you listen.");
                let r = ui.add(egui::Label::new(RichText::new(hint).color(pal.accent)).wrap().sense(Sense::click()));
                if r.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                    self.settings_group = realhires::settings::GROUPS.iter().position(|g| *g == "Playback").unwrap_or(0);
                    self.go(Page::Settings);
                }
            }
            ui.add_space(12.0);
            return;
        }
        let width = ui.available_width();
        let mut open = None;
        for (i, r) in rows.iter().enumerate() {
            let (rect, resp) = ui.allocate_exact_size(vec2(width, ROW_H), Sense::click());
            let p = ui.painter_at(rect);
            let fill = if resp.hovered() { pal.row_hover } else if i % 2 == 1 { pal.zebra } else { pal.bg };
            if fill != pal.bg {
                p.rect_filled(rect, CornerRadius::same(6), fill);
            }
            let mid = rect.center().y;
            paint_pin(&p, pos2(rect.left() + 14.0, mid), 15.0, pal.accent, fill);
            let right = tr!(
                "{plays} · {minutes} min",
                plays = plural(r.plays.max(0) as usize, "play"),
                minutes = realhires::stats::thousands((r.seconds / 60.0 + 0.5) as i64)
            );
            let rg = p.text(pos2(rect.right() - 12.0, mid), Align2::RIGHT_CENTER, right, look::text(13.0), pal.secondary);
            let g = elided(ui, &r.name, look::text(13.0), pal.text, (rect.width() - rg.width() - 60.0).max(40.0));
            let h = g.size().y;
            p.galley(pos2(rect.left() + 30.0, mid - h / 2.0), g, pal.text);
            self.places.hits.push((r.key.clone(), rect));
            if resp.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                open = Some(r.key.clone());
            }
        }
        ui.add_space(16.0);
        if let Some(k) = open {
            self.go(Page::Place(k));
        }
    }

    /// A place of Your Stats: its name, the period's plays and minutes there, Play,
    /// Shuffle and the songs played there, most played first.
    pub fn place_page(&mut self, ui: &mut Ui, pal: &Palette, key: &str) {
        if let Some(label) = self.back_label() {
            if crate::style::back_link(ui, &label).clicked() {
                self.go_back();
                return;
            }
        }
        let rows = self.period_places();
        let row = rows.iter().find(|r| r.key == key);
        let name = row.map(|r| r.name.clone()).unwrap_or_else(|| self.place_name(key));
        ui.label(RichText::new(&name).font(look::title(28.0)).color(pal.text));
        let Some(row) = row else {
            ui.label(RichText::new(tr!("No song was played at this place in this period.")).color(pal.secondary));
            return;
        };
        ui.label(
            RichText::new(tr!(
                "{period}: {minutes}, {plays}",
                period = realhires::i18n::tr(&self.stats_period),
                minutes = plural((row.seconds / 60.0 + 0.5) as usize, "minute"),
                plays = plural(row.plays.max(0) as usize, "play")
            ))
            .color(pal.secondary),
        );
        ui.add_space(10.0);
        let songs = places::place_songs(&self.period_plays(), key);
        let list: Vec<String> = songs.iter().map(|s| s.path.clone()).collect();
        let playable: Vec<String> = list.iter().filter(|p| self.lib.by_path.contains_key(*p)).cloned().collect();
        ui.horizontal_wrapped(|ui| {
            if !playable.is_empty() {
                if pill(ui, pal, Icon::Play, tr!("Play")).clicked() {
                    self.play_list(playable.clone(), Some(&playable[0]), &name);
                }
                if pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                    self.shuffle_list(playable.clone(), &name);
                }
            }
        });
        ui.add_space(10.0);
        // the Played column: when each song was last played at this place
        let played: HashMap<String, f64> = songs.iter().map(|s| (s.path.clone(), s.last)).collect();
        self.song_table_ex(ui, pal, &list, &name, false, Some(&played));
    }
}

/// A filled accent button with an icon (Play, Shuffle), as the other pages draw them.
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

/// The page title of a place's page (a name for `Page::title()`).
pub const PLACES_TITLE: &str = n_!("Places");

/// Stop a lookup that is running (the app closes).
pub fn stop(state: &PlacesState) {
    state.stop.store(true, Ordering::Relaxed);
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::sync::Mutex;
    use std::time::Instant;

    use super::*;
    use realhires::{i18n, musiclib, playerstate};

    const W: f32 = 1024.0;
    const H: f32 = 768.0;

    /// A fake location service on 127.0.0.1 (tests never ask the real one): answers
    /// every GET like FreeIPAPI with an invented place and records (path, User-Agent).
    fn fake_geo() -> (String, Arc<Mutex<Vec<(String, String)>>>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let seen: Arc<Mutex<Vec<(String, String)>>> = Arc::default();
        let log = seen.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut s) = stream else { continue };
                let mut buf = vec![];
                let mut chunk = [0u8; 1024];
                while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
                    match s.read(&mut chunk) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => buf.extend_from_slice(&chunk[..n]),
                    }
                }
                let text = String::from_utf8_lossy(&buf).to_string();
                let path = text.split_whitespace().nth(1).unwrap_or("").to_string();
                let ua = text
                    .lines()
                    .find_map(|l| l.to_lowercase().starts_with("user-agent:").then(|| l[11..].trim().to_string()))
                    .unwrap_or_default();
                log.lock().unwrap().push((path, ua));
                let body = r#"{"ipVersion": 4, "ipAddress": "198.51.100.7", "latitude": 35.68951, "longitude": 139.69171,
                    "countryName": "Japan", "countryCode": "JP", "cityName": "Shinjuku", "regionName": "Tokyo"}"#;
                let _ = write!(
                    s,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        (format!("http://127.0.0.1:{port}/api/v1/json/"), seen)
    }

    fn song(dir: &std::path::Path, title: &str, n: u32, secs: f64) -> String {
        std::fs::create_dir_all(dir).unwrap();
        let f = dir.join(format!("{n:02} {title}.flac")).to_string_lossy().into_owned();
        let src = format!("sine=frequency={}:sample_rate=44100:duration={secs}", 300 + 40 * n);
        let meta = [format!("title={title}"), "artist=Mira Calloway".into(), "album=Paper Lanterns".into(), format!("track={n}")];
        let mut args: Vec<&str> = vec!["-v", "error", "-y", "-f", "lavfi", "-i", &src];
        for m in &meta {
            args.extend(["-metadata", m.as_str()]);
        }
        args.push(&f);
        let st = std::process::Command::new(realhires::probe::tool("ffmpeg")).args(&args).status().unwrap();
        assert!(st.success());
        realhires::paths::abspath(&f)
    }

    fn input(events: Vec<egui::Event>) -> egui::RawInput {
        egui::RawInput { screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(W, H))), events, ..Default::default() }
    }

    fn frame(ctx: &egui::Context, p: &mut Player, events: Vec<egui::Event>) {
        ctx.run_ui(input(events), |ui| p.ui(ui)).drop_without_applying_deltas();
    }

    fn texts(ctx: &egui::Context, p: &mut Player) -> Vec<(String, Rect)> {
        fn walk(shape: &egui::Shape, out: &mut Vec<(String, Rect)>) {
            match shape {
                egui::Shape::Text(t) => out.push((t.galley.text().to_string(), t.visual_bounding_rect())),
                egui::Shape::Vec(v) => v.iter().for_each(|s| walk(s, out)),
                _ => {}
            }
        }
        for _ in 0..3 {
            frame(ctx, p, vec![]);
        }
        let output = ctx.run_ui(input(vec![]), |ui| p.ui(ui));
        let mut out = vec![];
        output.shapes.iter().for_each(|s| walk(&s.shape, &mut out));
        output.drop_without_applying_deltas();
        out
    }

    fn click(ctx: &egui::Context, p: &mut Player, at: egui::Pos2) {
        frame(ctx, p, vec![egui::Event::PointerMoved(at)]);
        for pressed in [true, false] {
            let e = egui::Event::PointerButton { pos: at, button: egui::PointerButton::Primary, pressed, modifiers: Default::default() };
            frame(ctx, p, vec![e]);
        }
        frame(ctx, p, vec![egui::Event::PointerMoved(pos2(5.0, H - 5.0))]);
    }

    fn wheel(ctx: &egui::Context, p: &mut Player, at: egui::Pos2, dy: f32) {
        frame(ctx, p, vec![egui::Event::PointerMoved(at)]);
        let e = egui::Event::MouseWheel {
            unit: egui::MouseWheelUnit::Point,
            delta: vec2(0.0, dy),
            phase: egui::TouchPhase::Move,
            source: egui::MouseWheelSource::Unknown,
            modifiers: Default::default(),
        };
        frame(ctx, p, vec![e]);
        for _ in 0..30 {
            frame(ctx, p, vec![]);
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    fn run_until(ctx: &egui::Context, p: &mut Player, what: &str, done: impl Fn(&Player) -> bool) {
        let t0 = Instant::now();
        while !done(p) {
            assert!(t0.elapsed() < Duration::from_secs(60), "timed out waiting for {what}");
            frame(ctx, p, vec![]);
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn has(t: &[(String, Rect)], text: &str) -> bool {
        t.iter().any(|(s, _)| s == text)
    }

    #[test]
    fn play_locations_settings_stats_and_a_place_page() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        i18n::set_thread_language("en");
        let tmp = std::env::temp_dir().join(format!("umc-places-ui-{}", std::process::id()));
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
        let a1 = song(&music, "Lanterns Over Water", 1, 1.5);
        let a2 = song(&music, "Paper in the Rain", 2, 1.5);
        let (server, seen) = fake_geo();
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        cfg.theme = "Light".into();
        cfg.location_server = server.clone();
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library", |p| p.lib.tracks.len() == 2);

        // off: nothing is asked, and plays get no place
        for _ in 0..5 {
            frame(&ctx, &mut p, vec![]);
        }
        assert!(seen.lock().unwrap().is_empty() && p.play_place().is_none());
        p.settings_group = realhires::settings::GROUPS.iter().position(|g| *g == "Playback").unwrap();
        p.go(Page::Settings);
        let t = texts(&ctx, &mut p);
        let note_start = "What is sent: one unencrypted HTTP request to 127.0.0.1:";
        assert!(t.iter().any(|(s, _)| s.starts_with(note_start) && s.contains("IP address")), "the note");
        assert!(has(&t, "Turned off: no place is asked for or stored.") && has(&t, "No play has a place yet."));
        assert!(has(&t, "Remember where songs were played") && has(&t, "Location service"));
        // the button fits the 1024-pixel screen
        let forget = p.places.forget_button.expect("Forget play locations…");
        assert!(forget.right() <= W, "{forget:?}");

        // turned on: one lookup at once, in a thread
        p.settings_draft().play_location = true;
        p.settings_changed();
        run_until(&ctx, &mut p, "the setting", |p| p.scanner.cfg.play_location);
        run_until(&ctx, &mut p, "the lookup", |p| p.places.cache.place.is_some());
        let place = p.play_place().cloned().unwrap();
        assert_eq!((place.city.as_str(), place.region.as_str(), place.country.as_str()), ("Shinjuku", "Tokyo", "Japan"));
        assert_eq!((place.latitude, place.longitude), (Some(35.69), Some(139.69)));
        {
            let s = seen.lock().unwrap();
            assert_eq!(s.len(), 1);
            assert_eq!(s[0].0, "/api/v1/json/");
            assert_eq!(s[0].1, places::user_agent());
        }
        let t = texts(&ctx, &mut p);
        assert!(t.iter().any(|(s, _)| s.starts_with("Approximate place now: Shinjuku, Tokyo, Japan (looked up at ")), "the status");

        // a song played: stored with the place; no second lookup within 2 hours
        p.play_list(vec![a1.clone(), a2.clone()], Some(&a1), "Test");
        run_until(&ctx, &mut p, "two plays", |p| p.history.as_ref().unwrap().plays(0.0, None).len() >= 2);
        let plays = p.history.as_ref().unwrap().plays(0.0, None);
        assert!(plays.iter().all(|pl| pl.place.as_ref() == Some(&place)), "{plays:?}");
        assert_eq!(seen.lock().unwrap().len(), 1);
        // a play somewhere else, earlier today
        let osaka = Place { city: "Osaka".into(), region: "Osaka".into(), country: "Japan".into(), country_code: "JP".into(), ..Default::default() };
        let now = wall_clock();
        let lp = realhires::stats::local_parts(now);
        let midnight = now - (lp.hour * 3600 + lp.minute * 60) as f64;
        let early = (midnight + 30.0).max(now - 7200.0).min(now - 5.0);
        let track = p.lib.track(&a2).cloned().unwrap();
        assert!(p.history.as_ref().unwrap().add_play_at(&track, early, 120.0, Some(&osaka)));
        assert!(p.history.as_ref().unwrap().add_play_at(&track, early + 1.0, 90.0, Some(&osaka)));
        p.history_gen += 1;

        // Your Stats: the day list names each play's place, and the Places part lists them
        p.go(Page::Stats);
        p.calendar_choose(lp.day);
        let _ = texts(&ctx, &mut p);
        assert_eq!(p.places.day_heads, vec!["Shinjuku, Tokyo, Japan".to_string(), "Osaka, Japan".to_string()]);
        let names: Vec<String> = p.period_places().iter().map(|r| r.name.clone()).collect();
        assert_eq!(names, vec!["Osaka, Japan", "Shinjuku, Tokyo, Japan"]);
        // scrolled into view at 1024 x 768, a click opens the place's page
        let key = place.key();
        for _ in 0..10 {
            let r = p.places.hits.iter().find(|(k, _)| *k == key).unwrap().1;
            if r.bottom() < H - 10.0 && r.top() > 80.0 {
                break;
            }
            wheel(&ctx, &mut p, pos2(600.0, 400.0), -300.0);
        }
        let t = texts(&ctx, &mut p);
        assert!(has(&t, "Places") && has(&t, "Shinjuku, Tokyo, Japan") && has(&t, "Osaka, Japan"));
        assert!(has(&t, "2 plays · 4 min"), "Osaka's plays and minutes");
        let r = p.places.hits.iter().find(|(k, _)| *k == key).unwrap().1;
        assert!(r.right() <= W && r.bottom() < H, "{r:?}");
        click(&ctx, &mut p, r.center());
        assert_eq!(p.page, Page::Place(key.clone()));
        let t = texts(&ctx, &mut p);
        assert!(has(&t, "Shinjuku, Tokyo, Japan") && has(&t, "This year: 0 minutes, 2 plays"), "{:?}", t.iter().map(|x| &x.0).collect::<Vec<_>>());
        assert!(has(&t, "Lanterns Over Water") && has(&t, "Paper in the Rain") && has(&t, "Your Stats"));
        assert_eq!(p.back_label().as_deref(), Some("Your Stats"));
        p.go(Page::NowPlaying);
        assert_eq!(p.back_label().as_deref(), Some("Shinjuku, Tokyo, Japan"));
        p.go_back();
        click(&ctx, &mut p, t.iter().find(|(s, _)| s == "Play").unwrap().1.center());
        run_until(&ctx, &mut p, "playing from the place", |p| p.queue_name == "Shinjuku, Tokyo, Japan");

        // Forget play locations… (confirmed): the plays stay, without places
        p.go(Page::Settings);
        let _ = texts(&ctx, &mut p);
        assert!(texts(&ctx, &mut p).iter().any(|(s, _)| s == "Plays with a place: 4."));
        let at = p.places.forget_button.unwrap().center();
        click(&ctx, &mut p, at);
        assert_eq!(p.confirm, Some(ConfirmAction::ForgetPlaces));
        let t = texts(&ctx, &mut p);
        assert!(has(&t, "Forget play locations") && has(&t, "Forget"));
        let action = p.confirm.take().unwrap();
        p.confirmed(action);
        let h = p.history.as_ref().unwrap();
        assert_eq!(h.places_count(), 0);
        assert!(h.plays(0.0, None).len() >= 4 && h.plays(0.0, None).iter().all(|pl| pl.place.is_none()));
        assert!(has(&texts(&ctx, &mut p), "No play has a place yet."));

        // turned off: the place is dropped, Your Stats shows the hint, nothing more is asked
        p.settings_draft().play_location = false;
        p.settings_changed();
        run_until(&ctx, &mut p, "the setting off", |p| !p.scanner.cfg.play_location);
        assert!(p.play_place().is_none() && p.places.cache.place.is_none());
        assert!(has(&texts(&ctx, &mut p), "Turned off: no place is asked for or stored."));
        p.go(Page::Stats);
        let t = texts(&ctx, &mut p);
        assert!(has(&t, "Turn on Settings → Playback → Remember where songs were played to see where you listen."));
        assert_eq!(seen.lock().unwrap().len(), 1);

        // in Japanese
        i18n::set_thread_language("ja");
        p.go(Page::Settings);
        let t = texts(&ctx, &mut p);
        assert!(has(&t, "オフです：場所を問い合わせることも保存することもありません。"), "{:?}", t.iter().map(|x| &x.0).collect::<Vec<_>>());
        assert!(t.iter().any(|(s, _)| s.starts_with("送信される内容：")));
        i18n::set_thread_language("en");
        p.on_exit();
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
