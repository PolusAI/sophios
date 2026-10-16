"""Regression coverage for `sophios.compiler` bugs that have no home elsewhere."""
import copy

import pytest

from sophios.lang.diagnostics import SophiosError
from sophios.wic_types import StepId, Yaml

from .hermetic import compile_hermetic_cwl
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS


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
        name.removesuffix('__file') for name in compiled['outputs'] if name != 'o'}


@pytest.mark.fast
def test_an_untyped_output_naming_a_generated_step_name_is_wic036() -> None:
    """With no `type:` to fall back on, the unknown step is reported."""
    with pytest.raises(SophiosError) as caught:
        compile_hermetic_cwl({'outputs': {'o': {'outputSource': 'oracle__step__1__mk_file/file'}},
                              'steps': [_mk_file()]})
    [diagnostic] = caught.value.diagnostics
    assert "`outputSource: oracle__step__1__mk_file/file` names no output of a step" in diagnostic.message


@pytest.mark.fast
def test_a_bare_id_shared_by_two_steps_is_named_on_stderr_and_still_means_the_first(
        capsys: pytest.CaptureFixture[str]) -> None:
    """`mk_file/file` with two `mk_file` steps resolves as before; the line gives `(index, id)`."""
    compiled = compile_hermetic_cwl({
        'outputs': {'o': {'type': 'File', 'outputSource': 'mk_file/file'}},
        'steps': [_mk_file('x'), _mk_file('y')]})
    assert compiled['outputs']['o']['outputSource'] == 'mk_file_1/file'
    assert capsys.readouterr().err.splitlines() == [
        "Warning! oracle.wic: output 'o' has outputSource 'mk_file/file', but 2 steps have the id "
        "'mk_file' and it means the first. Write '(1, mk_file)/file' to say so."]


@pytest.mark.fast
def test_an_authored_output_source_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """The line is for the generated spelling only."""
    compile_hermetic_cwl({'outputs': {'o': {'type': 'File', 'outputSource': 'mk_file/file'}},
                          'steps': [_mk_file()]})
    assert 'Warning!' not in capsys.readouterr().err


@pytest.mark.fast
def test_a_positional_output_source_prints_no_authored_spelling_line(capsys: pytest.CaptureFixture[str]) -> None:
    """The `(index, name)/port` spelling is what the line recommends; it must not be reported."""
    compile_hermetic_cwl({'outputs': {'o': {'type': 'File', 'outputSource': '(2, mk_file)/file'}},
                          'steps': [_mk_file('x'), _mk_file('y')]})

    assert 'Warning!' not in capsys.readouterr().err


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
