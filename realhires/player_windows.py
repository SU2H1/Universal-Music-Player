"""The 2.0 player's windows: Playback info (track, decoder, output, quality check),
the equalizer (graphic and parametric), and Edit Info (a song's information)."""

from __future__ import annotations

import copy
import math
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from . import eq, i18n, tagedit
from .i18n import N_, tr, tr_name, tr_text
from .player import fmt_db
from .player_theme import PillButton, Slider
from .player_pages import frame, label

if TYPE_CHECKING:  # pragma: no cover
    from .player_gui import PlayerApp


def close_window(w: tk.Toplevel) -> None:
    """Destroy a window of the player.  Its pending idle work (geometry, mapping) is
    run first: Tk on macOS crashed (SIGSEGV in TclServiceIdle, during a redraw) after
    the name dialog was destroyed with idle work still queued for it."""
    try:
        w.grab_release()
        w.update_idletasks()
        w.destroy()
    except tk.TclError:
        pass


def _window(app: "PlayerApp", title: str) -> tk.Toplevel:
    w = tk.Toplevel(app.root)
    w.title(title)
    w.transient(app.root)
    app.theme.reg(w, bg="bg")
    w.bind("<Escape>", lambda e: w.event_generate("<<Close>>"))
    return w


class NameDialog:
    """Asks for a name (playlist, preset, theme); `check(name)` returns the cleaned
    name or raises an error whose message is shown under the field."""

    def __init__(self, app: "PlayerApp", title: str, prompt: str, initial: str = "",
                 check: Optional[Callable[[str], str]] = None, ok: str = N_("OK"), parent=None):
        self.app, t = app, app.theme
        self.check = check
        self.result: Optional[str] = None
        self.win = _window(app, title)
        if parent is not None:
            self.win.transient(parent)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        self.win.bind("<<Close>>", lambda e: self.cancel())
        box = frame(self.win, t)
        box.pack(fill="both", expand=True, padx=22, pady=18)
        label(box, t, prompt, "body_b").pack(anchor="w")
        self.var = tk.StringVar(value=initial)
        self.entry = ttk.Entry(box, textvariable=self.var, width=40, style="P.TEntry", font=t.f("body"))
        self.entry.pack(anchor="w", fill="x", pady=(6, 0))
        self.error = label(box, t, "", "small", fg="error", wraplength=360, justify="left")
        self.error.pack(anchor="w", pady=(4, 0))
        btns = frame(box, t)
        btns.pack(fill="x", pady=(12, 0))
        PillButton(btns, t, tr(ok), command=self.submit).pack(side="right")
        PillButton(btns, t, tr("Cancel"), kind="plain", command=self.cancel).pack(side="right", padx=(0, 8))
        self.entry.bind("<Return>", lambda e: self.submit())
        self.entry.bind("<KP_Enter>", lambda e: self.submit())
        self.entry.select_range(0, "end")
        self.entry.focus_set()
        app.name_dialog = self

    def submit(self) -> None:
        name = self.var.get()
        try:
            name = self.check(name) if self.check is not None else " ".join(name.split())
        except ValueError as exc:  # playerstate.InvalidName, themes.ThemeError
            self.error.configure(text=tr_text(str(exc)))
            return
        if not name:
            self.error.configure(text=tr("Give it a name."))
            return
        self.result = name
        self.close()

    def cancel(self) -> None:
        self.result = None
        self.close()

    def close(self) -> None:
        if self.app.name_dialog is self:
            self.app.name_dialog = None
        close_window(self.win)

    def run(self) -> Optional[str]:
        try:
            self.win.grab_set()
        except tk.TclError:
            pass
        self.win.wait_window()
        return self.result


def ask_name(app: "PlayerApp", title: str, prompt: str, initial: str = "",
             check: Optional[Callable[[str], str]] = None, ok: str = N_("OK"), parent=None) -> Optional[str]:
    """A name from the user, or None when they cancel (waits for the dialog)."""
    return NameDialog(app, title, prompt, initial, check, ok, parent).run()


