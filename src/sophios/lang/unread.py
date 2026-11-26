"""A field a CWL value writes that no node holds a value of, with the refusal it earns.

The front door reads a CWL value into a node and names, for each field the node holds no value of, the refusal a
target that cannot run it gives; the target refuses item by item and reads no CWL. Nothing here reads CWL.
"""
from dataclasses import dataclass

from .error_codes import SophiosErrorCode


@dataclass(frozen=True, slots=True)
class UnreadField:
    """The field `name` of a CWL `owner` (the class it is written in, for a label), which no node holds a value of,
    and what refusing it says: the `code` and the `note` of the finding, which is at the field."""

    name: str
    owner: str
    code: SophiosErrorCode
    note: str
