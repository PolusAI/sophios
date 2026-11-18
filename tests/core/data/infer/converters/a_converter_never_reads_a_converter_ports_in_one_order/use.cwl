cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
$namespaces: {edam: 'https://edamontology.org/'}
inputs: {p1: {type: File, format: 'edam:format_crd'}, p2: {type: File, format: 'edam:format_amb_top'}}
outputs: {}
