"""Regression coverage for `sophios.inference` bugs that have no home elsewhere.

`perform_edge_inference` takes the compiler's whole per-step context as
parameters rather than reading it off `self`, so a unit test against it has to
build that context by hand instead of reaching for a fixture. The tests below
do that for the shapes `format` matching has to keep apart: a bare string,
which both iterates and tests membership per-character, and an absent format,
which means different things on a File output and on one that cannot carry one.
"""
from typing import Final

import pytest

import sophios.cli
from sophios import inference
from sophios.lang.cwl import CWL_VERSION
from sophios.utils import step_name_str
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
    """A `format` given as a bare string, not a single-element list, must
    still participate in the insertion search — on the consumer input the
    search iterates, and on the whitelisted converter's input it scans.

    Both used to be taken as written, and a bare string iterates character by
    character rather than as the one format it names, so `for in_format in
    in_formats` walked 'e', 'd', 'a', 'm', ... and `out_format in
    tool_in_formats_flat` compared a whole format string against single
    characters. Silent either way: no diagnostic, the whitelist scan just
    never finds the converter.

    Set up two real steps (`producer` -> `src_fmt`, `consumer` <- `dst_fmt`,
    formats deliberately unequal so the ordinary backward match never fires)
    plus one whitelisted converter tool, present in the registry but not used
    as a step. The consumer's and the converter's declared *input* formats are
    both bare strings - exactly the shape the bug mishandled, and one of them
    per site. `perform_edge_inference` mutably appends to `insertions`; the
    converter's `StepId` landing there is the only way this test can pass.
    """
    src_fmt, dst_fmt = 'edam:format_src', 'edam:format_dst'

    producer = _clt({}, {'file': {'type': 'File', 'format': src_fmt,
                                  'outputBinding': {'glob': 'src.txt'}}})
    # The consumer's format is a bare string too: the site the insertion search iterates.
    consumer = _clt({'file': {'type': 'File', 'format': dst_fmt, 'inputBinding': {'position': 1}}}, {})
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


@pytest.mark.fast
def test_a_format_is_not_matched_by_a_substring_of_itself() -> None:
    """Format matching is membership, not substring containment.

    `_match_outputs_of_step` accepts an output when `out_format in in_formats`.
    With a bare-string input format that is Python substring containment, so
    an output declaring `edam:format_123` matched an input declaring
    `edam:format_1234` and the edge was wired to the wrong producer - a false
    positive in the ordinary backward match, which no later pass rechecks.

    The two formats here are deliberately one character apart and the types
    match, so containment is the only thing that could join them. Not joined,
    `perform_edge_inference` defers the input to the parent workflow by
    binding it to its own namespaced name; joined, it binds it to
    `<producer step>/file`.
    """
    producer = _clt({}, {'file': {'type': 'File', 'format': 'edam:format_123',
                                  'outputBinding': {'glob': 'src.txt'}}})
    consumer = _clt({'file': {'type': 'File', 'format': 'edam:format_1234',
                              'inputBinding': {'position': 1}}}, {})

    tools: Tools = {
        StepId('producer', _NS): Tool('/synthetic/producer.cwl', producer),
        StepId('consumer', _NS): Tool('/synthetic/consumer.cwl', consumer),
    }
    tools_lst = [tools[StepId('producer', _NS)], tools[StepId('consumer', _NS)]]
    steps: list[Yaml] = [{'id': 'producer', 'in': {}}, {'id': 'consumer'}]
    steps_keys = ['producer', 'consumer']

    compiler_options, graph_settings, _tag_paths = sophios.cli.default_compilation_settings()
    graph = get_graph_reps('probe')
    deferred = f"{step_name_str('probe', 1, 'consumer')}___file"

    steps_i = inference.perform_edge_inference(
        compiler_options['inference_use_naming_conventions'], graph_settings, tools, tools_lst,
        steps_keys, 'probe', 1, steps, 'file', graph, False, [], [], {}, {}, {},
        'probe___consumer___file', False, False, [], {}, True)

    assert steps_i['in']['file'] == deferred, \
        'an output format that is a substring of the input format was matched as if equal'


