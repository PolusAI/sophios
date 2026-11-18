"""A tool, not a workflow: count the words of a file. The .wic beside it says the same."""
from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl


def tool() -> CommandLineTool:
    """`wc -w` on a file, in a container (a hint), its standard output kept as counts.txt."""
    inputs = Inputs(text=Input(cwl.file))
    return (CommandLineTool('tally_words', inputs, Outputs(counts=Output.stdout()))
            .docker('debian:bookworm-slim', as_hint=True).stdout('counts.txt').command('wc', '-w', inputs.text))
