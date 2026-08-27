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
//! Column-9 attribute parser. Hand-written state machine that handles:
//!  - GFF3:   `key=val;key=val,val2`
//!  - GTF:    `key "val"; key "val";`
//!  - Mixed:  quoted values containing `;` or `,`
//!  - Spaces around `;` (some vendors emit `; ` or ` ; `)
//!  - Trailing semicolons, leading semicolons.
//!  - Repeated keys (we add another (key, value, idx) row).

use crate::dialect::{Dialect, Format};
use crate::escape::unescape;

pub type AttributePairs = Vec<(String, String, i32)>;
pub type ParsedAttributes = (AttributePairs, Dialect);

/// Parse a single feature's column-9 string and return:
///   - the list of (key, value, multivalue_index) triples
///   - per-line dialect observations to feed `dialect::choose`
pub fn parse_attributes(
    blob: &str,
    decode_url_escapes: bool,
    compat_whole_value_quotes: bool,
) -> Result<ParsedAttributes, String> {
    let mut out: AttributePairs = Vec::new();
    let mut obs = Dialect::default();

    if blob.is_empty() {
        return Ok((out, obs));
    }

    // Detect leading / trailing semicolons.
    obs.leading_semicolon = blob.trim_start().starts_with(';');
    obs.trailing_semicolon = blob.trim_end().ends_with(';');
    if blob.contains("; ") {
        obs.field_separator = "; ".into();
    } else if blob.contains(" ; ") {
        obs.field_separator = " ; ".into();
    } else {
        obs.field_separator = ";".into();
    }

    // Decide GFF3 vs GTF: GFF3 has `key=value`; GTF has `key "value"` or
    // `key value` (space separator). The first non-empty record decides per-line.
    let segments = split_top_level_semicolons(blob, &mut obs);

    let mut keys_seen: std::collections::HashMap<String, i32> = std::collections::HashMap::new();
    let mut order: Vec<String> = Vec::new();
    let mut detected_fmt: Option<Format> = None;

    for raw_seg in segments {
        // Leading inter-attribute whitespace is separator spelling.  The
        // remainder, especially GFF3 bytes after '=', is literal value data.
        let seg = raw_seg.trim_start();
        if seg.trim().is_empty() {
            continue;
        }

        // Determine local key/val split.
        let (key, raw_val, kv_sep) = split_keyval(seg);
        if key.is_empty() {
            continue;
        }
        let local_fmt = match kv_sep {
            '=' => Format::Gff3,
            ' ' => Format::Gtf,
            _ => Format::Gff3,
        };
        match detected_fmt {
            None => detected_fmt = Some(local_fmt),
            Some(prev) if prev != local_fmt => {
                // Mixed-format line — keep first decision but flag.
            }
            _ => {}
        }

        // Quoted GTF value handling.
        let (clean_val, was_quoted) = if local_fmt == Format::Gff3 {
            if compat_whole_value_quotes {
                strip_quotes(raw_val)
            } else {
                (raw_val, false)
            }
        } else {
            strip_quotes(raw_val)
        };
        if was_quoted {
            obs.quoted_gff2_values = true;
        }

        // Multi-value handling.
        // GFF3: split on commas (canonical).
        // GTF:  rarely multi-value; we still split on comma for compatibility.
        let multi_values: Vec<&str> = if local_fmt == Format::Gff3 {
            split_unquoted_commas(clean_val)
        } else {
            vec![clean_val]
        };

        if !order.contains(&key.to_string()) {
            order.push(key.to_string());
        }
        let counter = keys_seen.entry(key.to_string()).or_insert(0);
        if *counter > 0 {
            obs.repeated_keys = true;
        }

        for v in multi_values {
            let decoded = if local_fmt == Format::Gff3 && decode_url_escapes {
                unescape(v).into_owned()
            } else {
                v.to_string()
            };
            out.push((key.to_string(), decoded, *counter));
            increment_attribute_index(counter)?;
        }
    }

    obs.fmt = detected_fmt.unwrap_or(Format::Gff3);
    obs.keyval_separator = if obs.fmt == Format::Gtf { ' ' } else { '=' };
    obs.order = order;
    Ok((out, obs))
}

