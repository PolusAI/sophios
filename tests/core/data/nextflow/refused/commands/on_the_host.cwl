cwlVersion: v1.2
class: CommandLineTool
doc: A tool that names its own container, beside a ShellCommandRequirement it inherits.
requirements:
  DockerRequirement: {dockerPull: debian}
baseCommand: [echo, hi]
inputs: {}
outputs: {}
