"""Every document the compiler manufactures is one the language accepts.

The corpus is not the interesting input: documents a *user* wrote already have
a parse property. What nothing checked is the documents the compiler *makes* —
the Python API's output, the inliner's intermediates, the tree after `wic:`
metadata is merged onto a step, the documents `rerun_cwltool` builds. Those
never pass through `sophios.lang`, so the grammar has never had an opinion
about them, and three defects in a row lived exactly there: step ids spelled
from the wrong stem, a producer still emitting a step form the grammar had
removed, and scatter readable from two places with no owner.

`MANUFACTURING_SITES` pins every site; a static scan fails when an unlisted one
appears. Sites no driver reaches are named in `UNREACHED` rather than ignored.
"""
import ast as pyast
import importlib
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from sophios.input_output import NoAliasDumper
from sophios.lang.parser import parse

from hypothesis import given

from sophios.api.python.workflow import Step, Workflow

from . import ast_strategies as strat
from .hermetic import ORACLE, compile_hermetic_cwl

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SOURCE: Final = REPO_ROOT / 'src'

#: Keys that make an object document-shaped rather than merely a mapping.
DOCUMENT_KEYS: Final = frozenset({'steps', 'wic'})

#: What each site builds. Only `DOCUMENT` is a `.wic` document the language owns.
DOCUMENT: Final = 'DOCUMENT'
SCHEMA: Final = 'SCHEMA'          # JSON Schema, a description of documents
RENDERER: Final = 'RENDERER'      # sophios.lang's own writer, pinned separately
TOOL: Final = 'TOOL'              # a CWL tool, which has `steps` only incidentally
CWL: Final = 'CWL'                # compiled CWL workflow, not Sophios source
CONTRIB: Final = 'CONTRIB'        # outside the core zone

#: Every place in `src/sophios` that builds or rewrites a document-shaped object,
#: found by the scan below and classified by hand. Adding a site to the code
#: without adding it here fails `test_the_manufacturing_inventory_is_complete`;
#: that is the whole point, because a new manufacturer is a new way to emit
#: something the language does not accept.
MANUFACTURING_SITES: Final[dict[str, str]] = {
    'sophios/api/python/_workflow_runtime.py::workflow_document': DOCUMENT,
    'sophios/ast.py::_tree_to_forest': DOCUMENT,
    'sophios/ast.py::merge_yml_trees': DOCUMENT,
    'sophios/ast.py::python_script_generate_cwl': DOCUMENT,
    'sophios/ast.py::read_ast_from_disk': DOCUMENT,
    'sophios/compiler.py::_prepare_compilation_state': DOCUMENT,
    'sophios/compiler.py::compile_workflow_once': DOCUMENT,
    'sophios/compiler.py::insert_step_into_workflow': DOCUMENT,
    'sophios/cwl_subinterpreter.py::rerun_cwltool': DOCUMENT,
    'sophios/inlineing.py::get_inlineable_subworkflows': DOCUMENT,
    'sophios/inlineing.py::inline_subworkflow': DOCUMENT,
    'sophios/inlineing.py::inline_subworkflow_wic_tag': DOCUMENT,
    'sophios/ir/pipeline.py::legacy_after_lower': DOCUMENT,
    'sophios/ir/pipeline.py::legacy_after_link': DOCUMENT,
    'sophios/ir/pipeline.py::legacy_after_infer': DOCUMENT,
    'sophios/main.py::_load_and_prepare_yaml_tree': DOCUMENT,
    'sophios/utils.py::extract_implementation': DOCUMENT,
    'sophios/utils.py::flatten_forest': DOCUMENT,
    # Not documents.
    'sophios/ir/emit.py::emit': CWL,
    'sophios/legacy_graph.py::legacy_emit': CWL,
    'sophios/lang/render.py::_Writer.document': RENDERER,
    'sophios/lang/render.py::_Writer.sidecar': RENDERER,
    'sophios/lang/schema.py::_defs': SCHEMA,
    'sophios/schemas/wic_schema.py::_wic_tag_schema': SCHEMA,
    'sophios/schemas/wic_schema.py::compile_workflow_generate_schema': SCHEMA,
    'sophios/schemas/wic_schema.py::wic_main_schema': SCHEMA,
    'sophios/utils_cwl.py::desugar_into_canonical_normal_form': TOOL,
    'sophios/contrib/converter.py::wfb_to_wic': CONTRIB,
    'sophios/contrib/rest/api.py::compile_wf': CONTRIB,
}

