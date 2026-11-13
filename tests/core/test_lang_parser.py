"""The Sophios syntax layer. Each family of documents under tests/core/data/parse reads as its expected.yaml states;
properties over generated text and documents show that parsing is total, spans its text, reads YAML as YAML does and
reads a construct alike in either spelling; and every diagnostic code is provoked somewhere."""
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, cast, get_args

import jsonschema
import pytest
import yaml
from hypothesis import example, given
from hypothesis import strategies as st

from sophios.lang import (
    SophiosError,
    SophiosErrorCode,
    CwlRecord,
    Document,
    EdgeDef,
    EdgeRef,
    InlineLiteral,
    InputValue,
    RawCwlRef,
    Step,
    UnresolvedName,
    WicSidecar,
    parse,
    render,
    to_json,
    wic_schema,
)
from sophios.lang.spans import SourceSpan
from sophios.utils_yaml import Key

from . import provocations
from .budgets import budget
from .cases import DATA, case_dirs, diagnostic as as_written
from .test_setup import workflow_paths
from .strategies import documents, identifiers, input_lines, pinned, scalar_spellings
from .wic_reading import YamlWithSophiosTagsOpaque

FAST = budget(200)

#: The exported schema, as an editor validates a document's JSON projection with it.
SCHEMA_VALIDATOR: Final = jsonschema.Draft202012Validator(wic_schema())


def reading(node: Any) -> Any:  # pylint: disable=too-many-return-statements  # one per node kind
    """`node` as plain data, the form a family's expected.yaml states it in: each node its AST fields by name, empty
    ones left out; a construct its tag around what it carries; a name itself; a `wic:` step key [index, name]; and a
    NaN, which equals nothing, the text NaN."""
    match node:
        case Document():
            return _fields(steps=[reading(step) for step in node.steps], wic=node.sidecar and reading(node.sidecar),
                           passthrough=node.passthrough)
        case Step():
            return _fields(id=node.id, inputs=node.inputs,
                           outputs=[out.name if out.edge_def is None else {out.name: reading(out.edge_def)}
                                    for out in node.outputs],
                           interpreted=node.interpreted, passthrough=node.passthrough)
        case WicSidecar():
            return _fields(steps=[[key.index, key.name, reading(child)] for key, child in node.steps],
                           entries=node.entries)
        case InlineLiteral():
            return {'!ii': reading(node.value)}
        case EdgeRef():
            return {'!*': node.name}
        case EdgeDef():
            return {'!&': node.name}
        case RawCwlRef():
            return {'!cwl': node.expression}
        case CwlRecord():
            return {'!cwl': {'source': [reading(source) for source in node.sources], **reading(dict(node.fields))}}
        case UnresolvedName():
            return node.name
        case dict():
            return {key: reading(value) for key, value in node.items()}
        case list():
            return [reading(value) for value in node]
        case float() if math.isnan(node):
            return 'NaN'
    return node


def _fields(**named: Any) -> dict[str, Any]:
    """Each field that holds something, its (key, value) pairs read as a mapping."""
    return {name: reading(dict(value)) if isinstance(value, tuple) else value
            for name, value in named.items() if value not in (None, (), [])}


@pytest.mark.fast
@pytest.mark.parametrize('family, expected', case_dirs('parse', 'documents', 'reads'))
def test_a_document_reads_as_its_family_states(family: Path, expected: dict[str, Any]) -> None:
    """Each `.wic` document of a family under tests/core/data/parse, one per form, holds many faults or none. One
    reading of it reports exactly the diagnostics its expected.yaml lists, each an error, by code, line and column in
    reading order, so no fault hides the next; and it reads as `reads` states, or else returns a document regardless."""
    assert sorted(expected['documents']) == sorted(path.name for path in family.glob('*.wic'))
    for name, report in expected['documents'].items():
        result = parse((family / name).read_text(encoding='utf-8'), name)
        found = [as_written(d) for d in result.diagnostics]
        assert [[d['code'], d['line'], d['column']] for d in found] == report, name
        assert {d['severity'] for d in found} <= {'error'}, name
        if name in expected.get('reads', {}):
            assert reading(result.document) == expected['reads'][name], name
        else:
            assert result.document is not None, name


_tag_pool = st.sampled_from(['', '!ii ', '!& ', '!* ', '!cwl ', '!foo ', '!x '])


