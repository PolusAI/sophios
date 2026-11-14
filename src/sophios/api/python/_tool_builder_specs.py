"""Private spec wrappers for the Tool Builder.

Every class here is a thin, order-preserving wrapper around a `cwl_utils`
generated class: constructors accept the builder's public (snake_case)
argument names, and `to_dict()` builds the matching
`cwl_utils.parser.cwl_v1_2` object and renders it with `.save()`. CWL
semantics (what fields exist, what they mean, how they serialize) come
from `cwl_utils`; this module only adapts the calling convention.
"""

# pylint: disable=too-few-public-methods
# pylint: disable=too-many-instance-attributes,too-many-arguments
# pylint: disable=too-many-locals,redefined-builtin,too-many-lines

from collections.abc import Mapping
import copy
from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Any, ClassVar, Final, Self

from cwl_utils.parser import cwl_v1_2 as _cwl_v12

from ._tool_builder_support import _merge_if_set, _render, _render_doc
from ._utils import validate_python_identifier_name

#: No `default` given: `None` is a default a tool can state.
_UNSET: Final = object()


def _canonicalize_type(type_: Any) -> Any:
    rendered = _render(type_)
    match rendered:
        case str() as text if text.endswith("?"):
            return ["null", _canonicalize_type(text[:-1])]
        case str() as text if text.endswith("[]"):
            return {"type": "array", "items": _canonicalize_type(text[:-2])}
        case dict() as mapping if mapping.get("type") == "array" and "items" in mapping:
            return {**mapping, "items": _canonicalize_type(mapping["items"])}
        case _:
            return rendered


