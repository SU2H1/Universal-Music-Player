"""Custom tabs in the Tk player (2.3, docs/player-spec.md "6. Custom tabs"): the
player's side of `smarttabs` (`TabsMixin`, which `PlayerApp` inherits), a tab's
page (`TabPage`) and the window that makes or changes a tab (`TabWindow`).

A tab is a file in the tabs folder (`smarttabs.tabs_folder()`); the folder is read
at start, after every change made here, and whenever the Library page or a tab's
page is shown (`PlayerApp.show_page()` calls `reload_tabs()`), so files copied into
it appear without a restart.  The songs of a tab are worked out once per library,
likes and tabs (`tab_tracks()`, cached), never while drawing or scrolling."""

from __future__ import annotations

import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from . import APP_NAME, gui, i18n, smarttabs
from .i18n import tr, tr_text
from .musiclib import Track
from .player_pages import Page, frame, label, plural, song_table, text_width
from .player_theme import IconButton, PillButton

if TYPE_CHECKING:  # pragma: no cover
    from .player_gui import PlayerApp


def _tab_uses_likes(tab: Optional[dict]) -> bool:
    return bool(tab) and any(r["field"] in ("liked", "disliked") for r in tab.get("rules", []))


# ----------------------------------------------------------- the app's part ---
class TabsMixin:
    """The tabs of the player: read from the tabs folder, shown in the sidebar and on
    the Library page, opened, made, changed, exported, imported and deleted."""

    def _init_tabs(self: "PlayerApp") -> None:
        self.tabs: List[dict] = []
        self.tab_problems: List[tuple] = []   # (file name, why) of the files that are not tabs
        self.tab_window: Optional["TabWindow"] = None
        self._tab_cache: Dict[str, List[Track]] = {}
        self._tab_hour = -1
        try:
            self.tabs, self.tab_problems = smarttabs.load_tabs()
        except Exception:  # noqa: BLE001 - a broken folder must not stop the app
            pass

    def reload_tabs(self: "PlayerApp") -> bool:
        """Read the tabs folder again; True (and everything showing tabs redrawn) when
        its tabs changed."""
        try:
            tabs, problems = smarttabs.load_tabs()
        except Exception:  # noqa: BLE001
            tabs, problems = [], []
        changed = tabs != self.tabs or problems != self.tab_problems
        self.tab_problems = problems
        if changed:
            self.tabs = tabs
            self.tabs_changed()
        return changed

    def tabs_changed(self: "PlayerApp") -> None:
        self._tab_cache.clear()
        self.sidebar.redraw()
        self._refresh_pages({"library", "tab"})
        settings = self.pages.get("settings")
        if settings is not None and self.page_name == "settings" and getattr(settings, "group", "") == "Library":
            self._settings_page_again()

    def tab_cache_clear(self: "PlayerApp") -> None:
        """The library or the likes changed: the tabs' songs are worked out again."""
        if hasattr(self, "_tab_cache"):
            self._tab_cache.clear()

    def tab(self: "PlayerApp", name: Optional[str]) -> Optional[dict]:
        return next((t for t in self.tabs if t["name"] == name), None) if name else None

    def tab_context(self: "PlayerApp") -> smarttabs.TabContext:
        return smarttabs.context(self.state.liked, self.state.disliked, time.time())

    def tab_tracks(self: "PlayerApp", name: Optional[str]) -> List[Track]:
        """The songs of a tab, in its order (worked out once per library, likes and tabs,
        and again every hour, for "Days since added")."""
        tab = self.tab(name)
        if tab is None:
            return []
        hour = int(time.time() // 3600)
        if hour != self._tab_hour:
            self._tab_cache.clear()
            self._tab_hour = hour
        if tab["name"] not in self._tab_cache:
            paths = smarttabs.tab_songs(tab, self.tracks, self.tab_context())
            self._tab_cache[tab["name"]] = [self.by_path[p] for p in paths if p in self.by_path]
        return self._tab_cache[tab["name"]]

    def tab_entries(self: "PlayerApp") -> list:
        """The Library page's entries after its fourteen: one per tab, with its songs."""
        return [(f"tab:{t['name']}", t["name"], "filter", plural(len(self.tab_tracks(t["name"])), "song"),
                 lambda n=t["name"]: self.open_tab(n)) for t in self.tabs]

    def open_tab(self: "PlayerApp", name: str) -> None:
        self.show_page("tab", tab=name)

    def tab_menu(self: "PlayerApp", name: str) -> tk.Menu:
        """The menu of a tab (right click in the sidebar, "…" on its page)."""
        m = tk.Menu(self.root, tearoff=False)
        m.add_command(label=tr("Edit Tab…"), command=lambda: self.edit_tab(name))
        m.add_command(label=tr("Export Tab…"), command=lambda: self.export_tab_file(name))
        m.add_command(label=tr("Delete Tab…"), command=lambda: self.delete_tab(name))
        m.add_separator()
        self.add_label_menu(m, "tab", name)
        self.theme_menu(m)
        self.last_menu = m
        return m

    def new_tab(self: "PlayerApp") -> "TabWindow":
        return self._tab_window(None)

    def edit_tab(self: "PlayerApp", name: str) -> Optional["TabWindow"]:
        tab = self.tab(name)
        return self._tab_window(tab) if tab is not None else None

    def _tab_window(self: "PlayerApp", tab: Optional[dict]) -> "TabWindow":
        w = self.tab_window
        if w is not None and w.win.winfo_exists():  # one at a time
            w.win.lift()
            w.win.focus_force()
            return w
        self.tab_window = TabWindow(self, tab)
        return self.tab_window

    def save_tab(self: "PlayerApp", tab: dict, old: Optional[dict] = None) -> dict:
        """Write a tab made or changed in the tab window (TabError when it cannot be)."""
        saved = smarttabs.save_tab(tab, old_file=old.get("file") if old else None)
        if old is not None and old["name"] != saved["name"]:
            self._tab_renamed(old["name"], saved["name"])
        self.reload_tabs()
        # the name it was read back with (another file may hold that name)
        return next((t for t in self.tabs if t.get("file") == saved["file"]), saved)

    def _tab_renamed(self: "PlayerApp", old: str, new: str) -> None:
        color = self.label_of("tab", old)  # the colour label follows the name (2.3)
        if color and old != new:
            self.state.set_label("tab", new, color)
            self.state.set_label("tab", old, None)
            self.save_state()
        for lst in (self.history, self.forward):
            for i, (name, args) in enumerate(lst):
                if name == "tab" and args.get("tab") == old:
                    lst[i] = (name, dict(args, tab=new))
        page = self.pages.get("tab")
        if isinstance(page, TabPage) and page.name == old:
            page.name = new
            if self.page_name == "tab":
                self.sidebar.select(f"tab:{new}")

    def delete_tab(self: "PlayerApp", name: str, confirm: bool = True) -> bool:
        tab = self.tab(name)
        if tab is None:
            return False
        if confirm and not messagebox.askyesno(tr("Delete Tab"), tr(
                "Delete the tab “{name}”? Its file is deleted from the tabs folder; the songs are not.", name=name),
                parent=self.root):
            return False
        smarttabs.delete_tab(tab)
        if self.label_of("tab", name):  # a new tab of that name starts without one
            self.state.set_label("tab", name, None)
            self.save_state()
        self.history = [h for h in self.history if not (h[0] == "tab" and h[1].get("tab") == name)]
        self.forward = [h for h in self.forward if not (h[0] == "tab" and h[1].get("tab") == name)]
        page = self.pages.get("tab")
        if self.page_name == "tab" and isinstance(page, TabPage) and page.name == name:
            self.show_page("library", remember=False)
        self.reload_tabs()
        return True

    def export_tab_file(self: "PlayerApp", name: str, path: Optional[str] = None) -> Optional[str]:
        """Export Tab…: write the tab's file where the user chooses (to share it)."""
        tab = self.tab(name)
        if tab is None:
            return None
        if path is None:
            path = filedialog.asksaveasfilename(parent=self.root, title=tr("Export the tab"),
                                                initialfile=smarttabs.tab_file_name(name), defaultextension=".json",
                                                filetypes=[(tr("{app} tab", app=APP_NAME), "*.json")])
        if not path:
            return None
        try:
            smarttabs.export_tab(tab, path)
        except smarttabs.TabError as exc:
            messagebox.showerror(tr("Export the tab"), tr_text(str(exc)), parent=self.root)
            return None
        return path

    def import_tab_file(self: "PlayerApp", path: Optional[str] = None) -> Optional[str]:
        """Settings → Library → Import tab…: a tab file copied into the tabs folder (a taken
        name gets " 2"), then its page.  Returns its name."""
        if path is None:
            path = filedialog.askopenfilename(parent=self.root, title=tr("Import a tab"),
                                              filetypes=[(tr("{app} tab", app=APP_NAME), "*.json"),
                                                         (tr("All files"), "*")])
        if not path:
            return None
        try:
            tab = smarttabs.import_tab(path)
        except smarttabs.TabError as exc:
            messagebox.showerror(tr("Import a tab"), tr_text(str(exc)), parent=self.root)
            return None
        self.reload_tabs()
        shown = next((t["name"] for t in self.tabs if t.get("file") == tab["file"]), tab["name"])
        self.open_tab(shown)
        return shown

    def open_tabs_folder(self: "PlayerApp", opener: Optional[Callable[[str], None]] = None) -> str:
        """Settings → Library → Open tabs folder (made when it does not exist yet)."""
        folder = smarttabs.tabs_folder()
        try:
            os.makedirs(folder, exist_ok=True)
            (opener or gui.open_with_default_app)(folder)
        except OSError as exc:
            messagebox.showerror(tr("Open tabs folder"), tr("The tabs folder {folder} could not be opened: {error}",
                                                            folder=folder, error=exc), parent=self.root)
        return folder

    def tab_problems_text(self: "PlayerApp") -> str:
        """The note under Settings → Library's tab buttons: the files of the tabs folder
        that are not tabs, and why."""
        if not self.tab_problems:
            return ""
        lines = [tr("These files in the tabs folder are not used:")]
        lines += [tr("{file}: {why}", file=n, why=tr_text(why)) for n, why in self.tab_problems]
        return "\n".join(lines)


# ------------------------------------------------------------------- the page ---
class TabPage(Page):
    """A tab's page: its name, "N songs, M minutes", Play, Shuffle, "…" (Edit Tab…,
    Export Tab…, Delete Tab…) and its songs in the tab's order."""

    def __init__(self, app: "PlayerApp"):
        super().__init__(app)
        t = self.theme
        self.name: Optional[str] = None
        top, self.title_l, self.sub = self.heading(self, "")
        top.pack(fill="x", padx=28, pady=(22, 8))
        self.more_b = IconButton(top, t, "dots", command=self.show_menu, size=32, icon_size=18,
                                 tooltip=lambda: tr("More"))
        self.more_b.pack(side="right", padx=(0, 10))
        self.table = song_table(self, self, kind="songs", sortable=False,
                                empty_text=tr("No song matches the rules of this tab. Edit Tab… changes them."),
                                on_activate=lambda i: app.play_list(self.table.rows, i, self.list_name))
        self.table.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    def show(self, tab: Optional[str] = None, **kw) -> None:
        if tab is not None:
            self.name = tab
        self.refresh()

    def refresh(self) -> None:
        tab = self.app.tab(self.name)
        self.title_l.configure(text=self.name or "")
        self.list_name = self.name or ""
        if tab is None:
            self.sub.configure(text=tr("This tab is not in the tabs folder any more."))
            self.table.set_rows([])
            return
        rows = self.app.tab_tracks(self.name)
        minutes = int(round(sum(r.duration for r in rows) / 60))
        self.sub.configure(text=tr("{a}, {b}", a=plural(len(rows), "song"), b=plural(minutes, "minute")))
        self.table.set_rows(rows, keep=True)

    def menu(self) -> Optional[tk.Menu]:
        return self.app.tab_menu(self.name) if self.app.tab(self.name) is not None else None

    def show_menu(self) -> None:
        m = self.menu()
        if m is not None:
            b = self.more_b
            m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def songs(self) -> List[Track]:
        return list(self.table.rows)

    def likes_changed(self) -> None:
        if _tab_uses_likes(self.app.tab(self.name)):
            self.refresh()
        else:
            self.table.redraw()

    def song_changed(self) -> None:
        self.table.redraw()


# ----------------------------------------------------------------- the window ---
class _RuleRow:
    """The widgets of one rule: field, operator, value (an entry, or a menu) and remove."""

    def __init__(self, win: "TabWindow", parent, index: int, rule: dict):
        t = win.app.theme
        self.win, self.field = win, rule["field"]
        f = self.field
        self.box = frame(parent, t)
        self.box.grid(row=index, column=0, sticky="we", pady=(0, 6))
        self.field_var = tk.StringVar(value=tr(smarttabs.FIELD_LABELS[f]))
        self.field_cb = ttk.Combobox(self.box, textvariable=self.field_var, values=win.field_names, state="readonly",
                                     width=win.field_width, style="P.TCombobox", font=t.f("body"))
        self.field_cb.pack(side="left")
        self.field_cb.bind("<<ComboboxSelected>>", lambda e: win.field_chosen(self, self.field_cb.current()))
        self.ops = list(smarttabs.ops_for(f))
        op_names = [tr(smarttabs.op_label(f, o)) for o in self.ops]
        self.op_var = tk.StringVar(value=op_names[self.ops.index(rule["op"])] if rule["op"] in self.ops
                                   else op_names[0])
        self.op_cb = ttk.Combobox(self.box, textvariable=self.op_var, values=op_names, state="readonly",
                                  width=max(text_width(n) for n in op_names) + 2, style="P.TCombobox",
                                  font=t.f("body"))
        self.op_cb.pack(side="left", padx=(6, 0))
        self.op_cb.bind("<<ComboboxSelected>>", lambda e: win.changed())
        kind = smarttabs.kind_of(f)
        self.choices: Optional[List] = None
        if kind == "yesno":
            self.choices = [True, False]
            shown = [tr(smarttabs.YES), tr(smarttabs.NO)]
        elif f in smarttabs.VALUE_CHOICES:
            self.choices = list(smarttabs.VALUE_CHOICES[f])
            shown = [i18n.tr(v) for v in self.choices]
        self.value_var = tk.StringVar()
        if self.choices is not None:
            v = rule["value"]
            self.value_var.set(shown[self.choices.index(v)] if v in self.choices else shown[0])
            self.value_w = ttk.Combobox(self.box, textvariable=self.value_var, values=shown, state="readonly",
                                        width=max(text_width(n) for n in shown) + 2, style="P.TCombobox",
                                        font=t.f("body"))
            self.value_w.bind("<<ComboboxSelected>>", lambda e: win.changed())
        else:
            self.value_var.set(smarttabs.value_text(rule["value"]))
            self.value_w = ttk.Entry(self.box, textvariable=self.value_var, width=22, style="P.TEntry",
                                     font=t.f("body"))
            self.value_w.bind("<Return>", lambda e: win.save())
            self.value_w.bind("<KP_Enter>", lambda e: win.save())
            self.value_var.trace_add("write", lambda *a: win.changed())
        self.value_w.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.remove_b = IconButton(self.box, t, "close", command=lambda: win.remove_rule(self), size=26, icon_size=12,
                                   tooltip=lambda: tr("Remove this rule"))
        self.remove_b.pack(side="left", padx=(6, 0))

    def rule(self) -> dict:
        op = self.ops[max(0, self.op_cb.current())]
        if self.choices is not None:
            value = self.choices[max(0, self.value_w.current())]
        else:
            value = smarttabs.parse_value(self.field, self.value_var.get())
        return {"field": self.field, "op": op, "value": value}


class TabWindow:
    """New Tab… / Edit Tab…: the name, "Start from" (the templates, New Tab… only), match
    all or any, the rules (field, an operator that fits it, a value: an entry or Yes/No),
    Add rule, a remove button per rule, Sort by with Descending, Limit, a live "N songs
    match", and Save / Cancel.  check_tab()'s message is shown under the rules and the
    window stays open."""

    RULE_ROWS = 7  # rules shown before the list scrolls (the window fits a 768-pixel screen)
    COUNT_DELAY_MS = 150

    def __init__(self, app: "PlayerApp", tab: Optional[dict]):
        from .player_windows import _window

        self.app, t = app, app.theme
        self.old = tab
        self.win = _window(app, tr("Edit Tab") if tab else tr("New Tab"))
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        self.win.bind("<<Close>>", lambda e: self.cancel())
        self._count_job: Optional[str] = None
        self.rows: List[_RuleRow] = []
        self.field_names = [tr(smarttabs.FIELD_LABELS[f]) for f in smarttabs.FIELDS]
        self.field_width = max(text_width(n) for n in self.field_names) + 2
        names = [x["name"] for x in app.tabs]
        start = dict(tab) if tab else smarttabs.new_tab(smarttabs.unique_name(tr("New Tab"), names))
        box = frame(self.win, t)
        box.pack(fill="both", expand=True, padx=22, pady=18)
        self.box = box
        row = frame(box, t)
        row.pack(fill="x")
        label(row, t, tr("Name"), "body", fg="text2").pack(side="left")
        self.name_var = tk.StringVar(value=start["name"])
        self.name_e = ttk.Entry(row, textvariable=self.name_var, width=36, style="P.TEntry", font=t.f("body"))
        self.name_e.pack(side="left", padx=(10, 0), fill="x", expand=True)
        self.name_e.bind("<Return>", lambda e: self.save())
        self.name_e.bind("<KP_Enter>", lambda e: self.save())
        self.template_cb: Optional[ttk.Combobox] = None
        if tab is None:
            row = frame(box, t)
            row.pack(fill="x", pady=(10, 0))
            label(row, t, tr("Start from"), "body", fg="text2").pack(side="left")
            shown = [tr("No template")] + [tr(x["name"]) for x in smarttabs.TEMPLATES]
            self.template_var = tk.StringVar(value=shown[0])
            self.template_cb = ttk.Combobox(row, textvariable=self.template_var, values=shown, state="readonly",
                                            width=max(text_width(n) for n in shown) + 2, style="P.TCombobox",
                                            font=t.f("body"))
            self.template_cb.pack(side="left", padx=(10, 0))
            self.template_cb.bind("<<ComboboxSelected>>", lambda e: self.template_chosen(self.template_cb.current()))
        match_names = [tr(smarttabs.MATCH_LABELS[m]) for m in ("all", "any")]
        self.match_var = tk.StringVar(value=match_names[1 if start["match"] == "any" else 0])
        self.match_cb = ttk.Combobox(box, textvariable=self.match_var, values=match_names, state="readonly",
                                     width=max(text_width(n) for n in match_names) + 2, style="P.TCombobox",
                                     font=t.f("body"))
        self.match_cb.pack(anchor="w", pady=(14, 8))
        self.match_cb.bind("<<ComboboxSelected>>", lambda e: self.changed())
        # the rules, in a list that scrolls once there are more than RULE_ROWS
        holder = frame(box, t)
        holder.pack(fill="x")
        self.rules_c = tk.Canvas(holder, highlightthickness=0, bd=0, height=10)
        t.reg(self.rules_c, bg="bg")
        self.rules_sb = ttk.Scrollbar(holder, orient="vertical", command=self.rules_c.yview,
                                      style="P.Vertical.TScrollbar")
        self.rules_c.configure(yscrollcommand=self.rules_sb.set)
        self.rules_c.pack(side="left", fill="x", expand=True)
        self.rules_f = frame(self.rules_c, t)
        self.rules_f.columnconfigure(0, weight=1)
        self._rules_item = self.rules_c.create_window(0, 0, window=self.rules_f, anchor="nw")
        self.rules_f.bind("<Configure>", lambda e: self._rules_size())
        self.rules_c.bind("<Configure>", lambda e: self.rules_c.itemconfigure(self._rules_item, width=e.width))
        self.no_rules = label(box, t, tr("No rules: every song of the library is in this tab."), "small",
                              fg="text2")
        add = frame(box, t)
        add.pack(fill="x", pady=(2, 0))
        self.add_b = PillButton(add, t, tr("Add rule"), icon="plus", kind="plain", command=self.add_rule)
        self.add_b.pack(side="left")
        self.error = label(box, t, "", "small", fg="error", wraplength=560, justify="left")
        self.error.pack(anchor="w", pady=(6, 0))
        sort = frame(box, t)
        sort.pack(fill="x", pady=(10, 0))
        label(sort, t, tr("Sort by"), "body", fg="text2").pack(side="left")
        self.sort_var = tk.StringVar(value=tr(smarttabs.FIELD_LABELS.get(start["sort"], "Title")))
        self.sort_cb = ttk.Combobox(sort, textvariable=self.sort_var, values=self.field_names, state="readonly",
                                    width=self.field_width, style="P.TCombobox", font=t.f("body"))
        self.sort_cb.pack(side="left", padx=(10, 0))
        self.sort_cb.bind("<<ComboboxSelected>>", lambda e: self.changed())
        self.desc_var = tk.BooleanVar(value=bool(start["descending"]))
        ttk.Checkbutton(sort, text=tr("Descending"), variable=self.desc_var, style="P.TCheckbutton",
                        command=self.changed).pack(side="left", padx=(12, 0))
        lim = frame(box, t)
        lim.pack(fill="x", pady=(10, 0))
        label(lim, t, tr("Limit"), "body", fg="text2").pack(side="left")
        self.limit_var = tk.StringVar(value=str(start["limit"]))
        self.limit_e = ttk.Entry(lim, textvariable=self.limit_var, width=8, style="P.TEntry", font=t.f("body"))
        self.limit_e.pack(side="left", padx=(10, 0))
        self.limit_var.trace_add("write", lambda *a: self.changed())
        self.limit_e.bind("<Return>", lambda e: self.save())
        self.limit_e.bind("<KP_Enter>", lambda e: self.save())
        label(lim, t, tr("songs at most (0: no limit)"), "small", fg="text2").pack(side="left", padx=(8, 0))
        self.count = label(box, t, "", "body_b")
        self.count.pack(anchor="w", pady=(14, 0))
        btns = frame(box, t)
        btns.pack(fill="x", pady=(14, 0))
        PillButton(btns, t, tr("Save"), command=self.save).pack(side="right")
        PillButton(btns, t, tr("Cancel"), kind="plain", command=self.cancel).pack(side="right", padx=(0, 8))
        self.name_var.trace_add("write", lambda *a: self.changed())
        self._build_rules([dict(r) for r in start["rules"]])
        self.name_e.focus_set()
        self.name_e.select_range(0, "end")
        self._count_now()

    # ---- the rules -------------------------------------------------------
    def _build_rules(self, rules: List[dict]) -> None:
        for r in self.rows:
            r.box.destroy()
        self.rows = [_RuleRow(self, self.rules_f, i, r) for i, r in enumerate(rules)]
        if rules:
            self.no_rules.pack_forget()
        else:
            self.no_rules.pack(anchor="w", before=self.add_b.master)
        self.add_b.set(enabled=len(rules) < smarttabs.MAX_RULES)
        self.win.after_idle(self._rules_size)
        self.changed()

    def _rules_size(self) -> None:
        try:
            self.rules_f.update_idletasks()
        except tk.TclError:
            return
        h = self.rules_f.winfo_reqheight()
        rows = len(self.rows)
        shown = h if rows <= self.RULE_ROWS else h * self.RULE_ROWS / max(rows, 1)
        self.rules_c.configure(height=max(1, int(shown)), width=self.rules_f.winfo_reqwidth(),
                               scrollregion=(0, 0, self.rules_f.winfo_reqwidth(), h))
        if rows > self.RULE_ROWS:
            self.rules_sb.pack(side="right", fill="y")
        else:
            self.rules_sb.pack_forget()
            self.rules_c.yview_moveto(0)

    def rules(self) -> List[dict]:
        return [r.rule() for r in self.rows]

    def add_rule(self) -> None:
        if len(self.rows) >= smarttabs.MAX_RULES:
            return
        self._build_rules(self.rules() + [smarttabs.new_rule("title")])
        self.rules_c.yview_moveto(1.0)
        self.rows[-1].value_w.focus_set()

    def remove_rule(self, row: _RuleRow) -> None:
        rules = self.rules()
        del rules[self.rows.index(row)]
        self._build_rules(rules)

    def field_chosen(self, row: _RuleRow, index: int) -> None:
        if index < 0:
            return
        rules = self.rules()
        i = self.rows.index(row)
        rules[i] = smarttabs.change_field(rules[i], smarttabs.FIELDS[index])
        self._build_rules(rules)

    def template_chosen(self, index: int) -> None:
        """Start from: a template's rules, match, sort and limit, and its name."""
        names = [x["name"] for x in self.app.tabs]
        tpl = smarttabs.TEMPLATES[index - 1] if index > 0 else smarttabs.new_tab(tr("New Tab"))
        self.name_var.set(smarttabs.unique_name(tr(tpl["name"]), names))
        self.match_var.set(self.match_cb["values"][1 if tpl["match"] == "any" else 0])
        self.sort_var.set(tr(smarttabs.FIELD_LABELS[tpl["sort"]]))
        self.desc_var.set(bool(tpl["descending"]))
        self.limit_var.set(str(tpl["limit"]))
        self.name_e.select_range(0, "end")
        self._build_rules([dict(r) for r in tpl["rules"]])

    # ---- the tab ---------------------------------------------------------
    def current(self) -> dict:
        """The tab as the window shows it (not checked yet)."""
        limit_text = self.limit_var.get().strip()
        limit = (int(limit_text) if limit_text.isascii() and limit_text.isdigit() else
                 0 if not limit_text else limit_text)
        sort_i = self.sort_cb.current()
        return {"format": smarttabs.TAB_FORMAT, "version": smarttabs.TAB_VERSION, "name": self.name_var.get(),
                "match": "any" if self.match_cb.current() == 1 else "all", "rules": self.rules(),
                "sort": smarttabs.FIELDS[sort_i] if sort_i >= 0 else "title",
                "descending": bool(self.desc_var.get()), "limit": limit}

    def changed(self) -> None:
        """Something was changed: the count again, a moment later."""
        if self._count_job is not None:
            try:
                self.win.after_cancel(self._count_job)
            except tk.TclError:
                pass
        try:
            self._count_job = self.win.after(self.COUNT_DELAY_MS, self._count_now)
        except tk.TclError:
            self._count_job = None

    def _count_now(self) -> None:
        self._count_job = None
        try:
            tab = smarttabs.check_tab(self.current())
        except smarttabs.TabError:
            self.count.configure(text="")
            return
        self.error.configure(text="")
        n = len(smarttabs.tab_songs(tab, self.app.tracks, self.app.tab_context()))
        self.count.configure(text=tr("1 song matches") if n == 1 else tr("{n} songs match", n=f"{n:,}"))

    def save(self) -> None:
        try:
            tab = smarttabs.check_tab(self.current())
            tab["name"] = smarttabs.check_name(tab["name"], self.app.tabs,
                                               renaming=self.old["name"] if self.old else "")
            saved = self.app.save_tab(tab, self.old)
        except smarttabs.TabError as exc:
            self.error.configure(text=tr_text(str(exc)))
            return
        self.close()
        if self.old is None:
            self.app.open_tab(saved["name"])

    def cancel(self) -> None:
        self.close()

    def close(self) -> None:
        from .player_windows import close_window

        if self._count_job is not None:
            try:
                self.win.after_cancel(self._count_job)
            except tk.TclError:
                pass
            self._count_job = None
        if self.app.tab_window is self:
            self.app.tab_window = None
        close_window(self.win)


__all__ = ["TabsMixin", "TabPage", "TabWindow"]
