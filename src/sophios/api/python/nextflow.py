"""Public Nextflow compilation types, artifact writers, and the import of a pipeline Sophios wrote.

``import_nextflow(directory)`` reads the three files Nextflow itself reads,
``workflow.nf``, ``nextflow_params.json`` and ``nextflow.config``, and builds
one Sophios ``Workflow`` through this API: a ``CommandLineTool`` made a step
for each process, in the order the pipeline calls them; each parameter a
workflow input holding its value; and every edge bound on the input it feeds.
Compiled to Nextflow with inference disabled, the workflow gives back the
model the files were written from (design §4.2). The import reads nothing
else: not ``nextflow_workflow.json``, no tool registry, no network, and no
path a parameter names.

It reads only what Sophios writes, and the one freedom is layout: indentation,
the length of a run of spaces or tabs, blank lines, comments, the order of the
process blocks and CR LF line endings, and JSON's own freedom in the params
file. Anything else stops it with a coded diagnostic at its line, ``wic047``
to ``wic054``, and nothing is returned. A pipeline no workflow built through
this API compiles to as it is written is ``wic054``: one with a name the
compile would change, such as a capitalized process name, or with a port of
a CWL tool named like an attribute of ``Inputs`` or ``Outputs``, such as
``keys``.

What the compile does not keep does not come back:

- original names: the import keeps the Nextflow spellings, in which ``~``,
  ``@`` and every character Nextflow does not allow are ``_``, and a reserved,
  capitalized or script-declared name has a leading ``_``, which the import
  keeps; normalization is idempotent, so a second round trip is exact;
- nesting: subworkflows are inlined, so the workflow is flat;
- labels, docs, the other compile-time fields, and Sophios-only hints;
- ``cudaDeviceCountMax``: the ``accelerator`` count is read as
  ``cudaDeviceCountMin``;
- comments and formatting;
- every choice that lowers to the same model: where a value came from (a tool
  or step default, an unbound optional input and a value written on a step
  each become a workflow input ``<step>_<port>`` holding it); a maximum
  resource; ``dockerImageId``; whether a requirement was a hint;
  ``$(inputs.<name>.path)``; ``type: stdout``; whether a token came from an
  argument or an input's own binding, and its position; a single-input
  scatter's method; a ``when``'s spelling; adjacent ``shellQuote: false``
  texts; and a tool's input order.
"""

from pathlib import Path
from typing import Any, NoReturn

from sophios.input_output_nf import (
    render_nextflow,
    render_nextflow_config,
    render_nextflow_params,
    write_nextflow_artifacts,
)
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.lang.spans import SourceSpan
from sophios.nf_expr import Expr, references, source_text
from sophios.nf_reader import read_nextflow
from sophios.nf_types import (
    MULTI_INPUT_ADAPTERS,
    ExecutableNextflowWorkflow,
    NfArrayBinding,
    NfBasenameReference,
    NfCommand,
    NfCommandToken,
    NfConnection,
    NfEntry,
    NfEnvVar,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfOptionalBinding,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfShellLiteral,
    NfSourceConnection,
    NfStepInputConnection,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)

from .tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from .workflow import Inference, Step, StepInput, Workflow


__all__ = [
    "ExecutableNextflowWorkflow",
    "NfArrayBinding",
    "NfBasenameReference",
    "NfCommand",
    "NfCommandToken",
    "NfConnection",
    "NfEntry",
    "NfEnvVar",
    "NfFlag",
    "NfInputReference",
    "NfLiteral",
    "NfOptionalBinding",
    "NfPort",
    "NfProcess",
    "NfProcessConnection",
    "NfResources",
    "NfShellLiteral",
    "NfStepInputConnection",
    "NfTemplate",
    "NfWorkflowInputConnection",
    "NfWorkflowOutputConnection",
    "import_nextflow",
    "render_nextflow",
    "render_nextflow_config",
    "render_nextflow_params",
    "write_nextflow_artifacts",
]

_KINDS = {"file": cwl.file, "directory": cwl.directory}
#: A connection into a process input.
_Incoming = NfSourceConnection | NfStepInputConnection


