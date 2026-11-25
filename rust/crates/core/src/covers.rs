//! Album cover thumbnails for the album/artist views (FFmpeg only): the
//! picture embedded in a track, or a cover image in the album folder, scaled
//! and cropped to a square PNG.
//!
//! Covers are kept on disk (`cached_cover_png()`), one PNG per album and size
//! in `covers/` in the data folder (`cache_dir()`), shared with the Python
//! app, so a cover is made by FFmpeg only once and not again after a restart.
//! An album without a cover is remembered as an empty file.  The file name
//! (`cache_name()`, the same as Python's) changes when one of the album's
//! first four songs or one of their folders changes; `trim_cache()` keeps the
//! folder under `CACHE_LIMIT_BYTES` by deleting the least recently used files.
//!
//! For the Cover window (2.1): `cover_source()`, `image_info()`, `fit_png()` and
//! `save_folder_cover()`, as in Python.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime};

use sha1::{Digest, Sha1};

use crate::paths::{extension, parent, stem};
use crate::probe::{command, tool};

pub const FOLDER_IMAGES: [&str; 7] = ["cover", "folder", "front", "album", "albumart", "albumartsmall", "artwork"];
pub const IMAGE_EXTS: [&str; 6] = [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"];

/// The covers folder (tests point it at a temporary one).
pub const COVERS_ENV: &str = "REALHIRES_COVERS";
/// Part of every key: a new way of making covers gives new names.
pub const CACHE_VERSION: u32 = 1;
/// The songs of an album that a cover is made from (and keyed by).
pub const CACHE_PATHS: usize = 4;
/// `trim_cache()` keeps the folder under this.
pub const CACHE_LIMIT_BYTES: u64 = 500 * 1024 * 1024;
/// A temp file older than this was left by a crash.
pub const TEMP_MAX_AGE: Duration = Duration::from_secs(3600);
const BLOCK: u64 = 4096;
const PNG_START: &[u8] = b"\x89PNG\r\n\x1a\n";
const PNG_END: &[u8] = b"\x00\x00\x00\x00IEND\xaeB`\x82";

/// (PNG or None, whether FFmpeg ran to the end).  False means it could not be
/// started or timed out, so "no picture" is not certain.
fn thumbnail(ffmpeg: &Path, source: &str, size: u32, embedded: bool) -> (Option<Vec<u8>>, bool) {
    let vf = format!("scale={size}:{size}:force_original_aspect_ratio=increase,crop={size}:{size}");
    let mut cmd = command(ffmpeg);
    cmd.args(["-v", "error", "-nostdin", "-i", source]);
    if embedded {
        cmd.args(["-map", "0:v:0"]);
    }
    cmd.args(["-an", "-frames:v", "1", "-vf", &vf, "-f", "image2pipe", "-c:v", "png", "-"]);
    let Ok(mut child) = cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn() else {
        return (None, false);
    };
    let Some(mut out) = child.stdout.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return (None, false);
    };
    let reader = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = std::io::Read::read_to_end(&mut out, &mut buf);
        buf
    });
    let t0 = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break st,
            Ok(None) if t0.elapsed() > Duration::from_secs(30) => {
                let _ = child.kill();
                let _ = child.wait();
                return (None, false);
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(_) => return (None, false),
        }
    };
    let Ok(data) = reader.join() else { return (None, false) };
    ((status.success() && data.starts_with(b"\x89PNG")).then_some(data), true)
}

/// Best cover-like image file in `folder`, if any.
pub fn folder_image(folder: &str) -> Option<String> {
    let mut names: Vec<String> = std::fs::read_dir(folder)
        .ok()?
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| IMAGE_EXTS.contains(&extension(n).to_lowercase().as_str()))
        .collect();
    names.sort();
    for s in FOLDER_IMAGES {
        for n in &names {
            let base = stem(n).to_lowercase();
            if base == s || base.starts_with(&format!("{s}_")) || base.starts_with("albumart_") {
                return Some(std::path::Path::new(folder).join(n).to_string_lossy().into_owned());
            }
        }
    }
    (names.len() == 1).then(|| std::path::Path::new(folder).join(&names[0]).to_string_lossy().into_owned())
}