@st.composite
def adversarial_yaml(draw: st.DrawFn) -> str:
    """Structured YAML text with anchors, aliases, tags, and real nesting, which arbitrary text almost never forms."""
    fragments = []
    anchors: list[str] = []
    for i in range(draw(st.integers(min_value=1, max_value=6))):
        key = draw(identifiers)
        shape = draw(st.sampled_from(['scalar', 'flow_list', 'flow_map', 'anchor', 'alias', 'self_ref', 'nested']))
        tag = draw(_tag_pool)
        match shape:
            case 'scalar':
                fragments.append(f'{key}: {tag}{draw(identifiers)}')
            case 'flow_list':
                items = ', '.join(draw(st.lists(identifiers, min_size=0, max_size=4)))
                fragments.append(f'{key}: {tag}[{items}]')
            case 'flow_map':
                fragments.append(f'{key}: {tag}{{{draw(identifiers)}: {draw(identifiers)}}}')
            case 'anchor':
                anchors.append(f'a{i}')
                fragments.append(f'{key}: &a{i} [{draw(identifiers)}]')
            case 'alias' if anchors:
                ref = draw(st.sampled_from(anchors))
                repeats = ', '.join([f'*{ref}'] * draw(st.integers(min_value=1, max_value=6)))
                anchors.append(f'a{i}')
                fragments.append(f'{key}: &a{i} [{repeats}]')
            case 'self_ref':
                fragments.append(f'{key}: &s{i} [x, *s{i}]')
            case _:
                nested = draw(identifiers)
                for _ in range(draw(st.integers(min_value=1, max_value=5))):
                    nested = f'{{n: {nested}}}'
                fragments.append(f'{key}: {nested}')
    return '\n'.join(fragments) + '\n'


@pytest.mark.fast
@given(st.one_of(st.text(max_size=400), st.text('abcdefghijklmnopqrstuvwxyz!&*i {}[]:#-', max_size=120),
                 adversarial_yaml()))
# Hypothesis's failure database does not travel to CI, so what it once found by luck is pinned.
@example('!: :')                   # an unknown tag on a mapping key, the position the tag rule first missed
@example('!foo x: y')              # the same, spelled legibly
@example('!!: :')                  # a core tag with an empty suffix, which YAML cannot construct
@example('a: !!int')               # a core number tag over an empty value, which raised IndexError
@example('wic: &w\n  steps: *w\n')  # a malformed key through an alias, not a cycle
@example('_: !& []\n')             # a name tag on a collection
@example('a: !cwl b\n')            # a tag the language owns, in passthrough
@budget(500)
def test_parsing_is_total(text: str) -> None:
    """Parsing never raises, whatever it is handed: any text, or YAML with aliases, cycles, tags and nesting. It
    returns a document or diagnostics; each diagnostic sits at a span inside the text, none is reported twice, and
    they come in reading order however many passes made them; and what it accepts, YAML loads with the Sophios tags
    opaque. The reverse is allowed (`!ii 2020-13-45` is wic009), but the parser is never more permissive than YAML."""
    result = parse(text, 'fuzz.wic')
    assert result.document is not None or result.diagnostics
    reports = [(d.severity, d.code, d.message, d.span) for d in result.diagnostics]
    assert len(reports) == len(set(reports)), f'duplicate diagnostics: {reports}'
    positions = []
    for found in result.diagnostics:
        span = found.span
        assert span is not None and span.file == 'fuzz.wic', found
        assert 1 <= span.start_line <= max(len(text.splitlines()), 1) + 1 and span.start_column >= 1, found
        positions.append((span.start_line, span.start_column))
    assert positions == sorted(positions), reports
    if result.ok and result.document is not None:
        yaml.load(text, Loader=YamlWithSophiosTagsOpaque)  # must not raise


def _spans(node: Any) -> list[SourceSpan | None]:
    """Every span reachable from an AST node; a value node's span is never absent on a parsed document."""
    match node:
        case Document():
            return [*_present(node.span), *(span for step in node.steps for span in _spans(step)),
                    *(_spans(node.sidecar) if node.sidecar is not None else [])]
        case Step():
            edges = [out.edge_def.span for out in node.outputs if out.edge_def is not None]
            return [*_present(node.span), *(span for _, value in node.inputs for span in _spans(value)),
                    *(out.span for out in node.outputs), *edges]
        case WicSidecar():
            return [*_present(node.span), *(span for _, child in node.steps for span in _spans(child))]
        case InlineLiteral() | EdgeRef() | RawCwlRef() | UnresolvedName():
            return [node.span]
    return []


