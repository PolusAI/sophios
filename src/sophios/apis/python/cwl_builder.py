"""Cleanroom CWL v1.2 CommandLineTool builder.

This module is intentionally separate from the workflow DSL. It is a plain
Python authoring layer for CWL CommandLineTool documents with three goals:

1. cover the common 90% of real CLT authoring cleanly,
2. validate generated documents through the cwltool/schema-salad stack, and
3. leave raw escape hatches for the remaining awkward corners of the spec.

Recommended style
-----------------
Prefer the structured helpers:

```python
tool = (
    CommandLineToolBuilder("custom-tool")
    .inputs(message=Input.string())
    .outputs(out=Output.file(glob="out.txt"))
    .time_limit(60)
)
```

The lower-level `.input(...)`, `.output(...)`, `.requirement(...)`, and
`.hint(...)` methods are still available as escape hatches.

Deliberate gaps
---------------
- SALAD authoring features such as `$import`, `$include`, `$mixin`, and `$graph`
  are not first-class builder concepts. They are document-assembly features, not
  CLT-structure features. Use `extra()` or post-process the rendered dict if you
  need them.
- The builder normalizes `requirements` and `hints` to map form keyed by class.
  That covers typical CLT usage, but it does not preserve array ordering.
- Expressions are treated as opaque CWL strings. Schema validation is delegated
  to cwltool/schema-salad; expression linting is intentionally out of scope.
- Implementation-specific extension objects are supported through `extra()` and
  raw dict payloads, but they do not get typed wrappers by default.
"""

# pylint: disable=missing-function-docstring,redefined-builtin,too-few-public-methods,too-many-arguments
# pylint: disable=too-many-instance-attributes,too-many-lines,too-many-locals,too-many-public-methods

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, overload

import yaml

from sophios import utils_cwl


_UNSET = object()


def _render(value: Any) -> Any:
    match value:
        case Path():
            return str(value)
        case list() as values:
            return [_render(item) for item in values]
        case tuple() as values:
            return [_render(item) for item in values]
        case dict() as values:
            return {key: _render(item) for key, item in values.items()}
        case _ if hasattr(value, "to_dict") and callable(value.to_dict):
            return _render(value.to_dict())
        case _:
            return value


def _merge_if_set(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = _render(value)


def _merge_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not _UNSET:
        target[key] = _render(value)


def _canonicalize_type(type_: Any) -> Any:
    return utils_cwl.canonicalize_type(_render(type_))


def _render_doc(value: str | list[str] | None) -> str | list[str] | None:
    match value:
        case None:
            return None
        case str() as text:
            return text
        case list() as texts:
            return [str(text) for text in texts]


def _render_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {key: _render(item) for key, item in value.items()}


def _render_secondary_files(value: Any) -> Any:
    if value is None:
        return None
    return _render(value)


@overload
def _optional_binding(binding: "CommandLineBinding") -> "CommandLineBinding | None":
    ...


@overload
def _optional_binding(binding: "CommandOutputBinding") -> "CommandOutputBinding | None":
    ...


def _optional_binding(
    binding: "CommandLineBinding | CommandOutputBinding",
) -> "CommandLineBinding | CommandOutputBinding | None":
    if binding.to_dict():
        return binding
    return None


def _import_cwltool_load_tool() -> Any:
    try:
        from cwltool import load_tool  # pylint: disable=import-outside-toplevel
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "cwltool/schema_salad is required to validate generated CommandLineTools"
        ) from exc
    return load_tool


@dataclass(slots=True)
class SecondaryFile:
    """A CWL secondary file pattern."""

    pattern: Any
    required: bool | str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> str | dict[str, Any]:
        if self.required is None and not self.extra and isinstance(self.pattern, str):
            return self.pattern
        data: dict[str, Any] = {"pattern": _render(self.pattern)}
        _merge_if_set(data, "required", self.required)
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class CommandLineBinding:
    """CWL CommandLineBinding fields shared by inputs and arguments."""

    position: int | float | None = None
    prefix: str | None = None
    separate: bool | None = None
    item_separator: str | None = None
    value_from: Any = None
    shell_quote: bool | None = None
    load_contents: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        _merge_if_set(data, "position", self.position)
        _merge_if_set(data, "prefix", self.prefix)
        _merge_if_set(data, "separate", self.separate)
        _merge_if_set(data, "itemSeparator", self.item_separator)
        _merge_if_set(data, "valueFrom", self.value_from)
        _merge_if_set(data, "shellQuote", self.shell_quote)
        _merge_if_set(data, "loadContents", self.load_contents)
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class CommandOutputBinding:
    """CWL CommandOutputBinding fields."""

    glob: Any = None
    load_contents: bool | None = None
    output_eval: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        _merge_if_set(data, "glob", self.glob)
        _merge_if_set(data, "loadContents", self.load_contents)
        _merge_if_set(data, "outputEval", self.output_eval)
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class Dirent:
    """InitialWorkDirRequirement listing entry."""

    entry: Any
    entryname: str | None = None
    writable: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {"entry": _render(self.entry)}
        _merge_if_set(data, "entryname", self.entryname)
        _merge_if_set(data, "writable", self.writable)
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class EnvironmentDef:
    """EnvVarRequirement entry."""

    env_name: str
    env_value: str

    def to_dict(self) -> dict[str, str]:
        return {"envName": self.env_name, "envValue": self.env_value}


