"""Source describes the code, not the process that produced it.

A reader has the source and not the trackers, so a register id names a row they
cannot open. Keep the fact, drop its discovery.
"""
import ast
import io
import re
import tokenize
from pathlib import Path
from typing import Final

import pytest

from .source_scan import REPO_ROOT, package_files, parsed

#: Tracker identifiers, abbreviated and spelled out both: a task is cited as
#: `T2.4` and as "Task 5".
#: NOT MATCHED: `wic0NN`, which is public contract -- matchable by a caller.
TRACKER_IDS: Final = (
    r'\bP\d{1,2}[a-c]?\b',                   # property register ids
    r'\bCE-\d+\b',                           # counterexample register ids
    r'\bT\d\.\d+\b',                         # task ids, abbreviated
    r'\bTasks? \d+(?:\s*[-\u2013]\s*\d+)?\b',  # task ids, spelled out
    r'(?<![\w/])#\d{3,}\b',                  # pull request numbers
)

#: Kept apart from the rest because it is the only one a `design_docs/` path
#: excuses: cited beside the document, a spec number names a section the reader
#: can open. Alone it names nothing.
SPEC_NUMBER: Final = r'\bSpec \d\b'

TRACKER_TOKENS: Final = re.compile('|'.join((*TRACKER_IDS, SPEC_NUMBER)))

#: The rule applied on a line that cites `design_docs/`. Exempting the whole
#: line would let a property, task or pull-request id ride along beside the
#: path, which is not what the exemption is for.
CITED_TOKENS: Final = re.compile('|'.join(TRACKER_IDS))

#: Narration of how the code came to be. A blacklist, not a boundary: no regex
#: decides whether a sentence narrates, so a green run means these phrasings
#: are absent and nothing more.
#: Case-sensitive at `this PR`, because `this property` is not narration.
PROCESS_NARRATION: Final = re.compile(
    r'an earlier (?:version|draft)|the first draft'
    r'|[Rr]eview\b[^.]{0,25}?\b(?:found|caught|added|proved|showed)'
    r'|found by mutation|round (?:\d+|one|two|three|four|five)\b'
    r'|as it stood before|semrefac|this PR\b|this pull request'
)

#: Exempt: this file alone, which cannot state the rule without spelling an
#: example of what it rejects.
ALLOWED: Final = frozenset({Path(__file__).resolve()})


def tracker_rule(text: str) -> re.Pattern[str]:
    """The rule for one line: the spec number is excused only beside a
    `design_docs/` path, where it names a section the reader can open."""
    return CITED_TOKENS if 'design_docs/' in text else TRACKER_TOKENS


def names_a_tracker_row(text: str) -> bool:
    """Whether a line cites something the reader cannot open."""
    return tracker_rule(text).search(text) is not None