def _present(span: SourceSpan | None) -> list[SourceSpan | None]:
    return [span] if span is not None else []


@pytest.mark.fast
@given(documents())
@example('steps:\n  touch:\n    in:\n      f: !ii x\n')
@example('steps:\n- id: touch\n  in:\n    f: !ii x\n')
@FAST
def test_a_well_formed_document_is_read_whole(source: str) -> None:
    """A well-formed document, in either step form with every input spelling, outputs and nested `wic:` blocks, parses
    without a diagnostic. Every node carries a span pointing at real source; every input value is one of the five
    forms; written in the other step form, it reads back in that form and is otherwise the same document, so the writer
    keeps the form it is given; and its JSON projection is JSON, which the exported schema accepts."""
    result = parse(source, 'gen.wic')
    assert result.ok and result.document is not None, [str(d) for d in result.diagnostics]
    document = result.document
    lines = source.splitlines()
    spans = _spans(document)
    assert spans, 'no spans collected; the walk is not reaching the tree'
    for span in spans:
        assert span is not None and 1 <= span.start_line <= len(lines), span
        assert 1 <= span.start_column <= len(lines[span.start_line - 1]) + 1, span
    for step in document.steps:
        assert all(isinstance(value, get_args(InputValue)) for _, value in step.inputs), step
    other = replace(document, steps_as_mapping=not document.steps_as_mapping)
    again = parse(render(other), 'b.wic')
    assert again.ok and again.document is not None, [str(d) for d in again.diagnostics]
    assert again.document.steps_as_mapping is other.steps_as_mapping
    assert to_json(replace(again.document, steps_as_mapping=document.steps_as_mapping)) == to_json(document)
    projection = json.loads(json.dumps(to_json(document)))
    assert SCHEMA_VALIDATOR.is_valid(projection), list(SCHEMA_VALIDATOR.iter_errors(projection))


#: Each tag and its desugared key.
SPELLINGS: Final = {'!ii': Key.INLINE_INPUT, '!*': Key.ALIAS, '!cwl': Key.RAW_CWL, '!&': Key.ANCHOR}


@pytest.mark.fast
@given(st.sampled_from(sorted(SPELLINGS)), identifiers, _tag_pool,
       st.one_of(identifiers, st.builds(lambda key: f'{{{key}: x}}', identifiers | st.sampled_from(sorted(Key.ALL)))))
@FAST
def test_a_construct_reads_alike_in_either_spelling_and_position(tag: str, payload: str, other: str,
                                                                 value: str) -> None:
    """One construct, one reading. Its tagged spelling, as people write it, and its desugared one, as the Python API
    emits it, parse to the same document: an input for `!ii`, `!*` and `!cwl`, and an output's edge for `!&`, which
    defines one only there. And a value makes the same unknown-tag call in input position as in passthrough, over each
    tag and each construct key, `!foo {wic_anchor: x}` among them, where the two positions once disagreed."""
    place = '  out:\n  - f:' if tag == '!&' else '  in:\n    f:'
    tagged, desugared = (parse(f'steps:\n- id: s\n{place} {spelled}\n', 'spelled.wic')
                         for spelled in (f'{tag} {payload}', f'{{{SPELLINGS[tag]}: {payload}}}'))
    assert tagged.ok and desugared.ok, [str(d) for d in (*tagged.diagnostics, *desugared.diagnostics)]
    assert reading(tagged.document) == reading(desugared.document)
    in_position = parse(f'steps:\n- id: s\n  in:\n    a: {other}{value}\n', 'position.wic')
    passthrough = parse(f'top: {other}{value}\n', 'position.wic')
    assert ({d.code for d in in_position.diagnostics} & {SophiosErrorCode.UNKNOWN_TAG}
            == {d.code for d in passthrough.diagnostics} & {SophiosErrorCode.UNKNOWN_TAG}), f'{other}{value}'


#: A step's own keys but `id`, written out rather than read off `Grammar.STEP_KEYS`, which they are held to.
_STEP_KEYS: Final = ('in', 'out', 'run', 'scatter', 'scatterMethod', 'when')
_ENTRY_BODIES = st.one_of(
    st.sampled_from(['', ' 3', ' [a, b]', ' {a: 1}', ' {}', ' !bogus x', ' !ii 1', ' {id: !ii 1}', ' !& e',
                     ' {wic_anchor: e}']),
    st.lists(st.one_of(st.just('id: t'), st.just('out: [f]'), st.just('label: x'),
                       input_lines(indent='      ').map(lambda line: 'in:\n' + line)),
             min_size=1, max_size=3, unique=True).map(lambda keys: ''.join(f'\n    {key}' for key in keys)))


