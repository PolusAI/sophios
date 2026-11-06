"""How a Python caller configures edge inference: each command-line flag, spelled as an argument."""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from ._errors import InvalidInferenceError

_FORMAT_RULES = ("continue", "break")


@dataclass(frozen=True, slots=True)
class Inference:  # pylint: disable=too-many-instance-attributes
    """Every choice that alters how an unbound input is connected to an earlier output.

    Each field is one of the command line's options, but the Python API reads
    no configuration file of yours: ``renamings`` starts from the ones Sophios
    ships (``energy_`` to ``edr_``, ``structure_`` to ``tpr_``, ``traj_`` to
    ``trr_``), the same the command line starts from, and ``format_rules`` is
    empty unless you pass it, where the command line takes both from the
    configuration. Likewise ``insert_steps=True`` inserts only the
    converters among the tools in ``tool_registry=`` (those named
    ``insert_steps_automatically_*``), where the command line searches every
    tool on its search paths; pass ``plugins.get_tools_cwl(config)`` to give
    Python the command line's set. Pass the ``Inference`` to
    ``Workflow.compile(inference=...)`` or ``Workflow.run(inference=...)``;
    ``run`` infers the same edges as ``compile`` does for the same ``Inference``.

    Args:
        enabled (bool): Infer at all; False is ``--inference_disable``.
        strict (bool): Make a choice between equally good outputs an error
            (``wic043``) instead of a note; ``--inference_strict``.
        naming_conventions (bool): Prefer the output whose name matches the
            input's: ``input_x`` takes ``output_x``, and ``x`` takes ``x``.
            On by default; False is ``--no_inference_naming_conventions``.
        renamings (Sequence[tuple[str, str]]): ``(before, after)`` pairs applied
            to an input's name when no output pairs with it as written, as the
            configuration file's ``renaming_conventions`` are. Given, they
            replace the shipped ones; they need ``naming_conventions=True``.
            None, the default, means the shipped ones.
        insert_steps (bool): Let inference insert a step that would make a
            connection possible; ``--insert_steps_automatically``.
        format_rules (Mapping[str, str]): Per output format, ``'continue'`` or
            ``'break'``, as the configuration file's ``inference_rules`` are.
            They are read only for an inserted step: ``'break'`` stops the
            backwards search at an inserted step whose output has that format.
            They need ``insert_steps=True``.

    Raises:
        InvalidInferenceError: (``api007``) If a format rule is not ``'continue'``
            or ``'break'``, a renaming is not a pair of strings, ``renamings`` is
            given while ``naming_conventions`` is False, or ``format_rules`` is
            given while ``insert_steps`` is False.
    """

    enabled: bool = True
    strict: bool = False
    naming_conventions: bool = True
    renamings: Sequence[tuple[str, str]] | None = None
    insert_steps: bool = False
    format_rules: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for renaming in self.renamings or ():
            if not (isinstance(renaming, (tuple, list)) and len(renaming) == 2
                    and all(isinstance(part, str) for part in renaming)):
                raise InvalidInferenceError(
                    f"Inference.renamings holds {renaming!r}, which is not a pair of strings",
                    "write each renaming as (before, after), such as ('structure_', 'tpr_')")
        if self.renamings and not self.naming_conventions:
            raise InvalidInferenceError(
                "Inference.renamings is given but naming_conventions is False, so the renamings would do nothing",
                "set naming_conventions=True, or drop renamings")
        if self.format_rules and not self.insert_steps:
            raise InvalidInferenceError(
                "Inference.format_rules is given but insert_steps is False, so the format rules would do nothing",
                "set insert_steps=True, or drop format_rules")
        for name, rule in self.format_rules.items():
            if not isinstance(name, str) or rule not in _FORMAT_RULES:
                raise InvalidInferenceError(
                    f"Inference.format_rules maps {name!r} to {rule!r}, which is not 'continue' or 'break'",
                    "map each format to 'continue' or 'break'")
        if self.renamings is not None:
            object.__setattr__(self, "renamings", tuple((before, after) for before, after in self.renamings))
        object.__setattr__(self, "format_rules", MappingProxyType(dict(self.format_rules)))
