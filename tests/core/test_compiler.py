"""Regression coverage for `sophios.compiler` bugs that have no home elsewhere."""
import pytest

from sophios import utils
from sophios.compiler import insert_step_into_workflow
from sophios.wic_types import StepId, Tool, Yaml

from .hermetic import compile_hermetic_cwl


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


@pytest.mark.fast
def test_a_referenced_input_keeps_the_documentation_the_user_wrote() -> None:
    """References neither append empty text nor synthesize absent fields."""
    documented: Yaml = {
        'inputs': {'wf_name': {'type': 'string', 'doc': 'mine', 'label': 'keep me'}},
        'steps': [{'id': 'mk_text', 'in': {'name': 'wf_name'}},
                  {'id': 'mk_file', 'in': {'name': 'wf_name'}}],
    }
    kept = compile_hermetic_cwl(documented, 'docs')['inputs']['wf_name']
    assert kept == {'type': 'string', 'doc': 'mine', 'label': 'keep me'}

    bare: Yaml = {
        'inputs': {'wf_name': {'type': 'string'}},
        'steps': [{'id': 'mk_text', 'in': {'name': 'wf_name'}}],
    }
    assert compile_hermetic_cwl(bare, 'docs')['inputs']['wf_name'] == {'type': 'string'}


@pytest.mark.fast
def test_a_documented_argument_still_documents_the_input_it_binds() -> None:
    """Skipping empty additions does not disable useful documentation propagation."""
    from copy import deepcopy  # pylint: disable=import-outside-toplevel

    from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS  # pylint: disable=import-outside-toplevel

    tools = deepcopy(SYNTHETIC_TOOLS)
    tools[StepId('mk_text', SYNTHETIC_NS)].cwl['inputs']['name']['doc'] = 'the file name'
    document: Yaml = {
        'inputs': {'wf_name': {'type': 'string'}},
        'steps': [{'id': 'mk_text', 'in': {'name': 'wf_name'}}],
    }
    compiled = compile_hermetic_cwl(document, 'docs', tools=tools)
    assert compiled['inputs']['wf_name']['doc'] == 'the file name'
