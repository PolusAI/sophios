"""Claims about a single compilation.

These are the Spec 2 properties that are *not* equivalences. An equivalence
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
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import given

import sophios.compiler
from sophios.lang.diagnostics import Code, SophiosError
from sophios.utils import parse_step_name_str, step_name_str
from sophios.wic_types import CompilerInfo, RoseTree, Yaml

from . import ast_strategies as strat
from .hermetic import ORACLE, compile_hermetic, compile_hermetic_cwl
from .reference_model import ReferenceExpectation, reference_expectation
from .synthetic_tools import inputs_of, outputs_of

# --------------------------------------------------------------------------
# Namespace injectivity
# --------------------------------------------------------------------------


def _expected_port_names(yml: Yaml, stem: str) -> list[str]:
    """Every namespaced output name this workflow should produce.

    Rebuilt from the AST and the registry rather than read back from the
    compiled document, and that direction is the whole point: a collision is
    one dict key silently overwriting another, so from the output alone it is
    invisible — the document simply has one fewer key than it should, and
    nothing in it says which one went missing.

    Mirrors `docs/dev/algorithms.md`: a namespace segment is
    `step_name_str(stem, i, key)` and a port is the segments joined with
    `'___'`.
    """
    return [
        f'{step_name_str(stem, index, str(step["id"]))}___{out_key}'
        for index, step in enumerate(yml['steps'])
        if not str(step['id']).endswith('.wic')
        for out_key in outputs_of(str(step['id']))
    ]


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(strat.workflows().filter(lambda w: len(w['steps']) >= 2))
@ORACLE
def test_distinct_ports_never_collide_after_namespacing(yml: Yaml) -> None:
    """Every port a workflow has survives namespacing under its own name.

    BLIND SPOTS: quantifies over the stems in the synthetic registry, none of
    which contain `__` — the separator `step_name_str` joins on. The
    workflow-stem half of that gap is covered by the parametrised test below,
    which supplies stems containing both separators directly. Subworkflow
    steps are skipped: their ports come from the subtree, so reconstructing
    them independently would mean recursing through it, and the relative
    namespacing that governs them is what plan Task 4's `split` rewrite
    already quantifies over. A workflow the compiler *diagnoses* is skipped
    rather than failed: this claim is about the ports a successful compilation
    produces, and a rejected document has none.
    """
    try:
        compiled = compile_hermetic_cwl(copy.deepcopy(yml), 'ns')
    except SophiosError:
        return  # a diagnosed workflow has no ports to collide; see BLIND SPOTS
    expected = _expected_port_names(yml, 'ns')

    duplicates = sorted({name for name in expected if expected.count(name) > 1})
    assert not duplicates, f'two distinct ports namespace to the same string: {duplicates}'

    emitted = set(compiled.get('inputs', {})) | set(compiled.get('outputs', {}))
    missing = sorted(set(expected) - emitted)
    assert not missing, f'namespaced ports were dropped or overwritten: {missing}'


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@pytest.mark.parametrize('stem', ['ns', 'a__b', 'x___y'], ids=['plain', 'double', 'triple'])
def test_injectivity_survives_a_workflow_name_containing_the_separators(stem: str) -> None:
    """`step_name_str` joins on `__` and ports join on `___`.

    Its docstring states the precondition — "as long as yaml_stem and step_key
    do not contain `__`" — and nothing enforces it. A workflow named `a__b` is
    a legal filename, so the precondition is reachable from ordinary use.
    Either injectivity holds anyway, or this finds the collision; both are
    results worth having, which is why the case is stated rather than avoided.
    """
    yml: Yaml = {'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a.txt'}}},
        {'id': 'mk_text', 'in': {'name': {'wic_inline_input': 'b.txt'}}},
    ]}
    compiled = compile_hermetic_cwl(copy.deepcopy(yml), stem)
    expected = _expected_port_names(yml, stem)

    assert len(set(expected)) == len(expected), (
        f'workflow stem {stem!r} makes two ports namespace alike: {expected}')
    emitted = set(compiled.get('inputs', {})) | set(compiled.get('outputs', {}))
    missing = sorted(set(expected) - emitted)
    assert not missing, f'namespaced ports were dropped or overwritten: {missing}'


# --------------------------------------------------------------------------
# Edge soundness
# --------------------------------------------------------------------------


def _compatible(in_type: Any, out_type: Any) -> bool:
    """Whether a CWL input type accepts a CWL output type.

    A second implementation of the rule `inference.types_match` states, written
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


