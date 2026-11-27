# Sites: Run a Workflow on a Cluster or a Machine as Yourself — Design

**Status:** Approved by the owner on 2026-11-09. Experimental. S1, S2 and S3 are implemented; S4 is not yet, so
what this file says of Nextflow on Slurm is its plan, not built. A commit that changes this file has a subject beginning
with `design:`. What S2 found while it was built is in §17, what S3 found in §18.
**Baseline:** PolusAI/sophios#586 (`feat/one-target-table`, measured at `865a3455`), itself on #585. Read against
Toil 9.5.0, cwltool 3.2.20260413085819 and Nextflow 26.04.2. It supersedes the sites design of 2026-10-29; every
measured fact below that this design keeps was measured for that design's draft of 2026-10-28.
**Scope:** four PRs, S1 to S4, each stacked on the one before, on top of #586 (§15). S1 changes #586's backend
protocol and nothing a user sees; S2 to S4 add `sophios.sites`. A later PR changes this file only by a `design:`
commit.

---

## 1. In one paragraph

Sophios stays a local install. A site is any computer you can `ssh` to: a Slurm login node, an EC2 instance, a
workstation. Sophios compiles and lowers the workflow here, through #586's target table, and writes the run's files
here. It copies those files (never the data) into a run folder there, and starts the runner, from the `bin` folder
you name: as one leader job on Slurm, or as a detached process on a machine. Every target of the target table runs on
every site, by a default runner or the one `runner=` names. A run takes the `RunConfig` a local run and the compute
service take, and its outputs folder behaves as the service's. Nothing of Sophios is installed there, and no service
runs. The run folder is the record, and any Sophios on any computer follows the run from it. `wf.run()` and
`collect()` stay local only.

## 2. The first message (every doc page and demo opens with it)

> **Your data must already be on the site.** Sophios sends the workflow there, never your data, and the results stay
> there beside your data. Give the project folder there as `relative_to`, and write input paths relative to it
> (`data/plate1`, not `/Users/me/...`): the same line then reads `./data/plate1` here and
> `/data/project/data/plate1` there. **Your runners are yours:** `bin` names the folder that holds them on the site.
> **Your container images must be in a registry the site can pull from.**

What still differs: the container engine (Docker here, Apptainer on a cluster), and a Toil step that declares no
memory gets no limit here and 4 GiB there.

## 3. What a scientist writes

```python
from sophios.api.python.workflow import Cwltool
from sophios.sites import Machine, RunConfig

ec2 = Machine('ssh://me@my-ec2', runs='/data/runs', bin='/data/envs/run/bin')   # or a Host: ssh://my-ec2

segment.inputs.images = {'class': 'Directory', 'location': 'data/plate1'}  # relative: here and there
config = RunConfig('results').relative_to('/data/project')
run = wf.start(config=RunConfig('results'))         # here
run = ec2.submit(wf, config)                        # there: CWL with Toil, single_machine
# Submitting segment-20261109T142205-7f3a91c2 to my-ec2: toil-cwl-runner (a site's default for cwl), at most 64 at
# once (Sophios's default).
# Outputs: /data/project/results (RunConfig(...))
run = ec2.submit(wf, RunConfig('masks').relative_to('/data/project'), target='nextflow')   # Nextflow, Docker
status = run.wait()          # Ctrl-C stops following; the run goes on
later = ec2.attach('segment-20261109T142205-7f3a91c2')
diagnostics = ec2.check(wf, config, runner=Cwltool())    # every problem submit would hit, at once; writes nothing
```

On Slurm the same calls take `Slurm('ssh://me@cluster', runs=..., bin=...)`, and the config's `SlurmSettings` say
where the jobs go (decisions 38 to 40):

```python
hpc = Slurm('ssh://me@login.cluster', runs='/project/me/runs', bin='/project/me/envs/run/bin')
run = hpc.submit(wf, config.slurm_settings(SlurmSettings(time='12:00:00')))   # Toil, each step a Slurm job
# Slurm: partition normal (the cluster's default), time 12:00:00 (SlurmSettings(time=...)), steps_partition normal
# (the run's partition), steps_time 12:00:00 (the run's time), account lab (your SBATCH_ACCOUNT).
```

## 4. The surface

`sophios.sites` is a core feature, not part of the Python API: `sophios.api.python` neither exports nor knows it, as
with `sophios.compute`.

```python
Machine(url, *, runs, bin=None, container_engine='docker')
Slurm(url, *, runs, bin=None, container_engine='singularity')
site.check(workflow, config=None, *, target='cwl', runner=None, tool_registry=None,
           inference=Inference()) -> list[Diagnostic]
site.submit(workflow, config=None, *, target='cwl', runner=None, tool_registry=None,
            inference=Inference()) -> Run
site.attach(run_id) -> Run
Run: id, status() -> RunStatus, wait(timeout=None) -> RunStatus, outputs() -> Json | None, logs() -> str,
     cancel() -> bool
```

- **A site's constructor says only what the computer is** (decision 30): `url`, `ssh://[user@]host[:port]` and
  nothing else, ssh given `[user@]host` and `-p <port>`; `runs`, the absolute folder there that holds a folder per
  run; `bin`; the container engine. Messages name the host.
- **`bin` is the folder on the site that holds the runners** and what they need: `toil-cwl-runner`, `cwltool`,
  `nextflow` and the Java Nextflow runs on. `check` and `submit` need it, a `ValueError` naming `bin=` without it;
  `attach` does not. Sophios never searches a login shell for a runner.
- **`config` is the `RunConfig`** a local run and the compute service take (decision 28), read as the service reads
  it, its outputs folder behaving as the service's (decision 33).
- **`target=` and `runner=` are `Workflow.run`'s** (decisions 34 to 36): `runner=None` is Toil for `cwl` and
  Nextflow for `nextflow`; any runner of the target runs on a site, cwltool too; its flags are passed on, but those
  the site sets itself; its own settings stay their defaults.
- **`check` returns diagnostics, as #586's `Workflow.check` does**, and writes nothing on either side. `submit`
  runs every check of `check` and raises them as a `SophiosError`.
- **`Run` is the local handle's protocol** (`status`, `wait`, `outputs`, `logs`, `cancel`, as `RunHandle` and
  `sophios.compute.Run`), plus `id`, with no `collect`. Its `repr` is the line that attaches to it. Its run outlives
  Python. When SSH or the scheduler does not answer, `status()` raises and decides nothing.
- **No setting takes shell code.** The URL is checked against its pattern and `runs` and `bin` are absolute folders
  of letters, digits and `/ . _ - +` only when the site is built (a `ValueError` names the one that is not, decision
  30); every other value, a path or a flag, enters a script only quoted, as one word, whatever it holds.
- **`Machine` is a computer of your own, never a login node.** Workstations are macOS and Linux; Windows is refused.
- **`Slurm` is a cluster's login node** whose compute nodes share `runs`, `bin` and the folders a run reads and writes.
  Each run is one Slurm job, its leader, and under Toil each step a job of its own, placed as the config's
  `SlurmSettings` say (decisions 38 to 40). Its `runs`, letters, digits and `/ . _ - +` only as every site's
  (decision 30), is also what each job's `#SBATCH` lines can hold (decision 41).

## 5. The one table Sites adds: each target's runner on each scheduler

A site is a transport and a scheduler. The transport is your own `ssh` for every site: nothing about it is per site.
The scheduler is the only per-site code: how the leader starts so it outlives SSH, how its state is read, how it is
stopped, and how each runner spreads the steps over it:

| Target (one of `targets.TARGETS`) / its runner on a site | `Machine` | `Slurm` |
|---|---|---|
| `cwl` / `toil-cwl-runner`, the default | `--batchSystem single_machine` | `--batchSystem slurm`, each step a Slurm job of its own (Appendix A) |
| `cwl` / `cwltool` | as locally, one process | one job, sized to the largest step, on the GPU partition when a step asks for a GPU (decision 42) |
| `nextflow` / `nextflow` | the local executor | S4, not yet built: `executor = 'slurm'`, each task a Slurm job of its own; a GPU task on the GPU partition, with `--gres`. Until then `check` refuses it, `wic067` |

- **Every target of the table runs on every scheduler.** A test holds that every target of `targets.TARGETS` has a
  runner a site runs it with by default, and every runner of `runners.RUNNERS` a line on a machine, so a target or a
  runner added there cannot silently miss Sites.
- **Each target has a default runner on a site, and any runner of the target runs there** (decision 35). Toil runs
  the same CWL on a machine and on Slurm, so it is the default for `cwl`, where a local run's is cwltool; `runner=`
  names another, as locally.
- **A Nextflow pipeline is written for the site's engine** (decisions 29 and 36): Docker's or Singularity's, as a
  local run writes it for its runner's. On a site whose `container_engine` is neither, `check` refuses each process
  with a container, by name (`wic065`); a workflow whose processes have none runs there.
- **What a site sets for Nextflow is a block it appends to the run's copy of the generated `nextflow.config`,** the
  one configuration the backend has Nextflow read (`-C`), from one function in `sophios.sites` (decision 25). Nextflow
  is set by its configuration, and only a configuration sets a task's options by the task. The Nextflow backend never
  learns of sites (decision 18).
- **How many steps run at once** is Toil's `--maxJobs` and Nextflow's `executor.queueSize`: 64, or what the runner
  value says, with `--maxJobs`, or with Nextflow's `-qs`, which the site writes in that block.
- **The outputs folder** is the config's, as on the compute service (decision 33), by default `<runs>/<id>/out`: a
  CWL runner's `--outdir`, and the folder a Nextflow pipeline publishes into, `-output-dir` (decision 29), on every
  target alike.

## 6. On #586: a backend describes, the machine acts (S1)

#586 made the target table the one place a target and its runners are found; each runner is a `Backend` with
`check`, `prepare`, `launch`, `outcome` and `realtime_command`. On #586 a backend both describes a run and acts on
this machine. A site needs the description without the action, so S1 splits them, with no change of behaviour
locally. S1 is built on #600's runner values (`sophios.runners`), which changed two things under it. A run's runner,
with its flags and its settings, is one value in the request's `RunSettings`, and names its own program: `name`
(`cwltool`, `toil-cwl-runner`, `nextflow`) and `program`, what this machine runs it as (`cwltool_filterlog` for
cwltool). And a CWL run's check left the protocol for `preflight.ready`. Below, "#586" is also what #600 left as #586
had it:

1. **`launch` is pure.** No `.resolve()` (on macOS it rewrites a site's `/tmp/...` to `/private/tmp/...`), no
   `Path.cwd()`, no `os.environ`, no clock: the caller gives absolute paths and an explicit output directory, and
   `run_local` merges this process's environment. A site runs `launch`'s argv with `argv[0]` set to
   `<bin>/<name>`, the runner's `name` (#600), while a local run keeps its `program`.
2. **`prepare` is `write` and then this machine's part.** `write` writes the run's files under a folder and returns
   what the runner's machine must still do before the run starts (today: make the output-target folders Nextflow
   stages, which `NextflowBackend._prepare` made on this machine). `prepare` stays what it is locally: `write`, then
   the checks, the pulls and the folders, here. A site calls `write` into a local staging folder.
