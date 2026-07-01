# pylint: disable=no-member
"""Hardware Requirements for ICT."""
from typing import Annotated, Optional, Union, Any

from pydantic import BaseModel, BeforeValidator, Field


def validate_str(s_t: Union[int, float, str]) -> Union[str, None]:
    """Return a string from int, float, or str."""
    if s_t is None:
        return None
    if isinstance(s_t, str):
        return s_t
    if not isinstance(s_t, (int, float)) or isinstance(s_t, bool):
        raise ValueError("must be an int, float, or str")
    return str(s_t)


StrInt = Annotated[str, BeforeValidator(validate_str)]


class CPU(BaseModel):
    """CPU object."""

    cpu_type: Optional[str] = Field(
        None,
        alias="type",
        description="Any non-standard or specific processor limitations.",
        examples=["arm64"],
    )
    cpu_min: Optional[StrInt] = Field(
        None,
        alias="min",
        description="Minimum requirement for CPU allocation where 1 CPU unit is equivalent to 1 physical CPU core or 1 virtual core.",
        examples=["100m"],
    )
    cpu_recommended: Optional[StrInt] = Field(
        None,
        alias="recommended",
        description="Recommended requirement for CPU allocation for optimal performance.",
        examples=["200m"],
    )


class Memory(BaseModel):
    """Memory object."""

    memory_min: Optional[StrInt] = Field(
        None,
        alias="min",
        description="Minimum requirement for memory allocation, measured in bytes.",
        examples=["129Mi"],
    )
    memory_recommended: Optional[StrInt] = Field(
        None,
        alias="recommended",
        description="Recommended requirement for memory allocation for optimal performance.",
        examples=["200Mi"],
    )


class GPU(BaseModel):
    """GPU object."""

    gpu_enabled: Optional[bool] = Field(
        None,
        alias="enabled",
        description="Boolean value indicating if the plugin is optimized for GPU.",
        examples=[False],
    )
    gpu_required: Optional[bool] = Field(
        None,
        alias="required",
        description="Boolean value indicating if the plugin requires a GPU to run.",
        examples=[False],
    )
    gpu_type: Optional[str] = Field(
        None,
        alias="type",
        description="	Any identifying label for GPU hardware specificity.",
        examples=["cuda11"],
    )


ATTRIBUTES = [
    "cpu_type",
    "cpu_min",
    "cpu_recommended",
    "memory_min",
    "memory_recommended",
    "gpu_enabled",
    "gpu_required",
    "gpu_type",
]


class HardwareRequirements(BaseModel):
    """HardwareRequirements object."""

    cpu: Optional[CPU] = Field(None, description="CPU requirements.")
    memory: Optional[Memory] = Field(None, description="Memory requirements.")
    gpu: Optional[GPU] = Field(None, description="GPU requirements.")

    def __getattribute__(self, name: str) -> Any:
        """Get attribute."""
        if name in ATTRIBUTES:
            return super().__getattribute__(name.split("_")[0]).__getattribute__(name)
        return super().__getattribute__(name)
