"""CWL generation for ICT objects."""

from collections.abc import Iterable
from typing import Any, TYPE_CHECKING, cast

from sophios.api.utils.ict.ict_spec.hardware import HardwareRequirements
from sophios.api.utils.ict.ict_spec.io import IO
from sophios.api.utils.ict.ict_spec.ui import UIItem

if TYPE_CHECKING:
    from sophios.api.utils.ict.ict_spec.model import ICT


def requirements(ict_: "ICT", network_access: bool) -> dict[str, Any]:
    """Return the requirements from an ICT object."""
    reqs: dict[str, Any] = {}
    reqs["DockerRequirement"] = {"dockerPull": ict_.container}
    output_names = [io.name for io in ict_.outputs]
    if "outDir" in output_names:
        reqs["InitialWorkDirRequirement"] = {
            "listing": [{"entry": "$(inputs.outDir)", "writable": True}]
        }
        reqs["InlineJavascriptRequirement"] = {}
    if network_access:
        reqs["NetworkAccess"] = {"networkAccess": True}
    return reqs


def clt_dict(ict_: "ICT", network_access: bool) -> dict[str, Any]:
    """Return a dict of a CommandLineTool from an ICT object."""

    clt_: dict[str, Any] = {
        "class": "CommandLineTool",
        "cwlVersion": "v1.2",
        "inputs": {
            io.name: io._input_to_cwl()  # pylint: disable=W0212
            for io in ict_.inputs + ict_.outputs
        },
        "outputs": {
            io.name: io._output_to_cwl(
                [io.name for io in ict_.outputs]
            )  # pylint: disable=W0212
            for io in ict_.outputs
        },
        "requirements": requirements(ict_, network_access),
        "baseCommand": [],
        "label": ict_.title,
        "doc": str(ict_.documentation),
    }

    return clt_


def remove_none(value: Any) -> Any:
    """Recursively remove keys with None values."""
    match value:
        case dict():
            return {key: remove_none(item) for key, item in value.items() if item is not None}
        case list():
            return [remove_none(item) for item in value if item is not None]
        case _:
            return value


def input_output_dict(parameters: Iterable[IO]) -> dict[str, Any]:
    """Return a input or output dictionary from an ICT object."""
    io_dict: dict[str, Any] = {}
    for prop in parameters:
        io_dict[prop.name] = {
            "type": prop.io_type.value,
            "description": prop.description,
            "defaultValue": prop.defaultValue,
            "required": prop.required,
            "format": prop.io_format,
        }
    # recursively remove keys with None values
    return cast(dict[str, Any], remove_none(io_dict))


def ui_dict(items: Iterable[UIItem] | None) -> list[dict[str, Any]]:
    """Return a CommandLineTool from an ICT object."""
    ui_list: list[dict[str, Any]] = []
    if items is None:
        return ui_list
    for prop in items:
        prop_dict: dict[str, Any] = {
            "key": prop.key.root,
            "title": prop.title,
            "description": prop.description,
            "type": prop.ui_type,
        }
        if prop.customType:
            prop_dict["customType"] = prop.customType
        if prop.condition:
            prop_dict["condition"] = prop.condition.root
        if prop.ui_type == "select":
            prop_dict["fields"] = prop.fields
        ui_list.append(prop_dict)
    return ui_list


def hardware_dict(requirements_: HardwareRequirements) -> dict[str, Any]:
    """Return a CommandLineTool from an ICT object."""
    cpu = requirements_.cpu
    memory = requirements_.memory
    gpu = requirements_.gpu
    hardware = {
        "cpu.type": None if cpu is None else cpu.cpu_type,
        "cpu.min": None if cpu is None else cpu.cpu_min,
        "cpu.recommended": None if cpu is None else cpu.cpu_recommended,
        "memory.min": None if memory is None else memory.memory_min,
        "memory.recommended": None if memory is None else memory.memory_recommended,
        "gpu.enabled": None if gpu is None else gpu.gpu_enabled,
        "gpu.required": None if gpu is None else gpu.gpu_required,
        "gpu.type": None if gpu is None else gpu.gpu_type,
    }
    return cast(dict[str, Any], remove_none(hardware))


def ict_dict(ict_: "ICT") -> dict[str, Any]:
    """Return a CommandLineTool from an ICT object."""
    inputs_dict = input_output_dict(ict_.inputs)
    outputs_dict = input_output_dict(ict_.outputs)
    clt_ = {
        "inputs": inputs_dict,
        "outputs": outputs_dict,
        "ui": ui_dict(ict_.ui),
    }
    if ict_.hardware is not None:
        clt_["hardware"] = hardware_dict(ict_.hardware)
    return clt_
