"""Private dataclasses for the Tool Builder."""

# pylint: disable=missing-function-docstring,too-few-public-methods
# pylint: disable=too-many-instance-attributes,too-many-arguments
# pylint: disable=too-many-locals,redefined-builtin,too-many-lines
# These frozen dataclasses mirror the CWL schema closely, so field-rich
# constructors and small fluent helpers are intentional rather than accidental.

from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Any, Callable, ClassVar, Mapping, NamedTuple, TypeVar, cast

from ._tool_builder_support import (
    _SUPPORT,
    _apply_required,
    _basename_expression,
    _canonicalize_type,
    _input_expression,
    _merge_if_set,
    _named_parameter,
    _optional_binding,
    _record_type_payload,
    _render,
    _render_doc,
    _validate_api_name,
)


FrozenSpecT = TypeVar("FrozenSpecT")


class _CWLField(NamedTuple):
    name: str
    cwl_name: str
    default: Any = _SUPPORT.unset
    render: Callable[[Any], Any] = _render
    omit_empty: bool = False


def _render_sequence(values: list[Any]) -> list[Any]:
    return [_render(value) for value in values]


def _canonicalize_sequence(values: list[Any]) -> list[Any]:
    return [_canonicalize_type(value) for value in values]


def _render_dataclass_cwl(obj: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for item in dataclass_fields(cast(Any, obj)):
        cwl_key = item.metadata.get("cwl")
        value = getattr(obj, item.name)
        if cwl_key is None or value is None or value is _SUPPORT.unset:
            continue
        payload[str(cwl_key)] = item.metadata.get("render", _render)(value)
    if extra := getattr(obj, "extra", None):
        payload.update(_render(extra))
    return payload


def _replace_frozen(obj: FrozenSpecT, **changes: Any) -> FrozenSpecT:
    """Copy a frozen dataclass-like object while overriding selected fields."""
    clone = object.__new__(obj.__class__)
    values = {
        item.name: getattr(obj, item.name)
        for item in dataclass_fields(cast(Any, obj))
    }
    values.update(changes)
    for name, value in values.items():
        object.__setattr__(clone, name, value)
    return clone


def _set_frozen_attrs(obj: Any, **values: Any) -> None:
    for name, value in values.items():
        object.__setattr__(obj, name, value)


class _CWLObject:
    _fields: ClassVar[tuple[_CWLField, ...]] = ()

    def __init__(self, *args: Any, extra: dict[str, Any] | None = None, **kwargs: Any) -> None:
        values = list(args)
        if len(values) > len(self._fields):
            if len(values) == len(self._fields) + 1 and extra is None:
                extra = values.pop()
            else:
                raise TypeError(f"{type(self).__name__} accepts at most {len(self._fields)} positional arguments")

        for item, value in zip(self._fields, values):
            if item.name in kwargs:
                raise TypeError(f"{type(self).__name__} got multiple values for {item.name!r}")
            setattr(self, item.name, value)
        for item in self._fields[len(values):]:
            value = kwargs.pop(item.name, item.default)
            if value is _SUPPORT.unset:
                raise TypeError(f"{type(self).__name__} missing required argument: {item.name!r}")
            setattr(self, item.name, value)
        if kwargs:
            unknown = next(iter(kwargs))
            raise TypeError(f"{type(self).__name__} got an unexpected keyword argument {unknown!r}")
        self.extra = dict(extra or {})

    def _render_cwl(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for item in self._fields:
            value = getattr(self, item.name)
            if value is None or value is _SUPPORT.unset or (item.omit_empty and not value):
                continue
            payload[item.cwl_name] = item.render(value)
        if self.extra:
            payload.update(_render(self.extra))
        return payload

    def to_dict(self) -> Any:
        return self._render_cwl()


class SecondaryFile(_CWLObject):
    """A CWL secondary file pattern."""

    _fields = (
        _CWLField("pattern", "pattern"),
        _CWLField("required", "required", None),
    )

    def to_dict(self) -> str | dict[str, Any]:
        match getattr(self, "pattern"), getattr(self, "required"), self.extra:
            case str() as pattern, None, extra if not extra:
                return pattern
        return self._render_cwl()


def secondary_file(pattern: Any, *, required: bool | str | None = None, **extra: Any) -> "SecondaryFile":
    """Create a secondary file specification."""
    return SecondaryFile(pattern=pattern, required=required, extra=dict(extra))


class Dirent(_CWLObject):
    """A CWL InitialWorkDirRequirement listing entry."""

    _fields = (
        _CWLField("entry", "entry"),
        _CWLField("entryname", "entryname", None),
        _CWLField("writable", "writable", None),
    )

    @classmethod
    def from_input(
        cls,
        reference: Any,
        *,
        writable: bool = False,
        entryname: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> "Dirent":
        name = _named_parameter(reference, kind="input")
        return cls(
            entry=_input_expression(name),
            entryname=entryname or _basename_expression(name),
            writable=writable,
            extra=dict(extra or {}),
        )


class EnvironmentDef(_CWLObject):
    """An EnvVarRequirement entry."""

    _fields = (
        _CWLField("env_name", "envName"),
        _CWLField("env_value", "envValue"),
    )

    def to_dict(self) -> dict[str, str]:
        return cast(dict[str, str], self._render_cwl())


class CommandLineBinding(_CWLObject):
    """A CWL input binding or argument binding."""

    _fields = (
        _CWLField("position", "position", None),
        _CWLField("prefix", "prefix", None),
        _CWLField("separate", "separate", None),
        _CWLField("item_separator", "itemSeparator", None),
        _CWLField("value_from", "valueFrom", None),
        _CWLField("shell_quote", "shellQuote", None),
    )


class CommandOutputBinding(_CWLObject):
    """A CWL output binding."""

    _fields = (
        _CWLField("glob", "glob", None),
        _CWLField("load_contents", "loadContents", None),
        _CWLField("output_eval", "outputEval", None),
    )


@dataclass(frozen=True, slots=True)
class CommandArgument:
    """A structured CWL command-line argument."""

    value: Any = None
    binding: CommandLineBinding | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_cwl(self) -> str | dict[str, Any]:
        binding_dict = {} if self.binding is None else self.binding.to_dict()
        if self.value is None and not binding_dict and not self.extra:
            return ""
        match self.value, binding_dict, self.extra:
            case str() as value, binding, extra if not binding and not extra:
                return value
        payload = dict(binding_dict)
        _merge_if_set(payload, "valueFrom", self.value)
        payload.update(_render(self.extra))
        return payload


class _RequirementSpec(_CWLObject):
    class_name: ClassVar[str]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        cls.class_name = cls.__name__

    def to_fields(self) -> dict[str, Any]:
        return self._render_cwl()


class DockerRequirement(_RequirementSpec):
    """DockerRequirement helper."""

    _fields = (
        _CWLField("docker_pull", "dockerPull", None),
        _CWLField("docker_load", "dockerLoad", None),
        _CWLField("docker_file", "dockerFile", None),
        _CWLField("docker_import", "dockerImport", None),
        _CWLField("docker_image_id", "dockerImageId", None),
        _CWLField("docker_output_directory", "dockerOutputDirectory", None),
    )


class InlineJavascriptRequirement(_RequirementSpec):
    """InlineJavascriptRequirement helper."""

    _fields = (_CWLField("expression_lib", "expressionLib", None, _render_sequence, True),)


class SchemaDefRequirement(_RequirementSpec):
    """SchemaDefRequirement helper."""

    _fields = (_CWLField("types", "types", _SUPPORT.unset, _canonicalize_sequence),)


class LoadListingRequirement(_RequirementSpec):
    """LoadListingRequirement helper."""

    _fields = (_CWLField("load_listing", "loadListing"),)


class ShellCommandRequirement(_RequirementSpec):
    """ShellCommandRequirement helper."""


class SoftwarePackage(_CWLObject):
    """A SoftwareRequirement package entry."""

    _fields = (
        _CWLField("package", "package"),
        _CWLField("version", "version", None),
        _CWLField("specs", "specs", None),
    )


class SoftwareRequirement(_RequirementSpec):
    """SoftwareRequirement helper."""

    _fields = (_CWLField("packages", "packages", _SUPPORT.unset, _render_sequence),)


class InitialWorkDirRequirement(_RequirementSpec):
    """InitialWorkDirRequirement helper."""

    _fields = (_CWLField("listing", "listing"),)


class EnvVarRequirement(_RequirementSpec):
    """EnvVarRequirement helper."""

    _fields = (_CWLField("env_def", "envDef", _SUPPORT.unset, _render_sequence),)


class ResourceRequirement(_RequirementSpec):
    """ResourceRequirement helper."""

    _fields = (
        _CWLField("cores_min", "coresMin", None),
        _CWLField("cores_max", "coresMax", None),
        _CWLField("ram_min", "ramMin", None),
        _CWLField("ram_max", "ramMax", None),
        _CWLField("tmpdir_min", "tmpdirMin", None),
        _CWLField("tmpdir_max", "tmpdirMax", None),
        _CWLField("outdir_min", "outdirMin", None),
        _CWLField("outdir_max", "outdirMax", None),
    )


class NetworkAccess(_RequirementSpec):
    """NetworkAccess helper."""

    _fields = (_CWLField("network_access", "networkAccess"),)


class WorkReuse(_RequirementSpec):
    """WorkReuse helper."""

    _fields = (_CWLField("enable_reuse", "enableReuse"),)


class InplaceUpdateRequirement(_RequirementSpec):
    """InplaceUpdateRequirement helper."""

    _fields = (_CWLField("inplace_update", "inplaceUpdate", True),)


class ToolTimeLimit(_RequirementSpec):
    """ToolTimeLimit helper."""

    _fields = (_CWLField("timelimit", "timelimit"),)


class _CommonSpecMixin:
    _name_context: ClassVar[str]

    @classmethod
    def array(cls: Any, items: Any, **kwargs: Any) -> Any:
        return cls({"type": "array", "items": _canonicalize_type(items)}, **kwargs)

    @classmethod
    def enum(cls: Any, *symbols: str, name: str | None = None, **kwargs: Any) -> Any:
        payload: dict[str, Any] = {"type": "enum", "symbols": list(symbols)}
        _merge_if_set(payload, "name", name)
        return cls(payload, **kwargs)

    @classmethod
    def record(
        cls: Any,
        fields: Mapping[str, "FieldSpec"] | list[Any],
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return cls(_record_type_payload(fields, name=name), **kwargs)

    def named(self, name: str) -> Any:
        return _replace_frozen(self, name=_validate_api_name(name, context=self._name_context))

    def label(self, text: str) -> Any:
        return _replace_frozen(self, label_text=text)

    def doc(self, text: str | list[str]) -> Any:
        return _replace_frozen(self, doc_text=text)


class _DefaultSpecMixin:
    def default(self, value: Any) -> Any:
        return _replace_frozen(self, default_value=value)


class _IOFacetMixin:
    def format(self, value: Any) -> Any:
        return _replace_frozen(self, format_value=value)

    def secondary_files(self, *values: Any) -> Any:
        return _replace_frozen(self, secondary_files_value=list(values))

    def streamable(self, value: bool) -> Any:
        return _replace_frozen(self, streamable_value=value)

    def load_contents(self, value: bool) -> Any:
        return _replace_frozen(self, load_contents_value=value)

    def load_listing(self, value: str) -> Any:
        return _replace_frozen(self, load_listing_value=value)


@dataclass(frozen=True, slots=True, init=False)
class FieldSpec(_CommonSpecMixin, _DefaultSpecMixin):
    """A record field definition."""

    _name_context: ClassVar[str] = "record field name"

    type_: Any
    name: str | None = None
    label_text: str | None = field(default=None, metadata={"cwl": "label"})
    doc_text: str | list[str] | None = field(default=None, metadata={"cwl": "doc", "render": _render_doc})
    default_value: Any = field(default=_SUPPORT.unset, metadata={"cwl": "default", "present": True})
    extra: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        type_: Any,
        *,
        name: str | None = None,
        label: str | None = None,
        doc: str | list[str] | None = None,
        default: Any = _SUPPORT.unset,
        extra: dict[str, Any] | None = None,
    ) -> None:
        _set_frozen_attrs(
            self,
            type_=type_,
            name=None if name is None else _validate_api_name(name, context="record field name"),
            label_text=label,
            doc_text=doc,
            default_value=default,
            extra=dict(extra or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        if self.name is None:
            raise ValueError("Record fields must have a name before serialization")
        payload = {"name": self.name, "type": _canonicalize_type(self.type_)}
        payload.update(_render_dataclass_cwl(self))
        return payload


@dataclass(frozen=True, slots=True, init=False)
class InputSpec(_CommonSpecMixin, _DefaultSpecMixin, _IOFacetMixin):
    """A CWL CommandLineTool input."""

    _name_context: ClassVar[str] = "input name"

    type_: Any
    position: int | float | None = None
    flag: str | None = None
    required: bool = True
    separate: bool | None = None
    item_separator: str | None = None
    binding_value_from: Any = None
    shell_quote: bool | None = None
    label_text: str | None = field(default=None, metadata={"cwl": "label"})
    doc_text: str | list[str] | None = field(default=None, metadata={"cwl": "doc", "render": _render_doc})
    format_value: Any = field(default=None, metadata={"cwl": "format"})
    secondary_files_value: Any = field(default=None, metadata={"cwl": "secondaryFiles"})
    streamable_value: bool | None = field(default=None, metadata={"cwl": "streamable"})
    load_contents_value: bool | None = field(default=None, metadata={"cwl": "loadContents"})
    load_listing_value: str | None = field(default=None, metadata={"cwl": "loadListing"})
    default_value: Any = field(default=_SUPPORT.unset, metadata={"cwl": "default", "present": True})
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
        default: Any = _SUPPORT.unset,
        binding_extra: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> None:
        _set_frozen_attrs(
            self,
            type_=type_,
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
            extra=dict(extra or {}),
            name=None if name is None else _validate_api_name(name, context="input name"),
        )

    def value_from(self, expression: Any) -> "InputSpec":
        return _replace_frozen(self, binding_value_from=expression)

    def to_dict(self) -> dict[str, Any]:
        payload = {"type": _apply_required(self.type_, self.required)}
        binding = _optional_binding(
            CommandLineBinding(
                position=self.position,
                prefix=self.flag,
                separate=self.separate,
                item_separator=self.item_separator,
                value_from=self.binding_value_from,
                shell_quote=self.shell_quote,
                extra=dict(self.binding_extra),
            )
        )
        if binding is not None:
            payload["inputBinding"] = binding.to_dict()
        payload.update(_render_dataclass_cwl(self))
        return payload


@dataclass(frozen=True, slots=True, init=False)
class OutputSpec(_CommonSpecMixin, _IOFacetMixin):
    """A CWL CommandLineTool output."""

    _name_context: ClassVar[str] = "output name"

    type_: Any
    required: bool = True
    glob: Any = None
    load_contents_value: bool | None = None
    output_eval: str | None = None
    label_text: str | None = field(default=None, metadata={"cwl": "label"})
    doc_text: str | list[str] | None = field(default=None, metadata={"cwl": "doc", "render": _render_doc})
    format_value: Any = field(default=None, metadata={"cwl": "format"})
    secondary_files_value: Any = field(default=None, metadata={"cwl": "secondaryFiles"})
    streamable_value: bool | None = field(default=None, metadata={"cwl": "streamable"})
    load_listing_value: str | None = field(default=None, metadata={"cwl": "loadListing"})
    binding_extra: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    name: str | None = None

    def __init__(
        self,
        type_: Any,
        *,
        glob: Any = None,
        from_input: Any = None,
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
        if glob is not None and from_input is not None:
            raise ValueError("Specify either glob= or from_input=, not both")
        glob_value = (
            _basename_expression(_named_parameter(from_input, kind="input"))
            if from_input is not None
            else glob
        )
        _set_frozen_attrs(
            self,
            type_=type_,
            required=required,
            glob=glob_value,
            load_contents_value=load_contents,
            output_eval=output_eval,
            label_text=label,
            doc_text=doc,
            format_value=format,
            secondary_files_value=secondary_files,
            streamable_value=streamable,
            load_listing_value=load_listing,
            binding_extra=dict(binding_extra or {}),
            extra=dict(extra or {}),
            name=None if name is None else _validate_api_name(name, context="output name"),
        )

    @classmethod
    def stdout(cls, **kwargs: Any) -> "OutputSpec":
        return cls("stdout", **kwargs)

    @classmethod
    def stderr(cls, **kwargs: Any) -> "OutputSpec":
        return cls("stderr", **kwargs)

    def to_dict(self) -> dict[str, Any]:
        payload = {"type": _apply_required(self.type_, self.required)}
        binding = _optional_binding(
            CommandOutputBinding(
                glob=self.glob,
                load_contents=self.load_contents_value,
                output_eval=self.output_eval,
                extra=dict(self.binding_extra),
            )
        )
        if binding is not None:
            payload["outputBinding"] = binding.to_dict()
        payload.update(_render_dataclass_cwl(self))
        return payload
