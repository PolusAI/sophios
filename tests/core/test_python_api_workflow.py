"""The Python API: a script of objects compiles to the document stated beside it, what the API can prove wrong is
refused as stated, `check()` answers what `compile()` and `run()` would, and `run()` hands the runner what `compile()`
returns; its properties judge types as the language does, name the user's line, and build one step through every
door."""
from contextlib import contextmanager
import copy
import pickle
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
import warnings
from types import SimpleNamespace
from typing import Any, Callable, Final, Iterator, Literal, cast
from unittest.mock import Mock, patch

from hypothesis import example, given, strategies as st
import numpy
import pytest
import yaml

import sophios.api.python._workflow_runtime as python_runtime
import sophios.plugins
import sophios.post_compile
from sophios import preflight
from sophios import run_local
from sophios.backends import RunRequest
from sophios.api.python import nextflow
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import (InvalidInputValueError, InvalidLinkError, InvalidStepError, RunPolicy,
                                         RunResult, RunState, RunStatus, SophiosError, Step, StepInput, Workflow,
                                         WorkflowRunError)
from sophios.lang.diagnostics import lines as printed_lines
from sophios.python_cwl_adapter import import_python_file
from sophios.wic_types import Json, Tools

from .budgets import budget
from .cases import DATA
from .test_setup import corpus_config
from .helpers import ADAPTERS, adapter_step, echo_workflow, no_preflight, python_process
from .wic_reading import read_wic

PYTHON_API: Final = DATA / 'python_api'


def _iter_python_workflow_paths(global_config: Json) -> list[tuple[str, Path]]:
    """Every discovered Python workflow, minus the corpora with their own lanes: the cookbook's scripts are
    test_cookbook.py's, each named for the `.wic` beside it, which writing it out here would overwrite."""
    paths = sophios.plugins.get_workflow_paths(global_config, 'py')
    return [
        (path_str, path)
        for _, paths_dict in paths.items()
        for path_str, path in paths_dict.items()
        if "mm-workflows" not in str(path) and "docs/tutorials/" not in str(path)
        and "docs/cookbook/" not in path.as_posix()
    ]


