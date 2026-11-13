"""The leak boundary against the real compiler: keys the language does not claim survive byte for byte and change
nothing else, and the residue validates as CWL. What the compiler does with the keys it owns is stated by
tests/core/data/compiler/boundary."""
from pathlib import Path
import tempfile
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

import sophios.post_compile
from sophios.lang import Grammar, parse
from sophios.lang.cwl import CWL_VERSION
from sophios.wic_types import Yaml

from . import ast_strategies as strat
from .ast_strategies import hints_values, passthrough_keys, passthrough_values
from .compile_harness import COMPILED, compile_info
from .cwl_validation import validate_cwl
from .hermetic import compile_hermetic_cwl

#: Top-level keys the *compiler* owns, a larger set than the parser claims: it writes `class` and `cwlVersion`,
#: merges into `inputs`, `outputs` and `requirements`, and keeps `$namespaces` and `$schemas` by its own rules.
COMPILER_OWNED_TOP_KEYS = frozenset({
    'steps', 'wic', 'class', 'cwlVersion', 'inputs', 'outputs', 'requirements', '$namespaces', '$schemas',
})


@pytest.mark.slow
@given(st.data())
@COMPILED
def test_freight_survives_byte_for_byte_and_changes_nothing_else(data: st.DataObject) -> None:
    """Step keys outside the claimed set, top-level keys outside the compiler's, and `hints:` on the document and the
    step survive compilation unchanged, and compiled without them the document is the same: the observable meaning of
    "not interpreted". The document has several steps and the freighted one scatters, so the requirement merge, which
    reads step facts and writes a top-level key, fires beside the freight. The parser routes each freight key, and
    no claimed one, to a step's passthrough, and each claimed key to what it interprets, never to both."""
    document, index = data.draw(strat.freighted_documents())
    step_freight = data.draw(st.dictionaries(passthrough_keys, passthrough_values, min_size=1, max_size=4))
    top_freight = data.draw(st.dictionaries(passthrough_keys.filter(lambda key: key not in COMPILER_OWNED_TOP_KEYS),
                                            passthrough_values, max_size=3))
    hints = data.draw(hints_values)
    hinted = {'hints': hints} if hints else {}
    freighted = strat.to_yml_with_freight(document, index, {**step_freight, **hinted})
    freighted.update(top_freight, **hinted)

    compiled = compile_hermetic_cwl(freighted)
    assert 'ScatterFeatureRequirement' in compiled['requirements']
    step = compiled['steps'][index]
    for key, value in {**step_freight, **hinted}.items():
        assert step.pop(key) == value, f'{key} was altered by compilation'
    for key, value in {**top_freight, **hinted}.items():
        assert compiled.pop(key) == value, f'{key} was altered by compilation'
    assert compiled == compile_hermetic_cwl(strat.to_yml(document))

    for key in [*step_freight, *Grammar.INTERPRETED_STEP_KEYS]:
        parsed = parse(f'steps:\n- id: s\n  {key}: x\n', 'freight.wic').document
        assert parsed is not None
        routed = (key in dict(parsed.steps[0].interpreted), key in dict(parsed.steps[0].passthrough))
        assert routed == ((True, False) if key in Grammar.INTERPRETED_STEP_KEYS else (False, True)), key


#: Passthrough spelled in CWL vocabulary: CWL v1.2 rejects unknown step fields, so arbitrary keys are the property
#: above's, and this one's claim is validity.
cwl_passthrough = st.fixed_dictionaries({}, optional={
    'label': st.text('abc xyz', min_size=1, max_size=20),
    'doc': st.text('abc xyz.', min_size=1, max_size=40),
    'hints': st.just({'LoadListingRequirement': {'loadListing': 'shallow_listing'}}),
})


@pytest.mark.needs_cwltool
@pytest.mark.skip_pypi_ci
@pytest.mark.slow
# Ten examples, not the shared hundred: `cwl_passthrough` varies only in which of three optional keys are present,
# and the free text inside `label`/`doc` cannot change whether a document is valid CWL.
@given(cwl_passthrough, st.booleans())
@example({}, True)
@settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_residue_validates_as_cwl_v1_2(freight: dict[str, Any], authored: bool) -> None:
    """Strip the Sophios syntax, inline the tools, and cwltool agrees the residue is CWL v1.2. An `outputSource:` the
    author wrote names a step cwltool finds: the workflow strategy never writes one, and the authored path once
    carried the document's own spelling into a document whose steps are all renamed."""
    workflow: Yaml = {'steps': [{'id': 'touch', 'in': {'filename': {'wic_inline_input': 'empty.txt'}}, **freight}]}
    if authored:
        workflow['outputs'] = {'mine': {'type': 'File', 'outputSource': 'touch/file'}}
    inlined = sophios.post_compile.inline_artifact_runs(compile_info(workflow, 'leak').artifact).cwl
    assert not authored or inlined['outputs']['mine']['outputSource'] == 'touch/file'
    with tempfile.TemporaryDirectory() as workdir:
        target = Path(workdir) / 'residue.cwl'
        target.write_text(yaml.safe_dump(inlined, sort_keys=False), encoding='utf-8')
        assert validate_cwl(str(target)) == 0
    assert inlined['cwlVersion'] == CWL_VERSION
