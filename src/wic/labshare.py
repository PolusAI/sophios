import argparse
import copy
from pathlib import Path
from typing import Dict, List

import requests
import yaml

from .wic_types import KV, Cwl, NodeData, RoseTree, Tools
from . import utils, __version__


def delete_previously_uploaded(args: argparse.Namespace, plugins_or_pipelines: str, name: str) -> None:
    """Delete plugins/pipelines previously uploaded to labshare.

    Args:
        args (argparse.Namespace): The command line arguments
        name (str): 'plugins' or 'pipelines'
    """
    access_token = args.compute_access_token

    response = requests.delete(args.compute_url + f'/compute/{plugins_or_pipelines}/' + name + ':' + __version__,
                                headers = {'Authorization': f'Bearer {access_token}'})
    # TODO: Check response for success


def remove_dollar(tree: Cwl) -> Cwl:
    """Removes $ from $namespaces and $schemas. Otherwise, you will get
    {'error': {'statusCode': 500, 'message': 'Internal Server Error'}}

    Args:
        tree (Cwl): A Cwl document

    Returns:
        Cwl: A Cwl document with $ removed from $namespaces and $schemas
    """
    tree_str = str(yaml.dump(tree, sort_keys=False, line_break='\n', indent=2))
    tree_str_no_dollar = tree_str.replace('$namespaces', 'namespaces').replace('$schemas', 'schemas')
    tree_no_dollar: Cwl = yaml.safe_load(tree_str_no_dollar)  # This effectively copies tree
    return tree_no_dollar


def pretty_print_request(request: requests.PreparedRequest) -> None:
    """pretty prints a requests.PreparedRequest

    Args:
        request (requests.PreparedRequest): The request to be printed
    """
    print('request.headers', request.headers)
    body = request.body
    if body is not None:
        if isinstance(body, bytes):
            body_str = body.decode('utf-8')
        else:
            body_str = body
        print('request.body\n', yaml.dump(yaml.safe_load(body_str)))
    else:
        print('request.body is None')


def upload_plugin(compute_url: str, access_token: str, tool: Cwl, name: str) -> str:
    """Uploads CWL CommandLineTools to Polus Compute

    Args:
        compute_url (str): The url to the Compute API
        access_token (str): The access token used for authentication
        tool (Cwl): The CWL CommandLineTool
        name (str): The name of the CWL CommandLineTool

    Raises:
        Exception: If the upload failed for any reason

    Returns:
        str: The unique id of the plugin
    """
    # Convert the compiled yaml file to json for labshare Compute.
    tool_no_dollar = remove_dollar(tool)
    compute_plugin: KV = {
        'name': name,
        # TODO: Using the WIC version works for now, but since the plugins
        # are supposed to be independent, they should have their own versions.
        # For biobb, we can extract the version from dockerPull
        'version': __version__,
        'cwlScript': tool_no_dollar
    }

    # Use http POST request to upload a primitive CommandLineTool / define a plugin and get its id hash.
    response = requests.post(compute_url + '/compute/plugins',
                             headers = {'Authorization': f'Bearer {access_token}'},
                             json = compute_plugin)
    r_json = response.json()

    # {'error': {'statusCode': 422, 'name': 'UnprocessableEntityError',
    # 'message': 'A Plugin with name pdb and version ... already exists.'}}
    if r_json.get('error', {}).get('statusCode', {}) == 422:
        return '-1'

    if 'id' not in r_json:
        pretty_print_request(response.request)
        print('post response')
        print(r_json)
        raise Exception(f'Error! Labshare plugin upload failed for {name}.')

    plugin_id: str = r_json['id'] # hash
    compute_plugin['id'] = plugin_id
    compute_plugin.update({'id': plugin_id}) # Necessary ?
    return plugin_id


def print_plugins(compute_url: str) -> None:
    """prints information on all currently available Compute plugins

    Args:
        compute_url (str): The url to the Compute API
    """
    r = requests.get(compute_url + '/compute/plugins/')
    for j in r.json():
        print(f"id {j.get('id')} class {j.get('class')} name {j.get('name')}")
        #print(j)
    print(len(r.json()))


