"""Build a Resolve snapshot from the files a user wrote.

The legacy path assembles a YAML tree by splicing every child ``.wic`` into
its parent, then dumps each subtree back to text so Parse has something to
read.  Every span Parse produces is then a position in that dump, not in the
file the user edited.  This module reads the bytes instead: the root's own
text is the source, and each reachable workflow is registered verbatim.

Nothing here serialises YAML.  ``yaml`` is parsed only to discover structure;
the text that reaches the registry is always what was on disk.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..lang import (
    Document,
    EdgeRef,
    InlineLiteral,
    InputValue,
    RawCwlRef,
    Step,
    UnresolvedName,
    WicSidecar,
    parse,
)
from ..python_cwl_adapter import generate_CWL_CommandLineTool, get_module
from ..wic_types import StepId, Tool, Tools
from .resolve import RegistrySnapshot, generated_process_id


@dataclass(frozen=True, slots=True)
class SourceBundle:
    """A root workflow's own text plus every workflow reachable from it."""

    source: str
    name: str
    registry: RegistrySnapshot
    #: Every `wic: lang_version:` the reachable documents pin. Collected
    #: while reading, because the version must be chosen before Parse runs
    #: and only the door has seen every file by then.
    lang_version_pins: tuple[str, ...] = ()


def bundle_from_disk(yml_path: Path,
                     yml_paths: dict[str, dict[str, Path]],
                     tools: Tools) -> SourceBundle:
    """Read ``yml_path`` and everything it reaches into an immutable snapshot."""
    source = yml_path.read_text(encoding='utf-8')
    workflows: dict[tuple[str, str], str] = {}
    generated: Tools = {}
    pins: list[str] = []
    _visit(source, yml_path.stem, yml_paths, yml_path.parent,
           workflows, generated, {yml_path.resolve()}, pins)
    return SourceBundle(source, yml_path.stem,
                        RegistrySnapshot.from_tools({**tools, **generated},
                                                    workflows=workflows),
                        tuple(pins))


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def _visit(source: str, stem: str,
           yml_paths: dict[str, dict[str, Path]],
           script_dir: Path,
           workflows: dict[tuple[str, str], str],
           generated: Tools,
           seen: set[Path],
           pins: list[str]) -> Document | None:
    """Register every workflow and generated tool one file's text reaches."""
    document = parse(source, f'{stem}.wic').document
    if document is None:
        return None
    pinned = dict(document.sidecar.entries).get('lang_version') if document.sidecar else None
    if pinned is not None:
        pins.append(pinned if isinstance(pinned, str) else str(pinned))
    _reach(document, yml_paths, script_dir, workflows, generated, seen, pins)
    return document


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def _reach(document: Document,
           yml_paths: dict[str, dict[str, Path]],
           script_dir: Path,
           workflows: dict[tuple[str, str], str],
           generated: Tools,
           seen: set[Path],
           pins: list[str]) -> None:
    """Follow every workflow and generated tool one document's steps reach.

    Its implementation bodies are followed too: each is a document written
    inline in this file, and the steps inside one reach further files exactly
    as this document's own steps do.
    """
    for index, step in enumerate(document.steps, start=1):
        namespace = _namespace(_step_sidecar(document.sidecar, index, step.id))
        if step.id == 'python_script':
            generated[StepId(generated_process_id(step), namespace)] = \
                _generated_tool(step, script_dir)
        elif step.id.endswith('.wic'):
            child_path = yml_paths[namespace][Path(step.id).stem]
            if child_path.resolve() in seen:
                continue
            seen.add(child_path.resolve())
            child_source = child_path.read_text(encoding='utf-8')
            _visit(child_source, child_path.stem, yml_paths, script_dir,
                   workflows, generated, seen, pins)
            # Keyed by the namespace the *call site* declares, which is the
            # one `_resolve_process` builds its `RegistryKey` from -- and the
            # same one this loop just used to find the file. A producer that
            # keys differently from the consumer files entries nothing reads.
            workflows[(namespace, child_path.stem)] = child_source
    for _name, body in (document.sidecar.implementations if document.sidecar else ()):
        _reach(body, yml_paths, script_dir, workflows, generated, seen, pins)


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
