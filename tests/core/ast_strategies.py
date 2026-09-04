"""Strategies over the Spec 1 AST, and their compilable projection.

Documents are built as `sophios.lang.nodes` values and then *rendered* to
source, rather than assembled as dicts. Three reasons, in order of weight:

  1. The space the properties quantify over is then the space the types admit,
     by construction, rather than the space a hand-written dict builder
     happened to imagine. CE-09 was found the first time a generator was
     derived from a type instead of written beside one.
  2. Hypothesis shrinks the AST, so a counterexample arrives as a minimal
     document rather than a minimal string (P27).
  3. `render` is the writer under test in Spec 1, so every compile-driving
     property is also, incidentally, a second consumer of it.

The projection goes through the compiler's own loader — `render` then
`yaml.load(..., Loader=wic_loader())` — and not through some third path, so a
disagreement between the two front ends is a test failure rather than a
difference the suite is blind to (CE-02).

Step ids name stems in `synthetic_tools`, and input bindings name that tool's
real inputs, so a generated document is a workflow the compiler can actually
resolve rather than a document that fails before reaching anything interesting.

CANNOT GENERATE (declared, per the negative-testing rules): `!cwl` (RawCwlRef)
— specified but not compilable until the Spec 3 migration, since `wic_loader`
does not know the tag; `python_script` steps — named with a uuid4, so nothing
over them is deterministic.

CE-13 (specification/implementation divergence, confirmed): an `!& name` edge
definition bound to a step *input* (the `edge_def` row of `CONSTRUCTS`,
distinct from `output_edge`) is documented as one of the five input forms
(language reference §4.1, with no not-yet-usable caveat like `!cwl`'s) but has
no handler in `compile_workflow_once`'s `in:` match statement
(`src/sophios/compiler.py:~780` — cases exist for `wic_alias` at 781 and
`wic_inline_input` at 896, none for `wic_anchor`, which is recognised only in
the `out:` walk at ~729). Every step input bound this way falls through to the
bare-string case, is unhashable as a dict, and always raises
`Code.UNRESOLVED_INPUT` (`wic011`), regardless of what `inputs:` declares.
Confirmed by construct-correlated measurement (200 documents, derandomized):
100% of `wic011` failures had an input-position `EdgeDef` and 0% of
non-failing documents did. `documents()` keeps generating it anyway —
`CONSTRUCTS` requires `edge_def` to appear (P26), and trimming the generator to
dodge a compiler gap is exactly the narrowing the binding constraints forbid.
`compilable_documents()` filters it out instead, via `NOT_YET_COMPILABLE`
below, with a companion test that keeps the filter honest.

PENDING FINDING (reported, not yet assigned a number): `!ii` places no
constraint relating a literal's value to the CWL type of the input it binds —
nothing in the grammar could, since that is a downstream compiler concern —
so a document binding e.g. the bare string `'0x1f'` or `'_'` to an `int`- or
`float`-typed input (`sink.n`, `scale.n`, `scale.factor` among the synthetic
stems) is well-formed. `generate_yaml_inputs`'s `populate_scalar_val`
(`src/sophios/compiler.py:1170` for `int`, `:1173` for `float`) calls
`int(value)` / `float(value)` with no `try`/`except` and no `SophiosError`, so
compilation crashes with a bare Python `ValueError` instead of a diagnostic —
a totality violation (the claim plan Task 6's P33 makes), not a generator
defect. Not excluded from `compilable_documents()`: unlike CE-13 it is not a
single AST-shape predicate (it depends on which literal value landed on which
typed argument), and the measured residual is small enough that the bulk of
`compilable_documents()` still compiles (see the Task 2 report). `documents()`
and `_step` are unchanged for this reason on purpose — narrowing `literals`
to dodge it would be the same move CE-13 already forbids, just aimed at a
different finding.
"""
from typing import Callable, Final

import yaml
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sophios import utils_cwl
from sophios.lang import (Code, Document, EdgeDef, EdgeRef, InlineLiteral, InputValue, OpaqueCwl,
                          OutputBinding, Step, StepKey, UnresolvedName, WicSidecar, render)
from sophios.lang.spans import SourceSpan
from sophios.utils_yaml import wic_loader
from sophios.wic_types import Yaml

from .synthetic_tools import STEMS, inputs_of, outputs_of, required_inputs_of

#: A span the AST needs and the surface never shows. Generated nodes have no
#: source, so they all carry the same one; nothing downstream reads it, and a
#: property about spans belongs to Spec 1 where real positions exist.
_SPAN: Final = SourceSpan('<generated>', 1, 1, 1, 1)

