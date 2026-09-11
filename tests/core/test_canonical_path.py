"""T2.6: the canonical path.

Four claims, each delivering one piece of the design doc's compatibility
contract:

  * **P35, path agreement.** A workflow built through the Python API and
    compiled directly must equal the same workflow written with `write_wic`
    and compiled from the file. "Two front ends under one name" is the shape
    a past counterexample had — a `.wic` document and a Python-API-built
    workflow that describe the same DAG must reach the same compiled CWL, or
    the compatibility contract the two front ends advertise is false.
  * **P36, CWL validity.** Compiled output validates under `cwltool`, checked
    in-process and at ten examples rather than the suite's usual hundred —
    see `test_compiled_output_validates_as_cwl`'s own docstring for why.
  * **P36a, compute-payload conformance.** `ComputeRequest` builds and
    validates its own payload from a `CompiledWorkflow`, for everything this
    oracle compiles. No network: build and validate, never submit.
  * **Passthrough fidelity, re-quantified.** `test_leak_boundary.py`'s
    passthrough properties quantify over one-step workflows carrying one key,
    so `maybe_add_requirements` never fires there and a step that scatters
    beside its own passthrough is unreachable by that generator.
    `ast_strategies.freighted_documents` forces exactly that shape.

CONFIRMED DIVERGENCE (P35). `test_the_two_front_ends_compile_to_the_same_cwl`
fails, reliably, and is marked `xfail(strict=True)` rather than adjusted to
avoid it: `Workflow.compile()` builds its in-memory document with
`workflow_document(..., concrete_step_ids=True)`
(`sophios/api/python/_workflow_runtime.py`), which pre-resolves a workflow
output's `outputSource` to the compiler's own concrete step id
(`oracle__step__3__join/file`) before compilation. `Workflow.write_wic()`
calls the same function *without* that flag, so the `.wic` file it writes
carries the bare, unresolved form (`join/file`) instead. The compiler never
resolves this itself — `compile_workflow_finish`
(`sophios/compiler.py:340-343`) takes a user-supplied `outputs:` mapping's
`outputSource` verbatim, with the comment "Assume the user has manually added
the correct namespaced CWL dependency" — so a `.wic` file `write_wic()` writes
is, today, compiled with a broken output reference whenever a workflow output
is declared. A `src/` fix (passing `concrete_step_ids=True` through
`write_workflow_wic`/`workflow_wic_yaml` too) is out of scope here; strict
`xfail` means the day that lands, this test XPASSes and fails loudly until
the marker is removed.

P25 covers this module both statically and under poisoned plugin discovery.
The passthrough alphabets live in `ast_strategies`, their environment-free
owner, so importing them here does not pull in the corpus compile harness.
"""
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sophios.api.python import _workflow_runtime
import sophios.compiler
import sophios.post_compile
from sophios.api.python.workflow import CompiledWorkflow, Step, Workflow
from sophios.cli import default_compilation_settings
from sophios.compute_request import ComputeExecutionConfig, ComputeOutputConfig, ComputeRequest
from sophios.utils_cwl import desugar_into_canonical_normal_form
from sophios.utils_graphs import get_graph_reps
from sophios.utils_yaml import wic_loader
from sophios.wic_types import CompilerInfo, StepId, Yaml, YamlTree

from . import ast_strategies as strat
from .ast_strategies import passthrough_keys, passthrough_values
from .equivalence import Strength, equivalent
from .hermetic import ORACLE, PARTITION, compile_hermetic, compile_hermetic_cwl
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS

# --------------------------------------------------------------------------
# P35: path agreement
# --------------------------------------------------------------------------


def _tool_document(stem: str) -> Yaml:
    """The tool's own CWL document, as `SYNTHETIC_TOOLS` holds it."""
    return dict(SYNTHETIC_TOOLS[StepId(stem, SYNTHETIC_NS)].cwl)


@dataclass(frozen=True, slots=True)
class _PathSpec:
    """Enough to build one small, Python-API-buildable workflow twice: two
    independent File sources feeding one `join` step, exposed as a single
    workflow output.

    Not `ast_strategies.documents()`: the Python API is a *stricter* front
    end than the `.wic` language — `Workflow._validate_graph_shape` rejects
    duplicate step names, links to non-children, and links to later siblings
    — so reusing that generator would draw documents this front end refuses
    to build before path agreement is ever tested. This is that narrower
    generator, declared as this property's own blind spot rather than left
    implicit.
    """

    file_name: str
    text_name: str
    join_name: str


