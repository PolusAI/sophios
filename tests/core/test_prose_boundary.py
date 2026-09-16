"""Source describes the code, not the process that produced it.

A reader has the source; they do not have the trackers, the pull requests, or
the review history. A register id in a comment names a row they cannot open,
and a sentence about how a defect came to light is a story about work they did
not do.

The facts those sentences carry are worth keeping. Their framing is not: state
the constraint, not its discovery.
"""
import ast
import re
from pathlib import Path
from typing import Final

import pytest

from .source_scan import REPO_ROOT, package_files, parsed

#: Tracker identifiers. Two deliberate absences: `wic0NN`, because a diagnostic
#: code is public contract, matchable by a caller and documented; and a spec
#: number cited alongside `design_docs/`, because that names a section of a
#: document the reader has. A bare spec number names nothing they can open.
TRACKER_TOKENS: Final = re.compile(
    r'\bP\d{2}[a-c]?\b'      # property register ids
    r'|\bCE-\d+\b'           # counterexample register ids
    r'|\bT\d\.\d+\b'         # task ids
    r'|\bSpec \d\b'          # specification numbers
    r'|(?<![\w/])#\d{3,}\b'  # pull request numbers
)

#: Narration of how the code came to be. The rule is that a reason survives and
#: its provenance does not, so these read as evidence that a rewrite was skipped.
PROCESS_NARRATION: Final = re.compile(
    r'an earlier version|the first draft|review (?:found|caught|added)'
    r'|found by mutation|round \d|as it stood before|semrefac',
    re.IGNORECASE,
)

#: A module docstring is a signpost. Past this, it is a document that belongs in
#: `design_docs/` where it can be read as one.
MAX_MODULE_DOCSTRING_LINES: Final = 12

#: The one file whose subject is the codes themselves.
ALLOWED: Final = frozenset({REPO_ROOT / 'src' / 'sophios' / 'lang' / 'diagnostics.py'})


def names_a_tracker_row(text: str) -> bool:
    """Whether a line of prose cites something the reader cannot open.

    Args:
        text (str): One line of docstring or comment.

    Returns:
        bool: True for a register, task, spec or pull-request id. A spec number
            beside a `design_docs/` path is a section of a document in this
            repository, so it is a reference and not bookkeeping.
    """
    return 'design_docs/' not in text and TRACKER_TOKENS.search(text) is not None


def _prose(path: Path) -> list[tuple[int, str]]:
    """Every docstring and comment line in a module, with its line number.

    Args:
        path (Path): The module to read.

    Returns:
        list[tuple[int, str]]: Line number and text, for prose only. Code is
            excluded so that a string literal under test is not mistaken for
            a docstring.
    """
    lines: list[tuple[int, str]] = []
    for offset, text in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        if text.lstrip().startswith('#'):
            lines.append((offset, text))
    for node in ast.walk(parsed(path)):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc:
            start = node.body[0].lineno
            lines.extend((start + i, text) for i, text in enumerate(doc.splitlines()))
    return lines


def _scanned() -> list[Path]:
    """Every module this boundary applies to.

    Returns:
        list[Path]: The package and the test suite, minus the allowlist.
    """
    tests = sorted((REPO_ROOT / 'tests').rglob('*.py'))
    return [path for path in package_files() + tests if path not in ALLOWED]


@pytest.mark.fast
def test_the_scan_sees_the_repo() -> None:
    """Zero files is a green test that enforces nothing."""
    assert len(_scanned()) > 50, 'the prose scan found almost nothing; it is aimed wrong'


@pytest.mark.fast
def test_no_tracker_identifier_reaches_the_source() -> None:
    """Property, counterexample, task, spec and pull-request ids stay offline.

    They name rows in documents the reader does not have. The claim a test
    makes belongs in its name and its docstring; the row that tracks it belongs
    in the tracker.
    """
    found = [
        f'{path.relative_to(REPO_ROOT)}:{line} {match.group(0)!r}'
        for path in _scanned()
        for line, text in _prose(path)
        if names_a_tracker_row(text) and (match := TRACKER_TOKENS.search(text))
    ]
    assert not found, (
        'tracker identifiers in source:\n  ' + '\n  '.join(found)
        + '\nState the claim instead. Diagnostic codes (wic0NN) are exempt: they are contract.')


@pytest.mark.fast
def test_no_comment_narrates_how_the_code_was_written() -> None:
    """A reason survives; the story of finding it does not.

    "An exclusion predicate of `lambda d: True` left the suite green" tells a
    reader about a review. "A per-entry check is vacuous when the mapping is
    empty" tells them why the code is shaped as it is, which is the part that
    stops them changing it back.
    """
    found = [
        f'{path.relative_to(REPO_ROOT)}:{line} {match.group(0)!r}'
        for path in _scanned()
        for line, text in _prose(path)
        if (match := PROCESS_NARRATION.search(text))
    ]
    assert not found, (
        'process narration in source:\n  ' + '\n  '.join(found)
        + '\nKeep the constraint, drop how it was discovered.')


@pytest.mark.fast
def test_no_module_docstring_is_a_document() -> None:
    """A module docstring says what the module is for, in a few lines."""
    found = [
        f'{path.relative_to(REPO_ROOT)}: {len(doc.splitlines())} lines'
        for path in _scanned()
        if (doc := ast.get_docstring(parsed(path), clean=False))
        and len(doc.splitlines()) > MAX_MODULE_DOCSTRING_LINES
    ]
    assert not found, (
        f'module docstrings over {MAX_MODULE_DOCSTRING_LINES} lines:\n  ' + '\n  '.join(found)
        + '\nA longer explanation belongs in design_docs/, where it reads as a document.')


@pytest.mark.fast
@pytest.mark.parametrize(('claim', 'text', 'pattern'), [
    ('a property id', '# P30 says emission is canonical', TRACKER_TOKENS),
    ('a counterexample id', '# see CE-11 for the shrunk case', TRACKER_TOKENS),
    ('a task id', '# delivered by T2.4', TRACKER_TOKENS),
    ('a bare spec number', '# Spec 2 owns this', TRACKER_TOKENS),
    ('a pull request', '# fixed in #412', TRACKER_TOKENS),
    ('review narration', '# review found this blind spot', PROCESS_NARRATION),
    ('an earlier draft', '# an earlier version used a set', PROCESS_NARRATION),
    ('a mutation story', '# found by mutation: the guard never fired', PROCESS_NARRATION),
])
def test_the_patterns_catch_what_they_claim_to(claim: str, text: str, pattern: re.Pattern[str]) -> None:
    """Each rule is shown firing, so a green run means it was checked.

    Args:
        claim (str): What the sample represents.
        text (str): A line the rule must reject.
        pattern (re.Pattern[str]): The rule under test.
    """
    assert pattern.search(text), claim


@pytest.mark.fast
@pytest.mark.parametrize('text', [
    '# wic006 is reported when a step has no id',
    '# the CWL v1.2 substrate declares this',
    '# see docs/dev/algorithms.md for namespacing',
    '# P is the port, not a property',
    '# See design_docs/core-refactor-design.md, Spec 1.',
])
def test_the_patterns_leave_real_prose_alone(text: str) -> None:
    """Diagnostic codes, versions and paths are content, not bookkeeping."""
    assert not names_a_tracker_row(text) and not PROCESS_NARRATION.search(text), text
