"""The source inliner discharges a workflow call boundary or leaves it intact."""
import copy

import pytest

from sophios.inlineing import get_inlineable_subworkflows, inline_subworkflow
from sophios.wic_types import StepId, Yaml, YamlTree

from .hermetic import compile_hermetic, subworkflow_step
from .synthetic_tools import SYNTHETIC_NS


def _tree(child: Yaml, call: Yaml, *, inputs: Yaml | None = None,
          wic: Yaml | None = None, tail: list[Yaml] | None = None) -> YamlTree:
    root: Yaml = {'steps': [subworkflow_step('child.wic', child), *(tail or [])]}
    root['steps'][0]['parentargs'] = call
    if inputs is not None:
        root['inputs'] = inputs
    if wic is not None:
        root['wic'] = wic
    return YamlTree(StepId('root', SYNTHETIC_NS), root)


def _inline_first(tree: YamlTree) -> YamlTree:
    offered = get_inlineable_subworkflows(
        tree, implementation=False, namespaces_init=[])
    assert offered
    inlined, _length = inline_subworkflow(tree, offered[0])
    return inlined


@pytest.mark.fast
def test_bare_formal_is_replaced_by_its_differently_named_actual() -> None:
    """Deleting a child declaration cannot leave its bare formal behind."""
    child: Yaml = {
        'inputs': {'formal': {'type': 'string'}},
        'steps': [{'id': 'mk_file', 'in': {'name': 'formal'}}],
    }
    tree = _tree(child, {'in': {'formal': 'actual'}},
                 inputs={'actual': {'type': 'string'}})
    original = copy.deepcopy(tree)

    compile_hermetic(tree.yml)
    inlined = _inline_first(tree)

    assert tree == original
    assert 'inputs' not in inlined.yml['steps'][0]
    assert inlined.yml['steps'][0]['in']['name'] == 'actual'
    compile_hermetic(inlined.yml)


@pytest.mark.fast
def test_formal_substitution_is_simultaneous() -> None:
    """An actual named like another formal must not be substituted twice."""
    child: Yaml = {
        'inputs': {'first': {'type': 'string'}, 'second': {'type': 'string'}},
        'steps': [{'id': 'mk_file', 'in': {'name': 'first'}}],
    }
    tree = _tree(
        child,
        {'in': {'first': 'second', 'second': {'wic_inline_input': 'literal.txt'}}},
        inputs={'second': {'type': 'string'}},
    )

    inlined = _inline_first(tree)

    assert inlined.yml['steps'][0]['in']['name'] == 'second'
    compile_hermetic(inlined.yml)


@pytest.mark.fast
def test_call_metadata_wins_before_formals_are_substituted() -> None:
    """The inliner must see the same effective call as the compiler."""
    child: Yaml = {
        'inputs': {'formal': {'type': 'string'}},
        'steps': [{'id': 'mk_file', 'in': {'name': 'formal'}}],
    }
    tree = _tree(
        child,
        {'in': {'formal': 'call_actual'}},
        inputs={'call_actual': {'type': 'string'}, 'metadata_actual': {'type': 'string'}},
        wic={'steps': {'(1, child.wic)': {'in': {'formal': 'metadata_actual'}}}},
    )

    inlined = _inline_first(tree)

    assert inlined.yml['steps'][0]['in']['name'] == 'metadata_actual'
    compile_hermetic(inlined.yml)


@pytest.mark.fast
def test_child_step_metadata_cannot_reintroduce_a_deleted_formal() -> None:
    """Child-side overrides are rewritten along with the authored step."""
    child: Yaml = {
        'inputs': {'formal': {'type': 'string'}},
        'steps': [{'id': 'mk_file', 'in': {'name': 'formal'}}],
        'wic': {'steps': {'(1, mk_file)': {'in': {'name': 'formal'}}}},
    }
    tree = _tree(child, {'in': {'formal': 'actual'}},
                 inputs={'actual': {'type': 'string'}})

    inlined = _inline_first(tree)

    assert inlined.yml['steps'][0]['in']['name'] == 'actual'
    assert inlined.yml['wic']['steps']['(1, mk_file)']['in']['name'] == 'actual'
    compile_hermetic(inlined.yml)


@pytest.mark.fast
def test_nested_child_keeps_its_formal_scope() -> None:
    """Only the nested call is rebound; its own body is a new scope."""
    nested: Yaml = {
        'inputs': {'formal': {'type': 'string'}},
        'steps': [{'id': 'mk_file', 'in': {'name': 'formal'}}],
    }
    child: Yaml = {
        'inputs': {'formal': {'type': 'string'}},
        'steps': [subworkflow_step('nested.wic', nested)],
    }
    tree = _tree(child, {'in': {'formal': 'actual'}},
                 inputs={'actual': {'type': 'string'}})

    inlined = _inline_first(tree)
    nested_call = inlined.yml['steps'][0]

    assert nested_call['parentargs']['in']['formal'] == 'actual'
    assert nested_call['subtree']['inputs'] == {'formal': {'type': 'string'}}
    assert nested_call['subtree']['steps'][0]['in']['name'] == 'formal'
    compile_hermetic(inlined.yml)


