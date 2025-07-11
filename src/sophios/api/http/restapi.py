from pathlib import Path
import argparse
import copy
import yaml


import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware

from sophios import __version__, compiler
from sophios import run_local, input_output
from sophios.utils_graphs import get_graph_reps
from sophios.utils_yaml import wic_loader
from sophios import utils_cwl
from sophios.post_compile import cwl_inline_runtag, remove_entrypoints
from sophios.cli import get_args
from sophios.wic_types import CompilerInfo, Json, Tool, Tools, StepId, YamlTree, Cwl, NodeData
from sophios.api.utils import converter
import sophios.plugins as plugins
# from .auth.auth import authenticate


# helper functions


def remove_dot_dollar(tree: Cwl) -> Cwl:
    """Removes . and $ from dictionary keys, e.g. $namespaces and $schemas. Otherwise, you will get
    {'error': {'statusCode': 500, 'message': 'Internal Server Error'}}
    This is due to MongoDB:
    See https://www.mongodb.com/docs/manual/reference/limits/#Restrictions-on-Field-Names
    Args:
        tree (Cwl): A Cwl document
    Returns:
        Cwl: A Cwl document with . and $ removed from $namespaces and $schemas
    """
    tree_str = str(yaml.dump(tree, sort_keys=False, line_break='\n', indent=2))
    tree_str_no_dd = tree_str.replace('$namespaces', 'namespaces').replace(
        '$schemas', 'schemas').replace('.wic', '_wic')
    tree_no_dd: Cwl = yaml.load(tree_str_no_dd, Loader=wic_loader())  # This effectively copies tree
    return tree_no_dd


def run_workflow(compiler_info: CompilerInfo, args: argparse.Namespace) -> int:
    """
    Get the Sophios yaml tree from incoming JSON
    Args:
        req (JSON): A raw JSON content of incoming JSON object
    Returns:
        Cwl: A Cwl document with . and $ removed from $namespaces and $schemas
    """
    # ========= WRITE OUT =======================
    input_output.write_to_disk(compiler_info.rose, Path('autogenerated/'), relative_run_path=True)
    # ======== TEST RUN =========================
    retval = run_local.run_local(args, compiler_info.rose, args.cachedir, 'cwltool', False)
    return retval


app = FastAPI()

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", status_code=status.HTTP_200_OK)
# @authenticate
async def root(request: Request) -> Json:
    """The api has 1 route: compile

    Returns:
        Dict[str, str]: {"message": "The api has 1 route: compile"}
    """
    return {"message": "The api has 1 route: compile"}


@app.post("/compile")
# @authenticate
async def compile_wf(request: Request) -> Json:
    """The compile route compiles the json object from http request object built elsewhere

    Args:
        request (Request): request object built elsewhere

    Returns:
        compute_workflow (JSON): workflow json object ready to submit to compute
    """
    print('---------- Compile Workflow! ---------')
    # ========= PROCESS REQUEST OBJECT ==========
    req: Json = await request.json()
    suppliedargs = ['--generate_cwl_workflow']
    # clean up and convert the incoming object
    # schema preserving
    req = converter.update_payload_missing_inputs_outputs(req)
    wfb_payload = converter.raw_wfb_to_lean_wfb(req)
    # schema non-preserving
    workflow_temp = converter.wfb_to_wic(wfb_payload, req["plugins"])
    wkflw_name = "workflow_"
    args = get_args(wkflw_name, suppliedargs)

    # Build canonical workflow object
    workflow_can = utils_cwl.desugar_into_canonical_normal_form(workflow_temp)

    # ========= BUILD WIC COMPILE INPUT =========
    # Build a list of CLTs
    # The default list
    tools_cwl: Tools = {}
    global_config = input_output.get_config(Path(args.config_file), Path(args.homedir)/'wic'/'global_config.json')
    tools_cwl = plugins.get_tools_cwl(global_config,
                                      args.validate_plugins,
                                      not args.no_skip_dollar_schemas,
                                      args.quiet)
    # Add to the default list if the tool is 'inline' in run tag
    # run tag will have the actual CommandLineTool
    for can_step in workflow_can["steps"]:
        if can_step.get("run", None):
            # add a new tool
            tools_cwl[StepId(can_step["id"], "global")] = Tool(".", can_step["run"])
    wic_obj = {'wic': workflow_can.get('wic', {})}
    plugin_ns = wic_obj['wic'].get('namespace', 'global')

    graph = get_graph_reps(wkflw_name)
    yaml_tree: YamlTree = YamlTree(StepId(wkflw_name, plugin_ns), workflow_can)

    # ========= COMPILE WORKFLOW ================
    if req.get('run_local_env') == 'true':
        args.ignore_dir_path = False
    else:
        args.ignore_dir_path = True
    compiler_info: CompilerInfo = compiler.compile_workflow(yaml_tree, args, [], [graph], {}, {}, {}, {},
                                                            tools_cwl, True, relative_run_path=True, testing=False)

    rose_tree = compiler_info.rose
    # generating cwl inline within the 'run' tag is post compile
    # and always on when compiling and preparing REST return payload
    rose_tree = cwl_inline_runtag(rose_tree)
    if args.docker_remove_entrypoints:
        rose_tree = remove_entrypoints(args.container_engine, rose_tree)
    # ======== OUTPUT PROCESSING ================
    # ========= PROCESS COMPILED OBJECT =========
    sub_node_data: NodeData = rose_tree.data
    yaml_stem = sub_node_data.name
    cwl_tree = sub_node_data.compiled_cwl
    yaml_inputs = sub_node_data.workflow_inputs_file

    # Convert the compiled yaml file to json for labshare Compute.
    cwl_tree_run = copy.deepcopy(cwl_tree)
    cwl_tree_run['steps_dict'] = {}
    for step in cwl_tree_run['steps']:
        node_name = step['id']
        step.pop('id', None)
        step = {node_name: step}
        step_copy = copy.deepcopy(step)
        cwl_tree_run['steps_dict'].update(step_copy)

    cwl_tree_run.pop('steps', None)
    cwl_tree_run['steps'] = cwl_tree_run.pop('steps_dict', None)
    compute_workflow: Json = {}
    compute_workflow = {
        "name": yaml_stem,
        "cwlJobInputs": yaml_inputs,
        **cwl_tree_run
    }
    compute_workflow["retval"] = str(0)
    return compute_workflow


if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=3000)
