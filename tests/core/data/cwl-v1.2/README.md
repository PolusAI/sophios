# CWL v1.2 conformance workflows

Copied unchanged from https://github.com/common-workflow-language/cwl-v1.2 at
`979083396fee912fca8ef778174216d317338a00`, with the `tests/` layout kept so relative references
resolve. Licensed under the Apache License 2.0 (`LICENSE.txt` in that repository).

`cases.yaml` names twelve entries of its `conformance_tests.yaml`: plain `class: Workflow`
documents (one packed `$graph` with a `main` Workflow), none of which needs a container.
`tests/core/test_plain_cwl.py` runs each through `sophios --allow_raw_cwl --run_local` and
through cwltool directly, and compares the two output objects.