/// (cover, certain): certain is false when an FFmpeg run failed to start or
/// timed out on the way to "no cover".
fn make(ffmpeg: &Path, tracks: &[String], size: u32) -> (Option<Vec<u8>>, bool) {
    let mut certain = true;
    for t in tracks.iter().take(3) {
        let (data, ok) = thumbnail(ffmpeg, t, size, true);
        certain &= ok;
        if data.is_some() {
            return (data, true);
        }
    }
    let mut seen = std::collections::HashSet::new();
    for t in tracks {
        let folder = parent(t);
        if !seen.insert(folder.clone()) {
            continue;
        }
        if let Some(img) = folder_image(&folder) {
            let (data, ok) = thumbnail(ffmpeg, &img, size, false);
            certain &= ok;
            if data.is_some() {
                return (data, true);
            }
        }
    }
    (None, certain)
}

/// PNG bytes of a size×size cover for an album/artist, or None.
pub fn cover_png(tracks: &[String], size: u32) -> Option<Vec<u8>> {
    make(&tool("ffmpeg"), tracks, size).0
}

// ---------------------------------------------------------------- on disk ---

/// `$REALHIRES_COVERS`, else `covers` in the data folder (`store::data_dir()`).
pub fn cache_dir() -> PathBuf {
    std::env::var_os(COVERS_ENV)
        .filter(|x| !x.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::store::data_dir().join("covers"))
}

/// (size, mtime_ns) of a file or folder, (-1, -1) when it cannot be read.
fn stat_or_none(path: &str) -> (i64, i64) {
    crate::store::stat(path).unwrap_or((-1, -1))
}

/// The text whose SHA-1 names a cover on disk, byte for byte as Python's
/// `covers.cache_key()` builds it: lines joined with "\n" (no newline at the
/// end), encoded as UTF-8:
///
/// ```text
/// v1<TAB><size>                       CACHE_VERSION and the size in pixels
/// <path><TAB><bytes><TAB><mtime_ns>   for each of the first four paths, in order
/// <folder><TAB><mtime_ns>             for each distinct folder of those paths
///                                     (os.path.dirname), in first-seen order
/// ```
///
/// Numbers are decimal integers; a file or folder that cannot be read gives
/// -1 for its size and time.
pub fn cache_key(paths: &[String], size: u32) -> String {
    let mut lines = vec![format!("v{CACHE_VERSION}\t{size}")];
    let mut folders: Vec<String> = vec![];
    for p in paths.iter().take(CACHE_PATHS) {
        let (n, mtime) = stat_or_none(p);
        lines.push(format!("{p}\t{n}\t{mtime}"));
        let folder = parent(p);
        if !folders.contains(&folder) {
            folders.push(folder);
        }
    }
    for folder in &folders {
        lines.push(format!("{folder}\t{}", stat_or_none(folder).1));
    }
    lines.join("\n")
}