@pytest.mark.fast
@given(st.one_of(identifiers, st.sampled_from([*_STEP_KEYS, 'doc', 'label'])).filter(lambda key: key != 'id'),
       _ENTRY_BODIES.filter(lambda body: not body.startswith(' !bogus')))
@example('in', ' [a, b]')                              # a forgotten id:, its body a non-mapping in:
@example('out', ' {a: 1}')                             # a forgotten id:, its body a non-sequence out:
@example('in', ' {id: !ii 1}')                         # a forgotten id:, an input legitimately named id
@example('touch', ' [a, b]')                           # a step named touch, its body a sequence
@example('touch', ' 3')                                # a scalar body
@example('touch', '')                                  # a null body
@example('touch', '\n    in:\n      f: !bogus x')      # an unknown tag inside
@example('touch', '\n    id: t')                       # a second identity
@example('touch', ' !bogus x').xfail(raises=AssertionError, reason='wic009 comes before wic003 here, after it in '
                                     "the mapping form")
@FAST
def test_a_step_entry_without_an_id_reports_what_its_fix_reports(key: str, body: str) -> None:
    """Taking the wic006 message's advice must not hand back a fresh error: `- K: V` owes exactly the diagnostics
    of the spelling that message recommends, plus its own wic006, or the user re-runs and is handed one the sequence
    spelling never mentioned. A step key `K` is a forgotten `- id:` line, so the entry is the body and the `id:` form
    holding it is the fix; any other `K` names the step, as `K: V` in the mapping form does. An unknown tag on a whole
    non-mapping body is not drawn: the mapping form reports its wic003 at the key, the sequence form at the value."""
    fix = f'steps:\n- id: s\n  {key}:{body}\n' if key in _STEP_KEYS else f'steps:\n  {key}:{body}\n'
    rejected = [d.code for d in parse(f'steps:\n- {key}:{body}\n', 'rejected.wic').diagnostics]
    assert rejected == [SophiosErrorCode.MISSING_STEP_ID, *(d.code for d in parse(fix, 'fix.wic').diagnostics)]


def _payload(value: Any) -> Any:
    """The value an `!ii` input carries."""
    assert isinstance(value, InlineLiteral), value
    return value.value


@st.composite
def tagged_documents(draw: st.DrawFn) -> tuple[str, bool]:
    """A core or global tag, or none, on a scalar, a flow sequence, a flow mapping or a merge, nested two deep, as a
    value, a key or an item, inside an `!ii` payload or a `!cwl` record; and whether that place is passthrough."""
    tags = st.sampled_from([''] * 12 + [
        '!!str ', '!!int ', '!!float ', '!!bool ', '!!null ', '!!timestamp ', '!!binary ', '!!map ', '!!seq ',
        '!!set ', '!!omap ', '!!pairs ', '!!merge ', '!!foo ', '!<tag:example.com,2000:x> '])

    def node(depth: int) -> str:
        shape = draw(st.sampled_from(['', '[{}]', '{{k: {}}}', '{{<<: {}}}'] if depth else ['']))
        leaf = st.sampled_from(['x', '1', '1.5', 'true', '', '2020-01-01', 'aGk='])
        return draw(tags) + (shape.format(node(depth - 1)) if shape else draw(leaf))

    where = draw(st.sampled_from(['a: {}', '? {}\n: v', 'a: [{}]', 'steps:\n  s:\n    in:\n      f: !ii [{}]',
                                  'steps:\n  s:\n    in:\n      f: !cwl {{default: {}}}']))
    return where.format(node(0 if where.startswith('?') else 2)) + '\n', not where.startswith('steps')


