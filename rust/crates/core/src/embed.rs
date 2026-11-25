//! Lyrics inside the audio file (2.0), the counterpart of `realhires/embed.py`,
//! writing byte-identical files: FLAC Vorbis comments `LYRICS` (synced) and
//! `UNSYNCEDLYRICS` (plain), MP3 ID3v2 frames `SYLT` and `USLT` (language
//! "XXX", empty description), as LRCGET embeds them.  Only the tag is
//! rewritten; the audio frames are copied byte for byte, through a temporary
//! file that replaces the original.

use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

use crate::paths::{extension, file_name};

pub const SUPPORTED: [&str; 2] = [".flac", ".mp3"];
pub const VENDOR: &str = "Real-Hi-Res";
const FLAC_SYNCED: &str = "LYRICS";
const FLAC_PLAIN: &str = "UNSYNCEDLYRICS";
const LANG: &[u8] = b"XXX";
pub const NEW_PADDING: usize = 4096;
/// More room than this (a removed cover picture) is not kept as FLAC padding.
pub const MAX_KEPT_PADDING: usize = 1 << 20;

#[derive(Debug)]
pub enum EmbedError {
    Format(String),
    Io(std::io::Error),
}

impl std::fmt::Display for EmbedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EmbedError::Format(m) => f.write_str(m),
            EmbedError::Io(e) => write!(f, "{e}"),
        }
    }
}

impl From<std::io::Error> for EmbedError {
    fn from(e: std::io::Error) -> Self {
        EmbedError::Io(e)
    }
}

fn err<T>(m: &str) -> Result<T, EmbedError> {
    Err(EmbedError::Format(m.to_string()))
}

pub fn supported(path: &str) -> bool {
    SUPPORTED.contains(&extension(&file_name(path)).to_lowercase().as_str())
}

/// (milliseconds, text) of every timed line, sorted by time (Python `lrc_entries`).
pub fn lrc_entries(synced: &str) -> Vec<(u32, String)> {
    let norm = synced.replace("\r\n", "\n").replace('\r', "\n");
    let mut out = vec![];
    for line in norm.split('\n') {
        let mut stamps = vec![];
        let mut rest = line;
        while let Some((ms, after)) = stamp(rest.trim_start_matches(crate::pyunicode::is_space)) {
            stamps.push(ms);
            rest = after;
        }
        if !stamps.is_empty() {
            let text = crate::pyunicode::strip(rest).to_string();
            for ms in stamps {
                out.push((ms, text.clone()));
            }
        }
    }
    out.sort_by_key(|e| e.0);
    out
}

/// Whether lyrics text is really LRC: at least half of its non-empty lines start with a
/// timestamp (Python `looks_synced`).  Some programs keep LRC in a .txt file or in the
/// plain-lyrics tag (USLT, UNSYNCEDLYRICS), and the player should still follow it.
pub fn looks_synced(text: &str) -> bool {
    let norm = text.replace("\r\n", "\n").replace('\r', "\n");
    let lines: Vec<&str> = norm.split('\n').filter(|l| !crate::pyunicode::strip(l).is_empty()).collect();
    let timed = lines.iter().filter(|l| stamp(l.trim_start_matches(crate::pyunicode::is_space)).is_some()).count();
    timed > 0 && timed * 2 >= lines.len()
}

/// `\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]` at the start of `s`.
fn stamp(s: &str) -> Option<(u32, &str)> {
    let b = s.as_bytes();
    if b.first() != Some(&b'[') {
        return None;
    }
    let digits = |from: usize, max: usize| {
        let mut i = from;
        while i < b.len() && i - from < max && b[i].is_ascii_digit() {
            i += 1;
        }
        i
    };
    let m_end = digits(1, usize::MAX);
    if m_end == 1 || b.get(m_end) != Some(&b':') {
        return None;
    }
    let s_end = digits(m_end + 1, 2);
    if s_end == m_end + 1 {
        return None;
    }
    let mut end = s_end;
    let mut frac = "0";
    if matches!(b.get(s_end), Some(b'.') | Some(b':')) {
        let f_end = digits(s_end + 1, 3);
        if f_end > s_end + 1 && b.get(f_end) == Some(&b']') {
            frac = &s[s_end + 1..f_end];
            end = f_end;
        }
    }
    if b.get(end) != Some(&b']') {
        return None;
    }
    let minutes: u64 = s[1..m_end].parse().ok()?;
    let secs: u64 = s[m_end + 1..s_end].parse().ok()?;
    let mut f = frac.to_string();
    while f.len() < 3 {
        f.push('0');
    }
    let ms = minutes * 60000 + secs * 1000 + f[..3].parse::<u64>().ok()?;
    Some((ms as u32, &s[end + 1..]))
}