def _build_workflow(spec: _PathSpec) -> Workflow:
    """Build one fresh `Workflow` from `spec`.

    Called twice per example, once per arm, rather than once and reused:
    sharing one `Workflow` object across both arms would make the property
    partly a claim about aliasing rather than about the two front ends.
    """
    mk_file = Step.from_cwl_document(_tool_document('mk_file'), process_name='mk_file',
                                     tool_registry=SYNTHETIC_TOOLS)
    mk_file.inputs.name = spec.file_name
    mk_text = Step.from_cwl_document(_tool_document('mk_text'), process_name='mk_text',
                                     tool_registry=SYNTHETIC_TOOLS)
    mk_text.inputs.name = spec.text_name
    join = Step.from_cwl_document(_tool_document('join'), process_name='join',
                                  tool_registry=SYNTHETIC_TOOLS)
    join.inputs.left = mk_file.outputs.file
    join.inputs.right = mk_text.outputs.text
    join.inputs.name = spec.join_name

    workflow = Workflow([mk_file, mk_text, join], 'oracle')
    workflow.outputs.result = join.outputs.file
    return workflow


def _compile_from_document(document: Yaml, name: str) -> CompilerInfo:
    """Compile a plain YAML document via the same compiler entry point and
    options `sophios.api.python._workflow_runtime.compile_workflow` uses for
    the direct path — built from a document already loaded off disk, rather
    than from `workflow_document(workflow)`.

    Mirrors that call's arguments exactly, `testing=False` included, so the
    only variable between the two arms is the one this property is actually
    about: where the document came from.
    """
    graph = get_graph_reps(name)
    yaml_tree = YamlTree(StepId(name, SYNTHETIC_NS), document)
    compiler_options, graph_settings, yaml_tag_paths = default_compilation_settings()
    return sophios.compiler.compile_workflow(
        yaml_tree, compiler_options, graph_settings, yaml_tag_paths,
        [], [graph], {}, {}, {}, {}, SYNTHETIC_TOOLS, True,
        relative_run_path=True, testing=False)


@pytest.mark.fast
def test_the_written_wic_file_is_a_real_independent_document() -> None:
    """Tautology guard for P35.

    "A test whose assertion is guaranteed true by something other than the
    thing it names is not a test": without this, the path-agreement property
    could hold for free — if `write_wic` did not really produce an
    independent, reloadable document, comparing "compiled directly" against
    "compiled from the file" would just be comparing a workflow with itself
    under a different name. Proves the file exists, is text, and round-trips
    through the loader into a distinct object.
    """
    workflow = _build_workflow(_PathSpec('a', 'b', 'c'))
    with tempfile.TemporaryDirectory() as workdir:
        path = workflow.write_wic(Path(workdir) / 'oracle.wic')
        assert path.exists(), 'write_wic did not write a file'
        text = path.read_text(encoding='utf-8')

    assert isinstance(text, str) and 'steps:' in text, 'the written file is not .wic text'
    loaded = yaml.load(text, Loader=wic_loader())
    assert loaded is not workflow.yaml, 'the reloaded document is the very object write_wic held in memory'
    assert loaded == workflow.yaml, (
        'the file on disk does not even reflect the workflow that wrote it, so any '
        'agreement downstream would prove nothing about the file-based path')


#: A restricted, safe alphabet for the three string-typed literals `_PathSpec`
#: carries. All three bind `string`-typed tool inputs (`mk_file.name`,
#: `mk_text.name`, `join.name`), so there is no scalar-coercion gap to avoid
#: here (contrast `ast_strategies.py`'s own PENDING FINDING, which is about
#: `int`/`float`-typed arguments); restricted anyway to keep this property
#: about path agreement rather than about YAML's more exotic corners.
_safe_text: Final = st.text('abcxyz_', max_size=8)


