"""Cost against nesting depth.

A cost that doubles per nesting level is invisible at the depths a test uses and
unusable at the depths a partitioned workflow reaches, and a bound on one depth
cannot tell it from a cost that is merely large. So each claim here times the
same work at several depths and bounds the factor it multiplies by per level,
the way `test_scattering_scaling` bounds the quadratic term over five sizes.
A cost that only grows with the depth is under that bound at any depth a test
can afford, so it is pinned by counting the work instead.
"""
import math
import statistics
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import Final

import pytest

from sophios.ir.types import AuthoredName, DerivedName, Namespace, PortName, StepId

#: The most a cost may multiply by per nesting level. Polynomial growth in the
#: depth stays under 1.2 over the depths used here; doubling per level is 2.
MAX_GROWTH_PER_LEVEL: Final = 1.4

#: Runs timed per depth; the fastest is kept, since a pause only adds time.
REPEATS: Final = 3


def _fastest(run: Callable[[], object]) -> float:
    """The least CPU time of `REPEATS` runs: time the process spends running,
    which a busy machine does not stretch the way it stretches the clock."""
    best = math.inf
    for _ in range(REPEATS):
        started = time.process_time()
        run()
        best = min(best, time.process_time() - started)
    return best


def _assert_growth_is_bounded(work_at: Callable[[int], object], depths: Sequence[int]) -> None:
    """Time `work_at(depth)` for each depth and bound the factor per level.

    The factor is `exp(slope)` of the least-squares line through the log of the
    timings, so one noisy depth moves it by a fraction of its own error.
    """
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


@pytest.mark.fast
def test_hashing_a_nested_step_does_not_double_per_level() -> None:
    """A step id holds its namespace and the namespace holds every enclosing step
    id, so a hash that walks them is called twice as often at each level down."""
    _assert_growth_is_bounded(lambda depth: hash(_step_nested_in(depth)), range(10, 21, 2))


class _CountedHashes(str):
    """A name that counts how often it is hashed."""

    hashed = 0

    def __hash__(self) -> int:
        self.hashed += 1
        return super().__hash__()


@pytest.mark.fast
def test_hashing_a_derived_name_does_not_hash_what_it_was_derived_from_again() -> None:
    """A derived name is a key all through the compiler and holds the name it was
    derived from, which may be derived in turn, so a hash that walks them costs
    one step for every level the name was exposed through, on every lookup."""
    port = _CountedHashes('port')
    name: PortName = AuthoredName(port)
    for level in range(1, 6):
        name = DerivedName(StepId(Namespace(), level, 'level'), name)

    for _ in range(3):
        hash(name)

    assert port.hashed <= 1
