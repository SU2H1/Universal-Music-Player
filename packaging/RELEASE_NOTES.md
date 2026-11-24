## What's new in 2.1.0

**English or Japanese (日本語).** Settings → Appearance → **Language** switches the whole app between English and Japanese: the player's menus, pages and windows, every setting and its explanation, and the quality checker with its verdicts, explanations, Fix and lyrics windows. The Rust version changes language at once; the Python version asks to restart (**Restart now**). Your songs' own information is shown as it is, and the command-line tools keep writing English. The Rust version now also shows Japanese titles with a system font that has Japanese characters.

**Song information from MusicBrainz.** **Get info from MusicBrainz…** (a song's menu) and **Get album info from MusicBrainz…** (an album page) look the song or the album up on [MusicBrainz](https://musicbrainz.org), list the releases found, match your songs to the chosen release's tracks and show what would change, field by field, before anything is written: title, artist, album, album artist, release date (when the album first came out), track and disc numbers, genre and composer. Choose the fields to write, or fill in only empty ones. **Settings → Library → Add song information from MusicBrainz automatically** (off by default) fills in the missing information of new songs by itself, only from certain matches and never replacing what a tag already holds; **Look up the library on MusicBrainz now** runs it at once. Only the tags change, never the audio. The app keeps to MusicBrainz's one request per second and names itself in every request.

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

Synced lyrics come from [LRCLIB](https://lrclib.net) and the people who contribute to it, and the lyrics feature is modelled on LRCGET by tranxuanthang ([github.com/tranxuanthang/lrcget](https://github.com/tranxuanthang/lrcget)), who also makes LRCLIB. Both are MIT-licensed, and Real-Hi-Res's own lyrics code contains no code from either. Both apps credit them in the lyrics window and in Help → About. Thank you to them.

Song information comes from [MusicBrainz](https://musicbrainz.org) and the people who contribute to it (core data CC0, genres CC BY-NC-SA 3.0); the app's MusicBrainz code is its own, written against MusicBrainz's published web service, and contains no code from MusicBrainz or Picard.

On Windows and Linux the apps use the [Inter](https://rsms.me/inter/) typeface by The Inter Project Authors (SIL Open Font License 1.1; its licence is in every package). The Python app plays sound through [miniaudio](https://github.com/irmen/pyminiaudio) (Irmen de Jong and David Reid, MIT License), the Rust app through [cpal](https://github.com/RustAudio/cpal) (Apache License 2.0).

The bundled FFmpeg executables are licensed separately (LGPL on Windows and Linux, GPL on macOS). See `FFMPEG-NOTICE.txt` inside the package.
