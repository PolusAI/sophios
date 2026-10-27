# Coding standards

What do the checks require of a change to Sophios? Four tools run in CI over
`src/`, `examples/`, `tests/` and `docs/cookbook/`, configured in
`pyproject.toml`, and a change is expected to pass them before it is pushed.

## Types

Every function is annotated, and mypy runs with `disallow_untyped_defs`,
`disallow_incomplete_defs`, `check_untyped_defs`, `warn_return_any`,
`warn_redundant_casts` and `warn_unused_ignores`; the pydantic plugin is
loaded. Third-party packages without stubs are listed under
`[[tool.mypy.overrides]]` with `ignore_missing_imports`. The paths are
`files` in `[tool.mypy]`, so a bare `mypy` checks what CI checks:

```bash
mypy
```

Prefer explicit types at a public boundary. A `# type: ignore` needs a reason
the reader can check, and an unused one fails the build.

## Style

pylint runs with a threshold of 9.25 and a line length of 120; modules are
kept under 750 lines. `missing-class-docstring`, `missing-module-docstring`,
`invalid-name`, `fixme` and a few others are disabled repository-wide, so a
docstring is required on functions and not on every class. autopep8 checks
formatting at the same line length:

```bash
pylint src/ examples/**/*.py tests/
autopep8 --exit-code --recursive --diff --max-line-length 120 examples/ src/ tests/
```

Docstrings say what a function is responsible for, what its arguments mean
and what the caller gets back, in Google style; types are in the signature
and not repeated in the docstring. A comment explains a decision, not the
line below it.

## Import boundaries

`lint-imports` checks the contracts in `pyproject.toml`:

- the core never imports `sophios.contrib`; contrib may import the core;
- the compiler is layered: `sophios.api` over `sophios.compiler` over
  `sophios.ir` over `sophios.lang`, and a lower layer never imports a higher
  one;
- `sophios.api.python.workflow` and `sophios.api.python.tool_builder` reach
  the compiler, the registry, the runner and the pre-flight only through
  their runtime adapter, never directly.

A change that needs to cross one of these lines is a design change, not a
lint exception.

## Shell and spelling

`shellcheck -e SC1017` runs over the shell scripts, and codespell over the
tree, with `codespellwords.txt` as its allow list.

## Tests

A behaviour change comes with a test through a public entry point: the
command line, `Workflow`, `Step`, `CommandLineTool`, or `compile_source`. A
test that pins existing behaviour should fail under a plausible mutation of
it. Markers are listed in [Validation and testing](../validation.md); a test
that pulls images or runs a container is marked so that the quick lanes skip
it. Every new error code lands with its provocation in
`tests/core/provocations.py` and its explanation in `EXPLANATIONS`, or the
suite fails.

## Before pushing

```bash
pytest -m fast
mypy
pylint src/ examples/**/*.py tests/
lint-imports
sphinx-build -W --keep-going -b html docs docs/_build/html   # when docs/ changed
```

The pre-commit hooks installed by `pre-commit install` run the formatting
and type checks on each commit. The mypy hook runs the `mypy` of the active
environment over the `files` in `[tool.mypy]`, as CI does, so commit with the
development environment activated.
