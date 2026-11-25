# Real-Hi-Res 2.0 music player: shared specification

Both desktop apps (Python/Tkinter `realhires/gui.py` + new modules, and Rust/egui `rust/crates/gui`) implement this
specification, so they behave the same. The maths and data live in the shared core modules, which both versions
already have and which are checked against each other: `eq` (equalizer), `playqueue` (shuffle/repeat queue),
`playerstate` (`player.json`: likes, volume, modes, EQ, last song), `musiclib` (`library.sqlite`: the tag index of
the library folders), `embed` (lyrics inside MP3/FLAC files) and `lyrics` (LRCLIB, with `lyrics_save_as`).

## What the user asked for (2026-11-24)

The app becomes a music player that looks similar to Apple Music, with SF Pro Display Bold for headings (SF Pro on
macOS from the system; Windows and Linux use a similar font, Inter, bundled under the SIL Open Font License; the user
can choose another font in Settings). Every function the app has today (scan for fake files, Fix, lyrics download,
saved results, all settings) is reached from Settings. The Now Playing view shows the album picture on the left and
the lyrics on the right; without lyrics the cover is centred. Songs can be liked. The playback info is easy to see,
and clicking it shows the track info and the decoder. Shuffle offers: Off, Songs in this list, All songs in the
library, Same genre, Same artist, Same album. Repeat offers: Off, Repeat song, Repeat album, Stop after this song.
There is a graphic and a parametric equalizer. Lyrics can be downloaded as the scan goes, and saved as an .lrc file,
inside the audio file, both, or as plain lyrics inside the file (`lyrics_save_as`, already in the core).

## Window

