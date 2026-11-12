cwlVersion: v1.2
class: CommandLineTool
doc: Print an array of arrays of files.
baseCommand: echo
inputs:
  files: {type: {type: array, items: {type: array, items: File}}, inputBinding: {position: 1}}
outputs: {}
