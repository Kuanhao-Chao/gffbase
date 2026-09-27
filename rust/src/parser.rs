// ---------------------------------------------------------------------------
// Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
// Copyright 2026 Kuan-Hao Chao
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
// ---------------------------------------------------------------------------
//! Streaming GFF3/GTF record parser.
//!
//! Reads from a file (mmap'd plain text or gzip'd) or from an in-memory byte
//! slice and yields `Record`s lazily. Splits lines and tab fields with `memchr`
//! for SIMD-accelerated scanning. Stops feature emission at the GFF3 `##FASTA`
//! sentinel.

use std::fs::File;
use std::io::{BufReader, Cursor, Read};
use std::path::Path;

use flate2::read::MultiGzDecoder;
use memchr::memchr2;

use crate::attributes::{parse_attributes, parse_attributes_observing};
use crate::dialect::{self, Dialect};
use crate::validate::{
    validate_attributes_pairs, validate_field_errors, ErrorKind, GffError, ValidationProfile,
};

#[derive(Debug, Clone)]
pub struct Record {
    pub seqid: String,
    pub source: String,
    pub featuretype: String,
    pub start: Option<i64>,
    pub end: Option<i64>,
    pub score: String,
    pub strand: String,
    pub frame: String,
    pub attributes_blob: Vec<u8>,
    pub attributes_pairs: Vec<(String, String, i32)>,
    pub extra: Vec<String>,
}

pub struct ParseOptions {
    pub checklines: usize,
    pub force_dialect_check: bool,
    pub force_gff: bool,
    /// When true (default), the iterator yields `Err(GffError)` on the
    /// first malformed line and the caller is expected to surface it as
    /// a Python `GFFFormatError`. When false, the iterator silently
    /// drops malformed lines and pushes a `GffError` onto its
    /// `warnings` vector — the caller can inspect them via
    /// `RecordIter::warnings()`.
    pub strict: bool,
    /// Which rule set to apply. `Gffutils` keeps a violating record and
    /// records a warning; `Ncbi` rejects it (raising or dropping per
    /// `strict`). See `validate::ValidationProfile`.
    pub profile: ValidationProfile,
    pub decode_url_escapes: bool,
}

/// A simple text source: either a Vec<u8> we own or a Read trait object.
pub enum FileSource {
    Bytes(Vec<u8>),
    Stream(Box<dyn Read + Send + Sync>),
}

/// The first two bytes of every gzip member (RFC 1952). bgzip output is a
/// series of gzip members, so it starts with them too.
const GZIP_MAGIC: [u8; 2] = [0x1f, 0x8b];

