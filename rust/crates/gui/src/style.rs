//! Colours, labels and small helpers shared by the windows of the desktop app,
//! and the player's widgets the Quality check page and its windows are drawn
//! with (2.1, `docs/player-spec.md`): flat rounded buttons, the accent-filled
//! primary button, segmented controls, verdict chips and a switch, all in the
//! colours of the theme in use.

use egui::{pos2, vec2, Align2, Color32, CornerRadius, Rect, Response, Role, Sense, Stroke, Ui, WidgetInfo};
use realhires::n_;
use realhires::verdict::Verdict;

use crate::icons::{self, Icon};
use crate::look::{self, mix, Palette};

pub fn hex(c: u32) -> Color32 {
    Color32::from_rgb((c >> 16) as u8, (c >> 8) as u8, c as u8)
}

/// (label, colour, description) of each verdict, as in the Python app.  The
/// label and description are English (the label is also matched by code);
/// show them with `realhires::i18n::tr()` or `verdict_label()`.
pub fn verdict_style(v: Verdict) -> (&'static str, Color32, &'static str) {
    match v {
        Verdict::FakeLossless => {
            (n_!("Fake lossless"), hex(0xa3122a), n_!("Lossless file (FLAC/WAV/ALAC...) made from an MP3/AAC"))
        }
        Verdict::Fake => (n_!("Fake hi-res"), hex(0xd32f2f), n_!("Upsampled and/or 16-bit padded")),
        Verdict::Suspect => (n_!("Suspicious"), hex(0xb26a00), n_!("Could not be confirmed either way")),
        Verdict::Real => (n_!("Real hi-res"), hex(0x2e7d32), n_!("Genuine high resolution")),
        Verdict::NotHires => (n_!("CD quality"), hex(0x1565c0), n_!("Genuine lossless, not hi-res")),
        Verdict::Lossy => (n_!("Lossy format"), hex(0x6a1b9a), n_!("MP3/AAC/... file")),
        Verdict::Error => (n_!("Error"), hex(0x616161), n_!("Could not be analysed")),
    }
}

/// The verdict's label in the language in use ("Fake hi-res", "偽ハイレゾ").
pub fn verdict_label(v: Verdict) -> &'static str {
    realhires::i18n::tr(verdict_style(v).0)
}

pub const PENDING_LABEL: &str = n_!("Not scanned");
pub const PENDING_COLOR: u32 = 0x808080;
pub const RUNNING_LABEL: &str = n_!("Analysing…");
pub const RUNNING_COLOR: u32 = 0x0b62c4;
pub const RING_FAKE: u32 = 0xd32f2f;
pub const RING_SUS: u32 = 0xef8f00;
pub const RING_OK: u32 = 0x43a047;

/// A count in the language in use: `count(n, n_!("1 file"), n_!("{n} files"))`
/// ("1 file", "3 files"; Japanese gives both forms the same text).
pub fn count(n: usize, one: &'static str, many: &'static str) -> String {
    if n == 1 {
        realhires::i18n::tr(one).to_string()
    } else {
        realhires::i18n::fill(realhires::i18n::tr(many), &[("n", n.to_string())])
    }
}

/// "3:07" or "1:02:03".
pub fn fmt_duration(sec: Option<f64>) -> String {
    let Some(s) = sec else { return "?".into() };
    let total = s.round_ties_even().max(0.0) as u64;
    let (h, m, s) = (total / 3600, (total / 60) % 60, total % 60);
    if h > 0 {
        format!("{h}:{m:02}:{s:02}")
    } else {
        format!("{m}:{s:02}")
    }
}

/// Open the file's folder in Explorer, Finder or the Linux file manager.
pub fn reveal_in_file_manager(path: &str) {
    let mut cmd;
    if cfg!(windows) {
        cmd = realhires::probe::command("explorer");
        cmd.arg(format!("/select,{}", path.replace('/', "\\")));
    } else if cfg!(target_os = "macos") {
        cmd = realhires::probe::command("open");
        cmd.args(["-R", path]);
    } else {
        cmd = realhires::probe::command("xdg-open");
        let dir = realhires::paths::parent(path);
        cmd.arg(if dir.is_empty() { ".".to_string() } else { dir });
    }
    let _ = cmd.spawn();
}

