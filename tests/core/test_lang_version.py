"""Language-version resolution, surfacing, and override.

The claims under test, one property each: the resolved version is the highest
that satisfies the source; no lower version is ever silently selected;
untagged files resolve rather than fail; every successful compile reports a
resolved version; the artifact annotation is present and keeps the CWL valid;
an explicit setting always beats a file tag; and one compilation yields
exactly one version, tree-wide.

The mechanism is exercised against fabricated version histories as well as the
real single-entry list — `sophios.lang.versions` states why, once. The claims
under test have their normative home in the reference, §7.
"""
from pathlib import Path
import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

import sophios.post_compile
from sophios.lang import KNOWN_VERSIONS, LANG_VERSION, resolve_lang_version
from sophios.lang.diagnostics import Code, SophiosError
from sophios.lang.versions import ANNOTATION_KEY, ANNOTATION_NAMESPACE, ANNOTATION_NAMESPACE_URI
from sophios.wic_types import StepId, Yaml, YamlTree

from .compile_harness import FAST, TOUCH, compile_cwl, compile_info

#: Fabricated, ordered version histories: what the resolver will face someday.
histories = st.lists(
    st.integers(min_value=0, max_value=40).map(lambda n: f'0.0.{n}'),
    min_size=1, max_size=6, unique=True,
).map(lambda vs: tuple(sorted(vs, key=lambda v: int(v.rsplit('.', 1)[1]))))


def _compile(yml: Yaml, lang_version: str | None = None) -> Yaml:
    """Compile one in-memory workflow and return the emitted CWL."""
    return compile_cwl(yml, 'lang_version', lang_version=lang_version)


# --------------------------------------------------------------------------
# The resolution rule, over fabricated histories
# --------------------------------------------------------------------------


@pytest.mark.fast
@given(histories)
@FAST
def test_untagged_resolves_to_the_newest(known: tuple[str, ...]) -> None:
    """With nothing to satisfy, the highest known version is chosen."""
    assert resolve_lang_version(None, (), known=known) == known[-1]


@pytest.mark.fast
@given(histories, st.data())
@FAST
def test_no_lower_version_is_silently_selected(known: tuple[str, ...], data: st.DataObject) -> None:
    """A pin is the only thing that moves resolution below the newest,
    and a pin is the author's word, not silence."""
    pin = data.draw(st.sampled_from(known))
    resolved = resolve_lang_version(None, (pin,), known=known)
    assert resolved == pin
    # Order is history position, not string order: '0.0.10' is newer than '0.0.2'.
    untagged = resolve_lang_version(None, (), known=known)
    assert known.index(untagged) >= known.index(resolved)


@pytest.mark.fast
@given(histories)
@FAST
def test_untagged_never_fails(known: tuple[str, ...]) -> None:
    """Resolution of an untagged tree cannot fail."""
    resolve_lang_version(None, (), known=known)  # must not raise


@pytest.mark.fast
def test_unknown_versions_are_reported_not_coerced() -> None:
    """Asking for a version that does not exist is an error naming the known
    ones — never a silent fallback to something else."""
    for request, pins in (('9.9.9', ()), (None, ('9.9.9',))):
        with pytest.raises(SophiosError) as caught:
            resolve_lang_version(request, pins)
        assert caught.value.diagnostics[0].code is Code.UNKNOWN_LANG_VERSION
        assert LANG_VERSION in caught.value.diagnostics[0].message


# --------------------------------------------------------------------------
# Precedence, and tree-wide oneness
# --------------------------------------------------------------------------


@pytest.mark.fast
@given(histories, st.data())
@FAST
def test_explicit_beats_any_tag(known: tuple[str, ...], data: st.DataObject) -> None:
    """The explicit setting wins regardless of what the tree pins."""
    explicit = data.draw(st.sampled_from(known))
    pins = tuple(data.draw(st.lists(st.sampled_from(known), max_size=4)))
    assert resolve_lang_version(explicit, pins, known=known) == explicit


