
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
        # Here goes the ICT to CLT extraction logic
        inp_restrict = inp_inter['state']
    return inp_restrict


def raw_wfb_to_lean_wfb(inp: Json) -> Json:
    """Drop all the unnecessary info from incoming wfb object"""
    if validate_schema_and_object(SCHEMA, inp):
        print('incoming object is valid against input object schema')
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


def wfb_to_wic(inp: Json) -> Cwl:
    """Convert lean wfb json to compliant wic"""
    # non-schema preserving changes
    inp_restrict = copy.deepcopy(inp)

    for node in inp_restrict['nodes']:
        if node.get('settings'):
            node['in'] = node['settings'].get('inputs')
            if node['settings'].get('outputs'):
                node['out'] = list({k: yaml.load('!& ' + v, Loader=wic_loader())} for k, v in node['settings']
                                   ['outputs'].items())  # outputs always have to be list
            # remove these (now) superfluous keys
            node.pop('settings', None)
            node.pop('pluginId', None)
            node.pop('internal', None)

    # setting the inputs of the non-sink nodes i.e. whose input doesn't depend on any other node's output
    # first get all target node ids
    target_node_ids = []
    for edg in inp_restrict['links']:
        target_node_ids.append(edg['targetId'])
    # now set inputs on non-sink nodes as inline input '!ii '
    # if inputs exist
    non_sink_nodes = [node for node in inp_restrict['nodes'] if node['id'] not in target_node_ids]
    for node in non_sink_nodes:
        if node.get('in'):
            for nkey in node['in']:
                node['in'][nkey] = yaml.load('!ii ' + node['in'][nkey], Loader=wic_loader())

    # After outs are set
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
                tgt_node['in'][sk] = yaml.load('!* ' + tgt_node['in'][sk], Loader=wic_loader())
            # the inputs which aren't dependent on previous/other steps
            # they are by default inline input
            diff_keys = set(tgt_in_keys) - set(src_out_keys)
            for dfk in diff_keys:
                tgt_node['in'][dfk] = yaml.load('!ii ' + tgt_node['in'][dfk], Loader=wic_loader())

    for node in inp_restrict['nodes']:
        node['id'] = node['name']  # just reuse name as node's id, wic id is same as wfb name
        node.pop('name', None)

    workflow_temp: Cwl = {}
    if inp_restrict["links"] != []:
        workflow_temp["steps"] = []
        for node in inp_restrict["nodes"]:
            workflow_temp["steps"].append(node)  # node["cwlScript"]  # Assume dict form
    else:  # A single node workflow
        node = inp_restrict["nodes"][0]
        workflow_temp = node["cwlScript"]
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