- **Sidebar** (left, about 220 px, slightly darker than the content): a Search box at the top; the heading
  "Library" with Recently Added, Artists, Albums, Songs, Genres; the heading "Playlists" with Liked Songs; at the
  bottom Settings, and a one-line status while the library index is being updated ("Updating the library: 120 of
  500 songs").
- **Playback bar** (top, about 64 px): left the buttons Shuffle, Previous, Play/Pause, Next, Repeat; in the middle
  the "LCD": a rounded panel with a 44 px cover, the title (bold), "Artist — Album" (grey), a thin seek bar
  (click or drag to seek) with the elapsed time on the left and the remaining time ("-1:23") on the right, and a
  quality badge ("Hi-Res Lossless", "Lossless", or the codec and bit rate, `musiclib.quality()`); right: the heart
  (like the current song), Lyrics (toggles the Now Playing view), Up Next (toggles the queue panel), EQ (opens the
  equalizer window), and the volume: a speaker icon (click mutes) and a slider. When nothing plays, the LCD shows
  the app name in grey and Previous, Next and the heart are disabled (volume, EQ, Lyrics and Up Next stay usable);
  Play then plays the songs of the page shown.
- **Icons** are drawn as vector shapes (Tk Canvas, egui painter), never emoji or rare Unicode symbols, which several
  Linux fonts cannot show. A mode that is on (shuffle not off, repeat not off) is drawn in the accent colour; repeat
  song adds a small "1", stop after this song draws a small square.
- **Shuffle button**: a click opens a menu with the six modes as radio items (labels from
  `playqueue.SHUFFLE_LABELS` / `shuffle_label()`); choosing one calls `PlayQueue.set_shuffle()` and saves it in the
  player state. **Repeat button**: the same with the four repeat modes (`REPEAT_LABELS` / `repeat_label()`). The
  tooltip names the current mode. "Stop after this song" is never saved (the core already drops it on load).
- **Pages** in the content area:
  - Recently Added: album cards, newest first (the newest `mtime_ns` of an album's tracks).
  - Albums: album cards in `musiclib.albums()` order. A card is the square cover (rounded corners), the album title
    (bold) and the artist (grey) below it. Clicking a card opens the album page.
  - Album page: the cover (about 220 px) on the left; on the right the title (large, bold), the artist (accent
    colour), "Genre · Year" (grey), the buttons Play and Shuffle (accent filled), then the track list: number, title,
    a heart for liked songs, the duration, and at the end "N songs, M minutes". Double-click (or Enter) plays from
    that song with the album as the list.
  - Artists: the artists (`musiclib.artists()`) as a list; choosing one shows its albums as cards, with Play and
    Shuffle for all its songs.
  - Songs: a table (Title, Artist, Album, Genre, Time, Quality, and a heart column); a click on a column heading
    sorts by it; double-click plays from that song with the shown rows as the list.
  - Genres: the genres (`musiclib.genres()`); choosing one shows its songs as the table above.
  - Liked Songs: the liked songs as the table above (`PlayerState.liked`, library order).
  - Search: typing in the sidebar's Search box (after a 200 ms pause) shows the albums and songs whose title, artist,
    album or genre contains the text (case-folded); clearing it returns to the page before.
  - Now Playing (the Lyrics button, or a click on the LCD's cover): the cover on the left half (as large as fits, at
    most 480 px) and the lyrics on the right. Synced lyrics (`embed.lrc_entries()`): every line, the current one
    (the last line whose time is not after the position) bold in the text colour, the others grey; the view scrolls
    smoothly to keep the current line about a third from the top unless the user scrolled in the last 3 s; a click
    on a line seeks there. Plain lyrics: the text, scrollable. No lyrics: the cover centred with the title and artist
    under it, and a button "Get lyrics" that downloads them for this song (the lyrics window, one song). The lyrics
    are found with `lyrics.read_lyrics_file()` (an .lrc/.txt next to the song, else the lyrics inside the file).
  - Settings (see below) and Quality check (the 1.x scanner UI, unchanged, with all its functions).
- **Up Next panel** (right, about 300 px, toggled): "Playing from <list name>", then `PlayQueue.up_next(50)` with
  title and artist; double-click jumps to that song.
- **Playback info window** (a click on the quality badge): three sections, each a two-column list, and Copy.
  "Track": Title, Artist, Album, Album artist, Genre, Year, Track ("3 of 12" when known), Disc, Duration, File (full
  path), Size. "Decoder": Format (codec upper-case, e.g. FLAC, with "lossless"/"lossy"), Sample rate ("96 kHz"),
  Bit depth ("24-bit", "32-bit float", or "—" for lossy), Channels ("Stereo", "Mono", "6 channels"), Bit rate
  ("2,304 kbps"), Decoder ("FFmpeg", plus its version line when known). "Output": Device (name), Output ("96 kHz,
  32-bit float, stereo"), Resampling ("None: the device runs at the song's rate" or "From 96 kHz to 48 kHz (soxr)"),
  Volume ("−6.0 dB" or "Muted"), Equalizer ("Off", "Graphic: Rock", "Parametric: 3 bands", plus " (pre-amp −4.5
  dB)" when not zero), Gapless ("On"/"Off"). A fourth section "Quality check" shows the saved Real-Hi-Res verdict and
  summary of the file when the results store has one ("Not checked yet" otherwise).
- **Equalizer window**: "Equalizer on" checkbox (`enabled`), "Prevent clipping" (`prevent_clipping`, automatic
  headroom from `eq.preamp_db()`), two tabs Graphic and Parametric (each with its own `enabled`). Above the tabs the
  combined response curve (`eq.response()`, 20 Hz–20 kHz log axis, ±12 dB). Graphic: a preset menu (`PRESETS` plus
  "Custom", which is shown when the gains match no preset, `preset_name()`), a pre-amp slider and ten vertical
  sliders (−12…+12 dB, labels `GRAPHIC_LABELS`), double-click on a slider resets it to 0. Parametric: a pre-amp slider
  and one row per band: on, type (`BAND_TYPE_LABELS`), frequency (20–20000 Hz), gain (ignored and disabled for the
  types without gain), Q (0.1–10), and Remove; "Add band" until `MAX_PARAMETRIC_BANDS`. Changes are heard at once
  (Rust: the biquads are recomputed in the audio thread; Python: the decoder restarts at the current position with the
  new `eq.ffmpeg_filter()`) and saved in the player state when the window closes or the app quits.
- **Likes**: the heart in the playback bar and the heart column of every song table toggle `PlayerState.set_liked()`;
  the state is saved at once.

## Settings page

The Settings page replaces the Settings window of 1.x (the window stays reachable from the Quality check page). It
has one tab (or section list on the left) per `settings.GROUPS` entry, built from `Settings.field_info()` as before,
and adds actions:

- Library: the folder list (`library_folders`) with Add folder… and Remove; "Update the library now" (re-reads the
  tags with `Library.refresh(library_files(cfg))`); "Check the library for fake files" (opens the Quality check page
  with the library folders as its sources and starts a scan); "Download lyrics for the whole library…" (the lyrics
  window with every library song); "Open the quality checker" (the Quality check page as it was).
- Playback: `output_device` (a menu of the system's output devices, "" = the system default), `gapless`, and
  "Equalizer…".
- Appearance: `font_family` (a menu of the installed font families, "" = "SF Pro (macOS) / Inter"), `theme`.
- Lyrics: as before, plus `lyrics_save_as` and `lyrics_while_scanning`.
- Saved results: as before, with "Delete all saved results…".

Every change is saved at once (as in 1.x); fonts and theme apply at once.

## Library index

`library_folders` lists the music folders. On start, the app shows the index saved in `library.sqlite` at once and
always refreshes it in a background thread (only new or changed files are probed; files that are gone are dropped),
updating the pages when it finishes; the same happens when the folders change. When `scan_library_on_start` ("Check
new songs when the app starts") is on, the refresh is followed by a quality scan of the library folders in the
Quality check page's scanner (saved results answer unchanged songs at once), without switching to that page. Before any folder is set, the Albums page
shows "Add your music folder in Settings to start" with a button "Choose a music folder…", which adds it and starts
the update. Covers come from `covers.cover_png()` (cached per album, loaded in a thread).

## Playback engine

- Decoding goes through FFmpeg (`probe.tool("ffmpeg")`, `external_env()`, no window on Windows), always to 32-bit
  float, stereo (mono files are duplicated, more channels are downmixed by FFmpeg `-ac 2`): `ffmpeg -nostdin -v
  error -ss <start> -i <file> -map 0:a:0 -vn [-af <filters>] -f f32le -ac 2 -ar <out rate> pipe:1`.
- The output opens at the song's sample rate when the device supports it ("Resampling: None"); otherwise at the
  device's default rate, and FFmpeg resamples with soxr (`aresample=resampler=soxr:precision=28`, or the default
  resampler when the FFmpeg build lacks soxr).
- Gapless (`gapless` on): the engine keeps about 2 s decoded ahead; when the current song has been decoded to its
  end it goes straight on decoding the next song of the queue (`PlayQueue.up_next(1)` when repeat is not song/stop;
  the same song again for repeat song) into the same buffer, so there is no silence between them, when that song gets
  the same output rate; otherwise the device is reopened at the next song's rate. The engine reports `Started` when a
  song's first sample is heard, `Ended(chained)` when its last one is (chained: the next song follows gaplessly) and
  `Failed`; on `Started` of a chained song the UI advances the queue with `next(auto=True)`.
- Volume: gain = volume² (volume 0…1 from the slider), 0 when muted; applied in the output callback, after the EQ.
  The Playback info shows it as dB (20·log10(gain)).
- EQ: when `enabled` and the active stages are not all identity, the stages of `eq.stages(state, out_rate)` run on
  the float samples before the volume, with the pre-amp `eq.preamp_db()` (0 when prevent clipping is off). Rust runs
  them in the audio callback (`EqProcessor`); Python lets FFmpeg run them (`eq.ffmpeg_filter()`).
- Position: the frames played since the song started (not decoded), so the seek bar and the lyrics follow what is
  heard. Seeking restarts FFmpeg at the new position and drops the buffer.
- Errors (no output device, unreadable file) show in the LCD's second line for that song, and playback moves to the
  next song after 2 s, except when every song failed.
- On quit the player state saves the song and position (`last_path`, `last_position`); on start the LCD shows that
  song paused at that position (it does not start playing by itself), with its album as the list it plays from.

## Lyrics while scanning

When `lyrics_while_scanning` is on, the Quality check scan (and "Check the library for fake files") passes every
analysed file to one lyrics worker (one `LrclibClient`, sequential requests, as LRCLIB asks), which follows the
Lyrics settings (`skip_reason()`, `lyrics_save_as`) and reports in the status line ("Lyrics: 12 saved, 3 not
found": " · Lyrics: N saved, M not found" at the end of the scanner's status line; saved = the outcome saved
anything, not found = NOT_FOUND). The scanner owns the worker (started on first use); Stop or a new scan stops it
after the current song. Lyrics written into a file keep its saved scan result (`ResultStore.retag()`).

## Fonts and theme

- macOS: the system font (`/System/Library/Fonts/SFNS.ttf`, the variable SF Pro) at weight 700 for headings ("SF Pro
  Display Bold") and 400 for text; Windows and Linux: the bundled Inter (`realhires/assets/fonts/InterVariable.ttf`,
  with `LICENSE-Inter.txt`), 700 and 400. Tk uses the family names ("SF Pro Display"/".AppleSystemUIFont" on macOS,
  "Inter" after loading the bundled file where Tk can, else the platform's UI font) with the bold weight.
- `font_family` set: that family for everything, bold for headings.
- Colours (light / dark): background #FFFFFF / #1E1E1E, sidebar #F5F5F7 / #262628, text #1D1D1F / #F5F5F7, secondary
  text #6E6E73 / #A1A1A6, separators #D2D2D7 / #3A3A3C, accent #FA2D48 (both), the LCD panel #F2F2F4 / #2C2C2E.
  `theme` "Match the system" follows the system's dark mode where it can be read (egui does; Tk reads the macOS and
  Windows setting and otherwise uses light).

## Quality check page

The 1.x app unchanged (sources, list, dashboard, summary, Fix, lyrics, find bar, card views, export), shown in the
content area with the sidebar beside it, so every existing test keeps working. Its scan also feeds the lyrics worker
when `lyrics_while_scanning` is on.

## Additions (asked for on 2026-11-24, after the first version of this spec)

The user asked for a Library landing page with fourteen entries, playlists, dislikes, a play history with "Your
Stats", named equalizer presets that can be imported and exported, and custom themes; and that Settings → "Scan
library" opens the quality checker as it is. The core of all of it exists in both versions, checked against each
other (`tests/test_player_library.py`, `rust/crates/core/tests/player_additions.rs`): `musiclib` (composer tag,
`album_artists()`, `years()`, `composers()`, `folder_view()`, `album_sorted()`, the play history `add_play()`,
`plays()`, `recently_played()`, `clear_history()`), `stats` (`compute()`, `cards()`, `period_range()`,
`library_stats()`, `PERIODS`), `playerstate` (`disliked`, `set_disliked()`, playlists, `eq_presets`), `playqueue`
(`disliked`: left out of shuffled orders), `eq` (`export_json()`, `export_apo()`, `import_eq()`), `themes`
(`BUILTIN`, `load_custom()`, `save_custom()`, `resolve()`, `check_name()`, `copy_name()`, `export_theme()`,
`import_theme()`), and the `theme` setting, which is now free text holding a theme name.

### The Library page (the start page)

The app opens on **Library**: the title "Library", then the fourteen entries as a grid of tiles (icon, name, and a
count in grey: songs, folders, albums...), in this order: **All Songs**, **Folders**, **Albums**, **Artists**,
**Album Artists**, **Genres**, **Years**, **Composers**, **Playlists**, **Liked Songs**, **Disliked Songs**,
**Recently Played**, **Recently Added**, **Your Stats**. Under the tiles: a row of the most recently added albums
(cards) and, when there are plays this year, a small "Your Stats" banner with the minutes listened this year.

The sidebar lists the same: **Library** (the landing page) at the top, then the section "Library" with All Songs,
Folders, Albums, Artists, Album Artists, Genres, Years and Composers; the section "Playlists" with Liked Songs,
Disliked Songs and every playlist by name (and "New Playlist…"); the section "History" with Recently Played,
Recently Added and Your Stats; Settings at the bottom. The pages of the first version keep their behaviour ("Songs"
is now called All Songs).

- **Folders**: the library folders as the roots; a folder page shows its subfolders (folder icon, name, song count;
  a click opens it) above its songs (a song table in album order), with a breadcrumb ("Music › Jazz › 1999") whose
  parts are clickable; Play and Shuffle play the songs in the folder and below it.
- **Artists** (the track's artist), **Album Artists**, **Genres**, **Years** (newest first, "Unknown Year" last)
  and **Composers**: the names on the left with their counts, the songs or albums of the chosen one on the right
  (Artists and Album Artists show album cards; Genres, Years and Composers show song tables), each with Play and
  Shuffle.
- **Playlists**: a page with the playlists as cards (a 2×2 mosaic of the first four songs' covers, the name and the
  number of songs) and "New Playlist…". A playlist page: the mosaic, the name (Rename…, Delete… with a
  confirmation), "N songs, M minutes", Play, Shuffle, and the song table in the playlist's order, where the context
  menu adds "Remove from this playlist", "Move up" and "Move down". Every song's context menu (and the playback
  bar's "…" menu for the song playing) has "Add to playlist ▸" with the playlists and "New Playlist…". Playlist
  songs whose files are gone are shown in grey as "Not in the library" and skipped when played.
- **Disliked Songs**: the songs marked with the dislike button (a thumbs-down drawn as a vector icon, next to the
  heart in the playback bar and in the song's context menu: "Dislike" / "Remove dislike"). Liking a song removes
  its dislike and the other way round. Disliked songs are left out when shuffling (`PlayQueue.disliked`), unless
  you choose one yourself.
- **Recently Played**: the last 200 different songs played (`Library.recently_played()`), newest first, as a song
  table with a "Played" column ("Today 14:05", "Yesterday", or the date), and "Clear history…" (confirmation).
- **Recently Added**: as before.
- **Your Stats**: a period menu (`stats.PERIODS`, "This year" by default) and the summary as large coloured cards
  (`stats.cards()`: label small, big text bold, detail grey; each card in its own accent colour, like a yearly music
  review), then **Details**. The details show: the totals (minutes, plays, songs, artists, albums, genres, days with
  music, longest streak), bar charts of listening by hour of the day (24 bars), by weekday and by month, the top 50
  songs, artists, albums and genres as numbered lists with plays and minutes, and what the library holds
  (`stats.library_stats()`: songs, albums, artists, total length and size, formats, Hi-Res Lossless / Lossless /
  Lossy, sample rates and bit depths). Without plays in the period: "No listening yet in this period. Play some
  music and come back."

### Play history

A song is logged (`Library.add_play(track, started, seconds)`) when it stops being the current song (it ended, the
user skipped or stopped, chose another, or quit), with the seconds actually heard: the sum of the position's forward
steps between UI updates while playing, so seeking does not count; songs heard for less than a second are not
logged. `started` is the wall-clock time when it began. Settings → Library has "Clear the play history…".

### Equalizer presets

The preset menu of the equalizer lists the built-in presets, then "My presets" (`PlayerState.eq_presets`), and
"Custom" when the gains match none. A user preset holds the whole equalizer (graphic and parametric). Buttons:
**Save as preset…** (asks for a name; `check_preset_name()` errors are shown under the field), **Rename…**,
**Delete…**, **Import…** (a file dialog for .json and .txt: `eq.import_eq()`; the imported preset is applied and
saved under its name, a taken name getting " 2"; the notes are shown) and **Export…** (save as .json, Real-Hi-Res's
own format, or .txt, Equalizer APO / AutoEQ text: `export_json()` / `export_apo()`, by the extension chosen). The
name of the matching user preset (`matching_eq_preset()`) is shown in the menu.

### Themes

Settings → Appearance → Theme is a menu of `themes.names()`: "Match the system", the built-in themes (Light, Dark,
Midnight, Graphite, Forest, Ocean, Sepia, Rose) and the user's own. Under it the theme editor: **New theme…**
(a copy of the chosen theme, named by `copy_name()`), and for the user's own themes **Rename…**, **Delete…**, a
"Dark theme" switch and a colour button for each of the eight colours (`COLOR_KEYS`, labels from
`COLOR_LABELS`/`color_label()`), which change the app at once; **Import…** and **Export…** (.json). Built-in themes
cannot be edited; editing one offers to make a copy. The user's themes are saved in `themes.json`.

### Theme files (asked for later on 2026-11-24: "import custom theme … via files")

A theme is a file: **Export…** writes one (`export_theme()`, format `real-hi-res-theme`) and **Import…** reads one
into the user's own themes. Besides that, the **themes folder** (`themes.folder()`: "themes" next to
`themes.json`) holds theme files that are used as they are: every `.json` file in it that is a theme appears in the
theme menu after the user's own themes (`themes.load_folder()`, in file-name order; a taken name gets " 2", " 3"…;
other files are ignored). Under the theme menu a line says "Theme files (.json) put into the themes folder appear
in this menu." with **Open themes folder** (creates the folder when needed and opens it in the file manager) and
**Import theme…**. The folder is read again whenever the menu can change: Python each time the menu opens (the
combobox's `postcommand`) and the Settings page is built, Rust when the Settings page is shown and then every two
seconds while it is. A theme from the folder is not edited in the app: the editor says "“Neon” comes from the file
neon.json in the themes folder. Change that file, or make a copy you can change here with New theme…", and offers
New theme… and Export…. The editor's row no longer has Import…: importing is the Import theme… button of that
line. If its file goes away, the theme falls back to "Match the system" (`resolve()`).
The Rust app also imports a theme `.json` dropped onto its window (into the user's own themes, selected at once,
as Import… does); Tk has no drag and drop, so the Python app has only the folder and Import….

### Song information (asked for later on 2026-11-24: "edit meta data ie album author, author release date")

**Edit info…** in every song's menu (right-click in a song table, and the playback bar's **…** for the song
playing) and **Edit album info…** on an album page (all of its songs) open the **Edit Info** window, one at a time
(titled "Edit Info"; at its top the song's title with "artist · album" under it, or the album's title with "artist ·
12 songs"; the cursor starts in Title for one song and in Album for an album, and Enter saves).
Its fields are `tagedit.FIELDS` with `tagedit.LABELS`: Title, Artist, Album, Album artist, Genre, Release date,
Track, Disc, Composer; Release date, Track and Disc have the grey hints of `tagedit.HINTS`. The window first reads
the songs' tags with ffprobe in a background thread ("Reading the information of 12 songs…", fields disabled),
then fills each field with `common_values()`: the songs' value, or, where they differ, an empty field with the
grey note "Several values: kept unless you type here". A field counts as changed when its `clean_value()` differs
from that of what it started with (for a "several values" field: when anything was typed, so emptying such a field
leaves it as it is). **Save** checks the changes
(`check_changes()`, its message in red under the fields, the window stays open), closes the window when nothing
changed, and otherwise saves in a background thread with `save_tags()` ("Saving 3 of 12…"): FLAC, MP3, Ogg Vorbis and
Opus natively, other formats through FFmpeg's stream copy (used only when the copy decodes to the same samples), saved results moved with the new tags, the library index updated.
Then the app refreshes the library (its pages and the LCD show the new values). When every song was saved without a
note the window closes; otherwise it shows `save_summary()` (headline and sentences) with a Close button.
**Cancel** (or Esc) closes it without saving; while saving it cannot be closed.

### Settings → Library

The buttons are: Update the library now; **Scan library** (opens the Quality check page, as in the first version,
with the library folders as its sources, and starts the scan); Download lyrics for the whole library…; Open the
quality checker; Clear the play history….

### Details settled while building both apps

- With one library folder, Folders opens it at once (the breadcrumb starts at that folder's name); the list of
  library folders appears only with several.
- The playlist mosaic shows the covers of up to four different albums of the playlist's songs; with fewer than four
  albums it shows the first album's cover full size.
- Your Stats → Details is a page of its own with a back link.
- New theme… makes the copy at once, named by `copy_name()`, and selects it; Rename… asks for a name.
- Choosing one of your own EQ presets keeps the equalizer on if it was on.
- When nothing plays, Previous, Next, the heart, the dislike button and "…" are disabled; Shuffle, Repeat, the
  volume, EQ, Lyrics and Up Next stay usable.
- A song counts in the play history from one second of listening; the seconds are the forward steps of the position
  while playing (the Python app ignores steps over 1.5 s, the Rust app over 2 s, between screen updates), so seeking
  never counts.

### Language (2.1, asked for on 2026-11-24: "add language settings … for now Japanese and English")

Settings → Appearance → **Language** (`language`, first in the group; "English" or "日本語", each shown in its own
language; default English). Every text the apps show is written in English in the code and passed through the
translation core (`realhires/i18n.py`, `rust/crates/core/src/i18n.rs`): `tr()` / `tr!()` for the apps' own texts
(with `{name}` placeholders filled after translating), `tr_name()` for library names (only the core's placeholders
such as "Unknown Album" are translated, never a real name), and `tr_text()` for the finished texts of the shared core
(verdict summaries, reasons and notes, Fix, lyrics, Edit Info and MusicBrainz messages, Your Stats cards, the time
left, the Playback info labels), which are matched against the catalog's patterns (`{name}` any text, `{name:num}` a
number; a text of "; "-joined parts or of several sentences is translated part by part, and the values a pattern
takes are translated in turn). Settings show `tr(group)`, `tr(label)`, `tr_text(help)` and `tr(choice)` but store
the English values, so `settings.json`, the saved results and `player.json` never depend on the language. The
catalog is `realhires/assets/lang/ja.json`, joined from `i18n/ja/*.json` by `tests/i18n_keys.py --merge`; the Rust
app includes the same file at build time. A text missing from the catalog is shown in English.

- The Rust app changes language at once (egui draws every frame; the scanner's `apply_settings()` calls
  `set_language()`). The Tk app sets the language once per start; when the chosen language differs from the one in
  use, the Appearance section shows "The new language is used when the app starts again." and **Restart now**
  (`PlayerApp.restart()`: saves everything as Quit does, then starts the app again with the same arguments); the
  Quality check page's own Settings window shows the note only. This is an intended difference.
- The command-line tools always write English.
- Japanese glyphs: Tk uses the system's font fallback; the Rust app adds a system CJK font as a fallback to its
  fonts (`look.rs`), so Japanese titles show in either language.

### Song information from MusicBrainz (2.1, asked for on 2026-11-24: "auto add metadata to library (default is off) … add metadata via musicbrainz")

The core is `realhires/musicbrainz.py` and `rust/crates/core/src/musicbrainz.rs` (checked against each other with
`rust/crates/core/tests/python_musicbrainz.json`): `MusicBrainzClient` (one request at a time, at least one second
apart across every client of the app that uses the real clock, the User-Agent "UniversalMusicPlayer/<version> (
<project URL> )", 503/429 waited for with Retry-After or a back-off and retried up to 4 times), `search_releases()`,
`search_recordings()`, `release()` (one lookup with the track list, artist credits, release group with its first
release date and genres, and the works' composers), `match_tracks()`, `changes_for()`, `search_candidates()`,
`find_album()`/`find_song()` (the automatic mode's certain matches) and `run_auto()` with the lookup log (table
`musicbrainz` in `library.sqlite`).

- **Get info from MusicBrainz…** in every song's menu (and so in the playback bar's "…") and **Get album info from
  MusicBrainz…** on an album page (next to Edit album info…) open the **MusicBrainz** window, one at a time: the
  heading as Edit Info's; a search row (Album or Title, Artist, Search; Enter searches) filled from the songs' tags
  (`album_of()` for an album, the title and artist for one song; the folder name or file name when the tag is
  empty), which searches at once; the status line ("Reading the information of 12 songs…", "Searching
  MusicBrainz…", "Reading the release…", "4 of 4 songs found on this release.", "Nothing found on MusicBrainz…");
  **Releases found**: one line per release ("Title — Artist  ·  date · country · formats · N tracks", plus the
  status when it is not Official), the first chosen and read at once; **Changes**: per song "file → number. title"
  (or "file: no matching track on this release, left as it is") and in grey "Field: old → new" ("(empty)" for an
  empty tag) or "Nothing to change"; one checkbox per field (`tagedit.FIELDS`, all on) and **Only fill in empty
  fields** (off), which redraw the changes; the error line (red); **Save** (only when something would change) and
  **Cancel**/Esc; the credit `musicbrainz.CREDIT` in small grey text. Save writes each song's own changes with
  `save_changes()` ("Saving 2 of 4…"), marks them `saved` in the lookup log and refreshes the library; it closes
  when everything was saved without a note, else it shows `tagedit.save_summary()` with Close.
- The date written is the release group's first release date (else the release's), the track "n/total", the disc
  "n/total" only for releases of several discs, the genre the release group's (else the release's) genre with the
  most votes in title case, the composer the works' composers (else their writers), joined with ", ".
- **Add song information from MusicBrainz automatically** (`musicbrainz_auto`, Settings → Library, off by default)
  and **MusicBrainz server** (`musicbrainz_server`). When it is on, every finished library update starts the
  automatic lookup in a thread (one at a time): songs not in the lookup log (or changed since) whose index entry
  misses one of artist, album, album artist, release date, track number or genre are read again with ffprobe,
  grouped by folder and album tag, and looked up (`find_album()`: the same album and artist, room for every song,
  every song found by number or title with the length within 3 s; at most 3 releases read per album;
  `find_song()` for a song without album tag: same title and artist, length within 3 s, the most suitable release:
  official, not a compilation or live album, album before EP before single, earliest). Only empty tags are filled.
  The sidebar's status line shows "Getting song information from MusicBrainz: 3 of 20 albums" and then
  `auto_summary()`; the library is refreshed when something was filled. **Look up the library on MusicBrainz now**
  (Settings → Library) runs it at once, also when the switch is off, and always reports. Quitting stops it.
- Help → About adds `musicbrainz.ABOUT_CREDIT`.

## 2.2 (asked for on 2026-11-24, after 2.0.0 was released; built as PR #4 and released as 2.2.0 after
2.1.0, which brought the language setting and MusicBrainz above. Every text 2.2 adds is translated as the
Language section says.)

The user asked: "Finding Fakes [the Quality check page] should have the same aesthetics as the player. Also remove
the Files, Controls, View and Help tab … from the top, it looks ugly. Implement Bit perfect playback as an option."

### No menu bars

- Neither app shows a menu bar in its window any more: the player's File / Controls / View / Help and the Quality
  check page's File / Edit / View / Help are gone (Tk: the player never calls `root.configure(menu=…)`, so macOS shows
  only Tk's default application menu; egui: no "menu" panel). The standalone 1.x window (`gui.scanner_main()`, only
  used for development) keeps its menu.
- Everything they held stays reachable, and every keyboard shortcut stays as it was:
  - Player: New Playlist… (sidebar), Add music folder / Update the library now / Scan library / Download lyrics for
    the whole library… / Clear the play history… (Settings → Library), Settings (sidebar), Quit (closing the window,
    Ctrl/Cmd+Q), playback controls (the playback bar and the keys), the pages (sidebar and the Library page), Now
    Playing (Lyrics button, Ctrl/Cmd+L), Up Next (its button), Equalizer and Playback info (their buttons).
  - The sidebar always shows **Quality Check** (the check-mark icon) just above Settings at the bottom, selected while
    that page is shown.
  - The Settings page shows a button "About Universal Music Player" under its group list (both apps), which opens the
    About box.
  - The Quality check page gets a **More** button (the player's "…" icon) at the right end of its toolbar. Its menu
    holds, in this order with separators between the groups: Export listed files as CSV… / as JSON…; Fix selected
    files to real resolution… / Fix all fake files to real resolution…; Download synced lyrics for selected songs… /
    for all songs…; Re-scan selected files / Re-scan files checked by older rules; Select all listed files / Copy path
    / Open file location / Open file / Find…; Scan summary… / Show problem files only / Show all files; Delete all
    saved results…. (Add Folder…, Add Files…, the view switch and Settings are buttons on the page.) The right-click
    menu of the list is unchanged.

### Quality check page: the player's look

The page keeps every function, widget name and shortcut (the tests drive them), but is drawn like the other pages,
in the theme's colours (light, dark and custom themes) and the app's fonts:

- A page title "Quality Check" in the page-title font (as "Library"), with the status line under it in secondary
  text (what the status bar said: files, problems, scanning progress and time left, saved results). The old status
  bar at the bottom goes.
- Buttons in the player's style: flat, rounded, the surface colour (the LCD panel colour) with text colour, hover
  slightly darker; the primary action **Scan** filled with the accent colour and white text (Stop takes its place
  while a scan runs). Toolbar: Add Folder…, Add Files…, Clear, the "Quick scan (first 90 s)" switch; on the right
  Scan/Stop, Settings…, More.
- Sources to scan: a rounded panel in the surface colour, rows like a song table, Remove as a small button.
- The view switch (Files / By album / By artist) and the dashboard's Spectrogram / Average spectrum / Lyrics switch
  are segmented controls, drawn like the Equalizer window's Graphic / Parametric tabs.
- The verdict filters are pill-shaped toggles ("chips") with a dot in the verdict's colour and the count
  ("Fake lossless 3"); a chip that is on is filled with the surface colour and drawn in the text colour, one that is off
  is outlined and grey. The search field is drawn like the sidebar's Search box.
- The file list looks like the player's song tables: no borders or grid lines, the same row height and fonts, column
  headings in bold secondary text, hover and selection in the theme's colours; the verdict and "Why" text keep the
  verdict colours. The By album / By artist cards look like the player's album cards (rounded cover, title bold,
  subtitle grey) with the fake/suspicious ring on the cover's corner.
- Dashboard: a rounded panel; the verdict badge as a rounded pill in the verdict colour; the file name bold; the facts
  in two columns (labels secondary, values text colour); the action buttons in the player's style; Details in the
  body font. The plots keep their dark plot area inside a rounded frame.
- The windows opened from the page (Settings, Fix, Fix finished, Download synced lyrics, Scan summary, the find bar)
  use the theme's colours and fonts too.

### Bit-perfect playback

New setting `bit_perfect` (Playback group, "Bit-perfect playback", default off; not a Detection setting): "Send each
song's samples to the sound device exactly as they are in the file: at the song's own sample rate, without the
equalizer, the volume control or resampling, and with the device to this app alone where the system allows it
(exclusive mode on Windows, the ALSA hardware device on Linux; on macOS the device is switched to the song's rate).
Set the volume on your amplifier or DAC. While a song plays this way, other programs may not be heard." Turning it
on or off applies from the next song (and at once to the song playing, which restarts at its position).

While it is on:

- **Signal path.** FFmpeg decodes with no `-ar` and no filters (`-af` only for a mono song: `pan=stereo|c0=c0|c1=c0`,
  a pure copy). Integer samples of up to 24 bits become 32-bit floats exactly, so the samples that reach the output
  are the file's own. The equalizer is bypassed (its settings are kept; the Equalizer window shows the note
  "Bypassed while bit-perfect playback is on."), and the gain is exactly 1: the volume slider is disabled and shows
  full volume, the saved volume is kept and used again when the setting is turned off. Mute still silences.
- **Output format.** The device is opened at the song's sample rate, stereo, in the first format it supports from:
  16-bit songs I16, I32, F32; 24-bit songs I32, F32; 32-bit integer songs I32, F32; float and lossy songs F32, I32,
  I16. Floats become integers as round(x · 2^(bits−1)), clamped, which gives back the file's integers.
- **Device access.** Windows: WASAPI exclusive mode (Python: miniaudio's `shareMode` exclusive; Rust: a small WASAPI
  exclusive output beside cpal). macOS: the device's nominal sample rate is set to the song's (cpal does this; the
  Python output does it through CoreAudio). Linux: the ALSA hardware device (`hw:` of the chosen output device's card,
  or of card 0) is opened directly. When this fails (another program or the sound server holds the device, or it
  cannot play that rate or format) the song plays through the normal path instead, and Playback info says why.
- **Gapless** joins only songs of the same sample rate and output format; otherwise the device is reopened.
- **Playback info**, section Output, gains a line "Bit-perfect": "Off" (setting off); "Yes: 24-bit, 96 kHz,
  exclusive mode (WASAPI)" / "Yes: 16-bit, 44.1 kHz, ALSA hardware device hw:0" / "Yes: 24-bit, 96 kHz, the device
  runs at 96 kHz (CoreAudio)"; or "No: " and every reason that applies, as full sentences:
  "The device cannot play 192 kHz, so the song is resampled to 96 kHz.", "The device is in use by another program,
  so the sound is mixed by the system.", "The device has no 24-bit format, so the samples are converted.",
  "This 6-channel song is mixed down to stereo.", "The 32-bit samples are played with 24-bit precision.",
  "The song is decoded from a lossy format, so it is played as decoded." (the last one does not make it "No": lossy
  songs say "Yes (decoded lossy audio): …"). The Output line names the format the device really gets ("96 kHz,
  24-bit integer in 32-bit, stereo").
- The decisions and texts come from one core module in both versions (`realhires/bitperfect.py`,
  `rust/crates/core/src/bitperfect.rs`), checked against Python's answers with parity vectors, so both apps say the
  same thing. The tests check bit-exact output through the manual output: 16-bit, 24-bit and mono FLACs come out of
  the engine as exactly the integers FFmpeg decodes, also with the volume at 50 % and the equalizer on.

### Now Playing and lyrics (asked for later on 2026-11-24)

- "When user is in full screen and unclicks lyric button, the album cover should be in the centered and the user
  shouldn't be put back in All songs": on Now Playing the **Lyrics button** (and Ctrl/Cmd+L) hides the lyrics
  column and the page stays, showing the cover in the middle with the title and the artist under it (no "Get
  lyrics" button, since the song has lyrics); pressed again it shows the lyrics. On any other page it opens Now
  Playing with the lyrics shown. The button is drawn "on" only while the lyrics are shown. The hidden state lasts
  for the session (`lyrics_hidden`). A click on the **LCD's cover** opens Now Playing, or goes back to the page
  before when Now Playing is shown. (The Rust app gained Ctrl/Cmd+L, which it lacked.)
- "LRCGET lyrics aren't synced": the Rust app showed the user's lyrics as plain text. The order in which a song's
  lyrics are found (`lyrics.read_lyrics_file()`, both versions) is now: the .lrc next to the song; the lyrics
  inside the file when they are synced; the .txt next to the song; the plain lyrics inside the file. Before, a
  plain .txt beat synced lyrics inside the file, which is what LRCGET's "plain text" and "embed" exports together
  leave behind. LRC kept in a .txt or in the plain-lyrics tag (USLT, UNSYNCEDLYRICS), recognised by
  `embed.looks_synced()` (at least half of the non-empty lines start with a timestamp), counts as synced.

### Album covers are kept (asked for later on 2026-11-24)

"When user scrolls down album or artist, it loads as it goes. Should be cached so it doesn't load in every time
user goes down their library." Covers were made by FFmpeg from the songs whenever a card first came into view,
after every start (and the Python app forgot all but 600 images). Now, in both versions:

- **On disk**, shared by both versions: `covers/` in the data folder (`store.data_dir()`; `$REALHIRES_COVERS`
  overrides it, and the tests point it at a temporary folder), one PNG per album and size. The name is the SHA-1
  of a key made from the size, the album's track paths as given to `covers.cover_png()` (the first four), each
  one's file size and `mtime_ns`, and the `mtime_ns` of their folders, so a changed song or a new folder image
  gives a new entry; an album without a cover is remembered as an empty file. Both versions compute the same name
  (`covers.cache_name()` / `covers::cache_name()`, checked by a parity vector). Files are written through a temp
  file and `os.replace`; a damaged or unreadable file is made again. The folder is kept under 500 MB: when it grows
  past that, the least recently used files (their modification time is touched when read) are deleted.
- **In memory**, a cover once shown stays for the session (Python: no eviction of the grid size, a generous limit
  for the large sizes; Rust: textures are kept, and the library refresh no longer drops them unless the songs
  changed).
- **Made ahead**: once the library index is ready, one background worker makes the grid-size covers of every album
  that has none on disk yet, after the covers asked for on screen (those always go first), so the next scroll finds
  them ready; it stops when the app quits.
- The sizes are the same in both apps where they can be, so the disk entries are shared.

### Ten more requests (later on 2026-11-24)

The user's words, then what both apps do:

1. "Equaliser's my preset delete button doesn't work": find out why in both apps and fix it. Deleting one of the
   user's own presets (after the confirmation) removes it from `player.json` (`PlayerState.delete_eq_preset()`), from
   the preset menu, and leaves the current EQ settings as they are (the menu then shows "Custom" when they match no
   preset). A test presses the button's handler and checks all three.
2. "Quality tab should display bit/kHz instead of the current display": the **Quality column** of every song table
   shows `musiclib.resolution()` ("24-bit/96 kHz", "16-bit/44.1 kHz", "32-bit float/192 kHz"); lossy songs show
   the codec, bit rate and rate ("MP3 · 320 kbps · 44.1 kHz"). Sorting by it sorts by bits, then rate. (The LCD badge
   keeps "Hi-Res Lossless" / "Lossless".)
3. "When user scrolls down library, it lags a little": scrolling the song tables, album grids and the new grids of
   item 10 must stay smooth on a library of 5,000 songs and 500 albums: nothing expensive per frame or per scroll
   step (no FFmpeg, no file access, no re-sorting, no per-pixel image work on the UI thread; draw only the visible
   rows/cards). Measure before and after (Tk: time a scroll step's redraw; egui: frame time) and say what changed.
   (Cover loading itself is handled by "Album covers are kept".)
4. "Allow user to merge genre folders, rename them etc": on the Genres page a genre's menu (right-click, and "…" on
   its page) has **Rename genre…** (a name dialog; every song of that genre gets the new name through
   `tagedit.save_tags()`, so the audio is untouched and the saved results and the library follow) and **Merge
   into…** (choose another genre from a list, or type a name; the songs of this genre get that genre). The Genres
   page also lets several genres be selected (Ctrl/Cmd-click) and merged at once ("Merge N genres…", asking for the
   resulting name, defaulting to the genre with the most songs). A confirmation says how many files will be
   changed; the outcome is reported as with Edit Info (`save_summary()`); files that cannot be changed are listed.
   Composers and Years get Rename… the same way (Years: the date's year is replaced, the rest of the date kept).
5. "The lyrics should display whether lyrics are synced or just a text": Now Playing shows a small label above the
   lyrics: "Synced lyrics" or "Lyrics (not synced)", plus where they come from in secondary text (".lrc file",
   "inside the file", ".txt file").
6. "When user scrolls while lyrics are synced, a button should appear so they can go back to where the lyrics are
   synced": while the user has scrolled synced lyrics away from the current line (the 3-second pause of the
   automatic scrolling), a pill button "Back to the current line" floats at the bottom of the lyrics column; a click
   scrolls back at once and resumes following. The button stays until clicked or the user scrolls the current line
   back into view; the automatic scrolling stays paused while it is shown (no 3-second timeout any more).
7. "Allow user to increase or decrease lyric font size and the jacket, album artist name on the left should be in
   the middle as a set": two small buttons "A−" / "A+" by the lyrics label change the lyrics' size in steps (16 to
   48 px, default 26 (Rust 24 → 26 as well)), saved in `player.json` as `lyrics_size` (PlayerState in both versions,
   with the parity vectors); and the cover with the title and "Artist — Album" under it form one block centred
   vertically (and horizontally) in the left half, as the centred layout without lyrics already is.
8. "When user clicks the jacket/album name or the artist name, it should bring them to the album or artist": on Now
   Playing the cover and the album name open the album page, the artist name opens the artist page (pointer cursor
   and underline on hover). The same in the playback bar's LCD: the title/album line opens the album, the artist
   opens the artist (the LCD's cover keeps opening/leaving Now Playing).
9. "Get lyrics button doesn't work": find out why in both apps and fix it (the Now Playing "Get lyrics" button and
   the song menus' Get lyrics): it must open the lyrics window for that song and start the download at once, and
   when lyrics are saved, Now Playing shows them without leaving the page. A test presses it against `FakeLrclib`.
10. "The artist, composer, year, genre etc tab should display like album, not series of artist names": Artists,
    Album Artists, Composers, Years and Genres are card grids like Albums: each card a square picture (an artist,
    album artist or composer: a mosaic of up to four of their album covers, or the one cover when there is one; a
    genre or year: the same mosaic of its albums) with the name in bold and "N albums · M songs" in grey under it,
    sorted as the list was. A click opens the same page as before. Unknown names keep their "Unknown …" label.

### Play Next and Add to Queue (asked for later on 2026-11-24)

"When user right clicks on song, user should have the option to add to queue or play next." Every song menu
(right-click, and "…") starts with **Play Next** and **Add to Queue**. The play queue (`playqueue.PlayQueue`, both
versions, parity vectors `user_runs` in `python_queue.json`) keeps the songs the user queued in `queued`: Play Next
puts the song before the songs queued earlier, Add to Queue after them. When the current song ends (or Next is
pressed), the first queued song plays before the list goes on, whatever the shuffle and repeat modes; repeat album
treats a queued song as an interruption (`inserted`: after it the album being repeated goes on). After Previous,
Next first walks back up through the songs already played (`top`). Up Next lists those songs, then the queued ones,
then the rest of the list; a double-click there (`jump()`) plays that song, dropping the queued songs listed before
it (or all of them when the chosen song comes from the list). With nothing playing, both items start playing the
song. The queue lasts for the session. The gapless look-ahead asks a copy of the queue in both apps, so the queued
song follows without a gap.

### Eight more requests (later on 2026-11-24)

1. "Near the lyrics, user should be able to choose lyrics or queue for the songs that'll be playing or
   description": on Now Playing, the right half has a segmented switch at its top: **Lyrics** | **Up Next** |
   **Details**. Lyrics is what was there (with the label, size buttons and Back button of the earlier requests);
   Up Next is the queue as in the Up Next panel (`up_next()`, Play Next / Add to Queue songs first, double-click
   jumps); Details is the song's description: title, artist, album, album artist, composer, genre, year, track and
   disc, duration, the format line ("FLAC · 24-bit/96 kHz · 2,304 kbps"), the file's path, the comment tag when
   there is one, and the Quality check verdict with its summary when the results store has one. The choice is kept
   for the session; the Lyrics button and Ctrl/Cmd+L select Lyrics (and hide/show it as before when it is selected).
2. "Add calendar on top of the current stats page, user can click on date to see what songs they played at what
   time": Your Stats starts with a month calendar (‹ month ›, Monday first, days with plays tinted by minutes
   played, today outlined); a click on a day lists that day's plays under the calendar: time (local, "14:05"),
   title, artist, minutes listened, newest first; a click on a song plays it. Uses `Library.plays(since, until)`
   with the day's local midnight bounds (`stats` gets a pure helper for the month's per-day minutes, with parity
   vectors).
3. "When user clicks on album, there's a list of songs but unlike the list on All Songs, it doesn't display artist,
   album, genre and quality but only time": the album page's track list uses the same columns as All Songs (number
   first, then Title, Artist, Album, Genre, Time, Quality, the heart), sortable the same way, in track order by
   default.
4. "Make it so default display for Quality is Lossless etc. but when user hovers over it, it displays the bit/Hz":
   this replaces item 2 of "Ten more requests": the Quality column shows `musiclib.quality()` ("Hi-Res Lossless",
   "Lossless", "MP3 320 kbps") and hovering a cell shows `musiclib.resolution()` as a tooltip ("24-bit/96 kHz",
   lossy songs "MP3 · 320 kbps · 44.1 kHz"). Sorting by the column sorts by bits, then rate.
5. "Mouse 4 and 5 should be usable": the mouse's back and forward buttons go back and forward between pages, like a
   browser. Both apps keep a forward list (cleared when a new page is opened); Alt+Left / Alt+Right (Cmd+[ / Cmd+]
   on macOS) do the same. Tk: X11 buttons 8/9, Windows and macOS buttons 4/5 (not the wheel); egui:
   `PointerButton::Extra1`/`Extra2`.
6. "User should be able to make edit window big or small": the song information window can be resized; the fields
   grow with its width, and its size is kept for the session.
7. "Instead of edit info, should be labeled as properties": the menu items read **Properties…** (a song) and
   **Album Properties…** (an album page), and the window's title is "Properties". (Names in the code may stay.)
8. A new app icon from the user's picture of a maki roll: the roll cut out of its (painted) checkerboard background
   onto transparency, centred with a small margin, as `realhires/assets/icon.png` (which every package, window and
   menu entry uses); `packaging/make_icon.py` makes it from `packaging/icon-source.png`.

### Several artists in one field (asked for later on 2026-11-24)

"When there are several artists, as an example 'Mariah Carey, Michael Bublé', and they click 'Michael Bublé', it
should send the user to their library's Michael Bublé page." `musiclib.split_artists(name, known)` (both versions,
vectors `python_artists.json`) gives the artists of an artist field: it always splits at ';' (how FFmpeg joins
several ARTIST tags) and at "feat." / "ft." / "featuring" (also in brackets), and splits at ", ", " & ", " x " and
" / " only when one of the names it gives stands on its own somewhere in the library (`known_artists()`: the
artist and album artist fields of every song, split at the first kind only), so "Earth, Wind & Fire" and "Simon &
Garfunkel" stay whole. Artists and Album Artists list every artist of the field on its own, and a song is on the
page of each of its artists. Wherever an artist is shown as a link (the album page's artist line, Now Playing, the
LCD, the Details view), each artist of the field is its own link, with the separators between them as plain text,
and a click opens that artist's page (`open_artist(name)`; a name that is not an artist of the library opens the
first artist it splits into).

