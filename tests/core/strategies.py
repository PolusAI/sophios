"""Generators for Sophios source text.

Shared because more than one suite quantifies over the same language: the
parser's properties, the renderer's round trip, and the schema's forward
direction all need documents, and a second generator beside the first is how
outputs and nested sidecars once became invisible to every property fed by it.

The alphabets here are deliberate. Each was widened in response to a class of
finding — spellings whose text differs from their value, keys carrying `$`,
empty names — and narrowing one narrows every property downstream.
"""
import json
from calendar import monthrange
from collections.abc import Callable
from functools import reduce
from typing import Any, Final

import yaml
from hypothesis import example, strategies as st
from hypothesis.strategies import SearchStrategy

from sophios.lang import Forms


def pinned(*examples: Any) -> Callable[[Any], Any]:
    """Each of `examples` as an `@example` of the property it decorates: a table too long for a decorator a case."""
    return lambda test: reduce(lambda done, case: example(case)(done), examples, test)


identifiers = st.text('abcdefghijklmnopqrstuvwxyz_', min_size=1, max_size=8)
#: Scalar payloads as YAML source text, spelled to include exactly what a
#: [a-z_]-only alphabet can never produce: type-ambiguous quoted strings,
#: exponent floats and specials, null, dates. Four round-trip defects traced to
#: the old alphabet's blind spot; this list is its complement.
scalar_payload_texts = st.sampled_from([
    'x', '0', '-3', '1.5', '1.0e+300', '.inf', '.nan',
    'true', 'false', 'null', 'on', '2020-01-01',
    "'0'", "'true'", "'null'", "'on'", "''", "'a b'",
    # Spellings YAML normalises away: the value no longer knows how it was
    # written, so anything that re-serialises the value instead of emitting
    # the source text loses them. They are the only spellings that can tell
    # transcription apart from reconstruction, and none of the above can.
    '0777', '1.50', 'yes', '0x1f', '1:30', '+5', '00',
])

#: Construct leaves that may appear nested inside an `!ii` payload.
#: Neither spelling of an edge definition appears here. An edge is defined on
#: an `out:` entry and nowhere else (.wic reference §3.6), so `!& d` and
#: `{wic_anchor: n}` both earn wic019 in a payload — `_out_lines` below
#: generates both spellings in the one position they are legal.
construct_payload_texts = st.sampled_from(['!* e', '!cwl a/b', '{wic_alias: n}'])

#: Recursive payload text over the closed OpaqueCwl union: scalars, nested
#: constructs, and flow collections of both. Derived from what the type
#: admits, not from what the tests happened to imagine.
_payload_collections = st.recursive(
    st.one_of(scalar_payload_texts, construct_payload_texts),
    lambda children: st.one_of(
        st.lists(children, min_size=0, max_size=3).map(lambda xs: '[' + ', '.join(xs) + ']'),
        st.lists(children, min_size=1, max_size=3).map(
            lambda xs: '{' + ', '.join(f'k{i}: {x}' for i, x in enumerate(xs)) + '}'),
    ),
    max_leaves=6,
).filter(lambda s: s.startswith(('[', '{')))

#: Direct payload of a tagged `!ii`: a scalar or a collection — a construct
#: leaf cannot be the whole payload, since two tags on one node is not YAML.
opaque_payload_texts = st.one_of(scalar_payload_texts, _payload_collections)

#: Desugared payloads can additionally carry a construct directly:
#: `{wic_inline_input: !* e}` is well-formed, and the writer must respell it.
desugared_payload_texts = st.one_of(opaque_payload_texts, construct_payload_texts)


@st.composite
def input_lines(draw: st.DrawFn, name: str | None = None, indent: str = '      ') -> str:
    """One `in:` binding, in any of the forms the language admits.

    Both spellings of each construct are generated — the tagged form people
    write and the desugared form tooling emits — so the properties quantify
    over the language, not over the half of it the tests happened to spell.
    """
    # pylint: disable=too-many-return-statements  # one return per surface form
    name = draw(identifiers) if name is None else name
    # No 'anchor' form: `!&` defines an edge and is legal only on an `out:`
    # entry (.wic reference §3.6), so an input carrying one is not a well-formed
    # document. `_out_lines` below still generates both of its spellings, so
    # edge definitions stay covered where they belong.
    form = draw(st.sampled_from(['ii', 'alias', 'cwl', 'bare',
                                 'ii_desugared', 'alias_desugared', 'cwl_desugared']))
    match form:
        case 'ii':
            return f'{indent}{name}: !ii {draw(opaque_payload_texts)}'
        case 'alias':
            return f'{indent}{name}: !* {draw(identifiers)}'
        case 'cwl':
            return f'{indent}{name}: !cwl {draw(identifiers)}/{draw(identifiers)}'
        case 'ii_desugared':
            return f'{indent}{name}: {{wic_inline_input: {draw(desugared_payload_texts)}}}'
        case 'alias_desugared':
            return f'{indent}{name}: {{wic_alias: {draw(identifiers)}}}'
        case 'cwl_desugared':
            return f'{indent}{name}: {{wic_raw_cwl: {draw(identifiers)}/{draw(identifiers)}}}'
        case _:
            return f'{indent}{name}: {draw(identifiers)}'