def _prose(path: Path) -> list[tuple[int, str]]:
    """Every comment, docstring and string literal in a module.

    CANNOT SEE: text assembled at runtime -- an id interpolated into an
    f-string from a variable is invisible to any static scan.
    """
    source = path.read_text(encoding='utf-8')
    lines: list[tuple[int, str]] = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    for node in ast.walk(parsed(path)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lines.extend((node.lineno + i, text) for i, text in enumerate(node.value.splitlines()))
    return lines


def _scanned() -> list[Path]:
    """The package and the test suite, minus the allowlist."""
    tests = sorted((REPO_ROOT / 'tests').rglob('*.py'))
    return [path for path in package_files() + tests if path not in ALLOWED]


@pytest.mark.fast
def test_the_scan_sees_the_repo() -> None:
    """Zero files is a green test that enforces nothing."""
    assert len(_scanned()) > 50, 'the prose scan found almost nothing; it is aimed wrong'


@pytest.mark.fast
def test_no_tracker_identifier_reaches_the_source() -> None:
    """Register, task, spec and pull-request ids stay offline: they name rows
    in documents the reader does not have."""
    found = [
        f'{path.relative_to(REPO_ROOT)}:{line} {match.group(0)!r}'
        for path in _scanned()
        for line, text in _prose(path)
        if (match := tracker_rule(text).search(text))
    ]
    assert not found, (
        'tracker identifiers in source:\n  ' + '\n  '.join(found)
        + '\nState the claim instead. Diagnostic codes (wic0NN) are exempt: they are contract.')


@pytest.mark.fast
def test_no_known_narration_phrasing_reaches_the_source() -> None:
    """None of the phrasings in `PROCESS_NARRATION` is in the tree.

    DOES NOT SHOW that the source contains no narration; a reader can restate
    any of these a way no pattern catches. Judgement covers the rest, at review.
    """
    found = [
        f'{path.relative_to(REPO_ROOT)}:{line} {match.group(0)!r}'
        for path in _scanned()
        for line, text in _prose(path)
        if (match := PROCESS_NARRATION.search(text))
    ]
    assert not found, (
        'known narration phrasings in source:\n  ' + '\n  '.join(found)
        + '\nKeep the constraint, drop how it was discovered.')


@pytest.mark.fast
@pytest.mark.parametrize(('claim', 'text', 'pattern'), [
    ('a property id', '# P30 says emission is canonical', TRACKER_TOKENS),
    ('a single-digit property id', '# the P4 inverse-pair lesson', TRACKER_TOKENS),
    ('a counterexample id', '# see CE-11 for the shrunk case', TRACKER_TOKENS),
    ('a task id', '# delivered by T2.4', TRACKER_TOKENS),
    ('a spelled-out task id', '# delivered by Task 5', TRACKER_TOKENS),
    ('a spelled-out task range', '# Tasks 3-7 quantify over this', TRACKER_TOKENS),
    ('a bare spec number', '# Spec 2 owns this', TRACKER_TOKENS),
    ('a pull request', '# fixed in #412', TRACKER_TOKENS),
    ('review narration', '# review found this blind spot', PROCESS_NARRATION),
    ('an earlier version', '# an earlier version used a set', PROCESS_NARRATION),
    ('an earlier draft', '# an earlier draft used a set', PROCESS_NARRATION),
    ('a mutation story', '# found by mutation: the guard never fired', PROCESS_NARRATION),
    ('a spelled-out round', '# settled in round three of the rewrite', PROCESS_NARRATION),
    ('review with words between', '# review of the corpus found the gap', PROCESS_NARRATION),
    ('a pull request by pronoun', '# this PR fixes the generator', PROCESS_NARRATION),
])
def test_the_patterns_catch_what_they_claim_to(claim: str, text: str, pattern: re.Pattern[str]) -> None:
    """Each rule is shown firing, so a green run means it was checked.

    Samples isolate one alternative each: one that also matches a second
    alternative keeps passing when its own is removed.
    """
    assert pattern.search(text), claim


@pytest.mark.fast
@pytest.mark.parametrize('text', [
    '# wic006 is reported when a step has no id',
    '# the CWL v1.2 substrate declares this',
    '# see docs/dev/algorithms.md for namespacing',
    '# P is the port, not a property',
    '# this property quantifies over documents',
    '# the review lane runs on every push',
    '# See design_docs/core-refactor-design.md, Spec 1.',
])
def test_the_patterns_leave_real_prose_alone(text: str) -> None:
    """Diagnostic codes, versions and paths are content, not bookkeeping."""
    assert not names_a_tracker_row(text) and not PROCESS_NARRATION.search(text), text


@pytest.mark.fast
def test_the_scan_reads_every_surface_a_reader_meets(tmp_path: Path) -> None:
    """A comment after code, an assert message and a rationale field are prose.

    Under `tmp_path` on purpose: a probe inside `tests/` is itself scanned, so
    an interrupted run would leave the repo-wide scan failing on a file that is
    not part of the repository.
    """
    probe = tmp_path / 'probe.py'
    probe.write_text(
        'x = 1  # P30 says emission is canonical\n'
        'def f() -> None:\n'
        '    assert x, "the T2.4 rewrite owns this"\n'
        'T = Transformation(rationale="the CE-11 lesson")\n',
        encoding='utf-8')
    found = {match.group(0) for _, text in _prose(probe)
             if (match := tracker_rule(text).search(text))}
    assert found == {'P30', 'T2.4', 'CE-11'}, found


@pytest.mark.fast
def test_a_design_docs_citation_excuses_only_the_spec_number() -> None:
    """The exemption is for a section number, not for the rest of the line."""
    cited = 'See design_docs/core-refactor-design.md, Spec 1.'
    assert not names_a_tracker_row(cited)
    assert names_a_tracker_row(f'{cited} P30 covers it')
    assert names_a_tracker_row(f'{cited} delivered by Task 5')