class PlaybackInfoWindow:
    """Track, Decoder, Output and Quality check, each a two-column list; Copy."""

    def __init__(self, app: "PlayerApp"):
        self.app, t = app, app.theme
        self.win = _window(app, tr("Playback Info"))
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<<Close>>", lambda e: self.close())
        box = frame(self.win, t)
        box.pack(fill="both", expand=True, padx=24, pady=18)
        self.values: Dict[Tuple[str, str], tk.Label] = {}
        self.body = frame(box, t)
        self.body.pack(fill="both", expand=True)
        bottom = frame(box, t)
        bottom.pack(fill="x", pady=(14, 0))
        self.copied = label(bottom, t, "", "small", fg="text2")
        self.copied.pack(side="left")
        PillButton(bottom, t, tr("Close"), kind="plain", command=self.close).pack(side="right")
        PillButton(bottom, t, tr("Copy"), command=self.copy).pack(side="right", padx=(0, 8))
        self.sections: List[Tuple[str, List[Tuple[str, str]]]] = []
        self.refresh()
        self.win.minsize(560, 300)

    def refresh(self) -> None:
        sections = self.app.playback_info()
        layout = [(s, [k for k, _ in rows]) for s, rows in sections]
        if layout != [(s, [k for k, _ in rows]) for s, rows in self.sections]:
            self._build(sections)
        else:
            for s, rows in sections:
                for k, v in rows:
                    self.values[(s, k)].configure(text=v)
        self.sections = sections

    def _build(self, sections) -> None:
        t = self.app.theme
        for w in self.body.winfo_children():
            w.destroy()
        t.prune()
        self.values = {}
        row = 0
        for s, rows in sections:
            label(self.body, t, s, "h2").grid(row=row, column=0, columnspan=2, sticky="w",
                                               pady=(0 if row == 0 else 14, 4))
            row += 1
            for k, v in rows:
                label(self.body, t, k, "body", fg="text2").grid(row=row, column=0, sticky="nw", padx=(0, 18))
                val = label(self.body, t, v, "body", wraplength=520, justify="left")
                val.grid(row=row, column=1, sticky="w")
                self.values[(s, k)] = val
                row += 1
        self.body.columnconfigure(1, weight=1)

    def text(self) -> str:
        lines = []
        for s, rows in self.sections:
            lines.append(s)
            lines += [f"  {k}: {v}" for k, v in rows]
        return "\n".join(lines)

    def copy(self) -> None:
        self.win.clipboard_clear()
        self.win.clipboard_append(self.text())
        self.copied.configure(text=tr("Copied to the clipboard."))

    def close(self) -> None:
        self.app.info_window = None
        close_window(self.win)


