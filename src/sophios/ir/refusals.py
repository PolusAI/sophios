"""What a target refuses: the constructs of the resolved view it cannot lower, and the error that carries every
refusal at once. Refusing is a target's job; the view (`sophios.ir.resolved`) refuses nothing."""
from dataclasses import dataclass
from enum import StrEnum

from ..lang.diagnostics import Diagnostic, Diagnostics, Locator, Severity, lines
from ..lang.error_codes import SophiosErrorCode
from ..lang.spans import SourceSpan


class Construct(StrEnum):
    """What a target refuses: the construct of the view a refusal is about."""

    WORKFLOW = 'workflow'
    WORKFLOW_INPUT = 'workflow input'
    WORKFLOW_OUTPUT = 'workflow output'
    STEP = 'step'
    STEP_INPUT = 'input'
    WHEN = 'when'
    SCATTER = 'scatter'
    CALL = 'call'
    REQUIREMENT = 'requirement'
    TOOL = 'tool'
    POLICY = 'policy'


@dataclass(frozen=True, slots=True)
class Refusal:  # pylint: disable=too-many-instance-attributes
    """One construct a target cannot lower, where its author wrote it, and the code of the reason.

    It is reported as its `diagnostic`, an error: `<span>: error [<code>] <construct>[ '<subject>']: <reason>`, then
    `(step '<step>')` for a construct of a step.
    """

    code: SophiosErrorCode
    construct: Construct
    #: The construct's name: an input or output, a requirement class, a policy, or for a tool the tool's file and the
    #: field's path in it, `echo.cwl: inputs.message.inputBinding`.
    subject: str | None
    #: The root workflow's name.
    workflow: str
    #: The qualified name of the step it is about; `None` for a construct of the workflow's own.
    step: str | None
    span: SourceSpan | None
    reason: str

    @property
    def message(self) -> str:
        """What it says: the construct, its subject, and the reason."""
        subject = f' {self.subject!r}' if self.subject is not None else ''
        return f'{self.construct}{subject}: {self.reason}'

    @property
    def diagnostic(self) -> Diagnostic:
        """The refusal as Sophios reports any error."""
        return Diagnostic(Severity.ERROR, self.code, self.message, self.span,
                          Locator(step=self.step) if self.step is not None else None)


class NotLowered(ValueError):
    """What a target refuses to lower: every refusal, all at once.

    Its text is its `diagnostics`, one line each, as Sophios prints any error.
    """

    def __init__(self, refusals: tuple[Refusal, ...]) -> None:
        self.refusals = refusals
        self.diagnostics = Diagnostics(refusal.diagnostic for refusal in refusals)
        super().__init__('\n'.join(lines(self.diagnostics)))
