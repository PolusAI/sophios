"""Claims about a single compilation.

These are the properties that are *not* equivalences. An equivalence
compares two compilations and needs a relation to say what may differ between
them; a predicate examines one compilation and needs none. Grouping them by
that fact rather than by which register row they came from is the point — a
reader can tell at a glance which properties compare two runs and which read
one.

Three claims live here:

  * **Namespace injectivity.** Distinct ports never collide after namespacing.
  * **Edge soundness.** Every inferred edge connects type-compatible ports.
  * **Termination.** Compilation returns or reports; it never raises a bare
    exception, and `max_iters` is never silently exhausted.

See design_docs/core-refactor-design.md §6.2.
"""
import copy
from pathlib import PurePosixPath
from typing import Any

import pytest
from hypothesis import given

from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.ir import InferencePolicy, Namespace, WorkflowGraph, infer
from sophios.ir.artifacts import CompilationResult
from sophios.ir.names import Names
from sophios.ir.types import AuthoredName, DerivedName
from sophios.wic_types import Yaml

from . import ast_strategies as strat
from .hermetic import ORACLE, compile_hermetic, compile_hermetic_cwl
from .reference_model import ReferenceExpectation, reference_expectation
from .synthetic_tools import STEMS, inputs_of, outputs_of

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


# --------------------------------------------------------------------------
# Edge soundness
# --------------------------------------------------------------------------


def _compatible(in_type: Any, out_type: Any) -> bool:
    """Whether a CWL input type accepts a CWL output type.

    A second implementation of the rule `ir.infer.types_match` states, written
    from the CWL semantics rather than transcribed from it. Calling the
    compiler's own predicate to grade the compiler's own choice would prove the
    two agree, which they would by construction; the point is that a bug in
    `types_match` shows up here as a disagreement instead of being reproduced
    faithfully on both sides.
    """
    match in_type, out_type:
        case list() as ins, list() as outs:
            return bool(set(ins) & set(outs))
        case list() as ins, _:
            return out_type in ins
        case _, list() as outs:
            return in_type in outs
        case _:
            return bool(in_type == out_type)


def _process(step: Yaml) -> str:
    """The process a step runs, read from its `run:` path and never from its id."""
    return PurePosixPath(str(step['run'])).stem


def _inferred_edges(compiled: Yaml) -> list[tuple[str, str, str]]:
    """`(consuming process, consuming arg, producing "step/out")` for each inferred edge.

    An inferred edge is a **bare string** with exactly one `/`
    (`src/sophios/inference.py`, `_finalize_matched_edge`). `{'source': …}` is
    written only by the explicit and inline paths and belongs to a different
    claim; a bare string with no `/` is a *deferred* inference, whose edge is
    made in a parent scope these single-root compilations never have.
    """
    edges = []
    for step in compiled['steps']:
        for arg, value in (step.get('in') or {}).items():
            if isinstance(value, str) and value.count('/') == 1:
                edges.append((_process(step), arg, value))
    return edges


def _producers(compiled: Yaml) -> dict[str, tuple[str, str]]:
    """Each `step/out` reference the document can make, to its process and output."""
    return {f'{step["id"]}/{out}': (_process(step), out)
            for step in compiled['steps'] for out in step.get('out') or []}


