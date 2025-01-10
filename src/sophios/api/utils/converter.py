
import copy
from pathlib import Path
from typing import Any, Dict, List, Union
import json
import yaml
from jsonschema import Draft202012Validator
from sophios.utils_yaml import wic_loader

from sophios.wic_types import Json, Cwl
from sophios.api.utils.ict.ict_spec.model import ICT
from sophios.api.utils.ict.ict_spec.cast import cast_to_ict
from sophios.api.utils.wfb_util import get_node_config

SCHEMA_FILE = Path(__file__).parent / "input_object_schema.json"
SCHEMA: Json = {}
with open(SCHEMA_FILE, 'r', encoding='utf-8') as f:
    SCHEMA = json.load(f)


def del_irrelevant_keys(ldict: List[Dict[Any, Any]], relevant_keys: List[Any]) -> None:
    """deletes irrelevant keys from every dict in the list of dicts"""
    for elem in ldict:
        ekeys = list(elem.keys())
        for ek in ekeys:
            if ek not in relevant_keys:
                # delete the key if it exists
                elem.pop(ek, None)


def validate_schema_and_object(schema: Json, jobj: Json) -> bool:
    """Validate schema object"""
    Draft202012Validator.check_schema(schema)
    df2012 = Draft202012Validator(schema)
    return df2012.is_valid(jobj)


def extract_state(inp: Json) -> Json:
    """Extract only the state information from the incoming wfb object.
       It includes converting "ICT" nodes to "CLT" using "plugins" tag of the object.
    """
    inp_restrict: Json = {}
    if not inp.get('plugins'):
        inp_restrict = copy.deepcopy(inp['state'])
    else:
        inp_inter = copy.deepcopy(inp)
        # drop all 'internal' nodes and all edges with 'internal' nodes
        step_nodes = [snode for snode in inp['state']['nodes'] if not snode['internal']]
        step_node_ids = [step_node['id'] for step_node in step_nodes]
        step_edges = [edg for edg in inp_inter['state']['links'] if edg['sourceId']
                      in step_node_ids and edg['targetId'] in step_node_ids]
        # overwrite 'links' and 'nodes'
        inp_inter['state'].pop('nodes', None)
        inp_inter['state'].pop('links', None)
        inp_inter['state']['nodes'] = step_nodes
        inp_inter['state']['links'] = step_edges
        # massage the plugins
        plugins = inp_inter['plugins']

        # Here goes the ICT to CLT extraction logic
        for node in inp_inter['state']['nodes']:
            node_pid = node["pluginId"]
            plugin = next((ict for ict in plugins if ict['pid'] == node_pid), None)
            clt: Json = {}
            if plugin:
                # by default have network access true
                # so we don't get runtime error for docker/container pull
                clt = ict_to_clt(plugin, True)
                # just have the clt payload in run
                node['run'] = clt
        inp_restrict = inp_inter['state']
    return inp_restrict


def raw_wfb_to_lean_wfb(inp: Json) -> Json:
    """Drop all the unnecessary info from incoming wfb object"""
    inp_restrict = extract_state(inp)
    keys = list(inp_restrict.keys())
    # To avoid deserialization
    # required attributes from schema
    prop_req = SCHEMA['definitions']['State']['required']
    nodes_req = SCHEMA['definitions']['NodeX']['required']
    links_req = SCHEMA['definitions']['Link']['required']
    do_not_rem_nodes_prop = ['cwlScript', 'run']
    do_not_rem_links_prop: list = []

    for k in keys:
        if k not in prop_req:
            del inp_restrict[k]
        elif k == 'links':
            lems = inp_restrict[k]
            rel_links_keys = links_req + do_not_rem_links_prop
            del_irrelevant_keys(lems, rel_links_keys)
        elif k == 'nodes':
            nems = inp_restrict[k]
            rel_nodes_keys = nodes_req + do_not_rem_nodes_prop
            del_irrelevant_keys(nems, rel_nodes_keys)
        else:
            pass

    return inp_restrict


def get_topological_order(links: list[dict[str, str]]) -> list[str]:
    """Get topological order of the nodes from links"""
    # Create adjacency list representation
    graph: dict[str, list[str]] = {}
    in_degree: dict[str, int] = {}

    # Initialize all nodes with 0 in-degree
    for link in links:
        source = link['sourceId']
        target = link['targetId']
        if source not in graph:
            graph[source] = []
        if target not in graph:
            graph[target] = []
        if source not in in_degree:
            in_degree[source] = 0
        if target not in in_degree:
            in_degree[target] = 0

    # Build the graph and count in-degrees
    for link in links:
        source = link['sourceId']
        target = link['targetId']
        graph[source].append(target)
        in_degree[target] += 1

    # Initialize queue with nodes that have 0 in-degree
    queue: list[str] = []
    for node in in_degree:
        if in_degree[node] == 0:
            queue.append(node)

    # Process the queue
    result: list[str] = []
    while queue:
        node = queue.pop(0)
        result.append(node)

        # Reduce in-degree of neighbors
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    return result


