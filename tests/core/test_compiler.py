"""Regression coverage for `sophios.compiler` bugs that have no home elsewhere."""
import copy
from pathlib import Path
from typing import Any

import pytest

from sophios import compiler, cwl_subinterpreter, utils
from sophios.cli import default_compilation_settings
from sophios.lang import to_json
from sophios.utils_graphs import get_graph_reps
from sophios.wic_types import StepId, Yaml

from .hermetic import bundle, compile_hermetic_cwl, subworkflow_step
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS


class _Captured(Exception):
    """Raised by the stub to stop `rerun_cwltool` once it has built its document."""


@pytest.mark.fast
@pytest.mark.parametrize(('cwl_tool', 'config'), [
    ('tool', {'id': 'elsewhere', 'in': {}}),
    ('tool.wic', {'in': {}}),
])
def test_rerun_cwltool_builds_an_id_form_step(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        cwl_tool: str, config: Yaml) -> None:
    """`rerun_cwltool` builds a step whose `id` can be read back, on both branches.

    Asserting on literals copied into the test proves nothing about the code:
    the documents are built inside `rerun_cwltool`, so the test has to get them
    from there. Each branch hands its document to exactly one function, which is
    the seam to stub — the CWL runner is never reached, and the cache directory
    is never touched. `_Captured` is not a `FileNotFoundError`, so the
    function's own handler does not swallow it.

    One row's config carries an `id` of its own, so the step the branch builds
    is only named for the tool if the config cannot overwrite it.
    """
    seen: list[Yaml] = []

    def capture(*args: Any, **_: Any) -> None:
        # Both branches end at the same door, so the document the branch
        # built is read back from the parse it bundled.
        seen.append(to_json(args[0].parsed.document))
        raise _Captured

    monkeypatch.setattr(compiler, 'compile_source', capture)
    with pytest.raises(_Captured):
        cwl_subinterpreter.rerun_cwltool(
            '', tmp_path, tmp_path, cwl_tool, config, {}, {}, tmp_path)

    assert [utils.require_step_id(step) for step in seen[0]['steps']] == [cwl_tool]


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


@pytest.mark.fast
@pytest.mark.parametrize(('dark_theme', 'font_colour'), [(False, 'white'), (True, 'black')])
def test_the_graph_draws_inferred_edges_in_the_font_colour_and_the_rest_in_blue(
        dark_theme: bool, font_colour: str) -> None:
    """An edge the document wrote is blue, whether it joins two steps of one
    document or reaches into a subworkflow; an edge the compiler inferred takes
    the theme's font colour, which is black on a dark theme and white otherwise.
    """
    document: Yaml = {'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}, 'out': [{'file': {'wic_anchor': 'f'}}]},
        {'id': 'xform', 'in': {'file': {'wic_alias': 'f'}, 'name': {'wic_inline_input': 'b'}}},
        subworkflow_step('sub.wic', {'steps': [
            {'id': 'xform', 'in': {'file': {'wic_alias': 'f'}, 'name': {'wic_inline_input': 'c'}}},
            {'id': 'count'},                                       # file inferred from xform
        ]}),
    ]}
    compiler_options, graph_settings, yaml_tag_paths = default_compilation_settings()
    graph_settings['graph_dark_theme'] = dark_theme
    compiled = compiler.compile_source(
        bundle(document, 'oracle', SYNTHETIC_TOOLS), compiler_options, graph_settings, yaml_tag_paths,
        relative_run_path=True, testing=True, graph_target=get_graph_reps('oracle'))

    root = compiled.artifact.graph_view.graphdata
    (sub,) = root.subgraphs
    colours = {(source, sink): attrs.get('color') for source, sink, attrs in root.edges + sub.edges}
    assert colours == {
        ('oracle__step__1__mk_file', 'oracle__step__2__xform'): 'blue',
        ('oracle__step__1__mk_file', 'oracle__step__3__sub.wic___sub__step__1__xform'): 'blue',
        ('oracle__step__3__sub.wic___sub__step__1__xform',
         'oracle__step__3__sub.wic___sub__step__2__count'): font_colour,
    }


def _mk_file(name: str = 'x') -> Yaml:
    """A step of the synthetic registry that binds its one input."""
    return {'id': 'mk_file', 'in': {'name': {'wic_inline_input': name}}}


