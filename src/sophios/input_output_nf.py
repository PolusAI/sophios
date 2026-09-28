"""Deterministic serializers for the supported Nextflow DSL2 subset."""

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from .nf_expr import Expr, NF_EXPRESSION_FUNCTIONS, NF_NUMBER_TEXT_HELPER, references, render_groovy, source_text
from .nf_types import (
    NF_NEST_HELPER,
    MULTI_INPUT_ADAPTERS,
    ExecutableNextflowWorkflow,
    NF_LOAD_CONTENTS_HELPER,
    NF_LOAD_CONTENTS_LIMIT,
    NF_SCATTER_INDEX_NAME,
    NF_SHELL_QUOTE_HELPER,
    NfArrayBinding,
    NfBasenameReference,
    NfConnection,
    NfFlag,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfShellLiteral,
    NfComputed,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
    process_dependencies,
    topological_order,
)


NEXTFLOW_JSON = "nextflow_workflow.json"
NEXTFLOW_SCRIPT = "workflow.nf"
NEXTFLOW_CONFIG = "nextflow.config"
NEXTFLOW_PARAMS = "nextflow_params.json"
NF_SHELL_QUOTE_FUNCTION = f'''def {NF_SHELL_QUOTE_HELPER}(value) {{
    return "'" + value.toString().replace("'", "'\\\"'\\\"'") + "'"
}}'''
# CWL v1.2 loadContents semantics, verbatim: read the whole file, fail above
# the byte limit, and decode UTF-8 strictly. CharsetDecoder.decode reports
# malformed input instead of substituting U+FFFD, and getText-style reads
# preserve a trailing newline where shell capture would strip it.
NF_LOAD_CONTENTS_FUNCTION = f'''def {NF_LOAD_CONTENTS_HELPER}(path) {{
    def bytes = path.readBytes()
    if( bytes.length > {NF_LOAD_CONTENTS_LIMIT} )
        throw new IllegalStateException("loadContents requires a UTF-8 text file of \
{NF_LOAD_CONTENTS_LIMIT} bytes or less; '" + path.getName() + "' is " + bytes.length + " bytes")
    try {{
        return java.nio.charset.StandardCharsets.UTF_8.newDecoder()
            .decode(java.nio.ByteBuffer.wrap(bytes)).toString()
    }}
    catch( java.nio.charset.CharacterCodingException e ) {{
        throw new IllegalStateException("loadContents requires a UTF-8 text file; '" \
+ path.getName() + "' is not valid UTF-8")
    }}
}}'''


def _require_executable(workflow: Any) -> ExecutableNextflowWorkflow:
    if not isinstance(workflow, ExecutableNextflowWorkflow):
        raise TypeError("Nextflow rendering requires ExecutableNextflowWorkflow")
    return workflow


