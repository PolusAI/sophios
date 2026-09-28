"""Build a Resolve snapshot from the files a user wrote.

Reads the bytes directly and parses each reachable file exactly once: the
root's own text is the source, and each reachable workflow is registered as
the parse of its own file, so every span is a position in the file the user
edited. Nothing here serialises YAML.
"""
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from ..lang.diagnostics import SophiosError
from ..lang.error_codes import SophiosErrorCode
from ..lang import (
    Document,
    EdgeRef,
    InlineLiteral,
    InputValue,
    ParseResult,
    RawCwlRef,
    Step,
    UnresolvedName,
    WicSidecar,
    parse,
)
from ..python_cwl_adapter import generate_CWL_CommandLineTool, get_module
from ..utils_cwl import desugar_into_canonical_normal_form
from ..utils_yaml import wic_loader
from ..wic_types import StepId, Tool, Tools
from .resolve import RegistrySnapshot, generated_process_id


@dataclass(frozen=True, slots=True)
class SourceBundle:
    """A parsed root workflow plus every workflow reachable from it.

    What the compiler's one door takes. A file's bundle carries the spans of
    the text its author wrote; one the Python API builds carries none.
    """

    parsed: ParseResult
    name: str
    registry: RegistrySnapshot
    #: Every `wic: lang_version:` the reachable documents pin. Collected
    #: while reading, because the version must be chosen before Resolve runs
    #: and only the door has seen every document by then.
    lang_version_pins: tuple[str, ...] = ()


def bundle_from_source(source: str, name: str,
                       yml_paths: dict[str, dict[str, Path]],
                       tools: Tools,
                       validator: Draft202012Validator | None = None) -> SourceBundle:
    """Bundle a root nobody wrote, plus every file its steps reach.

    For a caller that constructs its own root -- the runtime adapter builds a
    one-step workflow around a `.wic` it was handed. That root has no file and
    no spans worth keeping, but the documents it reaches are ordinary files,
    and they keep theirs.
    """
    workflows: dict[tuple[str, str], ParseResult] = {}
    generated: Tools = {}
    pins: list[str] = []
    parsed = _visit(source, name, None, yml_paths, Path('.'), workflows, generated, {}, pins,
                    validator)
    return SourceBundle(parsed, name,
                        RegistrySnapshot.from_tools({**tools, **generated},
                                                    workflows=workflows),
                        tuple(pins))


