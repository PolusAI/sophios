"""Step-only inputs and inherited requirements run under Nextflow as they run under cwltool (resolved-view §8, §9).

Each example is a batch of small workflows in one root workflow, run once under each runner, and the outputs are
compared by name and contents. The `DockerRequirement` cases of §8 and `greeting_if` run their tools in containers,
so they are a batch of their own, in a test that needs Docker.
"""

# pylint: disable=missing-function-docstring

from collections.abc import Iterable
from dataclasses import dataclass, field
import io
import json
from pathlib import Path
from typing import Any

from hypothesis import example, given, strategies as st
import pytest
import yaml

from sophios.backends import recorded_outputs
from sophios.input_output import write_artifacts_to_disk
from sophios.input_output_nf import write_nextflow_artifacts

from ..budgets import budget
from ..helpers import REPO_ROOT
from .testkit import assert_succeeded, compiled_case, execute_nextflow, lowered_case


def _adapter(name: str) -> dict[str, Any]:
    """A tool of cwl_adapters; one with no base command has an empty one, which a case's tool otherwise defaults."""
    document: dict[str, Any] = yaml.safe_load((REPO_ROOT / "cwl_adapters" / f"{name}.cwl").read_text("utf-8"))
    return {"baseCommand": [], **document}


@dataclass
class _Part:
    """One workflow of a batch: its steps, the tools they run, its inputs and their values, its outputs, and the
    requirements it writes on the batch's root."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    job: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    requirements: dict[str, Any] = field(default_factory=dict)


def _batch(parts: Iterable[_Part]) -> dict[str, Any]:
    """The parts as one case: one root workflow holding every part's steps, inputs and outputs."""
    whole = _Part()
    for part in parts:
        whole.steps += part.steps
        whole.tools += part.tools
        for mine, theirs in ((whole.inputs, part.inputs), (whole.job, part.job), (whole.outputs, part.outputs),
                             (whole.requirements, part.requirements)):
            mine.update(theirs)
    workflow = {"steps": whole.steps, "inputs": whole.inputs, "outputs": whole.outputs}
    return {"workflow": {**workflow, "requirements": whole.requirements} if whole.requirements else workflow,
            "tools": whole.tools, "job": whole.job}


def _source(name: str) -> dict[str, Any]:
    """A step input the tool does not declare, written as CWL's WorkflowStepInput: `!cwl {source: name}`."""
    return {"wic_raw_cwl": {"source": name}}


def _after(anchor: str) -> dict[str, Any]:
    """A step input the tool does not declare, fed by the output anchored as `anchor`: `!cwl {source: !* anchor}`."""
    return {"wic_raw_cwl": {"source": {"wic_alias": anchor}}}


