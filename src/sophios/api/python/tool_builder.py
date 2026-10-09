"""Public CWL v1.2 CommandLineTool authoring API.

The required core is intentionally small:

```python
inputs = Inputs(input=Input(cwl.directory, position=1))
outputs = Outputs(output=Output(cwl.directory, glob="$(inputs.input.basename)"))
tool = CommandLineTool("example", inputs, outputs)
```

Everything else is optional and chainable.
"""

# pylint: disable=too-many-lines

from dataclasses import MISSING, dataclass, field, fields as dataclass_fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from cwl_utils.parser import cwl_v1_2 as _cwl
from sophios.wic_types import Tools

from ._tool_builder_step_bridge import _command_line_tool_to_step
from ._tool_builder_namespaces import Field, Fields, Input, Inputs, Output, Outputs, cwl
from ._tool_builder_specs import (
    CommandArgument,
    CommandLineBinding,
    CommandOutputBinding,
    Dirent,
    DockerRequirement,
    EnvironmentDef,
    EnvVarRequirement,
    FieldSpec,
    InitialWorkDirRequirement,
    InlineJavascriptRequirement,
    InplaceUpdateRequirement,
    InputSpec,
    LoadListingRequirement,
    NetworkAccess,
    OutputSpec,
    ResourceRequirement,
    SchemaDefRequirement,
    SecondaryFile,
    ShellCommandRequirement,
    SoftwarePackage,
    SoftwareRequirement,
    ToolTimeLimit,
    WorkReuse,
    secondary_file,
)
from ._tool_builder_support import (
    _validate_path,
    _SUPPORT,
    _contains_expression,
    _merge_if_set,
    _normalize_requirement,
    _render,
    _render_doc,
    _sanitize_raw_mapping,
    _warn_raw_escape_hatch,
    ToolBuilderValidationError,
    ValidationResult,
    dump_wic_yaml,
    validate_cwl_document,
)
from ...lang.cwl import CWL_VERSION

if TYPE_CHECKING:
    from .workflow import Step


