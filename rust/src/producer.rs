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
//! Ingest rows as columns: records resolved to ids and batched without a
//! Python object per row.
//!
//! This is the per-row half of `ingest._build_database`, moved out of Python:
//! id_spec resolution, generated ids, the duplicate-id strategies, the
//! AUGUSTUS bare-token rows, and the Arrow-shaped column buffers. Every rule
//! here mirrors a line of the Python loop, which stays as the path for
//! `transform=`, callable id specs and the pure-Python engine -- and as the
//! oracle `tests/test_ingest_producer_equivalence.py` compares against.

use std::collections::HashMap;

use crate::parser::{Record, RecordIter};
use crate::validate::GffError;

/// One id_spec key: an attribute, or a GFF column written `:name:`.
#[derive(Debug, Clone)]
pub enum Key {
    Attribute(String),
    Column(String),
}

/// `create_db(id_spec=...)`, the forms that need no Python call.
#[derive(Debug, Clone)]
pub enum IdSpec {
    /// A key, or keys tried in order.
    Keys(Vec<Key>),
    /// featuretype -> keys. A featuretype absent from the mapping gets a
    /// generated id at once, without trying other keys (gffutils' rule).
    ByType(HashMap<String, Vec<Key>>),
}

/// What a repeated id does.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Strategy {
    Error,
    Warning,
    CreateUnique,
    /// `merge` / `replace`: resolved after the load against the stored row.
    Defer,
    /// `mode="strict"`: a candidate discontinuous feature, kept under a
    /// surrogate id for the multipart pass.
    Fuse,
}

pub struct Config {
    pub id_spec: IdSpec,
    pub strategy: Strategy,
    /// (gene key, transcript key) when the input is GTF.
    pub gtf_keys: Option<(String, String)>,
    pub surrogate_sep: String,
    pub seqid_band: i64,
    pub batch_size: usize,
}

/// What stops the load, besides a parse error.
#[derive(Debug)]
pub enum Stop {
    Parse(GffError),
    /// `merge_strategy="error"`: (id, line, line of the first occurrence).
    Duplicate(String, usize, usize),
    /// An id_spec key with several values.
    MultiValuedId(String),
}

/// A growable Arrow-style string/binary column: i64 offsets plus bytes.
#[derive(Default)]
pub struct Bytes {
    pub offsets: Vec<i64>,
    pub data: Vec<u8>,
}

impl Bytes {
    fn new() -> Self {
        Bytes::with_capacity(0, 0)
    }
    fn with_capacity(rows: usize, bytes: usize) -> Self {
        let mut offsets = Vec::with_capacity(rows + 1);
        offsets.push(0);
        Bytes {
            offsets,
            data: Vec::with_capacity(bytes),
        }
    }
    fn push(&mut self, value: &[u8]) {
        self.data.extend_from_slice(value);
        self.offsets.push(self.data.len() as i64);
    }
}

/// A nullable i64 column: values plus a validity bitmap (bit set = valid).
#[derive(Default)]
pub struct NullableI64 {
    pub values: Vec<i64>,
    pub validity: Vec<u8>,
    pub nulls: usize,
}

impl NullableI64 {
    fn push(&mut self, value: Option<i64>) {
        let i = self.values.len();
        if i % 8 == 0 {
            self.validity.push(0);
        }
        match value {
            Some(v) => {
                self.values.push(v);
                self.validity[i / 8] |= 1 << (i % 8);
            }
            None => {
                self.values.push(0);
                self.nulls += 1;
            }
        }
    }
}

/// One batch, column by column, in `_ArrowBatchBuilder.FEATURES_SCHEMA` and
/// `ATTRIBUTES_SCHEMA` order.
#[derive(Default)]
pub struct Batch {
    pub n: usize,
    pub id: Bytes,
    pub seqid: Bytes,
    pub source: Bytes,
    pub featuretype: Bytes,
    pub start: NullableI64,
    pub end: NullableI64,
    pub score: Bytes,
    pub strand: Bytes,
    pub frame: Bytes,
    pub attributes_blob: Bytes,
    pub extra_blob: Bytes,
    pub file_order: Vec<i64>,
    pub raw_id: Bytes,
    pub occ: Vec<i32>,
    pub id_origin: Bytes,
    pub seqid_y: Vec<i64>,
    pub a_n: usize,
    /// Each attribute row's feature, as its row in this batch: the column is
    /// dictionary-encoded against `id`. Spelled out, the repeated ids were
    /// the largest buffer in a batch (96 MiB of MANE's 346).
    pub a_feature_row: Vec<i32>,
    /// Each attribute row's key, as an index into `a_keys`: a file has a few
    /// dozen distinct keys and millions of attribute rows.
    pub a_key: Vec<i32>,
    pub a_keys: Bytes,
    a_key_index: HashMap<String, i32>,
    pub a_value: Bytes,
    pub a_idx: Vec<i32>,
    /// `merge` / `replace` rows: (id, file_order, record).
    pub deferred: Vec<(String, i64, Record)>,
    /// `merge_strategy="warning"`: the ids of the lines dropped, in order.
    pub dropped: Vec<String>,
}

