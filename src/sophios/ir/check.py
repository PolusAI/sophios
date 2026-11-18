"""Check: what a document must hold of its own nodes, whichever door made it, before anything is looked up.

The parser states what only text can say, the shape of each value, and a document the Python API builds never passes
it. The rules here are about the nodes themselves, so they are stated once, for every document Resolve reads, and
Resolve looks nothing up for a document this pass has not made a `CheckedDocument` of.
"""
from dataclasses import dataclass, field
from typing import Final

from ..lang.diagnostics import Diagnostics, Locator
from ..lang.error_codes import SophiosErrorCode
from ..lang.names import check_written_name
from ..lang.nodes import CwlRecord, Document, EdgeRef, Step

#: Held only here: what tells a `CheckedDocument` that `check` made it.
_CHECKED: Final = object()


@dataclass(frozen=True, slots=True)
class CheckedDocument:
    """A document that keeps every rule of its own nodes, which only `check` makes."""

    document: Document
    made_by: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.made_by is not _CHECKED:
            raise TypeError('only sophios.ir.check.check makes a CheckedDocument')


def check(document: Document, name: str, *, root: bool) -> tuple[CheckedDocument | None, Diagnostics]:
    """`document`, the workflow `name`, as a `CheckedDocument` when it keeps every rule of its own nodes, and each rule
    it breaks: a step has an id (wic007) that Sophios can write (wic045), as are the workflow's ports and the root's
    name; every name it binds or defines is written (wic027); an edge name has one producer (wic026); `scatter:` is an
    input name or a list of them (wic032); and a workflow output names one step/port (wic038)."""
    diagnostics = Diagnostics()
    defined: set[str] = set()
    for index, step in enumerate(document.steps, start=1):
        _check_step(step, index, defined, diagnostics)
    declared = dict(document.passthrough)
    for side in ('inputs', 'outputs'):
        ports = declared.get(side)
        for port, declaration in ports.items() if isinstance(ports, dict) else ():
            if (problem := check_written_name(str(port))) is not None:
                diagnostics.error(SophiosErrorCode.INVALID_NAME, f'workflow {side[:-1]} {str(port)!r} {problem}',
                                  document.span)
            if side == 'outputs' and isinstance(declaration, dict):
                rejected: list[str] = sorted(key for key in ('linkMerge', 'pickValue') if key in declaration)
                rejected += ['a list outputSource'] * isinstance(declaration.get('outputSource'), list)
                if rejected:
                    diagnostics.error(SophiosErrorCode.STEP_INPUT_RECORD, f"workflow output {str(port)!r} uses "
                                      f"{', '.join(rejected)}, which Sophios does not read; an output names one "
                                      'step/port', document.span)
    if root and (problem := check_written_name(name)) is not None:
        diagnostics.error(SophiosErrorCode.INVALID_NAME, f'workflow name {name!r} {problem}', document.span)
    return (None if diagnostics.has_errors else CheckedDocument(document, _CHECKED)), diagnostics


def _check_step(step: Step, index: int, defined: set[str], diagnostics: Diagnostics) -> None:
    """Report each rule `step`, the `index`th, breaks of its own nodes; `defined` holds the edge names steps before
    it define, and gets its own."""
    at = Locator(step=step.id, index=index)
    if not step.id:
        diagnostics.error(SophiosErrorCode.EMPTY_STEP_ID, 'a step needs an id', step.span)
    elif (problem := check_written_name(step.id)) is not None:
        diagnostics.error(SophiosErrorCode.INVALID_NAME, f'step id {step.id!r} {problem}', step.span, at)
    for port, value in step.inputs:
        if not port:
            diagnostics.error(SophiosErrorCode.EMPTY_NAME, f"step '{step.id}' binds an input with no name",
                              step.span, at)
        for reference in value.sources if isinstance(value, CwlRecord) else (value,):
            if isinstance(reference, EdgeRef) and not reference.name:
                diagnostics.error(SophiosErrorCode.EMPTY_NAME, f"'!*' on '{step.id}.{port}' names no edge",
                                  reference.span, Locator(step=step.id, index=index, port=port))
    for binding in step.outputs:
        if not binding.name:
            diagnostics.error(SophiosErrorCode.EMPTY_NAME, f"step '{step.id}' declares an out: entry with no name",
                              binding.span, at)
        if binding.edge_def is None:
            continue
        edge, where = binding.edge_def.name, Locator(step=step.id, index=index, port=binding.name)
        if not edge:
            diagnostics.error(SophiosErrorCode.EMPTY_NAME, f"'!&' on '{step.id}.{binding.name}' defines no edge",
                              binding.edge_def.span, where)
        elif edge in defined:
            diagnostics.error(SophiosErrorCode.DUPLICATE_EDGE_DEF, f"'&{edge}' is defined more than once. An edge "
                              'name identifies one producer.', binding.edge_def.span, where)
        defined.add(edge)
    _check_scatter(step, at, diagnostics)


def _check_scatter(step: Step, at: Locator, diagnostics: Diagnostics) -> None:
    """Report a `scatter:` of `step` that is neither an input name nor a list of them."""
    scatter = dict(step.interpreted).get('scatter')
    if scatter is not None and not isinstance(scatter, (str, list)):
        diagnostics.error(SophiosErrorCode.UNKNOWN_SCATTER_PORT, f"step '{step.id}' has `scatter: {scatter!r}`, which "
                          'names no input. `scatter:` is an input name or a list of them.', step.span, at)
    for entry in scatter if isinstance(scatter, list) else ():
        if not isinstance(entry, str):
            diagnostics.error(SophiosErrorCode.UNKNOWN_SCATTER_PORT, f"step '{step.id}' has the scatter entry "
                              f"{entry!r}, which is not a name. Each `scatter:` entry names one of the step's inputs.",
                              step.span, at)
