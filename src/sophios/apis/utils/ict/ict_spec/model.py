# pylint: disable=no-member, no-name-in-module, import-error
"""ICT model."""

import logging
from pathlib import Path
from typing import Optional, TypeVar

import yaml
from pydantic import model_validator

from sophios.apis.utils.ict.ict_spec.hardware import HardwareRequirements
from sophios.apis.utils.ict.ict_spec.io import IO
from sophios.apis.utils.ict.ict_spec.metadata import Metadata
from sophios.apis.utils.ict.ict_spec.tools import clt_dict, ict_dict
from sophios.apis.utils.ict.ict_spec.ui import UIItem

StrPath = TypeVar("StrPath", str, Path)

logger = logging.getLogger("ict")


class ICT(Metadata):
    """ICT object."""

    inputs: list[IO]
    outputs: list[IO]
    ui: Optional[list[UIItem]] = None
    hardware: Optional[HardwareRequirements] = None

    @model_validator(mode="after")
    def validate_ui(self) -> "ICT":
        """Validate that the ui matches the inputs and outputs."""
        if self.ui is not None:
            io_dict = {"inputs": [], "outputs": []}  # type: ignore
            ui_keys = [ui.key.root.split(".") for ui in self.ui]
            for ui_ in ui_keys:
                io_dict[ui_[0]].append(ui_[1])
            input_names = [io.name for io in self.inputs]
            output_names = [io.name for io in self.outputs]
            inp_bool = [x in input_names for x in io_dict["inputs"]]
            out_bool = [x in output_names for x in io_dict["outputs"]]

            # if not all(inp_bool):
            #     raise ValueError(
            #         f"The ui keys must match the inputs and outputs keys. Unmatched: inputs.{set(io_dict['inputs'])-set(input_names)}"
            #     )
            # if not all(out_bool):
            #     raise ValueError(
            #         f"The ui keys must match the inputs and outputs keys. Unmatched: outputs.{set(io_dict['outputs'])-set(output_names)}"
            #     )

        return self

    def to_clt(self, network_access: bool = False) -> dict:
        """Convert ICT to CWL CommandLineTool.


        Args:
            network_access: bool
                Default is `False`. If set to `True`, the
                requirements of the CLT will include
                `networkAccess`: `True`.

        Returns: `dict` representation of the CLT.
        """
        return clt_dict(self, network_access)

    @property
    def clt(self) -> dict:
        """CWL CommandLineTool from an ICT object."""
        return clt_dict(self, network_access=False)

    @property
    def ict(self) -> dict:
        """ICT yaml from an ICT object."""
        return ict_dict(self)

    def save_clt(self, cwl_path: StrPath, network_access: bool = False) -> Path:
        """Save the ICT as CommandLineTool to a file."""
        assert (
            str(cwl_path).rsplit(".", maxsplit=1)[-1] == "cwl"
        ), "Path must end in .cwl"
        with Path(cwl_path).open("w", encoding="utf-8") as file:
            yaml.dump(self.to_clt(network_access), file)
        return Path(cwl_path)

    def save_cwl(self, cwl_path: StrPath, network_access: bool = False) -> Path:
        """Save the ICT as CommandLineTool to a file.

        Alias for `save_clt`.
        """
        return self.save_clt(cwl_path, network_access)

    def save_yaml(self, yaml_path: StrPath) -> Path:
        """Save the ICT as yaml to a file."""
        assert str(yaml_path).rsplit(".", maxsplit=1)[-1] in [
            "yaml",
            "yml",
        ], "Path must end in .yaml or .yml"
        with Path(yaml_path).open("w", encoding="utf-8") as file:
            yaml.dump(
                self.model_dump(mode="json", exclude_none=True, by_alias=True), file
            )
        return Path(yaml_path)

    def save_yml(self, yml_path: StrPath) -> Path:
        """Save the ICT as yaml to a file.

        Alias for `save_yaml`.
        """
        return self.save_yaml(yml_path)
