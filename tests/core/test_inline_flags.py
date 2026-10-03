"""`--cwl_inline_runtag` changes how a compiled workflow is packaged: one file,
every step's `run:` carrying its process. Packaging must not change meaning, so
a tool embedded in a newer document keeps the defaults its own version gave it.
"""
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

import sophios.main
from sophios import post_compile
from sophios.ir.artifacts import CompilationArtifact
from sophios.lang.cwl import CWL_VERSIONS
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.wic_types import Yaml

from .hermetic import compile_hermetic, subworkflow_step
from .test_setup import workflow_paths

MK_FILE: Yaml = {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}}


def _one_tool(**tool_fields: Any) -> CompilationArtifact:
    """A compiled one-step workflow whose tool is rewritten with `tool_fields`."""
    artifact = compile_hermetic({'steps': [MK_FILE]}).artifact
    (tool,) = artifact.children
    return replace(artifact, children=(replace(tool, cwl={**tool.cwl, **tool_fields}),))


def _embedded(artifact: CompilationArtifact) -> Yaml:
    (step,) = post_compile.inline_artifact_runs(artifact).cwl['steps']
    embedded: Yaml = step['run']
    return embedded


@pytest.mark.fast
def test_the_table_says_what_every_cwl_version_implies() -> None:
    """Admitting a CWL version to the enum means deciding what it implies for an embedded tool."""
    assert tuple(post_compile.IMPLIED_BY_VERSION) == CWL_VERSIONS


@pytest.mark.fast
def test_a_v1_0_tool_keeps_its_network_and_listing_defaults_when_embedded() -> None:
    """cwltool gives a v1.0 file network access and a deep listing; embedded, it must still get both."""
    embedded = _embedded(_one_tool(cwlVersion='v1.0', hints={}))
    assert embedded['hints'] == {'LoadListingRequirement': {'loadListing': 'deep_listing'},
                                 'NetworkAccess': {'networkAccess': True}}
    assert 'cwlVersion' not in embedded


@pytest.mark.fast
@pytest.mark.parametrize('version', ['v1.1', 'v1.2'])
def test_a_tool_written_for_v1_1_or_later_gains_nothing_when_embedded(version: str) -> None:
    """Only v1.0 implied anything the newer defaults take away."""
    hints = {'ResourceRequirement': {'ramMin': 8}}
    assert _embedded(_one_tool(cwlVersion=version, hints=hints))['hints'] == hints


@pytest.mark.fast
def test_what_a_v1_0_tool_declares_itself_is_not_overridden() -> None:
    """A hint or requirement the tool wrote wins, in whichever form it wrote it."""
    hinted = _embedded(_one_tool(cwlVersion='v1.0', hints={'NetworkAccess': {'networkAccess': False}}))
    assert hinted['hints']['NetworkAccess'] == {'networkAccess': False}
    assert hinted['hints']['LoadListingRequirement'] == {'loadListing': 'deep_listing'}
    required = _embedded(_one_tool(cwlVersion='v1.0', hints={},
                                   requirements={'LoadListingRequirement': {'loadListing': 'no_listing'}}))
    assert 'LoadListingRequirement' not in required['hints']
    listed = _embedded(_one_tool(cwlVersion='v1.0', hints={},
                                 requirements=[{'class': 'NetworkAccess', 'networkAccess': False}]))
    assert list(listed['hints']) == ['LoadListingRequirement']


