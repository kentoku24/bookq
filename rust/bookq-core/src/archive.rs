//! Archive readers — Phase 1 W2 (issue #163).
//!
//! Byte-for-byte ports of the *listing/reading* semantics of
//! `app/archive.py`'s readers (CPython `zipfile` / PyPI `rarfile`).
//!
//! The contract is conservative on purpose: every public function returns
//! `Option` — `None` means "we could not prove the Python implementation
//! would produce exactly this result, fall back". `Some` values must be
//! identical to what CPython's `zipfile` / `rarfile` would return, including
//! ordering, sanitisation, and which failures yield an empty list versus a
//! partial one.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

// ══════════════════════════════════════════════════════════════════════════
// Shared decoding helpers
// ══════════════════════════════════════════════════════════════════════════

/// IBM437 code page, high half (0x80..=0xFF). Python `bytes.decode("cp437")`
/// never fails; every byte maps to exactly one char.
const CP437_HIGH: [char; 128] = [
    'Ç', 'ü', 'é', 'â', 'ä', 'à', 'å', 'ç', 'ê', 'ë', 'è', 'ï', 'î', 'ì', 'Ä', 'Å',
    'É', 'æ', 'Æ', 'ô', 'ö', 'ò', 'û', 'ù', 'ÿ', 'Ö', 'Ü', '¢', '£', '¥', '₧', 'ƒ',
    'á', 'í', 'ó', 'ú', 'ñ', 'Ñ', 'ª', 'º', '¿', '⌐', '¬', '½', '¼', '¡', '«', '»',
    '░', '▒', '▓', '│', '┤', '╡', '╢', '╖', '╕', '╣', '║', '╗', '╝', '╜', '╛', '┐',
    '└', '┴', '┬', '├', '─', '┼', '╞', '╟', '╚', '╔', '╩', '╦', '╠', '═', '╬', '╧',
    '╨', '╤', '╥', '╙', '╘', '╒', '╓', '║', '╪', '┘', '┌', '█', '▄', '▌', '▐', '▀',
    'α', 'ß', 'Γ', 'π', 'Σ', 'σ', 'µ', 'τ', 'Φ', 'Θ', 'Ω', 'δ', '∞', 'φ', 'ε', '∩',
    '≡', '±', '≥', '≤', '⌠', '⌡', '÷', '≈', '°', '∙', '·', '√', 'ⁿ', '²', '■', '\u{00A0}',
];

fn decode_cp437(b: &[u8]) -> String {
    b.iter()
        .map(|&c| if c < 0x80 { c as char } else { CP437_HIGH[(c - 0x80) as usize] })
        .collect()
}

/// windows-1252 with Python "replace" semantics: bytes 0x81,0x8D,0x8F,0x90,0x9D
/// are undefined in the codec and decode to U+FFFD each.
fn decode_cp1252_replace(b: &[u8]) -> String {
    const HIGH: [Option<char>; 32] = [
        Some('€'), None, Some('‚'), Some('ƒ'), Some('„'), Some('…'), Some('†'), Some('‡'),
        Some('ˆ'), Some('‰'), Some('Š'), Some('‹'), Some('Œ'), None, Some('Ž'), None,
        None, Some('‘'), Some('’'), Some('“'), Some('”'), Some('•'), Some('–'), Some('—'),
        Some('˜'), Some('™'), Some('š'), Some('›'), Some('œ'), None, Some('ž'), Some('Ÿ'),
    ];
    b.iter()
        .map(|&c| match c {
            0x00..=0x7F => c as char,
            0x80..=0x9F => HIGH[(c - 0x80) as usize].unwrap_or('\u{FFFD}'),
            _ => c as char,
        })
        .collect()
}

/// Python `decode("utf-16le")` strict: fails on odd length and unpaired
/// surrogates, mirroring the codec's errors.
fn decode_utf16le_strict(b: &[u8]) -> Option<String> {
    if b.len() % 2 != 0 {
        return None;
    }
    let units: Vec<u16> = b
        .chunks_exact(2)
        .map(|c| u16::from_le_bytes([c[0], c[1]]))
        .collect();
    let mut s = String::new();
    for r in char::decode_utf16(units) {
        match r {
            Ok(c) => s.push(c),
            Err(_) => return None,
        }
    }
    Some(s)
}

/// rarfile `_decode`: TRY_ENCODINGS = utf8, utf-16le, then charset
/// (windows-1252) with errors="replace".
fn decode_rar3_fallback(b: &[u8]) -> String {
    if let Ok(s) = std::str::from_utf8(b) {
        return s.to_string();
    }
    if let Some(s) = decode_utf16le_strict(b) {
        return s;
    }
    decode_cp1252_replace(b)
}

/// Python `decode("utf8", "replace")` == String::from_utf8_lossy.
fn decode_utf8_replace(b: &[u8]) -> String {
    String::from_utf8_lossy(b).into_owned()
}

fn load_le32(b: &[u8], pos: usize) -> Option<(u32, usize)> {
    let end = pos.checked_add(4)?;
    if end > b.len() {
        return None;
    }
    Some((u32::from_le_bytes(b[pos..end].try_into().ok()?), end))
}

fn load_le16(b: &[u8], pos: usize) -> Option<(u16, usize)> {
    let end = pos.checked_add(2)?;
    if end > b.len() {
        return None;
    }
    Some((u16::from_le_bytes(b[pos..end].try_into().ok()?), end))
}

fn load_byte(b: &[u8], pos: usize) -> Option<(u8, usize)> {
    if pos + 1 > b.len() {
        return None;
    }
    Some((b[pos], pos + 1))
}

fn load_bytes<'a>(b: &'a [u8], num: usize, pos: usize) -> Option<(&'a [u8], usize)> {
    let end = pos.checked_add(num)?;
    if end > b.len() {
        return None;
    }
    Some((&b[pos..end], end))
}

/// rarfile `load_vint`: up to 11 bytes, 7-bit groups; overrun → BadRarFile
/// (fatal in the RAR5 path).
fn load_vint(b: &[u8], mut pos: usize) -> Option<(u64, usize)> {
    let limit = std::cmp::min(pos + 11, b.len());
    let (mut res, mut ofs) = (0u128, 0u32);
    while pos < limit {
        let byte = b[pos];
        res += ((byte & 0x7F) as u128) << ofs;
        pos += 1;
        ofs += 7;
        if byte < 0x80 {
            return Some((u64::try_from(res).unwrap_or(u64::MAX), pos));
        }
    }
    None
}

fn load_vstr<'a>(b: &'a [u8], pos: usize) -> Option<(&'a [u8], usize)> {
    let (slen, pos) = load_vint(b, pos)?;
    load_bytes(b, slen as usize, pos)
}

/// datetime.fromtimestamp(secs, utc) range guard: must land inside
/// datetime(1..9999). Anything else raises (fatal in the RAR5 path).
fn unixtime_ok(secs: i64) -> bool {
    (-62135596800..=253402300799).contains(&secs)
}

// ══════════════════════════════════════════════════════════════════════════
// ZIP / CBZ — port of zipfile._EndRecData/_EndRecData64/_RealGetContents
// ══════════════════════════════════════════════════════════════════════════

const EOCD_SIG: &[u8] = b"PK\x05\x06";
const EOCD64_LOC_SIG: &[u8] = b"PK\x06\x07";
const EOCD64_SIG: &[u8] = b"PK\x06\x06";
const CD_SIG: &[u8] = b"PK\x01\x02";
const LFH_SIG: &[u8] = b"PK\x03\x04";
const SIZE_EOCD: usize = 22;
const SIZE_EOCD64_LOC: usize = 20;
const SIZE_EOCD64: usize = 56;
const SIZE_CD_ENTRY: usize = 46;
const SIZE_LFH: usize = 30;
const ZIP_MAX_COMMENT: usize = 65535;
const MASK_UTF_FILENAME: u16 = 1 << 11;
const MASK_ENCRYPTED: u16 = 1;
const MAX_EXTRACT_VERSION: u8 = 63;

#[derive(Debug, Clone)]
pub struct ZipEntry {
    pub filename: String,
    pub flag_bits: u16,
    pub compress_type: u16,
    pub crc: u32,
    pub compress_size: u64,
    pub file_size: u64,
    pub header_offset: i64, // absolute, concat-adjusted (Python semantics)
    pub end_offset: i64,    // next entry's header_offset; last = start_dir
}

struct EndRec {
    size_cd: u64,
    offset_cd: u64,
    location: i64, // file offset where the EOCD record was found
}

fn read_endrec64(f: &mut File, mut offset: i64, e: &mut EndRec) -> Result<(), ()> {
    offset -= SIZE_EOCD64_LOC as i64;
    if offset < 0 {
        return Ok(());
    }
    let mut loc = [0u8; SIZE_EOCD64_LOC];
    if f.seek(SeekFrom::Start(offset as u64)).is_err() || f.read_exact(&mut loc).is_err() {
        return Err(());
    }
    let (sig, diskno, reloff, disks) = (
        &loc[0..4],
        u32::from_le_bytes(loc[4..8].try_into().unwrap()),
        u64::from_le_bytes(loc[8..16].try_into().unwrap()),
        u32::from_le_bytes(loc[16..20].try_into().unwrap()),
    );
    if sig != EOCD64_LOC_SIG {
        return Ok(());
    }
    if diskno != 0 || disks > 1 {
        return Err(());
    }
    offset -= SIZE_EOCD64 as i64;
    if reloff as i64 > offset {
        return Err(());
    }
    let mut data = [0u8; SIZE_EOCD64];
    let mut extrasz: i64 = offset - reloff as i64;
    if f.seek(SeekFrom::Start(reloff)).is_err() || f.read_exact(&mut data).is_err() {
        return Err(());
    }
    if &data[0..4] != EOCD64_SIG && (reloff as i64) != offset {
        extrasz = 0;
        if f.seek(SeekFrom::Start(offset as u64)).is_err() || f.read_exact(&mut data).is_err() {
            return Err(());
        }
    }
    if &data[0..4] != EOCD64_SIG {
        return Err(());
    }
    let sz = u64::from_le_bytes(data[4..12].try_into().unwrap());
    let dirsize = u64::from_le_bytes(data[40..48].try_into().unwrap());
    let diroffset = u64::from_le_bytes(data[48..56].try_into().unwrap());
    if diroffset + dirsize != reloff || sz + 12 != SIZE_EOCD64 as u64 + extrasz as u64 {
        return Err(());
    }
    e.size_cd = dirsize;
    e.offset_cd = diroffset;
    e.location = offset - extrasz;
    Ok(())
}

/// zipfile._EndRecData + _EndRecData64.
/// Ok(None) → not a zip; Err → Python raised (caller maps to []).
fn read_endrec(f: &mut File) -> Result<Option<EndRec>, ()> {
    let filesize = f.metadata().map_err(|_| ())?.len() as i64;
    if filesize < SIZE_EOCD as i64 {
        return Ok(None);
    }
    {
        if f.seek(SeekFrom::End(-(SIZE_EOCD as i64))).is_err() {
            return Ok(None);
        }
        let mut data = [0u8; SIZE_EOCD];
        if f.read_exact(&mut data).is_err() {
            return Err(());
        }
        if &data[0..4] == EOCD_SIG && data[20] == 0 && data[21] == 0 {
            let mut e = EndRec {
                size_cd: u32::from_le_bytes(data[12..16].try_into().unwrap()) as u64,
                offset_cd: u32::from_le_bytes(data[16..20].try_into().unwrap()) as u64,
                location: filesize - SIZE_EOCD as i64,
            };
            read_endrec64(f, e.location, &mut e)?;
            return Ok(Some(e));
        }
    }
    let max_start = std::cmp::max(filesize - ZIP_MAX_COMMENT as i64 - SIZE_EOCD as i64, 0);
    if f.seek(SeekFrom::Start(max_start as u64)).is_err() {
        return Err(());
    }
    let mut data = Vec::new();
    if f.read_to_end(&mut data).is_err() {
        return Err(());
    }
    let start = match data.windows(4).rposition(|w| w == EOCD_SIG) {
        Some(s) => s,
        None => return Ok(None),
    };
    if start + SIZE_EOCD > data.len() {
        return Ok(None);
    }
    let rec = &data[start..start + SIZE_EOCD];
    let mut e = EndRec {
        size_cd: u32::from_le_bytes(rec[12..16].try_into().unwrap()) as u64,
        offset_cd: u32::from_le_bytes(rec[16..20].try_into().unwrap()) as u64,
        location: max_start + start as i64,
    };
    read_endrec64(f, e.location, &mut e)?;
    Ok(Some(e))
}

/// Python `_sanitize_filename` on POSIX: truncate at first NUL.
/// (os.sep == "/" on Linux/macOS, so the separator rewrite is a no-op.)
fn sanitize_filename(s: &str) -> String {
    match s.find('\0') {
        Some(i) => s[..i].to_string(),
        None => s.to_string(),
    }
}

