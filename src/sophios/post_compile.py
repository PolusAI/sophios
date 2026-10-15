from pathlib import PurePosixPath
import sys
import copy
from dataclasses import dataclass, replace
from typing import Any, Final
import docker
import podman
from podman.domain.images_build import BuildMixin
from . import plugins
from .wic_types import Cwl, Yaml
from .ir.artifacts import CompilationArtifact
from .ir.names import NAMESPACE_SEPARATOR
from .lang.cwl import CwlVersion
from .lang.diagnostics import SophiosError
from .lang.error_codes import SophiosErrorCode


#: Fields that belong to a CWL *document* rather than to a process. An embedded
#: process is not a document, so each has to leave the `run:` it is embedded
#: into -- either by moving up to the document that now contains it, or by
#: being dropped because that document already states it.
DOCUMENT_FIELDS = ('$namespaces', '$schemas', 'cwlVersion')

#: What a document written for an older CWL version is given by the runner that
#: loads it as a file, and loses once it is embedded in a newer document:
#: cwltool upgrades a v1.0 file by adding these two hints (`cwltool/update.py`,
#: `v1_0to1_1`) and adds nothing for v1.1. Keyed by every `CwlVersion`, so
#: admitting a new version means deciding what it implies.
IMPLIED_BY_VERSION: Final[dict[str, Cwl]] = {
    CwlVersion.V1_0.value: {'LoadListingRequirement': {'loadListing': 'deep_listing'},
                            'NetworkAccess': {'networkAccess': True}},
    CwlVersion.V1_1.value: {},
    CwlVersion.V1_2.value: {},
}


#: The classes cwltool's v1.0 updater (`rewrite` in `cwltool/update.py`) renames to a plain name when
#: a document declares them under cwltool's namespace, in the two spellings a document can use. Only the
#: two that `IMPLIED_BY_VERSION` adds are listed.
CWLTOOL_RENAMES: Final[dict[str, str]] = {
    f'{namespace}{name}': name
    for namespace in ('cwltool:', 'http://commonwl.org/cwltool#')
    for name in ('LoadListingRequirement', 'NetworkAccess')
}


def _renamed(section: Any) -> Any:
    """A `requirements:` or `hints:` section, in either CWL form, with `CWLTOOL_RENAMES` applied.

    Embedded, nothing updates the tool, and the plain class is the only one the runner of the
    newer document looks for.
    """
    match section:
        case dict():
            return {CWLTOOL_RENAMES.get(name, name): body for name, body in section.items()}
        case list():
            return [{**item, 'class': CWLTOOL_RENAMES.get(item['class'], item['class'])}
                    if isinstance(item, dict) and 'class' in item else item for item in section]
    return section


def _classes(section: Any) -> set[str]:
    """The requirement classes a `requirements:` or `hints:` section names, in either CWL form."""
    match section:
        case dict():
            return set(section)
        case list():
            return {item['class'] for item in section if isinstance(item, dict) and 'class' in item}
    return set()


def _keeping_version_defaults(process: Cwl, origin: str) -> Cwl:
    """`process` with the defaults its own `cwlVersion` implied, written out as hints.

    Embedding drops `cwlVersion`, so the document it lands in decides what the
    process means. A requirement or hint the process declares is left as it is,
    except that a v1.0 process's cwltool-namespaced spelling of an implied class
    is renamed to the plain one, as cwltool does when it loads the file.

    Args:
        process (Cwl): A process about to be embedded in a newer document.
        origin (str): Where it came from, for the error.

    Raises:
        SophiosError: `wic035` if the process declares no `cwlVersion`, or one Sophios does not embed.

    Returns:
        Cwl: `process`, with `hints` extended when its version implied more.
    """
    version = process.get('cwlVersion')
    if version not in IMPLIED_BY_VERSION:
        declared_version = 'declares no cwlVersion' if version is None else f'declares cwlVersion {version!r}'
        raise SophiosError.error(
            SophiosErrorCode.UNSUPPORTED_CWL_VERSION,
            f'{origin} {declared_version}, so it cannot be embedded; '
            f'Sophios embeds {", ".join(IMPLIED_BY_VERSION)}.')
    if version == CwlVersion.V1_0.value:
        process = {**process, **{section: _renamed(process[section])
                                 for section in ('hints', 'requirements') if section in process}}
    declared = _classes(process.get('hints')) | _classes(process.get('requirements'))
    implied = {name: copy.deepcopy(body) for name, body in IMPLIED_BY_VERSION[version].items()
               if name not in declared}
    if not implied:
        return process
    hints = process.get('hints')
    if isinstance(hints, list):
        return {**process, 'hints': [{'class': name, **body} for name, body in implied.items()] + hints}
    return {**process, 'hints': {**implied, **(hints or {})}}


