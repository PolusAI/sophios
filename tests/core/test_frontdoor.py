"""The file front door: each file is parsed as written, not as a regenerated document.

Every claim here is about bytes. A bundle built from disk is the parse of the
user's own text, so a span it reports is a position a reader can open an
editor at. `test_span_names_the_line_in_the_authored_file` is the one that
says so directly: it locates the failing construct by searching the file it
wrote, and pins that the round-tripped spelling of the same document reports a
different line -- which is the defect the front door removes.
"""
from pathlib import Path

import pytest
import yaml

from sophios.api.python.workflow import Step as ApiStep, Workflow as ApiWorkflow
from sophios.cli import default_compilation_settings
from sophios.compiler import compile_source
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
from sophios.lang.diagnostics import SophiosError
from sophios.plugins import get_tools_cwl
from sophios.wic_types import Tools, Yaml

from .cases import DATA, case_dirs, cases
from .synthetic_tools import SYNTHETIC_TOOLS

#: A root whose failing construct sits well below the top of the file, so a
#: reported line that came from a re-dump cannot coincide with the real one.
FORWARD_EDGE = '''# A workflow that references an edge before defining it.
#
# These comment lines are load-bearing: a YAML round trip drops them, which
# is exactly what moves every construct below them onto a different line.
#
steps:
- id: xform
  in:
    file: !* later
    name: out.txt
- id: mk_file
  in:
    name: made.txt
  out:
  - file: !& later
'''

PYTHON_SCRIPT = '''
inputs = {'name': {'type': 'string', 'format': 'edam:format_2330'}}
outputs = {'file': ('$(inputs.name)', {'type': 'File', 'format': 'edam:format_2330'})}


def main(name: str) -> None:
    """A script whose annotations are all the generator reads."""
'''


def _line_of(text: str, needle: str) -> int:
    """The 1-based line of `needle`, read out of the text itself."""
    return next(number for number, line in enumerate(text.splitlines(), start=1)
                if needle in line)


def _compile(bundle: SourceBundle) -> CompilationResult:
    """Compile a bundle the file door built, with default settings."""
    return compile_source(bundle, default_compilation_settings(), testing=True)


def _redump(text: str) -> str:
    """The same document written back out, as a YAML round trip hands it to the parser: comments gone."""
    document = parse(text, 'redump.wic').document
    assert document is not None
    return render(document)


@pytest.mark.fast
def test_source_is_the_file_verbatim(tmp_path: Path) -> None:
    """The root is parsed from its own bytes, comments and blank lines included."""
    text = ('# a leading comment\n'
            '\n'
            'wic:\n'
            '  graphviz:\n'
            '    label: Root\n'
            '\n'
            'steps:\n'
            '- id: mk_file\n'
            '  in:\n'
            '    name: made.txt\n')
    root = tmp_path / 'tutorial.wic'
    root.write_text(text, encoding='utf-8')

    bundle = bundle_from_disk(root, {'global': {}}, SYNTHETIC_TOOLS)

    assert bundle.parsed.document == parse(text, 'tutorial.wic').document
    assert bundle.name == 'tutorial'


@pytest.mark.fast
def test_child_workflow_is_registered_as_its_own_parse(tmp_path: Path) -> None:
    """A called `.wic` enters the registry parsed from the file, not from a re-dump."""
    child_text = ('# the child keeps its comments too\n'
                  'steps:\n'
                  '- id: mk_file\n'
                  '  in:\n'
                  '    name: child.txt\n')
    child = tmp_path / 'child.wic'
    child.write_text(child_text, encoding='utf-8')
    root = tmp_path / 'root.wic'
    root.write_text('steps:\n- id: child.wic\n', encoding='utf-8')

    bundle = bundle_from_disk(root, {'global': {'child': child}}, SYNTHETIC_TOOLS)

    entry = bundle.registry.workflow(RegistryKey('global', 'child'))
    assert entry is not None
    assert entry.parsed.document == parse(child_text, 'child.wic').document
    assert entry.parsed.document != parse(_redump(child_text), 'child.wic').document
    assert front_end(bundle.parsed, bundle.registry, name=bundle.name).graph is not None


