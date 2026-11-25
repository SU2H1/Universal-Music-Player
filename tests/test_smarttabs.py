"""Custom tabs (2.3, realhires/smarttabs.py): rules on the library's songs, the
checks and their messages, the tab files and the tabs folder, and that the Rust
vectors (rust/crates/core/tests/python_tabs.json) still hold Python's answers."""

import json
import os
from pathlib import Path

import pytest

from realhires import smarttabs
from realhires.musiclib import Track

VECTORS = Path(__file__).resolve().parents[1] / "rust" / "crates" / "core" / "tests" / "python_tabs.json"
DAY = 86400


def R(field, op, value):
    return {"field": field, "op": op, "value": value}


def T(name="T", rules=(), match="all", sort="title", descending=False, limit=0):
    return {"format": smarttabs.TAB_FORMAT, "version": 1, "name": name, "match": match, "rules": list(rules),
            "sort": sort, "descending": descending, "limit": limit}


NOW = 1_800_000_000.0


def songs():
    ns = int(NOW * 1e9)
    return [
        Track("/m/a/1.flac", title="Zebra", artist="Ann", codec="flac", sample_rate=96000, bits=24,
              duration=700, mtime_ns=ns - 2 * DAY * 10 ** 9, genre="Jazz", year="2001"),
        Track("/m/a/2.flac", title="apple", artist="Bob", codec="flac", sample_rate=44100, bits=16,
              duration=200, mtime_ns=ns - 90 * DAY * 10 ** 9, genre="jazz", year="1990"),
        Track("/m/b/3.mp3", title="", artist="", album_artist="Cy", codec="mp3", sample_rate=44100,
              is_lossy=True, bit_rate=192000, duration=180, mtime_ns=ns - 10 * DAY * 10 ** 9, genre="Pop"),
    ]


def ctx(liked=(), disliked=()):
    return smarttabs.context(liked, disliked, NOW)


def test_templates_pick_the_songs_their_names_say():
    tracks = songs()
    names = {t["name"]: smarttabs.tab_songs(t, tracks, ctx()) for t in smarttabs.TEMPLATES}
    assert names == {"Hi-Res": ["/m/a/1.flac"], "Lossy": ["/m/b/3.mp3"],
                     "Added in the last 30 days": ["/m/a/1.flac", "/m/b/3.mp3"],  # newest first
                     "Long songs": ["/m/a/1.flac"]}
    for t in smarttabs.TEMPLATES:
        assert smarttabs.check_tab(t) == t


def test_rules_text_numbers_and_yes_no():
    tracks = songs()
    c = ctx(liked=["/m/a/2.flac"], disliked=["/m/b/3.mp3"])
    got = lambda *rules, **kw: smarttabs.tab_songs(T(rules=rules, **kw), tracks, c)  # noqa: E731
    assert got(R("genre", "is", "JAZZ ")) == ["/m/a/2.flac", "/m/a/1.flac"]  # case-folded, by title
    assert got(R("genre", "is_not", "jazz")) == ["/m/b/3.mp3"]
    assert got(R("title", "starts_with", "z")) == ["/m/a/1.flac"]
    assert got(R("title", "contains", "3")) == ["/m/b/3.mp3"]  # the file name stands in for a missing title
    assert got(R("artist", "is", "cy")) == ["/m/b/3.mp3"]  # the album artist for a missing artist
    assert got(R("artist", "not_contains", "b")) == ["/m/b/3.mp3", "/m/a/1.flac"]
    assert got(R("format", "is", "mp3")) == ["/m/b/3.mp3"]
    assert got(R("folder", "is", "/m/a")) == ["/m/a/2.flac", "/m/a/1.flac"]
    assert got(R("quality", "is", "Lossless")) == ["/m/a/2.flac"]
    assert got(R("bit_rate", "=", 192)) == ["/m/b/3.mp3"]
    assert got(R("year", "<", 2000), R("year", ">", 0)) == ["/m/a/2.flac"]
    assert got(R("bits", ">=", 24), R("sample_rate", ">", 48000), match="any") == ["/m/a/1.flac"]
    assert got(R("liked", "is", True)) == ["/m/a/2.flac"]
    assert got(R("disliked", "is", False)) == ["/m/a/2.flac", "/m/a/1.flac"]
    assert got(R("lossless", "is", False)) == ["/m/b/3.mp3"]
    assert got(R("added_days", "<=", 10)) == ["/m/b/3.mp3", "/m/a/1.flac"]
    assert got() == ["/m/b/3.mp3", "/m/a/2.flac", "/m/a/1.flac"]  # no rules: every song
    assert got(sort="duration", descending=True) == ["/m/a/1.flac", "/m/a/2.flac", "/m/b/3.mp3"]
    assert got(sort="duration", descending=True, limit=2) == ["/m/a/1.flac", "/m/a/2.flac"]
    assert got(sort="added_days") == ["/m/a/1.flac", "/m/b/3.mp3", "/m/a/2.flac"]


