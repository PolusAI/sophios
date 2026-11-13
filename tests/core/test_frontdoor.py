"""The file front door parses each file as written, so a reported span is a position a reader can open an editor at;
the round-tripped spelling of the same document reports another line. The documents are tests/core/data/frontdoor,
tests/core/data/run_paths and tests/core/data/packed_tools."""
import shutil
from pathlib import Path
from typing import Final

import pytest
import yaml

from sophios.api.python.workflow import Step as ApiStep, Workflow as ApiWorkflow
from sophios.compiler import CompileSettings, compile_source
from sophios.input_output import write_artifacts_to_disk
from sophios.ir.artifacts import CompilationArtifact, CompilationResult
from sophios.ir.frontdoor import SourceBundle, bundle_from_disk, bundle_from_source
from sophios.ir.pipeline import front_end
from sophios.ir.resolve import RegistryKey, RegistrySnapshot, generated_process_id
from sophios.lang import (
    CWL_VERSION,
    Diagnostics,
    Document,
    EdgeDef,
    EdgeRef,
    OutputBinding,
    ParseResult,
    SophiosErrorCode,
    Step,
    parse,
    render,
)
from sophios.lang.diagnostics import Diagnostic, SophiosError
from sophios.plugins import get_tools_cwl
from sophios.wic_types import Yaml

from .cases import DATA, case_dirs, cases
from .synthetic_tools import SYNTHETIC_TOOLS

DOOR: Final = DATA / 'frontdoor'


def _line_of(text: str, needle: str) -> int:
    """The 1-based line of `needle`, read out of the text itself."""
    return next(number for number, line in enumerate(text.splitlines(), start=1)
                if needle in line)


def _compile(bundle: SourceBundle) -> CompilationResult:
    """Compile a bundle the file door built, with default settings."""
    return compile_source(bundle, CompileSettings(), quiet=True)


def _redump(text: str) -> str:
    """The same document written back out, as a YAML round trip hands it to the parser: comments gone."""
    document = parse(text, 'redump.wic').document
    assert document is not None
    return render(document)


def _undefined(diagnostics: Diagnostics) -> list[Diagnostic]:
    return [item for item in diagnostics if item.code is SophiosErrorCode.UNDEFINED_EDGE]


