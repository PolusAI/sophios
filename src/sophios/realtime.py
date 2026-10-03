"""Real-time analysis: compile each declared analysis once, and run it beside the workflow.

A `cwl_subinterpreter` step declares an analysis (see `sophios.ir.realtime`).
After the main compile, `compile_analyses` compiles each analysis as a one-step
workflow and `write` puts it under `<basepath>/realtime/<name>/`, with a manifest
`<basepath>/<workflow>.realtime.json` that `run_local` reads.
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from . import compiler
from . import input_output as io
from .ir import frontdoor
from .ir.artifacts import CompilationArtifact
from .ir.realtime import Declaration
from .lang.diagnostics import Diagnostic, Severity, SophiosError
from .lang.error_codes import SophiosErrorCode
from .utils_yaml import Key
from .wic_types import CompilerOptions, GraphSettings, Tools, Yaml

#: The directory, under the base path and under the cachedir, that holds each analysis's files.
DIRECTORY: Final = 'realtime'


@dataclass(frozen=True, slots=True)
class Analysis:
    """A declaration with its analysis compiled, under the name its files are kept by."""

    name: str
    declaration: Declaration
    artifact: CompilationArtifact


def compile_analyses(declarations: tuple[Declaration, ...],
                     yml_paths: dict[str, dict[str, Path]], tools: Tools,
                     compiler_options: CompilerOptions,
                     graph_settings: GraphSettings) -> tuple[Analysis, ...]:
    """Compile each declaration's analysis once, as a one-step workflow.

    Raises:
        SophiosError: `wic044` at the declaration, carrying each of the analysis's own
            diagnostics, when an analysis does not compile.
    """
    analyses: list[Analysis] = []
    taken: set[str] = set()
    for declaration in declarations:
        name = declaration.document
        suffix = 2
        while name in taken:
            name, suffix = f'{declaration.document}_{suffix}', suffix + 1
        taken.add(name)
        source = yaml.dump(_wrapper(declaration), Dumper=io.NoAliasDumper, sort_keys=False, line_break='\n')
        try:
            bundle = frontdoor.bundle_from_source(source, f'{Path(declaration.analysis).stem}_only', yml_paths, tools)
            result = compiler.compile_source(bundle, compiler_options, graph_settings,
                                             relative_run_path=True, testing=True)
        except SophiosError as e:
            raise SophiosError(Diagnostic(
                Severity.ERROR, SophiosErrorCode.REALTIME_DECLARATION,
                f'real-time analysis {declaration.analysis!r} does not compile: {diagnostic}',
                declaration.span) for diagnostic in e.diagnostics) from e
        analyses.append(Analysis(name, declaration, result.artifact))
    return tuple(analyses)


def _wrapper(declaration: Declaration) -> Yaml:
    """The one-step workflow that runs `declaration`'s analysis, configured by its `config`.

    Every `in:` value is a literal: a plain string there would otherwise be read as
    an edge, and a file is named by its basename, found when the analysis runs.
    """
    analysis = declaration.analysis
    config = _literal_inputs(declaration.config)
    if analysis.endswith('.wic'):
        return {'steps': [{'id': analysis}], 'wic': {'steps': {f'(1, {analysis})': {'wic': {'steps': config}}}}}
    # id last, so a config carrying its own `id` cannot retarget the step.
    return {'steps': [{**config, 'id': analysis}]}


def _literal_inputs(config: Any) -> Any:
    """`config` with every value of every `in:` mapping in it made an inline literal."""
    match config:
        case dict():
            return {key: ({port: {Key.INLINE_INPUT: value} for port, value in item.items()}
                          if key == 'in' and isinstance(item, dict) else _literal_inputs(item))
                    for key, item in config.items()}
        case list():
            return [_literal_inputs(item) for item in config]
    return config


def manifest_path(basepath: Path, workflow_name: str) -> Path:
    """Where `write` records the analyses of `workflow_name`, beside its names map."""
    return basepath / f'{workflow_name}.realtime.json'


def documents(analyses: tuple[Analysis, ...], basepath: Path) -> list[Path]:
    """The root CWL file of each analysis, as `write` writes it."""
    return [basepath / DIRECTORY / analysis.name / f'{analysis.artifact.name}.cwl' for analysis in analyses]


def write(analyses: tuple[Analysis, ...], basepath: Path, workflow_name: str, root_dir: Path) -> None:
    """Write each analysis under `<basepath>/realtime/<name>/` and the manifest that lists them.

    With no analyses there is no manifest, and one an earlier compile wrote is removed,
    so a run never watches for a declaration the workflow no longer has.

    Args:
        root_dir (Path): The root workflow's directory, where an analysis input that no step
            wrote is looked for, as the main run stages its own inputs.
    """
    manifest = manifest_path(basepath, workflow_name)
    if not analyses:
        manifest.unlink(missing_ok=True)
        return
    entries = []
    for analysis, cwl in zip(analyses, documents(analyses, basepath)):
        io.write_artifacts_to_disk(analysis.artifact, cwl.parent, True)
        declaration = analysis.declaration
        entries.append({
            'name': analysis.name,
            'analysis': declaration.analysis,
            'file_pattern': declaration.file_pattern,
            'max_times': declaration.max_times,
            'interval': declaration.interval,
            'cwl': str(cwl.relative_to(basepath)),
            'inputs': str(cwl.with_name(f'{analysis.artifact.name}_inputs.yml').relative_to(basepath)),
            'root_dir': str(root_dir.absolute()),
            'declared_at': str(declaration.span) if declaration.span else declaration.document,
        })
    manifest.write_text(json.dumps(entries, indent=2), encoding='utf-8')
