"""Lowering a document reached as a compile reaches it, through Resolve and the Check inside it: total, every reference
names an existing port, every authored binding kept, deterministic; and each document of tests/core/data/check and
tests/core/data/lower gives the diagnostics its expected.yaml states."""
from collections.abc import Callable

import pytest
from hypothesis import given

from sophios.ir.lower import lower
from sophios.ir.resolve import RegistrySnapshot, resolve
from sophios.lang.nodes import Document
from sophios.lang.parser import parse
from sophios.wic_types import Tools

from . import ast_strategies as strat
from .cases import DATA, diagnostic, match
from .hermetic import COVERAGE, bundle
from .helpers import stated
from .synthetic_tools import SYNTHETIC_TOOLS, clt, registered


def _running(document: Document) -> Tools:
    """A tool for each step of `document`, declaring the inputs the step binds and the outputs it names."""
    return registered({step.id: clt(dict.fromkeys((name for name, _ in step.inputs), {'type': 'Any'}),
                                    dict.fromkeys((out.name for out in step.outputs), {'type': 'File'}))
                       for step in document.steps})


@pytest.mark.fast
@pytest.mark.parametrize('family, tools', [('check', lambda _: {}), ('lower', _running)])
def test_a_document_gives_the_diagnostics_its_family_states(family: str,
                                                            tools: Callable[[Document], Tools]) -> None:
    """Each `.wic` of tests/core/data/<family> parses clean and gives, through Resolve and then Lower, exactly the
    diagnostics expected.yaml lists for it, by code, severity and line, and a graph exactly when none is an error. A
    `check` document breaks a rule of its own nodes, and with no tool to find gives Check's alone: Resolve looks
    nothing up for it."""
    expected = stated(DATA / family)
    assert sorted(expected) == sorted(path.name for path in (DATA / family).glob('*.wic'))
    for name, diagnostics in expected.items():
        parsed = parse((DATA / family / name).read_text(encoding='utf-8'), name)
        assert parsed.document is not None and not parsed.diagnostics.has_errors, (name, list(parsed.diagnostics))
        resolved = resolve(parsed.document, RegistrySnapshot.from_tools(tools(parsed.document)), name='probe')
        found = list(resolved.diagnostics)
        if resolved.document is not None:
            lowered = lower(resolved.document)
            assert (lowered.graph is None) is lowered.diagnostics.has_errors, name
            found += lowered.diagnostics
        assert match(diagnostics, [diagnostic(each) for each in found]), (name, found)


@pytest.mark.fast
@given(strat.documents())
@COVERAGE
def test_a_well_formed_document_lowers_whole(document: Document) -> None:
    """`documents()` draws only well-formed documents, so each lowers to a graph with a step for each of its steps,
    never a diagnostic or an exception; the same graph twice, in full, mappings included, so none varies with set
    iteration; with every binding written in `in:`, so Emit never reads the AST again; and with every reference its
    edges and mappings hold naming a port that exists, which a dangling `source:` would not."""
    model = bundle(strat.to_yml(document), 'probe', SYNTHETIC_TOOLS)
    assert model.parsed.document is not None
    resolved = resolve(model.parsed.document, model.registry, name='probe')
    assert resolved.document is not None, [str(each) for each in resolved.diagnostics]
    result = lower(resolved.document)
    assert result.graph is not None, [d.code.value for d in result.diagnostics]
    assert len(result.graph.steps) == len(document.steps)
    assert lower(resolved.document).graph == result.graph
    assert [b.value for step in result.graph.steps for b in step.bindings] == \
        [value for step in resolved.document.source.steps for _, value in step.inputs]
    graph = result.graph
    known = {p.id for s in graph.steps for p in s.inputs + s.outputs}
    held = [port for edge in graph.edges for port in (edge.source, edge.sink)]
    held += [port for _, port in (*graph.explicit_edge_defs, *graph.output_mapping)]
    held += [binding.sink for step in graph.steps for binding in step.bindings]
    held += [port for _, ports in graph.input_mapping for port in ports]
    assert set(held) <= known
