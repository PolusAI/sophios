"""Behavioural coverage for `sophios.plugins` transforms that reach emitted CWL.

`sophios.plugins` is FORBIDDEN to the oracle (`test_hermeticity.py`),
because importing it reads the config and walks the adapter search paths. That
makes the oracle unable to check the plugin transforms directly, so the ones
whose output lands in an emitted document are covered here instead.

Discovery is covered here too: which files are plugins, and which file a stem resolves to.
"""
import copy
from collections.abc import Callable
import glob
from pathlib import Path
from typing import Any

import pytest

import sophios.plugins
from sophios.wic_types import StepId, Yaml


@pytest.mark.fast
def test_partial_failure_success_codes_are_canonically_ordered() -> None:
    """`successCodes` is emitted CWL, so no set's iteration order may reach it.

    Under `--partial_failure_enable`, `main` transforms the emitted artifact
    tree and writes the result to disk, which makes this list output rather
    than an internal collection. The codes are
    chosen so a set's iteration order is not accidentally the sorted one.
    """
    tool: Yaml = {'class': 'CommandLineTool', 'baseCommand': 'true', 'inputs': {},
                  'outputs': {'o': {'type': 'File', 'outputBinding': {'glob': 'x'}}}}
    updated = sophios.plugins.cwl_update_outputs_optional(tool, [3, 6], [9, 4, 1])
    assert updated['successCodes'] == [0, 1, 3, 4, 5, 9]


@pytest.mark.fast
def test_partial_failure_makes_outputs_optional() -> None:
    """The other half of the same transform, so the test above cannot pass alone
    against a function that stopped doing its actual job."""
    tool: Yaml = {'class': 'CommandLineTool', 'baseCommand': 'true', 'inputs': {},
                  'outputs': {'o': {'type': 'File', 'outputBinding': {'glob': 'x'}}}}
    updated = sophios.plugins.cwl_update_outputs_optional(tool, [0, 1], [])
    assert updated['outputs']['o']['type'] == 'File?'


def _docker(spelling: str, **fields: Any) -> Yaml | list[Yaml]:
    """A fresh `hints` or `requirements` holding one `DockerRequirement`: a mapping
    keyed by class (hand-written adapters) or a list of `{class: ...}` entries (cwl_utils)."""
    if spelling == 'map-form':
        return {'DockerRequirement': fields}
    return [{'class': 'DockerRequirement', **fields}]


@pytest.mark.fast
@pytest.mark.parametrize('spelling', ['map-form', 'list-form'])
def test_a_dockerfile_include_is_resolved_in_either_hints_spelling(spelling: str) -> None:
    """cwl_utils renders `hints` as a list; hand-written adapters use a mapping.
    The list crashed `Workflow.compile()` on every hinted tool_builder tool."""
    tool: Yaml = {'class': 'CommandLineTool', 'baseCommand': 'true', 'inputs': {}, 'outputs': {},
                  'hints': _docker(spelling, dockerFile={'$include': 'Dockerfile_x'}, dockerImageId='x')}
    before = copy.deepcopy(tool)
    updated = sophios.plugins.cwl_prepend_dockerFile_include_path(tool, '/adapters/t.cwl')
    assert updated['hints'] == _docker(spelling, dockerFile={'$include': '/adapters/Dockerfile_x'},
                                       dockerImageId='x')
    assert tool == before, 'the input document is not mutated'


@pytest.mark.fast
@pytest.mark.parametrize('spelling', ['map-form', 'list-form'])
def test_the_noentrypoint_tag_is_appended_in_either_requirements_spelling(spelling: str) -> None:
    """The same two spellings of `requirements`, under `--docker_remove_entrypoints`."""
    tool: Yaml = {'class': 'CommandLineTool', 'baseCommand': 'true', 'inputs': {}, 'outputs': {},
                  'requirements': _docker(spelling, dockerPull='docker.io/bash:4.4')}
    before = copy.deepcopy(tool)
    updated = sophios.plugins.dockerPull_append_noentrypoint(tool)
    assert updated['requirements'] == _docker(spelling, dockerPull='docker.io/bash:4.4-noentrypoint')
    assert tool == before, 'the input document is not mutated'


