"""The graph Sophios draws: what each graph setting and the CLI's `--graphviz` do to it.

These compile in memory and read the DOT source `drawing.draw` makes of the result, or
run the CLI and read the `.gv` it writes. None of them renders an image.
"""
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from sophios import compiler, drawing, main, preflight
from sophios.cli import default_compilation_settings, get_args, get_dicts_for_compilation
from sophios.ir.resolve import run_path_name
from sophios.wic_types import Yaml

from .hermetic import bundle, subworkflow_step
from .synthetic_tools import SYNTHETIC_TOOLS

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


def _draw(document: Yaml = _DOCUMENT, *, label_edges: bool = False, label_stepname: bool = False,
          inline_depth: int = sys.maxsize, dark_theme: bool = False) -> str:
    """`document` compiled, then drawn with the given graph settings: the DOT source."""
    source = bundle(document, 'oracle', SYNTHETIC_TOOLS)
    compiled = compiler.compile_source(source, default_compilation_settings(), relative_run_path=True, testing=True)
    _options, settings = get_dicts_for_compilation(get_args())
    settings['graph_label_edges'] = label_edges
    settings['graph_label_stepname'] = label_stepname
    settings['graph_inline_depth'] = inline_depth
    settings['graph_dark_theme'] = dark_theme
    dot: str = drawing.draw(compiled, source, settings, 'oracle').source
    return dot


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
    block = re.search(rf'subgraph {name} {{\n((?:\t+\w+=.*\n)*)', dot)
    assert block is not None, f'{name} is not drawn'
    return {key: _unquoted(value) for key, value in _ATTRIBUTE.findall(block.group(1))}


def _ranks(dot: str) -> list[tuple[int, list[str]]]:
    """Each rank=same group, with its indent (2 in the root's cluster, 3 in a cluster within it) and its nodes."""
    return [(len(indent), [_unquoted(name) for name in names.split('; ')])
            for indent, names in re.findall(r'^(\t*)\{rank=same; (.*)\}$', dot, re.MULTILINE)]


@pytest.mark.fast
@pytest.mark.parametrize(('labelled', 'expected'), [(True, 'file'), (False, None)])
def test_graph_label_edges_names_the_output_each_edge_carries(labelled: bool, expected: str | None) -> None:
    """With `--graph_label_edges` an edge is labelled with the output it carries."""
    dot = _draw(label_edges=labelled)
    edges = _edges(dot)
    assert len(edges) == 2
    assert [attrs.get('label') for _source, _sink, attrs in edges] == [expected] * 2
    assert ('label=file' in dot) is labelled


@pytest.mark.fast
@pytest.mark.parametrize(('qualified', 'labels'), [
    (False, {'mk_file', 'sub.wic', 'xform', 'count', 'inner.wic', 'mk_text'}),
    (True, {'oracle__step__1__mk_file', 'oracle__step__2__sub.wic', 'sub__step__1__xform',
            'sub__step__2__count', 'sub__step__3__inner.wic', 'inner__step__1__mk_text'}),
])
def test_graph_label_stepname_labels_a_node_with_the_generated_step_name(qualified: bool, labels: set[str]) -> None:
    """A node is labelled with the id the document wrote; with `--graph_label_stepname` it is the generated
    step name instead, which a subworkflow's steps carry relative to the subworkflow."""
    assert {attrs['label'] for attrs in _nodes(_draw(label_stepname=qualified)).values()} == labels


@pytest.mark.fast
@pytest.mark.parametrize(('depth', 'clusters'), [
    (0, {'cluster_oracle'}),
    (1, {'cluster_oracle', 'cluster_sub'}),
    (2, {'cluster_oracle', 'cluster_sub', 'cluster_inner'}),
])
def test_graph_inline_depth_draws_that_many_levels_of_subworkflow(depth: int, clusters: set[str]) -> None:
    """`--graph_inline_depth N` draws the subworkflows N levels down as clusters and leaves the steps of the
    deeper ones out of the drawing."""
    dot = _draw(inline_depth=depth)
    assert set(re.findall(r'subgraph (cluster_\w+)', dot)) == clusters
    drawn = _nodes(dot)
    assert ('oracle__step__2__sub.wic___sub__step__2__count' in drawn) is (depth >= 1)
    assert ('oracle__step__2__sub.wic___sub__step__3__inner.wic___inner__step__1__mk_text' in drawn) is (depth >= 2)


