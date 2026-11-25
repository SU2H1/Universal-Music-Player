//! Custom tabs in the egui player (2.3, `docs/player-spec.md` "6. Custom tabs"), as
//! the Tk player's `player_tabs.py` does them: the tabs of the tabs folder
//! (`smarttabs::load_tabs()`) in the sidebar and on the Library page, a tab's page,
//! its menu (Edit Tab…, Export Tab…, Delete Tab…), and the tab window (`TabWindow`)
//! of New Tab… and Edit Tab….  The folder is read at start, after every change made
//! here, and whenever the Library page or a tab's page is shown (`page_shown()`).  A
//! tab's songs come from `cached_list()`, so they are worked out once per library,
//! likes and tabs, never per frame.

use std::sync::Arc;

use egui::{pos2, vec2, Align2, Color32, CornerRadius, Rect, RichText, Sense, Ui};
use realhires::json::{self, Value};
use realhires::smarttabs::{self, Rule, Tab, TabContext};
use realhires::{i18n, tr};

use crate::icons::{self, Icon};
use crate::library_pages::LibraryEntry;
use crate::look::{self, Palette};
use crate::player::{plural, ConfirmAction, Page, Player};

/// Rules shown before the list of rules scrolls (the window fits a 768-pixel screen).
const RULE_ROWS: f32 = 7.0;
const RULE_ROW_H: f32 = 34.0;

/// Where the sidebar drew the tabs and New Tab…, and the tab page's "…" (for the tests).
#[derive(Default)]
pub struct TabHits {
    pub sidebar: Vec<(String, egui::Rect)>,
    pub new_tab: Option<egui::Rect>,
    pub more: Option<egui::Rect>,
    /// the tab window as drawn last
    pub window: Option<egui::Rect>,
}

/// One rule of the tab window: the rule, and the text typed for a value that is an entry.
#[derive(Clone, Debug)]
pub struct RuleRow {
    pub rule: Rule,
    pub text: String,
}

impl RuleRow {
    pub fn new(rule: Rule) -> RuleRow {
        let text = smarttabs::value_text(&rule.value);
        RuleRow { rule, text }
    }

    /// The value is typed (text and number fields) rather than chosen (Yes/No, Quality).
    pub fn has_entry(&self) -> bool {
        smarttabs::kind_of(&self.rule.field) != "yesno" && smarttabs::value_choices(&self.rule.field).is_none()
    }

    pub fn rule(&self) -> Rule {
        let mut r = self.rule.clone();
        if self.has_entry() {
            r.value = smarttabs::parse_value(&r.field, &self.text);
        }
        r
    }
}

/// New Tab… / Edit Tab…: the name, "Start from" (New Tab… only), match all or any,
/// the rules, Add rule, Sort by with Descending, Limit, a live "N songs match", and
/// Save / Cancel; `check_tab()`'s message is shown under the rules.
pub struct TabWindow {
    /// the tab being changed (None: a new one)
    pub old: Option<Tab>,
    pub name: String,
    /// "Start from": 0 is no template, then `smarttabs::templates()`
    pub template: usize,
    pub match_any: bool,
    pub rules: Vec<RuleRow>,
    pub sort: String,
    pub descending: bool,
    pub limit: String,
    pub error: String,
    /// the live count, and the tab, library and likes it was worked out for
    pub count: Option<usize>,
    count_for: Option<(String, u64, u64)>,
    focus_name: bool,
}

impl TabWindow {
    pub fn new(old: Option<Tab>, taken: &[String]) -> TabWindow {
        let start = old.clone().unwrap_or_else(|| Tab::new(&smarttabs::unique_name(tr!("New Tab"), taken)));
        TabWindow {
            name: start.name.clone(),
            template: 0,
            match_any: start.match_mode == "any",
            rules: start.rules.iter().cloned().map(RuleRow::new).collect(),
            sort: start.sort.clone(),
            descending: start.descending,
            limit: start.limit.to_string(),
            error: String::new(),
            count: None,
            count_for: None,
            focus_name: true,
            old,
        }
    }

