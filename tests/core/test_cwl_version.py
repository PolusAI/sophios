"""The CWL substrate version: one declared value, enforced at every emitting path.

The design specifies `.wic` as an abstraction over a single declared CWL version
(`design_docs/core-refactor-design.md` §5.2). At baseline three sites disagreed:
the compiler emitted `v1.2`, a CommandLineTool generator emitted `v1.0`, and the
schema accepted any non-empty string.

The enforcement is a static scan rather than a compilation, because the
failure it guards against is silent. A document declaring the wrong version is
still valid YAML and still runs, right up until a runner rejects a feature it
should have supported. A grep-shaped property catches the literal at the moment
someone reintroduces it.
"""
import ast
from functools import cache
from pathlib import Path
from typing import Final

import pytest
import yaml
from jsonschema import Draft202012Validator

from sophios.lang.cwl import CWL_VERSION, CWL_VERSIONS, CwlVersion
from sophios.python_cwl_adapter import generate_CWL_CommandLineTool
from sophios.schemas.wic_schema import wic_main_schema

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SRC: Final = REPO_ROOT / 'src' / 'sophios'

#: The one module allowed to contain version literals: it defines them.
OWNER: Final = SRC / 'lang' / 'cwl.py'


def _python_files() -> list[Path]:
    """Every Python file in the package except the module that owns the values."""
    return sorted(path for path in SRC.rglob('*.py') if path != OWNER)


def _version_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string constant that IS a CWL version, wherever it sits.

    Deliberately not a catalogue of binding shapes. An earlier version of this
    scan enumerated the assignments it knew about, and review found it blind
    to exactly the shapes this change had fixed in production: a literal as a
    `.get()`/`.setdefault()` default rides in a positional argument, and a
    keyword-only parameter default lives in `arguments.kw_defaults` — neither
    is an assignment node. Shape lists lose that race by construction, so the
    rule is total instead: outside the owner module, the version strings
    themselves are banned in every position. Anything that needs one imports
    it. Prose mentioning a version ('requires CWL v1.2') is a longer constant
    and never exactly equal, so it does not trip the ban. The one declared
    blind spot is a literal assembled at runtime — a computed string is not a
    constant, and finding it would mean executing the module.
    """
    return [(node.lineno, str(node.value)) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value in set(CWL_VERSIONS)]


# --------------------------------------------------------------------------
# Every emitting path declares the one substrate version: only lang/cwl.py
# may spell a CWL version literal; everyone else imports it
# --------------------------------------------------------------------------


@pytest.mark.fast
@pytest.mark.parametrize('path', _python_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_module_carries_its_own_cwl_version(path: Path) -> None:
    """Only `lang/cwl.py` spells a CWL version; everyone else imports it.

    A module that hardcodes its own is how `python_cwl_adapter.py` sat on
    `v1.0` while the compiler emitted `v1.2` — the two never had to agree
    because nothing made them.
    """
    literals = _version_literals(ast.parse(path.read_text(encoding='utf-8'), str(path)))
    assert not literals, (
        f'{path.relative_to(REPO_ROOT)} hardcodes a CWL version at '
        f'{[f"line {line}: {value!r}" for line, value in literals]}; '
        f'import CWL_VERSION from sophios.lang.cwl instead'
    )


@pytest.mark.fast
def test_the_scan_can_actually_fail() -> None:
    """The scan detects each shape it claims to, so the ban is not vacuous."""
    shapes = [
        "d['cwlVersion'] = 'v1.0'",
        "d = {'cwlVersion': 'v1.0'}",
        "cwl_version: str = 'v1.0'",
        "cwl_version = 'v1.0'",
        "d = dict(cwlVersion='v1.0')",
        "emit(cwl_version='v1.0')",
        # The three shapes review round three proved the old shape-list
        # scan missed — all in real production code at the time:
        "d['cwlVersion'] = d.get('cwlVersion', 'v1.0')",
        "d.setdefault('cwlVersion', 'v1.0')",
        "def f(*, cwl_version: str = 'v1.0'): pass",
    ]
    for source in shapes:
        assert _version_literals(ast.parse(source)), f'scan missed: {source}'


@pytest.mark.fast
def test_the_scan_ignores_prose_that_mentions_a_version() -> None:
    """Prose containing a version is not a finding; the exact literal is.

    The ban is on the version strings themselves, so even `label = 'v1.0'`
    is a finding now — if a label needs the version, it imports it. What
    must never trip the scan is a longer string that merely talks about one.
    """
    assert not _version_literals(ast.parse("msg = 'requires CWL v1.2 or newer'"))
    assert not _version_literals(ast.parse('"""Targets CWL v1.2."""'))
    assert _version_literals(ast.parse("label = 'v1.0'"))  # exact literal: banned


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
    own imports: review round three found the previous spelling asserted
    membership in the tuple it had just imported — a tautology that left the
    schema's `enum` (the production change) with zero coverage, so reverting
    it to any-non-empty-string passed everything. That revert now fails here.
    """
    errors = list(_schema_validator().iter_errors({'cwlVersion': museum}))
    assert errors, f'the shipped schema accepted cwlVersion: {museum!r}'


@pytest.mark.fast
@pytest.mark.parametrize('version', sorted(CWL_VERSIONS))
def test_the_real_schema_accepts_every_supported_version(version: str) -> None:
    """The same schema admits each version the toolchain runs."""
    assert not list(_schema_validator().iter_errors({'cwlVersion': version}))