@dataclass(slots=True)
class SoftwarePackage:
    """SoftwareRequirement package entry."""

    package: str
    version: list[str] | None = None
    specs: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {"package": self.package}
        _merge_if_set(data, "version", self.version)
        _merge_if_set(data, "specs", self.specs)
        data.update(_render(self.extra))
        return data


class _RequirementSpec:
    class_name: ClassVar[str]

    def to_fields(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(slots=True)
class DockerRequirement(_RequirementSpec):
    class_name: ClassVar[str] = "DockerRequirement"

    docker_pull: str | None = None
    docker_load: str | None = None
    docker_file: str | dict[str, Any] | None = None
    docker_import: str | None = None
    docker_image_id: str | None = None
    docker_output_directory: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        _merge_if_set(data, "dockerPull", self.docker_pull)
        _merge_if_set(data, "dockerLoad", self.docker_load)
        _merge_if_set(data, "dockerFile", self.docker_file)
        _merge_if_set(data, "dockerImport", self.docker_import)
        _merge_if_set(data, "dockerImageId", self.docker_image_id)
        _merge_if_set(data, "dockerOutputDirectory", self.docker_output_directory)
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class ResourceRequirement(_RequirementSpec):
    class_name: ClassVar[str] = "ResourceRequirement"

    cores_min: int | float | str | None = None
    cores_max: int | float | str | None = None
    ram_min: int | float | str | None = None
    ram_max: int | float | str | None = None
    tmpdir_min: int | float | str | None = None
    tmpdir_max: int | float | str | None = None
    outdir_min: int | float | str | None = None
    outdir_max: int | float | str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        numeric_pairs = [
            ("cores", self.cores_min, self.cores_max),
            ("ram", self.ram_min, self.ram_max),
            ("tmpdir", self.tmpdir_min, self.tmpdir_max),
            ("outdir", self.outdir_min, self.outdir_max),
        ]
        for resource, minimum, maximum in numeric_pairs:
            if isinstance(minimum, (int, float)) and minimum < 0:
                raise ValueError(f"{resource} minimum cannot be negative")
            if isinstance(maximum, (int, float)) and maximum < 0:
                raise ValueError(f"{resource} maximum cannot be negative")
            if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)) and maximum < minimum:
                raise ValueError(f"{resource} maximum cannot be smaller than minimum")

        data: dict[str, Any] = {}
        _merge_if_set(data, "coresMin", self.cores_min)
        _merge_if_set(data, "coresMax", self.cores_max)
        _merge_if_set(data, "ramMin", self.ram_min)
        _merge_if_set(data, "ramMax", self.ram_max)
        _merge_if_set(data, "tmpdirMin", self.tmpdir_min)
        _merge_if_set(data, "tmpdirMax", self.tmpdir_max)
        _merge_if_set(data, "outdirMin", self.outdir_min)
        _merge_if_set(data, "outdirMax", self.outdir_max)
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class InitialWorkDirRequirement(_RequirementSpec):
    class_name: ClassVar[str] = "InitialWorkDirRequirement"

    listing: Any
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        data = {"listing": _render(self.listing)}
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class EnvVarRequirement(_RequirementSpec):
    class_name: ClassVar[str] = "EnvVarRequirement"

    env_defs: list[EnvironmentDef | dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        data = {"envDef": _render(self.env_defs)}
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class ShellCommandRequirement(_RequirementSpec):
    class_name: ClassVar[str] = "ShellCommandRequirement"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        return _render_mapping(self.extra)


@dataclass(slots=True)
class InlineJavascriptRequirement(_RequirementSpec):
    class_name: ClassVar[str] = "InlineJavascriptRequirement"

    expression_lib: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        _merge_if_set(data, "expressionLib", self.expression_lib)
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class SchemaDefRequirement(_RequirementSpec):
    class_name: ClassVar[str] = "SchemaDefRequirement"

    types: list[Any]
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        data = {"types": _render(self.types)}
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class LoadListingRequirement(_RequirementSpec):
    class_name: ClassVar[str] = "LoadListingRequirement"

    load_listing: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        data = {"loadListing": self.load_listing}
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class SoftwareRequirement(_RequirementSpec):
    class_name: ClassVar[str] = "SoftwareRequirement"

    packages: list[SoftwarePackage | dict[str, Any]] | dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        data = {"packages": _render(self.packages)}
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class WorkReuse(_RequirementSpec):
    class_name: ClassVar[str] = "WorkReuse"

    enable_reuse: bool | str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        data = {"enableReuse": _render(self.enable_reuse)}
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class NetworkAccess(_RequirementSpec):
    class_name: ClassVar[str] = "NetworkAccess"

    network_access: bool | str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        data = {"networkAccess": _render(self.network_access)}
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class InplaceUpdateRequirement(_RequirementSpec):
    class_name: ClassVar[str] = "InplaceUpdateRequirement"

    inplace_update: bool
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        data = {"inplaceUpdate": self.inplace_update}
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class ToolTimeLimit(_RequirementSpec):
    class_name: ClassVar[str] = "ToolTimeLimit"

    timelimit: int | str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        if isinstance(self.timelimit, int) and self.timelimit < 0:
            raise ValueError("timelimit cannot be negative")
        data = {"timelimit": _render(self.timelimit)}
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class CommandInput:
    """A single CLT input parameter."""

    name: str
    type_: Any
    binding: CommandLineBinding | None = None
    label: str | None = None
    doc: str | list[str] | None = None
    format: Any = None
    secondary_files: Any = None
    streamable: bool | None = None
    load_contents: bool | None = None
    load_listing: str | None = None
    default: Any = field(default=_UNSET, repr=False)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": _canonicalize_type(self.type_)}
        _merge_if_set(data, "label", self.label)
        _merge_if_set(data, "doc", _render_doc(self.doc))
        _merge_if_set(data, "format", self.format)
        _merge_if_set(data, "streamable", self.streamable)
        _merge_if_set(data, "loadContents", self.load_contents)
        _merge_if_set(data, "loadListing", self.load_listing)
        secondary_files = _render_secondary_files(self.secondary_files)
        if secondary_files is not None:
            data["secondaryFiles"] = secondary_files
        _merge_if_present(data, "default", self.default)
        if self.binding is not None:
            binding = self.binding.to_dict()
            if binding:
                data["inputBinding"] = binding
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class CommandOutput:
    """A single CLT output parameter."""

    name: str
    type_: Any
    binding: CommandOutputBinding | None = None
    label: str | None = None
    doc: str | list[str] | None = None
    format: Any = None
    secondary_files: Any = None
    streamable: bool | None = None
    load_listing: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": _canonicalize_type(self.type_)}
        _merge_if_set(data, "label", self.label)
        _merge_if_set(data, "doc", _render_doc(self.doc))
        _merge_if_set(data, "format", self.format)
        _merge_if_set(data, "streamable", self.streamable)
        _merge_if_set(data, "loadListing", self.load_listing)
        secondary_files = _render_secondary_files(self.secondary_files)
        if secondary_files is not None:
            data["secondaryFiles"] = secondary_files
        if self.binding is not None:
            binding = self.binding.to_dict()
            if binding:
                data["outputBinding"] = binding
        data.update(_render(self.extra))
        return data


@dataclass(slots=True)
class FieldSpec:
    """Structured record field specification."""

    type_: Any
    label: str | None = None
    doc: str | list[str] | None = None
    input_binding: CommandLineBinding | None = None
    output_binding: CommandOutputBinding | None = None
    secondary_files: Any = None
    streamable: bool | None = None
    format: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def named(self, name: str) -> dict[str, Any]:
        return record_field(
            name,
            self.type_,
            label=self.label,
            doc=self.doc,
            input_binding=self.input_binding,
            output_binding=self.output_binding,
            secondary_files=self.secondary_files,
            streamable=self.streamable,
            format=self.format,
            extra=self.extra,
        )


@dataclass(slots=True)
class InputSpec:
    """Structured input specification without repeating the input name."""

    type_: Any
    binding: CommandLineBinding | None = None
    label: str | None = None
    doc: str | list[str] | None = None
    format: Any = None
    secondary_files: Any = None
    streamable: bool | None = None
    load_contents: bool | None = None
    load_listing: str | None = None
    default: Any = field(default=_UNSET, repr=False)
    extra: dict[str, Any] = field(default_factory=dict)

    def named(self, name: str) -> CommandInput:
        return CommandInput(
            name=name,
            type_=self.type_,
            binding=self.binding,
            label=self.label,
            doc=self.doc,
            format=self.format,
            secondary_files=self.secondary_files,
            streamable=self.streamable,
            load_contents=self.load_contents,
            load_listing=self.load_listing,
            default=self.default,
            extra=self.extra,
        )


@dataclass(slots=True)
class OutputSpec:
    """Structured output specification without repeating the output name."""

    type_: Any
    binding: CommandOutputBinding | None = None
    label: str | None = None
    doc: str | list[str] | None = None
    format: Any = None
    secondary_files: Any = None
    streamable: bool | None = None
    load_listing: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def named(self, name: str) -> CommandOutput:
        return CommandOutput(
            name=name,
            type_=self.type_,
            binding=self.binding,
            label=self.label,
            doc=self.doc,
            format=self.format,
            secondary_files=self.secondary_files,
            streamable=self.streamable,
            load_listing=self.load_listing,
            extra=self.extra,
        )


@dataclass(slots=True)
class CommandArgument:
    """A CWL command line argument entry."""

    value: Any = None
    binding: CommandLineBinding = field(default_factory=CommandLineBinding)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_yaml(self) -> str | dict[str, Any]:
        binding = self.binding.to_dict()
        if not binding and isinstance(self.value, str) and not self.extra:
            return self.value
        if self.value is not None and "valueFrom" not in binding:
            binding["valueFrom"] = _render(self.value)
        binding.update(_render(self.extra))
        return binding


def secondary_file(
    pattern: Any,
    *,
    required: bool | str | None = None,
    extra: dict[str, Any] | None = None,
) -> SecondaryFile:
    return SecondaryFile(pattern=pattern, required=required, extra=dict(extra or {}))


def array_type(
    items: Any,
    *,
    name: str | None = None,
    label: str | None = None,
    doc: str | list[str] | None = None,
    input_binding: CommandLineBinding | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"type": "array", "items": _canonicalize_type(items)}
    _merge_if_set(data, "name", name)
    _merge_if_set(data, "label", label)
    _merge_if_set(data, "doc", _render_doc(doc))
    if input_binding is not None:
        binding = input_binding.to_dict()
        if binding:
            data["inputBinding"] = binding
    data.update(_render(extra or {}))
    return data


def enum_type(
    symbols: list[str],
    *,
    name: str | None = None,
    label: str | None = None,
    doc: str | list[str] | None = None,
    input_binding: CommandLineBinding | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"type": "enum", "symbols": list(symbols)}
    _merge_if_set(data, "name", name)
    _merge_if_set(data, "label", label)
    _merge_if_set(data, "doc", _render_doc(doc))
    if input_binding is not None:
        binding = input_binding.to_dict()
        if binding:
            data["inputBinding"] = binding
    data.update(_render(extra or {}))
    return data


def record_field(
    name: str,
    type_: Any,
    *,
    label: str | None = None,
    doc: str | list[str] | None = None,
    input_binding: CommandLineBinding | None = None,
    output_binding: CommandOutputBinding | None = None,
    secondary_files: Any = None,
    streamable: bool | None = None,
    format: Any = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"name": name, "type": _canonicalize_type(type_)}
    _merge_if_set(data, "label", label)
    _merge_if_set(data, "doc", _render_doc(doc))
    _merge_if_set(data, "format", format)
    _merge_if_set(data, "streamable", streamable)
    secondary_files_value = _render_secondary_files(secondary_files)
    if secondary_files_value is not None:
        data["secondaryFiles"] = secondary_files_value
    if input_binding is not None:
        binding = input_binding.to_dict()
        if binding:
            data["inputBinding"] = binding
    if output_binding is not None:
        binding = output_binding.to_dict()
        if binding:
            data["outputBinding"] = binding
    data.update(_render(extra or {}))
    return data


def record_type(
    fields: list[Any] | dict[str, Any],
    *,
    name: str | None = None,
    label: str | None = None,
    doc: str | list[str] | None = None,
    input_binding: CommandLineBinding | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"type": "record", "fields": _render(fields)}
    _merge_if_set(data, "name", name)
    _merge_if_set(data, "label", label)
    _merge_if_set(data, "doc", _render_doc(doc))
    if input_binding is not None:
        binding = input_binding.to_dict()
        if binding:
            data["inputBinding"] = binding
    data.update(_render(extra or {}))
    return data


class Type:
    """Structured CWL type helpers."""

    @staticmethod
    def null() -> str:
        return "null"

    @staticmethod
    def boolean() -> str:
        return "boolean"

    @staticmethod
    def int() -> str:
        return "int"

    @staticmethod
    def long() -> str:
        return "long"

    @staticmethod
    def float() -> str:
        return "float"

    @staticmethod
    def double() -> str:
        return "double"

    @staticmethod
    def string() -> str:
        return "string"

    @staticmethod
    def file() -> str:
        return "File"

    @staticmethod
    def directory() -> str:
        return "Directory"

    @staticmethod
    def stdout() -> str:
        return "stdout"

    @staticmethod
    def stderr() -> str:
        return "stderr"

    @staticmethod
    def any() -> str:
        return "Any"

    @staticmethod
    def array(
        items: Any,
        *,
        name: str | None = None,
        label: str | None = None,
        doc: str | list[str] | None = None,
        input_binding: CommandLineBinding | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return array_type(
            items,
            name=name,
            label=label,
            doc=doc,
            input_binding=input_binding,
            extra=extra,
        )

    @staticmethod
    def enum(
        *symbols: str,
        name: str | None = None,
        label: str | None = None,
        doc: str | list[str] | None = None,
        input_binding: CommandLineBinding | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return enum_type(
            list(symbols),
            name=name,
            label=label,
            doc=doc,
            input_binding=input_binding,
            extra=extra,
        )

    @staticmethod
    def record(
        fields: dict[str, FieldSpec] | list[Any],
        *,
        name: str | None = None,
        label: str | None = None,
        doc: str | list[str] | None = None,
        input_binding: CommandLineBinding | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        match fields:
            case dict() as mapping:
                rendered_fields = [
                    spec.named(field_name) if isinstance(spec, FieldSpec) else record_field(field_name, spec)
                    for field_name, spec in mapping.items()
                ]
            case list() as items:
                rendered_fields = _render(items)
            case _:
                raise TypeError("record fields must be a mapping or a list")
        return record_type(
            rendered_fields,
            name=name,
            label=label,
            doc=doc,
            input_binding=input_binding,
            extra=extra,
        )

    @staticmethod
    def optional(inner: Any) -> list[Any]:
        return ["null", _canonicalize_type(inner)]


class Field:
    """Structured record field helpers."""

    @staticmethod
    def of(
        type_: Any,
        *,
        label: str | None = None,
        doc: str | list[str] | None = None,
        input_binding: CommandLineBinding | None = None,
        output_binding: CommandOutputBinding | None = None,
        secondary_files: Any = None,
        streamable: bool | None = None,
        format: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> FieldSpec:
        return FieldSpec(
            type_=type_,
            label=label,
            doc=doc,
            input_binding=input_binding,
            output_binding=output_binding,
            secondary_files=secondary_files,
            streamable=streamable,
            format=format,
            extra=dict(extra or {}),
        )

    @staticmethod
    def string(**kwargs: Any) -> FieldSpec:
        return Field.of(Type.string(), **kwargs)

    @staticmethod
    def int(**kwargs: Any) -> FieldSpec:
        return Field.of(Type.int(), **kwargs)

    @staticmethod
    def long(**kwargs: Any) -> FieldSpec:
        return Field.of(Type.long(), **kwargs)

    @staticmethod
    def float(**kwargs: Any) -> FieldSpec:
        return Field.of(Type.float(), **kwargs)

    @staticmethod
    def double(**kwargs: Any) -> FieldSpec:
        return Field.of(Type.double(), **kwargs)

    @staticmethod
    def boolean(**kwargs: Any) -> FieldSpec:
        return Field.of(Type.boolean(), **kwargs)

    @staticmethod
    def file(**kwargs: Any) -> FieldSpec:
        return Field.of(Type.file(), **kwargs)

    @staticmethod
    def directory(**kwargs: Any) -> FieldSpec:
        return Field.of(Type.directory(), **kwargs)

    @staticmethod
    def array(items: Any, **kwargs: Any) -> FieldSpec:
        return Field.of(Type.array(items), **kwargs)

    @staticmethod
    def enum(*symbols: str, **kwargs: Any) -> FieldSpec:
        return Field.of(Type.enum(*symbols), **kwargs)

    @staticmethod
    def record(fields: dict[str, FieldSpec] | list[Any], **kwargs: Any) -> FieldSpec:
        return Field.of(Type.record(fields), **kwargs)


# pylint: disable=too-many-public-methods
class Input:
    """Structured CLT input helpers."""

    @staticmethod
    def of(
        type_: Any,
        *,
        position: int | float | None = None,
        prefix: str | None = None,
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
    ) -> InputSpec:
        binding = _optional_binding(CommandLineBinding(
            position=position,
            prefix=prefix,
            separate=separate,
            item_separator=item_separator,
            value_from=value_from,
            shell_quote=shell_quote,
            extra=dict(binding_extra or {}),
        ))
        return InputSpec(
            type_=type_,
            binding=binding,
            label=label,
            doc=doc,
            format=format,
            secondary_files=secondary_files,
            streamable=streamable,
            load_contents=load_contents,
            load_listing=load_listing,
            default=default,
            extra=dict(extra or {}),
        )

    @staticmethod
    def string(**kwargs: Any) -> InputSpec:
        return Input.of(Type.string(), **kwargs)

    @staticmethod
    def int(**kwargs: Any) -> InputSpec:
        return Input.of(Type.int(), **kwargs)

    @staticmethod
    def long(**kwargs: Any) -> InputSpec:
        return Input.of(Type.long(), **kwargs)

    @staticmethod
    def float(**kwargs: Any) -> InputSpec:
        return Input.of(Type.float(), **kwargs)

    @staticmethod
    def double(**kwargs: Any) -> InputSpec:
        return Input.of(Type.double(), **kwargs)

    @staticmethod
    def boolean(**kwargs: Any) -> InputSpec:
        return Input.of(Type.boolean(), **kwargs)

    @staticmethod
    def file(**kwargs: Any) -> InputSpec:
        return Input.of(Type.file(), **kwargs)

    @staticmethod
    def directory(**kwargs: Any) -> InputSpec:
        return Input.of(Type.directory(), **kwargs)

    @staticmethod
    def array(items: Any, **kwargs: Any) -> InputSpec:
        return Input.of(Type.array(items), **kwargs)

    @staticmethod
    def enum(*symbols: str, **kwargs: Any) -> InputSpec:
        return Input.of(Type.enum(*symbols), **kwargs)

    @staticmethod
    def record(fields: dict[str, FieldSpec] | list[Any], **kwargs: Any) -> InputSpec:
        return Input.of(Type.record(fields), **kwargs)


# pylint: disable=too-many-public-methods
class Output:
    """Structured CLT output helpers."""

    @staticmethod
    def of(
        type_: Any,
        *,
        glob: Any = None,
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
    ) -> OutputSpec:
        binding = _optional_binding(CommandOutputBinding(
            glob=glob,
            load_contents=load_contents,
            output_eval=output_eval,
            extra=dict(binding_extra or {}),
        ))
        return OutputSpec(
            type_=type_,
            binding=binding,
            label=label,
            doc=doc,
            format=format,
            secondary_files=secondary_files,
            streamable=streamable,
            load_listing=load_listing,
            extra=dict(extra or {}),
        )

    @staticmethod
    def string(**kwargs: Any) -> OutputSpec:
        return Output.of(Type.string(), **kwargs)

    @staticmethod
    def int(**kwargs: Any) -> OutputSpec:
        return Output.of(Type.int(), **kwargs)

    @staticmethod
    def long(**kwargs: Any) -> OutputSpec:
        return Output.of(Type.long(), **kwargs)

    @staticmethod
    def float(**kwargs: Any) -> OutputSpec:
        return Output.of(Type.float(), **kwargs)

    @staticmethod
    def double(**kwargs: Any) -> OutputSpec:
        return Output.of(Type.double(), **kwargs)

    @staticmethod
    def boolean(**kwargs: Any) -> OutputSpec:
        return Output.of(Type.boolean(), **kwargs)

    @staticmethod
    def file(**kwargs: Any) -> OutputSpec:
        return Output.of(Type.file(), **kwargs)

    @staticmethod
    def directory(**kwargs: Any) -> OutputSpec:
        return Output.of(Type.directory(), **kwargs)

    @staticmethod
    def stdout(**kwargs: Any) -> OutputSpec:
        return Output.of(Type.stdout(), **kwargs)

    @staticmethod
    def stderr(**kwargs: Any) -> OutputSpec:
        return Output.of(Type.stderr(), **kwargs)

    @staticmethod
    def array(items: Any, **kwargs: Any) -> OutputSpec:
        return Output.of(Type.array(items), **kwargs)

    @staticmethod
    def enum(*symbols: str, **kwargs: Any) -> OutputSpec:
        return Output.of(Type.enum(*symbols), **kwargs)

    @staticmethod
    def record(fields: dict[str, FieldSpec] | list[Any], **kwargs: Any) -> OutputSpec:
        return Output.of(Type.record(fields), **kwargs)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Result of validating a generated CLT with cwltool/schema-salad."""

    path: Path
    uri: str
    process: Any


class CWLBuilderValidationError(ValueError):
    """Raised when a generated CLT fails schema validation."""


def validate_cwl_document(
    document: dict[str, Any],
    *,
    filename: str = "tool.cwl",
    skip_schemas: bool = False,
) -> ValidationResult:
    with tempfile.TemporaryDirectory(prefix="sophios-cwl-builder-") as tmpdir:
        temp_path = Path(tmpdir) / filename
        temp_path.write_text(
            yaml.safe_dump(_render(document), sort_keys=False, line_break="\n"),
            encoding="utf-8",
        )
        return _validate_path(temp_path, skip_schemas=skip_schemas)


def _validate_path(path: Path, *, skip_schemas: bool = False) -> ValidationResult:
    del skip_schemas  # Reserved for parity with the rest of the codebase.
    load_tool = _import_cwltool_load_tool()
    try:
        loading_context, workflowobj, uri = load_tool.fetch_document(str(path))
        loading_context, uri = load_tool.resolve_and_validate_document(
            loading_context,
            workflowobj,
            uri,
            preprocess_only=False,
        )
        process = load_tool.make_tool(uri, loading_context)
    except Exception as exc:
        raise CWLBuilderValidationError(f"Generated CommandLineTool failed validation: {path}") from exc
    return ValidationResult(path=path, uri=uri, process=process)


def _normalize_requirement(
    requirement: str | _RequirementSpec | dict[str, Any],
    value: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    match requirement:
        case str() as class_name:
            payload = {} if value is None else dict(_render(value))
            return class_name, payload
        case _RequirementSpec() as spec:
            return spec.class_name, spec.to_fields()
        case dict() as payload:
            if "class" not in payload:
                raise ValueError("raw requirement dicts must include a 'class' key")
            payload_copy = dict(_render(payload))
            class_name = str(payload_copy.pop("class"))
            return class_name, payload_copy
        case _:
            raise TypeError("requirement must be a class name, requirement spec, or raw dict")


@dataclass(slots=True)
class CommandLineToolBuilder:
    """Fluent builder for CWL v1.2 `CommandLineTool` documents."""

    tool_id: str
    cwl_version: str = "v1.2"
    label_text: str | None = None
    doc_text: str | list[str] | None = None
    _base_command: list[str] = field(default_factory=list)
    _arguments: list[str | dict[str, Any]] = field(default_factory=list)
    _inputs: dict[str, CommandInput] = field(default_factory=dict)
    _outputs: dict[str, CommandOutput] = field(default_factory=dict)
    _requirements: dict[str, dict[str, Any]] = field(default_factory=dict)
    _hints: dict[str, dict[str, Any]] = field(default_factory=dict)
    _stdin: str | None = None
    _stdout: str | None = None
    _stderr: str | None = None
    _intent: list[str] = field(default_factory=list)
    _namespaces: dict[str, str] = field(default_factory=dict)
    _schemas: list[str] = field(default_factory=list)
    _success_codes: list[int] = field(default_factory=list)
    _temporary_fail_codes: list[int] = field(default_factory=list)
    _permanent_fail_codes: list[int] = field(default_factory=list)
    _extra: dict[str, Any] = field(default_factory=dict)

    def label(self, text: str) -> "CommandLineToolBuilder":
        self.label_text = text
        return self

    def doc(self, text: str | list[str]) -> "CommandLineToolBuilder":
        self.doc_text = text
        return self

    def namespace(self, prefix: str, iri: str) -> "CommandLineToolBuilder":
        self._namespaces[prefix] = iri
        return self

    def schema(self, iri: str) -> "CommandLineToolBuilder":
        self._schemas.append(iri)
        return self

    def intent(self, *identifiers: str) -> "CommandLineToolBuilder":
        self._intent.extend(identifiers)
        return self

    def base_command(self, *parts: str) -> "CommandLineToolBuilder":
        self._base_command = list(parts)
        return self

    def stdin(self, value: str) -> "CommandLineToolBuilder":
        self._stdin = value
        return self

    def stdout(self, value: str) -> "CommandLineToolBuilder":
        self._stdout = value
        return self

    def stderr(self, value: str) -> "CommandLineToolBuilder":
        self._stderr = value
        return self

    def add_input(self, input_spec: CommandInput) -> "CommandLineToolBuilder":
        self._inputs[input_spec.name] = input_spec
        return self

    def inputs(self, **input_specs: InputSpec) -> "CommandLineToolBuilder":
        for name, spec in input_specs.items():
            if not isinstance(spec, InputSpec):
                raise TypeError(f"input {name!r} must be an InputSpec")
            self.add_input(spec.named(name))
        return self

    def input(
        self,
        name: str,
        *,
        type_: Any,
        position: int | float | None = None,
        prefix: str | None = None,
        separate: bool | None = None,
        item_separator: str | None = None,
        value_from: Any = None,
        shell_quote: bool | None = None,
        load_contents: bool | None = None,
        load_listing: str | None = None,
        label: str | None = None,
        doc: str | list[str] | None = None,
        format: Any = None,
        secondary_files: Any = None,
        streamable: bool | None = None,
        default: Any = _UNSET,
        binding_extra: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        return self.inputs(
            **{
                name: Input.of(
                    type_,
                    position=position,
                    prefix=prefix,
                    separate=separate,
                    item_separator=item_separator,
                    value_from=value_from,
                    shell_quote=shell_quote,
                    load_contents=load_contents,
                    load_listing=load_listing,
                    label=label,
                    doc=doc,
                    format=format,
                    secondary_files=secondary_files,
                    streamable=streamable,
                    default=default,
                    binding_extra=binding_extra,
                    extra=extra,
                )
            }
        )

    def add_output(self, output_spec: CommandOutput) -> "CommandLineToolBuilder":
        self._outputs[output_spec.name] = output_spec
        return self

    def outputs(self, **output_specs: OutputSpec) -> "CommandLineToolBuilder":
        for name, spec in output_specs.items():
            if not isinstance(spec, OutputSpec):
                raise TypeError(f"output {name!r} must be an OutputSpec")
            self.add_output(spec.named(name))
        return self

    def output(
        self,
        name: str,
        *,
        type_: Any,
        glob: Any = None,
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
    ) -> "CommandLineToolBuilder":
        return self.outputs(
            **{
                name: Output.of(
                    type_,
                    glob=glob,
                    load_contents=load_contents,
                    output_eval=output_eval,
                    label=label,
                    doc=doc,
                    format=format,
                    secondary_files=secondary_files,
                    streamable=streamable,
                    load_listing=load_listing,
                    binding_extra=binding_extra,
                    extra=extra,
                )
            }
        )

    def add_argument(self, argument: str | CommandArgument | dict[str, Any]) -> "CommandLineToolBuilder":
        match argument:
            case str() as literal:
                self._arguments.append(literal)
            case CommandArgument() as structured:
                self._arguments.append(structured.to_yaml())
            case dict() as raw:
                self._arguments.append(_render(raw))
            case _:
                raise TypeError("argument must be a string, CommandArgument, or raw dict")
        return self

    def argument(
        self,
        value: Any = None,
        *,
        position: int | float | None = None,
        prefix: str | None = None,
        separate: bool | None = None,
        item_separator: str | None = None,
        value_from: Any = None,
        shell_quote: bool | None = None,
        load_contents: bool | None = None,
        binding_extra: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        binding = CommandLineBinding(
            position=position,
            prefix=prefix,
            separate=separate,
            item_separator=item_separator,
            value_from=value_from,
            shell_quote=shell_quote,
            load_contents=load_contents,
            extra=dict(binding_extra or {}),
        )
        return self.add_argument(CommandArgument(value=value, binding=binding, extra=dict(extra or {})))

    def requirement(
        self,
        requirement: str | _RequirementSpec | dict[str, Any],
        value: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        class_name, payload = _normalize_requirement(requirement, value)
        self._requirements[class_name] = payload
        return self

    def hint(
        self,
        requirement: str | _RequirementSpec | dict[str, Any],
        value: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        class_name, payload = _normalize_requirement(requirement, value)
        self._hints[class_name] = payload
        return self

    def docker(
        self,
        *,
        docker_pull: str | None = None,
        docker_load: str | None = None,
        docker_file: str | dict[str, Any] | None = None,
        docker_import: str | None = None,
        docker_image_id: str | None = None,
        docker_output_directory: str | None = None,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        spec = DockerRequirement(
            docker_pull=docker_pull,
            docker_load=docker_load,
            docker_file=docker_file,
            docker_import=docker_import,
            docker_image_id=docker_image_id,
            docker_output_directory=docker_output_directory,
            extra=dict(extra or {}),
        )
        return self.hint(spec) if as_hint else self.requirement(spec)

    def inline_javascript(
        self,
        *expression_lib: str,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        spec = InlineJavascriptRequirement(
            expression_lib=list(expression_lib) or None,
            extra=dict(extra or {}),
        )
        return self.hint(spec) if as_hint else self.requirement(spec)

    def schema_definitions(
        self,
        *types: Any,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        spec = SchemaDefRequirement(types=list(types), extra=dict(extra or {}))
        return self.hint(spec) if as_hint else self.requirement(spec)

    def load_listing(
        self,
        value: str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        spec = LoadListingRequirement(load_listing=value, extra=dict(extra or {}))
        return self.hint(spec) if as_hint else self.requirement(spec)

    def shell_command(self, *, as_hint: bool = False, extra: dict[str, Any] | None = None) -> "CommandLineToolBuilder":
        spec = ShellCommandRequirement(extra=dict(extra or {}))
        return self.hint(spec) if as_hint else self.requirement(spec)

    def software(
        self,
        packages: list[SoftwarePackage | dict[str, Any]] | dict[str, Any],
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        spec = SoftwareRequirement(packages=packages, extra=dict(extra or {}))
        return self.hint(spec) if as_hint else self.requirement(spec)

    def initial_workdir(
        self,
        listing: Any,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        spec = InitialWorkDirRequirement(listing=listing, extra=dict(extra or {}))
        return self.hint(spec) if as_hint else self.requirement(spec)

    def env_var(self, name: str, value: str, *, as_hint: bool = False) -> "CommandLineToolBuilder":
        target = self._hints if as_hint else self._requirements
        payload = target.setdefault("EnvVarRequirement", {"envDef": []})
        env_defs = payload.setdefault("envDef", [])
        env_defs.append(EnvironmentDef(name, value).to_dict())
        return self

    def resources(
        self,
        *,
        cores_min: int | float | str | None = None,
        cores_max: int | float | str | None = None,
        ram_min: int | float | str | None = None,
        ram_max: int | float | str | None = None,
        tmpdir_min: int | float | str | None = None,
        tmpdir_max: int | float | str | None = None,
        outdir_min: int | float | str | None = None,
        outdir_max: int | float | str | None = None,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        spec = ResourceRequirement(
            cores_min=cores_min,
            cores_max=cores_max,
            ram_min=ram_min,
            ram_max=ram_max,
            tmpdir_min=tmpdir_min,
            tmpdir_max=tmpdir_max,
            outdir_min=outdir_min,
            outdir_max=outdir_max,
            extra=dict(extra or {}),
        )
        return self.hint(spec) if as_hint else self.requirement(spec)

    def work_reuse(
        self,
        enable: bool | str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        spec = WorkReuse(enable_reuse=enable, extra=dict(extra or {}))
        return self.hint(spec) if as_hint else self.requirement(spec)

    def network_access(
        self,
        enable: bool | str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        spec = NetworkAccess(network_access=enable, extra=dict(extra or {}))
        return self.hint(spec) if as_hint else self.requirement(spec)

    def inplace_update(
        self,
        enable: bool = True,
        *,
        as_hint: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        spec = InplaceUpdateRequirement(inplace_update=enable, extra=dict(extra or {}))
        return self.hint(spec) if as_hint else self.requirement(spec)

    def time_limit(
        self,
        seconds: int | str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineToolBuilder":
        spec = ToolTimeLimit(timelimit=seconds, extra=dict(extra or {}))
        return self.hint(spec) if as_hint else self.requirement(spec)

    def success_codes(self, *codes: int) -> "CommandLineToolBuilder":
        self._success_codes = list(codes)
        return self

    def temporary_fail_codes(self, *codes: int) -> "CommandLineToolBuilder":
        self._temporary_fail_codes = list(codes)
        return self

    def permanent_fail_codes(self, *codes: int) -> "CommandLineToolBuilder":
        self._permanent_fail_codes = list(codes)
        return self

    def extra(self, **values: Any) -> "CommandLineToolBuilder":
        self._extra.update(_render(values))
        return self

    def build(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "cwlVersion": self.cwl_version,
            "class": "CommandLineTool",
            "id": self.tool_id,
            "inputs": {name: input_spec.to_dict() for name, input_spec in self._inputs.items()},
            "outputs": {name: output_spec.to_dict() for name, output_spec in self._outputs.items()},
        }
        if self._namespaces:
            document["$namespaces"] = dict(self._namespaces)
        if self._schemas:
            document["$schemas"] = list(self._schemas)
        _merge_if_set(document, "label", self.label_text)
        _merge_if_set(document, "doc", _render_doc(self.doc_text))
        if self._intent:
            document["intent"] = list(self._intent)
        if self._base_command:
            document["baseCommand"] = self._base_command[0] if len(
                self._base_command) == 1 else list(self._base_command)
        if self._arguments:
            document["arguments"] = list(self._arguments)
        if self._requirements:
            document["requirements"] = _render(self._requirements)
        if self._hints:
            document["hints"] = _render(self._hints)
        _merge_if_set(document, "stdin", self._stdin)
        _merge_if_set(document, "stdout", self._stdout)
        _merge_if_set(document, "stderr", self._stderr)
        if self._success_codes:
            document["successCodes"] = list(self._success_codes)
        if self._temporary_fail_codes:
            document["temporaryFailCodes"] = list(self._temporary_fail_codes)
        if self._permanent_fail_codes:
            document["permanentFailCodes"] = list(self._permanent_fail_codes)
        document.update(_render(self._extra))
        return document

    def to_dict(self) -> dict[str, Any]:
        return self.build()

    def to_yaml(self) -> str:
        rendered_yaml = yaml.safe_dump(self.build(), sort_keys=False, line_break="\n")
        return str(rendered_yaml)

    def save(self, path: str | Path, *, validate: bool = False, skip_schemas: bool = False) -> Path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(self.to_yaml(), encoding="utf-8")
        if validate:
            _validate_path(output_path, skip_schemas=skip_schemas)
        return output_path

    def validate(self, *, skip_schemas: bool = False) -> ValidationResult:
        return validate_cwl_document(self.build(), filename=f"{self.tool_id}.cwl", skip_schemas=skip_schemas)


__all__ = [
    "CWLBuilderValidationError",
    "CommandArgument",
    "CommandInput",
    "CommandLineBinding",
    "CommandLineToolBuilder",
    "CommandOutput",
    "CommandOutputBinding",
    "Dirent",
    "DockerRequirement",
    "EnvironmentDef",
    "EnvVarRequirement",
    "Field",
    "FieldSpec",
    "InitialWorkDirRequirement",
    "InlineJavascriptRequirement",
    "Input",
    "InputSpec",
    "InplaceUpdateRequirement",
    "LoadListingRequirement",
    "NetworkAccess",
    "Output",
    "OutputSpec",
    "ResourceRequirement",
    "SchemaDefRequirement",
    "SecondaryFile",
    "ShellCommandRequirement",
    "SoftwarePackage",
    "SoftwareRequirement",
    "ToolTimeLimit",
    "Type",
    "ValidationResult",
    "WorkReuse",
    "array_type",
    "enum_type",
    "record_field",
    "record_type",
    "secondary_file",
    "validate_cwl_document",
]