@pytest.mark.fast
@pytest.mark.parametrize(('dark_theme', 'font_colour'), [(False, 'white'), (True, 'black')])
def test_the_graph_draws_inferred_edges_in_the_font_colour_and_the_rest_in_blue(
        dark_theme: bool, font_colour: str) -> None:
    """An edge the document wrote is blue, whether it joins two steps of one
    document or reaches into a subworkflow; an edge the compiler inferred takes
    the theme's font colour, which is black on a dark theme and white otherwise.
    """
    document: Yaml = {'steps': [
        {'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}}, 'out': [{'file': {'wic_anchor': 'f'}}]},
        {'id': 'xform', 'in': {'file': {'wic_alias': 'f'}, 'name': {'wic_inline_input': 'b'}}},
        subworkflow_step('sub.wic', {'steps': [
            {'id': 'xform', 'in': {'file': {'wic_alias': 'f'}, 'name': {'wic_inline_input': 'c'}}},
            {'id': 'count'},                                       # file inferred from xform
        ]}),
    ]}
    colours = {(source, sink): attrs.get('color')
               for source, sink, attrs in _edges(_draw(document, dark_theme=dark_theme))}
    assert colours == {
        ('oracle__step__1__mk_file', 'oracle__step__2__xform'): 'blue',
        ('oracle__step__1__mk_file', 'oracle__step__3__sub.wic___sub__step__1__xform'): 'blue',
        ('oracle__step__3__sub.wic___sub__step__1__xform',
         'oracle__step__3__sub.wic___sub__step__2__count'): font_colour,
    }


@pytest.fixture(name='cli_graph')
def _cli_graph(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., str]:
    """Run the CLI on a workflow in `tmp_path` and return the `.gv` it wrote.

    `dot` is reported present and the commands that would run it are swallowed: what is under
    test is the DOT source Sophios writes, not the image Graphviz makes of it.
    """
    monkeypatch.setattr(preflight, 'prepare', lambda *_a, **_k: None)
    monkeypatch.setattr(main.shutil, 'which', lambda _name: '/usr/bin/dot')
    monkeypatch.setattr(main, 'sub', SimpleNamespace(run=lambda *_a, **_k: None))
    monkeypatch.chdir(tmp_path)

    def run(source: str, *flags: str, sub: str | None = None) -> str:
        workflow = tmp_path / 'w.wic'
        workflow.write_text(source, encoding='utf-8')
        if sub is not None:
            (tmp_path / 'sub.wic').write_text(sub, encoding='utf-8')
        monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), '--generate_cwl_workflow',
                                         '--graphviz', *flags])
        main.main()
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
        ('w__step__1__touch', 'touch'), ('w__step__2__cat', 'cat')]
    assert re.findall(r'^\s*(\w+) -> (\w+) \[', drawn, re.MULTILINE) == [('w__step__1__touch', 'w__step__2__cat')]


@pytest.mark.fast
@pytest.mark.parametrize(('flags', 'font'), [((), 'white'), (('--graph_dark_theme',), 'black')])
def test_graph_dark_theme_sets_the_font_of_the_drawing(
        cli_graph: Callable[..., str], flags: tuple[str, ...], font: str) -> None:
    """The drawing's font and its inferred edges take one colour, white, or black with `--graph_dark_theme`."""
    drawn = cli_graph(_TWO_STEPS, *flags)
    assert f'fontcolor={font}\n' in drawn
    assert f'-> w__step__2__cat [color={font}]' in drawn


