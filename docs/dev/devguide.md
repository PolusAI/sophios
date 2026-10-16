# Developer guide

How do I work on Sophios itself: set up a checkout, run the tests and checks,
build these docs, and find my way around the source? This page is the
entry point for the Developers section. [Architecture](architecture.md)
describes the compiler, the [language specification](language_spec.md) the
contract between its two surfaces, and [Validation and testing](../validation.md)
what the suite promises.

## A source checkout

```bash
git clone https://github.com/PolusAI/sophios.git
cd sophios
conda env create -n sophios_dev -f install/system_deps.yml
conda activate sophios_dev
python -m pip install -e ".[all_except_runner_src]"
pre-commit install
```

`install/system_deps.yml` (`install/system_deps_windows.yml` on Windows)
installs the non-Python programs the tests and local runs need, Node.js and
Graphviz among them, and the binary Python packages that are easier to get
from conda than from pip. It does not install Sophios. The editable install
puts the checkout's `src/` on the path and adds the test, documentation,
plotting and type-stub extras, as the CI installs them;
`".[test,doc,mypy-types]"` is enough for API tests and docs work. To update
an existing environment, `conda env update -n sophios_dev -f install/system_deps.yml --prune`.

Check that Python imports the checkout:

```bash
python -c "import sophios; print(sophios.__version__, sophios.__file__)"
sophios --help
```

The version is written by setuptools-scm when the package is built, so an
import of a checkout that was never installed says `unknown` and asks for
`pip install -e .`.

## Configuration for development

`sophios --generate_config` from a checkout writes `~/wic/global_config.json`
and copies the checkout's `cwl_adapters/` and `docs/tutorials/` to
`~/wic/cwl_adapters` and `~/wic/examples`. To compile against the checkout
itself rather than a copy, point the search paths at it:

```json
{
    "search_paths_cwl": {"global": ["/path/to/sophios/cwl_adapters"], "gpu": []},
    "search_paths_wic": {"global": ["/path/to/sophios/docs/tutorials", "/path/to/sophios/docs/cookbook"]}
}
```

The CI writes its configuration with `.github/update_sophios_config.py`,
which lists the tool and workflow repositories each lane checks out. A test
run reads the configuration the process finds; when there is none, the suite
falls back to the in-repository directories without writing one.

## Tests

```bash
pytest -m fast                      # the quick check: unit and API tests
pytest tests/core/test_cookbook.py  # the cookbook pairs
pytest -m "not serial" --workers 8  # the parallel part of the suite
pytest -m serial                    # the rest
```

[Validation and testing](../validation.md) lists the markers. Tests that run
workflows need a container engine and pull images; compiling and the API
tests do not. `tests/core/test_examples.py` compiles every workflow the
configuration reaches and runs those not blacklisted, so a broad
configuration makes it long.

## Checks

The CI runs, over `src/`, `examples/`, `tests/` and `docs/cookbook/`:

```bash
mypy src/ examples/ tests/ docs/cookbook/
pylint src/ examples/**/*.py tests/
autopep8 --exit-code --recursive --diff --max-line-length 120 examples/ src/ tests/
lint-imports
shellcheck -e SC1017 install/*.sh
```

`lint-imports` checks the three import contracts in `pyproject.toml`: the
core never imports `sophios.contrib`; `sophios.api` sits above
`sophios.compiler`, above `sophios.ir`, above `sophios.lang`; and the two
public API modules reach the compiler only through their runtime adapter.
[Coding standards](codingstandards.md) has the settings.

## Documentation

```bash
sphinx-build -W --keep-going -b html docs docs/_build/html
cd docs && make html
cd docs && make pdf
```

The site is built with warnings as errors, here and on Read the Docs. `make
pdf` builds one single-page HTML document from `docs/pdf_index.rst` and
prints it to `docs/_build/pdf/sophios-docs.pdf` with a local Chrome or
Chromium (`CHROME_BIN` names one). Pages are Markdown (MyST); the API pages
use autodoc, so the build imports `sophios` from `src/`. Every workflow a page
shows in both forms is a cookbook pair, which
`tests/core/test_cookbook.py` keeps honest; `docs/error_codes.md` is
generated from `EXPLANATIONS` in `src/sophios/lang/error_codes.py`, and a
test keeps the two equal.

## The layout of `src/sophios`

| Where | What |
|---|---|
| `lang/` | the language: parser, AST, renderer, JSON Schema, diagnostics, error codes, versions, the CWL support matrix |
| `ir/` | the compiler's phases: front door, resolve, lower, link, infer, namespaces, emit, and the graph types |
| `compiler.py` | `compile_source`, the one entry point |
| `api/python/` | the public Python API: `workflow.py`, `tool_builder.py`, and the private modules behind them |
| `main.py`, `cli.py` | the command line: argument parsing and the adapter from arguments to settings |
| `plugins.py`, `input_output.py` | tool and workflow discovery, the configuration, writing artifacts |
| `preflight.py` | what a local run needs from this machine |
| `run_local.py`, `realtime.py` | running the compiled CWL, and the real-time analyses beside it |
| `post_compile.py` | the rewrites of a compiled tree: flattening, embedding, partial failures |
| `drawing.py` | `--graphviz` |
| `contrib/` | see [Contrib](../contrib.md) |

## Runtime notes

**Glob order.** A CWL runner returns globbed files in filename order, not in
the order the inputs that produced them were given. A tool whose output
order matters writes a manifest and reads it downstream.

**Partial failures and expressions.** Under `--partial_failure_enable` a
command may be forgiven while an `outputEval` that assumes `self[0]` exists
still fails. Guard such expressions:

```javascript
${
  if (!self[0]) {
    return null;
  }
  const lines = self[0].contents.split("\n");
  return lines[0].trim() === "True";
}
```

**Singularity.** When cwltool or `cwl-docker-extract` reports a stale or
cached image, `singularity cache clean`.

**Toil.** A stale job store keeps older state; when changing inputs or flags
behaves strangely, `toil clean` the job store and remove `~/.toil`.

**Docker Desktop.** Many `com.docker` processes left behind can make a run
hang; the pre-flight counts them and says so. Quitting and restarting Docker
Desktop clears it.