@st.composite
def _path_specs(draw: st.DrawFn) -> _PathSpec:
    return _PathSpec(draw(_safe_text), draw(_safe_text), draw(_safe_text))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Confirmed divergence, not a harness bug: write_wic() omits the "
        "concrete_step_ids=True that Workflow.compile() uses, so the .wic file "
        "it writes carries an unresolved outputSource ('join/file') where the "
        "direct path already has the compiler's own concrete id "
        "('oracle__step__3__join/file'). The compiler itself never resolves a "
        "user-supplied outputs: mapping's outputSource (compiler.py:340-343). "
        "See this module's own docstring, 'CONFIRMED DIVERGENCE (P35)'."
    ),
)
@pytest.mark.slow
@given(_path_specs())
@PARTITION
def test_the_two_front_ends_compile_to_the_same_cwl(spec: _PathSpec) -> None:
    """P35: path agreement.

    `f` is "write the workflow to a `.wic` file and read it back"; the claim
    is that compiling directly and compiling `f(workflow)` are the same
    compilation. Checked at `Strength.IDENTICAL`, not a bare `==`, so a
    divergence reports *where* the two front ends disagree.

    Built twice from one drawn `spec` (see `_build_workflow`'s own docstring
    for why sharing one object would weaken the claim).

    Currently `xfail(strict=True)`: this property found a real divergence on
    its very first example. See the module docstring's own 'CONFIRMED
    DIVERGENCE (P35)' section for the reproduction; kept `@given`-driven and
    strict rather than reduced to one pinned example, so this stays exercised
    and any partial fix that made even one drawn spec pass would flip it to
    XPASS and demand attention, per this codebase's own "the day a fix lands,
    whoever is standing there removes the entry" discipline.

    BLIND SPOTS: one fixed topology (two File sources into one `join`, one
    workflow output) rather than the full grammar `ast_strategies.documents()`
    covers — see `_PathSpec`'s docstring for why. No subworkflow
    (Workflow-of-Workflow) step, no `scatter`/`when`, no workflow-level input
    reference — the Python API's own richer surface is not exercised here.
    """
    direct = _build_workflow(spec).compile(tool_registry=SYNTHETIC_TOOLS)

    via_file_workflow = _build_workflow(spec)
    with tempfile.TemporaryDirectory() as workdir:
        path = via_file_workflow.write_wic(Path(workdir) / f'{via_file_workflow.process_name}.wic')
        written_text = path.read_text(encoding='utf-8')
    document = desugar_into_canonical_normal_form(yaml.load(written_text, Loader=wic_loader()))
    info = _compile_from_document(document, via_file_workflow.process_name)
    via_file = _workflow_runtime.compiled_workflow_from_compiler_info(via_file_workflow, info)

    found = equivalent(direct.cwl_workflow, via_file.cwl_workflow, Strength.IDENTICAL)
    assert found is None, (
        'the Python API and the .wic file it writes disagree about what this workflow compiles to.\n'
        f'{found}\n\n--- written .wic ---\n{written_text}')

    inputs_found = equivalent(direct.cwl_job_inputs, via_file.cwl_job_inputs, Strength.IDENTICAL)
    assert inputs_found is None, (
        f'the two front ends disagree about the generated job inputs.\n{inputs_found}\n\n'
        f'--- written .wic ---\n{written_text}')


# --------------------------------------------------------------------------
# P36: CWL validity
# --------------------------------------------------------------------------


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
def test_cwltool_validate_rejects_an_invalid_document() -> None:
    """Tautology guard for P36: `cwltool` must be able to say no, or a 0 from
    `test_compiled_output_validates_as_cwl` proves nothing."""
    import cwltool.main  # pylint: disable=import-outside-toplevel  # expensive; slow lane only

    with tempfile.TemporaryDirectory() as workdir:
        target = Path(workdir) / 'invalid.cwl'
        # No `cwlVersion`, no `inputs`/`outputs`: not a document any version
        # of the CWL schema accepts.
        target.write_text(yaml.safe_dump({'class': 'Workflow', 'steps': []}, sort_keys=False),
                          encoding='utf-8')
        assert cwltool.main.main(['--validate', '--quiet', str(target)]) == 1


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(strat.workflows())
# Ten examples, not the suite's usual hundred: `workflows()`'s shape space is
# small (eight stems, a handful of surface forms) and the literal values
# inside a document cannot change whether the emitted document is valid CWL —
# `test_leak_boundary.test_residue_validates_as_cwl_v1_2` learned this at
# about ninety seconds of CI for a strategy with exactly this property. A
# budget set at design time from measurement, not a weakened count.
@settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_compiled_output_validates_as_cwl(yml: Yaml) -> None:
    """P36: `cwltool` agrees every compiled workflow this oracle produces is
    valid CWL v1.2.

    Checked in-process (`cwltool.main.main(['--validate', '--quiet', path])`),
    not a subprocess: confirmed to return 1 for an invalid document by
    `test_cwltool_validate_rejects_an_invalid_document`, so a 0 here is a real
    validity claim rather than an unchecked assumption about the oracle.

    BLIND SPOTS: `workflows()`'s own — no `!cwl`, no `python_script` steps, no
    `NOT_YET_COMPILABLE` exclusions (currently none, see `ast_strategies.py`).
    Validated, never executed: a document that validates can still fail at
    runtime, which is outside what `--validate` checks.
    """
    import cwltool.main  # pylint: disable=import-outside-toplevel  # expensive; slow lane only

    info = compile_hermetic(yml, 'oracle')
    inlined = sophios.post_compile.cwl_inline_runtag(info.rose).data.compiled_cwl

    with tempfile.TemporaryDirectory() as workdir:
        target = Path(workdir) / 'oracle.cwl'
        target.write_text(yaml.safe_dump(inlined, sort_keys=False), encoding='utf-8')
        assert cwltool.main.main(['--validate', '--quiet', str(target)]) == 0