@pytest.mark.fast
@given(tagged_documents())
@pinned(*[(source, True) for source in [
    'a: !!str [a]', 'a: !!map b', 'a: !!seq b', 'a: !!set [a]', '!!int a: b', 'a: !!int abc', 'a: !!float x',
    'a: !!bool x', 'a: !!timestamp x', 'a: !!int', 'a: !!int ""', 'a: !!float ""', 'a: !!omap [{b: !!int x}]',
    'a: !!pairs [{b: {c: !!bool x}}]', 'a: !!set {x: [!!int y]}', 'x: &m {p: 1}\na: {<<: b}\n', 'a: {<<: [b]}\n',
    'a: {<<: !!str s}\n', 'a: !!merge x\n', 'a: [!!merge x]\n']],
    *[(source, False) for source in ['!!: :', '!!foo: 1', 'k: !!foo 1', 'k: !<tag:example.com,2000:x> 1']],
    *[(f'steps:\n  s:\n    in:\n      f: {value}\n', False)
      for value in ['!ii {<<: b}', '!ii [!!merge x]', '!cwl {<<: b}']])
@FAST
def test_what_yaml_cannot_construct_is_one_wic009(document: tuple[str, bool]) -> None:
    """A tag YAML cannot construct, or a node its tag cannot build, is one wic009, and in passthrough nothing else; the
    parser never raises. The diagnostic names its subject: a failed merge as a merge, an undefined tag by name, a value
    YAML cannot read as such. What YAML builds is no wic009."""
    source, passthrough = document
    diagnostics = parse(source, 't.wic').diagnostics
    messages = [d.message for d in diagnostics if d.code is SophiosErrorCode.UNKNOWN_TAG]
    try:
        yaml.load(source, Loader=YamlWithSophiosTagsOpaque)
    except (yaml.YAMLError, ValueError, KeyError, AttributeError, IndexError) as exc:
        problem = str(getattr(exc, 'problem', ''))
        assert len(messages) == 1 and not (passthrough and len(diagnostics) > 1), [str(d) for d in diagnostics]
        named = re.search(r"for the tag '([^']*)'", problem)
        said = ('merg' if 'merg' in problem else f"'{named[1]}'" if named
                else '' if isinstance(exc, yaml.YAMLError) else 'YAML cannot read this value as ')
        assert said in messages[0], messages
    else:
        assert not messages, messages


@pytest.mark.fast
@given(scalar_spellings())
@pinned(*[(text, None) for text in ['5', '007', '1:30', 'true', "'5'", '"5"', "'007'", "'1:30'", "'true'", '"a\\tb"',
                                    "'2020-13-45'", '"2020-13-45"', '010', '0x1A', '2020-01-01', '.inf', '12:30',
                                    'null', '3.14', 'plain']], ('2020-13-45', 'timestamp'))
@FAST
def test_a_scalar_is_read_as_yaml_reads_it(spelled: tuple[str, str | None]) -> None:
    """Scalar reading is delegated to PyYAML, not re-implemented: a hand-written table diverged on every pinned
    passthrough spelling. A plain `!ii` scalar is typed as YAML types it, a quoted one is its text. A value YAML
    resolves but cannot construct is one wic009 at its line naming what YAML took it for, not a tag never written."""
    text, tag = spelled
    if tag is not None:
        for line, above in [(1, 'a:'), (2, 'top:\n  k:'), (4, 'steps:\n  s:\n    in:\n      f:'),
                            (4, 'steps:\n  s:\n    in:\n      f: !ii')]:
            reported = parse(f'{above} {text}\n', 's.wic').diagnostics
            assert [(d.code, d.message, d.span and d.span.start_line) for d in reported] == [
                (SophiosErrorCode.UNKNOWN_TAG, f'YAML cannot read this value as {tag}', line)], above
        return
    top, inline = (parse(f'{above} {text}\n', 'x') for above in ('top:\n  k:', 'steps:\n  s:\n    in:\n      f: !ii'))
    assert top.ok and inline.ok and top.document is not None and inline.document is not None, text
    read, held = yaml.safe_load(text), dict(top.document.passthrough)['top']
    for value in (held['k'] if isinstance(held, dict) else held, _payload(inline.document.steps[0].inputs[0][1])):
        assert (repr(value), type(value)) == (repr(read), type(read)), text


_MERGE_TARGETS: Final = {
    'passthrough': ('a:', 2, '{n}'), 'cwl': ('a: !cwl', 2, '{n}'), 'steps': ('steps:', 2, '{{in: {{x: !ii {n}}}}}'),
    'literal': ('steps:\n  s:\n    in:\n      f: !ii', 8, '{n}'), 'in': ('steps:\n  s:\n    in:', 6, '!ii {n}'),
    'record': ('steps:\n  s:\n    in:\n      f: !cwl', 8, '{n}')}


