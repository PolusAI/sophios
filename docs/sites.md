# Sites

> **Your data must already be on the site.** Sophios sends the workflow there, never your data, and the results stay
> there beside your data. Give the project folder there as `relative_to`, and write input paths relative to it
> (`data/plate1`, not `/Users/me/...`): the same line then reads `./data/plate1` here and
> `/data/project/data/plate1` there. **Your runners are yours:** `bin` names the folder that holds them on the site.
> **Your container images must be in a registry the site can pull from.**

How do I run a workflow on another computer, as myself? A **site** is a
computer you can `ssh` to. `sophios.sites` compiles the workflow here, as
`workflow.start()` does, copies the run's files (never your data) into a
folder of the run there, and starts the runner from the `bin` folder you
name: as one Slurm job on a cluster, or as a process of its own on a
machine, which outlives ssh and Python. Nothing of Sophios is installed
there, and no service runs: the run's folder is its record, and Sophios on
any computer follows the run from it.

`sophios.sites` is a core feature of Sophios, but it is **not part of the
Python API**: nothing under `sophios.api.python` exports or documents it.
It is experimental. It has two kinds of site: `Machine`, a computer of
your own, macOS or Linux, such as a workstation or a cloud instance; and
`Slurm`, a cluster's login node, whose compute nodes share its folders. A
login node is never a `Machine`: a run there would run on the login node.
The `RunConfig` both take is the one a local run
([Choose where the results go](running.md#choose-where-the-results-go)) and
the [compute service](compute.md) take, and a site reads it as the compute
service does.

## A machine

```python
from sophios.sites import Machine, RunConfig

ec2 = Machine('ssh://me@my-ec2', runs='/data/runs', bin='/data/envs/run/bin')
run = ec2.submit(workflow, RunConfig('results').relative_to('/data/project'))
status = run.wait()                       # Ctrl-C stops following; the run goes on
```

`submit` prints the run's id, its runner and where its outputs go, each
with where it comes from, and the line that follows the run from any
computer:

```text
Submitting segment-20261125T040118-82bd5020 to my-ec2: toil-cwl-runner (a site's default for cwl), at most 64 at once (Sophios's default).
Outputs: /data/project/results (RunConfig(...))
From anywhere, Machine(url='ssh://me@my-ec2', runs='/data/runs', bin='/data/envs/run/bin', container_engine='docker').attach('segment-20261125T040118-82bd5020') follows it.
```

| `Machine(...)` | What it is |
|---|---|
| `url` | The computer: `ssh://[user@]host[:port]`, and nothing else, no path and no query. ssh is given `[user@]host`, and `-p <port>` with a port, so a `Host` of your `~/.ssh/config` is `ssh://my-ec2` |
| `runs=` | Required: the absolute folder there that holds a folder per run, `<runs>/<id>/`, and the image cache, `<runs>/.images`; letters, digits and `/ . _ - +` only, as the compute service takes its own folder |
| `bin=` | The absolute folder there that holds the runners and what they need: `toil-cwl-runner`, `cwltool`, `nextflow` and the Java Nextflow runs on; letters, digits and `/ . _ - +` only. It is first on `PATH` in every job, and Sophios never searches for a runner. `check` and `submit` need it; `attach` does not |
| `container_engine=` | The engine every runner runs containers with, `docker` by default; `podman`, `singularity`, or another command that works as `docker` does. A Nextflow pipeline runs its containers with `docker` or `singularity` |

No setting takes shell code. A URL of another form, and a `runs` or `bin`
that is not an absolute folder of letters, digits and `/ . _ - +` only, is
a `ValueError` when the `Machine` is made: Toil runs each step's command
through a shell, its job store, in `runs`, on that command, and `bin` is a
folder of `PATH`, which `:` splits. Every other value Sophios puts into a
script there, a path or a flag of yours, goes in quoted, as one word,
whatever it holds.

## A Slurm cluster

```python
from sophios.sites import RunConfig, Slurm, SlurmSettings

hpc = Slurm('ssh://me@login.cluster.example.org', runs='/project/me/runs', bin='/project/me/envs/run/bin')
config = RunConfig('results').relative_to('/project/me/data').slurm_settings(SlurmSettings(time='12:00:00'))
run = hpc.submit(workflow, config)        # Toil, each step a Slurm job of its own
status = run.wait()
```

`submit` prints what a machine's does, and where the run's jobs go, each
setting with where it came from, and the job Slurm took:

```text
Submitting segment-20261125T040118-82bd5020 to login.cluster.example.org: toil-cwl-runner (a site's default for cwl), at most 64 at once (Sophios's default).
Slurm: partition normal (the cluster's default), time 12:00:00 (SlurmSettings(time=...)), steps_partition normal (the run's partition), steps_time 12:00:00 (the run's time), account lab (your SBATCH_ACCOUNT).
Outputs: /project/me/data/results (RunConfig(...))
Slurm has it as job 4242.
From anywhere, Slurm(url='ssh://me@login.cluster.example.org', runs='/project/me/runs', bin='/project/me/envs/run/bin', container_engine='singularity').attach('segment-20261125T040118-82bd5020') follows it.
```

| `Slurm(...)` | What it is |
|---|---|
| `url` | The cluster's login node, as a `Machine`'s `url` |
| `runs=` | As a `Machine`'s, on folders the compute nodes share; its letters, digits and `/ . _ - +` are what each job's `#SBATCH` lines can hold too |
| `bin=` | As a `Machine`'s, on folders the compute nodes share |
| `container_engine=` | As a `Machine`'s, `singularity` by default, which Apptainer provides |

Where a run's jobs go is the config's `SlurmSettings`, the same six the
[compute service](compute.md#running-a-workflow) takes, in the same forms
and checked as the compute client checks them: a value in a form the
service refuses, or a time of 0, which Slurm reads as no limit, is a
`ValueError` in the client's words, every problem at once, before anything
is compiled or sent. A setting about a job the run does not have is
neither used nor refused:

| `SlurmSettings` | What it sets | When it is not given | Runners |
|---|---|---|---|
| `partition=` | The partition of the leader, the Slurm job that runs the runner as long as the run lasts | Your `SBATCH_PARTITION` there, else the cluster's default partition | Toil; cwltool, unless a step asks for a GPU |
| `time=` | The leader's time limit, the whole run's | Your `SBATCH_TIMELIMIT` there, else the longest its partition allows; with no limit, `check` refuses, `wic067`: give one | Toil, cwltool |
| `memory=` | The leader's memory | None asked for; under cwltool, the most memory a step states (`ramMin`, else `ramMax`) | Toil, cwltool |
| `steps_partition=` | Where each step's job goes | The run's partition | Toil |
| `steps_time=` | How long each step's job may run | The shorter of the run's time and the shortest limit of the partitions the steps go to | Toil |
| `gpu_partition=` | Where a step that asks for a GPU goes | The one partition with GPUs, which `sinfo -a` lists; with none or several, `check` refuses, `wic067`: name one | Toil; cwltool, whose one job goes there |

The account is yours: your `SBATCH_ACCOUNT` there, written into the
leader's `#SBATCH` lines and each step job's options, else Slurm's
default. Any other `sbatch` option, such as a QOS, goes in your login
profile there, `export SBATCH_QOS=...` in `~/.bash_profile` or
`~/.profile`, and reaches the leader and every step job. Sophios writes
the options it sets into the leader's `#SBATCH` lines and unsets, for
`sbatch`, your `SBATCH_` variables of those options, which would beat the
lines; the others stay yours. A partition or a time that Slurm or your
`SBATCH_` settings give reaches the same lines, so it is held to the forms
the setting takes, and a run needs a time limit, as on the compute
service: `check` refuses either, `wic067`, naming the setting to give.
Every partition a job of the run goes to must be one `sinfo -a` lists,
`wic067` otherwise; a time is not checked against its partition's limit:
Slurm keeps such a job waiting, and `status()` stops the run, naming the
setting ([Following a run](#following-a-run)), or, on a cluster that
enforces partition limits at submission, `sbatch` refuses it and `submit`
raises `wic062`, quoting Slurm, as
[Checking and submitting](#checking-and-submitting) says.

**Under cwltool, the run is one job.** cwltool runs every step inside the
leader, so the leader asks for what the largest step needs, as the steps
state it in numbers (a requirement written as an expression is not read):
`--cpus-per-task`, the most cores (`coresMin`, else `coresMax`), at least
1; `--mem`, `memory=`, else the most memory a step states, else none; and,
when a step asks for a GPU (`cwltool:CUDARequirement`), `--gres=gpu:<n>`,
the most GPUs a step asks for, on the GPU partition, whose longest limit is
then the time when neither `time=` nor your `SBATCH_TIMELIMIT` gives one.
`steps_partition=` and `steps_time=` are not used.

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
profile applies: what a computer provides through an environment module,
load it there. A profile may print what it likes. One that reads its input
would eat part of the script, so Sophios sends the script's sha256 with it
and runs it only when it arrives whole; otherwise it refuses with `wic062`.
Sophios runs on macOS, Linux or WSL: on Windows itself, making a site
raises `wic062`.

## Checking and submitting

`check` and `submit` take the workflow and a `RunConfig`, or none, and
these options, as `Workflow.run` takes them:

| Option | What it sets |
|---|---|
| `target=` | `'cwl'`, the default, or `'nextflow'` |
| `runner=` | The runner as a value, `Toil(...)`, `Cwltool(...)` or `Nextflow(...)`: its flags ([Runners](#runners)). None for the site's default, Toil for `'cwl'` and Nextflow for `'nextflow'` |
| `tool_registry=`, `inference=` | How the workflow compiles, as `Workflow.compile` takes them |

`check(workflow, config)` compiles the workflow here and lowers it to its
target, so a workflow the target cannot run is refused with its `nf-s` code
before anything reaches the site. One call there then checks every File
and Directory the run reads, the runner in `bin` and its version, the
container engine when a step runs in a container (`docker info`, or
`singularity --version`), that `runs` can be written, and the outputs
folder ([A folder you name](#a-folder-you-name)). On Slurm the same call
asks `sbatch`, `squeue`, `scancel`, `sacct` (accounting must answer: a run
is read from it) and `sinfo -a` (the partitions, their limits and which
have GPUs), reads your `SBATCH_ACCOUNT`, `SBATCH_PARTITION` and
`SBATCH_TIMELIMIT`, and refuses `SBATCH_CLUSTERS` and `SLURM_CLUSTERS`,
which send jobs to another cluster, where no runner follows its step jobs,
each `wic067`. It returns every problem at once, with every note the
compile made, and under Toil a note, `wic066`, at each step whose tool
declares no memory; it writes nothing, here or there. A workflow with
real-time analyses is refused, `api008`: they watch cwltool's job folders
here.

`submit(workflow, config)` makes every check of `check`, raises its errors
as a `SophiosError` with nothing written, and otherwise prints the lines
above, starts the run in one call there and returns its `Run` at once. A
run's id is the workflow's name, the UTC time and eight hex digits. The
call ignores hang-ups, so a dropped connection cannot stop it between
starting the run and recording it; should it fail all the same, the error
says how to attach to the run, which may have started. One that did not
start holds its outputs folder for ten minutes after the submit, `QUEUED`
until then, and then ends `FAILED`.

On Slurm the call ends with `sbatch --parsable <run>/job.sh`. The leader
job is named after the run and carries the comment `sophios:<id>`, as each
of Toil's step jobs does. When the call fails or its answer is lost,
`submit` asks Slurm for a job named after the run, where the compute
service asks, `squeue` and then `sacct` since the submit, and adopts it;
if there is none, it runs `sbatch` again on the same folder. A run has one
leader all the same: the first job to start claims the run,
`--dependency=singleton` runs one job of that name at a time, and a later
one finds the claim and exits having changed nothing. When Slurm refuses
that `sbatch` too, the run is claimed `none`, so no job of it ever runs
it: `submit` raises `wic062`, quoting Slurm and saying the run did not
start, its `status()` reads `FAILED` with Slurm's refusal as its
`reason`, and its outputs folder is free for the same `RunConfig` again.
Only when Sophios cannot tell, as when that second call fails too, does
the error say how to attach to the run.

## The config

A `RunConfig` is read on a site as the compute service reads it
([Running a workflow](compute.md#running-a-workflow)): a config may be made
with the outputs folder, the folder there every output lands in, new or
empty, relative to `relative_to` when the config sets one, else absolute.
`relative_to` and the outputs folder are a pair, the project's root and the
outputs folder under it, and a site has no working directory to read a
relative folder from: a config that sets `relative_to` without the folder,
or with an absolute folder, a relative folder without `relative_to`, or a
`relative_to` that is not an absolute path, is refused with a `ValueError`
before anything is compiled or sent, every problem at once. So is what the
compute service refuses in its own words: a folder or `relative_to` that is
not a string, an empty folder, a folder that is a URL (`file:///...` too:
give the path), a path written for a shell, and a folder that holds a
control character, `?`, `#` or a `%`-escape, or starts with a blank, which
cwltool would read otherwise than as the path it spells. `..`, `.` and a
doubled or trailing `/` in the folder are read by name, as the service
reads them.

| `RunConfig` | On a site |
|---|---|
| `RunConfig(folder)` | The outputs folder: the folder there every output lands in, relative to `relative_to` when one is set, and then required, else absolute; missing in a folder that exists, or empty |
| `RunConfig()` | No outputs folder, and so no `relative_to`: the outputs land in the run's own folder, `<runs>/<id>/out` |
| `.relative_to(path)` | The absolute folder there that relative paths are relative to: those the tools read, and the outputs folder, which it requires; required when a tool reads a relative path |
| `.slurm_settings(SlurmSettings(...))` | On Slurm, where the run's jobs go and how long they may last ([A Slurm cluster](#a-slurm-cluster)); not used on a machine, which has no Slurm job |
| `.log_level(level)` | Toil's `--logLevel`, given before the flags of `Toil(...)`, so a level given there wins; cwltool and Nextflow do not use it |

Every path is a path there, absolute or relative to `relative_to`, or an
http(s) URL; it must exist and be readable. No path may start with `~` or
hold a `$VARIABLE`: a shell expands those on the computer that wrote them,
and no shell reads them on the site. A workflow that reads a relative path
names an outputs folder, since `relative_to` comes with one.

## Where the outputs go

Everything a run returns lands in the outputs folder, under each output's
own name, as on the compute service
([Where the outputs go](compute.md#where-the-outputs-go)):

| The workflow gives | Without a folder | `RunConfig('/data/run7')` |
|---|---|---|
| A file output, `glob='hello.txt'`, or stdout `hello.txt` | `<runs>/<id>/out/hello.txt` | `/data/run7/hello.txt` |
| A file output, `glob='results/p1/out.tsv'` | `<runs>/<id>/out/out.tsv` | `/data/run7/out.tsv` |
| A directory output, `glob='tiles'` | `<runs>/<id>/out/tiles/` | `/data/run7/tiles/` |
| An output folder given a name, `'masks'` | `<runs>/<id>/out/masks/` | `/data/run7/masks/` |
| An output folder given an absolute path, `'/data/masks/p1'` | refused | refused |

An **output folder** is a `Directory` input that a tool writes into and
returns, such as the folder a segmentation tool fills. **Given just a
name**, such as `step.inputs.output = 'masks'`, it is made empty in the
run, never anywhere `relative_to` points, and copied into the outputs
folder under that name. The name is one folder name: `seg/p1` would land
as `p1`, and is refused, `wic016`. **Given an absolute path** it is
refused, `wic016`, on either target: a site never writes one in place, so
give it a name. **Given in a list**, as a scattered step takes its output
folders, each is refused, `wic016`, as a local run makes none of them.

### A folder you name

The outputs folder of `RunConfig(folder)` is checked by `check`, and again
as the run starts:

| The folder is | What happens |
|---|---|
| missing, in a folder that exists | made as the run starts, one level |
| missing, and so is the folder it would be in | refused, `wic021` |
| empty, and used by no run there that has not ended | taken as it is |
| used by a run there that has not ended: that folder, one inside it, or one around it | refused, `wic021`, naming that run |
| not empty, or a file | refused, `wic021`: a run writing there would replace same-named files and merge into same-named folders |
| a link | refused, `wic021`: a site reads every path by name, and follows no link; name the folder itself |
| inside a folder the run reads | refused, `wic021`: put it beside it |
| `runs`, inside it, or around it | refused, `wic021`: `runs` holds every run's folder and the image cache, which a failed run's cleanup would empty |

So a folder made beforehand works, and so does the same `RunConfig` again
after a run that failed there, which left it empty, or whose job Slurm
refused; after a run that succeeded, the folder holds its outputs: name a
new one. A folder that was
free at `check` and holds something when the run starts stops the start,
`wic062`, before anything is written there. A run that starts takes the
folder: it writes its record, then looks for a run under the same `runs`
that has not ended and uses the folder, one inside it or one around it,
and finding one it does not start. So of two submits into one folder at
the same moment, at least one is refused, `wic021`, naming the other, and
its run ends `FAILED`, claimed by no job, its `reason` saying why. Runs
under another `runs`, on this computer or another, do not see each other:
two of them that start into one empty folder are both taken, and one that
fails then empties the folder, the other's outputs with it. Everything a
run writes is yours, and your umask applies.

### The run's own folder

Without a folder, the outputs land in the run's own folder,
`<runs>/<id>/out`: a new folder for every run, with no name to invent.
No run's folder is ever deleted. What a run removes as it ends is its
outputs, when it fails or is cancelled ([When a run fails](#when-a-run-fails)),
and Toil's scratch, as `--clean` says ([Cleanup](#cleanup)).

### When a run fails

A run that fails or is cancelled leaves its outputs folder empty: the job
empties it as it ends. The folder was missing or empty when the run
started, and no other run under the same `runs` takes it until this one
has ended, so everything in it is the run's; a run under another `runs`
can share it ([A folder you name](#a-folder-you-name)). A run whose job was killed
outright (SIGKILL, out of memory, the computer stopped) has its folder
emptied by the first `status()` that finds the job gone, and it has not
ended until then: its folder stays its own, and a `check` of another run
into it is refused, naming the run, until a `status()` of it has run. So
no `status()` ever empties a folder another run has written into since.

## Runners

A site runs each target with one runner from `bin` unless `runner=` names
another:

| Target | Runner | On a machine | On Slurm |
|---|---|---|---|
| `target='cwl'`, the default | Toil, by default | Toil's `single_machine` batch system: at most 64 steps at once (`--maxJobs`), each step's outputs and scratch in the run's folder | Toil's `slurm` batch system: each step a Slurm job of its own, on the steps' partition for the steps' time, a step that asks for a GPU on the GPU partition; at most 64 in Slurm's queue at once (`--maxJobs`); each step's outputs and scratch in the run's folder, its Slurm log in `<run>/batch` |
| `target='cwl'` | `runner=Cwltool(...)` | cwltool, one step at a time unless its flags say otherwise; it pulls the images it needs as it runs | The same, inside the leader, one job sized to the largest step ([A Slurm cluster](#a-slurm-cluster)) |
| `target='nextflow'` | Nextflow | Nextflow's local executor: at most 64 tasks at once (`executor.queueSize`, which the site adds to the run's `nextflow.config`). The pipeline publishes the outputs into the outputs folder, as a local run does ([Outputs](nextflow.md#outputs)) | Not yet: `check` refuses it, `wic067` |

A site needs cwltool 3.1.20241217163858 (the version Sophios itself
requires), Toil 9.5.0 and Nextflow 26.04.2 or later; `check` reads the
version of the one the run uses. A runner pulls the images its steps need as
it runs, so they must be in a registry the site can pull from; under
Singularity they are kept in `<runs>/.images`, and Singularity's and
Apptainer's own cache is `<runs>/.images/cache` unless your profile names
one. A Nextflow pipeline is written for the site's engine, `docker` or
`singularity`; on a site with another engine each step with a container is
refused, `wic065`, by name. A Toil step whose tool declares no memory gets
4 GiB there (`--defaultMemory 4Gi`), and Toil refuses to start it on a
machine with less, and on Slurm asks for 4 GiB for its job: `check` notes
each such step, `wic066`. The tool's own `ramMin` wins.

### Runner flags

`runner=` takes the runner as a value, as `Workflow.run` does, and its flags
are passed on to the program in order, after the site's own:

```python
from sophios.api.python.workflow import Cwltool, Nextflow, Toil

config = RunConfig('run8').relative_to('/data/project')
run = ec2.submit(workflow, config, runner=Toil('--maxJobs', '8', '--clean', 'onSuccess', '--retryCount', '2'))
run = ec2.submit(workflow, RunConfig('masks').relative_to('/data/project'), target='nextflow',
                 runner=Nextflow('-qs', '8'))
run = ec2.submit(workflow, RunConfig('copies').relative_to('/data/project'), runner=Cwltool('--parallel'))
```

The site reads three flags itself, given as `--flag value` or
`--flag=value`, and gives each to the runner where it reads it:

| Flag | What it sets |
|---|---|
| Toil's `--maxJobs`, Nextflow's `-qs` | How many steps run at once, 64 when not given. The site writes Nextflow's as `executor.queueSize` in the run's `nextflow.config` |
| Toil's `--clean` | When Toil's scratch is removed: `always`, when not given, `onSuccess`, `onError` or `never` ([Cleanup](#cleanup)) |

A flag the site or the runner's command line already sets, such as
`--outdir`, `--jobStore`, `--batchSystem` or `-output-dir`, or a setting
the site writes into Nextflow's configuration, `-executor.queueSize`, is
refused by name, and so is the option's other name, as the runner's own
help gives it: Toil's `--jobstore`, cwltool's `-w` (`--write-summary`),
Nextflow's `-o` and `-w` (`-output-dir`, `-work-dir`); Nextflow's
`-queue-size` is its `-qs`, which the site reads. A runner value with a
setting of its own is refused too, such as `Toil(container_engine='podman')`:
a site's engine is its `container_engine=`, and it pulls, checks and caches
images by itself. Each refusal is a `ValueError`, raised before anything is
compiled.

Every other flag is passed on as it is, as a local run passes it, and the
runner reads it as it reads it there. `Cwltool(quiet=...)` is not used: a
site's run is never quiet, as its log is where a failed step is read from;
a `--quiet` among cwltool's flags, or its own `--disable-pull`, which the
site does not give it, is passed on. A flag that undoes one the site sets,
such as Toil's `--cwl-default-ram` or cwltool's `--leave-outputs`, is passed
on, and the runner reads the last; so is an abbreviation cwltool takes for
an option (`--outd` for `--outdir`), which cwltool reads as that option, as
in a local run.

## Following a run

A run on a site is followed as a local one is: `machine.submit(workflow)`
is `workflow.start()`, and the `Run` it returns has the local `RunHandle`'s
`status`, `wait`, `outputs`, `logs` and `cancel`
([Follow a run from Python](running.md#follow-a-run-from-python)).

| | Local, `workflow.start()` | On a site, `machine.submit(workflow, config)` |
|---|---|---|
| `status()` | Where the run is, without blocking: a `RunStatus` | The same, read from the run's folder there; once it has ended, as it ended, its outputs and failed steps read by the runner's own outcome, as locally |
| `wait(timeout=None)` | Blocks until the run ends, and returns its `RunStatus`; after `timeout` seconds it raises `TimeoutError`, and the run goes on | The same, reading every 5 s for a minute, then every minute, and printing what `run.log` gained and each change; Ctrl-C prints how to follow the run again, and the run goes on |
| `outputs()` | The workflow's output object once the run has completed; `None` until then | The same, with paths there |
| `logs()` | Everything the runner has printed so far | `run.log` so far: the job's and the runner's two streams |
| `cancel()` | Stops the run: `True` once it is `CANCELLED`, by this call or before; `False` when it had ended `COMPLETED` or `FAILED`; its `reason` is `cancelled: the runner was stopped` | The same, its `reason` `cancelled`: a run whose job has not started never will, and a running one is stopped, its process group with TERM on a machine, every job named after it with `scancel` on Slurm, where a `scancel` Slurm refuses raises `wic062` having changed nothing; it waits up to two minutes for the end |
| `collect(timeout=None)`, `workflow.run()` | `wait()`, then the run's `RunResult` | Local only |
| `id`, `machine.attach(id)` | None | On a site only: the run's id, which `attach` follows from any computer, given the same kind of site, URL and `runs`; `repr(run)` is that line |

The states are those of a local run. A run is `QUEUED` until its job has
claimed it, a moment after the submit on a machine; one that no job has
claimed ten minutes after the submit, and of which Slurm lists no job that
may still claim it, ended `FAILED`, and its `reason` says why. When ssh or
the site does not answer, a method raises `wic062` and decides nothing.

On Slurm, `status()` reads the run's folder first, and asks Slurm only
while the folder has not said how the run ended: `squeue` for the run's
jobs, the leader by its name and Toil's step jobs by the run's comment, and
`sacct` for a leader that has left the queue. When either fails,
`status()` raises `wic062` and writes and cancels nothing.

- **`QUEUED`** while the leader waits in Slurm's queue, its `reason`
  Slurm's (`waiting in Slurm's queue (Priority)`), however long it waits;
  such a run keeps its outputs folder.
- **`RUNNING`** while the leader that claimed the run runs, and, while no
  `exit` shows, for two minutes after Slurm says it ended, so that its
  `exit` can show on a shared folder.
- **A job Slurm will never start**, the leader or a step job waiting for a
  limit no wait clears (`PartitionTimeLimit`, a per-job limit of your QOS
  or account, an account or QOS you may not use): `status()` stops the
  run, as `cancel()` does, and it ends `FAILED`, its `reason` naming what
  to change, such as `give a shorter SlurmSettings(time=...)`, or
  `steps_time=` for a step's job. The `reason` is written once Slurm has
  taken the `scancel`.
- **A leader that ended by no `exit` of its own**: `CANCELLED` when Slurm
  cancelled it, else `FAILED` with Slurm's state; a time limit reads
  `Slurm ended the run's job TIMEOUT: it reached its time limit: give a
  longer SlurmSettings(time=...)`. The status that finds it cancels the
  step jobs it left in Slurm's queue.

## The run's folder

```text
<runs>/<id>/
  run.json                  the id, the site's kind, the target, the runner and its flags, the settings it read,
                            on Slurm where its jobs go, and the outputs folder, each with its source, Sophios's
                            version
  job.sh                    the job script
  <workflow>.cwl            a CWL run's workflow, one document, and <workflow>_inputs.yml, its job
  nextflow_<workflow>/      a Nextflow run's pipeline, its configuration, its log and its work/ folder
  leader                    the claim: a link to the job's pid, on Slurm its job id, or to `cancelled` or `none`
  run.log                   the job script's and the runner's two streams
  stdout                    the runner's standard output alone, which its outcome reads
  provenance/               a CWL run's provenance, as a local run writes it; Toil on Slurm writes none
  batch/                    Toil's step jobs' Slurm logs, on Slurm
  output_<workflow>.json    cwltool's summary of the outputs, which its outcome reads
  toil/                     Toil's work/ and tmp/: every step's outputs and scratch, removed as --clean says
  jobStore_<workflow>/      Toil's job store, while it runs, and after it when --clean keeps it
  made/                     a Nextflow run's output folders given a name, before they are published
  out                       the outputs folder; when the config names one, a link to it
  reason                    why the run ended, written by the status that found its job killed outright, or
                            that stopped a run Slurm would never start, or by the submit whose job Slurm refused,
                            or whose outputs folder another run took as it started
  exit                      the job script's exit status, written last, after the cleanup
```

## Cleanup

Toil's `--clean always`, the default, cleans up however a run ends:
success, failure or cancel. Only Toil leaves scratch outside its outputs:
its `--clean always` removes its job store, and once Toil has stopped the
job script removes `toil/tmp` and `toil/work`, making writable first what a
tool left unwritable; neither follows a link, so linked inputs stay intact.
`onSuccess`, `onError` and `never` are Toil's own names, given as
`runner=Toil('--clean', 'onSuccess')`: the first keeps the job store and
`toil/` after any end but a success, the second after a success alone, the
third always. Nextflow keeps its
work folder in the run's, `nextflow_<workflow>/work`, which stays; cwltool
removes its own temporary folders.

A hard kill stops the job before its cleanup: the run reads `FAILED`, and
for Toil its `reason` names `<run>/toil` and the `rm -rf` line that removes
it. On Slurm, the `status()` that finds a leader killed cancels the step
jobs it left in Slurm's queue, by the run's comment, whatever `--clean`
says. A kill of the job's process group stops the runner, but not what Toil
started: each step runs in a worker of its own process group, which goes on
until its step ends. Find such a worker with `ps` (its command line names
the run's id) and stop its process group, `kill -- -<pgid>`.

## How a site differs from the compute service

The same `submit(workflow, config)` behaves on a site as on the compute
service but for these:

- **An output folder given an absolute path** is refused on a site, on
  either target: a site runs the workflow as a local run does, which never
  writes one in place. The service writes it in place on CWL.
- **An outputs folder written as a `file:///` URL** is refused on a site:
  give the path. The service takes it.
- **A link:** the service resolves every path to its real path, and a site
  reads each by name. So a site
  refuses an outputs folder that is a link, which the service takes as the
  folder it points to; and an outputs folder that reaches inside a folder
  the run reads only through a link is not seen as inside it.
- **An output folder given a name with a final `/`**, `'masks/'`, is
  refused on a site, as a local run refuses it; the service takes it as
  `'masks'`.
- **Output folders given as a list**, as a scattered step takes them, are
  refused on a site, `wic016`, each element: neither a site nor a local run
  makes them. The service makes each.
- **Output names under Nextflow:** the service refuses, `wic061`, an output
  Nextflow would rename and an output the workflow does not declare. A site
  runs the pipeline a local run runs: it takes them, and an output whose copy
  took another name (`out.txt_2`) keeps the tool's own name as its
  `basename`, where the service reports the copy's.
- **`SlurmSettings` are not used** on a machine, which has no Slurm job,
  nor, on Slurm, `steps_partition=` and `steps_time=` under cwltool, whose
  run is one job.
- **Containers** run under the site's `container_engine=`, Docker by
  default on a machine and Singularity on Slurm; the service runs every
  container under Singularity.
- **The run is yours.** It runs as you, everything it writes is yours with
  your umask, and no run's folder is deleted: the service runs as its own account,
  gives what a run wrote to the folder's group, deletes a run's own folder
  some days after it ended, and refuses a run when its disk is full. On
  Slurm a site's jobs go to your account, your `SBATCH_ACCOUNT` or Slurm's
  default, with your other `SBATCH_` settings, such as a QOS; the
  service's go to its own account, and take none of yours.
- **A time beyond a limit:** the service refuses, at submit, a `time=` or
  `steps_time=` longer than its partition allows or than the service
  account's QOS or association allows. A site checks no time against a
  limit: Slurm keeps such a job waiting, and `status()` stops the run,
  its `reason` naming the setting to change, or Slurm refuses it at
  submission where it enforces partition limits.
- **What is used when a setting is not given:** the service takes its
  driver's own settings first. Then the leader's partition is the cluster's
  default on both, a site taking your `SBATCH_PARTITION` before it; its time
  the longest its partition and the service account allow on the service,
  your `SBATCH_TIMELIMIT`, else the longest its partition allows, on a
  site. The steps' time is the longest that the run's time, the steps'
  partition, `gpu_partition=` and the service account allow on the service,
  and on a site the shorter of the run's time and the limits of the
  partitions the steps go to, the GPU partition only when a step asks for a
  GPU. On both a run whose time would be no limit, a partition's with
  none, is refused, asking for a time. A site also holds a partition or a
  time that Slurm or your `SBATCH_` settings give to the forms the setting
  takes, and refuses another, `wic067`; the service takes the cluster's as
  Slurm writes it.
- **Which partitions there are:** a site reads `sinfo -a`, which lists
  hidden partitions too, so a `partition=` naming one is taken there; the
  service reads `sinfo` alone, and refuses it.
- **Nextflow on Slurm:** `target='nextflow'` is refused on a Slurm site,
  `wic067`; the service runs it.
- **A step that asks for a GPU with no `gpu_partition=`** is refused by the
  service, unless its driver names a GPU partition; a site sends it to the
  one partition with GPUs. A `gpu_partition=` the cluster does not have is
  refused by the service even when no step asks for a GPU; a site checks it
  only when one does.
- **The leader's size under Toil:** the service's leader asks for 2 cores
  and 16G unless its driver or `memory=` says otherwise; a site's asks for
  no cores and, but for `memory=`, no memory.
- **A Toil step on Slurm** that declares no memory gets 8 GiB on the
  service and 4 GiB on a site; the service queues as many step jobs at once
  as Toil makes, a site at most 64 (`--maxJobs`).
- **A run's steps:** the service's `status().steps` lists each step's
  Slurm job, Toil's or a Nextflow task's, by its step's name, with its
  state, exit code and job id, while the run goes on and after it; a
  site's, under any runner, lists only the failed steps the runner's
  outcome names, with their exit codes and no job id, once the run has
  ended.
- **How the jobs are named:** the service's comment on every job of a run
  is `slurm-driver:<id>`, a site's `sophios:<id>`.
- **Nodes left out:** the service's driver may name nodes no job of its
  runs goes to (`--exclude`); a site leaves out none.
- **Nextflow's own settings:** the service's Nextflow runs offline, with a
  home of the run's own, and reads none of the account's `NXF_` and `JAVA_`
  variables; a site's reads yours, as a local run does.
- **A steps' time whose days are 0**, such as `0-12`: the service gives it
  to Toil as written, which Toil 9.5 reads as 12 minutes; a site gives Toil
  the time as hours, minutes and seconds, so a step may run 12 hours, as
  Slurm reads the time.
- **An input path** is refused at `check` when it is missing, unreadable,
  written for a shell, or relative without `relative_to`. A site writes it
  for the runner as a local run does, so the service's further refusals of
  a name cwltool would misread (a control character, `?`, `#`, a
  `%`-escape) or refuse are not made; a name the runner cannot read fails
  the run.
- **Paths outside the service's shared folders:** the service reads and
  writes only under the folders its admin shares, and refuses an input,
  `relative_to` or an outputs folder anywhere else; a site reads and writes
  any path you can.
- **Your Toil settings:** the service does not start while its account's
  `~/.toil/default.yaml` sets an option, so its Toil reads none; a site
  unsets only your `TOIL_` variables, so its Toil, on a machine and on
  Slurm, reads every option your `~/.toil/default.yaml` sets.
- **Two runs into one outputs folder:** the service takes a folder for one
  run at a time, whoever submits them. A site does so among the runs under
  one `runs`; runs under another `runs`, or on another site, do not see
  each other, and two of them that start into one empty folder are both
  taken: one that fails then empties the other's outputs
  ([A folder you name](#a-folder-you-name)).
- **A cancelled run's `reason`** is `cancelled` on a site, `cancelled by
  request` on the service.

And two ways it differs from a local run of the same workflow. A cancelled
run's `reason` is `cancelled`, a local run's `cancelled: the runner was
stopped`. And a `StepInput(default=...)` File or Directory is made absolute against the
working directory here when the workflow is built
([Choose where the results go](running.md#choose-where-the-results-go)), so
on a site it names a path of this computer, which the runner there does not
find. Bind such a value on a workflow input instead, which is read from
`relative_to`.

## When something goes wrong

A workflow that does not compile raises a `SophiosError`, with Sophios's
own [error codes](error_codes.md), as a local run does. A problem of the
site is a code of kind `machine`, which `sophios --explain` explains:

- `wic062`: the site cannot be reached or used: ssh failed, the login
  profile there read the script, a call there failed, the site is neither
  macOS nor Linux, an outputs folder filled between `check` and the
  start, or Slurm refused the run's job. Run `ssh` to the host in a
  terminal and fix what it says; the message quotes what ssh, the site or
  Slurm said.
- `wic063`: `bin` holds no runner you can run, or an older one than a site
  runs. Install the runner there, or name the folder that holds it.
- `wic064`: `attach` was given an id that names no run under `runs`, or
  one that the other kind of site submitted: the message names the kind to
  attach with.
- `wic065`: a Nextflow step with a container, on a machine whose engine is
  neither Docker nor Singularity.
- `wic066`, a note: a Toil step whose tool declares no memory, which gets
  4 GiB there.
- `wic067`: Slurm cannot run the run: one of its commands is missing or
  does not answer (`sacct` too), your login profile there sends jobs to
  another cluster or sets an `SBATCH_ACCOUNT` the run cannot take, a
  partition a job of the run goes to is not there, a step asks for a GPU
  and no one partition has GPUs, the run would have no time limit, a
  partition or a time Slurm or your `SBATCH_` settings give is in a form
  the setting would not take, or the runner is Nextflow, which a `Slurm`
  site does not run yet.
- `wic015`, `wic016`, `wic021`: the container engine, an input or an output
  folder of the workflow, or a folder Sophios writes, as for a local run,
  each message naming the site.
- `api008`: a workflow with real-time analyses, which no site runs.

A run that fails raises nothing, as a local one started with `start()`
does: `wait()` returns its status, `FAILED`, whose `reason` and `steps`
say why, and `run.logs()` holds the rest.

## What a site does not do

- It copies no data and fetches no outputs: `rsync` does both.
- It resumes nothing: a run that failed is submitted again from the start.
- It deletes no run's folder, and finds no runner: `bin` names them.
- It runs no real-time analysis, and no Windows computer is a site.
- It does not run Nextflow on Slurm yet: `check` refuses it, `wic067`.
