# Compatibility modules

Modules that exist so a `gffutils` import keeps resolving. They are not
import shims: each symbol has a behavioural test, and every deliberate
difference is declared in `tests/parity/deviations.toml` with the test that
pins it.

Current parity: **86 of 89 symbols (97%)**, zero modules outstanding.

| Module | What it is |
|---|---|
| `gffbase.bins` | UCSC genomic binning. Differentially tested against the oracle over 20,044 comparisons. |
| `gffbase.helpers` | Thirteen functions, including `make_query`, `infer_dialect`, `sanitize_gff_db` and `canonical_transcripts`. |
| `gffbase.constants` | Schema and dialect constants, plus the two documented global toggles. |
| `gffbase.attributes` | The attribute mapping, under its compatibility name. |
| `gffbase.convert` | `to_bed12`. |
| `gffbase.create` | `_DBCreator` and friends, as adapters over `create_db`. |
| `gffbase.inspect` | Summarise a source without building a database. |
| `gffbase.version` | `version`, read from the package rather than from installed metadata. |
| `gffbase.biopython_integration` | `to_seqfeature` / `from_seqfeature`. Needs the `biopython` extra. |
| `gffbase.pybedtools_integration` | `to_bedtool` / `tsses`. Needs the `pybedtools` extra. |
| `gffbase.contrib.plotting` | The `Gene` track renderer. Needs `pybedtools` and `matplotlib`. |

## Two live global toggles

`constants.always_return_list` and `constants.ignore_url_escape_characters`
are read at runtime by live code, not merely exported. The first decides
whether `feature.attributes["ID"]` gives you `["x"]` or `"x"`; the second
turns percent decoding **and** re-encoding off together.

::: gffbase.helpers
    options:
      show_root_heading: true
      members_order: source
      filters:
        - "!^_"

::: gffbase.bins
    options:
      show_root_heading: true
      members_order: source
      filters:
        - "!^_"

::: gffbase.inspect
    options:
      show_root_heading: true
      members_order: source
      filters:
        - "!^_"

::: gffbase.convert
    options:
      show_root_heading: true
      members_order: source
      filters:
        - "!^_"
