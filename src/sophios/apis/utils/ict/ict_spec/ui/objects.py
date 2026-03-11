"""UI objects."""

import enum
import re
from typing import Annotated, Literal, Optional, Union, Any

from pydantic import BaseModel, Field, RootModel, field_validator


class UIKey(RootModel):
    """UIKey object."""

    root: str

    @field_validator("root")
    @classmethod
    def check_ui_key(cls: Any, value: str) -> str:
        """Check the UI key follows the correct format."""
        sp_ = value.split(".")  # ruff: noqa: PLR2004
        if not len(sp_) == 2:
            raise ValueError(
                "The UI key must be in the format <inputs or outputs>.<parameter name>"
            )
        if not sp_[0] in ["inputs", "outputs"]:
            raise ValueError(
                "The UI key must be in the format <inputs or outputs>.<parameter name>"
            )
        return value

    def __repr__(self) -> str:
        """Repr."""
        return f"'{self.root}'"


class TypesEnum(str, enum.Enum):
    """Types enum."""

    TEXT = "text"
    NUMBER = "number"
    CHECKBOX = "checkbox"
    SELECT = "select"
    MULTISELECT = "multiselect"
    COLOR = "color"
    DATETIME = "datetime"
    PATH = "path"
    FILE = "file"


class ConditionalStatement(RootModel):
    """ConditionalStatement object."""

    root: str

    @field_validator("root")
    @classmethod
    def check_conditional_statement(cls: Any, value: str) -> str:
        """Check the conditional statement follows the correct format."""
        if not bool(
            re.match(r"^(inputs|outputs)\.\w+(==|!=|<|>|<=|>=|&&)'?\w+'?$", value)
        ):
            raise ValueError(
                "The conditional statement must be in the format <inputs or outputs>.<parameter name><operator><value>"
            )
        return value

    def __repr__(self) -> str:
        """Repr."""
        return f"'{self.root}'"


class UIBase(BaseModel):
    """UI BaseModel."""

    key: UIKey = Field(
        description="Identifier to connect UI configuration to specific parameter, "
        + "should take the form <inputs or outputs>.<parameter name>.",
        examples=["inputs.thresholdvalue"],
    )
    title: str = Field(
        description="User friendly label used in UI.",
        examples=["Thresholding Value"],
    )
    description: Optional[str] = Field(
        None,
        description="Short user friendly instructions for selecting appropriate parameter.",
        examples=["Enter a threshold value"],
    )
    customType: Optional[str] = Field(
        None, description="Optional label for a non-standard expected user interface."
    )
    condition: Optional[ConditionalStatement] = Field(
        None,
        json_schema_extra={"pattern": "^(inputs|outputs)\.\w+(==|!=|<|>|<=|>=|&&)\w+$"},
        description="Conditional statement that resolves to a boolean value based on UI configuration and selected value, "
        + "used to dictate relationship between parameters.",
        examples=["inputs.thresholdtype=='Manual'"],
    )


class UIText(UIBase, extra="forbid"):
    """Any arbitrary length string."""

    default: Optional[str] = Field(None, description="Prefilled value.")
    regex: Optional[str] = Field(None, description="Regular expression for validation.")
    toolbar: Optional[bool] = Field(
        None, description="Boolean value to add text formatting toolbar."
    )
    ui_type: Literal["text"] = Field(
        ...,
        alias="type",
        description="Defines the expected user interface based on a set of basic UI types.",
    )


class UINumber(UIBase, extra="forbid"):
    """Any numerical value."""

    default: Optional[Union[int, float]] = Field(None, description="Prefilled value.")
    integer: Optional[bool] = Field(
        None, description="Boolean value to force integers only."
    )
    number_range: Optional[tuple[Union[int, float], Union[int, float]]] = Field(
        None, alias="range", description="Minimum and maximum range as a tuple."
    )
    ui_type: Literal["number"] = Field(
        ...,
        alias="type",
        description="Defines the expected user interface based on a set of basic UI types.",
    )


class UICheckbox(UIBase, extra="forbid"):
    """Boolean operator, checked for `true` unchecked for `false`."""

    default: Optional[bool] = Field(
        None, description="Prefilled value, either `true` or `false`."
    )
    ui_type: Literal["checkbox"] = Field(
        ...,
        alias="type",
        description="Defines the expected user interface based on a set of basic UI types.",
    )


class UISelect(UIBase, extra="forbid"):
    """Single string value from a set of options."""

    fields: list[str] = Field(description="Required array of options.")
    optional: Optional[bool] = Field(None, description="Leave blank by default.")
    ui_type: Literal["select"] = Field(
        ...,
        alias="type",
        description="Defines the expected user interface based on a set of basic UI types.",
    )


class UIMultiselect(UIBase, extra="forbid"):
    """One or more string values from a set of options."""

    fields: list[str] = Field(description="Required array of options.")
    optional: Optional[bool] = Field(None, description="Leave blank by default.")
    limit: Optional[int] = Field(None, description="Maximum number of selections.")
    ui_type: Literal["multiselect"] = Field(
        ...,
        alias="type",
        description="Defines the expected user interface based on a set of basic UI types.",
    )


class UIColor(UIBase, extra="forbid"):
    """Color values passed as RGB color values."""

    fields: list[int] = Field(description="Array of preset RGB selections.")
    ui_type: Literal["color"] = Field(
        ...,
        alias="type",
        description="Defines the expected user interface based on a set of basic UI types.",
    )


class W3Format(str, enum.Enum):
    """W3Format enum."""

    YEAR = "YYYY"
    YEAR_MONTH = "YYYY-MM"
    COMPLETE_DATE = "YYYY-MM-DD"
    COMPLETE_DATE_TIME = "YYYY-MM-DDThh:mmTZD"
    COMPLETE_DATE_TIME_SEC = "YYYY-MM-DDThh:mm:ssTZD"
    COMPLETE_DATE_TIME_MS = "YYYY-MM-DDThh:mm:ss.sTZD"


class UIDatetime(UIBase, extra="forbid"):
    """Standardized date and time values."""

    w3_format: W3Format = Field(
        alias="format", description="Datetime format using W3C conventions."
    )
    ui_type: Literal["datetime"] = Field(
        ...,
        alias="type",
        description="Defines the expected user interface based on a set of basic UI types.",
    )


class UIPath(UIBase, extra="forbid"):
    """Absolute or relative path to file/directory using Unix conventions."""

    ext: Optional[list[str]] = Field(
        None, description="Array of allowed file extensions."
    )
    ui_type: Literal["path"] = Field(
        ...,
        alias="type",
        description="Defines the expected user interface based on a set of basic UI types.",
    )


class UIFile(UIBase, extra="forbid"):
    """User uploaded binary data."""

    ext: Optional[list[str]] = Field(
        None, description="Array of allowed file extensions."
    )
    limit: Optional[int] = Field(None, description="Maximum number of uploaded files.")
    size: Optional[int] = Field(None, description="Total size file limit.")
    ui_type: Literal["file"] = Field(
        ...,
        alias="type",
        description="Defines the expected user interface based on a set of basic UI types.",
    )


UIItem = Annotated[
    Union[
        UIText,
        UINumber,
        UICheckbox,
        UISelect,
        UIMultiselect,
        UIColor,
        UIDatetime,
        UIPath,
        UIFile,
    ],
    Field(discriminator="ui_type"),
]
