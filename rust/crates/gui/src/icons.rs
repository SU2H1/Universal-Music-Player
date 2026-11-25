//! Vector icons of the player (2.0): drawn with the painter, never emoji or
//! rare Unicode symbols, which several Linux fonts cannot show.

use egui::{pos2, vec2, Color32, Painter, Pos2, Rect, Response, Sense, Shape, Stroke, Ui, Vec2};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Icon {
    Play,
    Pause,
    Next,
    Previous,
    Shuffle,
    Repeat,
    RepeatOne,
    RepeatStop,
    Heart,
    HeartFilled,
    Speaker,
    SpeakerMuted,
    Lyrics,
    /// two corners pointing outwards (Now Playing's Lyrics only, 2.3)
    Expand,
    UpNext,
    Equalizer,
    Clock,
    Person,
    Albums,
    Note,
    Genres,
    Gear,
    Check,
    Search,
    Back,
    ThumbDown,
    More,
    Folder,
    Playlist,
    Calendar,
    Composer,
    Chart,
    Library,
    People,
    ChevronUp,
    ChevronDown,
    Close,
    /// the Library page as tiles (2.3)
    Tiles,
    /// the Library page as a list (2.3)
    ListView,
    /// a custom tab: a funnel (2.3)
    Filter,
    /// the Moods page and its happy cards: a smiling face (2.4)
    Mood,
    /// the Sad and Intense cards: a frowning face (2.4)
    MoodSad,
    /// the tempo cards: a metronome (2.4)
    Tempo,
}

fn heart_points(c: Pos2, s: f32) -> Vec<Pos2> {
    (0..48)
        .map(|i| {
            let t = i as f32 / 48.0 * std::f32::consts::TAU;
            let x = 16.0 * t.sin().powi(3);
            let y = 13.0 * t.cos() - 5.0 * (2.0 * t).cos() - 2.0 * (3.0 * t).cos() - (4.0 * t).cos();
            pos2(c.x + x * s / 34.0, c.y - y * s / 34.0 - s * 0.04)
        })
        .collect()
}

/// A polyline with round joins (egui's miter joins spike at sharp corners).
fn poly(p: &Painter, pts: &[Pos2], stroke: Stroke, closed: bool) {
    let n = pts.len();
    if n < 2 {
        return;
    }
    let segs = if closed { n } else { n - 1 };
    for i in 0..segs {
        p.line_segment([pts[i], pts[(i + 1) % n]], stroke);
    }
    let from = if closed { 0 } else { 1 };
    let to = if closed { n } else { n - 1 };
    for q in &pts[from..to] {
        p.circle_filled(*q, stroke.width / 2.0, stroke.color);
    }
}

fn arrow_head(p: &Painter, tip: Pos2, dir: Vec2, size: f32, color: Color32) {
    let d = dir.normalized();
    let n = vec2(-d.y, d.x);
    let a = tip - d * size + n * size * 0.6;
    let b = tip - d * size - n * size * 0.6;
    p.add(Shape::convex_polygon(vec![tip, a, b], color, Stroke::NONE));
}

