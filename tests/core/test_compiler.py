"""Regression coverage for `sophios.compiler` bugs that have no home elsewhere."""
import copy

import pytest

from sophios.wic_types import StepId, Yaml

from .hermetic import compile_hermetic_cwl, subworkflow_step
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS

pytestmark = pytest.mark.package


@pytest.mark.fast
@pytest.mark.parametrize(('claim', 'user', 'argument', 'expected'), [
    ('nothing to merge', {}, {}, {}),
    ('the user wrote it all', {'doc': 'mine', 'label': 'keep me'}, {},
     {'doc': 'mine', 'label': 'keep me'}),
    ('the argument documents a bare input', {}, {'doc': 'the file name'},
     {'doc': 'the file name'}),
    ('both, joined by one real newline', {'doc': 'mine', 'label': 'keep me'},
     {'doc': 'the file name', 'label': 'File name'},
     {'doc': 'mine\nthe file name', 'label': 'keep me\nFile name'}),
    ('argument doc is a list', {'doc': 'mine'}, {'doc': ['one', 'two']}, {'doc': 'mine\none\ntwo'}),
    ('user doc is a list', {'doc': ['one', 'two']}, {'doc': 'theirs'}, {'doc': 'one\ntwo\ntheirs'}),
    ('both are lists', {'doc': ['a', 'b']}, {'doc': ['c', 'd']}, {'doc': 'a\nb\nc\nd'}),
])
def test_a_referenced_input_merges_the_documentation_of_the_argument_it_binds(
        claim: str, user: Yaml, argument: Yaml, expected: Yaml) -> None:
    """A workflow input keeps what its author wrote and appends what the bound
    argument documents: no empty text, no synthesized field, no Python repr of
    a list (CWL types `doc` as `string | string[]`; `label` is string-only),
    and a newline character rather than the two-character escape.

    Two steps reference the input and only `mk_text.name` is documented, so an
    undocumented reference adds nothing.
    """
    tools = copy.deepcopy(SYNTHETIC_TOOLS)
    tools[StepId('mk_text', SYNTHETIC_NS)].cwl['inputs']['name'].update(argument)
    document: Yaml = {
        'inputs': {'wf_name': {'type': 'string', **user}},
        'steps': [{'id': 'mk_text', 'in': {'name': 'wf_name'}},
                  {'id': 'mk_file', 'in': {'name': 'wf_name'}}],
    }
    compiled = compile_hermetic_cwl(document, 'docs', tools=tools)
    assert compiled['inputs']['wf_name'] == {'type': 'string', **expected}, claim


def _mk_file(name: str = 'x') -> Yaml:
    """A step of the synthetic registry that binds its one input."""
    return {'id': 'mk_file', 'in': {'name': {'wic_inline_input': name}}}


@pytest.mark.fast
@pytest.mark.parametrize('steps', [[_mk_file()], [_mk_file('x'), _mk_file('y')]], ids=['one-step', 'repeated-id'])
def test_a_generated_step_name_in_output_source_is_an_unknown_step(steps: list[Yaml]) -> None:
    """`<workflow>__step__<n>__<id>` is not a spelling the compiler emits: it names no step, so it is
    written through as it is and the step's own output is lifted as if nothing had asked for it."""
    compiled = compile_hermetic_cwl({
        'outputs': {'o': {'type': 'File', 'outputSource': 'oracle__step__1__mk_file/file'}},
        'steps': steps})
    assert compiled['outputs']['o']['outputSource'] == 'oracle__step__1__mk_file/file'
    assert {step['id'] for step in compiled['steps']} == {
        name.removesuffix('~file') for name in compiled['outputs'] if name != 'o'}


@pytest.mark.fast
@pytest.mark.parametrize(('source', 'names', 'resolved', 'said'), [
    ('mk_file/file', 'xy', 'mk_file@1/file',
     ["Warning! oracle.wic: output 'o' has outputSource 'mk_file/file', but 2 steps have the id 'mk_file' and it "
      "means the first. Write '(1, mk_file)/file' to say so."]),
    ('mk_file/file', 'x', 'mk_file/file', []),
    ('(2, mk_file)/file', 'xy', 'mk_file@2/file', []),
    ('(1, mk_file)/file', 'x', 'mk_file/file', [])],
    ids=['shared-bare-id', 'authored', 'positional', 'positional-unique'])
def test_an_output_source_names_the_spelling_the_compiler_generated_and_only_that(
        capsys: pytest.CaptureFixture[str], source: str, names: str, resolved: str, said: list[str]) -> None:
    """A bare id shared by two steps still means the first, and the line gives `(index, id)`; it is for the generated
    spelling only, so an authored one prints nothing; and the `(index, name)/port` spelling is what the line
    recommends, so it is never reported; a position naming a step whose id is unique resolves to that plain id."""
    compiled = compile_hermetic_cwl({'outputs': {'o': {'type': 'File', 'outputSource': source}},
                                     'steps': [_mk_file(name) for name in names]})
    assert compiled['outputs']['o']['outputSource'] == resolved
    assert capsys.readouterr().err.splitlines() == said


@pytest.mark.fast
@pytest.mark.parametrize('expression, prints', [
    ('$(inputs.nope != null)', True), ('$(inputs["nope"] != null)', True), ('$(inputs.name != null)', False),
    ('$(inputs.name-1 > 0)', False)],
    ids=['dot', 'bracket', 'declared', 'minus'])
def test_a_when_that_reads_an_input_the_step_lacks_is_named_and_still_compiles(
        capsys: pytest.CaptureFixture[str], expression: str, prints: bool) -> None:
    """CWL evaluates `inputs.nope` as null, so the step never ran and nothing said so."""
    compiled = compile_hermetic_cwl({'steps': [{**_mk_file(), 'when': expression}]})
    assert compiled['steps'][0]['when'] == expression
    assert ('in `when:`, which its process does not declare' in capsys.readouterr().err) is prints


def _call_when(expression: str) -> Yaml:
    """A call to `child.wic`, whose `mk_file` leaves `name` for the caller, made conditional on `expression`."""
    return {'steps': [{**subworkflow_step('child.wic', {'steps': [{'id': 'mk_file'}]}),
                       'parentargs': {'when': expression}}]}


@pytest.mark.fast
@pytest.mark.parametrize(('expression', 'said'), [
    ('$(inputs["mk_file~name"] != null)', []),
    ('$(inputs.mk_file~name != null)',
     ["Warning! oracle.wic: step 'child.wic' reads inputs.mk_file in `when:`, which its process does not declare; "
      "CWL evaluates it as null. Write inputs['mk_file~name'] to read one it declares."])], ids=['bracket', 'dotted'])
def test_a_when_on_a_call_that_reads_a_made_name_dotted_is_told_the_bracket_form(
        capsys: pytest.CaptureFixture[str], expression: str, said: list[str]) -> None:
    """A call declares the names its child exposes, `mk_file~name` among them, and the bracket form reads it, so it
    draws no note. `inputs.mk_file~name` is `inputs.mk_file`, then `~name`: the note names the input and how to
    read it."""
    compiled = compile_hermetic_cwl(_call_when(expression))
    assert 'mk_file~name' in compiled['steps'][0]['in']
    assert capsys.readouterr().err.splitlines() == said
