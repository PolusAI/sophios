"""Hypothesis example budgets: `budget(n)` is `n` examples, times SOPHIOS_PROPERTY_SCALE when set (the weekly lane).
No seed is pinned; `print_blob` prints the `@reproduce_failure` line that replays a failure."""
import os
from typing import Final

from hypothesis import HealthCheck, settings

#: The environment variable that multiplies every budget.
SCALE_VARIABLE: Final = 'SOPHIOS_PROPERTY_SCALE'


def _scale() -> int:
    raw = os.environ.get(SCALE_VARIABLE, '1')
    try:
        scale = int(raw)
    except ValueError:
        scale = 0
    if scale < 1:
        raise ValueError(f'{SCALE_VARIABLE} must be a positive integer, found {raw!r}')
    return scale


#: Read once, here, and nowhere else.
SCALE: Final = _scale()


def budget(max_examples: int) -> settings:
    """`max_examples`, scaled, with the health check and deadline every
    property in this suite relaxes: generated workflows are slow to compile."""
    return settings(max_examples=max_examples * SCALE,
                    suppress_health_check=[HealthCheck.too_slow], deadline=None, print_blob=True)
