//! Colour labels (2.3, `docs/player-spec.md`, "9. Colours"): "Colour Label ▸" in the
//! menus of songs, albums, artists, album artists, genres, years, composers,
//! playlists (and tabs), with Red … Grey, Custom… (a colour picker in a small
//! window) and None; the label is kept in `player.json` (`PlayerState::label_of()`,
//! `set_label()`) and drawn as a dot 8 px wide (`paint_dot()`): before the title in
//! song tables and Up Next, at the bottom-left corner of a card's cover, and after a
//! playlist's name in the sidebar.  The counterpart of the Python app's
//! `PlayerApp.add_label_menu()` and `player_widgets.draw_label_dot()`.

use egui::{pos2, vec2, Color32, Painter, Pos2, Rect, RichText, Sense, Ui};
use realhires::i18n;
use realhires::playerstate::LABEL_COLORS;
use realhires::tr;

use crate::look::{self, Palette};
use crate::player::Player;

/// The width of a label's dot, in pixels.
pub const DOT: f32 = 8.0;

/// A colour label's dot centred on `center`; with `ring`, inside a 2-pixel ring of that
/// colour (on a cover, where the dot needs to stand out).
pub fn paint_dot(p: &Painter, center: Pos2, color: Color32, ring: Option<Color32>) {
    if let Some(r) = ring {
        p.circle_filled(center, DOT / 2.0 + 2.0, r);
    }
    p.circle_filled(center, DOT / 2.0, color);
}

/// The dot at the bottom-left corner of a card's cover `cover`.
pub fn paint_cover_dot(p: &Painter, cover: Rect, color: Color32, pal: &Palette) {
    paint_dot(p, cover.left_bottom() + vec2(14.0, -14.0), color, Some(pal.bg));
}

/// The window of Colour Label → Custom… (one at a time, `Player::label_picker`).
#[derive(Clone, Debug)]
pub struct LabelPicker {
    pub kind: String,
    pub key: String,
    pub color: Color32,
}

/// One entry of the Colour Label menu: a swatch, the colour's name, and the one in
/// use marked.
fn swatch_entry(ui: &mut Ui, color: Color32, label: &str, on: bool) -> egui::Response {
    ui.horizontal(|ui| {
        let (r, _) = ui.allocate_exact_size(vec2(14.0, 14.0), Sense::hover());
        ui.painter().circle_filled(r.center(), 5.5, color);
        ui.add(egui::Button::new(label).selected(on).frame(on))
    })
    .inner
}

impl Player {
    /// The colour label of a song (path), album (key), artist, album artist, genre, year
    /// or composer (name), playlist (id) or tab (name), or None.
    pub fn label_of(&self, kind: &str, key: &str) -> Option<Color32> {
        self.state.label_of(kind, key).map(look::parse_hex)
    }

    /// Give it a colour label ("#rrggbb"; None takes it away), saved at once.
    pub fn set_label(&mut self, kind: &str, key: &str, color: Option<&str>) {
        self.state.set_label(kind, key, color);
        self.save_state();
    }

    /// "Colour Label ▸" (2.3) in a menu: Red, Orange, Yellow, Green, Blue, Purple, Grey,
    /// Custom… (opens the colour picker window) and None, the label in use marked.
    /// `kind` is one of `playerstate::LABEL_KINDS` ("song", "album", "artist",
    /// "album_artist", "genre", "year", "composer", "playlist", "tab") and `key` what it is
    /// kept under (a path, an album key, a name, a playlist id, a tab name).
    pub fn label_menu(&mut self, ui: &mut Ui, kind: &str, key: &str) {
        ui.menu_button(tr!("Colour Label"), |ui| {
            let current = self.state.label_of(kind, key).map(str::to_string);
            for (name, hex) in LABEL_COLORS {
                if swatch_entry(ui, look::parse_hex(hex), i18n::tr(name), current.as_deref() == Some(hex)).clicked() {
                    self.set_label(kind, key, Some(hex));
                    ui.close();
                }
            }
            ui.separator();
            let custom = current.as_deref().is_some_and(|c| !LABEL_COLORS.iter().any(|(_, h)| *h == c));
            if ui.add(egui::Button::new(tr!("Custom…")).selected(custom).frame(custom)).clicked() {
                let color = current.as_deref().map(look::parse_hex).unwrap_or_else(|| look::parse_hex("#8e8e93"));
                self.label_picker = Some(LabelPicker { kind: kind.to_string(), key: key.to_string(), color });
                ui.close();
            }
            if ui.add_enabled(current.is_some(), egui::Button::new(tr!("None"))).clicked() {
                self.set_label(kind, key, None);
                ui.close();
            }
        });
    }