/// The file name of a cover in the covers folder: the SHA-1 (40 lower-case hex
/// digits) of `cache_key()`, then ".png".
pub fn cache_name(paths: &[String], size: u32) -> String {
    let digest = Sha1::digest(cache_key(paths, size).as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    format!("{hex}.png")
}

pub fn cache_file(paths: &[String], size: u32) -> PathBuf {
    cache_dir().join(cache_name(paths, size))
}

/// A whole PNG (signature and IEND chunk), or the empty "no cover" mark.
fn valid(data: &[u8]) -> bool {
    data.is_empty() || (data.starts_with(PNG_START) && data.ends_with(PNG_END))
}

fn touch(file: &Path) {
    let now = SystemTime::now();
    if let Ok(f) = std::fs::File::options().write(true).open(file) {
        let _ = f.set_times(std::fs::FileTimes::new().set_accessed(now).set_modified(now));
    }
}

/// One cache file: None when it is missing, unreadable or damaged, else the
/// cover (None inside for an album remembered without one).  Reading touches
/// its modification time, which is what `trim_cache()` goes by.
pub fn read_cached(file: &Path) -> Option<Option<Vec<u8>>> {
    let data = std::fs::read(file).ok()?;
    if !valid(&data) {
        return None;
    }
    touch(file);
    Some((!data.is_empty()).then_some(data))
}

/// Write through a temp file and a rename, so a reader never sees half a file;
/// failing to write only costs making the cover again next time.
fn write(file: &Path, data: &[u8]) {
    static COUNT: AtomicU64 = AtomicU64::new(0);
    let Some(folder) = file.parent() else { return };
    if std::fs::create_dir_all(folder).is_err() {
        return;
    }
    let nanos = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
    let tmp =
        folder.join(format!(".cover-{}-{}-{nanos}.tmp", std::process::id(), COUNT.fetch_add(1, Ordering::Relaxed)));
    let ok =
        std::fs::File::create(&tmp).and_then(|mut f| f.write_all(data)).is_ok() && std::fs::rename(&tmp, file).is_ok();
    if !ok {
        let _ = std::fs::remove_file(&tmp);
    }
}

/// Whether the covers folder has this cover (or its "no cover" mark).
pub fn is_cached(paths: &[String], size: u32) -> bool {
    is_cached_in(&cache_dir(), paths, size)
}

/// `is_cached()` in a given covers folder.
pub fn is_cached_in(dir: &Path, paths: &[String], size: u32) -> bool {
    dir.join(cache_name(paths, size)).is_file()
}

/// `cached_cover_png()` in a given covers folder with a given FFmpeg (the
/// player's worker keeps the folder it started with; tests: an FFmpeg that
/// cannot run).
pub fn cached_cover_png_in(dir: &Path, ffmpeg: &Path, paths: &[String], size: u32) -> Option<Vec<u8>> {
    let paths: Vec<String> = paths.iter().take(CACHE_PATHS).cloned().collect();
    let file = dir.join(cache_name(&paths, size));
    if let Some(found) = read_cached(&file) {
        return found;
    }
    let (data, certain) = make(ffmpeg, &paths, size);
    if data.is_some() || certain {
        write(&file, data.as_deref().unwrap_or_default());
    }
    data
}

/// `cover_png()` of the first four paths, from the covers folder when it has it
/// (None for an album remembered without a cover), else made with FFmpeg and
/// saved there.  A damaged or unreadable file is made again.  When FFmpeg could
/// not run, nothing is saved, so the album is tried again next time.
pub fn cached_cover_png(paths: &[String], size: u32) -> Option<Vec<u8>> {
    cached_cover_png_in(&cache_dir(), &tool("ffmpeg"), paths, size)
}

fn usage(n: u64) -> u64 {
    n.div_ceil(BLOCK).max(1) * BLOCK
}

/// Delete the least recently used covers (oldest modification time first)
/// until the folder holds at most `limit_bytes` (each file counted as whole
/// 4 KB blocks, an empty one as one block), and temp files older than an hour.
/// Returns the number of covers deleted.  `folder` None is `cache_dir()`.
pub fn trim_cache(limit_bytes: u64, folder: Option<&Path>) -> usize {
    let folder = folder.map(Path::to_path_buf).unwrap_or_else(cache_dir);
    let Ok(rd) = std::fs::read_dir(&folder) else { return 0 };
    let now = SystemTime::now();
    let mut entries: Vec<(i64, String, u64)> = vec![];
    for e in rd.filter_map(|e| e.ok()) {
        let name = e.file_name().to_string_lossy().into_owned();
        let path = folder.join(&name);
        let Ok(meta) = std::fs::metadata(&path) else { continue };
        if !meta.is_file() {
            continue;
        }
        if name.ends_with(".tmp") {
            let old =
                meta.modified().ok().and_then(|m| now.duration_since(m).ok()).is_some_and(|age| age > TEMP_MAX_AGE);
            if old {
                let _ = std::fs::remove_file(&path);
            }
            continue;
        }
        if name.ends_with(".png") {
            let mtime = match meta.modified().map(|m| m.duration_since(SystemTime::UNIX_EPOCH)) {
                Ok(Ok(d)) => d.as_nanos() as i64,
                Ok(Err(e)) => -(e.duration().as_nanos() as i64),
                Err(_) => i64::MIN,
            };
            entries.push((mtime, name, usage(meta.len())));
        }
    }
    entries.sort();
    let mut total: u64 = entries.iter().map(|e| e.2).sum();
    let mut deleted = 0;
    for (_, name, used) in entries {
        if total <= limit_bytes {
            break;
        }
        if std::fs::remove_file(folder.join(&name)).is_ok() {
            total -= used;
            deleted += 1;
        }
    }
    deleted
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("rhr-covers-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn set_mtime(path: &Path, secs: u64) {
        let f = std::fs::File::options().write(true).open(path).unwrap();
        f.set_modified(SystemTime::UNIX_EPOCH + Duration::from_secs(secs)).unwrap();
    }

    #[test]
    fn key_uses_the_first_four_songs_and_their_folders() {
        let paths: Vec<String> =
            ["/no/such/A/1.flac", "/no/such/A/2.flac", "/no/such/B/3.flac", "/no/such/A/4.flac", "/no/such/C/5.flac"]
                .iter()
                .map(|s| s.to_string())
                .collect();
        assert_eq!(
            cache_key(&paths, 176),
            "v1\t176\n/no/such/A/1.flac\t-1\t-1\n/no/such/A/2.flac\t-1\t-1\n/no/such/B/3.flac\t-1\t-1\n/no/such/A/4.flac\t-1\t-1\n/no/such/A\t-1\n/no/such/B\t-1"
        );
        assert_eq!(cache_name(&paths[..4], 176), cache_name(&paths, 176));
        assert_ne!(cache_name(&paths, 220), cache_name(&paths, 176));
        assert_eq!(cache_name(&paths, 176).len(), 44);
    }

    #[test]
    fn no_mark_when_ffmpeg_cannot_run() {
        let d = tmp("noffmpeg");
        let song = d.join("01.wav");
        std::fs::write(&song, b"RIFF").unwrap();
        std::fs::write(d.join("folder.png"), b"x").unwrap();
        let paths = vec![song.to_string_lossy().into_owned()];
        // the folder is given directly: the environment is shared with other tests
        let (data, certain) = make(Path::new("/no/such/ffmpeg"), &paths, 64);
        assert!(data.is_none() && !certain);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn trim_deletes_the_least_recently_used() {
        let d = tmp("trim");
        for (i, (name, size)) in
            [("a.png", 5000), ("b.png", 100), ("c.png", 0), ("d.png", 9000), ("e.png", 4096)].iter().enumerate()
        {
            let p = d.join(name);
            std::fs::write(&p, vec![b'x'; *size]).unwrap();
            set_mtime(&p, 1_000_000 + i as u64);
        }
        for n in [".cover-1.tmp", ".cover-2.tmp", "notes.txt"] {
            std::fs::write(d.join(n), b"x").unwrap();
        }
        set_mtime(&d.join(".cover-1.tmp"), 1_000_000);
        // usage in 4 KB blocks: a 8 KB, b 4 KB, c 4 KB, d 12 KB, e 4 KB = 32 KB
        assert_eq!(trim_cache(32 * 1024, Some(&d)), 0);
        assert_eq!(trim_cache(20 * 1024, Some(&d)), 2);
        let mut left: Vec<String> =
            std::fs::read_dir(&d).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).collect();
        left.sort();
        assert_eq!(left, [".cover-2.tmp", "c.png", "d.png", "e.png", "notes.txt"]);
        assert_eq!(trim_cache(0, Some(&d)), 3);
        assert_eq!(trim_cache(0, Some(&d.join("missing"))), 0);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn valid_files_and_marks() {
        assert!(valid(b""));
        assert!(valid(&[PNG_START, b"....", PNG_END].concat()));
        assert!(!valid(&PNG_START[..6]));
        assert!(!valid(b"not a png"));
    }
}

// ---------------------------------------------------- inspecting a cover (2.1) ---
// The Cover window (docs/player-spec.md, "Inspecting and changing a cover"):
// where the cover comes from and the picture as it is stored, read as Python
// `covers.py` reads it.

/// What a FLAC PICTURE block records about an image (metaflac's rules).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PictureInfo {
    pub mime: &'static str,
    pub width: u32,
    pub height: u32,
    /// bits per pixel
    pub depth: u32,
    /// the palette's size for an indexed PNG, else 0
    pub colors: u32,
}

fn be32(b: &[u8], at: usize) -> u32 {
    u32::from_be_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}

fn png_info(data: &[u8]) -> Option<PictureInfo> {
    if !data.starts_with(b"\x89PNG\r\n\x1a\n") {
        return None;
    }
    let mut pos = 8usize;
    let mut palette: Option<(u32, u32)> = None;
    while data.len() - pos > 12 {
        let clen = be32(data, pos) as usize;
        let kind = &data[pos + 4..pos + 8];
        if kind == b"IHDR" && clen == 13 && data.len() >= pos + 21 {
            let (w, h, bits, color) = (be32(data, pos + 8), be32(data, pos + 12), data[pos + 16] as u32, data[pos + 17]);
            if color == 3 {
                palette = Some((w, h));
            } else {
                let channels = match color {
                    0 => 1,
                    2 => 3,
                    4 => 2,
                    6 => 4,
                    _ => 0,
                };
                return Some(PictureInfo { mime: "image/png", width: w, height: h, depth: bits * channels, colors: 0 });
            }
        } else if let (Some((w, h)), b"PLTE") = (palette, kind) {
            return Some(PictureInfo { mime: "image/png", width: w, height: h, depth: 24, colors: (clen / 3) as u32 });
        }
        if clen + 12 > data.len() - pos {
            return None;
        }
        pos += 12 + clen;
    }
    None
}

const SOF: [u8; 13] = [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF];

fn jpeg_info(data: &[u8]) -> Option<PictureInfo> {
    if !data.starts_with(b"\xff\xd8") {
        return None;
    }
    let n = data.len();
    let mut pos = 2usize;
    loop {
        while pos < n && data[pos] != 0xFF {
            pos += 1;
        }
        while pos < n && data[pos] == 0xFF {
            pos += 1;
        }
        if pos >= n || data[pos] == 0xDA || data[pos] == 0xD9 {
            return None;
        }
        let marker = data[pos];
        pos += 1;
        if n - pos < 2 {
            return None;
        }
        let seg = ((data[pos] as usize) << 8) | data[pos + 1] as usize;
        if SOF.contains(&marker) {
            if seg < 8 || n - pos < seg {
                return None;
            }
            let h = ((data[pos + 3] as u32) << 8) | data[pos + 4] as u32;
            let w = ((data[pos + 5] as u32) << 8) | data[pos + 6] as u32;
            let depth = data[pos + 2] as u32 * data[pos + 7] as u32;
            return Some(PictureInfo { mime: "image/jpeg", width: w, height: h, depth, colors: 0 });
        }
        if seg < 2 || n - pos < seg {
            return None;
        }
        pos += seg;
    }
}

/// The type, size and depth of a JPEG or PNG image, read from its header
/// (None for anything else, or a header that cannot be read).
pub fn image_info(data: &[u8]) -> Option<PictureInfo> {
    png_info(data).or_else(|| jpeg_info(data))
}

/// "JPEG", "PNG", "GIF", "BMP", "WebP", or "" for something else.
pub fn image_format(data: &[u8]) -> &'static str {
    if data.starts_with(b"\xff\xd8") {
        "JPEG"
    } else if data.starts_with(b"\x89PNG") {
        "PNG"
    } else if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        "GIF"
    } else if data.starts_with(b"BM") {
        "BMP"
    } else if data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        "WebP"
    } else {
        ""
    }
}

