cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
$namespaces: {edam: 'https://edamontology.org/'}
inputs: {top: {type: File, format: 'edam:format_zip'}, crd: {type: File, format: 'edam:format_crd'}}
outputs: {file: {type: File, format: 'edam:format_amb_top'}}