@pytest.mark.fast
@pytest.mark.parametrize('root, child, namespaces, registered', [
    # `resolve._resolve_process` builds its `RegistryKey` from the *step's* sidecar namespace, so a child that declares
    # a namespace of its own does not move where its caller looks, and filing it there files it where nothing reads.
    ('steps:\n- id: child.wic\n', 'wic:\n  namespace: gpu\n', ['global'], {'global'}),
    # The traversal skips a path it has already walked, which stops a cycle; a second call site under another
    # namespace needs its own entry from the same text, and suppressing it left Resolve reporting a file as absent
    # from the registry when the front door had read it moments before.
    ('steps:\n- id: child.wic\n- id: child.wic\nwic:\n  steps:\n    (2, child.wic):\n      wic:\n'
     '        namespace: gpu\n', '', ['global', 'gpu'], {'global', 'gpu'}),
], ids=['declared-by-the-child', 'called-twice'])
def test_a_called_file_is_registered_under_each_namespace_its_call_sites_declare(
        tmp_path: Path, root: str, child: str, namespaces: list[str], registered: set[str]) -> None:
    """The caller's declaration files the child, under each namespace a call site declares, and nowhere else."""
    (tmp_path / 'child.wic').write_text(child + 'steps:\n- id: mk_file\n  in:\n    name: c.txt\n', encoding='utf-8')
    (tmp_path / 'root.wic').write_text(root, encoding='utf-8')
    paths = {namespace: {'child': tmp_path / 'child.wic'} for namespace in namespaces}
    registry = bundle_from_disk(tmp_path / 'root.wic', paths, SYNTHETIC_TOOLS).registry
    assert {space for space in ('global', 'gpu') if registry.workflow(RegistryKey(space, 'child'))} == registered


@pytest.mark.fast
def test_span_names_the_line_in_the_authored_file(tmp_path: Path) -> None:
    """A diagnostic points at the line the construct occupies on disk.

    The second assertion is the point of the first: feeding the same document
    through a YAML round trip -- what the legacy path does -- reports a line
    that exists in the dump and means something else in the file.
    """
    root = tmp_path / 'forward.wic'
    root.write_text(FORWARD_EDGE, encoding='utf-8')
    bundle = bundle_from_disk(root, {'global': {}}, SYNTHETIC_TOOLS)

    result = front_end(bundle.parsed, bundle.registry, name=bundle.name)

    undefined = [item for item in result.diagnostics
                 if item.code is SophiosErrorCode.UNDEFINED_EDGE]
    assert len(undefined) == 1
    assert undefined[0].span is not None
    assert undefined[0].span.start_line == _line_of(FORWARD_EDGE, '!* later')

    redumped = parse(_redump(FORWARD_EDGE), 'forward.wic')
    dumped = [item for item in front_end(redumped, bundle.registry, name=bundle.name).diagnostics
              if item.code is SophiosErrorCode.UNDEFINED_EDGE]
    assert len(dumped) == 1
    assert dumped[0].span is not None
    assert dumped[0].span.start_line != undefined[0].span.start_line


@pytest.mark.fast
def test_a_spanless_forward_edge_names_its_consuming_step() -> None:
    """An in-memory document has no span, so the locator is where the edge failed."""
    document = Document(steps=(
        Step('xform', inputs=(('file', EdgeRef('later', None)),)),
        Step('mk_file', outputs=(OutputBinding('file', EdgeDef('later', None), None),)),
    ))
    result = front_end(ParseResult(document, Diagnostics()),
                       RegistrySnapshot.from_tools(SYNTHETIC_TOOLS), name='memory')

    undefined = [item for item in result.diagnostics
                 if item.code is SophiosErrorCode.UNDEFINED_EDGE]
    assert len(undefined) == 1
    assert undefined[0].span is None
    assert undefined[0].locator is not None
    assert undefined[0].locator.step == 'xform'
    assert undefined[0].locator.index == 1
    assert undefined[0].locator.port == 'file'


