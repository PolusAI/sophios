from pathlib import Path
import copy
import sys


import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
import yaml

from sophios import compiler
from sophios import input_output
from sophios import utils_cwl
from sophios.post_compile import inline_artifact_runs
from sophios.cli import get_args, get_dicts_for_compilation
from sophios.runtime_inputs import normalize_artifact_cwl, normalize_artifact_job_inputs
from sophios.wic_types import Json, Tools
from sophios.contrib import converter
from sophios import plugins
from sophios.ir import frontdoor


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
async def root() -> Json:
    """The api has 1 route: compile

    Returns:
        Dict[str, str]: {"message": "The api has 1 route: compile"}
    """
    return {"message": "The api has 1 route: compile"}


@app.post("/compile")
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
    # clean up and convert the incoming object
    # schema preserving
    req = converter.update_payload_missing_inputs_outputs(req)
    wfb_payload = converter.raw_wfb_to_lean_wfb(req)
    # schema non-preserving
    workflow_temp = converter.wfb_to_wic(wfb_payload, req["plugins"])
    wkflw_name = "workflow_"
    args = get_args(wkflw_name)  # Mock CLI args

    # Build canonical workflow object
    workflow_can = utils_cwl.desugar_into_canonical_normal_form(workflow_temp)

    # ========= BUILD WIC COMPILE INPUT =========
    # Build a list of CLTs
    # The default list
    tools_cwl: Tools = {}
    global_config = input_output.get_config(args.config_file, Path(args.homedir))
    tools_cwl = plugins.get_tools_cwl(global_config, args.validate_plugins, args.quiet)

    # From the arguments this endpoint actually built, not a fresh default
    # parse: re-deriving configuration that is already in hand is how the two drift.
    compiler_options, _graph_settings = get_dicts_for_compilation(args)

    # ========= COMPILE WORKFLOW ================
    bundle = frontdoor.bundle_from_source(
        yaml.safe_dump(workflow_can, sort_keys=False), wkflw_name, {}, tools_cwl)
    result = compiler.compile_source(bundle, compiler_options, relative_run_path=True, testing=False)
    if result.realtime:
        analyses = ', '.join(declaration.analysis for declaration in result.realtime)
        print(f'Real-time analysis runs only with --run_local; the returned workflow has no step for {analyses}',
              file=sys.stderr)
    # generating cwl inline within the 'run' tag is post compile
    # and always on when compiling and preparing REST return payload
    artifact = inline_artifact_runs(result.artifact)
    # ======== OUTPUT PROCESSING ================
    # ========= PROCESS COMPILED OBJECT =========
    yaml_stem = artifact.name
    cwl_tree = normalize_artifact_cwl(artifact)
    yaml_inputs = normalize_artifact_job_inputs(artifact, artifact.job_inputs)

    # Convert the compiled yaml file to json for Compute API.
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
    compute_workflow: Json = {
        "name": yaml_stem,
        "cwlJobInputs": yaml_inputs,
        **cwl_tree_run
    }
    compute_workflow["retval"] = str(0)
    return compute_workflow


if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=3000)
