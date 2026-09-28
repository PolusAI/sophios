"""The completed typed pipeline, end to end.

The end-to-end property composes the independently tested phases over the full
generator and uses the established equivalence relation. ``UP_TO_EMBEDDING``
is the compatibility contract: generated paths may move, while workflow
structure, ports, types, bindings, requirements, and opaque payloads may not.

Nothing here asserts that a retired name has stayed retired. Those scans were
removed deliberately: a symbol that no longer exists cannot come back by
accident, so such a check restates its own deletion and fails only when someone
retypes the name on purpose -- which review catches and a test does not.
"""
import copy
from typing import Any

import pytest
from hypothesis import given, strategies as st

from sophios.ir.complete import coerce_job_value, complete
from sophios.ir.declarations import port_declaration
from sophios.ir.emit import emit, surface
from sophios.ir.names import Names
from sophios.ir.types import WorkflowGraph
from sophios.ir.infer import infer
from sophios.ir.link import link
from sophios.ir.pipeline import front_end
from sophios.lang.diagnostics import SophiosError
from sophios.wic_types import Yaml

from . import ast_strategies as strat
from .equivalence import Strength, equivalent
from .hermetic import ORACLE, bundle, compile_hermetic
from .synthetic_tools import SYNTHETIC_TOOLS
from .test_resolve import _scalar_literals_fit


def _render(graph: WorkflowGraph) -> Any:
    """`graph` emitted as the compiler emits its root document."""
    names = Names.of(graph)
    return emit(surface(graph, names), names)


@pytest.mark.skip_pypi_ci
@given(strat.workflows().filter(_scalar_literals_fit))
@ORACLE
def test_full_pipeline_agrees_at_up_to_embedding(workflow: Yaml) -> None:
    """The live boundary agrees with direct typed phase composition."""
    model = bundle(copy.deepcopy(workflow), 'oracle', SYNTHETIC_TOOLS)
    front = front_end(model.parsed, model.registry, name='oracle')
    assert front.graph is not None, list(front.diagnostics)

    prepared = complete(front.graph)
    linked = link(prepared)
    assert linked.graph is not None, list(linked.diagnostics)
    inferred = infer(complete(linked.graph))
    assert inferred.graph is not None, list(inferred.diagnostics)
    direct = _render(complete(inferred.graph))

    live = compile_hermetic(copy.deepcopy(workflow)).artifact.cwl
    divergence = equivalent(direct, live, Strength.UP_TO_EMBEDDING)
    assert divergence is None, divergence


@pytest.mark.skip_pypi_ci
@given(strat.workflows().filter(_scalar_literals_fit))
@ORACLE
def test_every_emitted_reference_resolves_in_its_own_document(workflow: Yaml) -> None:
    """A `source:` names something the document it appears in defines.

    The types close what a binding may *be*; they cannot say that the name
    inside one exists. A reference that resolves nowhere is accepted by CWL's
    schema and fails at the runner -- which is how an authored `outputSource`
    pointing at a step emission had renamed got as far as it did.

    Checked on the artifact rather than the graph, because the document is what
    has to be self-contained: a `run:` child is its own document, so each is
    judged against its own inputs and steps.
    """
    info = compile_hermetic(copy.deepcopy(workflow))

    def check(cwl: Yaml, where: str) -> None:
        if cwl.get('class') != 'Workflow':
            return
        defined = set(cwl.get('inputs') or {})
        for step in cwl.get('steps') or []:
            for out in step.get('out') or []:
                defined.add(f'{step["id"]}/{out}')
        for step in cwl.get('steps') or []:
            for port, value in (step.get('in') or {}).items():
                name = value.get('source') if isinstance(value, dict) else value
                if isinstance(name, str) and '$(' not in name and '${' not in name:
                    assert name in defined, f'{where}: {step["id"]}.{port} sources {name!r}'
        for name, declared in (cwl.get('outputs') or {}).items():
            source = declared.get('outputSource') if isinstance(declared, dict) else None
            if isinstance(source, str):
                assert source in defined, f'{where}: output {name} sources {source!r}'

    def walk(artifact: Any) -> None:
        check(artifact.cwl, artifact.name)
        for child in artifact.children:
            walk(child)

    walk(info.artifact)


@pytest.mark.skip_pypi_ci
@given(strat.workflows().filter(_scalar_literals_fit))
@ORACLE
def test_completing_twice_is_completing_once_for_any_workflow(workflow: Yaml) -> None:
    """`complete` is idempotent, over the generated space.

    It documents itself so and `_compile_front` calls it three times on that
    promise, so any arm reading its own previous output compounds silently.
    One such arm shipped, in the half that is now `surface`: the `run:` target
    was recomputed from the last pass and grew a prefix each time. That half
    runs once per document now and cannot compound; this covers what still
    repeats.
    """
    model = bundle(copy.deepcopy(workflow), 'oracle', SYNTHETIC_TOOLS)
    front = front_end(model.parsed, model.registry, name='oracle')
    assert front.graph is not None, list(front.diagnostics)
    once = complete(front.graph)
    assert _render(complete(once)) == _render(once)


_ATOMS = st.sampled_from(['string', 'int', 'float', 'boolean', 'File', 'Directory'])
_TYPES = st.recursive(
    st.one_of(_ATOMS, _ATOMS.map(lambda atom: atom + '?'), _ATOMS.map(lambda atom: atom + '[]')),
    lambda inner: st.one_of(inner.map(lambda item: ['null', item]),
                            inner.map(lambda item: {'type': 'array', 'items': item})),
    max_leaves=3)
_OBJECTS = st.fixed_dictionaries(
    {'class': st.sampled_from(['File', 'Directory']), 'location': st.text(max_size=2)},
    optional={'format': st.just('edam:format_2')})
_VALUES = st.recursive(
    st.one_of(st.text(max_size=2), st.integers(), st.booleans(),
              st.floats(allow_nan=False, allow_infinity=False), _OBJECTS),
    lambda inner: st.lists(inner, max_size=2), max_leaves=4)


@pytest.mark.skip_pypi_ci
@given(_TYPES, st.sampled_from([{}, {'format': 'edam:format_1'}]), _VALUES)
def test_a_job_value_is_its_own_normal_form(raw: Any, fmt: Yaml, value: Any) -> None:
    """Coercing a coerced job value changes nothing, which is what lets a
    child's job value be lifted a level and coerced against the outer port."""
    declaration = port_declaration({'type': raw, **fmt})
    try:
        once = coerce_job_value('x', declaration, value)
    except SophiosError:
        return
    assert coerce_job_value('x', declaration, once) == once
