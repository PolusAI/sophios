"""Reading one written port declaration: the type and the fields a workflow boundary may state of it, and the ports a
workflow's `inputs:` and `outputs:` declare. The door reads them; the middle end holds the result."""
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .cwl_fields import unread as refused_fields
from .spans import SourceSpan
from .subset import EXPRESSION
from .types import Array, PortDeclaration, Type, Union, Unread, read_typed
from .unread import UnreadField
from .values import read_value

if TYPE_CHECKING:
    from .nodes import Value


#: What a workflow boundary port may state, by direction: the fields the CWL
#: v1.2 schema gives `WorkflowOutputParameter` and `WorkflowInputParameter`.
#: An allowlist, not a denylist, because a tool's port record is a superset of a
#: workflow's (`inputBinding` is a `CommandLineBinding` on a tool and an
#: `InputBinding` on a workflow input, so a promoted `position` fails
#: validation; `outputBinding` has no workflow counterpart). Only an input may
#: ask for `loadContents` and `loadListing`.
_OUTPUT_BOUNDARY_FIELDS: Final = frozenset({'type', 'format', 'label', 'doc', 'secondaryFiles', 'streamable'})
_INPUT_BOUNDARY_FIELDS: Final = _OUTPUT_BOUNDARY_FIELDS | {'loadContents', 'loadListing'}


def _evaluated(entry: 'Value') -> bool:
    """Whether a `secondaryFiles` entry holds an expression, as its pattern or as its `required`."""
    texts = entry.values() if isinstance(entry, dict) else (entry,)
    return any(isinstance(text, str) and EXPRESSION.search(text) for text in texts)


def _statable_secondary_files(value: 'Value') -> 'Value':
    """The `secondaryFiles` entries a workflow boundary can state.

    An expression is evaluated by the tool that wrote it, against that tool's
    `inputs` and `expressionLib`. The boundary has neither, so it would
    evaluate the expression differently or fail on it at run time; the tool
    still declares the entry and keeps evaluating it.

    A bare pattern or a single mapping is stated as a one-element list: it is
    the same declaration, and cwltool's checker reads every entry as a mapping
    at workflow level, so it fails on the bare form.
    """
    entries = value if isinstance(value, list) else [] if value is None else [value]
    return [entry for entry in entries if not _evaluated(entry)]


def named_entries(raw: object) -> tuple[tuple[str, 'Value'], ...]:
    """CWL's map form (`{name: decl}`) or its `id`-keyed list form."""
    match raw:
        case dict():
            return tuple(raw.items())
        case list():
            return tuple((str(item['id']).rsplit('#', 1)[-1], {k: v for k, v in item.items() if k != 'id'})
                         for item in raw)
        case _:
            return ()


def _schemas(type_: Type, found: tuple[Unread, ...], owner: str) -> tuple[tuple[UnreadField, ...], ...]:
    """The refusals of what each array of `type_` (or of its members) writes beside `type` and `items`."""
    places = [((index,), member) for index, member in enumerate(type_.members)] if isinstance(type_, Union) \
        else [((), type_)]
    return tuple(refused_fields(owner, [item.key for item in found if item.path == path and item.key is not None])
                 for path, member in places if isinstance(member, Array))


def port_declaration(raw: 'Value', *, output: bool = False) -> PortDeclaration:
    """Preserve one CWL port declaration and expose its recognized fields, with those a workflow boundary may state
    of it (`kept`) reduced here, where the direction is known."""
    if not isinstance(raw, dict):
        type_, found = read_typed(raw)
        return PortDeclaration(type_, unread=found, shorthand=True,
                               schemas=_schemas(type_, found, 'OutputArraySchema' if output else 'InputArraySchema'))
    reserved = {'type', 'format', 'default'}
    if output:
        reserved.add('outputSource')
    passthrough = tuple((key, deepcopy(value)) for key, value in raw.items() if key not in reserved)
    allowed = _OUTPUT_BOUNDARY_FIELDS if output else _INPUT_BOUNDARY_FIELDS
    kept: list[tuple[str, 'Value']] = []
    for name, value in passthrough:
        if name not in allowed:
            continue
        if name == 'secondaryFiles':
            value = _statable_secondary_files(value)
            if not value:
                continue
        kept.append((name, deepcopy(value)))
    type_, found = read_typed(raw.get('type'))
    owner = 'WorkflowOutputParameter' if output else 'WorkflowInputParameter'
    return PortDeclaration(
        type=type_,
        unread=found,
        format=deepcopy(raw.get('format')),
        has_format='format' in raw,
        default=read_value(deepcopy(raw.get('default'))),
        has_default='default' in raw,
        passthrough=passthrough,
        kept=tuple(kept),
        refused=refused_fields(owner, (name for name, _ in passthrough)),
        schemas=_schemas(type_, found, 'OutputArraySchema' if output else 'InputArraySchema'),
    )


@dataclass(frozen=True, slots=True)
class BoundaryPort:
    """One input or output a workflow document declares, as the middle end reads it. `unread` names, for an output,
    what it wrote beside its source that Sophios does not read: `linkMerge`, `pickValue` and a list `outputSource`."""

    name: str
    declaration: PortDeclaration
    output_source: 'Value' = None
    has_output_source: bool = False
    span: SourceSpan | None = None
    unread: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Boundary:
    """The ports a workflow document declares, in the order written, and the sides it wrote as a list holding an
    `$import` or `$include`, whose ports only the loader of that file can read."""

    inputs: tuple[BoundaryPort, ...] = ()
    outputs: tuple[BoundaryPort, ...] = ()
    listed: tuple[str, ...] = ()


def read_boundary(passthrough: 'tuple[tuple[str, Value], ...]',
                  port_spans: tuple[tuple[str, str, SourceSpan], ...]) -> Boundary:
    """The ports a document's `inputs:` and `outputs:` declare, read out of the keys it passes through. A side written
    as a list is `listed` and holds no port."""
    written = dict(passthrough)
    ports: dict[str, tuple[BoundaryPort, ...]] = {}
    for side in ('inputs', 'outputs'):
        spans = {name: span for section, name, span in port_spans if section == side}
        raw = written.get(side)
        entries = tuple(raw.items()) if isinstance(raw, dict) else ()
        ports[side] = tuple(_boundary_port(str(name), declaration, side == 'outputs', spans.get(str(name)))
                            for name, declaration in entries)
    return Boundary(ports['inputs'], ports['outputs'],
                    tuple(side for side in ('inputs', 'outputs') if isinstance(written.get(side), list)))


def _boundary_port(name: str, raw: 'Value', output: bool, span: SourceSpan | None) -> BoundaryPort:
    """One port of a document's `inputs:` or `outputs:`; the declaration is read as it is written, whatever it is."""
    declaration = port_declaration(raw, output=output)
    if not (output and isinstance(raw, dict)):
        return BoundaryPort(name, declaration, span=span)
    unread: list[str] = [key for key in ('linkMerge', 'pickValue') if key in raw]
    unread += ['a list outputSource'] * isinstance(raw.get('outputSource'), list)
    return BoundaryPort(name, declaration, raw.get('outputSource'), 'outputSource' in raw, span, tuple(unread))