@st.composite
def merged_documents(draw: st.DrawFn) -> tuple[str, str]:
    """1-3 anchored base mappings, each maybe merging earlier ones, then a mapping in one of `_MERGE_TARGETS` merging
    1-3 of them among keys of its own, flow or block. In a record every base has a default: a record giving no value
    is wic038 whatever the merge."""
    position = draw(st.sampled_from(sorted(_MERGE_TARGETS)))
    above, indent, spelled = _MERGE_TARGETS[position]
    keys = st.lists(st.sampled_from(['default', 'label', 'loadContents'] if position == 'record' else ['p', 'q', 'r']))

    def entries(bases: int, own: list[str]) -> list[str]:
        written = [f'{key}: {spelled.format(n=draw(st.integers(0, 9)))}' for key in dict.fromkeys(own)]
        if bases:
            merged = [f'*b{i}' for i in draw(st.permutations(range(bases)))[:draw(st.integers(1, 3))]]
            merge = merged[0] if len(merged) == 1 and draw(st.booleans()) else f'[{", ".join(merged)}]'
            written.insert(draw(st.integers(0, len(written))), f'<<: {merge}')
        return written

    bases = [entries(i * draw(st.integers(0, 1)), ['default'] * (position == 'record') + draw(keys))
             for i in range(draw(st.integers(1, 3)))]
    target = entries(len(bases), draw(keys))
    body = f' {{{", ".join(target)}}}' if draw(st.booleans()) else ''.join(f'\n{" " * indent}{e}' for e in target)
    return ''.join(f'b{i}: &b{i} {{{", ".join(b)}}}\n' for i, b in enumerate(bases)) + f'{above}{body}\n', position


@pytest.mark.fast
@given(merged_documents())
@example(('x: &m {p: 1}\na: {<<: *m, q: 2}\n', 'passthrough'))
@example(('x: &m {p: 1}\na:\n  <<: *m\n  q: 2\n', 'passthrough'))
@example(('x: &m {p: 1}\na: {<<: [*m], q: 2}\n', 'passthrough'))
@example(('x: &m {p: 1}\na: !cwl {<<: *m, q: 2}\n', 'cwl'))
@example(('x: &m {p: 1}\na: {<<: *m, p: 2}\n', 'passthrough'))
@example(('x: &m {p: 1}\ny: &n {p: 3, r: 4}\na: {<<: [*m, *n]}\n', 'passthrough'))
@example(('x: &m {p: 1, r: 2}\ny: &n {<<: *m, r: 3}\na: {<<: *n, s: 4}\n', 'passthrough'))
@example(('d: &d {p: 1, r: 2}\na: {<<: *d, z: 0, p: 3}\n', 'passthrough'))
@example(('d: &d {p: 1}\ne: &e {r: 2, p: 4}\na: {z: 0, <<: [*d, *e], r: 5}\n', 'passthrough'))
@example(('d: &d {default: 1}\nsteps:\n  s:\n    in:\n      f: !cwl {<<: *d}\n', 'record'))
@example(('d: &d {p: 1}\nsteps:\n  s:\n    in:\n      f: !ii {<<: *d, q: 2}\n', 'literal'))
@example(('d: &d {p: 1}\nsteps:\n  s:\n    in: {<<: *d, g: !ii 2}\n', 'in'))
@example(('d: &d {p: 1}\nsteps:\n  s:\n    in: {<<: *d, p: !ii 3}\n', 'in'))
@example(('d: &d {p: {in: {x: !ii 1}}, r: {in: {x: !ii 2}}}\n'
          'steps: {<<: *d, a: {in: {x: !ii 0}}, p: {in: {x: !ii 3}}}\n', 'steps'))
