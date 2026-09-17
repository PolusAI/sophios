import json
import pathlib

import pytest

from sophios.contrib.converter import update_payload_missing_inputs_outputs


@pytest.mark.fast
def test_fix_multi_node_payload() -> None:
    """Missing inputs and outputs are filled in to match the recorded payload."""
    path = pathlib.Path(__file__).parent.resolve()

    wfb = json.loads((path / "data/wfb_data/multi_node/multi_node_wfb.json").read_text(encoding="utf-8"))

    updated_payload = update_payload_missing_inputs_outputs(wfb)

    truth = json.loads((path / "data/wfb_data/multi_node/multi_node_wfb_truth.json").read_text(encoding="utf-8"))

    assert updated_payload == truth
