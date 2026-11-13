# A tool that fails with status 3.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: [sh, -c, 'exit 3']
inputs:
  message: string
outputs: {}