/// Open the file with its default application.
pub fn open_with_default_app(path: &str) {
    let mut cmd = realhires::probe::command(if cfg!(windows) {
        "explorer"
    } else if cfg!(target_os = "macos") {
        "open"
    } else {
        "xdg-open"
    });
    cmd.arg(path);
    let _ = cmd.spawn();
}

// ------------------------------------------------------ the player's look ---

/// The palette of the theme in use.
pub fn pal(ui: &Ui) -> Palette {
    look::current(ui.ctx())
}

/// A verdict's colour for text on the page: the verdict colours as they are on
/// a light theme, lightened on a dark one so they stay readable.
pub fn on_page(c: Color32, pal: &Palette) -> Color32 {
    if pal.dark {
        mix(c, Color32::WHITE, 0.4)
    } else {
        c
    }
}

/// The colour of a verdict's text (see `on_page`).
pub fn verdict_text(v: Verdict, pal: &Palette) -> Color32 {
    on_page(pal.verdict(v), pal)
}

/// Amber for notes and warnings, readable on light and dark themes.
pub fn warn_color(pal: &Palette) -> Color32 {
    if pal.dark {
        hex(0xe0a541)
    } else {
        hex(0x8a5a00)
    }
}

/// Red for errors and empty sources, readable on light and dark themes.
pub fn error_color(pal: &Palette) -> Color32 {
    on_page(hex(0xc62828), pal)
}

/// Green for what went well, readable on light and dark themes.
pub fn ok_color(pal: &Palette) -> Color32 {
    on_page(hex(0x2e7d32), pal)
}

