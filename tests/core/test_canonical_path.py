"""Two front ends, one payload: the Python API and the file `write_wic` writes compile to one result, whatever the file
is called and whatever the steps were renamed to after binding."""
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sophios.api.python import _workflow_runtime
import sophios.compiler
from sophios.api.python.workflow import Step, Workflow
from sophios.ir.artifacts import CompilationResult
from sophios.ir.frontdoor import bundle_from_disk
from sophios.wic_types import StepId, Yaml

from .equivalence import Strength, equivalent
from .hermetic import PARTITION
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS


def _tool_document(stem: str) -> Yaml:
    """The tool's own CWL document, as `SYNTHETIC_TOOLS` holds it."""
    return dict(SYNTHETIC_TOOLS[StepId(stem, SYNTHETIC_NS)].cwl)


@dataclass(frozen=True, slots=True)
class _BundleSpec:  # pylint: disable=too-many-instance-attributes  # one field per varied fact
    """A small workflow the Python API can build twice: two File sources feeding a `join` step; `rename`, `nest` and
    `bound` vary its name, a nested call and a bound name, `renamed` renames `join` after its output is bound, and
    `stem` names the file written. Narrower than ast_strategies.documents(): the API refuses duplicate names and links
    to later siblings."""

    file_name: str
    text_name: str
    join_name: str
    rename: bool
    nest: bool
    bound: bool
    renamed: bool
    stem: str


def _build_workflow(spec: _BundleSpec) -> Workflow:
    """Build one fresh `Workflow` from `spec`, once per arm: a shared object would make the property a claim about
    aliasing."""
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
    if spec.bound:
        join.inputs.name = workflow.inputs.name
        workflow.inputs.name = spec.join_name
    if spec.renamed:
        join.process_name = 'after'
    return workflow


def _compile_bundle(root: Path) -> CompilationResult:
    """Compile a written bundle through the file door as the CLI reads it, mirroring compile_workflow_result's call
    (`quiet=False` included) so the only variable between the arms is where the document came from."""
    yml_paths = {'global': {path.stem: path for path in root.parent.glob('*.wic')}}
    return sophios.compiler.compile_source(
        bundle_from_disk(root, yml_paths, SYNTHETIC_TOOLS), sophios.compiler.CompileSettings(),
        quiet=False)


#: The three string-typed literals `_BundleSpec` carries, restricted to keep
#: this property about path agreement rather than YAML's exotic corners.
_safe_text: Final = st.text('abcxyz_', max_size=8)


@st.composite
def _bundle_specs(draw: st.DrawFn) -> _BundleSpec:
    return _BundleSpec(draw(_safe_text), draw(_safe_text), draw(_safe_text), draw(st.booleans()), draw(st.booleans()),
                       draw(st.booleans()), draw(st.booleans()), draw(st.sampled_from(['oracle', 'pipeline'])))


@pytest.mark.slow
@given(_bundle_specs())
@PARTITION
def test_the_two_front_ends_compile_to_the_same_cwl(spec: _BundleSpec) -> None:
    """Path agreement.

    `f` is "write the workflow's bundle to disk and read it back through the
    file door"; the claim is that compiling directly and compiling
    `f(workflow)` are the same compilation, once the values bound on the
    workflow's own inputs, which the bundle leaves open, are merged in as an
    inputs file merges them. Checked at `Strength.IDENTICAL`, not a bare `==`, so a
    divergence reports *where* the two front ends disagree.

    Built twice from one drawn `spec` (see `_build_workflow`'s own docstring
    for why sharing one object would weaken the claim).

    The output is bound to `join` and named by its step's name at compile time, a rename after binding included:
    binding once captured the name, so a rename raised a bare `KeyError` from inside serialization.

    BLIND SPOTS: one fixed topology (two File sources into one `join`, one
    workflow output) rather than the full grammar `ast_strategies.documents()`
    covers — see `_BundleSpec`'s docstring for why. No `scatter`/`when`, a
    workflow-level input only as `bound` makes one, and nesting only one level
    deep.
    """
    direct = _build_workflow(spec).compile(tool_registry=SYNTHETIC_TOOLS)

    via_file_workflow = _build_workflow(spec)
    with tempfile.TemporaryDirectory() as workdir:
        root = via_file_workflow.write_wic(Path(workdir) / f'{spec.stem}.wic')
        written_text = root.read_text(encoding='utf-8')
        info = _compile_bundle(root)
    via_file = _workflow_runtime.compiled_workflow_from_result(via_file_workflow, info)

    step = 'after' if spec.renamed else 'joined' if spec.rename else 'join'
    assert direct.cwl_workflow['outputs']['result']['outputSource'] == f'{step}/file', 'the output lost its step'
    found = equivalent(direct.cwl_workflow, via_file.cwl_workflow, Strength.IDENTICAL)
    assert found is None, (
        'the Python API and the bundle it writes disagree about what this workflow compiles to.\n'
        f'{found}\n\n--- written .wic ---\n{written_text}')

    inputs_file = {'name': spec.join_name} if spec.bound else {}
    assert all(name not in via_file.cwl_job_inputs for name in inputs_file), (
        "the bundle gives a value to an input bound in Python, which is the job's.\n"
        f'--- written .wic ---\n{written_text}')
    inputs_found = equivalent(direct.cwl_job_inputs, {**via_file.cwl_job_inputs, **inputs_file}, Strength.IDENTICAL)
    assert inputs_found is None, (
        f'the two front ends disagree about the generated job inputs.\n{inputs_found}\n\n'
        f'--- written .wic ---\n{written_text}')