@FAST
def test_a_merge_builds_what_yaml_builds(merged: tuple[str, str]) -> None:
    """A merge key is valid YAML, applied wherever the parser reads a mapping: no wic009, and what YAML builds, keys
    in YAML's order and no `<<` key: a mapping's entries, a record's fields, the inputs of `in:` (a `!ii` value as
    YAML reads its text), the step ids of `steps:`. A `!cwl` at the top is no record: only YAML's verdict counts."""
    source, position = merged
    result, loaded = parse(source, 'm.wic'), yaml.load(source, Loader=YamlWithSophiosTagsOpaque)
    assert SophiosErrorCode.UNKNOWN_TAG not in [d.code for d in result.diagnostics], source
    if position == 'cwl':
        return
    assert result.ok and result.document is not None, [str(d) for d in result.diagnostics]
    if position == 'passthrough':
        built, read = cast(dict, dict(result.document.passthrough)['a']), loaded['a']
    elif position == 'steps':
        built, read = dict.fromkeys(step.id for step in result.document.steps), dict.fromkeys(loaded['steps'])
    elif position == 'in':
        inputs, written = result.document.steps[0].inputs, loaded['steps']['s']['in']
        built, read = dict.fromkeys(name for name, _ in inputs), dict.fromkeys(written)
        assert all(v.value == yaml.safe_load(written[n]) for n, v in inputs if isinstance(v, InlineLiteral))
    else:
        node = result.document.steps[0].inputs[0][1]
        built = dict(node.fields) if isinstance(node, CwlRecord) else _payload(node)
        read = loaded['steps']['s']['in']['f']
    assert list(built.items()) == list(read.items()), source


def _fires(code: SophiosErrorCode) -> bool:
    """Whether the provocation registered for `code` raises a SophiosError carrying it."""
    try:
        provocations.COMPILED[code]()
    except SophiosError as raised:
        return any(found.code is code for found in raised.diagnostics)
    return False


@pytest.mark.skip_pypi_ci
def test_every_code_is_provoked() -> None:
    """Every SophiosErrorCode is provoked, as a code nothing provokes is dead on arrival: a document under
    tests/core/data/parse reports it, or else the provocation tests/core/provocations.py registers for it raises it.
    So a new code lands with its provocation in the same commit."""
    parsed = {SophiosErrorCode[report[0]] for path in sorted((DATA / 'parse').glob('*/expected.yaml'))
              for reports in yaml.safe_load(path.read_text(encoding='utf-8'))['documents'].values()
              for report in reports}
    assert not parsed & set(provocations.COMPILED), sorted(map(str, parsed & set(provocations.COMPILED)))
    unprovoked = [str(code) for code in SophiosErrorCode
                  if code not in parsed and (code not in provocations.COMPILED or not _fires(code))]
    assert not unprovoked, f'codes nothing provokes: {unprovoked}'


@pytest.mark.cwl_corpus
@pytest.mark.fast
def test_the_corpus_parses_validates_and_round_trips() -> None:
    """Every `.wic` the compiler's own discovery reaches parses without a diagnostic, its JSON projection is a document
    the exported schema accepts, and it reads the same once rendered: "the language accepts what exists today" is a
    falsifiable claim. Discovery reaching nothing is a failure, not a small corpus: discovery itself broke."""
    corpus = [path for paths in workflow_paths().values() for path in paths.values()]
    assert corpus, 'get_yml_paths found no workflows; check search_paths_wic'
    failed = []
    for path in corpus:
        first = parse(path.read_text(encoding='utf-8'), str(path))
        second = parse(render(first.document), str(path)) if first.document is not None else first
        if not (first.ok and second.ok and SCHEMA_VALIDATOR.is_valid(to_json(cast(Document, first.document)))
                and reading(second.document) == reading(first.document)):
            failed.append((str(path), [str(d) for d in (*first.diagnostics, *second.diagnostics)]))
    assert not failed, failed


@pytest.mark.fast
def test_reading_writing_and_the_schema_share_no_state() -> None:
    """Parsing, rendering and the schema export keep nothing between calls. A parse leaves `yaml.safe_load` blind to
    `!ii`, which registering on `yaml.SafeLoader` would teach every later load in the process, in any library. One
    source parsed and rendered from many threads at once gives every thread the same answer. And editing an exported
    schema three levels down, where a shallow copy once shared its interior, leaves the next export as it was."""
    source = (DATA / 'parse' / 'records' / 'inputs.wic').read_text(encoding='utf-8')
    assert parse(source, 'tags.wic').ok
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(source)

    def once(_: int) -> tuple[str, int]:
        document = parse(source, 'concurrent.wic').document
        assert document is not None
        return render(document), len(wic_schema()['$defs'])

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(set(pool.map(once, range(64)))) == 1, 'concurrent parses disagreed'

    edited, pristine = wic_schema(), wic_schema()
    edited['$defs']['stepBody']['properties']['in']['additionalProperties']['$ref'] = 'CORRUPTED'
    edited['$defs']['outputEntry']['oneOf'][0]['type'] = 'CORRUPTED'
    assert wic_schema() == pristine
