"""Compilation with no environment: compile_harness compiles against the registry test_setup discovers, so this
sibling compiles against SYNTHETIC_TOOLS."""
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

import sophios.compiler
from sophios.input_output import NoAliasDumper
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import SourceBundle
from sophios.ir.resolve import RegistrySnapshot
from sophios.lang import Document, ParseResult, parse
from sophios.wic_types import Tools, Yaml

from .budgets import budget
from .synthetic_tools import SYNTHETIC_TOOLS

#: Budgets; raise them for a dispatch run, never lower one to make a property pass.
COVERAGE: Final = budget(500)
ORACLE: Final = budget(100)
PARTITION: Final = budget(50)

#: The oracle suite: test files whose properties compile against SYNTHETIC_TOOLS alone, so the weekly property lane
#: deepens them on a bare hosted runner.
ORACLE_FILES: Final = (
    'tests/core/test_predicates.py',
    'tests/core/test_reference_compatibility.py',
    'tests/core/test_canonical_path.py',
    'tests/core/test_emit.py',
    'tests/core/test_resolve.py',
    'tests/core/test_infer_phase.py',
    'tests/core/test_edge_binding.py',
)


@contextmanager
def network_refused() -> Iterator[list[str]]:
    """Refuse every name lookup and connection, yielding the attempts so a caller can assert none; a context manager,
    since Hypothesis rejects function-scoped fixtures under @given."""
    attempts: list[str] = []

    def refuse(kind: str) -> Any:
        def refused(*args: Any, **_kwargs: Any) -> Any:
            attempts.append(f'{kind} {args[1:] if kind != "lookup" else args[:1]}')
            raise OSError(f'network refused: {kind}')
        return refused

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, 'getaddrinfo', refuse('lookup'))
        patch.setattr(socket.socket, 'connect', refuse('connect'))
        patch.setattr(socket.socket, 'connect_ex', refuse('connect_ex'))
        yield attempts


def _documents(*results: ParseResult) -> tuple[Document, ...]:
    return tuple(found for result in results if result.document is not None
                 for found in (result.document, *_implementation_bodies(result.document)))


def _implementation_bodies(document: Document) -> tuple[Document, ...]:
    bodies = document.sidecar.implementations if document.sidecar is not None else ()
    return tuple(body for _name, body in bodies) + tuple(
        nested for _name, body in bodies for nested in _implementation_bodies(body))


def bundle(yml: Yaml, name: str, tools: Tools) -> SourceBundle:
    """`yml` as the file door reads it: each `subtree` a registry entry under its step's stem, `parentargs` the step's
    keys, and each `wic: lang_version:` a pin."""
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
                     inference_strict: bool = False) -> CompilationResult:
    """Compile one in-memory workflow against the synthetic registry; options are named so a typo is a type error."""
    inference = replace(sophios.compiler.CompileSettings().inference,
                        insert_steps_automatically=insert_steps_automatically, strict=inference_strict)
    return sophios.compiler.compile_source(bundle(yml, name, SYNTHETIC_TOOLS if tools is None else tools),
                                           sophios.compiler.CompileSettings(inference), quiet=True)


def compile_hermetic_source(source: str, name: str = 'oracle', *, tools: Tools | None = None) -> CompilationResult:
    """Compile `.wic` text against the synthetic registry, for what only text says, such as whether an `!ii` scalar is
    quoted (bundle dumps with yaml.dump, which picks the quoting)."""
    parsed = parse(source, f'{name}.wic')
    return sophios.compiler.compile_source(
        SourceBundle(parsed, name, RegistrySnapshot.from_tools(SYNTHETIC_TOOLS if tools is None else tools), ()),
        sophios.compiler.CompileSettings(), quiet=True)


def compile_production(yml: Yaml, name: str = 'binding', *, tools: Tools | None = None) -> CompilationResult:
    """Compile with user-facing progress enabled (``quiet=False``)."""
    return sophios.compiler.compile_source(
        bundle(yml, name, SYNTHETIC_TOOLS if tools is None else tools),
        sophios.compiler.CompileSettings(), quiet=False)


def compile_hermetic_cwl(yml: Yaml, name: str = 'oracle', *,
                         tools: Tools | None = None,
                         insert_steps_automatically: bool = False) -> Yaml:
    """Compile one in-memory workflow and return the emitted CWL."""
    return compile_hermetic(yml, name, tools=tools, insert_steps_automatically=insert_steps_automatically).artifact.cwl


def nested_workflow(levels: int) -> Yaml:
    """A leaf step wrapped `levels` deep, each level a `subworkflow_step` of its own."""
    tree: Yaml = {'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'leaf.txt'}}}]}
    for level in range(levels):
        tree = {'steps': [subworkflow_step(f'level_{level}.wic', tree)]}
    return tree


def subworkflow_step(stem: str, subtree: Yaml) -> Yaml:
    """A subworkflow step, its body in `subtree` and its keys in `parentargs`: a partitioned workflow without files,
    so partition independence stays hermetic."""
    assert stem.endswith('.wic'), 'a subworkflow step is recognised by its .wic suffix only'
    return {'id': stem, 'subtree': subtree, 'parentargs': {}}
