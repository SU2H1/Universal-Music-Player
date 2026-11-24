//! The dashboard's spectrogram and average-spectrum views.

use egui::{Align2, Color32, FontId, Pos2, Rect, Sense, Shape, Stroke, TextureHandle, TextureOptions, Ui, Vec2};
use realhires::analysis::Plot;
use realhires::pyfmt::{f, g};
use realhires::tr;
use realhires::verdict::FileResult;

use crate::style::{fmt_duration, hex, verdict_style};

const L: f32 = 52.0;
const R: f32 = 12.0;
const T: f32 = 24.0;
const B: f32 = 28.0;

/// The spectrogram image of the selected file, rendered at the size shown.
#[derive(Default)]
pub struct SpectroTexture {
    key: Option<(String, u32, u32, String, u32)>,
    tex: Option<TextureHandle>,
}

fn khz_step(nyq: f64) -> u32 {
    if nyq <= 24000.0 {
        2
    } else if nyq <= 50000.0 {
        5
    } else if nyq <= 100000.0 {
        10
    } else {
        20
    }
}

fn message(ui: &Ui, rect: Rect, text: &str, color: Color32) {
    ui.painter().text(rect.center(), Align2::CENTER_CENTER, text, FontId::proportional(13.0), color);
}

pub enum State<'a> {
    Nothing,
    Pending,
    Waiting,
    Shown(&'a Plot),
    Missing,
}

pub fn spectrogram(
    ui: &mut Ui,
    r: Option<&FileResult>,
    state: State,
    cache: &mut SpectroTexture,
    range_db: f64,
    cmap: &str,
) {
    let size = ui.available_size().max(Vec2::new(80.0, 60.0));
    let (rect, _) = ui.allocate_exact_size(size, Sense::hover());
    let p = ui.painter_at(rect);
    p.rect_filled(rect, 0.0, hex(0x1e1e1e));
    let light = hex(0xaaaaaa);
    let (r, plot) = match (r, state) {
        (None, _) | (_, State::Nothing) => {
            return message(ui, rect, tr!("Select a scanned file to see its spectrogram"), light)
        }
        (_, State::Pending) => return message(ui, rect, tr!("Select a scanned file to see its spectrogram"), light),
        (_, State::Waiting) => return message(ui, rect, tr!("Drawing the spectrogram…"), light),
        (_, State::Missing) => return message(ui, rect, tr!("No spectrogram (silent or unreadable file)"), light),
        (Some(r), State::Shown(plot)) => (r, plot),
    };
    let Some(sg) = plot.spectrogram.as_ref() else {
        return message(ui, rect, tr!("No spectrogram (silent or unreadable file)"), light);
    };
    let (pw, ph) = (rect.width() - L - R, rect.height() - T - B);
    if pw < 20.0 || ph < 20.0 {
        return;
    }
    let ppp = ui.ctx().pixels_per_point();
    let (wpx, hpx) = ((pw * ppp).round() as u32, (ph * ppp).round() as u32);
    let key = (r.path.clone(), wpx, hpx, cmap.to_string(), range_db as u32);
    if cache.key.as_ref() != Some(&key) {
        cache.tex = realhires::imaging::spectrogram_rgb(sg, wpx as usize, hpx as usize, range_db, cmap).map(|img| {
            let ci = egui::ColorImage::from_rgb([img.width, img.height], &img.data);
            ui.ctx().load_texture("spectrogram", ci, TextureOptions::NEAREST)
        });
        cache.key = Some(key);
    }
    let area = Rect::from_min_size(rect.min + Vec2::new(L, T), Vec2::new(pw, ph));
    if let Some(tex) = &cache.tex {
        p.image(tex.id(), area, Rect::from_min_max(Pos2::ZERO, Pos2::new(1.0, 1.0)), Color32::WHITE);
    }
    let nyq = sg.nyquist;
    let y_of = |hz: f64| area.bottom() - ph * (hz / nyq) as f32;
    let small = FontId::proportional(10.0);
    let text = hex(0xcccccc);
    let step = khz_step(nyq);
    let mut k = 0;
    while (k * 1000) as f64 <= nyq {
        let y = y_of((k * 1000) as f64);
        p.line_segment([Pos2::new(area.left() - 4.0, y), Pos2::new(area.left(), y)], Stroke::new(1.0, hex(0x666666)));
        p.text(Pos2::new(area.left() - 6.0, y), Align2::RIGHT_CENTER, format!("{k}k"), small.clone(), text);
        k += step;
    }
    let total = sg.shape.0 as f64 * sg.seconds_per_column;
    for frac in [0.0, 0.25, 0.5, 0.75, 1.0] {
        let x = area.left() + frac as f32 * pw;
        p.line_segment([Pos2::new(x, area.bottom()), Pos2::new(x, area.bottom() + 4.0)], Stroke::new(1.0, light));
        p.text(
            Pos2::new(x, area.bottom() + 13.0),
            Align2::CENTER_CENTER,
            fmt_duration(Some(frac * total)),
            small.clone(),
            text,
        );
    }
    let mut marks = vec![];
    if let Some(u) = r.upsampled_from {
        marks.push((
            u as f64 / 2.0,
            hex(0x4fc3f7),
            tr!("{khz} kHz: limit of a {rate} kHz master", khz = g(u as f64 / 2000.0), rate = g(u as f64 / 1000.0)),
        ));
    }
    if let Some(l) = r.lossy_cutoff_hz {
        marks.push((l, hex(0xff6e6e), tr!("{khz} kHz: lossy encoder cut-off", khz = f(l / 1000.0, 1))));
    }
    for (hz, color, label) in marks {
        if 0.0 < hz && hz < nyq {
            let y = y_of(hz);
            p.extend(Shape::dashed_line(
                &[Pos2::new(area.left(), y), Pos2::new(area.right(), y)],
                Stroke::new(2.0, color),
                6.0,
                4.0,
            ));
            p.text(
                Pos2::new(area.right() - 4.0, y - 8.0),
                Align2::RIGHT_CENTER,
                label,
                FontId::proportional(12.0),
                color,
            );
        }
    }
    p.text(
        rect.min + Vec2::new(L, 12.0),
        Align2::LEFT_CENTER,
        tr!("Spectrogram (time →, frequency ↑)"),
        FontId::proportional(12.0),
        hex(0xeeeeee),
    );
}

