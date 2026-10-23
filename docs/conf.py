from pathlib import Path
import os
import sys
from typing import Dict

from sphinx.application import Sphinx
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
napoleon_preprocess_types = True  # `optional` in a type is a keyword, never a cross-reference
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
    exclude_patterns.append('index.md')
else:
    # landing.md re-includes the landing page for the PDF, whose root is a bare toctree.
    exclude_patterns += ['pdf_index.rst', 'landing.md']


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


# -- The Nextflow support table ----------------------------------------------

# docs/nextflow.md holds this marker where src/sophios/nf_support.py's table is rendered.
NF_SUPPORT_MARKER = '<!-- nf-support-table -->'

# Where a class's fields are written, for a class whose name does not say.
_NF_SUPPORT_PLACES = {
    'WorkflowStep': 'a workflow step',
    'WorkflowStepInput': "an entry of a step's `in`",
    'WorkflowInputParameter': 'a workflow input',
    'InputArraySchema': "a workflow input's array type",
    'WorkflowOutputParameter': 'a workflow output',
    'OutputArraySchema': "a workflow output's array type",
    'CommandInputParameter': 'a tool input',
    'CommandInputArraySchema': "a tool input's array type",
    'CommandLineBinding': 'an `inputBinding`, or an `arguments` entry',
    'CommandOutputParameter': 'a tool output',
    'CommandOutputBinding': 'an `outputBinding`',
    'Dirent': 'an entry of an `InitialWorkDirRequirement` listing',
    'EnvironmentDef': 'an `envDef` entry of an `EnvVarRequirement`',
}


def _nf_support_table(head: str, rows: list[tuple[str, ...]]) -> list[str]:
    columns = head.split(' | ')
    return [f'| {head} |', '|' + '---|' * len(columns), *(f"| {' | '.join(row)} |" for row in rows)]


def nf_support_markdown() -> str:
    """The support table as Markdown: a summary line, then one table per class."""
    from collections import Counter  # pylint: disable=import-outside-toplevel
    from sophios.lang import SUPPORT_MATRIX, Support  # pylint: disable=import-outside-toplevel
    from sophios.nf_support import FIELDS, REQUIREMENT_CLASSES, State  # pylint: disable=import-outside-toplevel

    native = Counter(FIELDS[cwl_class][name].state for cwl_class, rows in SUPPORT_MATRIX.items()
                     for name, row in rows.items() if row.support is Support.NATIVE)
    lines = [
        f'Of the {native.total()} fields Sophios supports natively, the Nextflow target lowers '
        f'{native[State.LOWERED]}, uses {native[State.COMPILE_TIME]} at compile time only, and rejects '
        f'{native[State.REJECTED]}.',
    ]
    requirements = {name for classes in REQUIREMENT_CLASSES.values() for name in classes}
    for cwl_class, rows in FIELDS.items():
        if cwl_class in requirements:
            continue
        place = _NF_SUPPORT_PLACES.get(cwl_class)
        lines += ['', f'### `{cwl_class}`' + (f', {place}' if place else ''), '']
        if (sophios := SUPPORT_MATRIX.get(cwl_class)) is None:
            lines += _nf_support_table('Field | Nextflow | Note', [
                (f'`{name}`', row.state, row.note) for name, row in rows.items()])
        else:
            lines += _nf_support_table('Field | Sophios | Nextflow | Note', [
                (f'`{name}`', sophios[name].support, row.state, row.note) for name, row in rows.items()])
    for level, classes in REQUIREMENT_CLASSES.items():
        kinds = 'Requirement and hint' if level == 'CommandLineTool' else 'Requirement'
        lines += ['', f'### {kinds} classes of a `{level}`', '']
        lines += _nf_support_table('Class | Nextflow | Note', [
            (f'`{name}`', row.state, row.note) for name, row in classes.items()])
        lines += ['', 'Any other class is rejected.']
    for cwl_class, rows in FIELDS.items():
        if cwl_class in requirements:
            lines += ['', f'### `{cwl_class}` fields', '']
            lines += _nf_support_table('Field | Nextflow | Note', [
                (f'`{name}`', row.state, row.note) for name, row in rows.items()])
    return '\n'.join(lines) + '\n'


def _render_nf_support(_app: Sphinx, docname: str, source: list[str]) -> None:
    if docname == 'nextflow':
        if NF_SUPPORT_MARKER not in source[0]:
            raise ValueError(f'docs/nextflow.md has no {NF_SUPPORT_MARKER} for the support table')
        source[0] = source[0].replace(NF_SUPPORT_MARKER, nf_support_markdown())


def setup(app: Sphinx) -> None:
    """Render the Nextflow support table into docs/nextflow.md as Sphinx reads it."""
    app.connect('source-read', _render_nf_support)