def _tool(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('cwlVersion: v1.2\nclass: CommandLineTool\nbaseCommand: true\ninputs: {}\noutputs: {}\n',
                    encoding='utf-8')


def _workflow(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('steps: {}\n', encoding='utf-8')


#: Search roots by namespace, each root listing its files without an extension. `twin` is defined more than
#: once, and another search path is read after its last definition is found.
_REPEAT_THEN_MORE_TO_READ = [
    pytest.param({'global': [['a/twin', 'b/twin'], ['other']]}, id='twice-in-one-root-then-another-root'),
    pytest.param({'global': [['twin'], ['twin'], ['other']]}, id='across-two-roots-then-a-third'),
    pytest.param({'global': [['a/twin', 'b/twin']], 'gpu': [['other']]}, id='twice-in-one-root-then-another-namespace'),
]


def _lay_out(base: Path, layout: dict[str, list[list[str]]], extension: str,
             write: Callable[[Path], None]) -> tuple[dict[str, list[str]], list[Path]]:
    """Write `layout` under `base`. Returns the search paths by namespace and the files that define `twin`."""
    search_paths: dict[str, list[str]] = {}
    twins: list[Path] = []
    for namespace, roots in layout.items():
        search_paths[namespace] = []
        for index, names in enumerate(roots):
            root = base / namespace / f'root{index}'
            search_paths[namespace].append(str(root))
            for name in names:
                path = root / f'{name}.{extension}'
                write(path)
                if path.stem == 'twin':
                    twins.append(path)
    return search_paths, twins


@pytest.fixture
def reversed_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The filesystem lists a directory's entries in reverse, as some do; discovery must not depend on it."""
    real_glob = glob.glob

    def listed_in_reverse(pattern: str, *, recursive: bool = False) -> list[str]:
        return sorted(real_glob(pattern, recursive=recursive), reverse=True)

    monkeypatch.setattr(glob, 'glob', listed_in_reverse)


@pytest.mark.fast
@pytest.mark.usefixtures('reversed_listing')
def test_tools_are_discovered_in_sorted_path_order(tmp_path: Path) -> None:
    """Tools come back ordered by path, not by stem and not by the order the filesystem lists them."""
    for name in ('b/zeta.cwl', 'c/mid.cwl', 'a/alpha.cwl'):
        _tool(tmp_path / name)
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(tmp_path)]}})
    assert [key.stem for key in tools] == ['alpha', 'zeta', 'mid']


@pytest.mark.fast
def test_nothing_under_an_autogenerated_directory_is_a_tool(tmp_path: Path) -> None:
    """`autogenerated/` is the compiler's own output, at the top of a search root or nested below it."""
    for name in ('a/alpha.cwl', 'autogenerated/ghost.cwl', 'x/autogenerated/y/ghost2.cwl'):
        _tool(tmp_path / name)
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(tmp_path)]}})
    assert [key.stem for key in tools] == ['alpha']


@pytest.mark.fast
def test_a_search_root_under_an_autogenerated_directory_still_finds_its_files(tmp_path: Path) -> None:
    """Only the part of a path below its search root is checked for `autogenerated/`."""
    root = tmp_path / 'autogenerated' / 'adapters'
    for name in ('a/alpha.cwl', 'autogenerated/ghost.cwl'):
        _tool(root / name)
    _workflow(root / 'w.wic')
    _workflow(root / 'autogenerated' / 'g.wic')
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(root)]}})
    assert [key.stem for key in tools] == ['alpha']
    found = sophios.plugins.get_yml_paths({'search_paths_wic': {'global': [str(root)]}})
    assert sorted(found['global']) == ['w']


