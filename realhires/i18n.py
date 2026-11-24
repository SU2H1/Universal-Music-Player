"""Languages of the desktop apps (2.1): English and Japanese.

Every text the apps show is written in English in the code and passed through
``tr()``, which looks it up in the catalog of the chosen language
(``realhires/assets/lang/<code>.json``).  The Rust app reads the same catalog
file (``rust/crates/core/src/i18n.rs`` includes it), so both apps say the same
thing in both languages.  A text missing from the catalog is shown in English.

The catalog has two tables:

* ``strings``: exact texts.  A text may hold placeholders such as ``{n}``,
  which ``tr(text, n=3)`` fills in after translating, so the translation can
  put them where its grammar wants them.
* ``patterns``: texts that the shared core builds from parts (a verdict's
  summary, a Fix or lyrics message) and that the apps only receive finished.
  ``tr_text()`` matches them against these templates; each ``{name}`` matches
  any non-empty text (the shortest that lets the rest match) and is put into
  the translation.

The command-line tool always writes English, so scripts that read its output
keep working, and so do the saved results and settings files, which store the
English texts.  The ``language`` setting holds the language's own name
("English", "日本語"), as its menu shows it.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple

LANGUAGES = ("English", "日本語")   # the choices of the `language` setting, each in its own language
CODES = {"English": "en", "日本語": "ja"}
DEFAULT_LANGUAGE = "English"
SENTENCE_JOIN = {"ja": ""}   # how translated sentences are joined again ("" = no space, as in Japanese)
PART_JOIN = {"ja": "；"}      # how the "; "-separated parts of a summary are joined again

# Names the core puts in place of a missing tag; tr_name() translates only these, never a real name.
PLACEHOLDER_NAMES = ("Unknown Artist", "Unknown Album", "Unknown Genre", "Unknown Composer", "Unknown Year",
                     "Unknown artist", "Unknown album")
# The verdicts as the result files store them; the apps show them translated.
VERDICT_NAMES = ("FAKE LOSSLESS", "FAKE HI-RES", "SUSPICIOUS", "REAL HI-RES", "NOT HI-RES", "LOSSY FORMAT", "ERROR")

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::num)?\}")
# In a pattern, {name:num} matches only a number ("44.1", "1,234", "−4.5", "50%"); {name} matches any text.
NUM_CHARS = frozenset("0123456789.,+-−%")

MAX_DEPTH = 3   # how deep the values taken by patterns are translated in turn

_current = "en"
_catalogs: Dict[str, "Catalog"] = {}


def lang_dir() -> str:
    """The folder of the catalog files (inside the package, also when frozen)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "lang")


def catalog_path(code: str) -> str:
    return os.path.join(lang_dir(), f"{code}.json")


def placeholders(text: str) -> List[str]:
    """The placeholder names of a text, in order (``{n}`` gives "n")."""
    return _PLACEHOLDER.findall(text)


def _split(template: str) -> Tuple[List[str], List[str], List[bool]]:
    """A template as literal parts, the placeholder names between them (len(lits) == len(names) + 1)
    and whether each placeholder takes only a number."""
    lits: List[str] = []
    names: List[str] = []
    nums: List[bool] = []
    pos = 0
    for m in _PLACEHOLDER.finditer(template):
        lits.append(template[pos:m.start()])
        names.append(m.group(1))
        nums.append(m.group(0).endswith(":num}"))
        pos = m.end()
    lits.append(template[pos:])
    return lits, names, nums


def _is_num(text: str) -> bool:
    return bool(text) and all(c in NUM_CHARS for c in text) and any(c.isdigit() for c in text)


def match_template(template: str, text: str) -> Optional[Dict[str, str]]:
    """The placeholder values when `text` is `template` filled in, else None.

    Each placeholder takes the shortest non-empty text that lets the rest of
    the template match (the Rust version matches the same way)."""
    lits, names, nums = _split(template)
    if not names:
        return {} if text == template else None
    if not text.startswith(lits[0]):
        return None
    got: Dict[str, str] = {}

    def walk(i: int, pos: int) -> bool:
        # placeholder i starts at pos; lits[i + 1] follows it
        lit = lits[i + 1]
        last = i == len(names) - 1
        if last:
            end = len(text) - len(lit)
            if end <= pos or not text.endswith(lit):
                return False
            if nums[i] and not _is_num(text[pos:end]):
                return False
            got[names[i]] = text[pos:end]
            return True
        start = pos + 1
        while True:
            at = text.find(lit, start)
            if at < 0:
                return False
            if (not nums[i] or _is_num(text[pos:at])) and walk(i + 1, at + len(lit)):
                got[names[i]] = text[pos:at]
                return True
            start = at + 1

    return got if walk(0, len(lits[0])) else None


