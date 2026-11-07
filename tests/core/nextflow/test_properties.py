"""Property-based adversarial tests for the executable Nextflow boundary."""

# pylint: disable=missing-function-docstring

from decimal import Decimal
import shlex
from typing import Any, Literal, cast

from cwl_utils.errors import JavascriptException, SubstitutionError
from cwl_utils.expression import interpolate
from hypothesis import example, given, strategies as st
import pytest

from sophios.input_output_nf import (
    _render_glob,
    _render_template,
    _shell_quote,
    render_nextflow,
    render_nextflow_config,
)
from sophios.lang.subset import Expr
from sophios.nf_expr import groovy_literal
from sophios.nf_symbols import is_nextflow_identifier, normalize_nextflow_identifier
from sophios.nf_types import (
    ExecutableNextflowWorkflow,
    NfCommand,
    NfEntry,
    NfInputReference,
    NfLiteral,
    NfPort,
    NfProcess,
    NfResources,
    NfTemplate,
    NfWorkflowInputConnection,
)
from sophios.utils_nf import _command as lower_command
from sophios.utils_nf import _template as lower_template
from sophios.utils_nf import _normalized_identifiers

from ..budgets import budget


SURROGATE_CATEGORIES: tuple[Literal["Cs"], ...] = ("Cs",)
INTERPOLATION_PIECES = ["a", " ", "\\", "$", "(", "{", "}", "$(inputs.x)", "\\$(", "\\${"]
SAFE_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=SURROGATE_CATEGORIES,
        blacklist_characters="\x00\r\n",
    ),
    min_size=1,
    max_size=80,
)


@budget(300)
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


@budget(120)
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


@budget(200)
@given(SAFE_TEXT | st.just(""))
def test_literal_template_is_exactly_one_shell_argument(value: str) -> None:
    assert shlex.split(_shell_quote(value)) == [value]
    assert "__sophios_shell_quote_9f72e" in _render_template(
        NfTemplate((NfLiteral(value),)), {}
    )


@budget(200)
@given(SAFE_TEXT, SAFE_TEXT)
def test_interpolated_template_remains_one_argument(prefix: str, suffix: str) -> None:
    value = "adversarial ' value; $(touch SHOULD_NOT_EXIST)"
    template = NfTemplate(
        (NfLiteral(prefix), NfInputReference("value"), NfLiteral(suffix))
    )
    rendered = _render_template(template, {})
    assert "value.toString()" in rendered
    assert shlex.split(_shell_quote(f"{prefix}{value}{suffix}")) == [
        f"{prefix}{value}{suffix}"
    ]


@budget(150)
@given(SAFE_TEXT | st.just(""))
def test_literal_globs_never_become_gstrings(value: str) -> None:
    rendered = _render_glob(NfTemplate((NfLiteral(value),)), {})
    assert rendered.startswith("'") and rendered.endswith("'")


@budget(150)
@given(SAFE_TEXT | st.just(""))
def test_interpolated_glob_literals_use_only_valid_gstring_escapes(value: str) -> None:
    rendered = _render_glob(NfTemplate((
        NfLiteral(f"`{value}"),
        NfInputReference("name"),
    )), {})
    # Groovy has no \\` escape, so no backtick may be escaped. A bare
    # substring check is not that property: the escaper doubles every
    # backslash, so a literal backslash before a backtick also matches it
    # while being correctly escaped. Count the run instead -- an even run
    # leaves the backtick unescaped.
    body = rendered[1:-1]
    for index, char in enumerate(body):
        if char == "`":
            prefix = body[:index]
            assert (len(prefix) - len(prefix.rstrip("\\"))) % 2 == 0
    assert "${name}" in rendered


@budget(150)
@given(st.text(alphabet=st.characters(blacklist_categories=SURROGATE_CATEGORIES)).filter(str.strip))
def test_every_single_quoted_groovy_position_uses_the_one_escaper(value: str) -> None:
    # An entry name is one path component, and never `.`, `..` or the reserved staging prefix.
    staged = "_" + value.translate(str.maketrans("/\x00", "__"))
    spoken = "".join(char for char in value if " " <= char <= "~" and char != "\\")  # a when literal's alphabet
    process = NfProcess(
        "TASK",
        [NfPort("s", "val"), NfPort("src", "path", entry=NfEntry(staged))],
        [],
        NfCommand((NfTemplate((NfLiteral("true"),)),)),
        container=value,
        condition=Expr("==", (Expr("ref", (), "s"), Expr("string", (), spoken))),
    )
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "PIPELINE",
        [process],
        [NfWorkflowInputConnection(name, "TASK", name) for name in ("s", "src")],
        {"s": "x", "src": "in.txt"}, {"s": "string", "src": "File"},
    ))
    for literal in (groovy_literal(value), groovy_literal(staged), groovy_literal(spoken)):
        assert literal in rendered
    assert not any(ord(char) < 32 and char != "\n" or ord(char) == 127 for char in rendered)


