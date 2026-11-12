"""A path input's references mean under Nextflow what they mean under cwltool (design §6): the idiom table's tests
read text, but `.name` of an input staged under a directory is the whole staged path, which only a run shows."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
from functools import partial
import hashlib
import io
import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import pytest

from sophios.api.python.nextflow import NotLowered
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, Workflow
from sophios.backends import recorded_outputs

from .testkit import assert_succeeded, execute_nextflow

#: One script for every cell. $1, $2, $3 are `$(inputs.x)`, `$(inputs.x.path)`, `$(inputs.x.basename)` as
#: command arguments, $4 a name derived from the basename, $5 how the input is staged. It prints, for each
#: reference, what a runner-independent observer sees: the last part of the path it names, whether that is
#: one object with the others, its contents, and, for an entry, whether it is the entry in the task directory.
#: A writable entry is written through every reference; its own-basename glob must then hold all three files.
PROBE = r'''
plain=$1 path=$2 base=$3 made=$4 mode=$5
contents() { if [ -d "$1" ]; then (cd -- "$1" && find . -type f | LC_ALL=C sort | xargs cksum); else cksum < "$1"; fi; }
here=$(pwd -P)
printf 'basename reference: %s\n' "$base"
for ref in "$plain" "$path"; do printf 'names: %s\n' "${ref##*/}"; contents "$ref"; done
[ "$plain" -ef "$path" ] && echo 'plain and path name one object'
if [ "$mode" != plain ]; then
  for ref in "$plain" "$path" "$base"; do
    if [ "$(cd -P -- "$(dirname -- "$ref")" && pwd)" = "$here" ]; then echo 'the entry in the task directory'
    else printf 'not the entry: %s\n' "${ref%/*}"; fi
    contents "$ref"
  done
fi
if [ -d "$plain" ]; then cp -RL -- "$plain" "$made"; else cp -- "$plain" "$made"; fi
if [ "$mode" = writable ]; then
  echo plain > "$plain/via_plain.txt"; echo path > "$path/via_path.txt"; echo base > "$base/via_base.txt"
fi
'''


def _probe(name: str, kind: Any, mode: str) -> CommandLineTool:
    """A tool whose stdout and outputs are named after its input's basename, staged as ``mode`` says."""
    outputs = {
        "report": Output(cwl.file, glob="$(inputs.x.basename).report"),
        "made": Output(kind, glob="$(inputs.x.basename).made"),
    }
    if mode != "plain":
        # An entry under its own basename is the one input a glob may name, by basename or by path (design §6);
        # by its plain reference, the object, neither runner takes it: the last test below.
        outputs |= {"own": Output(kind, glob="$(inputs.x.basename)"), "own_path": Output(kind, glob="$(inputs.x.path)")}
    tool = (
        CommandLineTool(name, Inputs(x=Input(kind)), Outputs(**outputs))
        .base_command("sh", "-c", PROBE, "probe")
        .argument("$(inputs.x)", position=1)
        .argument("$(inputs.x.path)", position=2)
        .argument("$(inputs.x.basename)", position=3)
        .argument("$(inputs.x.basename).made", position=4)
        .argument(mode, position=5)
        .stdout("$(inputs.x.basename).report")
    )
    return tool if mode == "plain" else tool.stage(tool.inputs.x, writable=mode == "writable")


def _inputs(root: Path) -> dict[str, Any]:
    """The workflow's input values, written under ``root``: each runner gets its own identical copy."""
    root.mkdir(parents=True)

    def folder(name: str) -> Path:
        (root / name / "sub").mkdir(parents=True)
        (root / name / "keep.txt").write_text(f"{name} keep\n", encoding="utf-8")
        (root / name / "sub" / "inner.txt").write_text(f"{name} inner\n", encoding="utf-8")
        return root / name

    def file(name: str) -> Path:
        (root / name).write_text(f"{name} text\n", encoding="utf-8")
        return root / name

    return {
        "plain_file": file("plain.txt"),
        "plain_dir": folder("plain_dir"),
        "entry_file": file("entry.txt"),
        "entry_dir": folder("entry_dir"),
        "scattered": [file("alpha.txt"), file("beta.txt")],
        "scattered_entry": [folder("one"), folder("two")],
    }


def _workflow(values: dict[str, Any]) -> Workflow:
    """Every cell in one workflow: each staging, each reference form, each position."""
    cells = {
        "plain_file": (cwl.file, "plain"),
        "plain_dir": (cwl.directory, "plain"),
        "entry_file": (cwl.file, "entry"),
        "entry_dir": (cwl.directory, "writable"),
        "scattered": (cwl.file, "plain"),
        "scattered_entry": (cwl.directory, "writable"),
    }
    steps = []
    workflow_outputs = []
    for name, (kind, mode) in cells.items():
        step = Step(_probe(name, kind, mode), step_name=name)
        step.inputs.x = values[name]
        if name.startswith("scattered"):
            step.scatter_on(step.inputs.x)
        steps.append(step)
        for output in ("report", "made") if mode == "plain" else ("report", "made", "own", "own_path"):
            workflow_outputs.append((f"{name}_{output}", getattr(step.outputs, output)))
    workflow = Workflow(steps, "reference_differential")
    for name, output in workflow_outputs:
        setattr(workflow.outputs, name, output)
    return workflow


