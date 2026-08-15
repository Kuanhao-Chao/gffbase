# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# Copyright 2026 Kuan-Hao Chao
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ---------------------------------------------------------------------------
"""The attribute mapping, under its compatibility name.

`Attributes` is `gffbase.feature._LazyAttributes` — the same class, not a
wrapper — so a feature's `.attributes` really is an instance of what this
module exports, and `isinstance(f.attributes, Attributes)` holds.

Values are stored as lists. `constants.always_return_list` (default True)
controls whether a single-valued key reads back as `["x"]` or `"x"`:

    >>> from gffbase import constants
    >>> from gffbase.attributes import Attributes
    >>> attr = Attributes()
    >>> attr["Name"] = "gene1"
    >>> attr["Name"]
    ['gene1']
    >>> constants.always_return_list = False
    >>> attr["Name"]
    'gene1'
    >>> constants.always_return_list = True
    >>> attr["Name"]
    ['gene1']

**The toggle applies to `__getitem__` only**, deliberately. Upstream also
routes `values()` and `items()` through it, which means that with the toggle
off, `for k, v in attrs.items()` yields strings where the code below expects
lists — and every consumer that iterates a value then walks it one character
at a time. gffutils' own `_reconstruct` has that fault, rendering `gene1` as
`g,e,n,e,1`. gffbase's write paths (`Feature._format_attributes`,
`FeatureDB.update`, `FeatureDB._write_back`) all iterate `.items()`, so
reproducing it would corrupt data rather than merely differ. Keeping `items()`
and `values()` list-valued is the deviation; it is recorded in
`tests/parity/deviations.toml`.
"""

from __future__ import annotations

from gffbase.feature import _LazyAttributes as Attributes

#: Which mapping class holds a feature's attributes. Upstream allows this to be
#: swapped for an ordinary `dict` to save memory on large databases; here it is
#: the lazy mapping, which is cheaper still because it does not decode column 9
#: until something reads it.
dict_class = Attributes

__all__ = ["Attributes", "dict_class"]
