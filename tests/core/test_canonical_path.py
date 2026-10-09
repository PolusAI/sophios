"""The canonical path: two front ends, one submittable payload.

Three claims about the compatibility contract the two front ends advertise:

  * **Path agreement.** A workflow built through the Python API and compiled
    directly must equal the same workflow written with `write_wic` and
    compiled from the file. Two spellings of one DAG, one compiled result.
  * **Compute-payload conformance.** `ComputeRequest` builds and validates its
    own payload. No network: build and validate, never submit.
  * **Passthrough fidelity over workflows the narrower generators miss** — a
    step that scatters beside its own passthrough, which one-step documents
    cannot express.

Nothing here is `skip_pypi_ci`, and no job names this file, so that marker
would mean "runs nowhere" rather than "excluded from one lane". CWL validity
is `test_emit.test_emit_validates_as_cwl_v1_2`'s.
"""
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from sophios.api.python import _workflow_runtime
import sophios.compiler
from sophios.api.python.workflow import CompiledWorkflow, Step, Workflow
from sophios.cli import default_compilation_settings
from sophios.compute_request import ComputeExecutionConfig, ComputeOutputConfig, ComputeRequest
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import bundle_from_disk
from sophios.utils_yaml import wic_loader
from sophios.wic_types import StepId, Yaml

from . import ast_strategies as strat
from .ast_strategies import passthrough_keys, passthrough_values
from .equivalence import Strength, equivalent
from .hermetic import ORACLE, PARTITION, compile_hermetic, compile_hermetic_cwl
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS

# --------------------------------------------------------------------------
# Path agreement
# --------------------------------------------------------------------------


def _tool_document(stem: str) -> Yaml:
    """The tool's own CWL document, as `SYNTHETIC_TOOLS` holds it."""
    return dict(SYNTHETIC_TOOLS[StepId(stem, SYNTHETIC_NS)].cwl)


