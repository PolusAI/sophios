"""The Sophios language layer: typed AST, parser, and diagnostics.

This package is the syntax layer of the specification — it answers "is this a
well-formed Sophios document?" without consulting which tools happen to be
installed. Name resolution and type checking are separate, environment-
dependent concerns.

`parse` and `render` are inverses. That claim lives in exactly one place —
the round-trip property in `tests/core/test_lang_render.py` — and this line
is a pointer to it, not a second copy that can drift.

See design_docs/core-refactor-design.md, Spec 1.
"""
from .cwl import CWL_VERSION, CWL_VERSIONS, CwlVersion
from .diagnostics import Code, Diagnostic, Diagnostics, Severity, SophiosError
from .nodes import (
    Document,
    EdgeDef,
    EdgeRef,
    InlineLiteral,
    InputValue,
    OpaqueCwl,
    OutputBinding,
    RawCwlRef,
    Step,
    StepKey,
    UnresolvedName,
    WicSidecar,
)
from ..utils_yaml import Key, Tag
from .parser import Forms, Grammar, ParseResult, parse
from .render import render, to_json
from .versions import KNOWN_VERSIONS, LANG_VERSION, resolve as resolve_lang_version
from .schema import wic_schema
from .spans import SourceSpan

__all__ = [
    'CWL_VERSION',
    'CWL_VERSIONS',
    'CwlVersion',
    'KNOWN_VERSIONS',
    'LANG_VERSION',
    'Code',
    'Diagnostic',
    'Diagnostics',
    'Document',
    'EdgeDef',
    'EdgeRef',
    'Forms',
    'Grammar',
    'InlineLiteral',
    'InputValue',
    'Key',
    'OpaqueCwl',
    'OutputBinding',
    'ParseResult',
    'RawCwlRef',
    'Severity',
    'SophiosError',
    'SourceSpan',
    'Step',
    'StepKey',
    'Tag',
    'UnresolvedName',
    'WicSidecar',
    'parse',
    'render',
    'resolve_lang_version',
    'to_json',
    'wic_schema',
]
