//! The music player window (2.0), as `docs/player-spec.md` describes it: a
//! sidebar with the library, a playback bar with the "LCD", the pages
//! (`pages.rs`), the Up Next panel and the Playback info and Equalizer windows.
//! The 1.x scanner (`app::App`) lives on as the Quality check page.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

use egui::{pos2, vec2, Align, Align2, Color32, CornerRadius, Frame, Layout, Margin, Rect, RichText, Sense, Stroke, Ui};
use realhires::i18n::{self, tr_name, tr_text};
use realhires::musiclib::{self, Album, Group, Library, Track};
use realhires::playerstate::{self, PlayerState};
use realhires::playqueue::{repeat_label, shuffle_label, PlayQueue, Song, REPEAT_MODES, SHUFFLE_MODES};
use realhires::pyunicode::casefold;
use realhires::settings::Settings;
use realhires::{n_, tr};

use crate::app::App;
use crate::audio::{Engine, Event};
use crate::cover_cache::Covers;
use crate::icons::{self, Icon};
use crate::look::{self, Palette};

#[derive(Clone, Debug, PartialEq)]
pub enum Page {
    /// the start page with the fifteen entries
    Library,
    AllSongs,
    /// a library folder or one of its subfolders (None: the library folders)
    Folders(Option<String>),
    Albums,
    Album(String),
    Artists,
    AlbumArtists,
    Genres,
    Years,
    Composers,
    /// one artist, album artist, genre, year or composer (a card of a grouping page)
    Group(&'static str, String),
    Playlists,
    Playlist(String),
    Liked,
    Disliked,
    RecentlyPlayed,
    Recent,
    /// a place of Your Stats (2.4), by `places::place_key()`
    Place(String),
    Stats,
    StatsDetails,
    /// the leaderboard of Your Stats as a page of its own (2.4)
    MostPlayed,
    Search,
    NowPlaying,
    Settings,
    Quality,
    /// a custom tab (2.3), by its name
    Tab(String),
    /// the Moods page (2.4): a card per mood and per tempo
    Moods,
    /// a card's songs (2.4): ("mood" or "tempo", its label in English)
    Mood(&'static str, String),
}

impl Page {
    /// The page's name in English (also used as an id and as the queue's
    /// name); `shown_title()` is the name in the language in use.
    pub fn title(&self) -> &'static str {
        match self {
            Page::Library => n_!("Library"),
            Page::AllSongs => n_!("All Songs"),
            Page::Folders(_) => n_!("Folders"),
            Page::Albums | Page::Album(_) => n_!("Albums"),
            Page::Artists => n_!("Artists"),
            Page::AlbumArtists => n_!("Album Artists"),
            Page::Genres => n_!("Genres"),
            Page::Years => n_!("Years"),
            Page::Composers => n_!("Composers"),
            Page::Group(kind, _) => GROUPINGS.iter().find(|g| g.0 == *kind).map(|g| g.1).unwrap_or(n_!("Artists")),
            Page::Playlists | Page::Playlist(_) => n_!("Playlists"),
            Page::Liked => n_!("Liked Songs"),
            Page::Disliked => n_!("Disliked Songs"),
            Page::RecentlyPlayed => n_!("Recently Played"),
            Page::Recent => n_!("Recently Added"),
            Page::Stats | Page::StatsDetails => n_!("Your Stats"),
            Page::MostPlayed => n_!("Most Played"),
            Page::Search => n_!("Search"),
            Page::NowPlaying => n_!("Now Playing"),
            Page::Settings => n_!("Settings"),
            Page::Quality => n_!("Quality check"),
            Page::Tab(_) => n_!("Tabs"),
            Page::Place(_) => crate::places::PLACES_TITLE,
            Page::Moods | Page::Mood(..) => n_!("Moods"),
        }
    }

    /// The page's name in the language in use.
    pub fn shown_title(&self) -> &'static str {
        i18n::tr(self.title())
    }
}

/// The names a queue gets from a page rather than from the library (see
/// `Page::title()`; "Songs" is the queue of a single song).
const PAGE_QUEUE_NAMES: [&str; 21] = [
    "Library", "All Songs", "Folders", "Albums", "Artists", "Album Artists", "Genres", "Years", "Composers",
    "Playlists", "Liked Songs", "Disliked Songs", "Recently Played", "Recently Added", "Your Stats", "Search",
    "Now Playing", "Settings", "Quality check", n_!("Songs"), "Most Played",
];

/// The queue's name as "Playing from …" shows it: the queue keeps the English
/// name of a page (or "Search: …"), shown in the language in use; an album,
/// artist or playlist name stays as it is.
pub fn shown_queue_name(name: &str) -> String {
    if PAGE_QUEUE_NAMES.contains(&name) {
        return i18n::tr(name).to_string();
    }
    if let Some(q) = name.strip_prefix("Search: ") {
        return tr!("Search: {query}", query = q);
    }
    tr_name(name).to_string()
}

/// A count in the language in use, with thousands separators:
/// `count(n, n_!("1 song"), n_!("{n} songs"))` ("1 song", "1,234 songs").
pub fn count(n: usize, one: &'static str, many: &'static str) -> String {
    if n == 1 {
        i18n::tr(one).to_string()
    } else {
        i18n::fill(i18n::tr(many), &[("n", realhires::stats::thousands(n as i64))])
    }
}

/// A count of a noun of the pages ("1 song", "12 songs", "3 albums") in the language in
/// use, as the Python app's `player_pages.plural()` (its `COUNTS`).
pub fn plural(n: usize, noun: &str) -> String {
    let (one, many) = match noun {
        "song" => (n_!("1 song"), n_!("{n} songs")),
        "album" => (n_!("1 album"), n_!("{n} albums")),
        "minute" => (n_!("1 minute"), n_!("{n} minutes")),
        "artist" => (n_!("1 artist"), n_!("{n} artists")),
        "album artist" => (n_!("1 album artist"), n_!("{n} album artists")),
        "genre" => (n_!("1 genre"), n_!("{n} genres")),
        "year" => (n_!("1 year"), n_!("{n} years")),
        "composer" => (n_!("1 composer"), n_!("{n} composers")),
        "folder" => (n_!("1 folder"), n_!("{n} folders")),
        "playlist" => (n_!("1 playlist"), n_!("{n} playlists")),
        "play" => (n_!("1 play"), n_!("{n} plays")),
        _ => {
            let k = realhires::stats::thousands(n as i64);
            return if n == 1 { format!("{k} {noun}") } else { format!("{k} {noun}s") };
        }
    };
    count(n, one, many)
}

/// The texts of Rename… for each grouping that has it, whole sentences for the
/// translations (the Python app's `RENAME_TEXTS`): (menu entry, title, prompt for the
/// songs without one, prompt, question for the songs without one, question, nothing to
/// change).
pub struct RenameTexts {
    pub menu: &'static str,
    pub title: &'static str,
    pub prompt_unknown: &'static str,
    pub prompt: &'static str,
    pub ask_unknown: &'static str,
    pub ask: &'static str,
    pub same: &'static str,
}

pub fn rename_texts(kind: &str) -> RenameTexts {
    match kind {
        "genres" => RenameTexts {
            menu: n_!("Rename genre…"),
            title: n_!("Rename genre"),
            prompt_unknown: n_!("Give the songs without a genre this genre:"),
            prompt: n_!("New name of the genre “{name}”:"),
            ask_unknown: n_!("Give {n} without a genre the genre “{new}”?"),
            ask: n_!("Rename the genre “{name}” to “{new}”? The genre of {n} changes."),
            same: n_!("Every song already has the genre “{new}”: nothing to change."),
        },
        "composers" => RenameTexts {
            menu: n_!("Rename composer…"),
            title: n_!("Rename composer"),
            prompt_unknown: n_!("Give the songs without a composer this composer:"),
            prompt: n_!("New name of the composer “{name}”:"),
            ask_unknown: n_!("Give {n} without a composer the composer “{new}”?"),
            ask: n_!("Rename the composer “{name}” to “{new}”? The composer of {n} changes."),
            same: n_!("Every song already has the composer “{new}”: nothing to change."),
        },
        _ => RenameTexts {
            menu: n_!("Rename year…"),
            title: n_!("Rename year"),
            prompt_unknown: n_!("Give the songs without a year this year:"),
            prompt: n_!("Change the year {name} of these songs to:"),
            ask_unknown: n_!("Give {n} without a year the year “{new}”?"),
            ask: n_!("Change the year {name} to {new}? The release date of {n} changes (only its year)."),
            same: n_!("Every song already has the year “{new}”: nothing to change."),
        },
    }
}

/// The quality badge of a song ("Hi-Res Lossless", "Lossless" or the codec
/// with its bit rate: the core's text) in the language in use.
pub fn quality_label(t: &Track) -> String {
    tr_text(&musiclib::quality(t))
}

/// "Artist — Album" under a song's title, with the names of missing tags translated.
pub fn artist_album(t: &Track) -> String {
    format!("{} — {}", tr_name(&t.display_artist()), tr_name(&t.display_album()))
}