#: Instrumented sites no driver below reaches, and why. Each is evidence not
#: gathered, so each needs a reason that can be checked rather than a shrug.
UNREACHED: Final[dict[str, str]] = {
    'sophios/ast.py::read_ast_from_disk': 'needs a .wic on disk plus a discovered tool registry',
    'sophios/ast.py::merge_yml_trees': 'on the file-loading path, which the in-memory drivers skip',
    'sophios/main.py::_load_and_prepare_yaml_tree': 'the CLI entry point, same file-loading path',
    'sophios/ast.py::python_script_generate_cwl': 'only reached by a `python_script` step',
    'sophios/cwl_subinterpreter.py::rerun_cwltool': 'shells out to a CWL runner; its documents are '
    'pinned directly by test_compiler.py',
    'sophios/inlineing.py::inline_subworkflow_wic_tag': 'reached only through the CLI inlining flags',
    'sophios/inlineing.py::inline_subworkflow': 'the inliner runs between loading and compiling, on '
                                                'the CLI path these drivers do not take',
    'sophios/inlineing.py::get_inlineable_subworkflows': 'same path as inline_subworkflow',
    'sophios/ast.py::_tree_to_forest': 'reached by the CLI post-compile path, not by compile_workflow',
    'sophios/utils.py::flatten_forest': 'same post-compile path as _tree_to_forest',
    'sophios/compiler.py::insert_step_into_workflow': 'speculative insertion fires only when the tool '
    'registry offers an insertable step, and the '
    'synthetic registry offers none',
    'sophios/ir/pipeline.py::legacy_after_lower': 'temporary differential bridge exercised by the '
    'typed Resolve property, not these legacy drivers',
    'sophios/ir/pipeline.py::legacy_after_link': 'temporary differential bridge exercised by the '
    'typed Link property, not these legacy drivers',
    'sophios/ir/pipeline.py::legacy_after_infer': 'temporary differential bridge exercised by the '
    'typed Infer property, not these legacy drivers',
}


def _qualified(node: pyast.AST, parents: dict[pyast.AST, pyast.AST]) -> str:
    """The dotted name of the function or class enclosing `node`."""
    names: list[str] = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef)):
            names.append(current.name)
        current = parents.get(current)
    return '.'.join(reversed(names)) or '<module>'


def _scan_for_sites() -> dict[str, list[int]]:
    """Every function that builds a document-shaped literal or writes a document key.

    Two syntactic shapes, which is what a scan can see: a dict literal carrying
    `steps` or `wic`, and an assignment into one of those keys. A site built
    some other way — a comprehension, or a helper handed the key as a variable —
    is invisible here, which is why the classification above is by hand and why
    this scan is a floor rather than a proof.

    Known to be below the floor, from reading the call graph: helper
    indirection (`inference.perform_edge_inference` reaches a step's `in:`
    through two more calls into `utils_cwl`), `maybe_add_requirements`, which
    writes a key none of these markers name, and every `mergedeep.merge` whose
    result set is decided at runtime. Those manufacture *into* documents the
    instrumented sites already hand over, so the parse claim still covers them
    — what this scan cannot promise is that a brand-new site built that way
    announces itself.
    """
    found: dict[str, list[int]] = {}
    for path in sorted(SOURCE.rglob('sophios/**/*.py')):
        source = path.read_text(encoding='utf-8')
        tree = pyast.parse(source, str(path))
        parents = {child: node for node in pyast.walk(tree) for child in pyast.iter_child_nodes(node)}
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Dict):
                keys = {k.value for k in node.keys
                        if isinstance(k, pyast.Constant) and isinstance(k.value, str)}
                hit = bool(keys & DOCUMENT_KEYS)
            elif isinstance(node, pyast.Assign):
                hit = any(isinstance(t, pyast.Subscript) and isinstance(t.slice, pyast.Constant)
                          and t.slice.value in DOCUMENT_KEYS for t in node.targets)
            else:
                continue
            if hit:
                key = f'{path.relative_to(SOURCE).as_posix()}::{_qualified(node, parents)}'
                found.setdefault(key, []).append(node.lineno)
    return found


