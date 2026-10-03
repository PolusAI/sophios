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

from .hermetic import compile_hermetic
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
        post_compile._keeping_version_defaults({'class': 'CommandLineTool'}, 'tool.cwl')  # pylint: disable=protected-access
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
