//! User-adjustable settings, shared by the GUI and the CLI, and by the Python
//! and the Rust version: both read and write the same `settings.json`.
//!
//! * Windows: %APPDATA%\RealHiRes\settings.json
//! * macOS:   ~/Library/Application Support/RealHiRes/settings.json
//! * Linux:   $XDG_CONFIG_HOME/realhires/settings.json (default ~/.config/realhires)
//!
//! Every field carries metadata (group, label, help, limits, kind) so the GUI
//! can build its Settings window from it.  Unknown keys are ignored and invalid
//! values fall back to the default, so an old or hand-edited file never stops
//! the program from starting.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use crate::json::{self, Value};

pub const GROUPS: [&str; 9] =
    ["Library", "Playback", "Appearance", "Lyrics", "Detection", "Scanning", "Saved results", "Display", "Fixing"];
pub const LYRICS_SAVE_CHOICES: [&str; 4] = [
    "An .lrc file next to the song",
    "Inside the audio file (MP3, FLAC)",
    "Both: .lrc file and inside the audio file",
    "Plain lyrics inside the audio file (no timing)",
];
/// The built-in theme names (2.0); with the user's own themes: `themes::names()`.
pub const THEMES: [&str; 9] = ["Match the system", "Light", "Dark", "Midnight", "Graphite", "Forest", "Ocean", "Sepia", "Rose"];
pub const SETTINGS_VERSION: i64 = 160;

pub const ALL_VERDICTS: [&str; 7] =
    ["FAKE LOSSLESS", "FAKE HI-RES", "SUSPICIOUS", "REAL HI-RES", "NOT HI-RES", "LOSSY FORMAT", "ERROR"];
pub const COLORMAPS: [&str; 4] = ["inferno", "viridis", "magma", "gray"];
pub const DEFAULT_EXTENSIONS: &str = ".flac .wav .wave .w64 .rf64 .aif .aiff .aifc .caf .m4a .mp4 .alac .ape .wv \
                                      .tta .tak .dsf .dff .mka .ogg .oga .opus .mp3 .aac .wma .mpc .ac3 .dts";

/// What kind of value a setting holds (the GUI picks its widget from this).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Float,
    Int,
    Bool,
    Str,
    Choice,
    Multi,
    /// a list of folders (2.0: the library folders)
    Paths,
}

/// A setting's value, independent of its Rust type.
#[derive(Clone, Debug, PartialEq)]
pub enum SValue {
    Float(f64),
    Int(i64),
    Bool(bool),
    Str(String),
    List(Vec<String>),
}

impl SValue {
    pub fn to_json(&self) -> Value {
        match self {
            SValue::Float(f) => Value::Float(*f),
            SValue::Int(i) => Value::Int(*i),
            SValue::Bool(b) => Value::Bool(*b),
            SValue::Str(s) => Value::Str(s.clone()),
            SValue::List(l) => Value::Array(l.iter().map(|s| Value::Str(s.clone())).collect()),
        }
    }

    /// The value as `--list-settings` prints it (Python's `repr`).
    pub fn repr(&self) -> String {
        match self {
            SValue::Float(f) => crate::pyfmt::repr_float(*f),
            SValue::Int(i) => i.to_string(),
            SValue::Bool(b) => if *b { "True" } else { "False" }.into(),
            SValue::Str(s) => py_str_repr(s),
            SValue::List(l) => format!("[{}]", l.iter().map(|s| py_str_repr(s)).collect::<Vec<_>>().join(", ")),
        }
    }
}

