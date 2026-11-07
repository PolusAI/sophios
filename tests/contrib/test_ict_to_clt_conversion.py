import json
import pathlib

import pytest

from sophios.contrib.converter import ict_to_clt


@pytest.mark.fast
@pytest.mark.parametrize('spec', ['label_to_vector', 'ome_conversion', 'czi_extract'])
def test_ict_to_clt_reproduces_the_recorded_clt(spec: str) -> None:
    """`ict_to_clt` reproduces the recorded CLT for each spec: label-to-vector, OME conversion and CZI extract."""
    recorded = pathlib.Path(__file__).parent.resolve() / 'data' / 'ict_data' / spec
    ict, clt = (json.loads((recorded / f'{spec}_{kind}.json').read_text(encoding='utf-8')) for kind in ('ict', 'clt'))
    assert ict_to_clt(ict) == clt