/// Buffer sizes of the previous batch, reserved up front for the next so
/// the buffers are not grown by doubling -- which left up to half of each
/// one unused at the peak.
#[derive(Clone, Copy, Default)]
pub struct Hint {
    rows: usize,
    attribute_rows: usize,
    text: usize,
    blob: usize,
    value: usize,
}

impl Batch {
    fn with_hint(h: Hint) -> Self {
        // Headroom for a batch a little larger than the last.
        let grow = |n: usize| n + n / 8;
        let (rows, attrs) = (grow(h.rows), grow(h.attribute_rows));
        let text = || Bytes::with_capacity(rows, grow(h.text));
        Batch {
            id: text(),
            seqid: text(),
            source: text(),
            featuretype: text(),
            score: text(),
            strand: text(),
            frame: text(),
            raw_id: text(),
            id_origin: text(),
            attributes_blob: Bytes::with_capacity(rows, grow(h.blob)),
            extra_blob: Bytes::with_capacity(rows, 0),
            file_order: Vec::with_capacity(rows),
            occ: Vec::with_capacity(rows),
            seqid_y: Vec::with_capacity(rows),
            a_feature_row: Vec::with_capacity(attrs),
            a_key: Vec::with_capacity(attrs),
            a_idx: Vec::with_capacity(attrs),
            a_value: Bytes::with_capacity(attrs, grow(h.value)),
            a_keys: Bytes::new(),
            ..Default::default()
        }
    }

    fn hint(&self) -> Hint {
        Hint {
            rows: self.n,
            attribute_rows: self.a_n,
            text: self.id.data.len(),
            blob: self.attributes_blob.data.len(),
            value: self.a_value.data.len(),
        }
    }

    fn new() -> Self {
        Batch {
            id: Bytes::new(),
            seqid: Bytes::new(),
            source: Bytes::new(),
            featuretype: Bytes::new(),
            score: Bytes::new(),
            strand: Bytes::new(),
            frame: Bytes::new(),
            attributes_blob: Bytes::new(),
            extra_blob: Bytes::new(),
            raw_id: Bytes::new(),
            id_origin: Bytes::new(),
            a_keys: Bytes::new(),
            a_value: Bytes::new(),
            ..Default::default()
        }
    }
}

pub struct Producer {
    pub iter: RecordIter,
    cfg: Config,
    /// id -> (how many lines so far yielded it, the line of the first), plus
    /// every generated or renamed id (count 1): the Python loop's `seen_ids`,
    /// with what the duplicate error quotes. One map, so one copy of each id.
    seen: HashMap<Box<str>, (u32, u32)>,
    pub autoinc: HashMap<String, i64>,
    /// Autoincrement bases in the order they were first used.
    pub autoinc_order: Vec<String>,
    pub seqid_y: HashMap<String, i64>,
    pub seqid_order: Vec<String>,
    pub file_order: i64,
    pub n_raw: u64,
    pub n_skipped: u64,
    done: bool,
    hint: Option<Hint>,
}

impl Producer {
    pub fn new(iter: RecordIter, cfg: Config, autoinc_seed: Vec<(String, i64)>) -> Self {
        let mut autoinc = HashMap::new();
        let mut autoinc_order = Vec::new();
        for (base, n) in autoinc_seed {
            autoinc_order.push(base.clone());
            autoinc.insert(base, n);
        }
        Producer {
            iter,
            cfg,
            seen: HashMap::new(),
            autoinc,
            autoinc_order,
            seqid_y: HashMap::new(),
            seqid_order: Vec::new(),
            file_order: 0,
            n_raw: 0,
            n_skipped: 0,
            done: false,
            hint: None,
        }
    }

