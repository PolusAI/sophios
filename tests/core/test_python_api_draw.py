"""`Workflow.draw` returns the drawing `--graphviz` makes, on a background of its own, from the same compile."""
import re
from typing import Any

import graphviz
import pytest

from sophios.api.python.workflow import Inference, Workflow

from .helpers import adapter_step

pytestmark = pytest.mark.package


def _workflow() -> Workflow:
    """`touch` feeds `append` by an edge written here, and `append` feeds `cat` by an edge inference adds; a workflow
    output named `touch` makes the compiled step `touch@1`."""
    touch = adapter_step("touch", filename="empty.txt")
    append = adapter_step("append", str="Hello", file=touch.outputs.file)
    flow = Workflow([touch, append, adapter_step("cat")], "wf")
    flow.outputs.touch = touch.outputs.file
    return flow


@pytest.mark.fast
@pytest.mark.parametrize('options, page, text, boxes, edges', [
    ({}, 'white', 'black', ['touch', 'append', 'cat'],
     {('touch@1', 'append'): ('', 'blue'), ('append', 'cat'): ('', 'black')}),
    ({'dark': True}, '"#1e1e1e"', 'white', ['touch', 'append', 'cat'],
     {('touch@1', 'append'): ('', 'lightskyblue'), ('append', 'cat'): ('', 'white')}),
    ({'inference': Inference(enabled=False)}, 'white', 'black', ['touch', 'append', 'cat'],
     {('touch@1', 'append'): ('', 'blue')}),
    ({'label_edges': True, 'label_stepname': True}, 'white', 'black', ['touch@1', 'append', 'cat'],
     {('touch@1', 'append'): ('file', 'blue'), ('append', 'cat'): ('file', 'black')}),
], ids=['default', 'dark', 'no-inference', 'labelled'])
def test_draw_paints_an_opaque_page_and_draws_what_the_flags_do(
        options: dict[str, Any], page: str, text: str, boxes: list[str], edges: dict[Any, tuple[str, str]]) -> None:
    """An opaque white page in black, or with `dark=True` a near-black one in white; a written edge blue, lighter on
    the dark page, and one inference added the text's colour, none with `Inference(enabled=False)`. `label_edges`
    names each edge's port, and `label_stepname` names each box by its name in the compiled CWL, which every edge
    names it by."""
    drawing = _workflow().draw(**options)
    assert isinstance(drawing, graphviz.Digraph) and 'transparent' not in drawing.source
    assert f'bgcolor={page}' in drawing.source and f'fontcolor={text}' in drawing.source
    drawn = re.findall(r'\[label="?([\w@]+)"? color=(\w+) fontcolor=(\w+) shape=box', drawing.source)
    assert drawn == [(box, text, text) for box in boxes]
    assert {(source, sink): (label, colour) for source, sink, label, colour in re.findall(
        r'^\s*"?([\w@]+)"? -> "?([\w@]+)"? \[(?:label=(\w+) )?color=(\w+)', drawing.source, re.MULTILINE)} == edges
