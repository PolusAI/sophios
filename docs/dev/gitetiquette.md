# Git Etiquette

## Branching Model

Prefer short-lived branches that are merged back after review and passing
checks. Long-running branches make workflow and API refactors harder to
integrate.

## Rebasing and Squashing

Clean branch history before merge when it is safe to do so. Interactive rebase
is useful for local branches, but avoid rewriting shared history unless the team
has agreed to it.

Squash merges are often a good default for feature branches because the branch
history remains available while `main` stays readable.

## Commit Frequency

Make commits logically focused. A good commit should be easy to review, easy to
revert, and limited to one coherent change.

## Pull Requests

Keep pull requests focused. Large PRs are harder to review well, especially when
they mix docs, API behavior, generated files, and workflow fixtures.

## Testing Before Push

Run the focused tests for the area you changed. For Python API changes, this is
a good baseline:

```bash
pytest tests/test_python_api.py tests/test_tool_builder.py -q
```

For broader workflow changes, also run:

```bash
pytest -m serial
pytest -m "not serial" --workers 8
```

Runtime workflow tests may require Docker or Podman.

## Staging

Use `git status` before staging. Avoid `git add *`; generated files and local
runner outputs are easy to stage accidentally.
