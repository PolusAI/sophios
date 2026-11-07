"""The package reports the version it was built as."""
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
@pytest.mark.parametrize(('built_as', 'version'), [('1.2.3', '1.2.3'), (None, 'unknown')])
def test_the_version_is_the_one_built_beside_the_code(tmp_path: Path, built_as: str | None, version: str) -> None:
    """__version__ is what the _version.py next to the package says; without one the version is unknown, and says
    why."""
    run = _import_copy(tmp_path, built_as)
    assert run.stdout.splitlines() == [str(tmp_path / 'sophios' / '__init__.py'), version]
    assert ('never built' in run.stderr) is (built_as is None)
    assert built_as or "RuntimeWarning: this copy of sophios was never built, so __version__ is 'unknown'" in run.stderr


@pytest.mark.fast
def test_the_compiled_header_and_the_cli_report_the_running_version(capsys: pytest.CaptureFixture[str]) -> None:
    """The header of compiled CWL and --version print the same version as __version__."""
    assert f'Sophios, version {sophios.__version__}\n' in sophios.auto_gen_header

    with pytest.raises(SystemExit):
        sophios.cli.parser.parse_args(['--version'])
    assert capsys.readouterr().out.strip() == sophios.__version__
