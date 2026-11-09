"""Properties of the Sophios syntax layer, the unit tests at the end only sanity-checking wiring: every AST node has a
span, parsing never raises, InputValue has five forms, diagnostic spans index real text, the corpus parses."""
import re
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, NamedTuple, cast, get_args

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
    Grammar,
    InlineLiteral,
    InputValue,
    RawCwlRef,
    Step,
    UnresolvedName,
    WicSidecar,
    parse,
    render,
    to_json,
)
from sophios.lang.spans import SourceSpan
from sophios.utils_yaml import Key

from . import provocations
from .budgets import budget
from .cases import cases, diagnostic as as_written, match
from .wic_corpus import CORPUS, corpus_id
from .strategies import WIC_VALUES, documents, identifiers, input_lines, pinned, scalar_spellings
from .wic_reading import YamlWithSophiosTagsOpaque

FAST = budget(200)


def _spans(node: Any) -> list[SourceSpan]:
    """Collect every span reachable from an AST node."""
    found: list[SourceSpan] = []
    match node:
        case Document():
            found += _opt(node.span)
            for step in node.steps:
                found += _spans(step)
            if node.sidecar is not None:
                found += _spans(node.sidecar)
        case Step():
            found += _opt(node.span)
            for _, value in node.inputs:
                found += _spans(value)
            for binding in node.outputs:
                found.append(_parsed(binding.span))
                if binding.edge_def is not None:
                    found.append(_parsed(binding.edge_def.span))
        case WicSidecar():
            found += _opt(node.span)
            for _, child in node.steps:
                found += _spans(child)
        case InlineLiteral() | EdgeDef() | EdgeRef() | RawCwlRef() | UnresolvedName():
            found.append(_parsed(node.span))
    return found


def _opt(span: SourceSpan | None) -> list[SourceSpan]:
    return [span] if span is not None else []


def _parsed(span: SourceSpan | None) -> SourceSpan:
    """A value node's span, which may be absent only on a document built in memory."""
    assert span is not None, 'a parsed value node has no span'
    return span


def _resolvable(span: SourceSpan, text: str) -> bool:
    """Whether a span points at a position that exists in the source."""
    lines = text.splitlines() or ['']
    if not 1 <= span.start_line <= len(lines):
        return False
    return bool(1 <= span.start_column <= len(lines[span.start_line - 1]) + 1)


@pytest.mark.fast
@given(st.text(max_size=400))
@FAST
def test_parsing_is_total_for_arbitrary_text(text: str) -> None:
    """Parsing never raises, whatever it is handed."""
    result = parse(text, 'fuzz.wic')
    assert result.document is not None or len(result.diagnostics) > 0


@pytest.mark.fast
@given(documents())
@FAST
def test_well_formed_documents_parse(source: str) -> None:
    """A syntactically valid document yields a document and no errors."""
    result = parse(source, 'gen.wic')
    assert result.ok, [str(d) for d in result.diagnostics]


@pytest.mark.fast
@given(documents())
@FAST
def test_every_node_carries_a_resolvable_span(source: str) -> None:
    """Every span in the tree points at real source."""
    result = parse(source, 'gen.wic')
    assert result.document is not None
    spans = _spans(result.document)
    assert spans, 'no spans collected; the walk is not reaching the tree'
    for span in spans:
        assert _resolvable(span, source), f'unresolvable span {span}'


@pytest.mark.fast
@given(documents())
@FAST
def test_input_values_are_closed(source: str) -> None:
    """Every input value is one of the five declared forms."""
    permitted = get_args(InputValue)
    result = parse(source, 'gen.wic')
    assert result.document is not None
    for step in result.document.steps:
        for name, value in step.inputs:
            assert isinstance(value, permitted), f'{name} produced {type(value).__name__}'


@pytest.mark.fast
@given(st.text(max_size=400))
@FAST
def test_diagnostic_spans_index_real_source(text: str) -> None:
    """A parse diagnostic always has a span, inside the document.

    The `Diagnostic` type allows a spanless entry because compile-phase
    failures may not know a position — but the *parser* always does, and this
    is where that stronger guarantee is enforced.
    """
    for diagnostic in parse(text, 'fuzz.wic').diagnostics:
        assert diagnostic.span is not None, f'parse diagnostic without a span: {diagnostic}'
        assert diagnostic.span.file == 'fuzz.wic'
        assert diagnostic.span.start_line >= 1
        assert diagnostic.span.start_column >= 1
        assert diagnostic.span.start_line <= max(len(text.splitlines()), 1) + 1


@pytest.mark.fast
@pytest.mark.parametrize('path', CORPUS, ids=corpus_id)
def test_corpus_files_parse(path: Path) -> None:
    """Every `.wic` in the repository corpus parses without error.

    This is what makes "the specification accepts what exists today"
    falsifiable rather than an aspiration.
    """
    source = path.read_text(encoding='utf-8')
    result = parse(source, str(path))
    assert result.ok, [str(d) for d in result.diagnostics]


