"""The CWL substrate the Sophios language compiles onto.

The language spec pins Sophios to one CWL version; that version belongs
here, in the language definition, not to whichever module emits a document.
Import `CWL_VERSION` rather than writing a version literal.
"""
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
import enum
from enum import StrEnum
from itertools import chain
from pathlib import Path
import re
from typing import Any, Final, TypeAlias, TypeGuard, TypeVar

from cwl_utils.errors import SubstitutionError
from cwl_utils.expression import scanner
from cwl_utils.parser import cwl_v1_2 as _standard
import yaml

from .diagnostics import SophiosError
from .error_codes import SophiosErrorCode
from .nodes import OpaqueCwl
from .subset import BOOLEAN, Expr, is_safe_subset_text, parse
from .types import (Array, Atom, Enum, Merge, Other, Pick, PortDeclaration, Place, Record, ScatterMethod, Type, Union,
                    Unread)


class CwlVersion(StrEnum):
    """Every CWL version the substrate toolchain actually accepts.

    Deliberately not the CWL spec's full `CWLVersion` enumeration: admitting a
    version cwltool cannot run would pass validation here and die later in the
    runner with a worse error. Sophios itself emits exactly one of these,
    `CWL_VERSION`.
    """

    V1_0 = 'v1.0'
    V1_1 = 'v1.1'
    V1_2 = 'v1.2'


#: The version Sophios emits. Every generated document declares this one.
#:
#: A plain `str`, not the enum member: PyYAML dispatches representers on
#: exact type, not `isinstance`, and cannot serialise a `StrEnum` subclass.
CWL_VERSION: Final[str] = CwlVersion.V1_2.value

#: Every admissible version as plain strings, for JSON Schema `enum` fields.
CWL_VERSIONS: Final[tuple[str, ...]] = tuple(version.value for version in CwlVersion)

# CWL's own vocabularies, read from the standard's schema as cwl_utils ships it: nothing else spells them.

#: The types that are not arrays, records or enums.
ATOMIC_TYPES: Final[frozenset[str]] = frozenset(_standard.CWLTypeLoader.symbols)

#: `scatterMethod`.
SCATTER_METHODS: Final[frozenset[str]] = frozenset(_standard.ScatterMethodLoader.symbols)

#: `linkMerge`.
LINK_MERGES: Final[frozenset[str]] = frozenset(_standard.LinkMergeMethodLoader.symbols)

#: `pickValue`, and the two of its methods that give one value where `all_non_null` gives a list.
PICK_VALUES: Final[frozenset[str]] = frozenset(_standard.PickValueMethodLoader.symbols)
PICKS_ONE: Final[frozenset[str]] = PICK_VALUES - {'all_non_null'}


def _hold_to_the_standard(enum: type[enum.Enum], *standard: Sequence[str]) -> None:
    """Fail at import when `enum` has a member more or a member less than the standard's own enumerations."""
    symbols = {symbol for each in standard for symbol in each}
    if (values := {member.value for member in enum}) != symbols:
        raise TypeError(f"{enum.__name__} is not the CWL standard's: {sorted(values ^ symbols)}")


_hold_to_the_standard(Atom, _standard.CWLTypeLoader.symbols, _standard.AnyLoader.symbols)
_hold_to_the_standard(ScatterMethod, _standard.ScatterMethodLoader.symbols)


#: What a tool document loads as: the standard's own classes.
Process: TypeAlias = _standard.CommandLineTool | _standard.Workflow | _standard.ExpressionTool | _standard.Operation

#: A port's type as the loader leaves it: a name, a schema of the standard's, or a union of them.
LoadedType: TypeAlias = str | _standard.Saveable | list['LoadedType']

#: What a document's sections hold: the field that names an entry, and the field a bare value in a mapping stands for.
_NAMED: Final = {'requirements': ('class', 'value'), 'hints': ('class', 'value'),
                 'inputs': ('id', 'type'), 'outputs': ('id', 'type')}
