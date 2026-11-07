"""Behavioural coverage for `sophios.plugins` transforms that reach emitted CWL.

`sophios.plugins` is FORBIDDEN to the oracle (`test_hermeticity.py`),
because importing it reads the config and walks the adapter search paths. That
makes the oracle unable to check the plugin transforms directly, so the ones
whose output lands in an emitted document are covered here instead.

Discovery is covered here too: which files are plugins, and which file a stem resolves to.
"""
import copy
import glob
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from hypothesis import example, given, strategies as st

import sophios.main
import sophios.plugins
import sophios.post_compile
import sophios.preflight
from sophios.wic_types import Yaml

from .budgets import budget
from .cases import cases


@pytest.mark.fast
@pytest.mark.parametrize(('code_range', 'direct', 'codes'), [
    ([3, 6], [9, 4, 1, 32], [0, 1, 3, 4, 5, 9, 32]), ([0, 1], [], [0])])
def test_partial_failure_takes_ordered_success_codes_and_makes_outputs_optional(
        code_range: list[int], direct: list[int], codes: list[int]) -> None:
    """`successCodes` is emitted CWL under `--partial_failure_enable`, so no set's iteration order may reach it: the
    first row's codes are chosen so that a set's order is not the sorted one. Each row checks both halves of the
    transform, so neither passes against a function that stopped doing the other."""
    tool: Yaml = {'class': 'CommandLineTool', 'baseCommand': 'true', 'inputs': {},
                  'outputs': {'o': {'type': 'File', 'outputBinding': {'glob': 'x'}}}}
    updated = sophios.plugins.cwl_update_outputs_optional(tool, code_range, direct)
    assert (updated['successCodes'], updated['outputs']['o']['type']) == (codes, 'File?')


def _section(before: list[Yaml], fields: Yaml | None, after: list[Yaml], mapping: bool) -> Yaml | list[Yaml]:
    """A `requirements` or `hints` holding `before`, a `DockerRequirement` of `fields` unless they are None, and
    `after`: a mapping keyed by class (hand-written adapters) or a list of `{class: ...}` entries (cwl_utils)."""
    entries = [*before, *([] if fields is None else [{'class': 'DockerRequirement', **fields}]), *after]
    if mapping:
        return {entry['class']: {k: v for k, v in entry.items() if k != 'class'} for entry in entries}
    return entries


def _tool(sections: Yaml) -> Yaml:
    return {'class': 'CommandLineTool', 'baseCommand': 'true', 'inputs': {}, 'outputs': {}, **sections}


def _workflow(runs: list[Any], mapping: bool, sections: Yaml) -> Yaml:
    """A workflow running each of `runs`, an embedded process or a file, its steps a mapping or a list."""
    steps = [{'id': f's{i}', 'in': {}, 'out': [], 'run': run} for i, run in enumerate(runs)]
    return {'class': 'Workflow', 'inputs': {}, 'outputs': {}, **sections,
            'steps': {step.pop('id'): step for step in steps} if mapping else steps}


def _pulls(section: str, image: str, mapping: bool = True) -> Yaml:
    return _tool({section: _section([], {'dockerPull': image}, [], mapping)})


#: Image references: a registry, with a port or none, 1 to 3 path segments, a tag or none, and a digest or none.
_IMAGES = st.from_regex(r'((docker\.io|localhost:5000|ghcr\.io)/)?([a-z0-9]+([._-][a-z0-9]+)?/){0,2}[a-z0-9]+'
                        r'(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,9})?(@sha256:8{64})?', fullmatch=True)
#: Requirements beside a `DockerRequirement`, which may itself name no image to pull.
_OTHERS = st.lists(st.sampled_from([{'class': 'InlineJavascriptRequirement'},
                                    {'class': 'ResourceRequirement', 'ramMin': 64}]), max_size=2)
_DOCKER = st.one_of(st.none(), st.just({'dockerImageId': 'local'}), _IMAGES.map(lambda image: {'dockerPull': image}))
_SECTIONS = st.dictionaries(st.sampled_from(['requirements', 'hints']),
                            st.builds(_section, _OTHERS, _DOCKER, _OTHERS, st.booleans()))
