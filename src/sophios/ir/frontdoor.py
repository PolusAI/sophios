"""Build a Resolve snapshot from the files a user wrote.

Reads the bytes directly and parses each reachable file exactly once: the
root's own text is the source, and each reachable workflow is registered as
the parse of its own file, so every span is a position in the file the user
edited. Nothing here serialises YAML.
"""
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from ..wic_types import StepId, Tool, Tools
from .resolve import RegistrySnapshot, generated_process_id, step_sidecar


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
                       tools: Tools) -> SourceBundle:
    """Bundle a root nobody wrote, plus every file its steps reach.

    For a caller that constructs its own root -- the runtime adapter builds a
    one-step workflow around a `.wic` it was handed. That root has no file and
    no spans worth keeping, but the documents it reaches are ordinary files,
    and they keep theirs.
    """
    workflows: dict[tuple[str, str], ParseResult] = {}
    generated: Tools = {}
    pins: list[str] = []
    parsed = _visit(source, name, None, yml_paths, Path('.'), workflows, generated, {}, pins)
    return SourceBundle(parsed, name,
                        RegistrySnapshot.from_tools({**tools, **generated},
                                                    workflows=workflows),
                        tuple(pins))


def bundle_from_disk(yml_path: Path,
                     yml_paths: dict[str, dict[str, Path]],
                     tools: Tools) -> SourceBundle:
    """Read ``yml_path`` and everything it reaches into an immutable snapshot.

    The parser is the only gate a file passes as it is read: whether it is
    well-formed is syntax, reported with positions. Whether its steps exist
    here, and whether their ports line up, is for the passes that follow.
    """
    workflows: dict[tuple[str, str], ParseResult] = {}
    generated: Tools = {}
    pins: list[str] = []
    parsed = _visit(yml_path.read_text(encoding='utf-8'), yml_path.stem, yml_path.resolve(), yml_paths, yml_path.parent,
                    workflows, generated, {}, pins)
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
           pins: list[str]) -> ParseResult:
    """Parse one file's text and register every workflow and generated tool it reaches.

    The parse is recorded under ``path`` before anything it reaches is read,
    so a file reached again -- a cycle, or a second namespace -- reuses it.
    """
    parsed = parse(source, f'{stem}.wic')
    if path is not None:
        read[path] = parsed
    document = parsed.document
    if document is not None:
        _collect_pins(document, pins)
        _reach(document, yml_paths, script_dir, workflows, generated, read, pins)
    return parsed


def _collect_pins(document: Document, pins: list[str]) -> None:
    """Append this document's pin and each inline implementation body's pin.

    A body is already parsed; this reads the pin the parser holds. `_reach`
    follows the body's steps, and selection compiles the body, so a pin that
    lives only there still decides the version.
    """
    _append_pin(document, pins)
    for _name, body in (document.sidecar.implementations if document.sidecar else ()):
        _collect_pins(body, pins)


def _append_pin(document: Document, pins: list[str]) -> None:
    pinned = dict(document.sidecar.entries).get('lang_version') if document.sidecar else None
    if pinned is not None:
        pins.append(pinned if isinstance(pinned, str) else str(pinned))


# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals
def _reach(document: Document,
           yml_paths: dict[str, dict[str, Path]],
           script_dir: Path,
           workflows: dict[tuple[str, str], ParseResult],
           generated: Tools,
           read: dict[Path, ParseResult],
           pins: list[str]) -> None:
    """Follow every workflow and generated tool one document's steps reach,
    including its inline implementation bodies.
    """
    counts = Counter(step.id for step in document.steps)
    for index, step in enumerate(document.steps, start=1):
        namespace = _namespace(step_sidecar(document.sidecar, index, step.id, counts[step.id]))
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
                yml_paths, script_dir, workflows, generated, read, pins)
    for _name, body in (document.sidecar.implementations if document.sidecar else ()):
        _reach(body, yml_paths, script_dir, workflows, generated, read, pins)


def _namespace(sidecar: WicSidecar | None) -> str:
    """The plugin namespace a ``wic:`` block declares, defaulting to global."""
    if sidecar is None:
        return 'global'
    return str(dict(sidecar.entries).get('namespace', 'global'))


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
