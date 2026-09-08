API reference
=============

Generated from the docstrings in the installed package, so this page cannot
drift from the code. The sibling sites hand-write their reference pages;
gffbase exports 90+ public symbols whose docstrings are themselves executed as
tests, and a hand-written copy of that surface starts going stale the day it is
written.

Name-mangled internals are omitted. Everything shown here is public API and is
covered by the compatibility guarantees in :doc:`migration`.

----

The database
------------

``FeatureDB`` is the handle returned by :func:`gffbase.create_db` and is where
almost every query lives.

.. autoclass:: gffbase.interface.FeatureDB
   :members:
   :undoc-members:
   :show-inheritance:

----

Building a database
-------------------

.. autofunction:: gffbase.create_db.create_db

.. autofunction:: gffbase.ingest.from_file

.. autoclass:: gffbase.ingest.IngestStats
   :members:

----

Features
--------

.. autoclass:: gffbase.feature.Feature
   :members:
   :show-inheritance:

.. autoclass:: gffbase.feature.MultipartFeature
   :members:
   :show-inheritance:

.. autoclass:: gffbase.feature.FeatureSegment
   :members:

.. autoclass:: gffbase.feature.ParsedFeature
   :no-index:

   The immutable parse result the engines hand to the ingest layer. Its fields
   mirror ``Feature``'s and are not repeated here: documenting both made every
   bare reference to ``seqid``, ``start`` or ``end`` ambiguous across the two
   classes, which is a warning under ``-W`` and a coin-flip link for a reader.

----

Parsing
-------

.. autofunction:: gffbase.parser.parse_gff

.. autofunction:: gffbase.parser.parse_bytes

.. autofunction:: gffbase.parser.detect_dialect

.. autofunction:: gffbase.parser.native_available

----

Reading and writing
-------------------

.. autoclass:: gffbase.iterators.DataIterator
   :members:

.. autoclass:: gffbase.gffwriter.GFFWriter
   :members:

.. autofunction:: gffbase.sqlite_export.export_sqlite

----

Merge criteria
--------------

Predicates consumed by ``FeatureDB.merge`` and ``merge_all``.

.. automodule:: gffbase.merge_criteria
   :members:

----

Validation and migration
------------------------

.. automodule:: gffbase.validate
   :members:

.. automodule:: gffbase.migrate
   :members:

----

Exceptions
----------

.. automodule:: gffbase.exceptions
   :members:
   :show-inheritance:

----

Compatibility modules
---------------------

Ported surface-for-surface from ``gffutils`` so that a script that imported
them keeps working. See :doc:`migration` for what is guaranteed.

.. automodule:: gffbase.helpers
   :members:

.. automodule:: gffbase.inspect
   :members:

.. automodule:: gffbase.convert
   :members:

.. automodule:: gffbase.bins
   :members:

----

→ :doc:`usage_gallery` for worked examples of every method · :doc:`migration`
for the gffutils mapping
