//! Up Next's song rows (2.3), shared by the Up Next panel and Now Playing's Up
//! Next view: a double-click plays the song, a press with the primary button
//! becomes a drag after `DRAG_START` pixels (a line in the accent colour with a
//! small circle at its left end shows the gap the song drops into, and the list
//! scrolls while the pointer is near its top or bottom edge), and a right click
//! opens a menu with "Remove from Up Next".  The caller applies the returned
//! action with `Player::jump_up_next()`, `move_in_up_next()` or
//! `remove_from_up_next()`, which set the gapless follower again.

use egui::{pos2, vec2, CornerRadius, Painter, Rect, Sense, Stroke, Ui};
use realhires::tr;

use crate::look::Palette;

/// Pixels the pointer must move before a press becomes a drag.
pub const DRAG_START: f32 = 5.0;
/// Pixels from the top or bottom edge where a drag scrolls the list.
const EDGE: f32 = 18.0;

/// What the user did with a row (an index into the rows shown).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UpNextAction {
    /// double-click: play it now
    Jump(usize),
    /// dragged from the first place to the second (counted after the move)
    Move(usize, usize),
    /// right click → Remove from Up Next
    Remove(usize),
}

/// The id of row `i` of the list `list` ("panel" or "now-playing"; the tests find the rows by it).
pub fn row_id(list: &str, i: usize) -> egui::Id {
    egui::Id::unique(("up-next-row", list, i))
}

/// Draw `count` rows of `row_h` pixels (the width of the list less `trim`), each
/// painted by `paint(painter, rect, index)` over its hover background, inside the
/// caller's scroll area.  Returns what the user did this frame, if anything.
pub fn rows(
    ui: &mut Ui,
    pal: &Palette,
    list: &'static str,
    count: usize,
    row_h: f32,
    trim: f32,
    mut paint: impl FnMut(&Painter, Rect, usize),
) -> Option<UpNextAction> {
    let key = egui::Id::unique(("up-next-drag", list));
    let mut dragging: Option<usize> = ui.data(|d| d.get_temp::<usize>(key)).filter(|i| *i < count);
    let (origin, at, down, released) = ui.input(|i| {
        (i.pointer.press_origin(), i.pointer.interact_pos(), i.pointer.primary_down(), i.pointer.primary_released())
    });
    let mut rects: Vec<Rect> = Vec::with_capacity(count);
    let mut action = None;
    for i in 0..count {
        let (_, rect) = ui.allocate_space(vec2((ui.available_width() - trim).max(1.0), row_h));
        let resp = ui.interact(rect, row_id(list, i), Sense::click_and_drag());
        rects.push(rect);
        // only the primary button drags, and only after the pointer has moved DRAG_START pixels
        if dragging.is_none() && down && resp.is_pointer_button_down_on() {
            if let (Some(o), Some(p)) = (origin, at) {
                if rect.contains(o) && o.distance(p) >= DRAG_START {
                    dragging = Some(i);
                }
            }
        }
        if dragging == Some(i) || (dragging.is_none() && resp.hovered()) {
            ui.painter().rect_filled(rect, CornerRadius::same(6), pal.row_hover);
        }
        paint(&ui.painter_at(rect), rect, i);
        if resp.double_clicked() && dragging.is_none() {
            action = Some(UpNextAction::Jump(i));
        }
        resp.context_menu(|ui| {
            if ui.button(tr!("Remove from Up Next")).clicked() {
                action = Some(UpNextAction::Remove(i));
                ui.close();
            }
        });
    }
    if let (Some(src), Some(at)) = (dragging, at) {
        // the gap nearest to the pointer: 0 = above the first song
        let gap = rects.iter().filter(|r| r.center().y < at.y).count();
        if released || !down {
            let dst = if gap <= src { gap } else { gap - 1 };
            if dst != src {
                action = Some(UpNextAction::Move(src, dst));
            }
            dragging = None;
        } else if let (Some(first), Some(last)) = (rects.first(), rects.last()) {
            let clip = ui.clip_rect();
            let y = rects.get(gap).map_or(last.bottom(), |r| r.top()).clamp(clip.top() + 2.0, clip.bottom() - 2.0);
            let stroke = Stroke::new(2.0, pal.accent);
            let p = ui.painter();
            p.line_segment([pos2(first.left() + 10.0, y), pos2(first.right() - 2.0, y)], stroke);
            p.circle_stroke(pos2(first.left() + 5.0, y), 4.0, stroke);
            // near an edge the list scrolls along
            if at.y < clip.top() + EDGE {
                ui.scroll_with_delta(vec2(0.0, row_h / 4.0));
            } else if at.y > clip.bottom() - EDGE {
                ui.scroll_with_delta(vec2(0.0, -row_h / 4.0));
            }
            ui.ctx().set_cursor_icon(egui::CursorIcon::Grabbing);
            ui.ctx().request_repaint();
        }
    } else {
        dragging = None;
    }
    ui.data_mut(|d| {
        if let Some(i) = dragging {
            d.insert_temp(key, i);
        } else {
            d.remove::<usize>(key);
        }
    });
    action
}
