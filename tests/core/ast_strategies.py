"""Strategies over the language AST, rendered and re-parsed by `parse` before compiling, so writer and reader must
agree and a counterexample shrinks to a minimal document; stems and bindings name synthetic_tools' real ports."""
import copy
from typing import Any, Final, cast

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sophios import utils_cwl
from sophios.lang import (CwlRecord, Document, EdgeDef, EdgeRef, Grammar, InlineLiteral, InputValue,
                          OpaqueCwl, OutputBinding, RawCwlRef, Step, StepKey, UnresolvedName, WicSidecar,
                          parse, render, to_json)
from sophios.lang.spans import SourceSpan
from sophios.wic_types import Yaml

from .reference_model import may_reference
from .synthetic_tools import SCHEMA_TYPES, STEMS, inputs_of, outputs_of, required_inputs_of

#: The one span generated nodes carry: they have no source, and nothing downstream reads it.
_SPAN: Final = SourceSpan('<generated>', 1, 1, 1, 1)

literals: Final = st.one_of(
    st.text('abcxyz_.', min_size=1, max_size=8),
    st.integers(min_value=-4, max_value=4),
    st.booleans(),
    st.sampled_from(['0777', '1.50', 'yes', '0x1f', '00']),
)

_LITERALS_BY_TYPE: Final[dict[str, SearchStrategy[Any]]] = {
    'string': st.text('abcxyz_.', min_size=1, max_size=8),
    'int': st.integers(min_value=-4, max_value=4),
    'float': st.floats(min_value=-4, max_value=4, allow_nan=False, allow_infinity=False),
    'boolean': st.booleans(),
    # No `.`: a location of `.` or `..` names a directory, which no File is.
    'File': st.text('abcxyz_', min_size=1, max_size=8),
    'Directory': st.text('abcxyz_', min_size=1, max_size=8),
}

_SCATTER_METHODS: Final = ('dotproduct', 'flat_crossproduct', 'nested_crossproduct')


def _literal_for(declared: Any) -> SearchStrategy[Any]:
    """Draw a well-typed literal for `declared`, structured types included; hostile ones are drawn elsewhere."""
    members = declared if isinstance(declared, list) else [declared]
    for member in members:
        name = member[:-1] if isinstance(member, str) and member.endswith('?') else member
        while isinstance(name, str) and name.endswith('[]'):
            name = name[:-2]
        if isinstance(name, str) and name in SCHEMA_TYPES:
            name = SCHEMA_TYPES[name]
        if isinstance(name, dict) and name.get('type') == 'record':
            return st.fixed_dictionaries({field['name']: _literal_for(field['type'])
                                          for field in name['fields']})
        if isinstance(name, dict) and name.get('type') == 'enum':
            return st.sampled_from(list(name['symbols']))
        if isinstance(name, dict) and name.get('type') == 'array':
            return st.lists(_literal_for(name['items']), max_size=2)
        if isinstance(name, str) and name in _LITERALS_BY_TYPE:
            return _LITERALS_BY_TYPE[name]
    return literals


edge_names: Final = st.text('abcdefgh', min_size=1, max_size=4)


def _fresh_edge(draw: st.DrawFn, defined_edges: list[tuple[str, Any]], carries: Any) -> str:
    """Draw a `!&` name no earlier definition in this document used: two alike are wic026, so the count of edges
    defined so far is appended."""
    name = f'{draw(edge_names)}{len(defined_edges)}'
    defined_edges.append((name, carries))
    return name


#: Workflow-level inputs a bare name may resolve to, one list for declaration and reference, so `documents()` declares
#: what any step referenced (an undeclared bare name is wic011) and UnresolvedName can be drawn at all.
declared_inputs: Final[tuple[tuple[str, Any], ...]] = (
    ('wf_name', 'string'),
    ('wf_count', 'int'),
    ('wf_file', 'File'),
    ('wf_factor', 'float'),
    ('wf_any', 'Any'),
)


