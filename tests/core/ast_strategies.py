"""Strategies over the language AST, and their compilable projection.

Documents are built as `sophios.lang.nodes` values and rendered to source, so
the space the properties quantify over is the space the types admit rather than
the space a dict builder imagined. Hypothesis shrinks the AST, so a
counterexample arrives as a minimal document instead of a minimal string.

The projection runs `render` then `yaml.load(..., Loader=wic_loader())` — the
compiler's own loader, not a third path — so the two front ends disagreeing is
a failure rather than a blind spot. Step ids name `synthetic_tools` stems and
bindings name that tool's real inputs, so what is drawn is a workflow the
compiler can resolve.
"""
import copy
from typing import Any, Callable, Final, cast

import yaml
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sophios import utils_cwl
from sophios.lang import (SophiosErrorCode, CwlRecord, Document, EdgeDef, EdgeRef, Grammar, InlineLiteral, InputValue,
                          OpaqueCwl, OutputBinding, RawCwlRef, Step, StepKey, UnresolvedName, WicSidecar,
                          render)
from sophios.lang.spans import SourceSpan
from sophios.utils_yaml import wic_loader
from sophios.wic_types import Yaml

from .reference_model import may_reference
from .synthetic_tools import STEMS, inputs_of, outputs_of, required_inputs_of

#: A span the AST needs and the surface never shows. Generated nodes have no
#: source, so they all carry the same one; nothing downstream reads it, and a
#: property about spans belongs to the parser, where real positions exist.
_SPAN: Final = SourceSpan('<generated>', 1, 1, 1, 1)

#: The construct kinds the coverage property enumerates. Derived from the reference's tables
#: (§3.1 surface forms, §3.3 outputs, §4.1 input forms, §4.3 interpreted keys,
#: §5 the sidecar) rather than from the strategy below, so a construct the
#: strategy stops producing is a failure instead of a silent narrowing.
#: Construct kinds the generator cannot draw, each with the reason. The design
#: requires every AST construct kind to appear within a bounded sample, so a kind
#: that cannot be drawn is a declared gap rather than a silent one --
#: `test_the_construct_inventory_accounts_for_every_input_kind` fails if a member
#: of the `InputValue` union appears in neither this nor `CONSTRUCTS`.
NOT_GENERATED: Final[dict[str, str]] = {}

CONSTRUCTS: Final[tuple[str, ...]] = (
    'steps_mapping', 'steps_sequence',
    'inline_literal', 'edge_ref', 'raw_cwl_ref', 'unresolved_name', 'cwl_record',
    'output_bare', 'output_edge',
    'interpreted_scatter', 'interpreted_scatterMethod', 'interpreted_when',
    'step_passthrough', 'top_passthrough',
    'sidecar', 'sidecar_steps',
    'subworkflow', 'inferred_input',
)


def constructs_in(document: Document) -> frozenset[str]:
    """Which construct kinds a document contains. One place, so the coverage property and the
    strategy cannot drift apart on what a construct is."""
    # pylint: disable=too-many-branches  # one branch per construct kind, by design
    found = {'steps_mapping' if document.steps_as_mapping else 'steps_sequence'}
    if document.passthrough:
        found.add('top_passthrough')
    if document.sidecar is not None:
        found.add('sidecar')
        if document.sidecar.steps:
            found.add('sidecar_steps')
    for step in document.steps:
        if step.id.endswith('.wic'):
            found.add('subworkflow')
        if step.passthrough:
            found.add('step_passthrough')
        for key, _ in step.interpreted:
            found.add(f'interpreted_{key}')
        for binding in step.outputs:
            found.add('output_edge' if binding.edge_def is not None else 'output_bare')
        bound = {name for name, _ in step.inputs}
        for _, value in step.inputs:
            match value:
                case InlineLiteral():
                    found.add('inline_literal')
                case EdgeRef():
                    found.add('edge_ref')
                case RawCwlRef():
                    found.add('raw_cwl_ref')
                case CwlRecord():
                    found.add('cwl_record')
                case _:
                    found.add('unresolved_name')
        if not step.id.endswith('.wic') and set(required_inputs_of(step.id)) - bound:
            found.add('inferred_input')
    return frozenset(found)


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
    """Draw an ordinary well-typed literal while leaving hostile literals reachable elsewhere."""
    members = declared if isinstance(declared, list) else [declared]
    for member in members:
        name = member[:-1] if isinstance(member, str) and member.endswith('?') else member
        while isinstance(name, str) and name.endswith('[]'):
            name = name[:-2]
        if isinstance(name, str) and name in _LITERALS_BY_TYPE:
            return _LITERALS_BY_TYPE[name]
    return literals