def test_check_tab_says_what_is_wrong():
    def error(**kw):
        with pytest.raises(smarttabs.TabError) as exc:
            smarttabs.check_tab(dict(T("x", [R("year", ">", 1)]), **kw))
        return str(exc.value)

    assert error(format="x") == "This file is not a tab of Universal Music Player."
    assert error(version=2).startswith("This tab was made by a newer version")
    assert error(name=" ") == "Give the tab a name."
    assert error(rules=[R("colour", "is", "x")]) == "Rule 1 uses the field “colour”, which tabs do not know."
    assert error(rules=[R("year", ">", 1), R("year", "contains", 1)]) == \
        "Rule 2: “Year” cannot use the operator “contains”."
    assert error(rules=[R("year", ">", "1")]) == "Rule 1: the value of “Year” must be a number."
    assert error(rules=[R("liked", "is", 1)]) == "Rule 1: the value of “Liked” must be yes or no."
    assert error(rules=[R("title", "is", 1)]) == "Rule 1: the value of “Title” must be a text."
    assert error(sort="colour") == "A tab cannot be sorted by “colour”."
    assert error(limit=-1).startswith("The limit must be a whole number")
    assert error(match="most") == "A tab matches “all” or “any” of its rules."
    # left out keys get their defaults; the name is cleaned
    assert smarttabs.check_tab({"format": smarttabs.TAB_FORMAT, "version": 1, "name": "  A   b "}) == T("A b")


def test_values_typed_into_the_tab_window():
    assert smarttabs.parse_value("year", " 2000 ") == 2000
    assert smarttabs.parse_value("duration", "2.5") == 2.5
    assert smarttabs.parse_value("year", "2,000") == "2,000"  # stays text: check_tab() refuses it
    assert smarttabs.parse_value("title", " 2000 ") == " 2000 "
    assert smarttabs.value_text(30) == "30" and smarttabs.value_text(2.5) == "2.5"
    assert smarttabs.change_field(R("year", ">=", 5), "bits") == R("bits", ">=", 5)
    assert smarttabs.change_field(R("year", ">=", 5), "title") == R("title", "contains", "")
    assert smarttabs.new_rule("year") == R("year", ">=", 0) and smarttabs.new_rule("liked") == R("liked", "is", True)
    assert smarttabs.change_field(R("title", "is", "x"), "quality") == R("quality", "is", "Hi-Res Lossless")


