"""One table of targets, which every entry point reads."""
import pytest

from sophios.targets import TARGETS
from sophios.wic_types import TARGET_NAMES


@pytest.mark.fast
def test_the_table_holds_the_targets_the_entry_points_name() -> None:
    """`--target` and every `target=` take `wic_types.TargetName`; the table holds exactly those targets."""
    assert set(TARGETS) == set(TARGET_NAMES)