#: By section, the place of each requirement or hint a document wrote, in its order: the vendor's class and plain
#: fields, or `None` for one its process holds.
Written: TypeAlias = dict[str, dict[str, tuple[str, dict[str, object]] | None]]
_Requirement = TypeVar('_Requirement')


def _local(identifier: str) -> str:
    """A port's own name, without the document and the process the loader resolves its id against."""
    return identifier.rsplit('#', maxsplit=1)[-1].rsplit('/', maxsplit=1)[-1]


def _listed(document: dict[str, OpaqueCwl], section: str) -> list[tuple[str, OpaqueCwl]]:
    """The entries a document wrote in a section, in the order it wrote them, each with its place (its key in a
    mapping, its index in a list) and as the list form writes it: the standard sorts a mapping's."""
    name, bare = _NAMED[section]
    match document.get(section):
        case dict() as written:
            return [(str(key), {name: key, **body} if isinstance(body, dict) else {name: key} if body is None
                     else {name: key, bare: body}) for key, body in written.items()]
        case list() as written:
            return [(str(index), item) for index, item in enumerate(written)]
    return []


def _is_extension(item: OpaqueCwl) -> TypeGuard[dict[str, Any]]:
    """Whether a requirement or hint is a vendor's, such as `cwltool:CUDARequirement`: the standard's have no colon."""
    return isinstance(item, dict) and ':' in str(item.get('class'))


def _separate_read_as_false(binding: OpaqueCwl) -> OpaqueCwl:
    """A binding that writes `separate: null` and no prefix, as cwltool reads it at run: `separate: false`, which it
    refuses. The loader reads null as unset, so the refusal would be lost."""
    if isinstance(binding, dict) and binding.get('separate', True) is None and binding.get('prefix') is None:
        return {**binding, 'separate': False}
    return binding


def _refusal(error: Exception) -> SophiosError:
    """The loader's own message for a tool it refuses, as one error."""
    return SophiosError.error(SophiosErrorCode.INVALID_TOOL, ' '.join(str(error).split()))


def load_process(document: dict[str, OpaqueCwl], run_path: str) -> tuple[Process, Written]:
    """A tool document read once, whole, into the standard's classes, with its ports in the order it wrote them and
    its vendor's requirements and hints set aside.

    Raises:
        SophiosError: `api004` with the loader's own message, for a document that is not a CWL process.
    """
    if not isinstance(document, dict):
        raise SophiosError.error(SophiosErrorCode.INVALID_TOOL, f'{run_path} is not a CWL mapping')
    # Read as the one version Sophios writes: a tool that states an older one is read alike.
    standard = {**document, 'cwlVersion': CWL_VERSION}
    written: Written = {section: {} for section in ('requirements', 'hints')}
    for section in _NAMED:
        if isinstance(document.get(section), (dict, list)):
            items = _listed(document, section)
            kept = [item for _, item in items if not _is_extension(item)]
            if section == 'inputs':
                kept = [{**port, 'inputBinding': _separate_read_as_false(port['inputBinding'])}
                        if isinstance(port, dict) and 'inputBinding' in port else port for port in kept]
            standard[section] = kept
            if section in written:
                written[section] = {
                    place: (item['class'], {key: value for key, value in item.items() if key != 'class'})
                    if _is_extension(item) else None for place, item in items}
    if isinstance(arguments := document.get('arguments'), list):
        standard['arguments'] = [_separate_read_as_false(argument) for argument in arguments]
    # The real file URI lets relative `$include` / `$import` resolve beside the tool; no link check across files.
    base = Path(run_path).absolute().as_uri()
    try:
        process: Process = _standard.load_document_by_yaml(
            standard, base, _standard.LoadingOptions(fileuri=base, no_link_check=True))
    except _standard.ValidationException as error:
        raise _refusal(error) from error
    return process, written


def ports_named(loaded: list[Any]) -> dict[str, Any]:
    """A process's loaded ports by their own names."""
    return {_local(port.id): port for port in loaded}


