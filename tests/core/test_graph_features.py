"""The graph Sophios draws: what each graph setting and the CLI's `--graphviz` do to its DOT source; no image is
rendered."""
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from sophios import compiler, drawing, main
from sophios.cli import default_compilation_settings, get_args, get_dicts_for_compilation
from sophios.wic_types import Yaml

from .helpers import no_preflight, run_cli, write_tree
from .hermetic import bundle, subworkflow_step
from .synthetic_tools import SYNTHETIC_TOOLS

pytestmark = pytest.mark.package

#: `mk_file` feeds `xform` of a subworkflow, which feeds `count`, which feeds a
#: subworkflow of its own: two levels of nesting under the root.
_DOCUMENT: Yaml = {'steps': [
    {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}, 'out': [{'file': {'wic_anchor': 'f'}}]},
    subworkflow_step('sub.wic', {'steps': [
        {'id': 'xform', 'in': {'file': {'wic_alias': 'f'}, 'name': {'wic_inline_input': 'b'}}},
        {'id': 'count'},
        subworkflow_step('inner.wic', {'steps': [
            {'id': 'mk_text', 'in': {'name': {'wic_inline_input': 'c'}}}]}),
    ]}),
]}
#: The ids `_DOCUMENT`'s steps are written with.
_AUTHORED_IDS = {'mk_file', 'sub.wic', 'xform', 'count', 'inner.wic', 'mk_text'}


def _draw(document: Yaml = _DOCUMENT, *, label_edges: bool = False, label_stepname: bool = False,
          inline_depth: int = sys.maxsize, dark_theme: bool = False) -> str:
    """`document` compiled, then drawn with the given graph settings: the DOT source."""
    source = bundle(document, 'oracle', SYNTHETIC_TOOLS)
    compiled = compiler.compile_source(source, default_compilation_settings(), quiet=True)
    _options, settings = get_dicts_for_compilation(get_args())
    settings['graph_label_edges'] = label_edges
    settings['graph_label_stepname'] = label_stepname
    settings['graph_inline_depth'] = inline_depth
    settings['graph_dark_theme'] = dark_theme
    return cast(str, drawing.draw(compiled, source, settings, 'oracle', background=None).source)


