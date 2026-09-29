"""Claims about a single compilation.

These are the properties that are *not* equivalences. An equivalence
compares two compilations and needs a relation to say what may differ between
them; a predicate examines one compilation and needs none. Grouping them by
that fact rather than by which register row they came from is the point — a
reader can tell at a glance which properties compare two runs and which read
one.

One claim lives here, **namespace injectivity**: distinct ports never
collide after namespacing. Edge soundness is `cwltool --validate`'s, over the
same generator (`test_emit.test_emit_validates_as_cwl_v1_2`); the fixed-point
guard's is `test_infer_phase.test_iteration_exhaustion_is_exactly_wic022`'s.

See design_docs/core-refactor-design.md §6.2.
"""
import copy

import pytest
from hypothesis import given

from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.ir.artifacts import CompilationResult
from sophios.ir.names import Names
from sophios.ir.types import AuthoredName, DerivedName
from sophios.wic_types import Yaml

from . import ast_strategies as strat
from .hermetic import ORACLE, compile_hermetic
from .synthetic_tools import outputs_of

# --------------------------------------------------------------------------
# Namespace injectivity
# --------------------------------------------------------------------------


def _expected_port_names(yml: Yaml, compiled: CompilationResult) -> dict[str, list[str]]:
    """The emitted spelling of every port this workflow should expose, per section.

    `inputs` holds the workflow input each inline literal (`wic_inline_input`)
    becomes, `outputs` each registry step's outputs. They are kept apart
    because they are separate namespaces: a tool may name an input and an
    output alike.

    Rebuilt from the AST and the registry rather than read back from the
    compiled document, and that direction is the whole point: a collision is
    one dict key silently overwriting another, so from the output alone it is
    invisible — the document simply has one fewer key than it should, and
    nothing in it says which one went missing.

    Only the step occurrences are taken from the compiled graph. Each port is
    a structural `DerivedName`, so each list is distinct by construction and a
    duplicate in its rendering is `Names` spelling two identities alike.
    """
    steps = compiled.graph.steps
    assert [step.id.name for step in steps] == [str(step['id']) for step in yml['steps']], (
        'the compiled graph no longer lists the authored steps in order')
    names = Names.of(compiled.graph)
    tools = [(node.id, step) for node, step in zip(steps, yml['steps'])
             if not str(step['id']).endswith('.wic')]
    return {
        'inputs': [names.port(DerivedName(identity, AuthoredName(key)))
                   for identity, step in tools
                   for key, value in (step.get('in') or {}).items()
                   if isinstance(value, dict) and 'wic_inline_input' in value],
        'outputs': [names.port(DerivedName(identity, AuthoredName(key)))
                    for identity, step in tools for key in outputs_of(str(step['id']))],
    }


def _assert_every_port_survives(yml: Yaml, compiled: CompilationResult) -> None:
    """Each expected port renders uniquely and is a key of its emitted section."""
    cwl = compiled.artifact.cwl
    for section, expected in _expected_port_names(yml, compiled).items():
        duplicates = sorted({name for name in expected if expected.count(name) > 1})
        assert not duplicates, f'two distinct {section} namespace to the same string: {duplicates}'
        missing = sorted(set(expected) - set(cwl.get(section) or {}))
        assert not missing, f'namespaced {section} were dropped or overwritten: {missing}'


def _refuses_no_collision(error: SophiosError) -> None:
    """A diagnosed workflow may be skipped, but not one refused for a collision."""
    assert all(diagnostic.code is not SophiosErrorCode.DUPLICATE_DOCUMENT_NAME
               for diagnostic in error.diagnostics), f'two distinct ports rendered alike: {error}'


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(strat.workflows().filter(lambda w: len(w['steps']) >= 2))
@ORACLE
def test_distinct_ports_never_collide_after_namespacing(yml: Yaml) -> None:
    """Every port a workflow has survives namespacing under its own name.

    BLIND SPOTS: quantifies over the stems in the synthetic registry, none of
    which contain `__`. The workflow-stem half of that gap is covered by the
    parametrised test below, which supplies stems containing both separators
    directly. Subworkflow steps are skipped: their ports come from the subtree,
    so reconstructing them independently would mean recursing through it, and
    the relative namespacing that governs them is what the `split` rewrite
    already quantifies over. A workflow the compiler *diagnoses* is skipped
    rather than failed — this claim is about the ports a successful
    compilation produces — unless the diagnosis is `wic031`, which is this
    claim failing at Emit rather than here.
    """
    try:
        compiled = compile_hermetic(copy.deepcopy(yml), 'ns')
    except SophiosError as error:
        _refuses_no_collision(error)
        return  # a diagnosed workflow has no ports to collide; see BLIND SPOTS
    _assert_every_port_survives(yml, compiled)


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@pytest.mark.parametrize('stem', ['ns', 'a__b', 'x___y'], ids=['plain', 'double', 'triple'])
def test_injectivity_survives_a_workflow_name_containing_the_separators(stem: str) -> None:
    """Steps render as `{workflow}__step__{i}__{name}` and ports join on `___`.

    Nothing stops a workflow's name containing either separator — `a__b` is a
    legal filename — so the spelling is not injective by construction for
    ordinary input. Either distinct identities still render apart, or this
    finds the collision; both are results worth having, which is why the case
    is stated rather than avoided.
    """
    yml: Yaml = {'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a.txt'}}},
        {'id': 'mk_text', 'in': {'name': {'wic_inline_input': 'b.txt'}}},
    ]}
    compiled = compile_hermetic(copy.deepcopy(yml), stem)
    _assert_every_port_survives(yml, compiled)
