"""The corpus: every workflow `search_paths_wic` reaches. Each compiles clean in every form the inline flags give,
cwltool reads every form as it is written, and the run lanes run each by the name of the test that runs it. The corpus
checks run over all but the large workflows on a pull request, and over the large ones weekly, as the items named
`weekly_corpus-<stem>`."""
import json
from collections import Counter
# pylint: disable=redefined-outer-name
from dataclasses import replace
from pathlib import Path
import argparse
from typing import Any, Final

import pytest
from mergedeep import merge, Strategy

import sophios.cli
import sophios.compiler
import sophios.input_output
from sophios.runtime import local
import sophios.utils
from sophios.ir import frontdoor
from sophios import plugins, post_compile, targets
from sophios.runtime import backends, realtime, setup, runners
from sophios.cli import get_args
from sophios.runtime.runners import RunSettings
from sophios.utils_yaml import Key
from sophios.post_compile import apply_inline_options, inline_artifact_runs
from sophios.lang import parse
from sophios.lang.diagnostics import SophiosError
from sophios.lang.error_codes import SophiosErrorCode
from sophios.ir.artifacts import CompilationArtifact, CompilationResult
from sophios.ir.names import Names
from sophios.ir.types import WorkflowGraph
from sophios.wic_types import Json

# pylint: disable-next=unused-import  # `corpus_registry` is a pytest fixture
from .test_setup import CorpusRegistry, corpus_config, corpus_registry, workflow_paths
from .helpers import python_workflow
from .cwl_validation import validate_cwl
from .equivalence import Strength, equivalent, flatten_model
from .hermetic import cwl_tree

yml_paths = workflow_paths()

# Each directory of search_paths_wic may hold a config_ci.json; their lists concatenate. `//` lines are comments,
# which JSON has none of.
config_ci: Json = {}
for yml_dir in (directory for directories in corpus_config()['search_paths_wic'].values() for directory in directories):
    if (config_ci_json := Path(yml_dir) / 'config_ci.json').exists():
        lines = config_ci_json.read_text(encoding='utf-8').splitlines()
        config_ci = merge(config_ci, json.loads('\n'.join(line for line in lines if not line.strip().startswith('//'))),
                          strategy=Strategy.TYPESAFE_ADDITIVE)

# Each stops with an error on purpose, which the page showing it explains, so no lane compiles or runs it.
refused: list[str] = config_ci.get("refused", [])
yml_paths_tuples = [(name, path) for paths in yml_paths.values() for name, path in paths.items()
                    if name not in refused]

# Too large to compile-check on every pull request.
large_workflows: list[str] = config_ci.get("large_workflows", [])


def _is_workflow_document(yml_path: Path) -> bool:
    """Whether the file has steps or a `wic: implementations:` block, or is a tool, which compiles as the root of one
    step, as `parse` reads it (not `read_wic`: an includer fragment carries diagnostics and is still a workflow
    document)."""
    parsed = parse(yml_path.read_text(encoding='utf-8'), str(yml_path))
    document = parsed.document
    if document is None:
        return parsed.tool is not None
    return (bool(document.steps) or document.steps_as_mapping
            or (document.sidecar is not None and 'implementations' in dict(document.sidecar.entries)))


yml_paths_tuples_not_large = [(s, p) for (s, p) in yml_paths_tuples
                              if s not in large_workflows and _is_workflow_document(p)]

# Library workflows: each reads an input or an edge that a parent workflow supplies,
# so none of them runs alone ("Missing required input parameter").
fragments: list[str] = config_ci.get("fragments", [])
# Skipped on every push; the weekly lane runs every one of them.
intensive: list[str] = config_ci.get("intensive", [])
run_partial_failures: list[str] = config_ci.get("run_partial_failures", [])

yml_paths_tuples_weekly = [(s, p) for (s, p) in yml_paths_tuples if s in intensive]

yml_paths_tuples_on_push = [(s, p) for (s, p) in yml_paths_tuples
                            if s not in fragments and s not in intensive and s not in run_partial_failures]

yml_paths_partial_failure = [(s, p) for (s, p) in yml_paths_tuples if s in run_partial_failures]

