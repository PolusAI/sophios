"""The one place a structural identity becomes the text a document carries.

Every phase before Emit holds step, port and source identities as values --
`StepId`, `PortName`, `StepOutputRef` -- and none of them spells one. `Names`
is built from a finished graph and assigns each identity its outward spelling,
so a definition and every reference to it are rendered by the same table
rather than reconstructed separately.

A step is spelled as it was written, less the `.wic` a call names its
workflow by; a name the compiler derives joins the step and the port with
`~`. Each emitted document's names are unique in it, and each emitted file is
named `<root>~<address>`, unique in the directory the tree is written to: see
`Names.of` and `Names.file`.
Rendering is one-way. Nothing here takes text apart, and nothing may: a
consumer that needs to know what a name came from is given the identity.
"""
from collections import Counter
from dataclasses import dataclass

from .types import DerivedName, PortName, StepId, StepOutputRef, WorkflowGraph

#: Joins a step to the name nested inside it.
NAMESPACE_SEPARATOR = '~'


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
    """The outward spelling of every step and file in a graph tree, and so of every derived name."""

    root: str
    steps: dict[StepId, str]

    @classmethod
    def of(cls, root: WorkflowGraph) -> 'Names':
        """The table for `root` and every graph nested in it.

        In each document, inputs, outputs and steps share CWL's one id space,
        and the names the author wrote claim it first:

        - an input or output keeps its authored name;
        - a step keeps its id, less `.wic`, unless another step has it or an
          input or output is called it: then it is `<id>@<index>`, the
          position the author wrote it at, which `(index, id)` names (a step
          Infer inserted is numbered after every authored one);
        - a derived name is `<step>~<port>`, which no authored name is: none
          contains `~` (`sophios.lang.names`). An input and an output may
          share a name, as CWL allows.

        Every document of the tree is written beside the root, named by the
        root and the step that runs it (`file`): a tool once per step that
        runs it, a called workflow once per call.

        Args:
            root (WorkflowGraph): The outermost graph that will be rendered.

        Returns:
            Names: The spelling of each step occurrence and derived name in the tree.
        """
        table = cls(root.name, {})
        table._spell(root)  # pylint: disable=protected-access
        return table

    def _spell(self, graph: WorkflowGraph) -> None:
        """Enter the steps of `graph`, then of every graph nested in it."""
        authored = {side: {str(port.name).casefold() for port in ports if not isinstance(port.name, DerivedName)}
                    for side, ports in (('in', graph.workflow_inputs), ('out', graph.workflow_outputs))}
        ids = authored['in'] | authored['out']
        written = [(step, step.id.name.removesuffix('.wic')) for step in graph.steps]
        repeated = Counter(name.casefold() for _step, name in written)
        for step, name in written:
            kept = repeated[name.casefold()] == 1 and name.casefold() not in ids
            # An index is unique in its graph (Infer numbers an inserted step after
            # every other), and no kept or authored name contains `@`.
            self.steps[step.id] = name if kept else f'{name}@{step.id.index}'
        for child in graph.children:
            self._spell(child)

    def step(self, step: StepId) -> str:
        """The id `step` carries in its own document."""
        return self.steps[step]

    def file(self, step: StepId) -> str:
        """The stem of the file `step`'s process is written to, beside the root:
        `<root>~<address>`, the address being `qualified(step)`. No root name
        contains `~`, so the first one splits it off, and an address is unique
        in its root: two roots in one directory never share a file."""
        return f'{self.root}{NAMESPACE_SEPARATOR}{self.qualified(step)}'

    def qualified(self, step: StepId) -> str:
        """`step`'s id prefixed by every step it is nested in, outermost first."""
        return NAMESPACE_SEPARATOR.join(
            (*(self.step(part) for part in step.namespace.parts), self.step(step)))

    def port(self, name: PortName) -> str:
        """The text a port or boundary name carries."""
        if isinstance(name, DerivedName):
            return f'{self.step(name.step)}{NAMESPACE_SEPARATOR}{self.port(name.port)}'
        return str(name)

    def source(self, ref: 'PortName | StepOutputRef') -> str:
        """The text a `source:` or `outputSource:` reference carries."""
        match ref:
            case StepOutputRef(step=step, port=port):
                return f'{self.step(step)}/{self.port(port)}'
            case _:
                return self.port(ref)
