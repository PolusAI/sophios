"""Multi-step workflows written in Sophios and run by Nextflow.

Each test builds one workflow in Python, writes the generated pipeline, runs it
under the pinned Nextflow, and compares the one report it writes with a literal.
The features: multi-input dotproduct and flat_crossproduct scatters over lists
built in Python; a scatter over an empty list, gathered downstream; an
unscattered step's output read by every task of a scattered step; same-named
files from several steps merged by linkMerge merge_flattened into one input;
same-named files from different directories in one File[] workflow input; and a
boolean parameter set in the params file and cleared on the command line.

Every task list is enumerated in Python when the workflow is built, and every
dependency is data that a task reads. Each workflow is built by a function of its
own, which the import's fixed point (test_import.py) reads as well.
"""

# pylint: disable=missing-function-docstring

import json
from pathlib import Path
from typing import Any

import pytest

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, StepInput, Workflow
from sophios.nf_types import NfWorkflowInputConnection

from .testkit import execute_nextflow

# One line per region the task covers.
_EMIT = 'group=$1; year=$2; shift 2; for region in "$@"; do printf "%s %s %s\\n" "$group" "$year" "$region"; done'
# One line, from the fetched file among the arguments that is this task's year.
_FROM_FETCH = ('year=$1; region=$2; shift 2; for data in "$@"; do read -r kind of < "$data"; '
               '[ "$of" = "$year" ] && printf "alpha %s %s %s\\n" "$year" "$region" "$kind"; done; true')
# A group's line count, then its lines, across every piece of every group.
_TALLY = ('group=$1; shift; lines=$(cat "$@" </dev/null | grep "^$group " || true); '
          'printf "%s %s\\n" "$group" "$(printf "%s" "$lines" | grep -c . || true)"; '
          '[ -z "$lines" ] || printf "%s\\n" "$lines"')
# The regions each year covers.
_REGIONS = {2001: ["north", "south"], 2002: ["north", "west"]}


def _sh(name: str, script: str, output: str, **inputs: Input) -> CommandLineTool:
    """A tool that runs `script` under sh with its inputs as positional parameters, and keeps stdout."""
    outputs = Outputs(**{output: Output(cwl.file, glob=f"{output}.txt")})
    tool = CommandLineTool(name, Inputs(**inputs), outputs)
    return tool.base_command("sh", "-c", script, name).stdout(f"{output}.txt")


def _report(**files: Any) -> Step:
    """One report: every file of each input, in input order."""
    tool = CommandLineTool(
        "report", Inputs(**{name: Input(cwl.array(cwl.file), position=index) for index, name in enumerate(files, 1)}),
        Outputs(report=Output(cwl.file, glob="report.txt")),
    ).base_command("cat").stdout("report.txt")
    report = Step(tool, step_name="report")
    for name, source in files.items():
        setattr(report.inputs, name, source)
    return report


def _run_report(workflow: Workflow, directory: Path, *arguments: str) -> str:
    """Write ``workflow``'s pipeline, run it with ``arguments``, and read the one report it writes."""
    run = execute_nextflow(workflow.compile(target="nextflow").write(directory), *arguments)
    assert run.returncode == 0, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    reports = list((directory / "work").rglob("report.txt"))
    assert len(reports) == 1, f"no single report was written:\n{run.stdout}"
    return reports[0].read_text(encoding="utf-8")


# Modelled on Matthew's repo, main.nf.
def plan_workflow() -> Workflow:
    """(year, region) pairs; a crossproduct; one task per year over a list; an empty scatter."""
    per_region = _sh("per_region", _EMIT, "piece", group=Input(cwl.string, position=1),
                     year=Input(cwl.int, position=2), region=Input(cwl.string, position=3))

    fetch = Step(_sh("fetch", 'printf "data %s\\n" "$1"', "data", year=Input(cwl.int, position=1)),
                 step_name="fetch")
    fetch.inputs.year = list(_REGIONS)
    fetch.scatter_on(fetch.inputs.year)

    # alpha: a dotproduct over (year, region) pairs; each task is given every
    # fetched file and keeps the one for its own year.
    pairs = [(year, region) for year, regions in _REGIONS.items() for region in regions]
    alpha = Step(_sh("from_fetch", _FROM_FETCH, "piece", year=Input(cwl.int, position=1),
                     region=Input(cwl.string, position=2), fetched=Input(cwl.array(cwl.file), position=3)),
                 step_name="alpha")
    alpha.inputs.year = [year for year, _ in pairs]
    alpha.inputs.region = [region for _, region in pairs]
    alpha.inputs.fetched = fetch.outputs.data
    alpha.scatter_on(alpha.inputs.year, alpha.inputs.region)

    # beta: a region list crossed with the years.
    beta = Step(per_region, step_name="beta")
    beta.inputs.group = "beta"
    beta.inputs.year = [2002, 2003]
    beta.inputs.region = ["north", "south"]
    beta.scatter_on(beta.inputs.year, beta.inputs.region, method="flat_crossproduct")

    # delta: one task per year over a whole region list.
    delta = Step(_sh("per_year", _EMIT, "piece", group=Input(cwl.string, position=1),
                     year=Input(cwl.int, position=2), regions=Input(cwl.array(cwl.string), position=3)),
                 step_name="delta")
    delta.inputs.group = "delta"
    delta.inputs.year = [2002, 2003]
    delta.inputs.regions = _REGIONS[2002]
    delta.scatter_on(delta.inputs.year)

    # gamma: no years, so no tasks; its tally still runs.
    gamma = Step(per_region, step_name="gamma")
    gamma.inputs.group = "gamma"
    gamma.inputs.year = []
    gamma.inputs.region = []
    gamma.scatter_on(gamma.inputs.year, gamma.inputs.region)

    groups = {"alpha": alpha, "beta": beta, "delta": delta, "gamma": gamma}
    tally = Step(_sh("tally", _TALLY, "tally", group=Input(cwl.string, position=1),
                     **{f"pieces_{name}": Input(cwl.array(cwl.file), position=2) for name in groups}),
                 step_name="tally")
    tally.inputs.group = list(groups)
    for name, group in groups.items():
        setattr(tally.inputs, f"pieces_{name}", group.outputs.piece)
    tally.scatter_on(tally.inputs.group)

    return Workflow([fetch, *groups.values(), tally, _report(tallies=tally.outputs.tally)], "plan")


