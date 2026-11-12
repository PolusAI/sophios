cwlVersion: v1.2
class: CommandLineTool
doc: Make a file named by a backtick and then the input.
baseCommand: touch
arguments: ['`$(inputs.name).txt']
inputs:
  name: string
outputs:
  result:
    type: File
    outputBinding: {glob: '`$(inputs.name).txt'}
