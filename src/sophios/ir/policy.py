"""How a compiled workflow runs: the run policy.

Partial failure is compile input. A job that exits with one of the policy's
success codes counts as a success, and leaves `null` where it wrote nothing; a
step runs only once every input it requires has arrived. Each document is written
for the policy as it is emitted, and nothing rewrites the CWL afterwards.
"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunPolicy:
    """Whether a job may fail partially, and the exit codes that then count as success.

    The command line's `--partial_failure_enable`, `--partial_failure_success_codes` and
    `--partial_failure_success_codes_range`, as one value. Under partial failure a job that exits with one of
    `success_codes` counts as a success, leaving `null` where it wrote nothing, and a step runs only once every input
    it requires has a value (docs/partial-failures.md).

    Args:
        partial_failure (bool): Let a job fail partially; `--partial_failure_enable`.
        success_codes (tuple[int, ...]): The exit codes that then count as success, kept sorted and without repeats;
            0 always does. `(0, 1)` by default, as on the command line.
    """

    partial_failure: bool = False
    #: Sorted, without repeats, 0 always among them.
    success_codes: tuple[int, ...] = (0, 1)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'success_codes', tuple(sorted({0, *self.success_codes})))