@pytest.mark.fast
@pytest.mark.parametrize('namespace', ['cwltool:', 'http://commonwl.org/cwltool#'])
def test_a_namespaced_declaration_is_renamed_to_the_class_cwltool_looks_for(namespace: str) -> None:
    """cwltool renames the namespaced class when it loads a v1.0 file; embedded, nothing does,
    so the tool's own value, whatever it is, must be written under the plain name and no default added."""
    hinted = _embedded(_one_tool(cwlVersion='v1.0', hints={f'{namespace}LoadListingRequirement':
                                                           {'loadListing': 'shallow_listing'},
                                                           f'{namespace}NetworkAccess':
                                                           {'networkAccess': False}}))
    assert hinted['hints'] == {'LoadListingRequirement': {'loadListing': 'shallow_listing'},
                               'NetworkAccess': {'networkAccess': False}}
    listed = _embedded(_one_tool(cwlVersion='v1.0',
                                 hints=[{'class': f'{namespace}LoadListingRequirement',
                                         'loadListing': 'no_listing'}],
                                 requirements=[{'class': f'{namespace}NetworkAccess', 'networkAccess': True}]))
    assert listed['hints'] == [{'class': 'LoadListingRequirement', 'loadListing': 'no_listing'}]
    assert listed['requirements'] == [{'class': 'NetworkAccess', 'networkAccess': True}]


@pytest.mark.fast
def test_a_v1_0_tool_with_no_hints_at_all_gets_them() -> None:
    """No `hints` key is the usual case, not an exception."""
    artifact = _one_tool(cwlVersion='v1.0')
    (tool,) = artifact.children
    tool.cwl.pop('hints', None)
    assert 'NetworkAccess' in _embedded(artifact)['hints']


@pytest.mark.fast
def test_hints_written_as_a_list_are_extended_as_a_list() -> None:
    """Straight at the helper: until `cwl_prepend_dockerFile_include_path` reads a list of hints,
    none reaches it through `inline_artifact_runs`."""
    process = {'cwlVersion': 'v1.0', 'hints': [{'class': 'ResourceRequirement', 'ramMin': 8}]}
    extended = post_compile._keeping_version_defaults(process, 'tool.cwl')  # pylint: disable=protected-access
    assert extended['hints'] == [{'class': 'LoadListingRequirement', 'loadListing': 'deep_listing'},
                                 {'class': 'NetworkAccess', 'networkAccess': True},
                                 {'class': 'ResourceRequirement', 'ramMin': 8}]


@pytest.mark.fast
def test_a_single_secondary_files_pattern_is_embedded_as_a_list() -> None:
    """cwltool's v1.0 updater lists a single pattern, and its checker indexes the list;
    embedded, nothing updates the tool, so the list must already be written."""
    embedded = _embedded(_one_tool(
        cwlVersion='v1.0',
        inputs={'name': {'type': 'File', 'secondaryFiles': {'pattern': '.fai'}},
                'other': {'type': 'File', 'secondaryFiles': ['.crai', '.csi']},
                'plain': 'string'},
        outputs={'file': {'type': 'File', 'secondaryFiles': '.bai'}}))
    assert embedded['inputs'] == {'name': {'type': 'File', 'secondaryFiles': [{'pattern': '.fai'}]},
                                  'other': {'type': 'File', 'secondaryFiles': ['.crai', '.csi']},
                                  'plain': 'string'}
    assert embedded['outputs'] == {'file': {'type': 'File', 'secondaryFiles': ['.bai']}}


@pytest.mark.fast
def test_ports_written_as_a_list_have_their_secondary_files_listed() -> None:
    """The same for a tool that writes `inputs` and `outputs` as lists of ports."""
    embedded = _embedded(_one_tool(
        cwlVersion='v1.0',
        inputs=[{'id': 'name', 'type': 'File', 'secondaryFiles': '.fai'}],
        outputs=[{'id': 'file', 'type': 'File', 'secondaryFiles': '.bai'}]))
    assert embedded['inputs'] == [{'id': 'name', 'type': 'File', 'secondaryFiles': ['.fai']}]
    assert embedded['outputs'] == [{'id': 'file', 'type': 'File', 'secondaryFiles': ['.bai']}]


@pytest.mark.fast
def test_embedding_a_tool_of_a_version_sophios_does_not_run_is_refused() -> None:
    """Embedding would otherwise guess what a version Sophios has never seen implied."""
    with pytest.raises(SophiosError) as caught:
        post_compile.inline_artifact_runs(_one_tool(cwlVersion='v1.3.0-dev1'))
    assert caught.value.diagnostics[0].code is SophiosErrorCode.UNSUPPORTED_CWL_VERSION


