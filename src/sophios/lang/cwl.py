"""The CWL substrate the Sophios language compiles onto.

The language spec pins Sophios to one CWL version; that version belongs
here, in the language definition, not to whichever module emits a document.
Import `CWL_VERSION` rather than writing a version literal.
"""
from enum import Enum, StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Final, TypeAlias, TypeVar

from cwl_utils.parser import cwl_v1_2 as _standard

from .diagnostics import SophiosError
from .error_codes import SophiosErrorCode


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


def _hold_to_the_standard(enum: type[Enum], *loaders: Any) -> None:
    """Fail at import when `enum` has a member more or a member less than the standard's own enumerations."""
    symbols = {symbol for loader in loaders for symbol in loader.symbols}
    if (values := {member.value for member in enum}) != symbols:
        raise TypeError(f"{enum.__name__} is not the CWL standard's: {sorted(values ^ symbols)}")


class CWLAtomicType(str, Enum):
    """The names of `ATOMIC_TYPES`, and `Any`."""

    NULL = "null"
    BOOLEAN = "boolean"
    INT = "int"
    LONG = "long"
    FLOAT = "float"
    DOUBLE = "double"
    STRING = "string"
    FILE = "File"
    DIRECTORY = "Directory"
    ANY = "Any"


class ScatterMethod(str, Enum):
    """The names of `SCATTER_METHODS`."""

    dotproduct = "dotproduct"
    flat_crossproduct = "flat_crossproduct"
    nested_crossproduct = "nested_crossproduct"


_hold_to_the_standard(CWLAtomicType, _standard.CWLTypeLoader, _standard.AnyLoader)
_hold_to_the_standard(ScatterMethod, _standard.ScatterMethodLoader)


#: What a tool document loads as: the standard's own classes.
Process: TypeAlias = _standard.CommandLineTool | _standard.Workflow | _standard.ExpressionTool | _standard.Operation

#: What a document's sections hold: the field that names an entry, and the field a bare value in a mapping stands for.
_NAMED: Final = {'requirements': ('class', 'value'), 'hints': ('class', 'value'),
                 'inputs': ('id', 'type'), 'outputs': ('id', 'type')}
#: By section, the place of each requirement or hint a document wrote, in its order: the vendor's class and plain
#: fields, or `None` for one its process holds.
Written: TypeAlias = dict[str, dict[str, tuple[str, dict[str, Any]] | None]]
_Requirement = TypeVar('_Requirement')


def _local(identifier: str) -> str:
    """A port's own name, without the document and the process the loader resolves its id against."""
    return identifier.rsplit('#', maxsplit=1)[-1].rsplit('/', maxsplit=1)[-1]


def _listed(document: dict[str, Any], section: str) -> list[tuple[str, Any]]:
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


def _is_extension(item: Any) -> bool:
    """Whether a requirement or hint is a vendor's, such as `cwltool:CUDARequirement`: the standard's have no colon."""
    return isinstance(item, dict) and ':' in str(item.get('class'))


def _separate_read_as_false(binding: Any) -> Any:
    """A binding that writes `separate: null` and no prefix, as cwltool reads it at run: `separate: false`, which it
    refuses. The loader reads null as unset, so the refusal would be lost."""
    if isinstance(binding, dict) and binding.get('separate', True) is None and binding.get('prefix') is None:
        return {**binding, 'separate': False}
    return binding


def _refusal(error: Exception) -> SophiosError:
    """The loader's own message for a tool it refuses, as one error."""
    return SophiosError.error(SophiosErrorCode.INVALID_TOOL, ' '.join(str(error).split()))


def load_process(document: dict[str, Any], run_path: str) -> tuple[Process, Written]:
    """A tool document read once, whole, into the standard's classes, with its ports in the order it wrote them and
    its vendor's requirements and hints set aside.

    Raises:
        SophiosError: `api004` with the loader's own message, for a document that is not a CWL process.
    """
    # Read as the one version Sophios writes: a tool that states an older one is read alike.
    standard = {**document, 'cwlVersion': CWL_VERSION}
    written: Written = {section: {} for section in ('requirements', 'hints')}
    for section in _NAMED:
        if isinstance(document.get(section), (dict, list)):
            items = _listed(document, section)
            standard[section] = [item for _, item in items if not _is_extension(item)]
            if section == 'inputs':
                standard[section] = [{**port, 'inputBinding': _separate_read_as_false(port['inputBinding'])}
                                     if isinstance(port, dict) and 'inputBinding' in port else port
                                     for port in standard[section]]
            if section in written:
                written[section] = {
                    place: (item['class'], {key: value for key, value in item.items() if key != 'class'})
                    if _is_extension(item) else None for place, item in items}
    if isinstance(document.get('arguments'), list):
        standard['arguments'] = [_separate_read_as_false(argument) for argument in document['arguments']]
    # A base with a scheme lets a tool's own types resolve, as the standard's loader checks no link across one.
    base = PurePosixPath('/', Path(run_path).name).as_uri()
    try:
        process: Process = _standard.load_document_by_yaml(
            standard, base, _standard.LoadingOptions(fileuri=base, no_link_check=True))
    except _standard.ValidationException as error:
        raise _refusal(error) from error
    return process, written


def ports_named(loaded: list[Any]) -> dict[str, Any]:
    """A process's loaded ports by their own names."""
    return {_local(port.id): port for port in loaded}


def inherited(process: Process, name: str, body: dict[str, Any]) -> Any:
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


def expand_shorthand(declared: Any) -> Any:
    """One layer of CWL's type shorthand, without changing the caller's declaration: `T[]` is an array of `T`, and
    `T?` is `T` or null."""
    if not isinstance(declared, str):
        return declared
    if declared.endswith('[]') and len(declared) > 2:
        return {'type': 'array', 'items': declared[:-2]}
    if declared.endswith('?') and len(declared) > 1:
        return ['null', declared[:-1]]
    return declared


def canonical_type(declared: Any) -> Any:
    """A written type with its shorthand expanded at every layer, through an array's items."""
    match expand_shorthand(declared) if isinstance(declared, str) else declared:
        case ['null', inner] if isinstance(declared, str):
            return ['null', canonical_type(inner)]
        case {'type': 'array', 'items': inner} as array:
            return {**array, 'items': canonical_type(inner)}
    return declared


def canonical(type_: Any) -> Any:
    """A loaded port type as `PortType.canonical` spells one, so a tool's types and a workflow's read alike: a union
    is a list, an array `{type: array, items}`, and any other schema as the standard writes it."""
    match type_:
        case list():
            return [canonical(member) for member in type_]
        case _standard.CommandInputArraySchema() | _standard.CommandOutputArraySchema():
            return {'type': 'array', 'items': canonical(type_.items)}
        case str():
            return type_
    return type_.save(top=False)


def required(type_: Any) -> tuple[Any, bool]:
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