def fill(template: str, values: Dict[str, object]) -> str:
    """Put `values` into the ``{name}`` placeholders; unknown names stay as they are."""
    if not values:
        return template
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


class Catalog:
    """The texts of one language."""

    def __init__(self, data: Optional[dict] = None):
        data = data if isinstance(data, dict) else {}
        strings = data.get("strings")
        self.strings: Dict[str, str] = {k: v for k, v in (strings or {}).items()
                                        if isinstance(k, str) and isinstance(v, str) and v}
        self.patterns: List[Tuple[str, str]] = []
        for k, v in (data.get("patterns") or {}).items():
            if isinstance(k, str) and isinstance(v, str) and v and placeholders(k):
                self.patterns.append((k, v))
        self.name = data.get("language") if isinstance(data.get("language"), str) else ""

    def text(self, text: str, depth: int = 0) -> Optional[str]:
        """The translation of one whole text (exact, then a pattern), or None.  The values a
        pattern takes from the text are translated the same way (up to MAX_DEPTH deep), so
        "Graphic: Rock" can become "グラフィック：ロック"."""
        hit = self.strings.get(text)
        if hit is not None:
            return hit
        if depth >= MAX_DEPTH:
            return None
        for template, translated in self.patterns:
            values = match_template(template, text)
            if values is not None:
                return fill(translated, {k: self._value(v, depth + 1) for k, v in values.items()})
        return None

    def _value(self, v: str, depth: int) -> str:
        hit = self.text(v, depth)
        return v if hit is None else hit


def load_catalog(code: str) -> Catalog:
    if code not in _catalogs:
        data = None
        if code != "en":
            try:
                with open(catalog_path(code), encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = None
        _catalogs[code] = Catalog(data)
    return _catalogs[code]


def code_of(language: str) -> str:
    """The code of a `language` setting value ("日本語" -> "ja"); unknown values give "en"."""
    if language in CODES:
        return CODES[language]
    return language if language in CODES.values() else "en"


def set_language(language: str) -> str:
    """Use this language from now on (a setting value or a code); returns its code."""
    global _current
    _current = code_of(language or DEFAULT_LANGUAGE)
    return _current


def language() -> str:
    """The code of the language in use ("en", "ja")."""
    return _current


def tr(text: str, /, **values) -> str:
    """`text` in the language in use, with ``{name}`` placeholders filled from `values`."""
    if _current != "en":
        hit = load_catalog(_current).strings.get(text)
        if hit is not None:
            text = hit
    return fill(text, values) if values else text


def N_(text: str) -> str:
    """Marks a literal in a table as a text to translate (it is translated where it is shown, with tr())."""
    return text


def tr_name(name: str) -> str:
    """A name from the library (an artist, album, genre...): translated only when it is one of the
    core's placeholders for a missing tag ("Unknown Album"), so real names are never changed."""
    return tr(name) if name in PLACEHOLDER_NAMES else name


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def tr_text(text: str) -> str:
    """A finished text from the shared core (a summary, a reason, a message) in the language in use.

    The whole text is tried first (exact, then the patterns); a text made of
    parts joined by "; " is translated part by part, and a text of several
    sentences sentence by sentence.  Parts without a translation stay
    English."""
    if _current == "en" or not text:
        return text
    cat = load_catalog(_current)
    hit = cat.text(text)
    if hit is not None:
        return hit
    if "; " in text:
        parts = text.split("; ")
        out = [cat.text(p) for p in parts]
        if any(o is not None for o in out):
            return PART_JOIN.get(_current, "; ").join(o if o is not None else p for o, p in zip(out, parts))
    sentences = _SENTENCE_END.split(text)
    if len(sentences) > 1:
        out = [cat.text(s) for s in sentences]
        if any(o is not None for o in out):
            joiner = SENTENCE_JOIN.get(_current, " ")
            return joiner.join(o if o is not None else s for o, s in zip(out, sentences))
    return text


def reset() -> None:
    """Forget loaded catalogs (tests that change the catalog file)."""
    _catalogs.clear()