@pytest.mark.fast
def test_corpus_is_not_empty() -> None:
    """Discovery must find workflows, or the corpus property passes vacuously.

    The corpus is whatever `search_paths_wic` reaches — the same discovery the
    compiler uses, so there is nothing separate to keep complete. An empty
    result does not mean a small corpus; it means discovery itself broke, and
    that is a failure, not a skip. CI provisions the config that reaches the
    sibling repositories, and CI is the arbiter of full coverage.
    """
    assert CORPUS, 'get_yml_paths found no workflows; check search_paths_wic'


@pytest.mark.fast
@given(
    st.sampled_from([('!ii', 'wic_inline_input', InlineLiteral),
                     ('!*', 'wic_alias', EdgeRef),
                     ('!cwl', 'wic_raw_cwl', RawCwlRef)]),
    identifiers,
)
@FAST
def test_surface_forms_are_equivalent(form: tuple[str, str, type], payload: str) -> None:
    """Tagged and desugared spellings of a construct parse to the same node.

    Humans write `!ii x`; the Python API emits `{wic_inline_input: x}`, because
    a constructor that re-emitted its own tag would fire again on reload. Both
    are the same language, so both must produce the same AST — otherwise the
    two surfaces have quietly diverged.
    """
    tag, key, expected = form
    tagged = parse(f'steps:\n- id: s\n  in:\n    f: {tag} {payload}\n', 'a.wic')
    sugared = parse(f'steps:\n- id: s\n  in:\n    f:\n      {key}: {payload}\n', 'b.wic')
    assert tagged.ok and sugared.ok
    assert tagged.document is not None and sugared.document is not None

    left = tagged.document.steps[0].inputs[0][1]
    right = sugared.document.steps[0].inputs[0][1]
    assert isinstance(left, expected) and isinstance(right, expected)
    assert _payload(left) == _payload(right)


@pytest.mark.fast
@given(identifiers)
@FAST
def test_the_two_spellings_of_an_edge_definition_agree_on_an_output(payload: str) -> None:
    """`!&` has the same two spellings as its siblings, in the one position it
    is legal (.wic reference §3.6). Tested separately rather than as a fourth row above,
    because that table quantifies over *input* position and an anchor there is
    now `wic019` — the case does not disappear, it moves to where it belongs.
    """
    tagged = parse(f'steps:\n- id: s\n  out:\n  - f: !& {payload}\n', 'a.wic')
    sugared = parse(f'steps:\n- id: s\n  out:\n  - f:\n      wic_anchor: {payload}\n', 'b.wic')
    assert tagged.ok and sugared.ok
    assert tagged.document is not None and sugared.document is not None

    left = tagged.document.steps[0].outputs[0].edge_def
    right = sugared.document.steps[0].outputs[0].edge_def
    assert isinstance(left, EdgeDef) and isinstance(right, EdgeDef)
    assert left.name == right.name == payload


def _payload(value: InputValue) -> Any:
    """The carried value of an input node, whatever its form."""
    match value:
        case InlineLiteral():
            return value.value
        case RawCwlRef():
            return value.expression
        case EdgeDef() | EdgeRef() | UnresolvedName():
            return value.name


# Every row is the same three steps — parse, check the verdict, inspect one
# thing — so only the source and the claim are worth reading. Each row is
# still a named case in the test report.


class Accepted(NamedTuple):
    """Source the language admits, and what the document must then hold."""

    claim: str
    source: str
    holds: Callable[[Document], bool]


def _first_keys(sidecar: WicSidecar | None) -> list[tuple[int | None, str]]:
    """The (index, name) of each step key, following the first `wic: steps:` entry down while there is one."""
    keys = []
    while sidecar is not None and sidecar.steps:
        key, sidecar = sidecar.steps[0]
        keys.append((key.index, key.name))
    return keys


def _merged_record(document: Document) -> bool:
    """Whether step 0's input f is a CwlRecord: typed sources, verbatim fields."""
    record = document.steps[0].input('f')
    return (isinstance(record, CwlRecord)
            and [(type(source).__name__, source.name) for source in record.sources]
            == [('EdgeRef', 'a'), ('UnresolvedName', 'wf_in')]
            and dict(record.fields) == {'linkMerge': 'merge_flattened'})


def _defines_e(document: Document) -> bool:
    edge = document.steps[0].outputs[0].edge_def
    return edge is not None and edge.name == 'e'


def _is_kv(value: Any) -> bool:
    """`!ii {k: v}` is one InlineLiteral around plain data: the passthrough walk and the input's caller both wrap
    tagged collections, and uncoordinated the same node was wrapped twice."""
    return isinstance(value, InlineLiteral) and value.value == {'k': 'v'}


