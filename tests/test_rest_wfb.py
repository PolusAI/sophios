import copy
import json
from pathlib import Path
import asyncio

from fastapi import Request

import pytest
from sophios.wic_types import Json


from sophios.apis.rest import api


def test_rest_multinode_wfb() -> None:
    """A multi node (with plugins) wfb -> sophios/restapi test"""
    inp_file = "multi_node_wfb.json"
    inp: Json = {}
    inp_path = Path(__file__).parent / 'rest_wfb_objects' / inp_file
    with open(inp_path, 'r', encoding='utf-8') as f:
        inp = json.load(f)
    print('----------- from rest api ----------- \n\n')
    scope = {}
    scope['type'] = 'http'

    async def receive() -> Json:
        inp_byte = json.dumps(inp).encode('utf-8')
        return {"type": "http.request", "body": inp_byte}

    # create a request object and pack it with our json payload
    req: Request = Request(scope)
    req._receive = receive
    res: Json = asyncio.run(api.compile_wf(req))  # call to rest api
    assert int(res['retval']) == 0
