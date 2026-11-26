"""Compile-cost cases and a scatter's run time, timed weekly; no timing fails anything. Not hermetic, since the
corpus trend is the point: a case that raises is reported, the others still run, and `main` returns 1."""
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, Final

# Run as `python tests/core/benchmarks.py`, outside any package: `core.*` is imported absolutely with its parent on
# sys.path (pytest puts it there already), so the imports follow; hence the blanket disable.
# pylint: disable=wrong-import-position
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.hermetic import compile_hermetic, nested_workflow
from core.synthetic_tools import SYNTHETIC_TOOLS
from core.test_setup import load_test_registry, workflow_paths

from sophios import frontdoor
import sophios.cli
import sophios.compiler
from sophios.lang import parse


@dataclass(frozen=True, slots=True)
class Case:
    """One thing worth timing, and why it is on the list."""
    name: str
    rationale: str
    run: Callable[[], object]


def _compile_corpus_file(path: Path) -> None:
    """Compile one corpus workflow from disk against the real registry, as test_examples.run_workflows does."""
    registry = load_test_registry()
    bundle = frontdoor.bundle_from_disk(path, registry.workflows, registry.tools)
    settings, _graph_settings = sophios.cli.settings_from_args(sophios.cli.get_args(str(path)))
    sophios.compiler.compile_source(bundle, settings)


_CORPUS: Final = sorted(path for paths in workflow_paths().values() for path in paths.values())
_DEEP_NESTING_YML: Final = nested_workflow(15)
_WIDE_INFERENCE_YML: Final = {'steps': [{'id': 'mk_file'} for _ in range(100)]}
_SCATTER: Final = Path(__file__).with_name('scattering_scaling.wic')


def _parse_corpus() -> None:
    for path in _CORPUS:
        parse(path.read_text(encoding='utf-8'), filename=path.name)


def _run_scatter(runner: str, size: int) -> None:
    """Run scattering_scaling.wic over `size` elements under `runner`, as the command line runs it."""
    with tempfile.TemporaryDirectory() as tmp:
        (job := Path(tmp) / 'job.yml').write_text(f'array_int~maxval: {size}\n', encoding='utf-8')
        subprocess.run([sys.executable, '-m', 'sophios.main', '--yaml', _SCATTER, '--run_local', '--quiet',
                        '--cwl_runner', runner, '--inputs_file', job, '--basepath', tmp],
                       cwd=tmp, check=True, capture_output=True)


CASES: Final[tuple[Case, ...]] = tuple(
    Case(f'corpus/{path.parent.name}/{path.name}',
         "One of the corpus's reachable workflows, compiled for real "
         "against this environment's plugin registry — the trend everyone "
         "actually cares about.",
         run=partial(_compile_corpus_file, path))
    for path in _CORPUS
) + (
    Case('deep_nesting',
         "A workflow partitioned fifteen levels deep with "
         "hermetic.subworkflow_step. Namespacing cost grows with depth and "
         "nothing else on this list has depth.",
         run=lambda: compile_hermetic(_DEEP_NESTING_YML, 'oracle', tools=SYNTHETIC_TOOLS)),
    Case('wide_inference',
         "One hundred steps, every input inferred and never matched, so "
         "Infer's backward scan always runs to completion. "
         "It scans backwards over previous steps, so this is the quadratic "
         "one.",
         run=lambda: compile_hermetic(_WIDE_INFERENCE_YML, 'oracle', tools=SYNTHETIC_TOOLS)),
    Case('parse_only',
         "sophios.lang.parse over the corpus, no compilation. Separates a "
         "parser regression from a compiler regression; without it a slow "
         "week has two candidate causes and no way to choose.",
         run=_parse_corpus),
) + tuple(
    Case(f'scatter/{runner}/{size}',
         "A scatter's run time should grow linearly with its size; Toil's once did not, on Slurm "
         "(DataBiosphere/toil#4434). Two sizes per runner show what each element costs.",
         run=partial(_run_scatter, runner, size))
    for runner in ('cwltool', 'toil-cwl-runner') for size in (200, 2000)
)


def main() -> int:
    """Time every case and print a table. Returns 1 if a case raised, else 0."""
    load_test_registry()  # Discovery is shared setup, not the first corpus case's cost.

    print('| case | seconds |')
    print('|---|---:|')
    corpus_total = 0.0
    all_total = 0.0
    failed = 0
    for case in CASES:
        start = time.perf_counter()
        try:
            case.run()
            elapsed_seconds = time.perf_counter() - start
            elapsed = f'{elapsed_seconds:.3f}'
        except Exception as exc:  # pylint: disable=broad-except
            elapsed_seconds = time.perf_counter() - start
            elapsed = f'error: {type(exc).__name__}'
            failed += 1
        all_total += elapsed_seconds
        if case.name.startswith('corpus/'):
            corpus_total += elapsed_seconds
        print(f'| {case.name} | {elapsed} |')
    print(f'| corpus (total) | {corpus_total:.3f} |')
    print(f'| all cases (total) | {all_total:.3f} |')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
