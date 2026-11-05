"""The shipped `append.cwl` appends the message as written, under Nextflow and under cwltool alike.

The tutorials and most cookbook workflows build on it. The message reaches `echo` as one quoted argument, so
shell syntax in it is text, and the tool appends to its own copy of the file, so the file it was given keeps
its contents.
"""

from functools import partial
from pathlib import Path

import pytest

from sophios.api.python.workflow import Step, Workflow

from .test_reference_differential import _run_cwltool, _run_nextflow
from .testkit import REPO_ROOT

#: What an unquoting shell would act on: a separator, a run of spaces, a quote, a substitution, a glob.
MESSAGE = "Hello;  it's $(echo substituted) *"


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.needs_cwltool
def test_the_shipped_append_tool_appends_the_message_as_written_under_both_runners(tmp_path: Path) -> None:
    """Each runner appends one line holding exactly the message, and the given file keeps its one line."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    for runner, run in (("nextflow", _run_nextflow), ("cwltool", partial(_run_cwltool, cwltool.main.main))):
        given = tmp_path / runner / "inputs" / "greeting.txt"
        given.parent.mkdir(parents=True)
        given.write_text("first line\n", encoding="utf-8")
        append = Step(clt_path=REPO_ROOT / "cwl_adapters" / "append.cwl")
        append.inputs.file = given
        append.inputs.str = MESSAGE
        workflow = Workflow([append], "append_as_written")
        workflow.outputs.appended = append.outputs.file

        appended = Path(run(workflow, tmp_path / runner / "run")["appended"]["path"])

        assert (appended.name, appended.read_text(encoding="utf-8")) == (
            "greeting.txt", f"first line\n{MESSAGE}\n"), runner
        assert given.read_text(encoding="utf-8") == "first line\n", f"{runner} changed the file it was given"
