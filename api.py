import argparse
import copy
from pathlib import Path
import sys
from typing import Dict
from unittest.mock import patch

from fastapi import Request, FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
import graphviz
import networkx as nx
import uvicorn

from auth.auth import authenticate
from wic import ast, cli, compiler, inference, labshare, utils, plugins, __version__  # , utils_graphs
from wic.schemas import wic_schema
from wic.wic_types import GraphData, GraphReps, Json, NodeData, StepId, YamlTree


app = FastAPI()

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_args(yaml_path: str = '') -> argparse.Namespace:
    """This is used to get mock command line arguments.

    Returns:
        argparse.Namespace: The mocked command line arguments
    """
    testargs = ['wic', '--yaml', yaml_path, '--cwl_output_intermediate_files', 'True']  # ignore --yaml
    # For now, we need to enable --cwl_output_intermediate_files. See comment in compiler.py
    with patch.object(sys, 'argv', testargs):
        args: argparse.Namespace = cli.parser.parse_args()
    return args


@app.get("/", status_code=status.HTTP_200_OK)
@authenticate
async def root() -> Dict[str, str]:
    """The api has 2 routes: compile and inference

    Returns:
        Dict[str, str]: {"message": "The api has 2 routes: compile and inference"}
    """
    return {"message": "The api has 2 routes: compile and inference"}


@app.post("/compile")
@authenticate
async def compile_wf(request: Request) -> Json:
    """The compile route compiles the json object from workflow builder ui which contains steps built in the ui.

    Args:
        request (Request): request object from workflow builder ui

    Returns:
        compute_workflow (JSON): workflow json object ready to submit to compute
    """
    print('----------Run Workflow!---------')
    root_yaml_tree = await request.json()

    tools_cwl = plugins.get_tools_cwl(Path('cwl_dirs.txt'))
    yml_paths = plugins.get_yml_paths(Path('yml_dirs.txt'))

    # Perform initialization via mutating global variables (This is not ideal)
    compiler.inference_rules = dict(utils.read_lines_pairs(Path('inference_rules.txt')))
    inference.renaming_conventions = utils.read_lines_pairs(Path('renaming_conventions.txt'))

    # Generate schemas for validation and vscode IntelliSense code completion
    yaml_stems = utils.flatten([list(p) for p in yml_paths.values()])
    validator = wic_schema.get_validator(tools_cwl, yaml_stems, write_to_disk=True)

    yaml_path = "workflow.json"

    # Load the high-level yaml root workflow file.
    Path('autogenerated/').mkdir(parents=True, exist_ok=True)
    wic_obj = {'wic': root_yaml_tree.get('wic', {})}
    plugin_ns = wic_obj['wic'].get('namespace', 'global')
    step_id = StepId(yaml_path, plugin_ns)
    y_t = YamlTree(step_id, root_yaml_tree)
    yaml_tree_raw = ast.read_ast_from_disk(y_t, yml_paths, tools_cwl, validator)
    yaml_tree = ast.merge_yml_trees(yaml_tree_raw, {}, tools_cwl)

    rootgraph = graphviz.Digraph(name=yaml_path)
    with rootgraph.subgraph(name=f'cluster_{yaml_path}') as subgraph_gv:
        subgraph_nx = nx.DiGraph()
        graphdata = GraphData(yaml_path)
        subgraph = GraphReps(subgraph_gv, subgraph_nx, graphdata)
        try:
            compiler_info = compiler.compile_workflow(yaml_tree, get_args(yaml_path), [], [subgraph], {}, {}, {}, {},
                                                      tools_cwl, True, relative_run_path=True, testing=False)
        except Exception as e:
            # Certain constraints are conditionally dependent on values and are
            # not easily encoded in the schema, so catch them here.
            # Moreover, although we check for the existence of input files in
            # stage_input_files, we cannot encode file existence in json schema
            # to check the python_script script: tag before compile time.
            print('Failed to compile', yaml_path)
            return {"error": str(e)}
        rose_tree = compiler_info.rose

    rose_tree = ast.inline_subworkflow_cwl(rose_tree)

    sub_node_data: NodeData = rose_tree.data
    yaml_stem = sub_node_data.name
    cwl_tree = sub_node_data.compiled_cwl
    yaml_inputs = sub_node_data.workflow_inputs_file

    steps = cwl_tree['steps']
    # steps_keys = utils.get_steps_keys([step for step in steps])
    steps_keys = [step for step in steps]

    cwl_tree_no_dd = labshare.remove_dot_dollar(cwl_tree)
    yaml_inputs_no_dd = labshare.remove_dot_dollar(yaml_inputs)

    # Replace 'run' with plugin:id
    cwl_tree_run = copy.deepcopy(cwl_tree_no_dd)
    for i, step_key in enumerate(steps_keys):
        stem = Path(step_key).stem
        version = '{__version__}'
        step_name_i = step_key
        # step_name_i = utils.step_name_str(yaml_stem, i, step_key)
        step_name_i = step_name_i.replace('.yml', '_yml')  # Due to calling remove_dot_dollar above
        step_name_stripped = step_name_i.split("__")[-1]
        version_key = f"({i+1}, {step_name_stripped})"
        if version_key in wic_obj["wic"]["steps"]:
            version = wic_obj["wic"]["steps"][version_key]["version"]
        run_val = f'plugin:{stem}:{version}'
        cwl_tree_run['steps'][step_name_i]['run'] = run_val

    compute_workflow = {
        "name": yaml_stem,
        "driver": "argo",
        "cwlJobInputs": yaml_inputs_no_dd,
        "cwlScript": { **cwl_tree_run }
    }
    return compute_workflow


if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=3000)