def _inputs_declared(referenced: set[str]) -> list[tuple[str, OpaqueCwl]]:
    """Top-level `inputs:` declaring exactly what the steps referenced: an undeclared bare name is wic011."""
    types = dict(declared_inputs)
    return [('inputs', {name: {'type': types[name]} for name in sorted(referenced)})] if referenced else []


def _structured(declared: Any) -> bool:
    """Whether `declared` is, or holds, a record, an enum or a named schema type."""
    members = declared if isinstance(declared, list) else [declared]
    for member in members:
        if isinstance(member, str) and member in SCHEMA_TYPES:
            return True
        if isinstance(member, dict) and (member.get('type') in ('record', 'enum') or _structured(member.get('items'))):
            return True
    return False


def _may_feed(source_type: Any, sink_type: Any) -> bool:
    """Whether a generated reference from `source_type` may feed `sink_type`; into a structured sink only `Any`, since
    cwltool rejects every other source the compiler lets through."""
    if _structured(sink_type):
        return isinstance(source_type, str) and source_type == 'Any'
    return may_reference(source_type, sink_type)


def _carried(declared: Any) -> Any:
    """What an edge from an output declared `declared` carries: `stdout` and `stderr` are a `File`."""
    return 'File' if declared in ('stdout', 'stderr') else declared


@st.composite
def _record(draw: st.DrawFn, sink: Any, fits: list[str], references: tuple[str, ...],
            referenced_inputs: set[str]) -> CwlRecord:
    """A step-input record: one source with a default, or, into an array, two merged (each fits, so merge_flattened
    keeps it one array)."""
    array = isinstance(sink, dict) and sink.get('type') == 'array'
    if len(fits) > 1 and array and draw(st.booleans()):
        pair = draw(st.permutations(fits))[:2]
        return CwlRecord(tuple(EdgeRef(edge, _SPAN) for edge in pair), (('linkMerge', 'merge_flattened'),), _SPAN)
    source: EdgeRef | UnresolvedName
    if fits and (not references or draw(st.booleans())):
        source = EdgeRef(draw(st.sampled_from(fits)), _SPAN)
    else:
        declared = draw(st.sampled_from(references))
        referenced_inputs.add(declared)
        source = UnresolvedName(declared, _SPAN)
    default = draw(_literal_for(sink['items'] if array else sink))
    return CwlRecord((source,), (('default', [default] if array else default),), _SPAN)


