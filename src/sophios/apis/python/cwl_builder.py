"""Public CWL v1.2 CommandLineTool authoring API.

The required core is intentionally small:

```python
inputs = Inputs(input=Input(cwl.directory, position=1))
outputs = Outputs(output=Output(cwl.directory, from_input=inputs.input))
tool = CommandLineTool("example", inputs, outputs)
```

Everything else is optional and chainable.
"""

# pylint: disable=missing-function-docstring
# The fluent builder intentionally exposes many small self-descriptive methods.

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import yaml
from sophios.wic_types import Tools

from ._cwl_builder_namespaces import Field, Input, Inputs, Output, Outputs, cwl
from ._cwl_builder_specs import (
    CommandArgument,
    CommandLineBinding,
    CommandOutputBinding,
    Dirent,
    DockerRequirement,
    EnvironmentDef,
    EnvVarRequirement,
    FieldSpec,
    InitialWorkDirRequirement,
    InlineJavascriptRequirement,
    InplaceUpdateRequirement,
    InputSpec,
    LoadListingRequirement,
    NetworkAccess,
    OutputSpec,
    ResourceRequirement,
    SchemaDefRequirement,
    SecondaryFile,
    ShellCommandRequirement,
    SoftwarePackage,
    SoftwareRequirement,
    ToolTimeLimit,
    WorkReuse,
    secondary_file,
)
from ._cwl_builder_support import (
    _validate_path,
    _SUPPORT,
    _contains_expression,
    _merge_if_set,
    _normalize_requirement,
    _render,
    _render_doc,
    _sanitize_raw_mapping,
    _warn_raw_escape_hatch,
    CWLBuilderValidationError,
    ValidationResult,
    validate_cwl_document,
)

if TYPE_CHECKING:
    from .api import Step