# --------------------------------------------------------------------------
# P36a: compute-payload conformance
# --------------------------------------------------------------------------


@pytest.mark.fast
@given(strat.workflows())
@ORACLE
def test_compute_request_builds_and_validates_every_compiled_workflow(yml: Yaml) -> None:
    """P36a: `ComputeRequest` builds and validates its own payload from a
    `CompiledWorkflow`, for everything this oracle compiles.

    Constructor keyword names and dumped-payload key names come from
    `test_python_api.test_compute_request_accepts_compiled_python_workflow`,
    not from invention here. No network: `to_mapping()`/`to_json()` build and
    validate entirely locally, against the checked-in schema
    (`ComputeRequest`'s own `_validate_compute_request`, which raises
    `ComputeRequestValidationError` on a mismatch); `.submit()` is never
    called.

    BLIND SPOTS: one `ComputeExecutionConfig` shape (`workflow_declared()`
    output mode; no `toilConfig`/`slurmConfig`). The schema itself is a
    checked-in artifact — if it drifts from the real service, this test
    cannot see that.
    """
    info = compile_hermetic(yml, 'oracle')
    compiled = CompiledWorkflow('oracle', info.rose.data.compiled_cwl, info.rose.data.workflow_inputs_file)
    request = ComputeRequest(
        compiled, compute_config=ComputeExecutionConfig(output=ComputeOutputConfig.workflow_declared()))

    mapping = request.to_mapping()
    assert mapping['cwlWorkflow'] == compiled.cwl_workflow
    assert mapping['cwlJobInputs'] == compiled.cwl_job_inputs
    assert mapping['id'] == 'oracle'
    assert json.loads(request.to_json()) == mapping


# --------------------------------------------------------------------------
# Passthrough fidelity, re-quantified over multi-step workflows
# --------------------------------------------------------------------------


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(st.data())
@ORACLE
def test_passthrough_survives_a_scatter_in_a_multi_step_workflow(data: st.DataObject) -> None:
    """Passthrough fidelity, re-quantified over the workflows
    `test_leak_boundary.py`'s single-step generator cannot reach.

    `test_step_passthrough_is_byte_identical` and its siblings quantify over
    `_touch_workflow`'s one, always-non-scattering step, so
    `maybe_add_requirements` never fires there and a step that scatters
    *beside* its own passthrough freight is unreachable by that generator.
    `ast_strategies.freighted_documents` forces exactly that shape; the
    passthrough alphabets are imported from `test_leak_boundary` rather than
    restated, so the next widening of either (`$`, uppercase, a real CWL key)
    reaches this property too, instead of a second copy silently missing it.

    Includes its own tautology guard: `ScatterFeatureRequirement` must appear
    in the compiled `requirements`, proving `maybe_add_requirements` really
    fired for this example rather than this property silently degrading into
    `test_leak_boundary`'s already-covered case.

    BLIND SPOTS: `freighted_documents`'s own (no mapping-form `steps:`, no
    subworkflow, no sidecar). Only one step is ever forced to scatter, and
    only via `scatter` — `when` and a `.wic` subworkflow step are
    `maybe_add_requirements`'s other two triggers, neither exercised here.
    """
    document, index = data.draw(strat.freighted_documents())
    freight = data.draw(st.dictionaries(passthrough_keys, passthrough_values, min_size=1, max_size=4))
    yml = strat.to_yml_with_freight(document, index, freight)

    compiled = compile_hermetic_cwl(yml, 'oracle')

    requirements = compiled.get('requirements') or {}
    assert 'ScatterFeatureRequirement' in requirements, (
        'the designated step did not actually scatter; this example is vacuous for the claim')

    step = compiled['steps'][index]
    for key, value in freight.items():
        assert step[key] == value, f'{key} was altered by compilation'