/// Where the cover shown for some songs comes from, and the picture as stored.
#[derive(Clone, Debug, PartialEq)]
pub struct CoverSource {
    /// "embedded" (inside a song) or "folder" (an image file next to it)
    pub kind: &'static str,
    /// the song, or the image file
    pub path: String,
    pub data: Vec<u8>,
    /// `image_format()`
    pub format: &'static str,
    pub width: u32,
    pub height: u32,
}

impl CoverSource {
    pub fn mime(&self) -> &'static str {
        match self.format {
            "JPEG" => "image/jpeg",
            "PNG" => "image/png",
            "GIF" => "image/gif",
            "BMP" => "image/bmp",
            "WebP" => "image/webp",
            _ => "application/octet-stream",
        }
    }

    /// The file extension to save the picture with (".jpg", ".png" ...).
    pub fn ext(&self) -> &'static str {
        match self.format {
            "JPEG" => ".jpg",
            "PNG" => ".png",
            "GIF" => ".gif",
            "BMP" => ".bmp",
            "WebP" => ".webp",
            _ => ".img",
        }
    }

    /// "Inside 01 Song.flac" or "cover.jpg in the album's folder".
    pub fn where_text(&self) -> String {
        where_text(self.kind, &self.path)
    }

    /// "1,200 × 1,200 pixels · JPEG · 245.3 KB".
    pub fn details(&self) -> String {
        details_text(self.width, self.height, self.format, self.data.len())
    }
}