### Choosing the columns of song lists (asked for later on 2026-11-24)

"Allow user to select what they tab for all songs, folders and album. Currently shows title, artist, album, genre,
time, quality on the top." A right-click on a song table's column headings (and on its empty area right of the
last heading) opens a menu of check items, one per column; ticking one shows that column, unticking hides it.
Title cannot be hidden. "Reset columns" returns to the default. The choice is kept per kind of list, in
`player.json` as `columns` (PlayerState, both versions, parity vectors): `"songs"` (All Songs, and every other list
of songs: genre, year, composer and artist pages, playlists, Liked, Disliked, search), `"folders"` (Folders) and
`"album"` (album pages); Recently Played keeps its own fixed columns. The columns, in this order: # (track number),
Title, Artist, Album, Album Artist, Composer, Genre, Year, Disc, Time, Quality (with the resolution tooltip),
Format (codec, e.g. "FLAC"), Bit rate ("1,411 kbps"), Date added (the file's date), File (its name), and the heart.
Defaults: songs and folders: Title, Artist, Album, Genre, Time, Quality, heart; album: #, Title, Artist, Album,
Genre, Time, Quality, heart. Any shown column can sort the list, as before. An unknown column name in the file is
ignored; an empty list falls back to the default.

### Inspecting and changing a cover (asked for later on 2026-11-24)

"When user is playing a song and entered fullscreen then clicks on jacket, allow user to inspect, change jacket
photo etc." On Now Playing, a click on the cover opens the **Cover** window (this replaces "the cover opens the
album" of the earlier request; the album name still opens the album, and the LCD's cover still opens/leaves Now
Playing). The window shows the cover as large as fits (resizable window), with under it: its size in pixels, its
format and where it comes from ("Inside 01 Song.flac", "cover.jpg in the album's folder"); and the buttons:

