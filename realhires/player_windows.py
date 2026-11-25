"""The 2.0 player's windows: Playback info (track, decoder, output, quality check),
the equalizer (graphic and parametric), and Edit Info (a song's information)."""

from __future__ import annotations

import copy
import math
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from . import bitperfect, eq, i18n, tagedit
from .i18n import N_, tr, tr_name, tr_text
from .player import fmt_db
from .player_theme import IconButton, PillButton, Slider
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
    """Asks for a name (playlist, preset, theme, genre); `check(name)` returns the
    cleaned name or raises an error whose message is shown under the field.  With
    `choices` the field is a drop-down of them that can also be typed into."""

    def __init__(self, app: "PlayerApp", title: str, prompt: str, initial: str = "",
                 check: Optional[Callable[[str], str]] = None, ok: str = N_("OK"), parent=None,
                 choices: Optional[List[str]] = None):
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
        label(box, t, prompt, "body_b", wraplength=420, justify="left").pack(anchor="w")
        self.var = tk.StringVar(value=initial)
        if choices:
            self.entry = ttk.Combobox(box, textvariable=self.var, values=list(choices), width=40, font=t.f("body"),
                                      height=min(len(choices), 14))
        else:
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
             check: Optional[Callable[[str], str]] = None, ok: str = N_("OK"), parent=None,
             choices: Optional[List[str]] = None) -> Optional[str]:
    """A name from the user, or None when they cancel (waits for the dialog)."""
    return NameDialog(app, title, prompt, initial, check, ok, parent, choices).run()


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
    PRESET_ROW_MAX = 720  # wider, the preset row wraps (English needs 674 px, Japanese about 800)

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
        # the preset row: the menu, then the buttons on its right, or (2.3) on a second line
        # for the buttons that do not fit the window's width (_wrap_presets())
        presets = self.presets = frame(box, t)
        presets.pack(fill="x", pady=(10, 0))
        self.presets2 = frame(box, t)
        self.preset_text = label(presets, t, tr("Preset"), "body", fg="text2")
        self.preset_text.pack(side="left", padx=(0, 8))
        self.v_preset = tk.StringVar(value="")
        self.preset_btn = PillButton(presets, t, "", kind="plain", arrow=True, command=self._post_presets)
        self.preset_btn.pack(side="left")
        self.preset_buttons: List[PillButton] = []
        for text, cmd in ((tr("Save as preset…"), self._save_as), (tr("Rename…"), self._rename),
                          (tr("Delete…"), self._delete), (tr("Import…"), self._import), (tr("Export…"), self._export)):
            b = PillButton(box, t, text, kind="plain", command=cmd)  # packed into either line
            self.preset_buttons.append(b)
            if cmd == self._delete:
                self.delete_btn = b
        self.preset_wrapped = False
        self.note = label(box, t, "", "small", fg="text2", wraplength=640, justify="left")
        self.note.pack(anchor="w", pady=(4, 0))
        # with bit-perfect playback on, a note that the equalizer changes the samples (2.3; before,
        # it was bypassed then); shown only then, so it leaves no empty line above the curve
        self.bypass = label(box, t, "", "body", fg="accent", wraplength=640, justify="left")
        self.preset_menu: Optional[tk.Menu] = None
        self.delete_menu: Optional[tk.Menu] = None
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
        t.on_change(self._wrap_presets, self.win)  # another font measures differently
        self.win.minsize(700, 600)
        self.draw_curve()
        self._show_preset()
        self.show_bypass()

    def show_bypass(self) -> None:
        """While bit-perfect playback is on: the equalizer works, but the sound is then not
        bit-perfect (`bitperfect.EQ_NOTE`)."""
        on = bool(self.app.cfg.bit_perfect)
        self.bypass.configure(text=tr(bitperfect.EQ_NOTE) if on else "")
        if on:
            self.bypass.pack(anchor="w", pady=(4, 0), before=self.curve)
        else:
            self.bypass.pack_forget()

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
        self._wrap_presets()

    def _wrap_presets(self) -> None:
        """Lay the preset row out within `PRESET_ROW_MAX` pixels: the buttons that do not
        fit after the menu go to a second line (2.3: in Japanese the row made the window
        much wider than its other content)."""
        gap = 8
        used = self.preset_text.winfo_reqwidth() + gap + self.preset_btn.winfo_reqwidth()
        first, rest = [], []
        for b in self.preset_buttons:
            w = gap + b.winfo_reqwidth()
            if not rest and used + w <= self.PRESET_ROW_MAX:
                first.append(b)
                used += w
            else:
                rest.append(b)
        for b in self.preset_buttons:
            b.pack_forget()
        # packed from the right in reverse order: they read left to right as listed
        for b in reversed(first):
            b.pack(in_=self.presets, side="right", padx=(gap, 0))
        for b in reversed(rest):
            b.pack(in_=self.presets2, side="right", padx=(gap, 0))
        self.preset_wrapped = bool(rest)
        if rest:
            self.presets2.pack(fill="x", pady=(6, 0), after=self.presets)
        else:
            self.presets2.pack_forget()

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
        """Delete…: the preset the equalizer is set to; when it is set to none of the
        user's presets (changed since, or never chosen), a menu of them to pick from."""
        name = self.app.state.matching_eq_preset()
        if name:
            self.delete_preset(name)
            return
        if not self.app.state.eq_presets:
            self.note.configure(text=tr("You have no presets of your own yet: Save as preset… keeps the current "
                                        "settings as one."), fg=self.t.c("error"))
            return
        b = self.delete_btn
        self.build_delete_menu().tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def build_delete_menu(self) -> tk.Menu:
        m = tk.Menu(self.win, tearoff=False)
        self.app.theme_menu(m)
        m.add_command(label=tr("Delete which preset?"), state="disabled")
        for p in self.app.state.eq_presets:
            m.add_command(label=p["name"], command=lambda n=p["name"]: self.delete_preset(n))
        self.delete_menu = m
        return m

    def delete_preset(self, name: str, confirm: bool = True) -> bool:
        """Delete one of the user's presets (after asking); the equalizer keeps its settings."""
        from tkinter import messagebox

        if self.app.state.eq_preset(name) is None:
            return False
        if confirm and not messagebox.askyesno(
                tr("Delete preset"),
                tr("Delete your preset “{name}”? The equalizer keeps its current settings.", name=name),
                parent=self.win):
            return False
        self.app.state.delete_eq_preset(name)
        self.app.save_state()
        self._show_preset()
        self.note.configure(text=tr("Deleted “{name}”.", name=name), fg=self.t.c("text2"))
        return True

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
    """Properties (2.0 "Edit Info", renamed in 2.1): title, artist, album, album
    artist, genre, release date, track, disc and composer of one song or of an
    album's songs (tagedit.FIELDS).  The tags are read with ffprobe in a thread
    first; where the songs differ a field starts empty with a note and is kept
    unless something is typed into it.  Save checks the changes
    (tagedit.check_changes()), writes them in a thread (tagedit.save_tags()) and
    refreshes the library; it closes when everything was saved without a note, else
    it shows tagedit.save_summary().  Results reach Tk through `self.q`.  The window
    can be resized (never smaller than its contents), the fields grow with its
    width, and a size the user chose is kept in `app.edit_size` for the session.

    2.3: Acquire Metadata… (bottom left) opens the MusicBrainz window for the same
    songs; when it saves, `files_changed()` reads the songs again and updates the
    fields nothing was typed into.  Each field's Search button opens the browser at
    tagedit.web_search_url() and then watches the clipboard (`app.clipboard_text()`,
    every tagedit.WATCH_POLL_MS for tagedit.WATCH_SECONDS): the first new text copied
    is put into that field (tagedit.clipboard_value()), which flashes."""

    TITLE = N_("Properties")   # shown as tr(TITLE)
    SEVERAL = N_("Several values: kept unless you type here")  # shown as tr(SEVERAL)
    WATCHING = N_("Copy the answer in your browser and it will be filled in here.")
    FILLED = N_("Filled in from the clipboard.")
    MIN_WIDTH = 560
    FLASH_MS = 1200

    def __init__(self, app: "PlayerApp", tracks: list, heading: str = "", subheading: str = "",
                 album: bool = False):
        from .settings import Settings

        self.app, t = app, app.theme
        self.tracks = list(tracks)
        self.album, self.heading, self.subheading = album, heading, subheading  # for Acquire Metadata…
        self.paths: List[str] = [tr.path for tr in tracks]
        self.clock: Callable[[], float] = time.monotonic  # the clipboard watch's clock (tests move it)
        self.watch: Optional[dict] = None   # the clipboard watch: field, the text at the click, until
        self._watch_job: Optional[str] = None
        self.filled: Optional[str] = None   # the field filled in from the clipboard last
        self.cfg = Settings.from_dict(app.cfg.to_dict())  # the threads get their own copy
        self.q: "queue.Queue" = queue.Queue()
        self.ready = self.saving = self.done = False
        self.initial: Dict[str, Optional[str]] = {}
        self.outcomes: list = []
        self.win = _window(app, tr(self.TITLE))
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
        self.search_bs: Dict[str, IconButton] = {}
        for i, f in enumerate(tagedit.FIELDS):
            label(grid, t, tr(tagedit.LABELS[f]), "body", fg="text2").grid(row=2 * i, column=0, sticky="w",
                                                                           padx=(0, 16), pady=(7, 0))
            var = tk.StringVar()
            e = ttk.Entry(grid, textvariable=var, width=44, style="P.TEntry", font=t.f("body"))
            e.grid(row=2 * i, column=1, sticky="we", pady=(7, 0))
            e.state(["disabled"])
            e.bind("<Return>", lambda ev: self.save())
            e.bind("<KP_Enter>", lambda ev: self.save())
            # 2.3: search the web for this field ("Search the web for the composer")
            b = IconButton(grid, t, "search", command=lambda f=f: self.search(f), size=26, icon_size=14,
                           tooltip=lambda f=f: self.search_tip(f))
            b.grid(row=2 * i, column=2, sticky="e", padx=(6, 0), pady=(7, 0))
            b.set(enabled=False)
            self.search_bs[f] = b
            note = label(grid, t, tr(tagedit.HINTS.get(f, "")) if tagedit.HINTS.get(f) else "", "small",
                         fg="text2", wraplength=440, justify="left")
            note.grid(row=2 * i + 1, column=1, sticky="w")
            if not tagedit.HINTS.get(f):
                note.grid_remove()
            self.vars[f], self.entries[f], self.notes[f] = var, e, note
        grid.columnconfigure(1, weight=1)
        self.watch_note = label(box, t, "", "small", fg="text2", wraplength=520, justify="left")
        self.watch_note.pack(anchor="w", pady=(10, 0))
        self.error = label(box, t, "", "small", fg="error", wraplength=520, justify="left")
        self.error.pack(anchor="w", pady=(4, 0))
        self.summary = label(box, t, "", "body", wraplength=520, justify="left")
        self.summary.pack(anchor="w")
        self.btns = frame(box, t)
        self.btns.pack(fill="x", pady=(12, 0))
        self.save_b = PillButton(self.btns, t, tr("Save"), command=self.save)
        self.save_b.pack(side="right")
        self.cancel_b = PillButton(self.btns, t, tr("Cancel"), kind="plain", command=self.cancel)
        self.cancel_b.pack(side="right", padx=(0, 8))
        # 2.3: the MusicBrainz window for the same songs, at the bottom left
        self.acquire_b = PillButton(self.btns, t, tr("Acquire Metadata…"), kind="plain", command=self.acquire)
        self.acquire_b.pack(side="left")
        if app.edit_size:
            self.win.geometry("%dx%d" % app.edit_size)
        self._fit()
        threading.Thread(target=self._read, daemon=True, name="realhires-edit-read").start()
        self.win.after(100, self._poll)

    # ---- threads ------------------------------------------------------------
    def _read(self, kind: str = "tags") -> None:
        from .probe import probe

        tags = []
        for p in self.paths:
            try:
                tags.append(probe(p).tags or {})
            except Exception:  # noqa: BLE001 (an unreadable file has no tags to show)
                tags.append({})
        self.q.put((kind, tags))

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
                elif kind == "retags":
                    self._refill(rest[0])
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
    def _fit(self) -> None:
        """Never smaller than the contents, which change (notes, an error, the summary):
        the minimum height follows them, so the buttons stay on the screen."""
        try:
            self.win.update_idletasks()
            self.win.minsize(self.MIN_WIDTH, self.win.winfo_reqheight())
        except tk.TclError:
            pass

    def _note(self, f: str, value: Optional[str]) -> None:
        """The grey line under a field: its hint, with "Several values…" while the songs differ."""
        hint = tr(tagedit.HINTS[f]) if tagedit.HINTS.get(f) else ""
        if value is None:
            self.notes[f].configure(text=tr("{several}. {hint}", several=tr(self.SEVERAL),
                                            hint=hint[0].upper() + hint[1:]) if hint else tr(self.SEVERAL))
            self.notes[f].grid()
        else:
            self.notes[f].configure(text=hint)
            if not hint:
                self.notes[f].grid_remove()

    def _fill(self, tags: List[dict]) -> None:
        self.initial = tagedit.common_values(tags)
        for f in tagedit.FIELDS:
            value = self.initial[f]
            self.vars[f].set(value or "")
            self.entries[f].state(["!disabled"])
            self.search_bs[f].set(enabled=True)
            if value is None:
                self._note(f, None)
        self.status.configure(text="")
        self.ready = True
        self._fit()
        first = self.entries["title" if len(self.paths) == 1 else "album"]
        first.focus_set()
        first.select_range(0, "end")

    def _changed(self, f: str) -> bool:
        text, start = self.vars[f].get(), self.initial.get(f)
        if start is None:
            return bool(tagedit.clean_value(text))
        return tagedit.clean_value(text) != tagedit.clean_value(start)

    def changes(self) -> Dict[str, str]:
        """The fields whose text differs from what they started with (a "several
        values" field: when something was typed)."""
        return {f: self.vars[f].get() for f in tagedit.FIELDS if self._changed(f)}

    # ---- Acquire Metadata… and searching the web (2.3) ------------------------------
    def acquire(self):
        """Acquire Metadata…: the MusicBrainz window for the same songs (this window stays)."""
        if self.saving:
            return None
        return self.app.musicbrainz(self.tracks, self.album, self.heading, self.subheading)

    def files_changed(self, paths: List[str]) -> None:
        """Other windows (MusicBrainz) wrote into these files: read the songs again and
        show the new values in the fields nothing was typed into."""
        if not self.ready or self.saving or self.done or not set(paths) & set(self.paths):
            return
        threading.Thread(target=self._read, args=("retags",), daemon=True, name="realhires-edit-reread").start()

    def _refill(self, tags: List[dict]) -> None:
        if self.saving or self.done:
            return
        fresh = tagedit.common_values(tags)
        for f in tagedit.FIELDS:
            if not self._changed(f):
                self.vars[f].set(fresh[f] or "")
                self._note(f, fresh[f])
            self.initial[f] = fresh[f]
        self._fit()

    def search_values(self) -> Dict[str, str]:
        """What the Search buttons search with: the fields as they are now (several songs:
        without the title, so the album is searched for)."""
        values = {f: tagedit.clean_value(self.vars[f].get()) for f in ("title", "artist", "album", "album_artist")}
        if len(self.paths) > 1:
            values["title"] = ""
        return values

    def search_tip(self, f: str) -> str:
        return tr("Search the web for the {field}", field=tagedit.search_name(f, i18n.language()))

    def search_url(self, f: str) -> str:
        return tagedit.web_search_url(f, self.search_values(), i18n.language())

    def search(self, f: str) -> None:
        """A field's Search button: the browser searches for it, and the next text copied
        within two minutes is put into the field."""
        if not self.ready or self.saving or self.done:
            return
        self.stop_watch()
        error = self.app.open_url(self.search_url(f))
        if error:
            self.watch_note.configure(text=error)
            return
        self.watch = {"field": f, "start": self.app.clipboard_text(), "until": self.clock() + tagedit.WATCH_SECONDS}
        self.watch_note.configure(text=tr(self.WATCHING))
        self._watch_job = self.win.after(tagedit.WATCH_POLL_MS, self._watch_tick)

    def stop_watch(self) -> None:
        if self._watch_job is not None:
            try:
                self.win.after_cancel(self._watch_job)
            except tk.TclError:
                pass
            self._watch_job = None
        if self.watch is not None:
            self.watch = None
            try:
                self.watch_note.configure(text="")
            except tk.TclError:
                pass

    def watch_step(self) -> Optional[str]:
        """Look at the clipboard once; the field filled in, or None."""
        w = self.watch
        if w is None:
            return None
        if self.clock() > w["until"]:
            self.stop_watch()
            return None
        text = self.app.clipboard_text()
        value = tagedit.clipboard_value(text) if text != w["start"] else ""
        if not value:
            return None
        f = w["field"]
        self.stop_watch()
        self.vars[f].set(value)
        self.filled = f
        self.watch_note.configure(text=tr(self.FILLED))
        e = self.entries[f]
        e.configure(style="Flash.P.TEntry")
        self.win.after(self.FLASH_MS, lambda: self._unflash(e))
        return f

    def _unflash(self, e: ttk.Entry) -> None:
        try:
            e.configure(style="P.TEntry")
        except tk.TclError:
            pass

    def _watch_tick(self) -> None:
        self._watch_job = None
        try:
            if not self.win.winfo_exists():
                return
        except tk.TclError:
            return
        self.watch_step()
        if self.watch is not None:
            self._watch_job = self.win.after(tagedit.WATCH_POLL_MS, self._watch_tick)

    def save(self) -> None:
        if not self.ready or self.saving or self.done:
            return
        self.stop_watch()
        changes = self.changes()
        try:
            changes = tagedit.check_changes(changes)
        except tagedit.TagError as exc:
            self.error.configure(text=tr_text(str(exc)))
            self._fit()
            return
        self.error.configure(text="")
        if not changes:
            self.close()
            return
        self.saving = True
        for e in self.entries.values():
            e.state(["disabled"])
        for b in self.search_bs.values():
            b.set(enabled=False)
        self.save_b.pack_forget()
        self.cancel_b.pack_forget()
        self.acquire_b.pack_forget()
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
        self._fit()

    def cancel(self) -> None:
        if not self.saving:
            self.close()

    def close(self) -> None:
        if self.saving:
            return
        self.stop_watch()
        if self.app.edit_window is self:
            self.app.edit_window = None
        try:  # a size the user chose (it differs from what the contents ask for) is kept
            size = (self.win.winfo_width(), self.win.winfo_height())
            if size != (self.win.winfo_reqwidth(), self.win.winfo_reqheight()) and min(size) > 1:
                self.app.edit_size = size
        except tk.TclError:
            pass
        close_window(self.win)


