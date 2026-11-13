cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
$namespaces: {edam: 'https://edamontology.org/'}
inputs: {file: {type: File, format: 'edam:format_src1'}}
outputs: {file: {type: File, format: 'edam:format_dst1'}}