def import_nextflow(directory: str | Path) -> Workflow:
    """Import the pipeline Sophios wrote into ``directory`` as one Sophios workflow.

    Compiled to Nextflow with inference disabled, the workflow gives back the
    model the three files were written from; the module docstring says what
    that model does not keep.

    Args:
        directory (str | Path): The directory holding ``workflow.nf``,
            ``nextflow_params.json`` and ``nextflow.config``.

    Raises:
        SophiosError: The files are not a pipeline Sophios writes, or no
            workflow built through this API compiles to them, with a code from
            ``wic047`` to ``wic054`` at the first line that is not.
        OSError: A file cannot be read.

    Returns:
        Workflow: The workflow, its steps in the order the pipeline calls them.
    """
    model, heads = read_nextflow(directory)
    incoming: dict[tuple[str, str], _Incoming] = {
        (edge.to_process, edge.to_port): edge for edge in model.connections
        if not isinstance(edge, NfWorkflowOutputConnection)}
    steps: dict[str, Step] = {}
    for process in model.processes:
        try:
            steps[process.name] = _tool(model, process, incoming).to_step()
        except (ValueError, TypeError) as exc:
            _stop(heads[process.name], f"process {process.name}, which the Python API cannot write: {exc}")
    try:
        workflow = Workflow(list(steps.values()), model.name)
        _bind(model, workflow, steps)
        compiled = workflow.compile(target="nextflow", inference=Inference(enabled=False))
    except (ValueError, TypeError, SophiosError) as exc:
        _stop(heads[model.name], f"workflow {model.name}, which no Python workflow compiles to: {exc}")
    for written, process in zip(compiled.processes, model.processes):
        if written != process:
            _stop(heads[process.name], f"process {process.name}, which no Python workflow compiles to as it is "
                                       f"written; the compile writes process {written.name}")
    if compiled != model:
        _stop(heads[model.name], f"workflow {model.name}, which no Python workflow compiles to as it is written")
    return workflow


def _stop(span: SourceSpan, what: str) -> NoReturn:
    raise SophiosError.error(SophiosErrorCode.IMPORT_UNWRITABLE, what, span=span)


def _text(template: NfTemplate) -> str:
    """The CWL text a template lowers from; literal text is escaped when the text interpolates."""
    interpolates = any(not isinstance(segment, NfLiteral) or "$(" in segment.value or "${" in segment.value
                       for segment in template.segments)
    return "".join(
        (segment.value.replace("\\", "\\\\").replace("$(", "\\$(").replace("${", "\\${") if interpolates
         else segment.value) if isinstance(segment, NfLiteral)
        else f"$(inputs.{segment.name}.basename)" if isinstance(segment, NfBasenameReference)
        else f"$(inputs.{segment.name})"
        for segment in template.segments)


def _argument(template: NfTemplate, position: int) -> dict[str, Any]:
    """An argument that lowers to ``template``: a leading literal is a prefix joined to the rest, as an input's own
    binding writes one, which CWL neither interpolates nor strips."""
    first, *rest = template.segments
    if isinstance(first, NfLiteral) and rest:
        return {"value": _text(NfTemplate(tuple(rest))), "prefix": first.value, "separate": False,
                "position": position}
    return {"value": _text(template), "position": position}


def _item(model: ExecutableNextflowWorkflow, edge: _Incoming) -> str:
    """The CWL type of each value an edge delivers."""
    if isinstance(edge, NfStepInputConnection):
        return _item(model, edge.sources[0])
    if isinstance(edge, NfWorkflowInputConnection):
        return model.param_types[edge.from_port].removesuffix("?").removesuffix("[]")
    output = next(port for process in model.processes if process.name == edge.from_process
                  for port in process.outputs if port.name == edge.from_port)
    return cwl.string if output.capture == "text" else _KINDS[output.path_kind or "file"]


def _null_compared(node: Expr) -> set[str]:
    """The inputs a condition compares with ``null``, which are optional."""
    found = {node.args[0].value} if node.op in ("==", "!=") and (node.args[0].op, node.args[1].op) == ("ref", "null") \
        else set()
    return found.union(*(_null_compared(arg) for arg in node.args))


def _optional(model: ExecutableNextflowWorkflow, process: NfProcess, port: NfPort, edge: _Incoming) -> bool:
    """Whether the tool input is optional: as its binding or the condition needs, else as its source may be absent."""
    if any(isinstance(token, NfOptionalBinding) and token.name == port.name for token in process.command.tokens):
        return True
    if process.condition is not None and port.name in references(process.condition):
        return port.name in _null_compared(process.condition)
    match edge:
        case NfWorkflowInputConnection(from_port=name, adapter=None):
            return model.param_types[name].endswith("?")
        case NfProcessConnection(from_process=source, from_port=emit, adapter=None):
            producer = next(process for process in model.processes if process.name == source)
            return producer.condition is not None or next(
                output.optional for output in producer.outputs if output.name == emit)
    return False


def _input(model: ExecutableNextflowWorkflow, process: NfProcess, port: NfPort, edge: _Incoming,
           binding: dict[str, Any]) -> Input:
    item = _item(model, edge)
    return Input(cwl.array(item) if port.is_array else item, required=not _optional(model, process, port, edge),
                 **binding)


def _output(port: NfPort) -> Output:
    glob = _text(port.glob or NfTemplate(()))
    if port.capture == "text":
        return Output(cwl.string, glob=glob, load_contents=True, output_eval="$(self[0].contents)")
    return Output(_KINDS[port.path_kind or "file"], glob=glob, required=not port.optional,
                  output_eval="$(self[0])" if port.capture == "single" else None)


