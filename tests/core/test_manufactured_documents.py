"""Every document the compiler manufactures (the Python API's, a real-time analysis's one-step workflow) is one the
language accepts: built rather than parsed, the grammar has no say over them unless asked."""
import importlib
from typing import Any, Final

import pytest
import yaml

from sophios import realtime
from sophios.cli import default_compilation_settings
from sophios.input_output import NoAliasDumper
from sophios.ir.realtime import Declaration
from sophios.lang import Document, parse, to_json
from sophios.api.python.workflow import Workflow
from sophios.wic_types import StepId, Tool

from .helpers import ADAPTERS, HELLOWORLD, adapter_step

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
    'sophios/realtime.py::_wrapper': DOCUMENT,
    # Not documents.
    'sophios/ir/emit.py::emit': CWL,
    'sophios/lang/render.py::_Writer.document': RENDERER,
    'sophios/lang/render.py::_Writer.sidecar': RENDERER,
    'sophios/lang/schema.py::_defs': SCHEMA,
    'sophios/utils_cwl.py::desugar_into_canonical_normal_form': TOOL,
    'sophios/contrib/converter.py::wfb_to_wic': CONTRIB,
    'sophios/contrib/rest/api.py::compile_wf': CONTRIB,
}


def _documents_in(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Every document-shaped mapping reachable from `value`, to a bounded depth: compiled trees are deep and self-
    referential."""
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
    """Record every document-shaped object that passes through one site, arguments as well as returns: some sites
    rewrite in place."""
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
    """Whether `sophios.lang` accepts this document, and what it said if not, re-parsed in the desugared spelling (§2):
    construct positions stay closed, a `wic_*` key elsewhere is open passthrough (§1)."""
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


def _drive_everything() -> None:
    """Run every entry point these drivers reach, instrumentation in place: the Python API's `workflow_document` once
    spelled step ids from the wrong stem."""
    touch = adapter_step('touch', filename='empty.txt')
    append = adapter_step('append', file=touch.outputs.file, str='Hello')
    # A declared workflow output, so the step's `out:` is built from a bound
    # port rather than left empty: `Step._as_workflow_step` spells it as an edge, and
    # an edge written there in the wrong spelling is the likeliest way this
    # front end emits something the grammar refuses.
    workflow = Workflow([touch, append], 'manufactured_py')
    workflow.outputs.result = append.outputs.file
    workflow.compile()
    # A real-time analysis of each kind: a tool configured by its `in:`, and a
    # `.wic` configured by its steps' sidecar, each with a file named by basename.
    tools = {StepId(stem, 'global'): Tool(str(ADAPTERS / f'{stem}.cwl'),
                                          yaml.safe_load((ADAPTERS / f'{stem}.cwl').read_text(encoding='utf-8')))
             for stem in ('append', 'echo')}
    realtime.compile_analyses((
        Declaration('wf', 'append', '*.txt', 2, 60, {'in': {'file': 'empty.txt', 'str': 'Hello'}}),
        Declaration('wf', 'helloworld.wic', '*.txt', 2, 60, {'(1, echo)': {'in': {'message': 'Hi'}}}),
    ), {'global': {'helloworld': HELLOWORLD}},
        tools, default_compilation_settings())