ACCEPTED: Final[tuple[Accepted, ...]] = (
    Accepted('a bare name is an unresolved name',
             'steps:\n  s:\n    in:\n      e: plain\n',
             lambda d: isinstance(d.steps[0].input('e'), UnresolvedName)),
    Accepted('an untagged mapping input is an inline literal (.wic reference §3.2)',
             'steps:\n- id: s\n  in:\n    a: {some: mapping}\n',
             lambda d: isinstance(d.steps[0].inputs[0][1], InlineLiteral)
             and d.steps[0].inputs[0][1].value == {'some': 'mapping'}),
    Accepted('sidecar step keys are structured, not strings',
             'wic:\n  steps:\n    (1, alpha):\n      wic:\n        graphviz: {}\n',
             lambda d: d.sidecar is not None
             and (d.sidecar.steps[0][0].index, d.sidecar.steps[0][0].name) == (1, 'alpha')),
    Accepted('a bare wic: is an empty sidecar, not an error',
             'wic:\nsteps:\n  s:\n    in:\n      a: !ii 1\n',
             lambda d: d.sidecar is not None),
    Accepted('a step with no body at all is well-formed',
             'steps:\n  some_step.wic:\n',
             lambda d: d.steps[0].id == 'some_step.wic' and d.steps[0].inputs == ()),
    Accepted('uninterpreted step keys survive too',
             'steps:\n  s:\n    label: a human label\n',
             lambda d: dict(d.steps[0].passthrough)['label'] == 'a human label'),
    Accepted('a scalar YAML cannot load keeps its literal text',
             'steps:\n  s:\n    in:\n      a: !ii "{unclosed"\n',
             lambda d: isinstance(d.steps[0].inputs[0][1], InlineLiteral)
             and d.steps[0].inputs[0][1].value == '{unclosed'),
    Accepted('uninterpreted top-level keys survive',
             '$namespaces:\n  edam: http://example\nsteps:\n  s: {}\n',
             lambda d: dict(d.passthrough)['$namespaces'] == {'edam': 'http://example'}),
    # `(index, name)` keys are StepKeys at every depth, not opaque strings: the child sidecar arrives wrapped in a
    # `wic:` key, and the parser descends through it, or StepKey's "nothing downstream parses that string again"
    # is false past the first depth.
    Accepted('sidecar nesting is normalised at every depth',
             'wic:\n  steps:\n    (1, outer):\n      wic:\n        steps:\n'
             '          (1, inner):\n            wic:\n              steps:\n'
             '                (1, innermost):\n                  namespace: global\n',
             lambda d: _first_keys(d.sidecar) == [(1, 'outer'), (1, 'inner'), (1, 'innermost')]),
    # A merged parent contribution lands beside a step's own `wic:` metadata once ast.py::merge_yml_trees combines
    # a distant ancestor's unwrapped `out:` with this step's wrapped sidecar (`cg.wic`'s `mdrun`, through
    # `basic.wic`). Both survive: the metadata is not stranded opaque under `wic:`, and `!&` under `out:` is accepted.
    Accepted('a sidecar steps out sibling to wic: still unwraps',
             'wic:\n  steps:\n    (1, s):\n      wic:\n        namespace: gpu\n'
             '      out:\n      - value: !& e\nsteps:\n- id: s\n',
             lambda d: d.sidecar is not None
             and (entries := dict(d.sidecar.steps[0][1].entries))['namespace'] == 'gpu'
             and entries['out'] == [{'value': {Key.ANCHOR: 'e'}}]),
    # The siblings alone, rather than a wic033 naming the wrapper as an unknown key; the schema reads it the same way.
    Accepted('an empty sidecar wrapper beside siblings is an empty mapping',
             'wic:\n  steps:\n    (1, a):\n      wic:\n      in: {x: 1}\nsteps:\n- id: a\n',
             lambda d: d.sidecar is not None and dict(d.sidecar.steps[0][1].entries) == {'in': {'x': 1}}),
    # Both edge spellings are honoured, in each of three layouts; a non-!& value is the case in edges.yaml.
    Accepted('an out value tagged !& defines an edge', 'steps:\n- id: s\n  out:\n  - file: !& e\n', _defines_e),
    Accepted('an out value desugared to wic_anchor defines an edge',
             'steps:\n- id: s\n  out:\n  - file: {wic_anchor: e}\n', _defines_e),
    Accepted('an out value with a block wic_anchor defines an edge',
             'steps:\n- id: s\n  out:\n  - f:\n      wic_anchor: e\n', _defines_e),
    Accepted('an inline literal collection in input is wrapped exactly once',
             'steps:\n- id: s\n  in:\n    a: !ii {k: v}\n', lambda d: _is_kv(d.steps[0].inputs[0][1])),
    Accepted('an inline literal collection in passthrough is wrapped exactly once',
             'top: !ii {k: v}\n', lambda d: _is_kv(dict(d.passthrough)['top'])),
    Accepted('only a mapping made of record fields alone is read as one; any other key keeps it a literal',
             'steps:\n  mk_file:\n    in:\n      name: {source: x, pdb_code: 1aki}\n',
             lambda d: isinstance(d.steps[0].input('name'), InlineLiteral)),
    Accepted('a bare step id is a sidecar step key with no index',
             'wic:\n  steps:\n    echo:\n      wic:\n        graphviz:\n          label: x\nsteps:\n  echo:\n',
             lambda d: d.sidecar is not None and _first_keys(d.sidecar) == [(None, 'echo')]
             and str(d.sidecar.steps[0][0]) == 'echo'),
    Accepted('!cwl builds a record: typed sources, verbatim fields',
             'steps:\n  s:\n    in:\n      f: !cwl {source: [!* a, wf_in], linkMerge: merge_flattened}\n',
             _merged_record),
    Accepted('wic_raw_cwl builds the same record',
             'steps:\n  s:\n    in:\n      f:\n        wic_raw_cwl:\n'
             '          source: [{wic_alias: a}, wf_in]\n          linkMerge: merge_flattened\n', _merged_record),
)