pub fn spectrum(ui: &mut Ui, r: Option<&FileResult>, state: State) {
    let size = ui.available_size().max(Vec2::new(80.0, 60.0));
    let (rect, _) = ui.allocate_exact_size(size, Sense::hover());
    let p = ui.painter_at(rect);
    p.rect_filled(rect, 0.0, Color32::WHITE);
    let grey = hex(0x888888);
    let (r, plot) = match (r, state) {
        (Some(r), State::Shown(plot)) if !plot.freqs.is_empty() => (r, plot),
        (None, _) | (_, State::Nothing) | (_, State::Pending) => {
            return message(ui, rect, tr!("Select a scanned file to see its spectrum"), grey)
        }
        (_, State::Waiting) => return message(ui, rect, tr!("Drawing the spectrum…"), grey),
        _ => return message(ui, rect, tr!("No spectrum available"), grey),
    };
    let (pw, ph) = (rect.width() - L - R, rect.height() - T - B);
    if pw < 20.0 || ph < 20.0 {
        return;
    }
    let area = Rect::from_min_size(rect.min + Vec2::new(L, T), Vec2::new(pw, ph));
    let nyq = plot.nyquist;
    let top = ((plot.peak_db + 5.0) as i64).div_euclid(10) * 10 + 10;
    let bottom = (((plot.floor_db.min(plot.threshold_db) - 15.0) as i64).div_euclid(10) * 10).max(top - 200);
    let x_of = |hz: f64| area.left() + pw * (hz / nyq) as f32;
    let y_of =
        |v: f64| area.top() + ph * ((top as f64 - v.clamp(bottom as f64, top as f64)) / (top - bottom) as f64) as f32;
    let small = FontId::proportional(10.0);
    let grid = Stroke::new(1.0, hex(0xeeeeee));
    let step = if top - bottom <= 120 { 10 } else { 20 };
    let mut lvl = top;
    while lvl >= bottom {
        let y = y_of(lvl as f64);
        p.line_segment([Pos2::new(area.left(), y), Pos2::new(area.right(), y)], grid);
        p.text(Pos2::new(area.left() - 6.0, y), Align2::RIGHT_CENTER, lvl.to_string(), small.clone(), hex(0x666666));
        lvl -= step;
    }
    let kstep = khz_step(nyq);
    let mut k = 0;
    while (k * 1000) as f64 <= nyq {
        let x = x_of((k * 1000) as f64);
        p.line_segment([Pos2::new(x, area.top()), Pos2::new(x, area.bottom())], grid);
        p.text(
            Pos2::new(x, area.bottom() + 12.0),
            Align2::CENTER_CENTER,
            format!("{k}k"),
            small.clone(),
            hex(0x666666),
        );
        k += kstep;
    }
    for (i, rf) in [22050.0, 24000.0, 44100.0, 48000.0].iter().enumerate() {
        if *rf < nyq * 0.9 {
            let x = x_of(*rf);
            p.extend(Shape::dashed_line(
                &[Pos2::new(x, area.top()), Pos2::new(x, area.bottom())],
                Stroke::new(1.0, hex(0xaaaaaa)),
                2.0,
                3.0,
            ));
            p.text(
                Pos2::new(x + 3.0, area.bottom() - 8.0 - 12.0 * (i % 2) as f32),
                Align2::LEFT_CENTER,
                format!("{}k", g(rf / 1000.0)),
                small.clone(),
                hex(0x999999),
            );
        }
    }
    let color = verdict_style(r.verdict).1;
    if plot.bandwidth_hz > 0.0 {
        let x = x_of(plot.bandwidth_hz);
        p.extend(Shape::dashed_line(
            &[Pos2::new(x, area.top()), Pos2::new(x, area.bottom())],
            Stroke::new(2.0, color),
            6.0,
            4.0,
        ));
        let (align, dx) =
            if x > area.right() - 130.0 { (Align2::RIGHT_CENTER, -4.0) } else { (Align2::LEFT_CENTER, 4.0) };
        p.text(
            Pos2::new(x + dx, area.top() + 10.0),
            align,
            tr!("bandwidth {khz} kHz", khz = f(plot.bandwidth_hz / 1000.0, 1)),
            FontId::proportional(12.0),
            color,
        );
    }
    let yf = y_of(plot.floor_db);
    p.extend(Shape::dashed_line(
        &[Pos2::new(area.left(), yf), Pos2::new(area.right(), yf)],
        Stroke::new(1.0, hex(0x8250df)),
        4.0,
        4.0,
    ));
    let pts: Vec<Pos2> = plot.freqs.iter().zip(&plot.db).map(|(hz, v)| Pos2::new(x_of(*hz), y_of(*v))).collect();
    if pts.len() >= 2 {
        p.add(Shape::line(pts, Stroke::new(1.0, hex(0x1f2328))));
    }
    p.rect_stroke(area, 0.0, Stroke::new(1.0, hex(0x999999)), egui::StrokeKind::Inside);
    p.text(
        rect.min + Vec2::new(L, 12.0),
        Align2::LEFT_CENTER,
        tr!("Average spectrum (dB per bin)"),
        FontId::proportional(12.0),
        hex(0x333333),
    );
}