fn increment_attribute_index(index: &mut i32) -> Result<(), String> {
    *index = index
        .checked_add(1)
        .ok_or_else(|| "attribute idx exceeds signed 32-bit INTEGER range".to_string())?;
    Ok(())
}

/// Split a column-9 string on top-level semicolons, honoring quoted ranges.
fn split_top_level_semicolons<'a>(blob: &'a str, obs: &mut Dialect) -> Vec<&'a str> {
    let mut out: Vec<&str> = Vec::new();
    let mut start = 0;
    let mut in_quotes = false;
    let mut escaped = false;
    for (i, ch) in blob.char_indices() {
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
        } else if ch == ';' && !in_quotes {
            out.push(&blob[start..i]);
            start = i + 1;
        } else if ch == ';' && in_quotes {
            obs.semicolon_in_quotes = true;
        }
    }
    if start <= blob.len() {
        out.push(&blob[start..]);
    }
    out
}

/// Split `key<sep>value` where sep is `=` (GFF3) or whitespace (GTF).
/// Returns (key, value, separator_char).
fn split_keyval(seg: &str) -> (&str, &str, char) {
    if let Some(eq) = find_unescaped_top_level(seg, Some('=')) {
        let (k, v) = seg.split_at(eq);
        return (k.trim(), &v[1..], '=');
    }
    // GTF-style: split on first whitespace.
    if let Some(ws) = find_unescaped_top_level(seg, None) {
        let (k, v) = seg.split_at(ws);
        return (k.trim(), v.trim_start().trim(), ' ');
    }
    (seg.trim(), "", '=')
}

fn find_unescaped_top_level(text: &str, delimiter: Option<char>) -> Option<usize> {
    let mut in_quotes = false;
    let mut escaped = false;
    for (index, ch) in text.char_indices() {
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
            continue;
        }
        let matches = delimiter.map_or_else(
            || ch.is_whitespace() && !ch.is_control(),
            |wanted| ch == wanted,
        );
        if !in_quotes && matches {
            return Some(index);
        }
    }
    None
}

fn strip_quotes(s: &str) -> (&str, bool) {
    let s = s.trim();
    if s.len() >= 2 && s.starts_with('"') && s.ends_with('"') {
        return (&s[1..s.len() - 1], true);
    }
    (s, false)
}