    /// The Custom… window: a colour picker with OK and Cancel.  Returns whether it is open.
    pub fn label_picker_window(&mut self, ctx: &egui::Context, pal: &Palette) -> bool {
        let Some(mut picker) = self.label_picker.take() else { return false };
        let mut open = true;
        let mut done: Option<bool> = None;
        egui::Window::new(tr!("Colour Label"))
            .id(egui::Id::new("label-picker"))
            .collapsible(false)
            .resizable(false)
            .open(&mut open)
            .anchor(egui::Align2::CENTER_CENTER, vec2(0.0, 0.0))
            .show(ctx, |ui| {
                ui.label(RichText::new(tr!("Choose the colour of the label.")).color(pal.secondary));
                ui.add_space(6.0);
                egui::color_picker::color_picker_color32(ui, &mut picker.color, egui::color_picker::Alpha::Opaque);
                ui.add_space(8.0);
                ui.horizontal(|ui| {
                    let (r, _) = ui.allocate_exact_size(vec2(18.0, 18.0), Sense::hover());
                    paint_dot(ui.painter(), r.center(), picker.color, None);
                    ui.label(RichText::new(look::to_hex(picker.color)).color(pal.secondary));
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if crate::style::primary_button(ui, tr!("OK"), true).clicked() {
                            done = Some(true);
                        }
                        if crate::style::flat_button(ui, tr!("Cancel"), true).clicked() {
                            done = Some(false);
                        }
                    });
                });
            });
        if ctx.input(|i| i.key_pressed(egui::Key::Escape)) {
            done = Some(false);
        }
        match done {
            Some(true) => {
                let hex = look::to_hex(picker.color);
                self.set_label(&picker.kind, &picker.key, Some(&hex));
                false
            }
            Some(false) => false,
            None if !open => false,
            None => {
                self.label_picker = Some(picker);
                true
            }
        }
    }

    /// The menu of a playlist (2.3: right click in the sidebar and on the Playlists page):
    /// Play, Shuffle, Rename…, Delete… and Colour Label.
    pub fn playlist_menu(&mut self, ui: &mut Ui, id: &str) {
        let Some(p) = self.state.playlist(id) else {
            ui.close();
            return;
        };
        let (name, songs) = (p.name.clone(), self.playlist_songs(id));
        let songs: Vec<String> = songs.into_iter().filter(|s| self.lib.track(s).is_some()).collect();
        if ui.add_enabled(!songs.is_empty(), egui::Button::new(tr!("Play"))).clicked() {
            self.play_list(songs.clone(), Some(&songs[0]), &name);
            ui.close();
        }
        if ui.add_enabled(!songs.is_empty(), egui::Button::new(tr!("Shuffle"))).clicked() {
            self.shuffle_list(songs, &name);
            ui.close();
        }
        ui.separator();
        if ui.button(tr!("Rename…")).clicked() {
            self.ask_name(tr!("Rename Playlist"), &name, crate::player::NameAction::RenamePlaylist(id.to_string()));
            ui.close();
        }
        if ui.button(tr!("Delete…")).clicked() {
            self.confirm = Some(crate::player::ConfirmAction::DeletePlaylist(id.to_string()));
            ui.close();
        }
        ui.separator();
        self.label_menu(ui, "playlist", id);
    }
}

/// The dot of a sidebar entry's label, after its name drawn from `text_left` with
/// `width` pixels of text.
pub fn paint_after_text(p: &Painter, text_left: f32, width: f32, y: f32, color: Color32) {
    paint_dot(p, pos2(text_left + width + 6.0 + DOT / 2.0, y), color, None);
}

#[cfg(test)]
mod tests {
    //! 2.3 "Colours" in the Rust player, headlessly: the quality colours of the Quality
    //! column and the playback bar's badge, colour labels set from the song menu (a
    //! fixed colour, Custom… with its colour picker, None) and shown as dots in the song
    //! table, Up Next, on cards and after a playlist's name in the sidebar, the More
    //! colours of the theme editor (set, shown, Reset) and the verdict colours.
    use super::*;
    use crate::player::{NameAction, Page};
    use egui::{Event, PointerButton};
    use realhires::settings::Settings;
    use realhires::{musiclib, playerstate, themes};
    use std::process::Command;
    use std::time::{Duration, Instant};

    thread_local! {
        /// The window's height: 768 as the CI runners' screens (1024 x 768), taller to see a
        /// whole settings page
        static HEIGHT: std::cell::Cell<f32> = const { std::cell::Cell::new(768.0) };
    }

