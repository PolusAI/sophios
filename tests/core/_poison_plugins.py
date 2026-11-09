"""A pytest plugin (`-p core._poison_plugins`) that makes plugin discovery an error: a module reaching `get_tools_cwl`
or `get_yml_paths` fails loudly instead of reading whatever the machine has."""
from typing import Any, NoReturn

import sophios.plugins


def _poisoned(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise AssertionError(
        'plugin discovery reached under the hermeticity run')


# setattr, not `sophios.plugins.get_tools_cwl = _poisoned`: pylint reads that assignment, takes every call to
# these functions in the repo for a call that never returns (NoReturn) and reports the code after it as unreachable.
for _name in ('get_tools_cwl', 'get_yml_paths'):
    setattr(sophios.plugins, _name, _poisoned)