/// A small window that asks for a name (new playlist, preset, theme...).
pub struct NameDialog {
    pub title: String,
    pub text: String,
    pub action: NameAction,
    pub error: String,
    /// what the field is for ("Name" when empty)
    pub prompt: String,
    /// names to choose from (Merge into…); the field can still be typed into
    pub choices: Vec<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum NameAction {
    NewPlaylist(Vec<String>),
    RenamePlaylist(String),
    SaveEqPreset,
    RenameEqPreset(String),
    NewTheme(String),
    RenameTheme(String),
    /// rename a genre, composer or year (the kind, its name)
    RenameGroup(&'static str, String),
    /// merge genres into the name typed or chosen (the kind, their names)
    MergeGroups(&'static str, Vec<String>),
}

/// A question before something is deleted.
#[derive(Clone, Debug, PartialEq)]
pub enum ConfirmAction {
    DeletePlaylist(String),
    /// Delete Tab… (2.3), by its name
    DeleteTab(String),
    DeleteEqPreset(String),
    DeleteTheme(String),
    ClearHistory,
    /// Forget play locations… (2.4)
    ForgetPlaces,
    /// write a new genre, composer or year into these files (the question, the kind, the
    /// paths, the new name, the groups it came from)
    RetagGroup { title: String, text: String, kind: &'static str, paths: Vec<String>, name: String, from: Vec<String> },
}

/// The song being listened to, for the play history.
struct Listening {
    token: u64,
    path: String,
    started: f64,
    seconds: f64,
    last_pos: f64,
}

fn wall_clock() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// What the automatic MusicBrainz lookup posts (2.1).
enum MbMsg {
    Progress(usize, usize),
    /// None when it was stopped
    Done(Option<Vec<realhires::musicbrainz::AutoOutcome>>),
}

enum LibMsg {
    Progress(usize, usize),
    Done(Vec<Track>),
}

/// The library, grouped for the pages.
#[derive(Default)]
pub struct LibView {
    pub tracks: Vec<Track>,
    pub by_path: HashMap<String, usize>,
    pub albums: Vec<Album>,
    pub album_index: HashMap<String, usize>,
    pub artists: Vec<Group>,
    pub album_artists: Vec<Group>,
    pub genres: Vec<Group>,
    pub years: Vec<Group>,
    pub composers: Vec<Group>,
    /// album indices, newest first
    pub recent: Vec<usize>,
    pub songs: Vec<Song>,
    /// the first four songs of each album (what its cover is made from), made once
    pub album_paths: Vec<Vec<String>>,
    /// per grouping ("artists"...), each group's albums (indices, in album order), made once
    pub group_albums: HashMap<&'static str, Vec<Vec<usize>>>,
    /// the artists that stand on their own (`musiclib::known_artists`), for split artist fields
    pub known: std::collections::HashSet<String>,
    /// a new number for each library read, so that the pages' caches know when to start again
    pub gen: u64,
}

static LIB_GEN: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

/// The groupings of the pages: (page kind, title, noun).
/// The key that selects with a click, as the Genres page's hint names it (Ctrl-click
/// is a right-click on macOS).
pub const MOD_NAME: &str = if cfg!(target_os = "macos") { "Cmd" } else { "Ctrl" };

pub const GROUPINGS: [(&str, &str, &str); 5] = [
    ("artists", n_!("Artists"), "artist"),
    ("album_artists", n_!("Album Artists"), "album artist"),
    ("genres", n_!("Genres"), "genre"),
    ("years", n_!("Years"), "year"),
    ("composers", n_!("Composers"), "composer"),
];

impl LibView {
    pub fn new(mut tracks: Vec<Track>) -> LibView {
        musiclib::sort_songs(&mut tracks);
        let by_path = tracks.iter().enumerate().map(|(i, t)| (t.path.clone(), i)).collect();
        let albums = musiclib::albums(&tracks);
        let album_index: HashMap<String, usize> = albums.iter().enumerate().map(|(i, a)| (a.key.clone(), i)).collect();
        let mut recent: Vec<usize> = (0..albums.len()).collect();
        let newest = |a: &Album| a.tracks.iter().map(|t| t.mtime_ns).max().unwrap_or(0);
        recent.sort_by(|a, b| newest(&albums[*b]).cmp(&newest(&albums[*a])));
        let album_paths = albums.iter().map(|a| a.tracks.iter().take(4).map(|t| t.path.clone()).collect()).collect();
        let mut lib = LibView {
            artists: musiclib::artists(&tracks),
            album_artists: musiclib::album_artists(&tracks),
            genres: musiclib::genres(&tracks),
            years: musiclib::years(&tracks),
            composers: musiclib::composers(&tracks),
            songs: tracks.iter().map(Track::song).collect(),
            known: musiclib::known_artists(&tracks),
            tracks,
            by_path,
            albums,
            album_index,
            recent,
            album_paths,
            group_albums: HashMap::new(),
            gen: LIB_GEN.fetch_add(1, Ordering::Relaxed),
        };
        let album_of_path: HashMap<&str, usize> =
            lib.albums.iter().enumerate().flat_map(|(i, a)| a.tracks.iter().map(move |t| (t.path.as_str(), i))).collect();
        for (kind, _, _) in GROUPINGS {
            let per: Vec<Vec<usize>> = lib
                .groups(kind)
                .iter()
                .map(|g| {
                    let mut idx: Vec<usize> = g.tracks.iter().filter_map(|t| album_of_path.get(t.path.as_str()).copied()).collect();
                    idx.sort_unstable();
                    idx.dedup();
                    idx
                })
                .collect();
            lib.group_albums.insert(kind, per);
        }
        drop(album_of_path);
        lib
    }

    pub fn track(&self, path: &str) -> Option<&Track> {
        self.by_path.get(path).map(|i| &self.tracks[*i])
    }

    pub fn album_of(&self, path: &str) -> Option<&Album> {
        let key = self.track(path)?.song().album_key();
        self.album_index.get(&key).map(|i| &self.albums[*i])
    }

    /// The artists of a song's artist field (`musiclib::split_artists()`), each a link.
    pub fn artists_of(&self, t: &Track) -> Vec<String> {
        let names = musiclib::split_artists(&t.display_artist(), &self.known);
        if names.is_empty() {
            vec![t.display_artist()]
        } else {
            names
        }
    }

    /// The groups of a grouping page ("artists", "album_artists", "genres", "years", "composers").
    pub fn groups(&self, kind: &str) -> &Vec<Group> {
        match kind {
            "artists" => &self.artists,
            "album_artists" => &self.album_artists,
            "genres" => &self.genres,
            "years" => &self.years,
            _ => &self.composers,
        }
    }

    /// The index of a group by its name (in any case).
    pub fn find_group(&self, kind: &str, name: &str) -> Option<usize> {
        let key = casefold(name.trim());
        self.groups(kind).iter().position(|g| casefold(g.name.trim()) == key)
    }
}

/// Where Now Playing drew what can be clicked, and what it showed (for the tests).
#[derive(Default)]
pub struct NpHits {
    pub cover: Option<Rect>,
    /// the back link at the top left and the name of the page it returns to (2.3)
    pub back_page: Option<(Rect, String)>,
    /// Lyrics only (2.3)
    pub wide_button: Option<Rect>,
    /// where the lyrics are drawn
    pub lyrics_rect: Option<Rect>,
    /// the album's songs in the left half (2.3): (row, path, drawn bold as the song playing)
    pub album_songs: Vec<(Rect, String, bool)>,
    /// the two dots under the left half: the cover, the album's songs (2.3)
    pub dots: Vec<Rect>,
    /// "Instrumental" under the centred block (2.3)
    pub instrumental: Option<Rect>,
    /// the Details block (2.3), and the view it is centred in
    pub details_block: Option<Rect>,
    pub details_view: Option<Rect>,
    /// the artist and album name links under the cover
    pub links: Vec<(Rect, crate::pages::Link)>,
    pub get_lyrics: Option<Rect>,
    /// A− and A+
    pub size_buttons: Vec<Rect>,
    /// "Back to the current line"
    pub back: Option<Rect>,
    pub up_next: Vec<String>,
    /// Details as "Label: value" lines
    pub details: Vec<String>,
    pub detail_links: Vec<(Rect, String)>,
}

/// Now Playing's right half (2.1): the switch Lyrics | Up Next | Details.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NpTab {
    Lyrics,
    UpNext,
    Details,
}

impl NpTab {
    pub const ALL: [(NpTab, &'static str); 3] =
        [(NpTab::Lyrics, n_!("Lyrics")), (NpTab::UpNext, n_!("Up Next")), (NpTab::Details, n_!("Details"))];
}

/// How Now Playing is laid out (2.3): nothing plays; only the block in the middle (the
/// lyrics hidden); an instrumental song's Lyrics (the block in the middle, the switch above
/// it); Lyrics only (the lyrics fill the page); or the two halves.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NpMode {
    Nothing,
    Centred,
    Instrumental,
    Wide,
    Halves,
}

#[derive(Clone, Debug, PartialEq)]
pub enum ShownLyrics {
    Synced(Vec<(u32, String)>),
    Plain(String),
    Instrumental,
}

/// What the Playback info window shows of the saved Real-Hi-Res verdict
/// (the verdict and its summary, shown in the language in use).
pub struct InfoWindow {
    pub path: String,
    pub verdict: Option<(realhires::verdict::Verdict, String)>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum SongCol {
    Num,
    Title,
    Artist,
    Album,
    AlbumArtist,
    Composer,
    Genre,
    Year,
    Disc,
    Time,
    Quality,
    Format,
    BitRate,
    /// the estimated tempo and mood (2.4, `moodpages.rs`)
    Bpm,
    Mood,
    Added,
    File,
    /// the heart (not sortable)
    Like,
    /// Recently Played's "Played" column
    Played,
}

impl SongCol {
    /// The column of a name in player.json's "columns" (`playerstate::SONG_COLUMNS`).
    pub fn from_name(name: &str) -> Option<SongCol> {
        Some(match name {
            "num" => SongCol::Num,
            "title" => SongCol::Title,
            "artist" => SongCol::Artist,
            "album" => SongCol::Album,
            "album_artist" => SongCol::AlbumArtist,
            "composer" => SongCol::Composer,
            "genre" => SongCol::Genre,
            "year" => SongCol::Year,
            "disc" => SongCol::Disc,
            "time" => SongCol::Time,
            "quality" => SongCol::Quality,
            "format" => SongCol::Format,
            "bit_rate" => SongCol::BitRate,
            "bpm" => SongCol::Bpm,
            "mood" => SongCol::Mood,
            "added" => SongCol::Added,
            "file" => SongCol::File,
            "like" => SongCol::Like,
            _ => return None,
        })
    }

    /// (heading, menu label, width weight, right-aligned); the texts are English,
    /// shown with `i18n::tr()`
    pub fn info(self) -> (&'static str, &'static str, f32, bool) {
        match self {
            SongCol::Num => ("#", n_!("# (track number)"), 0.35, true),
            SongCol::Title => (n_!("Title"), n_!("Title"), 3.0, false),
            SongCol::Artist => (n_!("Artist"), n_!("Artist"), 2.0, false),
            SongCol::Album => (n_!("Album"), n_!("Album"), 2.2, false),
            SongCol::AlbumArtist => (n_!("Album Artist"), n_!("Album Artist"), 1.8, false),
            SongCol::Composer => (n_!("Composer"), n_!("Composer"), 1.6, false),
            SongCol::Genre => (n_!("Genre"), n_!("Genre"), 1.1, false),
            SongCol::Year => (n_!("Year"), n_!("Year"), 0.55, false),
            SongCol::Disc => (n_!("Disc"), n_!("Disc"), 0.45, true),
            SongCol::Time => (n_!("Time"), n_!("Time"), 0.6, true),
            SongCol::Quality => (n_!("Quality"), n_!("Quality"), 1.15, false),
            SongCol::Format => (n_!("Format"), n_!("Format"), 0.7, false),
            SongCol::BitRate => (n_!("Bit rate"), n_!("Bit rate"), 0.9, true),
            SongCol::Bpm => (n_!("BPM"), n_!("BPM (estimated)"), 0.55, true),
            SongCol::Mood => (n_!("Mood"), n_!("Mood (estimated)"), 0.9, false),
            SongCol::Added => (n_!("Date added"), n_!("Date added"), 1.0, false),
            SongCol::File => (n_!("File"), n_!("File"), 1.8, false),
            SongCol::Like => ("", n_!("Heart (like)"), 0.0, false),
            SongCol::Played => (n_!("Played"), n_!("Played"), 1.1, false),
        }
    }
}

/// The Format column: the codec ("FLAC", "MP3"; PCM for WAV and AIFF samples).
pub fn format_label(t: &Track) -> String {
    let c = t.codec.to_lowercase();
    if c.starts_with("pcm_") {
        "PCM".into()
    } else {
        c.to_uppercase()
    }
}

/// A song table's cell (Python `track_value`).
pub fn cell_text(t: &Track, col: SongCol) -> String {
    match col {
        SongCol::Num => if t.track > 0 { t.track.to_string() } else { String::new() },
        SongCol::Title => t.display_title(),
        // "Unknown Artist" and "Unknown Album" in the language in use
        SongCol::Artist => tr_name(&t.display_artist()).to_string(),
        SongCol::Album => tr_name(&t.display_album()).to_string(),
        SongCol::AlbumArtist => t.album_artist.clone(),
        SongCol::Composer => t.composer.clone(),
        SongCol::Genre => t.genre.clone(),
        SongCol::Year => t.year.clone(),
        SongCol::Disc => if t.disc > 0 { t.disc.to_string() } else { String::new() },
        SongCol::Time => fmt_time(t.duration),
        SongCol::Quality => quality_label(t),
        SongCol::Format => format_label(t),
        SongCol::BitRate => {
            if t.bit_rate > 0 {
                format!("{} kbps", realhires::stats::thousands(realhires::pyfmt::round_int(t.bit_rate as f64 / 1000.0)))
            } else {
                String::new()
            }
        }
        SongCol::Added => {
            if t.mtime_ns <= 0 {
                return String::new();
            }
            let p = realhires::stats::local_parts(t.mtime_ns as f64 / 1e9);
            tr_text(&realhires::stats::day_label(&format!("{:04}-{:02}-{:02}", p.year, p.month, p.day)))
        }
        SongCol::File => realhires::paths::file_name(&t.path),
        SongCol::Bpm => crate::moodpages::cell(&t.path, false),
        SongCol::Mood => crate::moodpages::cell(&t.path, true),
        SongCol::Like | SongCol::Played => String::new(),
    }
}

/// What a click on a column heading sorts by (Python `track_sort`; numbers padded).
pub fn sort_key(t: &Track, col: SongCol) -> (String, String, String) {
    let title = casefold(&t.display_title());
    let n = |x: i64| if x == 0 { 9999 } else { x };
    match col {
        SongCol::Artist => (casefold(&t.display_artist()), casefold(&t.display_album()), format!("{:04}{:04}", t.disc, t.track)),
        SongCol::Num | SongCol::Disc => (format!("{:05}{:05}", n(t.disc), n(t.track)), title, t.path.clone()),
        SongCol::Time => (format!("{:012.3}", t.duration), title, t.path.clone()),
        SongCol::Quality => {
            let (bits, rate, float, kbps) = musiclib::quality_sort(t);
            (format!("{bits:04}{rate:09}{}{kbps:012}", float as u8), title, t.path.clone())
        }
        SongCol::BitRate => (format!("{:012}", t.bit_rate), title, t.path.clone()),
        SongCol::Added => (format!("{:020}", t.mtime_ns), title, t.path.clone()),
        SongCol::Bpm => (crate::moodpages::bpm_sort(&t.path), title, t.path.clone()),
        SongCol::Title => (title, casefold(&t.display_artist()), t.path.clone()),
        other => (casefold(&cell_text(t, other)), title, t.path.clone()),
    }
}

pub struct NowPlaying {
    pub token: u64,
    pub path: String,
    /// false once the queue ended or playback was stopped
    pub active: bool,
}

pub struct Player {
    pub scanner: App,
    pub ctx: egui::Context,
    pub state: PlayerState,
    state_dirty: bool,
    state_saved: Instant,
    pub lib: LibView,
    lib_rx: Option<Receiver<LibMsg>>,
    pub lib_progress: Option<(usize, usize)>,
    lib_stop: Arc<AtomicBool>,
    pub page: Page,
    pub back: Vec<Page>,
    /// the pages left with Back (the mouse's back button, Alt+Left), for Forward (2.1)
    pub forward: Vec<Page>,
    pub search: String,
    search_due: Option<Instant>,
    pub search_query: String,
    search_from: Option<Page>,
    pub engine: Engine,
    pub queue: Option<PlayQueue>,
    pub queue_name: String,
    pub now: Option<NowPlaying>,
    /// a message for the LCD's second line, and when playback moves on after it
    pub message: Option<(String, Option<Instant>)>,
    fail_streak: usize,
    /// the lyrics of the song playing: (path, lyrics_gen, lyrics, (label, source))
    lyrics: Option<(String, u64, Option<ShownLyrics>, (String, String))>,
    pub covers: Covers,
    pub show_up_next: bool,
    /// Now Playing without its lyrics column (the Lyrics button on that page, 2.1)
    pub lyrics_hidden: bool,
    /// Now Playing's right half: Lyrics, Up Next or Details (for the session)
    pub np_tab: NpTab,
    /// Now Playing's Lyrics only (2.3): no cover, the lyrics fill the page (for the session)
    pub lyrics_wide: bool,
    /// Now Playing's left half shows the album's songs instead of the cover (2.3, for the session)
    pub np_songs: bool,
    /// the press that turned the left half already (one turn per drag)
    pub(crate) np_swiped: bool,
    /// sideways scrolling over the left half since it last turned
    pub(crate) np_hscroll: f32,
    /// the album whose songs the left half showed last (scrolled to the song playing when it changes)
    pub(crate) np_songs_album: String,
    /// Details: per song its comment tag (None while ffprobe reads it) and saved verdict
    details_extra: HashMap<String, (Option<String>, Option<(String, String)>)>,
    details_song: String,
    details_tx: Sender<(String, String)>,
    details_rx: Receiver<(String, String)>,
    pub eq_open: bool,
    pub eq_tab: usize,
    /// the Equalizer window's outer rectangle and its content's, as drawn last (2.3: the
    /// content must lie inside the window in every language)
    pub eq_rects: Option<(Rect, Rect)>,
    pub info: Option<InfoWindow>,
    pub settings_group: usize,
    settings_draft: Option<Settings>,
    settings_due: Option<Instant>,
    /// the user scrolled the current line of synced lyrics out of view: no following, and
    /// the "Back to the current line" button shows (2.1)
    pub lyrics_away: bool,
    /// the Back button was pressed: the current line is scrolled to at once
    pub lyrics_back: bool,
    /// the user scrolled the lyrics: whether the current line is in view is checked again
    pub lyrics_check: bool,
    pub np_hits: NpHits,
    /// the LCD's links: the title and the album name (the album), each artist
    pub lcd_links: Vec<(Rect, crate::pages::Link)>,
    /// the line followed last
    pub lyrics_line: Option<usize>,
    pub song_sort: (SongCol, bool),
    applied_look: Option<(String, String, u64)>,
    /// the `language` setting in use (egui draws every frame, so a new
    /// language shows at once, whichever window changed the setting)
    applied_language: String,
    pub font_used: String,
    pub selected_song: Option<String>,
    /// "Check new songs when the app starts": scan once the library is read
    check_after_update: bool,
    /// the genres selected (Select, or Ctrl/Cmd-click) on the Genres page, to be merged or renamed
    pub group_selected: Vec<String>,
    /// the Genres page's select mode (2.3): a click on a card selects it
    pub group_select_mode: bool,
    /// the sort of album pages (None: track order) and the album it is for
    pub album_sort: Option<(SongCol, bool)>,
    album_sort_for: String,
    /// sorted song lists, made again only when the library, the sort or the likes change
    list_cache: HashMap<String, ((u64, Option<(SongCol, bool)>, u64), Arc<Vec<String>>)>,
    /// counts likes and dislikes, for the lists made of them
    pub likes_gen: u64,
    /// renaming a genre, composer or year, or merging genres (one at a time)
    pub tag_job: Option<crate::editinfo::RenameJob>,
    seed: u64,
    // 2.0 additions
    pub custom_themes: Vec<realhires::themes::Theme>,
    /// the themes of the files in the themes folder (read again every 2 s on the Settings page)
    pub file_themes: Vec<realhires::themes::FileTheme>,
    pub file_themes_read: Instant,
    themes_gen: u64,
    /// the Edit Info window (one at a time)
    pub edit_info: Option<crate::editinfo::EditInfo>,
    /// the Cover window (one at a time)
    pub cover_win: Option<crate::coverwin::CoverWin>,
    /// the MusicBrainz window (2.1, one at a time)
    pub mb_win: Option<crate::mbwin::MbWindow>,
    /// the automatic MusicBrainz lookup (2.1): its messages, stop flag, progress and last summary
    mb_rx: Option<Receiver<MbMsg>>,
    mb_stop: Arc<AtomicBool>,
    pub mb_progress: Option<(usize, usize)>,
    /// `musicbrainz::auto_summary()` of the last run (English, translated when drawn)
    pub mb_summary: String,
    pub mb_last: Option<Vec<realhires::musicbrainz::AutoOutcome>>,
    mb_forced: bool,
    /// the play history (`library.sqlite`)
    pub history: Option<Library>,
    pub history_gen: u64,
    listening: Option<Listening>,
    pub stats_period: String,
    stats_cache: Option<(String, u64, realhires::stats::Stats)>,
    /// 2.4: the leaderboard's switch ("songs", "artists", "albums"), kept for the session, and
    /// the period's leaderboards (made again when the period or the plays change)
    pub leaderboard_kind: &'static str,
    pub boards_cache: Option<(String, u64, crate::leaderboard::Boards)>,
    /// the calendar of Your Stats (2.1)
    pub calendar: crate::library_pages::Calendar,
    recent_cache: Option<(u64, Vec<(String, f64)>)>,
    pub name_dialog: Option<NameDialog>,
    pub confirm: Option<ConfirmAction>,
    /// Colour Label → Custom… (2.3): the colour picker window
    pub label_picker: Option<crate::labels::LabelPicker>,
    /// what the last EQ import said
    pub eq_notes: Vec<String>,
    /// the playlist whose page is shown (its songs get Remove/Move)
    pub table_playlist: Option<String>,
    /// 2.4: the subfolders (path, name, songs) the next song table shows above its songs,
    /// in the same scrolling list (the Folders page); taken by the table
    pub table_folders: Vec<(String, String, usize)>,
    /// the custom tabs of the tabs folder (2.3), the files there that are not tabs (and
    /// why), the tab window, and a number that changes with the tabs
    pub tabs: Vec<realhires::smarttabs::Tab>,
    pub tab_problems: Vec<(String, String)>,
    pub tab_win: Option<crate::tabs::TabWindow>,
    pub tabs_gen: u64,
    pub tab_hits: crate::tabs::TabHits,
    /// where songs were played (2.4): the place the plays are stored with, and its lookup
    pub places: crate::places::PlacesState,
    /// the songs' moods and their background analysis (2.4, `moodpages.rs`)
    pub moods: crate::moodpages::MoodState,
}

fn seed_now() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(1)
}

pub fn gain_of(state: &PlayerState) -> f32 {
    if state.muted {
        0.0
    } else {
        (state.volume * state.volume) as f32
    }
}

/// The gain the engine gets: exactly 1 (0 when muted) with bit-perfect playback,
/// which keeps the saved volume for when it is turned off.
pub fn engine_gain(state: &PlayerState, bit_perfect: bool) -> f32 {
    if bit_perfect && !state.muted {
        1.0
    } else {
        gain_of(state)
    }
}

/// What bit-perfect playback needs to know about a library song (rate 0: the engine reads the file).
pub fn song_of(t: Option<&Track>) -> realhires::bitperfect::Song {
    match t {
        Some(t) => realhires::bitperfect::Song {
            rate: t.sample_rate.max(0) as u32,
            channels: t.channels.max(0) as u32,
            bits: t.bits.max(0) as u32,
            is_float: t.is_float,
            is_lossy: t.is_lossy,
        },
        None => Default::default(),
    }
}

impl Player {
    pub fn new(ctx: &egui::Context, cfg: Settings, initial: Vec<String>) -> Player {
        realhires::i18n::set_language(&cfg.language);
        let quality_first = !initial.is_empty();
        let scanner = App::new(ctx, cfg, initial);
        let state = playerstate::load(None);
        let tracks = Library::open(None).map(|l| l.tracks()).unwrap_or_default();
        let mut engine = Engine::new();
        let (details_tx, details_rx) = mpsc::channel();
        engine.set_gain(engine_gain(&state, scanner.cfg.bit_perfect));
        engine.set_eq(&state.eq);
        engine.set_gapless(scanner.cfg.gapless);
        engine.set_bit_perfect(scanner.cfg.bit_perfect);
        let mut p = Player {
            covers: Covers::new(ctx),
            ctx: ctx.clone(),
            lib: LibView::new(tracks),
            lib_rx: None,
            lib_progress: None,
            lib_stop: Arc::new(AtomicBool::new(false)),
            page: if quality_first { Page::Quality } else { Page::Library },
            back: vec![],
            forward: vec![],
            search: String::new(),
            search_due: None,
            search_query: String::new(),
            search_from: None,
            engine,
            queue: None,
            queue_name: String::new(),
            now: None,
            message: None,
            fail_streak: 0,
            lyrics: None,
            show_up_next: false,
            lyrics_hidden: false,
            np_tab: NpTab::Lyrics,
            lyrics_wide: false,
            np_songs: false,
            np_swiped: false,
            np_hscroll: 0.0,
            np_songs_album: String::new(),
            details_extra: HashMap::new(),
            details_song: String::new(),
            details_tx,
            details_rx,
            eq_open: false,
            eq_tab: 0,
            eq_rects: None,
            info: None,
            settings_group: 0,
            settings_draft: None,
            settings_due: None,
            lyrics_away: false,
            lyrics_back: false,
            lyrics_check: false,
            np_hits: NpHits::default(),
            lcd_links: vec![],
            lyrics_line: None,
            song_sort: (SongCol::Artist, false),
            applied_look: None,
            applied_language: scanner.cfg.language.clone(),
            font_used: String::new(),
            selected_song: None,
            check_after_update: false,
            group_selected: vec![],
            group_select_mode: false,
            album_sort: None,
            album_sort_for: String::new(),
            list_cache: HashMap::new(),
            likes_gen: 0,
            tag_job: None,
            custom_themes: realhires::themes::load_custom(None),
            file_themes: vec![],
            file_themes_read: Instant::now(),
            themes_gen: 0,
            edit_info: None,
            cover_win: None,
            mb_win: None,
            mb_rx: None,
            mb_stop: Arc::new(AtomicBool::new(false)),
            mb_progress: None,
            mb_summary: String::new(),
            mb_last: None,
            mb_forced: false,
            history: Library::open(None).ok(),
            history_gen: 0,
            listening: None,
            stats_period: realhires::stats::DEFAULT_PERIOD.to_string(),
            stats_cache: None,
            leaderboard_kind: "songs",
            boards_cache: None,
            calendar: Default::default(),
            recent_cache: None,
            name_dialog: None,
            confirm: None,
            label_picker: None,
            eq_notes: vec![],
            table_playlist: None,
            table_folders: vec![],
            tabs: vec![],
            tab_problems: vec![],
            tab_win: None,
            tabs_gen: 0,
            tab_hits: Default::default(),
            places: Default::default(),
            moods: Default::default(),
            seed: seed_now(),
            state,
            state_dirty: false,
            state_saved: Instant::now(),
            scanner,
        };
        p.file_themes = realhires::themes::load_folder(&p.custom_themes, None);
        p.reload_tabs(); // 2.3: the tabs folder
        p.load_moods(); // 2.4: the moods analysed before
        p.covers.library_changed(&p.lib.albums);
        p.fill_choices();
        p.engine.set_device(&p.scanner.cfg.output_device.clone());
        p.update_library();
        p.check_after_update = p.scanner.cfg.scan_library_on_start && p.lib_rx.is_some();
        // the song of last time, paused where it was
        let last = p.state.last_path.clone();
        if !last.is_empty() && p.lib.by_path.contains_key(&last) && std::path::Path::new(&last).is_file() {
            let pos = p.state.last_position;
            // go on with the song's album, as after playing it from there
            let (list, name) = match p.lib.album_of(&last) {
                Some(a) => (a.tracks.iter().map(|t| t.path.clone()).collect(), a.title.clone()),
                None => (vec![last.clone()], "Songs".to_string()),
            };
            p.make_queue(list, &name);
            if let Some(q) = p.queue.as_mut() {
                q.start(&last);
            }
            let song = song_of(p.lib.track(&last));
            let token = p.engine.cue(&last, pos, song);
            p.now = Some(NowPlaying { token, path: last, active: true });
            p.update_next();
        }
        p
    }

    // ------------------------------------------------------------ library ---

    /// Read the tags of new and changed files in the library folders.
    pub fn update_library(&mut self) {
        if self.lib_rx.is_some() {
            return;
        }
        let cfg = self.scanner.cfg.clone();
        if cfg.library_folders.is_empty() {
            return;
        }
        let (tx, rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        self.lib_stop = stop.clone();
        let ctx = self.ctx.clone();
        std::thread::Builder::new()
            .name("library-update".into())
            .spawn(move || {
                let files = musiclib::library_files(&cfg);
                let _ = tx.send(LibMsg::Progress(0, files.len()));
                let Ok(mut lib) = Library::open(None) else {
                    let _ = tx.send(LibMsg::Done(vec![]));
                    return;
                };
                let jobs = realhires::effective_jobs(cfg.jobs).min(8);
                let mut last = Instant::now();
                let tracks = lib.refresh(
                    &files,
                    jobs,
                    &mut |done, total| {
                        if last.elapsed() > Duration::from_millis(200) || done == total {
                            last = Instant::now();
                            let _ = tx.send(LibMsg::Progress(done, total));
                            ctx.request_repaint();
                        }
                    },
                    Some(&stop),
                );
                let _ = tx.send(LibMsg::Done(tracks));
                ctx.request_repaint();
            })
            .expect("library thread");
        self.lib_rx = Some(rx);
        self.lib_progress = Some((0, 0));
    }

    fn poll_library(&mut self) {
        let Some(rx) = &self.lib_rx else { return };
        let mut done = None;
        while let Ok(m) = rx.try_recv() {
            match m {
                LibMsg::Progress(d, t) => self.lib_progress = Some((d, t)),
                LibMsg::Done(tracks) => done = Some(tracks),
            }
        }
        if let Some(tracks) = done {
            self.lib_rx = None;
            self.lib_progress = None;
            self.lib = LibView::new(tracks);
            // covers stay unless an album's songs changed; then the grid covers not on disk yet
            self.covers.library_changed(&self.lib.albums);
            self.covers.prefetch(&self.lib.albums);
            if let Some(q) = self.queue.as_mut() {
                q.library = self.lib.songs.clone();
            }
            if std::mem::take(&mut self.check_after_update) && !self.scanner.scanning {
                let page = self.page.clone();
                self.check_library();
                self.scanner.quiet_scan = true;
                self.page = page;
            }
            self.analyse_moods(false); // 2.4: the new and changed songs' moods, in the background
            self.lookup_library(false);
        }
    }

    // ------------------------------------------------- MusicBrainz (2.1) ---

    /// Open the MusicBrainz window for one song (one at a time).
    pub fn musicbrainz_song(&mut self, path: &str) {
        let (heading, sub) = match self.lib.track(path) {
            Some(t) => (
                t.display_title(),
                [t.artist.as_str(), t.album.as_str()].iter().filter(|x| !x.is_empty()).cloned().collect::<Vec<_>>().join(" · "),
            ),
            None => (realhires::paths::file_name(path), String::new()),
        };
        self.open_musicbrainz(vec![path.to_string()], false, heading, sub);
    }

    /// Open the MusicBrainz window for every song of an album.
    pub fn musicbrainz_album(&mut self, album: &Album) {
        let n = album.tracks.len();
        let count = count(n, n_!("1 song"), n_!("{n} songs"));
        let sub = [tr_name(&album.artist), count.as_str()].iter().filter(|x| !x.is_empty()).cloned().collect::<Vec<_>>().join(" · ");
        let paths = album.tracks.iter().map(|t| t.path.clone()).collect();
        self.open_musicbrainz(paths, true, album.title.clone(), sub);
    }

    pub fn open_musicbrainz(&mut self, paths: Vec<String>, album: bool, heading: String, subheading: String) {
        let paths: Vec<String> = paths.into_iter().filter(|p| std::path::Path::new(p).is_file()).collect();
        if self.mb_win.is_some() || paths.is_empty() {
            return;
        }
        let cfg = self.scanner.cfg.clone();
        self.mb_win = Some(crate::mbwin::MbWindow::new(&self.ctx, &cfg, paths, album, heading, subheading));
    }

    pub fn musicbrainz_busy(&self) -> bool {
        self.mb_rx.is_some()
    }

    /// Look the library's songs that miss information up on MusicBrainz in a
    /// thread: after each library update when `musicbrainz_auto` is on, or by
    /// hand (`force`, Settings → Library).  False when it did not start.
    pub fn lookup_library(&mut self, force: bool) -> bool {
        let cfg = self.scanner.cfg.clone();
        if self.mb_rx.is_some() || self.lib.tracks.is_empty() || (!force && !cfg.musicbrainz_auto) {
            return false;
        }
        use realhires::musicbrainz as mb;
        self.mb_forced = force;
        let known: HashMap<String, (Vec<(String, String)>, f64)> =
            self.lib.tracks.iter().map(|t| (t.path.clone(), (mb::index_tags(t), t.duration))).collect();
        let paths: Vec<String> = self.lib.tracks.iter().map(|t| t.path.clone()).collect();
        let (tx, rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        self.mb_stop = stop.clone();
        let ctx = self.ctx.clone();
        let _ = std::thread::Builder::new().name("musicbrainz-auto".into()).spawn(move || {
            let out = match mb::LookupLog::open(None) {
                Err(e) => Some(vec![mb::AutoOutcome { status: mb::FAILED.into(), message: e.to_string(), ..Default::default() }]),
                Ok(log) => {
                    let mut client = mb::MusicBrainzClient::new(&cfg.musicbrainz_server).with_stop(stop);
                    let p = tx.clone();
                    let c = ctx.clone();
                    match mb::run_auto(&mut client, &paths, Some(&cfg), &log, &|path| known.get(path).cloned(), move |i, n| {
                        let _ = p.send(MbMsg::Progress(i, n));
                        c.request_repaint();
                    }) {
                        Ok(o) => Some(o),
                        Err(mb::MbError::Stopped) => None,
                        Err(mb::MbError::Failed(m)) => {
                            Some(vec![mb::AutoOutcome { status: mb::FAILED.into(), message: m, ..Default::default() }])
                        }
                    }
                }
            };
            let _ = tx.send(MbMsg::Done(out));
            ctx.request_repaint();
        });
        self.mb_rx = Some(rx);
        self.mb_progress = Some((0, 0));
        self.mb_summary.clear();
        true
    }

    fn poll_musicbrainz(&mut self) {
        let Some(rx) = &self.mb_rx else { return };
        let mut done = None;
        while let Ok(m) = rx.try_recv() {
            match m {
                MbMsg::Progress(i, n) => self.mb_progress = Some((i, n)),
                MbMsg::Done(o) => done = Some(o),
            }
        }
        let Some(out) = done else { return };
        self.mb_rx = None;
        self.mb_progress = None;
        let Some(out) = out else {
            self.mb_summary.clear();
            return;
        };
        // after an automatic run that asked nothing the status stays quiet; a run by hand always answers
        let quiet = out.is_empty() && !self.mb_forced;
        self.mb_summary = if quiet { String::new() } else { realhires::musicbrainz::auto_summary(&out) };
        let filled = out.iter().any(|o| o.status == realhires::musicbrainz::FILLED);
        self.mb_last = Some(out);
        if filled {
            self.update_library();
        }
    }

    /// The sidebar's status line: the library update, else the MusicBrainz lookup.
    pub fn status_line(&self) -> Option<String> {
        if let Some(s) = self.library_status() {
            return Some(s);
        }
        match self.mb_progress {
            Some((i, n)) if i < n => {
                Some(tr!("Getting song information from MusicBrainz: {i} of {n} albums", i = realhires::stats::thousands(i as i64 + 1), n = realhires::stats::thousands(n as i64)))
            }
            Some(_) => None,
            None if !self.mb_summary.is_empty() => Some(tr_text(&self.mb_summary)),
            // 2.4: the mood analysis's progress, the least important text
            None => self.mood_status_line(),
        }
    }

    pub fn library_status(&self) -> Option<String> {
        let (done, total) = self.lib_progress?;
        Some(if total == 0 {
            tr!("Updating the library: looking for songs…").into()
        } else {
            tr!("Updating the library: {done} of {total} songs", done = done, total = total)
        })
    }

    // ---------------------------------------------------------- playback ---

    fn make_queue(&mut self, list: Vec<String>, name: &str) {
        self.seed = self.seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        let mut q = PlayQueue::new(self.lib.songs.clone(), list, &self.state.shuffle, &self.state.repeat, self.seed);
        q.disliked = self.state.disliked.iter().cloned().collect();
        self.queue = Some(q);
        self.queue_name = name.to_string();
    }

    /// Play `list` (paths) from `start` (None: from the first song, or a random
    /// one when shuffle is on); `name` is shown as "Playing from …".
    pub fn play_list(&mut self, list: Vec<String>, start: Option<&str>, name: &str) {
        // songs whose files are gone (in a playlist) are skipped
        let list: Vec<String> = list.into_iter().filter(|p| self.lib.by_path.contains_key(p)).collect();
        if list.is_empty() {
            return;
        }
        self.make_queue(list.clone(), name);
        let first = match start {
            Some(p) => p.to_string(),
            None if self.state.shuffle != "off" => {
                let i = (self.seed >> 33) as usize % list.len();
                list[i].clone()
            }
            None => list[0].clone(),
        };
        let path = self.queue.as_mut().unwrap().start(&first);
        self.fail_streak = 0;
        self.play_path(&path);
    }

    /// The Shuffle button of a page: shuffle on (songs of the list) and play.
    pub fn shuffle_list(&mut self, list: Vec<String>, name: &str) {
        if self.state.shuffle == "off" {
            self.state.shuffle = "songs".into();
            self.state_dirty = true;
        }
        self.play_list(list, None, name);
    }

    fn play_path(&mut self, path: &str) {
        let song = song_of(self.lib.track(path));
        let token = self.engine.play(path, 0.0, song);
        self.now = Some(NowPlaying { token, path: path.to_string(), active: true });
        self.message = None;
        self.update_next();
        self.state.last_path = path.to_string();
        self.state.last_position = 0.0;
        self.state_dirty = true;
    }

    /// Play Next (`first`) / Add to Queue (2.1): the songs come after the current one (Play
    /// Next before songs queued earlier, Add to Queue after them); with nothing playing they
    /// start playing.
    pub fn queue_songs(&mut self, paths: &[String], first: bool) {
        let paths: Vec<String> = paths.iter().filter(|p| self.lib.track(p).is_some()).cloned().collect();
        if paths.is_empty() {
            return;
        }
        let playing = self.now.as_ref().is_some_and(|n| n.active);
        match self.queue.as_mut() {
            Some(q) if playing && q.current().is_some() => {
                if first {
                    q.play_next(&paths);
                } else {
                    q.add_to_queue(&paths);
                }
                self.update_next();
            }
            _ => self.play_list(paths, None, "Queue"),
        }
    }

    /// Tell the engine which song follows gaplessly.
    pub fn update_next(&mut self) {
        let Some(q) = self.queue.as_ref() else {
            self.engine.set_next(None);
            return;
        };
        // what the queue plays when the current song ends by itself, asked of a copy (as the
        // Python app does), so repeat, the album rules and queued songs all count
        let next = q.clone().next(true);
        match next {
            Some(p) => {
                let song = song_of(self.lib.track(&p));
                self.engine.set_next(Some((&p, song)));
            }
            None => self.engine.set_next(None),
        }
    }

    pub fn is_playing(&self) -> bool {
        self.now.as_ref().is_some_and(|n| n.active) && !self.engine.is_paused()
    }

    pub fn toggle_play(&mut self) {
        match &self.now {
            None => {
                let list = self.page_songs();
                let name = self.page.title().to_string();
                self.play_list(list, None, &name);
            }
            Some(n) if !n.active => {
                let p = n.path.clone();
                self.play_path(&p);
            }
            Some(_) => {
                if self.engine.is_paused() {
                    self.engine.resume();
                } else {
                    self.engine.pause();
                    self.remember_position();
                }
            }
        }
    }

    pub fn next(&mut self) {
        let n = self.queue.as_mut().and_then(|q| q.next(false));
        match n {
            Some(p) => self.play_path(&p),
            None => self.stop_at_end(),
        }
    }

    pub fn previous(&mut self) {
        if self.engine.position().unwrap_or(0.0) > 3.0 {
            self.engine.seek(0.0);
            return;
        }
        match self.queue.as_mut().and_then(|q| q.previous()) {
            Some(p) => self.play_path(&p),
            None => self.engine.seek(0.0),
        }
    }

    fn stop_at_end(&mut self) {
        self.engine.stop();
        if let Some(n) = self.now.as_mut() {
            n.active = false;
        }
        self.state.last_position = 0.0;
        self.state_dirty = true;
    }

    pub fn seek(&mut self, seconds: f64) {
        if self.now.as_ref().is_some_and(|n| !n.active) {
            let p = self.now.as_ref().unwrap().path.clone();
            self.play_path(&p);
        }
        self.engine.seek(seconds);
    }

    pub fn set_shuffle(&mut self, mode: &str) {
        self.state.shuffle = mode.to_string();
        if let Some(q) = self.queue.as_mut() {
            q.set_shuffle(mode);
        }
        self.update_next();
        self.state_dirty = true;
    }

    pub fn set_repeat(&mut self, mode: &str) {
        self.state.repeat = mode.to_string();
        if let Some(q) = self.queue.as_mut() {
            q.set_repeat(mode);
        }
        self.update_next();
        self.state_dirty = true;
    }

    pub fn toggle_like(&mut self, path: &str) {
        let liked = self.state.is_liked(path);
        self.state.set_liked(path, !liked);
        self.likes_gen += 1;
        self.save_state();
    }

    pub fn set_volume(&mut self, v: f64) {
        if self.scanner.cfg.bit_perfect {
            return; // the volume is not used then: it is set on the amplifier or DAC
        }
        self.state.volume = v.clamp(0.0, 1.0);
        self.engine.set_gain(engine_gain(&self.state, false));
        self.state_dirty = true;
    }

    pub fn toggle_mute(&mut self) {
        self.state.muted = !self.state.muted;
        self.engine.set_gain(engine_gain(&self.state, self.scanner.cfg.bit_perfect));
        self.state_dirty = true;
    }

    pub fn set_eq(&mut self) {
        self.engine.set_eq(&self.state.eq);
        self.state_dirty = true;
    }

    fn remember_position(&mut self) {
        if let (Some(n), Some(pos)) = (&self.now, self.engine.position()) {
            self.state.last_path = n.path.clone();
            self.state.last_position = if n.active { pos } else { 0.0 };
            self.state_dirty = true;
        }
    }

    pub fn save_state(&mut self) {
        self.remember_position();
        if let Err(e) = playerstate::save(&self.state, None) {
            eprintln!("Real-Hi-Res: cannot save the player state: {e}");
        }
        self.state_dirty = false;
        self.state_saved = Instant::now();
    }

    fn poll_engine(&mut self) {
        // bit-perfect playback may also be switched in the Quality check page's Settings window
        if self.engine.bit_perfect() != self.scanner.cfg.bit_perfect {
            let on = self.scanner.cfg.bit_perfect;
            self.engine.set_gain(engine_gain(&self.state, on));
            self.engine.set_bit_perfect(on);
        }
        for ev in self.engine.take_events() {
            match ev {
                Event::Started { token, path } => {
                    if self.now.as_ref().is_some_and(|n| n.token == token) {
                        continue;
                    }
                    // a song that followed gaplessly
                    let want = self.queue.as_mut().and_then(|q| q.next(true));
                    if want.as_deref() == Some(path.as_str()) {
                        self.now = Some(NowPlaying { token, path: path.clone(), active: true });
                        self.fail_streak = 0;
                        self.state.last_path = path;
                        self.state_dirty = true;
                        self.update_next();
                    } else if let Some(p) = want {
                        self.play_path(&p);
                    } else {
                        self.stop_at_end();
                    }
                }
                Event::Ended { token, chained, .. } => {
                    if chained || !self.now.as_ref().is_some_and(|n| n.token == token) {
                        continue;
                    }
                    self.fail_streak = 0;
                    match self.queue.as_mut().and_then(|q| q.next(true)) {
                        Some(p) => self.play_path(&p),
                        None => self.stop_at_end(),
                    }
                }
                Event::Failed { token, message, .. } => {
                    if !self.now.as_ref().is_some_and(|n| n.token == token) {
                        continue;
                    }
                    self.fail_streak += 1;
                    let total = self.queue.as_ref().map(|q| q.order.len()).unwrap_or(1).max(1);
                    let go_on = self.fail_streak < total.min(50);
                    self.message = Some((message, go_on.then(|| Instant::now() + Duration::from_secs(2))));
                    if !go_on {
                        self.stop_at_end();
                    }
                }
                Event::OutputError(m) => {
                    self.message = Some((m, None));
                    self.stop_at_end();
                }
            }
        }
        if let Some((_, Some(at))) = &self.message {
            if Instant::now() >= *at {
                self.message.as_mut().unwrap().1 = None;
                // a manual next, so "repeat song" does not loop over a file that fails
                match self.queue.as_mut().and_then(|q| q.next(false)) {
                    Some(p) => {
                        let keep = self.message.take();
                        self.play_path(&p);
                        // the failure stays visible until the next song plays
                        if self.engine.last_error.is_empty() {
                            let _ = keep;
                        }
                    }
                    None => self.stop_at_end(),
                }
            }
        }
    }

    /// The songs of the page shown (for Play with nothing playing).
    pub fn page_songs(&self) -> Vec<String> {
        let all = || self.lib.tracks.iter().map(|t| t.path.clone()).collect::<Vec<_>>();
        match &self.page {
            Page::Album(key) => self
                .lib
                .album_index
                .get(key)
                .map(|i| self.lib.albums[*i].tracks.iter().map(|t| t.path.clone()).collect())
                .unwrap_or_default(),
            Page::Liked => self.liked_songs().iter().map(|t| t.path.clone()).collect(),
            Page::AllSongs => self.sorted_songs(self.lib.tracks.iter().collect()).iter().map(|t| t.path.clone()).collect(),
            Page::Playlist(id) => self.playlist_songs(id),
            Page::Tab(name) => self
                .tab(name)
                .map(|t| realhires::smarttabs::tab_songs(t, &self.lib.tracks, &self.tab_context()))
                .unwrap_or_default(),
            Page::Group(kind, name) => self
                .lib
                .find_group(kind, name)
                .map(|i| self.lib.groups(kind)[i].tracks.iter().map(|t| t.path.clone()).collect())
                .unwrap_or_default(),
            Page::Disliked => self.lib.tracks.iter().filter(|t| self.state.is_disliked(&t.path)).map(|t| t.path.clone()).collect(),
            Page::Mood(kind, label) => realhires::moods::card_songs(kind, label, &self.lib.tracks, &self.moods.map),
            _ => all(),
        }
    }

    pub fn liked_songs(&self) -> Vec<&Track> {
        self.lib.tracks.iter().filter(|t| self.state.is_liked(&t.path)).collect()
    }

    pub fn sorted_songs<'a>(&self, v: Vec<&'a Track>) -> Vec<&'a Track> {
        sort_tracks(v, Some(self.song_sort))
    }

    /// A song list of a page, sorted, made again only when the library, the sort or the
    /// likes changed: sorting thousands of songs every frame made scrolling lag.
    pub fn cached_list(
        &mut self,
        id: &str,
        sort: Option<(SongCol, bool)>,
        make: impl FnOnce(&Player) -> Vec<String>,
    ) -> Arc<Vec<String>> {
        let key = (self.lib.gen, sort, self.likes_gen);
        if let Some((k, v)) = self.list_cache.get(id) {
            if *k == key {
                return v.clone();
            }
        }
        let v = Arc::new(make(self));
        if self.list_cache.len() > 64 {
            self.list_cache.clear();
        }
        self.list_cache.insert(id.to_string(), (key, v.clone()));
        v
    }

    /// Forget every sorted list (2.4: the moods changed, which the BPM and Mood columns
    /// and the tabs' mood rules use).
    pub fn forget_lists(&mut self) {
        self.list_cache.clear();
    }

    pub fn album_sort_for(&self) -> &str {
        &self.album_sort_for
    }

    /// Another album page is shown: in track order again.
    pub fn set_album_sort_for(&mut self, key: &str) {
        self.album_sort_for = key.to_string();
        self.album_sort = None;
    }

    /// The sort of a kind of song list: album pages have their own (track order at first).
    pub fn sort_of(&self, kind: &str) -> Option<(SongCol, bool)> {
        if kind == "album" {
            self.album_sort
        } else {
            Some(self.song_sort)
        }
    }

    pub fn set_sort(&mut self, kind: &str, col: SongCol) {
        let cur = self.sort_of(kind);
        let next = match cur {
            Some((c, rev)) if c == col => (col, !rev),
            _ => (col, false),
        };
        if kind == "album" {
            self.album_sort = Some(next);
        } else {
            self.song_sort = next;
        }
    }

    /// The page of an artist (Artists, else Album Artists).  Each artist of a field such
    /// as "Mariah Carey, Michael Bublé" has a page of its own; a name that is not an
    /// artist of the library opens the first artist `split_artists()` gives.
    pub fn open_artist(&mut self, name: &str) {
        let mut names = vec![name.to_string()];
        names.extend(musiclib::split_artists(name, &self.lib.known).into_iter().take(1));
        for n in &names {
            for kind in ["artists", "album_artists"] {
                if let Some(i) = self.lib.find_group(kind, n) {
                    let name = self.lib.groups(kind)[i].name.clone();
                    self.go(Page::Group(kind, name));
                    return;
                }
            }
        }
        self.go(Page::Artists);
    }

    // --------------------------------------------- renaming and merging ---

    /// Rename… a genre, composer or year: asks for the new name.
    pub fn ask_rename_group(&mut self, kind: &'static str, name: &str) {
        let texts = rename_texts(kind);
        let unknown = name == unknown_name(kind);
        let prompt = if unknown {
            i18n::tr(texts.prompt_unknown).to_string()
        } else {
            i18n::fill(i18n::tr(texts.prompt), &[("name", name.to_string())])
        };
        let initial = if unknown { "" } else { name };
        self.ask_name_with(i18n::tr(texts.title), &prompt, initial, NameAction::RenameGroup(kind, name.to_string()), vec![]);
    }

    /// Done (or Esc) on the Genres page: out of select mode, nothing selected (2.3).
    pub fn done_selecting(&mut self) {
        self.group_select_mode = false;
        self.group_selected.clear();
    }

    /// Merge… with two or more genres selected: their name is asked for in one dialog
    /// (the selected genres, the biggest first, or a new name), then the files are confirmed.
    pub fn merge_selected(&mut self) {
        if self.group_selected.len() >= 2 {
            self.ask_merge_groups("genres", self.group_selected.clone());
        }
    }

    /// Rename… with exactly one genre selected.
    pub fn rename_selected(&mut self) {
        if let [name] = self.group_selected.as_slice() {
            let name = name.clone();
            self.ask_rename_group("genres", &name);
        }
    }

    /// Merge into… (one genre) or Merge N genres… (several): asks for the genre they become.
    pub fn ask_merge_groups(&mut self, kind: &'static str, names: Vec<String>) {
        let groups: Vec<&Group> = names.iter().filter_map(|n| self.lib.find_group(kind, n)).map(|i| &self.lib.groups(kind)[i]).collect();
        if groups.is_empty() {
            return;
        }
        if groups.len() == 1 {
            let mine = casefold(groups[0].name.trim());
            let others: Vec<String> = self
                .lib
                .groups(kind)
                .iter()
                .filter(|g| casefold(g.name.trim()) != mine && g.name != unknown_name(kind))
                .map(|g| g.name.clone())
                .collect();
            let prompt = tr!(
                "Merge the genre “{name}” into which genre? Choose one, or type a new name.",
                name = tr_name(&groups[0].name)
            );
            self.ask_name_with(tr!("Merge into"), &prompt, "", NameAction::MergeGroups(kind, names), others);
        } else {
            let mut biggest = groups.clone();
            biggest.sort_by_key(|g| std::cmp::Reverse(g.tracks.len()));
            let choices: Vec<String> = biggest.iter().filter(|g| g.name != unknown_name(kind)).map(|g| g.name.clone()).collect();
            let first = choices.first().cloned().unwrap_or_default();
            let k = groups.len();
            self.ask_name_with(
                &tr!("Merge {count} genres", count = k),
                &tr!("Merge {count} genres into one. Its name:", count = k),
                &first,
                NameAction::MergeGroups(kind, names),
                choices,
            );
        }
    }

    /// Check the new name, find the songs that do not have it yet and ask before
    /// changing their files (`ConfirmAction::RetagGroup`); Err keeps the name dialog open.
    pub fn retag_group(&mut self, kind: &'static str, names: &[String], text: &str, merge: bool) -> Result<(), String> {
        // the core's message, in the language in use
        let new = realhires::tagedit::check_group_name(kind, text).map_err(|e| tr_text(&e.0))?;
        if self.tag_job.as_ref().is_some_and(|j| !j.done()) {
            return Err(tr!("Songs are still being changed; try again when that has finished.").into());
        }
        let texts = rename_texts(kind);
        let groups: Vec<usize> = names.iter().filter_map(|n| self.lib.find_group(kind, n)).collect();
        let mut seen = HashSet::new();
        let mut paths = vec![];
        for &g in &groups {
            for t in &self.lib.groups(kind)[g].tracks {
                if group_value(kind, t) != new && seen.insert(t.path.clone()) {
                    paths.push(t.path.clone());
                }
            }
        }
        if paths.is_empty() {
            return Err(i18n::fill(i18n::tr(texts.same), &[("new", new.clone())]));
        }
        let from: Vec<String> = groups.iter().map(|&g| self.lib.groups(kind)[g].name.clone()).collect();
        let n = plural(paths.len(), "song");
        let old = from.first().cloned().unwrap_or_default();
        let (title, what) = if merge && from.len() > 1 {
            let listed: Vec<String> = from.iter().map(|f| tr!("“{name}”", name = tr_name(f))).collect();
            (
                tr!("Merge genres").to_string(),
                tr!(
                    "Merge {count} genres ({listed}) into “{target}”? The genre of {n} becomes “{target}”.",
                    count = from.len(),
                    listed = listed.join(tr!(", ")),
                    target = &new,
                    n = &n
                ),
            )
        } else if merge {
            (
                tr!("Merge genres").to_string(),
                tr!(
                    "Merge the genre “{name}” into “{target}”? The genre of {n} becomes “{target}”.",
                    name = tr_name(&old),
                    target = &new,
                    n = &n
                ),
            )
        } else if old == unknown_name(kind) {
            (i18n::tr(texts.title).to_string(), i18n::fill(i18n::tr(texts.ask_unknown), &[("n", n.clone()), ("new", new.clone())]))
        } else {
            (
                i18n::tr(texts.title).to_string(),
                i18n::fill(i18n::tr(texts.ask), &[("name", old.clone()), ("new", new.clone()), ("n", n.clone())]),
            )
        };
        self.confirm = Some(ConfirmAction::RetagGroup {
            title,
            text: tr!(
                "{question} Only the information in the files is changed; the music itself is not touched.",
                question = what
            ),
            kind,
            paths,
            name: new,
            from,
        });
        Ok(())
    }

    /// The Equalizer's Delete…: the preset named (chosen from its menu), else the one the
    /// equalizer is set to; asks first (`ConfirmAction::DeleteEqPreset`).
    pub fn eq_delete(&mut self, name: Option<String>) -> bool {
        let name = name.or_else(|| Some(self.state.matching_eq_preset()).filter(|n| !n.is_empty()));
        match name {
            Some(n) if self.state.eq_preset(&n).is_some() => {
                self.confirm = Some(ConfirmAction::DeleteEqPreset(n));
                true
            }
            _ => false,
        }
    }

    // ------------------------------------------------------------ lyrics ---

    /// The lyrics of the song playing, read again when lyrics were saved.
    pub fn lyrics_of_now(&mut self) -> Option<ShownLyrics> {
        use realhires::lyrics::Status;
        let path = self.now.as_ref()?.path.clone();
        let gen = self.scanner.lyrics_gen;
        if let Some((p, g, l, _)) = &self.lyrics {
            if *p == path && *g == gen {
                return l.clone();
            }
        }
        let mut file = String::new();
        let shown = realhires::lyrics::read_lyrics_file(&path).and_then(|(kind, text, f)| {
            file = f;
            match kind {
                Status::Instrumental => Some(ShownLyrics::Instrumental),
                Status::Synced => {
                    let lines = realhires::embed::lrc_entries(&text);
                    if lines.is_empty() {
                        let plain = realhires::lyrics::strip_timestamps(&text);
                        (!plain.trim().is_empty()).then_some(ShownLyrics::Plain(plain))
                    } else {
                        Some(ShownLyrics::Synced(lines))
                    }
                }
                _ => (!text.trim().is_empty()).then(|| ShownLyrics::Plain(text)),
            }
        });
        // the label above them says what is shown (synced lyrics without a timed line show as plain)
        let label = match &shown {
            Some(l) => {
                let kind = match l {
                    ShownLyrics::Synced(_) => Status::Synced,
                    ShownLyrics::Plain(_) => Status::Plain,
                    ShownLyrics::Instrumental => Status::Instrumental,
                };
                let (a, b) = realhires::lyrics::shown_label(kind, &path, &file);
                (a.to_string(), b)
            }
            None => (String::new(), String::new()),
        };
        self.lyrics = Some((path, gen, shown.clone(), label));
        self.lyrics_line = None;
        self.lyrics_away = false;
        shown
    }

    /// What Now Playing writes above the lyrics: ("Synced lyrics", ".lrc file"), or empty.
    pub fn lyrics_label_of_now(&mut self) -> (String, String) {
        self.lyrics_of_now();
        self.lyrics.as_ref().map(|l| l.3.clone()).unwrap_or_default()
    }

    /// A+ (+1) / A− (-1) on Now Playing: the lyrics larger or smaller, saved at once.
    pub fn lyrics_size_step(&mut self, steps: i64) {
        let before = self.state.lyrics_size;
        if self.state.step_lyrics_size(steps) != before {
            self.save_state();
        }
    }

    /// Get lyrics (Now Playing, a song's menu): the lyrics window for these songs, which
    /// starts downloading at once; Now Playing shows the lyrics when they are saved.
    pub fn get_lyrics(&mut self, paths: Vec<String>) {
        self.scanner.download_lyrics_now(paths);
    }

    /// Now Playing's Details for the song playing: the comment tag (read by ffprobe in a
    /// thread) and the saved Quality check verdict, read again when the song changes.
    pub fn details_extra(&mut self, path: &str) -> (String, Option<(String, String)>) {
        while let Ok((p, comment)) = self.details_rx.try_recv() {
            if let Some(e) = self.details_extra.get_mut(&p) {
                e.0 = Some(comment);
            }
        }
        if self.details_song != path {
            self.details_song = path.to_string();
            self.details_extra.retain(|_, e| e.0.is_none()); // still being read: the answer is on its way
            self.details_extra.remove(path);
        }
        if !self.details_extra.contains_key(path) {
            let skey = realhires::store::settings_key(&self.scanner.cfg);
            let verdict = self
                .scanner
                .results
                .get(path)
                .cloned()
                .or_else(|| self.scanner.store.as_ref().and_then(|s| s.lookup(path, &skey, None)))
                .map(|r| (crate::style::verdict_style(r.verdict).0.to_string(), r.summary.clone()));
            self.details_extra.insert(path.to_string(), (None, verdict));
            let (tx, p, ctx) = (self.details_tx.clone(), path.to_string(), self.ctx.clone());
            std::thread::spawn(move || {
                let comment = read_comment(&p);
                let _ = tx.send((p, comment));
                ctx.request_repaint();
            });
        }
        let e = &self.details_extra[path];
        (e.0.clone().unwrap_or_default(), e.1.clone())
    }

    // ------------------------------------------------------------ settings ---

    fn apply_look(&mut self, ctx: &egui::Context) {
        if self.scanner.cfg.language != self.applied_language {
            self.applied_language = self.scanner.cfg.language.clone();
            i18n::set_language(&self.applied_language);
            ctx.request_repaint();
        }
        let want = (self.scanner.cfg.font_family.clone(), self.scanner.cfg.theme.clone(), self.themes_gen);
        if self.applied_look.as_ref() != Some(&want) {
            if self.applied_look.as_ref().is_none_or(|a| a.0 != want.0) {
                self.font_used = look::install_fonts(ctx, &want.0);
            }
            look::install_theme(ctx, &want.1, &self.all_themes());
            self.applied_look = Some(want);
        }
    }

    /// The user's themes changed: save them and draw with them.
    pub fn themes_changed(&mut self) {
        if let Err(e) = realhires::themes::save_custom(&self.custom_themes, None) {
            self.scanner.info = Some((tr!("Themes").into(), tr!("The themes could not be saved: {e}.", e = e)));
        }
        self.file_themes = realhires::themes::load_folder(&self.custom_themes, None);
        self.themes_gen += 1;
        self.fill_choices();
    }

    /// The user's own themes, then those of the themes folder.
    pub fn all_themes(&self) -> Vec<realhires::themes::Theme> {
        let mut all = self.custom_themes.clone();
        all.extend(self.file_themes.iter().map(|f| f.theme.clone()));
        all
    }

    /// The file a theme of the themes folder comes from.
    pub fn theme_file(&self, name: &str) -> Option<&std::path::Path> {
        self.file_themes.iter().find(|f| f.theme.name == name).map(|f| f.file.as_path())
    }

    /// Read the themes folder again; true when its themes changed.
    pub fn reload_file_themes(&mut self) -> bool {
        self.file_themes_read = Instant::now();
        let now = realhires::themes::load_folder(&self.custom_themes, None);
        if now == self.file_themes {
            return false;
        }
        self.file_themes = now;
        self.themes_gen += 1;
        self.fill_choices();
        true
    }

    /// Import a theme file into the user's own themes and use it (Import theme…,
    /// or a .json dropped onto the window).
    pub fn import_theme_path(&mut self, path: &std::path::Path) {
        let all = self.all_themes();
        match std::fs::read(path).map_err(|e| e.to_string()).and_then(|b| {
            realhires::themes::import_theme(&String::from_utf8_lossy(&b), &all).map_err(|e| e.0)
        }) {
            Ok(t) => {
                let name = t.name.clone();
                self.custom_themes.push(t);
                self.themes_changed();
                self.set_theme(&name);
            }
            Err(e) => self.scanner.info = Some((tr!("Import theme").into(), tr_text(&e))),
        }
    }

    /// Open the Edit Info window for one song (one at a time).
    pub fn edit_song_info(&mut self, path: &str) {
        let (heading, sub) = match self.lib.track(path) {
            Some(t) => (
                t.display_title(),
                [t.artist.as_str(), t.album.as_str()].iter().filter(|x| !x.is_empty()).cloned().collect::<Vec<_>>().join(" · "),
            ),
            None => (realhires::paths::file_name(path), String::new()),
        };
        self.edit_info(vec![path.to_string()], heading, sub);
    }

    /// Open the Edit Info window for every song of an album.
    pub fn edit_album_info(&mut self, album: &Album) {
        let n = album.tracks.len();
        let count = count(n, n_!("1 song"), n_!("{n} songs"));
        let sub = [tr_name(&album.artist), count.as_str()].iter().filter(|x| !x.is_empty()).cloned().collect::<Vec<_>>().join(" · ");
        self.edit_info(album.tracks.iter().map(|t| t.path.clone()).collect(), tr_name(&album.title).to_string(), sub);
        if let Some(e) = self.edit_info.as_mut() {
            e.album = true; // Acquire Metadata… looks the album up
        }
    }

    /// Open the Edit Info window for these songs; while one is open nothing happens.
    pub fn edit_info(&mut self, paths: Vec<String>, heading: String, subheading: String) {
        let paths: Vec<String> = paths.into_iter().filter(|p| std::path::Path::new(p).is_file()).collect();
        if self.edit_info.is_some() || paths.is_empty() {
            return;
        }
        self.edit_info = Some(crate::editinfo::EditInfo::new(&self.ctx, paths, heading, subheading, false));
    }

    /// Open the Cover window of this song's cover (a click on Now Playing's
    /// cover, Change Cover… in the song and album menus); one at a time, and not
    /// while it works.
    pub fn open_cover_window(&mut self, path: &str) {
        if self.cover_win.as_ref().is_some_and(|w| w.busy || w.path == path) {
            return;
        }
        if !std::path::Path::new(path).is_file() {
            return;
        }
        let album = self.lib.album_of(path).cloned();
        let track = self.lib.track(path).cloned();
        self.cover_win = Some(crate::coverwin::CoverWin::new(&self.ctx, path, album.as_ref(), track.as_ref(), &self.scanner.cfg));
    }

    /// Use the theme called `name` (saved in the settings at once).
    pub fn set_theme(&mut self, name: &str) {
        let mut cfg = self.scanner.cfg.clone();
        cfg.theme = name.to_string();
        self.scanner.apply_settings(cfg.clone());
        if let Some(d) = self.settings_draft.as_mut() {
            d.theme = name.to_string();
        }
    }

    // ----------------------------------------------- history and stats ---

    /// Follow what is heard, and log a song to the play history when it stops
    /// being the current one.
    fn track_listening(&mut self) {
        let cur = self.now.as_ref().filter(|n| n.active).map(|n| (n.token, n.path.clone()));
        let pos = self.engine.position().unwrap_or(0.0);
        let same = match (&self.listening, &cur) {
            (Some(l), Some((token, _))) => l.token == *token,
            (None, None) => true,
            _ => false,
        };
        if !same {
            self.flush_listening();
            if let Some((token, path)) = cur {
                self.listening = Some(Listening { token, path, started: wall_clock(), seconds: 0.0, last_pos: pos });
            }
            return;
        }
        let playing = self.is_playing();
        if let Some(l) = self.listening.as_mut() {
            if playing {
                let d = pos - l.last_pos;
                if d > 0.0 && d < 2.0 {
                    l.seconds += d;
                }
            }
            l.last_pos = pos;
        }
    }

    fn flush_listening(&mut self) {
        let Some(l) = self.listening.take() else { return };
        let track = self.lib.track(&l.path).cloned().unwrap_or_else(|| Track { path: l.path.clone(), ..Default::default() });
        if let Some(h) = &self.history {
            if h.add_play_at(&track, l.started, l.seconds, self.play_place()) {
                self.history_gen += 1;
            }
        }
    }

    /// (stats of the chosen period), computed again when there are new plays.
    pub fn stats(&mut self) -> realhires::stats::Stats {
        let key = (self.stats_period.clone(), self.history_gen);
        if let Some((p, g, st)) = &self.stats_cache {
            if *p == key.0 && *g == key.1 {
                return st.clone();
            }
        }
        let (since, until) = realhires::stats::period_range(&self.stats_period, wall_clock());
        let plays = self.history.as_ref().map(|h| h.plays(since, until)).unwrap_or_default();
        let st = realhires::stats::compute(&plays, &realhires::stats::local_parts);
        self.stats_cache = Some((key.0, key.1, st.clone()));
        st
    }

    /// The songs played last, newest first, with when.
    pub fn recently_played(&mut self) -> Vec<(String, f64)> {
        if let Some((g, v)) = &self.recent_cache {
            if *g == self.history_gen {
                return v.clone();
            }
        }
        let mut v = vec![];
        if let Some(h) = &self.history {
            let paths = h.recently_played(200);
            let plays = h.plays(0.0, None);
            let mut last: HashMap<&str, f64> = HashMap::new();
            for p in &plays {
                last.insert(p.path.as_str(), p.started);
            }
            v = paths.into_iter().map(|p| {
                let t = last.get(p.as_str()).copied().unwrap_or(0.0);
                (p, t)
            }).collect();
        }
        self.recent_cache = Some((self.history_gen, v.clone()));
        v
    }

    pub fn clear_history(&mut self) {
        if let Some(h) = &self.history {
            h.clear_history();
        }
        self.history_gen += 1;
    }

    // ------------------------------------------- playlists and dislikes ---

    pub fn playlist_songs(&self, id: &str) -> Vec<String> {
        self.state.playlist(id).map(|p| p.songs.clone()).unwrap_or_default()
    }

    pub fn new_playlist(&mut self, name: &str, songs: &[String]) -> String {
        let id = self.state.create_playlist(name, songs, wall_clock());
        self.save_state();
        id
    }

    pub fn add_to_playlist(&mut self, id: &str, songs: &[String]) {
        let n = self.state.add_to_playlist(id, songs);
        self.save_state();
        if let Some(p) = self.state.playlist(id) {
            self.message_info(&tr!(
                "{songs} added to “{name}”.",
                songs = crate::style::count(n, n_!("1 song"), n_!("{n} songs")),
                name = p.name
            ));
        }
    }

    fn message_info(&mut self, text: &str) {
        self.scanner.status = text.to_string();
    }

    pub fn toggle_dislike(&mut self, path: &str) {
        let d = self.state.is_disliked(path);
        self.state.set_disliked(path, !d);
        self.likes_gen += 1;
        self.save_state();
        if let Some(q) = self.queue.as_mut() {
            q.disliked = self.state.disliked.iter().cloned().collect();
        }
        self.update_next();
    }

    /// Run what a name dialog was opened for; Err keeps it open with the message.
    pub fn apply_name(&mut self, action: NameAction, text: &str) -> Result<(), String> {
        match action {
            NameAction::NewPlaylist(songs) => {
                if realhires::playerstate::clean_name(text).is_empty() {
                    return Err(tr!("Give the playlist a name.").into());
                }
                let id = self.new_playlist(text, &songs);
                if songs.is_empty() {
                    self.go(Page::Playlist(id));
                }
            }
            NameAction::RenamePlaylist(id) => {
                self.state.rename_playlist(&id, text).map_err(|e| e.0)?;
                self.save_state();
            }
            NameAction::SaveEqPreset => {
                let eq = self.state.eq.clone();
                self.state.save_eq_preset(text, &eq, false).map_err(|e| e.0)?;
                self.save_state();
            }
            NameAction::RenameEqPreset(old) => {
                self.state.rename_eq_preset(&old, text).map_err(|e| e.0)?;
                self.save_state();
            }
            NameAction::NewTheme(base) => {
                let all = self.all_themes();
                let name = realhires::themes::check_name(text, &all, "").map_err(|e| e.0)?;
                let mut t = realhires::themes::resolve(&base, self.ctx.global_style().visuals.dark_mode, &all);
                t.name = name.clone();
                self.custom_themes.push(t);
                self.themes_changed();
                self.set_theme(&name);
            }
            NameAction::RenameGroup(kind, name) => self.retag_group(kind, &[name], text, false)?,
            NameAction::MergeGroups(kind, names) => self.retag_group(kind, &names, text, true)?,
            NameAction::RenameTheme(old) => {
                let name = realhires::themes::check_name(text, &self.all_themes(), &old).map_err(|e| e.0)?;
                if let Some(t) = self.custom_themes.iter_mut().find(|t| t.name == old) {
                    t.name = name.clone();
                }
                self.themes_changed();
                if self.scanner.cfg.theme == old {
                    self.set_theme(&name);
                }
            }
        }
        Ok(())
    }

    pub fn confirmed(&mut self, action: ConfirmAction) {
        match action {
            ConfirmAction::DeleteTab(name) => self.delete_tab(&name),
            ConfirmAction::DeletePlaylist(id) => {
                self.state.delete_playlist(&id);
                self.save_state();
                let gone = Page::Playlist(id);
                self.back.retain(|p| *p != gone);
                self.forward.retain(|p| *p != gone);
                if self.page == gone {
                    self.page = Page::Playlists;
                }
            }
            ConfirmAction::DeleteEqPreset(name) => {
                self.state.delete_eq_preset(&name);
                self.save_state();
            }
            ConfirmAction::DeleteTheme(name) => {
                self.custom_themes.retain(|t| t.name != name);
                self.themes_changed();
                if self.scanner.cfg.theme == name {
                    self.set_theme(realhires::themes::SYSTEM);
                }
            }
            ConfirmAction::ClearHistory => self.clear_history(),
            ConfirmAction::ForgetPlaces => self.forget_places(),
            ConfirmAction::RetagGroup { title, kind, paths, name, from, .. } => {
                if self.tag_job.as_ref().is_none_or(|j| j.done()) {
                    // the page of the group follows it to its new name
                    if let Page::Group(k, n) = &self.page {
                        if *k == kind && from.iter().any(|f| casefold(f.trim()) == casefold(n.trim())) {
                            self.page = Page::Group(kind, name.clone());
                        }
                    }
                    self.done_selecting();
                    let cfg = self.scanner.cfg.clone();
                    self.tag_job = Some(crate::editinfo::RenameJob::new(&self.ctx, title, kind, paths, name, cfg));
                }
            }
        }
    }

    pub fn ask_name(&mut self, title: &str, text: &str, action: NameAction) {
        self.ask_name_with(title, "", text, action, vec![]);
    }

    pub fn ask_name_with(&mut self, title: &str, prompt: &str, text: &str, action: NameAction, choices: Vec<String>) {
        self.name_dialog = Some(NameDialog {
            title: title.into(),
            text: text.into(),
            action,
            error: String::new(),
            prompt: prompt.into(),
            choices,
        });
    }

    /// Import an equalizer preset file's text: applied and kept under its name.
    pub fn import_eq_text(&mut self, text: &str, file_name: &str) -> Result<String, String> {
        let (name, st, notes) = realhires::eq::import_eq(text, file_name).map_err(|e| e.0)?;
        let mut candidate = name.clone();
        let mut n = 2;
        while let Err(e) = self.state.check_preset_name(&candidate, "") {
            if candidate.is_empty() || e.0.contains("built-in") {
                candidate = format!("{name} (imported)");
                if self.state.check_preset_name(&candidate, "").is_ok() {
                    break;
                }
            }
            candidate = format!("{name} {n}");
            n += 1;
            if n > 999 {
                return Err(e.0);
            }
        }
        let saved = self.state.save_eq_preset(&candidate, &st, false).map_err(|e| e.0)?;
        self.state.eq = st;
        self.set_eq();
        self.save_state();
        self.eq_notes = notes;
        Ok(saved)
    }

    /// The Settings page edits a draft that is applied after a short pause.
    pub fn settings_draft(&mut self) -> &mut Settings {
        if self.settings_draft.is_none() {
            self.settings_draft = Some(self.scanner.cfg.clone());
        }
        self.settings_draft.as_mut().unwrap()
    }

    /// A Settings page being edited follows the Library page's layout buttons (2.3).
    pub fn set_draft_library_layout(&mut self, layout: &str) {
        if let Some(draft) = self.settings_draft.as_mut() {
            draft.library_layout = layout.to_string();
        }
    }

    pub fn settings_changed(&mut self) {
        self.settings_due = Some(Instant::now() + Duration::from_millis(400));
    }

    fn apply_settings_due(&mut self, ctx: &egui::Context) {
        let Some(at) = self.settings_due else { return };
        if Instant::now() < at || ctx.input(|i| i.pointer.any_down()) {
            ctx.request_repaint_after(Duration::from_millis(100));
            return;
        }
        self.settings_due = None;
        let Some(new) = self.settings_draft.clone() else { return };
        let old = self.scanner.cfg.clone();
        if new == old {
            return;
        }
        self.scanner.apply_settings(new.clone());
        self.places_settings_changed(&old, &new); // 2.4: turned on, off, or another location service
        if new.language != old.language {
            // the next frame is drawn in the new language (no restart needed)
            self.applied_language = new.language.clone();
            i18n::set_language(&new.language);
            ctx.request_repaint();
        }
        if new.gapless != old.gapless {
            self.engine.set_gapless(new.gapless);
        }
        if new.output_device != old.output_device {
            self.engine.set_device(&new.output_device);
        }
        if new.bit_perfect != old.bit_perfect {
            // the song playing restarts where it is, the device opened the new way
            self.engine.set_gain(engine_gain(&self.state, new.bit_perfect));
            self.engine.set_bit_perfect(new.bit_perfect);
        }
        if new.analyse_moods != old.analyse_moods {
            self.moods_setting_changed(new.analyse_moods); // 2.4
        }
        if new.library_folders != old.library_folders || new.extension_set() != old.extension_set() {
            self.lib_stop.store(true, Ordering::Relaxed);
            self.lib_rx = None;
            self.update_library();
        }
    }

    pub fn fill_choices(&mut self) {
        if !self.scanner.choices.contains_key("font_family") {
            let mut fonts = vec![String::new()];
            fonts.extend(look::installed_families());
            self.scanner.choices.insert("font_family", fonts);
        }
        let mut devices = vec![String::new()];
        devices.extend(crate::audio::output_devices());
        self.scanner.choices.insert("output_device", devices);
        self.scanner.choices.insert("theme", realhires::themes::names(&self.all_themes()));
    }

    pub fn add_library_folder(&mut self) {
        if let Some(dir) = rfd::FileDialog::new().pick_folder() {
            let d = dir.to_string_lossy().into_owned();
            let draft = self.settings_draft();
            if !draft.library_folders.contains(&d) {
                draft.library_folders.push(d);
            }
            self.settings_due = Some(Instant::now());
        }
    }

    /// Settings → Library → Check the library for fake files.
    pub fn check_library(&mut self) {
        let folders = self.scanner.cfg.library_folders.clone();
        if folders.is_empty() {
            self.scanner.info =
                Some((tr!("Check the library").into(), tr!("Add your music folder under Settings → Library first.").into()));
            return;
        }
        self.scanner.clear();
        for f in folders {
            self.scanner.add_source(&f);
        }
        self.scanner.start_scan(None, false);
        self.go(Page::Quality);
    }

    pub fn lyrics_for_library(&mut self) {
        let paths: Vec<String> = self.lib.tracks.iter().map(|t| t.path.clone()).collect();
        if paths.is_empty() {
            self.scanner.info = Some((
                tr!("Download synced lyrics").into(),
                tr!("The library is empty; add your music folder under Settings → Library first.").into(),
            ));
            return;
        }
        self.scanner.download_lyrics(paths);
    }

    // -------------------------------------------------------------- pages ---

    /// Open a page (a new page clears the Forward list, as in a browser).
    pub fn go(&mut self, page: Page) {
        if self.page != page {
            self.forward.clear();
            self.enter(page);
        }
    }

    /// Show `page`, remembering the page left for Back (not Now Playing, Search,
    /// the Quality check or Settings).
    fn enter(&mut self, page: Page) {
        let old = std::mem::replace(&mut self.page, page);
        self.page_shown(&old);
        // a search page is remembered while it has a query (2.3: Now Playing's back link
        // returns to the results)
        let remember = match old {
            Page::NowPlaying | Page::Quality | Page::Settings => false,
            Page::Search => !self.search_query.is_empty(),
            _ => true,
        };
        if remember {
            self.back.push(old);
            if self.back.len() > 20 {
                self.back.remove(0);
            }
        }
        if self.page == Page::Settings {
            self.settings_draft = None;
            self.fill_choices();
        }
    }

    pub fn go_back(&mut self) {
        if let Some(p) = self.back.pop() {
            let left = std::mem::replace(&mut self.page, p);
            self.page_shown(&left);
            self.forward.push(left);
        }
    }

    /// Another page is shown: the Genres page starts again without a selection (2.3).
    fn page_shown(&mut self, old: &Page) {
        if self.page == Page::Genres && *old != Page::Genres {
            self.done_selecting();
        }
        // 2.3: tab files copied into the tabs folder appear when these pages are shown (and
        // Settings → Library names the files there that are not tabs)
        if matches!(self.page, Page::Library | Page::Tab(_) | Page::Settings) {
            self.reload_tabs();
        }
    }

    /// Forward (the mouse's forward button, Alt+Right, Cmd+] on macOS): the page
    /// left last with Back (2.1).
    pub fn go_forward(&mut self) {
        if let Some(p) = self.forward.pop() {
            self.enter(p);
        }
    }

    pub fn open_info(&mut self) {
        let Some(n) = &self.now else { return };
        let path = n.path.clone();
        let cfg = &self.scanner.cfg;
        let skey = realhires::store::settings_key(cfg);
        let verdict = self
            .scanner
            .results
            .get(&path)
            .cloned()
            .or_else(|| self.scanner.store.as_ref().and_then(|s| s.lookup(&path, &skey, None)))
            .map(|r| (r.verdict, r.summary.clone()));
        self.info = Some(InfoWindow { path, verdict });
    }

    // ----------------------------------------------------------------- UI ---

    pub fn ui(&mut self, ui: &mut Ui) {
        let ctx = ui.ctx().clone();
        let dropped: Vec<std::path::PathBuf> = ctx.input(|i| {
            i.raw.dropped_files.iter().map(|f| f.path().to_path_buf()).filter(|p| crate::app::is_theme_file(p)).collect()
        });
        for p in dropped {
            // a .json dropped onto the window: a tab file (2.3) or a theme
            if realhires::smarttabs::is_tab_file(&p) {
                self.import_tab_path(&p);
            } else {
                self.import_theme_path(&p);
            }
        }
        self.apply_look(&ctx);
        self.scanner.background(&ctx);
        self.covers.poll(&ctx);
        self.poll_library();
        self.poll_musicbrainz();
        self.poll_moods(); // 2.4
        self.poll_engine();
        self.track_listening();
        self.places_tick(); // 2.4: the place the plays are stored with
        self.apply_settings_due(&ctx);
        if let Some(at) = self.search_due {
            if Instant::now() >= at {
                self.search_due = None;
                self.search_query = self.search.trim().to_string();
                if self.search_query.is_empty() {
                    // the results are gone: Back no longer returns to them (2.3)
                    self.back.retain(|p| *p != Page::Search);
                    self.forward.retain(|p| *p != Page::Search);
                    if self.page == Page::Search {
                        let back = self.search_from.take().unwrap_or(Page::Albums);
                        self.page = back;
                    }
                } else if self.page != Page::Search {
                    self.search_from = Some(self.page.clone());
                    self.page = Page::Search;
                }
            } else {
                ctx.request_repaint_after(Duration::from_millis(50));
            }
        }
        if self.state_dirty && self.state_saved.elapsed() > Duration::from_secs(10) {
            self.save_state();
        }
        self.keys(&ctx);
        let pal = look::current(&ctx);

        egui::Panel::left("player-sidebar")
            .exact_size(224.0)
            .resizable(false)
            .frame(Frame::new().fill(pal.sidebar).inner_margin(Margin::symmetric(12, 14)))
            .show(ui, |ui| self.sidebar(ui, &pal));
        egui::Panel::top("player-bar")
            .exact_size(70.0)
            .resizable(false)
            .frame(Frame::new().fill(pal.bg).inner_margin(Margin::symmetric(14, 8)))
            .show(ui, |ui| self.playback_bar(ui, &pal));
        if self.show_up_next {
            egui::Panel::right("player-up-next")
                .default_size(300.0)
                .resizable(true)
                .frame(Frame::new().fill(pal.sidebar).inner_margin(Margin::same(14)))
                .show(ui, |ui| self.up_next(ui, &pal));
        }
        egui::CentralPanel::default().frame(Frame::new().fill(pal.bg)).show(ui, |ui| {
            if self.page == Page::Quality {
                self.scanner.page(ui);
            } else {
                Frame::new().inner_margin(Margin::symmetric(28, 18)).show(ui, |ui| self.content(ui, &pal));
            }
        });
        self.windows(&ctx, &pal);
        if self.is_playing() {
            ctx.request_repaint_after(Duration::from_millis(if self.page == Page::NowPlaying { 50 } else { 200 }));
        } else if self.message.as_ref().is_some_and(|m| m.1.is_some()) || self.lib_rx.is_some() {
            ctx.request_repaint_after(Duration::from_millis(200));
        }
    }

    fn keys(&mut self, ctx: &egui::Context) {
        // the mouse's back and forward buttons, on every page; Alt+Left / Alt+Right
        // (Cmd+[ / Cmd+] on macOS) when no text field has the keyboard (2.1)
        let typing = ctx.egui_wants_keyboard_input();
        let (back, forward) = ctx.input(|i| {
            let mac = cfg!(target_os = "macos");
            let key = |arrow, bracket| {
                if mac {
                    i.modifiers.mac_cmd && i.key_pressed(bracket)
                } else {
                    i.modifiers.alt && !i.modifiers.ctrl && i.key_pressed(arrow)
                }
            };
            (
                i.pointer.button_pressed(egui::PointerButton::Extra1)
                    || (!typing && key(egui::Key::ArrowLeft, egui::Key::OpenBracket)),
                i.pointer.button_pressed(egui::PointerButton::Extra2)
                    || (!typing && key(egui::Key::ArrowRight, egui::Key::CloseBracket)),
            )
        });
        if back {
            self.go_back();
        } else if forward {
            self.go_forward();
        }
        if self.page == Page::Quality || typing {
            return;
        }
        let (space, right, left, cmd_f, cmd_l) = ctx.input(|i| {
            (
                i.key_pressed(egui::Key::Space),
                i.modifiers.command && i.key_pressed(egui::Key::ArrowRight),
                i.modifiers.command && i.key_pressed(egui::Key::ArrowLeft),
                i.modifiers.command && i.key_pressed(egui::Key::F),
                i.modifiers.command && i.key_pressed(egui::Key::L),
            )
        });
        if cmd_l {
            self.lyrics_button();
        }
        if space {
            self.toggle_play();
        }
        if right && self.now.is_some() {
            self.next();
        }
        if left && self.now.is_some() {
            self.previous();
        }
        if cmd_f {
            ctx.memory_mut(|m| m.request_focus(egui::Id::new("player-search")));
        }
    }

    fn sidebar(&mut self, ui: &mut Ui, pal: &Palette) {
        ui.horizontal(|ui| {
            ui.add_space(4.0);
            ui.label(RichText::new(realhires::APP_NAME).font(look::title(17.0)).color(pal.text));
        });
        ui.add_space(10.0);
        let search = egui::TextEdit::singleline(&mut self.search)
            .id(egui::Id::new("player-search"))
            .hint_text(tr!("Search"))
            .desired_width(f32::INFINITY)
            .margin(Margin::symmetric(8, 5));
        if ui.add(search).changed() {
            self.search_due = Some(Instant::now() + Duration::from_millis(200));
        }
        ui.add_space(10.0);
        let section = |ui: &mut Ui, text: &str| {
            ui.add_space(8.0);
            ui.label(RichText::new(text).font(look::title(11.5)).color(pal.secondary));
            ui.add_space(2.0);
        };
        // room at the bottom for Quality Check, Settings and the library status
        let height = ui.available_height() - 96.0;
        egui::ScrollArea::vertical().id_salt("sidebar").max_height(height.max(100.0)).auto_shrink([false, true]).show(ui, |ui| {
            if sidebar_item(ui, pal, Icon::Library, tr!("Library"), self.page == Page::Library).clicked() {
                self.back.clear();
                self.go(Page::Library);
            }
            section(ui, tr!("Library"));
            let items: [(Icon, &str, Page); 9] = [
                (Icon::Note, n_!("All Songs"), Page::AllSongs),
                (Icon::Folder, n_!("Folders"), Page::Folders(None)),
                (Icon::Albums, n_!("Albums"), Page::Albums),
                (Icon::Person, n_!("Artists"), Page::Artists),
                (Icon::People, n_!("Album Artists"), Page::AlbumArtists),
                (Icon::Genres, n_!("Genres"), Page::Genres),
                (Icon::Calendar, n_!("Years"), Page::Years),
                (Icon::Composer, n_!("Composers"), Page::Composers),
                (Icon::Mood, n_!("Moods"), Page::Moods), // 2.4
            ];
            for (icon, label, page) in items {
                let selected = self.page == page
                    || matches!(
                        (&self.page, &page),
                        (Page::Album(_), Page::Albums) | (Page::Folders(Some(_)), Page::Folders(None)) | (Page::Mood(..), Page::Moods)
                    );
                if sidebar_item(ui, pal, icon, i18n::tr(label), selected).clicked() {
                    self.back.clear();
                    self.go(page);
                }
            }
            section(ui, tr!("Playlists"));
            if sidebar_item(ui, pal, Icon::Playlist, tr!("Playlists"), self.page == Page::Playlists).clicked() {
                self.go(Page::Playlists);
            }
            if sidebar_item(ui, pal, Icon::Heart, tr!("Liked Songs"), self.page == Page::Liked).clicked() {
                self.go(Page::Liked);
            }
            if sidebar_item(ui, pal, Icon::ThumbDown, tr!("Disliked Songs"), self.page == Page::Disliked).clicked() {
                self.go(Page::Disliked);
            }
            let lists: Vec<(String, String)> = self.state.playlists.iter().map(|p| (p.id.clone(), p.name.clone())).collect();
            for (id, name) in lists {
                let page = Page::Playlist(id.clone());
                // 2.3: its colour label after the name, and its menu on a right click
                let resp = sidebar_item_dot(ui, pal, Icon::Note, &name, self.page == page, self.label_of("playlist", &id));
                if resp.clicked() {
                    self.go(page);
                }
                resp.context_menu(|ui| self.playlist_menu(ui, &id));
            }
            if sidebar_item(ui, pal, Icon::Playlist, tr!("New Playlist…"), false).clicked() {
                self.ask_name(tr!("New Playlist"), tr!("New Playlist"), NameAction::NewPlaylist(vec![]));
            }
            // 2.3: the custom tabs, with their menu on a right click
            section(ui, tr!("Tabs"));
            self.tab_hits.sidebar.clear();
            for name in self.tab_names() {
                let page = Page::Tab(name.clone());
                let r = sidebar_item_dot(ui, pal, Icon::Filter, &name, self.page == page, self.label_of("tab", &name));
                self.tab_hits.sidebar.push((name.clone(), r.rect));
                if r.clicked() {
                    self.go(page);
                }
                r.context_menu(|ui| self.tab_menu(ui, &name));
            }
            let r = sidebar_item(ui, pal, Icon::Filter, tr!("New Tab…"), false);
            self.tab_hits.new_tab = Some(r.rect);
            if r.clicked() {
                self.new_tab();
            }
            section(ui, tr!("History"));
            let items: [(Icon, &str, Page); 3] = [
                (Icon::Clock, n_!("Recently Played"), Page::RecentlyPlayed),
                (Icon::Albums, n_!("Recently Added"), Page::Recent),
                (Icon::Chart, n_!("Your Stats"), Page::Stats),
            ];
            for (icon, label, page) in items {
                let selected = self.page == page || (page == Page::Stats && self.page == Page::StatsDetails);
                if sidebar_item(ui, pal, icon, i18n::tr(label), selected).clicked() {
                    self.go(page);
                }
            }
        });
        ui.with_layout(Layout::bottom_up(Align::Min), |ui| {
            if let Some(s) = self.status_line() {
                ui.label(RichText::new(s).small().color(pal.secondary));
                ui.add_space(4.0);
            }
            if sidebar_item(ui, pal, Icon::Gear, tr!("Settings"), self.page == Page::Settings).clicked() {
                self.go(Page::Settings);
            }
            // always there (2.1: the menu bars are gone), just above Settings
            if sidebar_item(ui, pal, Icon::Check, tr!("Quality Check"), self.page == Page::Quality).clicked() {
                self.go(Page::Quality);
            }
        });
    }

    fn playback_bar(&mut self, ui: &mut Ui, pal: &Palette) {
        let has = self.now.is_some();
        let full = ui.available_rect_before_wrap();
        ui.allocate_ui_with_layout(full.size(), Layout::left_to_right(Align::Center), |ui| {
            ui.spacing_mut().item_spacing.x = 6.0;
            // left: transport
            let shuffle_on = self.state.shuffle != "off";
            let tip = tr!("Shuffle: {mode}", mode = i18n::tr(shuffle_label(&self.state.shuffle)));
            let r = icons::button(ui, Icon::Shuffle, 30.0, shuffle_on, true, &tip);
            egui::Popup::menu(&r).show(|ui| {
                ui.label(RichText::new(tr!("Shuffle")).strong());
                for m in SHUFFLE_MODES {
                    if ui.radio(self.state.shuffle == m, i18n::tr(shuffle_label(m))).clicked() {
                        self.set_shuffle(m);
                        ui.close();
                    }
                }
            });
            if icons::button(ui, Icon::Previous, 34.0, false, has, tr!("Previous")).clicked() {
                self.previous();
            }
            let play_icon = if self.is_playing() { Icon::Pause } else { Icon::Play };
            if icons::button(ui, play_icon, 42.0, false, true, if self.is_playing() { tr!("Pause") } else { tr!("Play") }).clicked() {
                self.toggle_play();
            }
            if icons::button(ui, Icon::Next, 34.0, false, has, tr!("Next")).clicked() {
                self.next();
            }
            let rep = self.state.repeat.as_str();
            let rep_icon = match rep {
                "song" => Icon::RepeatOne,
                "stop" => Icon::RepeatStop,
                _ => Icon::Repeat,
            };
            let tip = tr!("Repeat: {mode}", mode = i18n::tr(repeat_label(rep)));
            let r = icons::button(ui, rep_icon, 30.0, rep != "off", true, &tip);
            egui::Popup::menu(&r).show(|ui| {
                ui.label(RichText::new(tr!("Repeat")).strong());
                for m in REPEAT_MODES {
                    if ui.radio(self.state.repeat == m, i18n::tr(repeat_label(m))).clicked() {
                        self.set_repeat(m);
                        ui.close();
                    }
                }
            });
            // right part is laid out from the right edge
            let right_w = 360.0;
            let lcd_w = (ui.available_width() - right_w - 16.0).clamp(280.0, 720.0);
            let gap = ((ui.available_width() - right_w - lcd_w) / 2.0).max(8.0);
            ui.add_space(gap);
            self.lcd(ui, pal, lcd_w);
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.spacing_mut().item_spacing.x = 4.0;
                // bit-perfect playback: the gain is exactly 1, so the slider shows full volume and cannot move
                let bit_perfect = self.scanner.cfg.bit_perfect;
                let mut v = if bit_perfect { 1.0 } else { self.state.volume as f32 };
                let slider = ui
                    .add_enabled_ui(!bit_perfect, |ui| {
                        ui.add_sized([96.0, 20.0], egui::Slider::new(&mut v, 0.0..=1.0).show_value(false))
                    })
                    .inner;
                let slider = if bit_perfect {
                    slider.on_disabled_hover_text(tr!("Bit-perfect playback is on: set the volume on your amplifier or DAC"))
                } else {
                    slider
                };
                if slider.changed() {
                    self.set_volume(v as f64);
                }
                let vol_icon = if self.state.muted || self.state.volume <= 0.0 { Icon::SpeakerMuted } else { Icon::Speaker };
                if icons::button(ui, vol_icon, 28.0, false, true, if self.state.muted { tr!("Unmute") } else { tr!("Mute") }).clicked() {
                    self.toggle_mute();
                }
                ui.add_space(6.0);
                if icons::button(ui, Icon::Equalizer, 28.0, self.state.eq.enabled, true, tr!("Equalizer")).clicked() {
                    self.eq_open = !self.eq_open;
                }
                if icons::button(ui, Icon::UpNext, 28.0, self.show_up_next, true, tr!("Up Next")).clicked() {
                    self.show_up_next = !self.show_up_next;
                }
                let lyrics_on = self.page == Page::NowPlaying && !self.lyrics_hidden && self.np_tab == NpTab::Lyrics;
                if icons::button(ui, Icon::Lyrics, 28.0, lyrics_on, true, tr!("Lyrics")).clicked() {
                    self.lyrics_button();
                }
                let now_path = self.now.as_ref().map(|n| n.path.clone());
                let r = icons::button(ui, Icon::More, 28.0, false, has, tr!("More"));
                if let Some(p) = now_path.clone() {
                    egui::Popup::menu(&r).show(|ui| self.song_menu(ui, &p));
                }
                let disliked = now_path.as_ref().is_some_and(|p| self.state.is_disliked(p));
                if icons::button(ui, Icon::ThumbDown, 28.0, disliked, has, if disliked { tr!("Remove dislike") } else { tr!("Dislike") }).clicked() {
                    if let Some(p) = now_path.clone() {
                        self.toggle_dislike(&p);
                    }
                }
                let liked = now_path.as_ref().is_some_and(|p| self.state.is_liked(p));
                let heart = if liked { Icon::HeartFilled } else { Icon::Heart };
                if icons::button(ui, heart, 28.0, liked, has, if liked { tr!("Unlike") } else { tr!("Like") }).clicked() {
                    if let Some(p) = now_path {
                        self.toggle_like(&p);
                    }
                }
            });
        });
    }

    fn go_back_from_now(&mut self) {
        let back = self.back.pop().unwrap_or(Page::Albums);
        self.page = back;
    }

    /// The Lyrics button and Ctrl/Cmd+L: elsewhere they open Now Playing with the lyrics; on
    /// Now Playing they hide or show the lyrics (hidden, the cover is in the middle) and the
    /// page stays (2.1, as the user asked).
    pub fn lyrics_button(&mut self) {
        if self.page == Page::NowPlaying {
            if self.np_tab != NpTab::Lyrics {
                self.np_tab = NpTab::Lyrics;
                self.lyrics_hidden = false;
            } else {
                self.lyrics_hidden = !self.lyrics_hidden;
            }
        } else {
            self.lyrics_hidden = false;
            self.np_tab = NpTab::Lyrics;
            self.go(Page::NowPlaying);
        }
    }

    /// Now Playing's switch: Lyrics, Up Next or Details (kept for the session).
    pub fn set_np_tab(&mut self, tab: NpTab) {
        self.np_tab = tab;
        self.lyrics_hidden = false;
    }

    /// How Now Playing is laid out now (2.3).
    pub fn np_mode(&mut self) -> NpMode {
        if self.now.is_none() {
            return NpMode::Nothing;
        }
        if self.lyrics_hidden {
            return NpMode::Centred;
        }
        if self.np_tab == NpTab::Lyrics {
            match self.lyrics_of_now() {
                Some(ShownLyrics::Instrumental) => return NpMode::Instrumental,
                Some(_) if self.lyrics_wide => return NpMode::Wide,
                _ => {}
            }
        }
        NpMode::Halves
    }

    /// Now Playing's Lyrics only (2.3): the cover goes and the lyrics fill the page, one step
    /// larger; pressed again (or Esc) the cover comes back.  Kept for the session.
    pub fn toggle_lyrics_wide(&mut self) {
        self.lyrics_wide = !self.lyrics_wide;
    }

    /// Now Playing's left half (2.3): the cover (false) or the album's songs (true), from a
    /// swipe or the dots; kept for the session.
    pub fn set_np_side(&mut self, songs: bool) {
        self.np_songs = songs;
    }

    /// A page's name as Now Playing's back link shows it (2.3): an album's, playlist's,
    /// folder's, artist's (genre's…) own name, else the page's title.
    pub fn page_label(&self, page: &Page) -> String {
        match page {
            Page::Album(key) => {
                if let Some(&i) = self.lib.album_index.get(key) {
                    return tr_name(&self.lib.albums[i].title).to_string();
                }
            }
            Page::Playlist(id) => {
                if let Some(p) = self.state.playlist(id) {
                    return p.name.clone();
                }
            }
            Page::Group(_, name) => return tr_name(name).to_string(),
            Page::Tab(name) => return name.clone(),
            Page::Place(key) => {
                let name = self.place_name(key);
                if !name.is_empty() {
                    return name;
                }
            }
            Page::Mood(_, label) => return i18n::tr(label).to_string(), // 2.4
            Page::Folders(folder) => {
                // with one library folder, Folders shows that folder (as Python's page does)
                let roots: Vec<String> = self.scanner.cfg.library_folders.iter().map(|f| realhires::paths::abspath(f)).collect();
                let shown = folder.clone().or_else(|| (roots.len() == 1).then(|| roots[0].clone()));
                if let Some(f) = shown {
                    let name = realhires::paths::file_name(&f);
                    return if name.is_empty() { f } else { name };
                }
            }
            _ => {}
        }
        page.shown_title().to_string()
    }

    /// The name of the page Back returns to, when there is one (2.3).
    pub fn back_label(&self) -> Option<String> {
        self.back.last().map(|p| self.page_label(p))
    }

    /// The album page of a song's album.
    pub fn open_album_of(&mut self, path: &str) {
        if let Some(key) = self.lib.album_of(path).map(|a| a.key.clone()) {
            self.go(Page::Album(key));
        }
    }

    /// Up Next (Now Playing): play that song now; the queue continues after it.
    pub fn jump_up_next(&mut self, i: usize) {
        // PlayQueue::jump() also handles the songs queued with Play Next / Add to Queue
        if let Some(p) = self.queue.as_mut().and_then(|q| q.jump(i)) {
            self.play_path(&p);
        }
    }

    /// A click on the LCD's cover opens Now Playing, or leaves it when it is shown.
    pub fn cover_clicked(&mut self) {
        if self.page == Page::NowPlaying {
            self.go_back_from_now();
        } else {
            self.go(Page::NowPlaying);
        }
    }

    /// The panel in the middle of the playback bar.
    fn lcd(&mut self, ui: &mut Ui, pal: &Palette, width: f32) {
        let (rect, _) = ui.allocate_exact_size(vec2(width, 54.0), Sense::hover());
        let p = ui.painter_at(rect.expand(2.0));
        p.rect(rect, CornerRadius::same(8), pal.lcd, Stroke::new(1.0, pal.separator), egui::StrokeKind::Inside);
        let Some((now_path, now_active)) = self.now.as_ref().map(|n| (n.path.clone(), n.active)) else {
            p.text(rect.center(), Align2::CENTER_CENTER, realhires::APP_NAME, look::title(15.0), pal.secondary);
            return;
        };
        let path = now_path;
        let track = self.lib.track(&path).cloned().unwrap_or_else(|| Track { path: path.clone(), ..Default::default() });
        // cover
        let cover_rect = Rect::from_min_size(rect.min + vec2(5.0, 5.0), vec2(44.0, 44.0));
        let key = track.song().album_key();
        let paths: Vec<String> = self.lib.album_of(&path).map(|a| a.tracks.iter().map(|t| t.path.clone()).collect()).unwrap_or_else(|| vec![path.clone()]);
        let tex = self.covers.get(&key, &paths);
        cover(&p, cover_rect, tex.as_ref(), &track.display_album(), 5);
        let cover_resp = ui.interact(cover_rect, ui.id().with("lcd-cover"), Sense::click());
        if cover_resp.on_hover_text(tr!("Now Playing")).clicked() {
            self.cover_clicked();
        }
        // texts
        let badge = quality_label(&track);
        let badge_font = look::title(10.0);
        let badge_w = ui.ctx().fonts_mut(|f| f.layout_no_wrap(badge.clone(), badge_font.clone(), pal.text).size().x) + 12.0;
        let text_left = cover_rect.right() + 10.0;
        let text_right = rect.right() - badge_w - 14.0;
        let mid = (text_left + text_right) / 2.0;
        let clip = Rect::from_x_y_ranges(text_left..=text_right, rect.y_range());
        let tp = ui.painter_at(clip);
        // the title and the album name open the album, each artist name that artist (2.1)
        let has_album = self.lib.album_of(&path).is_some();
        let mut open = None;
        let title = crate::pages::elided(ui, &track.display_title(), look::title(13.0), pal.text, clip.width());
        let trect = Rect::from_center_size(pos2(mid, rect.top() + 13.0), title.size());
        tp.galley(trect.min, title, pal.text);
        self.lcd_links.clear();
        if has_album {
            let r = ui.interact(trect, ui.scope_id().with("lcd-title"), Sense::click());
            if r.hovered() {
                tp.line_segment([pos2(trect.left(), trect.bottom() - 1.0), pos2(trect.right(), trect.bottom() - 1.0)], Stroke::new(1.0, pal.text));
                ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
            }
            if r.clicked() {
                open = Some(crate::pages::Link::Album);
            }
            self.lcd_links.push((trect, crate::pages::Link::Album));
        }
        match &self.message {
            Some((m, _)) => {
                tp.text(pos2(mid, rect.top() + 29.0), Align2::CENTER_CENTER, m.clone(), look::text(11.5), pal.accent);
            }
            None => {
                let artists = self.lib.artists_of(&track);
                let pieces = crate::pages::artist_line(ui, look::text(11.5), pal.secondary, &artists, &track.display_album(), has_album, clip.width());
                let (rects, clicked) = crate::pages::paint_links(ui, &tp, pieces, pos2(mid, rect.top() + 29.0), pal.secondary, ui.scope_id().with("lcd-links"));
                self.lcd_links.extend(rects);
                open = open.or(clicked);
            }
        }
        // seek bar
        let dur = track.duration.max(0.0);
        let pos = if now_active { self.engine.position().unwrap_or(0.0) } else { 0.0 };
        let bar = Rect::from_x_y_ranges(text_left + 34.0..=text_right - 38.0, rect.top() + 41.0..=rect.top() + 45.0);
        let resp = ui.interact(bar.expand2(vec2(0.0, 5.0)), ui.id().with("seek"), Sense::click_and_drag());
        let frac = if dur > 0.0 { (pos / dur).clamp(0.0, 1.0) as f32 } else { 0.0 };
        let shown = if resp.dragged() || resp.clicked() {
            resp.interact_pointer_pos().map(|q| ((q.x - bar.left()) / bar.width()).clamp(0.0, 1.0)).unwrap_or(frac)
        } else {
            frac
        };
        p.rect_filled(bar, CornerRadius::same(2), pal.separator);
        p.rect_filled(Rect::from_min_max(bar.min, pos2(bar.left() + bar.width() * shown, bar.bottom())), CornerRadius::same(2), pal.secondary);
        if resp.hovered() || resp.dragged() {
            p.circle_filled(pos2(bar.left() + bar.width() * shown, bar.center().y), 5.0, pal.text);
        }
        if (resp.drag_stopped() || resp.clicked()) && dur > 0.0 {
            self.seek(shown as f64 * dur);
        }
        let t_font = look::text(10.0);
        p.text(pos2(bar.left() - 6.0, bar.center().y), Align2::RIGHT_CENTER, fmt_time(shown as f64 * dur), t_font.clone(), pal.secondary);
        p.text(pos2(bar.right() + 6.0, bar.center().y), Align2::LEFT_CENTER, format!("-{}", fmt_time((dur - shown as f64 * dur).max(0.0))), t_font, pal.secondary);
        // quality badge: a click opens the Playback info
        let brect = Rect::from_center_size(pos2(rect.right() - badge_w / 2.0 - 8.0, rect.center().y), vec2(badge_w, 18.0));
        let bresp = ui.interact(brect, ui.id().with("quality-badge"), Sense::click());
        // 2.3: in the quality's colour (gold for Hi-Res Lossless, the accent for Lossless...)
        let qcol = pal.quality_of(&track);
        let bcol = if bresp.hovered() { look::mix(qcol, pal.text, 0.35) } else { qcol };
        p.rect_stroke(brect, CornerRadius::same(4), Stroke::new(1.0, bcol), egui::StrokeKind::Inside);
        p.text(brect.center(), Align2::CENTER_CENTER, badge, badge_font, bcol);
        if bresp.on_hover_text(tr!("Playback info")).clicked() {
            self.open_info();
        }
        match open {
            Some(crate::pages::Link::Artist(name)) => self.open_artist(&name),
            Some(crate::pages::Link::Album) => self.open_album_of(&path),
            None => {}
        }
    }

    fn up_next(&mut self, ui: &mut Ui, pal: &Palette) {
        ui.label(RichText::new(tr!("Up Next")).font(look::title(18.0)).color(pal.text));
        if !self.queue_name.is_empty() {
            let from = tr!("Playing from {name}", name = shown_queue_name(&self.queue_name));
            ui.label(RichText::new(from).color(pal.secondary));
        }
        ui.add_space(8.0);
        let Some(q) = &self.queue else {
            ui.label(RichText::new(tr!("Nothing is queued.")).color(pal.secondary));
            return;
        };
        let upcoming = q.up_next(50);
        if upcoming.is_empty() {
            ui.label(RichText::new(tr!("Nothing comes after this song.")).color(pal.secondary));
        }
        // 2.3: a song can be dragged to another place or removed with a right click
        let rows: Vec<(String, String, Option<Color32>)> = upcoming
            .iter()
            .map(|path| {
                let t = self.lib.track(path);
                let title = t.map(|t| t.display_title()).unwrap_or_else(|| path.clone());
                (title, t.map(|t| tr_name(&t.display_artist()).to_string()).unwrap_or_default(), self.label_of("song", path))
            })
            .collect();
        let action = egui::ScrollArea::vertical()
            .auto_shrink([false, false])
            .show(ui, |ui| {
                crate::upnext::rows(ui, pal, "panel", rows.len(), 38.0, 0.0, |p, rect, i| {
                    let (title, artist, label) = &rows[i];
                    let dx = if let Some(c) = label {
                        crate::labels::paint_dot(p, rect.left_top() + vec2(8.0 + crate::labels::DOT / 2.0, 12.0), *c, None);
                        crate::labels::DOT + 6.0
                    } else {
                        0.0
                    };
                    p.text(rect.left_top() + vec2(8.0 + dx, 4.0), Align2::LEFT_TOP, title, look::text(13.0), pal.text);
                    p.text(rect.left_top() + vec2(8.0, 21.0), Align2::LEFT_TOP, artist, look::text(11.0), pal.secondary);
                })
            })
            .inner;
        self.up_next_action(action);
    }

    /// What the user did with a row of Up Next (the panel's or Now Playing's).
    pub(crate) fn up_next_action(&mut self, action: Option<crate::upnext::UpNextAction>) {
        use crate::upnext::UpNextAction;
        match action {
            Some(UpNextAction::Jump(i)) => self.jump_up_next(i),
            Some(UpNextAction::Move(src, dst)) => self.move_in_up_next(src, dst),
            Some(UpNextAction::Remove(i)) => self.remove_from_up_next(i),
            None => {}
        }
    }

    /// Up Next (2.3): its song number `src` was dragged to place `dst`; the song that
    /// follows gaplessly is set again.
    pub fn move_in_up_next(&mut self, src: usize, dst: usize) {
        if self.queue.as_mut().is_some_and(|q| q.move_upcoming(src, dst)) {
            self.update_next();
        }
    }

    /// Up Next (2.3): take its song number `index` out of the queue (right click → Remove
    /// from Up Next); the song that follows gaplessly is set again.
    pub fn remove_from_up_next(&mut self, index: usize) {
        if self.queue.as_mut().and_then(|q| q.remove_upcoming(index)).is_some() {
            self.update_next();
        }
    }

    fn windows(&mut self, ctx: &egui::Context, pal: &Palette) {
        let had_dialog = self.name_dialog.is_some() || self.confirm.is_some() || self.tag_job.is_some();
        self.dialogs(ctx, pal);
        let picker_open = self.label_picker.is_some();
        self.label_picker_window(ctx, pal);
        if picker_open {
            return; // Esc belongs to the Colour Label window while it is open (2.3)
        }
        let cover_open = crate::coverwin::cover_window(self, ctx, pal);
        if crate::tabs::tab_window(self, ctx, pal) {
            return; // Esc and Enter belong to the tab window while it is open (2.3)
        }
        if let Some(mut w) = self.edit_info.take() {
            let cfg = self.scanner.cfg.clone();
            // Esc and Enter belong to the MusicBrainz window when Acquire Metadata… opened it over this one
            let keys = self.mb_win.is_none();
            let outcome = w.show(ctx, pal, &cfg, keys);
            // 2.3: Acquire Metadata… at the bottom left: the MusicBrainz window for the same songs
            let acquire = std::mem::take(&mut w.acquire).then(|| (w.paths.clone(), w.album, w.heading.clone(), w.subheading.clone()));
            match outcome {
                crate::editinfo::Outcome::Open => self.edit_info = Some(w),
                crate::editinfo::Outcome::Closed => {}
                crate::editinfo::Outcome::Saved { close } => {
                    self.update_library();
                    if !close {
                        self.edit_info = Some(w);
                    }
                }
            }
            if let Some((paths, album, heading, sub)) = acquire {
                self.open_musicbrainz(paths, album, heading, sub);
            }
            if self.edit_info.is_some() && self.mb_win.is_none() {
                return; // Esc belongs to the Edit Info window while it is open
            }
        }
        if let Some(mut job) = self.tag_job.take() {
            let (open, finished) = job.show(ctx, pal);
            if finished {
                self.update_library();
                // an open Properties window shows the new genre, composer or year (as in Python)
                if let Some(e) = self.edit_info.as_mut() {
                    e.files_changed(&job.paths);
                }
            }
            if open {
                self.tag_job = Some(job);
            }
        }
        if let Some(mut w) = self.mb_win.take() {
            match w.show(ctx, pal) {
                crate::mbwin::Outcome::Open => self.mb_win = Some(w),
                crate::mbwin::Outcome::Closed => {}
                crate::mbwin::Outcome::Saved { close } => {
                    self.update_library();
                    // an open Properties window shows what was written (2.3: Acquire Metadata… there)
                    if let Some(e) = self.edit_info.as_mut() {
                        e.files_changed(&w.paths);
                    }
                    if !close {
                        self.mb_win = Some(w);
                    }
                }
            }
            if self.mb_win.is_some() {
                return; // Esc belongs to the MusicBrainz window while it is open
            }
        }
        // Esc closes the topmost of the player's windows
        if ctx.input(|i| i.key_pressed(egui::Key::Escape)) && !ctx.egui_wants_keyboard_input() && !cover_open {
            if self.eq_open {
                self.eq_open = false;
                self.save_state();
            } else if self.info.is_some() {
                self.info = None;
            } else if self.page == Page::Genres
                && (self.group_select_mode || !self.group_selected.is_empty())
                && self.name_dialog.is_none()
                && self.confirm.is_none()
            {
                self.done_selecting(); // as Done (2.3)
            } else if !had_dialog && self.page == Page::NowPlaying && self.lyrics_wide && self.np_mode() == NpMode::Wide {
                self.lyrics_wide = false; // Lyrics only: Esc brings the cover back (2.3)
            }
        }
        if self.info.is_some() {
            crate::eqwin::info_window(self, ctx, pal);
        }
        if self.eq_open {
            crate::eqwin::eq_window(self, ctx, pal);
        }
    }

    /// The name and confirmation dialogs.
    fn dialogs(&mut self, ctx: &egui::Context, pal: &Palette) {
        if let Some(mut d) = self.name_dialog.take() {
            let mut open = true;
            let mut done: Option<bool> = None;
            egui::Window::new(d.title.clone())
                .id(egui::Id::new("player-name-dialog"))
                .open(&mut open)
                .collapsible(false)
                .resizable(false)
                .pivot(Align2::CENTER_CENTER)
                .default_pos(ctx.content_rect().center())
                .show(ctx, |ui| {
                    ui.set_max_width(420.0);
                    ui.label(if d.prompt.is_empty() { tr!("Name") } else { d.prompt.as_str() });
                    let r = ui.add(egui::TextEdit::singleline(&mut d.text).desired_width(320.0));
                    r.request_focus();
                    if !d.choices.is_empty() {
                        egui::ScrollArea::vertical().max_height(160.0).id_salt("name-choices").show(ui, |ui| {
                            for c in d.choices.clone() {
                                if ui.selectable_label(d.text == c, &c).clicked() {
                                    d.text = c;
                                }
                            }
                        });
                    }
                    if !d.error.is_empty() {
                        // the core's messages (a taken name...) are English: translated here
                        ui.label(RichText::new(tr_text(&d.error)).color(pal.accent));
                    }
                    ui.add_space(6.0);
                    ui.horizontal(|ui| {
                        if ui.button(RichText::new(tr!("OK")).strong()).clicked() || ui.input(|i| i.key_pressed(egui::Key::Enter)) {
                            done = Some(true);
                        }
                        if ui.button(tr!("Cancel")).clicked() || ui.input(|i| i.key_pressed(egui::Key::Escape)) {
                            done = Some(false);
                        }
                    });
                });
            match (open, done) {
                (false, _) | (_, Some(false)) => {}
                (_, Some(true)) => {
                    if let Err(e) = self.apply_name(d.action.clone(), &d.text) {
                        d.error = e;
                        self.name_dialog = Some(d);
                    }
                }
                _ => self.name_dialog = Some(d),
            }
        }
        if let Some(action) = self.confirm.clone() {
            let (title, text, button) = match &action {
                ConfirmAction::DeletePlaylist(id) => (
                    tr!("Delete playlist"),
                    tr!(
                        "Delete the playlist “{name}”? Its songs stay in your library.",
                        name = self.state.playlist(id).map(|p| p.name.as_str()).unwrap_or("")
                    ),
                    tr!("Delete"),
                ),
                ConfirmAction::DeleteTab(n) => (
                    tr!("Delete Tab"),
                    tr!("Delete the tab “{name}”? Its file is deleted from the tabs folder; the songs are not.", name = n),
                    tr!("Delete"),
                ),
                ConfirmAction::DeleteEqPreset(n) => (
                    tr!("Delete preset"),
                    tr!("Delete your preset “{name}”? The equalizer keeps its current settings.", name = n),
                    tr!("Delete"),
                ),
                ConfirmAction::DeleteTheme(n) => {
                    (tr!("Delete theme"), tr!("Delete the theme “{name}”?", name = i18n::tr(n)), tr!("Delete"))
                }
                // the title and text are made (translated) by `retag_group()`
                ConfirmAction::RetagGroup { title, text, .. } => (title.as_str(), text.clone(), tr!("Change")),
                ConfirmAction::ForgetPlaces => (
                    tr!("Forget play locations"),
                    tr!("Forget where your songs were played? The plays stay in the play history and in Your Stats, only \
                         their places are removed.")
                    .to_string(),
                    tr!("Forget"),
                ),
                ConfirmAction::ClearHistory => (
                    tr!("Clear the play history"),
                    tr!("Forget every song played so far? Recently Played and Your Stats start again from nothing.").to_string(),
                    tr!("Clear"),
                ),
            };
            let mut open = true;
            let mut done: Option<bool> = None;
            egui::Window::new(title)
                .id(egui::Id::new("player-confirm"))
                .open(&mut open)
                .collapsible(false)
                .resizable(false)
                .pivot(Align2::CENTER_CENTER)
                .default_pos(ctx.content_rect().center())
                .show(ctx, |ui| {
                    ui.set_max_width(380.0);
                    ui.label(text);
                    ui.add_space(8.0);
                    ui.horizontal(|ui| {
                        if ui.button(RichText::new(button).strong().color(pal.accent)).clicked() {
                            done = Some(true);
                        }
                        if ui.button(tr!("Cancel")).clicked() || ui.input(|i| i.key_pressed(egui::Key::Escape)) {
                            done = Some(false);
                        }
                    });
                });
            if !open || done.is_some() {
                self.confirm = None;
            }
            if done == Some(true) {
                self.confirmed(action);
            }
        }
    }

    pub fn on_exit(&mut self) {
        self.flush_listening();
        self.save_state();
        self.lib_stop.store(true, Ordering::Relaxed);
        self.mb_stop.store(true, Ordering::Relaxed);
        crate::places::stop(&self.places);
        self.stop_moods();
        self.engine.stop();
    }
}

pub fn fmt_time(sec: f64) -> String {
    crate::style::fmt_duration(Some(sec.floor()))
}

fn sidebar_item(ui: &mut Ui, pal: &Palette, icon: Icon, label: &str, selected: bool) -> egui::Response {
    sidebar_item_dot(ui, pal, icon, label, selected, None)
}

/// A sidebar entry with its colour label's dot after the name (2.3: playlists).
pub(crate) fn sidebar_item_dot(ui: &mut Ui, pal: &Palette, icon: Icon, label: &str, selected: bool, dot: Option<Color32>) -> egui::Response {
    let (rect, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 28.0), Sense::click());
    if selected {
        ui.painter().rect_filled(rect, CornerRadius::same(6), pal.selection);
    } else if resp.hovered() {
        ui.painter().rect_filled(rect, CornerRadius::same(6), pal.row_hover);
    }
    icons::paint(ui.painter(), Rect::from_center_size(pos2(rect.left() + 16.0, rect.center().y), vec2(18.0, 18.0)), icon, pal.accent);
    let room = rect.width() - 40.0 - if dot.is_some() { crate::labels::DOT + 8.0 } else { 0.0 };
    let g = crate::pages::elided(ui, label, look::text(13.0), pal.text, room.max(10.0));
    let w = g.size().x;
    ui.painter().galley(pos2(rect.left() + 32.0, rect.center().y - g.size().y / 2.0), g, pal.text);
    if let Some(c) = dot {
        crate::labels::paint_after_text(ui.painter(), rect.left() + 32.0, w, rect.center().y, c);
    }
    resp
}

/// The name of the group of songs without a genre, composer or year.
pub fn unknown_name(kind: &str) -> &'static str {
    match kind {
        "genres" => "Unknown Genre",
        "composers" => "Unknown Composer",
        "years" => "Unknown Year",
        _ => "",
    }
}