/// Port of `ZipInfo._decodeExtra` — mutates sizes/offset/filename.
/// Err == BadZipFile → whole CD listing fails.
fn decode_extra(
    extra: &[u8],
    file_size: &mut u64,
    compress_size: &mut u64,
    header_offset: &mut u64,
    filename: &mut String,
    filename_crc: u32,
) -> Result<(), ()> {
    let mut extra = extra;
    while extra.len() >= 4 {
        let tp = u16::from_le_bytes(extra[0..2].try_into().unwrap());
        let ln = u16::from_le_bytes(extra[2..4].try_into().unwrap()) as usize;
        if ln + 4 > extra.len() {
            return Err(());
        }
        let data = &extra[4..4 + ln];
        if tp == 0x0001 {
            let mut d = data;
            // order: file_size → compress_size → header_offset; only pulled
            // when the CD field was the 0xFFFFFFFF sentinel.
            if *file_size == 0xFFFF_FFFF_FFFF_FFFF || *file_size == 0xFFFF_FFFF {
                if d.len() < 8 {
                    return Err(());
                }
                *file_size = u64::from_le_bytes(d[..8].try_into().unwrap());
                d = &d[8..];
            }
            if *compress_size == 0xFFFF_FFFF {
                if d.len() < 8 {
                    return Err(());
                }
                *compress_size = u64::from_le_bytes(d[..8].try_into().unwrap());
                d = &d[8..];
            }
            if *header_offset == 0xFFFF_FFFF {
                if d.len() < 8 {
                    return Err(());
                }
                *header_offset = u64::from_le_bytes(d[..8].try_into().unwrap());
            }
        } else if tp == 0x7075 {
            if data.len() < 5 {
                return Err(());
            }
            let up_version = data[0];
            let up_name_crc = u32::from_le_bytes(data[1..5].try_into().unwrap());
            if up_version == 1 && up_name_crc == filename_crc {
                let up = std::str::from_utf8(&data[5..]).map_err(|_| ())?;
                if !up.is_empty() {
                    *filename = sanitize_filename(up);
                }
            }
        }
        extra = &extra[ln + 4..];
    }
    Ok(())
}

/// Port of `ZipFile._RealGetContents` (names + read metadata).
/// Ok(None) → Python "not a zip" → []; Err → Python BadZipFile → [].
fn read_cd(f: &mut File) -> Result<Option<Vec<ZipEntry>>, ()> {
    let endrec = match read_endrec(f)? {
        Some(e) => e,
        None => return Ok(None),
    };
    let concat: i64 = endrec.location - endrec.size_cd as i64 - endrec.offset_cd as i64;
    let start_dir: i64 = endrec.offset_cd as i64 + concat;
    if start_dir < 0 {
        return Err(());
    }
    if f.seek(SeekFrom::Start(start_dir as u64)).is_err() {
        return Err(());
    }
    let mut data = vec![0u8; endrec.size_cd as usize];
    if endrec.size_cd > 0 && f.read_exact(&mut data).is_err() {
        return Err(());
    }
    let mut entries: Vec<ZipEntry> = Vec::new();
    let mut total: u64 = 0;
    let mut pos: usize = 0;
    while total < endrec.size_cd {
        if pos + SIZE_CD_ENTRY > data.len() {
            return Err(());
        }
        let h = &data[pos..pos + SIZE_CD_ENTRY];
        if &h[0..4] != CD_SIG {
            return Err(());
        }
        let extract_version = h[6];
        let flag_bits = u16::from_le_bytes(h[8..10].try_into().unwrap());
        let compress_type = u16::from_le_bytes(h[10..12].try_into().unwrap());
        let crc = u32::from_le_bytes(h[16..20].try_into().unwrap());
        let mut compress_size = u32::from_le_bytes(h[20..24].try_into().unwrap()) as u64;
        let mut file_size = u32::from_le_bytes(h[24..28].try_into().unwrap()) as u64;
        let fnlen = u16::from_le_bytes(h[28..30].try_into().unwrap()) as usize;
        let extralen = u16::from_le_bytes(h[30..32].try_into().unwrap()) as usize;
        let commentlen = u16::from_le_bytes(h[32..34].try_into().unwrap()) as usize;
        let mut header_offset = u32::from_le_bytes(h[42..46].try_into().unwrap()) as u64;
        pos += SIZE_CD_ENTRY;

        if pos + fnlen > data.len() {
            return Err(());
        }
        let raw_name = &data[pos..pos + fnlen];
        let filename_crc = crc32fast::hash(raw_name);
        let mut filename = if flag_bits & MASK_UTF_FILENAME != 0 {
            match std::str::from_utf8(raw_name) {
                Ok(s) => s.to_string(),
                Err(_) => return Err(()),
            }
        } else {
            decode_cp437(raw_name)
        };
        pos += fnlen;
        if extract_version > MAX_EXTRACT_VERSION {
            return Err(());
        }
        if pos + extralen + commentlen > data.len() {
            return Err(());
        }
        let extra = data[pos..pos + extralen].to_vec();
        pos += extralen;
        pos += commentlen;

        filename = sanitize_filename(&filename); // ZipInfo.__init__, POSIX
        decode_extra(
            &extra,
            &mut file_size,
            &mut compress_size,
            &mut header_offset,
            &mut filename,
            filename_crc,
        )?;
        entries.push(ZipEntry {
            filename,
            flag_bits,
            compress_type,
            crc,
            compress_size,
            file_size,
            header_offset: header_offset as i64 + concat,
            end_offset: 0,
        });
        total = total
            + SIZE_CD_ENTRY as u64
            + fnlen as u64
            + extralen as u64
            + commentlen as u64;
    }
    let mut order: Vec<usize> = (0..entries.len()).collect();
    order.sort_by_key(|&i| entries[i].header_offset);
    let mut end = start_dir;
    for &i in order.iter().rev() {
        entries[i].end_offset = end;
        end = entries[i].header_offset;
    }
    Ok(Some(entries))
}

/// Internal read-by-name outcome: distinguishes "proven Python None" from
/// "uncertain → Python fallback".
enum ReadOutcome {
    Data(Vec<u8>),
    None, // Python would return None (name absent / encrypted)
    Bail, // uncertain — let the Python implementation decide
}

fn zip_read_with(f: &mut File, entries: &[ZipEntry], inner: &str) -> ReadOutcome {
    // NameToInfo: last duplicate wins.
    let e = match entries.iter().rev().find(|e| e.filename == inner) {
        Some(e) => e,
        None => return ReadOutcome::None,
    };
    zip_read_data(f, e)
}

fn zip_read_data(f: &mut File, e: &ZipEntry) -> ReadOutcome {
    if e.flag_bits & MASK_ENCRYPTED != 0 {
        return ReadOutcome::None; // RuntimeError → None
    }
    if e.filename.ends_with('/') {
        return ReadOutcome::Bail; // directory entry → Python decides
    }
    if e.header_offset < 0 {
        return ReadOutcome::Bail;
    }
    let mut lfh = [0u8; SIZE_LFH];
    if f.seek(SeekFrom::Start(e.header_offset as u64)).is_err()
        || f.read_exact(&mut lfh).is_err()
    {
        return ReadOutcome::Bail;
    }
    if &lfh[0..4] != LFH_SIG {
        return ReadOutcome::Bail;
    }
    let fnlen = u16::from_le_bytes(lfh[26..28].try_into().unwrap()) as u64;
    let extralen = u16::from_le_bytes(lfh[28..30].try_into().unwrap()) as u64;
    let data_start = e.header_offset + SIZE_LFH as i64 + fnlen as i64 + extralen as i64;
    if data_start < 0 || data_start + e.compress_size as i64 > e.end_offset {
        return ReadOutcome::Bail; // "Overlapped entries" → BadZipFile
    }
    let mut comp = vec![0u8; e.compress_size as usize];
    if f.seek(SeekFrom::Start(data_start as u64)).is_err()
        || f.read_exact(&mut comp).is_err()
    {
        return ReadOutcome::Bail;
    }
    let out: Vec<u8> = match e.compress_type {
        0 => comp, // stored
        8 => {
            use flate2::{Decompress, FlushDecompress};
            let mut d = Decompress::new(false); // raw deflate
            let mut out = Vec::with_capacity(e.file_size as usize);
            match d.decompress_vec(&comp, &mut out, FlushDecompress::Finish) {
                Ok(_) => out,
                Err(_) => return ReadOutcome::Bail,
            }
        }
        _ => return ReadOutcome::Bail, // bzip2/lzma/zstd → Python handles
    };
    if out.len() as u64 != e.file_size {
        return ReadOutcome::Bail;
    }
    if crc32fast::hash(&out) != e.crc {
        return ReadOutcome::Bail;
    }
    ReadOutcome::Data(out)
}

pub fn zip_names(path: &Path) -> Option<Vec<String>> {
    let mut f = File::open(path).ok()?;
    match read_cd(&mut f) {
        Ok(Some(entries)) => Some(entries.into_iter().map(|e| e.filename).collect()),
        _ => None,
    }
}

/// `zip_read_entry(path, inner)` →
///   Some(bytes) — proven identical to Python's bytes
///   None        — uncertain or proven-empty; Python fallback decides
/// (callers fall back to the Python reader either way)
pub fn zip_read_entry(path: &Path, inner: &str) -> Option<Vec<u8>> {
    let mut f = File::open(path).ok()?;
    let entries = match read_cd(&mut f) {
        Ok(Some(e)) => e,
        _ => return None,
    };
    match zip_read_with(&mut f, &entries, inner) {
        ReadOutcome::Data(b) => Some(b),
        _ => None,
    }
}

// ══════════════════════════════════════════════════════════════════════════
// EPUB — port of _epub_entries/_epub_spine_images/_epub_resolve
// ══════════════════════════════════════════════════════════════════════════

const NS_CONTAINER: &str = "urn:oasis:names:tc:opendocument:xmlns:container";
const NS_OPF: &str = "http://www.idpf.org/2007/opf";
const NS_XLINK: &str = "http://www.w3.org/1999/xlink";
const MAX_ARCHIVE_ENTRIES: usize = 2000;

/// Python posixpath.normpath.
fn posix_normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".to_string();
    }
    let mut initial_slashes = if path.starts_with('/') { 1 } else { 0 };
    if initial_slashes == 1 && path.starts_with("//") && !path.starts_with("///") {
        initial_slashes = 2;
    }
    let mut comps: Vec<&str> = Vec::new();
    for comp in path.split('/') {
        if comp.is_empty() || comp == "." {
            continue;
        }
        if comp != ".."
            || (initial_slashes == 0 && comps.is_empty())
            || (!comps.is_empty() && comps.last() == Some(&".."))
        {
            comps.push(comp);
        } else if !comps.is_empty() {
            comps.pop();
        }
    }
    let result = "/".repeat(initial_slashes) + &comps.join("/");
    if result.is_empty() {
        ".".to_string()
    } else {
        result
    }
}

/// Python posixpath.dirname.
fn posix_dirname(p: &str) -> String {
    let i = match p.rfind('/') {
        Some(i) => i + 1,
        None => return String::new(),
    };
    let head = &p[..i];
    if head.bytes().all(|c| c == b'/') {
        head.to_string() // all slashes — kept verbatim ("//x" → "//")
    } else {
        head.trim_end_matches('/').to_string()
    }
}

