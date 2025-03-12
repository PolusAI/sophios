"""Metadata Model."""

import re
from functools import singledispatchmethod
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    EmailStr,
    Field,
    RootModel,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from typing_extensions import Annotated


class Author(RootModel):
    """Author object."""

    root: str

    @field_validator("root")
    @classmethod
    def check_author(cls: Any, value: Any) -> Any:
        """Check the author follows the correct format."""
        if not len(value.split(" ")) == 2:
            raise ValueError(
                "The author must be in the format <first name> <last name>"
            )
        return value

    def __repr__(self) -> str:
        """Repr."""
        return self.root

    def __str__(self) -> str:
        """Str."""
        return self.root

    @singledispatchmethod
    def __eq__(self, other: Any) -> bool:  # type: ignore
        """Compare if two Author objects are equal."""
        msg = "invalid type for comparison."
        raise TypeError(msg)


@Author.__eq__.register(str)  # type: ignore # pylint: disable=no-member
def _(self: Author, other: Author) -> Any:
    return self.root == other


@Author.__eq__.register(Author)  # type: ignore # pylint: disable=no-member
def _(self: Author, other: Author) -> Any:
    return self.root == other.root


class DOI(RootModel):
    """DOI object."""

    root: str

    @field_validator("root")
    @classmethod
    def check_doi(cls: Any, value: Any) -> Any:
        """Check the doi follows the correct format."""
        if not value.startswith("10."):
            raise ValueError("The DOI must start with 10.")
        if not len(value.split("/")) == 2:
            raise ValueError("The DOI must be in the format <prefix>/<suffix>")
        return value

    def __repr__(self) -> str:
        """Repr."""
        return self.root

    def __str__(self) -> str:
        """Str."""
        return self.root

    @singledispatchmethod
    def __eq__(self, other: Any) -> bool:  # type: ignore
        """Compare if two DOI objects are equal."""
        msg = "invalid type for comparison."
        raise TypeError(msg)


@DOI.__eq__.register(str)  # type: ignore # pylint: disable=no-member
def _(self: Author, other: str) -> Any:
    return self.root == other


@DOI.__eq__.register(DOI)  # type: ignore # pylint: disable=no-member
def _(self: Author, other: DOI) -> Any:
    return self.root == other.root


EntrypointPath = Annotated[Path, WithJsonSchema({"type": "string", "format": "uri"})]


class Metadata(BaseModel):
    """Metadata BaseModel."""

    name: str = Field(
        description=(
            "Unique identifier for ICT tool scoped on organization or user,"
            "should take the format <organization/user>/<ICT name>."
        ),
        examples=["wipp/threshold"],
    )
    container: str = Field(
        description=(
            "Direct link to hosted ICT container image, should take the format"
            "<registry path>/<image repository>:<tag>, registry path may be omitted"
            "and will default to Docker Hub."
        ),
        examples=["wipp/threshold:1.1.1"],
    )
    entrypoint: Union[EntrypointPath, str] = Field(
        description="Absolute path to initial script or command within packaged image."
    )
    title: Optional[str] = Field(
        None,
        description="(optional) Descriptive human-readable name, will default to `name` if omitted.",
        examples=["Thresholding Plugin"],
    )
    description: Optional[str] = Field(
        None,
        description="(optional) Brief description of plugin.",
        examples=["Thresholding methods from ImageJ"],
    )
    author: list[Author] = Field(
        description=(
            "Comma separated list of authors, each author name should take the format"
            "<first name> <last name>."
        ),
        examples=["Mohammed Ouladi"],
    )
    contact: Union[EmailStr, AnyHttpUrl] = Field(
        description="Email or link to point of contact (ie. GitHub user page) for questions or issues.",
        examples=["mohammed.ouladi@labshare.org"],
    )
    repository: AnyHttpUrl = Field(
        description="Url for public or private repository hosting source code.",
        examples=["https://github.com/polusai/polus-plugins"],
    )
    documentation: Optional[AnyHttpUrl] = Field(
        None,
        description="Url for hosted documentation about using or modifying the plugin.",
    )
    citation: Optional[DOI] = Field(
        None,
        description="DOI link to relevant citation, plugin user should use this citation when using this plugin.",
    )

    @field_validator("name")
    @classmethod
    def check_name(cls: Any, value: Any) -> Any:
        """Check the name follows the correct format."""
        if not len(value.split("/")) in [2, 3]:
            raise ValueError(
                "The name must be in the format <organization/user>/<ICT name>"
            )
        return value

    @field_validator("container")
    @classmethod
    def check_container(cls: Any, value: Any) -> Any:
        """Check the container follows the correct format."""
        if not bool(
            re.match(r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+:[a-zA-Z0-9_\.\-]+$", value)
        ):
            raise ValueError(
                "The name must be in the format <registry path>/<image repository>:<tag>"
            )
        return value

    @model_validator(mode="after")
    def default_title(self) -> Any:
        """Set the title to the name if not provided."""
        if self.title is None:
            self.title = self.name
        return self
