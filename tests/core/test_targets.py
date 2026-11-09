"""One table of targets, which every entry point reads (design_docs/one-target-table-design.md §2)."""
import importlib
from typing import get_args, get_type_hints

import pytest

from sophios import cli
from sophios.api.python.workflow import Workflow


@pytest.mark.package
@pytest.mark.fast
def test_the_table_the_command_line_and_the_python_api_name_the_same_targets() -> None:
    """A target the table holds is one `--target` and every `target=` accept, and nothing else is."""
    targets = set(importlib.import_module("sophios.targets").TARGETS)
    choices = next(action.choices for action in cli.parser._actions  # pylint: disable=protected-access
                   if action.dest == "target")
    named = {method: set(get_args(get_type_hints(getattr(Workflow, method))["target"]))
             for method in ("compile", "check", "run", "start", "run_async")}
    assert set(choices or ()) == targets
    assert named == {method: targets for method in named}
