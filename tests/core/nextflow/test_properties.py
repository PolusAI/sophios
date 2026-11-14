"""Laws of the executable Nextflow boundary: names, quoting, how a command is read, and what the pipeline
writes, each over generated values."""

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
@given(st.text(alphabet=st.characters(blacklist_categories=SURROGATE_CATEGORIES), min_size=1, max_size=80),
       st.from_regex(r"[a-z][a-z0-9]{0,12}", fullmatch=True), st.from_regex(r"[a-z][a-z0-9]{0,12}", fullmatch=True))
def test_a_name_normalizes_to_one_identifier_and_two_names_never_to_one_silently(
        value: str, prefix: str, suffix: str) -> None:
    normalized = normalize_nextflow_identifier(value)
    assert is_nextflow_identifier(normalized)
    assert normalize_nextflow_identifier(normalized) == normalized
    with pytest.raises(ValueError, match="normalize to"):
        _normalized_identifiers((f"{prefix}-{suffix}", f"{prefix}_{suffix}"), context="workflow input")


@budget(200)
@given(SAFE_TEXT | st.just(""), SAFE_TEXT, st.text(alphabet=st.characters(
    blacklist_categories=SURROGATE_CATEGORIES)).filter(str.strip))
def test_a_text_reaches_the_shell_and_groovy_as_itself(value: str, suffix: str, any_text: str) -> None:
    """A literal or interpolated template is one shell argument; a literal glob stays a single-quoted string and an
    interpolated one escapes no backtick, which Groovy cannot; every single-quoted position of the pipeline uses the
    one escaper and writes no control character."""
    assert shlex.split(_shell_quote(value)) == [value]
    assert "__sophios_shell_quote_9f72e" in _render_template(NfTemplate((NfLiteral(value),)), {})
    injected = "adversarial ' value; $(touch SHOULD_NOT_EXIST)"
    assert "value.toString()" in _render_template(NfTemplate(
        (NfLiteral(value), NfInputReference("value"), NfLiteral(suffix))), {})
    assert shlex.split(_shell_quote(f"{value}{injected}{suffix}")) == [f"{value}{injected}{suffix}"]
    literal = _render_glob(NfTemplate((NfLiteral(value),)), {})
    assert literal.startswith("'") and literal.endswith("'")
    # Groovy has no \` escape, so no backtick may be escaped: the escaper doubles every backslash, so an even run of
    # them before a backtick leaves it unescaped.
    body = _render_glob(NfTemplate((NfLiteral(f"`{value}"), NfInputReference("name"))), {})[1:-1]
    for index, char in enumerate(body):
        if char == "`":
            assert (len(body[:index]) - len(body[:index].rstrip("\\"))) % 2 == 0
    assert "${name}" in body
    # An entry name is one path component, and never `.`, `..` or the reserved staging prefix.
    staged = "_" + any_text.translate(str.maketrans("/\x00", "__"))
    spoken = "".join(char for char in any_text if " " <= char <= "~" and char != "\\")  # a when literal's alphabet
    process = NfProcess("TASK", [NfPort("s", "val"), NfPort("src", "path", entry=NfEntry(staged))], [],
                        NfCommand((NfTemplate((NfLiteral("true"),)),)), container=any_text,
                        condition=Expr("==", (Expr("ref", (), "s"), Expr("string", (), spoken))))
    rendered = render_nextflow(ExecutableNextflowWorkflow(
        "PIPELINE", [process], [NfWorkflowInputConnection(name, "TASK", name) for name in ("s", "src")],
        {"s": "x", "src": "in.txt"}, {"s": "string", "src": "File"}))
    for written in (groovy_literal(any_text), groovy_literal(staged), groovy_literal(spoken)):
        assert written in rendered
    assert not any(ord(char) < 32 and char != "\n" or ord(char) == 127 for char in rendered)


@budget(300)
@given(st.lists(st.sampled_from(INTERPOLATION_PIECES), max_size=8).map("".join),
       st.permutations(("zeta", "alpha", "middle")))
