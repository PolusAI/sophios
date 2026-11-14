# Sites: Run a Workflow on a Cluster or a Machine as Yourself — Design

**Status:** Approved by the owner on 2026-11-09. Experimental. S1 to S4 are implemented. A commit that changes this
file has a subject beginning with `design:`. What S2 found while it was built is in §17, what S3 found in §18, what S4
found in §19.
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
here. It copies those files (never the data) into a run folder there, and starts the runner you chose, from the `bin`
folder you name: as one leader job on Slurm, or as a detached process on a machine. Every runner of the target table
runs on every site, or is refused there with a code. Nothing of Sophios is installed there, and no service runs. The
run folder is the record, and any Sophios on any computer follows the run from it. `wf.run()` and `collect()` stay
local only.

## 2. The first message (every doc page and demo opens with it)

> **Your data must already be on the site.** Sophios sends the workflow there, never your data, and the results stay
> there beside your data. Write input paths relative to your project folder (`data/plate1`, not `/Users/me/...`),
> and run Python from that folder. The same line then reads `./data/plate1` here and `/project/me/cp/data/plate1`
> there. **Your runners are yours:** `bin` names the folder that holds them on the site. **Your container images
> must be in a registry the site can pull from.**

What still differs: the container engine (Docker here, Apptainer on a cluster), and a Toil step that declares no
memory gets no limit here and 4 GiB there.

## 3. What a scientist writes

```python
from sophios.sites import Machine, Slurm

hpc = Slurm('me@cluster', root='/project/me/cp', bin='/project/me/envs/run/bin')
ec2 = Machine('my-ec2', root='/data/cp', bin='/opt/conda/envs/run/bin')   # a Host in ~/.ssh/config

segment.inputs.images = {'class': 'Directory', 'location': 'data/plate1'}  # relative: here and there
run = wf.start()                                    # here
run = hpc.submit(wf)                                # there: CWL with toil-cwl-runner, each step a Slurm job
# Submitting segment-20261109T142205-7f3a91c2 to me@cluster: toil-cwl-runner (a site's default for cwl),
# partition normal (the cluster's default), time 2-00:00:00 (its longest), at most 64 step jobs (Sophios's default).
# Outputs: /project/me/cp/sophios/runs/segment-20261109T142205-7f3a91c2/out
run = ec2.submit(wf, target='nextflow')             # there: Nextflow, Docker, the local executor
status = run.wait()          # Ctrl-C stops following; the run goes on
later = hpc.attach('segment-20261109T142205-7f3a91c2')
diagnostics = hpc.check(wf, target='nextflow')      # every problem submit would hit, at once; writes nothing
```

## 4. The surface

`sophios.sites` is a core feature, not part of the Python API: `sophios.api.python` neither exports nor knows it, as
with `sophios.compute`.

```python
Slurm(host, *, root, bin, container_engine='singularity', account=None, partition=None, gpu_partition=None,
      time=None, modules=())
Machine(host, *, root, bin, container_engine='docker', modules=())
site.check(workflow, *, target='cwl', runner=None, outdir=None, max_parallel=64, clean='always',
           tool_registry=None, inference=Inference()) -> list[Diagnostic]
site.submit(workflow, *, target='cwl', runner=None, outdir=None, max_parallel=64, clean='always',
            tool_registry=None, inference=Inference()) -> Run
site.attach(run_id) -> Run
Run: id, status() -> RunStatus, wait(timeout=None) -> RunStatus, outputs() -> Json | None, logs() -> str,
     cancel() -> bool
```

- **`bin` is required.** It is the folder on the site that holds the runners and what they need:
  `toil-cwl-runner`, `nextflow` and the Java it runs on. Sophios never searches a login shell for a runner.
- **`check` returns diagnostics, as #586's `Workflow.check` does**, and writes nothing on either side. `submit`
  runs every check of `check` and raises them as a `SophiosError`.
- **`Run` is the local handle's protocol** (`status`, `wait`, `outputs`, `logs`, `cancel`, as `RunHandle` and
  `sophios.compute.Run`), plus `id`, with no `collect`. Its `repr` is the line that attaches to it. Its run outlives
  Python. When SSH or the scheduler does not answer, `status()` raises and decides nothing.
- **No setting takes shell code.** Each is checked against a pattern when the site is built (a `ValueError` names
  it), and enters a script only quoted. `root` and `bin` hold only `[A-Za-z0-9/._+-]`: they go into `#SBATCH` lines.
- **`Machine` is a computer of your own, never a login node.** Workstations are macOS and Linux; Windows is refused.

## 5. The one table Sites adds: each runner on each scheduler

A site is a transport and a scheduler. The transport is your own `ssh` for every site: nothing about it is per site.
The scheduler is the only per-site code: how the leader starts so it outlives SSH, how its state is read, how it is
stopped, and how each runner spreads the steps over it:

| Runner (a row of `targets.TARGETS`) | `Machine` | `Slurm` |
|---|---|---|
| `cwl` / `cwltool` | refused, `wic058` (decision 22) | refused, `wic058` |
| `cwl` / `toil-cwl-runner` (the default) | `--batchSystem single_machine` | `--batchSystem slurm`, each step a Slurm job of its own (Appendix A) |
| `nextflow` / `nextflow` | the local executor | `executor = 'slurm'`, each task a Slurm job of its own; a GPU task on the GPU partition, with `--gres` |

- **Every runner of the table runs on every scheduler, or is refused there with a code.** A test walks
  `targets.TARGETS` across both schedulers, so a runner added to the target table cannot silently miss Sites.
- **No site runs cwltool** (decision 22). Toil runs the same CWL on a machine and on Slurm, so a site needs one CWL
  runner, and a site's default for a target is its first runner a site runs: `toil-cwl-runner` for `cwl`.
  `runner='cwltool'` is refused, `wic058`, naming Toil, before anything is compiled or sent.
- **Nextflow's containers need Docker on a site.** That is the restriction of the pipeline `compile()` writes, which
  a site runs, not Sites': its generated `nextflow.config` enables Docker, and each process with a container reads
  its image's entrypoint with `docker image inspect` in its `beforeScript`. On a site whose `container_engine` is not
  `docker`, `check` refuses each process with a container, by name; a workflow whose processes have none runs on Slurm.
- **What a site sets for Nextflow is a block it appends to the run's copy of the generated `nextflow.config`,** the
  one configuration the backend has Nextflow read (`-C`), from one function in `sophios.sites` (decision 25). Nextflow
  is set by its configuration, and only a configuration sets a task's options by the task. The Nextflow backend never
  learns of sites (decision 18).
- **`max_parallel`** is Toil's `--maxJobs` and Nextflow's `executor.queueSize`.
- **`outdir`** is a CWL runner's `--outdir`, by default `<run>/out`. A Nextflow run's outputs stay in its work
  folder, where the Nextflow target leaves them, so `outdir=` is refused with `target='nextflow'`.