/// urllib.parse.unquote (UTF-8, errors="replace"; '+' stays literal).
fn unquote(s: &str) -> String {
    let b = s.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let (Some(h), Some(l)) = (
                (b[i + 1] as char).to_digit(16),
                (b[i + 2] as char).to_digit(16),
            ) {
                out.push((h * 16 + l) as u8);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// urllib.parse.urlsplit(href).path — minimal faithful port.
fn urlsplit_path(href: &str) -> &str {
    let s = href.split('#').next().unwrap_or("");
    let s = s.split('?').next().unwrap_or("");
    let bytes = s.as_bytes();
    let mut scheme_end: Option<usize> = None;
    if !bytes.is_empty() && bytes[0].is_ascii_alphabetic() {
        let mut i = 0;
        while i < bytes.len()
            && (bytes[i].is_ascii_alphanumeric() || matches!(bytes[i], b'+' | b'-' | b'.'))
        {
            i += 1;
        }
        if i < bytes.len() && bytes[i] == b':' {
            scheme_end = Some(i + 1);
        }
    }
    let rest = match scheme_end {
        Some(e) => &s[e..],
        None => s,
    };
    if rest.starts_with("//") {
        match rest[2..].find('/') {
            Some(off) => &rest[2 + off..],
            None => "",
        }
    } else {
        rest
    }
}

/// Port of archive._epub_resolve.
fn epub_resolve(base_dir: &str, href: &str) -> String {
    let rel = unquote(urlsplit_path(href));
    if rel.is_empty() {
        return String::new();
    }
    let joined = if !base_dir.is_empty() {
        if rel.starts_with('/') {
            rel.clone()
        } else if base_dir.ends_with('/') {
            format!("{}{}", base_dir, rel)
        } else {
            format!("{}/{}", base_dir, rel)
        }
    } else {
        rel
    };
    posix_normpath(&joined)
}

/// Port of `_epub_spine_images` + `_epub_entries`'s raw-name collection.
/// Returns (raw_infolist_names, spine_resolved_names); spine may be empty
/// (any spine-level failure → Python falls back to natural order of raw).
pub fn epub_raw(path: &Path) -> Option<(Vec<String>, Vec<String>)> {
    let mut f = File::open(path).ok()?;
    let entries = match read_cd(&mut f) {
        Ok(Some(e)) => e,
        _ => return None,
    };
    epub_raw_with(&mut f, &entries)
}

/// `epub_raw` on an already-open file whose central directory is already
/// parsed — the memoized path's single-open form (no second fd, and no
/// window where the file could change between the two opens).
fn epub_raw_with(f: &mut File, entries: &[ZipEntry]) -> Option<(Vec<String>, Vec<String>)> {
    let raw: Vec<String> = entries.iter().map(|e| e.filename.clone()).collect();

    // ElementTree tolerates <!DOCTYPE ...>; roxmltree's default does not.
    // allow_dtd matches ET.fromstring's acceptance for well-formed docs.
    fn parse_xml(b: &[u8]) -> Option<roxmltree::Document<'_>> {
        const OPTS: roxmltree::ParsingOptions = roxmltree::ParsingOptions {
            allow_dtd: true,
            nodes_limit: u32::MAX,
        };
        std::str::from_utf8(b)
            .ok()
            .and_then(|s| roxmltree::Document::parse_with_options(s, OPTS).ok())
    }

    let spine = (|| -> Option<Vec<String>> {
        let read_entry = |f: &mut File, name: &str| -> Option<Vec<u8>> {
            match zip_read_with(f, entries, name) {
                ReadOutcome::Data(b) => Some(b),
                _ => None,
            }
        };
        let container_bytes = read_entry(f, "META-INF/container.xml")?;
        let doc = parse_xml(&container_bytes)?;
        let root_el = doc.root_element();
        // container.find(".//{ns}rootfile") — descendants only, doc order
        let rootfile = doc
            .root()
            .descendants()
            .filter(|n| n.is_element() && n.id() != root_el.id())
            .find(|n| {
                n.tag_name().name() == "rootfile"
                    && n.tag_name().namespace() == Some(NS_CONTAINER)
            })?;
        let full_path = rootfile.attribute("full-path")?;
        if full_path.is_empty() {
            return None;
        }
        let opf_path = posix_normpath(full_path);
        let opf_dir = posix_dirname(&opf_path);
        let opf_bytes = read_entry(f, &opf_path)?;
        let opf = parse_xml(&opf_bytes)?;

        // manifest: opf.iter("{ns}item") — includes the root element
        let mut manifest: std::collections::HashMap<String, String> =
            std::collections::HashMap::new();
        for n in opf.root().descendants() {
            if n.is_element()
                && n.tag_name().name() == "item"
                && n.tag_name().namespace() == Some(NS_OPF)
            {
                if let (Some(id), Some(href)) = (n.attribute("id"), n.attribute("href")) {
                    manifest.insert(id.to_string(), href.to_string());
                }
            }
        }
        let mut out: Vec<String> = Vec::new();
        let itemrefs: Vec<_> = opf
            .root()
            .descendants()
            .filter(|n| {
                n.is_element()
                    && n.tag_name().name() == "itemref"
                    && n.tag_name().namespace() == Some(NS_OPF)
            })
            .collect();
        for r in itemrefs {
            if out.len() >= MAX_ARCHIVE_ENTRIES {
                break;
            }
            let idref = r.attribute("idref").unwrap_or("");
            let href = match manifest.get(idref) {
                Some(h) => h.clone(),
                None => continue,
            };
            let doc_path = epub_resolve(&opf_dir, &href);
            let page_bytes = match read_entry(f, &doc_path) {
                Some(b) => b,
                None => continue,
            };
            let page = match parse_xml(&page_bytes) {
                Some(d) => d,
                None => continue,
            };
            let doc_dir = posix_dirname(&doc_path);
            for el in page.root().descendants() {
                if !el.is_element() {
                    continue;
                }
                let src: Option<String> = match el.tag_name().name() {
                    "img" => el.attribute("src").map(|s| s.to_string()),
                    "image" => {
                        let xlink = el
                            .attributes()
                            .find(|a| a.name() == "href" && a.namespace() == Some(NS_XLINK));
                        match xlink {
                            Some(a) => Some(a.value().to_string()),
                            None => el.attribute("href").map(|s| s.to_string()),
                        }
                    }
                    _ => None,
                };
                if let Some(s) = src {
                    if !s.is_empty() {
                        out.push(epub_resolve(&doc_dir, &s));
                    }
                }
            }
        }
        Some(out)
    })();
    Some((raw, spine.unwrap_or_default()))
}

// ══════════════════════════════════════════════════════════════════════════
// RAR / CBR — port of rarfile RAR3+RAR5 listing (names + is_dir only)
// ══════════════════════════════════════════════════════════════════════════

const RAR_ID: &[u8] = b"Rar!\x1a\x07\x00";
const RAR5_ID: &[u8] = b"Rar!\x1a\x07\x01\x00";
const RAR_SIG_PREFIX: &[u8] = b"Rar!\x1a\x07";
const SFX_MAX_SIZE: usize = 2 * 1024 * 1024;

const RAR_BLOCK_MARK: u8 = 0x72;
const RAR_BLOCK_MAIN: u8 = 0x73;
const RAR_BLOCK_FILE: u8 = 0x74;
const RAR_BLOCK_OLD_EXTRA: u8 = 0x76;
const RAR_BLOCK_OLD_SUB: u8 = 0x77;
const RAR_BLOCK_OLD_AUTH: u8 = 0x79;
const RAR_BLOCK_SUB: u8 = 0x7a;
const RAR_BLOCK_ENDARC: u8 = 0x7b;

const RAR_LONG_BLOCK: u16 = 0x8000;
const RAR_MAIN_COMMENT: u16 = 0x0002;
const RAR_MAIN_SOLID: u16 = 0x0008;
const RAR_MAIN_NEWNUMBERING: u16 = 0x0010;
const RAR_MAIN_PASSWORD: u16 = 0x0080;
const RAR_MAIN_FIRSTVOLUME: u16 = 0x0100;
const RAR_MAIN_ENCRYPTVER: u16 = 0x0200;

const RAR_FILE_SPLIT_BEFORE: u16 = 0x0001;
const RAR_FILE_SPLIT_AFTER: u16 = 0x0002;
const RAR_FILE_PASSWORD: u16 = 0x0004;
const RAR_FILE_COMMENT: u16 = 0x0008;
const RAR_FILE_SOLID: u16 = 0x0010;
const RAR_FILE_DIRECTORY: u16 = 0x00e0;
const RAR_FILE_LARGE: u16 = 0x0100;
const RAR_FILE_UNICODE: u16 = 0x0200;
const RAR_FILE_SALT: u16 = 0x0400;
const RAR_FILE_VERSION: u16 = 0x0800;
const RAR_FILE_EXTTIME: u16 = 0x1000;

const RAR_ENDARC_NEXT_VOLUME: u16 = 0x0001;
const RAR_ENDARC_DATACRC: u16 = 0x0002;
const RAR_ENDARC_VOLNR: u16 = 0x0008;

const RAR_OS_UNIX: u8 = 3;

const RAR5_BLOCK_MAIN: u64 = 1;
const RAR5_BLOCK_FILE: u64 = 2;
const RAR5_BLOCK_SERVICE: u64 = 3;
const RAR5_BLOCK_ENCRYPTION: u64 = 4;
const RAR5_BLOCK_ENDARC: u64 = 5;

const RAR5_BF_EXTRA_DATA: u64 = 0x01;
const RAR5_BF_DATA_AREA: u64 = 0x02;
const RAR5_BF_SPLIT_BEFORE: u64 = 0x08;
const RAR5_BF_SPLIT_AFTER: u64 = 0x10;

const RAR5_MAIN_HAS_VOLNR: u64 = 0x02;
const RAR5_MAIN_SOLID: u64 = 0x04;

const RAR5_FF_ISDIR: u64 = 0x01;
const RAR5_FF_HAS_MTIME: u64 = 0x02;
const RAR5_FF_HAS_CRC32: u64 = 0x04;

const RAR5_COMPR_SOLID: u64 = 0x40;
const RAR5_ENDARC_NEXT_VOL: u64 = 0x01;

const RAR5_XFILE_ENCRYPTION: u64 = 1;
const RAR5_XFILE_HASH: u64 = 2;
const RAR5_XFILE_TIME: u64 = 3;
const RAR5_XFILE_VERSION: u64 = 4;
const RAR5_XFILE_REDIR: u64 = 5;
const RAR5_XFILE_OWNER: u64 = 6;

const RAR5_XTIME_UNIXTIME: u64 = 0x01;
const RAR5_XTIME_HAS_MTIME: u64 = 0x02;
const RAR5_XTIME_HAS_CTIME: u64 = 0x04;
const RAR5_XTIME_HAS_ATIME: u64 = 0x08;
const RAR5_XTIME_UNIXTIME_NS: u64 = 0x10;

const RAR5_XENC_CHECKVAL: u64 = 0x01;
const RAR5_XENC_CIPHER_AES256: u64 = 0;
const RAR5_ENC_FLAG_HAS_CHECKVAL: u64 = 0x01;
const RAR5_XOWNER_UNAME: u64 = 0x01;
const RAR5_XOWNER_GNAME: u64 = 0x02;
const RAR5_XOWNER_UID: u64 = 0x04;
const RAR5_XOWNER_GID: u64 = 0x08;

const RAR_M0: u8 = 0x30;
const RAR_MAX_COMMENT: u64 = 256 * 1024;
const RAR5_MAX_HDR: u64 = 2 * 1024 * 1024;

/// Parse failure channel.
///   `Partial` — like rarfile `_set_error`/EOF: stop looping, keep names so far.
///   `Fatal`   — like a raised exception reaching `_rar_names`: result is [].
enum RarErr {
    Partial,
    Fatal,
}
type RResult<T> = Result<T, RarErr>;

/// Seekable byte source mirroring XFile/B2KFile semantics: a 64KB sliding
/// window plus a *lazy* seek — `seek` only moves the logical position, and
/// the next `read` reuses the window when the target is still inside it.
/// That is what makes header walks over compressed data areas cheap.
struct Fd {
    f: File,
    pos: u64,        // logical position
    buf: Vec<u8>,    // window contents
    buf_start: u64,  // file offset of buf[0]
    buf_avail: usize, // valid bytes in buf
}

// rarfile opens the parse fd with bufsize=1024: header reads are tiny, so a
// small window minimises cold-IO readahead when headers sit far apart.
// Ours is slightly larger to cover dense header clusters in fewer fills —
// and our seek is lazy, so we still pay strictly fewer syscalls than the
// Python BufferedReader(1024) does.
const FD_WIN: usize = 1024;

impl Fd {
    fn open(path: &Path) -> Result<Fd, RarErr> {
        File::open(path)
            .map(|f| Fd { f, pos: 0, buf: Vec::with_capacity(FD_WIN), buf_start: 0, buf_avail: 0 })
            .map_err(|_| RarErr::Partial)
    }
    fn tell(&self) -> u64 {
        self.pos
    }
    /// lazy: logical position only — no I/O.
    fn seek(&mut self, pos: u64) -> Result<(), RarErr> {
        self.pos = pos;
        Ok(())
    }
    /// (re)fill the window starting at self.pos; returns bytes read.
    /// buf stays allocated at FD_WIN; buf_avail tracks the valid prefix.
    fn fill(&mut self) -> Result<usize, RarErr> {
        self.f
            .seek(SeekFrom::Start(self.pos))
            .map_err(|_| RarErr::Partial)?;
        if self.buf.len() != FD_WIN {
            self.buf.resize(FD_WIN, 0);
        }
        let mut got = 0usize;
        while got < FD_WIN {
            match self.f.read(&mut self.buf[got..]) {
                Ok(0) => break,
                Ok(k) => got += k,
                Err(_) => return Err(RarErr::Partial),
            }
        }
        self.buf_start = self.pos;
        self.buf_avail = got;
        Ok(got)
    }
    /// fd.read(n) — may return fewer at EOF (never more than n).
    fn read(&mut self, n: u64) -> Result<Vec<u8>, RarErr> {
        let mut out = Vec::with_capacity(n.min(1 << 20) as usize);
        while (out.len() as u64) < n {
            let wend = self.buf_start + self.buf_avail as u64;
            if self.pos >= self.buf_start && self.pos < wend {
                let idx = (self.pos - self.buf_start) as usize;
                let take = ((wend - self.pos) as usize).min(n as usize - out.len());
                out.extend_from_slice(&self.buf[idx..idx + take]);
                self.pos += take as u64;
            } else if self.fill()? == 0 {
                break; // EOF
            }
        }
        Ok(out)
    }
}

/// rarfile.UnicodeFilename — RAR3 compressed-UTF16 name decompression.
struct UnicodeFilename<'a> {
    std_name: &'a [u8],
    encdata: &'a [u8],
    pos: usize,
    encpos: usize,
    buf: Vec<u8>,
    failed: bool,
}

impl<'a> UnicodeFilename<'a> {
    fn new(name: &'a [u8], encdata: &'a [u8]) -> Self {
        UnicodeFilename { std_name: name, encdata, pos: 0, encpos: 0, buf: Vec::new(), failed: false }
    }
    fn enc_byte(&mut self) -> u8 {
        if self.encpos < self.encdata.len() {
            let c = self.encdata[self.encpos];
            self.encpos += 1;
            c
        } else {
            self.failed = true;
            0
        }
    }
    fn std_byte(&mut self) -> u8 {
        if self.pos < self.std_name.len() {
            self.std_name[self.pos]
        } else {
            self.failed = true;
            b'?'
        }
    }
    fn put(&mut self, lo: u8, hi: u8) {
        self.buf.push(lo);
        self.buf.push(hi);
        self.pos += 1;
    }
    fn decode(&mut self) -> String {
        let hi = self.enc_byte();
        let mut flagbits = 0u32;
        let mut flags = 0u8;
        while self.encpos < self.encdata.len() {
            if flagbits == 0 {
                flags = self.enc_byte();
                flagbits = 8;
            }
            flagbits -= 2;
            let t = (flags >> flagbits) & 3;
            match t {
                0 => {
                    let lo = self.enc_byte();
                    self.put(lo, 0);
                }
                1 => {
                    let lo = self.enc_byte();
                    self.put(lo, hi);
                }
                2 => {
                    let lo = self.enc_byte();
                    let h = self.enc_byte();
                    self.put(lo, h);
                }
                _ => {
                    let n = self.enc_byte();
                    if n & 0x80 != 0 {
                        let c = self.enc_byte();
                        for _ in 0..((n & 0x7f) + 2) {
                            let lo = self.std_byte().wrapping_add(c);
                            self.put(lo, hi);
                        }
                    } else {
                        for _ in 0..(n + 2) {
                            let lo = self.std_byte();
                            self.put(lo, 0);
                        }
                    }
                }
            }
        }
        String::from_utf16_lossy(
            &self
                .buf
                .chunks_exact(2)
                .map(|c| u16::from_le_bytes([c[0], c[1]]))
                .collect::<Vec<u16>>(),
        )
    }
}