    /// The tab as the window shows it (not checked yet).
    pub fn current(&self) -> Value {
        let s = realhires::pyunicode::strip(&self.limit);
        let limit = if s.is_empty() {
            Value::Int(0)
        } else if s.bytes().all(|b| b.is_ascii_digit()) {
            s.parse::<i64>().map(Value::Int).unwrap_or_else(|_| Value::Str(s.to_string()))
        } else {
            Value::Str(s.to_string())
        };
        json::Obj::new()
            .put("format", smarttabs::TAB_FORMAT)
            .put("version", smarttabs::TAB_VERSION)
            .put("name", self.name.as_str())
            .put("match", if self.match_any { "any" } else { "all" })
            .put("rules", Value::Array(self.rules.iter().map(|r| r.rule().to_json()).collect()))
            .put("sort", self.sort.as_str())
            .put("descending", self.descending)
            .put("limit", limit)
            .build()
    }

    /// Start from: a template's rules, match, sort and limit, and its name (0: none).
    pub fn apply_template(&mut self, index: usize, taken: &[String]) {
        let templates = smarttabs::templates();
        let t = if index == 0 { Tab::new("New Tab") } else { templates[index - 1].clone() };
        self.template = index;
        self.name = smarttabs::unique_name(i18n::tr(&t.name), taken);
        self.match_any = t.match_mode == "any";
        self.rules = t.rules.into_iter().map(RuleRow::new).collect();
        self.sort = t.sort;
        self.descending = t.descending;
        self.limit = t.limit.to_string();
        self.focus_name = true;
    }

    pub fn add_rule(&mut self) {
        if self.rules.len() < smarttabs::MAX_RULES {
            self.rules.push(RuleRow::new(smarttabs::new_rule("title")));
        }
    }

    /// Another field for a rule: its operator and value are kept when they fit.
    pub fn set_field(&mut self, i: usize, field: &str) {
        let rule = self.rules[i].rule();
        self.rules[i] = RuleRow::new(smarttabs::change_field(&rule, field));
    }
}

impl Player {
    // ------------------------------------------------------------- the folder ---

    /// Read the tabs folder again; true (and everything showing tabs drawn again) when
    /// its tabs changed.
    pub fn reload_tabs(&mut self) -> bool {
        let (tabs, problems) = smarttabs::load_tabs(None);
        let changed = tabs != self.tabs || problems != self.tab_problems;
        if changed {
            self.tabs = tabs;
            self.tab_problems = problems;
            self.tabs_gen += 1;
        }
        changed
    }

    pub fn tab(&self, name: &str) -> Option<&Tab> {
        self.tabs.iter().find(|t| t.name == name)
    }

    pub fn tab_names(&self) -> Vec<String> {
        self.tabs.iter().map(|t| t.name.clone()).collect()
    }

    pub fn tab_context(&self) -> TabContext {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
        TabContext::new(&self.state.liked, &self.state.disliked, now).with_moods(self.moods.map.clone())
    }

    /// The songs of a tab in its order: worked out once per library, likes and tabs (and
    /// every hour, for "Days since added").
    pub fn tab_list(&mut self, name: &str) -> Arc<Vec<String>> {
        let Some(tab) = self.tab(name).cloned() else { return Arc::new(vec![]) };
        let hour = self.tab_context().now as u64 / 3600;
        let id = format!("tab:{}:{}:{hour}:{name}", self.tabs_gen, self.moods.gen);
        self.cached_list(&id, None, |p| smarttabs::tab_songs(&tab, &p.lib.tracks, &p.tab_context()))
    }

    /// The Library page's entries after its fifteen: one per tab, with its songs.
    pub fn tab_library_entries(&mut self) -> Vec<LibraryEntry> {
        let names = self.tab_names();
        names
            .into_iter()
            .map(|name| {
                let n = self.tab_list(&name).len();
                LibraryEntry { icon: Icon::Filter, name: name.clone(), count: plural(n, "song"), page: Page::Tab(name) }
            })
            .collect()
    }

    /// The note under Settings → Library's tab buttons: the files of the tabs folder that
    /// are not tabs, and why.
    pub fn tab_problems_text(&self) -> String {
        if self.tab_problems.is_empty() {
            return String::new();
        }
        let mut lines = vec![tr!("These files in the tabs folder are not used:").to_string()];
        for (file, why) in &self.tab_problems {
            lines.push(tr!("{file}: {why}", file = file, why = i18n::tr_text(why)));
        }
        lines.join("\n")
    }