- **Change cover…**: choose a JPEG or PNG image; a choice "All N songs of this album" (default) or "This song only",
  and a check "Also save it as cover.jpg in the album's folder" (off by default; a PNG is saved as cover.png).
  The picture is embedded as the front cover: FLAC natively (a PICTURE metadata block, type 3, replacing earlier
  front covers), MP3 natively (an APIC frame, type 3, replacing earlier ones), Ogg Vorbis/Opus natively
  (METADATA_BLOCK_PICTURE), and other formats FFmpeg can write through a stream copy with the picture as an
  attached_pic stream, kept only when the audio decodes to the same samples (`pcm_digest()`), as Edit Info does;
  formats that cannot hold a picture (WAV, AIFF without ID3, APE, DSF…) are listed as not changed (and only the
  folder file helps them). Only the tag changes; the saved scan results follow (`ResultStore.retag()`), the
  library re-reads the songs and the cover caches show the new picture at once. A summary says what was changed.
- **Save image as…**: writes the picture as it is stored (JPEG or PNG) to a chosen file.
- **Remove cover**: after a confirmation, removes the embedded front cover from the album's songs (the same choice
  as above); a folder image is not deleted.
- **Show album**, and **Close** (Esc).

The core lives with the tag editor: `tagedit.set_cover(paths, image_bytes, mime, cfg)` / `remove_cover(...)`, and
`covers.cover_source(paths)` (where the shown cover comes from), in both versions, with the native FLAC/MP3/Ogg
results byte-identical between them (parity vectors) and tests on real files.