/// A song's genre, composer or year, as a rename compares it with the new name.
pub fn group_value(kind: &str, t: &Track) -> String {
    match kind {
        "years" => t.year.clone(),
        "composers" => realhires::tagedit::clean_value(&t.composer),
        _ => realhires::tagedit::clean_value(&t.genre),
    }
}

/// Songs sorted by a column (None: as they are).
pub fn sort_tracks(mut v: Vec<&Track>, sort: Option<(SongCol, bool)>) -> Vec<&Track> {
    let Some((col, rev)) = sort else { return v };
    v.sort_by_cached_key(|t| sort_key(t, col));
    if rev {
        v.reverse();
    }
    v
}

/// A cover, or a coloured square with a note when the album has none.
/// The comment tag of a song ("" when it has none, or the file cannot be read); ffprobe,
/// so call it off the window's thread.
pub fn read_comment(path: &str) -> String {
    let out = realhires::probe::command(realhires::probe::tool("ffprobe"))
        .args(["-v", "error", "-show_entries", "format_tags:stream_tags", "-of", "json", path])
        .stdin(std::process::Stdio::null())
        .output();
    let Ok(out) = out else { return String::new() };
    let Ok(data) = realhires::json::parse(&String::from_utf8_lossy(&out.stdout)) else { return String::new() };
    let mut sources: Vec<&realhires::json::Value> = data.get("format").into_iter().collect();
    if let Some(realhires::json::Value::Array(streams)) = data.get("streams") {
        sources.extend(streams.iter());
    }
    for src in sources {
        if let Some(realhires::json::Value::Object(tags)) = src.get("tags") {
            for (k, v) in tags {
                if casefold(k.trim()) == "comment" {
                    if let Some(v) = v.as_str().map(str::trim).filter(|v| !v.is_empty()) {
                        return v.to_string();
                    }
                }
            }
        }
    }
    String::new()
}