def wfb_to_wic(inp: Json, plugins: List[dict[str, Any]]) -> Cwl:
    """Convert lean wfb json to compliant wic"""
    # non-schema preserving changes
    inp_restrict = copy.deepcopy(inp)
    plugin_config_map: dict[str, dict] = {}
    for plugin in plugins:
        pid: str = plugin.get("pid", "")
        if pid == "":
            continue
        plugin_config_map[pid] = get_node_config(plugin)

    for node in inp_restrict['nodes']:
        if node.get('settings'):
            node['in'] = node['settings'].get('inputs')
            if node['settings'].get('outputs'):
                node['out'] = list({k: yaml.load('!& ' + v, Loader=wic_loader())} for k, v in node['settings']
                                   ['outputs'].items())  # outputs always have to be list
            # remove these (now) superfluous keys
            node.pop('name', None)
            node.pop('internal', None)

    # setting the inputs of the non-sink nodes i.e. whose input doesn't depend on any other node's output
    # first get all target node ids
    target_node_ids = []
    for edg in inp_restrict['links']:
        target_node_ids.append(edg['targetId'])
    # keep track of all the args that processed
    node_arg_map: dict[int, set] = {}
    # now set inputs on non-sink nodes as inline input '!ii '
    # if inputs exist
    non_sink_nodes = [node for node in inp_restrict['nodes'] if node['id'] not in target_node_ids]
    for node in non_sink_nodes:
        if node["id"] not in node_arg_map:
            node_arg_map[node['id']] = set()
        if node.get('in'):
            for nkey in node['in']:
                if str(node['in'][nkey]) != "":
                    node['in'][nkey] = yaml.load('!ii ' + str(node['in'][nkey]), Loader=wic_loader())
                    node_arg_map[node['id']].add(nkey)

    if plugins != []:  # use the look up logic similar to WFB
        for edg in inp_restrict['links']:
            # links = edge. nodes and edges is the correct terminology!
            src_id = edg['sourceId']
            tgt_id = edg['targetId']
            src_node = next((node for node in inp_restrict['nodes'] if node['id'] == src_id), None)
            tgt_node = next((node for node in inp_restrict['nodes'] if node['id'] == tgt_id), None)
            assert src_node, f'output(s) of source node of edge{edg} must exist!'
            assert tgt_node, f'input(s) of target node of edge{edg} must exist!'
            if src_id not in node_arg_map:
                node_arg_map[src_id] = set()

            if tgt_id not in node_arg_map:
                node_arg_map[tgt_id] = set()

            src_node_ui_config = plugin_config_map.get(src_node['pluginId'], None)
            tgt_node_ui_config = plugin_config_map.get(tgt_node['pluginId'], None)
            if src_node_ui_config and tgt_node_ui_config:
                inlet_index = edg['inletIndex']
                outlet_index = edg['outletIndex']

                src_node_out_arg = src_node_ui_config['outputs'][outlet_index]["name"]
                tgt_node_in_arg = tgt_node_ui_config['inputs'][inlet_index]["name"]

                if tgt_node.get('in'):
                    source_output = src_node['out'][0][src_node_out_arg]
                    if isinstance(source_output, dict) and 'wic_anchor' in source_output:
                        source_output = source_output["wic_anchor"]
                    tgt_node['in'][tgt_node_in_arg] = yaml.load('!* ' + str(source_output), Loader=wic_loader())
                    node_arg_map[tgt_id].add(tgt_node_in_arg)

        for node in inp_restrict['nodes']:
            output_dict = node['settings'].get('outputs', {})
            for key in output_dict:
                if str(output_dict[key]) != "":
                    node['in'][key] = yaml.load('!ii ' + str(output_dict[key]), Loader=wic_loader())
                    node_arg_map[node['id']].add(key)
            node.pop('settings', None)

            if "in" in node:
                unprocessed_args = set(node['in'].keys())
                if node['id'] in node_arg_map:
                    unprocessed_args = unprocessed_args.difference(node_arg_map[node['id']])
                for arg in unprocessed_args:
                    node['in'][arg] = yaml.load('!ii ' + str(node['in'][arg]), Loader=wic_loader())
    else:  # No plugins, use the old logic
        # this logic is most likely not correct and need to be scrubbed
        # along with updating the non_wfb dummy tests
        for node in inp_restrict['nodes']:
            node.pop('settings', None)

        for edg in inp_restrict['links']:
            # links = edge. nodes and edges is the correct terminology!
            src_id = edg['sourceId']
            tgt_id = edg['targetId']
            src_node = next((node for node in inp_restrict['nodes'] if node['id'] == src_id), None)
            tgt_node = next((node for node in inp_restrict['nodes'] if node['id'] == tgt_id), None)
            assert src_node, f'output(s) of source node of edge{edg} must exist!'
            assert tgt_node, f'input(s) of target node of edge{edg} must exist!'
            # flattened list of keys
            if src_node.get('out') and tgt_node.get('in'):
                src_out_keys = [sk for sout in src_node['out'] for sk in sout.keys()]
                tgt_in_keys = tgt_node['in'].keys()
                # we match the source output tag type to target input tag type
                # and connect them through '!* ' for input, all outputs are '!& ' before this
                for sk in src_out_keys:
                    # It maybe possible that (explicit) outputs of src nodes might not have corresponding
                    # (explicit) inputs in target node
                    if tgt_node['in'].get(sk):
                        # if the output is a dict, it is a wic_anchor, so we need to get the anchor
                        # and use that as the input
                        src_node_out_arg = src_node['out'][0][sk]
                        source_output = src_node['out'][0][sk]
                        if isinstance(source_output, dict) and 'wic_anchor' in source_output:
                            source_output = source_output["wic_anchor"]
                        tgt_node['in'][sk] = yaml.load('!* ' + str(source_output), Loader=wic_loader())
                # the inputs which aren't dependent on previous/other steps
                # they are by default inline input
                diff_keys = set(tgt_in_keys) - set(src_out_keys)
                for dfk in diff_keys:
                    tgt_node['in'][dfk] = yaml.load('!ii ' + str(tgt_node['in'][dfk]), Loader=wic_loader())

    workflow_temp: Cwl = {}
    if inp_restrict["links"] != []:
        node_order = get_topological_order(inp_restrict["links"])
        workflow_temp["steps"] = []
        for id in node_order:
            node = next((n for n in inp_restrict["nodes"] if n["id"] == id), None)
            if node:
                # just reuse name as node's pluginId, wic id is same as wfb name
                node['id'] = node['pluginId'].split('@')[0].replace('/', '_')
                node.pop('pluginId', None)
                workflow_temp["steps"].append(node)
    else:  # A single node workflow
        node = inp_restrict["nodes"][0]
        node['id'] = node['pluginId'].split('@')[0].replace('/', '_')
        node.pop('pluginId', None)
        if node.get("cwlScript"):
            workflow_temp = node["cwlScript"]
        else:
            workflow_temp["steps"] = []
            workflow_temp["steps"].append(node)
    return workflow_temp