@st.composite
def _step(draw: st.DrawFn, stem: str, defined_edges: list[tuple[str, Any]],
          referenced_inputs: set[str]) -> Step:
    """One tool step: a real stem, its real input names, a subset bound; an unbound required input is the only way an
    inferred edge exists. `defined_edges` and `referenced_inputs` are the document's, so they are mutated."""
    # pylint: disable=too-many-branches,too-many-locals,too-many-statements  # one branch per construct
    names = sorted(inputs_of(stem))

    # Drawn first: a scattered input consumes an array, so a reference must carry one; a literal may be a scalar.
    interpreted: list[tuple[str, OpaqueCwl]] = []
    ports: list[str] = []
    layers = 0
    match draw(st.sampled_from([None, 'scatter', 'when'] if names else [None, 'when'])):
        case 'scatter':
            count = draw(st.sampled_from(range(1, min(3, len(names)) + 1)))
            ports = draw(st.permutations(names))[:count]
            scattered: list[OpaqueCwl] = list(ports)
            interpreted.append(('scatter', scattered))
            # Required by the spec over two or more ports; optional over one.
            method = draw(st.sampled_from(_SCATTER_METHODS) if len(ports) > 1
                          else st.sampled_from((None, *_SCATTER_METHODS)))
            if method is not None:
                interpreted.append(('scatterMethod', method))
            layers = len(ports) if method == 'nested_crossproduct' else 1
        case 'when':
            interpreted.append(('when', '$(true)'))

    def sink(name: str) -> Any:
        declared = inputs_of(stem)[name].get('type')
        return {'type': 'array', 'items': declared} if name in ports else declared

    def literal(name: str) -> InlineLiteral:
        value = draw(_literal_for(inputs_of(stem)[name].get('type')))
        return InlineLiteral([value] if name in ports else value, _SPAN)

    chosen = draw(st.lists(st.sampled_from(names), unique=True, max_size=len(names))) if names else []
    # Infer lifts only a required input, so a scattered one that is not must be bound.
    chosen += [name for name in ports if name not in chosen and name not in required_inputs_of(stem)]
    # Two inferred File inputs of one step would read one output, which inference refuses (wic055).
    if stem == 'join':
        chosen += [name for name in ('left', 'right') if name not in chosen]
    connectable = [name for name in names
                   if any(_may_feed(carries, sink(name)) for _, carries in defined_edges)]
    forced: str | None = None
    if bool(connectable) and draw(st.booleans()):
        forced = draw(st.sampled_from(connectable))
        chosen = chosen if forced in chosen else [*chosen, forced]

    bindings: list[tuple[str, InputValue]] = []
    for name in chosen:
        fits = [edge for edge, carries in defined_edges if _may_feed(carries, sink(name))]
        if name == forced:
            bindings.append((name, EdgeRef(draw(st.sampled_from(fits)), _SPAN)))
            continue

        references = tuple(input_name for input_name, source_type in declared_inputs
                           if _may_feed(source_type, sink(name)))
        forms = ['literal'] + (['unresolved', 'raw'] if references else []) + (['ref'] if fits else []) \
            + (['record'] if references or fits else [])
        match draw(st.sampled_from(forms)):
            case 'literal':
                bindings.append((name, literal(name)))
            case 'unresolved' | 'raw' as form:
                declared = draw(st.sampled_from(references))
                referenced_inputs.add(declared)
                bindings.append((name, UnresolvedName(declared, _SPAN) if form == 'unresolved'
                                 else RawCwlRef(declared, _SPAN)))
            case 'record':
                bindings.append((name, draw(_record(sink(name), fits, references, referenced_inputs))))
            case _:
                bindings.append((name, EdgeRef(draw(st.sampled_from(fits)), _SPAN)))

    # Inference refuses an input inferred from an output another input of the step reads (wic055), so a step that
    # reads an edge binds every required input.
    if any(isinstance(source, EdgeRef) for _, value in bindings
           for source in (value.sources if isinstance(value, CwlRecord) else (value,))):
        bindings += [(name, literal(name)) for name in required_inputs_of(stem) if name not in chosen]

    # `bool(...)` around the left operand of `and draw(...)`: mypy otherwise types draw's result from that operand.
    outs: list[OutputBinding] = []
    if bool(outputs_of(stem)):
        for out_name in draw(st.lists(st.sampled_from(sorted(outputs_of(stem))),
                                      unique=True, max_size=2)):
            if draw(st.booleans()):
                carries = _carried(outputs_of(stem)[out_name].get('type'))
                for _ in range(layers):
                    carries = {'type': 'array', 'items': carries}
                edge = _fresh_edge(draw, defined_edges, carries)
                outs.append(OutputBinding(out_name, EdgeDef(edge, _SPAN), _SPAN))
            else:
                outs.append(OutputBinding(out_name, None, _SPAN))

    passthrough: list[tuple[str, OpaqueCwl]] = []
    if draw(st.booleans()):
        passthrough.append((draw(st.sampled_from(['label', 'doc'])), draw(st.text('abc ', max_size=6))))

    return Step(id=stem, inputs=tuple(bindings), outputs=tuple(outs),
                interpreted=tuple(interpreted), passthrough=tuple(passthrough), span=_SPAN)


