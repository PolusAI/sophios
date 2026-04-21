"""Private dataclasses for the CWL builder."""

from __future__ import annotations

# pylint: disable=missing-function-docstring,too-few-public-methods
# pylint: disable=too-many-instance-attributes,too-many-arguments
# pylint: disable=too-many-locals,redefined-builtin,too-many-lines
# These frozen dataclasses mirror the CWL schema closely, so field-rich
# constructors and small fluent helpers are intentional rather than accidental.

from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Any, ClassVar, Mapping, TypeVar, cast

from ._cwl_builder_support import (
    _SUPPORT,
    _apply_required,
    _basename_expression,
    _canonicalize_type,
    _input_expression,
    _merge_if_present,
    _merge_if_set,
    _named_parameter,
    _optional_binding,
    _record_type_payload,
    _render,
    _render_doc,
)


FrozenSpecT = TypeVar("FrozenSpecT")


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


@dataclass(frozen=True, slots=True)
class SecondaryFile:
    """A CWL secondary file pattern."""

    pattern: Any
    required: bool | str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> str | dict[str, Any]:
        if self.required is None and not self.extra and isinstance(self.pattern, str):
            return self.pattern
        payload = {"pattern": _render(self.pattern)}
        _merge_if_set(payload, "required", self.required)
        payload.update(_render(self.extra))
        return payload


def secondary_file(pattern: Any, *, required: bool | str | None = None, **extra: Any) -> SecondaryFile:
    """Create a secondary file specification."""
    return SecondaryFile(pattern=pattern, required=required, extra=dict(extra))


