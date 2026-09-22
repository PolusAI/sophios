"""The text front door: Parse reads the file, not a regenerated document.

Every claim here is about bytes. A bundle built from disk hands Parse the
user's own text, so a span it reports is a position a reader can open an
editor at. `test_span_names_the_line_in_the_authored_file` is the one that
says so directly: it locates the failing construct by searching the file it
wrote, and pins that the round-tripped spelling of the same document reports a
different line -- which is the defect the front door removes.
"""
from pathlib import Path

import pytest
import yaml

from sophios.ir.frontdoor import bundle_from_disk
from sophios.ir.pipeline import front_end
from sophios.ir.resolve import RegistryKey, generated_process_id
from sophios.lang import SophiosErrorCode, parse
from sophios.utils_yaml import wic_loader

from .synthetic_tools import SYNTHETIC_TOOLS

#: A root whose failing construct sits well below the top of the file, so a
#: reported line that came from a re-dump cannot coincide with the real one.
FORWARD_EDGE = '''# A workflow that references an edge before defining it.
#
# These comment lines are load-bearing: a YAML round trip drops them, which
# is exactly what moves every construct below them onto a different line.
#
steps:
- id: xform
  in:
    file: !* later
    name: out.txt
- id: mk_file
  in:
    name: made.txt
  out:
  - file: !& later
'''

PYTHON_SCRIPT = '''
inputs = {'name': {'type': 'string', 'format': 'edam:format_2330'}}
outputs = {'file': ('$(inputs.name)', {'type': 'File', 'format': 'edam:format_2330'})}


def main(name: str) -> None:
    """A script whose annotations are all the generator reads."""
'''


def _line_of(text: str, needle: str) -> int:
    """The 1-based line of `needle`, read out of the text itself."""
    return next(number for number, line in enumerate(text.splitlines(), start=1)
                if needle in line)


def _redump(text: str) -> str:
    """The same document as the legacy path hands Parse: loaded and dumped."""
    return yaml.dump(yaml.load(text, Loader=wic_loader()),
                     sort_keys=False, line_break='\n', indent=2)


@pytest.mark.fast
def test_source_is_the_file_verbatim(tmp_path: Path) -> None:
    """The root's own bytes reach Parse, comments and blank lines included."""
    text = ('# a leading comment\n'
            '\n'
            'wic:\n'
            '  graphviz:\n'
            '    label: Root\n'
            '\n'
            'steps:\n'
            '- id: mk_file\n'
            '  in:\n'
            '    name: made.txt\n')
    root = tmp_path / 'tutorial.wic'
    root.write_text(text, encoding='utf-8')

    bundle = bundle_from_disk(root, {'global': {}}, SYNTHETIC_TOOLS)

    assert bundle.source == text
    assert bundle.name == 'tutorial'


@pytest.mark.fast
def test_child_workflow_is_registered_as_its_own_text(tmp_path: Path) -> None:
    """A called `.wic` enters the registry as the file, not as a re-dump."""
    child_text = ('# the child keeps its comments too\n'
                  'steps:\n'
                  '- id: mk_file\n'
                  '  in:\n'
                  '    name: child.txt\n')
    child = tmp_path / 'child.wic'
    child.write_text(child_text, encoding='utf-8')
    root = tmp_path / 'root.wic'
    root.write_text('steps:\n- id: child.wic\n', encoding='utf-8')

    bundle = bundle_from_disk(root, {'global': {'child': child}}, SYNTHETIC_TOOLS)

    entry = bundle.registry.workflow(RegistryKey('global', 'child'))
    assert entry is not None
    assert entry.source == child_text
    assert entry.source != _redump(child_text)
    assert front_end(bundle.source, bundle.registry, name=bundle.name).graph is not None


@pytest.mark.fast
def test_the_call_sites_namespace_keys_the_workflow(tmp_path: Path) -> None:
    """The caller's declaration files the child, not the child's own.

    `resolve._resolve_process` builds its `RegistryKey` from the *step's*
    sidecar namespace, so that is the key it will look under. A child that
    declares a different namespace for itself does not move where its caller
    will go looking, and filing it there would file it where nothing reads.
    """
    child = tmp_path / 'child.wic'
    child.write_text('wic:\n  namespace: gpu\nsteps:\n- id: mk_file\n  in:\n    name: c.txt\n',
                     encoding='utf-8')
    root = tmp_path / 'root.wic'
    root.write_text('steps:\n- id: child.wic\n', encoding='utf-8')

    bundle = bundle_from_disk(root, {'global': {'child': child}}, SYNTHETIC_TOOLS)

    assert bundle.registry.workflow(RegistryKey('global', 'child')) is not None
    assert bundle.registry.workflow(RegistryKey('gpu', 'child')) is None


