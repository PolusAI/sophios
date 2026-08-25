"""One attack per diagnostic code — the registry behind the provocation rule.

A diagnostic that cannot be provoked is dead on arrival (the #382 cycle's
UNKNOWN_TAG sat unreachable for days), so every `Code` member must appear in
exactly one tier here, and the meta-test in `test_lang_parser.py` fails the
build for any member that does not.

Two tiers, because the codes have two habitats. PARSE codes fire through
`parse()` alone and their provocations are source strings. COMPILED codes fire
through the compiler or its helpers; their provocations are zero-arg callables
that must raise `SophiosError` carrying the code, importing what they need
lazily so this module stays cheap to import. A branch that adds a `Code`
extends this registry in the same commit, or the meta-test says so.
"""
from collections.abc import Callable
from typing import Final

from sophios.lang.diagnostics import Code

#: Codes provoked through `parse()` alone: source text in, diagnostic out.
PARSE: Final[dict[Code, str]] = {
    Code.INVALID_YAML: 'steps:\n  - [unclosed\n',
    Code.NOT_A_MAPPING: '- just\n- a list\n',
    Code.EXPECTED_MAPPING: 'steps: 3\n',
    Code.EXPECTED_SEQUENCE: 'steps:\n- id: s\n  out: 3\n',
    Code.EXPECTED_SCALAR: 'steps:\n  ? [a, b]\n  : {}\n',
    Code.MISSING_STEP_ID: 'steps:\n- {a: 1, b: 2}\n',
    Code.EMPTY_STEP_ID: "steps:\n- id: ''\n",
    Code.MALFORMED_WIC_STEP_KEY: 'wic:\n  steps:\n    nope:\n      x: 1\n',
    Code.UNKNOWN_TAG: 'top: !foo bar\n',
    Code.DUPLICATE_KEY: 'steps:\n- id: s\n  in:\n    f: !ii a\n    f: !ii b\n',
    Code.RECURSIVE_ALIAS: 'top: &a [*a]\n',
}

#: Codes provoked through the compiler or its helpers. Callables raise
#: `SophiosError` carrying the code. Extended by the branches that add the
#: codes; empty here because this branch declares no compile-phase codes.
COMPILED: Final[dict[Code, Callable[[], object]]] = {}
