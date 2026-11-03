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
services, never on a TCP port. Name that host:

```python
from sophios.compute import Service

service = Service('ssh://login.cluster.example.org')
```

`ssh://[user@]host[:port][/socket]` reaches the socket on `host`, by
default `/run/slurm-driver/api.sock`, where the service is set up as its
README shows; a service listening elsewhere, such as one started from a
checkout for testing, is reached with its socket after the host, as
`ssh://host/scratch/me/api.sock`. `unix:///socket` reaches one on the host
Sophios runs on.
Sophios runs your own `ssh`, so your keys, agent, `~/.ssh/config`, jump
hosts and VPN apply as they do in a terminal, and a password or a second
factor is asked for there; Sophios never sees a credential. It forwards a
socket in a private temporary directory to the service's, no TCP port even
on your machine, on a connection of its own (a ControlMaster's is never
shared, so each `Service` logs in once), and closes the forward when the
`Service` is closed (`with Service(url) as service:`) or Python exits; a
Ctrl-C leaves it open. An ssh that fails, say an unknown host or a refused
key, fails `Service(url)` at once, with ssh's own error, as does one that
cannot be run, say none on PATH, with the system's; and so does the
next call once ssh has ended, say on a dropped connection: a new
`Service(url)` forwards again. Sophios stores neither the URL nor anything
else of a service.

`http://` and `https://` are refused: they need an HTTPS gateway in front of
the service, which there is not yet.

The compute client needs macOS, Linux or WSL. On Windows itself, Python
cannot open a Unix socket, and `Service(url)` raises `cmp002` for every URL,
saying so: run Sophios inside WSL.

Who may do what is the service's, and follows from who may log into its
host:

- Whoever may log into the host and open the socket may submit, and every
  run records the account that submitted it.
- Every allowed user may read every run: its status, outputs and logs.
- Only the account that submitted a run, or the service's own, may cancel
  it: `cancel()` on anyone else's run raises `cmp001`.
- Every run runs as the service's account, so every allowed user reaches
  what that account can: it should reach only the shared data and output
  folders.

## Running a workflow

```python
from sophios.api.python.workflow import RunState
from sophios.compute import RunConfig

config = (RunConfig('/shared/lab/results')    # the outputs folder: every output lands in it
          .basepath('/shared/lab')
          .leader(time='2:00:00', memory='4G')
          .steps(time='30:00'))
run = service.submit(workflow, config)
if run.wait().state is RunState.COMPLETED:    # wait() prints each change, and why
    print(run.outputs())
```

