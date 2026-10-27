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

config = (RunConfig('/shared/lab/results')    # the folder the outputs are copied into
          .basepath('/shared/lab')
          .leader(time='2:00:00', memory='4G')
          .steps(time='30:00'))
run = service.run(workflow, config)
print(run.outputs())
```

A `RunConfig` is made with the folder on the cluster the outputs are copied
into, absolute or relative to `basepath`: the supported way. For a quick
try, leave it out: `service.run(workflow)` copies the outputs into the run's
own folder on the cluster, with every setting the service's.

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

A `RunConfig` is everything of the request but the workflow and its
inputs. How it is made says where the outputs go, and nothing changes that
after. Each setter sets just what it is passed, leaves the rest as it was,
and returns the config, so the calls chain. What no setter sets is left out
of the request, so the service's own settings apply: only the service knows
the cluster. The values are strings in Slurm's own forms, such as `'2:00:00'`
and `'4G'`; Sophios checks them against the service's schema when it builds
the request, each problem at the request's field, such as `leader.time`.

| `RunConfig` | What it sets |
|---|---|
| `RunConfig(folder)` | Output mode `workflowOutDir`: the folder on the cluster the outputs are copied into, absolute or relative to `basepath`, created when missing |
| `RunConfig.in_place()` | Output mode `workflowInPlace`, an advanced choice: see below |
| `.basepath(path)` | The directory on the cluster that the relative paths of what the tools read are relative to; required when there is one |
| `.leader(partition=, time=, memory=)` | The Toil leader's Slurm job, which lasts as long as the run |
| `.steps(partition=, gpu_partition=, time=)` | Where step jobs go, those that ask for a GPU to `gpu_partition`, and how long each may run |
| `.log_level(level)` | Toil's log level: `DEBUG`, `INFO` (the default), `WARNING` or `ERROR` |

`run`, `submit` and `build_request` take the workflow and a `RunConfig`, or
none; besides, `tool_registry=` and `inference=` say how the workflow compiles,
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

- **`RunConfig('<folder>')`, output mode `workflowOutDir`: the supported
  way.** Toil copies the workflow's outputs into that folder on the
  cluster, an output folder given as `segmentation` as
  `<folder>/segmentation`. The folder may exist, empty, or the service
  creates it, in a parent it can write to.
- **No `RunConfig`, output mode `serviceOutDir`: the quick try.** The
  outputs are copied, as above, into the run's own folder on the cluster;
  the output object says where. Without a `RunConfig` there is no
  `basepath` either, so every path a tool reads is absolute.
- **`RunConfig.in_place()`, output mode `workflowInPlace`: an advanced
  choice, which the service warns of on every run.** Each output folder
  given as an absolute path is written there, once, without a copy, which
  is what very large outputs need; the
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
every process inline, its inputs, and what `config` sets. Its shape is
`run-request.schema.json`, Sophios's copy of the service's own schema, which
the service also serves at `GET /run-request.schema.json`. Sophios checks
every request against it before sending, and the service again on arrival.

## When something goes wrong

A workflow that does not compile raises a `SophiosError`, with Sophios's own
[error codes](error_codes.md), as a local run does, and nothing is sent.
A refused request, a failing service and a failed run each raise a
`ComputeError`, an exception of `sophios.compute`'s own: its `code` says
which of three it is, and `messages` holds every problem, a line each. The
codes belong to the compute client alone: a `ComputeError` is never a
`SophiosError`, and `sophios --explain` does not know them.

- `cmp001`, `RequestRefused`: Sophios or the service refused the request: a
  path, a name, an output folder, a partition or a time the cluster cannot
  use, or a real-time analysis. Each problem is a line, at the input or
  field it is about; do what it says. A relative path sent without
  `basepath` says to give one.
- `cmp002`, `ServiceUnavailable`: the service could not be reached, stayed
  busy for five minutes (Sophios asks again meanwhile, which never starts a
  second run), failed, or speaks another version of the contract. Check the
  SSH tunnel and that the service runs, then make the same call again; when
  the versions differ, upgrade Sophios or the service. A submission cut
  short this way says the run may exist, and how to attach.
- `cmp003`, `RunFailed`: the run ended `FAILED` or `CANCELLED`; the message
  has the reason and the end of the failed step's log, and `run.logs(step)`
  has all of it. Its `run` is the run, and `status` how it ended.
