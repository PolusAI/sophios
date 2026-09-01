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
import ast
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

import sophios.cli
import sophios.compiler
import sophios.main
from sophios.wic_types import CompilerOptions, GraphSettings, YamlTagPaths

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SRC: Final = REPO_ROOT / 'src' / 'sophios'

#: The one module that may hold an `argparse.Namespace`: it makes them.
PARSER_MODULE: Final = SRC / 'cli.py'

#: The CLI entry point, which parses and immediately converts.
CLI_ADAPTER: Final = SRC / 'main.py'


def _python_files() -> list[Path]:
    """Every Python file in the package."""
    return sorted(SRC.rglob('*.py'))


@pytest.mark.fast
def test_the_scans_are_pointed_at_something() -> None:
    """The file list is non-empty.

    Both scans below are parametrised over it, and pytest *skips* an empty
    parameter set rather than failing it — so if `src/` ever moves, or the
    suite runs against an installed package, this file's two headline claims
    would pass without reading a line. Proving the matcher works is not the
    same as proving it was aimed at anything.
    """
    assert _python_files(), 'no package modules discovered; the scan is broken'


def _patches_argv(tree: ast.AST) -> list[int]:
    """Lines that install a value over `sys.argv`."""
    found: list[int] = []
    for node in ast.walk(tree):
        match node:
            case ast.Call(func=ast.Attribute(attr='object'), args=[ast.Name(id='sys'), *_]):
                found.append(node.lineno)  # patch.object(sys, 'argv', ...)
            case ast.Assign(targets=[ast.Attribute(value=ast.Name(id='sys'), attr='argv')]):
                found.append(node.lineno)  # sys.argv = ...
    return found


