"""The support matrix is complete against the CWL schema."""
import pytest
from cwl_utils.parser import cwl_v1_2

from sophios.lang import SUPPORT_MATRIX, Support


@pytest.mark.fast
def test_every_schema_field_is_classified() -> None:
    """A field the CWL schema has and the matrix lacks, or one the schema dropped, fails here; every row carries one of
    the three classifications."""
    for class_name, table in SUPPORT_MATRIX.items():
        schema_fields = set(getattr(cwl_v1_2, class_name).attrs)
        assert set(table) == schema_fields, (
            f'{class_name}: unclassified {schema_fields - set(table)}, stale {set(table) - schema_fields}')
        assert all(support in Support for support in table.values())
