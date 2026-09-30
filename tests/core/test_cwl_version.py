"""The CWL substrate version: one declared value, enforced at every emitting path.

The design specifies `.wic` as an abstraction over a single declared CWL version
(`design_docs/core-refactor-design.md` §5.2). At baseline three sites disagreed:
the compiler emitted `v1.2`, a CommandLineTool generator emitted `v1.0`, and the
schema accepted any non-empty string.
"""
import pytest

import yaml

from sophios.lang.cwl import CWL_VERSION, CWL_VERSIONS, CwlVersion
from sophios.python_cwl_adapter import generate_CWL_CommandLineTool


# --------------------------------------------------------------------------
# Sanity checks
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_the_declared_version_is_v1_2() -> None:
    """§5.2 pins the substrate to CWL v1.2."""
    assert CWL_VERSION == 'v1.2'
    assert CWL_VERSION == CwlVersion.V1_2


@pytest.mark.fast
def test_the_version_serialises_as_a_plain_string() -> None:
    """`CWL_VERSION` must survive a YAML dump unchanged.

    This is the check that matters, and the one that `isinstance(x, str)` does
    not make. PyYAML picks a representer by exact type, so a `StrEnum` member
    compares equal to `'v1.2'`, formats as `v1.2`, and still raises
    `RepresenterError` the moment a generated document is written to disk.
    """
    assert type(CWL_VERSION) is str  # pylint: disable=unidiomatic-typecheck
    assert yaml.safe_dump({'cwlVersion': CWL_VERSION}) == "cwlVersion: v1.2\n"


@pytest.mark.fast
def test_a_generated_tool_declares_v1_2_and_dumps() -> None:
    """The CommandLineTool generator emits v1.2, and its output serialises.

    This generator is the one that was stuck on `v1.0`, and it is also where
    an unserialisable version value would first reach disk.
    """
    tool = generate_CWL_CommandLineTool({}, {})
    assert tool['cwlVersion'] == 'v1.2'
    assert 'cwlVersion: v1.2' in yaml.safe_dump(tool)


@pytest.mark.fast
def test_every_supported_version_is_admitted() -> None:
    """The versions the substrate toolchain runs are exactly the enum."""
    assert CWL_VERSIONS == ('v1.0', 'v1.1', 'v1.2')
