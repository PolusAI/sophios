"""Run a workflow on another computer, as yourself, over your own ssh: a site (design_docs/sites-design.md).

A core feature, but not part of the Python API: `sophios.api.python` neither exports nor knows it.

    from sophios.sites import Machine, Slurm

    hpc = Slurm('me@cluster', root='/project/me/cp', bin='/project/me/envs/run/bin')
    ec2 = Machine('my-ec2', root='/data/cp', bin='/opt/conda/envs/run/bin')   # a Host in ~/.ssh/config
    run = hpc.submit(workflow)            # prints the run's id, its runner and its settings, and where its outputs go
    status = run.wait()

Sophios compiles and lowers the workflow here, through `sophios.targets`, and writes the run's files here. It copies
those files, never the data, into the run's folder there, `<root>/sophios/runs/<id>`, and starts the runner from
`bin`: as one leader job on Slurm, or as a process of its own on a machine, which outlives ssh and Python. Nothing of
Sophios is installed there, and nothing of a site is stored here: the run folder is the record, and `repr(run)` is
the line that follows the run from any computer. A relative path the workflow reads is read against `root` there, as
it is read against the working directory here.

A `Machine` is a computer of your own, macOS or Linux: a workstation or a cloud instance, never a login node. A
`Slurm` is a cluster's login node, whose compute nodes share its filesystem.
"""
from ._run import Run
from ._site import Machine, Slurm

__all__ = ['Machine', 'Run', 'Slurm']