_PROCESSES = st.recursive(_SECTIONS.map(_tool), lambda processes: st.builds(
    _workflow, st.lists(st.one_of(processes, st.just('elsewhere.cwl')), min_size=1, max_size=3), st.booleans(),
    _SECTIONS), max_leaves=6)


def _tagged(document: Any) -> Any:
    """`document` with each `dockerPull` at any depth tagged as docker-py's own parser reads its image."""
    if isinstance(document, list):
        return [_tagged(value) for value in document]
    if not isinstance(document, dict):
        return document
    tagged = {key: _tagged(value) for key, value in document.items()}
    if 'dockerPull' in document and '@' not in document['dockerPull']:
        repo, tag = sophios.post_compile.docker.utils.parse_repository_tag(  # type: ignore[attr-defined]
            document['dockerPull'])
        tagged['dockerPull'] = f"{repo}:{tag or 'latest'}-noentrypoint"
    return tagged


_DIGEST = 'polusai/openfold-tool@sha256:' + '8' * 64


@pytest.mark.fast
@budget(100)
@given(_PROCESSES)
@example(_pulls('requirements', 'docker.io/bash:4.4'))
@example(_pulls('requirements', 'docker.io/bash:4.4', mapping=False))
@example(_pulls('hints', 'localhost:5000/tools/bash:4.4'))
@example(_pulls('hints', 'localhost:5000/tools/bash:4.4', mapping=False))
@example(_pulls('requirements', _DIGEST))
@example(_pulls('hints', _DIGEST))
@example(_workflow([_pulls('requirements', 'docker.io/bash:4.4'),
                    _workflow([_pulls('hints', 'bash:5')], True, {}), 'elsewhere.cwl'], False, {}))
def test_every_image_the_runner_pulls_gets_the_noentrypoint_tag(document: Yaml) -> None:
    """Under `--docker_remove_entrypoints` each `dockerPull` the runner reads, in `requirements` (which cwl_utils
    renders as a list, and hand-written adapters as a mapping) or in `hints` (cwltool runs the image a hint names),
    of the document and of each tool embedded in its steps, at any depth (as `--cwl_inline_runtag` and
    `Workflow.run()` give the runner one), names the tag built; an image pulled by digest has no tag and no
    -noentrypoint build, so appending to it would make an invalid reference. Tagging twice is tagging once, and the
    document given is not changed."""
    before = copy.deepcopy(document)
    updated = sophios.plugins.dockerPull_append_noentrypoint(document)
    assert document == before
    assert updated == _tagged(document)
    assert sophios.plugins.dockerPull_append_noentrypoint(updated) == updated


def _docker_pulls(document: Any) -> list[str]:
    """Every `dockerPull` in `document`, at any depth."""
    if isinstance(document, dict):
        own = [document['dockerPull']] if isinstance(document.get('dockerPull'), str) else []
        return own + [pull for value in document.values() for pull in _docker_pulls(value)]
    if isinstance(document, list):
        return [pull for value in document for pull in _docker_pulls(value)]
    return []


def _no_images() -> SimpleNamespace:
    """A docker client with no images, so `--docker_remove_entrypoints` builds nothing."""
    return SimpleNamespace(images=SimpleNamespace(list=lambda: []))


