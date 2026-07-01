import pytest
import json
import pathlib

from sophios.api.utils.converter import update_payload_missing_inputs_outputs


@pytest.mark.fast
def test_fix_multi_node_payload() -> None:

    path = pathlib.Path(__file__).parent.resolve()

    with open(
        path / "data/wfb_data/multi_node/multi_node_wfb.json", "r"
    ) as file:
        wfb = json.load(file)

    updated_payload = update_payload_missing_inputs_outputs(wfb)

    with open(
        path / "data/wfb_data/multi_node/multi_node_wfb_truth.json", "r"
    ) as file:
        truth = json.load(file)

    assert updated_payload == truth
