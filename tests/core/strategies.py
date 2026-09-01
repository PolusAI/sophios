"""Generators for Sophios source text.

Shared because more than one suite quantifies over the same language: the
parser's properties, the renderer's round trip, and the schema's forward
direction all need documents, and a second generator beside the first is how
outputs and nested sidecars once became invisible to every property fed by it.

The alphabets here are deliberate. Each was widened in response to a class of
finding — spellings whose text differs from their value, keys carrying `$`,
empty names — and narrowing one narrows every property downstream.
"""
from hypothesis import strategies as st

identifiers = st.text('abcdefghijklmnopqrstuvwxyz_', min_size=1, max_size=8)
#: Scalar payloads as YAML source text, spelled to include exactly what a
#: [a-z_]-only alphabet can never produce: type-ambiguous quoted strings,
#: exponent floats and specials, null, dates. The #383 review traced four of
#: its findings to the old alphabet's blind spot; these are its complement.
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
construct_payload_texts = st.sampled_from(['!* e', '!& d', '!cwl a/b', '{wic_anchor: n}'])

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
    form = draw(st.sampled_from(['ii', 'anchor', 'alias', 'cwl', 'bare',
                                 'ii_desugared', 'anchor_desugared', 'alias_desugared', 'cwl_desugared']))
    match form:
        case 'ii':
            return f'{indent}{name}: !ii {draw(opaque_payload_texts)}'
        case 'anchor':
            return f'{indent}{name}: !& {draw(identifiers)}'
        case 'alias':
            return f'{indent}{name}: !* {draw(identifiers)}'
        case 'cwl':
            return f'{indent}{name}: !cwl {draw(identifiers)}/{draw(identifiers)}'
        case 'ii_desugared':
            return f'{indent}{name}: {{wic_inline_input: {draw(desugared_payload_texts)}}}'
        case 'anchor_desugared':
            return f'{indent}{name}: {{wic_anchor: {draw(identifiers)}}}'
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
    sidecars. Review of this PR found the previous generator quantified over
    mapping-form `in:`-only documents, leaving outputs, sequence steps, and
    depth-2 sidecars structurally invisible to every property fed by it.
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
        lines += ['wic:', '  graphviz:', f'    label: {draw(identifiers)}']
        if draw(st.booleans()):
            # A nested sidecar: the (index, name) key wraps a wic: block.
            lines += ['  steps:', f'    (1, {names[0]}):', '      wic:',
                      '        steps:', f'          (1, {draw(identifiers)}):',
                      f'            label: {draw(identifiers)}']
    return '\n'.join(lines) + '\n'
