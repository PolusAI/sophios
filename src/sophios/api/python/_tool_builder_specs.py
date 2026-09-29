"""Private spec wrappers for the Tool Builder.

Every class here is a thin, order-preserving wrapper around a `cwl_utils`
generated class: constructors accept the builder's public (snake_case)
argument names, and `to_dict()`/`to_fields()` build the matching
`cwl_utils.parser.cwl_v1_2` object and render it with `.save()`. CWL
semantics (what fields exist, what they mean, how they serialize) come
from `cwl_utils`; this module only adapts the calling convention.
"""

# pylint: disable=missing-function-docstring,too-few-public-methods
# pylint: disable=too-many-instance-attributes,too-many-arguments
# pylint: disable=too-many-locals,redefined-builtin,too-many-lines

from collections.abc import Mapping
from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Any, ClassVar, TypeVar, cast

from cwl_utils.parser import cwl_v1_2 as _cwl_v12

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
    """A CWL secondary file pattern."""

    _cwl = _cwl_v12.SecondaryFileSchema
    pattern: Any
    required: Any = None


def secondary_file(pattern: Any, *, required: bool | str | None = None, **extra: Any) -> "SecondaryFile":
    """Create a secondary file specification."""
    return SecondaryFile(pattern=pattern, required=required, extra=dict(extra))


@dataclass(eq=False)
class Dirent(_CWLObject):
    """A CWL InitialWorkDirRequirement listing entry."""

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
        name = _named_parameter(reference, kind="input")
        return cls(_input_expression(name), entryname or _basename_expression(name), writable, extra=extra or {})


@dataclass(eq=False)
class EnvironmentDef(_CWLObject):
    """An EnvVarRequirement entry."""

    _cwl = _cwl_v12.EnvironmentDef
    env_name: Any
    env_value: Any


@dataclass(eq=False)
class CommandLineBinding(_CWLObject):
    """A CWL input binding or argument binding."""

    _cwl = _cwl_v12.CommandLineBinding
    position: Any = None
    prefix: Any = None
    separate: Any = None
    item_separator: Any = None
    value_from: Any = None
    shell_quote: Any = None


@dataclass(eq=False)
class CommandOutputBinding(_CWLObject):
    """A CWL output binding."""

    _cwl = _cwl_v12.CommandOutputBinding
    glob: Any = None
    load_contents: Any = None
    output_eval: Any = None


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
    """A requirement or hint: rendered as `to_fields()` under its `class_name`."""

    class_name: ClassVar[str]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        cls.class_name = cls.__name__

    def to_fields(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass(eq=False)
class DockerRequirement(_RequirementSpec):
    """DockerRequirement helper."""

    _cwl = _cwl_v12.DockerRequirement
    docker_pull: Any = None
    docker_load: Any = None
    docker_file: Any = None
    docker_import: Any = None
    docker_image_id: Any = None
    docker_output_directory: Any = None


@dataclass(eq=False)
class InlineJavascriptRequirement(_RequirementSpec):
    """InlineJavascriptRequirement helper."""

    _cwl = _cwl_v12.InlineJavascriptRequirement
    expression_lib: Any = None


@dataclass(eq=False)
class SchemaDefRequirement(_RequirementSpec):
    """SchemaDefRequirement helper."""

    _cwl = _cwl_v12.SchemaDefRequirement
    types: Any = field(metadata={"render": lambda values: [_canonicalize_type(value) for value in values]})


@dataclass(eq=False)
class LoadListingRequirement(_RequirementSpec):
    """LoadListingRequirement helper."""

    _cwl = _cwl_v12.LoadListingRequirement
    load_listing: Any


@dataclass(eq=False)
class ShellCommandRequirement(_RequirementSpec):
    """ShellCommandRequirement helper."""

    _cwl = _cwl_v12.ShellCommandRequirement


@dataclass(eq=False)
class SoftwarePackage(_CWLObject):
    """A SoftwareRequirement package entry."""

    _cwl = _cwl_v12.SoftwarePackage
    package: Any
    version: Any = None
    specs: Any = None


@dataclass(eq=False)
class SoftwareRequirement(_RequirementSpec):
    """SoftwareRequirement helper."""

    _cwl = _cwl_v12.SoftwareRequirement
    packages: Any


@dataclass(eq=False)
class InitialWorkDirRequirement(_RequirementSpec):
    """InitialWorkDirRequirement helper."""

    _cwl = _cwl_v12.InitialWorkDirRequirement
    listing: Any


@dataclass(eq=False)
class EnvVarRequirement(_RequirementSpec):
    """EnvVarRequirement helper."""

    _cwl = _cwl_v12.EnvVarRequirement
    env_def: Any


@dataclass(eq=False)
class ResourceRequirement(_RequirementSpec):
    """ResourceRequirement helper."""

    _cwl = _cwl_v12.ResourceRequirement
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
    """NetworkAccess helper."""

    _cwl = _cwl_v12.NetworkAccess
    network_access: Any


@dataclass(eq=False)
class WorkReuse(_RequirementSpec):
    """WorkReuse helper."""

    _cwl = _cwl_v12.WorkReuse
    enable_reuse: Any


@dataclass(eq=False)
class InplaceUpdateRequirement(_RequirementSpec):
    """InplaceUpdateRequirement helper."""

    _cwl = _cwl_v12.InplaceUpdateRequirement
    inplace_update: Any = True


@dataclass(eq=False)
class ToolTimeLimit(_RequirementSpec):
    """ToolTimeLimit helper."""

    _cwl = _cwl_v12.ToolTimeLimit
    timelimit: Any


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
        payload: dict[str, Any] = _cwl_v12.CommandInputRecordField(
            name=self.name,
            type_=_canonicalize_type(self.type_),
            label=self.label_text,
            doc=_render_doc(self.doc_text),
        ).save()
        if self.default_value is not _SUPPORT.unset:
            payload["default"] = _render(self.default_value)
        payload.update(_render(self.extra))
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
        if self.name is None:
            raise ValueError("Inputs must have a name before serialization")
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
        payload: dict[str, Any] = _cwl_v12.CommandInputParameter(
            id=self.name,
            type_=_apply_required(self.type_, self.required),
            label=self.label_text,
            doc=_render_doc(self.doc_text),
            format=_render(self.format_value),
            secondaryFiles=_render(self.secondary_files_value),
            streamable=self.streamable_value,
            loadContents=self.load_contents_value,
            loadListing=self.load_listing_value,
            inputBinding=None if binding is None else binding.to_dict(),
        ).save()
        if self.default_value is not _SUPPORT.unset:
            payload["default"] = _render(self.default_value)
        payload.update(_render(self.extra))
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
        if self.name is None:
            raise ValueError("Outputs must have a name before serialization")
        binding = _optional_binding(
            CommandOutputBinding(
                glob=self.glob,
                load_contents=self.load_contents_value,
                output_eval=self.output_eval,
                extra=dict(self.binding_extra),
            )
        )
        payload: dict[str, Any] = _cwl_v12.CommandOutputParameter(
            id=self.name,
            type_=_apply_required(self.type_, self.required),
            label=self.label_text,
            doc=_render_doc(self.doc_text),
            format=_render(self.format_value),
            secondaryFiles=_render(self.secondary_files_value),
            streamable=self.streamable_value,
            outputBinding=None if binding is None else binding.to_dict(),
        ).save()
        # loadListing is not part of CommandOutputParameter in the CWL v1.2
        # schema; kept as a builder extension for symmetry with InputSpec.
        if self.load_listing_value is not None:
            payload["loadListing"] = self.load_listing_value
        payload.update(_render(self.extra))
        return payload
