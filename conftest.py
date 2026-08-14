# See https://docs.pytest.org/en/7.1.x/example/simple.html
from pathlib import Path
from typing import Any

import pytest

# Zone membership is defined by directory: anything under tests/contrib/ is
# contrib. The marker is derived from that rather than applied by hand, so the
# directory stays the single source of truth and the two cannot drift.
# See design_docs/core-refactor-design.md, Spec 0.
CONTRIB_DIR = Path(__file__).resolve().parent / 'tests' / 'contrib'


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption('--cwl_runner', type=str, required=False, default='cwltool', choices=['cwltool', 'toil-cwl-runner'],
                     help='The CWL runner to use for running workflows locally.')


@pytest.fixture
def cwl_runner(request: pytest.FixtureRequest) -> Any:
    return request.config.getoption("--cwl_runner")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test under tests/contrib/ with the `contrib` marker.

    This hook is repo-wide by nature: pytest passes it every collected item
    regardless of where the conftest lives, which is why the zone is selected
    by path here rather than by placing the hook in a subdirectory.

    Both selection styles therefore agree:

        pytest tests/contrib          # by location
        pytest -m contrib             # by marker
        pytest tests/ -m "not contrib"
    """
    for item in items:
        if item.path.resolve().is_relative_to(CONTRIB_DIR):
            item.add_marker(pytest.mark.contrib)