def _write_text(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


_CONTROL_ESCAPES = {code: f"\\u{code:04x}" for code in (*range(32), 127)}
_GROOVY_LITERAL_TABLE = {ord("\\"): "\\\\", ord("'"): "\\'", **_CONTROL_ESCAPES}
_GSTRING_FRAGMENT_TABLE = {ord("\\"): "\\\\", ord('"'): '\\"', ord("$"): "\\$", **_CONTROL_ESCAPES}


def _groovy_literal(value: str) -> str:
    return f"'{value.translate(_GROOVY_LITERAL_TABLE)}'"


def _groovy_gstring_fragment(value: str) -> str:
    """Escape literal data embedded beside typed GString references."""
    return value.translate(_GSTRING_FRAGMENT_TABLE)


def _segment_expression(segment: Any) -> str:
    match segment:
        case NfInputReference():
            return f"{segment.name}.toString()"
        case NfBasenameReference():
            return f"{segment.name}.name.toString()"
        case _:
            return _groovy_literal(segment.value)


def _template_expression(template: Any) -> str:
    return " + ".join(_segment_expression(segment) for segment in template.segments)


def _shell_quote(value: str) -> str:
    """Return the POSIX shell word produced by the generated Groovy helper."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _render_template(template: Any) -> str:
    """Render one typed token through the generated shell-quoting function."""
    return f"${{{NF_SHELL_QUOTE_HELPER}({_template_expression(template)})}}"


def _render_array_binding(token: NfArrayBinding) -> str:
    """Render an array binding: nothing when empty, else prefix once plus each item."""
    items_expression = f"{token.name}.collect{{ {NF_SHELL_QUOTE_HELPER}(it.toString()) }}"
    if token.prefix is not None:
        quoted_prefix = f"{NF_SHELL_QUOTE_HELPER}({_groovy_literal(token.prefix)})"
        joined = f"([{quoted_prefix}] + {items_expression}).join(' ')"
    else:
        joined = f"{items_expression}.join(' ')"
    return f"${{{token.name}.isEmpty() ? '' : {joined}}}"


def _render_shell_literal(token: NfShellLiteral) -> str:
    """Render one approved shellQuote:false literal exactly as written, unquoted.

    Bypasses the shell-quoting helper entirely: the text is a CWL-author
    literal with no input reference, proven by capability analysis before
    this token can exist, so only the enclosing GString needs escaping.
    """
    return _groovy_gstring_fragment(token.text)


def _render_computed(token: NfComputed) -> str:
    """Render one safe-subset argv word through its fixed Groovy idiom."""
    inputs = "[" + ", ".join(f"{name}: {name}" for name in sorted(token.names)) + "]"
    if inputs == "[]":
        inputs = "[:]"
    where = _groovy_literal(token.where)
    expression = render_groovy(token.expression, where=token.where, inputs=inputs)
    integral = "true" if token.integral else "false"
    text = f"{NF_NUMBER_TEXT_HELPER}({expression}, {integral}, {where}, {inputs})"
    return f"${{{NF_SHELL_QUOTE_HELPER}({text})}}"


def _render_command_token(token: Any) -> str:
    """Render one argv token; flags and array bindings collapse away when falsy/empty."""
    if isinstance(token, NfComputed):
        return _render_computed(token)
    if isinstance(token, NfFlag):
        quoted = f"{NF_SHELL_QUOTE_HELPER}({_groovy_literal(token.prefix)})"
        return f"${{{token.name} ? {quoted} : ''}}"
    if isinstance(token, NfArrayBinding):
        return _render_array_binding(token)
    if isinstance(token, NfShellLiteral):
        return _render_shell_literal(token)
    return _render_template(token)


def _render_glob(template: Any) -> str:
    if all(isinstance(segment, NfLiteral) for segment in template.segments):
        return _groovy_literal("".join(segment.value for segment in template.segments))
    rendered: list[str] = []
    for segment in template.segments:
        match segment:
            case NfInputReference():
                rendered.append(f"${{{segment.name}}}")
            case NfBasenameReference():
                rendered.append(f"${{{segment.name}.name}}")
            case _:
                rendered.append(_groovy_gstring_fragment(segment.value))
    return f'"{"".join(rendered)}"'


def _render_number(value: int | float) -> str:
    """Render a validated JSON number without exponent notation."""
    if isinstance(value, int):
        return str(value)
    return format(Decimal(str(value)), "f")


_GLOB_METACHARACTERS = frozenset("*?[]{}")


def _glob_names_one_file(template: Any) -> bool:
    """True when an assembled output name is meant literally, not as a pattern.

    Nextflow pattern-matches an output ``path`` name, so a name assembled
    from a staged file's own name is matched against whatever metacharacters
    that name happens to carry: a staged ``a[1].txt`` yields the pattern
    ``a[1].txt``, whose ``[1]`` is a character class that cannot match the
    file the process wrote.

    Only a basename reference makes the name literal by construction -- it
    is some real file's name, never a pattern. A plain input reference
    carries arbitrary data, and a glob *is* a legitimate thing to pass as
    an input (``glob: $(inputs.pattern)`` with ``pattern="*.txt")``, so
    turning globbing off there would break a pattern that is meant to
    match. Which reference kind is present is decidable here, at compile
    time, where the assembled value is not; so the rule keys on the kind.

    A metacharacter written into a literal part is the author asking for a
    pattern and is left alone, and an all-literal name is unchanged.
    """
    references = [segment for segment in template.segments if not isinstance(segment, NfLiteral)]
    if not references:
        return False
    if not all(isinstance(segment, NfBasenameReference) for segment in references):
        return False
    return not any(
        isinstance(segment, NfLiteral) and _GLOB_METACHARACTERS & set(segment.value)
        for segment in template.segments
    )


def _capture_literal(port: NfPort) -> str:
    """Return the Groovy literal for a capture-marked port's literal glob."""
    # NfPort admits a capture marker only beside a single-literal glob.
    assert port.glob is not None
    segment = port.glob.segments[0]
    assert isinstance(segment, NfLiteral)
    return _groovy_literal(segment.value)


def _process_output(port: NfPort, *, tuple_element: bool = False) -> str:
    # NfProcess guarantees every output has a typed glob, and the val
    # qualifier only for a text capture.
    assert port.glob is not None
    emit = port.emit or port.name
    if port.capture == "text":
        read = f"{NF_LOAD_CONTENTS_HELPER}(task.workDir.resolve({_capture_literal(port)}))"
        body = f"val({read})"
        return body if tuple_element else f"{body}, emit: {emit}"
    # The two options are independent: one says how the name is matched, the
    # other how many matches the author declared. A text capture returns
    # above, so neither reaches a val declaration.
    literal = (
        ", glob: false"
        if port.capture == "single" or _glob_names_one_file(port.glob)
        else ""
    )
    # A "single" capture marker is the CWL author's own cardinality
    # declaration, so it is stated in the generated pipeline rather than
    # dropped: arity: '1' emits one path value and fails on no match.
    arity = ", arity: '1'" if port.capture == "single" else ""
    if tuple_element:
        # A multi-input-scattered process re-emits the hidden invocation
        # index alongside every output, one tuple line per output port
        # (design §6, Topology), so the "path ..." spelling used inside a
        # standalone output line becomes a parenthesized tuple element here.
        return f"path({_render_glob(port.glob)}{literal}{arity})"
    return f"path {_render_glob(port.glob)}{literal}{arity}, emit: {emit}"


def _process_input(port: NfPort, *, tuple_element: bool = False) -> str:
    if tuple_element:
        if port.stage_as is not None:
            return f"{port.qualifier}({port.name}, stageAs: {_groovy_literal(port.stage_as)})"
        return f"{port.qualifier}({port.name})"
    if port.stage_as is not None:
        return f"{port.qualifier} {port.name}, stageAs: {_groovy_literal(port.stage_as)}"
    return f"{port.qualifier} {port.name}"


def _unscattered_input(port: NfPort, gathered_ports: frozenset[str]) -> str:
    # A gathered File array holds one file per invocation, usually all with the
    # same basename; each is staged in its own numbered directory, as cwltool
    # does, so the names cannot collide. Nextflow numbers the directories per
    # port, so the port name in the pattern keeps two gathered ports apart.
    if port.name in gathered_ports and port.qualifier == "path":
        return f"{port.qualifier} {port.name}, stageAs: {_groovy_literal(f'gather_{port.name}_*/*')}"
    return _process_input(port)


def _render_process(
    process: NfProcess, multi_input_ports: tuple[str, ...] = (), gathered_ports: frozenset[str] = frozenset()
) -> str:
    lines = [f"process {process.name} {{"]
    if process.container is not None:
        lines.append(f"    container {_groovy_literal(process.container)}")
    if process.resources.cpus is not None:
        lines.append(f"    cpus {process.resources.cpus}")
    if process.resources.memory_mb is not None:
        lines.append(f'    memory "{_render_number(process.resources.memory_mb)} MB"')

    if process.inputs:
        lines.extend(["", "    input:"])
        if multi_input_ports:
            scattered = [port for port in process.inputs if port.name in multi_input_ports]
            other = [port for port in process.inputs if port.name not in multi_input_ports]
            tuple_elements = ", ".join(
                [f"val({NF_SCATTER_INDEX_NAME})"]
                + [_process_input(port, tuple_element=True) for port in scattered]
            )
            lines.append(f"    tuple {tuple_elements}")
            lines.extend(f"    {_unscattered_input(port, gathered_ports)}" for port in other)
        else:
            lines.extend(f"    {_unscattered_input(port, gathered_ports)}" for port in process.inputs)
    if process.outputs:
        lines.extend(["", "    output:"])
        if multi_input_ports:
            for port in process.outputs:
                emit = port.emit or port.name
                lines.append(
                    f"    tuple val({NF_SCATTER_INDEX_NAME}), "
                    f"{_process_output(port, tuple_element=True)}, emit: {emit}"
                )
        else:
            lines.extend(f"    {_process_output(port)}" for port in process.outputs)

    command = " ".join(_render_command_token(token) for token in process.command.tokens)
    streams = (
        ("<", process.command.stdin),
        (">", process.command.stdout),
        ("2>", process.command.stderr),
    )
    for operator, stream in streams:
        if stream is not None:
            command += f" {operator} {_render_template(stream)}"
    lines.extend(["", "    script:", '    \"\"\"'])
    lines.append(f"    {command}")
    lines.extend(['    \"\"\"', "}"])
    return "\n".join(lines)


def _conditional_channel_name(process_name: str, port_name: str) -> str:
    return f"ch_{process_name}_{port_name}"


def _rename_refs(node: Expr, mapping: dict[str, str]) -> Expr:
    """Rebuild a typed tree with every ``ref`` renamed through ``mapping``."""
    if node.op == "ref":
        return Expr("ref", (), mapping.get(node.value, node.value))
    if node.args:
        return Expr(node.op, tuple(_rename_refs(arg, mapping) for arg in node.args), node.value)
    return node


def _render_conditional_invocation(process: NfProcess, arguments: list[str]) -> list[str]:
    """Lower a conditional process call: branch on the predicate, mix in the sentinel.

    Combines the process's bound input channels into one tuple channel by
    merging them two at a time (the n-ary ``merge`` with a closure is not
    callable once a later argument is a queue channel, as a process output
    is), branches it on the rendered predicate (wrapped so a non-finite
    subexpression fails via the finite helper), calls the process with the
    run branch's per-input maps, and mixes each output with the skip branch
    mapped to the ``[]`` sentinel — one element per output per invocation.

    Closure parameters use synthetic names rather than the port names
    themselves: Groovy rejects a closure parameter that shadows an
    already-declared script variable, and every port name here is also the
    name of an outer take:/channel variable.
    """
    assert process.condition is not None
    ports = process.inputs
    synthetic = [f"__w{index}" for index in range(len(ports))]
    params = ", ".join(synthetic)
    in_channel = f"ch_{process.name}_in"
    branch_channel = f"ch_{process.name}_branch"
    lines: list[str] = []
    if len(ports) == 1:
        # A single-element channel needs no tuple: Nextflow only auto-spreads
        # a multi-element item across more than one closure parameter.
        in_channel = arguments[0]
    else:
        lines.append(
            f"    {in_channel} = {arguments[0]}.merge({arguments[1]}) "
            f"{{ {synthetic[0]}, {synthetic[1]} -> tuple({synthetic[0]}, {synthetic[1]}) }}"
        )
        for index in range(2, len(ports)):
            carried = ", ".join(f"__merged[{earlier}]" for earlier in range(index))
            lines.append(
                f"        .merge({arguments[index]}) "
                f"{{ __merged, {synthetic[index]} -> tuple({carried}, {synthetic[index]}) }}"
            )
    rename = dict(zip((port.name for port in ports), synthetic, strict=True))
    condition = _rename_refs(process.condition, rename)
    inputs_map = "[" + ", ".join(f"{name}: {name}" for name in sorted(references(condition))) + "]"
    if inputs_map == "[]":
        inputs_map = "[:]"
    where = f"{process.name} when {source_text(process.condition)}"
    predicate = render_groovy(condition, where=where, inputs=inputs_map)
    lines.append(f"    {branch_channel} = {in_channel}.branch {{ {params} ->")
    lines.append(f"        run: {predicate}")
    lines.append("        skip: true")
    lines.append("    }")
    call_args = ", ".join(
        f"{branch_channel}.run.map {{ {params} -> {synthetic[index]} }}"
        for index in range(len(ports))
    )
    lines.append(f"    {process.name}({call_args})")
    for port in process.outputs:
        emit = port.emit or port.name
        channel_name = _conditional_channel_name(process.name, emit)
        lines.append(
            f"    {channel_name} = {process.name}.out.{emit}.mix({branch_channel}.skip.map {{ [] }})"
        )
    return lines


def _render_conditional_scatter(
    process: NfProcess, scatter_channel: str, scattered: list[str], others: list[NfPort], other_args: list[str]
) -> list[str]:
    """Per-combination ``when`` over an indexed scatter (design §6, Topology).

    Each invocation item is ``[index, scattered elements..., broadcasts...]``;
    the run branch calls the process and the skip branch emits
    ``[index, []]`` per output, so a gather keeps CWL ``null`` at each skipped
    position. Broadcasts are boxed before ``combine`` so a list-valued one is
    carried whole rather than spliced into the item.
    """
    assert process.condition is not None
    index = "__i"
    elements = [f"__w{position}" for position in range(len(scattered))]
    broadcasts = [f"__b{position}" for position in range(len(others))]
    params = ", ".join([index, *elements, *broadcasts])
    combined = scatter_channel + "".join(f".combine({arg}.map {{ [it] }})" for arg in other_args)
    rename = dict(zip([*scattered, *(port.name for port in others)], [*elements, *broadcasts], strict=True))
    condition = _rename_refs(process.condition, rename)
    inputs_map = "[" + ", ".join(f"{name}: {name}" for name in sorted(references(condition))) + "]"
    predicate = render_groovy(
        condition,
        where=f"{process.name} when {source_text(process.condition)}",
        inputs=inputs_map if inputs_map != "[]" else "[:]",
    )
    branch = f"ch_{process.name}_branch"
    lines = [
        f"    {branch} = {combined}.branch {{ {params} ->",
        f"        run: {predicate}",
        "        skip: true",
        "    }",
    ]
    call_args = [f"{branch}.run.map {{ {params} -> tuple({', '.join([index, *elements])}) }}"]
    call_args.extend(f"{branch}.run.map {{ {params} -> {name} }}" for name in broadcasts)
    lines.append(f"    {process.name}({', '.join(call_args)})")
    for port in process.outputs:
        emit = port.emit or port.name
        lines.append(
            f"    {_conditional_channel_name(process.name, emit)} = {process.name}.out.{emit}"
            f".mix({branch}.skip.map {{ {params} -> tuple({index}, []) }})"
        )
    return lines


def _process_map(workflow: ExecutableNextflowWorkflow) -> dict[str, NfProcess]:
    return {process.name: process for process in workflow.processes}


def _incoming_connections(workflow: ExecutableNextflowWorkflow) -> dict[tuple[str, str], NfConnection]:
    # The executable IR rejects multiple sources per input at construction.
    return {
        (connection.to_process, connection.to_port): connection
        for connection in workflow.connections
        if isinstance(connection, (NfWorkflowInputConnection, NfProcessConnection))
    }


_ADAPTER_OPERATORS = {"scatter": ".flatten()"}


def _shape_channel(process_name: str) -> str:
    return f"ch_{process_name}_shape"


NF_NEST_FUNCTION = f'''def {NF_NEST_HELPER}(List flat, List shape) {{
    if( shape.size() == 1 )
        return flat
    def inner = shape.drop(1).inject(1) {{ product, size -> product * size }}
    return (0..<shape[0]).collect {{ position ->
        {NF_NEST_HELPER}(flat.subList(position * inner, (position + 1) * inner), shape.drop(1))
    }}
}}'''


def _gathered(expression: str) -> str:
    """Collect an indexed scatter's outputs once, as one array in invocation order."""
    return f"{expression}.toSortedList {{ it[0] }}.map {{ it.collect {{ row -> row[1] }} }}"


def _multi_input_sources(
    workflow: ExecutableNextflowWorkflow, process: NfProcess
) -> list[tuple[str, str, str]]:
    """Return (port, whole-array channel, label) for multi-input-scattered ports, in port order.

    A source is a workflow input, or the gathered output of an upstream
    multi-input scatter (design §6, Topology, Gather).
    """
    processes = _process_map(workflow)
    by_port: dict[str, tuple[str, str]] = {}
    for connection in workflow.connections:
        if (
            not isinstance(connection, (NfWorkflowInputConnection, NfProcessConnection))
            or connection.to_process != process.name
            or connection.adapter not in MULTI_INPUT_ADAPTERS
        ):
            continue
        match connection:
            case NfWorkflowInputConnection(from_port, _, to_port, _):
                by_port[to_port] = (from_port, from_port)
            case NfProcessConnection(from_process, from_port, _, to_port, _):
                upstream = _source_expression(replace(connection, adapter=None), processes)
                by_port[to_port] = (_gathered(upstream), f"{from_process}.{from_port}")
    return [(port.name, *by_port[port.name]) for port in process.inputs if port.name in by_port]


def _nested_process_names(workflow: ExecutableNextflowWorkflow) -> set[str]:
    """Names of the processes whose scattered inputs, workflow inputs or gathered arrays, nest."""
    return {
        connection.to_process
        for connection in workflow.connections
        if isinstance(connection, (NfWorkflowInputConnection, NfProcessConnection))
        and connection.adapter == "nested_crossproduct"
    }


def _multi_input_method(workflow: ExecutableNextflowWorkflow, process: NfProcess) -> str:
    """The one multi-input scatter method a process's adapted inputs share."""
    return next(
        connection.adapter
        for connection in workflow.connections
        if isinstance(connection, (NfWorkflowInputConnection, NfProcessConnection))
        and connection.to_process == process.name
        and connection.adapter in MULTI_INPUT_ADAPTERS
    )


def _render_multi_input_channel(
    channel_name: str, process_name: str, sources: list[tuple[str, str, str]], method: str = "dotproduct"
) -> str:
    """Combine multi-input scatter source value channels into one [index, elem...] queue channel.

    Combines the whole-array value channels the design requires (design §6,
    Topology), then computes every invocation from the whole arrays directly
    by one fixed Groovy idiom -- never by pairing per-element queue channels,
    whose pairing would depend on arrival order. ``dotproduct`` pairs the
    arrays by index: unequal lengths fail the run naming each scattered input
    and its length, and equal empty arrays yield zero invocations.
    ``flat_crossproduct`` runs every combination with the first declared input
    outermost and checks no lengths; any empty array yields zero invocations.
    ``nested_crossproduct`` runs the same invocations and also emits each
    input's length on a shape channel, so the workflow output can regroup the
    results into one dimension per input.
    """
    locals_ = [f"__d{index}" for index in range(len(sources))]
    # Nextflow's combine flattens a List-valued item into the concatenated
    # tuple, and a whole scattered array is exactly such a value; boxing each
    # one in a singleton list first keeps combine from splicing its elements
    # into the tuple instead of carrying the array itself.
    boxed = f"{sources[0][1]}.map {{ [it] }}"
    for _, channel, _label in sources[1:]:
        boxed = f"{boxed}.combine({channel}.map {{ [it] }})"
    # A one-parameter closure receives a single boxed item unspread, so a lone
    # array is passed as itself.
    combine_expr = boxed if len(sources) > 1 else sources[0][1]
    params_decl = ", ".join(locals_)
    if method in {"flat_crossproduct", "nested_crossproduct"}:
        # Nested loops with the first declared input outermost: the CWL
        # reference order. Groovy's combinations() varies the first list
        # fastest, so it cannot be used.
        # Strict Nextflow syntax has no for loops, so the nesting is collectMany.
        loop_vars = [f"__e{index}" for index in range(len(locals_))]
        product = f"[[{', '.join(loop_vars)}]]"
        for var, local in reversed(list(zip(loop_vars, locals_))):
            product = f"{local}.collectMany {{ {var} -> {product} }}"
        lines = [
            f"    {channel_name} = {combine_expr}.flatMap {{ {params_decl} ->",
            f"        {product}.withIndex().collect {{ combination, index -> [index] + combination }}",
            "    }",
        ]
        if method == "nested_crossproduct":
            # The same invocations as flat, plus each input's length, so the
            # gathered results can be regrouped into one dimension per input
            # even where a dimension is empty.
            sizes = ", ".join(f"{local}.size()" for local in locals_)
            lines.append(f"    {_shape_channel(process_name)} = {combine_expr}.map {{ {params_decl} -> [[{sizes}]] }}")
        return "\n".join(lines)
    mismatch = " || ".join(f"{locals_[0]}.size() != {name}.size()" for name in locals_[1:])
    detail = ", ".join(
        f"{label}=${{{local}.size()}}" for local, (_, _channel, label) in zip(locals_, sources)
    )
    message = f"{process_name}: dotproduct scatter inputs have mismatched lengths: {detail}"
    tuple_elements = ", ".join(f"{local}[i]" for local in locals_)
    return "\n".join([
        f"    {channel_name} = {combine_expr}.flatMap {{ {params_decl} ->",
        *([f'        if ({mismatch}) {{ throw new RuntimeException("{message}") }}'] if mismatch else []),
        f"        (0..<{locals_[0]}.size()).collect {{ i -> tuple(i, {tuple_elements}) }}",
        "    }",
    ])


def _source_expression(connection: NfConnection, processes: Mapping[str, NfProcess]) -> str:
    match connection:
        case NfWorkflowInputConnection(from_port, _, _, adapter):
            # The adapter is applied per consumption site, so each scattered
            # sink derives its own queue channel from the shared parameter.
            return from_port + (_ADAPTER_OPERATORS[adapter] if adapter else "")
        case NfProcessConnection(from_process, from_port, _, _, "gather"):
            return _gathered(_source_expression(replace(connection, adapter=None), processes))
        case NfProcessConnection(from_process, from_port, _, _, _) | NfWorkflowOutputConnection(
            from_process, from_port, _
        ):
            process = processes[from_process]
            output = next(port for port in process.outputs if port.name == from_port)
            emit = output.emit or output.name
            if process.condition is not None:
                return _conditional_channel_name(process.name, emit)
            return f"{process.name}.out.{emit}"


def _ordered_processes(workflow: ExecutableNextflowWorkflow) -> list[NfProcess]:
    """Return processes in stable topological order and reject cycles."""
    position = {process.name: index for index, process in enumerate(workflow.processes)}
    names = topological_order(
        process_dependencies(position, workflow.connections),
        error="Nextflow workflow connections contain a cycle",
        key=position.__getitem__,
    )
    return [workflow.processes[position[name]] for name in names]


def _workflow_input_names(workflow: ExecutableNextflowWorkflow) -> list[str]:
    return list(dict.fromkeys(
        connection.from_port
        for connection in workflow.connections
        if isinstance(connection, NfWorkflowInputConnection)
    ))


def _render_named_workflow(workflow: ExecutableNextflowWorkflow) -> str:
    processes = _process_map(workflow)
    incoming = _incoming_connections(workflow)
    workflow_inputs = _workflow_input_names(workflow)
    lines = [f"workflow {workflow.name} {{"]
    if workflow_inputs:
        lines.append("    take:")
        lines.extend(f"    {name}" for name in workflow_inputs)

    multi_input_process_names = {
        connection.to_process
        for connection in workflow.connections
        if isinstance(connection, (NfWorkflowInputConnection, NfProcessConnection))
        and connection.adapter in MULTI_INPUT_ADAPTERS
    }

    nested_process_names = _nested_process_names(workflow)

    lines.append("    main:")
    for process in _ordered_processes(workflow):
        multi_input_sources = _multi_input_sources(workflow, process)
        if multi_input_sources:
            multi_input_names = {name for name, _, _ in multi_input_sources}
            channel_name = f"ch_{process.name}_scatter"
            lines.append(_render_multi_input_channel(
                channel_name, process.name, multi_input_sources, _multi_input_method(workflow, process)
            ))
            other_ports = [port for port in process.inputs if port.name not in multi_input_names]
            arguments = [channel_name] + [
                _source_expression(incoming[(process.name, port.name)], processes)
                for port in other_ports
            ]
        else:
            # The executable IR guarantees every process input is connected.
            arguments = [
                _source_expression(incoming[(process.name, port.name)], processes)
                for port in process.inputs
            ]
        if process.condition is None:
            lines.append(f"    {process.name}({', '.join(arguments)})")
        elif multi_input_sources:
            lines.extend(_render_conditional_scatter(
                process, channel_name, [name for name, _, _ in multi_input_sources], other_ports, arguments[1:]
            ))
        else:
            lines.extend(_render_conditional_invocation(process, arguments))

    workflow_outputs = [
        connection
        for connection in workflow.connections
        if isinstance(connection, NfWorkflowOutputConnection)
    ]
    if workflow_outputs:
        lines.append("    emit:")
        for connection in workflow_outputs:
            expression = _source_expression(connection, processes)
            if connection.from_process in nested_process_names:
                # Regroup the index-sorted results by the recorded input
                # lengths: one array dimension per scattered input.
                expression = (
                    f"{expression}.toSortedList {{ it[0] }}.map {{ [it.collect {{ row -> row[1] }}] }}"
                    f".combine({_shape_channel(connection.from_process)})"
                    f".flatMap {{ flat, shape -> {NF_NEST_HELPER}(flat, shape) }}"
                )
            elif connection.from_process in multi_input_process_names:
                # Sort by the hidden invocation index before stripping it, so
                # the gathered workflow output never depends on task
                # completion order (design §6, Topology, Gather).
                expression = (
                    f"{expression}.toSortedList {{ it[0] }}"
                    ".flatMap { it.collect { row -> row[1] } }"
                )
            lines.append(f"    {connection.to_port} = {expression}")
    lines.append("}")
    return "\n".join(lines)


def _workflow_input_sink(
    workflow: ExecutableNextflowWorkflow,
    name: str,
) -> tuple[NfWorkflowInputConnection, NfPort]:
    processes = _process_map(workflow)
    for connection in workflow.connections:
        if isinstance(connection, NfWorkflowInputConnection) and connection.from_port == name:
            process = processes[connection.to_process]
            port = next(port for port in process.inputs if port.name == connection.to_port)
            return connection, port
    raise ValueError(f"workflow input {name!r} is not connected")


def _parameter_expression(workflow: ExecutableNextflowWorkflow, name: str) -> str:
    connection, port = _workflow_input_sink(workflow, name)
    scattered_processes = {
        candidate.to_process
        for candidate in workflow.connections
        if isinstance(candidate, NfWorkflowInputConnection)
        and candidate.adapter in ("scatter", *MULTI_INPUT_ADAPTERS)
    }
    feeds_scattered_process = any(
        isinstance(candidate, NfWorkflowInputConnection)
        and candidate.from_port == name
        and candidate.to_process in scattered_processes
        for candidate in workflow.connections
    )
    # A scatter- or multi-input-adapted parameter carries the whole source
    # array; the graph validator keeps every sink of one parameter in
    # agreement, so one sink decides the construction for all of them.
    carries_list = port.is_array or connection.adapter in ("scatter", *MULTI_INPUT_ADAPTERS)
    if port.qualifier == "path":
        path_type = "dir" if port.path_kind == "directory" else "file"
        if carries_list:
            # One Channel.value(...) element holding a Groovy list, so
            # Nextflow stages every element for a single process call
            # instead of fanning the channel out over several calls.
            return (
                f"Channel.value(params.{name}.collect {{ entry -> file("
                f"entry instanceof Map ? entry.path : entry, "
                f"checkIfExists: true, type: '{path_type}') }})"
            )
        if feeds_scattered_process:
            # A one-element queue pairs with only the first scatter task. A
            # value channel broadcasts the same staged path to every task.
            return (
                f"Channel.value(file(params.{name} instanceof Map ? params.{name}.path : "
                f"params.{name}, checkIfExists: true, type: '{path_type}'))"
            )
        return (
            f"Channel.fromPath(params.{name} instanceof Map ? params.{name}.path : "
            f"params.{name}, checkIfExists: true, type: '{path_type}', glob: false)"
        )
    return f"Channel.value(params.{name})"


def render_nextflow(workflow: ExecutableNextflowWorkflow) -> str:
    """Render a supported workflow as deterministic Nextflow DSL2 source.

    Args:
        workflow (ExecutableNextflowWorkflow): Validated executable IR; no
            other representation is accepted.

    Raises:
        TypeError: If the value is not an ``ExecutableNextflowWorkflow``.

    Returns:
        str: The complete ``workflow.nf`` text; byte-stable across calls.
    """
    _require_executable(workflow)
    sections = [
        "nextflow.enable.dsl=2",
        NF_SHELL_QUOTE_FUNCTION,
    ]
    # Emitted only where the capture is used, so artifacts for models that
    # predate it stay byte-identical and existing artifact pairs keep
    # promoting.
    if any(
        port.capture == "text"
        for process in workflow.processes
        for port in process.outputs
    ):
        sections.append(NF_LOAD_CONTENTS_FUNCTION)
    if any(
        isinstance(token, NfComputed)
        for process in workflow.processes
        for token in process.command.tokens
    ) or any(process.condition is not None for process in workflow.processes):
        sections.append(NF_EXPRESSION_FUNCTIONS)
    if _nested_process_names(workflow):
        sections.append(NF_NEST_FUNCTION)
    sections.extend(
        _render_process(
            process,
            tuple(name for name, _, _ in _multi_input_sources(workflow, process)),
            frozenset(
                connection.to_port
                for connection in workflow.connections
                if isinstance(connection, NfProcessConnection)
                and connection.to_process == process.name
                and connection.adapter == "gather"
            ),
        )
        for process in workflow.processes
    )
    sections.append(_render_named_workflow(workflow))
    arguments = ",\n        ".join(
        _parameter_expression(workflow, name) for name in _workflow_input_names(workflow)
    )
    invocation = f"    {workflow.name}({arguments})" if arguments else f"    {workflow.name}()"
    sections.append(f"workflow {{\n{invocation}\n}}")
    return "\n\n".join(sections) + "\n"


def render_nextflow_config(workflow: ExecutableNextflowWorkflow) -> str:
    """Render the deterministic executor configuration.

    Args:
        workflow (ExecutableNextflowWorkflow): Validated executable IR.

    Raises:
        TypeError: If the value is not an ``ExecutableNextflowWorkflow``.

    Returns:
        str: The ``nextflow.config`` text carrying the validated
            workflow-wide container policy.
    """
    _require_executable(workflow)
    return f"docker.enabled = {str(workflow.containers_enabled).lower()}\n"


def render_nextflow_params(workflow: ExecutableNextflowWorkflow) -> str:
    """Render workflow parameters as deterministic JSON.

    Args:
        workflow (ExecutableNextflowWorkflow): Validated executable IR.

    Raises:
        TypeError: If the value is not an ``ExecutableNextflowWorkflow``.

    Returns:
        str: The ``nextflow_params.json`` text with sorted keys.
    """
    _require_executable(workflow)
    return json.dumps(
        workflow.to_dict()["params"],
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


def write_nextflow_artifacts(
    workflow: ExecutableNextflowWorkflow,
    outdir: str | Path,
) -> tuple[Path, Path, Path, Path]:
    """Validate every representation before writing the four artifacts.

    Args:
        workflow (ExecutableNextflowWorkflow): Validated executable IR.
        outdir (str | Path): Output directory; created when missing.

    Raises:
        TypeError: If the value is not an ``ExecutableNextflowWorkflow``.

    Returns:
        tuple[Path, Path, Path, Path]: Paths to the versioned JSON IR,
            ``workflow.nf``, ``nextflow.config``, and
            ``nextflow_params.json``, in that order.
    """
    _require_executable(workflow)
    serialized = f"{workflow.to_json()}\n"
    script = render_nextflow(workflow)
    config = render_nextflow_config(workflow)
    params = render_nextflow_params(workflow)
    output = Path(outdir)
    return (
        _write_text(output / NEXTFLOW_JSON, serialized),
        _write_text(output / NEXTFLOW_SCRIPT, script),
        _write_text(output / NEXTFLOW_CONFIG, config),
        _write_text(output / NEXTFLOW_PARAMS, params),
    )
