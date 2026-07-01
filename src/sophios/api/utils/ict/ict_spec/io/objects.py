"""IO objects for ICT."""

import enum
from typing import Optional, Union, Any

from pydantic import BaseModel, Field
from sophios.api.utils.wfb_util import is_directory


CWL_IO_DICT: dict[str, str] = {
    "string": "string",
    "number": "double",
    "array": "string",
    "boolean": "boolean",
    # ICT path parameters do not distinguish File and Directory.
}


class TypesEnum(str, enum.Enum):
    """Types enum for ICT IO."""

    STRING = "string"
    NUMBER = "number"
    ARRAY = "array"
    BOOLEAN = "boolean"
    PATH = "path"


def _get_cwl_type(io_type: str) -> str:
    """Return the CWL type from the ICT IO type."""
    if io_type == "path":
        # ICT path inputs do not distinguish File and Directory; keep the historical Directory default.
        return "Directory"
    return CWL_IO_DICT[io_type]


class IO(BaseModel):
    """IO BaseModel."""

    name: str = Field(
        description=(
            "Unique input or output name for this plugin, case-sensitive match to"
            "corresponding variable expected by tool."
        ),
        examples=["thresholdtype"],
    )
    io_type: TypesEnum = Field(
        ...,
        alias="type",
        description="Defines the parameter passed to the ICT tool based on broad categories of basic types.",
        examples=["string"],
    )
    description: Optional[str] = Field(
        None,
        description="Short text description of expected value for field.",
        examples=["Algorithm type for thresholding"],
    )
    defaultValue: Optional[Any] = Field(
        None,
        description="Optional default value.",
        examples=["42"],
    )
    required: bool = Field(
        description="Boolean (true/false) value indicating whether this "
        + "field needs an associated value.",
        examples=["true"],
    )
    io_format: Union[list[str], dict] = Field(
        ...,
        alias="format",
        description="Defines the actual value(s) that the input/output parameter"
        + "represents using an ontology schema.",
    )

    @property
    def _is_optional(self) -> str:
        """Return '' if required, '?' if default exists, else '?'."""
        if self.defaultValue is not None:
            return "?"
        if self.required:
            return ""

        return "?"

    def convert_uri_format(self, uri_format: Any) -> str:
        """Convert to cwl format
        Args:
            format (_type_): _description_
        """
        return f"edam:format_{uri_format.split('_')[-1]}"

    def _input_to_cwl(self) -> dict:
        """Convert inputs to CWL."""
        cwl_dict_ = {
            "inputBinding": {"prefix": f"--{self.name}"},
            "type": f"{_get_cwl_type(self.io_type)}{self._is_optional}",
        }

        if (
            isinstance(self.io_format, dict)
            and self.io_format.get("uri", None) is not None  # pylint: disable=no-member
        ):
            # pylint: disable-next=unsubscriptable-object
            cwl_dict_["format"] = self.convert_uri_format(self.io_format["uri"])
        if self.defaultValue is not None:
            cwl_dict_["default"] = self.defaultValue
        return cwl_dict_

    def _output_to_cwl(self, inputs: Any) -> dict:
        """Convert outputs to CWL."""
        if self.io_type == "path":
            if self.name in inputs:
                if is_directory(dict(self)):
                    cwl_type = "Directory"
                else:
                    cwl_type = "File"

                cwl_dict_ = {
                    "outputBinding": {"glob": f"$(inputs.{self.name}.basename)"},
                    "type": cwl_type,
                }
                if (
                    not isinstance(self.io_format, list)
                    and self.io_format.get("uri", None)
                    is not None  # pylint: disable=no-member
                ):
                    # pylint: disable-next=unsubscriptable-object
                    cwl_dict_["format"] = self.convert_uri_format(self.io_format["uri"])
                return cwl_dict_

        raise NotImplementedError(f"Output not supported {self.name}")
