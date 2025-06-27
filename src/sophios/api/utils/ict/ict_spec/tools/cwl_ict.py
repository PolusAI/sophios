"""CWL generation for ICT objects.""" ""
from typing import Union, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sophios.api.utils.ict.ict_spec.model import ICT


def requirements(ict_: "ICT", network_access: bool) -> dict:
    """Return the requirements from an ICT object."""
    reqs: Dict[Any, Any] = {}
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


def split_entrypoint_string(enrtypoint: str) -> list[str]:
    """Fix str to list of str for entrypoint/baseCommand"""
    list_of_str_entry = enrtypoint.split(' ')
    return list_of_str_entry


def clt_dict(ict_: "ICT", network_access: bool) -> dict:
    """Return a dict of a CommandLineTool from an ICT object."""

    clt_: Dict[Any, Any] = {
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


def remove_none(d: Union[dict, str]) -> Union[dict, str]:
    """Recursively remove keys with None values."""
    match d:
        case dict():
            return {k: remove_none(v) for k, v in d.items() if v is not None}
        case str():
            return d


def input_output_dict(ict_: "ICT") -> Union[dict, str]:
    """Return a input or output dictionary from an ICT object."""
    io_dict: Dict[Any, Any] = {}
    for prop in ict_:
        io_dict[prop.name] = {  # type: ignore
            "type": prop.io_type.value,  # type: ignore
            "description": prop.description,  # type: ignore
            "defaultValue": prop.defaultValue,  # type: ignore
            "required": prop.required,  # type: ignore
            "format": prop.io_format,  # type: ignore
        }
    # recursively remove keys with None values
    return remove_none(io_dict)


def ui_dict(ict_: "ICT") -> list:
    """Return a CommandLineTool from an ICT object."""
    ui_list = []
    for prop in ict_:
        prop_dict: Dict[Any, Any] = {
            "key": prop.key.root,  # type: ignore # Assuming 'root' attribute contains the actual key
            "title": prop.title,  # type: ignore
            "description": prop.description,  # type: ignore
            "type": prop.ui_type,  # type: ignore
        }
        if prop.customType:  # type: ignore
            prop_dict["customType"] = prop.customType  # type: ignore
        if prop.condition:  # type: ignore
            prop_dict["condition"] = prop.condition.root  # type: ignore
        if prop.ui_type == "select":  # type: ignore
            prop_dict["fields"] = prop.fields  # type: ignore
        ui_list.append(prop_dict)
    return ui_list


def hardware_dict(ict_: "ICT") -> dict:
    """Return a CommandLineTool from an ICT object."""
    hardware_dict = {
        "cpu.type": ict_.cpu_type,  # type: ignore
        "cpu.min": ict_.cpu_min,  # type: ignore
        "cpu.recommended": ict_.cpu_recommended,  # type: ignore
        "memory.min": ict_.memory_min,  # type: ignore
        "memory.recommended": ict_.memory_recommended,  # type: ignore
        "gpu.enabled": ict_.gpu_enabled,  # type: ignore
        "gpu.required": ict_.gpu_required,  # type: ignore
        "gpu.type": ict_.gpu_type,  # type: ignore
    }
    return hardware_dict


def ict_dict(ict_: "ICT") -> dict:
    """Return a CommandLineTool from an ICT object."""
    inputs_dict = input_output_dict(ict_.inputs)  # type: ignore
    outputs_dict = input_output_dict(ict_.outputs)  # type: ignore
    clt_ = {
        "inputs": inputs_dict,
        "outputs": outputs_dict,
        "ui": ui_dict(ict_.ui),  # type: ignore
    }
    if ict_.hardware is not None:
        clt_["hardware"] = hardware_dict(ict_.hardware)  # type: ignore
    return clt_
