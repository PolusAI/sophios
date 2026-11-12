"""Executable IR invariants: identifiers, immutability, serialization, and graph rules."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import partial
import re
from typing import Any, cast

from hypothesis import example, given, strategies as st
import pytest

from sophios.api.python.workflow import CompiledWorkflow
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
    NfStepInputConnection,
    NfTemplate,
    NfWorkflowInputConnection,
    NfWorkflowOutputConnection,
)

from sophios.utils_nf import NEXTFLOW

from ..budgets import budget
from .testkit import command, output_port, ref, runtime_workflow, template

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
    # A scalar type is what a val input receives when that is one scalar: never a path's, a list's, or another type.
    **{f"NfPort-scalar-type-{name}": (partial(NfPort, "v", qualifier, is_array=array, scalar_type=scalar),
                                      "a scalar type is one of")
       for name, qualifier, array, scalar in (("of-a-path", "path", False, "string"),
                                              ("of-an-array", "val", True, "int"), ("File", "val", False, "File"))},
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
    # A step-only input is a val the command never reads; the condition may (design §9).
    "NfProcess-step-only-path": (partial(NfProcess, "P", [], [], command("true"),
                                         step_only_inputs=[NfPort("after", "path")]), "must be val inputs"),
    "NfProcess-step-only-named-like-an-input": (partial(NfProcess, "P", [NfPort("v", "val")], [], command("true"),
                                                        step_only_inputs=[NfPort("v", "val")]),
                                                "duplicate input port names"),
    **{f"NfProcess-step-only-read-by-{where}": (partial(NfProcess, "P", [], [], NfCommand(tokens, env=env),
                                                        step_only_inputs=[NfPort("s", "val")]),
                                                "command reads the step-only inputs s")
       for where, tokens, env in (
           ("a-token", (template("echo", ref("s")),), ()),
           ("an-environment-variable", (template("true"),), (NfEnvVar("S", template(ref("s"))),)))},
    "NfProcess-condition-over-a-step-only-input": (partial(
        NfProcess, "P", [], [output_port("result", "out.txt")], command("true"), condition=parse("$(inputs.s)"),
        step_only_inputs=[NfPort("s", "val", scalar_type="boolean")]), None),
    "NfProcess-condition-over-a-value-that-may-be-null": (partial(
        NfProcess, "P", [NfPort("s", "val", scalar_type="boolean?")], [], command("true"),
        condition=parse("$(inputs.s)")), "optional inputs.s may only be compared with null"),
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
    "a-connection-from-an-unknown-process": (partial(_workflow, [NfProcess(
        "CONSUME", [NfPort("source", "path")], [], command("true"))], NfProcessConnection(
        "ABSENT", "o", "CONSUME", "source")), "connection references unknown source process 'ABSENT'"),
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
    # A val input's scalar type is what its connection gives (design §9): given none, it is given that one.
    "val-inputs-given-no-scalar-type": (lambda: _workflow(
        [NfProcess("ONE", [NfPort("v", "val")], [], command("true"), step_only_inputs=[NfPort("s", "val")])],
        NfWorkflowInputConnection("v", "ONE", "v"), NfWorkflowInputConnection("s", "ONE", "s"), v="a", s=1,
    ).processes[0], NfProcess("ONE", [NfPort("v", "val", scalar_type="string")], [], command("true"),
                              step_only_inputs=[NfPort("s", "val", scalar_type="int")])),
    "a-scalar-type-its-connection-does-not-give": (partial(_workflow, [NfProcess(
        "ONE", [NfPort("v", "val", scalar_type="int")], [], command("true"))],
        NfWorkflowInputConnection("v", "ONE", "v"), v="a"), "ONE.v receives 'string', but its scalar type is 'int'"),
    # A condition types over the scalar types its inputs are given.
    **{f"a-condition-over-{given}-input-given-no-scalar-type": (partial(lambda value: _workflow([_conditional(parse(
        "$(inputs.a > 1)"))], NfWorkflowInputConnection("a", "TASK", "a"), a=value), value), outcome)
       for given, value, outcome in (("an-int", 1, None),
                                     ("a-string", "x", "TASK' condition does not type over its inputs"))},
    "a-condition-over-a-step-only-input-after-a-path-output": (lambda: _workflow([
        NfProcess("TOUCH", [], [output_port("f", "f.txt")], command("touch", "f.txt")),
        NfProcess("ECHO", [], [output_port("result", "out.txt")], command("true"), condition=parse("$(inputs.after)"),
                  step_only_inputs=[NfPort("after", "val")])],
        NfProcessConnection("TOUCH", "f", "ECHO", "after")), "only scalar inputs are admitted"),
    # A step-only input takes a step's path output as a value, staging nothing; it is never scattered.
    "a-step-only-input-after-a-path-output": (partial(_workflow, [
        NfProcess("TOUCH", [], [output_port("f", "f.txt")], command("touch", "f.txt")),
        NfProcess("ECHO", [], [], command("true"), step_only_inputs=[NfPort("after", "val")])],
        NfProcessConnection("TOUCH", "f", "ECHO", "after")), None),
    "a-scattered-step-only-input": (partial(_workflow, [NfProcess("ECHO", [], [], command("true"), step_only_inputs=[
        NfPort("s", "val", scalar_type="string")])], NfWorkflowInputConnection("ss", "ECHO", "s", "dotproduct"),
        ss=["a"]), "step-only input ECHO.s cannot be scattered"),
}


def _staged(segment: NfInputReference | NfBasenameReference) -> dict[str, tuple[Callable[[], Any], Any]]:
    """design §6, Basename references: a glob at a staged input, or at a name inside it, is not a file a CWL glob
    matches, and a stream written there writes through the link into the input; an entry is matched and a writable
    one is a copy, and a name derived from the input's is a new one."""
    kind = type(segment).__name__
    globbed = "output 'result' globs the staged input 'source', or a name inside it"
    rows: dict[str, tuple[Callable[[], Any], Any]] = {}
    for before, after in (("", ""), ("./", ""), ("", "/keep.txt"), ("a/../", "/")):
        output = NfPort("result", "path", "result", NfTemplate((NfLiteral(before), segment, NfLiteral(after))))
        rows[f"glob-{kind}-{before}{after}-staged"] = (partial(_entry_workflow, NfPort("source", "path"),
                                                               outputs=(output,)), globbed)
        rows[f"glob-{kind}-{before}{after}-entry"] = (partial(
            _entry_workflow, NfPort("source", "path", entry=NfEntry()), outputs=(output,)), None)
    derived_names = (NfTemplate((segment, NfLiteral(".copy"))), NfTemplate((NfLiteral("out/"), segment)))
    for index, derived in enumerate(derived_names):
        rows[f"glob-{kind}-derived-{index}"] = (partial(_entry_workflow, NfPort("source", "path"), outputs=(
            NfPort("result", "path", "result", derived),)), None)
    for stream in ("stdout", "stderr"):
        for after in ("", "/keep.txt"):
            target = NfTemplate((segment, NfLiteral(after)))
            for entry, outcome in ((None, f"{stream} writes through the link input 'source' is staged under"),
                                   (NfEntry(), f"{stream} writes through the link input 'source' is staged under"),
                                   (NfEntry(copy=True), None)):
                rows[f"{stream}-{kind}{after}-{entry!r}"] = (partial(
                    _entry_workflow, NfPort("source", "path", entry=entry), streams={stream: target}), outcome)
        rows[f"{stream}-{kind}-derived"] = (partial(_entry_workflow, NfPort("source", "path"), streams={
            stream: NfTemplate((segment, NfLiteral(".log")))}), None)
    # Only an entry under its own basename is referenced: a literal name is the one the command reads.
    rows[f"reference-{kind}-to-a-copied-entry"] = (partial(_entry_workflow, NfPort(
        "source", "path", entry=NfEntry(copy=True)), tokens=("cat", NfTemplate((segment,)))), None)
    rows[f"reference-{kind}-to-a-renamed-entry"] = (partial(_entry_workflow, NfPort(
        "source", "path", entry=NfEntry("renamed.txt")), tokens=("cat", NfTemplate((segment,)))),
        "references an input whose InitialWorkDirRequirement entry has a literal name")
    return rows