A `RunConfig` may be made with the outputs folder: the folder on the
cluster every output lands in, absolute or relative to `basepath`, new or
empty. Without one, the outputs land in the run's own folder on the
cluster, which the service deletes some days after the run ends: so they do
with `RunConfig()`, and with no `RunConfig` at all,
`service.submit(workflow)`, which takes every setting from the service
([Where the outputs go](#where-the-outputs-go)).

Every path goes as written; the service, not Sophios, reads it on the
cluster, before anything is queued, refuses what it cannot read, and
answers every problem at once. A path a tool reads is a path on the
cluster, absolute or relative to `basepath`, or an http(s) URL; it must
exist and be readable, with a name cwltool takes. An output folder is
written, not read: see below. No path on the cluster may start with `~`
or hold a `$VARIABLE`: a shell expands those on the machine that wrote
them, no shell reads the request, and the service reads only its shared
roots, which its refusal names.

`submit` returns at once, with the run's id and every warning of the
service printed, and the run is followed as a local one is
([Following a run](#following-a-run)): `wait()` returns its status however
it ended. The run goes on in Slurm whatever happens to the Python process,
and `Service.attach(id)` follows it from any session or machine.

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
| `RunConfig(folder)` | The outputs folder, the request's `workflowOutDir`: the folder on the cluster every output lands in, absolute or relative to `basepath`; missing (the service creates it) or empty, and used by no run that has not ended |
| `RunConfig()` | No outputs folder: the outputs land in the run's own folder |
| `.basepath(path)` | The directory on the cluster that relative paths are relative to: those the tools read, and the outputs folder's; required when there is one. An output folder given a name is made in the run, never there |
| `.leader(partition=, time=, memory=)` | The Toil leader's Slurm job, which lasts as long as the run |
| `.steps(partition=, gpu_partition=, time=)` | Where step jobs go, those that ask for a GPU to `gpu_partition`, and how long each may run |
| `.log_level(level)` | Toil's log level: `DEBUG`, `INFO` (the default), `WARNING` or `ERROR` |

`submit` and `build_request` take the workflow and a `RunConfig`, or none;
besides, `tool_registry=` and `inference=` say how the workflow compiles, as
`Workflow.compile` takes them.

## Where the outputs go

Every output lands in one folder, the **outputs folder**, under its own
name: the folder `RunConfig(folder)` names or, without one, the run's own
folder on the cluster, `<run>/out` below. The one exception is an output
folder given an absolute path, which its tool writes there, in place, and
which is never copied.

| The workflow gives | Without a folder | `RunConfig('/lab/run7')` |
|---|---|---|
| A file output, `glob='hello.txt'`, or stdout `hello.txt` | `<run>/out/hello.txt` | `/lab/run7/hello.txt` |
| A file output, `glob='results/p1/out.tsv'` | `<run>/out/out.tsv` | `/lab/run7/out.tsv` |
| A directory output, `glob='tiles'` | `<run>/out/tiles/` | `/lab/run7/tiles/` |
| An output folder given a name, `'masks'` | `<run>/out/masks/` | `/lab/run7/masks/` |
| An output folder given an absolute path, `'/lab/masks/p1'` | `/lab/masks/p1/`, in place | `/lab/masks/p1/`, in place |

Toil copies the outputs into the outputs folder once, after the whole run
has succeeded. The copy keeps each output's own name alone, so
`results/p1/out.tsv` lands as `out.tsv`, and two outputs of one run with
one name land as `out.tsv` and `out.tsv_2`. A completed run's `outputs()`
names where each output is.

An **output folder** is a `Directory` input that a tool writes into and
returns, such as the folder a segmentation tool fills; the service finds
them itself, whatever they are called and however deep in the workflow.

- **Given just a name**, such as `step.inputs.output = 'masks'`, it is made
  empty in the run, never anywhere `basepath` points, and copied into the
  outputs folder under that name. The name is one folder name: `seg/p1`
  would land as `p1`, and is refused.
- **Given an absolute path**, it is written there by its tool, once, in
  place, and never copied: what very large outputs need, and the path means
  the same in every run. It needs `cwlVersion` v1.1 or later; Sophios
  compiles to v1.2. A tool that cannot write it in place without changing
  what else the run reads is refused, naming the step and the folder, why,
  and what to change: the instances of a scattered step sharing one folder,
  another step that also takes the folder, a tool that also stages another
  input writable, or a step or workflow output that reads the folder
  through `linkMerge`, `pickValue`, `valueFrom`, several sources or a step
  with `when`. The way out is always the same: give the folder a name
  instead of a path, and it is copied into the outputs folder.
- An array of output folders is all names or all absolute paths: a tool
  writes them all in place, or none.

A folder that is not empty is never written into: to update one, read it
as an input and write a new one.

### A folder you name

Each folder you name, the outputs folder of `RunConfig(folder)` and every
output folder given an absolute path, is checked when the run is
submitted, before anything is queued:

| At submission, the folder is | What happens |
|---|---|
| missing, in a folder that exists | created, one level, mode `2775` |
| missing, and so is the folder it would be in | refused |
| empty, and used by no run that has not ended | taken as it is |
| used by a run that has not ended: that folder, one inside it, or one around it | refused, naming that run, which is still going |
| not empty | refused: a run writing there would replace same-named files and merge into same-named folders |

So a folder made beforehand works, and so does the same `RunConfig` again
after a run that failed there, which left it empty; after a run that
succeeded, the folder holds its outputs: name a new one. Two runs sent at
once with one `RunConfig` are never both taken, though the folder stays
empty until the first has succeeded: the second is refused, naming the
first. No folder you name may be inside or around an input or another
folder of the run, so an output folder given an absolute path inside the
outputs folder, which Toil's copy would merge into, is refused too.

### The run's own folder

Without a folder, the outputs land in the run's own folder on the cluster,
`<runs>/<account>/<run id>/out`, where `<runs>` is where the service keeps
its runs and `<account>` the account you reach the service as: a new
folder for every run, with no name to invent, so that each rerun, each
repetition of a stochastic workflow and each attempt to reproduce one keeps
its own. `outputs()` names it:

```python
{'autoseg~output': {'class': 'Directory', 'basename': 'segmentation',
                    'location': 'file:///projects/compute/runs/alice/segment-20261102T142205-7f3a/out/segmentation',
                    'path': '/projects/compute/runs/alice/segment-20261102T142205-7f3a/out/segmentation',
                    'listing': [...]}}
```

That folder is the service's: every file in it belongs to the service's
account, and the service deletes the run, its logs with it, some days after
it ended, 14 by default (the service's `RETENTION_DAYS`): copy what you
keep. A deleted run is gone for `Service.attach` and every method of a
`Run` of it, which raise `cmp001` saying so. The service never deletes a
run that has not ended, nor any folder you named.

### When a run fails

A run that fails or is cancelled leaves its outputs folder empty: Toil
copies the outputs only once the whole run has succeeded. A folder written
in place keeps what its tool wrote, whatever becomes of the run; as Toil
runs a failed step again once, it may also hold what a failed attempt
wrote.

The service refuses any new run while the disk that holds its runs is
full, 90% by default (its `MAX_DISK_PERCENT`), saying so; it makes room as
it deletes the runs that ended long enough ago.

## Following a run

A run on the cluster is followed as a local one is: `service.submit(workflow,
config)` is `workflow.start()`, and the `Run` it returns has the local
`RunHandle`'s `status`, `wait`, `outputs`, `logs` and `cancel`
([Follow a run from Python](running.md#follow-a-run-from-python)).

```python
run = workflow.start()                    # a local run, or
run = service.submit(workflow, config)    # a run on the cluster

status = run.wait()                       # how it ended; nothing is raised
if status.state is RunState.COMPLETED:
    print(run.outputs())
else:                                     # FAILED or CANCELLED, and why
    print(status.state, status.reason)
print(run.logs())
```

| | Local, `workflow.start()` | On the cluster, `service.submit(workflow, config)` |
|---|---|---|
| `status()` | Where the run is, without blocking: a `RunStatus` | The same, asked anew of the service |
| `wait(timeout=None)` | Blocks until the run ends, and returns its `RunStatus`: a run that failed or was cancelled is returned, not raised. After `timeout` seconds it raises `TimeoutError`, and the run goes on | The same, asking every 10 s and printing a line per change of the run's state and of each step's, the run's end last; Ctrl-C leaves the run going |
| `outputs()` | The workflow's output object once the run has completed; `None` until then | The same |
| `logs()` | Everything the runner has printed so far | `leader.log`, Toil's log of the whole run, so far; `''` until Slurm has started the leader |
| `cancel()` | Stops the run: `True` once it is `CANCELLED`, by this call or before; `False` when it had ended `COMPLETED` or `FAILED`, which it stays | The same, for a run you submitted and every job of it in Slurm's queue: sent once, then waited for as `wait(300)` waits: a run that has not ended within five minutes raises `TimeoutError`, its cancel still recorded |
| `collect(timeout=None)`, `workflow.run()` | `wait()`, then the run's `RunResult`; a run that did not complete raises `WorkflowRunError` | Local only |
| `id`, `Service.attach(id)` | None | On the cluster only: the run's id, the workflow's name, the UTC time and four hex digits, which `attach` follows from any session or machine |
| `warnings` | None | On the cluster only: what the service warned of when it accepted the run, also printed then |
| `slurm_job` | None | On the cluster only: the Slurm job id of the run's Toil leader, for `squeue` and `sacct` |

The states are those of a local run: `QUEUED`, `RUNNING`, then `COMPLETED`,
`FAILED` or `CANCELLED`, which never change. A run that did not complete
has its `reason`, such as the step that failed and its exit code.

## The request

`build_request(workflow, config)` returns the request `submit` sends, with
the same arguments: the workflow compiled into one document,
every process inline, its inputs, and what `config` sets. Its shape is
`run-request.schema.json`, Sophios's copy of the service's own schema, which
the service also serves at `GET /run-request.schema.json`. Sophios checks
every request against it before sending, and the service again on arrival.

## When something goes wrong

A workflow that does not compile raises a `SophiosError`, with Sophios's own
[error codes](error_codes.md), as a local run does, and nothing is sent.
A refused request and a failing service each raise a `ComputeError`, an
exception of `sophios.compute`'s own: its `code` says which of two it is,
and `messages` holds every problem, a line each. The
codes belong to the compute client alone: a `ComputeError` is never a
`SophiosError`, and `sophios --explain` does not know them.

- `cmp001`, `RequestRefused`: Sophios or the service refused the request: a
  path, a name, an output folder, a partition or a time the cluster cannot
  use, a folder that is not empty or that a run going on uses, a disk too
  full for a new run, or a real-time analysis; or a cancel of a run another
  account submitted; or there is no such run, as when the service has
  deleted it. Each problem is a line, at the input or field it is about; do
  what it says. A relative path sent without `basepath` says to give one.
- `cmp002`, `ServiceUnavailable`: Sophios runs on Windows itself, the URL is
  not one the service is reached at, ssh could not be run, failed before it
  forwarded the socket or has ended since, the `Service` is closed, or the
  service could not be reached, stayed busy for five minutes (Sophios asks
  again every 5 s meanwhile, which never starts a second run), failed, or
  speaks another version of the contract; or something other than the service
  answered at the URL. Check that the URL names the socket the service
  listens on, and that the service runs, then make the same call again; when
  the versions differ, upgrade Sophios or the service. A submission cut short
  this way says the run may exist, and how to attach.

A run that fails raises nothing, as a local one started with `start()`
does: `wait()` returns its status, `FAILED`, whose `reason` says why, and
`run.logs()` holds Toil's log of the whole run, which is empty when Slurm
never started the leader: the `reason` then says why.
