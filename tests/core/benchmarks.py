"""Compile-cost cases, run weekly; no timing fails anything. Not hermetic, since the corpus trend is the point: a
case that raises is reported, the others still run, and `main` returns 1."""
import sys
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
from core.synthetic_tools import SYNTHETIC_TOOLS, insertion_registry
from core.test_setup import load_test_registry, workflow_paths

from sophios.ir import frontdoor
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
    sophios.compiler.compile_source(bundle, settings, quiet=True)


_CORPUS: Final = sorted(path for paths in workflow_paths().values() for path in paths.values())
_SPECULATIVE_YML, _SPECULATIVE_TOOLS = insertion_registry()
_DEEP_NESTING_YML: Final = nested_workflow(15)
_WIDE_INFERENCE_YML: Final = {'steps': [{'id': 'mk_file'} for _ in range(100)]}


def _parse_corpus() -> None:
    for path in _CORPUS:
        parse(path.read_text(encoding='utf-8'), filename=path.name)


CASES: Final[tuple[Case, ...]] = tuple(
    Case(f'corpus/{path.parent.name}/{path.name}',
         "One of the corpus's reachable workflows, compiled for real "
         "against this environment's plugin registry — the trend everyone "
         "actually cares about.",
         run=partial(_compile_corpus_file, path))
    for path in _CORPUS
) + (
    Case('speculative_insertion',
         "A workflow with two branches, each needing one whitelisted-format-"
         "converter insertion. Two or more whitelisted converter tools put a "
         "compilation on this path, whose cost is O(2^n). The corpus never "
         "reaches it. This case times "
         "two real insertions and the fixed-point passes they force, so it is "
         "the one number on this list that moves with the insertion search "
         "rather than with per-step cost.",
         run=lambda: compile_hermetic(_SPECULATIVE_YML, 'oracle', tools=_SPECULATIVE_TOOLS,
                                      insert_steps_automatically=True)),
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
