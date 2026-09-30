"""Hypothesis example budgets, and the one knob that deepens all of them.

Every property's budget is `budget(n)`: `n` examples on an ordinary run, and
`n` times `SOPHIOS_PROPERTY_SCALE` when that is set. The weekly property lane
(`.github/workflows/property_weekly.yml`) sets it; nothing else does, so an
ordinary run draws exactly the examples it always did.

Nothing here pins a seed or derandomizes, so each run draws fresh examples.
`print_blob` makes a failure print the `@reproduce_failure` line that replays
it, beside the falsifying example itself.
"""
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