@pytest.mark.nextflow
@pytest.mark.serial
def test_four_scatter_shapes_feed_one_scattered_tally(tmp_path: Path) -> None:
    """Every group's pieces reach every task of the tally; the empty scatter's tally still runs."""
    assert _run_report(plan_workflow(), tmp_path) == """\
alpha 4
alpha 2001 north data
alpha 2001 south data
alpha 2002 north data
alpha 2002 west data
beta 4
beta 2002 north
beta 2002 south
beta 2003 north
beta 2003 south
delta 4
delta 2002 north
delta 2002 west
delta 2003 north
delta 2003 west
gamma 0
"""


# The (year, month) pairs of each lane.
_LANES = {"lane_a": [(2001, 11), (2001, 12), (2002, 1), (2002, 2)], "lane_b": [(2002, 6), (2002, 7)]}


# Modelled on Matthew's repo, satellite.nf.
def months_workflow(headers: Path) -> Workflow:
    """Each lane's step is a dotproduct over (year, month) pairs written in Python; the report reads lane headers.

    One workflow input holds each lane's header, `<headers>/<lane>/header.txt`, every one named header.txt.
    """
    per_month = _sh("per_month", 'printf "%s %04d-%02d\\n" "$1" "$2" "$3"', "piece",
                    lane=Input(cwl.string, position=1), year=Input(cwl.int, position=2),
                    month=Input(cwl.int, position=3))
    steps = {}
    for lane, pairs in _LANES.items():
        step = steps[lane] = Step(per_month, step_name=lane)
        step.inputs.lane = lane
        step.inputs.year = [year for year, _ in pairs]
        step.inputs.month = [month for _, month in pairs]
        step.scatter_on(step.inputs.year, step.inputs.month)
    report = _report(headers=[{"class": "File", "path": str(headers / lane / "header.txt")} for lane in _LANES],
                     **{name: step.outputs.piece for name, step in steps.items()})
    return Workflow([*steps.values(), report], "months")


@pytest.mark.nextflow
@pytest.mark.serial
def test_one_step_per_lane_runs_one_task_per_month(tmp_path: Path) -> None:
    """One task per (year, month) pair of each lane, after the lanes' headers."""
    for lane in _LANES:
        (tmp_path / lane).mkdir()
        (tmp_path / lane / "header.txt").write_text(f"{lane} header\n", encoding="utf-8")
    assert _run_report(months_workflow(tmp_path), tmp_path) == """\
lane_a header
lane_b header
lane_a 2001-11
lane_a 2001-12
lane_a 2002-01
lane_a 2002-02
lane_b 2002-06
lane_b 2002-07
"""


