---
name: Bug report
about: Report a defect — something GFFBase does that disagrees with its documented behavior or with `gffutils` parity.
title: "[bug] <short summary>"
labels: ["bug", "triage"]
assignees: []
---

<!--
Thanks for taking the time to file a bug. The fields below are the
information we need to reproduce the issue. If any of them is genuinely
not applicable, write "N/A" with a one-line reason — empty fields make
the report take longer to triage.
-->

## Summary

<!-- One sentence: what went wrong, in your own words. -->

## Environment

| | |
|---|---|
| **OS** | <!-- e.g. macOS 14.4 (Apple Silicon), Ubuntu 22.04 (x86_64), Windows 11 --> |
| **Python version** | <!-- output of `python --version` --> |
| **GFFBase version** | <!-- output of `python -c "import gffbase; print(gffbase.__version__)"` --> |
| **Install method** | <!-- pip install gffbase / pip install -e . / maturin develop --release --> |
| **DuckDB version** | <!-- output of `python -c "import duckdb; print(duckdb.__version__)"` --> |
| **PyArrow version** | <!-- output of `python -c "import pyarrow; print(pyarrow.__version__)"` --> |
| **Native extension built?** | <!-- output of `python -c "from gffbase import native_available; print(native_available())"` --> |

## Minimal reproducible example

<!--
PASTE THE SMALLEST CODE BLOCK THAT REPRODUCES THE BUG.

It should be:
- Self-contained: anyone can copy-paste and run it.
- Minimal: no unrelated business logic.
- Deterministic: doesn't depend on a specific corpus we don't have.

If the bug only reproduces on a specific real-world file (e.g. a
RefSeq release), describe the file in enough detail that we can
fetch it (URL + checksum is best). Do NOT attach gigabyte-scale
files — link to the canonical source.
-->

```python
import gffbase

# ... your reproduction here ...
```

If the bug requires a GFF/GTF input, please attach a **minimal**
trimmed-down `.gff3` / `.gtf` snippet (10-20 lines is usually
enough). Either inline as a fenced block:

```text
##gff-version 3
chr1	src	gene	1	1000	.	+	.	ID=g1
...
```

…or as a file attachment if it's larger than a few dozen lines.

## What you expected

<!-- The behavior you believe is correct, with a reference if applicable
(NCBI GFF3 spec section, gffutils API behavior, GFFBase docstring, etc.). -->

## What actually happened

<!-- The full traceback, or the wrong output, verbatim. Use a fenced
block. Don't truncate Python tracebacks — the lines above the final
exception are usually what we need. -->

```text
<paste output here>
```

## Additional context

<!-- Anything else: have you tried 0.0.1 vs. 0.1.0? Does it reproduce
on a different OS? Did it appear after a specific commit? Are there
related open issues? -->