    // ----------------------------------------------------------- the actions ---

    pub fn new_tab(&mut self) {
        if self.tab_win.is_none() {
            self.tab_win = Some(TabWindow::new(None, &self.tab_names()));
        }
    }

    /// Edit Tab… (one tab window at a time: an open one stays).
    pub fn edit_tab(&mut self, name: &str) {
        if self.tab_win.is_none() {
            if let Some(t) = self.tab(name).cloned() {
                self.tab_win = Some(TabWindow::new(Some(t), &self.tab_names()));
            }
        }
    }

    /// Write a tab made or changed in the tab window; its name as the sidebar shows it.
    pub fn save_tab(&mut self, tab: &Tab, old: Option<&Tab>) -> Result<String, String> {
        let saved = smarttabs::save_tab(tab, None, old.and_then(|o| o.file.as_deref())).map_err(|e| e.0)?;
        if let Some(o) = old {
            if o.name != saved.name {
                self.tab_renamed(&o.name, &saved.name);
            }
        }
        self.reload_tabs();
        Ok(self.tabs.iter().find(|t| t.file == saved.file).map(|t| t.name.clone()).unwrap_or(saved.name))
    }

    fn tab_renamed(&mut self, old: &str, new: &str) {
        // the colour label follows the name (2.3)
        if let Some(color) = self.state.label_of("tab", old).map(str::to_string) {
            if old != new {
                self.state.set_label("tab", new, Some(&color));
                self.state.set_label("tab", old, None);
                self.save_state();
            }
        }
        let (from, to) = (Page::Tab(old.to_string()), Page::Tab(new.to_string()));
        for p in self.back.iter_mut().chain(self.forward.iter_mut()) {
            if *p == from {
                *p = to.clone();
            }
        }
        if self.page == from {
            self.page = to;
        }
    }

    /// Delete a tab's file (after Delete Tab…'s question).
    pub fn delete_tab(&mut self, name: &str) {
        let Some(t) = self.tab(name).cloned() else { return };
        smarttabs::delete_tab(&t);
        if self.state.label_of("tab", name).is_some() {
            // a new tab of that name starts without one
            self.state.set_label("tab", name, None);
            self.save_state();
        }
        let gone = Page::Tab(name.to_string());
        self.back.retain(|p| *p != gone);
        self.forward.retain(|p| *p != gone);
        if self.page == gone {
            self.page = Page::Library;
        }
        self.reload_tabs();
    }

    /// Export Tab…: the tab's file where the user chooses (to share it).
    pub fn export_tab(&mut self, name: &str) {
        let Some(t) = self.tab(name).cloned() else { return };
        let file = rfd::FileDialog::new()
            .add_filter(tr!("{app} tab", app = realhires::APP_NAME), &["json"])
            .set_file_name(smarttabs::tab_file_name(name))
            .save_file();
        if let Some(path) = file {
            if let Err(e) = smarttabs::export_tab(&t, &path) {
                self.scanner.info = Some((tr!("Export the tab").into(), i18n::tr_text(&e.0)));
            }
        }
    }

    /// Import tab… (or a tab file dropped onto the window): copied into the tabs folder (a
    /// taken name gets " 2"), then its page.
    pub fn import_tab_path(&mut self, path: &std::path::Path) {
        match smarttabs::import_tab(path, None) {
            Ok(t) => {
                self.reload_tabs();
                let name = self.tabs.iter().find(|x| x.file == t.file).map(|x| x.name.clone()).unwrap_or(t.name);
                self.go(Page::Tab(name));
            }
            Err(e) => self.scanner.info = Some((tr!("Import a tab").into(), i18n::tr_text(&e.0))),
        }
    }

    pub fn import_tab_dialog(&mut self) {
        let file = rfd::FileDialog::new()
            .add_filter(tr!("{app} tab", app = realhires::APP_NAME), &["json"])
            .add_filter(tr!("All files"), &["*"])
            .pick_file();
        if let Some(path) = file {
            self.import_tab_path(&path);
        }
    }