@pytest.mark.fast
def test_python_script_resolves_without_writing_a_file(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """A generated tool is keyed as Resolve keys it and never reaches disk.

    `python_cwl_adapter` imports `workflow_types` from a path relative to the
    working directory, so the fixture supplies both the directory and that
    sibling file; nothing here reads the repository's own examples.
    """
    types = tmp_path / 'sophios' / 'examples' / 'scripts'
    types.mkdir(parents=True)
    (types / 'workflow_types.py').write_text(
        "string = {'type': 'string', 'format': 'edam:format_2330'}\n", encoding='utf-8')
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'my_script.py').write_text(PYTHON_SCRIPT, encoding='utf-8')
    text = 'steps:\n- id: python_script\n  in:\n    script: my_script.py\n    name: made.txt\n'
    root = work / 'root.wic'
    root.write_text(text, encoding='utf-8')
    monkeypatch.chdir(work)

    bundle = bundle_from_disk(root, {'global': {}}, SYNTHETIC_TOOLS)

    document = parse(text, 'root.wic').document
    assert document is not None
    expected = RegistryKey('global', generated_process_id(document.steps[0]))
    assert bundle.registry.tool(expected) is not None

    result = front_end(bundle.parsed, bundle.registry, name=bundle.name)
    assert result.resolved is not None and result.resolved.document is not None
    process = result.resolved.document.steps[0].process
    assert process.key == expected
    assert process.generated
    assert not (work / 'autogenerated').exists()