fn py_str_repr(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') {
        format!("\"{s}\"")
    } else {
        format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

trait SettingType: Sized {
    fn to_value(&self) -> SValue;
    fn from_value(v: SValue) -> Option<Self>;
}

impl SettingType for f64 {
    fn to_value(&self) -> SValue {
        SValue::Float(*self)
    }
    fn from_value(v: SValue) -> Option<Self> {
        match v {
            SValue::Float(f) => Some(f),
            SValue::Int(i) => Some(i as f64),
            _ => None,
        }
    }
}
impl SettingType for i64 {
    fn to_value(&self) -> SValue {
        SValue::Int(*self)
    }
    fn from_value(v: SValue) -> Option<Self> {
        match v {
            SValue::Int(i) => Some(i),
            _ => None,
        }
    }
}
impl SettingType for bool {
    fn to_value(&self) -> SValue {
        SValue::Bool(*self)
    }
    fn from_value(v: SValue) -> Option<Self> {
        match v {
            SValue::Bool(b) => Some(b),
            _ => None,
        }
    }
}
impl SettingType for String {
    fn to_value(&self) -> SValue {
        SValue::Str(self.clone())
    }
    fn from_value(v: SValue) -> Option<Self> {
        match v {
            SValue::Str(s) => Some(s),
            _ => None,
        }
    }
}
impl SettingType for Vec<String> {
    fn to_value(&self) -> SValue {
        SValue::List(self.clone())
    }
    fn from_value(v: SValue) -> Option<Self> {
        match v {
            SValue::List(l) => Some(l),
            _ => None,
        }
    }
}

#[derive(Clone, Debug)]
pub struct FieldInfo {
    pub name: &'static str,
    pub group: &'static str,
    pub label: &'static str,
    pub help: String,
    pub min: Option<f64>,
    pub max: Option<f64>,
    pub choices: &'static [&'static str],
    pub kind: Kind,
    pub default: SValue,
}

fn s(v: &str) -> String {
    v.to_string()
}

fn cpu_count() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}

macro_rules! settings {
    ($( $name:ident : $ty:ty = $default:expr, $group:expr, $label:expr, $help:expr, $kind:ident,
        $min:expr, $max:expr, $choices:expr; )*) => {
        /// Every setting (the same names and meanings as the Python version).
        #[derive(Clone, Debug, PartialEq)]
        pub struct Settings { $( pub $name: $ty, )* }

        impl Default for Settings {
            fn default() -> Self { Settings { $( $name: $default, )* } }
        }

        impl Settings {
            /// Metadata of every field, in declaration order.
            pub fn field_info() -> Vec<FieldInfo> {
                vec![ $( FieldInfo {
                    name: stringify!($name), group: $group, label: $label, help: $help,
                    min: $min, max: $max, choices: $choices, kind: Kind::$kind,
                    default: <$ty as SettingType>::to_value(&$default),
                }, )* ]
            }

            pub fn get(&self, name: &str) -> Option<SValue> {
                match name { $( stringify!($name) => Some(self.$name.to_value()), )* _ => None }
            }

            fn put(&mut self, name: &str, v: SValue) -> bool {
                match name {
                    $( stringify!($name) => match <$ty as SettingType>::from_value(v) {
                        Some(x) => { self.$name = x; true }
                        None => false
                    }, )*
                    _ => false,
                }
            }
        }
    };
}

const NUMS: &[&str] = &[];
const VERDICTS: &[&str] = &ALL_VERDICTS;
const MAPS: &[&str] = &COLORMAPS;