@dataclass(slots=True, init=False)
# pylint: disable=too-many-instance-attributes,too-many-public-methods
class CommandLineTool:
    """A CWL v1.2 `CommandLineTool`, built from Python objects.

    The constructor takes the tool's contract: its `id`, `inputs` and `outputs`.
    Each other method sets one more part of the document and returns the tool,
    so the calls chain. `to_cwl_document()` renders the document, `write_cwl()`
    writes it, `validate()` checks it with cwltool, and `Step(tool)` or
    `to_step()` puts it in a workflow.

    Args:
        name (str): The tool's `id`, and the default name of a step that runs it.
        inputs (Inputs): The tool's `inputs`, named by keyword.
        outputs (Outputs): The tool's `outputs`, named by keyword.
        cwl_version (str): The `cwlVersion` written, `v1.2` by default.

    Raises:
        TypeError: If `inputs` is not an `Inputs(...)` or `outputs` is not an `Outputs(...)`.
    """

    name: str
    inputs: Inputs
    outputs: Outputs
    cwl_version: str = CWL_VERSION
    label_text: str | None = None
    doc_text: str | list[str] | None = None
    _base_command: list[str] = field(default_factory=list)
    _arguments: list[str | dict[str, Any]] = field(default_factory=list)
    _requirements: dict[str, dict[str, Any]] = field(default_factory=dict)
    _hints: dict[str, dict[str, Any]] = field(default_factory=dict)
    _stdin: str | None = None
    _stdout: str | None = None
    _stderr: str | None = None
    _intent: list[str] = field(default_factory=list)
    _namespaces: dict[str, str] = field(default_factory=dict)
    _schemas: list[str] = field(default_factory=list)
    _success_codes: list[int] = field(default_factory=list)
    _temporary_fail_codes: list[int] = field(default_factory=list)
    _permanent_fail_codes: list[int] = field(default_factory=list)
    _extra: dict[str, Any] = field(default_factory=dict)

    _CONSTRUCTOR_FIELDS: ClassVar[frozenset[str]] = frozenset({"name", "inputs", "outputs", "cwl_version"})

    def __init__(
        self,
        name: str,
        inputs: Inputs,
        outputs: Outputs,
        *,
        cwl_version: str = CWL_VERSION,
    ) -> None:
        for item in dataclass_fields(self):
            if item.name in self._CONSTRUCTOR_FIELDS:
                continue
            setattr(self, item.name, item.default_factory() if item.default_factory is not MISSING else item.default)
        self.name = name
        self.inputs = inputs
        self.outputs = outputs
        self.cwl_version = cwl_version
        self.__post_init__()

    def __post_init__(self) -> None:
        match self.inputs:
            case Inputs():
                pass
            case _:
                raise TypeError("inputs must be an Inputs(...) collection")
        match self.outputs:
            case Outputs():
                pass
            case _:
                raise TypeError("outputs must be an Outputs(...) collection")

    def _store_requirement(
        self,
        bucket: dict[str, dict[str, Any]],
        requirement: Any,
        value: dict[str, Any] | None,
    ) -> None:
        class_name, payload = _normalize_requirement(requirement, value)
        if ":" in class_name:
            prefix, _ = class_name.split(":", 1)
            if prefix in _SUPPORT.known_namespaces and prefix not in self._namespaces:
                self._namespaces[prefix] = _SUPPORT.known_namespaces[prefix]
        bucket[class_name] = payload

    def _apply_spec(self, spec: Any, *, as_hint: bool) -> "CommandLineTool":
        self._store_requirement(self._hints if as_hint else self._requirements, spec, None)
        return self

    def _append_requirement_entry(
        self,
        class_name: str,
        list_key: str,
        item: Any,
        *,
        as_hint: bool = False,
    ) -> "CommandLineTool":
        target = self._hints if as_hint else self._requirements
        payload = target.setdefault(class_name, {list_key: []})
        listing = payload.setdefault(list_key, [])
        match listing:
            case list() as items:
                items.append(_render(item))
            case _:
                raise TypeError(f"{class_name} {list_key} must be a list")
        return self

    def describe(
        self,
        label: str | None = None,
        doc: str | list[str] | None = None,
    ) -> "CommandLineTool":
        """Set the tool's `label` and `doc` in one call.

        Args:
            label (str | None): The `label`, a short title. `None` keeps the current one.
            doc (str | list[str] | None): The `doc`, a longer description; a list is
                written as a list of lines. `None` keeps the current one.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        if label is not None:
            self.label_text = label
        if doc is not None:
            self.doc_text = doc
        return self

    def label(self, text: str) -> "CommandLineTool":
        """Set the tool's `label`, a short title.

        Args:
            text (str): The `label`.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        self.label_text = text
        return self

    def doc(self, text: str | list[str]) -> "CommandLineTool":
        """Set the tool's `doc`, a longer description.

        Args:
            text (str | list[str]): The `doc`; a list is written as a list of lines.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        self.doc_text = text
        return self

    def namespace(self, prefix: str, iri: str | None = None) -> "CommandLineTool":
        """Declare a prefix under `$namespaces`, for names such as `edam:format_1929`.

        Args:
            prefix (str): The prefix. `cwltool` and `edam` are known and need no `iri`.
            iri (str | None): The IRI the prefix stands for.

        Returns:
            CommandLineTool: This tool, for chaining.

        Raises:
            ValueError: If `iri` is not given and `prefix` is not a known prefix.
        """
        namespace_iri = iri if iri is not None else _SUPPORT.known_namespaces.get(prefix)
        if namespace_iri is None:
            raise ValueError(
                f"Unknown namespace prefix {prefix!r}; please provide an explicit iri"
            )
        self._namespaces[prefix] = namespace_iri
        return self

    def schema(self, iri: str) -> "CommandLineTool":
        """Add an ontology to `$schemas`, so the runner can check `format` values against it.

        Args:
            iri (str): The ontology's IRI, or `edam` for the EDAM ontology. An IRI
                already listed is not added twice.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        schema_iri = _SUPPORT.known_schemas.get(iri, iri)
        if schema_iri not in self._schemas:
            self._schemas.append(schema_iri)
        return self

    def edam(self) -> "CommandLineTool":
        """Declare the `edam` prefix and the EDAM ontology, for `format` and `intent` values.

        Writes the `edam` entry of `$namespaces` and the EDAM IRI in `$schemas`.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self.namespace("edam").schema("edam")

    def intent(self, *identifiers: str) -> "CommandLineTool":
        """Add to the tool's `intent`, the operations it performs.

        Args:
            identifiers (str): Operation identifiers, such as `edam:operation_3443`,
                appended to those already given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        self._intent.extend(identifiers)
        return self

    def base_command(self, *parts: str) -> "CommandLineTool":
        """Set the `baseCommand`, the program and the words that always follow it.

        Args:
            parts (str): The command, one word per argument, such as
                `base_command("python", "main.py")`. Replaces an earlier base command.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        self._base_command = list(parts)
        return self

    def stdin(self, value: str) -> "CommandLineTool":
        """Set `stdin`, the file the tool reads on standard input.

        Args:
            value (str): An expression naming a `File` input, such as `$(inputs.reads.path)`.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        self._stdin = value
        return self

    def stdout(self, value: str) -> "CommandLineTool":
        """Set `stdout`, the file the tool's standard output is written to.

        An `Output.stdout()` output is that file. Without this call the runner
        picks a random file name.

        Args:
            value (str): The file name, or an expression for it.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        self._stdout = value
        return self

    def stderr(self, value: str) -> "CommandLineTool":
        """Set `stderr`, the file the tool's standard error is written to.

        An `Output.stderr()` output is that file. Without this call the runner
        picks a random file name.

        Args:
            value (str): The file name, or an expression for it.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        self._stderr = value
        return self

    def add_argument(
        self,
        argument: str | CommandArgument | dict[str, Any],
    ) -> "CommandLineTool":
        """Append one entry to `arguments`, the command-line words that come from no input.

        Args:
            argument (str | CommandArgument | dict[str, Any]): A literal word or an
                expression; a `CommandArgument`; or a raw `CommandLineBinding` mapping,
                written as given after a check and with a `UserWarning`.

        Returns:
            CommandLineTool: This tool, for chaining.

        Raises:
            TypeError: If `argument` is none of these.
            ValueError: If a raw mapping sets `class` or a `$`-prefixed key.
        """
        match argument:
            case str() as literal:
                self._arguments.append(literal)
            case CommandArgument() as structured:
                self._arguments.append(structured.to_cwl())
            case dict() as raw:
                _warn_raw_escape_hatch("add_argument()")
                self._arguments.append(
                    _sanitize_raw_mapping(raw, context="raw argument mapping")
                )
            case _:
                raise TypeError("argument must be a string, CommandArgument, or raw dict")
        return self

    def argument(self, value: Any = None, **kwargs: Any) -> "CommandLineTool":
        """Append one entry to `arguments`, with its `CommandLineBinding` fields as keywords.

        `argument("--verbose")` writes the bare word; `argument("$(runtime.cores)",
        prefix="-j", position=0)` writes a binding.

        Args:
            value (Any): The `valueFrom`: a literal word or an expression.
            position (int | float | None): `position`, the sort key among inputs and arguments.
            prefix (str | None): `prefix`, a flag written before the value.
            separate (bool | None): `separate`; `False` joins the prefix and the value.
            item_separator (str | None): `itemSeparator`, to join an array into one word.
            value_from (str | None): `valueFrom`, used when `value` is not given.
            shell_quote (bool | None): `shellQuote`; `False` needs `shell_command()`.
            binding_extra (dict[str, Any] | None): Further binding fields, written as given.
            extra (dict[str, Any] | None): Further fields of the entry, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        binding_extra = dict(kwargs.pop("binding_extra", {}) or {})
        argument_extra = dict(kwargs.pop("extra", {}) or {})
        binding = CommandLineBinding(extra=binding_extra, **kwargs)
        return self.add_argument(
            CommandArgument(value=value, binding=binding, extra=argument_extra)
        )

    def requirement(self, requirement: Any, value: dict[str, Any] | None = None) -> "CommandLineTool":
        """Add an entry to `requirements`, which the runner must meet or refuse to run the tool.

        A class added again replaces the earlier entry. A class name with the
        `cwltool:` prefix also declares that prefix under `$namespaces`.

        Args:
            requirement (Any): A class name such as `cwltool:CUDARequirement`; a requirement
                object such as `DockerRequirement(docker_pull=...)`; or a raw mapping with a
                `class` key, written as given after a check and with a `UserWarning`.
            value (dict[str, Any] | None): The fields of the entry, when `requirement` is a
                class name.

        Returns:
            CommandLineTool: This tool, for chaining.

        Raises:
            TypeError: If `requirement` is none of these.
            ValueError: If the class name is malformed, or a raw mapping has no `class`.
        """
        self._store_requirement(self._requirements, requirement, value)
        return self

    def hint(self, requirement: Any, value: dict[str, Any] | None = None) -> "CommandLineTool":
        """Add an entry to `hints`, which the runner may use or ignore.

        Takes the same arguments as `requirement()`.

        Args:
            requirement (Any): A class name, a requirement object, or a raw mapping with a
                `class` key.
            value (dict[str, Any] | None): The fields of the entry, when `requirement` is a
                class name.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        self._store_requirement(self._hints, requirement, value)
        return self

    def docker(
        self,
        image: str | None = None,
        *,
        as_hint: bool = False,
        **kwargs: Any,
    ) -> "CommandLineTool":
        """Run the tool in a container: a `DockerRequirement`.

        Args:
            image (str | None): `dockerPull`, the image the runner pulls, such as
                `python:3.12`.
            as_hint (bool): Write it under `hints` instead of `requirements`.
            docker_pull (str | None): `dockerPull`, used instead of `image` when given.
            docker_image_id (str | None): `dockerImageId`, the name the image has
                locally; with only this field the runner does not pull.
            docker_load (str | None): `dockerLoad`, an image archive to load.
            docker_file (str | None): `dockerFile`, a Dockerfile to build the image from.
            docker_import (str | None): `dockerImport`, an image tarball to import.
            docker_output_directory (str | None): `dockerOutputDirectory`, where the tool
                writes its outputs inside the container.
            extra (dict[str, Any] | None): Further `DockerRequirement` fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._apply_spec(
            DockerRequirement(
                docker_pull=kwargs.pop("docker_pull", None) or image,
                extra=dict(kwargs.pop("extra", {}) or {}),
                **kwargs,
            ),
            as_hint=as_hint,
        )

    def inline_javascript(
        self,
        *expression_lib: str,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Allow JavaScript in expressions: an `InlineJavascriptRequirement`.

        `to_cwl_document()` adds a bare one when any field holds `$(` or `${`, so
        call this only to give an `expressionLib` or to write it as a hint.

        Args:
            expression_lib (str): `expressionLib`, JavaScript that every expression can use,
                such as function definitions.
            as_hint (bool): Write it under `hints` instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._apply_spec(
            InlineJavascriptRequirement(list(expression_lib) or None, extra=dict(extra or {})),
            as_hint=as_hint,
        )

    def schema_definitions(
        self,
        *types: Any,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Declare named types: a `SchemaDefRequirement`.

        Args:
            types (Any): `types`, each a named record or enum, such as
                `cwl.record({...}, name="Sample")`.
            as_hint (bool): Write it under `hints` instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._apply_spec(
            SchemaDefRequirement(list(types), extra=dict(extra or {})),
            as_hint=as_hint,
        )

    def load_listing(
        self,
        value: str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Set how much of each `Directory` input is listed: a `LoadListingRequirement`.

        Args:
            value (str): `loadListing`: `no_listing`, `shallow_listing` or `deep_listing`.
            as_hint (bool): Write it under `hints` instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._apply_spec(LoadListingRequirement(value, extra=dict(extra or {})), as_hint=as_hint)

    def shell_command(
        self,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Run the command line through a shell: a `ShellCommandRequirement`.

        Needed for an argument or input binding with `shellQuote: false`, such
        as a pipe or a redirection.

        Args:
            as_hint (bool): Write it under `hints` instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._apply_spec(ShellCommandRequirement(extra=dict(extra or {})), as_hint=as_hint)

    def software(
        self,
        packages: list[SoftwarePackage | dict[str, Any]],
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Name the software the tool needs: a `SoftwareRequirement`.

        Args:
            packages (list[SoftwarePackage | dict[str, Any]]): `packages`, each a
                `SoftwarePackage(package, version=[...], specs=[...])` or its mapping.
            as_hint (bool): Write it under `hints` instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._apply_spec(SoftwareRequirement(packages, extra=dict(extra or {})), as_hint=as_hint)

    def initial_workdir(
        self,
        listing: Any,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Set what is placed in the working directory first: an `InitialWorkDirRequirement`.

        Replaces the whole requirement, including entries added by `stage()`.
        To stage inputs one by one, use `stage()`.

        Args:
            listing (Any): `listing`: a list of `Dirent` objects, `File` or `Directory`
                values and expressions, or one expression that gives the list.
            as_hint (bool): Write it under `hints` instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._apply_spec(InitialWorkDirRequirement(listing, extra=dict(extra or {})), as_hint=as_hint)

    # This helper deliberately bundles the common staging knobs into one call.
    # The slightly wider signature is easier to use than forcing nested objects.
    def stage(  # pylint: disable=too-many-arguments
        self,
        reference: Any,
        *,
        writable: bool = False,
        entryname: str | None = None,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Stage one input in the working directory: an `InitialWorkDirRequirement` entry.

        Appends a `Dirent` with `entry: $(inputs.<name>)` to the requirement's
        `listing`, for a tool that reads or writes its input in place.

        Args:
            reference (Any): The input, as `inputs.<name>` of the `Inputs` the tool was built from.
            writable (bool): `writable`; `True` lets the tool change the staged file
                or directory, which the runner then copies first.
            entryname (str | None): `entryname`, the name in the working directory;
                `$(inputs.<name>.basename)` by default.
            as_hint (bool): Add the entry to the `hints` entry instead of `requirements`.
            extra (dict[str, Any] | None): Further `Dirent` fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.

        Raises:
            TypeError: If `reference` is not a named input, or the listing set by
                `initial_workdir()` is an expression rather than a list.
        """
        return self._append_requirement_entry(
            "InitialWorkDirRequirement",
            "listing",
            Dirent.from_input(
                reference,
                writable=writable,
                entryname=entryname,
                extra=extra,
            ).to_dict(),
            as_hint=as_hint,
        )

    def env_var(self, name: str, value: str, *, as_hint: bool = False) -> "CommandLineTool":
        """Set one environment variable for the tool: an `EnvVarRequirement` `envDef` entry.

        Args:
            name (str): `envName`, the variable's name.
            value (str): `envValue`, its value, or an expression such as `$(inputs.threads)`.
            as_hint (bool): Add the entry to the `hints` entry instead of `requirements`.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._append_requirement_entry(
            "EnvVarRequirement",
            "envDef",
            EnvironmentDef(name, value).to_dict(),
            as_hint=as_hint,
        )

    def resources(
        self,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> "CommandLineTool":
        """Set the cores, memory and disk the tool needs: a `ResourceRequirement`.

        `cores`, `ram`, `tmpdir` and `outdir` are short for the `_min` fields;
        when both spellings are given, the `_min` one is written.

        Args:
            as_hint (bool): Write it under `hints` instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, written as given.
            cores_min (int | float | str | None): `coresMin`, the cores to reserve.
            cores_max (int | float | str | None): `coresMax`.
            ram_min (int | float | str | None): `ramMin`, in mebibytes.
            ram_max (int | float | str | None): `ramMax`, in mebibytes.
            tmpdir_min (int | float | str | None): `tmpdirMin`, temporary disk in mebibytes.
            tmpdir_max (int | float | str | None): `tmpdirMax`, in mebibytes.
            outdir_min (int | float | str | None): `outdirMin`, output disk in mebibytes.
            outdir_max (int | float | str | None): `outdirMax`, in mebibytes.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        cores_min = kwargs.pop("cores_min", None)
        cores = kwargs.pop("cores", None)
        ram_min = kwargs.pop("ram_min", None)
        ram = kwargs.pop("ram", None)
        tmpdir_min = kwargs.pop("tmpdir_min", None)
        tmpdir = kwargs.pop("tmpdir", None)
        outdir_min = kwargs.pop("outdir_min", None)
        outdir = kwargs.pop("outdir", None)
        aliases = {
            "cores_min": cores if cores_min is None else cores_min,
            "ram_min": ram if ram_min is None else ram_min,
            "tmpdir_min": tmpdir if tmpdir_min is None else tmpdir_min,
            "outdir_min": outdir if outdir_min is None else outdir_min,
        }
        aliases.update(kwargs)
        return self._apply_spec(
            ResourceRequirement(extra=dict(extra or {}), **aliases),
            as_hint=as_hint,
        )

    # GPU hints naturally need a few related knobs, so this stays slightly wide.
    def gpu(  # pylint: disable=too-many-arguments
        self,
        *,
        cuda_version_min: str | None = None,
        compute_capability: str | None = None,
        device_count_min: int | str | None = None,
        as_hint: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Ask for NVIDIA GPUs: cwltool's `cwltool:CUDARequirement`, a hint by default.

        Also declares the `cwltool` prefix under `$namespaces`. Runners other
        than cwltool may not know this class; as a hint, they ignore it.

        Args:
            cuda_version_min (str | None): `cudaVersionMin`, such as `"11.7"`.
            compute_capability (str | None): `cudaComputeCapability`, such as `"3.0"`.
            device_count_min (int | str | None): `cudaDeviceCountMin`, the GPUs to reserve.
            as_hint (bool): Write it under `hints` (the default) instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, such as `cudaDeviceCountMax`.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        payload: dict[str, Any] = {}
        _merge_if_set(payload, "cudaVersionMin", cuda_version_min)
        _merge_if_set(payload, "cudaComputeCapability", compute_capability)
        _merge_if_set(payload, "cudaDeviceCountMin", device_count_min)
        payload.update(_render(extra or {}))
        if as_hint:
            return self.hint("cwltool:CUDARequirement", payload)
        return self.requirement("cwltool:CUDARequirement", payload)

    def work_reuse(
        self,
        enable: bool | str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Allow or forbid reusing an earlier run's results: a `WorkReuse`.

        Args:
            enable (bool | str): `enableReuse`, or an expression for it. `False` makes
                the runner run the tool every time, for a tool with side effects.
            as_hint (bool): Write it under `hints` instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._apply_spec(WorkReuse(enable, extra=dict(extra or {})), as_hint=as_hint)

    def network_access(
        self,
        enable: bool | str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Allow or forbid network access while the tool runs: a `NetworkAccess`.

        Without it, a CWL v1.2 runner may cut the tool off from the network.

        Args:
            enable (bool | str): `networkAccess`, or an expression for it.
            as_hint (bool): Write it under `hints` instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._apply_spec(NetworkAccess(enable, extra=dict(extra or {})), as_hint=as_hint)

    def inplace_update(
        self,
        enable: bool = True,
        *,
        as_hint: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Let the tool change a writable staged input in place: an `InplaceUpdateRequirement`.

        Without it, the runner copies a `writable` staged input before the tool
        changes it. Written as a hint by default.

        Args:
            enable (bool): `inplaceUpdate`.
            as_hint (bool): Write it under `hints` (the default) instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._apply_spec(
            InplaceUpdateRequirement(enable, extra=dict(extra or {})),
            as_hint=as_hint,
        )

    def time_limit(
        self,
        seconds: int | str,
        *,
        as_hint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> "CommandLineTool":
        """Stop the tool after a number of seconds: a `ToolTimeLimit`.

        Args:
            seconds (int | str): `timelimit`, in seconds, or an expression for it;
                `0` means no limit.
            as_hint (bool): Write it under `hints` instead of `requirements`.
            extra (dict[str, Any] | None): Further fields, written as given.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        return self._apply_spec(ToolTimeLimit(seconds, extra=dict(extra or {})), as_hint=as_hint)

    def success_codes(self, *codes: int) -> "CommandLineTool":
        """Set `successCodes`, the exit codes that mean the tool succeeded.

        Without it, only `0` is success.

        Args:
            codes (int): The exit codes. Replaces codes given earlier.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        self._success_codes = list(codes)
        return self

    def temporary_fail_codes(self, *codes: int) -> "CommandLineTool":
        """Set `temporaryFailCodes`, the exit codes that mean a run may succeed if retried.

        Args:
            codes (int): The exit codes. Replaces codes given earlier.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        self._temporary_fail_codes = list(codes)
        return self

    def permanent_fail_codes(self, *codes: int) -> "CommandLineTool":
        """Set `permanentFailCodes`, the exit codes that mean a retry will fail too.

        Args:
            codes (int): The exit codes. Replaces codes given earlier.

        Returns:
            CommandLineTool: This tool, for chaining.
        """
        self._permanent_fail_codes = list(codes)
        return self

    def extra(self, **values: Any) -> "CommandLineTool":
        """Write further top-level fields of the document, as given.

        For CWL the builder has no method for, such as `s:author`. It emits a
        `UserWarning`, since nothing checks the fields until `validate()`.

        Args:
            values (Any): The fields, by name.

        Returns:
            CommandLineTool: This tool, for chaining.

        Raises:
            ValueError: If a name is one the builder writes (`inputs`, `requirements`,
                `$namespaces`, ...), `class`, or another `$`-prefixed key.
        """
        _warn_raw_escape_hatch("extra()")
        self._extra.update(
            _sanitize_raw_mapping(
                values,
                context="extra()",
                reserved_keys=set(_SUPPORT.reserved_document_keys),
            )
        )
        return self

    def to_step(
        self,
        *,
        step_name: str | None = None,
        run_path: str | Path | None = None,
        step_inputs: dict[str, Any] | None = None,
        tool_registry: Tools | None = None,
    ) -> "Step":
        """Make a workflow `Step` that runs this tool, without writing a `.cwl` file.

        Args:
            step_name (str | None): The step's name; the tool's `name` by default.
            run_path (str | Path | None): The `.cwl` path the compiler records for the
                tool; `<step_name>.cwl` by default. Nothing is written there.
            step_inputs (dict[str, Any] | None): Input values to bind on the step, by input
                name, as written.
            tool_registry (Tools | None): A tool registry to keep on the step.

        Returns:
            Step: A workflow step backed by this tool's document.
        """
        return _command_line_tool_to_step(
            self,
            step_name=step_name,
            run_path=run_path,
            step_inputs=step_inputs,
            tool_registry=tool_registry,
        )

    def to_cwl_document(self) -> dict[str, Any]:
        """Render the tool as a CWL `CommandLineTool` document.

        `requirements` and `hints` are written as lists, a one-word base command
        as a string. When any field holds an expression (`$(` or `${`) and no
        `InlineJavascriptRequirement` is declared, one is added to `requirements`.

        Returns:
            dict[str, Any]: The document, ready to dump as YAML or JSON.
        """
        requirements = [{"class": name, **payload} for name, payload in self._requirements.items()]
        hints = [{"class": name, **payload} for name, payload in self._hints.items()]
        base_command = (
            self._base_command[0] if len(self._base_command) == 1 else list(self._base_command)
        ) if self._base_command else None
        clt = _cwl.CommandLineTool(
            id=self.name,
            cwlVersion=self.cwl_version,
            inputs=self.inputs.to_dict(),
            outputs=self.outputs.to_dict(),
            label=self.label_text,
            doc=_render_doc(self.doc_text),
            intent=list(self._intent) or None,
            baseCommand=base_command,
            arguments=list(self._arguments) or None,
            requirements=requirements or None,
            hints=hints or None,
            stdin=self._stdin,
            stdout=self._stdout,
            stderr=self._stderr,
            successCodes=list(self._success_codes) or None,
            temporaryFailCodes=list(self._temporary_fail_codes) or None,
            permanentFailCodes=list(self._permanent_fail_codes) or None,
        )
        document: dict[str, Any] = clt.save(top=True, relative_uris=False)
        if self._namespaces:
            document["$namespaces"] = dict(self._namespaces)
        if self._schemas:
            document["$schemas"] = list(self._schemas)
        document.update(_render(self._extra))
        if _contains_expression(document):
            requirements = document.setdefault("requirements", [])
            has_inline_js = any(
                item.get("class") == "InlineJavascriptRequirement"
                for item in [*requirements, *document.get("hints", [])]
            )
            if not has_inline_js:
                requirements.append({"class": "InlineJavascriptRequirement"})
        return document

    def to_cwl_yaml(self) -> str:
        """Render the tool as CWL YAML text, the document of `to_cwl_document()`.

        Returns:
            str: The YAML text.
        """
        return dump_wic_yaml(self.to_cwl_document())

    def write_cwl(self, path: str | Path, *, validate: bool = False, skip_schemas: bool = False) -> Path:
        """Write the tool as a `.cwl` file, creating its parent directories.

        Args:
            path (str | Path): The file to write.
            validate (bool): Validate the written file with cwltool, as `validate()` does.
            skip_schemas (bool): When validating, do not load the ontologies under
                `$schemas`, so no network access is needed.

        Returns:
            Path: The path written.

        Raises:
            ToolBuilderValidationError: If `validate` is set and the file is not a valid tool.
        """
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(self.to_cwl_yaml(), encoding="utf-8")
        if validate:
            _validate_path(output_path, skip_schemas=skip_schemas)
        return output_path

    def validate(self, *, skip_schemas: bool = False) -> ValidationResult:
        """Validate the tool with cwltool, as a runner would load it, without writing it.

        Args:
            skip_schemas (bool): Do not load the ontologies under `$schemas`, so no
                network access is needed.

        Returns:
            ValidationResult: The loaded tool, for inspection.

        Raises:
            ToolBuilderValidationError: If the document is not a valid CWL v1.2
                `CommandLineTool`; the cause names the field.
        """
        return validate_cwl_document(
            self.to_cwl_document(),
            filename=f"{self.name}.cwl",
            skip_schemas=skip_schemas,
        )


__all__ = [
    "ToolBuilderValidationError",
    "CommandArgument",
    "CommandLineBinding",
    "CommandLineTool",
    "CommandOutputBinding",
    "Dirent",
    "DockerRequirement",
    "EnvironmentDef",
    "EnvVarRequirement",
    "Field",
    "Fields",
    "FieldSpec",
    "InitialWorkDirRequirement",
    "InlineJavascriptRequirement",
    "InplaceUpdateRequirement",
    "Input",
    "InputSpec",
    "Inputs",
    "LoadListingRequirement",
    "NetworkAccess",
    "Output",
    "OutputSpec",
    "Outputs",
    "ResourceRequirement",
    "SchemaDefRequirement",
    "SecondaryFile",
    "ShellCommandRequirement",
    "SoftwarePackage",
    "SoftwareRequirement",
    "ToolTimeLimit",
    "ValidationResult",
    "WorkReuse",
    "cwl",
    "secondary_file",
    "validate_cwl_document",
]
