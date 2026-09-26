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
//! Dialect representation. Mirrors the `gffutils.constants.dialect` shape so
//! Python code can consume it without translation.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Gff3,
    Gtf,
}

#[derive(Debug, Clone)]
pub struct Dialect {
    pub fmt: Format,
    pub field_separator: String,  // ";", "; ", " ; "
    pub keyval_separator: char,   // '=' for GFF3, ' ' for GTF
    pub multival_separator: char, // typically ','
    pub leading_semicolon: bool,
    pub trailing_semicolon: bool,
    pub quoted_gff2_values: bool,
    pub repeated_keys: bool,
    pub semicolon_in_quotes: bool,
    pub order: Vec<String>,
}

impl Default for Dialect {
    fn default() -> Self {
        Dialect {
            fmt: Format::Gff3,
            field_separator: ";".into(),
            keyval_separator: '=',
            multival_separator: ',',
            leading_semicolon: false,
            trailing_semicolon: false,
            quoted_gff2_values: false,
            repeated_keys: false,
            semicolon_in_quotes: false,
            order: Vec::new(),
        }
    }
}

impl Dialect {
    pub fn fmt_str(&self) -> &'static str {
        match self.fmt {
            Format::Gff3 => "gff3",
            Format::Gtf => "gtf",
        }
    }
}

/// Reconcile per-line observations into a single dialect, the way gffutils'
/// `helpers._choose_dialect` does: every key is decided by its own vote, a
/// line weighs as much as it has distinct attributes, and a tie goes to the
/// value seen first.
///
/// This used to be a plain majority for the format and an OR over the flags.
/// The OR made one quoted line re-quote a whole file on output, and the
/// unweighted count let an attribute-less line (a 5-column row, say) outvote
/// a real one -- both differences from gffutils on files that load the same.
///
/// Tallies are insertion-ordered vectors, not a HashMap: a `HashMap`'s
/// per-process seed resolved ties differently on every run, which once made
/// dialect inference -- and therefore re-serialization -- nondeterministic.
pub fn choose(samples: &[Dialect]) -> Dialect {
    if samples.is_empty() {
        return Dialect::default();
    }

    fn vote<T: PartialEq + Clone>(samples: &[Dialect], get: impl Fn(&Dialect) -> T) -> T {
        let mut tally: Vec<(T, usize)> = Vec::new();
        for s in samples {
            let value = get(s);
            let weight = s.order.len();
            match tally.iter_mut().find(|(k, _)| *k == value) {
                Some((_, count)) => *count += weight,
                None => tally.push((value, weight)),
            }
        }
        // The first maximum: `>` rather than `>=` keeps the earliest value on
        // a tie, which is what a stable descending sort (gffutils) does.
        let mut best = 0;
        for (i, (_, count)) in tally.iter().enumerate().skip(1) {
            if *count > tally[best].1 {
                best = i;
            }
        }
        tally.swap_remove(best).0
    }

    let mut chosen = Dialect {
        fmt: vote(samples, |d| d.fmt),
        field_separator: vote(samples, |d| d.field_separator.clone()),
        keyval_separator: vote(samples, |d| d.keyval_separator),
        multival_separator: vote(samples, |d| d.multival_separator),
        leading_semicolon: vote(samples, |d| d.leading_semicolon),
        trailing_semicolon: vote(samples, |d| d.trailing_semicolon),
        quoted_gff2_values: vote(samples, |d| d.quoted_gff2_values),
        repeated_keys: vote(samples, |d| d.repeated_keys),
        semicolon_in_quotes: vote(samples, |d| d.semicolon_in_quotes),
        order: Vec::new(),
    };

    // Attribute key order: first appearance across samples, as gffutils.
    let mut seen = std::collections::HashSet::new();
    for s in samples {
        for k in &s.order {
            if seen.insert(k.clone()) {
                chosen.order.push(k.clone());
            }
        }
    }
    chosen
}
