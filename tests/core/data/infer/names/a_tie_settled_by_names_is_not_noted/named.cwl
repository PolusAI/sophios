cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs: {}
outputs:
  output_file:
    type: File
    outputBinding: {glob: a}
  output_other:
    type: File
    outputBinding: {glob: b}