pub fn cover(p: &egui::Painter, rect: Rect, tex: Option<&egui::TextureHandle>, title: &str, radius: u8) {
    match tex {
        Some(t) => {
            let shape = egui::epaint::RectShape::filled(rect, CornerRadius::same(radius), Color32::WHITE)
                .with_texture(t.id(), Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)));
            p.add(shape);
        }
        None => {
            // 2.3: a missing cover in the theme's "Missing cover" colours, as the Python app
            let pal = look::current(p.ctx());
            let _ = title;
            p.rect_filled(rect, CornerRadius::same(radius), pal.placeholder);
            icons::paint(p, Rect::from_center_size(rect.center(), rect.size() * 0.36), Icon::Note, pal.placeholder_icon);
        }
    }
}

#[cfg(test)]
mod tests {
    //! Drives the whole player headlessly with the null audio output: the
    //! library index, playback, a gapless song change with the queue, lyrics,
    //! likes, shuffle and repeat, the EQ and info windows, search, Settings,
    //! the Quality check page, and the state saved for the next start.
    use super::*;
    use std::process::Command;

    fn ffmpeg(args: &[&str]) {
        let st = Command::new(realhires::probe::tool("ffmpeg")).args(["-v", "error", "-y"]).args(args).status().unwrap();
        assert!(st.success(), "{args:?}");
    }

    fn song(dir: &std::path::Path, album: &str, title: &str, artist: &str, genre: &str, n: u32, rate: u32, secs: f64) -> String {
        let d = dir.join(album);
        std::fs::create_dir_all(&d).unwrap();
        let f = d.join(format!("{n:02} {title}.flac"));
        let src = format!("sine=frequency={}:sample_rate={rate}:duration={secs}", 300 + 40 * n);
        let meta = [format!("title={title}"), format!("artist={artist}"), format!("album={album}"), format!("track={n}"), format!("genre={genre}")];
        let mut args: Vec<&str> = vec!["-f", "lavfi", "-i", &src];
        for m in &meta {
            args.extend(["-metadata", m.as_str()]);
        }
        let out = f.to_string_lossy().into_owned();
        args.push(&out);
        ffmpeg(&args);
        realhires::paths::abspath(&out)
    }

