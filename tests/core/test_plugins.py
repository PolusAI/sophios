"""Behavioural coverage for `sophios.plugins` transforms that reach emitted CWL.

`sophios.plugins` is FORBIDDEN to the oracle (`test_hermeticity.py`),
because importing it reads the config and walks the adapter search paths. That
makes the oracle unable to check the plugin transforms directly, so the ones
whose output lands in an emitted document are covered here instead.
"""
import copy
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
