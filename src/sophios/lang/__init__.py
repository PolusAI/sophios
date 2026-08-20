"""The Sophios language layer: typed AST, parser, and diagnostics.

This package is the syntax layer of the specification — it answers "is this a
well-formed Sophios document?" without consulting which tools happen to be
installed. Name resolution and type checking are separate, environment-
dependent concerns.

`parse` and `render` are inverses: rendering a parsed document and parsing it
again reproduces the document it started from.

See design_docs/core-refactor-design.md, Spec 1.
"""
from .diagnostics import Code, Diagnostic, Diagnostics, Severity
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
from .parser import INTERPRETED_STEP_KEYS, TAG_RAW_CWL, ParseResult, parse
from .render import render, to_json
from .spans import SourceSpan

__all__ = [
    'INTERPRETED_STEP_KEYS',
    'TAG_RAW_CWL',
    'Code',
    'Diagnostic',
    'Diagnostics',
    'Document',
    'EdgeDef',
    'EdgeRef',
    'InlineLiteral',
    'InputValue',
    'OpaqueCwl',
    'OutputBinding',
    'ParseResult',
    'RawCwlRef',
    'Severity',
    'SourceSpan',
    'Step',
    'StepKey',
    'UnresolvedName',
    'WicSidecar',
    'parse',
    'render',
    'to_json',
]
