from dataclasses import dataclass, field
from typing import Any, NamedTuple, NotRequired, TypeAlias, TypedDict

import networkx as nx

# See https://mypy.readthedocs.io/en/stable/kinds_of_types.html#type-aliases

# See https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html#confval-autodoc_type_aliases
# See https://www.sphinx-doc.org/en/master/usage/extensions/napoleon.html#confval-napoleon_type_aliases
# The sphinx autodoc documentation claims type aliases can be added to
# autodoc_type_aliases in docs/conf.py instead of showing their expansions.
# However, I can't seem to get it to work.

Cwl: TypeAlias = dict[str, Any]
Json: TypeAlias = dict[str, Any]
RawJson: TypeAlias = str
Yaml: TypeAlias = dict[str, Any]

# In python there are unfortunately an enormous number of ways to represent the humble struct.
# I have chosen to use NamedTuple to emphasize the immutability aspect (see below).
# See https://mypy.readthedocs.io/en/stable/kinds_of_types.html#named-tuples


class Tool(NamedTuple):
    run_path: str
    cwl: Cwl


class StepId(NamedTuple):
    stem: str  # filename without extension
    plugin_ns: str  # left column of yml_paths.txt


Tools: TypeAlias = dict[StepId, Tool]

# NOTE: Please read the Namespacing section of docs/dev/devguide.md !!!
Namespace: TypeAlias = str
Namespaces: TypeAlias = list[Namespace]

DiGraph: TypeAlias = Any  # graphviz.DiGraph


@dataclass(slots=True)
class GraphData:
    # `default_factory` gives each instance its own list; a bare `[]` default
    # would be shared and mutated across every instance. Not `frozen=True`:
    # the compiler rebinds these fields in place while building a graph.
    name: str  # TODO: Should this be StepId?
    nodes: list[tuple[str, dict]] = field(default_factory=list)
    edges: list[tuple[str, str, dict]] = field(default_factory=list)
    subgraphs: list[Any] = field(default_factory=list)
    ranksame: list[str] = field(default_factory=list)


# This groups together the classes which represent our graph.
# Excluding --graph_inline_depth related code, all graph
# operations should be performed on all representations.
class GraphReps(NamedTuple):
    graphviz: DiGraph
    networkx: nx.DiGraph
    graphdata: GraphData


# Create a type for our Abstract Syntax Tree (AST).
# We can probably use Dict here if str is step_name_i not just yaml_stem.
# If we need to insert steps, that will happen
# after edge inference, so there should not be a uniqueness issue w.r.t. step
# number re-indexing.


class YamlTree(NamedTuple):
    step_id: StepId
    yml: Yaml


class CompilerOptions(TypedDict):
    """Core compiler flags needed for compilation and transformation into CWL."""
    partial_failure_enable: bool
    inference_use_naming_conventions: bool
    insert_steps_automatically: bool
    inference_disable: bool
    allow_raw_cwl: bool
    #: The explicit language version, if the user set one; None means infer.
    #: NotRequired so existing constructors of this dict stay valid.
    lang_version: NotRequired[str | None]
    inference_rules: NotRequired[dict[str, str]]
    renaming_conventions: NotRequired[list[tuple[str, str]]]


class GraphSettings(TypedDict):
    """Settings dict for graphviz graph generation."""
    graph_dark_theme: bool
    graph_inline_depth: int
    graph_label_edges: bool
    graph_label_stepname: bool
    graph_show_outputs: bool
    graph_show_inputs: bool


class YamlTagPaths(TypedDict):
    """Paths that need to be included in (generated) yaml tags."""
    cachedir: str
    yaml: str
    homedir: str


class PluginNodeConfig(TypedDict):
    """The UI-derived input/output configuration for a single WFB plugin node."""
    ui: list[Json]
    inputs: list[Json]
    outputs: list[Json]
