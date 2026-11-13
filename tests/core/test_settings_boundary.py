"""Configuration is a value parsed once at the boundary: nothing installs a command line over sys.argv to compute it,
and no argparse.Namespace travels past the CLI, so every flag reaches the compiler as given."""
import sys
from dataclasses import fields, is_dataclass
from operator import attrgetter
from pathlib import Path
from typing import Any, Final

import pytest

import sophios.cli
import sophios.compiler
import sophios.drawing
import sophios.main
from sophios.wic_types import GraphSettings

from .cases import DATA


#: A minimal workflow: enough to reach the compiler and the drawing, cheap enough to run once per setting. The
#: compiler's settings are caught before it does any work.
PROBE_WORKFLOW: Final = DATA / 'config' / 'w.wic'


class _Delivered(BaseException):
    """Ends the run once settings are captured; a BaseException, as `main` catches Exception and would swallow it."""

    def __init__(self, settings: Any) -> None:
        super().__init__('settings captured')
        self.settings = settings


#: Where each settings value is handed over, by its index in `_settings_fields`: the function, and
#: the position of the value among its arguments. The drawing is made only with `--graphviz`.
_RECEIVERS: Final = ((sophios.compiler, 'compile_source', 1, ()), (sophios.drawing, 'draw', 2, ('--graphviz',)))


def _settings_from_cli(monkeypatch: pytest.MonkeyPatch, workflow: Path, index: int, *flags: str) -> Any:
    """Run the real CLI via `main._main()` and return the settings the compiler (index 0) or the drawing (index 1)
    got. Driven end to end: the bug was a caller asking for defaults, which a test of the converter alone passed."""
    module, receiver, position, needs = _RECEIVERS[index]

    def capture(*args: Any, **_kwargs: Any) -> None:
        raise _Delivered(args[position])

    monkeypatch.setattr(module, receiver, capture)
    monkeypatch.setattr(sys, 'argv', ['sophios', '--yaml', str(workflow), *needs, *flags])
    try:
        sophios.main._main(*sophios.cli.parser.parse_known_args())
    except _Delivered as delivered:
        return delivered.settings
    raise AssertionError(f'{receiver} was never reached')


def _settings_fields() -> list[tuple[int, str, Any]]:
    """Every field of the two settings types, with its receiver, derived from the types so a new setting is covered
    the day it is added; a field of a policy the compile's settings hold by its path, such as `inference.strict`."""
    compile_fields = fields(sophios.compiler.CompileSettings)
    plain = [(field.name, field.type) for field in compile_fields if not is_dataclass(field.type)]
    nested = [(f'{field.name}.{inner.name}', inner.type)
              for field in compile_fields if is_dataclass(field.type) for inner in fields(field.type)]
    return [*((0, name, annotation) for name, annotation in plain + nested),
            *((1, name, annotation) for name, annotation in GraphSettings.__annotations__.items())]


def _non_default(name: str, annotation: type) -> tuple[list[str], object]:
    """A CLI spelling that sets `name` away from its default, and the value; the flag is the field's own name."""
    if name in SPELLED:
        return SPELLED[name]
    flag = name.rpartition('.')[2]
    if annotation is bool:
        return [f'--{flag}'], True          # every bool setting is store_true/False
    if annotation is int:
        return [f'--{flag}', '3'], 3
    return [f'--{flag}', f'chosen_{flag}'], f'chosen_{flag}'


#: Settings whose flag is not `--<name>`, or that the compiler gets in another form: the flags, and the value it gets.
#: The success codes are the codes given and those of the range, which by default holds 0 alone, sorted.
SPELLED: Final = {'inference.disabled': (['--inference_disable'], True),
                  'inference.use_naming_conventions': (['--no_inference_naming_conventions'], False),
                  'inference.strict': (['--inference_strict'], True),
                  'policy.partial_failure': (['--partial_failure_enable'], True),
                  'policy.success_codes': (['--partial_failure_success_codes', '3'], (0, 3))}

UNDELIVERABLE_BY_SENTINEL: Final = frozenset({
    # Values in global_config.json, not CLI flags, which main reads from that explicit configuration; and
    # inference's iteration limit, which nothing sets.
    'inference.format_rules', 'inference.renaming_conventions', 'inference.iteration_limit',
})

_DELIVERABLE: Final = [field for field in _settings_fields()
                       if field[1] not in UNDELIVERABLE_BY_SENTINEL]


@pytest.mark.fast
def test_every_setting_reaches_the_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    """A setting chosen on the command line is the setting the compiler, or the drawing, gets.

    One check over every field of the settings types, not a test per flag, and derived from the types instead of a
    list someone maintains. Every delivery bug found so far fails this: `main` asking for defaults, and the same
    mistake repeated in the end-to-end test helper, silently turned all of these into their defaults at once.
    """
    for index, name, annotation in _DELIVERABLE:
        flags, expected = _non_default(name, annotation)
        with monkeypatch.context() as patches:
            settings = _settings_from_cli(patches, PROBE_WORKFLOW, index, *flags)
        value = attrgetter(name)(settings) if index == 0 else settings[name]
        assert value == expected, (
            f'--{name} was set on the command line but {_RECEIVERS[index][1]} received '
            f'{value!r} instead of {expected!r}'
        )