@pytest.mark.fast
@pytest.mark.parametrize('path', _python_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_module_synthesises_a_command_line(path: Path) -> None:
    """Configuration is never obtained by faking argv."""
    lines = _patches_argv(ast.parse(path.read_text(encoding='utf-8'), str(path)))
    assert not lines, (
        f'{path.relative_to(REPO_ROOT)} installs a value over sys.argv at {lines}; '
        f'pass the arguments to parse_args() explicitly instead'
    )


@pytest.mark.fast
def test_the_scan_can_actually_fail() -> None:
    """The scan sees both spellings, so it is not vacuous."""
    patched = "from unittest.mock import patch\nimport sys\nwith patch.object(sys, 'argv', []):\n    pass"
    assert _patches_argv(ast.parse(patched))
    assert _patches_argv(ast.parse("import sys\nsys.argv = ['x']"))


def _namespace_parameters(tree: ast.AST) -> list[tuple[int, str]]:
    """Functions that accept an `argparse.Namespace`, by line and name."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = node.args
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
            annotation = ast.unparse(argument.annotation) if argument.annotation else ''
            # Word-bounded: sophios has its own `Namespaces` type for workflow
            # namespaces, and a substring test flags every compiler function.
            if re.search(r'\bNamespace\b', annotation):
                found.append((node.lineno, node.name))
    return found


@pytest.mark.fast
@pytest.mark.parametrize('path', _python_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_library_function_accepts_a_namespace(path: Path) -> None:
    """A `Namespace` is a CLI type, so only the CLI passes one.

    The CLI's own modules may hold what they parsed — `main` reads paths and
    flags out of it, and packing those into a five-parameter signature would
    be less readable, not more. What must not happen is the type escaping into
    the rest of the package, which is where "args sloshing everywhere" starts.
    The one function inside `main.py` that had no business taking one is
    pinned separately below.

    Deliberately permitted, because neither takes sophios configuration as a
    Namespace: `cwl_subinterpreter` has its own console-script parser and
    *returns* one, and `_tool_builder_support` *constructs* one because
    cwltool's API asks for that shape.
    """
    if path in (PARSER_MODULE, CLI_ADAPTER):
        return  # the CLI itself; its internals may hold what it parsed

    accepting = _namespace_parameters(ast.parse(path.read_text(encoding='utf-8'), str(path)))
    assert not accepting, (
        f'{path.relative_to(REPO_ROOT)} takes an argparse.Namespace in {accepting}; '
        f'pass the settings it needs instead'
    )


@pytest.mark.fast
def test_the_compile_helper_takes_settings_not_arguments() -> None:
    """`_build_and_compile_workflow` is handed settings, never a Namespace.

    Pinned by name because the module-wide rule above cannot catch it: this
    function lives in `main.py`, where holding parsed arguments is legitimate.
    It is still the wrong thing *here* — the function's only use of `args` was
    `graph_dark_theme`, which already travels inside `graph_settings`, so
    taking a Namespace bought nothing and widened a CLI type's reach. That was
    the first fix attempted for the dropped-flags bug, and it made the design
    worse while making the symptom go away.
    """
    import inspect

    import sophios.main

    signature = inspect.signature(sophios.main._build_and_compile_workflow)
    annotations = [str(parameter.annotation) for parameter in signature.parameters.values()]
    assert not any(re.search(r'\bNamespace\b', annotation) for annotation in annotations), \
        f'_build_and_compile_workflow takes a Namespace: {annotations}'
    assert 'compiler_options' in signature.parameters
    assert 'graph_settings' in signature.parameters


@pytest.mark.fast
def test_the_namespace_scan_can_actually_fail() -> None:
    """The signature scan sees both spellings it claims to."""
    assert _namespace_parameters(ast.parse('def f(args: argparse.Namespace) -> None: ...'))
    assert _namespace_parameters(ast.parse('def f(*, args: Namespace | None = None) -> None: ...'))
    assert not _namespace_parameters(ast.parse('def f(options: CompilerOptions) -> None: ...'))


@pytest.mark.fast
def test_defaults_are_available_without_a_command_line() -> None:
    """A library caller can ask for defaults by name, and gets the documented ones.

    The other half of the split. The CLI property below covers what a user
    *chooses*; this covers what a caller gets when nobody chooses — which is
    the path the Python API, the schema generator and the subinterpreter all
    take, and therefore the one the corpus workflows compile through. Deleting
    it as "subsumed" was wrong: the property never names this function.
    """
    options, graph, tags = sophios.cli.default_compilation_settings()
    assert options['allow_raw_cwl'] is False
    assert graph['graph_dark_theme'] is False
    assert tags['yaml'] == ''


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

    def __init__(self, settings: tuple[Any, Any, Any]) -> None:
        super().__init__('settings captured')
        self.settings = settings


@pytest.fixture(name='settings_from_cli')
def _settings_from_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., tuple[Any, Any, Any]]:
    """Run the real CLI and return the three settings dicts the compiler got.

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
    def run(*flags: str) -> tuple[Any, Any, Any]:
        workflow = tmp_path / 'probe.wic'
        workflow.write_text(yaml.safe_dump(PROBE_WORKFLOW), encoding='utf-8')

        def capture(_tree: Any, compiler_options: Any, graph_settings: Any,
                    yaml_tag_paths: Any, *_args: Any, **_kwargs: Any) -> None:
            raise _Delivered((compiler_options, graph_settings, yaml_tag_paths))

        monkeypatch.setattr(sophios.compiler, 'compile_workflow', capture)
        monkeypatch.setattr(sys, 'argv', ['sophios', '--yaml', str(workflow), *flags])
        try:
            sophios.main._main()
        except _Delivered as delivered:
            return delivered.settings
        raise AssertionError('the compiler was never reached')
    return run


def _settings_fields() -> list[tuple[int, str, type]]:
    """Every field of the three settings types, with which dict it belongs to.

    Derived from the `TypedDict`s rather than listed here, so a setting added
    later is covered the day it is added — the failure mode being guarded is
    precisely that someone adds a flag and nobody notices it is not delivered.
    """
    return [(index, name, annotation)
            for index, settings_type in enumerate((CompilerOptions, GraphSettings, YamlTagPaths))
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


#: `yaml` is the workflow path, supplied by the fixture, and `homedir` and
#: `cachedir` name real directories the run reads from — pointing them at
#: sentinels breaks the run rather than testing delivery. `yaml` is covered by
#: its own case below; the other two share the code path it exercises.
UNDELIVERABLE_BY_SENTINEL: Final = frozenset({'yaml', 'homedir', 'cachedir'})

_DELIVERABLE: Final = [field for field in _settings_fields()
                       if field[1] not in UNDELIVERABLE_BY_SENTINEL]


@pytest.mark.fast
@pytest.mark.parametrize('index,name,annotation', _DELIVERABLE,
                         ids=[name for _index, name, _annotation in _DELIVERABLE])
def test_every_setting_reaches_the_compiler(settings_from_cli: Callable[..., tuple[Any, Any, Any]],
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


@pytest.mark.fast
def test_the_workflow_path_reaches_the_compiler(settings_from_cli: Callable[..., tuple[Any, Any, Any]]) -> None:
    """`yaml_tag_paths['yaml']` is the workflow being compiled.

    Separate because its value is the path the fixture wrote, not a sentinel.
    It is the field that differed between `main` and every other caller, and
    the one a `cwl_subinterpreter` step reads.
    """
    _options, _graph, tag_paths = settings_from_cli()
    assert tag_paths['yaml'].endswith('probe.wic')