// ------------------------------------------------------------------ FLAC ---

fn syncsafe(b: &[u8]) -> usize {
    ((b[0] as usize) << 21) | ((b[1] as usize) << 14) | ((b[2] as usize) << 7) | b[3] as usize
}

fn to_syncsafe(n: usize) -> [u8; 4] {
    [((n >> 21) & 0x7F) as u8, ((n >> 14) & 0x7F) as u8, ((n >> 7) & 0x7F) as u8, (n & 0x7F) as u8]
}

pub(crate) fn id3_len(data: &[u8]) -> usize {
    if data.len() >= 10 && &data[..3] == b"ID3" {
        10 + syncsafe(&data[6..10]) + if data[5] & 0x10 != 0 { 10 } else { 0 }
    } else {
        0
    }
}

pub(crate) fn flac_blocks(data: &[u8], start: usize) -> Result<(Vec<(u8, Vec<u8>)>, usize), EmbedError> {
    let mut pos = start + 4;
    let mut blocks = vec![];
    loop {
        if pos + 4 > data.len() {
            return err("The FLAC file ends inside its metadata.");
        }
        let head = data[pos];
        let size = ((data[pos + 1] as usize) << 16) | ((data[pos + 2] as usize) << 8) | data[pos + 3] as usize;
        if pos + 4 + size > data.len() {
            return err("The FLAC file ends inside its metadata.");
        }
        blocks.push((head & 0x7F, data[pos + 4..pos + 4 + size].to_vec()));
        pos += 4 + size;
        if head & 0x80 != 0 {
            return Ok((blocks, pos));
        }
    }
}

fn le32(b: &[u8], at: usize) -> Result<usize, EmbedError> {
    b.get(at..at + 4)
        .map(|x| u32::from_le_bytes([x[0], x[1], x[2], x[3]]) as usize)
        .ok_or_else(|| EmbedError::Format("The FLAC file's Vorbis comment is damaged.".into()))
}

fn parse_vorbis(body: &[u8]) -> Result<(Vec<u8>, Vec<Vec<u8>>), EmbedError> {
    let damaged = || EmbedError::Format("The FLAC file's Vorbis comment is damaged.".into());
    let vlen = le32(body, 0)?;
    let vendor = body.get(4..4 + vlen).ok_or_else(damaged)?.to_vec();
    let count = le32(body, 4 + vlen)?;
    let mut pos = 8 + vlen;
    let mut comments = vec![];
    for _ in 0..count {
        let n = le32(body, pos)?;
        comments.push(body.get(pos + 4..pos + 4 + n).ok_or_else(damaged)?.to_vec());
        pos += 4 + n;
    }
    Ok((vendor, comments))
}

pub(crate) fn vorbis_body(vendor: &[u8], comments: &[Vec<u8>]) -> Vec<u8> {
    let mut out = (vendor.len() as u32).to_le_bytes().to_vec();
    out.extend_from_slice(vendor);
    out.extend_from_slice(&(comments.len() as u32).to_le_bytes());
    for c in comments {
        out.extend_from_slice(&(c.len() as u32).to_le_bytes());
        out.extend_from_slice(c);
    }
    out
}

pub(crate) fn comment_key(c: &[u8]) -> String {
    let key = c.split(|b| *b == b'=').next().unwrap_or(&[]);
    String::from_utf8_lossy(key).to_uppercase()
}