    fn frame(ctx: &egui::Context, p: &mut Player) {
        let input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1280.0, 840.0))),
            ..Default::default()
        };
        ctx.run_ui(input, |ui| p.ui(ui)).drop_without_applying_deltas();
    }

    /// A frame with these input events; a key's modifiers are held during the frame
    /// and let go in the next one.
    fn frame_with(ctx: &egui::Context, p: &mut Player, events: Vec<egui::Event>) {
        let held = events.iter().find_map(|e| if let egui::Event::Key { modifiers, .. } = e { Some(*modifiers) } else { None });
        let run = |p: &mut Player, events: Vec<egui::Event>| {
            let input = egui::RawInput {
                screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1280.0, 840.0))),
                events,
                ..Default::default()
            };
            ctx.run_ui(input, |ui| p.ui(ui)).drop_without_applying_deltas();
        };
        match held {
            Some(m) => {
                run(p, std::iter::once(egui::Event::ModifiersChanged(m)).chain(events).collect());
                run(p, vec![egui::Event::ModifiersChanged(egui::Modifiers::NONE)]);
            }
            None => run(p, events),
        }
    }

    /// A few frames (a new window takes some to reach its size), and every text the last one drew.
    fn texts(ctx: &egui::Context, p: &mut Player) -> String {
        fn walk(shape: &egui::Shape, out: &mut String) {
            match shape {
                egui::Shape::Text(t) => {
                    out.push_str(t.galley.text());
                    out.push('\n');
                }
                egui::Shape::Vec(v) => v.iter().for_each(|s| walk(s, out)),
                _ => {}
            }
        }
        for _ in 0..3 {
            frame(ctx, p);
        }
        let input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1280.0, 840.0))),
            ..Default::default()
        };
        let output = ctx.run_ui(input, |ui| p.ui(ui));
        let mut out = String::new();
        output.shapes.iter().for_each(|s| walk(&s.shape, &mut out));
        output.drop_without_applying_deltas();
        out
    }

    fn run_until(ctx: &egui::Context, p: &mut Player, what: &str, done: impl Fn(&Player) -> bool) {
        let t0 = Instant::now();
        while !done(p) {
            assert!(t0.elapsed() < Duration::from_secs(90), "timed out waiting for {what}");
            frame(ctx, p);
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn library_playback_lyrics_likes_modes_windows_and_state() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-ui-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        std::env::set_var(realhires::covers::COVERS_ENV, tmp.join("covers"));
        let a1 = song(&music, "Paper Lanterns", "Lanterns Over Water", "Mira Calloway", "Pop", 1, 44_100, 2.0);
        let a2 = song(&music, "Paper Lanterns", "Paper in the Rain", "Mira Calloway", "Pop", 2, 44_100, 1.5);
        let b1 = song(&music, "Harbour Lights", "Tidewater", "The Northern Quay", "Jazz", 1, 48_000, 1.5);
        std::fs::write(
            realhires::lyrics::sidecar(&a1, ".lrc"),
            "[00:00.20]Lanterns over water\n[00:01.00]Paper in the rain\n[00:01.60]Folded stars\n",
        )
        .unwrap();
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();

        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg.clone(), vec![]);
        assert_eq!(p.page, Page::Library);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 3 && p.lib_rx.is_none());
        assert_eq!(p.lib.albums.len(), 2);
        assert_eq!((p.lib.artists.len(), p.lib.genres.len()), (2, 2));
        // the Library page and every one of its entries
        let music_dir = realhires::paths::abspath(&music.to_string_lossy());
        for page in [
            Page::Library,
            Page::AllSongs,
            Page::Folders(None),
            Page::Folders(Some(music_dir.clone())),
            Page::Folders(Some(format!("{music_dir}{}Paper Lanterns", std::path::MAIN_SEPARATOR))),
            Page::Albums,
            Page::Artists,
            Page::AlbumArtists,
            Page::Genres,
            Page::Years,
            Page::Composers,
            Page::Playlists,
            Page::Liked,
            Page::Disliked,
            Page::RecentlyPlayed,
            Page::Recent,
            Page::Stats,
            Page::StatsDetails,
        ] {
            p.go(page);
            frame(&ctx, &mut p);
        }
        assert_eq!(p.lib.album_artists.len(), 2);
        let (subs, songs) = musiclib::folder_view(&p.lib.tracks, &music_dir);
        assert_eq!((subs.len(), songs.len()), (2, 0));

        // Back and Forward between pages (2.1): the mouse's side buttons and Alt+Left/Right
        // (Cmd+[ / Cmd+] on macOS); a new page clears the Forward list
        p.go(Page::Albums);
        p.go(Page::Genres);
        // a click of a side button: pressed and let go
        let side = |button| {
            [true, false]
                .map(|pressed| egui::Event::PointerButton { pos: pos2(700.0, 500.0), button, pressed, modifiers: egui::Modifiers::NONE })
                .to_vec()
        };
        frame_with(&ctx, &mut p, side(egui::PointerButton::Extra1));
        assert_eq!((&p.page, &p.forward), (&Page::Albums, &vec![Page::Genres]));
        frame_with(&ctx, &mut p, side(egui::PointerButton::Extra2));
        assert_eq!(p.page, Page::Genres);
        assert!(p.forward.is_empty() && p.back.last() == Some(&Page::Albums));
        let key = |key, mac_key| {
            let (key, modifiers) =
                if cfg!(target_os = "macos") { (mac_key, egui::Modifiers::MAC_CMD) } else { (key, egui::Modifiers::ALT) };
            egui::Event::Key { key, physical_key: None, pressed: true, repeat: false, modifiers }
        };
        frame_with(&ctx, &mut p, vec![key(egui::Key::ArrowLeft, egui::Key::OpenBracket)]);
        assert_eq!((&p.page, &p.forward), (&Page::Albums, &vec![Page::Genres]));
        p.go(Page::Years);
        assert!(p.forward.is_empty(), "a new page: nothing to go forward to");
        p.go_back();
        frame_with(&ctx, &mut p, vec![key(egui::Key::ArrowRight, egui::Key::CloseBracket)]);
        assert_eq!(p.page, Page::Years);

        // play the album from its first song; the next song follows gaplessly
        let key = p.lib.track(&a1).unwrap().song().album_key();
        p.go(Page::Album(key.clone()));
        frame(&ctx, &mut p);
        let album: Vec<String> = p.page_songs();
        assert_eq!(album, vec![a1.clone(), a2.clone()]);
        p.play_list(album.clone(), Some(&a1), "Paper Lanterns");
        assert_eq!(p.now.as_ref().unwrap().path, a1);
        run_until(&ctx, &mut p, "playback", |p| p.engine.position().unwrap_or(0.0) > 0.3);
        assert!(p.is_playing());
        assert_eq!(p.engine.info().unwrap().device, "Null output (no sound)");

        // Now Playing reads the synced lyrics
        p.go(Page::NowPlaying);
        frame(&ctx, &mut p);
        match p.lyrics_of_now() {
            Some(ShownLyrics::Synced(lines)) => assert_eq!(lines.len(), 3),
            other => panic!("no synced lyrics: {other:?}"),
        }
        // 2.1: on Now Playing the Lyrics button hides the lyrics and the page stays; pressed
        // again it shows them; the LCD's cover leaves the page and opens it again
        p.lyrics_button();
        frame(&ctx, &mut p);
        assert!(p.page == Page::NowPlaying && p.lyrics_hidden);
        p.lyrics_button();
        frame(&ctx, &mut p);
        assert!(p.page == Page::NowPlaying && !p.lyrics_hidden);
        p.cover_clicked();
        assert!(p.page != Page::NowPlaying);
        p.cover_clicked();
        frame(&ctx, &mut p);
        assert_eq!(p.page, Page::NowPlaying);
        // 2.1: Play Next from a song's menu comes first; the gapless look-ahead leaves the queue alone
        p.queue_songs(&[b1.clone()], true);
        assert_eq!(p.queue.as_ref().unwrap().up_next(2), vec![b1.clone(), a2.clone()]);
        assert_eq!(p.queue.as_ref().unwrap().queued, vec![b1.clone()]);
        p.queue.as_mut().unwrap().queued.clear();
        p.update_next();

        // likes are saved at once
        p.toggle_like(&a1);
        assert!(playerstate::load(None).is_liked(&a1));
        p.go(Page::Liked);
        frame(&ctx, &mut p);
        assert_eq!(p.page_songs(), vec![a1.clone()]);

        // the song ends and the next one of the album plays, without a gap
        run_until(&ctx, &mut p, "the second song", |p| p.now.as_ref().is_some_and(|n| n.path == a2));
        assert_eq!(p.queue.as_ref().unwrap().current(), Some(a2.as_str()));
        // then the album ends: playback stops (repeat off)
        run_until(&ctx, &mut p, "the end of the album", |p| p.now.as_ref().is_some_and(|n| !n.active));
        frame(&ctx, &mut p);
        // both songs are in the play history, the newest first
        let recent: Vec<String> = p.recently_played().into_iter().map(|(x, _)| x).collect();
        assert_eq!(recent, vec![a2.clone(), a1.clone()]);
        let st = p.stats();
        assert_eq!((st.logged, st.songs), (2, 2), "{st:?}");
        assert!(!realhires::stats::cards(&st).is_empty());
        for page in [Page::Library, Page::RecentlyPlayed, Page::Stats, Page::StatsDetails] {
            p.go(page);
            frame(&ctx, &mut p);
        }
        // the calendar of Your Stats (2.1): the month of the plays, the day's plays newest
        // first, and a song of the list plays the day's songs from it
        p.go(Page::Stats);
        frame(&ctx, &mut p);
        let first = realhires::stats::local_parts(p.history.as_ref().unwrap().plays(0.0, None)[0].started);
        p.calendar.month = (first.year, first.month);
        frame(&ctx, &mut p);
        let today = first.day as usize - 1;
        assert!(p.calendar.minutes[today] > 0.0 && p.calendar.levels[today] >= 1, "{:?}", p.calendar.minutes);
        p.calendar_choose(first.day);
        frame(&ctx, &mut p);
        let rows: Vec<(&str, &str, &str)> =
            p.calendar.rows.iter().map(|r| (r.title.as_str(), r.artist.as_str(), r.minutes.as_str())).collect();
        assert_eq!(rows, vec![("Paper in the Rain", "Mira Calloway", "<1 min"), ("Lanterns Over Water", "Mira Calloway", "<1 min")]);
        let lp = realhires::stats::local_parts(p.calendar.rows[1].started);
        assert_eq!(p.calendar.rows[1].time, format!("{:02}:{:02}", lp.hour, lp.minute));
        p.calendar_play(1);
        assert_eq!(p.now.as_ref().unwrap().path, a1);
        assert_eq!(p.queue.as_ref().unwrap().context, vec![a2.clone(), a1.clone()]);
        p.engine.stop();
        p.calendar_choose(first.day);
        frame(&ctx, &mut p);
        assert!(p.calendar.day.is_none() && p.calendar.rows.is_empty(), "the chosen day again hides the list");

        // playlists: make one, add, play from it, rename, delete
        p.apply_name(NameAction::NewPlaylist(vec![b1.clone()]), "Road Trip").unwrap();
        let pid = p.state.playlists[0].id.clone();
        p.add_to_playlist(&pid, &[a2.clone(), "/gone/song.flac".to_string()]);
        assert_eq!(p.playlist_songs(&pid), vec![b1.clone(), a2.clone(), "/gone/song.flac".to_string()]);
        p.go(Page::Playlist(pid.clone()));
        frame(&ctx, &mut p);
        p.play_list(p.playlist_songs(&pid), None, "Road Trip");
        assert_eq!(p.queue.as_ref().unwrap().context, vec![b1.clone(), a2.clone()], "missing files are skipped");
        p.engine.stop();
        p.apply_name(NameAction::RenamePlaylist(pid.clone()), "Night Drive").unwrap();
        assert_eq!(playerstate::load(None).playlists[0].name, "Night Drive");
        assert!(p.apply_name(NameAction::NewPlaylist(vec![]), "   ").is_err());
        p.confirmed(ConfirmAction::DeletePlaylist(pid));
        assert!(playerstate::load(None).playlists.is_empty());

        // a disliked song is left out of a shuffle
        p.toggle_dislike(&a2);
        assert!(playerstate::load(None).is_disliked(&a2));
        p.set_shuffle("library");
        p.play_list(vec![a1.clone(), a2.clone(), b1.clone()], Some(&a1), "Shuffle");
        assert!(!p.queue.as_ref().unwrap().order.contains(&a2), "{:?}", p.queue.as_ref().unwrap().order);
        p.set_shuffle("off");
        p.toggle_dislike(&a2);
        p.engine.stop();

        // EQ presets: save, rename, import an AutoEQ file, export both ways
        p.state.eq.apply_preset("Jazz");
        p.apply_name(NameAction::SaveEqPreset, "My Jazz").unwrap();
        assert!(p.apply_name(NameAction::SaveEqPreset, "rock").is_err());
        p.apply_name(NameAction::RenameEqPreset("My Jazz".into()), "Late Jazz").unwrap();
        assert_eq!(p.state.matching_eq_preset(), "Late Jazz");
        let auto = "Preamp: -6.2 dB\nFilter 1: ON PK Fc 105 Hz Gain 3.5 dB Q 0.70\nFilter 2: ON LSC Fc 105 Hz Gain 4.1 dB Q 0.71\n";
        assert_eq!(p.import_eq_text(auto, "HD 600").unwrap(), "HD 600");
        assert_eq!(p.import_eq_text(auto, "HD 600").unwrap(), "HD 600 2");
        assert!(p.state.eq.parametric_enabled && p.state.eq.bands.len() == 2);
        assert!(realhires::eq::export_apo("HD 600", &p.state.eq).contains("Filter 2: ON LSC Fc 105.0 Hz Gain 4.1 dB Q 0.71"));
        assert!(realhires::eq::import_eq(&realhires::eq::export_json("x", &p.state.eq), "y").is_ok());
        assert_eq!(playerstate::load(None).eq_presets.len(), 3);
        p.eq_open = true;
        frame(&ctx, &mut p);
        p.eq_open = false;

        // a theme of one's own: made from Midnight, a colour changed, in use
        p.set_theme("Midnight");
        frame(&ctx, &mut p);
        assert!(ctx.global_style().visuals.dark_mode);
        p.apply_name(NameAction::NewTheme("Midnight".into()), "My Night").unwrap();
        assert_eq!(p.scanner.cfg.theme, "My Night");
        let mine = p.custom_themes.iter().position(|t| t.name == "My Night").unwrap();
        p.custom_themes[mine].colors[5] = "#00ff00".into();
        p.themes_changed();
        frame(&ctx, &mut p);
        assert_eq!(crate::look::current(&ctx).accent, crate::look::parse_hex("#00ff00"));
        assert_eq!(realhires::themes::load_custom(None)[mine].colors[5], "#00ff00");
        p.go(Page::Settings);
        p.settings_group = realhires::settings::GROUPS.iter().position(|g| *g == "Appearance").unwrap();
        frame(&ctx, &mut p);
        p.confirmed(ConfirmAction::DeleteTheme("My Night".into()));
        assert_eq!(p.scanner.cfg.theme, realhires::themes::SYSTEM);
        p.set_theme("Light");
        frame(&ctx, &mut p);

        // shuffle and repeat modes go to the queue and the saved state
        p.play_list(album.clone(), Some(&a1), "Paper Lanterns");
        let first = p.now.as_ref().unwrap().token;
        p.set_shuffle("library");
        p.set_repeat("song");
        assert_eq!((p.queue.as_ref().unwrap().shuffle.as_str(), p.queue.as_ref().unwrap().repeat.as_str()), ("library", "song"));
        // the same song again, gaplessly, as a new play of it
        run_until(&ctx, &mut p, "repeat song", |p| p.now.as_ref().is_some_and(|n| n.token != first));
        assert_eq!(p.now.as_ref().unwrap().path, a1);
        p.set_repeat("off");
        p.set_shuffle("off");
        // paused from here on: the song lasts 2 s, and the quality scan below took longer on
        // the Windows runner, so the next song had started before quitting
        p.toggle_play();
        assert!(!p.is_playing());

        // the windows
        p.open_info();
        p.eq_open = true;
        p.state.eq = realhires::eq::EqState::default();
        p.state.eq.enabled = true;
        p.state.eq.apply_preset("Rock");
        p.set_eq();
        frame(&ctx, &mut p);
        let t = p.lib.track(&a1).unwrap().clone();
        let secs = crate::eqwin::info_sections(&t, 2, p.engine.info().as_ref(), &p.state, true, false, None);
        let flat: Vec<String> = secs.iter().flat_map(|(_, r)| r.iter().map(|(k, v)| format!("{k}={v}"))).collect();
        assert!(flat.contains(&"Sample rate=44.1 kHz".to_string()), "{flat:?}");
        assert!(flat.contains(&"Track=1 of 2".to_string()), "{flat:?}");
        assert!(flat.iter().any(|x| x.starts_with("Equalizer=Graphic: Rock")), "{flat:?}");
        assert!(flat.contains(&"Verdict=Not checked yet".to_string()), "{flat:?}");

        // search
        p.search = "tidewater".into();
        p.search_due = Some(Instant::now());
        frame(&ctx, &mut p);
        assert_eq!(p.page, Page::Search);
        p.search.clear();
        p.search_due = Some(Instant::now());
        frame(&ctx, &mut p);
        assert_ne!(p.page, Page::Search);

        // Settings: the menus that depend on the computer are filled
        p.go(Page::Settings);
        frame(&ctx, &mut p);
        assert!(p.scanner.choices.contains_key("font_family") && p.scanner.choices.contains_key("output_device"));
        p.settings_draft().gapless = false;
        p.settings_due = Some(Instant::now());
        frame(&ctx, &mut p);
        assert!(!p.scanner.cfg.gapless && !realhires::settings::load(None).gapless);

        // Quality check: the 1.x scanner on the library folders
        p.check_library();
        assert_eq!(p.page, Page::Quality);
        run_until(&ctx, &mut p, "the quality scan", |p| !p.scanner.scanning && p.scanner.results.len() == 3);
        p.open_info();
        assert!(p.info.as_ref().unwrap().verdict.is_some());

        // quitting remembers the song; the next start shows it paused there
        p.on_exit();
        drop(p);
        let st = playerstate::load(None);
        assert_eq!(st.last_path, a1);
        assert_eq!((st.eq.preset.as_str(), st.eq.enabled), ("Rock", true));
        let mut again = Player::new(&ctx, realhires::settings::load(None), vec![]);
        frame(&ctx, &mut again);
        assert_eq!(again.now.as_ref().map(|n| n.path.clone()), Some(a1.clone()));
        assert!(!again.is_playing() && again.engine.is_paused());
        assert!(again.state.is_liked(&a1));
        drop(again);
        let _ = std::fs::remove_dir_all(&tmp);
        let _ = b1;
    }

    #[test]
    fn edit_info_and_theme_files() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-edit-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        std::env::set_var(realhires::covers::COVERS_ENV, tmp.join("covers"));
        let a1 = song(&music, "Paper Lanterns", "Lanterns Over Water", "Mira Calloway", "Pop", 1, 44_100, 1.0);
        let a2 = song(&music, "Paper Lanterns", "Paper in the Rain", "Mira Calloway", "Pop", 2, 44_100, 1.0);
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 2 && p.lib_rx.is_none());

        // a theme file put into the themes folder appears in the menu, as it is
        let folder = realhires::themes::folder(None);
        assert_eq!(folder, tmp.join("themes"));
        std::fs::create_dir_all(&folder).unwrap();
        let neon = realhires::themes::normalize(&realhires::json::parse(
            r##"{"name": "Neon", "dark": true, "colors": {"accent": "#39ff14"}}"##).unwrap()).unwrap();
        std::fs::write(folder.join("neon.json"), realhires::themes::export_theme(&neon)).unwrap();
        std::fs::write(folder.join("broken.json"), "{").unwrap();
        assert!(p.reload_file_themes());
        assert!(p.scanner.choices["theme"].contains(&"Neon".to_string()));
        p.set_theme("Neon");
        p.go(Page::Settings);
        frame(&ctx, &mut p);
        assert_eq!(p.theme_file("Neon"), Some(folder.join("neon.json").as_path()));
        assert_eq!(look::current(&ctx).accent, look::parse_hex("#39ff14"));
        assert!(realhires::themes::load_custom(None).is_empty(), "a folder theme is not copied into themes.json");
        // a dropped (or imported) theme file becomes one of the user's own themes
        let sunset = tmp.join("sunset.json");
        let mut t = neon.clone();
        t.name = "Sunset".into();
        std::fs::write(&sunset, realhires::themes::export_theme(&t)).unwrap();
        assert!(crate::app::is_theme_file(&sunset) && !crate::app::is_theme_file(&tmp.join("missing.json")));
        p.import_theme_path(&sunset);
        assert_eq!(p.scanner.cfg.theme, "Sunset");
        assert_eq!(realhires::themes::load_custom(None)[0].name, "Sunset");
        // a second file called Neon gets a free name
        std::fs::write(folder.join("other.json"), realhires::themes::export_theme(&neon)).unwrap();
        p.reload_file_themes();
        assert!(p.scanner.choices["theme"].contains(&"Neon 2".to_string()));

        // Properties (2.0 "Edit Info") on one song: the composer and the release date
        p.go(Page::AllSongs);
        p.edit_song_info(&a1);
        run_until(&ctx, &mut p, "the tags", |p| !p.edit_info.as_ref().unwrap().reading());
        assert_eq!(p.edit_info.as_ref().unwrap().text[0], "Lanterns Over Water");
        // the window can be made bigger by its corner (2.1), and keeps that size for the next one
        frame(&ctx, &mut p);
        let window = || ctx.memory(|m| m.area_rect(egui::Id::new(crate::editinfo::WINDOW_ID))).unwrap();
        let before = window();
        assert!(before.width() >= 420.0, "{before:?}");
        let corner = before.max - vec2(3.0, 3.0);
        let wider = corner + vec2(200.0, 0.0);
        let button = |pos, pressed| egui::Event::PointerButton { pos, button: egui::PointerButton::Primary, pressed, modifiers: egui::Modifiers::NONE };
        frame_with(&ctx, &mut p, vec![egui::Event::PointerMoved(corner)]);
        frame_with(&ctx, &mut p, vec![button(corner, true)]);
        for i in 1..=4 {
            frame_with(&ctx, &mut p, vec![egui::Event::PointerMoved(corner + vec2(50.0 * i as f32, 0.0))]);
        }
        frame_with(&ctx, &mut p, vec![button(wider, false)]);
        frame(&ctx, &mut p);
        let after = window();
        assert!(after.width() >= before.width() + 150.0, "{before:?} → {after:?}");
        {
            let cfg = p.scanner.cfg.clone();
            let w = p.edit_info.as_mut().unwrap();
            w.set("composer", "Lena Frost");
            w.set("date", "2004-05-28");
            assert!(w.save(&ctx, &cfg));
        }
        run_until(&ctx, &mut p, "saving", |p| p.edit_info.is_none());
        run_until(&ctx, &mut p, "the library", |p| {
            p.lib_rx.is_none() && p.lib.track(&a1).is_some_and(|t| t.composer == "Lena Frost")
        });
        let tags = realhires::probe::probe(&a1).unwrap().tags;
        assert_eq!(realhires::probe::tag(&tags, "date"), Some("2004-05-28"));
        assert_eq!(realhires::probe::tag(&tags, "title"), Some("Lanterns Over Water"), "an unchanged field stays");

        // a bad track number is refused and nothing is written
        p.edit_song_info(&a2);
        run_until(&ctx, &mut p, "the tags", |p| !p.edit_info.as_ref().unwrap().reading());
        frame(&ctx, &mut p);
        assert_eq!(window().width(), after.width(), "the size is kept for the session");
        {
            let cfg = p.scanner.cfg.clone();
            let w = p.edit_info.as_mut().unwrap();
            w.set("track", "two");
            assert!(w.save(&ctx, &cfg));
            assert!(w.error.contains("must be a number"), "{}", w.error);
        }
        frame(&ctx, &mut p);
        assert!(p.edit_info.is_some());
        assert_eq!(realhires::probe::tag(&realhires::probe::probe(&a2).unwrap().tags, "track"), Some("2"));
        p.edit_info = None;

        // Edit album info: the album artist and the genre of every song of the album
        let key = p.lib.track(&a1).unwrap().song().album_key();
        p.go(Page::Album(key));
        frame(&ctx, &mut p);
        let album = p.lib.album_of(&a1).unwrap().clone();
        p.edit_album_info(&album);
        assert_eq!(p.edit_info.as_ref().unwrap().subheading, "Mira Calloway · 2 songs");
        run_until(&ctx, &mut p, "the tags", |p| !p.edit_info.as_ref().unwrap().reading());
        {
            let w = p.edit_info.as_ref().unwrap();
            assert_eq!(w.text[2], "Paper Lanterns");
            assert_eq!(w.text[0], "", "the titles differ, so the field starts empty");
            assert!(w.changes().is_empty());
        }
        frame(&ctx, &mut p);
        {
            let cfg = p.scanner.cfg.clone();
            let w = p.edit_info.as_mut().unwrap();
            w.set("genre", " Jazz ");
            w.set("album_artist", "Various Lanterns");
            assert_eq!(
                w.changes(),
                vec![("album_artist".to_string(), "Various Lanterns".to_string()), ("genre".to_string(), " Jazz ".to_string())]
            );
            assert!(w.save(&ctx, &cfg));
        }
        run_until(&ctx, &mut p, "saving the album", |p| p.edit_info.is_none());
        run_until(&ctx, &mut p, "the library", |p| {
            p.lib_rx.is_none()
                && [&a1, &a2].iter().all(|a| p.lib.track(a).is_some_and(|t| t.genre == "Jazz" && t.album_artist == "Various Lanterns"))
        });
        assert_eq!(p.lib.album_of(&a1).unwrap().tracks.len(), 2, "the album stays one album");
        assert_eq!(p.lib.track(&a2).unwrap().title, "Paper in the Rain");
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn bit_perfect_playback_controls() {
        // Settings, Playback, Bit-perfect playback (2.1): the song restarts where it was in its own
        // format, the gain is exactly 1 (the saved volume is kept), the equalizer is heard (2.3; it
        // was bypassed before), and the Playback info has the Bit-perfect line and the device's real
        // format
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-bitperfect-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        let a = song(&music, "Night Train", "Coastline", "Ada Lux", "Pop", 1, 96_000, 3.0);
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg.clone(), vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 1 && p.lib_rx.is_none());
        p.set_volume(0.5);
        p.state.eq.enabled = true;
        p.state.eq.apply_preset("Rock");
        p.set_eq();
        p.play_list(vec![a.clone()], Some(&a), "Night Train");
        run_until(&ctx, &mut p, "playback", |p| p.engine.position().unwrap_or(0.0) > 0.5);
        assert_eq!(p.engine.gain(), 0.25);
        assert_eq!(p.engine.info().unwrap().bit_perfect, "Off");
        let before = p.engine.position().unwrap();
        p.settings_draft().bit_perfect = true;
        p.settings_due = Some(Instant::now());
        frame(&ctx, &mut p);
        assert!(p.scanner.cfg.bit_perfect && realhires::settings::load(None).bit_perfect);
        assert!(p.engine.bit_perfect() && p.engine.gain() == 1.0 && p.state.volume == 0.5);
        run_until(&ctx, &mut p, "the restart", |p| p.engine.position().unwrap_or(0.0) > before + 0.1);
        assert_eq!(p.now.as_ref().unwrap().path, a, "the same song goes on");
        let info = p.engine.info().unwrap();
        assert_eq!((info.rate, info.sample_format), (96_000, realhires::bitperfect::Format::I16));
        let t = p.lib.track(&a).unwrap().clone();
        let secs = crate::eqwin::info_sections(&t, 1, Some(&info), &p.state, true, true, None);
        let flat: Vec<String> = secs.iter().flat_map(|(_, r)| r.iter().map(|(k, v)| format!("{k}={v}"))).collect();
        for want in [
            "Output=96 kHz, 16-bit integer, stereo",
            "Bit-perfect=No: The equalizer is on, so the samples are changed. The device still runs at 96 kHz.",
            "Volume=0.0 dB",
        ] {
            assert!(flat.contains(&want.to_string()), "{want}: {flat:?}");
        }
        assert!(flat.iter().any(|x| x.starts_with("Equalizer=Graphic: Rock")), "{flat:?}");
        p.eq_open = true;
        let shown = texts(&ctx, &mut p);
        assert!(shown.contains("Bit-perfect playback is on: with the equalizer on"), "{shown}");
        // the equalizer off: bit-perfect again
        p.state.eq.enabled = false;
        p.set_eq();
        let info = p.engine.info().unwrap();
        assert_eq!(info.bit_perfect, "Yes: 16-bit, 96 kHz, Null output (no sound)");
        p.set_volume(0.1); // the slider is disabled, and nothing else changes the volume either
        assert_eq!(p.state.volume, 0.5);
        p.settings_draft().bit_perfect = false;
        p.settings_due = Some(Instant::now());
        frame(&ctx, &mut p);
        assert!(!p.engine.bit_perfect() && p.engine.gain() == 0.25);
        run_until(&ctx, &mut p, "the normal path", |p| {
            p.engine.info().is_some_and(|i| i.sample_format == realhires::bitperfect::Format::F32)
        });
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// Album covers are made ahead once the library is read, stay when the
    /// library is read again, and only a changed album's cover is made again.
    #[test]
    fn covers_are_made_ahead_and_kept() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-covers-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        std::env::set_var(realhires::covers::COVERS_ENV, tmp.join("covers"));
        let a1 = song(&music, "Paper Lanterns", "Lanterns Over Water", "Mira Calloway", "Pop", 1, 44_100, 0.5);
        song(&music, "Harbour Lights", "Tidewater", "The Northern Quay", "Jazz", 1, 48_000, 0.5);
        for (album, name, colour) in [("Paper Lanterns", "cover.png", "red"), ("Harbour Lights", "folder.png", "blue")] {
            let img = music.join(album).join(name).to_string_lossy().into_owned();
            ffmpeg(&["-f", "lavfi", "-i", &format!("color=c={colour}:s=96x96"), "-frames:v", "1", &img]);
        }
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 2 && p.lib_rx.is_none());
        let paths = |a: &Album| -> Vec<String> { a.tracks.iter().map(|t| t.path.clone()).collect() };
        assert_eq!(p.covers.grid_px(), crate::cover_cache::GRID_PX);
        run_until(&ctx, &mut p, "the covers made ahead", |p| {
            p.covers.prefetching() == 0
                && p.lib.albums.iter().all(|a| realhires::covers::is_cached(&paths(a), crate::cover_cache::GRID_PX))
        });
        p.go(Page::Albums);
        let shown = |p: &Player| -> Vec<(String, egui::TextureId)> {
            let mut v: Vec<(String, egui::TextureId)> = p
                .lib
                .albums
                .iter()
                .filter_map(|a| {
                    let t = p.covers.tex.get(&a.key)?.iter().find(|(s, _)| *s == crate::cover_cache::GRID_PX)?;
                    Some((a.key.clone(), t.1.as_ref()?.id()))
                })
                .collect();
            v.sort();
            v
        };
        run_until(&ctx, &mut p, "the covers on screen", |p| shown(p).len() == 2);
        let before = shown(&p);

        // the library is read again: the textures stay
        p.update_library();
        run_until(&ctx, &mut p, "the second update", |p| p.lib_rx.is_none());
        frame(&ctx, &mut p);
        assert_eq!(shown(&p), before);

        // a song changes: only its album's cover is made again
        let f = std::fs::File::options().write(true).open(&a1).unwrap();
        f.set_modified(std::time::SystemTime::now() - Duration::from_secs(86_400)).unwrap();
        drop(f);
        let key_a = p.lib.track(&a1).unwrap().song().album_key();
        p.update_library();
        run_until(&ctx, &mut p, "the third update", |p| p.lib_rx.is_none());
        let now = shown(&p);
        assert_eq!(now.len(), 1, "{now:?}");
        assert!(now[0].0 != key_a && before.contains(&now[0]));
        run_until(&ctx, &mut p, "the new cover", |p| shown(p).len() == 2);
        let after = shown(&p);
        assert!(after.iter().any(|(k, id)| *k == key_a && !before.contains(&(k.clone(), *id))));
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// A click as a person makes it: the pointer moves there, presses, releases.
    fn click(ctx: &egui::Context, p: &mut Player, at: egui::Pos2) {
        frame_with(ctx, p, vec![egui::Event::PointerMoved(at)]);
        let button = |pressed| egui::Event::PointerButton {
            pos: at,
            button: egui::PointerButton::Primary,
            pressed,
            modifiers: Default::default(),
        };
        frame_with(ctx, p, vec![button(true)]);
        frame_with(ctx, p, vec![button(false)]);
        frame_with(ctx, p, vec![egui::Event::PointerMoved(pos2(5.0, 835.0))]);
    }

    /// A click that leaves the pointer where it is and draws no frame after the release,
    /// so a window it opens can be given a test client before it searches.
    fn click_only(ctx: &egui::Context, p: &mut Player, at: egui::Pos2) {
        frame_with(ctx, p, vec![egui::Event::PointerMoved(at)]);
        for pressed in [true, false] {
            let e = egui::Event::PointerButton { pos: at, button: egui::PointerButton::Primary, pressed, modifiers: Default::default() };
            frame_with(ctx, p, vec![e]);
        }
    }

    /// A right click (press, then the release in a later frame), which opens a context menu.
    fn right_click(ctx: &egui::Context, p: &mut Player, at: egui::Pos2) {
        frame_with(ctx, p, vec![egui::Event::PointerMoved(at)]);
        for pressed in [true, false] {
            let e = egui::Event::PointerButton { pos: at, button: egui::PointerButton::Secondary, pressed, modifiers: Default::default() };
            frame_with(ctx, p, vec![e]);
        }
    }

    /// Every text the last of a few frames drew and where, in drawing order (menus and
    /// windows come after the page).
    fn placed_texts(ctx: &egui::Context, p: &mut Player) -> Vec<(String, Rect)> {
        fn walk(shape: &egui::Shape, out: &mut Vec<(String, Rect)>) {
            match shape {
                egui::Shape::Text(t) => out.push((t.galley.text().to_string(), t.visual_bounding_rect())),
                egui::Shape::Vec(v) => v.iter().for_each(|s| walk(s, out)),
                _ => {}
            }
        }
        for _ in 0..3 {
            frame(ctx, p);
        }
        let input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1280.0, 840.0))),
            ..Default::default()
        };
        let output = ctx.run_ui(input, |ui| p.ui(ui));
        let mut out = vec![];
        output.shapes.iter().for_each(|s| walk(&s.shape, &mut out));
        output.drop_without_applying_deltas();
        out
    }

    /// Where the last text `text` was drawn (a menu's item comes after the page's own).
    fn last_text(texts: &[(String, Rect)], text: &str) -> egui::Pos2 {
        let names: Vec<&str> = texts.iter().map(|(t, _)| t.as_str()).collect();
        texts.iter().rev().find(|(t, _)| t == text).unwrap_or_else(|| panic!("no {text:?} in {names:?}")).1.center()
    }

    /// The texts of `after` that `before` did not have: what a menu opened in between shows.
    fn new_texts(before: &[(String, Rect)], after: &[(String, Rect)]) -> Vec<String> {
        let mut rest: Vec<&str> = before.iter().map(|(t, _)| t.as_str()).collect();
        let mut out = vec![];
        for (t, _) in after {
            // the moods' background analysis (2.4) changes the sidebar's status at any frame
            if t.starts_with("Analysing moods") || t.starts_with("ムードを解析中") {
                continue;
            }
            match rest.iter().position(|b| b == t) {
                Some(i) => {
                    rest.remove(i);
                }
                None => out.push(t.clone()),
            }
        }
        out
    }

    /// Right-click an album card (found by its title) and open its menu; the menu's items
    /// and the texts drawn with it open.
    fn album_card_menu(ctx: &egui::Context, p: &mut Player, title: &str) -> (Vec<String>, Vec<(String, Rect)>) {
        p.go(Page::Albums);
        let before = placed_texts(ctx, p);
        let names: Vec<&str> = before.iter().map(|(t, _)| t.as_str()).collect();
        // the lowest of the texts: the playback bar above may show the album's name too
        let card = before
            .iter()
            .filter(|(t, _)| t == title)
            .map(|(_, r)| *r)
            .max_by(|a, b| a.top().total_cmp(&b.top()))
            .unwrap_or_else(|| panic!("no card {title:?} in {names:?}"));
        // on the cover, above the title
        let at = pos2(card.left() + 60.0, card.top() - 80.0);
        frame_with(ctx, p, vec![egui::Event::PointerMoved(at)]);
        let before = placed_texts(ctx, p);
        right_click(ctx, p, at);
        let after = placed_texts(ctx, p);
        (new_texts(&before, &after), after)
    }

    /// The mouse wheel over `at` (a negative `dy` moves the content up: scrolls down).
    fn wheel(ctx: &egui::Context, p: &mut Player, at: egui::Pos2, dy: f32) {
        frame_with(ctx, p, vec![egui::Event::PointerMoved(at)]);
        frame_with(
            ctx,
            p,
            vec![egui::Event::MouseWheel {
                unit: egui::MouseWheelUnit::Point,
                delta: vec2(0.0, dy),
                phase: egui::TouchPhase::Move,
                source: egui::MouseWheelSource::Unknown,
                modifiers: Default::default(),
            }],
        );
        for _ in 0..30 {
            frame_with(ctx, p, vec![]);
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    fn link_at(links: &[(Rect, crate::pages::Link)], want: &crate::pages::Link) -> egui::Pos2 {
        links.iter().find(|(_, l)| l == want).unwrap_or_else(|| panic!("no link {want:?} in {links:?}")).0.center()
    }

    /// One song on a fake LRCLIB: /api/get answers "Midnight Sun" with synced lyrics.
    fn fake_lrclib() -> String {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut s) = stream else { continue };
                let mut req = vec![];
                let mut buf = [0u8; 1024];
                while !req.windows(4).any(|w| w == b"\r\n\r\n") {
                    match s.read(&mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => req.extend_from_slice(&buf[..n]),
                    }
                }
                let line = String::from_utf8_lossy(&req).lines().next().unwrap_or("").to_string();
                let (status, body) = if line.starts_with("GET /api/get?") && line.contains("track_name=Midnight") {
                    (
                        "200 OK",
                        r#"{"id": 7, "trackName": "Midnight Sun", "artistName": "Aurora Fields", "albumName": "Northern Lights", "duration": 20.0, "instrumental": false, "plainLyrics": "Midnight sun\nOver the bay", "syncedLyrics": "[00:00.50] Midnight sun\n[00:03.00] Over the bay"}"#,
                    )
                } else {
                    ("404 Not Found", r#"{"message": "Failed to find specified track"}"#)
                };
                let _ = write!(
                    s,
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        url
    }

    /// Now Playing's 2.3 requests (docs/player-spec.md, "2.3"): the back link at the top left
    /// naming the page it returns to (5), Lyrics only and Esc (10), an instrumental song's
    /// cover in the centre (4), swiping the cover to the album's songs (11) and Details in the
    /// centre (14).
    #[test]
    fn now_playing_back_wide_instrumental_swipe_and_details() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-np23-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        std::env::set_var(realhires::covers::COVERS_ENV, tmp.join("covers"));
        let first = song(&music, "Northern Lights", "First Light", "Aurora Fields", "Ambient", 1, 44_100, 20.0);
        let glacier = song(&music, "Northern Lights", "Glacier Song", "Aurora Fields", "Ambient", 2, 44_100, 20.0);
        let sun = song(&music, "Northern Lights", "Midnight Sun", "Aurora Fields", "Ambient", 3, 44_100, 20.0);
        let drift = song(&music, "Northern Lights", "Polar Drift", "Aurora Fields", "Ambient", 4, 44_100, 20.0);
        let taxi = song(&music, "City Rain", "Taxi Lights", "The Paper Boats", "Indie Pop", 1, 44_100, 20.0);
        let lines: Vec<String> = (0..12).map(|i| format!("[00:{:02}.00]Line number {i}", 1 + i)).collect();
        std::fs::write(realhires::lyrics::sidecar(&glacier, ".lrc"), lines.join("\n") + "\n").unwrap();
        // LRCGET's mark for an instrumental song
        std::fs::write(realhires::lyrics::sidecar(&sun, ".lrc"), "[au: instrumental]\n").unwrap();
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 5 && p.lib_rx.is_none());
        let album_songs = vec![first.clone(), glacier.clone(), sun.clone(), drift.clone()];
        let play = |ctx: &egui::Context, p: &mut Player, list: Vec<String>, at: &str, name: &str| {
            p.play_list(list, Some(at), name);
            run_until(ctx, p, "playback", |p| p.engine.position().is_some());
            if p.is_playing() {
                p.toggle_play(); // paused: the songs must not end during the checks
            }
            for _ in 0..3 {
                frame(ctx, p);
            }
        };
        let press = |ctx: &egui::Context, p: &mut Player, at: egui::Pos2, pressed: bool| {
            let e = egui::Event::PointerButton { pos: at, button: egui::PointerButton::Primary, pressed, modifiers: Default::default() };
            frame_with(ctx, p, vec![e]);
        };
        // a press, a move in two steps, and the release
        let drag = |ctx: &egui::Context, p: &mut Player, from: egui::Pos2, dx: f32| {
            frame_with(ctx, p, vec![egui::Event::PointerMoved(from)]);
            press(ctx, p, from, true);
            frame_with(ctx, p, vec![egui::Event::PointerMoved(from + vec2(dx / 2.0, 2.0))]);
            frame_with(ctx, p, vec![egui::Event::PointerMoved(from + vec2(dx, 3.0))]);
            press(ctx, p, from + vec2(dx, 3.0), false);
            for _ in 0..2 {
                frame(ctx, p);
            }
        };
        let sideways = |ctx: &egui::Context, p: &mut Player, at: egui::Pos2, dx: f32| {
            frame_with(ctx, p, vec![egui::Event::PointerMoved(at)]);
            let e = egui::Event::MouseWheel {
                unit: egui::MouseWheelUnit::Point,
                delta: vec2(dx, 0.0),
                phase: egui::TouchPhase::Move,
                source: egui::MouseWheelSource::Unknown,
                modifiers: Default::default(),
            };
            frame_with(ctx, p, vec![e]);
            for _ in 0..20 {
                frame_with(ctx, p, vec![]);
                std::thread::sleep(Duration::from_millis(10));
            }
        };
        // every text the last frame drew, with its rectangle and font
        fn fonts(ctx: &egui::Context, p: &mut Player) -> Vec<(String, Rect, egui::FontId)> {
            fn walk(shape: &egui::Shape, out: &mut Vec<(String, Rect, egui::FontId)>) {
                match shape {
                    egui::Shape::Text(t) => {
                        let font = t.galley.job.sections.first().map(|s| s.format.font_id.clone()).unwrap_or_default();
                        out.push((t.galley.text().to_string(), t.visual_bounding_rect(), font));
                    }
                    egui::Shape::Vec(v) => v.iter().for_each(|s| walk(s, out)),
                    _ => {}
                }
            }
            for _ in 0..2 {
                frame(ctx, p);
            }
            let input = egui::RawInput { screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1280.0, 840.0))), ..Default::default() };
            let output = ctx.run_ui(input, |ui| p.ui(ui));
            let mut out = vec![];
            output.shapes.iter().for_each(|s| walk(&s.shape, &mut out));
            output.drop_without_applying_deltas();
            out
        }

        // 5. the back link names the page Now Playing returns to, and goes there
        p.go(Page::AllSongs);
        play(&ctx, &mut p, album_songs.clone(), &glacier, "Northern Lights");
        p.cover_clicked();
        frame(&ctx, &mut p);
        assert_eq!(p.page, Page::NowPlaying);
        let (back, label) = p.np_hits.back_page.clone().expect("the back link");
        assert_eq!(label, "All Songs");
        assert!(back.left() < 260.0 && back.top() < 110.0, "at the top left: {back:?}");
        click(&ctx, &mut p, back.center());
        assert_eq!(p.page, Page::AllSongs);
        assert_eq!(p.forward.last(), Some(&Page::NowPlaying));
        p.go_forward();
        let city = p.lib.album_of(&taxi).unwrap().key.clone();
        p.go(Page::Album(city));
        p.lyrics_button();
        frame(&ctx, &mut p);
        assert_eq!(p.np_hits.back_page.as_ref().map(|b| b.1.as_str()), Some("City Rain"));
        // a search page comes back with its results
        p.search = "glacier".into();
        p.search_due = Some(Instant::now());
        frame(&ctx, &mut p);
        assert_eq!(p.page, Page::Search);
        p.cover_clicked();
        frame(&ctx, &mut p);
        let (back, label) = p.np_hits.back_page.clone().expect("the back link");
        assert_eq!(label, "Search");
        click(&ctx, &mut p, back.center());
        assert_eq!((p.page.clone(), p.search_query.as_str()), (Page::Search, "glacier"));
        p.cover_clicked();
        p.back.clear();
        frame(&ctx, &mut p);
        assert!(p.np_hits.back_page.is_none(), "no page to go back to: no link");

        // 10. Lyrics only: no cover, the lyrics fill the page, centred and 4 px larger; Esc
        assert_eq!(p.np_mode(), NpMode::Halves);
        let half = p.np_hits.lyrics_rect.expect("the lyrics");
        assert!(half.left() > 700.0, "{half:?}");
        let wide = p.np_hits.wide_button.expect("the Lyrics only button");
        click(&ctx, &mut p, wide.center());
        assert!(p.lyrics_wide && p.np_mode() == NpMode::Wide);
        let texts = fonts(&ctx, &mut p);
        assert!(p.np_hits.cover.is_none() && p.np_hits.dots.is_empty(), "no cover");
        let lrect = p.np_hits.lyrics_rect.expect("the lyrics");
        assert!(lrect.left() < 300.0 && lrect.width() > 900.0, "the lyrics use the whole page: {lrect:?}");
        let line = texts.iter().find(|t| t.0 == "Line number 0").expect("a lyrics line");
        assert_eq!(line.2.size, (p.state.lyrics_size + crate::pages::LYRICS_WIDE_EXTRA) as f32);
        assert!((line.1.center().x - lrect.center().x).abs() < 12.0, "centred: {:?} in {lrect:?}", line.1);
        assert!(texts.iter().any(|t| t.0 == "Up Next"), "the switch stays");
        p.set_np_tab(NpTab::UpNext); // Up Next keeps the two halves
        frame(&ctx, &mut p);
        assert!(p.np_mode() == NpMode::Halves && p.np_hits.cover.is_some());
        p.set_np_tab(NpTab::Lyrics);
        frame(&ctx, &mut p);
        assert_eq!(p.np_mode(), NpMode::Wide);
        let esc = egui::Event::Key { key: egui::Key::Escape, physical_key: None, pressed: true, repeat: false, modifiers: egui::Modifiers::NONE };
        frame_with(&ctx, &mut p, vec![esc]);
        frame(&ctx, &mut p);
        assert!(!p.lyrics_wide && p.np_mode() == NpMode::Halves && p.np_hits.cover.is_some(), "Esc brings the cover back");
        let line = fonts(&ctx, &mut p).into_iter().find(|t| t.0 == "Line number 0").expect("a lyrics line");
        assert_eq!(line.2.size, p.state.lyrics_size as f32);
        p.toggle_lyrics_wide();
        p.toggle_lyrics_wide(); // pressed again: the cover is back
        assert_eq!(p.np_mode(), NpMode::Halves);

        // 4. an instrumental song: the cover in the centre with "Instrumental", the switch above
        play(&ctx, &mut p, album_songs.clone(), &sun, "Northern Lights");
        assert_eq!(p.np_mode(), NpMode::Instrumental);
        let texts = fonts(&ctx, &mut p);
        assert!(p.np_hits.instrumental.is_some() && texts.iter().any(|t| t.0 == "Instrumental"));
        assert!(texts.iter().any(|t| t.0 == "Up Next"), "the switch stays");
        assert!(p.np_hits.lyrics_rect.is_none() && p.np_hits.get_lyrics.is_none() && p.np_hits.wide_button.is_none());
        let cover = p.np_hits.cover.expect("the cover");
        assert!((cover.center().x - (224.0 + 1280.0) / 2.0).abs() < 2.0, "in the middle of the page: {cover:?}");
        p.toggle_lyrics_wide(); // Lyrics only does not apply to an instrumental song
        assert_eq!(p.np_mode(), NpMode::Instrumental);
        p.toggle_lyrics_wide();
        p.set_np_tab(NpTab::Details); // Details keeps the two halves
        frame(&ctx, &mut p);
        assert!(p.np_mode() == NpMode::Halves && p.np_hits.cover.unwrap().center().x < 640.0);
        p.set_np_tab(NpTab::Lyrics);

        // 11. a sideways drag turns the left half to the album's songs; a click is still a click
        play(&ctx, &mut p, album_songs.clone(), &glacier, "Northern Lights");
        let cover = p.np_hits.cover.expect("the cover");
        click(&ctx, &mut p, cover.center());
        assert!(p.cover_win.is_some(), "a click opens the Cover window");
        p.cover_win = None;
        frame(&ctx, &mut p);
        frame_with(&ctx, &mut p, vec![egui::Event::PointerMoved(cover.center())]);
        press(&ctx, &mut p, cover.center(), true);
        frame_with(&ctx, &mut p, vec![egui::Event::PointerMoved(cover.center() - vec2(30.0, 0.0))]);
        assert!(!p.np_songs, "30 px is not a swipe yet");
        frame_with(&ctx, &mut p, vec![egui::Event::PointerMoved(cover.center() - vec2(90.0, -3.0))]);
        press(&ctx, &mut p, cover.center() - vec2(90.0, -3.0), false);
        frame(&ctx, &mut p);
        assert!(p.np_songs, "the swipe turned the left half");
        assert!(p.cover_win.is_none(), "a drag is not a click");
        let texts = fonts(&ctx, &mut p);
        let rows = p.np_hits.album_songs.clone();
        assert_eq!(rows.iter().map(|r| r.1.clone()).collect::<Vec<_>>(), album_songs);
        assert_eq!(rows.iter().map(|r| r.2).collect::<Vec<_>>(), vec![false, true, false, false]);
        let shown = |title: &str| texts.iter().find(|t| t.0 == title && t.1.center().x < 752.0).cloned().unwrap_or_else(|| panic!("{title} is not listed"));
        assert_eq!(shown("Glacier Song").2.family, look::title(13.0).family, "the song playing in bold");
        assert_eq!(shown("Polar Drift").2.family, look::text(13.0).family);
        assert_eq!(p.np_hits.dots.len(), 2);
        // a double-click plays a song with the album as the list (after a pause, or egui counts
        // it with the click on the cover as a triple click)
        for _ in 0..30 {
            frame(&ctx, &mut p);
        }
        let row = rows[3].0.center();
        frame_with(&ctx, &mut p, vec![egui::Event::PointerMoved(row)]);
        for pressed in [true, false, true, false] {
            press(&ctx, &mut p, row, pressed);
        }
        run_until(&ctx, &mut p, "Polar Drift", |p| p.now.as_ref().is_some_and(|n| n.path == drift));
        assert_eq!(p.queue_name, "Northern Lights");
        if p.is_playing() {
            p.toggle_play();
        }
        frame(&ctx, &mut p);
        assert_eq!(p.np_hits.album_songs.iter().filter(|r| r.2).map(|r| r.1.clone()).collect::<Vec<_>>(), vec![drift.clone()]);
        // it follows the song: another album's songs
        play(&ctx, &mut p, vec![taxi.clone()], &taxi, "City Rain");
        assert_eq!(p.np_hits.album_songs.iter().map(|r| r.1.clone()).collect::<Vec<_>>(), vec![taxi.clone()]);
        // the dots switch, and a drag either way turns the half again
        let dot = p.np_hits.dots[0].center();
        click(&ctx, &mut p, dot);
        assert!(!p.np_songs && p.np_hits.album_songs.is_empty());
        let cover = p.np_hits.cover.expect("the cover");
        drag(&ctx, &mut p, cover.center(), 80.0);
        assert!(p.np_songs);
        drag(&ctx, &mut p, cover.center(), 80.0);
        assert!(!p.np_songs);
        // a sideways scroll: the content moving left shows the songs, to the right the cover
        sideways(&ctx, &mut p, cover.center(), -120.0);
        assert!(p.np_songs);
        sideways(&ctx, &mut p, cover.center(), 120.0);
        assert!(!p.np_songs);
        assert!(p.cover_win.is_none());

        // 14. Details: one block in the middle of the right half
        play(&ctx, &mut p, album_songs.clone(), &glacier, "Northern Lights");
        p.set_np_tab(NpTab::Details);
        for _ in 0..4 {
            frame(&ctx, &mut p);
        }
        let block = p.np_hits.details_block.expect("the Details block");
        let view = p.np_hits.details_view.expect("the Details view");
        assert!(block.width() > 100.0 && block.height() > 100.0, "{block:?}");
        assert!((block.center().x - view.center().x).abs() < 14.0, "centred across: {block:?} in {view:?}");
        assert!((block.center().y - view.center().y).abs() < 14.0, "centred down: {block:?} in {view:?}");
        assert!(view.contains_rect(block), "a long path wraps within it");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// Now Playing (2.1): the label above the lyrics, A− / A+ saved in player.json, "Back to
    /// the current line" while the current line is scrolled away, the switch Lyrics | Up Next
    /// | Details, one link per artist (also in the LCD and Details), the cover's hook, and Get
    /// lyrics, which downloads at once (the user's "Get lyrics button doesn't work": it opened
    /// the lyrics window, which waited for its own Download lyrics button).
    #[test]
    fn now_playing_switch_links_lyrics_size_and_get_lyrics() {
        use crate::pages::Link;
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-np-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        std::env::set_var(realhires::lyrics::INTERVAL_ENV, "0");
        let glacier = song(&music, "Northern Lights", "Glacier Song", "Aurora Fields", "Ambient", 1, 44_100, 20.0);
        let sun = song(&music, "Northern Lights", "Midnight Sun", "Aurora Fields", "Ambient", 2, 44_100, 20.0);
        let kettle = song(&music, "Small Hours", "Blue Kettle", "June Harbor", "Jazz", 1, 44_100, 1.0);
        // a duet with a comment tag
        let duet_dir = music.join("Duets");
        std::fs::create_dir_all(&duet_dir).unwrap();
        let duet = realhires::paths::abspath(&duet_dir.join("01 Together.flac").to_string_lossy());
        ffmpeg(&["-f", "lavfi", "-i", "sine=frequency=500:sample_rate=44100:duration=2", "-metadata", "title=Together",
                 "-metadata", "artist=June Harbor, Aurora Fields", "-metadata", "album=Duets", "-metadata",
                 "comment=Recorded live", &duet]);
        let lines: Vec<String> = (0..40).map(|i| format!("[00:{:02}.{:02}]Line number {i}", i / 2, (i % 2) * 50)).collect();
        std::fs::write(realhires::lyrics::sidecar(&glacier, ".lrc"), lines.join("\n") + "\n").unwrap();
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        cfg.lyrics_server = fake_lrclib();
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 4 && p.lib_rx.is_none());
        p.play_list(vec![glacier.clone(), sun.clone()], Some(&glacier), "Northern Lights");
        run_until(&ctx, &mut p, "playback", |p| p.engine.position().unwrap_or(0.0) > 0.3);
        p.toggle_play(); // paused, so the current line stays
        p.lyrics_button();
        for _ in 0..3 {
            frame(&ctx, &mut p);
        }
        assert_eq!((p.page.clone(), p.np_tab), (Page::NowPlaying, NpTab::Lyrics));
        // the label says what the lyrics are and where they come from
        assert_eq!(p.lyrics_label_of_now(), ("Synced lyrics".to_string(), ".lrc file".to_string()));
        // the cover with the title and "Artist — Album": one block in the middle of the left half
        let cover = p.np_hits.cover.expect("the cover");
        let links = p.np_hits.links.clone();
        let line = links.iter().map(|(r, _)| r.center().x).fold(0.0, f32::max);
        assert!(cover.center().x < 640.0 && cover.left() < line && line < cover.right(), "{cover:?} {links:?}");
        // A− / A+: 26 px by default, 2 px a step, saved at once
        assert_eq!(p.state.lyrics_size, 26);
        let sizes = p.np_hits.size_buttons.clone();
        click(&ctx, &mut p, sizes[1].center());
        assert_eq!((p.state.lyrics_size, playerstate::load(None).lyrics_size), (28, 28));
        click(&ctx, &mut p, sizes[0].center());
        assert_eq!((p.state.lyrics_size, playerstate::load(None).lyrics_size), (26, 26));
        // scrolled away from the current line: following stops and the Back button shows
        let lyrics_at = pos2(1000.0, 600.0);
        wheel(&ctx, &mut p, lyrics_at, -3000.0);
        assert!(p.lyrics_away, "scrolled away");
        let back = p.np_hits.back.expect("the Back to the current line button");
        for _ in 0..5 {
            frame(&ctx, &mut p);
        }
        assert!(p.lyrics_away && p.np_hits.back.is_some(), "no automatic scrolling while it shows");
        click(&ctx, &mut p, back.center());
        for _ in 0..5 {
            frame(&ctx, &mut p);
        }
        assert!(!p.lyrics_away && p.np_hits.back.is_none());
        wheel(&ctx, &mut p, lyrics_at, -3000.0);
        assert!(p.lyrics_away);
        wheel(&ctx, &mut p, lyrics_at, 3000.0); // scrolled back into view: the button goes
        assert!(!p.lyrics_away && p.np_hits.back.is_none());
        // the switch: Up Next lists the rest of the album, Details describes the song
        p.set_np_tab(NpTab::UpNext);
        frame(&ctx, &mut p);
        assert_eq!(p.np_hits.up_next, vec![sun.clone()]);
        p.set_np_tab(NpTab::Details);
        frame(&ctx, &mut p);
        for needed in ["Title: Glacier Song", "Artist: Aurora Fields", "Track: 1 of 2", "Format: FLAC · 16-bit/44.1 kHz", "Quality check: Not checked yet"] {
            assert!(p.np_hits.details.iter().any(|l| l.starts_with(needed)), "{needed}: {:?}", p.np_hits.details);
        }
        p.lyrics_button(); // the Lyrics button chooses Lyrics again
        assert!(p.np_tab == NpTab::Lyrics && !p.lyrics_hidden);
        frame(&ctx, &mut p);
        // the album name opens the album, the artist the artist, the cover the Cover window hook
        let links = p.np_hits.links.clone();
        click(&ctx, &mut p, link_at(&links, &Link::Album));
        assert!(matches!(p.page, Page::Album(_)), "{:?}", p.page);
        p.go(Page::NowPlaying);
        frame(&ctx, &mut p);
        click(&ctx, &mut p, link_at(&links, &Link::Artist("Aurora Fields".into())));
        assert_eq!(p.page, Page::Group("artists", "Aurora Fields".into()));
        p.go(Page::NowPlaying);
        frame(&ctx, &mut p);
        click(&ctx, &mut p, cover.center());
        assert_eq!(p.page, Page::NowPlaying);
        assert!(p.cover_win.is_some(), "the Now Playing cover opens the Cover window");
        p.cover_win = None;
        frame(&ctx, &mut p);
        // the LCD: the title and the album name open the album, the artist the artist
        let lcd = p.lcd_links.clone();
        assert_eq!(lcd.iter().filter(|(_, l)| *l == Link::Album).count(), 2, "{lcd:?}");
        click(&ctx, &mut p, lcd[0].0.center());
        assert!(matches!(p.page, Page::Album(_)));
        click(&ctx, &mut p, link_at(&lcd, &Link::Artist("Aurora Fields".into())));
        assert_eq!(p.page, Page::Group("artists", "Aurora Fields".into()));

        // several artists in one field: each its own link, on Now Playing, in the LCD and in Details
        p.play_list(vec![duet.clone()], Some(&duet), "Duets");
        p.toggle_play();
        p.go(Page::NowPlaying);
        for _ in 0..3 {
            frame(&ctx, &mut p);
        }
        let artists: Vec<Link> = p.np_hits.links.iter().map(|(_, l)| l.clone()).filter(|l| *l != Link::Album).collect();
        assert_eq!(artists, vec![Link::Artist("June Harbor".into()), Link::Artist("Aurora Fields".into())]);
        let links = p.np_hits.links.clone();
        click(&ctx, &mut p, link_at(&links, &Link::Artist("June Harbor".into())));
        assert_eq!(p.page, Page::Group("artists", "June Harbor".into()));
        assert!(p.lcd_links.iter().any(|(_, l)| *l == Link::Artist("Aurora Fields".into())), "{:?}", p.lcd_links);
        p.go(Page::NowPlaying);
        p.set_np_tab(NpTab::Details);
        run_until(&ctx, &mut p, "the comment tag", |p| p.np_hits.details.iter().any(|l| l == "Comment: Recorded live"));
        assert!(p.np_hits.details.iter().any(|l| l == "Artist: June Harbor, Aurora Fields"), "{:?}", p.np_hits.details);
        for _ in 0..3 {
            frame(&ctx, &mut p); // the grid settles its column widths
        }
        let names: Vec<String> = p.np_hits.detail_links.iter().map(|(_, n)| n.clone()).collect();
        assert_eq!(names, vec!["June Harbor".to_string(), "Aurora Fields".to_string()]);
        let second = p.np_hits.detail_links[1].0.center();
        click(&ctx, &mut p, second);
        assert_eq!(p.page, Page::Group("artists", "Aurora Fields".into()));

        // Get lyrics: the download starts at once, and Now Playing shows the lyrics, staying
        p.play_list(vec![sun.clone()], Some(&sun), "Northern Lights");
        p.toggle_play();
        p.lyrics_button();
        for _ in 0..3 {
            frame(&ctx, &mut p);
        }
        assert!(p.lyrics_of_now().is_none());
        let get = p.np_hits.get_lyrics.expect("the Get lyrics button");
        click(&ctx, &mut p, get.center());
        assert!(p.scanner.lyrics_win.as_ref().is_some_and(|w| w.running), "the download started at once");
        run_until(&ctx, &mut p, "the lyrics", |p| p.scanner.lyrics_win.as_ref().is_some_and(|w| !w.running));
        assert_eq!(p.scanner.lyrics_win.as_ref().unwrap().status, "1 with synced lyrics.");
        frame(&ctx, &mut p);
        assert_eq!(p.page, Page::NowPlaying);
        match p.lyrics_of_now() {
            Some(ShownLyrics::Synced(lines)) => assert_eq!(lines[0].1, "Midnight sun"),
            other => panic!("no synced lyrics: {other:?}"),
        }
        assert!(std::path::Path::new(&realhires::lyrics::sidecar(&sun, ".lrc")).is_file());
        // a song's menu has Get lyrics… too (get_lyrics): this one is not on LRCLIB
        p.get_lyrics(vec![kettle.clone()]);
        assert!(p.scanner.lyrics_win.as_ref().is_some_and(|w| w.running));
        run_until(&ctx, &mut p, "the second lookup", |p| p.scanner.lyrics_win.as_ref().is_some_and(|w| !w.running));
        assert_eq!(p.scanner.lyrics_win.as_ref().unwrap().outcomes[0].status, realhires::lyrics::Status::NotFound);
        p.scanner.lyrics_win = None;
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// A song with a composer and a release date too.
    #[allow(clippy::too_many_arguments)]
    fn tagged(dir: &std::path::Path, album: &str, title: &str, artist: &str, genre: &str, composer: &str, date: &str, n: u32) -> String {
        let d = dir.join(album);
        std::fs::create_dir_all(&d).unwrap();
        let f = d.join(format!("{n:02} {title}.flac"));
        let src = format!("sine=frequency={}:sample_rate=44100:duration=1", 300 + 40 * n);
        let meta = [
            format!("title={title}"),
            format!("artist={artist}"),
            format!("album_artist={artist}"),
            format!("album={album}"),
            format!("track={n}"),
            format!("genre={genre}"),
            format!("composer={composer}"),
            format!("date={date}"),
        ];
        let mut args: Vec<&str> = vec!["-f", "lavfi", "-i", &src];
        for m in &meta {
            args.extend(["-metadata", m.as_str()]);
        }
        let out = f.to_string_lossy().into_owned();
        args.push(&out);
        ffmpeg(&args);
        realhires::paths::abspath(&out)
    }

    fn tag_of(path: &str, key: &str) -> String {
        realhires::probe::probe(path).unwrap().tags.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone()).unwrap_or_default()
    }

    /// Run a confirmed rename until its files are written and the library was read again.
    fn rename(ctx: &egui::Context, p: &mut Player, what: &str, check: impl Fn(&Player) -> bool) {
        let action = p.confirm.take().expect("a confirmation");
        p.confirmed(action);
        run_until(ctx, p, what, |p| p.tag_job.as_ref().is_some_and(|j| j.done()) && p.lib_rx.is_none() && check(p));
        assert!(p.tag_job.as_ref().unwrap().outcomes.as_ref().unwrap().iter().all(|o| o.ok));
    }

    #[test]
    fn grids_renames_columns_quality_and_preset_delete() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-grids-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        let a1 = tagged(&music, "Paper Lanterns", "Lanterns Over Water", "Mira Calloway", "Pop", "Ann", "2001-05-03", 1);
        let a2 = tagged(&music, "Paper Lanterns", "Paper in the Rain", "Mira Calloway", "Pop", "Ann", "2001", 2);
        let b1 = tagged(&music, "Harbour Lights", "Tidewater", "The Northern Quay", "Jazz", "", "1999", 1);
        let c1 = tagged(&music, "Two Rivers", "Where the Rivers Meet", "Mira Calloway, The Northern Quay", "Folk", "Ann", "2005", 1);
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg.clone(), vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 4 && p.lib_rx.is_none());

        // the grouping pages are card grids: each group with its albums (a duet on both artists)
        let names: Vec<&str> = p.lib.artists.iter().map(|g| g.name.as_str()).collect();
        assert_eq!(names, ["Mira Calloway", "The Northern Quay"]);
        assert_eq!(p.lib.group_albums["artists"][0].len(), 2, "Paper Lanterns and Two Rivers");
        assert_eq!(p.lib.group_albums["composers"].len(), 2);
        for page in [Page::Artists, Page::AlbumArtists, Page::Genres, Page::Years, Page::Composers] {
            p.go(page);
            frame(&ctx, &mut p);
        }
        // open_artist(): an artist, or the first artist of a field that is not one
        p.open_artist("Mira Calloway & Somebody New");
        assert_eq!(p.page, Page::Group("artists", "Mira Calloway".into()));
        frame(&ctx, &mut p);
        p.open_artist("The Northern Quay");
        assert_eq!(p.page, Page::Group("artists", "The Northern Quay".into()));
        // the album page: track order at first, sortable, the album columns (# first)
        let key = p.lib.track(&a1).unwrap().song().album_key();
        p.go(Page::Album(key.clone()));
        frame(&ctx, &mut p);
        assert_eq!(p.album_sort, None);
        assert_eq!(p.state.columns_of("album")[0], "num");
        p.set_sort("album", SongCol::Title);
        p.set_sort("album", SongCol::Title);
        assert_eq!(p.album_sort, Some((SongCol::Title, true)));
        frame(&ctx, &mut p);
        let duet = p.lib.track(&c1).unwrap().song().album_key();
        p.go(Page::Album(duet));
        frame(&ctx, &mut p);
        assert_eq!(p.album_sort, None, "another album starts in track order");
        // the Quality column: quality() in the cell, the resolution as its tooltip; sorted by bits, then rate
        let t = p.lib.track(&a1).unwrap();
        assert_eq!((cell_text(t, SongCol::Quality), musiclib::quality_detail(t)), ("Lossless".to_string(), "16-bit/44.1 kHz".to_string()));
        p.song_sort = (SongCol::Quality, false);
        p.go(Page::AllSongs);
        frame(&ctx, &mut p);
        // the columns of song lists: chosen per kind, kept in player.json
        p.state.toggle_column("songs", "year");
        p.save_state();
        frame(&ctx, &mut p);
        assert!(playerstate::load(None).columns_of("songs").contains(&"year".to_string()));
        assert!(!p.state.columns_of("album").contains(&"year".to_string()));
        p.state.reset_columns("songs");

        // rename a genre from its page: the files change, the page follows the new name
        p.go(Page::Group("genres", "Jazz".into()));
        frame(&ctx, &mut p);
        p.ask_rename_group("genres", "Jazz");
        assert_eq!(p.name_dialog.as_ref().unwrap().text, "Jazz");
        assert!(p.apply_name(NameAction::RenameGroup("genres", "Jazz".into()), "  ").is_err());
        p.name_dialog = None;
        p.retag_group("genres", &["Jazz".into()], "Smooth Jazz", false).unwrap();
        match &p.confirm {
            Some(ConfirmAction::RetagGroup { text, paths, .. }) => {
                assert!(text.starts_with("Rename the genre “Jazz” to “Smooth Jazz”? The genre of 1 song changes."), "{text}");
                assert_eq!(paths, &vec![b1.clone()]);
            }
            other => panic!("{other:?}"),
        }
        rename(&ctx, &mut p, "the genre renamed", |p| p.lib.track(&b1).is_some_and(|t| t.genre == "Smooth Jazz"));
        assert_eq!(tag_of(&b1, "genre"), "Smooth Jazz");
        assert_eq!(p.page, Page::Group("genres", "Smooth Jazz".into()));
        // merge two genres: only the songs that do not have the name yet are written
        p.go(Page::Genres);
        frame(&ctx, &mut p);
        p.group_selected = vec!["Pop".into(), "Folk".into()];
        p.ask_merge_groups("genres", p.group_selected.clone());
        assert_eq!(p.name_dialog.as_ref().unwrap().choices, vec!["Pop".to_string(), "Folk".to_string()], "the biggest first");
        p.name_dialog = None;
        p.retag_group("genres", &["Pop".into(), "Folk".into()], "Pop", true).unwrap();
        rename(&ctx, &mut p, "the genres merged", |p| p.lib.genres.len() == 2);
        assert_eq!(tag_of(&c1, "genre"), "Pop");
        assert!(p.group_selected.is_empty());
        assert!(p.retag_group("genres", &["Pop".into()], "Pop", true).is_err(), "nothing to change");
        // a year: only the year of each release date changes
        p.retag_group("years", &["2001".into()], "2004", false).unwrap();
        rename(&ctx, &mut p, "the year renamed", |p| p.lib.track(&a2).is_some_and(|t| t.year == "2004"));
        assert_eq!((tag_of(&a1, "date"), tag_of(&a2, "date")), ("2004-05-03".to_string(), "2004".to_string()));
        assert!(p.retag_group("years", &["2004".into()], "04", false).is_err());
        // a composer
        p.retag_group("composers", &["Ann".into()], "Ann Lind", false).unwrap();
        rename(&ctx, &mut p, "the composer renamed", |p| p.lib.track(&c1).is_some_and(|t| t.composer == "Ann Lind"));
        p.tag_job = None;

        // the Equalizer: a preset saved while the equalizer was off, chosen with it on, deleted
        p.state.eq.apply_preset("Rock");
        p.state.eq.enabled = false;
        p.apply_name(NameAction::SaveEqPreset, "Quiet Rock").unwrap();
        p.state.eq.enabled = true;
        assert_eq!(p.state.matching_eq_preset(), "Quiet Rock");
        let before = p.state.eq.clone();
        p.eq_open = true;
        frame(&ctx, &mut p);
        assert!(p.eq_delete(None), "the Delete… button");
        assert_eq!(p.confirm, Some(ConfirmAction::DeleteEqPreset("Quiet Rock".into())));
        frame(&ctx, &mut p);
        let action = p.confirm.take().unwrap();
        p.confirmed(action);
        assert!(p.state.eq_presets.is_empty() && playerstate::load(None).eq_presets.is_empty());
        assert_eq!(p.state.eq, before, "the equalizer keeps its settings");
        assert_eq!(p.state.matching_eq_preset(), "");
        // set to none of the user's presets: Delete… lists them
        p.apply_name(NameAction::SaveEqPreset, "Mine").unwrap();
        p.state.eq.apply_preset("Jazz");
        assert!(!p.eq_delete(None));
        frame(&ctx, &mut p);
        assert!(p.eq_delete(Some("Mine".into())));
        let action = p.confirm.take().unwrap();
        p.confirmed(action);
        assert!(playerstate::load(None).eq_presets.is_empty());
        p.eq_open = false;
        let _ = a2;
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// A click with Ctrl (Cmd on macOS) held: selects a genre card without select mode.
    fn command_click(ctx: &egui::Context, p: &mut Player, at: egui::Pos2) {
        let m = egui::Modifiers::COMMAND;
        frame_with(ctx, p, vec![egui::Event::PointerMoved(at)]);
        frame_with(ctx, p, vec![egui::Event::ModifiersChanged(m)]);
        for pressed in [true, false] {
            let e = egui::Event::PointerButton { pos: at, button: egui::PointerButton::Primary, pressed, modifiers: m };
            frame_with(ctx, p, vec![egui::Event::ModifiersChanged(m), e]);
        }
        frame_with(ctx, p, vec![egui::Event::ModifiersChanged(egui::Modifiers::NONE), egui::Event::PointerMoved(pos2(5.0, 835.0))]);
    }

    /// Every text drawn and where, after a few frames of a window `width` wide.
    fn placed_texts_at(ctx: &egui::Context, p: &mut Player, width: f32, events: Vec<egui::Event>) -> Vec<(String, Rect)> {
        fn walk(shape: &egui::Shape, out: &mut Vec<(String, Rect)>) {
            match shape {
                egui::Shape::Text(t) => out.push((t.galley.text().to_string(), t.visual_bounding_rect())),
                egui::Shape::Vec(v) => v.iter().for_each(|s| walk(s, out)),
                _ => {}
            }
        }
        let screen = Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(width, 840.0)));
        let mut events = Some(events);
        let mut out = vec![];
        for i in 0..4 {
            let input = egui::RawInput { screen_rect: screen, events: if i == 0 { events.take().unwrap() } else { vec![] }, ..Default::default() };
            let output = ctx.run_ui(input, |ui| p.ui(ui));
            if i == 3 {
                output.shapes.iter().for_each(|s| walk(&s.shape, &mut out));
            }
            output.drop_without_applying_deltas();
        }
        out
    }

    /// The texts equal to `text`, where they were drawn.
    fn all_texts(texts: &[(String, Rect)], text: &str) -> Vec<Rect> {
        texts.iter().filter(|(t, _)| t == text).map(|(_, r)| *r).collect()
    }

    /// 2.3: the Library page as a list (the setting, the buttons by the title), choosing
    /// genres to merge or rename (Select, "N selected", Merge…, Rename…, Done, Esc,
    /// Ctrl/Cmd-click), and Your Stats with the day's plays beside the calendar.
    #[test]
    fn library_list_choosing_genres_and_the_day_beside_the_calendar() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        realhires::i18n::set_thread_language("English");
        let tmp = std::env::temp_dir().join(format!("rhr-player-23-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        let a1 = tagged(&music, "Paper Lanterns", "Lanterns Over Water", "Mira Calloway", "Pop", "Ann", "2001", 1);
        let a2 = tagged(&music, "Paper Lanterns", "Paper in the Rain", "Mira Calloway", "Pop", "Ann", "2001", 2);
        let b1 = tagged(&music, "Harbour Lights", "Tidewater", "The Northern Quay", "Jazz", "", "1999", 1);
        let c1 = tagged(&music, "Two Rivers", "Where the Rivers Meet", "Mira Calloway", "Folk", "Ann", "2005", 1);
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg.clone(), vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 4 && p.lib_rx.is_none());

        // ---- the Library page: tiles by default, the entries in their order
        assert_eq!(p.library_layout(), "Tiles");
        let pages: Vec<Page> = p.library_entries().into_iter().map(|e| e.page).collect();
        assert_eq!(
            pages,
            [
                Page::AllSongs, Page::Folders(None), Page::Albums, Page::Artists, Page::AlbumArtists, Page::Genres,
                Page::Years, Page::Composers, Page::Moods, Page::Playlists, Page::Liked, Page::Disliked,
                Page::RecentlyPlayed, Page::Recent, Page::Stats,
            ]
        );
        p.go(Page::Library);
        let t = placed_texts(&ctx, &mut p);
        let (songs, folders) = (last_text(&t, "All Songs"), last_text(&t, "Folders"));
        assert!(folders.x > songs.x + 100.0 && (folders.y - songs.y).abs() < 4.0, "tiles side by side: {songs:?} {folders:?}");
        // the list button: one row per entry, 44 px each, saved at once
        let list = ctx.read_response(crate::library_pages::layout_button_id("List")).expect("the list button").rect;
        let tiles = ctx.read_response(crate::library_pages::layout_button_id("Tiles")).expect("the tiles button").rect;
        assert!(tiles.right() <= list.left() && list.right() > 1100.0, "{tiles:?} {list:?}");
        click(&ctx, &mut p, list.center());
        assert_eq!(p.scanner.cfg.library_layout, "List");
        assert_eq!(realhires::settings::load(None).library_layout, "List", "saved at once");
        let t = placed_texts(&ctx, &mut p);
        let names = ["All Songs", "Folders", "Albums", "Artists", "Album Artists", "Genres", "Years", "Composers", "Moods", "Playlists", "Liked Songs", "Disliked Songs", "Recently Played", "Recently Added", "Your Stats"];
        // the row (the page's first such text: "Recently Added" is also the albums' heading under the list)
        let rows: Vec<egui::Pos2> = names
            .iter()
            .map(|n| all_texts(&t, n).into_iter().filter(|r| r.left() > 240.0).map(|r| r.center()).min_by(|a, b| a.y.total_cmp(&b.y)).unwrap())
            .collect();
        for w in rows.windows(2) {
            assert!((w[1].y - w[0].y - crate::library_pages::LIST_ROW_H).abs() < 4.0 && w[1].x < w[0].x + 60.0, "{rows:?}");
        }
        assert!(t.iter().any(|(s, r)| s == "4 songs" && r.right() > 1100.0), "the count on the right");
        // a row opens its page
        click(&ctx, &mut p, last_text(&t, "Genres"));
        assert_eq!(p.page, Page::Genres);
        // a Settings page being edited follows the buttons
        p.go(Page::Settings);
        frame(&ctx, &mut p);
        let _ = p.settings_draft();
        p.set_library_layout("Tiles");
        assert_eq!(p.settings_draft().library_layout, "Tiles");
        assert_eq!(p.scanner.cfg.library_layout, "Tiles");
        assert_eq!(realhires::settings::load(None).library_layout, "Tiles");
        p.set_library_layout("Grid");
        assert_eq!(p.scanner.cfg.library_layout, "Tiles", "not a layout");

        // ---- Genres: Select, then a click selects instead of opening
        p.go(Page::Genres);
        let t = placed_texts(&ctx, &mut p);
        assert!(t.iter().any(|(s, _)| s.contains("Press Select or Ctrl-click genres to merge or rename them") || s.contains("Cmd-click")));
        click(&ctx, &mut p, last_text(&t, "Select"));
        assert!(p.group_select_mode && p.group_selected.is_empty());
        let t = placed_texts(&ctx, &mut p);
        assert!(t.iter().any(|(s, _)| s == "0 selected") && t.iter().any(|(s, _)| s.contains("Click genres to select them")));
        let card = |t: &[(String, Rect)], name: &str| last_text(t, name) - vec2(0.0, 80.0); // on the cover above the name
        click(&ctx, &mut p, card(&t, "Jazz"));
        assert_eq!((p.page.clone(), p.group_selected.clone()), (Page::Genres, vec!["Jazz".to_string()]));
        let t = placed_texts(&ctx, &mut p);
        assert!(t.iter().any(|(s, _)| s == "1 selected"));
        click(&ctx, &mut p, last_text(&t, "Merge…")); // one genre: Merge… is off
        assert!(p.name_dialog.is_none());
        click(&ctx, &mut p, card(&t, "Pop"));
        click(&ctx, &mut p, card(&t, "Folk"));
        click(&ctx, &mut p, card(&t, "Folk")); // again: unselected
        assert_eq!(p.group_selected, vec!["Jazz".to_string(), "Pop".to_string()]);
        let t = placed_texts(&ctx, &mut p);
        assert!(t.iter().any(|(s, _)| s == "2 selected"));
        click(&ctx, &mut p, last_text(&t, "Rename…")); // two genres: Rename… is off
        assert!(p.name_dialog.is_none());
        // Merge…: the name in one dialog, the selected genres to choose from (the biggest first)
        click(&ctx, &mut p, last_text(&t, "Merge…"));
        let d = p.name_dialog.as_ref().expect("the name dialog");
        assert_eq!((d.choices.clone(), d.text.as_str()), (vec!["Pop".to_string(), "Jazz".to_string()], "Pop"));
        p.name_dialog = None;
        // Done: out of select mode, nothing selected
        let t = placed_texts(&ctx, &mut p);
        click(&ctx, &mut p, last_text(&t, "Done"));
        assert!(!p.group_select_mode && p.group_selected.is_empty());
        // Esc leaves select mode as Done does
        p.group_select_mode = true;
        p.group_selected = vec!["Jazz".into()];
        frame_with(&ctx, &mut p, vec![egui::Event::Key { key: egui::Key::Escape, physical_key: None, pressed: true, repeat: false, modifiers: egui::Modifiers::NONE }]);
        assert!(!p.group_select_mode && p.group_selected.is_empty());
        // Ctrl/Cmd-click selects without select mode, and the title row shows the selection's buttons
        let t = placed_texts(&ctx, &mut p);
        command_click(&ctx, &mut p, card(&t, "Folk"));
        command_click(&ctx, &mut p, card(&t, "Jazz"));
        assert!(!p.group_select_mode && p.page == Page::Genres);
        assert_eq!(p.group_selected, vec!["Folk".to_string(), "Jazz".to_string()]);
        let t = placed_texts(&ctx, &mut p);
        assert!(t.iter().any(|(s, _)| s == "2 selected") && !t.iter().any(|(s, _)| s == "Select"));
        // the merge itself: the files change, the selection goes
        click(&ctx, &mut p, last_text(&t, "Merge…"));
        assert!(p.name_dialog.is_some());
        p.name_dialog = None;
        p.apply_name(NameAction::MergeGroups("genres", p.group_selected.clone()), "Jazz").unwrap();
        rename(&ctx, &mut p, "the genres merged", |p| p.lib.genres.len() == 2);
        assert_eq!(tag_of(&c1, "genre"), "Jazz");
        assert!(p.group_selected.is_empty() && !p.group_select_mode);
        p.tag_job = None;
        // Rename… with one genre
        let t = placed_texts(&ctx, &mut p);
        click(&ctx, &mut p, last_text(&t, "Select"));
        let t = placed_texts(&ctx, &mut p);
        click(&ctx, &mut p, card(&t, "Pop"));
        let t = placed_texts(&ctx, &mut p);
        click(&ctx, &mut p, last_text(&t, "Rename…"));
        let d = p.name_dialog.as_ref().expect("the rename dialog");
        assert_eq!((d.title.as_str(), d.text.as_str()), ("Rename genre", "Pop"));
        p.name_dialog = None;
        // the page shown again starts without a selection
        p.go(Page::Library);
        p.go(Page::Genres);
        assert!(!p.group_select_mode && p.group_selected.is_empty());

        // ---- Your Stats: the day's plays to the right of the calendar, scrolling inside it
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64();
        let today = realhires::stats::local_parts(now);
        let (day_start, _) = realhires::stats::day_bounds(today.year, today.month, today.day);
        for i in 0..30 {
            let path = [&a1, &a2, &b1, &c1][i % 4];
            let track = p.lib.track(path).unwrap().clone();
            // between midnight and now: today, and in this year's stats
            let started = day_start + (now - day_start) * (i as f64 + 1.0) / 32.0;
            assert!(p.history.as_ref().unwrap().add_play(&track, started, 30.0 + i as f64));
        }
        p.history_gen += 1;
        p.calendar.month = (today.year, today.month);
        p.calendar.day = None;
        p.go(Page::Stats);
        let t = placed_texts(&ctx, &mut p);
        let month = last_text(&t, &realhires::stats::month_title(today.year, today.month));
        let hint = last_text(&t, "Click a day to see the songs you played on it.");
        assert!(hint.x > month.x + 7.0 * 62.0 && (hint.y - month.y).abs() < 20.0, "the hint beside the calendar: {month:?} {hint:?}");
        p.calendar_choose(today.day);
        let t = placed_texts(&ctx, &mut p);
        let title = last_text(&t, &realhires::stats::day_title(today.year, today.month, today.day));
        assert!(title.x > month.x + 7.0 * 62.0 && (title.y - month.y).abs() < 20.0, "{month:?} {title:?}");
        assert_eq!(p.calendar.rows.len(), 30);
        // the cards start under the calendar, not under the 30 plays
        let weeks = realhires::stats::month_grid(today.year, today.month).len() as f32;
        let cards = last_text(&t, "Minutes listened");
        assert!(cards.y < month.y + 64.0 + 44.0 * weeks + 80.0, "{month:?} {cards:?}");
        // the newest play is the first row; a wheel step over the list moves it, not the calendar
        let first = &p.calendar.rows[0].title.clone();
        let row = all_texts(&t, first).into_iter().filter(|r| r.left() > month.x + 7.0 * 62.0).map(|r| r.center()).next().unwrap();
        let wheel = egui::Event::MouseWheel {
            unit: egui::MouseWheelUnit::Point,
            delta: vec2(0.0, -200.0),
            phase: egui::TouchPhase::Move,
            source: egui::MouseWheelSource::Wheel,
            modifiers: egui::Modifiers::NONE,
        };
        let t = placed_texts_at(&ctx, &mut p, 1280.0, vec![egui::Event::PointerMoved(row), wheel]);
        let month2 = last_text(&t, &realhires::stats::month_title(today.year, today.month));
        let row2 = all_texts(&t, first).into_iter().filter(|r| r.left() > month.x + 7.0 * 62.0).map(|r| r.center()).next().unwrap();
        assert!((month2.y - month.y).abs() < 0.5 && row2.y < row.y - 50.0, "{month:?} {month2:?} {row:?} {row2:?}");
        // a page narrower than the calendar plus 360 px: the list goes under the calendar
        let t = placed_texts_at(&ctx, &mut p, 900.0, vec![]);
        let month = all_texts(&t, &realhires::stats::month_title(today.year, today.month))[0];
        let title = all_texts(&t, &realhires::stats::day_title(today.year, today.month, today.day))[0];
        assert!(title.top() > month.top() + 64.0 + 44.0 * (weeks - 1.0) && (title.left() - month.left()).abs() < 10.0, "{month:?} {title:?}");
        assert!(crate::library_pages::calendar_side_by_side(7.0 * 62.0 + 360.0) && !crate::library_pages::calendar_side_by_side(7.0 * 62.0 + 359.0));
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// 5,000 invented songs on 500 albums (no files: the tracks are made directly).
    pub fn invented_tracks() -> Vec<Track> {
        let genres = ["Ambient", "Indie Pop", "Jazz", "Electronic", "Rock", "Classical", "Folk", "Soul", "Hip-Hop", "Metal"];
        let mut out = vec![];
        for a in 0..500usize {
            let artist = format!("Invented Artist {}", a % 120);
            let album = format!("Invented Album {a}");
            let lossy = a % 7 == 0;
            for n in 0..10usize {
                out.push(Track {
                    path: format!("/invented/Music/{artist}/{album}/{:02} Song {a}-{n}.{}", n + 1, if lossy { "mp3" } else { "flac" }),
                    title: format!("Song {a}-{n} of the invented library"),
                    artist: artist.clone(),
                    album: album.clone(),
                    album_artist: artist.clone(),
                    genre: genres[a % genres.len()].to_string(),
                    composer: format!("Composer {}", (a + n) % 60),
                    disc: 1,
                    track: n as i64 + 1,
                    year: (1970 + a % 45).to_string(),
                    duration: 120.0 + ((a * 7 + n * 13) % 200) as f64,
                    codec: if lossy { "mp3".into() } else { "flac".into() },
                    sample_rate: [44_100, 48_000, 96_000, 192_000][a % 4],
                    bits: if lossy { 0 } else { [16, 24, 24, 32][a % 4] },
                    is_lossy: lossy,
                    channels: 2,
                    bit_rate: if lossy { 320_000 } else { 900_000 },
                    size: 1000,
                    mtime_ns: (a * 1000 + n) as i64,
                    ..Default::default()
                });
            }
        }
        out
    }

    /// One frame with a mouse-wheel step over the page, tessellated as the window would.
    fn scroll_frame(ctx: &egui::Context, p: &mut Player, dy: f32) {
        let mut events = vec![egui::Event::PointerMoved(pos2(760.0, 520.0))];
        if dy != 0.0 {
            events.push(egui::Event::MouseWheel {
                unit: egui::MouseWheelUnit::Point,
                delta: vec2(0.0, dy),
                phase: egui::TouchPhase::Move,
                source: egui::MouseWheelSource::Wheel,
                modifiers: egui::Modifiers::NONE,
            });
        }
        let input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1280.0, 840.0))),
            events,
            ..Default::default()
        };
        let mut out = ctx.run_ui(input, |ui| p.ui(ui));
        let shapes = std::mem::take(&mut out.shapes);
        let ppp = out.pixels_per_point;
        out.drop_without_applying_deltas();
        let _ = ctx.tessellate(shapes, ppp);
    }

    /// Frame times while scrolling the pages of a big library (run with
    /// `cargo test --release -p realhires-gui profile_big_library -- --ignored --nocapture`).
    #[test]
    #[ignore]
    fn profile_big_library() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-profile-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(&tmp).unwrap();
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        std::env::set_var("REALHIRES_COVERS", tmp.join("covers"));
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, Settings::default(), vec![]);
        let t0 = Instant::now();
        p.lib = LibView::new(invented_tracks());
        println!("LibView::new (5,000 songs, 500 albums): {:.0} ms", t0.elapsed().as_secs_f64() * 1000.0);
        let pages = [
            ("Library", Page::Library),
            ("All Songs", Page::AllSongs),
            ("Albums", Page::Albums),
            ("Artists", Page::Artists),
            ("Genres", Page::Genres),
            ("Composers", Page::Composers),
            ("Years", Page::Years),
        ];
        for (name, page) in pages {
            p.go(page);
            for _ in 0..5 {
                scroll_frame(&ctx, &mut p, 0.0);
            }
            let mut ms = vec![];
            for i in 0..60 {
                let t = Instant::now();
                scroll_frame(&ctx, &mut p, if i % 20 < 10 { -60.0 } else { 60.0 });
                ms.push(t.elapsed().as_secs_f64() * 1000.0);
            }
            let mean = ms.iter().sum::<f64>() / ms.len() as f64;
            let max = ms.iter().cloned().fold(0.0, f64::max);
            println!("{name:>10}: frame while scrolling: mean {mean:6.2} ms, max {max:6.2} ms");
        }
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn musicbrainz_window_from_the_album_page() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-mb-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        let titles = [("Neon Streets", 201.0), ("Midnight Signal", 245.0), ("Harbour Lights", 187.0), ("Last Train Home", 263.0)];
        let paths: Vec<String> = titles
            .iter()
            .enumerate()
            .map(|(i, (t, secs))| song(&music, "Night Drive", t, "The Example Band", "Pop", i as u32 + 1, 8_000, *secs))
            .collect();
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 4 && p.lib_rx.is_none());
        // the switch is off by default: a library update asks MusicBrainz nothing
        assert!(!p.scanner.cfg.musicbrainz_auto);
        assert!(!p.lookup_library(false) && !p.musicbrainz_busy());
        // never musicbrainz.org: each window gets the test client right after the click
        // that opens it, and this server (nothing listens there) would only fail
        p.scanner.cfg.musicbrainz_server = "http://127.0.0.1:9".into();
        let album = p.lib.albums.iter().find(|a| a.title == "Night Drive").cloned().unwrap();
        let album_paths: Vec<String> = album.tracks.iter().map(|t| t.path.clone()).collect();
        assert_eq!(album_paths, paths, "in track order");

        // 2.2: an album card's right-click menu, as the user asked ("right click songs/album
        // and press acquire metadata")
        let (items, after) = album_card_menu(&ctx, &mut p, "Night Drive");
        assert_eq!(items, ["Play", "Shuffle", "Play Next", "Add to Queue", "Album Properties…", "Acquire Metadata…", "Change Cover…", "Colour Label"]);
        click_only(&ctx, &mut p, last_text(&after, "Acquire Metadata…"));
        p.mb_win.as_mut().expect("Acquire Metadata… opens the MusicBrainz window").set_client(crate::mbwin::test_client());
        {
            let w = p.mb_win.as_ref().unwrap();
            assert!(w.album, "for the album");
            assert_eq!((w.paths.clone(), w.heading.as_str()), (paths.clone(), "Night Drive"));
        }
        run_until(&ctx, &mut p, "the release from the card", |p| p.mb_win.as_ref().is_some_and(|w| w.detail.is_some() && !w.busy));
        assert_eq!(p.mb_win.as_ref().unwrap().query_title, "Night Drive");
        assert_eq!(p.mb_win.as_ref().unwrap().detail.as_ref().unwrap().release.id, "rel-night-2001");
        p.mb_win = None;
        frame(&ctx, &mut p);
        // Album Properties… opens Properties for all of the album's songs
        let (_, after) = album_card_menu(&ctx, &mut p, "Night Drive");
        click_only(&ctx, &mut p, last_text(&after, "Album Properties…"));
        {
            let e = p.edit_info.as_ref().expect("Album Properties… opens Properties");
            assert_eq!((e.paths.clone(), e.heading.as_str()), (paths.clone(), "Night Drive"));
        }
        p.edit_info = None;
        frame(&ctx, &mut p);

        // a song's right-click menu: Acquire Metadata… for that song (song tables select one song)
        p.go(Page::Album(album.key.clone()));
        let before = placed_texts(&ctx, &mut p);
        let row = before.iter().find(|(t, _)| t == "Harbour Lights").expect("the song's row").1.center();
        right_click(&ctx, &mut p, row);
        let after = placed_texts(&ctx, &mut p);
        let items = new_texts(&before, &after);
        assert!(items.contains(&"Acquire Metadata…".to_string()) && items.contains(&"Properties…".to_string()), "{items:?}");
        assert!(!items.iter().any(|t| t.contains("MusicBrainz")), "the old label is gone: {items:?}");
        let pos = items.iter().position(|t| t == "Acquire Metadata…").unwrap();
        assert_eq!(items[pos - 1], "Properties…", "after Properties…");
        click_only(&ctx, &mut p, last_text(&after, "Acquire Metadata…"));
        p.mb_win.as_mut().expect("the song's MusicBrainz window").set_client(crate::mbwin::test_client());
        {
            let w = p.mb_win.as_ref().unwrap();
            assert!(!w.album, "for one song");
            assert_eq!((w.paths.clone(), w.heading.as_str()), (vec![paths[2].clone()], "Harbour Lights"));
        }
        // one MusicBrainz window at a time
        p.musicbrainz_album(&album);
        assert!(!p.mb_win.as_ref().unwrap().album, "still the song's window");
        p.mb_win = None;
        frame(&ctx, &mut p);

        // the album page's button, Acquire Metadata… (as "Get album info from MusicBrainz…" did)
        p.go(Page::Album(album.key.clone()));
        let shown = placed_texts(&ctx, &mut p);
        assert!(!shown.iter().any(|(t, _)| t.contains("MusicBrainz")), "the old label is gone");
        click_only(&ctx, &mut p, last_text(&shown, "Acquire Metadata…"));
        p.mb_win.as_mut().expect("the MusicBrainz window").set_client(crate::mbwin::test_client());
        run_until(&ctx, &mut p, "the release", |p| p.mb_win.as_ref().is_some_and(|w| w.detail.is_some() && !w.busy));
        {
            let w = p.mb_win.as_mut().unwrap();
            assert!(w.album && w.query_title == "Night Drive");
            assert_eq!(w.detail.as_ref().unwrap().release.id, "rel-night-2001");
            assert_eq!(w.changes().len(), 4);
            w.only_empty = true;
            w.update_preview();
            assert!(!w.lines.iter().any(|(l, _)| l.contains("Genre:")), "the genre is filled");
            assert!(w.save());
        }
        run_until(&ctx, &mut p, "saving and the library update", |p| p.mb_win.is_none() && p.lib_rx.is_none());
        let tags = realhires::probe::probe(&paths[2]).unwrap().tags;
        let tag = |k: &str| realhires::probe::tag(&tags, k).unwrap_or("").to_string();
        assert_eq!((tag("album_artist").as_str(), tag("date").as_str(), tag("genre").as_str()), ("The Example Band", "1999-05-28", "Pop"));
        let t = p.lib.track(&paths[2]).unwrap();
        assert_eq!((t.album_artist.as_str(), t.year.as_str()), ("The Example Band", "1999"));

        // the card menu's Play, Add to Queue and Play Next: the album's songs in track order
        let (_, after) = album_card_menu(&ctx, &mut p, "Night Drive");
        click_only(&ctx, &mut p, last_text(&after, "Play"));
        assert_eq!(p.now.as_ref().map(|n| n.path.clone()), Some(paths[0].clone()), "Play starts with the first song");
        assert_eq!(p.queue_name, "Night Drive");
        let (_, after) = album_card_menu(&ctx, &mut p, "Night Drive");
        click_only(&ctx, &mut p, last_text(&after, "Add to Queue"));
        assert_eq!(p.queue.as_ref().unwrap().queued, paths);
        p.queue.as_mut().unwrap().queued = vec![paths[3].clone()];
        let (_, after) = album_card_menu(&ctx, &mut p, "Night Drive");
        click_only(&ctx, &mut p, last_text(&after, "Play Next"));
        let want: Vec<String> = paths.iter().chain([&paths[3]]).cloned().collect();
        assert_eq!(p.queue.as_ref().unwrap().queued, want, "Play Next comes before the songs queued earlier");
        p.engine.stop();
        frame(&ctx, &mut p);
        p.on_exit();
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// 2.3: Change Cover… in a song's menu, an album card's menu and the album page's
    /// "…" menu, Search Cover Online in the Cover window, and the Properties window's
    /// Acquire Metadata… and Search buttons with the clipboard watch (the browser and the
    /// clipboard are `crate::web`'s fakes: nothing is opened, nothing real is read).
    #[test]
    fn change_cover_search_online_and_properties_search() {
        use crate::web::fake;
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let tmp = std::env::temp_dir().join(format!("rhr-player-web-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        std::env::set_var("REALHIRES_COVERS", tmp.join("covers"));
        let paths: Vec<String> = [("Neon Streets", 2.0), ("Midnight Signal", 2.0)]
            .iter()
            .enumerate()
            .map(|(i, (t, secs))| song(&music, "Night Drive", t, "The Example Band", "Pop", i as u32 + 1, 8_000, *secs))
            .collect();
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 2 && p.lib_rx.is_none());
        p.scanner.cfg.musicbrainz_server = "http://127.0.0.1:9".into(); // never musicbrainz.org
        let album = p.lib.albums.iter().find(|a| a.title == "Night Drive").cloned().unwrap();
        fake::copy("what was copied before");

        // 1. a song's menu: Properties…, Acquire Metadata…, Change Cover… (the Cover window of
        // the song's album)
        p.go(Page::Album(album.key.clone()));
        let before = placed_texts(&ctx, &mut p);
        let row = before.iter().find(|(t, _)| t == "Midnight Signal").expect("the song's row").1.center();
        right_click(&ctx, &mut p, row);
        let after = placed_texts(&ctx, &mut p);
        let items = new_texts(&before, &after);
        let i = items.iter().position(|t| t == "Properties…").unwrap_or_else(|| panic!("{items:?}"));
        assert_eq!(items[i..i + 3], ["Properties…", "Acquire Metadata…", "Change Cover…"], "{items:?}");
        assert!(!items.iter().any(|t| t == "Cover…"), "{items:?}");
        click_only(&ctx, &mut p, last_text(&after, "Change Cover…"));
        {
            let w = p.cover_win.as_ref().expect("Change Cover… opens the Cover window");
            assert_eq!((w.path.as_str(), w.target_paths()), (paths[1].as_str(), paths.clone()));
        }
        // Search Cover Online, right after Change cover…: Google Images for the album
        let shown = placed_texts(&ctx, &mut p);
        let (change, search) = (last_text(&shown, "Change cover…"), last_text(&shown, "Search Cover Online"));
        assert!(search.x > change.x && (search.y - change.y).abs() < 2.0, "right after Change cover…");
        assert!(!shown.iter().any(|(t, r)| t == "Save image as…" && r.center().x < search.x), "before Save image as…");
        click_only(&ctx, &mut p, search);
        assert_eq!(fake::opened(), ["https://www.google.com/search?tbm=isch&q=The+Example+Band+Night+Drive"]);
        p.cover_win = None;
        frame(&ctx, &mut p);
        // an album card's menu and the album page's "…" menu
        let (items, after) = album_card_menu(&ctx, &mut p, "Night Drive");
        assert_eq!(items, ["Play", "Shuffle", "Play Next", "Add to Queue", "Album Properties…", "Acquire Metadata…", "Change Cover…", "Colour Label"]);
        click_only(&ctx, &mut p, last_text(&after, "Change Cover…"));
        assert_eq!(p.cover_win.as_ref().map(|w| w.target_paths()), Some(paths.clone()));
        p.cover_win = None;
        p.go(Page::Album(album.key.clone()));
        let before = placed_texts(&ctx, &mut p);
        let props = before.iter().rev().find(|(t, _)| t == "Acquire Metadata…").expect("the page's button").1;
        // the "…" button right after the page's buttons
        let mut after = vec![];
        for dx in (8..90).step_by(6) {
            click_only(&ctx, &mut p, pos2(props.right() + dx as f32, props.center().y));
            after = placed_texts(&ctx, &mut p);
            if new_texts(&before, &after).iter().any(|t| t == "Change Cover…") {
                break;
            }
            // still on the Acquire Metadata… button (its padding): that opened MusicBrainz
            p.mb_win = None;
            frame_with(&ctx, &mut p, vec![egui::Event::PointerMoved(pos2(5.0, 835.0))]);
        }
        assert_eq!(new_texts(&before, &after), ["Play", "Shuffle", "Play Next", "Add to Queue", "Album Properties…", "Acquire Metadata…", "Change Cover…", "Colour Label"]);
        click_only(&ctx, &mut p, last_text(&after, "Change Cover…"));
        assert!(p.cover_win.is_some(), "the page's menu opens the Cover window");
        p.cover_win = None;
        frame(&ctx, &mut p);

        // 12. Properties of one song: the Search buttons and Acquire Metadata…
        p.edit_song_info(&paths[1]);
        run_until(&ctx, &mut p, "the tags", |p| p.edit_info.as_ref().is_some_and(|e| e.editing()));
        let e = p.edit_info.as_ref().unwrap();
        assert_eq!(e.search_url("composer"), "https://www.google.com/search?q=%22Midnight+Signal%22+The+Example+Band+composer");
        assert_eq!(e.search_url("date"), "https://www.google.com/search?q=%22Midnight+Signal%22+The+Example+Band+release+date");
        // the composer's Search button, at the right end of its field
        let shown = placed_texts(&ctx, &mut p);
        let composer = last_text(&shown, "Composer");
        let button = p.edit_info.as_ref().unwrap().search_rects[FIELDS_COMPOSER];
        assert!((button.center().y - composer.y).abs() < 12.0, "on the composer's row");
        click_only(&ctx, &mut p, button.center());
        assert_eq!(fake::opened().last().unwrap(), "https://www.google.com/search?q=%22Midnight+Signal%22+The+Example+Band+composer");
        {
            let e = p.edit_info.as_mut().unwrap();
            assert_eq!(e.watch.as_ref().map(|w| (w.field, w.start.as_str())), Some((FIELDS_COMPOSER, "what was copied before")));
            assert_eq!(e.note, crate::editinfo::WATCHING);
            assert_eq!(e.watch_step(Instant::now()), None, "nothing new copied yet");
        }
        assert!(texts(&ctx, &mut p).contains("Copy the answer in your browser and it will be filled in here."));
        fake::copy("  \n ");
        assert_eq!(p.edit_info.as_mut().unwrap().watch_step(Instant::now()), None, "an empty copy is not an answer");
        fake::copy("  John   Williams \nfilm composer");
        run_until(&ctx, &mut p, "the copied text filled in", |p| p.edit_info.as_ref().unwrap().watch.is_none());
        {
            let e = p.edit_info.as_ref().unwrap();
            assert_eq!(e.text[FIELDS_COMPOSER], "John Williams");
            assert_eq!(e.filled.map(|(f, _)| f), Some(FIELDS_COMPOSER));
            assert_eq!(e.changes(), vec![("composer".to_string(), "John Williams".to_string())]);
        }
        assert!(texts(&ctx, &mut p).contains("Filled in from the clipboard."));
        let tags = realhires::probe::probe(&paths[1]).unwrap().tags;
        assert_eq!(realhires::probe::tag(&tags, "composer"), None, "not saved before Save");
        // another field's Search replaces the watch, which ends by itself after two minutes
        {
            let e = p.edit_info.as_mut().unwrap();
            e.search(5);
            assert_eq!(e.watch.as_ref().map(|w| w.field), Some(5));
            e.search(4);
            assert_eq!(e.watch.as_ref().map(|w| w.field), Some(4));
            fake::copy("Jazz");
            let later = Instant::now() + Duration::from_secs(realhires::tagedit::WATCH_SECONDS + 1);
            assert_eq!(e.watch_step(later), None);
            assert!(e.watch.is_none() && e.note.is_empty() && e.text[4] == "Pop", "nothing after two minutes");
        }
        // in Japanese the tooltips and the searches name the field in Japanese
        realhires::i18n::set_thread_language("ja");
        assert!(p.edit_info.as_ref().unwrap().search_url("composer").ends_with("+%E4%BD%9C%E6%9B%B2%E8%80%85"));
        realhires::i18n::set_thread_language("");
        // MusicBrainz (or anyone) writes into the song: the fields nothing was typed into follow
        p.edit_info.as_mut().unwrap().text[0] = "Midnight Signal (typed)".into();
        let changes = vec![("genre".to_string(), "Synthwave".to_string())];
        let out = realhires::tagedit::save_tags(&paths[1..], &changes, None, |_, _, _| {});
        assert!(out.iter().all(|o| o.ok));
        p.edit_info.as_mut().unwrap().files_changed(&paths[1..]);
        run_until(&ctx, &mut p, "the fields read again", |p| p.edit_info.as_ref().unwrap().text[4] == "Synthwave");
        {
            let e = p.edit_info.as_ref().unwrap();
            assert_eq!((e.text[0].as_str(), e.text[8].as_str()), ("Midnight Signal (typed)", "John Williams"));
            assert_eq!(e.changes().len(), 2, "{:?}", e.changes());
        }
        // Acquire Metadata… at the bottom left: the MusicBrainz window for the same song, over this one
        p.edit_info.as_mut().unwrap().search(1);
        let shown = placed_texts(&ctx, &mut p);
        let acquire = last_text(&shown, "Acquire Metadata…");
        let save = last_text(&shown, "Save");
        assert!(acquire.x < save.x - 200.0 && acquire.y > composer.y, "at the bottom left");
        click_only(&ctx, &mut p, acquire);
        p.mb_win.as_mut().expect("Acquire Metadata… opens the MusicBrainz window").set_client(crate::mbwin::test_client());
        {
            let w = p.mb_win.as_ref().unwrap();
            assert!(!w.album && w.paths == paths[1..], "{:?}", w.paths);
        }
        assert!(p.edit_info.is_some(), "Properties stays open");
        // Esc closes the MusicBrainz window only, then Properties (which ends the watch)
        let esc = || egui::Event::Key { key: egui::Key::Escape, physical_key: None, pressed: true, repeat: false, modifiers: Default::default() };
        frame_with(&ctx, &mut p, vec![esc()]);
        assert!(p.mb_win.is_none() && p.edit_info.is_some());
        frame_with(&ctx, &mut p, vec![esc()]);
        assert!(p.edit_info.is_none());
        let tags = realhires::probe::probe(&paths[1]).unwrap().tags;
        assert_eq!(realhires::probe::tag(&tags, "composer"), None, "Cancel wrote nothing");

        // Album Properties…: the album is searched for, Acquire Metadata… looks the album up
        p.edit_album_info(&album);
        run_until(&ctx, &mut p, "the album's tags", |p| p.edit_info.as_ref().is_some_and(|e| e.editing()));
        assert_eq!(
            p.edit_info.as_ref().unwrap().search_url("date"),
            "https://www.google.com/search?q=%22Night+Drive%22+The+Example+Band+release+date"
        );
        let shown = placed_texts(&ctx, &mut p);
        click_only(&ctx, &mut p, last_text(&shown, "Acquire Metadata…"));
        p.mb_win.as_mut().expect("the album's MusicBrainz window").set_client(crate::mbwin::test_client());
        assert!(p.mb_win.as_ref().unwrap().album && p.mb_win.as_ref().unwrap().paths == paths);
        p.mb_win = None;
        p.edit_info = None;
        p.engine.stop();
        frame(&ctx, &mut p);
        p.on_exit();
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// The composer's index in `tagedit::FIELDS`.
    const FIELDS_COMPOSER: usize = 8;

    /// The player in Japanese (2.1): every page, every Settings group and the
    /// windows draw Japanese texts.  Only this thread uses Japanese, so the
    /// tests that run at the same time keep English.
    #[test]
    fn japanese_pages_and_windows() {
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        realhires::i18n::set_thread_language("日本語");
        let tmp = std::env::temp_dir().join(format!("rhr-player-ja-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        // long enough to be logged in the play history (`musiclib::MIN_LOGGED_SECONDS`)
        let a1 = song(&music, "Paper Lanterns", "Lanterns Over Water", "Mira Calloway", "Pop", 1, 44_100, 2.0);
        let a2 = song(&music, "Paper Lanterns", "Paper in the Rain", "Mira Calloway", "Pop", 2, 44_100, 1.5);
        let b1 = song(&music, "Harbour Lights", "Tidewater", "The Northern Quay", "Jazz", 1, 48_000, 1.0);
        // the setting stays English: `set_language()` would switch every thread of the test run
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 3 && p.lib_rx.is_none());
        let has = |text: &str, want: &[&str], what: &str| {
            for w in want {
                assert!(text.contains(w), "{what}: no {w:?} in\n{text}");
            }
        };

        // the start page and the sidebar
        let t = texts(&ctx, &mut p);
        has(&t, &["ライブラリ", "すべての曲", "お気に入りの曲", "新規プレイリスト…", "履歴", "あなたの統計", "設定", "3曲", "アルバム2枚"], "Library");
        for english in ["All Songs", "Recently Played", "Liked Songs", "Settings", "3 songs"] {
            assert!(!t.contains(english), "English {english:?} on the Library page:\n{t}");
        }
        // every page before anything was played
        let music_dir = realhires::paths::abspath(&music.to_string_lossy());
        let key = p.lib.track(&a1).unwrap().song().album_key();
        let pages: Vec<(Page, Vec<&str>)> = vec![
            (Page::AllSongs, vec!["すべての曲", "タイトル", "アーティスト", "音質", "再生", "シャッフル", "ロスレス"]),
            (Page::Folders(None), vec!["Music", "再生"]),
            (Page::Folders(Some(music_dir.clone())), vec!["Paper Lanterns", "2曲"]),
            (Page::Albums, vec!["アルバム", "Harbour Lights"]),
            (Page::Album(key.clone()), vec!["戻る", "アルバムのプロパティ…", "メタデータを取得…", "2曲、"]),
            (Page::Artists, vec!["アーティスト", "Mira Calloway"]),
            (Page::AlbumArtists, vec!["アルバムアーティスト"]),
            (Page::Genres, vec!["ジャンル", "Jazz"]),
            (Page::Years, vec!["年", "不明な年"]),
            (Page::Composers, vec!["作曲者", "不明な作曲者"]),
            (Page::Playlists, vec!["プレイリスト", "まだプレイリストがありません"]),
            (Page::Liked, vec!["お気に入りの曲", "お気に入りにした曲がここに表示されます"]),
            (Page::Disliked, vec!["好きじゃない曲", "シャッフルでは再生されなくなります"]),
            (Page::RecentlyPlayed, vec!["最近再生した項目", "再生した曲がここに表示されます。"]),
            (Page::Recent, vec!["最近追加した項目"]),
            (Page::Stats, vec!["あなたの統計", "今年", "この期間にはまだ再生がありません"]),
            (Page::StatsDetails, vec!["詳細", "あなたのライブラリ", "サンプリング周波数", "ビット深度"]),
            (Page::NowPlaying, vec!["再生中の曲はありません", "アルバムか曲を選ぶか、再生ボタンを押してください。"]),
        ];
        for (page, want) in pages {
            p.go(page.clone());
            let t = texts(&ctx, &mut p);
            has(&t, &want, &format!("{page:?}"));
        }
        // 2.2: an album card's right-click menu
        let (items, _) = album_card_menu(&ctx, &mut p, "Harbour Lights");
        for w in ["再生", "シャッフル", "キューに追加", "メタデータを取得…", "アルバムのプロパティ…"] {
            assert!(items.iter().any(|t| t == w), "no {w:?} in the album menu: {items:?}");
        }
        egui::Popup::close_all(&ctx);
        frame(&ctx, &mut p);

        // playing: the playback bar, Now Playing, Up Next and the play history
        p.go(Page::Album(key.clone()));
        let album = p.page_songs();
        p.play_list(album.clone(), Some(&a1), "Paper Lanterns");
        p.show_up_next = true;
        p.go(Page::NowPlaying);
        let t = texts(&ctx, &mut p);
        has(&t, &["次に再生", "再生元：Paper Lanterns", "Paper in the Rain", "ロスレス"], "Up Next");
        p.show_up_next = false;
        run_until(&ctx, &mut p, "the end of the album", |p| p.now.as_ref().is_some_and(|n| !n.active));
        frame(&ctx, &mut p);
        p.play_list(vec![b1.clone()], None, "All Songs");
        p.show_up_next = true;
        let t = texts(&ctx, &mut p);
        has(&t, &["この曲には歌詞がありません。", "歌詞を取得", "再生元：すべての曲", "この曲の後に再生する曲はありません。"], "Now Playing");
        p.show_up_next = false;
        // 2.2: Now Playing's switch and its Details, a grouping grid and the Cover window
        p.set_np_tab(NpTab::Details);
        let t = texts(&ctx, &mut p);
        has(&t, &["歌詞", "次に再生", "詳細", "タイトル", "品質チェック", "まだチェックしていません"], "Now Playing, Details");
        assert!(p.np_hits.details.iter().any(|l| l.starts_with("タイトル: Tidewater")), "{:?}", p.np_hits.details);
        p.set_np_tab(NpTab::Lyrics);
        p.go(Page::Genres);
        let t = texts(&ctx, &mut p);
        has(&t, &["ジャンル", "2ジャンル", "+クリックでジャンルを選ぶと", "選択", "アルバム1枚 · 2曲"], "Genres");
        p.open_cover_window(&b1);
        let t = texts(&ctx, &mut p);
        has(&t, &["アートワーク", "アートワークを変更…", "アルバムを表示", "閉じる"], "the Cover window");
        p.cover_win = None;
        p.go(Page::NowPlaying);
        p.engine.stop();
        for (page, want) in [
            (Page::RecentlyPlayed, vec!["再生日時", "今日 ", "履歴を消去…"]),
            (Page::Stats, vec!["詳細"]),
            (Page::StatsDetails, vec!["合計", "時間帯別の再生時間（分）", "曜日別の再生時間（分）", "月", "トップソング", "回再生"]),
            (Page::Library, vec!["今年は"]),
        ] {
            p.go(page.clone());
            let t = texts(&ctx, &mut p);
            has(&t, &want, &format!("{page:?} after playing"));
        }

        // a playlist with a song whose file is gone
        p.apply_name(NameAction::NewPlaylist(vec![a1.clone(), "/gone/song.flac".to_string()]), "Road Trip").unwrap();
        let pid = p.state.playlists[0].id.clone();
        p.go(Page::Playlist(pid.clone()));
        let t = texts(&ctx, &mut p);
        has(&t, &["Road Trip", "名前を変更…", "削除…", "ライブラリにありません", "2曲、"], "a playlist");

        // search
        p.search = "lanterns".into();
        p.search_due = Some(Instant::now());
        let t = texts(&ctx, &mut p);
        assert_eq!(p.page, Page::Search);
        has(&t, &["「lanterns」の検索結果", "アルバム", "曲"], "Search");
        p.search.clear();
        p.search_due = Some(Instant::now());
        frame(&ctx, &mut p);

        // every Settings group: its name, its rows and its buttons in Japanese
        p.go(Page::Settings);
        for (i, group) in realhires::settings::GROUPS.iter().enumerate() {
            p.settings_group = i;
            let shown = realhires::i18n::tr(group);
            assert_ne!(shown, *group, "the group {group:?} has no Japanese name");
            let t = texts(&ctx, &mut p);
            has(&t, &[shown, "このグループを初期設定に戻す"], group);
            let more: &[&str] = match *group {
                "Library" => &["ライブラリを今すぐ更新", "ライブラリをスキャン", "再生履歴を消去…"],
                "Playback" => &["イコライザ…", "出力デバイスを再読み込み"],
                "Appearance" => &["テーマ", "使用中のフォントは", "テーマフォルダを開く", "新規テーマ…", "内蔵のテーマなので変更できません"],
                "Saved results" => &["保存されたすべての結果を削除…"],
                "Detection" => &["品質チェックを開く"],
                _ => &[],
            };
            has(&t, more, group);
        }

        // the windows: Equalizer (both tabs), Playback info, a name and a confirmation dialog, Edit Info
        p.play_list(vec![a1.clone()], None, "Songs");
        p.engine.pause();
        p.eq_open = true;
        p.eq_tab = 0;
        let t = texts(&ctx, &mut p);
        has(&t, &["イコライザ", "プリセット", "イコライザをオン", "グラフィック", "グラフィックイコライザを使う", "プリアンプ"], "Equalizer");
        p.eq_tab = 1;
        let t = texts(&ctx, &mut p);
        has(&t, &["パラメトリックイコライザを使う", "バンドを追加", "周波数", "ゲイン"], "Equalizer, parametric");
        p.eq_open = false;
        p.open_info();
        let t = texts(&ctx, &mut p);
        has(&t, &["再生情報", "トラック", "デコーダ", "サンプリング周波数", "ヌル出力（音は出ません）", "判定", "まだチェックしていません", "コピー"], "Playback info");
        p.info = None;
        p.ask_name(tr!("New Playlist"), tr!("New Playlist"), NameAction::NewPlaylist(vec![]));
        let t = texts(&ctx, &mut p);
        has(&t, &["新規プレイリスト", "名前", "キャンセル"], "the name dialog");
        assert_eq!(p.name_dialog.as_ref().unwrap().text, "新規プレイリスト", "the proposed name is translated");
        p.name_dialog = None;
        p.confirm = Some(ConfirmAction::DeletePlaylist(pid));
        let t = texts(&ctx, &mut p);
        has(&t, &["プレイリストを削除", "プレイリスト「Road Trip」を削除しますか？", "削除"], "the confirmation");
        p.confirm = None;
        p.edit_song_info(&a2);
        run_until(&ctx, &mut p, "the tags", |p| !p.edit_info.as_ref().unwrap().reading());
        let t = texts(&ctx, &mut p);
        has(&t, &["プロパティ", "タイトル", "保存", "キャンセル"], "Properties");
        p.edit_info = None;
        frame(&ctx, &mut p);
        p.engine.stop();
        drop(p);
        realhires::i18n::set_thread_language("");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// One frame with these input events; returns the labels and rectangles of the
    /// widgets AccessKit was told about (the context menu's buttons among them).
    fn accesskit_frame(ctx: &egui::Context, p: &mut Player, events: Vec<egui::Event>) -> Vec<(String, Rect)> {
        let raw = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1280.0, 840.0))),
            events,
            ..Default::default()
        };
        let out = ctx.run_ui(raw, |ui| p.ui(ui));
        let mut nodes = vec![];
        if let Some(update) = &out.platform_output.accesskit_update {
            for (_, node) in &update.nodes {
                if let (Some(label), Some(b)) = (node.label(), node.bounds()) {
                    let rect = Rect::from_min_max(pos2(b.x0 as f32, b.y0 as f32), pos2(b.x1 as f32, b.y1 as f32));
                    nodes.push((label.to_string(), rect));
                }
            }
        }
        out.drop_without_applying_deltas();
        nodes
    }

    fn pointer(pos: egui::Pos2, button: egui::PointerButton, pressed: bool) -> egui::Event {
        egui::Event::PointerButton { pos, button, pressed, modifiers: Default::default() }
    }

    /// Drag Up Next's row `from` of `list` with the primary button to `to` (y), in steps.
    fn drag_row(ctx: &egui::Context, p: &mut Player, list: &str, from: usize, to: f32) {
        let start = ctx.read_response(crate::upnext::row_id(list, from)).expect("an Up Next row").rect.center();
        accesskit_frame(ctx, p, vec![egui::Event::PointerMoved(start)]);
        accesskit_frame(ctx, p, vec![pointer(start, egui::PointerButton::Primary, true)]);
        accesskit_frame(ctx, p, vec![egui::Event::PointerMoved(pos2(start.x, start.y + if to < start.y { -3.0 } else { 3.0 }))]);
        assert!(ctx.data(|d| d.get_temp::<usize>(egui::Id::unique(("up-next-drag", list)))).is_none(), "a drag before 5 px");
        for y in [(start.y + to) / 2.0, to] {
            accesskit_frame(ctx, p, vec![egui::Event::PointerMoved(pos2(start.x, y))]);
        }
        assert_eq!(ctx.data(|d| d.get_temp::<usize>(egui::Id::unique(("up-next-drag", list)))), Some(from), "not dragging");
        accesskit_frame(ctx, p, vec![pointer(pos2(start.x, to), egui::PointerButton::Primary, false)]);
        frame(ctx, p);
    }

    /// Right-click Up Next's row `i` of `list` and click "Remove from Up Next" in its menu.
    fn remove_row(ctx: &egui::Context, p: &mut Player, list: &str, i: usize) {
        let at = ctx.read_response(crate::upnext::row_id(list, i)).expect("an Up Next row").rect.center();
        accesskit_frame(ctx, p, vec![egui::Event::PointerMoved(at)]);
        accesskit_frame(ctx, p, vec![pointer(at, egui::PointerButton::Secondary, true)]);
        accesskit_frame(ctx, p, vec![pointer(at, egui::PointerButton::Secondary, false)]);
        let mut item = None;
        for _ in 0..3 {
            let nodes = accesskit_frame(ctx, p, vec![]);
            item = nodes.into_iter().find(|(label, _)| label == "Remove from Up Next").map(|(_, r)| r.center());
            if item.is_some() {
                break;
            }
        }
        let item = item.expect("the right-click menu with Remove from Up Next");
        accesskit_frame(ctx, p, vec![egui::Event::PointerMoved(item)]);
        accesskit_frame(ctx, p, vec![pointer(item, egui::PointerButton::Primary, true)]);
        accesskit_frame(ctx, p, vec![pointer(item, egui::PointerButton::Primary, false)]);
        frame(ctx, p);
    }

    #[test]
    fn up_next_songs_can_be_dragged_and_removed() {
        // 2.3: in the Up Next panel and in Now Playing's Up Next view, a song is dragged to
        // another place (across the parts of Up Next) or removed with a right click, and the
        // song that follows gaplessly is set again
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        realhires::i18n::set_thread_language("en");
        let tmp = std::env::temp_dir().join(format!("rhr-up-next-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        std::env::set_var(realhires::covers::COVERS_ENV, tmp.join("covers"));
        let songs: Vec<String> = ["Dawn", "Noon", "Dusk", "Night"]
            .iter()
            .enumerate()
            .map(|(n, t)| song(&music, "Four Hours", t, "Ada Quill", "Folk", n as u32 + 1, 44_100, 20.0))
            .collect();
        let extra = song(&music, "Elsewhere", "Detour", "Ben Oak", "Folk", 1, 44_100, 20.0);
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        ctx.enable_accesskit();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 5 && p.lib_rx.is_none());
        p.play_list(songs.clone(), Some(&songs[0]), "Four Hours");
        p.engine.pause(); // the song must not end while the rows are dragged
        p.queue_songs(std::slice::from_ref(&extra), true);
        p.show_up_next = true;
        frame(&ctx, &mut p);
        frame(&ctx, &mut p);
        let up = |p: &Player| p.queue.as_ref().unwrap().up_next(10);
        assert_eq!(up(&p), vec![extra.clone(), songs[1].clone(), songs[2].clone(), songs[3].clone()]);
        assert_eq!(p.engine.next_path(), Some(extra.clone()));
        let row = |i: usize| ctx.read_response(crate::upnext::row_id("panel", i)).expect("an Up Next row").rect;

        // drag "Night" (a song of the list, row 3) to the top, before the queued song: it is queued too
        let to = row(0).top() + 3.0;
        drag_row(&ctx, &mut p, "panel", 3, to);
        assert_eq!(up(&p), vec![songs[3].clone(), extra.clone(), songs[1].clone(), songs[2].clone()]);
        assert_eq!(p.queue.as_ref().unwrap().queued, vec![songs[3].clone(), extra.clone()]);
        assert_eq!(p.engine.next_path(), Some(songs[3].clone()), "the gapless follower");

        // right click the queued "Detour" (row 1): Remove from Up Next
        remove_row(&ctx, &mut p, "panel", 1);
        assert_eq!(up(&p), vec![songs[3].clone(), songs[1].clone(), songs[2].clone()]);
        assert!(p.queue.as_ref().unwrap().removed.is_empty(), "a queued song is not kept out of the list");

        // Now Playing's Up Next view does the same
        p.show_up_next = false;
        p.go(Page::NowPlaying);
        p.set_np_tab(NpTab::UpNext);
        frame(&ctx, &mut p);
        frame(&ctx, &mut p);
        assert_eq!(p.np_hits.up_next, up(&p));
        let np_row = |i: usize| ctx.read_response(crate::upnext::row_id("now-playing", i)).expect("a row").rect;
        let to = np_row(2).bottom() - 3.0;
        drag_row(&ctx, &mut p, "now-playing", 0, to); // "Night" back after "Dusk", into the list
        assert_eq!(up(&p), vec![songs[1].clone(), songs[2].clone(), songs[3].clone()]);
        assert!(p.queue.as_ref().unwrap().queued.is_empty());
        assert_eq!(p.engine.next_path(), Some(songs[1].clone()));
        remove_row(&ctx, &mut p, "now-playing", 0); // "Noon"
        assert_eq!(up(&p), vec![songs[2].clone(), songs[3].clone()]);
        assert_eq!(p.engine.next_path(), Some(songs[2].clone()));
        // building the order again (a new shuffle mode) leaves the removed song out
        p.set_shuffle("off");
        assert_eq!(up(&p), vec![songs[2].clone(), songs[3].clone()]);
        assert!(p.queue.as_ref().unwrap().removed.contains(&songs[1]));
        // starting the list again brings it back
        p.play_list(songs.clone(), Some(&songs[0]), "Four Hours");
        assert_eq!(up(&p), songs[1..].to_vec());
        p.on_exit();
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn equalizer_window_holds_its_content_in_japanese() {
        // 2.3: in Japanese with bit-perfect playback on (its note above the curve), every widget
        // of the Equalizer window lies inside the window, so its title bar and close button span
        // the content; the preset row wraps instead of running past the window's edge
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        realhires::i18n::set_thread_language("日本語");
        let tmp = std::env::temp_dir().join(format!("rhr-eq-window-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(tmp.join("Music")).unwrap();
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, tmp.join("tabs"));
        std::env::set_var(realhires::covers::COVERS_ENV, tmp.join("covers"));
        let mut cfg = Settings::default();
        cfg.library_folders = vec![tmp.join("Music").to_string_lossy().into_owned()];
        cfg.language = "日本語".into();
        cfg.bit_perfect = true;
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        let mut p = Player::new(&ctx, cfg, vec![]);
        realhires::i18n::set_thread_language("日本語");
        // a preset of the user's own, so that Rename… and Delete… are buttons
        p.state.eq.enabled = true;
        p.state.eq.apply_preset("Rock");
        let eq = p.state.eq.clone();
        let _ = p.state.save_eq_preset("ロック（夜）", &eq, false);
        p.eq_notes = vec!["“ロック（夜）”として読み込みました。".into()];
        p.eq_open = true;
        ctx.enable_accesskit();
        for tab in [0, 1] {
            p.eq_tab = tab;
            let shown = texts(&ctx, &mut p);
            assert!(shown.contains("ビットパーフェクト再生がオンです"), "{shown}");
            let nodes = accesskit_frame(&ctx, &mut p, vec![]);
            let (outer, content) = p.eq_rects.expect("the Equalizer window");
            assert!(
                outer.expand(0.5).contains_rect(content),
                "tab {tab}: the content {content:?} runs past the window {outer:?}"
            );
            // the title bar spans the window: its close button sits at the window's right edge,
            // to the right of every widget (in 2.2 the preset row ran about 100 px past it)
            let close = nodes.iter().find(|(l, _)| l == "Close window").map(|(_, r)| *r).expect("the close button");
            assert!(close.right() <= outer.right() + 0.5 && outer.right() - close.right() < 16.0, "tab {tab}: {close:?} in {outer:?}");
            assert!(content.right() <= close.right() + 8.0, "tab {tab}: the content {content:?} runs past the close button {close:?}");
        }
        p.on_exit();
        drop(p);
        realhires::i18n::set_thread_language("en");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// One frame at the size of the CI runners' screens (1024 x 768), with these events.
    fn small_frame(ctx: &egui::Context, p: &mut Player, events: Vec<egui::Event>) -> Vec<(String, Rect)> {
        let raw = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1024.0, 768.0))),
            events,
            ..Default::default()
        };
        let out = ctx.run_ui(raw, |ui| p.ui(ui));
        let mut nodes = vec![];
        if let Some(update) = &out.platform_output.accesskit_update {
            for (_, node) in &update.nodes {
                if let (Some(label), Some(b)) = (node.label(), node.bounds()) {
                    nodes.push((label.to_string(), Rect::from_min_max(pos2(b.x0 as f32, b.y0 as f32), pos2(b.x1 as f32, b.y1 as f32))));
                }
            }
        }
        out.drop_without_applying_deltas();
        nodes
    }

    /// A click (pressed, then let go in the next frame) at `at` with `button`.
    fn click_at(ctx: &egui::Context, p: &mut Player, at: egui::Pos2, button: egui::PointerButton) {
        small_frame(ctx, p, vec![egui::Event::PointerMoved(at)]);
        small_frame(ctx, p, vec![pointer(at, button, true)]);
        small_frame(ctx, p, vec![pointer(at, button, false)]);
        small_frame(ctx, p, vec![egui::Event::PointerMoved(pos2(1000.0, 760.0))]);
    }

    /// Click the menu entry (or button) AccessKit calls `label`.
    fn click_label(ctx: &egui::Context, p: &mut Player, label: &str) {
        let mut at = None;
        for _ in 0..3 {
            at = small_frame(ctx, p, vec![]).into_iter().find(|(l, _)| l == label).map(|(_, r)| r.center());
            if at.is_some() {
                break;
            }
        }
        let at = at.unwrap_or_else(|| panic!("no {label}"));
        click_at(ctx, p, at, egui::PointerButton::Primary);
    }

    fn enter(ctx: &egui::Context, p: &mut Player) {
        let key = egui::Event::Key { key: egui::Key::Enter, physical_key: None, pressed: true, repeat: false, modifiers: Default::default() };
        small_frame(ctx, p, vec![key]);
        small_frame(ctx, p, vec![]);
    }

    #[test]
    fn custom_tabs_folder_pages_window_and_files() {
        // 2.3: the tabs folder read at start (a file that is not a tab is named), the sidebar's
        // Tabs and the Library page's entries, a tab's page and its "…" menu, Now Playing's back
        // link naming the tab, New Tab… with rules (the live count, errors that keep the window
        // open, a taken name), Edit Tab… renaming the tab and its file, a tab on likes, the
        // sidebar's right-click menu with Delete Tab…, Import tab…, a dropped tab file, files
        // copied into the folder, the window at 1024 x 768, and the texts in Japanese
        let _env = crate::TEST_ENV.lock().unwrap_or_else(|e| e.into_inner());
        realhires::i18n::set_thread_language("en");
        let tmp = std::env::temp_dir().join(format!("rhr-tabs-ui-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let music = tmp.join("Music");
        let tabs = tmp.join("tabs");
        std::env::set_var("REALHIRES_RESULTS", tmp.join("results.sqlite"));
        std::env::set_var("REALHIRES_SETTINGS", tmp.join("settings.json"));
        std::env::set_var(musiclib::LIBRARY_ENV, tmp.join("library.sqlite"));
        std::env::set_var(playerstate::PLAYER_ENV, tmp.join("player.json"));
        std::env::set_var(crate::audio::BACKEND_ENV, "null");
        std::env::set_var(realhires::themes::THEMES_ENV, tmp.join("themes.json"));
        std::env::set_var(realhires::covers::COVERS_ENV, tmp.join("covers"));
        std::env::set_var(realhires::smarttabs::TABS_ENV, &tabs);
        use realhires::smarttabs;
        let a1 = song(&music, "Paper Lanterns", "Lanterns Over Water", "Mira Calloway", "Pop", 1, 44_100, 2.0);
        let a2 = song(&music, "Paper Lanterns", "Paper in the Rain", "Mira Calloway", "Pop", 2, 44_100, 2.0);
        let b1 = song(&music, "Harbour Lights", "Tidewater", "The Northern Quay", "Jazz", 1, 96_000, 2.0);
        std::fs::create_dir_all(&tabs).unwrap();
        std::fs::write(tabs.join("Hi-Res.json"), smarttabs::tab_text(&smarttabs::templates()[0]).unwrap()).unwrap();
        std::fs::write(tabs.join("notes.json"), "{not a tab").unwrap();
        let mut cfg = Settings::default();
        cfg.library_folders = vec![music.to_string_lossy().into_owned()];
        realhires::settings::save(&cfg, None).unwrap();
        let ctx = egui::Context::default();
        ctx.enable_accesskit();
        let mut p = Player::new(&ctx, cfg, vec![]);
        run_until(&ctx, &mut p, "the library index", |p| p.lib.tracks.len() == 3 && p.lib_rx.is_none());
        assert_eq!(p.tab_names(), vec!["Hi-Res".to_string()]);
        assert_eq!(p.tab_problems, vec![("notes.json".to_string(), "This file is not a tab of Universal Music Player.".to_string())]);
        // the Library page lists the tab after its fifteen entries, the sidebar under "Tabs"
        let entries = p.library_entries();
        assert_eq!(entries.len(), 16);
        let last = entries.last().unwrap();
        assert_eq!((last.name.as_str(), last.count.as_str(), &last.page), ("Hi-Res", "1 song", &Page::Tab("Hi-Res".into())));
        let shown = texts(&ctx, &mut p);
        assert!(shown.contains("Tabs\n") && shown.contains("New Tab…") && shown.contains("Hi-Res\n1 song"), "{shown}");
        // a click on the sidebar's tab opens its page
        small_frame(&ctx, &mut p, vec![]);
        let row = p.tab_hits.sidebar.iter().find(|(n, _)| n == "Hi-Res").map(|(_, r)| r.center()).expect("the sidebar's tab");
        click_at(&ctx, &mut p, row, egui::PointerButton::Primary);
        assert_eq!(p.page, Page::Tab("Hi-Res".into()));
        let shown = texts(&ctx, &mut p);
        assert!(shown.contains("1 song, 0 minutes") && shown.contains("Tidewater"), "{shown}");
        assert!(!shown.contains("Paper in the Rain"));
        assert_eq!(p.page_songs(), vec![b1.clone()]);
        // its "…": Edit Tab…, Export Tab…, Delete Tab…
        small_frame(&ctx, &mut p, vec![]);
        let more = p.tab_hits.more.expect("the … button").center();
        click_at(&ctx, &mut p, more, egui::PointerButton::Primary);
        let labels: Vec<String> = small_frame(&ctx, &mut p, vec![]).into_iter().map(|(l, _)| l).collect();
        for want in ["Edit Tab…", "Export Tab…", "Delete Tab…", "Colour Label"] {
            assert!(labels.iter().any(|l| l == want), "{want} in {labels:?}");
        }
        click_label(&ctx, &mut p, "Edit Tab…");
        assert_eq!(p.tab_win.as_ref().and_then(|w| w.old.as_ref()).map(|t| t.name.as_str()), Some("Hi-Res"));
        p.tab_win = None;
        // play from the tab: Now Playing's back link names it
        let list = p.tab_list("Hi-Res");
        p.play_list(list.to_vec(), Some(&b1), "Hi-Res");
        p.engine.pause();
        p.go(Page::NowPlaying);
        assert_eq!(p.back_label().as_deref(), Some("Hi-Res"));
        p.go_back();

        // New Tab… from the sidebar
        small_frame(&ctx, &mut p, vec![]);
        let new_tab = p.tab_hits.new_tab.expect("New Tab…").center();
        click_at(&ctx, &mut p, new_tab, egui::PointerButton::Primary);
        let w = p.tab_win.as_mut().expect("the tab window");
        assert!(w.old.is_none() && w.name == "New Tab" && w.rules.is_empty());
        w.name = "Pop".into();
        w.add_rule();
        w.set_field(0, "genre");
        w.rules[0].rule.op = "is".into();
        w.rules[0].text = "pop".into();
        small_frame(&ctx, &mut p, vec![]);
        assert_eq!(p.tab_win.as_ref().unwrap().count, Some(2));
        let shown = texts(&ctx, &mut p);
        assert!(shown.contains("New Tab") && shown.contains("2 songs match") && shown.contains("Genre"), "{shown}");
        // the window fits the screen of the CI runners
        small_frame(&ctx, &mut p, vec![]);
        let win = p.tab_hits.window.expect("the window's rectangle");
        assert!(Rect::from_min_size(pos2(0.0, 0.0), vec2(1024.0, 768.0)).contains_rect(win), "{win:?}");
        // a value of the wrong kind, then a taken name: the window stays open with the message
        let w = p.tab_win.as_mut().unwrap();
        w.add_rule();
        w.set_field(1, "year");
        w.rules[1].text = "two thousand".into();
        enter(&ctx, &mut p);
        assert_eq!(p.tab_win.as_ref().map(|w| w.error.as_str()), Some("Rule 2: the value of “Year” must be a number."));
        let w = p.tab_win.as_mut().unwrap();
        w.rules.remove(1);
        w.name = "hi-res".into();
        enter(&ctx, &mut p);
        assert_eq!(p.tab_win.as_ref().map(|w| w.error.as_str()), Some("You already have a tab called “hi-res”."));
        p.tab_win.as_mut().unwrap().name = "Pop".into();
        enter(&ctx, &mut p);
        assert!(p.tab_win.is_none());
        assert!(tabs.join("Pop.json").is_file());
        assert_eq!(p.page, Page::Tab("Pop".into()));
        assert_eq!(p.tab_list("Pop").to_vec(), vec![a1.clone(), a2.clone()]);
        // Edit Tab…: renamed; the file, the page and Back follow
        p.go(Page::Library);
        p.go(Page::Tab("Pop".into()));
        p.go(Page::Albums);
        p.set_label("tab", "Pop", Some("#34c759")); // a colour label, which follows the name
        p.edit_tab("Pop");
        let w = p.tab_win.as_mut().unwrap();
        w.name = "Pop songs".into();
        w.descending = true;
        enter(&ctx, &mut p);
        assert!(tabs.join("Pop songs.json").is_file() && !tabs.join("Pop.json").exists());
        assert_eq!(p.back.last(), Some(&Page::Tab("Pop songs".into())));
        assert_eq!(p.state.label_of("tab", "Pop songs"), Some("#34c759"));
        assert_eq!(p.state.label_of("tab", "Pop"), None);
        assert_eq!(p.tab_list("Pop songs").to_vec(), vec![a2.clone(), a1.clone()], "descending by title");
        // a tab on likes follows them
        let mut loved = smarttabs::Tab::new("Loved");
        loved.rules = vec![smarttabs::new_rule("liked")];
        smarttabs::save_tab(&loved, Some(&tabs), None).unwrap();
        p.go(Page::Library); // the folder is read again
        assert!(p.tab("Loved").is_some() && p.tab_list("Loved").is_empty());
        p.toggle_like(&a1);
        assert_eq!(p.tab_list("Loved").to_vec(), vec![a1.clone()]);
        p.set_label("tab", "Loved", Some("#ff9500"));
        // the sidebar's right click: Delete Tab…, confirmed
        small_frame(&ctx, &mut p, vec![]);
        let row = p.tab_hits.sidebar.iter().find(|(n, _)| n == "Loved").map(|(_, r)| r.center()).expect("Loved");
        click_at(&ctx, &mut p, row, egui::PointerButton::Secondary);
        click_label(&ctx, &mut p, "Delete Tab…");
        assert_eq!(p.confirm, Some(ConfirmAction::DeleteTab("Loved".into())));
        let shown = texts(&ctx, &mut p);
        assert!(shown.contains("Delete the tab “Loved”? Its file is deleted from the tabs folder; the songs are not."), "{shown}");
        click_label(&ctx, &mut p, "Delete");
        assert!(p.confirm.is_none() && p.tab("Loved").is_none() && !tabs.join("Loved.json").exists());
        assert_eq!(p.state.label_of("tab", "Loved"), None, "its label went with it");
        // Export, then Import tab… (a taken name gets " 2"), and a tab file dropped onto the window
        let shared = tmp.join("shared.json");
        smarttabs::export_tab(p.tab("Pop songs").unwrap(), &shared).unwrap();
        p.import_tab_path(&shared);
        assert_eq!(p.page, Page::Tab("Pop songs 2".into()));
        assert!(tabs.join("Pop songs 2.json").is_file());
        p.import_tab_path(&tabs.join("Pop songs 2.json"));
        assert_eq!(p.scanner.info.as_ref().map(|(_, m)| m.as_str()), Some("This tab is in the tabs folder already."));
        p.scanner.info = None;
        let dropped = tmp.join("dropped.json");
        std::fs::write(&dropped, smarttabs::tab_text(&smarttabs::templates()[3]).unwrap()).unwrap();
        #[derive(Debug)]
        struct Dropped(std::path::PathBuf);
        impl egui::DroppedFile for Dropped {
            fn path(&self) -> &std::path::Path {
                &self.0
            }
            fn bytes(&self) -> Result<Vec<u8>, String> {
                std::fs::read(&self.0).map_err(|e| e.to_string())
            }
        }
        let dropped_input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), vec2(1024.0, 768.0))),
            dropped_files: vec![Arc::new(Dropped(dropped.clone()))],
            ..Default::default()
        };
        ctx.run_ui(dropped_input, |ui| p.ui(ui)).drop_without_applying_deltas();
        assert_eq!(p.page, Page::Tab("Long songs".into()));
        assert!(p.custom_themes.is_empty(), "a tab file is not a theme");
        assert!(p.scanner.sources.is_empty(), "nor a source of the quality check");
        // a file copied into the folder appears when the Library page is shown
        let mut short = smarttabs::Tab::new("Short");
        short.rules = vec![smarttabs::Rule { field: "duration".into(), op: "<".into(), value: realhires::json::Value::Int(60) }];
        std::fs::write(tabs.join("Short.json"), smarttabs::tab_text(&short).unwrap()).unwrap();
        p.go(Page::Library);
        assert_eq!(p.tab_list("Short").len(), 3);
        // Settings → Library: the buttons, the note and the file that is not a tab
        p.settings_group = realhires::settings::GROUPS.iter().position(|g| *g == "Library").unwrap();
        p.go(Page::Settings);
        let shown = texts(&ctx, &mut p);
        assert!(shown.contains("Import tab…") && shown.contains("Open tabs folder") && shown.contains("notes.json: This file is not a tab"), "{shown}");
        // in Japanese
        realhires::i18n::set_thread_language("ja");
        p.go(Page::Tab("Pop songs".into()));
        let shown = texts(&ctx, &mut p);
        assert!(shown.contains("タブ\n") && shown.contains("新規タブ…") && shown.contains("2曲、0分"), "{shown}");
        p.new_tab();
        let shown = texts(&ctx, &mut p);
        assert!(shown.contains("新規タブ") && shown.contains("テンプレート") && shown.contains("次のすべてのルールに一致"), "{shown}");
        let names = p.tab_names();
        p.tab_win.as_mut().unwrap().apply_template(1, &names);
        let shown = texts(&ctx, &mut p);
        assert!(shown.contains("ハイレゾ") && shown.contains("音質") && shown.contains("が次と一致") && shown.contains("1曲が一致"), "{shown}");
        realhires::i18n::set_thread_language("en");
        p.on_exit();
        drop(p);
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
