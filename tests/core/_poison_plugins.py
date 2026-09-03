"""A pytest plugin that makes plugin discovery an error.

Loaded with `-p core._poison_plugins`. Any module that reaches
`get_tools_cwl` or `get_yml_paths` under this plugin fails loudly instead of
quietly reading whatever the machine happens to have.
"""
from typing import Any, NoReturn

import sophios.plugins


def _poisoned(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise AssertionError(
        'plugin discovery reached under the hermeticity run; '
        'see design_docs/core-refactor-design.md §6.1')


sophios.plugins.get_tools_cwl = _poisoned
sophios.plugins.get_yml_paths = _poisoned
