"""An edge reference names a definition, and a name is defined once.

Both were enforced already, as bare `ValueError`s a caller could not match on,
suppress, or tell from a bug. They carry codes now.

Everything here compiles through `compile_production`, which is `testing=False`.
`compile_hermetic` passes `testing=True`, and that branch absorbs an undefined
edge into a workflow input, so these diagnostics are unreachable through it.
"""
import pytest
from hypothesis import example, given, strategies as st

from sophios.lang.diagnostics import Diagnostic, SophiosError
from sophios.wic_types import Yaml

from .budgets import budget
from .cases import cases
from .hermetic import compile_production


def _step(kind: str, name: str | None) -> Yaml:
    """sink reading its file from the edge `name` (R), or mk_file defining it on its file (D) or mk_text on its text
    (T), unless it is None."""
    if kind == 'R':
        return {'id': 'sink', 'in': {'file': {'wic_alias': name}}}
    stem, port = ('mk_file', 'file') if kind == 'D' else ('mk_text', 'text')
    return {'id': stem, 'in': {'name': {'wic_inline_input': 'a'}},
            **({} if name is None else {'out': [{port: {'wic_anchor': name}}]})}


@st.composite
def _documents(draw: st.DrawFn) -> Yaml:
    """A case as data/edge-binding writes one: a document whose every reference follows the one definition of its
    name, with at most one fault added, a reference no step defines, one above its definition or a second one."""
    steps: list[tuple[str, str | None]] = []
    for index in range(draw(st.integers(1, 6))):
        files = [name for kind, name in steps if kind == 'D' and name]
        kind = draw(st.sampled_from('DTR' if files else 'DT'))
        steps.append((kind, draw(st.sampled_from(files if kind == 'R' else [None, f'e{index}']))))
    defined = [(index, kind, name) for index, (kind, name) in enumerate(steps) if kind != 'R' and name]
    referable = [(index, str(name)) for index, kind, name in defined if kind == 'D']
    fault = draw(st.sampled_from(['none', 'absent'] + ['twice'] * bool(defined) + ['early'] * bool(referable)))
    codes, says = [], ''
    if fault == 'absent':
        steps.insert(draw(st.integers(0, len(steps))), ('R', 'absent'))
        codes, says = ['wic025'], 'absent'
    elif fault == 'early':
        index, says = draw(st.sampled_from(referable))
        steps.insert(draw(st.integers(0, index)), ('R', says))
        codes = ['wic025']
    elif fault == 'twice':
        index, _, name = draw(st.sampled_from(defined))
        steps.insert(draw(st.integers(index + 1, len(steps))), (draw(st.sampled_from('DT')), name))
        says = str(name)
        codes = ['wic026']
    return {'steps': [_step(*step) for step in steps], 'codes': codes, 'says': says}


@pytest.mark.fast
@budget(50)
@given(_documents())
def test_an_edge_reference_names_one_earlier_definition(case: Yaml) -> None:
    """Each case of data/edge-binding, and generated ones: the codes it raises, each message holding `says`; and,
    when it compiles, the source of each reference in the root is the port that defines its edge, and of each input
    `sources` names, as [step index, input, source]."""
    raised: list[Diagnostic] = []
    try:
        compiled = compile_production({'steps': case['steps']}, 'root').artifact.cwl
    except SophiosError as error:
        raised = list(error.diagnostics)
    else:
        made = {anchor['wic_anchor']: f"{compiled['steps'][index]['id']}/{port}" for index, step
                in enumerate(case['steps']) for out in step.get('out', []) for port, anchor in out.items()}
        sources = [[index, key, made[value['wic_alias']]] for index, step in enumerate(case['steps'])
                   for key, value in step.get('in', {}).items() if 'wic_alias' in value]
        for index, key, source in sources + case.get('sources', []):
            assert compiled['steps'][index]['in'][key]['source'] == source
    assert [found.code.value for found in raised] == case['codes']
    assert all(case.get('says', '') in found.message for found in raised)


for _case in cases('edge-binding'):
    example(*_case.values)(test_an_edge_reference_names_one_earlier_definition)
