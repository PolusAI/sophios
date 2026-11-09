"""Tools with known signatures, owned by the suite: get_tools_cwl depends on what a machine has checked out, so a
counterexample reproduces from this repository alone. Each stem reaches a compiler branch; none runs (`true`)."""
from collections.abc import Mapping
from typing import Final, cast

from sophios.lang.cwl import CWL_VERSION
from sophios.utils_cwl import desugar_into_canonical_normal_form
from sophios.wic_types import Cwl, StepId, Tool, Tools, Yaml

#: 'global', because the compiler looks a step's tool up under its `wic: namespace:`, which defaults to 'global'.
SYNTHETIC_NS: Final = 'global'

#: Each format prefix's URI; `clt` declares only the prefixes a tool's formats use.
_NAMESPACE_URIS: Final = {'edam': 'https://edamontology.org/'}
_TXT: Final = 'edam:format_2330'
_CSV: Final = 'edam:format_3752'


def _declared_prefixes(ports: dict[str, Cwl]) -> set[str]:
    """Every `prefix:` a port's `format` names."""
    found: set[str] = set()
    for port in ports.values():
        formats = port.get('format', []) if isinstance(port, dict) else []
        for entry in ([formats] if isinstance(formats, str) else formats):
            if isinstance(entry, str) and ':' in entry:
                found.add(entry.split(':', 1)[0])
    return found & set(_NAMESPACE_URIS)


def clt(inputs: dict[str, Cwl], outputs: dict[str, Cwl], *,
        javascript: bool = False, requirements: dict[str, Cwl] | None = None,
        canonical: bool = False) -> Cwl:
    """One stub CommandLineTool (`true` succeeds and makes nothing), `$namespaces` only for prefixes its formats use;
    `canonical` gives the normal form plugins.py applies on load."""
    tool: Cwl = {
        'cwlVersion': CWL_VERSION,
        'class': 'CommandLineTool',
        'baseCommand': 'true',
        'inputs': inputs,
        'outputs': outputs,
    }
    prefixes = _declared_prefixes(inputs) | _declared_prefixes(outputs)
    if prefixes:
        tool['$namespaces'] = {prefix: _NAMESPACE_URIS[prefix] for prefix in sorted(prefixes)}
    merged = {**({'InlineJavascriptRequirement': {}} if javascript else {}), **(requirements or {})}
    if merged:
        tool['requirements'] = merged
    return desugar_into_canonical_normal_form(tool) if canonical else tool


#: The named types `schemed` declares, so a literal strategy can draw a value for a port that only names one.
SCHEMA_TYPES: Final[dict[str, Cwl]] = {
    'Coords': {'name': 'Coords', 'type': 'record',
               'fields': [{'name': 'x', 'type': 'float'}, {'name': 'y', 'type': 'float'}]},
}

_SPECS: Final[dict[str, Cwl]] = {
    'mk_file': clt(
        {'name': {'type': 'string', 'inputBinding': {'position': 1}}},
        {'file': {'type': 'File', 'format': _TXT,
                  'outputBinding': {'glob': '$(inputs.name)'}}},
    ),
    'mk_text': clt(
        {'name': {'type': 'string', 'inputBinding': {'position': 1}}},
        {'text': {'type': 'File', 'format': _CSV,
                  'outputBinding': {'glob': '$(inputs.name)'}}},
    ),
    'xform': clt(
        {'file': {'type': 'File', 'inputBinding': {'position': 1}},
         'name': {'type': 'string', 'inputBinding': {'position': 2}}},
        {'file': {'type': 'File', 'format': _TXT,
                  'outputBinding': {'glob': '$(inputs.name)'}}},
    ),
    'join': clt(
        {'left': {'type': 'File', 'inputBinding': {'position': 1}},
         'right': {'type': 'File', 'inputBinding': {'position': 2}},
         'name': {'type': 'string', 'inputBinding': {'position': 3}}},
        {'file': {'type': 'File', 'format': _TXT,
                  'outputBinding': {'glob': '$(inputs.name)'}}},
    ),
    'count': clt(
        {'file': {'type': 'File', 'inputBinding': {'position': 1}}},
        {'n': {'type': 'int', 'outputBinding': {'outputEval': '$(1)'}}},
        javascript=True,
    ),
    'scale': clt(
        {'n': {'type': 'int', 'inputBinding': {'position': 1}},
         'factor': {'type': 'float', 'default': 1.0, 'inputBinding': {'position': 2}}},
        {'scaled': {'type': 'float', 'outputBinding': {'outputEval': '$(1.0)'}}},
        javascript=True,
    ),
    'poly': clt(
        {'value': {'type': ['int', 'string'], 'inputBinding': {'position': 1}}},
        {'value': {'type': 'string', 'outputBinding': {'outputEval': '$("x")'}}},
        javascript=True,
    ),
    'sink': clt(
        {'file': {'type': 'File', 'inputBinding': {'position': 1}},
         'n': {'type': 'int', 'inputBinding': {'position': 2}},
         'extras': {'type': 'File[]', 'default': [], 'inputBinding': {'position': 3}}},
        {},
    ),
    # The one Directory input, and the one tool with several outputs (File, File and int).
    'split': clt(
        {'dir': {'type': 'Directory', 'inputBinding': {'position': 1}},
         'name': {'type': 'string', 'inputBinding': {'position': 2}}},
        {'head': {'type': 'File', 'format': _TXT, 'outputBinding': {'glob': '$(inputs.name)'}},
         'tail': {'type': 'File', 'format': _CSV, 'outputBinding': {'glob': 'tail'}},
         'n': {'type': 'int', 'outputBinding': {'outputEval': '$(2)'}}},
        javascript=True,
    ),
    # A record input and an enum input: the two structured types no generator could draw.
    'typed': clt(
        {'pair': {'type': {'type': 'record', 'name': 'pair',
                           'fields': [{'name': 'left', 'type': 'string'}, {'name': 'right', 'type': 'int'}]},
                  'inputBinding': {'position': 1}},
         'mode': {'type': {'type': 'enum', 'name': 'mode', 'symbols': ['fast', 'slow']},
                  'inputBinding': {'position': 2}}},
        {'file': {'type': 'File', 'format': _TXT, 'outputBinding': {'glob': 'typed.txt'}}},
    ),
    # A named type from a SchemaDefRequirement, referenced by name.
    'schemed': clt(
        {'coords': {'type': 'Coords', 'inputBinding': {'position': 1}}},
        {'n': {'type': 'int', 'outputBinding': {'outputEval': '$(3)'}}},
        javascript=True,
        requirements={'SchemaDefRequirement': {'types': list(SCHEMA_TYPES.values())}},
    ),
    # secondaryFiles and loadContents on an input; a promoted boundary keeps both.
    'indexed': clt(
        {'file': {'type': 'File', 'secondaryFiles': ['.idx'], 'loadContents': True,
                  'inputBinding': {'position': 1}}},
        {'file': {'type': 'File', 'secondaryFiles': ['.idx'], 'format': _TXT,
                  'outputBinding': {'glob': 'indexed.txt'}}},
    ),
    # A captured stream as an output: the one tool-only type.
    'captured': {**clt({'name': {'type': 'string', 'inputBinding': {'position': 1}}},
                       {'out': {'type': 'stdout'}}), 'stdout': 'captured.txt'},
    # List-form ports, requirements and hints: desugaring lifts only `inputs` and `outputs`, so the compiler reads the
    # rest in list form itself.
    'listed': {
        'cwlVersion': CWL_VERSION, 'class': 'CommandLineTool', 'baseCommand': 'true',
        'inputs': [{'id': 'name', 'type': 'string', 'inputBinding': {'position': 1}}],
        'outputs': [{'id': 'file', 'type': 'File', 'format': _TXT,
                     'outputBinding': {'glob': '$(inputs.name)'}}],
        'requirements': [{'class': 'InlineJavascriptRequirement'}],
        'hints': [{'class': 'DockerRequirement', 'dockerPull': 'docker.io/bash:4.4'}],
        '$namespaces': {'edam': 'https://edamontology.org/'},
    },
}