@pytest.mark.fast
@given(histories, st.data())
@FAST
def test_one_version_or_a_conflict_report(known: tuple[str, ...], data: st.DataObject) -> None:
    """Agreeing pins resolve to their one version; disagreeing pins are
    a reported conflict, never an average or a quiet winner."""
    pins = tuple(data.draw(st.lists(st.sampled_from(known), min_size=1, max_size=4)))
    if len(set(pins)) == 1:
        assert resolve_lang_version(None, pins, known=known) == pins[0]
    else:
        with pytest.raises(SophiosError) as caught:
            resolve_lang_version(None, pins, known=known)
        assert caught.value.diagnostics[0].code is Code.LANG_VERSION_CONFLICT


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_conflicting_pins_across_a_tree_are_caught_at_the_root() -> None:
    """The same, through the compiler: a subworkflow pin that disagrees with the root's is a
    conflict at compile time — the walk sees the whole merged tree."""
    tree: Yaml = {
        'wic': {'lang_version': '0.0.1'},
        'steps': [{'id': 'touch',
                   'in': {'filename': {'wic_inline_input': 'empty.txt'}},
                   'wic': {'lang_version': '9.9.9'}}],
    }
    with pytest.raises(SophiosError) as caught:
        _compile(tree)
    assert caught.value.diagnostics[0].code is Code.UNKNOWN_LANG_VERSION


# --------------------------------------------------------------------------
# The resolved version is never a secret: every compile reports
# it (CLI, Python API), and the emitted CWL carries it as a namespaced
# annotation that keeps the artifact valid
# --------------------------------------------------------------------------


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_every_compile_reports_its_version() -> None:
    """The emitted artifact carries the resolved version, and so does the
    tagged and the explicitly-pinned compile."""
    assert _compile(TOUCH)[ANNOTATION_KEY] == LANG_VERSION
    assert _compile({'wic': {'lang_version': '0.0.1'}, **TOUCH})[ANNOTATION_KEY] == '0.0.1'
    assert _compile(TOUCH, lang_version='0.0.1')[ANNOTATION_KEY] == '0.0.1'


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_the_python_api_surfaces_the_version() -> None:
    """`CompiledWorkflow.lang_version` names the version, no digging."""
    from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
    from sophios.api.python.workflow import Step, Workflow

    emit = (CommandLineTool('emit_text',
                            Inputs(message=Input(cwl.string, position=1)),
                            Outputs(file=Output(cwl.file, glob='stdout.txt')))
            .base_command('echo')
            .stdout('stdout.txt'))
    step = Step(emit)
    step.inputs.message = 'hello'
    compiled = Workflow([step], 'p18_version').compile()
    assert compiled.lang_version == LANG_VERSION
    assert compiled.cwl_workflow[ANNOTATION_KEY] == LANG_VERSION


@pytest.mark.skip_pypi_ci
@pytest.mark.slow
def test_annotation_is_declared_and_the_cwl_stays_valid() -> None:
    """The annotation is a namespaced extension field, its namespace is
    declared, and cwltool still accepts the artifact as CWL v1.2."""
    import cwltool.main  # pylint: disable=import-outside-toplevel  # expensive; slow lane only

    info = compile_info(TOUCH, 'lang_version')
    inlined = sophios.post_compile.cwl_inline_runtag(info.rose).data.compiled_cwl

    assert inlined[ANNOTATION_KEY] == LANG_VERSION
    assert inlined['$namespaces'][ANNOTATION_NAMESPACE] == ANNOTATION_NAMESPACE_URI

    target = Path('autogenerated') / 'p19_annotated.cwl'
    target.parent.mkdir(exist_ok=True)
    target.write_text(yaml.safe_dump(inlined, sort_keys=False), encoding='utf-8')
    assert cwltool.main.main(['--validate', '--quiet', str(target)]) == 0


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_the_sophios_namespace_prefix_is_reserved() -> None:
    """A user binding of the `sophios` prefix is replaced by the canonical
    one, exactly like `edam` (reference §1, footnote): the annotation's
    meaning cannot be redirected by rebinding its namespace. Every other
    binding survives. Pinned as the actual behaviour, the CE-05 pattern."""
    compiled = _compile({'$namespaces': {'sophios': 'https://not-sophios/', 'mine': 'https://mine/'},
                         **TOUCH})
    assert compiled['$namespaces']['sophios'] == ANNOTATION_NAMESPACE_URI
    assert compiled['$namespaces']['mine'] == 'https://mine/'


# --------------------------------------------------------------------------
# Sanity
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_exactly_one_version_exists_today() -> None:
    """When this fails, versions.py has grown a second entry — go make sure
    the resolution table in the reference (§7) still tells the truth."""
    assert KNOWN_VERSIONS == ('0.0.1',)
    assert LANG_VERSION == '0.0.1'