/// The FLAC file with its Vorbis comments replaced by `edit(comments)`; every
/// other metadata block and the audio stay as they are (Python `flac_rewrite`).
pub fn flac_rewrite(
    data: &[u8],
    edit: impl FnOnce(Vec<Vec<u8>>) -> Vec<Vec<u8>>,
    too_long: &str,
) -> Result<Vec<u8>, EmbedError> {
    let edit_blocks = |mut blocks: Vec<(u8, Vec<u8>)>| -> Result<Vec<(u8, Vec<u8>)>, EmbedError> {
        let mut vendor = VENDOR.as_bytes().to_vec();
        let mut comments = vec![];
        let vi = blocks.iter().position(|(k, _)| *k == 4);
        if let Some(i) = vi {
            (vendor, comments) = parse_vorbis(&blocks[i].1)?;
        }
        let body = vorbis_body(&vendor, &edit(comments));
        match vi {
            Some(i) => blocks[i] = (4, body),
            None => blocks.insert(1.min(blocks.len()), (4, body)),
        }
        Ok(blocks)
    };
    flac_rewrite_blocks(data, edit_blocks, too_long)
}

/// The FLAC file with its metadata blocks replaced by `edit(blocks)` (type,
/// body); the padding is laid out again and the audio stays as it is (Python
/// `flac_rewrite_blocks`).
pub fn flac_rewrite_blocks(
    data: &[u8],
    edit: impl FnOnce(Vec<(u8, Vec<u8>)>) -> Result<Vec<(u8, Vec<u8>)>, EmbedError>,
    too_long: &str,
) -> Result<Vec<u8>, EmbedError> {
    let start = id3_len(data);
    if data.get(start..start + 4) != Some(b"fLaC") {
        return err("This is not a FLAC file.");
    }
    let (blocks, audio) = flac_blocks(data, start)?;
    let old_meta = audio - start - 4;
    let mut blocks = edit(blocks)?;
    if blocks.iter().any(|(_, b)| b.len() >= 1 << 24) {
        return err(too_long);
    }
    blocks.retain(|(k, _)| *k != 1);
    let used: usize = blocks.iter().map(|(_, b)| 4 + b.len()).sum();
    let pad = if old_meta >= used + 4 && old_meta - used - 4 <= MAX_KEPT_PADDING {
        old_meta - used - 4
    } else {
        NEW_PADDING
    };
    blocks.push((1, vec![0u8; pad]));
    let mut out = data[..start].to_vec();
    out.extend_from_slice(b"fLaC");
    let n = blocks.len();
    for (i, (kind, b)) in blocks.iter().enumerate() {
        out.push(kind | if i == n - 1 { 0x80 } else { 0 });
        out.extend_from_slice(&(b.len() as u32).to_be_bytes()[1..]);
        out.extend_from_slice(b);
    }
    out.extend_from_slice(&data[audio..]);
    Ok(out)
}

pub fn flac_with_lyrics(data: &[u8], synced: &str, plain: &str) -> Result<Vec<u8>, EmbedError> {
    let edit = |mut comments: Vec<Vec<u8>>| {
        comments.retain(|c| {
            let k = comment_key(c);
            k != FLAC_SYNCED && k != FLAC_PLAIN
        });
        if !synced.is_empty() {
            comments.push(format!("{FLAC_SYNCED}={synced}").into_bytes());
        }
        if !plain.is_empty() {
            comments.push(format!("{FLAC_PLAIN}={plain}").into_bytes());
        }
        comments
    };
    flac_rewrite(data, edit, "The lyrics are too long for a FLAC tag.")
}

pub fn flac_lyrics(data: &[u8]) -> Result<(String, String), EmbedError> {
    let start = id3_len(data);
    if data.get(start..start + 4) != Some(b"fLaC") {
        return Ok((String::new(), String::new()));
    }
    let (blocks, _) = flac_blocks(data, start)?;
    let (mut synced, mut plain) = (String::new(), String::new());
    for (kind, body) in &blocks {
        if *kind != 4 {
            continue;
        }
        for c in parse_vorbis(body)?.1 {
            let key = comment_key(&c);
            let value = match c.iter().position(|b| *b == b'=') {
                Some(i) => String::from_utf8_lossy(&c[i + 1..]).into_owned(),
                None => String::new(),
            };
            if key == FLAC_SYNCED && synced.is_empty() {
                synced = value;
            } else if key == FLAC_PLAIN && plain.is_empty() {
                plain = value;
            }
        }
    }
    Ok((synced, plain))
}