def _parameter(parameter: Any) -> Any:
    """`parameter`, with a single `secondaryFiles` pattern written as a one-element list."""
    if not isinstance(parameter, dict) or isinstance(parameter.get('secondaryFiles', []), list):
        return parameter
    return {**parameter, 'secondaryFiles': [parameter['secondaryFiles']]}


def _secondary_files_as_lists(process: Cwl) -> Cwl:
    """`process`, with every input's and output's `secondaryFiles` written as a list.

    A single pattern, a string or a mapping, means the same as a list holding it.
    cwltool's v1.0 updater writes the list, but an embedded process is not updated:
    the single pattern reaches cwltool's checker, which indexes it as a list.

    Args:
        process (Cwl): A process about to be embedded.

    Returns:
        Cwl: `process`, with its `inputs` and `outputs` rewritten where they held a single pattern.
    """
    sections: Cwl = {}
    for name in ('inputs', 'outputs'):
        match process.get(name):
            case dict() as ports:
                sections[name] = {port_id: _parameter(port) for port_id, port in ports.items()}
            case list() as ports:
                sections[name] = [_parameter(port) for port in ports]
    return {**process, **sections}


#: The requirement classes the workflow engine reads against the document that
#: holds a step's `when`, `valueFrom`, `scatter` and links, not against the
#: step. Flattening hoists them to the root document; every other requirement
#: moves onto the steps it applied to.
DOCUMENT_FEATURES: Final = ('InlineJavascriptRequirement', 'ScatterFeatureRequirement',
                            'StepInputExpressionRequirement', 'MultipleInputFeatureRequirement')

#: The keys a call step has when its author wrote nothing on it but `in` and `out`.
_PLAIN_CALL_KEYS: Final = frozenset({'id', 'in', 'run', 'out'})


def flat_step_id(call: str, inner: str) -> str:
    """The id a step takes when the subworkflow call `call` is dissolved into its steps.

    The spelling the nested document already gives a step as a qualified name
    (`Names.qualified`): where the step came from stays in its id.
    """
    return f'{call}{NAMESPACE_SEPARATOR}{inner}'


def _source_of(value: Any) -> str | None:
    """The one source an `in:` entry names, in either spelling Sophios writes; None otherwise."""
    match value:
        case str():
            return value
        case {'source': str() as source}:
            return source
    return None


def _with_source(value: Any, source: str) -> Any:
    """`value`, an `in:` entry, naming `source` instead, in the spelling it had."""
    return source if isinstance(value, str) else {**value, 'source': source}


class _Stays(Exception):
    """A call that cannot be dissolved without changing what the workflow means, and why."""