/// SFX scan — port of _find_sfx_header. Returns (ver, offset); ver 0 = none.
fn find_sfx(path: &Path) -> Result<(u8, u64), ()> {
    let mut f = File::open(path).map_err(|_| ())?;
    let mut buf: Vec<u8> = Vec::new();
    for step in [64usize, SFX_MAX_SIZE] {
        let mut chunk = vec![0u8; step];
        let mut got = 0usize;
        while got < step {
            match f.read(&mut chunk[got..]) {
                Ok(0) => break,
                Ok(k) => got += k,
                Err(_) => break,
            }
        }
        if got == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..got]);
        let mut findpos = 0usize;
        while findpos + RAR_SIG_PREFIX.len() <= buf.len() {
            let rel = buf[findpos..]
                .windows(RAR_SIG_PREFIX.len())
                .position(|w| w == RAR_SIG_PREFIX);
            let pos = match rel {
                Some(p) => findpos + p,
                None => break,
            };
            if buf.len() >= pos + RAR_ID.len() && &buf[pos..pos + RAR_ID.len()] == RAR_ID {
                return Ok((3, pos as u64));
            }
            if buf.len() >= pos + RAR5_ID.len() && &buf[pos..pos + RAR5_ID.len()] == RAR5_ID {
                return Ok((5, pos as u64));
            }
            findpos = pos + RAR_SIG_PREFIX.len();
        }
    }
    Ok((0, 0))
}

/// genericpath._splitext (posix): leading dots in basename don't count.
fn splitext(p: &str) -> (&str, &str) {
    let sep_idx = p.rfind('/');
    let dot_idx = p.rfind('.');
    if let Some(d) = dot_idx {
        if Some(d) > sep_idx.or(Some(0)).map(|x| x) || (dot_idx.is_some() && sep_idx.is_none()) {
            // careful: replicate `dotIndex > sepIndex` (rfind returns -1 → None)
            let sep = sep_idx.map(|i| i as isize).unwrap_or(-1);
            if (d as isize) > sep {
                // skip leading dots of the basename
                let mut fi = (sep + 1) as usize;
                while fi < d {
                    if p.as_bytes()[fi] != b'.' {
                        return (&p[..d], &p[d..]);
                    }
                    fi += 1;
                }
            }
        }
    }
    (p, "")
}

/// rarfile `_inc_volname` — character-level digit carry (+ char inc old-style).
fn inc_volname(volfile: &str, mut i: isize, inc_chars: bool) -> String {
    let mut f: Vec<char> = volfile.chars().collect();
    while i >= 0 {
        let idx = i as usize;
        if f[idx] == '9' {
            f[idx] = '0';
            i -= 1;
            if i < 0 {
                f.insert(0, '1');
            }
        } else if ('0'..='8').contains(&f[idx]) || inc_chars {
            f[idx] = std::char::from_u32(f[idx] as u32 + 1).unwrap_or(f[idx]);
            break;
        } else {
            f.insert(idx + 1, '1');
            break;
        }
    }
    f.into_iter().collect()
}

/// rarfile `_next_newvol` — BadRarName propagates (→ Fatal → []).
fn next_newvol(volfile: &str) -> Result<String, RarErr> {
    let (name, ext) = splitext(volfile);
    let el = ext.to_ascii_lowercase();
    let vf = if el.is_empty() || el == ".exe" || el == ".sfx" {
        format!("{}.rar", name)
    } else {
        volfile.to_string()
    };
    let chars: Vec<char> = vf.chars().collect();
    let mut i = chars.len() as isize - 1;
    while i >= 0 {
        let c = chars[i as usize];
        if c.is_ascii_digit() {
            return Ok(inc_volname(&vf, i, false));
        }
        if c == '/' {
            break;
        }
        i -= 1;
    }
    Err(RarErr::Fatal)
}

/// rarfile `_next_oldvol`.
fn next_oldvol(volfile: &str) -> String {
    let (stem, ext) = splitext(volfile);
    let mut ext = ext.to_string();
    let el = ext.to_ascii_lowercase();
    if el.is_empty() || el == ".exe" || el == ".sfx" {
        ext = ".rar".to_string();
    }
    let echars: Vec<char> = ext.chars().collect();
    let sfx: String = echars.iter().skip(2.min(echars.len())).collect();
    if !sfx.is_empty() && sfx.chars().all(|c| c.is_ascii_digit()) {
        ext = inc_volname(&ext, (echars.len() - 1) as isize, true);
    } else {
        let head: String = echars.iter().take(2.min(echars.len())).collect();
        ext = format!("{}00", head);
    }
    format!("{}{}", stem, ext)
}

// ── RAR3 ─────────────────────────────────────────────────────────────────

#[derive(Default)]
struct R3Header {
    btype: u8,
    flags: u16,
    add_size: u64,
    data_offset: u64,
    filename: String,
    is_dir: bool,
    host_os: u8,
    mode: u32,
    compress_size: u64,
    file_size: u64,
    compress_type: u8,
    endarc_volnr: Option<u16>,
}

/// `RAR3Parser._parse_file_header` — fields we need for listing.
/// Bounds failures → Partial (struct.error → _set_error → stop, keep names).
fn r3_parse_file_header(h: &mut R3Header, hdata: &[u8], mut pos: usize) -> RResult<usize> {
    let need = |hdata: &[u8], pos: usize, n: usize| -> RResult<()> {
        if pos + n > hdata.len() {
            Err(RarErr::Partial)
        } else {
            Ok(())
        }
    };
    need(hdata, pos, 25)?;
    let fld = &hdata[pos..pos + 25];
    h.compress_size = u32::from_le_bytes(fld[0..4].try_into().unwrap()) as u64;
    h.file_size = u32::from_le_bytes(fld[4..8].try_into().unwrap()) as u64;
    h.host_os = fld[8];
    // fld[9..13] crc, fld[13..17] dostime — to_datetime sanitises, never raises
    h.compress_type = fld[18];
    let name_size = u16::from_le_bytes(fld[19..21].try_into().unwrap()) as usize;
    h.mode = u32::from_le_bytes(fld[21..25].try_into().unwrap());
    pos += 25;
    if h.flags & RAR_FILE_LARGE != 0 {
        let (h1, p) = load_le32(hdata, pos).ok_or(RarErr::Partial)?;
        pos = p;
        let (h2, p) = load_le32(hdata, pos).ok_or(RarErr::Partial)?;
        pos = p;
        h.compress_size |= (h1 as u64) << 32;
        h.file_size |= (h2 as u64) << 32;
        h.add_size = h.compress_size;
    }
    need(hdata, pos, name_size)?;
    let name = &hdata[pos..pos + name_size];
    pos += name_size;

    let mut filename: String;
    if h.flags & RAR_FILE_UNICODE != 0 && name.contains(&0) {
        let nul = name.iter().position(|&c| c == 0).unwrap();
        let orig = &name[..nul];
        let mut u = UnicodeFilename::new(orig, &name[nul + 1..]);
        filename = u.decode();
        if u.failed {
            filename = decode_rar3_fallback(orig);
        }
    } else if h.flags & RAR_FILE_UNICODE != 0 {
        filename = decode_utf8_replace(name);
    } else {
        let nul = name.iter().position(|&c| c == 0).unwrap_or(name.len());
        filename = decode_rar3_fallback(&name[..nul]);
    }
    filename = filename.replace('\\', "/");
    filename = filename.trim_end_matches('/').to_string();
    // is_dir: type==FILE && !is_symlink && DIRECTORY flag
    h.is_dir = h.btype == RAR_BLOCK_FILE
        && !(h.host_os == RAR_OS_UNIX && h.mode & 0xF000 == 0xA000)
        && (h.flags & RAR_FILE_DIRECTORY) == RAR_FILE_DIRECTORY;
    if h.is_dir {
        filename.push('/');
    }
    h.filename = filename;
    if h.flags & RAR_FILE_SALT != 0 {
        need(hdata, pos, 8)?;
        pos += 8;
    }
    if h.flags & RAR_FILE_EXTTIME != 0 {
        pos = r3_parse_ext_time(hdata, pos)?;
    }
    Ok(pos)
}

/// `_parse_ext_time`/`_parse_xtime` — position arithmetic only (crc_pos).
/// to_datetime/to_nsdatetime sanitise; the only failure channel is a short
/// read (struct.error → Partial).
fn r3_parse_ext_time(hdata: &[u8], mut pos: usize) -> RResult<usize> {
    let mut flags: u16 = 0;
    if pos + 2 <= hdata.len() {
        flags = u16::from_le_bytes(hdata[pos..pos + 2].try_into().unwrap());
        pos += 2;
    }
    for shift in [12u32, 8, 4, 0] {
        let flag = (flags >> shift) & 0xF;
        if flag & 8 != 0 {
            // mtime slot reuses h.mtime (always set); the other three need a
            // base dostime first.
            if shift != 12 {
                let (_, p) = load_le32(hdata, pos).ok_or(RarErr::Partial)?;
                pos = p;
            }
            let cnt = flag & 3;
            for _ in 0..cnt {
                let (_, p) = load_byte(hdata, pos).ok_or(RarErr::Partial)?;
                pos = p;
            }
        }
    }
    Ok(pos)
}

/// `_parse_subblocks` — only walks record boundaries (comment payloads don't
/// affect listing). struct.error → Partial.
fn r3_parse_subblocks(hdata: &[u8], mut pos: usize) -> RResult<usize> {
    while pos < hdata.len() {
        if pos + 7 > hdata.len() {
            return Err(RarErr::Partial);
        }
        let slen = u16::from_le_bytes(hdata[pos + 5..pos + 7].try_into().unwrap()) as usize;
        let pos_next = pos + slen;
        pos += 7;
        if pos_next < pos {
            break;
        }
        pos = pos_next;
    }
    Ok(pos)
}

/// `RAR3Parser._parse_block_header`. Ok(None) → eof; Err(Partial) → stop too.
fn r3_parse_block_header(fd: &mut Fd) -> RResult<Option<R3Header>> {
    let buf = fd.read(7).map_err(|_| RarErr::Partial)?;
    if buf.is_empty() {
        return Ok(None);
    }
    if buf.len() < 7 {
        return Err(RarErr::Partial);
    }
    let header_crc = u16::from_le_bytes(buf[0..2].try_into().unwrap());
    let btype = buf[2];
    let flags = u16::from_le_bytes(buf[3..5].try_into().unwrap());
    let header_size = u16::from_le_bytes(buf[5..7].try_into().unwrap()) as usize;

    let mut hdata = buf;
    if header_size > 7 {
        let rest = fd.read((header_size - 7) as u64).map_err(|_| RarErr::Partial)?;
        hdata.extend_from_slice(&rest);
    }
    let data_offset = fd.tell();
    if hdata.len() != header_size {
        return Err(RarErr::Partial);
    }
    let mut pos = 7usize;
    let mut h = R3Header {
        btype,
        flags,
        data_offset,
        ..Default::default()
    };
    if flags & RAR_LONG_BLOCK != 0 {
        let (v, p) = load_le32(&hdata, pos).ok_or(RarErr::Partial)?;
        pos = p;
        h.add_size = v as u64;
    }
    let crc_pos: usize;
    match btype {
        RAR_BLOCK_MARK => {
            return Ok(Some(h));
        }
        RAR_BLOCK_MAIN => {
            pos += 6;
            if flags & RAR_MAIN_ENCRYPTVER != 0 {
                pos += 1;
            }
            crc_pos = pos;
            if flags & RAR_MAIN_COMMENT != 0 {
                r3_parse_subblocks(&hdata, pos)?;
            }
        }
        RAR_BLOCK_FILE => {
            pos = r3_parse_file_header(&mut h, &hdata, pos - 4)?;
            crc_pos = pos;
            if flags & RAR_FILE_COMMENT != 0 {
                r3_parse_subblocks(&hdata, pos)?;
            }
        }
        RAR_BLOCK_SUB => {
            r3_parse_file_header(&mut h, &hdata, pos - 4)?;
            crc_pos = header_size;
        }
        RAR_BLOCK_OLD_AUTH => {
            pos += 8;
            crc_pos = pos;
        }
        RAR_BLOCK_OLD_EXTRA => {
            pos += 7;
            crc_pos = pos;
        }
        RAR_BLOCK_OLD_SUB => {
            if pos + 3 > hdata.len() {
                return Err(RarErr::Partial);
            }
            let old_sub_type = u16::from_le_bytes(hdata[pos..pos + 2].try_into().unwrap());
            if old_sub_type == 0x101 || old_sub_type == 0x102 {
                return Ok(Some(h)); // skip CRC check
            }
            crc_pos = header_size;
        }
        RAR_BLOCK_ENDARC => {
            if flags & RAR_ENDARC_DATACRC != 0 {
                let (_, p) = load_le32(&hdata, pos).ok_or(RarErr::Partial)?;
                pos = p;
            }
            if flags & RAR_ENDARC_VOLNR != 0 {
                let (v, _) = load_le16(&hdata, pos).ok_or(RarErr::Partial)?;
                h.endarc_volnr = Some(v);
            }
            crc_pos = header_size;
        }
        _ => {
            crc_pos = header_size;
        }
    }
    let crcdat = &hdata[2..crc_pos.min(hdata.len())];
    let calc = (crc32fast::hash(crcdat) & 0xFFFF) as u16;
    if header_crc == calc {
        Ok(Some(h))
    } else {
        Err(RarErr::Partial)
    }
}

// ── RAR5 ─────────────────────────────────────────────────────────────────

#[derive(Clone, Default)]
struct R5Header {
    block_type: u64,
    block_flags: u64,
    flags: u16,
    add_size: u64,
    data_offset: u64,
    filename: String,
    is_dir: bool,
    file_version: bool,
    compress_type: u8,
    compress_size: u64,
    file_size: u64,
    file_encrypted: bool,
    file_redir: Option<u64>,
    block_extra_size: u64,
    main_has_volnr: bool,
    main_volnr: u64,
    endarc_next_vol: bool,
}