edge_names: Final = st.text('abcdefgh', min_size=1, max_size=4)


def _fresh_edge(draw: st.DrawFn, defined_edges: list[tuple[str, Any]], carries: Any) -> str:
    """Draws a `!&` name not already defined in this document.

    `edge_names` is a small bounded alphabet, drawn independently at each
    definition site; two steps landing on the same string would both try to
    `!&` it, which the compiler reports as `wic026`, not a workflow. Suffixing with how many edges the document has
    already defined makes every name unique — `defined_edges` is document-
    scoped (passed into every `_step` call for the document being built) —
    without touching what `edge_names` itself produces or how `EdgeRef` draws
    from `defined_edges`.
    """
    name = f'{draw(edge_names)}{len(defined_edges)}'
    defined_edges.append((name, carries))
    return name


#: Workflow-level input names a bare string may resolve to. A bare name is an
#: `UnresolvedName`, and it compiles only when the top-level `inputs:` mapping
#: has that key — `arg_var_is_input` at src/sophios/compiler.py:878; otherwise
#: it is `wic011` with advice to write `!ii`. So the declaration and the
#: reference are drawn from one list, and `documents()` declares whatever any
#: step referenced. Without this the strategy could not produce an
#: `UnresolvedName` at all, and the `unresolved_name` row of CONSTRUCTS would
#: be a construct the coverage property demands and nothing supplies.
declared_inputs: Final[tuple[tuple[str, Any], ...]] = (
    ('wf_name', 'string'),
    ('wf_count', 'int'),
    ('wf_file', 'File'),
    ('wf_factor', 'float'),
    ('wf_any', 'Any'),
)


def _references_for(sink_type: Any) -> tuple[str, ...]:
    """Inputs the independent model does not prove disjoint from this sink."""
    return tuple(input_name for input_name, source_type in declared_inputs
                 if may_reference(source_type, sink_type))