@dataclass(frozen=True, slots=True)
class _Call:
    """A step that runs a workflow, the document it sits in, and the (already flat) workflow it runs."""

    document: CompilationArtifact
    index: int
    step: Yaml
    inner: CompilationArtifact

    @property
    def id(self) -> str:
        """The call step's id in the document it sits in."""
        return str(self.step['id'])

    def announce(self, reason: str) -> None:
        """Say on stderr, in one plain line, that this call stays a subworkflow and why."""
        node = self.document.graph.steps[self.index] if self.document.graph is not None else None
        where = f'{node.span}: ' if node is not None and node.span is not None else ''
        name = node.id.name if node is not None else self.id
        print(f"Warning! {where}step '{name}' stays a subworkflow under --cwl_inline_subworkflows: {reason}.",
              file=sys.stderr)

    def written_keys(self) -> list[str]:
        """The keys on the emitted call besides `in` and `out`.

        A `when` counts whoever wrote it: under `--partial_failure_enable` the
        compiler's own is the call's gate over all its inputs, which the steps
        inside do not carry between them.
        """
        return sorted(set(self.step) - _PLAIN_CALL_KEYS)

    def dissolvable(self) -> None:
        """Raise `_Stays` unless the call's boundary can be removed without changing meaning."""
        carried = self.written_keys()
        if carried:
            raise _Stays('it carries ' + ', '.join(f'`{key}`' for key in carried))
        if self.inner.graph is not None and not self.inner.graph.inlineable:
            raise _Stays('it is marked `wic: inlineable: false`')
        for name, value in self.step.get('in', {}).items():
            if _source_of(value) is None or (isinstance(value, dict) and len(value) > 1):
                raise _Stays(f"input '{name}' is bound to {value!r}, not to a single source")
        for name, parameter in self.inner.cwl.get('inputs', {}).items():
            if isinstance(parameter, dict) and 'default' in parameter:
                raise _Stays(f"its input '{name}' has a default, which a flat step cannot take")

    def classes(self, section: Any, where: str) -> Cwl:
        """A `requirements:` or `hints:` section as a mapping from class to body.

        Raises:
            _Stays: If it is written as a list, which flattening does not read.
        """
        if section is None or isinstance(section, dict):
            return dict(section or {})
        raise _Stays(f'{where} are written as a list, which flattening does not read')

    def moved_requirements(self, root: Cwl) -> tuple[Cwl, Cwl]:
        """What the workflow the call runs required and hinted, for the steps now under it no longer.

        The classes in `DOCUMENT_FEATURES` are put in `root` (the root's requirements, which
        this updates); the rest are returned for each step, with the hints.

        Raises:
            _Stays: If a feature is declared differently from how `root` has it.
        """
        required = self.classes(self.inner.cwl.get('requirements'), "its workflow's requirements")
        required.pop('SubworkflowFeatureRequirement', None)
        for name in DOCUMENT_FEATURES:
            if name in required:
                body = required.pop(name)
                if root.setdefault(name, body) != body:
                    raise _Stays(f'it declares a different {name} from its caller')
        return required, self.classes(self.inner.cwl.get('hints'), "its workflow's hints")

    def rewired(self, inner_step: Yaml, bound: dict[str, str]) -> Yaml:
        """`inner_step` as a step of the document holding the call: its id, and where its inputs come from."""
        flat = copy.deepcopy(inner_step)
        flat['id'] = flat_step_id(self.id, inner_step['id'])
        flat['in'] = {}
        produced = {step['id'] for step in self.inner.cwl['steps']}
        for name, value in inner_step.get('in', {}).items():
            source = _source_of(value)
            if source is None:
                if isinstance(value, dict) and 'source' not in value:
                    flat['in'][name] = value
                    continue
                raise _Stays(f"its step '{inner_step['id']}' wires input '{name}' from {value!r}")
            producer, _, port = source.partition('/')
            if source in self.inner.cwl.get('inputs', {}):
                if source in bound:
                    flat['in'][name] = _with_source(value, bound[source])
                elif isinstance(value, dict) and len(value) > 1:
                    # Unbound, the source yields null; what else the entry says still applies.
                    flat['in'][name] = {key: item for key, item in value.items() if key != 'source'}
            elif producer in produced and port:
                flat['in'][name] = _with_source(value, f'{flat_step_id(self.id, producer)}/{port}')
            else:
                raise _Stays(f"its step '{inner_step['id']}' reads '{source}', which is neither "
                             'an input of the subworkflow nor an output of one of its steps')
        return flat

    def redirects(self) -> dict[str, str]:
        """For each output of the workflow the call ran, the output of the flat step that now makes it."""
        produced = {step['id'] for step in self.inner.cwl['steps']}
        found: dict[str, str] = {}
        for name, output in self.inner.cwl.get('outputs', {}).items():
            source = output.get('outputSource') if isinstance(output, dict) else None
            producer, _, port = source.partition('/') if isinstance(source, str) else ('', '', '')
            if producer not in produced or not port:
                raise _Stays(f"its output '{name}' is not the output of one of its steps")
            found[f'{self.id}/{name}'] = f'{flat_step_id(self.id, producer)}/{port}'
        return found

    def dissolved(self, root: Cwl) -> tuple[list[Yaml], list[CompilationArtifact], dict[str, str]]:
        """The steps (and their tool artifacts) that replace the call, wired as the call wired them.

        Raises:
            _Stays: If the call cannot be dissolved. `root` is then left as it was.
        """
        self.dissolvable()
        bound = {name: str(_source_of(value)) for name, value in self.step.get('in', {}).items()}
        trial = dict(root)
        required, hinted = self.moved_requirements(trial)
        steps: list[Yaml] = []
        children: list[CompilationArtifact] = []
        for inner_step, inner_child in zip(self.inner.cwl['steps'], self.inner.children):
            flat = self.rewired(inner_step, bound)
            for key, moved in (('requirements', required), ('hints', hinted)):
                if combined := moved | self.classes(inner_step.get(key), f"step '{inner_step['id']}' {key}"):
                    flat[key] = combined
            if isinstance(flat['run'], str):
                if PurePosixPath(flat['run']).parent.name != inner_child.namespace[-1]:
                    raise ValueError(f"{flat['run']} is not in the relative layout flattening needs")
                flat['run'] = f"{flat['id']}/{PurePosixPath(flat['run']).name}"
                inner_child = replace(inner_child, namespace=(flat['id'],))
            steps.append(flat)
            children.append(inner_child)
        redirects = self.redirects()
        root.update(trial)
        return steps, children, redirects