### Acquire Metadata (asked for later on 2026-11-24, after 2.1.0 was released)

The user asked: "User should be able to right click songs/album and press aquire meta deta to add metadata for
song/album via musisbrainz".

- The MusicBrainz items of 2.1 are named **Acquire Metadata…** (Japanese: メタデータを取得…) in both apps: in every
  song's right-click menu (and the playback bar's "…"), where "Get info from MusicBrainz…" was, and on the album page,
  where "Get album info from MusicBrainz…" was. They open the same MusicBrainz window as before, one at a time.
- A song's Acquire Metadata… looks up that song (song lists select one song at a time in both apps).
- Every album cover card (Albums, Recently Added, an artist's albums, search results) has a right-click menu:
  **Play**, **Shuffle**, **Play Next**, **Add to Queue**, then **Acquire Metadata…** (the MusicBrainz window for the
  whole album, as from the album page) and **Album Properties…** (the Properties window for all its songs).

## 2.3 (asked for on 2026-11-25, after 2.2.0 was released)

Fifteen requests, all in both apps, with every new text translated into Japanese as the Language section says, and
the shared maths and data checked against Python's answers with parity vectors, as before. Where a request names a
place ("full screen") it means Now Playing, the page with the cover on the left.

### Up Next can be edited

"Allow user to drag to change order or right click and select to remove from queue." Up Next is three parts (see
"Play Next and Add to Queue"): the songs stepped back over with Previous (`order[pos+1..=top]`), the queued songs
(`queued`), then the rest of the order (`order[top+1..]`). `PlayQueue` (both versions) gains:

- `remove_upcoming(index)` (an index into `up_next()`; returns the path, or None when out of range): a stepped-back
  song is removed from `order` (and `top` moves down by one), a queued song from `queued`, a song of the rest from
  `order`. A removed song that came from the list (stepped-back or rest) goes into `removed`, unless it is the song
  playing: `_upcoming()` and `_restart_album()` leave `removed` songs out, so a removed song stays out when the order
  is built again (another shuffle mode, repeat album) until a new list is played (`start()` empties `removed`).
- `move_upcoming(src, dst)` (indices into `up_next()`, `dst` counted after the move; False when either is out of
  range or they are equal): the song is taken out of its part (without going into `removed`) and put at `dst`, into
  the part of the song it is now placed before (at the very end: the rest of the order). So a song dragged to the top,
  before queued songs, becomes queued; a queued song dragged among the list's songs becomes part of the order.
  `inserted` positions are kept pointing at the same songs whenever `order` changes.
- Parity vectors `edit_runs` in `python_queue.json` (moves and removals within and across the three parts, then
  next/previous/shuffle/repeat album).

In the Up Next panel and in Now Playing's Up Next view: a row can be dragged to another place (a press becomes a drag
after 5 px; a line in the accent colour with a small circle at its left end shows the gap it will drop into; the
list scrolls when the pointer is near its top or bottom edge; only the primary button drags), and a right-click
opens a menu with one item, **Remove from Up Next**. After either, the song that follows gaplessly is set again.

