from typing import Any, Dict, List, NamedTuple, Tuple

import networkx as nx

# See https://mypy.readthedocs.io/en/stable/kinds_of_types.html#type-aliases

# See https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html#confval-autodoc_type_aliases
# See https://www.sphinx-doc.org/en/master/usage/extensions/napoleon.html#confval-napoleon_type_aliases
# The sphinx autodoc documentation claims type aliases can be added to
# autodoc_type_aliases in docs/conf.py instead of showing their expansions.
# However, I can't seem to get it to work.
# TODO: Consider removing all type aliases in favor of classes.

KV = Dict[str, Any]
Cwl = KV
Json = KV
Yaml = KV

# In python there are unfortunately an enormous number of ways to represent the humble struct.
# See https://stackoverflow.com/questions/53409117/what-are-the-main-differences-of-namedtuple-and-typeddict-in-python-mypy
# I have chosen to use NamedTuple to emphasize the immutability aspect (see below).
# See https://mypy.readthedocs.io/en/stable/kinds_of_types.html#named-tuples


class Tool(NamedTuple):
    run_path: str
    cwl: Cwl


class StepId(NamedTuple):
    stem: str  # filename without extension
    plugin_ns: str  # left column of yml_paths.txt


Tools = Dict[StepId, Tool]

# NOTE: Please read the Namespacing section of docs/devguide.md !!!
Namespace = str
Namespaces = List[Namespace]

WorkflowInputs = Dict[str, Any]
WorkflowInputsFile = Dict[str, Any]
WorkflowOutputs = List[Yaml]
InternalOutputs = List[str]
ExplicitEdgeDef = Tuple[Namespaces, str]
ExplicitEdgeDefs = Dict[str, ExplicitEdgeDef]
ExplicitEdgeCalls = Dict[str, ExplicitEdgeDef]
PluginID = int
StepName1 = str
DiGraph = Any  # graphviz.DiGraph


class GraphData():
    # pylint:disable=too-few-public-methods
    def __init__(self,
                 name: str,  # TODO: Should this be StepId?
                 nodes: List[Tuple[str, Dict]] = [],
                 edges: List[Tuple[str, str, Dict]] = [],
                 subgraphs: List[Any] = [],
                 ranksame: List[str] = []) -> None:
        # NOTE: See comments in utils_graphs.flatten_graphdata() !!!
        self.name = name
        self.nodes = nodes
        self.edges = edges
        self.subgraphs = subgraphs
        self.ranksame = ranksame


# This groups together the classes which represent our graph.
# Excluding --graph_inline_depth related code, all graph
# operations should be performed on all representations.
class GraphReps(NamedTuple):
    graphviz: DiGraph
    networkx: nx.DiGraph
    graphdata: GraphData


YamlDSLArgs = Yaml

# Since we cannot store extra tags in CWL files, we need a data structure
# to store temporary compiler info that gets passed through the recursion.
# The number of subworkflows is arbitrary (zero or more), so what we want is a
# Rose Tree https://en.wikipedia.org/wiki/Rose_tree
# Unfortunately, since mypy does not support Algebraic Data Types (ADTs)
# we have to break the recursion by replacing the recursive instance of RoseTree with Any :(
DataType = Any


class RoseTree(NamedTuple):
    data: DataType
    sub_trees: List[Any]  # Any = RoseTree
# Note that instead of DataType we could provide a specific type, but remember that
# a Rose Tree is defined by its structure, not by the specific type of data it contains.
# We can simply cast to a specific type at each call site, i.e.
# data: SpecificType = rose_tree.data

# Now we can define a specific data type for a single node in our Abstract Syntax Tree.


class NodeData(NamedTuple):
    namespaces: Namespaces
    name: str
    yml: Yaml  # i.e. The AST that was compiled.
    # If this is not the AST that was passed in, then the compiler introduced
    # some modifications (i.e. --insert_steps_automatically) and you need to recompile
    compiled_cwl: Cwl
    tool: Tool
    workflow_inputs_file: WorkflowInputsFile
    explicit_edge_defs: ExplicitEdgeDefs
    explicit_edge_calls: ExplicitEdgeCalls
    graph: GraphReps
    inputs_workflow: WorkflowInputs
    step_name_1: StepName1


class EnvData(NamedTuple):
    input_mapping: Dict[str, List[str]]
    output_mapping: Dict[str, str]
    inputs_file_workflow: WorkflowInputsFile
    vars_workflow_output_internal: InternalOutputs
    explicit_edge_defs: ExplicitEdgeDefs
    explicit_edge_calls: ExplicitEdgeCalls


class CompilerInfo(NamedTuple):
    rose: RoseTree
    env: EnvData
# Note that while Tuples and NamedTuples are immutable, they can contain mutable entries!
# Let's partition the data into entries which are immutable / fixed
# (well, at least after compilation of the subworkflow is complete) and entries
# which are mutably updated throughout the recursion. The latter are essentially
# environment variables of sorts, and they do not need to be stored in the Rose Tree.

# Create a type for our Abstract Syntax Tree (AST).
# We can probably use Dict here if str is step_name_i not just yaml_stem.
# If we need to insert steps, that will happen
# after edge inference, so there should not be a uniqueness issue w.r.t. step
# number re-indexing.


class YamlTree(NamedTuple):
    step_id: StepId
    yml: Yaml


class YamlForest(NamedTuple):
    yaml_tree: YamlTree
    sub_forests: List[Tuple[StepId, Any]]  # Any = YamlForest
