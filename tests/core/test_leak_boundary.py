"""The leak boundary, checked against the real compiler.

Four claims, each a property below: passthrough keys survive compilation
byte-identically; no key is both interpreted and passed through; the
interpreted set is exhaustive, so the compiler is inert to everything else;
and the stripped residue validates as CWL v1.2 under cwltool.

These run the actual `compile_workflow`, not the syntax layer, because the
boundary being specified is the compiler's behaviour. The syntax layer's
partition is checked exhaustively over the declared set as well, since
that is what the schema and the reference derive from.

The keys the compiler owns are the documented exceptions. Their normative
statement lives in the reference's §1 footnote — the single home — and each is
enforced by a named test below rather than by a property: `requirements`,
`inputs` and `outputs` are merged into, `$schemas` is append-only,
`$namespaces` reserves the `edam` prefix, and `class` and `cwlVersion` are
written.
A property broad enough to cover them would have to be weak enough to say
nothing, so the properties quantify over the keys that really are untouched
and the exceptions are pinned one at a time.

See design_docs/core-refactor-design.md §5.4.
"""
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import sophios.post_compile
from sophios.lang import Grammar, parse
from sophios.lang.cwl import CWL_VERSION
from sophios.wic_types import Yaml

from .compile_harness import COMPILED, FAST, compile_cwl, compile_info

#: Step keys the language claims for itself, and therefore not passthrough.
#: `wic:` is claimed at every level: §1 lists the block as Sophios-owned, and
#: a step-level `wic:` is metadata about the step, not CWL bound for the
#: output — so a property that generated it and then asserted it was
#: passthrough would be asserting against the reference.
CLAIMED_STEP_KEYS = frozenset({'id', 'in', 'out', 'wic'}) | Grammar.INTERPRETED_STEP_KEYS

#: Top-level keys the *compiler* owns, which is a larger set than the syntax
#: layer's. `_document` claims only `steps` and `wic`; `compile_workflow` also
#: writes `class` and `cwlVersion`, merges into `inputs`, `outputs` and
#: `requirements`, and reads `outputSource` out of the `outputs` you supply. These properties drive the compiler, so they must
#: exclude what the compiler owns — the old filter named the parser's two and
#: was saved from the rest only by the improbability of `st.text` spelling
#: `class`, which is safety by luck rather than by statement.
COMPILER_OWNED_TOP_KEYS = frozenset({
    'steps', 'wic', 'class', 'cwlVersion', 'inputs', 'outputs', 'requirements',
    '$namespaces', '$schemas',
})


def _compile(yml: Yaml) -> Yaml:
    """Compile one in-memory workflow and return the emitted CWL."""
    return compile_cwl(yml, 'leak')


def _step(cwl: Yaml) -> Yaml:
    """The single step of a compiled one-step workflow.

    Always a list: the compiler normalises mapping-form `steps:` away before
    emitting (`yaml_tree.update({'steps': steps_list})`), so the emitted
    document has only one shape whatever the source had.

    Returns the live sub-dict, not a copy — callers compare documents after
    mutating what they get back.
    """
    found: Yaml = cwl['steps'][0]
    return found


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

#: Keys that are definitely not claimed by the language. Filtered rather than
#: constructed, so the claim set stays the authority.
#:
#: The sampled half matters: a lowercase-ascii alphabet cannot produce `$` or
#: an uppercase letter, so `$namespaces`, `$schemas` and `scatterMethod` were
#: unreachable by construction and no property here ever touched the keys the
#: reference's footnote is about. Real CWL keys are drawn explicitly.
passthrough_keys = st.one_of(
    st.text('abcdefghijklmnopqrstuvwxyz_', min_size=3, max_size=12),
    st.sampled_from(['$namespaces', '$schemas', 'hints', 'label', 'doc', 'scatterMethod']),
).filter(lambda k: k not in CLAIMED_STEP_KEYS)

#: JSON-shaped values, byte-comparable after a round trip.
passthrough_values = st.recursive(
    st.one_of(st.integers(min_value=-100, max_value=100), st.booleans(),
              st.text('abc xyz', max_size=8), st.none()),
    lambda children: st.one_of(st.lists(children, max_size=3),
                               st.dictionaries(st.text('abc', min_size=1, max_size=5), children, max_size=3)),
    max_leaves=8,
)


def _touch_workflow(extra_step_keys: dict[str, Any], extra_top_keys: dict[str, Any]) -> Yaml:
    """A minimal real workflow carrying the given passthrough freight."""
    return {
        **extra_top_keys,
        'steps': [{'id': 'touch',
                   'in': {'filename': {'wic_inline_input': 'empty.txt'}},
                   **extra_step_keys}],
    }


