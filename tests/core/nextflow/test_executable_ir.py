"""Executable IR invariants: identifiers, immutability, serialization, and graph rules."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import partial
import re
from typing import Any, cast

from hypothesis import example, given, strategies as st
import pytest

from sophios.input_output_nf import render_nextflow
from sophios.lang.subset import Expr, parse
from sophios.nf_symbols import (
    NEXTFLOW_SCRIPT_DECLARED_NAMES, NEXTFLOW_SCRIPT_NAMES, is_nextflow_identifier, normalize_nextflow_identifier,
)
from sophios.nf_types import (
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
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)

from ..budgets import budget
from ..cases import match
from .testkit import command, output_port, ref, template

BROKEN_BY = {"cycle": "workflow connections contain a cycle", "duplicate name": "duplicate process name",
             "missing": "unknown (source|destination) process 'MISSING'", "repeated emit": "duplicate output emit name",
             "second source": "has more than one source"}


@st.composite
def graphs(draw: st.DrawFn) -> tuple[Any, ...]:
    """A DAG valid by construction, then up to two mutations, each breaking the one rule BROKEN_BY names it by."""
    names = [f"P{index}" for index in range(draw(st.integers(1, 6)))]
    processes, feeds, extra, broken = [], dict[tuple[str, str], Any](), list[Any](), []
    for index, name in enumerate(names):
        ports = [NfPort(f"in{port}", "path") for port in range(draw(st.integers(0, 2)))]
        processes.append(NfProcess(name, ports, [output_port("out", name)], command("true")))
        for each in ports:
            source = draw(st.sampled_from([None, *names[:index]]))
            feeds[name, each.name] = (NfProcessConnection(source, "out", name, each.name) if source
                                      else NfWorkflowInputConnection(f"{name}_{each.name}", name, each.name))
    types = {edge.from_port: "File" for edge in feeds.values() if isinstance(edge, NfWorkflowInputConnection)}
    # Parameters no step reads are listed in one order, whatever order the model holds them in.
    types |= {f"unread{index}": "int" for index in range(draw(st.integers(0, 2)))}
    emits = [NfWorkflowOutputConnection(n, "out", f"e{n}") for n in draw(st.lists(st.sampled_from(names), unique=True))]
    for rule in sorted(draw(st.lists(st.sampled_from(sorted(BROKEN_BY)), max_size=2, unique=True))):
        to, port = draw(st.sampled_from(sorted(feeds))) if feeds else ("", "")
        if rule == "cycle" and to:  # the feed of `to` from `to` or from a process below it
            below = {to}
            for _ in names:
                below |= {down for (down, _port), edge in feeds.items() if getattr(edge, "from_process", "") in below}
            feeds[to, port] = NfProcessConnection(draw(st.sampled_from(sorted(below))), "out", to, port)
        elif rule == "duplicate name":
            processes.append(NfProcess(draw(st.sampled_from(names)), [], [output_port("out", "dup")], command("true")))
        elif rule == "missing":
            ends = draw(st.permutations(["MISSING", draw(st.sampled_from(names))]))
            extra.append(NfProcessConnection(ends[0], "out", ends[1], "in0"))
        elif rule == "repeated emit" and emits and len(names) > 1:  # from another process, or it is a duplicate edge
            e = draw(st.sampled_from(emits))
            emits.append(NfWorkflowOutputConnection(next(n for n in names if n != e.from_process), "out", e.to_port))
        elif rule == "second source" and to:  # a parameter, so unlike the feed, or it is a duplicate edge
            types["second"] = "File"
            extra.append(NfWorkflowInputConnection("second", to, port))
        else:
            continue
        broken.append(rule)
    order = draw(st.permutations(sorted(types)))
    return processes, draw(st.permutations([*feeds.values(), *extra])), emits, {n: types[n] for n in order}, broken


_A, _B = (NfProcess(name, [NfPort("value", "path")], [output_port("out", name)], command("true")) for name in "AB")
#: A chain of twelve processes, and the edges that link it.
_CHAIN = [NfProcess(f"C{index}", [NfPort("source", "path")] * (index > 0), [output_port("out", f"C{index}")],
                    command("true")) for index in range(12)]
_LINKS = [NfProcessConnection(f"C{index - 1}", "out", f"C{index}", "source") for index in range(1, 12)]


@pytest.mark.fast
@budget(150)
@given(graphs())
@example(([_A, _B], [NfProcessConnection("A", "out", "B", "value"), NfProcessConnection("B", "out", "A", "value")], [],
          {}, ("cycle",)))
@example(([_A, _B], [NfWorkflowInputConnection(name, "A", "value") for name in ("first", "second")], [],
          {"first": "File", "second": "File"}, ("second source",)))
@example(([_A, _A], [], [], {}, ("duplicate name",)))
@example(([], [NfWorkflowInputConnection("message", "MISSING", "message")], [], {"message": "string"}, ("missing",)))
@example(([_A, _B], [], [NfWorkflowOutputConnection(name, "out", "result") for name in "AB"], {}, ("repeated emit",)))
@example(([], [], [], {"b": "int", "a": "int"}, ()))
# A chain longer than the generator draws, then closed by an edge from its last process back to its first.
@example((_CHAIN, _LINKS, [], {}, ()))
@example(([replace(_CHAIN[0], inputs=(NfPort("source", "path"),)), *_CHAIN[1:]],
          [*_LINKS, NfProcessConnection("C11", "out", "C0", "source")], [], {}, ("cycle",)))
def test_a_graph_constructs_exactly_when_it_breaks_no_rule(graph: tuple[Any, ...]) -> None:
    """A broken graph is refused naming a rule it breaks; a valid one constructs and renders one form, whatever order
    its incoming edges and params come in (design §5.2)."""
    processes, incoming, outputs, types, broken = graph
    params = {name: f"{name}.txt" if declared == "File" else 1 for name, declared in types.items()}
    try:
        workflow = ExecutableNextflowWorkflow("wf", processes, [*incoming, *outputs], params, types)
    except ValueError as error:
        assert broken and any(re.search(BROKEN_BY[rule], str(error)) for rule in broken), str(error)
        return
    assert not broken
    canonical = ExecutableNextflowWorkflow("wf", processes, [*sorted(incoming, key=repr), *outputs],
                                           dict(sorted(params.items())), dict(sorted(types.items())))
    assert canonical == workflow and render_nextflow(canonical) == render_nextflow(workflow)


#: (name, whether it is a Nextflow identifier), as the real v2 parser reads it: test_runtime.py runs each through it.
NAMES = [("café", True), ("Δelta", True), ("$money", True), ("_private", True), ("Ⅻstep", True), ("process", True),
         ("if", False), ("class", False), ("_", False), ("9start", False), ("dash-name", False), ("😀", False)]


@pytest.mark.fast
@pytest.mark.parametrize(("name", "valid"), NAMES)
def test_a_name_is_an_identifier_exactly_as_nextflow_reads_it(name: str, valid: bool) -> None:
    assert is_nextflow_identifier(name) is valid
    if valid:
        assert NfPort(name, "val").name == name
    else:
        with pytest.raises(ValueError, match="Nextflow identifier"):
            NfPort(name, "val")


def _proc(
    port: NfPort, *tokens: NfCommandToken, stdout: NfTemplate | None = None, outputs: tuple[NfPort, ...] = ()
) -> NfProcess:
    """A process with the one input `port` whose command is `echo` and then `tokens`."""
    return NfProcess("P", [port], list(outputs), NfCommand((template("echo"), *tokens), stdout=stdout))


def _env_process(*env: NfEnvVar, qualifier: str = "val") -> NfProcess:
    return NfProcess("ENV", [NfPort("x", qualifier)], [], NfCommand((template("env"),), env=env))


def _entry_workflow(
    *inputs: NfPort, outputs: tuple[NfPort, ...] = (), tokens: tuple[str | NfTemplate, ...] = ("true",),
    streams: Mapping[str, NfTemplate] | None = None,
) -> ExecutableNextflowWorkflow:
    process = NfProcess("STAGE", inputs, outputs, command(*tokens, **(streams or {})))
    connections = [NfWorkflowInputConnection(port.name, "STAGE", port.name) for port in inputs]
    return ExecutableNextflowWorkflow(
        "wf", [process], connections, {port.name: "in.txt" for port in inputs}, {port.name: "File" for port in inputs}
    )


def _text_capture_process(name: str = "READ") -> NfProcess:
    glob = template("out.txt")
    return NfProcess(
        name,
        [NfPort("message", "val")],
        [NfPort("text", "val", "text", glob, capture="text")],
        NfCommand((template("printf"),), stdout=glob),
    )


def _conditional(condition: Any = None, *, is_array: Any = False) -> NfProcess:
    return NfProcess("TASK", [NfPort("a", "val", is_array=is_array)], [output_port("result", "out.txt")],
                     command("true"), condition=condition)


_BASENAME = NfTemplate((NfBasenameReference("v"),))
_PLAIN = NfTemplate((NfInputReference("v"),))
_OUT = template("out.txt")
_ARRAY_ONLY = "array-marked inputs may only be referenced by array bindings"
_SEGMENTS = "^" + re.escape("template segments must be one of: NfLiteral, NfInputReference, NfBasenameReference") + "$"
_ARRAY = NfPort("v", "val", is_array=True)
_EITHER = (ValueError, TypeError)

#: Each IR value or process, built when its row runs, and how construction refuses it: a ValueError's pattern, an
#: (exception type, pattern) pair, or None for the accepted neighbour of a refusal.
UNREPRESENTABLE: dict[str, tuple[Callable[[], Any], Any]] = {
    **{f"NfPort-{name}-qualifier": (partial(NfPort, "reads", name), "qualifier")
       for name in ("tuple", "env", "stdin", "each", "unsupported")},
    "NfPort-reserved-name": (partial(NfPort, "__sophios_shell_quote_9f72e", "val"),
                             "reserved Nextflow backend identifier"),
    "NfPort-entry-on-a-val": (partial(NfPort, "source", "val", entry=NfEntry()), "only a path input that is not array"),
    "NfPort-entry-on-an-array": (partial(NfPort, "source", "path", is_array=True, entry=NfEntry()),
                                 "only a path input that is not array"),
    "NfPort-entry-on-an-output": (partial(NfPort, "source", "path", "source", template("out"), entry=NfEntry()),
                                  "only a path input that is not array"),
    # A capture marker is closed data, so it can never hold expression text.
    **{f"NfPort-capture-{text!r}": (partial(NfPort, "result", "path", "result", _OUT, capture=text),
                                    "port capture must be one of")
       for text in ("$(self[0])", "$(self[0].contents)", "single_path", "contents", "")},
    # Projecting the first match is provable only when one match exists.
    **{f"NfPort-capture-of-{glob}": (partial(NfPort, "result", "path", "result", template(glob), capture="single"),
                                     "no wildcard character")
       for glob in ("*.txt", "out?.txt", "out[0].txt", "*", "sub/*.txt")},
    # A reference's runtime value cannot be shown wildcard-free at compile time.
    **{f"NfPort-capture-of-a-reference-{index}": (partial(NfPort, "result", "path", "result", glob, capture="single"),
                                                  "single-literal glob with no input reference")
       for index, glob in enumerate((template(ref("name")), template("out-", ref("name")),
                                     NfTemplate((NfBasenameReference("source"), NfLiteral(".copy")))))},
    "NfPort-capture-without-a-glob": (partial(NfPort, "result", "path", "result", None, capture="single"),
                                      "single-literal glob with no input reference"),
    "NfPort-single-capture-on-a-val": (partial(NfPort, "result", "val", "result", _OUT, capture="single"),
                                       "capture 'single' requires a path port"),
    "NfPort-text-capture-on-a-path": (partial(NfPort, "text", "path", "text", _OUT, capture="text"),
                                      "capture 'text' requires a val port"),
    "NfPort-capture-and-array": (partial(NfPort, "result", "path", "result", _OUT, is_array=True, capture="single"),
                                 "cannot combine with an array marker"),
    **{f"NfEntry-{name!r}": (partial(NfEntry, name), reason) for name, reason in (
        ("", "non-empty"), ("   ", "non-empty"), ("sub/dir.txt", "path separator"), ("a\x00b", "NUL byte"),
        (".sophios_entry_x", "Sophios reserves"), (".", "names a directory"), ("..", "names a directory"))},
    **{f"NfFlag-prefix-{prefix!r}": (partial(NfFlag, "reverse", prefix), "prefix") for prefix in ("", "   ")},
    **{f"NfArrayBinding-prefix-{prefix!r}": (partial(NfArrayBinding, "names", prefix), "prefix")
       for prefix in ("", "   ")},
    "NfOptionalBinding-blank-prefix": (partial(NfOptionalBinding, "name", " ", True), "prefix"),
    "NfOptionalBinding-unseparated-without-a-prefix": (partial(NfOptionalBinding, "name", None, False),
                                                       "needs a prefix"),
    "NfShellLiteral-not-a-string": (partial(NfShellLiteral, cast(Any, 3)), (TypeError, "must be a string")),
    "NfShellLiteral-NUL": (partial(NfShellLiteral, "bad\x00text"), "NUL"),
    "NfTemplate-flag": (partial(NfTemplate, (cast(Any, NfFlag("reverse", "-r")),)), (TypeError, _SEGMENTS)),
    "NfTemplate-shell-literal": (partial(NfTemplate, (cast(Any, NfShellLiteral(">>")),)), (TypeError, _SEGMENTS)),
    **{f"NfEnvVar-value-{index}": (partial(NfEnvVar, "A", value), "literal text or one input reference")
       for index, value in enumerate((template("a", ref("x")), NfTemplate((NfBasenameReference("x"),))))},
    **{f"NfEnvVar-name-{name!r}": (partial(NfEnvVar, name, template("v")), "shell variable name")
       for name in ("", "1A", "A-B", "A B", "A=B")},
    **{f"NfResources-accelerator-{value!r}": (partial(NfResources, accelerator=cast(Any, value)),
                                              "accelerator must be a positive integer")
       for value in (0, -1, True, 1.5, "2")},
    "NfWorkflowInputConnection-None-process": (
        partial(NfWorkflowInputConnection, "message", cast(Any, None), "message"), ((TypeError, ValueError), None)),
    "NfProcess-env": (partial(_env_process, NfEnvVar("A", template("a $b")), NfEnvVar("_B2", template(ref("x")))),
                      None),
    "NfProcess-env-set-twice": (partial(_env_process, NfEnvVar("A", template("1")), NfEnvVar("A", template("2"))),
                                "more than once"),
    **{f"NfProcess-basename-of-a-{kind}": (partial(NfProcess, "COPY", [NfPort("source", "path", path_kind=kind)], [],
                                                   command("cp", NfTemplate((NfBasenameReference("source"),)))), None)
       for kind in ("file", "directory")},
    # Unlike a flag, an array binding may target either qualifier.
    **{f"NfProcess-array-binding-of-a-{qualifier}": (partial(
        NfProcess, "NAMES", [NfPort("names", qualifier, is_array=True)], [],
        NfCommand((template("echo"), NfArrayBinding("names")))), None) for qualifier in ("val", "path")},
    "NfProcess-array-typed-output": (partial(NfProcess, "MAKE", [], [NfPort("result", "path", "result", _OUT,
                                                                            is_array=True)], command("true")),
                                     "array-typed outputs are deferred"),
    "NfProcess-val-output": (partial(NfProcess, "P", [], [NfPort("o", "val", "o")], command("true")),
                             "path qualifier and typed glob"),
    "NfProcess-val-output-with-a-glob": (partial(NfProcess, "P", [], [NfPort("o", "val", "o", _OUT)], command("true")),
                                         "require path qualifier and typed glob"),
    "NfProcess-text-capture": (_text_capture_process, None),
    "NfProcess-two-entries-one-name": (partial(_entry_workflow, NfPort("a", "path", entry=NfEntry("same.txt")),
                                               NfPort("b", "path", entry=NfEntry("same.txt"))),
                                       "two InitialWorkDirRequirement entries with one literal name"),
    "NfProcess-ref-flag-to-unknown": (partial(_proc, NfPort("other", "val"), NfFlag("v", "-r")), "unknown inputs"),
    "NfProcess-ref-array-to-unknown": (partial(_proc, NfPort("other", "val"), NfArrayBinding("v", "-n")),
                                       "unknown inputs"),
    "NfProcess-ref-flag-to-a-path": (partial(_proc, NfPort("v", "path"), NfFlag("v", "-r")),
                                     "flag tokens must reference val inputs"),
    "NfProcess-ref-env-to-a-path": (partial(_env_process, NfEnvVar("A", template(ref("x"))), qualifier="path"),
                                    "environment variables must reference val inputs: x"),
    # A basename is valid in every template position, so each is checked.
    **{f"NfProcess-ref-basename-of-a-val-{where}": (partial(_proc, NfPort("v", "val"), *tokens, **streams),
                                                    "basename references must target path inputs")
       for where, tokens, streams in (("token", [_BASENAME], {}), ("stdout", [], {"stdout": _BASENAME}),
                                      ("glob", [], {"outputs": (NfPort("o", "path", "o", _BASENAME),)}))},
    "NfProcess-ref-array-binding-to-a-scalar": (partial(_proc, NfPort("v", "val"), NfArrayBinding("v", "--name")),
                                                "array bindings must reference array-marked inputs"),
    "NfProcess-ref-optional-binding-to-a-path": (partial(_proc, NfPort("v", "path"), NfOptionalBinding("v")),
                                                 "optional bindings must reference val inputs"),
    "NfProcess-ref-optional-binding-to-an-array": (partial(_proc, _ARRAY, NfOptionalBinding("v")), f"{_ARRAY_ONLY}: v"),
    # A plain reference renders values.toString(), so a two-item array reaches the command line as "[a, b]".
    "NfProcess-ref-array-in-text": (partial(_proc, _ARRAY, template("--joined=", ref("v"))), _ARRAY_ONLY),
    "NfProcess-ref-array-token": (partial(_proc, _ARRAY, _PLAIN), _ARRAY_ONLY),
    "NfProcess-ref-array-stdout": (partial(_proc, _ARRAY, stdout=_PLAIN), _ARRAY_ONLY),
    "NfProcess-ref-array-glob": (partial(_proc, _ARRAY, outputs=(NfPort("o", "path", "o", _PLAIN),)), _ARRAY_ONLY),
    # A File[]'s text is the whole list's, so its basename is the tail of [v1/a.txt, v2/b.txt], and no one file's glob.
    "NfProcess-ref-basename-of-an-array": (partial(_proc, NfPort("v", "path", is_array=True), NfTemplate(
        (NfLiteral("--tag="), NfBasenameReference("v")))), _ARRAY_ONLY),
    "NfProcess-ref-basename-of-an-array-glob": (partial(_proc, NfPort("v", "path", is_array=True), outputs=(NfPort(
        "o", "path", "o", NfTemplate((NfBasenameReference("v"), NfLiteral(".done")))),)), _ARRAY_ONLY),
    # A flag would let list truthiness decide whether its prefix appears.
    "NfProcess-ref-flag-to-an-array": (partial(_proc, _ARRAY, NfFlag("v", "--verbose")), f"{_ARRAY_ONLY}: v"),
    "NfProcess-condition-false": (partial(_conditional, False), (_EITHER, "condition must be a typed Expr")),
    "NfProcess-condition-string": (partial(_conditional, Expr("string", value="false")),
                                   (_EITHER, "condition must compute a boolean")),
    "NfProcess-condition-unknown-op": (partial(_conditional, Expr("bogus")),
                                       (_EITHER, "unsupported construct 'bogus'")),
    "NfProcess-condition-wrong-arity": (partial(_conditional, Expr(">", (Expr("ref", value="a"),))),
                                        (_EITHER, "> takes two operands")),
    **{f"NfProcess-is-array-{value!r}": (partial(_conditional, is_array=value), (_EITHER, "is_array must be a bool"))
       for value in ("false", 0)},
}


@pytest.mark.fast
@pytest.mark.parametrize(("build", "refusal"), UNREPRESENTABLE.values(), ids=UNREPRESENTABLE)
def test_a_malformed_ir_value_is_refused_at_construction(build: Callable[[], Any], refusal: Any) -> None:
    """A malformed IR value or process is unrepresentable: construction refuses it, naming the field."""
    if refusal is None:
        build()
        return
    error, pattern = refusal if isinstance(refusal, tuple) else (ValueError, refusal)
    with pytest.raises(error, match=pattern):
        build()


def _one(params: dict[str, Any], types: dict[str, str] | None = None) -> ExecutableNextflowWorkflow:
    return ExecutableNextflowWorkflow("WF", [NfProcess("ONE", [NfPort("value", "val")], [], command("true"))],
                                      [NfWorkflowInputConnection("value", "ONE", "value")], params, types or {})


def _shared(*ports: NfPort, adapter: str | None = None, declared: str = "string") -> ExecutableNextflowWorkflow:
    """One parameter `shared` into each port, the first through `adapter`."""
    tokens: list[Any] = [(template("x"), *[NfArrayBinding(port.name)] * port.is_array) for port in ports]
    return ExecutableNextflowWorkflow(
        "wf", [NfProcess(f"S{index}", [port], [], NfCommand(tokens[index])) for index, port in enumerate(ports)],
        [NfWorkflowInputConnection("shared", f"S{index}", port.name, adapter if index == 0 else None)
         for index, port in enumerate(ports)], {"shared": ["a", "b"]}, {"shared": declared})


def _adapted_workflow(
    adapter: str | None = "dotproduct",
    port: NfPort | None = None,
) -> ExecutableNextflowWorkflow:
    # The param has to match what the edge consumes: a scattered edge
    # takes the whole array, while an unadapted edge into a scalar port
    # takes one value.
    destination = port or NfPort("item", "val")
    array_valued = adapter is not None or destination.is_array
    return ExecutableNextflowWorkflow(
        "wf",
        [NfProcess("SCATTER", [destination], [], command("true"))],
        [NfWorkflowInputConnection("items", "SCATTER", "item", adapter)],
        {"items": ["a", "b"] if array_valued else "a"},
        {"items": "string[]" if array_valued else "string"},
    )


def _workflow(processes: list[NfProcess], *connections: NfConnection, **params: Any) -> ExecutableNextflowWorkflow:
    """`processes` joined by `connections`, each parameter typed by its value: string, string list, or int."""
    return ExecutableNextflowWorkflow("wf", processes, connections, params, {
        name: "string[]" if isinstance(value, list) else "string" if isinstance(value, str) else "int"
        for name, value in params.items()})


def _scattered_pair(method: str, *following: NfPort, edge: NfConnection, edge_first: bool = False,
                    condition: Expr | None = None, **more: NfConnection) -> ExecutableNextflowWorkflow:
    """PAIR scattered by `method` over two inputs, and NEXT, which `edge` and `more` feed."""
    pair = NfProcess("PAIR", [NfPort("a", "val"), NfPort("b", "val")], [output_port("f", "out.txt")], command("true"),
                     condition=condition)
    inputs = [NfWorkflowInputConnection(f"{name}vals", "PAIR", name, method) for name in "ab"]
    edges = [edge, *inputs] if edge_first else [*inputs, edge]
    return _workflow([pair, NfProcess("NEXT", following, [output_port("g", "g.txt")], command("true"))], *edges,
                     *more.values(), NfWorkflowOutputConnection("NEXT", "g", "r"),
                     **{f"{name}vals": [name] for name in ("a", "b", *more)})


def _optional_producer() -> NfProcess:
    return NfProcess("MAYBE", [NfPort("x", "val")], [NfPort("o", "path", "o", template("maybe.txt"), optional=True)],
                     command("true"))


_EACH = (NfWorkflowInputConnection("items", "FIRST", "item", "dotproduct"),
         NfWorkflowInputConnection("items", "SECOND", "item", "dotproduct"))
_TEXT = _text_capture_process("PRODUCE")
_MESSAGE = NfWorkflowInputConnection("message", "PRODUCE", "message")
_QUALIFIERS = "incompatible channel qualifiers"

#: Each workflow, built when its row runs, and what construction gives: a ValueError's pattern naming the edge, None
#: when it constructs, or the canonical connections it constructs with.
WORKFLOWS: dict[str, tuple[Callable[[], Any], Any]] = {
    "params-key-not-a-string": (partial(ExecutableNextflowWorkflow, "wf", (), (), cast(Any, {1: "value"})),
                                "keys must be strings"),
    "params-not-json": (partial(ExecutableNextflowWorkflow, "wf", (), (), {"value": cast(Any, {1, 2})}),
                        "JSON-compatible"),
    "param-without-a-type": (partial(_one, {"value": "a"}), "give a type to exactly the parameters in params: value"),
    "type-without-a-param": (partial(_one, {"value": "a"}, {"value": "string", "extra": "int"}),
                             "give a type to exactly the parameters in params: extra"),
    "param-type-long": (partial(_one, {"value": 1}, {"value": "long"}), "must be one of PARAM_TYPES, not 'long'"),
    **{f"param-type-{declared}-into-a-val": (partial(_one, {"value": "a"}, {"value": declared}),
                                             re.escape(f"'value' of type '{declared}' cannot feed ONE.value"))
       for declared in ("string[]", "File")},
    **{f"param-type-{declared}": (partial(_one, {"value": "a"}, {"value": declared}), None)
       for declared in ("boolean", "int", "float", "string", "string?")},
    # The port's list arity would deliver a scalar output as a one-element list and hide the mismatch.
    "scalar-output-into-an-array-port": (partial(_workflow, [
        NfProcess("PRODUCE", [], [output_port("out", "out.txt")], command("touch", "out.txt")),
        NfProcess("JOIN", [NfPort("values", "path", is_array=True)], [],
                  NfCommand((template("cat"), NfArrayBinding("values"))))],
        NfProcessConnection("PRODUCE", "out", "JOIN", "values")), "incompatible channel cardinalities"),
    # An array port and a scalar port must never share one workflow parameter.
    "one-param-into-a-scalar-and-an-array": (partial(_shared, NfPort("shared", "val"),
                                                     NfPort("shared", "val", is_array=True)), _QUALIFIERS),
    "one-param-into-a-val-and-a-path": (partial(_shared, NfPort("shared", "val"), NfPort("shared", "path")),
                                        _QUALIFIERS),
    "one-param-into-a-file-and-a-directory": (partial(_shared, NfPort("shared", "path"), NfPort(
        "shared", "path", path_kind="directory"), declared="File"), _QUALIFIERS),
    # A scattered sink takes the whole array, so the parameter carries a list there and a scalar here.
    "one-param-scattered-and-taken-as-a-scalar": (partial(_shared, NfPort("item", "val"), NfPort("item", "val"),
                                                          adapter="dotproduct", declared="string[]"), _QUALIFIERS),
    **{f"adapter-{adapter!r}": (partial(_adapted_workflow, adapter),
                                "channel adapter must be one of dotproduct, flat_crossproduct, nested_crossproduct")
       for adapter in ("gather", "scatter", "", "collect")},
    "adapter-into-an-array-port": (partial(_adapted_workflow, port=NfPort("item", "val", is_array=True)),
                                   "cannot target the array-marked port SCATTER.item"),
    # One element per invocation cannot fill an array-marked port, which would be bound to a bare Path.
    **{f"{method}-rescattered-into-an-array-port": (partial(
        _scattered_pair, "dotproduct", NfPort("x", "path", is_array=True), NfPort("y", "val"),
        edge=NfProcessConnection("PAIR", "f", "NEXT", "x", method),
        y=NfWorkflowInputConnection("yvals", "NEXT", "y", method)),
        f"channel adapter '{method}' cannot target the array-marked port NEXT.x")
       for method in ("dotproduct", "flat_crossproduct", "nested_crossproduct")},
    "process-mixing-scatter-methods": (partial(
        _workflow, [NfProcess("PAIR", [NfPort("first", "val"), NfPort("second", "val")], [], command("true"))],
        NfWorkflowInputConnection("firsts", "PAIR", "first", "flat_crossproduct"),
        NfWorkflowInputConnection("seconds", "PAIR", "second", "dotproduct"), firsts=["a"], seconds=["b"]),
        "mixes channel adapters dotproduct, flat_crossproduct"),
    **{f"{method}-ungathered-edge-{'first' if first else 'last'}": (partial(
        _scattered_pair, method, NfPort("x", "path"), edge=NfProcessConnection("PAIR", "f", "NEXT", "x"),
        edge_first=first), r"connection PAIR\.f -> NEXT\.x must carry a gather adapter")
       for method in ("dotproduct", "flat_crossproduct") for first in (False, True)},
    **{f"{method}-skipped-positions-gathered-{'first' if first else 'last'}": (partial(
        _scattered_pair, method, NfPort("x", "path", is_array=True), edge_first=first,
        edge=NfProcessConnection("PAIR", "f", "NEXT", "x", "gather"), condition=parse("$(inputs.a == 'x')")),
        r"connection PAIR\.f -> NEXT\.x gathers the skipped positions")
       for method in ("dotproduct", "flat_crossproduct") for first in (False, True)},
    # An adapter belongs to its scattered process; every sink here takes the whole array (design §5.2).
    "one-array-param-scattered-differently-and-taken-whole": (partial(_workflow, [
        NfProcess("PAIR", [NfPort("item", "val"), NfPort("tag", "val")], [], command("true")),
        NfProcess("EACH", [NfPort("item", "val")], [], command("true")),
        NfProcess("WHOLE", [NfPort("items", "val", is_array=True)], [],
                  NfCommand((template("echo"), NfArrayBinding("items"))))],
        NfWorkflowInputConnection("items", "PAIR", "item", "flat_crossproduct"),
        NfWorkflowInputConnection("tags", "PAIR", "tag", "flat_crossproduct"),
        NfWorkflowInputConnection("items", "EACH", "item", "dotproduct"),
        NfWorkflowInputConnection("items", "WHOLE", "items"),
        items=["a", "b"], tags=["t"]), None),
    "one-param-adapted-at-every-sink": (lambda: _workflow([NfProcess(name, [NfPort("item", "val")], [], command("true"))
                                                           for name in ("FIRST", "SECOND")], *_EACH,
                                                          items=["a", "b"]).connections, _EACH),
    # The graph has no qualifier agreement check for process edges to lean on.
    "captured-text-into-a-process": (partial(_workflow, [_TEXT, NfProcess(
        "CONSUME", [NfPort("text", "val")], [output_port("result", "copy.txt")],
        command("printf", "%s", template(ref("text")), stdout="copy.txt"))],
        _MESSAGE, NfProcessConnection("PRODUCE", "text", "CONSUME", "text"),
        NfWorkflowOutputConnection("CONSUME", "result", "result"), message="hi"), r"PRODUCE.text captures file text"),
    "captured-text-into-a-workflow-output": (partial(_workflow, [_TEXT], _MESSAGE, NfWorkflowOutputConnection(
        "PRODUCE", "text", "captured"), message="hi"), None),
    "workflow-named-like-a-process": (partial(ExecutableNextflowWorkflow, "echo", [NfProcess(
        "echo", [], [output_port("o", "o.txt")], command("true"))], [], {}),
        "workflow name 'echo' is also a process name"),
    "optional-output-into-a-workflow-output": (partial(_workflow, [_optional_producer()], NfWorkflowInputConnection(
        "x", "MAYBE", "x"), NfWorkflowOutputConnection("MAYBE", "o", "o"), x=1), None),
    "optional-output-into-a-process": (partial(_workflow, [_optional_producer(), NfProcess(
        "CONSUME", [NfPort("source", "path")], [], command("true"))], NfWorkflowInputConnection("x", "MAYBE", "x"),
        NfProcessConnection("MAYBE", "o", "CONSUME", "source"), x=1), "optional output"),
    "optional-output-of-a-scatter": (partial(_workflow, [_optional_producer()], NfWorkflowInputConnection(
        "xs", "MAYBE", "x", "dotproduct"), NfWorkflowOutputConnection("MAYBE", "o", "o"), xs=["1"]), "optional output"),
}


@pytest.mark.fast
@pytest.mark.parametrize(("build", "outcome"), WORKFLOWS.values(), ids=WORKFLOWS)
def test_a_workflow_constructs_exactly_when_its_channels_agree(build: Callable[[], Any], outcome: Any) -> None:
    """Connections and params agree on channel kind, cardinality, scatter adapter, capture and optionality, or
    construction fails naming the edge."""
    if isinstance(outcome, str):
        with pytest.raises(ValueError, match=outcome):
            build()
    else:
        built = build()
        assert outcome is None or built == outcome


@pytest.mark.fast
@pytest.mark.parametrize(("value", "payload"), [
    (NfArrayBinding("names"), {"exact": {"kind": "array", "name": "names", "prefix": None}}),
    (NfFlag("reverse", "-r"), {"exact": {"kind": "flag", "name": "reverse", "prefix": "-r"}}),
    (NfBasenameReference("source"), {"exact": {"kind": "basename", "name": "source"}}),
    (NfShellLiteral(">>"), {"exact": {"kind": "shell_literal", "text": ">>"}}),
    (NfOptionalBinding("name", "--name=", False),
     {"exact": {"kind": "optional_binding", "name": "name", "prefix": "--name=", "separate": False}}),
    *((_entry_workflow(NfPort("source", "path", entry=entry)),
       {"processes": [{"inputs": [{"entry": {"name": entry.name, "copy": entry.copy}}]}]})
      for entry in (NfEntry(), NfEntry(copy=True), NfEntry("renamed.txt"), NfEntry("a*b?.txt", True))),
    (ExecutableNextflowWorkflow("wf", [], [], {}),
     {"schema_version": ExecutableNextflowWorkflow.SCHEMA_VERSION, "representation_kind": "executable"}),
])
def test_a_value_writes_its_payload_fields(value: Any, payload: dict[str, Any]) -> None:
    """A command part's payload is exactly its fields; an entry and the workflow header are read out of a larger one."""
    assert match(payload, value.to_dict())


