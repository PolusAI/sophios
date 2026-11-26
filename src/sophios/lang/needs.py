"""What a step or a workflow asks of the tools beneath it: the requirements and hints it wrote, read out of the keys it
passes through, and the names of the other keys that nothing reads."""
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .unread import UnreadField

if TYPE_CHECKING:
    from .nodes import Value
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
    back. `unread` holds the refusal of each field of a feature class that it writes beyond what the class has."""

    name: str
    written: 'Value'
    hint: bool
    need: 'Need | None' = None
    refused: Exception | None = None
    unread: tuple[UnreadField, ...] = ()