#: A node or edge name in DOT: quoted, or a bare word.
_ID = r'"[^"]*"|\w+'
#: A node or edge statement: its node or its two ends, and its attributes.
_STATEMENT = re.compile(rf'^\s*({_ID})(?: -> ({_ID}))? \[(.*)\]$', re.MULTILINE)
_ATTRIBUTE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|[^\s"]+)')


def _unquoted(text: str) -> str:
    return text[1:-1] if text.startswith('"') else text


def _statements(dot: str) -> list[tuple[str, str, dict[str, str]]]:
    """Each node (with an empty sink) and each edge `dot` draws, with its attributes."""
    return [(_unquoted(name), _unquoted(sink), {key: _unquoted(value) for key, value in _ATTRIBUTE.findall(attrs)})
            for name, sink, attrs in _STATEMENT.findall(dot)]


def _nodes(dot: str) -> dict[str, dict[str, str]]:
    return {name: attrs for name, sink, attrs in _statements(dot) if not sink}


def _edges(dot: str) -> list[tuple[str, str, dict[str, str]]]:
    return [statement for statement in _statements(dot) if statement[1]]


def _cluster(dot: str, name: str) -> dict[str, str]:
    """The attributes of cluster `name`, written before its first node."""
    block = re.search(rf'subgraph "?{re.escape(name)}"? {{\n((?:\t+\w+=.*\n)*)', dot)
    assert block is not None, f'{name} is not drawn'
    return {key: _unquoted(value) for key, value in _ATTRIBUTE.findall(block.group(1))}


def _ranks(dot: str) -> list[tuple[int, list[str]]]:
    """Each rank=same group, with its indent (2 in the root's cluster, 3 in a cluster within it) and its nodes."""
    return [(len(indent), [_unquoted(name) for name in names.split('; ')])
            for indent, names in re.findall(r'^(\t*)\{rank=same; (.*)\}$', dot, re.MULTILINE)]


@pytest.mark.fast
@pytest.mark.parametrize(('flag', 'edge', 'nodes'), [
    ({}, None, _AUTHORED_IDS),
    ({'label_edges': True}, 'file', _AUTHORED_IDS),
    ({'label_stepname': True}, None, {'mk_file', 'sub', 'xform', 'count', 'inner', 'mk_text'}),
], ids=['neither', 'graph_label_edges', 'graph_label_stepname'])
def test_each_graph_label_flag_labels_what_it_names(flag: dict[str, Any], edge: str | None, nodes: set[str]) -> None:
    """With `--graph_label_edges` an edge is labelled with the output it carries. A node is labelled with the id the
    document wrote; with `--graph_label_stepname` it is the generated step name instead, which a subworkflow's steps
    carry relative to the subworkflow."""
    dot = _draw(**flag)
    assert [attrs.get('label') for *_, attrs in _edges(dot)] == [edge] * 2
    assert ('label=file' in dot) is (edge is not None)
    assert {attrs['label'] for attrs in _nodes(dot).values()} == nodes


@pytest.mark.fast
@pytest.mark.parametrize(('depth', 'clusters'), [
    (0, {'cluster_oracle'}),
    (1, {'cluster_oracle', 'cluster_sub'}),
    (2, {'cluster_oracle', 'cluster_sub',
         'cluster_sub~inner'}),
])
def test_graph_inline_depth_draws_that_many_levels_of_subworkflow(depth: int, clusters: set[str]) -> None:
    """`--graph_inline_depth N` draws the subworkflows N levels down as clusters and leaves the steps of the
    deeper ones out of the drawing."""
    dot = _draw(inline_depth=depth)
    assert {_unquoted(name) for name in re.findall(rf'subgraph ({_ID})', dot)} == clusters
    drawn = _nodes(dot)
    assert ('sub~count' in drawn) is (depth >= 1)
    assert ('sub~inner~mk_text' in drawn) is (depth >= 2)


@pytest.fixture(name='cli_graph')
def _cli_graph(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., str]:
    """Run the CLI on a workflow in `tmp_path` and return the `.gv` it wrote, with `dot` reported present and its
    commands swallowed."""
    no_preflight(monkeypatch)
    monkeypatch.setattr(main.shutil, 'which', lambda _name: '/usr/bin/dot')
    monkeypatch.setattr(main, 'sub', SimpleNamespace(run=lambda *_a, **_k: SimpleNamespace(stdout=b'')))
    monkeypatch.chdir(tmp_path)

    def run(source: str, *flags: str, sub: str | None = None) -> str:
        write_tree(tmp_path, {'w.wic': source, **({} if sub is None else {'sub.wic': sub})})
        assert run_cli(monkeypatch, '--yaml', tmp_path / 'w.wic', '--generate_cwl_workflow', '--graphviz',
                       *flags) is None
        return (tmp_path / 'autogenerated' / 'w.wic.gv').read_text(encoding='utf-8')
    return run


_TWO_STEPS = """\
steps:
- id: touch
  in:
    filename: !ii empty.txt
- id: cat
"""


@pytest.mark.fast
def test_graphviz_writes_the_graph_of_the_compiled_workflow(cli_graph: Callable[..., str]) -> None:
    """`--graphviz` writes `autogenerated/<stem>.wic.gv` with a node per step and an edge per dependency."""
    drawn = cli_graph(_TWO_STEPS)
    assert re.findall(r'^\s*(\w+) \[label=(\w+) ', drawn, re.MULTILINE) == [
        ('touch', 'touch'), ('cat', 'cat')]
    assert re.findall(r'^\s*(\w+) -> (\w+) \[', drawn, re.MULTILINE) == [('touch', 'cat')]


@pytest.mark.fast
@pytest.mark.parametrize(('flags', 'font', 'blue'), [((), 'black', 'blue'),
                                                     (('--graph_dark_theme',), 'white', 'lightskyblue')])
def test_the_drawing_takes_its_palette_from_the_theme(
        cli_graph: Callable[..., str], flags: tuple[str, ...], font: str, blue: str) -> None:
    """The drawing's text, its box outlines, its inferred edges and an edge's label take one colour: black, or white
    with `--graph_dark_theme`. The background is transparent either way, so a slide gives it its colour; a box is
    not filled, so it takes that colour too. An edge the document wrote is `blue`, whether it joins two steps of one
    document or reaches into a subworkflow; that does not read on a dark background, so the dark theme draws it in a
    lighter blue."""
    drawn = cli_graph(_TWO_STEPS, '--graph_label_edges', *flags)
    assert 'bgcolor=transparent\n' in drawn
    assert f'fontcolor={font}\n' in drawn
    boxes = _nodes(drawn)
    assert {(attrs['color'], attrs['fontcolor']) for attrs in boxes.values()} == {(font, font)}
    assert not any('filled' in attrs['style'] or 'fillcolor' in attrs for attrs in boxes.values())
    assert [(attrs['color'], attrs['fontcolor']) for *_, attrs in _edges(drawn)] == [(font, font)]
    nested = _draw({'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}, 'out': [{'file': {'wic_anchor': 'f'}}]},
        {'id': 'xform', 'in': {'file': {'wic_alias': 'f'}, 'name': {'wic_inline_input': 'b'}}},
        subworkflow_step('sub.wic', {'steps': [
            {'id': 'xform', 'in': {'file': {'wic_alias': 'f'}, 'name': {'wic_inline_input': 'c'}}},
            {'id': 'count'},                                       # file inferred from xform
        ]}),
    ]}, dark_theme='--graph_dark_theme' in flags)
    assert {(source, sink): attrs.get('color') for source, sink, attrs in _edges(nested)} == {
        ('mk_file', 'xform'): blue,
        ('mk_file', 'sub~xform'): blue,
        ('sub~xform', 'sub~count'): font,
    }