def upload_all(rose_tree: RoseTree, tools: Tools, args: argparse.Namespace, is_root: bool) -> str:
    """Uploads all Plugins, Pipelines, and the root Workflow to the Compute platform

    Args:
        rose_tree (RoseTree): The data associated with compiled subworkflows
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl()
        args (argparse.Namespace): The command line arguments
        is_root (bool): True if this is the root workflow

    Raises:
        Exception: If any of the uploads fails for any reason

    Returns:
        str: The unique id of the workflow
    """
    access_token = args.compute_access_token
    #print('access_token', access_token)

    sub_node_data: NodeData = rose_tree.data
    yaml_stem = sub_node_data.name
    cwl_tree = sub_node_data.compiled_cwl
    yaml_inputs = sub_node_data.workflow_inputs_file

    sub_rose_trees: Dict[str, RoseTree] = dict([(r.data.name, r) for r in rose_tree.sub_trees])
    #print(list(sub_rose_trees))

    steps = cwl_tree['steps']

    # Get the dictionary key (i.e. the name) of each step.
    steps_keys: List[str] = []
    for step in steps:
        step_key = utils.parse_step_name_str(step)[-1]
        steps_keys.append(step_key)
    #print(steps_keys)

    #subkeys = [key for key in steps_keys if key not in tools]

    cwl_tree_no_dollar = remove_dollar(cwl_tree)

    # Convert the compiled yaml file to json for labshare Compute.
    # Replace 'run' with plugin:id
    cwl_tree_run = copy.deepcopy(cwl_tree_no_dollar)
    for i, step_key in enumerate(steps_keys):
        stem = Path(step_key).stem
        tool_i = tools[stem].cwl
        step_name_i = utils.step_name_str(yaml_stem, i, step_key)

        #if step_key in subkeys: # and not is_root, but the former implies the latter
            #plugin_id = upload_plugin(args.compute_url, access_token, cwl_tree_run, yaml_stem)
        if stem in sub_rose_trees:
            subworkflow_id = upload_all(sub_rose_trees[stem], tools, args, False)
            run_val = f'pipeline:{stem}:{__version__}'
        else:
            # i.e. If this is either a primitive CommandLineTool and/or
            # a 'primitive' Workflow that we did NOT recursively generate.
            delete_previously_uploaded(args, 'plugins', stem)
            plugin_id = upload_plugin(args.compute_url, access_token, tool_i, stem)
            run_val = f'plugin:{stem}:{__version__}'
        cwl_tree_run['steps'][step_name_i]['run'] = run_val

    workflow_id: str = ''
    if is_root:
        compute_workflow = {
            "name": yaml_stem,
            #"version": __version__, # no version for workflows
            "driver": "slurm",
            "cwlJobInputs": yaml_inputs,
            **cwl_tree_run
        }
        # Use http POST request to upload a complete Workflow (w/ inputs) and get its id hash.
        response = requests.post(args.compute_url + '/compute/workflows',
                                 headers = {'Authorization': f'Bearer {access_token}'},
                                 json = compute_workflow)
        r_json = response.json()
        print('post response')
        j = r_json
        print(f"id {j.get('id')} class {j.get('class')} name {j.get('name')}")
        if 'id' not in r_json:
            pretty_print_request(response.request)
            print(r_json)
            raise Exception(f'Error! Labshare workflow upload failed for {yaml_stem}.')
        workflow_id = r_json['id'] # hash
    else:
        #  "owner": "string",
        #  "additionalProp1": {}
        # TODO: Check this.
        compute_pipeline = {
            "name": yaml_stem,
            "version": __version__,
            **cwl_tree_run
        }
        # Need to add owner and/or additionalProp1 ?
        # Need to remove headers and/or requirements? i.e.
        #yaml_tree['cwlVersion'] = 'v1.2' # Use 1.2 to support conditional workflows
        #yaml_tree['class'] = 'Workflow'
        #yaml_tree['requirements'] = subworkreqdict

        delete_previously_uploaded(args, 'pipelines', yaml_stem)
        # Use http POST request to upload a subworkflow / "pipeline" (no inputs) and get its id hash.
        response = requests.post(args.compute_url + '/compute/pipelines',
                                 headers = {'Authorization': f'Bearer {access_token}'},
                                 json = compute_pipeline)
        r_json = response.json()
        print('post response')
        j = r_json
        print(f"id {j.get('id')} class {j.get('class')} name {j.get('name')}")
        if 'id' not in r_json:
            pretty_print_request(response.request)
            print(r_json)
            raise Exception(f'Error! Labshare workflow upload failed for {yaml_stem}.')
        workflow_id = r_json['id'] # hash
    #if is_root:
    #    print_plugins(args.compute_url)

    return workflow_id
