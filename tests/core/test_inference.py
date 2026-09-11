"""Regression coverage for `sophios.inference` bugs that have no home elsewhere.

`perform_edge_inference` takes the compiler's whole per-step context as
parameters rather than reading it off `self`, so a unit test against it has to
build that context by hand instead of reaching for a fixture. The one test
below does that once, for the bare-string `format` bug in the insertion
search's whitelist scan.
"""
from typing import Final

import pytest

import sophios.cli
from sophios import inference
from sophios.lang.cwl import CWL_VERSION
from sophios.utils_cwl import desugar_into_canonical_normal_form
from sophios.utils_graphs import get_graph_reps
from sophios.wic_types import Cwl, StepId, Tool, Tools, Yaml

_NS: Final = 'global'


def _clt(inputs: dict[str, Cwl], outputs: dict[str, Cwl]) -> Cwl:
    """A minimal CommandLineTool stub: `true` succeeds and produces nothing."""
    return desugar_into_canonical_normal_form({
        'cwlVersion': CWL_VERSION,
        'class': 'CommandLineTool',
        'baseCommand': 'true',
        'inputs': inputs,
        'outputs': outputs,
    })


@pytest.mark.fast
def test_insertion_search_matches_a_bare_string_whitelist_format() -> None:  # pylint: disable=too-many-locals
    """A whitelisted converter's *input* `format` given as a bare string, not
    a single-element list, must still participate in the insertion search.

    `tool_in_formats` used to be flattened with `utils.flatten` directly, and
    `flatten` walks a bare string character by character rather than treating
    it as one item, so `out_format in tool_in_formats_flat` compared a whole
    format string against single characters and never matched. Silent: no
    diagnostic, the whitelist scan just never finds the converter.

    Set up two real steps (`producer` -> `src_fmt`, `consumer` <- `dst_fmt`,
    formats deliberately unequal so the ordinary backward match never fires)
    plus one whitelisted converter tool, present in the registry but not used
    as a step, whose declared *input* format is the bare string `src_fmt` -
    exactly the shape the bug mishandled. `perform_edge_inference` mutably
    appends to `insertions`; the converter's `StepId` landing there is the
    only way this test can pass.
    """
    src_fmt, dst_fmt = 'edam:format_src', 'edam:format_dst'

    producer = _clt({}, {'file': {'type': 'File', 'format': src_fmt,
                                  'outputBinding': {'glob': 'src.txt'}}})
    consumer = _clt({'file': {'type': 'File', 'format': [dst_fmt], 'inputBinding': {'position': 1}}}, {})
    # The converter's *input* format is a bare string, not [src_fmt]: the exact shape the bug mishandled.
    converter = _clt({'file': {'type': 'File', 'format': src_fmt, 'inputBinding': {'position': 1}}},
                     {'file': {'type': 'File', 'format': dst_fmt, 'outputBinding': {'glob': 'dst.txt'}}})

    tools: Tools = {
        StepId('producer', _NS): Tool('/synthetic/producer.cwl', producer),
        StepId('consumer', _NS): Tool('/synthetic/consumer.cwl', consumer),
        StepId('insert_steps_automatically_conv', _NS): Tool('/synthetic/conv.cwl', converter),
    }
    tools_lst = [tools[StepId('producer', _NS)], tools[StepId('consumer', _NS)]]
    steps: list[Yaml] = [{'id': 'producer', 'in': {}}, {'id': 'consumer'}]
    steps_keys = ['producer', 'consumer']

    compiler_options, graph_settings, _tag_paths = sophios.cli.default_compilation_settings()
    graph = get_graph_reps('probe')
    insertions: list[StepId] = []

    inference.perform_edge_inference(
        compiler_options['inference_use_naming_conventions'], graph_settings, tools, tools_lst,
        steps_keys, 'probe', 1, steps, 'file', graph, False, [], [], {}, {}, {},
        'probe___consumer___file', False, False, insertions, {}, True)

    assert StepId('insert_steps_automatically_conv', _NS) in insertions, \
        'the bare-string-format converter was never offered as a candidate insertion'