# --------------------------------------------------------------------------
# Passthrough fidelity: unclaimed keys survive byte-identically
# --------------------------------------------------------------------------


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(st.dictionaries(passthrough_keys, passthrough_values, min_size=1, max_size=4))
@COMPILED
def test_step_passthrough_is_byte_identical(freight: dict[str, Any]) -> None:
    """A step key outside the claimed set survives compilation unchanged."""
    compiled = _compile(_touch_workflow(freight, {}))
    step = _step(compiled)
    for key, value in freight.items():
        assert step[key] == value, f'{key} was altered by compilation'


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(st.dictionaries(passthrough_keys.filter(lambda k: k not in COMPILER_OWNED_TOP_KEYS),
                       passthrough_values, min_size=1, max_size=3))
@COMPILED
def test_top_level_passthrough_is_byte_identical(freight: dict[str, Any]) -> None:
    """An uninterpreted top-level key survives compilation unchanged."""
    compiled = _compile(_touch_workflow({}, freight))
    for key, value in freight.items():
        assert compiled[key] == value, f'{key} was altered by compilation'


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_user_namespaces_survive_except_edam() -> None:
    """The one qualification: `$namespaces` keeps every user binding but `edam`.

    CE-05. The compiler merges `{'edam': the canonical URL}` over the user's
    mapping, so a user binding of the `edam` prefix specifically is replaced.
    Pinned as the actual behaviour; if merging ever becomes pure passthrough,
    this test should fail and be inverted deliberately, not silently.
    """
    compiled = _compile(_touch_workflow({}, {'$namespaces': {'mine': 'https://mine/', 'edam': 'https://not-edam/'}}))
    assert compiled['$namespaces']['mine'] == 'https://mine/'
    assert compiled['$namespaces']['edam'] == 'https://edamontology.org/'  # user's binding lost


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_class_is_written_while_inputs_outputs_and_version_are_not() -> None:
    """The other four compiler-owned keys, each behaving differently.

    These four had no test behind them, and the footnote claiming each was
    pinned was wrong about three: `inputs`/`outputs` are merged, not written
    (`{**yours, **generated}`, so your entry survives unless the compiler
    generates the same name), and `cwlVersion` used to be *defaulted* — a
    document declaring `v1.0` kept it and still received `when:`, emitting CWL
    that cwltool rejects against the version it names. The compiler now writes
    it. A row asserting facts about the compiler needs a test per fact, or it
    is prose that drifts.
    """
    # `outputs` entries are *read*, not merely kept: the compiler pulls
    # `outputSource` out of each one, so a supplied entry needs a real one.
    user_output = {'type': 'File', 'outputSource': 'touch/file'}
    compiled = _compile({
        'class': 'ExpressionTool',        # a lie the compiler must overwrite
        'inputs': {'mine': {'type': 'string'}},
        'outputs': {'mine': user_output},
        'cwlVersion': 'v1.0',             # older than the declared substrate
        **_touch_workflow({}, {}),
    })

    assert compiled['class'] == 'Workflow', 'class is written, not merged'
    assert compiled['inputs']['mine'] == {'type': 'string'}, 'user input lost'
    assert len(compiled['inputs']) > 1, 'compiler inputs not merged alongside'
    assert compiled['outputs']['mine'] == user_output, 'user output lost'
    assert len(compiled['outputs']) > 1, 'compiler outputs not merged alongside'

    # The compiler wins. Sophios generates for one substrate version, so a
    # document cannot claim another and still be valid: declaring v1.0 and
    # receiving `when:` produced CWL that cwltool rejects. The tag is ignored
    # with a warning rather than refused — a stale pin should not stop a
    # workflow compiling.
    assert compiled['cwlVersion'] == CWL_VERSION, 'a supplied cwlVersion must not win'


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_user_requirements_are_merged_into_not_copied() -> None:
    """`requirements` keeps what you wrote and gains what the workflow needs.

    The third compiler-owned key, and the one the reference used to list as an
    example of pure passthrough. `maybe_add_requirements` fires whenever a step
    scatters, uses `when` or `valueFrom`, or names a `.wic` subworkflow — so a
    workflow that supplies its own `requirements` gets them back extended, not
    unchanged. The generated properties never see it: their one step is a plain
    tool step, so the requirement set is empty and the merge is skipped on
    every example.
    """
    compiled = _compile({
        'requirements': {'ResourceRequirement': {'coresMin': 4}},
        'steps': [{'id': 'touch',
                   'in': {'filename': {'wic_inline_input': 'empty.txt'}},
                   'scatter': ['filename']}],
    })
    requirements = compiled['requirements']
    assert requirements['ResourceRequirement'] == {'coresMin': 4}, 'user entry lost'
    assert 'ScatterFeatureRequirement' in requirements, 'scatter requirement not added'


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_user_schemas_are_kept_and_appended_to() -> None:
    """`$schemas` is append-only: user entries survive, Sophios adds its own."""
    compiled = _compile(_touch_workflow({}, {'$schemas': ['https://mine/schema.owl']}))
    assert 'https://mine/schema.owl' in compiled['$schemas']


