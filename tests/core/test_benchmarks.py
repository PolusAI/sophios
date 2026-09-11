"""Fast contracts for the non-gating weekly benchmark harness."""

from collections.abc import Iterator

import pytest

from . import benchmarks
from .benchmarks import Case


@pytest.mark.fast
def test_every_case_has_a_rationale() -> None:
    """A case nobody can explain is a number nobody will act on."""
    assert all(case.rationale.strip() for case in benchmarks.CASES)


@pytest.mark.fast
def test_corpus_setup_precedes_the_first_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugin discovery cannot be charged to the first sorted corpus row."""
    events: list[str] = []

    def corpus_setup() -> object:
        events.append('setup')
        return object()

    def clock() -> float:
        events.append('clock')
        return 0.0

    def run_case() -> None:
        events.append('run')

    monkeypatch.setattr(benchmarks, '_get_corpus_env', corpus_setup)
    monkeypatch.setattr(benchmarks.time, 'perf_counter', clock)
    monkeypatch.setattr(
        benchmarks, 'CASES',
        (Case('corpus/one', 'corpus case', run_case),))

    assert benchmarks.main() == 0
    assert events == ['setup', 'clock', 'run', 'clock']


@pytest.mark.fast
def test_main_reports_corpus_and_whole_run_totals(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Parse and compile aggregates are directly comparable in the table."""
    ticks: Iterator[float] = iter((0.0, 1.0, 1.0, 3.0, 3.0, 6.0))
    monkeypatch.setattr(benchmarks, '_get_corpus_env', object)
    monkeypatch.setattr(benchmarks.time, 'perf_counter', lambda: next(ticks))
    monkeypatch.setattr(benchmarks, 'CASES', (
        Case('corpus/one', 'first corpus case', lambda: None),
        Case('corpus/two', 'second corpus case', lambda: None),
        Case('parse_only', 'aggregate parse case', lambda: None),
    ))

    assert benchmarks.main() == 0
    output = capsys.readouterr().out
    assert '| corpus (total) | 3.000 |' in output
    assert '| all cases (total) | 6.000 |' in output


@pytest.mark.fast
def test_the_harness_reports_a_failing_case_without_failing_itself(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A case error is a table result, never a benchmark gate."""
    def explode() -> None:
        raise RuntimeError('deliberate')

    monkeypatch.setattr(
        benchmarks, 'CASES',
        (Case('boom', 'proves the harness tolerates a failure', explode),))
    assert benchmarks.main() == 0
    assert 'error: RuntimeError' in capsys.readouterr().out
