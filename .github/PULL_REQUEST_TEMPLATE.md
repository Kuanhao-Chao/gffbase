<!--
Thanks for the PR! Please read CONTRIBUTING.md if you haven't already.
The maintainers use the checklist below to triage — please leave the
boxes unchecked until each item is genuinely done, not just intended.
-->

## Summary

<!-- One paragraph: what does this change do, and why? -->

## Linked issue

<!-- Either:
       Closes #123
     (if this PR resolves an existing issue), or
       Refs #456
     (if it's part of a larger discussion), or
       N/A — small isolated change
-->

## Type of change

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would change existing behavior)
- [ ] Performance improvement (measured)
- [ ] Documentation only
- [ ] Tooling / CI / chore

## What changed (high level)

<!-- Bullet list of the actual edits, grouped by area. Example:

- `python/gffbase/interface.py`: added the `include_attributes` kwarg to
  `children_batched` and `parents_batched`; threads through
  `_relation_query_batched`.
- `tests/test_batched_api.py`: 4 new tests covering the new flag (True,
  False, default, and the format='polars' branch).
- `docs/cookbooks/machine_learning_workflows.md`: example updated to
  show the new keyword.
-->

-
-
-

## Testing

<!-- How did you verify the change? Paste the relevant `pytest` output
or the specific commands you ran. -->

```text
$ pytest
523 passed, 7 skipped in 30.73s
Required test coverage of 99% reached. Total coverage: 99.19%
```

## Checklist

> Don't tick a box you haven't actually done — failed CI is faster
> than a maintainer asking.

### Code

- [ ] My code follows the existing style (`ruff check` clean,
      `ruff format --check` clean).
- [ ] I added the **Apache-2.0 + author header** at the top of any
      new `.py` or `.rs` file (copy from an existing file in the
      same directory).
- [ ] I have not introduced any new external runtime dependency
      without an issue discussing it first.
- [ ] My commits are atomic and have descriptive messages
      (Conventional Commits style preferred).

### Tests

- [ ] I added tests that cover the new behavior, or a regression test
      that fails on `main` and passes on this branch.
- [ ] I ran the full suite locally: `pytest` (passes, ≥ 99 % coverage).
- [ ] If I touched the spatial-index path, I also ran
      `GFFBASE_TEST_DISABLE_RTREE=1 pytest` (B-tree fallback path).

### Documentation

- [ ] If this changes a public API, I updated the docstring.
- [ ] If this changes drop-in compatibility with `gffutils`, I added a
      note in `MIGRATION.md`.
- [ ] If I touched anything under `docs/`, I ran
      `mkdocs build --strict` and it's clean.
- [ ] If this is a perf change, I have a benchmark in `benchmarks/`
      that documents the before/after delta.

### Risk

- [ ] This change is backwards-compatible at the public API level.
      *(If unchecked: I have explained the migration path in the
      Summary above.)*
- [ ] I have considered the failure mode on the larger corpora
      (GENCODE / RefSeq / MANE / CHESS 3), not just on the unit-test
      fixtures.

## Anything else reviewers should know

<!-- Tradeoffs, follow-up work, areas you'd specifically like a second
opinion on, etc. -->
