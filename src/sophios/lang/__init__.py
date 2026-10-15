"""The Sophios language layer: typed AST, parser, and diagnostics.

Answers "is this a well-formed Sophios document?" without consulting which
tools happen to be installed; name resolution and type checking are separate,
environment-dependent concerns. `parse` and `render` are inverses.
"""
from .cwl import CWL_VERSION, CWL_VERSIONS, CwlVersion
from .diagnostics import Diagnostic, Diagnostics, Locator, Severity, SophiosError
from .error_codes import SophiosErrorCode
from .nodes import (
    CwlRecord,
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
    cwl_record,
)
from ..utils_yaml import Key, Tag
from .parser import Forms, Grammar, ParseResult, parse, step_key
from .render import render, to_json
from .versions import KNOWN_VERSIONS, LANG_VERSION, resolve as resolve_lang_version
from .schema import wic_schema
from .spans import SourceSpan
from .support import SUPPORT_MATRIX, Support

__all__ = [
    'CWL_VERSION',
    'CWL_VERSIONS',
    'CwlVersion',
    'KNOWN_VERSIONS',
    'LANG_VERSION',
    'SUPPORT_MATRIX',
    'SophiosErrorCode',
    'Diagnostic',
    'Diagnostics',
    'Locator',
    'CwlRecord',
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
    'Support',
    'Tag',
    'UnresolvedName',
    'WicSidecar',
    'cwl_record',
    'parse',
    'render',
    'resolve_lang_version',
    'step_key',
    'to_json',
    'wic_schema',
]