@pytest.mark.fast
def test_a_generated_step_name_in_output_source_is_named_on_stderr_and_still_compiles(
        capsys: pytest.CaptureFixture[str]) -> None:
    """The line names the file, the output, the generated name and the spelling to write."""
    compiled = compile_hermetic_cwl({
        'outputs': {'o': {'type': 'File', 'outputSource': 'oracle__step__1__mk_file/file'}},
        'steps': [_mk_file()]})
    assert compiled['outputs']['o']['outputSource'] == 'oracle__step__1__mk_file/file'
    assert capsys.readouterr().err.splitlines() == [
        "Warning! oracle.wic: output 'o' names its step 'oracle__step__1__mk_file', a name the "
        "compiler generates. Write 'mk_file/file' instead."]


@pytest.mark.fast
def test_a_generated_name_for_a_repeated_id_is_answered_with_its_position(
        capsys: pytest.CaptureFixture[str]) -> None:
    """`mk_file/file` would mean the first `mk_file`, so the line gives `(index, id)`."""
    compile_hermetic_cwl({
        'outputs': {'o': {'type': 'File', 'outputSource': 'oracle__step__2__mk_file/file'}},
        'steps': [_mk_file('x'), _mk_file('y')]})
    assert "Write '(2, mk_file)/file' instead" in capsys.readouterr().err


@pytest.mark.fast
def test_an_authored_output_source_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """The line is for the generated spelling only."""
    compile_hermetic_cwl({'outputs': {'o': {'type': 'File', 'outputSource': 'mk_file/file'}},
                          'steps': [_mk_file()]})
    assert 'Warning!' not in capsys.readouterr().err


@pytest.mark.fast
def test_a_generated_name_in_a_child_document_is_named_once_however_often_it_is_called(
        capsys: pytest.CaptureFixture[str]) -> None:
    """Two calls lower the child twice; its line is still one line."""
    child: Yaml = {'steps': [_mk_file()],
                   'outputs': {'o': {'type': 'File', 'outputSource': 'child__step__1__mk_file/file'}}}
    compile_hermetic_cwl({'steps': [subworkflow_step('child.wic', child), subworkflow_step('child.wic', child)]})
    assert capsys.readouterr().err.count("Warning! child.wic: output 'o' names its step") == 1


@pytest.mark.fast
def test_a_positional_output_source_prints_no_authored_spelling_line(capsys: pytest.CaptureFixture[str]) -> None:
    """The `(index, name)/port` spelling is what the line recommends; it must not be reported."""
    compile_hermetic_cwl({'outputs': {'o': {'type': 'File', 'outputSource': '(2, mk_file)/file'}},
                          'steps': [_mk_file('x'), _mk_file('y')]})

    assert 'Warning!' not in capsys.readouterr().err


@pytest.mark.fast
def test_a_when_that_reads_a_generated_name_is_named_and_still_compiles(
        capsys: pytest.CaptureFixture[str]) -> None:
    """A call's `when:` may read a lifted port by its generated name; CWL accepts it, the line says what to declare."""
    call = subworkflow_step('child.wic', {'steps': [{'id': 'mk_file'}]})
    call['parentargs'] = {'when': '$(inputs.child__step__1__mk_file___name != "x")'}
    compiled = compile_hermetic_cwl({'steps': [call]})
    assert compiled['steps'][0]['when'] == '$(inputs.child__step__1__mk_file___name != "x")'
    assert ("step 'child.wic' reads inputs.child__step__1__mk_file___name in `when:`, a name the compiler "
            "generates") in capsys.readouterr().err


@pytest.mark.fast
@pytest.mark.parametrize('expression, prints', [
    ('$(inputs.nope != null)', True), ('$(inputs["nope"] != null)', True), ('$(inputs.name != null)', False)],
    ids=['dot', 'bracket', 'declared'])
def test_a_when_that_reads_an_input_the_step_lacks_is_named_and_still_compiles(
        capsys: pytest.CaptureFixture[str], expression: str, prints: bool) -> None:
    """CWL evaluates `inputs.nope` as null, so the step never ran and nothing said so."""
    compiled = compile_hermetic_cwl({'steps': [{**_mk_file(), 'when': expression}]})
    assert compiled['steps'][0]['when'] == expression
    assert ('reads inputs.nope in `when:`, which its process does not declare' in capsys.readouterr().err) is prints