@pytest.mark.fast
# pylint: disable-next=too-many-locals
def test_the_file_door_reads_each_file_as_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The root and each child it calls are parsed from their own bytes, comments and blank lines included, so a
    diagnostic points at the line the construct occupies on disk; the same document through a YAML round trip, what the
    legacy path did, reports a line that exists in the dump and means something else in the file. An in-memory
    document has no span, so the locator is where the edge failed. A `wic:` key the language does not have is refused
    as the file is read, at the key, by the parser, under the file's own name, whatever its extension, with no
    environment involved. The caller's declaration files a child under each namespace a call site declares, and
    nowhere else: a namespace the child declares for itself does not move where its caller looks. A Python script
    step's tool is keyed as Resolve keys it and never reaches disk; it names the EDAM prefix its formats use and no
    ontology, as a document depends only on what its author wrote, at the CWL version Sophios writes.
    `python_cwl_adapter` imports `workflow_types` from a path relative to the working directory, so the project holds
    both that directory and that sibling file; nothing here reads the repository's own examples."""
    text = (DOOR / 'tutorial.wic').read_text(encoding='utf-8')
    bundle = bundle_from_disk(DOOR / 'tutorial.wic', {'global': {}}, SYNTHETIC_TOOLS)
    assert bundle.parsed.document == parse(text, 'tutorial.wic').document and bundle.name == 'tutorial'

    child = DOOR / 'calls' / 'child.wic'
    paths = {space: {'child': child} for space in ('global', 'gpu')}
    bundle = bundle_from_disk(DOOR / 'calls' / 'root.wic', paths, SYNTHETIC_TOOLS)
    for space in ('global', 'gpu'):
        entry = bundle.registry.workflow(RegistryKey(space, 'child'))
        assert entry is not None and entry.parsed.document == parse(child.read_text(encoding='utf-8'),
                                                                    'child.wic').document
        assert entry.parsed.document != parse(_redump(child.read_text(encoding='utf-8')), 'child.wic').document
    assert front_end(bundle.parsed, bundle.registry, name=bundle.name).graph is not None
    own = bundle_from_disk(DOOR / 'own' / 'root.wic', {'global': {'child': DOOR / 'own' / 'child.wic'}},
                           SYNTHETIC_TOOLS).registry
    assert [space for space in ('global', 'gpu') if own.workflow(RegistryKey(space, 'child'))] == ['global']

    forward = (DOOR / 'forward.wic').read_text(encoding='utf-8')
    bundle = bundle_from_disk(DOOR / 'forward.wic', {'global': {}}, SYNTHETIC_TOOLS)
    (undefined,) = _undefined(front_end(bundle.parsed, bundle.registry, name=bundle.name).diagnostics)
    assert undefined.span is not None and undefined.span.start_line == _line_of(forward, '!* later')
    (dumped,) = _undefined(front_end(parse(_redump(forward), 'forward.wic'), bundle.registry,
                                     name=bundle.name).diagnostics)
    assert dumped.span is not None and dumped.span.start_line != undefined.span.start_line
    document = Document(steps=(
        Step('xform', inputs=(('file', EdgeRef('later', None)),)),
        Step('mk_file', outputs=(OutputBinding('file', EdgeDef('later', None), None),)),
    ))
    (spanless,) = _undefined(front_end(ParseResult(document, Diagnostics()),
                                       RegistrySnapshot.from_tools(SYNTHETIC_TOOLS), name='memory').diagnostics)
    assert spanless.span is None and spanless.locator is not None
    assert (spanless.locator.step, spanless.locator.index, spanless.locator.port) == ('xform', 1, 'file')

    for name in ('probe.wic', 'probe.cwl', 'probe.yml'):
        diagnostics = bundle_from_disk(DOOR / 'gate' / name, {'global': {}}, {}).parsed.diagnostics
        assert [(d.code, d.span and (d.span.file, d.span.start_line)) for d in diagnostics] == [
            (SophiosErrorCode.UNKNOWN_WIC_KEY, (name, 2))]

    shutil.copytree(DOOR / 'script', tmp_path / 'script')
    work = tmp_path / 'script' / 'work'
    monkeypatch.chdir(work)
    bundle = bundle_from_disk(work / 'resolve.wic', {'global': {}}, SYNTHETIC_TOOLS)
    resolve = parse((work / 'resolve.wic').read_text(encoding='utf-8'), 'resolve.wic').document
    assert resolve is not None
    expected = RegistryKey('global', generated_process_id(resolve.steps[0]))
    assert bundle.registry.tool(expected) is not None
    result = front_end(bundle.parsed, bundle.registry, name=bundle.name)
    assert result.resolved is not None and result.resolved.document is not None
    process = result.resolved.document.steps[0].process
    assert process.key == expected and process.generated
    assert not (work / 'autogenerated').exists()

    artifact = _compile(bundle_from_disk(work / 'compile.wic', {'global': {}}, SYNTHETIC_TOOLS)).artifact
    (tool,) = artifact.children
    assert tool.cwl['cwlVersion'] == CWL_VERSION, 'the script tool generator once wrote v1.0'
    assert tool.cwl['$namespaces'] == {'edam': 'https://edamontology.org/'}
    assert '$schemas' not in tool.cwl and '$schemas' not in artifact.cwl


def _assert_runs(artifact: CompilationArtifact, written: Yaml, directory: Path, expected: list[Yaml]) -> None:
    """Each step runs the `expected` tool, compiled and in the file its written `run:` names, at `CWL_VERSION`."""
    files: list[str] = []
    for child, step, tool in zip(artifact.children, written['steps'], expected, strict=True):
        files.append((directory / step['run']).read_text(encoding='utf-8'))
        if 'steps' in tool:
            _assert_runs(child, yaml.safe_load(files[-1]), directory, tool['steps'])
            continue
        for cwl in (child.cwl, yaml.safe_load(files[-1])):
            assert (cwl['baseCommand'], cwl['inputs'], cwl['outputs']) == (
                tool['baseCommand'], tool['inputs'], tool.get('outputs', {}))
        assert (step['run'], yaml.safe_load(files[-1])['cwlVersion']) == (tool.get('run', step['run']), CWL_VERSION)
        assert 'same_file_as' not in tool or files[-1] == files[tool['same_file_as']]