## 6. On #586: a backend describes, the machine acts (S1)

#586 made the target table the one place a target and its runners are found; each runner is a `Backend` with
`check`, `prepare`, `launch`, `outcome` and `realtime_command`. On #586 a backend both describes a run and acts on
this machine. A site needs the description without the action, so S1 splits them, with no change of behaviour
locally:

1. **`launch` is pure.** No `.resolve()` (on macOS it rewrites a site's `/tmp/...` to `/private/tmp/...`), no
   `Path.cwd()`, no `os.environ`, no clock: the caller gives absolute paths and an explicit output directory, and
   `run_local` merges this process's environment. Each backend names its `program`, its executable (`cwltool`,
   `toil-cwl-runner`, `nextflow`); a site runs `launch`'s argv with `argv[0]` set to `<bin>/<program>`, while a
   local cwltool run keeps `cwltool_filterlog`.
2. **`prepare` is `write` and then this machine's part.** `write` writes the run's files under a folder and returns
   what the runner's machine must still do before the run starts (today: make the output-target folders Nextflow
   stages, which `NextflowBackend._prepare` made on this machine). `prepare` stays what it is locally: `write`, then
   the checks, the pulls and the folders, here. A site calls `write` into a local staging folder.
3. **`needs` says what a run asks of its machine:** the paths its jobs name, its program, and its images.
   `check` judges them on this machine, as on #586; a site judges them on the site, in one SSH call.
4. **`outcome` reads what it needs through `read(name) -> str | None`, relative to the run's folder, and writes
   nothing**; the caller writes the summary (`output_<name>.json`) that Toil's and Nextflow's outcomes wrote on #586.
   A site reads them over SSH, once, when the run has ended.

