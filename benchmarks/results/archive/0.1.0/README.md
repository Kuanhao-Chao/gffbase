# 0.1.0-era artifacts

Everything here is a **historical record, not a current result.** It is kept
so the 0.1.0 numbers that were published can still be inspected, and so the
claims 0.2.0 corrects can be checked against what they corrected.

Do not cite any of it. Specifically:

* `06_mega.json` contains **one** of the five corpora it was named as the
  provenance for. Each `--only` run of the old harness rewrote the whole file
  and deleted the others; `benchmarks/common.py::merge_results` now merges by
  corpus key so that cannot happen again.
* The `06_mega*.log` files are five separate runs spanning two different
  GENCODE releases (v45 and v49). The published table was stitched together
  from them by hand, which is how a v45 spatial-throughput number came to be
  printed in a row labelled v49.
* `05_vectorized.json` was assembled by hand rather than emitted by the
  script — it lacks the `label` / `exit_code` / `timed_out` / `peak_rss_bytes`
  keys the harness always writes. Its 50 000-anchor `gffbase_loop` value of
  642 s is `64.2 × 10`, an arithmetic estimate of a run that was killed before
  it finished.
* `legacy_full.log` and `legacy_rss.log` came from the retired `bench/` tree.
  They hold the one uncapped legacy GENCODE v45 GTF measurement (3,582 s), the
  anchor the old "≥ 2 hr 30 min" extrapolation was hung on.
* `RELEASE_NOTES_v0.1.0.md` describes a release that is being yanked from
  PyPI, and repeats the tables above.

Current results live in `benchmarks/results/`, are produced by one run of one
harness, and carry a full `environment` block recording the hardware, every
package version, and the git commit they were measured at.
