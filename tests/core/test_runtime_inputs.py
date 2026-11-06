"""A job's File format, resolved from two or more EDAM candidates by its extension, as EDAM's own table pairs them."""
import copy
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import example, given, strategies as st

from sophios.ir.artifacts import CompilationArtifact
from sophios.runtime_inputs import normalize_artifact_job_inputs

from .budgets import budget

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
    artifact = CompilationArtifact(namespace=(), name='w', run_path='w.cwl', cwl={}, job_inputs={}, graph=None)
    assert normalize_artifact_job_inputs(artifact, job) == resolved
    assert job == given_job