/// "Inside 01 Song.flac" or "cover.jpg in the album's folder" (Python `where_text`).
pub fn where_text(kind: &str, path: &str) -> String {
    let name = crate::paths::file_name(path);
    if kind == "embedded" { format!("Inside {name}") } else { format!("{name} in the album's folder") }
}

/// "1,200 × 1,200 pixels · JPEG · 245.3 KB" (Python `details_text`).
pub fn details_text(width: u32, height: u32, format: &str, size: usize) -> String {
    use crate::stats::thousands;
    let mut parts = vec![];
    if width > 0 && height > 0 {
        parts.push(format!("{} × {} pixels", thousands(width as i64), thousands(height as i64)));
    }
    parts.push(if format.is_empty() { "Unknown format".to_string() } else { format.to_string() });
    parts.push(crate::fix::fmt_bytes(size as f64));
    parts.join(" · ")
}

/// Run FFmpeg (or ffprobe) with a 30 s limit; its stdout when it succeeded.
fn run_output(mut cmd: std::process::Command, input: Option<&[u8]>) -> Option<Vec<u8>> {
    let mut child = cmd
        .stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    if let (Some(data), Some(mut stdin)) = (input.map(|d| d.to_vec()), child.stdin.take()) {
        std::thread::spawn(move || {
            let _ = std::io::Write::write_all(&mut stdin, &data);
        });
    }
    let mut out = child.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = std::io::Read::read_to_end(&mut out, &mut buf);
        buf
    });
    let t0 = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break st,
            Ok(None) if t0.elapsed() > Duration::from_secs(30) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(10)),
            Err(_) => return None,
        }
    };
    let data = reader.join().ok()?;
    status.success().then_some(data)
}

