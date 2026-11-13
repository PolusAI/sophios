cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs: {}
outputs:
  output_tpr_file:
    type: File
    outputBinding: {glob: t}
  output_report_file:
    type: File
    outputBinding: {glob: r}
