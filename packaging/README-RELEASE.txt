Universal Music Player - music player and fake hi-res audio detector
====================================================================

Universal Music Player (formerly Real-Hi-Res) is a music player with the
Real-Hi-Res quality checker built in.  This package is self-contained: Python, NumPy and FFmpeg are bundled, so
nothing else needs to be installed.

Start the app
-------------
  Windows : double-click UniversalMusicPlayer.exe
  macOS   : drag UniversalMusicPlayer.app into Applications, then open it
            (see "First launch on macOS" below)
  Linux   : run ./UniversalMusicPlayer
            Optional: ./install.sh adds Universal Music Player to your application menu
            and links realhires-cli into ~/.local/bin (no root needed).
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
into that folder appear in the theme menu as they are.  "Properties..." in a
song's menu and "Album Properties..." on an album page change the title,
artist, album, album artist, genre, release date, track, disc and composer
(only the tags change, never the audio).

Custom tabs (2.3): "New Tab..." in the sidebar makes a tab, a set of rules
(field, operator, value, e.g. Quality is Hi-Res Lossless) that picks songs
from your library.  Each tab is a small .json file in the tabs folder
(Settings > Library > "Open tabs folder"); "Export Tab..." writes one to
share, and Settings > Library > "Import tab..." adds one.  A tab
file holds only fields, operators and values, never code.  The theme editor
also lists "More colours" (every colour the app draws, including the
quality colours: Hi-Res Lossless in gold), and every song, album, artist,
genre, year, composer, playlist and tab has "Colour Label" in its menu.
Up Next songs can be dragged to a new place or removed with a right-click.

Every other function is in Settings too:
Settings > Library > "Scan library" checks your library for fake files in
the quality checker described below.

The quality checker
-------------------
Open it with Settings > Library > "Scan library" or "Open the quality
checker".  Click "Add Folder..." (or "Add Files...").  Every folder/file you add appears
in the "Sources to scan" panel with the number of audio files found (red if
none), and the files are listed right away as "Not scanned".  Then press
"Scan".  While it runs, the status line counts the files and, after the
first few, estimates the time left ("about 6 min left").  After the scan the list shows the problem files: FAKE LOSSLESS ("fake FLAC" made from an MP3/AAC), FAKE HI-RES
(upsampled or 16-bit padded) and SUSPICIOUS, each with a one-line reason; tick
the other boxes to see everything.  After the scan a summary window shows the
percentage of files per verdict as a pie chart (click a slice to list
only those files).

Click a row for the dashboard: verdict, reason, key facts, spectrogram and
average spectrum.  "Open Folder" (or double-click / Enter) shows the file in
your file manager, "Open File" (Ctrl/Cmd+Enter) plays it.  "View: By album /
By artist" shows your library as cover-art cards with a ring showing how many
songs are fake; click a card to list its songs.  Ctrl/Cmd+F finds text like a
web browser (Enter = next).  "Fix to real
resolution..." (Ctrl/Cmd+Shift+F) corrects selected fake files in place so their
properties show the real resolution (e.g. a fake 24/96 becomes 16/44.1), keeping
name, tags and cover art; originals go to a "Real-Hi-Res backups" folder.
Ctrl/Cmd+A selects every listed file: tick only "Fake lossless" and "Fake
hi-res", press Ctrl/Cmd+A, then "Fix to real resolution..." to fix them all.
Settings > Fixing > "After a scan" can ask, or fix the fake files on its own,
when a scan finishes.  Fake FLACs made from an MP3 that are already 16/44.1
cannot be improved; the Fix window lists them last and says why.  When
fixing has finished, a pop-up says how many files were fixed, how much smaller
they are and where the originals went.  The "After fix" column (next to
Bandwidth) and the details of each fake file say what Fix does with it and
whether it stays marked fake afterwards (a file made from an MP3/AAC stays
"Fake lossless" at any resolution).
"Get lyrics..." (Ctrl/Cmd+L, or Edit > Download synced lyrics for all songs)
downloads synced lyrics from LRCLIB (lrclib.net) and saves them as <name>.lrc
next to each song, as LRCGET does (plain lyrics as <name>.txt); no scan is
needed.  Songs are looked up by their title/artist/album tags and length, one
at a time as LRCLIB asks, and songs that already have an .lrc are skipped.
The Lyrics tab of the dashboard shows them.
"Settings..." lets you change detection thresholds, scanning options, display
and fixing preferences; they are saved and also used by the command-line tool.

Language (2.1): Settings > Appearance > Language switches the app between
English and Japanese (日本語); the app asks to restart.  The command-line tool
always writes English.
Song information (2.1): "Acquire Metadata..." (in the right-click menu of
songs and albums, and on an album page) looks songs up on
MusicBrainz (musicbrainz.org) and show what would change before saving.
Settings > Library > "Add song information from MusicBrainz automatically"
(off by default) fills in missing information of new songs by itself, only
from certain matches and never replacing a filled tag.

Results are saved: a file that has not changed is never analysed again, not
on the next scan, after restarting, or after updating Real-Hi-Res.  A folder
you checked before shows its verdicts as soon as you add it, and "Scan" only
analyses new or changed files.  Right-click > "Re-scan" analyses a file again.
The results are kept in your user folder (Settings > Saved results shows where
and can delete them); the command-line tool uses them too (--rescan,
--no-cache, --forget-results).

Command line
------------
realhires-cli is the command-line tool, for a terminal.  On Windows, double-
clicking realhires-cli.exe opens the app instead.
  Windows : realhires-cli.exe "D:\Music"
  macOS   : /Applications/UniversalMusicPlayer.app/Contents/MacOS/realhires-cli ~/Music
  Linux   : ./realhires-cli ~/Music     (or just realhires-cli after install.sh)

  Run with --help for all options (--only problems, --csv, --json,
  --spectrogram DIR, --fix, --fix-dir DIR, --lyrics, --lyrics-only,
  --set NAME=VALUE, -v ...).

First launch on macOS
---------------------
The app is not signed with an Apple Developer ID, so macOS blocks the first
launch ("damaged" / "cannot be opened").
  * macOS 15 Sequoia and later: System Settings > Privacy & Security, scroll
    down and click "Open Anyway" next to the UniversalMusicPlayer message.
  * Older macOS: right-click UniversalMusicPlayer.app > Open.
  * Any version, in Terminal:
      xattr -dr com.apple.quarantine /Applications/UniversalMusicPlayer.app

First launch on Windows
-----------------------
SmartScreen may warn about an unrecognised app.  Click "More info" > "Run
anyway".  Keep the whole folder together: UniversalMusicPlayer.exe and realhires-cli.exe
are small launchers, and the program itself is in UniversalMusicPlayer.pkg,
realhires-cli.pkg and the _internal folder next to them.  If an antivirus
program reports a trojan, it is a false positive caused by the PyInstaller
packaging (see the README on GitHub); restore the file and report it to the
vendor.

Linux requirements
------------------
glibc 2.28 or newer (any distribution from 2018 onwards) and, for the app, a
graphical desktop (X11, or Wayland with XWayland).  The command-line tool also
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
The player plays sound through miniaudio (Python bindings Copyright (c)
2019 Irmen de Jong, miniaudio Copyright 2019 David Reid; MIT License).

Full documentation: https://github.com/SU2H1/Universal-Music-Player
FFmpeg licence information: see FFMPEG-NOTICE.txt in the "ffmpeg" folder
inside the package (Windows/Linux: _internal/ffmpeg; macOS: inside the app,
right-click UniversalMusicPlayer.app > Show Package Contents > Contents).
