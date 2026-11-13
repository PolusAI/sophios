cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs: {}
outputs:
  output_log_path:
    type: File
    outputBinding: {glob: a}