def test_files_in_the_tabs_folder(tmp_path):
    d = tmp_path / "tabs"
    assert smarttabs.tabs_folder() == os.environ["REALHIRES_TABS"]
    a = smarttabs.save_tab(T("Hi-Res", [R("bits", ">=", 24)]), str(d))
    assert os.path.basename(a["file"]) == "Hi-Res.json"
    text = Path(a["file"]).read_text(encoding="utf-8")
    assert text == smarttabs.tab_text(a) and text.endswith("}\n") and '"format": "universal-music-player-tab"' in text
    b = smarttabs.save_tab(T("Hi-Res"), str(d))  # another file of that name: " 2"
    assert os.path.basename(b["file"]) == "Hi-Res 2.json"
    renamed = smarttabs.save_tab(T("Best"), str(d), old_file=b["file"])
    assert sorted(os.listdir(d)) == ["Best.json", "Hi-Res.json"]
    (d / "notes.json").write_text("not json", encoding="utf-8")
    tabs, skipped = smarttabs.load_tabs(str(d))
    assert [t["name"] for t in tabs] == ["Best", "Hi-Res"]
    assert skipped == [("notes.json", "This file is not a tab of Universal Music Player.")]
    # export, then import it back: a taken name gets " 2"
    out = smarttabs.export_tab(a, str(tmp_path / "shared.json"))
    imported = smarttabs.import_tab(out, str(d))
    assert imported["name"] == "Hi-Res 2" and os.path.basename(imported["file"]) == "Hi-Res 2.json"
    with pytest.raises(smarttabs.TabError, match="tabs folder already"):
        smarttabs.import_tab(imported["file"], str(d))
    assert smarttabs.delete_tab(renamed) and not smarttabs.delete_tab(renamed)
    assert smarttabs.is_tab_text(text) and not smarttabs.is_tab_text('{"name": "Neon", "colors": {}}')
    assert smarttabs.tab_file_name('a/b: c?') == "a_b_ c_.json" and smarttabs.tab_file_name("CON") == "_CON.json"


def test_rust_vectors_hold_pythons_answers():
    """python_tabs.json is what rust/tools/gen_player_vectors.py writes from this
    module: the checks and the evaluations still give the same answers."""
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    tracks = [Track.from_json(t) for t in data["tracks"]]
    from realhires.moods import Mood  # 2.4: the moods of some of the songs

    c = smarttabs.context(data["liked"], data["disliked"], data["now"],
                          {p: Mood(bpm=m["bpm"], mood=m["mood"], tempo=m["tempo"]) for p, m in data["moods"].items()})
    for case in data["evals"]:
        t = smarttabs.check_tab(case["tab"])
        assert smarttabs.tab_songs(t, tracks, c) == case["songs"], t["name"]
    for case in data["checks"]:
        try:
            got, err = smarttabs.check_tab(case["input"]), None
        except smarttabs.TabError as exc:
            got, err = None, str(exc)
        assert (got, err) == (case["tab"], case["error"]), case["input"]
    for case in data["texts"]:
        assert smarttabs.tab_text(case["tab"]) == case["text"]
    assert data["labels"]["templates"] == smarttabs.TEMPLATES


def test_every_message_and_label_is_shown_in_japanese():
    from realhires import i18n

    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    messages = {c["error"] for c in data["checks"] if c["error"]} | {why for _, why in data["folder"]["skipped"]}
    messages |= {r["error"] for r in data["op_results"] if "error" in r}
    messages |= {f"You already have a tab called “{'Hi-Res'}”.", "The tab could not be saved: [Errno 13] Permission denied"}
    i18n.set_language("日本語")
    try:
        for m in sorted(messages):
            ja = i18n.tr_text(m)
            assert ja != m and not any(w in ja for w in ("Rule ", "tab", " must ")), (m, ja)
        assert i18n.tr_text("Rule 2: “Year” cannot use the operator “contains”.") == \
            "ルール2：「年」には条件「が次を含む」を使えません。"
        for f in smarttabs.FIELDS:
            assert i18n.tr(smarttabs.FIELD_LABELS[f]) != smarttabs.FIELD_LABELS[f]
            for op in smarttabs.ops_for(f):
                assert i18n.tr(smarttabs.op_label(f, op)) != smarttabs.op_label(f, op)
        for t in smarttabs.TEMPLATES:
            assert i18n.tr(t["name"]) != t["name"]
    finally:
        i18n.set_language("English")