# Once refused: the text on the two sides of a token joined into `${`, `$(`.
@example("{$$(inputs.x){$", ("zeta", "alpha", "middle"))
@example("($$(inputs.x)($", ("alpha", "middle", "zeta"))
@example("$\\$((", ("middle", "zeta", "alpha"))
def test_a_command_is_read_as_cwltool_reads_it(text: str, order: tuple[str, ...]) -> None:
    """A template interpolates as cwltool interpolates it, or is refused where cwltool fails; inputs bound at one
    position come in the order of their names, after the arguments, whatever order they are declared in."""
    try:
        # cwltool interpolates a string only when it contains an expression.
        expected = interpolate(text, {"inputs": {"x": "VALUE"}}) if "$(" in text or "${" in text else text
    except (JavascriptException, SubstitutionError):
        with pytest.raises(ValueError, match="unsupported CWL expression"):
            lower_template(text)
    else:
        template = lower_template(text)
        assert "".join(segment.value if isinstance(segment, NfLiteral) else "VALUE"
                       for segment in template.segments) == expected
    command = lower_command({"baseCommand": "cmd", "arguments": ["a", "b", "c"],
                             "inputs": {name: {"type": "string", "inputBinding": {}} for name in order}})
    templates = [token for token in command.tokens if isinstance(token, NfTemplate)]
    assert [[segment.value if isinstance(segment, NfLiteral) else getattr(segment, "name")
             for segment in token.segments] for token in templates] == [
        ["cmd"], ["a"], ["b"], ["c"], ["alpha"], ["middle"], ["zeta"]]
    assert len(templates) == len(command.tokens)


@budget(150)
@given(SAFE_TEXT, st.sampled_from(("file", "directory")),
       st.one_of(st.integers(min_value=1, max_value=10**30),
                 st.floats(min_value=0.000001, max_value=10**15, allow_nan=False, allow_infinity=False)),
       st.integers(min_value=1, max_value=1_000_000).map(lambda value: value + 0.5),
       st.lists(st.booleans(), max_size=10))
@example("input.txt", "file", 1234567, 2.5, [False])
@example("input[1]", "directory", 1, 1.5, [True])
def test_a_pipeline_writes_its_parameters_and_directives_as_the_model_holds_them(
        path: str, path_kind: str, memory_mb: int | float, cpus: float, containers: list[bool]) -> None:
    """A path parameter, a bare path or the object the compiler writes, is one channel of its kind staged without
    globbing; memory is written without an exponent and keeps its value; a fractional core count is unrepresentable;
    Docker is enabled exactly when a process has a container."""
    declared = "Directory" if path_kind == "directory" else "File"
    read = NfProcess("READ", (NfPort("source", "path", path_kind=path_kind),), (),
                     NfCommand((NfTemplate((NfLiteral("true"),)),)), resources=NfResources(memory_mb=memory_mb))
    connections = (NfWorkflowInputConnection("source", "READ", "source"),)
    from_string = ExecutableNextflowWorkflow("wf", (read,), connections, {"source": path}, {"source": declared})
    from_mapping = ExecutableNextflowWorkflow(
        "wf", (read,), connections, {"source": {"class": declared, "path": path}}, {"source": declared})
    rendered = render_nextflow(from_string)
    assert rendered == render_nextflow(from_mapping)
    assert (f"Channel.value(file(__sophios_input_9f72e('source', params.source, '{declared}'), glob: false))"
            in rendered)
    assert from_string.to_dict()["processes"][0]["inputs"][0]["path_kind"] == path_kind
    memory = next(line.strip() for line in rendered.splitlines() if "memory " in line)
    written = memory.removeprefix('memory "').removesuffix(' MB"')
    assert "e" not in written.lower() and Decimal(written) == Decimal(str(memory_mb))
    with pytest.raises(ValueError, match="positive integer"):
        NfResources(cpus=cast(Any, cpus))
    processes = tuple(NfProcess(f"P{index}", (), (), NfCommand((NfTemplate((NfLiteral("true"),)),)),
                                container="ubuntu:24.04" if containerized else None)
                      for index, containerized in enumerate(containers))
    assert render_nextflow_config(ExecutableNextflowWorkflow("wf", processes, (), {})) == (
        f"docker.enabled = {str(any(containers)).lower()}\n")
