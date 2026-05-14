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
# import os
# import sys
# sys.path.insert(0, os.path.abspath('.'))


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
    # TODO: Remove all of the duplicated type annotations in the docstrings.
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
# However, I can't seem to get it to work.
# TODO: Consider removing all type aliases in favor of classes.
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
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']


# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = 'alabaster'

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ['_static']