#: The Python workflows discovered beside the corpus, but for mm-workflows' scripts, which are tools, and the
#: cookbook's, which test_cookbook.py runs.
PYTHON_WORKFLOWS: Final = [pytest.param(path, id=path.name)
                           for paths in plugins.get_workflow_paths(corpus_config(), 'py').values()
                           for path in paths.values() if not {'mm-workflows', 'cookbook'} & set(path.parts)]


def _artifacts(root: CompilationArtifact) -> list[CompilationArtifact]:
    """Return a stable depth-first view of an emitted artifact tree."""
    return [root, *(item for child in root.children for item in _artifacts(child))]


#: What the corpus checks run over: on a pull request every workflow but the large ones; weekly (`-k weekly_corpus`)
#: the large ones.
CHECKED: Final = [*(pytest.param(s, p, id=s) for s, p in yml_paths_tuples_not_large),
                  *(pytest.param(s, p, id=f'weekly_corpus-{s}') for s, p in yml_paths_tuples if s in large_workflows)]


@pytest.mark.cwl_corpus
@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_on_push + yml_paths_partial_failure)
def test_run_workflows_on_push(yml_path_str: str, yml_path: Path, cwl_runner: str,
                               corpus_registry: CorpusRegistry) -> None:
    """Runs every workflow auto-discovered from the directories in 'search_paths_wic',
       except the fragments and intensive workflows that the config_ci.json files list;
       those listed as partial failures run with --partial_failure_enable."""
    args = get_args(str(yml_path), ['--partial_failure_enable'] if yml_path_str in run_partial_failures else [])
    run_workflows(yml_path_str, yml_path, cwl_runner, args, corpus_registry)


@pytest.mark.cwl_corpus
@pytest.mark.parametrize('path', PYTHON_WORKFLOWS)
def test_corpus_python_workflow_compiles(path: Path, corpus_registry: CorpusRegistry) -> None:
    """Each discovered Python workflow compiles, on every leg; the run lane runs it below."""
    python_workflow(path, corpus_registry.tools).compile()