@pytest.mark.fast
@pytest.mark.parametrize("segment", [NfInputReference("source"), NfBasenameReference("source")])
def test_only_an_entry_under_its_own_basename_is_referenced(segment: NfInputReference | NfBasenameReference) -> None:
    _entry_workflow(NfPort("source", "path", entry=NfEntry(copy=True)), tokens=("cat", NfTemplate((segment,))))
    with pytest.raises(ValueError, match="references an input whose InitialWorkDirRequirement entry has a literal name"):
        _entry_workflow(NfPort("source", "path", entry=NfEntry("renamed.txt")), tokens=("cat", NfTemplate((segment,))))


@pytest.mark.fast
@pytest.mark.parametrize("segment", [NfInputReference("source"), NfBasenameReference("source")])
@pytest.mark.parametrize(("before", "after"), [("", ""), ("./", ""), ("", "/keep.txt"), ("a/../", "/")])
def test_a_glob_at_a_staged_input_is_rejected_unless_the_input_is_an_entry(
    segment: NfInputReference | NfBasenameReference, before: str, after: str,
) -> None:
    """design §6, Basename references: a staged input, or a name inside it, is not a file a CWL glob matches; an
    entry is, and a name derived from the input's is a new one."""
    output = NfPort("result", "path", "result", NfTemplate((NfLiteral(before), segment, NfLiteral(after))))
    with pytest.raises(ValueError, match="output 'result' globs the staged input 'source', or a name inside it"):
        _entry_workflow(NfPort("source", "path"), outputs=(output,))
    _entry_workflow(NfPort("source", "path", entry=NfEntry()), outputs=(output,))
    for derived in (NfTemplate((segment, NfLiteral(".copy"))), NfTemplate((NfLiteral("out/"), segment))):
        _entry_workflow(NfPort("source", "path"), outputs=(replace(output, glob=derived),))


