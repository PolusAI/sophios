cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
$namespaces: {myns: 'http://tool.example/', edam: 'http://edamontology.org/', s: 'http://schema.org/'}
inputs: {f: {type: File, format: 'myns:format_1'}}
outputs: {}