@st.composite
def documents(draw: st.DrawFn) -> Document:  # pylint: disable=too-many-locals
    """A well-formed document over every construct the .wic reference tabulates, well typed where it binds a literal,
    in both forms. Mapping-form stems are drawn unique up front: a step drawn then discarded would leave its `!&` names
    dangling."""
    as_mapping = draw(st.booleans())
    stems = draw(st.lists(st.sampled_from(STEMS), min_size=1, max_size=4,
                          unique=as_mapping))
    defined_edges: list[tuple[str, Any]] = []
    referenced_inputs: set[str] = set()

    steps: list[Step] = [draw(_step(stem, defined_edges, referenced_inputs))
                         for stem in stems]

    # Sometimes a subworkflow step, known by its `.wic` id; `to_yml` attaches its body, which the AST has no field for.
    if bool(steps) and draw(st.booleans()):
        body = draw(st.sampled_from(sorted(SUBWORKFLOW_BODIES)))
        call: tuple[tuple[str, InputValue], ...] = ()
        scatter: tuple[tuple[str, OpaqueCwl], ...] = ()
        if 'inputs' in SUBWORKFLOW_BODIES[body]:
            call = (('label', InlineLiteral(draw(_literal_for('string')), _SPAN)),)
            if draw(st.booleans()):
                scatter = (('scatter', ['label']),)
        steps.append(Step(id=f'{body}{len(steps)}.wic', inputs=call, interpreted=scatter,
                          span=_SPAN))

    sidecar = None
    if draw(st.booleans()):
        entries: tuple[tuple[str, OpaqueCwl], ...] = (('graphviz', {'label': draw(edge_names)}),)
        nested: tuple[tuple[StepKey, WicSidecar], ...] = ()
        if bool(steps) and draw(st.booleans()):
            first = steps[0].id
            unique = [step.id for step in steps].count(first) == 1 and bool(Grammar.WIC_STEP_ID.match(first))
            step_key = StepKey(None, first) if unique and draw(st.booleans()) else StepKey(1, first)
            nested = ((step_key,
                       WicSidecar(entries=(('graphviz', {'label': draw(edge_names)}),), span=_SPAN)),)
        sidecar = WicSidecar(steps=nested, entries=entries, span=_SPAN)

    passthrough = _inputs_declared(referenced_inputs)
    if draw(st.booleans()):
        key = draw(st.sampled_from(['label', 'doc', '$schemas']))
        passthrough.append((key, ['https://example/s.owl'] if key == '$schemas'
                            else draw(st.text('abc ', max_size=6))))

    return Document(steps=tuple(steps), sidecar=sidecar,
                    passthrough=tuple(passthrough), span=_SPAN,
                    steps_as_mapping=as_mapping)


