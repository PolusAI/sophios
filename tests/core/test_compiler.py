"""Regression coverage for `sophios.compiler` bugs that have no home elsewhere."""
import pytest

from sophios import utils
from sophios.compiler import insert_step_into_workflow
from sophios.wic_types import StepId, Tool, Yaml


@pytest.mark.fast
def test_an_inserted_step_is_resolvable_by_id() -> None:
    """`insert_step_into_workflow` must write a step `get_steps_keys` can read back.

    It used to insert `{stepid.stem: None}` - reference §3.1's third step
    surface form, a single-key mapping whose key is the step name - but
    `get_steps_keys` reads only `step_dict.get('id', '')`, so the inserted
    step's name came back as `''` and nothing downstream could resolve it.
    This is what made `--insert_steps_automatically` unusable: the step it
    just inserted was immediately unreadable on the next compile pass.
    """
    stepid = StepId('conv', 'global')
    tools = {stepid: Tool('/synthetic/conv.cwl', {'outputs': {}})}
    yaml_tree: Yaml = {'steps': [{'id': 'existing'}]}

    yaml_tree_mod = insert_step_into_workflow(yaml_tree, stepid, tools, 1)

    assert utils.get_steps_keys(yaml_tree_mod['steps']) == ['existing', 'conv']
