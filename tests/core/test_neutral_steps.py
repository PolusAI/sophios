"""A step's scatter, merge, pick and `when` are Sophios's own meanings; what no node holds rides unread."""
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sophios.lang.nodes import CwlRecord
from sophios.lang.subset import Boolean, Expr, Number, Op, Ref, Text, parse, written_text

from .hermetic import compile_hermetic, cwl_tree


def _steps(**fields: object) -> dict[str, Any]:
    """Two calls of `mk_file`, the second written with `fields`."""
    return {'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a.txt'}}},
                      {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'b.txt'}}, **fields}]}


def _second(document: dict[str, Any]) -> dict[str, Any]:
    """The second step of the CWL `document` compiles to."""
    step: dict[str, Any] = cwl_tree(compile_hermetic(document)).cwl['steps'][1]
    return step


@pytest.mark.fast
def test_a_when_the_subset_reads_is_written_from_its_tree_and_one_it_does_not_as_it_was() -> None:
    """A `when` in the safe subset is a tree and the inputs it reads, and the CWL target writes the tree; any other
    text is unread, kept as it was and written back as it was, and it still guards its step."""
    result = compile_hermetic(_steps(when='$(inputs.name  ==  "a")'))
    guard = result.graph.steps[1].guard
    assert guard is not None and guard.expr == Op('==', (Ref('name'), Text('a'))) and guard.reads == ('name',)
    assert not result.graph.steps[1].passthrough
    assert cwl_tree(result).cwl['steps'][1]['when'] == '$(inputs.name == "a")'
    for text in ('true', "${ return inputs.name == 'a'; }", '$(inputs.name ** 2)'):
        result = compile_hermetic(_steps(when=text))
        guard = result.graph.steps[1].guard
        assert guard is not None and guard.expr is None and guard.reads in ((), ('name',))
        assert dict(result.graph.steps[1].passthrough)['when'] == text
        assert cwl_tree(result).cwl['steps'][1]['when'] == text


@pytest.mark.fast
def test_a_scatter_method_the_standard_lacks_is_unread_and_written_back() -> None:
    """No node holds an unknown method: the scatter has none and the step carries it unread, which CWL gets back where
    it was."""
    result = compile_hermetic(_steps(scatterMethod='bogus'))
    assert result.graph.steps[1].scatter is None
    assert dict(result.graph.steps[1].passthrough) == {'scatterMethod': 'bogus'}
    assert _second(_steps(scatterMethod='bogus'))['scatterMethod'] == 'bogus'


@pytest.mark.fast
@pytest.mark.parametrize('field, known', [('linkMerge', 'merge_flattened'), ('pickValue', 'first_non_null')])
def test_a_merge_or_pick_the_standard_lacks_is_unread_and_one_it_has_is_a_meaning(field: str, known: str) -> None:
    """A step input that merges or picks as no way of the standard does holds neither: its field stays in `passthrough`
    for the target that refuses it by name, where a way of the standard is a meaning and no field."""
    unread = CwlRecord(fields=(('default', 1), (field, 'bogus'), ('label', 'x')))
    assert (unread.merge, unread.pick) == (None, None)
    assert unread.passthrough == ((field, 'bogus'), ('label', 'x'))
    meant = CwlRecord(fields=((field, known), ('label', 'x')))
    assert (meant.merge or meant.pick) is not None and meant.passthrough == (('label', 'x'),)


_NODES = st.recursive(
    st.one_of(st.builds(Ref, st.sampled_from(['a', 'b'])), st.builds(Number, st.sampled_from(['1', '2.5', '0'])),
              st.builds(Text, st.sampled_from(['x', "it's", 'say "hi"'])), st.builds(Boolean, st.booleans())),
    lambda inner: st.one_of(
        st.builds(lambda op, left, right: Op(op, (left, right)),
                  st.sampled_from(['||', '&&', '==', '!=', '<', '>=', '+', '-', '*', '/', '%']), inner, inner),
        st.builds(lambda op, operand: Op(op, (operand,)), st.sampled_from(['u!', 'u-']), inner),
        st.builds(lambda args: Op('Math.max', args), st.tuples(inner, inner))),
    max_leaves=8)


@pytest.mark.fast
@given(_NODES)
def test_a_tree_is_written_as_a_field_that_parses_back_to_it(node: Expr) -> None:
    """The text a tree is written as reads as the same tree: a parenthesis only where precedence needs one."""
    assert parse(written_text(node)) == node