@pytest.mark.fast
@pytest.mark.usefixtures('reversed_listing')
def test_a_duplicate_tool_stem_warns_and_the_last_file_found_is_used(tmp_path: Path,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """Every file that defines the stem is named on stderr, with the one used; stdout stays clean."""
    twins = [tmp_path / name / 'twin.cwl' for name in ('a', 'b', 'c')]
    for path in [*twins, tmp_path / 'a' / 'single.cwl']:
        _tool(path)
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(tmp_path)]}})
    assert tools[StepId('twin', 'global')].run_path == str(twins[-1])
    captured = capsys.readouterr()
    assert captured.out == ''
    assert all(str(path) in captured.err for path in twins)
    assert f'Using {twins[-1]}' in captured.err
    assert 'single' not in captured.err


@pytest.mark.fast
def test_every_duplicate_tool_stem_gets_its_own_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Discovery reads everything, then says so once per repeated stem."""
    for stem in ('pair', 'twin'):
        _tool(tmp_path / 'a' / f'{stem}.cwl')
        _tool(tmp_path / 'b' / f'{stem}.cwl')
    sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(tmp_path)]}})
    warnings = capsys.readouterr().err
    assert warnings.count('Warning! tool') == 2
    assert warnings.index("'pair'") < warnings.index("'twin'")


@pytest.mark.fast
@pytest.mark.parametrize('layout', _REPEAT_THEN_MORE_TO_READ)
def test_a_repeated_tool_stem_is_reported_once_after_every_search_path_is_read(
        tmp_path: Path, capsys: pytest.CaptureFixture[str], layout: dict[str, list[list[str]]]) -> None:
    """The warning waits for the last search path: it is not repeated, and it names every file."""
    search_paths, twins = _lay_out(tmp_path, layout, 'cwl', _tool)
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': search_paths})
    assert tools[StepId('twin', 'global')].run_path == str(twins[-1])
    warning = capsys.readouterr().err
    assert warning.count("Warning! tool 'twin'") == 1
    assert f'is defined by {len(twins)} files' in warning
    assert all(f'  {path}\n' in warning for path in twins)
    assert f'Using {twins[-1]}' in warning


@pytest.mark.fast
def test_a_later_search_path_overrides_an_earlier_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Search paths are taken in the order the config lists them, whatever their names sort to."""
    _tool(tmp_path / 'z_stock' / 'twin.cwl')
    _tool(tmp_path / 'a_mine' / 'twin.cwl')
    roots = [str(tmp_path / 'z_stock'), str(tmp_path / 'a_mine')]
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': roots}})
    assert tools[StepId('twin', 'global')].run_path == str(tmp_path / 'a_mine' / 'twin.cwl')
    assert f"Using {tmp_path / 'a_mine' / 'twin.cwl'}" in capsys.readouterr().err


@pytest.mark.fast
def test_the_same_stem_in_two_namespaces_is_not_a_duplicate(tmp_path: Path,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    """Namespaces keep their own stems, so the same stem in two of them is two tools and no warning."""
    _tool(tmp_path / 'a' / 'twin.cwl')
    _tool(tmp_path / 'b' / 'twin.cwl')
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(tmp_path / 'a')],
                                                                'gpu': [str(tmp_path / 'b')]}})
    assert {key.plugin_ns for key in tools} == {'global', 'gpu'}
    assert capsys.readouterr().err == ''


