"""What the run policy changes in the CWL a workflow is written as."""
from collections.abc import Sequence

from ..ir.policy import RunPolicy
from ..lang.cwl import one_expression
from ..lang.nodes import OpaqueCwl


def effective_when(authored: OpaqueCwl, needed: Sequence[str], policy: RunPolicy) -> OpaqueCwl:
    """The `when` a step is written with: the null checks of every input it requires, then its own.

    The checks come first, so the author's condition is evaluated only once every
    required input has arrived, and at most once: it is written once, verbatim.
    Without partial failure, or with no input to check, the step keeps the `when`
    its author wrote, or none. A `when` that is neither one `$(...)` nor one
    `${...}` is not a condition cwltool evaluates, and is written as it is.
    """
    if not policy.partial_failure or not needed:
        return authored
    checks = ' && '.join(f'inputs["{name}"] != null' for name in needed)
    if authored is None:
        return f'$({checks})'
    text = authored.strip() if isinstance(authored, str) else ''  # as cwltool reads an expression
    match one_expression(text):
        case '$(':
            return f'$({checks} && ({text[2:-1]}))'
        case '${':
            return f'${{if (!({checks})) {{ return false; }}{text[2:-1]}}}'
    return authored