@st.composite
def _record(draw: st.DrawFn, sink: Any, fits: list[str], references: tuple[str, ...],
            referenced_inputs: set[str]) -> CwlRecord:
    """A step-input record: one source with a default, or, into an array, two merged.

    Two sources each fit the array sink, so `merge_flattened` keeps it one.
    """
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
    """One tool step: real stem, real input names, a subset of them bound.

    Leaving a required input unbound is deliberate and load-bearing — it is
    the only way an *inferred* edge exists, which is what edge type-compatibility is about.

    The stem is passed in rather than drawn here. `documents()` has to know
    every stem before any step is built, because mapping form cannot repeat one
    — see its docstring for what drawing them here cost.

    `defined_edges` and `referenced_inputs` are mutated rather than returned:
    an `!*` reference is only well-formed after some `!&` defined the name, and
    a bare name is only well-formed once the document declares it, so both are
    facts about the document being built and not about this step.
    """
    # pylint: disable=too-many-branches,too-many-locals,too-many-statements  # one branch per construct
    names = sorted(inputs_of(stem))

    # Drawn first, because a scattered input consumes an array: a reference
    # must carry one, while a literal may be a scalar Complete wraps. Left
    # unbound, a required one is lifted by Infer or fed an array.
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

    chosen = draw(st.lists(st.sampled_from(names), unique=True, max_size=len(names))) if names else []
    # Infer lifts only a required input, so a scattered one that is not must be bound.
    chosen += [name for name in ports if name not in chosen and name not in required_inputs_of(stem)]
    connectable = [name for name in names
                   if any(may_reference(carries, sink(name)) for _, carries in defined_edges)]
    forced: str | None = None
    if bool(connectable) and draw(st.booleans()):
        forced = draw(st.sampled_from(connectable))
        chosen = chosen if forced in chosen else [*chosen, forced]

    bindings: list[tuple[str, InputValue]] = []
    for name in chosen:
        fits = [edge for edge, carries in defined_edges if may_reference(carries, sink(name))]
        if name == forced:
            bindings.append((name, EdgeRef(draw(st.sampled_from(fits)), _SPAN)))
            continue

        references = _references_for(sink(name))
        forms = ['literal'] + (['unresolved', 'raw'] if references else []) + (['ref'] if fits else []) \
            + (['record'] if references or fits else [])
        match draw(st.sampled_from(forms)):
            case 'literal':
                literal = draw(_literal_for(inputs_of(stem)[name].get('type')))
                if name in ports:
                    literal = [literal]
                bindings.append((name, InlineLiteral(literal, _SPAN)))
            case 'unresolved':
                declared = draw(st.sampled_from(references))
                referenced_inputs.add(declared)
                bindings.append((name, UnresolvedName(declared, _SPAN)))
            case 'raw':
                declared = draw(st.sampled_from(references))
                referenced_inputs.add(declared)
                bindings.append((name, RawCwlRef(declared, _SPAN)))
            case 'record':
                bindings.append((name, draw(_record(sink(name), fits, references, referenced_inputs))))
            case _:
                bindings.append((name, EdgeRef(draw(st.sampled_from(fits)), _SPAN)))

    # `bool(...)` around the left operand, here and at every other `X and
    # draw(...)` site below: mypy's bidirectional inference otherwise uses the
    # left operand's type as the expected-argument context for the generic
    # `DrawFn.__call__` on the right, so `outputs_of(stem) and draw(booleans())`
    # is checked as if `draw` had to return `dict[str, Cwl]`. A `bool()` around
    # a value already used only for truthiness costs nothing at runtime.
    outs: list[OutputBinding] = []
    if bool(outputs_of(stem)) and draw(st.booleans()):
        for out_name in draw(st.lists(st.sampled_from(sorted(outputs_of(stem))),
                                      unique=True, max_size=2)):
            if draw(st.booleans()):
                carries = outputs_of(stem)[out_name].get('type')
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
    """A well-formed Sophios document, over every construct in `CONSTRUCTS`.

    CANNOT GENERATE (declared, and checked): the kinds in `NOT_GENERATED`.
    ``python_script`` steps are absent because their module definition belongs
    to the registry; deterministic generated identities are tested separately.

    Well typed where it binds a literal: `_literal_for` draws one the bound
    input's type admits, so every draw compiles unless `NOT_YET_COMPILABLE`
    excludes it. Ill-typed literals are the provocation registry's.

    Both step surface forms, because mapping form and sequence form were once
    two languages to a generator that only spelled one. Mapping form cannot
    repeat a step name (reference §3.1), so its stems are drawn **unique and up
    front** — that is the language's constraint, not a convenience, and
    generating a document the language forbids would make every property
    downstream quantify over documents that fail before reaching the compiler.

    Up front, and not by drawing a step and discarding it on a collision, which
    costs two things. A discarded step would already have appended its `!&`
    names to `defined_edges` — document-scoped by design — and nothing
    un-appends them, so a later step could draw an `!*`
    reference to an edge no surviving step defines. `compile_hermetic` passes
    `testing=True`, and `compiler.py:784` raises for a dangling edge only when
    `not testing`, so that document did not fail: the compiler added a CWL
    input "for testing only" where the edge should have been, and the corrupted
    document reached every compile-driving property — including the relation, whose
    entire subject is which port is fed from where. The distribution suffered
    too: a mapping-form document asking for four steps routinely got two, so
    `count` did not mean what it said.
    """
    as_mapping = draw(st.booleans())
    stems = draw(st.lists(st.sampled_from(STEMS), min_size=1, max_size=4,
                          unique=as_mapping))
    defined_edges: list[tuple[str, Any]] = []
    referenced_inputs: set[str] = set()

    steps: list[Step] = [draw(_step(stem, defined_edges, referenced_inputs))
                         for stem in stems]

    # A subworkflow step, drawn sometimes. Its id is what makes it one:
    # a subworkflow is recognised by the `.wic` suffix and nothing else.
    # The body lives in `subtree`, which
    # the AST has no field for, so `to_yml` attaches it after rendering.
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

    passthrough: list[tuple[str, OpaqueCwl]] = []
    if referenced_inputs:
        # Not optional: a bare name that nothing declares is `wic011`, so the
        # document must declare exactly what its steps referenced. `inputs` is
        # top-level passthrough as far as the syntax layer is concerned — the
        # compiler reads it (compiler.py:878) but the language does not claim
        # it, which is why it lives here and not in a Document field.
        types = dict(declared_inputs)
        passthrough.append(('inputs', {name: {'type': types[name]}
                                       for name in sorted(referenced_inputs)}))
    if draw(st.booleans()):
        key = draw(st.sampled_from(['label', 'doc', '$schemas']))
        passthrough.append((key, ['https://example/s.owl'] if key == '$schemas'
                            else draw(st.text('abc ', max_size=6))))

    return Document(steps=tuple(steps), sidecar=sidecar,
                    passthrough=tuple(passthrough), span=_SPAN,
                    steps_as_mapping=as_mapping)


