"""Every file that pins the Nextflow Sophios is tested with pins the one its grammar is taken from."""
import re
from pathlib import Path

import pytest

from sophios.nf_symbols import NEXTFLOW_GRAMMAR_VERSION

ROOT = Path(__file__).resolve().parents[3]

#: Each file that pins Nextflow, and how it spells the version.
PINS = {
    '.github/workflows/nextflow.yml': r'''(?:NXF_VER: "|grep 'version )([0-9.]+)''',
    'install/system_deps_ci.yml': r'- nextflow=([0-9.]+)',
    'docs/install.md': r'nextflow=([0-9.]+)',
}


@pytest.mark.fast
@pytest.mark.parametrize('path', sorted(PINS))
def test_every_nextflow_pin_is_the_grammar_version(path: str) -> None:
    """The CI lanes and the install guide name the Nextflow whose grammar the backend is written against."""
    assert set(re.findall(PINS[path], (ROOT / path).read_text(encoding='utf-8'))) == {NEXTFLOW_GRAMMAR_VERSION}