@pytest.mark.fast
def test_the_cli_runs_the_noentrypoint_tags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The images are built after the pre-flight pulls the originals; the CWL the runner reads
    must then name the built ones."""
    monkeypatch.setattr(sophios.preflight, 'prepare', lambda *_a, **_k: None)
    monkeypatch.setattr(sophios.post_compile.docker, 'from_env', _no_images)
    monkeypatch.chdir(tmp_path)
    workflow = tmp_path / 'touch_one.wic'
    workflow.write_text('steps:\n  touch:\n    in:\n      filename: !ii empty.txt\n', encoding='utf-8')
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', str(workflow), '--generate_run_script',
                                     '--docker_remove_entrypoints'])
    sophios.main.main()
    pulls = [pull for written in sorted((tmp_path / 'autogenerated').glob('*.cwl'))
             for pull in _docker_pulls(yaml.safe_load(written.read_text(encoding='utf-8')))]
    # touch.cwl runs in docker.io/library/debian:bookworm-slim
    assert pulls == ['docker.io/library/debian:bookworm-slim-noentrypoint']


#: A file every tool case writes for each of its `files`; a workflow case writes `steps: {}`.
_TOOL = 'cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: true\ninputs: {}\noutputs: {}\n'

#: A warning `_warn_duplicates` prints, read back as its stem, namespace, count, files and the file used.
_WARNING = re.compile(r"Warning! (tool|workflow) '(.+)' in namespace '(.+)' is defined by (\d+) files:\n"
                      r"((?:  .+\n)+)Using (.+?): it is the last one found .*\nTo use another.*\n")

#: The filesystem's own listing.
_LISTED = glob.glob


def _listed_in_reverse(pattern: str, *, recursive: bool = False) -> list[str]:
    """A directory's entries in reverse, as some filesystems list them."""
    return sorted(_LISTED(pattern, recursive=recursive), reverse=True)


def _relative(path: str | Path, base: Path) -> str:
    return Path(os.path.relpath(path, base)).with_suffix('').as_posix()


def _found(kind: str, roots: dict[str, list[str]], base: Path) -> dict[str, dict[str, str]]:
    """The file each stem resolves to, by namespace, in the order discovery returns them."""
    if kind == 'workflow':
        return {ns: {stem: _relative(path, base) for stem, path in stems.items()}
                for ns, stems in sophios.plugins.get_yml_paths({'search_paths_wic': roots}).items()}
    found: dict[str, dict[str, str]] = {}
    for key, tool in sophios.plugins.get_tools_cwl({'search_paths_cwl': roots}).items():
        found.setdefault(key.plugin_ns, {})[key.stem] = _relative(tool.run_path, base)
    return found


def _warnings(err: str, base: Path, kind: str) -> list[list[Any]]:
    """Each warning on `err` as [namespace, stem, its files], which names `kind`, states their count and uses the last;
    `err` holds nothing else."""
    said = []
    for named_kind, stem, namespace, count, files, used in _WARNING.findall(err):
        named = [_relative(line[2:], base) for line in files.splitlines()]
        assert (named_kind, int(count), _relative(used, base)) == (kind, len(named), named[-1])
        said.append([namespace, stem, named])
    assert _WARNING.sub('', err) == ''
    return said


@pytest.mark.fast
@pytest.mark.parametrize('case', cases('discovery', 'files', 'found', 'kinds', 'roots', 'warnings', 'links'))
def test_each_stem_resolves_to_the_last_file_found(case: Yaml, tmp_path_factory: pytest.TempPathFactory,
                                                   monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """Each case of data/discovery (see its README), under the filesystem's own listing and under it reversed:
    discovery must not depend on it."""
    base = tmp_path_factory.mktemp('layout')   # not tmp_path, which holds the case id that biobb_md and _inputs read
    for link, target in case.get('links', {}).items():
        (base / link).symlink_to(base / target, target_is_directory=True)
    for kind in case.get('kinds', ['tool']):
        extension = 'cwl' if kind == 'tool' else 'wic'
        for name in case['files']:
            (base / name).parent.mkdir(parents=True, exist_ok=True)
            (base / f'{name}.{extension}').write_text(_TOOL if kind == 'tool' else 'steps: {}\n', encoding='utf-8')
        roots = {ns: [str(base / root) for root in paths] for ns, paths in case.get('roots', {'global': ['']}).items()}
        for listing in (_LISTED, _listed_in_reverse):
            monkeypatch.setattr(glob, 'glob', listing)
            found = _found(kind, roots, base)
            assert found == case['found']
            if kind == 'tool':
                assert [list(stems) for stems in found.values()] == [list(stems) for stems in case['found'].values()]
            out, err = capsys.readouterr()
            assert out == ''
            assert _warnings(err, base, kind) == [[ns, stem, files] for ns, stems in case.get('warnings', {}).items()
                                                  for stem, files in stems.items()]
