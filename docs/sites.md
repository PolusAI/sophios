# Sites

> **Your data must already be on the site.** Sophios sends the workflow there, never your data, and the results stay
> there beside your data. Write input paths relative to your project folder (`data/plate1`, not `/Users/me/...`),
> and run Python from that folder. The same line then reads `./data/plate1` here and `/project/me/cp/data/plate1`
> there. **Your runners are yours:** `bin` names the folder that holds them on the site. **Your container images
> must be in a registry the site can pull from.**

How do I run a workflow on another computer, as myself? A **site** is a
computer you can `ssh` to. `sophios.sites` compiles the workflow here, as
`workflow.start()` does, copies the run's files (never your data) into a
run folder there, and starts the runner from the `bin` folder you name, as
a process of its own, which outlives ssh and Python. Nothing of Sophios is
installed there, and no service runs: the run folder is the record, and
Sophios on any computer follows the run from it.

`sophios.sites` is a core feature of Sophios, but it is **not part of the
Python API**: nothing under `sophios.api.python` exports or documents it.
It is experimental. Its one kind of site is `Machine`: a computer of your
own, macOS or Linux, such as a workstation or a cloud instance. A
cluster's login node is not a `Machine`: a run there would run on the
login node.

## A machine

```python
from sophios.sites import Machine

ec2 = Machine('my-ec2', root='/data/cp', bin='/opt/conda/envs/run/bin')   # a Host in ~/.ssh/config

segment.inputs.images = {'class': 'Directory', 'location': 'data/plate1'}  # relative: here and there
diagnostics = ec2.check(workflow)               # every problem submit would hit, at once; writes nothing
run = ec2.submit(workflow)                      # Toil, a site's default for CWL
run = ec2.submit(workflow, target='nextflow')   # Nextflow, Docker, the local executor
status = run.wait()                             # Ctrl-C stops following; the run goes on
```

| Setting | What it is |
|---|---|
| `Machine(host)` | The computer, as `ssh` names it: a `Host` of `~/.ssh/config`, `host`, or `user@host` |
| `root=` | The folder there that relative paths are read against, as the working directory is here; Sophios makes `sophios/runs/<id>` in it for each run |
| `bin=` | Required: the folder there that holds the runners and what they need, `toil-cwl-runner`, `nextflow` and the Java Nextflow runs on. It is first on `PATH` in every job, and Sophios never searches for a runner |
| `container_engine=` | The engine a CWL runner runs containers with, `docker` by default; `podman` and `singularity` too |
| `modules=` | Environment modules each job loads first, by name: `modules=('nextflow/26.04',)` |

No setting takes shell code. Each is checked when the `Machine` is made,
and a `ValueError` names the one that does not fit: `root` and `bin` are
absolute and hold only letters, digits and `/._+-`. Every value Sophios
puts into a script there is quoted.

## Reaching it

Sophios runs your own `ssh`, so your keys, agent, `~/.ssh/config`, jump
hosts and VPN apply as they do in a terminal; Sophios never sees a
credential. Every call is one `ssh` that asks for nothing (`BatchMode`),
so a computer that wants a password or a second factor is reached through
a shared connection you open by hand first:

```text
Host my-ec2
    HostName ec2-12-34-56-78.compute-1.amazonaws.com
    User me
    ControlMaster auto
    ControlPath ~/.ssh/control-%C
    ControlPersist 8h
```

Each call runs a script there with `bash` as a login shell, so your login
profile, and the `module` command it sets up, apply. A profile may print
what it likes. One that reads its input would eat part of the script, so
Sophios sends the script's sha256 with it and runs it only when it arrives
whole; otherwise it refuses with `wic057`. Sophios runs on macOS, Linux or
WSL: on Windows itself, making a `Machine` raises `wic057`.

## Runners

Every runner Sophios runs locally runs on a machine, from `bin`, or is
refused there with a code:

| Runner | On a machine |
|---|---|
| `target='cwl'`, Toil, the default | Toil's `single_machine` batch system: at most `max_parallel` steps at once, each step's outputs and scratch in the run folder |
| `target='cwl'`, `runner='cwltool'` | Refused, `wic058`: Toil runs the same CWL on a machine and on a cluster alike |
| `target='nextflow'` | Nextflow's local executor: at most `max_parallel` tasks at once (`executor.queueSize`, which the site adds to the run's `nextflow.config`). Its outputs stay in its work folder, where the Nextflow target leaves them |

A site needs Toil 9.5.0 and Nextflow 26.04.2 or later; `check` reads
each one's version. A runner pulls the images its steps need as it runs,
so they must be in a registry the site can pull from. The Nextflow target runs containers with Docker only: on a machine
whose `container_engine` is not `docker`, each step with a container is
refused with `wic060`, by name. A Toil step whose tool declares no memory
gets 4 GiB there (`--defaultMemory 4Gi`), and Toil refuses to start it on
a machine with less: `check` notes each such step, `wic061`. The tool's own
`ramMin` wins.

`check` and `submit` take the workflow and these options:

| Option | What it sets |
|---|---|
| `target=` | `'cwl'`, the default, or `'nextflow'`, as `Workflow.run` takes it |
| `runner=` | The target's runner; by default `toil-cwl-runner` for `cwl`, as on every site |
| `outdir=` | A CWL runner's `--outdir`: a folder there that does not exist yet, relative to `root` or absolute; by default `<run>/out`. Refused with `target='nextflow'` |
| `max_parallel=` | How many steps run at once, 64 by default: Toil's `--maxJobs`, Nextflow's `executor.queueSize` |
| `clean=` | Toil's `--clean`: `'always'`, the default, `'onSuccess'` or `'never'` ([Cleanup](#cleanup)). The other runners take `'always'` |
| `tool_registry=`, `inference=` | How the workflow compiles, as `Workflow.compile` takes them |

An option the runner does not take is a `ValueError`, raised before
anything is compiled.

## Checking and submitting

`check(workflow)` compiles the workflow here and lowers it to its target,
reading each relative path against `root`, so a workflow the target cannot
run is refused with its `nf-s` code before anything reaches the site. One
call there then checks every File and Directory the run reads, the runner
in `bin` and its version, the container engine when a step runs in a
container (`docker info`), that `<root>/sophios` can be written, and that
an `outdir` you name does not exist yet. It returns every problem at once,
with every note the compile made, and under Toil a note, `wic061`, at each
step whose tool declares no memory; it writes nothing, here or there. A
path with `..`, `~` or `$` is refused: no shell there expands it, and `..`
leaves your project folder. So is a workflow with real-time analyses,
which watch cwltool's job folders here.

`submit(workflow)` makes every check of `check`, raises its errors as a
`SophiosError` with nothing written, and otherwise starts the run in one
call there and returns its `Run` at once, having printed its id, its
settings with where each comes from, and where its outputs go:

```text
Submitting segment-20261109T142205-7f3a91c2 to my-ec2: toil-cwl-runner (a site's default for cwl), at most 64 at once (Sophios's default).
Outputs: /data/cp/sophios/runs/segment-20261109T142205-7f3a91c2/out (Sophios's default)
From anywhere, Machine(host='my-ec2', root='/data/cp', bin='/opt/conda/envs/run/bin', container_engine='docker', modules=()).attach('segment-20261109T142205-7f3a91c2') follows it.
```

A run's id is the workflow's name, the UTC time and eight hex digits. The
call ignores hang-ups, so a dropped connection cannot stop it between
starting the run and recording it; should it fail all the same, the error
says how to attach to the run, which may have started.

## Following a run