def inherited(process: Process, name: str, body: Mapping[str, object]) -> _standard.Saveable | dict[str, object]:
    """The requirement or hint `name` that a process inherits, read as the standard's class, else as the loader leaves
    a class it does not know.

    Raises:
        SophiosError: `api004` with the loader's own message, for a body that is not that class.
    """
    cls = getattr(_standard, name, None)
    if not (isinstance(cls, type) and issubclass(cls, _standard.ProcessRequirement)):
        return {'class': name, **body}
    try:
        return cls.fromDoc({'class': name, **body}, str(process.id), process.loadingOptions)
    except _standard.ValidationException as error:
        raise _refusal(error) from error


def expand_shorthand(declared: OpaqueCwl) -> OpaqueCwl:
    """One layer of CWL's type shorthand, without changing the caller's declaration: `T[]` is an array of `T`, and
    `T?` is `T` or null."""
    if not isinstance(declared, str):
        return declared
    if declared.endswith('[]') and len(declared) > 2:
        return {'type': 'array', 'items': declared[:-2]}
    if declared.endswith('?') and len(declared) > 1:
        return ['null', declared[:-1]]
    return declared


def canonical_type(declared: OpaqueCwl) -> OpaqueCwl:
    """A written type with its shorthand expanded at every layer, through an array's items."""
    match expand_shorthand(declared):
        case ['null', inner] if isinstance(declared, str):
            return ['null', canonical_type(inner)]
        case dict() as array if array.get('type') == 'array' and 'items' in array:
            return {**array, 'items': canonical_type(array['items'])}
    return declared


_ATOMS: Final = {atom.value: atom for atom in Atom}
_STREAMS: Final = ('stdin', 'stdout', 'stderr')


def read_type(raw: 'OpaqueCwl') -> Type:
    """The neutral type a written one means; `read_typed` says what it wrote beyond that. `T?` and `T[]` shorthand
    is expanded wherever it is written, through array items and every union member, so `string?[]` is an array of
    nullable strings and not a nullable array, and `[File?, string]` accepts null in either spelling of that member.
    `stdin`, `stdout` and `stderr` mean `File`, the tool's own shorthand for "the stream, as a File"; only a
    CommandLineTool port may say it, and a workflow boundary promoted from one must say `File`, which is what
    cwltool makes of it. A type that holds itself is `Other` where it comes round again."""
    return _read(raw, (), [], frozenset())


def read_typed(raw: 'OpaqueCwl') -> tuple[Type, tuple[Unread, ...]]:
    """`read_type`, and what the type wrote that no node of it holds: every key of a schema but the ones its node
    holds, a record field's keys but its name and type, and the whole of a shape that is none of the types."""
    found: list[Unread] = []
    return _read(raw, (), found, frozenset()), tuple(found)


def _fields(raw: dict[str, Any]) -> 'list[tuple[str, Any, dict[str, Any]]] | None':
    """A record's fields as `(name, type, other keys)` in the order written, from the list form or the map form, or
    None where it writes neither."""
    match raw.get('fields'):
        case list() as entries if all(isinstance(entry, dict) and isinstance(entry.get('name'), str)
                                      and 'type' in entry for entry in entries):
            return [(entry['name'], entry['type'], {key: value for key, value in entry.items()
                                                    if key not in ('name', 'type')}) for entry in entries]
        case dict() as entries:
            return [(str(name), entry['type'], {key: value for key, value in entry.items() if key != 'type'})
                    if isinstance(entry, dict) and 'type' in entry else (str(name), entry, {})
                    for name, entry in entries.items()]
    return None


