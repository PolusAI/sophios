"""The library reports failures; only the CLI adapter exits: every failure is a coded diagnostic, a run is checked
against this machine before anything is pulled or started, and each scenario of tests/core/data/preflight says what
the command line gives back. A document's own refusals by line, step and port are tests/core/data/compiler/literals;
the no-exit claim's dynamic half is test_emit.py's laws."""
import datetime
import json
import math
import os
import re
import shutil
import string
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from hypothesis import assume, example, given, strategies as st

from sophios import compiler, preflight
from sophios.lang.diagnostics import Diagnostic, Diagnostics, Locator, Severity, SophiosError
from sophios.lang.error_codes import EXPLANATIONS, Kind, SophiosErrorCode
from sophios.lang.spans import SourceSpan

from .budgets import budget
from .cases import case_dirs, match, printed
from .helpers import REPO_ROOT, run_cli
from .hermetic import compile_hermetic, compile_hermetic_source
from .strategies import author_names, pinned
from .synthetic_tools import clt, registered
from .test_lang_parser import FAST

pytestmark = pytest.mark.package

_TYPES: Final = {'int': int, 'long': int, 'float': float, 'double': float, 'boolean': bool, 'string': str}
_TEXTS = st.text('07.e+-:axy', max_size=4)
_READ_BACK: Final[dict[type, Callable[[str], Any]]] = {str: str, int: int, float: float,
                                                       datetime.date: datetime.date.fromisoformat}
_OWN: Final = {int: st.integers(-2**70, 2**70), float: st.floats(), bool: st.booleans(), str: _TEXTS}
_LITERALS = st.recursive(
    st.one_of(st.integers(-2**70, 2**70), st.floats(), st.booleans(), _TEXTS, st.dates(), st.none()),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.sampled_from(['k', 'seen']), inner, max_size=2),
    max_leaves=4)
_PORTS_AND_LITERALS = st.sampled_from(sorted(_TYPES)).flatmap(lambda name: st.tuples(
    st.sampled_from([name, {'type': 'array', 'items': name}]),
    _OWN[_TYPES[name]] | st.lists(_OWN[_TYPES[name]] | st.none(), max_size=3) | _LITERALS))


def _binds(name: str, value: Any) -> bool:
    """Whether a port of the scalar CWL type `name` takes `value`: the JSON type `_TYPES` gives (a bool is no int), an
    int for a float held exactly, and for a string any scalar but null, or a JSON list or mapping."""
    wanted = _TYPES[name]
    if wanted is float and value.__class__ is int:
        return -2**1024 < value < 2**1024 and float(value) == value
    if wanted is str and isinstance(value, (list, dict)):
        try:
            return bool(json.dumps(value))
        except TypeError:
            return False
    return value.__class__ is wanted or wanted is str and isinstance(value, (int, float, datetime.date))


def _job_value(declared: object, literal: object) -> object:
    """The job value `!ii literal` gives the one input of a tool that declares `declared`."""
    tool = clt({'x': {'type': declared, 'inputBinding': {'position': 1}}}, {}, canonical=True)
    compiled = compile_hermetic({'steps': [{'id': 'probe', 'in': {'x': {'wic_inline_input': literal}}}]},
                                tools=registered({'probe': tool}, {}))
    (value,) = compiled.artifact.job_inputs.values()
    return value


@pytest.mark.fast
@given(_PORTS_AND_LITERALS)
@pinned(('int', 2.9), ('int', '007'), ('int', True), ('long', 2.9), ('boolean', 'yes'), ('boolean', 2),
        ('float', '1e3'), ('float', True), ('double', 'x'), ('float', 2**53 + 1), ('float', 10**400),
        ('string', {'seen': datetime.date(2024, 1, 15)}), ({'type': 'array', 'items': 'string'}, ['a', None]),
        ('string', 20), ('string', 1.5), ('string', True), ('string', False), ('string', datetime.date(2024, 1, 15)),
        ('string', {'seen': 'a'}), ('float', 1), ('double', 2**53), ('float', 1.0e-5), ('int', 3), ('long', 3),
        ('boolean', False), ('float', math.nan), ({'type': 'array', 'items': 'int'}, [1, None]), ('string', b'abc'))