// ------------------------------------------------------------------- ID3 ---

pub type Frame = ([u8; 4], [u8; 2], Vec<u8>);

fn be32(b: &[u8]) -> usize {
    u32::from_be_bytes([b[0], b[1], b[2], b[3]]) as usize
}

fn id3_frames(data: &[u8]) -> Result<(u8, Vec<Frame>, usize), EmbedError> {
    if data.len() < 10 || &data[..3] != b"ID3" {
        return Ok((4, vec![], 0));
    }
    let (major, flags) = (data[3], data[5]);
    if major != 3 && major != 4 {
        return Err(EmbedError::Format(format!("The MP3 file has an ID3v2.{major} tag, which cannot be updated.")));
    }
    if flags & 0x80 != 0 {
        return err("The MP3 file's ID3 tag uses unsynchronisation, which cannot be updated.");
    }
    let size = syncsafe(&data[6..10]);
    let total = 10 + size + if major == 4 && flags & 0x10 != 0 { 10 } else { 0 };
    let end = (10 + size).min(data.len());
    let mut pos = 10;
    if flags & 0x40 != 0 && data.len() >= 14 {
        pos += if major == 4 { syncsafe(&data[10..14]) } else { be32(&data[10..14]) + 4 };
    }
    let mut frames = vec![];
    while pos + 10 <= end {
        let fid = &data[pos..pos + 4];
        if fid == [0, 0, 0, 0] || !fid.iter().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit()) {
            break;
        }
        let n = if major == 4 { syncsafe(&data[pos + 4..pos + 8]) } else { be32(&data[pos + 4..pos + 8]) };
        if pos + 10 + n > data.len() {
            return err("The MP3 file's ID3 tag is damaged.");
        }
        frames.push((
            [fid[0], fid[1], fid[2], fid[3]],
            [data[pos + 8], data[pos + 9]],
            data[pos + 10..pos + 10 + n].to_vec(),
        ));
        pos += 10 + n;
    }
    Ok((major, frames, total))
}

/// (encoding byte, encoded text, terminator)
pub(crate) fn enc(text: &str, major: u8) -> (u8, Vec<u8>, &'static [u8]) {
    if major == 4 {
        return (3, text.as_bytes().to_vec(), b"\x00");
    }
    let mut b = vec![0xFF, 0xFE];
    for u in text.encode_utf16() {
        b.extend_from_slice(&u.to_le_bytes());
    }
    (1, b, b"\x00\x00")
}

fn uslt(plain: &str, major: u8) -> Vec<u8> {
    let (e, text, term) = enc(plain, major);
    let (_, desc, _) = enc("", major);
    let mut out = vec![e];
    out.extend_from_slice(LANG);
    out.extend_from_slice(&desc);
    out.extend_from_slice(term);
    out.extend_from_slice(&text);
    out
}

fn sylt(entries: &[(u32, String)], major: u8) -> Vec<u8> {
    let (e, _, term) = enc("", major);
    let (_, desc, _) = enc("", major);
    let mut out = vec![e];
    out.extend_from_slice(LANG);
    out.extend_from_slice(&[2, 1]);
    out.extend_from_slice(&desc);
    out.extend_from_slice(term);
    for (ms, line) in entries {
        out.extend_from_slice(&enc(line, major).1);
        out.extend_from_slice(term);
        out.extend_from_slice(&ms.to_be_bytes());
    }
    out
}

/// The MP3 file with its ID3v2 frames replaced by `edit(major, frames)`; the
/// audio after the tag stays as it is (Python `mp3_rewrite`).
pub fn mp3_rewrite(
    data: &[u8],
    edit: impl FnOnce(u8, Vec<Frame>) -> Vec<Frame>,
    too_long: &str,
) -> Result<Vec<u8>, EmbedError> {
    let (major, frames, total) = id3_frames(data)?;
    let mut body = vec![];
    for (id, flags, fb) in &edit(major, frames) {
        body.extend_from_slice(id);
        if major == 4 {
            body.extend_from_slice(&to_syncsafe(fb.len()));
        } else {
            body.extend_from_slice(&(fb.len() as u32).to_be_bytes());
        }
        body.extend_from_slice(flags);
        body.extend_from_slice(fb);
    }
    let old_body = if total > 0 { total - 10 } else { 0 };
    let pad = if total == 0 || old_body < body.len() { NEW_PADDING } else { old_body - body.len() };
    body.resize(body.len() + pad, 0);
    if body.len() >= 1 << 28 {
        return err(too_long);
    }
    let mut out = b"ID3".to_vec();
    out.extend_from_slice(&[major, 0, 0]);
    out.extend_from_slice(&to_syncsafe(body.len()));
    out.extend_from_slice(&body);
    out.extend_from_slice(&data[total.min(data.len())..]);
    Ok(out)
}