@pytest.mark.fast
def test_the_root_graphviz_label_titles_the_drawing(cli_graph: Callable[..., str]) -> None:
    """`wic: graphviz: label:` on the workflow is the title of the cluster that draws it."""
    drawn = cli_graph('wic:\n  graphviz:\n    label: My Pipeline\n' + _TWO_STEPS)
    assert 'label="My Pipeline"' in drawn
    assert 'label="My Pipeline"' not in cli_graph(_TWO_STEPS)


#: A workflow whose second step calls `sub.wic`, which the `cli_graph` fixture writes beside it.
_SUB_CLUSTER = 'cluster_sub'
_CALLS_SUB = """\
steps:
- id: touch
  in:
    filename: !ii empty.txt
- id: sub.wic
  run: sub.wic
"""


def _with(steps: str, graphviz: Yaml | None = None, entries: dict[str, Yaml] | None = None) -> str:
    """`steps` under a `wic:` block whose own `graphviz:` is `graphviz`, and whose `steps:` entry
    `key` has `entries[key]` as its `graphviz:`."""
    wic: Yaml = {'graphviz': graphviz} if graphviz else {}
    if entries:
        wic['steps'] = {key: {'wic': {'graphviz': value}} for key, value in entries.items()}
    return (yaml.safe_dump({'wic': wic}, sort_keys=False) if wic else '') + steps


@pytest.mark.fast
@pytest.mark.parametrize('address', ['(1, touch)', 'touch'])
@pytest.mark.parametrize(('flags', 'labels'), [
    ((), ('Make\\nit', 'cat')),
    (('--graph_label_stepname',), ('touch', 'cat')),
])
def test_a_step_label_names_its_box_unless_the_flag_asks_for_generated_names(
        cli_graph: Callable[..., str], address: str, flags: tuple[str, ...], labels: tuple[str, str]) -> None:
    """A step's `wic: graphviz: label`, written under its `(index, name)` or its id, replaces the id on its
    box, `\\n` and all; `--graph_label_stepname` replaces both with the generated step name."""
    nodes = _nodes(cli_graph(_with(_TWO_STEPS, entries={address: {'label': 'Make\\nit'}}), *flags))
    assert (nodes['touch']['label'], nodes['cat']['label']) == labels


@pytest.mark.fast
def test_a_step_style_is_appended_to_the_style_of_its_box(cli_graph: Callable[..., str]) -> None:
    """A step's `wic: graphviz: style` follows `rounded`; a step without one keeps that alone."""
    nodes = _nodes(cli_graph(_with(_TWO_STEPS, entries={'touch': {'style': 'dashed, bold'}})))
    assert nodes['touch']['style'] == 'rounded, dashed, bold'
    assert nodes['cat']['style'] == 'rounded'