@FAST
def test_a_literal_binds_only_as_the_value_it_is(case: Any) -> None:
    """`int('007')` and `bool('yes')` used to succeed, an int above 2**53 rounded silently, and a null inside an array
    raised a bare `TypeError`. A literal binds only as the port type's own value, an exact int into a float, or a
    string's text that reads back to it; else one wic020 names the input, the type and the literal (null, wic012)."""
    declared, value = case
    name = declared['items'] if isinstance(declared, dict) else declared
    items = value if isinstance(declared, dict) and isinstance(value, list) else [value]
    refused = [item for item in items if not _binds(name, item)]
    try:
        bound: Any = _job_value(declared, value)
    except SophiosError as caught:
        [diagnostic] = list(caught.diagnostics)
        assert refused and diagnostic.code is (SophiosErrorCode.LITERAL_TYPE_MISMATCH if value is not None
                                               else SophiosErrorCode.MISSING_REQUIRED_INPUT), diagnostic
        assert value is None or all(repr(part) in diagnostic.message for part in ('x', name, refused[0])), diagnostic
        assert (refused[0].__class__ is not int or _TYPES[name] is not float
                or 'cannot hold exactly' in diagnostic.message), diagnostic
        return
    assert value is not None and not refused, bound
    for item, held in zip(items, bound if isinstance(declared, dict) else [bound]):
        if _TYPES[name] is str:
            assert isinstance(held, str) and repr(_READ_BACK.get(type(item), json.loads)(held)) == repr(item), held
        else:
            assert held.__class__ is _TYPES[name] and repr(held) == repr(_TYPES[name](item)), held


@pytest.mark.fast
@given(st.from_regex(r'[-+]?[0-9]+(\.[0-9]*)?[eE][-+]?[0-9]+', fullmatch=True))
@pinned('1e-5', '1E3', '2.5e10')
@example('.5e3').xfail(raises=AssertionError, reason='no advice for a mantissa with no digit before its point')
@example('1_0e3').xfail(raises=AssertionError, reason='no advice for a mantissa with an underscore')
@FAST
def test_scientific_notation_yaml_reads_as_text_says_how_to_write_a_float(text: str) -> None:
    """YAML reads a float with no signed exponent as text, so it is wic020. The message gives the spelling YAML, and
    so the parser, reads as a float, with the value the author meant: the same float, infinity included."""
    tool = clt({'x': {'type': 'float', 'inputBinding': {'position': 1}}}, {}, canonical=True)
    assume(isinstance(yaml.safe_load(text), str))
    with pytest.raises(SophiosError) as caught:
        compile_hermetic_source(f'steps:\n- id: probe\n  in:\n    x: !ii {text}\n',
                                tools=registered({'probe': tool}, {}))
    advised = re.search(r'Write (\S+)\.$', caught.value.diagnostics[0].message)
    assert advised is not None and yaml.safe_load(advised[1]) == float(text), caught.value.diagnostics[0].message


_SPANS = st.builds(SourceSpan, st.text('aw.', min_size=1, max_size=4), *[st.integers(1, 9)] * 4)


@pytest.mark.fast
@given(st.sampled_from(Severity), st.sampled_from(SophiosErrorCode), st.text(max_size=8), st.none() | _SPANS,
       st.none() | st.builds(Locator, st.text('ab', min_size=1, max_size=2), st.none() | st.integers(1, 9),
                             st.none() | st.text('fp', min_size=1, max_size=2)))
@example(Severity.ERROR, SophiosErrorCode.MISSING_INPUT_FILE, 'gone.txt missing', None, None)
@example(Severity.NOTE, SophiosErrorCode.UNRESOLVED_INPUT, 'worth knowing', None, None)
@example(Severity.NOTE, SophiosErrorCode.INFERENCE_GUESS, 'cat.file <- touch.file (1 other fits)',
         SourceSpan('w.wic', 2, 7, 2, 9), Locator(step='cat', index=3, port='file'))
