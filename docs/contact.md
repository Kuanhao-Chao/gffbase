---
title: Contact
---

# Contact

## Bugs, questions and feature requests

**[Open a GitHub issue](https://github.com/Kuanhao-Chao/gffbase/issues)** —
that is the fastest route and it leaves an answer other people can find.

Please include:

- The **exact command or code** that fails, small enough to run.
- The **full traceback**, not just the last line.
- `gffbase.__version__`, and whether `gffbase.native_available()` is `True`.
- Your Python version and platform.
- Which **annotation** it happened on, and ideally a few lines that reproduce it.

For anything data-shaped, the smallest GFF3 that reproduces the problem is
worth more than a description of the file. A handful of lines is usually
enough.

```python
import gffbase, sys
print(gffbase.__version__, gffbase.native_available(), sys.version, sys.platform)
```

## Security

Please **do not** open a public issue for a vulnerability. The reporting
process, the supported versions and what is in scope are in
[the security policy](security-policy.md).

## Author

**Kuan-Hao Chao** — <kuanhao.chao@gmail.com> ·
[khchao.com](https://khchao.com/) ·
[ORCID 0000-0003-3026-4266](https://orcid.org/0000-0003-3026-4266)

## Contributing

Pull requests are welcome. [`CONTRIBUTING.md`](contributing.md) covers the
development setup, the test suite, and what a reviewable PR looks like.

---

## Other tools

| Tool | What it does |
| --- | --- |
| [**LiftOn**](https://khchao.com/LiftOn/) | Accurate annotation lift-over combining DNA and protein alignment |
| [**OpenSpliceAI**](https://khchao.com/OpenSpliceAI/) | An open, retrainable reimplementation of SpliceAI |
| [**Splam**](https://khchao.com/splam/) | Splice-junction recognition and alignment cleanup |
| [**Shorkie**](https://khchao.com/shorkie/) | Sequence-to-expression modelling in budding yeast |