def _read(raw: Any, path: Place, unread: list[Unread], open_: frozenset[int]) -> Type:
    """`raw` as a type, the keys no node holds appended to `unread` at `path`."""
    if isinstance(raw, str):
        if raw in _STREAMS:
            return Atom.FILE
        layer = expand_shorthand(raw)
        if layer is not raw:
            return _read(layer, path, unread, open_)
        return _ATOMS.get(raw) or Other(raw)
    if id(raw) in open_:
        unread.append(Unread(path, None, deepcopy(raw)))
        return Other()
    inside = open_ | {id(raw)}
    if isinstance(raw, list):
        # A member's own shorthand, expanded: `null` once, first; no duplicates. What a member left unread moves to
        # the place its type takes in the union that results, not the place it was written.
        read: list[tuple[Type, list[Unread]]] = []
        for item in raw:
            held: list[Unread] = []
            read.append((_read(item, (), held, inside), held))
        members: list[Type] = []
        for expanded, _ in read:
            for member in expanded.members if isinstance(expanded, Union) else (expanded,):
                if member not in members:
                    members.append(member)
        union = Union((*[Atom.NULL] * (Atom.NULL in members), *(member for member in members if member != Atom.NULL)))
        for expanded, held in read:
            for found in held:
                inner, rest = ((expanded.members[int(found.path[0])], found.path[1:]) if isinstance(expanded, Union)
                               else (expanded, found.path))
                unread.append(replace(found, path=(*path, union.members.index(inner), *rest)))
        return union
    kind = raw.get('type') if isinstance(raw, dict) else None
    fields = _fields(raw) if kind == 'record' else None
    if kind not in ('array', 'record', 'enum') or kind == 'record' and fields is None \
            or kind == 'enum' and not (isinstance(raw.get('symbols'), list)
                                       and all(isinstance(symbol, str) for symbol in raw['symbols'])):
        if raw is not None:
            unread.append(Unread(path, None, deepcopy(raw)))
        return Other()
    keys = {'array': ('type', 'items'), 'record': ('type', 'fields'), 'enum': ('type', 'symbols')}[kind]
    unread.extend(Unread(path, str(key), deepcopy(value))
                  for key, value in sorted(raw.items(), key=lambda entry: str(entry[0])) if key not in keys)
    match kind:
        case 'array':
            return Array(_read(raw['items'], (*path, 'items'), unread, inside) if 'items' in raw else None)
        case 'record':
            for name, _, other in fields or ():
                unread.extend(Unread((*path, 'fields', name), key, deepcopy(value)) for key, value in other.items())
            return Record(tuple((name, _read(field, (*path, 'fields', name, 'type'), unread, inside))
                                for name, field, _ in fields or ()))
    return Enum(tuple(raw['symbols']))


def read_when(written: OpaqueCwl) -> tuple[Expr | None, tuple[str, ...], str | None]:
    """A step's `when` read: its tree where the safe subset reads the text, the inputs the text reads, and where there
    is no tree, why, in the subset's words."""
    reads = inputs_read(written)
    if not isinstance(written, str):
        return None, reads, 'a when is a string'
    if not is_safe_subset_text(written):
        return None, reads, 'the safe JavaScript subset requires the whole field to be one $( … ) reference'
    try:
        return parse(written), reads, None
    except ValueError as error:
        return None, reads, str(error)


def boolean_problem(written: OpaqueCwl, type_: str | None) -> str | None:
    """Why a `when` written `written`, of the subset's type `type_`, cannot give `true` or `false`, which CWL
    requires of one, the `when` quoted first; `None` when it gives one, and when nothing here can tell: the subset
    cannot parse or type it, or it is not a string, which cwltool's schema validation refuses itself."""
    if isinstance(written, str) and one_expression(written) is None:
        return f'{written!r} is not one $(...) or ${{...}} expression'
    if type_ not in (None, BOOLEAN):
        return f'{written!r} must compute a boolean, not a {type_}'
    return None


def one_expression(field: str) -> str | None:
    """How `field` opens, `$(` or `${`, when it is one CWL expression from end to end; else None.

    Read as cwltool reads an expression field: stripped of surrounding whitespace, then scanned by
    `cwl_utils.expression.scanner`, as `interpolate` in cwl_utils/expression.py scans it. Anything else is a template,
    text with or without expressions in it, which evaluates to text.
    """
    text = field.strip()
    try:
        span = scanner(text)
    except SubstitutionError:
        return None
    return text[:2] if span == (0, len(text)) and text[:2] in ('$(', '${') else None


