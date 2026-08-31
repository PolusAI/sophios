"""Property-based adversarial tests for the executable Nextflow boundary."""

from decimal import Decimal
import json
import shlex
from typing import Any, Literal, cast

from hypothesis import given, settings, strategies as st
import pytest

from sophios.input_output_nf import (
    _render_glob,
    _render_template,
    _shell_quote,
    render_nextflow,
)
from sophios.nf_symbols import is_nextflow_identifier, normalize_nextflow_identifier
from sophios.nf_reader import parse_nf_text, promote_nextflow_document
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfCommand,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfProcessConnection,
    NfResources,
    NfTemplate,
    NfWorkflowInputConnection,
)
from sophios.utils_nf import _command, _normalized_identifiers


SURROGATE_CATEGORIES: tuple[Literal["Cs"], ...] = ("Cs",)
SAFE_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=SURROGATE_CATEGORIES,
        blacklist_characters="\x00\r\n",
    ),
    min_size=1,
    max_size=80,
)
JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | SAFE_TEXT,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(SAFE_TEXT, children, max_size=4),
    max_leaves=12,
)


@settings(max_examples=300, deadline=None)
@given(
    st.text(
        alphabet=st.characters(blacklist_categories=SURROGATE_CATEGORIES),
        min_size=1,
        max_size=80,
    )
)
def test_identifier_normalization_is_valid_and_idempotent(value: str) -> None:
    normalized = normalize_nextflow_identifier(value)
    assert is_nextflow_identifier(normalized)
    assert normalize_nextflow_identifier(normalized) == normalized


@settings(max_examples=120, deadline=None)
@given(
    st.from_regex(r"[a-z][a-z0-9]{0,12}", fullmatch=True),
    st.from_regex(r"[a-z][a-z0-9]{0,12}", fullmatch=True),
)
def test_identifier_normalization_collisions_are_never_last_wins(
    prefix: str,
    suffix: str,
) -> None:
    with pytest.raises(ValueError, match="normalize to"):
        _normalized_identifiers(
            (f"{prefix}-{suffix}", f"{prefix}_{suffix}"),
            context="workflow input",
        )


@settings(max_examples=200, deadline=None)
@given(SAFE_TEXT | st.just(""))
def test_literal_template_is_exactly_one_shell_argument(value: str) -> None:
    assert shlex.split(_shell_quote(value)) == [value]
    assert "__sophios_shell_quote_9f72e" in _render_template(
        NfTemplate((NfLiteral(value),))
    )


@settings(max_examples=200, deadline=None)
@given(SAFE_TEXT, SAFE_TEXT)
def test_interpolated_template_remains_one_argument(prefix: str, suffix: str) -> None:
    value = "adversarial ' value; $(touch SHOULD_NOT_EXIST)"
    template = NfTemplate(
        (NfLiteral(prefix), NfInputReference("value"), NfLiteral(suffix))
    )
    rendered = _render_template(template)
    assert "value.toString()" in rendered
    assert shlex.split(_shell_quote(f"{prefix}{value}{suffix}")) == [
        f"{prefix}{value}{suffix}"
    ]


@settings(max_examples=150, deadline=None)
@given(SAFE_TEXT | st.just(""))
def test_literal_globs_never_become_gstrings(value: str) -> None:
    rendered = _render_glob(NfTemplate((NfLiteral(value),)))
    assert rendered.startswith("'") and rendered.endswith("'")


@settings(max_examples=150, deadline=None)
@given(SAFE_TEXT | st.just(""))
def test_interpolated_glob_literals_use_only_valid_gstring_escapes(value: str) -> None:
    rendered = _render_glob(NfTemplate((
        NfLiteral(f"`{value}"),
        NfInputReference("name"),
    )))
    assert "\\`" not in rendered
    assert "${name}" in rendered


@settings(max_examples=150, deadline=None)
@given(st.dictionaries(SAFE_TEXT, JSON_VALUES, max_size=8))
def test_executable_json_roundtrip_is_canonical(params: dict[str, object]) -> None:
    workflow = ExecutableNextflowWorkflow("wf", (), (), params)
    hydrated = ExecutableNextflowWorkflow.from_json(workflow.to_json())
    assert hydrated == workflow
    assert hydrated.to_json() == workflow.to_json()
    assert json.loads(workflow.to_json())["representation_kind"] == "executable"


@settings(max_examples=120, deadline=None)
@given(SAFE_TEXT)
def test_path_parameter_script_does_not_depend_on_compiled_value_shape(path: str) -> None:
    process = NfProcess(
        "READ",
        (NfPort("source", "path"),),
        (),
        NfCommand((NfTemplate((NfLiteral("true"),)),)),
    )
    connections = (NfWorkflowInputConnection("source", "READ", "source"),)
    from_string = ExecutableNextflowWorkflow(
        "wf",
        (process,),
        connections,
        {"source": path},
    )
    from_mapping = ExecutableNextflowWorkflow(
        "wf",
        (process,),
        connections,
        {"source": {"class": "File", "path": path}},
    )
    assert render_nextflow(from_string) == render_nextflow(from_mapping)
    assert "type: 'file', glob: false" in render_nextflow(from_string)


@settings(max_examples=100, deadline=None)
@given(st.sampled_from(("file", "directory")))
def test_path_kind_is_serialized_and_controls_exact_channel_staging(
    path_kind: str,
) -> None:
    process = NfProcess(
        "READ",
        (NfPort("source", "path", path_kind=path_kind),),
        (),
        NfCommand((NfTemplate((NfLiteral("true"),)),)),
    )
    workflow = ExecutableNextflowWorkflow(
        "wf",
        (process,),
        (NfWorkflowInputConnection("source", "READ", "source"),),
        {"source": "input[1]"},
    )
    expected_type = "dir" if path_kind == "directory" else "file"

    assert workflow.to_dict()["processes"][0]["inputs"][0]["path_kind"] == path_kind
    assert f"type: '{expected_type}', glob: false" in render_nextflow(workflow)