3. **`needs` says what a run asks of its machine:** the paths its jobs name and its images; its program is its
   runner's. The pre-flight judges them on this machine, as on #586; a site judges them on the site, in one SSH call.
4. **`outcome` reads what it needs through `read(name) -> str | None`, relative to the run's folder, and writes
   nothing**; the caller writes the summary (`output_<name>.json`) that Toil's and Nextflow's outcomes wrote on #586.
   A site reads them over SSH, once, when the run has ended.

Reading relative paths against the config's `relative_to` needs nothing new: `targets.for_run` reads them against a
`Path` it never touches, so a site's folder is read as a name (the 2026-10-29 design's first PR is gone).

### 6.1 The protocol, as S1 builds it

```python
class Backend(Protocol):
    writes_summary: bool    # read-only: the runner writes output_<name>.json itself (cwltool, given --write-summary)

    def write(self, lowered, analyses, basepath, *, inputs_file, root_dir) -> run_setup.Prepared:
        """The run's files under basepath and nothing elsewhere; Prepared.folders, what its machine must still make."""
    def needs(self, documents, jobs) -> preflight.Needs:      # the jobs (their paths), the container requirements
    def prepare(self, lowered, analyses, basepath, settings) -> run_setup.Prepared:   # write, then this machine
    def launch(self, request) -> Launch:                      # from the request alone
    def outcome(self, request, exit_code, stdout, log, read) -> Outcome:              # writes nothing
    def realtime_command(self, request) -> realtime.Command | str:                    # unchanged
```

