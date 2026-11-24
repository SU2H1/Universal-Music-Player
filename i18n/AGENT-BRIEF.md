# Brief for translating one part of the apps (Universal Music Player 2.1)

The user asked for a language setting with English and Japanese. The repository is
`/home/user/universal-music-player` (branch `claude/tender-volta-n6x67y`, already checked out). It holds two desktop
apps that must behave the same: Python/Tk (`realhires/gui.py`, `realhires/player_*.py`) and Rust/egui
(`rust/crates/gui/src/*.rs`). `CLAUDE.md` describes everything; `docs/player-spec.md` specifies the player.

## What already exists (do not change these files)

- `realhires/i18n.py` and `rust/crates/core/src/i18n.rs`: the translation core. The catalog is
  `realhires/assets/lang/ja.json` (generated; Rust includes it at build time, so rebuild after a merge).
- The setting `language` (Settings → Appearance; values "English" and "日本語"). The Python app calls
  `i18n.set_language(cfg.language)` in `PlayerApp.__init__` and `gui.scanner_main()`; the Rust app in `Player::new`.
- `i18n/GLOSSARY.md`: the Japanese terms and style. Follow it.
- `tests/i18n_keys.py`: `python tests/i18n_keys.py realhires/gui.py` (or a Rust path such as
  `rust/crates/gui/src/app.rs`) prints, as JSON, every text of those files that has no translation yet;
  `python tests/i18n_keys.py --merge` rebuilds `ja.json` from `i18n/ja/*.json` (safe to run at any time, by anyone).

## The API

Python (`from .i18n import tr, tr_name, tr_text, N_`):
- `tr("Play")` → the text in the current language. `tr("Playing from {name}", name=x)` translates the template,
  then fills the placeholders. Turn every f-string the user sees into such a template: `f"{n} songs"` becomes
  `tr("{n} songs", n=n)`. Keep English plural branches as they are (`tr("1 song") if n == 1 else tr("{n} songs", n=n)`);
  Japanese gives both the same text.
- `tr_name(name)` for names that come from the library (artist, album, genre, composer, year): it translates only the
  core's placeholders for missing tags ("Unknown Album", …) and never a real name.
- `tr_text(text)` for finished texts that the shared core builds (verdict summaries and reasons, Fix, lyrics, Edit
  Info and stats messages, the time-left estimate, error messages): it matches catalog patterns. You do not write
  those patterns; only call `tr_text()` where such a text is shown and list those places in your report.
- `N_("…")` marks a literal in a table or constant (e.g. `VERDICT_STYLE`, column definitions, tile lists) so the
  extractor sees it; it returns the text unchanged. Translate where it is shown: `tr(label)`.
- Labels of the shared core that already are tables (`playqueue.SHUFFLE_LABELS`, `REPEAT_LABELS`, `eq.PRESETS` names,
  `eq.BAND_TYPE_LABELS`, `eq.CUSTOM`, `themes.COLOR_LABELS`, built-in theme names, `themes.SYSTEM`,
  `tagedit.LABELS`/`HINTS`, `stats.PERIODS`/`WEEKDAYS`/`MONTHS`, every Settings field's `group`, `label`, `help` and
  `choices`, the verdict names `FAKE LOSSLESS` …): show them as `tr(value)`. Their translations are written by the
  lead (in `i18n/ja/00-core.json`); you do not translate them.

Rust (`use realhires::{tr, n_};` and `realhires::i18n::{self, tr_name, tr_text}`):
- `tr!("Play")` → `&str`; `tr!("Playing from {name}", name = x)` → `String` (placeholders are filled with
  `to_string()`). Only string literals go into `tr!`; for a value use `i18n::tr(value)`.
- `i18n::tr_name(name)`, `i18n::tr_text(text)`, `n_!("…")` as in Python.

## Rules

1. Translate only what is shown to the user: window titles, menus, buttons, labels, tab names, column headings,
   tooltips, placeholders, status lines, messages, dialog texts. Never translate identifiers, dict keys, page names,
   values that code compares or stores (setting values, verdict codes, mode keys), tags, file names, paths, CLI or log
   output, and nothing that tests or other code match on internally. Where a displayed text is also used as a key
   (a Notebook tab text looked up by name, a combobox value written to the settings), keep the key in English and map
   it: e.g. a combobox shows `[tr(c) for c in choices]` and writes back `choices[index]`.
2. The Settings pages/windows: show `tr(group)` for the tab or section name, `tr(label)`, `tr(help)`, and `tr(choice)`
   for each choice, but store the English value. The `language` choices are shown as they are (their own names).
3. Every new `tr()`/`tr!()` text gets a Japanese translation in your file `i18n/ja/<your part>.json`
   (`{"strings": {"English": "日本語", …}, "patterns": {}}`), in natural Japanese following the glossary, with the
   same placeholders. When you are done, `python tests/i18n_keys.py <your files>` must print `{}` (it also counts the
   other parts' files, so a text another part already translated counts). Run `--merge` and rebuild before testing in
   Japanese.
4. Behaviour must not change in English: every existing test must still pass. Texts become longer or shorter in
   Japanese; make sure nothing important is cut off or overlaps in Japanese where a width is fixed (look at it).
5. Edit only the files of your part and your own `i18n/ja/<part>.json` (plus the test file named in your task). Do not
   edit `realhires/i18n.py`, `rust/crates/core/src/i18n.rs`, `tests/i18n_keys.py`, `ja.json` by hand, docs, or other
   parts' files; if you need a change there, say so in your report. Do not commit, push or create branches: the lead
   commits.
6. Python: use `/home/user/venv312/bin/python` (Python 3.12 with Tk, NumPy, miniaudio, pytest); GUI tests need
   `xvfb-run -a`. Rust: `cd rust && cargo build --release` and `cargo test --release -p realhires-gui` (another helper
   may be editing other files of the same crate at the same time; if the build fails in a file that is not yours, wait
   a minute and try again, and never edit it). In Rust tests use `realhires::i18n::set_thread_language("日本語")`
   (only the calling thread) instead of `set_language`, because tests share the process.
7. Look at the result in Japanese (Tk: run the app under `xvfb-run` with `REALHIRES_SETTINGS` pointing at a settings
   file holding `{"language": "日本語"}` and the other `REALHIRES_*` files in a temp folder, take screenshots with
   `import -window root shot.png` and view them with the Read tool; Rust: the same with the release binary, or
   `xdotool` as `CLAUDE.md` describes). Japanese fonts are installed (`fonts-noto-cjk`).

## Report

Reply with: the files you changed; how many texts you wrapped and translated; every place where you call
`tr_text()` and which core function produced that text; anything you left in English on purpose and why; texts you
could not translate well; the tests you ran and their results (with failures quoted); and anything the lead must do
(e.g. a change to the shared files).
