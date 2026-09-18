"""An edge reference names a definition, and a name is defined once.

Both were enforced already, as bare `ValueError`s a caller could not match on,
suppress, or tell from a bug. They carry codes now.

Everything here compiles through `compile_production`, which is `testing=False`.
`compile_hermetic` passes `testing=True`, and that branch absorbs an undefined
edge into a workflow input, so these diagnostics are unreachable through it.
"""
from typing import Final

import pytest

from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode

from .hermetic import compile_production, subworkflow_step

#: A step that produces a File, optionally naming it as an edge.
_SOURCE: Final = {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}}


def _codes(excinfo: pytest.ExceptionInfo[SophiosError]) -> list[str]:
    """The codes a raised `SophiosError` carries."""
    return [d.code.value for d in excinfo.value.diagnostics]


@pytest.mark.fast
def test_a_reference_to_a_defined_edge_compiles() -> None:
    """The ordinary case, so the tests below are about the exception and not the rule."""
    compile_production({'steps': [
        {**_SOURCE, 'out': [{'file': {'wic_anchor': 'produced'}}]},
        {'id': 'sink', 'in': {'file': {'wic_alias': 'produced'}}}]})


@pytest.mark.fast
def test_a_reference_with_no_definition_is_reported() -> None:
    """`wic025`, rather than a `ValueError` a caller cannot match on."""
    with pytest.raises(SophiosError) as excinfo:
        compile_production({'steps': [_SOURCE, {'id': 'sink', 'in': {'file': {'wic_alias': 'absent'}}}]})
    assert SophiosErrorCode.UNDEFINED_EDGE.value in _codes(excinfo)
    assert 'absent' in str(excinfo.value.diagnostics[0].message)


@pytest.mark.fast
def test_a_name_defined_twice_is_reported() -> None:
    """`wic026`. An edge name identifies one producer, so a second definition
    leaves no way to say which output a reference means."""
    with pytest.raises(SophiosError) as excinfo:
        compile_production({'steps': [
            {**_SOURCE, 'out': [{'file': {'wic_anchor': 'twice'}}]},
            {'id': 'mk_text', 'in': {'name': {'wic_inline_input': 'b'}},
             'out': [{'file': {'wic_anchor': 'twice'}}]},
            {'id': 'sink', 'in': {'file': {'wic_alias': 'twice'}}}]})
    assert SophiosErrorCode.DUPLICATE_EDGE_DEF.value in _codes(excinfo)


@pytest.mark.fast
def test_a_definition_nothing_consumes_is_not_reported() -> None:
    """The claim these rules deliberately do not make.

    An `!&` with no `!*` is how a workflow names an artifact it produces, and
    fifteen corpus documents rely on it. Without this test the rule reads as
    "definitions and references must pair up", which is the natural next
    tightening and would reject all fifteen.
    """
    compile_production({'steps': [{**_SOURCE, 'out': [{'file': {'wic_anchor': 'exported'}}]}]})


@pytest.mark.fast
def test_a_child_reference_binds_to_the_parents_definition() -> None:
    """A real root, a real child: the edge crosses the boundary.

    Compiling the child alone with `is_root=False` proves only that deferral is
    allowed. It says nothing about the obligation being discharged, which is
    the half that matters, so this builds the parent.
    """
    child = {'steps': [{'id': 'sink', 'in': {'file': {'wic_alias': 'shared'}}}]}
    info = compile_production({'steps': [
        {**_SOURCE, 'out': [{'file': {'wic_anchor': 'shared'}}]},
        subworkflow_step('child.wic', child)]}, 'root')

    bindings = info.rose.data.compiled_cwl['steps'][1]['in']
    source = bindings['child__step__1__sink___file']['source']
    assert source == 'root__step__1__mk_file/file', (
        f'the child bound to {source!r} rather than the parent output that defines it')


@pytest.mark.fast
def test_a_reference_must_follow_its_definition() -> None:
    """`!&` comes before `!*`, and the compilation is ordered, not a set.

    The definition exists in this document either way, so "it must exist" does
    not decide this case. Reversing the two steps is `wic025`.
    """
    defined_first = [{**_SOURCE, 'out': [{'file': {'wic_anchor': 'e'}}]},
                     {'id': 'sink', 'in': {'file': {'wic_alias': 'e'}}}]
    compile_production({'steps': defined_first})

    with pytest.raises(SophiosError) as excinfo:
        compile_production({'steps': list(reversed(defined_first))})
    assert SophiosErrorCode.UNDEFINED_EDGE.value in _codes(excinfo)


@pytest.mark.fast
def test_an_obligation_no_includer_discharges_is_not_reported() -> None:
    """The hole the diagnostic does not close, pinned so it is visible.

    `wic025` is raised by the root *invocation*, not over the compilation. A
    child's unresolved reference is absorbed into a workflow input one level
    down and its name is lost, so the root has nothing left to check: the
    document compiles and the obligation surfaces as a generated root input
    that nothing produces.

    Closing it means carrying the unresolved name up and checking the aggregate
    once at the root, which is what deferred-obligation discharge does in the
    typed IR. Doing it inside the per-step loop would be a third special case
    in the path that phase replaces. This test flips when that lands.
    """
    child = {'steps': [_SOURCE, {'id': 'sink', 'in': {'file': {'wic_alias': 'absent'}}}]}
    info = compile_production({'steps': [subworkflow_step('child.wic', child)]}, 'root')

    generated = info.rose.data.compiled_cwl['inputs']
    assert any(name.endswith('sink___file') for name in generated), (
        'the unresolved reference no longer reaches the root as an input; if it is '
        'reported now, this test has served its purpose and should become that assertion')
