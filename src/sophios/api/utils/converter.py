
import copy
from collections import deque
from pathlib import Path
from typing import Any
import json
import yaml
from jsonschema import Draft202012Validator
from sophios.utils_yaml import wic_loader, TAG_ANCHOR, TAG_ALIAS, TAG_INLINE_INPUT, KEY_ANCHOR

from sophios.wic_types import Json, Cwl, PluginNodeConfig
from sophios.api.utils.ict.ict_spec.model import ICT
from sophios.api.utils.ict.ict_spec.cast import cast_to_ict
from sophios.api.utils.wfb_util import get_node_config

SCHEMA_FILE = Path(__file__).parent / "input_object_schema.json"
SCHEMA: Json = {}
with open(SCHEMA_FILE, 'r', encoding='utf-8') as f:
    SCHEMA = json.load(f)


def del_irrelevant_keys(dicts: list[dict[Any, Any]], relevant_keys: list[Any]) -> None:
    """deletes irrelevant keys from every dict in the list of dicts"""
    for elem in dicts:
        keys = list(elem.keys())
        for key in keys:
            if key not in relevant_keys:
                # delete the key if it exists
                elem.pop(key, None)


def validate_schema_and_object(schema: Json, jobj: Json) -> bool:
    """Validate schema object"""
    Draft202012Validator.check_schema(schema)
    df2012 = Draft202012Validator(schema)
    return df2012.is_valid(jobj)


def normalize_plugin_ui(wfb_data: Json) -> None:
    """Normalize legacy plugin UI entries to the schema-backed WFB shape."""
    for plugin in wfb_data.get("plugins", []):
        inputs_by_name = {item.get("name"): item for item in plugin.get("inputs", [])}
        outputs_by_name = {item.get("name"): item for item in plugin.get("outputs", [])}

        for ui_item in plugin.get("ui", []):
            name = ui_item.get("name")
            if not isinstance(name, str):
                continue

            if "key" not in ui_item:
                prefix = "outputs" if name in outputs_by_name and name not in inputs_by_name else "inputs"
                ui_item["key"] = f"{prefix}.{name}"

            source = inputs_by_name.get(name) or outputs_by_name.get(name) or {}
            ui_item.setdefault("title", name)
            ui_item.setdefault("description", source.get("description", name))
            ui_item.setdefault("type", source.get("type", "string"))


def _find_by_id(items: list[Json], key: str, value: Any) -> Json | None:
    """Return the first item whose `key` field equals `value`, or None if none match."""
    return next((item for item in items if item[key] == value), None)


def extract_state(wfb_payload: Json) -> Json:
    """Extract only the state information from the incoming wfb object.
       It includes converting "ICT" nodes to "CLT" using "plugins" tag of the object.
    """
    inp_restrict: Json = {}
    if not wfb_payload.get('plugins'):
        inp_restrict = copy.deepcopy(wfb_payload['state'])
    else:
        inp_inter = copy.deepcopy(wfb_payload)
        # drop all 'internal' nodes and all edges with 'internal' nodes
        step_nodes = [snode for snode in wfb_payload['state']
                      ['nodes'] if not snode['internal']]
        step_node_ids = [step_node['id'] for step_node in step_nodes]
        step_edges = [edge for edge in inp_inter['state']['links'] if edge['sourceId']
                      in step_node_ids and edge['targetId'] in step_node_ids]
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
            plugin = _find_by_id(plugins, 'pid', node_pid)
            clt: Json = {}
            if plugin:
                # by default have network access true
                # so we don't get runtime error for docker/container pull
                clt = ict_to_clt(plugin, True)
                # just have the clt payload in run
                node['run'] = clt
        inp_restrict = inp_inter['state']
    return inp_restrict


def raw_wfb_to_lean_wfb(wfb_payload: Json) -> Json:
    """Drop all the unnecessary info from incoming wfb object"""
    inp_restrict = extract_state(wfb_payload)
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
            link_items = inp_restrict[k]
            rel_links_keys = links_req + do_not_rem_links_prop
            del_irrelevant_keys(link_items, rel_links_keys)
        elif k == 'nodes':
            node_items = inp_restrict[k]
            rel_nodes_keys = nodes_req + do_not_rem_nodes_prop
            del_irrelevant_keys(node_items, rel_nodes_keys)

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
    queue: deque[str] = deque(node for node, degree in in_degree.items() if degree == 0)

    # Process the queue
    result: list[str] = []
    while queue:
        node = queue.popleft()
        result.append(node)

        # Reduce in-degree of neighbors
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    return result


def _tag_value(tag: str, value: Any) -> dict:
    """Tag a value with a wic yaml tag (TAG_ANCHOR/TAG_ALIAS/TAG_INLINE_INPUT)."""
    tagged: dict = yaml.load(f'{tag} ' + str(value), Loader=wic_loader())
    return tagged