def _optional(glob: str, capture: str | None, *connections: NfConnection) -> ExecutableNextflowWorkflow:
    """An optional output of at most one file, which Nextflow checks only where it reaches a workflow output."""
    process = NfProcess("MAYBE", [], [NfPort("o", "path", "o", template(glob), capture=capture, optional=True)],
                        command("true"))
    return ExecutableNextflowWorkflow("WF", [process], connections, {})


def _merged(*sources: Any, link_merge: str = "merge_flattened", pick_value: str | None = None,
            port: NfPort | None = None) -> ExecutableNextflowWorkflow:
    """PAIR gathers into, and A and the parameter xs join at, JOIN's input."""
    pair = NfProcess("PAIR", [NfPort("item", "val")], [output_port("f", "f.txt")], command("true"))
    produce = NfProcess("A", [], [output_port("f", "a.txt")], command("true"))
    join = NfProcess("JOIN", [port or NfPort("files", "path", is_array=True)], [], command("true"))
    return ExecutableNextflowWorkflow(
        "WF", [pair, produce, join],
        [NfWorkflowInputConnection("items", "PAIR", "item", "dotproduct"),
         NfStepInputConnection(sources, link_merge, pick_value)],
        {"items": ["x"], "xs": ["x.txt"], "one": "o.txt"}, {"items": "string[]", "xs": "File[]", "one": "File"},
    )


