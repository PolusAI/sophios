cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs: {}
outputs:
  output_structure_file:
    type: File
    outputBinding: {glob: s}
  output_tpr_file:
    type: File
    outputBinding: {glob: t}
  output_report_file:
    type: File
    outputBinding: {glob: r}
