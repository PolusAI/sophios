"""The CWL substrate version: one declared value, enforced at every emitting path.

The design specifies `.wic` as an abstraction over a single declared CWL version
(`design_docs/core-refactor-design.md` §5.2). At baseline three sites disagreed:
the compiler emitted `v1.2`, a CommandLineTool generator emitted `v1.0`, and the
schema accepted any non-empty string.
"""
from functools import cache

import pytest

import yaml
from jsonschema import Draft202012Validator

from sophios.lang.cwl import CWL_VERSION, CWL_VERSIONS, CwlVersion
from sophios.python_cwl_adapter import generate_CWL_CommandLineTool
from sophios.schemas.wic_schema import wic_main_schema


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


@cache
def _schema_validator() -> Draft202012Validator:
    """The real main workflow schema, built with no tools, workflows, or
    store — enough for the `cwlVersion` field, which is what these tests own.
    Cached: the schema is deterministic and the tests below only read it."""
    return Draft202012Validator(wic_main_schema({}, [], {}))


@pytest.mark.fast
@pytest.mark.parametrize('museum', ['draft-2', 'draft-3', 'v1.0.dev4', 'v1.2.0-dev5', 'v1.3', ''])
def test_the_real_schema_rejects_unrunnable_versions(museum: str) -> None:
    """The shipped schema rejects what the toolchain cannot run — validation
    fails here, not later inside the runner with a worse error.

    Accepting a version is a promise to process it. The CWL spec's full
    enumeration includes drafts cwltool dropped years ago and `*-dev*`
    snapshots gated behind `--enable-dev`; a `.wic` file declaring one used
    to sail through the old any-non-empty-string schema and die downstream.
    `v1.3` and the empty string stand in for plain typos, caught by the same
    check for the same reason.

    This validates against `wic_main_schema` itself, not against this file's
    own imports. Asserting membership in the tuple this file has just imported
    is a tautology: it leaves the schema's `enum` — the production change —
    with no coverage at all, so reverting that to any-non-empty-string would
    pass. Validating against the schema is what makes such a revert fail.
    """
    errors = list(_schema_validator().iter_errors({'cwlVersion': museum}))
    assert errors, f'the shipped schema accepted cwlVersion: {museum!r}'


@pytest.mark.fast
@pytest.mark.parametrize('version', sorted(CWL_VERSIONS))
def test_the_real_schema_accepts_every_supported_version(version: str) -> None:
    """The same schema admits each version the toolchain runs."""
    assert not list(_schema_validator().iter_errors({'cwlVersion': version}))
