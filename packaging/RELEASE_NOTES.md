## What's new in 2.4.0

**Folders scroll.** The Folders page is one list again: a folder's subfolders come first, then its songs, and the whole page scrolls with the mouse wheel, the trackpad or the scroll bar, however many subfolders and songs it holds.

**Your Stats.** Every song, artist, album and genre named on Your Stats is now a link: a song plays (with the rest of that list after it), an artist, album or genre opens its page, and a right-click on a song opens its menu. The new **Leaderboard** ranks your most played **Songs**, **Artists** or **Albums** of the chosen period, with plays and minutes and the top three on gold, silver and bronze; **See All** opens **Most Played**, the whole ranking of up to 100, with Play and Shuffle.

**Where a song was played.** Turn on **Settings → Playback → Remember where songs were played** (it is off until you do) and each play is stored with the approximate place you were in: the city, region and country. Your Stats then names the place beside each play of the day you pick in the calendar, and a **Places** part lists where you listened, with plays and minutes; click a place to play its songs. The place comes from FreeIPAPI's free service (free.freeipapi.com), which sees only your computer's IP address: one request when the app starts or the setting is turned on, then at most every two hours while music plays, and no song, path or tag is ever sent. The places stay in your library file on your computer, and **Forget play locations…** removes them.

**Moods and playlists.** The player listens to your songs on your computer, in the background, and estimates each one's tempo (BPM), energy and key. From these it labels songs **Up-tempo**, **Mid-tempo** or **Slow**, **Energetic** or **Calm**, and **Happy**, **Sad**, **Chill** or **Intense**. They are estimates from the sound, and the app says so. The new **Moods** entry on the Library page shows a card for each label with its songs, and **Create Playlist** turns any of them into a playlist ("Sad songs"). Song lists can show **BPM** and **Mood** columns, Now Playing's Details shows the mood, tempo and key, and custom tabs can use them as rules. Nothing is sent anywhere; **Settings → Library → Analyse moods and tempo** turns it off.

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
