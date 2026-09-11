# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# Copyright 2026 Kuan-Hao Chao
# ---------------------------------------------------------------------------
"""Sphinx configuration, in the house style shared with shorkie / LiftOn /
OpenSpliceAI / splam: furo, sphinx-design, sphinx-copybutton, and no custom
CSS at all. Everything visual is stock furo.

One deliberate departure from the siblings: their API pages are hand-written,
this one is generated with autodoc + napoleon. gffbase exports 90+ public
symbols with extensive docstrings that are themselves executed as tests, and a
hand-written reference for that surface starts drifting the day it is written
-- the exact silent-rot failure this project has already had to fix twice.
"""

import os
import sys

sys.path.insert(0, os.path.abspath("../../python"))

project = "GFFBase"
copyright = "2026, Kuan-Hao Chao"
author = "Kuan-Hao Chao"
release = "0.2.0"
version = "0.2.0"

extensions = [
    # Generated API reference -- see the module docstring for why this differs
    # from the sibling sites.
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx.ext.mathjax",
    "sphinx_copybutton",
    "sphinx_design",
]

templates_path = ["_templates"]
exclude_patterns = []
source_suffix = {".rst": "restructuredtext"}
master_doc = "index"

# `make html` runs with -W (warnings as errors) so the published site can never
# quietly degrade. Nothing is suppressed here -- fix the warning instead.

autodoc_member_order = "bysource"
autodoc_typehints = "signature"
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    # Name-mangled internals are not API. The mkdocstrings config this replaces
    # filtered them with `filters: ["!^_"]`; this is the same rule.
    "exclude-members": "__weakref__",
}
napoleon_google_docstring = True
napoleon_numpy_docstring = True

# The docstrings are written in the project's house style, which -- like the
# Markdown the site was converted from -- uses a single backtick for code.
# RST's default role for a single backtick is `title-reference`, which renders
# as italics, so every `order_by` and `FeatureDB` in an autodoc'd docstring came
# out italicised prose rather than code. Sphinx does not warn: it is a valid
# role doing exactly what it is defined to do.
#
# Setting the default role to `code` makes a single backtick mean what the
# docstrings intend, for both the API pages and the hand-written ones. The
# alternative was rewriting several hundred spans across 38 modules.
default_role = "code"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# Don't let the link checker fail the build on sites that block CI user-agents.
linkcheck_ignore = [
    r"https://doi\.org/.*",
    r"https://www\.biorxiv\.org/.*",
    r"https://pypi\.org/.*",
]
linkcheck_timeout = 20

#: The canonical published location. GitHub Pages serves this project repo at
#: a path under the user site, which owns the apex domain.
html_baseurl = "https://khchao.com/gffbase/"

html_theme = "furo"
html_title = "GFFBase"
html_static_path = ["_static"]
html_logo = "_static/logo.svg"
# The siblings ship no favicon; gffbase already has one, so it keeps it.
html_favicon = "_static/favicon.svg"
html_copy_source = False
html_show_sourcelink = False

html_theme_options = {
    "sidebar_hide_name": True,
    "navigation_with_keys": True,
    "source_repository": "https://github.com/Kuanhao-Chao/gffbase/",
    "source_branch": "main",
    "source_directory": "docs/source/",
    "footer_icons": [
        {
            "name": "GitHub",
            "url": "https://github.com/Kuanhao-Chao/gffbase",
            "html": (
                '<svg stroke="currentColor" fill="currentColor" stroke-width="0" '
                'viewBox="0 0 16 16"><path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 '
                '3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37'
                '-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01'
                '1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64'
                '-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 '
                '2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44'
                '1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54'
                '.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.012 8.012 0 0 0 16 '
                '8c0-4.42-3.58-8-8-8z"></path></svg>'
            ),
            "class": "",
        },
    ],
}