A run on a site is followed as a local one is: `machine.submit(workflow)`
is `workflow.start()`, and the `Run` it returns has the local `RunHandle`'s
`status`, `wait`, `outputs`, `logs` and `cancel`
([Follow a run from Python](running.md#follow-a-run-from-python)).

| | Local, `workflow.start()` | On a site, `machine.submit(workflow)` |
|---|---|---|
| `status()` | Where the run is, without blocking: a `RunStatus` | The same, read from the run folder there; once it has ended, as it ended, its outputs and failed steps read by the runner's own outcome, as locally |
| `wait(timeout=None)` | Blocks until the run ends, and returns its `RunStatus`; after `timeout` seconds it raises `TimeoutError`, and the run goes on | The same, reading every 5 s for a minute, then every minute, and printing what `run.log` gained and each change; Ctrl-C prints how to follow the run again, and the run goes on |
| `outputs()` | The workflow's output object once the run has completed; `None` until then | The same, with paths there |
| `logs()` | Everything the runner has printed so far | `run.log` so far: the job's and the runner's two streams |
| `cancel()` | Stops the run: `True` once it is `CANCELLED`, by this call or before; `False` when it had ended `COMPLETED` or `FAILED` | The same: a run whose job has not started never will, and a running one's process group gets TERM; it waits up to two minutes for the end |
| `collect(timeout=None)`, `workflow.run()` | `wait()`, then the run's `RunResult` | Local only |
| `id`, `machine.attach(id)` | None | On a site only: the run's id, which `attach` follows from any computer; `repr(run)` is that line |

The states are those of a local run. A run is `QUEUED` until its job has
claimed it, a moment after the submit; one that no job has claimed ten
minutes after the submit ended `FAILED`, as its `run.log` says why. When
ssh or the site does not answer, a method raises `wic057` and decides
nothing.

## The run folder

```text
<root>/sophios/runs/<id>/
  run.json     the settings, each with its source, the target, runner and clean, Sophios's version
  job.sh       the job script; beside it, the workflow and its job, or the Nextflow pipeline
  leader       the claim: a link to the job's pid, or to `cancelled` or `none`
  run.log      the job script's and the runner's two streams
  stdout       the runner's standard output alone, which its outcome reads
  toil/        Toil's work/ and tmp/: every step's outputs and scratch, removed as clean= says
  out/         the outputs of a CWL runner, unless outdir= names another folder
  exit         the job script's exit status, written last, after the cleanup
```

Sophios never deletes a run folder. You run as yourself, so your umask
applies to everything the run writes.

## Cleanup

`clean='always'`, the default, cleans up however a run ends: success,
failure or cancel. Only Toil leaves scratch outside its outputs: its
`--clean always` removes its job store, and once Toil has stopped the job
script removes `toil/tmp` and `toil/work`, making writable first what a
tool left unwritable; neither follows a link, so linked inputs stay
intact. `'onSuccess'` and `'never'` are Toil's own names: the first keeps
the job store and `toil/` after any end but a success, the second always.
A Nextflow run's work folder holds its outputs, so for Nextflow only
`'always'` is accepted.

A hard kill (SIGKILL, the job out of memory, the computer stopped) stops
the job before its cleanup: the run reads `FAILED`, and for Toil its
`reason` names `<run>/toil` and the `rm -rf` line that removes it.

## When something goes wrong

A workflow that does not compile raises a `SophiosError`, with Sophios's
own [error codes](error_codes.md), as a local run does. A problem of the
site is a code of kind `machine`, which `sophios --explain` explains:

- `wic057`: the site cannot be reached or used: ssh failed, the login
  profile there read the script, a call there failed, or the site is
  neither macOS nor Linux. Run `ssh <host>` in a terminal and fix what it
  says; the message quotes what ssh or the site said.
- `wic058`: `runner='cwltool'`, which no site runs, or `bin` holds no
  runner you can run, or an older one than a site runs. Leave `runner=`
  out for Toil; install the runner there, or name the folder that holds it.
- `wic059`: `attach` was given an id that names no run under `root`.
- `wic060`: a Nextflow step with a container, on a machine whose engine is
  not Docker.
- `wic061`, a note: a Toil step whose tool declares no memory, which gets
  4 GiB there.
- `wic015`, `wic016`, `wic021`: the container engine, an input, or a
  folder Sophios writes, as for a local run, each message naming the site.
- `api008`: a workflow with real-time analyses, which no site runs.

A run that fails raises nothing, as a local one started with `start()`
does: `wait()` returns its status, `FAILED`, whose `reason` and `steps`
say why, and `run.logs()` holds the rest.

## What a site does not do

- It copies no data and fetches no outputs: `rsync` does both.
- It resumes nothing: a run that failed is submitted again from the start.
- It runs no real-time analysis, and no Windows computer is a site.
