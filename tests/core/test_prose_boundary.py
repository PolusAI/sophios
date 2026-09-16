"""Source describes the code, not the process that produced it.

A reader has the source; they do not have the trackers, the pull requests, or
the review history. A register id in a comment names a row they cannot open,
and a sentence about how a defect came to light is a story about work they did
not do.

The facts those sentences carry are worth keeping. Their framing is not: state
the constraint, not its discovery.
"""
import ast
import io
import re
import tokenize
from pathlib import Path
from typing import Final

import pytest

from .source_scan import REPO_ROOT, package_files, parsed

#: Tracker identifiers, in both the abbreviated and the spelled-out form: a
#: task is cited as `T2.4` and as "Task 5", and a rule that knows only the
#: first leaves the second in the tree. `wic0NN` is deliberately absent, because
#: a diagnostic code is public contract, matchable by a caller and documented.
TRACKER_IDS: Final = (
    r'\bP\d{2}[a-c]?\b',                     # property register ids
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

#: Narration of how the code came to be. The rule is that a reason survives and
#: its provenance does not, so these read as evidence that a rewrite was skipped.
PROCESS_NARRATION: Final = re.compile(
    r'an earlier (?:version|draft)|the first draft|review (?:found|caught|added)'
    r'|found by mutation|round \d|as it stood before|semrefac',
    re.IGNORECASE,
)

#: A module docstring is a signpost, and a signpost may still declare what the
#: module cannot do — several here carry a CANNOT DETECT or LIMITS register and
#: earn their length. Past this it stops being a signpost and becomes a document,
#: which belongs in `design_docs/` where it reads as one. The four that exceeded
#: it were 40 to 79 lines and were narratives, not registers.
MAX_MODULE_DOCSTRING_LINES: Final = 25

#: The two files whose subject is the identifiers themselves: the diagnostics
#: register, which defines the codes, and this module, which cannot state the
#: rule without spelling an example of what the rule rejects.
ALLOWED: Final = frozenset({
    REPO_ROOT / 'src' / 'sophios' / 'lang' / 'diagnostics.py',
    Path(__file__).resolve(),
})


def tracker_rule(text: str) -> re.Pattern[str]:
    """The rule that applies to one line.

    Args:
        text (str): One line of docstring, comment or assert message.

    Returns:
        re.Pattern[str]: The full rule, or the rule without the spec number
            when the line cites `design_docs/` and the number therefore names
            a section of a document the reader has.
    """
    return CITED_TOKENS if 'design_docs/' in text else TRACKER_TOKENS


def names_a_tracker_row(text: str) -> bool:
    """Whether a line of prose cites something the reader cannot open.

    Args:
        text (str): One line of docstring, comment or assert message.

    Returns:
        bool: True for a register, task, spec or pull-request id.
    """
    return tracker_rule(text).search(text) is not None


def _assert_message(node: ast.Assert) -> list[tuple[int, str]]:
    """Every string a failed assertion would print, with its line number.

    Walked rather than read directly, because a message is as often built --
    concatenated, or interpolated -- as it is written as one literal.

    Args:
        node (ast.Assert): The assertion.

    Returns:
        list[tuple[int, str]]: Line number and text for each string part.
    """
    if node.msg is None:
        return []
    return [
        (part.lineno + offset, text)
        for part in ast.walk(node.msg)
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
        for offset, text in enumerate(part.value.splitlines())
    ]


def _prose(path: Path) -> list[tuple[int, str]]:
    """Every line of a module addressed to a reader, with its line number.

    Three surfaces, not one. Comments are tokenized rather than matched by a
    leading `#`, which sees only a comment on its own line and misses one after
    code; and an assert message is prose a reader meets at the moment the test
    fails, so a tracker id there is exactly as unopenable as one in a comment.
    Code is otherwise excluded, so a string literal under test is not mistaken
    for a docstring.

    Args:
        path (Path): The module to read.

    Returns:
        list[tuple[int, str]]: Line number and text, for prose only.
    """
    source = path.read_text(encoding='utf-8')
    lines: list[tuple[int, str]] = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    for node in ast.walk(parsed(path)):
        if isinstance(node, ast.Assert):
            lines.extend(_assert_message(node))
        elif isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
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
        if (match := tracker_rule(text).search(text))
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
    ('a spelled-out task id', '# delivered by Task 5', TRACKER_TOKENS),
    ('a spelled-out task range', '# Tasks 3-7 quantify over this', TRACKER_TOKENS),
    ('a bare spec number', '# Spec 2 owns this', TRACKER_TOKENS),
    ('a pull request', '# fixed in #412', TRACKER_TOKENS),
    ('review narration', '# review found this blind spot', PROCESS_NARRATION),
    ('an earlier version', '# an earlier version used a set', PROCESS_NARRATION),
    ('an earlier draft', '# an earlier draft used a set', PROCESS_NARRATION),
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


@pytest.mark.fast
def test_the_scan_reads_every_surface_a_reader_meets() -> None:
    """A comment after code and an assert message are prose too.

    Collecting only lines that *begin* with `#` sees a comment on its own line
    and misses one sitting after code, and an assert message is the prose a
    reader meets at the exact moment a test fails. Both surfaces carried
    tracker ids while the scan reported none, so the rule is pinned to the
    surfaces rather than to the one that happened to be read first.
    """
    module = (
        'x = 1  # P30 says emission is canonical\n'
        'def f() -> None:\n'
        '    assert x, "the T2.4 rewrite owns this"\n'
    )
    scratch = REPO_ROOT / 'tests' / 'core' / '_prose_probe.py'
    scratch.write_text(module, encoding='utf-8')
    try:
        found = {match.group(0) for _, text in _prose(scratch)
                 if (match := tracker_rule(text).search(text))}
    finally:
        scratch.unlink()
    assert found == {'P30', 'T2.4'}, found


@pytest.mark.fast
def test_a_design_docs_citation_excuses_only_the_spec_number() -> None:
    """The exemption is for a section number, not for the rest of the line.

    Skipping the whole line lets a property, task or pull-request id ride along
    beside the path, which is the opposite of what citing a document in the
    repository is supposed to license.
    """
    cited = 'See design_docs/core-refactor-design.md, Spec 1.'
    assert not names_a_tracker_row(cited)
    assert names_a_tracker_row(f'{cited} P30 covers it')
    assert names_a_tracker_row(f'{cited} delivered by Task 5')