@pytest.mark.fast
def test_the_root_graphviz_label_titles_the_drawing(cli_graph: Callable[..., str]) -> None:
    """`wic: graphviz: label:` on the workflow is the title of the cluster that draws it."""
    drawn = cli_graph('wic:\n  graphviz:\n    label: My Pipeline\n' + _TWO_STEPS)
    assert 'label="My Pipeline"' in drawn
    assert 'label="My Pipeline"' not in cli_graph(_TWO_STEPS)


#: A workflow whose second step calls `sub.wic`, which the `cli_graph` fixture writes beside it,
#: and the name `sub.wic` is compiled under.
_SUB = run_path_name('sub.wic', 'sub.wic')
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
    (('--graph_label_stepname',), ('w__step__1__touch', 'w__step__2__cat')),
])
def test_a_step_label_names_its_box_unless_the_flag_asks_for_generated_names(
        cli_graph: Callable[..., str], address: str, flags: tuple[str, ...], labels: tuple[str, str]) -> None:
    """A step's `wic: graphviz: label`, written under its `(index, name)` or its id, replaces the id on its
    box, `\\n` and all; `--graph_label_stepname` replaces both with the generated step name."""
    nodes = _nodes(cli_graph(_with(_TWO_STEPS, entries={address: {'label': 'Make\\nit'}}), *flags))
    assert (nodes['w__step__1__touch']['label'], nodes['w__step__2__cat']['label']) == labels


@pytest.mark.fast
def test_a_step_style_is_appended_to_the_style_of_its_box(cli_graph: Callable[..., str]) -> None:
    """A step's `wic: graphviz: style` follows `rounded, filled`; a step without one keeps that alone."""
    nodes = _nodes(cli_graph(_with(_TWO_STEPS, entries={'touch': {'style': 'dashed, bold'}})))
    assert nodes['w__step__1__touch']['style'] == 'rounded, filled, dashed, bold'
    assert nodes['w__step__2__cat']['style'] == 'rounded, filled'


@pytest.mark.fast
@pytest.mark.parametrize(('own', 'call', 'title'), [
    (None, None, 'sub.wic'),
    ({'label': 'Own'}, None, 'Own'),
    (None, {'label': 'Called'}, 'Called'),
    ({'label': 'Own'}, {'label': 'Called'}, 'Called'),
])
def test_a_subworkflow_cluster_is_titled_by_its_label_else_by_the_id_that_calls_it(
        cli_graph: Callable[..., str], own: Yaml | None, call: Yaml | None, title: str) -> None:
    """The subworkflow's own `wic: graphviz: label` titles its cluster, and the calling step's entry wins
    over it; without either the cluster is titled with the calling step's id."""
    drawn = cli_graph(_with(_CALLS_SUB, entries={'sub.wic': call} if call else None), sub=_with(_TWO_STEPS, own))
    assert _cluster(drawn, f'cluster_{_SUB}')['label'] == title


@pytest.mark.fast
@pytest.mark.parametrize(('own', 'call'), [({'style': 'invis'}, None), (None, {'style': 'invis'})])
def test_an_invisible_subworkflow_hides_its_cluster_and_the_box_that_calls_it(
        cli_graph: Callable[..., str], own: Yaml | None, call: Yaml | None) -> None:
    """`style: invis` on a subworkflow, in its own file or at the call, hides the whole subworkflow."""
    drawn = cli_graph(_with(_CALLS_SUB, entries={'sub.wic': call} if call else None), sub=_with(_TWO_STEPS, own))
    assert _cluster(drawn, f'cluster_{_SUB}')['style'] == 'invis'
    nodes = _nodes(drawn)
    assert nodes['w__step__2__sub.wic']['style'] == 'rounded, filled, invis'
    assert nodes['w__step__1__touch']['style'] == 'rounded, filled'


@pytest.mark.fast
@pytest.mark.parametrize(('root', 'sub', 'ranks'), [
    (['(1, touch)', '(2, sub.wic)'], [], [(2, ['w__step__1__touch', 'w__step__2__sub.wic'])]),
    ([], ['(1, touch)', '(2, cat)'],
     [(3, [f'w__step__2__sub.wic___{_SUB}__step__1__touch', f'w__step__2__sub.wic___{_SUB}__step__2__cat'])]),
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