@pytest.mark.cwl_corpus
@pytest.mark.skip_pypi_ci
@pytest.mark.slow
@pytest.mark.parametrize('path', PYTHON_WORKFLOWS)
def test_run_workflows_on_push_from_python(path: Path, cwl_runner: str, corpus_registry: CorpusRegistry,
                                           tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each discovered Python workflow runs as its script runs it, by `Workflow.run`, in a directory of its own."""
    monkeypatch.chdir(tmp_path)
    python_workflow(path, corpus_registry.tools).run(runner=runners.named('cwl', cwl_runner)())


#: The three forms the inline flags give, each of which must run to the same
#: results as the default one: the shape alone, the packaging alone, and both.
INLINE_FORMS: Final = [
    pytest.param('flat', ['--cwl_inline_subworkflows'], id='flat'),
    pytest.param('embedded', ['--cwl_inline_runtag'], id='embedded'),
    pytest.param('flat_embedded', ['--cwl_inline_subworkflows', '--cwl_inline_runtag'], id='flat_embedded'),
]


def _calls_a_workflow(yml_path: Path) -> bool:
    """Whether `yml_path` calls a subworkflow: only then can `--cwl_inline_subworkflows` change it."""
    return bool(frontdoor.bundle_from_disk(yml_path, yml_paths, {}).registry.workflows)


#: Each workflow in each inline form that can differ from its other forms: without a call,
#: `flat` is the default form and `flat_embedded` is `embedded` (pinned below).
INLINE_CASES: Final = [pytest.param(name, path, *form.values, id=f'{name}-{form.id}')
                       for name, path in yml_paths_tuples_on_push for form in INLINE_FORMS
                       if form.id not in ('flat', 'flat_embedded') or _calls_a_workflow(path)]


@pytest.mark.cwl_corpus
@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path, form, flags", INLINE_CASES)
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_run_inlined_workflows_on_push(yml_path_str: str, yml_path: Path, form: str, flags: list[str],
                                       cwl_runner: str, corpus_registry: CorpusRegistry) -> None:
    """Runs every workflow auto-discovered from the directories in 'search_paths_wic',
       except the fragments, intensive workflows and partial failures that the
       config_ci.json files list, in each form the inline flags give."""
    args = get_args(str(yml_path), flags)
    run_workflows(yml_path_str, yml_path, cwl_runner, args, corpus_registry,
                  basepath=f'autogenerated/inline_{form}')


@pytest.mark.cwl_corpus
@pytest.mark.slow
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_weekly)
def test_run_workflows_weekly(yml_path_str: str, yml_path: Path, cwl_runner: str,
                              corpus_registry: CorpusRegistry) -> None:
    """Runs every intensive workflow the config_ci.json files list."""
    args = get_args(str(yml_path))
    run_workflows(yml_path_str, yml_path, cwl_runner, args, corpus_registry)


#: The one inline form the weekly workflows run in. A weekly run is a full-scale
#: stress run, and the flat and embedded forms are each checked at compile time
#: by the corpus checks below; `flat_embedded` stacks both transforms, so it is
#: the form a run adds the most to.
WEEKLY_INLINE_FORMS: Final = [form for form in INLINE_FORMS if form.id == 'flat_embedded']


@pytest.mark.cwl_corpus
@pytest.mark.slow
@pytest.mark.parametrize("form, flags", WEEKLY_INLINE_FORMS)
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_weekly)
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_run_inlined_workflows_weekly(yml_path_str: str, yml_path: Path, form: str, flags: list[str],
                                      cwl_runner: str, corpus_registry: CorpusRegistry) -> None:
    """Runs every intensive workflow the config_ci.json files list, in the inline
       form that stacks both transforms."""
    args = get_args(str(yml_path), flags)
    run_workflows(yml_path_str, yml_path, cwl_runner, args, corpus_registry,
                  basepath=f'autogenerated/inline_{form}')


@pytest.mark.cwl_corpus
@pytest.mark.parametrize("yml_path_str, yml_path", yml_paths_tuples_on_push)
def test_cwl_docker_extract(yml_path_str: str, yml_path: Path,
                            corpus_registry: CorpusRegistry) -> None:
    """ Uses cwl-docker-extract to recursively `docker pull`"""
    args = get_args(str(yml_path))
    run_workflows(yml_path_str, yml_path, 'cwltool', args, corpus_registry, docker_pull_only=True)


def _compiled(yml_path: Path, args: argparse.Namespace, registry: CorpusRegistry) -> CompilationResult:
    """`yml_path` compiled for `args`, before the inline flags shape its artifact tree."""
    bundle = frontdoor.bundle_from_disk(Path(yml_path), yml_paths, registry.tools)
    settings, _graph_settings = sophios.cli.settings_from_args(args)
    return sophios.compiler.compile_source(bundle, settings)


def _inlined(artifact: CompilationArtifact, args: argparse.Namespace) -> CompilationArtifact:
    """`artifact`'s tree as the inline flags in `args` shape it: what `run_workflows` writes and runs."""
    return apply_inline_options(
        artifact, subworkflows=args.cwl_inline_subworkflows, runtag=args.cwl_inline_runtag)[0]


# pylint: disable-next=too-many-arguments,too-many-locals
def run_workflows(
    yml_path_str: str,
    yml_path: Path,
    cwl_runner: str,
    args: argparse.Namespace,
    registry: CorpusRegistry,
    *,
    basepath: str = 'autogenerated',
    docker_pull_only: bool = False,
) -> None:
    """Compile, prepare and run one corpus workflow; `basepath` keeps each lane's and inline form's artifacts apart,
    since an artifact is named for its workflow and the weekly job runs several steps in one directory."""
    Path(basepath).mkdir(parents=True, exist_ok=True)
    compilation = _compiled(yml_path, args, registry)
    run = sophios.cli.run_settings_from_args(argparse.Namespace(**vars(args) | {'cwl_runner': cwl_runner}), [])
    backend = targets.backend(run.runner)
    # As the CLI prepares it: a relative path is read beside the workflow. The pull lane pulls; the run
    # lanes run on the images it pulled.
    # The view is read from the compiled tree, before the inline flags reshape it.
    row, root = targets.target('cwl'), Path(args.yaml).parent.absolute()
    view = sophios.compiler.resolved_view(compilation)
    lowered = row.lower(targets.for_run(row, view, root))
    lowered = replace(lowered, model=_inlined(lowered.model, args))
    prepared = backend.prepare(lowered, (), Path(basepath), setup.Settings(
        run, root, row.run_values(view), pull=docker_pull_only))
    if docker_pull_only:
        return
    # NOTE: Do not use --cachedir; we want to actually test everything.
    retval = local.run(backends.RunRequest(prepared.name, Path(basepath), RunSettings(run.runner)))
    assert retval == 0, retval


def _discover(yml_paths: dict[str, dict[str, Path]], stem: str) -> Path | None:
    """The file a discovered workflow stem names, in whichever namespace holds it."""
    return next((paths[stem] for paths in yml_paths.values() if stem in paths), None)


def _is_includer_fragment(error: SophiosError) -> bool:
    """Whether a document failed only for edges an includer would supply: it has no standalone compilation, so
    comparing it against an embedded one compares nothing."""
    return bool(error.diagnostics) and all(
        item.code is SophiosErrorCode.UNDEFINED_EDGE for item in error.diagnostics)


def _compile_corpus_workflow(yml_path_str: str, yml_path: Path, registry: CorpusRegistry) -> CompilationResult:
    """One corpus workflow compiled as the command line compiles it, skipping a fragment that has no standalone
    compile."""
    try:
        return _compiled(yml_path, get_args(str(yml_path)), registry)
    except SophiosError as error:
        if _is_includer_fragment(error):
            pytest.skip(f'{yml_path_str} consumes edges from an includer')
        raise


#: Fields a CWL `CommandLineTool` input may declare and a workflow input may not. `secondaryFiles`, `streamable`,
#: `loadContents` and `loadListing` are not here: the CWL v1.2 schema allows each on a workflow input.
_TOOL_ONLY_BOUNDARY_FIELDS: Final = ('inputBinding',)

#: Fields belonging to a CWL document rather than to a process, which an embedded process therefore may not keep.
#: Spelled out rather than read from `post_compile.DOCUMENT_FIELDS`, which is what the production code pops: a test
#: that reads the constant it is checking would agree with whatever it says.
_EMBEDDED_FORBIDDEN_FIELDS: Final = ('$namespaces', '$schemas', 'cwlVersion')


def _check_residue(nested: CompilationArtifact) -> None:
    """No Sophios word and no tool-only field survives into an emitted document, and no document field into an
    embedded process. Both reached a runner before a test: a `wic_alias` written into `in:`, a promoted port carrying
    its tool's `inputBinding`, and a tool declaring `cwlVersion: v1.0` that made every inlined workflow invalid."""
    for artifact in _artifacts(nested):
        text = json.dumps(artifact.cwl, default=str)
        for word in Key.ALL:
            assert f'"{word}"' not in text, f'{artifact.name} emits the Sophios key {word}'
        if artifact.cwl.get('class') == 'Workflow':
            for port, declared in (artifact.cwl.get('inputs') or {}).items():
                for field in _TOOL_ONLY_BOUNDARY_FIELDS:
                    assert not isinstance(declared, dict) or field not in declared, (
                        f'{artifact.name}: workflow input {port} declares {field}, which belongs to a tool input')

    def embedded(node: Any, depth: int = 0, path: str = 'root') -> None:
        if not isinstance(node, dict):
            return
        if depth:
            for field in _EMBEDDED_FORBIDDEN_FIELDS:
                assert field not in node, f'{path} is embedded and declares {field}, which belongs to a document'
            # A relative `$include` resolves against the embedding document, so an embedded tool is packed.
            assert '"$include"' not in json.dumps(node), f'{path} is embedded and still includes a file by its path'
        for step in node.get('steps', []) or []:
            embedded(step.get('run'), depth + 1, f'{path}/{step.get("id")}')

    embedded(inline_artifact_runs(nested).cwl)


def _workflows(graph: WorkflowGraph) -> list[WorkflowGraph]:
    """`graph` and every workflow nested in it."""
    return [graph, *(each for child in graph.children for each in _workflows(child))]


def _check_embedding_independence(graph: WorkflowGraph, nested: CompilationArtifact, registry: CorpusRegistry,
                                  options: sophios.compiler.CompileSettings) -> None:
    """Each subworkflow compiled alone, from its own file, as a root is the CWL it is embedded as (but for `run:`
    paths, whose namespaces differ in length)."""
    names = Names.of(graph)
    graphs = {tuple(names.step(part) for part in each.namespace.parts): each for each in _workflows(graph)}
    for embedded in [artifact for artifact in _artifacts(nested) if artifact.cwl['class'] == 'Workflow'][1:]:
        inside = graphs[embedded.namespace]
        sub_path = _discover(yml_paths, inside.name)
        assert sub_path is not None, f'{inside.name} is embedded but has no file of its own'
        try:
            alone = sophios.compiler.compile_source(frontdoor.bundle_from_disk(sub_path, yml_paths, registry.tools),
                                                    options)
        except SophiosError as error:
            if _is_includer_fragment(error):  # `test_rand_fail`'s only child, `fail.wic`, is not standalone
                continue
            raise
        assert (sophios.utils.recursively_delete_dict_key('run', embedded.cwl)
                == sophios.utils.recursively_delete_dict_key('run', cwl_tree(alone).cwl)), inside.name


#: The fields a CWL document declares its prefixes and ontologies in.
DECLARATIONS: Final = ('$namespaces', '$schemas')


def declared_beneath(artifact: CompilationArtifact) -> dict[str, Any]:
    """The `$namespaces` and `$schemas` of `artifact` and of every process beneath it, merged as
    embedding merges them: an embedded process may not declare its own, so they move up."""
    namespaces = dict(artifact.cwl.get('$namespaces', {}))
    schemas = list(artifact.cwl.get('$schemas', []))
    for child in artifact.children:
        below = declared_beneath(child)
        namespaces |= below.get('$namespaces', {})
        schemas += [schema for schema in below.get('$schemas', []) if schema not in schemas]
    return {field: value for field, value in zip(DECLARATIONS, (namespaces, schemas)) if value}


def _check_embedding_keeps_meaning(nested: CompilationArtifact) -> None:
    """Embedding linked child graphs changes placement, not workflow meaning; what UP_TO_EMBEDDING forgives in
    `$namespaces` and `$schemas` is the move up."""
    embedded = inline_artifact_runs(nested)
    divergence = equivalent(nested.cwl, embedded.cwl, Strength.UP_TO_EMBEDDING)
    assert divergence is None, divergence
    assert {field: embedded.cwl[field] for field in DECLARATIONS if field in embedded.cwl} == declared_beneath(nested)


def _kept_calls(artifact: CompilationArtifact) -> list[str]:
    """The ids of the steps anywhere in the tree that run a workflow and carry more than `in` and `out`, or say
    `wic: inlineable: false`: the calls `--cwl_inline_subworkflows` should leave nested, decided from the document and
    not read back from the code under test. (No corpus workflow uses `--partial_failure_enable`, which keeps all.)"""
    return [step['id'] for document in _artifacts(artifact)
            for step, child in zip(document.cwl.get('steps', []), document.children)
            if child.cwl.get('class') == 'Workflow'
            and (set(step) - {'id', 'in', 'run', 'out'} or not child.inlining.inlineable)]


def _check_flatten_keeps_meaning(yml_path_str: str, nested: CompilationArtifact) -> None:
    """Flattening dissolves each call it can and nothing else: the root is a model written separately
    (`flatten_model`) up to renaming, its inputs, outputs and job inputs untouched, and each call left nested is one
    `_kept_calls` expects."""
    flat, _ = post_compile.flatten_subworkflows(nested)
    remaining = [step['id'] for document in _artifacts(flat)
                 for step, child in zip(document.cwl.get('steps', []), document.children)
                 if child.cwl.get('class') == 'Workflow']
    # A dissolved call's steps are renamed `<call>~<step>`; the step's own id is the last part.
    assert sorted(i.split('~')[-1] for i in remaining) == sorted(i.split('~')[-1] for i in _kept_calls(nested)), \
        yml_path_str
    divergence = equivalent(flatten_model(nested), flatten_model(flat), Strength.UP_TO_RENAMING)
    assert divergence is None, divergence
    assert list(flat.cwl['inputs']) == list(nested.cwl['inputs'])
    assert list(flat.cwl['outputs']) == list(nested.cwl['outputs'])
    assert flat.job_inputs == nested.job_inputs


def _check_inline_flags_change_nothing(nested: CompilationArtifact) -> None:
    """`--cwl_inline_subworkflows` changes no document of a workflow that calls none, which is why `INLINE_CASES`
    runs no `flat` form of it. The flags shape only the compiled tree, so one compile serves every form."""
    for runtag in (False, True):
        assert (_artifacts(apply_inline_options(nested, subworkflows=True, runtag=runtag)[0])
                == _artifacts(apply_inline_options(nested, subworkflows=False, runtag=runtag)[0])), runtag


@pytest.mark.cwl_corpus
@pytest.mark.parametrize('yml_path_str, yml_path', CHECKED)
def test_corpus_compiles_clean_in_every_form(yml_path_str: str, yml_path: Path,
                                             corpus_registry: CorpusRegistry) -> None:
    """Each corpus workflow compiles to documents CWL can read, its subworkflows as they compile alone, and each form
    the inline flags give means what the nested form means; each real-time analysis it declares compiles. cwltool
    finds each defect these find, but only in a run, behind a container pull: here each costs a compile."""
    result = _compile_corpus_workflow(yml_path_str, yml_path, corpus_registry)
    options = sophios.cli.settings_from_args(get_args(str(yml_path)))[0]
    _check_residue(cwl_tree(result))
    _check_embedding_independence(result.graph, cwl_tree(result), corpus_registry, options)
    _check_embedding_keeps_meaning(cwl_tree(result))
    _check_flatten_keeps_meaning(yml_path_str, cwl_tree(result))
    if not _calls_a_workflow(yml_path):
        _check_inline_flags_change_nothing(cwl_tree(result))
    for analysis in realtime.compile_analyses(result.realtime, corpus_registry.workflows, corpus_registry.tools,
                                              options):
        assert analysis.artifact.cwl['steps'], analysis.declaration.analysis


def _tool_defaults(path: Path) -> Counter[tuple[str, str, Any, Any]]:
    """What cwltool makes of the network and directory-listing defaults of every tool under `path`."""
    from cwltool.context import LoadingContext  # pylint: disable=import-outside-toplevel
    from cwltool.load_tool import load_tool  # pylint: disable=import-outside-toplevel
    from cwltool.workflow import default_make_tool  # pylint: disable=import-outside-toplevel

    found: Counter[tuple[str, str, Any, Any]] = Counter()

    def walk(process: Any) -> None:
        for step in getattr(process, 'steps', []):
            tool = step.embedded_tool
            if tool.tool['class'] != 'Workflow':
                network, _ = tool.get_requirement('NetworkAccess')
                listing, _ = tool.get_requirement('LoadListingRequirement')
                found[(tool.tool['class'], str(tool.tool.get('baseCommand')),
                       network and network.get('networkAccess'), listing and listing.get('loadListing'))] += 1
            walk(tool)

    walk(load_tool(str(path), LoadingContext({'construct_tool_object': default_make_tool})))
    return found


@pytest.mark.cwl_corpus
@pytest.mark.needs_cwltool
@pytest.mark.parametrize('yml_path_str, yml_path', CHECKED)
def test_corpus_validates_in_cwltool_in_every_form(yml_path_str: str, yml_path: Path, tmp_path: Path,
                                                   corpus_registry: CorpusRegistry) -> None:
    """cwltool accepts the flat form and the flat form embedded, and gives a tool embedded the network and
    directory-listing defaults it gives the same tool read from its own file: a tool written for CWL v1.0, embedded
    in a v1.2 document with no `cwlVersion` of its own, ran without network access, and every download of the weekly
    lane failed. The large workflows are only validated, as the weekly lane always did."""
    nested = cwl_tree(_compile_corpus_workflow(yml_path_str, yml_path, corpus_registry))
    flat, _ = post_compile.flatten_subworkflows(nested)
    for form, artifact in (('flat', flat), ('flat_embedded', inline_artifact_runs(flat))):
        sophios.input_output.write_artifacts_to_disk(artifact, tmp_path / form, output_targets=())
        assert validate_cwl(str(tmp_path / form / f'{nested.name}.cwl')) == 0, form
    if yml_path_str not in large_workflows:
        sophios.input_output.write_artifacts_to_disk(nested, tmp_path / 'nested', output_targets=())
        sophios.input_output.write_artifacts_to_disk(inline_artifact_runs(nested), tmp_path / 'embedded',
                                                     output_targets=())
        root = f'{nested.name}.cwl'
        assert _tool_defaults(tmp_path / 'embedded' / root) == _tool_defaults(tmp_path / 'nested' / root)
