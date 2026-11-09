"""The CWL substrate version: one declared value, enforced at every emitting path; the compiler, a tool generator and
the schema once disagreed (v1.2, v1.0, any string)."""
import json
from typing import Final

import pytest
import yaml
from cwltool.update import ALLUPDATES, UPDATES
from hypothesis import given, strategies as st
from jsonschema import Draft202012Validator

from sophios.lang import SophiosErrorCode, parse, wic_schema
from sophios.lang.cwl import CWL_VERSION

from .budgets import budget
from .strategies import pinned


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


#: The main schema, built once; these tests only read it.
_SCHEMA_VALIDATOR: Final = Draft202012Validator(wic_schema())


@pytest.mark.fast
@given(st.one_of(st.sampled_from(sorted(ALLUPDATES)), st.text('v1.023-dev ', max_size=8), st.floats(0, 2), st.none(),
                 st.lists(st.just('v1.2'), max_size=1)))
@pinned('draft-2', 'draft-3', 'v1.0.dev4', 'v1.2.0-dev5', 'v1.3', '', *sorted(UPDATES))
@budget(100)
def test_a_version_is_accepted_by_the_parser_and_the_schema_exactly_when_cwltool_runs_it(version: object) -> None:
    """A version the toolchain cannot run is refused where it is written, not
    later inside the runner with a worse error.

    Accepting a version is a promise to process it. The CWL spec's full
    enumeration includes drafts cwltool dropped years ago and `*-dev*`
    snapshots gated behind `--enable-dev`; a `.wic` file declaring one used
    to sail through an any-non-empty-string schema and die downstream.
    `v1.3` and the empty string stand in for plain typos.

    Both readers of `Grammar.CWL_VERSION_VALUE` are checked against what cwltool
    runs without `--enable-dev`, not against this module's own tuple: asserting
    membership in it is a tautology, and would pass with the check reverted in either place.
    """
    runs = version in tuple(UPDATES)
    reported = [(d.code, d.span.start_line if d.span else None)
                for d in parse(f'cwlVersion: {json.dumps(version)}\n', 'version.wic').diagnostics]
    assert reported == ([] if runs else [(SophiosErrorCode.UNSUPPORTED_CWL_VERSION, 1)])
    assert _SCHEMA_VALIDATOR.is_valid({'cwlVersion': version}) is runs