settings! {
    // ---- Library (2.0, the player) -----------------------------------------
    library_folders: Vec<String> = vec![], "Library", "Library folders",
        s("The folders whose songs make up your music library. Every audio file in them and in their sub-folders \
           is listed in the player, with the title, artist, album and genre from its tags. On the command line, \
           separate several folders with ';' on Windows and ':' elsewhere."), Paths, None, None, NUMS;
    scan_library_on_start: bool = false, "Library", "Check new songs when the app starts",
        s("When the app starts, analyse the library songs that have not been checked for fake hi-res and fake \
           lossless yet. Songs checked before are answered from the saved results at once."), Bool, None, None, NUMS;
    lyrics_while_scanning: bool = false, "Library", "Download lyrics while scanning",
        s("While a scan runs, look each song up on LRCLIB as soon as it has been analysed and save its lyrics as \
           set on the Lyrics tab (one request at a time, as LRCLIB asks)."), Bool, None, None, NUMS;

    // ---- Playback (2.0) ---------------------------------------------------
    output_device: String = s(""), "Playback", "Output device",
        s("The sound device the player plays through. Empty = the system's default device."), Str, None, None, NUMS;
    gapless: bool = true, "Playback", "Gapless playback",
        s("Start the next song without a pause, so albums that flow from track to track play as one."), Bool, None,
        None, NUMS;

    // ---- Appearance (2.0) ---------------------------------------------------
    language: String = s(crate::i18n::DEFAULT_LANGUAGE), "Appearance", "Language",
        s("The language of the app's menus, pages and windows: English or 日本語 (Japanese). The information of your \
           songs is shown as it is in their tags, and the command-line tool always writes English."), Choice, None,
        None, &crate::i18n::LANGUAGES;
    font_family: String = s(""), "Appearance", "Font",
        s("The font of the app. Empty = SF Pro, the system font, on macOS and Inter on Windows and Linux (SF Pro may \
           only be used on Apple systems). Any font installed on your computer can be chosen."), Str, None, None, NUMS;
    theme: String = s("Match the system"), "Appearance", "Theme",
        s("The colours of the app: \"Match the system\" (light or dark as the system is), a built-in theme (Light, \
           Dark, Midnight, Graphite, Forest, Ocean, Sepia, Rose), one of your own themes, or a theme file put into \
           the themes folder."), Str, None, None, NUMS;

    // ---- Detection ------------------------------------------------------
    cliff_db: f64 = 25.0, "Detection", "Upsampling cliff (dB)",
        s("Minimum level drop at the Nyquist frequency of a lower sample rate (22.05 kHz for 44.1 kHz, 24 kHz for \
           48 kHz, ...) that marks a file as upsampled. Lower = stricter, more files flagged."),
        Float, Some(5.0), Some(150.0), NUMS;
    cliff_strong_db: f64 = 40.0, "Detection", "High-confidence cliff (dB)",
        s("A cliff at least this deep is reported with high confidence."), Float, Some(5.0), Some(200.0), NUMS;
    lossy_cliff_db: f64 = 25.0, "Detection", "Lossy cut-off drop (dB)",
        s("Minimum drop within ~1 kHz that counts as an MP3/AAC encoder low-pass (the sign of a fake FLAC/WAV made \
           from a lossy file)."), Float, Some(5.0), Some(150.0), NUMS;
    lossy_min_khz: f64 = 11.0, "Detection", "Lossy cut-off search from (kHz)",
        s("Lowest frequency at which a lossy encoder low-pass is searched for."), Float, Some(4.0), Some(22.0), NUMS;
    lossy_max_khz: f64 = 20.3, "Detection", "Lossy cut-off search up to (kHz)",
        s("Highest frequency at which a lossy low-pass is searched for. Raising it towards 21 kHz also catches \
           320 kbps MP3s but may flag genuine CD masters with a steep filter."), Float, Some(10.0), Some(23.0), NUMS;
    ultrasonic_khz: f64 = 24.5, "Detection", "Ultrasonic content expected above (kHz)",
        s("A high-sample-rate file with no content above this frequency (and no cliff) is reported as SUSPICIOUS."),
        Float, Some(15.0), Some(60.0), NUMS;
    steep_drop_db: f64 = 20.0, "Detection", "Brick-wall edge (dB)",
        s("Drop across the bandwidth edge that counts as a brick-wall filter."), Float, Some(5.0), Some(150.0), NUMS;
    content_margin_db: f64 = 10.0, "Detection", "Content above noise floor (dB)",
        s("How far above the noise floor a frequency band must be to count as real content."),
        Float, Some(1.0), Some(60.0), NUMS;
    relevant_range_db: f64 = 110.0, "Detection", "Ignore content below peak by (dB)",
        s("Content more than this far below the loudest part of the spectrum is ignored (removes resampler \
           artefacts)."), Float, Some(40.0), Some(200.0), NUMS;
    bit_threshold_pct: f64 = 0.5, "Detection", "Bit-usage threshold (%)",
        s("A bit position counts as used when at least this share of samples uses it. Real 24-bit audio uses its \
           lowest bit in ~50 % of samples."), Float, Some(0.001), Some(25.0), NUMS;
    deep_cliff_db: f64 = 45.0, "Detection", "Deep upsampling cliff (dB)",
        s("Minimum drop from just below a lower Nyquist frequency to the quietest part of the band above it \
           (104-130 % of that frequency). Catches soft resamplers such as FFmpeg's default one, whose cliff is too \
           gradual for the narrow test."), Float, Some(10.0), Some(200.0), NUMS;
    deep_cliff_strong_db: f64 = 60.0, "Detection", "High-confidence deep cliff (dB)",
        s("A deep cliff at least this large is reported with high confidence."), Float, Some(10.0), Some(250.0), NUMS;
    mid_ratio: f64 = 0.975, "Detection", "Resampler mid-point ratio",
        s("Expert: where resamplers put their -6 dB point relative to the source Nyquist (about 0.955 for soxr, 1.0 \
           for FFmpeg's default); used to choose between neighbouring source rates (44.1 vs 48 kHz)."),
        Float, Some(0.8), Some(1.1), NUMS;
    analog_floor_db: f64 = 15.0, "Detection", "Converter noise above rounding noise (dB)",
        s("A dull high-rate file counts as really recorded at its rate when its 26-40 kHz band holds an even noise \
           floor at least this far above the rounding noise (real converters leave 25-40 dB; an upsample leaves \
           about 0-5 dB)."), Float, Some(6.0), Some(60.0), NUMS;
    grid_strength: f64 = 0.35, "Detection", "Value-grid strength",
        s("Expert: how strongly the sample values must line up on an even grid before a file counts as a scaled or \
           dithered 16-bit master (0-1; real 24-bit audio stays below 0.05, a scaled 16-bit master reaches \
           0.85-1.0)."), Float, Some(0.1), Some(0.95), NUMS;

    // ---- Scanning -------------------------------------------------------
    jobs: i64 = 0, "Scanning", "Parallel jobs (0 = automatic)",
        format!("How many files are analysed at the same time. 0 uses one job per CPU core ({} on this computer), \
                 which is the fastest setting: more jobs than cores do not scan faster, they only use more memory. \
                 Up to 512 are allowed.", cpu_count()),
        Int, Some(0.0), Some(512.0), NUMS;
    max_seconds: f64 = 0.0, "Scanning", "Analyse at most (seconds, 0 = whole file)",
        s("Analysing only the start of each file is faster but can miss content."), Float, Some(0.0), Some(36000.0), NUMS;
    quick_scan_seconds: f64 = 90.0, "Scanning", "Quick scan length (seconds)",
        s("Length analysed when 'Quick scan' is ticked in the toolbar."), Float, Some(5.0), Some(3600.0), NUMS;
    recursive: bool = true, "Scanning", "Include sub-folders", s("Scan folders recursively."), Bool, None, None, NUMS;
    extensions: String = s(DEFAULT_EXTENSIONS), "Scanning", "File extensions",
        s("Space- or comma-separated list of file extensions to scan."), Str, None, None, NUMS;

    // ---- Saved results --------------------------------------------------
    save_results: bool = true, "Saved results", "Save results and reuse them",
        s("Remember the result of every scanned file, so a file that has not changed since is not analysed again: on \
           the next scan, after restarting and after updating Real-Hi-Res. Changing a Detection setting analyses the \
           files again."), Bool, None, None, NUMS;
    rescan_outdated: bool = false, "Saved results", "Re-scan results of older detection rules",
        s("When an update of Real-Hi-Res improves the detection rules, results saved by the older rules are kept and \
           marked. Turn this on to analyse those files again automatically during the next scan; otherwise use \
           'Re-scan files checked by older rules'."), Bool, None, None, NUMS;
    saved_graphs_mb: i64 = 500, "Saved results", "Space for saved graphs (MB, 0 = none)",
        s("Each file's spectrogram and spectrum take about 30-90 KB. Above this size the graphs viewed longest ago are \
           dropped (their results stay) and drawn again when the file is selected."),
        Int, Some(0.0), Some(100000.0), NUMS;

    // ---- Display --------------------------------------------------------
    shown_verdicts: Vec<String> = vec![s("FAKE LOSSLESS"), s("FAKE HI-RES"), s("SUSPICIOUS")], "Display",
        "Verdicts listed by default", s("Which verdicts the result list shows when the app starts."),
        Multi, None, None, VERDICTS;
    show_summary_popup: bool = true, "Display", "Show summary after each scan",
        s("Pop up the percentage breakdown when a scan finishes."), Bool, None, None, NUMS;
    double_click: String = s("Open folder"), "Display", "Double-click / Enter opens",
        s("What double-clicking a row (or pressing Enter) does."), Choice, None, None, &["Open folder", "Open file"];
    view_mode: String = s("Files"), "Display", "Start in view",
        s("Which view the result area shows when the app starts."), Choice, None, None,
        &["Files", "By album", "By artist"];
    card_label: String = s("Percentage"), "Display", "Album/artist rings show",
        s("What the ring on each album/artist card shows: the share of fake or suspicious songs, or how many songs \
           out of the total."), Choice, None, None, &["Percentage", "Songs (x of n)"];
    group_sort: String = s("Most fake first"), "Display", "Sort albums/artists by",
        s("Order of the album and artist cards."), Choice, None, None, &["Most fake first", "Name"];
    colormap: String = s("inferno"), "Display", "Spectrogram colours", s("Colour map of the spectrogram."),
        Choice, None, None, MAPS;
    spectrogram_range_db: f64 = 120.0, "Display", "Spectrogram range (dB)",
        s("Dynamic range shown in the spectrogram; quieter detail is drawn black."), Float, Some(40.0), Some(200.0), NUMS;

    // ---- Fixing ---------------------------------------------------------
    fix_output: String = s("Replace the file"), "Fixing", "Fix files by",
        s("'Replace the file' rewrites the file itself at its real resolution, keeping its name, folder, tags and \
           cover art, so its properties (sample rate, bit depth) show the actual values. The other choices write a \
           corrected copy instead and leave the file alone."), Choice, None, None,
        &["Replace the file", "Saving a copy next to it", "Saving a copy into a folder"];
    fix_backup: String = s("Move the original to a backup folder"), "Fixing", "Original when replacing",
        s("What happens to the original file when it is replaced. The backup folder is 'Real-Hi-Res backups' inside \
           the file's folder unless a folder is set below."), Choice, None, None,
        &["Move the original to a backup folder", "Keep it as <name>.bak next to the file", "Delete it (no backup)"];
    fix_backup_folder: String = s(""), "Fixing", "Backup folder",
        s("Where originals are moved when replacing. Empty = a 'Real-Hi-Res backups' folder next to each file."),
        Str, None, None, NUMS;
    fix_folder: String = s(""), "Fixing", "Folder for copies",
        s("Used with 'Saving a copy into a folder'. Empty = a 'Fixed' folder next to each original."),
        Str, None, None, NUMS;
    fix_format: String = s("Same as the original"), "Fixing", "Output format",
        s("'Same as the original' keeps FLAC as FLAC, WAV as WAV, ALAC as ALAC, AIFF as AIFF, WavPack as WavPack \
           (others become FLAC). 'FLAC' always writes FLAC."), Choice, None, None, &["Same as the original", "FLAC"];
    fix_bits: String = s("16-bit for CD-sourced files"), "Fixing", "Target bit depth",
        s("'As detected' keeps the bit depth actually found in the file. Resampling a 16-bit CD master to 96 kHz \
           fills the low bits, so such files are detected as 24-bit; '16-bit for CD-sourced files' restores 16-bit \
           when the file was upsampled from 44.1/48 kHz or made from a lossy file. 'Always 16-bit' converts \
           everything to 16-bit."), Choice, None, None,
        &["As detected", "16-bit for CD-sourced files", "Always 16-bit"];
    fix_suffix: String = s(" [{bits}-{rate}]"), "Fixing", "File name suffix",
        s("Added to the file name of fixed copies; {bits} and {rate} are replaced, e.g. 'Song [16-44.1].flac'. Only \
           used for copies; a replaced file keeps its name."), Str, None, None, NUMS;
    fix_verify: bool = true, "Fixing", "Re-check fixed files", s("Analyse every fixed file again and show its new verdict."),
        Bool, None, None, NUMS;
    fix_after_scan: String = s("Do nothing"), "Fixing", "After a scan",
        s("What happens when a scan finds fake hi-res or fake lossless files that can be fixed. 'Ask whether to fix \
           them' asks once after the scan; 'Fix them automatically' opens the Fix window and starts at once, with the \
           options above (originals still go to the backup folder; if you chose to delete originals, you are still \
           asked first). Only the desktop app does this; the command-line tool fixes with --fix."),
        Choice, None, None, &["Do nothing", "Ask whether to fix them", "Fix them automatically"];

    // ---- Lyrics (synced lyrics from LRCLIB, as LRCGET) ------------------
    lyrics_existing: String = s("Skip songs that have synced lyrics"), "Lyrics", "Songs that already have lyrics",
        s("Which songs are looked up. A song has synced lyrics when <name>.lrc is next to it, and plain lyrics when \
           <name>.txt is. 'Skip songs that have synced lyrics' still looks up songs that only have a .txt, to add an \
           .lrc (the .txt is kept). Lyrics files are never deleted; 'Download again and replace them' overwrites \
           them."), Choice, None, None,
        &["Skip songs that have synced lyrics", "Skip songs that have any lyrics", "Download again and replace them"];
    lyrics_plain: bool = true, "Lyrics", "Save plain lyrics as .txt",
        s("When LRCLIB has only plain (unsynced) lyrics for a song, save them as <name>.txt next to it. Off: such \
           songs are left without lyrics."), Bool, None, None, NUMS;
    lyrics_instrumental: bool = true, "Lyrics", "Mark instrumental songs",
        s("Save an .lrc holding '[au: instrumental]' for songs LRCLIB lists as instrumental, as LRCGET does, so they \
           are not looked up again."), Bool, None, None, NUMS;
    lyrics_search: bool = true, "Lyrics", "Search when there is no exact match",
        s("When LRCLIB has no record with the song's exact title, artist, album and length, search by title and \
           artist and take a result with the same title, the same artist and a length within 2 seconds. This finds \
           songs whose album is named differently on LRCLIB. Off: exact matches only (one request per song instead \
           of up to two)."), Bool, None, None, NUMS;
    lyrics_save_as: String = s(LYRICS_SAVE_CHOICES[0]), "Lyrics", "Save lyrics as",
        s("Where downloaded lyrics go. An .lrc file next to the song works with every player and never changes the \
           audio file. 'Inside the audio file' writes synced lyrics into the file's tag, as LRCGET does (MP3: SYLT and \
           USLT; FLAC: LYRICS and UNSYNCEDLYRICS); only the tag is rewritten, the audio itself is not changed, and \
           other formats get an .lrc file instead. 'Plain lyrics inside the audio file' stores the text without \
           timing, which more players show."), Choice, None, None, &LYRICS_SAVE_CHOICES;
    lyrics_server: String = s("https://lrclib.net"), "Lyrics", "LRCLIB server",
        s("Address of the LRCLIB server the lyrics come from. Change it only for a self-hosted LRCLIB."),
        Str, None, None, NUMS;
}

