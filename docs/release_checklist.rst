.. _release_checklist--020rc1-release-checklist:

0.2.0rc1 release checklist
==========================

This checklist stops at a tag-ready commit. Tagging, pushing, TestPyPI, PyPI,
GitHub Releases, and security-advisory publication require an explicit release
decision.

.. _release_checklist--1-freeze-the-candidate:

1. Freeze the candidate
-----------------------

- Work from a clean hardening branch and record the full commit SHA.
- Build an sdist and release wheel from that commit into an empty artifact
  directory.

- Record each artifact's SHA-256 and inspect its file list.
- Install the wheel into a clean environment and verify the Python package and
  native extension report exactly ``0.2.0rc1`` and resolve inside that environment.

- Run the installed-wheel parser sentinels, CLI entry point, and small database
  round trip without repository ``PYTHONPATH`` injection.

.. _release_checklist--2-pass-correctness-gates:

2. Pass correctness gates
-------------------------

- Python default and B-tree suites, bounded property suite, parity suite, slow
  suite, Rust tests, lint, formatting, typing, and strict documentation build.

- Minimum supported DuckDB/PyArrow and current dependency environments.
- All five real corpora with the selected duplicate policy, full validation,
  and deterministic feature/relationship signatures.

- No unexpected skips, xfails, parser panics, partial database files, or native
  version mismatch.

.. _release_checklist--3-finish-benchmark-evidence:

3. Finish benchmark evidence
----------------------------

- Complete the parallel cluster audit and then the isolated canonical run.
- Verify input hashes, job-status records, environment provenance, result
  schema, correctness signatures, and full-validation reports.

- Keep Linux canonical, Mac historical, GTF controls, and version-bridge
  results distinct; regenerate documentation from the checked-in result index.

- Re-run an affected canonical row after any source, dependency, input, or
  harness change.

.. _release_checklist--4-preparebut-do-not-publishthe-release-metadata:

4. Prepare—but do not publish—the release metadata
--------------------------------------------------

- Promote the ``0.2.0rc1`` changelog heading to ``0.2.0``, replace ``Unreleased``
  with the actual approved release date, and add the same ``date-released`` to
  ``CITATION.cff``.

- Change security and installation text from candidate language only after the
  public artifact is available.

- Confirm package, Cargo, Python, changelog, citation, and documentation version
  literals agree.

- Produce the proposed tag, artifact hashes, release notes, benchmark run ID,
  test summary, known limitations, and rollback instructions for review.

.. _release_checklist--5-authorization-boundary:

5. Authorization boundary
-------------------------

Only after explicit approval: create and push ``v0.2.0``, let the protected
release workflow build from that tag, verify the trusted-publisher target, and
publish the matching documentation and advisory. Never rebuild artifacts from a
different commit to repair a failed upload; correct the source and create a new
candidate.