# --------------------------------------------------------------------------
# The interpreted/passthrough partition really is a partition
# --------------------------------------------------------------------------


@pytest.mark.fast
@pytest.mark.parametrize('key', sorted(Grammar.INTERPRETED_STEP_KEYS))
def test_every_declared_key_is_interpreted(key: str) -> None:
    """Exhaustive half: each declared key routes to `interpreted`."""
    result = parse(f'steps:\n- id: s\n  {key}: x\n', 'p11.wic')
    assert result.document is not None
    step = result.document.steps[0]
    assert key in dict(step.interpreted)
    assert key not in dict(step.passthrough)


@pytest.mark.fast
@given(passthrough_keys)
@FAST
def test_no_key_lands_on_both_sides(key: str) -> None:
    """Generated half: any other key routes to passthrough, never both."""
    result = parse(f'steps:\n- id: s\n  {key}: x\n', 'p11.wic')
    assert result.document is not None
    step = result.document.steps[0]
    interpreted, passthrough = dict(step.interpreted), dict(step.passthrough)
    assert key in passthrough
    assert key not in interpreted
    assert not set(interpreted) & set(passthrough)


# --------------------------------------------------------------------------
# The compiler is inert outside the declared set
# --------------------------------------------------------------------------


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@given(passthrough_keys, passthrough_values)
@COMPILED
def test_unknown_keys_change_nothing_else(key: str, value: Any) -> None:
    """Adding an uninterpreted key changes exactly that key and nothing
    else in the output — the observable meaning of "not interpreted".
    """
    without = _compile(_touch_workflow({}, {}))
    with_key = _compile(_touch_workflow({key: value}, {}))

    # The whole document, not just the step: `maybe_add_requirements` reads
    # step keys and writes a *top-level* key, so a step key reaching that path
    # would change the document while leaving the step subtree identical — and
    # comparing only the step would call that inert. `_step` returns a
    # reference into `with_key`, so popping there removes the key from the
    # document being compared.
    assert _step(with_key).pop(key) == value
    assert with_key == without, f'{key} altered more than itself'


# --------------------------------------------------------------------------
# The residue is real CWL v1.2
# --------------------------------------------------------------------------

#: Passthrough spelled in CWL vocabulary, because CWL v1.2 itself rejects
#: unknown step fields — arbitrary keys are covered by the fidelity properties
#: above, but a document carrying them is not *valid CWL*, and this last claim
#: is about validity.
cwl_passthrough = st.fixed_dictionaries(
    {},
    optional={
        'label': st.text('abc xyz', min_size=1, max_size=20),
        'doc': st.text('abc xyz.', min_size=1, max_size=40),
        'hints': st.just({'LoadListingRequirement': {'loadListing': 'shallow_listing'}}),
    },
)


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
# Ten examples, not the shared hundred: `cwl_passthrough` varies only in which
# of three optional keys are present — eight shapes — and the free text inside
# `label`/`doc` cannot change whether a document is valid CWL. Hypothesis keeps
# drawing new strings and so never detects the exhaustion, spending about a
# minute and a half of CI on a claim eight examples settle.
@given(cwl_passthrough)
@settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_residue_validates_as_cwl_v1_2(freight: dict[str, Any]) -> None:
    """Strip the Sophios syntax, inline the tools, and cwltool agrees the
    residue is CWL v1.2."""
    import cwltool.main  # pylint: disable=import-outside-toplevel  # expensive; slow lane only

    info = compile_info(_touch_workflow(freight, {}), 'leak')
    inlined = sophios.post_compile.cwl_inline_runtag(info.rose).data.compiled_cwl

    # A throwaway file, not a repo artifact: it exists only to hand cwltool a
    # path. `tmp_path` would be the obvious choice, but it is function-scoped
    # and Hypothesis rightly refuses to reuse one fixture across examples.
    with tempfile.TemporaryDirectory() as workdir:
        target = Path(workdir) / 'residue.cwl'
        target.write_text(yaml.safe_dump(inlined, sort_keys=False), encoding='utf-8')
        assert cwltool.main.main(['--validate', '--quiet', str(target)]) == 0
    assert inlined['cwlVersion'] == CWL_VERSION