@dataclass(frozen=True, slots=True)
class Dirent:
    """A CWL InitialWorkDirRequirement listing entry."""

    entry: Any
    entryname: str | None = None
    writable: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {"entry": _render(self.entry)}
        _merge_if_set(payload, "entryname", self.entryname)
        _merge_if_set(payload, "writable", self.writable)
        payload.update(_render(self.extra))
        return payload

    @classmethod
    def from_input(
        cls,
        reference: Any,
        *,
        writable: bool = False,
        entryname: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Dirent:
        name = _named_parameter(reference, kind="input")
        return cls(
            entry=_input_expression(name),
            entryname=entryname or _basename_expression(name),
            writable=writable,
            extra=dict(extra or {}),
        )


@dataclass(frozen=True, slots=True)
class EnvironmentDef:
    """An EnvVarRequirement entry."""

    env_name: str
    env_value: str

    def to_dict(self) -> dict[str, str]:
        return {"envName": self.env_name, "envValue": self.env_value}


@dataclass(frozen=True, slots=True)
class CommandLineBinding:
    """A CWL input binding or argument binding."""

    position: int | float | None = None
    prefix: str | None = None
    separate: bool | None = None
    item_separator: str | None = None
    value_from: Any = None
    shell_quote: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        _merge_if_set(payload, "position", self.position)
        _merge_if_set(payload, "prefix", self.prefix)
        _merge_if_set(payload, "separate", self.separate)
        _merge_if_set(payload, "itemSeparator", self.item_separator)
        _merge_if_set(payload, "valueFrom", self.value_from)
        _merge_if_set(payload, "shellQuote", self.shell_quote)
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class CommandOutputBinding:
    """A CWL output binding."""

    glob: Any = None
    load_contents: bool | None = None
    output_eval: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        _merge_if_set(payload, "glob", self.glob)
        _merge_if_set(payload, "loadContents", self.load_contents)
        _merge_if_set(payload, "outputEval", self.output_eval)
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class CommandArgument:
    """A structured CWL command-line argument."""

    value: Any = None
    binding: CommandLineBinding | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_yaml(self) -> str | dict[str, Any]:
        binding_dict = {} if self.binding is None else self.binding.to_dict()
        if self.value is None and not binding_dict and not self.extra:
            return ""
        if self.value is not None and not binding_dict and not self.extra and isinstance(self.value, str):
            return str(self.value)
        payload = dict(binding_dict)
        _merge_if_set(payload, "valueFrom", self.value)
        payload.update(_render(self.extra))
        return payload


class _RequirementSpec:
    class_name: ClassVar[str]

    def to_fields(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class DockerRequirement(_RequirementSpec):
    """DockerRequirement helper."""

    docker_pull: str | None = None
    docker_load: str | None = None
    docker_file: str | dict[str, Any] | None = None
    docker_import: str | None = None
    docker_image_id: str | None = None
    docker_output_directory: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "DockerRequirement"

    def to_fields(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        _merge_if_set(payload, "dockerPull", self.docker_pull)
        _merge_if_set(payload, "dockerLoad", self.docker_load)
        _merge_if_set(payload, "dockerFile", self.docker_file)
        _merge_if_set(payload, "dockerImport", self.docker_import)
        _merge_if_set(payload, "dockerImageId", self.docker_image_id)
        _merge_if_set(payload, "dockerOutputDirectory", self.docker_output_directory)
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class InlineJavascriptRequirement(_RequirementSpec):
    """InlineJavascriptRequirement helper."""

    expression_lib: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "InlineJavascriptRequirement"

    def to_fields(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.expression_lib:
            payload["expressionLib"] = list(self.expression_lib)
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class SchemaDefRequirement(_RequirementSpec):
    """SchemaDefRequirement helper."""

    types: list[Any]
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "SchemaDefRequirement"

    def to_fields(self) -> dict[str, Any]:
        payload = {"types": [_canonicalize_type(type_) for type_ in self.types]}
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class LoadListingRequirement(_RequirementSpec):
    """LoadListingRequirement helper."""

    load_listing: str
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "LoadListingRequirement"

    def to_fields(self) -> dict[str, Any]:
        payload = {"loadListing": self.load_listing}
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class ShellCommandRequirement(_RequirementSpec):
    """ShellCommandRequirement helper."""

    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "ShellCommandRequirement"

    def to_fields(self) -> dict[str, Any]:
        return {key: _render(value) for key, value in self.extra.items()}


@dataclass(frozen=True, slots=True)
class SoftwarePackage:
    """A SoftwareRequirement package entry."""

    package: str
    version: list[str] | None = None
    specs: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {"package": self.package}
        _merge_if_set(payload, "version", self.version)
        _merge_if_set(payload, "specs", self.specs)
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class SoftwareRequirement(_RequirementSpec):
    """SoftwareRequirement helper."""

    packages: list[SoftwarePackage | dict[str, Any]]
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "SoftwareRequirement"

    def to_fields(self) -> dict[str, Any]:
        payload = {"packages": [_render(package) for package in self.packages]}
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class InitialWorkDirRequirement(_RequirementSpec):
    """InitialWorkDirRequirement helper."""

    listing: Any
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "InitialWorkDirRequirement"

    def to_fields(self) -> dict[str, Any]:
        payload = {"listing": _render(self.listing)}
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class EnvVarRequirement(_RequirementSpec):
    """EnvVarRequirement helper."""

    env_def: list[EnvironmentDef | dict[str, Any]]
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "EnvVarRequirement"

    def to_fields(self) -> dict[str, Any]:
        payload = {"envDef": [_render(item) for item in self.env_def]}
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class ResourceRequirement(_RequirementSpec):
    """ResourceRequirement helper."""

    cores_min: int | float | str | None = None
    cores_max: int | float | str | None = None
    ram_min: int | float | str | None = None
    ram_max: int | float | str | None = None
    tmpdir_min: int | float | str | None = None
    tmpdir_max: int | float | str | None = None
    outdir_min: int | float | str | None = None
    outdir_max: int | float | str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "ResourceRequirement"

    def to_fields(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        _merge_if_set(payload, "coresMin", self.cores_min)
        _merge_if_set(payload, "coresMax", self.cores_max)
        _merge_if_set(payload, "ramMin", self.ram_min)
        _merge_if_set(payload, "ramMax", self.ram_max)
        _merge_if_set(payload, "tmpdirMin", self.tmpdir_min)
        _merge_if_set(payload, "tmpdirMax", self.tmpdir_max)
        _merge_if_set(payload, "outdirMin", self.outdir_min)
        _merge_if_set(payload, "outdirMax", self.outdir_max)
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class NetworkAccess(_RequirementSpec):
    """NetworkAccess helper."""

    network_access: bool | str
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "NetworkAccess"

    def to_fields(self) -> dict[str, Any]:
        payload = {"networkAccess": self.network_access}
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class WorkReuse(_RequirementSpec):
    """WorkReuse helper."""

    enable_reuse: bool | str
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "WorkReuse"

    def to_fields(self) -> dict[str, Any]:
        payload = {"enableReuse": self.enable_reuse}
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class InplaceUpdateRequirement(_RequirementSpec):
    """InplaceUpdateRequirement helper."""

    inplace_update: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "InplaceUpdateRequirement"

    def to_fields(self) -> dict[str, Any]:
        payload = {"inplaceUpdate": self.inplace_update}
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True)
class ToolTimeLimit(_RequirementSpec):
    """ToolTimeLimit helper."""

    timelimit: int | str
    extra: dict[str, Any] = field(default_factory=dict)

    class_name: ClassVar[str] = "ToolTimeLimit"

    def to_fields(self) -> dict[str, Any]:
        payload = {"timelimit": self.timelimit}
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True, init=False)
class FieldSpec:
    """A record field definition."""

    type_: Any
    name: str | None = None
    label_text: str | None = None
    doc_text: str | list[str] | None = None
    default_value: Any = _SUPPORT.unset
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
        object.__setattr__(self, "type_", type_)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "label_text", label)
        object.__setattr__(self, "doc_text", doc)
        object.__setattr__(self, "default_value", default)
        object.__setattr__(self, "extra", dict(extra or {}))

    @classmethod
    def array(cls, items: Any, **kwargs: Any) -> FieldSpec:
        return cls({"type": "array", "items": _canonicalize_type(items)}, **kwargs)

    @classmethod
    def enum(cls, *symbols: str, name: str | None = None, **kwargs: Any) -> FieldSpec:
        payload: dict[str, Any] = {"type": "enum", "symbols": list(symbols)}
        _merge_if_set(payload, "name", name)
        return cls(payload, **kwargs)

    @classmethod
    def record(
        cls,
        fields: Mapping[str, FieldSpec] | list[Any],
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> FieldSpec:
        return cls(_record_type_payload(fields, name=name), **kwargs)

    def named(self, name: str) -> FieldSpec:
        return _replace_frozen(self, name=name)

    def label(self, text: str) -> FieldSpec:
        return _replace_frozen(self, label_text=text)

    def doc(self, text: str | list[str]) -> FieldSpec:
        return _replace_frozen(self, doc_text=text)

    def default(self, value: Any) -> FieldSpec:
        return _replace_frozen(self, default_value=value)

    def to_dict(self) -> dict[str, Any]:
        if self.name is None:
            raise ValueError("Record fields must have a name before serialization")
        payload = {"name": self.name, "type": _canonicalize_type(self.type_)}
        _merge_if_set(payload, "label", self.label_text)
        _merge_if_set(payload, "doc", _render_doc(self.doc_text))
        _merge_if_present(payload, "default", self.default_value)
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True, init=False)
class InputSpec:
    """A CWL CommandLineTool input."""

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
    default_value: Any = _SUPPORT.unset
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
        object.__setattr__(self, "type_", type_)
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "flag", flag)
        object.__setattr__(self, "required", required)
        object.__setattr__(self, "separate", separate)
        object.__setattr__(self, "item_separator", item_separator)
        object.__setattr__(self, "binding_value_from", value_from)
        object.__setattr__(self, "shell_quote", shell_quote)
        object.__setattr__(self, "label_text", label)
        object.__setattr__(self, "doc_text", doc)
        object.__setattr__(self, "format_value", format)
        object.__setattr__(self, "secondary_files_value", secondary_files)
        object.__setattr__(self, "streamable_value", streamable)
        object.__setattr__(self, "load_contents_value", load_contents)
        object.__setattr__(self, "load_listing_value", load_listing)
        object.__setattr__(self, "default_value", default)
        object.__setattr__(self, "binding_extra", dict(binding_extra or {}))
        object.__setattr__(self, "extra", dict(extra or {}))
        object.__setattr__(self, "name", name)

    @classmethod
    def array(cls, items: Any, **kwargs: Any) -> InputSpec:
        return cls({"type": "array", "items": _canonicalize_type(items)}, **kwargs)

    @classmethod
    def enum(cls, *symbols: str, name: str | None = None, **kwargs: Any) -> InputSpec:
        payload: dict[str, Any] = {"type": "enum", "symbols": list(symbols)}
        _merge_if_set(payload, "name", name)
        return cls(payload, **kwargs)

    @classmethod
    def record(
        cls,
        fields: Mapping[str, FieldSpec] | list[Any],
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> InputSpec:
        return cls(_record_type_payload(fields, name=name), **kwargs)

    def named(self, name: str) -> InputSpec:
        return _replace_frozen(self, name=name)

    def label(self, text: str) -> InputSpec:
        return _replace_frozen(self, label_text=text)

    def doc(self, text: str | list[str]) -> InputSpec:
        return _replace_frozen(self, doc_text=text)

    def default(self, value: Any) -> InputSpec:
        return _replace_frozen(self, default_value=value)

    def format(self, value: Any) -> InputSpec:
        return _replace_frozen(self, format_value=value)

    def secondary_files(self, *values: Any) -> InputSpec:
        return _replace_frozen(self, secondary_files_value=list(values))

    def streamable(self, value: bool) -> InputSpec:
        return _replace_frozen(self, streamable_value=value)

    def load_contents(self, value: bool) -> InputSpec:
        return _replace_frozen(self, load_contents_value=value)

    def load_listing(self, value: str) -> InputSpec:
        return _replace_frozen(self, load_listing_value=value)

    def value_from(self, expression: Any) -> InputSpec:
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
        _merge_if_set(payload, "label", self.label_text)
        _merge_if_set(payload, "doc", _render_doc(self.doc_text))
        _merge_if_set(payload, "format", self.format_value)
        _merge_if_set(payload, "secondaryFiles", self.secondary_files_value)
        _merge_if_set(payload, "streamable", self.streamable_value)
        _merge_if_set(payload, "loadContents", self.load_contents_value)
        _merge_if_set(payload, "loadListing", self.load_listing_value)
        _merge_if_present(payload, "default", self.default_value)
        payload.update(_render(self.extra))
        return payload


@dataclass(frozen=True, slots=True, init=False)
class OutputSpec:
    """A CWL CommandLineTool output."""

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
        object.__setattr__(self, "type_", type_)
        object.__setattr__(self, "required", required)
        object.__setattr__(self, "glob", glob_value)
        object.__setattr__(self, "load_contents_value", load_contents)
        object.__setattr__(self, "output_eval", output_eval)
        object.__setattr__(self, "label_text", label)
        object.__setattr__(self, "doc_text", doc)
        object.__setattr__(self, "format_value", format)
        object.__setattr__(self, "secondary_files_value", secondary_files)
        object.__setattr__(self, "streamable_value", streamable)
        object.__setattr__(self, "load_listing_value", load_listing)
        object.__setattr__(self, "binding_extra", dict(binding_extra or {}))
        object.__setattr__(self, "extra", dict(extra or {}))
        object.__setattr__(self, "name", name)

    @classmethod
    def array(cls, items: Any, **kwargs: Any) -> OutputSpec:
        return cls({"type": "array", "items": _canonicalize_type(items)}, **kwargs)

    @classmethod
    def enum(cls, *symbols: str, name: str | None = None, **kwargs: Any) -> OutputSpec:
        payload: dict[str, Any] = {"type": "enum", "symbols": list(symbols)}
        _merge_if_set(payload, "name", name)
        return cls(payload, **kwargs)

    @classmethod
    def record(
        cls,
        fields: Mapping[str, FieldSpec] | list[Any],
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> OutputSpec:
        return cls(_record_type_payload(fields, name=name), **kwargs)

    @classmethod
    def stdout(cls, **kwargs: Any) -> OutputSpec:
        return cls("stdout", **kwargs)

    @classmethod
    def stderr(cls, **kwargs: Any) -> OutputSpec:
        return cls("stderr", **kwargs)

    def named(self, name: str) -> OutputSpec:
        return _replace_frozen(self, name=name)

    def label(self, text: str) -> OutputSpec:
        return _replace_frozen(self, label_text=text)

    def doc(self, text: str | list[str]) -> OutputSpec:
        return _replace_frozen(self, doc_text=text)

    def format(self, value: Any) -> OutputSpec:
        return _replace_frozen(self, format_value=value)

    def secondary_files(self, *values: Any) -> OutputSpec:
        return _replace_frozen(self, secondary_files_value=list(values))

    def streamable(self, value: bool) -> OutputSpec:
        return _replace_frozen(self, streamable_value=value)

    def load_listing(self, value: str) -> OutputSpec:
        return _replace_frozen(self, load_listing_value=value)

    def load_contents(self, value: bool) -> OutputSpec:
        return _replace_frozen(self, load_contents_value=value)

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
        _merge_if_set(payload, "label", self.label_text)
        _merge_if_set(payload, "doc", _render_doc(self.doc_text))
        _merge_if_set(payload, "format", self.format_value)
        _merge_if_set(payload, "secondaryFiles", self.secondary_files_value)
        _merge_if_set(payload, "streamable", self.streamable_value)
        _merge_if_set(payload, "loadListing", self.load_listing_value)
        payload.update(_render(self.extra))
        return payload
