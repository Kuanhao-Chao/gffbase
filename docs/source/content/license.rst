.. _license--license:

License
=======

gffbase is released under the **Apache License, Version 2.0**.

.. code-block:: text

   Copyright 2026 Kuan-Hao Chao

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

The full text is in
`LICENSE <https://github.com/Kuanhao-Chao/gffbase/blob/main/LICENSE>`__ and
ships inside both the wheel and the source distribution.

----

.. _license--what-each-component-is-under:

What each component is under
----------------------------

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Component
     - Terms
   * - gffbase code and documentation
     - Apache-2.0, © 2026 Kuan-Hao Chao
   * - The Rust core (``rust/``)
     - Apache-2.0, same
   * - Vendored test fixtures under ``tests/data/upstream/``
     - Copied from ``gffutils``, **MIT**; ``PROVENANCE.md`` records the source commit and the licence text
   * - DuckDB, PyArrow (runtime dependencies)
     - MIT and Apache-2.0 respectively, installed by pip, not redistributed here
   * - Annotation corpora
     - **Not redistributed.** Each is downloaded from its publisher under that publisher's terms — see :doc:`datasets`

.. _license--in-short:

In short
--------

Apache-2.0 permits commercial use, modification, distribution and private use,
and asks that you preserve the copyright and licence notices and state
significant changes. It also grants an explicit patent licence, which is the
main reason it is used here rather than MIT.

It does **not** grant rights in the annotation data you process, and it comes
with no warranty.