_GATHERED = NfProcessConnection("PAIR", "f", "JOIN", "files", "gather")
_ONE = NfProcessConnection("A", "f", "JOIN", "files")
_XS = NfWorkflowInputConnection("xs", "JOIN", "files")
_ARRAY_JOIN = "an array source joins only by merge_flattened with no pickValue"
_CONDITION = NfProcess("CALC", [NfPort("a", "val", scalar_type="int")], [output_port("out", "out.txt")],
                       command("true"), condition=parse("$(Math.pow(inputs.a, 2) > 1)"))
_AT_MOST_ONE = "optional outputs o must match at most one file and must reach a workflow"

#: Every row above, and the staged-input rules for both kinds of reference.
CONSTRUCTIONS: dict[str, tuple[Callable[[], Any], Any]] = {
    **UNREPRESENTABLE, **WORKFLOWS, **_staged(NfInputReference("source")), **_staged(NfBasenameReference("source")),
    **{f"optional-{glob}-alone": (partial(_optional, glob, capture), _AT_MOST_ONE)
       for glob, capture in (("*.log", None), ("maybe.txt", "single"))},
    **{f"optional-{glob}-into-a-workflow-output": (partial(_optional, glob, capture, NfWorkflowOutputConnection(
        "MAYBE", "o", "result")), None) for glob, capture in (("*.log", None), ("maybe.txt", "single"))},
    # A step input's sources join as the idioms can, at one process input, and are never scattered.
    "step-input-one-value-to-an-array-port": (partial(_merged, _ONE, pick_value="first_non_null"),
                                              "gives one value to a port that is array-typed"),
    "step-input-a-list-to-a-scalar-port": (partial(_merged, _ONE, link_merge="merge_nested", port=NfPort(
        "files", "path")), "gives a list to a port that is not array-typed"),
    "step-input-an-array-merged-nested": (partial(_merged, _GATHERED, _ONE, link_merge="merge_nested"), _ARRAY_JOIN),
    "step-input-an-array-picked": (partial(_merged, _XS, _ONE, pick_value="all_non_null"), _ARRAY_JOIN),
    "step-input-a-val-array-into-a-path": (partial(_merged, NfWorkflowInputConnection("items", "JOIN", "files")),
                                           "workflow input 'items' is val\\[\\], which its port's path cannot take"),
    "step-input-two-processes": (partial(_merged, _ONE, NfProcessConnection("A", "f", "PAIR", "item")),
                                 "must all reach one process input"),
    "step-input-scattered": (partial(_merged, NfWorkflowInputConnection("xs", "JOIN", "files", "dotproduct")),
                             "a step input's sources cannot be scattered"),
    "step-input-merge-deep": (partial(_merged, _ONE, link_merge="merge_deep"), "linkMerge must be one of"),
    # The lowering reads the resolved view, never a compiled document.
    "lowering-a-compiled-document": (partial(NEXTFLOW.lower, cast(Any, CompiledWorkflow("wf", {
        "class": "Workflow", "cwlVersion": "v1.2", "inputs": {}, "outputs": {}, "steps": []}, {}))),
        (TypeError, "resolved view")),
    # Sophios never writes a parameter value its own entry check stops, read by a step or not.
    **{f"absent-optional-{declared}": (partial(lambda declared: replace(
        runtime_workflow(), params={**runtime_workflow().params, "unread": []},
        param_types={**runtime_workflow().param_types, "unread": declared}), declared),
        re.escape(f"workflow input 'unread' is an absent optional {declared},"))
       for declared in ("File?", "Directory?")},
    # A tree built without parsing, as the import builds one, is checked when its process is built.
    **{f"condition-built-with-{name}": (partial(replace, _CONDITION, condition=replace(
        cast(Expr, _CONDITION.condition), args=(node, *cast(Expr, _CONDITION.condition).args[1:]))), pattern)
       for name, node, pattern in (
           ("a-call", Expr("Runtime.getRuntime().exec", (Expr("ref", value="a"),)), "unsupported construct"),
           ("a-literal", Expr("number", value="010"), "not a strict-mode decimal"))},
}


