"""Configuration is a value, parsed once at the boundary.

Two claims, and both used to be false:

**Nothing synthesises a command line to obtain configuration.** `get_args`
built a full argv and installed it over `sys.argv` with `unittest.mock.patch`
so that a no-argument `parse_args()` would read it — a test idiom on the
production path, mutating global process state to compute a value, in a
library other people embed. `parse_args` accepts an explicit list, so none of
that was ever necessary.

**No `argparse.Namespace` travels past the CLI.** `main` parsed the user's
arguments and then asked for a fresh set of defaults, so every flag arrived at
the compiler as its default: `--allow_raw_cwl` permitted nothing,
`--inference_disable` disabled nothing, and the graph settings were whatever
the parser said. The fix is not to pass `args` further down — that would put a
CLI type into library signatures — but to convert at the boundary and pass the
settings themselves.
"""
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

import yaml

import sophios.cli
import sophios.compiler
import sophios.main
from sophios.wic_types import CompilerOptions, GraphSettings


@pytest.mark.fast
def test_defaults_are_available_without_a_command_line() -> None:
    """A library caller can ask for defaults by name, and gets the documented ones.

    The other half of the split. The CLI property below covers what a user
    *chooses*; this covers what a caller gets when nobody chooses — which is
    the path the Python API, the schema generator and a real-time analysis all
    take, and therefore the one the corpus workflows compile through. Deleting
    it as "subsumed" was wrong: the property never names this function.
    """
    options, graph = sophios.cli.default_compilation_settings()
    assert options['allow_raw_cwl'] is False
    assert graph['graph_dark_theme'] is False


@pytest.mark.fast
def test_the_converter_requires_arguments() -> None:
    """`get_dicts_for_compilation` has no default parse to fall back on.

    Its old default was a fresh parse of a synthesised argv, which is what let
    `main` hold the user's arguments and silently compile with defaults. A
    caller must now either pass what it parsed or say `default_...` out loud.
    """
    # Bound through a loosely typed alias so the call is a runtime experiment
    # rather than something the type checker rejects before it runs.
    converter: Callable[..., object] = sophios.cli.get_dicts_for_compilation
    with pytest.raises(TypeError):
        converter()


# --------------------------------------------------------------------------
# Every setting the user chooses is the setting the compiler is handed
# --------------------------------------------------------------------------

#: A minimal workflow: enough to reach the compiler, cheap enough to run once
#: per setting. Compilation is intercepted before it does any work.
PROBE_WORKFLOW: Final = {'steps': [{'id': 'touch', 'in': {'filename': {'wic_inline_input': 'empty.txt'}}}]}


class _Delivered(BaseException):
    """Ends the run once the settings have been captured.

    A `BaseException`, not an `Exception`: `main` catches `Exception` to turn
    compile failures into exit codes, and would otherwise swallow this and
    write an error file.
    """

    def __init__(self, settings: tuple[Any, Any]) -> None:
        super().__init__('settings captured')
        self.settings = settings


@pytest.fixture(name='settings_from_cli')
def _settings_from_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., tuple[Any, Any]]:
    """Run the real CLI and return the two settings dicts the compiler got.

    Deliberately driven through `main._main()` with a real argv rather than by
    calling the converter: the bug this guards was never in the conversion, it
    was in a caller that had the user's arguments and asked for defaults
    anyway. A test of the converter alone passed throughout.

    That makes these integration tests, and `fast` is a claim about cost
    rather than about layer: compilation is intercepted before it does any
    work, so a case runs in well under a second. They stay in the fast lane
    because a delivery bug is exactly the kind one wants to hear about on the
    first run, not the nightly one.
    """
    def run(*flags: str) -> tuple[Any, Any]:
        workflow = tmp_path / 'probe.wic'
        workflow.write_text(yaml.safe_dump(PROBE_WORKFLOW), encoding='utf-8')

        def capture(_bundle: Any, compiler_options: Any, graph_settings: Any,
                    *_args: Any, **_kwargs: Any) -> None:
            raise _Delivered((compiler_options, graph_settings))

        # The CLI reads files, so it enters through the source door.
        monkeypatch.setattr(sophios.compiler, 'compile_source', capture)
        monkeypatch.setattr(sys, 'argv', ['sophios', '--yaml', str(workflow), *flags])
        try:
            sophios.main._main(*sophios.cli.parser.parse_known_args())
        except _Delivered as delivered:
            return delivered.settings
        raise AssertionError('the compiler was never reached')
    return run


def _settings_fields() -> list[tuple[int, str, type]]:
    """Every field of the two settings types, with which dict it belongs to.

    Derived from the `TypedDict`s rather than listed here, so a setting added
    later is covered the day it is added — the failure mode being guarded is
    precisely that someone adds a flag and nobody notices it is not delivered.
    """
    return [(index, name, annotation)
            for index, settings_type in enumerate((CompilerOptions, GraphSettings))
            for name, annotation in settings_type.__annotations__.items()]


def _non_default(name: str, annotation: type) -> tuple[list[str], object]:
    """A CLI spelling that sets `name` away from its default, and the value.

    The flag is the field name: argparse and the settings dicts use the same
    words, which is what makes this derivable at all.
    """
    if annotation is bool:
        return [f'--{name}'], True          # every bool setting is store_true/False
    if annotation is int:
        return [f'--{name}', '3'], 3
    return [f'--{name}', f'chosen_{name}'], f'chosen_{name}'


UNDELIVERABLE_BY_SENTINEL: Final = frozenset({
    # These are values in global_config.json, not CLI flags. They still travel
    # in CompilerOptions after main has loaded that explicit configuration.
    'inference_rules', 'renaming_conventions',
})

_DELIVERABLE: Final = [field for field in _settings_fields()
                       if field[1] not in UNDELIVERABLE_BY_SENTINEL]


@pytest.mark.fast
@pytest.mark.parametrize('index,name,annotation', _DELIVERABLE,
                         ids=[name for _index, name, _annotation in _DELIVERABLE])
def test_every_setting_reaches_the_compiler(settings_from_cli: Callable[..., tuple[Any, Any]],
                                            index: int, name: str, annotation: type) -> None:
    """A setting chosen on the command line is the setting the compiler gets.

    One property instead of a test per flag, and derived from the settings
    types instead of a list someone maintains. Every delivery bug found so far
    fails this: `main` asking for defaults, and the same mistake repeated in
    the end-to-end test helper, silently turned all of these into their
    defaults at once.
    """
    flags, expected = _non_default(name, annotation)
    settings = settings_from_cli(*flags)
    assert settings[index][name] == expected, (
        f'--{name} was set on the command line but the compiler received '
        f'{settings[index][name]!r} instead of {expected!r}'
    )
