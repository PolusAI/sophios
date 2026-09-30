"""The CWL substrate version: one declared value, enforced at every emitting path.

The design specifies `.wic` as an abstraction over a single declared CWL version
(`design_docs/core-refactor-design.md` §5.2). At baseline three sites disagreed:
the compiler emitted `v1.2`, a CommandLineTool generator emitted `v1.0`, and the
schema accepted any non-empty string.
"""
from typing import Final

import pytest

import yaml
from jsonschema import Draft202012Validator

from sophios.lang import SophiosErrorCode, parse, wic_schema
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


#: The main schema, built once; these tests only read it.
_SCHEMA_VALIDATOR: Final = Draft202012Validator(wic_schema())


@pytest.mark.fast
@pytest.mark.parametrize('museum', ['draft-2', 'draft-3', 'v1.0.dev4', 'v1.2.0-dev5', 'v1.3', ''])
def test_an_unrunnable_version_is_refused_by_the_parser_and_the_schema(museum: str) -> None:
    """A version the toolchain cannot run is refused where it is written, not
    later inside the runner with a worse error.

    Accepting a version is a promise to process it. The CWL spec's full
    enumeration includes drafts cwltool dropped years ago and `*-dev*`
    snapshots gated behind `--enable-dev`; a `.wic` file declaring one used
    to sail through an any-non-empty-string schema and die downstream.
    `v1.3` and the empty string stand in for plain typos.

    Both readers of `Grammar.CWL_VERSION_VALUE` are checked, not this file's
    own imports: asserting membership in the tuple imported above is a
    tautology, and would pass with the check reverted in either place.
    """
    reported = [(d.code, d.span.start_line if d.span else None)
                for d in parse(f"cwlVersion: '{museum}'\n", 'museum.wic').diagnostics]
    assert reported == [(SophiosErrorCode.UNSUPPORTED_CWL_VERSION, 1)]
    assert list(_SCHEMA_VALIDATOR.iter_errors({'cwlVersion': museum})), \
        f'the schema accepted cwlVersion: {museum!r}'


@pytest.mark.fast
@pytest.mark.parametrize('version', sorted(CWL_VERSIONS))
def test_every_supported_version_is_accepted_by_the_parser_and_the_schema(version: str) -> None:
    """The same two readers admit each version the toolchain runs."""
    assert parse(f'cwlVersion: {version}\n', 'supported.wic').ok
    assert not list(_SCHEMA_VALIDATOR.iter_errors({'cwlVersion': version}))