@pytest.mark.fast
def test_wrapper_output_anchor_moves_to_its_declared_producer() -> None:
    """Inlining preserves the edge name exported through a workflow output."""
    child: Yaml = {
        'inputs': {'formal': {'type': 'string'}},
        'outputs': {
            'exported': {
                'type': 'File',
                'outputSource': 'child__step__1__mk_file/file',
            },
        },
        'steps': [{'id': 'mk_file', 'in': {'name': 'formal'}}],
    }
    call: Yaml = {
        'in': {'formal': {'wic_inline_input': 'made.txt'}},
        'out': [{'exported': {'wic_anchor': 'published'}}],
    }
    tail = [{'id': 'xform', 'in': {
        'file': {'wic_alias': 'published'},
        'name': {'wic_inline_input': 'copied.txt'},
    }}]
    tree = _tree(child, call, tail=tail)

    compile_hermetic(tree.yml)
    inlined = _inline_first(tree)
    producer = inlined.yml['steps'][0]

    assert producer['out'] == [{'file': {'wic_anchor': 'published'}}]
    compiled = compile_hermetic(inlined.yml).rose.data.compiled_cwl
    assert not any(name.endswith('xform___file') for name in compiled['inputs'])


@pytest.mark.fast
def test_output_anchor_moves_one_boundary_at_a_time() -> None:
    """A nested producer keeps the anchor until its own boundary is removed."""
    nested: Yaml = {
        'inputs': {'formal': {'type': 'string'}},
        'outputs': {'exported': {
            'type': 'File',
            'outputSource': 'nested__step__1__mk_file/file',
        }},
        'steps': [{'id': 'mk_file', 'in': {'name': 'formal'}}],
    }
    nested_step = subworkflow_step('nested.wic', nested)
    nested_step['parentargs'] = {'in': {'formal': 'formal'}}
    child: Yaml = {
        'inputs': {'formal': {'type': 'string'}},
        'outputs': {'exported': {
            'type': 'File',
            'outputSource': 'child__step__1__nested.wic/exported',
        }},
        'steps': [nested_step],
    }
    call: Yaml = {
        'in': {'formal': {'wic_inline_input': 'made.txt'}},
        'out': [{'exported': {'wic_anchor': 'published'}}],
    }

    once = _inline_first(_tree(child, call))
    nested_call = once.yml['steps'][0]
    assert nested_call['parentargs']['out'] == [
        {'exported': {'wic_anchor': 'published'}},
    ]

    twice = _inline_first(once)
    assert twice.yml['steps'][0]['out'] == [
        {'file': {'wic_anchor': 'published'}},
    ]
    compile_hermetic(twice.yml)


@pytest.mark.fast
@pytest.mark.parametrize(('claim', 'child_inputs', 'call'), [
    ('missing formal', {'first': {'type': 'string'}, 'second': {'type': 'string'}},
     {'in': {'first': 'actual'}}),
    ('extra formal', {'formal': {'type': 'string'}},
     {'in': {'formal': 'actual', 'extra': 'actual'}}),
    ('conditional call', {'formal': {'type': 'string'}},
     {'in': {'formal': 'actual'}, 'when': '$(true)'}),
    ('scattered call', {'formal': {'type': 'string'}},
     {'in': {'formal': 'actual'}, 'scatter': ['formal']}),
    ('scatter method', {'formal': {'type': 'string'}},
     {'in': {'formal': 'actual'}, 'scatterMethod': 'dotproduct'}),
    ('unknown call semantics', {'formal': {'type': 'string'}},
     {'in': {'formal': 'actual'}, 'custom': True}),
])
def test_unsafe_call_boundary_is_not_offered(
        claim: str, child_inputs: Yaml, call: Yaml) -> None:
    """A splice is optional; silently erasing call semantics is not."""
    child: Yaml = {
        'inputs': child_inputs,
        'steps': [{'id': 'mk_file', 'in': {'name': next(iter(child_inputs))}}],
    }
    tree = _tree(child, call, inputs={'actual': {'type': 'string'}})

    offered = get_inlineable_subworkflows(
        tree, implementation=False, namespaces_init=[])

    assert offered == [], claim


@pytest.mark.fast
@pytest.mark.parametrize('output', [
    {'missing': {'wic_anchor': 'edge'}},
    {'exported': {'wic_anchor': 'edge'}},
])
def test_unresolvable_or_conflicting_output_anchor_is_not_offered(output: Yaml) -> None:
    """An output boundary is removed only when its anchor has one safe home."""
    producer: Yaml = {
        'id': 'mk_file',
        'in': {'name': 'formal'},
        'out': [{'file': {'wic_anchor': 'different'}}],
    }
    child: Yaml = {
        'inputs': {'formal': {'type': 'string'}},
        'outputs': {'exported': {
            'type': 'File',
            'outputSource': 'child__step__1__mk_file/file',
        }},
        'steps': [producer],
    }
    tree = _tree(child, {'in': {'formal': 'actual'}, 'out': [output]},
                 inputs={'actual': {'type': 'string'}})

    offered = get_inlineable_subworkflows(
        tree, implementation=False, namespaces_init=[])

    assert offered == []


@pytest.mark.fast
def test_direct_request_to_erase_unsafe_boundary_is_rejected() -> None:
    """The mutator repeats discovery's proof instead of trusting its caller."""
    child: Yaml = {
        'inputs': {'formal': {'type': 'string'}},
        'steps': [{'id': 'mk_file', 'in': {'name': 'formal'}}],
    }
    tree = _tree(child, {'in': {'formal': 'actual'}, 'when': '$(true)'},
                 inputs={'actual': {'type': 'string'}})
    namespace = ['root__step__1__child.wic']

    with pytest.raises(ValueError, match='cannot preserve'):
        inline_subworkflow(tree, namespace)
