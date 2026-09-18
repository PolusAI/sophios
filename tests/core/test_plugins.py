"""Behavioural coverage for `sophios.plugins` transforms that reach emitted CWL.

`sophios.plugins` is FORBIDDEN to the oracle (`test_hermeticity.py`),
because importing it reads the config and walks the adapter search paths. That
makes the oracle unable to check the plugin transforms directly, so the ones
whose output lands in an emitted document are covered here instead.
"""
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
