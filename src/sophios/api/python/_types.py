"""CWL type definitions used by the Python APIs."""

from enum import Enum


class CWLAtomicType(str, Enum):
    """Atomic CWL type names.

    These are the string-valued leaf types that may appear directly in CWL
    input/output declarations. Structured types such as arrays, enums, and
    records are represented separately as schema objects.
    """

    NULL = "null"
    BOOLEAN = "boolean"
    INT = "int"
    LONG = "long"
    FLOAT = "float"
    DOUBLE = "double"
    STRING = "string"
    FILE = "File"
    DIRECTORY = "Directory"
    ANY = "Any"


class ScatterMethod(str, Enum):
    dotproduct = "dotproduct"
    flat_crossproduct = "flat_crossproduct"
    nested_crossproduct = "nested_crossproduct"