| | #586 | S1 |
|---|---|---|
| `launch` | `request.basepath.absolute().resolve()`; the default outdir from the clock; `Path.cwd()`; `{**os.environ, **request.env}` | the request's paths as given; `request.outdir` as given; `cwd` None for a CWL runner (wherever it is started), the pipeline's folder for Nextflow; `env` the run's variables alone (and `NXF_SYNTAX_PARSER=v2`) |
| who places a local run | each backend | `run_local.launch(request, backend)`: `basepath` absolute and resolved, `outdir` absolute and resolved or `<basepath>/outdir_<the runner's name up to its first '-'>_<name>_<time>` (`outdir_cwltool_`, `outdir_toil_`, as on #586), `cachedir` absolute, then `{**os.environ, **launch.env}` |
| `Launch` | `cwd: Path`, `env` this process's and the run's | `cwd: Path \| None`, `env` the run's alone |
| `prepare` | writes, checks, pulls, makes folders | `write`, then the check, the pulls (CWL), the entrypoint rewrite (CWL), and each of `write`'s folders that names nothing yet (Nextflow) |
| `run_setup.Prepared` | `name`, `plans`, `documents` | and `folders: tuple[str, ...]`, each output-target Directory Nextflow stages, sorted by input name; `()` for CWL, whose runners make them |
| `run_setup.write` | inside `_prepare` | its own function: the job, the documents and the analyses; `_prepare` calls it, and calls it again for `--docker_remove_entrypoints` |
| the check | CWL: `preflight.ready(documents, run, jobs)`, which reads its own needs; Nextflow: `check(documents, jobs, run)` | CWL: unchanged, and `needs(documents, jobs)` is the same reading; Nextflow: `check(needs(documents, jobs), run)`, its images read from the pipeline in `needs` |
| `preflight.Needs` | `documents`, `containers`, `jobs`; `_image_names(found)` | the same fields; `Needs.images` replaces `_image_names`; a Nextflow process's container is a `DockerRequirement` with that `dockerPull` |
| `outcome` | `outcome(request, exit_code, stdout, log: Path)`; Toil's and Nextflow's write the summary | `outcome(request, exit_code, stdout, log: str, read)`; `stdout` and `log` as text, `read` for the summary and `nextflow_<name>/nextflow_names.json`; it writes nothing |
| `Outcome` | `outputs`, `summary: Path \| None`, `failed` | `outputs`, `failed`; `RunHandle` writes `outputs` to `output_<name>.json` when the backend's runner does not (`writes_summary`) and there are outputs, and names the file when either holds, as on #586 |

Three choices this table makes, each the narrowest that keeps the four changes:

- **`needs`'s program is the runner's,** not a field of `Needs` nor of the backend. Locally the program a check looks
  for is the runner's `program` (`cwltool_filterlog` for cwltool); a site looks for `<bin>/<name>`. A `Needs.program`
  or a `Backend.program` would be a second record of a name #600's runner values already state.
- **`outcome` takes `stdout` and `log` as text and `read` for the rest.** The log's name is the caller's
  (`run_<name>.log` here, `run.log` in a site's run folder), so the caller reads it; `read` names only what the
  backend itself named: the summary it gave `--write-summary`, and its names file.
- **`writes_summary`.** On #586 cwltool's outcome named its summary however the run ended, and Toil's and
  Nextflow's only when they wrote one. With the write moved to the caller, the caller must know which runner wrote
  its own, or it would overwrite cwltool's (whose layout differs) or say nothing of it on a failed run.

### 6.2 Local runs are byte for byte what they are on #586

Measured outside the suite, as #586 measured its own change (§16): over the docs examples, the test cases and both
corpora, through the command line (`--generate_run_script`, `--run_local`, `--check`) and through the Python API
(`run()`, `run(generate_run_script=yes)`), with each runner, the clock pinned and the machine's answers stubbed, on
`865a3455` and on S1's tip: the argv each backend launches, the variables it adds, its working directory and output
directory; the `run.sh` written; every file written; what was printed; and every question the pre-flight was asked.

**Measured.** 189 workflows: the 158 `.wic` files of `docs/`, `examples/`, `tests/` and both corpora (38, 52, 68),
each with each runner through `--generate_run_script`, `--run_local` and `--check`, and the 31 Python workflows of
`docs/cookbook`, `examples/workflows` and image-workflows, each with each runner through `run()` and
`run(generate_run_script=yes)`: 1608 cases, 325 `run.sh` files and 325 launches, 7521 files, 1210 pre-flight
questions, and every case's stdout and stderr. The only difference is one file, `error_w.txt`, the traceback a crash
of `tests/core/data/run_paths/inline_body/w.wic` under `--check` writes on both sides (cwl-utils refuses its tool),
whose frames name the source tree and its line numbers. 668 cases exit non-zero and 40 raise on both sides
(fragments, refusal fixtures, Nextflow refusals), each compared as it ends.

**On #600's runner values.** S1 was rebuilt on #605, and again on #609 (`aef7ab98`), which gives Toil its own form of
an empty output directory and builds no-entrypoint images for the workflow's images only: `write` takes that form,
and the second `write` names those images. It was measured against #609, again outside the suite, in two parts. The
launch: 39,300 requests (each runner with its flags and settings; the base path, the output directory, the documents
and the environment, given or not) launch as one on both sides: argv, variables, working directory and output
directory. The command line: the 494 `.wic` files of `docs/`, `examples/` and `tests/`, each with each runner through
`--check` and `--generate_run_script`, and that again with `--outdir` and `--quiet`, the clock pinned and the
machine's answers stubbed: 4446 cases, 505 `run.sh` files, 2685 files, 1638 pre-flight questions, and every case's
stdout and stderr, with no difference. 3654 cases exit non-zero on both sides, each compared as it ends. The corpora,
`--run_local`, `--docker_remove_entrypoints` and the Python door were not measured again.

## 7. How a run goes

**Every call** is `ssh -T -o BatchMode=yes -o RemoteCommand=none -o ConnectTimeout=20 -o ServerAliveInterval=15 -o
ServerAliveCountMax=3 [-p <port>] [<user>@]<host> "bash -l -c '<guard>'"`. The script comes on stdin, and the guard
runs it only when its sha256 matches: a profile that reads its input would eat part of the script and run the rest
(measured). The guard prints the hash before it runs the script, so what a login profile prints is never read as the
script's (§17). A two-factor cluster works through a shared connection (`ControlMaster`) opened by hand. Tests
replace `ssh [<user>@]<host>` with a local `bash`, and nothing else.

**`check()`** reads the config (decision 33), then compiles and lowers here, through the target table, so a workflow
the target cannot run is refused with its `nf-s` code before anything reaches the site. It reads relative paths
against `relative_to`, by name, and refuses a path written for a shell (`~`, `$`), a relative path with no
`relative_to`, an output folder of the workflow given a path, and real-time analyses. One call then reports its
platform; every File and Directory the run reads, by the backend's `needs`; the runner, `<bin>/<name>`, and its
version against the range Sophios supports (§17); the container engine when a step runs in a container (`docker
info`, or `singularity --version`); whether `runs` is writable; and whether the outputs folder the config names is
missing in a folder that exists, or empty, and named by no run in `runs` that has not ended. On Slurm it first checks
the config's `SlurmSettings` as the compute client does (decision 39), then the same call asks `sbatch`, `squeue`,
`sacct` (it must answer), `scancel` and `sinfo -a` (each partition, its limit, its GRES), and reads your
`SBATCH_ACCOUNT`, `_PARTITION` and `_TIMELIMIT`; `SBATCH_CLUSTERS` and `SLURM_CLUSTERS` are refused there: no runner can
follow step jobs to another cluster. Where the run's jobs go is settled from those answers (decision 40).

**`submit()`** prints the id, the runner and its flags as given, how many steps run at once, on Slurm where the jobs
go, and the outputs folder, each with its source. One call sets `trap '' HUP PIPE` (a dropped connection cannot stop it between starting the
leader and the record), refuses an outputs folder that is no longer missing or empty, or is a link, makes `<run>`
exclusively and the outputs folder when it is missing, writes the files, and starts the leader: on a machine `set -m;
nohup bash -l <run>/job.sh >> <run>/run.log 2>&1 < /dev/null &`, bash's job control giving the leader a process
group of its own, whose id is its pid (§17); on Slurm `sbatch --parsable <run>/job.sh`, with the `SBATCH_`
variables of the options `job.sh` sets unset, since they beat `#SBATCH` lines, and it prints the job Slurm took. Run ids are
`<workflow>-<YYYYMMDD>T<HHMMSS>-<8 hex>`.

**`status()`** reads the folder and asks the scheduler only when the folder has not decided (Appendix B). Once the
run has ended, its outputs and failed steps are the backend's own `outcome` over the run's `stdout`, `run.log` and
summary, read once: a run there reads as it does here. **`wait()`** polls every 5 s for a minute, then every 60 s,
printing each change and what `run.log` gained; Ctrl-C prints the `attach` line and leaves the run going.
**`cancel()`** stops the run and claims it as `cancelled`: on a machine the claim first, then TERM to the job's process
group; on Slurm `scancel -u "$(id -un)" -n <id>` first, then the claim (§18). It waits about 2 minutes, and returns
whether it ended CANCELLED.

```
<runs>/<id>/
  run.json     the id, the site's kind (decision 43), the target, the runner and its flags as given, the settings it
               read (max_parallel, clean) and on Slurm placed (decision 40), each with its source, the outputs folder
               and its source, Sophios's version
  job.sh       the leader script (Appendix A); beside it, what the backend's `write` wrote
  leader       the claim: a link to the leader's pid (its job id on Slurm), or to `cancelled` or `none`
  run.log      the job script's and the runner's two streams
  stdout       the runner's standard output alone, which its `outcome` reads
  toil/        Toil's work/ and tmp/ (every step's outputs and scratch), removed as its --clean says
  batch/       on Slurm, Toil's step jobs' Slurm logs (`--batchLogsDir`); Toil there writes no provenance/
  made/        a Nextflow run's output folders given a name, made empty here and published by name
  out/         the outputs, unless the config names an outputs folder
  reason       why the run ended, when status() found its leader killed outright or stopped a run Slurm would
               never start, or submit() found Slurm refused its job twice
  exit         the job script's exit status, written last, after the cleanup
<runs>/.images the image cache under Singularity (decision 32)
```

## 8. One leader per run

On Slurm, SSH can drop after Slurm accepted the job, `sbatch` can fail or time out after Slurm
took it, and Slurm may list a job only later. So a run can get two leaders, and "Slurm has no job" is never proof.
**Sophios never deletes a run folder,** on any site. A machine starts its leader once, in the submit call, so it
adopts nothing; its claim takes `$$`, and `cancel()` and `status()` end a run before it starts as below.

- **Adopt, then submit again.** When the submit call fails or its answer is lost, `submit()` asks Slurm for a job
  named after the run where the compute service's driver asks, `squeue -n <id>`, then `sacct --name=<id>` since the
  submit (§18). If one exists, it adopts it. Otherwise it runs `sbatch` again on the same folder, which the claim
  makes safe. When Slurm refuses that too, the same call claims the run `none`, so no job of it Slurm took all the
  same ever runs it, and writes its `reason`: the run did not start, `submit()` says so, and its outputs folder is
  free. Only when the second call fails too does `submit()` say the run may have started, and how to attach to it;
  one that did not holds its outputs folder for the 10 minutes below.
- **The claim.** The leader is named after the run, and `#SBATCH --dependency=singleton` makes Slurm run one job of
  that name at a time. The first lines after `set -euo pipefail` claim the run with an `ln -s "$SLURM_JOB_ID"
  <run>/leader`, atomic on NFS too. A later leader exits 0 before it touches anything, so the folder keeps the first
  one's outcome. `--open-mode=append` keeps it from emptying `run.log`. On a machine the claim takes `$$`.
- **Ending a run before it starts.** Sophios takes the claim itself. `cancel()` links `cancelled`. `status()` links
  `none` when, 10 minutes after the submit, Slurm lists no leader of the run that may still claim it: none pending or
  running, none that ended less than 2 minutes ago. A leader the scheduler lists only later then exits at its claim,
  so an end, once seen, never changes.

## 9. Cleanup

**Toil's `--clean always` is the default: a run cleans up however it ends,** success, failure, cancel or time
limit. What is cleaned is the runner's scratch, and only Toil leaves any outside its outputs: Toil's `--clean always`
removes the job store, and once Toil has stopped the EXIT trap removes `toil/tmp` and `toil/work`, `chmod -R u+rwX`
first for a folder a tool left unwritable. Neither follows a link, so linked inputs stay intact. On a cancel (or, on
Slurm, the time limit) TERM reaches every process of the job; Toil stops its step jobs and exits, and only then, the
runner being in the foreground, does bash run its traps. Nothing is forwarded: a second TERM would cut Toil's shutdown
short. `onSuccess`, `onError` and `never` are Toil's names, given in the runner value (`Toil('--clean',
'onSuccess')`): the first keeps the job store and `toil/` after any end but a success, the second after a success.
Nextflow takes no `--clean`: its work folder, `nextflow_<name>/work` in the run, stays with the run, and the pipeline
publishes the outputs into the outputs folder (decision 29).

**A run that does not succeed leaves its outputs folder empty** (decision 33), as the compute service's does: the
folder was missing or empty when the run started, so everything in it is the run's, and the same EXIT trap empties
it, `chmod -R u+rwX` first, unless the run exited 0.

**Known leftovers.** A hard kill (SIGKILL, a node failure, the leader out of memory, an instance stopped) stops the
runner before its cleanup, and the trap too unless only the runner died: `<run>/toil` stays, and `status().reason`
says so, with the `rm` line. The `status()` that finds a hard kill empties the outputs folder and writes `reason`,
so every later status reads the same end; on Slurm it also cancels the step jobs still queued with the run's
comment, `sophios:<id>`, which Sites puts on every step job of every runner, under every `--clean`.

**Disk.** Nothing is node-local: every step's outputs and scratch stay in the run until it ends. Toil runs with
`--bypass-file-store --leave-tmpdir`: the bypass alone collapses a scattered step's same-named outputs into one file
(measured: "b b b" downstream, COMPLETED); `--leave-tmpdir` names them as the file store does (`out.txt`,
`out.txt_2`). Once a Toil release has DataBiosphere/toil PR #5663 (fixing #5662 and #5559), `--leave-tmpdir` goes.
**Permissions:** you run as yourself, so your umask applies.

## 10. Conventions, and the one override each

| What | Convention | Override |
|---|---|---|
| The runner | Toil for `cwl`, Nextflow for `nextflow` | `runner=`, any runner of the target (decision 35) |
| Outputs | `<runs>/<id>/out`, printed with the id, for every runner | `RunConfig(folder)`, missing or empty, as on the compute service (decision 33) |
| The leader | your `SBATCH_PARTITION`, else the default partition; your `SBATCH_TIMELIMIT`, else that partition's longest; no memory, under cwltool the largest step's; your `SBATCH_ACCOUNT` (decisions 40 to 42) | `SlurmSettings(partition=, time=, memory=)` |
| Steps (Toil) | each tool's resources; the run's partition, or for a GPU step the one partition with GPUs; the shorter of the run's time and the shortest limit of those partitions (decision 40) | `SlurmSettings(steps_partition=, steps_time=, gpu_partition=)` |
| Memory | a Toil step that declares none gets 4 GiB, `--no-cwl-default-ram --defaultMemory 4Gi` (Toil reads `4G` as 4·10⁹ bytes); `check()` notes each such step (`wic066`, §17); cwltool and Nextflow as they run locally | the tool's own `ramMin` |
| Parallel | at most 64: Toil's `--maxJobs`, Nextflow's `executor.queueSize`; cwltool as its flags say | `runner=`: `Toil('--maxJobs', '8')`, `Nextflow('-qs', '8')` |
| Toil's scratch | removed however the run ends | `runner=Toil('--clean', 'onSuccess')` |
| The runners | `<bin>/<name>`, `bin` first on PATH in every job | `bin=` (needed by `check` and `submit`) |
| What a module provides | your login profile there (decision 31) | - |
| The container engine | `docker` on a machine, `singularity` on Slurm | `container_engine=` |
| The image cache | `<runs>/.images` under Singularity (decision 32) | your `SINGULARITY_CACHEDIR`, `APPTAINER_CACHEDIR` for theirs |
| Any other `sbatch` option | `export SBATCH_QOS=...` and the like, in your login profile there | - (decision 23) |

## 11. Codes

A problem on a site is a problem of the machine a run needs, so it is a `wic0NN` code of kind `machine`, explained by
`sophios --explain` and listed in `docs/error_codes.md`, as #586 does for every code. Where the meaning is the one an
existing code has (a missing input file is `wic016` here and there), the site reuses that code, with a message naming
the site. Problems only a site has (it cannot be reached, a scheduler command is missing, a runner
`bin` lacks, a run id names no run, Nextflow's containers under another engine) get new `wic0NN` codes. No new range.

| Code | Meaning | PR |
|---|---|---|
| `wic062` | the site cannot be reached or used: ssh, a login profile that read the script, a call that failed there (an outputs folder filled between `check` and the start among them), a platform that is neither macOS nor Linux, or Sophios on Windows itself | S2 |
| `wic063` | `bin` lacks the runner, it does not run, or it is older than a site runs | S2 |
| `wic064` | a run id names no run under `runs`, or one the other kind of site submitted (decision 43) | S2, S3 |
| `wic065` | a Nextflow process with a container on a site whose engine is neither Docker nor Singularity | S2 |
| `wic066` | a note: a step Toil gives 4 GiB on a site, its tool declaring no memory (§10) | S2 |
| reused | `wic015` the engine; `wic016` an input, a path written for a shell, a relative one with no `relative_to`, or an output folder of the workflow given a path or in a list; `wic021` `runs`, or the outputs folder: not missing or empty, a link, its parent missing, used by a run that has not ended, inside a folder the run reads, or `runs`, inside it or around it; `api008` a real-time analysis (the call's, as `run_async`'s) | S2 |
| `wic067` | Slurm cannot run the run: one of its commands is missing or does not answer (`sacct` included); your login profile there sends jobs to another cluster (`SBATCH_CLUSTERS`, `SLURM_CLUSTERS`) or sets an `SBATCH_ACCOUNT` the run cannot take; a partition a job of the run goes to is not one `sinfo -a` lists; a step asks for a GPU and no one partition has GPUs; the run would have no time limit, or a partition or time Slurm or your `SBATCH_` settings give is in a form the setting would not take (decision 40); or the runner is Nextflow, which S4 runs on Slurm | S3 |

## 12. What Sites does not do

- **No Sophios on a site, no service, no daemon** (decision 16): nothing to install, version, run or upgrade there.
- **No search for a runner:** `bin` names it.
- **No `module load`** (decision 31): what a computer provides through a module comes from your login profile.
- **No shell code in any setting:** no `setup=`, no `sbatch=` lines, no `qos=` yet.
- **No copying of data, no fetch of outputs.** `rsync` exists, and the messages show it.
- **No resume** (decision 21). A failed run is submitted again from the start.
- **No sweep of what a hard kill leaves.** `<run>/toil` stays, and the reason names the `rm` line.
- **No real-time analyses on a site:** they watch cwltool's job folders here.
- **No output folder written in place** (decision 37): an output folder of the workflow given an absolute path is
  refused on a site, on either target.
- **No cloud plumbing:** a VM is a `Machine`; Sophios never creates, sizes or stops one.
- **No settings file, no site registry, no CLI in v1:** a site is one line of Python; `attach` is "come back
  tomorrow".
- **No run listing, no per-step logs or progress** (`ls`, `run.log`).
- **No PBS, LSF or Kubernetes, no Windows workstation, in v1.** Another scheduler would be one more column of the
  runner table.

## 13. Decided by the owner

1. **The goal:** develop and test on a workstation, then run the same workflow on a cluster or a cloud VM as
   yourself, changing about one line. The compute service is prior art only.
2. **`wf.run()` and `collect()` are local only.** The site submits: `Slurm(...).submit(wf)`, `.attach(id)`.
3. **The user is responsible for where the inputs are**, and every doc opens with it.
4. **The login and compute nodes share a filesystem** on the owner's clusters.
5. **Cleanup is the default** (`clean=`); leftover step jobs are cancelled under every `clean`. *Since 27 it is
   Toil's `--clean`, in the runner value.*
6. **No setting ever takes shell code:** `setup=` would teach scientists remote code execution through strings.
7. **Words:** "site" in the docs, the sites `Slurm` and `Machine`, the verbs `submit` and `attach`; a site is never
   called a backend (a runner's `Backend` is #586's).
8. **`sacct` is required.**
9. **Images are pulled in the leader's job** (a Nextflow task pulls its own, as the Nextflow target does).
10. **No node-local scratch.**
11. **4 GiB for a Toil step that declares no memory**; its own `ramMin` or `ramMax` wins; the warning stays.
12. **Site settings are Python only in v1.**
13. **`max_parallel` is 64.** *Since 27 the runner value says another: `--maxJobs`, or Nextflow's `-qs`.*
14. **No `sophios_dir=`, no `retries=`**; `gpu_partition=` is an override only. *Replaced by 28 and 30:
    `gpu_partition` is a field of `SlurmSettings`, and the folder a site keeps its runs in is `runs=`.*
15. **2026-10-29:** cleanup however a run ends; one leader per run; `--leave-tmpdir` until Toil's fix ships; your
    own umask.
16. **2026-11-09: Sophios stays a local install.** Nothing of Sophios goes on a site. The user names the runners'
    folder (`bin`, required); Sophios manages everything else on the user's behalf, with the user's own `ssh`.
17. **2026-11-09: every runner of #586's target table runs on every site, or is refused there with a code**; a test
    over `runners.RUNNERS` and the schedulers enforces it. This replaces "Toil only" (2026-10-20) and "no Nextflow
    on Slurm in v1" (2026-10-29). *Since 26 it is every target, and the test is over `targets.TARGETS`.*
18. **2026-11-09: the Nextflow design's non-goal gets one exception, for sites,** worded so it can be removed
    whole: a site sets the executor options of its own scheduler, through the run's ordinary passthrough
    arguments, from one function in `sophios.sites`. The backend never learns of sites, so lifting the non-goal
    later deletes a sentence and changes no code. *Its channel is replaced by 25; the user's own flags are passed on
    since 34.*
19. **2026-11-09: Nextflow's containerized processes stay Docker-only on a site**, the restriction of the pipeline
    `compile()` writes; Apptainer would be a change to that pipeline. *Replaced by 29 and 36: the pipeline is written
    for the site's engine, Docker or Singularity.*
20. **2026-11-09: the default runners:** a machine runs the target's default (cwltool for `cwl`, as locally); Slurm
    runs `toil-cwl-runner` for `cwl`. *Replaced by 22.*
21. **2026-11-09: no resume until Sites has met real use.** (It was open question 1; the 2026-10-29 design's "Later:
    resume" verified that Toil 9.5's `--restart` works with this design's flags, should it come back.)
22. **2026-11-11: no site runs cwltool; `toil-cwl-runner` is every site's default for `cwl`.** Toil runs on a
    machine (`single_machine`) and on Slurm (each step a job) alike, so a site has one CWL runner: no leader sized
    to the largest step, no one step at a time, no GPU step refused for a runner that asks Slurm for none.
    `runner='cwltool'` is refused there, `wic063`, so decision 17 holds. Toil must be in `bin` (it installs cwltool
    with it), and on a machine Toil holds each step to the machine's memory, so a step that declares none needs
    4 GiB there (the `wic066` note). *Replaced by 35: any runner of the target runs on a site, cwltool too; Toil
    stays the default for `cwl`.*
23. **2026-11-10: no QOS of Sites' own.** A site sets no QOS, as compute's request has none; one your login profile
    (`SBATCH_QOS`) or the cluster sets applies to every job. Where a QOS allows less time than the partition, the
    first `status()` fails the run, naming `time=`. *Since 28 that is `SlurmSettings(time=)`.*
24. **2026-11-10: every step job gets one time,** `time=`, else the shortest limit of the partitions the run uses,
    under Toil and Nextflow alike: Toil 9.5 gives every step job one `--time`, so a CPU step longer than a shorter
    GPU limit needs `time=`. *Since 28 the steps' time is `SlurmSettings(steps_time=)`, else the shorter of the run's
    time and that shortest limit (40).*
25. **2026-11-11: what a site sets for Nextflow is a block it appends to the run's copy of the generated
    `nextflow.config`,** not passthrough arguments: Nextflow is set by its configuration, and only a configuration sets
    a task's options by the task, so a GPU task under Nextflow on Slurm goes to the GPU partition with `--gres`, as
    Toil's step does. Measured with the pinned Nextflow: a closure given as `-process.queue` reaches the task as its
    text, a `withName:` selector on the command line is ignored, and the backend's `-C` ignores a second file given to
    `run -c`. The backend still never learns of sites, and lifting the non-goal still deletes a sentence and changes no
    code of the backend. *Since 34 a setting of the block given as a flag, `-executor.queueSize`, is refused.*
26. **2026-11-17: a site takes no `runner=`.** Since 22 a site runs each target with one runner, so the runner
    follows from `target=`. Locally #600 made the runner a value, `run(runner=Toil(...))`; a site taking that value
    would take settings that are this machine's (where images are pulled, the cache, the provenance) and flags that
    compete with the ones the site sets. So `check` and `submit` take `target=` alone, nothing is refused for its
    runner, `wic063` is only a runner that `bin` lacks, that does not run there or that is too old, and 17's test
    walks the targets. *Replaced by 27.*
27. **2026-11-24: a site takes the runner value, and from it only the flags a site knows.** `check` and `submit` take
    `target=` and `runner=` as `Workflow.run` does, and `max_parallel=` and `clean=` are gone: a keyword of a site's
    own is the wrong shape for a setting the runner value already carries. The value is the one runner a site runs
    the target with (22), or it is refused. One table, by runner, names each flag a site takes and the setting it
    gives, `--maxJobs` and `--clean` from Toil, `-qs` from Nextflow; the site reads each value, a count or one of
    `--clean`'s names, and gives it to the runner where the runner reads it: Toil's command line, Nextflow's
    configuration (25). Any other flag is refused, and so is a value with a setting of its own changed (the engine is
    the site's `container_engine=`, the rest act on this machine). Every refusal is a `ValueError`, before anything
    is compiled. A run's record keeps `max_parallel` and `clean`, each with the flag that gave it. *Replaced by 34,
    35 and 36: the other flags are passed on, any runner of the target runs.*
28. **2026-11-24: one `RunConfig` for every place a run runs.** A local run (`Workflow.run(config=)`, `start`,
    `run_async`, `check`), a site and the compute service take the same class, the same object, from
    `sophios.runtime.runners`:
    `RunConfig(folder).relative_to(path).slurm_settings(SlurmSettings(...)).log_level(level)`, three setters, each
    replacing what an earlier call of it set, and no public reader. One class, because the three places answer the
    same two questions, where the outputs go and how the jobs run, and a config written for one place is written for
    the others.
    - **`SlurmSettings`** is six optional strings in Slurm's own forms, given by keyword and checked where they are
      used: `partition`, `time` and `memory` of the leader, the job that lasts the whole run; `steps_partition` and
      `steps_time` of each step's own job (Toil, Nextflow); `gpu_partition`, where a step that asks for a GPU goes. A
      field about a job the run does not have is not used, never refused: a local run and a `Machine` have no Slurm
      job, and cwltool no step jobs. `log_level` is Toil's `--logLevel` wherever Toil runs; in a local run it comes
      before the flags of `Toil(...)`, so a level given there wins. cwltool and Nextflow do not use it.
    - **The pair rules** live in one function, `runners.read_config`, which every door reads a config with and which
      returns what it says and its problems in the user's words; each door reports them its own way, a `ValueError`
      locally and on a site, `RequestRefused` lines from the compute client. `relative_to` needs an output folder;
      `relative_to` is an absolute path; an absolute folder takes no `relative_to`; a relative folder needs
      `relative_to`, but in a local run, which reads it from the working directory, as before. Each rule is the
      service's own: its schema refuses a `basepath` without `workflowOutDir` and one not starting with `/`, and its
      path checks refuse the other two, reading a `file:` URL as absolute, as `read_config` does for every door but a
      local one, so the client never refuses a request the service takes.
    - **What is sent to the service does not change.** `relative_to` is the request's `basepath`, the folder its
      `workflowOutDir`, `log_level` its `logLevel`, and the six settings its `leader` and `steps` through one six-row
      table (`compute.SLURM_FIELDS`), each object sent only when one of its fields is given. The schema, the driver
      and the requests' goldens are not edited. Sophios's own refusals from the schema name what the user wrote,
      `SlurmSettings(time=...)`, through the same table read backwards; the service's keep its words.
    - **Locally**, the output folder is the config's: relative, read from `relative_to`, else from the working
      directory; absolute, as it is; none, `<basepath>/outdir_<runner>_<name>_<time>`. It may exist and hold files:
      missing or empty is a site's and the service's rule. `relative_to` is also where a relative File or Directory
      bound in Python is read from; a `StepInput(default=)` path is made absolute while compiling, where no config
      exists, and stays on the working directory. The Python API resolves the config once into `RunSettings.outdir`,
      the one folder the command line's `--outdir` also fills; the command line gains no flag. `outdir=` and `quiet=`
      leave `Workflow.run` and its siblings, which no release had; `quiet` is cwltool's, `Cwltool(quiet=)`, as
      `cachedir` is; the deprecated `run_args_dict` keeps both keys, its `outdir` winning over the config's folder.
29. **2026-11-24: a Nextflow pipeline publishes its outputs wherever it runs, and is written for its runner's
    engine.** One pipeline text serves a local run, a site and the compute service, so there is one text to read
    back and to prove, and an output folder means the same on every target (`design_docs/sophios_nextflow_backend.md`
    §4.5).
    - **Publishing in every form.** Once every output has completed, the pipeline copies each into the output folder,
      placed as a CWL run places them. A local run gives Nextflow `-output-dir` with its output folder, the config's
      or `--outdir`, else `<basepath>/outdir_nextflow_<name>_<time>`, the default every runner has, and its result
      names that folder; so an output folder is taken for the Nextflow target from Python and from the command line,
      which accepts `--outdir` and `--container_engine` with `--target nextflow`, its one change. The files also
      stay in the work folder. The pre-flight checks the folder can be written, as for CWL. A local folder may exist
      and hold files: each copy replaces what the folder holds under its name, a folder and all in it, and leaves
      the rest, so a run again into the folder, or with `-resume`, which copies every output again,
      publishes as the first did; nothing more is refused. Each path a local run reports keeps its own name as its
      `basename`, as cwltool reports a renamed copy's; its path names the copy.
    - **The engine is the runner's:** `Nextflow(container_engine='docker')`, or `'singularity'`. The pipeline and its
      `nextflow.config` are written for it: under Singularity the container directive, `--nv` for a GPU task, the
      command after the image's runscript and each task's `HOME` its task directory, the form the compute service
      runs; another engine is a `ValueError` where the pipeline is written, and on the command line a usage error,
      each naming the two. So a local run can use
      Singularity, and a site's engine reaches the pipeline through the runner value it builds.
    - **The record** of the outputs, the `sophios-output` lines a local run and a site read, names the published
      paths, each with its own name, and is printed once the run has finished, not one output at a time; the form
      that prints it is the one that replaces what the output folder holds under a copy's name. The compute client
      asks for the Singularity form without it, since the service reads Nextflow's own outputs object into a folder
      that is empty: that text, the request and the service's golden file are unchanged.
    - **Reading back.** The reader reads every form the renderer writes, the engine from `nextflow.config` and the
      record from the entry workflow, so a run's check reads a Singularity pipeline as it reads a Docker one. A
      pipeline with outputs written by an earlier release is compiled again; no second form is kept for it.
    - **Measured** on the pinned Nextflow: a path the pipeline has copied into the output folder needs no
      `workflow.output.mode`; without `-output-dir` Nextflow publishes into `results/` in the folder it starts from;
      the folder holds copies, never links. Nextflow's own copy into a folder that holds an output's name replaces a
      file, puts a folder inside the folder of its name, and on a third run fails after every task has run; with
      the replacing form, three runs into one folder, the third with `-resume`, each publish a file and a folder at
      the same paths, keep the folder's other files, and replace a link without touching what it names.
30. **2026-11-24: a site's constructor says only what the computer is:** `Machine(url, *, runs, bin=None,
    container_engine='docker')`, and on Slurm `Slurm(url, *, runs, bin=None, container_engine='singularity')`.
    - **`url`** is `ssh://[user@]host[:port]` and nothing else, no path and no query, the one way to name the
      computer, as the compute service is named: a `ValueError` otherwise. ssh is given `[user@]host`, and
      `-p <port>` with a port, so a `Host` of `~/.ssh/config` is `ssh://<Host>`. A user and a host start with a
      letter, a digit or `_`, so neither reads to ssh as an option. Messages name the host.
    - **`runs`** is required, an absolute folder there: a run lives in `<runs>/<id>/`, its default outputs folder
      `<runs>/<id>/out`. It replaces `root=` and its `<root>/sophios/runs`: one folder, named for what it holds,
      and no folder of Sophios's own beside the user's.
    - **`bin`** is the folder that holds the runners. `check` and `submit` need it, a `ValueError` naming `bin=`
      without it; `attach` does not, so a run is followed from a computer that knows only the URL and `runs`.
    - **`runs` and `bin` are letters, digits and `/ . _ - +` only,** as the service's driver takes its own folders,
      a `ValueError` when the site is made: every runner must run in them, and Toil's single-machine batch system
      runs each step's command through a shell, its job store, in `runs`, on that command, so a quote, a blank or a
      `$(...)` there breaks every Toil run or runs as shell; `bin` is a folder of `PATH`, which `:` splits.
    - **No `account`, `partition`, `gpu_partition` or `time` argument:** they are a run's, not the computer's, and are
      the config's `SlurmSettings` (28); the account is your `SBATCH_ACCOUNT`. A `Machine` asks nothing about Slurm.
31. **2026-11-24: no `module load` line.** `modules=` goes: what a computer provides through an environment module
    comes from your login profile there, which every call and every job reads (`bash -l`), as it does in a terminal.
    One way, the one you already use, instead of a second list of module names that must agree with the profile.
32. **2026-11-24: the image cache is `<runs>/.images`,** once `<root>/sophios/images`: the CWL runners'
    `CWL_SINGULARITY_CACHE`, with `SINGULARITY_CACHEDIR` and `APPTAINER_CACHEDIR` defaulting to
    `<runs>/.images/cache`, and a Nextflow run's `NXF_SINGULARITY_CACHEDIR`, `<runs>/.images/nextflow`, each only
    under Singularity. A run id starts as a workflow's name, never with a dot, so the cache is never a run's folder,
    nor read as one.
33. **2026-11-24: a site reads the config as the compute service does, and its outputs folder behaves as the
    service's** (`docs/compute.md`), so a config written for one is written for the other.
    - **The pair rules** are `read_config`'s (28), read as where there is no working directory. A site also refuses
      itself what that reading leaves to the service, which refuses it in its own words, since a site has no service
      behind it: a folder or `relative_to` that is not a string or is written for a shell (`~` first, a
      `$VARIABLE`), an empty folder, a folder that is a URL, `file:` too, and a folder the driver refuses as cwltool
      would read it otherwise than as the path it spells (a control character, a blank first, `?` or `#`, a
      `%`-escape). Its lines live in one function of the package, `_site.site_config`, which the pairs table reaches
      through the site's door. The folder is read as the service's driver reads it, `..`, `.` and a doubled or
      trailing `/` resolved by name.
    - **The outputs folder**: none, `<runs>/<id>/out`; relative, `<relative_to>/<folder>`; absolute, itself. It is
      missing in a folder that exists, or empty, no link, named by no run in `runs` that has not ended, that folder,
      one inside it or one around it, inside no Directory the run reads, and neither `runs`, inside it nor around it
      (the service refuses a folder among its runs: a failed run's cleanup would empty every run's folder and the
      image cache), all `wic021` at `check`; it is missing or empty again, and no link, when the run starts
      (`wic062`), and made then, one level.
    - **The start takes the folder against every run in `runs`:** it writes the run's record, with `out` a link to
      the folder, and only then looks for a run there that has not ended and uses the folder, one inside it or one
      around it (`_USED`); finding one, the run is claimed `none`, its `reason` written, and `submit` raises
      `wic021` naming it. Two runs that start at once each write before they look, so at least one finds the other,
      and no run's EXIT trap empties a folder another run there has written into. Runs under another `runs` do not
      see each other, which `docs/sites.md` says.
    - **A run that fails or is cancelled leaves it empty,** as the service's: the folder held nothing when the run
      started, so the job's EXIT trap empties it unless the run exited 0, and so does the status that finds a leader
      killed outright. That run has not ended until such a status writes its `reason`: until then its folder is its
      own, and `check` and the start refuse another run there, so no status empties a folder another run has written
      into since, and a status that read the leader gone empties nothing once a `reason` is there.
    - **A relative File or Directory input is read from `relative_to`,** by name, as the runner reads it; with no
      `relative_to` it is `wic016`, one line per input, whose remedy names `relative_to`. A path written for a shell
      is `wic016`.
    - **An output folder of the workflow** (a Directory a tool writes into and returns) given one name is made empty
      in the run, where no file of the run's is named so (a CWL runner makes it itself; for Nextflow it is
      `<run>/made/<name>`), and its filled copy lands in the outputs folder under that name; a name with a `/` is
      refused, `wic016`, as on the service. Output folders given in a list, as a scattered step takes them, are
      `wic016`, each element judged as one folder is, since a local run makes none of them either.
    - **`SlurmSettings` are not used on a `Machine`** (28); `log_level` is Toil's `--logLevel`, before the runner
      value's flags, so a `--logLevel` there wins, as in a local run, and is never refused.
