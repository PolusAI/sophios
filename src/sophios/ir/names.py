"""The one place a structural identity becomes the text a document carries.

Every phase before Emit holds step, port and source identities as values --
`StepId`, `PortName`, `StepOutputRef` -- and none of them spells one. `Names`
is built from a finished graph and assigns each identity its outward spelling,
so a definition and every reference to it are rendered by the same table
rather than reconstructed separately.

The spelling is the legacy one, kept for WIC and CWL compatibility:
`{workflow}__step__{i}__{name}` for a step, joined with `___` when nested.
Rendering is one-way. Nothing here takes text apart, and nothing may: a
consumer that needs to know what a name came from is given the identity.
"""
from dataclasses import dataclass
from typing import Any

from .types import DerivedName, Namespace, PortName, StepId, StepOutputRef, WorkflowGraph

#: Joins a step to the name nested inside it.
NAMESPACE_SEPARATOR = '___'


def render_step_id(workflow: str, index: int, name: str) -> str:
    """The text a step occurrence carries at `index` in `workflow`.

    Public for Lower, which recognizes a generated name in an authored
    `outputSource` by comparing against it, and for tests that state the
    spelling. Nothing may parse the result.

    Args:
        workflow (str): The name of the workflow the step belongs to.
        index (int): The step's one-based position in that workflow.
        name (str): The step's authored name.

    Returns:
        str: The rendered step id.
    """
    return f'{workflow}__step__{index}__{name}'


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


@dataclass(frozen=True, slots=True)
class Names:
    """The outward spelling of every step in a graph tree.

    A step's rendered index is its position in its graph when the table is
    built, not `StepId.index`: an occurrence keeps its identity when Infer
    inserts a step before it, but the document numbers steps as it lists them.
    """

    steps: dict[StepId, str]
    positions: dict[StepId, int]

    @classmethod
    def of(cls, root: WorkflowGraph) -> 'Names':
        """The table for `root` and every graph nested in it.

        Args:
            root (WorkflowGraph): The outermost graph that will be rendered.

        Returns:
            Names: The spelling of each step occurrence in the tree.
        """
        found: dict[StepId, str] = {}
        positions: dict[StepId, int] = {}
        seen: set[Namespace] = set()

        def visit(graph: WorkflowGraph) -> None:
            if graph.namespace in seen:
                return
            seen.add(graph.namespace)
            for position, step in enumerate(graph.steps, start=1):
                found[step.id] = render_step_id(graph.name, position, step.id.name)
                positions[step.id] = position
            nested = [step.run.child for step in graph.steps
                      if step.run is not None and step.run.child is not None]
            for child in (*graph.children, *nested):
                visit(child)

        visit(root)
        return cls(found, positions)

    def step(self, step: StepId) -> str:
        """The id `step` carries in its own document."""
        return self.steps[step]

    def position(self, step: StepId) -> int:
        """The one-based index `step` is rendered with."""
        return self.positions[step]

    def qualified(self, step: StepId) -> str:
        """`step`'s id prefixed by every step it is nested in, outermost first."""
        return NAMESPACE_SEPARATOR.join(
            (*(self.step(part) for part in step.namespace.parts), self.step(step)))

    def port(self, name: PortName) -> str:
        """The text a port or boundary name carries."""
        match name:
            case DerivedName(step=step, port=port):
                return f'{self.step(step)}{NAMESPACE_SEPARATOR}{self.port(port)}'
            case _:
                return str(name)

    def source(self, ref: 'PortName | StepOutputRef') -> str:
        """The text a `source:` or `outputSource:` reference carries."""
        match ref:
            case StepOutputRef(step=step, port=port):
                return f'{self.step(step)}/{self.port(port)}'
            case _:
                return self.port(ref)


def names_map(graph: WorkflowGraph, names: Names) -> dict[str, Any]:
    """Every emitted id in `graph`'s tree, mapped back to what the author wrote.

    Written beside the root CWL so a run-time message naming
    `w__step__2__append___file` can be read as step 2 `append`, port `file`,
    at `w.wic:7`. `steps` is keyed by the emitted step id, prefixed by every
    step it is nested in; each entry's `id` is the id the step carries in its
    own document, `index` is the position the author wrote the step at (the
    one a compile diagnostic counts), and `inserted` marks a step Infer added. `ports` is keyed by the emitted
    boundary name of every workflow input and output.

    Args:
        graph (WorkflowGraph): The compiled root graph.
        names (Names): The spelling table `graph` was emitted with.

    Returns:
        dict[str, Any]: `{'steps': {...}, 'ports': {...}}`, ready for JSON.
    """
    steps: dict[str, Any] = {}
    ports: dict[str, Any] = {}

    def visit(node: WorkflowGraph) -> None:
        for step in node.steps:
            steps[names.qualified(step.id)] = {
                'id': names.step(step.id), 'workflow': node.name,
                'index': step.id.index, 'name': step.id.name, 'inserted': step.synthesized,
                'file': step.span.file if step.span else None,
                'line': step.span.start_line if step.span else None}
        for port in (*node.workflow_inputs, *node.workflow_outputs):
            *parts, written = authored_path(port.name)
            ports[names.port(port.name)] = {'workflow': node.name, 'steps': parts, 'port': written}
        for child in node.children:
            visit(child)

    visit(graph)
    return {'steps': steps, 'ports': ports}
