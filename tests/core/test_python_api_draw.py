"""`Workflow.draw` returns the drawing `--graphviz` makes, on a background of its own, from the same compile."""
import re
from typing import Any

import graphviz
import pytest

from sophios.api.python.workflow import Inference, Workflow

from .helpers import adapter_step

pytestmark = pytest.mark.package


def _workflow() -> Workflow:
    """`touch` feeds `append` by an edge written here, and `append` feeds `cat` by an edge inference adds."""
    touch = adapter_step("touch", filename="empty.txt")
    append = adapter_step("append", str="Hello", file=touch.outputs.file)
    cat = adapter_step("cat")
    return Workflow([touch, append, cat], "wf")


def _box_labels(drawing: graphviz.Digraph) -> list[str]:
    """The label of each box, in the order the drawing declares the boxes."""
    return re.findall(r'\[label="?([\w@~]+)"? ', drawing.source)


def _edge_colours(drawing: graphviz.Digraph) -> dict[tuple[str, str], str]:
    """The colour of each edge, by the labels of the steps it joins."""
    edges = re.findall(r'^\s*"?(\w+)"? -> "?(\w+)"? \[color=(\w+)\]$',
                       drawing.source, re.MULTILINE)
    return {(source, sink): colour for source, sink, colour in edges}


@pytest.mark.fast
@pytest.mark.parametrize('options, page, text, edges', [
    ({}, 'white', 'black', {('touch', 'append'): 'blue', ('append', 'cat'): 'black'}),
    ({'dark': True}, '"#1e1e1e"', 'white', {('touch', 'append'): 'lightskyblue', ('append', 'cat'): 'white'}),
    ({'inference': Inference(enabled=False)}, 'white', 'black', {('touch', 'append'): 'blue'}),
], ids=['default', 'dark', 'no-inference'])
def test_draw_paints_an_opaque_page_and_colours_the_edges_by_who_made_them(
        options: dict[str, Any], page: str, text: str, edges: dict[tuple[str, str], str]) -> None:
    """An opaque white page in black, or with `dark=True` a near-black one in white; a written edge blue, lighter on
    the dark page, and one inference added the text's colour, none with `Inference(enabled=False)`."""
    drawing = _workflow().draw(**options)
    assert isinstance(drawing, graphviz.Digraph) and 'transparent' not in drawing.source
    assert f'bgcolor={page}' in drawing.source and f'fontcolor={text}' in drawing.source
    assert re.findall(r'\[label=\w+ color=(\w+) fontcolor=(\w+) shape=box', drawing.source) == [(text, text)] * 3
    assert _edge_colours(drawing) == edges


@pytest.mark.fast
def test_draw_labels_edges_and_boxes_as_the_flags_do() -> None:
    """`label_edges` names each edge's port, and `label_stepname` names each box by its name in the
    compiled CWL, which differs from the step's id when a workflow output has that id: `touch` is
    compiled as `touch@1`."""
    assert 'label=file' in _workflow().draw(label_edges=True).source
    flow = _workflow()
    flow.outputs.touch = flow.steps[0].outputs.file
    assert _box_labels(flow.draw()) == ['touch', 'append', 'cat']
    assert _box_labels(flow.draw(label_stepname=True)) == ['touch@1', 'append', 'cat']
