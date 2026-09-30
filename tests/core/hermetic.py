"""Compilation with no environment.

`tests/core/compile_harness.py` is the same idea, but it imports `tools_cwl`
from `test_setup`, which runs plugin discovery at import. The hermeticity
property forbids that here, so this module is its sibling: the same fourteen-
typed call, against `SYNTHETIC_TOOLS`.

The two are deliberately separate files rather than one file with a `tools=`
parameter. A shared module would have to import `test_setup` for one of its two
callers, and the import is the thing being forbidden.
"""
from pathlib import Path
from typing import Final

import yaml

import sophios.cli
import sophios.compiler
from sophios.input_output import NoAliasDumper
from sophios.utils_graphs import get_graph_reps
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import SourceBundle
from sophios.ir.resolve import RegistrySnapshot
from sophios.lang import Document, ParseResult, parse
from sophios.wic_types import Tools, Yaml

from .budgets import budget
from .synthetic_tools import SYNTHETIC_TOOLS

#: Budgets, per the TDD guide's table. They may be raised for a dispatch run.
#: They may never be lowered to make a failing property pass.
COVERAGE: Final = budget(500)
ORACLE: Final = budget(100)
PARTITION: Final = budget(50)


def _documents(*results: ParseResult) -> tuple[Document, ...]:
    """Each parsed document and the implementation bodies written inside it."""
    documents = []
    for result in results:
        if result.document is None:
            continue
        documents.append(result.document)
        documents.extend(_implementation_bodies(result.document))
    return tuple(documents)


def _implementation_bodies(document: Document) -> tuple[Document, ...]:
    bodies = document.sidecar.implementations if document.sidecar is not None else ()
    return tuple(body for _name, body in bodies) + tuple(
        nested for _name, body in bodies for nested in _implementation_bodies(body))


def bundle(yml: Yaml, name: str, tools: Tools) -> SourceBundle:
    """`yml` as the file door would read it, each `subtree` the file it stands for.

    A test-side model of the loader's shape (see `subworkflow_step`): the body
    becomes a registry entry keyed by the step's stem, `parentargs` the step's
    own keys, and every document's `wic: lang_version:` a pin.
    """
    workflows: dict[tuple[str, str], ParseResult] = {}

    def parsed(document: Yaml, stem: str) -> ParseResult:
        steps = []
        for step in document.get('steps', []):
            if isinstance(step, dict) and 'subtree' in step:
                child = Path(step['id']).stem
                workflows[('global', child)] = parsed(step['subtree'], child)
                step = {'id': step['id'], **step.get('parentargs', {})}
            steps.append(step)
        return parse(yaml.dump({**document, 'steps': steps}, Dumper=NoAliasDumper,
                               sort_keys=False, line_break='\n', indent=2), f'{stem}.wic')

    root = parsed(yml, name)
    pins = tuple(str(entries['lang_version'])
                 for document in _documents(root, *workflows.values())
                 if document.sidecar is not None
                 for entries in [dict(document.sidecar.entries)] if 'lang_version' in entries)
    return SourceBundle(root, name, RegistrySnapshot.from_tools(tools, workflows=workflows), pins)


def compile_hermetic(yml: Yaml, name: str = 'oracle', *,
                     tools: Tools | None = None,
                     insert_steps_automatically: bool = False,
                     is_root: bool = True) -> CompilationResult:
    """Compile one in-memory workflow against the synthetic registry.

    `insert_steps_automatically` is named rather than taken as `**options` so a
    typo is a type error instead of a silently ignored setting — the same
    reasoning as `compile_harness.compile_info`.
    """
    compiler_options, graph_settings, tag_paths = sophios.cli.default_compilation_settings()
    compiler_options['insert_steps_automatically'] = insert_steps_automatically
    graph = get_graph_reps(name)
    del is_root
    return sophios.compiler.compile_source(
        bundle(yml, name, SYNTHETIC_TOOLS if tools is None else tools),
        compiler_options, graph_settings, tag_paths,
        relative_run_path=True, testing=True, graph_target=graph)


def compile_production(yml: Yaml, name: str = 'binding', *,
                       tools: Tools | None = None,
                       is_root: bool = True) -> CompilationResult:
    """Compile with user-facing progress enabled (``testing=False``)."""
    compiler_options, graph_settings, tag_paths = sophios.cli.default_compilation_settings()
    del is_root
    return sophios.compiler.compile_source(
        bundle(yml, name, SYNTHETIC_TOOLS if tools is None else tools),
        compiler_options, graph_settings, tag_paths,
        relative_run_path=True, testing=False, graph_target=get_graph_reps(name))


def compile_hermetic_cwl(yml: Yaml, name: str = 'oracle', *,
                         tools: Tools | None = None,
                         insert_steps_automatically: bool = False) -> Yaml:
    """Compile one in-memory workflow and return the emitted CWL."""
    info = compile_hermetic(yml, name, tools=tools,
                            insert_steps_automatically=insert_steps_automatically)
    compiled: Yaml = info.artifact.cwl
    return compiled


def subworkflow_step(stem: str, subtree: Yaml) -> Yaml:
    """A subworkflow step: its body in `subtree`, its own keys in `parentargs`.

    `bundle` turns the body into the registry entry a file on disk would be.
    Building the shape here is what lets a partitioned workflow exist without
    a file on disk, which is what makes partition independence hermetic.
    """
    assert stem.endswith('.wic'), 'a subworkflow step is recognised by its .wic suffix only'
    return {'id': stem, 'subtree': subtree, 'parentargs': {}}