@dataclass(slots=True)
# pylint: disable=too-many-instance-attributes,too-many-public-methods
class CommandLineTool:
    """Declarative CWL CommandLineTool authoring object."""

    name: str
    inputs: Inputs
    outputs: Outputs
    cwl_version: str = "v1.2"
    label_text: str | None = None
    doc_text: str | list[str] | None = None
    _base_command: list[str] = field(default_factory=list)
    _arguments: list[str | dict[str, Any]] = field(default_factory=list)
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

    def __post_init__(self) -> None:
        if not isinstance(self.inputs, Inputs):
            raise TypeError("inputs must be an Inputs(...) collection")
        if not isinstance(self.outputs, Outputs):
            raise TypeError("outputs must be an Outputs(...) collection")

    def _store_requirement(
        self,
        bucket: dict[str, dict[str, Any]],
        requirement: Any,
        value: dict[str, Any] | None,
    ) -> None:
        class_name, payload = _normalize_requirement(requirement, value)
        if ":" in class_name:
            prefix, _ = class_name.split(":", 1)
            if prefix in _SUPPORT.known_namespaces and prefix not in self._namespaces:
                self._namespaces[prefix] = _SUPPORT.known_namespaces[prefix]
        bucket[class_name] = payload

    def _apply_spec(self, spec: Any, *, as_hint: bool) -> CommandLineTool:
        self._store_requirement(self._hints if as_hint else self._requirements, spec, None)
        return self

    def _append_requirement_entry(
        self,
        class_name: str,
        list_key: str,
        item: Any,
        *,
        as_hint: bool = False,
    ) -> CommandLineTool:
        target = self._hints if as_hint else self._requirements
        payload = target.setdefault(class_name, {list_key: []})
        listing = payload.setdefault(list_key, [])
        if not isinstance(listing, list):
            raise TypeError(f"{class_name} {list_key} must be a list")
        listing.append(_render(item))
        return self

    def describe(
        self,
        label: str | None = None,
        doc: str | list[str] | None = None,
    ) -> CommandLineTool:
        if label is not None:
            self.label_text = label
        if doc is not None:
            self.doc_text = doc
        return self

    def label(self, text: str) -> CommandLineTool:
        self.label_text = text
        return self

    def doc(self, text: str | list[str]) -> CommandLineTool:
        self.doc_text = text
        return self

    def namespace(self, prefix: str, iri: str | None = None) -> CommandLineTool:
        namespace_iri = iri if iri is not None else _SUPPORT.known_namespaces.get(prefix)
        if namespace_iri is None:
            raise ValueError(
                f"Unknown namespace prefix {prefix!r}; please provide an explicit iri"
            )
        self._namespaces[prefix] = namespace_iri
        return self

    def schema(self, iri: str) -> CommandLineTool:
        schema_iri = _SUPPORT.known_schemas.get(iri, iri)
        if schema_iri not in self._schemas:
            self._schemas.append(schema_iri)
        return self

    def edam(self) -> CommandLineTool:
        return self.namespace("edam").schema("edam")

    def intent(self, *identifiers: str) -> CommandLineTool:
        self._intent.extend(identifiers)
        return self

    def base_command(self, *parts: str) -> CommandLineTool:
        self._base_command = list(parts)
        return self

    def stdin(self, value: str) -> CommandLineTool:
        self._stdin = value
        return self

    def stdout(self, value: str) -> CommandLineTool:
        self._stdout = value
        return self

    def stderr(self, value: str) -> CommandLineTool:
        self._stderr = value
        return self

    def add_argument(
        self,
        argument: str | CommandArgument | dict[str, Any],
    ) -> CommandLineTool:
        match argument:
            case str() as literal:
                self._arguments.append(literal)
            case CommandArgument() as structured:
                self._arguments.append(structured.to_yaml())
            case dict() as raw:
                _warn_raw_escape_hatch("add_argument()")
                self._arguments.append(
                    _sanitize_raw_mapping(raw, context="raw argument mapping")
                )
            case _:
                raise TypeError("argument must be a string, CommandArgument, or raw dict")
        return self

    def argument(self, value: Any = None, **kwargs: Any) -> CommandLineTool:
        binding_extra = dict(kwargs.pop("binding_extra", {}) or {})
        argument_extra = dict(kwargs.pop("extra", {}) or {})
        binding = CommandLineBinding(extra=binding_extra, **kwargs)
        return self.add_argument(
            CommandArgument(value=value, binding=binding, extra=argument_extra)
        )

    def requirement(self, requirement: Any, value: dict[str, Any] | None = None) -> CommandLineTool:
        self._store_requirement(self._requirements, requirement, value)
        return self

    def hint(self, requirement: Any, value: dict[str, Any] | None = None) -> CommandLineTool:
        self._store_requirement(self._hints, requirement, value)
        return self

    def docker(
        self,
        image: str | None = None,
        *,
        as_hint: bool = False,
        **kwargs: Any,
    ) -> CommandLineTool:
        return self._apply_spec(
            DockerRequirement(
                docker_pull=kwargs.pop("docker_pull", None) or image,
                extra=dict(kwargs.pop("extra", {}) or {}),
                **kwargs,
            ),
            as_hint=as_hint,
        )

    def inline_javascript(
        self,
        *expression_lib: str,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        return self._apply_spec(
            InlineJavascriptRequirement(list(expression_lib) or None, dict(extra or {})),
            as_hint=as_hint,
        )

    def schema_definitions(
        self,
        *types: Any,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        return self._apply_spec(
            SchemaDefRequirement(list(types), dict(extra or {})),
            as_hint=as_hint,
        )

    def load_listing(
        self,
        value: str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        return self._apply_spec(LoadListingRequirement(value, dict(extra or {})), as_hint=as_hint)

    def shell_command(
        self,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        return self._apply_spec(ShellCommandRequirement(dict(extra or {})), as_hint=as_hint)

    def software(
        self,
        packages: list[SoftwarePackage | dict[str, Any]],
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        return self._apply_spec(SoftwareRequirement(packages, dict(extra or {})), as_hint=as_hint)

    def initial_workdir(
        self,
        listing: Any,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        return self._apply_spec(InitialWorkDirRequirement(listing, dict(extra or {})), as_hint=as_hint)

    # This helper deliberately bundles the common staging knobs into one call.
    # The slightly wider signature is easier to use than forcing nested objects.
    def stage(  # pylint: disable=too-many-arguments
        self,
        reference: Any,
        *,
        writable: bool = False,
        entryname: str | None = None,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        return self._append_requirement_entry(
            "InitialWorkDirRequirement",
            "listing",
            Dirent.from_input(
                reference,
                writable=writable,
                entryname=entryname,
                extra=extra,
            ).to_dict(),
            as_hint=as_hint,
        )

    def env_var(self, name: str, value: str, *, as_hint: bool = False) -> CommandLineTool:
        return self._append_requirement_entry(
            "EnvVarRequirement",
            "envDef",
            EnvironmentDef(name, value).to_dict(),
            as_hint=as_hint,
        )

    def resources(
        self,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> CommandLineTool:
        cores_min = kwargs.pop("cores_min", None)
        cores = kwargs.pop("cores", None)
        ram_min = kwargs.pop("ram_min", None)
        ram = kwargs.pop("ram", None)
        tmpdir_min = kwargs.pop("tmpdir_min", None)
        tmpdir = kwargs.pop("tmpdir", None)
        outdir_min = kwargs.pop("outdir_min", None)
        outdir = kwargs.pop("outdir", None)
        aliases = {
            "cores_min": cores if cores_min is None else cores_min,
            "ram_min": ram if ram_min is None else ram_min,
            "tmpdir_min": tmpdir if tmpdir_min is None else tmpdir_min,
            "outdir_min": outdir if outdir_min is None else outdir_min,
        }
        aliases.update(kwargs)
        return self._apply_spec(
            ResourceRequirement(extra=dict(extra or {}), **aliases),
            as_hint=as_hint,
        )

    # GPU hints naturally need a few related knobs, so this stays slightly wide.
    def gpu(  # pylint: disable=too-many-arguments
        self,
        *,
        cuda_version_min: str | None = None,
        compute_capability: str | None = None,
        device_count_min: int | str | None = None,
        as_hint: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        payload: dict[str, Any] = {}
        _merge_if_set(payload, "cudaVersionMin", cuda_version_min)
        _merge_if_set(payload, "cudaComputeCapability", compute_capability)
        _merge_if_set(payload, "cudaDeviceCountMin", device_count_min)
        payload.update(_render(extra or {}))
        if as_hint:
            return self.hint("cwltool:CUDARequirement", payload)
        return self.requirement("cwltool:CUDARequirement", payload)

    def work_reuse(
        self,
        enable: bool | str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        return self._apply_spec(WorkReuse(enable, dict(extra or {})), as_hint=as_hint)

    def network_access(
        self,
        enable: bool | str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        return self._apply_spec(NetworkAccess(enable, dict(extra or {})), as_hint=as_hint)

    def inplace_update(
        self,
        enable: bool = True,
        *,
        as_hint: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        return self._apply_spec(
            InplaceUpdateRequirement(enable, dict(extra or {})),
            as_hint=as_hint,
        )

    def time_limit(
        self,
        seconds: int | str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> CommandLineTool:
        return self._apply_spec(ToolTimeLimit(seconds, dict(extra or {})), as_hint=as_hint)

    def success_codes(self, *codes: int) -> CommandLineTool:
        self._success_codes = list(codes)
        return self

    def temporary_fail_codes(self, *codes: int) -> CommandLineTool:
        self._temporary_fail_codes = list(codes)
        return self

    def permanent_fail_codes(self, *codes: int) -> CommandLineTool:
        self._permanent_fail_codes = list(codes)
        return self

    def extra(self, **values: Any) -> CommandLineTool:
        _warn_raw_escape_hatch("extra()")
        self._extra.update(
            _sanitize_raw_mapping(
                values,
                context="extra()",
                reserved_keys=set(_SUPPORT.reserved_document_keys),
            )
        )
        return self

    def to_step(
        self,
        *,
        step_name: str | None = None,
        run_path: str | Path | None = None,
        config: dict[str, Any] | None = None,
        tool_registry: Tools | None = None,
    ) -> Step:
        """Convert this built CLT into an in-memory workflow `Step`.

        Args:
            step_name (str | None): Optional workflow step name override.
            run_path (str | Path | None): Optional virtual `.cwl` path for compiler bookkeeping.
            config (dict[str, Any] | None): Optional input values to pre-bind.
            tool_registry (Tools | None): Optional tool registry retained on the step.

        Returns:
            Step: A workflow step backed by this CLT without writing to disk.
        """
        return step_from_command_line_tool(
            self,
            step_name=step_name,
            run_path=run_path,
            config=config,
            tool_registry=tool_registry,
        )

    def build(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "class": "CommandLineTool",
            "cwlVersion": self.cwl_version,
            "id": self.name,
            "inputs": self.inputs.to_dict(),
            "outputs": self.outputs.to_dict(),
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
            document["baseCommand"] = (
                self._base_command[0]
                if len(self._base_command) == 1
                else list(self._base_command)
            )
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
        if _contains_expression(document):
            requirements = document.setdefault("requirements", {})
            if (
                "InlineJavascriptRequirement" not in requirements
                and "InlineJavascriptRequirement" not in document.get("hints", {})
            ):
                requirements["InlineJavascriptRequirement"] = {}
        return document

    def to_dict(self) -> dict[str, Any]:
        return self.build()

    def to_yaml(self) -> str:
        return str(yaml.safe_dump(self.build(), sort_keys=False, line_break="\n"))

    def save(self, path: str | Path, *, validate: bool = False, skip_schemas: bool = False) -> Path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(self.to_yaml(), encoding="utf-8")
        if validate:
            _validate_path(output_path, skip_schemas=skip_schemas)
        return output_path

    def validate(self, *, skip_schemas: bool = False) -> ValidationResult:
        return validate_cwl_document(self.build(), filename=f"{self.name}.cwl", skip_schemas=skip_schemas)


def array_type(items: Any) -> dict[str, Any]:
    """Return a CWL array type expression."""
    return cwl.array(items)


def enum_type(*symbols: str, name: str | None = None) -> dict[str, Any]:
    """Return a CWL enum type expression."""
    return cwl.enum(*symbols, name=name)


def record_type(
    fields: Mapping[str, FieldSpec] | list[FieldSpec | dict[str, Any]],
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Return a CWL record type expression."""
    return cwl.record(fields, name=name)


def record_field(type_: Any, **kwargs: Any) -> FieldSpec:
    """Return a named CWL record field helper."""
    return Field(type_, **kwargs)


def step_from_command_line_tool(
    tool: CommandLineTool,
    *,
    step_name: str | None = None,
    run_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
    tool_registry: Tools | None = None,
) -> Step:
    """Convert a built CLT into a workflow `Step` entirely in memory.

    Args:
        tool (CommandLineTool): Built CLT to wrap as a workflow step.
        step_name (str | None): Optional workflow step name override.
        run_path (str | Path | None): Optional virtual `.cwl` path for compiler bookkeeping.
        config (dict[str, Any] | None): Optional input values to pre-bind.
        tool_registry (Tools | None): Optional tool registry retained on the step.

    Returns:
        Step: A workflow step backed by the CLT without touching disk.
    """
    from ._cwl_builder_step_bridge import (  # pylint: disable=import-outside-toplevel
        step_from_command_line_tool as _step_from_command_line_tool,
    )

    return _step_from_command_line_tool(
        tool,
        step_name=step_name,
        run_path=run_path,
        config=config,
        tool_registry=tool_registry,
    )


__all__ = [
    "CWLBuilderValidationError",
    "CommandArgument",
    "CommandLineBinding",
    "CommandLineTool",
    "CommandOutputBinding",
    "Dirent",
    "DockerRequirement",
    "EnvironmentDef",
    "EnvVarRequirement",
    "Field",
    "FieldSpec",
    "InitialWorkDirRequirement",
    "InlineJavascriptRequirement",
    "InplaceUpdateRequirement",
    "Input",
    "InputSpec",
    "Inputs",
    "LoadListingRequirement",
    "NetworkAccess",
    "Output",
    "OutputSpec",
    "Outputs",
    "ResourceRequirement",
    "SchemaDefRequirement",
    "SecondaryFile",
    "ShellCommandRequirement",
    "SoftwarePackage",
    "SoftwareRequirement",
    "ToolTimeLimit",
    "ValidationResult",
    "WorkReuse",
    "array_type",
    "cwl",
    "enum_type",
    "record_field",
    "record_type",
    "secondary_file",
    "step_from_command_line_tool",
    "validate_cwl_document",
]