def _redirect(cwl: Yaml, redirects: dict[str, str]) -> None:
    """Point each source and `outputSource` in `cwl` that named a dissolved call's output at the flat step."""
    for step in cwl['steps']:
        for name, value in step.get('in', {}).items():
            match value:
                case {'source': [*sources]}:
                    step['in'][name] = {**value, 'source': [redirects.get(source, source) for source in sources]}
                case _:
                    source = _source_of(value)
                    if source in redirects:
                        step['in'][name] = _with_source(value, redirects[source])
    for output in cwl.get('outputs', {}).values():
        if output.get('outputSource') in redirects:
            output['outputSource'] = redirects[output['outputSource']]


# pylint: disable-next=too-many-locals
def flatten_subworkflows(artifact: CompilationArtifact) -> CompilationArtifact:
    """Replace every step that runs a workflow by that workflow's own steps, where that changes nothing.

    The result has one flat workflow in place of each call it could dissolve,
    recursively. The root's inputs, outputs and job inputs are untouched, so the
    flat form is run with the same inputs and returns the same outputs as the
    nested one. Each dissolved call's inner steps take `flat_step_id(call,
    inner)`, run the same tool files from `<id>/<tool>.cwl`, and are wired
    producer to consumer. What the workflow the call ran required or hinted
    moves with the steps: the classes in `DOCUMENT_FEATURES` to the root, the
    rest onto each step, where the step's own entry wins.

    A call stays a nested subworkflow step, and one stderr line says why, when
    it carries anything besides `in` and `out` (`scatter`, `when`, even the
    one `--partial_failure_enable` adds, `requirements`...), when it is marked `wic: inlineable: false`, or when
    the boundary cannot be removed without guessing: an input bound to anything
    but one source, an input with a default, requirements or hints written as a
    list, a document feature declared differently from the caller's, a source
    that names neither an input nor a step.

    Args:
        artifact (CompilationArtifact): A compiled artifact tree, written with
            the relative `run:` layout the CLI uses.

    Returns:
        CompilationArtifact: The same workflow with every dissolvable call replaced
            by its steps; tools are still separate files.
    """
    if artifact.cwl.get('class') != 'Workflow' or all(
            child.cwl.get('class') != 'Workflow' for child in artifact.children):
        return artifact
    cwl = copy.deepcopy(artifact.cwl)
    required = cwl.get('requirements')
    root: Cwl | None = dict(required or {}) if required is None or isinstance(required, dict) else None
    steps: list[Yaml] = []
    children: list[CompilationArtifact] = []
    redirects: dict[str, str] = {}
    dissolved_any = False
    for index, (step, child) in enumerate(zip(cwl['steps'], artifact.children)):
        if child.cwl.get('class') != 'Workflow':
            steps.append(step)
            children.append(child)
            continue
        call = _Call(artifact, index, step, flatten_subworkflows(child))
        try:
            if root is None:
                raise _Stays("the workflow's own requirements are written as a list, which flattening does not read")
            dissolved_steps, dissolved_children, found = call.dissolved(root)
        except _Stays as stays:
            call.announce(str(stays))
            steps.append(step)
            children.append(call.inner)
            continue
        dissolved_any = True
        steps += dissolved_steps
        children += dissolved_children
        redirects |= found
        cwl['$namespaces'] = cwl.get('$namespaces', {}) | call.inner.cwl.get('$namespaces', {})
        cwl['$schemas'] = list(dict.fromkeys(
            list(cwl.get('$schemas', [])) + list(call.inner.cwl.get('$schemas', []))))
    if not dissolved_any:
        return replace(artifact, children=tuple(children))
    cwl['steps'] = steps
    _redirect(cwl, redirects)
    assert root is not None, 'a dissolved call has read the root requirements'
    if all(child.cwl.get('class') != 'Workflow' for child in children):
        root.pop('SubworkflowFeatureRequirement', None)
    for key, value in (('requirements', root), ('$schemas', cwl.get('$schemas'))):
        if value:
            cwl[key] = value
        else:
            cwl.pop(key, None)
    return replace(artifact, cwl=cwl, children=tuple(children))