#: Output names as source text, including the empty one. `out: ['']` parses
#: without a diagnostic, so it is inside the language — and a generator whose
#: names were all non-empty is why a schema constraint that rejected it went
#: unnoticed. The alphabet has to reach the edges of what the parser admits.
output_name_texts = st.one_of(identifiers, st.just("''"))


@st.composite
def _out_lines(draw: st.DrawFn, indent: str) -> list[str]:
    """An `out:` block: bare names and `!&`-bound names, in both spellings."""
    lines = [f'{indent}out:']
    for _ in range(draw(st.integers(min_value=1, max_value=2))):
        name = draw(output_name_texts)
        match draw(st.sampled_from(['bare', 'edge', 'edge_desugared'])):
            case 'bare':
                lines.append(f'{indent}- {name}')
            case 'edge':
                lines.append(f'{indent}- {name}: !& {draw(identifiers)}')
            case _:
                lines.append(f'{indent}- {name}: {{wic_anchor: {draw(identifiers)}}}')
    return lines


@st.composite
def documents(draw: st.DrawFn) -> str:
    """A syntactically well-formed Sophios document.

    Generates both step surface forms (mapping and sequence-with-id), inputs
    in every admitted spelling, `out:` blocks, and nested `wic: steps:`
    sidecars. Every one of those is load-bearing: a generator drawing only
    mapping-form `in:`-only documents leaves outputs, sequence steps and
    depth-2 sidecars structurally invisible to every property fed by it, which
    no property downstream can report because none of them ever sees one.
    """
    lines = ['steps:']
    sequence_form = draw(st.booleans())
    names = draw(st.lists(identifiers, min_size=1, max_size=4, unique=True))
    for step in names:
        if sequence_form:
            lines.append(f'- id: {step}')
            body_indent = '  '
        else:
            lines.append(f'  {step}:')
            body_indent = '    '
        lines.append(f'{body_indent}in:')
        # Unique: binding the same input twice is a diagnosed error, not a
        # well-formed document (see wic010).
        for name in draw(st.lists(identifiers, min_size=1, max_size=3, unique=True)):
            lines.append(draw(input_lines(name, indent=body_indent + '  ')))
        if draw(st.booleans()):
            lines.extend(draw(_out_lines(body_indent)))
    if draw(st.booleans()):
        # Quoted: a label is a string (.wic reference §7), and `null`, `no` or `on` would
        # load as something else.
        lines += ['wic:', '  graphviz:', f"    label: '{draw(identifiers)}'"]
        if draw(st.booleans()):
            # A nested sidecar: the (index, name) key wraps a wic: block.
            lines += ['  steps:', f'    (1, {names[0]}):', '      wic:',
                      '        steps:', f'          (1, {draw(identifiers)}):',
                      '            graphviz:', f"              label: '{draw(identifiers)}'"]
    return '\n'.join(lines) + '\n'


@st.composite
def scalar_spellings(draw: st.DrawFn) -> tuple[str, str | None]:
    """A scalar as YAML text, and the tag YAML resolves it to but cannot construct, or None where YAML reads it:
    integers in each YAML 1.1 base and form, floats, booleans, nulls, the shared payloads, dates with month 0-14 and
    day 0-40, `0x_` and `0b_`. Any of them quoted, escaped, is text."""
    n, year, month, day = (draw(st.integers(-2**70, 2**70)), draw(st.integers(1, 9999)), draw(st.integers(0, 14)),
                           draw(st.integers(0, 40)))
    text, tag = draw(st.sampled_from([
        (str(n), None), (f'{n:+_}', None), (hex(n), None), (bin(n), None), (f'0{abs(n):o}', None),
        (f'{abs(n) // 60}:{abs(n) % 60:02}', None), (repr(draw(st.floats())), None), (draw(scalar_payload_texts), None),
        (draw(st.sampled_from(['Yes', 'NO', 'Off', '~', 'Null', '.NaN', '-.Inf', '1.', '1e3'])), None),
        (f'{year:04}-{month:02}-{day:02}', None if 0 < month < 13 and 0 < day <= monthrange(year, month)[1]
         else 'timestamp'), (draw(st.sampled_from(['0x_', '0b_'])), 'int')]))
    quote = draw(st.sampled_from(['', "'", '"']))
    return {'': (text, tag), "'": ("'" + text.replace("'", "''") + "'", None), '"': (json.dumps(text), None)}[quote]


#: A well-formed value for every `wic:` key, as flow YAML. Keyed by the
#: declaration, so a key added there without a row here fails the lookup.
WIC_VALUES: Final = {
    'graphviz': '{label: x, style: dashed, ranksame: ["(1, s)"]}', 'implementations': '{}',
    'driver': 'slurm', 'inlineable': 'true', 'scatterMethod': 'dotproduct', 'in': '{}',
    'scatter': '[x]', 'inference': '{}', 'implementation': 'x', 'default_implementation': 'x',
    'version': 'x', 'lang_version': 'x', 'namespace': 'x',
}