fn r5_load_time(hdata: &[u8], pos: usize, unix: bool) -> RResult<usize> {
    if unix {
        let (v, p) = load_le32(hdata, pos).ok_or(RarErr::Fatal)?;
        if !unixtime_ok(v as i64) {
            return Err(RarErr::Fatal);
        }
        Ok(p)
    } else {
        let (lo, p) = load_le32(hdata, pos).ok_or(RarErr::Fatal)?;
        let (hi, p2) = load_le32(hdata, p).ok_or(RarErr::Fatal)?;
        let val = ((hi as u64) << 32) | lo as u64;
        let secs = (val / 10_000_000) as i64 - 11644473600;
        if !unixtime_ok(secs) {
            return Err(RarErr::Fatal);
        }
        Ok(p2)
    }
}

fn r5_process_extra(h: &mut R5Header, xdata: &[u8]) -> RResult<()> {
    let (xtype, mut pos) = load_vint(xdata, 0).ok_or(RarErr::Fatal)?;
    match xtype {
        RAR5_XFILE_TIME => {
            let (tflags, mut p) = load_vint(xdata, pos).ok_or(RarErr::Fatal)?;
            let unix = tflags & RAR5_XTIME_UNIXTIME != 0;
            if tflags & RAR5_XTIME_HAS_MTIME != 0 {
                p = r5_load_time(xdata, p, unix)?;
            }
            if tflags & RAR5_XTIME_HAS_CTIME != 0 {
                p = r5_load_time(xdata, p, unix)?;
            }
            if tflags & RAR5_XTIME_HAS_ATIME != 0 {
                p = r5_load_time(xdata, p, unix)?;
            }
            if tflags & RAR5_XTIME_UNIXTIME_NS != 0 {
                if tflags & RAR5_XTIME_HAS_MTIME != 0 {
                    let (_, q) = load_le32(xdata, p).ok_or(RarErr::Fatal)?;
                    p = q;
                }
                if tflags & RAR5_XTIME_HAS_CTIME != 0 {
                    let (_, q) = load_le32(xdata, p).ok_or(RarErr::Fatal)?;
                    p = q;
                }
                if tflags & RAR5_XTIME_HAS_ATIME != 0 {
                    let (_, q) = load_le32(xdata, p).ok_or(RarErr::Fatal)?;
                    p = q;
                }
            }
            let _ = p;
            pos = xdata.len(); // consumed
            let _ = pos;
        }
        RAR5_XFILE_ENCRYPTION => {
            let (_algo, p) = load_vint(xdata, pos).ok_or(RarErr::Fatal)?;
            pos = p;
            let (eflags, p) = load_vint(xdata, pos).ok_or(RarErr::Fatal)?;
            pos = p;
            let (_kdf, p) = load_byte(xdata, pos).ok_or(RarErr::Fatal)?;
            pos = p;
            let (_, p) = load_bytes(xdata, 16, pos).ok_or(RarErr::Fatal)?;
            pos = p;
            let (_, p) = load_bytes(xdata, 16, pos).ok_or(RarErr::Fatal)?;
            pos = p;
            if eflags & RAR5_XENC_CHECKVAL != 0 {
                let (_, _) = load_bytes(xdata, 12, pos).ok_or(RarErr::Fatal)?;
            }
            h.file_encrypted = true;
            h.flags |= RAR_FILE_PASSWORD;
        }
        RAR5_XFILE_HASH => {
            let (hash_type, mut p) = load_vint(xdata, pos).ok_or(RarErr::Fatal)?;
            if hash_type == 0 {
                let (_, q) = load_bytes(xdata, 32, p).ok_or(RarErr::Fatal)?;
                p = q;
            }
            let _ = p;
        }
        RAR5_XFILE_VERSION => {
            let (_, p) = load_vint(xdata, pos).ok_or(RarErr::Fatal)?;
            let (_, _) = load_vint(xdata, p).ok_or(RarErr::Fatal)?;
            h.file_version = true;
        }
        RAR5_XFILE_REDIR => {
            let (rtype, p) = load_vint(xdata, pos).ok_or(RarErr::Fatal)?;
            pos = p;
            let (_rflags, p) = load_vint(xdata, pos).ok_or(RarErr::Fatal)?;
            pos = p;
            let (_name, _) = load_vstr(xdata, pos).ok_or(RarErr::Fatal)?;
            h.file_redir = Some(rtype);
        }
        RAR5_XFILE_OWNER => {
            let (oflags, mut p) = load_vint(xdata, pos).ok_or(RarErr::Fatal)?;
            if oflags & RAR5_XOWNER_UNAME != 0 {
                let (_, q) = load_vstr(xdata, p).ok_or(RarErr::Fatal)?;
                p = q;
            }
            if oflags & RAR5_XOWNER_GNAME != 0 {
                let (_, q) = load_vstr(xdata, p).ok_or(RarErr::Fatal)?;
                p = q;
            }
            if oflags & RAR5_XOWNER_UID != 0 {
                let (_, q) = load_vint(xdata, p).ok_or(RarErr::Fatal)?;
                p = q;
            }
            if oflags & RAR5_XOWNER_GID != 0 {
                let (_, _) = load_vint(xdata, p).ok_or(RarErr::Fatal)?;
            }
        }
        _ => {}
    }
    Ok(())
}

/// RAR5 `_parse_file_block` (file + service share it).
fn r5_parse_file_block(h: &mut R5Header, hdata: &[u8], mut pos: usize) -> RResult<()> {
    let (file_flags, p) = load_vint(hdata, pos).ok_or(RarErr::Fatal)?;
    pos = p;
    let (fsz, p) = load_vint(hdata, pos).ok_or(RarErr::Fatal)?;
    pos = p;
    h.file_size = fsz;
    let (_mode, p) = load_vint(hdata, pos).ok_or(RarErr::Fatal)?;
    pos = p;
    if file_flags & RAR5_FF_HAS_MTIME != 0 {
        pos = r5_load_time(hdata, pos, true)?;
    }
    if file_flags & RAR5_FF_HAS_CRC32 != 0 {
        let (_, p) = load_le32(hdata, pos).ok_or(RarErr::Fatal)?;
        pos = p;
    }
    let (compress_flags, p) = load_vint(hdata, pos).ok_or(RarErr::Fatal)?;
    pos = p;
    let (_host_os, p) = load_vint(hdata, pos).ok_or(RarErr::Fatal)?;
    pos = p;
    let (name, p) = load_vstr(hdata, pos).ok_or(RarErr::Fatal)?;
    pos = p;
    let nul = name.iter().position(|&c| c == 0).unwrap_or(name.len());
    let mut filename = decode_utf8_replace(&name[..nul]);
    filename = filename.trim_end_matches('/').to_string();
    h.compress_type = RAR_M0 + (((compress_flags >> 7) & 7) as u8);

    if h.block_extra_size > 0 {
        while pos < hdata.len().saturating_sub(1) {
            let (xsize, p) = load_vint(hdata, pos).ok_or(RarErr::Fatal)?;
            pos = p;
            let (xdata, p) = load_bytes(hdata, xsize as usize, pos).ok_or(RarErr::Fatal)?;
            pos = p;
            r5_process_extra(h, xdata)?;
        }
    }
    if h.block_flags & RAR5_BF_SPLIT_BEFORE != 0 {
        h.flags |= RAR_FILE_SPLIT_BEFORE;
    }
    if h.block_flags & RAR5_BF_SPLIT_AFTER != 0 {
        h.flags |= RAR_FILE_SPLIT_AFTER;
    }
    let is_dir = file_flags & RAR5_FF_ISDIR != 0 && h.file_redir.is_none();
    if is_dir {
        h.flags |= RAR_FILE_DIRECTORY;
        filename.push('/');
    }
    if compress_flags & RAR5_COMPR_SOLID != 0 {
        h.flags |= RAR_FILE_SOLID;
    }
    h.is_dir = is_dir;
    h.filename = filename;
    Ok(())
}

/// `RAR5Parser._parse_block_header`.
/// `enc` mirrors `_hdrenc_main`: once set, subsequent headers → None → break.
fn r5_parse_block_header(fd: &mut Fd, enc: &mut bool) -> RResult<Option<R5Header>> {
    if *enc {
        return Ok(None);
    }
    let mut start = fd.read(5).map_err(|_| RarErr::Partial)?;
    if start.len() < 5 {
        return Err(RarErr::Partial); // EOF → _set_error → stop
    }
    while *start.last().unwrap() & 0x80 != 0 {
        let b = fd.read(1).map_err(|_| RarErr::Partial)?;
        if b.is_empty() {
            return Err(RarErr::Partial);
        }
        start.push(b[0]);
    }
    let mut pos = 4usize;
    let (hdrlen, p) = load_vint(&start, pos).ok_or(RarErr::Partial)?;
    pos = p;
    if hdrlen > RAR5_MAX_HDR {
        return Err(RarErr::Partial);
    }
    let header_size = pos + hdrlen as usize;
    let mut hdata = start;
    if header_size > hdata.len() {
        let rest = fd
            .read((header_size - hdata.len()) as u64)
            .map_err(|_| RarErr::Partial)?;
        hdata.extend_from_slice(&rest);
    }
    let data_offset = fd.tell();
    if hdata.len() != header_size {
        return Err(RarErr::Partial);
    }
    let header_crc = u32::from_le_bytes(hdata[0..4].try_into().unwrap());
    if header_crc != crc32fast::hash(&hdata[4..]) {
        return Err(RarErr::Partial); // CRC error → _set_error → eof
    }
    // _parse_block_common: crc(4) hdrlen(vint) type(vint) flags(vint) [extra] [data]
    pos = 4;
    let (_hdrlen2, p) = load_vint(&hdata, pos).ok_or(RarErr::Partial)?;
    pos = p;
    let (block_type, p) = load_vint(&hdata, pos).ok_or(RarErr::Partial)?;
    pos = p;
    if !matches!(
        block_type,
        RAR5_BLOCK_MAIN | RAR5_BLOCK_FILE | RAR5_BLOCK_SERVICE | RAR5_BLOCK_ENCRYPTION | RAR5_BLOCK_ENDARC
    ) {
        return Ok(None); // unknown type → h=None → stop
    }
    let mut h = R5Header {
        block_type,
        data_offset,
        ..Default::default()
    };
    let (bflags, p) = load_vint(&hdata, pos).ok_or(RarErr::Fatal)?;
    pos = p;
    h.block_flags = bflags;
    if bflags & RAR5_BF_EXTRA_DATA != 0 {
        let (v, p) = load_vint(&hdata, pos).ok_or(RarErr::Fatal)?;
        pos = p;
        h.block_extra_size = v;
    }
    if bflags & RAR5_BF_DATA_AREA != 0 {
        let (v, p) = load_vint(&hdata, pos).ok_or(RarErr::Fatal)?;
        pos = p;
        h.add_size = v;
        h.compress_size = v;
        h.flags |= RAR_LONG_BLOCK;
    }
    match block_type {
        RAR5_BLOCK_MAIN => {
            let (mflags, mut p) = load_vint(&hdata, pos).ok_or(RarErr::Fatal)?;
            if mflags & RAR5_MAIN_HAS_VOLNR != 0 {
                let (v, q) = load_vint(&hdata, p).ok_or(RarErr::Fatal)?;
                p = q;
                h.main_has_volnr = true;
                h.main_volnr = v;
            }
            let _ = p;
            h.flags |= RAR_MAIN_NEWNUMBERING;
            if mflags & RAR5_MAIN_SOLID != 0 {
                h.flags |= RAR_MAIN_SOLID;
            }
            if *enc {
                h.flags |= RAR_MAIN_PASSWORD;
            }
            if mflags & RAR5_MAIN_HAS_VOLNR == 0 {
                h.flags |= RAR_MAIN_FIRSTVOLUME;
            }
        }
        RAR5_BLOCK_FILE | RAR5_BLOCK_SERVICE => {
            r5_parse_file_block(&mut h, &hdata, pos)?;
        }
        RAR5_BLOCK_ENCRYPTION => {
            let (algo, mut p) = load_vint(&hdata, pos).ok_or(RarErr::Fatal)?;
            let (eflags, q) = load_vint(&hdata, p).ok_or(RarErr::Fatal)?;
            p = q;
            let (_kdf, q) = load_byte(&hdata, p).ok_or(RarErr::Fatal)?;
            p = q;
            let (_, q) = load_bytes(&hdata, 16, p).ok_or(RarErr::Fatal)?;
            p = q;
            if eflags & RAR5_ENC_FLAG_HAS_CHECKVAL != 0 {
                let (_, q) = load_bytes(&hdata, 12, p).ok_or(RarErr::Fatal)?;
                p = q;
            }
            let _ = p;
            if algo != RAR5_XENC_CIPHER_AES256 {
                return Err(RarErr::Fatal); // BadRarFile → []
            }
            *enc = true; // _hdrenc_main → subsequent headers → None → break
        }
        RAR5_BLOCK_ENDARC => {
            let (eflags, _p) = load_vint(&hdata, pos).ok_or(RarErr::Fatal)?;
            if eflags & RAR5_ENDARC_NEXT_VOL != 0 {
                h.endarc_next_vol = true;
            }
        }
        _ => unreachable!(),
    }
    Ok(Some(h))
}

// ══════════════════════════════════════════════════════════════════════════
// Shared driver — mirrors CommonParser._parse_real for names-only listing
// ══════════════════════════════════════════════════════════════════════════

struct Entry {
    filename: String,
    is_dir: bool,
}

