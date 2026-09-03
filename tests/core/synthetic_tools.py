"""Tools with known signatures, owned by the suite.

`get_tools_cwl` globs `search_paths_cwl` (src/sophios/plugins.py:90-126), so the
tools a property sees depend on which plugin repositories a machine has checked
out. That cannot be an oracle. These eight stems are the whole vocabulary Spec 2
generates from, so a counterexample reproduces from this repository alone.

The signatures are chosen to reach the compiler's branches, not to model
anything real:

  - `mk_file` and `mk_text` are sources — no File input, one File output each,
    with different `format`s, so inference has a choice to make and a wrong
    choice is observable.
  - `xform` is File -> File: it chains, which makes step order matter.
  - `join` takes two Files: one step, two edges to infer.
  - `count` is File -> int and `scale` is int -> float: an edge that ignores
    types shows up as a type mismatch rather than a coincidence.
  - `poly` declares a union input type, reaching `types_match`'s list branches
    (src/sophios/inference.py:12-31), which single-typed tools never do.
  - `sink` has no outputs: the only tool that can end a workflow without
    contributing to `outputs:`.
  - `passthru` has an optional input with a default, so `args_required`
    (src/sophios/compiler.py:588) is a proper subset of the inputs somewhere.

The documents are real CWL v1.2: `test_every_stub_is_valid_cwl` runs cwltool
over each. They are never executed — `baseCommand` is `true` — so no container
is pulled and no property in this Spec needs one.
"""
from typing import Final

from sophios.lang.cwl import CWL_VERSION
from sophios.utils_cwl import desugar_into_canonical_normal_form
from sophios.wic_types import Cwl, StepId, Tool, Tools

#: The plugin namespace tools must be registered under.
#:
#: `'global'`, not `'synthetic'`, and this is not a naming preference. The
#: compiler resolves a step's tool as `StepId(stem, plugin_ns_i)` where
#: `plugin_ns_i = wic_step_i.get('wic', {}).get('namespace', 'global')`
#: (src/sophios/compiler.py:500-502) — so a tool registered under any other
#: namespace is invisible unless every step declares that namespace in a `wic:`
#: sidecar entry of its own. Verified by execution: registering under
#: `'synthetic'` fails with "Error! Neither mk_file nor  found!".
SYNTHETIC_NS: Final = 'global'

_EDAM: Final = {'edam': 'https://edamontology.org/'}
_TXT: Final = 'edam:format_2330'
_CSV: Final = 'edam:format_3752'


def _clt(inputs: dict[str, Cwl], outputs: dict[str, Cwl], *, javascript: bool = False) -> Cwl:
    """One stub CommandLineTool. `true` succeeds and produces nothing, which is
    all a compile-only registry needs."""
    tool: Cwl = {
        'cwlVersion': CWL_VERSION,
        'class': 'CommandLineTool',
        'baseCommand': 'true',
        '$namespaces': dict(_EDAM),
        'inputs': inputs,
        'outputs': outputs,
    }
    if javascript:
        tool['requirements'] = {'InlineJavascriptRequirement': {}}
    return tool


_SPECS: Final[dict[str, Cwl]] = {
    'mk_file': _clt(
        {'name': {'type': 'string', 'inputBinding': {'position': 1}}},
        {'file': {'type': 'File', 'format': _TXT,
                  'outputBinding': {'glob': '$(inputs.name)'}}},
    ),
    'mk_text': _clt(
        {'name': {'type': 'string', 'inputBinding': {'position': 1}}},
        {'text': {'type': 'File', 'format': _CSV,
                  'outputBinding': {'glob': '$(inputs.name)'}}},
    ),
    'xform': _clt(
        {'file': {'type': 'File', 'inputBinding': {'position': 1}},
         'name': {'type': 'string', 'inputBinding': {'position': 2}}},
        {'file': {'type': 'File', 'format': _TXT,
                  'outputBinding': {'glob': '$(inputs.name)'}}},
    ),
    'join': _clt(
        {'left': {'type': 'File', 'inputBinding': {'position': 1}},
         'right': {'type': 'File', 'inputBinding': {'position': 2}},
         'name': {'type': 'string', 'inputBinding': {'position': 3}}},
        {'file': {'type': 'File', 'format': _TXT,
                  'outputBinding': {'glob': '$(inputs.name)'}}},
    ),
    'count': _clt(
        {'file': {'type': 'File', 'inputBinding': {'position': 1}}},
        {'n': {'type': 'int', 'outputBinding': {'outputEval': '$(1)'}}},
        javascript=True,
    ),
    'scale': _clt(
        {'n': {'type': 'int', 'inputBinding': {'position': 1}},
         'factor': {'type': 'float', 'default': 1.0, 'inputBinding': {'position': 2}}},
        {'scaled': {'type': 'float', 'outputBinding': {'outputEval': '$(1.0)'}}},
        javascript=True,
    ),
    'poly': _clt(
        {'value': {'type': ['int', 'string'], 'inputBinding': {'position': 1}}},
        {'value': {'type': 'string', 'outputBinding': {'outputEval': '$("x")'}}},
        javascript=True,
    ),
    'sink': _clt(
        {'file': {'type': 'File', 'inputBinding': {'position': 1}},
         'n': {'type': 'int', 'inputBinding': {'position': 2}}},
        {},
    ),
}

#: Desugared exactly as `get_tools_cwl` desugars real adapters
#: (src/sophios/plugins.py:117), so the compiler sees the same shape either way.
#: `run_path` names a file that does not exist: nothing reads it — the emitted
#: `run:` is a string, and P36 inlines the documents with `cwl_inline_runtag`.
SYNTHETIC_TOOLS: Final[Tools] = {
    StepId(stem, SYNTHETIC_NS): Tool(f'/synthetic/{stem}.cwl',
                                     desugar_into_canonical_normal_form(dict(cwl)))
    for stem, cwl in _SPECS.items()
}

STEMS: Final[tuple[str, ...]] = tuple(sorted(_SPECS))


def _cwl(stem: str) -> Cwl:
    return SYNTHETIC_TOOLS[StepId(stem, SYNTHETIC_NS)].cwl


def inputs_of(stem: str) -> dict[str, Cwl]:
    """The tool's declared inputs, after desugaring."""
    found: dict[str, Cwl] = _cwl(stem)['inputs']
    return found


def outputs_of(stem: str) -> dict[str, Cwl]:
    """The tool's declared outputs, after desugaring."""
    found: dict[str, Cwl] = _cwl(stem)['outputs']
    return found


def required_inputs_of(stem: str) -> tuple[str, ...]:
    """Inputs the compiler will demand a value or an inferred edge for.

    Mirrors `_arg_has_default_or_is_optional` (src/sophios/compiler.py:117-126).
    Deliberately a second implementation rather than an import: a generator that
    asked the compiler which inputs are required, and then a property that
    checked the compiler honoured them, would be asking one implementation to
    grade itself.
    """
    required = []
    for name, spec in inputs_of(stem).items():
        arg_type = spec['type']
        optional = (isinstance(arg_type, str) and arg_type.endswith('?')) or (
            isinstance(arg_type, list) and 'null' in arg_type)
        if not (spec.get('default') is not None or optional):
            required.append(name)
    return tuple(required)
