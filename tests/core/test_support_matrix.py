"""The support matrix is complete against the CWL schema and every row is pinned."""
import ast
from pathlib import Path

import pytest
from cwl_utils.parser import cwl_v1_2

from sophios.lang import SUPPORT_MATRIX, Support

pytestmark = pytest.mark.package

TESTS = Path(__file__).resolve().parent


@pytest.mark.fast
@pytest.mark.parametrize('class_name', sorted(SUPPORT_MATRIX))
def test_every_schema_field_is_classified(class_name: str) -> None:
    """A field the schema has and the matrix lacks, or one the schema dropped, fails here, and every row carries one
    of the three classifications."""
    schema_fields = set(getattr(cwl_v1_2, class_name).attrs)
    assert set(SUPPORT_MATRIX[class_name]) == schema_fields, (
        f'{class_name}: unclassified {schema_fields - set(SUPPORT_MATRIX[class_name])}, '
        f'stale {set(SUPPORT_MATRIX[class_name]) - schema_fields}')
    assert all(row.support in Support for row in SUPPORT_MATRIX[class_name].values())


@pytest.mark.fast
def test_every_row_is_pinned_by_a_test_that_exists() -> None:
    """A row's `pinned_by` names a test function that exists under `tests/core`."""
    existing = {node.name for path in TESTS.glob('test_*.py')
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
                if isinstance(node, ast.FunctionDef) and node.name.startswith('test_')}
    missing = [(cls, field, row.pinned_by) for cls, table in SUPPORT_MATRIX.items()
               for field, row in table.items() if row.pinned_by not in existing]
    assert not missing, missing