# "Giving 3 songs the genre “Jazz”" (the heading of the TagJobWindow), whole sentences per grouping
GIVING = {
    "genres": (N_("Giving 1 song the genre “{name}”"), N_("Giving {n} songs the genre “{name}”")),
    "composers": (N_("Giving 1 song the composer “{name}”"), N_("Giving {n} songs the composer “{name}”")),
    "years": (N_("Giving 1 song the year “{name}”"), N_("Giving {n} songs the year “{name}”")),
}


class TagJobWindow:
    """Renaming a genre, composer or year, or merging genres (player_pages.rename_group()
    / merge_groups()): tagedit.rename_group() writes the new name into every file in a
    thread, the window shows how far it is, then the outcome as Edit Info does
    (tagedit.save_summary(), files that could not be changed included), and the library
    is read again."""

    def __init__(self, app: "PlayerApp", title: str, kind: str, paths: List[str], name: str):
        from .settings import Settings

        self.app, t = app, app.theme
        self.kind, self.paths, self.name = kind, list(paths), name
        self.cfg = Settings.from_dict(app.cfg.to_dict())  # the thread gets its own copy
        self.q: "queue.Queue" = queue.Queue()
        self.done = False
        self.outcomes: list = []
        self.headline, self.lines = "", []
        self.win = _window(app, title)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<<Close>>", lambda e: self.close())
        box = frame(self.win, t)
        box.pack(fill="both", expand=True, padx=24, pady=18)
        n = len(self.paths)
        one, many = GIVING[kind]
        label(box, t, tr(one, name=name) if n == 1 else tr(many, n=f"{n:,}", name=name), "h2", wraplength=480,
              justify="left").pack(anchor="w")
        self.status = label(box, t, tr("Starting…"), "small", fg="text2")
        self.status.pack(anchor="w", pady=(6, 0))
        self.bar = ttk.Progressbar(box, maximum=max(n, 1), length=440, mode="determinate")
        self.bar.pack(anchor="w", fill="x", pady=(8, 0))
        self.summary = label(box, t, "", "body", wraplength=480, justify="left")
        self.summary.pack(anchor="w", pady=(8, 0))
        self.btns = frame(box, t)
        self.btns.pack(fill="x", pady=(12, 0))
        self.win.minsize(500, 0)
        threading.Thread(target=self._work, daemon=True, name="realhires-rename").start()
        self.win.after(100, self._poll)

    def _work(self) -> None:
        try:
            out = tagedit.rename_group(self.kind, self.paths, self.name, self.cfg,
                                       progress=lambda i, n, p: self.q.put(("progress", i, n)))
        except Exception as exc:  # noqa: BLE001 (rename_group never raises; this is a last resort)
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
                if kind == "progress":
                    i, n = rest
                    self.bar.configure(value=i)
                    if i < n:
                        self.status.configure(text=tr("Saving {i} of {n}…", i=f"{i + 1:,}", n=f"{n:,}"))
                elif kind == "saved":
                    self._saved(rest[0])
                    return
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    def _saved(self, outcomes: list) -> None:
        self.done = True
        self.outcomes = outcomes
        self.app.tags_saved([o.path for o in outcomes if o.ok])
        self.headline, self.lines = tagedit.save_summary(outcomes)
        self.status.configure(text="")
        self.bar.pack_forget()
        more = len(self.lines) - 12
        self.summary.configure(text="\n\n".join([tr_text(self.headline)] + [tr_text(x) for x in self.lines[:12]] +
                                                 ([tr("… and {n} more.", n=f"{more:,}")] if more > 0 else [])))
        PillButton(self.btns, self.app.theme, tr("Close"), command=self.close).pack(side="right")

    def close(self) -> None:
        if not self.done:
            return  # the files are being written; the window closes when that is done
        close_window(self.win)


