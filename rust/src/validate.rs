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
//! NCBI GFF3 compliance validation.
//!
//! Enforces the rules at
//! <https://www.ncbi.nlm.nih.gov/datasets/docs/v2/reference-docs/file-formats/annotation-files/about-ncbi-gff3/>:
//! 9 tab-separated columns, 1-based integer coordinates with `start <= end`,
//! strand ∈ {`+`,`-`,`?`,`.`}, phase ∈ {`0`,`1`,`2`,`.`} (mandatory `0/1/2`
//! for `CDS` rows), score ∈ {float, `.`}, non-empty `seqid` and
//! whitespace-free `featuretype`, attributes parseable as `key=value` pairs.
//!
//! Errors are STRUCTURED — every `GffError` carries the offending line
//! number, an `ErrorKind` enum, and a human-readable message ready to be
//! shown to a user grepping a 3 GB annotation file.

use std::fmt;

/// Which rule set to apply.
///
/// `Gffutils` is the compatibility profile used by `create_db()`. It exists
/// because real annotation files violate the GFF3 specification routinely, and
/// gffutils reads them anyway -- validating to the spec on the drop-in path
/// meant refusing 6 of the 23 upstream fixtures the oracle ingests, including
/// its own canonical one. Under this profile every rule below still *runs*,
/// but a violation is recorded as a warning and the record is kept rather than
/// rejected, so a caller gets exactly gffutils' data plus a diagnostic
/// gffutils never offered.
///
/// `Ncbi` is the full specification, used by `parse_gff()` and by
/// `mode="strict"`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ValidationProfile {
    Gffutils,
    Ncbi,
}

impl ValidationProfile {
    /// Whether a violation should reject the record rather than annotate it.
    pub fn rejects(&self) -> bool {
        matches!(self, ValidationProfile::Ncbi)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    TooFewFields,
    TooManyFields,
    EmptySeqid,
    EmptyFeaturetype,
    InvalidFeaturetype,
    InvalidCoordinate,
    InvalidStrand,
    InvalidPhase,
    InvalidScore,
    InvalidAttribute,
}

impl ErrorKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            ErrorKind::TooFewFields => "TooFewFields",
            ErrorKind::TooManyFields => "TooManyFields",
            ErrorKind::EmptySeqid => "EmptySeqid",
            ErrorKind::EmptyFeaturetype => "EmptyFeaturetype",
            ErrorKind::InvalidFeaturetype => "InvalidFeaturetype",
            ErrorKind::InvalidCoordinate => "InvalidCoordinate",
            ErrorKind::InvalidStrand => "InvalidStrand",
            ErrorKind::InvalidPhase => "InvalidPhase",
            ErrorKind::InvalidScore => "InvalidScore",
            ErrorKind::InvalidAttribute => "InvalidAttribute",
        }
    }
}

#[derive(Debug, Clone)]
pub struct GffError {
    pub line_no: usize,
    pub kind: ErrorKind,
    pub message: String,
}

impl GffError {
    pub fn new(line_no: usize, kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            line_no,
            kind,
            message: message.into(),
        }
    }
}

impl fmt::Display for GffError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "GFF3 format error on line {} [{}]: {}",
            self.line_no,
            self.kind.as_str(),
            self.message,
        )
    }
}

/// Validate the parsed 9-column GFF3 record. The strict NCBI profile requires
/// concrete, positive coordinates; the compatibility profile invokes these
/// same checks but records violations instead of rejecting the record.
#[allow(clippy::too_many_arguments)]
#[cfg(test)]
pub fn validate_fields(
    line_no: usize,
    seqid: &str,
    featuretype: &str,
    start: Option<i64>,
    end: Option<i64>,
    score: &str,
    strand: &str,
    frame: &str,
    attrs_blob: &[u8],
    is_gtf: bool,
) -> Result<(), GffError> {
    match validate_field_errors(
        line_no,
        seqid,
        featuretype,
        start,
        end,
        score,
        strand,
        frame,
        attrs_blob,
        is_gtf,
    )
    .into_iter()
    .next()
    {
        Some(error) => Err(error),
        None => Ok(()),
    }
}