@pytest.mark.fast
def test_each_step_runs_the_tool_its_run_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In each project of tests/core/data/run_paths, a `run:` path is read from beside the document that names it,
    else from the registry by its stem; an inline body is a tool of its own. Each tool is the one its step runs,
    compiled and written out. A root with no file of its own reads its `run:` paths from the working directory, and a
    run path on another drive than the root is named by its absolute path, as `os.path.relpath` raises across Windows
    drives."""
    for case, expected in (param.values for param in case_dirs('run_paths', 'steps')):
        result = _compile(bundle_from_disk(case / 'w.wic', {}, SYNTHETIC_TOOLS))
        write_artifacts_to_disk(result.artifact, tmp_path / case.name, output_targets=())
        _assert_runs(result.artifact, yaml.safe_load((tmp_path / case.name / 'w.cwl').read_text(encoding='utf-8')),
                     tmp_path / case.name, expected['steps'])
    paths = DOOR / 'paths'
    monkeypatch.chdir(paths)
    (child,) = _compile(bundle_from_source((paths / 'w.wic').read_text(encoding='utf-8'), 'w', {},
                                           SYNTHETIC_TOOLS)).artifact.children
    assert child.cwl['baseCommand'] == 'echo'

    def across_drives(path: object, start: object = None) -> str:
        raise ValueError("path is on mount 'C:', start on mount 'D:'")
    monkeypatch.setattr('os.path.relpath', across_drives)
    result = _compile(bundle_from_disk(paths / 'w.wic', {}, SYNTHETIC_TOOLS))
    assert result.artifact.children[0].cwl['baseCommand'] == 'echo'


_AS_WIC: Final = ('Sophios cannot embed a CWL Workflow as a step: write it as a .wic subworkflow '
                  '(on search_paths_wic, or beside this document as run: <name>.wic)')
#: The advice for a CWL Workflow in a file, which --allow_raw_cwl can run on its own.
_ADVICE: Final = _AS_WIC + ', or run the CWL Workflow on its own with --allow_raw_cwl'


@pytest.mark.fast
@pytest.mark.parametrize('root, refusal', [
    ('search_paths.wic', "step 'say' runs {adapters}/say.cwl, a CWL Workflow from the tool search paths "
                         f'(search_paths_cwl). {_ADVICE}'),
    ('packed_on_search_paths.wic', "step 'packed' runs {adapters}/packed.cwl, a CWL Workflow from the tool search "
                                   f'paths (search_paths_cwl). {_ADVICE}'),
    ('run_path.wic', f"step 's' runs run: say.cwl, a CWL Workflow. {_ADVICE}"),
    ('inline_body.wic', f"step 's' runs an inline run: body that is a CWL Workflow. {_AS_WIC}"),
])
def test_a_cwl_workflow_as_a_step_is_refused_with_advice_for_where_it_came_from(root: str, refusal: str) -> None:
    """Its copy would lose the tool file beside it, so the step is refused instead of emitted broken. A `$graph`
    whose `main` is a Workflow is one too; an inline body is no file --allow_raw_cwl could run on its own."""
    adapters = DOOR / 'cwl_workflow' / 'adapters'
    tools = get_tools_cwl({'search_paths_cwl': {'global': [str(adapters)]}})
    with pytest.raises(SophiosError) as caught:
        _compile(bundle_from_disk(DOOR / 'cwl_workflow' / root, {}, tools))
    diagnostic, = caught.value.diagnostics
    assert (diagnostic.code, diagnostic.message) == (SophiosErrorCode.SUBWORKFLOW_INVALID,
                                                     refusal.format(adapters=adapters))


def _write_packed_tool_step(tmp_path: Path, where: str, packed: Path) -> Path:
    """Compile a root whose one step runs `packed`, copied `where` (beside the root, or on the tool search paths),
    and write it out; the directory written."""
    directory = tmp_path if where == 'beside' else tmp_path / 'adapters'
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy(packed, directory / 'packed.cwl')
    shutil.copy(DOOR / 'cwl_workflow' / ('run_packed.wic' if where == 'beside' else 'packed_on_search_paths.wic'),
                tmp_path / 'w.wic')
    tools = SYNTHETIC_TOOLS if where == 'beside' else get_tools_cwl({'search_paths_cwl': {'global': [str(directory)]}})
    write_artifacts_to_disk(_compile(bundle_from_disk(tmp_path / 'w.wic', {}, tools)).artifact, tmp_path / 'out',
                            output_targets=())
    return tmp_path / 'out'


def _packed_step(door: str, packed: Path, tmp_path: Path) -> tuple[list[list[list[str]]], Yaml]:
    """The step `door` makes to run `packed`, compiled with every input bound: its ports, each way they can be read,
    and its tool's CWL."""
    if door in ('beside', 'search-paths'):
        out = _write_packed_tool_step(tmp_path, door, packed)
        step, = yaml.safe_load((out / 'w.cwl').read_text(encoding='utf-8'))['steps']
        return [[list(step['in']), step['out']]], yaml.safe_load((out / step['run']).read_text(encoding='utf-8'))
    document = yaml.safe_load(packed.read_text(encoding='utf-8'))
    made = (ApiStep(clt_path=packed) if door == 'clt_path'
            else ApiStep.from_cwl_document(document, process_name='packed'))
    ports = [[port.name for port in made.inputs], [port.name for port in made.outputs]]
    for name in ports[0]:
        setattr(made.inputs, name, 'hi')
    compiled, = ApiWorkflow([made], 'w').compile().cwl_workflow['steps']
    return [ports, [list(compiled['in']), compiled['out']]], made.yaml


