"""An edge reference names a definition, and a name is defined once.

Both were enforced already, as bare `ValueError`s a caller could not match on,
suppress, or tell from a bug. They carry codes now.

MUST RUN WITH `testing=False`. Every other harness passes `testing=True` -- it
is what lets `test_cwl_embedding_independence` recompile each subworkflow as
though it were root -- and that branch absorbs an undefined edge into a
workflow input. The production path is reachable no other way.
"""
from typing import Any, Final

import pytest

import sophios.cli
import sophios.compiler
from sophios.lang.diagnostics import Code, SophiosError
from sophios.utils_graphs import get_graph_reps
from sophios.wic_types import StepId, Yaml, YamlTree

from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS

#: A step that produces a File, optionally naming it as an edge.
_SOURCE: Final = {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}}


def _compile(yml: Yaml, *, is_root: bool = True) -> Any:
    """Compile one workflow the way a root compilation does."""
    options, graph_settings, tag_paths = sophios.cli.default_compilation_settings()
    return sophios.compiler.compile_workflow(
        YamlTree(StepId('binding', SYNTHETIC_NS), yml), options, graph_settings, tag_paths,
        [], [get_graph_reps('binding')], {}, {}, {}, {}, SYNTHETIC_TOOLS,
        is_root, relative_run_path=True, testing=False)


def _codes(excinfo: pytest.ExceptionInfo[SophiosError]) -> list[str]:
    """The codes a raised `SophiosError` carries."""
    return [d.code.value for d in excinfo.value.diagnostics]


@pytest.mark.fast
def test_a_reference_to_a_defined_edge_compiles() -> None:
    """The ordinary case, so the tests below are about the exception and not the rule."""
    _compile({'steps': [
        {**_SOURCE, 'out': [{'file': {'wic_anchor': 'produced'}}]},
        {'id': 'sink', 'in': {'file': {'wic_alias': 'produced'}}}]})


@pytest.mark.fast
def test_a_reference_with_no_definition_is_reported() -> None:
    """`wic025`, rather than a `ValueError` a caller cannot match on."""
    with pytest.raises(SophiosError) as excinfo:
        _compile({'steps': [_SOURCE, {'id': 'sink', 'in': {'file': {'wic_alias': 'absent'}}}]})
    assert Code.UNDEFINED_EDGE.value in _codes(excinfo)
    assert 'absent' in str(excinfo.value.diagnostics[0].message)


@pytest.mark.fast
def test_a_name_defined_twice_is_reported() -> None:
    """`wic026`. An edge name identifies one producer, so a second definition
    leaves no way to say which output a reference means."""
    with pytest.raises(SophiosError) as excinfo:
        _compile({'steps': [
            {**_SOURCE, 'out': [{'file': {'wic_anchor': 'twice'}}]},
            {'id': 'mk_text', 'in': {'name': {'wic_inline_input': 'b'}},
             'out': [{'file': {'wic_anchor': 'twice'}}]},
            {'id': 'sink', 'in': {'file': {'wic_alias': 'twice'}}}]})
    assert Code.DUPLICATE_EDGE_DEF.value in _codes(excinfo)


@pytest.mark.fast
def test_a_definition_nothing_consumes_is_not_reported() -> None:
    """The claim these rules deliberately do not make.

    An `!&` with no `!*` is how a workflow names an artifact it produces, and
    fifteen corpus documents rely on it. Without this test the rule reads as
    "definitions and references must pair up", which is the natural next
    tightening and would reject all fifteen.
    """
    _compile({'steps': [{**_SOURCE, 'out': [{'file': {'wic_anchor': 'exported'}}]}]})


@pytest.mark.fast
def test_a_subworkflow_may_reference_what_its_parent_defines() -> None:
    """Below the root, an undefined edge is the includer's to satisfy.

    Thirty-three corpus documents reference a name defined in another file, so
    reporting away from the root would reject a supported arrangement rather
    than a mistake.
    """
    _compile({'steps': [_SOURCE, {'id': 'sink', 'in': {'file': {'wic_alias': 'from_parent'}}}]},
             is_root=False)