/// Validate/convert a JSON value for a field (Python `_coerce`); None = invalid.
fn coerce(f: &FieldInfo, v: &Value) -> Option<SValue> {
    let num = match f.kind {
        Kind::Bool => return Some(SValue::Bool(v.truthy())),
        Kind::Str => {
            return Some(SValue::Str(match v {
                Value::Str(s) => s.clone(),
                Value::Int(i) => i.to_string(),
                Value::Float(x) => crate::pyfmt::repr_float(*x),
                Value::Bool(b) => if *b { "True" } else { "False" }.into(),
                Value::Null => "None".into(),
                other => json::dumps(other, json::Style::DEFAULT),
            }))
        }
        Kind::Choice => {
            let s = v.as_str()?;
            return f.choices.contains(&s).then(|| SValue::Str(s.to_string()));
        }
        Kind::Multi => {
            let items = v.as_array()?;
            return Some(SValue::List(
                items.iter().filter_map(Value::as_str).filter(|s| f.choices.contains(s)).map(str::to_string).collect(),
            ));
        }
        Kind::Paths => {
            let mut out: Vec<String> = vec![];
            for item in v.as_array()? {
                if let Some(p) = item.as_str().map(crate::pyunicode::strip).filter(|p| !p.is_empty()) {
                    if !out.iter().any(|o| o == p) {
                        out.push(p.to_string());
                    }
                }
            }
            return Some(SValue::List(out));
        }
        Kind::Int => match v {
            Value::Int(i) => *i as f64,
            Value::Float(x) if x.is_finite() => x.trunc(),
            Value::Bool(b) => *b as i64 as f64,
            Value::Str(s) => s.trim().parse::<i64>().ok()? as f64,
            _ => return None,
        },
        Kind::Float => match v {
            Value::Int(i) => *i as f64,
            Value::Float(x) => *x,
            Value::Bool(b) => *b as i64 as f64,
            Value::Str(s) => s.trim().parse::<f64>().ok()?,
            _ => return None,
        },
    };
    if !num.is_finite() {
        return None;
    }
    if f.min.is_some_and(|m| num < m) || f.max.is_some_and(|m| num > m) {
        return None;
    }
    Some(if f.kind == Kind::Int { SValue::Int(num as i64) } else { SValue::Float(num) })
}