    fn song(dir: &std::path::Path, album: &str, title: &str, genre: &str, n: u32, rate: u32) -> String {
        let d = dir.join(album);
        std::fs::create_dir_all(&d).unwrap();
        let out = d.join(format!("{n:02} {title}.flac")).to_string_lossy().into_owned();
        let src = format!("sine=frequency={}:sample_rate={rate}:duration=20", 300 + 40 * n);
        let meta = [format!("title={title}"), format!("artist=Ada Quill"), format!("album={album}"), format!("track={n}"), format!("genre={genre}")];
        let mut args: Vec<&str> = vec!["-v", "error", "-y", "-f", "lavfi", "-i", &src];
        for m in &meta {
            args.extend(["-metadata", m.as_str()]);
        }
        args.push(&out);
        assert!(Command::new(realhires::probe::tool("ffmpeg")).args(&args).status().unwrap().success());
        realhires::paths::abspath(&out)
    }

    /// One frame with these events; the texts (with their colours and places) and the
    /// filled circles it drew, and the AccessKit nodes (label, rect).
    struct Drawn {
        texts: Vec<(String, Color32, Rect)>,
        dots: Vec<(Pos2, f32, Color32)>,
        nodes: Vec<(String, Rect)>,
    }

    fn run(ctx: &egui::Context, p: &mut Player, events: Vec<Event>) -> Drawn {
        fn walk(shape: &egui::Shape, d: &mut Drawn) {
            match shape {
                egui::Shape::Text(t) => {
                    let color = t.galley.job.sections.first().map(|s| s.format.color).unwrap_or(t.fallback_color);
                    let color = if color == Color32::PLACEHOLDER { t.fallback_color } else { color };
                    d.texts.push((t.galley.text().to_string(), color, t.galley.rect.translate(t.pos.to_vec2())));
                }
                egui::Shape::Circle(c) => d.dots.push((c.center, c.radius, c.fill)),
                egui::Shape::Vec(v) => v.iter().for_each(|s| walk(s, d)),
                _ => {}
            }
        }
        let screen = Rect::from_min_size(pos2(0.0, 0.0), vec2(1024.0, HEIGHT.with(|h| h.get())));
        let raw = egui::RawInput { screen_rect: Some(screen), events, ..Default::default() };
        let out = ctx.run_ui(raw, |ui| p.ui(ui));
        let mut d = Drawn { texts: vec![], dots: vec![], nodes: vec![] };
        out.shapes.iter().for_each(|s| walk(&s.shape, &mut d));
        if let Some(update) = &out.platform_output.accesskit_update {
            for (_, node) in &update.nodes {
                if let (Some(label), Some(b)) = (node.label(), node.bounds()) {
                    d.nodes.push((label.to_string(), Rect::from_min_max(pos2(b.x0 as f32, b.y0 as f32), pos2(b.x1 as f32, b.y1 as f32))));
                }
            }
        }
        out.drop_without_applying_deltas();
        d
    }

    /// A few frames: the texts and dots of the last, the AccessKit nodes of all (an update
    /// lists only the nodes that changed).
    fn frames(ctx: &egui::Context, p: &mut Player) -> Drawn {
        let mut nodes = run(ctx, p, vec![]).nodes;
        nodes.extend(run(ctx, p, vec![]).nodes);
        let mut last = run(ctx, p, vec![]);
        nodes.append(&mut last.nodes);
        let mut seen = std::collections::HashSet::new();
        nodes.retain(|(l, r)| seen.insert((l.clone(), r.min.x.to_bits(), r.min.y.to_bits())));
        last.nodes = nodes;
        last
    }

    fn press(pos: Pos2, button: PointerButton, pressed: bool) -> Event {
        Event::PointerButton { pos, button, pressed, modifiers: Default::default() }
    }

    /// A click (press and release in separate frames) with `button` at `at`.
    fn click(ctx: &egui::Context, p: &mut Player, at: Pos2, button: PointerButton) {
        run(ctx, p, vec![Event::PointerMoved(at)]);
        run(ctx, p, vec![press(at, button, true)]);
        run(ctx, p, vec![press(at, button, false)]);
    }

    /// The rect of the AccessKit node `label` (a few frames for a menu to appear).
    fn node(ctx: &egui::Context, p: &mut Player, label: &str) -> Rect {
        for _ in 0..4 {
            if let Some((_, r)) = run(ctx, p, vec![]).nodes.into_iter().find(|(l, _)| l == label) {
                return r;
            }
        }
        panic!("no {label:?} on the screen");
    }

