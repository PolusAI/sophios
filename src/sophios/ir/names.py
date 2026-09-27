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

from .types import DerivedName, Namespace, PortName, StepId, StepOutputRef, WorkflowGraph

#: Joins a step to the name nested inside it.
NAMESPACE_SEPARATOR = '___'


def render_step_id(workflow: str, index: int, name: str) -> str:
    """The text a step occurrence carries at `index` in `workflow`.

    Public for the two document readers that recognize a generated name by
    comparing against it: an authored `outputSource` in Lower and generated
    WIC in the Python API. Neither may parse the result.

    Args:
        workflow (str): The name of the workflow the step belongs to.
        index (int): The step's one-based position in that workflow.
        name (str): The step's authored name.

    Returns:
        str: The rendered step id.
    """
    return f'{workflow}__step__{index}__{name}'


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
            nested = [step.emission.run.child for step in graph.steps
                      if step.emission is not None and step.emission.run.child is not None]
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
