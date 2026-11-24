"""The language setting (2.1): the catalog, its completeness and placeholders,
the pattern matcher, and that the Rust vectors are current."""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import i18n_keys  # noqa: E402

from realhires import i18n  # noqa: E402
from realhires.settings import Settings  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _english_after():
    yield
    i18n.set_language("English")


def test_catalog_is_built_from_its_sources():
    data, _ = i18n_keys.merged()
    with open(i18n_keys.CATALOG, encoding="utf-8") as fh:
        assert fh.read() == i18n_keys.catalog_text(data), "run: python tests/i18n_keys.py --merge"


def test_every_text_of_both_apps_has_a_japanese_translation():
    keys = i18n_keys.all_keys()
    strings = i18n_keys.catalog()["strings"]
    missing = {k: w[:2] for k, w in keys.items() if k not in strings}
    assert not missing, f"{len(missing)} texts without Japanese, run: python tests/i18n_keys.py"
    assert len(keys) > 900


def test_translations_keep_the_placeholders():
    cat = i18n_keys.catalog()
    for table in ("strings", "patterns"):
        for k, v in cat[table].items():
            assert sorted(i18n.placeholders(k)) == sorted(i18n.placeholders(v)), (table, k, v)
    for k in cat["patterns"]:
        assert i18n.placeholders(k), k
        assert "}{" not in k, f"two placeholders must be separated by text: {k}"


def test_translations_follow_the_style():
    strings = i18n_keys.catalog()["strings"]
    for en, ja in strings.items():
        if en.endswith("…") and len(en) < 40:  # a menu entry or button that opens a window
            assert ja.endswith("…"), (en, ja)
    assert strings["Library"] == "ライブラリ" and strings["Settings"] == "設定"


def test_english_by_default_and_japanese_when_chosen():
    assert Settings().language == "English"
    choices = next(f["choices"] for f in Settings.field_info() if f["name"] == "language")
    assert choices == ["English", "日本語"]
    assert i18n.tr("Library") == "Library"
    assert i18n.set_language("日本語") == "ja" and i18n.language() == "ja"
    assert i18n.tr("Library") == "ライブラリ"
    assert i18n.tr("Playing from {name}", name="Rock") == "再生元：Rock"
    assert i18n.tr_name("Rock") == "Rock" and i18n.tr_name("Unknown Album") == "不明なアルバム"
    assert i18n.N_("Library") == "Library"
    assert i18n.code_of("Klingon") == "en"
    assert i18n.tr_text("Upsampled from 44.1 kHz: nothing above 22.05 kHz") == \
        "44.1 kHzからアップサンプリング：22.05 kHzより上は空"
    assert i18n.tr_text("Not in the catalogue at all.") == "Not in the catalogue at all."


def test_patterns_take_numbers_only_where_marked():
    assert i18n.match_template("{d:num} {m} {y:num}", "28 May 2026") == {"d": "28", "m": "May", "y": "2026"}
    assert i18n.match_template("{d:num} {m} {y:num}", "Upsampled from 44.1 kHz") is None
    assert i18n.match_template("{a}, {b}", "x, y, z") == {"a": "x", "b": "y, z"}
    assert i18n.match_template("{a} kHz", " kHz") is None


def test_rust_vectors_are_current(tmp_path):
    path = os.path.join(ROOT, "rust", "crates", "core", "tests", "python_i18n.json")
    with open(path, encoding="utf-8") as fh:
        before = fh.read()
    subprocess.run([sys.executable, os.path.join(ROOT, "rust", "tools", "gen_i18n_vectors.py")], check=True,
                   capture_output=True)
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == before, "python_i18n.json was out of date; it has been rewritten"
