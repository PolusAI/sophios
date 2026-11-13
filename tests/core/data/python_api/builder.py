"""A workflow built from objects, as a user's script builds one. builder.cwl beside it is the CWL `workflow().compile()`
returns, and builder_job.yml its job; each comment says what they show of the line below it."""
from pathlib import Path

from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl
from sophios.api.python.workflow import Step, StepInput, Workflow

HERE = Path(__file__).resolve().parent


def workflow() -> Workflow:
    """Emit a message to a file, count it in a child workflow, make a folder, and print a file on another machine."""
    emit = (CommandLineTool('emit_text', Inputs(message=Input(cwl.string, position=1)),
                            Outputs(file=Output(cwl.file, glob='stdout.txt')))
            .base_command('echo').stdout('stdout.txt').to_step())
    count = (CommandLineTool('count', Inputs(file=Input(cwl.file, position=1), scale=Input(cwl.float, flag='-s')),
                             Outputs(counts=Output.stdout()))
             .base_command('wc', '-c').stdout('counts.txt').to_step())
    # A child workflow: its step reads the child's formal parameters, which the parent binds by name.
    child = Workflow([count], 'child')
    count.inputs.file = child.inputs.file
    child.inputs.file = emit.outputs.file
    child.outputs.counts = count.outputs.counts
    count.inputs.scale = 0.5
    # An output directory the step creates: a string input the runner fills, which its glob reads back.
    folder = Step(clt_path=HERE / 'write_dir.cwl')
    folder.inputs.outDir = Path('result.outDir')
    # A path is bound as written, read from no disk: it may name a file on a cluster.
    remote = Step(clt_path=HERE / 'cat.cwl')
    remote.inputs.f = Path('/cluster/images.ome.zarr')
    flow = Workflow([emit, child, folder, remote], 'builder')
    # A record on an input the tool does not declare, read only by `when:`, does not type the workflow input it reads:
    # the plain binding after it does. Reading `flow.message` declares that input, and warns of the clearer spelling.
    remote.inputs.go = StepInput(source=flow.message)
    remote.when = '$(inputs.go == "hello")'
    emit.inputs.message = flow.inputs.message
    # A value bound on a workflow input is its job value, and a workflow output may pass a workflow input through.
    flow.inputs.message = 'hello'
    flow.outputs.said = flow.inputs.message
    # A workflow output whose name starts with `_` is declared by binding it.
    setattr(flow.outputs, '_Counted', child.outputs.counts)
    return flow