    fn autoincrement(&mut self, base: &str) -> String {
        let n = match self.autoinc.get_mut(base) {
            Some(n) => {
                *n += 1;
                *n
            }
            None => {
                self.autoinc.insert(base.to_string(), 1);
                self.autoinc_order.push(base.to_string());
                1
            }
        };
        format!("{base}_{n}")
    }

    /// `IdSpecResolver.resolve`: (id, generated?).
    fn resolve(&mut self, rec: &Record) -> Result<(String, bool), Stop> {
        let keys = match &self.cfg.id_spec {
            IdSpec::Keys(keys) => Some(keys.clone()),
            IdSpec::ByType(by_type) => by_type.get(&rec.featuretype).cloned(),
        };
        let Some(keys) = keys else {
            return Ok((self.autoincrement(&rec.featuretype), true));
        };
        for key in &keys {
            match key {
                Key::Column(name) => return Ok((column(rec, name), false)),
                Key::Attribute(name) => {
                    let mut values = rec
                        .attributes_pairs
                        .iter()
                        .filter(|(k, _, _)| k == name)
                        .map(|(_, v, _)| v);
                    let first = values.next();
                    if values.next().is_some() {
                        return Err(Stop::MultiValuedId(name.clone()));
                    }
                    // `ID=` is treated as absent, as gffutils does.
                    if let Some(v) = first.filter(|v| !v.is_empty()) {
                        return Ok((v.clone(), false));
                    }
                }
            }
        }
        Ok((self.autoincrement(&rec.featuretype), true))
    }

    /// The next batch, or None once the input is exhausted.
    pub fn next_batch(&mut self) -> Result<Option<Batch>, Stop> {
        if self.done {
            return Ok(None);
        }
        let mut batch = self.hint.map_or_else(Batch::new, Batch::with_hint);
        while batch.n < self.cfg.batch_size {
            let mut rec = match self.iter.next() {
                None => {
                    self.done = true;
                    break;
                }
                Some(Err(e)) => return Err(Stop::Parse(e)),
                Some(Ok(rec)) => rec,
            };
            self.file_order += 1;
            self.n_raw += 1;
            let line_no = self.iter.line_no();

            if let Some((gene_key, transcript_key)) = &self.cfg.gtf_keys {
                if rec.featuretype == "gene" || rec.featuretype == "transcript" {
                    let key = if rec.featuretype == "gene" {
                        gene_key
                    } else {
                        transcript_key
                    };
                    name_bare_gtf_parent(&mut rec, key);
                }
            }

            let (mut fid, generated) = self.resolve(&rec)?;
            if generated {
                // A generated id a line before this one used literally is not
                // a duplicate of it; draw the next free one instead.
                while self.seen.contains_key(fid.as_str()) {
                    fid = self.autoincrement(&rec.featuretype);
                }
            }
            let raw_id = fid.clone();
            let (occ, first) = self
                .seen
                .get(fid.as_str())
                .copied()
                .unwrap_or((0, line_no as u32));
            if occ > 0 {
                match self.cfg.strategy {
                    Strategy::Fuse => {
                        fid = format!("{raw_id}{}{occ}", self.cfg.surrogate_sep);
                    }
                    Strategy::Error => {
                        return Err(Stop::Duplicate(fid, line_no, first as usize));
                    }
                    Strategy::Warning => {
                        batch.dropped.push(fid);
                        self.n_skipped += 1;
                        continue;
                    }
                    Strategy::CreateUnique => {
                        fid = self.autoincrement(&raw_id);
                        while self.seen.contains_key(fid.as_str()) {
                            fid = self.autoincrement(&raw_id);
                        }
                    }
                    Strategy::Defer => {
                        batch.deferred.push((fid, self.file_order, rec));
                        continue;
                    }
                }
            }
            self.seen
                .insert(raw_id.clone().into_boxed_str(), (occ + 1, first));
            if fid != raw_id {
                self.seen
                    .insert(fid.clone().into_boxed_str(), (1, line_no as u32));
            }
            let origin = if generated {
                "autoincrement"
            } else {
                "attribute"
            };
            self.append(&mut batch, &fid, &rec, &raw_id, occ as i32, origin);
        }
        if batch.n == 0 && batch.deferred.is_empty() && batch.dropped.is_empty() && self.done {
            return Ok(None);
        }
        self.hint = Some(batch.hint());
        Ok(Some(batch))
    }