/// Return every applicable field diagnostic in the same order strict mode
/// uses to select its first rejection.
#[allow(clippy::too_many_arguments)]
pub fn validate_field_errors(
    line_no: usize,
    seqid: &str,
    featuretype: &str,
    start: Option<i64>,
    end: Option<i64>,
    score: &str,
    strand: &str,
    frame: &str,
    attrs_blob: &[u8],
    is_gtf: bool,
) -> Vec<GffError> {
    let mut errors = Vec::new();
    if seqid.is_empty() {
        errors.push(GffError::new(
            line_no,
            ErrorKind::EmptySeqid,
            "seqid (col 1) is empty",
        ));
    }

    if featuretype.is_empty() {
        errors.push(GffError::new(
            line_no,
            ErrorKind::EmptyFeaturetype,
            "featuretype (col 3) is empty",
        ));
    }
    if featuretype.chars().any(is_token_whitespace_or_control) {
        errors.push(GffError::new(
            line_no,
            ErrorKind::InvalidFeaturetype,
            format!("featuretype contains whitespace: {:?}", featuretype),
        ));
    }

    match start {
        None => {
            errors.push(GffError::new(
                line_no,
                ErrorKind::InvalidCoordinate,
                "start coordinate must be a positive integer; got '.'",
            ));
        }
        Some(value) if value < 1 => errors.push(GffError::new(
            line_no,
            ErrorKind::InvalidCoordinate,
            format!("start coordinate must be >= 1 (got {})", value),
        )),
        Some(_) => {}
    }
    match end {
        None => {
            errors.push(GffError::new(
                line_no,
                ErrorKind::InvalidCoordinate,
                "end coordinate must be a positive integer; got '.'",
            ));
        }
        Some(value) if value < 1 => errors.push(GffError::new(
            line_no,
            ErrorKind::InvalidCoordinate,
            format!("end coordinate must be >= 1 (got {})", value),
        )),
        Some(_) => {}
    }
    if let (Some(start), Some(end)) = (start, end) {
        if start > 0 && end > 0 && end < start {
            errors.push(GffError::new(
                line_no,
                ErrorKind::InvalidCoordinate,
                format!("end < start ({} < {})", end, start),
            ));
        }
    }

    if !matches!(strand, "+" | "-" | "?" | ".") {
        errors.push(GffError::new(
            line_no,
            ErrorKind::InvalidStrand,
            format!("strand must be one of '+', '-', '?', '.'; got {:?}", strand),
        ));
    }

    if !matches!(frame, "." | "0" | "1" | "2") {
        errors.push(GffError::new(
            line_no,
            ErrorKind::InvalidPhase,
            format!("phase must be 0, 1, 2, or '.'; got {:?}", frame),
        ));
    }
    if featuretype == "CDS" && frame == "." {
        errors.push(GffError::new(
            line_no,
            ErrorKind::InvalidPhase,
            "CDS row missing required phase (must be 0, 1, or 2)",
        ));
    }

    if score != "." && !score.is_empty() && score != score.trim() {
        errors.push(GffError::new(
            line_no,
            ErrorKind::InvalidScore,
            format!(
                "score must not contain surrounding whitespace; got {:?}",
                score
            ),
        ));
    } else if score != "." && !score.is_empty() {
        match score.parse::<f64>() {
            Ok(value) if value.is_finite() => {}
            Ok(_) => {
                errors.push(GffError::new(
                    line_no,
                    ErrorKind::InvalidScore,
                    format!("score must be finite or '.'; got {:?}", score),
                ));
            }
            Err(_) => {
                errors.push(GffError::new(
                    line_no,
                    ErrorKind::InvalidScore,
                    format!("score must be a float or '.'; got {:?}", score),
                ));
            }
        }
    }

    // Attribute-string structure is validated AFTER parsing in
    // `parser.rs::Iterator::next` — see `validate_attributes_pairs`
    // below. Mixing the check in here would force this function to
    // duplicate the parser's GTF/GFF3 dispatch.
    let _ = (attrs_blob, is_gtf);

    errors
}