impl Settings {
    pub fn extension_set(&self) -> BTreeSet<String> {
        self.extensions
            .replace(',', " ")
            .split_whitespace()
            .filter(|p| !p.trim_matches('.').is_empty())
            .map(|p| format!(".{}", p.trim_start_matches('.')).to_lowercase())
            .collect()
    }

    pub fn field(name: &str) -> Option<FieldInfo> {
        Self::field_info().into_iter().find(|f| f.name == name)
    }

    pub fn to_json(&self) -> Value {
        let mut items: Vec<(String, Value)> =
            Self::field_info().iter().map(|f| (f.name.to_string(), self.get(f.name).unwrap().to_json())).collect();
        items.push(("_version".into(), Value::Int(SETTINGS_VERSION)));
        Value::Object(items)
    }

    pub fn from_json(data: &Value) -> Settings {
        let mut s = Settings::default();
        let Some(items) = data.as_object() else {
            return s;
        };
        let mut items = items.clone();
        let get = |items: &Vec<(String, Value)>, k: &str| items.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
        let set = |items: &mut Vec<(String, Value)>, k: &str, v: Value| {
            if let Some(slot) = items.iter_mut().find(|(n, _)| n == k) {
                slot.1 = v;
            } else {
                items.push((k.to_string(), v));
            }
        };
        let fix_output = get(&items, "fix_output").and_then(|v| v.as_str().map(str::to_string));
        if fix_output.as_deref() == Some("Replace the original (keep .bak)") && get(&items, "fix_backup").is_none() {
            set(&mut items, "fix_backup", "Keep it as <name>.bak next to the file".into());
        }
        let migrated = match fix_output.as_deref() {
            Some("Next to the original") | Some("Replace the original (keep .bak)") => Some("Replace the file"),
            Some("Into a folder") => Some("Saving a copy into a folder"),
            _ => None,
        };
        if let Some(m) = migrated {
            set(&mut items, "fix_output", m.into());
        }
        // Files saved before 1.6.0 stored every field, so "Saving a copy next to it" there is just the
        // migrated old default, not a choice: switch them to the new default (replace the file).
        let version = get(&items, "_version").and_then(|v| v.as_f64()).unwrap_or(0.0);
        if version < SETTINGS_VERSION as f64
            && get(&items, "fix_output").and_then(|v| v.as_str().map(str::to_string)).as_deref()
                == Some("Saving a copy next to it")
        {
            set(&mut items, "fix_output", "Replace the file".into());
        }
        for f in Self::field_info() {
            if let Some(v) = get(&items, f.name) {
                if let Some(x) = coerce(&f, &v) {
                    s.put(f.name, x);
                }
            }
        }
        s
    }