/// The first picture inside a song as it is stored (FFmpeg's first video
/// stream, which `cover_png()` shows), or empty.
fn embedded_picture(path: &str) -> Vec<u8> {
    let mut cmd = command(tool("ffmpeg"));
    cmd.args(["-v", "error", "-nostdin", "-i", path, "-map", "0:v:0", "-c", "copy", "-frames:v", "1", "-f", "image2pipe", "-"]);
    run_output(cmd, None).unwrap_or_default()
}

/// Width and height of the first picture of a file, by ffprobe (images that
/// are not JPEG or PNG).
fn probe_size(path: &str) -> (u32, u32) {
    let mut cmd = command(tool("ffprobe"));
    cmd.args(["-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", path]);
    let Some(out) = run_output(cmd, None) else { return (0, 0) };
    let text = String::from_utf8_lossy(&out);
    let mut it = text.trim().split(',');
    match (it.next().and_then(|w| w.trim().parse().ok()), it.next().and_then(|h| h.trim().parse().ok())) {
        (Some(w), Some(h)) => (w, h),
        _ => (0, 0),
    }
}

fn source(kind: &'static str, path: &str, data: Vec<u8>) -> CoverSource {
    let (width, height) = match image_info(&data) {
        Some(i) => (i.width, i.height),
        None => probe_size(path),
    };
    CoverSource { kind, path: path.to_string(), format: image_format(&data), data, width, height }
}