#: What `shapes()` writes where the language reads a name: strings only.
_SHAPE_NAMES = st.sampled_from(['s', 'in', 'out', 'id', '', 'a.wic', 'x y'])
_SHAPE_SCALARS = st.one_of(st.none(), st.booleans(), st.integers(-1, 2), st.sampled_from(['', 'x', '(1, s)', 'slurm']))
#: Any tag-free value. No key is `id` or `out`, so a value drawn where steps go holds no name of another type.
_ANY = st.recursive(_SHAPE_SCALARS, lambda inner: st.lists(inner, max_size=2) | st.dictionaries(
    st.sampled_from(['s', 'in', '', 'a.wic', 'x y']), inner, max_size=2), max_leaves=4)


def _or_any(shape: SearchStrategy[Any]) -> SearchStrategy[Any]:
    """The shape the language gives a place, or, a third of the time, any value."""
    return st.one_of(shape, shape, _ANY)


#: A step body: `in:` values are any value or a construct, and an `out:` entry a name, a list or one `!&`.
_BODY = st.fixed_dictionaries({}, optional={
    'in': _or_any(st.dictionaries(_SHAPE_NAMES, _ANY | st.sampled_from(sorted(Forms.DESUGARED_KEYS)).map(
        lambda key: {key: 'x'}) | st.just({'wic_raw_cwl': {'source': 'x'}}), max_size=2)),
    'out': _SHAPE_SCALARS | st.dictionaries(_SHAPE_NAMES, _ANY, max_size=1) | st.lists(
        _SHAPE_NAMES | st.lists(_SHAPE_NAMES, max_size=1) | st.dictionaries(
            _SHAPE_NAMES, st.just({'wic_anchor': 'e'}), max_size=1), max_size=2),
    'run': _ANY, 'label': _ANY})
_IDS = _SHAPE_NAMES | st.lists(_SHAPE_NAMES, max_size=1) | st.dictionaries(_SHAPE_NAMES, _ANY, max_size=1)
_STEPS = _or_any(st.dictionaries(_SHAPE_NAMES, _or_any(_BODY), max_size=2) | st.lists(
    st.builds(lambda name, body: {'id': name, **body}, _IDS, _BODY)
    | st.dictionaries(_SHAPE_NAMES.filter(lambda name: name != 'id'), _or_any(_BODY), min_size=1, max_size=1),
    max_size=2))


def _wic(depth: int) -> SearchStrategy[Any]:
    """A `wic:` block: each key with its well-formed value or any other, and `steps:` entries `depth` deep."""
    entries = {key: st.just(yaml.safe_load(text)) | _ANY for key, text in WIC_VALUES.items()} | {'nonsense': _ANY}
    if depth:
        entry = _wic(depth - 1)
        entries['steps'] = _or_any(st.dictionaries(st.sampled_from(['(1, s)', 's', 'not a key']), entry | st.builds(
            lambda wic, rest: {'wic': wic, **rest}, entry, st.fixed_dictionaries({}, optional={'in': _ANY})),
            max_size=2))
    return _or_any(st.fixed_dictionaries({}, optional=entries))


def shapes() -> SearchStrategy[dict[str, Any]]:
    """A tag-free document over the parser's own keys, as YAML loads it: each place holds the shape the language
    gives it or another, so most are malformed and some are well-formed. JSON writes it as YAML the parser reads.

    Not drawn, the four places the two readers disagree, each pinned where it is tested: a name YAML types, as `1`,
    which the parser reads as its text and the schema refuses; and an `out:` entry binding anything but an edge
    definition, a construct around a collection, and `out:` under `wic: steps:`, which the parser refuses alone."""
    return st.fixed_dictionaries({}, optional={'steps': _STEPS, 'wic': _wic(2), 'label': _ANY})


def cwl_types(*atoms: Any) -> SearchStrategy[Any]:
    """A CWL v1.2 type over `atoms` as a document may write it (the Type DSL): an atom; a str atom with the
    shorthand `?` or `[]`; and, recursively, a union list of 1 to 3 types and `{type: array, items: T}`."""
    atom = st.sampled_from(atoms)
    named = st.sampled_from([each for each in atoms if isinstance(each, str)])
    return st.recursive(
        atom | named.map(lambda each: f'{each}?') | named.map(lambda each: f'{each}[]'),
        lambda inner: (st.lists(inner, min_size=1, max_size=3)
                       | inner.map(lambda items: {'type': 'array', 'items': items})),
        max_leaves=5)


#: Names as an author writes a port, step or workflow: both cases (a and A meet where case does not count), digits,
#: _ . -, each character Sophios or a file system refuses in a name, and a letter outside ASCII; and the names Python
#: treats apart: keywords, a leading _, dunders, a port collection's attributes.
#: No `'` (the step-id property writes each name inside single-quoted YAML) and no brace.
author_names = st.text('aAzZ09_.-~@/\\ #?%:*"<>|\xe9', min_size=1, max_size=6) | st.sampled_from([
    'class', 'in', 'lambda', '_', '_x', '_store', '_items', 'items', 'keys', 'to_dict', '__x__', '__deepcopy__',
    'input-file', '\xe9', 'A', 'a'])
