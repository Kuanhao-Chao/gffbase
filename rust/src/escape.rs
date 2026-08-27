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
//! GFF3 percent-encoding. Fast-path: if no `%` byte appears, return the input
//! borrowed; otherwise decode in place. Decoding errors fall back to the
//! original byte (matches `urllib.parse.unquote` defensive behavior).

use std::borrow::Cow;

pub fn unescape(input: &str) -> Cow<'_, str> {
    if !input.contains('%') {
        return Cow::Borrowed(input);
    }
    let bytes = input.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let (Some(h), Some(l)) = (hex_val(bytes[i + 1]), hex_val(bytes[i + 2])) {
                out.push((h << 4) | l);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    Cow::Owned(String::from_utf8_lossy(&out).into_owned())
}

#[inline]
fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn passthrough_when_no_percent() {
        assert!(matches!(unescape("hello world"), Cow::Borrowed(_)));
    }

    #[test]
    fn decodes_percent_escapes() {
        assert_eq!(unescape("a%2Cb"), "a,b");
        assert_eq!(unescape("%3D%3B"), "=;");
    }

    #[test]
    fn malformed_escapes_pass_through() {
        assert_eq!(unescape("100%"), "100%");
        assert_eq!(unescape("%ZZ"), "%ZZ");
    }

    #[test]
    fn invalid_utf8_is_replaced_without_undoing_other_escapes() {
        assert_eq!(unescape("ok%20bad%FFtail%GG%2"), "ok bad\u{fffd}tail%GG%2");
    }
}
