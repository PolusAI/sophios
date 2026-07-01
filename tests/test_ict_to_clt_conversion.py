import pytest
import json
import pathlib

from sophios.api.utils.converter import ict_to_clt


@pytest.mark.fast
def test_ict_to_clt_label_to_vector_conversion() -> None:

    path = pathlib.Path(__file__).parent.resolve()

    with open(
        path / "data/ict_data/label_to_vector/label_to_vector_ict.json", "r"
    ) as file:
        label_to_vector_ict = json.load(file)

    with open(
        path / "data/ict_data/label_to_vector/label_to_vector_clt.json", "r"
    ) as file:
        label_to_vector_clt = json.load(file)

    result = ict_to_clt(label_to_vector_ict)

    assert result == label_to_vector_clt


@pytest.mark.fast
def test_ict_to_clt_ome_conversion() -> None:

    path = pathlib.Path(__file__).parent.resolve()

    with open(
        path / "data/ict_data/ome_conversion/ome_conversion_ict.json", "r"
    ) as file:
        ome_conversion_ict = json.load(file)

    with open(
        path / "data/ict_data/ome_conversion/ome_conversion_clt.json", "r"
    ) as file:
        ome_conversion_clt = json.load(file)

    result = ict_to_clt(ome_conversion_ict)

    assert result == ome_conversion_clt


@pytest.mark.fast
def test_ict_to_clt_czi_extract_conversion() -> None:

    path = pathlib.Path(__file__).parent.resolve()

    with open(path / "data/ict_data/czi_extract/czi_extract_ict.json", "r") as file:
        czi_extract_ict = json.load(file)

    with open(path / "data/ict_data/czi_extract/czi_extract_clt.json", "r") as file:
        czi_extract_clt = json.load(file)

    result = ict_to_clt(czi_extract_ict)

    assert result == czi_extract_clt