def _workflow_input_edges(compiled: Yaml) -> list[tuple[str, str, str, bool]]:
    """Return consuming process/argument, workflow input, and scatter state."""
    declared = compiled.get('inputs') or {}
    edges = []
    for step in compiled['steps']:
        scatter = step.get('scatter') or []
        for argument, value in (step.get('in') or {}).items():
            source = value.get('source') if isinstance(value, dict) else value
            if isinstance(source, str) and source in declared:
                edges.append((_process(step), argument, source, argument in scatter))
    return edges


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_the_edge_recogniser_tells_the_three_emitted_shapes_apart() -> None:
    """Without this, `_inferred_edges` returning `[]` makes the property below
    pass over nothing at all.

    One workflow carrying an inline literal (a `source` mapping) and an
    inferred edge (a bare `step/out`), so the recogniser is shown selecting
    the second and ignoring the first.
    """
    compiled = compile_hermetic_cwl(
        {'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a.txt'}}},
                   {'id': 'xform', 'in': {'name': {'wic_inline_input': 'b.txt'}}}]},
        'shapes')
    edges = _inferred_edges(compiled)
    assert edges, 'no inferred edge recognised; the soundness property would be vacuous'
    assert all(value.count('/') == 1 for _, _, value in edges)


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_the_workflow_input_recogniser_is_not_vacuous() -> None:
    """The recognizer selects workflow inputs and excludes step-to-step edges."""
    compiled = compile_hermetic_cwl(
        {'inputs': {'wf_name': {'type': 'string'}},
         'steps': [{'id': 'mk_file', 'in': {'name': 'wf_name'}},
                   {'id': 'xform', 'in': {'name': {'wic_inline_input': 'b.txt'}}}]},
        'refs')
    edges = _workflow_input_edges(compiled)
    assert [(argument, source) for _, argument, source, _ in edges] == [
        ('name', 'wf_name'), ('name', 'refs__step__2__xform___name')]
    assert _inferred_edges(compiled), 'the fixture no longer carries the step edge to exclude'


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(strat.workflows().filter(lambda w: len(w['steps']) >= 2))
@ORACLE
def test_every_inferred_edge_connects_compatible_ports(yml: Yaml) -> None:
    """Inference never wires an input to an output it cannot accept.

    BLIND SPOTS: deferred inferences, whose edge is made in a parent scope
    these single-root compilations never have; `!cwl` references, which do not
    compile until the front end is wired in; and `format` compatibility, which
    `types_match` does not consider and `_match_outputs_of_step` handles on a
    separate axis. Edges into or out of a subworkflow step are skipped: the
    port's declared type lives in the subtree rather than in the registry. A
    diagnosed workflow is skipped: it has no inferred edges to check.
    """
    try:
        compiled = compile_hermetic_cwl(copy.deepcopy(yml), 'edges')
    except SophiosError:
        return  # a diagnosed workflow has no inferred edges; see BLIND SPOTS

    producers = _producers(compiled)
    for stem, arg, source in _inferred_edges(compiled):
        producer_stem, out_key = producers[source]
        if stem not in STEMS or producer_stem not in STEMS:
            continue  # a subworkflow port; its type lives in the subtree
        in_type = inputs_of(stem)[arg]['type']
        out_type = outputs_of(producer_stem)[out_key]['type']
        assert _compatible(in_type, out_type), (
            f'{stem}.{arg} ({in_type}) <- {producer_stem}.{out_key} ({out_type})\n'
            f'{strat.render_yml(yml) if hasattr(strat, "render_yml") else yml}')


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(strat.workflows())
@ORACLE
def test_every_generated_workflow_input_reference_is_not_proven_disjoint(yml: Yaml) -> None:
    """Generated references compile and agree with the independent model.

    There is intentionally no catch-all exception arm.  A false rejection —
    especially of ``Any`` — is a property failure, not a document to skip.
    """
    compiled = compile_hermetic_cwl(copy.deepcopy(yml), 'refs')
    declared = compiled.get('inputs') or {}
    for stem, argument, source, scattered in _workflow_input_edges(compiled):
        if stem not in STEMS:
            continue
        sink_type = inputs_of(stem)[argument]['type']
        if scattered:
            sink_type = {'type': 'array', 'items': sink_type}
        source_type = declared[source].get('type')
        assert reference_expectation(source_type, sink_type) is not ReferenceExpectation.DISJOINT, (
            f'{stem}.{argument} ({sink_type}) <- {source} ({source_type})\n{yml}')


# --------------------------------------------------------------------------
# Termination
# --------------------------------------------------------------------------


def never_converges() -> None:
    """Drive the typed Infer fixed-point guard to its explicit limit."""
    result = infer(WorkflowGraph(Namespace()), InferencePolicy(iteration_limit=0))
    if result.graph is None:
        raise SophiosError(result.diagnostics)


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_exhausting_max_iters_is_a_diagnostic_not_a_crash() -> None:
    """The fixed-point guard reports; it does not explode.

    This site was never a `sys.exit`, so a sweep over those does not reach it,
    and an embedder catching `SophiosError` sees a bare `RuntimeError` escape.
    """
    with pytest.raises(SophiosError) as caught:
        never_converges()
    assert caught.value.diagnostics[0].code is SophiosErrorCode.FIXED_POINT_NOT_REACHED


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_exhaustion_prints_no_workflow_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """The guard used to `print(yaml.dump(node_data.yml))` before raising — a
    whole workflow on stdout, on a path that is now a reportable error. A
    diagnostic is a message meant to be read; a YAML dump is not.
    """
    with pytest.raises(SophiosError):
        never_converges()
    assert 'steps:' not in capsys.readouterr().out


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(strat.workflows())
@ORACLE
def test_compilation_returns_or_reports_but_never_raises_bare(yml: Yaml) -> None:
    """Every outcome is a result or a diagnostic.

    `RuntimeError`, `AttributeError`, `KeyError` and friends are all failures
    here: the claim is that a caller can catch `SophiosError` and be done.

    BLIND SPOTS: quantifies over `compilable_documents()`, so constructs the
    generator excludes — `!cwl`, `python_script` — are not covered, and nor is
    any failure that hangs rather than raises.
    """
    try:
        compile_hermetic(copy.deepcopy(yml), 'total')
    except SophiosError:
        pass
