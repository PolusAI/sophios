"""One `RunConfig` for every place a run runs: its surface, and the pair rules of its outputs folder and `relative_to`,
which every door that takes one, a local run, a site and the compute service, reads in `runners.read_config` and
refuses the same."""
import inspect
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import given, strategies as st
from jsonschema import Draft202012Validator

import sophios.api.python.workflow as api
from sophios import compute, sites
from sophios.lang.diagnostics import SophiosError
from sophios.runtime import preflight, runners
from sophios.runtime.runners import RunConfig, SlurmSettings

from .budgets import FAST
from .cases import cases
from .helpers import echo_workflow


@pytest.mark.fast
def test_one_run_config_serves_every_place_and_says_only_what_its_setters_set() -> None:
    """`RunConfig` and `SlurmSettings` are one class each, the same object from the Python API, the compute client and
    the runners. A RunConfig's public surface is its constructor, which takes the outputs folder, and three setters,
    each returning the config so the calls chain; it has no public reader. `SlurmSettings` is six optional strings,
    given by keyword alone, and frozen."""
    assert api.RunConfig is compute.RunConfig is sites.RunConfig is runners.RunConfig
    assert api.SlurmSettings is compute.SlurmSettings is sites.SlurmSettings is runners.SlurmSettings
    assert {'RunConfig', 'SlurmSettings'} <= set(api.__all__)
    public = {name for name in dir(RunConfig) if not name.startswith('_')}
    assert public == {'relative_to', 'slurm_settings', 'log_level'}
    assert list(inspect.signature(RunConfig).parameters) == ['workflow_out_dir']
    config = RunConfig('results')
    assert config.relative_to('/lab').slurm_settings(SlurmSettings()).log_level('INFO') is config
    assert [(field.name, field.default) for field in fields(SlurmSettings)] == [
        (name, None) for name in ('partition', 'time', 'memory', 'steps_partition', 'steps_time', 'gpu_partition')]
    with pytest.raises(TypeError):
        SlurmSettings('normal')  # type: ignore[call-arg]  # pylint: disable=too-many-function-args  # by keyword alone
    with pytest.raises(FrozenInstanceError):
        SlurmSettings().time = '1:00:00'  # type: ignore[misc]  # frozen, as the test shows


def _local(config: RunConfig) -> list[str]:
    """A local run's refusal, as `Workflow.check` raises it."""
    try:
        echo_workflow().check(config=config)
    except ValueError as refused:
        return str(refused).splitlines()
    return []


def _compute(config: RunConfig) -> list[str]:
    """The compute client's refusal, before anything is sent."""
    try:
        compute.request_for('r1', {'class': 'Workflow', 'cwlVersion': 'v1.2'}, {}, config)
    except compute.RequestRefused as refused:
        return list(refused.messages)
    return []


def _site(config: RunConfig) -> list[str]:
    """A site's refusal, as `Machine.check` raises it before anything is compiled or sent; a config it takes reaches
    the transport, here a command no computer has (`wic062`)."""
    try:
        sites.Machine('ssh://pairs', runs='/runs', bin='/bin').check(echo_workflow(), config)
    except ValueError as refused:
        return str(refused).splitlines()
    except SophiosError as unreachable:
        assert 'sophios-no-such-ssh' in str(unreachable), unreachable
    return []


#: Every door that takes a RunConfig: whether it runs where there is no working directory, and its refusal's lines.
DOORS: Final[dict[str, tuple[bool, Callable[[RunConfig], list[str]]]]] = {
    'local': (False, _local), 'site': (True, _site), 'compute': (True, _compute)}


@pytest.mark.fast
@pytest.mark.parametrize('door', list(DOORS))
@pytest.mark.parametrize('case', cases('run-config-pairs.yaml', 'folder', 'relative_to', 'refused', 'remote_only',
                                       'site_refused'))
def test_every_door_refuses_the_pairs_of_folder_and_relative_to_the_rules_refuse(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, door: str, case: dict[str, Any]) -> None:
    """`relative_to` needs an outputs folder and is absolute; an absolute folder takes no `relative_to`; and a relative
    folder needs one, but in a local run, which reads it from the working directory. Each door refuses a pair with
    the same lines, in the user's words, every problem at once, before anything is compiled or sent; a site adds its
    own after them."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(preflight, 'ready', lambda *_args, **_kwargs: None)  # this machine is not the question
    # pylint: disable-next=protected-access  # the transport is the one thing a site's tests replace
    monkeypatch.setattr(sites._ssh, '_transport', lambda url: ['sophios-no-such-ssh'])
    remote, refusal = DOORS[door]
    config = RunConfig(case['folder'])
    if case['relative_to'] is not None:
        config.relative_to(case['relative_to'])
    refused = case['refused'] if remote or not case.get('remote_only', False) else []
    assert refusal(config) == [*refused, *(case.get('site_refused', []) if door == 'site' else [])]


@pytest.mark.fast
@pytest.mark.parametrize('door', list(DOORS))
def test_every_door_refuses_a_config_that_is_not_a_run_config(door: str) -> None:
    """The folder itself, given where a RunConfig goes, is refused by every door with the one line that says how to
    make one, before anything is compiled or sent."""
    folder: Any = 'results'
    assert DOORS[door][1](folder) == ["config='results' is not a RunConfig: make one, as RunConfig('results')"]


#: An outputs folder or a `relative_to`, made of what decides how a path is read: `/`, a `file:` URL in either case, a
#: name, `~`, a `$VARIABLE`, another scheme; or None, not given.
_SPELLINGS: Final = st.none() | st.lists(st.sampled_from(['/', 'file:', 'FILE:', '//', 'lab', '.', '~', '$HOME', ':',
                                                          'C:', ' ']), max_size=4).map(''.join)


@pytest.mark.fast
@FAST
@given(_SPELLINGS, _SPELLINGS)
def test_the_pair_rules_the_vendored_schema_also_holds_refuse_exactly_what_it_refuses(
        folder: str | None, relative_to: str | None) -> None:
    """Two of the rules are the schema's too: `relative_to` with no outputs folder (`basepath` requires
    `workflowOutDir`) and a `relative_to` that is not absolute (`basepath` starts with `/`). The compute client
    refuses a pair by one of them exactly as often as the vendored schema refuses the request the pair is sent as."""
    config = RunConfig(folder) if relative_to is None else RunConfig(folder).relative_to(relative_to)
    ours = [line for line in runners.read_config(config, local=False).problems
            if 'is given with no outputs folder' in line or 'is not an absolute path' in line]
    sent = {'workflowOutDir': folder, 'basepath': relative_to}
    request = {'id': 'r1', 'workflow': {'class': 'Workflow', 'cwlVersion': 'v1.2'}, 'inputs': {},
               **{field: value for field, value in sent.items() if value is not None}}
    assert len(ours) == len(list(Draft202012Validator(compute.SCHEMA).iter_errors(request))), (ours, request)
