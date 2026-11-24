"""User-adjustable settings, shared by the GUI and the CLI.

Settings are stored as JSON in the platform's config folder:

* Windows: %APPDATA%\\RealHiRes\\settings.json
* macOS:   ~/Library/Application Support/RealHiRes/settings.json
* Linux:   $XDG_CONFIG_HOME/realhires/settings.json (default ~/.config/realhires)

Every field carries metadata (label, group, limits, help text) so the GUI can
build its Settings window automatically.  Unknown keys in the file are ignored
and invalid values fall back to the default, so an old or hand-edited file can
never stop the program from starting.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

from .i18n import LANGUAGES

GROUPS = ("Library", "Playback", "Appearance", "Lyrics", "Detection", "Scanning", "Saved results", "Display",
          "Fixing")
SETTINGS_VERSION = 160  # bumped when old saved values need migrating (see from_dict)

ALL_VERDICTS = ["FAKE LOSSLESS", "FAKE HI-RES", "SUSPICIOUS", "REAL HI-RES", "NOT HI-RES",
                "LOSSY FORMAT", "ERROR"]
COLORMAPS = ["inferno", "viridis", "magma", "gray"]
LYRICS_SAVE_CHOICES = ["An .lrc file next to the song", "Inside the audio file (MP3, FLAC)",
                       "Both: .lrc file and inside the audio file", "Plain lyrics inside the audio file (no timing)"]
# the built-in theme names (2.0); the full list, with the user's own themes, is themes.names()
THEMES = ["Match the system", "Light", "Dark", "Midnight", "Graphite", "Forest", "Ocean", "Sepia", "Rose"]
DEFAULT_EXTENSIONS = (".flac .wav .wave .w64 .rf64 .aif .aiff .aifc .caf .m4a .mp4 .alac .ape .wv "
                      ".tta .tak .dsf .dff .mka .ogg .oga .opus .mp3 .aac .wma .mpc .ac3 .dts")


def _f(default, group, label, help_text, lo=None, hi=None, choices=None, kind=None):
    md = {"group": group, "label": label, "help": help_text, "min": lo, "max": hi,
          "choices": choices, "kind": kind}
    if isinstance(default, list):
        return field(default_factory=lambda: list(default), metadata=md)
    return field(default=default, metadata=md)


@dataclass
class Settings:
    # ---- Library (2.0, the player) -----------------------------------------
    library_folders: list = _f(
        [], "Library", "Library folders",
        "The folders whose songs make up your music library. Every audio file in them and in their "
        "sub-folders is listed in the player, with the title, artist, album and genre from its tags. "
        "On the command line, separate several folders with ';' on Windows and ':' elsewhere.",
        kind="paths")
    scan_library_on_start: bool = _f(
        False, "Library", "Check new songs when the app starts",
        "When the app starts, analyse the library songs that have not been checked for fake hi-res and "
        "fake lossless yet. Songs checked before are answered from the saved results at once.", kind="bool")
    lyrics_while_scanning: bool = _f(
        False, "Library", "Download lyrics while scanning",
        "While a scan runs, look each song up on LRCLIB as soon as it has been analysed and save its "
        "lyrics as set on the Lyrics tab (one request at a time, as LRCLIB asks).", kind="bool")

    # ---- Playback (2.0) ---------------------------------------------------
    output_device: str = _f(
        "", "Playback", "Output device",
        "The sound device the player plays through. Empty = the system's default device.", kind="str")
    gapless: bool = _f(
        True, "Playback", "Gapless playback",
        "Start the next song without a pause, so albums that flow from track to track play as one.",
        kind="bool")

    # ---- Appearance (2.0) ---------------------------------------------------
    language: str = _f(
        "English", "Appearance", "Language",
        "The language of the app's menus, pages and windows: English or 日本語 (Japanese). The information of "
        "your songs is shown as it is in their tags, and the command-line tool always writes English.",
        choices=list(LANGUAGES), kind="choice")
    font_family: str = _f(
        "", "Appearance", "Font",
        "The font of the app. Empty = SF Pro, the system font, on macOS and Inter on Windows and Linux "
        "(SF Pro may only be used on Apple systems). Any font installed on your computer can be chosen.",
        kind="str")
    theme: str = _f(
        "Match the system", "Appearance", "Theme",
        "The colours of the app: \"Match the system\" (light or dark as the system is), a built-in theme "
        "(Light, Dark, Midnight, Graphite, Forest, Ocean, Sepia, Rose), one of your own themes, or a theme file "
        "put into the themes folder.", kind="str")

    # ---- Detection ------------------------------------------------------
    cliff_db: float = _f(
        25.0, "Detection", "Upsampling cliff (dB)",
        "Minimum level drop at the Nyquist frequency of a lower sample rate (22.05 kHz for "
        "44.1 kHz, 24 kHz for 48 kHz, ...) that marks a file as upsampled. Lower = stricter, "
        "more files flagged.", 5, 150)
    cliff_strong_db: float = _f(
        40.0, "Detection", "High-confidence cliff (dB)",
        "A cliff at least this deep is reported with high confidence.", 5, 200)
    lossy_cliff_db: float = _f(
        25.0, "Detection", "Lossy cut-off drop (dB)",
        "Minimum drop within ~1 kHz that counts as an MP3/AAC encoder low-pass "
        "(the sign of a fake FLAC/WAV made from a lossy file).", 5, 150)
    lossy_min_khz: float = _f(
        11.0, "Detection", "Lossy cut-off search from (kHz)",
        "Lowest frequency at which a lossy encoder low-pass is searched for.", 4, 22)
    lossy_max_khz: float = _f(
        20.3, "Detection", "Lossy cut-off search up to (kHz)",
        "Highest frequency at which a lossy low-pass is searched for. Raising it towards 21 kHz "
        "also catches 320 kbps MP3s but may flag genuine CD masters with a steep filter.", 10, 23)
    ultrasonic_khz: float = _f(
        24.5, "Detection", "Ultrasonic content expected above (kHz)",
        "A high-sample-rate file with no content above this frequency (and no cliff) is "
        "reported as SUSPICIOUS.", 15, 60)
    steep_drop_db: float = _f(
        20.0, "Detection", "Brick-wall edge (dB)",
        "Drop across the bandwidth edge that counts as a brick-wall filter.", 5, 150)
    content_margin_db: float = _f(
        10.0, "Detection", "Content above noise floor (dB)",
        "How far above the noise floor a frequency band must be to count as real content.", 1, 60)
    relevant_range_db: float = _f(
        110.0, "Detection", "Ignore content below peak by (dB)",
        "Content more than this far below the loudest part of the spectrum is ignored "
        "(removes resampler artefacts).", 40, 200)
    bit_threshold_pct: float = _f(
        0.5, "Detection", "Bit-usage threshold (%)",
        "A bit position counts as used when at least this share of samples uses it. "
        "Real 24-bit audio uses its lowest bit in ~50 % of samples.", 0.001, 25)
    deep_cliff_db: float = _f(
        45.0, "Detection", "Deep upsampling cliff (dB)",
        "Minimum drop from just below a lower Nyquist frequency to the quietest part of the band "
        "above it (104-130 % of that frequency). Catches soft resamplers such as FFmpeg's default "
        "one, whose cliff is too gradual for the narrow test.", 10, 200)
    deep_cliff_strong_db: float = _f(
        60.0, "Detection", "High-confidence deep cliff (dB)",
        "A deep cliff at least this large is reported with high confidence.", 10, 250)
    mid_ratio: float = _f(
        0.975, "Detection", "Resampler mid-point ratio",
        "Expert: where resamplers put their -6 dB point relative to the source Nyquist (about "
        "0.955 for soxr, 1.0 for FFmpeg's default); used to choose between neighbouring source "
        "rates (44.1 vs 48 kHz).", 0.8, 1.1)
    analog_floor_db: float = _f(
        15.0, "Detection", "Converter noise above rounding noise (dB)",
        "A dull high-rate file counts as really recorded at its rate when its 26-40 kHz band "
        "holds an even noise floor at least this far above the rounding noise (real converters "
        "leave 25-40 dB; an upsample leaves about 0-5 dB).", 6, 60)
    grid_strength: float = _f(
        0.35, "Detection", "Value-grid strength",
        "Expert: how strongly the sample values must line up on an even grid before a file counts "
        "as a scaled or dithered 16-bit master (0-1; real 24-bit audio stays below 0.05, a "
        "scaled 16-bit master reaches 0.85-1.0).", 0.1, 0.95)

    # ---- Scanning -------------------------------------------------------
    jobs: int = _f(
        0, "Scanning", "Parallel jobs (0 = automatic)",
        "How many files are analysed at the same time. 0 uses one job per CPU core "
        f"({os.cpu_count() or 1} on this computer), which is the fastest setting: more jobs than cores "
        "do not scan faster, they only use more memory. Up to 512 are allowed. (Python on Windows "
        "limits one process pool to 61 workers; Real-Hi-Res uses several pools to go beyond that.)",
        0, 512, kind="int")
    max_seconds: float = _f(
        0.0, "Scanning", "Analyse at most (seconds, 0 = whole file)",
        "Analysing only the start of each file is faster but can miss content.", 0, 36000)
    quick_scan_seconds: float = _f(
        90.0, "Scanning", "Quick scan length (seconds)",
        "Length analysed when 'Quick scan' is ticked in the toolbar.", 5, 3600)
    recursive: bool = _f(True, "Scanning", "Include sub-folders",
                         "Scan folders recursively.", kind="bool")
    extensions: str = _f(
        DEFAULT_EXTENSIONS, "Scanning", "File extensions",
        "Space- or comma-separated list of file extensions to scan.", kind="str")

    # ---- Saved results --------------------------------------------------
    save_results: bool = _f(
        True, "Saved results", "Save results and reuse them",
        "Remember the result of every scanned file, so a file that has not changed since is not "
        "analysed again: on the next scan, after restarting and after updating Real-Hi-Res. "
        "Changing a Detection setting analyses the files again.", kind="bool")
    rescan_outdated: bool = _f(
        False, "Saved results", "Re-scan results of older detection rules",
        "When an update of Real-Hi-Res improves the detection rules, results saved by the older "
        "rules are kept and marked. Turn this on to analyse those files again automatically "
        "during the next scan; otherwise use 'Re-scan files checked by older rules'.", kind="bool")
    saved_graphs_mb: int = _f(
        500, "Saved results", "Space for saved graphs (MB, 0 = none)",
        "Each file's spectrogram and spectrum take about 30-90 KB. Above this size the graphs "
        "viewed longest ago are dropped (their results stay) and drawn again when the file is "
        "selected.", 0, 100000, kind="int")

    # ---- Display --------------------------------------------------------
    shown_verdicts: List[str] = _f(
        ["FAKE LOSSLESS", "FAKE HI-RES", "SUSPICIOUS"], "Display", "Verdicts listed by default",
        "Which verdicts the result list shows when the app starts.", choices=ALL_VERDICTS,
        kind="multi")
    show_summary_popup: bool = _f(True, "Display", "Show summary after each scan",
                                  "Pop up the percentage breakdown when a scan finishes.",
                                  kind="bool")
    double_click: str = _f("Open folder", "Display", "Double-click / Enter opens",
                           "What double-clicking a row (or pressing Enter) does.",
                           choices=["Open folder", "Open file"], kind="choice")
    view_mode: str = _f("Files", "Display", "Start in view",
                        "Which view the result area shows when the app starts.",
                        choices=["Files", "By album", "By artist"], kind="choice")
    card_label: str = _f("Percentage", "Display", "Album/artist rings show",
                         "What the ring on each album/artist card shows: the share of fake or "
                         "suspicious songs, or how many songs out of the total.",
                         choices=["Percentage", "Songs (x of n)"], kind="choice")
    group_sort: str = _f("Most fake first", "Display", "Sort albums/artists by",
                         "Order of the album and artist cards.",
                         choices=["Most fake first", "Name"], kind="choice")
    colormap: str = _f("inferno", "Display", "Spectrogram colours",
                       "Colour map of the spectrogram.", choices=COLORMAPS, kind="choice")
    spectrogram_range_db: float = _f(
        120.0, "Display", "Spectrogram range (dB)",
        "Dynamic range shown in the spectrogram; quieter detail is drawn black.", 40, 200)

    # ---- Fixing ("Fix resolution") ---------------------------------------
    fix_output: str = _f(
        "Replace the file", "Fixing", "Fix files by",
        "'Replace the file' rewrites the file itself at its real resolution, keeping its name, "
        "folder, tags and cover art, so its properties (sample rate, bit depth) show the actual "
        "values. The other choices write a corrected copy instead and leave the file alone.",
        choices=["Replace the file", "Saving a copy next to it", "Saving a copy into a folder"],
        kind="choice")
    fix_backup: str = _f(
        "Move the original to a backup folder", "Fixing", "Original when replacing",
        "What happens to the original file when it is replaced. The backup folder is "
        "'Real-Hi-Res backups' inside the file's folder unless a folder is set below.",
        choices=["Move the original to a backup folder", "Keep it as <name>.bak next to the file",
                 "Delete it (no backup)"], kind="choice")
    fix_backup_folder: str = _f(
        "", "Fixing", "Backup folder",
        "Where originals are moved when replacing. Empty = a 'Real-Hi-Res backups' folder next to "
        "each file.", kind="str")
    fix_folder: str = _f(
        "", "Fixing", "Folder for copies",
        "Used with 'Saving a copy into a folder'. Empty = a 'Fixed' folder next to each original.",
        kind="str")
    fix_format: str = _f(
        "Same as the original", "Fixing", "Output format",
        "'Same as the original' keeps FLAC as FLAC, WAV as WAV, ALAC as ALAC, AIFF as AIFF, WavPack "
        "as WavPack (others become FLAC). 'FLAC' always writes FLAC.",
        choices=["Same as the original", "FLAC"], kind="choice")
    fix_bits: str = _f(
        "16-bit for CD-sourced files", "Fixing", "Target bit depth",
        "'As detected' keeps the bit depth actually found in the file. Resampling a 16-bit CD "
        "master to 96 kHz fills the low bits, so such files are detected as 24-bit; "
        "'16-bit for CD-sourced files' restores 16-bit when the file was upsampled from 44.1/48 kHz "
        "or made from a lossy file. 'Always 16-bit' converts everything to 16-bit.",
        choices=["As detected", "16-bit for CD-sourced files", "Always 16-bit"], kind="choice")
    fix_suffix: str = _f(
        " [{bits}-{rate}]", "Fixing", "File name suffix",
        "Added to the file name of fixed copies; {bits} and {rate} are replaced, e.g. "
        "'Song [16-44.1].flac'. Only used for copies; a replaced file keeps its name.", kind="str")
    fix_verify: bool = _f(True, "Fixing", "Re-check fixed files",
                          "Analyse every fixed file again and show its new verdict.", kind="bool")
    fix_after_scan: str = _f(
        "Do nothing", "Fixing", "After a scan",
        "What happens when a scan finds fake hi-res or fake lossless files that can be fixed. "
        "'Ask whether to fix them' asks once after the scan; 'Fix them automatically' opens the Fix "
        "window and starts at once, with the options above (originals still go to the backup folder; "
        "if you chose to delete originals, you are still asked first). Only the desktop app does "
        "this; the command-line tool fixes with --fix.",
        choices=["Do nothing", "Ask whether to fix them", "Fix them automatically"], kind="choice")

    # ---- Lyrics (synced lyrics from LRCLIB, as LRCGET) --------------------
    lyrics_existing: str = _f(
        "Skip songs that have synced lyrics", "Lyrics", "Songs that already have lyrics",
        "Which songs are looked up. A song has synced lyrics when <name>.lrc is next to it, and plain "
        "lyrics when <name>.txt is. 'Skip songs that have synced lyrics' still looks up songs that only "
        "have a .txt, to add an .lrc (the .txt is kept). Lyrics files are never deleted; 'Download "
        "again and replace them' overwrites them.",
        choices=["Skip songs that have synced lyrics", "Skip songs that have any lyrics",
                 "Download again and replace them"], kind="choice")
    lyrics_plain: bool = _f(
        True, "Lyrics", "Save plain lyrics as .txt",
        "When LRCLIB has only plain (unsynced) lyrics for a song, save them as <name>.txt next to it. "
        "Off: such songs are left without lyrics.", kind="bool")
    lyrics_instrumental: bool = _f(
        True, "Lyrics", "Mark instrumental songs",
        "Save an .lrc holding '[au: instrumental]' for songs LRCLIB lists as instrumental, as LRCGET "
        "does, so they are not looked up again.", kind="bool")
    lyrics_search: bool = _f(
        True, "Lyrics", "Search when there is no exact match",
        "When LRCLIB has no record with the song's exact title, artist, album and length, search by "
        "title and artist and take a result with the same title, the same artist and a length within "
        "2 seconds. This finds songs whose album is named differently on LRCLIB. Off: exact matches only "
        "(one request per song instead of up to two).", kind="bool")
    lyrics_save_as: str = _f(
        LYRICS_SAVE_CHOICES[0], "Lyrics", "Save lyrics as",
        "Where downloaded lyrics go. An .lrc file next to the song works with every player and never "
        "changes the audio file. 'Inside the audio file' writes synced lyrics into the file's tag, as "
        "LRCGET does (MP3: SYLT and USLT; FLAC: LYRICS and UNSYNCEDLYRICS); only the tag is rewritten, the "
        "audio itself is not changed, and other formats get an .lrc file instead. 'Plain lyrics inside "
        "the audio file' stores the text without timing, which more players show.",
        choices=LYRICS_SAVE_CHOICES, kind="choice")
    lyrics_server: str = _f(
        "https://lrclib.net", "Lyrics", "LRCLIB server",
        "Address of the LRCLIB server the lyrics come from. Change it only for a self-hosted LRCLIB.",
        kind="str")

    # ------------------------------------------------------------------
    def extension_set(self) -> set:
        parts = self.extensions.replace(",", " ").split()
        return {("." + p.lstrip(".")).lower() for p in parts if p.strip(".")}

    @classmethod
    def field_info(cls) -> List[Dict[str, Any]]:
        out = []
        for f in fields(cls):
            default = f.default if f.default is not MISSING else f.default_factory()  # type: ignore[misc]
            out.append(dict(name=f.name, default=default, **f.metadata))
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["_version"] = SETTINGS_VERSION
        return d

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "Settings":
        s = cls()
        if not isinstance(data, dict):
            return s
        data = dict(data)
        old = {"Next to the original": "Replace the file",                  # 1.4.0 default -> new default
               "Into a folder": "Saving a copy into a folder",
               "Replace the original (keep .bak)": "Replace the file"}
        if data.get("fix_output") == "Replace the original (keep .bak)":
            data.setdefault("fix_backup", "Keep it as <name>.bak next to the file")
        if data.get("fix_output") in old:
            data["fix_output"] = old[data["fix_output"]]
        # Files saved before 1.6.0 stored every field, so "Saving a copy next to it" there is just
        # the migrated old default, not a choice: switch them to the new default (replace the file).
        if data.get("_version", 0) < SETTINGS_VERSION and data.get("fix_output") == "Saving a copy next to it":
            data["fix_output"] = "Replace the file"
        for f in fields(cls):
            if f.name in data:
                v = _coerce(f, data[f.name])
                if v is not None:
                    setattr(s, f.name, v)
        return s

    def set_value(self, name: str, raw: str) -> None:
        """Set one field from text (CLI --set name=value). Raises ValueError."""
        f = next((f for f in fields(self) if f.name == name), None)
        if f is None:
            raise ValueError(f"unknown setting '{name}' (known: {', '.join(x.name for x in fields(self))})")
        kind = f.metadata.get("kind")
        value: Any = raw
        if kind == "multi":
            value = [v.strip().upper() for v in raw.split(",") if v.strip()]
        elif kind == "paths":
            value = [v.strip() for v in raw.split(os.pathsep) if v.strip()]
        elif kind == "bool":
            value = raw.strip().lower() in ("1", "true", "yes", "on")
        v = _coerce(f, value)
        if v is None:
            raise ValueError(f"invalid value for {name}: {raw!r}")
        setattr(self, name, v)


def _coerce(f, value):
    """Validate/convert a value for field `f`; None means invalid."""
    md = f.metadata
    kind = md.get("kind")
    try:
        if kind == "bool":
            return bool(value)
        if kind == "str":
            return str(value)
        if kind == "choice":
            return value if value in md["choices"] else None
        if kind == "multi":
            vals = [v for v in value if v in md["choices"]]
            return vals if isinstance(value, (list, tuple)) else None
        if kind == "paths":
            if not isinstance(value, (list, tuple)):
                return None
            out: List[str] = []
            for v in value:
                if isinstance(v, str) and v.strip() and v.strip() not in out:
                    out.append(v.strip())
            return out
        num = int(value) if kind == "int" else float(value)
    except (TypeError, ValueError, KeyError):
        return None
    if md.get("min") is not None and num < md["min"]:
        return None
    if md.get("max") is not None and num > md["max"]:
        return None
    return num


def config_dir() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
        return os.path.join(base, "RealHiRes")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/RealHiRes")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "realhires")


def default_path() -> str:
    return os.environ.get("REALHIRES_SETTINGS") or os.path.join(config_dir(), "settings.json")


def load(path: Optional[str] = None) -> Settings:
    path = path or default_path()
    try:
        with open(path, encoding="utf-8") as fh:
            return Settings.from_dict(json.load(fh))
    except (OSError, ValueError):
        return Settings()


def save(settings: Settings, path: Optional[str] = None) -> str:
    path = path or default_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings.to_dict(), fh, indent=2)
    os.replace(tmp, path)
    return path