def _tree(path: Path) -> Any:
    """What a runner-independent observer sees at ``path``: a file's text, or a directory's files and texts."""
    if path.is_dir():
        return {str(item.relative_to(path)): _tree(item) for item in sorted(path.rglob("*")) if item.is_file()}
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return hashlib.sha256(data).hexdigest()


def _observed(value: Any) -> Any:
    """An output as a runner-independent observer sees it: a File's or Directory's class, basename and contents,
    never its path; null and a string, number or boolean as they are."""
    if isinstance(value, list):
        return [_observed(item) for item in value]
    if not isinstance(value, dict):
        return value
    # cwltool gives a Directory it left where its step made it a location and no path, and an entry it captured the
    # path of the file the entry links to: the name is the object's.
    path = Path(value["path"] if "path" in value else unquote(urlsplit(value["location"]).path))
    return {"class": value["class"], "basename": value.get("basename", path.name), "contents": _tree(path)}


def _snapshot(values: dict[str, Any]) -> dict[str, Any]:
    flat = [item for value in values.values() for item in (value if isinstance(value, list) else [value])]
    return {path.name: _tree(path) for path in flat}


def _run_nextflow(workflow: Workflow, directory: Path) -> dict[str, Any]:
    result = execute_nextflow(workflow.compile(target="nextflow").write(directory))
    assert_succeeded(result)
    return dict(recorded_outputs(result.stdout))


def _cwltool(main: Callable[..., int], workflow: Workflow, directory: Path) -> tuple[int, str, str]:
    """cwltool's exit code, output object and messages for the CWL `workflow` compiles to, written in `directory`.
    Each output stays where its step left it: moved into one directory, two steps' out.txt could not both keep
    the name."""
    compiled = workflow.compile()
    document, job = compiled.write_cwl(directory), compiled.write_job_inputs(directory)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(
        ["--quiet", "--leave-outputs", "--leave-tmpdir", "--tmpdir-prefix", f"{directory}/tmp/",
         "--tmp-outdir-prefix", f"{directory}/tmp_out/", str(document), str(job)],
        stdout=stdout, stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def _run_cwltool(main: Callable[..., int], workflow: Workflow, directory: Path) -> dict[str, Any]:
    code, outputs, messages = _cwltool(main, workflow, directory)
    assert code == 0, messages
    return cast(dict[str, Any], json.loads(outputs))


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.needs_cwltool
def test_a_path_inputs_references_mean_under_nextflow_what_they_mean_under_cwltool(tmp_path: Path) -> None:
    """Plain File and Directory inputs, an entry linked and a writable one copied, and scattered elements of
    both, each referenced plainly, by path and by basename, as an argument, the stdout target and a glob. The one
    cell left out, an entry under its own basename globbed by its plain reference, neither runner takes: CWL reads
    `$(inputs.x)` as the Directory object, and a glob must be a string, so cwltool fails the step, and the Nextflow
    target refuses it when compiling instead of running what CWL does not (design §6)."""
    import cwltool.main  # pylint: disable=import-outside-toplevel

    observed, problems = {}, []
    for runner, run in (("nextflow", _run_nextflow), ("cwltool", partial(_run_cwltool, cwltool.main.main))):
        values = _inputs(tmp_path / runner / "inputs")
        before = _snapshot(values)
        outputs = run(_workflow(values), tmp_path / runner / "run")
        observed[runner] = {name: _observed(value) for name, value in sorted(outputs.items())}
        after = _snapshot(values)
        problems += [f"{runner} changed its input {name}: {after[name]}"
                     for name in before if after[name] != before[name]]
    nf, cw = observed["nextflow"], observed["cwltool"]
    problems += [
        f"output {name}:\n  nextflow: {nf.get(name)}\n  cwltool:  {cw.get(name)}"
        for name in sorted(nf.keys() | cw.keys()) if nf.get(name) != cw.get(name)
    ]
    assert not problems, "\n".join(problems)

    _neither_globs_an_entrys_object(cwltool.main.main, tmp_path / "object_glob")


def _neither_globs_an_entrys_object(main: Callable[..., int], directory: Path) -> None:
    """A writable entry globbed by its plain reference: refused by the Nextflow target, failed by cwltool."""
    fill = CommandLineTool("fill", Inputs(x=Input(cwl.directory, position=1)),
                           Outputs(o=Output(cwl.directory, glob="$(inputs.x)"))).base_command("touch")
    step = Step(fill.stage(fill.inputs.x, writable=True), step_name="fill")
    (directory / "in.d").mkdir(parents=True)
    step.inputs.x = directory / "in.d"
    workflow = Workflow([step], "object_glob")
    workflow.outputs.o = step.outputs.o
    with pytest.raises(NotLowered) as refused:
        workflow.compile(target="nextflow")
    assert [(refusal.code.value, refusal.subject) for refusal in refused.value.refusals] == [
        ("nf-s046", "fill.cwl: outputs.o.outputBinding.glob")]
    code, _, messages = _cwltool(main, workflow, directory)
    assert code != 0 and "Resolved glob patterns must be strings" in messages, messages
