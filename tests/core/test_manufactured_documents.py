"""Every document the compiler manufactures is one the language accepts.

The corpus is not the interesting input: documents a *user* wrote already have
a parse property. What nothing checked is the documents the compiler *makes* —
the Python API's output and the documents `rerun_cwltool` builds. Those
are built rather than parsed, so the grammar has no opinion about them unless
asked, and three defects in a row lived exactly there: step ids spelled
from the wrong stem, a producer still emitting a step form the grammar had
removed, and scatter readable from two places with no owner.

Sites no driver reaches are named in `UNREACHED` rather than ignored.
"""
import importlib
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from sophios.input_output import NoAliasDumper
from sophios.lang import Document, parse, to_json
from sophios.api.python.workflow import Step, Workflow

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: What each site builds. Only `DOCUMENT` is a `.wic` document the language owns.
DOCUMENT: Final = 'DOCUMENT'
SCHEMA: Final = 'SCHEMA'          # JSON Schema, a description of documents
RENDERER: Final = 'RENDERER'      # sophios.lang's own writer, pinned separately
TOOL: Final = 'TOOL'              # a CWL tool, which has `steps` only incidentally
CWL: Final = 'CWL'                # compiled CWL workflow, not Sophios source
CONTRIB: Final = 'CONTRIB'        # outside the core zone

#: Every place in `src/sophios` that builds or rewrites a document-shaped object,
#: classified by hand.
MANUFACTURING_SITES: Final[dict[str, str]] = {
    'sophios/api/python/_workflow_runtime.py::workflow_document': DOCUMENT,
    'sophios/cwl_subinterpreter.py::rerun_cwltool': DOCUMENT,
    # Not documents.
    'sophios/ir/emit.py::emit': CWL,
    'sophios/lang/render.py::_Writer.document': RENDERER,
    'sophios/lang/render.py::_Writer.sidecar': RENDERER,
    'sophios/lang/schema.py::_defs': SCHEMA,
    'sophios/utils_cwl.py::desugar_into_canonical_normal_form': TOOL,
    'sophios/contrib/converter.py::wfb_to_wic': CONTRIB,
    'sophios/contrib/rest/api.py::compile_wf': CONTRIB,
}

#: Instrumented sites no driver below reaches, and why. Each is evidence not
#: gathered, so each needs a reason that can be checked rather than a shrug.
UNREACHED: Final[dict[str, str]] = {
    'sophios/cwl_subinterpreter.py::rerun_cwltool': 'shells out to a CWL runner; its documents are '
    'pinned directly by test_compiler.py',
}


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
    elif isinstance(value, Document):    # the Python API builds the AST directly
        found.append(to_json(value))
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
    """Run every entry point these drivers can reach, instrumentation in place:
    the Python API, whose `workflow_document` is the site that spelled step ids
    from the wrong stem.
    """
    adapters = REPO_ROOT / 'cwl_adapters'
    touch = Step(clt_path=adapters / 'touch.cwl')
    touch.inputs.filename = 'empty.txt'
    append = Step(clt_path=adapters / 'append.cwl')
    append.inputs.file = touch.outputs.file
    append.inputs.str = 'Hello'
    # A declared workflow output, so the step's `out:` is built from a bound
    # port rather than left empty: `Step._as_workflow_step` spells it as an edge, and
    # an edge written there in the wrong spelling is the likeliest way this
    # front end emits something the grammar refuses.
    workflow = Workflow([touch, append], 'manufactured_py')
    workflow.outputs.result = append.outputs.file
    workflow.compile()
    # Exercise the direct Python API serialization entry point as well as its
    # compile path.
    workflow.to_wic_yaml()