/// The fill of a flat button: the surface (LCD panel) colour, or the page
/// colour when the button sits on a surface-coloured panel.
pub fn button_fill(pal: &Palette, on_surface: bool) -> Color32 {
    if on_surface {
        pal.bg
    } else {
        pal.lcd
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ButtonLook {
    Flat { on_surface: bool },
    Primary,
}

fn draw_button(ui: &mut Ui, icon: Option<Icon>, label: &str, enabled: bool, kind: ButtonLook, height: f32) -> Response {
    let pal = pal(ui);
    let primary = kind == ButtonLook::Primary;
    let (size, pad) = if height < 24.0 {
        (12.0, 9.0)
    } else if height < 28.0 {
        (12.5, 9.5)
    } else {
        (13.0, 13.0)
    };
    let font = if primary { look::title(size) } else { look::text(size) };
    let text_color = match (primary, enabled) {
        (true, true) => pal.on_accent,
        (true, false) => Color32::from_white_alpha(200),
        (false, true) => pal.text,
        (false, false) => pal.secondary.gamma_multiply(0.7),
    };
    let galley = ui.painter().layout_no_wrap(label.to_string(), font, text_color);
    let icon_w = if icon.is_some() { 20.0 } else { 0.0 };
    let w = (galley.size().x + 2.0 * pad + icon_w).max(if primary { 76.0 } else { 0.0 });
    let sense = if enabled { Sense::click() } else { Sense::hover() };
    let (rect, resp) = ui.allocate_exact_size(vec2(w, height), sense);
    resp.widget_info(|| WidgetInfo::labeled(Role::Button, enabled, label));
    if ui.is_rect_visible(rect) {
        let pressed = enabled && resp.is_pointer_button_down_on();
        let hovered = enabled && resp.hovered();
        let fill = match kind {
            ButtonLook::Primary => {
                if !enabled {
                    pal.accent.gamma_multiply(0.45)
                } else if pressed {
                    mix(pal.accent, Color32::BLACK, 0.2)
                } else if hovered {
                    mix(pal.accent, Color32::BLACK, 0.1)
                } else {
                    pal.accent
                }
            }
            ButtonLook::Flat { on_surface } => {
                let base = button_fill(&pal, on_surface);
                if pressed {
                    mix(base, pal.text, 0.14)
                } else if hovered {
                    mix(base, pal.text, 0.07)
                } else {
                    base
                }
            }
        };
        let p = ui.painter();
        p.rect_filled(rect, CornerRadius::same(7), fill);
        if resp.has_focus() {
            p.rect_stroke(rect, CornerRadius::same(7), Stroke::new(1.5, pal.accent), egui::StrokeKind::Outside);
        }
        let mut x = rect.left() + pad;
        if let Some(i) = icon {
            let c = if primary {
                pal.on_accent
            } else if enabled {
                pal.accent
            } else {
                text_color
            };
            icons::paint(p, Rect::from_center_size(pos2(x + 8.0, rect.center().y), vec2(17.0, 17.0)), i, c);
            x += icon_w;
        }
        p.galley(pos2(x, rect.center().y - galley.size().y / 2.0), galley, text_color);
    }
    resp
}

/// A flat rounded button of the player: the surface colour with the text
/// colour, a little darker while the pointer is on it.
pub fn flat_button(ui: &mut Ui, label: &str, enabled: bool) -> Response {
    draw_button(ui, None, label, enabled, ButtonLook::Flat { on_surface: false }, 28.0)
}

/// The same on a surface-coloured panel (it is filled with the page colour).
pub fn panel_button(ui: &mut Ui, label: &str, enabled: bool) -> Response {
    draw_button(ui, None, label, enabled, ButtonLook::Flat { on_surface: true }, 26.0)
}

/// A flat button with an icon in the accent colour before its text.
pub fn icon_text_button(ui: &mut Ui, icon: Icon, label: &str, enabled: bool) -> Response {
    draw_button(ui, Some(icon), label, enabled, ButtonLook::Flat { on_surface: false }, 28.0)
}

/// A small flat button (Remove, Re-scan them); `on_surface` as in `panel_button`.
pub fn small_button(ui: &mut Ui, label: &str, enabled: bool, on_surface: bool) -> Response {
    draw_button(ui, None, label, enabled, ButtonLook::Flat { on_surface }, 22.0)
}

/// A flat button holding only an icon (the More button's "…"), in the text colour.
pub fn flat_icon_button(ui: &mut Ui, icon: Icon, tip: &str) -> Response {
    let pal = pal(ui);
    let (rect, resp) = ui.allocate_exact_size(vec2(34.0, 28.0), Sense::click());
    resp.widget_info(|| WidgetInfo::labeled(Role::Button, true, tip));
    if ui.is_rect_visible(rect) {
        let fill = if resp.is_pointer_button_down_on() {
            mix(pal.lcd, pal.text, 0.14)
        } else if resp.hovered() {
            mix(pal.lcd, pal.text, 0.07)
        } else {
            pal.lcd
        };
        ui.painter().rect_filled(rect, CornerRadius::same(7), fill);
        icons::paint(ui.painter(), Rect::from_center_size(rect.center(), vec2(22.0, 22.0)), icon, pal.text);
    }
    resp.on_hover_text(tip)
}

/// The primary action: filled with the accent colour, white bold text.
pub fn primary_button(ui: &mut Ui, label: &str, enabled: bool) -> Response {
    draw_button(ui, None, label, enabled, ButtonLook::Primary, 28.0)
}

/// A segmented control (the view switch, the dashboard's plot switch), drawn
/// like the Equalizer window's Graphic / Parametric tabs: the chosen segment
/// filled with the accent colour.  Returns the segment clicked, if it is not
/// the chosen one already.
pub fn segmented(ui: &mut Ui, id: &str, labels: &[&str], selected: usize, on_surface: bool) -> Option<usize> {
    let pal = pal(ui);
    let widths: Vec<f32> = labels
        .iter()
        .map(|l| ui.painter().layout_no_wrap(l.to_string(), look::title(13.0), pal.text).size().x + 26.0)
        .collect();
    let (h, pad) = (28.0, 2.0);
    let total = widths.iter().sum::<f32>() + 2.0 * pad;
    let (rect, _) = ui.allocate_exact_size(vec2(total, h), Sense::hover());
    let track = button_fill(&pal, on_surface);
    ui.painter().rect_filled(rect, CornerRadius::same(8), track);
    let mut x = rect.left() + pad;
    let mut clicked = None;
    for (i, (label, w)) in labels.iter().zip(&widths).enumerate() {
        let seg = Rect::from_min_size(pos2(x, rect.top() + pad), vec2(*w, h - 2.0 * pad));
        let sel = i == selected;
        let resp = ui.interact(seg, ui.scope_id().with((id, i)), Sense::click());
        resp.widget_info(|| WidgetInfo::selected(Role::Tab, true, sel, *label));
        let p = ui.painter();
        if sel {
            p.rect_filled(seg, CornerRadius::same(6), pal.accent);
        } else if resp.hovered() {
            p.rect_filled(seg, CornerRadius::same(6), mix(track, pal.text, 0.08));
        }
        let (font, color) = if sel { (look::title(13.0), pal.on_accent) } else { (look::text(13.0), pal.text) };
        p.text(seg.center(), Align2::CENTER_CENTER, *label, font, color);
        if resp.clicked() && !sel {
            clicked = Some(i);
        }
        x += w;
    }
    clicked
}

/// A pill-shaped toggle with a dot in `dot`'s colour and a count ("Fake
/// lossless 3"): filled with the surface colour when on, outlined and grey
/// when off.
pub fn chip(ui: &mut Ui, label: &str, count: Option<usize>, dot: Color32, on: bool) -> Response {
    let pal = pal(ui);
    let text_color = if on { pal.text } else { pal.secondary };
    let name = ui.painter().layout_no_wrap(label.to_string(), look::text(12.5), text_color);
    let num = count.map(|n| ui.painter().layout_no_wrap(n.to_string(), look::title(12.5), text_color));
    let w = 26.0 + name.size().x + num.as_ref().map(|g| g.size().x + 5.0).unwrap_or(0.0) + 12.0;
    let (rect, resp) = ui.allocate_exact_size(vec2(w, 26.0), Sense::click());
    let text = match count {
        Some(n) => format!("{label} {n}"),
        None => label.to_string(),
    };
    resp.widget_info(|| WidgetInfo::selected(Role::CheckBox, true, on, &text));
    if ui.is_rect_visible(rect) {
        let p = ui.painter();
        let r = CornerRadius::same(13);
        if on {
            let fill = if resp.hovered() { mix(pal.lcd, pal.text, 0.07) } else { pal.lcd };
            p.rect_filled(rect, r, fill);
        } else {
            let stroke = if resp.hovered() { pal.secondary } else { pal.separator };
            p.rect_stroke(rect, r, Stroke::new(1.0, stroke), egui::StrokeKind::Inside);
        }
        p.circle_filled(
            pos2(rect.left() + 14.0, rect.center().y),
            4.5,
            if on { dot } else { mix(dot, pal.secondary, 0.45) },
        );
        let x = rect.left() + 26.0;
        let nw = name.size().x;
        p.galley(pos2(x, rect.center().y - name.size().y / 2.0), name, text_color);
        if let Some(g) = num {
            p.galley(pos2(x + nw + 5.0, rect.center().y - g.size().y / 2.0), g, text_color);
        }
    }
    resp
}

/// A switch (the Quick scan option): a rounded track, accent when on, with
/// its label after it.  Returns true when it was flipped.
pub fn switch(ui: &mut Ui, on: &mut bool, label: &str) -> bool {
    let pal = pal(ui);
    let galley = ui.painter().layout_no_wrap(label.to_string(), look::text(13.0), pal.text);
    let (tw, th) = (34.0, 20.0);
    let (rect, resp) = ui.allocate_exact_size(vec2(tw + 8.0 + galley.size().x, 28.0), Sense::click());
    if resp.clicked() {
        *on = !*on;
    }
    let state = *on;
    resp.widget_info(|| WidgetInfo::selected(Role::Switch, true, state, label));
    if ui.is_rect_visible(rect) {
        let t = ui.ctx().animate_bool_responsive(resp.id, state);
        let track = Rect::from_min_size(pos2(rect.left(), rect.center().y - th / 2.0), vec2(tw, th));
        let off = mix(pal.separator, pal.text, if resp.hovered() { 0.12 } else { 0.0 });
        let p = ui.painter();
        p.rect_filled(track, CornerRadius::same(10), mix(off, pal.accent, t));
        let x = track.left() + th / 2.0 + t * (tw - th);
        p.circle_filled(pos2(x, track.center().y + 0.7), th / 2.0 - 2.0, Color32::from_black_alpha(45));
        p.circle_filled(pos2(x, track.center().y), th / 2.0 - 2.0, Color32::WHITE);
        p.galley(pos2(track.right() + 8.0, rect.center().y - galley.size().y / 2.0), galley, pal.text);
    }
    resp.clicked()
}

/// A row of buttons at the right edge (the first one added is the rightmost),
/// only as tall as the buttons.
pub fn buttons_right<R>(ui: &mut Ui, add: impl FnOnce(&mut Ui) -> R) -> R {
    ui.horizontal(|ui| {
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            ui.spacing_mut().item_spacing.x = 8.0;
            add(ui)
        })
        .inner
    })
    .inner
}

