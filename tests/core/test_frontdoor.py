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

from sophios.cli import default_compilation_settings
from sophios.compiler import compile_source
from sophios.input_output import write_artifacts_to_disk
from sophios.ir.artifacts import CompilationResult
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
from sophios.wic_types import Tools

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
    return compile_source(bundle, default_compilation_settings(), relative_run_path=True, testing=True)


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
def test_the_call_sites_namespace_keys_the_workflow(tmp_path: Path) -> None:
    """The caller's declaration files the child, not the child's own.

    `resolve._resolve_process` builds its `RegistryKey` from the *step's*
    sidecar namespace, so that is the key it will look under. A child that
    declares a different namespace for itself does not move where its caller
    will go looking, and filing it there would file it where nothing reads.
    """
    child = tmp_path / 'child.wic'
    child.write_text('wic:\n  namespace: gpu\nsteps:\n- id: mk_file\n  in:\n    name: c.txt\n',
                     encoding='utf-8')
    root = tmp_path / 'root.wic'
    root.write_text('steps:\n- id: child.wic\n', encoding='utf-8')

    bundle = bundle_from_disk(root, {'global': {'child': child}}, SYNTHETIC_TOOLS)

    assert bundle.registry.workflow(RegistryKey('global', 'child')) is not None
    assert bundle.registry.workflow(RegistryKey('gpu', 'child')) is None


@pytest.mark.fast
def test_one_file_called_twice_is_registered_under_each_namespace(tmp_path: Path) -> None:
    """Reading a file once does not mean filing it once.

    The traversal skips a path it has already walked, which is what stops a
    cycle. The registry is keyed by the namespace the call site declares, so a
    second call site under a different namespace needs its own entry from the
    same text -- and suppressing it left Resolve reporting a file as absent
    from the registry when the front door had read it moments before.
    """
    leaf = tmp_path / 'leaf.wic'
    leaf.write_text('steps:\n- id: mk_file\n  in:\n    name: l.txt\n', encoding='utf-8')
    root = tmp_path / 'root.wic'
    root.write_text('steps:\n- id: leaf.wic\n- id: leaf.wic\n'
                    'wic:\n  steps:\n    (2, leaf.wic):\n      wic:\n        namespace: gpu\n',
                    encoding='utf-8')

    bundle = bundle_from_disk(
        root, {'global': {'leaf': leaf}, 'gpu': {'leaf': leaf}}, SYNTHETIC_TOOLS)

    assert bundle.registry.workflow(RegistryKey('global', 'leaf')) is not None
    assert bundle.registry.workflow(RegistryKey('gpu', 'leaf')) is not None


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
def test_the_parser_is_the_gate_a_file_passes_as_it_is_read(tmp_path: Path) -> None:
    """A document with a `wic:` key the language does not have is refused as
    it is read, at the key, and by the parser.

    A generated jsonschema used to be applied here, and the parser let the key
    through as opaque data. Nothing validates against a schema now, so the
    refusal is the parser's -- positioned, and with no environment involved:
    the registry is empty.
    """
    written = tmp_path / 'probe.wic'
    written.write_text('wic:\n  nonsense_key: 1\nsteps:\n- id: mk_file\n  in:\n    name: !ii a\n',
                       encoding='utf-8')

    diagnostics = bundle_from_disk(written, {'global': {}}, {}).parsed.diagnostics
    assert [(d.code, d.span.start_line if d.span else None) for d in diagnostics] == [
        (SophiosErrorCode.UNKNOWN_WIC_KEY, 2)]


@pytest.mark.fast
@pytest.mark.parametrize('name', ['probe.wic', 'probe.cwl', 'probe.yml'])
def test_a_diagnostic_names_the_file_as_it_is_on_disk(tmp_path: Path, name: str) -> None:
    """A diagnostic's file is the file's own name, whatever its extension."""
    written = tmp_path / name
    written.write_text('wic:\n  nonsense_key: 1\nsteps:\n- id: mk_file\n', encoding='utf-8')

    diagnostics = bundle_from_disk(written, {'global': {}}, {}).parsed.diagnostics
    assert [d.span.file for d in diagnostics if d.span] == [name]