def _inferred_edges(compiled: Yaml) -> list[tuple[str, str, str]]:
    """`(consuming stem, consuming arg, producing "step/out")` for each inferred edge.

    An inferred edge is a **bare string** with exactly one `/`
    (`src/sophios/inference.py`, `_finalize_matched_edge`). `{'source': …}` is
    written only by the explicit and inline paths and belongs to a different
    claim; a bare string with no `/` is a *deferred* inference, whose edge is
    made in a parent scope these single-root compilations never have.
    """
    edges = []
    for step in compiled['steps']:
        _, _, stem = parse_step_name_str(str(step['id']))
        for arg, value in (step.get('in') or {}).items():
            if isinstance(value, str) and value.count('/') == 1:
                edges.append((stem, arg, value))
    return edges


def _workflow_input_edges(compiled: Yaml) -> list[tuple[str, str, str, bool]]:
    """Return consuming tool/argument, workflow input, and scatter state."""
    declared = compiled.get('inputs') or {}
    edges = []
    for step in compiled['steps']:
        _, _, stem = parse_step_name_str(str(step['id']))
        scatter = step.get('scatter') or []
        for argument, value in (step.get('in') or {}).items():
            source = value.get('source') if isinstance(value, dict) else value
            if isinstance(source, str) and source in declared:
                edges.append((stem, argument, source, argument in scatter))
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
    compile until the Spec 3 migration; and `format` compatibility, which
    `types_match` does not consider and `_match_outputs_of_step` handles on a
    separate axis. Edges into or out of a subworkflow step are skipped: the
    port's declared type lives in the subtree rather than in the registry. A
    diagnosed workflow is skipped: it has no inferred edges to check.
    """
    try:
        compiled = compile_hermetic_cwl(copy.deepcopy(yml), 'edges')
    except SophiosError:
        return  # a diagnosed workflow has no inferred edges; see BLIND SPOTS

    for stem, arg, source in _inferred_edges(compiled):
        producer, out_key = source.split('/')
        _, _, producer_stem = parse_step_name_str(producer)
        if stem not in inputs_of.__globals__['STEMS'] or producer_stem.endswith('.wic'):
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
        if stem not in inputs_of.__globals__['STEMS'] or stem.endswith('.wic'):
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
    """Drive the fixed-point loop to exhaustion, through the real guard.

    `compile_workflow_once` is wrapped so the AST it reports always differs
    from the one it was given. `ast_modified` is then true on every pass, the
    loop runs its full count, and the guard fires in place, reached the way it
    is reached in production.

    A workflow contrived to insert steps forever would be slower, would depend
    on inference choices this test has no opinion about, and would exercise
    exactly the same two lines. Patching `max_iters` itself would be testing a
    number rather than the guard.

    Module-level so `provocations.py` can import it lazily: that module reaches
    `compile_harness` and therefore plugin discovery, so the dependency runs
    that way and never the reverse.
    """
    real = sophios.compiler.compile_workflow_once
    tick = 0

    def always_modified(yaml_tree_ast: Any, *args: Any, **kwargs: Any) -> CompilerInfo:
        nonlocal tick
        tick += 1
        info: CompilerInfo = real(yaml_tree_ast, *args, **kwargs)
        moved = info.rose.data._replace(yml={**info.rose.data.yml, '_tick': tick})
        return info._replace(rose=RoseTree(moved, info.rose.sub_trees))

    with patch.object(sophios.compiler, 'compile_workflow_once', always_modified):
        compile_hermetic_cwl(
            {'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a.txt'}}}]},
            'diverge')


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_exhausting_max_iters_is_a_diagnostic_not_a_crash() -> None:
    """The fixed-point guard reports; it does not explode.

    CR-104 converted the `sys.exit` sites and did not reach this one, because
    it was never an exit. An embedder catching `SophiosError` still saw a bare
    `RuntimeError` escape.
    """
    with pytest.raises(SophiosError) as caught:
        never_converges()
    assert caught.value.diagnostics[0].code is Code.FIXED_POINT_NOT_REACHED


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
