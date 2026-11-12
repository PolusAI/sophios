cwlVersion: v1.2
class: CommandLineTool
doc: Print a file, if there is one.
baseCommand: echo
inputs:
  file: {type: 'File?', inputBinding: {position: 1}}
outputs: {}