fn rar3_names(path: &Path, sfx_offset: u64) -> RResult<Vec<Entry>> {
    let mut fd = Fd::open(path)?;
    fd.seek(sfx_offset)?;
    let sig = fd.read(7)?;
    if sig.as_slice() != RAR_ID {
        return Err(RarErr::Fatal); // NotRarFile → []
    }
    let mut entries: Vec<Entry> = Vec::new();
    let mut volume = 0u32;
    let mut more_vols = false;
    let mut endarc = false;
    let mut volfile = path.to_path_buf();
    let mut main_flags: Option<u16> = None;
    let mut raise_need_first_vol = false;

    loop {
        let h: Option<R3Header> = if endarc {
            None
        } else if main_flags.map(|f| f & RAR_MAIN_PASSWORD != 0).unwrap_or(false) {
            None // encrypted headers, no password → _parse_header returns None
        } else {
            match r3_parse_block_header(&mut fd) {
                Ok(v) => v,
                Err(RarErr::Partial) => None,
                Err(RarErr::Fatal) => return Err(RarErr::Fatal),
            }
        };
        let h = match h {
            Some(h) => h,
            None => {
                if raise_need_first_vol {
                    return Err(RarErr::Fatal); // NeedFirstVolume → []
                }
                if more_vols {
                    volume += 1;
                    let next = match main_flags {
                        // _main unset → AttributeError in rarfile → []
                        None => return Err(RarErr::Fatal),
                        Some(f) if f & RAR_MAIN_NEWNUMBERING != 0 => {
                            next_newvol(volfile.to_str().ok_or(RarErr::Fatal)?)?
                        }
                        _ => next_oldvol(volfile.to_str().ok_or(RarErr::Fatal)?),
                    };
                    let np = PathBuf::from(&next);
                    match Fd::open(&np) {
                        Ok(mut nfd) => {
                            let s = nfd.read(7)?;
                            if s.as_slice() != RAR_ID {
                                break; // invalid volume sig → _set_error → stop
                            }
                            fd = nfd;
                            volfile = np;
                            more_vols = false;
                            endarc = false;
                            main_flags = None;
                            continue;
                        }
                        Err(_) => break, // cannot open next volume → stop
                    }
                }
                break;
            }
        };
        match h.btype {
            RAR_BLOCK_MAIN if main_flags.is_none() => {
                main_flags = Some(h.flags);
                if volume == 0
                    && (h.flags & RAR_MAIN_NEWNUMBERING) != 0
                    && (h.flags & RAR_MAIN_FIRSTVOLUME) == 0
                {
                    raise_need_first_vol = true; // delayed until ENDARC volnr
                }
                if h.flags & RAR_MAIN_PASSWORD != 0 {
                    break; // _needs_password, no password → stop
                }
            }
            RAR_BLOCK_ENDARC => {
                if h.flags & RAR_ENDARC_NEXT_VOLUME != 0 {
                    more_vols = true;
                }
                endarc = true;
                if raise_need_first_vol && h.flags & RAR_ENDARC_VOLNR != 0 {
                    return Err(RarErr::Fatal); // NeedFirstVolume → []
                }
            }
            RAR_BLOCK_FILE => {
                if h.flags & RAR_FILE_SPLIT_AFTER != 0 {
                    more_vols = true;
                }
                if volume == 0 && h.flags & RAR_FILE_SPLIT_BEFORE != 0 {
                    raise_need_first_vol = true;
                }
            }
            _ => {}
        }
        // process_entry: FILE blocks only; SPLIT_BEFORE merges into previous
        if h.btype == RAR_BLOCK_FILE
            && h.flags & RAR_FILE_VERSION == 0
            && h.flags & RAR_FILE_SPLIT_BEFORE == 0
        {
            entries.push(Entry { filename: h.filename.clone(), is_dir: h.is_dir });
        }
        // SUB "CMT" comment decode is skipped — it cannot affect the listing.
        if h.add_size > 0 {
            fd.seek(h.data_offset + h.add_size)?;
        }
    }
    Ok(entries)
}

fn rar5_names(path: &Path, sfx_offset: u64) -> RResult<Vec<Entry>> {
    let mut fd = Fd::open(path)?;
    fd.seek(sfx_offset)?;
    let sig = fd.read(8)?;
    if sig.as_slice() != RAR5_ID {
        return Err(RarErr::Fatal);
    }
    let mut entries: Vec<Entry> = Vec::new();
    let mut volume = 0u32;
    let mut more_vols = false;
    let mut endarc = false;
    let mut volfile = path.to_path_buf();
    let mut main_seen = false;
    let mut enc = false;
    let mut raise_need_first_vol = false;

    loop {
        let h: Option<R5Header> = if endarc {
            None
        } else {
            match r5_parse_block_header(&mut fd, &mut enc) {
                Ok(v) => v,
                Err(RarErr::Partial) => None,
                Err(RarErr::Fatal) => return Err(RarErr::Fatal),
            }
        };
        let h = match h {
            Some(h) => h,
            None => {
                if raise_need_first_vol {
                    return Err(RarErr::Fatal);
                }
                if more_vols {
                    volume += 1;
                    if !main_seen {
                        return Err(RarErr::Fatal); // _main None → AttributeError
                    }
                    let next = next_newvol(volfile.to_str().ok_or(RarErr::Fatal)?)?;
                    let np = PathBuf::from(&next);
                    match Fd::open(&np) {
                        Ok(mut nfd) => {
                            let s = nfd.read(8)?;
                            if s.as_slice() != RAR5_ID {
                                break;
                            }
                            fd = nfd;
                            volfile = np;
                            more_vols = false;
                            endarc = false;
                            main_seen = false;
                            enc = false;
                            continue;
                        }
                        Err(_) => break,
                    }
                }
                break;
            }
        };
        match h.block_type {
            RAR5_BLOCK_MAIN if !main_seen => {
                main_seen = true;
                if volume == 0 && (h.flags & RAR_MAIN_FIRSTVOLUME) == 0 {
                    if h.main_has_volnr {
                        return Err(RarErr::Fatal); // NeedFirstVolume
                    }
                    raise_need_first_vol = true;
                }
                if h.flags & RAR_MAIN_PASSWORD != 0 {
                    break; // _needs_password, no password → stop
                }
            }
            RAR5_BLOCK_ENDARC => {
                if h.endarc_next_vol {
                    more_vols = true;
                }
                endarc = true;
            }
            RAR5_BLOCK_FILE => {
                if h.block_flags & RAR5_BF_SPLIT_AFTER != 0 {
                    more_vols = true;
                }
                if volume == 0 && h.block_flags & RAR5_BF_SPLIT_BEFORE != 0 {
                    raise_need_first_vol = true;
                }
            }
            _ => {}
        }
        // process_entry
        match h.block_type {
            RAR5_BLOCK_FILE => {
                if !h.file_version && h.block_flags & RAR5_BF_SPLIT_BEFORE == 0 {
                    entries.push(Entry { filename: h.filename.clone(), is_dir: h.is_dir });
                }
            }
            RAR5_BLOCK_SERVICE if h.filename == "CMT" => {
                // _load_comment guards that can raise → replicate fatal paths
                if h.block_flags & (RAR5_BF_SPLIT_BEFORE | RAR5_BF_SPLIT_AFTER) == 0
                    && h.compress_type == RAR_M0
                    && h.compress_size <= RAR_MAX_COMMENT
                    && h.file_size <= RAR_MAX_COMMENT
                {
                    if h.file_encrypted {
                        return Err(RarErr::Fatal); // pwd=None → AttributeError
                    }
                    let data = fd.read(h.file_size)?;
                    if data.len() as u64 != h.file_size {
                        return Err(RarErr::Fatal);
                    }
                    let end = data.iter().position(|&c| c == 0).unwrap_or(data.len());
                    if std::str::from_utf8(&data[..end]).is_err() {
                        return Err(RarErr::Fatal); // strict utf-8 decode
                    }
                }
            }
            _ => {}
        }
        if h.add_size > 0 {
            fd.seek(h.data_offset + h.add_size)?;
        }
    }
    Ok(entries)
}

/// `rar_names(path)` → (filename, is_dir) pairs in header order.
/// None → Python fallback (rarfile decides).
pub fn rar_names(path: &Path) -> Option<Vec<(String, bool)>> {
    let (ver, ofs) = match find_sfx(path) {
        Ok(v) => v,
        Err(_) => return None,
    };
    let r = match ver {
        3 => rar3_names(path, ofs),
        5 => rar5_names(path, ofs),
        _ => return Some(Vec::new()), // NotRarFile → []
    };
    match r {
        Ok(entries) => Some(entries.into_iter().map(|e| (e.filename, e.is_dir)).collect()),
        Err(RarErr::Fatal) => Some(Vec::new()), // raised → rarfile → []
        Err(RarErr::Partial) => None,           // fd-level failure → Python decides
    }
}

/// `_read_rar`'s extraction half, in-process: the member is selected by
/// ORDINAL in the ported listing, not by name — the dll's own name decode
/// (ACTW_OEM under the C locale for RAR3, plus RAR5 sanitizing) produces
/// different strings than `decode_rar3_fallback`, so name comparison
/// 404s every non-ASCII member. rarfile's `_info_map` overwrite gives
/// LAST-duplicate-wins, hence `rposition`.
pub fn rar_read_entry(path: &Path, inner: &str) -> Option<Vec<u8>> {
    let names = rar_names(path)?;
    let ord = names.iter().rposition(|(n, d)| n == inner && !*d)?;
    rar_read_ordinal(path, ord, inner)
}

/// Sequential RAR processing until file-header index `ord`, via rarlab
/// unrar — the same engine rarfile shells out to, without one 7z
/// subprocess per page. For solid archives entry *n* still decompresses
/// 0..n (a format constraint rarfile+7z pays identically). `inner` is
/// passed only for the ASCII misalignment guard below. Open/seek/read
/// errors, encrypted members, and short bodies (RAR5 -oi file-reference
/// entries read back empty in test mode) all yield `None` → 404; the
/// `ArchiveToolMissing` (501) case cannot occur in-process.
fn rar_read_ordinal(path: &Path, ord: usize, inner: &str) -> Option<Vec<u8>> {
    let mut ar = unrar::Archive::new(path).open_for_processing().ok()?;
    let mut idx = 0usize;
    while let Some(header) = ar.read_header().ok()? {
        if idx == ord {
            let e = header.entry();
            // Misalignment guard: unrar never mangles ASCII names, so a
            // mismatch means the two header walks diverged (version-marked
            // entries are the known cause) — refuse rather than serve the
            // wrong page. Non-ASCII names can't be checked this way.
            if inner.is_ascii() && e.filename.as_os_str().to_str() != Some(inner) {
                return None;
            }
            if e.is_directory() || e.is_encrypted() {
                return None;
            }
            let want = e.unpacked_size;
            let (data, _rest) = header.read().ok()?;
            // A short body (file-reference entry, truncated extraction)
            // must never go out as a 200 — a poisoned CacheFirst entry in
            // the service worker would survive otherwise.
            return if data.len() as u64 == want { Some(data) } else { None };
        }
        idx += 1;
        // A corrupt member earlier in a SOLID archive makes the dll's
        // test-mode RAR_SKIP fail (CRC is verified); the typestate API
        // consumes the handle, so the walk cannot resume — later members
        // of a broken solid stream are undecodable anyway, so the 404
        // here matches what 7z would return.
        ar = header.skip().ok()?;
    }
    None
}

// ══════════════════════════════════════════════════════════════════════════
// Open-archive memo — port of app/archive.py's _open_archives LRU (#217)
// ══════════════════════════════════════════════════════════════════════════
//
// Serving /image/<archive>/<entry> (and the thumbs/view hit paths) used to
// re-open the archive — re-parsing the ZIP central directory / every RAR
// header block / the EPUB OPF — on EVERY page request, O(E) per read. A
// reading session issues one request per page against the same work, so
// parsed archive state is memoized in a small LRU keyed by
// (resolved path, st_mtime_ns, st_size): a modified archive is a different
// key and is re-parsed, while its stale entry ages out and the File closes
// on drop.
//
// Python serializes reads on each cached object because ZipFile/RarFile
// share one file cursor; the Rust equivalent keeps Mutex<File> for the
// same reason — a read must own the seek cursor end-to-end or concurrent
// readers interleave seeks. Open/parse happens OUTSIDE the LRU lock so a
// slow header parse never serializes unrelated archive traffic, and a
// concurrent insert race keeps the first object (the loser's File closes
// unused — Python closes it explicitly; Drop does it here).
//
// Open failures are not cached: the next request retries, matching
// Python's "return the exception to the caller" behaviour — callers see
// `None` and fall back to the uncached one-shot functions, exactly like
// Python's `entry is None → _read_zip_uncached`.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::UNIX_EPOCH;

const OPEN_ARCHIVE_CACHE_MAX: usize = 8;

type ArchiveKey = (String, u64, u64); // (resolved path, mtime_ns, size)

enum Parsed {
    /// .zip/.cbz/.epub — parsed central directory + shared file handle.
    Zip {
        file: Mutex<File>,
        entries: Vec<ZipEntry>,
        /// py `NameToInfo` — last duplicate wins; makes entry lookup
        /// `getinfo()`-O(1) instead of `namelist()`-O(E).
        name_idx: HashMap<String, usize>,
        /// .epub only — (raw image names, spine order) from `epub_raw`.
        epub: Option<(Vec<String>, Vec<String>)>,
    },
    /// .rar/.cbr — parsed header names. Entry bytes go through
    /// `rar_read_entry` (in-process unrar, sequential per read); the memo
    /// still saves the O(E) header walk the listing was paying per request.
    Rar { names: Vec<(String, bool)> },
}

/// One cached open archive. All fields are immutable after construction;
/// reads go through the per-format accessors below.
pub struct OpenArchive {
    parsed: Parsed,
}

