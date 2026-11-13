cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs:
  left:
    type: File
    inputBinding: {position: 1}
  right:
    type: File
    inputBinding: {position: 1}
outputs: {}