@pytest.mark.fast
def test_embedding_a_tool_without_a_version_says_it_declares_none() -> None:
    """A tool that omits `cwlVersion` is refused, and the message does not claim it declared one."""
    with pytest.raises(SophiosError) as caught:
        post_compile._keeping_version_defaults({'class': 'CommandLineTool'},  # pylint: disable=protected-access
                                               'tool.cwl')
    (diagnostic,) = caught.value.diagnostics
    assert diagnostic.code is SophiosErrorCode.UNSUPPORTED_CWL_VERSION
    assert 'declares no cwlVersion' in diagnostic.message


def _cli(directory: Path, monkeypatch: pytest.MonkeyPatch, workflow: str, *flags: str) -> Path:
    """Run the CLI on a tutorial from a fresh `directory`; the directory it wrote."""
    directory.mkdir()
    monkeypatch.chdir(directory)
    source = next(paths[workflow] for paths in workflow_paths().values() if workflow in paths)
    monkeypatch.setattr(sys, 'argv', ['sophios', '--yaml', str(source), '--generate_cwl_workflow',
                                      '--quiet', *flags])
    sophios.main._main()  # pylint: disable=protected-access
    return directory / 'autogenerated'


@pytest.mark.fast
def test_inline_runtag_embeds_a_tool_with_the_defaults_it_was_written_for(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag, end to end: `echo.cwl` is v1.0 and the embedded copy still reaches the network."""
    written = _cli(tmp_path / 'embedded', monkeypatch, 'helloworld', '--cwl_inline_runtag')
    loaded = yaml.safe_load((written / 'helloworld.cwl').read_text(encoding='utf-8'))
    (step,) = loaded['steps']
    assert step['run']['hints']['NetworkAccess'] == {'networkAccess': True}


# --------------------------------------------------------------------------
# Flattening
# --------------------------------------------------------------------------

XFORM: Yaml = {'id': 'xform', 'in': {'name': {'wic_inline_input': 'b'}}}


def _called(subtree: Yaml | None = None, **call: Any) -> Yaml:
    """A step that runs `child.wic` (one `xform` unless `subtree`), with `call` keys of its own."""
    return {**subworkflow_step('child.wic', subtree or {'steps': [XFORM]}), 'parentargs': call}


def _nested(**call: Any) -> CompilationArtifact:
    return compile_hermetic({'steps': [MK_FILE, _called(**call)]}, 'wf').artifact


def _stays(artifact: CompilationArtifact, capsys: pytest.CaptureFixture[str]) -> str:
    """Flatten, assert the call kept its place as a Workflow, and return what was said on stderr."""
    flat = post_compile.flatten_subworkflows(artifact)
    assert [step['id'] for step in flat.cwl['steps']] == [step['id'] for step in artifact.cwl['steps']]
    assert flat.children[1].cwl['class'] == 'Workflow'
    assert flat.cwl['requirements']['SubworkflowFeatureRequirement'] == {}
    said: str = capsys.readouterr().err
    return said


@pytest.mark.fast
@pytest.mark.parametrize('call, carries', [({'when': '$(inputs.file != null)'}, '`when`'),
                                           ({'requirements': {'ResourceRequirement': {'ramMin': 1}}},
                                            '`requirements`'),
                                           ({'label': 'a call'}, '`label`')],
                         ids=['when', 'requirements', 'label'])
def test_a_call_that_carries_anything_but_in_and_out_stays_nested(
        call: Yaml, carries: str, capsys: pytest.CaptureFixture[str]) -> None:
    """The call's boundary is proved removable only when the author wrote `in` and `out` and nothing else.

    `scatter` is the same rule and is pinned end to end by `test_a_scattered_call_stays_nested...`.
    """
    said = _stays(_nested(**call), capsys)
    assert f"step 'child.wic' stays a subworkflow under --cwl_inline_subworkflows: it carries {carries}" in said


@pytest.mark.fast
def test_a_child_that_opts_out_stays_nested(capsys: pytest.CaptureFixture[str]) -> None:
    """`wic: inlineable: false` in the child's own file is honoured again."""
    child = {'wic': {'inlineable': False}, 'steps': [XFORM]}
    artifact = compile_hermetic({'steps': [MK_FILE, _called(child)]}, 'wf').artifact
    assert "its workflow says `wic: inlineable: false`" in _stays(artifact, capsys)
    opted_in = compile_hermetic({'steps': [MK_FILE, _called({'wic': {'inlineable': True}, 'steps': [XFORM]})]},
                                'wf').artifact
    assert all(child.cwl['class'] != 'Workflow' for child in post_compile.flatten_subworkflows(opted_in).children)


@pytest.mark.fast
def test_a_call_stays_nested_when_dissolving_it_would_have_to_guess(capsys: pytest.CaptureFixture[str]) -> None:
    """Each boundary the flat form cannot remove without guessing keeps its call, with the reason."""
    merged = _nested()
    name = next(iter(merged.cwl['steps'][1]['in']))
    merged.cwl['steps'][1]['in'][name] = {'source': 'a', 'valueFrom': '$(self)'}
    assert f"input '{name}' is bound to" in _stays(merged, capsys)

    defaulted = _nested()
    (child,) = (c for c in defaulted.children if c.cwl['class'] == 'Workflow')
    next(iter(child.cwl['inputs'].values()))['default'] = 'x'
    assert 'has a default' in _stays(defaulted, capsys)

    listed = _nested()
    (child,) = (c for c in listed.children if c.cwl['class'] == 'Workflow')
    child.cwl['requirements'] = [{'class': 'EnvVarRequirement', 'envDef': {'A': 'b'}}]
    assert "its workflow's requirements are written as a list" in _stays(listed, capsys)


@pytest.mark.fast
def test_a_kept_call_does_not_stop_the_others_from_flattening(capsys: pytest.CaptureFixture[str]) -> None:
    """Calls are judged one at a time; a reference to a dissolved call's output is rewired past a kept one."""
    two = {'steps': [MK_FILE, _called({'wic': {'inlineable': False}, 'steps': [XFORM]}),
                     subworkflow_step('other.wic', {'steps': [{**XFORM, 'id': 'xform'}]})]}
    nested = compile_hermetic(two, 'wf').artifact
    flat = post_compile.flatten_subworkflows(nested)
    kinds = [(step['id'], child.cwl['class']) for step, child in zip(flat.cwl['steps'], flat.children)]
    assert kinds == [('wf__step__1__mk_file', 'CommandLineTool'), ('wf__step__2__child.wic', 'Workflow'),
                     ('wf__step__3__other.wic___other__step__1__xform', 'CommandLineTool')]
    assert capsys.readouterr().err.count('Warning!') == 1


@pytest.mark.fast
def test_a_document_feature_declared_differently_keeps_the_call(capsys: pytest.CaptureFixture[str]) -> None:
    """Two bodies cannot both be the root's, and nothing is half moved when the call stays."""
    caller = {'requirements': {'InlineJavascriptRequirement': {'expressionLib': ['var a = 1;']}},
              'steps': [MK_FILE, _called({
                  'requirements': {'InlineJavascriptRequirement': {'expressionLib': ['var a = 2;']}},
                  'steps': [XFORM]})]}
    artifact = compile_hermetic(caller, 'wf').artifact
    assert 'it declares a different InlineJavascriptRequirement from its caller' in _stays(artifact, capsys)
    assert post_compile.flatten_subworkflows(artifact).cwl['requirements'][
        'InlineJavascriptRequirement'] == {'expressionLib': ['var a = 1;']}


@pytest.mark.fast
def test_flattening_moves_what_the_subworkflow_required_onto_the_steps_that_ran_under_it() -> None:
    """Requirements move with the steps they applied to; the engine's own features go to the root."""
    child = {'requirements': {'EnvVarRequirement': {'envDef': {'FOO': 'bar'}}},
             'steps': [{**XFORM, 'when': '$(inputs.name != null)'}]}
    flat = post_compile.flatten_subworkflows(
        compile_hermetic({'steps': [MK_FILE, _called(child)]}).artifact).cwl
    mk_file, xform = flat['steps']
    assert 'requirements' not in mk_file
    assert xform['requirements'] == {'EnvVarRequirement': {'envDef': {'FOO': 'bar'}}}
    assert flat['requirements'] == {'InlineJavascriptRequirement': {}}, (
        'the engine reads `when` against the root, so the feature goes there, and the call is gone')


@pytest.mark.fast
def test_a_step_keeps_its_own_requirement_over_the_subworkflows() -> None:
    """The most specific entry wins, as in CWL."""
    nested = compile_hermetic({'steps': [MK_FILE, _called({
        'requirements': {'ResourceRequirement': {'ramMin': 100}}, 'steps': [XFORM]})]}).artifact
    inner = next(c for c in nested.children if c.cwl['class'] == 'Workflow')
    inner.cwl['steps'][0]['requirements'] = {'ResourceRequirement': {'ramMin': 5}}
    assert post_compile.flatten_subworkflows(nested).cwl['steps'][1]['requirements'] == {
        'ResourceRequirement': {'ramMin': 5}}


@pytest.mark.fast
def test_an_input_the_call_leaves_unbound_keeps_what_else_the_inner_step_says_about_it() -> None:
    """Unbound, a source yields null: the entry loses its source and keeps its `valueFrom`, or goes."""
    nested = _nested()
    call = nested.cwl['steps'][1]
    inner = next(c for c in nested.children if c.cwl['class'] == 'Workflow')
    formal = next(iter(call['in']))
    del call['in'][formal]
    (entry,) = (name for name, value in inner.cwl['steps'][0]['in'].items() if value == formal)
    inner.cwl['steps'][0]['in'][entry] = {'source': formal, 'valueFrom': '$(self)'}
    assert post_compile.flatten_subworkflows(nested).cwl['steps'][1]['in'][entry] == {'valueFrom': '$(self)'}


@pytest.mark.fast
def test_every_level_of_nesting_is_dissolved_and_named_for_where_the_step_came_from() -> None:
    """A step in a subworkflow in a subworkflow carries both calls in its id."""
    middle = {'steps': [subworkflow_step('inner.wic', {'steps': [XFORM]})]}
    nested = compile_hermetic({'steps': [MK_FILE, subworkflow_step('middle.wic', middle)]}, 'wf').artifact
    flat = post_compile.flatten_subworkflows(nested)
    assert [step['id'] for step in flat.cwl['steps']] == [
        'wf__step__1__mk_file', 'wf__step__2__middle.wic___middle__step__1__inner.wic___inner__step__1__xform']
    assert [child.namespace for child in flat.children] == [(step['id'],) for step in flat.cwl['steps']]
    assert all(child.cwl['class'] == 'CommandLineTool' for child in flat.children)
    assert flat.job_inputs == nested.job_inputs


@pytest.mark.fast
def test_a_workflow_with_no_subworkflow_is_returned_as_it_is() -> None:
    """Nothing to dissolve is nothing to copy."""
    artifact = compile_hermetic({'steps': [MK_FILE]}).artifact
    assert post_compile.flatten_subworkflows(artifact) is artifact


@pytest.mark.fast
def test_the_flags_apply_shape_first_and_do_not_depend_on_each_other() -> None:
    """Each flag alone, and both, from one nested workflow."""
    nested = _nested()
    flat = post_compile.apply_inline_options(nested, subworkflows=True, runtag=False)
    embedded = post_compile.apply_inline_options(nested, subworkflows=False, runtag=True)
    both = post_compile.apply_inline_options(nested, subworkflows=True, runtag=True)
    assert post_compile.apply_inline_options(nested, subworkflows=False, runtag=False) is nested
    assert all(isinstance(step['run'], str) for step in flat.cwl['steps'])
    assert embedded.cwl['steps'][1]['run']['class'] == 'Workflow'
    assert [step['run']['class'] for step in both.cwl['steps']] == ['CommandLineTool'] * 2
    assert [step['id'] for step in both.cwl['steps']] == [step['id'] for step in flat.cwl['steps']]