@pytest.mark.fast
def test_an_inline_run_body_is_registered_and_emitted_as_its_own_tool(tmp_path: Path) -> None:
    """An inline `run:` mapping is the step's tool, emitted as its own file."""
    (tmp_path / 'w.wic').write_text(
        'steps:\n  mytool:\n    run:\n      class: CommandLineTool\n      cwlVersion: v1.2\n'
        '      baseCommand: true\n      inputs: {x: string}\n      outputs: {}\n    in:\n      x: !ii hi\n',
        encoding='utf-8')
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, {}))
    step, = result.artifact.cwl['steps']
    assert step['run'].startswith('w__step__1__mytool/mytool_') and step['run'].endswith('.cwl')
    child, = result.artifact.children
    assert child.cwl['inputs'] == {'x': {'type': 'string'}}


@pytest.mark.fast
def test_a_run_path_resolves_relative_to_the_document_before_the_registry(tmp_path: Path) -> None:
    """A `.cwl` beside the document shadows the registry tool of the same stem."""
    (tmp_path / 'tools').mkdir()
    (tmp_path / 'tools' / 'mk_file.cwl').write_text(
        'cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: true\n'
        'inputs: {label: string}\noutputs: {}\n', encoding='utf-8')
    (tmp_path / 'w.wic').write_text('steps:\n  s:\n    run: tools/mk_file.cwl\n    in:\n      label: !ii a\n',
                                    encoding='utf-8')
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS))
    child, = result.artifact.children
    assert 'label' in child.cwl['inputs'] and 'name' not in child.cwl['inputs']


@pytest.mark.fast
def test_a_run_path_that_does_not_exist_falls_back_to_the_registry_stem(tmp_path: Path) -> None:
    """A `run:` path with no file beside the document is looked up by its stem."""
    (tmp_path / 'w.wic').write_text('steps:\n  s:\n    run: elsewhere/mk_file.cwl\n    in:\n      name: !ii a\n',
                                    encoding='utf-8')
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS))
    assert result.artifact.children[0].run_path == '/synthetic/mk_file.cwl'


@pytest.mark.fast
def test_a_run_wic_path_is_read_from_beside_the_document(tmp_path: Path) -> None:
    """A `run: x.wic` path is read from beside the document, not looked up in the search paths."""
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'sub' / 'child.wic').write_text('steps:\n  mk_file:\n    in:\n      name: !ii a\n', encoding='utf-8')
    (tmp_path / 'w.wic').write_text('steps:\n  call:\n    run: sub/child.wic\n', encoding='utf-8')
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS))
    assert result.artifact.children[0].name.startswith('child')


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
def test_a_cwl_workflow_from_the_tool_search_paths_as_a_step_is_refused(tmp_path: Path) -> None:
    """Its copy would lose the tool file beside it, so the step is refused instead of emitted broken."""
    adapters = tmp_path / 'adapters'
    adapters.mkdir()
    (adapters / 'say.cwl').write_text(_CWL_WORKFLOW, encoding='utf-8')
    (adapters / 'echo.cwl').write_text(_ECHO_TOOL, encoding='utf-8')
    tools = get_tools_cwl({'search_paths_cwl': {'global': [str(adapters)]}}, quiet=True)
    message = _cwl_workflow_step_diagnostic(tmp_path, '  - id: say\n', tools)
    assert message == (f"step 'say' runs {adapters / 'say.cwl'}, a CWL Workflow from the "
                       f'tool search paths (search_paths_cwl). {_ADVICE}')


@pytest.mark.fast
def test_a_packed_cwl_workflow_from_the_tool_search_paths_as_a_step_is_refused(tmp_path: Path) -> None:
    """A `$graph` whose `main` is a Workflow has no top-level class, and is refused like any CWL Workflow."""
    adapters = tmp_path / 'adapters'
    adapters.mkdir()
    (adapters / 'packed.cwl').write_text(
        'cwlVersion: v1.2\n$graph:\n'
        '  - {id: main, class: Workflow, inputs: {text: string}, '
        'outputs: {said: {type: File, outputSource: echo/out}}, '
        'steps: {echo: {run: "#echo", in: {text: text}, out: [out]}}}\n'
        '  - {id: echo, class: CommandLineTool, baseCommand: echo, '
        'inputs: {text: string}, outputs: {out: stdout}}\n', encoding='utf-8')
    tools = get_tools_cwl({'search_paths_cwl': {'global': [str(adapters)]}}, quiet=True)
    message = _cwl_workflow_step_diagnostic(tmp_path, '  - id: packed\n', tools)
    assert message == (f"step 'packed' runs {adapters / 'packed.cwl'}, a CWL Workflow from the "
                       f'tool search paths (search_paths_cwl). {_ADVICE}')