/// Read up to `buf.len()` bytes, stopping early only at end of input.
fn read_up_to(r: &mut impl Read, buf: &mut [u8]) -> std::io::Result<usize> {
    let mut n = 0;
    while n < buf.len() {
        match r.read(&mut buf[n..]) {
            Ok(0) => break,
            Ok(k) => n += k,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(n)
}

const TAR_BLOCK: usize = 512;

/// A tar header number: octal text, or GNU base-256 for large values.
fn tar_number(field: &[u8]) -> Option<u64> {
    if field.first().is_some_and(|b| b & 0x80 != 0) {
        let mut n = u64::from(field[0] & 0x7f);
        for &b in &field[1..] {
            n = n.checked_mul(256)?.checked_add(u64::from(b))?;
        }
        return Some(n);
    }
    let end = field.iter().position(|&b| b == 0).unwrap_or(field.len());
    let digits = std::str::from_utf8(&field[..end]).ok()?.trim_matches(' ');
    if digits.is_empty() {
        return Some(0);
    }
    u64::from_str_radix(digits, 8).ok()
}

/// A ustar header block with a valid checksum. The checksum keeps a GFF
/// file that happens to have `ustar` at byte 257 from reading as an archive.
fn is_tar_header(block: &[u8]) -> bool {
    if block.len() != TAR_BLOCK || &block[257..262] != b"ustar" {
        return false;
    }
    let sum: u64 = block[..148]
        .iter()
        .chain(&block[156..])
        .map(|&b| u64::from(b))
        .sum::<u64>()
        + 8 * 0x20;
    tar_number(&block[148..156]) == Some(sum)
}

/// The byte range of the one regular file in a tar archive read from `r`, or
/// None if `r` is not a tar archive.
///
/// FlyBase publishes `dmel-all-r6.69.gff.gz` as a gzipped *tar* of the GFF:
/// read as text, the 512-byte header became part of line 1 and the zero
/// padding a last line. A one-file archive is read as that file; anything
/// else is refused before a record is parsed. Headers are read and bodies
/// skipped, so the archive is never held in memory. Mirrors `_tar_member` in
/// the Python fallback, message for message.
fn tar_member<R: Read>(mut r: R) -> Result<Option<(u64, u64)>, String> {
    let io = |e: std::io::Error| e.to_string();
    let mut block = [0u8; TAR_BLOCK];
    let (mut at, mut member, mut pax_size) = (0u64, None, None::<u64>);
    let mut first = true;
    loop {
        let n = read_up_to(&mut r, &mut block).map_err(io)?;
        if n < TAR_BLOCK || block.iter().all(|&b| b == 0) {
            break;
        }
        if !is_tar_header(&block) {
            if first {
                return Ok(None);
            }
            return Err("malformed tar archive: a header block is corrupt".to_string());
        }
        first = false;
        let mut size = tar_number(&block[124..136])
            .ok_or("malformed tar archive: a header size is not a number")?;
        let body = at + TAR_BLOCK as u64;
        let mut consumed = 0u64;
        match block[156] {
            b'x' => {
                // pax extended header: may carry the real size
                let mut records = Vec::new();
                consumed = (&mut r).take(size).read_to_end(&mut records).map_err(io)? as u64;
                for record in records.split(|&b| b == b'\n') {
                    let kv = record.splitn(2, |&b| b == b' ').nth(1).unwrap_or(b"");
                    if let Some(v) = kv.strip_prefix(b"size=") {
                        pax_size = std::str::from_utf8(v).ok().and_then(|v| v.parse().ok());
                    }
                }
            }
            b'0' | 0 | b'7' => {
                if let Some(real) = pax_size.take() {
                    size = real;
                }
                if member.is_some() {
                    return Err(
                        "tar archive holds more than one file; extract the one to load".to_string(),
                    );
                }
                member = Some((body, body.saturating_add(size)));
            }
            _ => {}
        }
        let padded = size.div_ceil(TAR_BLOCK as u64) * TAR_BLOCK as u64;
        std::io::copy(
            &mut (&mut r).take(padded - consumed.min(padded)),
            &mut std::io::sink(),
        )
        .map_err(io)?;
        at = body.saturating_add(padded);
    }
    if first {
        return Ok(None);
    }
    member
        .map(Some)
        .ok_or_else(|| "tar archive holds no regular file".to_string())
}

/// Why an input could not be opened: the file itself, or what it holds.
#[derive(Debug)]
pub enum OpenError {
    Io(std::io::Error),
    Format(String),
    /// The content could not be decoded (a corrupt or truncated gzip stream).
    Read(String),
}

/// What a failed read of the (decoded) input means, in the words both
/// engines use.
fn describe_read_error(e: &std::io::Error) -> String {
    if e.kind() == std::io::ErrorKind::UnexpectedEof {
        "the compressed input ends before its end marker".to_string()
    } else {
        format!("the input could not be read: {e}")
    }
}

/// An error from the file (it has an OS error code) or from its content.
fn io_or_read(e: std::io::Error) -> OpenError {
    if e.raw_os_error().is_some() {
        OpenError::Io(e)
    } else {
        OpenError::Read(describe_read_error(&e))
    }
}

impl From<std::io::Error> for OpenError {
    fn from(e: std::io::Error) -> Self {
        OpenError::Io(e)
    }
}

/// `path`'s bytes, gunzipped if the content is gzip.
///
/// Decided by content, not by name. Keying on a `.gz` extension read `.bgz`,
/// `.GZ` and extensionless gzip files as text: the compressed bytes then
/// parsed as zero features and ingest built an empty database without a
/// word. The magic bytes are chained back in front of the rest of the file
/// rather than seeked over, so a pipe works as well as a file.
fn decoded<P: AsRef<Path>>(path: P) -> std::io::Result<Box<dyn Read + Send + Sync>> {
    let mut f = File::open(path.as_ref())?;
    let mut magic = [0u8; 2];
    let n = read_up_to(&mut f, &mut magic)?;
    let source = Cursor::new(magic[..n].to_vec()).chain(f);
    Ok(if n == GZIP_MAGIC.len() && magic == GZIP_MAGIC {
        // MultiGzDecoder, not GzDecoder: it reads every member of a
        // concatenated (bgzip) stream instead of stopping after the first.
        Box::new(MultiGzDecoder::new(BufReader::new(source)))
    } else {
        Box::new(BufReader::new(source))
    })
}

impl FileSource {
    /// Open `path` as a stream: gunzipped if it is gzip, and narrowed to the
    /// one file inside if it is a tar archive. Nothing is read ahead beyond a
    /// header block, so a whole-genome file is never held in memory; a tar
    /// archive is read twice, once to check it holds exactly one file.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, OpenError> {
        let mut r = decoded(path.as_ref())?;
        let mut head = vec![0u8; TAR_BLOCK];
        let n = read_up_to(&mut r, &mut head).map_err(io_or_read)?;
        head.truncate(n);
        let is_tar = n == TAR_BLOCK && is_tar_header(&head);
        let r: Box<dyn Read + Send + Sync> = Box::new(Cursor::new(head).chain(r));
        if !is_tar {
            return Ok(FileSource::Stream(r));
        }
        let (body, end) = tar_member(r)
            .map_err(OpenError::Format)?
            .ok_or_else(|| OpenError::Format("malformed tar archive".to_string()))?;
        let mut r = decoded(path.as_ref())?;
        std::io::copy(&mut (&mut r).take(body), &mut std::io::sink())?;
        Ok(FileSource::Stream(Box::new(r.take(end - body))))
    }

    /// An in-memory input, narrowed to the one file inside if it is a tar
    /// archive.
    pub fn from_bytes(b: Vec<u8>) -> Result<Self, String> {
        match tar_member(Cursor::new(b.as_slice()))? {
            None => Ok(FileSource::Bytes(b)),
            Some((body, end)) => {
                let end = (end as usize).min(b.len());
                Ok(FileSource::Bytes(b[(body as usize).min(end)..end].to_vec()))
            }
        }
    }
}

/// How much input the window reads at a time.
const CHUNK: usize = 1 << 20;

/// Records from a `FileSource`, parsed a line at a time.
///
/// The input is read through a window of about `CHUNK` bytes, refilled as
/// lines are consumed. It used to be read whole into memory first: 1.9 GB
/// of RSS for GENCODE before a single record was parsed, 6.7 GB for FlyBase.
pub struct RecordIter {
    src: Option<Box<dyn Read + Send + Sync>>,
    /// The window: `buf[pos..]` is unread input.
    buf: Vec<u8>,
    pos: usize,
    eof: bool,
    /// While set, input from this offset on stays in the window, so the
    /// dialect peek can rewind to it. Kept current as the window slides.
    pinned: Option<usize>,
    /// A read failure, reported again on every later read.
    read_error: Option<String>,
    /// Set once a read failure has been returned: nothing follows it.
    done: bool,
    dialect: Dialect,
    directives: Vec<String>,
    fasta_reached: bool,
    line_no: usize,
    strict: bool,
    profile: ValidationProfile,
    decode_url_escapes: bool,
    warnings: Vec<GffError>,
}

impl RecordIter {
    pub fn new(source: FileSource, opts: ParseOptions) -> Result<Self, String> {
        let (src, buf, eof) = match source {
            FileSource::Bytes(b) => (None, b, true),
            FileSource::Stream(r) => (Some(r), Vec::new(), false),
        };
        let mut iter = RecordIter {
            src,
            buf,
            pos: 0,
            eof,
            pinned: None,
            read_error: None,
            done: false,
            dialect: Dialect::default(),
            directives: Vec::new(),
            fasta_reached: false,
            line_no: 0,
            strict: opts.strict,
            profile: opts.profile,
            decode_url_escapes: opts.decode_url_escapes,
            warnings: Vec::new(),
        };
        if !iter.eof {
            iter.fill().map_err(|e| e.message)?;
        }
        // A UTF-8 byte-order mark is not part of the first seqid.
        if iter.buf.starts_with(b"\xef\xbb\xbf") {
            iter.pos = 3;
        }
        iter.peek_dialect(&opts);
        Ok(iter)
    }

    /// Read more input into the window, first dropping what has been
    /// consumed (all of it, or up to the pin while the dialect peek runs).
    fn fill(&mut self) -> Result<(), GffError> {
        let keep_from = self.pinned.map_or(self.pos, |p| p.min(self.pos));
        if keep_from > 0 {
            self.buf.drain(..keep_from);
            self.pos -= keep_from;
            if let Some(p) = self.pinned.as_mut() {
                *p -= keep_from;
            }
        }
        let Some(src) = self.src.as_mut() else {
            self.eof = true;
            return Ok(());
        };
        let old = self.buf.len();
        self.buf.resize(old + CHUNK, 0);
        let mut n = 0;
        while n < CHUNK {
            match src.read(&mut self.buf[old + n..]) {
                Ok(0) => break,
                Ok(k) => n += k,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => {
                    // Keep what was read before the failure: its complete
                    // lines are still returned, and the error after them.
                    self.read_error = Some(describe_read_error(&e));
                    break;
                }
            }
        }
        self.buf.truncate(old + n);
        if n < CHUNK {
            self.eof = true;
            self.src = None;
        }
        Ok(())
    }

    /// The pending read failure, as the error that ends iteration.
    fn read_failure(&self) -> Option<GffError> {
        self.read_error
            .as_ref()
            .map(|m| GffError::new(self.line_no, ErrorKind::ReadError, m.clone()))
    }

    /// Errors collected when running with `strict=false`. Empty in
    /// strict mode (errors propagate via `Iterator::next` instead).
    pub fn warnings(&self) -> &[GffError] {
        &self.warnings
    }

    pub fn dialect(&self) -> &Dialect {
        &self.dialect
    }

    pub fn directives(&self) -> &[String] {
        &self.directives
    }

    /// The number of the last line read: the line of the record `next`
    /// just returned.
    pub fn line_no(&self) -> usize {
        self.line_no
    }

    /// Peek up to `checklines` features (without consuming them) to compute
    /// the dialect. We snapshot `pos`, walk forward, then reset.
    fn peek_dialect(&mut self, opts: &ParseOptions) {
        self.pinned = Some(self.pos);
        let saved_line = self.line_no;
        let saved_directives = self.directives.clone();
        let saved_fasta = self.fasta_reached;
        let saved_warnings = self.warnings.len();

        let mut samples: Vec<Dialect> = Vec::new();
        // `checklines + 1`, not `checklines`: gffutils' `peek(n)` appends
        // before testing `i == n`, so it samples one record more than it
        // says -- and `checklines=0` still samples one. The Python engine
        // counts the same way.
        let limit = if opts.force_dialect_check {
            usize::MAX
        } else {
            opts.checklines.saturating_add(1)
        };
        while samples.len() < limit {
            match self.next_raw_record() {
                Some(Ok((_, _, _, _, _, _, _, _, blob, _))) => {
                    let blob_str = std::str::from_utf8(&blob).unwrap_or("");
                    if let Ok((_pairs, obs)) =
                        parse_attributes(blob_str, opts.decode_url_escapes, !opts.profile.rejects())
                    {
                        samples.push(obs);
                    }
                }
                // Skip it and keep sampling, as the Python engine does. This
                // used to `break`, so one malformed line near the top of a
                // GTF left no samples, the dialect defaulted to GFF3, and the
                // whole gene/transcript hierarchy was silently never built.
                // The record is not lost: iteration re-reads it after the
                // reset below and raises or records it there. A read failure
                // ends sampling: the input stops there.
                Some(Err(e)) if e.kind == ErrorKind::ReadError => break,
                Some(Err(_)) => continue,
                None => break,
            }
        }
        self.dialect = dialect::choose(&samples);

        // Reset.
        self.pos = self.pinned.take().unwrap_or(0);
        self.line_no = saved_line;
        self.directives = saved_directives;
        self.fasta_reached = saved_fasta;
        self.warnings.truncate(saved_warnings);

        // force_gff overrides format detection.
        if opts.force_gff {
            self.dialect.fmt = crate::dialect::Format::Gff3;
            self.dialect.keyval_separator = '=';
        }
    }

    /// Read the next non-comment, non-blank line as tab-separated fields.
    /// Returns the raw fields plus the attributes blob (col 9 raw bytes).
    /// Errors are structured `GffError` values carrying the offending
    /// line number; `lib.rs` converts them to Python `GFFFormatError`s.
    #[allow(clippy::type_complexity)]
    fn next_raw_record(
        &mut self,
    ) -> Option<
        Result<
            (
                String,      // seqid
                String,      // source
                String,      // featuretype
                Option<i64>, // start
                Option<i64>, // end
                String,      // score
                String,      // strand
                String,      // frame
                Vec<u8>,     // blob
                Vec<String>, // extra
            ),
            GffError,
        >,
    > {
        loop {
            if self.fasta_reached {
                return None;
            }
            let line_owned: Vec<u8> = match self.read_line()? {
                Ok(line) => line,
                Err(e) => return Some(Err(e)),
            };
            let cur_line_no = self.line_no;
            if line_owned.is_empty() {
                continue;
            }
            // Trim the trailing \r BEFORE anything inspects the line. It used
            // to happen further down, after directive handling had already
            // run, so on a CRLF file every directive was stored with a
            // trailing \r -- `##sequence-region chr1 1 1000\r` -- while the
            // Python fallback, which reads with universal newlines, stored it
            // clean. The two engines are supposed to be indistinguishable.
            let line = trim_cr(line_owned.as_slice());
            // Whitespace-only lines are blank, as gffutils treats them. One
            // used to become a feature named after the spaces.
            if line.iter().all(|b| b.is_ascii_whitespace()) {
                continue;
            }
            // Validate UTF-8 once, for the whole line, before any field is
            // read. Doing it per-field meant each field chose its own failure
            // mode: the attribute blob silently became "" (dropping every
            // attribute on the line, ID included), while seqid and
            // featuretype went through `from_utf8_lossy` and silently became
            // U+FFFD -- a chromosome name that matches nothing, with no
            // warning. The Python fallback decodes the whole line at read
            // time, so a line-level check is also what makes the two engines
            // agree.
            if std::str::from_utf8(line).is_err() {
                return Some(Err(GffError::new(
                    self.line_no,
                    ErrorKind::InvalidAttribute,
                    "line is not valid UTF-8".to_string(),
                )));
            }
            // A NUL byte never occurs in text, so the line is binary data. It
            // needs saying: once a lone CR ends a line, a binary file splits
            // into short runs of control bytes that ARE valid UTF-8, and
            // compat mode would keep each one as a feature.
            if memchr::memchr(0, line).is_some() {
                return Some(Err(GffError::new(
                    self.line_no,
                    ErrorKind::InvalidAttribute,
                    "line contains a NUL byte (binary data, not text)".to_string(),
                )));
            }
            // Directive / comment handling.
            if line.starts_with(b"##") {
                let s = std::str::from_utf8(line).unwrap_or("").to_string();
                if s.starts_with("##FASTA") {
                    self.fasta_reached = true;
                    return None;
                }
                // Strip the leading `##`, matching gffutils' directive
                // handler: `db.directives` is a documented attribute, so the
                // stored form is part of the compatibility contract.
                self.directives.push(s[2..].to_string());
                continue;
            }
            // A bare `>` line starts an embedded FASTA section even without a
            // preceding `##FASTA` directive -- gffutils stops at either, and
            // FBgn0031208.gff relies on it. Without this the sequence lines
            // become degenerate features.
            if line.first() == Some(&b'>') {
                self.fasta_reached = true;
                return None;
            }
            if line.starts_with(b"#") {
                continue;
            }
            // Tab split. (`line` was \r-trimmed above, before any path
            // inspected it.)
            let mut fields = split_tabs(line);
            match fields.len().cmp(&9) {
                std::cmp::Ordering::Less => {
                    let err = GffError::new(
                        cur_line_no,
                        ErrorKind::TooFewFields,
                        format!(
                            "expected at least 9 tab-separated fields, found {}",
                            fields.len()
                        ),
                    );
                    if self.profile.rejects() {
                        return Some(Err(err));
                    }
                    // Compat: gffutils never errors here. `feature_from_line`
                    // splits on tab and `zip(_gffkeys, fields)` truncates, so a
                    // space-delimited line becomes one feature whose seqid is the
                    // whole line and whose remaining columns take their defaults.
                    // Reproducing that is deliberate -- refusing a file the oracle
                    // reads is worse for a drop-in -- and the warning is what
                    // makes it safe.
                    self.warnings.push(err);
                    while fields.len() < 9 {
                        fields.push(if fields.len() == 8 { b"" } else { b"." });
                    }
                }
                std::cmp::Ordering::Greater => {
                    let err = GffError::new(
                        cur_line_no,
                        ErrorKind::TooManyFields,
                        format!(
                            "expected exactly 9 tab-separated fields, found {}",
                            fields.len()
                        ),
                    );
                    if self.profile.rejects() {
                        return Some(Err(err));
                    }
                    // Compatibility mode preserves the extra columns because
                    // gffutils exposes them on Feature.extra, but surfaces the
                    // standards violation to callers.
                    self.warnings.push(err);
                }
                std::cmp::Ordering::Equal => {}
            }
            let seqid = bytes_to_string(fields[0]);
            let source = bytes_to_string(fields[1]);
            let featuretype = bytes_to_string(fields[2]);
            // Coordinate parsing distinguishes "valid `.` / empty"
            // (yielding `None`) from "non-numeric trash" (a hard error).
            let start = match parse_coord_strict(fields[3]) {
                Ok(v) => v,
                Err(_) => {
                    return Some(Err(GffError::new(
                        cur_line_no,
                        ErrorKind::InvalidCoordinate,
                        format!(
                            "start coordinate is not an integer: {:?}",
                            std::str::from_utf8(fields[3]).unwrap_or("<non-utf8>")
                        ),
                    )));
                }
            };
            let end = match parse_coord_strict(fields[4]) {
                Ok(v) => v,
                Err(_) => {
                    return Some(Err(GffError::new(
                        cur_line_no,
                        ErrorKind::InvalidCoordinate,
                        format!(
                            "end coordinate is not an integer: {:?}",
                            std::str::from_utf8(fields[4]).unwrap_or("<non-utf8>")
                        ),
                    )));
                }
            };
            let score = bytes_to_string(fields[5]);
            let strand = bytes_to_string(fields[6]);
            let frame = bytes_to_string(fields[7]);
            let blob = fields[8].to_vec();
            let mut extra: Vec<String> = Vec::new();
            for ex in fields.iter().skip(9) {
                extra.push(bytes_to_string(ex));
            }
            return Some(Ok((
                seqid,
                source,
                featuretype,
                start,
                end,
                score,
                strand,
                frame,
                blob,
                extra,
            )));
        }
    }

    /// The next physical line, without its terminator; None at the end.
    /// `\n`, `\r\n` and a lone `\r` (classic Mac) all end a line, as they do
    /// for gffutils and the Python engine. Splitting on `\n` alone read a
    /// CR-only file as ONE line, so its first feature swallowed every other
    /// (an ID of `g1\rchr1...`).
    fn read_line(&mut self) -> Option<Result<Vec<u8>, GffError>> {
        loop {
            if let Some(i) = memchr2(b'\n', b'\r', &self.buf[self.pos..]) {
                let at = self.pos + i;
                // A CR at the edge of the window may be half of a CRLF.
                if self.buf[at] == b'\r' && at + 1 == self.buf.len() && !self.eof {
                    if let Err(e) = self.fill() {
                        return Some(Err(e));
                    }
                    continue;
                }
                let crlf = self.buf[at] == b'\r' && self.buf.get(at + 1) == Some(&b'\n');
                let line = self.buf[self.pos..at].to_vec();
                self.pos = at + if crlf { 2 } else { 1 };
                self.line_no += 1;
                return Some(Ok(line));
            }
            if self.eof {
                // After a read failure the unterminated tail is a fragment,
                // not a line: the failure is reported instead.
                if let Some(e) = self.read_failure() {
                    return Some(Err(e));
                }
                if self.pos >= self.buf.len() {
                    return None;
                }
                let line = self.buf[self.pos..].to_vec();
                self.pos = self.buf.len();
                self.line_no += 1;
                return Some(Ok(line));
            }
            if let Err(e) = self.fill() {
                return Some(Err(e));
            }
        }
    }
}

impl Iterator for RecordIter {
    type Item = Result<Record, GffError>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.done {
            return None;
        }
        loop {
            let raw = match self.next_raw_record() {
                Some(Ok(r)) => r,
                // The input stops here whatever the strictness: raise it.
                Some(Err(e)) if e.kind == ErrorKind::ReadError => {
                    self.done = true;
                    return Some(Err(e));
                }
                Some(Err(e)) => {
                    if self.strict && self.profile.rejects() {
                        return Some(Err(e));
                    }
                    self.warnings.push(e);
                    // Try the next line.
                    continue;
                }
                None => return None,
            };
            let (seqid, source, featuretype, start, end, score, strand, frame, blob, extra) = raw;

            let blob_str = std::str::from_utf8(&blob).unwrap_or("");
            let (pairs, obs) = match parse_attributes_observing(
                blob_str,
                self.decode_url_escapes,
                !self.profile.rejects(),
                false,
            ) {
                Ok(parsed) => parsed,
                Err(message) => {
                    return Some(Err(GffError::new(
                        self.line_no,
                        ErrorKind::InvalidAttribute,
                        message,
                    )));
                }
            };
            let is_gtf = matches!(obs.fmt, crate::dialect::Format::Gtf);
            let field_errors = validate_field_errors(
                self.line_no, // line we just consumed
                &seqid,
                &featuretype,
                start,
                end,
                &score,
                &strand,
                &frame,
                &blob,
                is_gtf,
            );
            if let Some(e) = field_errors.first() {
                if self.profile.rejects() {
                    if self.strict {
                        return Some(Err(e.clone()));
                    }
                    self.warnings.push(e.clone());
                    continue;
                }
                // Compat: annotate every applicable rule, then keep the record.
                self.warnings.extend(field_errors);
            }

            // Post-parse attribute structure check (handles both GFF3 and
            // GTF correctly because it inspects what the parser produced).
            if let Err(e) = validate_attributes_pairs(self.line_no, pairs.len(), &blob, is_gtf) {
                if self.profile.rejects() {
                    if self.strict {
                        return Some(Err(e));
                    }
                    self.warnings.push(e);
                    continue;
                }
                self.warnings.push(e);
            }
            return Some(Ok(Record {
                seqid,
                source,
                featuretype,
                start,
                end,
                score,
                strand,
                frame,
                attributes_blob: blob,
                attributes_pairs: pairs,
                extra,
            }));
        }
    }
}