#: Constructs the specification admits that the compiler does not accept
#: today. Each entry names the construct, the finding it belongs to, and
#: where the finding lives in `src/sophios/compiler.py`, so an exclusion
#: cannot outlive the defect that justified it.
#:
#: The companion is `test_the_compilable_subset_still_reaches_every_construct_
#: it_does_not_exclude` in `test_generators.py`. It holds the *filter* to the
#: standard construct coverage holds the generator to: a construct `compilable_documents()`
#: stops reaching turns that test red, so an exclusion cannot quietly cost a
#: construct. Deliberately not a per-entry "this still fails to compile" check
#: — that shape is vacuous whenever the mapping is empty, which is exactly when
#: a filter that silently excluded everything would go unnoticed: an exclusion
#: predicate of `lambda d: True` leaves the suite green under it.
#:
#: `documents()` keeps producing all of these — `CONSTRUCTS` and the
#: parse-level properties quantify over the whole language, and trimming the
#: generator to dodge a compiler gap is the narrowing the binding constraints
#: forbid. `compilable_documents()` is the subset with these filtered out,
#: for properties that need their input to actually compile. Keys
#: name the exclusion, not a `CONSTRUCTS` row: an exclusion is an AST *shape*
#: narrower than any single construct.
NOT_YET_COMPILABLE: Final[dict[str, str]] = {}


#: One predicate per `NOT_YET_COMPILABLE` entry, keyed identically. Separate
#: from `NOT_YET_COMPILABLE` itself (a plain name-to-reason mapping, so the
#: reason reads as documentation and not as code) rather than folded into one
#: dict of `(reason, predicate)` pairs; the assertion below is what keeps the
#: two from drifting apart, the same discipline `Tag.ALL`/`Key.ALL` in
#: `utils_yaml.py` uses for the analogous problem.
_EXCLUSION_PREDICATES: Final[dict[str, Callable[[Document], bool]]] = {}
assert NOT_YET_COMPILABLE.keys() == _EXCLUSION_PREDICATES.keys(), (
    'NOT_YET_COMPILABLE and _EXCLUSION_PREDICATES must name exactly the same exclusions')