@dataclass(frozen=True, slots=True)
class _BundleSpec:
    """Enough to build one small, Python-API-buildable workflow twice: two
    independent File sources feeding one `join` step, exposed as a single
    workflow output. `rename` names the `join` step apart from its tool, so
    the written document carries `run: join.cwl`; `nest` calls `mk_text`
    through a nested workflow.

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
    rename: bool
    nest: bool


def _build_workflow(spec: _BundleSpec) -> Workflow:
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
    join = Step.from_cwl_document(_tool_document('join'), process_name='joined' if spec.rename else 'join',
                                  run_path='join.cwl', tool_registry=SYNTHETIC_TOOLS)
    join.inputs.left = mk_file.outputs.file
    join.inputs.name = spec.join_name

    text_step: Step | Workflow = mk_text
    if spec.nest:
        inner = Workflow([mk_text], 'inner')
        inner.outputs.text = mk_text.outputs.text
        join.inputs.right = inner.outputs.text
        text_step = inner
    else:
        join.inputs.right = mk_text.outputs.text

    workflow = Workflow([mk_file, text_step, join], 'oracle')
    workflow.outputs.result = join.outputs.file
    return workflow


def _compile_bundle(root: Path) -> CompilationResult:
    """Compile a written bundle through the file door, as the CLI reads it.

    `run:` paths resolve beside the document; nested `.wic` documents are found
    the way the CLI finds them, with the bundle's directory on the search
    paths. Mirrors the compiler call `compile_workflow_result` makes for the
    direct path, `testing=False` included, so the only variable between the
    two arms is where the document came from.
    """
    yml_paths = {'global': {path.stem: path for path in root.parent.glob('*.wic')}}
    return sophios.compiler.compile_source(
        bundle_from_disk(root, yml_paths, SYNTHETIC_TOOLS), default_compilation_settings(),
        relative_run_path=True, testing=False)


@pytest.mark.fast
def test_the_written_wic_file_is_a_real_independent_document() -> None:
    """Tautology guard for path agreement.

    "A test whose assertion is guaranteed true by something other than the
    thing it names is not a test": without this, the path-agreement property
    could hold for free — if `write_wic` did not really produce an
    independent, reloadable document, comparing "compiled directly" against
    "compiled from the file" would just be comparing a workflow with itself
    under a different name. Proves the file exists, is text, and loads into
    the document of the workflow that wrote it.
    """
    workflow = _build_workflow(_BundleSpec('a', 'b', 'c', rename=True, nest=False))
    with tempfile.TemporaryDirectory() as workdir:
        path = workflow.write_wic(Path(workdir) / 'oracle.wic')
        assert path.exists(), 'write_wic did not write a file'
        text = path.read_text(encoding='utf-8')

    assert isinstance(text, str) and 'steps:' in text, 'the written file is not .wic text'
    loaded = yaml.load(text, Loader=wic_loader())
    assert [step['id'] for step in loaded['steps']] == ['mk_file', 'mk_text', 'joined'], (
        'the file on disk does not even reflect the workflow that wrote it, so any '
        'agreement downstream would prove nothing about the file-based path')
    assert loaded['steps'][2]['run'] == 'join.cwl'
    assert loaded['outputs']['result']['outputSource'] == 'joined/file'


#: The three string-typed literals `_BundleSpec` carries, restricted to keep
#: this property about path agreement rather than YAML's exotic corners.
_safe_text: Final = st.text('abcxyz_', max_size=8)


@st.composite
def _bundle_specs(draw: st.DrawFn) -> _BundleSpec:
    return _BundleSpec(draw(_safe_text), draw(_safe_text), draw(_safe_text), draw(st.booleans()),
                       draw(st.booleans()))


@pytest.mark.slow
@given(_bundle_specs())
@PARTITION
def test_the_two_front_ends_compile_to_the_same_cwl(spec: _BundleSpec) -> None:
    """Path agreement.

    `f` is "write the workflow's bundle to disk and read it back through the
    file door"; the claim is that compiling directly and compiling
    `f(workflow)` are the same compilation. Checked at `Strength.IDENTICAL`,
    not a bare `==`, so a divergence reports *where* the two front ends
    disagree.

    Built twice from one drawn `spec` (see `_build_workflow`'s own docstring
    for why sharing one object would weaken the claim).

    BLIND SPOTS: one fixed topology (two File sources into one `join`, one
    workflow output) rather than the full grammar `ast_strategies.documents()`
    covers — see `_BundleSpec`'s docstring for why. No `scatter`/`when`, no
    workflow-level input reference, and nesting only one level deep.
    """
    direct = _build_workflow(spec).compile(tool_registry=SYNTHETIC_TOOLS)

    via_file_workflow = _build_workflow(spec)
    with tempfile.TemporaryDirectory() as workdir:
        root = via_file_workflow.write_wic(workdir)
        written_text = root.read_text(encoding='utf-8')
        info = _compile_bundle(root)
    via_file = _workflow_runtime.compiled_workflow_from_result(via_file_workflow, info)

    found = equivalent(direct.cwl_workflow, via_file.cwl_workflow, Strength.IDENTICAL)
    assert found is None, (
        'the Python API and the bundle it writes disagree about what this workflow compiles to.\n'
        f'{found}\n\n--- written .wic ---\n{written_text}')

    inputs_found = equivalent(direct.cwl_job_inputs, via_file.cwl_job_inputs, Strength.IDENTICAL)
    assert inputs_found is None, (
        f'the two front ends disagree about the generated job inputs.\n{inputs_found}\n\n'
        f'--- written .wic ---\n{written_text}')


# --------------------------------------------------------------------------
# Compute-payload conformance
# --------------------------------------------------------------------------


@pytest.mark.fast
@given(strat.workflows())
@ORACLE
def test_compute_request_builds_and_validates_every_compiled_workflow(yml: Yaml) -> None:
    """`ComputeRequest` builds and validates its own payload from a
    `CompiledWorkflow`, for everything this oracle compiles.

    Constructor keyword names and dumped-payload key names come from
    `test_python_api_workflow.test_compute_request_accepts_compiled_python_workflow`,
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
    compiled = CompiledWorkflow('oracle', info.artifact.cwl, info.artifact.job_inputs)
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


