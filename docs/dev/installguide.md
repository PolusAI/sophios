# Developer Install Guide

This page is for working from a Sophios source checkout. Use it when you want
to edit Sophios, run tests, build the docs, or work with repository examples.

If you only want to install Sophios as a package, use the
[user install guide](../installguide.md).

## What the Developer Install Provides

A developer install has three layers:

- a source checkout managed with Git,
- a Python environment with the system tools used by tests and local workflow
  execution,
- an editable `pip` install so imports come from the checkout.

The Python requirement is declared in `pyproject.toml`. For this checkout, use
Python 3.11 or newer.

## Step 1: Clone the Repository

If you have a fork, clone your fork:

```bash
git clone git@github.com:<your-github-user>/sophios.git
cd sophios
git remote add upstream https://github.com/PolusAI/sophios.git
```

If you only need a read-only checkout of upstream:

```bash
git clone https://github.com/PolusAI/sophios.git
cd sophios
```

Check the remotes:

```bash
git remote -v
```

## Step 2: Create the Development Environment

Conda or mamba is the recommended source-development path because the test and
documentation workflows need a few non-Python executables such as Node.js and
Graphviz.

On macOS and Linux:

```bash
conda env create -n sophios_dev -f install/system_deps.yml
conda activate sophios_dev
python --version
```

On Windows:

```bash
conda env create -n sophios_dev -f install/system_deps_windows.yml
conda activate sophios_dev
python --version
```

If the environment already exists, update it:

```bash
conda activate sophios_dev
conda env update -n sophios_dev -f install/system_deps.yml --prune
```

The environment files install binary dependencies used by development and
testing. They do not install Sophios itself.

## Step 3: Install Sophios in Editable Mode

From the repository root:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[all_except_runner_src]"
```

This mirrors the main CI install: it includes test, documentation, plotting,
cytoscape, and mypy-type extras while keeping released runner packages.

For a lighter install that is enough for most API tests and docs work:

```bash
python -m pip install -e ".[test,doc,mypy-types]"
```

Use `.[all]` only when you intentionally want the source-runner extra declared
in `pyproject.toml`. That extra installs `cwl-utils` from a Git source instead
of using only released runner packages.

Install the pre-commit hooks after the editable install:

```bash
pre-commit install
```

## Step 4: Verify the Checkout

Confirm that Python imports Sophios from this checkout:

```bash
python - <<'PY'
import sophios
from sophios.api.python.workflow import Step, Workflow
from sophios.api.python.tool_builder import CommandLineTool, Input, Output, cwl

print(f"Sophios version: {sophios.__version__}")
print(f"Sophios module:  {sophios.__file__}")
print("Workflow API and tool builder API are available")
PY
```

The printed module path should point inside the checkout.

Confirm the CLI:

```bash
sophios --help
```

## Step 5: Build the Documentation

Build the RTD site locally with:

```bash
sphinx-build -b html docs docs/_build/html
```

Open `docs/_build/html/index.html` to inspect the generated site.

To build a unified PDF of the user docs followed by the developer docs, run:

```bash
cd docs
make pdf
```

The PDF builder reuses the Sphinx documentation source, builds a single HTML
document with a PDF-specific table of contents, and prints it with a local
Chrome or Chromium executable. The generated file is written to
`docs/_build/pdf/sophios-docs.pdf`.

## Step 6: Run Tests

For a fast local confidence check:

```bash
pytest -m fast
```

For the focused Python API and tool-builder tests:

```bash
pytest tests/test_python_api.py tests/test_tool_builder.py -q
```

For the serial tests:

```bash
pytest -m serial
```

For the non-serial tests with pytest-parallel:

```bash
pytest -m "not serial" --workers 8
```

Some workflow runtime tests require Docker or Podman and may pull container
images. Slow workflow tests can take substantially longer than API-only tests.

## Step 7: Run Static Checks

The main CI workflows run mypy and pylint over source, examples, and tests.
Before pushing substantial changes, run:

```bash
mypy src/ examples/ tests/
pylint src/ examples/**/*.py tests/
```

The exact CI jobs are defined in `.github/workflows/`.

## Optional: Configure `.wic` Discovery

Sophios uses `~/wic/global_config.json` as the default discovery config for CWL
tools and `.wic` workflows. Generate a starter config with:

```bash
sophios --generate_config
```

Generate schemas for editor validation with:

```bash
sophios --generate_schemas
```

If discovery looks stale after adding, removing, or renaming tools, inspect
`~/wic/global_config.json` and regenerate schemas. Do not delete `~/wic`
blindly if it contains local configuration you want to keep.

## Optional: External Workflow Repositories

Some CI jobs and workflow-regression paths use external workflow repositories
such as `mm-workflows`. They are not required for ordinary Sophios development,
API tests, documentation builds, or package work.

The scripts in `install/install_*.sh` are legacy helpers for specialized
external workflow setups. The primary Sophios developer setup is the conda
environment file plus editable `pip` install described above.

## Container Runtime Notes

Docker or Podman is required for local execution of containerized CWL tools.
Compilation and most Python API tests do not require a running container engine.

On macOS, Docker Desktop can occasionally leave many background processes and
cause local workflow execution to hang. Restarting Docker Desktop is usually the
first fix. On Linux, Podman can be used by passing `container_engine: "podman"`
to Python `Workflow.run(...)` or `--container_engine podman` to the CLI.
