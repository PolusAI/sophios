"""Source positions for Sophios documents.

Every AST node carries a span so a diagnostic can name a file, a line, and a
column instead of describing a schema violation somewhere in a generated
document.
"""
from dataclasses import dataclass
from typing import Self

import yaml


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A half-open region of a source file, with 1-based line and column.

    PyYAML reports 0-based marks; conversion happens once, here, so that every
    consumer sees the same convention an editor would display.
    """

    file: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    @classmethod
    def from_marks(cls, file: str, start: yaml.Mark, end: yaml.Mark) -> Self:
        """Build a span from a PyYAML node's start and end marks."""
        return cls(
            file=file,
            start_line=start.line + 1,
            start_column=start.column + 1,
            end_line=end.line + 1,
            end_column=end.column + 1,
        )

    @classmethod
    def of(cls, file: str, node: yaml.nodes.Node) -> Self:
        """Build a span covering a composed YAML node."""
        return cls.from_marks(file, node.start_mark, node.end_mark)

    def __str__(self) -> str:
        return f'{self.file}:{self.start_line}:{self.start_column}'