@budget(300)
@given(st.lists(st.sampled_from(INTERPOLATION_PIECES), max_size=8).map("".join))
# Once refused: the text on the two sides of a token joined into `${`, `$(`.
@example("{$$(inputs.x){$")
@example("($$(inputs.x)($")
@example("$\\$((")
def test_template_interpolation_agrees_with_cwltool(text: str) -> None:
    try:
        # cwltool interpolates a string only when it contains an expression.
        expected = interpolate(text, {"inputs": {"x": "VALUE"}}) if "$(" in text or "${" in text else text
    except (JavascriptException, SubstitutionError):
        with pytest.raises(ValueError, match="unsupported CWL expression"):
            lower_template(text, context="property")
        return
    template = lower_template(text, context="property")
    lowered = "".join(segment.value if isinstance(segment, NfLiteral) else "VALUE" for segment in template.segments)
    assert lowered == expected


@budget(120)
@given(SAFE_TEXT, st.sampled_from(("file", "directory")))
@example("input.txt", "file")
@example("input[1]", "directory")
def test_a_path_parameter_is_one_channel_of_its_kind_whatever_its_value_shape(path: str, path_kind: str) -> None:
    """A path parameter is a bare path or the object the compiler writes; either is one channel of the declared
    kind, staged without globbing, and the port keeps its kind in the model."""
    declared = "Directory" if path_kind == "directory" else "File"
    process = NfProcess(
        "READ",
        (NfPort("source", "path", path_kind=path_kind),),
        (),
        NfCommand((NfTemplate((NfLiteral("true"),)),)),
    )
    connections = (NfWorkflowInputConnection("source", "READ", "source"),)
    from_string = ExecutableNextflowWorkflow("wf", (process,), connections, {"source": path}, {"source": declared})
    from_mapping = ExecutableNextflowWorkflow(
        "wf", (process,), connections, {"source": {"class": declared, "path": path}}, {"source": declared},
    )
    assert render_nextflow(from_string) == render_nextflow(from_mapping)
    assert (f"Channel.value(file(__sophios_input_9f72e('source', params.source, '{declared}'), glob: false))"
            in render_nextflow(from_string))
    assert from_string.to_dict()["processes"][0]["inputs"][0]["path_kind"] == path_kind


@budget(100)
@given(st.integers(min_value=1, max_value=1_000_000).map(lambda value: value + 0.5))
@example(2.5)
def test_fractional_cpu_requests_are_unrepresentable(cpus: float) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        NfResources(cpus=cast(Any, cpus))


@budget(150)
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
@example(1234567)
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


@budget(60)
@given(st.lists(st.booleans(), max_size=10))
@example([False])
@example([True])
def test_docker_is_enabled_exactly_when_a_process_has_a_container(modes: list[bool]) -> None:
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
    workflow = ExecutableNextflowWorkflow("wf", processes, (), {})
    assert render_nextflow_config(workflow) == f"docker.enabled = {str(any(modes)).lower()}\n"


@budget(80)
@given(st.permutations(("zeta", "alpha", "middle")))
def test_equal_position_input_bindings_order_by_field_name(order: tuple[str, ...]) -> None:
    inputs = {
        name: {"type": "string", "inputBinding": {}}
        for name in order
    }
    command = lower_command({"baseCommand": "cmd", "arguments": ["a", "b", "c"], "inputs": inputs})
    templates = [token for token in command.tokens if isinstance(token, NfTemplate)]
    assert len(templates) == len(command.tokens), "string bindings never lower to flag tokens"
    references = [
        segment.name
        for token in templates
        for segment in token.segments
        if isinstance(segment, NfInputReference)
    ]
    literals: list[str] = []
    for token in templates[:4]:
        segment = token.segments[0]
        assert isinstance(segment, NfLiteral)
        literals.append(segment.value)
    assert literals == ["cmd", "a", "b", "c"]
    assert references == ["alpha", "middle", "zeta"]


def test_value_from_literal_and_reference_is_one_typed_token() -> None:
    command = lower_command({
        "baseCommand": "cp",
        "arguments": [{"valueFrom": "my report $(inputs.name).txt"}],
        "inputs": {"name": {"type": "string"}},
    })
    rendered = _render_template(command.tokens[-1], {})
    assert "'my report ' + name.toString() + '.txt'" in rendered
