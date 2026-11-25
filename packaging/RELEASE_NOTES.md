## What's new in 2.3.0

**Up Next can be edited.** Drag a song in Up Next (the panel or Now Playing's Up Next view) to another place, with a line showing where it will go, or right-click it and choose **Remove from Up Next**. A removed song stays out until you play another list, also when you change the shuffle mode.

**Custom tabs.** A tab is a saved set of rules that picks songs from your library, such as "Hi-Res", "Lossy", "Added in the last 30 days" or anything else, shown in the sidebar under **Tabs** and on the Library page. **New Tab…** opens the tab window, where each rule is a field (title, artist, genre, quality, year, bit depth, sample rate, bit rate, length, days since added, liked and more), an operator and a value, and a line says how many songs match while you change them. Each tab is one small `.json` file in the tabs folder, so tabs can be shared: **Export Tab…** writes the file, and **Settings → Library → Import tab…** (or dropping the file onto the Rust version's window, or copying it into the tabs folder) adds it. A tab file holds only fields, operators and values, so no code runs when you open one.

**Colours.** The theme editor lists **More colours**: every other colour the app draws, from the row under the pointer, the selected item and text fields to the quality colours and the Quality Check verdicts. Unless your theme sets them, they follow its eight colours. The quality badge and the Quality column now show Hi-Res Lossless in gold, Lossless in the accent colour and lossy in grey. **Colour Label ▸** in the menu of every song, album, artist, album artist, genre, year, composer, playlist and tab marks it with a small dot in a colour of your choice.

**Now Playing.** A back link at the top left returns to where you came from (the search results, All Songs, an album and so on). **Lyrics only** (next to A− / A+) hides the cover so that the lyrics fill the page, and an instrumental song's cover is centred by itself. Drag or swipe the cover sideways to see the album's songs, with the one playing in bold. **Details** is centred like the cover.

**Covers and song information.** **Change Cover…** is in the right-click menus of songs and albums, and the Cover window has **Search Cover Online**, which opens your browser on an image search for the artist and album. The **Properties** window has **Acquire Metadata…**, and each field has a **Search** button that opens your browser on a web search for that field (for example the composer of the song); text you copy there within two minutes is filled in for you.

**Library.** **Settings → Appearance → Library page** (or the two small buttons next to the Library title) shows the Library page as tiles or as a list. On the Genres page, **Select** lets you choose several genres and **Merge…** them under one name. In Your Stats, the songs of the chosen day are listed beside the calendar instead of under it, when the window is wide enough.

**Equalizer.** The equalizer now works together with bit-perfect playback: it is applied in 64-bit precision, with dither for devices that take integers, while the device still runs at the song's own rate for this app alone. The Playback info says so, and turning the equalizer off gives bit-perfect sound again. The Equalizer window's close button is at its right edge, and its rows wrap instead of being cut off.

Everything new is in Japanese too when Settings → Appearance → Language is set to 日本語.

## Download

Pick the package for your computer, unpack it and run it. Python, NumPy and FFmpeg are bundled, so nothing else has to be installed.

| System | Download | Start the app |
| --- | --- | --- |
| Windows 10/11, 64-bit | `UniversalMusicPlayer-<version>-windows-x64.zip` | `UniversalMusicPlayer\UniversalMusicPlayer.exe` |
| macOS 12+ on Apple Silicon (M1 and later) | `UniversalMusicPlayer-<version>-macos-arm64.dmg` | drag `UniversalMusicPlayer` to Applications |
| macOS 10.15+ on Intel | `UniversalMusicPlayer-<version>-macos-x86_64.dmg` | drag `UniversalMusicPlayer` to Applications |
| Linux x86-64 | `UniversalMusicPlayer-<version>-linux-x64.tar.gz` | `UniversalMusicPlayer/UniversalMusicPlayer` (optional: `UniversalMusicPlayer/install.sh` adds a menu entry) |
| Linux ARM64 (e.g. Raspberry Pi 4/5 with 64-bit OS) | `UniversalMusicPlayer-<version>-linux-arm64.tar.gz` | `UniversalMusicPlayer/UniversalMusicPlayer` (optional: `UniversalMusicPlayer/install.sh`) |

**Rust version.** The same music player and command-line tool, written in Rust, with FFmpeg bundled; it shares your library, likes, playlists, play history, themes and saved results with the version above:

| System | Download | Start the app |
| --- | --- | --- |
| Windows 10/11, 64-bit | `UniversalMusicPlayer-Rust-<version>-windows-x64.zip` | `UniversalMusicPlayer-Rust\universal-music-player.exe` |
| macOS 12+ on Apple Silicon | `UniversalMusicPlayer-Rust-<version>-macos-arm64.dmg` | drag `UniversalMusicPlayer-Rust` to Applications |
| macOS 10.15+ on Intel | `UniversalMusicPlayer-Rust-<version>-macos-x86_64.dmg` | drag `UniversalMusicPlayer-Rust` to Applications |
| Linux x86-64 | `UniversalMusicPlayer-Rust-<version>-linux-x64.tar.gz` | `UniversalMusicPlayer-Rust/universal-music-player` (optional: `UniversalMusicPlayer-Rust/install.sh`) |
| Linux ARM64 | `UniversalMusicPlayer-Rust-<version>-linux-arm64.tar.gz` | `UniversalMusicPlayer-Rust/universal-music-player` (optional: `UniversalMusicPlayer-Rust/install.sh`) |

Its command-line tool is `realhires` (`realhires.exe`) next to the app, with the same options as `realhires-cli`.

The `.zip` files for macOS contain the same app as the `.dmg` files. The Linux packages need glibc 2.28 or newer (any distribution from 2018 onwards), and the Rust version's app also needs OpenGL, which every desktop has. Every package also contains the command-line tool. On Windows, keep the `.pkg` files and the `_internal` folder next to the `.exe` files of the Python version, and the `ffmpeg` folder next to those of the Rust version.

**First launch.** The apps are not code-signed. On Windows, SmartScreen may ask you to confirm with **More info → Run anyway**. On macOS 15 and later, open **System Settings → Privacy & Security** and click **Open Anyway**. On older macOS, right-click the app and choose **Open**, or run `xattr -dr com.apple.quarantine /Applications/UniversalMusicPlayer.app` (or `/Applications/UniversalMusicPlayer-Rust.app`) once in Terminal.

## Credits

Synced lyrics come from [LRCLIB](https://lrclib.net) and the people who contribute to it, and the lyrics feature is modelled on LRCGET by tranxuanthang ([github.com/tranxuanthang/lrcget](https://github.com/tranxuanthang/lrcget)), who also makes LRCLIB. Both are MIT-licensed, and Real-Hi-Res's own lyrics code contains no code from either. Both apps credit them in the lyrics window and in the About box (Settings → About Universal Music Player). Thank you to them.

Song information comes from [MusicBrainz](https://musicbrainz.org) and the people who contribute to it (core data CC0, genres CC BY-NC-SA 3.0); the app's MusicBrainz code is its own, written against MusicBrainz's published web service, and contains no code from MusicBrainz or Picard.

On Windows and Linux the apps use the [Inter](https://rsms.me/inter/) typeface by The Inter Project Authors (SIL Open Font License 1.1; its licence is in every package). The Python app plays sound through [miniaudio](https://github.com/irmen/pyminiaudio) (Irmen de Jong and David Reid, MIT License), the Rust app through [cpal](https://github.com/RustAudio/cpal) (Apache License 2.0).

The bundled FFmpeg executables are licensed separately (LGPL on Windows and Linux, GPL on macOS). See `FFMPEG-NOTICE.txt` inside the package.
