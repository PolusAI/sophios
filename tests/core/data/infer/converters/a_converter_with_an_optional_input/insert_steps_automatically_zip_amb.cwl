cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
$namespaces: {edam: 'https://edamontology.org/'}
inputs: {file: {type: File, format: 'edam:format_zip'}, extra: {type: 'File?', format: 'edam:format_crd'}}
outputs: {file: {type: File, format: 'edam:format_amb_top'}}