@FAST
def test_a_diagnostic_is_one_json_object_it_can_be_rebuilt_from(
        severity: Severity, code: SophiosErrorCode, message: str, span: SourceSpan | None,
        locator: Locator | None) -> None:
    """The object `--diagnostics json` prints has every key docs/cli-reference.md lists, null where Sophios does not
    know the value, so a reader never tests for a key; it carries the position, the step and port, and the catalog's
    kind and fix, so a reader rebuilds the diagnostic from it. The text form reads where, how serious, the code and
    the message, and a compile-phase failure that knows no line invents none. A note does not fail anything; an
    error holds at least one diagnostic, and says each of them."""
    diagnostics = Diagnostics()
    (diagnostics.error if severity is Severity.ERROR else diagnostics.note)(code, message, span, locator)
    [found] = list(diagnostics)
    written = json.loads(json.dumps(found.to_json()))
    assert set(written) == {'severity', 'code', 'kind', 'message', 'fix', 'file', 'line', 'column', 'end_line',
                            'end_column', 'step', 'index', 'port'}
    assert (written['kind'], written['fix']) == (str(code.explanation.kind), code.explanation.fix)
    where = [written[key] for key in ('file', 'line', 'column', 'end_line', 'end_column')]
    assert Diagnostic(Severity(written['severity']), SophiosErrorCode(written['code']), written['message'],
                      None if where[0] is None else SourceSpan(*where),
                      None if written['step'] is None else Locator(written['step'], written['index'], written['port'])
                      ) == found
    said = f'{where[0]}:{where[1]}:{where[2]}: ' * (span is not None) + f'{severity} [{code}] {message}'
    assert str(found) == said if locator is None else str(found).startswith(said)
    assert diagnostics.has_errors is (severity is Severity.ERROR)
    assert found.message in str(SophiosError([found]))
    pytest.raises(ValueError, SophiosError, [])


#: Where a document writes a name, as text with a `{name}` slot, and what the refusal calls it.
_NAMED: Final = {
    'step id': "steps:\n- id: mk_file\n  in:\n    name: !ii a\n- id: '{name}'\n  run: mk_file\n  in:\n"
               "    name: !ii b\n",
    'mapping step id': "steps:\n  mk_file:\n    in:\n      name: !ii a\n  '{name}':\n    run: mk_file\n    in:\n"
                       "      name: !ii b\n",
    'workflow input': "inputs:\n  '{name}': string\nsteps:\n- id: mk_file\n  in:\n    name: !ii a\n",
    'workflow output': "outputs:\n  '{name}': {{outputSource: mk_file/file}}\nsteps:\n- id: mk_file\n  in:\n"
                       "    name: !ii a\n",
    'workflow name': "steps:\n- id: mk_file\n  in:\n    name: !ii a\n",
}


@pytest.mark.fast
@given(st.tuples(st.sampled_from(sorted(_NAMED)), author_names))
@pinned(*[(where, name) for where in ('step id', 'mapping step id')
          for name in ['a~b', 'echo@2', 'a/b', 'a b', 'a#b', '.', '..', '.hidden', 'con', 'aux', 'nul', 'com1']],
        *[(where, name) for where in ('workflow input', 'workflow output', 'workflow name')
          for name in ['a~b', '.hidden']])
@budget(100)
def test_a_step_id_outside_the_allowed_characters_is_refused_at_its_line(case: tuple[str, str]) -> None:
    """A step id is part of the compiled ids, of the names made with `~` and `@`, and of a file name; a workflow input
    or output is an id of the compiled CWL as a step is, and a workflow's name the stem of the files it is written to.
    So each is letters, digits, `_`, `.` and `-`, not starting with `.` (docs/wic-reference.md), or one wic045 at the
    step's line naming it; else it compiles as written. A step named like a Windows device (`con`, `aux`) compiles:
    its file name starts with its workflow's."""
    where, name = case
    try:
        compiled = compile_hermetic_source(_NAMED[where].format(name=name), name if where == 'workflow name' else 'w')
    except SophiosError as caught:
        [found] = list(caught.diagnostics)
        assert name[0] == '.' or set(name) - set(string.ascii_letters + string.digits + '_.-'), name
        assert found.code is SophiosErrorCode.INVALID_NAME, found
        assert f"{where.removeprefix('mapping ')} {name!r} is not a name Sophios can write" in found.message, found
        assert found.span is not None and found.span.file.endswith('.wic'), found
        assert where not in ('step id', 'mapping step id') or (found.span.start_line, found.span.start_column) == (
            5, 3), found
        return
    assert name[0] != '.' and not set(name) - set(string.ascii_letters + string.digits + '_.-'), name
    assert where not in ('step id', 'mapping step id') or [
        step['id'] for step in compiled.artifact.cwl['steps']] == ['mk_file', name]


