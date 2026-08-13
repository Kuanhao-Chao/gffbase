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
"""Compatibility vs standards mode.

Validation used to conflate two independent questions -- *which rules apply*
and *what a violation does*. Separating them is what makes gffbase usable as a
drop-in again without giving up strict checking.

Why it mattered: `rust/src/validate.rs` validated to the NCBI GFF3
specification unconditionally, but `create_db()` is the compatibility entry
point. Real annotation files break that spec routinely, so gffbase refused six
of the twenty-three upstream fixtures gffutils reads -- including
`FBgn0031208.gff`, gffutils' own canonical fixture. Strict validation is a
genuinely useful feature; it just cannot be the behaviour of the drop-in API.

The two axes:

===============  ==========================  =====================================
Axis             Values                      Meaning
===============  ==========================  =====================================
``validation``   ``gffutils`` / ``ncbi``     which rules apply
``on_error``     ``raise`` / ``warn``        what a rejection does
===============  ==========================  =====================================

:data:`MODES` maps the user-facing ``mode`` onto both.
"""

from __future__ import annotations

import warnings
from typing import NamedTuple

#: Rule sets.
VALIDATION_GFFUTILS = "gffutils"
VALIDATION_NCBI = "ncbi"
VALIDATION_PROFILES = (VALIDATION_GFFUTILS, VALIDATION_NCBI)

#: Rejection dispositions.
ON_ERROR_RAISE = "raise"
ON_ERROR_WARN = "warn"
ON_ERROR_VALUES = (ON_ERROR_RAISE, ON_ERROR_WARN)

#: User-facing modes.
MODE_COMPAT = "compat"
MODE_STRICT = "strict"
MODES = (MODE_COMPAT, MODE_STRICT)


class ResolvedMode(NamedTuple):
    """The settled (mode, validation, on_error) triple."""

    mode: str
    validation: str
    on_error: str

    @property
    def raises(self) -> bool:
        return self.on_error == ON_ERROR_RAISE


#: What each mode selects.
_MODE_DEFAULTS = {
    MODE_COMPAT: (VALIDATION_GFFUTILS, ON_ERROR_RAISE),
    MODE_STRICT: (VALIDATION_NCBI, ON_ERROR_RAISE),
}

_UNSET = object()


def resolve_mode(
    mode: str = MODE_COMPAT,
    *,
    validation: str | None = None,
    on_error: str | None = None,
    strict=_UNSET,
) -> ResolvedMode:
    """Settle the mode axes, honouring the deprecated ``strict`` boolean.

    ``strict`` was the old single switch and meant *error disposition*, not
    *rule set*. It keeps working for one deprecation cycle:
    ``strict=True`` -> ``on_error="raise"``, ``strict=False`` ->
    ``on_error="warn"``.

    Passing both ``strict`` and ``on_error`` raises ``TypeError``. That is an
    argument conflict rather than a data problem, which is why it is the one
    error here that is not a ``ValueError``.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}; got {mode!r}")

    default_validation, default_on_error = _MODE_DEFAULTS[mode]

    if strict is not _UNSET:
        if on_error is not None:
            raise TypeError(
                "pass either strict= or on_error=, not both; "
                "strict is deprecated in favour of on_error"
            )
        warnings.warn(
            "strict= is deprecated; use on_error='raise' or on_error='warn'. "
            "It controls the error disposition, not the rule set -- for the "
            "rule set use mode='compat' / mode='strict'.",
            DeprecationWarning,
            stacklevel=3,
        )
        on_error = ON_ERROR_RAISE if strict else ON_ERROR_WARN

    validation = validation if validation is not None else default_validation
    on_error = on_error if on_error is not None else default_on_error

    if validation not in VALIDATION_PROFILES:
        raise ValueError(f"validation must be one of {VALIDATION_PROFILES}; got {validation!r}")
    if on_error not in ON_ERROR_VALUES:
        raise ValueError(f"on_error must be one of {ON_ERROR_VALUES}; got {on_error!r}")

    return ResolvedMode(mode=mode, validation=validation, on_error=on_error)
