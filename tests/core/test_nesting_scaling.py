"""Cost against nesting depth: each claim times the same work at several depths and bounds the factor per level, since
one depth cannot tell a doubling from a large constant. A cost that only grows with depth is pinned by counting work."""
import math
import statistics
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import Final

import pytest

from sophios.ir.types import AuthoredName, DerivedName, Namespace, PortName, StepId

from .hermetic import compile_hermetic, nested_workflow

#: The most a cost may multiply by per nesting level. Polynomial growth in the
#: depth stays under 1.2 over the depths used here; doubling per level is 2.
MAX_GROWTH_PER_LEVEL: Final = 1.4

#: Samples taken per depth; the fastest is kept, since a pause only adds time.
REPEATS: Final = 3

#: CPU seconds one sample runs the work for. The clock that counts them advances
#: in ticks as coarse as 15.6 ms (Windows), so a sample must span many ticks or
#: the work reads as taking no time at all.
SAMPLE_SECONDS: Final = 0.05


def _fastest(run: Callable[[], object]) -> float:
    """The least CPU time of one run over `REPEATS` samples (a busy machine stretches the clock, not process time); a
    sample repeats the run for `SAMPLE_SECONDS`."""
    best = math.inf
    for _ in range(REPEATS):
        runs = 0
        started = time.process_time()
        while True:
            run()
            runs += 1
            elapsed = time.process_time() - started
            if elapsed >= SAMPLE_SECONDS:
                break
        best = min(best, elapsed / runs)
    return best


def _assert_growth_is_bounded(work_at: Callable[[int], object], depths: Sequence[int]) -> None:
    """Time `work_at(depth)` per depth and bound the factor per level: `exp(slope)` of the least-squares line through
    the log timings."""
    seconds = {depth: _fastest(partial(work_at, depth)) for depth in depths}
    slope = statistics.linear_regression(list(seconds), [math.log(s) for s in seconds.values()]).slope
    growth = math.exp(slope)
    timings = ', '.join(f'{depth}: {taken * 1000:.2f} ms' for depth, taken in seconds.items())
    assert growth < MAX_GROWTH_PER_LEVEL, (
        f'the cost multiplies by {growth:.2f} per nesting level, over the bound of '
        f'{MAX_GROWTH_PER_LEVEL}. Timings: {timings}')


def _step_nested_in(depth: int) -> StepId:
    """A step with `depth` steps enclosing it, each in the namespace of the last."""
    step = StepId(Namespace(), 1, 'level')
    for _ in range(depth):
        step = StepId(step.namespace.child(step), 1, 'level')
    return step


class _CountedHashes(str):
    """A name that counts how often it is hashed."""

    hashed = 0

    def __hash__(self) -> int:
        self.hashed += 1
        return super().__hash__()


@pytest.mark.fast
def test_the_cost_of_a_nested_step_does_not_double_per_nesting_level() -> None:
    """Every phase names the steps of a nested workflow by identities that hold their enclosing steps, so anything that
    writes them out, hashes them or reports on them eagerly pays twice as much at each level down: compiling a nested
    workflow, and hashing a step id, whose namespace holds every enclosing step id, keep their growth per level
    bounded; and a derived name, a key all through the compiler that holds the name it was derived from, hashes the
    name it was derived from once, not once per level it was exposed through."""
    _assert_growth_is_bounded(lambda depth: compile_hermetic(nested_workflow(depth)), range(6, 13, 2))
    _assert_growth_is_bounded(lambda depth: hash(_step_nested_in(depth)), range(10, 21, 2))
    port = _CountedHashes('port')
    name: PortName = AuthoredName(port)
    for level in range(1, 6):
        name = DerivedName(StepId(Namespace(), level, 'level'), name)
    for _ in range(3):
        hash(name)
    assert port.hashed <= 1