#: What a workflow boundary port may state, by direction: the fields the CWL
#: v1.2 schema gives `WorkflowOutputParameter` and `WorkflowInputParameter`.
#: An allowlist, not a denylist, because a tool's port record is a superset of a
#: workflow's (`inputBinding` is a `CommandLineBinding` on a tool and an
#: `InputBinding` on a workflow input, so a promoted `position` fails
#: validation; `outputBinding` has no workflow counterpart). Only an input may
#: ask for `loadContents` and `loadListing`.
_OUTPUT_BOUNDARY_FIELDS: Final = frozenset({'type', 'format', 'label', 'doc', 'secondaryFiles', 'streamable'})
_INPUT_BOUNDARY_FIELDS: Final = _OUTPUT_BOUNDARY_FIELDS | {'loadContents', 'loadListing'}


def _evaluated(entry: OpaqueCwl) -> bool:
    """Whether a `secondaryFiles` entry holds an expression, as its pattern or as its `required`."""
    texts = entry.values() if isinstance(entry, dict) else (entry,)
    return any(isinstance(text, str) and EXPRESSION.search(text) for text in texts)


def _statable_secondary_files(value: OpaqueCwl) -> OpaqueCwl:
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


def port_declaration(raw: OpaqueCwl, *, output: bool = False) -> PortDeclaration:
    """Preserve one CWL port declaration and expose its recognized fields, with those a workflow boundary may state
    of it (`kept`) reduced here, where the direction is known."""
    if not isinstance(raw, dict):
        type_, unread = read_typed(raw)
        return PortDeclaration(type_, unread=unread, shorthand=True)
    reserved = {'type', 'format', 'default'}
    if output:
        reserved.add('outputSource')
    passthrough = tuple((key, deepcopy(value)) for key, value in raw.items() if key not in reserved)
    allowed = _OUTPUT_BOUNDARY_FIELDS if output else _INPUT_BOUNDARY_FIELDS
    kept: list[tuple[str, OpaqueCwl]] = []
    for name, value in passthrough:
        if name not in allowed:
            continue
        if name == 'secondaryFiles':
            value = _statable_secondary_files(value)
            if not value:
                continue
        kept.append((name, deepcopy(value)))
    type_, unread = read_typed(raw.get('type'))
    return PortDeclaration(
        type=type_,
        unread=unread,
        format=deepcopy(raw.get('format')),
        has_format='format' in raw,
        default=deepcopy(raw.get('default')),
        has_default='default' in raw,
        passthrough=passthrough,
        kept=tuple(kept),
    )


def canonical(type_: LoadedType) -> OpaqueCwl:
    """A loaded port type as `spell` writes one, so a tool's types and a workflow's read alike: a union
    is a list, an array `{type: array, items}`, and any other schema as the standard writes it."""
    match type_:
        case list():
            return [canonical(member) for member in type_]
        case _standard.CommandInputArraySchema() | _standard.CommandOutputArraySchema():
            return {'type': 'array', 'items': canonical(type_.items)}
        case str():
            return type_
    # An inline schema's name is the loader's `_:` id, no path: made relative, Windows reads `_:` as a drive.
    return type_.save(top=False, relative_uris=not str(getattr(type_, 'name', '')).startswith('_:'))


def required(type_: OpaqueCwl) -> tuple[OpaqueCwl, bool]:
    """A port's type without its `null`, and whether it had one: the one type that remains, or the list of the
    several."""
    if not isinstance(type_, list) or 'null' not in type_:
        return type_, False
    rest = [member for member in type_ if member != 'null']
    return (rest[0] if len(rest) == 1 else rest), True


def requirement(process: Process, cls: type[_Requirement]) -> _Requirement | None:
    """The first requirement of the standard's class `cls`, else the first hint of it."""
    return next((each for each in (*(process.requirements or ()), *(process.hints or ())) if isinstance(each, cls)),
                None)


#: Where a CWL expression starts: a parameter reference or a JavaScript expression, `$(`, or a function body, `${`.
EXPRESSION: Final = re.compile(r'\$[({]')