PARSE_KEYS: Final = ('source', 'report', 'excludes', 'ok', 'recovers', 'passthrough', 'step_passthrough', 'yaml')


@pytest.mark.fast
@pytest.mark.parametrize('case', cases('parse', *PARSE_KEYS))
def test_parse_case(case: dict[str, Any]) -> None:
    """`source` parses as the case says, each expectation optional: `report`, its diagnostics in reading order, each
    as `--diagnostics json` writes it with the code by name; `excludes`, codes it never reports; `ok`; `recovers`, a
    document returned; `passthrough` and `step_passthrough`, keys of the document's or its first step's passthrough
    and exactly what each holds; `yaml`, whether YAML with the Sophios tags opaque `loads` the text or `raises`.

    Whatever the case, every diagnostic has a span and they come back in reading order: tags are reported in one walk
    and everything else in the structural pass, so pass order is not reading order.
    """
    result = parse(case['source'], 'case.wic')
    found = [as_written(d) for d in result.diagnostics]
    assert all(d['line'] is not None for d in found), found
    assert [(d['line'], d['column']) for d in found] == sorted((d['line'], d['column']) for d in found), found
    assert match(case.get('report', found), found), found
    assert not {d['code'] for d in found} & set(case.get('excludes', ())), found
    recovered = result.document is not None
    assert result.ok is case.get('ok', result.ok) and recovered is case.get('recovers', recovered)
    for key, held in case.get('passthrough', {}).items():
        assert result.document is not None and dict(result.document.passthrough)[key] == held
    for key, held in case.get('step_passthrough', {}).items():
        assert result.document is not None and dict(result.document.steps[0].passthrough)[key] == held
    if 'yaml' in case:
        with pytest.raises(yaml.YAMLError) if case['yaml'] == 'raises' else nullcontext():
            yaml.load(case['source'], Loader=YamlWithSophiosTagsOpaque)


@pytest.mark.fast
@pytest.mark.parametrize('case', ACCEPTED, ids=[case.claim for case in ACCEPTED])
def test_accepted_shapes(case: Accepted) -> None:
    """Each admitted shape parses, and the AST carries what it should."""
    result = parse(case.source, 'accepted.wic')
    assert result.ok, [str(d) for d in result.diagnostics]
    assert result.document is not None
    assert case.holds(result.document), case.claim


@pytest.mark.fast
@given(documents())
@example('steps:\n  touch:\n    in:\n      f: !ii x\n')
@example('steps:\n- id: touch\n  in:\n    f: !ii x\n')
@budget(50)
def test_sequence_and_mapping_steps_agree(source: str) -> None:
    """Both `steps:` surface forms produce the same AST shape: a document written out in the other form reads back
    in that form, and is otherwise the same document. So the writer keeps the form it is given, either way."""
    document = parse(source, 'a.wic').document
    assert document is not None
    other = replace(document, steps_as_mapping=not document.steps_as_mapping)
    result = parse(render(other), 'b.wic')
    assert result.ok and result.document is not None, [str(d) for d in result.diagnostics]
    assert result.document.steps_as_mapping is other.steps_as_mapping
    assert to_json(replace(result.document, steps_as_mapping=document.steps_as_mapping)) == to_json(document)


#: A step's own keys but `id`, written out rather than read off `Grammar.STEP_KEYS`, which they are held to.
_STEP_KEYS: Final = ('in', 'out', 'run', 'scatter', 'scatterMethod', 'when')
_ENTRY_BODIES = st.one_of(
    st.sampled_from(['', ' 3', ' [a, b]', ' {a: 1}', ' {}', ' !bogus x', ' !ii 1', ' {id: !ii 1}', ' !& e',
                     ' {wic_anchor: e}']),
    st.lists(st.one_of(st.just('id: t'), st.just('out: [f]'), st.just('label: x'),
                       input_lines(indent='      ').map(lambda line: 'in:\n' + line)),
             min_size=1, max_size=3, unique=True).map(lambda keys: ''.join(f'\n    {key}' for key in keys)))


def _placed_alike(body: str) -> bool:
    """Whether both spellings place a body's diagnostics alike: not so for an unknown tag on a whole non-mapping
    body, whose wic003 the mapping form reports at the key and the sequence form at the value, whatever the key."""
    return not body.startswith(' !bogus')


@pytest.mark.fast
@given(st.one_of(identifiers, st.sampled_from(_STEP_KEYS)).filter(lambda key: key != 'id'),
       _ENTRY_BODIES.filter(_placed_alike))
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
    holding it is the fix; any other `K` names the step, as `K: V` in the mapping form does. Walking the key's value
    instead would lose the body's own shape errors, and report an input named `id` as a second identity."""
    fix = f'steps:\n- id: s\n  {key}:{body}\n' if key in _STEP_KEYS else f'steps:\n  {key}:{body}\n'
    rejected = [d.code for d in parse(f'steps:\n- {key}:{body}\n', 'rejected.wic').diagnostics]
    assert rejected == [SophiosErrorCode.MISSING_STEP_ID, *(d.code for d in parse(fix, 'fix.wic').diagnostics)]


