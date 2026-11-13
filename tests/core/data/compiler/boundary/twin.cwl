cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
$namespaces: {myns: 'http://tool.example/', edam: 'https://edamontology.org/', s: 'https://schema.org/'}
inputs: {f: {type: File, format: 'myns:format_1'}}
outputs: {}