    /// Right-click the text `text`, open Colour Label and click `entry` in it.
    fn label_from_menu(ctx: &egui::Context, p: &mut Player, text: &str, entry: &str) {
        let d = frames(ctx, p);
        let at = d.texts.iter().find(|(t, _, _)| t == text).unwrap_or_else(|| panic!("no {text:?}")).2.center();
        click(ctx, p, at, PointerButton::Secondary);
        let sub = node(ctx, p, "Colour Label").center();
        run(ctx, p, vec![Event::PointerMoved(sub)]);
        let item = node(ctx, p, entry).center();
        // along the submenu's row first, so the pointer does not cross other entries
        run(ctx, p, vec![Event::PointerMoved(pos2(item.x, sub.y))]);
        click(ctx, p, item, PointerButton::Primary);
        frames(ctx, p);
    }

    fn has_dot(d: &Drawn, color: &str) -> bool {
        let c = look::parse_hex(color);
        d.dots.iter().any(|(_, r, fill)| *fill == c && (*r - DOT / 2.0).abs() < 0.01)
    }

    #[test]
    fn quality_colours_labels_and_more_colours() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        realhires::i18n::set_thread_language("en");
        let tmp = std::env::temp_dir().join(format!("rhr-colours-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::covers::COVERS_ENV, tmp.join("covers"));
        let hires = song(&music, "High Tide", "Breakers", "Folk", 1, 96_000);
        let cd = song(&music, "Low Tide", "Shallows", "Jazz", 1, 44_100);
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        cfg.theme = "Light".into();
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        ctx.enable_accesskit();
        let mut p = Player::new(&ctx, cfg, vec![]);
        let t0 = Instant::now();
        while !(p.lib.tracks.len() == 2) {
            assert!(t0.elapsed() < Duration::from_secs(90), "the library index");
            run(&ctx, &mut p, vec![]);
            std::thread::sleep(Duration::from_millis(20));
        }

        // the quality colours: gold for Hi-Res Lossless, the accent for Lossless (Light theme)
        p.go(Page::AllSongs);
        let d = frames(&ctx, &mut p);
        let pal = look::current(&ctx);
        assert_eq!((pal.quality_hires, pal.quality_lossless), (look::parse_hex("#c9a227"), look::parse_hex("#fa2d48")));
        let color_of = |d: &Drawn, text: &str| d.texts.iter().find(|(t, _, _)| t == text).map(|(_, c, _)| *c);
        assert_eq!(color_of(&d, "Hi-Res Lossless"), Some(pal.quality_hires));
        assert_eq!(color_of(&d, "Lossless"), Some(pal.quality_lossless));
        // ... and the playback bar's badge
        p.play_list(vec![hires.clone(), cd.clone()], Some(&hires), "Tides");
        p.engine.pause();
        let d = frames(&ctx, &mut p);
        let badges: Vec<Color32> = d.texts.iter().filter(|(t, _, _)| t == "Hi-Res Lossless").map(|(_, c, _)| *c).collect();
        assert!(badges.len() >= 2 && badges.iter().all(|c| *c == pal.quality_hires), "{badges:?}");

        // a colour label from the song menu: a dot before the title, saved at once
        label_from_menu(&ctx, &mut p, "Shallows", "Red");
        assert_eq!(playerstate::load(None).label_of("song", &cd), Some("#ff3b30"));
        let d = frames(&ctx, &mut p);
        assert!(has_dot(&d, "#ff3b30"), "the label's dot in the table");
        let title = d.texts.iter().find(|(t, _, _)| t == "Shallows").unwrap().2;
        assert!(d.dots.iter().any(|(c, _, f)| *f == look::parse_hex("#ff3b30") && c.x < title.left() && (c.y - title.center().y).abs() < 4.0));
        // Custom…: the colour picker window; OK keeps its colour
        label_from_menu(&ctx, &mut p, "Shallows", "Custom…");
        let picker = p.label_picker.as_mut().expect("the Colour Label window");
        assert_eq!((picker.kind.as_str(), picker.color), ("song", look::parse_hex("#ff3b30")));
        picker.color = Color32::from_rgb(0x12, 0x34, 0x56);
        let ok = node(&ctx, &mut p, "OK").center();
        click(&ctx, &mut p, ok, PointerButton::Primary);
        assert!(p.label_picker.is_none());
        assert_eq!(p.state.label_of("song", &cd), Some("#123456"));
        // Up Next shows the dot too
        p.show_up_next = true;
        let d = frames(&ctx, &mut p);
        let row = d.texts.iter().filter(|(t, _, _)| t == "Shallows").map(|(_, _, r)| *r).fold(Rect::NOTHING, |a, r| if r.left() > a.left() || a == Rect::NOTHING { r } else { a });
        assert!(row.left() > 600.0, "Shallows in the Up Next panel at the right: {row:?}");
        assert!(d.dots.iter().any(|(c, _, f)| *f == look::parse_hex("#123456") && c.x < row.left() && c.x > row.left() - 20.0), "the dot in Up Next");
        p.show_up_next = false;
        // None takes it away
        label_from_menu(&ctx, &mut p, "Shallows", "None");
        assert_eq!(playerstate::load(None).label_of("song", &cd), None);
        assert!(!has_dot(&frames(&ctx, &mut p), "#123456"));

        // an album's, a genre's and a playlist's labels: on the cards and in the sidebar
        let album = p.lib.albums.iter().find(|a| a.title == "High Tide").unwrap().key.clone();
        p.set_label("album", &album, Some("#34c759"));
        p.go(Page::Albums);
        assert!(has_dot(&frames(&ctx, &mut p), "#34c759"), "the album card's dot");
        p.set_label("genre", "JAZZ", Some("#af52de"));
        p.go(Page::Genres);
        assert!(has_dot(&frames(&ctx, &mut p), "#af52de"), "the genre card's dot (names in any case)");
        let id = p.new_playlist("Road Trip", &[hires.clone()]);
        label_from_menu(&ctx, &mut p, "Road Trip", "Orange"); // the sidebar's playlist menu
        assert_eq!(playerstate::load(None).label_of("playlist", &id), Some("#ff9500"));
        let d = frames(&ctx, &mut p);
        let name = d.texts.iter().find(|(t, _, _)| t == "Road Trip").unwrap().2;
        assert!(d.dots.iter().any(|(c, _, f)| *f == look::parse_hex("#ff9500") && c.x > name.right()), "after the name");

        // More colours: an own theme sets an extra colour, which the app draws; Reset
        let _ = p.apply_name(NameAction::NewTheme("Light".into()), "Gold Rush");
        p.go(Page::Settings);
        p.settings_group = realhires::settings::GROUPS.iter().position(|g| *g == "Appearance").unwrap();
        HEIGHT.with(|h| h.set(1600.0)); // the whole Appearance page
        let d = frames(&ctx, &mut p);
        let shown: Vec<&str> = d.texts.iter().map(|(t, _, _)| t.as_str()).collect();
        for key in themes::EXTRA_COLOR_KEYS {
            assert!(shown.contains(&themes::extra_color_label(key)), "{key} in More colours: {shown:?}");
        }
        assert_eq!(shown.iter().filter(|t| **t == "automatic").count(), themes::EXTRA_COLOR_KEYS.len());
        assert!(shown.contains(&"More colours") && !shown.contains(&"Reset"));
        let i = p.custom_themes.iter().position(|t| t.name == "Gold Rush").unwrap();
        p.custom_themes[i].set_extra("quality_hires", Some("#00aa88"));
        p.custom_themes[i].set_extra("verdict_fake_hires", Some("#0000ff"));
        p.themes_changed();
        p.set_theme("Gold Rush");
        frames(&ctx, &mut p);
        let pal = look::current(&ctx);
        assert_eq!(pal.quality_hires, look::parse_hex("#00aa88"));
        assert_eq!(pal.verdict(realhires::verdict::Verdict::Fake), look::parse_hex("#0000ff"));
        assert_eq!(crate::style::verdict_text(realhires::verdict::Verdict::Fake, &pal), look::parse_hex("#0000ff"));
        assert_eq!(themes::load_custom(None)[0].extra_color("quality_hires"), Some("#00aa88"));
        let resets: Vec<Rect> = frames(&ctx, &mut p).nodes.into_iter().filter(|(l, _)| l == "Reset").map(|(_, r)| r).collect();
        assert_eq!(resets.len(), 2, "a Reset for each colour the theme sets");
        click(&ctx, &mut p, resets[0].center(), PointerButton::Primary); // quality_hires (the first set)
        frames(&ctx, &mut p);
        assert_eq!(themes::load_custom(None)[0].extra_color("quality_hires"), None);
        assert_eq!(look::current(&ctx).quality_hires, look::parse_hex("#c9a227"), "derived again");
        HEIGHT.with(|h| h.set(768.0));
        p.go(Page::AllSongs);
        let d = frames(&ctx, &mut p);
        assert_eq!(color_of(&d, "Hi-Res Lossless"), Some(look::parse_hex("#c9a227")));
        p.on_exit();
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