@pytest.mark.fast
def test_the_deprecated_md_adapters_are_neither_discovered_nor_duplicates(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`biobb_md` is deprecated in favor of `biobb_gromacs`: its tools are skipped, so a shared stem is no duplicate.

    The skip matches the substring on the whole path, so this test's own name must not contain it.
    """
    gromacs = tmp_path / 'biobb_gromacs' / 'grompp.cwl'
    _tool(gromacs)
    _tool(tmp_path / 'biobb_md' / 'grompp.cwl')
    _tool(tmp_path / 'biobb_md' / 'only_here.cwl')
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(tmp_path)]}})
    assert {key.stem: tool.run_path for key, tool in tools.items()} == {'grompp': str(gromacs)}
    assert capsys.readouterr().err == ''


@pytest.mark.fast
def test_a_file_reached_through_overlapping_search_paths_is_one_file(tmp_path: Path,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """A root and its own subdirectory both find `a/one.cwl`; that is one tool and no duplicate."""
    _tool(tmp_path / 'a' / 'one.cwl')
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(tmp_path), str(tmp_path / 'a')]}})
    assert [key.stem for key in tools] == ['one']
    assert capsys.readouterr().err == ''


@pytest.mark.fast
def test_a_file_reached_through_a_symlinked_search_path_is_one_file(tmp_path: Path,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    """The same directory under two names is one set of files."""
    _tool(tmp_path / 'real' / 'one.cwl')
    link = tmp_path / 'link'
    try:
        link.symlink_to(tmp_path / 'real', target_is_directory=True)
    except OSError:
        pytest.skip('this platform does not let the test create a symlink')
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': [str(tmp_path / 'real'), str(link)]}})
    assert [key.stem for key in tools] == ['one']
    assert capsys.readouterr().err == ''


@pytest.mark.fast
def test_overlapping_search_paths_do_not_misname_the_file_used(tmp_path: Path,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    """`sub/twin.cwl` is found last, by the root after the subdirectory; the warning lists it once and names it."""
    _tool(tmp_path / 'a' / 'twin.cwl')
    _tool(tmp_path / 'sub' / 'twin.cwl')
    roots = [str(tmp_path / 'sub'), str(tmp_path)]
    tools = sophios.plugins.get_tools_cwl({'search_paths_cwl': {'global': roots}})
    used = tmp_path / 'sub' / 'twin.cwl'
    assert tools[StepId('twin', 'global')].run_path == str(used)
    warning = capsys.readouterr().err
    assert warning.count(str(used)) == 2   # once in the list of files, once as the file used
    assert f'Using {used}' in warning


@pytest.mark.fast
def test_workflow_discovery_skips_inputs_files_and_the_compilers_output(tmp_path: Path) -> None:
    """`_inputs` files and anything under `autogenerated/` are not workflows."""
    for name in ('b/z.wic', 'a/a.wic', 'autogenerated/g.wic', 'a/a_inputs.wic'):
        _workflow(tmp_path / name)
    found = sophios.plugins.get_yml_paths({'search_paths_wic': {'global': [str(tmp_path)]}})
    assert sorted(found['global']) == ['a', 'z']


@pytest.mark.fast
@pytest.mark.usefixtures('reversed_listing')
def test_the_last_workflow_in_sorted_path_order_wins_a_shared_stem(tmp_path: Path) -> None:
    """Of two workflows with one stem in one search root, the last path in sorted order wins, not the shortest."""
    for name in ('a/dup.wic', 'zz/sub/dup.wic'):
        _workflow(tmp_path / name)
    found = sophios.plugins.get_yml_paths({'search_paths_wic': {'global': [str(tmp_path)]}})
    assert found['global'] == {'dup': tmp_path / 'zz' / 'sub' / 'dup.wic'}


@pytest.mark.fast
@pytest.mark.usefixtures('reversed_listing')
def test_a_duplicate_workflow_stem_warns_and_names_the_file_used(tmp_path: Path,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    """Workflows follow the tools' rule and say the same thing on stderr."""
    for name in ('a/dup.wic', 'zz/sub/dup.wic'):
        _workflow(tmp_path / name)
    sophios.plugins.get_yml_paths({'search_paths_wic': {'global': [str(tmp_path)]}})
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'Warning! workflow' in captured.err
    assert str(tmp_path / 'a' / 'dup.wic') in captured.err
    assert f"Using {tmp_path / 'zz' / 'sub' / 'dup.wic'}" in captured.err


@pytest.mark.fast
@pytest.mark.parametrize('layout', _REPEAT_THEN_MORE_TO_READ)
def test_a_repeated_workflow_stem_is_reported_once_after_every_search_path_is_read(
        tmp_path: Path, capsys: pytest.CaptureFixture[str], layout: dict[str, list[list[str]]]) -> None:
    """The warning waits for the last search path: it is not repeated, and it names every file."""
    search_paths, twins = _lay_out(tmp_path, layout, 'wic', _workflow)
    found = sophios.plugins.get_yml_paths({'search_paths_wic': search_paths})
    assert found['global']['twin'] == twins[-1]
    warning = capsys.readouterr().err
    assert warning.count("Warning! workflow 'twin'") == 1
    assert f'is defined by {len(twins)} files' in warning
    assert all(f'  {path}\n' in warning for path in twins)
    assert f'Using {twins[-1]}' in warning


@pytest.mark.fast
def test_a_later_workflow_search_path_overrides_an_earlier_one(tmp_path: Path,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    """Search paths are taken in the order the config lists them, whatever their names sort to."""
    _workflow(tmp_path / 'z_stock' / 'dup.wic')
    _workflow(tmp_path / 'a_mine' / 'dup.wic')
    roots = [str(tmp_path / 'z_stock'), str(tmp_path / 'a_mine')]
    found = sophios.plugins.get_yml_paths({'search_paths_wic': {'global': roots}})
    assert found['global'] == {'dup': tmp_path / 'a_mine' / 'dup.wic'}
    assert f"Using {tmp_path / 'a_mine' / 'dup.wic'}" in capsys.readouterr().err


@pytest.mark.fast
def test_the_same_workflow_stem_in_two_namespaces_is_not_a_duplicate(tmp_path: Path,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """Namespaces keep their own stems."""
    _workflow(tmp_path / 'a' / 'dup.wic')
    _workflow(tmp_path / 'b' / 'dup.wic')
    found = sophios.plugins.get_yml_paths({'search_paths_wic': {'global': [str(tmp_path / 'a')],
                                                                'other': [str(tmp_path / 'b')]}})
    assert sorted(found) == ['global', 'other']
    assert capsys.readouterr().err == ''


@pytest.mark.fast
def test_the_authored_names_filter_rewrites_emitted_ids_longest_first() -> None:
    """A nested id is rewritten as itself, never as the shorter step id it starts with."""
    import logging  # pylint: disable=import-outside-toplevel
    names = {'steps': {'w__step__1__child.wic': {'id': 'w__step__1__child.wic', 'index': 1, 'name': 'child.wic'},
                       'w__step__1__child.wic___child__step__1__mk_file': {'id': 'child__step__1__mk_file',
                                                                           'index': 1, 'name': 'mk_file'}},
             'ports': {'w__step__1__child.wic___child__step__1__mk_file___name': {
                 'steps': ['child.wic', 'mk_file'], 'port': 'name'},
                 'name': {'steps': [], 'port': 'name'}}}
    names_filter = sophios.plugins.AuthoredNamesFilter(names)

    def rewritten(message: str, *args: object) -> str:
        record = logging.LogRecord('cwltool', logging.ERROR, __file__, 1, message, args, None)
        assert names_filter.filter(record)
        return record.getMessage()

    assert rewritten('missing %s', 'w__step__1__child.wic___child__step__1__mk_file___name') == (
        'missing child.wic/mk_file/name (w__step__1__child.wic___child__step__1__mk_file___name)')
    assert rewritten('[step child__step__1__mk_file] failed; filename unset') == (
        "[step step 1 'mk_file' (child__step__1__mk_file)] failed; filename unset")
    assert rewritten('w__step__1__child.wic.') == "step 1 'child.wic' (w__step__1__child.wic)."
