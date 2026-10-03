"""The version Sophios reports is the version of the code that is running."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import sophios
import sophios.cli


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
