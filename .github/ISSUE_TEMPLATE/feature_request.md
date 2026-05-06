---
name: Feature request
about: Propose a new public-API surface, performance improvement, or quality-of-life enhancement.
title: "[feature] <short summary>"
labels: ["enhancement", "triage"]
assignees: []
---

<!--
Thanks for the suggestion. Before filing, please:

1. Search existing issues / discussions to avoid duplicates.
2. Check `plans/FUTURE_ROADMAP.md` — your idea may already be on the
   roadmap, possibly with a tier (Immediate / Medium / Moonshot).
3. For "how do I do X?" questions, prefer GitHub Discussions; this
   template is for proposing changes to the library itself.
-->

## Problem statement

<!-- What problem are you trying to solve? What does GFFBase currently
NOT do that you wish it did? Please frame this in terms of the use
case, not the solution — that helps us evaluate alternatives. -->

## Proposed solution

<!-- Concrete API sketch if you have one. Pseudo-code is fine.

For example:
```python
db.children_batched(
    transcript_ids,
    featuretype="exon",
    include_attributes=True,    # NEW: also return the attribute table joined in
    format="arrow",
)
```
-->

## Alternatives considered

<!-- What other approaches did you think about? Why did you settle on
the one above? Existing workarounds you've used? -->

## Impact / motivation

<!-- Who benefits from this change? Performance numbers if relevant,
e.g. "this would let us extract 1.6 M exons in one query instead of
50 000 round-trips". Citations to upstream tooling that has the
feature, if any. -->

## Backwards compatibility

<!-- Does this change any existing behavior? Does it require a major
version bump? Is there a clean migration path for current users? -->

## Tier estimate (optional)

- [ ] **Immediate** — small, self-contained, < 1 day of work.
- [ ] **Medium** — needs design discussion, ~1 week of work.
- [ ] **Moonshot** — research-grade, multi-month, may not fit.

## Are you willing to contribute the implementation?

- [ ] Yes — I'd like to open the PR myself once we've agreed on the design.
- [ ] No — I'm happy to test or review, but won't write the code.
- [ ] Maybe — depends on complexity once we scope it.
