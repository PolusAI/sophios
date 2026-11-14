cwlVersion: v1.2
class: CommandLineTool
baseCommand: echo
inputs:
  message:
    type: strin  # codespell:ignore strin
    inputBinding: {position: 1}
outputs: {}