def compilable_documents() -> SearchStrategy[Document]:
    """`documents()`, minus the constructs `NOT_YET_COMPILABLE` names.

    A property comparing two compilations cannot use a document that does not
    compile, so the compile-driving properties quantify over this. The
    parse-level ones keep `documents()` — the whole language, unfiltered.

    The strategy the compile-driving properties need: partition independence and the other
    compile-driving properties cannot compare two compilations of a document
    that does not compile, so they quantify over this, not over `documents()`
    itself. `CONSTRUCTS` and the parse-level properties still use
    `documents()` — the whole language, unfiltered — so this function's
    narrowing is not the narrowing the binding constraints forbid; it is the
    generator drawing a line between "the language" and "what those properties
    can use today", with that line named and tested rather than silent.
    """
    # pylint: disable=no-member  # see workflows()'s identical disable, below
    return documents().filter(lambda d: not any(pred(d) for pred in _EXCLUSION_PREDICATES.values()))


def excluded_documents(name: str) -> SearchStrategy[Document]:
    """Documents that trip the named `NOT_YET_COMPILABLE` exclusion.

    The other half of `compilable_documents()`'s filter — this module's own
    non-vacuity check needs documents *matching* an exclusion's predicate to
    prove the predicate's excuse is still true, the same way `hostile_documents`
    needs documents outside the language to prove a diagnostic still fires.
    """
    # pylint: disable=no-member  # see workflows()'s identical disable, below
    return documents().filter(_EXCLUSION_PREDICATES[name])


#: The bodies a subworkflow step's id selects, by its stem less the index
#: `documents()` appends. Each literal inside one is a child job value that
#: Complete lifts to the caller: a string, a File written bare and as an
#: object, a Directory. `decl` declares an input, so its caller binds it
#: and may scatter over it.
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
    """The compiler's input for a generated document.

    Round-tripped through source rather than constructed directly: the compiler
    reads `.wic` files, so anything a property proves about a dict this module
    built by hand would be a claim about a path no user takes. This is also what
    makes a counterexample printable — `render(document)` is the file to paste
    into a bug report.

    Stands in for `sophios.ast.read_ast_from_disk`, which is the only real pass
    this oracle cannot run — it reads the disk. That function's first line is
    `utils_cwl.desugar_into_canonical_normal_form`, which is why this function
    calls it too: mapping-form `steps:` reaches the compiler as a dict, and
    the compiler's step lowering (`step.interpreted.get('run')`)
    indexes it as a list, so a mapping-form document that skipped this call
    raised `KeyError: 0` before reaching anything interesting. Do not remove
    this call to "simplify" `to_yml` — a mapping-form document depends on it to
    compile at all.

    Subworkflow bodies are attached here rather than generated into the AST.
    `Step` has no subtree field, because in the language a subworkflow's body
    lives in another file; `read_ast_from_disk` is what puts it inline. This
    runs after desugaring so the attachment loop sees canonical list-form steps
    regardless of which surface form the document used.
    """
    loaded: Yaml = utils_cwl.desugar_into_canonical_normal_form(
        yaml.load(render(document), Loader=wic_loader()))
    for step in loaded.get('steps', []):
        if isinstance(step, dict) and str(step.get('id', '')).endswith('.wic'):
            body = str(step['id']).removesuffix('.wic').rstrip('0123456789')
            step['subtree'] = copy.deepcopy(SUBWORKFLOW_BODIES[body])
            step['parentargs'] = {}
    return loaded


def workflows() -> SearchStrategy[Yaml]:
    """The strategy the compile-driving properties quantify over:
    `compilable_documents()` rendered through `to_yml`."""
    return compilable_documents().map(to_yml)