/// Where the cover that `cover_png()` shows for these songs comes from: the
/// picture inside the first of the first three songs that has one, else the
/// folder image, with the picture's bytes, size and format (Python
/// `cover_source`).  None when there is no cover.
pub fn cover_source(tracks: &[String]) -> Option<CoverSource> {
    for t in tracks.iter().take(3) {
        let data = embedded_picture(t);
        if !data.is_empty() {
            return Some(source("embedded", t, data));
        }
    }
    let mut seen = std::collections::HashSet::new();
    for t in tracks {
        let folder = parent(t);
        if !seen.insert(folder.clone()) {
            continue;
        }
        if let Some(img) = folder_image(&folder) {
            if let Ok(data) = std::fs::read(&img) {
                if !data.is_empty() {
                    return Some(source("folder", &img, data));
                }
            }
        }
    }
    None
}

/// The picture as a PNG that fits into size × size (aspect kept; a smaller one
/// is enlarged only with `enlarge`), made by FFmpeg, for showing it (Python
/// `fit_png`).
pub fn fit_png(data: &[u8], size: u32, enlarge: bool) -> Option<Vec<u8>> {
    let vf = if enlarge {
        format!("scale=w={size}:h={size}:force_original_aspect_ratio=decrease")
    } else {
        format!("scale=w='min({size},iw)':h='min({size},ih)':force_original_aspect_ratio=decrease")
    };
    let mut cmd = command(tool("ffmpeg"));
    cmd.args(["-v", "error", "-nostdin", "-i", "pipe:0", "-frames:v", "1", "-vf", &vf, "-f", "image2pipe", "-c:v", "png", "-"]);
    run_output(cmd, Some(data)).filter(|d| d.starts_with(b"\x89PNG"))
}

/// Where "Also save it as cover.jpg in the album's folder" writes: cover.jpg,
/// or cover.png for a PNG, keeping the spelling of one already there.
pub fn folder_cover_name(folder: &str, data: &[u8]) -> String {
    let name = if image_format(data) == "PNG" { "cover.png" } else { "cover.jpg" };
    let mut names: Vec<String> = std::fs::read_dir(folder)
        .map(|rd| rd.filter_map(|e| e.ok()).map(|e| e.file_name().to_string_lossy().into_owned()).collect())
        .unwrap_or_default();
    names.sort();
    let chosen = names.into_iter().find(|n| n.to_lowercase() == name).unwrap_or_else(|| name.to_string());
    std::path::Path::new(folder).join(chosen).to_string_lossy().into_owned()
}

/// Write the picture as cover.jpg (cover.png) into the folder, through a
/// temporary file; returns its path (Python `save_folder_cover`).
pub fn save_folder_cover(folder: &str, data: &[u8]) -> std::io::Result<String> {
    let path = folder_cover_name(folder, data);
    crate::embed::replace_file(&path, data)?;
    Ok(path)
}

// ------------------------------------------------- Search Cover Online (2.3) ---

/// Google Images (with `tbm=isch`).
pub const IMAGE_SEARCH_URL: &str = "https://www.google.com/search";

/// The words Search Cover Online looks for: "<album artist (else artist)>
/// <album>", or, for a song without an album, "<artist (else album artist)>
/// <title>"; unknown parts are left out (Python `cover_search_query`).
pub fn cover_search_query(album_artist: &str, artist: &str, album: &str, title: &str) -> String {
    let part = crate::tagedit::clean_value;
    let album = part(album);
    let parts = if !album.is_empty() {
        let who = part(album_artist);
        [if who.is_empty() { part(artist) } else { who }, album]
    } else {
        let who = part(artist);
        [if who.is_empty() { part(album_artist) } else { who }, part(title)]
    };
    parts.into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join(" ")
}

/// Google Images searching for the cover (`cover_search_query()`), as the
/// browser opens it (Python `cover_search_url`).
pub fn cover_search_url(album_artist: &str, artist: &str, album: &str, title: &str) -> String {
    let q = cover_search_query(album_artist, artist, album, title);
    format!("{IMAGE_SEARCH_URL}?{}", crate::lyrics::urlencode(&[("tbm".into(), "isch".into()), ("q".into(), q)]))
}
