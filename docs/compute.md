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
from sophios.compute import RunConfig

config = (RunConfig('/shared/lab/results')    # the folder the outputs are copied into
          .basepath('/shared/lab')
          .leader(time='2:00:00', memory='4G')
          .steps(time='30:00'))
run = service.submit(workflow, config)
status = run.wait()
print(status.state, run.outputs())
```

A `RunConfig` is made with the folder on the cluster the outputs are copied
into, absolute or relative to `basepath`: the supported way. For a quick
try, leave it out: `service.submit(workflow)` copies the outputs into the
run's own folder on the cluster, with every setting the service's.

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
| `RunConfig(folder)` | Output mode `workflowOutDir`: the folder on the cluster the outputs are copied into, absolute or relative to `basepath`, created when missing |
| `RunConfig.in_place()` | Output mode `workflowInPlace`, an advanced choice: see below |
| `.basepath(path)` | The directory on the cluster that the relative paths of what the tools read are relative to; required when there is one |
| `.leader(partition=, time=, memory=)` | The Toil leader's Slurm job, which lasts as long as the run |
| `.steps(partition=, gpu_partition=, time=)` | Where step jobs go, those that ask for a GPU to `gpu_partition`, and how long each may run |
| `.log_level(level)` | Toil's log level: `DEBUG`, `INFO` (the default), `WARNING` or `ERROR` |

`submit` and `build_request` take the workflow and a `RunConfig`, or none;
besides, `tool_registry=` and `inference=` say how the workflow compiles, as
`Workflow.compile` takes them.

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
  says why it is slower. An array of output folders is all absolute paths
  or all names, since a tool writes them all in place or none: the service
  refuses a mix.

## Following a run

A run on the cluster is followed as a local one is: `service.submit(workflow,
config)` is `workflow.start()`, and the `Run` it returns has the local
`RunHandle`'s `status`, `wait`, `outputs`, `logs` and `cancel`
([Follow a run from Python](running.md#follow-a-run-from-python)).

```python
run = workflow.start()                    # a local run, or
run = service.submit(workflow, config)    # a run on the cluster

status = run.wait()                       # how it ended; nothing is raised
print(status.state, status.reason, run.outputs())
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
  use, or a real-time analysis; or a cancel of a run another account
  submitted. Each problem is a line, at the input or field it is about; do
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