pub fn mp3_with_lyrics(data: &[u8], synced: &str, plain: &str) -> Result<Vec<u8>, EmbedError> {
    let edit = |major: u8, mut frames: Vec<Frame>| {
        frames.retain(|(id, _, _)| id != b"USLT" && id != b"SYLT");
        let entries = if synced.is_empty() { vec![] } else { lrc_entries(synced) };
        if !entries.is_empty() {
            frames.push((*b"SYLT", [0, 0], sylt(&entries, major)));
        }
        if !plain.is_empty() {
            frames.push((*b"USLT", [0, 0], uslt(plain, major)));
        }
        frames
    };
    mp3_rewrite(data, edit, "The lyrics are too long for an ID3 tag.")
}

fn split_text(b: &[u8], encoding: u8) -> (String, &[u8]) {
    if encoding == 1 || encoding == 2 {
        let mut i = 0;
        while i + 1 < b.len() && !(b[i] == 0 && b[i + 1] == 0) {
            i += 2;
        }
        let raw = &b[..i.min(b.len())];
        let rest = b.get(i + 2..).unwrap_or(&[]);
        return (decode_utf16(raw, encoding == 2), rest);
    }
    let i = b.iter().position(|x| *x == 0).unwrap_or(b.len());
    let text = if encoding == 3 {
        String::from_utf8_lossy(&b[..i]).into_owned()
    } else {
        b[..i].iter().map(|c| *c as char).collect()
    };
    (text, b.get(i + 1..).unwrap_or(&[]))
}

fn decode_utf16(raw: &[u8], big_endian: bool) -> String {
    let (mut be, mut data) = (big_endian, raw);
    if !big_endian && raw.len() >= 2 {
        if raw[..2] == [0xFF, 0xFE] {
            data = &raw[2..];
        } else if raw[..2] == [0xFE, 0xFF] {
            be = true;
            data = &raw[2..];
        }
    }
    let units: Vec<u16> = data
        .chunks_exact(2)
        .map(|c| if be { u16::from_be_bytes([c[0], c[1]]) } else { u16::from_le_bytes([c[0], c[1]]) })
        .collect();
    String::from_utf16_lossy(&units)
}

fn sylt_to_lrc(body: &[u8]) -> String {
    let (encoding, fmt) = (body[0], body[4]);
    let (_, mut rest) = split_text(&body[6..], encoding);
    let mut lines = vec![];
    while !rest.is_empty() {
        let (text, after) = split_text(rest, encoding);
        if after.len() < 4 {
            break;
        }
        let t = be32(after);
        rest = &after[4..];
        if fmt == 2 {
            let line = format!("[{:02}:{:02}.{:02}] {text}", t / 60000, t / 1000 % 60, t % 1000 / 10);
            lines.push(line.trim_end_matches(crate::pyunicode::is_space).to_string());
        }
    }
    lines.join("\n")
}

pub fn mp3_lyrics(data: &[u8]) -> (String, String) {
    let Ok((_, frames, _)) = id3_frames(data) else {
        return (String::new(), String::new());
    };
    let (mut synced, mut plain) = (String::new(), String::new());
    for (id, _, body) in &frames {
        if id == b"USLT" && plain.is_empty() && body.len() > 4 {
            let (_, rest) = split_text(&body[4..], body[0]);
            plain = if body[0] == 1 || body[0] == 2 {
                decode_utf16(rest, body[0] == 2)
            } else if body[0] == 3 {
                String::from_utf8_lossy(rest).into_owned()
            } else {
                rest.iter().map(|c| *c as char).collect()
            };
            plain = plain.trim_end_matches('\u{0}').to_string();
        } else if id == b"SYLT" && synced.is_empty() && body.len() > 6 {
            synced = sylt_to_lrc(body);
        }
    }
    (synced, plain)
}