    /// Set one field from text (CLI `--set name=value`).
    pub fn set_value(&mut self, name: &str, raw: &str) -> Result<(), String> {
        let Some(f) = Self::field(name) else {
            let known: Vec<&str> = Self::field_info().iter().map(|f| f.name).collect();
            return Err(format!("unknown setting '{name}' (known: {})", known.join(", ")));
        };
        let v = match f.kind {
            Kind::Multi => Value::Array(
                raw.split(',').map(str::trim).filter(|v| !v.is_empty()).map(|v| Value::Str(v.to_uppercase())).collect(),
            ),
            Kind::Bool => Value::Bool(["1", "true", "yes", "on"].contains(&raw.trim().to_lowercase().as_str())),
            Kind::Paths => Value::Array(
                raw.split(if cfg!(windows) { ';' } else { ':' })
                    .map(crate::pyunicode::strip)
                    .filter(|v| !v.is_empty())
                    .map(|v| Value::Str(v.to_string()))
                    .collect(),
            ),
            _ => Value::Str(raw.to_string()),
        };
        match coerce(&f, &v) {
            Some(x) => {
                self.put(name, x);
                Ok(())
            }
            None => Err(format!("invalid value for {name}: {}", py_str_repr(raw))),
        }
    }

    /// Set a field from an already typed value (the GUI's Settings window).
    pub fn set(&mut self, name: &str, v: SValue) -> Result<(), String> {
        let f = Self::field(name).ok_or_else(|| format!("unknown setting '{name}'"))?;
        match coerce(&f, &v.to_json()) {
            Some(x) => {
                self.put(name, x);
                Ok(())
            }
            None => Err(format!("invalid value for {}", f.label)),
        }
    }
}