/// Post-parse attribute check. If the parser yielded zero `(key, value)`
/// pairs from a non-empty col-9 string, the blob is structurally
/// malformed regardless of dialect. Returns `Ok(())` for the empty / `.`
/// blob (some real-world files emit these).
///
/// For GFF3 inputs we additionally require the raw blob to contain at
/// least one `=` somewhere — the attribute parser is permissive enough
/// to yield a `(key, "")` pair from raw garbage like `"justsomegarbage"`,
/// which the GFF3 spec forbids.
pub fn validate_attributes_pairs(
    line_no: usize,
    n_pairs: usize,
    attrs_blob: &[u8],
    is_gtf: bool,
) -> Result<(), GffError> {
    let trimmed = std::str::from_utf8(attrs_blob).unwrap_or("").trim();
    if trimmed.is_empty() || trimmed == "." {
        return Ok(());
    }

    if let Err(message) = validate_attribute_syntax(trimmed, is_gtf) {
        return Err(GffError::new(line_no, ErrorKind::InvalidAttribute, message));
    }
    if n_pairs == 0 {
        return Err(GffError::new(
            line_no,
            ErrorKind::InvalidAttribute,
            format!(
                "attribute string did not parse into any key/value pair: {:?}",
                preview(trimmed)
            ),
        ));
    }
    Ok(())
}

fn preview(value: &str) -> String {
    value.chars().take(60).collect()
}

fn valid_attribute_key(key: &str) -> bool {
    !key.is_empty()
        && !key.chars().any(|ch| {
            is_token_whitespace_or_control(ch) || matches!(ch, ';' | ',' | '=' | '%' | '&' | '"')
        })
}

fn is_token_whitespace_or_control(ch: char) -> bool {
    ch.is_whitespace() || ch.is_control()
}

fn valid_percent_escapes(value: &str) -> bool {
    let bytes = value.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] != b'%' {
            index += 1;
            continue;
        }
        if index + 2 >= bytes.len()
            || !bytes[index + 1].is_ascii_hexdigit()
            || !bytes[index + 2].is_ascii_hexdigit()
        {
            return false;
        }
        index += 3;
    }
    true
}

fn valid_gtf_quoted_value(value: &str) -> bool {
    if value.len() < 2 || !value.starts_with('"') || !value.ends_with('"') {
        return false;
    }
    let inner = &value[1..value.len() - 1];
    let mut escaped = false;
    for ch in inner.chars() {
        if ch == '"' && !escaped {
            return false;
        }
        if ch == '\\' {
            escaped = !escaped;
        } else {
            escaped = false;
        }
    }
    true
}

