"""Regression coverage for `sophios.compiler` bugs that have no home elsewhere."""
import copy
from pathlib import Path
from typing import Any

import pytest
from jsonschema.validators import Draft202012Validator

import yaml

from sophios import compiler, cwl_subinterpreter, utils
from sophios.utils_yaml import wic_loader
from sophios.wic_types import StepId, Tool, Yaml

from .hermetic import compile_hermetic_cwl
from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS


class _Captured(Exception):
    """Raised by the stub to stop `rerun_cwltool` once it has built its document."""


@pytest.mark.fast
@pytest.mark.parametrize(('cwl_tool', 'config'), [
    ('tool', {'id': 'elsewhere', 'in': {}}),
    ('tool.wic', {'in': {}}),
])
def test_rerun_cwltool_builds_an_id_form_step(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        cwl_tool: str, config: Yaml) -> None:
    """`rerun_cwltool` builds a step `get_steps_keys` can read back, on both branches.

    Asserting on literals copied into the test proves nothing about the code:
    the documents are built inside `rerun_cwltool`, so the test has to get them
    from there. Each branch hands its document to exactly one function, which is
    the seam to stub — the CWL runner is never reached, and the cache directory
    is never touched. `_Captured` is not a `FileNotFoundError`, so the
    function's own handler does not swallow it. The validator is never reached
    on either branch, but it is a real one rather than a `None` the signature
    does not admit.

    One row's config carries an `id` of its own, so the step the branch builds
    is only named for the tool if the config cannot overwrite it.
    """
    seen: list[Yaml] = []

    def capture(*args: Any, **_: Any) -> None:
        # Both branches end at the same door now, and it is handed text, so the
        # document the branch built is read back from the source it bundled.
        seen.append(yaml.load(args[0].source, Loader=wic_loader()))
        raise _Captured

    monkeypatch.setattr(compiler, 'compile_source', capture)
    with pytest.raises(_Captured):
        cwl_subinterpreter.rerun_cwltool(
            '', tmp_path, tmp_path, cwl_tool, config, {}, {},
            Draft202012Validator({}), tmp_path)

    assert [utils.require_step_id(step) for step in seen[0]['steps']] == [cwl_tool]


@pytest.mark.fast
def test_a_referenced_input_keeps_the_documentation_the_user_wrote() -> None:
    """References neither append empty text nor synthesize absent fields."""
    documented: Yaml = {
        'inputs': {'wf_name': {'type': 'string', 'doc': 'mine', 'label': 'keep me'}},
        'steps': [{'id': 'mk_text', 'in': {'name': 'wf_name'}},
                  {'id': 'mk_file', 'in': {'name': 'wf_name'}}],
    }
    kept = compile_hermetic_cwl(documented, 'docs')['inputs']['wf_name']
    assert kept == {'type': 'string', 'doc': 'mine', 'label': 'keep me'}

    bare: Yaml = {
        'inputs': {'wf_name': {'type': 'string'}},
        'steps': [{'id': 'mk_text', 'in': {'name': 'wf_name'}}],
    }
    assert compile_hermetic_cwl(bare, 'docs')['inputs']['wf_name'] == {'type': 'string'}


@pytest.mark.fast
@pytest.mark.parametrize(('claim', 'user_doc', 'argument_doc', 'expected'), [
    ('argument doc is a list', 'mine', ['one', 'two'], 'mine\none\ntwo'),
    ('user doc is a list', ['one', 'two'], 'theirs', 'one\ntwo\ntheirs'),
    ('both are lists', ['a', 'b'], ['c', 'd'], 'a\nb\nc\nd'),
    ('neither is', 'mine', 'theirs', 'mine\ntheirs'),
])
def test_a_list_valued_doc_is_joined_rather_than_repr_d(
        claim: str, user_doc: Any, argument_doc: Any, expected: str) -> None:
    """CWL types `doc` as `string | string[]`, and either side of the merge may be a list.

    Interpolating a list into the join writes its Python repr into the very
    block this code exists to preserve. `label` is string-only in CWL, so only
    `doc` is affected.
    """
    documented = copy.deepcopy(SYNTHETIC_TOOLS)
    key = StepId('mk_text', SYNTHETIC_NS)
    documented[key] = Tool(documented[key].run_path,
                           copy.deepcopy(documented[key].cwl))
    documented[key].cwl['inputs']['name']['doc'] = argument_doc

    listed: Yaml = {
        'inputs': {'wf_name': {'type': 'string', 'doc': user_doc}},
        'steps': [{'id': 'mk_text', 'in': {'name': 'wf_name'}}],
    }
    doc = compile_hermetic_cwl(listed, 'docs', tools=documented)['inputs']['wf_name']['doc']
    assert doc == expected, claim


@pytest.mark.fast
def test_a_documented_argument_still_documents_the_input_it_binds() -> None:
    """Skipping empty additions does not disable useful documentation propagation."""
    from copy import deepcopy  # pylint: disable=import-outside-toplevel

    from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS  # pylint: disable=import-outside-toplevel

    tools = deepcopy(SYNTHETIC_TOOLS)
    tools[StepId('mk_text', SYNTHETIC_NS)].cwl['inputs']['name']['doc'] = 'the file name'
    document: Yaml = {
        'inputs': {'wf_name': {'type': 'string'}},
        'steps': [{'id': 'mk_text', 'in': {'name': 'wf_name'}}],
    }
    compiled = compile_hermetic_cwl(document, 'docs', tools=tools)
    assert compiled['inputs']['wf_name']['doc'] == 'the file name'


@pytest.mark.fast
def test_two_documentations_are_joined_by_one_real_newline() -> None:
    """The join is the only branch that writes a separator, and the separator
    is a newline character, not the two-character escape the previous spelling
    emitted (`'\\\\n'` in single quotes is a backslash followed by an `n`).

    Exact equality, not `in`: a doc that reads `mine\\\\nthe file name` in every
    renderer that shows it is the defect, and `'the file name' in doc` holds
    just as well for it.
    """
    from copy import deepcopy  # pylint: disable=import-outside-toplevel

    from .synthetic_tools import SYNTHETIC_NS, SYNTHETIC_TOOLS  # pylint: disable=import-outside-toplevel

    tools = deepcopy(SYNTHETIC_TOOLS)
    argument = tools[StepId('mk_text', SYNTHETIC_NS)].cwl['inputs']['name']
    argument['doc'] = 'the file name'
    argument['label'] = 'File name'
    document: Yaml = {
        'inputs': {'wf_name': {'type': 'string', 'doc': 'mine', 'label': 'keep me'}},
        'steps': [{'id': 'mk_text', 'in': {'name': 'wf_name'}}],
    }

    assert compile_hermetic_cwl(document, 'docs', tools=tools)['inputs']['wf_name'] == {
        'type': 'string', 'doc': 'mine\nthe file name', 'label': 'keep me\nFile name'}