#: A packed document whose `main` is a tool, beside a process nothing runs; `%s` is main's id.
_PACKED_TOOL = ('cwlVersion: v1.2\n$namespaces: {edam: https://edamontology.org/}\n$graph:\n'
                '  - id: "%s"\n    class: CommandLineTool\n    baseCommand: echo\n'
                '    inputs: {text: {type: string, inputBinding: {position: 1}}}\n'
                '    outputs: {out: {type: stdout, format: edam:format_1964}}\n'
                '  - {id: other, class: CommandLineTool, baseCommand: "true", inputs: {}, outputs: {}}\n')


def _packed_tool_bundle(tmp_path: Path, where: str, main: str) -> SourceBundle:
    """A root running `packed.cwl`, a packed tool with `main` as its main's id, found `where`: beside the root or
    on the tool search paths."""
    if where == 'beside':
        packed = tmp_path / 'packed.cwl'
        packed.write_text(_PACKED_TOOL % main, encoding='utf-8')
        step = '  - id: s\n    run: packed.cwl\n'
        tools: Tools = SYNTHETIC_TOOLS
    else:
        adapters = tmp_path / 'adapters'
        adapters.mkdir()
        packed = adapters / 'packed.cwl'
        packed.write_text(_PACKED_TOOL % main, encoding='utf-8')
        step = '  - id: packed\n'
        tools = get_tools_cwl({'search_paths_cwl': {'global': [str(adapters)]}}, quiet=True)
    (tmp_path / 'w.wic').write_text('steps:\n' + step + '    in: {text: !ii hi}\n', encoding='utf-8')
    return bundle_from_disk(tmp_path / 'w.wic', {}, tools)


def _write_packed_tool_step(tmp_path: Path, where: str, main: str) -> Path:
    """Compile `_packed_tool_bundle` and write it out; the directory written."""
    result = _compile(_packed_tool_bundle(tmp_path, where, main))
    out = tmp_path / 'autogenerated'
    write_artifacts_to_disk(result.artifact, out, relative_run_path=True)
    return out


@pytest.mark.fast
@pytest.mark.parametrize('where', ['beside', 'search-paths'])
@pytest.mark.parametrize('main', ['main', '#main'])
def test_a_packed_tool_as_a_step_runs_its_main(tmp_path: Path, where: str, main: str) -> None:
    """A `$graph` whose `main` is a CommandLineTool is that tool: the step has main's ports, and the file the
    step runs is main alone, with the document's `cwlVersion` and `$namespaces`."""
    out = _write_packed_tool_step(tmp_path, where, main)
    step, = yaml.safe_load((out / 'w.cwl').read_text(encoding='utf-8'))['steps']
    assert (list(step['in']), step['out']) == (['text'], ['out'])
    tool = yaml.safe_load((out / step['run']).read_text(encoding='utf-8'))
    assert tool['class'] == 'CommandLineTool' and '$graph' not in tool and 'id' not in tool
    assert (tool['cwlVersion'], tool['$namespaces']) == (CWL_VERSION, {'edam': 'https://edamontology.org/'})


@pytest.mark.needs_cwltool
@pytest.mark.fast
def test_a_packed_tool_step_validates_in_cwltool(tmp_path: Path) -> None:
    """cwltool accepts the workflow and the `main` written for its step."""
    from .cwl_validation import validate_cwl  # pylint: disable=import-outside-toplevel  # no cwltool on Windows
    out = _write_packed_tool_step(tmp_path, 'beside', 'main')
    assert validate_cwl(str(out / 'w.cwl'), str(out / 'w_inputs.yml')) == 0