@pytest.mark.fast
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("segment", [NfInputReference("source"), NfBasenameReference("source")])
@pytest.mark.parametrize("after", ["", "/keep.txt"])
def test_a_stream_writing_a_staged_input_is_rejected_unless_the_input_is_a_copy(
    stream: str, segment: NfInputReference | NfBasenameReference, after: str,
) -> None:
    """design §6, Basename references: writing the link an input is staged under, or a name inside it, writes
    through the link into the input; a writable entry is a copy, and a name derived from the input's is a new one."""
    target = NfTemplate((segment, NfLiteral(after)))
    for entry in (None, NfEntry()):
        with pytest.raises(ValueError, match=f"{stream} writes through the link input 'source' is staged under"):
            _entry_workflow(NfPort("source", "path", entry=entry), streams={stream: target})
    _entry_workflow(NfPort("source", "path", entry=NfEntry(copy=True)), streams={stream: target})
    _entry_workflow(NfPort("source", "path"), streams={stream: NfTemplate((segment, NfLiteral(".log")))})


@pytest.mark.fast
@pytest.mark.parametrize(("name", "normalized"), [
    *((name, f"_{name}") for name in [*sorted(NEXTFLOW_SCRIPT_NAMES), "Math", "Élan", "if"]), ("9-café", "_9_café"),
])
def test_no_name_takes_a_name_the_script_resolves(name: str, normalized: str) -> None:
    assert normalize_nextflow_identifier(name) == normalized
    assert normalize_nextflow_identifier(name, variable=False) == normalized


