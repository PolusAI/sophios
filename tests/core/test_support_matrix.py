"""The support matrix is complete against the CWL schema."""
import pytest
from cwl_utils.parser import cwl_v1_2

from sophios.lang import SUPPORT_MATRIX, Support


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
