# Contrib: REST API, ICT conversion, payload fixing

What else ships in the `sophios` package besides the compiler and the two
authoring forms? Three peripheral surfaces, under `sophios.contrib`, kept
working and deliberately outside the core: the core never imports them, and
nothing on the other pages depends on them. They serve a graphical workflow
builder that composes tools described by ICT files and sends the result to be
compiled.

## The REST API

`sophios.contrib.rest.api` is a FastAPI application with one route,
`POST /compile`. Start it with uvicorn:

```bash
uvicorn sophios.contrib.rest.api:app --host 0.0.0.0 --port 3000
```

`python -m sophios.contrib.rest.api` starts it the same way, on port 3000.

The route takes a workflow-builder payload as JSON: the `plugins` the
builder knows (each with its inputs, outputs and UI entries), the nodes
placed on the canvas with their settings, and the links between them. It
converts the payload to a `.wic` document in memory, compiles that with the
tools found through the configuration the server's user has
(`~/wic/global_config.json`), and returns the compiled workflow as JSON:

```json
{
  "name": "workflow_",
  "cwlJobInputs": {"...": "..."},
  "steps": {"echo": {"...": "..."}},
  "inputs": {"...": "..."},
  "outputs": {"...": "..."},
  "retval": "0"
}
```

The returned document is the compiled CWL with every `run:` embedded, as
`--cwl_inline_runtag` writes it, and with `steps` as a mapping keyed by step
id rather than CWL's list. A payload that declares a real-time analysis is
compiled without it, and the server prints a line saying which analysis the
returned workflow has no step for, since an analysis runs only under
`--run_local`. A payload the compiler rejects is an HTTP 500 with no body to
read; the diagnostics are in the server's log. The sample payloads under `tests/contrib/rest_wfb_objects/` in
the repository show the shape the route accepts.

## ICT to CommandLineTool

An ICT (Interoperable Computational Tool) description names a containerised
tool: its inputs and outputs with their types, the container image, and the
UI entries a builder shows. `sophios.contrib.converter.ict_to_clt` turns one
into a CWL `CommandLineTool` document:

```python
from sophios.contrib.converter import ict_to_clt

ict = {
    'specVersion': '1.0.0', 'name': 'example/word-count', 'version': '0.1.0',
    'title': 'Word count', 'description': 'Count the words in each file of a directory.',
    'container': 'example/word-count:0.1.0', 'entrypoint': 'wc -w',
    'author': ['A. Author'], 'contact': 'a@example.org', 'repository': 'https://example.org/word-count',
    'inputs': [{'name': 'inpDir', 'type': 'path', 'description': 'The files to count',
                'required': True, 'format': ['genericData']}],
    'outputs': [{'name': 'outDir', 'type': 'path', 'description': 'The counts',
                 'required': True, 'format': ['genericData']}],
    'ui': [{'key': 'inputs.inpDir', 'title': 'Input directory',
            'description': 'The files to count', 'type': 'path'}],
}
clt = ict_to_clt(ict, network_access=False)
print(clt['inputs'], clt['requirements'])
```

The argument is a path to a JSON or YAML file, a mapping as above, or an
`ICT` object from `sophios.contrib.ict.ict_spec`. Each ICT input and output
becomes a tool input with `--<name>` as its prefix (`path` is a `Directory`),
each ICT output also a `Directory` output collected by its input's name; the
container becomes a `DockerRequirement`; an output called `outDir` is staged
writable with an `InitialWorkDirRequirement`; and `network_access=True` adds
`NetworkAccess`. The result is a plain mapping
that `Step.from_cwl_document()` takes, or that you write out as a `.cwl`
file. For a tool you are writing yourself, the [tool builder](tool-builder.md)
is the direct route.

## Payload fixing

`sophios.contrib.converter.update_payload_missing_inputs_outputs(payload)`
takes a workflow-builder payload whose nodes omit inputs or outputs their
plugins declare, and fills them in from the plugin definitions, so the
payload compiles. `raw_wfb_to_lean_wfb(payload)` strips a payload to the
fields the conversion reads, and `wfb_to_wic(payload, plugins)` is the
conversion itself, which the REST route runs. These are the pieces behind the
route, exposed for a client that wants the `.wic` document rather than the
compiled CWL.
