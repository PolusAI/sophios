"""What a runner gets as the job: a File's format resolved from two or more EDAM candidates by its extension, as
EDAM's own table pairs them, and an output-target Directory as a literal the runner creates."""
import copy
import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import example, given, strategies as st

from sophios import targets
from sophios.compiler import CompileSettings, compile_source, resolved_view
from sophios.ir.frontdoor import bundle_from_disk
from sophios.runtime_inputs import runner_job_inputs
from sophios.wic_types import StepId, Tool

from .budgets import budget
from .helpers import write_tree

DATA = Path(__file__).resolve().parent / 'data' / 'output_directory'

#: A File, its candidates as `edam:format_<n>`, and the one EDAM pairs with its extension: SDF `.sdf`, PDB `.pdb`,
#: PDBQT `.pdbqt`; `.mol`'s is dropped, not guessed at: a runner treats `format` as a claim.
_CASES = (({'class': 'File', 'location': '/data/benzamidine.sdf'}, (1476, 3814, 3816), 3814),
          ({'class': 'File', 'location': '/data/receptor.pdb'}, (1476, 3814), 1476),
          ({'class': 'File', 'basename': 'ligand.pdbqt'}, (1476, 4036), 4036),
          ({'class': 'File', 'location': '/data/ligand.mol'}, (1476, 3815), None))
_SPELLINGS = ('edam:format_{}', 'http://edamontology.org/format_{}', 'https://edamontology.org/format_{}')
_OTHER = (st.integers() | st.text(max_size=3)
          | st.just({'class': 'File', 'location': 'a.txt', 'format': 'edam:format_1964'}))


@st.composite
def _jobs(draw: st.DrawFn) -> tuple[dict[str, Any], dict[str, Any]]:
    """A job holding one of `_CASES` in lists, records and `secondaryFiles`, its candidates in any order and EDAM
    spelling, beside formats not EDAM's; and the job with its format resolved."""
    file, numbers, number = draw(st.sampled_from(_CASES))
    spelled = {n: draw(st.sampled_from(_SPELLINGS)).format(n) for n in numbers}
    others = draw(st.lists(st.sampled_from(['http://example.org/format', 'edam:data_0006']), max_size=1))
    value: Any = {**file, 'format': draw(st.permutations([*spelled.values(), *others]))}
    resolved: Any = {**file, **({'format': spelled[number]} if number else {})}
    for container in draw(st.lists(st.sampled_from(['list', 'record', 'secondaryFiles']), max_size=3)):
        other = draw(_OTHER)
        wrap: Callable[[Any, Any], Any] = {
            'list': lambda o, inner: [o, inner], 'record': lambda o, inner: {'x': inner, 'y': o},
            'secondaryFiles': lambda _, inner: {'class': 'File', 'location': 'p.txt', 'secondaryFiles': [inner]},
        }[container]
        value, resolved = wrap(other, value), wrap(other, resolved)
    other = draw(_OTHER)
    return {'input': value, 'other': other}, {'input': resolved, 'other': other}


@pytest.mark.fast
@given(_jobs())
@example(({'ligand': {'class': 'File', 'location': '/data/benzamidine.sdf',
                      'format': ['edam:format_1476', 'edam:format_3814', 'edam:format_3816']},
           'nested': [{'class': 'File', 'location': '/data/receptor.pdb',
                       'format': ['edam:format_1476', 'edam:format_3814']}]},
          {'ligand': {'class': 'File', 'location': '/data/benzamidine.sdf', 'format': 'edam:format_3814'},
           'nested': [{'class': 'File', 'location': '/data/receptor.pdb', 'format': 'edam:format_1476'}]}))
@budget(50)
def test_a_file_has_the_format_its_extension_names_or_none(drawn: tuple[dict[str, Any], dict[str, Any]]) -> None:
    """Each File's candidates become the one its extension names, as spelled, wherever it is; nothing else changes."""
    job, resolved = drawn
    given_job = copy.deepcopy(job)
    assert runner_job_inputs(job, set()) == resolved
    assert job == given_job


def _written(tmp_path: Path, source: str, inputs_file: str = '') -> Path:
    """`data/output_directory/<source>` compiled and written under `tmp_path / 'out'` as the CWL target writes it,
    its job read from `inputs_file` when given."""
    tool = DATA / 'write_dir.cwl'
    tools = {StepId('write_dir', 'global'): Tool(str(tool), yaml.safe_load(tool.read_text(encoding='utf-8')))}
    siblings = {'global': {p.stem: p for p in DATA.glob('*.wic')}}
    result = compile_source(bundle_from_disk(DATA / source, siblings, tools), CompileSettings(),
                            quiet=True)
    if inputs_file:
        write_tree(tmp_path, {'job.yml': inputs_file})
    cwl = targets.target('cwl')
    cwl.write(cwl.lower(result, resolved_view(result)), (), tmp_path / 'out',
              inputs_file=str(tmp_path / 'job.yml') if inputs_file else '', root_dir=tmp_path)
    return tmp_path / 'out'


def _job(out: Path, stem: str) -> Any:
    return yaml.safe_load((out / f'{stem}_inputs.yml').read_text(encoding='utf-8'))


@pytest.mark.fast
@pytest.mark.parametrize('given_directory', [
    'result.outDir', '{class: Directory, location: result.outDir}', '{class: Directory, path: result.outDir}',
    '{class: Directory, basename: result.outDir}'], ids=['name', 'location', 'path', 'basename'])
def test_an_output_target_directory_in_a_job_is_a_directory_literal(tmp_path: Path, given_directory: str) -> None:
    """Whichever way an inputs file gave it, the runner is asked to create an empty directory of that name; nothing
    is made on disk."""
    out = _written(tmp_path, 'w.wic', f'write_dir~outDir: {given_directory}\n')
    assert _job(out, 'w')['write_dir~outDir'] == {'class': 'Directory', 'basename': 'result.outDir', 'listing': []}
    assert not (tmp_path / 'result.outDir').exists()


@pytest.mark.fast
@pytest.mark.parametrize(('source', 'key'), [('nested.wic', 'w~write_dir~outDir'),
                                             ('deeper.wic', 'nested~w~write_dir~outDir')], ids=['one', 'two'])
def test_an_output_target_directory_below_calls_is_a_directory_literal_at_its_root_key(
        tmp_path: Path, source: str, key: str) -> None:
    """The job key is the step's qualified input, however many calls deep."""
    out = _written(tmp_path, source, f'{key}: {{class: Directory, location: result.outDir}}\n')
    assert _job(out, source.removesuffix('.wic')) == {
        key: {'class': 'Directory', 'basename': 'result.outDir', 'listing': []}}


@pytest.mark.needs_cwltool
def test_a_tool_below_a_call_writes_into_its_output_target_directory(tmp_path: Path) -> None:
    """cwltool creates the directory the job's literal names, and the tool fills it."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    out = _written(tmp_path, 'nested.wic')
    assert cwltool.main.main(['--quiet', '--outdir', str(tmp_path / 'run'), str(out / 'nested.cwl'),
                              str(out / 'nested_inputs.yml')], stdout=io.StringIO()) == 0
    assert (tmp_path / 'run' / 'result.outDir' / 'done.txt').is_file()