@pytest.mark.skip_pypi_ci
@pytest.mark.fast
def test_a_file_tag_survives_schema_validation() -> None:
    """The `wic: lang_version:` pin reaches the compiler through the real path.

    Every other test here hands a tree straight to `compile_workflow`, which
    skips the step a real `.wic` file cannot skip: `read_ast_from_disk`
    validates against the generated wic schema first, and that schema closes
    the `wic:` block with `additionalProperties: False`. A key the schema does
    not list is rejected there, so the tag can be specified, parsed, resolved
    and surfaced and still be dead on arrival for anyone writing a file.

    That is what happened — `lang_version` was added to §7, to the resolver
    and to the compiler, but not to the one list that decides whether a `wic:`
    key is allowed to exist.
    """
    import sophios.ast
    import sophios.cli

    from .test_setup import tools_cwl, validator, yml_paths

    pinned: Yaml = {'wic': {'lang_version': LANG_VERSION}, **TOUCH}
    tree = YamlTree(StepId('pinned.wic', 'global'), pinned)
    args = sophios.cli.get_args('pinned.wic')

    # Must not raise: the validator is the gate the compiler sits behind.
    sophios.ast.read_ast_from_disk(args.homedir, tree, yml_paths, tools_cwl, validator, False)


@pytest.mark.fast
def test_the_cli_flag_reaches_the_compiler() -> None:
    """`--lang_version` survives the trip from argv to compiler options.

    The flag's entire contract is precedence — an explicit setting beats a
    file pin — and it could not keep it: `main` parsed the real argv, then
    `_build_and_compile_workflow` threw those arguments away and called
    `get_dicts_for_compilation()`, which re-parses a patched
    `sys.argv` of `['sophios', '--yaml', '']`. Every flag arrived at the
    compiler as its default, so `--lang_version 9.9.9` compiled at the newest
    version instead of reporting an unknown one.

    Tested at the seam because that is where the value was being dropped. The
    caller side needs no test of its own any more: `get_dicts_for_compilation`
    requires its argument, so a `main` that forgets to pass what it parsed is
    a TypeError and a mypy failure rather than a silent compile with defaults.
    The guarantee moved from a test into the signature.
    """
    import sophios.cli

    supplied = sophios.cli.get_args('wf.wic', ['--lang_version', '9.9.9'])
    options, _graph_settings, _tag_paths = sophios.cli.get_dicts_for_compilation(supplied)
    assert options['lang_version'] == '9.9.9', 'the flag never reached the compiler'

    # And a library caller asking for defaults gets no pin, as it should.
    defaults, _g, _t = sophios.cli.default_compilation_settings()
    assert defaults['lang_version'] is None


@pytest.mark.fast
def test_pin_collection_survives_recursive_trees() -> None:
    """CE-07: the pins walk must not recurse forever on a cyclic tree.

    The loader constructs self-referential structures from YAML aliases, so
    the walk meets them through the real compile path. Found by applying the
    parser's cycle-guard lesson as an audit lens across the stack — the same
    defect class, recurring in code written after the lesson.
    """
    import yaml as _yaml

    from sophios.compiler import _lang_version_pins
    from sophios.utils_yaml import wic_loader

    cyclic = _yaml.load('steps: &a\n- id: s\n  wic: {x: *a}\n', Loader=wic_loader())
    assert _lang_version_pins(cyclic) == ()  # must not raise

    pinned = _yaml.load('wic: {lang_version: 0.0.1}\nsteps: &a\n- id: s\n  wic: {x: *a}\n',
                        Loader=wic_loader())
    assert _lang_version_pins(pinned) == ('0.0.1',)  # acyclic regions still visited


@pytest.mark.fast
def test_a_mistyped_pin_is_reported_not_ignored() -> None:
    """`lang_version: 1.0` is a YAML float, not a string. A walk that only
    collected strings made it vanish — silently inferred over, when the
    author plainly asked for something, and by the no-silent-selection rule
    above, silence is the one wrong answer. Any value under the key is a pin
    claim; a non-version is reported as unknown, naming what was written."""
    import yaml as _yaml

    from sophios.compiler import _lang_version_pins
    from sophios.utils_yaml import wic_loader

    mistyped = _yaml.load('wic: {lang_version: 1.0}\nsteps:\n- id: s\n', Loader=wic_loader())
    assert _lang_version_pins(mistyped) == ('1.0',)

    with pytest.raises(SophiosError) as caught:
        resolve_lang_version(None, _lang_version_pins(mistyped))
    assert caught.value.diagnostics[0].code is Code.UNKNOWN_LANG_VERSION
    assert "'1.0'" in caught.value.diagnostics[0].message