// -------------------------------------------------------------- the files ---

/// Only the tag area of the file (Python `_tag_bytes`).
fn tag_bytes(path: &str, ext: &str) -> std::io::Result<Vec<u8>> {
    let mut f = std::fs::File::open(path)?;
    let mut head = [0u8; 10];
    let n = f.read(&mut head)?;
    let mut lead = vec![];
    if n == 10 && &head[..3] == b"ID3" {
        f.seek(SeekFrom::Start(0))?;
        lead.resize(id3_len(&head), 0);
        let got = f.read(&mut lead)?;
        lead.truncate(got);
    }
    if ext == ".mp3" {
        return Ok(lead);
    }
    f.seek(SeekFrom::Start(lead.len() as u64))?;
    let mut magic = [0u8; 4];
    if f.read(&mut magic)? < 4 || &magic != b"fLaC" {
        return Ok(vec![]);
    }
    loop {
        let mut h = [0u8; 4];
        if f.read(&mut h)? < 4 {
            break;
        }
        let size = ((h[1] as usize) << 16) | ((h[2] as usize) << 8) | h[3] as usize;
        if h[0] & 0x7F == 4 {
            let mut body = vec![0u8; size];
            f.read_exact(&mut body)?;
            let mut out = b"fLaC".to_vec();
            out.push(0x84);
            out.extend_from_slice(&h[1..4]);
            out.extend_from_slice(&body);
            return Ok(out);
        }
        if h[0] & 0x80 != 0 {
            break;
        }
        f.seek(SeekFrom::Current(size as i64))?;
    }
    Ok(b"fLaC\x81\x00\x00\x00".to_vec())
}

/// (synced, plain) lyrics inside an MP3 or FLAC file ("" when none).
pub fn embedded_lyrics(path: &str) -> (String, String) {
    let ext = extension(&file_name(path)).to_lowercase();
    if !SUPPORTED.contains(&ext.as_str()) {
        return (String::new(), String::new());
    }
    let Ok(data) = tag_bytes(path, &ext) else {
        return (String::new(), String::new());
    };
    if ext == ".flac" {
        flac_lyrics(&data).unwrap_or_default()
    } else {
        mp3_lyrics(&data)
    }
}

/// Write the lyrics into the file's tag (replacing earlier ones).
pub fn embed_lyrics(path: &str, synced: &str, plain: &str) -> Result<(), EmbedError> {
    let ext = extension(&file_name(path)).to_lowercase();
    if !SUPPORTED.contains(&ext.as_str()) {
        return err("Lyrics can only be embedded in MP3 and FLAC files.");
    }
    let data = std::fs::read(path)?;
    let new =
        if ext == ".flac" { flac_with_lyrics(&data, synced, plain)? } else { mp3_with_lyrics(&data, synced, plain)? };
    replace_file(path, &new).map_err(EmbedError::Io)
}

/// Write `data` over the file through a temporary file in its folder (keeping
/// its permissions), so a failure never leaves half a file (Python `replace_file`).
/// 2.4: on Windows a file that another program has open (FFmpeg reading it for the mood
/// analysis or a scan, an antivirus scanner, the search indexer) cannot be replaced for a
/// moment, so the rename is tried again for up to this long (Python `REPLACE_RETRY_S`).
pub const REPLACE_RETRY_S: f64 = 5.0;

/// `fs::rename(tmp, path)`, tried again while Windows says the file is in use (access
/// denied or a sharing violation); elsewhere a rename never waits for readers.
pub fn rename_retrying(tmp: &Path, path: &str) -> std::io::Result<()> {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs_f64(REPLACE_RETRY_S);
    let mut delay = std::time::Duration::from_millis(50);
    loop {
        match std::fs::rename(tmp, path) {
            Ok(()) => return Ok(()),
            Err(e) if cfg!(windows)
                && (e.kind() == std::io::ErrorKind::PermissionDenied || e.raw_os_error() == Some(32))
                && std::time::Instant::now() < deadline =>
            {
                std::thread::sleep(delay);
                delay = (delay * 2).min(std::time::Duration::from_millis(400));
            }
            Err(e) => return Err(e),
        }
    }
}

