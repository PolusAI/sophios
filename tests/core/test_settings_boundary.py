"""Configuration is a value parsed once at the boundary: nothing installs a command line over sys.argv to compute it,
and no argparse.Namespace travels past the CLI, so every flag reaches the compiler as given."""
import sys
from pathlib import Path
from typing import Any, Final

import pytest

import sophios.cli
import sophios.compiler
import sophios.drawing
import sophios.main
from sophios.wic_types import CompilerOptions, GraphSettings

from .cases import DATA


#: A minimal workflow: enough to reach the compiler and the drawing, cheap enough to run once per setting. The
#: compiler's options are caught before it does any work.
PROBE_WORKFLOW: Final = DATA / 'config' / 'w.wic'


class _Delivered(BaseException):
    """Ends the run once settings are captured; a BaseException, as `main` catches Exception and would swallow it."""

    def __init__(self, settings: Any) -> None:
        super().__init__('settings captured')
        self.settings = settings


#: Where each settings dict is handed over, by its index in `_settings_fields`: the function, and
#: the position of the dict among its arguments. The drawing is made only with `--graphviz`.
_RECEIVERS: Final = ((sophios.compiler, 'compile_source', 1, ()), (sophios.drawing, 'draw', 2, ('--graphviz',)))


def _settings_from_cli(monkeypatch: pytest.MonkeyPatch, workflow: Path, index: int, *flags: str) -> Any:
    """Run the real CLI via `main._main()` and return the settings dict the compiler (index 0) or the drawing (index 1)
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


def _settings_fields() -> list[tuple[int, str, type]]:
    """Every field of the two settings types, with its dict, derived from the `TypedDict`s so a new setting is covered
    the day it is added."""
    return [(index, name, annotation)
            for index, settings_type in enumerate((CompilerOptions, GraphSettings))
            for name, annotation in settings_type.__annotations__.items()]


def _non_default(name: str, annotation: type) -> tuple[list[str], object]:
    """A CLI spelling that sets `name` away from its default, and the value; the flag is the field name."""
    if name in OFF_SWITCHES:
        return [OFF_SWITCHES[name]], False
    if name in SPELLED:
        return SPELLED[name]
    if annotation is bool:
        return [f'--{name}'], True          # every bool setting is store_true/False
    if annotation is int:
        return [f'--{name}', '3'], 3
    return [f'--{name}', f'chosen_{name}'], f'chosen_{name}'


#: Settings that are on by default: the flag that turns each off, instead of `--<name>`.
OFF_SWITCHES: Final = {'inference_use_naming_conventions': '--no_inference_naming_conventions'}
#: Settings the compiler gets in another form than the flag's: the flags, and the value it gets. The success codes
#: are the codes given, then those of the range, which by default holds 0 alone.
SPELLED: Final = {'partial_failure_success_codes': (['--partial_failure_success_codes', '3'], (3, 0))}

UNDELIVERABLE_BY_SENTINEL: Final = frozenset({
    # These are values in global_config.json, not CLI flags. They still travel
    # in CompilerOptions after main has loaded that explicit configuration.
    'inference_rules', 'renaming_conventions',
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
        assert settings[name] == expected, (
            f'--{name} was set on the command line but {_RECEIVERS[index][1]} received '
            f'{settings[name]!r} instead of {expected!r}'
        )
