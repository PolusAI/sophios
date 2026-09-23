"""Build a Resolve snapshot from the files a user wrote.

The legacy path assembles a YAML tree by splicing every child ``.wic`` into
its parent, then dumps each subtree back to text so Parse has something to
read.  Every span Parse produces is then a position in that dump, not in the
file the user edited.  This module reads the bytes instead: the root's own
text is the source, and each reachable workflow is registered verbatim.

Nothing here serialises YAML.  ``yaml`` is parsed only to discover structure;
the text that reaches the registry is always what was on disk.
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
    """A root workflow's own text plus every workflow reachable from it."""

    source: str
    name: str
    registry: RegistrySnapshot
    #: Every `wic: lang_version:` the reachable documents pin. Collected
    #: while reading, because the version must be chosen before Parse runs
    #: and only the door has seen every file by then.
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
    workflows: dict[tuple[str, str], str] = {}
    generated: Tools = {}
    pins: list[str] = []
    _visit(source, name, yml_paths, Path('.'), workflows, generated, set(), pins, validator)
    return SourceBundle(source, name,
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
    source = yml_path.read_text(encoding='utf-8')
    workflows: dict[tuple[str, str], str] = {}
    generated: Tools = {}
    pins: list[str] = []
    _visit(source, yml_path.stem, yml_paths, yml_path.parent,
           workflows, generated, {yml_path.resolve()}, pins, validator)
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
           pins: list[str],
           validator: Draft202012Validator | None) -> Document | None:
    """Register every workflow and generated tool one file's text reaches."""
    _validate(source, stem, validator)
    document = parse(source, f'{stem}.wic').document
    if document is None:
        return None
    pinned = dict(document.sidecar.entries).get('lang_version') if document.sidecar else None
    if pinned is not None:
        pins.append(pinned if isinstance(pinned, str) else str(pinned))
    _reach(document, yml_paths, script_dir, workflows, generated, seen, pins, validator)
    return document


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def _reach(document: Document,
           yml_paths: dict[str, dict[str, Path]],
           script_dir: Path,
           workflows: dict[tuple[str, str], str],
           generated: Tools,
           seen: set[Path],
           pins: list[str],
           validator: Draft202012Validator | None) -> None:
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
            # An undiscovered workflow is left unregistered rather than raising:
            # Resolve reports it as absent from the registry, which is a
            # diagnostic the reader can act on, not a KeyError from a loader.
            child_path = yml_paths.get(namespace, {}).get(Path(step.id).stem)
            if child_path is None:
                continue
            # Keyed by the namespace the *call site* declares, which is the
            # one `_resolve_process` builds its `RegistryKey` from -- and the
            # same one this loop just used to find the file. A producer that
            # keys differently from the consumer files entries nothing reads.
            key = (namespace, child_path.stem)
            if key in workflows:
                continue
            child_source = child_path.read_text(encoding='utf-8')
            workflows[key] = child_source
            # The recursion is what `seen` is for, and it is keyed by path
            # because a cycle is a cycle whichever namespace reaches it. The
            # registry is keyed by namespace, so one file called under two
            # namespaces needs both entries though it is read and walked once.
            if child_path.resolve() in seen:
                continue
            seen.add(child_path.resolve())
            _visit(child_source, child_path.stem, yml_paths, script_dir,
                   workflows, generated, seen, pins, validator)
    for _name, body in (document.sidecar.implementations if document.sidecar else ()):
        _reach(body, yml_paths, script_dir, workflows, generated, seen, pins, validator)


def _validate(source: str, stem: str, validator: Draft202012Validator | None) -> None:
    """Check one document against the generated schema before anything reads it.

    The schema closes the `wic:` block, so a key the language does not have is
    refused here rather than carried through as opaque data. Validated in the
    canonical normal form, as the schema is written against it.

    The traceback goes to a file: a jsonschema failure prints a wall of text
    that tells a reader nothing about their workflow.
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