/// Split on commas that are not inside double quotes.
fn split_unquoted_commas(s: &str) -> Vec<&str> {
    let mut out: Vec<&str> = Vec::new();
    let mut start = 0;
    let mut in_quotes = false;
    let mut escaped = false;
    for (i, ch) in s.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        match ch {
            '\\' => escaped = true,
            '"' => in_quotes = !in_quotes,
            ',' if !in_quotes => {
                out.push(&s[start..i]);
                start = i + 1;
            }
            _ => {}
        }
    }
    out.push(&s[start..]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gff3_simple() {
        let (pairs, dialect) = parse_attributes("ID=g1;Name=foo;Parent=p1", true, false).unwrap();
        assert_eq!(pairs.len(), 3);
        assert_eq!(pairs[0], ("ID".into(), "g1".into(), 0));
        assert_eq!(dialect.fmt, Format::Gff3);
    }

    #[test]
    fn gff3_multivalue() {
        let (pairs, _) = parse_attributes("Parent=a,b,c", true, false).unwrap();
        assert_eq!(pairs.len(), 3);
        assert_eq!(pairs[0].2, 0);
        assert_eq!(pairs[1].2, 1);
        assert_eq!(pairs[2].2, 2);
    }

    #[test]
    fn gtf_quoted() {
        let (pairs, dialect) =
            parse_attributes(r#"gene_id "ENSG"; transcript_id "ENST";"#, true, false).unwrap();
        assert_eq!(pairs.len(), 2);
        assert_eq!(pairs[0].0, "gene_id");
        assert_eq!(pairs[0].1, "ENSG");
        assert_eq!(dialect.fmt, Format::Gtf);
        assert!(dialect.quoted_gff2_values);
        assert!(dialect.trailing_semicolon);
    }

    #[test]
    fn semicolon_in_quotes() {
        let (pairs, dialect) = parse_attributes(r#"note "a;b";ID=g1"#, true, false).unwrap();
        assert_eq!(pairs.len(), 2);
        assert_eq!(pairs[0].1, "a;b");
        assert!(dialect.semicolon_in_quotes);
    }

    #[test]
    fn percent_escapes() {
        let (pairs, _) = parse_attributes("Note=hello%20world%2C%20you", true, false).unwrap();
        assert_eq!(pairs[0].1, "hello world, you");
    }

    #[test]
    fn delimiters_inside_quotes_or_escape_sequences_are_data() {
        let blob = r#"gene_id "G=雪"; note "quoted \"value\" and escaped\;semicolon %3B";"#;
        let (pairs, dialect) = parse_attributes(blob, true, false).unwrap();
        assert_eq!(dialect.fmt, Format::Gtf);
        assert_eq!(
            pairs,
            vec![
                ("gene_id".into(), "G=雪".into(), 0),
                (
                    "note".into(),
                    r#"quoted \"value\" and escaped\;semicolon %3B"#.into(),
                    0,
                ),
            ]
        );
    }

    #[test]
    fn escaped_semicolon_does_not_split_gff3_attribute() {
        let (pairs, dialect) = parse_attributes(
            r#"ID=x;Note=left\;right;Encoded=%E9%9B%AA%3Bdone"#,
            true,
            false,
        )
        .unwrap();
        assert_eq!(dialect.fmt, Format::Gff3);
        assert_eq!(
            pairs,
            vec![
                ("ID".into(), "x".into(), 0),
                ("Note".into(), r#"left\;right"#.into(), 0),
                ("Encoded".into(), "雪;done".into(), 0),
            ]
        );
    }

    #[test]
    fn preserves_gff3_whitespace_after_equals() {
        let (pairs, _) = parse_attributes("ID=x;Note= ", true, false).unwrap();
        assert_eq!(pairs[1], ("Note".into(), " ".into(), 0));
    }

    #[test]
    fn url_escape_decoding_can_be_disabled() {
        let (pairs, _) = parse_attributes("Note=ok%20bad%3Btail", false, false).unwrap();
        assert_eq!(pairs[0].1, "ok%20bad%3Btail");
    }

    #[test]
    fn attribute_indices_exceed_unsigned_16_bit_without_wrapping() {
        let values = (0..=65_536)
            .map(|index| format!("p{index}"))
            .collect::<Vec<_>>()
            .join(",");
        let (pairs, _) = parse_attributes(&format!("Parent={values}"), true, false).unwrap();
        assert_eq!(pairs.last().unwrap().2, 65_536);
    }

    #[test]
    fn attribute_index_overflow_is_explicit() {
        let mut index = i32::MAX;
        let error = increment_attribute_index(&mut index).unwrap_err();
        assert!(error.contains("signed 32-bit INTEGER"));
    }

    #[test]
    fn compatibility_strips_whole_quoted_gff3_values_before_splitting() {
        let (pairs, dialect) = parse_attributes(r#"ID="001";types="a,b,c""#, true, true).unwrap();
        assert_eq!(
            pairs,
            vec![
                ("ID".into(), "001".into(), 0),
                ("types".into(), "a".into(), 0),
                ("types".into(), "b".into(), 1),
                ("types".into(), "c".into(), 2),
            ]
        );
        assert!(dialect.quoted_gff2_values);
    }
}
