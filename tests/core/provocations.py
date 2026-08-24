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


def _compile_minimal(yml: dict) -> None:
    """Compile one in-memory workflow with the real tool registry.

    Imported lazily, like everything else here: this module is imported by the
    meta-test to enumerate codes, and pulling in the compiler and the tool
    registry to do that would make a cheap import expensive.
    """
    from .compile_harness import compile_info  # pylint: disable=import-outside-toplevel

    compile_info(yml, 'provoke')


def _provoke_unresolved_input() -> None:
    _compile_minimal({'steps': [{'id': 'touch', 'in': {'filename': 'not_a_workflow_input'}}]})


def _provoke_missing_required_input() -> None:
    # CE-01's exact shape: a null !ii on a non-nullable input.
    _compile_minimal({'steps': [{'id': 'touch', 'in': {'filename': {'wic_inline_input': None}}}]})


def _provoke_subworkflow_invalid() -> None:
    from typing import cast  # pylint: disable=import-outside-toplevel

    import sophios.ast  # pylint: disable=import-outside-toplevel
    from jsonschema import Draft202012Validator  # pylint: disable=import-outside-toplevel
    from sophios.wic_types import StepId, YamlTree  # pylint: disable=import-outside-toplevel

    class _RefusesEverything:  # pylint: disable=too-few-public-methods
        def validate(self, _tree: object) -> None:
            raise ValueError('provoked')

    tree = YamlTree(StepId('provoke.wic', 'global'), {'steps': [{'id': 's'}]})
    # cast: the raise path only needs .validate; a real validator that always
    # refuses would drag schema construction into a provocation.
    sophios.ast.read_ast_from_disk('.', tree, {}, {}, cast(Draft202012Validator, _RefusesEverything()), False)


def _provoke_script_argument_mismatch() -> None:
    from types import ModuleType  # pylint: disable=import-outside-toplevel

    from sophios.python_cwl_adapter import check_args_match_inputs  # pylint: disable=import-outside-toplevel

    module = ModuleType('provoked_script')
    module.inputs = {'expected': int}  # type: ignore[attr-defined]
    check_args_match_inputs(module, {'unexpected': 1}, check=True)


def _provoke_container_engine_unavailable() -> None:
    from unittest import mock  # pylint: disable=import-outside-toplevel

    from sophios import post_compile  # pylint: disable=import-outside-toplevel

    with mock.patch.object(post_compile.sub, 'run', side_effect=FileNotFoundError('docker')):
        post_compile.verify_container_engine_config('docker', False)


def _provoke_missing_input_file() -> None:
    import tempfile  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    from sophios import post_compile  # pylint: disable=import-outside-toplevel

    with tempfile.TemporaryDirectory() as root:
        post_compile.stage_input_files({'f': {'class': 'File', 'location': 'definitely_absent.txt'}},
                                       Path(root), root, throw=True)


COMPILED.update({
    Code.UNRESOLVED_INPUT: _provoke_unresolved_input,
    Code.MISSING_REQUIRED_INPUT: _provoke_missing_required_input,
    Code.SUBWORKFLOW_INVALID: _provoke_subworkflow_invalid,
    Code.SCRIPT_ARGUMENT_MISMATCH: _provoke_script_argument_mismatch,
    Code.CONTAINER_ENGINE_UNAVAILABLE: _provoke_container_engine_unavailable,
    Code.MISSING_INPUT_FILE: _provoke_missing_input_file,
})
