"""The safe JavaScript subset, admitted only in a step's when (design §6, Expressions). What a when decides is the
subset differential's; here, which texts the subset admits and how it reads them."""

# pylint: disable=missing-function-docstring

from hypothesis import example, given, strategies as st
import pytest

from sophios.lang.subset import canonical, check, parse, source_text
from sophios.nf_expr import render_groovy

from ..budgets import budget
from .test_subset_differential import _BOOLEAN, _NUMBER, TYPES

#: Each input an expression here may read, by its CWL type; `w` and `d` are not scalar inputs.
_TYPES = {**TYPES, "a": "int", "w": "long", "d": "double"}


@pytest.mark.fast
@budget(200)
@given(st.one_of(_NUMBER.map(lambda text: (text, "number")), _BOOLEAN.map(lambda text: (text, "boolean"))),
       st.text(st.characters(codec="utf-8"), max_size=8))
@example(("1 + 2 * 3 - -4 < 5 || true && false", "boolean"), "CALC\narguments[0]\t")
@example(("Math.round(Math.pow(inputs.x, 2)) + Math.sqrt(4)", "number"), "")
@example(("inputs.maybe === null && null != inputs.maybe", "boolean"), "")
def test_an_admitted_expression_reads_back_as_written(admitted: tuple[str, str], where: str) -> None:
    """An expression of the subset's grammar types as its grammar says; its canonical text parses to the same tree,
    precedence and all; and its Groovy names where it stands without a raw control character."""
    text, kind = admitted
    node = parse(f"$({text})")
    assert check(node, _TYPES) == kind
    assert canonical(parse(f"$({source_text(node)})")) == canonical(node)
    rendered = render_groovy(node, where=where, inputs="[:]")
    assert not any(ord(character) < 0x20 for character in rendered)


#: Text the subset does not read, each where an operand stands: an operator, a property, a name, a literal, a
#: character, or an operand of the wrong type.
_OUTSIDE = ["inputs.a ** 2", "--inputs.a", "1 ++ 2", "inputs.a ? 1 : 2", "inputs.a & 1", "Math.PI", "inputs.s.length",
            "self", "runtime.cores", "inputs.s + 1", "inputs.a == 'x'", "inputs.maybe + 1", "inputs.a === null",
            "!inputs.a", "Math.pow(1)", "Math.min(1)", "1e999", "010 + 1", "08", "٣", "inputs.nope",
            "inputs.w", "inputs.d", '"a\\"b"']


@pytest.mark.fast
@budget(200)
@given(_NUMBER, st.sampled_from(_OUTSIDE))
@example("inputs.i", "inputs.a === null")
def test_an_expression_with_text_outside_the_subset_is_refused(admitted: str, outside: str) -> None:
    """Text outside the subset is refused, alone and beside an admitted number, by parsing or by type, never read as
    something else; beside a number it is admitted, so the text is what is refused."""
    with pytest.raises(ValueError):
        check(parse(f"$({outside})"), _TYPES)
    frame = "$(({}) + ({}) > 0)"
    assert check(parse(frame.format(admitted, "1")), _TYPES) == "boolean"
    with pytest.raises(ValueError):
        check(parse(frame.format(admitted, outside)), _TYPES)