def _refusal(outcome: Any) -> tuple[Any, str | None] | None:
    """The exception type and pattern `outcome` states, or None when it states that construction succeeds."""
    if isinstance(outcome, str):
        return ValueError, outcome
    if isinstance(outcome, tuple) and len(outcome) == 2 and (isinstance(outcome[0], type) or (
            isinstance(outcome[0], tuple) and all(isinstance(each, type) for each in outcome[0]))):
        return outcome
    return None


def _as_stated(build: Callable[[], Any], outcome: Any) -> str:
    """Nothing when constructing as `build` does gives `outcome`; else what it gives."""
    refusal = _refusal(outcome)
    try:
        built = build()
    except (ValueError, TypeError) as error:
        stated = refusal is not None and isinstance(error, refusal[0]) and (
            refusal[1] is None or re.search(refusal[1], str(error)) is not None)
        return "" if stated else f"raised {error!r}"
    return "" if refusal is None and outcome in (None, built) else f"constructed {built!r}"


@pytest.mark.fast
def test_an_ir_value_constructs_exactly_as_its_row_says() -> None:
    """A malformed IR value, process or workflow is unrepresentable: construction refuses it, naming the field, or the
    edge where connections and params disagree on channel kind, cardinality, scatter adapter, capture, optionality or
    staging; its accepted neighbour constructs, as the canonical value where its row states one."""
    assert {name: problem for name, (build, outcome) in CONSTRUCTIONS.items()
            if (problem := _as_stated(build, outcome))} == {}


@pytest.mark.fast
def test_a_name_is_an_identifier_exactly_as_nextflow_reads_it() -> None:
    """A port takes a name Nextflow reads as an identifier, NAMES says which; normalizing a name the script resolves
    to something else prefixes it; only a process or workflow avoids a name every script declares."""
    for name, valid in NAMES:
        assert is_nextflow_identifier(name) is valid, name
        if valid:
            assert NfPort(name, "val").name == name
        else:
            with pytest.raises(ValueError, match="Nextflow identifier"):
                NfPort(name, "val")
    for name, normalized in [*((name, f"_{name}") for name in [*sorted(NEXTFLOW_SCRIPT_NAMES), "Math", "Élan", "if"]),
                             ("9-café", "_9_café")]:
        assert (normalize_nextflow_identifier(name), normalize_nextflow_identifier(name, variable=False)) == (
            normalized, normalized), name
    for name in sorted(NEXTFLOW_SCRIPT_DECLARED_NAMES - NEXTFLOW_SCRIPT_NAMES):
        assert (normalize_nextflow_identifier(name, variable=False), normalize_nextflow_identifier(name)) == (
            f"_{name}", name)
        with pytest.raises(ValueError, match="already declared by every Nextflow script"):
            NfProcess(name, [], [output_port("o", "o.txt")], command("true"))
        with pytest.raises(ValueError, match="already declared by every Nextflow script"):
            ExecutableNextflowWorkflow(name, [], [], {})