def _find_edge_nodes(edge: Json, nodes: list[Json]) -> tuple[Json, Json]:
    """Look up an edge's source and target nodes by id, asserting both exist."""
    src_node = _find_by_id(nodes, 'id', edge['sourceId'])
    tgt_node = _find_by_id(nodes, 'id', edge['targetId'])
    assert src_node, f'output(s) of source node of edge{edge} must exist!'
    assert tgt_node, f'input(s) of target node of edge{edge} must exist!'
    return src_node, tgt_node


def _resolve_wic_anchor(value: Any) -> Any:
    """Unwrap a `!&`-tagged (wic_anchor) output value to its underlying value."""
    if isinstance(value, dict) and KEY_ANCHOR in value:
        return value[KEY_ANCHOR]
    return value


def _build_plugin_config_map(plugins: list[dict[str, Any]]) -> dict[str, PluginNodeConfig]:
    """Map each plugin's pid to its UI-derived input/output configuration."""
    config_map: dict[str, PluginNodeConfig] = {}
    for plugin in plugins:
        pid: str = plugin.get("pid", "")
        if pid == "":
            continue
        config_map[pid] = get_node_config(plugin)
    return config_map


def wfb_to_wic(wfb_payload: Json, plugins: list[dict[str, Any]]) -> Cwl:
    """Convert lean wfb json to compliant wic"""
    def sanitize_step_id(raw: Any) -> str:
        """Normalize a candidate step id into a WIC-friendly identifier."""
        return str(raw).strip().replace(' ', '_').replace('/', '_')

    def node_to_step_id(node: Json) -> str:
        """Derive a stable non-empty WIC step id from a WFB node."""
        cached_step_id = sanitize_step_id(node.get('_wic_step_id', ''))
        if cached_step_id != '':
            return cached_step_id

        plugin_id = sanitize_step_id(
            str(node.get('pluginId', '')).split('@', maxsplit=1)[0])
        if plugin_id != '':
            return plugin_id

        name = sanitize_step_id(node.get('name', ''))
        if name != '':
            return name

        return f"step_{node.get('id', 'unknown')}"

    # non-schema preserving changes
    inp_restrict = copy.deepcopy(wfb_payload)
    plugin_config_map = _build_plugin_config_map(plugins)

    for node in inp_restrict['nodes']:
        node['_wic_step_id'] = node_to_step_id(node)
        if node.get('settings'):
            node['in'] = node['settings'].get('inputs')
            if node['settings'].get('outputs'):
                node['out'] = list({k: _tag_value(TAG_ANCHOR, v)} for k, v in node['settings']
                                   # outputs always have to be list
                                   ['outputs'].items())
            # remove these (now) superfluous keys
            node.pop('name', None)
            node.pop('internal', None)

    # setting the inputs of the non-sink nodes i.e. whose input doesn't depend on any other node's output
    # first get all target node ids
    target_node_ids = [edge['targetId'] for edge in inp_restrict['links']]
    # keep track of all the args that processed
    node_arg_map: dict[int, set] = {}
    # now set inputs on non-sink nodes as inline input '!ii '
    # if inputs exist
    non_sink_nodes = [node for node in inp_restrict['nodes']
                      if node['id'] not in target_node_ids]
    for node in non_sink_nodes:
        if node["id"] not in node_arg_map:
            node_arg_map[node['id']] = set()
        if node.get('in'):
            for nkey in node['in']:
                if str(node['in'][nkey]) != "":
                    node['in'][nkey] = _tag_value(TAG_INLINE_INPUT, node['in'][nkey])
                    node_arg_map[node['id']].add(nkey)

    if plugins != []:  # use the look up logic similar to WFB
        for edge in inp_restrict['links']:
            # links = edge. nodes and edges is the correct terminology!
            src_id = edge['sourceId']
            tgt_id = edge['targetId']
            src_node, tgt_node = _find_edge_nodes(edge, inp_restrict['nodes'])
            if src_id not in node_arg_map:
                node_arg_map[src_id] = set()

            if tgt_id not in node_arg_map:
                node_arg_map[tgt_id] = set()

            src_node_ui_config = plugin_config_map.get(src_node['pluginId'], None)
            tgt_node_ui_config = plugin_config_map.get(tgt_node['pluginId'], None)
            if src_node_ui_config and tgt_node_ui_config:
                inlet_index = edge['inletIndex']
                outlet_index = edge['outletIndex']

                src_node_out_arg = src_node_ui_config['outputs'][outlet_index]["name"]
                tgt_node_in_arg = tgt_node_ui_config['inputs'][inlet_index]["name"]

                if tgt_node.get('in'):
                    source_output = _resolve_wic_anchor(src_node['out'][0][src_node_out_arg])
                    tgt_node['in'][tgt_node_in_arg] = _tag_value(TAG_ALIAS, source_output)
                    node_arg_map[tgt_id].add(tgt_node_in_arg)

        for node in inp_restrict['nodes']:
            output_dict = node['settings'].get('outputs', {})
            for key in output_dict:
                if str(output_dict[key]) != "":
                    node['in'][key] = _tag_value(TAG_INLINE_INPUT, output_dict[key])
                    node_arg_map[node['id']].add(key)
            node.pop('settings', None)

            if "in" in node:
                unprocessed_args = set(node['in'].keys())
                if node['id'] in node_arg_map:
                    unprocessed_args = unprocessed_args.difference(
                        node_arg_map[node['id']])
                for arg in unprocessed_args:
                    node['in'][arg] = _tag_value(TAG_INLINE_INPUT, node['in'][arg])
    else:  # No plugins, use the node/link mapping directly.
        for node in inp_restrict['nodes']:
            node.pop('settings', None)

        for edge in inp_restrict['links']:
            src_node, tgt_node = _find_edge_nodes(edge, inp_restrict['nodes'])
            # flattened list of keys
            if src_node.get('out') and tgt_node.get('in'):
                src_out_keys = [sk for sout in src_node['out']
                                for sk in sout.keys()]
                tgt_in_keys = tgt_node['in'].keys()
                # we match the source output tag type to target input tag type
                # and connect them through '!* ' for input, all outputs are '!& ' before this
                for sk in src_out_keys:
                    # It maybe possible that (explicit) outputs of src nodes might not have corresponding
                    # (explicit) inputs in target node
                    if tgt_node['in'].get(sk):
                        # if the output is a dict, it is a wic_anchor, so we need to get the anchor
                        # and use that as the input
                        source_output = _resolve_wic_anchor(src_node['out'][0][sk])
                        tgt_node['in'][sk] = _tag_value(TAG_ALIAS, source_output)
                # the inputs which aren't dependent on previous/other steps
                # they are by default inline input
                diff_keys = set(tgt_in_keys) - set(src_out_keys)
                for dfk in diff_keys:
                    tgt_node['in'][dfk] = _tag_value(TAG_INLINE_INPUT, tgt_node['in'][dfk])

    workflow_temp: Cwl = {}
    if inp_restrict["links"] != []:
        node_order = get_topological_order(inp_restrict["links"])
        workflow_temp["steps"] = []
        for node_id in node_order:
            node = _find_by_id(inp_restrict["nodes"], "id", node_id)
            if node:
                # just reuse name as node's pluginId, wic id is same as wfb name
                node['id'] = node_to_step_id(node)
                node.pop('pluginId', None)
                node.pop('_wic_step_id', None)
                workflow_temp["steps"].append(node)
    else:  # A single node workflow
        node = inp_restrict["nodes"][0]
        node['id'] = node_to_step_id(node)
        node.pop('pluginId', None)
        node.pop('_wic_step_id', None)
        if node.get("cwlScript"):
            workflow_temp = node["cwlScript"]
        else:
            workflow_temp["steps"] = []
            workflow_temp["steps"].append(node)
    return workflow_temp


