"""Run a workflow on another computer, as yourself, over your own ssh: a site (design_docs/sites-design.md).

A core feature, but not part of the Python API: `sophios.api.python` neither exports nor knows it.

    from sophios.sites import Machine, RunConfig

    ec2 = Machine('ssh://me@my-ec2', runs='/data/runs', bin='/opt/conda/envs/run/bin')
    run = ec2.submit(workflow, RunConfig('results').relative_to('/data/project'))   # prints its id and settings
    status = run.wait()

Sophios compiles and lowers the workflow here, through `sophios.targets`, and writes the run's files here, as a local
run writes them. It copies those files, never the data, into the run's folder there, `<runs>/<id>`, and starts the
runner from `bin` as a process of its own, which outlives ssh and Python. Nothing of Sophios is installed there, and
nothing of a site is stored here: the run folder is the record, and `repr(run)` is the line that follows the run from
any computer. A run takes the `RunConfig` a local run and the compute service take: its outputs folder is where the
outputs land, and its `relative_to` is where a relative path is read from there.

A `Machine` is a computer of your own, macOS or Linux: a workstation or a cloud instance, never a login node.
"""
from ..runtime.runners import RunConfig, SlurmSettings
from ._run import Run
from ._site import Machine

__all__ = ['Machine', 'Run', 'RunConfig', 'SlurmSettings']