@contextmanager
def _step_registry_injected(tool_registry: Tools) -> Iterator[None]:
    """Inject a default tool registry into imported Python workflow scripts,
    so they see the patched ``Step``."""
    from sophios.api.python import workflow  # pylint: disable=C0415:import-outside-toplevel

    step_class = workflow.Step

    def step_factory(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("tool_registry", tool_registry)
        return step_class(*args, **kwargs)

    with patch.object(workflow, "Step", step_factory):
        yield


@pytest.mark.cwl_corpus
@pytest.mark.fast
def test_compile_python_workflows() -> None:
    """Import and compile all auto-discovered Python workflow scripts."""
    from sophios.api.python import workflow  # pylint: disable=C0415:import-outside-toplevel

    global_config = corpus_config()
    tools_cwl = sophios.plugins.get_tools_cwl(global_config)
    paths_tuples = _iter_python_workflow_paths(global_config)
    import_errors: list[str] = []
    for path_stem, path in paths_tuples:
        try:
            with _step_registry_injected(tools_cwl):
                module = import_python_file(path_stem, path)
                retval: workflow.Workflow = module.workflow()

            retval.compile()
            retval.write_wic(path.parent)

            config_ci = path.parent / "config_ci.json"
            json_contents = {}
            if config_ci.exists():
                with open(config_ci, mode="r", encoding="utf-8") as r:
                    json_contents = json.load(r)
            fragments: list[str] = json_contents.get("fragments", [])
            subworkflows: list[workflow.Workflow] = retval._flatten_subworkflows()[
                1:]
            fragments += [wf.process_name for wf in subworkflows]
            json_contents["fragments"] = fragments
            with open(config_ci, mode="w", encoding="utf-8") as f:
                json.dump(json_contents, f)

        except Exception as exc:  # pylint: disable=W0718:broad-exception-caught
            import_errors.append(f"{path_stem}: {type(exc).__name__}: {exc}")
            traceback.print_exception(type(exc), value=exc, tb=None)
    if import_errors:
        pytest.fail("Python workflow imports failed:\n" +
                    "\n".join(import_errors))


@pytest.mark.fast
def test_importing_sophios_leaves_the_root_logger_alone(tmp_path: Path) -> None:
    """A library that adds a handler to the root logger on import changes the logging of every program
    that imports it. Each module of the Python API is an attribute of its package, imported on first use."""
    # A new interpreter: pytest puts handlers on the root logger before any test imports Sophios.
    run = python_process('-c', 'import logging\nimport sophios.main\nimport sophios.api.python as api\n'
                         'assert api.workflow.Workflow\nprint(logging.getLogger().handlers)\n', cwd=tmp_path,
                         capture_output=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == '[]'


@pytest.mark.fast
def test_a_script_of_objects_compiles_to_the_document_stated_and_writes_what_compiles_alike(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """tests/core/data/python_api/builder.py compiles to builder.cwl and builder_job.yml beside it, as its comments
    say. The bundle `write_wic` writes names each step's tool file by its stem and never the script, leaves a tool file
    of that stem that is the same tool as it is, and the child declares the interface its parent binds by name
    (tests/core/data/pairs compiles such bundles); each writer of the compiled workflow writes the in-memory documents
    and no `.wic`, `write()` to the current directory by default."""
    builder = import_python_file('builder', PYTHON_API / 'builder.py')
    with pytest.warns(UserWarning, match=r'Prefer workflow\.inputs\.message, or workflow\.inputs\.message\.as_type'):
        workflow = builder.workflow()
    assert getattr(workflow.outputs, '_repr_html_', None) is None  # as IPython's display probes, declaring nothing
    compiled = workflow.compile()
    assert compiled.cwl_workflow == yaml.safe_load((PYTHON_API / 'builder.cwl').read_text(encoding='utf-8'))
    assert compiled.cwl_job_inputs == yaml.safe_load((PYTHON_API / 'builder_job.yml').read_text(encoding='utf-8'))

    root = workflow.write_wic(tmp_path / 'bundle')
    tools = {path: path.read_text(encoding='utf-8') for path in (tmp_path / 'bundle').glob('*.cwl')}
    assert workflow.write_wic(tmp_path / 'bundle') == root  # each tool file of its stem is the same tool, left as is
    assert tools == {path: path.read_text(encoding='utf-8') for path in (tmp_path / 'bundle').glob('*.cwl')}
    written = root.read_text(encoding='utf-8')
    assert [step.get('run', step['id']) for step in read_wic(written)['steps']] == [
        'emit_text.cwl', 'child.wic', 'write_dir.cwl', 'cat.cwl']
    assert '__step__' not in written and 'builder.py' not in written
    assert read_wic((tmp_path / 'bundle' / 'child.wic').read_text(encoding='utf-8'))['inputs'] == {
        'file': {'type': 'File'}}

    monkeypatch.chdir(tmp_path)
    assert (compiled.write_cwl(tmp_path), compiled.write_job_inputs(tmp_path)) == (
        tmp_path / 'builder.cwl', tmp_path / 'builder_inputs.yml')
    assert (yaml.safe_load(compiled.to_cwl_yaml()), yaml.safe_load(compiled.to_job_inputs_yaml())) == (
        compiled.cwl_workflow, compiled.cwl_job_inputs)
    for path, directory in ((None, Path('.')), (tmp_path / 'out', tmp_path / 'out')):
        assert compiled.write(path) == directory
        assert yaml.safe_load((directory / 'builder.cwl').read_text(encoding='utf-8')) == compiled.cwl_workflow
        assert yaml.safe_load((directory / 'builder_inputs.yml').read_text(encoding='utf-8')) == compiled.cwl_job_inputs
        assert not list(directory.glob('*.wic'))


#: Every exception refused.yaml names, by its name.
_RAISED: Final = {error.__name__: error for error in (AttributeError, ValueError, TypeError, SophiosError,
                                                      InvalidLinkError, InvalidStepError, InvalidInputValueError,
                                                      nextflow.NotLowered)}


@pytest.mark.fast
def test_what_the_api_can_prove_wrong_is_refused_as_stated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Each function of tests/core/data/python_api/refused.py raises what refused.yaml states, and its message names
    what it states. A caller writes one `except SophiosError` and reads `.diagnostics`, whatever the compile refused;
    nothing is pulled or started."""
    refused = import_python_file('refused', PYTHON_API / 'refused.py')
    stated = yaml.safe_load((PYTHON_API / 'refused.yaml').read_text(encoding='utf-8'))
    assert set(stated) == {name for name, value in vars(refused).items()
                           if inspect.isfunction(value) and value.__module__ == refused.__name__
                           and not name.startswith('_')}
    prepared = no_preflight(monkeypatch)
    monkeypatch.setattr(python_runtime.run_local, 'start', lambda *_args, **_kwargs: pytest.fail('ran'))
    monkeypatch.chdir(tmp_path)
    for name, expected in stated.items():
        with warnings.catch_warnings(), pytest.raises(_RAISED[expected['raises']]) as caught:
            warnings.simplefilter('ignore')  # raw CWL injection warns before it is refused
            getattr(refused, name)()
        assert type(caught.value).__name__ == expected['raises'], name
        assert [item.code.name for item in getattr(caught.value, 'diagnostics', [])] == (
            [expected['code']] if 'code' in expected else []), name
        assert all(text in str(caught.value) for text in expected['names']), (name, str(caught.value))
    assert not prepared


_FLOATS: Final = st.floats(allow_nan=False, allow_infinity=False)
#: What a port of each type may be bound, numpy's float64 among the floats.
_BINDABLE: Final = {'string': st.text(max_size=5), 'int': st.integers(-2**31, 2**31 - 1),
                    'float': _FLOATS | _FLOATS.map(numpy.float64), 'boolean': st.booleans()}


@st.composite
def _built_tools(draw: st.DrawFn) -> tuple[CommandLineTool, dict[str, Any]]:
    """A tool echoing up to three inputs, and the values bound to those not left to the workflow."""
    names = draw(st.lists(st.sampled_from(['message', 'n', 'size', 'flag']), unique=True, min_size=1, max_size=3))
    types = {name: draw(st.sampled_from(sorted(_BINDABLE))) for name in names}
    bindings: list[dict[str, Any]] = [draw(st.sampled_from([{'position': 1}, {'flag': f'--{name}'}])) for name in types]
    inputs = Inputs(**{name: Input(declared, **binding) for (name, declared), binding in zip(types.items(), bindings)})
    tool = (CommandLineTool(draw(st.sampled_from(['echo_tool', 'emit'])), inputs, Outputs(out=Output.stdout()))
            .base_command('echo').stdout('stdout.txt'))
    return tool, {name: draw(_BINDABLE[declared]) for name, declared in types.items() if draw(st.booleans())}


@pytest.mark.fast
@given(_built_tools(), st.sampled_from([None, 'say']))
@example((CommandLineTool('emit_text', Inputs(message=Input(cwl.string, position=1)),
                          Outputs(file=Output(cwl.file, glob='stdout.txt'))).base_command('echo').stdout('stdout.txt'),
          {'message': 'hello'}), None)  # a built tool becomes a step named for it
@example((CommandLineTool('echo_tool', Inputs(message=Input(cwl.string, position=1)), Outputs(out=Output.stdout()))
          .base_command('echo').stdout('stdout.txt'), {'message': 'hi'}), 'say')  # `step_name` renames it and its path
@budget(15)
def test_a_built_tool_is_one_step_through_every_door(drawn: tuple[CommandLineTool, dict[str, Any]],
                                                     step_name: str | None) -> None:
    """Each in-memory door, binding by attribute or `step_inputs=`, writes nothing and makes the step `Step(clt_path=)`
    makes of the tool's file: named `step_name` or for the tool, with a path of that name, compiling alike; and the
    job holds plain values, a numpy float a Python one."""
    tool, values = drawn
    name = step_name or tool.name
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.chdir(directory)
        steps = [_bind(Step(tool, step_name=step_name), **values), _bind(tool.to_step(step_name=step_name), **values),
                 _bind(Step.from_cwl_document(tool.to_cwl_document(), process_name=step_name), **values),
                 tool.to_step(step_name=step_name, step_inputs=values),
                 Step.from_cwl_document(tool.to_cwl_document(), process_name=step_name, step_inputs=values)]
        assert not list(Path(directory).iterdir())
        steps.append(_bind(Step(clt_path=tool.write_cwl(Path(directory) / f'{name}.cwl')), **values))
        compiled = [Workflow([step], 'wf').compile() for step in steps]
    assert {(step.process_name, step.clt_path.name) for step in steps} == {(name, f'{name}.cwl')}
    assert all((each.cwl_workflow, each.cwl_job_inputs) == (compiled[-1].cwl_workflow, compiled[-1].cwl_job_inputs)
               for each in compiled)
    assert all(type(value) in (str, int, float, bool) for each in compiled for value in each.cwl_job_inputs.values())


def _bind(step: Step, **values: Any) -> Step:
    """`step`, with `values` bound to its inputs."""
    for name, value in values.items():
        setattr(step.inputs, name, value)
    return step


#: Statements of a user's script, `# here` on the line its diagnostic, or the step it makes, must name.
_SPANNED: Final = {
    'literal': ('scale = Step.from_cwl_document(SCALE, process_name="scale")',
                'scale.inputs.n = "not an int"  # here', 'workflow = Workflow([scale], "wf")'),
    'bind': ('touch = Step(clt_path=TOUCH)', 'cat = Step(clt_path=CAT)',
             'cat.inputs.file = [touch.outputs.file]  # here'),
    'order': ('touch = Step(clt_path=TOUCH)', 'cat = Step(clt_path=CAT)  # here',
              'cat.inputs.file = touch.outputs.file', 'workflow = Workflow([cat, touch], "backwards")'),
    'made': ('step = Step(clt_path=TOUCH)  # here',),
}


def _script(kind: str, depth: int, padding: list[str]) -> str:
    lines = [*_SPANNED[kind], 'made.update(locals())']
    for _ in range(depth):
        lines = ['def build():', *(f'    {line}' for line in lines), 'build()']
    return '\n'.join([*padding, *lines]) + '\n'


@pytest.mark.fast
@given(st.sampled_from(sorted(_SPANNED)), st.integers(0, 2),
       st.lists(st.sampled_from(['', '# note', 'pass']), max_size=3),
       st.sampled_from(['<string>', '<ipython-input-3-abc>', 'build.py', str(Path(__file__).with_name('built.py'))]))
@example('literal', 1, [], __file__)  # a literal of the wrong type bound in a function is reported where it is bound
@example('literal', 0, ['import sophios'], 'build.py')  # one bound at a script's top level, at the script's line
@example('bind', 1, [], __file__)  # an error raised while binding names the bind, not a line inside Sophios
@example('order', 1, [], __file__)  # a step listed before the step it reads from, where that step was made
@example('made', 0, [''], '<string>')  # code without a file keeps its pseudo-name, not a path under the cwd
@budget(20)
def test_a_diagnostic_names_the_python_line_that_made_its_object(kind: str, depth: int, padding: list[str],
                                                                 file: str) -> None:
    """A diagnostic names the line of the user's innermost frame that bound the port or made the step, under the
    name its code was compiled with: a path resolved, a pseudo-name such as `<string>` kept."""
    source = _script(kind, depth, padding)
    line = next(number for number, text in enumerate(source.splitlines(), start=1) if text.endswith('# here'))
    made: dict[str, Any] = {}
    names = {'Step': Step, 'Workflow': Workflow, 'TOUCH': ADAPTERS / 'touch.cwl', 'CAT': ADAPTERS / 'cat.cwl',
             'made': made, 'SCALE': {'cwlVersion': 'v1.2', 'class': 'CommandLineTool', 'baseCommand': 'true',
                                     'inputs': {'n': 'int'}, 'outputs': {}}}
    named = file if file.startswith('<') else str(Path(file).resolve())
    if kind == 'made':
        exec(compile(source, file, 'exec'), names)  # pylint: disable=exec-used
        span = made['step']._span  # pylint: disable=protected-access
    else:
        with pytest.raises({'literal': SophiosError, 'bind': InvalidInputValueError,
                            'order': InvalidStepError}[kind]) as caught:
            exec(compile(source, file, 'exec'), names)  # pylint: disable=exec-used
            made['workflow'].compile()
        diagnostic = cast(SophiosError, caught.value).diagnostics[0]
        span = diagnostic.span
        assert kind != 'literal' or str(diagnostic).startswith(f'{named}:{line}:1: error [wic020]')
    assert span is not None and (span.file, span.start_line) == (named, line)


def _noted(_directory: Path) -> Workflow:
    """A guessed edge, noted on either target (`wic043`); a `when` that gives a string, noted on CWL (`wic056`); and
    `cat` running on a `when` that reads an input it does not have, noted on either target (`wic056`)."""
    echo = adapter_step('echo', message='hi')
    echo.when = '$(inputs.message)'
    cat = adapter_step('cat')
    cat.when = '$(inputs.nope)'
    first, second = (adapter_step('touch', name, filename=f'{name}.txt') for name in ('first', 'second'))
    return Workflow([first, second, cat, echo], 'noted')


def _on_a_gpu(_directory: Path) -> Workflow:
    """A CUDA field the Nextflow target does not check, which its lowering notes (`wic046`)."""
    gpu = (CommandLineTool('gpu_tool', Inputs(), Outputs(file=Output(cwl.file, glob='out.txt')))
           .base_command('touch', 'out.txt').gpu(cuda_version_min='11.4'))
    return Workflow([Step(gpu, step_name='gpu')], 'noted')


def _comparing_strings(_directory: Path) -> Workflow:
    """A `when` comparing strings, which the Nextflow target's subset does not do (`nf-s010`)."""
    echo = adapter_step('echo', message='hi')
    echo.when = '$(inputs.message < "27")'
    return Workflow([echo], 'refused')


def _ambiguous(_directory: Path) -> Workflow:
    """Two inputs of one step that inference would both give `touch`'s file: `wic055`, an error."""
    both = (CommandLineTool('both', Inputs(left=Input(cwl.file), right=Input(cwl.file)), Outputs())
            .base_command('true').to_step())
    return Workflow([adapter_step('touch', filename='a.txt'), both], 'ambiguous')


def _reading_an_absent_file(directory: Path) -> Workflow:
    """`cat` of a file bound on the workflow's input, which is not there (`wic016`)."""
    step = Step(clt_path=PYTHON_API / 'cat.cwl')
    workflow = Workflow([step], 'wf')
    step.inputs.f = workflow.inputs.file.as_type('File')
    workflow.inputs.file = str(directory / 'absent.txt')
    return workflow


@pytest.mark.fast
@pytest.mark.parametrize('build, target, policy, codes, refused, compiled', [
    (_noted, 'cwl', RunPolicy(), ['wic043', 'wic056', 'wic056'], False, None),
    (_on_a_gpu, 'nextflow', RunPolicy(), ['wic046'], False, None),
    (_comparing_strings, 'nextflow', RunPolicy(), ['nf-s010'], False, nextflow.NotLowered),
    (_on_a_gpu, 'nextflow', RunPolicy(partial_failure=True), ['nf-s001'], False, nextflow.NotLowered),
    (_ambiguous, 'nextflow', RunPolicy(), ['wic055'], True, SophiosError),
    (_reading_an_absent_file, 'cwl', RunPolicy(), ['wic016'], True, Workflow),
    (_reading_an_absent_file, 'nextflow', RunPolicy(), ['wic016', 'wic029'], True, Workflow),
], ids=['cwl-notes', 'nextflow-notes', 'nextflow-refuses', 'nextflow-refuses-a-policy', 'does-not-compile',
        'absent-input', 'absent-input-and-nextflow'])
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_check_answers_what_compile_and_run_would_and_prints_and_writes_nothing(
        build: Callable[[Path], Workflow], target: Literal['cwl', 'nextflow'], policy: RunPolicy, codes: list[str],
        refused: bool, compiled: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`check()` returns every note `compile()` keeps, a target's refusals among them as errors, which `compile()`
    raises; and it raises what `compile()` raises, and what the pre-flight finds, every problem at once, as `run()`
    does: an input that is not there (`wic016`) and, where the run calls one and there is none, `nextflow`
    (`wic029`). It prints nothing and writes nothing where the base path is; `compile()` prints the notes."""
    monkeypatch.setattr(preflight, 'PLATFORM_LACKS_RUNNERS', None)
    monkeypatch.setattr(preflight, 'runner_program', lambda name: None if name == 'nextflow' and refused else name)
    if not refused:  # a nextflow that is there answers `-version`
        monkeypatch.setattr(preflight, 'nextflow_problems', lambda _settings: [])
    monkeypatch.setattr(preflight, 'engine_problems', lambda _images, _settings: [])
    monkeypatch.chdir(tmp_path)
    workflow = build(tmp_path)
    capsys.readouterr()
    try:
        notes = workflow.check(target=target, policy=policy)
    except SophiosError as error:
        assert [str(item.code) for item in error.diagnostics] == codes and refused, error
        notes = tuple(error.diagnostics)
    else:
        assert [str(note.code) for note in notes] == codes and not refused
    assert tuple(capsys.readouterr()) == ('', '') and not list(tmp_path.iterdir())
    if compiled is None:
        assert workflow.compile(target=target, policy=policy).diagnostics == notes
        assert capsys.readouterr().err.splitlines() == printed_lines(notes)
    elif compiled is not Workflow:  # the compile refuses what the check reports, the target's refusals as errors
        with pytest.raises(compiled) as from_compile:
            workflow.compile(target=target, policy=policy)
        assert [note for note in notes if str(note.severity) == 'error'] == list(from_compile.value.diagnostics)


class _Ended:  # pylint: disable=too-few-public-methods
    """A started run that has completed: for a test of what `run()` does around the run, not of the run."""

    def collect(self) -> RunResult:
        """The completed run's result."""
        return RunResult(RunState.COMPLETED, None, Path('outdir'), Path('run.log'), 0)


@pytest.mark.fast
# pylint: disable-next=too-many-locals  # one run, followed from the documents to the runner
def test_run_writes_what_compile_returns_and_hands_the_runner_its_own_flags(monkeypatch: pytest.MonkeyPatch,
                                                                            tmp_path: Path) -> None:
    """`run()` writes the one document `compile()` returns and its job, and nothing else: a workflow that declares
    its outputs runs with those alone, and a relative path, bound in Python, in a step inputs file (which binds values
    as written) or as a `StepInput` default, names the file in the working directory, absolute. It prepares that
    document in its basepath with docker, the images built without entrypoints when told, every embedded tool's and a
    hint's too; and hands the runner its own flags, never the Python API's: `quiet` is on unless told otherwise and
    never a positional, and any spelling of yes for `generate_run_script` writes run.sh without running."""
    monkeypatch.chdir(tmp_path)
    Path('inputs').mkdir()
    Path('data.txt').write_text('hello\n', encoding='utf-8')
    Path('inputs/cat.yml').write_text('f: {class: File, location: data.txt}\n', encoding='utf-8')
    by_file = Step(clt_path=PYTHON_API / 'cat.cwl', step_inputs_file='inputs/cat.yml')
    by_value, by_default = (Step(clt_path=PYTHON_API / 'cat.cwl', step_name=name) for name in ('read', 'by_default'))
    by_value.inputs.f = 'data.txt'
    by_default.inputs.f = StepInput(default={'class': 'File', 'location': 'data.txt'})
    workflow = Workflow([by_file, by_value, by_default, adapter_step('touch', filename='empty.txt'),
                         Step(clt_path=PYTHON_API / 'hinted.cwl')], 'exact')
    workflow.outputs.printed = by_file.outputs.out
    prepared: list[tuple[list[Path], preflight.RunSettings]] = []
    monkeypatch.setattr(python_runtime.preflight, 'prepare',
                        lambda documents, settings, jobs=(): prepared.append((documents, settings)))
    monkeypatch.setattr(sophios.post_compile.docker, 'from_env',
                        lambda: SimpleNamespace(images=SimpleNamespace(list=lambda: [])))
    for quiet, spelled in ((None, 'yes'), ('yes', 'true'), ('true', '1'), (True, 'on'), ('no', True), (False, 'yes')):
        run_args: dict[str, Any] = {'generate_run_script': spelled, 'docker_remove_entrypoints': 'yes'}
        workflow.run(basepath='base', run_args_dict=run_args | ({} if quiet is None else {'quiet': quiet}))
        command = (tmp_path / 'run.sh').read_text(encoding='utf-8').splitlines()[-1].split()
        assert ('--quiet' in command) is (quiet in (None, 'yes', 'true', True))
        assert not {'yes', 'no', 'true', 'false', 'True', 'False'} & set(command)

    compiled = workflow.compile()
    assert sorted(path.name for path in (tmp_path / 'base').iterdir()) == ['exact.cwl', 'exact_inputs.yml']
    written = yaml.safe_load((tmp_path / 'base' / 'exact.cwl').read_text(encoding='utf-8'))
    assert list(written['outputs']) == ['printed']
    assert [step['run'].get('requirements', step['run'].get('hints', {})).get('DockerRequirement')
            for step in written['steps']] == [None, None, None,
                                              {'dockerPull': 'docker.io/library/debian:bookworm-slim-noentrypoint'},
                                              {'dockerPull': 'docker.io/alpine:3.20-noentrypoint'}]
    here = {'class': 'File', 'location': (tmp_path / 'data.txt').as_uri()}
    assert written['steps'][2]['in']['f']['default'] == here
    assert yaml.safe_load((tmp_path / 'base' / 'exact_inputs.yml').read_text(encoding='utf-8')) == {
        **compiled.cwl_job_inputs, 'cat~f': here, 'read~f': here}
    assert {(tuple(path.resolve() for path in documents), settings.container_engine, settings.ignore_install)
            for documents, settings in prepared} == {(((tmp_path / 'base' / 'exact.cwl').resolve(),), 'docker', False)}

    requests: list[RunRequest] = []

    def run(request: RunRequest) -> int:
        requests.append(request)
        return 0
    monkeypatch.setattr(python_runtime.run_local, 'run', run)
    workflow.run(basepath=str(tmp_path), run_args_dict={'generate_run_script': 'yes', 'logLevel': 'INFO',
                                                        'outdir': str(tmp_path / 'workflow_output')})
    (request,) = requests
    assert (request.passthrough_args, request.generate_run_script, request.outdir, request.workflow_name,
            request.basepath) == (('--logLevel', 'INFO'), True, str(tmp_path / 'workflow_output'), 'exact', tmp_path)


def _missing_paths(monkeypatch: pytest.MonkeyPatch, workflow: Workflow) -> list[str]:
    """The `wic016` lines `workflow.run()` raises, which it must raise before the runner starts."""
    monkeypatch.setattr(python_runtime.run_local, 'start', lambda _request: pytest.fail('ran'))
    with pytest.raises(SophiosError) as caught:
        workflow.run()
    return [str(diagnostic) for diagnostic in caught.value.diagnostics if str(diagnostic.code) == 'wic016']


@pytest.mark.fast
@pytest.mark.parametrize("tool, bound, files, step_inputs_file, inputs_file, fragments", [
    ("cat", {"f": "{cwd}/gone.txt"}, {}, None, None, ["names '{cwd}/gone.txt', which does not exist"]),
    ("cat", {"f": "gone.txt"}, {}, None, None,
     ["whose relative paths are read from {cwd}) names 'gone.txt'", "does not exist at {cwd}/gone.txt"]),
    ("cat", {}, {"cat.yml": "f: {class: File, location: gone.txt}"}, "cat.yml", None, ["names 'gone.txt'"]),
    ("cat", {"f": "gone.txt"}, {"data.txt": "hello", "in.yml": "cat~f: {class: File, location: data.txt}"}, None,
     "in.yml", []),
    ("write_dir", {"outDir": Path("result.outDir")}, {}, None, None, []),
    ("write_dir", {}, {"in.yml": "write_dir~outDir: {class: Directory, location: result.outDir}"}, None, "in.yml", []),
], ids=["absolute", "relative", "in-a-step-inputs-file", "replaced-by-an-inputs-file", "output-directory",
        "output-directory-in-an-inputs-file"])
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_the_pre_flight_checks_exactly_the_paths_the_runner_reads(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool: str, bound: dict[str, Any], files: dict[str, str],
        step_inputs_file: str | None, inputs_file: str | None, fragments: list[str]) -> None:
    """A path bound in Python or in a step inputs file is checked before the run starts, as an inputs file's is, from
    the working directory the run reads it from. A value an inputs file replaces is not the one the runner gets, and
    an output directory names one the step creates: neither is looked for."""
    monkeypatch.chdir(tmp_path)
    for name, text in files.items():
        Path(name).write_text(text, encoding="utf-8")
    step = _bind(Step(clt_path=PYTHON_API / f"{tool}.cwl", step_inputs_file=step_inputs_file),
                 **{port: value.format(cwd=Path.cwd()) if isinstance(value, str) else value
                    for port, value in bound.items()})
    if fragments:
        line, = _missing_paths(monkeypatch, Workflow([step], "wf"))
        assert line.startswith("error [wic016] input 'cat~f' (from the workflow, ")
        assert all(fragment.format(cwd=Path.cwd()) in line for fragment in fragments), line
        return
    start = Mock(return_value=_Ended())
    monkeypatch.setattr(python_runtime.run_local, "start", start)
    Workflow([step], "wf").run(run_args_dict={"inputs_file": inputs_file} if inputs_file else {})
    start.assert_called_once()
    assert not Path("result.outDir").exists(), "an output directory is the step's to make, not the run's"
    if "outDir" in bound:
        job = yaml.safe_load(Path("autogenerated/wf_inputs.yml").read_text(encoding="utf-8"))
        assert job["write_dir~outDir"] == {"class": "Directory", "basename": "result.outDir", "listing": []}


@pytest.mark.fast
def test_run_stops_before_the_runner_or_says_how_the_run_ended(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    """A `basepath` that cannot be created is one `wic021` before the run, not a PermissionError. A failed run raises
    `WorkflowRunError` with the runner's exit code, and the error survives being duplicated, so it can come back from
    a worker process or be re-raised elsewhere; a Ctrl-C is no failed run, so a loop that handles them still stops."""
    no_preflight(monkeypatch)
    if sys.platform != 'win32' and os.geteuid() != 0:  # directory modes stop neither Windows nor root
        locked = tmp_path / 'locked'
        locked.mkdir(mode=0o555)
        with pytest.MonkeyPatch.context() as unstarted, pytest.raises(SophiosError) as caught:
            unstarted.setattr(python_runtime.run_local, 'start', lambda *_args, **_kwargs: pytest.fail('ran'))
            echo_workflow('locked').run(basepath=str(locked / 'autogenerated'))
        (problem,) = caught.value.diagnostics
        assert str(problem.code) == 'wic021' and f'but {locked} is not writable by you' in problem.message

    monkeypatch.setattr(python_runtime.input_output, 'write_artifacts_to_disk', lambda *_a, **_k: None)
    monkeypatch.setattr(run_local.RunHandle, '_start', lambda _handle: None)
    failed = RunStatus(RunState.FAILED, 'the runner exited with status 7', exit_code=7)
    monkeypatch.setattr(run_local.RunHandle, 'wait', lambda _handle, _timeout=None: failed)
    with pytest.raises(WorkflowRunError) as raised:
        echo_workflow('failing').run()
    for clone in (raised.value, pickle.loads(pickle.dumps(raised.value)), copy.copy(raised.value),
                  copy.deepcopy(raised.value)):
        assert isinstance(clone, WorkflowRunError) and str(clone) == str(raised.value)
        assert (str(clone.diagnostics[0].code), clone.exit_code, clone.reason) == (
            'api005', 7, 'the runner exited with status 7') and "'failing'" in str(clone)

    def interrupted(_request: RunRequest) -> _Ended:
        raise KeyboardInterrupt
    monkeypatch.setattr(python_runtime.run_local, 'start', interrupted)
    with pytest.raises(KeyboardInterrupt):
        echo_workflow('interrupted').run()


@pytest.mark.needs_cwltool
def test_a_real_run_prints_its_output_or_names_the_step_that_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                    capfd: pytest.CaptureFixture[str]) -> None:
    """Real runs by cwltool. A user's script, tests/core/data/python_api/say_hello.py, run by a new interpreter, runs
    its workflow and reads the summary the run wrote. A failing step is named with its status, in cwltool's own
    words, which the summary reads: if a cwltool upgrade changes them, this fails, not a user's summary. capfd, not
    capsys: cwltool hands the real stderr's file descriptor to the step."""
    run = python_process(PYTHON_API / 'say_hello.py', cwd=tmp_path, capture_output=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.endswith('hello\n')

    step = Step(clt_path=PYTHON_API / 'exits_3.cwl')
    step.inputs.message = 'x'
    monkeypatch.chdir(tmp_path)
    with pytest.raises(WorkflowRunError):
        Workflow([step], 'exits').run(basepath=str(tmp_path / 'autogenerated'))
    err = capfd.readouterr().err
    assert "Failure! step 'exits_3" in err and 'exited with status 3.' in err
