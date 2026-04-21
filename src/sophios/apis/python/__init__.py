"""Python workflow and CWL builder API exports."""

from importlib import import_module
from typing import TYPE_CHECKING, Any


_API_EXPORTS = {
    "InvalidLinkError",
    "InvalidStepError",
    "MissingRequiredValueError",
    "Step",
    "Workflow",
}

_ERROR_EXPORTS = {
    "InvalidCLTError",
    "InvalidInputValueError",
    "InvalidLinkError",
    "InvalidStepError",
    "MissingRequiredValueError",
}

_CWL_BUILDER_EXPORTS = {
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
}

__all__ = sorted(_API_EXPORTS | _ERROR_EXPORTS | _CWL_BUILDER_EXPORTS)


if TYPE_CHECKING:
    from ._errors import (
        InvalidCLTError,
        InvalidInputValueError,
        InvalidLinkError,
        InvalidStepError,
        MissingRequiredValueError,
    )
    from .api import (
        Step,
        Workflow,
    )
    from .cwl_builder import (
        CWLBuilderValidationError,
        CommandArgument,
        CommandLineBinding,
        CommandLineTool,
        CommandOutputBinding,
        Dirent,
        DockerRequirement,
        EnvironmentDef,
        EnvVarRequirement,
        Field,
        FieldSpec,
        InitialWorkDirRequirement,
        InlineJavascriptRequirement,
        InplaceUpdateRequirement,
        Input,
        InputSpec,
        Inputs,
        LoadListingRequirement,
        NetworkAccess,
        Output,
        OutputSpec,
        Outputs,
        ResourceRequirement,
        SchemaDefRequirement,
        SecondaryFile,
        ShellCommandRequirement,
        SoftwarePackage,
        SoftwareRequirement,
        ToolTimeLimit,
        ValidationResult,
        WorkReuse,
        array_type,
        cwl,
        enum_type,
        record_field,
        record_type,
        secondary_file,
        step_from_command_line_tool,
        validate_cwl_document,
    )


def __getattr__(name: str) -> Any:
    if name in _ERROR_EXPORTS:
        module = import_module("._errors", __name__)
        return getattr(module, name)
    if name in _API_EXPORTS:
        module = import_module(".api", __name__)
        return getattr(module, name)
    if name in _CWL_BUILDER_EXPORTS:
        module = import_module(".cwl_builder", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