@pytest.mark.fast
@pytest.mark.parametrize('spelling', sorted(Key.ALL))
def test_the_desugared_spellings_are_not_reported_against_themselves(spelling: str) -> None:
    """The rule names a prefix, so the constructs it exists for must pass it."""
    result = parse(f'steps:\n- id: s\n  in:\n    f:\n      {spelling}: e\n', 'spelled.wic')
    assert SophiosErrorCode.RESERVED_KEY not in [d.code for d in result.diagnostics], spelling


@pytest.mark.fast
@pytest.mark.parametrize(('name', 'leads'), [('touch', "write '- id: touch'"), ('in', "add the '- id:' line")])
def test_the_single_key_message_leads_with_the_likelier_reading(name: str, leads: str) -> None:
    """`- in:` is almost certainly a forgotten id:; `- touch:` almost certainly a name."""
    diagnostics = parse(f'steps:\n- {name}:\n    a: !ii 1\n', 'reading.wic').diagnostics
    message = next(d.message for d in diagnostics if d.code is SophiosErrorCode.MISSING_STEP_ID)
    assert message.startswith(f'a step in a sequence carries its name in an id: key — {leads}')
    assert f"write '- id: {name}'" in message and "add the '- id:' line" in message


#: Keys offered to a step body: every one `Grammar.STEP_KEYS` claims, plus
#: three it must not claim. Written out rather than read off `STEP_KEYS`,
#: because a probe derived from the set under test moves with it and a member
#: could then be dropped from both sides unnoticed.
_PROBE_KEYS: Final = ('id', 'in', 'out', 'run', 'scatter', 'scatterMethod', 'when', 'doc', 'label', 'touch')


@pytest.mark.fast
def test_step_keys_is_exactly_the_set_step_body_acts_on() -> None:
    """`Grammar.STEP_KEYS` is exactly the set `_step_body` acts on.

    The two sides must agree for wic006's reading to be right: a key that falls
    through to passthrough is a step name, not a step key. `_step_body` names
    `id`, `in` and `out` itself, so those three are spelled in two places and
    this is what stops them drifting.
    """
    body = ''.join(f'  {key}: {{}}\n' for key in _PROBE_KEYS if key != 'id')
    document = parse(f'steps:\n- id: s\n{body}', 'keys.wic').document
    assert document is not None
    passed_through = {key for key, _ in document.steps[0].passthrough}
    assert passed_through == {'doc', 'label', 'touch'}
    assert set(_PROBE_KEYS) - passed_through == set(Grammar.STEP_KEYS)


@pytest.mark.fast
def test_python_api_emits_documents_this_parser_accepts(tmp_path: Path) -> None:
    """The Python API is the second surface of the same language.

    Whatever it emits must parse, or the two surfaces have diverged and the
    language spec is describing something that does not exist. See
    docs/dev/language_spec.md, section 3.
    """
    # Imported here: the API pulls in the whole compiler, which the syntax
    # layer deliberately does not depend on.
    # pylint: disable=import-outside-toplevel
    from sophios.api.python.workflow import Workflow

    from .helpers import adapter_step

    touch = adapter_step('touch', filename='empty.txt')
    workflow = Workflow([touch], 'adherence')

    result = parse(workflow.write_wic(tmp_path).read_text(encoding='utf-8'), 'api_emitted.wic')

    assert result.ok, [str(d) for d in result.diagnostics]
    assert result.document is not None
    emitted = result.document.steps[0].input('filename')
    assert isinstance(emitted, InlineLiteral)
    assert emitted.value == 'empty.txt'


def _nested_api_workflow() -> Any:
    """A parent calling one nested workflow whose steps share an edge."""
    # pylint: disable=import-outside-toplevel
    from sophios.api.python.workflow import Workflow

    from .helpers import adapter_step

    touch = adapter_step('touch', filename='empty.txt')
    append = adapter_step('append', file=touch.outputs.file, str='Hello')
    child = Workflow([touch, append], 'adherence_child')
    return Workflow([child], 'adherence_parent')


@pytest.mark.fast
def test_python_api_writes_nested_workflows_this_parser_accepts(tmp_path: Path) -> None:
    """Every `.wic` file of a written nested workflow parses: the parent calls
    the child by name, and the child's edge is an ordinary `!&`/`!*` pair in
    its own document (language spec §3). Each tool is written beside them."""
    root = _nested_api_workflow().write_wic(tmp_path)

    written = sorted(tmp_path.glob('*.wic'))
    assert root in written and len(written) == 2, written
    assert [path.name for path in sorted(tmp_path.glob('*.cwl'))] == ['append.cwl', 'touch.cwl']
    for path in written:
        result = parse(path.read_text(), path.name)
        assert result.ok, (path.name, [str(d) for d in result.diagnostics])

# `yaml.SafeLoader` rejects unknown tags with ConstructorError; a syntax layer
# that silently accepted them would specify a superset of the language. Both
# escapes found so far are pinned, plus the agreement property.


