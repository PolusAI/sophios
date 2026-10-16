# The compute client

How do I hand a compiled workflow to the compute service? `sophios.compute_request`
is a client for a separate service, which runs CWL workflows on a cluster. It
is part of the `sophios` package so that a compiled workflow can be submitted
from the same program that built it, and this page covers only Sophios's
side: what the client sends, how to configure the request, and how status
comes back. What the service does with a request, where it runs, and how to
deploy it belong to the service's own documentation.

## From a compiled workflow to a request

```python
from sophios.api.python.workflow import Step, Workflow
from sophios.compute_request import ComputeRequest

touch = Step(clt_path='cwl_adapters/touch.cwl')
touch.inputs.filename = 'greeting.txt'
compiled = Workflow([touch], 'greeting').compile()

request = ComputeRequest(compiled, workflow_id='greeting_2026_10_15')
print(request.to_json(indent=2))
```

`ComputeRequest` takes the result of `Workflow.compile()`, any object with
`name`, `cwl_workflow` and `cwl_job_inputs`, and three keyword arguments:

- `workflow_id`: the id the service knows the submission by; the compiled
  workflow's name when not given;
- `jobs`: a mapping the service reads, passed through as it is; empty by
  default;
- `compute_config`: the execution settings below.

`to_mapping()` and `to_json()` render the request and validate it against the
JSON Schema that ships with Sophios, `src/sophios/compute_request_schema.json`.
The request is:

```json
{
  "id": "greeting_2026_10_15",
  "cwlWorkflow": {"...the compiled CWL, every run: embedded..."},
  "cwlJobInputs": {"touch__filename": "greeting.txt"},
  "jobs": {},
  "computeConfig": {"...when given..."}
}
```

`cwlWorkflow`, `cwlJobInputs` and `jobs` are required; a request the schema
rejects is `ComputeRequestValidationError`. The CWL is the one `compile()`
returned: one document with every process embedded, so the service needs no
other file. Relative `File` paths in the job inputs are sent as written; the
service resolves them where it runs, so give absolute paths or paths the
service can reach.

## Execution settings

```python
from sophios.compute_request import (
    ComputeExecutionConfig, ComputeOutputConfig, SlurmJobConfig, ToilRuntimeConfig,
)

config = ComputeExecutionConfig(
    toil=ToilRuntimeConfig(log_level='INFO'),
    output=ComputeOutputConfig.user_specified('/data/results'),
    slurm=SlurmJobConfig(partition='normal_gpu', cpus_per_task=4, memory='16G'),
)
request = ComputeRequest(compiled, workflow_id='greeting_2026_10_15', compute_config=config)
```

Each part is optional and is written under `computeConfig` only when given:

| Object | Key | Fields |
|---|---|---|
| `ToilRuntimeConfig` | `toilConfig` | `log_level` |
| `ComputeOutputConfig` | `outputConfig` | `service_default()`, `workflow_declared()`, or `user_specified(output_dir)`; `mode` and `outputDir` in the request |
| `SlurmJobConfig` | `slurmConfig` | `job_name`, `partition`, `slurm_job_gpu_count`, `cpus_per_task`, `nodes`, `tasks_per_node`, `output`, `error`, `time_limit` (`time`), `memory` |

The field names are the schema's, in Python spelling; the request carries
the schema's spelling. What each setting makes the service do is the
service's to define.

## Submitting

```python
submission = request.submit('http://compute.example.org:7998/compute/')
print(submission.phase, submission.ok)
```

`submit(url, timeout=(5, 30), poll_interval_seconds=15, fetch_logs=True,
log_path=None)` does the following, in order:

1. `POST`s the validated request JSON to `url`. A response that is not OK
   returns at once with `accepted=False` and the response body in
   `submit_response`.
2. Polls `GET <url>/<workflow_id>/status/` every `poll_interval_seconds`
   until the body's `status` is `RUNNING`, `COMPLETED`, `ERROR` or
   `CANCELLED`; it waits as long as that takes.
3. When the phase is `RUNNING` and `fetch_logs` is set, fetches
   `GET <url>/<workflow_id>/logs/` once, and writes the body to `log_path` if
   given.

It returns a `ComputeSubmission` with `workflow_id`, `phase`, `accepted`
(true for `RUNNING` and `COMPLETED`), and the raw `submit_response`,
`status_response` and `logs` bodies, parsed as JSON when they are JSON.
`submission.ok` is `accepted`, and `submission.exit_code` is `0` when
accepted and `1` otherwise, for a script that wants a process status. A
request with no `workflow_id` and a compiled workflow with no name cannot be
submitted (`ValueError`).

The client does none of what a local run does: no pre-flight, no image pull,
no run. Whether the service's machine can run the workflow is the service's
to check. `--check` on this machine says nothing about that one.

## From the command line

There is no command-line flag for submission. Compile with
`--generate_cwl_workflow --cwl_inline_runtag` to get the same self-contained
CWL the client sends, and submit it with a client of your own, or build the
request in Python as above.
