Universal Music Player (Rust version) - music player and fake hi-res audio detector
===================================================================================

This is the Rust version of Universal Music Player (formerly Real-Hi-Res),
released next to the Python version.  It plays your music the same way and
finds the same fakes, with the same verdicts and reasons,
and it shares its settings and saved results with the Python version: a file
checked by one of them is not analysed again by the other.  FFmpeg is
bundled, so nothing else needs to be installed.

Start the app
-------------
  Windows : double-click universal-music-player.exe
  macOS   : drag UniversalMusicPlayer-Rust.app into Applications, then open it
            (see "First launch on macOS" below)
  Linux   : run ./universal-music-player
            Optional: ./install.sh adds "Universal Music Player (Rust)" to your
            application menu and links the command-line tool into
            ~/.local/bin as realhires-rust (no root needed).
            ./uninstall.sh removes that again.  The app runs from this
            folder, so re-run install.sh if you move the folder.

The music player (2.0)
----------------------
The app opens on your Library.  The first time, choose your music folder
(the Library page has a button, or Settings > Library).  The Library page
and the sidebar lead to All Songs, Folders, Albums, Artists, Album Artists,
Genres, Years, Composers, Playlists, Liked Songs, Disliked Songs, Recently
Played, Recently Added and Your Stats.  Double-click a song to play it.

The playback bar has Shuffle (off, this list, the whole library, same genre,
same artist, same album), Previous, Play/Pause, Next and Repeat (off, song,
album, stop after this song); in the middle the cover, title, seek bar and a
quality badge (click it for the Playback info: track, decoder, output); on
the right Like, Dislike (disliked songs are left out of shuffles), "..." (add
to a playlist and more), Lyrics (Now Playing: cover left, synced lyrics
right), Up Next, the equalizer and the volume.  Space plays and pauses.

The equalizer has a 10-band graphic and a parametric part, built-in presets
and your own (Save as preset..., Rename..., Delete...); Import... and
Export... read and write the app's own .json files and Equalizer APO / AutoEQ
.txt files.  Your Stats sums up your listening like a yearly music review;
Details shows the full numbers.  Settings > Appearance has the font, the
theme (8 built-in themes, or your own: New theme... and the colour editor),
Import theme..., Export... and "Open themes folder": theme .json files put
into that folder appear in the theme menu as they are, and a theme file
dropped onto the window is imported.  "Properties..." in a
song's menu and "Album Properties..." on an album page change the title,
artist, album, album artist, genre, release date, track, disc and composer
(only the tags change, never the audio).

Custom tabs (2.3): "New Tab..." in the sidebar makes a tab, a set of rules
(field, operator, value, e.g. Quality is Hi-Res Lossless) that picks songs
from your library.  Each tab is a small .json file in the tabs folder
(Settings > Library > "Open tabs folder"); "Export Tab..." writes one to
share, and Settings > Library > "Import tab..." adds one (or drop it onto
the window).  A tab
file holds only fields, operators and values, never code.  The theme editor
also lists "More colours" (every colour the app draws, including the
quality colours: Hi-Res Lossless in gold), and every song, album, artist,
genre, year, composer, playlist and tab has "Colour Label" in its menu.
Up Next songs can be dragged to a new place or removed with a right-click.

2.4: Moods (on the Library page) sorts your songs by their estimated mood
(Happy, Sad, Chill, Intense) and tempo (Up-tempo, Mid-tempo, Slow), worked
out on your computer from the sound; each card has "Create Playlist".  Your
Stats' names are links, and its Leaderboard and "Most Played" rank your
most played songs, artists and albums.  Settings > Playback > "Remember
where songs were played" (off by default) stores the approximate city of
each play, looked up from your IP address by FreeIPAPI (free.freeipapi.com;
nothing else is sent), shown beside the calendar and under Places.

Every other function is in Settings too:
Settings > Library > "Scan library" checks your library for fake files in
the quality checker described below.

The quality checker
-------------------
Open it with Settings > Library > "Scan library" or "Open the quality
checker".  Click "Add Folder..." or "Add Files..." (or drop folders onto the window).
Every folder or file you add appears under "Sources to scan" with the number
of audio files found, and the files are listed at once as "Not scanned";
files checked before show their saved verdict straight away.  Then press
"Scan".  The status line counts the files and estimates the time left.
Afterwards the list shows the problem files: FAKE LOSSLESS ("fake FLAC" made
from an MP3/AAC), FAKE HI-RES (upsampled or 16-bit padded) and SUSPICIOUS,
each with a one-line reason; tick the other boxes to see everything.  A
summary window shows the share of each verdict (click a slice to list only
those files).

