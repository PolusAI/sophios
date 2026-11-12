cwlVersion: v1.2
class: CommandLineTool
doc: Requirements written as a class name.
requirements: DockerRequirement
baseCommand: echo
inputs:
  x: {type: string, inputBinding: {position: 1}}
outputs: {}