@pytest.mark.fast
@pytest.mark.parametrize('where', ['beside', 'search-paths'])
def test_a_packed_document_with_no_main_is_refused(tmp_path: Path, where: str) -> None:
    """A `$graph` with no `main` names no process to run: the file and its processes are named, with the fix."""
    with pytest.raises(SophiosError) as caught:
        _packed_tool_bundle(tmp_path, where, 'say')
    diagnostic, = caught.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.SUBWORKFLOW_INVALID
    assert diagnostic.message == (
        f'{tmp_path / "packed.cwl" if where == "beside" else tmp_path / "adapters" / "packed.cwl"} is a packed '
        'CWL document ($graph) with no main process; it holds: say, other. CWL runs a packed document through '
        'its main, so name the process to run main, or unpack it into a file of its own')


@pytest.mark.fast
@pytest.mark.parametrize(('run', 'expected'), [
    ('say.cwl', f'run: say.cwl, a CWL Workflow. {_ADVICE}'),
    (_CWL_WORKFLOW, f'an inline run: body that is a CWL Workflow. {_AS_WIC}'),
], ids=['run-path', 'inline-body'])
def test_a_cwl_workflow_a_step_names_itself_is_refused(tmp_path: Path, run: str, expected: str) -> None:
    """A `run:` path beside the document, or an inline body, is refused the same way; an inline
    body is no file --allow_raw_cwl could run on its own."""
    (tmp_path / 'say.cwl').write_text(_CWL_WORKFLOW, encoding='utf-8')
    (tmp_path / 'echo.cwl').write_text(_ECHO_TOOL, encoding='utf-8')
    message = _cwl_workflow_step_diagnostic(tmp_path, f'  - id: s\n    run: {run}\n', SYNTHETIC_TOOLS)
    assert message == f"step 's' runs {expected}"


_ECHO = ('{class: CommandLineTool, cwlVersion: v1.2, baseCommand: %s, '
         'inputs: {a: string}, outputs: {}}')


@pytest.mark.fast
def test_inline_bodies_sharing_a_step_id_are_separate_tools(tmp_path: Path) -> None:
    """Two steps with the same id and different inline bodies each run their own."""
    (tmp_path / 'w.wic').write_text(
        'steps:\n  - id: t\n    run: ' + _ECHO % 'echo' + '\n    in: {a: !ii x}\n'
        '  - id: t\n    run: ' + _ECHO % 'rm' + '\n    in: {a: !ii y}\n', encoding='utf-8')
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS))
    assert [child.cwl['baseCommand'] for child in result.artifact.children] == ['echo', 'rm']


@pytest.mark.fast
@pytest.mark.parametrize('declared', ['', ', cwlVersion: v1.0'])
def test_a_written_inline_run_body_states_the_emitted_cwl_version(tmp_path: Path, declared: str) -> None:
    """A body written out as its own tool states the one version we emit; a version inside it is ignored."""
    body = '{class: CommandLineTool, baseCommand: echo, inputs: {a: string}, outputs: {}' + declared + '}'
    (tmp_path / 'w.wic').write_text('steps:\n  - id: s\n    run: ' + body + '\n    in: {a: !ii x}\n',
                                    encoding='utf-8')
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS))
    out = tmp_path / 'autogenerated'
    write_artifacts_to_disk(result.artifact, out, relative_run_path=True)
    step, = yaml.safe_load((out / 'w.cwl').read_text(encoding='utf-8'))['steps']
    tool = yaml.safe_load((out / step['run']).read_text(encoding='utf-8'))
    assert tool['cwlVersion'] == CWL_VERSION


@pytest.mark.fast
def test_a_version_inside_an_inline_run_body_does_not_name_its_tool(tmp_path: Path) -> None:
    """Bodies that differ only in the cwlVersion they declare are one tool, under one name."""
    body = '{class: CommandLineTool, baseCommand: echo, inputs: {a: string}, outputs: {}%s}'
    (tmp_path / 'w.wic').write_text(
        'steps:\n  - id: t\n    run: ' + body % '' + '\n    in: {a: !ii x}\n'
        '  - id: t\n    run: ' + body % ', cwlVersion: v1.0' + '\n    in: {a: !ii y}\n', encoding='utf-8')
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS))
    out = tmp_path / 'autogenerated'
    write_artifacts_to_disk(result.artifact, out, relative_run_path=True)
    first, second = yaml.safe_load((out / 'w.cwl').read_text(encoding='utf-8'))['steps']
    assert Path(first['run']).name == Path(second['run']).name