@pytest.mark.fast
@pytest.mark.parametrize('door', ['clt_path', 'cwl_document', 'beside', 'search-paths'])
def test_a_packed_document_as_a_step_runs_its_main(tmp_path: Path, door: str) -> None:
    """Each packed CommandLineTool of tests/core/data/packed_tools, a `$graph`'s `main` or the one tool `cwltool --pack`
    writes, is that tool through each door a step has, Python's and the file's: main's ports, and main alone as the
    step's CWL, with the document's `$namespaces` and its `cwlVersion`, or written out, the one we emit. With no
    `main`, the document, as the door names it, is refused with its processes and the fix."""
    for case in (param.values[0] for param in cases('packed_tools', 'ports', 'refused')):
        packed, where = DATA / 'packed_tools' / f"{case['id']}.cwl", tmp_path / case['id']
        if 'refused' in case:
            with pytest.raises(SophiosError) as caught:
                _packed_step(door, packed, where)
            named = {'clt_path': packed, 'cwl_document': 'packed.cwl', 'beside': where / 'packed.cwl',
                     'search-paths': where / 'adapters' / 'packed.cwl'}[door]
            diagnostic, = caught.value.diagnostics
            assert (diagnostic.code, diagnostic.message) == (SophiosErrorCode.SUBWORKFLOW_INVALID,
                                                             f"{named} {case['refused']}"), case['id']
            continue
        ports, tool = _packed_step(door, packed, where)
        document = yaml.safe_load(packed.read_text(encoding='utf-8'))
        assert ports == [case['ports']] * len(ports), case['id']
        assert tool['class'] == 'CommandLineTool' and '$graph' not in tool and 'id' not in tool, case['id']
        assert (tool['cwlVersion'], tool['$namespaces']) == (
            document['cwlVersion'] if door in ('clt_path', 'cwl_document') else CWL_VERSION,
            document['$namespaces']), case['id']


@pytest.mark.needs_cwltool
@pytest.mark.fast
def test_a_packed_tool_step_validates_in_cwltool(tmp_path: Path) -> None:
    """cwltool accepts the workflow and the `main` written for its step."""
    from .cwl_validation import validate_cwl  # pylint: disable=import-outside-toplevel  # no cwltool on Windows
    out = _write_packed_tool_step(tmp_path, 'beside', DATA / 'packed_tools' / 'single-tool.cwl')
    assert validate_cwl(str(out / 'w.cwl'), str(out / 'w_inputs.yml')) == 0