impl OpenArchive {
    /// ZIP-family entry bytes via the memoized CD + shared file.
    /// `Some(bytes)` only on a proven-identical read — anything else is
    /// `None`, matching `zip_read_entry`'s fallback contract.
    pub fn read_zip_entry(&self, inner: &str) -> Option<Vec<u8>> {
        let Parsed::Zip {
            file,
            entries,
            name_idx,
            ..
        } = &self.parsed
        else {
            return None;
        };
        let i = *name_idx.get(inner)?;
        let mut f = file.lock().ok()?;
        match zip_read_data(&mut f, &entries[i]) {
            ReadOutcome::Data(b) => Some(b),
            _ => None,
        }
    }

    /// ZIP central-directory names in stored order (`zip_names` shape).
    pub fn zip_names(&self) -> Option<Vec<String>> {
        match &self.parsed {
            Parsed::Zip { entries, .. } => {
                Some(entries.iter().map(|e| e.filename.clone()).collect())
            }
            _ => None,
        }
    }

    /// Format-dispatched entry bytes — `read_archive_entry`'s shape.
    /// `Some(bytes)` only on a proven-identical read; anything else is
    /// `None` and the caller produces the same 404 Python would.
    pub fn read_entry(&self, path: &Path, inner: &str) -> Option<Vec<u8>> {
        match &self.parsed {
            Parsed::Zip { .. } => self.read_zip_entry(inner),
            // RAR: ordinal in the memoized listing (last duplicate wins,
            // matching rarfile's _info_map) — the dll's name decode can't
            // be compared to ours, and a missing/directory member never
            // pays the extraction walk (Python's `not in namelist → None`).
            Parsed::Rar { names } => {
                let ord = names.iter().rposition(|(n, d)| n == inner && !*d)?;
                rar_read_ordinal(path, ord, inner)
            }
        }
    }

    /// EPUB (raw names, spine order) — `epub_raw` shape; `None` for non-epub.
    pub fn epub_raw(&self) -> Option<(Vec<String>, Vec<String>)> {
        match &self.parsed {
            Parsed::Zip { epub: Some(e), .. } => Some(e.clone()),
            _ => None,
        }
    }

    /// RAR (name, is_dir) pairs — `rar_names` shape; `None` for non-rar.
    pub fn rar_names(&self) -> Option<Vec<(String, bool)>> {
        match &self.parsed {
            Parsed::Rar { names } => Some(names.clone()),
            _ => None,
        }
    }
}

/// Global LRU: resolved-path+mtime+size → parsed archive.
static OPEN_ARCHIVES: LazyLock<
    Mutex<(HashMap<ArchiveKey, Arc<OpenArchive>>, VecDeque<ArchiveKey>)>,
> = LazyLock::new(|| Mutex::new((HashMap::new(), VecDeque::new())));

/// `_archive_cache_key`: resolved path + (mtime, size) — an edited file is
/// a different key. `None` when the path can't be stat'ed (callers then do
/// a direct one-shot open, exactly like Python).
fn archive_cache_key(path: &Path) -> Option<ArchiveKey> {
    let st = path.metadata().ok()?;
    let mtime_ns = st
        .modified()
        .ok()?
        .duration_since(UNIX_EPOCH)
        .ok()?
        .as_nanos() as u64;
    let resolved = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
    Some((resolved.to_string_lossy().into_owned(), mtime_ns, st.len()))
}

/// Lowercased ".ext" for format dispatch (serve.rs::suffix_lower twin —
/// bookq-core can't reach into viewer-rs).
fn archive_suffix(path: &Path) -> String {
    path.extension()
        .and_then(|e| e.to_str())
        .map(|e| format!(".{}", e.to_lowercase()))
        .unwrap_or_default()
}

/// Parse *path*'s headers by format — `None` when any step fails (the
/// failure is not cached; the next request retries).
fn parse_archive(path: &Path) -> Option<Parsed> {
    let suffix = archive_suffix(path);
    match suffix.as_str() {
        ".rar" | ".cbr" => Some(Parsed::Rar {
            names: rar_names(path)?,
        }),
        _ => {
            let mut f = File::open(path).ok()?;
            let entries = match read_cd(&mut f) {
                Ok(Some(e)) => e,
                _ => return None,
            };
            // .epub derives its spine on the same open fd — no second open
            // and no window where the file could change mid-parse.
            let epub = if suffix == ".epub" {
                epub_raw_with(&mut f, &entries)
            } else {
                None
            };
            Some(Parsed::Zip {
                file: Mutex::new(f),
                name_idx: name_index(&entries),
                entries,
                epub,
            })
        }
    }
}

/// `NameToInfo`: last duplicate filename wins (mirrors `zip_read_with`'s
/// `.rev().find()` order).
fn name_index(entries: &[ZipEntry]) -> HashMap<String, usize> {
    let mut m = HashMap::with_capacity(entries.len());
    for (i, e) in entries.iter().enumerate() {
        m.insert(e.filename.clone(), i);
    }
    m
}

/// Return the memoized parsed archive for *path*, parsing on miss.
/// `None` = no cache key (unstat'able) or parse failure — callers fall
/// back to the uncached one-shot functions, same as Python.
pub fn open_archive(path: &Path) -> Option<Arc<OpenArchive>> {
    let key = archive_cache_key(path)?;
    {
        let (map, order) = &mut *OPEN_ARCHIVES.lock().ok()?;
        if let Some(hit) = map.get(&key) {
            let hit = hit.clone();
            if let Some(pos) = order.iter().position(|k| *k == key) {
                let k = order.remove(pos).unwrap();
                order.push_back(k);
            }
            return Some(hit);
        }
    }
    // Parse outside the LRU lock: header walks are the expensive part and
    // must not serialize unrelated archives.
    let parsed = parse_archive(path)?;
    let entry = Arc::new(OpenArchive { parsed });
    let (map, order) = &mut *OPEN_ARCHIVES.lock().ok()?;
    if let Some(existing) = map.get(&key) {
        // A racing thread won — keep its object; our File closes on drop.
        return Some(existing.clone());
    }
    order.push_back(key.clone());
    map.insert(key, entry.clone());
    while map.len() > OPEN_ARCHIVE_CACHE_MAX {
        let Some(old) = order.pop_front() else { break };
        map.remove(&old);
    }
    Some(entry)
}

/// Drop every cached archive (test isolation).
#[cfg(test)]
pub fn clear_open_archive_cache() {
    if let Ok(mut guard) = OPEN_ARCHIVES.lock() {
        let (map, order) = &mut *guard;
        map.clear();
        order.clear();
    }
}

#[cfg(test)]
mod memo_tests {
    use super::*;
    use std::time::SystemTime;

    fn le16(v: u16) -> [u8; 2] {
        v.to_le_bytes()
    }
    fn le32(v: u32) -> [u8; 4] {
        v.to_le_bytes()
    }

    /// Minimal stored-method ZIP writer — local headers + CD + EOCD.
    fn make_zip(path: &Path, members: &[(&str, &[u8])]) {
        let mut local: Vec<u8> = Vec::new();
        let mut cd: Vec<u8> = Vec::new();
        for (name, data) in members {
            let crc = crc32fast::hash(data);
            let off = local.len() as u32;
            local.extend_from_slice(&le32(0x0403_4b50));
            local.extend_from_slice(&le16(20)); // version needed
            local.extend_from_slice(&le16(0)); // flags
            local.extend_from_slice(&le16(0)); // stored
            local.extend_from_slice(&le16(0)); // mtime
            local.extend_from_slice(&le16(0)); // mdate
            local.extend_from_slice(&le32(crc));
            local.extend_from_slice(&le32(data.len() as u32));
            local.extend_from_slice(&le32(data.len() as u32));
            local.extend_from_slice(&le16(name.len() as u16));
            local.extend_from_slice(&le16(0)); // extra
            local.extend_from_slice(name.as_bytes());
            local.extend_from_slice(data);

            cd.extend_from_slice(&le32(0x0201_4b50));
            cd.extend_from_slice(&le16(20)); // made by
            cd.extend_from_slice(&le16(20)); // needed
            cd.extend_from_slice(&le16(0)); // flags
            cd.extend_from_slice(&le16(0)); // stored
            cd.extend_from_slice(&le16(0));
            cd.extend_from_slice(&le16(0));
            cd.extend_from_slice(&le32(crc));
            cd.extend_from_slice(&le32(data.len() as u32));
            cd.extend_from_slice(&le32(data.len() as u32));
            cd.extend_from_slice(&le16(name.len() as u16));
            cd.extend_from_slice(&le16(0)); // extra
            cd.extend_from_slice(&le16(0)); // comment
            cd.extend_from_slice(&le16(0)); // disk
            cd.extend_from_slice(&le16(0)); // int attr
            cd.extend_from_slice(&le32(0)); // ext attr
            cd.extend_from_slice(&le32(off));
            cd.extend_from_slice(name.as_bytes());
        }
        let mut out = local;
        let cd_off = out.len() as u32;
        let cd_len = cd.len() as u32;
        out.extend_from_slice(&cd);
        out.extend_from_slice(&le32(0x0605_4b50));
        out.extend_from_slice(&le16(0));
        out.extend_from_slice(&le16(0));
        out.extend_from_slice(&le16(members.len() as u16));
        out.extend_from_slice(&le16(members.len() as u16));
        out.extend_from_slice(&le32(cd_len));
        out.extend_from_slice(&le32(cd_off));
        out.extend_from_slice(&le16(0));
        std::fs::write(path, out).unwrap();
    }

    fn tmp_cbz(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "bookq-arch-{tag}-{}-{}.cbz",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    /// Minimal RAR5 *store* writer — port of tests/fixtures/rar5.py:
    /// signature + main header + per-entry (file header, raw data) + end
    /// header. Only what a RAR5 parser needs to list/extract stored files.
    fn make_rar5_store(path: &Path, members: &[(&str, &[u8])]) {
        fn vint(mut n: u64, out: &mut Vec<u8>) {
            loop {
                let b = (n & 0x7f) as u8;
                n >>= 7;
                if n > 0 {
                    out.push(b | 0x80);
                } else {
                    out.push(b);
                    return;
                }
            }
        }
        fn header(body: &[u8]) -> Vec<u8> {
            let mut size = Vec::new();
            vint(body.len() as u64, &mut size);
            let mut v = size.clone();
            v.extend_from_slice(body);
            let mut out = crc32fast::hash(&v).to_le_bytes().to_vec();
            out.extend_from_slice(&v);
            out
        }
        let mut out = b"Rar!\x1a\x07\x01\x00".to_vec();
        let mut main = Vec::new();
        vint(1, &mut main);
        vint(0, &mut main);
        vint(0, &mut main);
        out.extend(header(&main));
        for (name, data) in members {
            let nb = name.as_bytes();
            let mut body = Vec::new();
            vint(2, &mut body); // file header
            vint(0x0002, &mut body); // data area present
            vint(data.len() as u64, &mut body);
            vint(0x0004, &mut body); // CRC32 present
            vint(data.len() as u64, &mut body); // unpacked size
            vint(0x20, &mut body); // attributes
            body.extend_from_slice(&crc32fast::hash(data).to_le_bytes());
            vint(0, &mut body); // compression: version 0, method store
            vint(1, &mut body); // host OS: unix
            vint(nb.len() as u64, &mut body);
            body.extend_from_slice(nb);
            out.extend(header(&body));
            out.extend_from_slice(data);
        }
        let mut end = Vec::new();
        vint(5, &mut end);
        vint(0, &mut end);
        vint(0, &mut end);
        out.extend(header(&end));
        std::fs::write(path, out).unwrap();
    }

    /// `make_rar5_store` plus an is_dir flag (file_flags |= RAR5_FF_ISDIR).
    /// Directory entries must appear in BOTH listings for the ordinal
    /// contract to hold — this is what the alignment test asserts.
    fn make_rar5_store_ext(path: &Path, members: &[(&str, &[u8], bool)]) {
        fn vint(mut n: u64, out: &mut Vec<u8>) {
            loop {
                let b = (n & 0x7f) as u8;
                n >>= 7;
                if n > 0 {
                    out.push(b | 0x80);
                } else {
                    out.push(b);
                    return;
                }
            }
        }
        fn header(body: &[u8]) -> Vec<u8> {
            let mut size = Vec::new();
            vint(body.len() as u64, &mut size);
            let mut v = size.clone();
            v.extend_from_slice(body);
            let mut out = crc32fast::hash(&v).to_le_bytes().to_vec();
            out.extend_from_slice(&v);
            out
        }
        let mut out = b"Rar!\x1a\x07\x01\x00".to_vec();
        let mut main = Vec::new();
        vint(1, &mut main);
        vint(0, &mut main);
        vint(0, &mut main);
        out.extend(header(&main));
        for (name, data, is_dir) in members {
            let nb = name.as_bytes();
            let mut body = Vec::new();
            vint(2, &mut body);
            vint(0x0002, &mut body);
            vint(data.len() as u64, &mut body);
            vint(0x0004 | (*is_dir as u64), &mut body); // CRC32 | ISDIR
            vint(data.len() as u64, &mut body);
            vint(0x20, &mut body);
            body.extend_from_slice(&crc32fast::hash(data).to_le_bytes());
            vint(0, &mut body);
            vint(1, &mut body);
            vint(nb.len() as u64, &mut body);
            body.extend_from_slice(nb);
            out.extend(header(&body));
            out.extend_from_slice(data);
        }
        let mut end = Vec::new();
        vint(5, &mut end);
        vint(0, &mut end);
        vint(0, &mut end);
        out.extend(header(&end));
        std::fs::write(path, out).unwrap();
    }

    /// Minimal RAR3 *store* writer — signature + main + per-entry (file
    /// header, raw data) + endarc. Names are raw bytes so cp932/UTF-8
    /// encodings can be built explicitly; `flags` lets callers set
    /// LHD_DIRECTORY (0x00e0) etc. HEAD_CRC = crc32 over header[2..].
    fn make_rar3_store(path: &Path, members: &[(Vec<u8>, &[u8], u16)]) {
        fn hdr(btype: u8, flags: u16, body: &[u8]) -> Vec<u8> {
            let mut h = Vec::with_capacity(body.len() + 7);
            h.push(btype);
            h.extend_from_slice(&flags.to_le_bytes());
            h.extend_from_slice(&(body.len() as u16 + 7).to_le_bytes());
            h.extend_from_slice(body);
            let crc = (crc32fast::hash(&h) & 0xFFFF) as u16;
            let mut out = crc.to_le_bytes().to_vec();
            out.extend_from_slice(&h);
            out
        }
        let mut out = b"Rar!\x1a\x07\x00".to_vec();
        out.extend(hdr(0x73, 0, &[0; 6])); // MAIN
        for (name, data, flags) in members {
            let mut b = Vec::new();
            b.extend_from_slice(&(data.len() as u32).to_le_bytes()); // pack
            b.extend_from_slice(&(data.len() as u32).to_le_bytes()); // unp
            b.push(3);                                              // unix
            b.extend_from_slice(&crc32fast::hash(data).to_le_bytes());
            b.extend_from_slice(&0u32.to_le_bytes()); // ftime
            b.push(20);                              // unp_ver 2.0
            b.push(0x30);                            // method: store
            b.extend_from_slice(&(name.len() as u16).to_le_bytes());
            b.extend_from_slice(&0u32.to_le_bytes()); // attr
            b.extend_from_slice(name);
            out.extend(hdr(0x74, *flags | 0x8000, &b)); // LONG_BLOCK
            out.extend_from_slice(data);
        }
        out.extend(hdr(0x7b, 0, &[])); // ENDARC
        std::fs::write(path, out).unwrap();
    }

    fn tmp_rar(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "bookq-arch-{tag}-{}-{}.rar",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    /// The LRU is process-global and cargo runs tests in parallel — stateful
    /// assertions serialize here so another test's opens can't evict the
    /// entry under test mid-assertion. Poison is ignored so one test's panic
    /// doesn't cascade into the rest.
    static SERIAL: Mutex<()> = Mutex::new(());

    fn serial() -> std::sync::MutexGuard<'static, ()> {
        SERIAL.lock().unwrap_or_else(|e| e.into_inner())
    }

    #[test]
    fn memo_returns_same_object_and_reads() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_cbz("same");
        make_zip(&p, &[("001.png", b"one"), ("002.png", b"two")]);

        let a = open_archive(&p).unwrap();
        let b = open_archive(&p).unwrap();
        assert!(Arc::ptr_eq(&a, &b), "repeat open must reuse the memo");
        assert_eq!(a.read_zip_entry("001.png").unwrap(), b"one");
        assert_eq!(a.read_zip_entry("002.png").unwrap(), b"two");
        assert_eq!(a.read_zip_entry("999.png"), None);
        assert_eq!(a.zip_names().unwrap(), vec!["001.png", "002.png"]);
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn memo_rekeys_on_content_change() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_cbz("rekey");
        make_zip(&p, &[("001.png", b"first")]);
        let a = open_archive(&p).unwrap();
        assert_eq!(a.read_zip_entry("001.png").unwrap(), b"first");

        // Rewrite same size; force a distinct mtime so the key must change
        // even on coarse-timestamp filesystems.
        make_zip(&p, &[("001.png", b"secnd")]);
        File::options()
            .write(true)
            .open(&p)
            .unwrap()
            .set_modified(SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000))
            .unwrap();

