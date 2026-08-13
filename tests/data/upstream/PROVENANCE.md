# Upstream test corpus — provenance and license

Every file in this directory is copied **verbatim** from the `gffutils` test
suite and is used here as a differential-correctness oracle: the same input is
run through both libraries and the results compared.

## Source

```text
project:    gffutils
repository: https://github.com/daler/gffutils
commit:     6b84330f472dd2b4c69e36f319da7ade95bd5961
describe:   v0.13-33-g6b84330
declared:   0.14
path:       gffutils/test/data/  (and gffutils/test/attr_test_cases.py)
```

## License

`gffutils` is distributed under the MIT License:

```text
The MIT License (MIT)

Copyright (c) 2013 Ryan Dale

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

MIT is permissive and compatible with gffbase's Apache-2.0 licensing; the
notice above satisfies its attribution requirement. These files are **data**,
not source, and gffbase's own license does not apply to them.

## Why these files

Each was picked because it pins a behaviour a successor can get wrong. Grouped
by what they exercise:

| File | What it pins |
|---|---|
| `FBgn0031208.gff` / `.gtf` | The canonical small fixture; same gene in both dialects |
| `gencode-v19.gtf` | GENCODE GTF with `##description:` directives |
| `ensembl_gtf.txt` | Ensembl GTF conventions |
| `wormbase_gff2.txt`, `wormbase_gff2_alt.txt` | GFF2 with spaced semicolons and semicolons *inside* quotes |
| `jgi_gff2.txt` | GFF2 with unquoted numeric values |
| `ncbi_gff3.txt` | Repeated IDs across different featuretypes — malformed GFF3 that the oracle refuses to load with default settings |
| `hybrid1.gff3` | Embedded `##FASTA` section; parsing must stop there |
| `glimmer_nokeyval.gff3` | Attributes with no key/value separator |
| `gms2_example.gff3` | GeneMarkS-2 output |
| `keyval_sep_in_attrs.gff` | `=` inside an attribute *value* |
| `mouse_extra_comma.gff3` | Trailing comma in a multi-valued attribute |
| `nonascii` | Non-ASCII (U+2212) inside attribute values |
| `sharr.gtf` | Coordinates past `MAX_CHROM_SIZE`, where UCSC binning degenerates |
| `keep-order-test.gtf` | Attribute-order preservation |
| `synthetic.gff3` | The merge fixture: 18 features, 13 overlapping |
| `random-chr.gff` | `:` and `@` inside IDs; same-type repeated IDs |
| `issue167.gff` | Space-delimited GFF with `start=0` |
| `issue174.gtf` | Human BRCA2 exons from GRCh38.p13 |
| `issue_197.gff` | EVM contig annotations |
| `unsanitized.gff` | Rows where `start > end` |
| `gff_example1.gff3` / `.gff3.gz` | Identical content plain and gzipped |
| `c_elegans_WS199_*` | GFF2-ish WormBase records; annotation-only lines |
| `F3-unique-3.v2.gff` | SOLiD `##solid-gff-version` directive |
| `intro_docs_example.gff` | The example used in the upstream docs |
| `dm6-chr2L.fa`, `c_elegans_WS199_dna_shortened.fa` | FASTA for `Feature.sequence()` |
| `attr_test_cases.py` | Data-only table of attribute-string parse cases |

## Deliberately not copied

`dmel-all-no-analysis-r5.49_50k_lines.gff` (9 MB) is excluded to keep the sdist
small. It is the best available fixture for genuine **discontinuous
(multipart)** features — it contains 345 same-id/same-seqid/same-source/
same-type/same-strand segment runs — so the multipart tests read it from a
gffutils checkout when one is available:

```bash
export GFFBASE_GFFUTILS_DATA=/path/to/gffutils/gffutils/test/data
pytest -m parity
```

Tests that need it skip cleanly when the variable is unset.

## Regenerating

```bash
python tools/gen_parity_manifest.py     # refresh the API manifest
```

Re-copy the data files only when deliberately re-pinning the oracle to a new
commit; update the commit hash above in the same change.
