# The compute service

How do I run a workflow on the cluster? `sophios.compute` compiles a workflow
built with the Python API and sends it to the cluster's compute service, the
slurm driver of PolusAI/compute, which runs it with Toil on Slurm: the Toil
leader as one Slurm job, and each step as a Slurm job of its own.

`sophios.compute` is a core feature of Sophios, but it is **not part of the
Python API**: nothing under `sophios.api.python` exports or documents it, and
its interface follows the service's contract rather than the API's. This page
covers Sophios's side; how the service checks a request, runs it and is
deployed is in the service's own README.

## Reaching the service

The service listens on a Unix socket on a host of the cluster set aside for
services, reached through SSH. One block in `~/.ssh/config` names it:

```
Host sophios-compute
    HostName <the host the service runs on>
    # ProxyJump <login node>      # when that host is reached through a login node
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 8h
```

`Service.over_ssh` opens the tunnel itself, for as long as a `with` block:

```python
from sophios.compute import Service

with Service.over_ssh('sophios-compute') as service:
    ...
```

It runs `ssh` in batch mode, so it never stops to ask for a password or a
code. When `ssh` would need one, the call fails at once and says so: run
`ssh sophios-compute true` in a terminal once, and the `ControlMaster`
connection serves Sophios for the next eight hours. The socket defaults to
`/run/slurm-driver/api.sock`; `over_ssh(host, socket=...)` names another. A
tunnel you open yourself works too:

```bash
ssh -N -L 127.0.0.1:7998:/run/slurm-driver/api.sock sophios-compute
```

```python
service = Service('http://127.0.0.1:7998')
```

## Running a workflow

```python
from sophios.compute import RunConfig

config = RunConfig(basepath='/projects/lab/data', output_mode='workflowOutDir',
                   workflow_out_dir='/projects/lab/results/run7')
run = service.run(workflow, config)
print(run.outputs())
```

Every path goes as written; the service, not Sophios, reads it on the
cluster, before anything is queued, and answers every problem at once. A
path a tool reads is a path on the cluster, absolute or relative to
`basepath`, or an http(s) URL; it must exist and be readable, with a name
cwltool takes. An output folder is written, not read: see below. Sophios
refuses before sending only what names nothing on the cluster: a path with
`~` or an environment variable in it.

`run` returns once the run has completed, and raises otherwise. `submit`
returns at once instead, with the run's id printed; either way the run goes
on in Slurm whatever happens to the Python process, and `Service.attach(id)`
follows it from any session or machine.

A `RunConfig` holds everything of the request but the workflow and its
inputs: one field per field of the request, named as the request names it
in snake_case, and sent as it is given. A field left `None` is left out of
the request, for the service's default.

| `RunConfig` field | What it is |
|---|---|
| `basepath` | The directory on the cluster that the relative paths of what the tools read are relative to; required when there is one |
| `output_mode` | Where the outputs go: `serviceOutDir` (the default), `workflowOutDir` or `workflowInPlace`: see below |
| `workflow_out_dir` | With `output_mode='workflowOutDir'`, and only with it: the folder the outputs are copied into, absolute or relative to `basepath` |
| `leader` | The Toil leader's Slurm job: `{'partition': ..., 'time': ..., 'memory': ...}`; the service's settings otherwise |
| `steps` | Where step jobs go and how long each may run: `{'partition': ..., 'gpuPartition': ..., 'time': ...}` |
| `log_level` | Toil's log level: `DEBUG`, `INFO` (the default), `WARNING` or `ERROR` |

`run`, `submit` and `build_request` take the workflow and a `RunConfig`;
besides, `tool_registry=` and `inference=` say how the workflow compiles,
as `Workflow.compile` takes them, and `run`'s `timeout=` is how many seconds
it waits at most: the run goes on after a `TimeoutError`.

## Where the outputs go

An output folder is a `Directory` input that a tool writes into and returns,
such as the folder a segmentation tool fills; the service finds them itself,
whatever they are called and however deep in the workflow. Give one **just a
name**, such as `step.inputs.output = 'segmentation'`: the service makes it,
empty, in the run, and its result lands in the outputs' folder under that
name, never anywhere `basepath` points. Give one an absolute path only to
have it written in place (below), or to hand the tool an existing folder as
its input: the tool then works on a copy of it, and the folder is left as it
is, which the service warns of.

- **`output_mode='workflowOutDir'`, `workflow_out_dir='<folder>'`: the
  supported way.** Toil copies the workflow's outputs into that folder on
  the cluster, an output folder given as `segmentation` as
  `<folder>/segmentation`. The folder may exist, empty, or the service
  creates it, in a parent it can write to. `workflow_out_dir` goes with this
  mode and only with it; Sophios refuses either without the other.
- **`output_mode='serviceOutDir'`, or none:** the outputs are copied, as
  above, into the run's own folder on the cluster; the output object says
  where.
- **`output_mode='workflowInPlace'`: an advanced choice, which the service
  warns of on every run.** Each output folder given as an absolute path is written
  there, once, without a copy, which is what very large outputs need; the
  service creates it when it is missing (one folder, in a parent it can
  write), and an existing one must be empty. An output folder given as a
  name is copied into the run's own folder, with a warning. So is the output
  of a tool that also stages another input writable, since writing in place
  would change that input too, and of a step scattered over something other
  than its output folders: each falls back to copying, with a warning that
  says why it is slower. `run.targets` names the folders written in place.

## Following a run

| `Run` | What it does |
|---|---|
| `id` | The run's id: the workflow's name, the UTC time and four hex digits |
| `status()` | Where the run is, asked anew: a `RunStatus`, as a local `Workflow.start()` handle reports it |
| `wait(timeout=None)` | Asks every 10 s until the run ends, printing a line per change; Ctrl-C leaves the run going |
| `logs(step=None)` | Toil's `leader.log`, or the logs of a step by its name, such as `compact/merge` |
| `outputs()` | The workflow's output object once the run has completed |
| `cancel()` | Cancels the run and every job of it in Slurm's queue; safe to send again |
| `targets`, `warnings`, `slurm_job` | The folders written in place, what the service warned of, the leader's Slurm job |

The states are those of a local run: `QUEUED`, `RUNNING`, then `COMPLETED`,
`FAILED` or `CANCELLED`, which never change.

## The request

`build_request(workflow, config)` returns the request `run` and `submit`
send, with the same arguments: the workflow compiled into one document,
every process inline, its inputs, and the fields of `config`. Its shape is
`run-request.schema.json`, Sophios's copy of the service's own schema, which
the service also serves at `GET /run-request.schema.json`. Sophios checks
every request against it before sending, and the service again on arrival.

## When something goes wrong

Each failure is a `SophiosError` with a code of the compute range
([Error codes](error_codes.md)):

- `cmp001`, `RequestRefused`: Sophios or the service refused the request.
  Each problem is a line, at the input it is about; a relative path sent
  without `basepath` says to pass one.
- `cmp002`, `ServiceUnavailable`: the service could not be reached, stayed
  busy for five minutes (Sophios asks again meanwhile, which never starts a
  second run), failed, or speaks another version of the contract. A
  submission cut short this way says the run may exist, and how to attach.
- `cmp003`, `RunFailed`: the run ended `FAILED` or `CANCELLED`; the message
  has the reason and the end of the failed step's log.