#: A size, as `needs: ram:` may write one: a whole number, not zero, of mebibytes, gibibytes or tebibytes.
SIZE: Final = re.compile('([0-9]*[1-9][0-9]*)([MGT])')


def declared(process: dict[str, Any], cls: str) -> list[tuple[str, dict[str, Any]]]:
    """The requirements and hints of a process document of the class `cls`, as the list form writes them, each with
    its section (`requirements` or `hints`), whether the section is written as a mapping or as a list."""
    return [(section, item) for section in ('requirements', 'hints') for _, item in _listed(process, section)
            if isinstance(item, dict) and item.get('class') == cls]


def require(process: dict[str, Any], section: str, cls: str) -> dict[str, Any] | None:
    """The entry of the class `cls` in `section` (`requirements` or `hints`) of a process document, added where the
    document writes that section, a mapping or a list; an entry it wrote is the one returned, with what it says. None
    for a section, or an entry, of another shape."""
    written = process.setdefault(section, [])
    found = None
    if isinstance(written, dict):
        found = written[cls] = written.get(cls) or {}
    elif isinstance(written, list):
        found = next((item for item in written if isinstance(item, dict) and item.get('class') == cls), None)
        if found is None:
            written.append(found := {'class': cls})
    return found if isinstance(found, dict) else None


def literal(text: str) -> str:
    """Text as it is written inside a CWL string that holds an expression, which cwltool unescapes: a backslash,
    and what would start an expression, each behind a backslash."""
    return text.replace('\\', '\\\\').replace('$(', '\\$(').replace('${', '\\${')


def strings(value: Any) -> Iterator[str]:
    """Every string in `value`: itself, or the strings at any depth of a list or of a mapping's values."""
    match value:
        case str():
            yield value
        case list():
            yield from chain.from_iterable(map(strings, value))
        case Mapping():
            yield from strings(list(value.values()))


def declare_javascript(process: dict[str, Any]) -> None:
    """Declare `InlineJavascriptRequirement` in a tool that holds an expression and declares it nowhere, as a
    requirement or a hint: one rule, for a tool written in `.wic` and for the Python builder. A `requirements` that is
    neither a mapping nor a list is left as it is."""
    javascript = _standard.InlineJavascriptRequirement.__name__
    if any(map(EXPRESSION.search, strings(process))):
        if not declared(process, javascript):
            require(process, 'requirements', javascript)


#: An input a CWL expression reads, `inputs.name` or `inputs["name"]`: `inputs.name-1` is `inputs.name` minus 1.
_INPUT_READ: Final = re.compile(r'\binputs(?:\.([A-Za-z_]\w*)|\[\s*["\']([^"\']+)["\']\s*\])')


def inputs_read(value: Any) -> tuple[str, ...]:
    """Every input the CWL expressions in `value` (a string, or the strings at any depth of a list or mapping) read,
    in order of first mention."""
    return tuple(dict.fromkeys(a or b for text in strings(value) for a, b in _INPUT_READ.findall(text)))


def input_reference(name: str, field: str = '') -> str | None:
    """The CWL parameter reference to `field` of the input `name`, in the form CWL has for the name: `$(inputs.name)`
    for a symbol, `$(inputs["name"])` for any other. It is what `inputs_read` reads back; None for a name neither
    form holds (one with a quote in it)."""
    read = next((text for text in (f'inputs.{name}', f'inputs["{name}"]') if inputs_read(text) == (name,)), None)
    return read and f'$({read}{field})'


def fields_set(value: Any) -> list[str]:
    """The fields of a loaded object that were written, spelled as the standard spells them, then a vendor's as its
    document spelled them."""
    spelled = ((name, name + '_' if name in ('class', 'in', 'type') else name) for name in sorted(type(value).attrs))
    # The loader names an anonymous schema `_:<uuid>`, which no author wrote.
    standard = [name for name, attribute in spelled
                if (found := getattr(value, attribute)) is not None and not (name == 'name' and found.startswith('_:'))]
    namespaces = value.loadingOptions.namespaces or {}
    return [*standard, *(next((f'{prefix}:{name.removeprefix(uri)}' for prefix, uri in namespaces.items()
                               if name.startswith(uri)), name) for name in value.extension_fields)]


