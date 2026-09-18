"""One way to compile an in-memory workflow in tests.

The split below is the only distinction call sites need: some want the typed
result and most want only emitted CWL.
"""
import graphviz
import networkx as nx
from hypothesis import HealthCheck, settings

import sophios.cli
import sophios.compiler
from sophios.ir.artifacts import CompilationResult
from sophios.wic_types import GraphData, GraphReps, StepId, Yaml, YamlTree

from .test_setup import load_test_registry

#: Shared Hypothesis budgets. Compiled properties are an order of magnitude
#: slower than parse-only ones, so they get their own, smaller, count.
FAST = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)
COMPILED = settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)

#: A minimal real workflow: one tool step, one inline literal input.
TOUCH: Yaml = {'steps': [{'id': 'touch', 'in': {'filename': {'wic_inline_input': 'empty.txt'}}}]}


def compile_info(yml: Yaml, name: str = 'harness', *,
                 lang_version: str | None = None,
                 allow_raw_cwl: bool | None = None) -> CompilationResult:
    """Compile one in-memory workflow and return the whole compiler result.

    The two overrides are named rather than taken as `**options` so that a
    typo is a type error instead of a silently ignored setting.
    """
    compiler_options, graph_settings, tag_paths = sophios.cli.default_compilation_settings()
    if lang_version is not None:
        compiler_options['lang_version'] = lang_version
    if allow_raw_cwl is not None:
        compiler_options['allow_raw_cwl'] = allow_raw_cwl
    graph = GraphReps(graphviz.Digraph(name=f'cluster_{name}'), nx.DiGraph(), GraphData(name))
    tools = load_test_registry().tools
    return sophios.compiler.compile_document(
        YamlTree(StepId(name, 'global'), yml),
        compiler_options, graph_settings, tag_paths, tools,
        relative_run_path=True, testing=True, graph_target=graph)


def compile_cwl(yml: Yaml, name: str = 'harness', *,
                lang_version: str | None = None,
                allow_raw_cwl: bool | None = None) -> Yaml:
    """Compile one in-memory workflow and return the emitted CWL."""
    info = compile_info(yml, name, lang_version=lang_version, allow_raw_cwl=allow_raw_cwl)
    compiled: Yaml = info.artifact.cwl
    return compiled