def _tool(model: ExecutableNextflowWorkflow, process: NfProcess,
          incoming: dict[tuple[str, str], _Incoming]) -> CommandLineTool:
    """The tool that lowers to ``process``: leading literal words are its base command, and every other token
    has its index among the rest as its position."""
    command = process.command
    lead = 0
    while lead < len(command.tokens) and isinstance(token := command.tokens[lead], NfTemplate) and all(
            isinstance(segment, NfLiteral) for segment in token.segments):
        lead += 1
    # A base command is not interpolated: each word is its literal text.
    words = [segment.value for token in command.tokens[:lead] if isinstance(token, NfTemplate)
             for segment in token.segments if isinstance(segment, NfLiteral)]
    bindings: dict[str, dict[str, Any]] = {}
    arguments: list[dict[str, Any]] = []
    for position, token in enumerate(command.tokens[lead:]):
        match token:
            case NfArrayBinding(name=name, prefix=prefix):
                bindings[name] = {"position": position, "flag": prefix}
            case NfOptionalBinding(name=name, prefix=prefix, separate=separate):
                bindings[name] = {"position": position, "flag": prefix, "separate": None if separate else False}
            case NfFlag(name=name, prefix=prefix):
                arguments.append({"value": f"$(inputs.{name})", "prefix": prefix, "position": position})
            case NfShellLiteral(text=text):
                arguments.append({"value": text, "shell_quote": False, "position": position})
            case NfTemplate():
                arguments.append(_argument(token, position))
    inputs = Inputs(**{port.name: _input(model, process, port, incoming[(process.name, port.name)],
                                         bindings.get(port.name, {}))
                       for port in process.inputs})
    tool = CommandLineTool(process.name, inputs, Outputs(**{port.name: _output(port) for port in process.outputs}))
    if words:
        tool.base_command(*words)
    for argument in arguments:
        tool.argument(**argument)
    if any(isinstance(token, NfShellLiteral) for token in command.tokens):
        tool.shell_command()
    for stream, target in (("stdin", command.stdin), ("stdout", command.stdout), ("stderr", command.stderr)):
        if target is not None:
            getattr(tool, stream)(_text(target))
    for variable in command.env:
        tool.env_var(variable.name, _text(variable.value))
    for port in process.inputs:
        if port.entry is not None:
            tool.stage(inputs[port.name], writable=port.entry.copy, entryname=port.entry.name)
    if process.container is not None:
        tool.docker(process.container)
    resources = process.resources
    if resources.cpus is not None or resources.memory_mb is not None:
        tool.resources(cores_min=resources.cpus, ram_min=resources.memory_mb)
    if resources.accelerator is not None:
        capability = resources.cuda_compute_capability
        tool.gpu(device_count_min=resources.accelerator, cuda_version_min=resources.cuda_version_min,
                 extra=None if capability is None else {
                     "cudaComputeCapability": capability if isinstance(capability, str) else list(capability)})
    return tool


def _source(workflow: Workflow, steps: dict[str, Step], edge: NfSourceConnection) -> Any:
    if isinstance(edge, NfWorkflowInputConnection):
        return getattr(workflow.inputs, edge.from_port)
    return getattr(steps[edge.from_process].outputs, edge.from_port)


def _bind(model: ExecutableNextflowWorkflow, workflow: Workflow, steps: dict[str, Step]) -> None:
    """Each parameter a workflow input holding its value, each edge bound on its input, each scatter and condition."""
    values = model.to_dict()["params"]
    for name, declared in model.param_types.items():
        getattr(workflow.inputs, name).as_type(declared)
        # The [] of an optional scalar stands for its absence; an array's is the empty array.
        if not (declared.endswith("?") and not declared.endswith("[]?") and values[name] == []):
            setattr(workflow.inputs, name, values[name])
    for edge in model.connections:
        match edge:
            case NfWorkflowOutputConnection(from_process=source, from_port=emit, to_port=name):
                setattr(workflow.outputs, name, getattr(steps[source].outputs, emit))
            case NfStepInputConnection(sources=sources, link_merge=link_merge, pick_value=pick_value):
                setattr(steps[edge.to_process].inputs, edge.to_port, StepInput(
                    source=[_source(workflow, steps, source) for source in sources], link_merge=link_merge,
                    pick_value=pick_value))
            case NfWorkflowInputConnection() | NfProcessConnection():
                setattr(steps[edge.to_process].inputs, edge.to_port, _source(workflow, steps, edge))
    for process in model.processes:
        step = steps[process.name]
        scattered = [edge for edge in model.connections
                     if isinstance(edge, NfSourceConnection)
                     and edge.to_process == process.name and edge.adapter in MULTI_INPUT_ADAPTERS]
        if scattered:
            ports = (getattr(step.inputs, edge.to_port) for edge in scattered)
            step.scatter_on(*ports, method=str(scattered[0].adapter))
        if process.condition is not None:
            step.when = f"$({source_text(process.condition)})"