### 1. Change Cover… in the menus, and Search Cover Online

"Change cover tab should be displayed when user right clicks then selects to change jacket. Next to change cover,
there should be option to search cover; it should redirect user to browser that searches image of [artist name]
[album name]." Every song menu (right-click, and "…"), every album card's menu and the album page's menu get
**Change Cover…**, which opens the Cover window for that song's album (the window of "Inspecting and changing a
cover", with Change cover… as before). In the Cover window, right after **Change cover…**, a button **Search Cover
Online** opens the system's browser at Google Images, `https://www.google.com/search?tbm=isch&q=` + the URL-encoded
query `<album artist (else artist)> <album>` (a song without an album: `<artist> <title>`; parts that are unknown are
left out). The query is built by one core function in both versions (`covers.cover_search_url()`), with vectors.

### 2. The Library page as a list

"User should have the option to have All Songs, Folders etc. to be in a list." New setting `library_layout`
(Appearance group, label "Library page", choices "Tiles" (default) and "List"; not a Detection setting). List shows
the Library page's entries (the fourteen, then the custom tabs of item 6) as rows 44 px high, the width of the page:
the entry's icon in the accent colour, its name in bold and its count in secondary text on the right, a line between
rows, hover in the theme's hover colour; a click opens the entry as a tile click does. The Library page's title row
gets two small icon buttons on its right (tiles, list) that switch the same setting (the one in use is drawn on).

