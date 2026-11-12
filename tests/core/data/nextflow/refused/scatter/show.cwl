cwlVersion: v1.2
class: CommandLineTool
doc: Print a file.
baseCommand: echo
inputs:
  file: {type: File, inputBinding: {position: 1}}
outputs: {}