pub fn home() -> PathBuf {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn env_dir(var: &str) -> Option<PathBuf> {
    std::env::var_os(var).filter(|v| !v.is_empty()).map(PathBuf::from)
}

pub fn config_dir() -> PathBuf {
    if cfg!(windows) {
        env_dir("APPDATA").unwrap_or_else(|| home().join("AppData").join("Roaming")).join("RealHiRes")
    } else if cfg!(target_os = "macos") {
        home().join("Library").join("Application Support").join("RealHiRes")
    } else {
        env_dir("XDG_CONFIG_HOME").unwrap_or_else(|| home().join(".config")).join("realhires")
    }
}

pub fn default_path() -> PathBuf {
    env_dir("REALHIRES_SETTINGS").unwrap_or_else(|| config_dir().join("settings.json"))
}

pub fn load(path: Option<&Path>) -> Settings {
    let path = path.map(Path::to_path_buf).unwrap_or_else(default_path);
    match std::fs::read_to_string(&path) {
        Ok(text) => match json::parse(&text) {
            Ok(v) => Settings::from_json(&v),
            Err(_) => Settings::default(),
        },
        Err(_) => Settings::default(),
    }
}

pub fn save(settings: &Settings, path: Option<&Path>) -> std::io::Result<PathBuf> {
    let path = path.map(Path::to_path_buf).unwrap_or_else(default_path);
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let mut tmp = path.clone().into_os_string();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    std::fs::write(&tmp, json::dumps(&settings.to_json(), json::Style { ensure_ascii: true, ..json::Style::PRETTY }))?;
    std::fs::rename(&tmp, &path)?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_and_validation() {
        let mut s = Settings::default();
        s.cliff_db = 30.0;
        s.shown_verdicts = vec!["ERROR".into()];
        let back = Settings::from_json(&s.to_json());
        assert_eq!(back, s);
        let bad = json::parse(r#"{"cliff_db": 1, "jobs": "x", "colormap": "nope", "recursive": 0}"#).unwrap();
        let t = Settings::from_json(&bad);
        assert_eq!(t.cliff_db, 25.0);
        assert_eq!(t.jobs, 0);
        assert_eq!(t.colormap, "inferno");
        assert!(!t.recursive);
    }

    #[test]
    fn migrates_old_fix_output() {
        let v = json::parse(r#"{"fix_output": "Replace the original (keep .bak)"}"#).unwrap();
        let s = Settings::from_json(&v);
        assert_eq!(s.fix_output, "Replace the file");
        assert_eq!(s.fix_backup, "Keep it as <name>.bak next to the file");
        let v = json::parse(r#"{"fix_output": "Saving a copy next to it", "_version": 150}"#).unwrap();
        assert_eq!(Settings::from_json(&v).fix_output, "Replace the file");
        let v = json::parse(r#"{"fix_output": "Saving a copy next to it", "_version": 160}"#).unwrap();
        assert_eq!(Settings::from_json(&v).fix_output, "Saving a copy next to it");
    }

    #[test]
    fn set_value_from_text() {
        let mut s = Settings::default();
        s.set_value("cliff_db", "30").unwrap();
        s.set_value("shown_verdicts", "error, real hi-res").unwrap();
        s.set_value("recursive", "no").unwrap();
        assert_eq!(s.cliff_db, 30.0);
        assert_eq!(s.shown_verdicts, vec!["ERROR".to_string(), "REAL HI-RES".to_string()]);
        assert!(!s.recursive);
        assert!(s.set_value("cliff_db", "1").is_err());
        assert!(s.set_value("nope", "1").is_err());
        assert!(s.extension_set().contains(".flac"));
    }
}