Reading relative paths against the site's `root` needs nothing new: `run_setup.absolute_view` takes a `PurePath` and
touches no filesystem (the 2026-10-29 design's first PR is gone).

### 6.1 The protocol, as S1 builds it

```python
class Backend(Protocol):
    program: str            # read-only: cwltool, toil-cwl-runner, nextflow; launch's argv[0] but for cwltool
    writes_summary: bool    # read-only: the runner writes output_<name>.json itself (cwltool, given --write-summary)

    def write(self, lowered, analyses, basepath, *, inputs_file, root_dir) -> run_setup.Prepared:
        """The run's files under basepath and nothing elsewhere; Prepared.folders, what its machine must still make."""
    def needs(self, documents, jobs) -> preflight.Needs:      # the jobs (their paths), the container requirements
    def check(self, needs, settings) -> None:                 # judged on this machine; the program by settings.runner
    def prepare(self, lowered, analyses, basepath, settings) -> run_setup.Prepared:   # write, then this machine
    def launch(self, request) -> Launch:                      # from the request alone
    def outcome(self, request, exit_code, stdout, log, read) -> Outcome:              # writes nothing
    def realtime_command(self, request) -> realtime.Command | str:                    # unchanged
```

| | #586 | S1 |
|---|---|---|
| `launch` | `request.basepath.absolute().resolve()`; the default outdir from the clock; `Path.cwd()`; `{**os.environ, **request.env}` | the request's paths as given; `request.outdir` as given; `cwd` None for a CWL runner (wherever it is started), the pipeline's folder for Nextflow; `env` the run's variables alone (and `NXF_SYNTAX_PARSER=v2`) |
| who places a local run | each backend | `run_local.launch(request, backend)`: `basepath` absolute and resolved, `outdir` absolute and resolved or `<basepath>/outdir_<program up to its first '-'>_<name>_<time>` (`outdir_cwltool_`, `outdir_toil_`, as on #586), `cachedir` absolute, then `{**os.environ, **launch.env}` |
| `Launch` | `cwd: Path`, `env` this process's and the run's | `cwd: Path \| None`, `env` the run's alone |
| `prepare` | writes, checks, pulls, makes folders | `write`, then the check, the pulls (CWL), the entrypoint rewrite (CWL), and each of `write`'s folders that names nothing yet (Nextflow) |
| `run_setup.Prepared` | `name`, `plans`, `documents` | and `folders: tuple[str, ...]`, each output-target Directory Nextflow stages, sorted by input name; `()` for CWL, whose runners make them |
| `run_setup.write` | inside `_prepare` | its own function: the job's paths absolute against `root_dir`, the documents and the analyses; `_prepare` calls it, and calls it again for `--docker_remove_entrypoints` |
| `check` | `check(documents, jobs, settings)` | `check(needs(documents, jobs), settings)`; Nextflow's images are read from the pipeline in `needs` |
| `preflight.Needs` | `documents`, `containers`, `jobs`; `_image_names(found)` | the same fields; `Needs.images` replaces `_image_names`; a Nextflow process's container is a `DockerRequirement` with that `dockerPull` |
| `outcome` | `outcome(request, exit_code, stdout, log: Path)`; Toil's and Nextflow's write the summary | `outcome(request, exit_code, stdout, log: str, read)`; `stdout` and `log` as text, `read` for the summary and `nextflow_<name>/nextflow_names.json`; it writes nothing |
| `Outcome` | `outputs`, `summary: Path \| None`, `failed` | `outputs`, `failed`; `RunHandle` writes `outputs` to `output_<name>.json` when the backend's runner does not (`writes_summary`) and there are outputs, and names the file when either holds, as on #586 |

Three choices this table makes, each the narrowest that keeps the four changes:

- **`needs`'s program is the backend's `program`,** not a field of `Needs`. Locally the program a check looks for is
  `RunSettings.runner`'s, as on #586 (`cwltool_filterlog` for cwltool); a site looks for `<bin>/<program>`. A
  `Needs.program` would be a second record of the same name with no reader in S1.
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

## 7. How a run goes

**Every call** is `ssh -T -o BatchMode=yes -o RemoteCommand=none -o ConnectTimeout=20 -o ServerAliveInterval=15 -o
ServerAliveCountMax=3 <host> "bash -l -c '<guard>'"`. The script comes on stdin, and the guard runs it only when its
sha256 matches: a profile that reads its input would eat part of the script and run the rest (measured). A two-factor
cluster works through a shared connection (`ControlMaster`) opened by hand. The guard prints the hash before it runs
the script, so what a login profile prints is never read as the script's (§17). Tests replace `ssh <host>` with a
local `bash`, and nothing else.

**`check()`** compiles and lowers here, through the target table, so a workflow the target cannot run is refused with
its `nf-s` code before anything reaches the site. It reads relative paths against `root`, refusing `..`, `~`, `$` and
real-time analyses. One call then reports every File and Directory the run reads, by the backend's `needs`; the
runner, `<bin>/<program>`, and its version against the range Sophios supports (§17); the container engine (`docker info`
on a machine; `singularity` or `apptainer` on Slurm); on Slurm `sbatch`, `squeue`, `sacct` (it must answer),
`scancel` and `sinfo -a`, and your `SBATCH_ACCOUNT`, `_PARTITION` and `_TIMELIMIT`; and whether `<root>/sophios` is
writable and `outdir` is free. `SBATCH_CLUSTERS` is refused: no runner can follow step jobs to another cluster.

**`submit()`** prints the id, the runner, the partition, time and `max_parallel` with their sources, and the outputs
folder. One call sets `trap '' HUP PIPE` (a dropped connection cannot stop it between starting the leader and the
record), makes `<run>` and `outdir` exclusively, writes the files, and starts the leader: on Slurm `sbatch --parsable
<run>/job.sh`, with the `SBATCH_` variables of the options `job.sh` sets unset, since they beat `#SBATCH` lines; on
a machine `set -m; nohup bash -l <run>/job.sh > <run>/run.log 2>&1 < /dev/null &`, bash's job control giving the
leader a process group of its own, whose id is its pid (§17). Run ids are
`<workflow>-<YYYYMMDD>T<HHMMSS>-<8 hex>`.

**`status()`** reads the folder and asks the scheduler only when the folder has not decided (Appendix B). Once the
run has ended, its outputs and failed steps are the backend's own `outcome` over the run's `stdout`, `run.log` and
summary, read once: a run there reads as it does here. **`wait()`** polls every 5 s for a minute, then every 60 s,
printing each change and what `run.log` gained; Ctrl-C prints the `attach` line and leaves the run going.
**`cancel()`** claims the run as `cancelled`, stops the leader (`scancel -u "$(id -un)" -n <id>`, before the claim
(§18); on a machine, TERM to the job's process group), waits about 2 minutes, and returns whether it ended CANCELLED.

```
<root>/sophios/runs/<id>/
  run.json     the settings, each with its source, the target, runner and clean, Sophios's version
  job.sh       the leader script (Appendix A); beside it, what the backend's `write` wrote
  leader       the claim: a link to the leader's job id (its pid on a machine), or to `cancelled` or `none`
  run.log      the job script's and the runner's two streams; batch/ holds Toil's step jobs' Slurm logs
  stdout       the runner's standard output alone, which its `outcome` reads
  toil/        Toil's work/ and tmp/ (every step's outputs and scratch), removed as clean= says
  out/         the outputs, unless outdir= names another folder (a CWL runner's --outdir)
  reason       only when status() stopped a run the scheduler would never start
  exit         the job script's exit status, written last, after the cleanup
```

## 8. One leader per run

SSH can drop after Slurm accepted the job, `sbatch` can fail or time out after Slurm took it, and Slurm may list a
job only later. So a run can get two leaders, and "Slurm has no job" is never proof. **Sophios never deletes a run
folder.**

- **Adopt, then submit again.** When the submit call fails or its answer is lost, `submit()` asks Slurm for a job
  named after the run (`squeue -n <id>`, then `sacct --name=<id> --starttime=<submit>`). If one exists, it adopts
  it. Otherwise it runs `sbatch` again on the same folder, which the claim makes safe.
- **The claim.** The leader is named after the run, and `#SBATCH --dependency=singleton` makes Slurm run one job of
  that name at a time. The first lines after `set -euo pipefail` claim the run with an `ln -s "$SLURM_JOB_ID"
  <run>/leader`, atomic on NFS too. A later leader exits 0 before it touches anything, so the folder keeps the first
  one's outcome. `--open-mode=append` keeps it from emptying `run.log`. On a machine the claim takes `$$`.
- **Ending a run before it starts.** Sophios takes the claim itself. `cancel()` links `cancelled`. `status()` links
  `none` when neither `squeue` nor `sacct` has a job of the run 10 minutes after the submit. A leader the scheduler
  lists only later then exits at its claim, so an end, once seen, never changes.

## 9. Cleanup

**`clean='always'` is the default: a run cleans up however it ends,** success, failure, cancel or time limit. What
is cleaned is the runner's scratch, and only Toil leaves any outside its outputs: Toil's `--clean always` removes the
job store, and once Toil has stopped the EXIT trap removes `toil/tmp` and `toil/work`, `chmod -R u+rwX` first for a
folder a tool left unwritable. Neither follows a link, so linked inputs stay intact. On a cancel or the time limit
Slurm sends TERM to every process of the job; Toil stops its step jobs and exits, and only then, the runner being in
the foreground, does bash run its traps. Nothing is forwarded: a second TERM would cut Toil's shutdown short.
`'onSuccess'` and `'never'` are Toil's names: the first keeps the job store and `toil/` after any end but a success.
A Nextflow run's work folder holds its outputs, so for Nextflow only the default is accepted.

**Known leftovers.** A hard kill (SIGKILL, a node failure, the leader out of memory, an instance stopped) stops the
runner before its cleanup, and the trap too unless only the runner died: `<run>/toil` stays, and `status().reason`
says so, with the `rm` line. The `status()` that finds a hard kill cancels the step jobs still queued with the run's
comment, `sophios:<id>`, which Sites puts on every step job of every runner, under every `clean`.

**Disk.** Nothing is node-local: every step's outputs and scratch stay in the run until it ends. Toil runs with
`--bypass-file-store --leave-tmpdir`: the bypass alone collapses a scattered step's same-named outputs into one file
(measured: "b b b" downstream, COMPLETED); `--leave-tmpdir` names them as the file store does (`out.txt`,
`out.txt_2`). Once a Toil release has DataBiosphere/toil PR #5663 (fixing #5662 and #5559), `--leave-tmpdir` goes.
**Permissions:** you run as yourself, so your umask applies.

## 10. Conventions, and the one override each

| What | Convention | Override |
|---|---|---|
| The runner | the target's first runner a site runs: `toil-cwl-runner` for `cwl`, `nextflow` for `nextflow` | `runner=` |
| Outputs | `<run>/out`, printed with the id; a Nextflow run's in its work folder | `outdir=` (CWL runners) |
| The leader | your `SBATCH_PARTITION`, else the default partition; your `SBATCH_TIMELIMIT`, else that partition's longest | `partition=`, `time=`, `account=` |
| Steps | each tool's resources; the run's partition, or for a GPU step the one partition with GPUs; `time=`, else the shortest limit of those partitions | `gpu_partition=` (several GPU partitions), `time=` |
| Memory | a Toil step that declares none gets 4 GiB, `--no-cwl-default-ram --defaultMemory 4Gi` (Toil reads `4G` as 4·10⁹ bytes); `check()` warns for each such step (`wic061`, a note; §17); Nextflow as it runs locally | the tool's own `ramMin` |
| Parallel | at most 64: Toil's `--maxJobs`, Nextflow's `executor.queueSize` | `max_parallel=` |
| The runners | `<bin>/<program>`, `bin` first on PATH in every job | `bin=` (required) |
| Modules | none | `modules=` (names) |
| The container engine | `docker` on a machine, `singularity` on Slurm | `container_engine=` |
| Any other `sbatch` option | `export SBATCH_QOS=...` and the like, in your login profile there | - (decision 23) |

## 11. Codes

A problem on a site is a problem of the machine a run needs, so it is a `wic0NN` code of kind `machine`, explained by
`sophios --explain` and listed in `docs/error_codes.md`, as #586 does for every code. Where the meaning is the one an
existing code has (a missing input file is `wic016` here and there), the site reuses that code, with a message naming
the site. Problems only a site has (it cannot be reached, a scheduler command is missing, a runner no site runs or
`bin` lacks, a run id names no run, Nextflow's containers without Docker) get new `wic0NN` codes. No new range.

| Code | Meaning | PR |
|---|---|---|
| `wic057` | the site cannot be reached or used: ssh, a login profile that read the script, a call that failed there, a platform that is neither macOS nor Linux, or Sophios on Windows itself | S2 |
| `wic058` | the runner is cwltool, which no site runs (decision 22), or `bin` lacks it, it does not run, or it is older than a site runs | S2 |
| `wic059` | a run id names no run there | S2 |
| `wic060` | a Nextflow process with a container on a site whose engine is not Docker | S2 |
| `wic061` | a note: a step Toil gives 4 GiB on a site, its tool declaring no memory (§10) | S2 |
| reused | `wic015` the engine; `wic016` an input, or a path with `..`, `~` or `$`; `wic021` `<root>/sophios`, or an `outdir` that exists; `api008` a real-time analysis (the call's, as `run_async`'s) | S2 |
| `wic062` | Slurm cannot run the run: one of its commands is missing or does not answer (`sacct` included), your login profile sends jobs to another cluster or sets an `SBATCH_` value no job script can carry, a partition the run needs is not there, or a GPU step has no one partition with GPUs | S3 |

## 12. What Sites does not do

- **No Sophios on a site, no service, no daemon** (decision 16): nothing to install, version, run or upgrade there.
- **No search for a runner:** `bin` names it.
- **No shell code in any setting:** no `setup=`, no `sbatch=` lines, no `qos=` yet.
- **No copying of data, no fetch of outputs.** `rsync` exists, and the messages show it.
- **No resume** (decision 21). A failed run is submitted again from the start.
- **No sweep of what a hard kill leaves.** `<run>/toil` stays, and the reason names the `rm` line.
- **No real-time analyses on a site:** they watch cwltool's job folders here.
- **No Apptainer for Nextflow's containers** (decision 19): that is a change to the pipeline `compile()` writes, which
  a site runs.
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
5. **Cleanup is the default** (`clean=`); leftover step jobs are cancelled under every `clean`.
6. **No setting ever takes shell code:** `setup=` would teach scientists remote code execution through strings.
7. **Words:** "site" in the docs, the sites `Slurm` and `Machine`, the verbs `submit` and `attach`; a site is never
   called a backend (a runner's `Backend` is #586's).
8. **`sacct` is required.**
9. **Images are pulled in the leader's job** (a Nextflow task pulls its own, as the Nextflow target does).
10. **No node-local scratch.**
11. **4 GiB for a Toil step that declares no memory**; its own `ramMin` or `ramMax` wins; the warning stays.
12. **Site settings are Python only in v1.**
13. **`max_parallel` is 64.**
14. **No `sophios_dir=`, no `retries=`**; `gpu_partition=` is an override only.
15. **2026-10-29:** cleanup however a run ends; one leader per run; `--leave-tmpdir` until Toil's fix ships; your
    own umask.
16. **2026-11-09: Sophios stays a local install.** Nothing of Sophios goes on a site. The user names the runners'
    folder (`bin`, required); Sophios manages everything else on the user's behalf, with the user's own `ssh`.
17. **2026-11-09: every runner of #586's target table runs on every site, or is refused there with a code**; a test
    over `targets.TARGETS` and the schedulers enforces it. This replaces "Toil only" (2026-10-20) and "no Nextflow
    on Slurm in v1" (2026-10-29).
18. **2026-11-09: the Nextflow design's non-goal gets one exception, for sites,** worded so it can be removed
    whole: a site sets the executor options of its own scheduler, through the run's ordinary passthrough
    arguments, from one function in `sophios.sites`. The backend never learns of sites, so lifting the non-goal
    later deletes a sentence and changes no code. *Its channel is replaced by 25.*
19. **2026-11-09: Nextflow's containerized processes stay Docker-only on a site**, the restriction of the pipeline
    `compile()` writes; Apptainer would be a change to that pipeline.
20. **2026-11-09: the default runners:** a machine runs the target's default (cwltool for `cwl`, as locally); Slurm
    runs `toil-cwl-runner` for `cwl`. *Replaced by 22.*
21. **2026-11-09: no resume until Sites has met real use.** (It was open question 1; the 2026-10-29 design's "Later:
    resume" verified that Toil 9.5's `--restart` works with this design's flags, should it come back.)
22. **2026-11-11: no site runs cwltool; `toil-cwl-runner` is every site's default for `cwl`.** Toil runs on a
    machine (`single_machine`) and on Slurm (each step a job) alike, so a site has one CWL runner: no leader sized
    to the largest step, no one step at a time, no GPU step refused for a runner that asks Slurm for none.
    `runner='cwltool'` is refused there, `wic058`, so decision 17 holds. Toil must be in `bin` (it installs cwltool
    with it), and on a machine Toil holds each step to the machine's memory, so a step that declares none needs
    4 GiB there (the `wic061` note).
23. **2026-11-10: no QOS of Sites' own.** A site sets no QOS, as compute's request has none; one your login profile
    (`SBATCH_QOS`) or the cluster sets applies to every job. Where a QOS allows less time than the partition, the
    first `status()` fails the run, naming `time=`.
24. **2026-11-10: every step job gets one time,** `time=`, else the shortest limit of the partitions the run uses,
    under Toil and Nextflow alike: Toil 9.5 gives every step job one `--time`, so a CPU step longer than a shorter
    GPU limit needs `time=`.
25. **2026-11-11: what a site sets for Nextflow is a block it appends to the run's copy of the generated
    `nextflow.config`,** not passthrough arguments: Nextflow is set by its configuration, and only a configuration
    sets a task's options by the task, so a GPU task under Nextflow on Slurm goes to the GPU partition with `--gres`,
    as Toil's step does. Measured with the pinned Nextflow: a closure given as `-process.queue` reaches the task as
    its text, a `withName:` selector on the command line is ignored, and the backend's `-C` ignores a second file
    given to `run -c`. The backend still never learns of sites, and lifting the non-goal still deletes a sentence
    and changes no code of the backend.

## 14. Open for the owner

Nothing is open.

## 15. Build order

#586 merges first (#585 under it); then one PR at a time, each stacked on the one before, each with its design, its
code and tests, and its docs (two or three commits):

1. **S1. A backend describes, the machine acts:** the four changes of §6; local runs byte for byte as on #586. It
   adds this document. No user-visible change, so no docs commit.
2. **S2. `Machine`:** the transport, the run folder, `job.sh`, the claim, the states, `check`, `submit`, `attach` and
   `Run`, every runner of the table (Toil `single_machine`, Nextflow's local executor, cwltool refused), the codes,
   the refusal of Nextflow's containers without Docker, the one exception in the Nextflow design's non-goal
   (decisions 18 and 25; §17), and `docs/sites.md`. An EC2 instance works from here on.
3. **S3. `Slurm` with the CWL runners:** `sbatch`, the claim and adoption, the defaults and the steps' time, Toil's
   Slurm flags, a leader or step job that would never start, leftover step jobs.
4. **S4. Nextflow on Slurm:** Slurm's settings in the block the exception S2 makes already allows (decision 25),
   each GPU task on the partition with GPUs; S2's refusal of containers without Docker applies on Slurm as on any site.

## 16. Tests

The owner's hierarchy (property, then fixture, then a table, then a single case), within the owner's test budget:

- **The states** (Appendix B) as a function of generated folder and scheduler facts: an end, once seen, never
  changes; a failed `squeue` or `sacct` decides nothing; the folder decides before the scheduler.
- **Quoting:** every value that enters a script is printed back by the real `bash`, over generated strings.
- **The argv:** Toil's read back by Toil's own parser; Nextflow's settings by a run of the pinned Nextflow against the
  fake Slurm, which reads them from the run's configuration, and `executor.queueSize` by the capacity line in its log
  (§19.3).
- **The table:** every runner of `targets.TARGETS` on both schedulers runs, or is refused with its code.
- **End to end:** the scripts with `bash` for `ssh`, a fake Slurm of Sophios's own (the compute driver's is in a
  private repository Sophios CI may not check out), and the real runners, each in the lane that already installs it.
- **S1:** byte for byte over the corpora, as #586 measured its own change (§6.2), outside the suite. In the suite, all
  in the package job, which leaves out the tests marked `nextflow`: in `tests/core/test_backends.py`, each CWL
  runner's argv, read by the parser of its `program`, as `run_local.launch` places a request (the property #586
  has); every backend of `targets.TARGETS` launches from the request alone (a relative base path
  through a link, neither made absolute nor followed, the working directory and the environment changed between two
  launches); each `outcome` reads through `read` and writes into no folder; `write`'s folders, and `prepare` making them
  only for a run. In `tests/core/data/runner-transcripts`, the summary cwltool leaves is kept byte for byte, and which
  runs name it. In `tests/core/test_python_api_workflow.py`, `--docker_remove_entrypoints` rewrites a real-time
  analysis's images too, through the second `write`.
- **S2,** in `tests/core/test_sites.py`, with a local `bash` for `ssh <host>`: in the package job, the states over
  every life a run's folder can have on a machine (exhaustively: each claim, each exit status, with and without a hard
  kill); every value a script carries printed back by the real `bash` through the guard, over generated strings; a login
  profile that prints, and one that reads its input; Toil's argv, as `job.sh` writes it, read back by Toil's own parser
  over generated `max_parallel`, `clean`, engine and outdir; the leader's EXIT trap run by `bash` for each runner,
  `clean` and exit status; `check` finding every problem at once and writing nothing; the settings and options refused;
  whether a step declares memory, as `check` reads it for `wic061`, against cwltool's own lookup, which Toil asks, over
  every placement of a `ResourceRequirement` a tool can write or inherit; and, end to end with the real Toil, a run,
  one that fails, one cancelled while it runs, and one whose job, held in its login profile, finds the run claimed
  `cancelled` by `cancel` or `none` by `status` ten minutes after its submit. The walk over `targets.TARGETS` is one
  test parametrized by its rows: cwltool's is refused, `wic058`, with nothing compiled or sent; its Nextflow row,
  whose executor takes `max_parallel` from the run's configuration, marked `nextflow`, Lint And Test's Nextflow
  Runtime step runs with `tests/core/nextflow`, which it selects by that marker. The site codes' provocations
  (`tests/core/provocations.py`) go through the same local `bash`; on the Windows leg, where Sophios makes no site, only
  `wic057` fires, and `tests/core/test_run_on_windows.py` checks that a `Machine` is refused there.
- **S3,** in the same file and lanes, with a fake Slurm of Sophios's own, `tests/core/fake_slurm.py` (`sbatch`,
  `squeue`, `sacct`, `scancel`, `sinfo`, as much of each as Sophios and Toil ask; a job pends for `hold`, a time longer
  than its partition's, or `singleton`): the states property takes Slurm's lives too, every claim, exit status and
  state Slurm ends a leader in, with the ten-minute and two-minute windows, a job that will never start, and the step
  jobs a killed runner left, and a failing `squeue` or `sacct` deciding nothing; Toil's argv on Slurm read back by
  Toil's own parser, the steps' time by Toil's `parse_slurm_time` and by Sophios's reading of a partition's limit,
  over every time `time=` takes; where a run goes, by a table of settings, `SBATCH_` values and `sinfo` rows; and, end
  to end with the real Toil (`--batchSystem slurm`, each step a job of the fake), the runner table's Slurm column, `sbatch` given none of the `SBATCH_` variables of the header's options, a failed run, a cancel of a running
  and of a pending one, a leader that will never start, Toil's leader killed hard with a step job left, which is
  cancelled, and a submit whose answer is lost before or after Slurm took the job, with a second leader of the same
  folder that waits for the first (`singleton`) and exits at its claim.
- **S4,** in the same file and lanes: the walk over `targets.TARGETS` runs Nextflow on Slurm too, end to end in the
  Nextflow Runtime step with the pinned Nextflow and the fake Slurm (about 20 s more there): on Slurm the leader and
  each job its runner submits, Toil's step or Nextflow's task, carry the run's comment, account, partition and time, as
  the fake Slurm read them from `sbatch`, under a login profile that sets other `SBATCH_` values, which every job
  sources again (§19); on Slurm Nextflow's walk adds a step that asks for a GPU, whose task is a job on the partition
  with GPUs asking for its GPU (`--gres`), as the closures of the run's configuration send it (Toil runs a GPU step only
  where cwltool finds a GPU, so its walk has none); Nextflow's task's `sbatch` gets none of the `SBATCH_` variables of
  its header's options, nor `SBATCH_ERROR`; and `executor.queueSize` is the capacity Nextflow's log gives its executor.
  In the package job, the property over Toil's argv also reads Nextflow's settings for the same run, the block the site
  appends to its configuration, against what Toil's own parser read (`queueSize` its `--maxJobs`, the partition and the
  GPU partition, the steps' time, none for no limit, the comment and account); the conventions' table places a GPU step
  alike under either runner, since where a step goes no longer depends on its runner; and `check` on Slurm refuses a
  Nextflow process with a container (`wic060`).
- **No cluster probes** (the compute design's decision 38). By hand on one of the owner's clusters before a site
  leaves "experimental": the claim and `exit` over NFS, a cleanup cut short by `KillWait`, `module load` under
  `set -u`, Apptainer across nodes, Nextflow's head job under the partition's default memory.

## 17. What S2 found while it was built

Each is the smallest change that keeps the design's intent.

1. **No `setsid` on macOS.** A workstation may be a Mac, which has no `setsid`. The submit call runs `set -m` before
   `nohup bash -l <run>/job.sh ... &`: bash's job control puts the leader in a process group of its own, whose id is
   its pid, the claim, which `cancel()` signals. Measured with bash 3.2 (macOS) and 5 (Linux).
2. **The traps follow the claim at once** (Appendix A): a line of the environment that fails under `set -e` (a
   `module load`, say) is recorded in `exit` as the job's failure rather than read as a hard kill, and a TERM before
   the runner starts is a cancel. A leader that loses the claim exits before it sets them, so it records nothing.
3. **Toil's scratch folders are made before Toil starts:** Toil 9.5 refuses a `--workDir` that does not exist
   (measured), so `job.sh` makes `<run>/toil/work` and `<run>/toil/tmp`, as it makes Nextflow's output-target
   folders, and goes to `<run>` (`--chdir` on Slurm), or to the folder `launch` names.
4. **What a login profile prints is never read as the script's:** the guard prints the script's hash before it runs
   it, and Sophios reads only what follows. A profile that prints a banner is common; one that reads its input is
   refused, `wic057`.
5. **The range Sophios supports** for a runner on a site is a floor: Toil 9.5.0 and Nextflow 26.04.2, the versions
   this design was read against. The version is the
   last `a.b.c` that `<bin>/<program> --version` (`-version` for Nextflow) prints.
6. **On a machine, Toil's `single_machine` holds each step to the machine's memory:** it gives no more than the
   machine has, and refuses a step that asks for more when it would start it (measured with Toil 9.5 under
   `--maxMemory 2Gi`: a step that declares no memory fails, asking for 4 GiB; one with `ramMin` runs). So the memory
   note holds on a machine as on Slurm: `check()` notes, `wic061`, each Toil step whose tool declares no memory,
   which a machine with less than 4 GiB fails. A step declares memory as Toil reads it: the `ResourceRequirement`
   that wins by cwltool's lookup (the tool's own requirement, else the one it inherits, else its own hint, else the
   hint it inherits) names `ramMin` or `ramMax`.
7. **Decision 18's exception lands with S2,** the first PR that runs a generated pipeline on a site and sets one of
   its settings (`executor.queueSize`, in the block it appends to the run's configuration, decision 25), so the
   Nextflow design's non-goal says so from the PR that makes it true. S4 adds Slurm's settings to the same block, from
   the same function.
8. **On a machine, a run unclaimed 10 minutes after its submit is claimed `none`,** as on Slurm, so a job script that
   died before its claim (a login profile that exits, say) does not read QUEUED for ever. A hard kill's reason names
   `<run>/toil` only for Toil, and only without `exit`, since with one the trap ran; a machine has no step jobs.
9. **`outcome` reads each file it names in a call of its own, once:** the ended status is kept, so however often
   `status()` is asked, a run's outcome is read once.
10. **The codes a site reuses** (§11): real-time analyses are `api008`, `run_async`'s, whose meaning (a way of
    running that never runs them) is the site's; a path with `..`, `~` or `$` is `wic016`; an `outdir` that exists
    is `wic021`.
11. **Sophios on Windows itself makes no site:** `Machine(...)` raises `wic057` when it is made, as
    `sophios.compute.Service` refuses Windows when it is made, so neither `check`, `submit` nor `attach` starts
    there: a site's paths are POSIX paths, which Python on Windows reads as its own. Inside WSL, Sophios makes one.
12. **Known limit: a `StepInput(default=...)` File or Directory** is made absolute against the working directory
    when the Python document is built (`_with_defaults_resolved`), before any target or site sees it, so on a site it
    names a path here. It sits in the document, not the job, so neither the pre-flight nor a site's `check` reads it,
    and the runner there reports it. Bind such a value on a workflow input instead; reading defaults against `root`
    would change the document builder, which S2 leaves as it is.

## 18. What S3 found while it was built

Each is the smallest change that keeps the design's intent.

1. **`reason` decides first** (Appendix B): `status()` stops a run Slurm will never start as `cancel()` does, whose
   claim or TERM would otherwise read CANCELLED. A run is stopped for a step job only while its runner has not exited.
2. **One `squeue` lists the user's jobs with their comment and name** (`-o '%i|%T|%r|%k|%j'`), not only those named
   after the run: the leaders by name, Toil's step jobs by the run's comment, for a step job that will never start and
   for those a killed runner left. `scancel` has no comment filter, so `status()` cancels those by id, whenever Slurm
   tells it the leader ended; the reason does not name them. Submit's adoption asks `squeue -n <id>` as designed.
3. **Asked with the folder, decided after it.** `squeue` and `sacct` answer in the call that reads the folder, before
   its files, as `ps` does on a machine; the decision reads them only when the folder has not decided, so a failed
   one raises only then (`wic057`, as ssh not answering does), and nothing is written or cancelled.
4. **One decision for both schedulers.** A machine's `none` claim, which S2 wrote in the reading call, is decided by
   the same function as Slurm's and written by a second call that reads the claim back: an end is reported only once
   it holds; while the claim cannot be written, or a leader took it first, the run reads QUEUED.
5. **"Neither squeue nor sacct has a job of the run"** counts a job that ended at least two minutes ago without
   claiming the run (its login profile exited, a `scancel` by hand while it pended) as none, so such a run is claimed
   `none` ten minutes after its submit rather than read QUEUED for ever.
6. **A time whose days are 0 is refused** (`0-12`): Toil 9.5 reads it as 12 minutes, Slurm as 12 hours. Found by the
   differential test of the steps' time against Toil's own parser.
7. **The `SBATCH_` variables unset** are sbatch(1)'s for each option the header writes, `--no-requeue`'s two
   (`SBATCH_NO_REQUEUE`, `SBATCH_REQUEUE`) among them; `--comment`, `--chdir` and `--dependency` have none. An option
   the header does not write keeps yours: the leader takes your `SBATCH_MEM_PER_NODE`, say.
8. **`SLURM_CLUSTERS` is refused with `SBATCH_CLUSTERS`:** `sbatch` reads either.
9. **No cwltool on Slurm** (decision 22, 2026-11-11). As built first, cwltool ran every step inside the leader, which
   asked for the largest step's cores and memory and for no GPU, so a GPU step under it was refused. Toil, the
   default, runs each step as a job of its own on the partition it needs, so `runner='cwltool'` is refused, `wic058`,
   and the leader asks for neither cores nor memory.
10. **The code S3 adds,** `wic062` (§11), covers each problem of Slurm's that `check()` finds, and Nextflow on Slurm,
    which S4 lifts. `sacct --starttime` is the submit's time, `run.json`'s, read on the site (`date -r run.json`); the
    two minutes are counted on the site's clock against `sacct`'s End (`SLURM_TIME_FORMAT=%s`).
11. **A stop is done before it is recorded.** On Slurm, `cancel()`, and `status()` when it stops a run, run `scancel`
    first, then the claim, and `status()` writes `reason` last; a `scancel` Slurm refuses ends the call, which raises
    (`wic057`) having written nothing, so the next `status()` decides again. `scancel` by name stops a job whether or
    not it has claimed the run, so none starts in between; on a machine the claim still comes first, since TERM
    reaches only a claimed pid. The step jobs a killed runner left are cancelled the same way. `scancel` exits 0 for a
    job that has ended and for a name no job has (Slurm's `scancel.c`), so only a Slurm that does not answer fails it.
12. **An `exit` above 128 decides alone once Slurm lists its leader no more,** in neither `squeue` nor `sacct` (its
    record purged): FAILED, by the signal, and the step jobs left are cancelled. While Slurm lists the leader, its
    state still gives the cause. A claimed run with no `exit` whose leader Slurm lists nowhere reads RUNNING: just
    after its end, before `sacct` records it, `exit` may not show yet over NFS.

## 19. What S4 found while it was built

Each is the smallest change that keeps the design's intent.

1. **Nextflow reads no Slurm time.** The pinned Nextflow stops a run given the time `2-00:00:00` or `30:00` ("Not a
   valid `time` value", measured), so a site gives the steps' time in seconds, `time = '172800s'`, which Nextflow
   writes as `#SBATCH -t 48:00:00` (measured). A steps' time of `0`, no limit, gives no `time`. The seconds are
   Sophios's reading of the time, which the property over Toil's argv holds to Toil's `parse_slurm_time`.
2. **A GPU step under Nextflow on Slurm goes to the partition with GPUs, asking for its GPUs** (decision 25), as one
   under Toil does: Nextflow's Slurm executor reads no `accelerator` (measured with 26.04.2 and 26.10.5: a task with `accelerator 1`
   gets no `--gres`), so for a workflow with GPU steps the site's `queue` and `clusterOptions` are each a closure on
   `task.accelerator`, which Nextflow evaluates per task: a task with one gets the GPU partition and
   `--gres=gpu:<count>`, every other the run's partition (measured: the fake Slurm's `sbatch` reads them so). The
   GPU partition is found as for Toil (`gpu_partition=`, else the one partition with GPUs), so where a step goes no
   longer depends on its runner. Every task keeps the one steps' time (decision 24). The Nextflow target gives Docker
   no `--gpus`, so a GPU step's container sees a GPU only where the engine shows one by default; that is the Nextflow
   target's to change, here and locally alike.
3. **The pinned Nextflow verifies what the site sets by a run against the fake Slurm** (§16): the job of each task
   carries the partition, time, comment, account and GPUs the site gave Nextflow, as `sbatch` read them, and
   `executor.queueSize` is the capacity Nextflow's log gives its executor, on a machine and on Slurm.
4. **One function gives Toil's `--slurmArgs=` and Nextflow's `clusterOptions` their text,** the run's comment and
   account, which Nextflow writes as one `#SBATCH` line, as it writes any `clusterOptions` string; and one,
   `_nextflow`, writes every setting a site gives Nextflow. No value a site takes holds a quote (each matches its
   setting's pattern), so each is a Groovy string as it is.
5. **Nextflow's job unsets, before Nextflow starts, the `SBATCH_` variables of every option Nextflow writes as an
   `#SBATCH` line,** as `submit` does for the leader's header (§18.7). Nextflow writes each task's header
   (measured with 26.04.2 and 26.10.5): `-J`, `-o`, `--no-requeue`, `--signal B:USR2@30`, `-c` and `--mem` for a step that names its cores and
   memory, `-t`, `-p`, and `clusterOptions`, the comment, account and a GPU task's `--gres`. Each variable beats its
   line (sbatch(1)), and the leader's login shell (`#!/bin/bash -l`) sets it again from your profile: a profile's
   `SBATCH_PARTITION` would send every task elsewhere than `partition=` says, its `SBATCH_MEM_PER_NODE` would cut a
   task's `--mem`, and its `SBATCH_OUTPUT` would send each task's log where it may not be written. `SBATCH_ERROR` goes
   too: Nextflow writes `-o` alone, so a task's two streams go to one file, `.command.log`. `--signal`'s variable,
   `SBATCH_SIGNAL`, is one the leader's header never needed. The others are yours, as for the leader: a profile's
   `SBATCH_QOS` reaches every task. Toil gives each option it sets on `sbatch`'s command line, which beats them. The
   fake Slurm reads the account, partition and time variables as Slurm does, and records the variables each `sbatch`
   got.
6. **The Nextflow design's exception, which S2 wrote (§17.7), already names what S4 sets** in the run's
   configuration, so S4 changes no word of it; lifting the non-goal still deletes that sentence whole and changes no
   code of the backend (decisions 18 and 25).

## Appendix A. The leader script

One script for every runner and both schedulers. Sophios fills each `<...>` with a validated, quoted value. On
Slurm, with `toil-cwl-runner`:

```bash
#!/bin/bash -l
#SBATCH --job-name=<id>
#SBATCH --comment=sophios:<id>
#SBATCH --account=<account>
#SBATCH --partition=<partition>
#SBATCH --time=<time>
#SBATCH --output=<run>/run.log
#SBATCH --error=<run>/run.log
#SBATCH --open-mode=append
#SBATCH --chdir=<run>
#SBATCH --no-requeue
#SBATCH --dependency=singleton
set -euo pipefail
ln -s "$SLURM_JOB_ID" <run>/leader 2> /dev/null || [ -L <run>/leader ] || ln -s "$SLURM_JOB_ID" <run>/leader
[ "$(readlink <run>/leader)" = "$SLURM_JOB_ID" ] || exit 0
trap 'code=$?; set +e; chmod -R u+rwX <run>/toil/tmp <run>/toil/work; rm -rf <run>/toil/tmp <run>/toil/work; echo "$code" > <run>/exit.partial; mv <run>/exit.partial <run>/exit' EXIT
trap 'exit 143' TERM
unset "${!TOIL_@}" "${!PYTHON@}"
export PYTHONNOUSERSITE=1 TOIL_HISTORY=False
module load <modules>
export PATH=<bin>:$PATH
export CWL_SINGULARITY_CACHE=<root>/sophios/images
export SINGULARITY_CACHEDIR="${SINGULARITY_CACHEDIR:-<root>/sophios/images/cache}"
export APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-<root>/sophios/images/cache}"
mkdir -p <run>/toil/work <run>/toil/tmp
cd <run>
{ <bin>/toil-cwl-runner <launch's flags, as on #586> \
    --batchSystem slurm --bypass-file-store --leave-tmpdir \
    --no-cwl-default-ram --defaultMemory 4Gi --maxJobs <max_parallel> \
    --workDir <run>/toil/work --tmpdir-prefix <run>/toil/tmp/t --tmp-outdir-prefix <run>/toil/tmp/o \
    --batchLogsDir <run>/batch --slurmPartition <partition> --slurmTime <steps' time> \
    '--slurmArgs=--comment=sophios:<id> --account=<account>' --clean <clean> \
    <the documents> | tee <run>/stdout; } >> <run>/run.log 2>&1
```

**What varies.** Only the scheduler's header, the runner's line and the trap's scratch:

- **On a machine:** no `#SBATCH` lines; the claim takes `$$` for `$SLURM_JOB_ID`, so `leader` names the pid
  `cancel()` signals; Toil runs `--batchSystem single_machine` and no `--slurm*` flags, nor `--batchLogsDir`, which
  holds step jobs' Slurm logs.
- **Nextflow:** `<bin>/nextflow` with `launch`'s flags and none of the site's: what the site sets is the block it
  appends to the run's copy of the generated `nextflow.config` (decision 25): `executor { queueSize = <max_parallel> }`,
  and on Slurm `process { executor = 'slurm'; queue = '<partition>'; time = '<steps' time in seconds>s';
  clusterOptions = '--comment=sophios:<id> --account=<account>' }` (no `time` when the steps have no limit), where for
  a workflow with GPU steps `queue` and `clusterOptions` are each a closure on `task.accelerator`, giving a GPU task
  the GPU partition and `--gres=gpu:<count>`; before it, `mkdir -p` of each folder `write` returned, `cd` to the
  folder `launch` names, and on Slurm `unset` of the `SBATCH_` variables of each option Nextflow writes in a task's
  header, and `SBATCH_ERROR` (§19.5); the trap removes no scratch; no image caches.
- **Written only when needed:** `--account` (both places) with `account=` or your `SBATCH_ACCOUNT`; `module load`
  with `modules=`; `--slurmGPUPartition` for a workflow with GPU steps under Toil; the image caches with
  `singularity`.
- **`clean=`** (Toil only): `'onSuccess'` passes `--clean onSuccess`, and the trap removes `toil/` only when `$code`
  is 0; `'never'` passes `--clean never`, and the trap removes nothing.

**Facts it relies on** (measured, 2026-10-28): `--slurmArgs=` needs its `=`, or Toil refuses the comment; the `TOIL_`
unset keeps a `TOIL_SLURM_ARGS` in your profile from overriding the step flags; `TOIL_HISTORY=False` keeps every run
out of one SQLite file in an NFS home; `--defaultMemory` does nothing without `--no-cwl-default-ram`; a requeued
leader would start on its first attempt's job store; `#!/bin/bash -l` makes `module` and your profile available to
the job; `--error` is written, since an `SBATCH_ERROR` in your profile would take stderr elsewhere. With `set -o
pipefail` the pipeline's status is the runner's, so the EXIT trap records the runner's exit.

## Appendix B. The states

The first row that applies decides.

| State | How `status()` reads it |
|---|---|
| FAILED | `reason` exists, which `status()` wrote when it stopped the run (§18) |
| COMPLETED | `exit` is 0: the runner's outputs, by its backend's `outcome`, are in place |
| CANCELLED | `leader` is `cancelled`; or `exit` is 143 and `sacct` says CANCELLED (on a machine, 143 alone); or no `exit`, and the leader ended CANCELLED at least 2 minutes ago (a cleanup cut short: `<run>/toil` is left) |
| FAILED | `leader` is `none` ("Slurm never took the job"); `exit` is anything else, with the reason from the backend's `outcome` (the failed steps) or `sacct` (TIMEOUT is "time limit"; PREEMPTED; OUT_OF_MEMORY); or a hard kill: `exit` above 128 but not 143, or no `exit` and the leader ended at least 2 minutes ago. A hard kill's reason says `<run>/toil` is left, and its step jobs are cancelled |
| QUEUED | no claim, and a job named after the run is pending (Slurm's reason); or no job listed yet, within 10 minutes of the submit |
| RUNNING | the claimed leader runs; or it ended less than 2 minutes ago and no `exit` shows yet |

- **The folder decides first.** Once `exit`, or a `none` or `cancelled` claim, exists, it alone decides; Slurm is
  asked then only for the cause of an `exit` above 128, which decides alone once Slurm lists its leader no more
  (§18), and for the step jobs a killed runner left.
- **Otherwise Slurm is asked:** `squeue -a -h -u "$(id -un)" -o '%i|%T|%r|%k|%j'`, the run's jobs by its comment
  and its leaders by its name (§18), then `sacct -n -X -P --name=<id> --starttime=<submit> -o JobID,State,End`.
  Without `-a` a hidden partition's jobs are not listed; without `-j`, `sacct` lists only today's.
- **A failed `squeue` or `sacct` decides nothing:** it raises, and nothing is written or cancelled. "Not listed" comes
  only from a `squeue` that exited 0. The 2 minutes let `exit` show over NFS, which caches entries up to 60 s.
- **A leader or step job that will never start** (`PartitionTimeLimit`, `QOSMaxWallDurationPerJobLimit`, ...):
  `status()` cancels as `cancel()` does, then writes `reason`, naming the setting to change (§18).
- **On a machine**, `ps -ww -o args= -p <pid>` naming `<run>/job.sh` is RUNNING, read before the files (a job found
  gone has left its `exit`); gone with no `exit` is a hard kill. `started_at` is the claim's time, `ended_at`
  `exit`'s, `outputs` and `steps` from the backend's `outcome`. A run no leader has claimed 10 minutes after its
  submit is claimed `none` and FAILED, as on Slurm (§17).
