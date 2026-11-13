cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs: {}
outputs:
  first:
    type: File
    outputBinding: {glob: first}
  last:
    type: File
    outputBinding: {glob: last}