@pytest.mark.slow
@given(st.data())
@ORACLE
def test_passthrough_survives_a_scatter_in_a_multi_step_workflow(data: st.DataObject) -> None:
    """Passthrough fidelity, re-quantified over the workflows
    `test_leak_boundary.py`'s single-step generator cannot reach.

    `test_step_passthrough_is_byte_identical` and its siblings quantify over
    `_touch_workflow`'s one, always-non-scattering step, so the requirement
    merge in `ir/complete.py` never fires there and a step that scatters
    *beside* its own passthrough freight is unreachable by that generator.
    `ast_strategies.freighted_documents` forces exactly that shape; the
    passthrough alphabets are imported from `test_leak_boundary` rather than
    restated, so the next widening of either (`$`, uppercase, a real CWL key)
    reaches this property too, instead of a second copy silently missing it.

    Includes its own tautology guard: `ScatterFeatureRequirement` must appear
    in the compiled `requirements`, proving the requirement merge really
    fired for this example rather than this property silently degrading into
    `test_leak_boundary`'s already-covered case.

    BLIND SPOTS: `freighted_documents`'s own (no mapping-form `steps:`, no
    subworkflow, no sidecar). Only one step is ever forced to scatter, and
    only via `scatter` — `when` and a `.wic` subworkflow step are the
    merge's other two triggers, neither exercised here.
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


def _written_output_source(workflow: Workflow, directory: Path) -> str:
    """The `outputSource` of `result` in the document `write_wic` writes for `workflow`."""
    document = yaml.load(workflow.write_wic(directory).read_text(encoding='utf-8'), Loader=wic_loader())
    return str(document['outputs']['result']['outputSource'])


@pytest.mark.fast
def test_a_renamed_step_still_resolves_the_workflow_output_bound_to_it(tmp_path: Path) -> None:
    """`process_name` is a mutable public attribute, so a bound output may not
    cache it.

    Binding captured the source step's name at bind time and serialization
    indexed the concrete-step mapping with that snapshot, so renaming a step
    after binding raised a bare `KeyError` from inside serialization — on an
    object graph that is still perfectly valid, since the output is bound to
    the same child object it always was. Resolution goes through the source
    parameter's live owner now.
    """
    workflow = _build_workflow(_BundleSpec('a', 'b', 'c', rename=False, nest=False))
    before = _written_output_source(workflow, tmp_path / 'before')

    renamed = next(step for step in workflow.steps if step.process_name == 'join')
    renamed.process_name = 'after'
    after = _written_output_source(workflow, tmp_path / 'after')

    assert before == 'join/file'
    assert after == 'after/file', 'the output did not follow the step it is bound to'


@pytest.mark.fast
@pytest.mark.parametrize('stem', ['oracle', 'pipeline'])
def test_a_written_document_compiles_under_any_file_name(stem: str) -> None:
    """`write_wic` accepts any `*.wic` name, and nothing in the document depends
    on it: outputs name their authored step, and the bundle compiles from disk
    under the name it was saved as."""
    workflow = _build_workflow(_BundleSpec('a', 'b', 'c', rename=True, nest=False))
    with tempfile.TemporaryDirectory() as workdir:
        root = workflow.write_wic(Path(workdir) / f'{stem}.wic')
        document = yaml.load(root.read_text(encoding='utf-8'), Loader=wic_loader())
        compiled = _compile_bundle(root).artifact.cwl

    assert document['outputs']['result']['outputSource'] == 'joined/file'
    assert compiled['outputs']['result']['outputSource'] == f'{stem}__step__3__joined/file'