_SCATTERABLE_STRING_INPUTS: Final[tuple[tuple[str, str], ...]] = (
    ('mk_file', 'name'), ('mk_text', 'name'), ('xform', 'name'), ('join', 'name'),
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

    passthrough: list[tuple[str, OpaqueCwl]] = []
    if referenced_inputs:
        types = dict(declared_inputs)
        passthrough.append(('inputs', {name: {'type': types[name]}
                                       for name in sorted(referenced_inputs)}))
    return Document(steps=tuple(steps), passthrough=tuple(passthrough), span=_SPAN), scatter_at


def to_yml_with_freight(document: Document, step_index: int, freight: dict[str, Any]) -> Yaml:
    """Render ``document`` and add passthrough ``freight`` to its designated step."""
    loaded = to_yml(document)
    steps: list[Yaml] = loaded['steps']
    steps[step_index] = {**steps[step_index], **freight}
    return loaded


@st.composite
def partitionings(draw: st.DrawFn, steps: int) -> tuple[tuple[int, ...], ...]:
    """A contiguous grouping of `range(steps)`.

    Contiguous because that is what a subworkflow is: a step list splits at a
    `.wic` id, and the steps a subworkflow contains are the ones
    written inside its file. A non-contiguous 'partitioning' is not expressible
    in the language, so generating one would test nothing.
    """
    if steps <= 1:
        return ((0,),) if steps else ()
    cuts = draw(st.lists(st.integers(min_value=1, max_value=steps - 1),
                         unique=True, max_size=steps - 1)).copy()
    cuts.sort()
    bounds = [0, *cuts, steps]
    return tuple(tuple(range(a, b)) for a, b in zip(bounds, bounds[1:]))


#: Ill-formed source paired with the code it must provoke. Pairs, not bare
#: strings: "some diagnostic fired" is satisfied by the wrong diagnostic, and a
#: generator whose every output produces `wic001` would look green while saying
#: nothing about the eleven other codes.
_HOSTILE: Final = (
    ('steps:\n  s:\n    in:\n      f: !ii a\n      f: !ii b\n', SophiosErrorCode.DUPLICATE_KEY),
    ('steps:\n- id: s\n  in:\n    f: !foo bar\n', SophiosErrorCode.UNKNOWN_TAG),
    ('steps:\n- id: ""\n', SophiosErrorCode.EMPTY_STEP_ID),
    # A sequence step carries its name in `id:` (reference §3.1), so an entry
    # without one earns MISSING_STEP_ID whatever else it has — here, nothing.
    ('steps:\n- {}\n', SophiosErrorCode.MISSING_STEP_ID),
    ('steps:\n- id: s\n  out: {a: b}\n', SophiosErrorCode.EXPECTED_SEQUENCE),
    ('steps: 3\n', SophiosErrorCode.EXPECTED_MAPPING),
    # A step's own `wic:` key is passthrough, not a sidecar (`_step_body` has
    # no case for it) — the malformed key must live in the document-level
    # `wic: steps:` mapping, which is the only place that string is parsed.
    ('wic:\n  steps:\n    "not a key": {}\n', SophiosErrorCode.MALFORMED_WIC_STEP_KEY),
    ('steps: &a\n- id: s\n  wic: {x: *a}\n', SophiosErrorCode.RECURSIVE_ALIAS),
)


def hostile_documents() -> SearchStrategy[tuple[str, SophiosErrorCode]]:
    """Documents outside the language, each with the code it must earn."""
    return st.sampled_from(_HOSTILE)


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

#: A `hints:` value as the parser keeps it. Not free-form freight like the above:
#: CWL's list form of `hints:` is read as this mapping from requirement class to
#: body, so a list survives byte-identically only when it holds an `$import` or
#: `$include`, which `test_leak_boundary` pins by name.
hints_values: Final = st.dictionaries(
    st.sampled_from(['DockerRequirement', 'ResourceRequirement', 'NetworkAccess', 'cwltool:CUDARequirement']),
    st.dictionaries(st.text('abc', min_size=1, max_size=5), passthrough_values, max_size=3),
    max_size=3,
)