34. **2026-11-24: a runner value's flags are passed on, but those the site sets itself.** A site runs the runner a
    local run would run, so its flags mean there what they mean here. They are passed on to the program in order,
    after the site's own. A site reads three itself, in both spellings (`--flag value`, `--flag=value`), since it
    needs their values in the run's record and in the leader's EXIT trap: Toil's `--maxJobs` and `--clean` and
    Nextflow's `-qs`, 64 and `always` when not given. A flag whose option the site or the runner's backend already
    sets is refused, a `ValueError` naming it. The set refused is derived, never written by hand: the options of the
    command line the site runs with none of the user's flags (its engine, never quiet, without the `--disable-pull`
    cwltool's backend gives it), and each setting of Nextflow's configuration block as `-<scope>.<name>`, but those
    the site reads; an option the backend or the block starts to set is refused at once. Another name the runner
    gives one of those options is that option, refused or read (`_runners._OTHER_NAMES`: Toil's `--jobstore`,
    cwltool's `-w`, Nextflow's `-o`, `-w` and `-queue-size`), as passed on each was measured to break the run or
    its record: Nextflow refuses an option given twice ("Can only specify option -o once"), so a run `check` took
    failed at its start; `-queue-size 5` ran 5 tasks at once where the record said 64; and `--jobstore` made Toil's
    job store outside the run's folder. A test compares the table with Toil's and cwltool's parsers and with
    `nextflow run -h`. A flag that undoes one the site sets (Toil's `--cwl-default-ram`, cwltool's
    `--leave-outputs`) and an abbreviation cwltool takes for an option (`--outd`; Toil and Nextflow take none) are
    passed on, as a local run passes them, and the runner reads the last.