@pytest.mark.fast
@given(st.text('abcdefghijklmnopqrstuvwxyz!&*i {}[]:#-', max_size=120))
@FAST
@example('!: :')      # unknown tag on a mapping *key* — the position that was missed
@example('!foo x: y')  # the same, spelled legibly
@example('!!: :')     # a core tag with an empty suffix, which YAML cannot construct
@example('a: !!str [a]')  # a core tag on the wrong node kind
@example('a: !!int')  # a core number tag over an empty value, which raises IndexError
@example('a: !!int abc')  # a core tag over a value its constructor cannot read
def test_parser_is_not_more_permissive_than_yaml(text: str) -> None:
    """Any document the parser accepts without diagnostics, `yaml.SafeLoader`
    loads with the Sophios tags opaque.

    The reverse is allowed — the parser reports what YAML accepts as an opaque
    tag's text (`!ii 2020-13-45` is wic009) — but this direction is the
    specification's half of the bargain.

    The pinned cases are tags in key position. The property found `!: :` on
    its own, months after the tag rule was written, because the alphabet only
    rarely composes a tagged key — which is exactly why it is pinned now:
    Hypothesis's failure database does not travel to CI, so a find that came
    from one lucky seed would otherwise be re-discovered by luck or not at all.
    """
    result = parse(text, 'agree.wic')
    if result.ok and result.document is not None:
        yaml.load(text, Loader=YamlWithSophiosTagsOpaque)  # must not raise


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
    parser never raises. A failed merge is worded as a merge, an undefined tag is named, and a value YAML cannot read
    says so. What YAML builds is no wic009."""
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


@pytest.mark.fast
def test_the_wic_block_is_closed_at_every_depth() -> None:
    """Each `wic:` key the language has parses clean; any other is `wic033`,
    positioned at the key itself.

    The generated jsonschema used to refuse such a key as the file was read.
    The parser is the gate now, so it is the parser that must refuse it -- at
    the root, and on a `steps:` entry, whose vocabulary is the root's plus
    what it says about the step it names.
    """
    for key in sorted(Grammar.SIDECAR_KEYS - {'steps'}):
        root = parse(f'wic:\n  {key}: {WIC_VALUES[key]}\nsteps:\n- id: s\n', 'root.wic')
        assert not root.diagnostics.has_errors, (key, [str(d) for d in root.diagnostics])
    for key in sorted(Grammar.SIDECAR_STEP_KEYS - {'steps', 'out'}):
        entry = parse(f'wic:\n  steps:\n    (1, s):\n      {key}: {WIC_VALUES[key]}\nsteps:\n- id: s\n',
                      'entry.wic')
        assert not entry.diagnostics.has_errors, (key, [str(d) for d in entry.diagnostics])

    for source, line, column in [
            ('wic:\n  nonsense_key: 1\nsteps:\n- id: s\n', 2, 3),
            ('wic:\n  steps:\n    (1, s):\n      wic:\n        nonsense_key: 1\nsteps:\n- id: s\n', 5, 9),
            ('wic:\n  steps:\n    (1, s):\n      nonsense_key: 1\nsteps:\n- id: s\n', 4, 7),
            # A step-only key is not a root key.
            ('wic:\n  scatter: [x]\nsteps:\n- id: s\n', 2, 3)]:
        reported = [(d.code, d.span.start_line, d.span.start_column)
                    for d in parse(source, 'closed.wic').diagnostics if d.span is not None]
        assert reported == [(SophiosErrorCode.UNKNOWN_WIC_KEY, line, column)], source


@pytest.mark.fast
def test_alias_expansion_is_bounded() -> None:
    """A widening alias chain is cut off by budget, quickly, once."""
    laughs = 'a0: &a0 [x, x, x, x, x, x, x, x, x]\n'
    for i in range(1, 8):
        refs = ', '.join([f'*a{i-1}'] * 9)
        laughs += f'a{i}: &a{i} [{refs}]\n'

    started = time.perf_counter()
    result = parse(laughs, 'laughs.wic')  # must not raise or hang
    assert time.perf_counter() - started < 5.0
    assert not result.ok
    assert any(d.code is SophiosErrorCode.RECURSIVE_ALIAS for d in result.diagnostics)


# `st.text()` almost never forms valid YAML with aliases, so quantifying
# totality over it left the entire anchor/alias class untested: cycles raised
# RecursionError and widening chains exhausted memory while the totality
# property stayed green.
# These strategies generate *structured* YAML — anchors, aliases (including
# self-referential ones), tags known and unknown, deep nesting — so the
# property covers the inputs that actually break parsers.


_tag_pool = st.sampled_from(['', '!ii ', '!& ', '!* ', '!cwl ', '!foo ', '!x '])


@st.composite
def adversarial_yaml(draw: st.DrawFn) -> str:
    """Structured YAML text with anchors, aliases, tags, and real nesting."""
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
                inner = draw(identifiers)
                fragments.append(f'{key}: {tag}{{{inner}: {draw(identifiers)}}}')
            case 'anchor':
                anchors.append(f'a{i}')
                fragments.append(f'{key}: &a{i} [{draw(identifiers)}]')
            case 'alias' if anchors:
                ref = draw(st.sampled_from(anchors))
                repeats = ', '.join([f'*{ref}'] * draw(st.integers(min_value=1, max_value=6)))
                anchors.append(f'a{i}')
                fragments.append(f'{key}: &a{i} [{repeats}]')
            case 'self_ref':
                # A node that contains itself — legal YAML, lethal to naive walks.
                fragments.append(f'{key}: &s{i} [x, *s{i}]')
            case _:
                depth = draw(st.integers(min_value=1, max_value=5))
                nested = draw(identifiers)
                for _ in range(depth):
                    nested = f'{{n: {nested}}}'
                fragments.append(f'{key}: {nested}')
    return '\n'.join(fragments) + '\n'


@pytest.mark.fast
@given(adversarial_yaml())
# Deterministic pins: Hypothesis is randomised and its failure database does
# not travel to CI, so every counterexample this property has caught (or was
# built to catch) is an @example — same coverage, no separate test functions.
@example('top: &a [*a]\n')                                # alias cycle
@example('steps: &a\n- id: s\n  in:\n    x: *a\n')        # cycle via steps
@example('wic: &w\n  steps: *w\n')                        # malformed key via alias, not a cycle
@example('wic: &w\n  steps:\n    (1, a):\n      wic: *w\n')  # cycle via sidecar
@example('_: !& []\n')                                    # name tag on a collection
@example('_: !foo bar\n')                                 # unknown tag, passthrough
@example('a: !cwl b\n')                                   # an owned tag on passthrough
@example('_: !ii {k: v}\n')                               # collection literal, single wrap
@example('- !foo a\n')                                    # reading order, a non-mapping root
@example('wic: !foo {}\nsteps:\n- in: {}\n')                # reading order, a tag above a structural error
@example('steps:\n- in: {}\n- id: s\n  in: !foo {}\n')      # reading order, a tag below one
@FAST
def test_parsing_is_total_for_adversarial_structure(text: str) -> None:
    """The other half of totality: over real YAML structure — aliases, cycles,
    unknown tags, nesting — and not over text alone.

    Never raises; never accepts silently what YAML rejects; never reports the
    same problem twice — several parse paths legitimately visit the same node,
    and duplicate (code, span, message) reports were a measured regression once
    already; and reports in reading order, each at a span, however many passes
    produced them.
    """
    result = parse(text, 'adversarial.wic')  # must not raise
    assert result.document is not None or len(result.diagnostics) > 0

    reports = [(d.severity, d.code, d.message, d.span) for d in result.diagnostics]
    assert len(reports) == len(set(reports)), f'duplicate diagnostics: {reports}'
    positions = [(d.span.start_line, d.span.start_column) for d in result.diagnostics if d.span]
    assert len(positions) == len(reports) and positions == sorted(positions), reports

    if result.ok and result.document is not None:
        yaml.load(text, Loader=YamlWithSophiosTagsOpaque)  # agreement holds here too


@pytest.mark.fast
@given(_tag_pool, st.sampled_from(['scalar', 'flow_map']), st.data())
@FAST
def test_tag_decisions_agree_across_positions(tag: str, shape: str, data: st.DataObject) -> None:
    """Input position and passthrough position make the same unknown-tag call.

    `!foo {wic_anchor: x}` was reported in passthrough but
    swallowed in input position — two paths, one language, two answers. This
    pins the agreement for every tag and payload shape the generator makes.
    """
    # The mapping key is drawn from the construct keys as well as arbitrary
    # identifiers. `identifiers` caps at eight characters and every desugared
    # key is longer, so without this union the generator could not produce
    # `!foo {wic_anchor: x}` — the one payload where the two positions once
    # disagreed — and the property was green over a space missing its own
    # motivating case.
    key = data.draw(st.one_of(identifiers, st.sampled_from(sorted(Key.ALL))))
    payload = data.draw(identifiers) if shape == 'scalar' else f'{{{key}: x}}'
    value = f'{tag}{payload}'

    in_position = parse(f'steps:\n- id: s\n  in:\n    a: {value}\n', 'pos.wic')
    passthrough = parse(f'top: {value}\n', 'pos.wic')

    # Only the unknown-tag verdict, which is what this property is about.
    # Subtracting the codes that legitimately differ by position instead --
    # `wic024` is one, since a single-key `wic_` mapping is a construct attempt
    # where a construct may appear and ordinary CWL where one may not -- makes
    # every future position-specific diagnostic another exemption here.
    unknown_in = any(d.code is SophiosErrorCode.UNKNOWN_TAG for d in in_position.diagnostics)
    unknown_through = any(d.code is SophiosErrorCode.UNKNOWN_TAG for d in passthrough.diagnostics)
    assert unknown_in == unknown_through, (
        f'{value!r}: input reported unknown-tag={unknown_in}, '
        f'passthrough={unknown_through}')


@pytest.mark.fast
def test_every_code_has_a_registered_provocation() -> None:
    """Each declared SophiosErrorCode appears in exactly one tier of the registry.

    Adding a SophiosErrorCode without its attack fails here, in the same commit."""
    registered = set(provocations.PARSE) | set(provocations.COMPILED)
    missing = set(SophiosErrorCode) - registered
    doubled = set(provocations.PARSE) & set(provocations.COMPILED)
    assert not missing, f'codes with no registered provocation: {sorted(map(str, missing))}'
    assert not doubled, f'codes registered in both tiers: {sorted(map(str, doubled))}'


@pytest.mark.fast
@pytest.mark.parametrize('code', sorted(provocations.PARSE), ids=lambda c: c.name)
def test_parse_provocations_fire(code: SophiosErrorCode) -> None:
    """Every parse-tier provocation actually fires its code."""
    result = parse(provocations.PARSE[code], 'provoke.wic')
    assert any(d.code is code for d in result.diagnostics), f'{code.name} did not fire'


@pytest.mark.skip_pypi_ci
@pytest.mark.parametrize('code', sorted(provocations.COMPILED), ids=lambda c: c.name)
def test_compiled_provocations_fire(code: SophiosErrorCode) -> None:
    """Every compiled-tier provocation raises SophiosError carrying its code.

    `SophiosError` is looked up dynamically: it arrives with the diagnostics
    branch, and on branches before it the COMPILED tier is empty so this test
    has no instances — the lookup keeps the file importable stack-wide.
    """
    from sophios.lang import diagnostics as diagnostics_module
    sophios_error: type[Exception] = getattr(diagnostics_module, 'SophiosError', AssertionError)
    with pytest.raises(sophios_error) as caught:
        provocations.COMPILED[code]()
    fired = getattr(caught.value, 'diagnostics', ())
    assert any(d.code is code for d in fired), f'{code.name} did not fire'


@pytest.mark.fast
@pytest.mark.parametrize('source, expected', [
    ('steps:\n- id: s\n  in:\n    f: !& e\n', 'e'),
    ('steps:\n- id: s\n  in:\n    f: {wic_anchor: e}\n', 'e'),
    ('top: !& x\n', 'x'),
    ('steps:\n- id: s\n  in:\n    f: !& [a]\n', ''),
], ids=['tagged', 'desugared', 'passthrough', 'collection has no name'])
def test_a_misplaced_edge_definition_keeps_the_name_it_was_given(
        source: str, expected: str) -> None:
    """Recovery preserves the name, in both spellings and every position.

    `_output_binding` states the rule this follows: the AST preserves what it
    was given, and a silent drop is the one thing a total parser must never
    do. Reporting the construct is not a licence to discard the name it
    carried — a caller rendering the recovered AST would otherwise emit `''`
    where the source said `e`. A collection genuinely has no name to keep, so
    the empty string is honest there rather than invented.
    """
    result = parse(source, 'keep.wic')
    assert result.document is not None
    step = result.document.steps[0] if result.document.steps else None
    value = step.inputs[0][1] if step else result.document.passthrough[0][1]
    assert isinstance(value, UnresolvedName)
    assert value.name == expected


@pytest.mark.fast
@pytest.mark.parametrize('payload, expected', [
    ('{wic_anchor: x}', {SophiosErrorCode.UNKNOWN_TAG, SophiosErrorCode.MISPLACED_EDGE_DEF}),
    ('{wic_alias: x}', {SophiosErrorCode.UNKNOWN_TAG}),
    ('bar', {SophiosErrorCode.UNKNOWN_TAG}),
], ids=['anchor payload', 'alias payload', 'scalar payload'])
def test_an_unknown_tag_and_a_misplaced_anchor_are_both_reported_in_both_positions(
        payload: str, expected: set) -> None:
    """A node can be two things wrong at once, and both positions must say so.

    `!foo {wic_anchor: x}` is an unknown tag *and* a misplaced edge
    definition. `_input_value` used to return on the anchor before reaching
    the tag check while `_opaque` checked the tag first, so input position
    reported one code and passthrough reported two — the same construct, two
    verdicts. The property above cannot be relied on alone to catch this:
    until its payload keys were widened it could not spell `wic_anchor`.
    """
    in_position = parse(f'steps:\n- id: s\n  in:\n    a: !foo {payload}\n', 'both.wic')
    passthrough = parse(f'top: !foo {payload}\n', 'both.wic')

    assert {d.code for d in in_position.diagnostics} == expected
    assert {d.code for d in passthrough.diagnostics} == expected


@pytest.mark.fast
def test_a_run_stem_nowhere_is_reported() -> None:
    """A `run:` path with no file beside the document and no registry entry is wic013."""
    from .hermetic import compile_hermetic  # pylint: disable=import-outside-toplevel
    with pytest.raises(SophiosError) as caught:
        compile_hermetic({'steps': [{'id': 's', 'run': 'nowhere.cwl'}]})
    assert caught.value.diagnostics[0].code is SophiosErrorCode.SUBWORKFLOW_INVALID


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


@pytest.mark.fast
@pytest.mark.parametrize('value', [
    '&x !cwl {q: *x}',
    '&x !cwl {<<: {q: *x}}',
    '&x !cwl {q: *x, <<: {r: 1}}',
    '&x !cwl {q: !cwl {r: *x}}',
    '&x {wic_raw_cwl: {q: *x}}',
], ids=['direct', 'through-merge', 'beside-merge', 'nested', 'desugared'])
@pytest.mark.parametrize('where', ['passthrough', 'step-input'])
def test_a_raw_cwl_record_that_contains_itself_is_an_alias_cycle(value: str, where: str) -> None:
    """A `!cwl` record reached through its own alias is the cycle every other alias cycle is."""
    source = f'a: {value}\n' if where == 'passthrough' else f'steps:\n  s:\n    in:\n      a: {value}\n'
    result = parse(source, 'c.wic')
    cycles = [d.message for d in result.diagnostics if d.code is SophiosErrorCode.RECURSIVE_ALIAS]
    assert cycles == ['alias cycle: a node contains itself'], [str(d) for d in result.diagnostics]