@settings(max_examples=100, deadline=None)
@given(st.integers(min_value=1, max_value=1_000_000).map(lambda value: value + 0.5))
def test_fractional_cpu_requests_are_unrepresentable(cpus: float) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        NfResources(cpus=cast(Any, cpus))


@settings(max_examples=150, deadline=None)
@given(
    st.one_of(
        st.integers(min_value=1, max_value=10**30),
        st.floats(
            min_value=0.000001,
            max_value=10**15,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
)
def test_memory_rendering_is_non_exponential_and_value_preserving(
    memory_mb: int | float,
) -> None:
    process = NfProcess(
        "TASK",
        (),
        (),
        NfCommand((NfTemplate((NfLiteral("true"),)),)),
        resources=NfResources(memory_mb=memory_mb),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow("wf", (process,), (), {}))
    line = next(line.strip() for line in rendered.splitlines() if "memory " in line)
    rendered_number = line.removeprefix('memory "').removesuffix(' MB"')

    assert "e" not in rendered_number.lower()
    assert Decimal(rendered_number) == Decimal(str(memory_mb))


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.booleans(), min_size=2, max_size=10).filter(
        lambda modes: any(modes) and not all(modes)
    )
)
def test_mixed_container_policy_is_unrepresentable(modes: list[bool]) -> None:
    processes = tuple(
        NfProcess(
            f"P{index}",
            (),
            (),
            NfCommand((NfTemplate((NfLiteral("true"),)),)),
            container="ubuntu:24.04" if containerized else None,
        )
        for index, containerized in enumerate(modes)
    )
    with pytest.raises(ValueError, match="mixed container execution"):
        ExecutableNextflowWorkflow("wf", processes, (), {})


@settings(max_examples=80, deadline=None)
@given(st.permutations(("zeta", "alpha", "middle")))
def test_equal_position_input_bindings_order_by_field_name(order: tuple[str, ...]) -> None:
    inputs = {
        name: {"type": "string", "inputBinding": {}}
        for name in order
    }
    command = _command({"baseCommand": "cmd", "arguments": ["a", "b", "c"], "inputs": inputs})
    references = [
        segment.name
        for token in command.tokens
        for segment in token.segments
        if isinstance(segment, NfInputReference)
    ]
    literals: list[str] = []
    for token in command.tokens[:4]:
        segment = token.segments[0]
        assert isinstance(segment, NfLiteral)
        literals.append(segment.value)
    assert literals == ["cmd", "a", "b", "c"]
    assert references == ["alpha", "middle", "zeta"]


def test_value_from_literal_and_reference_is_one_typed_token() -> None:
    command = _command({
        "baseCommand": "cp",
        "arguments": [{"valueFrom": "my report $(inputs.name).txt"}],
        "inputs": {"name": {"type": "string"}},
    })
    rendered = _render_template(command.tokens[-1])
    assert "'my report ' + name.toString() + '.txt'" in rendered


def test_empty_arguments_are_preserved_and_boolean_positions_are_rejected() -> None:
    command = _command({"baseCommand": "printf", "arguments": [""]})
    assert command.tokens[-1] == NfTemplate((NfLiteral(""),))
    with pytest.raises(ValueError, match="non-integer CWL command position"):
        _command({"baseCommand": "echo", "arguments": [{"valueFrom": "x", "position": True}]})


@settings(max_examples=60, deadline=None)
@given(st.integers(min_value=2, max_value=12))
def test_linear_graph_accepts_exactly_until_a_cycle_is_added(size: int) -> None:
    processes = []
    connections = []
    for index in range(size):
        inputs = () if index == 0 else (NfPort("source", "path"),)
        output = NfPort(
            "result", "path", "result", NfTemplate((NfLiteral(f"{index}.txt"),))
        )
        processes.append(NfProcess(f"P{index}", inputs, (output,), NfCommand((NfTemplate((NfLiteral("true"),)),))))
        if index:
            connections.append(NfProcessConnection(f"P{index - 1}", "result", f"P{index}", "source"))
    ExecutableNextflowWorkflow("wf", processes, connections, {})
    first = processes[0]
    processes[0] = NfProcess(first.name, (NfPort("source", "path"),), first.outputs, first.command)
    with pytest.raises(ValueError, match="cycle"):
        ExecutableNextflowWorkflow(
            "wf",
            processes,
            [*connections, NfProcessConnection(f"P{size - 1}", "result", "P0", "source")],
            {},
        )


@settings(max_examples=150, deadline=None)
@given(
    st.text(
        alphabet=st.characters(
            blacklist_categories=SURROGATE_CATEGORIES,
            blacklist_characters="\x00\r\n{}",
        ),
        min_size=1,
        max_size=80,
    )
)
def test_unknown_process_syntax_is_never_silently_promoted(value: str) -> None:
    source = f'''nextflow.enable.dsl=2
process TASK {{
    unsupportedDirective {json.dumps(value)}
    script:
    """
    true
    """
}}
workflow PIPELINE {{
    main:
    TASK()
}}
workflow {{ PIPELINE() }}
'''
    parsed = parse_nf_text(source)
    assert any("unsupportedDirective" in region for region in parsed.opaque_regions)
    with pytest.raises(ValueError, match="opaque regions"):
        promote_nextflow_document(parsed)
