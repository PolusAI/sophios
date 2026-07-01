# Coding Standards

This page describes the development checks used in the Sophios repository. For
environment setup, start with the [Developer Install Guide](installguide.md).

## Type Checking

Sophios uses mypy type annotations throughout the source tree. The active mypy
configuration lives in `pyproject.toml`.

Run the same broad paths used by CI:

```bash
mypy src/ examples/ tests/
```

When adding or changing public APIs, prefer explicit types at the API boundary.
Internal helper code can still be compact, but typed inputs and return values
make the workflow compiler and Python API much easier to maintain.

## Docstrings

Docstrings should explain what a function or class is responsible for, what its
important arguments mean, and what callers can expect back.

Use docstrings to clarify behavior, not to repeat obvious type information. For
example, this is useful because it explains the naming convention:

```python
def step_name_str(yaml_stem: str, i: int, step_key: str) -> str:
    """Return the stable internal name for one workflow step."""
    ...
```

## Tests

Tests live under `tests/` and use pytest markers declared in `pyproject.toml`:

- `fast`: quick API, schema, and unit-style checks,
- `serial`: tests that should not run in parallel,
- `slow`: workflow or integration checks that can take longer.

Useful local commands:

```bash
pytest -m fast
pytest tests/test_python_api.py tests/test_tool_builder.py -q
pytest -m serial
pytest -m "not serial" --workers 8
```

Runtime workflow tests may need Docker or Podman and can pull container images.
Prefer the focused API tests while iterating on Python API or documentation
changes.

## Coverage

The repository includes `pytest-cov`. To generate a coverage report, add
`--cov` and use the checked-in `.coveragerc`:

```bash
pytest -m fast --cov --cov-config=.coveragerc
```

## Linting

Sophios uses pylint for style, formatting, and common mistake detection. The
configuration lives in `pyproject.toml`.

Run:

```bash
pylint src/ examples/**/*.py tests/
```

The current configured maximum line length is 120 characters.

## CI Checks

GitHub Actions workflows live under `.github/workflows/`. The main Linux CI
workflow installs `.[all_except_runner_src]` and runs:

- `mypy src/ examples/ tests/`,
- `pylint src/ examples/**/*.py tests/`,
- focused Python API and workflow tests,
- selected runtime workflow tests.

Before pushing a broad change, run the focused checks that match the area you
touched. For Python API or docs changes, the most useful baseline is:

```bash
sphinx-build -b html docs docs/_build/html
pytest tests/test_python_api.py tests/test_tool_builder.py -q
```