# Modelled on Matthew's repo, nlcd.nf.
def summaries_workflow() -> Workflow:
    """A year's fetched file reaches every task of that year's summary; another step reads them all merged."""
    fetch_tool = _sh("fetch", 'printf "data %s\\n" "$1"', "data", year=Input(cwl.int, position=1))
    summarise_tool = _sh("summarise", 'read -r kind of < "$2"; printf "%s %s %s\\n" "$of" "$1" "$kind"', "summary",
                         region=Input(cwl.string, position=1), data=Input(cwl.file, position=2))
    fetches, summaries = [], {}
    for year, regions in _REGIONS.items():
        fetch = Step(fetch_tool, step_name=f"fetch_{year}")
        fetch.inputs.year = year
        summarise = summaries[f"summarise_{year}"] = Step(summarise_tool, step_name=f"summarise_{year}")
        summarise.inputs.region = regions
        summarise.inputs.data = fetch.outputs.data
        summarise.scatter_on(summarise.inputs.region)
        fetches.append(fetch)

    # Every task is given every fetched file, all named data.txt, and keeps the one for its own year.
    pairs = [(year, region) for year, regions in _REGIONS.items() for region in regions]
    across = Step(_sh("from_fetch", _FROM_FETCH, "summary", year=Input(cwl.int, position=1),
                      region=Input(cwl.string, position=2), fetched=Input(cwl.array(cwl.file), position=3)),
                  step_name="across")
    across.inputs.year = [year for year, _ in pairs]
    across.inputs.region = [region for _, region in pairs]
    across.inputs.fetched = StepInput(source=[fetch.outputs.data for fetch in fetches], link_merge="merge_flattened")
    across.scatter_on(across.inputs.year, across.inputs.region)

    report = _report(**{name: step.outputs.summary for name, step in {**summaries, "across": across}.items()})
    return Workflow([*fetches, *summaries.values(), across, report], "summaries")


@pytest.mark.nextflow
@pytest.mark.serial
def test_unscattered_outputs_reach_every_task_of_a_scattered_step(tmp_path: Path) -> None:
    """Each summary task keeps its year's file; the merged step keeps its own year's from all of them."""
    assert _run_report(summaries_workflow(), tmp_path) == """\
2001 north data
2001 south data
2002 north data
2002 west data
alpha 2001 north data
alpha 2001 south data
alpha 2002 north data
alpha 2002 west data
"""


# Modelled on Matthew's repo, quality.nf.
def tallies_workflow() -> Workflow:
    """Scattered groups, one with no tasks, merge_flattened into one input of a scattered tally."""
    per_region = _sh("per_region", _EMIT, "piece", group=Input(cwl.string, position=1),
                     year=Input(cwl.int, position=2), region=Input(cwl.string, position=3))
    groups = {name: Step(per_region, step_name=name) for name in ("alpha", "beta", "gamma")}
    for name, group in groups.items():
        group.inputs.group = name
    alpha, beta, gamma = groups.values()
    alpha.inputs.year, alpha.inputs.region = [2001, 2002], ["north", "south"]
    beta.inputs.year, beta.inputs.region = [2002, 2003], ["north", "west"]
    gamma.inputs.year, gamma.inputs.region = [], []
    alpha.scatter_on(alpha.inputs.year, alpha.inputs.region)
    beta.scatter_on(beta.inputs.year, beta.inputs.region, method="flat_crossproduct")
    gamma.scatter_on(gamma.inputs.year, gamma.inputs.region)

    tally = Step(_sh("tally", _TALLY, "tally", group=Input(cwl.string, position=1),
                     pieces=Input(cwl.array(cwl.file), position=2)), step_name="tally")
    tally.inputs.group = list(groups)
    tally.inputs.pieces = StepInput(source=[group.outputs.piece for group in groups.values()],
                                    link_merge="merge_flattened")
    tally.scatter_on(tally.inputs.group)

    return Workflow([*groups.values(), tally, _report(tallies=tally.outputs.tally)], "tallies")


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_tally_per_group_reads_every_groups_pieces_merged_into_one_input(tmp_path: Path) -> None:
    """Each group's tally counts its own pieces among every group's."""
    assert _run_report(tallies_workflow(), tmp_path) == """\
alpha 2
alpha 2001 north
alpha 2002 south
beta 4
beta 2002 north
beta 2002 west
beta 2003 north
beta 2003 west
gamma 0
"""


# Modelled on Matthew's repo, quality.nf.
def flags_workflow() -> Workflow:
    """Three boolean flags, the params file setting strict and verbose and clearing fast."""
    flags = Step(_sh("flags", 'printf "%s\\n" "$@"', "report", strict=Input(cwl.boolean, position=1, flag="--strict"),
                     verbose=Input(cwl.boolean, position=2, flag="--verbose"),
                     fast=Input(cwl.boolean, position=3, flag="--fast")), step_name="flags")
    flags.inputs.strict, flags.inputs.verbose, flags.inputs.fast = True, True, False
    return Workflow([flags], "flags")


@pytest.mark.nextflow
@pytest.mark.serial
def test_a_command_line_false_overrides_a_flag_the_params_file_sets(tmp_path: Path) -> None:
    """The params file sets strict and verbose and clears fast; the command line clears verbose."""
    workflow = flags_workflow()
    verbose = next(connection.from_port
                   for connection in workflow.compile(target="nextflow").nextflow_workflow.connections
                   if isinstance(connection, NfWorkflowInputConnection) and connection.to_port == "verbose")
    assert _run_report(workflow, tmp_path, f"--{verbose}", "false") == "--strict\n"
    assert json.loads((tmp_path / "nextflow_params.json").read_text(encoding="utf-8"))[verbose] is True
