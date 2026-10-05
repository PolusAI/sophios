"""Build a Resolve snapshot from the files a user wrote.

Reads the bytes directly and parses each reachable file exactly once: the
root's own text is the source, and each reachable workflow is registered as
the parse of its own file, so every span is a position in the file the user
edited. Nothing here serialises YAML.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

import yaml

from ..lang import (
    CWL_VERSION,
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
from ..wic_types import StepId, Tool, Tools
from .resolve import (RegistrySnapshot, generated_process_id, inline_run_name, is_run_path, run_path_name,
                      step_sidecar)


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


@dataclass(slots=True)
# pylint: disable-next=too-many-instance-attributes
class _Reading:
    """What one bundle accumulates as it follows the steps from its root."""

    yml_paths: dict[str, dict[str, Path]]
    #: Where ``python_script`` files are: the root's directory.
    script_dir: Path
    root_directory: Path
    workflows: dict[tuple[str, str], ParseResult] = field(default_factory=dict)
    directories: dict[tuple[str, str], Path] = field(default_factory=dict)
    generated: Tools = field(default_factory=dict)
    #: Keyed by path: a cycle is a cycle regardless of namespace.
    read: dict[Path, ParseResult] = field(default_factory=dict)
    run_names: dict[tuple[Path, str], str] = field(default_factory=dict)
    pins: list[str] = field(default_factory=list)

    def bundle(self, parsed: ParseResult, name: str, tools: Tools) -> SourceBundle:
        """Freeze what was read into the bundle."""
        registry = RegistrySnapshot.from_tools(
            {**tools, **self.generated}, workflows=self.workflows, directories=self.directories,
            run_names=self.run_names, root_directory=self.root_directory)
        return SourceBundle(parsed, name, registry, tuple(self.pins))


def bundle_from_source(source: str, name: str,
                       yml_paths: dict[str, dict[str, Path]],
                       tools: Tools) -> SourceBundle:
    """Bundle a root nobody wrote, plus every file its steps reach.

    For a caller that constructs its own root -- the runtime adapter builds a
    one-step workflow around a `.wic` it was handed. That root has no file and
    no spans worth keeping, but the documents it reaches are ordinary files,
    and they keep theirs.
    """
    here = Path('.').resolve()
    reading = _Reading(yml_paths, here, here)
    return reading.bundle(_visit(source, name, None, reading), name, tools)


def bundle_from_disk(yml_path: Path,
                     yml_paths: dict[str, dict[str, Path]],
                     tools: Tools) -> SourceBundle:
    """Read ``yml_path`` and everything it reaches into an immutable snapshot.

    The parser is the only gate a file passes as it is read: whether it is
    well-formed is syntax, reported with positions. Whether its steps exist
    here, and whether their ports line up, is for the passes that follow.
    """
    reading = _Reading(yml_paths, yml_path.parent, yml_path.resolve().parent)
    parsed = _visit(yml_path.read_text(encoding='utf-8'), yml_path.stem, yml_path.resolve(), reading)
    return reading.bundle(parsed, yml_path.stem, tools)


def _visit(source: str, stem: str, path: Path | None, reading: _Reading) -> ParseResult:
    """Parse one file's text and register every workflow and generated tool it reaches.

    The parse is recorded under ``path`` before anything it reaches is read,
    so a file reached again -- a cycle, or a second namespace -- reuses it.
    """
    parsed = parse(source, f'{stem}.wic')
    if path is not None:
        reading.read[path] = parsed
    document = parsed.document
    if document is not None:
        _collect_pins(document, reading.pins)
        _reach(document, path.parent if path is not None else reading.script_dir, reading)
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


def _reach(document: Document, document_dir: Path, reading: _Reading) -> None:
    """Follow every workflow and generated tool one document's steps reach,
    including its inline implementation bodies.

    ``document_dir`` is where this document is, which is what a ``run:`` path
    is relative to.
    """
    counts = Counter(step.id for step in document.steps)
    for index, step in enumerate(document.steps, start=1):
        namespace = _namespace(step_sidecar(document.sidecar, index, step.id, counts[step.id]))
        if _register_run(step, namespace, document_dir, reading):
            continue
        if step.id == 'python_script':
            reading.generated[StepId(generated_process_id(step), namespace)] = \
                _generated_tool(step, reading.script_dir)
        elif step.id.endswith('.wic'):
            # Left unregistered rather than raising: Resolve reports it as
            # absent, a diagnostic the reader can act on.
            child_path = reading.yml_paths.get(namespace, {}).get(Path(step.id).stem)
            if child_path is None:
                continue
            # Keyed by the call site's namespace, matching how `_resolve_process` builds its `RegistryKey`.
            _register_workflow((namespace, child_path.stem), child_path.resolve(), reading)
    for _name, body in (document.sidecar.implementations if document.sidecar else ()):
        _reach(body, document_dir, reading)


def _register_workflow(key: tuple[str, str], path: Path, reading: _Reading) -> None:
    """Register the workflow at ``path`` under ``key``, parsing the file only if nothing has.

    A file called under two namespaces still gets both registry entries.
    """
    if key in reading.workflows:
        return
    reading.workflows[key] = reading.read[path] if path in reading.read else _visit(
        path.read_text(encoding='utf-8'), path.stem, path, reading)
    reading.directories[key] = path.parent


def _register_run(step: Step, namespace: str, document_dir: Path, reading: _Reading) -> bool:
    """Register what a step's ``run:`` names, when it names something here.

    An inline mapping is a tool keyed by the step's id, written in
    ``CWL_VERSION`` whatever ``cwlVersion`` it declares. A ``.cwl`` or ``.wic``
    path that exists relative to the document is read from there and keyed by
    the file it is, shadowing a registry entry of that stem for this
    compilation. A path that does not exist here is left for Resolve, which
    looks the stem up in the registry and reports it as absent if it is
    nowhere.

    Returns:
        bool: Whether ``run`` was registered here.
    """
    run = dict(step.interpreted).get('run')
    if isinstance(run, dict):
        name = inline_run_name(step)
        assert name is not None
        body = desugar_into_canonical_normal_form({**deepcopy(run), 'cwlVersion': CWL_VERSION})
        reading.generated[StepId(name, namespace)] = Tool(f'{name}.cwl', body)
        return True
    if not isinstance(run, str) or not is_run_path(run):
        return False
    target = (document_dir / run).resolve()
    if not target.is_file():
        return False
    try:
        identity = os.path.relpath(target, reading.root_directory)
    except ValueError:  # Windows: the file and the root directory are on different drives
        identity = target.as_posix()
    name = run_path_name(run, identity)
    reading.run_names[(document_dir, run)] = name
    if run.endswith('.cwl'):
        with open(target, mode='r', encoding='utf-8') as handle:
            reading.generated[StepId(name, namespace)] = Tool(
                str(target), desugar_into_canonical_normal_form(yaml.safe_load(handle.read())))
    else:
        _register_workflow((namespace, name), target, reading)
    return True


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