fn split_tabs(line: &[u8]) -> Vec<&[u8]> {
    let mut out: Vec<&[u8]> = Vec::with_capacity(9);
    let mut start = 0usize;
    let mut i = 0usize;
    while i < line.len() {
        if line[i] == b'\t' {
            out.push(&line[start..i]);
            start = i + 1;
        }
        i += 1;
    }
    out.push(&line[start..]);
    out
}

fn trim_cr(line: &[u8]) -> &[u8] {
    if let Some((&b'\r', rest)) = line.split_last() {
        rest
    } else {
        line
    }
}

fn bytes_to_string(b: &[u8]) -> String {
    String::from_utf8_lossy(b).into_owned()
}

/// Parse a coordinate column, distinguishing "valid `.` / empty" from
/// "non-numeric trash". Returns `Ok(None)` for `.` / empty, `Ok(Some(n))`
/// for a real integer, and `Err(())` for anything else. The caller turns
/// the `Err` into a structured `GffError`.
fn parse_coord_strict(b: &[u8]) -> Result<Option<i64>, ()> {
    let s = std::str::from_utf8(b).map_err(|_| ())?;
    // Trim surrounding whitespace before parsing.
    //
    // Python's `int()` strips whitespace and Rust's `parse::<i64>()` does not,
    // so without this the two engines disagree on any file with a padded
    // coordinate column -- `wormbase_gff2.txt` has `944828 ` and the Rust
    // engine dropped that record while the pure-Python fallback kept it.
    // gffutils accepts it, so compat requires accepting it too.
    let s = s.trim();
    if s == "." || s.is_empty() {
        return Ok(None);
    }
    s.parse::<i64>().map(Some).map_err(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;
    use flate2::write::GzEncoder;
    use flate2::Compression;
    use std::io::Write;
    use std::path::PathBuf;

    const TEXT: &[u8] = b"##gff-version 3\nchr1\tt\tgene\t1\t10\t.\t+\t.\tID=g1\n";

    fn gz(data: &[u8]) -> Vec<u8> {
        let mut e = GzEncoder::new(Vec::new(), Compression::default());
        e.write_all(data).unwrap();
        e.finish().unwrap()
    }

    /// A file in the system temp dir, removed when dropped.
    struct TempFile(PathBuf);

    impl TempFile {
        fn new(name: &str, contents: &[u8]) -> Self {
            let path = std::env::temp_dir().join(format!(
                "gffbase-parser-{}-{}",
                std::process::id(),
                name
            ));
            std::fs::write(&path, contents).unwrap();
            TempFile(path)
        }
    }

    impl Drop for TempFile {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }

    fn read_all(name: &str, contents: &[u8]) -> Vec<u8> {
        let f = TempFile::new(name, contents);
        match FileSource::open(&f.0).unwrap() {
            FileSource::Bytes(b) => b,
            FileSource::Stream(mut r) => {
                let mut v = Vec::new();
                r.read_to_end(&mut v).unwrap();
                v
            }
        }
    }

    #[test]
    fn plain_text_is_read_verbatim() {
        assert_eq!(read_all("plain.gff3", TEXT), TEXT);
    }

    #[test]
    fn gzip_is_detected_by_content_whatever_the_name() {
        for name in ["a.gff3.gz", "b.gff3.bgz", "c.GFF3.GZ", "d_no_extension"] {
            assert_eq!(read_all(name, &gz(TEXT)), TEXT, "{name}");
        }
    }

    #[test]
    fn a_gz_name_on_plain_text_is_still_text() {
        assert_eq!(read_all("mislabelled.gff3.gz", TEXT), TEXT);
    }

    #[test]
    fn every_member_of_a_multi_member_stream_is_read() {
        // bgzip writes a series of independent gzip members.
        let mut data = gz(&TEXT[..16]);
        data.extend(gz(&TEXT[16..]));
        assert_eq!(read_all("multi.bgz", &data), TEXT);
    }

    fn opts(checklines: usize, force_dialect_check: bool) -> ParseOptions {
        ParseOptions {
            checklines,
            force_dialect_check,
            force_gff: false,
            strict: false,
            profile: ValidationProfile::Gffutils,
            decode_url_escapes: true,
        }
    }

    fn gene(i: usize) -> String {
        format!("chr1\tt\tgene\t{}\t{}\t.\t+\t.\tID=g{i}", i + 1, i + 2)
    }

    fn ids(iter: RecordIter) -> Vec<String> {
        iter.map(|r| r.unwrap().attributes_pairs[0].1.clone())
            .collect()
    }

    #[test]
    fn a_crlf_split_by_the_window_edge_is_one_line_end() {
        // Line 1 ends with `\r` exactly at the end of the first window, so its
        // `\n` arrives with the next read.
        let first = gene(0);
        let pad = "x".repeat(CHUNK - first.len() - "\r".len() - ";Note=".len());
        let mut text = format!("{first};Note={pad}\r\n");
        assert_eq!(text.len(), CHUNK + 1);
        text.push_str(&format!("{}\r\n", gene(1)));
        let f = TempFile::new("crlf_edge.gff3", text.as_bytes());
        let iter = RecordIter::new(FileSource::open(&f.0).unwrap(), opts(10, false)).unwrap();
        assert_eq!(ids(iter), ["g0", "g1"]);
    }

    #[test]
    fn a_line_longer_than_the_window_is_read_whole() {
        let long = format!("{};Note={}\n{}\n", gene(0), "y".repeat(3 * CHUNK), gene(1));
        let f = TempFile::new("long_line.gff3", long.as_bytes());
        let iter = RecordIter::new(FileSource::open(&f.0).unwrap(), opts(10, false)).unwrap();
        let records: Vec<Record> = iter.map(|r| r.unwrap()).collect();
        assert_eq!(records.len(), 2);
        assert_eq!(records[0].attributes_pairs[1].1.len(), 3 * CHUNK);
    }

    #[test]
    fn many_windows_keep_every_line_and_its_number() {
        let n = 3 * CHUNK / 40;
        let text: String = (0..n).map(|i| gene(i) + "\n").collect();
        let f = TempFile::new("many.gff3.gz", &gz(text.as_bytes()));
        // A dialect peek over every line pins the whole input; it must still
        // rewind to the first record.
        for force in [false, true] {
            let iter = RecordIter::new(FileSource::open(&f.0).unwrap(), opts(10, force)).unwrap();
            let got = ids(iter);
            assert_eq!(got.len(), n, "force_dialect_check={force}");
            assert_eq!(got[0], "g0");
            assert_eq!(got[n - 1], format!("g{}", n - 1));
        }
    }

    #[test]
    fn a_truncated_gzip_ends_with_a_read_error() {
        let text: String = (0..200_000).map(|i| gene(i) + "\n").collect();
        let z = gz(text.as_bytes());
        let f = TempFile::new("trunc.gff3.gz", &z[..z.len() / 2]);
        let mut iter = RecordIter::new(FileSource::open(&f.0).unwrap(), opts(10, false)).unwrap();
        let mut n = 0;
        let err = loop {
            match iter.next() {
                Some(Ok(_)) => n += 1,
                Some(Err(e)) => break e,
                None => panic!("a truncated stream ended without an error"),
            }
        };
        assert!(n > 0, "records before the cut are kept");
        assert_eq!(err.kind, ErrorKind::ReadError);
        assert!(iter.next().is_none(), "nothing follows a read error");
    }

    #[test]
    fn a_tar_archive_on_disk_streams_its_one_file() {
        let body = format!("{}\n{}\n", gene(0), gene(1));
        let mut header = [0u8; TAR_BLOCK];
        header[..6].copy_from_slice(b"a.gff3");
        header[100..108].copy_from_slice(b"0000644\0");
        header[124..136].copy_from_slice(format!("{:011o}\0", body.len()).as_bytes());
        header[156] = b'0';
        header[257..263].copy_from_slice(b"ustar\0");
        header[263..265].copy_from_slice(b"00");
        header[148..156].copy_from_slice(b"        ");
        let sum: u32 = header.iter().map(|&b| u32::from(b)).sum();
        header[148..156].copy_from_slice(format!("{sum:06o}\0 ").as_bytes());
        let mut archive = header.to_vec();
        archive.extend(body.as_bytes());
        archive.resize(
            archive.len().div_ceil(TAR_BLOCK) * TAR_BLOCK + 2 * TAR_BLOCK,
            0,
        );
        let f = TempFile::new("a.tar.gz", &gz(&archive));
        let iter = RecordIter::new(FileSource::open(&f.0).unwrap(), opts(10, false)).unwrap();
        assert_eq!(ids(iter), ["g0", "g1"]);
    }

    /// Where parse time goes, on a real file:
    /// `GFFBASE_PROFILE_INPUT=<file> cargo test --release -- --ignored --nocapture profile_parse`
    #[test]
    #[ignore]
    fn profile_parse() {
        use std::time::Instant;
        let Ok(path) = std::env::var("GFFBASE_PROFILE_INPUT") else {
            return;
        };
        let open = || RecordIter::new(FileSource::open(&path).unwrap(), opts(10, false)).unwrap();
        let t = Instant::now();
        let mut it = open();
        let mut n = 0;
        while let Some(Ok(_)) = it.read_line() {
            n += 1;
        }
        println!(
            "lines only:      {:.2} s ({n} lines)",
            t.elapsed().as_secs_f64()
        );
        let t = Instant::now();
        let mut it = open();
        let mut n = 0;
        while let Some(r) = it.next_raw_record() {
            r.unwrap();
            n += 1;
        }
        println!(
            "fields split:    {:.2} s ({n} records)",
            t.elapsed().as_secs_f64()
        );
        let mut it = open();
        let mut raws = Vec::new();
        while let Some(r) = it.next_raw_record() {
            raws.push(r.unwrap());
        }
        let t = Instant::now();
        for r in &raws {
            let blob = std::str::from_utf8(&r.8).unwrap_or("");
            std::hint::black_box(parse_attributes(blob, true, true).unwrap());
        }
        println!("parse_attributes: {:.2} s", t.elapsed().as_secs_f64());
        let t = Instant::now();
        for r in &raws {
            std::hint::black_box(validate_field_errors(
                1, &r.0, &r.2, r.3, r.4, &r.5, &r.6, &r.7, &r.8, false,
            ));
        }
        println!("field validation: {:.2} s", t.elapsed().as_secs_f64());
        let t = Instant::now();
        let n = open().count();
        println!(
            "full records:    {:.2} s ({n} records)",
            t.elapsed().as_secs_f64()
        );
    }

    #[test]
    fn files_shorter_than_the_magic_are_text() {
        assert_eq!(read_all("empty", b""), b"");
        assert_eq!(read_all("one_byte", b"\x1f"), b"\x1f");
    }
}