def _documents_in(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Every document-shaped mapping reachable from `value`.

    Bounded depth: a compiled tree is deep and self-referential in places, and
    an unbounded walk over one is a way to hang a test rather than to fail it.
    """
    if depth > 6:
        return []
    if isinstance(value, dict) and 'steps' in value:
        return [value]
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for item in value.values():
            found += _documents_in(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found += _documents_in(item, depth + 1)
    elif hasattr(value, 'yml'):          # YamlTree and the node types wrapping one
        found += _documents_in(getattr(value, 'yml'), depth + 1)
    return found


def _instrument(monkeypatch: pytest.MonkeyPatch, site: str,
                seen: list[tuple[str, dict[str, Any]]]) -> None:
    """Record every document-shaped object that passes through one site.

    Arguments are recorded as well as return values: several of these sites
    rewrite a document in place and return nothing, and the rewritten document
    is the thing worth checking.
    """
    module_path, _, qualname = site.partition('::')
    module = importlib.import_module(module_path[:-len('.py')].replace('/', '.'))
    holder: Any = module
    *outer, name = qualname.split('.')
    for attribute in outer:
        holder = getattr(holder, attribute)
    original = getattr(holder, name)

    def recording(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        for candidate in (result, *args, *kwargs.values()):
            for document in _documents_in(candidate):
                seen.append((site, document))
        return result

    monkeypatch.setattr(holder, name, recording)


def _parses(document: dict[str, Any]) -> tuple[bool, list[str]]:
    """Whether `sophios.lang` accepts this document, and what it said if not.

    Dumping and re-parsing is a fair test rather than a round-trip through a
    lossy form, because the tags desugar on load -- `!& e` becomes
    `{'wic_anchor': 'e'}` -- so a manufactured document is already in the
    desugared spelling, which the language reference (§6.1) says the parser
    accepts equally.

    The desugared spelling carries no tags, but every construct position is
    closed, so a manufactured mistake cannot pass silently. In input position a
    single-key mapping such as `{'wic_not_a_thing': 1}` is reported as a
    misspelled construct and a correctly spelled `wic_anchor` as a misplaced
    one; in `out:`, a mapping value must be an edge definition and every other
    shape is reported. A `wic_`-prefixed key anywhere else stays ordinary
    passthrough by design -- §1 makes that vocabulary open -- and is not read
    as a failed construct.
    """
    text = yaml.dump(document, sort_keys=False, line_break='\n', indent=2, Dumper=NoAliasDumper)
    result = parse(text, 'manufactured.wic')
    return result.ok, [f'{d.code} at {d.span.start_line}:{d.span.start_column}'
                       for d in result.diagnostics if d.span]


@pytest.mark.fast
def test_the_manufacturing_inventory_is_complete() -> None:
    """The scan finds exactly the sites listed, and every one is classified.

    A new manufacturer is a new way to emit something the language does not
    accept, so it has to be looked at rather than absorbed. The scan sees two
    syntactic shapes and no more — its job is to make *adding* a site loud, not
    to prove none was missed a different way.
    """
    found = set(_scan_for_sites())
    pinned = set(MANUFACTURING_SITES)

    assert found - pinned == set(), (
        'these build or rewrite a document and are not in MANUFACTURING_SITES:\n  '
        + '\n  '.join(sorted(found - pinned))
        + '\nClassify each one. If it makes a .wic document, mark it DOCUMENT so it is checked.')
    assert pinned - found == set(), (
        'these are listed but the scan no longer finds them — deleted or rewritten:\n  '
        + '\n  '.join(sorted(pinned - found)))
    assert set(UNREACHED) <= {s for s, kind in MANUFACTURING_SITES.items() if kind == DOCUMENT}, (
        'UNREACHED names a site that is not an instrumented DOCUMENT site')


@pytest.mark.fast
def test_every_manufactured_document_is_one_the_language_accepts() -> None:
    """Whatever the compiler makes, `sophios.lang` parses.

    The documents are taken from the compiler while it runs rather than
    rebuilt here, because a document reconstructed by the test proves something
    about the test. Each is dumped and parsed in the desugared spelling the
    compiler actually holds.
    """
    monkeypatch = pytest.MonkeyPatch()
    seen: list[tuple[str, dict[str, Any]]] = []
    instrumented = [site for site, kind in MANUFACTURING_SITES.items() if kind == DOCUMENT]
    try:
        for site in instrumented:
            _instrument(monkeypatch, site, seen)
        _drive_everything()
    finally:
        monkeypatch.undo()

    assert seen, 'no documents were captured at all, so this asserts nothing'

    rejected = [
        f'{site}: {codes}' for site, document in seen
        for accepted, codes in [_parses(document)] if not accepted
    ]
    assert not rejected, (
        'the compiler manufactured documents the language does not accept:\n  '
        + '\n  '.join(sorted(set(rejected))))


@pytest.mark.fast
def test_the_sites_no_driver_reaches_are_the_recorded_ones() -> None:
    """An instrumented site nothing exercises is a gap, and it is named here.

    The claim this file makes is only as wide as the drivers, so the width is
    written down. A site that starts being reached should leave `UNREACHED`;
    one that stops being reached is evidence quietly lost, and both fail here.
    """
    monkeypatch = pytest.MonkeyPatch()
    seen: list[tuple[str, dict[str, Any]]] = []
    instrumented = [site for site, kind in MANUFACTURING_SITES.items() if kind == DOCUMENT]
    try:
        for site in instrumented:
            _instrument(monkeypatch, site, seen)
        _drive_everything()
    finally:
        monkeypatch.undo()

    reached = {site for site, _ in seen}
    unreached = set(instrumented) - reached

    assert unreached == set(UNREACHED), (
        f'newly unreached (evidence lost): {sorted(unreached - set(UNREACHED)) or "none"}\n'
        f'now reached (drop from UNREACHED): {sorted(set(UNREACHED) - unreached) or "none"}')


def _drive_everything() -> None:
    """Run every entry point these drivers can reach, instrumentation in place.

    Two doors, because they manufacture different things: `compile_workflow`
    for the in-memory path, and the Python API, whose `workflow_document` is
    the site that spelled step ids from the wrong stem.
    """
    for document in _DRIVERS:
        compile_hermetic_cwl(document, 'manufactured', insert_steps_automatically=True)

    adapters = REPO_ROOT / 'cwl_adapters'
    touch = Step(clt_path=adapters / 'touch.cwl')
    touch.inputs.filename = 'empty.txt'
    append = Step(clt_path=adapters / 'append.cwl')
    append.inputs.file = touch.outputs.file
    append.inputs.str = 'Hello'
    # A declared workflow output, so the step's `out:` is built from a bound
    # port rather than left empty: `Step._yml` spells it `[{name: value}]`, and
    # an edge written there in the wrong spelling is the likeliest way this
    # front end emits something the grammar refuses.
    workflow = Workflow([touch, append], 'manufactured_py')
    workflow.outputs.result = append.outputs.file
    workflow.compile()
    # Exercise the direct Python API serialization entry point as well as its
    # compile path.
    workflow.to_wic_yaml()


#: Documents chosen to drive the manufacturing sites rather than to be
#: interesting themselves: a linear chain, an explicit edge, and a `wic:`
#: sidecar — the shapes these drivers put in front of the manufacturing sites.
_DRIVERS: Final[list[dict[str, Any]]] = [
    {'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'x'}}},
               {'id': 'mk_text', 'in': {'name': {'wic_inline_input': 'y'}}}]},
    {'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'a'}},
                'out': [{'file': {'wic_anchor': 'produced'}}]},
               {'id': 'sink', 'in': {'file': {'wic_alias': 'produced'}}}]},
    {'wic': {'steps': {'(1, mk_file)': {'wic': {}}}},
     'steps': [{'id': 'mk_file', 'in': {'name': {'wic_inline_input': 'z'}}}]},
]


@pytest.mark.slow
@given(strat.workflows())
@ORACLE
def test_manufactured_documents_parse_over_generated_input(yml: dict[str, Any]) -> None:
    """The same claim, quantified over the oracle's generator rather than a list.

    `_DRIVERS` is chosen to reach particular sites and is therefore a statement
    about those sites. This one says nothing about which sites it reaches and
    everything about the range of input: whatever `workflows()` can draw, the
    documents the compiler makes from it are documents the language accepts.
    """
    monkeypatch = pytest.MonkeyPatch()
    seen: list[tuple[str, dict[str, Any]]] = []
    try:
        for site, kind in MANUFACTURING_SITES.items():
            if kind == DOCUMENT:
                _instrument(monkeypatch, site, seen)
        compile_hermetic_cwl(yml, 'generated')
    finally:
        monkeypatch.undo()

    rejected = [f'{site}: {codes}' for site, document in seen
                for accepted, codes in [_parses(document)] if not accepted]
    assert not rejected, ('the compiler manufactured documents the language does not accept:\n  '
                          + '\n  '.join(sorted(set(rejected))))