class _CWLNamespace:
    """The CWL type vocabulary, as `cwl`: the types an `Input`, `Output` or `Field` takes.

    The attributes are the CWL atomic types, written as their CWL names:
    `cwl.null`, `cwl.boolean`, `cwl.int`, `cwl.long`, `cwl.float`, `cwl.double`,
    `cwl.string`, `cwl.file` (`File`) and `cwl.directory` (`Directory`). The
    methods build the composite types. A CWL type string such as `"File?"`,
    `"string[]"` or `"Any"` is accepted wherever a type is.
    """

    __slots__ = ()

    null = "null"
    boolean = "boolean"
    int = "int"
    long = "long"
    float = "float"
    double = "double"
    string = "string"
    file = "File"
    directory = "Directory"

    def optional(self, type_: Any) -> list[Any]:
        """Make a type optional: the union `["null", type_]`.

        Args:
            type_ (Any): The type; a type that is already optional is returned as it is.

        Returns:
            list[Any]: The union, as CWL writes it.
        """
        canonical = _canonicalize_type(type_)
        if isinstance(canonical, list) and self.null in canonical:
            return canonical
        return [self.null, canonical]

    def array(self, items: Any) -> dict[str, Any]:
        """Make an array type: `{type: array, items: ...}`, which `"<items>[]"` also spells.

        Args:
            items (Any): The type of each element.

        Returns:
            dict[str, Any]: The array type.
        """
        return {"type": "array", "items": _canonicalize_type(items)}

    def enum(self, *symbols: str, name: str | None = None) -> dict[str, Any]:
        """Make an enum type: `{type: enum, symbols: [...]}`, a string from a fixed set.

        Args:
            symbols (str): The allowed values.
            name (str | None): The type's `name`; a type given to `schema_definitions()`
                needs one.

        Returns:
            dict[str, Any]: The enum type.
        """
        payload: dict[str, Any] = {"type": "enum", "symbols": list(symbols)}
        _merge_if_set(payload, "name", name)
        return payload

    def record(
        self,
        fields: Mapping[str, "FieldSpec"] | list["FieldSpec | dict[str, Any]"],
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Make a record type: `{type: record, fields: [...]}`, a mapping with typed fields.

        Args:
            fields (Mapping[str, FieldSpec] | list[FieldSpec | dict[str, Any]]): The
                `fields`: a `Fields(...)` collection or a mapping of names to `Field`
                objects; or a list of named `Field` objects or field mappings.
            name (str | None): The type's `name`; a type given to `schema_definitions()`
                needs one.

        Returns:
            dict[str, Any]: The record type.
        """
        if isinstance(fields, Mapping):
            fields = [spec.named(field_name) for field_name, spec in fields.items()]
        payload: dict[str, Any] = {"type": "record", "fields": _render([*fields])}
        _merge_if_set(payload, "name", name)
        return payload


cwl = _CWLNamespace()


def _camel(name: str) -> str:
    """`docker_pull` -> `dockerPull`: a builder field name as its CWL name."""
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


@dataclass(eq=False)
class _CWLObject:
    """A builder spec whose fields are passed, camelCased, to its `cwl_utils` class `_cwl`.

    `extra` is a raw CWL mapping applied over the rendered payload last. A
    field's `render` metadata, if any, prepares its value.
    """

    _cwl: ClassVar[type]
    extra: dict[str, Any] = field(default_factory=dict, kw_only=True)

    def to_dict(self) -> dict[str, Any]:
        """Render the object as its CWL mapping, with `extra` applied last.

        Returns:
            dict[str, Any]: The mapping, without its `class`.
        """
        kwargs = {
            _camel(item.name): item.metadata.get("render", _render)(getattr(self, item.name))
            for item in dataclass_fields(self)
            if item.name != "extra" and getattr(self, item.name) is not None
        }
        payload: dict[str, Any] = self._cwl(**kwargs).save()
        payload.pop("class", None)
        payload.update(_render(self.extra))
        return payload


@dataclass(eq=False)
class SecondaryFile(_CWLObject):
    """A `secondaryFiles` entry: a file that travels with the primary `File`.

    Args:
        pattern (Any): `pattern`: a suffix such as `.bai` added to the primary file's
            name, `^` to strip one extension first (`^.bai`), or an expression.
        required (Any): `required`: whether the runner fails when the file is missing.
            CWL's default is `True` for inputs and `False` for outputs.
        extra (dict[str, Any]): Further fields, written as given.
    """

    _cwl = _cwl_v12.SecondaryFileSchema
    pattern: Any
    required: Any = None


def secondary_file(pattern: Any, *, required: bool | str | None = None, **extra: Any) -> "SecondaryFile":
    """Make a `secondaryFiles` entry, for `Input(secondary_files=[...])` or `Output(...)`.

    Args:
        pattern (Any): `pattern`, such as `.bai` or `^.bai`.
        required (bool | str | None): `required`, or an expression for it.
        extra (Any): Further fields, written as given.

    Returns:
        SecondaryFile: The entry.
    """
    return SecondaryFile(pattern=pattern, required=required, extra=extra)


@dataclass(eq=False)
class Dirent(_CWLObject):
    """An `InitialWorkDirRequirement` `listing` entry: a file or directory placed before the tool runs.

    Args:
        entry (Any): `entry`: the contents, as text, or an expression such as
            `$(inputs.reads)` for a `File` or `Directory`.
        entryname (Any): `entryname`, the name in the working directory.
        writable (Any): `writable`: whether the tool may change it.
        extra (dict[str, Any]): Further fields, written as given.
    """

    _cwl = _cwl_v12.Dirent
    entry: Any
    entryname: Any = None
    writable: Any = None

    @classmethod
    def from_input(
        cls,
        reference: Any,
        *,
        writable: bool = False,
        entryname: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> "Dirent":
        """Make the entry that stages one input, as `CommandLineTool.stage()` does.

        Args:
            reference (Any): The input, as `inputs.<name>`.
            writable (bool): `writable`.
            entryname (str | None): `entryname`; `$(inputs.<name>.basename)` by default.
            extra (dict[str, Any] | None): Further fields, written as given.

        Returns:
            Dirent: The entry, with `entry: $(inputs.<name>)`.

        Raises:
            TypeError: If `reference` is not a named input.
        """
        name = getattr(reference, "name", None)
        if not (isinstance(name, str) and name):
            raise TypeError("input reference must be a named Input/Output object")
        return cls(f"$(inputs.{name})", entryname or f"$(inputs.{name}.basename)", writable, extra=extra or {})


@dataclass(eq=False)
class EnvironmentDef(_CWLObject):
    """An `EnvVarRequirement` `envDef` entry: one environment variable.

    Args:
        env_name (Any): `envName`.
        env_value (Any): `envValue`, or an expression for it.
        extra (dict[str, Any]): Further fields, written as given.
    """

    _cwl = _cwl_v12.EnvironmentDef
    env_name: Any
    env_value: Any


@dataclass(eq=False)
class CommandLineBinding(_CWLObject):
    """A `CommandLineBinding`: how a value becomes words on the command line.

    Args:
        position (Any): `position`, the sort key among inputs and arguments.
        prefix (Any): `prefix`, a flag written before the value.
        separate (Any): `separate`; `False` joins the prefix and the value.
        item_separator (Any): `itemSeparator`, to join an array into one word.
        value_from (Any): `valueFrom`, an expression that replaces the value.
        shell_quote (Any): `shellQuote`; `False` needs a `ShellCommandRequirement`.
        extra (dict[str, Any]): Further fields, written as given.
    """

    _cwl = _cwl_v12.CommandLineBinding
    position: Any = None
    prefix: Any = None
    separate: Any = None
    item_separator: Any = None
    value_from: Any = None
    shell_quote: Any = None


@dataclass(eq=False)
class CommandOutputBinding(_CWLObject):
    """A `CommandOutputBinding`: how an output is collected after the tool runs.

    Args:
        glob (Any): `glob`, the file name pattern, or an expression, matched in the
            output directory.
        load_contents (Any): `loadContents`: read the first 64 KiB of each match into
            its `contents`.
        output_eval (Any): `outputEval`, an expression over the matches (`self`) that
            gives the output's value.
        extra (dict[str, Any]): Further fields, written as given.
    """

    _cwl = _cwl_v12.CommandOutputBinding
    glob: Any = None
    load_contents: Any = None
    output_eval: Any = None


@dataclass(frozen=True, slots=True)
class CommandArgument:
    """An `arguments` entry, for `CommandLineTool.add_argument()`.

    Args:
        value (Any): The `valueFrom`: a literal word or an expression.
        binding (CommandLineBinding | None): The binding fields, such as `position`.
        extra (dict[str, Any]): Further fields, written as given.
    """

    value: Any = None
    binding: CommandLineBinding | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_cwl(self) -> str | dict[str, Any]:
        """Render the argument as CWL writes it.

        Returns:
            str | dict[str, Any]: The bare word when only a string `value` is set,
            otherwise the binding mapping.
        """
        payload = {} if self.binding is None else dict(self.binding.to_dict())
        if not payload and not self.extra and isinstance(self.value, str | None):
            return self.value or ""
        _merge_if_set(payload, "valueFrom", self.value)
        payload.update(_render(self.extra))
        return payload


class _RequirementSpec(_CWLObject):
    """A requirement or hint: `to_fields()` under its `class_name`, saved by the `cwl_utils` class `_cwl`."""

    class_name: ClassVar[str]

    def __init_subclass__(cls) -> None:
        cls.class_name = cls.__name__
        cls._cwl = getattr(_cwl_v12, cls.__name__)

    def to_fields(self) -> dict[str, Any]:
        """Render the requirement's fields, without its `class`.

        Returns:
            dict[str, Any]: The fields.
        """
        return self.to_dict()


@dataclass(eq=False)
class DockerRequirement(_RequirementSpec):
    """A `DockerRequirement`: the container the tool runs in. `CommandLineTool.docker()` writes one.

    Args:
        docker_pull (Any): `dockerPull`, the image the runner pulls.
        docker_load (Any): `dockerLoad`, an image archive to load.
        docker_file (Any): `dockerFile`, a Dockerfile to build the image from.
        docker_import (Any): `dockerImport`, an image tarball to import.
        docker_image_id (Any): `dockerImageId`, the name the image has locally.
        docker_output_directory (Any): `dockerOutputDirectory`, where the tool writes
            its outputs inside the container.
        extra (dict[str, Any]): Further fields, written as given.
    """

    docker_pull: Any = None
    docker_load: Any = None
    docker_file: Any = None
    docker_import: Any = None
    docker_image_id: Any = None
    docker_output_directory: Any = None


@dataclass(eq=False)
class InlineJavascriptRequirement(_RequirementSpec):
    """An `InlineJavascriptRequirement`: JavaScript in expressions. `CommandLineTool.inline_javascript()` writes one.

    Args:
        expression_lib (Any): `expressionLib`, JavaScript that every expression can use.
        extra (dict[str, Any]): Further fields, written as given.
    """

    expression_lib: Any = None


@dataclass(eq=False)
class SchemaDefRequirement(_RequirementSpec):
    """A `SchemaDefRequirement`: named types. `CommandLineTool.schema_definitions()` writes one.

    Args:
        types (Any): `types`, each a named record or enum.
        extra (dict[str, Any]): Further fields, written as given.
    """

    types: Any = field(metadata={"render": lambda values: [_canonicalize_type(value) for value in values]})


@dataclass(eq=False)
class LoadListingRequirement(_RequirementSpec):
    """A `LoadListingRequirement`: how deep `Directory` inputs are listed. `CommandLineTool.load_listing()` writes one.

    Args:
        load_listing (Any): `loadListing`: `no_listing`, `shallow_listing` or `deep_listing`.
        extra (dict[str, Any]): Further fields, written as given.
    """

    load_listing: Any


@dataclass(eq=False)
class ShellCommandRequirement(_RequirementSpec):
    """A `ShellCommandRequirement`: run the command line through a shell. `CommandLineTool.shell_command()` writes one.

    Args:
        extra (dict[str, Any]): Further fields, written as given.
    """


@dataclass(eq=False)
class SoftwarePackage(_CWLObject):
    """A `SoftwareRequirement` `packages` entry.

    Args:
        package (Any): `package`, the software's name.
        version (Any): `version`, a list of acceptable versions.
        specs (Any): `specs`, IRIs that identify the package, such as a bio.tools or
            Bioconda IRI.
        extra (dict[str, Any]): Further fields, written as given.
    """

    _cwl = _cwl_v12.SoftwarePackage
    package: Any
    version: Any = None
    specs: Any = None


@dataclass(eq=False)
class SoftwareRequirement(_RequirementSpec):
    """A `SoftwareRequirement`: the software the tool needs. `CommandLineTool.software()` writes one.

    Args:
        packages (Any): `packages`, each a `SoftwarePackage` or its mapping.
        extra (dict[str, Any]): Further fields, written as given.
    """

    packages: Any


@dataclass(eq=False)
class InitialWorkDirRequirement(_RequirementSpec):
    """An `InitialWorkDirRequirement`: what is placed in the working directory first.

    `CommandLineTool.initial_workdir()` writes one; `CommandLineTool.stage()` adds to it.

    Args:
        listing (Any): `listing`: `Dirent` objects, `File` or `Directory` values and
            expressions, or one expression that gives the list.
        extra (dict[str, Any]): Further fields, written as given.
    """

    listing: Any


@dataclass(eq=False)
class EnvVarRequirement(_RequirementSpec):
    """An `EnvVarRequirement`: environment variables. `CommandLineTool.env_var()` adds to one.

    Args:
        env_def (Any): `envDef`, each an `EnvironmentDef` or its mapping.
        extra (dict[str, Any]): Further fields, written as given.
    """

    env_def: Any


@dataclass(eq=False)
class ResourceRequirement(_RequirementSpec):
    """A `ResourceRequirement`: cores, memory and disk. `CommandLineTool.resources()` writes one.

    Args:
        cores_min (Any): `coresMin`.
        cores_max (Any): `coresMax`.
        ram_min (Any): `ramMin`, in mebibytes.
        ram_max (Any): `ramMax`, in mebibytes.
        tmpdir_min (Any): `tmpdirMin`, in mebibytes.
        tmpdir_max (Any): `tmpdirMax`, in mebibytes.
        outdir_min (Any): `outdirMin`, in mebibytes.
        outdir_max (Any): `outdirMax`, in mebibytes.
        extra (dict[str, Any]): Further fields, written as given.
    """

    cores_min: Any = None
    cores_max: Any = None
    ram_min: Any = None
    ram_max: Any = None
    tmpdir_min: Any = None
    tmpdir_max: Any = None
    outdir_min: Any = None
    outdir_max: Any = None


@dataclass(eq=False)
class NetworkAccess(_RequirementSpec):
    """A `NetworkAccess`: whether the tool may reach the network. `CommandLineTool.network_access()` writes one.

    Args:
        network_access (Any): `networkAccess`, or an expression for it.
        extra (dict[str, Any]): Further fields, written as given.
    """

    network_access: Any


@dataclass(eq=False)
class WorkReuse(_RequirementSpec):
    """A `WorkReuse`: whether an earlier run's results may be reused. `CommandLineTool.work_reuse()` writes one.

    Args:
        enable_reuse (Any): `enableReuse`, or an expression for it.
        extra (dict[str, Any]): Further fields, written as given.
    """

    enable_reuse: Any


@dataclass(eq=False)
class InplaceUpdateRequirement(_RequirementSpec):
    """An `InplaceUpdateRequirement`: change writable staged inputs in place.

    `CommandLineTool.inplace_update()` writes one.

    Args:
        inplace_update (Any): `inplaceUpdate`.
        extra (dict[str, Any]): Further fields, written as given.
    """

    inplace_update: Any = True


@dataclass(eq=False)
class ToolTimeLimit(_RequirementSpec):
    """A `ToolTimeLimit`: the seconds the tool may run. `CommandLineTool.time_limit()` writes one.

    Args:
        timelimit (Any): `timelimit`, in seconds, or an expression; `0` means no limit.
        extra (dict[str, Any]): Further fields, written as given.
    """

    timelimit: Any


class _ParameterSpec:
    """A CWL parameter under construction: what `Field`, `Input` and `Output` share.

    The subclasses are frozen dataclasses. A facet such as `label` is given to the constructor by keyword or set
    by the method of the same name, which returns a changed copy.
    """

    _name_context: ClassVar[str]
    _plural: ClassVar[str]
    type_: Any
    name: str | None
    extra: dict[str, Any]

    def _assign(self, type_: Any, name: str | None, extra: dict[str, Any] | None, **attributes: Any) -> None:
        """Set the attributes of a new parameter: the arguments the subclasses share, checked, then theirs."""
        attributes |= {
            "type_": type_,
            "name": name if name is None else validate_python_identifier_name(name, context=self._name_context),
            "extra": dict(extra or {}),
        }
        for attribute, value in attributes.items():
            object.__setattr__(self, attribute, value)

    def _replace(self, **attributes: Any) -> Self:
        clone = copy.copy(self)
        for attribute, value in attributes.items():
            object.__setattr__(clone, attribute, value)
        return clone

    def _saved(self, cwl_class: type, after: dict[str, Any], **fields: Any) -> dict[str, Any]:
        """The parameter as `cwl_class` saves these fields, then the fields `after` as given and `extra` rendered."""
        if self.name is None:
            raise ValueError(f"{self._plural} must have a name before serialization")
        saved: dict[str, Any] = cwl_class(**fields | {"doc": _render_doc(fields["doc"])}).save()
        saved.update(after | _render(self.extra))
        return saved

    @classmethod
    def array(cls: Any, items: Any, **kwargs: Any) -> Any:
        """Make an input, output or field whose type is an array: `Input.array(cwl.file, position=1)`.

        Args:
            items (Any): The type of each element.
            kwargs (Any): The other constructor arguments.

        Returns:
            Any: The new object, of this class.
        """
        return cls(cwl.array(items), **kwargs)

    @classmethod
    def enum(cls: Any, *symbols: str, name: str | None = None, **kwargs: Any) -> Any:
        """Make an input, output or field whose type is an enum: `Input.enum("fast", "slow", flag="--mode")`.

        Args:
            symbols (str): The allowed values.
            name (str | None): The enum type's `name`.
            kwargs (Any): The other constructor arguments.

        Returns:
            Any: The new object, of this class.
        """
        return cls(cwl.enum(*symbols, name=name), **kwargs)

    @classmethod
    def record(
        cls: Any,
        fields: Mapping[str, "FieldSpec"] | list[Any],
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Make an input, output or field whose type is a record, as `cwl.record()` builds it.

        Args:
            fields (Mapping[str, FieldSpec] | list[Any]): The record's fields: a `Fields(...)`
                collection or a mapping of names to `Field` objects, or a list of named fields.
            name (str | None): The record type's `name`.
            kwargs (Any): The other constructor arguments.

        Returns:
            Any: The new object, of this class.
        """
        return cls(cwl.record(fields, name=name), **kwargs)

    def named(self, name: str) -> Self:
        """Return a copy with its name set; `Inputs`, `Outputs` and `Fields` call it.

        Args:
            name (str): The `id` (or a field's `name`), a Python identifier.

        Returns:
            Self: The copy.

        Raises:
            ValueError: If `name` is not a Python identifier.
        """
        return self._replace(name=validate_python_identifier_name(name, context=self._name_context))

    def label(self, text: str) -> Self:
        """Return a copy with its `label` set, a short title.

        Args:
            text (str): The `label`.

        Returns:
            Self: The copy.
        """
        return self._replace(label_text=text)

    def doc(self, text: str | list[str]) -> Self:
        """Return a copy with its `doc` set, a longer description.

        Args:
            text (str | list[str]): The `doc`; a list is written as a list of lines.

        Returns:
            Self: The copy.
        """
        return self._replace(doc_text=text)


class _DefaultSpecMixin(_ParameterSpec):
    default_value: Any

    def default(self, value: Any) -> Self:
        """Return a copy with its `default` set, the value used when none is given.

        Args:
            value (Any): The `default`; for a `File`, a mapping such as
                `{"class": "File", "location": "reads.fastq"}`.

        Returns:
            Self: The copy.
        """
        return self._replace(default_value=value)

    def _default_entry(self) -> dict[str, Any]:
        return {} if self.default_value is _UNSET else {"default": _render(self.default_value)}


class _IOFacetMixin(_ParameterSpec):
    required: bool
    label_text: str | None
    doc_text: str | list[str] | None
    format_value: Any
    secondary_files_value: Any
    streamable_value: bool | None
    load_contents_value: bool | None
    load_listing_value: str | None

    def _facets(self) -> dict[str, Any]:
        """The facets both an input and an output hold, as `cwl_utils` takes them."""
        return {
            "label": self.label_text,
            "doc": self.doc_text,
            "format": _render(self.format_value),
            "secondaryFiles": _render(self.secondary_files_value),
            "streamable": self.streamable_value,
        }

    def _type(self) -> Any:
        """The declared type, made optional unless the parameter is required."""
        return _canonicalize_type(self.type_) if self.required else cwl.optional(self.type_)

    def format(self, value: Any) -> Self:
        """Return a copy with its `format` set, the file format as an ontology term.

        Args:
            value (Any): The `format`, such as `edam:format_2572`; the prefix must be
                declared, for EDAM with `CommandLineTool.edam()`.

        Returns:
            Self: The copy.
        """
        return self._replace(format_value=value)

    def secondary_files(self, *values: Any) -> Self:
        """Return a copy with its `secondaryFiles` set, the files that travel with it.

        Args:
            values (Any): The entries: `secondary_file(...)` objects, patterns such as
                `.bai`, or mappings.

        Returns:
            Self: The copy.
        """
        return self._replace(secondary_files_value=list(values))

    def streamable(self, value: bool) -> Self:
        """Return a copy with `streamable` set: the file may be read or written as a stream.

        Args:
            value (bool): `streamable`.

        Returns:
            Self: The copy.
        """
        return self._replace(streamable_value=value)

    def load_contents(self, value: bool) -> Self:
        """Return a copy with `loadContents` set: read the first 64 KiB of the file into `contents`.

        On an output it is written under `outputBinding`, for `output_eval`.

        Args:
            value (bool): `loadContents`.

        Returns:
            Self: The copy.
        """
        return self._replace(load_contents_value=value)

    def load_listing(self, value: str) -> Self:
        """Return a copy with `loadListing` set, how deep a `Directory` is listed.

        On an output it is written on the output parameter, where CWL v1.2 does not
        define it, so `validate()` rejects the tool.

        Args:
            value (str): `no_listing`, `shallow_listing` or `deep_listing`.

        Returns:
            Self: The copy.
        """
        return self._replace(load_listing_value=value)


@dataclass(frozen=True, slots=True, init=False)
class FieldSpec(_DefaultSpecMixin):
    """A field of a record type, written as one entry of its `fields`. `Field` is this class.

    `Field(cwl.int)` makes a field; the keyword it is given to in `Fields(...)`
    or `cwl.record({...})` is its `name`.
    """

    _name_context: ClassVar[str] = "record field name"
    _plural: ClassVar[str] = "Record fields"
    type_: Any
    name: str | None = None
    label_text: str | None = None
    doc_text: str | list[str] | None = None
    default_value: Any = _UNSET
    extra: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        type_: Any,
        *,
        name: str | None = None,
        label: str | None = None,
        doc: str | list[str] | None = None,
        default: Any = _UNSET,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Make a record field.

        Args:
            type_ (Any): The field's `type`, such as `cwl.int` or `"File?"`.
            name (str | None): The field's `name`, when it is not given by a keyword.
            label (str | None): `label`, a short title.
            doc (str | list[str] | None): `doc`, a longer description.
            default (Any): `default`, the value used when none is given.
            extra (dict[str, Any] | None): Further fields, written as given.

        Raises:
            ValueError: If `name` is not a Python identifier.
        """
        self._assign(type_, name, extra, label_text=label, doc_text=doc, default_value=default)

    def to_dict(self) -> dict[str, Any]:
        """Render the field as one entry of a record's `fields`.

        Returns:
            dict[str, Any]: The entry.

        Raises:
            ValueError: If the field has no name.
        """
        return self._saved(
            _cwl_v12.CommandInputRecordField,
            self._default_entry(),
            name=self.name,
            type_=_canonicalize_type(self.type_),
            label=self.label_text,
            doc=self.doc_text,
        )


@dataclass(frozen=True, slots=True, init=False)
class InputSpec(_DefaultSpecMixin, _IOFacetMixin):
    """A tool input, written as one entry of the tool's `inputs`. `Input` is this class.

    `Input(cwl.file, position=1)` is a file given as the first positional
    argument; `Input(cwl.int, flag="--threads", required=False)` an optional
    flag. The keyword it is given to in `Inputs(...)` is its `id`. Each method
    returns a changed copy, so the calls chain.
    """

    _name_context: ClassVar[str] = "input name"
    _plural: ClassVar[str] = "Inputs"
    type_: Any
    position: int | float | None = None
    flag: str | None = None
    required: bool = True
    separate: bool | None = None
    item_separator: str | None = None
    binding_value_from: Any = None
    shell_quote: bool | None = None
    label_text: str | None = None
    doc_text: str | list[str] | None = None
    format_value: Any = None
    secondary_files_value: Any = None
    streamable_value: bool | None = None
    load_contents_value: bool | None = None
    load_listing_value: str | None = None
    default_value: Any = _UNSET
    binding_extra: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    name: str | None = None

    def __init__(
        self,
        type_: Any,
        *,
        position: int | float | None = None,
        flag: str | None = None,
        required: bool = True,
        separate: bool | None = None,
        item_separator: str | None = None,
        value_from: Any = None,
        shell_quote: bool | None = None,
        label: str | None = None,
        doc: str | list[str] | None = None,
        format: Any = None,
        secondary_files: Any = None,
        streamable: bool | None = None,
        load_contents: bool | None = None,
        load_listing: str | None = None,
        default: Any = _UNSET,
        binding_extra: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> None:
        """Make a tool input.

        `position`, `flag`, `separate`, `item_separator`, `value_from`,
        `shell_quote` and `binding_extra` are written under `inputBinding`;
        without any of them the input is not put on the command line, and the
        tool reads it through expressions or staging.

        Args:
            type_ (Any): The input's `type`, such as `cwl.file`, `cwl.array(cwl.string)`
                or `"int?"`.
            position (int | float | None): `inputBinding.position`, the sort key among
                inputs and arguments.
            flag (str | None): `inputBinding.prefix`, such as `--threads`.
            required (bool): `False` makes the type optional, `["null", type]`.
            separate (bool | None): `inputBinding.separate`; `False` joins the flag and the
                value into one word.
            item_separator (str | None): `inputBinding.itemSeparator`, to join an array
                into one word.
            value_from (Any): `inputBinding.valueFrom`, an expression over the value (`self`)
                whose result is put on the command line instead.
            shell_quote (bool | None): `inputBinding.shellQuote`; `False` needs
                `CommandLineTool.shell_command()`.
            label (str | None): `label`, a short title.
            doc (str | list[str] | None): `doc`, a longer description.
            format (Any): `format`, the file format as an ontology term, such as
                `edam:format_2572`.
            secondary_files (Any): `secondaryFiles`, the files that travel with this one.
            streamable (bool | None): `streamable`.
            load_contents (bool | None): `loadContents`: read the first 64 KiB of the file
                into `contents`.
            load_listing (str | None): `loadListing`, how deep a `Directory` is listed.
            default (Any): `default`, the value used when none is given.
            binding_extra (dict[str, Any] | None): Further `inputBinding` fields, written as given.
            extra (dict[str, Any] | None): Further fields of the input, written as given.
            name (str | None): The input's `id`, when it is not given by a keyword.

        Raises:
            ValueError: If `name` is not a Python identifier.
        """
        self._assign(
            type_,
            name,
            extra,
            position=position,
            flag=flag,
            required=required,
            separate=separate,
            item_separator=item_separator,
            binding_value_from=value_from,
            shell_quote=shell_quote,
            label_text=label,
            doc_text=doc,
            format_value=format,
            secondary_files_value=secondary_files,
            streamable_value=streamable,
            load_contents_value=load_contents,
            load_listing_value=load_listing,
            default_value=default,
            binding_extra=dict(binding_extra or {}),
        )

    def value_from(self, expression: Any) -> "InputSpec":
        """Return a copy with `inputBinding.valueFrom` set.

        Args:
            expression (Any): An expression over the value (`self`) whose result is put
                on the command line instead.

        Returns:
            InputSpec: The copy.
        """
        return self._replace(binding_value_from=expression)

    def to_dict(self) -> dict[str, Any]:
        """Render the input as one entry of the tool's `inputs`.

        Returns:
            dict[str, Any]: The entry.

        Raises:
            ValueError: If the input has no name.
        """
        binding = CommandLineBinding(
            position=self.position,
            prefix=self.flag,
            separate=self.separate,
            item_separator=self.item_separator,
            value_from=self.binding_value_from,
            shell_quote=self.shell_quote,
            extra=dict(self.binding_extra),
        )
        return self._saved(
            _cwl_v12.CommandInputParameter,
            self._default_entry(),
            id=self.name,
            type_=self._type(),
            loadContents=self.load_contents_value,
            loadListing=self.load_listing_value,
            inputBinding=binding.to_dict() or None,
            **self._facets(),
        )


@dataclass(frozen=True, slots=True, init=False)
class OutputSpec(_IOFacetMixin):
    """A tool output, written as one entry of the tool's `outputs`. `Output` is this class.

    `Output(cwl.file, glob="*.csv")` is the file the tool writes;
    `Output.stdout()` is its standard output. The keyword it is given to in
    `Outputs(...)` is its `id`. Each method returns a changed copy, so the
    calls chain.
    """

    _name_context: ClassVar[str] = "output name"
    _plural: ClassVar[str] = "Outputs"
    type_: Any
    required: bool = True
    glob: Any = None
    load_contents_value: bool | None = None
    output_eval: str | None = None
    label_text: str | None = None
    doc_text: str | list[str] | None = None
    format_value: Any = None
    secondary_files_value: Any = None
    streamable_value: bool | None = None
    load_listing_value: str | None = None
    binding_extra: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    name: str | None = None

    def __init__(
        self,
        type_: Any,
        *,
        glob: Any = None,
        required: bool = True,
        load_contents: bool | None = None,
        output_eval: str | None = None,
        label: str | None = None,
        doc: str | list[str] | None = None,
        format: Any = None,
        secondary_files: Any = None,
        streamable: bool | None = None,
        load_listing: str | None = None,
        binding_extra: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> None:
        """Make a tool output.

        `glob`, `load_contents` and `output_eval` are written under `outputBinding`.

        Args:
            type_ (Any): The output's `type`, such as `cwl.file`, `cwl.array(cwl.file)`
                or `cwl.int`.
            glob (Any): `outputBinding.glob`, the file name pattern, or an expression such
                as `$(inputs.out.basename)`, matched in the output directory.
            required (bool): `False` makes the type optional, `["null", type]`, so the
                run does not fail when nothing matches.
            load_contents (bool | None): `outputBinding.loadContents`: read the first
                64 KiB of each match into `contents`, for `output_eval`.
            output_eval (str | None): `outputBinding.outputEval`, an expression over the
                matches (`self`) that gives the output's value.
            label (str | None): `label`, a short title.
            doc (str | list[str] | None): `doc`, a longer description.
            format (Any): `format`, the file format as an ontology term.
            secondary_files (Any): `secondaryFiles`, the files collected with this one.
            streamable (bool | None): `streamable`.
            load_listing (str | None): `loadListing`, written on the output parameter,
                where CWL v1.2 does not define it, so `validate()` rejects the tool.
            binding_extra (dict[str, Any] | None): Further `outputBinding` fields, such as
                `loadListing`, written as given.
            extra (dict[str, Any] | None): Further fields of the output, written as given.
            name (str | None): The output's `id`, when it is not given by a keyword.

        Raises:
            ValueError: If `name` is not a Python identifier.
        """
        self._assign(
            type_,
            name,
            extra,
            glob=glob,
            required=required,
            load_contents_value=load_contents,
            output_eval=output_eval,
            label_text=label,
            doc_text=doc,
            format_value=format,
            secondary_files_value=secondary_files,
            streamable_value=streamable,
            load_listing_value=load_listing,
            binding_extra=dict(binding_extra or {}),
        )

    @classmethod
    def stdout(cls, **kwargs: Any) -> "OutputSpec":
        """Make the output that is the tool's standard output: `type: stdout`.

        Name the file with `CommandLineTool.stdout()`; the output is a `File`.

        Args:
            kwargs (Any): The other constructor arguments, such as `label`.

        Returns:
            OutputSpec: The output.
        """
        return cls("stdout", **kwargs)

    @classmethod
    def stderr(cls, **kwargs: Any) -> "OutputSpec":
        """Make the output that is the tool's standard error: `type: stderr`.

        Name the file with `CommandLineTool.stderr()`; the output is a `File`.

        Args:
            kwargs (Any): The other constructor arguments, such as `label`.

        Returns:
            OutputSpec: The output.
        """
        return cls("stderr", **kwargs)

    def to_dict(self) -> dict[str, Any]:
        """Render the output as one entry of the tool's `outputs`.

        Returns:
            dict[str, Any]: The entry.

        Raises:
            ValueError: If the output has no name.
        """
        binding = CommandOutputBinding(
            glob=self.glob,
            load_contents=self.load_contents_value,
            output_eval=self.output_eval,
            extra=dict(self.binding_extra),
        )
        # loadListing is not part of CommandOutputParameter in the CWL v1.2 schema; kept as a builder extension for
        # symmetry with InputSpec.
        return self._saved(
            _cwl_v12.CommandOutputParameter,
            {} if self.load_listing_value is None else {"loadListing": self.load_listing_value},
            id=self.name,
            type_=self._type(),
            outputBinding=binding.to_dict() or None,
            **self._facets(),
        )