@pytest.mark.fast
def test_a_python_script_tool_declares_no_schemas_of_sophios_own(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool generated for a script names the EDAM prefix its formats use, and no ontology: a document
    depends only on what its author wrote."""
    types = tmp_path / 'sophios' / 'examples' / 'scripts'
    types.mkdir(parents=True)
    (types / 'workflow_types.py').write_text(
        "string = {'type': 'string', 'format': 'edam:format_2330'}\n", encoding='utf-8')
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'my_script.py').write_text(PYTHON_SCRIPT, encoding='utf-8')
    (work / 'root.wic').write_text('steps:\n- id: python_script\n  in:\n    script: !ii my_script.py\n'
                                   '    name: !ii made.txt\n', encoding='utf-8')
    monkeypatch.chdir(work)
    artifact = _compile(bundle_from_disk(work / 'root.wic', {'global': {}}, SYNTHETIC_TOOLS)).artifact
    (tool,) = artifact.children
    assert tool.cwl['$namespaces'] == {'edam': 'https://edamontology.org/'}
    assert '$schemas' not in tool.cwl and '$schemas' not in artifact.cwl


@pytest.mark.fast
@pytest.mark.parametrize('name', ['probe.wic', 'probe.cwl', 'probe.yml'])
def test_the_parser_is_the_gate_a_file_passes_as_it_is_read(tmp_path: Path, name: str) -> None:
    """A document with a `wic:` key the language does not have is refused as it is read, at the key, by the parser,
    under the file's own name, whatever its extension.

    A generated jsonschema used to be applied here, and the parser let the key
    through as opaque data. Nothing validates against a schema now, so the
    refusal is the parser's -- positioned, and with no environment involved:
    the registry is empty.
    """
    (tmp_path / name).write_text('wic:\n  nonsense_key: 1\nsteps:\n- id: mk_file\n  in:\n    name: !ii a\n',
                                 encoding='utf-8')
    diagnostics = bundle_from_disk(tmp_path / name, {'global': {}}, {}).parsed.diagnostics
    assert [(d.code, d.span and (d.span.file, d.span.start_line)) for d in diagnostics] == [
        (SophiosErrorCode.UNKNOWN_WIC_KEY, (name, 2))]


def _assert_runs(artifact: CompilationArtifact, written: Yaml, directory: Path, expected: list[Yaml]) -> None:
    """Each step runs the `expected` tool, compiled and in the file its written `run:` names, at `CWL_VERSION`."""
    files: list[str] = []
    for child, step, tool in zip(artifact.children, written['steps'], expected, strict=True):
        files.append((directory / step['run']).read_text(encoding='utf-8'))
        if 'steps' in tool:
            _assert_runs(child, yaml.safe_load(files[-1]), directory, tool['steps'])
            continue
        for cwl in (child.cwl, yaml.safe_load(files[-1])):
            assert (cwl['baseCommand'], cwl['inputs']) == (tool['baseCommand'], tool['inputs'])
        assert (step['run'], yaml.safe_load(files[-1])['cwlVersion']) == (tool.get('run', step['run']), CWL_VERSION)
        assert 'same_file_as' not in tool or files[-1] == files[tool['same_file_as']]


@pytest.mark.fast
@pytest.mark.parametrize('case, expected', case_dirs('run_paths'))
def test_each_step_runs_the_tool_its_run_names(tmp_path: Path, case: Path, expected: Yaml) -> None:
    """A `run:` path is read from beside the document that names it, else from the registry by its stem; an inline
    body is a tool of its own. Each tool is the one its step runs, compiled and written out."""
    result = _compile(bundle_from_disk(case / 'w.wic', {}, SYNTHETIC_TOOLS))
    write_artifacts_to_disk(result.artifact, tmp_path)
    _assert_runs(result.artifact, yaml.safe_load((tmp_path / 'w.cwl').read_text(encoding='utf-8')), tmp_path,
                 expected['steps'])


#: A plain CWL Workflow whose one step runs a tool file beside it.
_CWL_WORKFLOW = ('{class: Workflow, cwlVersion: v1.2, inputs: {text: string}, '
                 'outputs: {said: {type: File, outputSource: echo/out}}, '
                 'steps: {echo: {run: echo.cwl, in: {text: text}, out: [out]}}}')
_ECHO_TOOL = ('cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: echo\n'
              'inputs: {text: {type: string, inputBinding: {position: 1}}}\noutputs: {out: stdout}\n')
_AS_WIC = ('Sophios cannot embed a CWL Workflow as a step: write it as a .wic subworkflow '
           '(on search_paths_wic, or beside this document as run: <name>.wic)')
#: The advice for a CWL Workflow in a file, which --allow_raw_cwl can run on its own.
_ADVICE = _AS_WIC + ', or run the CWL Workflow on its own with --allow_raw_cwl'


def _cwl_workflow_step_diagnostic(tmp_path: Path, step: str, tools: Tools) -> str:
    """Compile a root with `step` as its one step and return the error it is refused with."""
    (tmp_path / 'w.wic').write_text('steps:\n' + step + '    in: {text: !ii hi}\n', encoding='utf-8')
    with pytest.raises(SophiosError) as caught:
        _compile(bundle_from_disk(tmp_path / 'w.wic', {}, tools))
    diagnostic, = caught.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.SUBWORKFLOW_INVALID
    return diagnostic.message


@pytest.mark.fast
@pytest.mark.parametrize('step, refusal', [
    ('  - id: say\n', "step 'say' runs {adapters}/say.cwl, a CWL Workflow from the tool search paths "
                      f'(search_paths_cwl). {_ADVICE}'),
    ('  - id: packed\n', "step 'packed' runs {adapters}/packed.cwl, a CWL Workflow from the tool search paths "
                         f'(search_paths_cwl). {_ADVICE}'),
    ('  - id: s\n    run: say.cwl\n', f"step 's' runs run: say.cwl, a CWL Workflow. {_ADVICE}"),
    (f'  - id: s\n    run: {_CWL_WORKFLOW}\n', f"step 's' runs an inline run: body that is a CWL Workflow. {_AS_WIC}"),
], ids=['search-paths', 'packed-on-search-paths', 'run-path', 'inline-body'])
def test_a_cwl_workflow_as_a_step_is_refused_with_advice_for_where_it_came_from(tmp_path: Path, step: str,
                                                                                refusal: str) -> None:
    """Its copy would lose the tool file beside it, so the step is refused instead of emitted broken. A `$graph`
    whose `main` is a Workflow is one too; an inline body is no file --allow_raw_cwl could run on its own."""
    adapters = tmp_path / 'adapters'
    adapters.mkdir()
    for directory in (tmp_path, adapters):
        (directory / 'say.cwl').write_text(_CWL_WORKFLOW, encoding='utf-8')
        (directory / 'echo.cwl').write_text(_ECHO_TOOL, encoding='utf-8')
    (adapters / 'packed.cwl').write_text(
        'cwlVersion: v1.2\n$graph:\n'
        '  - {id: main, class: Workflow, inputs: {text: string},\n'
        '     outputs: {said: {type: File, outputSource: echo/out}},\n'
        '     steps: {echo: {run: "#echo", in: {text: text}, out: [out]}}}\n'
        '  - {id: echo, class: CommandLineTool, baseCommand: echo, inputs: {text: string}, outputs: {out: stdout}}\n',
        encoding='utf-8')
    tools = get_tools_cwl({'search_paths_cwl': {'global': [str(adapters)]}})
    assert _cwl_workflow_step_diagnostic(tmp_path, step, tools) == refusal.format(adapters=adapters)


def _write_packed_tool_step(tmp_path: Path, where: str, packed: Path) -> Path:
    """Compile a root whose one step runs `packed`, copied `where` (beside the root, or on the tool search paths),
    and write it out; the directory written."""
    directory = tmp_path if where == 'beside' else tmp_path / 'adapters'
    directory.mkdir(exist_ok=True)
    (directory / 'packed.cwl').write_text(packed.read_text(encoding='utf-8'), encoding='utf-8')
    step = '  - id: s\n    run: packed.cwl\n' if where == 'beside' else '  - id: packed\n'
    (tmp_path / 'w.wic').write_text('steps:\n' + step + '    in: {text: !ii hi}\n', encoding='utf-8')
    tools = SYNTHETIC_TOOLS if where == 'beside' else get_tools_cwl({'search_paths_cwl': {'global': [str(directory)]}})
    write_artifacts_to_disk(_compile(bundle_from_disk(tmp_path / 'w.wic', {}, tools)).artifact, tmp_path / 'out')
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
@pytest.mark.parametrize('case', cases('packed_tools'))
def test_a_packed_document_as_a_step_runs_its_main(tmp_path: Path, case: Yaml, door: str) -> None:
    """A packed CommandLineTool, a `$graph`'s `main` or the one tool `cwltool --pack` writes, is that tool through each
    door a step has, Python's and the file's: main's ports, and main alone as the step's CWL, with the document's
    `$namespaces` and its `cwlVersion`, or written out, the one we emit. With no `main`, the document, as the door
    names it, is refused with its processes and the fix."""
    packed = DATA / 'packed_tools' / f"{case['id']}.cwl"
    if 'refused' in case:
        with pytest.raises(SophiosError) as caught:
            _packed_step(door, packed, tmp_path)
        named = {'clt_path': packed, 'cwl_document': 'packed.cwl', 'beside': tmp_path / 'packed.cwl',
                 'search-paths': tmp_path / 'adapters' / 'packed.cwl'}[door]
        diagnostic, = caught.value.diagnostics
        assert (diagnostic.code, diagnostic.message) == (SophiosErrorCode.SUBWORKFLOW_INVALID,
                                                         f"{named} {case['refused']}")
        return
    ports, tool = _packed_step(door, packed, tmp_path)
    document = yaml.safe_load(packed.read_text(encoding='utf-8'))
    assert ports == [case['ports']] * len(ports)
    assert tool['class'] == 'CommandLineTool' and '$graph' not in tool and 'id' not in tool
    assert (tool['cwlVersion'], tool['$namespaces']) == (
        document['cwlVersion'] if door in ('clt_path', 'cwl_document') else CWL_VERSION, document['$namespaces'])


@pytest.mark.needs_cwltool
@pytest.mark.fast
def test_a_packed_tool_step_validates_in_cwltool(tmp_path: Path) -> None:
    """cwltool accepts the workflow and the `main` written for its step."""
    from .cwl_validation import validate_cwl  # pylint: disable=import-outside-toplevel  # no cwltool on Windows
    out = _write_packed_tool_step(tmp_path, 'beside', DATA / 'packed_tools' / 'single-tool.cwl')
    assert validate_cwl(str(out / 'w.cwl'), str(out / 'w_inputs.yml')) == 0


_ECHO = ('{class: CommandLineTool, cwlVersion: v1.2, baseCommand: %s, '
         'inputs: {a: string}, outputs: {}}')


def _tool_file(path: Path, command: str) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text('cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: ' + command
                    + '\ninputs: {a: string}\noutputs: {}\n', encoding='utf-8')


@pytest.mark.fast
def test_a_run_path_on_another_drive_than_the_root_still_compiles(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """`os.path.relpath` raises across Windows drives; the tool is then named by its absolute path."""
    _tool_file(tmp_path / 't.cwl', 'echo')
    (tmp_path / 'w.wic').write_text('steps:\n  - id: s\n    run: t.cwl\n    in: {a: !ii x}\n', encoding='utf-8')

    def across_drives(path: object, start: object = None) -> str:
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr('os.path.relpath', across_drives)
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS))
    assert result.artifact.children[0].cwl['baseCommand'] == 'echo'


@pytest.mark.fast
def test_a_run_path_in_a_source_root_resolves_beside_the_working_directory(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A root with no file of its own reads its `run:` paths from the working directory."""
    (tmp_path / 't.cwl').write_text(_ECHO % 'echo', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    result = _compile(bundle_from_source('steps:\n  - id: s\n    run: t.cwl\n    in: {a: !ii x}\n',
                                         'w', {}, SYNTHETIC_TOOLS))
    child, = result.artifact.children
    assert child.cwl['baseCommand'] == 'echo'