pub fn replace_file(path: &str, data: &[u8]) -> std::io::Result<()> {
    let ext = extension(&file_name(path)).to_lowercase();
    let folder = crate::paths::parent(&crate::paths::abspath(path));
    static N: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let tmp = Path::new(&folder).join(format!(
        ".realhires-embed-{}-{}{ext}",
        std::process::id(),
        N.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ));
    let res = (|| -> std::io::Result<()> {
        let mut f = std::fs::OpenOptions::new().write(true).create_new(true).open(&tmp)?;
        f.write_all(data)?;
        drop(f);
        if let Ok(meta) = std::fs::metadata(path) {
            let _ = std::fs::set_permissions(&tmp, meta.permissions());
        }
        rename_retrying(&tmp, path)
    })();
    if res.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    res
}

/// (kind, text) of the lyrics embedded in a song, for display (Python `lyrics_of`): the
/// synced lyrics, else the plain ones, which count as synced when they are LRC.
pub fn lyrics_of(path: &str) -> Option<(crate::lyrics::Status, String)> {
    use crate::lyrics::{is_instrumental_lrc, Status};
    if !supported(path) {
        return None;
    }
    let (synced, plain) = embedded_lyrics(path);
    if !synced.is_empty() {
        let kind = if is_instrumental_lrc(&synced) { Status::Instrumental } else { Status::Synced };
        return Some((kind, synced));
    }
    if !plain.is_empty() {
        let kind = if is_instrumental_lrc(&plain) {
            Status::Instrumental
        } else if looks_synced(&plain) {
            Status::Synced
        } else {
            Status::Plain
        };
        return Some((kind, plain));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lrc_lines_like_python() {
        let s = "[ar: Band]\n[00:00.50] First line ♪\n[00:01.20][00:02.40] Twice\n[00:03.00] 日本語の行";
        assert_eq!(
            lrc_entries(s),
            vec![
                (500, "First line ♪".to_string()),
                (1200, "Twice".into()),
                (2400, "Twice".into()),
                (3000, "日本語の行".into())
            ]
        );
        assert_eq!(lrc_entries("[01:02.5]x\n[1:02.345] y"), vec![(62345, "y".to_string()), (62500, "x".into())]);
    }

    #[test]
    fn flac_and_mp3_round_trip_in_memory() {
        // a minimal FLAC: STREAMINFO, a Vorbis comment and padding, then "audio"
        let mut flac = b"fLaC".to_vec();
        flac.push(0);
        flac.extend_from_slice(&[0, 0, 34]);
        flac.extend_from_slice(&[7u8; 34]);
        let vc = vorbis_body(b"x", &[b"TITLE=t".to_vec()]);
        flac.push(4);
        flac.extend_from_slice(&(vc.len() as u32).to_be_bytes()[1..]);
        flac.extend_from_slice(&vc);
        flac.push(0x81);
        flac.extend_from_slice(&[0, 4, 0]);
        flac.extend_from_slice(&[0u8; 1024]);
        flac.extend_from_slice(b"AUDIO");
        let out = flac_with_lyrics(&flac, "[00:01.00] la", "la").unwrap();
        assert_eq!(out.len(), flac.len());
        assert!(out.ends_with(b"AUDIO"));
        assert_eq!(flac_lyrics(&out).unwrap(), ("[00:01.00] la".to_string(), "la".to_string()));
        let mp3 = b"\xff\xfbAUDIO".to_vec();
        for major in [3u8, 4] {
            let mut tagged = b"ID3".to_vec();
            tagged.extend_from_slice(&[major, 0, 0]);
            tagged.extend_from_slice(&to_syncsafe(100));
            tagged.extend_from_slice(&[0u8; 100]);
            tagged.extend_from_slice(&mp3);
            let out = mp3_with_lyrics(&tagged, "[00:01.00] la ♪", "la ♪").unwrap();
            assert!(out.ends_with(&mp3));
            assert_eq!(mp3_lyrics(&out), ("[00:01.00] la ♪".to_string(), "la ♪".to_string()));
        }
    }
}