def _echo(step: str, message: Any, **more: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """A step running `echo.cwl`, named `step`, its stdout anchored by its name; and its tool."""
    return ({"id": step, "in": {"message": message, **more}, "out": [{"stdout": {"wic_anchor": step}}]},
            {**_adapter("echo"), "id": step})


def _when(index: int, loud: bool) -> _Part:
    """The cookbook's `when` (docs/cookbook/when.wic): echo runs only when `loud` is true, which echo does not take."""
    name, loud_input = f"when{index}", f"when{index}_loud"
    step, tool = _echo(name, {"wic_inline_input": "Hello"}, enabled=_source(loud_input))
    said = {"type": "File?", "outputSource": f"{name}/stdout"}
    return _Part([{**step, "when": "$(inputs.enabled)"}], [tool], {loud_input: "boolean"}, {loud_input: loud},
                 {f"{name}_said": said})


def _waits() -> _Part:
    """§9's example 2: `query` reads nothing of `db`'s and must run after it."""
    db, db_tool = _echo("db", {"wic_inline_input": "db"})
    query, query_tool = _echo("query", {"wic_inline_input": "query"}, after=_after("db"))
    return _Part([db, query], [db_tool, query_tool],
                 outputs={"waited": {"type": "File", "outputSource": "query/stdout"}})


def _greet(step: str) -> dict[str, Any]:
    """A host tool that prints `SOPHIOS_GREETING`."""
    return {"id": step, "class": "CommandLineTool", "cwlVersion": "v1.2",
            "baseCommand": ["sh", "-c", 'printf %s "$SOPHIOS_GREETING"'], "stdout": "greeting.txt", "inputs": {},
            "outputs": {"out": {"type": "File", "outputBinding": {"glob": "greeting.txt"}}}}


def _environment() -> _Part:
    """An `EnvVarRequirement` on the root reaches a tool at the root; a nested step's own reaches its tool instead,
    being the innermost (§8)."""
    own = {"EnvVarRequirement": {"envDef": {"SOPHIOS_GREETING": "from the step"}}}
    nested = {"id": "nested.wic", "subtree": {"steps": [{"id": "inner", "requirements": own}],
                                              "outputs": {"out": {"type": "File", "outputSource": "inner/out"}}}}
    return _Part([{"id": "outer"}, nested], [_greet("outer"), _greet("inner")],
                 outputs={"outer_greeting": {"type": "File", "outputSource": "outer/out"},
                          "inner_greeting": {"type": "File", "outputSource": "nested.wic/out"}},
                 requirements={"EnvVarRequirement": {"envDef": {"SOPHIOS_GREETING": "from the root"}}})


#: Each workflow input a generated `when` reads, by name, with the conditions over it.
_READS = {
    "flag": ("boolean", ["inputs.flag", "!inputs.flag"]),
    "n": ("int", ["inputs.n > 1", "inputs.n == 2", "inputs.n <= 0"]),
    "s": ("string", ["inputs.s == 'a'", "inputs.s != ''"]),
}


@st.composite
def _generated(draw: st.DrawFn) -> dict[str, Any]:
    """A workflow of at most four steps, as data `_part` builds: each step's tool, words, the earlier step a later
    `append` appends to, the earlier steps it waits on, the workflow inputs it reads, and its `when`."""
    steps: list[dict[str, Any]] = []
    for index in range(draw(st.integers(1, 4))):
        appendable = [earlier for earlier, step in enumerate(steps) if step["when"] is None]
        reads = draw(st.lists(st.sampled_from(sorted(_READS)), unique=True, max_size=2))
        conditions = [draw(st.sampled_from(_READS[name][1])) for name in reads]
        joined, gated, appends = draw(st.sampled_from([" && ", " || "])), draw(st.booleans()), draw(st.booleans())
        waits: list[int] = draw(st.lists(st.integers(0, index - 1), unique=True, max_size=2)) if index else []
        steps.append({
            "words": draw(st.sampled_from(["a", "b b", "it's"])),
            "appends": draw(st.sampled_from(appendable)) if appendable and appends else None,
            "waits": waits,
            "reads": reads,
            "when": f"$({joined.join(conditions)})" if conditions and gated else None,
        })
    values = {"flag": draw(st.booleans()), "n": draw(st.integers(-1, 3)), "s": draw(st.sampled_from(["a", "b", ""]))}
    return {"steps": steps, "values": values}


def _part(index: int, drawn: dict[str, Any]) -> _Part:
    part = _Part(inputs={f"g{index}_{name}": kind for name, (kind, _) in _READS.items()},
                 job={f"g{index}_{name}": value for name, value in drawn["values"].items()})
    for number, spec in enumerate(drawn["steps"]):
        name = f"g{index}_{number}"
        waits = {f"after{earlier}": _after(f"g{index}_{earlier}") for earlier in spec["waits"]}
        reads = {read: _source(f"g{index}_{read}") for read in spec["reads"]}
        if spec["appends"] is None:
            step, tool = _echo(name, {"wic_inline_input": spec["words"]}, **waits, **reads)
            port = "stdout"
        else:
            step = {"id": name, "in": {"str": {"wic_inline_input": spec["words"]},
                                       "file": {"wic_alias": f"g{index}_{spec['appends']}"}, **waits, **reads},
                    "out": [{"file": {"wic_anchor": name}}]}
            tool, port = {**_adapter("append"), "id": name}, "file"
        part.steps.append({**step, "when": spec["when"]} if spec["when"] else step)
        part.tools.append(tool)
        part.outputs[f"o{index}_{number}"] = {"type": "File?", "outputSource": f"{name}/{port}"}
    return part


def _contents(value: Any) -> Any:
    """What a runner-independent observer sees of an output: a file's text, a captured text, or null."""
    return Path(value["path"]).read_bytes().decode("utf-8") if isinstance(value, dict) else value


def _under_nextflow(case: dict[str, Any], directory: Path) -> dict[str, Any]:
    write_nextflow_artifacts(lowered_case(case)[0], directory)
    result = execute_nextflow(directory, timeout=300)
    assert_succeeded(result)
    return {name: _contents(value) for name, value in recorded_outputs(result.stdout)}


def _under_cwltool(case: dict[str, Any], directory: Path) -> dict[str, Any]:
    import cwltool.main  # pylint: disable=import-outside-toplevel

    artifact = compiled_case(case).artifact
    write_artifacts_to_disk(artifact, directory, output_targets=())
    jobs = directory / f"{artifact.name}_inputs.yml"
    jobs.write_text(json.dumps({**(yaml.safe_load(jobs.read_text("utf-8")) or {}), **case["job"]}), "utf-8")
    stdout, stderr = io.StringIO(), io.StringIO()
    code = cwltool.main.main(
        ["--quiet", "--outdir", str(directory / "out"), "--tmpdir-prefix", f"{directory}/tmp/",
         "--tmp-outdir-prefix", f"{directory}/tmp_out/", str(directory / f"{artifact.name}.cwl"), str(jobs)],
        stdout=stdout, stderr=stderr)
    assert code == 0, stderr.getvalue()
    # The CWL target writes each step's outputs too; the case declares the ones it keeps.
    return {name: _contents(value) for name, value in json.loads(stdout.getvalue()).items()
            if name in case["workflow"]["outputs"]}


def _alike(case: dict[str, Any], root: Path) -> None:
    """The case's outputs under Nextflow are its outputs under cwltool, each by name and contents."""
    nextflow, cwltool = _under_nextflow(case, root / "nextflow"), _under_cwltool(case, root / "cwltool")
    assert nextflow == cwltool, yaml.safe_dump(case, sort_keys=False)


#: The hand-written cases: one batch.
HAND_WRITTEN = [_when(0, True), _when(1, False), _waits(), _environment()]


@pytest.mark.nextflow
@pytest.mark.serial
@pytest.mark.needs_cwltool
@budget(3)
@given(st.lists(_generated(), min_size=1, max_size=12).map(lambda drawn: [
    _part(index, part) for index, part in enumerate(drawn)]))
@example(HAND_WRITTEN)
def test_step_only_inputs_and_inherited_requirements_run_as_under_cwltool(
        tmp_path_factory: pytest.TempPathFactory, parts: list[_Part]) -> None:
    _alike(_batch(parts), tmp_path_factory.mktemp("batch"))


def _docker_case(index: int, outside: str, tool: str) -> _Part:
    """A nested workflow whose `outside` section and whose tool's `tool` section each name a DockerRequirement: the
    tool's requirement wins over the workflow's hint, and the workflow's requirement over the tool's hint (§8). The one
    that wins names Debian, whose name the tool prints; the other an image that does not exist."""
    images = dict.fromkeys((outside, tool), "sophios.invalid/never:1")
    images[tool if tool == "requirements" else outside] = "docker.io/library/debian:bookworm-slim"
    step = f"distro{index}"
    run = {"id": step, "class": "CommandLineTool", "cwlVersion": "v1.2",
           "baseCommand": ["sh", "-c", '. /etc/os-release && printf %s "$ID"'], "stdout": "distro.txt", "inputs": {},
           "outputs": {"out": {"type": "File", "outputBinding": {"glob": "distro.txt"}}},
           tool: {"DockerRequirement": {"dockerPull": images[tool]}}}
    nested = {"id": f"call{index}.wic", "subtree": {
        "steps": [{"id": step}], outside: {"DockerRequirement": {"dockerPull": images[outside]}},
        "outputs": {"out": {"type": "File", "outputSource": f"{step}/out"}}}}
    return _Part([nested], [run], outputs={f"distro{index}": {"type": "File", "outputSource": f"call{index}.wic/out"}})


def _greeting_if(index: int, show: bool) -> _Part:
    """The cookbook's `greeting_if` (docs/cookbook/greeting_if.wic): cat reads the greeting back only when `show`."""
    names = [f"touch{index}", f"append{index}", f"cat{index}"]
    steps: list[dict[str, Any]] = [
        {"id": names[0], "in": {"filename": {"wic_inline_input": "greeting.txt"}},
         "out": [{"file": {"wic_anchor": f"empty{index}"}}]},
        {"id": names[1], "in": {"file": {"wic_alias": f"empty{index}"}, "str": {"wic_inline_input": "Hello, Sophios"}},
         "out": [{"file": {"wic_anchor": f"greeting{index}"}}]},
        {"id": names[2], "when": "$(inputs.show)",
         "in": {"file": {"wic_alias": f"greeting{index}"}, "show": _source(f"show{index}")}},
    ]
    tools = [{**_adapter(tool), "id": name} for tool, name in zip(("touch", "append", "cat"), names)]
    return _Part(steps, tools, {f"show{index}": "boolean"}, {f"show{index}": show},
                 {f"read_back{index}": {"type": "string?", "outputSource": f"{names[2]}/output"}})


@pytest.mark.nextflow
@pytest.mark.docker
@pytest.mark.serial
@pytest.mark.needs_cwltool
def test_inherited_docker_requirements_and_greeting_if_run_as_under_cwltool(tmp_path: Path) -> None:
    _alike(_batch([_docker_case(0, "hints", "requirements"), _docker_case(1, "requirements", "hints"),
                   _greeting_if(0, True), _greeting_if(1, False)]), tmp_path)
