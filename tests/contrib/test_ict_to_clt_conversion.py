import json
import pathlib

import pytest

from sophios.contrib.converter import ict_to_clt


@pytest.mark.fast
def test_ict_to_clt_label_to_vector_conversion() -> None:
    """`ict_to_clt` reproduces the recorded CLT for the label-to-vector spec."""
    path = pathlib.Path(__file__).parent.resolve()

    with open(
        path / "data/ict_data/label_to_vector/label_to_vector_ict.json", "r", encoding="utf-8"
    ) as file:
        label_to_vector_ict = json.load(file)

    with open(
        path / "data/ict_data/label_to_vector/label_to_vector_clt.json", "r", encoding="utf-8"
    ) as file:
        label_to_vector_clt = json.load(file)

    result = ict_to_clt(label_to_vector_ict)

    assert result == label_to_vector_clt


@pytest.mark.fast
def test_ict_to_clt_ome_conversion() -> None:
    """`ict_to_clt` reproduces the recorded CLT for the OME conversion spec."""
    path = pathlib.Path(__file__).parent.resolve()

    with open(
        path / "data/ict_data/ome_conversion/ome_conversion_ict.json", "r", encoding="utf-8"
    ) as file:
        ome_conversion_ict = json.load(file)

    with open(
        path / "data/ict_data/ome_conversion/ome_conversion_clt.json", "r", encoding="utf-8"
    ) as file:
        ome_conversion_clt = json.load(file)

    result = ict_to_clt(ome_conversion_ict)

    assert result == ome_conversion_clt


@pytest.mark.fast
def test_ict_to_clt_czi_extract_conversion() -> None:
    """`ict_to_clt` reproduces the recorded CLT for the CZI extract spec."""
    path = pathlib.Path(__file__).parent.resolve()

    with open(path / "data/ict_data/czi_extract/czi_extract_ict.json", "r", encoding="utf-8") as file:
        czi_extract_ict = json.load(file)

    with open(path / "data/ict_data/czi_extract/czi_extract_clt.json", "r", encoding="utf-8") as file:
        czi_extract_clt = json.load(file)

    result = ict_to_clt(czi_extract_ict)

    assert result == czi_extract_clt
