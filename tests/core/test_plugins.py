"""Behavioural coverage for `sophios.plugins` transforms that reach emitted CWL.

`sophios.plugins` is FORBIDDEN to the oracle (`test_hermeticity.py`),
because importing it reads the config and walks the adapter search paths. That
makes the oracle unable to check the plugin transforms directly, so the ones
whose output lands in an emitted document are covered here instead.

Discovery is covered here too: which files are plugins, and which file a stem resolves to.
"""
import copy
import glob
from pathlib import Path
from typing import Any

import pytest

import sophios.plugins
from sophios.wic_types import Yaml


@pytest.mark.fast
def test_partial_failure_success_codes_are_canonically_ordered() -> None:
    """`successCodes` is emitted CWL, so no set's iteration order may reach it.

    Under `--partial_failure_enable`, `main` transforms the emitted artifact
    tree and writes the result to disk, which makes this list output rather
    than an internal collection. The codes are
    chosen so a set's iteration order is not accidentally the sorted one.
    """
    tool: Yaml = {'class': 'CommandLineTool', 'baseCommand': 'true', 'inputs': {},
                  'outputs': {'o': {'type': 'File', 'outputBinding': {'glob': 'x'}}}}
    updated = sophios.plugins.cwl_update_outputs_optional(tool, [3, 6], [9, 4, 1])
    assert updated['successCodes'] == [0, 1, 3, 4, 5, 9]


@pytest.mark.fast
def test_partial_failure_makes_outputs_optional() -> None:
    """The other half of the same transform, so the test above cannot pass alone
    against a function that stopped doing its actual job."""
    tool: Yaml = {'class': 'CommandLineTool', 'baseCommand': 'true', 'inputs': {},
                  'outputs': {'o': {'type': 'File', 'outputBinding': {'glob': 'x'}}}}
    updated = sophios.plugins.cwl_update_outputs_optional(tool, [0, 1], [])
    assert updated['outputs']['o']['type'] == 'File?'


def _docker(spelling: str, **fields: Any) -> Yaml | list[Yaml]:
    """A fresh `hints` or `requirements` holding one `DockerRequirement`: a mapping
    keyed by class (hand-written adapters) or a list of `{class: ...}` entries (cwl_utils)."""
    if spelling == 'map-form':
        return {'DockerRequirement': fields}
    return [{'class': 'DockerRequirement', **fields}]


@pytest.mark.fast
@pytest.mark.parametrize('spelling', ['map-form', 'list-form'])
def test_a_dockerfile_include_is_resolved_in_either_hints_spelling(spelling: str) -> None:
    """cwl_utils renders `hints` as a list; hand-written adapters use a mapping.
    The list crashed `Workflow.compile()` on every hinted tool_builder tool."""
    tool: Yaml = {'class': 'CommandLineTool', 'baseCommand': 'true', 'inputs': {}, 'outputs': {},
                  'hints': _docker(spelling, dockerFile={'$include': 'Dockerfile_x'}, dockerImageId='x')}
    before = copy.deepcopy(tool)
    updated = sophios.plugins.cwl_prepend_dockerFile_include_path(tool, '/adapters/t.cwl')
    assert updated['hints'] == _docker(spelling, dockerFile={'$include': '/adapters/Dockerfile_x'},
                                       dockerImageId='x')
    assert tool == before, 'the input document is not mutated'


@pytest.mark.fast
@pytest.mark.parametrize('spelling', ['map-form', 'list-form'])
def test_the_noentrypoint_tag_is_appended_in_either_requirements_spelling(spelling: str) -> None:
    """The same two spellings of `requirements`, under `--docker_remove_entrypoints`."""
    tool: Yaml = {'class': 'CommandLineTool', 'baseCommand': 'true', 'inputs': {}, 'outputs': {},
                  'requirements': _docker(spelling, dockerPull='docker.io/bash:4.4')}
    before = copy.deepcopy(tool)
    updated = sophios.plugins.dockerPull_append_noentrypoint(tool)
    assert updated['requirements'] == _docker(spelling, dockerPull='docker.io/bash:4.4-noentrypoint')
    assert tool == before, 'the input document is not mutated'


def _tool(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: true\ninputs: {}\noutputs: {}\n',
                    encoding='utf-8')


def _workflow(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('steps: {}\n', encoding='utf-8')


@pytest.fixture
def reversed_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The filesystem lists a directory's entries in reverse, as some do; discovery must not depend on it."""
    real_glob = glob.glob

    def listed_in_reverse(pattern: str, *, recursive: bool = False) -> list[str]:
        return sorted(real_glob(pattern, recursive=recursive), reverse=True)

    monkeypatch.setattr(glob, 'glob', listed_in_reverse)


@pytest.mark.fast
@pytest.mark.usefixtures('reversed_listing')
def test_tools_are_discovered_in_sorted_path_order(tmp_path: Path) -> None:
    """Tools come back ordered by path, not by stem and not by the order the filesystem lists them."""
    for name in ('b/zeta.cwl', 'c/mid.cwl', 'a/alpha.cwl'):
        _tool(tmp_path / name)
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(tmp_path)]}})
    assert [key.stem for key in tools] == ['alpha', 'zeta', 'mid']


@pytest.mark.fast
def test_nothing_under_an_autogenerated_directory_is_a_tool(tmp_path: Path) -> None:
    """`autogenerated/` is the compiler's own output, at the top of a search root or nested below it."""
    for name in ('a/alpha.cwl', 'autogenerated/ghost.cwl', 'x/autogenerated/y/ghost2.cwl'):
        _tool(tmp_path / name)
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(tmp_path)]}})
    assert [key.stem for key in tools] == ['alpha']


@pytest.mark.fast
def test_workflow_discovery_skips_inputs_files_and_the_compilers_output(tmp_path: Path) -> None:
    """`_inputs` files and anything under `autogenerated/` are not workflows."""
    for name in ('b/z.wic', 'a/a.wic', 'autogenerated/g.wic', 'a/a_inputs.wic'):
        _workflow(tmp_path / name)
    found = sophios.plugins.get_yml_paths({'search_paths_wic': {'global': [str(tmp_path)]}})
    assert sorted(found['global']) == ['a', 'z']


@pytest.mark.fast
@pytest.mark.usefixtures('reversed_listing')
def test_the_last_workflow_in_sorted_path_order_wins_a_shared_stem(tmp_path: Path) -> None:
    """Of two workflows with one stem in one search root, the last path in sorted order wins, not the shortest."""
    for name in ('a/dup.wic', 'zz/sub/dup.wic'):
        _workflow(tmp_path / name)
    found = sophios.plugins.get_yml_paths({'search_paths_wic': {'global': [str(tmp_path)]}})
    assert found['global'] == {'dup': tmp_path / 'zz' / 'sub' / 'dup.wic'}