/// A thin progress bar: the accent colour on a track in the separator colour.
pub fn progress(ui: &mut Ui, frac: f32, width: f32) -> Response {
    let pal = pal(ui);
    let (rect, resp) = ui.allocate_exact_size(vec2(width, 5.0), Sense::hover());
    ui.painter().rect_filled(rect, CornerRadius::same(3), pal.separator);
    let frac = frac.clamp(0.0, 1.0);
    if frac > 0.0 {
        let done = Rect::from_min_size(rect.min, vec2((rect.width() * frac).max(rect.height()), rect.height()));
        ui.painter().rect_filled(done, CornerRadius::same(3), pal.accent);
    }
    resp
}

/// "Back to …" with the player's back chevron, in the accent colour.
pub fn back_link(ui: &mut Ui, label: &str) -> Response {
    let pal = pal(ui);
    let galley = ui.painter().layout_no_wrap(label.to_string(), look::text(13.0), pal.accent);
    let (rect, resp) = ui.allocate_exact_size(vec2(galley.size().x + 22.0, 24.0), Sense::click());
    resp.widget_info(|| WidgetInfo::labeled(Role::Link, true, label));
    let c = if resp.hovered() { mix(pal.accent, pal.text, 0.25) } else { pal.accent };
    icons::paint(
        ui.painter(),
        Rect::from_center_size(pos2(rect.left() + 8.0, rect.center().y), vec2(16.0, 16.0)),
        Icon::Back,
        c,
    );
    ui.painter().galley(pos2(rect.left() + 18.0, rect.center().y - galley.size().y / 2.0), galley, c);
    resp
}

/// A rounded verdict badge: the label in white on the verdict's colour.
pub fn badge(ui: &mut Ui, text: &str, fill: Color32) -> Response {
    let galley = ui.painter().layout_no_wrap(text.to_string(), look::title(12.0), Color32::WHITE);
    let (rect, resp) = ui.allocate_exact_size(vec2(galley.size().x + 22.0, 24.0), Sense::hover());
    ui.painter().rect_filled(rect, CornerRadius::same(12), fill);
    ui.painter().galley(rect.center() - galley.size() / 2.0, galley, Color32::WHITE);
    resp
}

/// The heading of a column or a block: bold, secondary text.
pub fn heading_text(text: &str, pal: &Palette) -> egui::RichText {
    egui::RichText::new(text).font(look::title(11.5)).color(pal.secondary)
}

/// A heading inside a window, in the heading font.
pub fn window_heading(text: &str, pal: &Palette) -> egui::RichText {
    egui::RichText::new(text).font(look::title(16.0)).color(pal.text)
}

/// Body text in the secondary colour.
pub fn secondary(text: impl Into<String>, pal: &Palette) -> egui::RichText {
    egui::RichText::new(text).color(pal.secondary)
}