    /// Open tabs folder (made when it does not exist yet).
    pub fn open_tabs_folder(&mut self) {
        let dir = smarttabs::tabs_folder();
        match std::fs::create_dir_all(&dir) {
            Ok(()) => crate::style::open_with_default_app(&dir.to_string_lossy()),
            Err(e) => {
                self.scanner.info = Some((
                    tr!("Open tabs folder").into(),
                    tr!("The tabs folder {folder} could not be opened: {error}", folder = dir.display(), error = e),
                ))
            }
        }
    }

    /// The menu of a tab (right click in the sidebar, "…" on its page).
    pub fn tab_menu(&mut self, ui: &mut Ui, name: &str) {
        if ui.button(tr!("Edit Tab…")).clicked() {
            self.edit_tab(name);
            ui.close();
        }
        if ui.button(tr!("Export Tab…")).clicked() {
            ui.close();
            self.export_tab(name);
        }
        if ui.button(tr!("Delete Tab…")).clicked() {
            self.confirm = Some(ConfirmAction::DeleteTab(name.to_string()));
            ui.close();
        }
        ui.separator();
        self.label_menu(ui, "tab", name);
    }

    // -------------------------------------------------------------- the page ---

    /// A tab's page: its name, "N songs, M minutes", Play, Shuffle, "…" and its songs.
    pub fn tab_page(&mut self, ui: &mut Ui, pal: &Palette, name: &str) {
        ui.label(RichText::new(name).font(look::title(28.0)).color(pal.text));
        if self.tab(name).is_none() {
            ui.label(RichText::new(tr!("This tab is not in the tabs folder any more.")).color(pal.secondary));
            return;
        }
        let list = self.tab_list(name);
        let secs: f64 = list.iter().filter_map(|p| self.lib.track(p)).map(|t| t.duration.max(0.0)).sum();
        let minutes = realhires::pyfmt::round_int(secs / 60.0).max(0) as usize;
        ui.label(RichText::new(tr!("{a}, {b}", a = plural(list.len(), "song"), b = plural(minutes, "minute"))).color(pal.secondary));
        ui.add_space(10.0);
        ui.horizontal_wrapped(|ui| {
            if !list.is_empty() {
                if pill(ui, pal, Icon::Play, tr!("Play")).clicked() {
                    self.play_list(list.to_vec(), Some(&list[0]), name);
                }
                if pill(ui, pal, Icon::Shuffle, tr!("Shuffle")).clicked() {
                    self.shuffle_list(list.to_vec(), name);
                }
            }
            let r = icons::button(ui, Icon::More, 28.0, false, true, tr!("More"));
            self.tab_hits.more = Some(r.rect);
            egui::Popup::menu(&r).show(|ui| self.tab_menu(ui, name));
        });
        ui.add_space(10.0);
        if list.is_empty() {
            ui.label(RichText::new(tr!("No song matches the rules of this tab. Edit Tab… changes them.")).color(pal.secondary));
            return;
        }
        self.song_table(ui, pal, &list, name, false);
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

// ------------------------------------------------------------------ the window ---

fn combo<'a>(ui: &mut Ui, id: impl std::hash::Hash + std::fmt::Debug, shown: &str, width: f32, items: &[(String, &'a str)]) -> Option<&'a str> {
    let mut chosen = None;
    egui::ComboBox::from_id_salt(id).selected_text(shown).width(width).show_ui(ui, |ui| {
        for (label, key) in items {
            if ui.selectable_label(label == shown, label).clicked() {
                chosen = Some(*key);
            }
        }
    });
    chosen
}

fn text_width(ui: &Ui, items: &[(String, &str)]) -> f32 {
    let font = egui::TextStyle::Button.resolve(ui.style());
    items.iter().map(|(t, _)| ui.ctx().fonts_mut(|f| f.layout_no_wrap(t.clone(), font.clone(), Color32::WHITE).size().x)).fold(40.0, f32::max) + 28.0
}