/// Draw `icon` in `rect` (square works best).
pub fn paint(p: &Painter, rect: Rect, icon: Icon, color: Color32) {
    let c = rect.center();
    let s = rect.width().min(rect.height());
    let w = (s * 0.09).max(1.3);
    let stroke = Stroke::new(w, color);
    match icon {
        Icon::Play => {
            let h = s * 0.42;
            p.add(Shape::convex_polygon(
                vec![pos2(c.x - h * 0.42, c.y - h * 0.5), pos2(c.x + h * 0.55, c.y), pos2(c.x - h * 0.42, c.y + h * 0.5)],
                color,
                Stroke::NONE,
            ));
        }
        Icon::Pause => {
            let h = s * 0.42;
            for dx in [-0.22f32, 0.22] {
                p.rect_filled(
                    Rect::from_center_size(pos2(c.x + dx * h * 1.2, c.y), vec2(h * 0.22, h)),
                    egui::CornerRadius::same(1),
                    color,
                );
            }
        }
        Icon::Next | Icon::Previous => {
            let h = s * 0.34;
            let dir = if icon == Icon::Next { 1.0 } else { -1.0 };
            for k in [0.0f32, 1.0] {
                let x0 = c.x + dir * (k * h * 0.8 - h * 0.8);
                p.add(Shape::convex_polygon(
                    vec![pos2(x0, c.y - h * 0.5), pos2(x0 + dir * h * 0.8, c.y), pos2(x0, c.y + h * 0.5)],
                    color,
                    Stroke::NONE,
                ));
            }
        }
        Icon::Shuffle => {
            let h = s * 0.32;
            let (l, r) = (c.x - h * 1.15, c.x + h * 0.75);
            let (t, b) = (c.y - h * 0.6, c.y + h * 0.6);
            poly(p, &[pos2(l, b), pos2(c.x - h * 0.55, b), pos2(c.x + h * 0.25, t), pos2(r, t)], stroke, false);
            poly(p, &[pos2(l, t), pos2(c.x - h * 0.55, t), pos2(c.x + h * 0.25, b), pos2(r, b)], stroke, false);
            arrow_head(p, pos2(r + h * 0.45, t), vec2(1.0, 0.0), h * 0.5, color);
            arrow_head(p, pos2(r + h * 0.45, b), vec2(1.0, 0.0), h * 0.5, color);
        }
        Icon::Repeat | Icon::RepeatOne | Icon::RepeatStop => {
            let h = s * 0.3;
            let r = Rect::from_center_size(c, vec2(h * 2.1, h * 1.35));
            poly(
                p,
                &[pos2(r.left(), r.bottom() - h * 0.35), pos2(r.left(), r.top() + h * 0.3), pos2(r.left() + h * 0.3, r.top()), pos2(r.right() - h * 0.55, r.top())],
                stroke,
                false,
            );
            poly(
                p,
                &[pos2(r.right(), r.top() + h * 0.35), pos2(r.right(), r.bottom() - h * 0.3), pos2(r.right() - h * 0.3, r.bottom()), pos2(r.left() + h * 0.55, r.bottom())],
                stroke,
                false,
            );
            arrow_head(p, pos2(r.right() - h * 0.05, r.top()), vec2(1.0, 0.0), h * 0.5, color);
            arrow_head(p, pos2(r.left() + h * 0.05, r.bottom()), vec2(-1.0, 0.0), h * 0.5, color);
            if icon == Icon::RepeatOne {
                let font = egui::FontId::new(s * 0.34, crate::look::bold());
                p.text(c, egui::Align2::CENTER_CENTER, "1", font, color);
            } else if icon == Icon::RepeatStop {
                p.rect_filled(Rect::from_center_size(c, Vec2::splat(h * 0.55)), 1.0, color);
            }
        }
        Icon::Heart | Icon::HeartFilled => {
            let pts = heart_points(c, s * 0.62);
            if icon == Icon::HeartFilled {
                // the heart is not convex: fill it as triangles from its middle
                let mid = pos2(c.x, c.y - s * 0.02);
                let mut mesh = egui::Mesh::default();
                mesh.colored_vertex(mid, color);
                for q in &pts {
                    mesh.colored_vertex(*q, color);
                }
                let n = pts.len() as u32;
                for i in 0..n {
                    mesh.add_triangle(0, 1 + i, 1 + (i + 1) % n);
                }
                p.add(Shape::mesh(mesh));
            } else {
                poly(p, &pts, stroke, true);
            }
        }
        Icon::Speaker | Icon::SpeakerMuted => {
            let h = s * 0.3;
            let x0 = c.x - h * 0.9;
            p.add(Shape::convex_polygon(
                vec![
                    pos2(x0, c.y - h * 0.3),
                    pos2(x0 + h * 0.4, c.y - h * 0.3),
                    pos2(x0 + h * 0.95, c.y - h * 0.8),
                    pos2(x0 + h * 0.95, c.y + h * 0.8),
                    pos2(x0 + h * 0.4, c.y + h * 0.3),
                    pos2(x0, c.y + h * 0.3),
                ],
                color,
                Stroke::NONE,
            ));
            if icon == Icon::SpeakerMuted {
                let x = c.x + h * 0.55;
                let d = h * 0.38;
                p.line_segment([pos2(x - d, c.y - d), pos2(x + d, c.y + d)], stroke);
                p.line_segment([pos2(x - d, c.y + d), pos2(x + d, c.y - d)], stroke);
            } else {
                for (k, rr) in [(0usize, h * 0.55), (1, h * 0.95)] {
                    let _ = k;
                    let pts: Vec<Pos2> = (0..=12)
                        .map(|i| {
                            let a = -0.9 + 1.8 * i as f32 / 12.0;
                            pos2(c.x + h * 0.15 + rr * a.cos(), c.y + rr * a.sin())
                        })
                        .collect();
                    p.add(Shape::line(pts, stroke));
                }
            }
        }
        Icon::Lyrics => {
            let h = s * 0.32;
            let r = Rect::from_center_size(pos2(c.x, c.y - h * 0.1), vec2(h * 2.0, h * 1.5));
            p.rect_stroke(r, egui::CornerRadius::same((h * 0.4) as u8), stroke, egui::StrokeKind::Middle);
            p.add(Shape::convex_polygon(
                vec![pos2(r.left() + h * 0.4, r.bottom() - 1.0), pos2(r.left() + h * 0.9, r.bottom() - 1.0), pos2(r.left() + h * 0.3, r.bottom() + h * 0.5)],
                color,
                Stroke::NONE,
            ));
            for dx in [-0.3f32, 0.3] {
                p.circle_filled(pos2(r.center().x + dx * h, r.center().y), w * 0.9, color);
            }
        }
        Icon::Expand => {
            let (q, k) = (s * 0.32, s * 0.15);
            for d in [-1.0f32, 1.0] {
                let corner = pos2(c.x + d * q, c.y + d * q);
                poly(p, &[pos2(corner.x - d * k, corner.y), corner, pos2(corner.x, corner.y - d * k)], stroke, false);
                p.line_segment([corner, pos2(c.x + d * s * 0.08, c.y + d * s * 0.08)], stroke);
            }
        }
        Icon::UpNext => {
            let h = s * 0.3;
            for k in -1..=1 {
                let y = c.y + k as f32 * h * 0.6;
                p.circle_filled(pos2(c.x - h * 0.85, y), w * 0.8, color);
                p.line_segment([pos2(c.x - h * 0.45, y), pos2(c.x + h, y)], stroke);
            }
        }
        Icon::Equalizer => {
            let h = s * 0.32;
            for (k, knob) in [(-1.0f32, 0.35f32), (0.0, -0.4), (1.0, 0.1)] {
                let x = c.x + k * h * 0.7;
                p.line_segment([pos2(x, c.y - h), pos2(x, c.y + h)], stroke);
                p.circle_filled(pos2(x, c.y + knob * h), w * 1.6, color);
            }
        }
        Icon::Clock => {
            let h = s * 0.34;
            p.circle_stroke(c, h, stroke);
            p.line_segment([c, pos2(c.x, c.y - h * 0.6)], stroke);
            p.line_segment([c, pos2(c.x + h * 0.45, c.y + h * 0.2)], stroke);
        }
        Icon::Person => {
            let h = s * 0.34;
            p.circle_stroke(pos2(c.x, c.y - h * 0.35), h * 0.42, stroke);
            let pts: Vec<Pos2> = (0..=16)
                .map(|i| {
                    let a = std::f32::consts::PI * (1.0 + i as f32 / 16.0);
                    pos2(c.x + h * 0.85 * a.cos(), c.y + h + h * 0.75 * a.sin())
                })
                .collect();
            p.add(Shape::line(pts, stroke));
        }
        Icon::Albums => {
            let h = s * 0.32;
            let r = Rect::from_center_size(pos2(c.x - h * 0.1, c.y + h * 0.1), Vec2::splat(h * 1.6));
            p.rect_stroke(r, 2.0, stroke, egui::StrokeKind::Middle);
            p.line_segment([pos2(r.left() + h * 0.3, r.top() - h * 0.3), pos2(r.right() + h * 0.3, r.top() - h * 0.3)], stroke);
            p.line_segment([pos2(r.right() + h * 0.3, r.top() - h * 0.3), pos2(r.right() + h * 0.3, r.bottom() - h * 0.3)], stroke);
        }
        Icon::Note => {
            let h = s * 0.34;
            let stem = pos2(c.x + h * 0.25, c.y - h);
            p.circle_filled(pos2(c.x - h * 0.15, c.y + h * 0.55), h * 0.38, color);
            p.line_segment([pos2(stem.x, c.y + h * 0.5), stem], stroke);
            p.add(Shape::line(vec![stem, pos2(stem.x + h * 0.55, stem.y + h * 0.25), pos2(stem.x + h * 0.55, stem.y + h * 0.6)], stroke));
        }
        Icon::Genres => {
            let h = s * 0.3;
            for (dx, dy) in [(-1.0f32, -1.0f32), (1.0, -1.0), (-1.0, 1.0), (1.0, 1.0)] {
                p.rect_stroke(
                    Rect::from_center_size(pos2(c.x + dx * h * 0.5, c.y + dy * h * 0.5), Vec2::splat(h * 0.75)),
                    2.0,
                    stroke,
                    egui::StrokeKind::Middle,
                );
            }
        }
        Icon::Gear => {
            let h = s * 0.3;
            for i in 0..8 {
                let a = i as f32 / 8.0 * std::f32::consts::TAU;
                let (cs, sn) = (a.cos(), a.sin());
                p.line_segment([pos2(c.x + cs * h * 0.7, c.y + sn * h * 0.7), pos2(c.x + cs * h * 1.05, c.y + sn * h * 1.05)], Stroke::new(w * 1.6, color));
            }
            p.circle_stroke(c, h * 0.72, stroke);
            p.circle_stroke(c, h * 0.3, stroke);
        }
        Icon::Check => {
            let h = s * 0.32;
            p.circle_stroke(c, h, stroke);
            p.add(Shape::line(vec![pos2(c.x - h * 0.45, c.y), pos2(c.x - h * 0.1, c.y + h * 0.35), pos2(c.x + h * 0.5, c.y - h * 0.35)], stroke));
        }
        Icon::Search => {
            let h = s * 0.3;
            p.circle_stroke(pos2(c.x - h * 0.2, c.y - h * 0.2), h * 0.6, stroke);
            p.line_segment([pos2(c.x + h * 0.25, c.y + h * 0.25), pos2(c.x + h * 0.8, c.y + h * 0.8)], stroke);
        }
        Icon::ThumbDown => {
            let h = s * 0.32;
            // the cuff at the top, the hand below it with the thumb pointing down
            let cuff = Rect::from_min_max(pos2(c.x - h * 0.95, c.y - h), pos2(c.x - h * 0.45, c.y + h * 0.15));
            p.rect_stroke(cuff, 1.0, stroke, egui::StrokeKind::Middle);
            poly(
                p,
                &[
                    pos2(c.x - h * 0.3, c.y - h),
                    pos2(c.x + h * 0.75, c.y - h),
                    pos2(c.x + h * 0.95, c.y - h * 0.1),
                    pos2(c.x + h * 0.85, c.y + h * 0.15),
                    pos2(c.x + h * 0.15, c.y + h * 0.15),
                    pos2(c.x + h * 0.3, c.y + h * 0.75),
                    pos2(c.x + h * 0.05, c.y + h * 1.0),
                    pos2(c.x - h * 0.3, c.y + h * 0.2),
                    pos2(c.x - h * 0.3, c.y - h),
                ],
                stroke,
                false,
            );
        }
        Icon::More => {
            for dx in [-1.0f32, 0.0, 1.0] {
                p.circle_filled(pos2(c.x + dx * s * 0.22, c.y), (s * 0.07).max(1.5), color);
            }
        }
        Icon::Folder => {
            let h = s * 0.32;
            let r = Rect::from_min_max(pos2(c.x - h, c.y - h * 0.55), pos2(c.x + h, c.y + h * 0.75));
            p.rect_stroke(r, 2.0, stroke, egui::StrokeKind::Middle);
            poly(p, &[pos2(r.left(), r.top()), pos2(r.left(), r.top() - h * 0.3), pos2(c.x - h * 0.2, r.top() - h * 0.3), pos2(c.x, r.top())], stroke, false);
        }
        Icon::Playlist => {
            let h = s * 0.3;
            for k in 0..3 {
                let y = c.y - h * 0.8 + k as f32 * h * 0.55;
                p.line_segment([pos2(c.x - h, y), pos2(c.x + h * (if k == 2 { 0.1 } else { 0.6 }), y)], stroke);
            }
            p.circle_filled(pos2(c.x + h * 0.45, c.y + h * 0.75), h * 0.28, color);
            p.line_segment([pos2(c.x + h * 0.7, c.y + h * 0.75), pos2(c.x + h * 0.7, c.y - h * 0.05)], stroke);
            p.line_segment([pos2(c.x + h * 0.7, c.y - h * 0.05), pos2(c.x + h * 1.05, c.y + h * 0.1)], stroke);
        }
        Icon::Calendar => {
            let h = s * 0.32;
            let r = Rect::from_center_size(pos2(c.x, c.y + h * 0.1), vec2(h * 2.0, h * 1.7));
            p.rect_stroke(r, 2.0, stroke, egui::StrokeKind::Middle);
            p.line_segment([pos2(r.left(), r.top() + h * 0.5), pos2(r.right(), r.top() + h * 0.5)], stroke);
            for dx in [-0.5f32, 0.5] {
                p.line_segment([pos2(c.x + dx * h, r.top() - h * 0.25), pos2(c.x + dx * h, r.top() + h * 0.2)], stroke);
            }
        }
        Icon::Composer => {
            // a pen nib
            let h = s * 0.34;
            poly(
                p,
                &[pos2(c.x, c.y + h), pos2(c.x - h * 0.55, c.y - h * 0.1), pos2(c.x - h * 0.3, c.y - h), pos2(c.x + h * 0.3, c.y - h), pos2(c.x + h * 0.55, c.y - h * 0.1)],
                stroke,
                true,
            );
            p.line_segment([pos2(c.x, c.y + h), pos2(c.x, c.y - h * 0.05)], stroke);
            p.circle_filled(pos2(c.x, c.y - h * 0.15), w * 0.9, color);
        }
        Icon::Chart => {
            let h = s * 0.34;
            for (k, hh) in [(-1.0f32, 0.8f32), (0.0, 1.6), (1.0, 1.15)] {
                let x = c.x + k * h * 0.7;
                p.rect_filled(Rect::from_min_max(pos2(x - h * 0.22, c.y + h - hh * h), pos2(x + h * 0.22, c.y + h)), 1.0, color);
            }
        }
        Icon::Library => {
            let h = s * 0.34;
            for k in 0..2 {
                let x = c.x - h * 0.85 + k as f32 * h * 0.55;
                p.line_segment([pos2(x, c.y - h), pos2(x, c.y + h)], Stroke::new(w * 1.3, color));
            }
            p.line_segment([pos2(c.x + h * 0.15, c.y - h * 0.9), pos2(c.x + h * 0.85, c.y + h)], Stroke::new(w * 1.3, color));
        }
        Icon::People => {
            let h = s * 0.3;
            for (dx, k) in [(-0.45f32, 0.85f32), (0.45, 1.0)] {
                let cx = c.x + dx * h;
                p.circle_stroke(pos2(cx, c.y - h * 0.4), h * 0.38 * k, stroke);
                let pts: Vec<Pos2> = (0..=12)
                    .map(|i| {
                        let a = std::f32::consts::PI * (1.0 + i as f32 / 12.0);
                        pos2(cx + h * 0.7 * k * a.cos(), c.y + h * 0.95 + h * 0.6 * k * a.sin())
                    })
                    .collect();
                p.add(Shape::line(pts, stroke));
            }
        }
        Icon::Back => {
            let h = s * 0.3;
            p.add(Shape::line(vec![pos2(c.x + h * 0.3, c.y - h * 0.7), pos2(c.x - h * 0.4, c.y), pos2(c.x + h * 0.3, c.y + h * 0.7)], stroke));
        }
        Icon::ChevronUp | Icon::ChevronDown => {
            let h = s * 0.3;
            let d = if icon == Icon::ChevronUp { -1.0 } else { 1.0 };
            poly(p, &[pos2(c.x - h * 0.75, c.y - d * h * 0.35), pos2(c.x, c.y + d * h * 0.4), pos2(c.x + h * 0.75, c.y - d * h * 0.35)], stroke, false);
        }
        Icon::Close => {
            let h = s * 0.22;
            p.line_segment([pos2(c.x - h, c.y - h), pos2(c.x + h, c.y + h)], stroke);
            p.line_segment([pos2(c.x - h, c.y + h), pos2(c.x + h, c.y - h)], stroke);
        }
        Icon::Tiles => {
            let (q, g) = (s * 0.38, s * 0.07);
            let radius = egui::CornerRadius::same((s * 0.07).round() as u8);
            for (x0, y0) in [(-q, -q), (g, -q), (-q, g), (g, g)] {
                p.rect_filled(Rect::from_min_size(pos2(c.x + x0, c.y + y0), Vec2::splat(q - g)), radius, color);
            }
        }
        Icon::ListView => {
            let q = s * 0.07;
            for dy in [-0.3f32, 0.0, 0.3] {
                let y = c.y + dy * s;
                p.rect_filled(Rect::from_min_max(pos2(c.x - 0.42 * s, y - q), pos2(c.x - 0.28 * s, y + q)), 0.0, color);
                p.line_segment([pos2(c.x - 0.14 * s, y), pos2(c.x + 0.42 * s, y)], stroke);
            }
        }
        Icon::Filter => {
            let pts = [
                pos2(c.x - 0.38 * s, c.y - 0.32 * s),
                pos2(c.x + 0.38 * s, c.y - 0.32 * s),
                pos2(c.x + 0.09 * s, c.y + 0.04 * s),
                pos2(c.x + 0.09 * s, c.y + 0.36 * s),
                pos2(c.x - 0.09 * s, c.y + 0.26 * s),
                pos2(c.x - 0.09 * s, c.y + 0.04 * s),
            ];
            poly(p, &pts, stroke, true);
        }
        Icon::Mood | Icon::MoodSad => {
            p.circle_stroke(c, s * 0.36, stroke);
            for dx in [-0.13f32, 0.13] {
                p.circle_filled(pos2(c.x + dx * s, c.y - 0.11 * s), s * 0.05, color);
            }
            // the mouth: an arc below the eyes, turned up (smiling) or down
            let sad = icon == Icon::MoodSad;
            let pts: Vec<Pos2> = (0..=12)
                .map(|i| {
                    let a = std::f32::consts::PI * (0.15 + 0.7 * i as f32 / 12.0);
                    let (x, y) = (c.x - 0.2 * s * a.cos(), 0.12 * s * a.sin());
                    if sad {
                        pos2(x, c.y + 0.2 * s - y)
                    } else {
                        pos2(x, c.y + 0.05 * s + y)
                    }
                })
                .collect();
            poly(p, &pts, stroke, false);
        }
        Icon::Tempo => {
            // a metronome: its body, the base and the pendulum
            let pts = [
                pos2(c.x - 0.14 * s, c.y - 0.42 * s),
                pos2(c.x + 0.14 * s, c.y - 0.42 * s),
                pos2(c.x + 0.32 * s, c.y + 0.4 * s),
                pos2(c.x - 0.32 * s, c.y + 0.4 * s),
            ];
            poly(p, &pts, stroke, true);
            p.line_segment([pos2(c.x - 0.24 * s, c.y + 0.16 * s), pos2(c.x + 0.24 * s, c.y + 0.16 * s)], stroke);
            p.line_segment([pos2(c.x, c.y + 0.16 * s), pos2(c.x + 0.3 * s, c.y - 0.36 * s)], stroke);
        }
    }
}

/// A round icon button; `active` draws it in the accent colour.
pub fn button(ui: &mut Ui, icon: Icon, size: f32, active: bool, enabled: bool, tip: &str) -> Response {
    let (rect, resp) = ui.allocate_exact_size(Vec2::splat(size), if enabled { Sense::click() } else { Sense::hover() });
    let pal = crate::look::current(ui.ctx());
    if enabled && resp.hovered() {
        ui.painter().circle_filled(rect.center(), size * 0.5, pal.hover);
    }
    let color = if !enabled {
        pal.disabled
    } else if active {
        pal.accent
    } else {
        pal.text
    };
    paint(ui.painter(), rect.shrink(size * 0.12), icon, color);
    if tip.is_empty() {
        resp
    } else {
        resp.on_hover_text(tip)
    }
}
