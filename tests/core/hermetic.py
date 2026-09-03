"""Compilation with no environment: the Spec 2 entry point.

`tests/core/compile_harness.py` is the same idea for Spec 1, but it imports
`tools_cwl` from `test_setup`, which runs plugin discovery at import. Spec 2
cannot use it (P25), so this module is its hermetic sibling: the same fourteen-
argument call, against `SYNTHETIC_TOOLS`.

The two are deliberately separate files rather than one file with a `tools=`
parameter. A shared module would have to import `test_setup` for one of its two
callers, and the import is the thing being forbidden.
"""
from typing import Final

from hypothesis import HealthCheck, settings

import sophios.cli
import sophios.compiler
from sophios.utils_graphs import get_graph_reps
from sophios.wic_types import CompilerInfo, StepId, Tools, Yaml, YamlTree

from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS

#: Budgets, per the TDD guide's table. They may be raised for a dispatch run.
#: They may never be lowered to make a failing property pass.
COVERAGE: Final = settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow], deadline=None)
ORACLE: Final = settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
PARTITION: Final = settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow], deadline=None)


def compile_hermetic(yml: Yaml, name: str = 'oracle', *,
                     tools: Tools | None = None,
                     insert_steps_automatically: bool = False,
                     is_root: bool = True) -> CompilerInfo:
    """Compile one in-memory workflow against the synthetic registry.

    `insert_steps_automatically` is named rather than taken as `**options` so a
    typo is a type error instead of a silently ignored setting — the same
    reasoning as `compile_harness.compile_info`.
    """
    compiler_options, graph_settings, tag_paths = sophios.cli.default_compilation_settings()
    compiler_options['insert_steps_automatically'] = insert_steps_automatically
    graph = get_graph_reps(name)
    return sophios.compiler.compile_workflow(
        YamlTree(StepId(name, SYNTHETIC_NS), yml),
        compiler_options, graph_settings, tag_paths,
        [], [graph], {}, {}, {}, {},
        SYNTHETIC_TOOLS if tools is None else tools,
        is_root, relative_run_path=True, testing=True)


def compile_hermetic_cwl(yml: Yaml, name: str = 'oracle', *,
                         tools: Tools | None = None,
                         insert_steps_automatically: bool = False) -> Yaml:
    """Compile one in-memory workflow and return the emitted CWL."""
    info = compile_hermetic(yml, name, tools=tools,
                            insert_steps_automatically=insert_steps_automatically)
    compiled: Yaml = info.rose.data.compiled_cwl
    return compiled


def subworkflow_step(stem: str, subtree: Yaml) -> Yaml:
    """A subworkflow step, shaped as `read_ast_from_disk` shapes one.

    `read_ast_from_disk` splits a `.wic` step into `subtree` (applied before
    compilation) and `parentargs` (applied after) — src/sophios/ast.py:119-125 —
    and `compile_workflow_once` reads both unconditionally
    (src/sophios/compiler.py:492, :512). Building the shape here is what lets a
    partitioned workflow exist without a file on disk, which is what makes P28
    hermetic.
    """
    assert stem.endswith('.wic'), 'get_subkeys recognises a subworkflow by suffix only'
    return {'id': stem, 'subtree': subtree, 'parentargs': {'id': stem}}