fn validate_attribute_syntax(trimmed: &str, is_gtf: bool) -> Result<(), String> {
    let mut segments: Vec<&str> = Vec::new();
    let mut start = 0;
    let mut in_quotes = false;
    let mut escaped = false;
    for (index, ch) in trimmed.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
            continue;
        }
        if ch == '"' {
            in_quotes = !in_quotes;
        }
        if ch == ';' && !in_quotes {
            segments.push(&trimmed[start..index]);
            start = index + ch.len_utf8();
        }
    }
    if in_quotes {
        return Err("attribute string contains an unbalanced double quote".to_string());
    }
    segments.push(&trimmed[start..]);
    if segments
        .last()
        .is_some_and(|segment| segment.trim().is_empty())
    {
        segments.pop();
    }
    if segments.is_empty() || segments.iter().any(|segment| segment.trim().is_empty()) {
        return Err("attribute string contains an empty attribute".to_string());
    }

    for raw_segment in segments {
        let segment = raw_segment.trim();
        if is_gtf {
            let split_at = segment
                .find(|ch: char| ch.is_whitespace() && !ch.is_control())
                .ok_or_else(|| {
                    format!(
                        "GTF attribute is not a quoted key/value pair: {:?}",
                        preview(segment)
                    )
                })?;
            let key = &segment[..split_at];
            let value = segment[split_at..].trim();
            if !valid_attribute_key(key) {
                return Err(format!(
                    "attribute key is empty or contains a reserved character: {:?}",
                    key
                ));
            }
            if !valid_gtf_quoted_value(value) {
                return Err(format!(
                    "GTF attribute value must be double-quoted: {:?}",
                    preview(segment)
                ));
            }
            continue;
        }

        let (key, value) = segment
            .split_once('=')
            .ok_or_else(|| format!("GFF3 attribute is missing '=': {:?}", preview(segment)))?;
        if !valid_attribute_key(key) {
            return Err(format!(
                "attribute key is empty or contains a reserved character: {:?}",
                key
            ));
        }
        if value.contains('"') {
            return Err(format!(
                "GFF3 attribute value contains an unescaped quote: {:?}",
                preview(segment)
            ));
        }
        if !valid_percent_escapes(value) {
            return Err(format!(
                "GFF3 attribute contains an invalid percent escape: {:?}",
                preview(segment)
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    // A GFF row is nine columns wide, so the per-column test helpers below
    // legitimately take one argument per column.
    #![allow(clippy::too_many_arguments)]

    use super::*;

    fn ok(
        seqid: &str,
        ft: &str,
        s: Option<i64>,
        e: Option<i64>,
        score: &str,
        strand: &str,
        frame: &str,
        attrs: &[u8],
    ) {
        validate_fields(1, seqid, ft, s, e, score, strand, frame, attrs, false).unwrap();
    }
    fn err_kind(
        seqid: &str,
        ft: &str,
        s: Option<i64>,
        e: Option<i64>,
        score: &str,
        strand: &str,
        frame: &str,
        attrs: &[u8],
    ) -> ErrorKind {
        validate_fields(1, seqid, ft, s, e, score, strand, frame, attrs, false)
            .unwrap_err()
            .kind
    }

    #[test]
    fn happy_path() {
        ok("chr1", "exon", Some(1), Some(100), ".", "+", ".", b"ID=x");
    }

    #[test]
    fn empty_seqid() {
        assert_eq!(
            err_kind("", "exon", Some(1), Some(10), ".", "+", ".", b"ID=x"),
            ErrorKind::EmptySeqid,
        );
    }

    #[test]
    fn whitespace_in_featuretype() {
        assert_eq!(
            err_kind(
                "chr1",
                "exon foo",
                Some(1),
                Some(10),
                ".",
                "+",
                ".",
                b"ID=x"
            ),
            ErrorKind::InvalidFeaturetype,
        );
    }

    #[test]
    fn coord_zero_or_negative() {
        assert_eq!(
            err_kind("chr1", "exon", Some(0), Some(10), ".", "+", ".", b"ID=x"),
            ErrorKind::InvalidCoordinate,
        );
        assert_eq!(
            err_kind("chr1", "exon", None, Some(10), ".", "+", ".", b"ID=x"),
            ErrorKind::InvalidCoordinate,
        );
        assert_eq!(
            err_kind("chr1", "exon", Some(1), None, ".", "+", ".", b"ID=x"),
            ErrorKind::InvalidCoordinate,
        );
        assert_eq!(
            err_kind("chr1", "exon", Some(1), Some(-1), ".", "+", ".", b"ID=x"),
            ErrorKind::InvalidCoordinate,
        );
        assert_eq!(
            err_kind("chr1", "exon", Some(-5), Some(10), ".", "+", ".", b"ID=x"),
            ErrorKind::InvalidCoordinate,
        );
    }

    #[test]
    fn end_less_than_start() {
        assert_eq!(
            err_kind("chr1", "exon", Some(100), Some(50), ".", "+", ".", b"ID=x"),
            ErrorKind::InvalidCoordinate,
        );
    }

    #[test]
    fn invalid_strand() {
        assert_eq!(
            err_kind("chr1", "exon", Some(1), Some(10), ".", "@", ".", b"ID=x"),
            ErrorKind::InvalidStrand,
        );
        assert_eq!(
            err_kind("chr1", "exon", Some(1), Some(10), ".", "+-", ".", b"ID=x"),
            ErrorKind::InvalidStrand,
        );
    }

    #[test]
    fn strand_question_and_dot_ok() {
        ok("chr1", "exon", Some(1), Some(10), ".", "?", ".", b"ID=x");
        ok("chr1", "exon", Some(1), Some(10), ".", ".", ".", b"ID=x");
    }

    #[test]
    fn invalid_phase() {
        assert_eq!(
            err_kind("chr1", "exon", Some(1), Some(10), ".", "+", "5", b"ID=x"),
            ErrorKind::InvalidPhase,
        );
    }

    #[test]
    fn cds_requires_concrete_phase() {
        assert_eq!(
            err_kind("chr1", "CDS", Some(1), Some(10), ".", "+", ".", b"ID=x"),
            ErrorKind::InvalidPhase,
        );
        ok("chr1", "CDS", Some(1), Some(10), ".", "+", "0", b"ID=x");
    }

    #[test]
    fn invalid_score() {
        assert_eq!(
            err_kind("chr1", "exon", Some(1), Some(10), "abc", "+", ".", b"ID=x"),
            ErrorKind::InvalidScore,
        );
        ok("chr1", "exon", Some(1), Some(10), "0.95", "+", ".", b"ID=x");
        ok("chr1", "exon", Some(1), Some(10), ".", "+", ".", b"ID=x");
        for score in ["nan", "NaN", "inf", "+inf", "-inf", "Infinity"] {
            assert_eq!(
                err_kind("chr1", "exon", Some(1), Some(10), score, "+", ".", b"ID=x"),
                ErrorKind::InvalidScore,
            );
        }
    }

    #[test]
    fn validate_attributes_pairs_rules() {
        // Empty/dot accepted.
        validate_attributes_pairs(1, 0, b"", false).unwrap();
        validate_attributes_pairs(1, 0, b".", false).unwrap();
        validate_attributes_pairs(1, 0, b"   ", false).unwrap();
        // GFF3 garbage → error even if the parser produced one stub pair.
        let e = validate_attributes_pairs(7, 1, b"justsomegarbage", false).unwrap_err();
        assert_eq!(e.kind, ErrorKind::InvalidAttribute);
        assert_eq!(e.line_no, 7);
        // GFF3 well-formed → OK.
        validate_attributes_pairs(1, 1, b"ID=x", false).unwrap();
        // GTF well-formed → OK.
        validate_attributes_pairs(1, 1, br#"gene_id "ENSG";"#, true).unwrap();
        // GTF unquoted garbage (no `=`, no `"`) → error.
        let e = validate_attributes_pairs(1, 1, b"unquoted", true).unwrap_err();
        assert_eq!(e.kind, ErrorKind::InvalidAttribute);
        // A GTF blob cannot masquerade as strict GFF3.
        assert!(validate_attributes_pairs(1, 1, br#"gene_id "ENSG";"#, false).is_err());
        // Missing separators, empty keys, unbalanced quotes, and malformed
        // percent escapes are all rejected by the strict grammar.
        for blob in [
            b"ID=x;broken".as_slice(),
            b"=x".as_slice(),
            b"ID=x;Name=\"unterminated".as_slice(),
            b"ID=bad%XZ".as_slice(),
        ] {
            assert!(validate_attributes_pairs(1, 1, blob, false).is_err());
        }
    }
}
