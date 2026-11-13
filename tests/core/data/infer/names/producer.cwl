cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs: {}
outputs:
  output_other:
    type: File
    outputBinding: {glob: o}
  output_structure:
    type: File
    outputBinding: {glob: s}