def ict_to_clt(ict: Union[ICT, Path, str, dict], network_access: bool = False) -> dict:
    """
    Convert ICT to CWL CommandLineTool

    Args:
        ict (Union[ICT, Path, str, dict]): ICT to convert to CLT. ICT can be an ICT object,
        a path to a yaml file, or a dictionary containing ICT

    Returns:
        dict: A dictionary containing the CLT
    """

    ict_local = ict if isinstance(ict, ICT) else cast_to_ict(ict)

    return ict_local.to_clt(network_access=network_access)


def update_payload_missing_inputs_outputs(wfb_data: Json) -> Json:
    """Update payload with missing inputs and outputs using links"""

    # ensure the incoming wfb data is valid
    if validate_schema_and_object(SCHEMA, wfb_data):
        print('incoming object is valid against input object schema')

    # return if no plugins are found in data
    if not wfb_data['plugins']:
        return wfb_data

    wfb_data_copy = copy.deepcopy(wfb_data)

    links = wfb_data_copy["state"]["links"]
    nodes = wfb_data_copy["state"]["nodes"]
    plugins = wfb_data_copy["plugins"]

    if plugins != []:
        plugin_config_map: dict[str, dict] = {}
        plugin_output_map: dict[str, list] = {}
        for plugin in plugins:
            pid: str = plugin.get("pid", "")
            if pid == "":
                continue
            plugin_config_map[pid] = get_node_config(plugin)
            plugin_output_map[pid] = plugin.get("outputs", [])

        # add missing outputs
        for node in nodes:
            base_name = node["name"].replace(" ", "").lower() + "_" + str(node["id"])
            if "outputs" not in node["settings"]:
                node["settings"]["outputs"] = {}
            node_outputs = node["settings"]["outputs"]
            plugin_outputs: list = plugin_output_map.get(node["pluginId"], [])
            for output in plugin_outputs:
                if output["type"] == "path" and output["name"] not in node_outputs:
                    node_outputs[output["name"]] = base_name + "-" + output["name"]

        # hashmap of node id to nodes for fast node lookup
        nodes_dict = {node['id']: node for node in nodes}
        # use the look up logic similar to WFB to connect inputs and outputs
        for link in links:
            src_node = nodes_dict[link['sourceId']]
            tgt_node = nodes_dict[link['targetId']]

            src_node_ui_config = plugin_config_map.get(src_node['pluginId'], None)
            tgt_node_ui_config = plugin_config_map.get(tgt_node['pluginId'], None)

            if src_node_ui_config and tgt_node_ui_config:
                inlet_index = link['inletIndex']
                outlet_index = link['outletIndex']
                src_node_out_arg = src_node_ui_config['outputs'][outlet_index]["name"]
                tgt_node_in_arg = tgt_node_ui_config['inputs'][inlet_index]["name"]
                tgt_node["settings"]["inputs"][tgt_node_in_arg] = src_node["settings"]["outputs"][src_node_out_arg]

    if validate_schema_and_object(SCHEMA, wfb_data_copy):
        print('Updated object is valid against input object schema')

    return wfb_data_copy