#: The construct kinds P26 enumerates. Derived from the reference's tables
#: (§3.1 surface forms, §3.3 outputs, §4.1 input forms, §4.3 interpreted keys,
#: §5 the sidecar) rather than from the strategy below, so a construct the
#: strategy stops producing is a failure instead of a silent narrowing.
CONSTRUCTS: Final[tuple[str, ...]] = (
    'steps_mapping', 'steps_sequence',
    'inline_literal', 'edge_def', 'edge_ref', 'unresolved_name',
    'output_bare', 'output_edge',
    'interpreted_scatter', 'interpreted_when',
    'step_passthrough', 'top_passthrough',
    'sidecar', 'sidecar_steps',
    'subworkflow', 'inferred_input',
)


def constructs_in(document: Document) -> frozenset[str]:
    """Which construct kinds a document contains. One place, so P26 and the
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
                case EdgeDef():
                    found.add('edge_def')
                case EdgeRef():
                    found.add('edge_ref')
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
edge_names: Final = st.text('abcdefgh', min_size=1, max_size=4)


def _fresh_edge(draw: st.DrawFn, defined_edges: list[str]) -> str:
    """Draws a `!&` name not already defined in this document.

    `edge_names` is a small bounded alphabet, drawn independently at each
    definition site; two steps landing on the same string would both try to
    `!&` it, and the compiler treats that as `ValueError: Multiple definitions
    of &name!`, not a workflow. Suffixing with how many edges the document has
    already defined makes every name unique — `defined_edges` is document-
    scoped (passed into every `_step` call for the document being built) —
    without touching what `edge_names` itself produces or how `EdgeRef` draws
    from `defined_edges`.
    """
    name = f'{draw(edge_names)}{len(defined_edges)}'
    defined_edges.append(name)
    return name


#: Workflow-level input names a bare string may resolve to. A bare name is an
#: `UnresolvedName`, and it compiles only when the top-level `inputs:` mapping
#: has that key — `arg_var_is_input` at src/sophios/compiler.py:878; otherwise
#: it is `wic011` with advice to write `!ii`. So the declaration and the
#: reference are drawn from one list, and `documents()` declares whatever any
#: step referenced. Without this the strategy could not produce an
#: `UnresolvedName` at all, and the `unresolved_name` row of CONSTRUCTS would
#: be a construct P26 demands and nothing supplies.
declared_inputs: Final = ('wf_name', 'wf_count')


@st.composite
def _step(draw: st.DrawFn, defined_edges: list[str], referenced_inputs: set[str]) -> Step:
    """One tool step: real stem, real input names, a subset of them bound.

    Leaving a required input unbound is deliberate and load-bearing — it is
    the only way an *inferred* edge exists, which is what P32 is about.

    `defined_edges` and `referenced_inputs` are mutated rather than returned:
    an `!*` reference is only well-formed after some `!&` defined the name, and
    a bare name is only well-formed once the document declares it, so both are
    facts about the document being built and not about this step.
    """
    # pylint: disable=too-many-branches  # one branch per input/output construct
    stem = draw(st.sampled_from(STEMS))
    names = sorted(inputs_of(stem))
    chosen = draw(st.lists(st.sampled_from(names), unique=True, max_size=len(names))) if names else []
    bindings: list[tuple[str, InputValue]] = []
    for name in chosen:
        forms = ['literal', 'def', 'unresolved'] + (['ref'] if defined_edges else [])
        match draw(st.sampled_from(forms)):
            case 'literal':
                bindings.append((name, InlineLiteral(draw(literals), _SPAN)))
            case 'def':
                edge = _fresh_edge(draw, defined_edges)
                bindings.append((name, EdgeDef(edge, _SPAN)))
            case 'unresolved':
                declared = draw(st.sampled_from(declared_inputs))
                referenced_inputs.add(declared)
                bindings.append((name, UnresolvedName(declared, _SPAN)))
            case _:
                bindings.append((name, EdgeRef(draw(st.sampled_from(defined_edges)), _SPAN)))

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
                edge = _fresh_edge(draw, defined_edges)
                outs.append(OutputBinding(out_name, EdgeDef(edge, _SPAN), _SPAN))
            else:
                outs.append(OutputBinding(out_name, None, _SPAN))

    interpreted: list[tuple[str, OpaqueCwl]] = []
    if bool(bindings) and draw(st.booleans()):
        match draw(st.sampled_from(['scatter', 'when'])):
            case 'scatter':
                interpreted.append(('scatter', [bindings[0][0]]))
            case _:
                interpreted.append(('when', '$(true)'))

    passthrough: list[tuple[str, OpaqueCwl]] = []
    if draw(st.booleans()):
        passthrough.append((draw(st.sampled_from(['label', 'doc'])), draw(st.text('abc ', max_size=6))))

    return Step(id=stem, inputs=tuple(bindings), outputs=tuple(outs),
                interpreted=tuple(interpreted), passthrough=tuple(passthrough), span=_SPAN)


@st.composite
def documents(draw: st.DrawFn) -> Document:
    """A well-formed, compilable Sophios document.

    Both step surface forms, because mapping form and sequence form were once
    two languages to a generator that only spelled one. Mapping form cannot
    repeat a step name (reference §3.1), so its stems are drawn unique — that
    is the language's constraint, not a convenience, and generating a document
    the language forbids would make every property downstream quantify over
    documents that fail before reaching the compiler.
    """
    as_mapping = draw(st.booleans())
    count = draw(st.integers(min_value=1, max_value=4))
    defined_edges: list[str] = []
    referenced_inputs: set[str] = set()

    steps: list[Step] = []
    used: set[str] = set()
    for _ in range(count):
        step = draw(_step(defined_edges, referenced_inputs))
        if as_mapping:
            # A repeated key is `wic010`, not a document. Skip rather than
            # filter: filtering a composite this deep hits filter_too_much.
            if step.id in used:
                continue
            used.add(step.id)
        steps.append(step)

    # A subworkflow step, drawn sometimes. Its id is what makes it one:
    # `get_subkeys` recognises a subworkflow by the `.wic` suffix and nothing
    # else (src/sophios/utils.py:145-155). The body lives in `subtree`, which
    # the AST has no field for, so `to_yml` attaches it after rendering.
    if bool(steps) and draw(st.booleans()):
        steps.append(Step(id=f'sub{len(steps)}.wic', span=_SPAN))

    sidecar = None
    if draw(st.booleans()):
        entries: tuple[tuple[str, OpaqueCwl], ...] = (('graphviz', {'label': draw(edge_names)}),)
        nested: tuple[tuple[StepKey, WicSidecar], ...] = ()
        if bool(steps) and draw(st.booleans()):
            nested = ((StepKey(1, steps[0].id),
                       WicSidecar(entries=(('label', draw(edge_names)),), span=_SPAN)),)
        sidecar = WicSidecar(steps=nested, entries=entries, span=_SPAN)

    passthrough: list[tuple[str, OpaqueCwl]] = []
    if referenced_inputs:
        # Not optional: a bare name that nothing declares is `wic011`, so the
        # document must declare exactly what its steps referenced. `inputs` is
        # top-level passthrough as far as the syntax layer is concerned — the
        # compiler reads it (compiler.py:878) but the language does not claim
        # it, which is why it lives here and not in a Document field.
        passthrough.append(('inputs', {name: {'type': 'string'}
                                       for name in sorted(referenced_inputs)}))
    if draw(st.booleans()):
        key = draw(st.sampled_from(['label', 'doc', '$schemas']))
        passthrough.append((key, ['https://example/s.owl'] if key == '$schemas'
                            else draw(st.text('abc ', max_size=6))))

    return Document(steps=tuple(steps), sidecar=sidecar,
                    passthrough=tuple(passthrough), span=_SPAN,
                    steps_as_mapping=as_mapping)


def _binds_edge_def_as_input(document: Document) -> bool:
    """CE-13: does any step bind `!& name` (an `EdgeDef`) to an *input*.

    The one AST-shape predicate `NOT_YET_COMPILABLE['edge_def_in_input']`
    names — kept as its own function, rather than inlined into a lambda,
    so `excluded_documents` can filter *for* it (the exclusion's own
    contract test) as well as `compilable_documents` filtering it *out*.
    """
    return any(isinstance(value, EdgeDef) for step in document.steps for _, value in step.inputs)


#: Constructs the specification admits that the compiler does not accept
#: today. Each entry names the construct, the finding it belongs to, and
#: where the finding lives in `src/sophios/compiler.py`, so an exclusion
#: cannot outlive the defect that justified it — `test_generators.py` has a
#: companion asserting every one of these still genuinely fails to compile;
#: the day a fix lands, that test goes red and whoever is standing there
#: removes the entry instead of it living on as a permanent blind spot.
#:
#: `documents()` keeps producing all of these — `CONSTRUCTS` and the
#: parse-level properties quantify over the whole language, and trimming the
#: generator to dodge a compiler gap is the narrowing the binding constraints
#: forbid. `compilable_documents()` is the subset with these filtered out,
#: for properties (Tasks 3-7) that need their input to actually compile.
NOT_YET_COMPILABLE: Final[dict[str, str]] = {
    'edge_def_in_input': ('CE-13 — an `!& name` edge definition bound to a step input has no case in '
                          "compile_workflow_once's `in:` match statement (compiler.py:~780; `wic_anchor` "
                          'is only recognised in the `out:` walk at ~729), so it always raises '
                          '`Code.UNRESOLVED_INPUT` (wic011) regardless of what `inputs:` declares.'),
}

#: One predicate per `NOT_YET_COMPILABLE` entry, keyed identically. Separate
#: from `NOT_YET_COMPILABLE` itself (a plain name-to-reason mapping, so the
#: reason reads as documentation and not as code) rather than folded into one
#: dict of `(reason, predicate)` pairs; the assertion below is what keeps the
#: two from drifting apart, the same discipline `Tag.ALL`/`Key.ALL` in
#: `utils_yaml.py` uses for the analogous problem.
_EXCLUSION_PREDICATES: Final[dict[str, Callable[[Document], bool]]] = {
    'edge_def_in_input': _binds_edge_def_as_input,
}
assert NOT_YET_COMPILABLE.keys() == _EXCLUSION_PREDICATES.keys(), (
    'NOT_YET_COMPILABLE and _EXCLUSION_PREDICATES must name exactly the same exclusions')


def compilable_documents() -> SearchStrategy[Document]:
    """`documents()`, minus the constructs `NOT_YET_COMPILABLE` names.

    The strategy Tasks 3-7 need: partition independence and the other
    compile-driving properties cannot compare two compilations of a document
    that does not compile, so they quantify over this, not over `documents()`
    itself. `CONSTRUCTS`/P26 and the parse-level properties still use
    `documents()` — the whole language, unfiltered — so this function's
    narrowing is not the narrowing the binding constraints forbid; it is the
    generator drawing a line between "the language" and "what Tasks 3-7 can
    use today", with that line named and tested rather than silent.
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
    `_compile_workflow`'s per-step loop (`setup.steps[i].get('run', '')`)
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
            step['subtree'] = {'steps': [{'id': 'mk_file',
                                          'in': {'name': {'wic_inline_input': 'sub.txt'}}}]}
            step['parentargs'] = {'id': step['id']}
    return loaded