__all__ = ["PlaybackInfoWindow", "EqualizerWindow", "EditInfoWindow", "TagJobWindow", "NameDialog", "ask_name"]


def forget_covers(app: "PlayerApp", keys) -> None:
    """Drop these albums' (and songs') covers from the player's cover cache, so every
    page draws them again from the changed files; the cache's listeners redraw."""
    keys = set(keys)
    cache = app.covers
    if hasattr(cache, "forget"):  # a cache that knows how
        cache.forget(keys)
        return
    gone = set()
    for store_ in (getattr(cache, "images", {}), getattr(cache, "png", {})):
        for k in [k for k in store_ if isinstance(k, tuple) and k and k[0] in keys]:
            del store_[k]
            gone.add(k)
    for key, size in gone:
        for fn in list(getattr(cache, "listeners", [])):
            try:
                fn(key, size)
            except tk.TclError:
                pass


class CoverWindow:
    """The Cover window (2.1, docs/player-spec.md "Inspecting and changing a cover"):
    the cover of a song's album as large as the window allows (FFmpeg makes a PNG that
    fits, covers.fit_png(), because Tk cannot decode JPEG), its size in pixels, format
    and where it comes from (covers.cover_source()), and Change cover…, Search Cover
    Online (2.3: Google Images in the browser, covers.cover_search_url()), Save image as…,
    Remove cover, Show album and Close.  Changing and removing run in a thread
    (tagedit.set_cover()/remove_cover()) for all the album's songs or this song only,
    with a progress line and tagedit.cover_summary() afterwards; the player's covers
    are drawn again and the library reads the songs again.  Results reach Tk through
    `self.q`."""

    MIN_SHOW = 120

    def __init__(self, app: "PlayerApp", path: str):
        from .musiclib import Track
        from .settings import Settings

        self.app, t = app, app.theme
        self.path = path
        track = app.track(path) or Track(path)
        self.track = track
        self.album = None if getattr(track, "missing", False) else app.album_of(track)
        songs = self.album["tracks"] if self.album else [track]
        self.album_paths: List[str] = [x.path for x in songs if not getattr(x, "missing", False)]
        if path not in self.album_paths:
            self.album_paths.insert(0, path)
        self.cfg = Settings.from_dict(app.cfg.to_dict())  # the threads get their own copy
        self.q: "queue.Queue" = queue.Queue()
        self.source = None              # covers.CoverSource, or None when there is no cover
        self.reading = True
        self.busy = False
        self.outcomes: list = []
        self.summary_text = ""
        self.photo: Optional[tk.PhotoImage] = None
        self.previews: Dict[int, Optional[bytes]] = {}
        self._asked: set = set()
        self._gen = 0
        self._resize_job: Optional[str] = None
        self.win = _window(app, tr("Cover"))
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<<Close>>", lambda e: self.close())
        self.win.resizable(True, True)
        box = frame(self.win, t)
        box.pack(fill="both", expand=True, padx=24, pady=18)
        if self.album:
            n = len(self.album_paths)
            heading = tr_name(self.album["title"])
            songs_n = tr("1 song") if n == 1 else tr("{n} songs", n=f"{n:,}")
            sub = " · ".join(x for x in (tr_name(self.album["artist"]), songs_n) if x)
        else:
            heading = track.display_title
            sub = " · ".join(x for x in (track.artist, track.album) if x)
        label(box, t, heading, "h2", wraplength=560, justify="left").pack(anchor="w")
        if sub:
            label(box, t, sub, "body", fg="text2", wraplength=560, justify="left").pack(anchor="w")
        bottom = frame(box, t)  # packed before the picture, so a small window shrinks the picture, not this
        bottom.pack(fill="x", side="bottom")
        self.canvas = tk.Canvas(box, width=440, height=420, highlightthickness=0, bd=0)
        t.reg(self.canvas, bg="bg")
        self.canvas.pack(fill="both", expand=True, pady=(12, 10))
        self.canvas.bind("<Configure>", self._resized)
        t.on_change(self._draw, self.win)
        self.details = label(bottom, t, "", "body")
        self.details.pack(anchor="w")
        self.where = label(bottom, t, "", "body", fg="text2")
        self.where.pack(anchor="w")
        opts = frame(bottom, t)
        opts.pack(fill="x", pady=(10, 0))
        n = len(self.album_paths)
        self.scope = tk.StringVar(value="album" if n > 1 else "song")
        if n > 1:
            ttk.Radiobutton(opts, text=tr("All {n} songs of this album", n=f"{n:,}"), value="album",
                            variable=self.scope, style="P.TRadiobutton").pack(anchor="w")
            ttk.Radiobutton(opts, text=tr("This song only"), value="song", variable=self.scope,
                            style="P.TRadiobutton").pack(anchor="w")
        self.also_folder = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text=tr("Also save it as cover.jpg in the album's folder"), variable=self.also_folder,
                        style="P.TCheckbutton").pack(anchor="w", pady=(4, 0))
        self.status = label(bottom, t, tr("Reading the cover…"), "small", fg="text2")
        self.status.pack(anchor="w", pady=(8, 0))
        self.summary = label(bottom, t, "", "body", wraplength=600, justify="left")
        self.summary.pack(anchor="w")
        btns = frame(bottom, t)
        btns.pack(fill="x", pady=(12, 0))
        self.close_b = PillButton(btns, t, tr("Close"), kind="plain", command=self.close)
        self.close_b.pack(side="right")
        self.album_b = PillButton(btns, t, tr("Show album"), kind="plain", command=self.show_album)
        self.album_b.pack(side="right", padx=(16, 8))  # a gap from the left group when the window is narrow
        self.change_b = PillButton(btns, t, tr("Change cover…"), command=self.change)
        self.change_b.pack(side="left")
        self.search_b = PillButton(btns, t, tr("Search Cover Online"), kind="plain", command=self.search_online)
        self.search_b.pack(side="left", padx=(8, 0))
        self.save_b = PillButton(btns, t, tr("Save image as…"), kind="plain", command=self.save_image)
        self.save_b.pack(side="left", padx=(8, 0))
        self.remove_b = PillButton(btns, t, tr("Remove cover"), kind="plain", command=self.remove)
        self.remove_b.pack(side="left", padx=(8, 0))
        self.win.minsize(700, 560)
        self._buttons()
        self._start_read()
        self.win.after(100, self._poll)

    # ---- which songs ----------------------------------------------------------
    def shown_paths(self) -> List[str]:
        """The songs whose cover is shown: this one first, then the album's."""
        return [self.path] + [p for p in self.album_paths if p != self.path]

    def target_paths(self) -> List[str]:
        return list(self.album_paths) if self.scope.get() == "album" else [self.path]

    def folder(self) -> str:
        return os.path.dirname(os.path.abspath(self.path))

    # ---- threads ------------------------------------------------------------
    def _start_read(self) -> None:
        self.reading = True
        self._gen += 1
        gen, paths = self._gen, self.shown_paths()

        def work():
            from . import covers

            try:
                src = covers.cover_source(paths)
            except Exception:  # noqa: BLE001 (an unreadable cover is shown as none)
                src = None
            self.q.put(("source", gen, src))

        threading.Thread(target=work, daemon=True, name="realhires-cover-read").start()

    def _want_preview(self, size: int) -> None:
        if self.source is None or size in self._asked:
            return
        self._asked.add(size)
        gen, data = self._gen, self.source.data

        def work():
            from . import covers

            try:
                png = covers.fit_png(data, size, enlarge=True)
            except Exception:  # noqa: BLE001
                png = None
            self.q.put(("preview", gen, size, png))

        threading.Thread(target=work, daemon=True, name="realhires-cover-preview").start()

    def _work(self, image: Optional[bytes], paths: List[str], also_folder: bool) -> None:
        from . import covers

        progress = lambda i, n, p: self.q.put(("progress", i, n))  # noqa: E731
        notes: List[str] = []
        try:
            if image is None:
                outcomes = tagedit.remove_cover(paths, self.cfg, progress)
                kept = covers.folder_image(self.folder())
                if kept:
                    notes.append(tagedit.folder_kept_note(kept))
            else:
                outcomes = tagedit.set_cover(paths, image, "", self.cfg, progress)
                if also_folder:
                    try:
                        notes.append(tagedit.folder_saved_note(covers.save_folder_cover(self.folder(), image)))
                    except OSError as exc:
                        notes.append(tagedit.folder_failed_note(covers.folder_cover_name(self.folder(), image), exc))
        except Exception as exc:  # noqa: BLE001 (set_cover never raises; this is a last resort)
            outcomes = [tagedit.TagOutcome(p, False, tr("Could not change the cover: {error}", error=exc))
                        for p in paths]
        self.q.put(("done", image is None, outcomes, notes))

    def _poll(self) -> None:
        try:
            if not self.win.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "source":
                    gen, src = rest
                    if gen == self._gen:
                        self._got_source(src)
                elif kind == "preview":
                    gen, size, png = rest
                    if gen == self._gen:
                        self.previews[size] = png
                        self._draw()
                elif kind == "progress":
                    i, n = rest
                    if i < n:
                        if n > 1:
                            text = (tr("Removing the cover of {i} of {n}…", i=f"{i + 1:,}", n=f"{n:,}")
                                    if self._removing else
                                    tr("Changing the cover of {i} of {n}…", i=f"{i + 1:,}", n=f"{n:,}"))
                        else:
                            text = (tr("Removing the cover of the song…") if self._removing else
                                    tr("Changing the cover of the song…"))
                        self.status.configure(text=text)
                elif kind == "done":
                    self._done(*rest)
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    # ---- the window ---------------------------------------------------------
    def _got_source(self, src) -> None:
        self.reading = False
        self.source = src
        self.previews, self._asked = {}, set()
        if src is None:
            self.details.configure(text=tr("No cover"))
            self.where.configure(text=tr("Neither the songs nor the album's folder hold a picture."))
        else:
            self.details.configure(text=tr_text(src.details))
            self.where.configure(text=tr_text(src.where))
        if not self.busy:
            self.status.configure(text="")
        self._buttons()
        self._draw()

    def _box(self) -> int:
        try:
            return max(self.MIN_SHOW, min(self.canvas.winfo_width(), self.canvas.winfo_height()) - 4)
        except tk.TclError:
            return self.MIN_SHOW

    def _resized(self, event=None) -> None:
        if self._resize_job is not None:
            self.win.after_cancel(self._resize_job)
        self._resize_job = self.win.after(150, self._draw)

    def _draw(self) -> None:
        self._resize_job = None
        c = self.canvas
        try:
            c.delete("all")
            w, h = c.winfo_width(), c.winfo_height()
        except tk.TclError:
            return
        t = self.app.theme
        if self.source is None:
            text = tr("Reading the cover…") if self.reading else tr("No cover")
            c.create_text(w / 2, h / 2, text=text, fill=t.c("text2"), font=t.f("h2"))
            return
        size = self._box()
        step = size - size % 20  # a new picture every 20 pixels of window size
        png = self.previews.get(step)
        if png is None:
            near = [s for s, p in self.previews.items() if p]
            self._want_preview(step)
            if not near:
                c.create_text(w / 2, h / 2, text=tr("Drawing…"), fill=t.c("text2"), font=t.f("body"))
                return
            png = self.previews[min(near, key=lambda s: abs(s - step))]
        try:
            import base64

            self.photo = tk.PhotoImage(master=c, data=base64.b64encode(png).decode("ascii"), format="png")
        except tk.TclError:
            c.create_text(w / 2, h / 2, text=tr("The picture cannot be shown."), fill=t.c("text2"),
                          font=t.f("body"))
            return
        pw, ph = self.photo.width(), self.photo.height()
        if pw > size or ph > size:  # a bigger picture made earlier: shrink by whole steps
            f = max(-(-pw // size), -(-ph // size))
            self.photo = self.photo.subsample(f, f)
        c.create_image(w / 2, h / 2, image=self.photo, anchor="center")

    def _buttons(self) -> None:
        idle = not self.busy and not self.reading
        self.change_b.set(enabled=idle)
        self.remove_b.set(enabled=idle)
        self.save_b.set(enabled=idle and self.source is not None)
        self.album_b.set(enabled=not self.busy and self.album is not None)
        self.close_b.set(enabled=not self.busy)

    _removing = False

    def _start(self, image: Optional[bytes]) -> None:
        self.busy, self._removing = True, image is None
        self.summary.configure(text="")
        self.summary_text = ""
        n = len(self.target_paths())
        if image is None:
            text = tr("Removing the cover of {n} songs…", n=f"{n:,}") if n > 1 else tr("Removing the cover…")
        else:
            text = tr("Changing the cover of {n} songs…", n=f"{n:,}") if n > 1 else tr("Changing the cover…")
        self.status.configure(text=text)
        self._buttons()
        threading.Thread(target=self._work, args=(image, self.target_paths(), self.also_folder.get()), daemon=True,
                         name="realhires-cover-save").start()

    def _done(self, removing: bool, outcomes: list, notes: List[str]) -> None:
        self.busy = False
        self.outcomes = outcomes
        head, lines = tagedit.cover_summary(outcomes, removing, notes)
        self.summary_text = "\n".join([tr_text(head)] + [tr_text(x) for x in lines])
        self.summary.configure(text=self.summary_text)
        self.status.configure(text="")
        changed = [o.path for o in outcomes if o.ok and not o.message]
        keys = set(self.album_paths)
        if self.album:
            keys.add(self.album["key"])
        forget_covers(self.app, keys)
        if changed:
            self.app.tags_saved(changed)
        self._start_read()
        self._buttons()

    def change(self) -> None:
        """Change cover…: choose a JPEG or PNG image."""
        from tkinter import filedialog

        if self.busy or self.reading:
            return
        name = filedialog.askopenfilename(parent=self.win, title=tr("Choose the new cover"),
                                          filetypes=[(tr("JPEG or PNG images"),
                                                      "*.jpg *.jpeg *.png *.JPG *.JPEG *.PNG"),
                                                     (tr("All files"), "*")])
        if name:
            self.change_to(name)

    def change_to(self, image_path: str) -> bool:
        """Make the picture in this file the cover (of the songs the choice names)."""
        if self.busy:
            return False
        try:
            with open(image_path, "rb") as fh:
                data = fh.read()
            tagedit.check_picture(data)
        except (OSError, tagedit.TagError) as exc:
            self.summary.configure(text=tr("{file} cannot be used: {error}", file=os.path.basename(image_path),
                                           error=tr_text(str(exc))))
            return False
        self._start(data)
        return True

    def remove(self, confirm: bool = True) -> bool:
        """Remove cover: after a confirmation, the covers inside the songs go."""
        from tkinter import messagebox

        if self.busy or self.reading:
            return False
        n = len(self.target_paths())
        question = (tr("Remove the cover inside all {n} songs of this album? A picture in the album's folder is "
                       "not deleted.", n=f"{n:,}") if n > 1 else
                    tr("Remove the cover inside this song? A picture in the album's folder is not deleted."))
        if confirm and not messagebox.askyesno(tr("Remove Cover"), question, parent=self.win):
            return False
        self._start(None)
        return True

    def search_url(self) -> str:
        """Search Cover Online's address: Google Images for the album's artist and title
        (a song without an album: its artist and title), from the song's tags."""
        from . import covers

        t = self.track
        return covers.cover_search_url(t.album_artist, t.artist, t.album, t.title or t.display_title)

    def search_online(self) -> None:
        """Search Cover Online (2.3): the browser looks for the cover; a picture saved from
        it can then be chosen with Change cover…."""
        error = self.app.open_url(self.search_url())
        self.status.configure(text=error)

    def save_image(self) -> None:
        """Save image as…: the picture as it is stored."""
        from tkinter import filedialog

        if self.source is None:
            return
        kind = (tr("{format} image", format=self.source.format) if self.source.format else tr("Image"))
        name = filedialog.asksaveasfilename(parent=self.win, title=tr("Save the cover as"), initialfile="cover"
                                            + self.source.ext, defaultextension=self.source.ext,
                                            filetypes=[(kind, "*" + self.source.ext), (tr("All files"), "*")])
        if name:
            self.save_image_to(name)

    def save_image_to(self, path: str) -> bool:
        if self.source is None:
            return False
        try:
            with open(path, "wb") as fh:
                fh.write(self.source.data)
        except OSError as exc:
            self.status.configure(text=tr("Could not save the picture: {error}", error=exc))
            return False
        self.status.configure(text=tr("Saved the picture as {file}.", file=os.path.basename(path)))
        return True

    def show_album(self) -> None:
        if self.album is not None and not self.busy:
            album = self.album
            self.close()
            self.app.open_album(album)

    def close(self) -> None:
        if self.busy:
            return
        if getattr(self.app, "cover_window", None) is self:
            self.app.cover_window = None
        close_window(self.win)


__all__ = ["PlaybackInfoWindow", "EqualizerWindow", "EditInfoWindow", "CoverWindow", "NameDialog", "ask_name",
           "forget_covers"]
