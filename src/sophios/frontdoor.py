"""Build a Resolve snapshot from the files a user wrote.

Reads the bytes directly and parses each reachable file exactly once: the
root's own text is the source, and each reachable workflow is registered as
the parse of its own file, so every span is a position in the file the user
edited. Nothing here serialises YAML.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .ir.resolve import (RegistrySnapshot, inline_run_name, namespace_of, path_from_root, run_path_name, step_sidecar,
                         version_pins)
from .lang import CWL_VERSION, Document, ParseResult, Step, ToolDocument, UnresolvedName, Value, parse
from .wic_types import StepId, Tool, Tools


#: What the door sets over an inline `run:` body: it is read as the one version Sophios writes, whatever it declares.
_STAMP: Final = (('cwlVersion', CWL_VERSION),)


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
    root_directory: Path
    workflows: dict[tuple[str, str], ParseResult] = field(default_factory=dict)
    directories: dict[tuple[str, str], Path] = field(default_factory=dict)
    generated: Tools = field(default_factory=dict)
    #: Keyed by path: a cycle is a cycle regardless of namespace.
    read: dict[Path, ParseResult] = field(default_factory=dict)
    run_names: dict[tuple[Path, str], str] = field(default_factory=dict)
    pins: list[str] = field(default_factory=list)
    #: The registry's index: the tools discovery found, which no file this reading reaches replaces.
    tools: Tools = field(default_factory=dict)

    def bundle(self, parsed: ParseResult, name: str) -> SourceBundle:
        """Freeze what was read into the bundle.

        A root that is a tool is the workflow of one step that runs it, as an author would write it: `run:` names the
        tool, and each port of the tool is an input of the workflow of its own name, so the job of the workflow is the
        job of the tool.
        """
        if (tool := parsed.tool) is not None and not parsed.diagnostics.has_errors:
            self.generated[StepId(name, 'global')] = _written_tool(self.root_directory / f'{name}.wic', tool)
            step = Step(name, tuple((port.name, UnresolvedName(port.name, port.span)) for port in tool.inputs),
                        interpreted=(('run', f'{name}.wic'),), span=tool.span)
            inputs: Value = {port.name: {key: value for key, value in port.fields if key in ('type', 'default')}
                             for port in tool.inputs}
            parsed = ParseResult(Document((step,), passthrough=(('inputs', inputs),), span=tool.span, port_spans=tuple(
                ('inputs', port.name, port.span) for port in tool.inputs if port.span)), parsed.diagnostics)
        registry = RegistrySnapshot.from_tools(
            {**self.tools, **self.generated}, workflows=self.workflows, directories=self.directories,
            run_names=self.run_names, root_directory=self.root_directory, stamp=_STAMP)
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
    reading = _Reading(yml_paths, here, tools=tools)
    return reading.bundle(_visit(source, f'{name}.wic', None, reading), name)


def bundle_from_disk(yml_path: Path,
                     yml_paths: dict[str, dict[str, Path]],
                     tools: Tools) -> SourceBundle:
    """Read ``yml_path`` and everything it reaches into an immutable snapshot.

    The parser is the only gate a file passes as it is read: whether it is
    well-formed is syntax, reported with positions. Whether its steps exist
    here, and whether their ports line up, is for the passes that follow.
    """
    reading = _Reading(yml_paths, yml_path.resolve().parent, tools=tools)
    parsed = _visit(yml_path.read_text(encoding='utf-8'), yml_path.name, yml_path.resolve(), reading)
    return reading.bundle(parsed, yml_path.stem)


def _visit(source: str, file: str, path: Path | None, reading: _Reading) -> ParseResult:
    """Parse one file's text and register every workflow and generated tool it reaches.

    The parse is recorded under ``path`` before anything it reaches is read,
    so a file reached again -- a cycle, or a second namespace -- reuses it.
    """
    parsed = parse(source, file)
    if path is not None:
        reading.read[path] = parsed
    document = parsed.document
    if document is not None:
        _collect_pins(document, reading.pins)
        _reach(document, path.parent if path is not None else reading.root_directory, reading)
    return parsed


def _collect_pins(document: Document, pins: list[str]) -> None:
    """Append this document's pin and each inline implementation body's pin.

    A body is already parsed; this reads the pin the parser holds. `_reach`
    follows the body's steps, and selection compiles the body, so a pin that
    lives only there still decides the version.
    """
    pins.extend(version_pins(document))
    for _name, body in (document.sidecar.implementations if document.sidecar else ()):
        _collect_pins(body, pins)


def _reach(document: Document, document_dir: Path, reading: _Reading) -> None:
    """Follow every workflow and generated tool one document's steps reach,
    including its inline implementation bodies.

    ``document_dir`` is where this document is, which is what a ``run:`` path
    is relative to.
    """
    counts = Counter(step.id for step in document.steps)
    for index, step in enumerate(document.steps, start=1):
        namespace = namespace_of(step_sidecar(document.sidecar, index, step.id, counts[step.id]))
        if _register_run(step, namespace, document_dir, reading):
            continue
        if step.id.endswith('.wic'):
            # Left unregistered rather than raising: Resolve reports it as
            # absent, a diagnostic the reader can act on.
            child_path = reading.yml_paths.get(namespace, {}).get(Path(step.id).stem)
            if child_path is None:
                continue
            # Keyed by the call site's namespace, matching how `_resolve_process` builds its `RegistryKey`.
            _register_document((namespace, child_path.stem), child_path.resolve(), reading)
    for _name, body in (document.sidecar.implementations if document.sidecar else ()):
        _reach(body, document_dir, reading)


def _register_document(key: tuple[str, str], path: Path, reading: _Reading) -> None:
    """Register the document at ``path`` under ``key``, parsing the file only if nothing has: a workflow, or a tool
    written in `.wic`, which is a tool of that name unless the index holds one: the index names each tool once.

    A file called under two namespaces still gets both registry entries.
    """
    tool_key = StepId(key[1], key[0])
    if key in reading.workflows:
        return
    parsed = reading.read[path] if path in reading.read else _visit(
        path.read_text(encoding='utf-8'), path.name, path, reading)
    if parsed.tool is not None:
        if tool_key not in reading.tools:
            reading.generated[tool_key] = _written_tool(path, parsed.tool, parsed.diagnostics.has_errors)
        return
    reading.workflows[key] = parsed
    reading.directories[key] = path.parent


def _written_tool(path: Path, tool: ToolDocument, refused: bool = False) -> Tool:
    """The tool the `.wic` at ``path`` says; a tool the parse refused is left to be read, and refused, when a step runs
    it."""
    return Tool(str(path)) if refused else Tool(str(path), written=tool)


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
        name = inline_run_name(step.id, run, path_from_root(document_dir, reading.root_directory), _STAMP)
        # Written in the document, so its relative references are read beside it, as the step's own file would be.
        beside = document_dir / Path(step.id).stem
        if step.tool is not None:  # a tool written in `.wic`
            reading.generated[StepId(name, namespace)] = _written_tool(Path(f'{beside}.wic'), step.tool)
        else:
            reading.generated[StepId(name, namespace)] = Tool(
                f'{beside}.cwl', {**deepcopy(run), **dict(_STAMP)})
        return True
    # Any other string names a registry entry, not a file.
    if not isinstance(run, str) or not run.endswith(('.cwl', '.wic')):
        return False
    target = (document_dir / run).resolve()
    if not target.is_file():
        return False
    name = run_path_name(run, path_from_root(target, reading.root_directory))
    reading.run_names[(document_dir, run)] = name
    if run.endswith('.cwl'):
        reading.generated[StepId(name, namespace)] = Tool(str(target), from_file=True)
    else:
        _register_document((namespace, name), target, reading)
    return True