@pytest.mark.fast
def test_an_output_that_cannot_carry_a_format_matches_a_format_declaring_input() -> None:
    """An output that could not have declared a format does not fail on its absence.

    CWL defines `format` for File-valued parameters only, so a `string` output
    declares none because it cannot — see the note in `cwl_adapters/ambiguous.cwl`.
    Requiring one there would make `format` on a `string` input unsatisfiable by
    construction, and every such edge would silently defer to the parent instead
    of binding to its producer.

    Both sides here are `string`, so the only question this asks is whether the
    empty side is read as unconstrained or as a mismatch.
    """
    producer = _clt({}, {'file': {'type': 'string',
                                  'outputBinding': {'outputEval': '$(inputs.x)'}}})
    consumer = _clt({'file': {'type': 'string', 'format': 'someformat',
                              'inputBinding': {'position': 1}}}, {})

    tools: Tools = {
        StepId('producer', _NS): Tool('/synthetic/producer.cwl', producer),
        StepId('consumer', _NS): Tool('/synthetic/consumer.cwl', consumer),
    }
    tools_lst = [tools[StepId('producer', _NS)], tools[StepId('consumer', _NS)]]
    steps: list[Yaml] = [{'id': 'producer', 'in': {}}, {'id': 'consumer'}]
    steps_keys = ['producer', 'consumer']

    compiler_options, graph_settings, _tag_paths = sophios.cli.default_compilation_settings()
    graph = get_graph_reps('probe')
    deferred = f"{step_name_str('probe', 1, 'consumer')}___file"

    steps_i = inference.perform_edge_inference(
        compiler_options['inference_use_naming_conventions'], graph_settings, tools, tools_lst,
        steps_keys, 'probe', 1, steps, 'file', graph, False, [], [], {}, {}, {},
        'probe___consumer___file', False, False, [], {}, True)

    assert steps_i['in']['file'] != deferred, \
        'a formatless output was refused a format-declaring input, so the edge was never wired'
    assert steps_i['in']['file'].endswith('/file'), \
        'the input should bind to the producing step output'


@pytest.mark.fast
def test_a_formatless_file_output_does_not_outrank_the_formatted_one() -> None:
    """A File output declaring no `format` has an unknown format, not a free one.

    CWL does permit a `format` on a File output, so one that omits it is making
    no claim, and matching it against a format-declaring input is a guess. It is
    also the guess that wins: outputs are scanned in reverse declaration order,
    and the conventional `stderr` output is declared last.

    Reproduces `mm-workflows/cwl_adapters/convert_mol2.cwl`, whose outputs are
    `output_mol2_path` (File, `edam:format_3816`) and a formatless `stderr`, and
    whose consumers declare the mol2 format as a single-element list - the
    spelling under which `''` is not a member. Matched loosely, every such step
    pipes stderr into the next tool instead of its actual output.
    """
    fmt = 'edam:format_3816'
    producer = _clt({}, {
        'output_mol2_path': {'type': 'File', 'format': fmt,
                             'outputBinding': {'glob': 'system.mol2'}},
        'stderr': {'type': 'File', 'outputBinding': {'glob': 'stderr'}},
    })
    consumer = _clt({'input_path': {'type': 'File', 'format': [fmt],
                                    'inputBinding': {'position': 1}}}, {})

    tools: Tools = {
        StepId('producer', _NS): Tool('/synthetic/producer.cwl', producer),
        StepId('consumer', _NS): Tool('/synthetic/consumer.cwl', consumer),
    }
    tools_lst = [tools[StepId('producer', _NS)], tools[StepId('consumer', _NS)]]
    steps: list[Yaml] = [{'id': 'producer', 'in': {}}, {'id': 'consumer'}]
    steps_keys = ['producer', 'consumer']

    compiler_options, graph_settings, _tag_paths = sophios.cli.default_compilation_settings()
    graph = get_graph_reps('probe')
    producer_step = step_name_str('probe', 0, 'producer')

    steps_i = inference.perform_edge_inference(
        compiler_options['inference_use_naming_conventions'], graph_settings, tools, tools_lst,
        steps_keys, 'probe', 1, steps, 'input_path', graph, False, [], [], {}, {}, {},
        'probe___consumer___input_path', False, False, [], {}, True)

    assert steps_i['in']['input_path'] == f'{producer_step}/output_mol2_path', \
        'the formatless stderr output was matched in preference to the declared format match'
