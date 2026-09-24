"""The completed typed pipeline, and the phases' freedom from ambient state.

The end-to-end property composes the independently tested phases over the full
generator and uses the established equivalence relation. ``UP_TO_EMBEDDING``
is the compatibility contract: generated paths may move, while workflow
structure, ports, types, bindings, requirements, and opaque payloads may not.

The ambient-state property is static because mutable state can hide on a branch
no generated example reaches. It inspects the public phase signatures and module
bodies, and its planted breach proves the detector is not a vacuous scan.

Nothing here asserts that a retired name has stayed retired. Those scans were
removed deliberately: a symbol that no longer exists cannot come back by
accident, so such a check restates its own deletion and fails only when someone
retypes the name on purpose -- which review catches and a test does not.
"""
import ast
import copy
import importlib
import inspect
from typing import Any, Final

import pytest
from hypothesis import given

from sophios.ir.complete import complete
from sophios.ir.emit import emit
from sophios.ir.infer import infer
from sophios.ir.link import link
from sophios.ir.pipeline import front_end
from sophios.ir.resolve import RegistrySnapshot
from sophios.wic_types import StepId as LegacyStepId, Yaml

from . import ast_strategies as strat
from .compile_harness import TOUCH, compile_cwl
from .equivalence import Strength, equivalent
from .hermetic import ORACLE, compile_hermetic, subworkflow_step
from .synthetic_tools import SYNTHETIC_TOOLS
from .test_resolve import _scalar_literals_fit, _source_model


resolve_module = importlib.import_module('sophios.ir.resolve')
lower_module = importlib.import_module('sophios.ir.lower')
link_module = importlib.import_module('sophios.ir.link')
infer_module = importlib.import_module('sophios.ir.infer')
emit_module = importlib.import_module('sophios.ir.emit')
PHASE_MODULES: Final = (resolve_module, lower_module, link_module,
                        infer_module, emit_module)


@pytest.mark.skip_pypi_ci
@given(strat.workflows().filter(_scalar_literals_fit))
@ORACLE
def test_full_pipeline_agrees_at_up_to_embedding(workflow: Yaml) -> None:
    """The live boundary agrees with direct typed phase composition."""
    source, workflows = _source_model(copy.deepcopy(workflow))
    registry = RegistrySnapshot.from_tools(SYNTHETIC_TOOLS, workflows=workflows)
    front = front_end(source, registry, name='oracle')
    assert front.graph is not None, list(front.diagnostics)

    prepared = complete(front.graph)
    linked = link(prepared)
    assert linked.graph is not None, list(linked.diagnostics)
    inferred = infer(complete(linked.graph))
    assert inferred.graph is not None, list(inferred.diagnostics)
    direct = emit(complete(inferred.graph))

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
    """`complete` is idempotent, over the generated space and both run paths.

    It documents itself so and `_compile_front` calls it three times on that
    promise, so any arm reading its own previous output compounds silently.
    One such arm shipped: the namespaced `run:` target took `PurePath(target).stem`
    of the last pass and prefixed again, so a subworkflow gained a prefix per
    call and named no file that was ever written. `relative_run_path=False` is
    the arm that drifts, and `cwl_subinterpreter` compiles with it.
    """
    source, workflows = _source_model(copy.deepcopy(workflow))
    registry = RegistrySnapshot.from_tools(SYNTHETIC_TOOLS, workflows=workflows)
    front = front_end(source, registry, name='oracle')
    assert front.graph is not None, list(front.diagnostics)
    for relative in (True, False):
        once = complete(front.graph, relative_run_path=relative)
        assert emit(complete(once, relative_run_path=relative)) == emit(once), relative


@pytest.mark.fast
def test_an_assembled_implementation_body_reaches_the_registry() -> None:
    """The bundle detaches what the loader attached, not only step subtrees.

    `read_ast_from_disk` leaves each implementation body inline and rekeys the
    mapping by `StepId`. A `StepId` has no YAML representation, and Resolve
    selects an implementation from the registry rather than from the document,
    so a body left in place is both undumpable and unreachable.
    """
    dispatcher: Yaml = {'wic': {
        'implementations': {LegacyStepId('impl', 'global'): copy.deepcopy(TOUCH)},
        'implementation': 'impl',
        'namespace': 'global',
    }}
    compiled = compile_cwl(dispatcher, 'dispatch')
    assert [step['id'] for step in compiled['steps']] == ['dispatch__step__1__touch']


def _mutable_module_bindings(source: str) -> tuple[str, ...]:
    """Names bound to mutable literals at module scope."""
    tree = ast.parse(source)
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if not isinstance(value, (ast.Dict, ast.List, ast.Set)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found.extend(ast.unparse(target) for target in targets)
    return tuple(found)


@pytest.mark.fast
def test_no_phase_reads_mutable_cross_phase_state() -> None:
    """Phases exchange typed values, never module state or four dictionaries."""
    expected_signatures = {
        resolve_module.resolve: ('document', 'registry', 'name', 'lang_version'),
        lower_module.lower: ('document', 'namespace'),
        link_module.link: ('graph',),
        infer_module.infer: ('graph', 'policy', 'catalog'),
        emit_module.emit: ('graph',),
    }
    for phase, expected in expected_signatures.items():
        assert tuple(inspect.signature(phase).parameters) == expected

    for module in PHASE_MODULES:
        source = inspect.getsource(module)
        tree = ast.parse(source)
        assert not _mutable_module_bindings(source), module.__name__
        assert not [node for node in ast.walk(tree) if isinstance(node, ast.Global)], module.__name__
        imported = {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not any(name.endswith('compiler') for name in imported), module.__name__


@pytest.mark.fast
def test_the_mutable_state_scan_detects_a_planted_breach() -> None:
    """The state scan fails on the ordinary global-dictionary regression."""
    assert _mutable_module_bindings('phase_cache = {}\n') == ('phase_cache',)