def workflows() -> SearchStrategy[Yaml]:
    """The strategy Tasks 3-7 quantify over: `compilable_documents()` mapped
    through `to_yml`, not `documents()` itself. Those properties compile their
    input (partition independence compares two compilations; it cannot do
    that with a document that does not compile once), so this excludes
    exactly what `compilable_documents()` excludes — see `NOT_YET_COMPILABLE`
    for the current list and why each entry is there."""
    return compilable_documents().map(to_yml)


@st.composite
def partitionings(draw: st.DrawFn, steps: int) -> tuple[tuple[int, ...], ...]:
    """A contiguous grouping of `range(steps)`.

    Contiguous because that is what a subworkflow is: `get_subkeys` splits a
    step list at a `.wic` id, and the steps a subworkflow contains are the ones
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
    ('steps:\n  s:\n    in:\n      f: !ii a\n      f: !ii b\n', Code.DUPLICATE_KEY),
    ('steps:\n- id: s\n  in:\n    f: !foo bar\n', Code.UNKNOWN_TAG),
    ('steps:\n- id: ""\n', Code.EMPTY_STEP_ID),
    # A single-key sequence step takes its key as the id (reference §3.1: the
    # `- touch: {...}` shorthand); a step with no `id:` and no `out:` either
    # earns MISSING_STEP_ID only once it has more than one key, so nothing
    # picks it as the name.
    ('steps:\n- {}\n', Code.MISSING_STEP_ID),
    ('steps:\n- id: s\n  out: {a: b}\n', Code.EXPECTED_SEQUENCE),
    ('steps: 3\n', Code.EXPECTED_MAPPING),
    # A step's own `wic:` key is passthrough, not a sidecar (`_step_body` has
    # no case for it) — the malformed key must live in the document-level
    # `wic: steps:` mapping, which is the only place that string is parsed.
    ('wic:\n  steps:\n    "not a key": {}\n', Code.MALFORMED_WIC_STEP_KEY),
    ('steps: &a\n- id: s\n  wic: {x: *a}\n', Code.RECURSIVE_ALIAS),
)


def hostile_documents() -> SearchStrategy[tuple[str, Code]]:
    """Documents outside the language, each with the code it must earn."""
    return st.sampled_from(_HOSTILE)
