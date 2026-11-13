cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs:
  left:
    type: File
    inputBinding: {position: 1}
    format: edam:format_2330
  right:
    type: File
    inputBinding: {position: 1}
    format: edam:format_2330
outputs: {}
$namespaces: {edam: 'https://edamontology.org/'}
