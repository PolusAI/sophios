"""Every place that names a release names the same one.

release-please tags a release on master, setuptools-scm reads the version from
that tag, and the package reports what it was built as. None of this needs a
tag to check: the rules are in the files that drive each step.
"""
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest

import sophios
import sophios.cli

REPO_ROOT: Final = Path(__file__).resolve().parents[2]


def _json(name: str) -> Any:
    return json.loads((REPO_ROOT / name).read_text(encoding='utf-8'))


def _scm() -> dict[str, str]:
    pyproject = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    scm: dict[str, str] = pyproject['tool']['setuptools_scm']
    return scm


@pytest.mark.fast
def test_release_please_tags_a_release_as_setuptools_scm_reads_it() -> None:
    """The next tag is vX.Y.Z after the manifest's release, which tag_regex reads."""
    config, manifest = _json('release-please-config.json'), _json('.release-please-manifest.json')
    assert config['packages'].keys() == manifest.keys() == {'.'}

    package = config['packages']['.']
    # A component would prefix the tag (sophios-vX.Y.Z, which tag_regex does not
    # read) and name the release branch after the package.
    assert 'package-name' not in package and 'component' not in package
    assert package['include-component-in-tag'] is False
    assert package['include-v-in-tag'] is True

    assert re.fullmatch(_scm()['tag_regex'], f"v{manifest['.']}")


@pytest.mark.fast
def test_a_build_with_no_tag_in_reach_follows_the_last_release() -> None:
    """A tagless build counts from the release the manifest names, and moves with it."""
    config, manifest = _json('release-please-config.json'), _json('.release-please-manifest.json')
    assert _scm()['fallback_version'] == f"{manifest['.']}.post0.dev0"

    # release-please moves the base with each release through this marker.
    lines = (REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8').splitlines()
    assert [line for line in lines if 'x-release-please-version' in line] == [
        f'fallback_version = "{manifest["."]}.post0.dev0"  # x-release-please-version'
    ]
    assert {'type': 'generic', 'path': 'pyproject.toml'} in config['packages']['.']['extra-files']


def _import_copy(root: Path, built_as: str | None) -> subprocess.CompletedProcess[str]:
    """Import a copy of the package from `root`, built as `built_as` if given.

    The environment has its own `sophios` installed, with its own metadata, so the
    copy reports a version of its own only if it reads it from beside its code.
    """
    package = root / 'sophios'
    package.mkdir()
    shutil.copy(Path(sophios.__file__), package / '__init__.py')
    if built_as is not None:
        (package / '_version.py').write_text(f'__version__ = {built_as!r}\n', encoding='utf-8')
    script = ('import sys; sys.path.insert(0, sys.argv[1]); import sophios; '
              'print(sophios.__file__); print(sophios.__version__)')
    return subprocess.run([sys.executable, '-c', script, str(root)], cwd=root,
                          capture_output=True, text=True, check=True)


@pytest.mark.fast
def test_the_version_is_the_one_built_beside_the_code(tmp_path: Path) -> None:
    """__version__ is what the _version.py next to the package says."""
    run = _import_copy(tmp_path, built_as='1.2.3')
    assert run.stdout.splitlines() == [str(tmp_path / 'sophios' / '__init__.py'), '1.2.3']
    assert 'never built' not in run.stderr


@pytest.mark.fast
def test_a_copy_that_was_never_built_says_so(tmp_path: Path) -> None:
    """Without a _version.py the version is unknown, and says why."""
    run = _import_copy(tmp_path, built_as=None)
    assert run.stdout.splitlines() == [str(tmp_path / 'sophios' / '__init__.py'), 'unknown']
    assert "RuntimeWarning: this copy of sophios was never built, so __version__ is 'unknown'" in run.stderr


@pytest.mark.fast
def test_the_compiled_header_and_the_cli_report_the_running_version(capsys: pytest.CaptureFixture[str]) -> None:
    """The header of compiled CWL and --version print the same version as __version__."""
    assert f'Sophios, version {sophios.__version__}\n' in sophios.auto_gen_header

    with pytest.raises(SystemExit):
        sophios.cli.parser.parse_args(['--version'])
    assert capsys.readouterr().out.strip() == sophios.__version__
