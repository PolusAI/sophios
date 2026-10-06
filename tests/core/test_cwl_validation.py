"""The validation helper must stay importable where cwltool.main is not (Windows has no `pwd`)."""
import subprocess
import sys

import pytest


@pytest.mark.fast
def test_import_does_not_load_cwltool_main() -> None:
    """Importing the helper leaves cwltool.main (and spython, hence `pwd`) to the first validation."""
    code = ('import sys; sys.path.insert(0, "tests/core"); import cwl_validation; '
            'sys.exit(int("cwltool.main" in sys.modules))')
    assert subprocess.run([sys.executable, '-c', code], check=False).returncode == 0