35. **2026-11-24: any runner of the target runs on a site, cwltool too.** `runner=None` is Toil for `cwl`, which runs
    the same CWL on a machine and on Slurm, and Nextflow for `nextflow`. cwltool runs from `<bin>/cwltool`, not
    Sophios's own `cwltool_filterlog`, which is not there; without `--disable-pull`, since nothing pulled the images
    first, so cwltool pulls them, into the image cache under Singularity; and never quiet. Its oldest version is the
    one Sophios requires (`pyproject.toml`). On Slurm it is one job, sized to the largest step, on the GPU
    partition when a step asks for a GPU (42).
36. **2026-11-24: a runner value's own settings stay their defaults on a site.** The engine is the site's
    `container_engine=`, and the site evolves the value to it, so it reaches the backend as a local run's engine
    does: the CWL runners' flags, and the Nextflow pipeline, written for Docker or Singularity (29), with its record;
    any other engine is `wic065` for a process with a container. A value that sets an engine, `pull_dir`,
    `ignore_install`, `ignore_processes`, `provenance`, `remove_entrypoints` or `cachedir` is a `ValueError` saying
    what decides it there (the site's `container_engine=`, or that a site pulls, checks and caches by itself).
    `Cwltool(quiet=...)` is not used and never refused: a site's run is never quiet, as its log is where a failed
    step is read from.
37. **2026-11-24: where a site differs from the compute service for the same call,** each because a site runs the
    workflow as a local run does, as you, with no service: an output folder of the workflow given an absolute path is
    refused on either target, with the remedy "give it a name" (the service writes one in place on CWL); one given a
    name with a final `/`, or in a list, is refused, as a local run refuses it (the service takes it); an outputs
    folder written as a `file:` URL is refused (the service takes it); a path is read by name, never resolved through
    a link, so an outputs folder that is a link is refused and one inside an input only through a link is not seen
    as inside it (the service reads real paths); under Nextflow an output Nextflow would rename, or one the workflow
    does not declare, is taken and reported under its own name, as a local run reports it (the service refuses it,
    `wic061`); `SlurmSettings` are not used on a `Machine`; containers run under the site's engine, not always
    Singularity; a run is yours, written with your umask, and no run's folder is deleted; an input path is refused
    only when it is missing, unreadable, written for a shell or relative with no `relative_to`, and is written for
    the runner as a local run writes it; a path outside the service's shared folders is read; Toil reads your
    `~/.toil/default.yaml` (the service refuses to start while its account's sets an option); `status().steps` lists
    only the failed steps the runner's outcome names, once the run has ended (the service's lists each step's job,
    while it runs too); Nextflow reads your `NXF_` and `JAVA_` variables and is not offline; a cancelled run's
    `reason` is `cancelled` (the service's, `cancelled by request`); and runs into one folder see each other only
    under one `runs` (33). Two ways a site differs from a local run: a cancelled run's `reason` (the local one's,
    `cancelled: the runner was stopped`), and a `StepInput(default=...)` path names a path of this computer (§17,
    16). `docs/sites.md` lists each of these. *Decisions 39 to 42 add
    Slurm's: under cwltool the steps' settings are not used; no time is checked against a limit, where the service
    refuses one above its partition's or its account's; the defaults (your `SBATCH_` settings before the cluster's, the
    steps' time with no account limit and the GPU partition only for a GPU step); a GPU step with no `gpu_partition`
    goes to the one partition with GPUs, which the service refuses; a `gpu_partition` the cluster lacks is checked only
    for a GPU step; the leader under Toil asks for no cores and, but for `memory`, no memory (the service's, 2 cores and
    16G); a Toil step that declares no memory gets 4 GiB (the service's, 8 GiB), at most 64 step jobs at once; the jobs
    go to your account with your other `SBATCH_` settings; a steps' time of 0 days, `0-12`, reaches Toil as Slurm
    reads it, where the service's reaches it as Toil 9.5 misreads it, 12 minutes; a partition or time Slurm or your
    `SBATCH_` settings give is held to the setting's forms, where the service takes the cluster's as it is; a site
    reads `sinfo -a`, so a hidden partition is one, where the service reads `sinfo` and refuses it; every job's
    comment is `sophios:<id>` (the service's, `slurm-driver:<id>`); a site leaves out no node (the service's driver
    may, `--exclude`); and Nextflow is refused on a Slurm site until S4.*
38. **2026-11-25: the Slurm settings are the config's, never the constructor's.** A `Slurm` site reads the six
    `SlurmSettings` of the `RunConfig` (28), the same object the compute service takes, so a config written for the
    service places a run on a Slurm site, and the constructor says only what the computer is (30). They are read at
    `check` and `submit`, as the config is; nothing about a run is kept on the site object. A `Machine` reads none and
    asks nothing about Slurm: its check runs no Slurm command and reads no `SBATCH_` setting, and its job holds no
    `#SBATCH` line. A setting about a job the run does not have is neither used nor refused: under cwltool, whose
    run has no step jobs, `steps_partition` and `steps_time`. `gpu_partition` is used, and checked against the
    cluster, only when a step asks for a GPU.
39. **2026-11-25: one authority for the settings' forms, the compute service's.** A Slurm site and the service run
    the same kind of jobs with the same six settings, so one `SlurmSettings` is taken or refused alike by both:
    `sophios.compute.slurm_problems(settings)`, beside the vendored request schema and the six-row table, reads each
    form from the schema through that table, adds the one refusal the service's driver makes of the same fields as it
    takes the run, a time of 0 (Slurm reads it as no limit), and words each line as the compute client words it,
    naming what the user wrote. The request builder uses it for its own refusal, which now refuses a time of 0 before
    sending, as the service would; nothing else of the request changes, and its goldens and the client's other lines
    stay byte for byte. A Slurm site raises its lines as one `ValueError`, before anything is compiled or sent, for
    each setting the run uses. A property over generated settings holds that a site and the client refuse the same
    settings, with the same lines. So a time of 0 days, `0-12`, is taken, as the service takes it; a site gives Toil
    the steps' time as hours, minutes and seconds, which Toil 9.5 reads as Slurm does (§18).
40. **2026-11-25: each job goes where its setting says, else by the convention, and the record says which.** The run's
    partition: the setting, else your `SBATCH_PARTITION`, else the cluster's default. Its time: the setting, else your
    `SBATCH_TIMELIMIT`, else its partition's longest; a run with no limit is refused, `wic067`, asking for `time=`, as
    the service's driver refuses a leader with none, so a site and the service take the same configs here, and the
    steps' time below is never none either. Your `SBATCH_TIMELIMIT` is read only when the setting is not given, as
    your `SBATCH_PARTITION` is. Each value Slurm or your profile gives, a partition's name or a time, reaches a job's
    `#SBATCH` lines or Toil's flags as a setting's would, so it is held to the setting's forms (`slurm_problems`),
    `wic067` otherwise, naming the setting to give; a partition is given to Toil as `--slurmPartition=<name>`, one
    word, which a name that starts with `-` cannot break. Its memory: the setting, as the
    leader's `--mem`, else no line (under cwltool, decision 42). Under Toil, the steps' partition: the setting, else
    the run's; a GPU step's: the setting, else the one partition `sinfo -a` lists with GPUs; the steps' time: the
    setting, else the shorter of the run's time and the shortest limit of the partitions the steps go to, so a step
    never asks for more than its partition allows, nor for longer than the run. Every partition a job goes to must be
    one `sinfo -a` lists, `wic067` at `check`, naming the setting to give; the steps' partitions are checked only for
    a runner with step jobs, the GPU partition only for a GPU step. No time is checked against a limit at `check`:
    Slurm keeps such a job waiting, and `status()` stops the run, naming the setting (Appendix B). `submit` prints, and
    `run.json` records, each setting in force with where it came from, and what the leader's header and Toil's flags
    hold is what the record says.
41. **2026-11-25: the account is your `SBATCH_ACCOUNT`, read from your login profile there.** No `account=` argument
    and no setting: the account is a person's, not a run's, and Slurm's default applies without one. `check` reads it
    with the profile every job reads, and the site writes it into the leader's `#SBATCH` lines and into each step job's
    options (Toil's `--slurmArgs`, which Toil splits as a shell does), so it must be letters, digits and `_ . / + -`
    only (`wic067` otherwise), as an `SBATCH_TIMELIMIT` must be a time the config would take. A `Slurm` site's
    `runs`, which each header holds (`--output`, `--chdir`), needs no rule of its own: every site's is letters, digits
    and `/ . _ - +` only (30), and in an `#SBATCH` line none of these ends a word, is read as a quote or `\`, starts a
    comment (`#`) or a pattern (`%`). Every other `SBATCH_` setting of yours stays yours (23), but those of the options
    the header sets, which `sbatch` would read before its lines: the site unsets them for `sbatch`.
42. **2026-11-25: cwltool on Slurm is one job.** cwltool runs every step inside its own process, so its run is the
    leader alone, sized to the largest step as the steps state it in numbers: `--cpus-per-task` the most cores
    (`coresMin`, else `coresMax`, rounded up, at least 1); `--mem` the `memory` setting, else the most `ramMin` (else
    `ramMax`), else no line; and when a step asks for a GPU (`cwltool:CUDARequirement`), `--gres=gpu:<n>`, the most
    GPUs a step asks for, on the GPU partition (40), whose longest limit is then the time when none is given. A
    requirement written as an expression is not read: a run that needs it gives `memory`, or a step its numbers. Each
    requirement is the one that wins at the step (§17, 6). `steps_partition` and `steps_time` are not used (38).
43. **2026-11-25: `run.json` records the site's kind, and `attach` refuses a run another kind submitted.** A run's
    folder is its record, and a `Machine` and a `Slurm` read a run's state differently (its claim is a pid or a job
    id), so `attach` of a run the other kind submitted is `wic064`, its message naming the kind to attach with, the
    same URL and `runs`; a `Slurm` with no `bin` attaches, as a `Machine` does.

## 14. Open for the owner

Nothing is open.

## 15. Build order

#586 merges first (#585 under it); then one PR at a time, each stacked on the one before, each with its design, its
code and tests, and its docs (two or three commits):

1. **S1. A backend describes, the machine acts:** the four changes of §6; local runs byte for byte as on #586. It
   adds this document. No user-visible change, so no docs commit.
2. **S2. `Machine`:** the package `sophios.sites` (`_ssh`, `_states`, `_runners`, `_site` with the base class and its
   scheduler hooks, `_run`), the transport, the run folder, `job.sh`, the claim, the states, `check`, `submit`,
   `attach` and `Run`, every target of the table with each of its runners (Toil `single_machine`, cwltool, Nextflow's
   local executor), the config read as the compute service reads it, the codes, and `docs/sites.md`. An EC2 instance
   works from here on.
3. **S3. `Slurm` with the CWL runners** (built): a class beside `Machine`, in a module of its own, `_slurm`,
   `sbatch`, the claim and adoption, the
   `SlurmSettings` and their defaults, the steps' time, Toil's Slurm flags, cwltool as one job, a leader or step job
   that would never start, leftover step jobs; `run.json` records the site's kind, and `attach` refuses a run another
   kind of site submitted.
4. **S4. Nextflow on Slurm:** Slurm's settings in the block the exception S2 makes already allows (decision 25),
   each GPU task on the partition with GPUs, the pipeline written for Singularity, Slurm's default engine; S2's
   refusal of containers under another engine applies on Slurm as on any site.

## 16. Tests

The owner's hierarchy (property, then fixture, then a table, then a single case), within the owner's test budget:

- **The states** (Appendix B) as a function of generated folder and scheduler facts: an end, once seen, never
  changes; a failed `squeue` or `sacct` decides nothing; the folder decides before the scheduler.
- **Quoting:** every value that enters a script is printed back by the real `bash`, over generated strings.
- **The argv:** Toil's read back by Toil's own parser; Nextflow's settings by the pinned Nextflow, from the run's
  configuration.
- **The table:** every target of `targets.TARGETS` runs on both schedulers.
- **End to end:** the scripts with `bash` for `ssh`, a fake Slurm of Sophios's own (the compute driver's is in a
  private repository Sophios CI may not check out), and the real runners, each in the lane that already installs it.
- **S1:** byte for byte over the corpora, as #586 measured its own change (§6.2), outside the suite. In the suite, all
  in the package job, which leaves out the tests marked `nextflow`: in `tests/core/test_backends.py`, each CWL
  runner's argv, read by its own parser, as `run_local.launch` places a request (the property #586
  has); the backend of every runner launches from the request alone (a relative base path
  through a link, neither made absolute nor followed, the working directory and the environment changed between two
  launches); each `outcome` reads through `read` and writes into no folder; `write`'s folders, and `prepare` making them
  only for a run; each CWL runner's `write` leaving the files its `prepare` leaves, the job's empty directory in the
  runner's own form. In `tests/core/data/runner-transcripts`, the summary cwltool leaves is kept byte for byte, and which
  runs name it. In `tests/core/test_python_api_workflow.py`, `--docker_remove_entrypoints` rewrites a real-time
  analysis's images too, through the second `write`.
- **S2,** in `tests/core/test_sites_unit.py` (what needs no site) and `tests/core/test_sites.py` (the walks), with a
  local `bash` for `ssh [user@]host`: in the package job, the states over every life a run's folder can have on a
  machine (exhaustively: each claim, each exit status, with and without a hard kill, and a `reason` written over each);
  every value a script carries printed back by the real `bash` through the guard, over generated strings; a login
  profile that prints, and one that reads its input; the URL and what ssh is given; Toil's argv, as `job.sh` writes it,
  read back by Toil's own parser over generated steps at once, `--clean`, engine and outputs folder; the leader's EXIT
  trap run by `bash` for each `--clean` and exit status, emptying the outputs folder; the options a site refuses against
  the command line it builds; how a runner value's flags are read or passed on; the outputs folder read as the service's
  driver reads it; `check` finding every problem at once and writing nothing; the settings, config and runner values
  refused; whether a step declares memory, as `check` reads it for `wic066`, against cwltool's own lookup, which Toil
  asks; every value a user gives, holding spaces, quotes, a command substitution and a newline, reaching the runner as
  one word; and, end to end, a run that fails, one cancelled while it runs, one killed outright, and one whose job, held
  in its login profile, finds the run claimed `cancelled` by `cancel` or `none` by `status`, each leaving its outputs
  folder empty, a second submit into it refused while it runs, and once killed until its status, and taken after; a late
  status of the killed run deletes nothing the next run wrote. The same workflow run on a machine and here, by each
  runner, returns the same outputs, value for value and place for place. The rows that need Nextflow are marked
  `nextflow`, and only for them does the tests' site hold Nextflow in `bin`; Lint And Test's Nextflow Runtime step
  selects them in `tests/core/test_sites.py` with `tests/core/nextflow`. The one test there that needs no site, which
  checks the other names of the options a site sets or reads against Toil's and cwltool's parsers and `nextflow run -h`,
  stays there, so that one file holds every test that needs Nextflow and `tests/core/test_sites_unit.py` stays under 750
  lines. `tests/core/test_ci_coverage.py` fails when a package lane selects one, or no pull-request lane does. The pair
  rules of `tests/core/data/run-config-pairs.yaml` run through the site's door beside the local and the compute doors,
  with the lines only a site adds (`site_refused`). The site codes' provocations (`tests/core/provocations.py`) go
  through the same local `bash`; on the Windows leg, where Sophios makes no site, only `wic062` fires, and
  `tests/core/test_run_on_windows.py` checks that a `Machine` is refused there.
- **S3,** in the same two files and lanes, with a fake Slurm of Sophios's own, `tests/core/fake_slurm.py`, a helper no
  lane collects as a test (`sbatch`, `squeue`, `sacct`, `scancel`, `sinfo`, as much of each as Sophios and Toil ask;
  each `sbatch` option recorded; a job pends for `hold`, a time longer than its partition's, or `singleton`). In
  `tests/core/test_sites_unit.py`: a Slurm site refuses a `SlurmSettings` the compute client refuses, in its words,
  every problem at once, the steps' settings too under Toil (the forms themselves are
  `sophios.compute.slurm_problems`'s, which the client's tests hold); the states property takes Slurm's lives too, every
  claim, exit status and state Slurm ends a leader in, the ten-minute and two-minute windows, a job that will never
  start, the step jobs a killed runner left, and a failing `squeue` or `sacct` deciding nothing; a table of what a
  status reads from Slurm's own output, every pending reason a job never starts with and two that clear in time; Toil's
  argv on Slurm read back by Toil's own parser over generated placements, the account split as Toil splits it, and the
  leader's header and the `SBATCH_` variables unset for it; where a run's jobs go, each setting with where it came from,
  by a property against a plain model of the fallback rules, over generated settings, `SBATCH_` values, `sinfo` rows
  (with no GPU partition, one or two), runners and steps' needs, and a few rows at its edges (a partition your
  `SBATCH_PARTITION`, the cluster's default or the one GPU partition gives in a form the setting would not take); what
  each question asks of Slurm, the run's own jobs alone, that a status whose `squeue` or `sacct` fails raises, deciding
  nothing, and that a stop or a cancel Slurm refuses does nothing. In `tests/core/test_sites.py`, end to end with the
  real Toil and cwltool: the same outputs as a local run and a `Machine`, which sends no script that names Slurm, and a
  submit whose answer is lost before or after Slurm took the job.
- **No cluster probes** (the compute design's decision 38). By hand on one of the owner's clusters before a site
  leaves "experimental": the claim and `exit` over NFS, a cleanup cut short by `KillWait`, a login profile's
  `module load` under `set -u`, Apptainer across nodes, Nextflow's head job under the partition's default memory.

## 17. What S2 found while it was built

Each is the smallest change that keeps the design's intent.

1. **No `setsid` on macOS.** A workstation may be a Mac, which has no `setsid`. The submit call runs `set -m` before
   `nohup bash -l <run>/job.sh ... &`: bash's job control puts the leader in a process group of its own, whose id is
   its pid, the claim, which `cancel()` signals. Measured with bash 3.2 (macOS) and 5 (Linux).
2. **The traps follow the claim at once** (Appendix A): a line of the environment that fails under `set -e` is
   recorded in `exit` as the job's failure rather than read as a hard kill, and a TERM before the runner starts is a
   cancel. A leader that loses the claim exits before it sets them, so it records nothing.
3. **Toil's scratch folders are made before Toil starts:** Toil 9.5 refuses a `--workDir` that does not exist
   (measured), so `job.sh` makes `<run>/toil/work` and `<run>/toil/tmp`, as it makes Nextflow's output folders, and
   goes to `<run>`, or to the folder `launch` names.
4. **What a login profile prints is never read as the script's:** the guard prints the script's hash before it runs
   it, and Sophios reads only what follows. A profile that prints a banner is common; one that reads its input is
   refused, `wic062`.
5. **The range Sophios supports** for a runner on a site is a floor: cwltool 3.1.20241217163858, the version Sophios
   requires, Toil 9.5.0 and Nextflow 26.04.2, the versions this design was read against. The version is the last
   `a.b.c` that `<bin>/<name> --version` (`-version` for Nextflow) prints.
6. **On a machine, Toil's `single_machine` holds each step to the machine's memory:** it gives no more than the
   machine has, and refuses a step that asks for more when it would start it (measured with Toil 9.5 under
   `--maxMemory 2Gi`: a step that declares no memory fails, asking for 4 GiB; one with `ramMin` runs). So the memory
   note holds on a machine as on Slurm: `check()` notes, `wic066`, each Toil step whose tool declares no memory. A
   step declares memory as Toil reads it: the `ResourceRequirement` that wins by cwltool's lookup (the tool's own
   requirement, else the one it inherits, else its own hint, else the hint it inherits) names `ramMin` or `ramMax`.
7. **Decision 18's exception lands with S2,** the first PR that runs a generated pipeline on a site and sets one of
   its settings (`executor.queueSize`, in the block it appends to the run's configuration, decision 25), so the
   Nextflow design's non-goal says so from the PR that makes it true.
8. **On a machine, a run unclaimed 10 minutes after its submit is claimed `none`,** as on Slurm, so a job script that
   died before its claim (a login profile that exits, say) does not read QUEUED for ever. A hard kill's reason names
   `<run>/toil` only for Toil, and only without `exit`, since with one the trap ran.
9. **`outcome` reads each file it names in a call of its own, once:** the ended status is kept, so however often
   `status()` is asked, a run's outcome is read once.
10. **Sophios on Windows itself makes no site:** `Machine(...)` raises `wic062` when it is made, as
    `sophios.compute.Service` refuses Windows when it is made: a site's paths are POSIX paths, which Python on Windows
    reads as its own. Inside WSL, Sophios makes one.
11. **Under `--bypass-file-store`, Toil cannot read its own form of an empty folder** (`toildir:` fails: no such file
    or directory, measured), the form a local Toil run writes for an output folder of the workflow. A site writes
    both CWL runners' files as cwltool's backend writes them, the folder a CWL literal, `{"class": "Directory",
    "basename": ..., "listing": []}`, which Toil reads under the bypass, as the compute service sends it.
12. **A live leader is found by the run's id, not by its folder:** `ps` prints a newline in an argument as `\012`
    (measured on macOS), so with a `runs` holding one a live run read as killed, and its status would have emptied
    its outputs folder. The id holds no character `ps` prints otherwise.
13. **On a site, Nextflow fills a copy of an output folder given a name** in its work folder: `<run>/made/<name>`
    stays empty, and the filled copy is published into the outputs folder (measured).
14. **Toil, cwltool and Nextflow each fail on a path that holds a newline,** a local run's as a site's (measured), so
    the test of hostile values runs a recording stand-in for the runner; the quoting is what it checks.
15. **The lowering for a site lives in `sophios.sites._site`,** beside its other reading of the workflow:
    `api/python/_workflow_runtime.py` stays within its line budget, and a site reaches the compile through
    `compile_workflow_result` and the resolved view, as the runtime adapter does.
16. **Known limit: a `StepInput(default=...)` File or Directory** is made absolute against the working directory
    when the Python document is built, before any target or site sees it, so on a site it names a path here. It
    sits in the document, not the job, so neither the pre-flight nor a site's `check` reads it, and the runner there
    reports it. Bind such a value on a workflow input instead, which is read from `relative_to`; `docs/sites.md` says
    so.
17. **A run killed outright has not ended until a status says so:** with the leader gone and no `exit`, `check`
    once read the run as ended, so a second run could take its outputs folder and complete there, and the first
    status of the killed run then emptied the second run's outputs (measured with Toil). Such a run now holds its
    folder until a status has emptied it and written its `reason`, and the status's emptying runs only while no
    `reason` is there (decision 33).
18. **A hard kill of the leader's process group leaves Toil's steps running:** each runs in a worker of its own
    process group, which goes on until its step ends (measured with Toil 9.5 on macOS). `docs/sites.md` says so, and
    the tests end every process that names the test's folder.

## 18. What S3 found while it was built

Each is the smallest change that keeps the design's intent.

1. **Toil 9.5 reads a time whose days are 0 otherwise than Slurm** (measured: its `parse_slurm_time('0-12')` is 720
   seconds, where Slurm reads 43200), and the service, whose forms a site takes (39), takes `0-12`. So a site gives
   Toil the steps' time as `H:MM:SS`, which both read alike, and never no limit (40); the leader's `--time` is
   Slurm's to read, as written. The service gives Toil the time as written.
2. **Toil writes no provenance under `--batchSystem slurm`**, which the backend already drops, and makes
   `--batchLogsDir` itself; the run's folder holds `batch/` and no `provenance/`. Toil tries a failed step again
   once, so a failed run leaves the leader's job and two of the step's.
3. **One `squeue` lists the user's jobs with their comment and name** (`-o '%i|%T|%r|%k|%j'`): the leaders by name,
   Toil's step jobs by the run's comment, for a step job that will never start and for those a killed runner left.
   `scancel` has no comment filter, so `status()` cancels those by id.
4. **A stop is done before it is recorded.** On Slurm, `cancel()`, and `status()` when it stops a run, run `scancel`
   first, then the claim, and `status()` writes `reason` last; a `scancel` Slurm refuses ends the call, which raises
   (`wic062`) having written nothing, so the next `status()` decides again. `scancel` by name stops a job whether or
   not it has claimed the run, so none starts in between. On a machine the claim still comes first, since TERM
   reaches only a claimed pid.
5. **A run Slurm lists a job of has not ended,** whatever its folder says: it holds its outputs folder at `check`,
   so a leader that waits in the queue for hours, or one whose `exit` has not shown yet, keeps it; while `squeue` does
   not answer, every run with no `exit` holds its folder. With 33's rule for a killed leader, no status and no trap
   empties a folder another run has taken.
6. **A cancel whose cleanup Slurm cut short** (a leader Slurm ended CANCELLED, with no `exit` two minutes on) has its
   outputs folder emptied by the status that finds it, which writes a `reason` starting `cancelled`, read as
   CANCELLED, so every later status reads the same end.
7. **Adoption asks where the service's driver asks:** `squeue` for a job named after the run, then `sacct` since the
   submit, so a leader Slurm took and that ended before the second look is adopted, and the job `submit` prints is the
   job that ran; submitting again would be safe, the claim making a second leader exit having changed nothing, but
   it would print a job that did nothing. When the `sbatch` that submits again fails, the run is claimed `none` in the
   same call: a job Slurm took all the same then exits at its claim, so the run did not start, which `submit` says,
   and its `reason` says why; the claim failing means a leader claimed the run meanwhile, which is adopted.
8. **The `SBATCH_` variables unset** are sbatch(1)'s for each option the header writes: three for `--mem`, `SBATCH_GRES`
   for `--gres`, two for `--no-requeue`; `--comment`, `--chdir`, `--dependency` and `--cpus-per-task` have none. An
   option the header does not write keeps yours.
9. **A step's resources are read as CWL reads them**, `coresMin` else `coresMax`, rounded up, and the same for `ram`
   and `cudaDeviceCount`, from the requirement that wins at the step; an expression is not read.
10. **`sacct --starttime` is the submit's time,** `run.json`'s, read on the site (`date -r run.json`); the two minutes
    are counted on the site's clock against `sacct`'s End (`SLURM_TIME_FORMAT=%s`).

## Appendix A. The leader script

One script for every runner and both schedulers. Sophios fills each `<...>` with a quoted value, one word whatever it
holds. On a machine, with `toil-cwl-runner`, as S2 writes it:

```bash
#!/bin/bash -l
set -euo pipefail
ln -s "$$" <run>/leader 2> /dev/null || [ -L <run>/leader ] || ln -s "$$" <run>/leader
[ "$(readlink <run>/leader)" = "$$" ] || exit 0
trap 'code=$?; set +e; chmod -R u+rwX <run>/toil/tmp <run>/toil/work; rm -rf <run>/toil/tmp <run>/toil/work; [ "$code" = 0 ] || { chmod -R u+rwX <outdir>; find <outdir> -mindepth 1 -delete; }; echo "$code" > <run>/exit.partial; mv <run>/exit.partial <run>/exit' EXIT
trap 'exit 143' TERM
unset "${!TOIL_@}" "${!PYTHON@}"
export PYTHONNOUSERSITE=1 TOIL_HISTORY=False
export PATH=<bin>:"$PATH"
mkdir -p <run>/toil/work
mkdir -p <run>/toil/tmp
cd <run>
{ <bin>/toil-cwl-runner <launch's flags, the --outdir <outdir> among them> \
    --batchSystem single_machine --bypass-file-store --leave-tmpdir \
    --no-cwl-default-ram --defaultMemory 4Gi --maxJobs <max_parallel> \
    --workDir <run>/toil/work --tmpdir-prefix <run>/toil/tmp/t --tmp-outdir-prefix <run>/toil/tmp/o \
    --clean <clean> [--logLevel <the config's log_level>] <the runner value's other flags> \
    <the documents> | tee <run>/stdout; } >> <run>/run.log 2>&1
```

`<run>` is `<runs>/<id>`, `<outdir>` the outputs folder (decision 33), and `<max_parallel>` and `<clean>` what the
runner value's `--maxJobs` and `--clean` say, else 64 and `always` (decision 34).

**What varies.** Only the scheduler's lines, the runner's line and the trap's scratch:

- **cwltool:** `<bin>/cwltool` with `launch`'s flags, without the `--disable-pull` `launch` gives it and never quiet
  (decision 35), then the runner value's flags, a `--disable-pull` or `--quiet` of its own among them; the trap
  removes no scratch.
- **Nextflow:** `<bin>/nextflow` with `launch`'s flags (`-output-dir <outdir>` among them), then the runner value's
  flags but `-qs` (`-queue-size`); `export NXF_SYNTAX_PARSER=v2`, `launch`'s variable; `cd` to the pipeline's
  folder; what the site sets is the block it appends to the run's copy of the generated `nextflow.config` (decision
  25), `executor { queueSize = <max_parallel> }`; before it, `mkdir -p` of each folder `write` returned; the trap
  removes no scratch.
- **Written only when needed:** under Singularity, the image caches (decision 32): `export
  CWL_SINGULARITY_CACHE=<runs>/.images` for a CWL runner, `export NXF_SINGULARITY_CACHEDIR=<runs>/.images/nextflow`
  for Nextflow, and for both `export SINGULARITY_CACHEDIR=${SINGULARITY_CACHEDIR:-<runs>/.images/cache}` and the
  same for `APPTAINER_CACHEDIR`; Toil's `--logLevel` with the config's `log_level`.
- **`--clean`** (Toil only): `onSuccess` and `onError` pass on, and the trap removes `toil/` only after a success, or
  only after a failure; `never` passes on, and the trap removes nothing.
- **On Slurm:** the header below, after the first line; the claim takes `$SLURM_JOB_ID` for `$$`. Toil runs
  `--batchSystem slurm` (not `single_machine`) and, before `--clean`, `--batchLogsDir <run>/batch
  --slurmPartition=<steps_partition> --slurmTime <steps_time> [--slurmGPUPartition=<gpu_partition>]
  '--slurmArgs=--comment=sophios:<id> [--account=<account>]'`, each as placed (decision 40); the steps' time as
  `H:MM:SS`, which Toil reads as Slurm does (§18), and never no limit (decision 40). cwltool's line is a machine's:
  its run is the leader alone, which the header sizes (decision 42). The values are where the site placed the run,
  the same values `run.json` records.

```bash
#SBATCH --job-name=<id>
#SBATCH --comment=sophios:<id>
#SBATCH --account=<account>
#SBATCH --partition=<partition>
#SBATCH --time=<time>
#SBATCH --mem=<memory>
#SBATCH --cpus-per-task=<cores>
#SBATCH --gres=gpu:<gpus>
#SBATCH --output=<run>/run.log
#SBATCH --error=<run>/run.log
#SBATCH --open-mode=append
#SBATCH --chdir=<run>
#SBATCH --no-requeue
#SBATCH --dependency=singleton
```

`--account` is written only with your `SBATCH_ACCOUNT`, `--mem` only with a memory (decision 40), `--cpus-per-task`
only under cwltool, and `--gres` only under cwltool for a step that asks for a GPU. `submit` unsets, for `sbatch`, your
`SBATCH_` variables of each option written: `SBATCH_JOB_NAME`, `_ACCOUNT`, `_PARTITION`, `_TIMELIMIT`, the three of
`--mem` (`_MEM_PER_NODE`, `_MEM_PER_CPU`, `_MEM_PER_GPU`), `_GRES`, `_OUTPUT`, `_ERROR`, `_OPEN_MODE`, and the two of
`--no-requeue` (`_NO_REQUEUE`, `_REQUEUE`).

**Facts it relies on** (measured, 2026-10-28): the `TOIL_` unset keeps a `TOIL_SLURM_ARGS` in your profile from
overriding the step flags; `TOIL_HISTORY=False` keeps every run out of one SQLite file in an NFS home;
`--defaultMemory` does nothing without `--no-cwl-default-ram`; `#!/bin/bash -l` makes your profile, and what it
loads, available to the job. With `set -o pipefail` the pipeline's status is the runner's, so the EXIT trap records
the runner's exit. On Slurm, `--slurmArgs=` needs its `=`, or Toil refuses the comment; a requeued leader would start
on its first attempt's job store; `--error` is written, since an `SBATCH_ERROR` in your profile would take stderr
elsewhere.

## Appendix B. The states

The first row that applies decides. A machine reads its folder and `ps` alone; on Slurm the folder decides first, and
Slurm's `squeue` and `sacct`, asked in the same call, only when it has not.

| State | How `status()` reads it |
|---|---|
| FAILED, or CANCELLED | `reason` exists: written by a status that found the leader killed outright, or that stopped a run Slurm would never start; it reads CANCELLED when it starts with `cancelled` (a cancel whose cleanup was cut short) |
| COMPLETED | `exit` is 0: the runner's outputs, by its backend's `outcome`, are in place |
| CANCELLED | `leader` is `cancelled` |
| FAILED | `leader` is `none` (no job claimed the run within 10 minutes); `exit` is 1 to 128, with the failed steps from the backend's `outcome` |
| on a machine | `exit` 143 is CANCELLED, another above 128 a kill, FAILED; no claim is QUEUED, and 10 minutes after the submit the run is claimed `none`; a claimed leader `ps` finds is RUNNING, and one gone with no `exit` a hard kill, FAILED, whose status empties the outputs folder and writes `reason` |
| on Slurm, a job that will never start | a job of the run pends for a reason no wait clears (`PartitionTimeLimit`, a per-job limit of the QOS or the association, an account or QOS not allowed) while no `exit` shows (for an unclaimed run, its leader): `status()` stops the run as `cancel()` does, then writes `reason`, naming the setting to change, the run's for its own job, the steps' for a step's; FAILED |
| on Slurm, unclaimed | QUEUED while Slurm lists a leader of the run pending (its reason Slurm's), running, or ended less than 2 minutes ago, or within 10 minutes of the submit; then claimed `none` |
| on Slurm, claimed | RUNNING while Slurm lists the claimed leader unended, or ended less than 2 minutes ago with no `exit`, or lists it nowhere and no `exit` shows. Otherwise it ended: CANCELLED when Slurm says CANCELLED and `exit` is 143 or none; else FAILED, by Slurm's state (TIMEOUT names `time`; OUT_OF_MEMORY, PREEMPTED, NODE_FAIL) or by the signal. With no `exit`, the status empties the outputs folder and writes `reason`, naming `<run>/toil` for Toil; the step jobs the runner left are cancelled |

- **Slurm is asked for the user's jobs:** `squeue -a -h -u "$(id -un)" -o '%i|%T|%r|%k|%j'`, the run's step jobs by
  its comment, `sophios:<id>`, its leaders by its name, then `sacct -n -X -P --name=<id> --starttime=<submit> -o
  JobID,State,End`, with `SLURM_TIME_FORMAT=%s`, for a leader that has left the queue. Without `-a` a hidden
  partition's jobs are not listed; without `--starttime`, `sacct` lists only today's.
- **A failed `squeue` or `sacct` decides nothing:** it raises, `wic062`, and nothing is written or cancelled. "Not
  listed" comes only from a `squeue` that exited 0. The 2 minutes let `exit` show over NFS, which caches entries up to
  60 s.
- **A stop is done before it is recorded** (§18): `scancel` first, then the claim and `reason`; a `scancel` Slurm
  refuses ends the call, which raises having written nothing.
- **A run that has not ended holds its outputs folder** (decision 33): on Slurm, a run Slurm lists a job of, whatever
  its folder says, and, while `squeue` does not answer, every run with no `exit`.
- `started_at` is the claim's time, `ended_at` `exit`'s, `outputs` and `steps` from the backend's `outcome`.
