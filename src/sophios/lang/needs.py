"""What a step or a workflow asks of the tools beneath it: the requirements and hints it wrote, read out of the keys it
passes through, and the names of the other keys that nothing reads."""
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .nodes import OpaqueCwl
    from .tools import Need

#: The classes that decide only whether a workflow may use a feature, which cwltool reads at the step: on a workflow or
#: a step each is an inert declaration, and a tool that inherits one lowers it to nothing.
FEATURES = frozenset({'MultipleInputFeatureRequirement', 'ScatterFeatureRequirement',
                      'StepInputExpressionRequirement', 'SubworkflowFeatureRequirement'})


@dataclass(frozen=True, slots=True)
class Inherited:
    """A requirement or hint a step or a workflow wrote, by class: the tools beneath it inherit it. `need` is what the
    front door read of it, once, where it was written; a body that is not its class is `refused`, with the loader's
    message, for a tool that inherits it to raise. `written` is the body as written, for the CWL target to write
    back."""

    name: str
    written: 'OpaqueCwl'
    hint: bool
    need: 'Need | None' = None
    refused: Exception | None = None


def split(passthrough: 'tuple[tuple[str, OpaqueCwl], ...]') -> tuple[tuple[Inherited, ...], tuple[str, ...]]:
    """The requirements then the hints written as a mapping, in the order written; and the name of every other key,
    a `requirements` or `hints` of another shape among them."""
    # pylint: disable-next=import-outside-toplevel,cyclic-import  # the front door reads it where it is written
    from .cwl_tool import read_inherited
    found: list[Inherited] = []
    unread: list[str] = []
    for key, value in passthrough:
        if key in ('requirements', 'hints') and isinstance(value, dict):
            found.extend(Inherited(str(name), body, key == 'hints', *read_inherited(str(name), body, key == 'hints'))
                         for name, body in value.items())
        else:
            unread.append(key)
    return tuple(sorted(found, key=lambda each: each.hint)), tuple(unread)