@pytest.mark.fast
@pytest.mark.parametrize(('own', 'call', 'cluster', 'box'), [
    (None, None, ('label', 'sub.wic'), 'rounded'),
    ({'label': 'Own'}, None, ('label', 'Own'), 'rounded'),
    (None, {'label': 'Called'}, ('label', 'Called'), 'rounded'),
    ({'label': 'Own'}, {'label': 'Called'}, ('label', 'Called'), 'rounded'),
    ({'style': 'invis'}, None, ('style', 'invis'), 'rounded, invis'),
    (None, {'style': 'invis'}, ('style', 'invis'), 'rounded, invis'),
], ids=['call-id', 'own-label', 'call-label', 'call-wins', 'own-invis', 'call-invis'])
def test_a_subworkflow_cluster_takes_the_label_and_style_of_its_call_else_its_own(
        cli_graph: Callable[..., str], own: Yaml | None, call: Yaml | None, cluster: tuple[str, str], box: str) -> None:
    """The subworkflow's own `wic: graphviz: label` titles its cluster, and the calling step's entry wins
    over it; without either the cluster is titled with the calling step's id. `style: invis`, in its own file or
    at the call, hides the whole subworkflow: its cluster and the box that calls it."""
    drawn = cli_graph(_with(_CALLS_SUB, entries={'sub.wic': call} if call else None), sub=_with(_TWO_STEPS, own))
    attr, value = cluster
    assert _cluster(drawn, _SUB_CLUSTER)[attr] == value
    nodes = _nodes(drawn)
    assert (nodes['sub']['style'], nodes['touch']['style']) == (box, 'rounded')


@pytest.mark.fast
def test_two_calls_to_one_subworkflow_are_two_clusters_each_with_its_own_look(
        cli_graph: Callable[..., str]) -> None:
    """Each call to a subworkflow is a cluster named after the step that calls it, so the label and
    style a call's `wic: steps:` entry gives belong to that call alone."""
    calls = _CALLS_SUB + "- id: sub.wic\n  run: sub.wic\n"
    entries = {'(2, sub.wic)': {'label': 'First'}, '(3, sub.wic)': {'label': 'Second', 'style': 'invis'}}
    drawn = cli_graph(_with(calls, entries=entries), sub=_TWO_STEPS)
    first, second = _cluster(drawn, 'cluster_sub@2'), _cluster(drawn, 'cluster_sub@3')
    assert (first['label'], first.get('style'), second['label'], second['style']) == ('First', None, 'Second', 'invis')
    assert _nodes(drawn)['sub@2']['style'] == 'rounded'


@pytest.mark.fast
@pytest.mark.parametrize(('root', 'sub', 'ranks'), [
    (['(1, touch)', '(2, sub.wic)'], [], [(2, ['touch', 'sub'])]),
    ([], ['(1, touch)', '(2, cat)'],
     [(3, ['sub~touch', 'sub~cat'])]),
    (['(1, touch)', '(3, touch)'], [], []),
])
def test_ranksame_puts_the_steps_it_names_on_one_rank_in_their_workflow_s_cluster(
        cli_graph: Callable[..., str], root: list[str], sub: list[str], ranks: list[tuple[int, list[str]]]) -> None:
    """A workflow's `ranksame` steps share one rank=same group in its own cluster, the root's included.
    A group of fewer than two steps that exist is not drawn."""
    drawn = cli_graph(_with(_CALLS_SUB, {'ranksame': root}), sub=_with(_TWO_STEPS, {'ranksame': sub}))
    assert _ranks(drawn) == ranks


@pytest.mark.fast
def test_a_ranksame_entry_that_addresses_no_step_is_named_on_stderr(
        cli_graph: Callable[..., str], capsys: pytest.CaptureFixture[str]) -> None:
    """The line names the file, the entry and the steps the document has, as for a stale `wic: steps:` key."""
    cli_graph(_with(_TWO_STEPS, {'ranksame': ['(1, touch)', '(3, touch)']}))
    assert ("Warning! w.wic: wic: graphviz: ranksame entry (3, touch) addresses no step of 'w': "
            'there is no step 3; the document has 2 steps. The entry is ignored.') in capsys.readouterr().err