@pytest.mark.fast
@pytest.mark.parametrize("name", sorted(NEXTFLOW_SCRIPT_DECLARED_NAMES - NEXTFLOW_SCRIPT_NAMES))
def test_only_a_process_or_workflow_avoids_a_name_every_script_declares(name: str) -> None:
    assert normalize_nextflow_identifier(name, variable=False) == f"_{name}"
    assert normalize_nextflow_identifier(name) == name
    with pytest.raises(ValueError, match="already declared by every Nextflow script"):
        NfProcess(name, [], [output_port("o", "o.txt")], command("true"))
    with pytest.raises(ValueError, match="already declared by every Nextflow script"):
        ExecutableNextflowWorkflow(name, [], [], {})


@pytest.mark.fast
@pytest.mark.parametrize(("glob", "capture"), [("*.log", None), ("maybe.txt", "single")], ids=["pattern", "single"])
def test_an_optional_output_of_at_most_one_file_is_checked_at_its_workflow_output(
    glob: str, capture: str | None
) -> None:
    # Nextflow fails arity '1' when nothing matches, optional or not, and refuses '0..1'.
    process = NfProcess(
        "MAYBE", [], [NfPort("o", "path", "o", template(glob), capture=capture, optional=True)], command("true")
    )
    with pytest.raises(ValueError, match="optional outputs o must match at most one file and must reach a workflow"):
        ExecutableNextflowWorkflow("WF", [process], [], {})
    workflow = ExecutableNextflowWorkflow("WF", [process], [NfWorkflowOutputConnection("MAYBE", "o", "result")], {})
    rendered = render_nextflow(workflow)
    assert "arity: '0..*', emit: o" in rendered and "optional: true" not in rendered