def inline_artifact_runs(artifact: CompilationArtifact) -> CompilationArtifact:
    """Embed every emitted child in its parent's ``run`` field."""
    children = tuple(inline_artifact_runs(child) for child in artifact.children)
    cwl = copy.deepcopy(artifact.cwl)
    if cwl.get('class') == 'Workflow':
        for child in children:
            step_id = child.namespace[-1]
            step = next(item for item in cwl['steps'] if item.get('id') == step_id)
            # An embedded process has no location of its own, so a relative
            # `$include` would resolve against whichever document embeds it.
            # Its own version's defaults and the list form of its secondary
            # files are written out first, because dropping `cwlVersion` below
            # takes the upgrade cwltool would have made with it.
            step['run'] = _secondary_files_as_lists(_keeping_version_defaults(
                plugins.cwl_prepend_dockerFile_include_path(child.cwl, child.run_path), child.run_path))
            # A prefix and an ontology must be declared in the document that
            # uses them, so these move up. `cwlVersion` is dropped instead:
            # the parent already names one, and a second on an embedded
            # process is resolved as a reference and fails validation -- which
            # is what a tool declaring `v1.0` did to every inlined corpus
            # workflow, in a lane that runs weekly.
            cwl['$namespaces'] = cwl.get('$namespaces', {}) | step['run'].get(
                '$namespaces', {})
            cwl['$schemas'] = list(dict.fromkeys(
                list(cwl.get('$schemas', [])) + list(step['run'].get('$schemas', []))))
            if not cwl['$schemas']:
                cwl.pop('$schemas')
            for field in DOCUMENT_FIELDS:
                step['run'].pop(field, None)
    return replace(artifact, cwl=cwl, children=children)


def apply_inline_options(artifact: CompilationArtifact, *,
                         subworkflows: bool, runtag: bool) -> CompilationArtifact:
    """Apply what `--cwl_inline_subworkflows` and `--cwl_inline_runtag` ask for.

    Orthogonal: the first changes the shape (flat wherever a call can be
    dissolved), the second the packaging (every `run:` carries its process).
    Shape first, so both together give one self-contained file, flat wherever
    it could be.
    """
    if subworkflows:
        artifact = flatten_subworkflows(artifact)
    if runtag:
        artifact = inline_artifact_runs(artifact)
    return artifact


def remove_artifact_entrypoints(container_engine: str,
                                artifact: CompilationArtifact) -> CompilationArtifact:
    """Build no-entrypoint images and rewrite the immutable artifact tree."""
    if container_engine == 'docker':
        client = docker.from_env()  # type: ignore
        plugins.remove_entrypoints(client, client.images)
    elif container_engine == 'podman':
        # See https://github.com/containers/podman-py?tab=readme-ov-file#example-usage
        uri = "unix:///run/user/1000/podman/podman.sock"
        with podman.PodmanClient(base_url=uri) as client:
            plugins.remove_entrypoints(client, BuildMixin())
    return plugins.dockerPull_append_noentrypoint_artifact(artifact)
