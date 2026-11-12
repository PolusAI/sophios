"""Compiled CWL is plain CWL: no `sophios` key or prefix, and EDAM only where a format says `edam:`."""
from typing import Any

import pytest

from sophios.ir.artifacts import CompilationArtifact
from sophios.ir.emit import EDAM_NAMESPACE

from .hermetic import compile_hermetic

pytestmark = pytest.mark.package


#: The fields a CWL document declares its prefixes and ontologies in.
DECLARATIONS = ('$namespaces', '$schemas')


def declared_beneath(artifact: CompilationArtifact) -> dict[str, Any]:
    """The `$namespaces` and `$schemas` of `artifact` and of every process beneath it, merged as
    embedding merges them: an embedded process may not declare its own, so they move up."""
    namespaces = dict(artifact.cwl.get('$namespaces', {}))
    schemas = list(artifact.cwl.get('$schemas', []))
    for child in artifact.children:
        below = declared_beneath(child)
        namespaces |= below.get('$namespaces', {})
        schemas += [schema for schema in below.get('$schemas', []) if schema not in schemas]
    return {field: value for field, value in zip(DECLARATIONS, (namespaces, schemas)) if value}


@pytest.mark.fast
def test_a_workflow_no_format_of_says_edam_binds_no_prefix() -> None:
    """`count` takes an unformatted File and makes an int: nothing in the document is an EDAM CURIE, so
    there is no `$namespaces` or `$schemas` at all. (The corpus tests validate such documents with cwltool.)"""
    artifact = compile_hermetic({'steps': [{'id': 'count'}]}, 'plain').artifact
    assert '$namespaces' not in artifact.cwl and '$schemas' not in artifact.cwl


@pytest.mark.fast
@pytest.mark.parametrize('workflow', [
    {'inputs': {'f': {'type': 'File', 'format': 'edam:format_2330'}}, 'steps': [{'id': 'count', 'in': {'file': 'f'}}]},
    {'steps': [{'id': 'mk_file'}]},
    {'steps': [{'id': 'count', 'in': {'file': {'wic_inline_input': {
        'class': 'File', 'location': 'x.txt', 'format': 'edam:format_2330'}}}}]},
    {'inputs': {'f': {'type': 'File', 'default': {'class': 'File', 'location': 'x.txt', 'format': 'edam:format_2330'}}},
     'steps': [{'id': 'count', 'in': {'file': 'f'}}]},
    {'steps': [{'id': 'count', 'in': {'file': {'wic_raw_cwl': {
        'default': {'class': 'File', 'location': 'x.txt', 'format': 'edam:format_2330'}}}}}]},
], ids=['authored-input-format', 'promoted-output-format', 'job-file-format', 'input-default-format',
        'step-input-default-format'])
def test_a_workflow_a_format_of_says_edam_binds_the_prefix(workflow: dict[str, Any]) -> None:
    """An authored format, a format promoted from a tool, and a File in the job file: a runner reads each
    CURIE against the workflow's `$namespaces`, so each needs `edam` bound. No schema is added: an ontology
    is a dependency only its author adds."""
    cwl = compile_hermetic(workflow).artifact.cwl
    assert cwl['$namespaces'] == {EDAM_NAMESPACE[0]: EDAM_NAMESPACE[1]} and '$schemas' not in cwl