_ROW = re.compile(r'^\| `(?P<code>[a-z]{3}\d{3}|nf-s\d{3})` \| (?P<kind>[a-z]+) \| (?P<meaning>.+) \| (?P<fix>.+) \|$')


@pytest.mark.fast
def test_every_code_is_explained_alike_by_the_catalog_its_page_and_explain(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A code an agent cannot look up is a code it cannot act on. Each code has a meaning and a fix of one line, and
    docs/error_codes.md and `sophios --explain <CODE>`, in any case, with no workflow, say what the catalog says. A
    document's mistake carries a language code and a call's mistake does not, so a caller can route on `is_language`
    and land on the right reference."""
    assert set(EXPLANATIONS) == set(SophiosErrorCode)
    for code, explanation in EXPLANATIONS.items():
        text = explanation.meaning + explanation.fix
        assert explanation.meaning and explanation.fix and '|' not in text and '\n' not in text, code
        if explanation.kind in (Kind.DOCUMENT, Kind.CALL):
            assert code.is_language == (explanation.kind is Kind.DOCUMENT), code
        assert run_cli(monkeypatch, '--explain', str(code).upper()) == 0
        said = f'{code} ({explanation.kind}): {explanation.meaning}\nFix: {explanation.fix}\n'
        assert capsys.readouterr().out == said
    page = REPO_ROOT / 'docs' / 'error_codes.md'
    rows = {found['code']: (found['kind'], found['meaning'], found['fix'])
            for found in map(_ROW.match, page.read_text(encoding='utf-8').splitlines()) if found}
    assert rows == {str(code): (str(e.kind), e.meaning, e.fix) for code, e in EXPLANATIONS.items()}


class _Machine:
    """This machine as a scenario's `machine` describes it: every program on PATH but those `missing`; `docker
    info` and `docker` itself `running`, `missing` or exiting 1 having `said` something; `processes` Docker Desktop
    processes; each command starting with one of `fails` exiting 1 having `said` it; `nextflow -version` exiting
    `nextflow_exit` having `said` it; `DOCKER_HOST` as `docker_host` gives it, and no default socket; each of
    `unreadable` mode 0; Windows when `windows` says so; the compile crashing when `crash` says so; and a dockerLoad
    URL downloaded. Every command run is kept, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, root: Path, spec: dict[str, Any]) -> None:
        self.spec = {'engine': 'running', 'said': '', 'processes': 0, 'missing': [], 'fails': [], **spec}
        self.commands: list[list[str]] = []
        monkeypatch.setattr(subprocess, 'run', self.run)
        monkeypatch.setattr(sys, 'platform', 'linux')
        monkeypatch.setattr(shutil, 'which', lambda name, *_a, **_k: (
            None if name in self.spec['missing'] else f'/usr/bin/{name}'))
        monkeypatch.setattr(preflight, 'PLATFORM_LACKS_RUNNERS', preflight.PLATFORM_LACKS_RUNNERS
                            if self.spec.get('windows') is None else self.spec['windows'])
        monkeypatch.setattr(preflight, 'DEFAULT_DOCKER_SOCKET', root / 'absent.sock')
        monkeypatch.delenv('DOCKER_HOST', raising=False)
        if 'docker_host' in self.spec:
            monkeypatch.setenv('DOCKER_HOST', self.spec['docker_host'])
        for name in self.spec.get('unreadable', []):
            (root / name).chmod(0)
        if self.spec.get('crash'):
            monkeypatch.setattr(compiler, 'compile_source', self.crash)
        monkeypatch.setattr(preflight.requests, 'get', lambda *_a, **_k: _Download())

    @staticmethod
    def crash(*_args: object, **_kwargs: object) -> None:
        """A failure Sophios did not expect."""
        raise RuntimeError('boom')

    def run(self, cmd: list[str], *_args: object, **options: object) -> subprocess.CompletedProcess[Any]:
        """Each command as this machine answers it."""
        self.commands.append(list(map(str, cmd)))
        code, said = 0, ''
        if cmd[0] == 'pgrep':
            said = '1\n' * self.spec['processes']
        elif cmd[0] == 'docker' and self.spec['engine'] == 'missing':
            raise FileNotFoundError('docker')
        elif cmd[:2] == ['docker', 'info'] and self.spec['engine'] == 'fails':
            code, said = 1, self.spec['said']
        elif cmd[1:] == ['-version']:
            code, said = self.spec.get('nextflow_exit', 0), self.spec['said']
        elif any(cmd[:len(prefix)] == prefix for prefix in self.spec['fails']):
            code, said = 1, self.spec['said']
        text = said if options.get('text') else said.encode()
        return subprocess.CompletedProcess(cmd, code, stdout=text, stderr=text)

    def pulled(self) -> bool:
        """Whether an image was pulled."""
        return any(cmd[0] == 'cwl-docker-extract' for cmd in self.commands)


class _Download:
    """A dockerLoad URL's answer: the image, in one chunk."""

    def __enter__(self) -> '_Download':
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        """It succeeded."""

    def iter_content(self, _size: int) -> list[bytes]:
        """The body."""
        return [b'image']


def _files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


@pytest.mark.fast
@pytest.mark.parametrize('scenario, expected', case_dirs('preflight', 'runs'))
# pylint: disable-next=too-many-locals
def test_the_command_line_on_this_machine_does_what_its_scenario_states(
        scenario: Path, expected: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Each run of a scenario of tests/core/data/preflight (see its README), in a copy of the scenario, on the
    machine it describes: what the command line exits with, the diagnostics it reports, what it prints, asks of the
    engine, pulls, fetches, writes, leaves alone and never makes. Nothing is pulled or started once a check has
    failed."""
    for number, run in enumerate(expected['runs'], start=1):
        root = tmp_path / str(number)
        shutil.copytree(scenario, root, ignore=shutil.ignore_patterns('expected.yaml'))
        if run.get('posix_user') and (sys.platform == 'win32' or os.geteuid() == 0):
            continue   # file modes stop neither Windows nor root
        for path in root.rglob('*'):
            if path.suffix in ('.wic', '.yml', '.json'):
                path.write_text(path.read_text(encoding='utf-8').replace('{tmp_uri}', root.as_uri()), encoding='utf-8')
        cwd = root / run.get('cwd', '')
        cwd.mkdir(exist_ok=True)
        if run.get('cwd'):
            cwd.chmod(0o555)
        monkeypatch.chdir(cwd)
        before = _files(root)
        with monkeypatch.context() as machine_patches:
            machine = _Machine(machine_patches, root, _placed(run.get('machine', {}), root))
            code = run_cli(monkeypatch, *_placed(run['argv'], root))
        for path in (cwd, *(root / name for name in run.get('machine', {}).get('unreadable', []))):
            path.chmod(0o755)
        out, err = capsys.readouterr()
        after = _files(root)
        said = f'run {number}: {" ".join(run["argv"])}\n{out}{err}'
        expect = _placed(run, root)
        assert code == expect.get('exit'), said
        assert match(expect.get('diagnostics', []), printed(err)), said
        for stream, text in (('stdout', out), ('stderr', err)):
            assert stream not in expect or match(expect[stream], text), said
        assert not [text for text in expect.get('lacks', []) if text in out + err], said
        assert 'commands' not in expect or match(expect['commands'], machine.commands), (said, machine.commands)
        assert expect.get('asked') is None or (['docker', 'info'] in machine.commands) is expect['asked'], said
        assert machine.pulled() is expect.get('pulled', False), said
        assert 'written' not in expect or match(expect['written'], sorted(after.keys() - before.keys())), (
            said, sorted(after))
        assert not expect.get('unchanged') or after == before, said
        assert not [name for name in expect.get('absent', []) if (root / name).exists()], said
        for name, value in expect.get('files', {}).items():
            assert match(value, yaml.safe_load(after[name])), (said, name, after[name])
        for name, value in expect.get('texts', {}).items():
            assert match(value, after[name].decode('utf-8')), (said, name, after[name])


def _placed(value: Any, root: Path) -> Any:
    """`value` with `{tmp}` and `{tmp_uri}` in each string the copy of the scenario, as a path and as a URI."""
    if isinstance(value, dict):
        return {key: _placed(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_placed(item, root) for item in value]
    return value.replace('{tmp_uri}', root.as_uri()).replace('{tmp}', str(root)) if isinstance(value, str) else value