@pytest.mark.fast
def test_span_names_the_line_in_the_authored_file(tmp_path: Path) -> None:
    """A diagnostic points at the line the construct occupies on disk.

    The second assertion is the point of the first: feeding the same document
    through a YAML round trip -- what the legacy path does -- reports a line
    that exists in the dump and means something else in the file.
    """
    root = tmp_path / 'forward.wic'
    root.write_text(FORWARD_EDGE, encoding='utf-8')
    bundle = bundle_from_disk(root, {'global': {}}, SYNTHETIC_TOOLS)

    result = front_end(bundle.source, bundle.registry, name=bundle.name)

    undefined = [item for item in result.diagnostics
                 if item.code is SophiosErrorCode.UNDEFINED_EDGE]
    assert len(undefined) == 1
    assert undefined[0].span is not None
    assert undefined[0].span.start_line == _line_of(FORWARD_EDGE, '!* later')

    dumped = [item for item in front_end(_redump(FORWARD_EDGE), bundle.registry,
                                         name=bundle.name).diagnostics
              if item.code is SophiosErrorCode.UNDEFINED_EDGE]
    assert len(dumped) == 1
    assert dumped[0].span is not None
    assert dumped[0].span.start_line != undefined[0].span.start_line


@pytest.mark.fast
def test_python_script_resolves_without_writing_a_file(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """A generated tool is keyed as Resolve keys it and never reaches disk.

    `python_cwl_adapter` imports `workflow_types` from a path relative to the
    working directory, so the fixture supplies both the directory and that
    sibling file; nothing here reads the repository's own examples.
    """
    types = tmp_path / 'sophios' / 'examples' / 'scripts'
    types.mkdir(parents=True)
    (types / 'workflow_types.py').write_text(
        "string = {'type': 'string', 'format': 'edam:format_2330'}\n", encoding='utf-8')
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'my_script.py').write_text(PYTHON_SCRIPT, encoding='utf-8')
    text = 'steps:\n- id: python_script\n  in:\n    script: my_script.py\n    name: made.txt\n'
    root = work / 'root.wic'
    root.write_text(text, encoding='utf-8')
    monkeypatch.chdir(work)

    bundle = bundle_from_disk(root, {'global': {}}, SYNTHETIC_TOOLS)

    document = parse(text, 'root.wic').document
    assert document is not None
    expected = RegistryKey('global', generated_process_id(document.steps[0]))
    assert bundle.registry.tool(expected) is not None

    result = front_end(bundle.source, bundle.registry, name=bundle.name)
    assert result.resolved is not None and result.resolved.document is not None
    process = result.resolved.document.steps[0].process
    assert process.key == expected
    assert process.generated
    assert not (work / 'autogenerated').exists()


@pytest.mark.fast
def test_the_schema_gate_runs_before_anything_reads_the_document(tmp_path: Path) -> None:
    """A document the schema refuses is refused as it is read.

    The `wic:` block is closed, so a key the language does not have is caught
    here rather than carried through as opaque data -- which is what happened
    while nothing validated. Supplying no validator is the other half of the
    claim: it is the gate that rejects, not the parser.
    """
    from sophios.lang.diagnostics import SophiosError  # pylint: disable=import-outside-toplevel

    from .test_setup import load_test_registry  # pylint: disable=import-outside-toplevel

    registry = load_test_registry()
    written = tmp_path / 'probe.wic'
    written.write_text('wic:\n  nonsense_key: 1\nsteps:\n- id: mk_file\n  in:\n    name: !ii a\n',
                       encoding='utf-8')

    with pytest.raises(SophiosError) as caught:
        bundle_from_disk(written, {'global': {}}, registry.tools, registry.validator)
    assert [item.code for item in caught.value.diagnostics][0] is SophiosErrorCode.SUBWORKFLOW_INVALID

    # Without the gate the same document is accepted, so the rejection above
    # is the validator's and not something the parser would have caught.
    assert bundle_from_disk(written, {'global': {}}, registry.tools) is not None