        let b = open_archive(&p).unwrap();
        assert!(!Arc::ptr_eq(&a, &b), "edited file is a different key");
        assert_eq!(b.read_zip_entry("001.png").unwrap(), b"secnd");
        // The old object never serves stale bytes: its fd sees the rewritten
        // inode, the read produces the new bytes, and the OLD entry's CRC
        // rejects them → None (same safe-side failure Python would give).
        assert_eq!(a.read_zip_entry("001.png"), None);
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn memo_lru_evicts_oldest() {
        let _g = serial();
        clear_open_archive_cache();
        let mut paths = Vec::new();
        for i in 0..(OPEN_ARCHIVE_CACHE_MAX + 2) {
            let p = tmp_cbz(&format!("lru{i}"));
            make_zip(&p, &[("001.png", format!("data{i}").as_bytes())]);
            paths.push(p);
        }
        let first = open_archive(&paths[0]).unwrap();
        for p in &paths[1..] {
            open_archive(p).unwrap();
        }
        // paths[0] was evicted — a fresh open re-parses into a new object.
        let again = open_archive(&paths[0]).unwrap();
        assert!(!Arc::ptr_eq(&first, &again), "oldest entry must be evicted");
        assert_eq!(again.read_zip_entry("001.png").unwrap(), b"data0".to_vec());
        // The most recent archives still hit the memo.
        let last_a = open_archive(paths.last().unwrap()).unwrap();
        let last_b = open_archive(paths.last().unwrap()).unwrap();
        assert!(Arc::ptr_eq(&last_a, &last_b));
        for p in &paths {
            std::fs::remove_file(p).ok();
        }
    }

    #[test]
    fn memo_duplicate_name_last_wins() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_cbz("dup");
        make_zip(
            &p,
            &[("a.png", b"old"), ("b.png", b"mid"), ("a.png", b"new")],
        );
        let a = open_archive(&p).unwrap();
        assert_eq!(
            a.read_zip_entry("a.png").unwrap(),
            b"new",
            "NameToInfo last-duplicate-wins must match zip_read_with"
        );
        assert_eq!(zip_read_entry(&p, "a.png").unwrap(), b"new");
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn memo_bad_archive_not_cached() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_cbz("bad");
        std::fs::write(&p, b"definitely not a zip file").unwrap();
        assert!(open_archive(&p).is_none());
        // A failed parse is never cached — a repaired file parses next time.
        make_zip(&p, &[("001.png", b"fixed")]);
        let a = open_archive(&p).unwrap();
        assert_eq!(a.read_zip_entry("001.png").unwrap(), b"fixed");
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn memo_concurrent_reads_are_correct() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_cbz("conc");
        let members: Vec<(String, Vec<u8>)> = (0..16)
            .map(|i| (format!("{i:03}.png"), format!("payload-{i}").into_bytes()))
            .collect();
        let refs: Vec<(&str, &[u8])> = members
            .iter()
            .map(|(n, d)| (n.as_str(), d.as_slice()))
            .collect();
        make_zip(&p, &refs);
        let a = open_archive(&p).unwrap();

        let mut handles = Vec::new();
        for t in 0..8 {
            let arc = a.clone();
            let expected: Vec<(String, Vec<u8>)> = (0..16)
                .map(|i| {
                    let idx = (i + t) % 16;
                    (
                        format!("{idx:03}.png"),
                        format!("payload-{idx}").into_bytes(),
                    )
                })
                .collect();
            handles.push(std::thread::spawn(move || {
                for (name, want) in expected {
                    assert_eq!(arc.read_zip_entry(&name).unwrap(), want);
                }
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn rar_store_entry_reads_and_missing_returns_none() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_rar("store");
        make_rar5_store(
            &p,
            &[
                ("pages/001.png", b"first"),
                ("pages/002.png", b"second"),
                ("notes.txt", b"not-an-image"),
            ],
        );
        let a = open_archive(&p).unwrap();
        // Deep-member read: sequential processing reaches it in-process.
        assert_eq!(a.read_entry(&p, "pages/002.png").unwrap(), b"second");
        assert_eq!(a.read_entry(&p, "pages/001.png").unwrap(), b"first");
        // Missing member → None (the memoized listing gate, like rarfile).
        assert_eq!(a.read_entry(&p, "pages/999.png"), None);
        // Uncached one-shot fallback also extracts.
        assert_eq!(rar_read_entry(&p, "pages/001.png").unwrap(), b"first");
        assert_eq!(rar_read_entry(&p, "pages/999.png"), None);
        std::fs::remove_file(&p).ok();
    }

    /// Ordinal, not name, selection — the case that broke name-matching:
    /// a RAR3 UTF-8 name without LHD_UNICODE decodes correctly in the
    /// ported listing ("café.png") but the dll's C-locale OEM decode
    /// mangles it, so `to_str() == inner` never held → 404 on every page.
    #[test]
    fn rar3_nonascii_name_reads_by_ordinal() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_rar("r3utf8");
        make_rar3_store(&p, &[
            ("café.png".as_bytes().to_vec(), b"cafe-bytes", 0),
            (b"plain.png".to_vec(), b"plain-bytes", 0),
        ]);
        let a = open_archive(&p).unwrap();
        assert_eq!(a.rar_names().unwrap()[0].0, "café.png");
        assert_eq!(a.read_entry(&p, "café.png").unwrap(), b"cafe-bytes");
        assert_eq!(a.read_entry(&p, "plain.png").unwrap(), b"plain-bytes");
        std::fs::remove_file(&p).ok();
    }

    /// rarfile's `_info_map` overwrites on duplicate names → LAST wins.
    /// (Name matching returned the first.) Same for the one-shot path.
    #[test]
    fn rar5_dup_names_last_wins() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_rar("dup");
        make_rar5_store(
            &p,
            &[("a.png", b"old"), ("b.png", b"middle"), ("a.png", b"new")],
        );
        let a = open_archive(&p).unwrap();
        assert_eq!(a.read_entry(&p, "a.png").unwrap(), b"new");
        assert_eq!(a.read_entry(&p, "b.png").unwrap(), b"middle");
        assert_eq!(rar_read_entry(&p, "a.png").unwrap(), b"new");
        std::fs::remove_file(&p).ok();
    }

    /// Directory entries must appear in BOTH header walks or ordinals
    /// shift — asserts unrar yields dir headers like the ported walker.
    #[test]
    fn rar5_dir_entries_keep_ordinal_aligned() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_rar("dirmix");
        make_rar5_store_ext(
            &p,
            &[
                ("sub/", b"", true),
                ("sub/001.png", b"img1", false),
                ("top.png", b"img2", false),
            ],
        );
        let a = open_archive(&p).unwrap();
        let names = a.rar_names().unwrap();
        assert_eq!(names.len(), 3);
        assert!(names[0].1); // dir
        assert_eq!(a.read_entry(&p, "sub/001.png").unwrap(), b"img1");
        assert_eq!(a.read_entry(&p, "top.png").unwrap(), b"img2");
        // A directory name itself must never extract.
        assert_eq!(a.read_entry(&p, "sub/"), None);
        std::fs::remove_file(&p).ok();
    }

    /// Encrypted-member and misalignment guards refuse rather than serve
    /// wrong bytes (the last-dup ASCII check catches a shifted walk).
    #[test]
    fn rar_missing_name_is_none() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_rar("miss");
        make_rar5_store(&p, &[("x.png", b"x")]);
        let a = open_archive(&p).unwrap();
        assert_eq!(a.read_entry(&p, "nope.png"), None);
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn rar_listing_and_read_agree() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_rar("agree");
        make_rar5_store(&p, &[("x/1.png", b"aaa"), ("x/2.png", b"bbb")]);
        let names = rar_names(&p).unwrap();
        let files: Vec<String> = names
            .into_iter()
            .filter(|(_, d)| !*d)
            .map(|(n, _)| n)
            .collect();
        for f in &files {
            assert!(
                rar_read_entry(&p, f).is_some(),
                "listed member must extract: {f}"
            );
        }
        std::fs::remove_file(&p).ok();
    }

    /// The name-decode divergence this build exists for: the ported
    /// listing decodes a LHD_UNICODE UTF-8 name correctly, while the
    /// bundled unrar under the C locale mangles ≥0x80 bytes to
    /// U+FFFE/U+E0xx — so only the ORDINAL can match member to header.
    /// Also covers plain (no LHD_UNICODE) UTF-8 names and rarfile's
    /// last-duplicate-wins `read()` semantics.
    #[test]
    fn rar3_nonascii_names_read_by_ordinal() {
        let _g = serial();
        clear_open_archive_cache();
        let p = tmp_rar("r3nonascii");
        // 0x0200 = LHD_UNICODE; no NUL separator → unrar does NOT decode
        // it as the unicode-encoded form, so its filename diverges.
        make_rar3_store(
            &p,
            &[
                ("表紙.png".as_bytes().to_vec(), &b"cover"[..], 0x0200),
                ("第2話/001.png".as_bytes().to_vec(), &b"page1"[..], 0),
                ("dup.png".as_bytes().to_vec(), &b"first"[..], 0x0200),
                ("dup.png".as_bytes().to_vec(), &b"second"[..], 0x0200),
            ],
        );
        let names = rar_names(&p).unwrap();
        let files: Vec<String> = names
            .into_iter()
            .filter(|(_, d)| !*d)
            .map(|(n, _)| n)
            .collect();
        assert_eq!(
            files,
            vec!["表紙.png", "第2話/001.png", "dup.png", "dup.png"]
        );
        assert_eq!(rar_read_entry(&p, "表紙.png").unwrap(), b"cover");
        assert_eq!(rar_read_entry(&p, "第2話/001.png").unwrap(), b"page1");
        // rarfile `_info_map` overwrite semantics: the LAST duplicate wins.
        assert_eq!(rar_read_entry(&p, "dup.png").unwrap(), b"second");
        // Memoized path agrees.
        let a = open_archive(&p).unwrap();
        assert_eq!(a.read_entry(&p, "表紙.png").unwrap(), b"cover");
        assert_eq!(a.read_entry(&p, "dup.png").unwrap(), b"second");
        std::fs::remove_file(&p).ok();
    }
}