class EqualizerWindow:
    """Equalizer on, Prevent clipping, the response curve, and the Graphic and
    Parametric tabs.  Changes are heard at once; saved when the window closes."""

    CURVE_W, CURVE_H = 660, 150

    def __init__(self, app: "PlayerApp"):
        self.app, self.t = app, app.theme
        t = self.t
        self.state = eq.normalize(copy.deepcopy(app.state.eq))
        self.win = _window(app, tr("Equalizer"))
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<<Close>>", lambda e: self.close())
        box = frame(self.win, t)
        box.pack(fill="both", expand=True, padx=20, pady=16)
        top = frame(box, t)
        top.pack(fill="x")
        self.v_on = tk.BooleanVar(value=self.state["enabled"])
        self.v_clip = tk.BooleanVar(value=self.state["prevent_clipping"])
        ttk.Checkbutton(top, text=tr("Equalizer on"), variable=self.v_on, style="P.TCheckbutton",
                        command=self._toggles).pack(side="left")
        ttk.Checkbutton(top, text=tr("Prevent clipping"), variable=self.v_clip, style="P.TCheckbutton",
                        command=self._toggles).pack(side="left", padx=(18, 0))
        self.pre_note = label(top, t, "", "small", fg="text2")
        self.pre_note.pack(side="right")
        presets = frame(box, t)
        presets.pack(fill="x", pady=(10, 0))
        label(presets, t, tr("Preset"), "body", fg="text2").pack(side="left", padx=(0, 8))
        self.v_preset = tk.StringVar(value="")
        self.preset_btn = PillButton(presets, t, "", kind="plain", arrow=True, command=self._post_presets)
        self.preset_btn.pack(side="left")
        for text, cmd in ((tr("Export…"), self._export), (tr("Import…"), self._import), (tr("Delete…"), self._delete),
                          (tr("Rename…"), self._rename), (tr("Save as preset…"), self._save_as)):
            PillButton(presets, t, text, kind="plain", command=cmd).pack(side="right", padx=(8, 0))
        self.note = label(box, t, "", "small", fg="text2", wraplength=640, justify="left")
        self.note.pack(anchor="w", pady=(4, 0))
        self.preset_menu: Optional[tk.Menu] = None
        self.curve = tk.Canvas(box, width=self.CURVE_W, height=self.CURVE_H, highlightthickness=0, bd=0)
        t.reg(self.curve, bg="lcd")
        self.curve.pack(fill="x", pady=(10, 10))
        self.curve.bind("<Configure>", lambda e: self.draw_curve())
        self.nb = ttk.Notebook(box, style="P.TNotebook")
        self.nb.pack(fill="both", expand=True)
        self.graphic = frame(self.nb, t)
        self.param = frame(self.nb, t)
        self.nb.add(self.graphic, text=tr("Graphic"))
        self.nb.add(self.param, text=tr("Parametric"))
        self._build_graphic()
        self._build_param()
        bottom = frame(box, t)
        bottom.pack(fill="x", pady=(12, 0))
        PillButton(bottom, t, tr("Done"), command=self.close).pack(side="right")
        PillButton(bottom, t, tr("Reset"), kind="plain", command=self._reset).pack(side="right", padx=(0, 8))
        t.on_change(self.draw_curve, self.win)
        self.win.minsize(700, 600)
        self.draw_curve()
        self._show_preset()

    # ---- graphic --------------------------------------------------------
    def _build_graphic(self) -> None:
        t, g = self.t, self.state["graphic"]
        head = frame(self.graphic, t)
        head.pack(fill="x", pady=(12, 6), padx=8)
        self.v_gon = tk.BooleanVar(value=g["enabled"])
        ttk.Checkbutton(head, text=tr("Use the graphic equalizer"), variable=self.v_gon, style="P.TCheckbutton",
                        command=self._toggles).pack(side="left")
        bands = frame(self.graphic, t)
        bands.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.g_sliders: List[Slider] = []
        self.g_values: List[tk.Label] = []
        cols = [(tr("Pre-amp"), g["preamp"])] + list(zip(eq.GRAPHIC_LABELS, g["gains"]))
        for i, (name, value) in enumerate(cols):
            col = frame(bands, t)
            col.grid(row=0, column=i, padx=(0, 18) if i == 0 else 4, sticky="ns")
            val = label(col, t, self._db(value), "small", fg="text2", anchor="center", width=6)
            val.pack()
            s = Slider(col, t, -eq.GAIN_LIMIT_DB, eq.GAIN_LIMIT_DB, value,
                       command=lambda v, final, i=i: self._graphic_band(i, v, final), length=190,
                       orient="vertical", origin=0.0, reset=0.0, thickness=28)
            s.pack(pady=4)
            label(col, t, name, "small_b" if i == 0 else "small", fg="text" if i == 0 else "text2",
                  anchor="center").pack()
            if i:
                self.g_sliders.append(s)
                self.g_values.append(val)
            else:
                self.g_pre, self.g_pre_val = s, val
        label(self.graphic, t, tr("Drag a slider to boost or cut that band (up to 12 dB). Double-click a slider "
                                  "to set it back to 0 dB."), "small", fg="text2").pack(anchor="w", padx=8, pady=(0, 8))

    def _db(self, v: float) -> str:
        return ("+" if v > 0.05 else "") + f"{v:.1f}"

    def _graphic_band(self, i: int, value: float, final: bool) -> None:
        value = round(value * 2) / 2  # half-dB steps
        g = self.state["graphic"]
        if i == 0:
            g["preamp"] = value
            self.g_pre_val.configure(text=self._db(value))
        else:
            g["gains"][i - 1] = value
            self.g_values[i - 1].configure(text=self._db(value))
            g["preset"] = eq.preset_name(g["gains"])
        self._changed()

    def _preset(self, name: str) -> None:
        if name == eq.CUSTOM:
            return
        self.state = eq.apply_preset(self.state, name)
        for s, v, lab in zip(self.g_sliders, self.state["graphic"]["gains"], self.g_values):
            s.set(v)
            lab.configure(text=self._db(v))
        self._changed()

    # ---- parametric -----------------------------------------------------
    def _build_param(self) -> None:
        t, p = self.t, self.state["parametric"]
        head = frame(self.param, t)
        head.pack(fill="x", pady=(12, 6), padx=8)
        self.v_pon = tk.BooleanVar(value=p["enabled"])
        ttk.Checkbutton(head, text=tr("Use the parametric equalizer"), variable=self.v_pon, style="P.TCheckbutton",
                        command=self._toggles).pack(side="left")
        pre = frame(self.param, t)
        pre.pack(fill="x", padx=8, pady=(2, 8))
        label(pre, t, tr("Pre-amp"), "body_b").pack(side="left")
        self.p_pre_val = label(pre, t, f"{self._db(p['preamp'])} dB", "body", fg="text2", width=8)
        self.p_pre = Slider(pre, t, -eq.GAIN_LIMIT_DB, eq.GAIN_LIMIT_DB, p["preamp"], command=self._param_pre,
                            length=260, origin=0.0, reset=0.0)
        self.p_pre.pack(side="left", padx=(12, 6))
        self.p_pre_val.pack(side="left")
        self.rows = frame(self.param, t)
        self.rows.pack(fill="both", expand=True, padx=8)
        bottom = frame(self.param, t)
        bottom.pack(fill="x", padx=8, pady=(8, 10))
        self.add_btn = PillButton(bottom, t, tr("Add band"), kind="plain", command=self._add_band)
        self.add_btn.pack(side="left")
        self._band_rows()

    def _band_rows(self) -> None:
        t = self.t
        for w in self.rows.winfo_children():
            w.destroy()
        t.prune()
        heads = (tr("On"), tr("Type"), tr("Frequency (Hz)"), tr("Gain (dB)"), "Q", "")
        for c, text in enumerate(heads):
            label(self.rows, t, text, "small_b", fg="text2").grid(row=0, column=c, sticky="w", padx=(0, 12),
                                                                   pady=(0, 4))
        type_labels = [tr(eq.BAND_TYPE_LABELS[k]) for k in eq.BAND_TYPES]
        for r, b in enumerate(self.state["parametric"]["bands"], start=1):
            on = tk.BooleanVar(value=b["on"])
            ttk.Checkbutton(self.rows, variable=on, style="P.TCheckbutton",
                            command=lambda r=r, v=on: self._band(r - 1, "on", v.get())).grid(row=r, column=0,
                                                                                            sticky="w")
            kind = tk.StringVar(value=tr(eq.BAND_TYPE_LABELS[b["type"]]))
            cb = ttk.Combobox(self.rows, textvariable=kind, values=type_labels, state="readonly", width=11,
                              style="P.TCombobox", font=t.f("body"))
            cb.grid(row=r, column=1, sticky="w", padx=(0, 12), pady=3)
            cb.bind("<<ComboboxSelected>>", lambda e, r=r, v=kind: self._band(
                r - 1, "type", eq.BAND_TYPES[type_labels.index(v.get())]))
            specs = (("freq", eq.MIN_FREQ, eq.MAX_FREQ, 10, "{:g}"),
                     ("gain", -eq.GAIN_LIMIT_DB, eq.GAIN_LIMIT_DB, 0.5, "{:g}"),
                     ("q", eq.MIN_Q, eq.MAX_Q, 0.1, "{:.2f}"))
            for c, (key, lo, hi, inc, fmt) in enumerate(specs, start=2):
                var = tk.StringVar(value=fmt.format(b[key]))
                sp = ttk.Spinbox(self.rows, textvariable=var, from_=lo, to=hi, increment=inc, width=9,
                                 style="P.TSpinbox", font=t.f("body"),
                                 command=lambda r=r, k=key, v=var: self._band_text(r - 1, k, v))
                sp.grid(row=r, column=c, sticky="w", padx=(0, 12), pady=3)
                for ev in ("<FocusOut>", "<Return>"):
                    sp.bind(ev, lambda e, r=r, k=key, v=var: self._band_text(r - 1, k, v))
                if key == "gain" and b["type"] not in eq.GAIN_TYPES:
                    sp.state(["disabled"])
            PillButton(self.rows, t, tr("Remove"), kind="plain", height=28,
                       command=lambda r=r: self._remove_band(r - 1)).grid(row=r, column=5, sticky="w")
        self.add_btn.set(enabled=len(self.state["parametric"]["bands"]) < eq.MAX_PARAMETRIC_BANDS)

    def _band_text(self, i: int, key: str, var: tk.StringVar) -> None:
        try:
            value = float(var.get().replace(",", "."))
        except ValueError:
            return
        self._band(i, key, value)

    def _band(self, i: int, key: str, value) -> None:
        bands = self.state["parametric"]["bands"]
        if i >= len(bands):
            return
        old_type = bands[i]["type"]
        bands[i][key] = value
        bands[i] = eq.normalize_band(bands[i])
        if key == "type" and (old_type in eq.GAIN_TYPES) != (value in eq.GAIN_TYPES):
            self._band_rows()  # the gain field turns on or off
        self._changed()

    def _add_band(self) -> None:
        bands = self.state["parametric"]["bands"]
        if len(bands) < eq.MAX_PARAMETRIC_BANDS:
            bands.append({"type": "peak", "freq": 1000.0, "gain": 0.0, "q": 1.0, "on": True})
            self._band_rows()
            self._changed()

    def _remove_band(self, i: int) -> None:
        bands = self.state["parametric"]["bands"]
        if i < len(bands):
            bands.pop(i)
            self._band_rows()
            self._changed()

    def _param_pre(self, value: float, final: bool) -> None:
        value = round(value * 2) / 2
        self.state["parametric"]["preamp"] = value
        self.p_pre_val.configure(text=f"{self._db(value)} dB")
        self._changed()

    # ---- both -----------------------------------------------------------
    def _toggles(self) -> None:
        s = self.state
        s["enabled"], s["prevent_clipping"] = self.v_on.get(), self.v_clip.get()
        s["graphic"]["enabled"], s["parametric"]["enabled"] = self.v_gon.get(), self.v_pon.get()
        self._changed()

    def _reset(self) -> None:
        on = self.state["enabled"]
        self.state = eq.default_state()
        self.state["enabled"] = on
        g = self.state["graphic"]
        self.g_pre.set(0.0)
        self.g_pre_val.configure(text=self._db(0.0))
        for s, lab in zip(self.g_sliders, self.g_values):
            s.set(0.0)
            lab.configure(text=self._db(0.0))
        self.v_gon.set(g["enabled"])
        self.v_pon.set(self.state["parametric"]["enabled"])
        self.v_clip.set(self.state["prevent_clipping"])
        self.p_pre.set(0.0)
        self.p_pre_val.configure(text=f"{self._db(0.0)} dB")
        self._band_rows()
        self._changed()

    def _changed(self) -> None:
        self.state = eq.normalize(self.state)
        self.app.set_eq(self.state)
        self.draw_curve()
        self._show_preset()

    # ---- presets ----------------------------------------------------------
    def preset_label(self) -> str:
        """The user preset the EQ is, else the built-in preset of the graphic bands, or Custom."""
        return self.app.state.matching_eq_preset() or eq.preset_name(self.state["graphic"]["gains"])

    @staticmethod
    def shown_name(name: str) -> str:
        """A preset's name as the menu and the button show it: the built-in presets and
        "Custom" translated, the user's own as they are named."""
        return tr(name) if name in eq.PRESET_NAMES or name == eq.CUSTOM else name

    def _show_preset(self) -> None:
        name = self.preset_label()
        self.v_preset.set(name)
        self.preset_btn.set(text=self.shown_name(name))

    def build_preset_menu(self) -> tk.Menu:
        """Built-in presets, "My presets", and "Custom" when the gains match none."""
        m = tk.Menu(self.win, tearoff=False)
        self.app.theme_menu(m)
        cur = self.preset_label()
        for name in eq.PRESET_NAMES:
            m.add_radiobutton(label=tr(name), value=name, variable=self.v_preset,
                              command=lambda n=name: self._preset(n))
        mine = self.app.state.eq_presets
        if mine:
            m.add_separator()
            m.add_command(label=tr("My presets"), state="disabled")
            for p in mine:
                m.add_radiobutton(label=p["name"], value=p["name"], variable=self.v_preset,
                                  command=lambda n=p["name"]: self.use_preset(n))
        if cur == eq.CUSTOM:
            m.add_separator()
            m.add_radiobutton(label=tr(eq.CUSTOM), value=eq.CUSTOM, variable=self.v_preset, state="disabled")
        self.v_preset.set(cur)
        self.preset_menu = m
        return m

    def _post_presets(self) -> None:
        b = self.preset_btn
        self.build_preset_menu().tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def use_preset(self, name: str) -> None:
        """One of the user's presets: the whole equalizer (graphic and parametric)."""
        p = self.app.state.eq_preset(name)
        if p is not None:
            st = eq.normalize(copy.deepcopy(p["eq"]))
            st["enabled"] = self.state["enabled"] or st["enabled"]
            self.load_state(st)

    def load_state(self, st: dict) -> None:
        """Show (and apply) another equalizer state in every control."""
        self.state = eq.normalize(st)
        s, g, p = self.state, self.state["graphic"], self.state["parametric"]
        self.v_on.set(s["enabled"])
        self.v_clip.set(s["prevent_clipping"])
        self.v_gon.set(g["enabled"])
        self.v_pon.set(p["enabled"])
        self.g_pre.set(g["preamp"])
        self.g_pre_val.configure(text=self._db(g["preamp"]))
        for sl, v, lab in zip(self.g_sliders, g["gains"], self.g_values):
            sl.set(v)
            lab.configure(text=self._db(v))
        self.p_pre.set(p["preamp"])
        self.p_pre_val.configure(text=f"{self._db(p['preamp'])} dB")
        self._band_rows()
        self._changed()

    def save_preset(self, name: str) -> str:
        """Keep the current equalizer as a preset of the user's (raises InvalidName)."""
        name = self.app.state.save_eq_preset(name, self.state)
        self.app.save_state()
        self._show_preset()
        self.note.configure(text=tr("Saved as “{name}”.", name=name), fg=self.t.c("text2"))
        return name

    def _save_as(self) -> None:
        from . import playerstate

        name = ask_name(self.app, tr("Save as preset"), tr("Name of the new preset"), tr("My preset"),
                        check=self.app.state.check_preset_name, ok=tr("Save"), parent=self.win)
        if name:
            try:
                self.save_preset(name)
            except playerstate.InvalidName as exc:
                self.note.configure(text=tr_text(str(exc)), fg=self.t.c("error"))

    def _mine(self) -> str:
        name = self.app.state.matching_eq_preset()
        if not name:
            self.note.configure(text=tr("Choose one of your own presets first (the menu lists them under My "
                                        "presets)."),
                                fg=self.t.c("error"))
        return name

    def rename_preset(self, old: str, new: str) -> str:
        new = self.app.state.rename_eq_preset(old, new)
        self.app.save_state()
        self._show_preset()
        return new

    def _rename(self) -> None:
        old = self._mine()
        if old:
            new = ask_name(self.app, tr("Rename preset"), tr("New name of the preset"), old, parent=self.win,
                           check=lambda n: self.app.state.check_preset_name(n, renaming=old), ok=tr("Rename"))
            if new:
                self.rename_preset(old, new)

    def _delete(self) -> None:
        from tkinter import messagebox

        name = self._mine()
        if name and messagebox.askyesno(tr("Delete preset"), tr("Delete your preset “{name}”?", name=name),
                                        parent=self.win):
            self.app.state.delete_eq_preset(name)
            self.app.save_state()
            self._show_preset()
            self.note.configure(text=tr("Deleted “{name}”.", name=name), fg=self.t.c("text2"))

    def import_file(self, path: str) -> str:
        """Read a .json or Equalizer APO / AutoEQ .txt preset, apply it and save it under
        its name (a taken name gets " 2"); returns the name.  Raises EqFileError / OSError."""
        import os

        from . import playerstate

        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        name, st, notes = eq.import_eq(text, os.path.splitext(os.path.basename(path))[0])
        base, n = name, 2
        while True:
            try:
                name = self.app.state.check_preset_name(name)
                break
            except playerstate.InvalidName:
                name = f"{base} {n}"
                n += 1
        self.load_state(st)
        self.save_preset(name)
        joiner = i18n.SENTENCE_JOIN.get(i18n.language(), " ")
        self.note.configure(text=joiner.join([tr("Imported “{name}”.", name=name)] + [tr_text(x) for x in notes]),
                            fg=self.t.c("text2"))
        return name

    def _import(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(parent=self.win, title=tr("Import an equalizer preset"),
                                          filetypes=[(tr("Equalizer presets"), "*.json *.txt"), (tr("All files"), "*")])
        if path:
            try:
                self.import_file(path)
            except (eq.EqFileError, OSError) as exc:
                self.note.configure(text=tr_text(str(exc)), fg=self.t.c("error"))

    def export_file(self, path: str) -> None:
        """.txt: Equalizer APO / AutoEQ text; anything else: Real-Hi-Res's own .json."""
        name = self.preset_label()
        text = eq.export_apo(name, self.state) if path.lower().endswith(".txt") else eq.export_json(name, self.state)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        self.note.configure(text=tr("Exported “{name}” to {path}.", name=self.shown_name(name), path=path),
                            fg=self.t.c("text2"))

    def _export(self) -> None:
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(
            parent=self.win, title=tr("Export the equalizer"), initialfile=f"{self.preset_label()}.json",
            defaultextension=".json", filetypes=[(tr("Real-Hi-Res preset"), "*.json"),
                                                 (tr("Equalizer APO / AutoEQ text"), "*.txt")])
        if path:
            try:
                self.export_file(path)
            except OSError as exc:
                self.note.configure(text=tr("Could not export: {error}", error=exc), fg=self.t.c("error"))


    def draw_curve(self) -> None:
        t, c = self.t, self.curve
        c.delete("all")
        W, H = max(c.winfo_width(), 100), max(c.winfo_height(), 60)
        left, right, top, bottom = 44, 14, 12, 22
        pw, ph = W - left - right, H - top - bottom

        def xf(f: float) -> float:
            return left + pw * math.log10(f / 20.0) / 3.0

        def yf(db: float) -> float:
            return top + ph * (1 - (db + 12.0) / 24.0)

        for db in (-12, -6, 0, 6, 12):
            y = yf(db)
            c.create_line(left, y, W - right, y, fill=t.c("sep"), dash=() if db == 0 else (2, 3))
            c.create_text(left - 6, y, text=f"{db:+d}" if db else "0", anchor="e", fill=t.c("text2"),
                          font=t.f("small"))
        for f, text in ((20, "20"), (50, "50"), (100, "100"), (200, "200"), (500, "500"), (1000, "1K"),
                        (2000, "2K"), (5000, "5K"), (10000, "10K"), (20000, "20K")):
            x = xf(f)
            c.create_line(x, top, x, top + ph, fill=t.c("sep"), dash=(2, 3))
            c.create_text(x, H - 10, text=text, fill=t.c("text2"), font=t.f("small"))
        on = self.state["enabled"]
        pts = []
        for f, db in eq.response(self.state):
            pts += [xf(f), yf(max(-12.0, min(12.0, db)))]
        c.create_line(*pts, fill=t.c("accent") if on else t.c("text2"), width=2.5, smooth=True)
        pre = eq.preamp_db(self.state)
        self.pre_note.configure(text=(tr("Pre-amp {db}", db=fmt_db(pre)) if on and abs(pre) >= 0.05 else
                                      "" if on else tr("The equalizer is off")))

    def close(self) -> None:
        self.app.save_eq(self.state)
        self.app.eq_window = None
        close_window(self.win)


class EditInfoWindow:
    """Edit Info: title, artist, album, album artist, genre, release date, track, disc
    and composer of one song or of an album's songs (tagedit.FIELDS).  The tags are
    read with ffprobe in a thread first; where the songs differ a field starts empty
    with a note and is kept unless something is typed into it.  Save checks the
    changes (tagedit.check_changes()), writes them in a thread (tagedit.save_tags())
    and refreshes the library; it closes when everything was saved without a note,
    else it shows tagedit.save_summary().  Results reach Tk through `self.q`."""

    SEVERAL = N_("Several values: kept unless you type here")  # shown as tr(SEVERAL)

    def __init__(self, app: "PlayerApp", tracks: list, heading: str = "", subheading: str = ""):
        from .settings import Settings

        self.app, t = app, app.theme
        self.paths: List[str] = [tr.path for tr in tracks]
        self.cfg = Settings.from_dict(app.cfg.to_dict())  # the threads get their own copy
        self.q: "queue.Queue" = queue.Queue()
        self.ready = self.saving = self.done = False
        self.initial: Dict[str, Optional[str]] = {}
        self.outcomes: list = []
        self.win = _window(app, tr("Edit Info"))
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        self.win.bind("<<Close>>", lambda e: self.cancel())
        box = frame(self.win, t)
        box.pack(fill="both", expand=True, padx=24, pady=18)
        if not heading and len(tracks) == 1:
            heading = tracks[0].display_title
            subheading = " · ".join(x for x in (tracks[0].artist, tracks[0].album) if x)
        label(box, t, tr_name(heading) if heading else tr("{n} songs", n=f"{len(tracks):,}"), "h2",
              wraplength=520, justify="left").pack(anchor="w")
        if subheading:
            label(box, t, subheading, "body", fg="text2", wraplength=520, justify="left").pack(anchor="w")
        n = len(self.paths)
        self.status = label(box, t, tr("Reading the information of 1 song…") if n == 1 else
                            tr("Reading the information of {n} songs…", n=f"{n:,}"), "small", fg="text2")
        self.status.pack(anchor="w", pady=(6, 0))
        grid = frame(box, t)
        grid.pack(fill="x", pady=(8, 0))
        self.vars: Dict[str, tk.StringVar] = {}
        self.entries: Dict[str, ttk.Entry] = {}
        self.notes: Dict[str, tk.Label] = {}
        for i, f in enumerate(tagedit.FIELDS):
            label(grid, t, tr(tagedit.LABELS[f]), "body", fg="text2").grid(row=2 * i, column=0, sticky="w",
                                                                           padx=(0, 16), pady=(7, 0))
            var = tk.StringVar()
            e = ttk.Entry(grid, textvariable=var, width=44, style="P.TEntry", font=t.f("body"))
            e.grid(row=2 * i, column=1, sticky="we", pady=(7, 0))
            e.state(["disabled"])
            e.bind("<Return>", lambda ev: self.save())
            e.bind("<KP_Enter>", lambda ev: self.save())
            note = label(grid, t, tr(tagedit.HINTS.get(f, "")) if tagedit.HINTS.get(f) else "", "small",
                         fg="text2", wraplength=440, justify="left")
            note.grid(row=2 * i + 1, column=1, sticky="w")
            if not tagedit.HINTS.get(f):
                note.grid_remove()
            self.vars[f], self.entries[f], self.notes[f] = var, e, note
        grid.columnconfigure(1, weight=1)
        self.error = label(box, t, "", "small", fg="error", wraplength=520, justify="left")
        self.error.pack(anchor="w", pady=(10, 0))
        self.summary = label(box, t, "", "body", wraplength=520, justify="left")
        self.summary.pack(anchor="w")
        self.btns = frame(box, t)
        self.btns.pack(fill="x", pady=(12, 0))
        self.save_b = PillButton(self.btns, t, tr("Save"), command=self.save)
        self.save_b.pack(side="right")
        self.cancel_b = PillButton(self.btns, t, tr("Cancel"), kind="plain", command=self.cancel)
        self.cancel_b.pack(side="right", padx=(0, 8))
        self.win.minsize(560, 0)
        threading.Thread(target=self._read, daemon=True, name="realhires-edit-read").start()
        self.win.after(100, self._poll)

    # ---- threads ------------------------------------------------------------
    def _read(self) -> None:
        from .probe import probe

        tags = []
        for p in self.paths:
            try:
                tags.append(probe(p).tags or {})
            except Exception:  # noqa: BLE001 (an unreadable file has no tags to show)
                tags.append({})
        self.q.put(("tags", tags))

    def _write(self, changes: Dict[str, str]) -> None:
        try:
            out = tagedit.save_tags(self.paths, changes, self.cfg,
                                    progress=lambda i, n, p: self.q.put(("progress", i, n)))
        except Exception as exc:  # noqa: BLE001 (save_tags never raises; this is a last resort)
            out = [tagedit.TagOutcome(p, False, tr("Could not save the information: {error}", error=exc))
                   for p in self.paths]
        self.q.put(("saved", out))

    def _poll(self) -> None:
        try:
            if not self.win.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "tags":
                    self._fill(rest[0])
                elif kind == "progress":
                    i, n = rest
                    if i < n:
                        self.status.configure(text=tr("Saving {i} of {n}…", i=f"{i + 1:,}", n=f"{n:,}"))
                elif kind == "saved":
                    self._saved(rest[0])
                    return
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    # ---- the window ---------------------------------------------------------
    def _fill(self, tags: List[dict]) -> None:
        self.initial = tagedit.common_values(tags)
        for f in tagedit.FIELDS:
            value = self.initial[f]
            self.vars[f].set(value or "")
            self.entries[f].state(["!disabled"])
            hint = tr(tagedit.HINTS[f]) if tagedit.HINTS.get(f) else ""
            if value is None:
                self.notes[f].configure(text=tr("{several}. {hint}", several=tr(self.SEVERAL),
                                                hint=hint[0].upper() + hint[1:]) if hint else tr(self.SEVERAL))
                self.notes[f].grid()
        self.status.configure(text="")
        self.ready = True
        first = self.entries["title" if len(self.paths) == 1 else "album"]
        first.focus_set()
        first.select_range(0, "end")

    def changes(self) -> Dict[str, str]:
        """The fields whose text differs from what they started with (a "several
        values" field: when something was typed)."""
        out = {}
        for f in tagedit.FIELDS:
            text, start = self.vars[f].get(), self.initial.get(f)
            if start is None:
                if tagedit.clean_value(text):
                    out[f] = text
            elif tagedit.clean_value(text) != tagedit.clean_value(start):
                out[f] = text
        return out

    def save(self) -> None:
        if not self.ready or self.saving or self.done:
            return
        changes = self.changes()
        try:
            changes = tagedit.check_changes(changes)
        except tagedit.TagError as exc:
            self.error.configure(text=tr_text(str(exc)))
            return
        self.error.configure(text="")
        if not changes:
            self.close()
            return
        self.saving = True
        for e in self.entries.values():
            e.state(["disabled"])
        self.save_b.pack_forget()
        self.cancel_b.pack_forget()
        n = len(self.paths)
        self.status.configure(text=tr("Saving {i} of {n}…", i="1", n=f"{n:,}") if n > 1 else tr("Saving…"))
        threading.Thread(target=self._write, args=(changes,), daemon=True, name="realhires-edit-save").start()

    def _saved(self, outcomes: list) -> None:
        self.saving, self.done = False, True
        self.outcomes = outcomes
        self.app.tags_saved([o.path for o in outcomes if o.ok])
        head, lines = tagedit.save_summary(outcomes)
        if all(o.ok for o in outcomes) and not lines:
            self.close()
            return
        self.status.configure(text="")
        self.summary.configure(text="\n\n".join([tr_text(head)] + [tr_text(x) for x in lines]))
        PillButton(self.btns, self.app.theme, tr("Close"), command=self.close).pack(side="right")

    def cancel(self) -> None:
        if not self.saving:
            self.close()

    def close(self) -> None:
        if self.saving:
            return
        if self.app.edit_window is self:
            self.app.edit_window = None
        close_window(self.win)


__all__ = ["PlaybackInfoWindow", "EqualizerWindow", "EditInfoWindow", "NameDialog", "ask_name"]
