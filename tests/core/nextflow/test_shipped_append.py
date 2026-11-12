"""The shipped `append.cwl` appends the message as written under cwltool; the Nextflow half is a `runs.yaml` case."""

from pathlib import Path

import pytest

from sophios.api.python.workflow import Workflow

from ..helpers import adapter_step
from .test_reference_differential import _run_cwltool

#: What an unquoting shell would act on: a separator, a run of spaces, a quote, a substitution, a glob.
MESSAGE = "Hello;  it's $(echo substituted) * a\\tb\\nc 100%s"


@pytest.mark.needs_cwltool
def test_the_shipped_append_tool_appends_the_message_as_written_under_cwltool(tmp_path: Path) -> None:
    """One line holding exactly the message is appended, and the given file keeps its one line."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    given = tmp_path / "inputs" / "greeting.txt"
    given.parent.mkdir()
    given.write_text("first line\n", encoding="utf-8")
    append = adapter_step("append", file=given, str=MESSAGE)
    workflow = Workflow([append], "append_as_written")
    workflow.outputs.appended = append.outputs.file

    appended = Path(_run_cwltool(cwltool.main.main, workflow, tmp_path / "run")["appended"]["path"])

    assert (appended.name, appended.read_text(encoding="utf-8")) == ("greeting.txt", f"first line\n{MESSAGE}\n")
    assert given.read_text(encoding="utf-8") == "first line\n"