/// The tab window, once per frame (true while it is open).
pub fn tab_window(p: &mut Player, ctx: &egui::Context, pal: &Palette) -> bool {
    let Some(mut w) = p.tab_win.take() else { return false };
    let taken = p.tab_names();
    let fields: Vec<(String, &str)> = smarttabs::FIELDS.iter().map(|f| (i18n::tr(smarttabs::field_label(f)).to_string(), *f)).collect();
    let mut open = true;
    let mut save = false;
    let mut cancel = false;
    let title = if w.old.is_some() { tr!("Edit Tab") } else { tr!("New Tab") };
    let shown = egui::Window::new(title)
        .id(egui::Id::new("player-tab-window"))
        .open(&mut open)
        .collapsible(false)
        .resizable(false)
        .pivot(Align2::CENTER_CENTER)
        .default_pos(ctx.content_rect().center())
        .show(ctx, |ui| {
            let field_w = text_width(ui, &fields);
            ui.horizontal(|ui| {
                ui.label(RichText::new(tr!("Name")).color(pal.secondary));
                let r = ui.add(egui::TextEdit::singleline(&mut w.name).id(egui::Id::new("tab-name")).desired_width(320.0));
                if std::mem::take(&mut w.focus_name) {
                    r.request_focus();
                }
            });
            if w.old.is_none() {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(tr!("Start from")).color(pal.secondary));
                    let mut items: Vec<(String, &str)> = vec![(tr!("No template").to_string(), "")];
                    let templates = smarttabs::templates();
                    let names: Vec<String> = templates.iter().map(|t| i18n::tr(&t.name).to_string()).collect();
                    for n in &names {
                        items.push((n.clone(), n.as_str()));
                    }
                    let shown = items[w.template].0.clone();
                    let width = text_width(ui, &items);
                    if let Some(key) = combo(ui, "tab-template", &shown, width, &items) {
                        let index = items.iter().position(|(_, k)| *k == key).unwrap_or(0);
                        w.apply_template(index, &taken);
                    }
                });
            }
            ui.add_space(6.0);
            let modes: Vec<(String, &str)> =
                smarttabs::MATCH_MODES.iter().map(|m| (i18n::tr(smarttabs::match_label(m)).to_string(), *m)).collect();
            let shown = modes[usize::from(w.match_any)].0.clone();
            let width = text_width(ui, &modes);
            if let Some(m) = combo(ui, "tab-match", &shown, width, &modes) {
                w.match_any = m == "any";
            }
            ui.add_space(4.0);
            let mut remove = None;
            let mut field_change = None;
            egui::ScrollArea::vertical()
                .id_salt("tab-rules")
                .max_height(RULE_ROW_H * RULE_ROWS)
                .auto_shrink([false, true])
                .show(ui, |ui| {
                    for (i, row) in w.rules.iter_mut().enumerate() {
                        ui.horizontal(|ui| {
                            let f = row.rule.field.clone();
                            let shown = i18n::tr(smarttabs::field_label(&f)).to_string();
                            if let Some(nf) = combo(ui, ("tab-field", i), &shown, field_w, &fields) {
                                if nf != f {
                                    field_change = Some((i, nf.to_string()));
                                }
                            }
                            let ops: Vec<(String, &str)> = smarttabs::ops_for(&f)
                                .iter()
                                .map(|o| (i18n::tr(smarttabs::op_label(&f, o)).to_string(), *o))
                                .collect();
                            let shown = ops.iter().find(|(_, o)| *o == row.rule.op).map(|(l, _)| l.clone()).unwrap_or_default();
                            let width = text_width(ui, &ops);
                            if let Some(op) = combo(ui, ("tab-op", i), &shown, width, &ops) {
                                row.rule.op = op.to_string();
                            }
                            if row.has_entry() {
                                ui.add(egui::TextEdit::singleline(&mut row.text).id(egui::Id::new(("tab-value", i))).desired_width(170.0));
                            } else {
                                let choices: Vec<(String, Value)> = if smarttabs::kind_of(&f) == "yesno" {
                                    vec![(tr!("Yes").to_string(), Value::Bool(true)), (tr!("No").to_string(), Value::Bool(false))]
                                } else {
                                    smarttabs::value_choices(&f)
                                        .unwrap_or(&[])
                                        .iter()
                                        .map(|c| (i18n::tr(c).to_string(), Value::Str(c.to_string())))
                                        .collect()
                                };
                                let keys: Vec<String> = (0..choices.len()).map(|k| k.to_string()).collect();
                                let items: Vec<(String, &str)> = choices.iter().zip(&keys).map(|((l, _), k)| (l.clone(), k.as_str())).collect();
                                let shown = choices.iter().find(|(_, v)| *v == row.rule.value).map(|(l, _)| l.clone()).unwrap_or_default();
                                let width = text_width(ui, &items);
                                if let Some(k) = combo(ui, ("tab-choice", i), &shown, width, &items) {
                                    row.rule.value = choices[k.parse::<usize>().unwrap_or(0)].1.clone();
                                }
                            }
                            if icons::button(ui, Icon::Close, 22.0, false, true, tr!("Remove this rule")).clicked() {
                                remove = Some(i);
                            }
                        });
                    }
                });
            if let Some((i, f)) = field_change {
                w.set_field(i, &f);
            }
            if let Some(i) = remove {
                w.rules.remove(i);
            }
            if w.rules.is_empty() {
                ui.label(RichText::new(tr!("No rules: every song of the library is in this tab.")).small().color(pal.secondary));
            }
            if ui.add_enabled(w.rules.len() < smarttabs::MAX_RULES, egui::Button::new(tr!("Add rule"))).clicked() {
                w.add_rule();
            }
            if !w.error.is_empty() {
                ui.label(RichText::new(i18n::tr_text(&w.error)).color(crate::style::error_color(pal)));
            }
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                ui.label(RichText::new(tr!("Sort by")).color(pal.secondary));
                let shown = i18n::tr(smarttabs::field_label(&w.sort)).to_string();
                if let Some(f) = combo(ui, "tab-sort", &shown, field_w, &fields) {
                    w.sort = f.to_string();
                }
                ui.checkbox(&mut w.descending, tr!("Descending"));
            });
            ui.horizontal(|ui| {
                ui.label(RichText::new(tr!("Limit")).color(pal.secondary));
                ui.add(egui::TextEdit::singleline(&mut w.limit).id(egui::Id::new("tab-limit")).desired_width(60.0));
                ui.label(RichText::new(tr!("songs at most (0: no limit)")).small().color(pal.secondary));
            });
            ui.add_space(6.0);
            // the live count, worked out again only when the tab, the library or the likes change
            let current = w.current();
            let key = (json::dumps(&current, json::Style::DEFAULT), p.lib.gen, p.likes_gen);
            if w.count_for.as_ref() != Some(&key) {
                w.count = smarttabs::check_tab(&current).ok().map(|t| smarttabs::tab_songs(&t, &p.lib.tracks, &p.tab_context()).len());
                if w.count.is_some() {
                    w.error.clear();
                }
                w.count_for = Some(key);
            }
            let count = match w.count {
                Some(1) => tr!("1 song matches").to_string(),
                Some(n) => tr!("{n} songs match", n = realhires::stats::thousands(n as i64)),
                None => String::new(),
            };
            ui.label(RichText::new(count).strong().color(pal.text));
            ui.add_space(8.0);
            ui.horizontal(|ui| {
                if ui.button(RichText::new(tr!("Save")).strong().color(pal.accent)).clicked() {
                    save = true;
                }
                if ui.button(tr!("Cancel")).clicked() {
                    cancel = true;
                }
            });
        });
    p.tab_hits.window = shown.map(|s| s.response.rect);
    let others = p.name_dialog.is_some() || p.confirm.is_some();
    let popup = egui::Popup::is_any_open(ctx);
    if !others && !popup && ctx.input(|i| i.key_pressed(egui::Key::Escape)) {
        cancel = true;
    }
    if !others && !popup && ctx.input(|i| i.key_pressed(egui::Key::Enter)) {
        save = true;
    }
    if !open || cancel {
        return true;
    }
    if save {
        let old = w.old.clone();
        let result = smarttabs::check_tab(&w.current())
            .and_then(|t| {
                let name = smarttabs::check_name(&t.name, &p.tabs, old.as_ref().map(|o| o.name.as_str()).unwrap_or(""))?;
                Ok(Tab { name, ..t })
            })
            .map_err(|e| e.0)
            .and_then(|t| p.save_tab(&t, old.as_ref()));
        match result {
            Ok(name) => {
                if old.is_none() {
                    p.go(Page::Tab(name));
                }
                return true;
            }
            Err(e) => w.error = e,
        }
    }
    p.tab_win = Some(w);
    true
}
