.. _release_checklist--020rc1-release-checklist:

0.2.0 release checklist
=======================

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
  native extension report exactly ``0.2.0`` and resolve inside that environment.

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

What a published number must rest on is one isolated canonical run of the five
corpora, measured on the installed artifact and admitted by the schema-v3
evidence contract. That contract is not advisory: it refuses a row whose
validation was sampled rather than exhaustive, refuses a run whose tree was
dirty or whose rows disagree about the commit, and refuses a speedup unless both
engines' correctness signatures match.

- Run all five corpora in one invocation, on the candidate wheel, with no
  repository ``PYTHONPATH``, exhaustive validation, and the canonical
  ``no-infer`` GTF arm. Write the databases to a local filesystem: the default
  output directory is inside the repository, and on network storage the ingest
  numbers measure the network.

- Verify input hashes, environment provenance, result schema, correctness
  signatures, and full-validation reports. A censored comparator is a result:
  it records its cap, reports no wall time, and publishes no ratio.

- Keep Linux and Mac measurements distinct. The macOS artifact is retained
  byte-for-byte under its own name and is never the target of a write;
  regenerate the documentation from the platform artifact.

- Re-run an affected canonical row after any source, dependency, input, or
  harness change.

The 36-job cluster campaign — thread scaling, GTF-inference controls, and the
0.1.0 version bridge — is deeper evidence and remains supported, but it is not
what a candidate has to clear. It measures roughly seven times the work for the
same five published rows, and the parts a release depends on are exactly the
five this section requires. Record which of the two produced the numbers being
published; do not present one as the other.

.. warning::

   **The campaign will not start under a campaign root on setgid storage.**
   ``preflight`` creates each run directory with ``os.mkdir(mode=0o700)`` and
   then requires the result to be exactly mode ``0700``. On a parent directory
   carrying the setgid bit -- the normal arrangement for group-shared cluster
   storage -- the kernel returns ``02700``, and the run aborts with *new run
   directory is not a private mode-0700 directory*. ``02700`` is exactly as
   private as ``0700``: setgid on a directory governs group inheritance and
   grants no access, which is why ``safe_io``'s ancestor allowlist already
   accepts it. The two checks disagree, and ``preflight`` is the stricter one.

   Until they are reconciled, point ``--campaign-root`` at a directory whose
   parent is not setgid (``chmod g-s`` on the parent, or a path under ``/tmp``).
   This does not affect the five-corpus canonical run, which does not go through
   ``preflight``.

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