### 3. Choosing genres to merge

"User should be able to merge the genre folders/lists then rename them." The Genres page's title row gets a pill
button **Select**. In select mode a click on a card toggles it (a filled check mark in the accent colour in the
cover's top-right corner, a ring in the accent colour around the cover) instead of opening it, and the title row
shows "N selected", **Merge…** (enabled from two genres), **Rename…** (exactly one) and **Done** (leaves select mode
and clears the selection). Merge… asks for the merged genre's name in one dialog (a combobox listing the selected
genres, prefilled with the one with the most songs, and free to type a new name), then confirms how many files will
change, as Merge N genres… did. Ctrl-click (Cmd-click on macOS, where Ctrl-click is a right-click) still selects
without select mode, and the page's hint names the right key for the system.

### 4. Instrumental songs: the cover in the centre

"If song is instrumental, jacket should be automatically centered." When the song's lyrics are marked instrumental
(`[au: instrumental]` from LRCGET/LRCLIB) and Now Playing shows the Lyrics view, the page uses the centred layout: the
cover with the title and the artist under it and "Instrumental" under them in secondary text (no lyrics column, no
Get lyrics). Up Next and Details keep the two halves.

### 5. Back from Now Playing

"After clicking on a song then full screening, there should be an option to return in the full screen top left.
Return to whatever was the last screen, i.e. search bar result, all songs etc." Now Playing gets a back button at
its top left: a chevron and the name of the page it returns to ("All Songs", "Search", the album's name, "Genres",
a playlist's name…), drawn in the accent colour, pointer cursor. A click goes back to that page as the mouse's back
button does (a search page comes back with its text and results). It is shown only when there is a page to go back
to.

### 6. Custom tabs

"Allow user to add custom tabs like High Res or anything via files. Make it benefit the fact this is open source."
A **tab** is a saved set of rules that picks songs from the library, shown in the sidebar like a playlist. No code
runs, so a shared tab file is safe to open.

- Core module `realhires/smarttabs.py` / `rust/crates/core/src/smarttabs.rs`. A tab is JSON:
  `{"format": "universal-music-player-tab", "version": 1, "name": "Hi-Res", "match": "all" | "any", "rules": [{"field":
  …, "op": …, "value": …}, …], "sort": <field>, "descending": false, "limit": 0}`. Fields: text `title`, `artist`,
  `album`, `album_artist`, `genre`, `composer`, `format` (the codec as the Quality column's tooltip names it, e.g.
  "FLAC", "MP3"), `folder` (the song's folder path), `quality` ("Hi-Res Lossless", "Lossless" or "Lossy"); numbers
  `year`, `bits` (0 for lossy), `sample_rate` (Hz), `bit_rate` (kbps), `duration` (seconds), `added_days` (days since
  the file's modification time); yes/no `lossless`, `liked`, `disliked`. Operators: text `is`, `is_not`, `contains`,
  `not_contains`, `starts_with` (case-folded); numbers `=`, `!=`, `>`, `>=`, `<`, `<=`; yes/no `is`. `matches(tab,
  track, ctx)` (ctx: liked and disliked sets, the time now) and `tab_songs(tab, tracks, ctx)` (the matching paths,
  sorted by `sort` then title, at most `limit` when it is above 0). `check_tab()` raises `TabError` with a full
  sentence for a missing name, an unknown field or operator, a value of the wrong kind, or a format/version it does
  not know. `TEMPLATES`: "Hi-Res" (quality is "Hi-Res Lossless": the rules are a flat all/any list, so the badge's
  own definition stands in for "lossless and (bits >= 24 or sample_rate > 48000)"), "Lossy" (lossless is no),
  "Added in the last 30 days" (added_days <= 30, sorted by added_days), "Long songs" (duration >= 600). A tab
  without rules has every song; a new rule starts with an operator every song passes (text contains "", number
  >= 0, yes/no is Yes), so the count does not drop to 0 before a value is typed; Quality's value is chosen from a
  menu (shown translated, stored in English). Names are unique case-insensitively; at most 100 rules, a limit of
  0..100000, files over 1 MB are skipped.
- Files: one `.json` per tab in the tabs folder (`tabs` in `store.data_dir()`, `$REALHIRES_TABS` overrides it),
  read in file-name order (`load_tabs()`, which skips files that are not tabs and says which); `save_tab()` (written
  through a temp file), `delete_tab()`, `export_tab(tab, path)`, `import_tab(path)` (copied into the folder; a taken
  name gets " 2", " 3"…), `tab_file_name()`. Both versions write the same bytes (vectors `python_tabs.json`: rule
  evaluation on invented tracks, every check message, file round trips).
- Sidebar: a heading **Tabs** after Playlists, with each tab (a filter icon) and **New Tab…**. The Library page lists
  the tabs after its fourteen entries, with their song counts. A tab's page: its name as the title, "N songs, M
  minutes", Play and Shuffle, and the song table (column kind "songs"). A right-click on a tab in the sidebar (and
  "…" on its page): **Edit Tab…**, **Export Tab…**, **Delete Tab…** (confirmed; the file is deleted) and
  **Colour Label** (the label follows a rename and goes with a delete). Saving a new tab, importing one or dropping a
  tab file opens its page; renaming a tab renames its file.
- The tab window (New Tab… / Edit Tab…): Name; "Start from" (the templates, New Tab… only); "Match all / any of these
  rules"; one row per rule (field menu, operator menu fitting the field, value: an entry, or Yes/No); Add rule and a
  remove button per row; Sort by (field menu and Descending); Limit (0 = no limit); a live line "N songs match"; Save
  and Cancel. Errors from `check_tab()` are shown under the rules and keep the window open.
- Settings → Library gets **Import tab…** and **Open tabs folder**. The Rust app also imports a tab file dropped onto
  its window (as themes). The folder is read at start, after every change made in the app, and whenever the Library
  page or a tab's page is shown, so files copied into it appear without a restart.

### 7. The equalizer with bit-perfect playback

"Is it possible for bit perfect and EQ to work together? Currently Bypassed while bit-perfect playback is on." While
bit-perfect playback is on and the equalizer is on, the equalizer is applied (it is no longer bypassed); everything
else of bit-perfect playback stays: the device runs at the song's own rate, in exclusive mode / on the hardware
device, and the volume control is not used (gain 1, the slider disabled as before). The equalizer runs in 64-bit
float on the decoded samples; when the device takes integers, the result is converted with TPDF dither (±1 LSB of the
output format) and clamped. With the equalizer off the output is bit-perfect exactly as before (the tests' bit-exact
checks keep passing). Playback info's "Bit-perfect" line then reads "No: The equalizer is on, so the samples are
changed. The device still runs at 96 kHz in exclusive mode." (the rate and the access from the plan), and the
Equalizer window's note reads "Bit-perfect playback is on: with the equalizer on, the sound is not bit-perfect. The
device still gets the song's own sample rate." The texts come from `bitperfect` in both versions (vectors).

### 8. The Equalizer window's close button

"Equalizer X button is not on the actual right." In Japanese (and with the bit-perfect note) the Rust window's
content was wider than its fixed width, so the content overflowed and the title bar with its X kept the smaller
width. Both apps: the Equalizer window is as wide as its content in every language, and its title bar (with the
close button) spans the whole window. The preset row wraps to a second line rather than overflowing when it is too
wide. A test opens the window in Japanese with bit-perfect on and checks that no widget lies outside the window.

### 9. Colours

"Currently quality color is all gray, allow user to customize the color code each quality type. In fact, allow user to
color code anything."

- **Every colour the app draws is a theme colour.** Besides the eight `COLOR_KEYS`, a theme may set the colours that
  are now derived from them (`EXTRA_COLOR_KEYS`, each with a label and its derivation from the eight, in
  `themes.py`/`themes.rs`): `row_hover`, `zebra`, `selection`, `field`, `placeholder`, `placeholder_icon`,
  `disabled`, `on_accent`; the quality colours `quality_hires`, `quality_lossless`, `quality_lossy`; the verdict
  colours of the Quality Check page (`verdict_fake_lossless`, `verdict_fake_hires`, `verdict_suspicious`,
  `verdict_real_hires`, `verdict_cd`, `verdict_lossy`, `verdict_error`); and `calendar` (the tint of days with plays).
  `themes.colors(theme)` returns all of them (a theme's own value, else the derivation), with vectors. Built-in
  themes keep their look, except that the quality colours are no longer grey: Hi-Res Lossless in gold (`#c9a227`
  in light themes, `#e0b84a` in dark ones), Lossless in the accent colour, Lossy in the secondary text colour.
- The theme editor shows the eight colours as before, then a section **More colours** with every extra colour (its
  swatch shows the derived colour while it is not set, and a small Reset button returns it to derived). Exported and
  imported theme files carry the extra colours that are set; older files without them keep working.
- **Quality colours** are used by the playback bar's quality badge (outline and text) and the Quality column of the
  song tables (the text).
- **Colour labels.** Every song menu, album card menu, artist/album artist/genre/year/composer card menu, playlist
  menu and tab menu gets **Colour Label ▸** with Red, Orange, Yellow, Green, Blue, Purple, Grey (fixed colours that
  read on light and dark themes), **Custom…** (the system's colour chooser) and **None**. Labels are kept in
  `player.json` as `labels`: `{"song": {path: "#rrggbb"}, "album": {album key: …}, "artist": {…}, "album_artist":
  {…}, "genre": {…}, "year": {…}, "composer": {…}, "playlist": {id: …}, "tab": {name: …}}` (PlayerState
  `label_of(kind, key)`, `set_label(kind, key, color or None)`, vectors). A label is drawn as a dot 8 px wide: before
  the title in song tables (and Up Next), at the bottom-left corner of a card's cover, and after a playlist's or tab's
  name in the sidebar.
- As built: both apps use the same derivations (the Python app's former ones), so the Rust app's text fields are the
  "Text fields" colour (grey in Light) and its missing covers the grey placeholder. "Selected item" colours the
  sidebar selection (and the selected song row in Rust; Python's tables keep their accent-filled row). Not theme
  colours: the Quality Check card rings, the plots, the error/warning/ok texts, the Your Stats hero cards and egui's
  text-selection highlight. Label keys of artists, album artists, genres and composers are case-folded, since the
  groupings join names in any case; albums use the album key, years are kept as written, playlists by id, tabs by
  name. A playlist's menu (right-click in the sidebar or on a Playlists card) has Play, Shuffle, Rename…, Delete… and
  Colour Label. The Rust editor's More colours grid drops to one column below 660 px.

### 10. Lyrics filling Now Playing

"Allow synced lyrics to be full screen." The lyrics header (next to A− / A+) gets a button **Lyrics only** (an
expand icon; on while active). While it is on, the cover column is hidden and the lyrics use the whole page width,
centred, one step (4 px) larger than the chosen size; the header with the Lyrics / Up Next / Details switch stays.
Pressed again, or Esc, brings the cover back. It lasts for the session (`lyrics_wide`) and applies to synced and plain
lyrics (an instrumental song or a song without lyrics keeps the centred cover).

### 11. Swiping the cover to the album's songs

"Allow user to drag/swipe jacket while in full screen to see album's playlist and make the current song's title
bold." On Now Playing, dragging the cover sideways by more than 60 px (or a horizontal scroll / trackpad swipe over
it) turns the left half to the album's songs: a list of the album's tracks in track order (number, title, time), the
song playing in bold with the speaker icon, hover in the theme's colour, a double-click plays that song with the album
as the list. Two small dots under it (cover, songs) show which one is shown and switch on a click; swiping the other
way returns to the cover. A click on the cover without a drag still opens the Cover window. The choice lasts for the
session and follows the song (a song from another album shows that album's songs).

### 12. Properties: Acquire Metadata and searching the web

"Acquire data should be under properties. Additionally acquire data should have option for google as well: if they
select a song then want to acquire the data for composer, allow user to press a button that redirects them to their
browser with an already googled result, then if copied, autofill."

- In every menu, **Acquire Metadata…** comes right after **Properties…** (and Album Properties…).
- The Properties window gets **Acquire Metadata…** at the bottom left (it opens the MusicBrainz window for the same
  songs, as the menu item does).
- Every field of the Properties window gets a small **Search** button (a magnifier icon, tooltip "Search the web for
  the composer") at its right end. A click opens the browser at `https://www.google.com/search?q=` + the URL-encoded
  query: for a song, `"<title>" <artist> <field>`; for several songs (Album Properties), `"<album>" <album artist>
  <field>`; the field's English name in lower case ("composer", "release date" for the date, "genre", "album artist",
  "track number", "disc number"…), or its Japanese name when the language is Japanese. The query comes from one core
  function (`tagedit.web_search_url(field, values, language)`, vectors).
- After the click the window watches the clipboard for up to two minutes (Tk: `clipboard_get()` every 500 ms; Rust:
  the system clipboard through `arboard`): the first new text that differs from what the clipboard held at the click,
  trimmed, one line, at most `MAX_VALUE` characters, is put into that field (the field flashes in the accent colour
  and the note "Filled in from the clipboard." appears under the fields). It is not saved until Save. Clicking
  another field's Search, closing the window or Save stops the watch. A note under the fields says, while watching:
  "Copy the answer in your browser and it will be filled in here."

### 13. Your Stats: the day's songs beside the calendar

"If user press your stats then clicks on a date, use the space next to calendar, it's wasted space." The calendar
keeps its size on the left, and the day's plays (and the hint "Click a day to see the songs you played on it.") fill
the space to its right, as tall as the calendar and scrolling inside it when longer. When the page is narrower than
the calendar plus 360 px, the list goes under the calendar as before.

### 14. Details in the centre

"When user clicks details, details should be centered like the jacket." Now Playing's Details view is one block
centred vertically in the right half (and horizontally, the labels and values kept in their two columns), as the cover
block is in the left half; a long file path wraps within the block. When the block is taller than the page it starts
at the top and scrolls.
