import copy
import json
from pathlib import Path

import pytest

from sophios.contrib.converter import update_payload_missing_inputs_outputs

# Each input an edge feeds, filled with the output that feeds it
_FILLED = {1: {'inpDir': 'filerenaming_3-outDir'}, 3: {'inpDir': 'omeconverter_2-outDir'},
           4: {'stitchPath': 'montage_4-outDir', 'imgPath': 'omeconverter_2-outDir'},
           5: {'inpDir': 'imageassembler_5-outDir'}}


@pytest.mark.fast
def test_fix_multi_node_payload() -> None:
    """Missing inputs and outputs are filled in to match the recorded payload."""
    wfb = json.loads((Path(__file__).parent / "rest_wfb_objects/multi_node_wfb.json").read_text(encoding="utf-8"))
    expected = copy.deepcopy(wfb)
    for node, inputs in _FILLED.items():
        expected['state']['nodes'][node]['settings']['inputs'].update(inputs)
    assert update_payload_missing_inputs_outputs(wfb) == expected