Click a row for the dashboard: verdict, reason, key facts, spectrogram and
average spectrum.  "Open Folder" (or double-click / Enter) shows the file in
your file manager, "Open File" (Ctrl/Cmd+Enter) plays it.  "View: By album /
By artist" shows your library as cover-art cards with a ring showing how
many songs are fake; click a card to list its songs.  Ctrl/Cmd+F finds text
(Enter = next, Esc = close).  "Fix to real resolution..." (Ctrl/Cmd+Shift+F)
corrects the selected fake files in place so their properties show the real
resolution, keeping name, tags and cover art; originals go to a
"Real-Hi-Res backups" folder.  Ctrl/Cmd+A selects every listed file.
The "After fix" column says what Fix would do with each fake file, and the
details say whether it stays marked fake afterwards; a pop-up sums up each
fix.  "Get lyrics..." (Ctrl/Cmd+L) downloads synced lyrics for the selected
songs from LRCLIB (lrclib.net), as LRCGET does, and saves them as .lrc files
next to the songs; the dashboard's Lyrics tab shows them.
"Settings..." holds the detection thresholds, scanning, saved results,
display, fixing and lyrics options.
Language (2.1): Settings > Appearance > Language switches the app between
English and Japanese (日本語) at once; the command-line tool always writes
English.  Song information (2.1): "Acquire Metadata..." (in the right-click
menu of songs and albums, and on an album page) looks songs up
on MusicBrainz (musicbrainz.org) and show what would change before saving;
Settings > Library > "Add song information from MusicBrainz automatically"
(off by default) fills in missing information of new songs by itself.

Command line
------------
  realhires.exe (realhires on macOS/Linux) is the command-line tool, for a
  terminal.  Double-clicked in Explorer it opens the app instead.
  Windows : realhires.exe "D:\Music"
  macOS   : /Applications/UniversalMusicPlayer-Rust.app/Contents/MacOS/realhires ~/Music
  Linux   : ./realhires ~/Music     (or realhires-rust after install.sh)

  The options are those of the Python version's realhires-cli: run with
  --help (--only problems, --csv, --json, --spectrogram DIR, --fix,
  --fix-dir DIR, --lyrics, --lyrics-only, --set NAME=VALUE, -v ...).

First launch on macOS
---------------------
The app is not signed with an Apple Developer ID, so macOS blocks the first
launch ("damaged" / "cannot be opened").
  * macOS 15 Sequoia and later: System Settings > Privacy & Security, scroll
    down and click "Open Anyway" next to the message about the app.
  * Older macOS: right-click UniversalMusicPlayer-Rust.app > Open.
  * Any version, in Terminal:
      xattr -dr com.apple.quarantine /Applications/UniversalMusicPlayer-Rust.app

First launch on Windows
-----------------------
SmartScreen may warn about an unrecognised app.  Click "More info" > "Run
anyway".  Keep the "ffmpeg" folder next to the two programs.

Linux requirements
------------------
glibc 2.28 or newer (any distribution from 2018 onwards) and, for the app, a
graphical desktop with OpenGL (X11 or Wayland).  The command-line tool also
works on servers without a desktop.

Credits
-------
Synced lyrics come from LRCLIB (https://lrclib.net), a free and open lyrics
database, and from the people who contribute lyrics to it.  The lyrics
feature is modelled on LRCGET (https://github.com/tranxuanthang/lrcget), the
official LRCLIB client, and names and marks lyrics files the same way.
LRCGET ("Copyright (c) 2023 tranxuanthang") and the LRCLIB server
("Copyright (c) 2024 tranxuanthang", https://github.com/tranxuanthang/lrclib)
are released under the MIT License.  Real-Hi-Res's lyrics code was written
for Real-Hi-Res and contains no code from either project.  Thank you to
tranxuanthang and to everyone who contributes lyrics to LRCLIB.

Song information comes from MusicBrainz (https://musicbrainz.org), the open
music encyclopedia, and from the people who contribute to it.  Its core data
is public domain (CC0); tags and genres are under CC BY-NC-SA 3.0
(https://musicbrainz.org/doc/About/Data_License).  The app's MusicBrainz
code was written for it against MusicBrainz's published web service and
contains no code from MusicBrainz or MusicBrainz Picard.

On Windows and Linux the app uses the Inter typeface
(https://rsms.me/inter/, Copyright (c) 2016 The Inter Project Authors),
released under the SIL Open Font License 1.1; see LICENSE-Inter.txt.
The player plays sound through cpal (https://github.com/RustAudio/cpal,
Apache License 2.0).

Full documentation: https://github.com/SU2H1/Universal-Music-Player
FFmpeg licence information: see FFMPEG-NOTICE.txt (Windows/Linux: in the
"ffmpeg" folder; macOS: inside the app, right-click > Show Package Contents >
Contents > Resources).