#: The bodies a subworkflow step's id selects, by its stem less the appended index; each literal is a child job value
#: Complete lifts to the caller, and `decl` declares an input its caller binds and may scatter over.
SUBWORKFLOW_BODIES: Final[dict[str, Yaml]] = {
    'sub': {'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'sub.txt'}}}]},
    'file': {'steps': [{'id': 'xform', 'in': {'file': {'wic_inline_input': 'in.txt'},
                                              'name': {'wic_inline_input': 'out.txt'}}}]},
    'object': {'steps': [{'id': 'xform', 'in': {
        'file': {'wic_inline_input': {'class': 'File', 'location': 'in.txt'}},
        'name': {'wic_inline_input': 'out.txt'}}}]},
    'dir': {'steps': [{'id': 'split', 'in': {'dir': {'wic_inline_input': 'data'},
                                             'name': {'wic_inline_input': 'head.txt'}}}]},
    'decl': {'inputs': {'label': {'type': 'string'}},
             'steps': [{'id': 'mk_file', 'in': {'name': 'label'}}]},
}


def to_yml(document: Document) -> Yaml:
    """The compiler's input for a generated document: rendered and parsed as a user's file is, then desugared as
    read_ast_from_disk does (mapping-form steps fail without it), and subworkflow bodies attached as files would be."""
    reread = parse(render(document), 'generated.wic')
    assert not reread.diagnostics.has_errors and reread.document is not None, [str(d) for d in reread.diagnostics]
    loaded: Yaml = utils_cwl.desugar_into_canonical_normal_form(to_json(reread.document))
    for step in loaded.get('steps', []):
        if isinstance(step, dict) and str(step.get('id', '')).endswith('.wic'):
            body = str(step['id']).removesuffix('.wic').rstrip('0123456789')
            step['subtree'] = copy.deepcopy(SUBWORKFLOW_BODIES[body])
            step['parentargs'] = {}
    return loaded


def workflows() -> SearchStrategy[Yaml]:
    """The strategy the compile-driving properties quantify over: `documents()` through `to_yml`."""
    return documents().map(to_yml)  # pylint: disable=no-member


#: `join` is absent: its `left` and `right` would be inferred from one output, which inference refuses (wic055).
_SCATTERABLE_STRING_INPUTS: Final[tuple[tuple[str, str], ...]] = (
    ('mk_file', 'name'), ('mk_text', 'name'), ('xform', 'name'),
)


@st.composite
def _scattering_step(draw: st.DrawFn) -> Step:
    """One tool step forced to scatter over a correctly array-valued input."""
    stem, name = draw(st.sampled_from(_SCATTERABLE_STRING_INPUTS))
    literal = cast(OpaqueCwl, draw(st.lists(
        st.text('abcxyz_.', min_size=1, max_size=8), min_size=1, max_size=3)))
    return Step(id=stem, inputs=((name, InlineLiteral(literal, _SPAN)),),
                interpreted=(('scatter', [name]),), span=_SPAN)


@st.composite
def freighted_documents(draw: st.DrawFn) -> tuple[Document, int]:
    """A multi-step document with one scattering step designated for passthrough freight."""
    defined_edges: list[tuple[str, Any]] = []
    referenced_inputs: set[str] = set()
    count = draw(st.integers(min_value=2, max_value=4))
    scatter_at = draw(st.integers(min_value=0, max_value=count - 1))

    steps: list[Step] = []
    for index in range(count):
        steps.append(draw(_scattering_step()) if index == scatter_at
                     else draw(_step(draw(st.sampled_from(STEMS)),
                                     defined_edges, referenced_inputs)))

    return Document(steps=tuple(steps), passthrough=tuple(_inputs_declared(referenced_inputs)), span=_SPAN), scatter_at


def to_yml_with_freight(document: Document, step_index: int, freight: dict[str, Any]) -> Yaml:
    """Render ``document`` and add passthrough ``freight`` to its designated step."""
    loaded = to_yml(document)
    loaded['steps'][step_index].update(freight)
    return loaded


#: Keys and JSON-shaped values the language does not claim and must preserve.
#: `requirements` and `hints` are claimed: the parser reads their list form as a mapping.
CLAIMED_STEP_KEYS: Final = (frozenset({'id', 'in', 'out', 'wic', 'requirements', 'hints'})
                            | Grammar.INTERPRETED_STEP_KEYS)

passthrough_keys: Final = st.one_of(
    st.text('abcdefghijklmnopqrstuvwxyz_', min_size=3, max_size=12),
    st.sampled_from(['$namespaces', '$schemas', 'label', 'doc', 'scatterMethod']),
).filter(lambda key: key not in CLAIMED_STEP_KEYS)

passthrough_values: Final = st.recursive(
    st.one_of(st.integers(min_value=-100, max_value=100), st.booleans(),
              st.text('abc xyz', max_size=8), st.none()),
    lambda children: st.one_of(st.lists(children, max_size=3),
                               st.dictionaries(st.text('abc', min_size=1, max_size=5),
                                               children, max_size=3)),
    max_leaves=8,
)

#: A `hints:` value as the parser keeps it: the list form is read as a mapping, so it survives byte-identically only
#: with an $import or $include, which test_leak_boundary pins.
hints_values: Final = st.dictionaries(
    st.sampled_from(['DockerRequirement', 'ResourceRequirement', 'NetworkAccess', 'cwltool:CUDARequirement']),
    st.dictionaries(st.text('abc', min_size=1, max_size=5), passthrough_values, max_size=3),
    max_size=3,
)
