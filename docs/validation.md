# Validation and testing

What does Sophios validate, at which layer, and how is the compiler itself
tested? This page is for people who change Sophios. Each layer answers one
question about one artifact, and the test suite holds the compiler to the
promises the user pages make.

## The layers

| Layer | Artifact | Question | Where |
|---|---|---|---|
| Parser | a `.wic` file | is it a well-formed document? | `sophios.lang.parse`; codes `wic001`–`wic010` and the other `PARSE` codes of the [language specification](dev/language_spec.md) |
| Compiler | the document and the tools it names | does every step, port, reference and literal resolve and type-check? | `sophios.compiler.compile_source`, through Resolve, Lower, Link and Infer |
| Tool builder | a `CommandLineTool` document | is it a valid CWL v1.2 tool? | `CommandLineTool.validate()` and `write_cwl(validate=True)`, which load it with cwltool; `--validate_plugins` does the same for every tool on the search paths |
| Editor schema | a `.wic` file in an editor | is its shape right? | `sophios --generate_schemas`, a JSON Schema generated from the AST |
| Pre-flight | this machine | can it run the compiled CWL? | `sophios.preflight`, before every local run |
| Runner | the compiled CWL | does the runner accept it? | cwltool or Toil, when the document is loaded |

The parser and the compiler report with `wic0NN` codes and positions; the
pre-flight with `wic0NN` codes of kind `machine`; the tool builder raises
`ToolBuilderValidationError` naming the field. The editor schema is an
over-approximation on purpose: JSON has no YAML tags and passthrough CWL is
open, so it describes the desugared projection, closes only the `wic:` block,
and admits everything else. It says nothing about which tools exist here;
that is the compiler's question.

## The Python API's own checks

`Step` and `Workflow` check a call as it is made: a value an input cannot
take, a link to a step outside the workflow or later in it, a scatter over an
input that is not array-valued, a `when` that is not an expression. These are
`api0NN` codes or the usual built-in exceptions, raised at the script line.
Required inputs may be left unbound, since inference fills them when the
workflow is compiled.

## The test suite

`tests/core/` holds the compiler's tests, `tests/contrib/` the contrib
surfaces'. The markers, declared in `pyproject.toml`:

- `fast`: unit and API tests; `pytest -m fast` is the quick local check;
- `slow`: workflow and integration tests;
- `serial`: tests that cannot run under `pytest --workers`;
- `needs_cwltool`: calls cwltool in-process; skipped off POSIX;
- `skip_pypi_ci`: left out of the packaging lane;
- `contrib`: a contrib surface rather than the core compiler;
- `sanity`: a static scan over the repository, run weekly.

Three kinds of test carry the promises the documentation makes:

- **Pairs.** `tests/core/test_cookbook.py` compiles every `docs/cookbook/`
  pair through both doors and requires the same CWL, job file and notes.
- **The corpus.** Every `.wic` reachable from `search_paths_wic`, which in
  CI includes `docs/tutorials/`, `docs/cookbook/` and the external workflow
  repositories, is compiled. Every push also runs them, except those the
  directory's `config_ci.json` lists as `fragments` (they read an input or
  edge a parent supplies), `intensive` (the weekly lane runs those) or
  `run_partial_failures`. `tests/core/test_examples.py` drives it.
- **The catalog.** `test_the_error_codes_page_says_what_the_code_says` keeps
  `docs/error_codes.md` equal to `EXPLANATIONS`; every code has a provocation
  in `tests/core/provocations.py` and an explanation, or the suite fails.

## Property-Based Testing

Sophios generates synthetic workflows from the language's own generators
(`tests/core/strategies.py`, `tests/core/ast_strategies.py`, with Hypothesis)
and checks that the language and the compiler behave consistently over them:

- the two spellings of every construct compile alike, and `parse` and
  `render` are inverses;
- compilation does not depend on how a workflow is partitioned into
  subworkflows: a flattened graph and the nested one agree at
  `UP_TO_RENAMING` (`tests/core/equivalence.py`);
- the emitted CWL says nothing CWL cannot read, and passthrough fields
  survive byte for byte (`tests/core/test_leak_boundary.py`).

Every property's example budget goes through `tests/core/budgets.py`.
`SOPHIOS_PROPERTY_SCALE` multiplies all of them; the weekly property lane
runs the hermetic suite at twenty times its usual depth with a fresh seed.