    /// `_ArrowBatchBuilder.append`.
    fn append(
        &mut self,
        b: &mut Batch,
        fid: &str,
        rec: &Record,
        raw_id: &str,
        occ: i32,
        origin: &str,
    ) {
        let y = match self.seqid_y.get(&rec.seqid) {
            Some(y) => *y,
            None => {
                let y = self.seqid_order.len() as i64 * self.cfg.seqid_band;
                self.seqid_y.insert(rec.seqid.clone(), y);
                self.seqid_order.push(rec.seqid.clone());
                y
            }
        };
        b.n += 1;
        b.id.push(fid.as_bytes());
        b.seqid.push(rec.seqid.as_bytes());
        b.source.push(rec.source.as_bytes());
        b.featuretype.push(rec.featuretype.as_bytes());
        b.start.push(rec.start);
        b.end.push(rec.end);
        b.score.push(rec.score.as_bytes());
        b.strand.push(rec.strand.as_bytes());
        b.frame.push(rec.frame.as_bytes());
        b.attributes_blob.push(&rec.attributes_blob);
        b.extra_blob.push(rec.extra.join("\t").as_bytes());
        b.file_order.push(self.file_order);
        b.raw_id.push(raw_id.as_bytes());
        b.occ.push(occ);
        b.id_origin.push(origin.as_bytes());
        b.seqid_y.push(y);

        // gffutils' rule, as the Python builder applies it: a wholly empty
        // value (`ID=`) means the key has no values, while a multi-valued
        // attribute keeps its empty parts (`Parent=x,` -> ["x", ""]).
        let mut multivalued: Option<Vec<&str>> = None;
        for (k, v, idx) in &rec.attributes_pairs {
            if v.is_empty() {
                let mv = multivalued.get_or_insert_with(|| {
                    rec.attributes_pairs
                        .iter()
                        .filter(|(_, _, i)| *i > 0)
                        .map(|(key, _, _)| key.as_str())
                        .collect()
                });
                if !mv.contains(&k.as_str()) {
                    continue;
                }
            }
            let key = match b.a_key_index.get(k.as_str()) {
                Some(i) => *i,
                None => {
                    let i = b.a_key_index.len() as i32;
                    b.a_key_index.insert(k.clone(), i);
                    b.a_keys.push(k.as_bytes());
                    i
                }
            };
            b.a_n += 1;
            b.a_feature_row.push((b.n - 1) as i32);
            b.a_key.push(key);
            b.a_value.push(v.as_bytes());
            b.a_idx.push(*idx);
        }
    }
}

/// `str(getattr(parsed, name))` for the columns a `:name:` key may name.
/// Python-side eligibility (`_options.native_id_spec`) admits only these.
fn column(rec: &Record, name: &str) -> String {
    let coord = |v: Option<i64>| v.map_or_else(|| "None".to_string(), |v| v.to_string());
    match name {
        "seqid" | "chrom" => rec.seqid.clone(),
        "source" => rec.source.clone(),
        "featuretype" => rec.featuretype.clone(),
        "score" => rec.score.clone(),
        "strand" => rec.strand.clone(),
        "frame" => rec.frame.clone(),
        "start" => coord(rec.start),
        "end" | "stop" => coord(rec.end),
        _ => unreachable!("an id_spec column the Python side did not admit: {name}"),
    }
}

/// `ingest._name_bare_gtf_parent`: an AUGUSTUS gene/transcript row whose
/// column 9 is one bare token is read as that row's gene_id/transcript_id.
fn name_bare_gtf_parent(rec: &mut Record, key: &str) {
    if rec.attributes_pairs.len() != 1 {
        return;
    }
    let (token, value, _) = &rec.attributes_pairs[0];
    if !value.is_empty() || token.is_empty() || token.chars().any(|c| " =\"';".contains(c)) {
        return;
    }
    let blob = String::from_utf8_lossy(&rec.attributes_blob);
    if blob.trim().trim_end_matches(';') != token {
        return;
    }
    let token = token.clone();
    rec.attributes_blob = format!("{key} \"{token}\";").into_bytes();
    rec.attributes_pairs = vec![(key.to_string(), token, 0)];
}