def bundle_from_disk(yml_path: Path,
                     yml_paths: dict[str, dict[str, Path]],
                     tools: Tools,
                     validator: Draft202012Validator | None = None) -> SourceBundle:
    """Read ``yml_path`` and everything it reaches into an immutable snapshot.

    Each file is validated against ``validator`` as it is read, when one is
    supplied -- the same gate the file loader applied, at the same point: before
    anything downstream sees the document.
    """
    workflows: dict[tuple[str, str], ParseResult] = {}
    generated: Tools = {}
    pins: list[str] = []
    parsed = _visit(yml_path.read_text(encoding='utf-8'), yml_path.stem, yml_path.resolve(), yml_paths, yml_path.parent,
                    workflows, generated, {}, pins, validator)
    return SourceBundle(parsed, yml_path.stem,
                        RegistrySnapshot.from_tools({**tools, **generated},
                                                    workflows=workflows),
                        tuple(pins))


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def _visit(source: str, stem: str, path: Path | None,
           yml_paths: dict[str, dict[str, Path]],
           script_dir: Path,
           workflows: dict[tuple[str, str], ParseResult],
           generated: Tools,
           read: dict[Path, ParseResult],
           pins: list[str],
           validator: Draft202012Validator | None) -> ParseResult:
    """Parse one file's text and register every workflow and generated tool it reaches.

    The parse is recorded under ``path`` before anything it reaches is read,
    so a file reached again -- a cycle, or a second namespace -- reuses it.
    """
    _validate(source, stem, validator)
    parsed = parse(source, f'{stem}.wic')
    if path is not None:
        read[path] = parsed
    document = parsed.document
    if document is not None:
        pinned = dict(document.sidecar.entries).get('lang_version') if document.sidecar else None
        if pinned is not None:
            pins.append(pinned if isinstance(pinned, str) else str(pinned))
        _reach(document, yml_paths, script_dir, workflows, generated, read, pins, validator)
    return parsed


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def _reach(document: Document,
           yml_paths: dict[str, dict[str, Path]],
           script_dir: Path,
           workflows: dict[tuple[str, str], ParseResult],
           generated: Tools,
           read: dict[Path, ParseResult],
           pins: list[str],
           validator: Draft202012Validator | None) -> None:
    """Follow every workflow and generated tool one document's steps reach,
    including its inline implementation bodies.
    """
    for index, step in enumerate(document.steps, start=1):
        namespace = _namespace(_step_sidecar(document.sidecar, index, step.id))
        if step.id == 'python_script':
            generated[StepId(generated_process_id(step), namespace)] = \
                _generated_tool(step, script_dir)
        elif step.id.endswith('.wic'):
            # Left unregistered rather than raising: Resolve reports it as
            # absent, a diagnostic the reader can act on.
            child_path = yml_paths.get(namespace, {}).get(Path(step.id).stem)
            if child_path is None:
                continue
            # Keyed by the call site's namespace, matching how `_resolve_process` builds its `RegistryKey`.
            key = (namespace, child_path.stem)
            if key in workflows:
                continue
            # `read` is keyed by path (a cycle is a cycle regardless of namespace);
            # a file called under two namespaces still gets both registry entries.
            resolved = child_path.resolve()
            workflows[key] = read[resolved] if resolved in read else _visit(
                child_path.read_text(encoding='utf-8'), child_path.stem, resolved,
                yml_paths, script_dir, workflows, generated, read, pins, validator)
    for _name, body in (document.sidecar.implementations if document.sidecar else ()):
        _reach(body, yml_paths, script_dir, workflows, generated, read, pins, validator)


def _validate(source: str, stem: str, validator: Draft202012Validator | None) -> None:
    """Check one document, in canonical normal form, against the schema before
    anything reads it; the traceback goes to a file, not the reader's screen.
    """
    if validator is None:
        return
    try:
        validator.validate(desugar_into_canonical_normal_form(
            yaml.load(source, Loader=wic_loader())))
    except Exception as error:  # pylint: disable=broad-exception-caught
        # Deliberately broad: any failure while validating, not only a
        # ValidationError, is reported to the reader rather than raised raw.
        report = Path(f'validation_{stem}.txt')
        with report.open(mode='w', encoding='utf-8') as handle:
            traceback.print_exception(type(error), value=error, tb=None, file=handle)
        raise SophiosError.error(
            SophiosErrorCode.SUBWORKFLOW_INVALID,
            f'Failed to validate {stem}',
            f'See {report} for detailed technical information.') from error


def _namespace(sidecar: WicSidecar | None) -> str:
    """The plugin namespace a ``wic:`` block declares, defaulting to global."""
    if sidecar is None:
        return 'global'
    return str(dict(sidecar.entries).get('namespace', 'global'))


def _step_sidecar(sidecar: WicSidecar | None, index: int, name: str) -> WicSidecar | None:
    if sidecar is None:
        return None
    return next((child for key, child in sidecar.steps
                 if key.index == index and key.name == name), None)


def _generated_tool(step: Step, script_dir: Path) -> Tool:
    """Generate a Python step's CommandLineTool without writing it anywhere.

    The run path names the tool it would have been written as, so downstream
    ``run:`` fields read as before; no such file is created.
    """
    args: dict[str, Any] = dict(step.inputs)
    script = script_dir / _text(args.pop('script', None))
    docker_pull = _text(args.pop('dockerPull', None))
    module = get_module(script.name[:-3], script, args)
    cwl = generate_CWL_CommandLineTool(module.inputs, module.outputs, docker_pull)
    return Tool(f'autogenerated/{generated_process_id(step)}.cwl', cwl)


def _text(value: InputValue | None) -> str:
    match value:
        case InlineLiteral(value=literal):
            return str(literal)
        case EdgeRef(name=name) | UnresolvedName(name=name):
            return name
        case RawCwlRef(expression=expression):
            return expression
        case None:
            return ''