@pytest.mark.fast
def test_run_paths_sharing_a_stem_are_separate_tools(tmp_path: Path) -> None:
    """`run: a/t.cwl` and `run: b/t.cwl` each run their own file, and a plain `mk_file` step keeps the registry's."""
    for directory, command in (('a', 'echo'), ('b', 'rm')):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / 't.cwl').write_text(
            'cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: ' + command
            + '\ninputs: {a: string}\noutputs: {}\n', encoding='utf-8')
    (tmp_path / 'w.wic').write_text(
        'steps:\n  - id: s\n    run: a/t.cwl\n    in: {a: !ii x}\n'
        '  - id: s\n    run: b/t.cwl\n    in: {a: !ii y}\n'
        '  - id: mk_file\n    in: {name: !ii z}\n', encoding='utf-8')
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS))
    first, second, plain = result.artifact.children
    assert (first.cwl['baseCommand'], second.cwl['baseCommand']) == ('echo', 'rm')
    assert plain.run_path == '/synthetic/mk_file.cwl'


@pytest.mark.fast
def test_each_written_run_path_tool_is_the_file_its_step_runs(tmp_path: Path) -> None:
    """On disk, each step's `run:` names the file written for it, and a shared stem overwrites nothing."""
    for directory, command in (('a', 'echo'), ('b', 'rm')):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / 't.cwl').write_text(
            'cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: ' + command
            + '\ninputs: {a: string}\noutputs: {}\n', encoding='utf-8')
    (tmp_path / 'w.wic').write_text(
        'steps:\n  - id: s\n    run: a/t.cwl\n    in: {a: !ii x}\n'
        '  - id: s\n    run: b/t.cwl\n    in: {a: !ii y}\n', encoding='utf-8')
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS))
    out = tmp_path / 'autogenerated'
    write_artifacts_to_disk(result.artifact, out, relative_run_path=True)
    written = yaml.safe_load((out / 'w.cwl').read_text(encoding='utf-8'))
    commands = [yaml.safe_load((out / step['run']).read_text(encoding='utf-8'))['baseCommand']
                for step in written['steps']]
    assert commands == ['echo', 'rm']


def _tool_file(path: Path, command: str) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text('cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: ' + command
                    + '\ninputs: {a: string}\noutputs: {}\n', encoding='utf-8')


@pytest.mark.fast
def test_the_same_run_path_beside_two_documents_is_two_tools(tmp_path: Path) -> None:
    """Each document's `run: t.cwl` is the file beside it, so a root `t.cwl` and a `sub/t.cwl` coexist."""
    _tool_file(tmp_path / 't.cwl', 'echo')
    _tool_file(tmp_path / 'sub' / 't.cwl', 'rm')
    (tmp_path / 'sub' / 'child.wic').write_text('steps:\n  - id: s\n    run: t.cwl\n    in: {a: !ii x}\n',
                                                encoding='utf-8')
    (tmp_path / 'w.wic').write_text(
        'steps:\n  - id: s\n    run: t.cwl\n    in: {a: !ii x}\n  - id: c\n    run: sub/child.wic\n',
        encoding='utf-8')
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS))
    root_tool, child = result.artifact.children
    assert root_tool.cwl['baseCommand'] == 'echo'
    assert child.children[0].cwl['baseCommand'] == 'rm'


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
def test_a_run_path_with_no_file_beside_its_document_ignores_another_documents_file(tmp_path: Path) -> None:
    """A child's `sub/mk_file.cwl` is not what a root's unresolved `run: mk_file.cwl` runs: it is the registry's."""
    _tool_file(tmp_path / 'sub' / 'mk_file.cwl', 'rm')
    (tmp_path / 'sub' / 'child.wic').write_text('steps:\n  - id: s\n    run: mk_file.cwl\n    in: {a: !ii x}\n',
                                                encoding='utf-8')
    (tmp_path / 'w.wic').write_text(
        'steps:\n  - id: s\n    run: mk_file.cwl\n    in: {name: !ii x}\n  - id: c\n    run: sub/child.wic\n',
        encoding='utf-8')
    result = _compile(bundle_from_disk(tmp_path / 'w.wic', {}, SYNTHETIC_TOOLS))
    assert result.artifact.children[0].run_path == '/synthetic/mk_file.cwl'


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
