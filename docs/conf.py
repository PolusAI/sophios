from pathlib import Path
import os
import sys
from typing import Dict
# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


# -- Project information -----------------------------------------------------

project = 'Sophios'
copyright = '2023, Axle Informatics'
author = 'Jake Fennick'


# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    'myst_parser',
    'sphinx.ext.napoleon',  # Support google (and numpy) docstring styles
    "sphinx_autodoc_typehints",  # Load AFTER napoleon
    # See https://github.com/agronholm/sphinx-autodoc-typehints/issues/15
    # NOTE: sphinx_autodoc_typehints automatically strips type annotations
    # from the function signature and inserts them into the docstring.
    # This avoids duplication and looks much cleaner. (particularly
    # since type aliases get expanded and the trick below isn't working...)
    # Docstrings should avoid repeating type annotations already present in signatures.
    'sphinx.ext.duration',
    'sphinx.ext.doctest',
    'sphinx.ext.autodoc',
    'sphinx.ext.autosummary',
    'sphinx.ext.intersphinx',
]

intersphinx_mapping = {
    'python': ('https://docs.python.org/3/', None),
    'sphinx': ('https://www.sphinx-doc.org/en/master/', None),
    'numpy': ('https://numpy.org/doc/stable/', None),
}
intersphinx_disabled_domains = ['std']

# See https://myst-parser.readthedocs.io/en/latest/syntax/optional.html#auto-generated-header-anchors
myst_heading_anchors = 6
# Unbelievably, this is not enabled by default.
# See https://github.com/executablebooks/MyST-Parser/blob/master/CHANGELOG.md#0170---2022-02-11

autodoc_default_options = {
    'members': True,
    'member-order': 'bysource',
    'special-members': '__init__',
    'undoc-members': True,
}


# See https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html#confval-autodoc_type_aliases
# See https://www.sphinx-doc.org/en/master/usage/extensions/napoleon.html#confval-napoleon_type_aliases
# The sphinx autodoc documentation claims type aliases defined in wic_types.py
# can be added to autodoc_type_aliases instead of showing their expansions.
# The automatic alias expansion is disabled until it works reliably.
autodoc_type_aliases: Dict[str, str] = {
}
napoleon_use_param = True
napoleon_type_aliases: Dict[str, str] = {
}

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
building_pdf = os.environ.get('SOPHIOS_BUILD_PDF') == '1'

exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']
if building_pdf:
    exclude_patterns.append('index.rst')
else:
    exclude_patterns.append('pdf_index.rst')


# -- Options for HTML output -------------------------------------------------

# The regular documentation site keeps the RTD-facing theme. The PDF build uses
# Sphinx's minimal theme plus a dedicated print stylesheet so the generated
# document reads like a native PDF instead of a captured web page.
if building_pdf:
    html_theme = 'basic'
    html_css_files = ['pdf.css']
else:
    html_theme = 'alabaster'
    html_css_files = []
html_context = {'building_pdf': building_pdf}

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ['_static']