def ict_to_clt(ict: ICT | Path | str | dict, network_access: bool = False) -> dict:
    """
    Convert ICT to CWL CommandLineTool

    Args:
        ict (ICT | Path | str | dict): ICT to convert to CLT. ICT can be an ICT object,
        a path to a yaml file, or a dictionary containing ICT

    Returns:
        dict: A dictionary containing the CLT
    """

    ict_local = ict if isinstance(ict, ICT) else cast_to_ict(ict)

    return ict_local.to_clt(network_access=network_access)


def update_payload_missing_inputs_outputs(wfb_data: Json) -> Json:
    """Update payload with missing inputs and outputs using links"""

    wfb_data_copy = copy.deepcopy(wfb_data)
    normalize_plugin_ui(wfb_data_copy)

    if not validate_schema_and_object(SCHEMA, wfb_data_copy):
        raise ValueError("Incoming WFB payload does not match the input object schema.")

    if not wfb_data_copy['plugins']:
        return wfb_data_copy

    links = wfb_data_copy["state"]["links"]
    nodes = wfb_data_copy["state"]["nodes"]
    plugins = wfb_data_copy["plugins"]

    if plugins != []:
        plugin_config_map = _build_plugin_config_map(plugins)
        plugin_output_map: dict[str, list] = {}
        for plugin in plugins:
            pid: str = plugin.get("pid", "")
            if pid == "":
                continue
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

    if not validate_schema_and_object(SCHEMA, wfb_data_copy):
        raise ValueError("Updated WFB payload does not match the input object schema.")

    return wfb_data_copy