def read_tool(path: str) -> dict[str, Any]:
    """The process the tool file at `path` defines, read when a step first needs it.

    Raises:
        SophiosError: `wic001` for a file that is not YAML or cannot be read; see `tool_process` for the others.
    """
    try:
        document = yaml.safe_load(tool_text(path))
    except yaml.YAMLError as error:
        raise SophiosError.error(SophiosErrorCode.INVALID_YAML,
                                 f'The tool {path} is not valid YAML: {" ".join(str(error).split())}') from error
    return tool_process(document, path)


def tool_text(path: str) -> str:
    """The text of the tool file at `path`, in whatever language it is written.

    Raises:
        SophiosError: `wic001` for a file that cannot be read as UTF-8 text.
    """
    try:
        return Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as error:
        raise SophiosError.error(SophiosErrorCode.INVALID_YAML,
                                 f'The tool {path} cannot be read as UTF-8 text: {error}') from error


def tool_process(cwl: dict[str, Any], source: str) -> dict[str, Any]:
    """The process a tool document defines: the document itself, or its ``main`` if it is packed.

    A CWL runner runs a packed document through its ``main`` process and
    refuses one without; Sophios reads a tool the same way, once, where the
    document is loaded, so every reader of a tool's CWL sees one process.
    ``main`` is the ``$graph`` entry of that id, or the whole document when
    ``cwltool --pack`` packs a single tool (no ``$graph``, ``id: '#main'``).
    It is written out as a file of its own, so it takes the ``cwlVersion``,
    ``$namespaces`` and ``$schemas`` the packed document declares once at its
    top, and drops the ``id`` that placed it in the graph. A packer writes
    ``main``'s ports as ``#main/<port>``; they lose that prefix, so each port
    keeps the name it is bound by.

    Args:
        cwl (dict[str, Any]): A loaded CWL document.
        source (str): Where it came from, for the diagnostic.

    Raises:
        SophiosError: ``SUBWORKFLOW_INVALID`` for a ``$graph`` with no ``main``.
    """
    if not isinstance(cwl, dict):
        return cwl  # not a CWL document: cwltool refuses it at the step that runs it
    graph = cwl.get('$graph')
    if not isinstance(graph, list):
        return _as_main(cwl) if cwl.get('id') in ('main', '#main') else cwl
    processes = [entry for entry in graph if isinstance(entry, dict)]
    main = next((entry for entry in processes if entry.get('id') in ('main', '#main')), None)
    if main is None:
        ids = ', '.join(str(entry.get('id')) for entry in processes)
        raise SophiosError.error(
            SophiosErrorCode.SUBWORKFLOW_INVALID,
            f'{source} is a packed CWL document ($graph) with no main process; it holds: {ids}. '
            'CWL runs a packed document through its main, so name the process to run main, '
            'or unpack it into a file of its own')
    shared: dict[str, Any] = {key: cwl[key] for key in ('cwlVersion', '$namespaces', '$schemas') if key in cwl}
    return _as_main({**shared, **main})


def _as_main(process: dict[str, Any]) -> dict[str, Any]:
    """A packed ``main`` as a file of its own: without its ``id``, and its ports named as they are bound."""
    process = {key: value for key, value in process.items() if key != 'id'}
    for ports in ('inputs', 'outputs'):
        if isinstance(process.get(ports), list):
            process[ports] = [_without_main_prefix(port) for port in process[ports]]
    return process


def _without_main_prefix(port: Any) -> Any:
    """A port of a packed ``main`` with its ``id`` as ``<port>``, not ``#main/<port>`` or ``main/<port>``."""
    if isinstance(port, dict) and isinstance(port.get('id'), str):
        return {**port, 'id': port['id'].removeprefix('#').removeprefix('main/')}
    return port
