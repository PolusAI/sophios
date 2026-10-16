"""The one place a structural identity becomes the text a document carries.

Every phase before Emit holds step, port and source identities as values --
`StepId`, `PortName`, `StepOutputRef` -- and none of them spells one. `Names`
is built from a finished graph and assigns each identity its outward spelling,
so a definition and every reference to it are rendered by the same table
rather than reconstructed separately.

A step is spelled as it was written, less the `.wic` a call names its
workflow by; a name the compiler derives joins the step and the port with
`__`. Each emitted document's names are unique in it, and each emitted file's
name is unique in the one directory the tree is written to: see `Names.of`.
Rendering is one-way. Nothing here takes text apart, and nothing may: a
consumer that needs to know what a name came from is given the identity.
"""
from collections import Counter
from dataclasses import dataclass

from .types import DerivedName, Namespace, PortName, ProcessRun, RegistryKey, StepId, StepOutputRef, WorkflowGraph

#: Joins a step to the name nested inside it.
NAMESPACE_SEPARATOR = '__'


def authored_path(name: PortName) -> tuple[str, ...]:
    """What `name` is called where it was written: the authored name of each
    step it was exposed through, outermost first, then the port's own name.

    Walks the identity, one level per step, so it stays linear in the depth a
    derived name was exposed through.

    Args:
        name (PortName): A port or boundary name, written or derived.

    Returns:
        tuple[str, ...]: The step names, then the port name.
    """
    parts: list[str] = []
    while isinstance(name, DerivedName):
        parts.append(name.step.name)
        name = name.port
    return (*parts, name)


def claim(text: str, taken: set[str]) -> str:
    """`text`, or `text_2`, `text_3`, ... the first not in `taken`, which it is added to.

    Names are compared casefolded: `Foo.cwl` and `foo.cwl` are one file on
    macOS and Windows, and CWL ids are names a person tells apart by reading.
    """
    claimed, suffix = text, 2
    while claimed.casefold() in taken:
        claimed, suffix = f'{text}_{suffix}', suffix + 1
    taken.add(claimed.casefold())
    return claimed


def run_stem(run: ProcessRun) -> str:
    """The stem of the file `run`'s process was read from: `touch` for `.../touch.cwl`,
    `make_greeting` for `.../make_greeting.wic`; its registry name when it was read from none."""
    leaf = run.target.replace('\\', '/').rsplit('/', 1)[-1]
    stem = leaf.rsplit('.', 1)[0] if leaf.endswith(('.cwl', '.wic')) else ''
    return stem or run.process_id.name


@dataclass(frozen=True, slots=True)
class Names:
    """The outward spelling of every step, derived name and file in a graph tree."""

    steps: dict[StepId, str]
    derived: dict[DerivedName, str]
    files: dict[StepId, str]

    @classmethod
    def of(cls, root: WorkflowGraph) -> 'Names':
        """The table for `root` and every graph nested in it.

        In each document, inputs, outputs and steps share CWL's one id space,
        and the names the author wrote claim it first:

        - an input or output keeps its authored name;
        - a step keeps its id, less `.wic`, unless another step has it or an
          input or output is called it: then it is `<id>_<index>`, the
          position the author wrote it at, which `(index, id)` names (a step
          Infer inserted is numbered after every authored one);
        - a derived name is `<step>__<port>`, and `_2`, `_3`, ... when that
          is taken, by a step or by a name on its own side (an input and an
          output may share a name, as CWL allows).

        Every document of the tree is written to one directory, each named
        for the file it was read from (`run_stem`), the root first and then in
        the order the steps run them: a tool once however many steps run it, a
        called workflow once per call, and `_2`, `_3`, ... after a name another
        process took.

        Args:
            root (WorkflowGraph): The outermost graph that will be rendered.

        Returns:
            Names: The spelling of each step occurrence and derived name in the tree.
        """
        graphs: list[WorkflowGraph] = []
        seen: set[Namespace] = set()

        def collect(graph: WorkflowGraph) -> None:
            if graph.namespace in seen:
                return
            seen.add(graph.namespace)
            nested = [step.run.child for step in graph.steps
                      if step.run is not None and step.run.child is not None]
            for child in (*graph.children, *nested):
                collect(child)
            graphs.append(graph)  # after its children: a derived name spells theirs

        collect(root)
        table = cls({}, {}, {})
        for graph in graphs:
            table._spell(graph)  # pylint: disable=protected-access
        taken = {root.name.casefold()}
        tools: dict[RegistryKey, str] = {}

        def name_files(graph: WorkflowGraph) -> None:
            for step in graph.steps:
                if step.run is None:
                    continue
                if step.run.child is None:
                    if step.run.process_id not in tools:
                        tools[step.run.process_id] = claim(run_stem(step.run), taken)
                    table.files[step.id] = tools[step.run.process_id]
                else:
                    table.files[step.id] = claim(run_stem(step.run), taken)
                    name_files(step.run.child)

        name_files(root)
        return table

    def _spell(self, graph: WorkflowGraph) -> None:
        """Enter `graph`'s steps and derived boundary names, its children's already entered."""
        authored = {side: {str(port.name).casefold() for port in ports if not isinstance(port.name, DerivedName)}
                    for side, ports in (('in', graph.workflow_inputs), ('out', graph.workflow_outputs))}
        ids = authored['in'] | authored['out']
        written = [(step, step.id.name.removesuffix('.wic')) for step in graph.steps]
        repeated = Counter(name.casefold() for _step, name in written)
        for step, name in written:
            if repeated[name.casefold()] == 1 and name.casefold() not in ids:
                self.steps[step.id] = claim(name, ids)
        for step, name in written:
            if step.id not in self.steps:
                self.steps[step.id] = claim(f'{name}_{step.id.index}', ids)
        steps = {self.steps[step.id].casefold() for step in graph.steps}
        for side, ports in (('in', graph.workflow_inputs), ('out', graph.workflow_outputs)):
            taken = authored[side] | steps
            for port in ports:
                if isinstance(port.name, DerivedName) and port.name not in self.derived:
                    self.derived[port.name] = claim(self._joined(port.name), taken)

    def _joined(self, name: DerivedName) -> str:
        return f'{self.step(name.step)}{NAMESPACE_SEPARATOR}{self.port(name.port)}'

    def step(self, step: StepId) -> str:
        """The id `step` carries in its own document."""
        return self.steps[step]

    def file(self, step: StepId) -> str:
        """The stem of the file `step`'s process is written to: its `run:` is `<stem>.cwl`."""
        return self.files[step]

    def qualified(self, step: StepId) -> str:
        """`step`'s id prefixed by every step it is nested in, outermost first."""
        return NAMESPACE_SEPARATOR.join(
            (*(self.step(part) for part in step.namespace.parts), self.step(step)))

    def port(self, name: PortName) -> str:
        """The text a port or boundary name carries."""
        match name:
            case DerivedName():
                return self.derived.get(name) or self._joined(name)
            case _:
                return str(name)

    def source(self, ref: 'PortName | StepOutputRef') -> str:
        """The text a `source:` or `outputSource:` reference carries."""
        match ref:
            case StepOutputRef(step=step, port=port):
                return f'{self.step(step)}/{self.port(port)}'
            case _:
                return self.port(ref)
