cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
$namespaces: {edam: 'https://edamontology.org/'}
inputs: {p2: {type: File, format: 'edam:format_amb_top'}, p1: {type: File, format: 'edam:format_crd'}}
outputs: {}