#: Desugared as get_tools_cwl desugars real adapters; the run path names no file, since nothing reads it.
SYNTHETIC_TOOLS: Final[Tools] = {
    StepId(stem, SYNTHETIC_NS): Tool(f'/synthetic/{stem}.cwl',
                                     desugar_into_canonical_normal_form(dict(cwl)))
    for stem, cwl in _SPECS.items()
}

STEMS: Final[tuple[str, ...]] = tuple(sorted(_SPECS))


def registered(tools: Mapping[str, Cwl], base: Tools = SYNTHETIC_TOOLS) -> Tools:
    """`base` plus each of `tools` under its stem, as `get_tools_cwl` registers a plugin's."""
    return {**base, **{StepId(stem, SYNTHETIC_NS): Tool(f'/synthetic/{stem}.cwl', cwl) for stem, cwl in tools.items()}}


def insertion_registry() -> tuple[Yaml, Tools]:
    """Two branches, each needing exactly one whitelisted-converter insertion: the O(2^n) speculative path."""
    specs: dict[str, Cwl] = {}
    for k in (1, 2):
        source, sink = f'edam:format_src{k}', f'edam:format_dst{k}'
        specs |= {f'mk_{k}': clt({}, {'file': {'type': 'File', 'format': source}}, canonical=True),
                  f'use_{k}': clt({'file': {'type': 'File', 'format': sink}}, {}, canonical=True),
                  f'insert_steps_automatically_conv_{k}': clt({'file': {'type': 'File', 'format': source}},
                                                              {'file': {'type': 'File', 'format': sink}},
                                                              canonical=True)}
    return {'steps': [{'id': f'{kind}_{k}'} for k in (1, 2) for kind in ('mk', 'use')]}, registered(specs, {})


def inputs_of(stem: str) -> dict[str, Cwl]:
    """The tool's declared inputs, after desugaring."""
    return cast(dict[str, Cwl], SYNTHETIC_TOOLS[StepId(stem, SYNTHETIC_NS)].cwl['inputs'])


def outputs_of(stem: str) -> dict[str, Cwl]:
    """The tool's declared outputs, after desugaring."""
    return cast(dict[str, Cwl], SYNTHETIC_TOOLS[StepId(stem, SYNTHETIC_NS)].cwl['outputs'])


def _nullable(kind: Cwl) -> bool:
    return kind.endswith('?') if isinstance(kind, str) else isinstance(kind, list) and 'null' in kind


def required_inputs_of(stem: str) -> tuple[str, ...]:
    """Inputs the compiler must bind or infer, computed here so the generator never asks the compiler to grade itself;
    a non-null default satisfies an input, a falsy one (sink.extras: []) included."""
    return tuple(name for name, spec in inputs_of(stem).items()
                 if spec.get('default') is None and not _nullable(spec['type']))
